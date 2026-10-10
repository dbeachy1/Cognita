"""Focused conformance tests for the 10.1 structured result foundation."""

import json

import pytest
from jsonschema import Draft202012Validator, ValidationError

from cognita.result_contracts import (
    MUTATING_TOOLS,
    OUTPUT_SCHEMAS_BY_TOOL,
    PUBLIC_TOOL_NAMES,
    attach_output_schema,
    build_tool_result,
    validate_schema_registry,
    validate_structured_payload,
)
from cognita.books.schemas import ALL_ADDITIVE_TOOL_NAMES


def test_registry_covers_exactly_the_authoritative_public_names():
    assert set(OUTPUT_SCHEMAS_BY_TOOL) == set(PUBLIC_TOOL_NAMES)
    assert len(OUTPUT_SCHEMAS_BY_TOOL) == len(PUBLIC_TOOL_NAMES)
    validate_schema_registry()


def test_every_schema_is_valid_draft_2020_object_root_and_strict_success():
    for name, schema in OUTPUT_SCHEMAS_BY_TOOL.items():
        Draft202012Validator.check_schema(schema)
        assert schema["oneOf"]
        # 16.1.3: the root itself must say "type": "object" (MCP requires it of
        # an outputSchema). This test used to excuse the generated book
        # envelopes because each oneOf branch was an object; a client that
        # checks the catalog does not look that far and drops every tool.
        assert schema["type"] == "object", name
        assert all(branch.get("type") == "object" for branch in schema["oneOf"]), name
        success_branches = [
            branch for branch in schema["oneOf"]
            if branch.get("properties", {}).get("status", {}).get("const") != "error"
        ]
        assert success_branches, name
        assert all(branch["required"] != ["status"] for branch in success_branches), name
        assert all(branch["additionalProperties"] is False for branch in success_branches), name


def test_only_errors_and_user_metadata_extensions_are_open_objects():
    open_paths = []

    def visit(value, path=()):
        if isinstance(value, dict):
            if value.get("type") == "object" and value.get("additionalProperties") is True:
                open_paths.append((path, value))
            for key, child in value.items():
                visit(child, (*path, key))
        elif isinstance(value, list):
            for index, child in enumerate(value):
                visit(child, (*path, index))

    visit(OUTPUT_SCHEMAS_BY_TOOL)
    assert open_paths
    for path, schema in open_paths:
        is_error = schema.get("properties", {}).get("status", {}).get("const") == "error"
        # Normative book/storage errors carry a deliberately open JsonObject
        # under their explicit `details` field. Legacy errors themselves stay
        # open for additive diagnostics, and asset metadata owns `extensions`.
        assert is_error or path[-1] in {"extensions", "details"}, path


def test_attach_output_schema_deep_copies_without_mutating_input():
    original = {"name": "search_knowledge", "inputSchema": {"type": "object"}}
    attached = attach_output_schema(original)
    assert "outputSchema" not in original
    attached["outputSchema"]["oneOf"][0]["required"].append("local_only")
    assert "local_only" not in OUTPUT_SCHEMAS_BY_TOOL["search_knowledge"]["oneOf"][0]["required"]


def test_error_contracts_keep_legacy_diagnostics_but_book_errors_strict():
    additive = set(ALL_ADDITIVE_TOOL_NAMES) & set(OUTPUT_SCHEMAS_BY_TOOL)
    for name in set(OUTPUT_SCHEMAS_BY_TOOL) - additive:
        validate_structured_payload(name, {"status": "error", "reason": "not_found", "custom": {"safe": True}})
        with pytest.raises(ValidationError):
            validate_structured_payload(name, {"status": "success", "unexpected": True})
    for name in additive:
        strict_error = {
            "status": "error", "reason": "not_found", "message": "not found",
            "operation_outcome": "not_applied", "correlation_id": "test-correlation",
            "details": {"safe": True},
        }
        validate_structured_payload(name, strict_error)
        with pytest.raises(ValidationError):
            validate_structured_payload(name, {**strict_error, "custom": {"not": "allowed"}})
        with pytest.raises(ValidationError):
            validate_structured_payload(name, {"status": "success", "unexpected": True})


