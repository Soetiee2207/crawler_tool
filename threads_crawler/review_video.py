"""Video review: ghép video và ảnh theo thứ tự thành video dọc, có thể kèm giọng đọc AI, không nhạc (chọn nhạc khi đăng TikTok).

Tách khỏi trình dựng Threads. Nguồn là file người dùng tự tải lên, hoặc video và ảnh trong review mà người mua đã đồng ý;
điều kiện này được kiểm tra ở cả lúc lấy file và lúc xuất video.
"""

from __future__ import annotations

import logging
import re
import shutil
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from PIL import Image, UnidentifiedImageError
from pydantic import BaseModel, Field

from . import cards, media, shopee_reviews as sr, tts
from .jobs import JobManager
from .storage import Store
from .video import FPS, H, W, WATERMARK_SPEED, WATERMARK_TEXT, WORK_DIR, _run_ffmpeg, bounce_expr

log = logging.getLogger(__name__)

VIDEO_EXTS = {".mp4", ".mov", ".webm", ".mkv"}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp"}
MAX_UPLOAD_BYTES = 1024**3
UPLOAD_ID = re.compile(r"[0-9a-f]{16}\.[a-z0-9]+")
MIN_LENGTH = 0.2
VOICE_TAIL = 0.25  # nghỉ một chút sau câu đọc rồi mới sang mục sau
CLIP_UNDER_VOICE = 0.15  # âm lượng tiếng gốc của clip khi đang có giọng đọc
SPOKEN_REVIEW_LEN = 180


class NotAllowed(Exception):
    """Người mua chưa đồng ý, hoặc không còn file nguồn."""


class Item(BaseModel):
    source: Literal["review_video", "review_image", "upload"]
    stem: str = ""
    review_id: str = ""
    index: int = Field(0, ge=0)
    upload_id: str = ""
    start: float = Field(0, ge=0)  # video: cắt từ giây này
    end: float | None = Field(None, ge=0)  # video: đến giây này, None = hết video
    duration: float = Field(3, ge=0.5, le=60)  # ảnh: hiện bao lâu
    keep_audio: bool = True
    voice_text: str = Field("", max_length=1500)


class Project(BaseModel):
    title: str = Field("", max_length=120)
    fit: Literal["blur", "crop"] = "blur"
    voice: str = tts.DEFAULT_VOICE
    speed: float = Field(1.1, ge=0.7, le=1.5)
    items: list[Item] = Field(min_length=1, max_length=100)


class FetchRequest(BaseModel):
    stem: str
    review_id: str
    kind: Literal["video", "image"]
    index: int = Field(0, ge=0)


class ScriptItem(BaseModel):
    source: Literal["review_video", "review_image", "upload"]
    stem: str = ""
    review_id: str = ""


class ScriptRequest(BaseModel):
    items: list[ScriptItem] = Field(min_length=1, max_length=100)


def _granted_row(reviews_dir: Path, stem: str, review_id: str) -> dict:
    for row in sr.picked_or_marked(reviews_dir, stem):
        if row["review_id"] == review_id and row["mark"].get("permission") == "granted":
            return row
    raise NotAllowed("Review này chưa ở trạng thái Đã đồng ý")


def fetch_review_media(reviews_dir: Path, stem: str, review_id: str, kind: str, index: int) -> Path:
    """Trả về file đã tải về máy của một video hoặc ảnh trong review người mua đã đồng ý."""
    row = _granted_row(reviews_dir, stem, review_id)
    urls = (row.get("video_url") or "").split() if kind == "video" else sr.review_images(reviews_dir, stem).get(review_id, [])
    if index >= len(urls):
        raise NotAllowed("Không tìm thấy file trong review")
    folder, suffix, types = (("_videos", ".mp4", sr.VIDEO_TYPES) if kind == "video" else ("_images", ".jpg", ("image/",)))
    dest = reviews_dir / f"{stem}{folder}" / f"{review_id}_{index}{suffix}"
    if not dest.exists():
        if not sr._is_video_host(urls[index]):
            raise NotAllowed("URL không thuộc miền của Shopee")
        dest.parent.mkdir(parents=True, exist_ok=True)
        sr.fetch_with_retry(urls[index], dest, types)
    return dest


def _shorten(text: str, limit: int = SPOKEN_REVIEW_LEN) -> str:
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    cut = text[:limit]
    end = max(cut.rfind(c) for c in ".!?")
    return cut[:end + 1] if end >= limit // 2 else cut.rsplit(" ", 1)[0] + "."


