"""Cognita 10.0 connector-scoped gateway contract tests."""

from __future__ import annotations

import json

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from httpx import ASGITransport, AsyncClient

from cognita import __version__
from cognita.auth_policy import AuthenticationPolicyStore
from cognita.config import CognitaConfig
from cognita.connectors import PUBLIC_CONTRACT_VERSION, ConnectorStore
from cognita.gateway import create_gateway_app
from cognita.registry import Project, Registry
from engine_fakes import FakeEngineHost


def _rpc(method, params=None, msg_id=1):
    return {"jsonrpc": "2.0", "id": msg_id, "method": method,
            "params": params or {}}


@pytest.fixture
def env(tmp_path, full_mode_workspace_service):
    seen: list[dict] = []
    worker = FastAPI()

    @worker.post("/mcp")
    async def mcp(request: Request):
        message = await request.json()
        message["_connector_header"] = request.headers.get("x-cognita-connector-id")
        seen.append(message)
        method = message.get("method")
        if method == "tools/call":
            name = message["params"]["name"]
            payload = {"status": "success", "tool": name,
                       "arguments": message["params"].get("arguments", {})}
            return JSONResponse({"jsonrpc": "2.0", "id": message.get("id"), "result": {
                "content": [{"type": "text", "text": json.dumps(payload)}],
                "isError": False,
            }})
        return JSONResponse({"jsonrpc": "2.0", "id": message.get("id"), "result": {}})

    registry = Registry(tmp_path / "registry.yaml")
    registry.add(Project(name="A", documents_dir=tmp_path, data_dir=tmp_path / "a"))
    registry.add(Project(name="B", documents_dir=tmp_path, data_dir=tmp_path / "b",
                         enabled=True))
    # 13.0 §7.3: the `config.test_mode` registry-token fallback this fixture
    # authenticated through is deleted. One GLOBAL static key replaces the two
    # project tokens — these tests are about connector scoping, and a global
    # key reaches every project the connector grants, which is what the old
    # fallback effectively did. OAuth is off so an unknown bearer stays a 401.
    auth = AuthenticationPolicyStore(
        tmp_path / "authentication.yaml", project_names=["A", "B"]
    )
    token_a = auth.mutate_global(
        expected_revision=0, oauth_enabled=False, static_key_action="generate"
    )["generated_key"]
    store = ConnectorStore(tmp_path / "connectors.yaml")
    all_config = store.create(expected_revision=0, name="All", project_names=["A", "B"])
    all_id = all_config.connectors[0].id
    selected_config = store.create(
        expected_revision=1, name="Selected", project_mode="selected", default_access=None,
        project_access={"A": "read"}, project_names=["A", "B"],
    )
    selected_id = selected_config.connectors[1].id
    config = CognitaConfig(
        registry_path=registry.path, connectors_path=store.path, data_root=tmp_path,
        public_base_url="https://cognita.example",
    )
    app = create_gateway_app(config, registry, engine=FakeEngineHost(worker),
                             connector_store=store, authentication_store=auth,
                             workspace_service=full_mode_workspace_service)
    app.state.test_connector_slugs = {
        all_id: all_config.connectors[0].slug,
        selected_id: selected_config.connectors[1].slug,
    }
    return app, all_id, selected_id, token_a, seen


async def _post(app, connector_id, token, payload, *, contract_version=PUBLIC_CONTRACT_VERSION):
    connector_slug = app.state.test_connector_slugs[connector_id]
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://t") as client:
        return await client.post(
            f"/mcp/connectors/{connector_slug}/mcp/v{contract_version}", json=payload,
            headers={"Authorization": f"Bearer {token}"},
        )


@pytest.mark.asyncio
async def test_initialize_advertises_public_cognita_icon(env):
    app, connector_id, _selected_id, token, _seen = env
    initialized = await _post(app, connector_id, token, _rpc(
        "initialize", {"protocolVersion": "2025-11-25"}
    ))
    result = initialized.json()["result"]
    assert result["protocolVersion"] == "2025-11-25"
    assert result["serverInfo"] == {
        "name": "Cognita",
        "title": "Cognita",
        "version": __version__,
        "description": "Private knowledge, securely connected through Cognita.",
        "icons": [{
            "src": f"https://cognita.example/assets/cognita-icon-512.png?v={__version__}",
            "mimeType": "image/png",
            "sizes": ["512x512"],
        }],
    }

    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://t") as client:
        icon = await client.get("/assets/cognita-icon-512.png")
    assert icon.status_code == 200
    assert icon.headers["content-type"] == "image/png"
    assert icon.headers["cache-control"] == "public, max-age=86400"
    assert icon.content[:8] == b"\x89PNG\r\n\x1a\n"
    assert int.from_bytes(icon.content[16:20], "big") == 512
    assert int.from_bytes(icon.content[20:24], "big") == 512


