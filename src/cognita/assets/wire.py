"""MCP contracts and result adapters for the PNG asset surface."""

from __future__ import annotations

import base64
import json
from typing import Any

from .limits import MAX_DATA_URL_CHARS, MAX_LIST_RESULTS, MAX_PNG_BYTES, MAX_SEARCH_RESULTS


def _obj(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "required": required or [], "additionalProperties": False}


_metadata_schema = _obj({
    "title": {"type": "string", "maxLength": 512},
    "description": {"type": "string", "maxLength": 4096},
    "alt_text": {"type": "string", "maxLength": 4096},
    "prompts": _obj({
        "user": {"type": ["string", "null"], "maxLength": 16384},
        "effective": {"type": ["string", "null"], "maxLength": 16384},
        "negative": {"type": ["string", "null"], "maxLength": 16384},
    }),
    "generation": _obj({
        "provider": {"type": ["string", "null"], "maxLength": 512},
        "model": {"type": ["string", "null"], "maxLength": 512},
        "tool": {"type": ["string", "null"], "maxLength": 512},
        "created_at": {"type": ["string", "null"], "maxLength": 128},
    }),
    "source": _obj({"type": {"type": "string", "enum": ["generated", "imported"]}}),
    "tags": {"type": "array", "maxItems": 64, "items": {"type": "string", "maxLength": 128}},
    "related_documents": {"type": "array", "maxItems": 64, "items": {"type": "string", "maxLength": 1024}},
    "extensions": {"type": "object", "additionalProperties": True},
})

IMAGE_SCHEMA = _obj({
    "image_url": {"type": "string", "maxLength": MAX_DATA_URL_CHARS},
    "output_hint": {"type": "string", "maxLength": 4096},
}, ["image_url"])

PUT_ASSET_TOOL = {
    "name": "put_asset",
    "description": "Publish a bounded generated static PNG asset and its metadata.",
    "inputSchema": _obj({
        "filepath": {"type": "string"}, "image": IMAGE_SCHEMA, "metadata": _metadata_schema,
        "metadata_action": {"type": "string", "enum": ["merge", "replace"], "default": "merge"},
        "metadata_storage": {"type": "string", "enum": ["auto", "embedded", "catalog"], "default": "auto"},
        "overwrite": {"type": "boolean", "default": False}, "operation_id": {"type": "string", "maxLength": 128},
        "expected_current_sha256": {"type": "string", "pattern": "^[0-9a-fA-F]{64}$"},
        "expected_received_size": {"type": "integer", "minimum": 1, "maximum": MAX_PNG_BYTES},
        "expected_received_sha256": {"type": "string", "pattern": "^[0-9a-fA-F]{64}$"},
    }, ["filepath", "image", "operation_id"]),
}

UPDATE_ASSET_METADATA_TOOL = {
    "name": "update_asset_metadata",
    "description": "Update metadata for an existing PNG asset with an optimistic hash guard.",
    "inputSchema": _obj({
        "filepath": {"type": "string"}, "metadata": _metadata_schema,
        "metadata_action": {"type": "string", "enum": ["merge", "replace"]},
        "expected_sha256": {"type": "string", "pattern": "^[0-9a-fA-F]{64}$"},
        "operation_id": {"type": "string", "maxLength": 128},
        "metadata_storage": {"type": "string", "enum": ["auto", "embedded", "catalog", "preserve_current"], "default": "preserve_current"},
    }, ["filepath", "metadata", "metadata_action", "expected_sha256", "operation_id"]),
}

