"""Parse Threads' internal JSON (embedded page data + GraphQL responses) into flat post dicts.

Threads không có HTML ổn định để parse; dữ liệu thật nằm trong JSON được nhúng
(<script type="application/json" data-sjs>) và trong các response GraphQL.
Thay vì phụ thuộc vào đường dẫn cố định (hay thay đổi), ta duyệt đệ quy và nhận
diện "post" theo hình dạng object.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterator


def _is_post(obj: dict) -> bool:
    return (
        isinstance(obj.get("user"), dict)
        and "username" in obj["user"]
        and "code" in obj
        and ("caption" in obj or "text_post_app_info" in obj)
        and ("pk" in obj or "id" in obj)
    )


def iter_raw_posts(data: Any) -> Iterator[dict]:
    """Yield every object in `data` that looks like a Threads post."""
    stack = [data]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            if _is_post(node):
                yield node
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)


def _best_image(item: dict) -> str | None:
    candidates = (item.get("image_versions2") or {}).get("candidates") or []
    if not candidates:
        return None
    best = max(candidates, key=lambda c: (c.get("width") or 0) * (c.get("height") or 0))
    return best.get("url")


def _media_of(item: dict) -> list[dict]:
    videos = item.get("video_versions") or []
    if videos:
        return [{"type": "video", "url": videos[0].get("url"), "thumbnail": _best_image(item)}]
    image = _best_image(item)
    return [{"type": "image", "url": image}] if image else []


def normalize_post(raw: dict) -> dict:
    caption = raw.get("caption") or {}
    info = raw.get("text_post_app_info") or {}
    user = raw["user"]
    taken_at = raw.get("taken_at")

    media: list[dict] = []
    for child in raw.get("carousel_media") or []:
        media.extend(_media_of(child))
    if not media:
        media = _media_of(raw)

    reply_to = info.get("reply_to_author") or {}
    return {
        "id": str(raw.get("pk") or raw.get("id")).split("_")[0],
        "code": raw["code"],
        "url": f"https://www.threads.com/@{user['username']}/post/{raw['code']}",
        "username": user["username"],
        "user_verified": bool(user.get("is_verified")),
        "avatar_url": user.get("profile_pic_url"),
        "text": caption.get("text") or "",
        "published_at": (
            datetime.fromtimestamp(taken_at, tz=timezone.utc).isoformat() if taken_at else None
        ),
        "like_count": raw.get("like_count") or 0,
        "reply_count": info.get("direct_reply_count") or 0,
        "repost_count": info.get("repost_count") or 0,
        "quote_count": info.get("quote_count") or 0,
        "is_reply": bool(info.get("is_reply") or reply_to),
        "reply_to_username": reply_to.get("username"),
        "media": [m for m in media if m.get("url")],
    }


def extract_posts(data: Any) -> dict[str, dict]:
    """Return normalized posts found in `data`, keyed by post code."""
    posts: dict[str, dict] = {}
    for raw in iter_raw_posts(data):
        try:
            post = normalize_post(raw)
        except (KeyError, TypeError, ValueError):
            continue
        posts[post["code"]] = merge_post(posts.get(post["code"]), post)
    return posts


def merge_post(prev: dict | None, new: dict) -> dict:
    """Merge two copies of the same post: một bản có thể thiếu text, media hoặc thông tin reply."""
    if prev is None:
        return new
    richer, other = (new, prev) if len(new["text"]) + len(new["media"]) > len(prev["text"]) + len(prev["media"]) else (prev, new)
    merged = {**other, **{k: v for k, v in richer.items() if v}}
    merged["is_reply"] = prev["is_reply"] or new["is_reply"]
    return merged
