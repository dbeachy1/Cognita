"""Exact public result schemas for the 10.1 MCP contract.

Every successful object and every established nested record is closed.  The
only deliberately extensible success object is user-owned asset metadata's
``extensions`` member.  Error envelopes remain extensible at the public tool
boundary so diagnostics can be added without changing the error contract.
"""

from __future__ import annotations

import copy
from collections.abc import Iterable, Mapping
from typing import Any


S = {"type": "string"}
INT = {"type": "integer"}
NNI = {"type": "integer", "minimum": 0}
PI = {"type": "integer", "minimum": 1}
N = {"type": "number"}
B = {"type": "boolean"}
HASH = {"type": "string", "pattern": "^[0-9a-f]{64}$"}
UUID = {"type": "string", "format": "uuid"}


def nullable(schema: Mapping[str, Any]) -> dict[str, Any]:
    return {"anyOf": [dict(schema), {"type": "null"}]}


def arr(items: Mapping[str, Any], **bounds: Any) -> dict[str, Any]:
    return {"type": "array", "items": dict(items), **bounds}


def obj(properties: Mapping[str, Any], required: Iterable[str] = ()) -> dict[str, Any]:
    return {
        "type": "object", "properties": dict(properties),
        "required": list(required), "additionalProperties": False,
    }


def branch(status: str, properties: Mapping[str, Any], required: Iterable[str]) -> dict[str, Any]:
    props = {"status": {"const": status}, **dict(properties)}
    return obj(props, ("status", *required))


ERROR = {
    "type": "object",
    "properties": {
        "status": {"const": "error"}, "reason": S, "message": S,
        # 12.18.1: a broker-relayed Workspace error carries the broker's
        # correlation_id, which is null when the broker sent none.  Requiring
        # a string here turned every such error into an opaque
        # output_contract_violation instead of its real reason.
        "correlation_id": nullable(S), "operation_outcome": S,
    },
    "required": ["status", "reason"],
    "additionalProperties": True,
}


def contract(*branches: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "Cognita MCP structured result", "type": "object",
        "oneOf": [copy.deepcopy(dict(item)) for item in branches] + [copy.deepcopy(ERROR)],
    }


STRINGS = arr(S)
NULL_S = nullable(S)
NULL_I = nullable(INT)
NULL_HASH = nullable(HASH)
TIER = {"type": "string", "enum": ["embedded", "registered"]}
LINE_ENDINGS = nullable(S)

BYTE_FACT_PROPERTIES = {
    "bytes_sha256": NULL_HASH, "size_bytes": NNI, "line_endings": LINE_ENDINGS,
    "utf8_valid": nullable(B), "decode_error_bytes": nullable(NNI),
    "content_is_lossy": B, "index_text_sanitized": B,
    "content_sha256": NULL_HASH,
}
BYTE_FACT_REQUIRED = tuple(BYTE_FACT_PROPERTIES)
INDEXING_FACT = obj({
    "state": {"type": "string", "enum": [
        "pending", "indexed", "stale", "excluded", "blocked", "failed",
    ]},
    "job_id": NULL_S,
    "error": nullable(obj({"code": S, "message": S}, ("code", "message"))),
}, ("state", "job_id", "error"))
WRITE_RECEIPT = obj({
    "filepath": S, **BYTE_FACT_PROPERTIES, "indexing": INDEXING_FACT,
}, ("filepath", *BYTE_FACT_REQUIRED))

FILE_FACT_PROPERTIES = {
    "on_disk": B, "size_bytes": NNI, "mtime": nullable(S),
    "mtime_epoch": nullable(N), **BYTE_FACT_PROPERTIES, "error": S,
}

DOCUMENT_METADATA = obj(
    {"type": S, "file_size": NNI, "modified": nullable(S)},
    ("type", "file_size", "modified"),
)
FULL_DOCUMENT_PROPERTIES = {
    "content": S, "content_is_extracted": B,
    "content_encoding": {"type": "string", "enum": ["utf-8", "base64"]},
    "content_note": S, "source": S, "filepath": S, "filename": S,
    "category": S, "format": S, "content_sha256": NULL_HASH,
    "bytes_sha256": NULL_HASH, "size_bytes": NNI, "utf8_valid": nullable(B),
    "decode_error_bytes": nullable(NNI), "content_is_lossy": B,
    "index_text_sanitized": B, "line_endings": LINE_ENDINGS,
    "mtime": S, "indexed_sha256": NULL_HASH,
    "index_drift": nullable(B), "metadata": DOCUMENT_METADATA,
    "keywords": STRINGS, "chunk_count": NNI, "tier": TIER,
    "semantic_searchable": B,
}
FULL_DOCUMENT = obj(FULL_DOCUMENT_PROPERTIES, tuple(FULL_DOCUMENT_PROPERTIES))

FACTS_DOCUMENT_PROPERTIES = {
    "filepath": S, "source": S, "indexed": B, "include_content": {"const": False},
    "category": S, "chunk_count": NNI, "tier": nullable(TIER),
    **FILE_FACT_PROPERTIES, "indexed_sha256": NULL_HASH, "index_drift": nullable(B),
}
FACTS_DOCUMENT = obj(
    FACTS_DOCUMENT_PROPERTIES,
    ("filepath", "source", "indexed", "include_content", "category", "chunk_count",
     "tier", "on_disk"),
)

SEARCH_HIT_PROPERTIES = {
    "content": S, "source": S, "filepath": S, "filename": S, "category": S,
    "chunk_index": NNI, "tier": TIER, "semantic_searchable": B, "score": N,
    "raw_rrf_score": N, "reranker_score": nullable(N),
    "semantic_rank": nullable(INT), "bm25_rank": nullable(INT),
    "search_method": {"type": "string", "enum": ["hybrid", "semantic", "keyword"]},
    "keywords": STRINGS, "routed_by": S, "context_expanded": B,
    "content_length": NNI,
}
SEARCH_HIT = obj(
    SEARCH_HIT_PROPERTIES,
    ("content", "source", "filepath", "filename", "category", "chunk_index", "tier",
     "semantic_searchable", "score", "raw_rrf_score", "reranker_score",
     "semantic_rank", "bm25_rank", "search_method", "keywords", "routed_by"),
)

SIMILAR_HIT = obj(
    {"source": S, "filepath": S, "filename": S, "category": S, "preview": S,
     "similarity": N, "score": N},
    ("source", "filepath", "filename", "category", "preview", "similarity", "score"),
)

LIST_DOCUMENT_BASE = {
    # Document IDs are content-addressed hashes (see parsing.compute_doc_id),
    # not database sequence numbers.  The engine has always emitted them as
    # strings; keeping the contract aligned with that stable wire shape lets
    # list_documents results pass the runtime validator.
    "id": S, "source": S, "filepath": S, "category": S, "format": S,
    "chunks": NNI, "keywords": STRINGS, "tier": TIER, "semantic_searchable": B,
}
LIST_DOCUMENT = obj(
    {**LIST_DOCUMENT_BASE, **FILE_FACT_PROPERTIES, "indexed_sha256": NULL_HASH,
     "index_drift": B},
    tuple(LIST_DOCUMENT_BASE),
)

