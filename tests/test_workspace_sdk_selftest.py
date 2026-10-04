from __future__ import annotations

import importlib.util
import json
from pathlib import Path

from cognita.proxy import (
    BRIDGE_TOOL_NAMES,
    PUBLIC_TOOL_NAMES,
    public_tool_catalog,
    workspace_tool_catalog,
)
from cognita.selftest import SELFTEST_TOOL_NAME
from cognita.workspace_selftest import (
    WORKSPACE_ONLY_SECTIONS,
    WORKSPACE_SELFTEST_TOOL_NAME,
    WORKSPACE_TOOL_NAMES,
    workspace_selftest_plan,
    workspace_selftest_tool_definition,
)


_RUNNER_SPEC = importlib.util.spec_from_file_location(
    "cognita_workspace_selftest_runner",
    Path(__file__).parents[1] / "scripts" / "run-selftest.py",
)
assert _RUNNER_SPEC and _RUNNER_SPEC.loader
_RUNNER = importlib.util.module_from_spec(_RUNNER_SPEC)
_RUNNER_SPEC.loader.exec_module(_RUNNER)


def test_workspace_v3_plan_is_catalog_bound_and_excludes_bridge():
    catalog = WORKSPACE_TOOL_NAMES + (WORKSPACE_SELFTEST_TOOL_NAME,)
    plan = workspace_selftest_plan(server_version="12.4.0", section="index", catalog=catalog)
    assert plan["status"] == "success"
    assert {row["id"] for row in plan["sections"]} == set(WORKSPACE_ONLY_SECTIONS)
    assert "W11" not in {row["id"] for row in plan["sections"]}


def test_public_workspace_plan_contains_only_caller_executable_steps():
    for bridge, catalog, plan_tool in (
        (False, WORKSPACE_TOOL_NAMES + (WORKSPACE_SELFTEST_TOOL_NAME,), WORKSPACE_SELFTEST_TOOL_NAME),
        (True, PUBLIC_TOOL_NAMES, SELFTEST_TOOL_NAME),
    ):
        plan = workspace_selftest_plan(
            server_version="12.8.0", bridge=bridge, catalog=catalog,
            plan_tool_name=plan_tool, project="Self-Test" if bridge else None,
            workspace_only=not bridge,
        )
        assert plan["status"] == "success"
        assert not any(f"## {section}:" in plan["plan"] for section in ("W3", "W10"))
        assert "ADMIN_" not in plan["plan"]
        assert "/api/workspaces/" not in plan["plan"]
        assert "/api/workspace-settings" not in plan["plan"]


def test_workspace_plan_fails_closed_on_missing_catalog_tool():
    plan = workspace_selftest_plan(server_version="12.4.0", catalog=WORKSPACE_TOOL_NAMES)
    assert plan["status"] == "blocked"
    assert plan["reason"] == "catalog_missing"
    assert WORKSPACE_SELFTEST_TOOL_NAME in plan["missing_tools"]


def test_workspace_plan_unknown_section_is_bounded():
    plan = workspace_selftest_plan(server_version="12.4.0", section="W99")
    assert plan["status"] == "error"
    assert plan["reason"] == "unknown_section"
    assert "W99" not in plan["valid_sections"]


def test_w13_wait_timeout_step_tolerates_one_broker_round_trip():
    # 12.18.2 (bug 3): the poll loop backing a timeout wake overshoots the
    # requested wait_ms by up to one broker round trip (observed 1071ms on a
    # requested 1000ms), so an exact "waited_ms is 1000" assertion is a false
    # failure, not a real one. The tolerance is now a range.
    plan = workspace_selftest_plan(server_version="12.18.2", section="W13")
    assert plan["status"] == "success"
    assert "waited_ms is between 1000 and 1500" in plan["plan"]
    assert "waited_ms is 1000." not in plan["plan"]


def test_workspace_runbook_has_all_sections_and_no_host_shell_instruction():
    text = Path("docs/WORKSPACE-SELF-TEST.md").read_text(encoding="utf-8")
    for section in ("W", *WORKSPACE_ONLY_SECTIONS):
        assert f"| {section} |" in text
    assert "host shell" in text
    assert "msb doctor" not in text


def test_workspace_selftest_schema_is_strict():
    schema = workspace_selftest_tool_definition()["inputSchema"]
    assert schema["additionalProperties"] is False
    assert schema["properties"]["section"]["type"] == "string"
    assert schema["properties"]["section"]["maxLength"] == 64


