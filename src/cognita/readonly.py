"""Read-only enforcement for the remote MCP endpoint (DESIGN.md §6).

The public /mcp surface exposes only read tools; all mutation stays local
(admin API / stdio mode).
"""

from __future__ import annotations

READONLY_TOOLS: frozenset[str] = frozenset(
    {
        "search_knowledge",
        "get_document",
        "get_documents",
        "batch",  # The envelope is allowed; each child is authorized independently.
        "search_similar",
        "list_documents",
        "list_categories",
        "get_index_stats",
        "get_reindex_status",
        "evaluate_retrieval",
        "find_literal",  # exhaustive literal/regex corpus search (4.5); read-only by construction
        "read_document",  # gateway-provided ranged/section read (2.3), not an engine tool
        "list_backups",  # gateway-provided backup listing (2.4), not an engine tool
        "diff_backup",  # gateway-provided backup-vs-current diff (2.8), not an engine tool
        "get_self_test_plan",  # gateway-provided versioned test protocol (2.10)
        "search_assets",
        "list_assets",
        "get_asset_info",
        "get_asset",
        "ocr_asset",
        "audiobook_inspect_chapter", "audiobook_get_chapter", "audiobook_find_chunk",
        "list_project_files", "read_project_file",
    }
)

MUTATING_TOOLS: frozenset[str] = frozenset(
    {
        "add_document",
        "update_document",
        "remove_document",
        "remove_documents",
        "move_document",  # rename/relocate an indexed doc (4.1); backed up like the other writes
        "add_from_url",
        "reindex_documents",
        "edit_document",  # gateway-provided (DESIGN-2.0-edit-document.md), not an engine tool
        "edit_document_batch",  # gateway-provided; atomic multi-edit (2.1)
        "restore_backup",  # gateway-provided; writes via update_document (2.4)
        "insert_in_document",  # gateway-provided; section-aware insert (2.5)
        # 5.0 directory tools. Engine-served like add/update, but deliberately
        # NOT in proxy._BACKUP_TOOLS: they act on many files, so the gateway's
        # single-`filepath` backup hook cannot describe them. Each takes its own
        # backups per file inside the engine, still under the project write lock.
        "copy_document",
        "copy_directory",
        "remove_directory",
        # 🔴 6.1.0. Missing this is not cosmetic: this set gates the ENGINE's
        # per-project write lock (`engine_local._dispatch`), so an omitted
        # mutating tool runs unserialized against a full rebuild and against
        # every other write — silently, because nothing errors. It also drives
        # the read-only refusal and the tools/list annotation. A new mutating
        # tool belongs here in the same commit that adds it.
        "write_documents",
        "put_asset",
        "update_asset_metadata",
        "reindex_assets",
        "remove_asset",
        "audiobook_prepare_chapter", "set_folder_indexing",
    }
)


def is_tool_allowed_remote(tool_name: str) -> bool:
    """Unknown tools are denied by default (allow-list, not deny-list)."""
    return tool_name in READONLY_TOOLS