NESTED_ERROR = obj(
    {
        "status": {"const": "error"}, "reason": S, "message": S, "on_disk": B,
        "filepath": S, "size_bytes": NNI, "bytes_sha256": NULL_HASH, "mtime": nullable(S),
        "hint": S, "limit_bytes": NNI, "content_sha256": NULL_HASH,
        # remove_document contributes these facts on a few error paths (for
        # example an unindexable extension or a failed unlink).  They are
        # optional because not-found and validation errors have no disk facts.
        "source": S, "chunks_removed": NNI, "was_indexed": B,
        "delete_file_requested": B, "file_deleted": B,
    },
    ("status", "reason"),
)
PLURAL_DOCUMENT_ITEM = {
    "oneOf": [
        obj({"index": NNI, "filepath": S, "status": {"const": "success"},
             "document": {"oneOf": [FULL_DOCUMENT, FACTS_DOCUMENT]}},
            ("index", "filepath", "status", "document")),
        obj({"index": NNI, "filepath": S, "status": {"const": "error"}, "reason": S,
             "error": NESTED_ERROR, "document": {"oneOf": [FULL_DOCUMENT, FACTS_DOCUMENT]}},
            ("index", "filepath", "status", "reason", "error")),
    ]
}

LITERAL_MATCH = obj(
    {"filepath": S, "source": S, "tier": TIER, "line_number": PI, "column": PI,
     "line": S, "match": S, "context_before": STRINGS, "context_after": STRINGS,
     "utf8_valid": B, "decode_error_bytes": NNI, "content_is_lossy": B,
     "index_text_sanitized": B},
    ("filepath", "source", "tier", "line_number", "column", "line", "match",
     "context_before", "context_after"),
)
SKIPPED_FILE = obj({"filepath": S, "reason": S, "detail": S}, ("filepath", "reason"))

BACKUP_ENTRY = obj(
    {"filepath": S, "backup_id": S, "created": S, "size_bytes": NULL_I},
    ("filepath", "backup_id", "created", "size_bytes"),
)
DELETION_BACKUP = obj(
    {"filepath": S, "backup_id": S, "deleted_mtime": nullable(S),
     "deleted_bytes_sha256": NULL_HASH},
    ("filepath", "backup_id"),
)

COPY_ENTRY_PROPERTIES = {
    "filepath": S, "source_filepath": S, "chunks_added": NNI, "tier": TIER,
    "indexed": B, **FILE_FACT_PROPERTIES, "overwrote_existing": B,
    "previous_backup_id": NULL_S,
}
COPY_ENTRY = obj(
    COPY_ENTRY_PROPERTIES,
    ("filepath", "source_filepath", "chunks_added", "tier", "indexed", "on_disk"),
)

REMOVE_DOCUMENT_PROPERTIES = {
    # Plural removal nests the complete single-document result, including its
    # success discriminator.  The top-level branch adds the same const, while
    # this member keeps successful child receipts inside the oneOf contract.
    "status": {"const": "success"},
    "filepath": S, "source": S, "chunks_removed": NNI, "was_indexed": B,
    "delete_file_requested": B, "file_deleted": B, "pruned_directories": STRINGS,
    "indexing_suppressed": B, "already_deindexed": B, "note": S,
    "file_was_on_disk": B, "deleted_mtime": nullable(S), "deleted_size_bytes": NNI,
    "deleted_bytes_sha256": NULL_HASH, "ghost_check": S, "previous_backup_id": NULL_S,
}
REMOVE_DOCUMENT_REQUIRED = (
    "filepath", "source", "chunks_removed", "was_indexed", "delete_file_requested",
    "file_deleted", "pruned_directories", "indexing_suppressed",
)
REMOVE_DOCUMENT_RESULT = obj(REMOVE_DOCUMENT_PROPERTIES, REMOVE_DOCUMENT_REQUIRED)
REMOVE_DOCUMENT_CHILD = {
    "oneOf": [
        obj({"index": NNI, "filepath": S, "status": {"const": "success"},
             "result": REMOVE_DOCUMENT_RESULT}, ("index", "filepath", "status", "result")),
        obj({"index": NNI, "filepath": S, "status": {"const": "error"}, "reason": S,
             "error": NESTED_ERROR}, ("index", "filepath", "status", "reason", "error")),
        obj({"index": NNI, "filepath": S, "status": {"const": "skipped"},
             "reason": {"const": "previous_error"}},
            ("index", "filepath", "status", "reason")),
    ]
}

PROMPTS = obj(
    {"user": nullable(S), "effective": nullable(S), "negative": nullable(S)},
    ("user", "effective", "negative"),
)
GENERATION = obj(
    {"provider": nullable(S), "model": nullable(S), "tool": nullable(S),
     "created_at": nullable(S)},
)
SOURCE_METADATA = obj(
    {"type": {"type": "string", "enum": ["generated", "imported"]},
     "received_sha256": HASH}, ("type", "received_sha256"),
)
CANONICAL_METADATA_PROPERTIES = {
    "schema": {"const": "urn:cognita:image-metadata:v1"}, "schema_version": {"const": 1},
    "asset_id": UUID, "kind": {"const": "image"}, "title": S, "description": S,
    "alt_text": S, "prompts": PROMPTS, "generation": GENERATION,
    "source": SOURCE_METADATA, "tags": arr(S, maxItems=64),
    "related_documents": arr(S, maxItems=64),
    "extensions": {"type": "object", "additionalProperties": True},
}
CANONICAL_METADATA = obj(CANONICAL_METADATA_PROPERTIES, tuple(CANONICAL_METADATA_PROPERTIES))
EMPTY_METADATA = obj({})

ASSET_FULL_BASE = {
    "filepath": S, "asset_id": UUID, "title": S, "description": S, "alt_text": S,
    "tags": arr(S, maxItems=64), "width": PI, "height": PI, "final_sha256": HASH,
    "metadata_storage": {"type": "string", "enum": ["embedded", "catalog"]},
    "provenance_state": {"type": "string", "enum": ["none", "cabx_present_unverified"]},
    "score": {"type": "number", "minimum": 0, "maximum": 1},
    "search_method": {"type": "string", "enum": ["lexical", "keyword", "semantic", "hybrid"]},
}
# A listing carries no search-only score/search_method (13.0.2); a search does.
ASSET_FULL_LIST_BASE = {key: value for key, value in ASSET_FULL_BASE.items()
                        if key not in {"score", "search_method"}}
