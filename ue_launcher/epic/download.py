"""Download Fab assets via Epic chunked manifests."""

from __future__ import annotations

import hashlib
import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Callable

import requests
from requests.adapters import HTTPAdapter

from .fab import FabDownloadPlan
from .manifest import ChunkInfo, FileEntry, chunk_url, decode_chunk_payload

ProgressCb = Callable[[str, int, int], None]  # message, done, total

# Epic CDN is latency-bound; more workers help a lot on long-haul links (e.g. AU).
CHUNK_CONCURRENCY = max(4, min(32, int(os.environ.get("UE_LAUNCHER_FAB_CONCURRENCY", "16"))))
# After a refetch retry, allow writing files that still fail SHA1 (rare CDN/manifest drift).
ALLOW_HASH_MISMATCH = os.environ.get("UE_LAUNCHER_FAB_ALLOW_HASH_MISMATCH", "").lower() in (
    "1",
    "true",
    "yes",
)


class DownloadError(Exception):
    pass


def _safe_relative(filename: str) -> Path:
    unix = filename.replace("\\", "/")
    parts = [p for p in Path(unix).parts if p not in ("", ".", "..")]
    if unix.startswith("/") or (len(unix) > 1 and unix[1] == ":"):
        parts = [p for p in Path(unix).parts if p not in ("/", "\\") and not (len(p) == 2 and p[1] == ":")]
        parts = [p for p in parts if p not in (".", "..")]
    return Path(*parts) if parts else Path("file")


_thread_local = threading.local()


def _http_session() -> requests.Session:
    """One Session per worker thread — Session is not safe for concurrent use."""
    session = getattr(_thread_local, "session", None)
    if session is not None:
        return session
    session = requests.Session()
    adapter = HTTPAdapter(pool_connections=4, pool_maxsize=4, max_retries=2)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.headers.update(
        {
            "User-Agent": (
                "EpicGamesLauncher/16.0.0-38817803+++Portal+Release-Live "
                "Windows/10.0.19045.1.256.64bit"
            ),
            "Accept": "*/*",
            "Connection": "keep-alive",
        }
    )
    _thread_local.session = session
    return session


def _verify_chunk_payload(info: ChunkInfo, payload: bytes) -> None:
    if info.sha_hash and len(info.sha_hash) == 20:
        digest = hashlib.sha1(payload).digest()
        if digest != info.sha_hash:
            raise DownloadError(
                f"Chunk SHA1 mismatch for {info.guid}: "
                f"got {digest.hex()}, want {info.sha_hash.hex()}"
            )


def _fetch_chunk(
    chunk: ChunkInfo,
    base_urls: list[str],
    version: int,
) -> bytes:
    session = _http_session()
    last_err: Exception | None = None
    for base in base_urls:
        url = chunk_url(chunk, base, version)
        try:
            response = session.get(url, timeout=(10, 120))
            if response.status_code >= 400:
                last_err = DownloadError(
                    f"Chunk fetch failed for {chunk.guid}: HTTP {response.status_code}"
                )
                continue
            payload = decode_chunk_payload(response.content)
            _verify_chunk_payload(chunk, payload)
            return payload
        except (requests.RequestException, ValueError, OSError, DownloadError) as exc:
            last_err = exc
            continue
    raise DownloadError(f"Chunk fetch failed for {chunk.guid}: {last_err}")


def _assemble_file(
    entry: FileEntry,
    chunk_payloads: dict[str, bytes],
    target: Path,
    *,
    enforce_hash: bool = True,
) -> int:
    buffer = bytearray(entry.file_size)
    write_offset = 0
    for part in entry.chunk_parts:
        payload = chunk_payloads[part.guid]
        end = part.offset + part.size
        if end > len(payload):
            raise DownloadError(
                f"Chunk {part.guid} too small (need {end}, have {len(payload)})"
            )
        buffer[write_offset : write_offset + part.size] = payload[part.offset : end]
        write_offset += part.size

    if write_offset != entry.file_size:
        raise DownloadError(
            f"Size mismatch for {entry.filename}: wrote {write_offset}, expected {entry.file_size}"
        )

    if entry.file_hash and entry.file_hash != ("0" * 40):
        digest = hashlib.sha1(buffer).hexdigest()
        want = entry.file_hash.lower()
        if digest != want:
            # JSON manifests historically store the digest with reversed byte order.
            if digest != bytes.fromhex(want)[::-1].hex():
                if enforce_hash and not ALLOW_HASH_MISMATCH:
                    raise DownloadError(
                        f"SHA1 mismatch for {entry.filename}: got {digest}, want {want}"
                    )
            else:
                # Accept endian-flipped expected hash from odd manifests.
                pass

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(buffer)
    return len(buffer)


def _distribution_bases(plan: FabDownloadPlan) -> list[str]:
    bases: list[str] = []
    primary = plan.distribution_base_url.rstrip("/")
    if primary:
        bases.append(primary)
    for _manifest_url, base in plan.manifest_pointers:
        cleaned = str(base).rstrip("/")
        if cleaned and cleaned not in bases:
            bases.append(cleaned)
    return bases or [primary]


