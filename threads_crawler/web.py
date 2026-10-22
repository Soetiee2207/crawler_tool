"""FastAPI web UI: crawl theo từ khóa, kho bài, tạo video, video đã xuất."""

from __future__ import annotations

import asyncio
import io
import json
import logging
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

from . import cards, keywords, media, tts, video
from .crawler import DEFAULT_PROFILE_DIR
from .jobs import JobManager
from .pipeline import CrawlOptions, CrawlTarget, crawl, parse_post_url, parse_username
from .storage import Store

log = logging.getLogger(__name__)
STATIC_DIR = Path(__file__).parent / "static"
PACKAGE_DIR = Path(__file__).parent


def code_stamp() -> float:
    """Lần sửa code Python gần nhất. Khác lúc server khởi động = server đang chạy code cũ."""
    return max(f.stat().st_mtime for f in PACKAGE_DIR.glob("*.py"))


class CrawlRequest(BaseModel):
    keywords: list[str] = []
    urls: list[str] = []  # link bài cụ thể
    users: list[str] = []  # tên tài khoản: crawl các bài của người đó
    limit: int = Field(20, ge=1, le=200)
    replies: int = Field(10, ge=0, le=100)
    min_likes: int = Field(0, ge=0)
    recent: bool = False
    media: bool = True
    show_browser: bool = False


class ScriptItem(BaseModel):
    code: str
    reply_codes: list[str] = []


class ScheduleRequest(BaseModel):
    publish_at: str | None = None

    @field_validator("publish_at")
    @classmethod
    def _check(cls, v: str | None) -> str | None:
        return datetime.fromisoformat(v).strftime("%Y-%m-%dT%H:%M") if v else None


class ScriptRequest(BaseModel):
    items: list[ScriptItem]
    max_lines: int = Field(11, ge=4, le=20)
    with_image: bool = True


class TtsRequest(BaseModel):
    text: str
    voice: str = tts.DEFAULT_VOICE
    precision: Literal["fp32", "int8"] = "fp32"
    normalize: bool = False  # True = làm sạch text trước (dùng cho câu nghe thử)


class StarRequest(BaseModel):
    starred: bool


class CardRequest(BaseModel):
    segment: video.Segment
    theme: Literal["light", "dark"] = "dark"


