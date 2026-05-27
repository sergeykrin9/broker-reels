"""
orchestrator.py — end-to-end broker reel generation.

Input: unit data + project data + broker data + 5 photo paths (or URLs)
Output: MP4 (1080×1920, 15-20 sec, Ken Burns zoom, без voiceover, с captions)

Steps:
  1. caption_generator → 5 slides JSON (NVIDIA Llama cascade)
  2. reel_composer → 5 PNG slides
  3. ffmpeg → MP4 (concat + Ken Burns + optional bg music)

CLI:
  python3 orchestrator.py --demo   # из demo_photos/ + hardcoded data
  python3 orchestrator.py --input config.json --photos path/to/photos --output reel.mp4
"""
from __future__ import annotations
import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
from reel_composer import render_deck
from caption_generator import generate_reel_script, fallback_reel_script


SLIDE_DURATION_SEC = 3.5   # каждый слайд по 3.5 сек = 17.5 сек total
SLIDE_FPS = 24             # 24 вместо 30 (-20% frames, не заметно для image-based slideshow)
W, H = 1080, 1920


# ───────────────────────────────────────────────────────────────────
# ffmpeg compose: 5 PNG → MP4 с Ken Burns zoom + optional music
# ───────────────────────────────────────────────────────────────────
def _check_ffmpeg() -> bool:
    """Check if ffmpeg is on PATH."""
    return shutil.which("ffmpeg") is not None


def _ken_burns_filter(slide_idx: int, dur_sec: float, fps: int) -> str:
    """
    Плавное camera movement через scale+crop+pan вместо zoompan (jitter-free).

    Стратегия: scale image в 1.25x → crop 1080×1920 со смещением которое плавно
    меняется по времени. Каждый слайд получает РАЗНОЕ движение для разнообразия:
      0 (hero):      pan right → left (cinematic reveal)
      1 (interior):  zoom in slow (1.0 → 1.15 inside scaled frame)
      2 (view):      pan left → right
      3 (roi):       very subtle zoom in (текст должен читаться)
      4 (cta):       pan top → down + slight zoom

    Subpixel-smooth т.к. crop не округляет float positions до integer pixel grid
    (в отличие от zoompan который это делает и оттого "дрожит").
    """
    # ВАЖНО: slide.png рендерится в reel_composer уже в 1080×1920 С ТЕКСТОМ
    # по краям. Если scale slide на 1.25× и потом crop с pan — текст уезжает
    # за края 1080×1920 crop (его SAFE-AREA в композере = 80px от краёв,
    # т.е. при 25% buffer текст вылезает на 8-10%).
    #
    # Решение: уменьшаем scale до 1.08 (8% буфер) — text-safe.
    # Движения тоже ослабляем — pan только ~40% от и так маленького buffer.
    SCALE = 1.08
    scaled_w = int(W * SCALE)
    scaled_h = int(H * SCALE)
    max_pan_x = scaled_w - W  # ≈ 86px
    max_pan_y = scaled_h - H  # ≈ 153px

    # Все движения МЕЛКИЕ — это subtle camera drift внутри 8% буфера.
    # Текст ВСЕГДА в видимой области, дрожи нет, движение «дышит».
    patterns = [
        # 0: HERO — pan right → center
        {
            "x": f"{max_pan_x}*(0.7-0.5*t/{dur_sec})",
            "y": f"{max_pan_y}/2",
        },
        # 1: INTERIOR — gentle zoom in (1.04 → 1.10) — текст безопасен
        {
            "zoom_anim": True,
        },
        # 2: VIEW — pan left → center
        {
            "x": f"{max_pan_x}*(0.2+0.5*t/{dur_sec})",
            "y": f"{max_pan_y}/2",
        },
        # 3: ROI — еле заметный sin/cos drift
        {
            "x": f"{max_pan_x}/2 + 12*sin(t*PI/{dur_sec})",
            "y": f"{max_pan_y}/2 + 8*cos(t*PI/{dur_sec})",
        },
        # 4: CTA — slow pan downward
        {
            "x": f"{max_pan_x}/2",
            "y": f"{max_pan_y}*(0.3+0.4*t/{dur_sec})",
        },
    ]
    p = patterns[slide_idx % len(patterns)]

    if p.get("zoom_anim"):
        # Static center crop, scale slowly grows from 1.04 → 1.10
        return (
            f"scale=w='{W}*(1.04+0.06*t/{dur_sec})':h='{H}*(1.04+0.06*t/{dur_sec})':eval=frame,"
            f"crop={W}:{H}:(in_w-{W})/2:(in_h-{H})/2,"
            f"setsar=1,fps={fps}"
        )

    return (
        f"scale={scaled_w}:{scaled_h},"
        f"crop={W}:{H}:'{p['x']}':'{p['y']}',"
        f"setsar=1,fps={fps}"
    )


