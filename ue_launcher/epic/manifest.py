"""Epic binary/JSON manifest parser (Fab CDN downloads)."""

from __future__ import annotations

import json
import struct
import zlib
from dataclasses import dataclass, field
from hashlib import sha1


MANIFEST_MAGIC = 0x44BEC00C
CHUNK_MAGIC = 0xB1FE3AA2
STORED_COMPRESSED = 0x1
STORED_ENCRYPTED = 0x2


@dataclass
class ChunkInfo:
    guid: str
    hash_hex: str
    group_number: int
    window_size: int
    file_size: int


@dataclass
class ChunkPart:
    guid: str
    offset: int
    size: int


@dataclass
class FileEntry:
    filename: str
    file_size: int
    file_hash: str
    chunk_parts: list[ChunkPart] = field(default_factory=list)


@dataclass
class ParsedManifest:
    version: int
    app_name: str
    build_version: str
    chunks: dict[str, ChunkInfo]
    files: list[FileEntry]


class _Reader:
    def __init__(self, data: bytes) -> None:
        self.data = data
        self.pos = 0

    def remaining(self) -> int:
        return len(self.data) - self.pos

    def seek(self, absolute: int) -> None:
        self.pos = absolute

    def skip(self, n: int) -> None:
        self.pos += n

    def u8(self) -> int:
        v = self.data[self.pos]
        self.pos += 1
        return v

    def u32(self) -> int:
        (v,) = struct.unpack_from("<I", self.data, self.pos)
        self.pos += 4
        return v

    def i32(self) -> int:
        (v,) = struct.unpack_from("<i", self.data, self.pos)
        self.pos += 4
        return v

    def u64(self) -> int:
        (v,) = struct.unpack_from("<Q", self.data, self.pos)
        self.pos += 8
        return v

    def i64(self) -> int:
        (v,) = struct.unpack_from("<q", self.data, self.pos)
        self.pos += 8
        return v

    def bytes(self, n: int) -> bytes:
        out = self.data[self.pos : self.pos + n]
        self.pos += n
        return out

    def fstring(self) -> str:
        length = self.i32()
        if length == 0:
            return ""
        if length > 0:
            raw = self.bytes(length - 1)
            self.skip(1)
            return raw.decode("utf-8", errors="replace")
        char_count = -length
        raw = self.bytes((char_count - 1) * 2)
        self.skip(2)
        return raw.decode("utf-16-le", errors="replace")


def _guid_to_string(parts: tuple[int, int, int, int]) -> str:
    return "".join(f"{p:08X}" for p in parts)


def _read_guid(reader: _Reader) -> str:
    return _guid_to_string((reader.u32(), reader.u32(), reader.u32(), reader.u32()))


def _bytes_to_hex(data: bytes) -> str:
    return data.hex()


def parse_manifest_bytes(data: bytes) -> ParsedManifest:
    if len(data) < 4:
        raise ValueError("Manifest too short")
    (head,) = struct.unpack_from("<I", data, 0)
    if head == MANIFEST_MAGIC:
        return _parse_binary(data)
    return _parse_json(data)


def _parse_binary(data: bytes) -> ParsedManifest:
    reader = _Reader(data)
    magic = reader.u32()
    if magic != MANIFEST_MAGIC:
        raise ValueError(f"Bad manifest magic: 0x{magic:x}")
    header_size = reader.u32()
    reader.u32()  # size uncompressed
    reader.u32()  # size compressed
    sha_header = reader.bytes(20)
    stored_as = reader.u8()
    version = reader.u32()

    if stored_as & STORED_ENCRYPTED:
        raise ValueError("Encrypted manifests are not supported")

    if version >= 22:
        reader.skip(32)

    if reader.pos != header_size:
        reader.seek(header_size)

    body = reader.bytes(reader.remaining())
    if stored_as & STORED_COMPRESSED:
        body = zlib.decompress(body)
        if sha1(body).digest() != sha_header:
            raise ValueError("Manifest body SHA1 mismatch")

    return _read_body(body, version)