def test_get_documents_facts_include_index_metadata():
    validate_structured_payload("get_documents", {
        "status": "success", "result_key": "documents",
        "succeeded": 1, "failed": 0, "skipped": 0,
        "documents": [{
            "index": 0, "filepath": "rocm.md", "status": "success",
            "document": {
                "filepath": "rocm.md", "source": "/docs/rocm.md",
                "indexed": True, "include_content": False,
                "category": "general", "chunk_count": 3, "tier": "embedded",
                "on_disk": True, "size_bytes": 10, "mtime": None,
                "mtime_epoch": None, "bytes_sha256": None,
                "line_endings": None, "utf8_valid": True,
                "decode_error_bytes": 0, "content_is_lossy": False,
                "index_text_sanitized": False, "content_sha256": None,
                "error": "",
            },
        }],
    })


def test_helper_keeps_text_and_structured_content_deeply_equal():
    payload = {
        "status": "success", "query": "x", "hybrid_alpha": 0.3,
        "result_count": 0, "filtered_by_score": 0, "cache_hit_rate": 0.0,
        "result_key": "results", "results": [],
    }
    result = build_tool_result("search_knowledge", payload)
    assert result["structuredContent"] == payload
    assert json.loads(result["content"][0]["text"]) == result["structuredContent"]
    assert result["isError"] is False


def test_book_search_labels_are_optional_but_validate_when_admitted():
    """Book provenance labels survive the closed public search contracts."""
    search_hit = {
        "content": "current approved prose", "source": "C:/P/Chapters/1/chapter.docx",
        "filepath": "Chapters/1/chapter.docx", "filename": "chapter.docx",
        "category": "general", "chunk_index": 0, "tier": "embedded",
        "semantic_searchable": True, "score": 1.0, "raw_rrf_score": 0.1,
        "reranker_score": None, "semantic_rank": 1, "bm25_rank": 1,
        "search_method": "hybrid", "keywords": [], "routed_by": "none",
        "book_role": "chapter_working", "chapter_id": "ch1",
        "editorial_status": "approved", "summary_freshness": "not_applicable",
    }
    validate_structured_payload("search_knowledge", {
        "status": "success", "query": "prose", "hybrid_alpha": 0.3,
        "result_count": 1, "filtered_by_score": 0, "cache_hit_rate": 0.0,
        "result_key": "results", "results": [search_hit],
    })
    validate_structured_payload("search_similar", {
        "status": "success", "reference": "Chapters/1/chapter.docx", "count": 1,
        "result_key": "similar_documents", "results": [{
            "source": "C:/P/Project Files/ref.docx", "filepath": "Project Files/ref.docx",
            "filename": "ref.docx", "category": "general", "preview": "approved reference",
            "similarity": 0.9, "score": 0.9, "book_role": "reference",
            "chapter_id": None, "editorial_status": None,
            "summary_freshness": "not_applicable",
        }],
        "similar_documents": [{
            "source": "C:/P/Project Files/ref.docx", "filepath": "Project Files/ref.docx",
            "filename": "ref.docx", "category": "general", "preview": "approved reference",
            "similarity": 0.9, "score": 0.9, "book_role": "reference",
            "chapter_id": None, "editorial_status": None,
            "summary_freshness": "not_applicable",
        }],
    })


def test_image_extra_content_is_appended_without_entering_structured_content():
    payload = {"status": "success", "project": "p", "filepath": "a.png", "size": 3,
               "final_sha256": "a" * 64, "width": 1, "height": 1,
               "embedded_metadata_present": False, "provenance_state": "none",
               "cabx_chunk_count": 0, "catalog_drift": False}
    result = build_tool_result(
        "get_asset", payload,
        extra_content=[{"type": "image", "mimeType": "image/png", "data": "AAEC"}],
    )
    assert [block["type"] for block in result["content"]] == ["text", "image"]
    assert "data" not in result["structuredContent"]
    assert json.loads(result["content"][0]["text"]) == result["structuredContent"]
    for tool in ("get_asset_info", "get_asset"):
        with pytest.raises(ValidationError):
            validate_structured_payload(tool, {**payload, "score": 0.0, "search_method": "keyword"})


def test_asset_search_no_match_reason_is_a_valid_empty_success():
    validate_structured_payload(
        "search_assets",
        {"status": "success", "project": "p", "results": [], "reason": "no_matches"},
    )


def test_invalid_read_result_uses_bounded_internal_error_fallback():
    result = build_tool_result("search_knowledge", {"status": "success"})
    payload = result["structuredContent"]
    assert result["isError"] is True
    assert payload["status"] == "error"
    assert payload["reason"] == "internal_error"
    assert "results" not in payload
    validate_structured_payload("search_knowledge", payload)


