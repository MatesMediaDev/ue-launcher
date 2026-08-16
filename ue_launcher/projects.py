"""Discover and create Unreal projects."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .config import Config
from .engines import EngineInstall
from .launch import clean_host_env

UPROJECT_RE = re.compile(r"\.uproject$", re.IGNORECASE)
SAFE_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
GIT_URL_RE = re.compile(
    r"^(?:https?://|git@|ssh://|git://).+",
    re.IGNORECASE,
)


class ProjectImportError(Exception):
    pass


@dataclass(frozen=True)
class UProject:
    path: Path
    name: str
    engine_association: str
    has_code: bool

    @property
    def directory(self) -> Path:
        return self.path.parent


def project_thumbnail_path(project: UProject) -> Path | None:
    """Best local image for a project row (editor AutoScreenshot, then common fallbacks)."""
    root = project.directory
    candidates = [
        root / "Saved" / "AutoScreenshot.png",
        root / "Build" / "Linux" / "Application.png",
        root / "Build" / "Linux" / "Resources" / "Icon.png",
        root / "Content" / "Splash" / "Splash.png",
    ]
    for path in candidates:
        if path.is_file() and path.stat().st_size > 64:
            return path
    shots = root / "Saved" / "Screenshots"
    if shots.is_dir():
        pngs = sorted(shots.glob("*.png"), key=lambda p: p.stat().st_mtime, reverse=True)
        for path in pngs[:8]:
            if path.stat().st_size > 64:
                return path
    return None


@dataclass(frozen=True)
class TemplateInfo:
    path: Path
    name: str
    category: str
    description: str = ""
    source: str = "engine"  # engine | custom
    template_id: str = ""

    @property
    def label(self) -> str:
        if self.source == "custom":
            return f"{self.name} · Custom"
        return f"{self.name} · {self.category}"


def parse_uproject(path: Path) -> UProject | None:
    path = path.expanduser()
    if not path.is_file() or path.suffix.lower() != ".uproject":
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return None
    modules = data.get("Modules") or []
    has_code = any(
        isinstance(m, dict) and str(m.get("Type", "")).lower() == "runtime" for m in modules
    )
    # Blueprints-only still lists modules sometimes; treat presence of Source/ as stronger signal
    if (path.parent / "Source").is_dir():
        has_code = True
    return UProject(
        path=path.resolve(),
        name=path.stem,
        engine_association=str(data.get("EngineAssociation", "")),
        has_code=has_code,
    )


def discover_projects(config: Config, max_depth: int = 4) -> list[UProject]:
    found: dict[Path, UProject] = {}

    for root in config.project_scan_roots:
        root = root.expanduser()
        if not root.is_dir():
            continue
        _scan_tree(root, found, max_depth=max_depth, depth=0)

    # Favorites / recent even if outside scan roots
    for key in ("favorite_projects", "recent_projects"):
        for raw in config.get(key, []) or []:
            proj = parse_uproject(Path(raw))
            if proj:
                found[proj.path] = proj

    projects = list(found.values())
    projects.sort(key=lambda p: p.path.stat().st_mtime if p.path.exists() else 0, reverse=True)
    return projects


def _scan_tree(root: Path, found: dict[Path, UProject], max_depth: int, depth: int) -> None:
    if depth > max_depth:
        return
    skip_names = {
        "Engine",
        "Intermediate",
        "Binaries",
        "DerivedDataCache",
        "Saved",
        ".git",
        "node_modules",
        "__pycache__",
    }
    try:
        entries = list(root.iterdir())
    except OSError:
        return

    # Prefer uprojects in this directory first (project root)
    for entry in entries:
        if entry.is_file() and entry.suffix.lower() == ".uproject":
            proj = parse_uproject(entry)
            if proj:
                found[proj.path] = proj

    if depth == max_depth:
        return

    for entry in entries:
        if not entry.is_dir():
            continue
        if entry.name in skip_names or entry.name.startswith("."):
            continue
        _scan_tree(entry, found, max_depth, depth + 1)


def list_templates(engine: EngineInstall) -> list[TemplateInfo]:
    """Engine Templates/ folder plus built-in custom starters (MinUEmal, …)."""
    from .templates import list_custom_templates

    results: list[TemplateInfo] = []
    # Custom first so MinUEmal is easy to pick
    results.extend(list_custom_templates(refresh=False))

    templates_dir = engine.templates_dir
    if templates_dir.is_dir():
        try:
            children = sorted(templates_dir.iterdir())
        except OSError:
            children = []
        for child in children:
            if not child.is_dir():
                continue
            uprojects = list(child.glob("*.uproject"))
            if not uprojects:
                continue
            category = "Blueprint" if child.name.endswith("BP") else "Code"
            results.append(
                TemplateInfo(path=child, name=child.name, category=category, source="engine")
            )
    return results


def create_project_from_template(
    engine: EngineInstall,
    template: TemplateInfo,
    dest_parent: Path,
    project_name: str,
) -> UProject:
    if not SAFE_NAME_RE.match(project_name):
        raise ValueError(
            "Project name must start with a letter/underscore and contain only A-Z, 0-9, _"
        )

    # Fetch custom templates on demand if cache is cold
    if template.source == "custom":
        from .templates import resolve_custom_template

        template = resolve_custom_template(template, refresh=False)

    if not template.path.is_dir() or not list(template.path.glob("*.uproject")):
        raise FileNotFoundError(f"Template missing or incomplete: {template.path}")

    dest_parent = dest_parent.expanduser()
    dest_parent.mkdir(parents=True, exist_ok=True)
    dest = dest_parent / project_name
    if dest.exists():
        raise FileExistsError(f"Destination already exists: {dest}")

    shutil.copytree(
        template.path,
        dest,
        ignore=shutil.ignore_patterns(
            ".git",
            ".github",
            "__pycache__",
            ".DS_Store",
            "Binaries",
            "Intermediate",
            "DerivedDataCache",
            "Saved",
        ),
    )

    # Rename .uproject
    old_projects = list(dest.glob("*.uproject"))
    if not old_projects:
        raise RuntimeError("Template copy has no .uproject")
    old = old_projects[0]
    new_path = dest / f"{project_name}.uproject"
    data = json.loads(old.read_text(encoding="utf-8-sig"))
    data["EngineAssociation"] = (
        f"{engine.version_tuple[0]}.{engine.version_tuple[1]}"
        if engine.version_tuple[0]
        else engine.version_label.replace("UE_", "")
    )
    new_path.write_text(json.dumps(data, indent=4) + "\n", encoding="utf-8")
    if old != new_path:
        old.unlink(missing_ok=True)

    # Best-effort rename of Config/Default*.ini GameName references is optional;
    # UE rewrites most of this on first open.
    proj = parse_uproject(new_path)
    if not proj:
        raise RuntimeError("Failed to parse newly created project")
    return proj


def guess_repo_folder_name(git_url: str) -> str:
    """Derive a folder name from a git remote URL."""
    raw = git_url.strip().rstrip("/")
    if raw.endswith(".git"):
        raw = raw[:-4]
    # git@host:owner/repo or https://host/owner/repo
    if ":" in raw and not raw.split(":")[-1].startswith("//"):
        raw = raw.split(":")[-1]
    name = Path(raw).name
    safe = re.sub(r"[^A-Za-z0-9_\-]+", "_", name).strip("_")
    return safe or "UnrealProject"


def find_uproject_in_tree(root: Path, max_depth: int = 3) -> Path | None:
    """Find the shallowest .uproject under root."""
    root = root.expanduser()
    if not root.is_dir():
        return None
    best: Path | None = None
    best_depth = 10_000
    skip = {"Engine", "Intermediate", "Binaries", "DerivedDataCache", "Saved", ".git"}

    def walk(path: Path, depth: int) -> None:
        nonlocal best, best_depth
        if depth > max_depth or depth >= best_depth:
            return
        try:
            entries = list(path.iterdir())
        except OSError:
            return
        for entry in entries:
            if entry.is_file() and entry.suffix.lower() == ".uproject":
                if depth < best_depth:
                    best = entry
                    best_depth = depth
                return
        for entry in entries:
            if not entry.is_dir() or entry.name in skip or entry.name.startswith("."):
                continue
            walk(entry, depth + 1)

    walk(root, 0)
    return best


def set_engine_association(uproject: Path, engine: EngineInstall) -> None:
    data = json.loads(uproject.read_text(encoding="utf-8-sig"))
    data["EngineAssociation"] = (
        f"{engine.version_tuple[0]}.{engine.version_tuple[1]}"
        if engine.version_tuple[0]
        else engine.version_label.replace("UE_", "")
    )
    uproject.write_text(json.dumps(data, indent=4) + "\n", encoding="utf-8")


def import_project_from_git(
    git_url: str,
    dest_parent: Path,
    *,
    folder_name: str | None = None,
    branch: str = "",
    engine: EngineInstall | None = None,
    depth: int = 1,
) -> UProject:
    """Clone a git repo and register the .uproject inside it."""
    url = git_url.strip()
    if not url or not GIT_URL_RE.match(url):
        raise ProjectImportError("Enter a valid git URL (https://… or git@…)")

    dest_parent = dest_parent.expanduser()
    dest_parent.mkdir(parents=True, exist_ok=True)
    name = (folder_name or guess_repo_folder_name(url)).strip()
    if not name:
        raise ProjectImportError("Folder name is empty")
    dest = dest_parent / name
    if dest.exists():
        raise ProjectImportError(f"Destination already exists: {dest}")

    cmd = ["git", "clone"]
    if depth and depth > 0:
        cmd.extend(["--depth", str(depth)])
    if branch.strip():
        cmd.extend(["--branch", branch.strip()])
    cmd.extend([url, str(dest)])

    proc = subprocess.run(
        cmd, capture_output=True, text=True, check=False, env=clean_host_env()
    )
    if proc.returncode != 0:
        shutil.rmtree(dest, ignore_errors=True)
        detail = (proc.stderr or proc.stdout or "git clone failed").strip()
        raise ProjectImportError(detail)

    uproject = find_uproject_in_tree(dest)
    if uproject is None:
        shutil.rmtree(dest, ignore_errors=True)
        raise ProjectImportError(
            f"Cloned {url} but no .uproject was found under {dest}"
        )

    if engine is not None:
        try:
            set_engine_association(uproject, engine)
        except (OSError, json.JSONDecodeError, TypeError):
            pass

    proj = parse_uproject(uproject)
    if not proj:
        raise ProjectImportError(f"Failed to parse {uproject}")
    return proj
