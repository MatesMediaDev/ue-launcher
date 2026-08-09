"""Discover Unreal Engine installations on disk."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from .config import Config

VERSION_RE = re.compile(r"UE[_\-]?(\d+)\.(\d+)(?:\.(\d+))?", re.IGNORECASE)


@dataclass(frozen=True)
class EngineInstall:
    path: Path
    version_label: str  # e.g. UE_5.6
    version_tuple: tuple[int, int, int]
    editor: Path
    editor_cmd: Path | None

    @property
    def name(self) -> str:
        return self.path.name

    @property
    def templates_dir(self) -> Path:
        return self.path / "Templates"

    def is_valid(self) -> bool:
        return self.editor.is_file() and os_access_exec(self.editor)


def os_access_exec(path: Path) -> bool:
    return path.exists() and os_x_ok(path)


def os_x_ok(path: Path) -> bool:
    import os

    return os.access(path, os.X_OK)


def _read_build_version(engine_root: Path) -> tuple[int, int, int] | None:
    build_file = engine_root / "Engine" / "Build" / "Build.version"
    if not build_file.is_file():
        return None
    try:
        data = json.loads(build_file.read_text(encoding="utf-8"))
        major = int(data.get("MajorVersion", 0))
        minor = int(data.get("MinorVersion", 0))
        patch = int(data.get("PatchVersion", 0))
        if major:
            return major, minor, patch
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    return None


def _label_from_tuple(ver: tuple[int, int, int]) -> str:
    major, minor, patch = ver
    if patch:
        return f"UE_{major}.{minor}.{patch}"
    return f"UE_{major}.{minor}"


def _guess_from_dirname(name: str) -> tuple[int, int, int] | None:
    match = VERSION_RE.search(name)
    if not match:
        return None
    major = int(match.group(1))
    minor = int(match.group(2))
    patch = int(match.group(3) or 0)
    return major, minor, patch


def detect_engine(path: Path) -> EngineInstall | None:
    path = path.expanduser().resolve()
    editor = path / "Engine" / "Binaries" / "Linux" / "UnrealEditor"
    if not editor.is_file():
        return None
    cmd = path / "Engine" / "Binaries" / "Linux" / "UnrealEditor-Cmd"
    ver = _read_build_version(path) or _guess_from_dirname(path.name) or (0, 0, 0)
    label = _label_from_tuple(ver) if ver != (0, 0, 0) else path.name
    # Prefer UE_Major.Minor (no patch) for association matching
    short_label = f"UE_{ver[0]}.{ver[1]}" if ver[0] else label
    install = EngineInstall(
        path=path,
        version_label=short_label,
        version_tuple=ver,
        editor=editor,
        editor_cmd=cmd if cmd.is_file() else None,
    )
    return install if install.is_valid() else None


def discover_engines(config: Config) -> list[EngineInstall]:
    found: dict[Path, EngineInstall] = {}

    # Explicit default path first
    if config.default_engine_path:
        eng = detect_engine(config.default_engine_path)
        if eng:
            found[eng.path] = eng

    for root in config.engine_roots:
        root = root.expanduser()
        if not root.is_dir():
            continue
        # Root itself might be an engine
        eng = detect_engine(root)
        if eng:
            found[eng.path] = eng
            continue
        try:
            children = sorted(root.iterdir())
        except OSError:
            continue
        for child in children:
            if not child.is_dir():
                continue
            eng = detect_engine(child)
            if eng:
                found[eng.path] = eng

    engines = list(found.values())
    engines.sort(key=lambda e: e.version_tuple, reverse=True)
    return engines


def pick_engine(engines: list[EngineInstall], preferred: str | None = None) -> EngineInstall | None:
    if not engines:
        return None
    if preferred:
        for eng in engines:
            if eng.version_label == preferred or eng.path.name == preferred:
                return eng
            if preferred.startswith("UE_") and eng.version_label.startswith(preferred):
                return eng
    return engines[0]