def draft_script(reviews_dir: Path, items: list[ScriptItem]) -> list[str]:
    """Bản nháp lời đọc, mỗi mục một đoạn, chỉ từ dữ liệu đã crawl. Không viết thay kết quả test: file của người dùng để trống."""
    stem = next((i.stem for i in items if i.source != "upload"), None)
    if stem is None:
        raise NotAllowed("Hãy thêm ít nhất một video hoặc ảnh review để có dữ liệu sản phẩm")
    if sr.read_reviews(reviews_dir, stem) is None:  # kiểm tra luôn `stem` đúng dạng
        raise NotAllowed("Không có dữ liệu của sản phẩm này")
    rows = sr._read_jsonl(reviews_dir / f"{stem}.jsonl")
    if not rows:
        raise NotAllowed("Không có dữ liệu của sản phẩm này")
    name = sr.parse_item_name(rows[0]["product_url"], 200)
    five = sum(r.get("rating") == 5 for r in rows)
    # tên trên Shopee là lời quảng cáo của shop, nên đọc như lời của shop và đặt câu hỏi
    intro = (f"Sản phẩm này được bán với tên: {name}. Trong {len(rows)} đánh giá mình đã xem, "
             f"có {five} đánh giá năm sao. Người mua nói gì, cùng nghe nhé.")
    lines, read = [], set()
    for item in items:
        line = ""
        if item.source != "upload" and (item.stem, item.review_id) not in read:
            read.add((item.stem, item.review_id))
            row = _granted_row(reviews_dir, item.stem, item.review_id)
            content = _shorten(row.get("review_content") or "")
            if content:
                stars = f"chấm {row['rating']} sao " if row.get("rating") else ""
                line = f"Một người mua {stars}nói: {content}"
        lines.append(line)
    lines[0] = f"{intro} {lines[0]}".strip()
    if not any(i.source == "upload" for i in items):
        lines[-1] = f"{lines[-1]} Mình sẽ mua về test xem có đúng như vậy không.".strip()
    return lines


def library(reviews_dir: Path, uploads_dir: Path, url_of: Callable[[str], str | None]) -> dict:
    reviews, waiting = [], {}
    for p in sr.list_products(reviews_dir):
        images = sr.review_images(reviews_dir, p["stem"])
        for row in sr.picked_or_marked(reviews_dir, p["stem"]):
            urls = (row.get("video_url") or "").split()
            pics = images.get(row["review_id"], [])
            if row["mark"].get("permission") != "granted":
                if row["mark"].get("picked") and (urls or pics):
                    waiting[p["stem"]] = waiting.get(p["stem"], 0) + 1
                continue
            if urls or pics:
                reviews.append({
                    "stem": p["stem"], "product": p["name"], "review_id": row["review_id"], "username": row["username"],
                    "rating": row.get("rating"), "text": row["review_content"], "picked": row["mark"].get("picked", False),
                    "videos": len(urls), "images": pics,
                })
    uploads = []
    for f in sorted(uploads_dir.iterdir(), key=lambda x: -x.stat().st_mtime) if uploads_dir.is_dir() else []:
        if UPLOAD_ID.fullmatch(f.name):
            is_video = f.suffix in VIDEO_EXTS
            uploads.append({"id": f.name, "kind": "video" if is_video else "image", "url": url_of(str(f)),
                            "duration": media.probe(f)["duration"] if is_video else None})
    return {"reviews": reviews, "uploads": uploads, "waiting": waiting}


def _resolve(item: Item, reviews_dir: Path, uploads_dir: Path) -> tuple[Path, bool]:
    if item.source == "upload":
        path = uploads_dir / item.upload_id
        if not UPLOAD_ID.fullmatch(item.upload_id) or not path.is_file():
            raise NotAllowed("Không còn file đã tải lên")
        return path, path.suffix in IMAGE_EXTS
    kind = "video" if item.source == "review_video" else "image"
    return fetch_review_media(reviews_dir, item.stem, item.review_id, kind, item.index), kind == "image"


def _fit_filter(fit: str, hold: float = 0) -> str:
    cover = f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H}"
    if fit == "crop":
        chain = f"[0:v]{cover}"
    else:  # giữ nguyên khung hình, phần trống là chính hình đó làm mờ
        chain = (f"[0:v]split[a][b];[a]{cover},gblur=sigma=30[bg];"
                 f"[b]scale={W}:{H}:force_original_aspect_ratio=decrease[fg];[bg][fg]overlay=(W-w)/2:(H-h)/2")
    # giọng đọc dài hơn clip thì giữ khung hình cuối cho đọc hết
    extend = f",tpad=stop_mode=clone:stop_duration={hold:.3f}" if hold > 0 else ""
    return f"{chain},setsar=1,fps={FPS},format=yuv420p{extend}[v]"


