"""Cognita 12 independent route-family and policy contracts."""

import uuid

import pytest
from argon2 import PasswordHasher
from httpx import ASGITransport, AsyncClient

from cognita.auth_policy import CredentialPolicyStore
from cognita.config import CognitaConfig
from cognita.connectors import (
    ConnectorDefinition,
    ConnectorStore,
    PUBLIC_CONTRACT_VERSION,
    RouteResource,
    RevisionConflict,
    WorkspaceConnectorStore,
    is_supported_route,
    parse_route_path,
    supported_route_versions,
)
from cognita.gateway import create_gateway_app
from cognita.registry import Project, Registry


def test_route_families_accept_stable_and_exact_generation_window():
    assert parse_route_path("/mcp/connectors/primary/mcp") == RouteResource("combined", "primary")
    assert parse_route_path(f"/mcp/connectors/primary/mcp/v{PUBLIC_CONTRACT_VERSION}") == RouteResource(
        "combined", "primary", PUBLIC_CONTRACT_VERSION,
    )
    assert parse_route_path("/mcp/workspace/tools/mcp") == RouteResource("workspace", "tools")
    assert parse_route_path("/mcp/workspace/tools/mcp/v3") == RouteResource("workspace", "tools", 3)
    assert supported_route_versions("combined") == (PUBLIC_CONTRACT_VERSION,)
    assert supported_route_versions("workspace") == (3,)
    assert is_supported_route(RouteResource("combined", "primary"))
    assert is_supported_route(RouteResource("combined", "primary", PUBLIC_CONTRACT_VERSION))
    assert not is_supported_route(RouteResource("combined", "primary", PUBLIC_CONTRACT_VERSION - 1))
    assert not is_supported_route(RouteResource("combined", "primary", PUBLIC_CONTRACT_VERSION + 1))
    assert is_supported_route(RouteResource("workspace", "tools"))
    assert is_supported_route(RouteResource("workspace", "tools", 3))
    assert not is_supported_route(RouteResource("workspace", "tools", 2))
    assert not is_supported_route(RouteResource("workspace", "tools", 4))


@pytest.mark.parametrize(
    "path",
    [
        "/mcp/connectors/primary/mcp/",
        "/mcp/connectors/primary/mcp/v03",
        "/mcp/connectors/primary/mcp/v0",
        "/mcp/connectors/primary/mcp/v2",
        "/mcp/connectors/primary/mcp/v3",
        f"/mcp/connectors/primary/mcp/v{PUBLIC_CONTRACT_VERSION + 1}",
        f"/mcp/connectors/primary/mcp/v{PUBLIC_CONTRACT_VERSION}?x=5",
        "/mcp/connectors/primary/mcp?x=4",
        "/mcp/workspace/tools/mcp/",
        "/mcp/workspace/tools/mcp/v01",
        "/mcp/workspace/tools/mcp/v1",
        "/mcp/workspace/tools/mcp/v1?x=1",
    ],
)
def test_route_parser_rejects_noncanonical_paths(path):
    parsed = parse_route_path(path)
    assert parsed is None or not is_supported_route(parsed)


def test_connector_workspace_defaults_are_on_and_transfer_is_independent():
    # 13.0.1: a new connector gets Workspace tools and transfer by default
    # (Doug, 2026-09-22); a per-project deny still wins over the default.
    connector = ConnectorDefinition(id=str(uuid.uuid4()), name="Primary")
    assert connector.workspace_enabled is True
    assert connector.default_workspace_transfer == "allow"
    assert connector.transfer_allowed("Project") is True
    restricted = connector.model_copy(update={
        "project_transfer": {"Project": "deny"},
    })
    assert restricted.transfer_allowed("Other") is True
    assert restricted.transfer_allowed("Project") is False
    off = connector.model_copy(update={"workspace_enabled": False})
    assert off.transfer_allowed("Other") is False


