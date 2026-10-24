"""Графические карточки экранов бота ADMIRAL (1280×720 HD, Pillow).

Каждый экран бота рендерится как фирменная тёмно-неоновая карточка клуба поверх
атмосферных фонов из ``bot/assets/backgrounds/`` со шрифтами ``Unbounded``,
``Manrope`` и ``JetBrains Mono`` из ``bot/assets/fonts/``.

Чтобы кнопки в Telegram откликались мгновенно (50–100 мс) без повторной загрузки
картинок на сервера Telegram:

* сгенерированные JPEG-байты кэшируются в памяти по ключу содержимого карточки;
* после первой отправки в Telegram запоминается серверный ``file_id`` фотографии
  (отдельно для каждого ``bot.id``), и последующие ``editMessageMedia`` передают
  только короткую строку ``file_id``.
"""

from __future__ import annotations

import hashlib
import io
import logging
import re
from collections import OrderedDict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont, ImageOps

from bot import texts
from bot.config import ClubInfo
from bot.db.models import Admin, Booking, BookingStats, BookingStatus

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# Размеры и палитра кибер-клуба
# --------------------------------------------------------------------------- #
CARD_W: int = 1280
CARD_H: int = 720

ASSETS_DIR: Path = Path(__file__).resolve().parent.parent / "assets"
FONTS_DIR: Path = ASSETS_DIR / "fonts"
BG_DIR: Path = ASSETS_DIR / "backgrounds"
LOGO_PATH: Path = ASSETS_DIR / "logo.png"

# Фирменный неоновый градиент клуба ADMIRAL (по официальному логотипу):
# пурпурно-фиолетовый (#C424FF) → индиго (#6C48FF) → электрический синий (#2269FF)
BRAND_PURPLE = (196, 36, 255, 255)
BRAND_INDIGO = (108, 72, 255, 255)
BRAND_BLUE = (34, 105, 255, 255)

# Основные цвета интерфейса (RGBA)
COL_WHITE = (248, 250, 255, 255)
COL_TEXT = (226, 232, 248, 255)
COL_MUTED = (154, 164, 194, 255)
COL_DIM = (102, 112, 145, 255)

COL_CYAN = (84, 162, 255, 255)  # электрический сапфирово-ледяной акцент
COL_BLUE = (52, 126, 255, 255)  # глубокий королевский синий (#347EFF)
COL_VIOLET = (196, 62, 255, 255)  # фирменный неоновый фиолетовый (#C43EFF)
COL_GOLD = (232, 92, 255, 255)  # премиальный орхидейно-пурпурный неон (VIP / реквизиты)
COL_GREEN = (36, 232, 152, 255)  # изумрудный неон (подтверждено / онлайн)
COL_RED = (255, 78, 126, 255)  # неоновый малиново-алый (отклонено / антифрод)
COL_AMBER = (250, 180, 56, 255)  # статус ожидания проверки

TIER_COLORS: dict[str, tuple[int, int, int, int]] = {
    "standart": (62, 146, 255, 255),
    "vip": (126, 96, 255, 255),
    "svip": (196, 56, 255, 255),
    "double": (236, 84, 226, 255),
}

# Удаляем эмодзи и вариационные селекторы: .ttf-шрифты рисуют вместо них «□».
_EMOJI_RE = re.compile(
    "[\U0001f1e0-\U0001f1ff\U0001f300-\U0001faff\U00002600-\U000027bf\U0000fe00-\U0000fe0f\U0000200d]+"
)


def clean_card_text(value: object) -> str:
    """Убрать эмодзи и лишние пробелы перед отрисовкой в Pillow."""
    cleaned = _EMOJI_RE.sub("", str(value or ""))
    return " ".join(cleaned.split())


# --------------------------------------------------------------------------- #
# Кэш рендеринга и Telegram file_id
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class RenderedCard:
    """Готовая графическая карточка экрана."""

    cache_key: str
    jpeg_bytes: bytes


class CardCache:
    """Двухуровневый кэш: ``cache_key -> JPEG`` и ``(bot_id, cache_key) -> file_id``."""

    def __init__(self, max_images: int = 96, max_file_ids: int = 512) -> None:
        self._max_images = max_images
        self._max_file_ids = max_file_ids
        self._images: OrderedDict[str, bytes] = OrderedDict()
        self._file_ids: OrderedDict[tuple[int, str], str] = OrderedDict()

    def get_bytes(self, key: str) -> bytes | None:
        data = self._images.get(key)
        if data is not None:
            self._images.move_to_end(key)
        return data

    def put_bytes(self, key: str, data: bytes) -> None:
        self._images[key] = data
        self._images.move_to_end(key)
        while len(self._images) > self._max_images:
            self._images.popitem(last=False)

    def get_file_id(self, bot_id: int, key: str) -> str | None:
        fid = self._file_ids.get((bot_id, key))
        if fid is not None:
            self._file_ids.move_to_end((bot_id, key))
        return fid

    def put_file_id(self, bot_id: int, key: str, file_id: str) -> None:
        if not file_id:
            return
        pair = (bot_id, key)
        self._file_ids[pair] = file_id
        self._file_ids.move_to_end(pair)
        while len(self._file_ids) > self._max_file_ids:
            self._file_ids.popitem(last=False)

    def invalidate_file_id(self, bot_id: int, key: str) -> None:
        self._file_ids.pop((bot_id, key), None)


card_cache = CardCache()


def _make_key(kind: str, *parts: object) -> str:
    raw = "|".join([kind, *(str(p) for p in parts)])
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]
    return f"{kind}:{digest}"


# --------------------------------------------------------------------------- #
# Загрузка шрифтов и фонов
# --------------------------------------------------------------------------- #
@lru_cache(maxsize=64)
def _font(family: str, size: int, weight: int = 600) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    """Загрузить кириллический вариативный шрифт нужного размера и насыщенности."""
    filenames = {
        "display": "Unbounded.ttf",
        "sans": "Manrope.ttf",
        "mono": "JetBrainsMono.ttf",
    }
    candidates = [
        FONTS_DIR / filenames.get(family, "Manrope.ttf"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
    ]
    for path in candidates:
        if path.is_file():
            try:
                loaded = ImageFont.truetype(str(path), size=size)
                try:
                    axes = loaded.get_variation_axes()
                    if axes:
                        lo = int(axes[0].get("minimum", 200))
                        hi = int(axes[0].get("maximum", 900))
                        loaded.set_variation_by_axes([max(lo, min(hi, weight))])
                except Exception:  # noqa: BLE001
                    pass
                return loaded
            except Exception:  # noqa: BLE001
                continue
    return ImageFont.load_default()


def _procedural_bg(theme: str) -> Image.Image:
    """Резервный обсидианово-неоновый фон в фирменной гамме #C424FF → #2269FF."""
    _ = theme
    base = Image.new("RGB", (CARD_W, CARD_H), (4, 3, 12))
    draw = ImageDraw.Draw(base, "RGBA")
    for step in range(0, CARD_W, 64):
        draw.line([(step, 0), (step, CARD_H)], fill=(126, 84, 255, 15), width=1)
    for step in range(0, CARD_H, 64):
        draw.line([(0, step), (CARD_W, step)], fill=(64, 120, 255, 15), width=1)
    glow = Image.new("RGBA", (CARD_W, CARD_H), (0, 0, 0, 0))
    gdraw = ImageDraw.Draw(glow)
    gdraw.ellipse((-220, 180, 640, 920), fill=(196, 36, 255, 86))
    gdraw.ellipse((660, -180, 1480, 560), fill=(34, 105, 255, 86))
    glow = glow.filter(ImageFilter.GaussianBlur(95))
    return Image.alpha_composite(base.convert("RGBA"), glow).convert("RGB")


@lru_cache(maxsize=8)
def _load_backdrop(theme: str) -> tuple[Image.Image, Image.Image]:
    """Вернуть ``(базовый_фон_RGB, размытый_фон_для_стекла_RGB)``.

    Важно: холст хранится в режиме ``RGB``, чтобы ``ImageDraw.Draw(canvas, "RGBA")``
    смешивал полупрозрачные плашки и обводки по альфа-каналу, а не перезаписывал
    альфа-байт пикселей.
    """
    path = BG_DIR / f"{theme}.jpg"
    if path.is_file():
        try:
            raw = Image.open(path).convert("RGB")
            fitted = ImageOps.fit(raw, (CARD_W, CARD_H), method=Image.Resampling.LANCZOS)
        except Exception:  # noqa: BLE001
            fitted = _procedural_bg(theme)
    else:
        fitted = _procedural_bg(theme)

    darkened = ImageEnhance.Brightness(fitted).enhance(0.68)
    saturated = ImageEnhance.Color(darkened).enhance(1.18)

    base_rgba = saturated.convert("RGBA")
    vignette = Image.new("RGBA", (CARD_W, CARD_H), (4, 3, 14, 96))
    ImageDraw.Draw(vignette).rectangle((70, 56, CARD_W - 70, CARD_H - 56), fill=(4, 3, 14, 36))
    vignette = vignette.filter(ImageFilter.GaussianBlur(65))
    base_rgb = Image.alpha_composite(base_rgba, vignette).convert("RGB")

    # Матовое стекло: глубокое размытие фона + обсидианово-индиговый оттенок.
    glass_blur = fitted.filter(ImageFilter.GaussianBlur(24))
    glass_blur = ImageEnhance.Brightness(glass_blur).enhance(0.34).convert("RGBA")
    tint = Image.new("RGBA", (CARD_W, CARD_H), (7, 5, 20, 182))
    glass_rgb = Image.alpha_composite(glass_blur, tint).convert("RGB")
    return base_rgb, glass_rgb


# --------------------------------------------------------------------------- #
# Примитивы: матовое стекло, бейджи, эмблема, текст
# --------------------------------------------------------------------------- #
def _text_width(draw: ImageDraw.ImageDraw, text: str, font: Any) -> float:
    return float(draw.textlength(text, font=font))


def _ellipsize(draw: ImageDraw.ImageDraw, text: str, font: Any, max_w: float) -> str:
    text = clean_card_text(text)
    if _text_width(draw, text, font) <= max_w:
        return text
    ellipsis = "…"
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if _text_width(draw, text[:mid].rstrip() + ellipsis, font) <= max_w:
            lo = mid
        else:
            hi = mid - 1
    return text[:lo].rstrip() + ellipsis if lo > 0 else ellipsis


def _wrap_lines(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: Any,
    max_w: float,
    max_lines: int = 3,
) -> list[str]:
    words = clean_card_text(text).split()
    if not words:
        return []
    lines: list[str] = []
    cur = words[0]
    for word in words[1:]:
        trial = f"{cur} {word}"
        if _text_width(draw, trial, font) <= max_w:
            cur = trial
        else:
            lines.append(cur)
            cur = word
            if len(lines) == max_lines - 1:
                break
    used = sum(len(line.split()) for line in lines)
    rest = " ".join(words[used:])
    if rest:
        lines.append(_ellipsize(draw, rest, font, max_w))
    return lines[:max_lines]


def _fit_display_font(
    draw: ImageDraw.ImageDraw,
    text: str,
    max_w: float,
    start_size: int,
    min_size: int = 28,
    weight: int = 800,
) -> Any:
    for size in range(start_size, min_size - 1, -2):
        f = _font("display", size, weight)
        if _text_width(draw, text, f) <= max_w:
            return f
    return _font("display", min_size, weight)


def _draw_gradient_bar(
    canvas: Image.Image,
    box: tuple[int, int, int, int],
    *,
    c_left: tuple[int, int, int, int] = BRAND_PURPLE,
    c_right: tuple[int, int, int, int] = BRAND_BLUE,
    radius: int = 2,
) -> None:
    """Нарисовать фирменную неоновую полосу-градиент #C424FF → #2269FF с мягким свечением."""
    x0, y0, x1, y1 = box
    w, h = x1 - x0, y1 - y0
    if w <= 0 or h <= 0:
        return

    strip = Image.new("RGBA", (w, 1), (0, 0, 0, 0))
    spx = strip.load()
    denom = max(1, w - 1)
    for ix in range(w):
        t = ix / denom
        r = int(round(c_left[0] + (c_right[0] - c_left[0]) * t))
        g = int(round(c_left[1] + (c_right[1] - c_left[1]) * t))
        b = int(round(c_left[2] + (c_right[2] - c_left[2]) * t))
        spx[ix, 0] = (r, g, b, 255)
    bar_img = strip.resize((w, h), Image.Resampling.BILINEAR)

    mask = Image.new("L", (w, h), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, w - 1, h - 1), radius=radius, fill=255)

    pad = 12
    glow_layer = Image.new("RGBA", (w + pad * 2, h + pad * 2), (0, 0, 0, 0))
    glow_layer.paste(bar_img, (pad, pad), mask)
    glow_layer = glow_layer.filter(ImageFilter.GaussianBlur(5))
    canvas.paste(glow_layer, (x0 - pad, y0 - pad), glow_layer)
    canvas.paste(bar_img, (x0, y0), mask)


