"""SQLite storage for crawled posts and rendered videos, plus media download and JSON export."""

from __future__ import annotations

import json
import logging
import re
import shutil
import sqlite3
import threading
from datetime import datetime
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
    used INTEGER DEFAULT 0,    -- đánh dấu đã dùng làm video
    starred INTEGER DEFAULT 0  -- bài yêu thích (gắn sao) để làm content sau
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
    publish_at TEXT,           -- giờ dự định đăng TikTok (YYYY-MM-DDTHH:MM, giờ máy)
    created_at TEXT DEFAULT (datetime('now'))
);

-- Dự án video đang dở (trạng thái trình dựng, lưu tự động); xuất video xong thì xóa.
CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY,
    data_json TEXT,
    created_at TEXT,
    updated_at TEXT            -- lần chỉnh/lưu cuối (giờ máy, ISO có múi giờ)
);
-- Dự án đã xuất video: chặn trình dựng còn mở lưu lại (hồi sinh) dự án đã xóa.
CREATE TABLE IF NOT EXISTS finished_projects (
    id TEXT PRIMARY KEY,
    video_id INTEGER
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
    "posts": {"reply_to_username": "TEXT", "avatar_path": "TEXT", "starred": "INTEGER DEFAULT 0"},
    "videos": {"publish_at": "TEXT"},
}

# Video chưa hẹn giờ đăng nằm ở data/videos/<UNSCHEDULED>/, có hẹn giờ thì ở data/videos/<YYYY-MM-DD>/.
UNSCHEDULED = "chưa_hẹn_giờ"


