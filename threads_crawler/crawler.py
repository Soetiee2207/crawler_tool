"""Playwright-driven crawling: search results and post detail pages (with replies)."""

from __future__ import annotations

import asyncio
import json
import logging
import random
from pathlib import Path
from typing import Callable
from urllib.parse import quote

from playwright.async_api import BrowserContext, Page, Response, async_playwright

from .extract import PostPage, ReplyTree, extract_posts, is_junk_reply, merge_post

log = logging.getLogger(__name__)

BASE_URL = "https://www.threads.com"
DEFAULT_PROFILE_DIR = Path(".browser_profile")


class PostCollector:
    """Collect posts from embedded page JSON and GraphQL responses of a page."""

    def __init__(self) -> None:
        self.posts: dict[str, dict] = {}
        # Khối JSON giữ nguyên cấu trúc cho trang chi tiết. JSON nhúng trong trang (trang bình luận đầu,
        # các mạch hot nhất) phải đứng trước các trang GraphQL tải thêm, dù GraphQL có thể về trước.
        self.embedded: list = []
        self.responses: list = []
        self._scripts: set[int] = set()

    @property
    def blobs(self) -> list:
        return self.embedded + self.responses

    def add(self, data, embedded: bool = False) -> None:
        (self.embedded if embedded else self.responses).append(data)
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
            if '"code"' not in raw or hash(raw) in self._scripts:
                continue
            self._scripts.add(hash(raw))
            try:
                self.add(json.loads(raw), embedded=True)
            except json.JSONDecodeError:
                continue


async def _human_pause(lo: float = 1.0, hi: float = 2.5) -> None:
    await asyncio.sleep(random.uniform(lo, hi))


