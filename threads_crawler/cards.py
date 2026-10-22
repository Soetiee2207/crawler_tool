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

# Kích thước theo giao diện Threads, phóng to theo U (1 "px" Threads ở cỡ chữ 15px).
TEXT_SIZE = 42
U = TEXT_SIZE / 15
CARD_W = 940
PAD = round(16 * U)
AVATAR = round(36 * U)
GUTTER = round(12 * U)  # khoảng giữa avatar và cột tên
LINE_H = int(TEXT_SIZE * 1.4)
POST_TEXT_W = CARD_W - 2 * PAD  # bài viết: nội dung chạy hết bề ngang dưới avatar
COMMENT_TEXT_W = CARD_W - 2 * PAD - AVATAR - GUTTER  # bình luận: thụt vào thẳng cột tên
MAX_CARD_H = 1320  # = dải dành cho thẻ trong khung 1920 (xem video.CARD_TOP)

THEMES = {
    "dark": {
        "bg": (16, 16, 16, 255), "border": (51, 51, 51), "text": (243, 245, 247),
        "muted": (119, 119, 119), "icon": (204, 204, 204),
    },
    "light": {
        "bg": (255, 255, 255, 255), "border": (219, 219, 219), "text": (0, 0, 0),
        "muted": (153, 153, 153), "icon": (66, 66, 66),
    },
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


def paginate(text: str, max_lines: int = 12, width: int = POST_TEXT_W) -> list[str]:
    """Split long text into chunks that each fit on one card, cắt theo câu khi có thể."""
    f = font(TEXT_SIZE)
    sentences = [s for s in _SENTENCE_RE.findall(text.strip()) if s]
    pages: list[str] = []
    cur = ""
    for sent in sentences:
        candidate = cur + sent
        if len(wrap(candidate.strip(), f, width)) <= max_lines:
            cur = candidate
            continue
        if cur.strip():
            pages.append(cur.strip())
        cur = sent
        lines = wrap(cur.strip(), f, width)
        while len(lines) > max_lines:  # một câu quá dài -> cắt theo dòng
            pages.append(" ".join(line_text(line) for line in lines[:max_lines]).strip())
            lines = lines[max_lines:]
            cur = " ".join(line_text(line) for line in lines)
    if cur.strip():
        pages.append(cur.strip())
    return pages or [""]


# ── Thẻ bài viết / bình luận kiểu Threads ─────────────────────────────────────


def _circle_mask(size: int) -> Image.Image:
    mask = Image.new("L", (size * 4, size * 4), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, size * 4 - 1, size * 4 - 1), fill=255)
    return mask.resize((size, size), Image.LANCZOS)


def _avatar(path: str | None, username: str, ring) -> Image.Image:
    """Avatar tròn + nút "+" (theo dõi) màu trắng ở góc dưới phải, viền cùng màu nền thẻ."""
    av = None
    if path and Path(path).exists():
        try:
            av = ImageOps.fit(Image.open(path).convert("RGBA"), (AVATAR, AVATAR), Image.LANCZOS)
        except OSError:
            av = None
    if av is None:
        hue = sum(map(ord, username)) % 360
        av = Image.new("RGBA", (AVATAR, AVATAR), f"hsl({hue}, 45%, 45%)")
        ImageDraw.Draw(av).text((AVATAR / 2, AVATAR / 2), username[:1].upper(), font=font(int(AVATAR * 0.45), True),
                                fill="white", anchor="mm")
    av.putalpha(_circle_mask(AVATAR))

    badge = round(AVATAR * 0.46)
    border = max(2, round(2.2 * U))
    out = Image.new("RGBA", (AVATAR + border, AVATAR + border), (0, 0, 0, 0))
    out.alpha_composite(av, (0, 0))
    s4 = (badge + 2 * border) * 4
    b = Image.new("RGBA", (s4, s4), (0, 0, 0, 0))
    d = ImageDraw.Draw(b)
    d.ellipse((0, 0, s4 - 1, s4 - 1), fill=ring)
    d.ellipse((border * 4, border * 4, s4 - border * 4 - 1, s4 - border * 4 - 1), fill="white")
    c, arm, w = s4 / 2, badge * 4 * 0.24, max(4, round(badge * 4 * 0.09))
    d.line([(c - arm, c), (c + arm, c)], fill="black", width=w)
    d.line([(c, c - arm), (c, c + arm)], fill="black", width=w)
    b = b.resize((badge + 2 * border, badge + 2 * border), Image.LANCZOS)
    out.alpha_composite(b, (AVATAR + border - b.width + border, AVATAR + border - b.height + border))
    return out


