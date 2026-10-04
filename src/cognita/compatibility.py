"""Retired-contract compatibility seam.

The current public catalog is owned by :mod:`cognita.proxy`, while
:mod:`cognita.connectors` owns the current-only generation predicate.  Earlier
catalogs are retained only as historical migration constants; runtime callers
must not use them to authorize, route, or advertise a connector.
"""

from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Iterable, Mapping
from typing import Any

from .connectors import is_supported_contract_version


# Cognita v3 is the frozen pre-Workspace surface.  Keep the value explicit:
# compatibility is a code boundary, not ``current - 1`` hidden at call sites.
COMPATIBILITY_CONTRACT_VERSION = 3

# 10.0's accepted public catalog, in wire order.  The tuple is intentionally
# independent of the current catalog so an accidental current-only tool cannot
# become callable on v2 merely because it was appended to engine definitions.
V2_PUBLIC_TOOL_NAMES: tuple[str, ...] = (
    "search_knowledge", "get_document", "search_similar", "get_documents", "list_documents",
    "list_categories", "get_index_stats", "get_reindex_status", "evaluate_retrieval",
    "add_document", "update_document", "write_documents", "remove_document", "remove_documents",
    "move_document", "add_from_url", "reindex_documents", "find_literal", "copy_document",
    "copy_directory", "remove_directory", "put_asset", "update_asset_metadata", "search_assets",
    "list_assets", "get_asset_info", "get_asset", "reindex_assets", "ocr_asset", "read_document",
    "list_backups", "diff_backup", "get_self_test_plan", "edit_document", "edit_document_batch",
    "insert_in_document", "restore_backup", "batch", "list_projects",
)

V2_PUBLIC_TOOL_COUNT = len(V2_PUBLIC_TOOL_NAMES)

# v3 is the 11.4 Knowledge catalog.  It is intentionally independent from the
# current v5 catalog so adding a Workspace/bridge tool cannot leak into a
# retired generation.
V3_PUBLIC_TOOL_NAMES: tuple[str, ...] = (
    "search_knowledge", "get_document", "search_similar", "get_documents", "list_documents",
    "list_categories", "get_index_stats", "get_reindex_status", "evaluate_retrieval",
    "add_document", "update_document", "write_documents", "remove_document", "remove_documents",
    "move_document", "add_from_url", "reindex_documents", "find_literal", "copy_document",
    "copy_directory", "remove_directory", "put_asset", "update_asset_metadata", "search_assets",
    "list_assets", "get_asset_info", "get_asset", "reindex_assets", "ocr_asset", "remove_asset",
    "read_document", "list_backups", "diff_backup", "get_self_test_plan", "edit_document",
    "edit_document_batch", "insert_in_document", "restore_backup", "batch", "list_projects",
)
V3_PUBLIC_TOOL_COUNT = len(V3_PUBLIC_TOOL_NAMES)

# 10.1's persistent self-test fixtures changed this existing tool description.
# v2 discovery must keep the accepted 10.0 wording even though calls execute the
# current plan implementation.
V2_SELFTEST_TOOL_DESCRIPTION = (
    "Return this server's current self-test plan — a versioned, ordered "
    "checklist exercising this connector's tool surface. Call this when the "
    "user asks to run the Cognita self-test, then EXECUTE the returned steps "
    "exactly, in order, and report the scorecard it asks for. The plan is "
    "self-contained for documents (it creates and deletes its own test files); "
    "asset checks reuse two reserved 1x1 PNG canaries because the asset API has "
    "no delete operation. First call list_projects, choose the exact project "
    "name you intend to test, and include that project argument on every "
    "project-scoped call in this plan. It is updated server-side whenever "
    "functionality changes, so it is always current. It ends "
    "with two OPTIONAL sections a connector client cannot run — raw HTTPS transport "
    "checks and server-shell checks — which are reported SKIPPED, not failed. "
    "Two tools are deliberately NOT covered and their absence is not a gap: "
    "add_from_url (needs a stable external URL) and get_self_test_plan itself "
    "(calling it IS this step). reindex_documents and get_reindex_status appear "
    "ONLY in the optional server-shell section, because a full reindex of a live "
    "knowledge base is too expensive to run unconditionally. Read-only; the "
    "plan's steps do the testing. Optional section selects the stable index, "
    "a group, or one exact test while retaining shared safety/setup/cleanup "
    "instructions; omit it for the full plan."
)

