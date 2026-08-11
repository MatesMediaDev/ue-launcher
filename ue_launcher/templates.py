"""Custom project templates (e.g. MinUEmal from GitHub)."""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .config import CACHE_DIR
from .launch import clean_host_env
from .projects import TemplateInfo

TEMPLATES_CACHE = CACHE_DIR / "templates"


@dataclass(frozen=True)
class CustomTemplateSpec:
    template_id: str
    name: str
    git_url: str
    category: str = "Custom"
    description: str = ""
    # Optional branch / tag; empty = default branch
    ref: str = ""


# Built-in custom starters. MinUEmal is litruv's minimal UE 5.7+ template.
CUSTOM_TEMPLATE_SPECS: tuple[CustomTemplateSpec, ...] = (
    CustomTemplateSpec(
        template_id="minuemal",
        name="MinUEmal",
        git_url="https://github.com/litruv/MinUEmal.git",
        category="Custom",
        description="Minimal, performant UE 5.7+ starter (heavy features off)",
    ),
)


class TemplateError(Exception):
    pass


def custom_template_dir(spec: CustomTemplateSpec) -> Path:
    return TEMPLATES_CACHE / spec.template_id


def _has_uproject(path: Path) -> bool:
    return path.is_dir() and any(path.glob("*.uproject"))


def ensure_custom_template(
    spec: CustomTemplateSpec,
    *,
    refresh: bool = False,
) -> TemplateInfo:
    """Clone (or update) a custom template into the local cache."""
    dest = custom_template_dir(spec)
    TEMPLATES_CACHE.mkdir(parents=True, exist_ok=True)

    if dest.exists() and not _has_uproject(dest):
        shutil.rmtree(dest, ignore_errors=True)

    if dest.exists() and refresh:
        # Prefer pull when it's a git checkout
        if (dest / ".git").is_dir():
            cmd = ["git", "-C", str(dest), "pull", "--ff-only"]
            proc = subprocess.run(
                cmd, capture_output=True, text=True, check=False, env=clean_host_env()
            )
            if proc.returncode != 0:
                # Fall back to fresh clone
                shutil.rmtree(dest, ignore_errors=True)
        else:
            shutil.rmtree(dest, ignore_errors=True)

    if not dest.exists():
        cmd = ["git", "clone", "--depth", "1"]
        if spec.ref:
            cmd.extend(["--branch", spec.ref])
        cmd.extend([spec.git_url, str(dest)])
        proc = subprocess.run(
            cmd, capture_output=True, text=True, check=False, env=clean_host_env()
        )
        if proc.returncode != 0:
            raise TemplateError(
                f"Failed to fetch {spec.name}: {(proc.stderr or proc.stdout).strip()}"
            )

    if not _has_uproject(dest):
        raise TemplateError(f"Template {spec.name} has no .uproject at {dest}")

    return TemplateInfo(
        path=dest,
        name=spec.name,
        category=spec.category,
        description=spec.description,
        source="custom",
        template_id=spec.template_id,
    )


def list_custom_templates(*, refresh: bool = False) -> list[TemplateInfo]:
    """List custom templates. Does not block on git clone — fetch happens on create."""
    out: list[TemplateInfo] = []
    for spec in CUSTOM_TEMPLATE_SPECS:
        dest = custom_template_dir(spec)
        if refresh or not _has_uproject(dest):
            if refresh:
                try:
                    out.append(ensure_custom_template(spec, refresh=True))
                    continue
                except TemplateError:
                    pass
        out.append(
            TemplateInfo(
                path=dest if _has_uproject(dest) else dest,
                name=spec.name,
                category=spec.category,
                description=spec.description,
                source="custom",
                template_id=spec.template_id,
            )
        )
    return out


def resolve_custom_template(template: TemplateInfo, *, refresh: bool = False) -> TemplateInfo:
    """Ensure a custom TemplateInfo is fully fetched before copy."""
    if template.source != "custom" or not template.template_id:
        return template
    spec = next(
        (s for s in CUSTOM_TEMPLATE_SPECS if s.template_id == template.template_id),
        None,
    )
    if spec is None:
        if _has_uproject(template.path):
            return template
        raise TemplateError(f"Unknown custom template {template.name}")
    return ensure_custom_template(spec, refresh=refresh)
