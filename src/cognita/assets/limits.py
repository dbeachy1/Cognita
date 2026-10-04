"""Hard ceilings and validation helpers for the 7.1 asset wire contract."""

from __future__ import annotations

import base64
import binascii
import json
import math
import re
from collections.abc import Mapping
from typing import Any

from .models import AssetError

MAX_PNG_BYTES = 16 * 1_048_576
# Connector hosts have a materially smaller response envelope than local
# catalog/OCR ingestion.  Keep exact image responses at the proven 7.1 bound
# even though 10.0 may catalog, update, and OCR larger local PNGs.
MAX_INLINE_PNG_BYTES = 1_048_576
PNG_DATA_URL_PREFIX = "data:image/png;base64,"
MAX_DATA_URL_CHARS = len(PNG_DATA_URL_PREFIX) + ((MAX_PNG_BYTES + 2) // 3) * 4
MAX_METADATA_BYTES = 65_536
MAX_PROMPT_BYTES = 16_384
MAX_DIMENSION = 4_096
MAX_PIXELS = 16_777_216
MAX_PNG_CHUNKS = 2_048
MAX_PNG_CHUNK_BYTES = MAX_PNG_BYTES
MAX_LIST_RESULTS = 200
MAX_SEARCH_RESULTS = 20
MAX_OPERATION_ID_CHARS = 128
OPERATION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")


def utf8_len(value: str) -> int:
    return len(value.encode("utf-8"))


def bounded_string(value: Any, name: str, limit: int, *, empty: bool = True) -> str:
    if not isinstance(value, str) or (not empty and not value):
        raise AssetError("invalid_arguments", f"{name} must be a string")
    if utf8_len(value) > limit:
        raise AssetError("metadata_limit", f"{name} exceeds its byte limit")
    return value


def operation_id(value: Any) -> str:
    if not isinstance(value, str) or not OPERATION_ID_RE.fullmatch(value):
        raise AssetError("invalid_arguments", "operation_id has an invalid format")
    return value


def sha256_value(value: Any, name: str = "sha256") -> str:
    if not isinstance(value, str) or not SHA256_RE.fullmatch(value):
        raise AssetError("invalid_arguments", f"{name} must be a SHA-256 hex digest")
    return value.lower()


def integer(value: Any, name: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise AssetError("invalid_arguments", f"{name} must be between {minimum} and {maximum}")
    return value


def validate_data_url(value: Any) -> str:
    if not isinstance(value, str):
        raise AssetError("invalid_data_url", "image_url must be a string")
    if len(value) > MAX_DATA_URL_CHARS:
        raise AssetError("encoded_limit", "image_url exceeds the encoded PNG limit")
    if not value.startswith(PNG_DATA_URL_PREFIX):
        raise AssetError("invalid_data_url", "only the exact PNG data URL is accepted")
    encoded = value[len(PNG_DATA_URL_PREFIX):]
    if not encoded:
        raise AssetError("invalid_base64", "image_url contains no image data")
    if not re.fullmatch(r"[A-Za-z0-9+/]*={0,2}", encoded):
        raise AssetError("invalid_base64", "image_url is not valid standard base64")
    return encoded


def decode_base64(encoded: str) -> bytes:
    try:
        return base64.b64decode(encoded.encode("ascii"), validate=True)
    except (UnicodeEncodeError, binascii.Error, ValueError) as exc:
        raise AssetError("invalid_base64", "image_url is not valid standard base64") from exc


def validate_json_tree(value: Any, *, depth: int = 0, nodes: list[int] | None = None) -> None:
    """Reject executable/prototype-shaped metadata and bound recursive JSON."""
    nodes = nodes if nodes is not None else [0]
    nodes[0] += 1
    if depth > 8 or nodes[0] > 1_024:
        raise AssetError("metadata_limit", "metadata JSON is too deep or large")
    if isinstance(value, dict):
        for key, child in value.items():
            if not isinstance(key, str):
                raise AssetError("metadata_invalid", "metadata object keys must be strings")
            validate_json_tree(child, depth=depth + 1, nodes=nodes)
    elif isinstance(value, list):
        for child in value:
            validate_json_tree(child, depth=depth + 1, nodes=nodes)
    elif not isinstance(value, (str, int, float, bool)) and value is not None:
        raise AssetError("metadata_invalid", "metadata must contain JSON values")
    elif isinstance(value, float) and not math.isfinite(value):
        raise AssetError("metadata_invalid", "metadata numbers must be finite")


def canonical_json(value: Mapping[str, Any]) -> bytes:
    try:
        validate_json_tree(value)
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise AssetError("metadata_invalid", "metadata is not canonical JSON") from exc
    result = encoded.encode("utf-8")
    if len(result) > MAX_METADATA_BYTES:
        raise AssetError("metadata_limit", "metadata exceeds 65,536 UTF-8 bytes")
    return result