def test_invalid_mutation_result_reports_unknown_outcome():
    result = build_tool_result("remove_asset", {"status": "success"})
    payload = result["structuredContent"]
    assert "remove_asset" in MUTATING_TOOLS
    assert result["isError"] is True
    assert payload["reason"] == "output_contract_violation"
    assert payload["operation_outcome"] == "unknown"
    validate_structured_payload("remove_asset", payload)
    assert json.loads(result["content"][0]["text"]) == payload


def test_invalid_image_result_drops_private_extra_content():
    result = build_tool_result(
        "get_asset", {"status": "success"},
        extra_content=[{"type": "image", "mimeType": "image/png", "data": "private"}],
    )
    assert result["isError"] is True
    assert result["structuredContent"]["reason"] == "internal_error"
    assert [block["type"] for block in result["content"]] == ["text"]


def test_remove_asset_schema_matches_the_complete_success_receipt():
    payload = {
        "status": "success", "project": "Self-Test", "filepath": "probe.png",
        "file_deleted": True, "catalog_removed": True, "ocr_removed": True,
        "deleted_size": 68, "deleted_sha256": "a" * 64,
        "backup_id": "20260916-120000", "idempotent_replay": False,
    }
    validate_structured_payload("remove_asset", payload)
    result = build_tool_result("remove_asset", payload, mutating=True)
    assert result["structuredContent"] == payload
    assert result["isError"] is False


def test_write_documents_accepts_the_engine_atomic_receipt_shape():
    receipt = {
        "filepath": "notes/one.md",
        "bytes_sha256": "a" * 64,
        "size_bytes": 12,
        "line_endings": "lf",
        "utf8_valid": True,
        "decode_error_bytes": 0,
        "content_is_lossy": False,
        "index_text_sanitized": False,
        "content_sha256": "b" * 64,
    }
    payload = {
        "status": "success",
        "documents_written": 2,
        "chunks_indexed": 3,
        "filepaths": ["notes/one.md", "reference.pdf"],
        "receipts": [
            receipt,
            {
                "filepath": "reference.pdf",
                "bytes_sha256": "c" * 64,
                "size_bytes": 4096,
                "line_endings": None,
                "utf8_valid": None,
                "decode_error_bytes": None,
                "content_is_lossy": False,
                "index_text_sanitized": False,
                "content_sha256": None,
            },
        ],
        "previous_backup_ids": {"notes/one.md": "20260916-120000"},
    }

    validate_structured_payload("write_documents", payload)
    result = build_tool_result("write_documents", payload, mutating=True)
    assert result["structuredContent"] == payload
    assert json.loads(result["content"][0]["text"]) == payload


def test_managed_book_write_indexing_fact_is_optional_and_strict():
    indexing = {
        "state": "blocked", "job_id": "managed-write-1",
        "error": {"code": "indexing_unavailable", "message": "Indexing is temporarily unavailable."},
    }
    payload = {
        "status": "success", "chunks_added": 0, "dedup_skipped": 0,
        "category": "general", "filepath": "Chapters/1/chapter.docx",
        "source": "C:/project/Chapters/1/chapter.docx", "bytes_sha256": "a" * 64,
        "size_bytes": 12, "line_endings": None, "utf8_valid": None,
        "decode_error_bytes": None, "content_is_lossy": False,
        "index_text_sanitized": False, "content_sha256": None,
        "tier": "embedded", "semantic_searchable": False, "indexing": indexing,
    }
    validate_structured_payload("add_document", payload)
    receipt = {
        "filepath": payload["filepath"], "bytes_sha256": payload["bytes_sha256"],
        "size_bytes": payload["size_bytes"], "line_endings": payload["line_endings"],
        "utf8_valid": payload["utf8_valid"], "decode_error_bytes": payload["decode_error_bytes"],
        "content_is_lossy": payload["content_is_lossy"],
        "index_text_sanitized": payload["index_text_sanitized"],
        "content_sha256": payload["content_sha256"], "indexing": indexing,
    }
    validate_structured_payload("write_documents", {
        "status": "success", "documents_written": 1, "chunks_indexed": 0,
        "filepaths": [payload["filepath"]], "receipts": [receipt],
    })
    with pytest.raises(ValidationError):
        validate_structured_payload("add_document", {
            **payload, "indexing": {**indexing, "unexpected": True},
        })


