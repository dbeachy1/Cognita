"""16.1.3, Part A: every tool result matches the outputSchema the server advertises.

MCP 2025-06-18 and 2025-11-25 (Tools, Output Schema): a server that advertises an
outputSchema MUST return structured results that conform to it. The official
TypeScript SDK client validates ``structuredContent`` against the tool's schema
even when ``isError`` is true and throws on a mismatch, so the model never saw the
real reason for a refusal. These tests do what that client does: take each tool's
schema from the real ``tools/list`` and validate the real result against it.
"""

from __future__ import annotations

import json
import re
import sys
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from jsonschema import Draft202012Validator

from cognita import __version__
from cognita.auth_policy import AuthenticationPolicyStore
from cognita.books.schemas import ALL_ADDITIVE_MUTATING_TOOLS, ALL_ADDITIVE_TOOL_NAMES
from cognita.config import CognitaConfig
from cognita.connectors import PUBLIC_CONTRACT_VERSION, ConnectorPolicyError, ConnectorStore
from cognita.gateway import create_gateway_app
from cognita.proxy import public_tool_catalog
from cognita.registry import Project, Registry
from cognita.result_contracts import (
    _fallback, build_tool_result, is_known_tool, refusal_payload,
    validate_structured_payload,
)
from cognita.result_contracts import PUBLIC_TOOL_NAMES as CORE_TOOL_NAMES
from cognita.selftest import build_self_test_plan, self_test_section_catalog
from cognita.workspace_selftest import (
    KNOWLEDGE_BRIDGE_PREFIXES, WORKSPACE_SECTIONS, WORKSPACE_TOOL_NAMES,
)
from engine_fakes import FakeEngineHost

COMBINED_TOOLS = tuple(tool["name"] for tool in public_tool_catalog(PUBLIC_CONTRACT_VERSION))
STRICT_MUTATING = "audiobook_build"
STRICT_READ = "audiobook_get_book"
LEGACY_MUTATING = "add_document"
LEGACY_READ = "get_document"
CORRELATION_ID = re.compile(r"^[0-9a-f]{32}$")

# Every refusal the gateway and proxy build themselves, as (reason, message).
REFUSAL_KINDS = (
    ("project_unavailable", "The requested project is unavailable through this connector."),
    ("project_unavailable", "No projects are configured for this key."),
    ("read_only", "Tool 'x' is not available: this knowledge base is read-only."),
    ("policy_unavailable", "Connector policy is unavailable; try again later."),
    ("invalid", "project must be a nonempty exact string"),
    ("invalid", "operation_id must be a string; got int."),
    ("upgrade_required", "Tool 'x' is not available under this connector generation."),
)


# ---------------------------------------------------------------- helper level


def test_the_combined_catalog_is_covered():
    assert set(ALL_ADDITIVE_TOOL_NAMES) <= set(COMBINED_TOOLS)
    assert len(ALL_ADDITIVE_TOOL_NAMES) == 16
    assert all(is_known_tool(name) for name in COMBINED_TOOLS)


@pytest.mark.parametrize("tool", COMBINED_TOOLS)
def test_every_refusal_kind_validates_against_that_tools_schema(tool):
    for reason, message in REFUSAL_KINDS:
        payload = refusal_payload(tool, reason, message)
        result = build_tool_result(tool, payload, is_error=True)
        structured = result["structuredContent"]
        # Not replaced by the contract-violation containment error.
        assert structured["reason"] == reason, (tool, reason, structured)
        assert structured["message"] == message and structured["status"] == "error"
        assert result["isError"] is True
        validate_structured_payload(tool, structured)
        assert json.loads(result["content"][0]["text"]) == structured
        if tool in ALL_ADDITIVE_TOOL_NAMES:
            assert structured["operation_outcome"] == "not_applied"
            assert CORRELATION_ID.match(structured["correlation_id"])
            assert "error_code" not in structured
        else:
            assert structured == {
                "status": "error", "reason": reason, "message": message,
                "error_code": "INVALID_ARGUMENT",
            }


