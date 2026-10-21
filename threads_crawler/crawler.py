"""Playwright-driven crawling: search results and post detail pages (with replies)."""

from __future__ import annotations

import asyncio
import json
import logging
import random
from pathlib import Path
from urllib.parse import quote

from playwright.async_api import BrowserContext, Page, Response, async_playwright

from .extract import extract_posts, merge_post

log = logging.getLogger(__name__)

BASE_URL = "https://www.threads.com"
DEFAULT_PROFILE_DIR = Path(".browser_profile")


class PostCollector:
    """Collect posts from embedded page JSON and GraphQL responses of a page."""

    def __init__(self) -> None:
        self.posts: dict[str, dict] = {}

    def add(self, data) -> None:
        for code, post in extract_posts(data).items():
            self.posts[code] = merge_post(self.posts.get(code), post)

    async def on_response(self, response: Response) -> None:
        if "graphql" not in response.url and "/api/" not in response.url:
            return
        try:
            text = await response.text()
        except Exception:
            return
        # Một số response trả về nhiều dòng JSON nối nhau.
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("for (;;);"):
                line = line[len("for (;;);"):]
            if not line.startswith(("{", "[")):
                continue
            try:
                self.add(json.loads(line))
            except json.JSONDecodeError:
                continue

    async def scan_embedded(self, page: Page) -> None:
        scripts = await page.locator('script[type="application/json"][data-sjs]').all_inner_texts()
        for raw in scripts:
            if '"code"' not in raw:
                continue
            try:
                self.add(json.loads(raw))
            except json.JSONDecodeError:
                continue


async def _human_pause(lo: float = 1.0, hi: float = 2.5) -> None:
    await asyncio.sleep(random.uniform(lo, hi))


async def _scroll_collect(page: Page, collector: PostCollector, limit: int, max_idle: int = 4) -> None:
    """Scroll until `limit` posts are collected or no new posts appear `max_idle` times in a row."""
    idle = 0
    while len(collector.posts) < limit and idle < max_idle:
        before = len(collector.posts)
        await page.mouse.wheel(0, random.randint(2500, 4000))
        await _human_pause()
        await collector.scan_embedded(page)
        idle = idle + 1 if len(collector.posts) == before else 0
        log.info("  ... %d posts", len(collector.posts))


class ThreadsCrawler:
    def __init__(self, context: BrowserContext) -> None:
        self.context = context

    async def _open(self, url: str) -> tuple[Page, PostCollector]:
        page = await self.context.new_page()
        collector = PostCollector()
        page.on("response", collector.on_response)
        await page.goto(url, wait_until="domcontentloaded")
        try:
            await page.wait_for_load_state("networkidle", timeout=10_000)
        except Exception:
            pass
        await collector.scan_embedded(page)
        if "/login" in page.url:
            await page.close()
            raise RuntimeError("Threads yêu cầu đăng nhập. Chạy `threads-crawler login` trước.")
        return page, collector

    async def search(self, query: str, limit: int = 30, recent: bool = False) -> list[dict]:
        """Search posts by keyword or #hashtag. `recent=True` sorts by newest instead of top."""
        url = f"{BASE_URL}/search?q={quote(query)}&serp_type={'recent' if recent else 'default'}"
        if query.startswith("#"):
            url = f"{BASE_URL}/search?q={quote(query[1:])}&serp_type=tags"
        log.info("Search: %s", url)
        page, collector = await self._open(url)
        try:
            await _scroll_collect(page, collector, limit)
        finally:
            await page.close()
        # Search chỉ nên trả về bài gốc, không phải reply lẻ.
        posts = [p for p in collector.posts.values() if not p["is_reply"]]
        return posts[:limit]

    async def replies(self, post: dict, limit: int = 20) -> list[dict]:
        """Open a post page and return its replies (excluding the post itself)."""
        page, collector = await self._open(post["url"])
        try:
            await _scroll_collect(page, collector, limit + 1, max_idle=2)
        finally:
            await page.close()
        # Trang chi tiết còn chứa bài "gợi ý" không liên quan (is_reply=False); chỉ giữ reply thật.
        found = [p for code, p in collector.posts.items() if code != post["code"] and p["is_reply"]]
        # Bài chi tiết có thể chứa phiên bản đầy đủ hơn của post gốc (vd: text dài).
        if full := collector.posts.get(post["code"]):
            post.update(merge_post(post, full))
        return found[:limit]


class open_browser:
    """Async context manager yielding a persistent browser context (keeps login cookies)."""

    def __init__(self, profile_dir: Path = DEFAULT_PROFILE_DIR, headless: bool = True) -> None:
        self.profile_dir = profile_dir
        self.headless = headless

    async def __aenter__(self) -> BrowserContext:
        self._pw = await async_playwright().start()
        self._context = await self._pw.chromium.launch_persistent_context(
            str(self.profile_dir),
            headless=self.headless,
            viewport={"width": 1280, "height": 900},
            locale="vi-VN",
        )
        return self._context

    async def __aexit__(self, *exc) -> None:
        await self._context.close()
        await self._pw.stop()
