"""Bounded static-PNG framing and Cognita iTXt handling.

The module deliberately never inflates image pixels.  It only walks chunk framing,
checks CRCs and reads the single bounded metadata chunk owned by Cognita.
"""

from __future__ import annotations

import json
import struct
import zlib
from dataclasses import dataclass
from typing import Any

from .limits import (
    MAX_DIMENSION,
    MAX_METADATA_BYTES,
    MAX_PIXELS,
    MAX_PNG_BYTES,
    MAX_PNG_CHUNK_BYTES,
    MAX_PNG_CHUNKS,
)
from .models import AssetError, PngFacts

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_VALID_TYPES = set(b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz")


@dataclass(frozen=True, slots=True)
class PngChunk:
    kind: bytes
    data: bytes
    raw: bytes


def _error(reason: str, message: str) -> AssetError:
    return AssetError(reason, message)


def scan_png(
    data: bytes,
    *,
    metadata_limit: int = MAX_METADATA_BYTES,
    byte_limit: int = MAX_PNG_BYTES,
    dimension_limit: int = MAX_DIMENSION,
    pixel_limit: int = MAX_PIXELS,
    chunk_limit: int = MAX_PNG_CHUNKS,
    chunk_byte_limit: int = MAX_PNG_CHUNK_BYTES,
) -> tuple[PngFacts, list[PngChunk]]:
    if not isinstance(data, bytes) or len(data) < 8 or data[:8] != PNG_SIGNATURE:
        raise _error("invalid_png", "file is not a PNG")
    if len(data) > byte_limit:
        raise _error("byte_limit", "PNG exceeds the raw byte limit")
    offset = 8
    chunks: list[PngChunk] = []
    width = height = 0
    ihdr_count = idat_count = iend_count = 0
    cabx_count = 0
    cognita: list[tuple[dict[str, Any], bool]] = []
    while offset < len(data):
        if len(data) - offset < 12:
            raise _error("invalid_png", "PNG chunk is truncated")
        start = offset
        length = struct.unpack_from(">I", data, offset)[0]
        kind = data[offset + 4 : offset + 8]
        offset += 8
        if length > chunk_byte_limit or len(data) - offset < length + 4:
            raise _error("invalid_png", "PNG chunk exceeds its bound")
        payload = data[offset : offset + length]
        expected = struct.unpack_from(">I", data, offset + length)[0]
        actual = zlib.crc32(kind + payload) & 0xFFFFFFFF
        offset += length + 4
        if not all(byte in _VALID_TYPES for byte in kind):
            raise _error("invalid_png", "PNG chunk name is invalid")
        if expected != actual:
            raise _error("invalid_png", "PNG chunk CRC is invalid")
        chunks.append(PngChunk(kind, payload, data[start:offset]))
        if len(chunks) > chunk_limit:
            raise _error("invalid_png", "PNG has too many chunks")
        if len(chunks) == 1 and kind != b"IHDR":
            raise _error("invalid_png", "IHDR must be first")
        if kind == b"IHDR":
            ihdr_count += 1
            if ihdr_count > 1 or len(payload) != 13:
                raise _error("invalid_png", "PNG has an invalid IHDR")
            width, height, depth, color, compression, filtering, interlace = struct.unpack(">IIBBBBB", payload)
            depths = {0: {1, 2, 4, 8, 16}, 2: {8, 16}, 3: {1, 2, 4, 8}, 4: {8, 16}, 6: {8, 16}}
            if width > dimension_limit or height > dimension_limit:
                raise _error(
                    "dimension_limit",
                    f"PNG dimensions exceed the {dimension_limit:,}-pixel per-side limit",
                )
            if width * height > pixel_limit:
                raise _error(
                    "dimension_limit",
                    f"PNG pixel count exceeds the {pixel_limit:,}-pixel limit",
                )
            if (not width or not height or color not in depths
                    or depth not in depths[color] or compression != 0 or filtering != 0
                    or interlace not in (0, 1)):
                raise _error("invalid_png", "PNG IHDR is invalid")
        elif kind == b"IDAT":
            idat_count += 1
        elif kind == b"IEND":
            iend_count += 1
            if payload or iend_count > 1 or offset != len(data):
                raise _error("invalid_png", "PNG has an invalid IEND or trailing data")
        elif kind in (b"acTL", b"fcTL", b"fdAT"):
            raise _error("animated_png", "animated PNGs are not supported")
        elif kind == b"caBX":
            cabx_count += 1
        elif kind == b"iTXt":
            parsed = _parse_cognita_itxt(payload, metadata_limit)
            if parsed is not None:
                cognita.append(parsed)
    if ihdr_count != 1 or idat_count == 0 or iend_count != 1:
        raise _error("invalid_png", "PNG is missing required chunks")
    if len(cognita) > 1:
        raise _error("ambiguous_metadata", "PNG contains multiple Cognita metadata chunks")
    metadata = cognita[0][0] if cognita else None
    compressed = cognita[0][1] if cognita else False
    return PngFacts(width, height, len(chunks), cabx_count, len(cognita), metadata, compressed), chunks


def _parse_cognita_itxt(payload: bytes, limit: int) -> tuple[dict[str, Any], bool] | None:
    # keyword, compression flag, compression method, language tag, translated
    # keyword, then UTF-8 text.  Unknown keywords are not Cognita metadata.
    try:
        keyword_end = payload.index(b"\0")
        keyword = payload[:keyword_end]
        if keyword != b"Cognita":
            return None
        if len(payload) < keyword_end + 3:
            raise ValueError
        if payload[keyword_end + 1] not in (0, 1):
            raise ValueError
        compressed = payload[keyword_end + 1] == 1
        if payload[keyword_end + 2] not in (0,):
            raise ValueError
        pos = keyword_end + 3
        lang_end = payload.index(b"\0", pos)
        pos = lang_end + 1
        translated_end = payload.index(b"\0", pos)
        text = payload[translated_end + 1 :]
        if compressed:
            inflater = zlib.decompressobj()
            text = inflater.decompress(text, limit + 1)
            if len(text) > limit or inflater.unconsumed_tail:
                raise OverflowError
            text += inflater.flush(max(1, limit + 1 - len(text)))
            if len(text) > limit:
                raise OverflowError
            if not inflater.eof or inflater.unused_data:
                raise ValueError
        if len(text) > limit:
            raise OverflowError
        parsed = json.loads(text.decode("utf-8"))
        if not isinstance(parsed, dict) or parsed.get("schema") != "urn:cognita:image-metadata:v1":
            raise AssetError("unsupported_metadata_schema", "Cognita metadata schema is unsupported")
        if parsed.get("schema_version") != 1:
            raise AssetError("unsupported_metadata_schema", "Cognita metadata schema is unsupported")
        return parsed, compressed
    except AssetError:
        raise
    except OverflowError as exc:
        raise AssetError("metadata_limit", "embedded metadata exceeds its limit") from exc
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError, zlib.error) as exc:
        raise AssetError("invalid_png", "Cognita iTXt metadata is invalid") from exc