ASSET_FULL_LIST = obj(ASSET_FULL_LIST_BASE, tuple(ASSET_FULL_LIST_BASE))
ASSET_FULL_SEARCH = {
    "oneOf": [
        obj(ASSET_FULL_BASE, tuple(ASSET_FULL_BASE)),
        obj({**ASSET_FULL_BASE, "provenance": {"const": "ocr"}, "source_sha256": HASH},
            (*tuple(ASSET_FULL_BASE), "provenance", "source_sha256")),
    ]
}

ASSET_SUMMARY_BASE = {
    "filepath": S, "asset_id": UUID, "title": S, "width": PI, "height": PI,
    "mime_type": {"const": "image/png"}, "final_size": PI, "final_sha256": HASH,
}
ASSET_SUMMARY_SEARCH_BASE = {
    **ASSET_SUMMARY_BASE,
    "score": {"type": "number", "minimum": 0, "maximum": 1},
    "search_method": {"type": "string", "enum": ["lexical", "keyword", "semantic", "hybrid"]},
}
ASSET_SUMMARY_LIST = obj(ASSET_SUMMARY_BASE, tuple(ASSET_SUMMARY_BASE))
ASSET_SUMMARY_SEARCH = {
    "oneOf": [
        obj(ASSET_SUMMARY_SEARCH_BASE, tuple(ASSET_SUMMARY_SEARCH_BASE)),
        obj({**ASSET_SUMMARY_SEARCH_BASE, "provenance": {"const": "ocr"}, "source_sha256": HASH},
            (*tuple(ASSET_SUMMARY_SEARCH_BASE), "provenance", "source_sha256")),
    ]
}

ASSET_INFO_BASE = {
    **{key: value for key, value in ASSET_FULL_BASE.items()
       if key not in {"score", "search_method"}},
    "size": NNI, "metadata": {"oneOf": [CANONICAL_METADATA, EMPTY_METADATA]},
    "metadata_revision": NNI, "embedded_metadata_present": B,
    "cabx_chunk_count": NNI, "catalog_drift": B,
}
UNCATALOGED_INFO_REQUIRED = (
    "filepath", "size", "final_sha256", "width", "height", "embedded_metadata_present",
    "provenance_state", "cabx_chunk_count", "catalog_drift", "metadata",
)
CATALOGED_INFO_REQUIRED = (
    "filepath", "asset_id", "title", "description", "alt_text", "tags", "width", "height",
    "final_sha256", "metadata_storage", "provenance_state", "size",
    "metadata", "metadata_revision", "embedded_metadata_present", "cabx_chunk_count", "catalog_drift",
)

OCR_REGION = obj(
    {"text": S, "bbox": arr(NNI, minItems=4, maxItems=4),
     "polygon": arr(arr(NNI, minItems=2, maxItems=2), minItems=1),
     "confidence": nullable({"type": "number", "minimum": 0, "maximum": 1}),
     "paragraph": NNI, "line": NNI, "order": NNI},
    ("text", "bbox", "polygon", "confidence", "paragraph", "line", "order"),
)
OCR_ENGINE = obj(
    {"name": S, "version": S, "model_fingerprint": HASH, "pipeline_version": PI,
     "device": S, "backend": S, "device_binding": S},
    ("name", "version", "model_fingerprint", "pipeline_version", "device", "backend"),
)
OCR_LIMITS = obj(
    {k: PI for k in ("max_png_bytes", "max_pixels", "max_dimension", "max_regions", "max_result_bytes")},
    ("max_png_bytes", "max_pixels", "max_dimension", "max_regions", "max_result_bytes"),
)
OCR_WARNING = {
    "oneOf": [
        obj({"code": {"const": "low_confidence"}, "message": S, "regions": arr(NNI)},
            ("code", "message", "regions")),
        obj({"code": {"const": "partial_text"}, "message": S}, ("code", "message")),
        obj({"code": {"const": "gpu_fallback"}, "message": S, "reason": S},
            ("code", "message", "reason")),
    ]
}
SEARCH_WARNING = obj(
    {"code": {"const": "ocr_freshness"}, "message": S}, ("code", "message")
)

SELF_TEST_SECTION = obj(
    {"id": S, "title": S, "group": nullable(S), "prerequisite_ids": STRINGS,
     "cleanup_ids": STRINGS,
     "scope": {"type": "string", "enum": ["writable", "both", "readonly", "connector", "server"]},
     "available": B},
    ("id", "title", "group", "prerequisite_ids", "cleanup_ids", "scope", "available"),
)

# 13.2.7 (DESIGN-13.2 §7): the rows `workspace_selftest.py` appends to the
# `section="index"` answer (W, W1…W14) — the shape `_WorkspaceSection.row()`
# emits, which is not SELF_TEST_SECTION's. The index has carried both shapes in
# one list since 12.x; the contract only ever described the first, so a client
# that validates results against the advertised outputSchema rejected every
# index for a Workspace-enabled connector. Described as-is rather than
# reshaped: reshaping would change what existing readers see.
WORKSPACE_SELF_TEST_SECTION = obj(
    {"id": S, "title": S, "coverage": STRINGS, "prerequisites": STRINGS, "cleanup": STRINGS,
     "available": B, "bridge": B},
    ("id", "title", "coverage", "prerequisites", "cleanup", "available", "bridge"),
)
SELF_TEST_INDEX_ROW = {"oneOf": [SELF_TEST_SECTION, WORKSPACE_SELF_TEST_SECTION]}

EDIT_ITEM = obj(
    {"index": NNI, "replacements": NNI,
     "match_mode": {"type": "string", "enum": ["exact", "newline_normalized"]}},
    ("index", "replacements", "match_mode"),
)
EDIT_ENGINE = {
    "old_chunks_removed": NNI, "new_chunks_added": NNI, "dedup_skipped": NNI,
    "source": S, "content_sha256": NULL_HASH, **BYTE_FACT_PROPERTIES,
    "tier": TIER, "semantic_searchable": B,
}
LEGACY_WRITE_FIELDS = {
    "old_chunks_removed": NNI, "new_chunks_added": NNI, "dedup_skipped": NNI,
    "previous_backup_id": NULL_S, "overwrote_existing": B,
    "previous_content_sha256": HASH, "gateway_warning": S,
}

TEXT_BLOCK = obj({"type": {"const": "text"}, "text": S}, ("type", "text"))
IMAGE_BLOCK = obj(
    {"type": {"const": "image"}, "data": S, "mimeType": {"const": "image/png"}},
    ("type", "data", "mimeType"),
)
JSONRPC_ERROR = obj({"code": INT, "message": S, "reason": S}, ("code", "message"))


def _with_replay(schema: dict[str, Any]) -> dict[str, Any]:
    for item in schema["oneOf"]:
        if item.get("properties", {}).get("status", {}).get("const") != "error":
            item["properties"]["replayed"] = B
    return schema


