"""9.2 connector-scoped sequential batch contract.

These tests use an owned in-memory worker and temporary project fixtures only;
they never dispatch a batch against a personal project or corpus.
"""

from __future__ import annotations

import json

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from httpx import ASGITransport, AsyncClient

from cognita.auth_policy import AuthenticationPolicyStore
from cognita.books.caller_context import CURRENT_BOOK_CALLER
from cognita.config import CognitaConfig
from cognita.connectors import ConnectorStore
from cognita.gateway import create_gateway_app
from cognita.registry import Project, Registry
from engine_fakes import FakeEngineHost


def _call(tool: str, arguments: dict, msg_id: int = 1) -> dict:
    return {"jsonrpc": "2.0", "id": msg_id, "method": "tools/call",
            "params": {"name": tool, "arguments": arguments}}


def _payload(response) -> dict:
    return json.loads(response.json()["result"]["content"][0]["text"])


@pytest.fixture
def batch_env(tmp_path):
    seen: list[dict] = []
    policy_change = {"enabled": False, "done": False}
    worker = FastAPI()

    @worker.post("/mcp")
    async def mcp(request: Request):
        message = await request.json()
        caller = CURRENT_BOOK_CALLER.get()
        message["_book_caller"] = None if caller is None else {
            "principal_kind": caller.principal.kind,
            "principal_id": caller.principal.principal_id,
            "project_name": caller.project.name,
            "connector_id": caller.connector_id,
        }
        seen.append(message)
        name = (message.get("params") or {}).get("name")
        if name == "list_categories" and policy_change["enabled"] and not policy_change["done"]:
            current = store.snapshot()
            store.update(config.connectors[0].id, expected_revision=current.revision,
                         project_mode="selected", default_access=None, project_access={})
            policy_change["done"] = True
        if name == "unknown_tool":
            return JSONResponse({"jsonrpc": "2.0", "id": message.get("id"),
                                 "error": {"code": -32602,
                                           "message": "Unknown tool: unknown_tool"}})
        if name == "get_index_stats":
            payload = {"status": "error", "reason": "application_failed",
                       "message": "fixture failure"}
        elif name == "get_documents":
            payload = {"status": "success", "content": "x" * (1024 * 1024 + 10)}
        else:
            payload = {"status": "success", "tool": name}
        return JSONResponse({"jsonrpc": "2.0", "id": message.get("id"),
                             "result": {"content": [{"type": "text",
                                                         "text": json.dumps(payload)},],
                                         "isError": payload.get("status") == "error"}})

    docs = tmp_path / "docs"
    docs.mkdir()
    registry = Registry(tmp_path / "registry.yaml")
    registry.add(Project(name="RW", documents_dir=docs, data_dir=tmp_path / "data"))
    # 13.0 §7.3: the `config.test_mode` registry-token fallback this fixture
    # authenticated through is deleted; it now uses the production path, a
    # global static key from the parent-owned policy store. OAuth is off so an
    # unknown bearer stays a 401 rather than a 503 from the absent OAuth child.
    auth = AuthenticationPolicyStore(
        tmp_path / "authentication.yaml", project_names=["RW"]
    )
    token = auth.mutate_global(
        expected_revision=0, oauth_enabled=False, static_key_action="generate"
    )["generated_key"]
    store = ConnectorStore(tmp_path / "connectors.yaml")
    config = store.create(expected_revision=0, name="Batch", project_names=["RW"])
    app_config = CognitaConfig(registry_path=registry.path, connectors_path=store.path,
                               data_root=tmp_path)
    app = create_gateway_app(app_config, registry, engine=FakeEngineHost(worker),
                             connector_store=store, authentication_store=auth)
    app.state.batch_policy_change = policy_change
    return app, config.connectors[0].slug, token, seen


async def _post(app, connector_slug: str, token: str, calls: list[dict], *, on_error="stop"):
    request = _call("batch", {"calls": calls, "on_error": on_error})
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        return await client.post(
            f"/mcp/connectors/{connector_slug}/mcp/v5", json=request,
            headers={"Authorization": f"Bearer {token}"},
        )


