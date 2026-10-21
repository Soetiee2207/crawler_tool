"""Build a TikTok video (1080x1920) from a Threads post: script -> TTS -> cards -> ffmpeg."""

from __future__ import annotations

import logging
import random
import shutil
import subprocess
import uuid
from datetime import datetime
from pathlib import Path
from typing import Callable, Literal

import numpy as np
import soundfile as sf
from PIL import Image
from pydantic import BaseModel, Field

from . import PROJECT_ROOT, cards, media, tts
from .storage import Store

log = logging.getLogger(__name__)

W, H = 1080, 1920
FPS = 30
TITLE_Y = 150
SAFE_BOTTOM = 300  # vùng caption/nút của TikTok
LEAD_IN = 0.35  # giây im lặng trước câu đầu
GAP = 0.35  # giây nghỉ giữa các đoạn
TAIL = 0.9  # giây giữ hình sau câu cuối
SILENT_SEGMENT = 2.5  # đoạn không có lời đọc vẫn hiện bấy nhiêu giây
WORK_DIR = PROJECT_ROOT / ".cache" / "render"

Position = Literal[
    "top-left", "top-center", "top-right",
    "middle-left", "middle-center", "middle-right",
    "bottom-left", "bottom-center", "bottom-right",
]


class Segment(BaseModel):
    kind: Literal["post", "reply"] = "post"
    code: str = ""
    username: str
    avatar_path: str | None = None
    verified: bool = False
    reply_to: str | None = None
    display_text: str
    read_text: str = ""
    likes: int | None = None
    replies: int | None = None
    image_path: str | None = None
    page_label: str | None = None


class MemeSpec(BaseModel):
    path: str  # tương đối trong materials/meme_video
    segment: int = 0  # xuất hiện khi đoạn số `segment` bắt đầu...
    offset: float = 0.0  # ...cộng thêm bấy nhiêu giây
    position: Position = "bottom-right"
    size: float = Field(0.4, ge=0.1, le=1.0)  # tỉ lệ so với chiều ngang khung hình
    volume: float = Field(1.0, ge=0, le=2)
    chroma: bool | None = None  # None = tự phát hiện phông xanh


class RenderSpec(BaseModel):
    post_code: str
    segments: list[Segment]
    voice: str = tts.DEFAULT_VOICE
    precision: Literal["fp32", "int8"] = "fp32"
    speed: float = Field(1.1, ge=0.7, le=1.8)
    voice_volume: float = Field(1.0, ge=0, le=2)
    theme: Literal["light", "dark"] = "light"
    title: str = ""
    background: str = "random"  # đường dẫn trong materials/background_videos hoặc "random"
    music: str = ""  # đường dẫn, "random:<thư mục>" hoặc "" (không nhạc)
    music_volume: float = Field(0.15, ge=0, le=1)
    memes: list[MemeSpec] = []


# ── Kịch bản ─────────────────────────────────────────────────────────────────


def first_image(post: dict) -> str | None:
    return next((m["path"] for m in post["media"] if m["type"] == "image" and m.get("path")), None)


def build_script(post: dict, reply_codes: list[str], max_lines: int = 11, with_image: bool = True) -> list[Segment]:
    """Turn a post + chosen replies (theo thứ tự chọn) into card segments with default read text."""
    by_code = {r["code"]: r for r in post["replies"]}
    segments: list[Segment] = []

    def add(p: dict, kind: str) -> None:
        is_continuation = kind == "reply" and p["username"] == post["username"]
        image = first_image(p) if with_image else None
        pages = cards.paginate(p["text"], max_lines - (5 if image else 0)) if p["text"].strip() else [""]
        for i, page in enumerate(pages):
            segments.append(Segment(
                kind=kind,
                code=p["code"],
                username=p["username"],
                avatar_path=p.get("avatar_path"),
                verified=p["user_verified"],
                reply_to=None if (kind == "post" or is_continuation) else p.get("reply_to_username"),
                display_text=page,
                read_text=tts.normalize_for_speech(page),
                likes=p["like_count"] if kind == "post" and i == 0 else None,
                replies=p["reply_count"] if kind == "post" and i == 0 else None,
                image_path=image if i == 0 else None,
                page_label=f"{i + 1}/{len(pages)}" if len(pages) > 1 else None,
            ))

    add(post, "post")
    for code in reply_codes:
        if code in by_code:
            add(by_code[code], "reply")
    return segments