def compose_mp4(slide_paths: List[Path], output_mp4: Path,
                music_path: Optional[Path] = None,
                slide_duration: float = SLIDE_DURATION_SEC) -> None:
    """
    Сшивает 5 PNG-слайдов в MP4 9:16 с Ken Burns zoom + crossfade transitions.
    """
    if not _check_ffmpeg():
        raise RuntimeError("ffmpeg не установлен. brew install ffmpeg")

    # Build complex filter for 5 slides
    inputs_args = []
    filter_parts = []

    for i, p in enumerate(slide_paths):
        # Each image looped → Ken Burns pan/scale → trim → format
        # Используем -t на input чтобы зацикливание не вышло за границы
        inputs_args += ["-loop", "1", "-t", str(slide_duration), "-i", str(p)]
        kb = _ken_burns_filter(i, slide_duration, SLIDE_FPS)
        filter_parts.append(
            f"[{i}:v]{kb},trim=duration={slide_duration},setpts=PTS-STARTPTS,format=yuv420p[v{i}]"
        )

    # Crossfade chain (0.4 sec overlap)
    XF = 0.4
    chain = "[v0]"
    cur_label = "v0"
    cumulative_offset = slide_duration - XF
    for i in range(1, len(slide_paths)):
        next_label = f"x{i}"
        filter_parts.append(
            f"[{cur_label}][v{i}]xfade=transition=fade:duration={XF}:offset={cumulative_offset}[{next_label}]"
        )
        cur_label = next_label
        cumulative_offset += slide_duration - XF

    filter_complex = ";".join(filter_parts)

    # Audio: optional bg music + fade in/out
    audio_args = []
    if music_path and music_path.exists():
        inputs_args += ["-i", str(music_path)]
        total_dur = len(slide_paths) * slide_duration - (len(slide_paths) - 1) * XF
        audio_filter = (
            f"[{len(slide_paths)}:a]volume=0.18,"
            f"afade=in:st=0:d=0.6,"
            f"afade=out:st={total_dur - 1.2}:d=1.2,"
            f"atrim=duration={total_dur}[a]"
        )
        filter_complex += ";" + audio_filter
        audio_args = ["-map", "[a]"]
    else:
        audio_args = []

    cmd = [
        "ffmpeg", "-y",
        *inputs_args,
        "-filter_complex", filter_complex,
        "-map", f"[{cur_label}]",
        *audio_args,
        "-c:v", "libx264",
        "-pix_fmt", "yuv420p",
        # veryfast vs medium: ~3-4x быстрее, ±5% bitrate (для 16-сек social video незаметно)
        "-preset", "veryfast",
        # crf 23 vs 21: ~10% меньше bitrate, визуально идентично на 1080p
        "-crf", "23",
        # Многопоток: используем все доступные ядра
        "-threads", "0",
        "-r", str(SLIDE_FPS),
        "-shortest",
        str(output_mp4),
    ]
    print(f"[ffmpeg] {' '.join(cmd[:8])}...", file=sys.stderr)
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        # Print last 30 lines of stderr to debug
        err_lines = result.stderr.splitlines()[-30:]
        print("\n".join(err_lines), file=sys.stderr)
        raise RuntimeError(f"ffmpeg failed with code {result.returncode}")
    print(f"  ✓ MP4 saved → {output_mp4}", file=sys.stderr)


