"""Crawl workflow shared by the CLI and the web UI."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import httpx

from .crawler import BASE_URL, DEFAULT_PROFILE_DIR, ThreadsCrawler, open_browser
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
    limit: int = 20
    replies: int = 10
    min_likes: int = 0
    recent: bool = False
    media: bool = True
    headless: bool = True
    profile: Path = DEFAULT_PROFILE_DIR


@dataclass
class CrawlStats:
    posts: int = 0
    replies: int = 0
    errors: list[str] = field(default_factory=list)


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
                            f"{BASE_URL}/@{user}/post/{code}", code, limit=opts.replies
                        )
                        if not post:
                            raise RuntimeError("không tìm thấy bài (đã bị xóa hoặc cần đăng nhập?)")
                        posts = [post]
                        prefetched[code] = replies
                    elif target.kind == "user":
                        posts = await crawler.user_posts(target.query, limit=opts.limit)
                    else:
                        posts = await crawler.search(target.query, limit=opts.limit, recent=opts.recent)
                except Exception as e:  # một mục lỗi không làm hỏng cả lượt crawl
                    log.exception("Crawl failed: %s", target.query)
                    stats.errors.append(f"{target.query}: {e}")
                    continue
                if target.kind != "post":  # link chỉ định cụ thể thì không lọc theo like
                    posts = [p for p in posts if p["like_count"] >= opts.min_likes]
                log.info("'%s': %d bài (>= %d likes)", target.label, len(posts), opts.min_likes)

                done = [p for p in posts if store.is_done(p["code"])]
                if done:
                    log.info("'%s': bỏ qua %d bài đã làm video", target.label, len(done))
                    if target.kind == "post":
                        stats.errors.append(f"{target.query}: bài này đã làm video rồi")
                    posts = [p for p in posts if p not in done]
                for i, post in enumerate(posts, 1):
                    on_progress(base + span * i / (len(posts) + 1), f"'{target.label}': bài {i}/{len(posts)}")
                    replies = prefetched.get(post["code"], [])
                    if post["code"] not in prefetched and opts.replies > 0 and post["reply_count"] > 0:
                        try:
                            replies = await crawler.replies(post, limit=opts.replies)
                        except Exception as e:
                            stats.errors.append(f"{post['url']}: {e}")
                    for p in (post, *replies):
                        store.download_avatar(p, client)
                        if opts.media:
                            store.download_media(p, client)
                    store.save(post, query=target.label)
                    for r in replies:
                        store.save(r, query=target.label, parent_code=post["code"])
                    stats.posts += 1
                    stats.replies += len(replies)
    on_progress(1.0, f"Xong: {stats.posts} bài, {stats.replies} bình luận")
    return stats
