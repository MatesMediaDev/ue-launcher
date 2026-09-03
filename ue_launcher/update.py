"""Check GitHub Releases and self-update the AppImage."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from urllib.request import Request, urlopen

from . import __version__

GITHUB_REPO = "MatesMediaDev/ue-launcher"
RELEASES_API = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
RELEASES_PAGE = f"https://github.com/{GITHUB_REPO}/releases"
APPIMAGE_ASSET = "Unreal_Launcher-x86_64.AppImage"
USER_AGENT = f"mates-unreal-launcher/{__version__}"

ProgressCb = Callable[[str, int | None], None]


@dataclass(frozen=True)
class ReleaseInfo:
    tag: str
    version: str
    name: str
    body: str
    html_url: str
    appimage_url: str | None
    appimage_size: int | None


def running_appimage() -> Path | None:
    """Path to *this* AppImage when launched via the AppImage runtime.

    Ignores inherited ``APPIMAGE`` from a host IDE (e.g. Cursor) so source /
    menu launches do not try to overwrite the wrong file.
    """
    raw = os.environ.get("APPIMAGE", "").strip()
    if not raw:
        return None
    path = Path(raw)
    if not path.is_file():
        return None
    name = path.name.lower()
    if "unreal" not in name and "mates" not in name:
        return None
    return path


def parse_version(text: str) -> tuple[int, ...]:
    cleaned = text.strip().lstrip("vV")
    parts = re.findall(r"\d+", cleaned)
    if not parts:
        return (0,)
    return tuple(int(p) for p in parts)


def is_newer(candidate: str, current: str = __version__) -> bool:
    return parse_version(candidate) > parse_version(current)


def fetch_latest_release(timeout: float = 20.0) -> ReleaseInfo:
    """Fetch the latest GitHub release metadata (network)."""
    import json

    req = Request(
        RELEASES_API,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": USER_AGENT,
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    with urlopen(req, timeout=timeout) as resp:  # noqa: S310 — fixed GitHub URL
        payload = json.loads(resp.read().decode("utf-8"))

    tag = str(payload.get("tag_name") or "")
    version = tag.lstrip("vV")
    assets = payload.get("assets") or []
    appimage_url: str | None = None
    appimage_size: int | None = None
    for asset in assets:
        if not isinstance(asset, dict):
            continue
        name = str(asset.get("name") or "")
        if name == APPIMAGE_ASSET or name.endswith(".AppImage"):
            appimage_url = str(asset.get("browser_download_url") or "") or None
            size = asset.get("size")
            appimage_size = int(size) if isinstance(size, int) else None
            if name == APPIMAGE_ASSET:
                break

    return ReleaseInfo(
        tag=tag,
        version=version,
        name=str(payload.get("name") or tag),
        body=str(payload.get("body") or ""),
        html_url=str(payload.get("html_url") or RELEASES_PAGE),
        appimage_url=appimage_url,
        appimage_size=appimage_size,
    )


def download_appimage(
    url: str,
    dest: Path,
    *,
    expected_size: int | None = None,
    progress: ProgressCb | None = None,
    timeout: float = 120.0,
) -> Path:
    """Download an AppImage to ``dest`` atomically (via a sibling .partial file)."""
    dest = dest.expanduser().resolve()
    dest.parent.mkdir(parents=True, exist_ok=True)
    partial = dest.with_name(dest.name + ".partial")
    if partial.exists():
        partial.unlink()

    req = Request(
        url,
        headers={"User-Agent": USER_AGENT, "Accept": "application/octet-stream"},
    )
    with urlopen(req, timeout=timeout) as resp:  # noqa: S310 — GitHub release asset URL
        total = expected_size
        raw_len = resp.headers.get("Content-Length")
        if raw_len and raw_len.isdigit():
            total = int(raw_len)
        written = 0
        with partial.open("wb") as out:
            while True:
                chunk = resp.read(1024 * 256)
                if not chunk:
                    break
                out.write(chunk)
                written += len(chunk)
                if progress is not None:
                    pct = int(written * 100 / total) if total else None
                    progress(f"Downloading update… {written // (1024 * 1024)} MiB", pct)

    partial.chmod(0o755)
    # Replace in place; keep a one-shot backup next to the AppImage.
    backup = dest.with_name(dest.name + ".bak")
    if dest.exists():
        try:
            if backup.exists():
                backup.unlink()
            dest.replace(backup)
        except OSError:
            # Fall back to overwrite without backup (e.g. read-only parent quirks).
            pass
    partial.replace(dest)
    if progress is not None:
        progress("Update installed — relaunch the AppImage", 100)
    return dest


def download_to_cache(
    url: str,
    *,
    cache_dir: Path,
    expected_size: int | None = None,
    progress: ProgressCb | None = None,
) -> Path:
    """Download AppImage into cache (for non-AppImage / source installs)."""
    cache_dir = cache_dir.expanduser().resolve()
    cache_dir.mkdir(parents=True, exist_ok=True)
    dest = cache_dir / APPIMAGE_ASSET
    return download_appimage(
        url, dest, expected_size=expected_size, progress=progress
    )