# ───────────────────────────────────────────────────────────────────
# Main orchestration: unit/project/broker → captions → slides → mp4
# ───────────────────────────────────────────────────────────────────
def build_broker_reel(
    unit: Dict,
    project: Dict,
    broker: Dict,
    photo_paths: List[Path],
    output_mp4: Path,
    work_dir: Optional[Path] = None,
    music_path: Optional[Path] = None,
    use_ai_captions: bool = True,
    **kwargs,
) -> Dict:
    """
    End-to-end broker reel build.
    Returns metadata dict {script, slide_paths, mp4, duration_sec, generation_time_sec}.
    """
    if len(photo_paths) != 5:
        raise ValueError(f"photo_paths должен содержать ровно 5 фото, got {len(photo_paths)}")

    t_start = time.time()
    if work_dir is None:
        work_dir = Path(tempfile.mkdtemp(prefix="broker_reel_"))
    work_dir = Path(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)

    # Step 1: Generate captions via NVIDIA Llama (with fallback)
    print(f"[1/3] Generating captions...", file=sys.stderr)
    script = None
    if use_ai_captions:
        script = generate_reel_script(unit, project, broker)
    if script is None:
        print("  using fallback script", file=sys.stderr)
        script = fallback_reel_script(unit, project, broker)

    # Inject photo paths into script (matching by index/type order)
    for i, slide in enumerate(script["slides"]):
        slide["photo"] = str(photo_paths[i])

    # Step 2: Render 5 PNG slides
    print(f"[2/3] Rendering 5 slides...", file=sys.stderr)
    slides_dir = work_dir / "slides"
    slide_paths = render_deck(script["slides"], slides_dir)

    # Step 3: Compose MP4
    print(f"[3/3] Composing MP4 (ffmpeg)...", file=sys.stderr)
    compose_mp4(slide_paths, output_mp4, music_path=music_path)

    elapsed = time.time() - t_start
    total_dur = len(slide_paths) * SLIDE_DURATION_SEC - (len(slide_paths) - 1) * 0.4

    return {
        "script": script,
        "slide_paths": [str(p) for p in slide_paths],
        "mp4": str(output_mp4),
        "duration_sec": round(total_dur, 1),
        "generation_time_sec": round(elapsed, 1),
    }


# ───────────────────────────────────────────────────────────────────
# CLI
# ───────────────────────────────────────────────────────────────────
def _demo_run(use_ai: bool = True) -> None:
    """Smoke test with demo_photos/ + Sun Hills Lakeside data."""
    here = Path(__file__).resolve().parent
    photos = [
        here / "demo_photos" / "hero.webp",
        here / "demo_photos" / "interior.webp",
        here / "demo_photos" / "view.webp",
        here / "demo_photos" / "roi.webp",
        here / "demo_photos" / "cta.webp",
    ]
    for p in photos:
        if not p.exists():
            print(f"❌ Demo photo missing: {p}", file=sys.stderr)
            sys.exit(1)

    unit = {"price_rub": 12_500_000, "area_sqm": 65, "bedrooms": 2, "floor": "5/8", "type": "Apartment"}
    project = {
        "name": "Sun Hills Lakeside",
        "location": "Layan · Phuket",
        "city": "Phuket",
        "country": "Thailand",
        "completion_year": 2028,
        "roi_pct": 9.1,
        "installment_terms": "0% · 2 года",
        "description": "Премиум-комплекс из 6 корпусов в Layan, 5 минут до пляжа Bang Tao, гостиничный оператор Unicorn Hospitality, фрихолд.",
    }
    broker = {"name": "Любовь Стрельцова", "telegram_handle": "@ABG_MEDIA"}

    output = here / "demo_output.mp4"
    # 2026-05-26: тестируем разные треки. inspired_energy не подошёл — слишком
    # обычный для премиум-real-estate. Тестим elevator_uplifting + TODO найти
    # cinematic ambient (Bensound / Pixabay / Epidemic Sound).
    music = here.parent / "daily-poster" / "music" / "elevator_uplifting.mp3"
    if not music.exists():
        music = None
        print(f"⚠ Music not found: {music}", file=sys.stderr)
    else:
        print(f"♪ Using music: {music.name}", file=sys.stderr)

    result = build_broker_reel(
        unit=unit, project=project, broker=broker,
        photo_paths=photos, output_mp4=output,
        work_dir=here / "_work", use_ai_captions=use_ai,
        music_path=music,
    )

    print("\n" + "=" * 60)
    print(f"✓ DONE in {result['generation_time_sec']} sec")
    print(f"  Duration: {result['duration_sec']} sec")
    print(f"  Output:   {result['mp4']}")
    print(f"  Slides:   {len(result['slide_paths'])} PNGs in _work/slides/")
    print("=" * 60)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--demo", action="store_true", help="Run demo build with sample data")
    parser.add_argument("--no-ai", action="store_true", help="Skip NVIDIA Llama, use fallback captions only")
    args = parser.parse_args()

    if args.demo:
        _demo_run(use_ai=not args.no_ai)
    else:
        print("Usage: python3 orchestrator.py --demo")
        sys.exit(1)
