"""Thu thập green screen meme video từ greenscreenmemes.com vào materials/meme_video/.

Hỗ trợ lấy từ WordPress REST API (bài mới nhất) và Rank Math Video Sitemaps (kho hơn 7000 meme).
Clip được xếp tự động vào nhóm cảm xúc (thư mục con) theo từ khóa trong tiêu đề và mô tả.
"""

from __future__ import annotations

import concurrent.futures
import logging
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import unquote, urlparse

import httpx

from .media import KINDS, MATERIALS_DIR, probe

log = logging.getLogger(__name__)

BASE_URL = "https://greenscreenmemes.com"
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
FALLBACK_CATEGORY = "thịnh_hành"

# Nhóm meme (tên thư mục con) -> từ khóa tiếng Anh trong tiêu đề / mô tả.
CATEGORY_KEYWORDS: dict[str, list[str]] = {
    "cười": [
        "laugh", "laughing", "laughs", "lol", "giggle", "haha", "funny", "hilarious",
        "chuckle", "snicker", "grin", "grinning", "lmao", "rofl", "humor", "joke", "smiling", "smile"
    ],
    "buồn_khóc": [
        "cry", "crying", "cries", "sad", "tears", "sob", "sobbing", "violin",
        "heartbroken", "weep", "tearing up", "feels", "depressed", "depression",
        "unhappy", "sorrow", "broke", "mourn", "grief", "sadness", "disappointed"
    ],
    "sốc": [
        "shock", "shocked", "shocking", "surprised", "surprise", "gasp", "omg",
        "wow", "whaaat", "jaw drop", "stunned", "disbelief", "unbelievable",
        "mind blown", "astonished", "insane"
    ],
    "tức_giận": [
        "angry", "anger", "rage", "mad", "furious", "yelling", "smash",
        "irritated", "annoyed", "pissed", "fume", "aggressive", "fighting", "scowl"
    ],
    "sợ_hãi": [
        "scared", "scary", "fear", "afraid", "horror", "terrified", "panic",
        "scream", "screaming", "frightened", "terror", "worried", "anxiety",
        "oh no", "creepy", "spooky", "freaked out", "nightmare", "chills"
    ],
    "vỗ_tay": [
        "clap", "clapping", "applause", "slow clap", "cheering", "ovation"
    ],
    "nhảy": [
        "dance", "dancing", "dances", "shimmy", "groove", "grooving", "twerk",
        "twerking", "vibing", "ballet", "party walk", "bouncing", "kicking"
    ],
    "vui_mừng": [
        "happy", "celebrate", "celebration", "excited", "yay", "cheer",
        "victory", "hooray", "singing", "joy", "cheerful"
    ],
    "yêu": [
        "love", "kiss", "heart", "romantic", "crush", "blush", "hug",
        "hugging", "cuddle", "sweet", "wholesome", "affection"
    ],
    "từ_chối": [
        "no", "nope", "refuse", "denied", "absolutely not", "no no no",
        "stop", "never", "nah", "disagree", "reject"
    ],
    "bỏ_đi": [
        "leave", "leaving", "walk away", "walking away", "bye", "goodbye",
        "exit", "i'm out", "driving away", "run away", "running away", "escape", "flee", "disappear"
    ],
    "cạn_lời": [
        "speechless", "facepalm", "crickets", "silence", "bruh", "blank stare",
        "stare", "deadpan", "staring"
    ],
    "khó_hiểu": [
        "confused", "confusion", "huh", "not sure", "thinking", "what",
        "wait", "puzzled", "clueless", "wut", "idk", "question"
    ],
    "nghi_ngờ": [
        "suspicious", "sus", "side eye", "side-eye", "doubt", "hmm",
        "skeptical", "squint", "watching", "stalking", "lurking",
        "investigating", "detective", "stealing", "trick", "spy"
    ],
    "mệt_mỏi": [
        "tired", "exhausted", "sleepy", "bored", "sigh", "yawn", "groan",
        "sleep", "sleeping", "nap", "exhaustion", "chilling", "lounging", "relax"
    ],
    "ngượng": [
        "awkward", "embarrassed", "cringe", "nervous", "shy", "sweating"
    ],
    "ghê_tởm": [
        "disgust", "disgusted", "gross", "eww", "ew", "vomit", "yuck", "nasty"
    ],
    "tự_đắc": [
        "smug", "proud", "deal with it", "flex", "evil laugh", "cool",
        "boss", "sigma", "sassy", "swagger", "badass", "win", "cocky"
    ],
}


