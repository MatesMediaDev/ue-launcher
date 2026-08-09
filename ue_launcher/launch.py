"""Launch UnrealEditor with a sanitized Linux environment."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from .config import Config
from .engines import EngineInstall

# ICDs we may auto-pick when present
_NVIDIA_ICD_CANDIDATES = (
    "/usr/share/vulkan/icd.d/nvidia_icd.json",
    "/usr/share/vulkan/icd.d/nvidia_icd.x86_64.json",
    "/etc/vulkan/icd.d/nvidia_icd.json",
)
_RADV_ICD_CANDIDATES = (
    "/usr/share/vulkan/icd.d/radeon_icd.x86_64.json",
    "/usr/share/vulkan/icd.d/radeon_icd.json",
    "/usr/lib/x86_64-linux-gnu/GL/vulkan/icd.d/radeon_icd.x86_64.json",
)


def _is_junk_lib_path(part: str) -> bool:
    lower = part.lower()
    markers = (
        "/tmp/.mount_cursor",
        "/cursor/resources/",
        "/tmp/.mount_",  # AppImage FUSE mounts
        "appimage_extracted",
        "unreallauncher.appdir",
        "/squashfs-root/",
    )
    return any(m in lower for m in markers)


def clean_ld_library_path(value: str | None) -> str | None:
    """Strip Cursor/AppImage mounts that break host Vulkan/Mesa for UE."""
    if not value:
        return None
    cleaned: list[str] = []
    for part in value.split(":"):
        if not part or _is_junk_lib_path(part):
            continue
        cleaned.append(part)
    return ":".join(cleaned) if cleaned else None


def _first_existing(paths: tuple[str, ...]) -> Path | None:
    for raw in paths:
        path = Path(raw)
        if path.is_file():
            return path
    return None


def _detect_vulkan_icd(configured: str | None) -> Path | None:
    """Resolve ICD: explicit config if valid, else NVIDIA if present, else RADV."""
    if configured:
        path = Path(str(configured)).expanduser()
        if path.is_file():
            return path
    nvidia = _first_existing(_NVIDIA_ICD_CANDIDATES)
    if nvidia is not None:
        return nvidia
    return _first_existing(_RADV_ICD_CANDIDATES)


def build_env(config: Config, base: dict[str, str] | None = None) -> dict[str, str]:
    env = dict(base if base is not None else os.environ)

    # Editor must use the host GPU stack — never inherit AppImage lib paths.
    env.pop("LD_LIBRARY_PATH", None)
    env.pop("PYTHONPATH", None)
    env.pop("PYTHONHOME", None)
    env.pop("GI_TYPELIB_PATH", None)
    env.pop("GSETTINGS_SCHEMA_DIR", None)
    env.pop("GDK_PIXBUF_MODULEDIR", None)
    env.pop("GDK_PIXBUF_MODULE_FILE", None)
    env.pop("APPDIR", None)
    env.pop("APPIMAGE", None)
    env.pop("OWD", None)
    env.pop("ARGV0", None)

    if config.get("prefer_x11", True):
        env.setdefault("QT_QPA_PLATFORM", "xcb")
        env.setdefault("SDL_VIDEODRIVER", "x11")

    # Drop any inherited ICD / layer overrides from a bad shell or launcher.
    env.pop("VK_ICD_FILENAMES", None)
    env.pop("VK_DRIVER_FILES", None)
    env.pop("VK_LAYER_PATH", None)
    env.pop("__GLX_VENDOR_LIBRARY_NAME", None)
    env.pop("DRI_PRIME", None)

    configured = config.get("vulkan_icd") or ""
    # Empty string / old NVIDIA default that isn't on this machine → auto-detect
    icd = _detect_vulkan_icd(str(configured) if configured else None)
    if icd is not None:
        env["VK_ICD_FILENAMES"] = str(icd)
        if "nvidia" in icd.name.lower():
            env["__GLX_VENDOR_LIBRARY_NAME"] = "nvidia"
            env.setdefault("DRI_PRIME", "1")

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