def test_strict_refusal_carries_extra_fields_in_details_and_is_unknown_safe():
    envelope = refusal_payload(STRICT_READ, "invalid", "m", executed=0)
    assert envelope["details"] == {"executed": 0}
    validate_structured_payload(STRICT_READ, envelope)
    assert refusal_payload(LEGACY_READ, "invalid", "m", executed=0) == {
        "status": "error", "reason": "invalid", "message": "m", "executed": 0,
    }
    # A name with no registered schema gets the legacy shape and is "not known".
    assert refusal_payload("no_such_tool", "invalid", "m") == {
        "status": "error", "reason": "invalid", "message": "m",
    }
    assert not is_known_tool("no_such_tool") and not is_known_tool(None) and not is_known_tool({})


@pytest.mark.parametrize("tool", COMBINED_TOOLS)
@pytest.mark.parametrize("mutating", [True, False])
def test_the_contract_violation_fallback_validates_for_every_tool(tool, mutating):
    fallback = _fallback(tool, mutating=mutating, correlation_id="a" * 32)
    validate_structured_payload(tool, fallback)
    if tool in ALL_ADDITIVE_TOOL_NAMES:
        if mutating:
            assert fallback["reason"] == "output_contract_violation"
            assert fallback["operation_outcome"] == "outcome_unknown"
        else:
            assert fallback["reason"] == "internal_error"
            assert fallback["operation_outcome"] == "not_applied"
        assert fallback["correlation_id"] == "a" * 32 and fallback["message"]
        assert set(fallback) == {"status", "reason", "message", "operation_outcome", "correlation_id"}


@pytest.mark.parametrize("tool", COMBINED_TOOLS)
def test_an_invalid_result_is_replaced_by_a_valid_one_and_mutating_is_decided_by_the_tool(tool):
    result = build_tool_result(tool, {"status": "success", "not_in_any_schema": object.__name__})
    structured = result["structuredContent"]
    validate_structured_payload(tool, structured)
    assert result["isError"] is True and structured["status"] == "error"
    if tool in ALL_ADDITIVE_TOOL_NAMES:
        expected = "output_contract_violation" if tool in ALL_ADDITIVE_MUTATING_TOOLS else "internal_error"
        assert structured["reason"] == expected


# ---------------------------------------------------------------- gateway level


def _build(tmp_path, *, projects=("RW",), default_access="write", workspace_enabled=True,
           transfer="allow", workspace_service=None, name="Conformance"):
    registry = Registry(tmp_path / "registry.yaml")
    for project in projects:
        docs = tmp_path / f"docs-{project}"
        docs.mkdir()
        registry.add(Project(name=project, documents_dir=docs, data_dir=tmp_path / f"data-{project}"))
    store = ConnectorStore(tmp_path / "connectors.yaml")
    config = store.create(
        expected_revision=0, name=name, project_names=list(projects),
        default_access=default_access, workspace_enabled=workspace_enabled,
        default_workspace_transfer=transfer,
    )
    auth = AuthenticationPolicyStore(tmp_path / "authentication.yaml", project_names=list(projects))
    token = auth.mutate_global(
        expected_revision=0, oauth_enabled=False, static_key_action="generate",
    )["generated_key"]
    app = create_gateway_app(
        CognitaConfig(registry_path=registry.path, connectors_path=store.path, data_root=tmp_path),
        registry, engine=FakeEngineHost(FastAPI()), connector_store=store,
        authentication_store=auth, workspace_service=workspace_service,
    )
    return SimpleNamespace(
        app=app, store=store, slug=config.connectors[0].slug, token=token,
        path=f"/mcp/connectors/{config.connectors[0].slug}/mcp/v{PUBLIC_CONTRACT_VERSION}",
    )


async def _post(env, body):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=env.app), base_url="http://fixture",
    ) as client:
        response = await client.post(
            env.path, json=body, headers={"Authorization": f"Bearer {env.token}"},
        )
    assert response.status_code == 200, response.text
    return response.json()


def _tools_call(name, arguments=..., msg_id=7):
    params = {"name": name}
    if arguments is not ...:
        params["arguments"] = arguments
    return {"jsonrpc": "2.0", "id": msg_id, "method": "tools/call", "params": params}


