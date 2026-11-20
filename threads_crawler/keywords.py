"""Gợi ý từ khóa crawl: bộ chọn sẵn theo chủ đề + gợi ý rút ra từ dữ liệu đã crawl."""

from __future__ import annotations

import re
from collections import Counter

from .storage import Store

# Chỉ mảng công sở + hài. Cụm từ ở ngôi thứ nhất ("sếp mình", "hôm nay tui") hay ra bài kể chuyện thật,
# từ đơn chung chung ("lương", "áp lực") kéo về đủ thứ lạc đề.
GROUPS: list[dict] = [
    {"name": "Sếp & đồng nghiệp", "emoji": "🏢", "keywords": [
        "sếp mình", "sếp tôi", "đồng nghiệp mình", "đồng nghiệp toxic", "sếp toxic", "chuyện ở công ty",
        "công ty mình", "nhân viên mới", "chị HR", "đi họp", "team building", "#congso"]},
    {"name": "Đi làm", "emoji": "💼", "keywords": [
        "hôm nay đi làm", "dân văn phòng", "chạy deadline", "chạy KPI", "tăng ca", "xin nghỉ phép",
        "nộp đơn nghỉ việc", "đi phỏng vấn", "thực tập sinh", "lương tháng này", "thưởng tết", "#danvanphong"]},
    {"name": "Hài đời thường", "emoji": "😂", "keywords": [
        "cười xỉu", "cười đau bụng", "khó đỡ", "quê xệ", "pha xử lý", "ba mẹ tui",
        "đứa bạn thân", "chuyện nhà tui", "hôm nay tui", "người yêu tui", "ảo ma", "#haihuoc"]},
    {"name": "Tấu hài / meme", "emoji": "🤡", "keywords": [
        "tấu hài", "meme", "thanh niên", "xàm xí", "cà khịa", "trend",
        "hài hước", "góc giải trí", "sự thật ngầm hiểu", "ai hiểu không", "relate quá", "#cuoixiu"]},
]

_HASHTAG_RE = re.compile(r"#(\w[\w_]{1,40})", re.UNICODE)


def suggestions(store: Store, limit: int = 24) -> dict:
    crawled = {q["query"].lower().lstrip("#"): q for q in store.list_queries()}

    # Hashtag hay gặp trong bài đã crawl (cả bình luận) mà chưa crawl riêng.
    counts: Counter[str] = Counter()
    display: dict[str, str] = {}
    for (text,) in store.db.execute("SELECT text FROM posts WHERE text LIKE '%#%'"):
        for tag in set(_HASHTAG_RE.findall(text or "")):
            key = tag.lower()
            if key.isdigit():
                continue
            counts[key] += 1
            display.setdefault(key, tag)
    from_data = [
        {"keyword": f"#{display[k]}", "count": n}
        for k, n in counts.most_common(limit * 2) if k not in crawled and n >= 2
    ][:limit]

    # Hiệu quả của từng từ khóa đã crawl: like trung bình / cao nhất của bài gốc.
    # Tính cả bài đã làm video (đã xóa khỏi kho, còn lưu ở done_posts).
    # fresh = số bài đăng trong 14 ngày (chỉ bài còn trong kho mới biết ngày đăng).
    performance = [dict(r) for r in store.db.execute(
        """SELECT query, COUNT(*) AS posts, CAST(AVG(like_count) AS INTEGER) AS avg_likes,
                  MAX(like_count) AS max_likes, SUM(done) AS used, SUM(fresh) AS fresh
           FROM (SELECT query, like_count, 0 AS done,
                        COALESCE(julianday('now') - julianday(published_at) <= 14, 0) AS fresh
                 FROM posts WHERE parent_code IS NULL
                 UNION ALL SELECT query, like_count, 1, 0 FROM done_posts)
           WHERE query IS NOT NULL GROUP BY query ORDER BY avg_likes DESC"""
    )]

    groups = [
        {**g, "keywords": [{"keyword": k, "crawled": crawled.get(k.lower(), {}).get("count", 0)} for k in g["keywords"]]}
        for g in GROUPS
    ]
    return {"groups": groups, "from_data": from_data, "performance": performance}