# SHA-256 of the accepted 10.0/v2 catalog's canonical JSON.  The catalog can be
# projected from current definitions only while every retained definition is
# byte-for-byte equivalent on the wire; this guard turns an input-schema,
# annotation, description, or ordering change into a fail-closed error instead
# of silently leaking a newer contract through v2.
V2_PUBLIC_CATALOG_SHA256 = "9f4952a8328eaf7813d5d0c6edbd03aacfed7563bba8119dbd0b04a0d600d726"


def v2_tool_available(tool_name: str, contract_version: int) -> bool:
    """Whether a name is declared by the frozen v2 catalog.

    The generation argument is required at this boundary so callers cannot
    accidentally use the v2 name set to authorize a current-only request.
    """
    return False


def v3_tool_available(tool_name: str, contract_version: int) -> bool:
    return False


def tool_available_for_contract(
    tool_name: str,
    contract_version: int,
    current_tool_names: Iterable[str],
    current_version: int,
) -> bool:
    """Check availability only on the sole current generation."""
    return (
        is_supported_contract_version(contract_version, current_version)
        and tool_name in set(current_tool_names)
    )


def frozen_v2_catalog(current_catalog: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Project the reviewed 10.0 catalog from current code-owned definitions.

    10.1 added ``remove_asset`` and advertised ``outputSchema``.  Both are
    removed here; every other definition is deep-copied so a v2 response cannot
    mutate the v3 catalog or share nested input-schema state with it.  The
    explicit name/order and full-schema fingerprint checks turn any unreviewed
    future catalog change into a startup/test failure instead of silent v2
    drift. The accepted 10.0 commit is authoritative; its older design note's
    fingerprint predates the final 39-tool release catalog.
    """
    raise RuntimeError("retired connector contract v2 is not a callable catalog")
    result: list[dict[str, Any]] = []
    for definition in current_catalog:
        if definition.get("name") == "remove_asset":
            continue
        projected = copy.deepcopy(dict(definition))
        projected.pop("outputSchema", None)
        if projected.get("name") == "get_self_test_plan":
            projected["description"] = V2_SELFTEST_TOOL_DESCRIPTION
        result.append(projected)
    names = tuple(item.get("name") for item in result)
    if names != V2_PUBLIC_TOOL_NAMES:
        raise RuntimeError(
            "Cognita v2 compatibility catalog drift: "
            f"expected {V2_PUBLIC_TOOL_COUNT} names, got {len(names)}"
        )
    fingerprint = hashlib.sha256(
        json.dumps(result, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    if fingerprint != V2_PUBLIC_CATALOG_SHA256:
        raise RuntimeError(
            "Cognita v2 compatibility catalog schema drift: "
            f"expected {V2_PUBLIC_CATALOG_SHA256}, got {fingerprint}"
        )
    return result


def frozen_v3_catalog(current_catalog: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Return the immutable 11.4 Knowledge catalog for combined v3.

    The v5 catalog is assembled from this same Knowledge implementation plus
    Workspace and bridge adapters. Selecting by an explicit allowlist prevents
    those additions (and any future v5-only fields) from appearing on v3.
    """
    raise RuntimeError("retired connector contract v3 is not a callable catalog")
    by_name = {item.get("name"): item for item in current_catalog}
    missing = [name for name in V3_PUBLIC_TOOL_NAMES if name not in by_name]
    if missing:
        raise RuntimeError(f"Cognita v3 compatibility catalog missing tools: {missing!r}")
    return [copy.deepcopy(dict(by_name[name])) for name in V3_PUBLIC_TOOL_NAMES]


def upgrade_required_payload(tool_name: str, contract_version: int) -> dict[str, str]:
    """Build the bounded tool-level error for an operation absent from v2."""
    return {
        "status": "error",
        "reason": "upgrade_required",
        "message": (
            f"Tool {tool_name!r} is not available on connector contract v{contract_version}; "
            "reconnect using the current connector URL."
        ),
    }
