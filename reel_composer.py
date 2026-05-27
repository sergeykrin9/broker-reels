"""
reel_composer.py — PIL слайды 1080×1920 для broker reels.

Адаптация daily-poster/slide_composer.py под real-estate use case:
- Фото юнита как bg (вместо FLUX generation)
- Real-estate бейджи: price, ROI, location-pin, area
- Warm precision luxury палитра (matches ClientRoom/BrokerMobile)
- Captions: top eyebrow + main headline + bottom sub

Usage (standalone smoke test):
  python3 reel_composer.py demo_photos/hero.jpg demo_output_slide.png --type hero --price "12.5M ₽" --area "65 м²"
"""
from __future__ import annotations
from PIL import Image, ImageDraw, ImageFont, ImageFilter
from pathlib import Path
from typing import Dict, List, Optional, Tuple
import argparse
import sys

W, H = 1080, 1920

# ───────────────────────────────────────────────────────────────────
# Дизайн-токены (warm precision luxury, matches ClientRoom)
# ───────────────────────────────────────────────────────────────────
TOKENS = {
    # Брендовая палитра
    "navy":        (0, 61, 85),       # #003D55
    "navy_deep":   (0, 30, 46),       # #001E2E
    "jade":        (30, 92, 94),      # #1E5C5E
    "gold":        (169, 133, 71),    # #A98547
    "gold_light":  (215, 180, 122),   # #D7B47A
    "cream":       (251, 247, 241),   # #FBF7F1
    "ivory":       (245, 235, 215),
    "ink":         (31, 26, 23),      # #1F1A17
    # Текст
    "text_on_dark":   (255, 246, 230),
    "text_muted":     (220, 200, 175),
}

# Шрифты: macOS dev → HelveticaNeue.ttc; production server → Inter/Manrope/Montserrat .ttf рядом
FONT_PATHS = [
    "/System/Library/Fonts/HelveticaNeue.ttc",       # macOS
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",  # Ubuntu fallback
]
FONT_HEADLINE_IDX = 9  # HelveticaNeue Condensed Black
FONT_SUB_IDX = 1       # HelveticaNeue Bold


def _font(size: int, weight_idx: int = 9) -> ImageFont.FreeTypeFont:
    """Try fonts in order; fall back to PIL default if none work."""
    for path in FONT_PATHS:
        try:
            if path.endswith(".ttc"):
                return ImageFont.truetype(path, size, index=weight_idx)
            return ImageFont.truetype(path, size)
        except Exception:
            continue
    return ImageFont.load_default()


# ───────────────────────────────────────────────────────────────────
# Text safety: auto-shrink + smart wrap (защита от overflow за canvas)
# ───────────────────────────────────────────────────────────────────
def _text_width(text: str, font) -> int:
    """Возвращает ширину текста в пикселях."""
    if not text:
        return 0
    bbox = font.getbbox(text)
    return bbox[2] - bbox[0]


def _fit_font(text: str, max_width: int, base_size: int,
              weight_idx: int = FONT_HEADLINE_IDX,
              min_size: int = 28) -> ImageFont.FreeTypeFont:
    """
    Подбирает font_size так, чтобы текст одной строкой влезал в max_width.
    Уменьшает size с base_size до min_size шагом 4px. Если даже min_size
    не помещается — возвращает min_size font (caller сам решает что делать).
    """
    if not text:
        return _font(base_size, weight_idx)
    size = base_size
    while size > min_size:
        f = _font(size, weight_idx)
        if _text_width(text, f) <= max_width:
            return f
        size -= 4
    return _font(min_size, weight_idx)


def _truncate_for_width(text: str, font, max_width: int) -> str:
    """Обрезает текст с многоточием если не помещается в max_width."""
    if _text_width(text, font) <= max_width:
        return text
    ellipsis = "…"
    cur = text
    while cur and _text_width(cur + ellipsis, font) > max_width:
        cur = cur[:-1].rstrip()
    return (cur + ellipsis) if cur else ellipsis


