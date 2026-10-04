"""Disposable Phase 0 generated-image handoff probe.

This module is intentionally independent of Cognita's production gateway and
storage layers.  It accepts only bounded static PNG probes, keeps redacted facts
in memory, and removes every received payload from its temporary file before a
request completes.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import hmac
import json
import logging
import os
import secrets
import tempfile
import time
import zlib
from collections import OrderedDict
from collections.abc import Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from starlette.requests import ClientDisconnect

log = logging.getLogger("cognita.asset_probe")

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
PNG_MEDIA_TYPE = "image/png"
PROBE_PROTOCOL_VERSION = "2025-03-26"
UPLOAD_TTL_SECONDS = 5 * 60
MAX_PNG_BYTES = 4 * 1024 * 1024
MAX_INLINE_BASE64_CHARS = ((MAX_PNG_BYTES + 2) // 3) * 4
MAX_PROMPT_BYTES = 16 * 1024
MAX_MCP_BODY_BYTES = MAX_INLINE_BASE64_CHARS + 128 * 1024
PNG_DATA_URL_PREFIX = "data:image/png;base64,"
MAX_PNG_CHUNKS = 4096
MAX_PNG_CHUNK_BYTES = MAX_PNG_BYTES
MAX_DIMENSION = 32_768
MAX_PIXELS = 268_435_456
MAX_RESULTS = 128
MAX_ARTIFACT_NODES = 256
MAX_ARTIFACT_DEPTH = 6
MAX_ARTIFACT_FIELD_LENGTH = 128

TOOL_INLINE = "probe_inline_png"
TOOL_ARTIFACT = "probe_artifact_reference"
TOOL_PREPARE = "prepare_probe_upload"
TOOL_RESULT = "get_probe_result"


class ProbeInputError(ValueError):
    """A bounded, client-visible probe argument error."""

    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason
        self.message = message


class ProbeNotFound(KeyError):
    """A result or capability that is not available."""


def _now_iso(now: float) -> str:
    return datetime.fromtimestamp(now, tz=UTC).isoformat()


def _prompt_facts(prompt: Any) -> dict[str, Any]:
    """Hash prompt text immediately and retain no prompt content."""
    if prompt is None:
        return {"prompt_present": False, "prompt_length": 0, "prompt_sha256": None}
    if not isinstance(prompt, str):
        raise ProbeInputError("invalid_prompt", "prompt must be a string when provided")
    encoded = prompt.encode("utf-8")
    if len(encoded) > MAX_PROMPT_BYTES:
        raise ProbeInputError("prompt_limit", "prompt exceeds the probe limit")
    return {
        "prompt_present": True,
        "prompt_length": len(encoded),
        "prompt_sha256": hashlib.sha256(encoded).hexdigest(),
    }


def _validate_expected_facts(
    arguments: Mapping[str, Any], *, required: bool
) -> tuple[int | None, str | None]:
    size = arguments.get("expected_size")
    digest = arguments.get("expected_sha256")
    if not required and size is None and digest is None:
        return None, None
    if required and size is None:
        raise ProbeInputError("invalid_expected_size", "expected_size is required")
    if size is not None and (
        not isinstance(size, int) or isinstance(size, bool) or size < 1 or size > MAX_PNG_BYTES
    ):
        raise ProbeInputError(
            "invalid_expected_size", "expected_size must be within the PNG byte limit"
        )
    if required and digest is None:
        raise ProbeInputError("invalid_expected_hash", "expected_sha256 is required")
    if digest is not None and (not isinstance(digest, str) or len(digest) != 64):
        raise ProbeInputError(
            "invalid_expected_hash", "expected_sha256 must be a SHA-256 hex digest"
        )
    if digest is not None:
        try:
            int(digest, 16)
        except ValueError as exc:
            raise ProbeInputError(
                "invalid_expected_hash", "expected_sha256 must be a SHA-256 hex digest"
            ) from exc
        digest = digest.lower()
    return size, digest


def _validate_declared_media_type(arguments: Mapping[str, Any]) -> str:
    media_type = arguments.get("media_type", PNG_MEDIA_TYPE)
    if not isinstance(media_type, str) or media_type != PNG_MEDIA_TYPE:
        raise ProbeInputError("unsupported_media_type", "only image/png is accepted")
    return media_type


def _validate_filename(arguments: Mapping[str, Any]) -> None:
    filename = arguments.get("filename")
    if filename is not None and (not isinstance(filename, str) or len(filename) > 255):
        raise ProbeInputError("invalid_filename", "filename must be a bounded string")


def _validation_error(reason: str, *, detail: str | None = None) -> dict[str, Any]:
    return {"reason": reason, "detail": detail or reason}


def validate_static_png(data: bytes) -> dict[str, Any]:
    """Validate PNG framing and static-image structure without decoding pixels."""
    if len(data) < len(PNG_SIGNATURE) or data[:8] != PNG_SIGNATURE:
        return _validation_error("invalid_signature")

    offset = len(PNG_SIGNATURE)
    chunks = 0
    ihdr_count = 0
    idat_count = 0
    iend_count = 0
    saw_iend = False
    width = height = None
    while offset < len(data):
        if saw_iend:
            return _validation_error("trailing_data")
        if len(data) - offset < 12:
            return _validation_error("truncated_chunk")
        chunk_length = int.from_bytes(data[offset : offset + 4], "big")
        chunk_type = data[offset + 4 : offset + 8]
        offset += 8
        if chunk_length > MAX_PNG_CHUNK_BYTES or len(data) - offset < chunk_length + 4:
            return _validation_error("invalid_chunk_length")
        chunk_data = data[offset : offset + chunk_length]
        expected_crc = int.from_bytes(
            data[offset + chunk_length : offset + chunk_length + 4], "big"
        )
        actual_crc = zlib.crc32(chunk_type + chunk_data) & 0xFFFFFFFF
        offset += chunk_length + 4
        chunks += 1
        if chunks > MAX_PNG_CHUNKS:
            return _validation_error("chunk_limit")
        if not all(65 <= byte <= 90 or 97 <= byte <= 122 for byte in chunk_type):
            return _validation_error("invalid_chunk_type")
        if expected_crc != actual_crc:
            return _validation_error("crc_mismatch")

        if chunks == 1 and chunk_type != b"IHDR":
            return _validation_error("ihdr_not_first")
        if chunk_type == b"IHDR":
            ihdr_count += 1
            if ihdr_count > 1 or len(chunk_data) != 13:
                return _validation_error("invalid_ihdr")
            width = int.from_bytes(chunk_data[0:4], "big")
            height = int.from_bytes(chunk_data[4:8], "big")
            bit_depth, color_type = chunk_data[8], chunk_data[9]
            compression, filter_method, interlace = chunk_data[10:13]
            valid_depths = {
                0: {1, 2, 4, 8, 16},
                2: {8, 16},
                3: {1, 2, 4, 8},
                4: {8, 16},
                6: {8, 16},
            }
            if (
                not width
                or not height
                or width > MAX_DIMENSION
                or height > MAX_DIMENSION
                or width * height > MAX_PIXELS
                or color_type not in valid_depths
                or bit_depth not in valid_depths.get(color_type, set())
                or compression != 0
                or filter_method != 0
                or interlace not in (0, 1)
            ):
                return _validation_error("invalid_ihdr")
        elif chunk_type == b"IDAT":
            idat_count += 1
        elif chunk_type == b"IEND":
            iend_count += 1
            if len(chunk_data) != 0 or iend_count > 1:
                return _validation_error("invalid_iend")
            saw_iend = True
        elif chunk_type in (b"acTL", b"fcTL", b"fdAT"):
            return _validation_error("animated_png")

    if ihdr_count != 1:
        return _validation_error("missing_ihdr")
    if not idat_count:
        return _validation_error("missing_idat")
    if iend_count != 1 or not saw_iend:
        return _validation_error("missing_iend")
    return {
        "valid": True,
        "reason": None,
        "width": width,
        "height": height,
        "chunk_count": chunks,
        "static": True,
    }


def _safe_field_name(value: Any) -> str:
    if not isinstance(value, str):
        return "<non-string>"
    text = "".join(char if char.isprintable() else "?" for char in value)
    return text[:MAX_ARTIFACT_FIELD_LENGTH]


def summarize_artifact(value: Any) -> dict[str, Any]:
    """Describe an opaque artifact shape without retaining or returning values."""
    nodes = 0

    def visit(item: Any, depth: int) -> dict[str, Any]:
        nonlocal nodes
        nodes += 1
        if nodes > MAX_ARTIFACT_NODES:
            raise ProbeInputError("artifact_too_large", "artifact shape exceeds the probe limit")
        if depth > MAX_ARTIFACT_DEPTH:
            return {"type": "nested", "depth_limited": True}
        if item is None:
            return {"type": "null"}
        if isinstance(item, bool):
            return {"type": "boolean"}
        if isinstance(item, str):
            return {"type": "string", "length": len(item)}
        if isinstance(item, (int, float)):
            return {"type": "number"}
        if isinstance(item, list):
            if len(item) > MAX_ARTIFACT_NODES:
                raise ProbeInputError(
                    "artifact_too_large", "artifact array exceeds the probe limit"
                )
            return {
                "type": "array",
                "length": len(item),
                "item_types": sorted({visit(child, depth + 1)["type"] for child in item}),
            }
        if isinstance(item, dict):
            if len(item) > MAX_ARTIFACT_NODES:
                raise ProbeInputError(
                    "artifact_too_large", "artifact object exceeds the probe limit"
                )
            return {
                "type": "object",
                "field_names": [_safe_field_name(key) for key in item],
                "field_count": len(item),
                "fields": {
                    _safe_field_name(key): visit(child, depth + 1) for key, child in item.items()
                },
            }
        raise ProbeInputError("invalid_artifact", "artifact must be JSON data")

    return visit(value, 0)


@dataclass
class ProbeResult:
    attempt_id: str
    transport: str
    started_at: str
    completed_at: str | None = None
    received_byte_count: int = 0
    received_sha256: str | None = None
    png_valid: bool | None = None
    png_reason: str | None = None
    png_width: int | None = None
    png_height: int | None = None
    png_chunk_count: int | None = None
    prompt_present: bool = False
    prompt_length: int = 0
    prompt_sha256: str | None = None
    failure_reason: str | None = None
    artifact_shape: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        payload = {
            "attempt_id": self.attempt_id,
            "transport": self.transport,
            "received_byte_count": self.received_byte_count,
            "received_sha256": self.received_sha256,
            "png_valid": self.png_valid,
            "png_reason": self.png_reason,
            "png_width": self.png_width,
            "png_height": self.png_height,
            "png_chunk_count": self.png_chunk_count,
            "prompt_present": self.prompt_present,
            "prompt_length": self.prompt_length,
            "prompt_sha256": self.prompt_sha256,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "failure_reason": self.failure_reason,
        }
        if self.artifact_shape is not None:
            payload["artifact_shape"] = self.artifact_shape
        return payload


@dataclass
class UploadTicket:
    attempt_id: str
    token_digest: bytes
    expected_size: int
    expected_sha256: str
    media_type: str
    prompt: dict[str, Any]
    expires_at: float
    state: str = "prepared"
    receiving: bool = False


class ProbeState:
    """In-memory tickets/results and disposable staging lifecycle."""

    def __init__(
        self,
        *,
        root_capability: str | None = None,
        clock: Callable[[], float] = time.time,
        temp_dir: Path | None = None,
    ):
        self.root_capability = root_capability or secrets.token_urlsafe(32)
        self._clock = clock
        self._temp_dir = Path(temp_dir) if temp_dir is not None else None
        if self._temp_dir is not None:
            self._temp_dir.mkdir(parents=True, exist_ok=True)
        self._tickets: dict[bytes, UploadTicket] = {}
        self._results: OrderedDict[str, tuple[float, ProbeResult]] = OrderedDict()
        self._active_paths: set[Path] = set()
        self._lock = asyncio.Lock()

    def _cleanup_expired(self, now: float) -> None:
        for token, ticket in list(self._tickets.items()):
            if ticket.expires_at <= now:
                self._tickets.pop(token, None)
                result = self._results.get(ticket.attempt_id)
                if result is not None and result[1].completed_at is None:
                    result[1].completed_at = _now_iso(now)
                    result[1].failure_reason = "ticket_expired"
                    self._results.move_to_end(ticket.attempt_id)
                log.info("asset probe upload expired outcome=ticket_expired")
        cutoff = now - UPLOAD_TTL_SECONDS
        for attempt_id, (created, _result) in list(self._results.items()):
            if created < cutoff:
                self._drop_result(attempt_id)
        while len(self._results) > MAX_RESULTS:
            attempt_id = next(iter(self._results))
            self._drop_result(attempt_id)

    def _drop_result(self, attempt_id: str) -> None:
        """Remove a result and every ticket that could otherwise orphan it."""
        self._results.pop(attempt_id, None)
        for token_digest, ticket in list(self._tickets.items()):
            if ticket.attempt_id == attempt_id:
                self._tickets.pop(token_digest, None)

    def _new_result(self, transport: str, prompt: dict[str, Any]) -> ProbeResult:
        now = self._clock()
        result = ProbeResult(
            attempt_id=secrets.token_urlsafe(16),
            transport=transport,
            started_at=_now_iso(now),
            prompt_present=prompt["prompt_present"],
            prompt_length=prompt["prompt_length"],
            prompt_sha256=prompt["prompt_sha256"],
        )
        self._results[result.attempt_id] = (now, result)
        self._cleanup_expired(now)
        return result

    def create_upload(
        self, *, expected_size: int, expected_sha256: str, media_type: str, prompt: dict[str, Any]
    ) -> tuple[str, ProbeResult, float]:
        now = self._clock()
        result = self._new_result("direct_put", prompt)
        token = secrets.token_urlsafe(32)
        token_digest = hashlib.sha256(token.encode("ascii")).digest()
        self._tickets[token_digest] = UploadTicket(
            attempt_id=result.attempt_id,
            token_digest=token_digest,
            expected_size=expected_size,
            expected_sha256=expected_sha256,
            media_type=media_type,
            prompt=prompt,
            expires_at=now + UPLOAD_TTL_SECONDS,
        )
        log.info("asset probe upload prepared transport=direct_put expected_size=%d", expected_size)
        return token, result, now + UPLOAD_TTL_SECONDS

    def get_result(self, attempt_id: str) -> ProbeResult:
        self._cleanup_expired(self._clock())
        try:
            return self._results[attempt_id][1]
        except KeyError as exc:
            raise ProbeNotFound from exc

    def _stage_path(self) -> tuple[int, Path]:
        fd, name = tempfile.mkstemp(prefix="cognita-probe-", suffix=".png", dir=self._temp_dir)
        path = Path(name)
        self._active_paths.add(path)
        return fd, path

    def _finish_path(self, path: Path) -> bool:
        try:
            path.unlink()
            self._active_paths.discard(path)
            return True
        except FileNotFoundError:
            self._active_paths.discard(path)
            return True
        except OSError:
            # Keep tracking it so shutdown can retry instead of forgetting a payload.
            return False

    def close(self) -> None:
        for path in list(self._active_paths):
            self._finish_path(path)
        self._tickets.clear()
        self._results.clear()

    def _complete_from_file(
        self,
        result: ProbeResult,
        path: Path,
        *,
        expected_size: int | None,
        expected_sha256: str | None,
    ) -> None:
        data = path.read_bytes()
        result.received_byte_count = len(data)
        result.received_sha256 = hashlib.sha256(data).hexdigest()
        if expected_size is not None and len(data) != expected_size:
            result.failure_reason = "size_mismatch"
        elif expected_sha256 is not None and not hmac.compare_digest(
            result.received_sha256, expected_sha256
        ):
            result.failure_reason = "hash_mismatch"
        else:
            png = validate_static_png(data)
            result.png_valid = png.get("valid", False)
            result.png_reason = png.get("reason")
            result.png_width = png.get("width")
            result.png_height = png.get("height")
            result.png_chunk_count = png.get("chunk_count")
            if not result.png_valid:
                result.failure_reason = png["reason"]
        result.completed_at = _now_iso(self._clock())

    def receive_inline(
        self,
        *,
        data_base64: str,
        expected_size: int | None,
        expected_sha256: str | None,
        prompt: dict[str, Any],
        transport: str = "inline",
    ) -> ProbeResult:
        result = self._new_result(transport, prompt)
        fd, path = self._stage_path()
        try:
            if len(data_base64) > MAX_INLINE_BASE64_CHARS:
                result.failure_reason = "encoded_limit"
                result.completed_at = _now_iso(self._clock())
                return result
            if len(data_base64) % 4:
                result.failure_reason = "invalid_base64"
                result.completed_at = _now_iso(self._clock())
                return result
            padding_at = data_base64.find("=")
            if padding_at != -1 and padding_at < len(data_base64) - 2:
                result.failure_reason = "invalid_base64"
                result.completed_at = _now_iso(self._clock())
                return result
            decoded_count = 0
            with os.fdopen(fd, "wb") as staged:
                fd = -1
                try:
                    # 64 KiB is divisible by four, so only the final block can
                    # contain base64 padding. Decoded bytes never accumulate in RAM.
                    for offset in range(0, len(data_base64), 64 * 1024):
                        block = base64.b64decode(
                            data_base64[offset : offset + 64 * 1024], validate=True
                        )
                        decoded_count += len(block)
                        if decoded_count > MAX_PNG_BYTES:
                            result.failure_reason = "byte_limit"
                            result.completed_at = _now_iso(self._clock())
                            return result
                        staged.write(block)
                except (binascii.Error, TypeError, ValueError):
                    result.failure_reason = "invalid_base64"
                    result.completed_at = _now_iso(self._clock())
                    return result
            self._complete_from_file(
                result, path, expected_size=expected_size, expected_sha256=expected_sha256
            )
            return result
        finally:
            if fd != -1:
                os.close(fd)
            cleaned = self._finish_path(path)
            log.info(
                "asset probe completed transport=%s size=%d sha256=%s outcome=%s cleanup=%s",
                result.transport,
                result.received_byte_count,
                result.received_sha256 or "none",
                result.failure_reason or "success",
                cleaned,
            )

    async def receive_direct(
        self,
        request: Request,
        token: str,
    ) -> ProbeResult:
        async with self._lock:
            now = self._clock()
            self._cleanup_expired(now)
            token_digest = hashlib.sha256(token.encode("utf-8")).digest()
            ticket = self._tickets.get(token_digest)
            if ticket is None:
                raise ProbeInputError(
                    "invalid_upload_capability", "upload capability is invalid or expired"
                )
            if ticket.state != "prepared" or ticket.receiving:
                raise ProbeInputError("ticket_replayed", "upload capability has already been used")
            content_type = request.headers.get("content-type")
            if (
                not content_type
                or content_type.split(";", 1)[0].strip().lower() != ticket.media_type
            ):
                ticket.state = "rejected"
                result = self.get_result(ticket.attempt_id)
                result.failure_reason = "media_type_mismatch"
                result.completed_at = _now_iso(now)
                raise ProbeInputError("media_type_mismatch", "content type must be image/png")
            content_length = request.headers.get("content-length")
            if content_length is not None and (
                not content_length.isdigit() or int(content_length) > MAX_PNG_BYTES
            ):
                ticket.state = "rejected"
                result = self.get_result(ticket.attempt_id)
                result.failure_reason = "byte_limit"
                result.completed_at = _now_iso(now)
                raise ProbeInputError("byte_limit", "request body exceeds the PNG byte limit")
            ticket.receiving = True
            result = self.get_result(ticket.attempt_id)
        fd = -1
        path: Path | None = None
        count = 0
        digest = hashlib.sha256()
        try:
            fd, path = self._stage_path()
            with os.fdopen(fd, "wb") as staged:
                fd = -1
                async for chunk in request.stream():
                    if not chunk:
                        continue
                    count += len(chunk)
                    if count > MAX_PNG_BYTES:
                        result.failure_reason = "byte_limit"
                        break
                    digest.update(chunk)
                    staged.write(chunk)
            result.received_byte_count = count
            result.received_sha256 = None if result.failure_reason else digest.hexdigest()
            if result.failure_reason is None and count != ticket.expected_size:
                result.failure_reason = "size_mismatch"
            elif result.failure_reason is None and not hmac.compare_digest(
                result.received_sha256, ticket.expected_sha256
            ):
                result.failure_reason = "hash_mismatch"
            elif result.failure_reason is None:
                data = path.read_bytes()
                png = validate_static_png(data)
                result.png_valid = png.get("valid", False)
                result.png_reason = png.get("reason")
                result.png_width = png.get("width")
                result.png_height = png.get("height")
                result.png_chunk_count = png.get("chunk_count")
                if not result.png_valid:
                    result.failure_reason = png["reason"]
            result.completed_at = _now_iso(self._clock())
            ticket.state = "uploaded" if result.failure_reason is None else "rejected"
            log.info(
                "asset probe completed transport=direct_put size=%d sha256=%s outcome=%s cleanup=pending",
                result.received_byte_count,
                result.received_sha256 or "none",
                result.failure_reason or "success",
            )
            return result
        except (asyncio.CancelledError, ClientDisconnect):
            result.failure_reason = "client_disconnected"
            result.completed_at = _now_iso(self._clock())
            ticket.state = "rejected"
            log.info(
                "asset probe completed transport=direct_put outcome=client_disconnected cleanup=pending"
            )
            raise
        except Exception:
            result.failure_reason = "receive_failed"
            result.completed_at = _now_iso(self._clock())
            ticket.state = "rejected"
            log.warning("asset probe completed transport=direct_put outcome=receive_failed")
            raise
        finally:
            if fd != -1:
                os.close(fd)
            cleaned = self._finish_path(path) if path is not None else True
            ticket.receiving = False
            log.info("asset probe cleanup transport=direct_put cleanup=%s", cleaned)


def _tool_def(
    name: str,
    description: str,
    properties: dict[str, Any],
    required: list[str],
    *,
    meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    definition = {
        "name": name,
        "description": description,
        "inputSchema": {
            "type": "object",
            "properties": properties,
            "required": required,
            "additionalProperties": False,
        },
    }
    if meta is not None:
        definition["_meta"] = meta
    return definition


TOOL_DEFS = [
    _tool_def(
        TOOL_INLINE,
        "Phase 0 only: send a bounded static PNG as base64; received bytes are validated and discarded.",
        {
            "data_base64": {"type": "string", "maxLength": MAX_INLINE_BASE64_CHARS},
            "filename": {"type": "string", "maxLength": 255},
            "media_type": {"type": "string", "enum": [PNG_MEDIA_TYPE]},
            "prompt": {"type": "string", "maxLength": MAX_PROMPT_BYTES},
            "expected_size": {"type": "integer", "minimum": 1, "maximum": MAX_PNG_BYTES},
            "expected_sha256": {"type": "string", "pattern": "^[0-9a-fA-F]{64}$"},
        },
        ["data_base64"],
    ),
    _tool_def(
        TOOL_ARTIFACT,
        "Phase 0 only: receive a native generated-image result containing a PNG data URL; "
        "validate and discard the exact bytes without returning encoded content.",
        {
            "artifact": {
                "type": "object",
                "properties": {
                    "image_url": {
                        "type": "string",
                        "maxLength": MAX_INLINE_BASE64_CHARS + len(PNG_DATA_URL_PREFIX),
                    },
                    "output_hint": {"type": "string", "maxLength": MAX_PROMPT_BYTES},
                },
                "required": ["image_url"],
                "additionalProperties": False,
            },
            "filename": {"type": "string", "maxLength": 255},
            "media_type": {"type": "string", "enum": [PNG_MEDIA_TYPE]},
        },
        ["artifact"],
    ),
    _tool_def(
        TOOL_PREPARE,
        "Phase 0 only: prepare a one-time direct PUT for a bounded static PNG; no project is touched.",
        {
            "expected_size": {"type": "integer", "minimum": 1, "maximum": MAX_PNG_BYTES},
            "expected_sha256": {"type": "string", "pattern": "^[0-9a-fA-F]{64}$"},
            "filename": {"type": "string", "maxLength": 255},
            "media_type": {"type": "string", "enum": [PNG_MEDIA_TYPE]},
            "prompt": {"type": "string", "maxLength": MAX_PROMPT_BYTES},
        },
        ["expected_size", "expected_sha256"],
    ),
    _tool_def(
        TOOL_RESULT,
        "Phase 0 only: retrieve bounded redacted facts for one probe attempt; never returns payloads or secrets.",
        {"attempt_id": {"type": "string", "maxLength": 64}},
        ["attempt_id"],
    ),
]
TOOL_DEFS_BY_NAME = {tool["name"]: tool for tool in TOOL_DEFS}


def _validate_tool_arguments(name: str, arguments: Mapping[str, Any]) -> None:
    """Apply the advertised required/closed argument contract at the wire boundary."""
    schema = TOOL_DEFS_BY_NAME[name]["inputSchema"]
    missing = [key for key in schema.get("required", []) if key not in arguments]
    if missing:
        raise ProbeInputError("missing_argument", f"required argument is missing: {missing[0]}")
    unknown = sorted(set(arguments) - set(schema.get("properties", {})))
    if unknown:
        raise ProbeInputError("unknown_argument", f"unknown argument: {unknown[0]}")


def _rpc_error(msg_id: Any, code: int, message: str) -> JSONResponse:
    return JSONResponse(
        {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}
    )


def _tool_response(msg_id: Any, payload: dict[str, Any], *, error: bool = False) -> JSONResponse:
    return JSONResponse(
        {
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": {
                "content": [{"type": "text", "text": json.dumps(payload)}],
                "isError": error,
            },
        }
    )


def _tool_payload(result: ProbeResult) -> dict[str, Any]:
    payload = result.as_dict()
    payload["status"] = "error" if result.failure_reason else "success"
    return payload


async def _bounded_request_body(request: Request) -> bytes:
    """Read MCP JSON under a hard cap without buffering an unbounded body."""
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > MAX_MCP_BODY_BYTES:
            raise ProbeInputError("body_limit", "request body exceeds the probe limit")
    return bytes(body)


def _origin(request: Request, public_origin: str | None) -> str:
    if public_origin:
        return public_origin
    forwarded = request.headers.get("x-forwarded-proto")
    scheme = forwarded.split(",", 1)[0].strip() if forwarded else request.url.scheme
    return f"{scheme}://{request.headers.get('host', request.url.netloc)}"


def _validated_public_origin(public_origin: str | None) -> str | None:
    """Accept only a credential-free HTTPS tunnel base URL."""
    if public_origin is None:
        return None
    parsed = urlsplit(public_origin)
    if (
        parsed.scheme.lower() != "https"
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(
            "public origin must be a credential-free HTTPS URL without query or fragment"
        )
    return public_origin.rstrip("/")


def create_probe_app(
    state: ProbeState | None = None, *, public_origin: str | None = None
) -> FastAPI:
    """Create the disposable app without loading Cognita production services."""
    state = state or ProbeState()
    public_origin = _validated_public_origin(public_origin)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        try:
            yield
        finally:
            state.close()

    app = FastAPI(
        title="Cognita Phase 0 Asset Probe",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )

    def authorized(request: Request, capability: str) -> bool:
        return hmac.compare_digest(capability, state.root_capability)

    @app.post("/mcp/KEI")
    async def mcp(request: Request) -> Response:
        content_length = request.headers.get("content-length")
        if content_length and (
            not content_length.isdigit() or int(content_length) > MAX_MCP_BODY_BYTES
        ):
            return _rpc_error(None, -32600, "request body exceeds the probe limit")
        try:
            raw = await _bounded_request_body(request)
            message = json.loads(raw)
        except ProbeInputError as exc:
            return _rpc_error(None, -32600, exc.message)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return _rpc_error(None, -32700, "invalid JSON")
        if (
            not isinstance(message, dict)
            or message.get("jsonrpc") != "2.0"
            or "method" not in message
        ):
            return _rpc_error(
                message.get("id") if isinstance(message, dict) else None,
                -32600,
                "invalid JSON-RPC request",
            )
        msg_id = message.get("id")
        method = message["method"]
        if method == "notifications/initialized":
            return Response(status_code=202)
        if method == "initialize":
            return JSONResponse(
                {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "result": {
                        "protocolVersion": PROBE_PROTOCOL_VERSION,
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": "cognita-asset-probe", "version": "0.1.0-poc"},
                    },
                }
            )
        if method == "tools/list":
            return JSONResponse({"jsonrpc": "2.0", "id": msg_id, "result": {"tools": TOOL_DEFS}})
        if method != "tools/call":
            return _rpc_error(msg_id, -32601, "method not found")
        params = message.get("params")
        if not isinstance(params, dict) or not isinstance(params.get("name"), str):
            return _rpc_error(msg_id, -32602, "tools/call parameters are invalid")
        name = params["name"]
        arguments = params.get("arguments", {})
        if not isinstance(arguments, dict):
            return _rpc_error(msg_id, -32602, "tool arguments must be an object")
        if name not in TOOL_DEFS_BY_NAME:
            return _rpc_error(msg_id, -32602, "unknown probe tool")
        try:
            _validate_tool_arguments(name, arguments)
            if name == TOOL_INLINE:
                data_base64 = arguments.get("data_base64")
                if not isinstance(data_base64, str):
                    raise ProbeInputError("invalid_base64", "data_base64 is required")
                _validate_filename(arguments)
                _validate_declared_media_type(arguments)
                expected_size, expected_sha256 = _validate_expected_facts(arguments, required=False)
                prompt = _prompt_facts(arguments.get("prompt"))
                result = state.receive_inline(
                    data_base64=data_base64,
                    expected_size=expected_size if "expected_size" in arguments else None,
                    expected_sha256=expected_sha256 if "expected_sha256" in arguments else None,
                    prompt=prompt,
                )
                return _tool_response(
                    msg_id, _tool_payload(result), error=bool(result.failure_reason)
                )
            if name == TOOL_ARTIFACT:
                _validate_filename(arguments)
                _validate_declared_media_type(arguments)
                artifact = arguments.get("artifact")
                if not isinstance(artifact, dict):
                    raise ProbeInputError("invalid_artifact", "artifact must be an object")
                unknown_fields = sorted(set(artifact) - {"image_url", "output_hint"})
                if unknown_fields:
                    raise ProbeInputError(
                        "invalid_artifact",
                        f"unknown generated-image field: {unknown_fields[0]}",
                    )
                image_url = artifact.get("image_url")
                if not isinstance(image_url, str):
                    raise ProbeInputError(
                        "invalid_artifact", "artifact.image_url must be a string"
                    )
                header, separator, data_base64 = image_url.partition(",")
                if not separator or header.lower() != PNG_DATA_URL_PREFIX[:-1]:
                    raise ProbeInputError(
                        "unsupported_artifact",
                        "artifact.image_url must be a base64 PNG data URL",
                    )
                output_hint = artifact.get("output_hint")
                if output_hint is not None and (
                    not isinstance(output_hint, str)
                    or len(output_hint.encode("utf-8")) > MAX_PROMPT_BYTES
                ):
                    raise ProbeInputError(
                        "invalid_artifact", "artifact.output_hint exceeds the probe limit"
                    )
                prompt = _prompt_facts(None)
                shape = summarize_artifact(arguments.get("artifact"))
                result = state.receive_inline(
                    data_base64=data_base64,
                    expected_size=None,
                    expected_sha256=None,
                    prompt=prompt,
                    transport="generated_image_data_url",
                )
                result.artifact_shape = shape
                return _tool_response(
                    msg_id, _tool_payload(result), error=bool(result.failure_reason)
                )
            if name == TOOL_PREPARE:
                _validate_filename(arguments)
                media_type = _validate_declared_media_type(arguments)
                expected_size, expected_sha256 = _validate_expected_facts(arguments, required=True)
                prompt = _prompt_facts(arguments.get("prompt"))
                token, result, expires_at = state.create_upload(
                    expected_size=expected_size,
                    expected_sha256=expected_sha256,
                    media_type=media_type,
                    prompt=prompt,
                )
                upload_url = f"{_origin(request, public_origin)}/probe/{state.root_capability}/upload/{token}"
                return _tool_response(
                    msg_id,
                    {
                        "status": "prepared",
                        "attempt_id": result.attempt_id,
                        "transport": "direct_put",
                        "upload_url": upload_url,
                        "expires_at": _now_iso(expires_at),
                        "expected_size": expected_size,
                        "expected_sha256": expected_sha256,
                        "media_type": media_type,
                    },
                )
            attempt_id = arguments.get("attempt_id")
            if not isinstance(attempt_id, str) or not attempt_id or len(attempt_id) > 64:
                raise ProbeInputError("invalid_attempt_id", "attempt_id must be a bounded string")
            result = state.get_result(attempt_id)
            return _tool_response(msg_id, _tool_payload(result), error=bool(result.failure_reason))
        except ProbeNotFound:
            return _tool_response(
                msg_id,
                {"status": "error", "reason": "not_found", "message": "probe attempt not found"},
                error=True,
            )
        except ProbeInputError as exc:
            log.info("asset probe rejected transport=%s outcome=%s", name, exc.reason)
            return _tool_response(
                msg_id,
                {"status": "error", "reason": exc.reason, "message": exc.message},
                error=True,
            )

    @app.put("/probe/{capability}/upload/{token}")
    async def upload(request: Request, capability: str, token: str) -> Response:
        if not authorized(request, capability):
            return Response(status_code=404, content="Not Found")
        try:
            result = await state.receive_direct(request, token)
            if result.failure_reason:
                return JSONResponse(
                    {
                        "status": "error",
                        "reason": result.failure_reason,
                        "attempt_id": result.attempt_id,
                        "received_byte_count": result.received_byte_count,
                    },
                    status_code=400,
                )
            return JSONResponse(
                {
                    "status": "success",
                    "attempt_id": result.attempt_id,
                    "received_byte_count": result.received_byte_count,
                }
            )
        except ProbeInputError as exc:
            log.info("asset probe rejected transport=direct_put outcome=%s", exc.reason)
            status = (
                410
                if exc.reason == "invalid_upload_capability"
                else 409
                if exc.reason == "ticket_replayed"
                else 400
            )
            return JSONResponse(
                {"status": "error", "reason": exc.reason, "message": exc.message},
                status_code=status,
            )

    return app


def build_probe_app(*, public_origin: str | None = None) -> tuple[FastAPI, ProbeState]:
    """Build a disposable probe and its state for the CLI entry point."""
    state = ProbeState()
    return create_probe_app(state, public_origin=public_origin), state
