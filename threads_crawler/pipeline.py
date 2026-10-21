"""Crawl workflow shared by the CLI and the web UI."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import httpx

from .crawler import DEFAULT_PROFILE_DIR, ThreadsCrawler, open_browser
from .storage import Store

log = logging.getLogger(__name__)


@dataclass
class CrawlTarget:
    query: str


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
                on_progress(base, f"Tìm '{target.query}'...")
                try:
                    posts = await crawler.search(target.query, limit=opts.limit, recent=opts.recent)
                except Exception as e:  # một từ khóa lỗi không làm hỏng cả lượt crawl
                    log.exception("Search failed: %s", target.query)
                    stats.errors.append(f"{target.query}: {e}")
                    continue
                posts = [p for p in posts if p["like_count"] >= opts.min_likes]
                log.info("'%s': %d bài (>= %d likes)", target.query, len(posts), opts.min_likes)

                done = [p for p in posts if store.is_done(p["code"])]
                if done:
                    log.info("'%s': bỏ qua %d bài đã làm video", target.query, len(done))
                    posts = [p for p in posts if p not in done]
                for i, post in enumerate(posts, 1):
                    on_progress(base + span * i / (len(posts) + 1), f"'{target.query}': bài {i}/{len(posts)}")
                    replies = []
                    if opts.replies > 0 and post["reply_count"] > 0:
                        try:
                            replies = await crawler.replies(post, limit=opts.replies)
                        except Exception as e:
                            stats.errors.append(f"{post['url']}: {e}")
                    for p in (post, *replies):
                        store.download_avatar(p, client)
                        if opts.media:
                            store.download_media(p, client)
                    store.save(post, query=target.query)
                    for r in replies:
                        store.save(r, query=target.query, parent_code=post["code"])
                    stats.posts += 1
                    stats.replies += len(replies)
    on_progress(1.0, f"Xong: {stats.posts} bài, {stats.replies} bình luận")
    return stats
