"""Render Threads-style post/comment cards and title banners as transparent PNGs (Pillow)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps

FONT_DIR = Path(__file__).parent / "assets" / "fonts"
EMOJI_FONT_CANDIDATES = [
    "/usr/share/fonts/noto/NotoColorEmoji.ttf",
    "/usr/share/fonts/truetype/noto/NotoColorEmoji.ttf",
    "/usr/share/fonts/google-noto-emoji/NotoColorEmoji.ttf",
    "/System/Library/Fonts/Apple Color Emoji.ttc",
]

CARD_W = 940
PAD = 44
AVATAR = 92
TEXT_SIZE = 44
LINE_H = int(TEXT_SIZE * 1.38)
TEXT_W = CARD_W - 2 * PAD
MAX_CARD_H = 1380  # chừa chỗ cho tiêu đề phía trên trong khung 1920

THEMES = {
    "light": {"bg": (255, 255, 255, 255), "text": (16, 16, 16), "muted": (130, 130, 130), "line": (225, 225, 225)},
    "dark": {"bg": (24, 24, 24, 255), "text": (243, 245, 247), "muted": (150, 150, 150), "line": (60, 60, 60)},
}

EMOJI_RE = re.compile(
    r"(?:[\U0001F1E6-\U0001F1FF]{2}"
    r"|[0-9#*]️?⃣"
    r"|[\U0001F000-\U0001FAFF☀-➿⬀-⯿←-⇿⌀-⏿©®‼⁉™ℹ〰〽]"
    r"[️\U0001F3FB-\U0001F3FF]*"
    r"(?:‍[\U0001F000-\U0001FAFF☀-➿⬀-⯿][️\U0001F3FB-\U0001F3FF]*)*)"
)


@lru_cache(maxsize=None)
def font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(FONT_DIR / ("NotoSans-Bold.ttf" if bold else "NotoSans-Regular.ttf")), size)


@lru_cache(maxsize=1)
def _emoji_font() -> ImageFont.FreeTypeFont | None:
    for path in EMOJI_FONT_CANDIDATES:
        if Path(path).exists():
            try:
                return ImageFont.truetype(path, 109)  # font bitmap màu chỉ hỗ trợ cỡ 109
            except OSError:
                continue
    return None


@lru_cache(maxsize=512)
def _emoji_image(cluster: str, size: int) -> Image.Image | None:
    ef = _emoji_font()
    if ef is None:
        return None
    img = Image.new("RGBA", (160, 160), (0, 0, 0, 0))
    ImageDraw.Draw(img).text((10, 10), cluster, font=ef, embedded_color=True)
    bbox = img.getbbox()
    if not bbox:
        return None
    return img.crop(bbox).resize((size, size), Image.LANCZOS)


# ── Bố cục chữ (chữ + emoji, tự xuống dòng) ──────────────────────────────────


@dataclass
class Atom:
    text: str
    emoji: bool
    width: float


def _atoms(text: str, f: ImageFont.FreeTypeFont) -> list[Atom]:
    """Split a paragraph into words (kèm khoảng trắng phía sau) and emoji clusters."""
    atoms: list[Atom] = []
    pos = 0
    esize = f.size * 1.12
    for m in EMOJI_RE.finditer(text):
        atoms += _word_atoms(text[pos:m.start()], f)
        atoms.append(Atom(m.group(0), True, esize))
        pos = m.end()
    atoms += _word_atoms(text[pos:], f)
    return atoms


def _word_atoms(text: str, f: ImageFont.FreeTypeFont) -> list[Atom]:
    return [Atom(w, False, f.getlength(w)) for w in re.findall(r"\S+\s*|\s+", text)]


def wrap(text: str, f: ImageFont.FreeTypeFont, max_w: float) -> list[list[Atom]]:
    """Wrap text into lines of atoms. Explicit newlines start new lines; blank lines are kept."""
    lines: list[list[Atom]] = []
    for para in text.split("\n"):
        line: list[Atom] = []
        width = 0.0
        for atom in _atoms(para, f):
            if line and width + atom.width - _trailing_space(atom, f) > max_w:
                lines.append(line)
                line, width = [], 0.0
                if not atom.emoji and not atom.text.strip():
                    continue
            if not atom.emoji and atom.width > max_w:  # từ quá dài (link...) -> cắt theo ký tự
                for ch in atom.text:
                    cw = f.getlength(ch)
                    if width + cw > max_w and line:
                        lines.append(line)
                        line, width = [], 0.0
                    line.append(Atom(ch, False, cw))
                    width += cw
                continue
            line.append(atom)
            width += atom.width
        lines.append(line)
    return lines


def _trailing_space(atom: Atom, f: ImageFont.FreeTypeFont) -> float:
    if atom.emoji:
        return 0.0
    stripped = atom.text.rstrip()
    return atom.width - f.getlength(stripped) if stripped != atom.text else 0.0


def line_text(line: list[Atom]) -> str:
    return "".join(a.text for a in line).rstrip()


def draw_lines(
    img: Image.Image, lines: list[list[Atom]], x: int, y: int, f: ImageFont.FreeTypeFont,
    fill, line_h: int, stroke: int = 0, stroke_fill=None, align: str = "left", box_w: int = 0,
) -> int:
    """Draw wrapped lines onto img; returns y after the last line."""
    draw = ImageDraw.Draw(img)
    for line in lines:
        lw = sum(a.width for a in line) - (_trailing_space(line[-1], f) if line else 0)
        cx = x + (box_w - lw) / 2 if align == "center" else x
        for a in line:
            if a.emoji:
                em = _emoji_image(a.text, int(f.size * 1.05))
                if em:
                    img.alpha_composite(em, (int(cx), int(y + (line_h - em.height) / 2)))
            else:
                draw.text((cx, y + (line_h - f.size * 1.36) / 2), a.text, font=f, fill=fill,
                          stroke_width=stroke, stroke_fill=stroke_fill)
            cx += a.width
        y += line_h
    return y


# ── Chia trang ───────────────────────────────────────────────────────────────

_SENTENCE_RE = re.compile(r"[^.!?…\n]*(?:[.!?…]+|\n+|$)\s*")


def paginate(text: str, max_lines: int = 12) -> list[str]:
    """Split long text into chunks that each fit on one card, cắt theo câu khi có thể."""
    f = font(TEXT_SIZE)
    sentences = [s for s in _SENTENCE_RE.findall(text.strip()) if s]
    pages: list[str] = []
    cur = ""
    for sent in sentences:
        candidate = cur + sent
        if len(wrap(candidate.strip(), f, TEXT_W)) <= max_lines:
            cur = candidate
            continue
        if cur.strip():
            pages.append(cur.strip())
        cur = sent
        lines = wrap(cur.strip(), f, TEXT_W)
        while len(lines) > max_lines:  # một câu quá dài -> cắt theo dòng
            pages.append(" ".join(line_text(line) for line in lines[:max_lines]).strip())
            lines = lines[max_lines:]
            cur = " ".join(line_text(line) for line in lines)
    if cur.strip():
        pages.append(cur.strip())
    return pages or [""]


# ── Thẻ bài viết ─────────────────────────────────────────────────────────────


def _avatar(path: str | None, username: str) -> Image.Image:
    mask = Image.new("L", (AVATAR * 4, AVATAR * 4), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, AVATAR * 4 - 1, AVATAR * 4 - 1), fill=255)
    mask = mask.resize((AVATAR, AVATAR), Image.LANCZOS)
    if path and Path(path).exists():
        try:
            av = ImageOps.fit(Image.open(path).convert("RGBA"), (AVATAR, AVATAR), Image.LANCZOS)
            av.putalpha(mask)
            return av
        except OSError:
            pass
    hue = sum(map(ord, username)) % 360
    av = Image.new("RGBA", (AVATAR, AVATAR), f"hsl({hue}, 55%, 50%)")
    d = ImageDraw.Draw(av)
    d.text((AVATAR / 2, AVATAR / 2), username[:1].upper(), font=font(44, True), fill="white", anchor="mm")
    av.putalpha(mask)
    return av


def _verified_badge(size: int) -> Image.Image:
    s = size * 4
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((0, 0, s - 1, s - 1), fill=(0, 149, 246))
    d.line([(s * 0.28, s * 0.52), (s * 0.44, s * 0.68), (s * 0.73, s * 0.36)], fill="white", width=int(s * 0.11),
           joint="curve")
    return img.resize((size, size), Image.LANCZOS)


def _fmt_count(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}".rstrip("0").rstrip(".") + "M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}".rstrip("0").rstrip(".") + "K"
    return str(n)


def _rounded(img: Image.Image, radius: int) -> Image.Image:
    mask = Image.new("L", img.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, img.width - 1, img.height - 1), radius, fill=255)
    out = img.convert("RGBA")
    out.putalpha(mask)
    return out


def render_card(
    username: str,
    text: str,
    *,
    avatar_path: str | None = None,
    verified: bool = False,
    reply_to: str | None = None,
    likes: int | None = None,
    replies: int | None = None,
    image_path: str | None = None,
    page_label: str | None = None,
    theme: str = "light",
) -> Image.Image:
    """Render one card. `likes`/`replies` None = ẩn dòng thống kê (dùng cho trang tiếp theo)."""
    t = THEMES.get(theme, THEMES["light"])
    f_text, f_name, f_small = font(TEXT_SIZE), font(38, True), font(30)
    lines = wrap(text, f_text, TEXT_W)

    header_h = AVATAR
    body_h = len(lines) * LINE_H if text.strip() else 0
    stats_h = 70 if likes is not None else 0

    # Ảnh co lại cho vừa phần còn trống; quá ít chỗ thì bỏ ảnh.
    pic = None
    room = MAX_CARD_H - (PAD + header_h + 26 + body_h + stats_h + PAD + 18)
    if image_path and Path(image_path).exists() and room >= 260:
        try:
            pic = Image.open(image_path).convert("RGB")
            pic.thumbnail((TEXT_W, min(room, 680)), Image.LANCZOS)
            pic = _rounded(pic, 24)
        except OSError:
            pic = None
    pic_h = pic.height + 28 if pic else 0
    h = PAD + header_h + 26 + body_h + pic_h + stats_h + PAD - 10

    shadow_pad = 30
    out = Image.new("RGBA", (CARD_W + 2 * shadow_pad, h + 2 * shadow_pad), (0, 0, 0, 0))
    shadow = Image.new("RGBA", out.size, (0, 0, 0, 0))
    ImageDraw.Draw(shadow).rounded_rectangle(
        (shadow_pad, shadow_pad + 8, shadow_pad + CARD_W, shadow_pad + h + 8), 40, fill=(0, 0, 0, 110)
    )
    out.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(16)))

    card = Image.new("RGBA", (CARD_W, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(card)
    d.rounded_rectangle((0, 0, CARD_W - 1, h - 1), 40, fill=t["bg"])

    # Header: avatar + tên + tích xanh + dòng phụ
    card.alpha_composite(_avatar(avatar_path, username), (PAD, PAD))
    nx = PAD + AVATAR + 24
    d.text((nx, PAD + 4), username, font=f_name, fill=t["text"])
    name_w = f_name.getlength(username)
    if verified:
        card.alpha_composite(_verified_badge(34), (int(nx + name_w + 12), PAD + 14))
    sub = f"Trả lời @{reply_to}" if reply_to else "Threads"
    if page_label:
        sub += f"  ·  {page_label}"
    d.text((nx, PAD + 52), sub, font=f_small, fill=t["muted"])

    y = PAD + header_h + 26
    if text.strip():
        y = draw_lines(card, lines, PAD, y, f_text, t["text"], LINE_H)
    if pic:
        y += 14
        card.alpha_composite(pic, (PAD + (TEXT_W - pic.width) // 2, y))
        y += pic.height + 14
    if likes is not None:
        y += 14
        x = PAD
        for icon, n in (("❤️", likes), ("💬", replies or 0)):
            em = _emoji_image(icon, 36)
            if em:
                card.alpha_composite(em, (x, y + 2))
            d.text((x + 48, y - 2), _fmt_count(n), font=f_small, fill=t["muted"])
            x += 48 + int(f_small.getlength(_fmt_count(n))) + 44

    out.alpha_composite(card, (shadow_pad, shadow_pad))
    return out


def render_title(text: str, max_w: int = 980) -> Image.Image:
    """Bold white title with black outline (kiểu chữ TikTok), centered, tối đa 3 dòng."""
    f = font(64, True)
    lines = wrap(text.strip(), f, max_w - 40)[:3]
    line_h = int(64 * 1.3)
    img = Image.new("RGBA", (max_w, len(lines) * line_h + 30), (0, 0, 0, 0))
    draw_lines(img, lines, 20, 12, f, "white", line_h, stroke=7, stroke_fill="black", align="center", box_w=max_w - 40)
    return img