async def _schemas(env):
    listed = await _post(env, {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
    return {tool["name"]: tool["outputSchema"] for tool in listed["result"]["tools"]}


async def _call(env, name, arguments=...):
    return await _post(env, _tools_call(name, arguments))


def _assert_refusal(reply, schema, tool, *, reason, message=None):
    """The result a validating client sees: schema-valid, isError, same reason and text."""
    assert "error" not in reply, reply
    result = reply["result"]
    structured = result["structuredContent"]
    Draft202012Validator(schema).validate(structured)
    assert result["isError"] is True
    assert structured["status"] == "error" and structured["reason"] == reason, structured
    if message is not None:
        assert structured["message"] == message
    # 16.1.3: the validated path writes the text block as compact JSON.
    assert result["content"][0]["text"] == json.dumps(
        structured, ensure_ascii=False, separators=(",", ":"))
    if tool in ALL_ADDITIVE_TOOL_NAMES:
        assert structured["operation_outcome"] == "not_applied"
        assert CORRELATION_ID.match(structured["correlation_id"])
        assert "error_code" not in structured
    else:
        assert set(structured) == {"status", "reason", "message", "error_code"}
        assert structured["error_code"] == "INVALID_ARGUMENT"
    return structured


ALL_FOUR = (STRICT_MUTATING, STRICT_READ, LEGACY_MUTATING, LEGACY_READ)


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", ALL_FOUR)
async def test_wrong_project_refusal_matches_the_tools_schema(tmp_path, tool):
    env = _build(tmp_path)
    schemas = await _schemas(env)
    reply = await _call(env, tool, {"project": "nope", "filepath": "a.md"})
    _assert_refusal(reply, schemas[tool], tool, reason="project_unavailable",
                    message="The requested project is unavailable through this connector.")


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", ALL_FOUR)
async def test_no_projects_refusal_matches_the_tools_schema(tmp_path, tool):
    env = _build(tmp_path, projects=())
    schemas = await _schemas(env)
    reply = await _call(env, tool, {"project": "RW"})
    _assert_refusal(reply, schemas[tool], tool, reason="project_unavailable",
                    message="No projects are configured for this key.")


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", (STRICT_MUTATING, LEGACY_MUTATING))
async def test_read_only_write_refusal_matches_the_tools_schema(tmp_path, tool):
    env = _build(tmp_path, default_access="read")
    schemas = await _schemas(env)
    reply = await _call(env, tool, {"project": "RW", "filepath": "a.md", "content": "x"})
    _assert_refusal(reply, schemas[tool], tool, reason="read_only",
                    message=f"Tool '{tool}' is not available: this knowledge base is read-only.")


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", ALL_FOUR)
@pytest.mark.parametrize("arguments", [
    {"filepath": "a.md"},                   # no project, two projects so none is inferred
    {"project": "", "filepath": "a.md"},    # empty
    {"project": " RW ", "filepath": "a.md"},  # whitespace padded
])
async def test_missing_or_malformed_project_is_a_tool_error_not_a_protocol_error(
    tmp_path, tool, arguments
):
    env = _build(tmp_path, projects=("RW", "RW2"))
    schemas = await _schemas(env)
    reply = await _call(env, tool, arguments)
    _assert_refusal(reply, schemas[tool], tool, reason="invalid",
                    message="project must be a nonempty exact string")


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", ALL_FOUR)
async def test_nested_project_routing_is_a_tool_error(tmp_path, tool):
    env = _build(tmp_path)
    schemas = await _schemas(env)
    reply = await _call(env, tool, {"project": "RW", "options": {"project": "RW2"}})
    _assert_refusal(reply, schemas[tool], tool, reason="invalid",
                    message="nested project routing is not allowed")


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", (STRICT_MUTATING, "set_folder_indexing", LEGACY_MUTATING))
async def test_bad_operation_id_on_a_mutating_tool_is_a_refusal_in_that_tools_shape(tmp_path, tool):
    env = _build(tmp_path)
    schemas = await _schemas(env)
    reply = await _call(env, tool, {"project": "RW", "operation_id": 12345})
    _assert_refusal(reply, schemas[tool], tool, reason="invalid",
                    message="operation_id must be a string; got int.")


@pytest.mark.asyncio
async def test_legacy_tools_keep_exactly_their_old_structured_content(tmp_path):
    env = _build(tmp_path)
    reply = await _call(env, LEGACY_READ, {"project": "nope", "filepath": "a.md"})
    assert reply["result"]["structuredContent"] == {
        "status": "error", "reason": "project_unavailable",
        "message": "The requested project is unavailable through this connector.",
        "error_code": "INVALID_ARGUMENT",
    }
    assert reply["result"]["isError"] is True


@pytest.mark.asyncio
async def test_omitted_arguments_work_for_list_projects_and_every_other_tool(tmp_path):
    env = _build(tmp_path)
    schemas = await _schemas(env)
    reply = await _call(env, "list_projects")
    structured = reply["result"]["structuredContent"]
    assert reply["result"]["isError"] is False and structured["status"] == "success"
    assert [row["name"] for row in structured["projects"]] == ["RW"]
    Draft202012Validator(schemas["list_projects"]).validate(structured)
    # `arguments: {}` stays equivalent.
    again = await _call(env, "list_projects", {})
    assert again["result"]["structuredContent"] == structured
    # A project-scoped tool without `arguments` is answered like one with `{}`: a
    # tool error the model can read (two projects, so none is inferred), not -32602.
    (tmp_path / "two").mkdir()
    two = _build(tmp_path / "two", projects=("RW", "RW2"))
    other = await _call(two, LEGACY_READ)
    _assert_refusal(other, (await _schemas(two))[LEGACY_READ], LEGACY_READ, reason="invalid",
                    message="project must be a nonempty exact string")
    # And a batch with no arguments is the batch's own tool error, not -32602.
    batch = await _call(env, "batch")
    assert batch["result"]["isError"] is True
    assert batch["result"]["structuredContent"]["reason"] == "invalid_batch"
    Draft202012Validator(schemas["batch"]).validate(batch["result"]["structuredContent"])


@pytest.mark.asyncio
async def test_list_projects_with_arguments_is_a_tool_error(tmp_path):
    env = _build(tmp_path)
    schemas = await _schemas(env)
    reply = await _call(env, "list_projects", {"project": "RW"})
    _assert_refusal(reply, schemas["list_projects"], "list_projects", reason="invalid",
                    message="list_projects accepts no arguments")


@pytest.mark.asyncio
@pytest.mark.parametrize("name,arguments", [
    ("list_projects", "x"), ("list_projects", None), ("list_projects", [1]),
    (LEGACY_READ, "x"), (LEGACY_READ, None), (LEGACY_READ, 7),
    ("workspace_info", "x"), ("workspace_info", None),
    (STRICT_READ, [1]),
])
async def test_arguments_present_and_not_an_object_stay_a_protocol_error(
    tmp_path, full_mode_workspace_service, name, arguments
):
    env = _build(tmp_path, workspace_service=full_mode_workspace_service)
    reply = await _call(env, name, arguments)
    assert reply["error"]["code"] == -32602 and "id" in reply and reply["id"] == 7


@pytest.mark.asyncio
@pytest.mark.parametrize("params", [
    5, "x", [], {}, {"name": None}, {"name": 7}, {"name": ["a"]}, {"name": {"a": 1}},
    {"name": "no_such_tool", "arguments": {}},
])
async def test_params_that_name_no_known_tool_stay_a_protocol_error(tmp_path, params):
    env = _build(tmp_path)
    reply = await _post(env, {"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": params})
    assert reply["error"]["code"] == -32602 and reply["id"] == 7, reply


@pytest.mark.asyncio
async def test_non_object_params_stay_a_protocol_error_even_with_no_projects(tmp_path):
    env = _build(tmp_path, projects=())
    reply = await _post(env, {"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": 5})
    assert reply["error"]["code"] == -32602


@pytest.mark.asyncio
async def test_batch_envelope_error_is_built_for_the_batch_tool(tmp_path):
    env = _build(tmp_path)
    schemas = await _schemas(env)
    reply = await _call(env, "batch", {"calls": [], "on_error": "stop"})
    structured = reply["result"]["structuredContent"]
    Draft202012Validator(schemas["batch"]).validate(structured)
    assert reply["result"]["isError"] is True
    assert structured["reason"] == "invalid_batch" and structured["executed"] == 0
    assert structured["message"] == "calls must contain 1-50 objects"
    assert structured["error_code"] == "INVALID_ARGUMENT"


def _caller_is(function_name):
    frame = sys._getframe(2)
    while frame is not None:
        if frame.f_code.co_name == function_name:
            return True
        frame = frame.f_back
    return False


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", (STRICT_MUTATING, STRICT_READ, LEGACY_MUTATING, "no_such_tool"))
async def test_batch_child_policy_unavailable_is_in_the_childs_own_shape(tmp_path, monkeypatch, tool):
    env = _build(tmp_path)
    schemas = await _schemas(env)
    real = env.store.snapshot

    def snapshot(*args, **kwargs):
        if _caller_is("_dispatch_connector_batch"):
            raise ConnectorPolicyError("policy store down")
        return real(*args, **kwargs)

    monkeypatch.setattr(env.store, "snapshot", snapshot)
    reply = await _call(env, "batch", {"calls": [{"tool": tool, "arguments": {"project": "RW"}}]})
    structured = reply["result"]["structuredContent"]
    Draft202012Validator(schemas["batch"]).validate(structured)
    child = structured["results"][0]
    assert child["status"] == "error" and child["error"]["reason"] == "policy_unavailable"
    child_payload = child["result"]["structuredContent"]
    assert child_payload["reason"] == "policy_unavailable"
    assert child_payload["message"] == "Connector policy is unavailable; try again later."
    if tool in ALL_ADDITIVE_TOOL_NAMES:
        assert child_payload["operation_outcome"] == "not_applied"
        Draft202012Validator(schemas[tool]).validate(child_payload)
    elif is_known_tool(tool):
        Draft202012Validator(schemas[tool]).validate(child_payload)
    else:
        # A child name with no registered schema keeps the unvalidated path.
        assert child_payload["error_code"] == "INVALID_ARGUMENT"


class _BadWorkspaceService:
    """A Workspace runtime that returns results no schema allows."""

    def execute(self, _principal, tool, _arguments, *, connector_id=None):
        del connector_id
        return {"status": "success", "not_in_any_schema": tool}


@pytest.mark.asyncio
async def test_a_contract_violation_on_a_workspace_write_is_an_unknown_outcome(tmp_path):
    env = _build(tmp_path, workspace_service=_BadWorkspaceService())
    schemas = await _schemas(env)
    write = await _call(env, "workspace_write_file", {"path": "/workspace/a.txt", "text": "x"})
    read = await _call(env, "workspace_info", {})
    for reply, tool in ((write, "workspace_write_file"), (read, "workspace_info")):
        Draft202012Validator(schemas[tool]).validate(reply["result"]["structuredContent"])
        assert reply["result"]["isError"] is True
    # A write that may have committed must not look retryable (internal_error).
    assert write["result"]["structuredContent"]["reason"] == "output_contract_violation"
    assert write["result"]["structuredContent"]["operation_outcome"] == "unknown"
    assert read["result"]["structuredContent"]["reason"] == "internal_error"


# ---------------------------------------------------------------- self-test plan


def _knowledge_section_ids():
    ids = {str(row["id"]) for readonly in (True, False) for row in self_test_section_catalog(readonly=readonly)}
    return sorted(ids)


SECTION_ARGUMENTS = [
    None, "full", "index", *_knowledge_section_ids(), *WORKSPACE_SECTIONS,
    "nope", "W99", "Wzzz", "w", "",
]


def _shape(structured):
    """Name the payload shape, so the matrix can prove it saw all of them."""
    if structured.get("status") == "blocked":
        return "catalog_missing" if "missing_tools" in structured else "gateway_blocked"
    if "catalog_assertions" in structured:
        return "workspace_plan" if structured["status"] == "success" else "workspace_error"
    return "knowledge_plan" if structured["status"] == "success" else "knowledge_error"


@pytest.mark.asyncio
async def test_every_self_test_section_validates_for_every_connector_and_host_configuration(tmp_path):
    seen: dict[str, int] = {}
    for workspace_enabled in (True, False):
        for transfer in ("allow", "deny"):
            for host in (True, False):
                for access in ("write", "read"):
                    sub = tmp_path / f"{workspace_enabled}-{transfer}-{host}-{access}"
                    sub.mkdir()
                    env = _build(
                        sub, default_access=access, workspace_enabled=workspace_enabled,
                        transfer=transfer,
                        workspace_service=_BadWorkspaceService() if host else None,
                    )
                    schemas = await _schemas(env)
                    schema = schemas["get_self_test_plan"]
                    batch = []
                    for index, section in enumerate(SECTION_ARGUMENTS):
                        arguments = {"project": "RW"}
                        if section is not None:
                            arguments["section"] = section
                        batch.append(_tools_call("get_self_test_plan", arguments, msg_id=index))
                    replies = await _post(env, batch)
                    assert len(replies) == len(SECTION_ARGUMENTS)
                    label = (workspace_enabled, transfer, host, access)
                    for reply in sorted(replies, key=lambda item: item["id"]):
                        section = SECTION_ARGUMENTS[reply["id"]]
                        assert "error" not in reply, (label, section, reply)
                        result = reply["result"]
                        structured = result["structuredContent"]
                        # oneOf: exactly one branch matches, never zero or two.
                        Draft202012Validator(schema).validate(structured)
                        assert json.loads(result["content"][0]["text"]) == structured
                        shape = _shape(structured)
                        seen[shape] = seen.get(shape, 0) + 1
                        # A block is not a failure: the self-test text treats BLOCKED
                        # as distinct from FAIL.
                        if structured["status"] == "blocked":
                            assert result["isError"] is False, (label, section, structured)
                        elif structured["status"] == "error":
                            assert result["isError"] is True
                        else:
                            assert result["isError"] is False
                        if shape == "gateway_blocked":
                            assert set(structured) == {"status", "reason", "section"}
                            assert structured["reason"] in {"workspace_unavailable", "bridge_unavailable"}
                        if shape == "catalog_missing":
                            assert structured["reason"] == "catalog_missing"
    # The matrix means nothing unless it reached every payload shape the gateway sends.
    assert {"workspace_plan", "gateway_blocked", "catalog_missing", "workspace_error",
            "knowledge_plan", "knowledge_error"} <= set(seen), seen


@pytest.mark.asyncio
async def test_gateway_blocked_reasons_are_each_reachable(tmp_path):
    seen = set()
    cases = (
        (dict(workspace_enabled=False), "W", "workspace_unavailable"),
        (dict(workspace_enabled=True, transfer="deny", workspace_service=_BadWorkspaceService()),
         "W11", "bridge_unavailable"),
    )
    for index, (options, section, reason) in enumerate(cases):
        sub = tmp_path / f"case{index}"
        sub.mkdir()
        env = _build(sub, **options)
        schemas = await _schemas(env)
        reply = await _call(env, "get_self_test_plan", {"project": "RW", "section": section})
        structured = reply["result"]["structuredContent"]
        assert structured == {"status": "blocked", "reason": reason, "section": section}
        assert reply["result"]["isError"] is False
        Draft202012Validator(schemas["get_self_test_plan"]).validate(structured)
        seen.add(structured["reason"])
    assert seen == {"workspace_unavailable", "bridge_unavailable"}


@pytest.mark.asyncio
async def test_self_test_argument_error_is_built_for_the_tool(tmp_path):
    env = _build(tmp_path)
    schemas = await _schemas(env)
    reply = await _call(env, "get_self_test_plan", {"project": "RW", "section": 5})
    _assert_refusal(reply, schemas["get_self_test_plan"], "get_self_test_plan", reason="invalid",
                    message="section must be a string when supplied")


async def _post_raw(env, raw: bytes):
    """A request body sent verbatim (httpx's json= refuses a lone surrogate)."""
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=env.app), base_url="http://fixture",
    ) as client:
        response = await client.post(
            env.path, content=raw,
            headers={"Authorization": f"Bearer {env.token}", "Content-Type": "application/json"},
        )
    assert response.status_code == 200, response.text
    return response.json()


