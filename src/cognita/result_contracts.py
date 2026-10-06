"""Canonical MCP structured-output contracts.

This module intentionally has no dependency on the engine, gateway, project
registry, or asset service.  It is the small shared boundary those layers use
to advertise and produce the same result contract.
"""

from __future__ import annotations

import copy
import json
import logging
import uuid
from collections.abc import Iterable, Mapping
from typing import Any

from jsonschema import Draft202012Validator, ValidationError

from .strict_result_schemas import build_adapter_schemas, build_schemas
from .books.schemas import ALL_ADDITIVE_MUTATING_TOOLS

log = logging.getLogger(__name__)

# Keep this list independent from catalog construction.  Importing proxy or
# engine here would create the circular dependency this module is meant to
# prevent.  The tuple is the public writable catalog in catalog order.
PUBLIC_TOOL_NAMES: tuple[str, ...] = (
    "search_knowledge", "get_document", "search_similar", "get_documents",
    "list_documents", "list_categories", "get_index_stats", "get_reindex_status",
    "evaluate_retrieval", "add_document", "update_document", "write_documents",
    "remove_document", "remove_documents", "move_document", "add_from_url",
    "reindex_documents", "find_literal", "copy_document", "copy_directory",
    "remove_directory", "put_asset", "update_asset_metadata", "search_assets",
    "list_assets", "get_asset_info", "get_asset", "reindex_assets", "remove_asset", "ocr_asset",
    "audiobook_inspect_chapter", "audiobook_prepare_chapter",
    "audiobook_get_chapter", "audiobook_find_chunk",
    "audiobook_record_generation", "audiobook_import_audio",
    "audiobook_build", "audiobook_commit_build",
    "audiobook_get_job", "audiobook_cancel_job", "audiobook_get_generations",
    "set_folder_indexing", "list_project_files", "read_project_file",
    "read_document", "list_backups", "diff_backup", "get_self_test_plan",
    "edit_document", "edit_document_batch", "insert_in_document", "restore_backup",
    "batch", "list_projects",
)

MUTATING_TOOLS = frozenset({
    "add_document", "update_document", "write_documents", "remove_document",
    "remove_documents", "move_document", "add_from_url", "reindex_documents",
    "copy_document", "copy_directory", "remove_directory", "put_asset",
    "update_asset_metadata", "reindex_assets", "remove_asset", "edit_document", "edit_document_batch",
    "insert_in_document", "restore_backup",
    "audiobook_prepare_chapter", "audiobook_record_generation", "audiobook_import_audio",
    "audiobook_build", "audiobook_commit_build",
    "audiobook_cancel_job", "set_folder_indexing",
})

OUTPUT_SCHEMAS_BY_TOOL = build_schemas(MUTATING_TOOLS)
ADAPTER_OUTPUT_SCHEMAS_BY_TOOL = build_adapter_schemas()
_ALL_OUTPUT_SCHEMAS_BY_TOOL = {
    **OUTPUT_SCHEMAS_BY_TOOL, **ADAPTER_OUTPUT_SCHEMAS_BY_TOOL,
}
ADAPTER_MUTATING_TOOLS = frozenset({
    "workspace_write_file", "workspace_edit_file", "workspace_make_directory",
    "workspace_copy_paths", "workspace_move_paths", "workspace_remove_paths",
    "workspace_start_job", "workspace_cancel_job", "copy_to_workspace",
    "copy_from_workspace",
})


def validate_schema_registry(tool_names: Iterable[str] = PUBLIC_TOOL_NAMES) -> None:
    """Validate every schema and require exact catalog/registry name equality."""
    expected = tuple(tool_names)
    actual = tuple(OUTPUT_SCHEMAS_BY_TOOL)
    if set(expected) != set(actual) or len(expected) != len(actual):
        raise RuntimeError(
            f"structured output contract mismatch: missing={sorted(set(expected)-set(actual))} "
            f"orphaned={sorted(set(actual)-set(expected))}"
        )
    for name, schema in OUTPUT_SCHEMAS_BY_TOOL.items():
        try:
            Draft202012Validator.check_schema(schema)
        except Exception as exc:  # pragma: no cover - startup diagnostic
            raise RuntimeError(f"invalid output schema for {name}: {exc}") from exc
        if schema.get("oneOf", [{}])[0].get("type") != "object":
            raise RuntimeError(f"output schema for {name} does not have an object root")