def categorize(title: str, description: str = "") -> str:
    """Xếp meme vào danh mục tiếng Việt dựa trên từ khóa trong tiêu đề và mô tả."""
    text = f"{title} {description}".lower()
    best, best_score = FALLBACK_CATEGORY, 0
    for cat, words in CATEGORY_KEYWORDS.items():
        score = sum(len(re.findall(rf"(?<![a-z]){re.escape(w)}(?![a-z])", text)) for w in words)
        if score > best_score:
            best, best_score = cat, score
    return best


def existing_filenames(base: Path) -> set[str]:
    """Tập hợp tên file đã có trong materials/meme_video/ (chữ thường, cả stem và name)."""
    if not base.exists():
        return set()
    res = set()
    for f in base.rglob("*.mp4"):
        res.add(f.name.lower())
        res.add(f.stem.lower())
    return res


def _clean_filename(url: str, fallback_slug: str = "") -> str:
    path = unquote(urlparse(url).path)
    name = Path(path).name
    if not name or not name.endswith(".mp4"):
        name = f"{fallback_slug or 'meme'}.mp4"
    name = re.sub(r"[^\w\-.]", "_", name)
    return name


def fetch_from_rest_api(client: httpx.Client, pages: int = 2) -> list[dict]:
    """Lấy các meme mới nhất thông qua WordPress REST API (/wp-json/wp/v2/posts)."""
    clips: list[dict] = []
    for page in range(1, pages + 1):
        try:
            resp = client.get(
                f"{BASE_URL}/wp-json/wp/v2/posts",
                params={"per_page": 50, "page": page},
            )
            if resp.status_code == 400:  # Hết trang
                break
            resp.raise_for_status()
            posts = resp.json()
        except Exception as e:
            log.warning("Lỗi fetch REST posts page %d: %s", page, e)
            break

        if not posts:
            break

        # Gom media_ids từ acf.video_url
        media_ids = [
            p["acf"]["video_url"]
            for p in posts
            if isinstance(p.get("acf"), dict) and p["acf"].get("video_url")
        ]
        media_map: dict[int, str] = {}
        if media_ids:
            try:
                m_resp = client.get(
                    f"{BASE_URL}/wp-json/wp/v2/media",
                    params={"include": ",".join(map(str, media_ids)), "per_page": 100},
                )
                if m_resp.status_code == 200:
                    for m in m_resp.json():
                        if m.get("source_url"):
                            media_map[m["id"]] = m["source_url"]
            except Exception as e:
                log.warning("Lỗi fetch media IDs: %s", e)

        for p in posts:
            video_url = None
            if isinstance(p.get("acf"), dict):
                video_url = media_map.get(p["acf"].get("video_url"))

            title = p.get("title", {}).get("rendered", "")
            desc = p.get("acf", {}).get("video_description", "") if isinstance(p.get("acf"), dict) else ""
            slug = p.get("slug", "")

            if video_url:
                clips.append({
                    "id": p["id"],
                    "title": title,
                    "description": desc,
                    "slug": slug,
                    "url": video_url,
                    "filename": _clean_filename(video_url, slug),
                })
        log.info("greenscreenmemes REST API: page %d -> %d clips", page, len(clips))
    return clips


def fetch_from_sitemaps(client: httpx.Client, max_sitemaps: int = 3) -> list[dict]:
    """Lấy danh sách clip từ Rank Math Video Sitemaps (video-sitemap1.xml, ...)."""
    clips: list[dict] = []
    ns = {
        "sm": "http://www.sitemaps.org/schemas/sitemap/0.9",
        "video": "http://www.google.com/schemas/sitemap-video/1.1",
    }
    for i in range(1, max_sitemaps + 1):
        sitemap_url = f"{BASE_URL}/video-sitemap{i}.xml"
        try:
            resp = client.get(sitemap_url)
            if resp.status_code != 200:
                break
            root = ET.fromstring(resp.content)
        except Exception as e:
            log.warning("Lỗi fetch sitemap %s: %s", sitemap_url, e)
            continue

        for url_node in root.findall("sm:url", ns):
            v_node = url_node.find("video:video", ns)
            if v_node is None:
                continue
            content_loc = v_node.find("video:content_loc", ns)
            if content_loc is None or not content_loc.text:
                continue

            video_url = content_loc.text.strip()
            title_node = v_node.find("video:title", ns)
            desc_node = v_node.find("video:description", ns)
            title = title_node.text.strip() if title_node is not None and title_node.text else ""
            desc = desc_node.text.strip() if desc_node is not None and desc_node.text else ""
            loc_node = url_node.find("sm:loc", ns)
            slug = loc_node.text.strip().rstrip("/").split("/")[-1] if loc_node is not None and loc_node.text else ""

            clips.append({
                "id": video_url,
                "title": title,
                "description": desc,
                "slug": slug,
                "url": video_url,
                "filename": _clean_filename(video_url, slug),
            })
        log.info("greenscreenmemes sitemap %d -> %d total clips", i, len(clips))
    return clips


