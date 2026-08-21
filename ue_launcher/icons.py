"""Bundled Lucide icons exposed as GTK icon names.

Icons are from Lucide (https://lucide.dev), ISC license.
Runtime icons are pre-rendered PNGs (AppImage has no SVG pixbuf loader).
Source SVGs live under assets/icons-src/ for regeneration.
"""

from __future__ import annotations

from pathlib import Path

ICONS_DIR = Path(__file__).resolve().parent / "assets" / "icons"
HICOLOR_DIR = ICONS_DIR / "hicolor"

# Lucide-backed icon names.
REFRESH = "mates-refresh"
PLAY = "mates-play"
LAUNCH = "mates-launch"
FOLDER = "mates-folder"
FOLDER_OPEN = "mates-folder-open"
DOWNLOAD = "mates-download"
PLUS = "mates-plus"
GIT = "mates-git"
GIT_FOLDER = "mates-git-folder"
ACCOUNT = "mates-account"
SETTINGS = "mates-settings"
ENGINES = "mates-engines"
LIBRARY = "mates-library"
SAVE = "mates-save"
CHECK = "mates-check"
BUILD = "mates-build"
DISK = "mates-disk"
PLUGIN = "mates-plugin"
EXTERNAL = "mates-external"
SEARCH = "mates-search"
LOGIN = "mates-login"
LOGOUT = "mates-logout"
BOXES = "mates-boxes"
TRASH = "mates-trash"


def png_path(name: str, size: int = 16) -> Path | None:
    """Best-size PNG for a logical icon size (falls back to nearest baked size)."""
    for px in (size, 24, 32, 16):
        path = HICOLOR_DIR / f"{px}x{px}" / "actions" / f"{name}.png"
        if path.is_file():
            return path
    return None
