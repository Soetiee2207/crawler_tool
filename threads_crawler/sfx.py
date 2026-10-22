"""Tải hiệu ứng âm thanh ngắn (sfx meme: vine boom, bruh, wow...) từ myinstants.com vào materials/sfx/.

Trang danh sách là HTML tĩnh: mỗi âm thanh là một nút `play('/media/sounds/<file>.mp3', ...)` kèm link
`/en/instant/<slug>/` mang tên hiển thị. Phân trang bằng `?page=N`. Âm thanh được xếp vào nhóm tình huống
(thư mục con) theo từ khóa tiếng Anh trong tên; âm thanh dài quá `max_duration` bị bỏ.
"""

from __future__ import annotations

import html
import logging
import re
import time
from urllib.parse import quote

import httpx

from . import media
from .media import KINDS, MATERIALS_DIR

log = logging.getLogger(__name__)

BASE_URL = "https://www.myinstants.com"
CRAWL_DELAY = 1.0
FALLBACK_CATEGORY = "khác"
VIETNAMESE_CATEGORY = "câu_nói_việt"  # câu thoại meme tiếng Việt không khớp nhóm nào
VIETNAMESE_RE = re.compile(r"[ăâđêôơưàảãáạằẳẵắặầẩẫấậèẻẽéẹềểễếệìỉĩíịòỏõóọồổỗốộờởỡớợùủũúụừửữứựỳỷỹýỵ]", re.I)
# Nguồn mặc định, theo thứ tự ưu tiên: đang hot ở Việt Nam, hot toàn cầu, rồi các mục hợp làm video.
DEFAULT_SOURCES = ["vn", "trending", "sound effects", "memes", "reactions", "tiktok trends"]

INSTANT_RE = re.compile(
    r"play\('(?P<mp3>/media/sounds/[^']+\.mp3)'.*?"
    r'<a href="/en/instant/(?P<slug>[^/"]+)/" class="instant-link[^"]*">(?P<title>[^<]+)</a>',
    re.S,
)

# Nhóm (tên thư mục) -> từ khóa trong tên âm thanh. Nhóm đứng trước thắng khi hòa điểm.
CATEGORY_KEYWORDS: dict[str, list[str]] = {
    "nhấn_mạnh": ["vine boom", "boom", "bass", "dun dun", "airhorn", "air horn", "mlg", "hitmarker", "ding",
                   "ting", "dramatic", "drama", "taco bell", "thud", "explosion", "riser", "build up",
                   "record scratch", "alert", "undertaker", "the rock", "za warudo", "time stop", "dj stop",
                   "censor", "beep", "tới công chuyện"],
    "va_chạm": ["punch", "slap", "pan hit", "bone crack", "crack", "gunshot", "gun", "pistol", "shot", "bonk",
                 "metal pipe", "breaking", "hurt", "death", "hit", "kick", "smack"],
    "đúng_sai": ["correct", "check mark", "ta da", "incorrect", "buzzer", "wrong", "error", "question mark",
                  "ka ching", "kaching", "cha ching", "money", "apple pay", "cash"],
    "cười": ["laugh", "laughing", "haha", "giggle", "sitcom", "hehe", "muhehehe", "hee hee", "lol", "cười"],
    "sốc": ["wow", "gasp", "omg", "what", "wut", "huh", "oh no", "surprise", "shock", "shocked", "shocking",
            "wait", "nani", "oh my god", "oooh", "sus", "fah", "scream", "screaming", "aaa", "trời ơi",
            "ối dồi ôi", "ghê", "khoan"],
    "thất_bại": ["bruh", "fail", "oof", "trombone", "sad", "violin", "windows", "nope", "no", "womp",
                  "emotional damage", "no god", "get out", "crying", "cry", "aww", "mệt"],
    "vui_mừng": ["applause", "clap", "cheer", "cheering", "crowd", "yay", "yippee", "victory", "win",
                  "congratulations", "yeah", "lets go", "let's go"],
    "ngượng_im_lặng": ["awkward", "cricket", "crickets", "silence", "snore", "sleep"],
    "chuyển_cảnh": ["whoosh", "swoosh", "swish", "transition", "pop", "click", "slide", "camera", "shutter",
                     "moments later", "running", "moving", "loading", "lagging", "intro"],
    "thông_báo": ["discord", "messenger", "notification", "ps5", "startup", "iphone", "school bell",
                   "ringtone", "facebook", "tiktok", "discordjoin", "join", "spike plant"],
    "căng_thẳng": ["suspense", "horror", "scary", "ominous", "tension", "spooky", "thriller", "jumpscare"],
    "hài_hước": ["fart", "burp", "boing", "honk", "squeak", "cartoon", "goofy", "spring", "clown", "duck",
                  "cat", "meow", "dog", "bird", "chicken", "anime", "uwu", "rizz", "quack", "among us", "ack",
                  "eating", "munch", "mario", "minecraft", "jump", "magic"],
}


