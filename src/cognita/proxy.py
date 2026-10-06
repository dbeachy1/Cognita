"""Streaming MCP reverse proxy (DESIGN.md §4.3, §4.4).

Forwards MCP streamable-http traffic from the authenticated gateway endpoint
to a project's worker, preserving sessions and streaming SSE without
buffering. Enforces the remote read-only policy (§6):

- tools/call for a mutating tool  -> synthesized JSON-RPC error, NOT forwarded
- legacy tools/list responses     -> normalized with the stable public catalog
- tools/call edit_document        -> gateway tool (DESIGN-2.0-edit-document.md):
                                     anchored splice done here, then transformed
                                     into an update_document call to the worker

Everything else passes through untouched.
"""

from __future__ import annotations

import asyncio
import base64
import copy
import hashlib
import json
import logging
import re
from contextlib import AsyncExitStack, asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Callable

import httpx
from fastapi import Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from .assets.wire import ASSET_MUTATING_TOOLS
from .workspace_selftest import WORKSPACE_SELFTEST_TOOL_NAME, workspace_selftest_tool_definition
from .backups import (
    DIFF_BACKUP_TOOL_DEF,
    DIFF_BACKUP_TOOL_NAME,
    LIST_BACKUPS_TOOL_DEF,
    LIST_BACKUPS_TOOL_NAME,
    RESTORE_BACKUP_TOOL_DEF,
    RESTORE_BACKUP_TOOL_NAME,
    BackupError,
    backup_id_of,
    backup_if_exists,
    find_backup,
    list_backup_entries,
    resolve_target,
)
from .byte_facts import (
    MAX_BASE64_ATOMIC_SET_BYTES,
    check_expected_bytes_sha256,
    check_expected_bytes_sha256_digest,
    classify_text_bytes,
    decode_base64,
    validate_expected_bytes_sha256,
)
# The historical v3 catalog name stays importable from ``compatibility`` for
# migration/test code, but it must not be used to construct or authorize a
# public route: retired catalogs are not exposed by ``public_tool_catalog``,
# and Workspace has its own current-only v3 catalog and route window.
# (Superseded: this module imported ``V3_PUBLIC_TOOL_NAMES`` alongside the
# payload helper and never used it; importing it here made it look like part
# of the proxy's route surface, which is exactly what it must not be.)
from .compatibility import upgrade_required_payload
from .connectors import PUBLIC_CONTRACT_VERSION, WORKSPACE_CONTRACT_VERSION
from .editing import (
    BATCH_TOOL_DEF,
    BATCH_TOOL_NAME,
    EDIT_TOOL_DEF,
    EDIT_TOOL_NAME,
    MAX_FILE_BYTES,
    SHA_PREFIX_MIN,
    EditReject,
    apply_batch,
    apply_edit,
    content_sha256,
    restore_line_endings,
    sha_matches,
)
from .idempotency import (
    OPERATION_ID_ARG,
    REPLAY_MARKER,
    OperationLog,
    normalize_operation_id,
    operation_request_digest,
    with_operation_id_argument,
)
from .manifest import TEXT_HASH_MAX_BYTES
from .reading import (
    INSERT_TOOL_DEF,
    INSERT_TOOL_NAME,
    READ_TOOL_DEF,
    READ_TOOL_NAME,
    apply_insert,
    read_slice,
)
from .readonly import MUTATING_TOOLS, READONLY_TOOLS, is_tool_allowed_remote
from .books.schemas import ALL_ADDITIVE_MUTATING_TOOLS
from .result_contracts import (
    OUTPUT_SCHEMAS_BY_TOOL, attach_output_schema, build_tool_result,
    normalize_legacy_error_payload,
)
from .selftest import SELFTEST_TOOL_DEF, SELFTEST_TOOL_NAME, select_self_test_plan
from .toolargs import reject_unknown_arguments, reject_wrong_types, wire_error

log = logging.getLogger("cognita.proxy")

# 5.2 (review finding C6): remembers completed operation_ids so a client retry
# after a timeout replays the original result instead of re-running the write
# against the state that write already produced. Process-wide, like the write
# locks beside it.
_operations = OperationLog()

# Hop-by-hop / gateway-managed headers never forwarded in either direction.
_SKIP_REQUEST_HEADERS = {
    "host", "authorization", "content-length", "connection", "transfer-encoding",
    "x-cognita-connector-id", "x-cognita-project-key-project",
    "x-cognita-principal-id",
}
_SKIP_RESPONSE_HEADERS = {
    "content-length", "connection", "transfer-encoding", "date", "server",
}

# Generous read timeout: GET SSE channels are long-lived by design (§4.4 #5).
TIMEOUT = httpx.Timeout(connect=5.0, read=None, write=30.0, pool=None)


def make_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=TIMEOUT)


def _forward_headers(request: Request) -> dict[str, str]:
    headers = {
        k: v for k, v in request.headers.items() if k.lower() not in _SKIP_REQUEST_HEADERS
    }
    # Gateway-only trusted context. The asset worker can consume this at the
    # engine/AssetService seam; an untrusted caller cannot spoof it because the
    # incoming header is removed before this value is added.
    connector_id = getattr(getattr(request, "state", None), "cognita_connector_id", None)
    if isinstance(connector_id, str) and connector_id:
        headers["x-cognita-connector-id"] = connector_id
    project_key_project = getattr(
        getattr(request, "state", None), "cognita_project_key_project", None
    )
    if isinstance(project_key_project, str) and project_key_project:
        headers["x-cognita-project-key-project"] = project_key_project
    principal = getattr(getattr(request, "state", None), "cognita_principal", None)
    if principal is not None:
        principal_id = getattr(principal, "principal_id", None)
        key_id = getattr(principal, "key_id", None)
        identity = principal_id or key_id
        if isinstance(identity, str) and identity:
            # This value comes only from the gateway's authenticated request
            # state. The caller-supplied header was removed above.
            headers["x-cognita-principal-id"] = identity
    return headers


def _response_headers(upstream: httpx.Response) -> dict[str, str]:
    return {
        k: v for k, v in upstream.headers.items() if k.lower() not in _SKIP_RESPONSE_HEADERS
    }


def _jsonrpc_error(msg_id, message: str, code: int = -32602) -> JSONResponse:
    """Synthesized JSON-RPC error. streamable-http lets servers answer POSTs
    with plain application/json, so clients handle this without SSE framing."""
    return JSONResponse(
        {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}
    )


def _filter_tools_payload(payload: dict) -> dict:
    """Drop non-allow-listed tools from a tools/list JSON-RPC response."""
    result = payload.get("result")
    if isinstance(result, dict) and isinstance(result.get("tools"), list):
        result["tools"] = [t for t in result["tools"] if t.get("name") in READONLY_TOOLS]
    return payload


def _inject_gateway_tools_payload(payload: dict, readonly: bool) -> dict:
    """Add the gateway's own tools to a legacy worker tools/list response."""
    result = payload.get("result")
    if isinstance(result, dict) and isinstance(result.get("tools"), list):
        defs = [READ_TOOL_DEF, LIST_BACKUPS_TOOL_DEF, DIFF_BACKUP_TOOL_DEF, SELFTEST_TOOL_DEF,
                EDIT_TOOL_DEF, BATCH_TOOL_DEF, INSERT_TOOL_DEF, RESTORE_BACKUP_TOOL_DEF,
                CONNECTOR_BATCH_TOOL_DEF]
        present = {t.get("name") for t in result["tools"]}
        for tool_def in defs:
            if tool_def["name"] not in present:
                result["tools"].append(copy.deepcopy(tool_def))
        if LIST_PROJECTS_TOOL_NAME not in present:
            result["tools"].append(copy.deepcopy(LIST_PROJECTS_TOOL_DEF))
        # 5.2: every MUTATING tool advertises the optional operation_id, including
        # the engine's own — the gateway consumes the argument, so it has to be
        # discoverable here or a caller could never learn it exists (and the
        # strict-argument gate would refuse it as undeclared).
        if not readonly:
            result["tools"] = [
                with_operation_id_argument(t)
                if isinstance(t, dict) and t.get("name") in MUTATING_TOOLS else t
                for t in result["tools"]
            ]
    return payload


def _stamp_tools_payload(payload: dict, project_name: str, documents_dir) -> dict:
    """Prefix every tool description with the project's identity.

    Multiple Cognita connectors in one chat expose byte-identical toolsets —
    tool descriptions are the model's primary signal for picking a tool, so the
    stamp is what makes "which knowledge base?" unambiguous (DESIGN.md §4.1 #3).
    """
    if not project_name or project_name == "?":
        return payload
    folder = Path(documents_dir).name if documents_dir else ""
    tag = f"[{project_name} knowledge base" + (f" — folder: {folder}]" if folder else "]")
    result = payload.get("result")
    if isinstance(result, dict) and isinstance(result.get("tools"), list):
        for tool in result["tools"]:
            if isinstance(tool, dict):
                desc = tool.get("description") or ""
                tool["description"] = f"{tag} {desc}".rstrip()
    return payload


def _rewrite_buffered_response(upstream: httpx.Response, transform) -> Response:
    """Buffer one (small) upstream response — JSON or SSE-framed — apply a
    payload transform to each JSON-RPC payload, re-emit in the same framing.

    Used for tools/list and the edit_document result; everything else streams.
    """
    content_type = upstream.headers.get("content-type", "")
    body = upstream.content
    try:
        if content_type.startswith("application/json"):
            payload = transform(json.loads(body))
            return JSONResponse(payload, status_code=upstream.status_code,
                                headers=_response_headers(upstream))
        if content_type.startswith("text/event-stream"):
            out_lines: list[str] = []
            for line in body.decode("utf-8", errors="replace").splitlines():
                if line.startswith("data:"):
                    data = line[5:].strip()
                    try:
                        payload = transform(json.loads(data))
                        line = "data: " + json.dumps(payload)
                    except json.JSONDecodeError:
                        pass  # non-JSON data frame; pass through
                out_lines.append(line)
            return Response("\n".join(out_lines) + "\n",
                            status_code=upstream.status_code,
                            headers=_response_headers(upstream),
                            media_type="text/event-stream")
    except Exception:  # rewriting must never take the endpoint down
        log.exception("buffered response rewrite failed; passing through unmodified")
    return Response(body, status_code=upstream.status_code,
                    headers=_response_headers(upstream), media_type=content_type or None)


def _rewrite_tools_list_response(
    upstream: httpx.Response, readonly: bool, project_name: str = "?", documents_dir=None,
    contract_version: int = PUBLIC_CONTRACT_VERSION,
) -> Response:
    """Normalize a legacy worker tools/list response to the public contract."""

    def transform(payload: dict) -> dict:
        # The public contract is stable per generation across connectors.
        # Read-only is enforced at tools/call, so discovery does not change when
        # an administrator changes a connector relationship.  Do not merge the
        # upstream worker's list here: older workers omit asset/gateway tools,
        # and exposing that omission makes clients treat required tools as
        # optional.  Calls to an unavailable worker capability fail explicitly.
        result = payload.setdefault("result", {})
        if isinstance(result, dict):
            result["tools"] = public_tool_catalog(contract_version)
        return payload

    return _rewrite_buffered_response(upstream, transform)


# Mutating tools that carry a `filepath` we must back up before the worker writes.
# KNOWN LIMITATION: add_from_url is mutating but absent — its filename is derived
# from the URL/title INSIDE the engine, so the gateway cannot know which file to
# back up. An add_from_url whose derived name collides with an existing document
# overwrites it without a snapshot; every other write path is backed up.
# move_document backs up its SOURCE (arguments.filepath) before the engine relocates
# it. The destination is refused if it already exists. remove_document owns its
# snapshot inside the engine's delete lock, including for unindexed sources;
# backing it up here as well would create two snapshots for one deletion.
_BACKUP_TOOLS = frozenset(
    {"update_document", "add_document", "move_document"}
)

# 🔴 6.1.0: write_documents is mutating and is NOT in `_BACKUP_TOOLS`, because
# that path is built around a single `arguments.filepath` — it locks one file
# and backs up one file. A batch has N, so it gets its own handler below.
#
# ⚠️ IT MUST NOT SIMPLY BE ADDED TO THE SET ABOVE. `_handle_engine_write` reads
# `arguments.filepath`, finds none on a batch, and takes the "no filepath to
# lock/back up" branch — which forwards the call with NO backup and NO edit
# lock. That is silent: the write succeeds, the backups just do not exist, and
# the lost-update race the lock exists to stop is reopened. A mutating tool the
# gateway does not recognize degrades to unprotected rather than refused.
BATCH_WRITE_TOOL_NAME = "write_documents"

CONNECTOR_BATCH_TOOL_NAME = "batch"
CONNECTOR_BATCH_TOOL_DEF: dict = {
    "name": CONNECTOR_BATCH_TOOL_NAME,
    "description": (
        "Sequential and non-atomic. Successful earlier calls remain applied after a "
        "failure; nothing rolls back. Other callers may interleave between elements. "
        "Use write_documents for an all-or-nothing document set. Executes 1-50 child "
        "calls, each with its own exact project, through normal authenticated policy, "
        "argument validation, locking, and replay. on_error=stop skips later calls after "
        "an error or oversized result; on_error=continue attempts independent calls. "
        "Nested batch and get_asset are not allowed. The request is limited to 8 MiB of "
        "UTF-8 JSON and each serialized child result to 1 MiB; larger reads require an "
        "individual or ranged tool."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "calls": {
                "type": "array", "minItems": 1, "maxItems": 50,
                "items": {"type": "object"},
                "description": "Exact objects containing only tool and arguments.",
            },
            "on_error": {
                "type": "string", "enum": ["stop", "continue"], "default": "stop",
            },
        },
        "required": ["calls"],
    },
    # The envelope may contain writes even though read-only connectors can use
    # it for reads; clients must therefore treat it as potentially mutating.
    "annotations": {"readOnlyHint": False, "destructiveHint": False},
}


