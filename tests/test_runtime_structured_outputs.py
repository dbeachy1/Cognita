"""Focused 10.1 runtime wiring checks (no database or model dependencies)."""

import json

import pytest
from jsonschema import ValidationError

from cognita.assets.wire import image_result
from cognita.engine_local import ENGINE_TOOL_DEFS
from cognita.proxy import _normalize_tool_rpc_payload, public_tool_catalog, workspace_tool_catalog
from cognita.result_contracts import (
    ADAPTER_OUTPUT_SCHEMAS_BY_TOOL,
    OUTPUT_SCHEMAS_BY_TOOL,
    build_tool_result,
    validate_structured_payload,
)


def test_engine_and_public_catalogs_advertise_the_same_output_schemas():
    engine = {item["name"]: item for item in ENGINE_TOOL_DEFS}
    public = {item["name"]: item for item in public_tool_catalog()}
    assert set(OUTPUT_SCHEMAS_BY_TOOL) <= set(public)
    assert all(item.get("outputSchema") == OUTPUT_SCHEMAS_BY_TOOL[name]
               for name, item in engine.items())
    assert all(public[name].get("outputSchema") == schema
               for name, schema in OUTPUT_SCHEMAS_BY_TOOL.items())


def test_gateway_upgrades_legacy_text_result_and_prefers_structured_content():
    original = {"status": "success", "query": "gpu", "result_key": "results", "results": []}
    message = {"jsonrpc": "2.0", "id": 1, "result": {
        "content": [{"type": "text", "text": json.dumps(original)}], "isError": False,
    }}
    normalized = _normalize_tool_rpc_payload(message, "search_knowledge")
    result = normalized["result"]
    assert result["structuredContent"] == original
    assert json.loads(result["content"][0]["text"]) == original

    preferred = {"status": "error", "reason": "stale_file"}
    message["result"]["structuredContent"] = preferred
    message["result"]["content"][0]["text"] = json.dumps(original)
    normalized = _normalize_tool_rpc_payload(message, "search_knowledge")
    expected = {**preferred, "error_code": "INVALID_ARGUMENT"}
    assert preferred == {"status": "error", "reason": "stale_file"}
    assert normalized["result"]["structuredContent"] == expected
    assert json.loads(normalized["result"]["content"][0]["text"]) == expected
    assert normalized["result"]["isError"] is False


def test_gateway_preserves_an_image_block_when_content_lacks_leading_text():
    facts = {"status": "success", "project": "P", "filepath": "a.png",
             "size": 3, "final_sha256": "a" * 64, "width": 1, "height": 1,
             "embedded_metadata_present": False, "provenance_state": "none",
             "cabx_chunk_count": 0, "catalog_drift": False}
    message = {"jsonrpc": "2.0", "id": 1, "result": {
        "structuredContent": facts,
        "content": [{"type": "image", "mimeType": "image/png", "data": "AAEC"}],
        "isError": False,
    }}
    result = _normalize_tool_rpc_payload(message, "get_asset")["result"]
    assert [block["type"] for block in result["content"]] == ["text", "image"]
    assert result["content"][1]["data"] == "AAEC"


def test_image_result_keeps_text_then_image_and_separates_bytes():
    facts = {"status": "success", "project": "P", "filepath": "a.png"}
    result = image_result(facts, b"png-bytes")
    assert result["structuredContent"] == facts
    assert result["content"][0]["type"] == "text"
    assert result["content"][1]["type"] == "image"
    assert "png-bytes" not in result["content"][0]["text"]
    assert "data" not in result["structuredContent"]