def test_restore_backup_accepts_update_and_recreate_receipts():
    common = {
        "status": "success",
        "filepath": "notes/one.md",
        "restored_from_backup": "20260916-120000",
        "new_content_sha256": "a" * 64,
        "context_diff": "",
        "source": "C:/project/notes/one.md",
        "content_sha256": "b" * 64,
        "bytes_sha256": "c" * 64,
        "size_bytes": 12,
        "line_endings": "lf",
        "utf8_valid": True,
        "decode_error_bytes": 0,
        "content_is_lossy": False,
        "index_text_sanitized": False,
        "tier": "embedded",
        "semantic_searchable": True,
    }
    update_result = {
        **common,
        "old_chunks_removed": 2,
        "new_chunks_added": 3,
        "dedup_skipped": 0,
        "previous_backup_id": "20260916-120100",
    }
    recreate_result = {
        **common,
        "line_endings": None,
        "utf8_valid": None,
        "decode_error_bytes": None,
        "content_sha256": None,
        "content_is_lossy": False,
        "index_text_sanitized": False,
        "tier": "registered",
        "semantic_searchable": False,
        "chunks_added": 0,
        "dedup_skipped": 0,
        "category": "general",
    }

    validate_structured_payload("restore_backup", update_result)
    validate_structured_payload("restore_backup", recreate_result)
    with pytest.raises(ValidationError):
        validate_structured_payload(
            "restore_backup", {**update_result, "restored_from_backup": None}
        )


def test_mutating_success_accepts_replay_marker_but_read_success_stays_strict():
    payload = {
        "status": "success",
        "filepath": "notes/one.md",
        "replacements": 1,
        "match_mode": "exact",
        "new_content_sha256": "a" * 64,
        "previous_backup_id": "20260916-120000",
        "old_chunks_removed": 2,
        "new_chunks_added": 3,
        "dedup_skipped": 0,
        "context_diff": "",
        "replayed": True,
    }

    validate_structured_payload("edit_document", payload)
    result = build_tool_result("edit_document", payload, mutating=True)
    assert result["structuredContent"] == payload
    assert json.loads(result["content"][0]["text"]) == payload
    with pytest.raises(ValidationError):
        validate_structured_payload(
            "search_knowledge",
            {"status": "success", "query": "x", "hybrid_alpha": 0.3,
             "result_count": 0, "filtered_by_score": 0, "cache_hit_rate": 0.0,
             "result_key": "results", "results": [], "replayed": True},
        )


def test_workspace_job_results_validate_after_the_job_key_fix():
    """DESIGN-12.18-WORKSPACE-NEXT-FEATURES SS1/SS3.1: before this fix,
    ``workspace_success`` had no "job" property, so every job tool result
    (which always carries a top-level ``job`` object) failed schema
    validation and was replaced by a contract-failure error -- a
    pre-existing bug, not something Release A introduced. This also covers
    A1's ``waited_ms``/``wake_reason`` top-level keys on a waited
    ``workspace_get_job`` response."""

    start_payload = {
        "status": "success",
        "workspace": {"workspace_id": "11111111-1111-4111-8111-111111111111", "state": "running"},
        "job": {"job_id": "22222222-2222-4222-8222-222222222222", "state": "running"},
    }
    validate_structured_payload("workspace_start_job", start_payload)

    waited_get_payload = {
        "status": "success",
        "workspace": {"workspace_id": "11111111-1111-4111-8111-111111111111", "state": "running"},
        "job": {"job_id": "22222222-2222-4222-8222-222222222222", "state": "succeeded"},
        "waited_ms": 1000,
        "wake_reason": "exited",
    }
    validate_structured_payload("workspace_get_job", waited_get_payload)

    replayed_write_payload = {
        "status": "success",
        "workspace": {"workspace_id": "11111111-1111-4111-8111-111111111111", "state": "running"},
        "data": {"path": "notes.txt"},
        "replayed": True,
    }
    validate_structured_payload("workspace_write_file", replayed_write_payload)

    with pytest.raises(ValidationError):
        validate_structured_payload("workspace_get_job", {**waited_get_payload, "unexpected_field": "x"})


