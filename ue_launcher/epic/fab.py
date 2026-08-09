"""Fab.com library + manifest API (Bearer = Epic access token)."""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import requests

from ..config import CACHE_DIR
from .auth import AuthTokens, ensure_fresh_tokens
from .manifest import ParsedManifest, parse_manifest_bytes

FAB_HOST = "https://www.fab.com"
FAB_USER_AGENT = "ue-launcher/0.1 (+linux)"
LIBRARY_PAGE_SIZE = 100
LIBRARY_CACHE_PATH = CACHE_DIR / "fab_library.json"
LIBRARY_CACHE_MAX_AGE_SEC = 6 * 60 * 60  # 6 hours


class FabError(Exception):
    pass


@dataclass
class FabAsset:
    asset_id: str
    title: str
    distribution_method: str
    namespace: str
    owned_at: str
    thumbnail_url: str
    project_versions: list[dict[str, Any]] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def engine_versions(self) -> list[str]:
        versions: list[str] = []
        for pv in self.project_versions:
            for ev in pv.get("engineVersions") or []:
                if isinstance(ev, str) and ev not in versions:
                    versions.append(ev)
        return versions


@dataclass
class FabDownloadPlan:
    asset: FabAsset
    artifact_id: str
    namespace: str
    manifest: ParsedManifest
    distribution_base_url: str
    manifest_pointers: list[tuple[str, str]]


def _headers(tokens: AuthTokens) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {tokens.access_token}",
        "Accept": "application/json",
        "User-Agent": FAB_USER_AGENT,
    }


def _string_field(record: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = record.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def _project_versions(record: dict[str, Any]) -> list[dict[str, Any]]:
    raw = record.get("projectVersions")
    if not isinstance(raw, list):
        return []
    out: list[dict[str, Any]] = []
    for entry in raw:
        if isinstance(entry, dict) and entry.get("artifactId"):
            out.append(entry)
    return out


def _parse_engine(s: str) -> int:
    match = re.match(r"^UE_(\d+)\.(\d+)", s)
    if not match:
        return -1
    return int(match.group(1)) * 100 + int(match.group(2))


def pick_artifact_id(versions: list[dict[str, Any]], preferred_engine: str) -> str:
    exact = next(
        (
            v
            for v in versions
            if preferred_engine in (v.get("engineVersions") or [])
        ),
        None,
    )
    if exact:
        return str(exact["artifactId"])

    target = _parse_engine(preferred_engine)
    best: dict[str, Any] | None = None
    best_score = -10_000
    for v in versions:
        evs = [ _parse_engine(x) for x in (v.get("engineVersions") or []) if isinstance(x, str)]
        if not evs:
            continue
        max_v = max(evs)
        score = max_v if max_v <= target or target < 0 else max_v - 1000
        if score > best_score:
            best_score = score
            best = v
    return str(best["artifactId"]) if best else ""


def _summarize(record: dict[str, Any]) -> FabAsset:
    thumb = ""
    images = record.get("images") or record.get("thumbnail") or {}
    if isinstance(images, str):
        thumb = images
    elif isinstance(images, dict):
        thumb = _string_field(images, "url", "uri", "thumbnailUrl")
    elif isinstance(images, list) and images:
        first = images[0]
        if isinstance(first, dict):
            thumb = _string_field(first, "url", "uri")
        elif isinstance(first, str):
            thumb = first

    return FabAsset(
        asset_id=_string_field(record, "assetId", "asset_id", "uid"),
        title=_string_field(record, "title", "name", "description") or "Untitled",
        distribution_method=_string_field(
            record, "distributionMethod", "distribution_method", "listingType", "category"
        ),
        namespace=_string_field(record, "assetNamespace", "namespace", "ns"),
        owned_at=_string_field(record, "addedAt", "added_at", "ownedAt", "owned_at"),
        thumbnail_url=thumb or _string_field(record, "thumbnailUrl", "thumbnail_url"),
        project_versions=_project_versions(record),
        raw=record,
    )


def list_library(tokens: AuthTokens | None = None) -> list[FabAsset]:
    tokens = ensure_fresh_tokens(tokens)
    items: list[FabAsset] = []
    cursor: str | None = None

    while True:
        params: dict[str, str] = {"count": str(LIBRARY_PAGE_SIZE)}
        if cursor:
            params["cursor"] = cursor
        url = f"{FAB_HOST}/e/accounts/{tokens.account_id}/ue/library"
        response = requests.get(url, headers=_headers(tokens), params=params, timeout=60)
        if response.status_code >= 400:
            raise FabError(f"Library request failed: HTTP {response.status_code}")
        page = response.json()
        for record in page.get("results") or []:
            if isinstance(record, dict):
                items.append(_summarize(record))
        next_cursor = (page.get("cursors") or {}).get("next")
        if isinstance(next_cursor, str) and next_cursor:
            cursor = next_cursor
        else:
            break
    save_library_cache(tokens.account_id, items)
    return items


def _asset_to_cache(asset: FabAsset) -> dict[str, Any]:
    return {
        "asset_id": asset.asset_id,
        "title": asset.title,
        "distribution_method": asset.distribution_method,
        "namespace": asset.namespace,
        "owned_at": asset.owned_at,
        "thumbnail_url": asset.thumbnail_url,
        "project_versions": asset.project_versions,
    }


def _asset_from_cache(data: dict[str, Any]) -> FabAsset:
    versions = data.get("project_versions")
    return FabAsset(
        asset_id=str(data.get("asset_id") or ""),
        title=str(data.get("title") or "Untitled"),
        distribution_method=str(data.get("distribution_method") or ""),
        namespace=str(data.get("namespace") or ""),
        owned_at=str(data.get("owned_at") or ""),
        thumbnail_url=str(data.get("thumbnail_url") or ""),
        project_versions=versions if isinstance(versions, list) else [],
        raw={},
    )


def save_library_cache(account_id: str, assets: list[FabAsset]) -> Path:
    LIBRARY_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "account_id": account_id,
        "fetched_at": time.time(),
        "assets": [_asset_to_cache(a) for a in assets],
    }
    tmp = LIBRARY_CACHE_PATH.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh)
    tmp.replace(LIBRARY_CACHE_PATH)
    return LIBRARY_CACHE_PATH


