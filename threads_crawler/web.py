"""FastAPI web UI: crawl theo từ khóa, kho bài, tạo video, video đã xuất."""

from __future__ import annotations

import asyncio
import io
import json
import logging
import subprocess
import threading
from datetime import datetime
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

from . import cards, keywords, live, media, product, review_video, shopee, shopee_reviews, tts, video
from .crawler import DEFAULT_PROFILE_DIR
from .jobs import JobManager
from .pipeline import CrawlOptions, CrawlTarget, crawl, parse_post_url, parse_username
from .quality import QualityFilter
from .storage import Store

log = logging.getLogger(__name__)
STATIC_DIR = Path(__file__).parent / "static"
PACKAGE_DIR = Path(__file__).parent
UPLOAD_CATEGORY = "video_của_tôi"
MAX_UPLOAD_BYTES = 1024**3


class NoCacheStaticFiles(StaticFiles):
    """Giao diện luôn được kiểm tra lại (ETag) để sửa code xong tải lại là thấy, không dính cache cũ."""

    def file_response(self, *args, **kwargs):
        resp = super().file_response(*args, **kwargs)
        resp.headers["Cache-Control"] = "no-cache"
        return resp


def code_stamp() -> float:
    """Lần sửa code Python gần nhất. Khác lúc server khởi động = server đang chạy code cũ."""
    return max(f.stat().st_mtime for f in PACKAGE_DIR.glob("*.py"))


class CrawlRequest(BaseModel):
    keywords: list[str] = []
    urls: list[str] = []  # link bài cụ thể
    users: list[str] = []  # tên tài khoản: crawl các bài của người đó
    limit: int = Field(20, ge=1, le=200)
    replies: int = Field(10, ge=0, le=100)  # số mạch bình luận mỗi bài
    branches: int = Field(3, ge=1, le=20)  # số câu trả lời giữ ở mỗi bình luận, mọi tầng
    expand_pages: int = Field(60, ge=0, le=500)  # chặn số trang mở thêm mỗi bài để lấy nhánh bị ẩn
    min_likes: int = Field(0, ge=0)
    min_replies: int = Field(3, ge=0)
    max_age_days: int = Field(14, ge=0)  # 0 = không giới hạn
    tabs: Literal["both", "top", "recent"] = "both"
    clean: bool = True  # bỏ bài quá ngắn (không ảnh) và bài quảng cáo
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


class SourceRequest(BaseModel):
    username: str


class ProductRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    intro: str = Field(min_length=1, max_length=3000)
    price: str = Field("", max_length=60)
    source: str = Field(min_length=1, max_length=30)  # đánh giá lấy từ đâu, hiện trên thẻ
    reviews: str = Field(min_length=1, max_length=50000)
    cta: str = Field("", max_length=300)
    image: str | None = None  # data URL


class ProductFetchRequest(BaseModel):
    url: str = Field(min_length=1, max_length=2000)


class ShopeeReviewsRequest(BaseModel):
    urls: list[str]
    limit: int = Field(200, ge=1, le=5000)
    download_videos: bool = False
    refresh: bool = False
    videos_only: bool = False
    delay: float = Field(3, ge=1, le=60)  # không cho đặt dưới 1 giây để giữ nhịp truy cập
    timeout: float = Field(45, ge=5, le=300)
    retries: int = Field(2, ge=0, le=5)


class IngestRequest(BaseModel):
    page_url: str = Field(max_length=8000)
    api_url: str = Field(max_length=8000)
    body: str = Field(max_length=5_000_000)


