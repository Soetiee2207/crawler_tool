"""Crawl workflow shared by the CLI and the web UI."""

from __future__ import annotations

import logging
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import httpx

from .crawler import BASE_URL, DEFAULT_PROFILE_DIR, ThreadsCrawler, open_browser
from .extract import merge_post
from .quality import QualityFilter, hot_score
from .storage import Store

log = logging.getLogger(__name__)


POST_URL_RE = re.compile(r"threads\.(?:com|net)/@([\w.]+)/post/([\w-]+)")
USERNAME_RE = re.compile(r"^@?([\w.]{1,30})$")
LINK_QUERY = "link"  # nhãn "từ khóa" của bài crawl theo link (để lọc trong Kho bài)


@dataclass
class CrawlTarget:
    query: str  # từ khóa / link bài / tên tài khoản tùy `kind`
    kind: str = "search"  # "search" | "post" | "user"

    @property
    def label(self) -> str:
        """Giá trị lưu vào cột `query`: tài khoản -> "@tên", link -> "link"."""
        return {"user": f"@{self.query}", "post": LINK_QUERY}.get(self.kind, self.query)


def parse_post_url(text: str) -> tuple[str, str] | None:
    """'https://www.threads.com/@user/post/CODE?xmt=..' -> (user, CODE)."""
    m = POST_URL_RE.search(text)
    return (m.group(1), m.group(2)) if m else None


def parse_username(text: str) -> str | None:
    """'@user', 'user' hoặc link trang cá nhân -> 'user'."""
    text = text.strip()
    if m := re.search(r"threads\.(?:com|net)/@([\w.]+)", text):
        return m.group(1)
    m = USERNAME_RE.match(text)
    return m.group(1) if m else None


@dataclass
class CrawlOptions:
    limit: int = 20  # số bài giữ lại mỗi từ khóa / tài khoản (sau khi lọc, bài hot nhất trước)
    replies: int = 10  # số bình luận gốc mỗi bài (kèm cây trả lời bên dưới)
    branches: int = 3  # số câu trả lời giữ lại ở mỗi bình luận, mọi tầng (lần tới tận cùng)
    expand_pages: int = 60  # chặn số trang bình luận mở thêm mỗi bài để lấy nhánh bị ẩn
    tabs: str = "both"  # tab kết quả tìm kiếm: "both" | "top" (nổi bật) | "recent" (mới nhất)
    quality: QualityFilter = field(default_factory=QualityFilter)
    media: bool = True
    headless: bool = True
    profile: Path = DEFAULT_PROFILE_DIR


@dataclass
class CrawlStats:
    posts: int = 0
    replies: int = 0
    errors: list[str] = field(default_factory=list)
    skipped: Counter[str] = field(default_factory=Counter)  # lý do loại -> số bài


def select_posts(posts: list[dict], opts: CrawlOptions, store: Store, stats: CrawlStats) -> list[dict]:
    """Bỏ bài đã làm video và bài không đạt chất lượng; giữ `limit` bài hot nhất."""
    kept = []
    for p in posts:
        reason = "đã làm video" if store.is_done(p["code"]) else opts.quality.reject(p)
        if reason:
            stats.skipped[reason] += 1
        else:
            kept.append(p)
    kept.sort(key=hot_score, reverse=True)
    return kept[: opts.limit]


async def crawl(
    store: Store,
    targets: list[CrawlTarget],
    opts: CrawlOptions,
    on_progress: Callable[[float, str], None] = lambda frac, msg: None,
) -> CrawlStats:
    """Search each target, fetch replies and media, and save everything to `store`."""
    stats = CrawlStats()
    async with open_browser(opts.profile, headless=opts.headless) as context:
        crawler = ThreadsCrawler(context)
        accept = lambda p: opts.quality.reject(p) is None and not store.is_done(p["code"])  # noqa: E731
        with httpx.Client(timeout=60, follow_redirects=True) as client:
            for ti, target in enumerate(targets):
                base = ti / len(targets)
                span = 1 / len(targets)
                on_progress(base, f"Crawl {target.label if target.kind != 'post' else target.query}...")
                prefetched: dict[str, list[dict]] = {}  # link bài: reply đã lấy cùng lúc với bài
                try:
                    if target.kind == "post":
                        user, code = parse_post_url(target.query) or (None, None)
                        if not code:
                            raise ValueError("link không đúng dạng threads.com/@user/post/...")
                        post, replies = await crawler.thread(
                            f"{BASE_URL}/@{user}/post/{code}", code, limit=opts.replies, branches=opts.branches,
                            max_pages=opts.expand_pages,
                        )
                        if not post:
                            raise RuntimeError("không tìm thấy bài (đã bị xóa hoặc cần đăng nhập?)")
                        # link chỉ định cụ thể thì không lọc, kể cả bài đã làm video (vd. làm phần 2)
                        store.undo_done(code)
                        posts = [post]
                        prefetched[code] = replies
                    elif target.kind == "user":
                        found = await crawler.user_posts(
                            target.query, limit=opts.limit, accept=accept, too_old=opts.quality.too_old
                        )
                        store.touch_source(target.query)
                        posts = select_posts(found, opts, store, stats)
                    else:
                        # hashtag chỉ có một kiểu kết quả
                        tabs = ("top", "recent") if opts.tabs == "both" else (opts.tabs,)
                        merged: dict[str, dict] = {}
                        for tab in tabs[:1] if target.query.startswith("#") else tabs:
                            on_progress(base, f"'{target.label}': tìm ở tab {'Mới nhất' if tab == 'recent' else 'Nổi bật'}...")
                            for p in await crawler.search(
                                target.query, limit=opts.limit, recent=tab == "recent", accept=accept
                            ):
                                merged[p["code"]] = merge_post(merged.get(p["code"]), p)
                        found = list(merged.values())
                        posts = select_posts(found, opts, store, stats)
                except Exception as e:  # một mục lỗi không làm hỏng cả lượt crawl
                    log.exception("Crawl failed: %s", target.query)
                    stats.errors.append(f"{target.query}: {e}")
                    continue
                if target.kind != "post":
                    log.info("'%s': giữ %d / %d bài", target.label, len(posts), len(found))
                    on_progress(base, f"'{target.label}': giữ {len(posts)} / {len(found)} bài thấy được")
                for i, post in enumerate(posts, 1):
                    on_progress(base + span * i / (len(posts) + 1), f"'{target.label}': bài {i}/{len(posts)}")
                    replies = prefetched.get(post["code"], [])
                    # Bài đã có trong kho kèm bình luận (crawl nguồn hằng ngày): chỉ cập nhật số like.
                    known = post["code"] not in prefetched and store.has_replies(post["code"])
                    if not known and post["code"] not in prefetched and opts.replies > 0 and post["reply_count"] > 0:
                        try:
                            replies = await crawler.replies(
                                post, limit=opts.replies, branches=opts.branches, max_pages=opts.expand_pages
                            )
                        except Exception as e:
                            stats.errors.append(f"{post['url']}: {e}")
                    for p in (post, *replies):
                        store.download_avatar(p, client)
                        if opts.media and not known:
                            store.download_media(p, client)
                    store.save(post, query=target.label)
                    if replies:
                        store.replace_replies(post["code"], {r["code"] for r in replies})
                    for r in replies:
                        store.save(r, query=target.label, parent_code=post["code"])
                    stats.posts += 1
                    stats.replies += len(replies)
    if stats.skipped:
        log.info("Đã loại: %s", ", ".join(f"{n} {why}" for why, n in stats.skipped.most_common()))
    on_progress(1.0, f"Xong: {stats.posts} bài, {stats.replies} bình luận")
    return stats
