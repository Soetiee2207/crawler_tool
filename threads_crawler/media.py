"""Helpers around ffmpeg/ffprobe and the `materials/` library (background videos, music, memes, sfx)."""

from __future__ import annotations

import hashlib
import json
import subprocess
from functools import lru_cache
from pathlib import Path

from PIL import Image

from . import PROJECT_ROOT

MATERIALS_DIR = PROJECT_ROOT / "materials"
THUMB_DIR = PROJECT_ROOT / ".cache" / "thumbs"

KINDS = {
    "backgrounds": ("background_videos", {".mp4", ".mov", ".mkv", ".webm"}),
    "music": ("background_musics", {".mp3", ".wav", ".m4a", ".ogg", ".aac"}),
    "memes": ("meme_video", {".mp4", ".mov", ".webm", ".gif"}),
    "sfx": ("sfx", {".mp3", ".wav", ".m4a", ".ogg"}),  # âm thanh ngắn chèn vào tình huống
}


def run(cmd: list[str]) -> str:
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        raise RuntimeError(f"{cmd[0]} lỗi: {res.stderr.strip()[-800:]}")
    return res.stdout


@lru_cache(maxsize=256)
def _probe(path: str, mtime: float) -> dict:
    out = run(["ffprobe", "-v", "error", "-show_entries", "format=duration:stream=codec_type,width,height",
               "-of", "json", path])
    data = json.loads(out)
    streams = data.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), {})
    return {
        "duration": float(data.get("format", {}).get("duration") or 0),
        "width": video.get("width"),
        "height": video.get("height"),
        "has_audio": any(s.get("codec_type") == "audio" for s in streams),
    }


def probe(path: str | Path) -> dict:
    """ffprobe info, cached in memory and on disk (khởi động lại server không phải đo lại)."""
    p = Path(path)
    mtime = p.stat().st_mtime
    cached = _thumb_path(p, "probe", "json")
    if cached.exists():
        try:
            return json.loads(cached.read_text())
        except ValueError:
            pass
    info = _probe(str(p), mtime)
    cached.parent.mkdir(parents=True, exist_ok=True)
    cached.write_text(json.dumps(info))
    return info


def safe_material(rel: str, kind: str) -> Path:
    """Resolve a path relative to materials/<kind dir>, refusing anything outside it."""
    base = (MATERIALS_DIR / KINDS[kind][0]).resolve()
    p = (base / rel).resolve()
    if not p.is_relative_to(base) or not p.is_file():
        raise FileNotFoundError(rel)
    return p


def list_materials() -> dict:
    """{kind: [{path (relative), name, category, duration}]}; category = tên thư mục con."""
    result: dict[str, list[dict]] = {}
    for kind, (folder, exts) in KINDS.items():
        base = MATERIALS_DIR / folder
        items = []
        for f in sorted(base.rglob("*")) if base.exists() else []:
            if f.suffix.lower() not in exts or not f.is_file():
                continue
            rel = f.relative_to(base)
            try:
                info = probe(f)
            except RuntimeError:
                continue
            items.append({
                "path": str(rel),
                "name": f.stem,
                "category": str(rel.parent) if rel.parent != Path(".") else "",
                "duration": round(info["duration"], 2),
            })
        result[kind] = items
    return result


def _thumb_path(src: Path, tag: str, ext: str) -> Path:
    key = hashlib.sha1(f"{src}|{src.stat().st_mtime}|{tag}".encode()).hexdigest()[:16]
    return THUMB_DIR / f"{key}.{ext}"


def extract_frame(src: Path, at: float, out: Path) -> Path:
    out.parent.mkdir(parents=True, exist_ok=True)
    run(["ffmpeg", "-y", "-v", "error", "-ss", f"{at:.2f}", "-i", str(src), "-frames:v", "1", str(out)])
    return out


def cover_1080x1920(img: Image.Image) -> Image.Image:
    """Scale + center-crop to fill a 1080x1920 frame (giống bộ lọc ffmpeg dùng khi render)."""
    from PIL import ImageOps

    return ImageOps.fit(img.convert("RGBA"), (1080, 1920), Image.LANCZOS)


def background_frame(src: Path, at: float | None = None) -> Image.Image:
    info = probe(src)
    at = info["duration"] * 0.3 if at is None else at
    out = _thumb_path(src, f"frame{at:.1f}", "jpg")
    if not out.exists():
        extract_frame(src, at, out)
    return cover_1080x1920(Image.open(out))


def background_thumb(src: Path) -> Path:
    out = _thumb_path(src, "thumb", "jpg")
    if not out.exists():
        img = background_frame(src).convert("RGB")
        img.thumbnail((270, 480))
        img.save(out, quality=82)
    return out


# ── Meme phông xanh ──────────────────────────────────────────────────────────


def detect_key_color(src: Path) -> str | None:
    """Return hex key color (vd "0x00ff00") nếu góc khung hình là phông xanh/xanh dương, else None.

    Kết quả được lưu ra file nhỏ: giải mã lại khung hình mỗi lần liệt kê thư viện rất chậm.
    """
    cached = _thumb_path(src, "key", "txt")
    if cached.exists():
        return cached.read_text().strip() or None
    key = _detect_key_color(src)
    cached.parent.mkdir(parents=True, exist_ok=True)
    cached.write_text(key or "")
    return key


def _detect_key_color(src: Path) -> str | None:
    frame = _thumb_path(src, "keyframe", "png")
    if not frame.exists():
        extract_frame(src, min(0.3, probe(src)["duration"] / 2), frame)
    img = Image.open(frame).convert("RGB")
    w, h = img.size
    pts = [(3, 3), (w - 4, 3), (3, h - 4), (w - 4, h - 4), (w // 2, 3)]
    r, g, b = (sum(img.getpixel(p)[i] for p in pts) // len(pts) for i in range(3))
    if g > r + 50 and g > b + 50 or b > r + 50 and b > g + 50:
        return f"0x{r:02x}{g:02x}{b:02x}"
    return None


def key_out(img: Image.Image, key: str, similarity: float = 0.32) -> Image.Image:
    """PIL/numpy approximation of ffmpeg chromakey, dùng cho ảnh xem trước."""
    import numpy as np

    kr, kg, kb = int(key[2:4], 16), int(key[4:6], 16), int(key[6:8], 16)
    arr = np.asarray(img.convert("RGBA")).copy()
    dist = np.sqrt(((arr[..., :3].astype(np.float32) - (kr, kg, kb)) ** 2).sum(axis=-1))
    arr[..., 3] = np.where(dist < similarity * 255 * 1.7, 0, arr[..., 3])
    return Image.fromarray(arr, "RGBA")


def meme_thumb(src: Path) -> Path:
    """Small PNG of the meme with its green screen removed (cho giao diện)."""
    out = _thumb_path(src, "meme", "png")
    if not out.exists():
        frame = _thumb_path(src, "memeframe", "png")
        extract_frame(src, probe(src)["duration"] * 0.4, frame)
        img = Image.open(frame)
        img.thumbnail((240, 240))
        key = detect_key_color(src)
        (key_out(img, key) if key else img.convert("RGBA")).save(out)
    return out