validate_schema_registry()
_VALIDATORS = {
    name: Draft202012Validator(schema)
    for name, schema in _ALL_OUTPUT_SCHEMAS_BY_TOOL.items()
}


def validator_for_tool(tool_name: str) -> Draft202012Validator:
    try:
        return _VALIDATORS[tool_name]
    except KeyError as exc:
        raise KeyError(f"unknown public tool: {tool_name}") from exc


def attach_output_schema(tool_definition: Mapping[str, Any]) -> dict[str, Any]:
    """Deep-copy a tool definition and attach its canonical output schema."""
    name = tool_definition.get("name")
    if name not in _ALL_OUTPUT_SCHEMAS_BY_TOOL:
        raise KeyError(f"unknown public tool: {name!r}")
    result = copy.deepcopy(dict(tool_definition))
    result["outputSchema"] = copy.deepcopy(_ALL_OUTPUT_SCHEMAS_BY_TOOL[name])
    return result


def output_schema_for_tool(tool_name: str) -> dict[str, Any]:
    """Return an isolated copy suitable for a tools/list definition."""
    return copy.deepcopy(_ALL_OUTPUT_SCHEMAS_BY_TOOL[tool_name])


def validate_structured_payload(tool_name: str, payload: Any) -> None:
    """Raise :class:`jsonschema.ValidationError` for a non-conforming payload."""
    validator_for_tool(tool_name).validate(payload)


# Short aliases make the contract boundary convenient for gateway adapters
# without exposing the validator cache as mutable state.
validate_output = validate_structured_payload
schema_for_tool = output_schema_for_tool


def _encoded_payload(payload: Mapping[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _fallback(tool_name: str, *, mutating: bool, correlation_id: str) -> dict[str, Any]:
    if mutating:
        return {
            "status": "error", "reason": "output_contract_violation",
            "message": "The operation completed far enough that its outcome must be verified before retrying.",
            "operation_outcome": "unknown", "correlation_id": correlation_id,
        }
    return {
        "status": "error", "reason": "internal_error",
        "message": "The operation returned an invalid result.", "correlation_id": correlation_id,
    }


def build_tool_result(
    tool_name: str,
    payload: Mapping[str, Any],
    *,
    is_error: bool = False,
    extra_content: Iterable[Mapping[str, Any]] = (),
    mutating: bool | None = None,
    error: bool | None = None,
) -> dict[str, Any]:
    """Validate and build synchronized MCP text/structured result content.

    ``extra_content`` is appended verbatim (for example the PNG block).  It is
    deliberately excluded from structured content, so binary bytes cannot leak
    into the advertised JSON result.
    """
    if error is not None:
        is_error = bool(error)
    if not isinstance(payload, Mapping):
        valid = False
    else:
        try:
            validate_structured_payload(tool_name, payload)
            valid = True
        except ValidationError as exc:
            valid = False
            cid = uuid.uuid4().hex
            log.error(
                "structured output contract violation tool=%s schema_path=%s keyword=%s "
                "payload_type=%s correlation_id=%s",
                tool_name, ".".join(str(p) for p in exc.absolute_path),
                exc.validator, type(payload).__name__, cid,
            )
    if not valid:
        cid = locals().get("cid", uuid.uuid4().hex)
        payload = _fallback(
            tool_name,
            mutating=(
                tool_name in (MUTATING_TOOLS | ADAPTER_MUTATING_TOOLS | ALL_ADDITIVE_MUTATING_TOOLS)
                if mutating is None else mutating
            ),
            correlation_id=cid,
        )
        is_error = True
        # Contract-invalid output is replaced, not decorated. In particular,
        # never retain private image or document content on the containment
        # error returned to the client.
        extra_content = ()
    structured = dict(payload)
    content: list[dict[str, Any]] = [{"type": "text", "text": _encoded_payload(structured)}]
    content.extend(copy.deepcopy(list(extra_content)))
    return {"content": content, "structuredContent": structured, "isError": bool(is_error)}


# Names used by early integration branches; retain one obvious alias while all
# callers converge on build_tool_result.
build_result = build_tool_result