@pytest.mark.asyncio
async def test_self_test_unknown_argument_is_named_as_an_unknown_argument(tmp_path):
    env = _build(tmp_path)
    schemas = await _schemas(env)
    for arguments in ({"project": "RW", "bogus": 1}, {"project": "RW", "section": "index", "bogus": 1},
                      {"project": "RW", "section": 5, "bogus": 1}):
        reply = await _call(env, "get_self_test_plan", arguments)
        structured = _assert_refusal(reply, schemas["get_self_test_plan"], "get_self_test_plan",
                                     reason="unknown_argument")
        assert "'bogus'" in structured["message"]
        assert "section must be a string" not in structured["message"]
        assert "NOTHING was executed" in structured["message"]
        assert "Accepted arguments: project, section." in structured["message"]
    # The "section must be a string" message is still what a non-string section gets.
    reply = await _call(env, "get_self_test_plan", {"project": "RW", "section": ["index"]})
    _assert_refusal(reply, schemas["get_self_test_plan"], "get_self_test_plan", reason="invalid",
                    message="section must be a string when supplied")
    # A valid call is unchanged.
    ok = await _call(env, "get_self_test_plan", {"project": "RW", "section": "index"})
    assert ok["result"]["isError"] is False and ok["result"]["structuredContent"]["status"] == "success"