def _encode_segment(item: Item, src: Path, is_image: bool, fit: str, dest: Path,
                    voice: Path | None = None, speed: float = 1.0) -> float:
    spoken = tts.duration_of(voice) / speed + VOICE_TAIL if voice else 0.0
    hold = 0.0
    if is_image:
        length, has_audio = max(item.duration, spoken), False
        source = ["-loop", "1", "-framerate", str(FPS), "-t", f"{length:.3f}", "-i", str(src)]
    else:
        info = media.probe(src)
        start = min(item.start, max(0.0, info["duration"] - MIN_LENGTH))
        end = min(item.end if item.end is not None else info["duration"], info["duration"])
        clip = end - start
        if clip < MIN_LENGTH:
            raise ValueError(f"Đoạn cắt quá ngắn ({src.name})")
        length = max(clip, spoken)
        hold = length - clip
        has_audio = info["has_audio"] and item.keep_audio
        source = ["-ss", f"{start:.3f}", "-t", f"{clip:.3f}", "-i", str(src)]
    graph = _fit_filter(fit, hold)
    if voice:
        said = f"[2:a]atempo={speed:.3f},aresample=44100,aformat=channel_layouts=stereo"
        if has_audio:
            graph += (f";[0:a:0]apad,volume={CLIP_UNDER_VOICE}[clip];{said}[said];"
                      "[clip][said]amix=inputs=2:duration=first:normalize=0[a]")
        else:
            graph += f";{said},apad[a]"
        audio = ["-map", "[a]"]
    else:
        audio = ["-map", "0:a:0", "-af", "apad"] if has_audio else ["-map", "1:a"]
    media.run(["ffmpeg", "-y", "-v", "error", *source, "-f", "lavfi", "-t", f"{length:.3f}",
               "-i", "anullsrc=r=44100:cl=stereo", *(["-i", str(voice)] if voice else []),
               "-filter_complex", graph, "-map", "[v]", *audio,
               "-t", f"{length:.3f}", "-c:v", "libx264", "-preset", "veryfast", "-crf", "16",
               "-c:a", "aac", "-ar", "44100", "-ac", "2", str(dest)])
    return length


def render(store: Store, p: Project, reviews_dir: Path, uploads_dir: Path,
           progress: Callable[[float, str], None] = lambda f, m: None) -> dict:
    work = WORK_DIR / uuid.uuid4().hex[:10]
    work.mkdir(parents=True, exist_ok=True)
    try:
        parts, total = [], 0.0
        for i, item in enumerate(p.items):
            progress(0.7 * i / len(p.items), f"Xử lý mục {i + 1}/{len(p.items)}")
            src, is_image = _resolve(item, reviews_dir, uploads_dir)
            part = work / f"part_{i:03d}.mp4"
            text = tts.normalize_for_speech(item.voice_text)
            if text:
                progress(0.7 * i / len(p.items), f"Tạo giọng đọc mục {i + 1}/{len(p.items)}")
            voice = tts.synthesize(text, p.voice) if text else None
            total += _encode_segment(item, src, is_image, p.fit, part, voice, p.speed)
            parts.append(part)
        (work / "list.txt").write_text("".join(f"file '{x.name}'\n" for x in parts))
        watermark = work / "watermark.png"
        cards.render_watermark(WATERMARK_TEXT).save(watermark)
        vx, vy = WATERMARK_SPEED
        out = work / "out.mp4"
        _run_ffmpeg(
            ["ffmpeg", "-y", "-v", "error", "-progress", "pipe:1", "-nostats", "-f", "concat", "-safe", "0",
             "-i", str(work / "list.txt"), "-i", str(watermark), "-filter_complex",
             f"[0:v][1:v]overlay='{bounce_expr(vx, 'main_w-overlay_w')}':'{bounce_expr(vy, 'main_h-overlay_h')}'[v]",
             "-map", "[v]", "-map", "0:a", "-c:v", "libx264", "-preset", "medium", "-crf", "20", "-pix_fmt", "yuv420p",
             "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", str(out)],
            total, work, lambda f: progress(0.7 + 0.28 * f, "Ghép video"),
        )
        thumb = out.with_suffix(".jpg")
        media.run(["ffmpeg", "-y", "-v", "error", "-ss", f"{min(0.5, total / 2):.2f}", "-i", str(out), "-frames:v", "1",
                   "-vf", "scale=360:-2", str(thumb)])
        code = "rv" + uuid.uuid4().hex[:12]
        title = p.title.strip() or "Video review"
        out, thumb = store.place_video(out, thumb, store.video_dest(None, title, code))
        video_id = store.add_video(code, title, out, thumb, round(total, 2), p.model_dump())
        return {"id": video_id, "duration": round(total, 2)}
    finally:
        shutil.rmtree(work, ignore_errors=True)