async def _scroll_collect(
    page: Page,
    collector: PostCollector,
    count: Callable[[], int],
    limit: int,
    max_idle: int = 4,
    max_scrolls: int = 25,
    stop: Callable[[], bool] = lambda: False,
) -> None:
    """Cuộn tới khi `count()` đạt `limit`, hết bài mới `max_idle` lần liền, quá `max_scrolls` lần, hoặc `stop()`.

    `count` đếm bài *đạt yêu cầu* (vd chưa quá cũ), nên tab nổi bật toàn bài cũ vẫn được cuộn tiếp có giới hạn.
    """
    idle = scrolls = 0
    while count() < limit and idle < max_idle and scrolls < max_scrolls and not stop():
        before = len(collector.posts)
        await page.mouse.wheel(0, random.randint(2500, 4000))
        await _human_pause()
        await collector.scan_embedded(page)
        scrolls += 1
        idle = idle + 1 if len(collector.posts) == before else 0
        log.info("  ... %d bài đạt / %d bài đã thấy", count(), len(collector.posts))


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

    async def search(
        self, query: str, limit: int = 30, recent: bool = False, accept: Callable[[dict], bool] | None = None
    ) -> list[dict]:
        """Bài gốc tìm theo từ khóa hoặc #hashtag. `recent=True` lấy tab mới nhất thay vì nổi bật.

        Cuộn tới khi có `limit` bài qua được `accept`; trả về mọi bài gốc đã thấy (kể cả bài không đạt).
        """
        url = f"{BASE_URL}/search?q={quote(query)}&serp_type={'recent' if recent else 'default'}"
        if query.startswith("#"):
            url = f"{BASE_URL}/search?q={quote(query[1:])}&serp_type=tags"
        log.info("Search: %s", url)
        page, collector = await self._open(url)
        # Search chỉ nên trả về bài gốc, không phải reply lẻ.
        roots = lambda: [p for p in collector.posts.values() if not p["is_reply"]]  # noqa: E731
        try:
            await _scroll_collect(page, collector, lambda: sum(map(accept or bool, roots())), limit)
        finally:
            await page.close()
        return roots()

    async def _post_page(self, url: str, code: str, limit: int) -> tuple[dict | None, PostPage]:
        """Mở trang bài, cuộn tới khi có `limit` mạch bình luận đọc được (hoặc hết)."""
        page, collector = await self._open(url)

        def parsed() -> PostPage:
            pp = PostPage()
            for blob in collector.blobs:
                pp.add(blob, code)
            return pp

        try:
            await _scroll_collect(
                page, collector, lambda: sum(not is_junk_reply(t.top) for t in parsed().threads), limit,
                max_idle=2, max_scrolls=10,
            )
        finally:
            await page.close()
        return collector.posts.get(code), parsed()

    async def thread(
        self, url: str, code: str, limit: int = 20, branches: int = 3, max_pages: int = 60
    ) -> tuple[dict | None, list[dict]]:
        """(bản đầy đủ của bài `code`, cây bình luận của nó dàn phẳng theo thứ tự đọc).

        Lấy `limit` bình luận gốc theo thứ tự Threads. Mỗi bình luận giữ tối đa `branches` câu trả lời,
        mỗi câu trả lời lại giữ tối đa `branches` câu trả lời của nó... tới tận cùng. Bình luận nào báo còn
        trả lời mà trên trang chưa hiện đủ thì mở trang riêng của nó để lấy; `max_pages` chặn số trang mở
        thêm cho một bài (~5 giây mỗi trang), duyệt theo thứ tự đọc nên nhánh hot được lấy trước.
        Mỗi bình luận trả về có thêm: rank (thứ tự đọc), thread_code (mã bình luận gốc của nhánh, = `code`
        với phần tác giả viết tiếp), reply_parent (mã bình luận nó trả lời, None nếu trả lời bài),
        depth (0 = bình luận gốc / viết tiếp, 1 = trả lời, 2 = trả lời của trả lời...).
        """
        post, pp = await self._post_page(url, code, limit)
        tree = ReplyTree()
        tree.add_page(code, pp)  # phần tác giả viết tiếp nối thành chuỗi bài → phần 1 → phần 2...
        self_codes = {p["code"] for p in pp.self_thread}
        tops = [k for k in tree.kids(code) if k["code"] not in self_codes][:limit]

        def split(node: dict) -> tuple[list[dict], list[dict]]:
            """(phần người viết tự viết tiếp, các câu trả lời khác ≤ branches). Phần viết tiếp luôn giữ,
            không tính vào giới hạn nhánh: câu chuyện kể nhiều phần không bị cắt giữa chừng."""
            kids = tree.kids(node["code"])
            cont = next((k for k in kids if k["username"] == node["username"]
                         and k["reply_to_username"] == node["username"]), None)
            return ([cont] if cont else []), [k for k in kids if k is not cont][:branches]

        pages = 0
        grown: set[str] = set()

        async def grow(node: dict) -> None:
            nonlocal pages
            if node["code"] in grown:
                return
            grown.add(node["code"])
            cont, others = split(node)
            if len(cont) + len(others) < min(node["reply_count"], branches + 1) and pages < max_pages:
                pages += 1
                log.info("  mở nhánh @%s (%d trả lời)", node["username"], node["reply_count"])
                try:
                    _, sub = await self._post_page(node["url"], node["code"], limit=branches)
                    tree.add_page(node["code"], sub)
                except Exception as e:  # thiếu một nhánh không làm hỏng cả bài
                    log.warning("Không mở được nhánh %s: %s", node["url"], e)
                cont, others = split(node)
            for kid in cont + others:
                await grow(kid)

        for top in pp.self_thread[:1] + tops:
            await grow(top)
        if pages >= max_pages:
            log.warning("Bài %s: đã mở đủ %d trang nhánh, các nhánh sau có thể thiếu", code, pages)

        replies: list[dict] = []
        walked: set[str] = set()

        def walk(node: dict, depth: int, thread_code: str, parent: str | None) -> None:
            """Thứ tự đọc: bình luận, các câu trả lời của nó (thụt vào), rồi phần nó tự viết tiếp (cùng tầng)."""
            if node["code"] in walked:
                return
            walked.add(node["code"])
            replies.append({**node, "thread_code": thread_code, "reply_parent": parent, "depth": depth})
            cont, others = split(node)
            for kid in others:
                # người khác trả lời phần tác giả viết tiếp: thành một mạch bình luận riêng
                walk(kid, depth + 1, kid["code"] if thread_code == code else thread_code, node["code"])
            for kid in cont:
                walk(kid, depth, thread_code, node["code"])

        if pp.self_thread:
            walk(pp.self_thread[0], 0, code, None)
        for top in tops:
            walk(top, 0, top["code"], None)
        for i, r in enumerate(replies):
            r["rank"] = i
        return post, replies

    async def replies(self, post: dict, limit: int = 20, branches: int = 3, max_pages: int = 60) -> list[dict]:
        """Bình luận của bài (xem `thread`); cập nhật `post` bằng bản đầy đủ trên trang chi tiết."""
        full, found = await self.thread(post["url"], post["code"], limit, branches, max_pages)
        # Bài chi tiết có thể chứa phiên bản đầy đủ hơn của post gốc (vd: text dài).
        if full:
            post.update(merge_post(post, full))
        return found

    async def user_posts(
        self,
        username: str,
        limit: int = 30,
        accept: Callable[[dict], bool] | None = None,
        too_old: Callable[[dict], bool] | None = None,
    ) -> list[dict]:
        """Bài gốc (không tính reply, repost của người khác) trên trang cá nhân @username.

        Trang cá nhân xếp mới → cũ (trừ vài bài ghim), nên gặp từ 3 bài `too_old` là dừng cuộn.
        """
        url = f"{BASE_URL}/@{quote(username)}"
        log.info("User: %s", url)
        page, collector = await self._open(url)
        own = lambda: [  # noqa: E731
            p for p in collector.posts.values() if p["username"].lower() == username.lower() and not p["is_reply"]
        ]
        try:
            await _scroll_collect(
                page, collector, lambda: sum(map(accept or bool, own())), limit,
                stop=lambda: too_old is not None and sum(map(too_old, own())) >= 3,
            )
        finally:
            await page.close()
        if not collector.posts:
            raise RuntimeError(f"Không thấy bài nào của @{username} (sai tên hoặc tài khoản riêng tư?)")
        return own()


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