def _verified_badge(size: int) -> Image.Image:
    s = size * 4
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((0, 0, s - 1, s - 1), fill=(0, 149, 246))
    d.line([(s * 0.28, s * 0.52), (s * 0.44, s * 0.68), (s * 0.73, s * 0.36)], fill="white", width=int(s * 0.11),
           joint="curve")
    return img.resize((size, size), Image.LANCZOS)


@lru_cache(maxsize=32)
def _icon(name: str, size: int, color: tuple) -> Image.Image:
    """Line icons giống thanh tương tác của Threads (vẽ to 4 lần rồi thu nhỏ cho mịn)."""
    import math

    S = size * 4
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    w = max(4, round(S * 0.085))
    col = (*color, 255)
    P = lambda x, y: (x * S, y * S)  # noqa: E731

    if name == "heart":
        pts = []
        for i in range(120):
            t = 2 * math.pi * i / 120
            x = 16 * math.sin(t) ** 3
            y = 13 * math.cos(t) - 5 * math.cos(2 * t) - 2 * math.cos(3 * t) - math.cos(4 * t)
            pts.append(P(0.5 + x / 38, 0.47 - y / 38))
        d.line(pts + pts[:2], fill=col, width=w, joint="curve")
    elif name == "comment":
        box = (*P(0.1, 0.1), *P(0.9, 0.9))
        d.arc(box, start=140, end=110 + 360, fill=col, width=w)
        cx, cy, r = S / 2, S / 2, 0.4 * S - w / 2
        a1, a2 = math.radians(110), math.radians(140)
        p1 = (cx + r * math.cos(a1), cy + r * math.sin(a1))
        p2 = (cx + r * math.cos(a2), cy + r * math.sin(a2))
        d.line([p1, P(0.12, 0.9), p2], fill=col, width=w, joint="curve")
    elif name == "repost":
        d.line([P(0.2, 0.58), P(0.2, 0.36), P(0.26, 0.28), P(0.84, 0.28)], fill=col, width=w, joint="curve")
        d.line([P(0.7, 0.14), P(0.85, 0.28), P(0.7, 0.42)], fill=col, width=w, joint="curve")
        d.line([P(0.8, 0.42), P(0.8, 0.64), P(0.74, 0.72), P(0.16, 0.72)], fill=col, width=w, joint="curve")
        d.line([P(0.3, 0.58), P(0.15, 0.72), P(0.3, 0.86)], fill=col, width=w, joint="curve")
    elif name == "share":
        tip, left, inner, bottom = P(0.88, 0.12), P(0.1, 0.4), P(0.44, 0.56), P(0.6, 0.9)
        d.line([tip, left, inner, bottom, tip], fill=col, width=w, joint="curve")
        d.line([tip, inner], fill=col, width=w)
    elif name == "dots":
        for x in (0.2, 0.5, 0.8):
            r = S * 0.075
            d.ellipse((x * S - r, S / 2 - r, x * S + r, S / 2 + r), fill=col)
    return img.resize((size, size), Image.LANCZOS)


def _fmt_count(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}".rstrip("0").rstrip(".") + "M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}".rstrip("0").rstrip(".") + "K"
    return str(n)