def _itxt_chunk(metadata: dict[str, Any]) -> bytes:
    text = json.dumps(metadata, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    if len(text) > MAX_METADATA_BYTES:
        raise AssetError("metadata_limit", "embedded metadata exceeds its limit")
    payload = b"Cognita\0\0\0\0\0" + text
    return struct.pack(">I", len(payload)) + b"iTXt" + payload + struct.pack(">I", zlib.crc32(b"iTXt" + payload) & 0xFFFFFFFF)


def embed_metadata(data: bytes, metadata: dict[str, Any]) -> bytes:
    """Replace one Cognita chunk or insert one immediately before IEND."""
    facts, chunks = scan_png(data)
    if facts.cabx_chunk_count:
        raise AssetError("provenance_requires_preserve", "caBX PNGs must remain byte-identical")
    replacement = _itxt_chunk(metadata)
    output = bytearray(PNG_SIGNATURE)
    inserted = False
    for chunk in chunks:
        if chunk.kind == b"iTXt" and _is_cognita_chunk(chunk.data):
            if inserted:
                raise AssetError("ambiguous_metadata", "PNG contains multiple Cognita metadata chunks")
            output.extend(replacement)
            inserted = True
        elif chunk.kind == b"IEND" and not inserted:
            output.extend(replacement)
            output.extend(chunk.raw)
            inserted = True
        else:
            output.extend(chunk.raw)
    if not inserted:
        raise AssetError("invalid_png", "PNG has no IEND")
    return bytes(output)


def _is_cognita_chunk(payload: bytes) -> bool:
    return payload.startswith(b"Cognita\0")


def facts_for(data: bytes) -> PngFacts:
    return scan_png(data)[0]


def validate_static_png(data: bytes) -> dict[str, Any]:
    """Compatibility-shaped validator useful to callers that need a facts map."""
    try:
        facts, _ = scan_png(data)
    except AssetError as exc:
        return {"valid": False, "reason": exc.reason}
    return {"valid": True, "reason": None, "width": facts.width, "height": facts.height,
            "chunk_count": facts.chunk_count, "static": True}