class MarkRequest(BaseModel):
    picked: bool = False
    claim: str = Field("", max_length=500)
    permission: Literal["none", "asked", "granted", "denied"] = "none"


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
        quality = QualityFilter(
            max_age_days=req.max_age_days, min_likes=req.min_likes, min_replies=req.min_replies,
            min_chars=QualityFilter.min_chars if req.clean else 0, skip_ads=req.clean,
        )
        opts = CrawlOptions(
            limit=req.limit, replies=req.replies, branches=req.branches, expand_pages=req.expand_pages, tabs=req.tabs, quality=quality,
            media=req.media, headless=not req.show_browser, profile=profile,
        )

        def run(progress):
            job_store = Store(data_dir)
            try:
                stats = asyncio.run(crawl(job_store, targets, opts, progress))
            finally:
                job_store.close()
            return {"posts": stats.posts, "replies": stats.replies, "errors": stats.errors,
                    "skipped": dict(stats.skipped.most_common())}

        def short(t: CrawlTarget) -> str:
            return f"🔗 {parse_post_url(t.query)[1]}" if t.kind == "post" else t.label

        title = ", ".join(short(t) for t in targets[:3]) + (f" +{len(targets) - 3}" if len(targets) > 3 else "")
        kinds = {"search": "🔎", "post": "🔗", "user": "👤"}
        detail = [f"{kinds[t.kind]} {t.query if t.kind == 'post' else t.label}" for t in targets]
        detail.append(
            f"⚙️ {req.limit} bài/mục, {req.replies} bình luận/bài, {req.branches} nhánh (≤{req.expand_pages} trang mở thêm), trong {req.max_age_days or '∞'} ngày, "
            f"≥{req.min_likes} like, ≥{req.min_replies} bình luận, tab {req.tabs}"
            + (", lọc rác" if req.clean else "")
        )
        return jobs.submit("crawl", f"Crawl: {title}", run, detail)

    # ── Kho bài ──────────────────────────────────────────────────────────────

    @app.get("/api/keywords")
    def keyword_suggestions():
        return keywords.suggestions(store)

    @app.get("/api/sources")
    def sources():
        return {"sources": store.list_sources(), "suggested": store.suggest_sources()}

    @app.post("/api/sources")
    def add_source(req: SourceRequest):
        name = parse_username(req.username)
        if not name:
            raise HTTPException(400, f"Tên tài khoản không hợp lệ: {req.username}")
        store.add_source(name)
        return {"ok": True, "username": name}

    @app.delete("/api/sources/{username}")
    def remove_source(username: str):
        store.remove_source(username)
        return {"ok": True}

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

    @app.delete("/api/posts")
    def delete_unstarred_posts():
        return {"ok": True, "deleted": store.delete_unstarred()}

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

    # ── Sản phẩm ─────────────────────────────────────────────────────────────

    @app.post("/api/products")
    def create_product(req: ProductRequest):
        try:
            return {"code": product.create(store, req.name, req.intro, req.price, req.source, req.reviews, req.cta, req.image)}
        except ValueError as e:
            raise HTTPException(400, str(e))

    @app.post("/api/products/fetch")
    def fetch_product(req: ProductFetchRequest):
        if not shopee.is_shopee_url(req.url):
            raise HTTPException(400, "Chỉ nhận link shopee.vn")
        url = req.url.strip()
        return jobs.submit("crawl", f"Shopee: {shopee.parse_item_name(url)}",
                           lambda progress: asyncio.run(shopee.fetch_product(url, progress)), [f"🔗 {url}"])

    # ── Review Shopee ────────────────────────────────────────────────────────

    reviews_dir = data_dir / "shopee_reviews"

    @app.post("/api/shopee-reviews/ingest")
    def ingest_shopee_reviews(req: IngestRequest, request: Request):
        # Web bất kỳ trên trình duyệt cũng gọi được địa chỉ 127.0.0.1, nên chỉ nhận từ extension
        if not request.headers.get("origin", "").startswith("chrome-extension://"):
            raise HTTPException(403, "Chỉ nhận dữ liệu từ extension")
        try:
            body = json.loads(req.body)
        except ValueError:
            return {"rejected": True}
        return shopee_reviews.ingest_response(reviews_dir, req.page_url, req.api_url, body)

    @app.put("/api/shopee-reviews/{stem}/marks/{review_id}")
    def mark_shopee_review(stem: str, review_id: str, req: MarkRequest):
        if not shopee_reviews.set_mark(reviews_dir, stem, review_id, req.picked, req.claim, req.permission):
            raise HTTPException(404, "Không tìm thấy review")
        return {"ok": True}

    @app.delete("/api/shopee-reviews/{stem}/picks")
    def clear_shopee_picks(stem: str):
        if shopee_reviews.picked_reviews(reviews_dir, stem) is None:
            raise HTTPException(404, "Không tìm thấy sản phẩm")
        shopee_reviews.clear_picks(reviews_dir, stem)
        return {"ok": True}

    @app.get("/api/shopee-reviews/{stem}/test-list.csv")
    def shopee_test_list(stem: str):
        rows = shopee_reviews.picked_reviews(reviews_dir, stem)
        if rows is None:
            raise HTTPException(404, "Không tìm thấy sản phẩm")
        return Response(shopee_reviews.test_list_csv(rows), media_type="text/csv; charset=utf-8",
                        headers={"Content-Disposition": f'attachment; filename="test_{stem}.csv"'})

    @app.post("/api/shopee-reviews/{stem}/download-picked")
    def download_shopee_picked(stem: str):
        if not shopee_reviews.picked_reviews(reviews_dir, stem):
            raise HTTPException(400, "Chưa chọn bình luận nào")

        def run(progress):
            r = shopee_reviews.download_picked(reviews_dir, stem, progress=progress)
            return {"kind": "shopee_reviews", "added": 0, "videos_saved": r["saved"], "videos_failed": r["failed"],
                    "problems": r["problems"], "blocked": False}

        return jobs.submit("crawl", "Review Shopee: Tải video đã chọn", run, [f"📦 {stem}"])

    @app.post("/api/shopee-reviews")
    def start_shopee_reviews(req: ShopeeReviewsRequest):
        refs, bad = {}, []
        for u in filter(str.strip, req.urls):
            if ref := shopee_reviews.parse_item_ref(u):
                refs[ref.stem] = ref
            else:
                bad.append(u.strip())
        if bad:
            raise HTTPException(400, "Không phải link sản phẩm shopee.vn có id: " + ", ".join(bad[:3]))
        if not refs:
            raise HTTPException(400, "Nhập ít nhất một link sản phẩm")
        targets = list(refs.values())

        def run(progress):
            opts = shopee_reviews.Options(
                limit=req.limit, delay=req.delay, timeout=req.timeout, retries=req.retries, refresh=req.refresh,
                download_videos=req.download_videos or req.videos_only, videos_only=req.videos_only, progress=progress,
            )
            results = asyncio.run(shopee_reviews.crawl_reviews(targets, reviews_dir, opts))
            return {
                "kind": "shopee_reviews",
                "products": len(targets),
                "done": len(results),
                "added": sum(r.added for r in results),
                "videos_saved": sum(r.videos_saved for r in results),
                "videos_failed": sum(r.videos_failed for r in results),
                "problems": [f"{r.ref.stem}: {r.error}" for r in results if r.error],
                "blocked": any(r.status == "blocked" for r in results),
            }

        action = "Tải video" if req.videos_only else "Quét"
        detail = [f"🔗 {t.url}" for t in targets]
        detail.append(f"⚙️ tối đa {req.limit} đánh giá/sản phẩm, nghỉ {req.delay:g} giây/trang, "
                      f"tải video: {'có' if req.download_videos or req.videos_only else 'không'}")
        return jobs.submit("crawl", f"Review Shopee: {action} {len(targets)} sản phẩm", run, detail)

    @app.get("/api/shopee-reviews")
    def shopee_review_products():
        items = shopee_reviews.list_products(reviews_dir)
        for p in items:
            p["jsonl"], p["csv"] = media_url(p["jsonl"], data_dir), media_url(p["csv"], data_dir)
        merged = reviews_dir / shopee_reviews.ALL_REVIEWS
        return {"items": items, "all_jsonl": media_url(str(merged), data_dir) if merged.exists() else None,
                "all_csv": media_url(str(merged.with_suffix(".csv")), data_dir) if merged.exists() else None}

    @app.get("/api/shopee-reviews/{stem}")
    def shopee_review_list(stem: str, page: int = 1, video_only: bool = False, rating: int = 0, q: str = "",
                           picked_only: bool = False):
        data = shopee_reviews.read_reviews(reviews_dir, stem, max(page, 1), 20, video_only, rating, q, picked_only)
        if data is None:
            raise HTTPException(404, "Không tìm thấy sản phẩm")
        for r in data["items"]:
            for v in r["videos"]:
                v["local"] = media_url(v["local"], data_dir)
        return data

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

    @app.post("/api/materials/upload")
    async def upload_clip(request: Request, name: str):
        """Thêm video của người dùng vào thư viện meme, để chèn và chỉnh trong trình dựng như một meme."""
        folder, exts = media.KINDS["memes"]
        src = Path(name).name
        suffix = Path(src).suffix.lower()
        if suffix not in exts:
            raise HTTPException(400, "Chỉ nhận video .mp4, .mov, .webm hoặc .gif")
        dest_dir = media.MATERIALS_DIR / folder / UPLOAD_CATEGORY
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / src
        n = 1
        while dest.exists():
            dest = dest_dir / f"{Path(src).stem}_{n}{suffix}"
            n += 1
        size = 0
        try:
            with dest.open("wb") as f:
                async for chunk in request.stream():
                    size += len(chunk)
                    if size > MAX_UPLOAD_BYTES:
                        raise HTTPException(413, "Video quá lớn (tối đa 1GB)")
                    f.write(chunk)
            await asyncio.to_thread(media.probe, dest)
        except HTTPException:
            dest.unlink(missing_ok=True)
            raise
        except RuntimeError:
            dest.unlink(missing_ok=True)
            raise HTTPException(400, "Không đọc được file video")
        return {"path": dest.relative_to(media.MATERIALS_DIR / folder).as_posix()}

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
            src = media.safe_material(path, kind)
        except (FileNotFoundError, KeyError):
            raise HTTPException(404)
        # nhạc nền: bản đã chuẩn hóa độ to, giống hệt bản dùng khi xuất video
        if kind == "music":
            return FileResponse(media.normalized_music(src), media_type="audio/mp4")
        return FileResponse(src)

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
        detail = [f"🎬 {len(project.segments)} thẻ, {len(project.memes)} meme"]
        return jobs.submit("render", f"Video: {label}", run, detail)

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

    @app.post("/api/jobs/{job_id}/cancel")
    def cancel_job(job_id: int):
        if not jobs.cancel(job_id):
            raise HTTPException(400, "Tác vụ đã xong hoặc không tồn tại")
        return {"ok": True}

    @app.get("/api/jobs/{job_id}")
    def get_job(job_id: int):
        job = jobs.get(job_id)
        if not job:
            raise HTTPException(404)
        return job

    def warm_music() -> None:  # chuẩn hóa sẵn nhạc nền ở nền để lần nghe thử đầu không phải chờ
        for m in media.list_materials()["music"]:
            try:
                media.normalized_music(media.safe_material(m["path"], "music"))
            except (RuntimeError, FileNotFoundError) as e:
                log.warning("Không chuẩn hóa được nhạc %s: %s", m["path"], e)

    threading.Thread(target=warm_music, name="warm-music", daemon=True).start()

    tts.CACHE_DIR.mkdir(parents=True, exist_ok=True)
    app.include_router(live.make_router(data_dir))
    app.include_router(review_video.make_router(store, jobs, data_dir, lambda: code_stamp() > started_stamp,
                                                lambda p: media_url(p, data_dir)))

    app.mount("/tts", StaticFiles(directory=tts.CACHE_DIR), name="tts")
    app.mount("/files", StaticFiles(directory=data_dir), name="files")
    app.mount("/", NoCacheStaticFiles(directory=STATIC_DIR, html=True), name="static")
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
    uvicorn.run(create_app(data_dir, profile), host=host, port=port, log_level="warning", timeout_graceful_shutdown=2)  # luồng SSE của tab LIVE không tự đóng