@pytest.mark.asyncio
async def test_self_test_unknown_argument_names_are_sanitized_and_bounded(tmp_path):
    env = _build(tmp_path)
    schemas = await _schemas(env)
    evil = "a\\nINFO forged \\u001b[31m" + "Z" * 200
    raw = ('{"jsonrpc":"2.0","id":7,"method":"tools/call","params":{"name":"get_self_test_plan",'
           '"arguments":{"project":"RW","%s":1,"b-lone\\ud800":2,"k1":1,"k2":1,"k3":1,"k4":1,"k5":1}}}' % evil)
    reply = await _post_raw(env, raw.encode("ascii"))
    structured = _assert_refusal(reply, schemas["get_self_test_plan"], "get_self_test_plan",
                                 reason="unknown_argument")
    message = structured["message"]
    assert "\n" not in message and "\x1b" not in message and "\ud800" not in message
    assert "'a INFO forged ?[31m" in message and "Z" * 41 not in message  # one line, 40 characters
    assert "'b-lone?'" in message and "(and 2 more)" in message  # at most five names
    assert len(message) < 400


@pytest.mark.asyncio
async def test_the_batch_contract_still_builds_and_stays_within_its_size_limit():
    from cognita.result_contracts import OUTPUT_SCHEMAS_BY_TOOL

    batch = OUTPUT_SCHEMAS_BY_TOOL["batch"]
    Draft202012Validator.check_schema(batch)
    assert len(json.dumps(batch, separators=(",", ":")).encode("utf-8")) < 600_000
    assert batch["type"] == "object"


