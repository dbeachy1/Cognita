"""Canonical Cognita image metadata v1 and deterministic search projection."""

from __future__ import annotations

import copy
import re
from collections.abc import Mapping
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from .limits import (
    MAX_METADATA_BYTES,
    MAX_PROMPT_BYTES,
    canonical_json,
    utf8_len,
    validate_json_tree,
)
from .models import AssetError

SCHEMA = "urn:cognita:image-metadata:v1"
EDITABLE_FIELDS = frozenset({"title", "description", "alt_text", "prompts", "generation", "source", "tags", "related_documents", "extensions"})
SERVER_FIELDS = frozenset({"schema", "schema_version", "asset_id", "kind"})
_SAFE_RELATIVE = re.compile(r"^(?![\\/])(?:[^\\/:*?\"<>|]+[\\/]?)+$")


def _text(value: Any, field: str, limit: int) -> str:
    if not isinstance(value, str) or utf8_len(value) > limit:
        raise AssetError("metadata_invalid", f"metadata field {field} is invalid")
    return value


def _optional_text(value: Any, field: str, limit: int) -> str | None:
    if value is None:
        return None
    return _text(value, field, limit)


def _editable_input(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise AssetError("metadata_invalid", "metadata must be an object")
    unknown = set(value) - EDITABLE_FIELDS
    if unknown:
        raise AssetError("metadata_invalid", "metadata contains an unknown field")
    validate_json_tree(value)
    return copy.deepcopy(dict(value))


def _validate_prompts(value: Any) -> dict[str, str | None]:
    if not isinstance(value, Mapping) or set(value) - {"user", "effective", "negative"}:
        raise AssetError("metadata_invalid", "prompts must contain only v1 prompt fields")
    return {key: _optional_text(value.get(key), f"prompts.{key}", MAX_PROMPT_BYTES) for key in ("user", "effective", "negative")}


def _validate_generation(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) - {"provider", "model", "tool", "created_at"}:
        raise AssetError("metadata_invalid", "generation contains an unknown field")
    result: dict[str, Any] = {}
    for key in ("provider", "model", "tool"):
        if key in value:
            result[key] = _optional_text(value[key], f"generation.{key}", 512)
    if "created_at" in value and value["created_at"] is not None:
        created = _text(value["created_at"], "generation.created_at", 128)
        try:
            parsed = datetime.fromisoformat(created)
            if parsed.utcoffset() is None:
                raise ValueError
        except ValueError as exc:
            raise AssetError("metadata_invalid", "generation.created_at must be RFC 3339") from exc
        result["created_at"] = created
    return result


def _validate_path_list(values: Any, field: str) -> list[str]:
    if not isinstance(values, list) or len(values) > 64:
        raise AssetError("metadata_invalid", f"{field} must contain at most 64 paths")
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if not isinstance(value, str) or utf8_len(value) > 1_024 or not _SAFE_RELATIVE.fullmatch(value):
            raise AssetError("metadata_invalid", f"{field} contains an unsafe path")
        normalized = value.replace("\\", "/")
        if normalized.startswith("/") or "/../" in f"/{normalized}/" or normalized == "..":
            raise AssetError("metadata_invalid", f"{field} contains an unsafe path")
        if normalized not in seen:
            seen.add(normalized)
            result.append(normalized)
    return result


def _validate_tags(values: Any) -> list[str]:
    if not isinstance(values, list) or len(values) > 64:
        raise AssetError("metadata_invalid", "tags must contain at most 64 strings")
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if not isinstance(value, str):
            raise AssetError("metadata_invalid", "tags must contain strings")
        value = value.strip()
        if not value or utf8_len(value) > 128:
            raise AssetError("metadata_invalid", "tag is empty or too long")
        key = value.casefold()
        if key not in seen:
            seen.add(key)
            result.append(value)
    return result


def canonical_metadata(
    supplied: Mapping[str, Any] | None,
    *,
    asset_id: str | None = None,
    received_sha256: str | None = None,
    base: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Validate editable caller data and add server-owned identity fields."""
    incoming = _editable_input(supplied)
    merged = copy.deepcopy(dict(base)) if base else {}
    merged = merge_metadata(merged, incoming) if base and incoming else {**merged, **incoming}
    title = _text(merged.get("title", ""), "title", 512)
    description = _text(merged.get("description", ""), "description", 4_096)
    alt_text = _text(merged.get("alt_text", ""), "alt_text", 4_096)
    prompts = _validate_prompts(merged.get("prompts", {}))
    generation = _validate_generation(merged.get("generation", {}))
    source_in = merged.get("source", {})
    if not isinstance(source_in, Mapping) or set(source_in) - {"type"}:
        raise AssetError("metadata_invalid", "source contains an unknown field")
    source_type = source_in.get("type", "imported")
    if source_type not in {"generated", "imported"}:
        raise AssetError("metadata_invalid", "source.type is invalid")
    tags = _validate_tags(merged.get("tags", []))
    related = _validate_path_list(merged.get("related_documents", []), "related_documents")
    extensions = merged.get("extensions", {})
    if not isinstance(extensions, Mapping):
        raise AssetError("metadata_invalid", "extensions must be an object")
    validate_json_tree(extensions)
    result = {
        "schema": SCHEMA,
        "schema_version": 1,
        "asset_id": asset_id or str(uuid4()),
        "kind": "image",
        "title": title,
        "description": description,
        "alt_text": alt_text,
        "prompts": prompts,
        "generation": generation,
        "source": {"type": source_type, "received_sha256": received_sha256 or ""},
        "tags": tags,
        "related_documents": related,
        "extensions": copy.deepcopy(dict(extensions)),
    }
    if not _is_uuid4(result["asset_id"]):
        raise AssetError("metadata_invalid", "asset_id must be a UUIDv4")
    encoded = canonical_json(result)
    if len(encoded) > MAX_METADATA_BYTES:
        raise AssetError("metadata_limit", "metadata exceeds its limit")
    return result


def _is_uuid4(value: Any) -> bool:
    try:
        return UUID(str(value)).version == 4
    except (ValueError, AttributeError, TypeError):
        return False


def merge_metadata(old: Mapping[str, Any], new: Mapping[str, Any]) -> dict[str, Any]:
    """Merge caller values over prior values with caller-first stable unions."""
    result = copy.deepcopy(dict(old))
    for key, value in new.items():
        if value is None:
            continue
        if key in {"tags", "related_documents"} and isinstance(value, list):
            prior = result.get(key, []) if isinstance(result.get(key), list) else []
            key_of = (lambda item: item.casefold()) if key == "tags" else (lambda item: item)
            seen = {key_of(item) for item in value if isinstance(item, str)}
            result[key] = copy.deepcopy(value) + [
                copy.deepcopy(item) for item in prior
                if not isinstance(item, str) or key_of(item) not in seen
            ]
        elif isinstance(value, Mapping) and isinstance(result.get(key), Mapping):
            result[key] = merge_metadata(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def replace_metadata(old: Mapping[str, Any], new: Mapping[str, Any]) -> dict[str, Any]:
    """Replace only editable fields while retaining server identity later."""
    result = {key: copy.deepcopy(value) for key, value in new.items() if key in EDITABLE_FIELDS}
    return result


def prepare_metadata(
    supplied: Mapping[str, Any] | None,
    *,
    action: str = "merge",
    asset_id: str | None = None,
    received_sha256: str | None = None,
    existing: Mapping[str, Any] | None = None,
    native: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    base = existing or native
    if action not in {"merge", "replace"}:
        raise AssetError("invalid_arguments", "metadata_action must be merge or replace")
    prior = {key: copy.deepcopy(value) for key, value in (base or {}).items() if key in EDITABLE_FIELDS}
    if isinstance(prior.get("source"), Mapping):
        prior["source"] = {"type": prior["source"].get("type", "imported")}
    source = merge_metadata(prior, supplied or {}) if action == "merge" else replace_metadata({}, supplied or {})
    return canonical_metadata(source, asset_id=asset_id, received_sha256=received_sha256, base=None)


def search_projection(metadata: Mapping[str, Any], filepath: str = "") -> str:
    """Produce the bounded, labeled text that may enter asset search."""
    lines: list[str] = []
    fields = [("Path", filepath), ("Title", metadata.get("title", "")), ("Description", metadata.get("description", "")), ("Alt text", metadata.get("alt_text", ""))]
    prompts = metadata.get("prompts", {})
    if isinstance(prompts, Mapping):
        fields.extend((("User prompt", prompts.get("user", "")), ("Effective prompt", prompts.get("effective", "")), ("Negative prompt", prompts.get("negative", ""))))
    fields.append(("Tags", ", ".join(metadata.get("tags", []))))
    generation = metadata.get("generation", {})
    if isinstance(generation, Mapping):
        fields.extend((("Generator provider", generation.get("provider", "")), ("Generator model", generation.get("model", "")), ("Generator tool", generation.get("tool", ""))))
    fields.append(("Related documents", ", ".join(metadata.get("related_documents", []))))
    for label, value in fields:
        if isinstance(value, str) and value:
            lines.append(f"{label}: {value}")
    return "\n".join(lines)
