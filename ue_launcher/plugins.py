"""Discover and install Unreal Engine plugins / Fab projects."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .engines import EngineInstall
from .epic.download import ProgressCb, download_asset
from .epic.fab import FabAsset, FabError, prepare_download
from .projects import UProject

PLUGIN_METHOD_HINTS = ("plugin", "code_plugin", "code plugin", "engine plugin")


@dataclass(frozen=True)
class InstalledPlugin:
    name: str
    path: Path
    enabled_by_default: bool
    friendly_name: str
    description: str
    engine: EngineInstall


def asset_kind(asset: FabAsset) -> str:
    method = (asset.distribution_method or "").strip().upper()
    if "PLUGIN" in method:
        return "plugin"
    if method in ("COMPLETE_PROJECT", "PROJECT"):
        return "project"
    if method == "ENGINE":
        return "engine"
    return "content"


def is_plugin_asset(asset: FabAsset) -> bool:
    method = (asset.distribution_method or "").strip().lower().replace("-", "_")
    if "plugin" in method:
        return True
    title = asset.title.lower()
    return title.endswith(" plugin")


def is_project_asset(asset: FabAsset) -> bool:
    return asset_kind(asset) == "project"


def filter_plugin_assets(assets: list[FabAsset]) -> list[FabAsset]:
    """Owned code plugins + complete projects (installable from this tab)."""
    wanted_titles = (
        "blueprint assist",
        "auto size comments",
        "autosizecomments",
        "node graph assistant",
    )
    out: list[FabAsset] = []
    seen: set[str] = set()
    for asset in assets:
        include = is_plugin_asset(asset) or is_project_asset(asset)
        if not include and any(w in asset.title.lower() for w in wanted_titles):
            include = True
        if not include:
            continue
        key = asset.asset_id or asset.title
        if key in seen:
            continue
        seen.add(key)
        out.append(asset)
    out.sort(key=lambda a: (asset_kind(a) != "plugin", a.title.lower()))
    return out


def search_plugins(assets: list[FabAsset], query: str) -> list[FabAsset]:
    q = " ".join(query.lower().split())
    if not q:
        return list(assets)
    terms = q.split()
    hits: list[FabAsset] = []
    for asset in assets:
        hay = " ".join(
            [
                asset.title.lower(),
                (asset.distribution_method or "").lower(),
                asset_kind(asset),
                " ".join(asset.engine_versions).lower(),
            ]
        )
        if all(term in hay for term in terms):
            hits.append(asset)
    return hits


def marketplace_plugins_dir(engine: EngineInstall) -> Path:
    return engine.path / "Engine" / "Plugins" / "Marketplace"


def project_plugins_dir(project: UProject) -> Path:
    return project.directory / "Plugins"


def discover_engine_plugins(engine: EngineInstall) -> list[InstalledPlugin]:
    roots = [
        engine.path / "Engine" / "Plugins" / "Marketplace",
        engine.path / "Engine" / "Plugins" / "Runtime",
        engine.path / "Engine" / "Plugins" / "Editor",
    ]
    found: list[InstalledPlugin] = []
    seen: set[Path] = set()
    for root in roots:
        if not root.is_dir():
            continue
        for uplugin in root.rglob("*.uplugin"):
            parent = uplugin.parent.resolve()
            if parent in seen:
                continue
            seen.add(parent)
            meta = _read_uplugin(uplugin)
            found.append(
                InstalledPlugin(
                    name=meta.get("FriendlyName") or uplugin.stem,
                    path=parent,
                    enabled_by_default=bool(meta.get("EnabledByDefault", False)),
                    friendly_name=str(meta.get("FriendlyName") or uplugin.stem),
                    description=str(meta.get("Description") or ""),
                    engine=engine,
                )
            )
    found.sort(key=lambda p: p.friendly_name.lower())
    return found


def _read_uplugin(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return {}


def _find_plugin_roots(download_dir: Path) -> list[Path]:
    """Locate folders that contain a .uplugin after a Fab extract."""
    roots: list[Path] = []
    for uplugin in download_dir.rglob("*.uplugin"):
        roots.append(uplugin.parent)
    roots.sort(key=lambda p: len(p.parts))
    unique: list[Path] = []
    for root in roots:
        if any(root == u or u in root.parents for u in unique):
            continue
        unique.append(root)
    return unique


def _find_uproject_roots(download_dir: Path) -> list[Path]:
    roots: list[Path] = []
    for uproject in download_dir.rglob("*.uproject"):
        roots.append(uproject.parent)
    roots.sort(key=lambda p: len(p.parts))
    unique: list[Path] = []
    for root in roots:
        if any(root == u or u in root.parents for u in unique):
            continue
        unique.append(root)
    return unique


def _plugin_copy_ignore(directory: str, names: list[str]) -> set[str]:
    base = Path(directory).name
    skip: set[str] = set()
    if base == "Intermediate":
        return set(names)
    if base == "Binaries":
        return {n for n in names if n.lower() not in ("linux",)}
    for n in names:
        lower = n.lower()
        if lower.endswith(".pdb") or lower == "intermediate":
            skip.add(n)
    return skip


def install_plugin_from_dir(plugin_dir: Path, dest_parent: Path) -> Path:
    """Copy a plugin folder into dest_parent/<Name> (engine or project Plugins)."""
    plugin_dir = plugin_dir.resolve()
    uplugins = list(plugin_dir.glob("*.uplugin"))
    if not uplugins:
        raise FabError(f"No .uplugin in {plugin_dir}")
    name = uplugins[0].stem
    dest = dest_parent / name
    dest_parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(plugin_dir, dest, ignore=_plugin_copy_ignore)
    return dest


def _stage_fab_download(
    asset: FabAsset,
    preferred_engine: str,
    cache_dir: Path,
    *,
    progress: ProgressCb | None = None,
) -> Path:
    plan = prepare_download(asset, preferred_engine=preferred_engine)
    staging = cache_dir / f"fab_{asset.asset_id[:12]}"
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True, exist_ok=True)
    download_asset(plan, staging, progress=progress)
    return staging


def install_fab_plugin(
    asset: FabAsset,
    engine: EngineInstall,
    cache_dir: Path,
    *,
    project: UProject | None = None,
    build_linux: bool = True,
    progress: ProgressCb | None = None,
) -> Path:
    """Download a Fab plugin and install into engine Marketplace or a project."""
    staging = _stage_fab_download(
        asset, engine.version_label, cache_dir, progress=progress
    )
    plugin_roots = _find_plugin_roots(staging)
    if not plugin_roots and (staging / "Engine" / "Plugins").exists():
        nested = list((staging / "Engine" / "Plugins").rglob("*.uplugin"))
        plugin_roots = [p.parent for p in nested]
    if not plugin_roots:
        raise FabError(
            f"Downloaded {asset.title!r} but found no .uplugin — "
            "this asset may be content or a full project."
        )

    dest_parent = (
        project_plugins_dir(project) if project is not None else marketplace_plugins_dir(engine)
    )
    installed = [install_plugin_from_dir(root, dest_parent) for root in plugin_roots]
    dest = installed[0]

    # Engine plugins need Linux binaries before the editor will load them.
    if project is None and build_linux and not plugin_has_linux_binaries(dest):
        build_plugin_linux(engine, dest, progress=progress)
    return dest


def install_fab_project(
    asset: FabAsset,
    engine: EngineInstall,
    projects_root: Path,
    cache_dir: Path,
    *,
    progress: ProgressCb | None = None,
) -> Path:
    """Download a Fab complete project and place it under projects_root."""
    staging = _stage_fab_download(
        asset, engine.version_label, cache_dir, progress=progress
    )
    roots = _find_uproject_roots(staging)
    if not roots:
        raise FabError(
            f"Downloaded {asset.title!r} but found no .uproject — "
            "not a complete project package."
        )
    source = roots[0]
    uproject = next(source.glob("*.uproject"))
    safe = "".join(c if c.isalnum() or c in "-_ " else "" for c in uproject.stem).strip()
    safe = "_".join(safe.split()) or f"FabProject_{asset.asset_id[:8]}"
    dest = projects_root.expanduser() / safe
    projects_root.expanduser().mkdir(parents=True, exist_ok=True)
    if dest.exists():
        raise FabError(f"Project folder already exists: {dest}")
    shutil.copytree(source, dest)
    return dest / uproject.name


def plugin_module_names(plugin_dir: Path) -> list[str]:
    names: list[str] = []
    for uplugin in plugin_dir.glob("*.uplugin"):
        meta = _read_uplugin(uplugin)
        for mod in meta.get("Modules") or []:
            if isinstance(mod, dict) and mod.get("Name"):
                names.append(str(mod["Name"]))
    return names


def plugin_has_linux_binaries(plugin_dir: Path) -> bool:
    binaries = plugin_dir / "Binaries" / "Linux"
    if not binaries.is_dir():
        return False
    return any(binaries.rglob("*.so")) or any(
        p.is_file() and p.suffix == "" for p in binaries.rglob("*")
    )


def ensure_engine_toolchain_executable(engine: EngineInstall) -> None:
    """Make bundled clang/SDK binaries executable (zip installs often lose +x)."""
    sdk_host = engine.path / "Engine" / "Extras" / "ThirdPartyNotUE" / "SDKs" / "HostLinux"
    if not sdk_host.is_dir():
        return
    for path in sdk_host.rglob("*"):
        if not path.is_file() or path.is_symlink():
            continue
        posix = path.as_posix()
        if "/bin/" in posix or "/libexec/" in posix or path.suffix == ".sh":
            try:
                path.chmod(path.stat().st_mode | 0o111)
            except OSError:
                pass


def build_plugin_linux(
    engine: EngineInstall,
    plugin_dir: Path,
    *,
    progress: ProgressCb | None = None,
) -> Path:
    """Compile a marketplace/engine plugin for Linux via RunUAT BuildPlugin.

    Fab packages ship Win/Mac binaries only. Engine plugins cannot be compiled when
    the editor starts — this builds Linux binaries into the plugin folder.
    """
    ensure_engine_toolchain_executable(engine)
    uplugins = list(plugin_dir.glob("*.uplugin"))
    if not uplugins:
        raise FabError(f"No .uplugin in {plugin_dir}")
    uplugin = uplugins[0]
    runuat = engine.path / "Engine" / "Build" / "BatchFiles" / "RunUAT.sh"
    if not runuat.is_file():
        raise FabError(f"RunUAT.sh missing in {engine.path}")

    package = Path("/tmp") / "ue-launcher-plugin-build" / plugin_dir.name
    if package.exists():
        shutil.rmtree(package)
    package.mkdir(parents=True, exist_ok=True)

    if progress:
        progress(f"Building {plugin_dir.name} for Linux…", 0, 0)

    cmd = [
        str(runuat),
        "BuildPlugin",
        f"-Plugin={uplugin}",
        f"-Package={package}",
        "-TargetPlatforms=Linux",
        "-HostPlatforms=Linux",
    ]
    env = os.environ.copy()
    # Avoid Cursor AppImage libs breaking the toolchain
    ld = env.get("LD_LIBRARY_PATH", "")
    if ld:
        cleaned = ":".join(
            p for p in ld.split(":") if p and "/tmp/.mount_cursor" not in p.lower()
        )
        if cleaned:
            env["LD_LIBRARY_PATH"] = cleaned
        else:
            env.pop("LD_LIBRARY_PATH", None)

    proc = subprocess.run(
        cmd,
        cwd=str(engine.path),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        tail = (proc.stdout or "")[-1500:] + "\n" + (proc.stderr or "")[-1500:]
        raise FabError(f"BuildPlugin failed for {plugin_dir.name}:\n{tail.strip()}")

    # Merge Linux binaries (+ rebuilt Intermediate if present) back into the live plugin.
    # BuildPlugin writes either Package/<files> or Package/HostProject/Plugins/<Name>/.
    built_plugin = package
    if not (package / uplugin.name).is_file():
        nested = list(package.rglob(uplugin.name))
        if nested:
            built_plugin = nested[0].parent
    if not list(built_plugin.glob("*.uplugin")):
        raise FabError(f"BuildPlugin produced no plugin tree under {package}")

    for sub in ("Binaries", "Intermediate"):
        src = built_plugin / sub
        if not src.is_dir():
            continue
        dest = plugin_dir / sub
        if sub == "Binaries":
            linux_src = src / "Linux"
            if not linux_src.is_dir():
                continue
            linux_dest = dest / "Linux"
            dest.mkdir(parents=True, exist_ok=True)
            if linux_dest.exists():
                shutil.rmtree(linux_dest)
            shutil.copytree(linux_src, linux_dest)
        else:
            if dest.exists():
                shutil.rmtree(dest)
            shutil.copytree(src, dest)

    if not plugin_has_linux_binaries(plugin_dir):
        raise FabError(
            f"Build finished but no Linux binaries found under {plugin_dir / 'Binaries' / 'Linux'}"
        )
    if progress:
        progress(f"Built {plugin_dir.name}", 1, 1)
    return plugin_dir


def plugin_installed_for_asset(
    asset: FabAsset,
    engine: EngineInstall | None = None,
    project: UProject | None = None,
) -> Path | None:
    """Best-effort match of a Fab title to an installed plugin folder."""
    needle = re.sub(r"[^a-z0-9]", "", asset.title.lower())
    parents: list[Path] = []
    if project is not None:
        parents.append(project_plugins_dir(project))
    if engine is not None:
        parents.append(marketplace_plugins_dir(engine))
    for market in parents:
        if not market.is_dir():
            continue
        for child in market.iterdir():
            if not child.is_dir():
                continue
            name = re.sub(r"[^a-z0-9]", "", child.name.lower())
            if needle and (needle in name or name in needle):
                return child
            for uplugin in child.glob("*.uplugin"):
                meta = _read_uplugin(uplugin)
                friendly = re.sub(
                    r"[^a-z0-9]", "", str(meta.get("FriendlyName", "")).lower()
                )
                if friendly and (needle in friendly or friendly in needle):
                    return child
    return None
