"""FastAPI web UI: crawl theo từ khóa, kho bài, tạo video, video đã xuất."""

from __future__ import annotations

import asyncio
import io
import logging
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import media, tts, video
from .crawler import DEFAULT_PROFILE_DIR
from .jobs import JobManager
from .pipeline import CrawlOptions, CrawlTarget, crawl
from .storage import Store

log = logging.getLogger(__name__)
STATIC_DIR = Path(__file__).parent / "static"


class CrawlRequest(BaseModel):
    keywords: list[str] = []
    limit: int = Field(20, ge=1, le=200)
    replies: int = Field(10, ge=0, le=100)
    min_likes: int = Field(0, ge=0)
    recent: bool = False
    media: bool = True
    show_browser: bool = False


class ScriptRequest(BaseModel):
    code: str
    reply_codes: list[str] = []
    max_lines: int = Field(11, ge=4, le=20)
    with_image: bool = True


class SampleRequest(BaseModel):
    voice: str
    text: str = "Xin chào các bạn, đây là giọng đọc thử cho video TikTok của mình."
    precision: str = "fp32"


class UsedRequest(BaseModel):
    used: bool


def create_app(data_dir: Path, profile: Path = DEFAULT_PROFILE_DIR) -> FastAPI:
    app = FastAPI(title="Threads → TikTok")
    jobs = JobManager()
    data_dir = data_dir.resolve()
    store = Store(data_dir)  # sqlite connection dùng chung (check_same_thread=False)

    def with_urls(p: dict) -> dict:
        p["avatar_src"] = media_url(p.get("avatar_path"), data_dir)
        for m in p["media"]:
            m["src"] = media_url(m.get("path"), data_dir)
        return p

    # ── Crawl ────────────────────────────────────────────────────────────────

    @app.post("/api/crawl")
    def start_crawl(req: CrawlRequest):
        targets = [CrawlTarget(k.strip()) for k in req.keywords if k.strip()]
        if not targets:
            raise HTTPException(400, "Nhập ít nhất một từ khóa")
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

        title = ", ".join(t.query for t in targets[:3]) + (f" +{len(targets) - 3}" if len(targets) > 3 else "")
        return jobs.submit("crawl", f"Crawl: {title}", run)

    # ── Kho bài ──────────────────────────────────────────────────────────────

    @app.get("/api/queries")
    def queries():
        return store.list_queries()

    @app.get("/api/posts")
    def posts(
        query: str | None = None, q: str | None = None,
        used: bool | None = None, sort: str = "likes", limit: int = 30, offset: int = 0,
    ):
        items, total = store.list_posts(query, q, used, sort, limit, offset)
        return {"items": [with_urls(p) for p in items], "total": total}

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

    @app.post("/api/posts/{code}/used")
    def set_used(code: str, req: UsedRequest):
        store.mark_used([code], req.used)
        return {"ok": True}

    # ── Tạo video ────────────────────────────────────────────────────────────

    @app.get("/api/materials")
    def materials():
        return media.list_materials()

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

    @app.post("/api/voices/sample")
    def voice_sample(req: SampleRequest):
        wav = tts.synthesize(tts.normalize_for_speech(req.text), req.voice, req.precision)
        return FileResponse(wav, media_type="audio/wav")

    @app.post("/api/script")
    def script(req: ScriptRequest):
        p = store.get_post(req.code)
        if not p:
            raise HTTPException(404, "Không tìm thấy bài")
        return video.build_script(p, req.reply_codes, req.max_lines, req.with_image)

    @app.post("/api/preview")
    def preview(spec: video.RenderSpec, segment: int = 0):
        img = video.preview(spec, segment)
        img.thumbnail((540, 960))
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=85)
        return Response(buf.getvalue(), media_type="image/jpeg")

    @app.post("/api/render")
    def start_render(spec: video.RenderSpec):
        if not spec.segments:
            raise HTTPException(400, "Kịch bản trống")

        def run(progress):
            job_store = Store(data_dir)
            try:
                return video.render(job_store, spec, progress)
            finally:
                job_store.close()

        label = spec.title.strip() or spec.segments[0].display_text[:40]
        return jobs.submit("render", f"Video: {label}", run)

    # ── Video đã xuất ────────────────────────────────────────────────────────

    @app.get("/api/videos")
    def videos():
        items = store.list_videos()
        for v in items:
            v["url"] = f"/files/videos/{Path(v['path']).name}"
            v["thumb_url"] = f"/files/videos/{Path(v['thumb_path']).name}" if v["thumb_path"] else None
        return items

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

    app.mount("/files", StaticFiles(directory=data_dir), name="files")
    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
    return app


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