def test_workspace_connector_store_is_independent_revisioned_and_durable(tmp_path):
    path = tmp_path / "workspace-connectors.yaml"
    store = WorkspaceConnectorStore(path)
    created = store.create(
        expected_revision=0, display_name="Runner", enabled=True, slug="runner"
    )
    assert created.slug == "runner" and created.enabled is True
    assert store.snapshot().revision == 1
    updated = store.update(
        created.id, expected_revision=1, display_name="Build Runner", enabled=False
    )
    assert updated.id == created.id and updated.slug == created.slug
    assert updated.revision == 2 and updated.enabled is False
    with pytest.raises(RevisionConflict):
        store.update(created.id, expected_revision=1, enabled=True)
    reloaded = WorkspaceConnectorStore(path).snapshot()
    assert reloaded.revision == 2
    assert reloaded.workspace_connectors == [updated]
    disabled = store.delete(created.id, expected_revision=2, confirm=True)
    assert disabled == {"deleted": created.id, "revision": 3}
    retained = WorkspaceConnectorStore(path).snapshot().workspace_connectors
    assert len(retained) == 1 and retained[0].id == created.id
    assert retained[0].enabled is False and retained[0].revision == 3


@pytest.mark.anyio
async def test_named_credential_auth_and_current_catalog_follow_connector_policy(
    tmp_path, monkeypatch, full_mode_workspace_service,
):
    registry = Registry(tmp_path / "registry.yaml")
    registry.add(Project(name="Knowledge", documents_dir=tmp_path, data_dir=tmp_path))
    connectors = ConnectorStore(tmp_path / "connectors.yaml")
    # 13.0.1: Workspace is on by default, so this test turns it off explicitly
    # to keep proving the catalog follows the policy in both directions.
    snapshot = connectors.create(
        expected_revision=0, name="Primary", project_names=["Knowledge"],
        workspace_enabled=False, default_workspace_transfer="deny",
    )
    surface = snapshot.connectors[0]
    credentials = CredentialPolicyStore(
        tmp_path / "credentials-v2.json",
        master_key_dir=tmp_path / "master-keys",
        admin_password_hash=PasswordHasher().hash("correct horse"),
    )
    _row, secret = credentials.add_credential(
        "combined", surface.id, "Client", surface_slug=surface.slug,
        password="correct horse",
    )
    verification_calls = []
    original_verify = credentials.verify_for_surface

    def recording_verify(presented, **kwargs):
        verification_calls.append(kwargs["surface_kind"])
        return original_verify(presented, **kwargs)

    monkeypatch.setattr(credentials, "verify_for_surface", recording_verify)
    config = CognitaConfig(
        registry_path=registry.path,
        connectors_path=connectors.path,
        public_base_url="https://example.test",
        oauth_enabled=False,
    )
    app = create_gateway_app(
        config, registry, connector_store=connectors,
        credential_store=credentials, workspace_service=full_mode_workspace_service,
    )
    request = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    headers = {"Authorization": f"Bearer {secret}"}
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://example.test"
    ) as client:
        disabled = await client.post(
            f"/mcp/connectors/{surface.slug}/mcp/v{PUBLIC_CONTRACT_VERSION}", json=request, headers=headers
        )
        connectors.update(
            surface.id, expected_revision=1, project_names=["Knowledge"],
            workspace_enabled=True,
        )
        enabled = await client.post(
            f"/mcp/connectors/{surface.slug}/mcp/v{PUBLIC_CONTRACT_VERSION}", json=request, headers=headers
        )
        connectors.update(
            surface.id, expected_revision=2, project_names=["Knowledge"],
            default_workspace_transfer="allow",
        )
        bridge = await client.post(
            f"/mcp/connectors/{surface.slug}/mcp/v{PUBLIC_CONTRACT_VERSION}", json=request, headers=headers
        )
        stable = await client.post(
            f"/mcp/connectors/{surface.slug}/mcp", json=request, headers=headers
        )
        stable_query = await client.post(
            f"/mcp/connectors/{surface.slug}/mcp?alias=1", json=request, headers=headers
        )
        stable_trailing = await client.post(
            f"/mcp/connectors/{surface.slug}/mcp/", json=request, headers=headers
        )
        stable_alias = await client.post(
            f"/mcp/connectors/{surface.slug}/mcp/stable", json=request, headers=headers
        )
        encoded_current_alias = await client.post(
            f"/mcp/connectors/{surface.slug}/mcp/%76%{PUBLIC_CONTRACT_VERSION}", json=request, headers=headers
        )
        retired = await client.post(
            f"/mcp/connectors/{surface.slug}/mcp/v{PUBLIC_CONTRACT_VERSION - 1}", json=request, headers=headers
        )
        future = await client.post(
            f"/mcp/connectors/{surface.slug}/mcp/v{PUBLIC_CONTRACT_VERSION + 1}", json=request, headers=headers
        )
    assert disabled.status_code == 200
    assert not any(
        item["name"].startswith("workspace_")
        for item in disabled.json()["result"]["tools"]
    )
    enabled_names = {item["name"] for item in enabled.json()["result"]["tools"]}
    assert "workspace_info" in enabled_names
    assert "copy_to_workspace" not in enabled_names
    bridge_names = {item["name"] for item in bridge.json()["result"]["tools"]}
    assert {"copy_to_workspace", "copy_from_workspace"} <= bridge_names
    assert stable.status_code == bridge.status_code == 200
    assert stable.json()["result"]["tools"] == bridge.json()["result"]["tools"]
    assert stable_query.status_code == stable_trailing.status_code == 404
    assert stable_alias.status_code == encoded_current_alias.status_code == 404
    assert retired.status_code == future.status_code == 404
    assert verification_calls == ["combined"] * 4


