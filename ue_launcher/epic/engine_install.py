"""Download + extract official Linux Unreal Engine zip builds."""

from __future__ import annotations

import os
import shutil
import zipfile
from pathlib import Path
from typing import Callable

import requests

from .cosmos import EngineBlob

ProgressCb = Callable[[str, int, int], None]


class EngineInstallError(Exception):
    pass


def download_engine_zip(
    blob: EngineBlob,
    dest_zip: Path,
    *,
    progress: ProgressCb | None = None,
    chunk_size: int = 1024 * 1024,
) -> Path:
    dest_zip = dest_zip.expanduser()
    dest_zip.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest_zip.with_suffix(dest_zip.suffix + ".partial")

    existing = tmp.stat().st_size if tmp.exists() else 0
    headers: dict[str, str] = {}
    if existing > 0:
        headers["Range"] = f"bytes={existing}-"

    with requests.get(blob.url, stream=True, headers=headers, timeout=120) as response:
        if response.status_code == 416:
            if tmp.exists():
                tmp.replace(dest_zip)
            return dest_zip
        if response.status_code not in (200, 206):
            raise EngineInstallError(
                f"Download failed for {blob.name}: HTTP {response.status_code}"
            )

        total = blob.size
        content_range = response.headers.get("Content-Range")
        if content_range and "/" in content_range:
            try:
                total = int(content_range.rsplit("/", 1)[1])
            except ValueError:
                pass
        elif response.headers.get("Content-Length") and response.status_code == 200:
            total = int(response.headers["Content-Length"])
            existing = 0

        mode = "ab" if response.status_code == 206 and existing else "wb"
        if mode == "wb":
            existing = 0

        done = existing
        with tmp.open(mode) as fh:
            for chunk in response.iter_content(chunk_size=chunk_size):
                if not chunk:
                    continue
                fh.write(chunk)
                done += len(chunk)
                if progress:
                    progress(f"Downloading {blob.name}", done, total or blob.size)

    if blob.size > 0 and abs(tmp.stat().st_size - blob.size) > 1024:
        raise EngineInstallError(
            f"Size mismatch for {blob.name}: got {tmp.stat().st_size}, expected {blob.size}"
        )

    tmp.replace(dest_zip)
    if progress:
        size = dest_zip.stat().st_size
        progress(f"Download complete: {blob.name}", size, size)
    return dest_zip


def _apply_zip_permissions(info: zipfile.ZipInfo, dest_root: Path) -> None:
    """Restore Unix mode bits that ZipFile.extract() often drops."""
    # external_attr high 16 bits = Unix mode when created on Unix
    mode = (info.external_attr >> 16) & 0o7777
    if not mode:
        return
    path = dest_root / info.filename
    if path.exists() and not path.is_symlink():
        try:
            path.chmod(mode)
        except OSError:
            pass


def _ensure_linux_binaries_executable(engine_root: Path) -> None:
    binaries = engine_root / "Engine" / "Binaries"
    if binaries.is_dir():
        skip_suffixes = {
            ".debug",
            ".sym",
            ".target",
            ".modules",
            ".version",
            ".dll",
            ".pdb",
            ".so",
            ".a",
            ".txt",
            ".ini",
            ".json",
            ".xml",
            ".png",
            ".jpg",
        }
        for path in binaries.rglob("*"):
            if not path.is_file() or path.is_symlink():
                continue
            if path.suffix.lower() in skip_suffixes:
                continue
            # Mark extensionless binaries and shell scripts executable
            if path.suffix == "" or path.suffix == ".sh":
                mode = path.stat().st_mode
                path.chmod(mode | 0o111)

    # Zip extract often strips +x from the bundled clang toolchain — without this,
    # marketplace plugin compiles fail with "Permission denied" on clang++.
    sdk_host = engine_root / "Engine" / "Extras" / "ThirdPartyNotUE" / "SDKs" / "HostLinux"
    if sdk_host.is_dir():
        for path in sdk_host.rglob("*"):
            if not path.is_file() or path.is_symlink():
                continue
            if "/bin/" in path.as_posix() or path.suffix == ".sh" or "/libexec/" in path.as_posix():
                try:
                    path.chmod(path.stat().st_mode | 0o111)
                except OSError:
                    pass


def extract_engine_zip(
    zip_path: Path,
    install_root: Path,
    dirname: str,
    *,
    progress: ProgressCb | None = None,
) -> Path:
    zip_path = zip_path.expanduser()
    install_root = install_root.expanduser()
    install_root.mkdir(parents=True, exist_ok=True)
    target = install_root / dirname
    if target.exists():
        raise EngineInstallError(f"Install directory already exists: {target}")

    staging = install_root / f".staging_{dirname}"
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)

    try:
        with zipfile.ZipFile(zip_path) as zf:
            members = zf.infolist()
            total = len(members) or 1
            for i, info in enumerate(members, start=1):
                zf.extract(info, path=staging)
                _apply_zip_permissions(info, staging)
                if progress and (i % 50 == 0 or i == total):
                    progress(f"Extracting {dirname}", i, total)

        children = [p for p in staging.iterdir()]
        if len(children) == 1 and children[0].is_dir():
            children[0].rename(target)
            shutil.rmtree(staging, ignore_errors=True)
        else:
            staging.rename(target)

        _ensure_linux_binaries_executable(target)

        editor = target / "Engine" / "Binaries" / "Linux" / "UnrealEditor"
        if not editor.is_file():
            raise EngineInstallError(f"Extracted tree missing UnrealEditor at {editor}")
        if not os.access(editor, os.X_OK):
            editor.chmod(editor.stat().st_mode | 0o111)
    except Exception:
        if target.exists():
            shutil.rmtree(target, ignore_errors=True)
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        raise

    if progress:
        progress(f"Installed {dirname}", 1, 1)
    return target


def install_engine(
    blob: EngineBlob,
    *,
    install_root: Path,
    cache_dir: Path,
    keep_zip: bool = False,
    progress: ProgressCb | None = None,
) -> Path:
    cache_dir = cache_dir.expanduser()
    cache_dir.mkdir(parents=True, exist_ok=True)
    zip_path = cache_dir / blob.name
    if not zip_path.exists() or (blob.size and zip_path.stat().st_size != blob.size):
        download_engine_zip(blob, zip_path, progress=progress)
    target = extract_engine_zip(zip_path, install_root, blob.install_dirname, progress=progress)
    if not keep_zip:
        try:
            zip_path.unlink(missing_ok=True)
        except OSError:
            pass
    return target