# ───────────────────────────────────────────────────────────────────
# Background composer: фото юнита + warm overlay для читаемости текста
# ───────────────────────────────────────────────────────────────────
def _photo_bg(photo_path: str | Path, dim: float = 0.35, blur: float = 0.0) -> Image.Image:
    """
    Загружает фото юнита, ресайзит в 1080×1920 с cover-cropping, накладывает
    тёмный navy-gradient оверлей для читаемости текста.

    dim: 0..1 — насколько затемнить фото (0.35 → 35% navy overlay)
    blur: 0..10 — гауссовский блюр (для CTA-кадра можно blur=4)
    """
    img = Image.open(photo_path).convert("RGB")

    # Cover-crop to 1080×1920
    src_w, src_h = img.size
    src_ratio = src_w / src_h
    tgt_ratio = W / H
    if src_ratio > tgt_ratio:
        # Source wider — crop sides
        new_w = int(src_h * tgt_ratio)
        offset = (src_w - new_w) // 2
        img = img.crop((offset, 0, offset + new_w, src_h))
    else:
        # Source taller — crop top/bottom (small bias to top — faces usually upper third)
        new_h = int(src_w / tgt_ratio)
        offset = (src_h - new_h) // 3  # 1/3 from top
        img = img.crop((0, offset, src_w, offset + new_h))
    img = img.resize((W, H), Image.LANCZOS)

    if blur > 0:
        img = img.filter(ImageFilter.GaussianBlur(blur))

    # Vertical gradient overlay: navy_deep top → transparent middle → navy_deep bottom
    # This makes top + bottom dark enough for white text overlay without losing photo
    overlay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    navy = TOKENS["navy_deep"]
    for y in range(H):
        # Top zone (0..350) — alpha 0.7 → 0.0
        # Middle (350..1450) — alpha 0.0
        # Bottom (1450..H) — alpha 0.0 → 0.85
        if y < 350:
            t = 1 - (y / 350)
            alpha = int(180 * t)
        elif y > 1450:
            t = (y - 1450) / (H - 1450)
            alpha = int(220 * t)
        else:
            alpha = 0
        # Plus uniform dim for the whole image
        base_alpha = int(255 * dim)
        final_alpha = min(255, base_alpha + alpha)
        draw.line([(0, y), (W, y)], fill=(*navy, final_alpha))

    img_rgba = img.convert("RGBA")
    img_rgba.alpha_composite(overlay)
    return img_rgba.convert("RGB")


