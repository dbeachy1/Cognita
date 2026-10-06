"""JSON-RPC 2.0 batch requests through the gateway (5.0 §1.1).

An array body used to come back as -32600, so 17 pushes were 17 HTTP round
trips through the tunnel. Batching is part of JSON-RPC 2.0 and MCP inherits it.

The two things these tests pin, beyond "it works": every element goes through
the SAME policy path as a single call — a batch that skipped the read-only gate
would be a hole in it wearing a JSON array as a disguise — and one failing
element does not take the others down, because a batch is a transport
optimization and not a transaction.
"""

import json

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from httpx import ASGITransport, AsyncClient

from cognita.auth_policy import AuthenticationPolicyStore
from cognita.config import CognitaConfig
from cognita.connectors import ConnectorStore
from cognita.gateway import create_gateway_app
from cognita.registry import Project, Registry
from engine_fakes import FakeEngineHost

ENGINE_TOOLS = [
    {"name": n, "description": n,
     "inputSchema": {"type": "object", "properties": {}, "required": []}}
    for n in ["list_categories", "get_index_stats", "add_document", "update_document"]
]


def make_fake_worker(seen: list) -> FastAPI:
    app = FastAPI()

    @app.post("/mcp")
    async def mcp(request: Request):
        msg = await request.json()
        seen.append(msg)
        if msg.get("method") == "tools/list":
            return JSONResponse({"jsonrpc": "2.0", "id": msg["id"],
                                 "result": {"tools": ENGINE_TOOLS}})
        name = (msg.get("params") or {}).get("name")
        if name == "no_such_tool":
            return JSONResponse({"jsonrpc": "2.0", "id": msg["id"],
                                 "error": {"code": -32602,
                                           "message": "Unknown tool: no_such_tool"}})
        payload = {"status": "success", "tool": name}
        return JSONResponse({"jsonrpc": "2.0", "id": msg["id"], "result": {
            "content": [{"type": "text", "text": json.dumps(payload)}],
            "isError": False}})

    return app


@pytest.fixture
def env(tmp_path, full_mode_workspace_service):
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "note.md").write_text("# Note\n\nalpha line\n", encoding="utf-8")
    registry = Registry(tmp_path / "registry.yaml")
    registry.add(Project(name="RW", documents_dir=docs, data_dir=tmp_path / "d1"))
    registry.add(Project(name="RO", documents_dir=docs, data_dir=tmp_path / "d2",
                         writable=False))
    # 13.0 §7.3: the `config.test_mode` registry-token fallback these tests
    # used to authenticate through is deleted. They now authenticate the way
    # production does, with project-scoped keys from the parent-owned policy
    # store — one per project, because the helpers below key their connector
    # and project maps by the credential. OAuth is off so an unknown bearer
    # stays a 401 instead of a 503 from the absent OAuth child.
    auth = AuthenticationPolicyStore(
        tmp_path / "authentication.yaml", project_names=["RW", "RO"]
    )
    auth.mutate_global(expected_revision=0, oauth_enabled=False, static_key_action="generate")
    tok_rw = auth.mutate_project("RW", expected_revision=1, static_key_action="generate")["generated_key"]
    tok_ro = auth.mutate_project("RO", expected_revision=2, static_key_action="generate")["generated_key"]
    seen: list = []
    worker = make_fake_worker(seen)
    config = CognitaConfig(registry_path=tmp_path / "registry.yaml", data_root=tmp_path)
    store = ConnectorStore(tmp_path / "connectors.yaml")
    all_config = store.create(expected_revision=0, name="All", project_names=["RW", "RO"])
    read_config = store.create(
        expected_revision=1, name="Read", project_mode="selected", default_access=None,
        project_access={"RW": "write", "RO": "read"}, project_names=["RW", "RO"],
    )
    config.connectors_path = store.path
    app = create_gateway_app(
        config, registry, engine=FakeEngineHost(worker),
        connector_store=store, authentication_store=auth,
        workspace_service=full_mode_workspace_service,
    )
    app.state.test_connector_slugs = {tok_rw: all_config.connectors[0].slug,
                                     tok_ro: read_config.connectors[1].slug}
    app.state.test_projects = {tok_rw: "RW", tok_ro: "RO"}
    return app, tok_rw, tok_ro, docs, seen


async def post(app, token, payload):
    connector_slug = app.state.test_connector_slugs[token]
    project = app.state.test_projects[token]
    payload = json.loads(json.dumps(payload))
    messages = payload if isinstance(payload, list) else [payload]
    for message in messages:
        params = message.get("params") if isinstance(message, dict) else None
        if isinstance(params, dict) and params.get("name") != "list_projects":
            arguments = params.get("arguments")
            if isinstance(arguments, dict):
                arguments.setdefault("project", project)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        return await c.post(f"/mcp/connectors/{connector_slug}/mcp/v{PUBLIC_CONTRACT_VERSION}", json=payload,
                            headers={"Authorization": f"Bearer {token}"})


