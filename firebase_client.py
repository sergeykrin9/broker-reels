"""
firebase_client.py — Firebase Admin SDK для download/upload media.

Используется http_server.py (Flask) на сервере 213.171.15.45.
Авторизация через service account JSON, путь в env BROKER_REELS_SA_KEY
(default: ./firebase_sa.json).

Functions:
  - download_photos(urls, dest_dir) → list of local Path (parallel via ThreadPool)
  - upload_reel(local_mp4, storage_path) → public download URL
  - get_default_bucket() → Bucket object

Setup (one-time, на сервере):
  1. Скачать service account JSON в Firebase Console:
     Settings → Service Accounts → Generate new private key
  2. Положить в /opt/broker-reels/firebase_sa.json (или путь в BROKER_REELS_SA_KEY)
  3. chmod 600 firebase_sa.json
"""
from __future__ import annotations
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import List, Optional
from urllib.parse import urlparse, unquote

import requests

try:
    import firebase_admin
    from firebase_admin import credentials, storage
    _ADMIN_AVAILABLE = True
except ImportError:
    _ADMIN_AVAILABLE = False
    firebase_admin = None
    credentials = None
    storage = None


# Firebase Storage bucket — derived from project_id
# В нашем случае: axonleads-app.firebasestorage.app (или .appspot.com legacy)
DEFAULT_BUCKET = os.environ.get("FIREBASE_STORAGE_BUCKET", "axonleads-app.firebasestorage.app")

_initialized = False


def _init_admin() -> None:
    """Lazy-init firebase_admin. Idempotent."""
    global _initialized
    if _initialized:
        return
    if not _ADMIN_AVAILABLE:
        raise RuntimeError(
            "firebase-admin not installed. pip install firebase-admin"
        )

    sa_key_path = os.environ.get("BROKER_REELS_SA_KEY", "firebase_sa.json")
    if not Path(sa_key_path).exists():
        raise RuntimeError(
            f"Service account key not found at {sa_key_path}. "
            "Set BROKER_REELS_SA_KEY env var or place firebase_sa.json in cwd."
        )

    cred = credentials.Certificate(sa_key_path)
    firebase_admin.initialize_app(cred, {"storageBucket": DEFAULT_BUCKET})
    _initialized = True
    print(f"[firebase] ✓ initialized with bucket={DEFAULT_BUCKET}", file=sys.stderr)


def get_default_bucket():
    """Return the default Storage bucket. Lazy init."""
    _init_admin()
    return storage.bucket()


# ───────────────────────────────────────────────────────────────────
# Download photos by HTTPS URL → local dir
# ───────────────────────────────────────────────────────────────────
def _download_one(url: str, dest: Path, timeout: int = 30) -> Path:
    """Download single URL to dest path. Returns dest."""
    r = requests.get(url, stream=True, timeout=timeout)
    r.raise_for_status()
    with open(dest, "wb") as f:
        for chunk in r.iter_content(chunk_size=64 * 1024):
            f.write(chunk)
    return dest


def download_photos(urls: List[str], dest_dir: Path, max_workers: int = 5) -> List[Path]:
    """
    Параллельно скачивает 5 фото из Firebase Storage по URL.
    URLs могут быть signed-URL (с alt=media&token=) или firebase-проксированные.
    Возвращает list of local Path в порядке входных URL.
    """
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)

    # Sniff extension from URL or fallback to .jpg
    def _ext_for(url: str, idx: int) -> str:
        # URLs могут быть `.../path/file.jpg?alt=media&token=...`
        path = urlparse(url).path
        for ext in (".jpg", ".jpeg", ".webp", ".png"):
            if path.lower().endswith(ext):
                return ext
        return ".jpg"

    paths = [None] * len(urls)
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {
            pool.submit(_download_one, url, dest_dir / f"src_{i + 1:02d}{_ext_for(url, i)}"): i
            for i, url in enumerate(urls)
        }
        for fut in as_completed(futures):
            i = futures[fut]
            try:
                paths[i] = fut.result()
                print(f"  [dl] ✓ photo {i + 1}", file=sys.stderr)
            except Exception as e:
                print(f"  [dl] ✗ photo {i + 1}: {e}", file=sys.stderr)
                raise
    return paths


# ───────────────────────────────────────────────────────────────────
# Upload MP4 → Firebase Storage, return public URL
# ───────────────────────────────────────────────────────────────────
def upload_reel(local_mp4: Path, storage_path: str, public: bool = True) -> str:
    """
    Загружает MP4 в Firebase Storage по пути storage_path
    (например: 'broker_reels/{unitId}_{timestamp}.mp4').

    Если public=True — генерирует public download URL.
    Иначе — signed URL валидный 24 часа.

    Returns: download URL.
    """
    bucket = get_default_bucket()
    blob = bucket.blob(storage_path)

    print(f"[upload] → {storage_path}...", file=sys.stderr)
    blob.upload_from_filename(
        str(local_mp4),
        content_type="video/mp4",
    )
    blob.cache_control = "public, max-age=86400"
    blob.patch()

    if public:
        blob.make_public()
        url = blob.public_url
    else:
        from datetime import timedelta
        url = blob.generate_signed_url(expiration=timedelta(hours=24), method="GET")

    print(f"[upload] ✓ {url}", file=sys.stderr)
    return url


# ───────────────────────────────────────────────────────────────────
# CLI smoke test
# ───────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python3 firebase_client.py <test_url> [<out_dir>]")
        print("       python3 firebase_client.py --upload <local_mp4> <storage_path>")
        sys.exit(1)

    if sys.argv[1] == "--upload":
        url = upload_reel(Path(sys.argv[2]), sys.argv[3])
        print(f"Uploaded: {url}")
    else:
        out = Path(sys.argv[2]) if len(sys.argv) > 2 else Path("_dl_test")
        paths = download_photos([sys.argv[1]], out)
        for p in paths:
            print(f"  ✓ {p} ({p.stat().st_size:,} bytes)")