# ───────────────────────────────────────────────────────────────────
# Бейджи: price, ROI, area, location, brand-stamp
# ───────────────────────────────────────────────────────────────────
def _draw_price_badge(img: Image.Image, text: str, position: Tuple[int, int]) -> None:
    """
    Бейдж цены: gold gradient pill с белым жирным текстом.
    Position — это верхний левый угол бейджа.
    Auto-shrink: если бейдж не помещается слева→справа canvas с 80px paddings,
    уменьшаем шрифт до 38.
    """
    draw = ImageDraw.Draw(img)
    text = (text or "").strip() or "по запросу"
    # Maximum badge width: canvas width - 80px (left position) - 80px (right padding)
    badge_max_w = W - 160
    pad_x, pad_y = 32, 18
    # Auto-shrink font
    size = 58
    font = _font(size=size, weight_idx=FONT_HEADLINE_IDX)
    while size > 38:
        bbox_try = draw.textbbox((0, 0), text, font=font)
        if (bbox_try[2] - bbox_try[0]) + pad_x * 2 <= badge_max_w:
            break
        size -= 4
        font = _font(size=size, weight_idx=FONT_HEADLINE_IDX)

    # Измеряем текст
    bbox = draw.textbbox((0, 0), text, font=font)
    text_w = bbox[2] - bbox[0]
    text_h = bbox[3] - bbox[1]
    # Если даже на 38 не лезет — обрезаем
    if text_w + pad_x * 2 > badge_max_w:
        text = _truncate_for_width(text, font, badge_max_w - pad_x * 2)
        bbox = draw.textbbox((0, 0), text, font=font)
        text_w = bbox[2] - bbox[0]
        text_h = bbox[3] - bbox[1]

    badge_w = text_w + pad_x * 2
    badge_h = text_h + pad_y * 2 + 8

    x, y = position
    # Тень
    shadow = Image.new("RGBA", (badge_w + 30, badge_h + 30), (0, 0, 0, 0))
    sdraw = ImageDraw.Draw(shadow)
    sdraw.rounded_rectangle(
        [10, 10, badge_w + 10, badge_h + 10],
        radius=badge_h // 2,
        fill=(0, 0, 0, 120),
    )
    shadow = shadow.filter(ImageFilter.GaussianBlur(8))
    img.paste(shadow, (x - 10, y - 5), shadow)

    # Бейдж (gold gradient через две rounded fill)
    badge = Image.new("RGBA", (badge_w, badge_h), (0, 0, 0, 0))
    bdraw = ImageDraw.Draw(badge)
    gold = TOKENS["gold"]
    gold_light = TOKENS["gold_light"]
    # Имитация gradient через 4 layered rect
    for i, color in enumerate([gold_light, gold, gold, gold]):
        y_start = (badge_h * i) // 4
        y_end = (badge_h * (i + 1)) // 4
        bdraw.rounded_rectangle(
            [0, y_start, badge_w, y_end],
            radius=badge_h // 2 if i == 0 else 0,
            fill=color,
        )
    # Финальный round mask
    mask = Image.new("L", (badge_w, badge_h), 0)
    mdraw = ImageDraw.Draw(mask)
    mdraw.rounded_rectangle([0, 0, badge_w, badge_h], radius=badge_h // 2, fill=255)
    badge.putalpha(mask)
    img.paste(badge, (x, y), badge)

    # Текст
    draw.text(
        (x + pad_x, y + pad_y - 4),
        text,
        font=font,
        fill=(255, 255, 255),
    )


def _draw_eyebrow(draw: ImageDraw.ImageDraw, text: str, y: int, color=None) -> None:
    """
    Маленький uppercase eyebrow сверху (как в ClientRoom).
    Auto-shrink + усечение если text слишком длинный для canvas.
    """
    if color is None:
        color = TOKENS["gold_light"]
    text_upper = (text or "").upper()
    # Eyebrow max_width = W - 160 (80px padding с каждой стороны).
    # С учётом 4px letter-spacing итоговая ширина ≈ sum(char_w) + 4*(len-1).
    max_width = W - 160
    font = _font(size=28, weight_idx=FONT_SUB_IDX)
    # Прикинем общую ширину (char-by-char + 4px spacing)
    def total_w(s, f):
        w = 0
        for c in s:
            bbox = f.getbbox(c)
            w += (bbox[2] - bbox[0]) + 4
        return max(0, w - 4)
    # Auto-shrink: если не влезает, уменьшаем шрифт до 20px
    size = 28
    while size >= 20 and total_w(text_upper, font) > max_width:
        size -= 2
        font = _font(size=size, weight_idx=FONT_SUB_IDX)
    # Если даже на 20px не лезет — обрезаем многоточием
    if total_w(text_upper, font) > max_width:
        while text_upper and total_w(text_upper + "…", font) > max_width:
            text_upper = text_upper[:-1]
        text_upper = (text_upper + "…") if text_upper else "…"
    # Letter-spacing trick: рисуем char by char с offset
    x = 80
    for ch in text_upper:
        draw.text((x, y), ch, font=font, fill=color)
        bbox = draw.textbbox((0, 0), ch, font=font)
        x += (bbox[2] - bbox[0]) + 4
    # Декоративная gold line под eyebrow
    line_y = y + 50
    draw.line([(80, line_y), (180, line_y)], fill=color, width=2)


def _draw_main_headline(draw: ImageDraw.ImageDraw, text: str, y: int, max_lines: int = 3) -> int:
    """
    Главный headline: огромный Condensed Black, white, drop-shadow.
    Auto-shrink: если строка вылезает за canvas — уменьшаем font до 60,
    потом разрешаем доп. перенос. Возвращает Y следующей линии.
    """
    text = (text or "").strip()
    max_width = W - 160
    # Стартуем с 92, уменьшаем до 60 если хоть одна строка не помещается
    size = 92
    font = _font(size=size, weight_idx=FONT_HEADLINE_IDX)
    lines = _wrap_text(text, font, max_width=max_width, max_lines=max_lines)
    while size > 60 and any(_text_width(l, font) > max_width for l in lines):
        size -= 6
        font = _font(size=size, weight_idx=FONT_HEADLINE_IDX)
        lines = _wrap_text(text, font, max_width=max_width, max_lines=max_lines)
    # Финальная safety: усечь каждую строку, если что-то ещё не влезло
    safe_lines = [
        l if _text_width(l, font) <= max_width else _truncate_for_width(l, font, max_width)
        for l in lines
    ]
    line_h = int(size * 1.08)  # пропорционально текущему размеру
    for i, line in enumerate(safe_lines):
        # Drop shadow
        draw.text((84, y + i * line_h + 4), line, font=font, fill=(0, 0, 0, 180))
        # Main text
        draw.text((80, y + i * line_h), line, font=font, fill=(255, 255, 255))
    return y + len(safe_lines) * line_h + 20


def _draw_sub(draw: ImageDraw.ImageDraw, text: str, y: int, color=None) -> int:
    """Подзаголовок: средний sub, soft white. Auto-shrink + safety truncate."""
    if color is None:
        color = TOKENS["text_muted"]
    text = (text or "").strip()
    max_width = W - 160
    size = 44
    font = _font(size=size, weight_idx=FONT_SUB_IDX)
    lines = _wrap_text(text, font, max_width=max_width, max_lines=2)
    while size > 28 and any(_text_width(l, font) > max_width for l in lines):
        size -= 4
        font = _font(size=size, weight_idx=FONT_SUB_IDX)
        lines = _wrap_text(text, font, max_width=max_width, max_lines=2)
    safe_lines = [
        l if _text_width(l, font) <= max_width else _truncate_for_width(l, font, max_width)
        for l in lines
    ]
    line_h = int(size * 1.27)
    for i, line in enumerate(safe_lines):
        draw.text((80, y + i * line_h), line, font=font, fill=color)
    return y + len(safe_lines) * line_h + 16


def _wrap_text(text: str, font, max_width: int, max_lines: int) -> List[str]:
    """
    Word-wrap с защитой от single oversized words.
    Если одно слово шире max_width — режем посимвольно (для длинных
    русских слов типа «Расположение» при малом max_width).
    """
    words = text.split()
    lines, cur = [], ""

    def _break_long_word(word: str) -> List[str]:
        """Режет word посимвольно так, чтобы каждый кусок ≤ max_width."""
        parts = []
        chunk = ""
        for ch in word:
            if _text_width(chunk + ch, font) <= max_width:
                chunk += ch
            else:
                if chunk:
                    parts.append(chunk)
                chunk = ch
        if chunk:
            parts.append(chunk)
        return parts

    i = 0
    while i < len(words):
        w = words[i]
        candidate = (cur + " " + w).strip()
        if _text_width(candidate, font) <= max_width:
            cur = candidate
            i += 1
            continue
        # candidate не помещается
        if cur:
            lines.append(cur)
            cur = ""
            if len(lines) >= max_lines:
                return lines[:max_lines]
        # Теперь cur пустой, и одно слово w может быть слишком длинным
        if _text_width(w, font) > max_width:
            chunks = _break_long_word(w)
            for c in chunks:
                if len(lines) >= max_lines - 1:
                    lines.append(c)
                    return lines[:max_lines]
                lines.append(c)
            cur = ""
            i += 1
        else:
            cur = w
            i += 1
    if cur:
        lines.append(cur)
    return lines[:max_lines]


# ───────────────────────────────────────────────────────────────────
# Шаблоны слайдов: 5 типов для broker-reels
# ───────────────────────────────────────────────────────────────────
def _draw_offer_chip(img: Image.Image, text: str, y: int) -> None:
    """
    Sales-chip — золотая овальная плашка с короткой sales-фразой.
    Используется на HERO между sub и price-badge.
    Текст: 1 строка, auto-shrink с 32 до 22, max width = W - 200.
    """
    if not text:
        return
    draw = ImageDraw.Draw(img)
    text = str(text).strip()
    pad_x, pad_y = 22, 12
    max_w = W - 200
    # Auto-shrink
    size = 32
    font = _font(size=size, weight_idx=FONT_SUB_IDX)
    while size > 22:
        bbox = draw.textbbox((0, 0), text, font=font)
        if (bbox[2] - bbox[0]) + pad_x * 2 <= max_w:
            break
        size -= 2
        font = _font(size=size, weight_idx=FONT_SUB_IDX)
    bbox = draw.textbbox((0, 0), text, font=font)
    text_w = bbox[2] - bbox[0]
    text_h = bbox[3] - bbox[1]
    if text_w + pad_x * 2 > max_w:
        text = _truncate_for_width(text, font, max_w - pad_x * 2)
        bbox = draw.textbbox((0, 0), text, font=font)
        text_w = bbox[2] - bbox[0]
    chip_w = text_w + pad_x * 2
    chip_h = text_h + pad_y * 2 + 6
    x = 80
    # Полупрозрачный navy фон с gold-кантом
    chip = Image.new("RGBA", (chip_w, chip_h), (0, 0, 0, 0))
    cdraw = ImageDraw.Draw(chip)
    cdraw.rounded_rectangle([0, 0, chip_w, chip_h], radius=chip_h // 2,
                            fill=(0, 30, 46, 200), outline=TOKENS["gold_light"], width=2)
    img.paste(chip, (x, y), chip)
    # Маленькая золотая точка слева (sales pulse)
    dot_r = 6
    dot_x = x + pad_x // 2 + 2
    dot_y = y + chip_h // 2 - dot_r // 2
    draw.ellipse([dot_x, dot_y, dot_x + dot_r, dot_y + dot_r], fill=TOKENS["gold"])
    # Текст
    draw.text((x + pad_x + dot_r + 6, y + pad_y - 2), text, font=font, fill=(255, 246, 230))


def render_slide_hero(photo_path: str | Path, output: str | Path,
                      project_name: str, location: str, price: str,
                      offer_chip: Optional[str] = None) -> None:
    """
    Слайд №1 — HERO. Главное фото комплекса/виллы + название + локация + price-badge.
    Optional sales-chip над price (короткий закрывающий аргумент).
    """
    img = _photo_bg(photo_path, dim=0.30)
    draw = ImageDraw.Draw(img)

    # Eyebrow
    _draw_eyebrow(draw, "Sun Hills · Phuket".replace("Sun Hills · Phuket", location.split("·")[-1].strip() or "Premium"), y=160)

    # Main headline
    next_y = _draw_main_headline(draw, project_name, y=240)

    # Sub
    _draw_sub(draw, location, y=next_y)

    # Sales chip (optional, выше price badge)
    if offer_chip:
        _draw_offer_chip(img, offer_chip, y=1520)

    # Price badge (bottom)
    _draw_price_badge(img, price, position=(80, 1600))

    img.save(output, "PNG", optimize=True)


def render_slide_interior(photo_path: str | Path, output: str | Path,
                          title: str, area: str, beds: Optional[str] = None) -> None:
    """
    Слайд №2 — INTERIOR. Фото интерьера + название + площадь/планировка.
    """
    img = _photo_bg(photo_path, dim=0.32)
    draw = ImageDraw.Draw(img)

    _draw_eyebrow(draw, "Интерьер", y=160)
    next_y = _draw_main_headline(draw, title, y=240, max_lines=2)
    sub_text = f"{area}" + (f" · {beds}" if beds else "")
    _draw_sub(draw, sub_text, y=next_y)

    img.save(output, "PNG", optimize=True)


def render_slide_view(photo_path: str | Path, output: str | Path,
                      headline: str, distance: str) -> None:
    """
    Слайд №3 — VIEW / LOCATION. Фото вида или окружения + расстояние до точек.
    """
    img = _photo_bg(photo_path, dim=0.30)
    draw = ImageDraw.Draw(img)

    _draw_eyebrow(draw, "Расположение", y=160)
    next_y = _draw_main_headline(draw, headline, y=240)
    _draw_sub(draw, distance, y=next_y)

    img.save(output, "PNG", optimize=True)


def _is_empty_metric(v) -> bool:
    """True если значение пустое/неопределённое и слайд не должен его показывать."""
    if v is None:
        return True
    s = str(v).strip()
    return s in ("", "—", "-", "tba", "TBA", "n/a", "N/A", "None", "null", "0")


def render_slide_roi(photo_path: str | Path, output: str | Path,
                     roi_pct: str, installment: str, completion: str,
                     highlight: Optional[str] = None) -> None:
    """
    Слайд №4 — ROI / NUMBERS. Фото проекта + ДО 3 ключевых цифр крупно.
    Пустые поля скрываются (например, если у проекта нет рассрочки —
    показываем только ROI + Сдача, и центрируем их по высоте).
    """
    img = _photo_bg(photo_path, dim=0.45, blur=1.5)
    draw = ImageDraw.Draw(img)

    _draw_eyebrow(draw, "Цифры проекта", y=160)

    label_font = _font(size=36, weight_idx=FONT_SUB_IDX)
    value_max_width = W - 160

    # Собираем ТОЛЬКО непустые метрики (cleanup от "—" / "tba" / "0").
    candidates = [
        ("ROI", roi_pct),
        ("Рассрочка", installment),
        ("Сдача", completion),
    ]
    metrics = [(lbl, val) for lbl, val in candidates if not _is_empty_metric(val)]

    if not metrics:
        # Если вообще ничего нет — показываем общую "Премиум-проект" метку.
        metrics = [("Премиум-проект", "")]

    # Динамический layout: 1 metric → ровно по центру; 2 → top/bottom thirds;
    # 3 → 3 ряда (как раньше). Это убирает "висящие" пустые слоты "—".
    block_h = 280
    n = len(metrics)
    if n == 1:
        block_y_start = 800
        spacing = 0
    elif n == 2:
        block_y_start = 640
        spacing = block_h + 80
    else:
        block_y_start = 560
        spacing = block_h + 40

    for i, (label, value) in enumerate(metrics):
        y = block_y_start + i * spacing

        # Label (золотой uppercase)
        label_upper = label.upper()
        draw.text((80, y), label_upper, font=label_font, fill=TOKENS["gold_light"])

        if not value:
            continue
        # Value (огромный белый, auto-shrink от 140 до 64)
        value_str = str(value).strip()
        big_font = _fit_font(value_str, max_width=value_max_width,
                             base_size=140, weight_idx=FONT_HEADLINE_IDX,
                             min_size=64)
        if _text_width(value_str, big_font) > value_max_width:
            value_str = _truncate_for_width(value_str, big_font, value_max_width)
        draw.text((84, y + 50 + 4), value_str, font=big_font, fill=(0, 0, 0, 180))
        draw.text((80, y + 50), value_str, font=big_font, fill=(255, 255, 255))

    # Closing highlight (1 строка под цифрами) — sales hook вроде "Кэшбэк 6%".
    if highlight:
        _draw_offer_chip(img, highlight, y=1620)

    img.save(output, "PNG", optimize=True)


def render_slide_cta(photo_path: str | Path, output: str | Path,
                     broker_name: str, contact: str, project_name: str) -> None:
    """
    Слайд №5 — CTA. Фото брокера (если есть) или проекта.
    Если фото брокера — оставляем фото видимым (минимальный blur+dim), но
    усиливаем контраст текста через локальные scrim-полосы сверху и снизу.
    """
    # Минимальный blur (1.5px) и средний dim (0.45) — лицо видно но текст читается
    img = _photo_bg(photo_path, dim=0.45, blur=1.5)
    # Дополнительный top + bottom scrim для гарантированной читаемости текста
    scrim = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    sdraw = ImageDraw.Draw(scrim)
    # Top scrim: 0..600px густо-тёмный → прозрачный
    for y in range(600):
        alpha = int(170 * (1 - y / 600) ** 1.4)
        sdraw.line([(0, y), (W, y)], fill=(0, 18, 28, alpha))
    # Bottom scrim под gold-bar: 1380..H
    for y in range(1380, H):
        t = (y - 1380) / (H - 1380)
        alpha = int(200 * (t ** 1.2))
        sdraw.line([(0, y), (W, y)], fill=(0, 18, 28, alpha))
    img_rgba = img.convert("RGBA")
    img_rgba.alpha_composite(scrim)
    img = img_rgba.convert("RGB")

    draw = ImageDraw.Draw(img)

    _draw_eyebrow(draw, "Свяжитесь со мной", y=200)

    next_y = _draw_main_headline(draw, broker_name, y=320, max_lines=1)
    _draw_sub(draw, project_name, y=next_y, color=TOKENS["gold_light"])

    # CTA contact bar — большой gold rectangle с контактом
    bar_y = 1500
    bar_h = 140
    pad = 80
    bar_inner_w = W - pad * 2 - 48  # 24px inner padding с каждой стороны
    draw.rounded_rectangle(
        [pad, bar_y, W - pad, bar_y + bar_h],
        radius=bar_h // 2,
        fill=TOKENS["gold"],
    )
    # Contact text centered + auto-shrink (handle очень длинный? уменьшаем)
    contact_str = (contact or "@broker").strip()
    contact_font = _fit_font(contact_str, max_width=bar_inner_w,
                             base_size=54, weight_idx=FONT_HEADLINE_IDX,
                             min_size=32)
    if _text_width(contact_str, contact_font) > bar_inner_w:
        contact_str = _truncate_for_width(contact_str, contact_font, bar_inner_w)
    bbox = draw.textbbox((0, 0), contact_str, font=contact_font)
    text_w = bbox[2] - bbox[0]
    text_h = bbox[3] - bbox[1]
    draw.text(
        ((W - text_w) // 2, bar_y + (bar_h - text_h) // 2 - 6),
        contact_str,
        font=contact_font,
        fill=(255, 255, 255),
    )

    img.save(output, "PNG", optimize=True)


# ───────────────────────────────────────────────────────────────────
# Deck-level orchestration: список слайдов → PNG-файлы
# ───────────────────────────────────────────────────────────────────
def render_deck(slides: List[Dict], output_dir: str | Path) -> List[Path]:
    """
    Принимает список slide-dict'ов с полем `type` (hero/interior/view/roi/cta)
    и type-specific полями. Рендерит каждый в PNG, возвращает список путей.

    Example slides:
    [
      {"type": "hero", "photo": "p1.jpg", "project_name": "...", "location": "...", "price": "..."},
      {"type": "interior", "photo": "p2.jpg", "title": "Премиум интерьер", "area": "65 м²"},
      ...
    ]
    """
    from concurrent.futures import ThreadPoolExecutor
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    # Унифицированный маппер: AI-output schema (eyebrow/headline/sub) → render-функции
    def _render_one(i_slide):
        i, slide = i_slide
        out = output_dir / f"slide_{i + 1:02d}.png"
        t = slide["type"]

        def g(key, *aliases, default=""):
            for k in (key, *aliases):
                if slide.get(k):
                    return slide[k]
            return default

        if t == "hero":
            render_slide_hero(
                slide["photo"], out,
                project_name=g("headline", "project_name", default="Проект"),
                location=g("sub", "location", default=""),
                price=g("price", default="по запросу"),
                offer_chip=slide.get("offer_chip"),
            )
        elif t == "interior":
            render_slide_interior(
                slide["photo"], out,
                title=g("headline", "title", default="Юнит"),
                area=g("sub", "area", default=""),
                beds=g("beds", default=None),
            )
        elif t == "view":
            render_slide_view(
                slide["photo"], out,
                headline=g("headline", default="Расположение"),
                distance=g("sub", "distance", default=""),
            )
        elif t == "roi":
            render_slide_roi(
                slide["photo"], out,
                roi_pct=g("roi_pct", default="—"),
                installment=g("installment", default="—"),
                completion=g("completion", default="—"),
                highlight=slide.get("highlight"),
            )
        elif t == "cta":
            render_slide_cta(
                slide["photo"], out,
                broker_name=g("broker_name", "headline", default="Брокер"),
                contact=g("contact", default="@broker"),
                project_name=g("project_name", "sub", default=""),
            )
        else:
            raise ValueError(f"Unknown slide type: {t}")
        print(f"  ✓ rendered slide {i + 1} ({t}) → {out}")
        return (i, out)

    # Параллельный рендеринг — 5 слайдов одновременно (vs sequential)
    # ThreadPool достаточно: PIL GIL-bound, но ImageOps + LANCZOS не CPU-наглые
    with ThreadPoolExecutor(max_workers=5) as pool:
        results = list(pool.map(_render_one, enumerate(slides)))
    # Возвращаем в правильном порядке
    results.sort(key=lambda x: x[0])
    return [out for _, out in results]


# ───────────────────────────────────────────────────────────────────
# CLI smoke test
# ───────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Render a single broker-reel slide for smoke testing")
    parser.add_argument("photo", help="Path to background photo")
    parser.add_argument("output", help="Output PNG path")
    parser.add_argument("--type", default="hero", choices=["hero", "interior", "view", "roi", "cta"])
    parser.add_argument("--price", default="12,5 млн ₽", help="for hero")
    parser.add_argument("--project", default="Sun Hills Layan", help="project name")
    parser.add_argument("--location", default="Layan · Phuket · Thailand", help="location")
    parser.add_argument("--area", default="65 м²", help="for interior")
    parser.add_argument("--title", default="Премиум интерьер", help="for interior")
    parser.add_argument("--roi", default="9.1%", help="for roi")
    parser.add_argument("--installment", default="0% · 2 года", help="for roi")
    parser.add_argument("--completion", default="2028", help="for roi")
    parser.add_argument("--broker", default="Любовь Стрельцова", help="for cta")
    parser.add_argument("--contact", default="@ABG_MEDIA", help="for cta")
    args = parser.parse_args()

    if args.type == "hero":
        render_slide_hero(args.photo, args.output, args.project, args.location, args.price)
    elif args.type == "interior":
        render_slide_interior(args.photo, args.output, args.title, args.area)
    elif args.type == "view":
        render_slide_view(args.photo, args.output, "5 минут до пляжа", "Bang Tao · Layan")
    elif args.type == "roi":
        render_slide_roi(args.photo, args.output, args.roi, args.installment, args.completion)
    elif args.type == "cta":
        render_slide_cta(args.photo, args.output, args.broker, args.contact, args.project)

    print(f"✓ Slide rendered: {args.output}")