# ---------------------------------------------------------------- the workspace-only route


@pytest.mark.asyncio
async def test_workspace_only_self_test_results_are_validated(tmp_path):
    from argon2 import PasswordHasher

    from cognita.auth_policy import CredentialPolicyStore
    from cognita.connectors import WorkspaceConnectorStore

    registry = Registry(tmp_path / "registry.yaml")
    connectors = ConnectorStore(tmp_path / "connectors.yaml")
    workspace_connectors = WorkspaceConnectorStore(tmp_path / "workspace-connectors.yaml")
    surface = workspace_connectors.create(
        expected_revision=0, display_name="Runner", enabled=True, slug="runner")
    credentials = CredentialPolicyStore(
        tmp_path / "credentials-v2.json", master_key_dir=tmp_path / "master-keys",
        admin_password_hash=PasswordHasher().hash("admin-password"),
    )
    _row, secret = credentials.add_credential(
        "workspace", surface.id, "Runner client", surface_slug=surface.slug,
        password="admin-password",
    )
    app = create_gateway_app(
        CognitaConfig(registry_path=registry.path, connectors_path=connectors.path,
                      public_base_url="https://example.test", oauth_enabled=False),
        registry, connector_store=connectors,
        workspace_connector_store=workspace_connectors, credential_store=credentials,
        workspace_service=_BadWorkspaceService(),
    )
    path = f"/mcp/workspace/{surface.slug}/mcp/v3"
    headers = {"Authorization": f"Bearer {secret}"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="https://example.test") as client:
        listed = (await client.post(path, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                                    headers=headers)).json()
        schema = next(tool["outputSchema"] for tool in listed["result"]["tools"]
                      if tool["name"] == "workspace_generate_self_test")
        for request_id, arguments, error in (
            (2, {}, False), (3, {"section": "W2"}, False), (4, {"section": "index"}, False),
            (5, {"section": "nope"}, True), (6, {"section": 5}, True),
        ):
            reply = (await client.post(path, json=_tools_call(
                "workspace_generate_self_test", arguments, request_id), headers=headers)).json()
            result = reply["result"]
            Draft202012Validator(schema).validate(result["structuredContent"])
            assert result["isError"] is error, result
            # Validated now: the compact text block, not the indented one.
            assert result["content"][0]["text"] == json.dumps(
                result["structuredContent"], ensure_ascii=False, separators=(",", ":"))
        assert reply["result"]["structuredContent"]["reason"] == "invalid"
        # 16.1.3: an argument the tool does not take is named as such, not
        # answered with the section message; still a valid, isError result.
        for request_id, arguments in ((8, {"bogus": 1}), (9, {"section": "W2", "extra\nline": 1}),
                                      (10, {"section": 5, "bogus": 1})):
            reply = (await client.post(path, json=_tools_call(
                "workspace_generate_self_test", arguments, request_id), headers=headers)).json()
            result = reply["result"]
            Draft202012Validator(schema).validate(result["structuredContent"])
            assert result["isError"] is True
            assert result["structuredContent"]["reason"] == "unknown_argument", result
            message = result["structuredContent"]["message"]
            assert "section must be a string" not in message and "\n" not in message
            assert "workspace_generate_self_test does not accept " in message
            assert "Accepted arguments: section." in message
            assert {8: "'bogus'", 9: "'extra line'", 10: "'bogus'"}[request_id] in message


