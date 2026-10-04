"""Project-identity stamping of tools/list descriptions (DESIGN.md §4.1 #3).

Multiple Cognita connectors in one claude.ai chat expose byte-identical
toolsets; the stamp prefixed to every tool description is what lets the model
pick the right knowledge base. A fake ASGI worker answers tools/list with
canned engine tools (same rig as test_edit_proxy.py).
"""

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from httpx import ASGITransport, AsyncClient

from cognita.auth_policy import AuthenticationPolicyStore
from cognita.config import CognitaConfig
from cognita.gateway import create_gateway_app
from cognita.connectors import ConnectorStore
from cognita.registry import Project, Registry
from engine_fakes import FakeEngineHost

ENGINE_TOOLS = [
    {"name": "search_knowledge", "description": "Search the knowledge base.",
     "inputSchema": {"type": "object"}},
    {"name": "list_documents", "description": "List all documents.",
     "inputSchema": {"type": "object"}},
    {"name": "update_document", "description": "Update a document.",
     "inputSchema": {"type": "object"}},
    {"name": "no_description_tool", "inputSchema": {"type": "object"}},
]


def make_fake_worker() -> FastAPI:
    app = FastAPI()

    @app.post("/mcp")
    async def mcp(request: Request):
        msg = await request.json()
        assert msg.get("method") == "tools/list"
        return JSONResponse({"jsonrpc": "2.0", "id": msg["id"],
                             "result": {"tools": ENGINE_TOOLS}})

    return app


@pytest.fixture
def env(tmp_path, full_mode_workspace_service):
    docs = tmp_path / "Altea Docs"
    docs.mkdir()

    registry = Registry(tmp_path / "registry.yaml")
    registry.add(Project(name="ALTEA", documents_dir=docs, data_dir=tmp_path / "d1"))
    registry.add(Project(name="ALTEA-RO", documents_dir=docs, data_dir=tmp_path / "d2",
                         writable=False))
    # 13.0 §7.3: the `config.test_mode` registry-token fallback this fixture
    # authenticated through is deleted. Two project-scoped keys from the
    # parent-owned policy store replace the two project tokens, so the
    # writable and read-only halves stay distinguishable by credential. OAuth
    # is off so an unknown bearer stays a 401, not a 503.
    auth = AuthenticationPolicyStore(
        tmp_path / "authentication.yaml", project_names=["ALTEA", "ALTEA-RO"]
    )
    auth.mutate_global(expected_revision=0, oauth_enabled=False, static_key_action="generate")
    tok_rw = auth.mutate_project(
        "ALTEA", expected_revision=1, static_key_action="generate")["generated_key"]
    tok_ro = auth.mutate_project(
        "ALTEA-RO", expected_revision=2, static_key_action="generate")["generated_key"]

    connector_store = ConnectorStore(tmp_path / "connectors.yaml")
    connector_store.create(
        expected_revision=0,
        name="Cognita",
        project_names=["ALTEA", "ALTEA-RO"],
        project_access={"ALTEA-RO": "read"},
    )

    config = CognitaConfig(
        registry_path=tmp_path / "registry.yaml",
        connectors_path=tmp_path / "connectors.yaml",
        data_root=tmp_path,
    )
    app = create_gateway_app(
        config, registry, engine=FakeEngineHost(make_fake_worker()),
        connector_store=connector_store,
        authentication_store=auth, workspace_service=full_mode_workspace_service,
    )
    connector = connector_store.snapshot().connectors[0]
    app.state.test_connector_id = connector.id
    app.state.test_connector_slug = connector.slug
    return app, tok_rw, tok_ro


async def tools_of(app, token) -> list[dict]:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.post(
            f"/mcp/connectors/{app.state.test_connector_slug}/mcp/v5",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                         headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    return r.json()["result"]["tools"]


STAMP = "[ALTEA knowledge base — folder: Altea Docs]"


# 1. every connector exposes the same stable catalog; no project is stamped.
async def test_all_descriptions_stamped_writable(env):
    app, tok_rw, _ = env
    tools = await tools_of(app, tok_rw)
    names = {t["name"] for t in tools}
    assert len(tools) == 57  # 13.0.1: 40 Knowledge + 17 Workspace tools, Workspace on by default
    assert "edit_document" in names
    for tool in tools:
        assert "folder:" not in tool.get("description", "").lower(), tool["name"]
        # Workspace tools are principal-scoped and take no `project` argument.
        if tool["name"] not in {"list_projects", "batch"} and not tool["name"].startswith("workspace_"):
            assert "project" in tool["inputSchema"]["required"], tool["name"]


# 2. descriptions explain explicit project routing.
async def test_original_description_preserved(env):
    app, tok_rw, _ = env
    tools = {t["name"]: t for t in await tools_of(app, tok_rw)}
    assert "ALTEA knowledge base" not in tools["search_knowledge"]["description"]


# 3. connector discovery is a first-class tool, not a project description stamp.
async def test_missing_description_gets_bare_stamp(env):
    app, tok_rw, _ = env
    tools = {t["name"]: t for t in await tools_of(app, tok_rw)}
    assert "list_projects" in tools
    assert "project" in tools["list_projects"]["description"].lower()


# 4. read-only access no longer changes discovery; write policy is checked on call.
async def test_readonly_list_stamped_with_own_name(env):
    app, _, tok_ro = env
    tools = await tools_of(app, tok_ro)
    names = {t["name"] for t in tools}
    assert "update_document" in names
    assert len(tools) == 57  # 13.0.1: 40 Knowledge + 17 Workspace tools, Workspace on by default


def test_public_catalog_has_no_project_or_folder_stamp():
    from cognita.proxy import public_tool_catalog

    tools = public_tool_catalog()
    assert len(tools) == 57
    assert all("folder:" not in (tool.get("description") or "").lower() for tool in tools)
