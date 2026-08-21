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
from .epic.download import ProgressCb, download_asset, staging_is_complete
from .epic.fab import FabAsset, FabError, prepare_download
from .launch import clean_host_env
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


def is_content_asset(asset: FabAsset) -> bool:
    return asset_kind(asset) == "content"


def filter_plugin_assets(assets: list[FabAsset]) -> list[FabAsset]:
    """Owned Fab library items: plugins, complete projects, and content packs."""
    wanted_titles = (
        "blueprint assist",
        "auto size comments",
        "autosizecomments",
        "node graph assistant",
    )
    kind_order = {"plugin": 0, "project": 1, "content": 2}
    out: list[FabAsset] = []
    seen: set[str] = set()
    for asset in assets:
        kind = asset_kind(asset)
        include = kind in ("plugin", "project", "content")
        if kind == "engine":
            include = False
        if not include and any(w in asset.title.lower() for w in wanted_titles):
            include = True
        if not include:
            continue
        key = asset.asset_id or asset.title
        if key in seen:
            continue
        seen.add(key)
        out.append(asset)
    out.sort(key=lambda a: (kind_order.get(asset_kind(a), 9), a.title.lower()))
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
    if staging_is_complete(
        staging, asset_id=asset.asset_id, artifact_id=plan.artifact_id
    ):
        if progress:
            progress("Using cached download", 1, 1)
        return staging
    if staging.exists():
        # Keep .chunks so a cancelled/partial download can resume.
        for child in staging.iterdir():
            if child.name == ".chunks":
                continue
            if child.is_dir():
                shutil.rmtree(child)
            else:
                child.unlink(missing_ok=True)
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

    # Fab code plugins ship Win/Mac binaries only — compile for Linux before use.
    if build_linux and plugin_needs_linux_build(dest) and not plugin_has_linux_binaries(dest):
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


def _content_payload_root(staging: Path) -> Path:
    """Pick the folder whose children belong under a project's Content/ tree."""
    contents = sorted(
        (p for p in staging.rglob("Content") if p.is_dir()),
        key=lambda p: len(p.parts),
    )
    if contents:
        return contents[0]
    return staging


def _merge_tree(src: Path, dest: Path) -> None:
    """Copy src into dest, merging directories (overwrite files)."""
    dest.mkdir(parents=True, exist_ok=True)
    ignore = {"__macosx", ".ds_store", "intermediate", ".ue-launcher-asset.json"}
    for child in src.iterdir():
        if child.name.lower() in ignore or child.name.startswith("."):
            continue
        target = dest / child.name
        if child.is_dir():
            _merge_tree(child, target)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(child, target)


def _has_world_partition_layout(root: Path) -> bool:
    return (root / "__ExternalActors__").is_dir() or (root / "__ExternalObjects__").is_dir()


def install_fab_content(
    asset: FabAsset,
    engine: EngineInstall,
    project: UProject,
    cache_dir: Path,
    *,
    progress: ProgressCb | None = None,
) -> Path:
    """Download Fab content and merge into the project's Content/ root.

    World Partition maps require ``__ExternalActors__`` / ``__ExternalObjects__``
    at Content root (and asset folders like RacingUIPro/ beside them). Nesting
    under Content/Fab/<name>/ leaves maps empty in the editor.
    """
    staging = _stage_fab_download(
        asset, engine.version_label, cache_dir, progress=progress
    )
    # Prefer plugin/project handlers if the pack actually contains those.
    if _find_plugin_roots(staging):
        return install_fab_plugin(
            asset, engine, cache_dir, project=project, build_linux=True, progress=progress
        )
    if _find_uproject_roots(staging):
        raise FabError(
            f"{asset.title!r} looks like a complete project — use project install."
        )

    payload = _content_payload_root(staging)
    content_root = project.directory / "Content"
    content_root.mkdir(parents=True, exist_ok=True)

    if payload.name == "Content":
        source = payload
    elif _has_world_partition_layout(payload) or any(
        (payload / name).exists() for name in ("__ExternalActors__", "Maps", "Collections")
    ):
        source = payload
    else:
        # Loose pack without Content/ — keep a named folder so it stays findable.
        safe = "".join(c if c.isalnum() or c in "-_ " else "" for c in asset.title).strip()
        safe = "_".join(safe.split()) or f"FabContent_{asset.asset_id[:8]}"
        dest = content_root / "Fab" / safe
        if dest.exists():
            shutil.rmtree(dest)
        shutil.copytree(
            payload,
            dest,
            ignore=lambda _d, names: {
                n for n in names if n.lower() in ("__macosx", ".ds_store", "intermediate")
            },
        )
        return dest

    if progress:
        progress("Merging into project Content…", 0, 1)
    _merge_tree(source, content_root)

    # Remove a previous nested Fab/<name> install of the same pack if present.
    safe = "".join(c if c.isalnum() or c in "-_ " else "" for c in asset.title).strip()
    safe = "_".join(safe.split())
    if safe:
        nested = content_root / "Fab" / safe
        if nested.is_dir() and _has_world_partition_layout(nested):
            shutil.rmtree(nested, ignore_errors=True)
            fab_dir = content_root / "Fab"
            try:
                if fab_dir.is_dir() and not any(fab_dir.iterdir()):
                    fab_dir.rmdir()
            except OSError:
                pass

    return content_root


