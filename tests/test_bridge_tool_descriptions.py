"""13.2.8: the copy tools tell the model where a selection lands.

DESIGN-13.2-CONNECTOR-DIAGNOSTICS §9. A selected FILE lands at
<destination>/<basename>, a selected DIRECTORY at <destination>/<its name>/ with
its subtree — so selecting a tree's files one by one flattens them. That has
always been the behavior; the description said only "copy selected files", and a
    client reported the flattening as a bug on 2026-09-23. This pins the
wording on the wire, in both directions, on the description and on the argument.
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from cognita.auth_policy import AuthenticationPolicyStore
from cognita.config import CognitaConfig
from cognita.connectors import ConnectorStore
from cognita.gateway import create_gateway_app
from cognita.proxy import BRIDGE_TOOL_DEFS
from cognita.registry import Project, Registry


def _placement_is_stated(tool: dict) -> None:
    description = tool["description"]
    assert "a selected FILE lands at <destination>/<basename>" in description
    assert "a selected DIRECTORY lands at <destination>/<its name>/ with its whole subtree" in description
    assert "flattens them into <destination>/" in description
    assert "build/a.md and build/b.md" in description and "build/sections/a.md" in description
    paths = tool["inputSchema"]["properties"]["paths"]
    assert "its own directory is NOT kept" in paths["description"]
    assert "Select a directory, not its files, to preserve layout." in paths["description"]
    destination = tool["inputSchema"]["properties"]["destination"]
    assert "\".\" or omitted means the root" in destination["description"]
    # The contract itself is untouched: same arguments, same required list.
    assert set(tool["inputSchema"]["properties"]) == {
        "project", "paths", "destination", "conflict_policy", "expected_destination_hashes", "idempotency_key",
    }
    assert tool["inputSchema"]["required"] == ["project", "paths"]


def test_both_bridge_definitions_state_the_placement_rule():
    for tool in BRIDGE_TOOL_DEFS:
        _placement_is_stated(tool)
    assert BRIDGE_TOOL_DEFS[0]["description"].startswith(
        "Copy selected Knowledge project files or directories into the authenticated Workspace.")
    assert BRIDGE_TOOL_DEFS[1]["description"].startswith(
        "Copy selected authenticated Workspace files or directories into the Knowledge project.")


@pytest.mark.asyncio
async def test_tools_list_serves_the_placement_rule_on_the_connector_route(
    tmp_path, full_mode_workspace_service,
):
    docs = tmp_path / "docs"
    docs.mkdir()
    registry = Registry(tmp_path / "registry.yaml")
    registry.add(Project(name="RW", documents_dir=docs, data_dir=tmp_path / "data"))
    auth = AuthenticationPolicyStore(tmp_path / "authentication.yaml", project_names=["RW"])
    token = auth.mutate_global(expected_revision=0, oauth_enabled=False,
                               static_key_action="generate")["generated_key"]
    store = ConnectorStore(tmp_path / "connectors.yaml")
    config = store.create(expected_revision=0, name="Bridge", project_names=["RW"],
                          default_workspace_transfer="allow")
    app = create_gateway_app(
        CognitaConfig(registry_path=registry.path, connectors_path=store.path, data_root=tmp_path),
        registry, connector_store=store, authentication_store=auth,
        workspace_service=full_mode_workspace_service,
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.post(f"/mcp/connectors/{config.connectors[0].slug}/mcp/v5",
                              json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                              headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200, r.text
    tools = {t["name"]: t for t in r.json()["result"]["tools"]}
    assert "copy_to_workspace" in tools and "copy_from_workspace" in tools
    _placement_is_stated(tools["copy_to_workspace"])
    _placement_is_stated(tools["copy_from_workspace"])
