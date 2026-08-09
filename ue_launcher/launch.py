"""Launch UnrealEditor with a sanitized Linux environment."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from .config import Config
from .engines import EngineInstall


def clean_ld_library_path(value: str | None) -> str | None:
    """Strip Cursor/AppImage mounts that break Vulkan/NVIDIA for UE."""
    if not value:
        return None
    cleaned: list[str] = []
    for part in value.split(":"):
        if not part:
            continue
        lower = part.lower()
        if "/tmp/.mount_cursor" in lower or "/cursor/resources/" in lower:
            continue
        cleaned.append(part)
    return ":".join(cleaned) if cleaned else None


def build_env(config: Config, base: dict[str, str] | None = None) -> dict[str, str]:
    env = dict(base if base is not None else os.environ)
    cleaned = clean_ld_library_path(env.get("LD_LIBRARY_PATH"))
    if cleaned:
        env["LD_LIBRARY_PATH"] = cleaned
    else:
        env.pop("LD_LIBRARY_PATH", None)

    if config.get("prefer_x11", True):
        env.setdefault("QT_QPA_PLATFORM", "xcb")
        env.setdefault("SDL_VIDEODRIVER", "x11")

    vulkan_icd = config.get("vulkan_icd")
    if vulkan_icd and Path(vulkan_icd).exists():
        env["VK_ICD_FILENAMES"] = str(vulkan_icd)

    # Prefer discrete NVIDIA when present (harmless no-op otherwise)
    env.setdefault("__GLX_VENDOR_LIBRARY_NAME", "nvidia")
    env.setdefault("DRI_PRIME", "1")
    env.pop("VK_LAYER_PATH", None)
    return env


def launch_editor(
    engine: EngineInstall,
    config: Config,
    project: Path | None = None,
    extra_args: list[str] | None = None,
) -> subprocess.Popen:
    cmd = [str(engine.editor)]
    if project is not None:
        cmd.append(str(Path(project).expanduser().resolve()))
    if extra_args:
        cmd.extend(extra_args)

    env = build_env(config)
    # Detach from launcher so closing the UI doesn't kill the editor
    return subprocess.Popen(
        cmd,
        env=env,
        cwd=str(engine.path),
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def open_in_file_manager(path: Path) -> None:
    path = path.expanduser()
    if shutil.which("xdg-open"):
        subprocess.Popen(["xdg-open", str(path)], start_new_session=True)