def _squash(text: str) -> str:
    """Gộp chữ lặp ("faaaah" -> "fah", "yippeeee" -> "yipe") để khớp từ khóa kéo dài kiểu meme."""
    return re.sub(r"(.)\1+", r"\1", re.sub(r"[\W_]+", " ", text.lower())).strip()


def categorize(title: str) -> str:
    text = _squash(title)
    best, best_score = FALLBACK_CATEGORY, 0
    for cat, words in CATEGORY_KEYWORDS.items():
        score = sum(len(re.findall(rf"(?<!\w){re.escape(_squash(w))}(?!\w)", text)) for w in words)
        if score > best_score:
            best, best_score = cat, score
    if best == FALLBACK_CATEGORY and VIETNAMESE_RE.search(text):
        return VIETNAMESE_CATEGORY
    return best


def source_url(source: str, page: int) -> str:
    """'vn' / 'us'... = đang hot theo nước; 'trending' = hot toàn cầu; còn lại = tên mục (vd 'sound effects')."""
    if source == "trending":
        path = "/en/trending/"
    elif re.fullmatch(r"[a-z]{2}", source):
        path = f"/en/index/{source}/"
    else:
        path = f"/en/categories/{quote(source)}/"
    return f"{BASE_URL}{path}?page={page}"


def parse_page(text: str) -> list[dict]:
    return [
        {"mp3": m["mp3"], "slug": m["slug"], "title": html.unescape(m["title"]).strip()}
        for m in INSTANT_RE.finditer(text)
    ]


def _filename(sound: dict) -> str:
    """<tên hiển thị (giữ dấu tiếng Việt)>__<slug myinstants>.mp3; slug dùng để nhận ra âm thanh đã tải."""
    name = re.sub(r"[^\w]+", "-", sound["title"].lower()).strip("-_")[:60] or "sound"
    return f"{name}__{sound['slug']}.mp3"


def existing_ids(base) -> set[str]:
    return {f.stem.rsplit("__", 1)[-1] for f in base.rglob("*.mp3")} if base.exists() else set()


def crawl(sources: list[str], limit: int = 200, pages: int = 3, max_duration: float = 6.0) -> dict[str, int]:
    """Tải tối đa `limit` âm thanh mới từ các nguồn (mỗi nguồn `pages` trang). Trả về số âm thanh mới mỗi nhóm."""
    base = MATERIALS_DIR / KINDS["sfx"][0]
    have = existing_ids(base)
    added: dict[str, int] = {}
    seen: set[str] = set()
    headers = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/126 Safari/537.36"}
    with httpx.Client(timeout=30, follow_redirects=True, headers=headers) as client:
        for source in sources:
            for page in range(1, pages + 1):
                if sum(added.values()) >= limit:
                    return added
                try:
                    resp = client.get(source_url(source, page))
                    resp.raise_for_status()
                except httpx.HTTPError as e:
                    log.warning("Không mở được %s trang %d: %s", source, page, e)
                    break
                sounds = parse_page(resp.text)
                log.info("myinstants %s trang %d: %d âm thanh", source, page, len(sounds))
                if not sounds:
                    break
                for s in sounds:
                    sid = s["slug"]
                    if sid in have or s["mp3"] in seen or sum(added.values()) >= limit:
                        continue
                    seen.add(s["mp3"])
                    if _download(client, s, base, max_duration):
                        have.add(sid)
                        cat = categorize(s["title"])
                        added[cat] = added.get(cat, 0) + 1
                time.sleep(CRAWL_DELAY)
    return added


def _download(client: httpx.Client, sound: dict, base, max_duration: float) -> bool:
    try:
        resp = client.get(BASE_URL + sound["mp3"])
        resp.raise_for_status()
    except httpx.HTTPError as e:
        log.warning("Không tải được %s: %s", sound["title"], e)
        return False
    dest = base / categorize(sound["title"]) / _filename(sound)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(resp.content)
    try:
        duration = media.probe(dest)["duration"]
    except RuntimeError:
        duration = 0
    if not 0 < duration <= max_duration:  # hỏng hoặc quá dài (nhạc, đoạn hội thoại) → không phải sfx
        dest.unlink(missing_ok=True)
        return False
    log.info("  + %s/%s (%.1fs)", dest.parent.name, dest.name, duration)
    return True