def call(name, arguments, msg_id):
    return {"jsonrpc": "2.0", "id": msg_id, "method": "tools/call",
            "params": {"name": name, "arguments": arguments}}


async def test_batch_returns_an_array_with_matching_ids(env):
    app, tok, _, _, _ = env
    r = await post(app, tok, [call("list_categories", {}, 1),
                              call("get_index_stats", {}, 2)])
    assert r.status_code == 200
    body = r.json()
    assert isinstance(body, list) and len(body) == 2
    assert [item["id"] for item in body] == [1, 2]
    assert all("result" in item for item in body)


async def test_batch_preserves_order(env):
    """Elements execute in order: they can touch the same file, and the write
    lock is per project, so running them concurrently would interleave writes."""
    app, tok, _, _, _ = env
    ids = [10, 11, 12, 13]
    r = await post(app, tok, [call("list_categories", {}, i) for i in ids])
    assert [item["id"] for item in r.json()] == ids


async def test_one_failing_element_does_not_fail_the_batch(env):
    app, tok, _, _, _ = env
    r = await post(app, tok, [call("list_categories", {}, 1),
                              call("no_such_tool", {}, 2),
                              call("get_index_stats", {}, 3)])
    body = r.json()
    assert len(body) == 3
    assert "result" in body[0] and "result" in body[2]
    assert "error" in body[1] and body[1]["id"] == 2


async def test_empty_batch_is_invalid_request(env):
    app, tok, _, _, _ = env
    r = await post(app, tok, [])
    assert r.json()["error"]["code"] == -32600


async def test_non_object_element_is_reported_without_sinking_the_batch(env):
    app, tok, _, _, _ = env
    r = await post(app, tok, ["not an object", call("list_categories", {}, 2)])
    body = r.json()
    assert len(body) == 2
    assert body[0]["error"]["code"] == -32600 and body[0]["id"] is None
    assert "result" in body[1]


async def test_notifications_get_no_entry(env):
    app, tok, _, _, _ = env
    r = await post(app, tok, [{"jsonrpc": "2.0", "method": "notifications/initialized"},
                              call("list_categories", {}, 5)])
    body = r.json()
    assert len(body) == 1 and body[0]["id"] == 5


async def test_batch_of_only_notifications_returns_202_and_no_body(env):
    app, tok, _, _, _ = env
    r = await post(app, tok, [{"jsonrpc": "2.0", "method": "notifications/initialized"}])
    assert r.status_code == 202
    assert not r.content


async def test_batch_enforces_the_read_only_gate_per_element(env):
    """The hole this test exists to prevent: policy applied to single calls but
    not to array elements would make read-only projects writable via a batch."""
    app, _, tok_ro, _, seen = env
    r = await post(app, tok_ro, [call("list_categories", {}, 1),
                                 call("update_document",
                                      {"filepath": "note.md", "content": "x"}, 2)])
    body = r.json()
    assert "result" in body[0]
    blocked = json.loads(body[1]["result"]["content"][0]["text"])
    assert blocked["reason"] == "read_only"
    assert all((m.get("params") or {}).get("name") != "update_document" for m in seen)


async def test_batch_applies_strict_argument_checking_per_element(env):
    app, tok, _, _, _ = env
    r = await post(app, tok, [call("read_document",
                                   {"filepath": "note.md", "lines": "1-2"}, 1)])
    payload = json.loads(r.json()[0]["result"]["content"][0]["text"])
    assert payload["reason"] == "unknown_argument"


async def test_batch_serves_gateway_tools_from_disk(env):
    """read_document is answered by the gateway, never forwarded — that must
    hold inside a batch too, or half the surface disappears from one."""
    app, tok, _, _, seen = env
    r = await post(app, tok, [call("read_document",
                                   {"filepath": "note.md", "start_line": 1,
                                    "end_line": 1}, 1)])
    payload = json.loads(r.json()[0]["result"]["content"][0]["text"])
    assert payload["status"] == "success"
    assert payload["text"] == "# Note"
    assert seen == []  # nothing reached the worker


async def test_tools_list_inside_a_batch_is_still_rewritten(env):
    """The gateway's own tools are injected into tools/list; a batched
    tools/list that skipped the rewrite would advertise a smaller surface."""
    app, tok, _, _, _ = env
    r = await post(app, tok, [{"jsonrpc": "2.0", "id": 1, "method": "tools/list"}])
    tools = r.json()[0]["result"]["tools"]
    assert len(tools) == 73 and "list_projects" in {t["name"] for t in tools}


async def test_single_message_still_works_unchanged(env):
    app, tok, _, _, _ = env
    r = await post(app, tok, call("list_categories", {}, 1))
    body = r.json()
    assert isinstance(body, dict) and body["id"] == 1
