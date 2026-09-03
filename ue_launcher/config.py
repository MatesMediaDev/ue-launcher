"""Persisted launcher settings (XDG config)."""

from __future__ import annotations

import json
import os
from copy import deepcopy
from pathlib import Path
from typing import Any

APP_ID = "mates-unreal-launcher"
APP_NAME = "Unreal Launcher"


def xdg_config_home() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))


def xdg_data_home() -> Path:
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))


def xdg_cache_home() -> Path:
    return Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))


CONFIG_DIR = xdg_config_home() / APP_ID
CONFIG_PATH = CONFIG_DIR / "config.json"
AUTH_PATH = CONFIG_DIR / "auth.json"
CACHE_DIR = xdg_cache_home() / APP_ID
ENGINE_INSTALL_DEFAULT = Path.home() / "UnrealEngine"
ENGINE_CACHE_DEFAULT = Path.home() / "Downloads" / "MatesUnrealLauncher"
PLUGIN_CACHE_DEFAULT = Path.home() / "Downloads" / "MatesUnrealLauncher" / "Plugins"

DEFAULT_CONFIG: dict[str, Any] = {
    "engine_roots": [str(Path.home() / "UnrealEngine")],
    "project_scan_roots": [
        str(Path.home() / "Documents"),
        str(Path.home() / "Projects"),
        str(Path.home() / "UnrealProjects"),
    ],
    "favorite_projects": [],
    "recent_projects": [],
    "engine_install_dir": str(ENGINE_INSTALL_DEFAULT),
    "engine_cache_dir": str(ENGINE_CACHE_DEFAULT),
    "plugin_cache_dir": str(PLUGIN_CACHE_DEFAULT),
    "keep_engine_zips": False,
    "preferred_engine": "UE_5.7",
    "default_engine_path": str(Path.home() / "UnrealEngine" / "UE_5.7.4"),
    # Empty = auto-detect (NVIDIA ICD if present, else RADV). Never force NVIDIA on Deck/AMD.
    "vulkan_icd": "",
    "prefer_x11": True,
    # UE 5.7 + Wayland: Slate tooltips are separate windows and eat the first click.
    "slate_disable_tooltips": True,
    # When tooltips stay on (slate_disable_tooltips false), delay before they appear (seconds).
    # Long delay (e.g. 4) often avoids the first-click steal while still allowing Blueprint pin help.
    "slate_tooltip_delay": 4.0,
    "slate_disable_notifications": True,
    # Auto-update (GitHub Releases AppImage).
    "check_updates_on_startup": True,
    "skipped_update_tag": "",
    "last_update_check": 0,
}


class Config:
    def __init__(self, data: dict[str, Any] | None = None) -> None:
        self._data = deepcopy(DEFAULT_CONFIG)
        if data:
            self._data.update(data)

    @classmethod
    def load(cls) -> Config:
        if not CONFIG_PATH.exists():
            cfg = cls()
            cfg.save()
            return cfg
        try:
            with CONFIG_PATH.open("r", encoding="utf-8") as fh:
                raw = json.load(fh)
            if not isinstance(raw, dict):
                return cls()
            return cls(raw)
        except (OSError, json.JSONDecodeError):
            return cls()

    def save(self) -> None:
        CONFIG_DIR.mkdir(parents=True, mode=0o700, exist_ok=True)
        tmp = CONFIG_PATH.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(self._data, fh, indent=2)
            fh.write("\n")
        tmp.chmod(0o600)
        tmp.replace(CONFIG_PATH)

    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self._data[key] = value

    @property
    def engine_roots(self) -> list[Path]:
        return [Path(p).expanduser() for p in self._data.get("engine_roots", [])]

    @property
    def project_scan_roots(self) -> list[Path]:
        return [Path(p).expanduser() for p in self._data.get("project_scan_roots", [])]

    @property
    def engine_install_dir(self) -> Path:
        return Path(self.get("engine_install_dir", ENGINE_INSTALL_DEFAULT)).expanduser()

    @property
    def engine_cache_dir(self) -> Path:
        return Path(self.get("engine_cache_dir", ENGINE_CACHE_DEFAULT)).expanduser()

    @property
    def plugin_cache_dir(self) -> Path:
        return Path(self.get("plugin_cache_dir", PLUGIN_CACHE_DEFAULT)).expanduser()

    @property
    def preferred_engine(self) -> str:
        return str(self.get("preferred_engine", "UE_5.7"))

    @property
    def default_engine_path(self) -> Path | None:
        raw = self.get("default_engine_path")
        return Path(raw).expanduser() if raw else None

    def push_recent_project(self, path: str, limit: int = 12) -> None:
        recent = [p for p in self._data.get("recent_projects", []) if p != path]
        recent.insert(0, path)
        self._data["recent_projects"] = recent[:limit]