def test_irrelevant_known_top_level_fields_are_rejected_per_tool():
    search = {
        "status": "no_results", "query": "x", "message": "none",
        "result_key": "results", "results": [],
    }
    projects = {
        "status": "success", "connector": {
            "id": "12345678-1234-4234-8234-123456789abc", "name": "c"
        }, "revision": 1, "projects": [],
    }
    with pytest.raises(ValidationError):
        validate_structured_payload("search_knowledge", {**search, "backup_id": "foreign"})
    with pytest.raises(ValidationError):
        validate_structured_payload("list_projects", {**projects, "query": "foreign"})


def test_unknown_nested_fields_are_rejected():
    projects = {
        "status": "success", "connector": {
            "id": "12345678-1234-4234-8234-123456789abc", "name": "c"
        }, "revision": 1, "projects": [{"name": "P", "access": "read"}],
    }
    validate_structured_payload("list_projects", projects)
    projects["projects"][0]["filepath"] = "irrelevant"
    with pytest.raises(ValidationError):
        validate_structured_payload("list_projects", projects)


def test_private_ocr_snapshot_fields_are_not_public_success_fields():
    payload = {
        "status": "success", "outcome": "no_text", "project": "P",
        "filepath": "a.png", "sha256": "a" * 64, "width": 1, "height": 1,
        "text": "", "regions": [],
        "engine": {"name": "paddle", "version": "1", "model_fingerprint": "b" * 64,
                   "pipeline_version": 1, "device": "cpu", "backend": "onnx"},
        "languages": ["en"], "cache_hit": False, "searchable": False,
        "warnings": [], "duration_ms": 1,
        "limits": {"max_png_bytes": 1, "max_pixels": 1, "max_dimension": 1,
                   "max_regions": 1, "max_result_bytes": 1},
    }
    validate_structured_payload("ocr_asset", payload)
    with pytest.raises(ValidationError):
        validate_structured_payload(
            "ocr_asset", {**payload, "source_snapshot": {"filepath": "private"}}
        )
    payload["engine"]["unknown_backend_fact"] = True
    with pytest.raises(ValidationError):
        validate_structured_payload("ocr_asset", payload)


def test_broker_relayed_error_with_null_correlation_id_validates():
    """12.18.1: broker errors relay ``correlation_id: null``; the real reason must reach the client."""
    from cognita.result_contracts import build_tool_result

    payload = {
        "status": "error", "reason": "runtime_unavailable",
        "message": "Workspace runtime rejected the operation",
        "broker_code": "runtime_failure", "broker_stage": None,
        "correlation_id": None, "retryable": True,
    }
    result = build_tool_result("workspace_start_job", payload, is_error=True)
    assert result["isError"] is True
    assert result["structuredContent"]["reason"] == "runtime_unavailable"


def test_legacy_error_normalization_preserves_diagnostics_and_is_error_flag():
    payload = {
        "status": "error", "reason": "not_found", "message": "missing",
        "diagnostic": {"path": "safe"},
    }
    result = build_tool_result("get_document", payload, is_error=False)
    assert payload == {
        "status": "error", "reason": "not_found", "message": "missing",
        "diagnostic": {"path": "safe"},
    }
    assert result["structuredContent"] == {
        **payload, "error_code": "INVALID_ARGUMENT",
    }
    assert result["isError"] is False
    assert json.loads(result["content"][0]["text"]) == result["structuredContent"]

    explicit = build_tool_result(
        "get_document",
        {"status": "error", "error_code": "CUSTOM_CODE", "reason": "invalid"},
        is_error=True,
    )
    assert explicit["structuredContent"]["error_code"] == "CUSTOM_CODE"


def test_invalid_legacy_fallback_gets_error_code_but_book_error_stays_strict():
    fallback = build_tool_result("search_knowledge", {"status": "success"})
    assert fallback["structuredContent"]["error_code"] == "INVALID_ARGUMENT"
    assert json.loads(fallback["content"][0]["text"]) == fallback["structuredContent"]

    book_error = {
        "status": "error", "reason": "not_found", "message": "not found",
        "operation_outcome": "not_applied", "correlation_id": "test-correlation",
        "details": {"safe": True},
    }
    result = build_tool_result("audiobook_get_book", book_error, is_error=True)
    assert result["structuredContent"] == book_error
    assert "error_code" not in result["structuredContent"]
    validate_structured_payload("audiobook_get_book", result["structuredContent"])