# Request logging (2.10.1): log the tool and bounded argument SHAPE, never
# values. Truncating content still leaked the first 200 characters of private
# documents and edit anchors into the Docker log (observed 2026-09-19). Even a
# short query, path, operation_id, or nested batch argument may be personal.
def _log_arg_shape(value) -> dict:
    if not isinstance(value, dict):
        return {"argument_type": type(value).__name__}
    # Keys are client-controlled too; a malformed call could put document text
    # in a key, so even truncated key names are not safe log material.
    result: dict = {"argument_count": len(value)}
    for name in ("documents", "calls"):
        items = value.get(name)
        if isinstance(items, list):
            result[f"{name}_count"] = len(items)
    image = value.get("image")
    if isinstance(image, dict) and isinstance(image.get("image_url"), str):
        result["image_url_chars"] = len(image["image_url"])
    return result


def _log_request(project: str, message: dict) -> None:
    """Log the requested tool and shape without client-supplied values."""
    method = message.get("method")
    if method == "tools/call":
        params = _params_of(message)  # tolerate malformed (non-dict) params
        tool = params.get("name", "?")
        if not isinstance(tool, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", tool):
            tool = "<invalid>"
        args = json.dumps(_log_arg_shape(params.get("arguments") or {}), ensure_ascii=False)
        log.info("MCP [%s] call %s %s", project, tool, args)
    elif method:
        # An unknown method is client-controlled as well. Keep diagnostics for
        # protocol methods without writing arbitrary request text to the log.
        safe_method = method if isinstance(method, str) and re.fullmatch(r"[a-z][a-z0-9_/]{0,63}", method) else "<invalid>"
        log.debug("MCP [%s] %s", project, safe_method)


def _tool_result(msg_id, payload: dict, tool_name: str | None = None) -> JSONResponse:
    """Synthesized MCP tool result (status JSON in a text block) — the engine's
    own error convention, so the model handles gateway and engine errors
    identically and can self-correct in-context. JSON-RPC errors stay reserved
    for policy blocks (read-only).

    5.0 §11.1: isError now tracks the payload's own status instead of being
    hardcoded false. A failing tool used to arrive as HTTP 200 with a
    well-formed result and status:"error" buried inside the text block, so a
    client checking transport status, or merely the presence of `result`, read
    it as success — which is how a get_document for a DELETED file was reported
    as "the file is still present" on 2026-08-29. The status field is unchanged
    for every existing client; the flag is what a new one will check first.
    """
    if payload.get("status") == "error" and not payload.get("reason"):
        # 5.0.2 backstop, mirroring engine_local._with_reason. Clients are told
        # to branch on `reason` and never on message text; that is only honest
        # if the field is always present.
        log.warning("gateway error with no reason field: %r", payload.get("message"))
        payload["reason"] = "error"
    # Error branches are shared by every tool.  Successful gateway results are
    # identified by their stable shape so locally synthesized responses also
    # pass the same validator as worker responses.
    if tool_name is None and payload.get("status") != "error":
        if "plan_version" in payload:
            tool_name = SELFTEST_TOOL_NAME
        elif "identical" in payload and "backup_id" in payload:
            tool_name = DIFF_BACKUP_TOOL_NAME
        elif "naming" in payload and "backups" in payload:
            tool_name = LIST_BACKUPS_TOOL_NAME
        elif "text" in payload and ("content_sha256" in payload or "bytes_sha256" in payload):
            tool_name = READ_TOOL_NAME
        elif "context_diff" in payload:
            tool_name = (BATCH_TOOL_NAME if "edits_applied" in payload else
                         INSERT_TOOL_NAME if "inserted_at_line" in payload else EDIT_TOOL_NAME)
    if tool_name is not None:
        result = build_tool_result(tool_name, payload,
                                   is_error=payload.get("status") == "error",
                                   mutating=tool_name in MUTATING_TOOLS)
    else:
        # Generic errors are valid against every public tool's common error
        # branch; retain this fallback for policy refusals before tool routing.
        normalized = normalize_legacy_error_payload(payload)
        result = {"content": [{"type": "text", "text": json.dumps(normalized, indent=2)}],
                  "structuredContent": normalized,
                  "isError": payload.get("status") == "error"}
    return JSONResponse({"jsonrpc": "2.0", "id": msg_id, "result": result})


# Gateway-served tools, by name — the schemas 2.4's strict argument check runs
# against. The engine validates its own 18 against ENGINE_TOOL_DEFS; neither
# layer may import the other, which is why toolargs.py holds the logic and each
# side supplies its own defs.
GATEWAY_TOOL_DEFS: dict[str, dict] = {
    d["name"]: d
    for d in (
        READ_TOOL_DEF, LIST_BACKUPS_TOOL_DEF, DIFF_BACKUP_TOOL_DEF, SELFTEST_TOOL_DEF,
        EDIT_TOOL_DEF, BATCH_TOOL_DEF, INSERT_TOOL_DEF, RESTORE_BACKUP_TOOL_DEF,
        CONNECTOR_BATCH_TOOL_DEF,
    )
}

LIST_PROJECTS_TOOL_NAME = "list_projects"
# This is the release-wire contract.  Keep it explicit instead of deriving the
# public catalog from whichever worker happens to be running: a legacy worker
# may not know about a newer tool, but clients must still see a stable catalog
# and receive a bounded failure when that tool cannot be served.
PUBLIC_TOOL_NAMES: tuple[str, ...] = (
    "search_knowledge", "get_document", "search_similar", "get_documents", "list_documents",
    "list_categories", "get_index_stats", "get_reindex_status", "evaluate_retrieval",
    "add_document", "update_document", "write_documents", "remove_document",
    "remove_documents",
    "move_document", "add_from_url", "reindex_documents", "find_literal",
    "copy_document", "copy_directory", "remove_directory", "put_asset",
    "update_asset_metadata", "search_assets", "list_assets", "get_asset_info",
    "get_asset", "reindex_assets", "ocr_asset", "remove_asset",
    "audiobook_inspect_chapter", "audiobook_prepare_chapter",
    "audiobook_get_chapter", "audiobook_find_chunk",
    "audiobook_record_generation", "audiobook_import_audio",
    "audiobook_build", "audiobook_commit_build",
    "audiobook_get_job", "audiobook_cancel_job", "book_get_index_status",
    "audiobook_get_generations", "audiobook_get_book",
    "set_folder_indexing", "list_project_files", "read_project_file",
    "read_document", "list_backups", "diff_backup",
    "get_self_test_plan", "edit_document", "edit_document_batch", "insert_in_document",
    "restore_backup", "batch", "list_projects",
    "workspace_info", "workspace_list_files", "workspace_stat", "workspace_read_file",
    "workspace_write_file", "workspace_edit_file", "workspace_make_directory",
    "workspace_copy_paths", "workspace_move_paths", "workspace_remove_paths",
    "workspace_search", "workspace_start_job", "workspace_get_job",
    "workspace_cancel_job", "workspace_web_search", "copy_to_workspace", "copy_from_workspace",
)
PUBLIC_TOOL_COUNT = len(PUBLIC_TOOL_NAMES)

# Workspace and bridge use dedicated runtime-backed dispatch services. Keeping
# their names grouped here makes the public connector catalog deterministic.
WORKSPACE_TOOL_NAMES: tuple[str, ...] = (
    "workspace_info", "workspace_list_files", "workspace_stat", "workspace_read_file",
    "workspace_write_file", "workspace_edit_file", "workspace_make_directory",
    "workspace_copy_paths", "workspace_move_paths", "workspace_remove_paths",
    "workspace_search", "workspace_start_job", "workspace_get_job",
    "workspace_cancel_job", "workspace_web_search",
)
BRIDGE_TOOL_NAMES: tuple[str, ...] = ("copy_to_workspace", "copy_from_workspace")

def _adapter_tool(
    name: str, description: str, properties: dict | None = None,
    required: tuple[str, ...] = (),
) -> dict:
    return {
        "name": name,
        "description": description,
        "inputSchema": {
            "type": "object", "properties": properties or {},
            "required": list(required), "additionalProperties": False,
        },
    }

_WS_PATH = {"type": "string", "maxLength": 4096}
_WS_BOOL = {"type": "boolean"}
_WS_INT = {"type": "integer", "minimum": 0}
_WS_FILES = {"type": "array", "items": {"type": "string", "maxLength": 4096}, "minItems": 1, "maxItems": 1000}
_WS_IDEMPOTENCY = {"type": "string", "minLength": 1, "maxLength": 128}
# A1/A2 (DESIGN-12.18 §10): run-and-wait plus text/base64 job output, shared
# by workspace_start_job and workspace_get_job. Copied verbatim from the
# design's appendix.
_WS_WAIT = {"type": "integer", "minimum": 0, "maximum": 55000}
# The default is base64 and must stay so: a call that names no encoding gets the 12.x response
# byte-for-byte (A2, DESIGN-12.18 §3.2).  It was undocumented, and claude.ai assumed "auto" and
# reported plain ASCII output "wrongly" returned as base64 (installer proof P5, 2026-09-29).
_WS_ENCODING = {
    "type": "string", "enum": ["auto", "text", "base64"],
    "description": "Omitted means base64. auto returns text when the output is valid UTF-8, else base64; "
                   "the result's stdout_encoding/stderr_encoding say which.",
}
_WS_WAIT_DESCRIPTION = (
    " wait_ms: block up to this long for the job to finish (max 55000). "
    "If you don't know your own tool-call timeout, pass 25000."
)
# A4 (DESIGN-12.18 SS3.4): deletion_due_at is a sliding deadline, not a fixed
# expiration -- it moves forward on every accepted call (a file operation, a
# job start, or this call itself). No behavior change; this only documents
# the existing slide so a caller does not read one observed value as a hard
# countdown.
_WS_DELETION_DUE_DESCRIPTION = (
    " deletion_due_at in the response advances on every accepted call; it is "
    "a sliding idle deadline, not a fixed expiration."
)
# For line-range reads, `has_more` means `max_bytes` cut the selected lines;
# `total_lines` reports the file's full line count. A selected range can end
# before the file does while `has_more` remains false.
_WS_LINE_HAS_MORE_DESCRIPTION = (
    " For start_line/end_line/tail_lines reads, has_more means max_bytes cut "
    "the selected lines; total_lines says how many lines the file has."
)
# Callers previously interpreted a successful null info result as an unavailable
# feature. Explain lazy creation in the shared catalog for both route families;
# this clarifies existing behavior without changing any wire schema.
_WS_FIRST_USE_DESCRIPTION = (
    " This inspection does not create a Workspace. A successful response with "
    "workspace: null means this authenticated identity has no Workspace yet; "
    "it does not mean Workspace is disabled or unavailable, and it does not "
    "check runtime readiness. When connector permissions and runtime availability "
    "allow it, the first Workspace file or job operation automatically creates "
    "the Workspace. To begin, call workspace_list_files with path '/workspace', "
    "then call workspace_info again. An empty file list is normal for a new "
    "Workspace. Report any actual permission or runtime error from that operation; "
    "do not infer unavailability from workspace: null alone."
)
WORKSPACE_TOOL_DEFS = (
    _adapter_tool(WORKSPACE_TOOL_NAMES[0], "Inspect the authenticated Workspace." + _WS_FIRST_USE_DESCRIPTION + _WS_DELETION_DUE_DESCRIPTION, {}, ()),
    _adapter_tool(WORKSPACE_TOOL_NAMES[1], "List bounded Workspace file metadata. Use path '/workspace' to begin: this operation automatically creates your Workspace on first use when permitted and the runtime is available. An empty file list is a successful result, not an unavailable Workspace.", {"path": _WS_PATH, "recursive": _WS_BOOL, "max_entries": {"type": "integer", "minimum": 1, "maximum": 2000}}, ("path",)),
    _adapter_tool(WORKSPACE_TOOL_NAMES[2], "Inspect one Workspace path.", {"path": _WS_PATH, "include_hash": _WS_BOOL}, ("path",)),
    _adapter_tool(WORKSPACE_TOOL_NAMES[3], "Read bounded Workspace file content." + _WS_LINE_HAS_MORE_DESCRIPTION, {"path": _WS_PATH, "offset": _WS_INT, "max_bytes": {"type": "integer", "minimum": 1, "maximum": 1048576}, "encoding": {"type": "string", "enum": ["text", "base64"]}, "start_line": {"type": "integer", "minimum": 1}, "end_line": {"type": "integer", "minimum": 1}, "tail_lines": {"type": "integer", "minimum": 1, "maximum": 10000}}, ("path",)),
    _adapter_tool(WORKSPACE_TOOL_NAMES[4], "Atomically write bounded Workspace file content.", {"path": _WS_PATH, "text": {"type": "string", "maxLength": 1048576}, "base64": {"type": "string", "maxLength": 1398104}, "create_policy": {"type": "string", "enum": ["parents", "existing", "fail"]}, "expected_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"}, "idempotency_key": _WS_IDEMPOTENCY}, ("path",)),
    _adapter_tool(WORKSPACE_TOOL_NAMES[5], "Apply exact-match edits atomically.", {"path": _WS_PATH, "edits": {"type": "array", "items": {"type": "object", "properties": {"match": {"type": "string", "maxLength": 1048576}, "replacement": {"type": "string", "maxLength": 1048576}}, "required": ["match", "replacement"], "additionalProperties": False}, "minItems": 1, "maxItems": 256}, "expected_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"}, "idempotency_key": _WS_IDEMPOTENCY}, ("path", "edits")),
    _adapter_tool(WORKSPACE_TOOL_NAMES[6], "Create a Workspace directory.", {"path": _WS_PATH, "parents": _WS_BOOL, "idempotency_key": _WS_IDEMPOTENCY}, ("path",)),
    _adapter_tool(WORKSPACE_TOOL_NAMES[7], "Copy paths within the Workspace.", {"sources": _WS_FILES, "destination": _WS_PATH, "conflict_policy": {"type": "string", "enum": ["fail", "skip", "replace", "rename"]}, "idempotency_key": _WS_IDEMPOTENCY}, ("sources", "destination")),
    _adapter_tool(WORKSPACE_TOOL_NAMES[8], "Move paths within the Workspace.", {"sources": _WS_FILES, "destination": _WS_PATH, "conflict_policy": {"type": "string", "enum": ["fail", "skip", "replace", "rename"]}, "idempotency_key": _WS_IDEMPOTENCY}, ("sources", "destination")),
    _adapter_tool(WORKSPACE_TOOL_NAMES[9], "Remove exact Workspace paths.", {"paths": _WS_FILES, "recursive": _WS_BOOL, "expected_hashes": {"type": "object", "additionalProperties": {"type": "string", "pattern": "^[0-9a-f]{64}$"}}, "idempotency_key": _WS_IDEMPOTENCY}, ("paths",)),
    _adapter_tool(WORKSPACE_TOOL_NAMES[10], "Search bounded Workspace paths.", {"roots": _WS_FILES, "pattern": {"type": "string", "maxLength": 4096}, "mode": {"type": "string", "enum": ["glob", "text", "regex"]}, "max_paths": {"type": "integer", "minimum": 1, "maximum": 2000}, "max_matches": {"type": "integer", "minimum": 1, "maximum": 10000}}, ("roots", "pattern")),
    _adapter_tool(WORKSPACE_TOOL_NAMES[11], "Start an asynchronous Workspace job." + _WS_WAIT_DESCRIPTION, {"argv": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 256}, "shell_script": {"type": "string"}, "cwd": _WS_PATH, "timeout": {"type": "integer", "minimum": 1, "maximum": 3600}, "env": {"type": "object", "maxProperties": 128, "additionalProperties": {"type": "string"}}, "idempotency_key": _WS_IDEMPOTENCY, "wait_ms": _WS_WAIT, "output_encoding": _WS_ENCODING, "strip_ansi": _WS_BOOL}, ()),
    _adapter_tool(WORKSPACE_TOOL_NAMES[12], "Read bounded asynchronous job state/output." + _WS_WAIT_DESCRIPTION, {"job_id": {"type": "string"}, "stdout_offset": _WS_INT, "stderr_offset": _WS_INT, "max_bytes": {"type": "integer", "minimum": 1, "maximum": 1048576}, "wait_ms": _WS_WAIT, "output_encoding": _WS_ENCODING, "strip_ansi": _WS_BOOL, "tail_lines": {"type": "integer", "minimum": 1, "maximum": 10000}}, ("job_id",)),
    _adapter_tool(WORKSPACE_TOOL_NAMES[13], "Cancel an asynchronous Workspace job.", {"job_id": {"type": "string"}, "idempotency_key": _WS_IDEMPOTENCY}, ("job_id",)),
    _adapter_tool(WORKSPACE_TOOL_NAMES[14], "Search the web through configured Brave metadata only.", {"query": {"type": "string", "maxLength": 4096}, "result_count": {"type": "integer", "minimum": 1, "maximum": 20}}, ("query",)),
)
# 13.2.8 (DESIGN-13.2 §9): the placement rule, stated where the model reads it.
# A selected FILE lands at <destination>/<its basename>; a selected DIRECTORY
# lands at <destination>/<its name>/ with its whole subtree. Selecting the
# files of a tree one by one therefore FLATTENS them into <destination>/ —
# which is what bridge._knowledge_manifest and _workspace_manifest have always
# done. On 2026-09-23, a client read this flattening as a bug because the
# description said only "copy selected files"; the placement rule now states
# what the command does.
# Description text and per-argument descriptions are catalog metadata; names,
# argument shapes and results are unchanged (D4.4).
_BRIDGE_PLACEMENT = (
    " Placement: a selected FILE lands at <destination>/<basename>; a selected "
    "DIRECTORY lands at <destination>/<its name>/ with its whole subtree. Selecting "
    "files one by one flattens them into <destination>/ (paths [\"sections/a.md\", "
    "\"sections/b.md\"] with destination \"build\" give build/a.md and build/b.md); "
    "to keep a tree's layout, select the directory (paths [\"sections\"] gives "
    "build/sections/a.md and build/sections/b.md)."
)
# The result sentence is per tool because the two manifests differ (final review, 2026-09-29):
# copy_from_workspace lists the Workspace SOURCE paths (bridge._workspace_manifest), while
# copy_to_workspace lists the Workspace DESTINATION paths, the same as committed
# (bridge._knowledge_manifest).
_BRIDGE_RESULT = {
    "copy_to_workspace": " In the result, manifest and committed both list the Workspace paths "
                         "the files were written to.",
    "copy_from_workspace": " In the result, manifest lists the Workspace SOURCE files as read "
                           "(path, size, sha256); committed lists where each one landed in the project.",
}
# The result sentence (installer proof P5, 2026-09-29): claude.ai read a from_workspace manifest
# path (the Workspace source, proof-test/x.txt) as the destination and reported the file "at the
# wrong path" although committed showed x.txt at the project root, which is correct.
_BRIDGE_PATHS_DESCRIPTION = (
    "Relative paths to copy, 1-10000. Each FILE is placed at "
    "<destination>/<basename> (its own directory is NOT kept); each DIRECTORY "
    "is placed at <destination>/<its name>/ with its subtree. Select a "
    "directory, not its files, to preserve layout."
)
_BRIDGE_DESTINATION_DESCRIPTION = (
    "Relative directory the selection is placed under; \".\" or omitted means the "
    "root. Created as needed."
)
_BRIDGE_PROPERTIES = {
    "project": {"type": "string", "minLength": 1}, "paths": _WS_FILES,
    "destination": {**_WS_PATH, "description": _BRIDGE_DESTINATION_DESCRIPTION},
    "conflict_policy": {"type": "string", "enum": ["fail", "skip", "replace", "rename"]},
    "expected_destination_hashes": {"type": "object", "additionalProperties": {"type": "string", "pattern": "^[0-9a-f]{64}$"}},
    "idempotency_key": _WS_IDEMPOTENCY,
}
_BRIDGE_FILES = {
    "type": "array", "items": {"type": "string", "maxLength": 4096},
    "minItems": 1, "maxItems": 10000, "description": _BRIDGE_PATHS_DESCRIPTION,
}
_BRIDGE_PROPERTIES["paths"] = _BRIDGE_FILES
BRIDGE_TOOL_DEFS = tuple(
    _adapter_tool(
        name,
        (
            "Copy selected Knowledge project files or directories into the authenticated Workspace."
            if name == "copy_to_workspace" else
            "Copy selected authenticated Workspace files or directories into the Knowledge project."
        ) + _BRIDGE_PLACEMENT + _BRIDGE_RESULT[name],
        _BRIDGE_PROPERTIES,
        ("project", "paths"),
    )
    for name in BRIDGE_TOOL_NAMES
)