@pytest.mark.asyncio
async def test_connector_scoped_catalog_and_projects(env):
    app, connector_id, _selected_id, token, _seen = env
    listed = await _post(app, connector_id, token, _rpc("tools/list"))
    tools = listed.json()["result"]["tools"]
    # Audiobook M1 adds four book and three project-storage tools; Workspace is on by default.
    assert listed.status_code == 200 and len(tools) == 64
    # Workspace tools are principal-scoped, not project-scoped, so they carry no
    # `project` argument; every Knowledge tool but the two catalog tools does.
    project_tools = [tool for tool in tools
                     if tool["name"] not in {"list_projects", "batch"}
                     and not tool["name"].startswith("workspace_")]
    assert all("project" in tool["inputSchema"]["required"] for tool in project_tools)
    assert "project" not in next(tool for tool in tools if tool["name"] == "list_projects")["inputSchema"]["required"]

    projects = await _post(app, connector_id, token,
                           _rpc("tools/call", {"name": "list_projects"}))
    payload = json.loads(projects.json()["result"]["content"][0]["text"])
    assert [(item["name"], item["access"]) for item in payload["projects"]] == [
        ("A", "write"), ("B", "write")
    ]
    assert payload["connector"]["id"] == connector_id


@pytest.mark.asyncio
async def test_retired_v3_fails_closed_and_current_catalog_is_bound(env):
    app, connector_id, _selected_id, token, seen = env
    retired = await _post(
        app, connector_id, token, _rpc("tools/list"), contract_version=3,
    )
    assert retired.status_code == 404

    listed = await _post(app, connector_id, token, _rpc("tools/list"))
    tools = listed.json()["result"]["tools"]
    # Audiobook M1 adds four book and three project-storage tools; Workspace is on by default.
    assert listed.status_code == 200 and len(tools) == 64
    assert all("outputSchema" in tool for tool in tools)

    current = await _post(app, connector_id, token, _rpc(
        "tools/call",
        {"name": "search_knowledge", "arguments": {"project": "A", "query": "v4"}},
    ))
    assert json.loads(current.json()["result"]["content"][0]["text"])["status"] == "success"
    assert seen[-1]["params"]["name"] == "search_knowledge"


@pytest.mark.asyncio
async def test_exact_project_is_stripped_and_batch_routes_each_element(env):
    app, connector_id, _selected_id, token, seen = env
    one = await _post(app, connector_id, token, _rpc(
        "tools/call", {"name": "search_knowledge", "arguments": {"project": "A", "query": "x"}}
    ))
    assert json.loads(one.json()["result"]["content"][0]["text"])["status"] == "success"
    assert seen[-1]["params"]["arguments"] == {"query": "x"}
    assert seen[-1]["_connector_header"] == connector_id

    batch = await _post(app, connector_id, token, [
        _rpc("tools/call", {"name": "search_knowledge", "arguments": {"project": "B", "query": "y"}}, 2),
        _rpc("tools/call", {"name": "search_knowledge", "arguments": {"project": "A", "query": "z"}}, 3),
    ])
    assert [item["id"] for item in batch.json()] == [2, 3]
    assert [item["params"]["arguments"]["query"] for item in seen[-2:]] == ["y", "z"]
    assert all("project" not in item["params"]["arguments"] for item in seen[-2:])


@pytest.mark.asyncio
async def test_read_only_and_invalid_projects_are_bounded(env):
    app, _connector_id, selected_id, token, seen = env
    denied = await _post(app, selected_id, token, _rpc(
        "tools/call", {"name": "update_document", "arguments": {"project": "A", "filepath": "x", "content": "y"}}
    ))
    denied_payload = json.loads(denied.json()["result"]["content"][0]["text"])
    assert denied.status_code == 200 and denied.json()["result"]["isError"] is True
    assert denied_payload["reason"] == "read_only"
    assert not any(item.get("params", {}).get("name") == "update_document" for item in seen)

    unknown = await _post(app, selected_id, token, _rpc(
        "tools/call", {"name": "search_knowledge", "arguments": {"project": "B", "query": "x"}}
    ))
    payload = json.loads(unknown.json()["result"]["content"][0]["text"])
    assert payload == {
        "status": "error", "reason": "project_unavailable",
        "message": "The requested project is unavailable through this connector.",
    }
    legacy = await _post(app, selected_id, token, _rpc("tools/call", {
        "name": "search_knowledge", "arguments": {"project": "../A", "query": "x"}
    }))
    assert json.loads(legacy.json()["result"]["content"][0]["text"])["reason"] == "project_unavailable"


@pytest.mark.asyncio
async def test_legacy_path_never_interpreted_as_connector(env):
    app, _connector_id, _selected_id, token, _seen = env
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://t") as client:
        response = await client.post("/mcp/A", json=_rpc("ping"),
                                     headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 404