def test_workspace_edit_schema_accepts_plan_object_edits_in_both_catalogs():
    for catalog in (workspace_tool_catalog(), public_tool_catalog()):
        schema = next(item["inputSchema"] for item in catalog
                      if item["name"] == "workspace_edit_file")
        edits = schema["properties"]["edits"]
        assert edits["type"] == "array"
        assert edits["items"]["type"] == "object"
        assert edits["items"]["required"] == ["match", "replacement"]
        assert edits["items"]["additionalProperties"] is False
        assert set(edits["items"]["properties"]) == {"match", "replacement"}
        assert all(value["type"] == "string"
                   for value in edits["items"]["properties"].values())
    plan = workspace_selftest_plan(server_version="12.10.0", section="W2")["plan"]
    edit_call = next(line for line in plan.splitlines()
                     if line.startswith("CALL workspace_edit_file "))
    arguments = json.loads(edit_call.split(" ", 2)[2])
    assert arguments["edits"] == [{"match": "needle-one", "replacement": "needle-two"}]


def test_workspace_catalog_descriptions_explain_reuse_and_scratch_storage_by_surface():
    combined = {item["name"]: item for item in public_tool_catalog()
                if item["name"] in WORKSPACE_TOOL_NAMES}
    workspace_only = {item["name"]: item for item in workspace_tool_catalog()}
    common_phrases = (
        "reusable scratch workbed",
        "this credential across chats",
        "cleaned up for age or space",
        "availability is not guaranteed forever",
        "only copy of important data",
        "durable",
    )

    assert set(combined) == set(WORKSPACE_TOOL_NAMES)
    assert set(workspace_only) == set(WORKSPACE_TOOL_NAMES) | {WORKSPACE_SELFTEST_TOOL_NAME}
    for definitions in (combined.values(), workspace_only.values()):
        for definition in definitions:
            description = definition["description"]
            assert all(phrase in description for phrase in common_phrases), definition["name"]

    assert all("copy_from_workspace" in item["description"] for item in combined.values())
    assert all("another authorized durable destination" in item["description"]
               for item in workspace_only.values())
    assert all("copy_from_workspace" not in item["description"]
               for item in workspace_only.values())


def test_public_w4_proves_unpolled_completion_can_release_mutations():
    plan = workspace_selftest_plan(server_version="12.11.0", section="W4")["plan"]
    lines = plan.splitlines()
    unpolled = next(index for index, line in enumerate(lines)
                    if line.startswith("CALL workspace_start_job ") and '"<RUN>-unpolled"' in line)
    mutation = next(index for index, line in enumerate(lines)
                    if line.startswith("CALL workspace_make_directory ") and '"<RUN>-after-unpolled"' in line)
    assert unpolled < mutation
    assert not any(line.startswith("CALL workspace_get_job ") for line in lines[unpolled:mutation])
    assert "without polling <UNPOLLED_JOB_ID>" in lines[mutation + 1]


def test_workspace_job_plan_requires_base64_decode_for_plaintext_markers():
    plan = workspace_selftest_plan(server_version="12.17.0", section="W7")["plan"]
    assert "base64-decode stdout" in plan
    assert "begin\\n" in plan
    assert "has_more=false" in plan
    w4 = workspace_selftest_plan(server_version="12.17.0", section="W4")["plan"]
    assert "base64-decode stdout" in w4
    assert "shell-ok" in w4


def test_public_w8_uses_caller_reachable_negative_contracts():
    plan = workspace_selftest_plan(server_version="12.11.0", section="W8")["plan"]
    assert "may complete successfully with no matches or return reason=search_timeout" in plan
    assert "Traversal is rejected with invalid_arguments." in plan
    assert "path_unavailable" not in plan
    assert "unknown_field" not in plan
    assert "<BOUNDED_QUOTA_PLUS_ONE_BYTES>" not in plan
    assert "The dedicated Self-Test Workspace reports its configured quota" in plan
    assert "client schema or server" in plan


def test_w8_bounded_regex_probe_accepts_success_or_search_timeout_only():
    arguments = {"roots": ["/workspace/.cognita-self-test/run"], **_RUNNER._BOUNDED_REGEX_PROBE}
    assert _RUNNER._workspace_call_satisfies_plan(
        "W8", "workspace_search", {"status": "success", "data": {"matches": []}},
        negative=True, arguments=arguments,
    )
    assert _RUNNER._workspace_call_satisfies_plan(
        "W8", "workspace_search", {"status": "error", "reason": "search_timeout"},
        negative=True, arguments=arguments,
    )
    assert not _RUNNER._workspace_call_satisfies_plan(
        "W8", "workspace_search", {"status": "error", "reason": "invalid_arguments"},
        negative=True, arguments=arguments,
    )