def test_workspace_and_bridge_catalogs_publish_output_schemas():
    catalog = {item["name"]: item for item in public_tool_catalog()}
    for name in ADAPTER_OUTPUT_SCHEMAS_BY_TOOL:
        if name == "workspace_generate_self_test":
            continue
        assert catalog[name]["outputSchema"] == ADAPTER_OUTPUT_SCHEMAS_BY_TOOL[name]
    workspace_catalog = {item["name"]: item for item in workspace_tool_catalog()}
    assert workspace_catalog["workspace_generate_self_test"]["outputSchema"] == ADAPTER_OUTPUT_SCHEMAS_BY_TOOL["workspace_generate_self_test"]
    assert catalog["copy_to_workspace"]["inputSchema"]["properties"]["paths"]["maxItems"] == 10000
    assert catalog["copy_from_workspace"]["inputSchema"]["properties"]["paths"]["maxItems"] == 10000


def test_workspace_and_bridge_success_children_pass_the_batch_contract():
    workspace_info = build_tool_result(
        "workspace_info", {"status": "success", "workspace": None}
    )
    workspace_write = build_tool_result(
        "workspace_write_file",
        {"status": "success", "workspace": {}, "data": {"path": "batch.txt"}},
    )
    workspace_write_replay = build_tool_result(
        "workspace_write_file",
        {"status": "success", "workspace": {}, "data": {"path": "batch.txt"},
         "idempotent_replay": True},
    )
    workspace_jobs = {
        name: build_tool_result(
            name,
            {"status": "success", "workspace": {},
             "job": {"job_id": "job-1", "state": state},
             **({"idempotent_replay": True}
                if name in {"workspace_start_job", "workspace_cancel_job"} else {})},
        )
        for name, state in (
            ("workspace_start_job", "running"),
            ("workspace_get_job", "succeeded"),
            ("workspace_cancel_job", "canceled"),
        )
    }
    bridge = build_tool_result(
        "copy_to_workspace",
        {
            "status": "success", "transfer_id": "12345678-1234-4234-8234-123456789abc",
            "direction": "to_workspace", "project": "Self-Test", "file_count": 0,
            "bytes": 0, "manifest": [], "committed": [], "skipped": [],
        },
    )
    bridge_replay = build_tool_result(
        "copy_to_workspace",
        {
            "status": "success", "transfer_id": "12345678-1234-4234-8234-123456789abc",
            "direction": "to_workspace", "project": "Self-Test", "file_count": 0,
            "bytes": 0, "manifest": [], "committed": [], "skipped": [],
            "idempotent_replay": True,
        },
    )
    for tool, result in (("workspace_info", workspace_info),
                         ("workspace_write_file", workspace_write),
                         ("workspace_write_file", workspace_write_replay),
                         ("copy_to_workspace", bridge),
                         ("copy_to_workspace", bridge_replay)):
        validate_structured_payload(tool, result["structuredContent"])
        assert result["isError"] is False
    for tool, result in workspace_jobs.items():
        validate_structured_payload(tool, result["structuredContent"])
        assert result["isError"] is False
    batch = build_tool_result(
        "batch",
        {
            "status": "success", "result_key": "results", "on_error": "stop",
            "results": [
                {"index": 0, "tool": "workspace_info", "status": "success", "result": workspace_info},
                {"index": 1, "tool": "workspace_write_file", "status": "success", "result": workspace_write},
                {"index": 2, "tool": "copy_to_workspace", "status": "success", "result": bridge},
            ],
            "succeeded": 3, "failed": 0, "skipped": 0, "omitted": 0,
        },
    )
    assert batch["isError"] is False
    assert batch["structuredContent"]["status"] == "success"


def test_adapter_success_variants_remain_exact_at_the_boundary():
    with pytest.raises(ValidationError):
        validate_structured_payload(
            "workspace_info", {"status": "success", "workspace": None, "data": {}},
        )
    with pytest.raises(ValidationError):
        validate_structured_payload(
            "workspace_write_file", {"status": "success", "workspace": {}},
        )
    with pytest.raises(ValidationError):
        validate_structured_payload(
            "workspace_start_job", {"status": "success", "workspace": {}, "data": {}},
        )