def _with_adapter_replay(schema: dict[str, Any]) -> dict[str, Any]:
    """Allow the Workspace/bridge idempotency marker on success envelopes.

    Knowledge mutations retain their historical ``replayed`` marker.  The
    runtime-backed adapters use ``idempotent_replay`` instead, and the marker
    is optional because first executions do not include it.
    """
    for item in schema["oneOf"]:
        if item.get("properties", {}).get("status", {}).get("const") != "error":
            item["properties"]["idempotent_replay"] = B
            # 12.18 (DESIGN-12.18 section 1): a replayed Workspace call also
            # carries the document tools' `replayed` marker, so both replays
            # read the same way to a client. Additive; both stay.
            item["properties"]["replayed"] = B
    return schema


def build_adapter_schemas() -> dict[str, dict[str, Any]]:
    """Build result contracts for gateway-served Workspace and bridge tools."""
    # Adapter tools are separate from the Knowledge registry, but their MCP
    # results participate in the connector batch child union.  ``data`` is
    # intentionally open because its shape is owned by the pinned Workspace
    # runtime operation while the gateway-owned envelope remains typed.
    workspace_info_success = contract(branch(
        "success",
        {"workspace": {"anyOf": [{"type": "object"}, {"type": "null"}]}},
        ("workspace",),
    ))
    workspace_data_success = contract(branch(
        "success",
        {"workspace": {"anyOf": [{"type": "object"}, {"type": "null"}]}, "data": {}},
        ("workspace", "data"),
    ))
    job_result = obj(
        {
            "job_id": S, "state": S, "stdout": S, "stderr": S,
            "exit_code": nullable(INT), "created_at": N, "finished_at": N,
            "duration_seconds": N, "pid": NNI, "process_start_token": S,
            "process_group_id": NNI, "stdout_bytes": NNI, "stderr_bytes": NNI,
            "stdout_first_available_offset": NNI, "stdout_next_offset": NNI,
            "stdout_truncated": B, "stdout_has_more": B,
            "stderr_first_available_offset": NNI, "stderr_next_offset": NNI,
            "stderr_truncated": B, "stderr_has_more": B, "has_more": B,
            # 12.18 (13.1.0): has_more_<stream> mirrors <stream>_has_more;
            # <stream>_encoding/_lossy come from output_encoding/strip_ansi
            # (DESIGN-12.18 section 3.2); <stream>_lines is the broker's line
            # count on a terminal tail read (section 3.3).
            "has_more_stdout": B, "has_more_stderr": B,
            "stdout_encoding": S, "stderr_encoding": S,
            "stdout_lossy": B, "stderr_lossy": B,
            "stdout_lines": NNI, "stderr_lines": NNI,
        },
        ("job_id", "state"),
    )
    workspace_job_success = contract(branch(
        "success",
        {
            "workspace": {"anyOf": [{"type": "object"}, {"type": "null"}]},
            "job": job_result,
            # waited_ms/wake_reason are the top-level keys a waited
            # workspace_start_job/workspace_get_job response adds (A1,
            # DESIGN-12.18 section 3.1). Every other 12.18 field lives inside
            # "workspace" (already open) or "job" (typed above).
            "waited_ms": NNI, "wake_reason": S,
        },
        ("workspace", "job"),
    ))
    manifest_entry = obj(
        {"path": S, "size": NNI, "sha256": HASH},
        ("path", "size", "sha256"),
    )
    bridge_success = contract(branch(
        "success",
        {
            "transfer_id": UUID,
            "direction": {"type": "string", "enum": ["to_workspace", "from_workspace"]},
            "project": S,
            "file_count": NNI,
            "bytes": NNI,
            "manifest": arr(manifest_entry),
            "committed": STRINGS,
            "skipped": STRINGS,
        },
        ("transfer_id", "direction", "project", "file_count", "bytes",
         "manifest", "committed", "skipped"),
    ))
    adapter_mutating = frozenset({
        "workspace_write_file", "workspace_edit_file", "workspace_make_directory",
        "workspace_copy_paths", "workspace_move_paths", "workspace_remove_paths",
        "workspace_start_job", "workspace_cancel_job", "copy_to_workspace",
        "copy_from_workspace",
    })
    schemas: dict[str, dict[str, Any]] = {"workspace_info": workspace_info_success}
    for name in (
        "workspace_list_files", "workspace_stat", "workspace_read_file",
        "workspace_write_file", "workspace_edit_file", "workspace_make_directory",
        "workspace_copy_paths", "workspace_move_paths", "workspace_remove_paths",
        "workspace_search", "workspace_web_search",
    ):
        schema = copy.deepcopy(workspace_data_success)
        schemas[name] = _with_adapter_replay(schema) if name in adapter_mutating else schema
    for name in ("workspace_start_job", "workspace_get_job", "workspace_cancel_job"):
        schema = copy.deepcopy(workspace_job_success)
        schemas[name] = _with_adapter_replay(schema) if name in adapter_mutating else schema
    schemas.update({
        "copy_to_workspace": _with_adapter_replay(copy.deepcopy(bridge_success)),
        "copy_from_workspace": _with_adapter_replay(copy.deepcopy(bridge_success)),
        "workspace_generate_self_test": contract(
            branch(
                "success",
                {"server_version": S, "plan_version": S, "section": S,
                 "catalog_assertions": {}, "plan": S, "sections": arr({}),
                 "workspace_plan_version": S},
                ("server_version", "plan_version", "section", "catalog_assertions"),
            ),
            branch(
                "blocked",
                {"server_version": S, "plan_version": S, "section": S,
                 "catalog_assertions": {}, "reason": S, "missing_tools": STRINGS},
                ("server_version", "plan_version", "section", "catalog_assertions",
                 "reason", "missing_tools"),
            ),
        ),
    })
    return schemas