def test_w8_regex_exception_does_not_relax_other_negative_calls():
    arguments = {"roots": ["/workspace/.cognita-self-test/run"], "mode": "regex", "pattern": "other"}
    assert _RUNNER._workspace_call_satisfies_plan(
        "W8", "workspace_search", {"status": "error", "reason": "invalid_arguments"},
        negative=True, arguments=arguments,
    )
    assert not _RUNNER._workspace_call_satisfies_plan(
        "W8", "workspace_search", {"status": "success"},
        negative=True, arguments=arguments,
    )


def test_public_w1_lists_only_workspace_info_fields():
    plan = workspace_selftest_plan(server_version="12.12.0", section="W1")['plan']
    assert "record workspace:null if no Workspace exists" in plan
    assert "If a Workspace existed before W2, require its ID to remain unchanged." in plan
    assert "Workspace ID/state, quota, measured usage, runtime generation, and effective network mode" in plan
    assert "authenticated principal" not in plan
    assert "toolbox digest" not in plan


def test_combined_w2_plan_covers_workspace_batch_success_path():
    plan = workspace_selftest_plan(
        server_version="12.17.0", section="W2", bridge=True,
        catalog=PUBLIC_TOOL_NAMES, plan_tool_name=SELFTEST_TOOL_NAME,
        project="Self-Test", workspace_only=False,
    )["plan"]
    batch_line = next(line for line in plan.splitlines() if line.startswith("CALL batch "))
    assert '"tool":"workspace_info"' in batch_line
    assert '"tool":"workspace_write_file"' in batch_line
    assert "workspace:null" in plan
    assert "without output_contract_violation" in plan


def test_combined_w2_runner_executes_workspace_batch_call():
    plan = workspace_selftest_plan(
        server_version="12.17.0", section="W2", bridge=True,
        catalog=PUBLIC_TOOL_NAMES, plan_tool_name=SELFTEST_TOOL_NAME,
        project="Self-Test", workspace_only=False,
    )["plan"]

    class Mcp:
        _id = 1

        def __init__(self):
            self.called = []

        def call(self, tool, arguments):
            self.called.append((tool, arguments))
            if tool == "workspace_write_file":
                return {"status": "success", "data": {"sha256": "a" * 64}}
            return {"status": "success"}

    mcp = Mcp()
    scorecard = _RUNNER.Scorecard()
    _RUNNER.run_workspace_plan(mcp, scorecard, plan)

    batch_calls = [arguments for tool, arguments in mcp.called if tool == "batch"]
    assert len(batch_calls) == 1
    assert [call["tool"] for call in batch_calls[0]["calls"]] == [
        "workspace_info", "workspace_write_file",
    ]


def test_w7_running_output_assertion_checks_bytes_offsets_and_state():
    arguments = {"job_id": "job-1", "stdout_offset": 0, "stderr_offset": 0, "max_bytes": 16}
    job = {
        "job_id": "job-1", "state": "running", "stdout": "YmVnaW4K",
        "stdout_bytes": 6, "stdout_next_offset": 6, "has_more": False,
    }
    assert _RUNNER._workspace_call_satisfies_plan(
        "W7", "workspace_get_job", {"status": "success", "job": job},
        negative=False, arguments=arguments, require_running_output=True,
    )
    for field, value in (
        ("state", "succeeded"), ("stdout", ""),
        ("stdout_next_offset", 5), ("has_more", True),
    ):
        rejected = {**job, field: value}
        assert not _RUNNER._workspace_call_satisfies_plan(
            "W7", "workspace_get_job", {"status": "success", "job": rejected},
            negative=False, arguments=arguments, require_running_output=True,
        )