def relative_time(iso: str | None) -> str:
    """'3h', '2d', '5w' như Threads; quá 1 năm thì ghi ngày."""
    if not iso:
        return ""
    from datetime import datetime, timezone

    try:
        then = datetime.fromisoformat(iso)
    except ValueError:
        return ""
    if then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)
    sec = max(0, (datetime.now(timezone.utc) - then).total_seconds())
    for unit, size in (("w", 604800), ("d", 86400), ("h", 3600), ("m", 60)):
        if sec >= size:
            n = int(sec // size)
            if unit == "w" and n > 52:
                return then.strftime("%m/%d/%y")
            return f"{n}{unit}"
    return "now"


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
    style: str = "post",
    avatar_path: str | None = None,
    verified: bool = False,
    published_at: str | None = None,
    likes: int | None = None,
    replies: int | None = None,
    reposts: int | None = None,
    image_path: str | None = None,
    page_label: str | None = None,
    theme: str = "dark",
) -> Image.Image:
    """Render a card giống hệt Threads.

    style="post": nội dung chạy hết bề ngang dưới avatar (trang chi tiết bài viết).
    style="comment": nội dung và thanh tương tác thụt vào thẳng cột tên.
    `likes` None = ẩn thanh tương tác (trang giữa của một bài dài).
    """
    t = THEMES.get(theme, THEMES["dark"])
    comment = style == "comment"
    f_text, f_name, f_meta, f_count = font(TEXT_SIZE), font(TEXT_SIZE, True), font(TEXT_SIZE), font(round(14 * U))
    col_x = PAD + AVATAR + GUTTER
    text_x, text_w = (col_x, COMMENT_TEXT_W) if comment else (PAD, POST_TEXT_W)
    lines = wrap(text, f_text, text_w) if text.strip() else []

    name_h = round(TEXT_SIZE * 1.35)
    text_top = PAD + (name_h + round(2 * U) if comment else AVATAR + round(10 * U))
    body_h = len(lines) * LINE_H
    icon = round(19.5 * U)
    actions_h = icon + round(14 * U) if likes is not None else 0
    fixed = text_top + body_h + actions_h + PAD

    pic = None
    room = MAX_CARD_H - fixed - round(12 * U)
    if image_path and Path(image_path).exists() and room >= 260:
        try:
            pic = Image.open(image_path).convert("RGB")
            pic.thumbnail((text_w, min(room, 700)), Image.LANCZOS)
            pic = _rounded(pic, round(8 * U))
        except OSError:
            pic = None
    pic_h = pic.height + round(10 * U) if pic else 0
    h = max(fixed + pic_h, PAD + AVATAR + PAD)

    shadow_pad = 30
    radius = round(14 * U)
    out = Image.new("RGBA", (CARD_W + 2 * shadow_pad, h + 2 * shadow_pad), (0, 0, 0, 0))
    shadow = Image.new("RGBA", out.size, (0, 0, 0, 0))
    ImageDraw.Draw(shadow).rounded_rectangle(
        (shadow_pad, shadow_pad + 8, shadow_pad + CARD_W, shadow_pad + h + 8), radius, fill=(0, 0, 0, 120)
    )
    out.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(16)))

    card = Image.new("RGBA", (CARD_W, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(card)
    d.rounded_rectangle((0, 0, CARD_W - 1, h - 1), radius, fill=t["bg"], outline=t["border"], width=max(2, round(U)))

    # Header: avatar(+) · tên · tích xanh · thời gian ············· ···
    card.alpha_composite(_avatar(avatar_path, username, t["bg"][:3]), (PAD, PAD))
    name_y = PAD if comment else PAD + (AVATAR - name_h) // 2
    d.text((col_x, name_y), username, font=f_name, fill=t["text"])
    x = col_x + f_name.getlength(username) + round(6 * U)
    if verified:
        vb = round(14 * U)
        card.alpha_composite(_verified_badge(vb), (int(x), int(name_y + (name_h - vb) / 2 + U)))
        x += vb + round(6 * U)
    meta = relative_time(published_at)
    if page_label:
        meta = f"{meta} · {page_label}" if meta else page_label
    if meta:
        d.text((x, name_y), meta, font=f_meta, fill=t["muted"])
    dots = _icon("dots", round(20 * U), t["muted"])
    card.alpha_composite(dots, (CARD_W - PAD - dots.width + round(4 * U), int(name_y + (name_h - dots.height) / 2)))

    y = text_top
    if lines:
        y = draw_lines(card, lines, text_x, y, f_text, t["text"], LINE_H)
    if pic:
        y += round(6 * U)
        card.alpha_composite(pic, (text_x, y))
        y += pic.height + round(4 * U)
    if likes is not None:
        y += round(12 * U)
        x = text_x - round(2 * U)
        for name, n in (("heart", likes), ("comment", replies), ("repost", reposts), ("share", None)):
            card.alpha_composite(_icon(name, icon, t["icon"]), (int(x), int(y)))
            x += icon + round(5 * U)
            if n:
                label = _fmt_count(n)
                d.text((x, y + (icon - f_count.size * 1.36) / 2), label, font=f_count, fill=t["icon"])
                x += f_count.getlength(label)
            x += round(16 * U)

    out.alpha_composite(card, (shadow_pad, shadow_pad))
    return out


def render_watermark(text: str, size: int = 46, alpha: int = 90) -> Image.Image:
    """Chữ trắng mờ viền tối nhẹ, dùng làm watermark chạy khắp video."""
    f = font(size, True)
    stroke = 2
    x0, y0, x1, y1 = f.getbbox(text, stroke_width=stroke)
    img = Image.new("RGBA", (x1 - x0 + 4, y1 - y0 + 4), (0, 0, 0, 0))
    ImageDraw.Draw(img).text((2 - x0, 2 - y0), text, font=f, fill=(255, 255, 255, alpha),
                             stroke_width=stroke, stroke_fill=(0, 0, 0, alpha // 2))
    return img


def render_title(text: str, max_w: int = 980) -> Image.Image:
    """Bold white title with black outline (kiểu chữ TikTok), centered, tối đa 3 dòng."""
    f = font(64, True)
    lines = wrap(text.strip(), f, max_w - 40)[:3]
    line_h = int(64 * 1.3)
    img = Image.new("RGBA", (max_w, len(lines) * line_h + 30), (0, 0, 0, 0))
    draw_lines(img, lines, 20, 12, f, "white", line_h, stroke=7, stroke_fill="black", align="center", box_w=max_w - 40)
    return img