@pytest.mark.asyncio
async def test_batch_dispatches_in_order_and_stops_or_continues(batch_env):
    app, connector_slug, token, seen = batch_env
    calls = [{"tool": "list_categories", "arguments": {"project": "RW"}},
             {"tool": "get_index_stats", "arguments": {"project": "RW"}},
             {"tool": "search_knowledge", "arguments": {"project": "RW"}}]
    stopped = _payload(await _post(app, connector_slug, token, calls))
    assert [entry["status"] for entry in stopped["results"]] == ["success", "error", "skipped"]
    assert (stopped["succeeded"], stopped["failed"], stopped["skipped"]) == (1, 1, 1)
    assert [item["params"]["name"] for item in seen] == ["list_categories", "get_index_stats"]
    assert all(item["_book_caller"]["principal_kind"] == "static_global" for item in seen)
    assert all(item["_book_caller"]["project_name"] == "RW" for item in seen)
    assert all(item["_book_caller"]["connector_id"] for item in seen)

    seen.clear()
    continued = _payload(await _post(app, connector_slug, token, calls, on_error="continue"))
    assert [entry["status"] for entry in continued["results"]] == ["success", "error", "success"]
    assert [item["params"]["name"] for item in seen] == ["list_categories", "get_index_stats", "search_knowledge"]


@pytest.mark.asyncio
async def test_batch_rejects_malformed_nested_and_image_envelopes(batch_env):
    app, connector_slug, token, seen = batch_env
    bad = _call("batch", {"calls": [{"tool": "list_categories", "arguments": {"project": "RW"},
                                      "extra": True}]})
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(f"/mcp/connectors/{connector_slug}/mcp/v5", json=bad,
                                     headers={"Authorization": f"Bearer {token}"})
    assert _payload(response)["reason"] == "invalid_batch"
    assert _payload(response)["executed"] == 0
    assert not seen

    for tool, reason in (("batch", "nested_batch_not_allowed"),
                         ("get_asset", "tool_not_batchable")):
        response = await _post(app, connector_slug, token,
                               [{"tool": tool, "arguments": {"project": "RW"}}])
        assert _payload(response)["reason"] == reason
    assert not seen


@pytest.mark.asyncio
async def test_batch_preserves_jsonrpc_errors_and_omits_large_results(batch_env):
    app, connector_slug, token, seen = batch_env
    errors = _payload(await _post(
        app, connector_slug, token,
        [{"tool": "unknown_tool", "arguments": {}},
         {"tool": "search_knowledge", "arguments": {"project": "RW"}}], on_error="continue",
    ))
    assert errors["results"][0]["error"]["code"] == -32602
    assert errors["results"][0]["error"]["message"] == "Unknown tool: unknown_tool"
    assert errors["results"][0]["error"]["reason"] == "unknown_tool"
    assert errors["results"][1]["status"] == "success"

    seen.clear()
    large = _payload(await _post(
        app, connector_slug, token,
        [{"tool": "get_documents", "arguments": {"project": "RW"}},
         {"tool": "search_knowledge", "arguments": {"project": "RW"}}],
    ))
    assert large["results"][0]["status"] == "success"
    assert large["results"][0]["result_omitted"] is True
    assert large["results"][1]["status"] == "skipped"
    assert large["omitted"] == 1 and large["failed"] == 0
    assert [item["params"]["name"] for item in seen] == ["get_documents"]


@pytest.mark.asyncio
async def test_batch_rechecks_current_project_policy_between_children(batch_env):
    app, connector_slug, token, seen = batch_env
    app.state.batch_policy_change["enabled"] = True
    payload = _payload(await _post(
        app, connector_slug, token,
        [{"tool": "list_categories", "arguments": {"project": "RW"}},
         {"tool": "search_knowledge", "arguments": {"project": "RW"}}], on_error="continue",
    ))
    assert payload["results"][0]["status"] == "success"
    assert payload["results"][1]["status"] == "error"
    assert payload["results"][1]["error"]["reason"] == "project_unavailable"
    assert [item["params"]["name"] for item in seen] == ["list_categories"]