# ── Bố cục (dùng chung cho xem trước và render) ──────────────────────────────


def card_image(seg: Segment, theme: str) -> Image.Image:
    return cards.render_card(
        seg.username, seg.display_text, avatar_path=seg.avatar_path, verified=seg.verified,
        reply_to=seg.reply_to, likes=seg.likes, replies=seg.replies, image_path=seg.image_path,
        page_label=seg.page_label, theme=theme,
    )


def card_xy(card: Image.Image, title_h: int) -> tuple[int, int]:
    top = TITLE_Y + title_h + 30 if title_h else 200
    bottom = H - SAFE_BOTTOM
    y = top + max(0, (bottom - top - card.height) // 2)
    return (W - card.width) // 2, y


def meme_size(src: Path, frac: float) -> tuple[int, int]:
    info = media.probe(src)
    w = int(W * frac) // 2 * 2
    h = int(w * (info["height"] or w) / (info["width"] or w)) // 2 * 2
    return w, h


def meme_xy(position: str, w: int, h: int) -> tuple[int, int]:
    row, col = position.split("-")
    x = {"left": 40, "center": (W - w) // 2, "right": W - w - 40}[col]
    y = {"top": 200, "middle": (H - h) // 2, "bottom": H - h - SAFE_BOTTOM + 40}[row]
    return x, y


def resolve_background(spec: RenderSpec) -> Path:
    if spec.background and spec.background != "random":
        return media.safe_material(spec.background, "backgrounds")
    items = media.list_materials()["backgrounds"]
    if not items:
        raise RuntimeError("Chưa có video nền trong materials/background_videos")
    return media.safe_material(random.choice(items)["path"], "backgrounds")


def resolve_music(spec: RenderSpec) -> Path | None:
    if not spec.music:
        return None
    if spec.music.startswith("random:"):
        cat = spec.music.split(":", 1)[1]
        items = [m for m in media.list_materials()["music"] if not cat or m["category"] == cat]
        return media.safe_material(random.choice(items)["path"], "music") if items else None
    return media.safe_material(spec.music, "music")


def meme_key(m: MemeSpec, src: Path) -> str | None:
    if m.chroma is False:
        return None
    key = media.detect_key_color(src)
    return key or ("0x00ff00" if m.chroma else None)


def preview(spec: RenderSpec, segment: int = 0) -> Image.Image:
    """Single-frame mockup of the layout (nền + tiêu đề + thẻ + meme) — nhanh, không cần TTS."""
    frame = media.background_frame(resolve_background(spec))
    title_h = 0
    if spec.title.strip():
        t = cards.render_title(spec.title)
        frame.alpha_composite(t, ((W - t.width) // 2, TITLE_Y))
        title_h = t.height
    if spec.segments:
        seg = spec.segments[min(segment, len(spec.segments) - 1)]
        c = card_image(seg, spec.theme)
        frame.alpha_composite(c, card_xy(c, title_h))
    for m in spec.memes:
        src = media.safe_material(m.path, "memes")
        w, h = meme_size(src, m.size)
        tmp = WORK_DIR / "preview" / f"{src.stem}.png"
        media.extract_frame(src, media.probe(src)["duration"] * 0.4, tmp)
        img = Image.open(tmp)
        key = meme_key(m, src)
        img = (media.key_out(img, key) if key else img.convert("RGBA")).resize((w, h), Image.LANCZOS)
        frame.alpha_composite(img, meme_xy(m.position, w, h))
    return frame.convert("RGB")


# ── Render ───────────────────────────────────────────────────────────────────


def render(
    store: Store, spec: RenderSpec, on_progress: Callable[[float, str], None] = lambda f, m: None
) -> dict:
    if not spec.segments:
        raise ValueError("Kịch bản trống")
    work = WORK_DIR / uuid.uuid4().hex[:10]
    work.mkdir(parents=True, exist_ok=True)
    try:
        return _render(store, spec, work, on_progress)
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _render(store: Store, spec: RenderSpec, work: Path, on_progress) -> dict:
    # 1. Giọng đọc cho từng đoạn
    clips: list[np.ndarray | None] = []
    sr = None
    for i, seg in enumerate(spec.segments):
        on_progress(0.6 * i / len(spec.segments), f"Tạo giọng đọc đoạn {i + 1}/{len(spec.segments)}")
        text = seg.read_text.strip()
        if not text:
            clips.append(None)
            continue
        audio, rate = sf.read(tts.synthesize(text, spec.voice, spec.precision), dtype="float32")
        if sr and rate != sr:
            raise RuntimeError("Các đoạn giọng đọc khác sample rate")
        sr = rate
        clips.append(audio if audio.ndim == 1 else audio.mean(axis=1))
    sr = sr or 48_000

    # 2. Ghép thành một track, ghi lại mốc thời gian mỗi đoạn (tính theo tốc độ đọc)
    parts = [np.zeros(int(LEAD_IN * spec.speed * sr), np.float32)]
    starts: list[float] = []
    pos = len(parts[0])
    for clip in clips:
        starts.append(pos / sr / spec.speed)
        clip = clip if clip is not None else np.zeros(int(SILENT_SEGMENT * spec.speed * sr), np.float32)
        gap = np.zeros(int(GAP * spec.speed * sr), np.float32)
        parts += [clip, gap]
        pos += len(clip) + len(gap)
    voice_path = work / "voice.wav"
    sf.write(voice_path, np.concatenate(parts), sr)
    total = pos / sr / spec.speed + TAIL
    ends = starts[1:] + [total]

    # 3. Hình: tiêu đề + thẻ
    on_progress(0.62, "Vẽ thẻ bài viết")
    title_h = 0
    title_png = None
    if spec.title.strip():
        t = cards.render_title(spec.title)
        title_png, title_h = work / "title.png", t.height
        t.save(title_png)
    card_pngs = []
    for i, seg in enumerate(spec.segments):
        c = card_image(seg, spec.theme)
        path = work / f"card_{i:03d}.png"
        c.save(path)
        card_pngs.append((path, card_xy(c, title_h)))

    # 4. Lệnh ffmpeg
    bg = resolve_background(spec)
    bg_dur = media.probe(bg)["duration"]
    music = resolve_music(spec)

    inputs: list[list[str]] = []

    def add_input(*args: str) -> int:
        inputs.append(list(args))
        return len(inputs) - 1

    if bg_dur > total + 1:
        bg_start = random.uniform(0, bg_dur - total - 0.5)  # chỉ lấy một đoạn ngẫu nhiên vừa đủ dài
        i_bg = add_input("-ss", f"{bg_start:.2f}", "-t", f"{total + 0.5:.2f}", "-i", str(bg))
    else:
        bg_start = 0.0
        i_bg = add_input("-stream_loop", "-1", "-t", f"{total + 0.5:.2f}", "-i", str(bg))
    i_voice = add_input("-i", str(voice_path))
    i_music = add_input("-stream_loop", "-1", "-i", str(music)) if music else None

    filters = [
        f"[{i_bg}:v]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},setsar=1,fps={FPS}[v0]"
    ]
    cur = "v0"
    n = 0

    def overlay(src_label: str, x: int, y: int, extra: str = "") -> None:
        nonlocal cur, n
        n += 1
        filters.append(f"[{cur}][{src_label}]overlay={x}:{y}{extra}[v{n}]")
        cur = f"v{n}"

    if title_png:
        i_t = add_input("-i", str(title_png))
        overlay(f"{i_t}:v", (W - Image.open(title_png).width) // 2, TITLE_Y)
    for (path, (x, y)), a, b in zip(card_pngs, starts, ends):
        i_c = add_input("-i", str(path))
        overlay(f"{i_c}:v", x, y, f":enable='between(t,{a:.3f},{b:.3f})'")

    audio_labels = []
    filters.append(f"[{i_voice}:a]atempo={spec.speed:.3f},volume={spec.voice_volume:.2f},apad[voice]")
    audio_labels.append("voice")
    if i_music is not None:
        filters.append(
            f"[{i_music}:a]volume={spec.music_volume:.3f},afade=t=in:d=0.8,"
            f"afade=t=out:st={max(0, total - 2):.2f}:d=2[music]"
        )
        audio_labels.append("music")

    for k, m in enumerate(spec.memes):
        src = media.safe_material(m.path, "memes")
        info = media.probe(src)
        w, h = meme_size(src, m.size)
        x, y = meme_xy(m.position, w, h)
        st = starts[min(m.segment, len(starts) - 1)] + m.offset
        if st >= total:
            continue
        i_m = add_input("-i", str(src))
        key = meme_key(m, src)
        chain = f"[{i_m}:v]"
        if key:
            chain += f"chromakey={key}:0.16:0.08,"
            kr, kg, kb = int(key[2:4], 16), int(key[4:6], 16), int(key[6:8], 16)
            chain += f"despill=type={'green' if kg >= kb else 'blue'},"
        chain += f"scale={w}:{h},setpts=PTS-STARTPTS+{st:.3f}/TB[m{k}]"
        filters.append(chain)
        overlay(f"m{k}", x, y, ":eof_action=pass")
        if info["has_audio"] and m.volume > 0:
            ms = int(st * 1000)
            filters.append(f"[{i_m}:a]adelay={ms}:all=1,volume={m.volume:.2f}[ma{k}]")
            audio_labels.append(f"ma{k}")

    filters.append(
        "".join(f"[{a}]" for a in audio_labels)
        + f"amix=inputs={len(audio_labels)}:duration=first:normalize=0,alimiter=limit=0.95[aout]"
    )

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out = store.videos_dir / f"{stamp}_{spec.post_code}.mp4"
    cmd = ["ffmpeg", "-y", "-v", "error", "-nostats", "-progress", "pipe:1"]
    for args in inputs:
        cmd += args
    cmd += [
        "-filter_complex", ";".join(filters), "-map", f"[{cur}]", "-map", "[aout]",
        "-t", f"{total:.3f}", "-r", str(FPS),
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "21", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-movflags", "+faststart", str(out),
    ]
    (work / "ffmpeg_cmd.txt").write_text(" ".join(cmd))
    _run_ffmpeg(cmd, total, work, lambda f: on_progress(0.65 + 0.33 * f, f"Dựng video {int(f * 100)}%"))

    thumb = out.with_suffix(".jpg")
    media.run(["ffmpeg", "-y", "-v", "error", "-ss", "1", "-i", str(out), "-frames:v", "1",
               "-vf", "scale=360:-2", str(thumb)])
    config = spec.model_dump(exclude={"segments"}) | {
        "background_file": str(bg.relative_to(media.MATERIALS_DIR)),
        "background_start": round(bg_start, 2),
        "music_file": str(music.relative_to(media.MATERIALS_DIR)) if music else None,
        "segments": len(spec.segments),
    }
    title = spec.title.strip() or spec.segments[0].display_text[:80]
    video_id = store.add_video(spec.post_code, title, out, thumb, round(total, 2), config)
    store.mark_used([spec.post_code])
    on_progress(1.0, "Xong")
    return {"id": video_id, "path": str(out), "duration": total}


def _run_ffmpeg(cmd: list[str], total: float, work: Path, on_frac: Callable[[float], None]) -> None:
    err_path = work / "ffmpeg_err.txt"
    with open(err_path, "w") as err:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=err, text=True)
        assert proc.stdout
        for line in proc.stdout:
            if line.startswith("out_time_us="):
                try:
                    on_frac(min(1.0, int(line.split("=")[1]) / 1e6 / total))
                except ValueError:
                    pass
        proc.wait()
    if proc.returncode != 0:
        raise RuntimeError("ffmpeg lỗi:\n" + err_path.read_text()[-1500:])
