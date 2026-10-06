"""13.2.7: get_self_test_plan's result matches the outputSchema the server advertises.

DESIGN-13.2-CONNECTOR-DIAGNOSTICS §7. With Workspace on, the gateway adds
``workspace_plan_version`` to the plan and the advertised schema forbade unknown
keys. The server never validated this one result (it was the only tool result
built without its tool name), so nothing was logged; a client that validates
results against outputSchema — SillyTavern's node MCP client — discarded every
plan as "[No content]" for five releases, while claude.ai, which does not
validate, showed it. This test does what that client does: take the schema from
tools/list and validate the real result against it.
"""

from __future__ import annotations

import json

import jsonschema
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from cognita.auth_policy import AuthenticationPolicyStore
from cognita.config import CognitaConfig
from cognita.connectors import ConnectorStore
from cognita.gateway import create_gateway_app
from cognita.registry import Project, Registry
from engine_fakes import FakeEngineHost


@pytest.fixture
def env(tmp_path, full_mode_workspace_service):
    docs = tmp_path / "docs"
    docs.mkdir()
    registry = Registry(tmp_path / "registry.yaml")
    registry.add(Project(name="RW", documents_dir=docs, data_dir=tmp_path / "data"))
    auth = AuthenticationPolicyStore(tmp_path / "authentication.yaml", project_names=["RW"])
    token = auth.mutate_global(
        expected_revision=0, oauth_enabled=False, static_key_action="generate"
    )["generated_key"]
    store = ConnectorStore(tmp_path / "connectors.yaml")
    config = store.create(expected_revision=0, name="Plan", project_names=["RW"])
    assert config.connectors[0].workspace_enabled, "the bug needs Workspace on (the 13.0.1 default)"
    app = create_gateway_app(
        CognitaConfig(registry_path=registry.path, connectors_path=store.path, data_root=tmp_path),
        registry, engine=FakeEngineHost(FastAPI()), connector_store=store,
        authentication_store=auth, workspace_service=full_mode_workspace_service,
    )
    return app, config.connectors[0].slug, token


async def _rpc(app, slug, token, method, params=None):
    body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.post(f"/mcp/connectors/{slug}/mcp/v{PUBLIC_CONTRACT_VERSION}", json=body,
                              headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200, r.text
    return r.json()["result"]


@pytest.mark.asyncio
@pytest.mark.parametrize("arguments", [{}, {"section": "full"}, {"section": "index"}, {"section": "1"}])
async def test_plan_result_validates_against_the_advertised_output_schema(env, arguments):
    app, slug, token = env
    tools = (await _rpc(app, slug, token, "tools/list"))["tools"]
    schema = next(t["outputSchema"] for t in tools if t["name"] == "get_self_test_plan")

    result = await _rpc(app, slug, token, "tools/call",
                        {"name": "get_self_test_plan", "arguments": {"project": "RW", **arguments}})
    structured = result["structuredContent"]
    assert result["isError"] is False
    assert structured["status"] == "success", structured
    # The very field that broke the contract must be present for this to mean
    # anything (the gateway adds it to the full and index answers; a single
    # knowledge section has no workspace counterpart and gets none).
    if arguments.get("section", "full") in {"full", "index"}:
        assert structured["workspace_plan_version"]
    if arguments.get("section") == "index":
        # Both row shapes are in the one list; that is what the schema must describe.
        assert {"1", "W11"} <= {row["id"] for row in structured["sections"]}
    jsonschema.validate(structured, schema)
    # The text block is the same payload; a client may read either.
    assert json.loads(result["content"][0]["text"]) == structured


@pytest.mark.asyncio
async def test_unknown_section_is_a_contract_valid_error(env):
    app, slug, token = env
    tools = (await _rpc(app, slug, token, "tools/list"))["tools"]
    schema = next(t["outputSchema"] for t in tools if t["name"] == "get_self_test_plan")
    result = await _rpc(app, slug, token, "tools/call",
                        {"name": "get_self_test_plan", "arguments": {"project": "RW", "section": "nope"}})
    structured = result["structuredContent"]
    assert result["isError"] is True and structured["reason"] == "unknown_section"
    jsonschema.validate(structured, schema)