def _glass_panel(
    canvas: Image.Image,
    glass_src: Image.Image,
    box: tuple[int, int, int, int],
    *,
    radius: int = 24,
    border: tuple[int, int, int, int] = (148, 112, 255, 58),
    top_accent: tuple[int, int, int, int] | None = None,
    gradient_bar: bool = False,
    fill_boost: tuple[int, int, int, int] | None = None,
) -> None:
    """Нарисовать панель из матового стекла с размытием подложки и тонким неоновым кантом."""
    x0, y0, x1, y1 = box
    w, h = x1 - x0, y1 - y0
    if w <= 0 or h <= 0:
        return

    patch = glass_src.crop(box).copy()
    if fill_boost is not None:
        ImageDraw.Draw(patch, "RGBA").rectangle((0, 0, w, h), fill=fill_boost)

    mask_hi = Image.new("L", (w * 2, h * 2), 0)
    ImageDraw.Draw(mask_hi).rounded_rectangle((0, 0, w * 2 - 1, h * 2 - 1), radius=radius * 2, fill=255)
    mask = mask_hi.resize((w, h), Image.Resampling.LANCZOS)
    canvas.paste(patch, (x0, y0), mask)

    draw = ImageDraw.Draw(canvas, "RGBA")
    draw.rounded_rectangle(box, radius=radius, outline=border, width=2)

    if gradient_bar:
        accent_w = min(w - radius * 2, max(120, int(w * 0.54)))
        ax0 = x0 + radius
        _draw_gradient_bar(canvas, (ax0, y0, ax0 + accent_w, y0 + 4), radius=2)
    elif top_accent is not None:
        accent_w = min(w - radius * 2, max(64, int(w * 0.42)))
        ax0 = x0 + radius
        c_end = BRAND_BLUE if top_accent == COL_CYAN else top_accent
        _draw_gradient_bar(canvas, (ax0, y0, ax0 + accent_w, y0 + 4), c_left=top_accent, c_right=c_end, radius=2)


