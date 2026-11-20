"""Video sản phẩm: lưu sản phẩm như một bài trong kho, đánh giá như các bình luận, để dùng lại trình dựng."""

from __future__ import annotations

import base64
import binascii
import io
import uuid

from PIL import Image, UnidentifiedImageError

from .storage import Store

QUERY = "🛒 Sản phẩm"
HEADER_NAME_LEN = 26  # dài hơn sẽ đè lên phần số sao trên đầu thẻ
MAX_IMAGE_BYTES = 15 * 1024 * 1024


def mask_name(name: str) -> str:
    """Che tên người mua: họ không đồng ý xuất hiện trong video. Tên sàn đã che sẵn thì giữ nguyên."""
    name = name.strip()
    if "*" in name or len(name) < 2:
        return name
    return f"{name[0]}***{name[-1]}"


def parse_reviews(raw: str) -> list[dict]:
    """Mỗi dòng một đánh giá: `nội dung`, `sao | nội dung`, `tên | nội dung` hoặc `tên | sao | nội dung`."""
    reviews = []
    for line in raw.splitlines():
        parts = [p.strip() for p in line.split("|", 2)]
        if not parts[-1]:
            continue
        name, stars = "", None
        if len(parts) == 3:
            name = parts[0]
            stars = parts[1]
        elif len(parts) == 2:
            if parts[0] in {"1", "2", "3", "4", "5"}:
                stars = parts[0]
            else:
                name = parts[0]
        rating = int(stars) if stars in {"1", "2", "3", "4", "5"} else None
        reviews.append({"name": mask_name(name), "rating": rating, "text": parts[-1]})
    return reviews


def save_image(store: Store, code: str, data_url: str) -> str:
    """Lưu ảnh tải lên (data URL) thành JPEG trong data/media/<code>/."""
    try:
        raw = base64.b64decode(data_url.split(",", 1)[-1], validate=True)
        if len(raw) > MAX_IMAGE_BYTES:
            raise ValueError("Ảnh sản phẩm quá lớn (tối đa 15MB)")
        img = Image.open(io.BytesIO(raw)).convert("RGB")
    except (binascii.Error, UnidentifiedImageError, OSError) as e:
        raise ValueError("Không đọc được ảnh sản phẩm") from e
    dest = store.media_dir / code / "00_image.jpg"
    dest.parent.mkdir(parents=True, exist_ok=True)
    img.save(dest, quality=92)
    return str(dest)


def _entry(code: str, username: str, text: str, media: list[dict] | None = None, avatar: str | None = None) -> dict:
    # Lượt tương tác để None: thẻ ẩn thanh tim/bình luận thay vì hiện số không có thật.
    return {
        "code": code, "id": code, "url": "", "username": username, "user_verified": False, "text": text,
        "published_at": None, "like_count": None, "reply_count": None, "repost_count": None, "quote_count": None,
        "reply_to_username": None, "media": media or [], "avatar_path": avatar,
    }


def create(store: Store, name: str, intro: str, price: str, source: str, reviews_raw: str, cta: str,
           image: str | None) -> str:
    reviews = parse_reviews(reviews_raw)
    if not reviews:
        raise ValueError("Cần ít nhất một đánh giá")
    code = "sp" + uuid.uuid4().hex[:12]
    name = name.strip()
    path = save_image(store, code, image) if image else None
    media = [{"type": "image", "url": "", "path": path}] if path else []
    short = name if len(name) <= HEADER_NAME_LEN else name[:HEADER_NAME_LEN - 1] + "…"
    head = f"{name}\nGiá: {price.strip()}" if price.strip() else name
    body = f"{head}\n\n{intro.strip()}"
    store.save(_entry(code, short, body, media, path), query=QUERY)

    source = source.strip()
    # thread_code = mã sản phẩm: trình dựng coi như phần viết tiếp, tự đưa mọi đánh giá vào video.
    extras = {"thread_code": code, "depth": 0}
    for i, r in enumerate(reviews):
        stars = "★" * r["rating"] + "☆" * (5 - r["rating"]) if r["rating"] else ""
        rcode = f"{code}r{i}"
        store.save({
            **_entry(rcode, r["name"] or f"Người mua {i + 1}", r["text"]), **extras, "rank": i,
            "badge": " · ".join(filter(None, [stars, source])),
        }, query=QUERY, parent_code=code)
    if cta.strip():
        store.save({**_entry(f"{code}cta", short, cta.strip(), avatar=path), **extras, "rank": len(reviews)},
                   query=QUERY, parent_code=code)
    return code
