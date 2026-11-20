"""Lấy thông tin sản phẩm và đánh giá dạng chữ từ trang Shopee, để điền sẵn vào form tab Sản phẩm.

Mở cửa sổ trình duyệt thật và đọc các response JSON mà trang tự tải. Nếu Shopee đòi đăng nhập hoặc xác minh,
người dùng tự làm trong cửa sổ đó; tool không vượt captcha. Video và ảnh của người mua không được tải về:
họ không đồng ý cho dùng để bán hàng, nên chỉ đếm số đánh giá có video.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
import time
from collections.abc import Callable
from pathlib import Path
from urllib.parse import unquote, urlparse

from playwright.async_api import Page, Response, async_playwright

log = logging.getLogger(__name__)

PROFILE_DIR = Path(".browser_profile_shopee")  # tách khỏi hồ sơ Threads; chứa phiên đăng nhập, không commit
IMAGE_BASE = "https://down-vn.img.susercontent.com/file/"
PRICE_SCALE = 100_000  # Shopee lưu giá nhân 100000
INTRO_LEN = 400
LOGIN_WAIT = 180.0  # giây chờ người dùng đăng nhập / xác minh
NEXT_PAGE = ".shopee-page-controller .shopee-icon-button--right"


def is_shopee_url(url: str) -> bool:
    u = urlparse(url.strip())
    host = (u.hostname or "").lower()
    return u.scheme in {"http", "https"} and (host == "shopee.vn" or host.endswith(".shopee.vn"))


def _walk(data):
    stack = [data]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            yield cur
            stack.extend(cur.values())
        elif isinstance(cur, list):
            stack.extend(reversed(cur))  # giữ thứ tự đánh giá như trên trang


class Collector:
    """Nhận diện sản phẩm và đánh giá theo cấu trúc object, vì đường dẫn API của Shopee hay đổi."""

    def __init__(self) -> None:
        self.product: dict | None = None
        self.reviews: dict[str, dict] = {}

    def add(self, data) -> None:
        for d in _walk(data):
            if "rating_star" in d and isinstance(d.get("comment"), str):
                key = str(d.get("cmtid") or d.get("orderid") or d["comment"])
                self.reviews[key] = d
            elif d.get("itemid") and d.get("name") and (d.get("images") or d.get("image")):
                if self.product is None or (d.get("description") and not self.product.get("description")):
                    self.product = d

    async def on_response(self, response: Response) -> None:
        if "/api/" not in response.url or "json" not in response.headers.get("content-type", ""):
            return
        try:
            self.add(json.loads(await response.text()))
        except Exception as e:  # response hủy giữa chừng khi chuyển trang
            log.debug("Bỏ qua response %s: %r", response.url[:80], e)


def format_price(p: dict) -> str:
    def money(v) -> str:
        return f"{round(v / PRICE_SCALE):,}".replace(",", ".") + "đ"

    lo, hi = p.get("price_min") or p.get("price"), p.get("price_max")
    if not lo:
        return ""
    return f"Từ {money(lo)}" if hi and hi != lo else money(lo)


def format_reviews(raw: list[dict]) -> str:
    """Thành các dòng `tên | sao | nội dung` của form; bỏ đánh giá không có chữ."""
    lines = []
    for r in raw:
        text = " ".join(r["comment"].split())
        if text:
            name = str(r.get("author_username") or "").replace("|", " ").strip()
            lines.append(f"{name} | {r.get('rating_star') or ''} | {text}")
    return "\n".join(lines)


def to_result(c: Collector, image: str | None) -> dict:
    p = c.product or {}
    reviews = list(c.reviews.values())
    desc = (p.get("description") or "").strip()
    return {
        "name": p.get("name", ""),
        "price": format_price(p),
        "intro": desc[:INTRO_LEN].rsplit(" ", 1)[0] if len(desc) > INTRO_LEN else desc,
        "reviews": format_reviews(reviews),
        "review_count": sum(bool(r["comment"].strip()) for r in reviews),
        "with_video": sum(bool(r.get("videos")) for r in reviews),
        "image": image,
    }


async def _load_reviews(page: Page, c: Collector, limit: int, stop: Callable[[], None]) -> None:
    idle = 0
    while len(c.reviews) < limit and idle < 4:
        stop()
        before = len(c.reviews)
        await page.mouse.wheel(0, 1500)
        await asyncio.sleep(1.5)
        if c.reviews:  # đã tới phần đánh giá: sang trang kế như người dùng bấm
            button = page.locator(NEXT_PAGE).first
            if await button.count():
                await button.click(timeout=3000)
                await asyncio.sleep(2)
        idle = idle + 1 if len(c.reviews) == before else 0


async def fetch_product(url: str, progress: Callable[[float, str], None], limit: int = 30,
                        profile: Path = PROFILE_DIR) -> dict:
    async with async_playwright() as pw:
        context = await pw.chromium.launch_persistent_context(
            str(profile), headless=False, viewport={"width": 1280, "height": 900}, locale="vi-VN"
        )
        try:
            c = Collector()
            page = await context.new_page()
            page.on("response", c.on_response)
            progress(0.1, "Đang mở trang sản phẩm")
            await page.goto(url, wait_until="domcontentloaded")
            deadline = time.monotonic() + LOGIN_WAIT
            while c.product is None:
                if time.monotonic() > deadline:
                    raise RuntimeError("Không đọc được sản phẩm. Shopee có thể đang chặn hoặc chưa đăng nhập xong.")
                progress(0.2, "Nếu Shopee yêu cầu, hãy đăng nhập hoặc xác minh trong cửa sổ trình duyệt")
                await asyncio.sleep(1)
            progress(0.5, "Đang đọc đánh giá")
            await _load_reviews(page, c, limit, lambda: progress(0.6, f"Đã có {len(c.reviews)} đánh giá"))
            image = None
            names = c.product.get("images") or [c.product.get("image")]
            resp = await context.request.get(IMAGE_BASE + names[0])
            if resp.ok:
                image = "data:image/jpeg;base64," + base64.b64encode(await resp.body()).decode()
            return to_result(c, image)
        finally:
            await context.close()


def parse_item_name(url: str, limit: int = 40) -> str:
    m = re.search(r"/([^/?]+?)-i\.\d+\.\d+", urlparse(url).path)
    return unquote(m.group(1)).replace("-", " ")[:limit] if m else "sản phẩm"
