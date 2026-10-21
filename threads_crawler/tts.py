"""Vietnamese text-to-speech via VieNeu-TTS (ONNX on CPU), with caching and text cleanup."""

from __future__ import annotations

import hashlib
import logging
import re
import threading
from pathlib import Path

import numpy as np
import soundfile as sf

from . import PROJECT_ROOT

log = logging.getLogger(__name__)

CACHE_DIR = PROJECT_ROOT / ".cache" / "tts"
DEFAULT_VOICE = "Hải Đăng"

_models: dict[str, object] = {}
_lock = threading.RLock()

# Teencode / viết tắt hay gặp trên Threads -> chữ đầy đủ để TTS đọc tự nhiên.
ABBREVIATIONS = {
    "ko": "không", "k": "không", "kh": "không", "hok": "không", "hong": "không", "hông": "không",
    "khum": "không", "dc": "được", "đc": "được", "dk": "được", "vs": "với", "mn": "mọi người",
    "mng": "mọi người", "mk": "mình", "mik": "mình", "j": "gì", "z": "vậy", "zị": "vậy", "dị": "vậy",
    "v": "vậy", "ng": "người", "r": "rồi", "rùi": "rồi", "bn": "bạn", "b": "bạn", "trc": "trước",
    "ny": "người yêu", "ck": "chồng", "vk": "vợ", "cx": "cũng", "cg": "cũng", "ib": "nhắn tin",
    "inb": "nhắn tin", "nt": "nhắn tin", "sdt": "số điện thoại", "đt": "điện thoại", "tr": "trời",
    "lun": "luôn", "nhìu": "nhiều", "bít": "biết", "bik": "biết", "bjo": "bây giờ", "bh": "bây giờ",
    "h": "giờ", "ntn": "như thế nào", "e": "em", "a": "anh", "c": "chị", "ae": "anh em",
    "tp": "thành phố", "hcm": "Hồ Chí Minh", "hn": "Hà Nội", "sg": "Sài Gòn", "đn": "Đà Nẵng",
    "ok": "ô kê", "oke": "ô kê", "okela": "ô kê la", "kk": "", "kkk": "", "haha": "ha ha",
    "hihi": "hi hi", "huhu": "hu hu", "trùi": "trời", "thui": "thôi",
}

_EMOJI_RE = re.compile(
    "[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U00002B00-\U00002BFF\U0001F1E6-\U0001F1FF"
    "\U0000FE0F\U0000200D\U000020E3\U00002190-\U000021FF\U00002300-\U000023FF]+"
)
_URL_RE = re.compile(r"https?://\S+|www\.\S+")
_WORD_RE = re.compile(r"\b\w+\b", re.UNICODE)


def normalize_for_speech(text: str) -> str:
    """Clean social-media text so TTS reads it naturally: bỏ link/emoji, mở rộng viết tắt."""
    text = _URL_RE.sub(" ", text)
    text = _EMOJI_RE.sub(" ", text)
    text = re.sub(r"[#@](\w+)", r"\1", text)
    text = re.sub(r"(\w)\1{2,}", r"\1", text)  # "ơiiii" -> "ơi", "quáaaa" -> "quá"
    text = re.sub(r"=\)+|:\)+|:\(+|\)\)+|\(\(+|:v|=\(+|<3", " ", text)

    def expand(m: re.Match) -> str:
        word = m.group(0)
        return ABBREVIATIONS.get(word.lower(), word)

    text = _WORD_RE.sub(expand, text)
    text = re.sub(r"^[ \t]*[-*•+>]+[ \t]*", "", text, flags=re.M)  # gạch đầu dòng
    text = re.sub(r"[*_~`|]", " ", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"([.!?,:;…])\s*\n\s*", r"\1 ", text)
    text = re.sub(r"\s*\n\s*", ". ", text)  # xuống dòng -> ngắt câu
    text = re.sub(r"([.!?,])(\s*[.!?,])+", r"\1", text)
    return text.strip(" .")


def get_model(precision: str = "fp32"):
    """Load (once) and return the VieNeu model. `precision` = "fp32" (chuẩn) or "int8" (nhanh)."""
    with _lock:
        if precision not in _models:
            from vieneu import Vieneu

            log.info("Đang nạp VieNeu-TTS (%s)... lần đầu có thể mất ~1 phút", precision)
            _models[precision] = Vieneu(precision=precision)
        return _models[precision]


def list_voices() -> list[dict]:
    """Preset voices from the file bundled with vieneu (không cần nạp model), giọng tuyển chọn trước."""
    import json

    import vieneu

    data = json.loads((Path(vieneu.__file__).parent / "assets" / "voices_v3_turbo.json").read_text("utf-8"))
    voices = [
        {
            "id": name,
            "label": f"{'⭐ ' if v.get('featured') else ''}{name} — {v.get('description', '')}",
            "gender": v.get("gender"),
            "region": v.get("region"),
            "featured": int(v.get("featured") or 99),
        }
        for name, v in data["presets"].items()
    ]
    return sorted(voices, key=lambda v: v["featured"])


def synthesize(text: str, voice: str = DEFAULT_VOICE, precision: str = "fp32") -> Path:
    """Synthesize `text` to a cached WAV file and return its path."""
    key = hashlib.sha1(f"{precision}|{voice}|{text}".encode()).hexdigest()[:20]
    out = CACHE_DIR / f"{key}.wav"
    if out.exists():
        return out
    model = get_model(precision)
    with _lock:  # ONNX session không an toàn khi gọi song song
        audio = model.infer(text, voice=voice)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    sf.write(out, np.asarray(audio, dtype=np.float32), model.sample_rate)
    return out


def duration_of(wav: Path) -> float:
    info = sf.info(wav)
    return info.frames / info.samplerate