def download_asset(
    plan: FabDownloadPlan,
    target_dir: Path,
    *,
    preserve_structure: bool = True,
    progress: ProgressCb | None = None,
) -> dict:
    target_dir = target_dir.expanduser()
    target_dir.mkdir(parents=True, exist_ok=True)

    files = plan.manifest.files
    if not files:
        raise DownloadError(f"Asset {plan.asset.asset_id} has no files")

    needed: dict[str, ChunkInfo] = {}
    for entry in files:
        for part in entry.chunk_parts:
            info = plan.manifest.chunks.get(part.guid)
            if not info:
                raise DownloadError(f"Unknown chunk GUID {part.guid}")
            needed[part.guid] = info

    bases = _distribution_bases(plan)
    guids = list(needed.keys())
    total_chunks = len(guids)
    total_bytes = sum(max(0, needed[g].file_size) for g in guids) or total_chunks
    use_bytes = total_bytes > total_chunks
    done_bytes = 0
    done_chunks = 0
    lock = threading.Lock()
    chunk_payloads: dict[str, bytes] = {}

    chunk_cache = target_dir / ".chunks"
    chunk_cache.mkdir(parents=True, exist_ok=True)

    def _invalidate(guid: str) -> None:
        path = chunk_cache / f"{guid}.bin"
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass

    def _load_or_fetch(guid: str, *, force: bool = False) -> tuple[str, bytes, int]:
        info = needed[guid]
        cache_path = chunk_cache / f"{guid}.bin"
        if not force and cache_path.is_file() and cache_path.stat().st_size > 0:
            payload = cache_path.read_bytes()
            try:
                _verify_chunk_payload(info, payload)
                return guid, payload, len(payload)
            except DownloadError:
                _invalidate(guid)
        payload = _fetch_chunk(info, bases, plan.manifest.version)
        try:
            cache_path.write_bytes(payload)
        except OSError:
            pass
        return guid, payload, len(payload)

    def _fetch_many(to_fetch: list[str], *, force: bool = False) -> None:
        nonlocal done_bytes, done_chunks
        if not to_fetch:
            return
        with ThreadPoolExecutor(max_workers=CHUNK_CONCURRENCY) as pool:
            futures = [pool.submit(_load_or_fetch, g, force=force) for g in to_fetch]
            for fut in as_completed(futures):
                guid, payload, nbytes = fut.result()
                chunk_payloads[guid] = payload
                with lock:
                    done_chunks += 1
                    done_bytes += nbytes
                    if progress:
                        if use_bytes:
                            progress(
                                f"Chunk {done_chunks}/{total_chunks}",
                                min(done_bytes, total_bytes),
                                total_bytes,
                            )
                        else:
                            progress(
                                f"Chunk {done_chunks}/{total_chunks}",
                                done_chunks,
                                total_chunks,
                            )

    if progress:
        progress(
            f"Downloading {total_chunks} chunks…",
            0,
            total_bytes if use_bytes else total_chunks,
        )
    _fetch_many(guids)

    written: list[str] = []
    bytes_total = 0
    for i, entry in enumerate(files, start=1):
        rel = _safe_relative(entry.filename)
        if not preserve_structure:
            rel = Path(rel.name)
        dest = target_dir / rel
        try:
            nbytes = _assemble_file(entry, chunk_payloads, dest)
        except DownloadError as exc:
            if "SHA1 mismatch" not in str(exc):
                raise
            # Refetch only the chunks that make up this file, then try once more.
            if progress:
                progress(f"Retrying {rel.name}…", i, len(files))
            retry_guids = list({part.guid for part in entry.chunk_parts})
            for guid in retry_guids:
                _invalidate(guid)
            _fetch_many(retry_guids, force=True)
            nbytes = _assemble_file(
                entry,
                chunk_payloads,
                dest,
                enforce_hash=not ALLOW_HASH_MISMATCH,
            )
        written.append(str(dest))
        bytes_total += nbytes
        if progress:
            progress(f"File {i}/{len(files)}: {rel}", i, len(files))

    # Drop raw chunks once assembled to reclaim disk.
    try:
        for path in chunk_cache.glob("*.bin"):
            path.unlink(missing_ok=True)
        chunk_cache.rmdir()
    except OSError:
        pass

    marker = target_dir / ".ue-launcher-asset.json"
    marker.write_text(
        json.dumps(
            {
                "asset_id": plan.asset.asset_id,
                "title": plan.asset.title,
                "artifact_id": plan.artifact_id,
                "bytes_total": bytes_total,
                "files": len(written),
            },
            indent=0,
        )
        + "\n",
        encoding="utf-8",
    )

    return {
        "files": written,
        "bytes_total": bytes_total,
        "target_dir": str(target_dir),
    }


def asset_download_dir(base: Path, asset_title: str, asset_id: str) -> Path:
    safe = "".join(c if c.isalnum() or c in "-_ " else "_" for c in asset_title).strip() or asset_id
    safe = "_".join(safe.split())
    return base / f"{safe}_{asset_id[:8]}"


def staging_is_complete(staging: Path, *, asset_id: str, artifact_id: str) -> bool:
    """True if a previous Fab download finished for this artifact."""
    marker = staging / ".ue-launcher-asset.json"
    if not marker.is_file():
        return False
    try:
        data = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if data.get("asset_id") != asset_id or data.get("artifact_id") != artifact_id:
        return False
    for path in staging.rglob("*"):
        if path.is_file() and path.name != ".ue-launcher-asset.json":
            return True
    return False