def _row_to_post(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["media"] = json.loads(d.pop("media_json") or "[]")
    d["user_verified"] = bool(d["user_verified"])
    d["used"] = bool(d["used"])
    d["starred"] = bool(d.get("starred"))
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
        for table, columns in MIGRATIONS.items():
            cols = {r[1] for r in self.db.execute(f"PRAGMA table_info({table})")}
            if cols:
                for col, typ in columns.items():
                    if col not in cols:
                        self.db.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
        self.db.executescript(SCHEMA)
        # Bài đánh dấu "đã dùng" từ phiên bản cũ → chuyển sang cơ chế xóa khỏi kho.
        used = [r[0] for r in self.db.execute("SELECT code FROM posts WHERE used = 1 AND parent_code IS NULL")]
        if used:
            self.archive_used(used)
        # Video xuất từ phiên bản cũ nằm thẳng trong data/videos/ → gom vào thư mục chưa hẹn giờ.
        for row in self.db.execute("SELECT id, path FROM videos WHERE publish_at IS NULL").fetchall():
            if Path(row["path"]).parent == self.videos_dir:
                self.reschedule_video(row["id"], None)

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
        # Giữ lại đường dẫn media đã tải, trạng thái đã dùng và sao nếu post được crawl lại.
        old = self.db.execute(
            "SELECT media_json, used, avatar_path, starred FROM posts WHERE code = ?", (post["code"],)
        ).fetchone()
        if old:
            old_paths = {m["url"]: m.get("path") for m in json.loads(old["media_json"] or "[]")}
            for m in post["media"]:
                m.setdefault("path", old_paths.get(m["url"]))
        self.db.execute(
            """INSERT OR REPLACE INTO posts
               (code, id, url, username, user_verified, text, published_at, like_count, reply_count,
                repost_count, quote_count, parent_code, reply_to_username, query, avatar_path,
                media_json, used, starred)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                post["code"], post["id"], post["url"], post["username"], int(post["user_verified"]),
                post["text"], post["published_at"], post["like_count"], post["reply_count"],
                post["repost_count"], post["quote_count"], parent_code, post["reply_to_username"], query,
                post.get("avatar_path") or (old["avatar_path"] if old else None),
                json.dumps(post["media"], ensure_ascii=False), old["used"] if old else 0,
                (old["starred"] or 0) if old else 0,
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

    def set_starred(self, code: str, starred: bool) -> None:
        self.db.execute("UPDATE posts SET starred = ? WHERE code = ? AND parent_code IS NULL", (int(starred), code))
        self.db.commit()

    def count_starred(self) -> int:
        return self.db.execute("SELECT COUNT(*) FROM posts WHERE starred = 1 AND parent_code IS NULL").fetchone()[0]

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
        starred: bool = False,
    ) -> tuple[list[dict], int]:
        where, args = ["parent_code IS NULL"], []
        if starred:
            where.append("starred = 1")
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

    # ── Dự án đang dở ─────────────────────────────────────────────────────────

    def save_project(self, project_id: str, data: dict) -> bool:
        """Lưu (ghi đè) dự án. False nếu dự án này đã xuất video rồi."""
        if self.db.execute("SELECT 1 FROM finished_projects WHERE id = ?", (project_id,)).fetchone():
            return False
        now = datetime.now().astimezone().isoformat(timespec="seconds")
        self.db.execute(
            """INSERT INTO projects (id, data_json, created_at, updated_at) VALUES (?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET data_json = excluded.data_json, updated_at = excluded.updated_at""",
            (project_id, json.dumps(data, ensure_ascii=False), now, now),
        )
        self.db.commit()
        return True

    def get_project(self, project_id: str) -> dict | None:
        row = self.db.execute("SELECT data_json FROM projects WHERE id = ?", (project_id,)).fetchone()
        return json.loads(row["data_json"]) if row else None

    def list_projects(self) -> list[dict]:
        """Tóm tắt các dự án đang dở, mới chỉnh gần nhất trước."""
        result = []
        for row in self.db.execute("SELECT * FROM projects ORDER BY updated_at DESC"):
            d = json.loads(row["data_json"] or "{}")
            segs = d.get("segments") or []
            result.append({
                "id": row["id"],
                "name": d.get("name") or "",
                "preview": next((s.get("display_text") for s in segs if s.get("display_text")), ""),
                "usernames": list(dict.fromkeys(s.get("username") for s in segs if s.get("kind") == "post")),
                "posts": len(d.get("post_codes") or []),
                "segments": len(segs),
                "memes": len(d.get("memes") or []),
                "sfx": len(d.get("sfx") or []),
                "publish_at": d.get("publish_at"),
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
            })
        return result

    def delete_project(self, project_id: str) -> None:
        self.db.execute("DELETE FROM projects WHERE id = ?", (project_id,))
        self.db.commit()

    def finish_project(self, project_id: str, video_id: int) -> None:
        """Đã xuất video: xóa tiến trình và ghi nhớ để không bị lưu lại."""
        self.db.execute("INSERT OR REPLACE INTO finished_projects (id, video_id) VALUES (?, ?)", (project_id, video_id))
        self.delete_project(project_id)

    # ── Video đã xuất ─────────────────────────────────────────────────────────

    def video_dest(self, publish_at: str | None, title: str, code: str, current: Path | None = None) -> Path:
        """Đường dẫn file video theo lịch đăng: videos/2026-10-25/19h30 - <tiêu đề>.mp4.

        `current` là file hiện tại của chính video này (khi đổi giờ), không tính là trùng tên.
        """
        if publish_at:
            when = datetime.fromisoformat(publish_at)
            folder, prefix = when.strftime("%Y-%m-%d"), when.strftime("%Hh%M")
        else:
            folder, prefix = UNSCHEDULED, datetime.now().strftime("%Y%m%d_%H%M")
        name = re.sub(r'[\\/:*?"<>|\s]+', " ", title).strip()
        if len(name) > 60:  # cắt ở ranh giới từ cho tên file dễ đọc
            name = name[:60].rsplit(" ", 1)[0]
        name = name or code
        dest = self.videos_dir / folder / f"{prefix} - {name}.mp4"
        n = 2
        while dest.exists() and dest != current:
            dest = dest.with_name(f"{prefix} - {name} ({n}).mp4")
            n += 1
        return dest

    def place_video(self, src: Path, thumb: Path | None, dest: Path) -> tuple[Path, Path | None]:
        """Chuyển file video (kèm ảnh bìa cùng tên .jpg) tới dest; xóa thư mục cũ nếu rỗng."""
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(src, dest)
        new_thumb = None
        if thumb and thumb.exists():
            new_thumb = dest.with_suffix(".jpg")
            shutil.move(thumb, new_thumb)
        if src.parent != dest.parent and src.parent.parent == self.videos_dir and not any(src.parent.iterdir()):
            src.parent.rmdir()
        return dest, new_thumb

    def add_video(
        self, post_code: str, title: str, path: Path, thumb: Path, duration: float, config: dict,
        publish_at: str | None = None,
    ) -> int:
        cur = self.db.execute(
            """INSERT INTO videos (post_code, title, path, thumb_path, duration, config_json, publish_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (post_code, title, str(path), str(thumb), duration, json.dumps(config, ensure_ascii=False), publish_at),
        )
        self.db.commit()
        return cur.lastrowid

    def reschedule_video(self, video_id: int, publish_at: str | None) -> None:
        """Đổi giờ đăng và chuyển file sang thư mục của ngày mới."""
        row = self.db.execute("SELECT * FROM videos WHERE id = ?", (video_id,)).fetchone()
        if not row:
            raise KeyError(video_id)
        path = Path(row["path"])
        thumb = Path(row["thumb_path"]) if row["thumb_path"] else None
        dest = self.video_dest(publish_at, row["title"], row["post_code"], current=path)
        if path.exists() and dest != path:
            path, thumb = self.place_video(path, thumb, dest)
        self.db.execute(
            "UPDATE videos SET publish_at = ?, path = ?, thumb_path = ? WHERE id = ?",
            (publish_at, str(path), str(thumb) if thumb else None, video_id),
        )
        self.db.commit()

    def list_videos(self) -> list[dict]:
        rows = self.db.execute(
            """SELECT v.*, COALESCE(p.username, d.username) AS username, COALESCE(p.query, d.query) AS query
               FROM videos v LEFT JOIN posts p ON p.code = v.post_code
               LEFT JOIN done_posts d ON d.code = v.post_code
               ORDER BY v.publish_at IS NULL, v.publish_at, v.id DESC"""
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
            folder = Path(row["path"]).parent
            if folder.parent == self.videos_dir and folder.exists() and not any(folder.iterdir()):
                folder.rmdir()
        self.db.execute("DELETE FROM videos WHERE id = ?", (video_id,))
        self.db.commit()