def build_schemas(mutating_tools: Iterable[str]) -> dict[str, dict[str, Any]]:
    write_common = {**BYTE_FACT_PROPERTIES, "tier": TIER, "semantic_searchable": B,
                    "previous_backup_id": NULL_S, "overwrote_existing": B,
                    "previous_content_sha256": HASH}
    add_props = {"chunks_added": NNI, "dedup_skipped": NNI, "category": S,
                 "filepath": S, "source": S, **write_common}
    add_props["indexing"] = INDEXING_FACT
    add_required = ("chunks_added", "dedup_skipped", "category", "filepath", "source",
                    *BYTE_FACT_REQUIRED, "tier", "semantic_searchable")
    update_props = {"old_chunks_removed": NNI, "new_chunks_added": NNI,
                    "dedup_skipped": NNI, "filepath": S, "source": S, **write_common}
    update_props["indexing"] = INDEXING_FACT
    update_required = ("old_chunks_removed", "new_chunks_added", "dedup_skipped", "filepath",
                       "source", *BYTE_FACT_REQUIRED, "tier", "semantic_searchable")

    schemas: dict[str, dict[str, Any]] = {
        "search_knowledge": contract(
            branch("success", {"query": S, "hybrid_alpha": N, "result_count": NNI,
                   "filtered_by_score": NNI, "cache_hit_rate": N,
                   "results": arr(SEARCH_HIT), "result_key": {"const": "results"}},
                   ("query", "hybrid_alpha", "result_count", "filtered_by_score",
                    "cache_hit_rate", "results", "result_key")),
            branch("success", {"query": S, "results": arr(SEARCH_HIT),
                   "result_key": {"const": "results"}}, ("query", "results", "result_key")),
            branch("no_results", {"query": S, "message": S, "results": arr(SEARCH_HIT, maxItems=0),
                   "result_key": {"const": "results"}}, ("query", "message", "results", "result_key"))),
        "get_document": contract(branch("success", {"document": FULL_DOCUMENT}, ("document",))),
        "get_documents": contract(*[
            branch(status, {"result_key": {"const": "documents"},
                   "documents": arr(PLURAL_DOCUMENT_ITEM), "succeeded": NNI, "failed": NNI,
                   "skipped": NNI}, ("result_key", "documents", "succeeded", "failed", "skipped"))
            for status in ("success", "partial_failure")]),
        "search_similar": contract(
            branch("success", {"reference": S, "count": NNI,
                   "similar_documents": arr(SIMILAR_HIT), "result_key": {"const": "similar_documents"},
                   "results": arr(SIMILAR_HIT)},
                   ("reference", "count", "similar_documents", "result_key", "results")),
            branch("no_results", {"message": S, "similar_documents": arr(SIMILAR_HIT, maxItems=0),
                   "result_key": {"const": "similar_documents"}, "results": arr(SIMILAR_HIT, maxItems=0)},
                   ("message", "similar_documents", "result_key", "results"))),
        "list_documents": contract(branch("success", {
            "filter": S, "prefix": S, "count": NNI, "documents": arr(LIST_DOCUMENT),
            "result_key": {"const": "documents"}, "embedded_count": NNI, "registered_count": NNI,
            "corpus_size": NNI, "message": S, "available_categories": STRINGS,
            "hashes": {"const": "on_disk"}, "drift_count": NNI,
            "missing_on_disk_count": NNI, "drift_hint": S,
        }, ("filter", "prefix", "count", "documents", "result_key", "embedded_count", "registered_count"))),
        "list_categories": contract(branch("success", {
            "categories": {"type": "object", "additionalProperties": {"type": "integer", "minimum": 0}},
            "total_documents": NNI}, ("categories", "total_documents"))),
    }

    tier_stats = obj({"documents": NNI, "chunks": NNI, "vectors": NNI, "extensions": STRINGS},
                     ("documents", "chunks", "vectors", "extensions"))
    reindex_inactive = obj({"active": {"const": False}, "last_result": obj({
        "indexed": NNI, "skipped": NNI, "removed": NNI, "errors": NNI, "total_files": NNI,
        "tier_changed": NNI, "chunks_purged": NNI,
        "sync_conflicts_skipped": NNI}, ("indexed", "skipped", "errors")),
        "last_error": S, "sync_conflicts_skipped": NNI}, ("active",))
    reindex_active = obj({"active": {"const": True}, "operation": nullable(S), "progress": S,
                          "percent": NNI, "indexed": NNI, "skipped": NNI, "errors": NNI,
                          "started_at": nullable(S)},
                         ("active", "operation", "progress", "percent", "indexed", "skipped", "errors", "started_at"))
    reindex_block = {"oneOf": [reindex_inactive, reindex_active]}
    cache_stats = obj({"size": NNI, "max_size": NNI, "hits": NNI, "misses": NNI, "hit_rate": N},
                      ("size", "max_size", "hits", "misses", "hit_rate"))
    scheduler_job = obj({"job_id": S, "project": S, "kind": S, "state": S,
                         "queued": NNI, "in_flight": NNI, "completed": NNI, "failed": NNI,
                         "canceled": NNI, "attempted": NNI,
                         "device_completed": {"type": "object", "additionalProperties": NNI},
                         "waiting_reason": nullable(S)},
                        ("job_id", "project", "kind", "state", "queued", "in_flight", "completed",
                         "failed", "canceled", "attempted", "device_completed", "waiting_reason"))
    scheduler = nullable(obj({"queued": NNI, "in_flight": NNI,
                              "cpu": obj({"state": S, "completed": NNI}, ("state", "completed")),
                              "gpus": arr(obj({"device": S, "state": S, "reason": nullable(S),
                                               "generation": NNI, "completed": NNI, "cooldown": N},
                                              ("device", "state", "reason", "generation", "completed", "cooldown"))),
                              "jobs": arr(scheduler_job)}, ("queued", "in_flight", "cpu", "gpus", "jobs")))
    stats = obj({"total_documents": NNI, "total_chunks": NNI,
                 "tiers": obj({"embedded": tier_stats, "registered": tier_stats}, ("embedded", "registered")),
                 "categories": {"type": "object", "additionalProperties": NNI}, "supported_formats": STRINGS,
                 "embedding_model": S, "embedding_dim": PI, "reranker_model": S,
                 "chunk_size": PI, "chunk_overlap": NNI, "query_cache": cache_stats,
                 "reindex": reindex_block, "scheduler": scheduler,
                 "sync_conflicts": obj({"count": NNI, "files": STRINGS, "patterns": STRINGS, "note": S},
                                       ("count", "files", "patterns", "note")),
                 "deindexed": obj({"count": NNI, "files": STRINGS, "list_file": S, "note": S, "load_error": S},
                                    ("count", "files", "list_file", "note"))},
                ("total_documents", "total_chunks", "tiers", "categories", "supported_formats",
                 "embedding_model", "embedding_dim", "reranker_model", "chunk_size", "chunk_overlap",
                 "query_cache", "reindex", "scheduler", "sync_conflicts", "deindexed"))
    schemas.update({
        "get_index_stats": contract(branch("success", {"stats": stats}, ("stats",))),
        "get_reindex_status": contract(branch("success", {"reindex": reindex_block}, ("reindex",))),
    })

    per_query = obj({"query": S, "expected": S, "found_at_rank": nullable(PI),
                     "reciprocal_rank": N, "top_result": S, "top_result_filepath": NULL_S},
                    ("query", "expected", "found_at_rank", "reciprocal_rank", "top_result", "top_result_filepath"))
    literal_props = {"pattern": S, "regex": B, "case_sensitive": B, "files_scanned": NNI,
                     "files_with_matches": NNI, "total_matches": NNI, "truncated": B,
                     "matches": arr(LITERAL_MATCH), "result_key": {"const": "matches"},
                     "results": arr(LITERAL_MATCH), "category": S, "filepath_glob": S,
                     "reason": {"type": "string", "enum": ["no_documents_selected", "no_matches"]},
                     "corpus_size": NNI, "message": S, "files_skipped": NNI,
                     "skipped": arr(SKIPPED_FILE), "timed_out": {"const": True}, "exhaustive": {"const": False}}
    schemas.update({
        "evaluate_retrieval": contract(branch("success", {"total_queries": PI, "mrr_at_5": N,
            "recall_at_5": N, "per_query": arr(per_query), "result_key": {"const": "per_query"},
            "results": arr(per_query)}, ("total_queries", "mrr_at_5", "recall_at_5", "per_query", "result_key", "results"))),
        "find_literal": contract(branch("success", literal_props,
            ("pattern", "regex", "case_sensitive", "files_scanned", "files_with_matches",
             "total_matches", "truncated", "matches", "result_key"))),
        "add_document": contract(
            branch("success", add_props, add_required),
            branch("success", {"filepath": S, **LEGACY_WRITE_FIELDS, "indexing": INDEXING_FACT},
                   ("filepath", "old_chunks_removed", "new_chunks_added"))),
        "update_document": contract(
            branch("success", update_props, update_required),
            branch("success", {"filepath": S, **LEGACY_WRITE_FIELDS, "indexing": INDEXING_FACT},
                   ("filepath", "old_chunks_removed", "new_chunks_added"))),
        "add_from_url": contract(
            branch("success", add_props, add_required),
            branch("success", {"filepath": S, **LEGACY_WRITE_FIELDS, "indexing": INDEXING_FACT},
                   ("filepath", "old_chunks_removed", "new_chunks_added"))),
        "write_documents": contract(branch("success", {"documents_written": NNI, "chunks_indexed": NNI,
            "filepaths": STRINGS, "receipts": arr(WRITE_RECEIPT),
            "previous_backup_ids": {"type": "object", "additionalProperties": S}},
            ("documents_written", "chunks_indexed", "filepaths", "receipts"))),
        "remove_document": contract(
            branch("success", REMOVE_DOCUMENT_PROPERTIES, REMOVE_DOCUMENT_REQUIRED),
            branch("success", {"filepath": S, **LEGACY_WRITE_FIELDS},
                   ("filepath", "old_chunks_removed", "new_chunks_added"))),
        "remove_documents": contract(*[branch(status, {"result_key": {"const": "documents"},
            "on_error": {"type": "string", "enum": ["stop", "continue"]},
            "documents": arr(REMOVE_DOCUMENT_CHILD), "succeeded": NNI, "failed": NNI, "skipped": NNI,
            "backups": arr(DELETION_BACKUP)},
            ("result_key", "on_error", "documents", "succeeded", "failed", "skipped", "backups"))
            for status in ("success", "partial_failure")]),
        "move_document": contract(branch("success", {"old_filepath": S, "new_filepath": S,
            "filepath": S, "old_source": S, "new_source": S, "source": S, "doc_id": S,
            "chunks_moved": NNI, "previous_backup_id": NULL_S},
            ("old_filepath", "new_filepath", "filepath", "old_source", "new_source", "source", "doc_id", "chunks_moved"))),
        "reindex_documents": contract(
            branch("started", {"operation": S, "message": S}, ("operation", "message")),
            branch("already_running", {"operation": nullable(S), "progress": S, "hint": S},
                   ("operation", "progress", "hint"))),
        "copy_document": contract(branch("success", {"category": S, **COPY_ENTRY_PROPERTIES},
            ("category", "filepath", "source_filepath", "chunks_added", "tier", "indexed", "on_disk"))),
        "copy_directory": contract(branch("success", {"src_prefix": S, "dst_prefix": S,
            "recursive": B, "files_copied": NNI, "chunks_added": NNI, "overwrote": NNI,
            "destination_paths": STRINGS, "documents": arr(COPY_ENTRY),
            "result_key": {"const": "documents"}, "skipped": arr(SKIPPED_FILE)},
            ("src_prefix", "dst_prefix", "recursive", "files_copied", "chunks_added", "overwrote",
             "destination_paths", "documents", "result_key", "skipped"))),
        "remove_directory": contract(
            branch("success", {"prefix": S, "recursive": B,
            "documents_removed": NNI, "chunks_removed": NNI, "files_deleted": NNI,
            "backups": arr(DELETION_BACKUP), "result_key": {"const": "backups"},
            "backup_ids": STRINGS, "pruned_directories": STRINGS,
            "restore_hint": S},
            ("prefix", "recursive", "documents_removed", "chunks_removed", "files_deleted",
             "backups", "result_key", "backup_ids", "pruned_directories")),
            branch("partial", {"prefix": S, "recursive": B,
            "documents_removed": NNI, "chunks_removed": NNI, "files_deleted": NNI,
            "backups": arr(DELETION_BACKUP), "result_key": {"const": "backups"},
            "backup_ids": STRINGS, "pruned_directories": STRINGS,
            "failures": arr(obj({"filepath": S, "error": S}, ("filepath", "error"))),
            "message": S},
            ("prefix", "recursive", "documents_removed", "chunks_removed", "files_deleted",
             "backups", "result_key", "backup_ids", "pruned_directories", "failures", "message"))),
    })

    asset_receipt = {"project": S, "filepath": S, "asset_id": UUID, "received_size": NNI,
                     "received_sha256": HASH, "final_size": PI, "final_sha256": HASH,
                     "width": PI, "height": PI,
                     "metadata_storage": {"type": "string", "enum": ["embedded", "catalog"]},
                     "metadata_revision": PI, "schema_version": {"const": 1},
                     "provenance_state": {"type": "string", "enum": ["none", "cabx_present_unverified"]},
                     "cabx_chunk_count": NNI, "embedded_metadata_present": B,
                     "previous_backup_id": NULL_S, "received_backup_id": NULL_S,
                     "indexed": B, "idempotent_replay": B}
    asset_receipt_required = tuple(asset_receipt)
    uncataloged_info = {key: ASSET_INFO_BASE[key] for key in UNCATALOGED_INFO_REQUIRED}
    cataloged_info = {key: ASSET_INFO_BASE[key] for key in CATALOGED_INFO_REQUIRED}
    schemas.update({
        "put_asset": contract(branch("success", asset_receipt, asset_receipt_required)),
        "update_asset_metadata": contract(branch("success", asset_receipt, asset_receipt_required)),
        "list_assets": contract(branch("success", {"project": S,
            "assets": arr({"oneOf": [ASSET_FULL_LIST, ASSET_SUMMARY_LIST]}, maxItems=200),
            "next_cursor": nullable(S)}, ("project", "assets", "next_cursor"))),
        "search_assets": contract(branch("success", {"project": S,
            "results": arr({"oneOf": [ASSET_FULL_SEARCH, ASSET_SUMMARY_SEARCH]}, maxItems=20),
            "warnings": arr(SEARCH_WARNING), "reason": {"const": "no_matches"}},
            ("project", "results"))),
        "get_asset_info": contract(
            branch("success", {"project": S, **uncataloged_info}, ("project", *UNCATALOGED_INFO_REQUIRED)),
            branch("success", {"project": S, **cataloged_info}, ("project", *CATALOGED_INFO_REQUIRED))),
        "get_asset": contract(
            branch("success", {"project": S, **{k: v for k, v in uncataloged_info.items() if k != "metadata"}},
                   ("project", *[k for k in UNCATALOGED_INFO_REQUIRED if k != "metadata"])),
            branch("success", {"project": S, **{k: v for k, v in cataloged_info.items() if k != "metadata"}},
                   ("project", *[k for k in CATALOGED_INFO_REQUIRED if k != "metadata"]))),
        "reindex_assets": contract(branch("success", {"project": S, "indexed": NNI, "removed": NNI,
            "errors": arr(obj({"filepath": S, "reason": S}, ("filepath", "reason")), maxItems=20),
            "idempotent_replay": B}, ("project", "indexed", "removed", "errors", "idempotent_replay"))),
        "remove_asset": contract(branch("success", {"project": S, "filepath": S, "file_deleted": B,
            "catalog_removed": B, "ocr_removed": B, "deleted_size": NULL_I,
            "deleted_sha256": NULL_HASH, "backup_id": NULL_S, "idempotent_replay": B},
            ("project", "filepath", "file_deleted", "catalog_removed", "ocr_removed", "deleted_size",
             "deleted_sha256", "backup_id", "idempotent_replay"))),
    })
    ocr_common = {"project": S, "filepath": S, "sha256": HASH, "width": PI, "height": PI,
                  "regions": arr(OCR_REGION, maxItems=10000), "engine": OCR_ENGINE,
                  "languages": arr(S, minItems=1, maxItems=3, uniqueItems=True), "cache_hit": B,
                  "warnings": arr(OCR_WARNING, maxItems=128), "duration_ms": NNI, "limits": OCR_LIMITS}
    ocr_required = tuple(ocr_common)
    schemas["ocr_asset"] = contract(
        branch("success", {"outcome": {"const": "text"}, "text": {"type": "string", "minLength": 1},
                           "searchable": {"const": True}, **ocr_common},
               ("outcome", "text", "searchable", *ocr_required)),
        branch("success", {"outcome": {"const": "no_text"}, "text": {"const": ""},
                           "searchable": {"const": False}, **ocr_common},
               ("outcome", "text", "searchable", *ocr_required)))

    read_props = {"filepath": S, "text": S, "total_lines": NNI, "start_line": PI,
                  "end_line": NNI, "truncated": B, "content_sha256": NULL_HASH,
                  "bytes_sha256": HASH, "size_bytes": NNI, "line_endings": S, "utf8_valid": B,
                  "decode_error_bytes": NNI, "content_is_lossy": B, "index_text_sanitized": B,
                  "normalized_line_endings": {"const": False}, "content_note": S,
                  "section": S, "message": S, "mtime": S}
    schemas.update({
        "read_document": contract(branch("success", read_props,
            ("filepath", "text", "total_lines", "start_line", "end_line", "truncated",
             "content_sha256", "bytes_sha256", "size_bytes", "line_endings", "utf8_valid",
             "decode_error_bytes", "content_is_lossy", "index_text_sanitized",
             "normalized_line_endings", "content_note"))),
        "list_backups": contract(branch("success", {"count": NNI, "backups": arr(BACKUP_ENTRY),
            "result_key": {"const": "backups"}, "results": arr(BACKUP_ENTRY), "naming": S,
            "message": S}, ("count", "backups", "result_key", "results", "naming"))),
        "diff_backup": contract(
            branch("success", {"filepath": S, "backup_id": S, "identical": {"const": True}, "message": S},
                   ("filepath", "backup_id", "identical")),
            branch("success", {"filepath": S, "backup_id": S, "identical": {"const": False},
                   "diff": S, "message": S}, ("filepath", "backup_id", "identical", "diff"))),
        # 13.2.7 (DESIGN-13.2 §7): `workspace_plan_version` is OPTIONAL on every
        # success branch. The gateway adds it whenever the connector has
        # Workspace on, and these branches forbid unknown keys — so the result
        # violated its own advertised outputSchema, the server never validated
        # this one tool's result, and a client that validates (SillyTavern's
        # node MCP client) discarded it as "[No content]" while claude.ai,
        # which does not validate, showed it. Additive: an optional output
        # field is not a wire-shape change (CLAUDE.md, D4.4).
        "get_self_test_plan": contract(
            branch("success", {"server_version": S, "plan_version": S, "section": {"const": "full"},
                   "plan": S, "workspace_plan_version": S},
                   ("server_version", "plan_version", "section", "plan")),
            branch("success", {"server_version": S, "plan_version": S, "section": {"const": "index"},
                   "sections": arr(SELF_TEST_INDEX_ROW), "workspace_plan_version": S},
                   ("server_version", "plan_version", "section", "sections")),
            branch("success", {"server_version": S, "plan_version": S, "section": S,
                   "section_id": S, "title": S, "prerequisite_ids": STRINGS, "cleanup_ids": STRINGS,
                   "scope": {"type": "string", "enum": ["writable", "both", "readonly", "connector", "server"]},
                   "instructions": S, "plan": S, "workspace_plan_version": S},
                   ("server_version", "plan_version", "section", "section_id", "title",
                    "prerequisite_ids", "cleanup_ids", "scope", "instructions", "plan"))),
    })

    edit_applied_common = {"filepath": S, "new_content_sha256": HASH,
                           "previous_backup_id": S, "context_diff": S, **EDIT_ENGINE}
    edit_applied_required = ("filepath", "new_content_sha256", "previous_backup_id", "context_diff",
                             "old_chunks_removed", "new_chunks_added", "dedup_skipped")
    dry_common = {"filepath": S, "dry_run": {"const": True}, "applied": {"const": False},
                  "current_content_sha256": HASH, "context_diff": S, "message": S}
    schemas.update({
        "edit_document": contract(
            branch("success", {**edit_applied_common, "replacements": NNI,
                   "match_mode": {"type": "string", "enum": ["exact", "newline_normalized"]}},
                   (*edit_applied_required, "replacements", "match_mode")),
            branch("success", {**dry_common, "replacements": NNI,
                   "match_mode": {"type": "string", "enum": ["exact", "newline_normalized"]}},
                   ("filepath", "dry_run", "applied", "current_content_sha256", "context_diff", "message",
                    "replacements", "match_mode"))),
        "edit_document_batch": contract(
            branch("success", {**edit_applied_common, "edits_applied": NNI, "replacements": NNI,
                   "edits": arr(EDIT_ITEM)}, (*edit_applied_required, "edits_applied", "replacements", "edits")),
            branch("success", {**dry_common, "edits_applied": NNI, "replacements": NNI,
                   "edits": arr(EDIT_ITEM)},
                   ("filepath", "dry_run", "applied", "current_content_sha256", "context_diff", "message",
                    "edits_applied", "replacements", "edits"))),
        "insert_in_document": contract(
            branch("success", {**edit_applied_common, "inserted_at_line": PI,
                   "position": {"type": "string", "enum": ["start", "end", "end_of_section", "end_of_intro"]},
                   "section": S},
                   (*edit_applied_required, "inserted_at_line", "position")),
            branch("success", {**dry_common, "inserted_at_line": PI,
                   "position": {"type": "string", "enum": ["start", "end", "end_of_section", "end_of_intro"]},
                   "section": S},
                   ("filepath", "dry_run", "applied", "current_content_sha256", "context_diff", "message",
                    "inserted_at_line", "position"))),
        "restore_backup": contract(
            branch("success", {**edit_applied_common, "restored_from_backup": S},
                   ("filepath", "new_content_sha256", "previous_backup_id", "context_diff",
                    "old_chunks_removed", "new_chunks_added", "dedup_skipped", "source",
                    *BYTE_FACT_REQUIRED, "tier", "semantic_searchable", "restored_from_backup")),
            branch("success", {"filepath": S, "restored_from_backup": S, "new_content_sha256": HASH,
                   "context_diff": S, **add_props},
                   ("filepath", "restored_from_backup", "new_content_sha256", "context_diff",
                    "chunks_added", "dedup_skipped", "category", "source", *BYTE_FACT_REQUIRED,
                    "tier", "semantic_searchable")),
            branch("success", {"filepath": S, "restored_from_backup": S,
                   "new_content_sha256": HASH, "context_diff": S,
                   "old_chunks_removed": NNI, "new_chunks_added": NNI, "dedup_skipped": NNI,
                   "previous_backup_id": S},
                   ("filepath", "restored_from_backup", "new_content_sha256", "context_diff",
                    "old_chunks_removed", "new_chunks_added", "dedup_skipped", "previous_backup_id")),
            branch("success", {"filepath": S, "restored_from_backup": S,
                   "new_content_sha256": HASH, "context_diff": S,
                   "old_chunks_removed": NNI, "new_chunks_added": NNI, "dedup_skipped": NNI},
                   ("filepath", "restored_from_backup", "new_content_sha256", "context_diff",
                    "old_chunks_removed", "new_chunks_added", "dedup_skipped"))),
    })

    project_access = obj({"name": S, "access": {"type": "string", "enum": ["read", "write"]}},
                         ("name", "access"))
    schemas["list_projects"] = contract(branch("success", {
        "connector": obj({"id": UUID, "name": S}, ("id", "name")), "revision": NNI,
        "projects": arr(project_access)}, ("connector", "revision", "projects")))

    # Generated book/storage envelopes are included in batch child validation.
    from .books.schemas import (
        BOOK_OUTPUT_SCHEMAS,
        PROJECT_STORAGE_OUTPUT_SCHEMAS, PROJECT_STORAGE_MUTATING_TOOLS,
    )
    implemented_book_tools = {
        "audiobook_inspect_chapter", "audiobook_prepare_chapter",
        "audiobook_get_chapter", "audiobook_find_chunk",
    }
    schemas.update({name: schema for name, schema in BOOK_OUTPUT_SCHEMAS.items()
                    if name in implemented_book_tools})
    schemas.update(PROJECT_STORAGE_OUTPUT_SCHEMAS)

    # Batch child structured content is a discriminated union of every other
    # public result branch.  ``anyOf`` is intentional because extensible error
    # envelopes overlap; successful records remain closed by their own schema.
    adapter_schemas = build_adapter_schemas()
    payload_variants = [copy.deepcopy(item) for name, schema in schemas.items()
                        if name != "batch" for item in schema["oneOf"]]
    payload_variants.extend(
        copy.deepcopy(item)
        for schema in adapter_schemas.values()
        for item in schema["oneOf"]
    )
    # Connector gateways can front a pre-10.1 worker while advertising the
    # gateway's current contract.  Its retained batch child is still bounded:
    # the legacy success envelope contains only the worker's tool marker.
    payload_variants.append(obj({"status": {"const": "success"}, "tool": S}, ("status", "tool")))
    child_error = {
        "oneOf": [
            JSONRPC_ERROR,
            obj({"reason": S, "message": S}, ("reason",)),
        ]
    }
    batch_receipt = obj({
        "status": S, "reason": S, "filepath": S, "filepaths": STRINGS,
        "bytes_sha256": NULL_HASH, "size_bytes": NNI, "line_endings": LINE_ENDINGS,
        "previous_backup_id": NULL_S,
        "previous_backup_ids": {"type": "object", "additionalProperties": S},
        "backup_id": NULL_S, "backups": arr(DELETION_BACKUP),
        "documents_written": NNI, "succeeded": NNI, "failed": NNI, "skipped": NNI,
        "message": S, "receipt_truncated": {"const": True},
    }, ("status",))
    content_blocks = arr({"oneOf": [TEXT_BLOCK, IMAGE_BLOCK]}, minItems=1)
    mcp_result = {
        "oneOf": [
            obj({"content": content_blocks, "structuredContent": {"anyOf": payload_variants},
                 "isError": B}, ("content", "structuredContent", "isError")),
            # A legacy worker has no structuredContent.  The gateway decodes its
            # first text block for accounting but retains the original MCP result.
            obj({"content": content_blocks, "isError": B}, ("content", "isError")),
        ]
    }
    batch_child = {"oneOf": [
        obj({"index": NNI, "tool": S, "status": {"type": "string", "enum": ["success", "error"]},
             "result": mcp_result, "error": child_error}, ("index", "tool", "status", "result")),
        obj({"index": NNI, "tool": S, "status": {"type": "string", "enum": ["success", "error"]},
             "result_omitted": {"const": True}, "reason": {"const": "result_too_large"},
             "receipt": batch_receipt},
            ("index", "tool", "status", "result_omitted", "reason")),
        obj({"index": NNI, "tool": S, "status": {"const": "error"}, "reason": S,
             "error": child_error}, ("index", "tool", "status", "reason", "error")),
        obj({"index": NNI, "tool": S, "status": {"const": "skipped"},
             "reason": {"const": "previous_error"}}, ("index", "tool", "status", "reason")),
    ]}
    schemas["batch"] = contract(*[
        branch(status, {"result_key": {"const": "results"},
               "on_error": {"type": "string", "enum": ["stop", "continue"]},
               "results": arr(batch_child), "succeeded": NNI, "failed": NNI,
               "skipped": NNI, "omitted": NNI},
               ("result_key", "on_error", "results", "succeeded", "failed", "skipped", "omitted"))
        for status in ("success", "partial_failure")])

    for name in mutating_tools:
        schemas[name] = _with_replay(schemas[name])
    return schemas