# ---------------------------------------------------------------- step R2 of the plan (A5)


def test_step_r2_states_no_stale_tool_count():
    plan = build_self_test_plan(__version__, readonly=False)
    assert "exactly 40" not in plan
    core, workspace, bridge = len(CORE_TOOL_NAMES), len(WORKSPACE_TOOL_NAMES), len(KNOWLEDGE_BRIDGE_PREFIXES)
    assert f"{core} tool definitions" in plan
    assert f"({core + workspace} in all)" in plan and f"({core + workspace + bridge} in all)" in plan


@pytest.mark.asyncio
async def test_step_r2_counts_match_the_catalogs_the_connector_really_advertises(
    tmp_path, full_mode_workspace_service
):
    core, workspace, bridge = len(CORE_TOOL_NAMES), len(WORKSPACE_TOOL_NAMES), len(KNOWLEDGE_BRIDGE_PREFIXES)
    expected = {
        "workspace off": (dict(workspace_enabled=False), core),
        "workspace on, no bridge": (dict(transfer="deny", workspace_service=full_mode_workspace_service),
                                    core + workspace),
        "workspace on, bridge": (dict(workspace_service=full_mode_workspace_service),
                                 core + workspace + bridge),
    }
    for label, (options, count) in expected.items():
        sub = tmp_path / label.replace(" ", "-").replace(",", "")
        sub.mkdir()
        env = _build(sub, **options)
        listed = await _post(env, {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
        assert len(listed["result"]["tools"]) == count, label
