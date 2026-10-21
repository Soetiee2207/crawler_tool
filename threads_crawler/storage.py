"""SQLite storage for crawled posts and rendered videos, plus media download and JSON export."""

from __future__ import annotations

import json
import logging
import shutil
import sqlite3
import threading
from pathlib import Path
from urllib.parse import urlparse

import httpx

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS posts (
    code TEXT PRIMARY KEY,
    id TEXT,
    url TEXT,
    username TEXT,
    user_verified INTEGER,
    text TEXT,
    published_at TEXT,
    like_count INTEGER,
    reply_count INTEGER,
    repost_count INTEGER,
    quote_count INTEGER,
    parent_code TEXT,          -- NULL = bài gốc, khác NULL = reply của bài đó
    reply_to_username TEXT,    -- trả lời ai (tác giả gốc trả lời chính mình = viết tiếp thread)
    query TEXT,                -- từ khóa đã dùng để tìm ra bài
    avatar_path TEXT,          -- ảnh đại diện tác giả đã tải về
    media_json TEXT,           -- [{"type","url","path"}]
    crawled_at TEXT DEFAULT (datetime('now')),
    used INTEGER DEFAULT 0     -- đánh dấu đã dùng làm video
);
CREATE INDEX IF NOT EXISTS idx_posts_parent ON posts(parent_code);
CREATE INDEX IF NOT EXISTS idx_posts_query ON posts(query);

CREATE TABLE IF NOT EXISTS videos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    post_code TEXT,
    title TEXT,
    path TEXT,
    thumb_path TEXT,
    duration REAL,
    config_json TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);

