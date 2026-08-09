"""Download and cache Fab plugin thumbnails."""

from __future__ import annotations

import hashlib
import threading
from pathlib import Path

from .config import CACHE_DIR
from .epic.http_browser import http_get_bytes

THUMB_DIR = CACHE_DIR / "plugin_thumbs"
_lock = threading.Lock()


def thumb_cache_path(asset_id: str, url: str = "") -> Path:
    key = asset_id or url
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:20]
    suffix = ".jpg"
    lower = url.lower()
    if ".png" in lower:
        suffix = ".png"
    elif ".webp" in lower:
        suffix = ".webp"
    elif ".jpeg" in lower or ".jpg" in lower:
        suffix = ".jpg"
    return THUMB_DIR / f"{digest}{suffix}"


def cached_thumbnail(asset_id: str, url: str) -> Path | None:
    if not url:
        return None
    path = thumb_cache_path(asset_id, url)
    if path.is_file() and path.stat().st_size > 64:
        return path
    return None


def fetch_thumbnail(asset_id: str, url: str, *, timeout: float = 20.0) -> Path | None:
    """Return a local path for the thumbnail, downloading when needed."""
    if not url:
        return None
    existing = cached_thumbnail(asset_id, url)
    if existing is not None:
        return existing

    path = thumb_cache_path(asset_id, url)
    with _lock:
        if path.is_file() and path.stat().st_size > 64:
            return path
        THUMB_DIR.mkdir(parents=True, exist_ok=True)
        try:
            content = http_get_bytes(
                url,
                timeout=timeout,
                headers={"Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8"},
            )
            if not content:
                return None
            # Sniff real type — Fab URLs often lie / omit extensions.
            if content[:8] == b"\x89PNG\r\n\x1a\n":
                path = path.with_suffix(".png")
            elif content[:2] == b"\xff\xd8":
                path = path.with_suffix(".jpg")
            elif content[:4] == b"RIFF" and content[8:12] == b"WEBP":
                path = path.with_suffix(".webp")
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_bytes(content)
            tmp.replace(path)
            return path
        except OSError:
            return None
