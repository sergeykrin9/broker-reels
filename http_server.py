"""
http_server.py — Flask endpoint /generate-reel для broker reels pipeline.

Деплоится на сервер 213.171.15.45 как systemd service broker-reels.service
(см. deploy/broker-reels.service).

Endpoints:
  GET  /health                — health check
  POST /generate-reel         — sync generation (waits ~30-60s, returns URL)
  POST /generate-reel/async   — async (returns jobId, polls /jobs/{id})  TODO v2
  GET  /jobs/{id}             — job status                                TODO v2

Auth: shared-secret header `X-Broker-Reels-Token` matches env BROKER_REELS_TOKEN.
В Cloud Function токен берётся из Firebase Secret Manager.

Sync flow (~30-60 sec wall time):
  1. Validate auth token
  2. Validate payload (unitId, photos[5], unit/project/broker dicts)
  3. Download 5 photos to /tmp/{jobId}/src/
  4. Run orchestrator.build_broker_reel() → /tmp/{jobId}/reel.mp4
  5. Upload to Firebase Storage broker_reels/{unitId}_{timestamp}.mp4
  6. Return {ok: true, reelUrl, durationSec, generationTimeSec}

Limits (server-side guards):
  - max body size: 20 KB (only URLs + metadata, no binary)
  - photos[] length: exactly 5 (composer requires)
  - rate limit: 10 req/min per IP (basic)
"""
from __future__ import annotations
import os
import shutil
import sys
import tempfile
import time
import uuid
from pathlib import Path

from flask import Flask, jsonify, request

sys.path.insert(0, str(Path(__file__).resolve().parent))
from orchestrator import build_broker_reel
from firebase_client import download_photos, upload_reel


app = Flask(__name__)
SHARED_TOKEN = os.environ.get("BROKER_REELS_TOKEN", "")
WORK_BASE = Path(os.environ.get("BROKER_REELS_WORK_DIR", "/tmp/broker_reels"))
WORK_BASE.mkdir(parents=True, exist_ok=True)

# Basic in-memory rate limiter: ip → (count, window_start)
_RATE: dict[str, tuple[int, float]] = {}
RATE_LIMIT_PER_MIN = 10


def _check_rate(ip: str) -> bool:
    now = time.time()
    count, window = _RATE.get(ip, (0, now))
    if now - window > 60:
        _RATE[ip] = (1, now)
        return True
    if count >= RATE_LIMIT_PER_MIN:
        return False
    _RATE[ip] = (count + 1, window)
    return True


def _client_ip() -> str:
    fwd = request.headers.get("X-Forwarded-For", "")
    return fwd.split(",")[0].strip() if fwd else (request.remote_addr or "0.0.0.0")


# ───────────────────────────────────────────────────────────────────
# Endpoints
# ───────────────────────────────────────────────────────────────────
@app.route("/health", methods=["GET"])
def health():
    return jsonify({"ok": True, "service": "broker-reels", "ts": int(time.time())})