def load_library_cache(
    account_id: str | None = None,
    *,
    max_age_sec: float = LIBRARY_CACHE_MAX_AGE_SEC,
) -> tuple[list[FabAsset], float] | None:
    """Return (assets, age_seconds) if a usable cache exists."""
    if not LIBRARY_CACHE_PATH.is_file():
        return None
    try:
        with LIBRARY_CACHE_PATH.open("r", encoding="utf-8") as fh:
            payload = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    if account_id and payload.get("account_id") and payload.get("account_id") != account_id:
        return None
    fetched_at = float(payload.get("fetched_at") or 0)
    age = time.time() - fetched_at
    if fetched_at <= 0 or age > max_age_sec:
        return None
    raw_assets = payload.get("assets")
    if not isinstance(raw_assets, list):
        return None
    assets = [_asset_from_cache(a) for a in raw_assets if isinstance(a, dict)]
    return assets, age


def list_library_cached(
    tokens: AuthTokens | None = None,
    *,
    force_refresh: bool = False,
    max_age_sec: float = LIBRARY_CACHE_MAX_AGE_SEC,
) -> tuple[list[FabAsset], bool]:
    """Return (assets, from_cache). Refreshes from Fab when forced or cache is cold."""
    tokens = ensure_fresh_tokens(tokens)
    if not force_refresh:
        cached = load_library_cache(tokens.account_id, max_age_sec=max_age_sec)
        if cached is not None:
            return cached[0], True
    return list_library(tokens), False


def clear_library_cache() -> None:
    try:
        LIBRARY_CACHE_PATH.unlink(missing_ok=True)
    except OSError:
        pass


def prepare_download(
    asset: FabAsset,
    preferred_engine: str = "UE_5.6",
    tokens: AuthTokens | None = None,
) -> FabDownloadPlan:
    tokens = ensure_fresh_tokens(tokens)
    namespace = asset.namespace or _string_field(asset.raw, "assetNamespace", "namespace", "ns")
    artifact_id = pick_artifact_id(asset.project_versions, preferred_engine)
    if not artifact_id or not namespace:
        raise FabError(
            f"Asset {asset.asset_id!r} missing artifact/namespace "
            f"(versions={len(asset.project_versions)}, ns={bool(namespace)})"
        )

    manifest_url = f"{FAB_HOST}/e/artifacts/{artifact_id}/manifest"
    response = requests.post(
        manifest_url,
        headers={**_headers(tokens), "Content-Type": "application/json"},
        json={
            "item_id": asset.asset_id,
            "namespace": namespace,
            "platform": "Windows",
        },
        timeout=60,
    )
    if response.status_code >= 400:
        raise FabError(f"Manifest request failed: HTTP {response.status_code} — {response.text[:200]}")

    payload = response.json()
    artifacts = payload.get("downloadInfo") or payload.get("download_info") or []
    pointers: list[tuple[str, str]] = []
    for artifact in artifacts:
        if not isinstance(artifact, dict):
            continue
        points = artifact.get("distributionPoints") or []
        base_urls = artifact.get("distributionPointBaseUrls") or []
        for i, point in enumerate(points):
            if not isinstance(point, dict):
                continue
            m_url = point.get("manifestUrl") or point.get("manifest_url") or ""
            base = base_urls[i] if i < len(base_urls) else ""
            if m_url and base:
                pointers.append((str(m_url), str(base)))

    if not pointers:
        raise FabError(f"No distribution points for asset {asset.asset_id}")

    primary_url, primary_base = pointers[0]
    manifest_bytes = requests.get(primary_url, timeout=120).content
    parsed = parse_manifest_bytes(manifest_bytes)

    return FabDownloadPlan(
        asset=asset,
        artifact_id=artifact_id,
        namespace=namespace,
        manifest=parsed,
        distribution_base_url=primary_base.rstrip("/"),
        manifest_pointers=pointers,
    )