_WORKSPACE_SCRATCH_WARNING_COMBINED = (
    "Workspace is a reusable scratch workbed for this credential across chats. "
    "It may be stopped or cleaned up for age or space, so availability is not "
    "guaranteed forever. Never keep the only copy of important data here. Save "
    "important inputs and outputs to durable project files; use the authorized "
    "copy_from_workspace tool to publish selected outputs."
)
_WORKSPACE_SCRATCH_WARNING_ONLY = (
    "Workspace is a reusable scratch workbed for this credential across chats. "
    "It may be stopped or cleaned up for age or space, so availability is not "
    "guaranteed forever. Never keep the only copy of important data here. Save "
    "important inputs and outputs to another authorized durable destination."
)


def _workspace_catalog_definitions(
    definitions: tuple[dict, ...] | list[dict], *, combined: bool,
) -> list[dict]:
    """Add the surface-specific scratch-workbed guidance to Workspace tools.

    The warning is catalog metadata only.  Keep the underlying definitions and
    schemas unchanged so wording updates do not become MCP contract changes.
    """
    warning = _WORKSPACE_SCRATCH_WARNING_COMBINED if combined else _WORKSPACE_SCRATCH_WARNING_ONLY
    catalog: list[dict] = []
    for definition in definitions:
        item = copy.deepcopy(definition)
        item["description"] = f"{warning} {item.get('description', '')}".rstrip()
        catalog.append(item)
    return catalog
LIST_PROJECTS_TOOL_DEF: dict = {
    "name": LIST_PROJECTS_TOOL_NAME,
    "description": (
        "List the enabled projects this connector can access, their effective access "
        "mode, and the current connector-policy revision. Use the exact returned project "
        "name as the required project argument on every other tool call."
    ),
    "inputSchema": {"type": "object", "properties": {}, "required": []},
}

_PROJECT_PROPERTY = {
    "type": "string",
    "description": (
        "Required exact project name from list_projects. This call affects only that "
        "project; names are case-sensitive and path syntax is not accepted."
    ),
}


def with_project_argument(tool_def: dict) -> dict:
    """Return the stable public schema for one project-scoped tool."""
    out = copy.deepcopy(tool_def)
    if out.get("name") == CONNECTOR_BATCH_TOOL_NAME:
        return out
    schema = out.setdefault("inputSchema", {})
    properties = schema.setdefault("properties", {})
    properties["project"] = copy.deepcopy(_PROJECT_PROPERTY)
    required = list(schema.get("required") or [])
    if "project" not in required:
        required.insert(0, "project")
    schema["required"] = required
    return out


def _current_public_tool_catalog() -> list[dict]:
    """Build the connector-wide current catalog without consulting a project.

    The engine owns the asset and document definitions. Importing them lazily keeps the
    proxy importable without the engine while ensuring tools/list is
    stable and does not need an arbitrary project just to discover schemas.
    (Before 14.0.0 this also kept the proxy usable with the worker rollback mode.)
    """
    from .engine_local import ENGINE_TOOL_DEFS

    catalog = [
        with_operation_id_argument(with_project_argument(attach_output_schema(tool)))
        if tool.get("name") in MUTATING_TOOLS else with_project_argument(attach_output_schema(tool))
        for tool in ENGINE_TOOL_DEFS
    ]
    for tool in GATEWAY_TOOL_DEFS.values():
        public = with_project_argument(attach_output_schema(tool))
        if tool["name"] in MUTATING_TOOLS:
            public = with_operation_id_argument(public)
        catalog.append(public)
    catalog.append(attach_output_schema(LIST_PROJECTS_TOOL_DEF))
    catalog.extend(_workspace_catalog_definitions(
        [attach_output_schema(item) for item in WORKSPACE_TOOL_DEFS], combined=True,
    ))
    catalog.extend(_workspace_catalog_definitions(
        [attach_output_schema(item) for item in BRIDGE_TOOL_DEFS], combined=True,
    ))
    names = tuple(tool.get("name") for tool in catalog)
    if names != PUBLIC_TOOL_NAMES:
        raise RuntimeError(
            "Cognita public MCP contract drift: "
            f"expected {PUBLIC_TOOL_COUNT} tools, got {len(names)}"
        )
    return catalog


def public_tool_catalog(contract_version: int = PUBLIC_CONTRACT_VERSION) -> list[dict]:
    """Return the sole current combined public catalog.

    Retired generations are not retained as compatibility catalogs.  A caller
    holding a retired URL must reconnect to the explicitly qualified current
    route instead of receiving a silently downgraded schema.
    """
    if contract_version != PUBLIC_CONTRACT_VERSION:
        raise ValueError(f"unsupported public contract version: {contract_version!r}")
    return _current_public_tool_catalog()


def public_tool_names_for_contract(
    contract_version: int = PUBLIC_CONTRACT_VERSION,
) -> tuple[str, ...]:
    """Return the exact ordered tool names advertised for one generation."""
    if contract_version == PUBLIC_CONTRACT_VERSION:
        return PUBLIC_TOOL_NAMES
    raise ValueError(f"unsupported public contract version: {contract_version!r}")


def workspace_tool_catalog(contract_version: int = WORKSPACE_CONTRACT_VERSION) -> list[dict]:
    """Return the exact current Workspace-only catalog."""
    if contract_version != WORKSPACE_CONTRACT_VERSION:
        raise ValueError(f"unsupported Workspace contract version: {contract_version!r}")
    return _workspace_catalog_definitions(
        [*(attach_output_schema(item) for item in WORKSPACE_TOOL_DEFS),
         attach_output_schema(workspace_selftest_tool_definition())], combined=False,
    )


def workspace_tool_names(contract_version: int = WORKSPACE_CONTRACT_VERSION) -> tuple[str, ...]:
    if contract_version != WORKSPACE_CONTRACT_VERSION:
        raise ValueError(f"unsupported Workspace contract version: {contract_version!r}")
    return (*WORKSPACE_TOOL_NAMES, WORKSPACE_SELFTEST_TOOL_NAME)


def public_tool_available(
    tool_name: str,
    contract_version: int = PUBLIC_CONTRACT_VERSION,
) -> bool:
    """Whether a tool name is callable under the requested generation."""
    return contract_version == PUBLIC_CONTRACT_VERSION and tool_name in PUBLIC_TOOL_NAMES


