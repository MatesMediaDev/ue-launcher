"""Download Fab assets via Epic chunked manifests."""

from __future__ import annotations

import hashlib
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Callable

import requests

from .fab import FabDownloadPlan
from .manifest import ChunkInfo, FileEntry, chunk_url, decode_chunk_payload

ProgressCb = Callable[[str, int, int], None]  # message, done, total
CHUNK_CONCURRENCY = 8


class DownloadError(Exception):
    pass


def _safe_relative(filename: str) -> Path:
    unix = filename.replace("\\", "/")
    parts = [p for p in Path(unix).parts if p not in ("", ".", "..")]
    if unix.startswith("/") or (len(unix) > 1 and unix[1] == ":"):
        parts = [p for p in Path(unix).parts if p not in ("/", "\\") and not (len(p) == 2 and p[1] == ":")]
        parts = [p for p in parts if p not in (".", "..")]
    return Path(*parts) if parts else Path("file")


def _fetch_chunk(chunk: ChunkInfo, base_url: str, version: int) -> bytes:
    url = chunk_url(chunk, base_url, version)
    response = requests.get(url, timeout=120)
    if response.status_code >= 400:
        raise DownloadError(f"Chunk fetch failed for {chunk.guid}: HTTP {response.status_code}")
    return decode_chunk_payload(response.content)


def _assemble_file(
    entry: FileEntry,
    chunk_payloads: dict[str, bytes],
    target: Path,
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
        if digest != entry.file_hash.lower():
            raise DownloadError(
                f"SHA1 mismatch for {entry.filename}: got {digest}, want {entry.file_hash.lower()}"
            )

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(buffer)
    return len(buffer)


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

    chunk_payloads: dict[str, bytes] = {}
    guids = list(needed.keys())
    total_chunks = len(guids)
    done_chunks = 0

    def _one(guid: str) -> tuple[str, bytes]:
        return guid, _fetch_chunk(needed[guid], plan.distribution_base_url, plan.manifest.version)

    with ThreadPoolExecutor(max_workers=CHUNK_CONCURRENCY) as pool:
        futures = [pool.submit(_one, g) for g in guids]
        for fut in as_completed(futures):
            guid, payload = fut.result()
            chunk_payloads[guid] = payload
            done_chunks += 1
            if progress:
                progress(f"Chunk {done_chunks}/{total_chunks}", done_chunks, total_chunks)

    written: list[str] = []
    bytes_total = 0
    for i, entry in enumerate(files, start=1):
        rel = _safe_relative(entry.filename)
        if not preserve_structure:
            rel = Path(rel.name)
        dest = target_dir / rel
        nbytes = _assemble_file(entry, chunk_payloads, dest)
        written.append(str(dest))
        bytes_total += nbytes
        if progress:
            progress(f"File {i}/{len(files)}: {rel}", i, len(files))

    marker = target_dir / ".ue-launcher-asset.json"
    marker.write_text(
        f'{{"asset_id": "{plan.asset.asset_id}", "title": {plan.asset.title!r}, '
        f'"artifact_id": "{plan.artifact_id}"}}\n',
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