def make_router(store: Store, jobs: JobManager, data_dir: Path, is_stale: Callable[[], bool],
                url_of: Callable[[str], str | None]) -> APIRouter:
    router = APIRouter(prefix="/api/review-studio")
    reviews_dir = data_dir / "shopee_reviews"
    uploads_dir = data_dir / "review_studio" / "uploads"

    @router.get("/library")
    def get_library():
        return library(reviews_dir, uploads_dir, url_of)

    @router.post("/fetch")
    def fetch(req: FetchRequest):
        try:
            path = fetch_review_media(reviews_dir, req.stem, req.review_id, req.kind, req.index)
        except NotAllowed as e:
            raise HTTPException(403, str(e))
        except (ValueError, OSError, sr.TransientError, sr.httpx.HTTPError) as e:
            raise HTTPException(502, f"Không tải được file: {e}")
        return {"url": url_of(str(path)), "duration": media.probe(path)["duration"] if req.kind == "video" else None}

    @router.post("/script")
    def script(req: ScriptRequest):
        try:
            return {"lines": draft_script(reviews_dir, req.items)}
        except NotAllowed as e:
            raise HTTPException(403, str(e))

    @router.post("/upload")
    async def upload(request: Request, name: str):
        suffix = Path(name).suffix.lower()
        if suffix not in VIDEO_EXTS | IMAGE_EXTS:
            raise HTTPException(400, "Chỉ nhận video .mp4, .mov, .webm, .mkv hoặc ảnh .jpg, .png, .webp")
        uploads_dir.mkdir(parents=True, exist_ok=True)
        dest = uploads_dir / f"{uuid.uuid4().hex[:16]}{suffix}"
        size = 0
        try:
            with dest.open("wb") as f:
                async for chunk in request.stream():
                    size += len(chunk)
                    if size > MAX_UPLOAD_BYTES:
                        raise HTTPException(413, "File quá lớn (tối đa 1GB)")
                    f.write(chunk)
            if suffix in IMAGE_EXTS:
                with Image.open(dest) as img:
                    img.verify()
            else:
                media.probe(dest)
        except HTTPException:
            dest.unlink(missing_ok=True)
            raise
        except (RuntimeError, UnidentifiedImageError, OSError):
            dest.unlink(missing_ok=True)
            raise HTTPException(400, "Không đọc được file")
        return {"id": dest.name}

    @router.delete("/uploads/{upload_id}")
    def delete_upload(upload_id: str):
        if not UPLOAD_ID.fullmatch(upload_id):
            raise HTTPException(404)
        (uploads_dir / upload_id).unlink(missing_ok=True)
        return {"ok": True}

    @router.post("/render")
    def start_render(project: Project):
        if is_stale():
            raise HTTPException(409, "Code đã được cập nhật nhưng server chưa khởi động lại. Hãy tắt rồi mở lại app, sau đó bấm Xuất video.")
        if any(i.voice_text.strip() for i in project.items) and project.voice not in {v["id"] for v in tts.list_voices()}:
            raise HTTPException(400, "Giọng đọc không tồn tại")
        try:  # báo lỗi ngay thay vì chờ tới lúc chạy nền
            for item in project.items:
                _resolve(item, reviews_dir, uploads_dir)
        except NotAllowed as e:
            raise HTTPException(403, str(e))
        except (ValueError, OSError, sr.TransientError, sr.httpx.HTTPError) as e:
            raise HTTPException(502, f"Không tải được file: {e}")

        def run(progress):
            job_store = Store(data_dir)
            try:
                return render(job_store, project, reviews_dir, uploads_dir, progress)
            finally:
                job_store.close()

        return jobs.submit("render", f"Video review: {project.title.strip() or f'{len(project.items)} mục'}", run,
                           [f"🎞 {len(project.items)} mục, kiểu {'nền mờ' if project.fit == 'blur' else 'cắt đầy khung'}"])

    return router

