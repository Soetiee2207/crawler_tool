"""Lọc và chấm điểm bài crawl: bỏ bài cũ, quá ngắn, quảng cáo; xếp theo tốc độ tương tác."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone

# Điểm "độ hot" = tương tác / tuổi bài (giờ). +6 giờ để bài vừa đăng vài phút không bị thổi phồng.
# Bình luận, repost nặng ký hơn like: bài nhiều người bàn tán mới có chất liệu làm video.
HOT_SCORE_SQL = (
    "(like_count + 2 * reply_count + 3 * (repost_count + quote_count))"
    " / ((julianday('now') - julianday(published_at)) * 24 + 6)"
)

# Dấu hiệu bài bán hàng / tuyển dụng / kéo traffic. Mạnh: gặp một là bỏ; yếu: gặp từ 2 dấu hiệu khác nhau.
# Không tính tên sàn (Shopee, TikTok Shop...): bài than phiền, review hài về sàn rất hay viral.
_AD_STRONG = re.compile(
    r"(?<!\d)0\d{9}(?!\d)|link\s*(ở|trong|tại)?\s*bio|chốt\s*đơn|freeship"
    r"|(inbox|ib)\s*ngay|kiếm tiền online|cộng tác viên",
    re.IGNORECASE,
)
_AD_WEAK = re.compile(
    r"\b(inbox|ib|zalo|hotline|sđt|ctv|sale|voucher)\b|giảm giá|khuyến mãi|giá chỉ|giá sốc|mua ngay"
    r"|tuyển dụng|tuyển gấp|đặt lịch|liên hệ ngay",
    re.IGNORECASE,
)


def age_hours(post: dict, now: datetime | None = None) -> float | None:
    if not post.get("published_at"):
        return None
    now = now or datetime.now(timezone.utc)
    return (now - datetime.fromisoformat(post["published_at"])).total_seconds() / 3600


def hot_score(post: dict, now: datetime | None = None) -> float:
    """Như HOT_SCORE_SQL; bài không rõ ngày đăng xếp cuối."""
    age = age_hours(post, now)
    if age is None:
        return 0.0
    engagement = post["like_count"] + 2 * post["reply_count"] + 3 * (post["repost_count"] + post["quote_count"])
    return engagement / (max(age, 0) + 6)


def is_ad(text: str) -> bool:
    if _AD_STRONG.search(text):
        return True
    return len({m.group(0).lower() for m in _AD_WEAK.finditer(text)}) >= 2


@dataclass
class QualityFilter:
    max_age_days: int = 14  # 0 = không giới hạn
    min_likes: int = 0
    min_replies: int = 0
    min_chars: int = 40  # bài ngắn hơn chỉ giữ nếu có ảnh/video
    skip_ads: bool = True

    def too_old(self, post: dict) -> bool:
        age = age_hours(post)
        return bool(self.max_age_days) and age is not None and age > self.max_age_days * 24

    def reject(self, post: dict) -> str | None:
        """Lý do loại bài gốc (dùng làm nhãn thống kê), None nếu bài đạt."""
        text = post["text"].strip()
        if self.too_old(post):
            return f"cũ hơn {self.max_age_days} ngày"
        if post["like_count"] < self.min_likes:
            return "ít like"
        if post["reply_count"] < self.min_replies:
            return "ít bình luận"
        if self.min_chars and len(text) < self.min_chars and not post["media"]:
            return "quá ngắn"
        if self.skip_ads and is_ad(text):
            return "quảng cáo"
        return None
