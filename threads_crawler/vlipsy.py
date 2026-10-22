"""Tải meme video từ vlipsy.com (mặc định: mục Trending, theo thứ tự đang hot) vào materials/meme_video/.

Trang chỉ nhúng sẵn vài chục clip đầu; phần còn lại tải bằng một Next.js server action nhận
body `[offset, limit]`. Mã action đổi theo mỗi lần site deploy nên ta mở trang bằng Playwright,
cuộn để trang tự gọi action một lần, bắt lấy header của request đó rồi gọi lại với offset khác.
Clip được xếp vào nhóm cảm xúc (thư mục con) theo tag/tiêu đề tiếng Anh.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from pathlib import Path
from urllib.parse import quote

import httpx
from playwright.async_api import Page, async_playwright

from .media import KINDS, MATERIALS_DIR

log = logging.getLogger(__name__)

BASE_URL = "https://vlipsy.com"
PAGE_SIZE = 20
CRAWL_DELAY = 1.0  # robots.txt: Crawl-delay: 1
FALLBACK_CATEGORY = "thịnh_hành"

# Nhóm meme (tên thư mục sẵn có) -> từ khóa tiếng Anh trong tag/tiêu đề. Nhóm đứng trước thắng khi hòa điểm.
CATEGORY_KEYWORDS: dict[str, list[str]] = {
    "cười": ["laugh", "laughing", "laughs", "lol", "giggle", "haha", "funny", "hilarious", "chuckle"],
    "buồn_khóc": ["cry", "crying", "cries", "sad", "tears", "sob", "sobbing", "violin", "heartbroken", "weep", "tearing up", "feels"],
    "sốc": ["shock", "shocked", "shocking", "surprised", "surprise", "gasp", "omg", "wow", "whaaat", "jaw drop"],
    "tức_giận": ["angry", "anger", "rage", "mad", "furious", "yelling", "smash"],
    "sợ_hãi": ["scared", "scary", "fear", "afraid", "horror", "terrified", "panic", "scream", "screaming",
                 "frightened", "terror", "worried", "anxiety", "oh no"],
    "vỗ_tay": ["clap", "clapping", "applause", "slow clap"],
    "nhảy": ["dance", "dancing", "dances", "shimmy", "groove", "twerk"],
    "vui_mừng": ["happy", "celebrate", "celebration", "excited", "yay", "cheer", "cheering", "victory", "hooray"],
    "yêu": ["love", "kiss", "heart", "romantic", "crush", "blush"],
    "từ_chối": ["no", "nope", "refuse", "denied", "absolutely not", "no no no"],
    "bỏ_đi": ["leave", "leaving", "walk away", "walking away", "bye", "goodbye", "exit", "i'm out"],
    "cạn_lời": ["speechless", "facepalm", "crickets", "silence", "disappointed", "bruh", "blank stare", "stare"],
    "khó_hiểu": ["confused", "confusion", "huh", "not sure", "thinking", "what", "wait"],
    "nghi_ngờ": ["suspicious", "sus", "side eye", "side-eye", "doubt", "hmm", "skeptical", "squint",
                  "watching", "stalking", "lurking", "investigating", "detective"],
    "mệt_mỏi": ["tired", "exhausted", "sleepy", "bored", "sigh", "yawn", "groan"],
    "ngượng": ["awkward", "embarrassed", "cringe", "nervous", "shy", "sweating"],
    "ghê_tởm": ["disgust", "disgusted", "gross", "eww", "ew", "vomit", "yuck"],
    "tự_đắc": ["smug", "proud", "deal with it", "flex", "evil laugh", "cool", "boss", "sigma"],
}


def categorize(clip: dict) -> str:
    meta = clip.get("metadata") or {}
    text = " ".join([meta.get("title") or "", meta.get("from") or "", *(meta.get("tags") or [])]).lower()
    best, best_score = FALLBACK_CATEGORY, 0
    for cat, words in CATEGORY_KEYWORDS.items():
        score = sum(len(re.findall(rf"(?<![a-z]){re.escape(w)}(?![a-z])", text)) for w in words)
        if score > best_score:
            best, best_score = cat, score
    return best


def source_url(source: str) -> str:
    """'trending' -> mục Trending; chữ khác -> trang tìm kiếm của từ đó (vd 'green screen')."""
    if source == "trending":
        return f"{BASE_URL}/category/trending"
    return f"{BASE_URL}/search/{quote(re.sub(r'\s+', '-', source.strip().lower()))}"


def _parse_action(text: str) -> list[dict]:
    """Response dạng RSC: các dòng `n:<json>`; dòng có {"data": [...]} chứa danh sách clip."""
    for line in text.splitlines():
        _, _, body = line.partition(":")
        if '"data"' not in body:
            continue
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict) and isinstance(data.get("data"), list):
            return data["data"]
    return []


async def _capture_action(page: Page, url: str) -> dict:
    """Mở trang và cuộn tới khi trang tự gọi action phân trang; trả về header của request đó."""
    is_paging = lambda r: (  # noqa: E731
        r.method == "POST" and "next-action" in r.headers and re.fullmatch(r"\[\d+,\d+\]", r.post_data or "")
    )
    async with page.expect_request(is_paging, timeout=30_000) as info:
        await page.goto(url, wait_until="domcontentloaded")
        await page.wait_for_timeout(2500)
        for _ in range(10):
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await page.wait_for_timeout(1000)
    req = await info.value
    return {k: v for k, v in req.headers.items() if k in ("next-action", "next-router-state-tree", "accept", "content-type")}


async def fetch_clips(source: str = "trending", limit: int = 100, max_duration: float = 15.0) -> list[dict]:
    """Danh sách clip theo thứ tự của site (Trending = đang hot nhất trước), lọc theo thời lượng."""
    url = source_url(source)
    clips: dict[str, dict] = {}
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        try:
            page = await browser.new_page(viewport={"width": 1280, "height": 900})
            headers = await _capture_action(page, url)
            offset, empty = 0, 0
            while len(clips) < limit and empty < 2:
                text = await page.evaluate(
                    "([url, h, body]) => fetch(url, {method: 'POST', headers: h, body}).then(r => r.text())",
                    [url, headers, json.dumps([offset, PAGE_SIZE])],
                )
                batch = _parse_action(text)
                empty = empty + 1 if not batch else 0
                for c in batch:
                    try:
                        duration = float(c.get("duration") or 0)  # có clip trả về dạng chuỗi
                    except (TypeError, ValueError):
                        continue
                    if c.get("id") and 0 < duration <= max_duration:
                        clips.setdefault(c["id"], c)
                log.info("vlipsy %s: offset %d -> %d clip", source, offset, len(clips))
                offset += PAGE_SIZE
                await asyncio.sleep(CRAWL_DELAY)
        finally:
            await browser.close()
    return list(clips.values())[:limit]


def _best_video(clip: dict) -> str | None:
    videos = ((clip.get("media") or {}).get("videos") or {}).values()
    best = max(videos, key=lambda v: (v.get("width") or 0) * (v.get("height") or 0), default=None)
    return best.get("url") if best else None


def existing_ids(base: Path) -> set[str]:
    """Id clip vlipsy đã có trong thư viện (tên file = slug, kết thúc bằng -<id 8 ký tự>)."""
    return {f.stem.rsplit("-", 1)[-1] for f in base.rglob("*.mp4")} if base.exists() else set()


def download_clips(clips: list[dict]) -> dict[str, int]:
    """Tải clip vào materials/meme_video/<nhóm>/<slug>.mp4, bỏ qua clip đã có. Trả về số clip mới mỗi nhóm."""
    base = MATERIALS_DIR / KINDS["memes"][0]
    have = existing_ids(base)
    added: dict[str, int] = {}
    with httpx.Client(timeout=60, follow_redirects=True, headers={"Referer": f"{BASE_URL}/"}) as client:
        for c in clips:
            url = _best_video(c)
            if c["id"] in have or not url:
                continue
            cat = categorize(c)
            dest = base / cat / f"{c.get('slug') or c['id']}.mp4"
            try:
                resp = client.get(url)
                resp.raise_for_status()
            except httpx.HTTPError as e:
                log.warning("Không tải được %s: %s", c.get("slug"), e)
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(resp.content)
            have.add(c["id"])
            added[cat] = added.get(cat, 0) + 1
            log.info("  + %s/%s", cat, dest.name)
    return added