def _pill(
    draw: ImageDraw.ImageDraw,
    x: int,
    y: int,
    text: str,
    *,
    fg: tuple[int, int, int, int] = COL_CYAN,
    bg: tuple[int, int, int, int] = (84, 142, 255, 32),
    border: tuple[int, int, int, int] | None = (118, 146, 255, 118),
    dot: tuple[int, int, int, int] | None = None,
    size: int = 17,
    pad_x: int = 16,
    height: int = 36,
    align_right: bool = False,
) -> tuple[int, int]:
    """Нарисовать аккуратную полупрозрачную капсулу-бейдж и вернуть ``(x0, x1)``."""
    label = clean_card_text(text).upper()
    font = _font("mono", size, 700)
    tw = int(_text_width(draw, label, font))
    dot_space = 18 if dot is not None else 0
    total_w = tw + pad_x * 2 + dot_space
    x0 = x - total_w if align_right else x
    x1 = x0 + total_w
    y1 = y + height
    cy = y + height // 2

    draw.rounded_rectangle((x0, y, x1, y1), radius=height // 2, fill=bg, outline=border, width=1 if border else 0)
    tx = x0 + pad_x
    if dot is not None:
        draw.ellipse((tx, cy - 5, tx + 10, cy + 5), fill=dot)
        tx += 18
    draw.text((tx, cy), label, font=font, fill=fg, anchor="lm")
    return x0, x1


@lru_cache(maxsize=12)
def _load_admiral_logo(diameter: int, opacity: float = 1.0) -> Image.Image:
    """Загрузить официальный неоновый логотип-трезубец ADMIRAL (или отрисовать векторно)."""
    if LOGO_PATH.is_file():
        try:
            raw = Image.open(LOGO_PATH).convert("RGBA")
            resized = raw.resize((diameter, diameter), Image.Resampling.LANCZOS)
            if opacity < 0.999:
                r, g, b, a = resized.split()
                a = a.point(lambda v: int(round(v * opacity)))
                resized = Image.merge("RGBA", (r, g, b, a))
            return resized
        except Exception:  # noqa: BLE001
            pass

    # Точный векторный резерв: неоновое кольцо + трезубец-стрела (#C424FF → #2269FF)
    scale = 2
    box = diameter * scale
    grad = Image.new("RGBA", (box, box), (0, 0, 0, 0))
    gpx = grad.load()
    denom = max(1, (box - 1) * 2)
    for yy in range(box):
        for xx in range(box):
            t = (xx + (box - 1 - yy)) / denom
            cr = int(round(BRAND_PURPLE[0] + (BRAND_BLUE[0] - BRAND_PURPLE[0]) * t))
            cg = int(round(BRAND_PURPLE[1] + (BRAND_BLUE[1] - BRAND_PURPLE[1]) * t))
            cb = int(round(BRAND_PURPLE[2] + (BRAND_BLUE[2] - BRAND_PURPLE[2]) * t))
            gpx[xx, yy] = (cr, cg, cb, int(round(255 * opacity)))

    mask = Image.new("L", (box, box), 0)
    md = ImageDraw.Draw(mask)
    pad = int(box * 0.08)
    ring_w = max(3, int(box * 0.045))
    md.ellipse((pad, pad, box - pad, box - pad), outline=255, width=ring_w)

    cx = box // 2
    # Центральная стрела вверх
    shaft_w = max(4, int(box * 0.085))
    md.rectangle((cx - shaft_w // 2, int(box * 0.30), cx + shaft_w // 2, int(box * 0.76)), fill=255)
    md.polygon(
        [(cx, int(box * 0.16)), (int(box * 0.37), int(box * 0.34)), (int(box * 0.63), int(box * 0.34))], fill=255
    )
    # Нижний V-трезубец с боковыми зубцами
    md.line([(cx, int(box * 0.78)), (int(box * 0.25), int(box * 0.48))], fill=255, width=shaft_w)
    md.line([(cx, int(box * 0.78)), (int(box * 0.75), int(box * 0.48))], fill=255, width=shaft_w)
    md.polygon(
        [(int(box * 0.19), int(box * 0.46)), (int(box * 0.37), int(box * 0.46)), (int(box * 0.29), int(box * 0.56))],
        fill=255,
    )
    md.polygon(
        [(int(box * 0.81), int(box * 0.46)), (int(box * 0.63), int(box * 0.46)), (int(box * 0.71), int(box * 0.56))],
        fill=255,
    )

    out = Image.new("RGBA", (box, box), (0, 0, 0, 0))
    out.paste(grad, (0, 0), mask)
    return out.resize((diameter, diameter), Image.Resampling.LANCZOS)


@lru_cache(maxsize=8)
def _load_exact_avatar_rgb(diameter: int) -> Image.Image | None:
    """Точная копия исходного логотипа клуба из ``bot/assets/avatar.jpg``."""
    avatar_path = ASSETS_DIR / "avatar.jpg"
    if not avatar_path.is_file():
        return None
    try:
        return Image.open(avatar_path).convert("RGB").resize((diameter, diameter), Image.Resampling.LANCZOS)
    except Exception:  # noqa: BLE001
        return None


def _draw_admiral_emblem(
    canvas: Image.Image,
    cx: int,
    cy: int,
    size: int = 34,
    color: tuple[int, int, int, int] = COL_CYAN,
    *,
    opacity: float = 1.0,
) -> None:
    """Скопировать фирменный логотип ADMIRAL с центром в ``(cx, cy)`` без квадратной рамки."""
    import math

    _ = color
    diameter = max(16, int(size * 2))
    x0 = cx - diameter // 2
    y0 = cy - diameter // 2

    src_rgb = _load_exact_avatar_rgb(diameter)
    if opacity >= 0.95 and src_rgb is not None:
        patch = canvas.crop((x0, y0, x0 + diameter, y0 + diameter)).convert("RGB")
        spx = src_rgb.load()
        ppx = patch.load()
        mid = (diameter - 1) / 2.0
        max_r = diameter / 2.0
        for yy in range(diameter):
            for xx in range(diameter):
                dist = math.hypot(xx - mid, yy - mid) / max_r
                if dist >= 0.995:
                    continue
                sr, sg, sb = spx[xx, yy]
                br, bg_c, bb = ppx[xx, yy]
                if dist <= 0.77:
                    # Внутри неонового кольца сохраняем 1:1 пиксели исходной картинки
                    ppx[xx, yy] = (sr, sg, sb)
                else:
                    # За пределами кольца мягко сводим оригинальное неоновое свечение в фон (Screen blend)
                    t = max(0.0, min(1.0, (dist - 0.77) / (0.995 - 0.77)))
                    fade = 0.5 * (1.0 + math.cos(math.pi * t))
                    # Плавный переход подложки от чёрного ядра к фону в зоне 0.77..0.85
                    if dist < 0.85:
                        k = (dist - 0.77) / 0.08
                        br = int(round(br * k))
                        bg_c = int(round(bg_c * k))
                        bb = int(round(bb * k))
                    lr = int(round(sr * fade))
                    lg = int(round(sg * fade))
                    lb = int(round(sb * fade))
                    ppx[xx, yy] = (
                        255 - ((255 - br) * (255 - lr)) // 255,
                        255 - ((255 - bg_c) * (255 - lg)) // 255,
                        255 - ((255 - bb) * (255 - lb)) // 255,
                    )
        canvas.paste(patch, (x0, y0))
        return

    emblem = _load_admiral_logo(diameter, round(opacity, 2))
    canvas.paste(emblem, (x0, y0), emblem)


def _top_brand_bar(
    canvas: Image.Image,
    draw: ImageDraw.ImageDraw,
    club: ClubInfo,
    *,
    right_badge: str,
    accent: tuple[int, int, int, int] = COL_CYAN,
) -> None:
    """Верхняя фирменная полоса всех внутренних экранов с логотипом ADMIRAL."""
    _draw_admiral_emblem(canvas, 74, 58, size=29, color=accent)
    brand_font = _font("display", 22, 800)
    sub_font = _font("sans", 18, 600)
    name = clean_card_text(club.name).upper()
    city = clean_card_text(club.city).upper()
    draw.text((114, 58), name, font=brand_font, fill=COL_WHITE, anchor="lm")
    nw = int(_text_width(draw, name, brand_font))
    draw.text((114 + nw + 16, 58), f"·  {city}  ·  24/7", font=sub_font, fill=COL_MUTED, anchor="lm")

    _pill(
        draw,
        CARD_W - 48,
        40,
        right_badge,
        fg=accent,
        bg=(accent[0], accent[1], accent[2], 32),
        border=(accent[0], accent[1], accent[2], 125),
        dot=accent,
        align_right=True,
    )


def _encode_jpeg(canvas: Image.Image) -> bytes:
    buf = io.BytesIO()
    canvas.convert("RGB").save(buf, format="JPEG", quality=90, optimize=True, progressive=True)
    return buf.getvalue()


def _cached_render(key: str, builder: Any) -> RenderedCard:
    cached = card_cache.get_bytes(key)
    if cached is not None:
        return RenderedCard(cache_key=key, jpeg_bytes=cached)
    canvas = builder()
    jpeg = _encode_jpeg(canvas)
    card_cache.put_bytes(key, jpeg)
    return RenderedCard(cache_key=key, jpeg_bytes=jpeg)


# --------------------------------------------------------------------------- #
# 1. Главное меню (Hero Card)
# --------------------------------------------------------------------------- #
def render_menu_card(
    club: ClubInfo,
    *,
    is_admin: bool = False,
    role_title: str | None = None,
) -> RenderedCard:
    tariff_sig = tuple((t.key, t.hour1, t.night) for t in texts.TARIFFS)
    key = _make_key("menu", club.name, club.city, club.address, club.admin_phone, is_admin, role_title, tariff_sig)

    def _build() -> Image.Image:
        canvas, glass = _load_backdrop("lounge")
        canvas = canvas.copy()

        _glass_panel(
            canvas,
            glass,
            (48, 42, CARD_W - 48, 378),
            radius=28,
            border=(148, 96, 255, 96),
            gradient_bar=True,
        )
        # Мягкий фирменный водяной знак логотипа справа внутри hero-панели
        _draw_admiral_emblem(canvas, 1086, 212, size=128, opacity=0.14)
        draw = ImageDraw.Draw(canvas, "RGBA")

        _pill(
            draw,
            84,
            72,
            f"CYBER LOUNGE · {club.city}",
            fg=COL_CYAN,
            bg=(64, 128, 255, 34),
            border=(116, 148, 255, 125),
            dot=COL_VIOLET,
        )
        if is_admin:
            role_badge = f"ADMIN · {role_title or 'ДОСТУП'}"
            _pill(
                draw,
                CARD_W - 84,
                72,
                role_badge,
                fg=COL_GOLD,
                bg=(196, 56, 255, 36),
                border=(228, 92, 255, 145),
                dot=COL_GOLD,
                align_right=True,
            )
        else:
            _pill(
                draw,
                CARD_W - 84,
                72,
                "ОТКРЫТО 24/7 · FACEIT",
                fg=COL_GREEN,
                bg=(36, 232, 152, 28),
                border=(36, 232, 152, 118),
                dot=COL_GREEN,
                align_right=True,
            )

        _draw_admiral_emblem(canvas, 140, 206, size=62, color=COL_VIOLET)
        title = clean_card_text(club.name).upper()
        title_font = _fit_display_font(draw, title, max_w=900, start_size=74, min_size=42, weight=850)

        glow = Image.new("RGBA", (CARD_W, CARD_H), (0, 0, 0, 0))
        gd = ImageDraw.Draw(glow)
        gd.text((216, 144), title, font=title_font, fill=(196, 36, 255, 96))
        gd.text((222, 144), title, font=title_font, fill=(34, 105, 255, 96))
        glow = glow.filter(ImageFilter.GaussianBlur(16))
        canvas.paste(glow, (0, 0), glow)

        draw = ImageDraw.Draw(canvas, "RGBA")
        draw.text((218, 144), title, font=title_font, fill=COL_WHITE)

        sub_font = _font("sans", 24, 650)
        draw.text(
            (220, 240),
            _ellipsize(draw, f"Игровые залы про-уровня · 4 зоны · {club.hours}", sub_font, 920),
            font=sub_font,
            fill=COL_TEXT,
        )

        draw.line([(84, 304), (CARD_W - 84, 304)], fill=(255, 255, 255, 32), width=1)
        addr_font = _font("sans", 20, 600)
        phone_font = _font("mono", 21, 750)
        phone_str = clean_card_text(club.admin_phone)
        pw = int(_text_width(draw, phone_str, phone_font))
        draw.text((CARD_W - 84 - pw, 326), phone_str, font=phone_font, fill=COL_CYAN)
        draw.text(
            (84, 326),
            _ellipsize(draw, f"{club.city}, {club.address}", addr_font, CARD_W - 210 - pw),
            font=addr_font,
            fill=COL_MUTED,
        )

        # 4 карточки игровых зон в ряд
        gap = 20
        total_w = CARD_W - 96
        card_w = (total_w - gap * 3) // 4
        y0, y1 = 402, CARD_H - 42

        for idx, tariff in enumerate(texts.TARIFFS[:4]):
            x0 = 48 + idx * (card_w + gap)
            x1 = x0 + card_w
            accent = TIER_COLORS.get(tariff.key, COL_CYAN)
            _glass_panel(
                canvas,
                glass,
                (x0, y0, x1, y1),
                radius=22,
                border=(accent[0], accent[1], accent[2], 95),
                top_accent=accent,
            )
            draw = ImageDraw.Draw(canvas, "RGBA")

            zone_font = _font("display", 23, 800)
            pcs_label = f"{tariff.pcs} ПК" if tariff.pcs else "DUO · 2 ПК"
            draw.text((x0 + 22, y0 + 24), clean_card_text(tariff.title).upper(), font=zone_font, fill=COL_WHITE)

            _pill(
                draw,
                x0 + 22,
                y0 + 66,
                pcs_label,
                fg=accent,
                bg=(accent[0], accent[1], accent[2], 32),
                border=(accent[0], accent[1], accent[2], 110),
                size=15,
                pad_x=12,
                height=30,
            )

            price_font = _font("mono", 40, 800)
            unit_font = _font("sans", 19, 600)
            p_str = f"{tariff.hour1} ₽"
            draw.text((x0 + 22, y0 + 116), p_str, font=price_font, fill=COL_WHITE)
            pw_item = int(_text_width(draw, p_str, price_font))
            draw.text((x0 + 22 + pw_item + 8, y0 + 135), "/ час", font=unit_font, fill=COL_MUTED)

            draw.line([(x0 + 22, y0 + 188), (x1 - 22, y0 + 188)], fill=(255, 255, 255, 28), width=1)
            pack_font = _font("sans", 18, 600)
            pack_val = _font("mono", 18, 700)
            draw.text((x0 + 22, y0 + 204), "Утро / День", font=pack_font, fill=COL_MUTED)
            md_str = f"{tariff.morning}/{tariff.day} ₽"
            draw.text(
                (x1 - 22 - int(_text_width(draw, md_str, pack_val)), y0 + 204), md_str, font=pack_val, fill=COL_TEXT
            )

            draw.text((x0 + 22, y0 + 234), "Ночь", font=pack_font, fill=COL_MUTED)
            n_str = f"от {tariff.night} ₽"
            draw.text((x1 - 22 - int(_text_width(draw, n_str, pack_val)), y0 + 234), n_str, font=pack_val, fill=accent)

        return canvas

    return _cached_render(key, _build)


# --------------------------------------------------------------------------- #
# 2. Залы и цены (полная визуальная таблица прайса)
# --------------------------------------------------------------------------- #
def render_prices_card(club: ClubInfo) -> RenderedCard:
    tariff_sig = tuple(
        (t.key, t.pcs, t.hour1, t.hour3, t.hour5, t.morning, t.day, t.night, t.night_weekend) for t in texts.TARIFFS
    )
    key = _make_key("prices", club.name, club.city, tariff_sig)

    def _build() -> Image.Image:
        canvas, glass = _load_backdrop("vip")
        canvas = canvas.copy()
        draw = ImageDraw.Draw(canvas, "RGBA")

        _top_brand_bar(canvas, draw, club, right_badge="ПРАЙС-ЛИСТ КЛУБА", accent=COL_CYAN)
        draw = ImageDraw.Draw(canvas, "RGBA")

        h_font = _font("display", 38, 850)
        draw.text((48, 102), "ЗАЛЫ И ПАКЕТЫ", font=h_font, fill=COL_WHITE)

        _pill(
            draw,
            CARD_W - 48,
            108,
            f"УТРО {texts.MORNING_SLOT} · ДЕНЬ {texts.DAY_SLOT} · НОЧЬ {texts.NIGHT_SLOT}",
            fg=COL_TEXT,
            bg=(255, 255, 255, 22),
            border=(255, 255, 255, 55),
            size=15,
            align_right=True,
        )

        _glass_panel(
            canvas,
            glass,
            (48, 168, CARD_W - 48, 608),
            radius=26,
            border=(148, 96, 255, 92),
            gradient_bar=True,
        )
        draw = ImageDraw.Draw(canvas, "RGBA")

        cols = [
            ("ЗАЛ", 78, "left"),
            ("1 ЧАС", 432, "right"),
            ("3 ЧАСА", 556, "right"),
            ("5 ЧАСОВ", 684, "right"),
            ("УТРО", 808, "right"),
            ("ДЕНЬ", 932, "right"),
            ("НОЧЬ", 1064, "right"),
            ("ПТ–ВС", 1200, "right"),
        ]
        head_font = _font("mono", 17, 700)
        for label, cx, align in cols:
            tw = int(_text_width(draw, label, head_font))
            tx = cx if align == "left" else cx - tw
            draw.text((tx, 194), label, font=head_font, fill=COL_MUTED)

        draw.line([(72, 232), (CARD_W - 72, 232)], fill=(255, 255, 255, 36), width=1)

        row_h = 88
        for idx, t in enumerate(texts.TARIFFS[:4]):
            ry = 244 + idx * row_h
            accent = TIER_COLORS.get(t.key, COL_CYAN)
            if idx % 2 == 1:
                draw.rounded_rectangle((66, ry, CARD_W - 66, ry + row_h - 8), radius=16, fill=(255, 255, 255, 16))

            draw.rounded_rectangle((76, ry + 14, 84, ry + row_h - 22), radius=4, fill=accent)

            z_font = _font("display", 24, 800)
            sub_z = _font("sans", 17, 600)
            draw.text((100, ry + 12), clean_card_text(t.title).upper(), font=z_font, fill=COL_WHITE)
            pcs_txt = f"{t.pcs} игровых ПК" if t.pcs else "Парная комната · 2 ПК"
            draw.text((100, ry + 46), pcs_txt, font=sub_z, fill=COL_MUTED)

            val_font = _font("mono", 25, 750)
            hi_font = _font("mono", 25, 800)
            values = [
                (f"{t.hour1} ₽", 432, COL_WHITE, val_font),
                (f"{t.hour3} ₽", 556, COL_TEXT, val_font),
                (f"{t.hour5} ₽", 684, COL_TEXT, val_font),
                (f"{t.morning} ₽", 808, COL_TEXT, val_font),
                (f"{t.day} ₽", 932, COL_TEXT, val_font),
                (f"{t.night} ₽", 1064, accent, hi_font),
                (f"{t.night_weekend} ₽", 1200, COL_GOLD, hi_font),
            ]
            for val, cx, color, fnt in values:
                vw = int(_text_width(draw, val, fnt))
                draw.text((cx - vw, ry + 24), val, font=fnt, fill=color)

        _glass_panel(
            canvas,
            glass,
            (48, 624, CARD_W - 48, 684),
            radius=18,
            border=(196, 62, 255, 110),
            fill_boost=(142, 42, 255, 26),
        )
        draw = ImageDraw.Draw(canvas, "RGBA")
        foot_font = _font("sans", 19, 700)
        foot_txt = "Предоплата — только на НОЧЬ в ПТ–ВС  ·  В остальные дни оплата на месте  ·  FACEIT работает"
        draw.text((CARD_W // 2, 654), foot_txt, font=foot_font, fill=COL_GOLD, anchor="mm")

        return canvas

    return _cached_render(key, _build)


# --------------------------------------------------------------------------- #
# 3. Мастер бронирования (шаги 1..4 с живым прогресс-баром)
# --------------------------------------------------------------------------- #
def render_wizard_card(
    club: ClubInfo,
    *,
    step: int,
    step_label: str,
    tariff_key: str | None = None,
    time_slot: str | None = None,
    client_name: str | None = None,
    amount_display: str | None = None,
    prepay_required: bool = False,
    waiting_receipt: bool = False,
    is_weekend_day: bool = False,
) -> RenderedCard:
    key = _make_key(
        "wizard",
        club.name,
        club.city,
        club.payment_card,
        step,
        step_label,
        tariff_key,
        time_slot,
        client_name,
        amount_display,
        prepay_required,
        waiting_receipt,
        is_weekend_day,
    )

    def _build() -> Image.Image:
        canvas, glass = _load_backdrop("vip")
        canvas = canvas.copy()
        draw = ImageDraw.Draw(canvas, "RGBA")

        _top_brand_bar(canvas, draw, club, right_badge=f"БРОНЬ · ШАГ {step} ИЗ 4", accent=COL_CYAN)
        draw = ImageDraw.Draw(canvas, "RGBA")

        # --- Прогресс-бар 4 шагов (без наезда линий на подписи) ---
        _glass_panel(canvas, glass, (48, 100, CARD_W - 48, 186), radius=22, border=(255, 255, 255, 42))
        draw = ImageDraw.Draw(canvas, "RGBA")

        steps = [(1, "ЗАЛ"), (2, "ВРЕМЯ"), (3, "ГОСТЬ"), (4, "ОПЛАТА")]
        xs = [96, 392, 696, 1000]
        cy_bar = 143
        spans: list[tuple[int, int]] = []

        for (num, name), rx in zip(steps, xs, strict=True):
            is_done = num < step
            if is_done:
                c_bg, c_fg, c_border = (196, 56, 255, 48), COL_WHITE, COL_VIOLET
            elif num == step:
                c_bg, c_fg, c_border = (52, 126, 255, 56), COL_WHITE, COL_CYAN
            else:
                c_bg, c_fg, c_border = (255, 255, 255, 16), COL_MUTED, (255, 255, 255, 52)

            draw.ellipse((rx, cy_bar - 20, rx + 40, cy_bar + 20), fill=c_bg, outline=c_border, width=2)
            if is_done:
                ccx = rx + 20
                draw.line(
                    [(ccx - 8, cy_bar), (ccx - 2, cy_bar + 6), (ccx + 9, cy_bar - 6)],
                    fill=COL_GOLD,
                    width=3,
                    joint="curve",
                )
            else:
                draw.text((rx + 20, cy_bar), str(num), font=_font("mono", 19, 800), fill=c_fg, anchor="mm")

            lbl_font = _font("sans", 19, 750 if num == step else 600)
            draw.text((rx + 52, cy_bar), name, font=lbl_font, fill=COL_WHITE if num <= step else COL_MUTED, anchor="lm")
            end_x = rx + 52 + int(_text_width(draw, name, lbl_font))
            spans.append((rx, end_x))

        for idx in range(3):
            line_x0 = spans[idx][1] + 22
            line_x1 = spans[idx + 1][0] - 22
            if line_x1 > line_x0:
                if step > idx + 1:
                    _draw_gradient_bar(canvas, (line_x0, cy_bar - 2, line_x1, cy_bar + 2), radius=2)
                else:
                    draw = ImageDraw.Draw(canvas, "RGBA")
                    draw.line([(line_x0, cy_bar), (line_x1, cy_bar)], fill=(255, 255, 255, 42), width=3)
        draw = ImageDraw.Draw(canvas, "RGBA")

        tariff = texts.get_tariff(tariff_key)

        # --- Шаг 1: витрина выбора зала ---
        if step == 1:
            draw.text((52, 210), "ВЫБЕРИТЕ ИГРОВОЙ ЗАЛ", font=_font("display", 32, 800), fill=COL_WHITE)

            gap = 20
            cw = (CARD_W - 96 - gap) // 2
            ch = 198
            for idx, t in enumerate(texts.TARIFFS[:4]):
                col_i, row_i = idx % 2, idx // 2
                x0 = 48 + col_i * (cw + gap)
                y0 = 266 + row_i * (ch + 18)
                accent = TIER_COLORS.get(t.key, COL_CYAN)
                _glass_panel(
                    canvas,
                    glass,
                    (x0, y0, x0 + cw, y0 + ch),
                    radius=22,
                    border=(accent[0], accent[1], accent[2], 98),
                    top_accent=accent,
                )
                draw = ImageDraw.Draw(canvas, "RGBA")
                zf = _font("display", 28, 850)
                df = _font("sans", 19, 600)
                draw.text((x0 + 28, y0 + 24), clean_card_text(t.title).upper(), font=zf, fill=COL_WHITE)
                draw.text((x0 + 28, y0 + 64), clean_card_text(t.description), font=df, fill=COL_MUTED)

                pf = _font("mono", 36, 800)
                p_txt = f"{t.hour1} ₽/ч"
                pw = int(_text_width(draw, p_txt, pf))
                draw.text((x0 + cw - 28 - pw, y0 + 22), p_txt, font=pf, fill=accent)

                draw.line([(x0 + 28, y0 + 114), (x0 + cw - 28, y0 + 114)], fill=(255, 255, 255, 30), width=1)
                mf = _font("mono", 19, 650)
                line_info = (
                    f"3ч {t.hour3} ₽  ·  5ч {t.hour5} ₽  ·  "
                    f"Утро {t.morning} ₽  ·  Ночь {t.night_weekend if is_weekend_day else t.night} ₽"
                )
                draw.text((x0 + 28, y0 + 138), _ellipsize(draw, line_info, mf, cw - 56), font=mf, fill=COL_TEXT)
            return canvas

        # --- Шаг 2: пакеты времени под выбранный зал ---
        if step == 2 and tariff is not None:
            accent = TIER_COLORS.get(tariff.key, COL_CYAN)
            _glass_panel(
                canvas,
                glass,
                (48, 208, CARD_W - 48, 334),
                radius=22,
                border=(accent[0], accent[1], accent[2], 95),
                top_accent=accent,
            )
            draw = ImageDraw.Draw(canvas, "RGBA")
            draw.text((80, 230), "ВЫБРАННЫЙ ЗАЛ", font=_font("mono", 17, 700), fill=COL_MUTED)
            draw.text((80, 258), clean_card_text(tariff.title).upper(), font=_font("display", 34, 850), fill=COL_WHITE)
            draw.text((430, 232), "ПОЧАСОВОЙ ТАРИФ", font=_font("mono", 17, 700), fill=COL_MUTED)
            draw.text(
                (430, 262),
                f"1ч {tariff.hour1} ₽  ·  3ч {tariff.hour3} ₽  ·  5ч {tariff.hour5} ₽",
                font=_font("mono", 25, 750),
                fill=accent,
            )

            slots = [
                ("СЕЙЧАС", "Почасовая игра", f"{tariff.hour1} ₽/ч", COL_CYAN),
                ("УТРО", texts.MORNING_SLOT, f"{tariff.morning} ₽", COL_BLUE),
                ("ДЕНЬ", texts.DAY_SLOT, f"{tariff.day} ₽", COL_VIOLET),
                ("НОЧЬ", texts.NIGHT_SLOT, f"{tariff.night_weekend if is_weekend_day else tariff.night} ₽", COL_GOLD),
            ]
            gap = 18
            sw = (CARD_W - 96 - gap * 3) // 4
            for idx, (stitle, srange, sprice, scol) in enumerate(slots):
                sx = 48 + idx * (sw + gap)
                _glass_panel(
                    canvas,
                    glass,
                    (sx, 358, sx + sw, 672),
                    radius=22,
                    border=(scol[0], scol[1], scol[2], 90),
                    top_accent=scol,
                )
                draw = ImageDraw.Draw(canvas, "RGBA")
                draw.text((sx + 24, 390), stitle, font=_font("display", 24, 800), fill=COL_WHITE)
                draw.text((sx + 24, 432), srange, font=_font("mono", 19, 650), fill=COL_MUTED)
                draw.line([(sx + 24, 490), (sx + sw - 24, 490)], fill=(255, 255, 255, 28), width=1)
                draw.text((sx + 24, 520), sprice, font=_font("mono", 38, 800), fill=scol)
                sub_note = (
                    "ПТ–ВС: предоплата"
                    if stitle == "НОЧЬ" and is_weekend_day
                    else ("Весь пакет целиком" if stitle != "СЕЙЧАС" else "Любая длительность")
                )
                draw.text((sx + 24, 598), sub_note, font=_font("sans", 17, 600), fill=COL_TEXT)
            return canvas

        # --- Шаги 3 и 4: живая карточка предпросмотра брони и оплаты ---
        accent = TIER_COLORS.get(tariff.key if tariff else "", COL_CYAN)
        _glass_panel(
            canvas,
            glass,
            (48, 210, CARD_W - 48, 676),
            radius=26,
            border=(148, 96, 255, 98),
            gradient_bar=True,
        )
        draw = ImageDraw.Draw(canvas, "RGBA")

        headline = (
            "ОЖИДАНИЕ ФОТО ЧЕКА"
            if waiting_receipt
            else ("ПРОВЕРКА И СПОСОБ ОПЛАТЫ" if step == 4 else clean_card_text(step_label).upper())
        )
        draw.text((84, 242), headline, font=_font("display", 32, 850), fill=COL_WHITE)

        if prepay_required or waiting_receipt:
            _pill(
                draw,
                CARD_W - 84,
                244,
                "ТРЕБУЕТСЯ ЧЕК ОПЛАТЫ",
                fg=COL_GOLD,
                bg=(196, 56, 255, 38),
                border=(228, 92, 255, 145),
                dot=COL_GOLD,
                align_right=True,
            )

        fields = [
            ("ЗАЛ", tariff.title if tariff else "—", accent),
            ("ВРЕМЯ", time_slot or "Укажите удобное время в чате…", COL_WHITE if time_slot else COL_CYAN),
            ("ГОСТЬ", client_name or "Напишите имя в чат…", COL_WHITE if client_name else COL_CYAN),
            ("К ОПЛАТЕ", amount_display or "По прайсу зала", COL_GOLD if step == 4 else COL_TEXT),
        ]
        for idx, (flabel, fval, fcol) in enumerate(fields):
            col_i, row_i = idx % 2, idx // 2
            fx = 84 + col_i * 566
            fy = 320 + row_i * 116
            draw.rounded_rectangle(
                (fx, fy, fx + 530, fy + 96),
                radius=18,
                fill=(255, 255, 255, 18),
                outline=(255, 255, 255, 38),
                width=1,
            )
            draw.text((fx + 24, fy + 16), flabel, font=_font("mono", 16, 700), fill=COL_MUTED)
            draw.text(
                (fx + 24, fy + 44),
                _ellipsize(draw, fval, _font("sans", 25, 750), 482),
                font=_font("sans", 25, 750),
                fill=fcol,
            )

        draw.line([(84, 568), (CARD_W - 84, 568)], fill=(255, 255, 255, 30), width=1)
        if step >= 4:
            draw.text((84, 590), "ЕДИНЫЙ НОМЕР КЛУБА ДЛЯ ПЕРЕВОДА:", font=_font("mono", 17, 700), fill=COL_MUTED)
            req_str = f"{club.payment_system}  {club.payment_card}  ({club.payment_holder})"
            draw.text(
                (84, 618),
                _ellipsize(draw, req_str, _font("mono", 24, 800), 1100),
                font=_font("mono", 24, 800),
                fill=COL_CYAN,
            )
        else:
            tip = "Напишите ответ одним сообщением — бот сам примет данные и очистит лишнюю реплику."
            draw.text(
                (84, 604),
                _ellipsize(draw, tip, _font("sans", 20, 600), 1110),
                font=_font("sans", 20, 600),
                fill=COL_MUTED,
            )

        return canvas

    return _cached_render(key, _build)


# --------------------------------------------------------------------------- #
# 4. Билет брони (BOOK_DONE) и список «Мои брони» (BOOKINGS)
# --------------------------------------------------------------------------- #
def render_ticket_card(club: ClubInfo, booking: Booking) -> RenderedCard:
    key = _make_key(
        "ticket",
        club.name,
        club.city,
        booking.id,
        booking.tariff,
        booking.time_slot,
        booking.client_name,
        bool(booking.receipt_file_id),
        booking.status,
    )

    def _build() -> Image.Image:
        canvas, glass = _load_backdrop("pass")
        canvas = canvas.copy()
        draw = ImageDraw.Draw(canvas, "RGBA")

        _top_brand_bar(canvas, draw, club, right_badge=f"TICKET #{booking.id:04d}", accent=COL_CYAN)

        _glass_panel(
            canvas,
            glass,
            (56, 118, CARD_W - 56, 660),
            radius=28,
            border=(148, 96, 255, 104),
            gradient_bar=True,
        )
        _draw_admiral_emblem(canvas, 1102, 520, size=104, opacity=0.11)
        draw = ImageDraw.Draw(canvas, "RGBA")

        status_map = {
            BookingStatus.PENDING: ("ОЖИДАЕТ ПОДТВЕРЖДЕНИЯ · 1–2 МИН", COL_AMBER),
            BookingStatus.APPROVED: ("БРОНЬ ПОДТВЕРЖДЕНА · ЖДЁМ В КЛУБЕ", COL_GREEN),
            BookingStatus.REJECTED: ("ЗАЯВКА ОТКЛОНЕНА", COL_RED),
        }
        st_label, st_col = status_map.get(booking.status, ("ЗАЯВКА ПРИНЯТА", COL_GREEN))

        draw.text((96, 156), f"ЗАЯВКА №{booking.id}", font=_font("display", 46, 850), fill=COL_WHITE)
        _pill(
            draw,
            CARD_W - 96,
            168,
            st_label,
            fg=st_col,
            bg=(st_col[0], st_col[1], st_col[2], 34),
            border=(st_col[0], st_col[1], st_col[2], 135),
            dot=st_col,
            align_right=True,
        )

        for dash_x in range(96, CARD_W - 96, 22):
            draw.line([(dash_x, 242), (dash_x + 12, 242)], fill=(255, 255, 255, 48), width=2)

        pay_label = "Чек отправлен на проверку" if booking.receipt_file_id else "Оплата на месте в клубе"
        cells = [
            ("ИГРОВОЙ ЗАЛ", booking.tariff, COL_CYAN),
            ("ВРЕМЯ СЕССИИ", booking.time_slot, COL_WHITE),
            ("ИМЯ ГОСТЯ", booking.client_name, COL_WHITE),
            ("СТАТУС ОПЛАТЫ", pay_label, COL_GREEN if booking.receipt_file_id else COL_GOLD),
        ]
        for idx, (clabel, cval, ccol) in enumerate(cells):
            col_i, row_i = idx % 2, idx // 2
            cx = 96 + col_i * 556
            cy = 274 + row_i * 136
            draw.rounded_rectangle(
                (cx, cy, cx + 528, cy + 112),
                radius=20,
                fill=(255, 255, 255, 18),
                outline=(255, 255, 255, 38),
                width=1,
            )
            draw.text((cx + 26, cy + 18), clabel, font=_font("mono", 17, 700), fill=COL_MUTED)
            draw.text(
                (cx + 26, cy + 50),
                _ellipsize(draw, cval, _font("sans", 27, 750), 476),
                font=_font("sans", 27, 750),
                fill=ccol,
            )

        foot = f"На входе назовите имя и номер брони №{booking.id}  ·  {club.address}"
        draw.text(
            (96, 588), _ellipsize(draw, foot, _font("sans", 20, 600), 1080), font=_font("sans", 20, 600), fill=COL_MUTED
        )
        return canvas

    return _cached_render(key, _build)


def render_bookings_list_card(club: ClubInfo, bookings: Sequence[Booking], tz: ZoneInfo) -> RenderedCard:
    _ = tz
    sig = tuple((b.id, b.tariff, b.time_slot, b.status) for b in bookings[:4])
    key = _make_key("my_bookings", club.name, club.city, len(bookings), sig)

    def _build() -> Image.Image:
        canvas, glass = _load_backdrop("pass")
        canvas = canvas.copy()
        draw = ImageDraw.Draw(canvas, "RGBA")
        _top_brand_bar(canvas, draw, club, right_badge=f"МОИ БРОНИ · {len(bookings)}", accent=COL_CYAN)

        _glass_panel(
            canvas,
            glass,
            (48, 114, CARD_W - 48, 668),
            radius=26,
            border=(136, 98, 255, 92),
            gradient_bar=True,
        )
        draw = ImageDraw.Draw(canvas, "RGBA")
        draw.text((84, 144), "ИСТОРИЯ ВАШИХ ЗАЯВОК", font=_font("display", 34, 850), fill=COL_WHITE)

        if not bookings:
            draw.rounded_rectangle(
                (84, 240, CARD_W - 84, 520),
                radius=22,
                fill=(255, 255, 255, 16),
                outline=(255, 255, 255, 36),
                width=1,
            )
            draw.text(
                (130, 316), "У вас пока нет активных или прошлых броней", font=_font("display", 28, 750), fill=COL_WHITE
            )
            draw.text(
                (130, 374),
                "Нажмите «Забронировать» под карточкой — выбор зала и времени займёт меньше минуты.",
                font=_font("sans", 22, 600),
                fill=COL_MUTED,
            )
            return canvas

        badge_cfg = {
            BookingStatus.PENDING: ("ОЖИДАЕТ", COL_AMBER),
            BookingStatus.APPROVED: ("ОДОБРЕНА", COL_GREEN),
            BookingStatus.REJECTED: ("ОТКЛОНЕНА", COL_RED),
        }
        for idx, b in enumerate(bookings[:4]):
            ry = 220 + idx * 104
            st_txt, st_col = badge_cfg.get(b.status, (b.status.upper(), COL_CYAN))
            draw.rounded_rectangle(
                (84, ry, CARD_W - 84, ry + 88),
                radius=18,
                fill=(255, 255, 255, 16),
                outline=(st_col[0], st_col[1], st_col[2], 78),
                width=1,
            )
            draw.rounded_rectangle((84, ry, 92, ry + 88), radius=4, fill=st_col)

            draw.text((114, ry + 16), f"#{b.id}", font=_font("mono", 26, 800), fill=COL_WHITE)
            draw.text((210, ry + 15), clean_card_text(b.tariff).upper(), font=_font("display", 23, 800), fill=COL_CYAN)
            draw.text(
                (210, ry + 49),
                _ellipsize(draw, f"{b.time_slot}  ·  гость: {b.client_name}", _font("sans", 19, 600), 690),
                font=_font("sans", 19, 600),
                fill=COL_MUTED,
            )
            _pill(
                draw,
                CARD_W - 112,
                ry + 26,
                st_txt,
                fg=st_col,
                bg=(st_col[0], st_col[1], st_col[2], 32),
                border=(st_col[0], st_col[1], st_col[2], 120),
                dot=st_col,
                align_right=True,
            )
        return canvas

    return _cached_render(key, _build)


# --------------------------------------------------------------------------- #
# 5. Информационные карточки: Контакты, FAQ, Правила, 152-ФЗ
# --------------------------------------------------------------------------- #
def render_contacts_card(club: ClubInfo) -> RenderedCard:
    key = _make_key(
        "contacts",
        club.name,
        club.city,
        club.address,
        club.hours,
        club.admin_name,
        club.admin_phone,
        club.payment_system,
        club.payment_card,
        club.payment_holder,
    )

    def _build() -> Image.Image:
        canvas, glass = _load_backdrop("lounge")
        canvas = canvas.copy()
        draw = ImageDraw.Draw(canvas, "RGBA")
        _top_brand_bar(canvas, draw, club, right_badge="КОНТАКТЫ И РЕКВИЗИТЫ", accent=COL_CYAN)

        _glass_panel(canvas, glass, (48, 116, 650, 668), radius=26, border=(84, 152, 255, 95), top_accent=COL_BLUE)
        _glass_panel(
            canvas, glass, (674, 116, CARD_W - 48, 668), radius=26, border=(196, 62, 255, 98), top_accent=COL_VIOLET
        )
        draw = ImageDraw.Draw(canvas, "RGBA")

        draw.text((82, 150), "СВЯЗЬ С КЛУБОМ", font=_font("display", 30, 850), fill=COL_WHITE)
        draw.text((82, 212), "ТЕЛЕФОН АДМИНИСТРАТОРА", font=_font("mono", 16, 700), fill=COL_MUTED)
        draw.text(
            (82, 242),
            _ellipsize(draw, club.admin_phone, _font("mono", 36, 800), 536),
            font=_font("mono", 36, 800),
            fill=COL_CYAN,
        )
        draw.text((82, 294), clean_card_text(club.admin_name), font=_font("sans", 20, 600), fill=COL_TEXT)

        draw.line([(82, 348), (616, 348)], fill=(255, 255, 255, 30), width=1)
        draw.text((82, 372), "АДРЕС КЛУБА", font=_font("mono", 16, 700), fill=COL_MUTED)
        addr_lines = _wrap_lines(draw, f"г. {club.city}, {club.address}", _font("sans", 23, 700), 530, max_lines=3)
        for i, ln in enumerate(addr_lines):
            draw.text((82, 404 + i * 34), ln, font=_font("sans", 23, 700), fill=COL_WHITE)

        _pill(
            draw,
            82,
            596,
            f"РЕЖИМ РАБОТЫ: {club.hours}",
            fg=COL_GREEN,
            bg=(36, 232, 152, 28),
            border=(36, 232, 152, 115),
            dot=COL_GREEN,
        )

        draw.text((708, 150), "ОПЛАТА БРОНИ", font=_font("display", 30, 850), fill=COL_WHITE)
        draw.text(
            (708, 212),
            f"ЕДИНЫЙ НОМЕР ({clean_card_text(club.payment_system).upper()})",
            font=_font("mono", 16, 700),
            fill=COL_MUTED,
        )
        draw.text(
            (708, 242),
            _ellipsize(draw, club.payment_card, _font("mono", 36, 800), 490),
            font=_font("mono", 36, 800),
            fill=COL_GOLD,
        )
        draw.text(
            (708, 294),
            f"Получатель: {clean_card_text(club.payment_holder)}",
            font=_font("sans", 21, 700),
            fill=COL_WHITE,
        )

        draw.rounded_rectangle(
            (708, 358, CARD_W - 82, 632),
            radius=20,
            fill=(255, 92, 92, 26),
            outline=(255, 92, 92, 105),
            width=1,
        )
        draw.text((734, 382), "ЗАЩИТА ОТ МОШЕННИКОВ", font=_font("mono", 18, 800), fill=COL_RED)
        warn_lines = _wrap_lines(
            draw,
            (
                "Администраторы НЕ пишут первыми в личные сообщения с просьбой перевести деньги. "
                "Других реквизитов у клуба нет."
            ),
            _font("sans", 20, 650),
            440,
            max_lines=5,
        )
        for i, ln in enumerate(warn_lines):
            draw.text((734, 422 + i * 32), ln, font=_font("sans", 20, 650), fill=COL_TEXT)

        return canvas

    return _cached_render(key, _build)


def render_info_card(
    club: ClubInfo,
    *,
    cache_tag: str,
    badge: str,
    title: str,
    subtitle: str,
    highlights: Sequence[tuple[str, str]],
    accent: tuple[int, int, int, int] = COL_CYAN,
    theme: str = "lounge",
) -> RenderedCard:
    """Универсальная карточка разделов FAQ, Правил и 152-ФЗ."""
    key = _make_key("info", cache_tag, club.name, club.city, badge, title, subtitle, tuple(highlights))

    def _build() -> Image.Image:
        canvas, glass = _load_backdrop(theme)
        canvas = canvas.copy()
        draw = ImageDraw.Draw(canvas, "RGBA")
        _top_brand_bar(canvas, draw, club, right_badge=badge, accent=accent)

        _glass_panel(
            canvas,
            glass,
            (48, 114, CARD_W - 48, 668),
            radius=28,
            border=(accent[0], accent[1], accent[2], 92),
            gradient_bar=accent in (COL_CYAN, COL_VIOLET, COL_GOLD),
            top_accent=accent,
        )
        _draw_admiral_emblem(canvas, 1110, 224, size=94, opacity=0.09)
        draw = ImageDraw.Draw(canvas, "RGBA")

        t_lines = _wrap_lines(draw, title.upper(), _font("display", 33, 850), 1100, max_lines=2)
        y_cur = 148
        for ln in t_lines:
            draw.text((84, y_cur), ln, font=_font("display", 33, 850), fill=COL_WHITE)
            y_cur += 44

        sub_lines = _wrap_lines(draw, subtitle, _font("sans", 21, 600), 1100, max_lines=2)
        y_cur += 6
        for ln in sub_lines:
            draw.text((84, y_cur), ln, font=_font("sans", 21, 600), fill=COL_MUTED)
            y_cur += 30

        items = list(highlights[:4])
        if items:
            cols_n = 2 if len(items) > 1 else 1
            gap = 20
            bw = (CARD_W - 168 - (gap if cols_n == 2 else 0)) // cols_n
            bh = 140 if len(items) > 2 else 236
            start_y = 332
            for idx, (htitle, hbody) in enumerate(items):
                ci, ri = idx % cols_n, idx // cols_n
                bx = 84 + ci * (bw + gap)
                by = start_y + ri * (bh + 18)
                draw.rounded_rectangle(
                    (bx, by, bx + bw, by + bh),
                    radius=20,
                    fill=(255, 255, 255, 16),
                    outline=(accent[0], accent[1], accent[2], 65),
                    width=1,
                )
                draw.text(
                    (bx + 24, by + 18),
                    _ellipsize(draw, htitle.upper(), _font("mono", 17, 800), bw - 48),
                    font=_font("mono", 17, 800),
                    fill=accent,
                )
                body_lines = _wrap_lines(draw, hbody, _font("sans", 21, 650), bw - 48, max_lines=2 if bh < 180 else 4)
                for li, bln in enumerate(body_lines):
                    draw.text((bx + 24, by + 52 + li * 30), bln, font=_font("sans", 21, 650), fill=COL_WHITE)

        return canvas

    return _cached_render(key, _build)


# --------------------------------------------------------------------------- #
# 6. Админ-панель: живой дашборд KPI и экраны управления
# --------------------------------------------------------------------------- #
def render_admin_dashboard_card(club: ClubInfo, stats: BookingStats, now_str: str) -> RenderedCard:
    key = _make_key(
        "admin_dash",
        club.name,
        club.city,
        stats.total,
        stats.pending,
        stats.approved,
        stats.rejected,
        stats.users,
        stats.admins_active,
        stats.admins,
        now_str,
    )

    def _build() -> Image.Image:
        canvas, glass = _load_backdrop("admin")
        canvas = canvas.copy()
        draw = ImageDraw.Draw(canvas, "RGBA")
        _top_brand_bar(canvas, draw, club, right_badge=f"CONTROL CENTER · {now_str}", accent=COL_GOLD)

        draw.text((48, 104), "ПАНЕЛЬ УПРАВЛЕНИЯ", font=_font("display", 38, 850), fill=COL_WHITE)
        if stats.pending > 0:
            _pill(
                draw,
                CARD_W - 48,
                110,
                f"ЖДУТ РЕШЕНИЯ: {stats.pending}",
                fg=COL_AMBER,
                bg=(255, 183, 20, 36),
                border=(255, 183, 20, 145),
                dot=COL_AMBER,
                align_right=True,
            )
        else:
            _pill(
                draw,
                CARD_W - 48,
                110,
                "ОЧЕРЕДЬ ПУСТА",
                fg=COL_GREEN,
                bg=(0, 230, 118, 30),
                border=(0, 230, 118, 115),
                dot=COL_GREEN,
                align_right=True,
            )

        tiles = [
            ("ОЖИДАЮТ РЕШЕНИЯ", str(stats.pending), "Активная очередь", COL_AMBER),
            ("ПОДТВЕРЖДЕНО", str(stats.approved), "Одобренные брони", COL_GREEN),
            ("ОТКЛОНЕНО", str(stats.rejected), "Отменённые заявки", COL_RED),
            ("ВСЕГО ЗАЯВОК", str(stats.total), "За всё время", COL_CYAN),
            ("КЛИЕНТОВ В БАЗЕ", str(stats.users), f"Согласие ПДн: {stats.consented}", COL_VIOLET),
            ("АДМИНИСТРАТОРЫ", f"{stats.admins_active} / {stats.admins}", "Активных в смене", COL_GOLD),
        ]
        gap_x, gap_y = 22, 20
        tw = (CARD_W - 96 - gap_x * 2) // 3
        th = 222
        for idx, (tlabel, tval, tsub, tcol) in enumerate(tiles):
            ci, ri = idx % 3, idx // 3
            x0 = 48 + ci * (tw + gap_x)
            y0 = 176 + ri * (th + gap_y)
            _glass_panel(
                canvas,
                glass,
                (x0, y0, x0 + tw, y0 + th),
                radius=24,
                border=(tcol[0], tcol[1], tcol[2], 92),
                top_accent=tcol,
            )
            draw = ImageDraw.Draw(canvas, "RGBA")
            draw.text((x0 + 28, y0 + 26), tlabel, font=_font("mono", 17, 750), fill=COL_MUTED)
            draw.text(
                (x0 + 28, y0 + 74),
                tval,
                font=_font("mono", 56, 800),
                fill=tcol if idx == 0 and stats.pending else COL_WHITE,
            )
            draw.text((x0 + 28, y0 + 166), tsub, font=_font("sans", 19, 600), fill=tcol)

        return canvas

    return _cached_render(key, _build)


def render_admin_pending_card(
    club: ClubInfo,
    entries: Sequence[tuple[Booking, Any]],
    *,
    total_pending: int,
    page: int,
    pages: int,
    tz: ZoneInfo,
) -> RenderedCard:
    today = datetime.now(tz).date()
    sig = tuple((b.id, b.tariff, b.time_slot, b.client_name) for b, _ in entries)
    key = _make_key("admin_pending", club.name, total_pending, page, pages, sig, today.isoformat())

    def _build() -> Image.Image:
        canvas, glass = _load_backdrop("admin")
        canvas = canvas.copy()
        draw = ImageDraw.Draw(canvas, "RGBA")
        badge = f"ОЧЕРЕДЬ · {total_pending}" + (f" · СТР {page + 1}/{pages}" if pages > 1 else "")
        _top_brand_bar(canvas, draw, club, right_badge=badge, accent=COL_AMBER)

        _glass_panel(
            canvas, glass, (48, 112, CARD_W - 48, 672), radius=26, border=(255, 183, 20, 92), top_accent=COL_AMBER
        )
        draw = ImageDraw.Draw(canvas, "RGBA")
        draw.text((84, 140), "ЗАЯВКИ НА ПОДТВЕРЖДЕНИЕ", font=_font("display", 32, 850), fill=COL_WHITE)

        if not entries:
            draw.rounded_rectangle(
                (84, 232, CARD_W - 84, 520),
                radius=22,
                fill=(0, 230, 118, 20),
                outline=(0, 230, 118, 82),
                width=1,
            )
            draw.text(
                (130, 314), "Все заявки разобраны — очередь чиста", font=_font("display", 30, 800), fill=COL_GREEN
            )
            draw.text(
                (130, 374),
                "Новые брони придут в этот чат автоматически вместе с чеком и кнопками решения.",
                font=_font("sans", 21, 600),
                fill=COL_TEXT,
            )
            return canvas

        for idx, (b, _u) in enumerate(entries[:5]):
            ry = 204 + idx * 88
            amt = texts.amount_for_booking(b.tariff, b.time_slot, today)
            amt_txt = f"{amt} ₽" if amt is not None else "По прайсу"
            pay_kind = "ЧЕК" if b.receipt_file_id else "НА МЕСТЕ"
            pcol = COL_GREEN if b.receipt_file_id else COL_GOLD

            draw.rounded_rectangle(
                (84, ry, CARD_W - 84, ry + 76),
                radius=16,
                fill=(255, 255, 255, 16),
                outline=(255, 255, 255, 36),
                width=1,
            )
            draw.text((108, ry + 20), f"#{b.id}", font=_font("mono", 25, 800), fill=COL_AMBER)
            draw.text(
                (204, ry + 13),
                _ellipsize(draw, f"{b.tariff}  ·  {b.time_slot}", _font("sans", 22, 750), 650),
                font=_font("sans", 22, 750),
                fill=COL_WHITE,
            )
            draw.text(
                (204, ry + 43),
                _ellipsize(draw, f"Гость: {b.client_name}", _font("sans", 18, 600), 650),
                font=_font("sans", 18, 600),
                fill=COL_MUTED,
            )
            _pill(
                draw,
                CARD_W - 108,
                ry + 20,
                f"{amt_txt} · {pay_kind}",
                fg=pcol,
                bg=(pcol[0], pcol[1], pcol[2], 32),
                border=(pcol[0], pcol[1], pcol[2], 115),
                size=16,
                align_right=True,
            )
        return canvas

    return _cached_render(key, _build)


def render_admin_team_card(
    club: ClubInfo,
    admins: Sequence[Admin],
    *,
    selected: Admin | None = None,
    adding: bool = False,
) -> RenderedCard:
    sig = tuple((a.user_id, a.role, a.is_active) for a in admins)
    sel_sig = (selected.user_id, selected.role, selected.is_active) if selected else None
    key = _make_key("admin_team", club.name, sig, sel_sig, adding)

    def _build() -> Image.Image:
        canvas, glass = _load_backdrop("admin")
        canvas = canvas.copy()
        draw = ImageDraw.Draw(canvas, "RGBA")
        _top_brand_bar(canvas, draw, club, right_badge="КОМАНДА КЛУБА", accent=COL_GOLD)

        _glass_panel(
            canvas,
            glass,
            (48, 112, CARD_W - 48, 672),
            radius=26,
            border=(196, 62, 255, 96),
            gradient_bar=True,
        )
        draw = ImageDraw.Draw(canvas, "RGBA")

        if adding:
            draw.text((84, 150), "НОВЫЙ АДМИНИСТРАТОР", font=_font("display", 34, 850), fill=COL_WHITE)
            draw.text(
                (84, 214),
                "Отправьте числовой Telegram ID сотрудника одним сообщением в чат",
                font=_font("sans", 23, 650),
                fill=COL_TEXT,
            )
            steps = [
                (
                    "ШАГ 1 · УЗНАТЬ ID",
                    "Попросите сотрудника написать боту @userinfobot и прислать вам его числовой ID.",
                ),
                (
                    "ШАГ 2 · НАЖАТЬ /START",
                    "Сотрудник должен хотя бы раз запустить этого бота, чтобы получать уведомления.",
                ),
            ]
            for idx, (st, sb) in enumerate(steps):
                by = 294 + idx * 150
                draw.rounded_rectangle(
                    (84, by, CARD_W - 84, by + 128),
                    radius=20,
                    fill=(255, 255, 255, 16),
                    outline=(196, 62, 255, 76),
                    width=1,
                )
                draw.text((112, by + 20), st, font=_font("mono", 18, 800), fill=COL_GOLD)
                for li, ln in enumerate(_wrap_lines(draw, sb, _font("sans", 21, 600), 1040, max_lines=2)):
                    draw.text((112, by + 56 + li * 30), ln, font=_font("sans", 21, 600), fill=COL_WHITE)
            return canvas

        if selected is not None:
            st_col = COL_GREEN if selected.is_active else COL_RED
            draw.text((84, 150), "КАРТОЧКА АДМИНИСТРАТОРА", font=_font("display", 34, 850), fill=COL_WHITE)
            _pill(
                draw,
                CARD_W - 84,
                156,
                "АКТИВЕН" if selected.is_active else "ОТКЛЮЧЁН",
                fg=st_col,
                bg=(st_col[0], st_col[1], st_col[2], 32),
                border=(st_col[0], st_col[1], st_col[2], 125),
                dot=st_col,
                align_right=True,
            )
            info_boxes = [
                ("TELEGRAM ID", str(selected.user_id), COL_CYAN),
                ("РОЛЬ В БОТЕ", selected.role_title.upper(), COL_GOLD if selected.is_owner else COL_WHITE),
            ]
            for idx, (il, iv, ic) in enumerate(info_boxes):
                bx = 84 + idx * 566
                draw.rounded_rectangle(
                    (bx, 248, bx + 534, 412),
                    radius=22,
                    fill=(255, 255, 255, 16),
                    outline=(255, 255, 255, 36),
                    width=1,
                )
                draw.text((bx + 28, 274), il, font=_font("mono", 17, 750), fill=COL_MUTED)
                draw.text((bx + 28, 320), iv, font=_font("mono", 38, 800), fill=ic)
            draw.text(
                (84, 470),
                "Управляйте доступом и ролью сотрудника кнопками под этой карточкой.",
                font=_font("sans", 22, 600),
                fill=COL_MUTED,
            )
            return canvas

        active_cnt = sum(1 for a in admins if a.is_active)
        draw.text((84, 144), "АДМИНИСТРАТОРЫ БОТА", font=_font("display", 34, 850), fill=COL_WHITE)
        _pill(
            draw,
            CARD_W - 84,
            150,
            f"АКТИВНЫХ: {active_cnt} ИЗ {len(admins)}",
            fg=COL_GOLD,
            bg=(196, 56, 255, 34),
            border=(228, 92, 255, 130),
            dot=COL_GREEN,
            align_right=True,
        )
        for idx, adm in enumerate(admins[:5]):
            ry = 216 + idx * 84
            scol = COL_GREEN if adm.is_active else COL_RED
            rcol = COL_GOLD if adm.is_owner else COL_CYAN
            draw.rounded_rectangle(
                (84, ry, CARD_W - 84, ry + 70),
                radius=16,
                fill=(255, 255, 255, 16),
                outline=(255, 255, 255, 36),
                width=1,
            )
            draw.ellipse((110, ry + 27, 126, ry + 43), fill=scol)
            draw.text((146, ry + 19), f"ID {adm.user_id}", font=_font("mono", 24, 750), fill=COL_WHITE)
            _pill(
                draw,
                CARD_W - 108,
                ry + 17,
                adm.role_title,
                fg=rcol,
                bg=(rcol[0], rcol[1], rcol[2], 30),
                border=(rcol[0], rcol[1], rcol[2], 110),
                size=16,
                align_right=True,
            )
        return canvas

    return _cached_render(key, _build)


__all__ = [
    "CARD_H",
    "CARD_W",
    "COL_AMBER",
    "COL_BLUE",
    "COL_CYAN",
    "COL_GOLD",
    "COL_GREEN",
    "COL_RED",
    "COL_VIOLET",
    "CardCache",
    "RenderedCard",
    "card_cache",
    "clean_card_text",
    "render_admin_dashboard_card",
    "render_admin_pending_card",
    "render_admin_team_card",
    "render_bookings_list_card",
    "render_contacts_card",
    "render_info_card",
    "render_menu_card",
    "render_prices_card",
    "render_ticket_card",
    "render_wizard_card",
]