def fetch_clips(limit: int = 100, category: str | None = None) -> list[dict]:
    """Tổng hợp danh sách clip mới (REST API bài mới nhất + video sitemap), loại trùng."""
    base = MATERIALS_DIR / KINDS["memes"][0]
    existing = existing_filenames(base)

    candidates: dict[str, dict] = {}
    with httpx.Client(
        timeout=30,
        follow_redirects=True,
        headers={"User-Agent": USER_AGENT},
    ) as client:
        # 1. Lấy bài mới nhất từ REST API
        rest_pages = min(5, max(2, (limit // 50) + 1))
        rest_clips = fetch_from_rest_api(client, pages=rest_pages)
        for c in rest_clips:
            fname = c["filename"].lower()
            if fname not in existing and c["filename"] not in candidates:
                cat = categorize(c["title"], c["description"])
                if category is None or cat == category:
                    c["category"] = cat
                    candidates[c["filename"]] = c

        # 2. Nếu chưa đủ limit, lấy thêm từ video sitemaps
        if len(candidates) < limit:
            sitemaps_needed = min(8, max(2, (limit // 500) + 1))
            sitemap_clips = fetch_from_sitemaps(client, max_sitemaps=sitemaps_needed)
            for c in sitemap_clips:
                fname = c["filename"].lower()
                if fname not in existing and c["filename"] not in candidates:
                    cat = categorize(c["title"], c["description"])
                    if category is None or cat == category:
                        c["category"] = cat
                        candidates[c["filename"]] = c
                if len(candidates) >= limit * 2:
                    break

    return list(candidates.values())[:limit]


def _download_single(
    c: dict,
    base: Path,
    max_duration: float,
) -> tuple[str, bool]:
    """Tải 1 clip, kiểm tra duration và lưu vào đúng nhóm cảm xúc."""
    url = c["url"]
    fname = c["filename"]
    cat = c.get("category") or categorize(c["title"], c["description"])
    dest = base / cat / fname
    dest.parent.mkdir(parents=True, exist_ok=True)

    with httpx.Client(
        timeout=60,
        follow_redirects=True,
        headers={"User-Agent": USER_AGENT, "Referer": f"{BASE_URL}/"},
    ) as client:
        try:
            resp = client.get(url)
            resp.raise_for_status()
            dest.write_bytes(resp.content)
        except Exception as e:
            log.warning("Không tải được %s (%s): %s", fname, url, e)
            if dest.exists():
                dest.unlink(missing_ok=True)
            return cat, False

    try:
        info = probe(dest)
        dur = info.get("duration") or 0
        if max_duration and dur > max_duration:
            log.info("Bỏ qua %s: quá dài (%.1fs > %.1fs)", fname, dur, max_duration)
            dest.unlink(missing_ok=True)
            return cat, False
    except Exception as e:
        log.warning("Không probe được %s: %s", fname, e)

    log.info("  + [%s] %s", cat, dest.name)
    return cat, True


def download_clips(
    clips: list[dict],
    max_duration: float = 20.0,
    max_workers: int = 4,
) -> dict[str, int]:
    """Tải clip vào materials/meme_video/<nhóm>/<filename>, phân loại và kiểm tra độ dài."""
    base = MATERIALS_DIR / KINDS["memes"][0]
    existing = existing_filenames(base)
    to_download = [c for c in clips if c["filename"].lower() not in existing]

    added: dict[str, int] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [
            executor.submit(_download_single, c, base, max_duration)
            for c in to_download
        ]
        for fut in concurrent.futures.as_completed(futures):
            try:
                cat, success = fut.result()
                if success:
                    added[cat] = added.get(cat, 0) + 1
            except Exception as e:
                log.warning("Lỗi tải clip: %s", e)

    return added