@app.route("/generate-reel", methods=["POST"])
def generate_reel():
    # 1. Auth
    if not SHARED_TOKEN:
        return jsonify({"error": "server_unconfigured", "detail": "BROKER_REELS_TOKEN not set"}), 500
    token = request.headers.get("X-Broker-Reels-Token", "")
    if token != SHARED_TOKEN:
        return jsonify({"error": "unauthorized"}), 401

    # 2. Rate limit
    ip = _client_ip()
    if not _check_rate(ip):
        return jsonify({"error": "rate_limited", "detail": f"max {RATE_LIMIT_PER_MIN} req/min"}), 429

    # 3. Validate payload
    try:
        data = request.get_json(force=True, silent=False) or {}
    except Exception as e:
        return jsonify({"error": "bad_json", "detail": str(e)}), 400

    unit_id = data.get("unitId") or ""
    photos = data.get("photos") or []
    unit = data.get("unit") or {}
    project = data.get("project") or {}
    broker = data.get("broker") or {}
    use_ai = bool(data.get("useAi", True))

    if not unit_id:
        return jsonify({"error": "missing_unitId"}), 400
    if not isinstance(photos, list) or len(photos) != 5:
        return jsonify({"error": "photos_must_be_5"}), 400
    if not isinstance(unit, dict) or not isinstance(project, dict) or not isinstance(broker, dict):
        return jsonify({"error": "unit/project/broker must be objects"}), 400

    # 4. Generate (sync, ~30-60 sec wall time)
    job_id = uuid.uuid4().hex[:12]
    job_dir = WORK_BASE / job_id
    src_dir = job_dir / "src"
    out_mp4 = job_dir / "reel.mp4"

    t_total_start = time.time()
    try:
        # 4a. Download photos
        print(f"[{job_id}] downloading 5 photos...", file=sys.stderr)
        photo_paths = download_photos(photos, src_dir)

        # 4a.1 — Если у брокера есть photo_url, грузим как 6-е фото и
        # подменяем photos[4] (CTA slide). Если фото не загрузилось — fallback
        # на исходную CTA-картинку (фото проекта с blur).
        broker_photo_url = (broker or {}).get("photo_url") or (broker or {}).get("photoURL")
        if broker_photo_url:
            try:
                broker_photos = download_photos([broker_photo_url], src_dir / "broker")
                if broker_photos and broker_photos[0].exists():
                    photo_paths[4] = broker_photos[0]
                    print(f"[{job_id}] broker photo used for CTA slide", file=sys.stderr)
            except Exception as e:
                print(f"[{job_id}] broker photo download failed: {e}", file=sys.stderr)

        # 4b. Build reel
        print(f"[{job_id}] building reel...", file=sys.stderr)
        # Music: рандомный выбор из всех mp3 в ./music/ (или daily-poster/music/).
        # Детерминированный seed = unit_id → один и тот же юнит всегда получит
        # тот же трек (consistency для clients), но разные юниты получают
        # разную музыку (variety в feed).
        # Опционально: если файл в music/ начинается с "_" — он priority
        # (используется для специальных слайдов или fallback).
        music_path = None
        here = Path(__file__).resolve().parent
        for base in (here / "music", here.parent / "daily-poster" / "music"):
            if not base.exists():
                continue
            tracks = sorted([p for p in base.glob("*.mp3") if p.is_file()])
            if not tracks:
                continue
            # Детерминированный pick: hash(unit_id) % len(tracks)
            import hashlib
            seed_bytes = hashlib.md5(unit_id.encode("utf-8")).digest()
            idx = int.from_bytes(seed_bytes[:4], "big") % len(tracks)
            music_path = tracks[idx]
            print(f"[{job_id}] music: {music_path.name} (of {len(tracks)} tracks)", file=sys.stderr)
            break

        result = build_broker_reel(
            unit=unit, project=project, broker=broker,
            photo_paths=photo_paths, output_mp4=out_mp4,
            work_dir=job_dir / "_work",
            music_path=music_path,
            use_ai_captions=use_ai,
        )

        # 4c. Upload to Firebase Storage
        ts = int(time.time())
        storage_path = f"broker_reels/{unit_id}_{ts}.mp4"
        reel_url = upload_reel(out_mp4, storage_path, public=True)

        elapsed = time.time() - t_total_start
        return jsonify({
            "ok": True,
            "jobId": job_id,
            "unitId": unit_id,
            "reelUrl": reel_url,
            "storagePath": storage_path,
            "durationSec": result["duration_sec"],
            "generationTimeSec": round(elapsed, 1),
            "captionsAi": use_ai,
        })

    except Exception as e:
        print(f"[{job_id}] FAILED: {type(e).__name__}: {e}", file=sys.stderr)
        return jsonify({
            "ok": False,
            "jobId": job_id,
            "error": "generation_failed",
            "detail": f"{type(e).__name__}: {e}"[:300],
        }), 500
    finally:
        # 5. Cleanup tmp files (keep MP4 for debug if KEEP_TMP=1)
        if not os.environ.get("KEEP_TMP"):
            try:
                shutil.rmtree(job_dir, ignore_errors=True)
            except Exception:
                pass


# ───────────────────────────────────────────────────────────────────
# Entry point
# ───────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5050))
    host = os.environ.get("HOST", "0.0.0.0")
    print(f"[broker-reels] listening on {host}:{port}", file=sys.stderr)
    print(f"[broker-reels] BROKER_REELS_TOKEN: {'SET' if SHARED_TOKEN else 'NOT SET (will 500)'}", file=sys.stderr)
    app.run(host=host, port=port, threaded=True)
