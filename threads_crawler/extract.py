"""Parse Threads' internal JSON (embedded page data + GraphQL responses) into flat post dicts.

Threads không có HTML ổn định để parse; dữ liệu thật nằm trong JSON được nhúng
(<script type="application/json" data-sjs>) và trong các response GraphQL.
Thay vì phụ thuộc vào đường dẫn cố định (hay thay đổi), ta duyệt đệ quy và nhận
diện "post" theo hình dạng object.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
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


def _giphy_image(item: dict) -> str | None:
    """Bình luận bằng GIF: ảnh không nằm trong image_versions2 mà trong giphy_media_info."""
    info = item.get("giphy_media_info") or (item.get("text_post_app_info") or {}).get("giphy_media_info") or {}
    images = info.get("first_party_cdn_proxied_images") or info.get("images") or {}
    versions = [v for v in images.values() if isinstance(v, dict) and v.get("url")]
    if not versions:
        return info.get("url")
    best = max(versions, key=lambda v: int(v.get("width") or 0) * int(v.get("height") or 0))
    return best["url"]


def _media_of(item: dict) -> list[dict]:
    videos = item.get("video_versions") or []
    if videos:
        return [{"type": "video", "url": videos[0].get("url"), "thumbnail": _best_image(item)}]
    image = _best_image(item) or _giphy_image(item)
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


# ── Trang chi tiết bài: bình luận theo đúng cấu trúc của Threads ─────────────
#
# media.text_post_app_info.self_thread.posts.edges[]       phần tác giả viết tiếp (bài dài chia nhiều phần)
# media.text_post_app_info.direct_replies.edges[i]          mạch bình luận thứ i, theo thứ tự Threads hiển thị
#     .node.posts.edges[0]                                  bình luận gốc
#     .node.posts.edges[1..]                                các câu trả lời bên dưới, theo thứ tự hội thoại
#     .node.posts.page_info.has_next_page                   còn câu trả lời bị ẩn (phải mở trang bình luận)
# Trang đầu có các mạch hot nhất; cuộn thêm thì GraphQL trả về các trang tiếp theo cùng cấu trúc.

_MENTION_RE = re.compile(r"@[\w.]+")


def is_junk_reply(post: dict) -> bool:
    """Bình luận chỉ tag tên bạn bè hoặc vài ký tự, không ảnh: không có gì để đọc."""
    return not post["media"] and len(_MENTION_RE.sub("", post["text"]).strip(" .\n")) < 2


def _edge_posts(conn: dict | None) -> list[dict]:
    posts = []
    for edge in (conn or {}).get("edges") or []:
        node = (edge or {}).get("node")
        if isinstance(node, dict) and _is_post(node):
            try:
                posts.append(normalize_post(node))
            except (KeyError, TypeError, ValueError):
                continue
    return posts


@dataclass
class CommentThread:
    items: list[dict]  # [bình luận gốc, câu trả lời...]
    has_more: bool  # còn câu trả lời chưa hiện trên trang

    @property
    def top(self) -> dict:
        return self.items[0]


@dataclass
class PostPage:
    self_thread: list[dict] = field(default_factory=list)
    threads: list[CommentThread] = field(default_factory=list)

    def add(self, data: Any, code: str) -> None:
        """Thêm các mạch bình luận của bài `code` có trong một khối JSON (theo thứ tự xuất hiện)."""
        seen = {t.top["code"] for t in self.threads}
        own = {p["code"] for p in self.self_thread}
        for info, media in _iter_reply_blocks(data):
            if media.get("code") not in (None, code):
                continue  # khối bình luận của bài khác (vd bài gợi ý)
            for p in _edge_posts((info.get("self_thread") or {}).get("posts")):
                if p["code"] not in own:
                    own.add(p["code"])
                    self.self_thread.append(p)
            for edge in (info.get("direct_replies") or {}).get("edges") or []:
                node = (edge or {}).get("node") or {}
                items = _edge_posts(node.get("posts"))
                if not items or items[0]["code"] in seen or items[0]["code"] in own:
                    continue
                seen.add(items[0]["code"])
                more = bool(((node.get("posts") or {}).get("page_info") or {}).get("has_next_page"))
                self.threads.append(CommentThread(items, more or items[0]["reply_count"] > len(items) - 1))


def _iter_reply_blocks(data: Any) -> Iterator[tuple[dict, dict]]:
    """(text_post_app_info, media) của mọi media có self_thread / direct_replies."""
    stack = [data]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            info = node.get("text_post_app_info")
            if isinstance(info, dict) and ("direct_replies" in info or "self_thread" in info):
                yield info, node
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(reversed(node))  # giữ thứ tự duyệt từ đầu danh sách


class ReplyTree:
    """Cây bình luận: gộp các trang (bài, rồi trang riêng của từng bình luận) thành quan hệ cha → con.

    Trên trang của X, mỗi mạch [a, b, c...] nghĩa là a trả lời X, b trả lời a, c trả lời b (chuỗi hội
    thoại Threads hiện sẵn). Con của một nút giữ thứ tự Threads hiển thị.
    """

    def __init__(self) -> None:
        self.nodes: dict[str, dict] = {}
        self.children: dict[str, list[str]] = {}
        self.parent: dict[str, str] = {}

    def _link(self, parent: str, child: dict) -> None:
        code = child["code"]
        self.nodes[code] = merge_post(self.nodes.get(code), child)
        if code in self.parent or code == parent:
            return
        self.parent[code] = parent
        self.children.setdefault(parent, []).append(code)

    def add_page(self, parent: str, page: PostPage, with_self: bool = True) -> None:
        """`with_self`: phần người viết tự viết tiếp (self_thread) là chuỗi trả lời nối sau `parent`.
        Trang bài gốc tách riêng phần này (False); trang của một bình luận thì tính vào cây."""
        if with_self:
            prev = parent
            for p in page.self_thread:
                self._link(prev, p)
                prev = p["code"]
        for t in page.threads:
            self._link(parent, t.items[0])
            for j in range(1, len(t.items)):
                item = t.items[j]
                # cha = bình luận gần nhất phía trên trong chuỗi của đúng người được trả lời
                up = next((p for p in reversed(t.items[:j]) if p["username"] == item["reply_to_username"]), t.items[j - 1])
                self._link(up["code"], item)

    def kids(self, code: str) -> list[dict]:
        return [self.nodes[c] for c in self.children.get(code, []) if not is_junk_reply(self.nodes[c])]