-- Bài đã làm video: xóa khỏi kho, chỉ giữ lại dấu vết để crawl lại không bị thêm vào nữa.
CREATE TABLE IF NOT EXISTS done_posts (
    code TEXT PRIMARY KEY,
    username TEXT,
    query TEXT,
    like_count INTEGER,
    video_id INTEGER,
    done_at TEXT DEFAULT (datetime('now'))
);
"""

# Cột thêm sau phiên bản đầu; DB cũ được bổ sung khi mở.
MIGRATIONS = {
    "reply_to_username": "TEXT",
    "avatar_path": "TEXT",
}


def _row_to_post(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["media"] = json.loads(d.pop("media_json") or "[]")
    d["user_verified"] = bool(d["user_verified"])
    d["used"] = bool(d["used"])
    return d


class Store:
    def __init__(self, data_dir: Path) -> None:
        data_dir = data_dir.resolve()
        self.data_dir = data_dir
        self.media_dir = data_dir / "media"
        self.videos_dir = data_dir / "videos"
        self.media_dir.mkdir(parents=True, exist_ok=True)
        self.videos_dir.mkdir(parents=True, exist_ok=True)
        self._path = data_dir / "threads.db"
        self._local = threading.local()
        cols = {r[1] for r in self.db.execute("PRAGMA table_info(posts)")}
        if cols:
            for col, typ in MIGRATIONS.items():
                if col not in cols:
                    self.db.execute(f"ALTER TABLE posts ADD COLUMN {col} {typ}")
        self.db.executescript(SCHEMA)
        # Bài đánh dấu "đã dùng" từ phiên bản cũ → chuyển sang cơ chế xóa khỏi kho.
        used = [r[0] for r in self.db.execute("SELECT code FROM posts WHERE used = 1 AND parent_code IS NULL")]
        if used:
            self.archive_used(used)

    @property
    def db(self) -> sqlite3.Connection:
        """One connection per thread: web server xử lý request song song trên nhiều luồng."""
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self._path, timeout=30)
            conn.row_factory = sqlite3.Row
            self._local.conn = conn
        return conn

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    # ── Ghi dữ liệu crawl ─────────────────────────────────────────────────────

    def save(
        self, post: dict, query: str | None = None, parent_code: str | None = None
    ) -> None:
        if self.is_done(post["code"]) or (parent_code and self.is_done(parent_code)):
            return
        # Giữ lại đường dẫn media đã tải và trạng thái đã dùng nếu post được crawl lại.
        old = self.db.execute(
            "SELECT media_json, used, avatar_path FROM posts WHERE code = ?", (post["code"],)
        ).fetchone()
        if old:
            old_paths = {m["url"]: m.get("path") for m in json.loads(old["media_json"] or "[]")}
            for m in post["media"]:
                m.setdefault("path", old_paths.get(m["url"]))
        self.db.execute(
            """INSERT OR REPLACE INTO posts
               (code, id, url, username, user_verified, text, published_at, like_count, reply_count,
                repost_count, quote_count, parent_code, reply_to_username, query, avatar_path,
                media_json, used)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                post["code"], post["id"], post["url"], post["username"], int(post["user_verified"]),
                post["text"], post["published_at"], post["like_count"], post["reply_count"],
                post["repost_count"], post["quote_count"], parent_code, post["reply_to_username"], query,
                post.get("avatar_path") or (old["avatar_path"] if old else None),
                json.dumps(post["media"], ensure_ascii=False), old["used"] if old else 0,
            ),
        )
        self.db.commit()

    def download_media(self, post: dict, client: httpx.Client) -> None:
        """Download each media item of `post` into data/media/<code>/ and record its local path."""
        target = self.media_dir / post["code"]
        for i, m in enumerate(post["media"]):
            if m.get("path") and Path(m["path"]).exists():
                continue
            ext = Path(urlparse(m["url"]).path).suffix or (".mp4" if m["type"] == "video" else ".jpg")
            dest = target / f"{i:02d}_{m['type']}{ext}"
            if self._download(client, m["url"], dest):
                m["path"] = str(dest)

    def download_avatar(self, post: dict, client: httpx.Client) -> None:
        """Download the author's avatar (once per username) into data/media/avatars/."""
        dest = self.media_dir / "avatars" / f"{post['username']}.jpg"
        if dest.exists() or (post.get("avatar_url") and self._download(client, post["avatar_url"], dest)):
            post["avatar_path"] = str(dest)

    @staticmethod
    def _download(client: httpx.Client, url: str, dest: Path) -> bool:
        try:
            resp = client.get(url)
            resp.raise_for_status()
        except httpx.HTTPError as e:
            log.warning("Không tải được %s: %s", url[:80], e)
            return False
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(resp.content)
        return True

    def is_done(self, code: str) -> bool:
        return self.db.execute("SELECT 1 FROM done_posts WHERE code = ?", (code,)).fetchone() is not None

    def archive_used(self, codes: list[str], video_id: int | None = None) -> None:
        """Bài đã làm video: ghi vào done_posts rồi xóa khỏi kho (cả bình luận và ảnh/video đã tải)."""
        for code in codes:
            row = self.db.execute(
                "SELECT username, query, like_count FROM posts WHERE code = ?", (code,)
            ).fetchone()
            self.db.execute(
                "INSERT OR REPLACE INTO done_posts (code, username, query, like_count, video_id) VALUES (?, ?, ?, ?, ?)",
                (code, row["username"] if row else None, row["query"] if row else None,
                 row["like_count"] if row else None, video_id),
            )
            self.delete_post(code)

    def delete_post(self, code: str) -> None:
        codes = [r[0] for r in self.db.execute("SELECT code FROM posts WHERE code = ? OR parent_code = ?", (code, code))]
        for c in codes:  # avatars dùng chung giữa các bài nên giữ lại
            shutil.rmtree(self.media_dir / c, ignore_errors=True)
        self.db.execute("DELETE FROM posts WHERE code = ? OR parent_code = ?", (code, code))
        self.db.commit()

    # ── Đọc dữ liệu cho giao diện ─────────────────────────────────────────────

    def list_queries(self) -> list[dict]:
        rows = self.db.execute(
            """SELECT query, COUNT(*) AS count, MAX(crawled_at) AS last_crawled
               FROM posts WHERE parent_code IS NULL GROUP BY query ORDER BY last_crawled DESC"""
        )
        return [dict(r) for r in rows]

    def list_posts(
        self,
        query: str | None = None,
        search: str | None = None,
        sort: str = "likes",
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[dict], int]:
        where, args = ["parent_code IS NULL"], []
        if query:
            where.append("query = ?")
            args.append(query)
        if search:
            where.append("(text LIKE ? OR username LIKE ?)")
            args += [f"%{search}%", f"%{search}%"]
        order = {
            "likes": "like_count DESC",
            "replies": "reply_count DESC",
            "newest": "published_at DESC",
            "crawled": "crawled_at DESC",
        }.get(sort, "like_count DESC")
        sql_where = " AND ".join(where)
        total = self.db.execute(f"SELECT COUNT(*) FROM posts WHERE {sql_where}", args).fetchone()[0]
        rows = self.db.execute(
            f"""SELECT p.*, (SELECT COUNT(*) FROM posts r WHERE r.parent_code = p.code) AS saved_replies
                FROM posts p WHERE {sql_where} ORDER BY {order} LIMIT ? OFFSET ?""",
            [*args, limit, offset],
        )
        return [_row_to_post(r) for r in rows], total

    def get_post(self, code: str) -> dict | None:
        row = self.db.execute("SELECT * FROM posts WHERE code = ?", (code,)).fetchone()
        if not row:
            return None
        post = _row_to_post(row)
        post["replies"] = [
            _row_to_post(r)
            for r in self.db.execute(
                "SELECT * FROM posts WHERE parent_code = ? ORDER BY published_at ASC", (code,)
            )
        ]
        return post

    def export_json(self, out: Path, query: str | None = None) -> int:
        """Export root posts (with nested replies) to a JSON file. Returns number of posts exported."""
        posts, _ = self.list_posts(query=query, limit=-1)
        result = [self.get_post(p["code"]) for p in posts]
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        return len(result)

    # ── Video đã xuất ─────────────────────────────────────────────────────────

    def add_video(self, post_code: str, title: str, path: Path, thumb: Path, duration: float, config: dict) -> int:
        cur = self.db.execute(
            "INSERT INTO videos (post_code, title, path, thumb_path, duration, config_json) VALUES (?, ?, ?, ?, ?, ?)",
            (post_code, title, str(path), str(thumb), duration, json.dumps(config, ensure_ascii=False)),
        )
        self.db.commit()
        return cur.lastrowid

    def list_videos(self) -> list[dict]:
        rows = self.db.execute(
            """SELECT v.*, COALESCE(p.username, d.username) AS username, COALESCE(p.query, d.query) AS query
               FROM videos v LEFT JOIN posts p ON p.code = v.post_code
               LEFT JOIN done_posts d ON d.code = v.post_code ORDER BY v.id DESC"""
        )
        result = []
        for r in rows:
            d = dict(r)
            d["config"] = json.loads(d.pop("config_json") or "{}")
            result.append(d)
        return result

    def delete_video(self, video_id: int) -> None:
        row = self.db.execute("SELECT path, thumb_path FROM videos WHERE id = ?", (video_id,)).fetchone()
        if row:
            for p in (row["path"], row["thumb_path"]):
                if p:
                    Path(p).unlink(missing_ok=True)
        self.db.execute("DELETE FROM videos WHERE id = ?", (video_id,))
        self.db.commit()
