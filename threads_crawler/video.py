"""Build a TikTok video (1080x1920) from a timeline project: script -> TTS -> cards -> ffmpeg.

Mốc thời gian (phải khớp với `computeTimeline` trong static/app.js):
- Các thẻ (segment) nối tiếp nhau trên hàng giọng đọc: bắt đầu sau LEAD_IN, mỗi thẻ dài
  = thời lượng giọng đọc / tốc độ + `hold`, giữa hai thẻ nghỉ `gap` giây.
- Meme và chữ gắn (anchor) vào một thẻ: start = start của thẻ đó + offset. Đổi thứ tự thẻ thì
  meme/chữ đi theo.
- Nhạc tính theo giây tuyệt đối trên video.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import uuid
from datetime import datetime
from pathlib import Path
from typing import Callable, Literal

import numpy as np
import soundfile as sf
from PIL import Image
from pydantic import BaseModel, Field, field_validator

from . import PROJECT_ROOT, cards, media, tts
from .storage import Store

log = logging.getLogger(__name__)

W, H = 1080, 1920
FPS = 30
TEXT_TOP = 150  # chữ ở vị trí "trên"
CARD_TOP = 300  # thẻ được canh giữa trong dải [CARD_TOP, H - SAFE_BOTTOM]
SAFE_BOTTOM = 300  # vùng caption/nút của TikTok
LEAD_IN = 0.35  # giây im lặng trước thẻ đầu
TAIL = 0.9  # giây giữ hình sau thẻ cuối
SILENT_SEGMENT = 2.5  # thẻ không có lời đọc vẫn hiện bấy nhiêu giây
WORK_DIR = PROJECT_ROOT / ".cache" / "render"

Position = Literal[
    "top-left", "top-center", "top-right",
    "middle-left", "middle-center", "middle-right",
    "bottom-left", "bottom-center", "bottom-right",
]


def new_id() -> str:
    return uuid.uuid4().hex[:8]


class Segment(BaseModel):
    id: str = Field(default_factory=new_id)
    post_code: str = ""
    kind: Literal["post", "reply"] = "post"
    code: str = ""
    username: str
    avatar_path: str | None = None
    verified: bool = False
    reply_to: str | None = None
    display_text: str
    read_text: str = ""
    published_at: str | None = None
    likes: int | None = None  # None = ẩn thanh tương tác (các trang trước của một bài dài)
    replies: int | None = None
    reposts: int | None = None
    image_path: str | None = None
    page_label: str | None = None
    hold: float = Field(0.0, ge=0, le=60)  # giữ thẻ thêm sau khi đọc xong


class Anchor(BaseModel):
    seg: str | None = None  # id thẻ; None = tính từ đầu video
    offset: float = 0.0


class MemeItem(BaseModel):
    id: str = Field(default_factory=new_id)
    path: str  # tương đối trong materials/meme_video
    anchor: Anchor = Anchor()
    duration: float | None = Field(None, gt=0)  # None = cả meme
    position: Position = "bottom-right"
    size: float = Field(0.4, ge=0.1, le=1.0)  # tỉ lệ so với chiều ngang khung hình
    volume: float = Field(1.0, ge=0, le=2)
    chroma: bool | None = None  # None = tự phát hiện phông xanh


class TextItem(BaseModel):
    id: str = Field(default_factory=new_id)
    text: str
    anchor: Anchor = Anchor()
    duration: float | None = Field(None, gt=0)  # None = đến hết video
    position: Literal["top", "middle", "bottom"] = "top"


class MusicItem(BaseModel):
    id: str = Field(default_factory=new_id)
    path: str  # tương đối trong materials/background_musics
    start: float = Field(0.0, ge=0)
    end: float | None = None  # None = đến hết video
    volume: float = Field(0.15, ge=0, le=1)


class Background(BaseModel):
    path: str  # tương đối trong materials/background_videos
    start: float = Field(0.0, ge=0)  # bắt đầu lấy từ giây này của video nền


class Project(BaseModel):
    name: str = ""
    post_codes: list[str] = []
    segments: list[Segment]
    texts: list[TextItem] = []
    memes: list[MemeItem] = []
    music: list[MusicItem] = []
    background: Background
    voice: str = tts.DEFAULT_VOICE
    precision: Literal["fp32", "int8"] = "fp32"
    speed: float = Field(1.1, ge=0.7, le=1.8)
    gap: float = Field(0.35, ge=0, le=5)
    theme: Literal["light", "dark"] = "dark"
    publish_at: str | None = None  # giờ dự định đăng, "YYYY-MM-DDTHH:MM" -> quyết định thư mục chứa video

    @field_validator("publish_at")
    @classmethod
    def _check_publish_at(cls, v: str | None) -> str | None:
        if not v:
            return None
        return datetime.fromisoformat(v).strftime("%Y-%m-%dT%H:%M")


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
        width = cards.POST_TEXT_W if kind == "post" else cards.COMMENT_TEXT_W
        pages = cards.paginate(p["text"], max_lines - (5 if image else 0), width) if p["text"].strip() else [""]
        last = len(pages) - 1
        for i, page in enumerate(pages):
            segments.append(Segment(
                post_code=post["code"],
                kind=kind,
                code=p["code"],
                username=p["username"],
                avatar_path=p.get("avatar_path"),
                verified=p["user_verified"],
                reply_to=None if (kind == "post" or is_continuation) else p.get("reply_to_username"),
                display_text=page,
                read_text=tts.normalize_for_speech(page),
                published_at=p.get("published_at"),
                likes=p["like_count"] if i == last else None,  # thanh tương tác ở trang cuối, như Threads
                replies=p["reply_count"] if i == last else None,
                reposts=p["repost_count"] if i == last else None,
                image_path=image if i == 0 else None,
                page_label=f"{i + 1}/{len(pages)}" if len(pages) > 1 else None,
            ))

    add(post, "post")
    for code in reply_codes:
        if code in by_code:
            add(by_code[code], "reply")
    return segments


# ── Bố cục & mốc thời gian ───────────────────────────────────────────────────


def card_image(seg: Segment, theme: str) -> Image.Image:
    return cards.render_card(
        seg.username, seg.display_text, style="post" if seg.kind == "post" else "comment",
        avatar_path=seg.avatar_path, verified=seg.verified, published_at=seg.published_at,
        likes=seg.likes, replies=seg.replies, reposts=seg.reposts, image_path=seg.image_path,
        page_label=seg.page_label, theme=theme,
    )


def card_xy(card: Image.Image) -> tuple[int, int]:
    band = H - SAFE_BOTTOM - CARD_TOP
    return (W - card.width) // 2, CARD_TOP + max(0, (band - card.height) // 2)


def text_xy(img: Image.Image, position: str) -> tuple[int, int]:
    y = {"top": TEXT_TOP, "middle": (H - img.height) // 2, "bottom": H - SAFE_BOTTOM - img.height}[position]
    return (W - img.width) // 2, y


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


def meme_key(m: MemeItem, src: Path) -> str | None:
    if m.chroma is False:
        return None
    key = media.detect_key_color(src)
    return key or ("0x00ff00" if m.chroma else None)


class Timeline:
    """Absolute start/end of every segment and item, given real voice durations (giây, tốc độ 1.0)."""

    def __init__(self, project: Project, voice_durations: dict[str, float]) -> None:
        self.seg: dict[str, tuple[float, float]] = {}
        t = LEAD_IN
        for s in project.segments:
            voice = voice_durations.get(s.id)
            length = (voice / project.speed if voice else SILENT_SEGMENT) + s.hold
            self.seg[s.id] = (t, t + length)
            t += length + project.gap
        self.segments_end = t - project.gap if project.segments else LEAD_IN

        ends = [self.segments_end + TAIL]
        self.memes: dict[str, tuple[float, float]] = {}
        for m in project.memes:
            src = media.safe_material(m.path, "memes")
            full = media.probe(src)["duration"]
            st = self.at(m.anchor)
            self.memes[m.id] = (st, st + min(m.duration or full, full))
            ends.append(self.memes[m.id][1])
        for x in project.texts:
            if x.duration:
                ends.append(self.at(x.anchor) + x.duration)
        self.total = max(ends)
        self.texts = {
            x.id: (self.at(x.anchor), min(self.total, self.at(x.anchor) + x.duration) if x.duration else self.total)
            for x in project.texts
        }

    def at(self, anchor: Anchor) -> float:
        base = self.seg[anchor.seg][0] if anchor.seg in self.seg else 0.0
        return max(0.0, base + anchor.offset)


# ── Render ───────────────────────────────────────────────────────────────────


def render(
    store: Store, project: Project, on_progress: Callable[[float, str], None] = lambda f, m: None
) -> dict:
    if not project.segments:
        raise ValueError("Timeline chưa có thẻ nào")
    work = WORK_DIR / uuid.uuid4().hex[:10]
    work.mkdir(parents=True, exist_ok=True)
    try:
        return _render(store, project, work, on_progress)
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _render(store: Store, p: Project, work: Path, on_progress) -> dict:
    # 1. Giọng đọc (đã cache nếu giao diện tạo trước)
    clips: dict[str, np.ndarray] = {}
    sr = 48_000
    for i, seg in enumerate(p.segments):
        on_progress(0.5 * i / len(p.segments), f"Giọng đọc thẻ {i + 1}/{len(p.segments)}")
        if seg.read_text.strip():
            audio, sr = sf.read(tts.synthesize(seg.read_text.strip(), p.voice, p.precision), dtype="float32")
            clips[seg.id] = audio if audio.ndim == 1 else audio.mean(axis=1)
    tl = Timeline(p, {k: len(v) / sr for k, v in clips.items()})
    total = tl.total

    # 2. Track giọng đọc: đặt từng clip ở vị trí (thời gian video × tốc độ) rồi atempo về đúng tốc độ
    voice = np.zeros(int((total * p.speed + 1) * sr), np.float32)
    for seg_id, clip in clips.items():
        pos = int(tl.seg[seg_id][0] * p.speed * sr)
        voice[pos:pos + len(clip)] += clip[: len(voice) - pos]
    voice_path = work / "voice.wav"
    sf.write(voice_path, voice, sr)

    # 3. Hình: thẻ + chữ
    on_progress(0.52, "Vẽ thẻ và chữ")
    starts = [tl.seg[s.id][0] for s in p.segments]
    shows = list(zip(starts, starts[1:] + [total]))  # thẻ hiện đến khi thẻ sau bắt đầu
    card_pngs = []
    for i, seg in enumerate(p.segments):
        c = card_image(seg, p.theme)
        path = work / f"card_{i:03d}.png"
        c.save(path)
        card_pngs.append((path, card_xy(c)))
    text_pngs = []
    for k, x in enumerate(p.texts):
        if not x.text.strip():
            continue
        img = cards.render_title(x.text)
        path = work / f"text_{k:03d}.png"
        img.save(path)
        text_pngs.append((path, text_xy(img, x.position), tl.texts[x.id]))

    # 4. Lệnh ffmpeg
    bg = media.safe_material(p.background.path, "backgrounds")
    bg_dur = media.probe(bg)["duration"]
    inputs: list[list[str]] = []

    def add_input(*args: str) -> int:
        inputs.append(list(args))
        return len(inputs) - 1

    if bg_dur > total + 0.5:
        bg_start = min(p.background.start, bg_dur - total - 0.5)
        i_bg = add_input("-ss", f"{bg_start:.2f}", "-t", f"{total + 0.5:.2f}", "-i", str(bg))
    else:
        bg_start = 0.0
        i_bg = add_input("-stream_loop", "-1", "-t", f"{total + 0.5:.2f}", "-i", str(bg))
    i_voice = add_input("-i", str(voice_path))

    filters = [f"[{i_bg}:v]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},setsar=1,fps={FPS}[v0]"]
    cur, n = "v0", 0

    def overlay(src: str, x: int, y: int, extra: str = "") -> None:
        nonlocal cur, n
        n += 1
        filters.append(f"[{cur}][{src}]overlay={x}:{y}{extra}[v{n}]")
        cur = f"v{n}"

    for (path, (x, y)), (a, b) in zip(card_pngs, shows):
        overlay(f"{add_input('-i', str(path))}:v", x, y, f":enable='between(t,{a:.3f},{b:.3f})'")

    audio = [f"[{i_voice}:a]atempo={p.speed:.3f}[voice]"]
    labels = ["voice"]
    for k, m in enumerate(p.memes):
        st, en = tl.memes[m.id]
        if st >= total:
            continue
        src = media.safe_material(m.path, "memes")
        w, h = meme_size(src, m.size)
        x, y = meme_xy(m.position, w, h)
        i_m = add_input("-i", str(src))
        chain = f"[{i_m}:v]trim=0:{en - st:.3f},setpts=PTS-STARTPTS,"
        if key := meme_key(m, src):
            kg, kb = int(key[4:6], 16), int(key[6:8], 16)
            chain += f"chromakey={key}:0.16:0.08,despill=type={'green' if kg >= kb else 'blue'},"
        chain += f"scale={w}:{h},setpts=PTS+{st:.3f}/TB[m{k}]"
        filters.append(chain)
        overlay(f"m{k}", x, y, ":eof_action=pass")
        if media.probe(src)["has_audio"] and m.volume > 0:
            audio.append(
                f"[{i_m}:a]atrim=0:{en - st:.3f},asetpts=PTS-STARTPTS,volume={m.volume:.2f},"
                f"adelay={int(st * 1000)}:all=1[ma{k}]"
            )
            labels.append(f"ma{k}")

    for path, (x, y), (a, b) in text_pngs:  # chữ nằm trên cùng
        overlay(f"{add_input('-i', str(path))}:v", x, y, f":enable='between(t,{a:.3f},{b:.3f})'")

    for k, mu in enumerate(p.music):
        st = min(mu.start, total)
        en = min(mu.end if mu.end is not None else total, total)
        if en - st < 0.3:
            continue
        i_mu = add_input("-stream_loop", "-1", "-i", str(media.safe_material(mu.path, "music")))
        d = en - st
        audio.append(
            f"[{i_mu}:a]atrim=0:{d:.3f},asetpts=PTS-STARTPTS,volume={mu.volume:.3f},"
            f"afade=t=in:d={min(0.8, d / 4):.2f},afade=t=out:st={max(0, d - 1.5):.2f}:d={min(1.5, d / 3):.2f},"
            f"adelay={int(st * 1000)}:all=1[mu{k}]"
        )
        labels.append(f"mu{k}")

    filters += audio
    filters.append(
        "".join(f"[{a}]" for a in labels)
        + f"amix=inputs={len(labels)}:duration=longest:normalize=0,alimiter=limit=0.95[aout]"
    )

    first_code = p.post_codes[0] if p.post_codes else (p.segments[0].post_code or "video")
    out = work / "out.mp4"  # dựng xong mới chuyển vào thư mục lịch đăng, tránh file dở dang ở đó
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
    _run_ffmpeg(cmd, total, work, lambda f: on_progress(0.55 + 0.43 * f, f"Dựng video {int(f * 100)}%"))

    thumb = out.with_suffix(".jpg")
    media.run(["ffmpeg", "-y", "-v", "error", "-ss", "1", "-i", str(out), "-frames:v", "1",
               "-vf", "scale=360:-2", str(thumb)])
    config = p.model_dump(include={"voice", "precision", "speed", "theme", "post_codes"}) | {
        "background_file": p.background.path,
        "background_start": round(bg_start, 2),
        "music_file": p.music[0].path if p.music else None,
        "segments": len(p.segments),
        "memes": len(p.memes),
    }
    title = p.name.strip() or next((x.text for x in p.texts if x.text.strip()), "") or p.segments[0].display_text[:80]
    out, thumb = store.place_video(out, thumb, store.video_dest(p.publish_at, title, first_code))
    video_id = store.add_video(first_code, title, out, thumb, round(total, 2), config, p.publish_at)
    store.archive_used(p.post_codes or [first_code], video_id)
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