def test_w7_runner_applies_running_output_assertion_before_terminal_poll():
    plan = (
        "## W7: running output\n"
        'CALL workspace_get_job {"job_id":"<CANCEL_JOB_ID>","stdout_offset":0,'
        '"stderr_offset":0,"max_bytes":16}'
    )

    class Mcp:
        _id = 1

        def call(self, _tool, _arguments):
            return {
                "status": "success",
                "job": {
                    "job_id": "job-1", "state": "running", "stdout": "",
                    "stdout_bytes": 0, "stdout_next_offset": 0, "has_more": False,
                },
            }

    scorecard = _RUNNER.Scorecard()
    _RUNNER.run_workspace_plan(Mcp(), scorecard, plan)
    assert any(line.startswith("  FAIL  W7 workspace_get_job") for line in scorecard.lines)


def test_public_w11_denied_policy_step_is_conditional():
    plan = workspace_selftest_plan(
        server_version="12.12.0", section="W11", bridge=True,
        catalog=PUBLIC_TOOL_NAMES, plan_tool_name=SELFTEST_TOOL_NAME,
        project="Self-Test", workspace_only=False,
    )["plan"]
    assert "If the caller already has a read-only or denied-policy connector" in plan
    assert "Admin" not in plan


def test_workspace_full_plan_contains_schema_valid_executable_calls_for_every_tool():
    plan = workspace_selftest_plan(server_version="12.4.0")["plan"]
    definitions = {item["name"]: item for item in workspace_tool_catalog()}
    seen = set()
    for line in plan.splitlines():
        if not line.startswith(("CALL ", "NEGATIVE_CALL ")):
            continue
        prefix, tool, encoded = line.split(" ", 2)
        arguments = json.loads(encoded)
        schema = definitions[tool]["inputSchema"]
        if prefix == "CALL":
            assert set(arguments) <= set(schema["properties"])
            assert set(schema.get("required", ())) <= set(arguments)
            seen.add(tool)
    assert seen == set(WORKSPACE_TOOL_NAMES) | {WORKSPACE_SELFTEST_TOOL_NAME}


def test_combined_workspace_plan_uses_current_plan_tool_and_conditionally_includes_bridge():
    plan = workspace_selftest_plan(
        server_version="12.4.0", bridge=True, catalog=PUBLIC_TOOL_NAMES,
        plan_tool_name=SELFTEST_TOOL_NAME, project="Self-Test", workspace_only=False,
    )
    assert plan["status"] == "success"
    assert "## W11:" in plan["plan"]
    assert '"destination":".cognita-self-test/<RUN>"' in plan["plan"]
    w11 = plan["plan"].split("## W11:", 1)[1].split("\n## ", 1)[0]
    # W11 round-trips the file it copied in itself (2026-10-01: ChatGPT ran W11 after cleaning up
    # W2's alpha.txt, which W11 used to copy back out while declaring only W1 as a prerequisite).
    assert '"paths":[".cognita-self-test/<RUN>/cognita-workspace-bridge-<RUN>.md"]' in w11
    assert "alpha.txt" not in w11
    assert w11.index("CALL workspace_make_directory") < w11.index("CALL copy_to_workspace")
    assert '"filepath":"cognita-workspace-bridge-out-<RUN>/cognita-workspace-bridge-<RUN>.md"' in w11
    w12 = plan["plan"].split("## W12:", 1)[1]
    assert '"filepath":"cognita-workspace-bridge-out-<RUN>/cognita-workspace-bridge-<RUN>.md"' in w12
    assert '"destination":"/workspace/.cognita-self-test/<RUN>"' not in w11
    assert 'CALL get_self_test_plan {"project":"Self-Test","section":"index"}' in plan["plan"]
    for tool in BRIDGE_TOOL_NAMES:
        assert f"CALL {tool} " in plan["plan"]
        assert '"project":"Self-Test"' in plan["plan"]
    definitions = {item["name"]: item for item in public_tool_catalog()}
    for line in plan["plan"].splitlines():
        if not line.startswith(("CALL ", "NEGATIVE_CALL ")):
            continue
        prefix, tool, encoded = line.split(" ", 2)
        arguments = json.loads(encoded)
        schema = definitions[tool]["inputSchema"]
        if prefix == "CALL":
            assert set(arguments) <= set(schema["properties"])
            assert set(schema.get("required", ())) <= set(arguments)
        if "project" in schema.get("required", ()):
            assert arguments["project"] == "Self-Test"


def test_runbook_documents_copyable_generated_call_format():
    text = Path("docs/WORKSPACE-SELF-TEST.md").read_text(encoding="utf-8")
    assert 'CALL workspace_generate_self_test {"section":"index"}' in text
    assert "CALL workspace_start_job" in text
    assert "Combined v1-v4 are retired" in text