def _read_body(body: bytes, version: int) -> ParsedManifest:
    reader = _Reader(body)
    meta_start = reader.pos
    meta_size = reader.u32()
    data_version = reader.u8()
    reader.u32()  # feature level
    reader.u8()  # is file data
    reader.u32()  # app id
    app_name = reader.fstring()
    build_version = reader.fstring()
    reader.fstring()  # launch exe
    reader.fstring()  # launch command
    prereq_count = reader.u32()
    for _ in range(prereq_count):
        reader.fstring()
    reader.fstring()
    reader.fstring()
    reader.fstring()
    if data_version >= 1:
        reader.fstring()
    if data_version >= 2:
        reader.fstring()
        reader.fstring()
    consumed = reader.pos - meta_start
    if consumed < meta_size:
        reader.skip(meta_size - consumed)

    # Chunk data list
    cdl_start = reader.pos
    cdl_size = reader.u32()
    reader.u8()
    count = reader.u32()
    guids = [_read_guid(reader) for _ in range(count)]
    hashes = [reader.u64() for _ in range(count)]
    for _ in range(count):
        reader.bytes(20)  # sha
    groups = [reader.u8() for _ in range(count)]
    windows = [reader.u32() for _ in range(count)]
    sizes = [reader.i64() for _ in range(count)]
    if version >= 22:
        reader.skip(count * 16)
        reader.skip(count * 4)
        reader.skip(count * 16)
    cdl_consumed = reader.pos - cdl_start
    if cdl_consumed < cdl_size:
        reader.skip(cdl_size - cdl_consumed)

    chunks: dict[str, ChunkInfo] = {}
    for i, guid in enumerate(guids):
        chunks[guid] = ChunkInfo(
            guid=guid,
            hash_hex=f"{hashes[i]:016X}",
            group_number=groups[i],
            window_size=windows[i],
            file_size=int(sizes[i]),
        )

    # File manifest list
    fml_start = reader.pos
    fml_size = reader.u32()
    fml_version = reader.u8()
    fcount = reader.u32()
    filenames = [reader.fstring() for _ in range(fcount)]
    for _ in range(fcount):
        reader.fstring()  # symlink
    file_hashes = [reader.bytes(20) for _ in range(fcount)]
    for _ in range(fcount):
        reader.u8()  # flags
    for _ in range(fcount):
        tag_count = reader.u32()
        for _ in range(tag_count):
            reader.fstring()

    files: list[FileEntry] = []
    for i in range(fcount):
        part_count = reader.u32()
        parts: list[ChunkPart] = []
        for _ in range(part_count):
            part_start = reader.pos
            part_size = reader.u32()
            guid = _read_guid(reader)
            offset = reader.u32()
            size = reader.u32()
            parts.append(ChunkPart(guid=guid, offset=offset, size=size))
            part_consumed = reader.pos - part_start
            if part_consumed < part_size:
                reader.skip(part_size - part_consumed)
        file_size = sum(p.size for p in parts)
        files.append(
            FileEntry(
                filename=filenames[i],
                file_size=file_size,
                file_hash=_bytes_to_hex(file_hashes[i]),
                chunk_parts=parts,
            )
        )

    if fml_version >= 1:
        for _ in range(fcount):
            if reader.u32():
                reader.skip(16)
        for _ in range(fcount):
            reader.fstring()
    if fml_version >= 2:
        reader.skip(fcount * 32)

    fml_consumed = reader.pos - fml_start
    if fml_consumed < fml_size:
        reader.skip(fml_size - fml_consumed)

    return ParsedManifest(
        version=version,
        app_name=app_name,
        build_version=build_version,
        chunks=chunks,
        files=files,
    )


def _blob_to_int(blob: str) -> int:
    result = 0
    shift = 0
    for i in range(0, len(blob), 3):
        byte = int(blob[i : i + 3] or "0")
        result += byte << shift
        shift += 8
    return result


def _guid_from_json_hex(hex_str: str) -> str:
    parts = (
        int(hex_str[0:8], 16),
        int(hex_str[8:16], 16),
        int(hex_str[16:24], 16),
        int(hex_str[24:32], 16),
    )
    return _guid_to_string(parts)


def _parse_json(data: bytes) -> ParsedManifest:
    payload = json.loads(data.decode("utf-8"))
    version = int(_blob_to_int(payload.get("ManifestFileVersion", "013000000000")))
    cfl = payload.get("ChunkFilesizeList") or {}
    chl = payload.get("ChunkHashList") or {}
    dgl = payload.get("DataGroupList") or {}

    chunks: dict[str, ChunkInfo] = {}
    for guid_hex, size_blob in cfl.items():
        guid = _guid_from_json_hex(guid_hex)
        chunks[guid] = ChunkInfo(
            guid=guid,
            hash_hex=f"{_blob_to_int(chl.get(guid_hex, '0')):016X}",
            group_number=_blob_to_int(dgl.get(guid_hex, "0")),
            window_size=1024 * 1024,
            file_size=_blob_to_int(size_blob),
        )

    files: list[FileEntry] = []
    for entry in payload.get("FileManifestList") or []:
        parts = [
            ChunkPart(
                guid=_guid_from_json_hex(cp.get("Guid", "")),
                offset=_blob_to_int(cp.get("Offset", "0")),
                size=_blob_to_int(cp.get("Size", "0")),
            )
            for cp in (entry.get("FileChunkParts") or [])
        ]
        # JSON FileHash is blob-encoded little-endian 160-bit; reverse to canonical SHA1 hex
        hash_int = _blob_to_int(entry.get("FileHash", "0"))
        hash_le = hash_int.to_bytes(20, "little", signed=False)
        file_hash = hash_le[::-1].hex()
        files.append(
            FileEntry(
                filename=entry.get("Filename") or "",
                file_size=sum(p.size for p in parts),
                file_hash=file_hash,
                chunk_parts=parts,
            )
        )

    return ParsedManifest(
        version=version,
        app_name=payload.get("AppNameString") or "",
        build_version=payload.get("BuildVersionString") or "",
        chunks=chunks,
        files=files,
    )


def chunk_dir_for_version(version: int) -> str:
    if version >= 22:
        return "ChunksV5"
    if version >= 15:
        return "ChunksV4"
    if version >= 6:
        return "ChunksV3"
    if version >= 3:
        return "ChunksV2"
    return "Chunks"


def chunk_url(chunk: ChunkInfo, distribution_base_url: str, manifest_version: int) -> str:
    directory = chunk_dir_for_version(manifest_version)
    group = f"{chunk.group_number:02d}"
    base = distribution_base_url.rstrip("/")
    return f"{base}/{directory}/{group}/{chunk.hash_hex}_{chunk.guid}.chunk"


def decode_chunk_payload(chunk_bytes: bytes) -> bytes:
    reader = _Reader(chunk_bytes)
    magic = reader.u32()
    if magic != CHUNK_MAGIC:
        raise ValueError(f"Bad chunk magic: 0x{magic:x}")
    reader.u32()  # header version
    header_size = reader.u32()
    reader.u32()  # compressed size
    reader.skip(16)  # guid
    reader.skip(8)  # hash
    stored_as = reader.u8()
    if stored_as & STORED_ENCRYPTED:
        raise ValueError("Encrypted chunks are not supported")
    payload = chunk_bytes[header_size:]
    if stored_as & STORED_COMPRESSED:
        return zlib.decompress(payload)
    return payload