def public_tools_response(
    msg_id,
    contract_version: int = PUBLIC_CONTRACT_VERSION,
) -> JSONResponse:
    """Answer connector-scoped tools/list from the selected public catalog."""
    tools = public_tool_catalog(contract_version)
    fingerprint = hashlib.sha256(
        json.dumps(tools, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:16]
    log.info(
        "MCP tools/list connector-scoped contract=v%d count=%d fingerprint=%s",
        contract_version, len(tools), fingerprint,
    )
    return JSONResponse({"jsonrpc": "2.0", "id": msg_id,
                         "result": {"tools": tools}})


def _tool_error(msg_id, reason: str, message: str, **fields) -> JSONResponse:
    """Shorthand for the status:error tool-result shape used everywhere."""
    return _tool_result(msg_id, {"status": "error", "reason": reason,
                                 "message": message, **fields})


def upgrade_required_response(msg_id, tool_name: str, contract_version: int) -> JSONResponse:
    """Return the structured tool-level refusal for an unavailable old call."""
    return _tool_result(msg_id, upgrade_required_payload(tool_name, contract_version))


def _params_of(message: dict) -> dict:
    """params as a dict, tolerating malformed JSON-RPC (a list/str params from
    an authenticated client must produce a clean error, not an AttributeError
    500 out of the gateway)."""
    params = message.get("params")
    return params if isinstance(params, dict) else {}


def _arguments_of(message: dict) -> dict:
    args = _params_of(message).get("arguments")
    return args if isinstance(args, dict) else {}


def _strip_operation_id(message: dict) -> None:
    """Remove operation_id from a message before it is forwarded or handled.

    The gateway consumes this argument entirely. Leaving it in place would reach
    the ENGINE's strict-argument gate, whose schemas do not declare it, and be
    refused — turning the retry guard into a call that always fails.
    """
    args = _params_of(message).get("arguments")
    if isinstance(args, dict):
        args.pop(OPERATION_ID_ARG, None)


def _remember_operation(connector: str, project: str, tool: str, operation_id: str | None,
                        response: Response, msg_id, request_digest: str | None = None) -> Response:
    """Store this call's result under `operation_id` so a retry can replay it.

    Errors are stored too: a retry of a call that genuinely failed must not
    silently re-attempt a write the caller believes did not happen.
    """
    if operation_id is None:
        return response
    payload = _payload_from_response(response, msg_id)
    result = (payload or {}).get("result")
    if isinstance(result, dict):
        stored = result.get("structuredContent")
        blocks = result.get("content")
        if not isinstance(stored, dict) and isinstance(blocks, list) and blocks and isinstance(blocks[0], dict):
            try:
                stored = json.loads(blocks[0].get("text") or "")
            except (json.JSONDecodeError, TypeError):
                return response  # not a tool payload we can replay; store nothing
        if isinstance(stored, dict):
            _operations.remember(connector, project, tool, operation_id, stored,
                                 request_digest=request_digest)
    return response


def _resolve_doc(msg_id, documents_dir, filepath: str):
    """Shared prologue: (target_path, None) or (None, rejection Response)."""
    if not filepath:
        return None, _tool_error(msg_id, "invalid", "filepath is required.")
    target = resolve_target(documents_dir, filepath)
    if target is None:
        return None, _tool_error(
            msg_id, "invalid_path", f"filepath resolves outside this project: {filepath!r}"
        )
    return target, None


def _find_backup_or_hint(msg_id, documents_dir, filepath: str, backup_id: str):
    """Shared by restore/diff: (backup_path, None) or (None, not_found Response
    listing the available backup_ids)."""
    backup = find_backup(documents_dir, filepath, backup_id)
    if backup is not None:
        return backup, None
    available = [e["backup_id"] for e in list_backup_entries(documents_dir, filepath)]
    return None, _tool_error(
        msg_id, "not_found", f"No backup {backup_id!r} for {filepath!r}.",
        hint=(f"Available backup_ids (newest first): {', '.join(available[:10])}"
              if available else "This document has no backups yet."),
    )


async def _send_buffered(client: httpx.AsyncClient, request: Request,
                         worker_url: str, message: dict) -> httpx.Response | Response:
    """Forward one synthesized JSON-RPC message to the worker, buffered.

    Returns the upstream httpx.Response, or a clean 503 Response if the worker
    died between the supervisor health check and the forward (httpx errors
    used to escape as raw 500s)."""
    try:
        return await client.send(client.build_request(
            "POST",
            worker_url,
            headers=_forward_headers(request),
            content=json.dumps(message).encode(),
        ))
    except httpx.HTTPError as exc:
        log.warning("worker connection failed mid-request: %s", exc)
        return Response(status_code=503, content="Worker connection failed; try again shortly",
                        headers={"Retry-After": "5"})


# Per-file locks: an edit holds its file's lock from disk-read until the worker
# write completes, so two concurrent edits to one file cannot splice from the
# same stale base and silently drop each other's change (lost update). Keyed by
# resolved path; never pruned — bounded by distinct files edited per process.
_EDIT_LOCKS: dict[str, asyncio.Lock] = {}


def _edit_lock(key: str) -> asyncio.Lock:
    lock = _EDIT_LOCKS.get(key)
    if lock is None:
        lock = _EDIT_LOCKS[key] = asyncio.Lock()
    return lock


@asynccontextmanager
async def _edit_locks(keys: tuple[str, ...]):
    """Acquire all file locks in canonical order for multi-path writes."""
    async with AsyncExitStack() as stack:
        for key in sorted(set(keys)):
            await stack.enter_async_context(_edit_lock(key))
        yield


# Per-worker WRITE lock: serialize EVERY mutating tool call to one project's
# worker. The engine's single-doc write paths (add/update/remove_document) take
# NO lock (verified in mcp_server/server.py), so concurrent writes drive
# concurrent SQLite writes into the ChromaDB store — the SHORT_READ / disk-I/O
# corruption that repeatedly reset the collection and re-fired the engine's
# embedding-function recovery ("index churn"). Cognita is the gateway in front of
# every worker, so it is the single-writer gate. Coarser than _edit_lock (which
# only guards same-file lost updates); keyed by project so writes to *different*
# files in one project also serialize. Never pruned — bounded by project count.
_WORKER_WRITE_LOCKS: dict[str, asyncio.Lock] = {}


def _worker_write_lock(key: str) -> asyncio.Lock:
    lock = _WORKER_WRITE_LOCKS.get(key)
    if lock is None:
        lock = _WORKER_WRITE_LOCKS[key] = asyncio.Lock()
    return lock


def _load_document_bytes(target: Path) -> tuple[bytes | None, dict | None]:
    """The raw file, with the readability and size guards. BOM NOT stripped.

    Split out of _load_document_text (5.0.1) so read_document can report facts
    about the FILE — its byte hash, its size, its line-ending style — without
    reading it a second time.
    """
    try:
        raw = target.read_bytes()
    except OSError as exc:
        # deleted between check and read, exclusively locked, or an
        # unhydrated OneDrive placeholder — a clean error, not a 500
        return None, {
            "status": "error", "reason": "unreadable",
            # str(OSError) renders "[Errno 2] ...: 'C:\private\...\notes.md'" — the
            # server's directory layout and username, in the shared error payload
            # for every gateway read/edit/diff/restore path, shown to the user
            # verbatim and kept in connector transcripts.
            "message": f"Could not read the file: {wire_error(exc)}",
        }
    if len(raw) > MAX_FILE_BYTES:
        return None, {
            "status": "error", "reason": "too_large",
            "message": f"File exceeds the {MAX_FILE_BYTES} byte limit.",
        }
    return raw, None


_BOM = "﻿"


def _strip_bom(text: str) -> str:
    return text[1:] if text.startswith(_BOM) else text


def _decode_verbatim(raw: bytes) -> tuple[str | None, dict | None]:
    """UTF-8 decode that PRESERVES the BOM and the file's line endings.

    `_decode_document` below is the right thing for anchors and comparisons —
    everything the edit path matches against is normalized. It is the wrong
    thing for a path that writes its result straight back to disk, because since
    5.0 the engine persists exactly the bytes it is handed. Restore uses this so
    an undo reproduces the backup byte for byte.
    """
    try:
        return raw.decode("utf-8"), None
    except UnicodeDecodeError:
        return None, {
            "status": "error", "reason": "not_text",
            "message": "File is not valid UTF-8 text; only text documents are supported.",
        }


def _decode_document(raw: bytes) -> tuple[str | None, dict | None]:
    """BOM strip + UTF-8 decode — the ONE way every gateway tool sees text."""
    if raw.startswith(b"\xef\xbb\xbf"):
        raw = raw[3:]  # drop UTF-8 BOM (documented: not preserved through edits)
    try:
        return raw.decode("utf-8"), None
    except UnicodeDecodeError:
        return None, {
            "status": "error", "reason": "not_text",
            "message": "File is not valid UTF-8 text; only text documents are supported.",
        }


def _load_document_text(target: Path) -> tuple[str | None, dict | None]:
    """Read + decode a document the ONE way every gateway tool sees it.

    Shared by the edit and read paths so anchors built from read_document are
    guaranteed to match what edit_document compares against. Returns
    (text, None) or (None, error_payload)."""
    raw, err = _load_document_bytes(target)
    if err is not None:
        return None, err
    return _decode_document(raw)


def _stale_check(msg_id, args: dict, text: str) -> Response | None:
    """Enforce the optional expected_sha256 staleness guard (2.7).

    Returns a rejection Response, or None when the write may proceed."""
    expected = args.get("expected_sha256")
    if expected in (None, ""):
        return None
    expected = str(expected).strip().lower()
    if len(expected) < SHA_PREFIX_MIN:
        return _tool_result(msg_id, {
            "status": "error", "reason": "invalid",
            "message": f"expected_sha256 must be at least {SHA_PREFIX_MIN} hex characters.",
        })
    actual = content_sha256(text)
    if not sha_matches(expected, actual):
        return _tool_result(msg_id, {
            "status": "error", "reason": "stale_file",
            "message": "The file changed since you read it; nothing was written.",
            "expected_sha256": expected,
            "actual_sha256": actual,
            "hint": "Re-read with read_document and rebuild the edit against the current content.",
        })
    return None


_EXTRACTED_DOCUMENT_SUFFIXES = frozenset({".pdf", ".docx", ".xlsx", ".pptx"})


def _validate_write_content(target: Path, args: dict) -> dict | None:
    """Validate encoded bytes before a gateway backup can be created.

    The engine remains authoritative and repeats these checks for local/stdio
    callers.  The gateway copy is required because its backup boundary is
    intentionally outside the engine: a refused remote write must not create an
    undo point for a mutation that never happened.
    """
    encoding = args.get("content_encoding", "utf-8")
    content = args.get("content")
    if encoding not in ("utf-8", "base64"):
        return {"status": "error", "reason": "invalid",
                "message": "content_encoding must be 'utf-8' or 'base64'."}
    if not isinstance(content, str):
        return {"status": "error", "reason": "invalid",
                "message": "content must be a string."}
    try:
        raw = decode_base64(content) if encoding == "base64" else content.encode("utf-8")
    except ValueError as exc:
        return {"status": "error", "reason": "invalid", "message": str(exc)}
    if target.suffix.lower() not in _EXTRACTED_DOCUMENT_SUFFIXES:
        view = classify_text_bytes(raw)
        if not view.accepted:
            return {"status": "error", "reason": view.reason or "binary_content",
                    "message": view.message or "Content is not accepted text."}
    return None


async def _handle_batch_write(
    client,
    request,
    worker_url: str,
    message: dict,
    documents_dir,
    backup_keep: int,
) -> Response:
    """🔴 6.1.0: `write_documents` — N files, so N locks and N backups.

    The single-file twin below is built around `arguments.filepath`. A batch has
    a `documents` array instead, and handing it to that function would take the
    "no filepath" branch: forwarded with NO backup and NO edit lock, silently.
    So the batch gets its own path, and it holds the gateway to the same two
    promises for every document in the call.

    🔴 **LOCKS ARE TAKEN IN SORTED ORDER.** Two concurrent batches sharing two
    files, each grabbing them in its own argument order, is a textbook deadlock
    — and it would hang the write lock for the whole project, not just the two
    callers. A canonical order makes it impossible rather than unlikely.

    🔴 **A FAILED BACKUP ABORTS THE WHOLE CALL, BEFORE ANYTHING IS WRITTEN**,
    which is the standing invariant (`backups.py`) applied to a set. Backing up
    three of four and then writing all four would leave one document with no
    undo point, and the caller with no way to know WHICH.
    """
    msg_id = message.get("id")
    documents = _arguments_of(message).get("documents")
    if not isinstance(documents, list) or not documents:
        # Malformed: let the engine produce its own validation error rather
        # than inventing a second wording for the same condition here.
        upstream = await _send_buffered(client, request, worker_url, message)
        if isinstance(upstream, Response):
            return upstream
        return Response(upstream.content, status_code=upstream.status_code,
                        headers=_response_headers(upstream),
                        media_type=upstream.headers.get("content-type"))

    allowed = {"filepath", "content", "category", "expected_sha256",
               "expected_bytes_sha256", "content_encoding"}
    prepared: list[tuple[str, Path, dict]] = []
    seen: set[Path] = set()
    base64_bytes = 0
    for index, entry in enumerate(documents):
        if not isinstance(entry, dict):
            return _tool_error(msg_id, "invalid",
                               f"documents[{index}] must be an object. NOTHING was written.")
        unknown = sorted(set(entry) - allowed)
        if unknown:
            return _tool_error(msg_id, "unknown_argument",
                               f"documents[{index}] has unknown key(s): {unknown}. NOTHING was written.")
        fp = (entry.get("filepath") or "").strip()
        if not fp:
            return _tool_error(msg_id, "invalid",
                               f"documents[{index}].filepath must be non-empty. NOTHING was written.")
        target = resolve_target(documents_dir, fp)
        if target is None:
            return _tool_error(
                msg_id, "invalid_path",
                f"Write refused: {fp!r} resolves outside this project. "
                f"NOTHING was written — write_documents is all-or-nothing.",
                filepath=fp,
            )
        canonical = target.resolve()
        if canonical in seen:
            return _tool_error(msg_id, "invalid",
                               f"{fp!r} appears more than once in the batch. NOTHING was written.",
                               filepath=fp)
        seen.add(canonical)
        invalid_content = _validate_write_content(target, entry)
        if invalid_content is not None:
            return _tool_result(msg_id, {**invalid_content, "filepath": fp,
                                         "documents_written": 0})
        if entry.get("content_encoding", "utf-8") == "base64":
            # Validation above proved canonical base64, so this arithmetic is
            # the exact decoded size without retaining another decoded copy.
            encoded = entry["content"]
            base64_bytes += (len(encoded) // 4) * 3 - (len(encoded) - len(encoded.rstrip("=")))
            if base64_bytes > MAX_BASE64_ATOMIC_SET_BYTES:
                return _tool_error(
                    msg_id, "too_large",
                    "Decoded base64 content exceeds the 32 MiB atomic-set limit. "
                    "NOTHING was written — write_documents is all-or-nothing.",
                    limit_bytes=MAX_BASE64_ATOMIC_SET_BYTES, documents_written=0,
                )
        prepared.append((fp, target, entry))

    # Sorted by resolved path, and de-duplicated: asyncio.Lock is not reentrant,
    # so a batch naming one file twice would deadlock against itself. The engine
    # refuses duplicates too; this must not depend on that.
    ordered = sorted({str(target): target for _fp, target, _entry in prepared}.items())

    async with AsyncExitStack() as stack:
        for key, _target in ordered:
            await stack.enter_async_context(_edit_lock(key))

        # Guards belong inside the same file locks as backup/publication, but
        # still before backups.  This preserves the existing gateway ordering
        # while making per-member exact-byte guards meaningful for the set.
        for fp, target, entry in prepared:
            expected_bytes = entry.get("expected_bytes_sha256")
            try:
                expected_bytes = validate_expected_bytes_sha256(expected_bytes)
            except ValueError as exc:
                byte_guard = {"status": "error", "reason": "invalid",
                              "message": str(exc), "guard": "expected_bytes_sha256"}
            else:
                actual_bytes = None
                if expected_bytes is not None and target.is_file():
                    with target.open("rb") as handle:
                        actual_bytes = hashlib.file_digest(handle, "sha256").hexdigest()
                byte_guard = check_expected_bytes_sha256_digest(actual_bytes, expected_bytes)
            if byte_guard is not None:
                return _tool_result(msg_id, {**byte_guard, "filepath": fp,
                                             "documents_written": 0})
            if entry.get("expected_sha256") not in (None, ""):
                if target.is_file() and target.stat().st_size > TEXT_HASH_MAX_BYTES:
                    return _tool_result(msg_id, {
                        "status": "error", "reason": "too_large", "filepath": fp,
                        "documents_written": 0, "limit_bytes": TEXT_HASH_MAX_BYTES,
                        "message": "The current file is too large for expected_sha256; NOTHING was written.",
                    })
                current_raw = target.read_bytes() if target.is_file() else None
                if current_raw is None:
                    return _tool_result(msg_id, {
                        "status": "error", "reason": "stale_file", "filepath": fp,
                        "expected_sha256": str(entry["expected_sha256"]).strip().lower(),
                        "actual_sha256": None, "documents_written": 0,
                        "message": "The expected file is absent. NOTHING was written.",
                    })
                view = classify_text_bytes(current_raw)
                if not view.accepted or not view.utf8_valid:
                    return _tool_result(msg_id, {
                        "status": "error", "reason": "not_text", "filepath": fp,
                        "documents_written": 0,
                        "message": "The current file is not valid UTF-8 text, so expected_sha256 cannot be checked. NOTHING was written.",
                    })
                expected = str(entry["expected_sha256"]).strip().lower()
                if len(expected) < SHA_PREFIX_MIN:
                    return _tool_result(msg_id, {
                        "status": "error", "reason": "invalid", "filepath": fp,
                        "documents_written": 0,
                        "message": f"expected_sha256 must be at least {SHA_PREFIX_MIN} hex characters.",
                    })
                actual = content_sha256(_strip_bom(view.text))
                if not sha_matches(expected, actual):
                    return _tool_result(msg_id, {
                        "status": "error", "reason": "stale_file", "filepath": fp,
                        "expected_sha256": expected, "actual_sha256": actual,
                        "documents_written": 0,
                        "message": "The file changed since you read it; NOTHING was written.",
                    })

        augment: dict = {}
        backups: dict[str, str] = {}
        try:
            for fp, _target, _entry in prepared:
                made = backup_if_exists(documents_dir, fp, keep=backup_keep)
                if made is not None and (bid := backup_id_of(made)) is not None:
                    backups[fp] = bid
        except BackupError as exc:
            log.warning("Batch write aborted — backup failed: %s", exc)
            return _tool_error(
                msg_id, "backup_failed",
                f"Write aborted: {wire_error(exc)}. No changes were made.")
        if backups:
            augment["previous_backup_ids"] = backups

        upstream = await _send_buffered(client, request, worker_url, message)
        if isinstance(upstream, Response):
            return upstream
        if augment:
            return _rewrite_buffered_response(
                upstream, _augment_result_transform(augment, when_success=True,
                                                    tool_name=BATCH_WRITE_TOOL_NAME))
        return Response(upstream.content, status_code=upstream.status_code,
                        headers=_response_headers(upstream),
                        media_type=upstream.headers.get("content-type"))


async def _handle_engine_write(
    client: httpx.AsyncClient,
    request: Request,
    worker_url: str,
    message: dict,
    documents_dir,
    backup_keep: int,
) -> Response:
    """Direct engine writes (update/add/move_document) — locked + backed up.

    These used to be backed up and then streamed through WITHOUT the per-file
    edit lock, so a direct update_document racing an edit_document could land
    between the edit's disk read and its write: the edit's full-content
    transform then silently reverted it (lost update, no error). Now every
    write to a file — gateway tool or engine tool — serializes on the same
    lock. Buffered (small JSON result) because with a streamed forward the
    response headers can arrive before the tool has actually run, which would
    release the lock too early."""
    msg_id = message.get("id")
    filepath = _arguments_of(message).get("filepath")
    if not filepath or not isinstance(filepath, str):
        # Batch writes have their own lock and backup handler. A single-file
        # write without a filepath cannot use this path; forward it for engine
        # validation without claiming a file lock or backup.
        upstream = await _send_buffered(client, request, worker_url, message)
        if isinstance(upstream, Response):
            return upstream
        return Response(upstream.content, status_code=upstream.status_code,
                        headers=_response_headers(upstream),
                        media_type=upstream.headers.get("content-type"))

    target = resolve_target(documents_dir, filepath)
    if target is None:
        # A tool-level refusal, not a malformed request: the engine returns
        # reason "invalid_path" for exactly this condition, and a client
        # branching on `reason` (which §11.1 tells it to do) saw a bare
        # JSON-RPC error here instead.
        return _tool_error(
            msg_id, "invalid_path",
            f"Write refused: {filepath!r} resolves outside this project.",
            filepath=filepath,
        )
    tool = _params_of(message).get("name")
    move_destination = None
    if tool == "move_document":
        new_filepath = _arguments_of(message).get("new_filepath")
        if not isinstance(new_filepath, str) or not new_filepath:
            return _tool_error(msg_id, "invalid", "filepath and new_filepath are required")
        move_destination = resolve_target(documents_dir, new_filepath)
        if move_destination is None:
            return _tool_error(
                msg_id, "invalid_path",
                f"new_filepath resolves outside this project: {new_filepath!r}",
            )
    lock_keys = (str(target),) + ((str(move_destination),) if move_destination is not None else ())
    async with _edit_locks(lock_keys):
        if tool == "move_document":
            # These are gateway-owned refusals so a failed move cannot create a
            # source backup.  Both paths are locked in canonical order above,
            # preserving race safety for callers that invoke this seam directly.
            if move_destination == target:
                return _tool_error(
                    msg_id, "same_path",
                    "filepath and new_filepath are the same",
                )
            if not target.is_file():
                return _tool_error(
                    msg_id, "not_found",
                    f"Document not found: {filepath}",
                )
            if move_destination.exists():
                return _tool_error(
                    msg_id, "destination_exists",
                    f"destination already exists: {_arguments_of(message).get('new_filepath')}",
                )
        if tool in ("add_document", "update_document"):
            invalid_content = _validate_write_content(target, _arguments_of(message))
            if invalid_content is not None:
                return _tool_result(msg_id, {**invalid_content, "filepath": filepath})
        # 5.0 §8: optimistic-concurrency guard on the two tools that rewrite a
        # WHOLE file. edit_document and edit_document_batch have had
        # expected_sha256 since 2.7; update_document — the one tool that replaces
        # everything — had none, and the push path uses add_document to overwrite,
        # so until now every push could clobber a concurrent change on the host with
        # only a backup to show for it. Checked BEFORE the backup, so a rejected
        # write leaves no trace at all. The engine re-checks (that copy covers the
        # admin API and stdio mode); this one is what stops the backup.
        expected = _arguments_of(message).get("expected_sha256")
        expected_bytes = _arguments_of(message).get("expected_bytes_sha256")
        if tool in ("add_document", "update_document") and expected_bytes not in (None, ""):
            current_raw = target.read_bytes() if target.is_file() else None
            byte_guard = check_expected_bytes_sha256(current_raw, expected_bytes)
            if byte_guard is not None:
                return _tool_result(msg_id, {**byte_guard, "filepath": filepath})
        if tool in ("add_document", "update_document") and expected not in (None, ""):
            if not target.is_file():
                return _tool_result(msg_id, {
                    "status": "error", "reason": "stale_file",
                    "message": ("expected_sha256 was given, but no file exists at that "
                                "path — it was deleted or moved since you read it. "
                                "Nothing was written."),
                    "filepath": filepath,
                    "expected_sha256": str(expected).strip().lower(),
                    "actual_sha256": None,
                })
            current_text, load_err = _load_document_text(target)
            if load_err is not None:
                return _tool_result(msg_id, {**load_err, "filepath": filepath})
            stale = _stale_check(msg_id, {"expected_sha256": expected}, current_text)
            if stale is not None:
                log.info("Refused %s on %s: expected_sha256 no longer matches", tool, filepath)
                return stale
        # Forensics for the overwrite path (2.10.6): an add_document that finds
        # a file already on disk is how a cloud-sync resurrection announces
        # itself (the run-5 ghost). Hash the leftover BEFORE it's overwritten
        # so the result identifies exactly what was buried — the manual
        # investigation that cracked the first ghost, automated.
        overwrote: dict | None = None
        if tool == "add_document" and target.is_file():
            prev_text, prev_err = _load_document_text(target)
            overwrote = {"overwrote_existing": True}
            if prev_err is None:
                overwrote["previous_content_sha256"] = content_sha256(prev_text)
        # 5.5: EVERY write that takes a backup names the backup it took. Until
        # now only the add_document OVERWRITE path did, so a caller that had just
        # replaced or deleted a file could not name the snapshot holding the old
        # bytes: the only route back was list_backups afterwards, taking the
        # newest entry and hoping nothing else had written that path in between.
        # On a shared path that is a guess, and this is the ONLY undo path in the
        # system. Success only — the error envelope (§11.1) is a closed shape and
        # a field meaning "here is the undo point for the write that happened"
        # must not ride on the refusal saying it did not.
        augment: dict = {}
        try:
            made = backup_if_exists(documents_dir, filepath, keep=backup_keep)
            if made is not None and (bid := backup_id_of(made)) is not None:
                augment["previous_backup_id"] = bid
                if overwrote is not None:
                    overwrote["previous_backup_id"] = bid  # 5.0 shape, unchanged
        except BackupError as exc:
            log.warning("Write aborted — backup failed: %s", exc)
            # The highest-consequence error this server produces — the write did
            # NOT happen and the cause is recoverable (free some disk, release
            # the OneDrive lock, retry). As a bare JSON-RPC error a
            # reason-branching client read it as a malformed request.
            return _tool_error(msg_id, "backup_failed",
                               f"Write aborted: {wire_error(exc)}. No changes were made.")
        upstream = await _send_buffered(client, request, worker_url, message)
        if isinstance(upstream, Response):
            return upstream
        if overwrote is not None:
            log.warning("add_document overwrote an existing %s (sha %s, backup %s)",
                        filepath, overwrote.get("previous_content_sha256", "?")[:16],
                        overwrote.get("previous_backup_id", "?"))
            return _rewrite_buffered_response(
                upstream, _augment_result_transform(overwrote, when_success=True,
                                                    tool_name=tool))

        # Belt and braces on the delete. Since 5.7 the engine checks this too —
        # `file_deleted` is an observed outcome now, not the echo of the argument
        # it used to be, and a still-present path comes back as `delete_failed`.
        # This check survives because it runs LATER: it catches a cloud-sync
        # daemon that recreated the file in the window between the engine's stat
        # and this response, which the engine cannot see from where it stands.
        if (tool == "remove_document" and _arguments_of(message).get("delete_file")
                and target.exists()):
            log.warning("remove_document reported success but %s still exists on disk",
                        filepath)
            augment["gateway_warning"] = (
                "The engine reported deletion, but the file STILL EXISTS on disk "
                "(locked or sync interference). It may be re-indexed by the file "
                "watcher; retry remove_document or delete it manually."
            )
        if augment:
            return _rewrite_buffered_response(
                upstream, _augment_result_transform(augment, when_success=True,
                                                    tool_name=tool))
        return Response(upstream.content, status_code=upstream.status_code,
                        headers=_response_headers(upstream),
                        media_type=upstream.headers.get("content-type"))


async def _handle_list_backups(message: dict, documents_dir) -> Response:
    """The list_backups gateway tool — reads the backups/ tree, never the worker."""
    msg_id = message.get("id")
    args = _arguments_of(message)
    try:
        entries = list_backup_entries(
            documents_dir,
            args.get("filepath") or None,
            prefix=args.get("prefix") or None,
            since=args.get("since") or None,
            until=args.get("until") or None,
        )
    except BackupError as exc:
        return _tool_result(msg_id, {
            "status": "error", "reason": "invalid_path", "message": str(exc),
        })
    payload = {
        "status": "success",
        "count": len(entries),
        "backups": entries[:200],
        # 5.0 §5.3: every collection response names the key holding its own
        # collection, so a generic client can do payload[payload["result_key"]]
        # instead of memorizing four different names. "results" is duplicated
        # here because the list is capped at 200 and the convenience is worth
        # the bytes; list_documents, which is unbounded, only names its key.
        "result_key": "backups",
        "results": entries[:200],
        "naming": "backups/<subpath>/<name>.<YYYYMMDD-HHMMSS><ext>; the timestamp is the backup_id",
    }
    if len(entries) > 200:
        payload["message"] = f"Showing newest 200 of {len(entries)}."
    return _tool_result(msg_id, payload)


async def _handle_diff_backup(message: dict, documents_dir) -> Response:
    """The diff_backup gateway tool — what changed SINCE a backup, no restore.

    Read-only history view: unified diff backup -> current, served entirely
    from disk. Larger caps than edit-result diffs (it IS the payload here)."""
    from .editing import _normalize, _unified_context_diff

    msg_id = message.get("id")
    args = _arguments_of(message)
    filepath = args.get("filepath") or ""
    backup_id = args.get("backup_id") or ""
    if not filepath or not backup_id:
        return _tool_error(msg_id, "invalid", "diff_backup requires filepath and backup_id.")
    target, err = _resolve_doc(msg_id, documents_dir, filepath)
    if err is not None:
        return err
    backup, err = _find_backup_or_hint(msg_id, documents_dir, filepath, backup_id)
    if err is not None:
        return err
    backup_text, load_err = _load_document_text(backup)
    if load_err is not None:
        return _tool_result(msg_id, load_err)
    current_text = ""
    current_missing = not target.is_file()
    if not current_missing:
        current_text, load_err = _load_document_text(target)
        if load_err is not None:
            return _tool_result(msg_id, load_err)

    before, after = _normalize(backup_text), _normalize(current_text)
    payload = {
        "status": "success",
        "filepath": filepath,
        "backup_id": backup_id,
        "identical": before == after,
    }
    if current_missing:
        payload["message"] = "The document no longer exists on disk; diff is vs empty."
    if not payload["identical"]:
        payload["diff"] = _unified_context_diff(
            before, after,
            fromfile=f"backup {backup_id}", tofile="current",
            max_lines=200, max_chars=10_000,
        )
    return _tool_result(msg_id, payload)


async def _handle_restore_backup(
    client: httpx.AsyncClient,
    request: Request,
    worker_url: str,
    message: dict,
    documents_dir,
    backup_keep: int = 0,
) -> Response:
    """The restore_backup gateway tool — the undo button.

    Reads the chosen backup, backs up the CURRENT content first (a restore is
    itself undoable), then routes through the same update_document transform as
    the edit tools so the engine owns the write + reindex."""
    from .editing import _normalize, _unified_context_diff

    msg_id = message.get("id")
    args = _arguments_of(message)
    filepath = args.get("filepath") or ""
    backup_id = args.get("backup_id") or ""
    if not filepath or not backup_id:
        return _tool_error(msg_id, "invalid", "restore_backup requires filepath and backup_id.")
    target, err = _resolve_doc(msg_id, documents_dir, filepath)
    if err is not None:
        return err

    async with _edit_lock(str(target)):
        backup, err = _find_backup_or_hint(msg_id, documents_dir, filepath, backup_id)
        if err is not None:
            return err
        # The backup on disk is byte-exact (shutil.copy2). Decoding it the
        # ordinary way would strip its BOM, and _normalize would rewrite every
        # CRLF to LF — so "undo" silently changed the file's line endings and
        # its bytes_sha256 no longer matched the backup it claimed to restore.
        # 5.0 made writes byte-verbatim; the restore path has to hand over the
        # bytes it actually wants persisted.
        restored_raw, load_err = _load_document_bytes(backup)
        if load_err is not None:
            return _tool_result(msg_id, load_err)
        backup_view = classify_text_bytes(restored_raw)
        if not backup_view.accepted and backup_view.reason != "binary_content":
            return _tool_result(msg_id, {
                "status": "error", "reason": backup_view.reason or "not_text",
                "message": backup_view.message or "Backup is not a supported document.",
            })
        # Binary/extracted formats and lossy UTF-8 are restored through the
        # exact-byte base64 input path; no text round trip is allowed.
        restore_encoding = "utf-8" if backup_view.accepted and backup_view.utf8_valid else "base64"
        new_content = (backup_view.text if restore_encoding == "utf-8" else
                       base64.b64encode(restored_raw).decode("ascii"))

        current_text = ""
        undo = None  # the snapshot of what this restore overwrites, if anything
        target_exists = target.is_file()
        if target_exists:
            current_raw, current_load_err = _load_document_bytes(target)
            if current_load_err is not None:
                return _tool_result(msg_id, current_load_err)
            byte_guard = check_expected_bytes_sha256(
                current_raw, args.get("expected_bytes_sha256")
            )
            if byte_guard is not None:
                return _tool_result(msg_id, byte_guard)
            current_view = classify_text_bytes(current_raw)
            if current_view.reason == "binary_content" or current_view.decode_error_bytes:
                if args.get("expected_sha256") not in (None, ""):
                    return _tool_result(msg_id, {"status": "error", "reason": "not_text",
                                                "message": "expected_sha256 cannot guard a binary file."})
                current_text = ""
            else:
                current_text, load_err = _load_document_text(target)
                if load_err is not None:
                    return _tool_result(msg_id, load_err)
                stale = _stale_check(msg_id, args, current_text)
                if stale is not None:
                    return stale
            if current_raw == restored_raw:
                return _tool_result(msg_id, {
                    "status": "error", "reason": "no_change",
                    "message": f"The document already matches backup {backup_id}; nothing to restore.",
                })
            # a restore must be undoable too: snapshot the current state first
            try:
                undo = backup_if_exists(documents_dir, filepath, keep=backup_keep)
            except BackupError as exc:
                # A backup failure is a TOOL-level outcome the caller can act on
                # (free some disk, retry), not a malformed request. Sending it as
                # a bare JSON-RPC error left a reason-branching client treating
                # the single most consequential failure this server produces as a
                # protocol fault.
                return _tool_error(msg_id, "backup_failed",
                                   f"Restore aborted: {wire_error(exc)}. No changes were made.")
        elif args.get("expected_sha256") not in (None, "") or args.get("expected_bytes_sha256") not in (None, ""):
            # The guard used to be nested inside the exists() branch, so naming a
            # version to replace was silently ignored for a file that had since
            # been deleted — a declared precondition that did nothing. Both write
            # tools treat exactly this case as stale_file.
            return _tool_error(
                msg_id, "stale_file",
                "The file does not exist, so it cannot match the expected_sha256 you "
                "named. Nothing was restored. Re-read the document (or omit "
                "expected_sha256 to recreate it from the backup).",
                filepath=filepath, actual_sha256=None,
            )

        upstream_message = {
            "jsonrpc": "2.0",
            "id": msg_id,
            "method": "tools/call",
            "params": {
                # A deleted file cannot be restored with update_document: the
                # engine refuses a path that is not on disk (reason: not_found).
                # That made restore_backup fail for the exact case remove_document
                # and remove_directory advertise it for — "restore_backup puts any
                # of them back" — which is the one recovery path DESIGN-5.0 §7.2
                # documents. add_document takes the same guard set for a new path.
                "name": "update_document" if target_exists else "add_document",
                # absolute path — same engine path quirk as the edit tools
                "arguments": {"filepath": str(target), "content": new_content,
                               "content_encoding": restore_encoding},
            },
        }
        upstream = await _send_buffered(client, request, worker_url, upstream_message)
        if isinstance(upstream, Response):
            return upstream  # worker died mid-request -> clean 503
        log.info("restore_backup %s <- %s", Path(filepath).name, backup_id)
        fields = {
            "restored_from_backup": backup_id,
            # Credential for the next guarded write. Hash what the engine will
            # actually persist — since 5.0 that is the content VERBATIM, so the
            # .strip() that used to be here would now predict the wrong hash for
            # any file with a trailing newline.
            "new_content_sha256": content_sha256(new_content),
        }
        # 5.5: name the snapshot taken of what this restore overwrote, so the
        # undo is itself undoable by id rather than by inference. Absent when the
        # restore recreated a DELETED file — there was nothing to snapshot.
        undo_id = backup_id_of(undo) if undo is not None else None
        if undo_id is not None:
            fields["previous_backup_id"] = undo_id
        # Both sides normalized: the diff is for a human/model to read, and an
        # EOL-only restore would otherwise render as every line changed.
        diff = ("" if restore_encoding == "base64" else
                _unified_context_diff(_normalize(current_text), _normalize(new_content)))
        return _rewrite_buffered_response(
            upstream, _edit_response_transform(filepath, fields, diff, RESTORE_BACKUP_TOOL_NAME)
        )


async def _handle_read_document(message: dict, documents_dir) -> Response:
    """The read_document gateway tool — verbatim ranged/section reads from disk.

    Never touches the worker; available on read-only projects too."""
    msg_id = message.get("id")
    args = _arguments_of(message)
    target, err = _resolve_doc(msg_id, documents_dir, args.get("filepath") or "")
    if err is not None:
        return err
    filepath = args["filepath"]
    if not target.is_file():
        return _tool_error(msg_id, "not_found", f"No such document: {filepath!r}.")
    raw, load_err = _load_document_bytes(target)
    if load_err is not None:
        return _tool_result(msg_id, load_err)
    # 5.6: VERBATIM — BOM and line endings intact. read_slice normalizes
    # internally for addressing and for content_sha256; what it returns is the
    # file. The old _decode_document here folded CRLF and dropped the BOM, so
    # the one tool documented as "verbatim ranged reads from disk" was the tool
    # that could not be byte-compared against the disk.
    byte_view = classify_text_bytes(raw)
    if not byte_view.accepted:
        return _tool_result(msg_id, {
            "status": "error", "reason": byte_view.reason or "not_text",
            "message": byte_view.message or "File is not readable UTF-8 text.",
        })
    # The readable view is tolerant: replacement characters are exposed while
    # the exact bytes remain available through get_document(base64=true).
    text = byte_view.text
    try:
        payload = read_slice(
            text, args.get("start_line"), args.get("end_line"), args.get("section")
        )
    except EditReject as exc:
        return _tool_result(msg_id, exc.payload)
    payload["filepath"] = filepath
    # The facts about the file itself, so a byte check needs no second call.
    #
    # 5.0.1 documented that `text` was newline-normalized and argued the anchor
    # matcher required it. That argument was wrong: apply_edit normalizes the
    # ANCHOR as well (editing._normalize on old_str), so a CRLF anchor has always
    # matched a CRLF file. The folding bought nothing and cost byte fidelity on
    # the one tool whose whole job is verbatim reads. 5.6 removes it — `text` is
    # now the file, and sha256 of its UTF-8 encoding equals bytes_sha256 for a
    # whole-file read.
    payload["bytes_sha256"] = hashlib.sha256(raw).hexdigest()
    payload["size_bytes"] = len(raw)
    if byte_view.content_is_lossy:
        payload["content_sha256"] = None
    payload["line_endings"] = byte_view.line_endings
    payload.update({
        "utf8_valid": byte_view.utf8_valid,
        "decode_error_bytes": byte_view.decode_error_bytes,
        "content_is_lossy": byte_view.content_is_lossy,
        "index_text_sanitized": byte_view.index_text_sanitized,
    })
    # Kept in the shape (a connector may branch on it) and now always false:
    # nothing normalizes `text` any more.
    payload["normalized_line_endings"] = False
    payload["content_note"] = (
        "`text` is a readable UTF-8 view containing U+FFFD replacements for malformed "
        "source bytes, so it does NOT hash to bytes_sha256. Use get_document with "
        "content_encoding=base64 for the exact original bytes."
        if byte_view.content_is_lossy else
        "`text` is VERBATIM — the file's bytes decoded as UTF-8, with its own line "
        "endings and BOM intact. A whole-file read hashes to bytes_sha256. "
        "`content_sha256` is the separate write-guard hash (BOM dropped, CRLF/CR "
        "folded to LF) that expected_sha256 compares against; it is a version stamp, "
        "not a description of this text."
    )
    try:  # staleness companion to content_sha256 (2.7)
        payload["mtime"] = datetime.fromtimestamp(target.stat().st_mtime).isoformat(sep=" ")
    except OSError:
        pass
    return _tool_result(msg_id, payload)


def _augment_result_transform(extra: dict, when_success: bool = False,
                              tool_name: str | None = None):
    """Payload transform that merges extra fields into the engine's JSON text
    block (used to decorate a passthrough result, e.g. a gateway warning).

    when_success: merge only into a payload the engine reported `success` for.
    The error envelope (DESIGN-5.0 §11.1) is a documented closed shape, and a
    field that only means something for a write that HAPPENED must not appear
    on the refusal saying it did not.
    """

    def transform(payload: dict) -> dict:
        result = payload.get("result")
        if not isinstance(result, dict):
            return payload
        engine = result.get("structuredContent")
        if not isinstance(engine, dict):
            content = result.get("content")
            if not (isinstance(content, list) and content and isinstance(content[0], dict)):
                return payload
            try:
                engine = json.loads(content[0].get("text", ""))
            except (json.JSONDecodeError, TypeError):
                return payload
        if isinstance(engine, dict):
            if when_success and engine.get("status") != "success":
                return payload
            merged = {**engine, **extra}
            if tool_name in OUTPUT_SCHEMAS_BY_TOOL:
                result.update(build_tool_result(
                    tool_name, merged, is_error=bool(result.get("isError")),
                    extra_content=(result.get("content") or [])[1:],
                    mutating=tool_name in MUTATING_TOOLS,
                ))
            else:
                result["structuredContent"] = merged
                result["content"] = [{"type": "text", "text": json.dumps(merged, indent=2)}]
        return payload

    return transform


def _edit_response_transform(filepath: str, fields: dict, context_diff: str,
                             tool_name: str = EDIT_TOOL_NAME):
    """Merge the engine's update_document result with the gateway's edit fields.

    `fields` is the tool-specific accounting: {replacements, match_mode} for
    edit_document; {edits_applied, replacements, edits} for the batch tool.
    """

    def transform(payload: dict) -> dict:
        result = payload.get("result")
        if not isinstance(result, dict):
            return payload
        content = result.get("content")
        block = content[0] if isinstance(content, list) and content and isinstance(content[0], dict) else {}
        engine = result.get("structuredContent")
        parsed_ok = isinstance(engine, dict)
        if not parsed_ok:
            if block.get("type") != "text":
                return payload
            try:
                engine = json.loads(block.get("text", ""))
            except (json.JSONDecodeError, TypeError):
                parsed_ok = False
                engine = {"engine_response": block.get("text")}
        if not isinstance(engine, dict):
            parsed_ok = False
            engine = {"engine_response": engine}
        # An unparseable engine body used to default to status "success" — so any
        # framing change or truncation on the engine hop turned a FAILED write
        # into a reported success carrying new_content_sha256 for content that
        # was never persisted. Default to error, and name why.
        default_status = "success" if parsed_ok else "error"
        status = engine.pop("status", default_status)
        merged = {
            "status": status,
            "filepath": engine.pop("filepath", filepath),
            # Only decorate a SUCCESS with the gateway's accounting. On a failure
            # `fields` carried new_content_sha256 and the payload carried a
            # context_diff — a hash and a diff describing a change that was never
            # written. A client that fed that hash back as expected_sha256 got a
            # spurious stale_file forever.
            **(fields if status == "success" else {}),
            **engine,  # old_chunks_removed / new_chunks_added / dedup_skipped / ...
        }
        if status == "success":
            merged["context_diff"] = context_diff
        else:
            merged.setdefault(
                "reason", "error" if parsed_ok else "internal_error",
            )
            if not parsed_ok:
                merged.setdefault(
                    "message",
                    "The engine returned a response this gateway could not parse. "
                    "The write may not have been applied — re-read the document "
                    "before retrying.",
                )
                log.error("Unparseable engine response for %s; reported as error", filepath)
        result.update(build_tool_result(
            tool_name, merged, is_error=status == "error",
            extra_content=(content or [])[1:] if isinstance(content, list) else [],
            mutating=tool_name in MUTATING_TOOLS,
        ))
        return payload

    return transform


async def _handle_edit_document(
    client: httpx.AsyncClient,
    request: Request,
    worker_url: str,
    message: dict,
    documents_dir,
    mode: str = "single",
    backup_keep: int = 0,
) -> Response:
    """The edit_document / edit_document_batch / insert_in_document gateway
    tools (DESIGN-2.0-edit-document.md). mode: "single" | "batch" | "insert".

    Validate -> read file from disk -> splice (anchored edit(s) or insertion) ->
    mandatory backup -> transform into ONE update_document call (same JSON-RPC
    id, session headers preserved) -> forward -> synthesize the merged result.
    The whole flow holds the file's edit lock; anchored modes' exactly-once
    match doubles as the concurrency check against out-of-band writers. A batch
    is all-or-nothing: any failed match rejects the lot before anything writes.
    """
    msg_id = message.get("id")
    args = _arguments_of(message)
    filepath = args.get("filepath") or ""
    tool_name = {"single": EDIT_TOOL_NAME, "batch": BATCH_TOOL_NAME,
                 "insert": INSERT_TOOL_NAME}[mode]
    if mode == "batch":
        bad_args = not filepath or not isinstance(args.get("edits"), list)
        requires = "filepath and a non-empty edits array"
    elif mode == "insert":
        bad_args = (not filepath or not isinstance(args.get("text"), str)
                    or not isinstance(args.get("position"), str))
        requires = "filepath, text and position"
    else:
        old_str, new_str = args.get("old_str"), args.get("new_str")
        bad_args = not filepath or not isinstance(old_str, str) or not isinstance(new_str, str)
        requires = "filepath, old_str and new_str"
    if bad_args:
        return _tool_result(msg_id, {
            "status": "error", "reason": "invalid",
            "message": f"{tool_name} requires {requires}.",
        })

    target = resolve_target(documents_dir, filepath)
    if target is None:
        return _tool_result(msg_id, {
            "status": "error", "reason": "invalid_path",
            "message": f"filepath resolves outside this project: {filepath!r}",
        })

    async with _edit_lock(str(target)):
        if not target.is_file():
            return _tool_result(msg_id, {
                "status": "error", "reason": "not_found",
                "message": f"No such document: {filepath!r}. Use add_document to create new files.",
            })
        # Two views of the same read. `verbatim` is the file — BOM, line endings
        # and all — and is what the edited result gets re-dressed in below.
        # `text` is the BOM-stripped view every matcher, hint and staleness hash
        # has always seen, unchanged.
        raw, load_err = _load_document_bytes(target)
        if load_err is not None:
            return _tool_result(msg_id, load_err)
        byte_view = classify_text_bytes(raw)
        if byte_view.decode_error_bytes:
            return _tool_result(msg_id, {
                "status": "error", "reason": "lossy_edit_unsupported",
                "message": (
                    "Surgical edits do not accept malformed UTF-8 because replacement "
                    "characters could rewrite unrelated bytes. Read the exact file with "
                    "content_encoding=base64 and use a full guarded replacement instead."
                ),
                "decode_error_bytes": byte_view.decode_error_bytes,
                "bytes_sha256": byte_view.facts()["bytes_sha256"],
            })
        if not byte_view.accepted:
            return _tool_result(msg_id, {
                "status": "error", "reason": byte_view.reason or "not_text",
                "message": byte_view.message or "File is not editable text.",
            })
        byte_guard = check_expected_bytes_sha256(raw, args.get("expected_bytes_sha256"))
        if byte_guard is not None:
            return _tool_result(msg_id, byte_guard)
        verbatim, load_err = _decode_verbatim(raw)
        if load_err is not None:
            return _tool_result(msg_id, load_err)
        text = _strip_bom(verbatim)
        stale = _stale_check(msg_id, args, text)
        if stale is not None:
            log.info("%s rejected (stale/invalid sha): %s", tool_name, filepath)
            return stale

        try:
            if mode == "batch":
                b = apply_batch(text, args.get("edits") or [])
                new_content, context_diff = b.new_content, b.context_diff
                fields = {
                    "edits_applied": len(b.edits),
                    "replacements": sum(e["replacements"] for e in b.edits),
                    "edits": b.edits,
                }
            elif mode == "insert":
                ins = apply_insert(
                    text, args.get("text", ""), args.get("position", ""),
                    args.get("section") or None,
                )
                new_content, context_diff = ins.new_content, ins.context_diff
                fields = {
                    "inserted_at_line": ins.inserted_at_line,
                    "position": args.get("position"),
                }
                if args.get("section"):
                    fields["section"] = args["section"]
            else:
                outcome = apply_edit(
                    text, old_str, new_str, bool(args.get("replace_all", False))
                )
                new_content, context_diff = outcome.new_content, outcome.context_diff
                fields = {
                    "replacements": outcome.replacements,
                    "match_mode": outcome.match_mode,
                }
        except EditReject as exc:
            payload = exc.payload
            if bool(args.get("dry_run", False)):
                payload = {**payload, "dry_run": True}  # a failed dry run is the use case
            log.info("%s rejected (%s): %s", tool_name, payload.get("reason"), filepath)
            return _tool_result(msg_id, payload)

        # 5.6: the edit happened in LF-space; the FILE goes back in its own
        # flavor. All three modes above return normalized text, so this one line
        # covers edit, batch and insert. Before it, a one-word edit to a CRLF
        # document rewrote every line ending in the file and dropped its BOM —
        # a change nobody asked for, dressed as the change they did.
        new_content = restore_line_endings(verbatim, new_content)

        if bool(args.get("dry_run", False)):
            # Full match + diff, zero side effects: no backup, no worker call,
            # no write, no reindex. `applied: false` so the model can't mistake
            # the preview for a landed edit. The file is UNCHANGED, so the
            # credential for the real apply is the CURRENT hash (returning the
            # would-be hash here would bait a spurious stale_file on apply).
            log.info("%s dry run: %s %s", tool_name, Path(filepath).name, fields)
            return _tool_result(msg_id, {
                "status": "success",
                "dry_run": True,
                "applied": False,
                "filepath": filepath,
                **fields,
                "current_content_sha256": content_sha256(text),
                "context_diff": context_diff,
                "message": "Dry run: anchors validated and diff previewed. NOTHING was "
                           "written — repeat without dry_run to apply, passing "
                           "current_content_sha256 as expected_sha256.",
            })

        # Hand back the credential for the NEXT guarded write: a successful
        # edit invalidates the hash the caller just used (its own write changed
        # the file), so without this every follow-up edit would need a pointless
        # re-read. Hash what the engine will actually persist — verbatim as of
        # 5.0, so no strip here either (see LocalEngineHost._write_verbatim).
        fields["new_content_sha256"] = content_sha256(new_content)

        # Mandatory backup BEFORE the worker writes (DESIGN.md §6.1) — same
        # guarantee as update_document; a failed backup aborts the edit.
        try:
            made = backup_if_exists(documents_dir, filepath, keep=backup_keep)
        except BackupError as exc:
            log.warning("Edit aborted — backup failed: %s", exc)
            return _tool_error(msg_id, "backup_failed",
                               f"Edit aborted: {wire_error(exc)}. No changes were made.")
        # 5.5: the id of the snapshot this edit just took — the undo point for
        # THIS call, handed back rather than left to be guessed at from a later
        # listing. edit_document always finds the file on disk (the not_found
        # check above), so a backup is always made.
        if made is not None and (bid := backup_id_of(made)) is not None:
            fields["previous_backup_id"] = bid

        upstream_message = {
            "jsonrpc": "2.0",
            "id": msg_id,
            "method": "tools/call",
            "params": {
                "name": "update_document",
                # ABSOLUTE path: the engine resolves relative paths against the
                # worker's CWD (its base dir), not the docs dir — the known
                # update_document path quirk. resolve_target gave us the truth.
                "arguments": {
                    "filepath": str(target), "content": new_content,
                    **({"expected_bytes_sha256": args["expected_bytes_sha256"]}
                       if args.get("expected_bytes_sha256") not in (None, "") else {}),
                },
            },
        }
        upstream = await _send_buffered(client, request, worker_url, upstream_message)
        if isinstance(upstream, Response):
            return upstream  # worker died mid-request -> clean 503
        log.info(
            "%s %s: %s", tool_name, Path(filepath).name,
            {k: v for k, v in fields.items() if k != "edits"},  # keep the line short
        )
        return _rewrite_buffered_response(
            upstream, _edit_response_transform(filepath, fields, context_diff, tool_name)
        )


async def _intercept(
    client: httpx.AsyncClient,
    request: Request,
    worker_url: str,
    message: dict,
    *,
    readonly: bool,
    documents_dir,
    backup_keep: int,
    project_name: str,
    connector_id: str = "legacy",
    write_admission: Callable[[], tuple[str, str] | None] | None = None,
) -> Response | None:
    """Everything the gateway does to one JSON-RPC message before (or instead
    of) forwarding it: read-only policy, strict argument checking, and the
    gateway's own tools.

    Returns the Response to send, or None meaning "not ours — forward it".
    Factored out of proxy_mcp so the batch path (5.0 §1.1) runs byte-identical
    policy per element; a batch that skipped any of this would be a hole in the
    read-only gate wearing a JSON array as a disguise.
    """
    method = message.get("method")
    if method != "tools/call":
        return None
    msg_id = message.get("id")
    params = _params_of(message)  # malformed (non-dict) params must not 500
    tool = params.get("name", "")

    if readonly and not is_tool_allowed_remote(tool):
        # edit_document is not allow-listed, so read-only blocks it here too
        log.info("Blocked mutating tool (read-only project): %s", tool)
        return _tool_error(
            msg_id, "read_only",
            f"Tool '{tool}' is not available: this knowledge base is read-only.",
        )

    # 14.0.0: the 5.2 guard that refused arguments (and asset tools) the pinned
    # 3.x engine could not honor lived here, behind `legacy_engine`. The 3.x
    # `engine: workers` rollback was removed, so there is nothing left to refuse.

    # 5.0 §2: strict arguments for the gateway's own tools. The engine runs
    # the same check against its own defs, so between the two every tool on the
    # surface refuses an argument it does not implement instead of dropping it.
    if (tool_def := GATEWAY_TOOL_DEFS.get(tool)) is not None:
        # A mutating gateway tool also accepts operation_id (5.2). Validating
        # against the bare def would reject it as undeclared BEFORE the replay
        # guard below ever runs, making the argument impossible to use on the
        # very tools it exists for.
        if tool in MUTATING_TOOLS:
            tool_def = with_operation_id_argument(tool_def)
        rejected = reject_unknown_arguments(tool_def, _arguments_of(message))
        if rejected is not None:
            log.info("Refused %s: unknown argument(s) %s", tool, rejected["rejected_arguments"])
            return _tool_result(msg_id, rejected)
        # And the declared TYPE, which nothing enforced: a wrong type reached the
        # handler and came back as internal_error with raw exception text.
        mistyped = reject_wrong_types(tool_def, _arguments_of(message))
        if mistyped is not None:
            log.info("Refused %s: wrong type for %s", tool, mistyped["invalid_arguments"])
            return _tool_result(msg_id, mistyped)

    if tool == SELFTEST_TOOL_NAME:
        # Server-versioned test protocol; plan tailored to writability.
        from . import __version__

        return _tool_result(msg_id, select_self_test_plan(
            __version__, readonly, _arguments_of(message).get("section")
        ))
    if tool in (READ_TOOL_NAME, LIST_BACKUPS_TOOL_NAME, DIFF_BACKUP_TOOL_NAME):
        # Gateway-served, read-only: allowed on every project, answered from
        # disk, never forwarded to the worker.
        if documents_dir is None:
            return _jsonrpc_error(msg_id, f"{tool} is unavailable for this project.")
        if tool == READ_TOOL_NAME:
            return await _handle_read_document(message, documents_dir)
        if tool == DIFF_BACKUP_TOOL_NAME:
            return await _handle_diff_backup(message, documents_dir)
        return await _handle_list_backups(message, documents_dir)
    if not readonly and tool in MUTATING_TOOLS:
        # 5.2: optional replay guard. A client that timed out and retried is NOT
        # canceling the first attempt — that one is still running and still
        # holding the lock below, so the retry executes against the state the
        # first attempt already produced and is told stale_file / not_found /
        # destination_exists for a write that SUCCEEDED.
        operation_id, bad_id = normalize_operation_id(
            _arguments_of(message).get(OPERATION_ID_ARG)
        )
        if bad_id is not None:
            return _tool_result(msg_id, bad_id)
        request_digest = operation_request_digest(message)
        # A directory move carries its receipt in source-side ProjectState.  The
        # source may already be gone on a retry, so classification is based on
        # its required policy CAS argument rather than a fresh ``is_dir`` check.
        # It must bypass the gateway's file backup/replay cache entirely.
        directory_move = (
            tool == "move_document"
            and "expected_policy_revision" in _arguments_of(message)
        )
        # SINGLE-WRITER GATE (3.0): hold the per-worker write lock for the whole
        # (synchronous) write so no two mutating calls to this project's worker
        # overlap — the engine leaves its add/update/remove paths unlocked, so
        # overlap corrupts SQLite.
        async with _worker_write_lock(project_name):
            # Policy can change while the request waits for this project lock.
            # Recheck before replay lookup or any write-side effect.
            if write_admission is not None and (denied := write_admission()) is not None:
                reason, message = denied
                log.info(
                    "Refused queued write connector_id=%s project=%s tool=%s reason=%s",
                    connector_id, project_name, tool, reason,
                )
                return _tool_error(msg_id, reason, message)

            if (operation_id is not None and not directory_move and tool not in ASSET_MUTATING_TOOLS
                    and tool not in ALL_ADDITIVE_MUTATING_TOOLS):
                # Check after acquiring the project lock as well as the policy
                # authorization above. Two simultaneous retries must not both
                # miss an empty cache and then perform the same write serially.
                replay_state, done = _operations.lookup(
                    connector_id, project_name, tool, operation_id, request_digest
                )
                if replay_state == "conflict":
                    return _tool_error(
                        msg_id, "operation_conflict",
                        "operation_id was already used for different operation content.",
                    )
                if done is not None:
                    log.info("Replaying %s for operation_id=%s (already completed)",
                             tool, operation_id)
                    return _tool_result(msg_id, {**done, REPLAY_MARKER: True})
                # The engine's own strict-argument gate would refuse a key its
                # schema does not declare, so consume it here rather than
                # forwarding it.
                _strip_operation_id(message)
            if tool in (EDIT_TOOL_NAME, BATCH_TOOL_NAME,
                        INSERT_TOOL_NAME, RESTORE_BACKUP_TOOL_NAME):
                if documents_dir is None:
                    return _jsonrpc_error(msg_id, f"{tool} is unavailable for this project.")
                if tool == RESTORE_BACKUP_TOOL_NAME:
                    return _remember_operation(connector_id, project_name, tool, operation_id,
                        await _handle_restore_backup(
                            client, request, worker_url, message, documents_dir,
                            backup_keep=backup_keep,
                        ), msg_id, request_digest)
                mode = {EDIT_TOOL_NAME: "single", BATCH_TOOL_NAME: "batch",
                        INSERT_TOOL_NAME: "insert"}[tool]
                return _remember_operation(connector_id, project_name, tool, operation_id,
                    await _handle_edit_document(
                        client, request, worker_url, message, documents_dir,
                        mode=mode, backup_keep=backup_keep,
                    ), msg_id, request_digest)
            if tool == BATCH_WRITE_TOOL_NAME and documents_dir is not None:
                return _remember_operation(connector_id, project_name, tool, operation_id,
                    await _handle_batch_write(
                        client, request, worker_url, message, documents_dir,
                        backup_keep,
                    ), msg_id, request_digest)
            if tool in _BACKUP_TOOLS and not directory_move and documents_dir is not None:
                return _remember_operation(connector_id, project_name, tool, operation_id,
                    await _handle_engine_write(
                        client, request, worker_url, message, documents_dir, backup_keep,
                    ), msg_id, request_digest)
            # Other mutating engine tools (remove_document; reindex_documents; the 5.0 directory
            # tools, which take their own per-file backups inside the engine
            # because one `filepath` cannot describe them; add_from_url when no
            # documents_dir): forward buffered, still under the write lock so
            # they cannot overlap a concurrent add/update.
            upstream = await _send_buffered(client, request, worker_url, message)
            if isinstance(upstream, Response):
                return upstream  # worker died mid-request; nothing to remember
            forwarded = Response(
                upstream.content, status_code=upstream.status_code,
                headers=_response_headers(upstream),
                media_type=upstream.headers.get("content-type"),
            )
            # These mutations carry their receipt and replay authority in the
            # project's FULL-synchronous state database. The process-local
            # gateway cache must not strip their operation_id or shadow that
            # durable authority.
            if tool in ALL_ADDITIVE_MUTATING_TOOLS or directory_move:
                return forwarded
            return _remember_operation(connector_id, project_name, tool, operation_id,
                                       forwarded, msg_id, request_digest)
    return None


def _payload_from_response(response: Response, msg_id) -> dict | None:
    """The JSON-RPC payload carried by a Response, for assembling a batch reply.

    Handles the three shapes a forward can come back as: plain JSON, an
    SSE-framed data line (workers mode), and a bare non-JSON body (a 503 from a
    dead worker) — which becomes a JSON-RPC error entry rather than corrupting
    the array or vanishing from it.
    """
    body = getattr(response, "body", b"") or b""
    text = body.decode("utf-8", errors="replace") if isinstance(body, bytes) else str(body)
    try:
        payload = json.loads(text)
        if isinstance(payload, dict):
            return payload
    except (json.JSONDecodeError, ValueError):
        for line in text.splitlines():
            if line.startswith("data:"):
                try:
                    payload = json.loads(line[5:].strip())
                except json.JSONDecodeError:
                    continue
                if isinstance(payload, dict):
                    return payload
    if response.status_code >= 400:
        return {"jsonrpc": "2.0", "id": msg_id,
                "error": {"code": -32603,
                          "message": f"Upstream returned {response.status_code}: "
                                     f"{text[:200] or 'no body'}"}}
    return None


def _normalize_tool_rpc_payload(payload: dict, tool_name: str) -> dict:
    """Upgrade a worker result to the 10.1 dual representation.

    New engines already emit ``structuredContent``; older workers emit only a
    JSON text block.  The latter is parsed once at this boundary, then rebuilt
    through the shared contract builder so the public response never remains
    text-only and both representations cannot drift during gateway transforms.
    """
    result = payload.get("result")
    if not isinstance(result, dict) or "error" in payload:
        return payload
    content = result.get("content")
    if not isinstance(content, list) or not content:
        candidate = result.get("structuredContent")
        extras = []
    else:
        first_is_text = isinstance(content[0], dict) and content[0].get("type") == "text"
        extras = content[1:] if first_is_text else content
        candidate = result.get("structuredContent")
        if not isinstance(candidate, dict):
            first = content[0] if first_is_text else {}
            try:
                candidate = json.loads(first.get("text", ""))
            except (TypeError, ValueError, AttributeError):
                candidate = None
    if not isinstance(candidate, dict):
        candidate = {}
    built = build_tool_result(
        tool_name, candidate,
        is_error=bool(result.get("isError", candidate.get("status") == "error")),
        extra_content=extras,
        mutating=tool_name in MUTATING_TOOLS,
    )
    payload["result"] = built
    return payload


async def _forward_one(
    client: httpx.AsyncClient,
    request: Request,
    worker_url: str,
    message: dict,
    *,
    readonly: bool,
    project_name: str,
    documents_dir,
) -> Response:
    """Forward one non-intercepted message, buffered (never streamed).

    The streaming path in proxy_mcp cannot be used inside a batch: the array has
    to be assembled before anything is sent.
    """
    upstream = await _send_buffered(client, request, worker_url, message)
    if isinstance(upstream, Response):
        return upstream
    if message.get("method") == "tools/list":
        return _rewrite_tools_list_response(upstream, readonly, project_name, documents_dir)
    if message.get("method") == "tools/call":
        body = getattr(upstream, "body", b"") or b""
        try:
            decoded = json.loads(body)
        except (TypeError, ValueError):
            decoded = None
        if isinstance(decoded, dict):
            decoded = _normalize_tool_rpc_payload(
                decoded, str((_params_of(message) or {}).get("name") or "")
            )
            return JSONResponse(decoded, status_code=upstream.status_code,
                                headers=_response_headers(upstream))
    return Response(upstream.content, status_code=upstream.status_code,
                    headers=_response_headers(upstream),
                    media_type=upstream.headers.get("content-type"))


async def _handle_batch(
    client: httpx.AsyncClient,
    request: Request,
    worker_url: str,
    messages: list,
    *,
    readonly: bool,
    documents_dir,
    backup_keep: int,
    project_name: str,
    connector_id: str = "legacy",
    write_admission: Callable[[], tuple[str, str] | None] | None = None,
) -> Response:
    """A JSON-RPC 2.0 batch: an array in, an array of results out (5.0 §1.1).

    Batching is part of JSON-RPC 2.0 and MCP inherits it; an array used to be
    answered with -32600, so 17 pushes were 17 HTTP round trips through the
    tunnel. Elements are executed IN ORDER and sequentially, never concurrently:
    they can touch the same file, and the write lock is per project, so
    overlapping them would deadlock or interleave two writes to one document.

    Batching is a transport optimization and nothing more — it is NOT a
    transaction. One element failing does not roll back the others, which is
    exactly why copy_directory exists as its own tool rather than as a batch of
    copy_document calls.
    """
    if not messages:
        return _jsonrpc_error(None, "Invalid request: empty batch", code=-32600)
    replies: list[dict] = []
    for message in messages:
        if not isinstance(message, dict):
            replies.append({"jsonrpc": "2.0", "id": None,
                            "error": {"code": -32600, "message": "Invalid request"}})
            continue
        _log_request(project_name, message)
        method = message.get("method")
        if isinstance(method, str) and method.startswith("notifications/"):
            continue  # a notification gets no reply, in a batch or out of one
        response = await _intercept(
            client, request, worker_url, message, readonly=readonly,
            documents_dir=documents_dir, backup_keep=backup_keep,
            project_name=project_name, connector_id=connector_id,
            write_admission=write_admission,
        )
        if response is None:
            response = await _forward_one(
                client, request, worker_url, message, readonly=readonly,
                project_name=project_name, documents_dir=documents_dir,
            )
        payload = _payload_from_response(response, message.get("id"))
        if payload is not None and message.get("method") == "tools/call":
            tool_name = str((_params_of(message) or {}).get("name") or "")
            if tool_name:
                payload = _normalize_tool_rpc_payload(payload, tool_name)
        if payload is not None and message.get("id") is not None:
            replies.append(payload)
    if not replies:
        # Every element was a notification: the spec says return nothing.
        return Response(status_code=202)
    return JSONResponse(replies)


async def proxy_mcp(
    client: httpx.AsyncClient,
    request: Request,
    worker_url: str,
    readonly: bool = True,
    documents_dir=None,
    backup_keep: int = 0,
    project_name: str = "?",
    connector_id: str = "legacy",
    body_override: bytes | None = None,
    write_admission: Callable[[], tuple[str, str] | None] | None = None,
) -> Response:
    """Forward one /mcp request to the project's worker (stateless per request —
    routing is derived from the token upstream of this call, §4.4 #1).

    readonly=True  -> filter tools/list to read tools, reject mutating calls.
    readonly=False -> full toolset; back up any file BEFORE a destructive write
                      (§6.1). A failed backup aborts the write.
    backup_keep    -> retention (config backup_keep_per_file): newest N backups
                      kept per file after each new one; 0 = unlimited.

    A JSON array body is a JSON-RPC batch and is answered as one (5.0 §1.1).
    """
    url = worker_url
    body = await request.body() if body_override is None else body_override

    # ---- policy + message inspection (POSTs carry JSON-RPC) ----
    is_tools_list = False
    tool_call_name: str | None = None
    if request.method == "POST" and body:
        try:
            message = json.loads(body)
        except json.JSONDecodeError:
            message = None
        if isinstance(message, list):
            return await _handle_batch(
                client, request, worker_url, message, readonly=readonly,
                documents_dir=documents_dir, backup_keep=backup_keep,
                project_name=project_name, connector_id=connector_id,
                write_admission=write_admission,
            )
        if isinstance(message, dict):
            _log_request(project_name, message)  # before policy: rejections show too
            intercepted_tool = (
                str((_params_of(message) or {}).get("name") or "")
                if message.get("method") == "tools/call" else ""
            )
            intercepted = await _intercept(
                client, request, worker_url, message, readonly=readonly,
                documents_dir=documents_dir, backup_keep=backup_keep,
                project_name=project_name, connector_id=connector_id,
                write_admission=write_admission,
            )
            if intercepted is not None:
                # Intercepted write/transform paths may still carry a legacy
                # worker's text-only result. Apply the same final public-contract
                # normalization as the ordinary forwarding path.
                if intercepted_tool:
                    intercepted_body = getattr(intercepted, "body", b"") or b""
                    try:
                        intercepted_payload = json.loads(intercepted_body)
                    except (TypeError, ValueError):
                        intercepted_payload = None
                    if isinstance(intercepted_payload, dict):
                        intercepted_payload = _normalize_tool_rpc_payload(
                            intercepted_payload, intercepted_tool
                        )
                        return JSONResponse(
                            intercepted_payload,
                            status_code=intercepted.status_code,
                            headers=_response_headers(intercepted),
                        )
                return intercepted
            is_tools_list = message.get("method") == "tools/list"
            if message.get("method") == "tools/call":
                tool_call_name = str((_params_of(message) or {}).get("name") or "")

    upstream_request = client.build_request(
        request.method, url, headers=_forward_headers(request), content=body
    )

    # The connector gateway supplies a single already-validated JSON-RPC
    # element through body_override when it is assembling a mixed-project
    # batch. Buffer that response so the gateway can preserve one ordered
    # reply per element; ordinary direct calls retain the streaming path.
    if body_override is not None:
        try:
            upstream = await client.send(upstream_request)
        except httpx.HTTPError as exc:
            log.warning("worker connection failed mid-request: %s", exc)
            return Response(status_code=503, content="Worker connection failed; try again shortly",
                            headers={"Retry-After": "5"})
        return Response(upstream.content, status_code=upstream.status_code,
                        headers=_response_headers(upstream),
                        media_type=upstream.headers.get("content-type"))

    # tools/list: buffered (small) — normalized for legacy direct proxy callers.
    # Everything else: streamed. Either way, a worker
    # dying between the health check and the forward is a clean 503, not a
    # raw httpx exception 500ing out of the gateway.
    try:
        if is_tools_list:
            upstream = await client.send(upstream_request)
            return _rewrite_tools_list_response(upstream, readonly, project_name, documents_dir)
        if tool_call_name:
            # Results must be inspected to upgrade legacy text-only workers and
            # to keep structured/text halves synchronized.  This remains
            # bounded because tool responses already enforce their normal size
            # limits; long-lived GET/SSE channels are still streamed below.
            upstream = await client.send(upstream_request)
            body = upstream.content
            try:
                decoded = json.loads(body)
            except (TypeError, ValueError):
                decoded = None
            if isinstance(decoded, dict):
                decoded = _normalize_tool_rpc_payload(decoded, tool_call_name)
                return JSONResponse(decoded, status_code=upstream.status_code,
                                    headers=_response_headers(upstream))
            if upstream.headers.get("content-type", "").startswith("text/event-stream"):
                return _rewrite_buffered_response(
                    upstream, lambda item: _normalize_tool_rpc_payload(item, tool_call_name)
                )
            return Response(body, status_code=upstream.status_code,
                            headers=_response_headers(upstream),
                            media_type=upstream.headers.get("content-type"))
        upstream = await client.send(upstream_request, stream=True)
    except httpx.HTTPError as exc:
        log.warning("worker connection failed mid-request: %s", exc)
        return Response(status_code=503, content="Worker connection failed; try again shortly",
                        headers={"Retry-After": "5"})

    async def stream_body():
        try:
            async for chunk in upstream.aiter_raw():
                yield chunk
        except (httpx.StreamClosed, httpx.ReadError) as exc:
            # Client or worker went away mid-stream: routine, not an error (§4.4 #3)
            log.debug("stream ended early: %s", exc)
        finally:
            await upstream.aclose()

    return StreamingResponse(
        stream_body(),
        status_code=upstream.status_code,
        headers=_response_headers(upstream),
        media_type=upstream.headers.get("content-type"),
    )