@pytest.mark.anyio
async def test_workspace_stable_and_v3_authenticate_and_retired_routes_fail_closed(tmp_path, monkeypatch):
    registry = Registry(tmp_path / "registry.yaml")
    registry.add(Project(name="Knowledge", documents_dir=tmp_path, data_dir=tmp_path))
    workspace = WorkspaceConnectorStore(tmp_path / "workspace-connectors.yaml")
    surface = workspace.create(
        expected_revision=0, display_name="Workspace", enabled=True, slug="workspace"
    )
    credentials = CredentialPolicyStore(
        tmp_path / "credentials-v2.json",
        master_key_dir=tmp_path / "master-keys",
        admin_password_hash=PasswordHasher().hash("correct horse"),
    )
    _row, secret = credentials.add_credential(
        "workspace", surface.id, "Runner", surface_slug=surface.slug,
        password="correct horse",
    )
    verification_calls = []
    original_verify = credentials.verify_for_surface

    def recording_verify(presented, **kwargs):
        verification_calls.append(kwargs["surface_kind"])
        return original_verify(presented, **kwargs)

    monkeypatch.setattr(credentials, "verify_for_surface", recording_verify)
    config = CognitaConfig(
        registry_path=registry.path,
        public_base_url="https://example.test",
        oauth_enabled=False,
    )
    app = create_gateway_app(
        config, registry,
        connector_store=ConnectorStore(tmp_path / "connectors.yaml"),
        workspace_connector_store=workspace,
        credential_store=credentials,
    )
    request = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    headers = {"Authorization": f"Bearer {secret}"}
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://example.test"
    ) as client:
        stable = await client.post("/mcp/workspace/workspace/mcp", json=request, headers=headers)
        current = await client.post("/mcp/workspace/workspace/mcp/v3", json=request, headers=headers)
        retired = await client.post("/mcp/workspace/workspace/mcp/v2", json=request, headers=headers)
        future = await client.post("/mcp/workspace/workspace/mcp/v4", json=request, headers=headers)

    assert stable.status_code == current.status_code == 200
    assert stable.json() == current.json()
    assert retired.status_code == future.status_code == 404
    assert verification_calls == ["workspace"] * 2
