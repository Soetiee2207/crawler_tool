"""Gợi ý từ khóa crawl: bộ chọn sẵn theo chủ đề + gợi ý rút ra từ dữ liệu đã crawl."""

from __future__ import annotations

import re
from collections import Counter

from .storage import Store

# Chủ đề hay ra bài kể chuyện / hóng drama trên Threads Việt — hợp làm video TikTok dạng đọc truyện.
GROUPS: list[dict] = [
    {"name": "Tình yêu", "emoji": "💔", "keywords": [
        "người yêu cũ", "chia tay", "bị cắm sừng", "ngoại tình", "crush", "tình đơn phương",
        "yêu xa", "red flag", "ghen tuông", "hẹn hò"]},
    {"name": "Công sở", "emoji": "🏢", "keywords": [
        "chuyện công sở", "sếp", "đồng nghiệp", "nghỉ việc", "phỏng vấn xin việc", "lương",
        "thực tập", "bị đuổi việc", "tăng ca", "toxic công sở"]},
    {"name": "Gia đình", "emoji": "👨‍👩‍👧", "keywords": [
        "mẹ chồng", "nhà chồng", "bố mẹ", "con cái", "chị em dâu", "họ hàng",
        "về quê", "ra mắt nhà người yêu", "đám cưới", "sính lễ"]},
    {"name": "Hài / khó đỡ", "emoji": "😂", "keywords": [
        "cười ỉa", "khó đỡ", "chuyện hài", "quê độ", "pha xử lý", "tấu hài",
        "shipper", "đi chợ", "chuyện nhà tui", "mất mặt"]},
    {"name": "Tâm linh / kinh dị", "emoji": "👻", "keywords": [
        "chuyện ma", "tâm linh", "gặp ma", "chuyện lạ", "rợn người", "nhà ma",
        "chuyện có thật", "điềm báo", "giấc mơ lạ", "trải nghiệm kinh dị"]},
    {"name": "Tiền bạc", "emoji": "💸", "keywords": [
        "vay tiền", "đòi nợ", "bị lừa", "lừa đảo", "tiết kiệm", "mua nhà",
        "nợ nần", "kiếm tiền", "chi tiêu", "tiền lương"]},
    {"name": "Học đường", "emoji": "🎓", "keywords": [
        "sinh viên", "ký túc xá", "bạn cùng phòng", "đại học", "thi cử", "giáo viên",
        "bạn thân", "thanh xuân", "họp phụ huynh", "đi học"]},
    {"name": "Tâm sự", "emoji": "🗣️", "keywords": [
        "tâm sự", "cho em hỏi", "mọi người ơi", "có ai giống mình", "lời khuyên", "áp lực",
        "cô đơn", "tuổi 25", "kể chuyện", "trầm cảm"]},
    {"name": "Drama / bóc phốt", "emoji": "🍿", "keywords": [
        "drama", "bóc phốt", "phốt", "hàng xóm", "chủ trọ", "bị bắt nạt",
        "review tệ", "người lạ", "bị chửi", "cãi nhau"]},
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
    performance = [dict(r) for r in store.db.execute(
        """SELECT query, COUNT(*) AS posts, CAST(AVG(like_count) AS INTEGER) AS avg_likes,
                  MAX(like_count) AS max_likes, SUM(done) AS used
           FROM (SELECT query, like_count, 0 AS done FROM posts WHERE parent_code IS NULL
                 UNION ALL SELECT query, like_count, 1 FROM done_posts)
           WHERE query IS NOT NULL GROUP BY query ORDER BY avg_likes DESC"""
    )]

    groups = [
        {**g, "keywords": [{"keyword": k, "crawled": crawled.get(k.lower(), {}).get("count", 0)} for k in g["keywords"]]}
        for g in GROUPS
    ]
    return {"groups": groups, "from_data": from_data, "performance": performance}