def repair_nested_fab_content(project: UProject) -> list[Path]:
    """Move World Partition packs out of Content/Fab/<name>/ into Content/."""
    fab_root = project.directory / "Content" / "Fab"
    if not fab_root.is_dir():
        return []
    content_root = project.directory / "Content"
    repaired: list[Path] = []
    for child in list(fab_root.iterdir()):
        if not child.is_dir():
            continue
        if not _has_world_partition_layout(child):
            continue
        _merge_tree(child, content_root)
        shutil.rmtree(child, ignore_errors=True)
        repaired.append(child)
    try:
        if fab_root.is_dir() and not any(fab_root.iterdir()):
            fab_root.rmdir()
    except OSError:
        pass
    return repaired


def plugin_module_names(plugin_dir: Path) -> list[str]:
    names: list[str] = []
    for uplugin in plugin_dir.glob("*.uplugin"):
        meta = _read_uplugin(uplugin)
        for mod in meta.get("Modules") or []:
            if isinstance(mod, dict) and mod.get("Name"):
                names.append(str(mod["Name"]))
    return names


def plugin_needs_linux_build(plugin_dir: Path) -> bool:
    """True when the plugin ships C++ source and needs a local Linux compile."""
    if not (plugin_dir / "Source").is_dir():
        return False
    return bool(plugin_module_names(plugin_dir))


def _write_uplugin(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, indent="\t") + "\n", encoding="utf-8")


def prepare_plugin_for_linux_build(plugin_dir: Path) -> bool:
    """Ensure .uplugin modules allow Linux — Fab often ships Win/Mac only.

    BuildPlugin exits 0 without producing .so files when Linux is missing from
    PlatformAllowList, which is why marketplace installs looked successful but
    had empty Binaries/Linux folders.
    """
    uplugins = list(plugin_dir.glob("*.uplugin"))
    if not uplugins:
        return False
    uplugin_path = uplugins[0]
    meta = _read_uplugin(uplugin_path)
    if not meta:
        return False

    changed = False
    allow_keys = ("PlatformAllowList", "WhitelistPlatforms")
    deny_keys = ("PlatformDenyList", "BlacklistPlatforms")
    for mod in meta.get("Modules") or []:
        if not isinstance(mod, dict):
            continue
        for key in allow_keys:
            platforms = mod.get(key)
            if not isinstance(platforms, list):
                continue
            if "Linux" not in platforms:
                mod[key] = [*platforms, "Linux"]
                changed = True
        for key in deny_keys:
            platforms = mod.get(key)
            if not isinstance(platforms, list) or "Linux" not in platforms:
                continue
            mod[key] = [p for p in platforms if p != "Linux"]
            changed = True

    if not changed:
        return False

    backup = uplugin_path.with_suffix(uplugin_path.suffix + ".bak")
    if not backup.is_file():
        shutil.copy2(uplugin_path, backup)
    _write_uplugin(uplugin_path, meta)
    return True


def plugin_has_linux_binaries(plugin_dir: Path) -> bool:
    binaries = plugin_dir / "Binaries" / "Linux"
    if not binaries.is_dir():
        return False
    if any(binaries.glob("libUnrealEditor-*.so")):
        return True
    if (binaries / "UnrealEditor.modules").is_file():
        return True
    return any(binaries.rglob("*.so"))


def _plugin_build_output_root(package: Path, uplugin_name: str) -> Path:
    """Locate BuildPlugin output (Binaries/Linux may sit at package root)."""
    candidates: list[Path] = []
    if (package / "Binaries" / "Linux").is_dir():
        candidates.append(package)
    for uplugin_path in package.rglob(uplugin_name):
        parent = uplugin_path.parent
        if (parent / "Binaries" / "Linux").is_dir():
            candidates.append(parent)
    if candidates:
        return min(candidates, key=lambda p: len(p.parts))
    if (package / uplugin_name).is_file():
        return package
    nested = sorted(package.rglob(uplugin_name), key=lambda p: len(p.parts))
    if nested:
        return nested[0].parent
    return package


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
    prepare_plugin_for_linux_build(plugin_dir)
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
    env = clean_host_env()

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

    built_plugin = _plugin_build_output_root(package, uplugin.name)
    if not list(built_plugin.glob("*.uplugin")) and not (
        built_plugin / "Binaries" / "Linux"
    ).is_dir():
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