def create_app(data_dir: Path, profile: Path = DEFAULT_PROFILE_DIR) -> FastAPI:
    app = FastAPI(title="Threads → TikTok")
    jobs = JobManager()
    data_dir = data_dir.resolve()
    store = Store(data_dir)  # mỗi luồng tự mở kết nối sqlite riêng
    started_stamp = code_stamp()

    def with_urls(p: dict) -> dict:
        p["avatar_src"] = media_url(p.get("avatar_path"), data_dir)
        for m in p["media"]:
            m["src"] = media_url(m.get("path"), data_dir)
        return p

    # ── Crawl ────────────────────────────────────────────────────────────────

    @app.post("/api/crawl")
    def start_crawl(req: CrawlRequest):
        targets = [CrawlTarget(k.strip()) for k in req.keywords if k.strip()]
        bad = []
        for u in filter(str.strip, req.urls):
            if parse_post_url(u):
                targets.append(CrawlTarget(u.strip(), "post"))
            else:
                bad.append(u.strip())
        for u in filter(str.strip, req.users):
            if name := parse_username(u):
                targets.append(CrawlTarget(name, "user"))
            else:
                bad.append(u.strip())
        if bad:
            raise HTTPException(400, "Không hiểu: " + ", ".join(bad[:3]))
        if not targets:
            raise HTTPException(400, "Nhập ít nhất một từ khóa, link bài hoặc tài khoản")
        opts = CrawlOptions(
            limit=req.limit, replies=req.replies, min_likes=req.min_likes, recent=req.recent,
            media=req.media, headless=not req.show_browser, profile=profile,
        )

        def run(progress):
            job_store = Store(data_dir)
            try:
                stats = asyncio.run(crawl(job_store, targets, opts, progress))
            finally:
                job_store.close()
            return {"posts": stats.posts, "replies": stats.replies, "errors": stats.errors}

        def short(t: CrawlTarget) -> str:
            return f"🔗 {parse_post_url(t.query)[1]}" if t.kind == "post" else t.label

        title = ", ".join(short(t) for t in targets[:3]) + (f" +{len(targets) - 3}" if len(targets) > 3 else "")
        return jobs.submit("crawl", f"Crawl: {title}", run)

    # ── Kho bài ──────────────────────────────────────────────────────────────

    @app.get("/api/keywords")
    def keyword_suggestions():
        return keywords.suggestions(store)

    @app.get("/api/queries")
    def queries():
        return store.list_queries()

    @app.get("/api/posts")
    def posts(
        query: str | None = None, q: str | None = None,
        sort: str = "likes", limit: int = 30, offset: int = 0, starred: bool = False,
    ):
        items, total = store.list_posts(query, q, sort, limit, offset, starred)
        return {"items": [with_urls(p) for p in items], "total": total, "starred": store.count_starred()}

    @app.put("/api/posts/{code}/star")
    def star_post(code: str, req: StarRequest):
        store.set_starred(code, req.starred)
        return {"ok": True, "starred": store.count_starred()}

    @app.get("/api/posts/{code}")
    def post(code: str):
        p = store.get_post(code)
        if not p:
            raise HTTPException(404, "Không tìm thấy bài")
        with_urls(p)
        for r in p["replies"]:
            with_urls(r)
        return p

    @app.delete("/api/posts/{code}")
    def delete_post(code: str):
        store.delete_post(code)
        return {"ok": True}

    # ── Tạo video ────────────────────────────────────────────────────────────

    @app.get("/api/materials")
    def materials():
        items = media.list_materials()
        for m in items["memes"]:  # trình phát xem trước cần biết màu phông + kích thước để tự tách nền
            src = media.safe_material(m["path"], "memes")
            info = media.probe(src)
            m |= {"key": media.detect_key_color(src), "width": info["width"], "height": info["height"],
                  "has_audio": info["has_audio"]}
        return items

    @app.get("/api/materials/thumb/{kind}/{path:path}")
    def material_thumb(kind: str, path: str):
        try:
            src = media.safe_material(path, kind)
        except (FileNotFoundError, KeyError):
            raise HTTPException(404)
        thumb = media.meme_thumb(src) if kind == "memes" else media.background_thumb(src)
        return FileResponse(thumb)

    @app.get("/api/materials/file/{kind}/{path:path}")
    def material_file(kind: str, path: str):
        try:
            return FileResponse(media.safe_material(path, kind))
        except (FileNotFoundError, KeyError):
            raise HTTPException(404)

    @app.get("/api/voices")
    def voices():
        return tts.list_voices()

    @app.post("/api/tts")
    def synthesize(req: TtsRequest):
        """Giọng đọc cho một thẻ (có cache). Trả về URL + thời lượng để timeline hiển thị đúng."""
        text = tts.normalize_for_speech(req.text) if req.normalize else req.text.strip()
        if not text:
            raise HTTPException(400, "Lời đọc trống")
        wav = tts.synthesize(text, req.voice, req.precision)
        return {"url": f"/tts/{wav.name}", "duration": tts.duration_of(wav)}

    @app.post("/api/script")
    def script(req: ScriptRequest):
        segments = []
        for item in req.items:
            p = store.get_post(item.code)
            if not p:
                raise HTTPException(404, f"Không tìm thấy bài {item.code}")
            segments += video.build_script(p, item.reply_codes, req.max_lines, req.with_image)
        return segments

    @app.post("/api/card")
    def card(req: CardRequest):
        return png_response(video.card_image(req.segment, req.theme, scaled=False))

    @app.get("/api/text-image")
    def text_image(text: str):
        return png_response(cards.render_title(text))

    @app.post("/api/render")
    def start_render(project: video.Project):
        if not project.segments:
            raise HTTPException(400, "Timeline chưa có thẻ nào")
        if code_stamp() > started_stamp:
            # trình dựng (file tĩnh) đã là bản mới nhưng server còn code cũ → video xuất ra sẽ lệch bản chỉnh
            raise HTTPException(409, "Code đã được cập nhật nhưng server chưa khởi động lại, video xuất ra sẽ khác "
                                     "bản đang chỉnh. Hãy tắt rồi chạy lại `threads-crawler web`, sau đó bấm Xuất video.")

        def run(progress):
            job_store = Store(data_dir)
            try:
                result = video.render(job_store, project, progress)
                if project.id:
                    job_store.finish_project(project.id, result["id"])
                return result | {"project_id": project.id}
            finally:
                job_store.close()

        label = project.name.strip() or project.segments[0].display_text[:40]
        return jobs.submit("render", f"Video: {label}", run)

    # ── Dự án đang dở ────────────────────────────────────────────────────────

    @app.get("/api/projects")
    def projects():
        return store.list_projects()

    @app.get("/api/projects/{project_id}")
    def get_project(project_id: str):
        p = store.get_project(project_id)
        if p is None:
            raise HTTPException(404, "Không tìm thấy dự án (có thể đã xuất video hoặc bị xóa)")
        return p

    @app.post("/api/projects/{project_id}")
    async def save_project(project_id: str, request: Request):
        # Nhận body thô: trình duyệt gửi bằng sendBeacon (text/plain) khi đóng trang.
        try:
            data = json.loads(await request.body())
        except ValueError:
            raise HTTPException(400, "Dữ liệu dự án không hợp lệ")
        if not isinstance(data, dict):
            raise HTTPException(400, "Dữ liệu dự án không hợp lệ")
        return {"saved": store.save_project(project_id, data)}

    @app.delete("/api/projects/{project_id}")
    def delete_project(project_id: str):
        store.delete_project(project_id)
        return {"ok": True}

    # ── Video đã xuất ────────────────────────────────────────────────────────

    @app.get("/api/videos")
    def videos():
        items = store.list_videos()
        for v in items:
            v["url"] = media_url(v["path"], data_dir)
            v["thumb_url"] = media_url(v["thumb_path"], data_dir)
            v["folder"] = str(Path(v["path"]).parent)
        return items

    @app.patch("/api/videos/{video_id}")
    def reschedule_video(video_id: int, req: ScheduleRequest):
        try:
            store.reschedule_video(video_id, req.publish_at)
        except KeyError:
            raise HTTPException(404, "Không tìm thấy video")
        return {"ok": True}

    @app.post("/api/videos/{video_id}/open")
    def open_video_folder(video_id: int):
        """Mở thư mục chứa video trong trình quản lý file của máy (tool chạy local)."""
        v = next((x for x in store.list_videos() if x["id"] == video_id), None)
        if not v:
            raise HTTPException(404, "Không tìm thấy video")
        try:
            subprocess.Popen(["xdg-open", str(Path(v["path"]).parent)], start_new_session=True,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError as e:
            raise HTTPException(500, f"Không mở được thư mục: {e}")
        return {"ok": True}

    @app.delete("/api/videos/{video_id}")
    def delete_video(video_id: int):
        store.delete_video(video_id)
        return {"ok": True}

    # ── Tác vụ nền ───────────────────────────────────────────────────────────

    @app.get("/api/jobs")
    def list_jobs():
        return jobs.list()

    @app.get("/api/jobs/{job_id}")
    def get_job(job_id: int):
        job = jobs.get(job_id)
        if not job:
            raise HTTPException(404)
        return job

    tts.CACHE_DIR.mkdir(parents=True, exist_ok=True)
    app.mount("/tts", StaticFiles(directory=tts.CACHE_DIR), name="tts")
    app.mount("/files", StaticFiles(directory=data_dir), name="files")
    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
    return app


def png_response(img) -> Response:
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return Response(buf.getvalue(), media_type="image/png", headers={"Cache-Control": "max-age=3600"})


def media_url(path: str | None, data_dir: Path) -> str | None:
    if not path:
        return None
    try:
        return "/files/" + str(Path(path).resolve().relative_to(data_dir.resolve()))
    except ValueError:
        return None


def serve(data_dir: Path, profile: Path, host: str, port: int) -> None:
    import uvicorn

    data_dir.mkdir(parents=True, exist_ok=True)
    print(f"Mở trình duyệt: http://{host}:{port}")
    uvicorn.run(create_app(data_dir, profile), host=host, port=port, log_level="warning")