SEARCH_ASSETS_TOOL = {"name": "search_assets", "description": "Search PNG asset metadata only.", "inputSchema": _obj({
    "query": {"type": "string", "minLength": 1, "maxLength": 4096}, "max_results": {"type": "integer", "minimum": 1, "maximum": MAX_SEARCH_RESULTS, "default": 5},
    "path_prefix": {"type": "string"}, "tags": {"type": "array", "minItems": 1, "maxItems": 64, "items": {"type": "string"}},
    "hybrid_alpha": {"type": "number", "minimum": 0, "maximum": 1, "default": 0.3}, "min_score": {"type": "number", "minimum": 0, "maximum": 1, "default": 0},
    "detail": {"type": "string", "enum": ["full", "summary"], "default": "full"},
}, ["query"])}
LIST_ASSETS_TOOL = {"name": "list_assets", "description": "List published PNG assets.", "inputSchema": _obj({
    "prefix": {"type": "string"}, "max_results": {"type": "integer", "minimum": 1, "maximum": MAX_LIST_RESULTS, "default": 100}, "cursor": {"type": "string"},
    "detail": {"type": "string", "enum": ["full", "summary"], "default": "full"},
})}
GET_ASSET_INFO_TOOL = {"name": "get_asset_info", "description": "Inspect PNG asset metadata and file facts without image bytes.", "inputSchema": _obj({"filepath": {"type": "string"}}, ["filepath"])}
GET_ASSET_TOOL = {"name": "get_asset", "description": "Return exact PNG bytes as MCP image content plus bounded facts.", "inputSchema": _obj({"filepath": {"type": "string"}, "expected_sha256": {"type": "string", "pattern": "^[0-9a-fA-F]{64}$"}}, ["filepath"])}
REINDEX_ASSETS_TOOL = {"name": "reindex_assets", "description": "Reconcile PNG files into the asset catalog without changing bytes.", "inputSchema": _obj({"prefix": {"type": "string"}, "operation_id": {"type": "string", "maxLength": 128}}, ["operation_id"])}
OCR_ASSET_TOOL = {"name": "ocr_asset", "description": "Extract searchable text and bounded regions from an authorized static PNG.", "inputSchema": _obj({
    "project": {"type": "string"}, "filepath": {"type": "string"},
    "languages": {"type": "array", "minItems": 1, "maxItems": 3, "items": {"type": "string", "maxLength": 32}},
    "timeout_seconds": {"type": "integer", "minimum": 10, "maximum": 600, "default": 120},
}, ["project", "filepath"])}

REMOVE_ASSET_TOOL = {"name": "remove_asset", "description": "Safely remove an authorized static PNG asset and detach its catalog and path-bound OCR state.", "inputSchema": _obj({
    "filepath": {"type": "string"},
    "expected_sha256": {"type": "string", "pattern": "^[0-9a-fA-F]{64}$"},
    "operation_id": {"type": "string", "maxLength": 128},
}, ["filepath", "operation_id"])}

ASSET_TOOL_DEFS = [PUT_ASSET_TOOL, UPDATE_ASSET_METADATA_TOOL, SEARCH_ASSETS_TOOL, LIST_ASSETS_TOOL, GET_ASSET_INFO_TOOL, GET_ASSET_TOOL, REINDEX_ASSETS_TOOL, OCR_ASSET_TOOL, REMOVE_ASSET_TOOL]
ASSET_TOOL_NAMES = frozenset(tool["name"] for tool in ASSET_TOOL_DEFS)
ASSET_MUTATING_TOOLS = frozenset({"put_asset", "update_asset_metadata", "reindex_assets", "remove_asset"})


def text_result(payload: dict[str, Any], *, error: bool = False) -> dict[str, Any]:
    return {"isError": error, "content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False, separators=(",", ":"))}], "structuredContent": payload}


def image_result(payload: dict[str, Any], data: bytes) -> dict[str, Any]:
    # Deliberately keep base64 out of both textual and structured content.
    return {"isError": False, "content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False, separators=(",", ":"))}, {"type": "image", "data": base64.b64encode(data).decode("ascii"), "mimeType": "image/png"}], "structuredContent": payload}


def error_result(reason: str, message: str) -> dict[str, Any]:
    return text_result({"status": "error", "reason": reason, "message": message[:512]}, error=True)
