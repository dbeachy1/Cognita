from __future__ import annotations

import asyncio
from types import SimpleNamespace

import httpx
import pytest
from argon2 import PasswordHasher
from httpx import ASGITransport, AsyncClient

import cognita.__main__ as main_module
import cognita.config as config_module
from cognita.admin_api import create_admin_app
from cognita.config import (
    CognitaConfig,
    ensure_oauth_service_key,
    oauth_service_key_path,
    oauth_service_readiness,
)
from cognita.connectors import ConnectorStore, build_connector_url, build_route_url
from cognita.gateway import create_gateway_app
from cognita.oauth_service_client import IntrospectionResult, OAuthServiceUnavailable
from cognita.registry import Project, Registry


class FakeOAuthClient:
    def __init__(self, resource: str, *, ready: bool = True) -> None:
        self.supervisor = SimpleNamespace(snapshot=SimpleNamespace(state="ready" if ready else "unavailable"))
        self.connections = [{
            "id": "connection-1", "client_id": "client", "client_name": "Test", "project": None,
            "connector": {
                "id": "2c520a44-2037-4bb5-a565-d88ec2bb02d1", "name": "Cognita", "enabled": True, "revision": 1,
                "projects": [{"name": "KEI", "access": "write"}],
            },
            "resource": resource, "created_at": "2026-09-13T00:00:00Z", "last_used_at": None,
        }]
        self.response = httpx.Response(200, headers=[("set-cookie", "a=1"), ("set-cookie", "b=2")], content=b"ok")
        self.unavailable = False
        self.readiness_checks = 0
        self.introspection = IntrospectionResult(
            True, ("cognita:access",), (resource,), "client", "subject",
            "7b979d72-037b-4c8e-b5f8-62390ed4e72a",
        )

    async def check_readiness(self):
        self.readiness_checks += 1
        return not self.unavailable and self.supervisor.snapshot.state == "ready"

    async def forward(self, request):
        if self.unavailable:
            raise OAuthServiceUnavailable("down")
        return self.response

    async def introspect(self, token):
        if self.unavailable:
            raise OAuthServiceUnavailable("down")
        return self.introspection

    async def list_connections(self):
        if self.unavailable:
            raise OAuthServiceUnavailable("down")
        return list(self.connections)

    async def revoke_connection(self, connection_id):
        if self.unavailable:
            raise OAuthServiceUnavailable("down")
        return 1 if connection_id == "connection-1" else 0

    async def revoke_all_connections(self):
        if self.unavailable:
            raise OAuthServiceUnavailable("down")
        return len(self.connections)


def _context(tmp_path):
    registry = Registry(tmp_path / "registry.yaml")
    registry.add(Project(name="KEI", documents_dir=tmp_path, data_dir=tmp_path))
    config = CognitaConfig(
        registry_path=registry.path,
        connectors_path=tmp_path / "connectors.yaml",
        data_root=tmp_path / "data",
        public_base_url="https://cognita.example",
        oauth_enabled=True,
        admin_allowed_hosts=["*"],
    )
    connector_store = ConnectorStore(config.connectors_path)
    connector_store.create(expected_revision=0, name="Cognita", project_names=["KEI"])
    connector = connector_store.snapshot().connectors[0]
    resource = build_connector_url(config.public_base_url, connector.slug)
    return config, registry, connector.slug, resource


@pytest.mark.parametrize(
    "host",
    ["*", ".chatgpt.com", "*.chatgpt.com", "https://chatgpt.com", "chatgpt.com."],
)
def test_cimd_allowlist_rejects_non_exact_hosts(host):
    with pytest.raises(ValueError, match="must be exact hostnames"):
        CognitaConfig(oauth_cimd_allowed_hosts=[host])


async def _client(app):
    return AsyncClient(transport=ASGITransport(app=app), base_url="https://cognita.example")


@pytest.mark.asyncio
async def test_gateway_oauth_health_and_proxy_use_shared_client(tmp_path):
    config, registry, connector_id, resource = _context(tmp_path)
    oauth = FakeOAuthClient(resource)
    app = create_gateway_app(config, registry, oauth_client=oauth)
    async with await _client(app) as client:
        health = await client.get("/healthz")
        forwarded = await client.get("/oauth/token")
        protected = await client.post(
                f"/mcp/connectors/{connector_id}/mcp/v5",
            json={
                "jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {
                    "name": "read_document",
                    "arguments": {"project": "KEI", "filepath": "missing.md"},
                },
            },
            headers={"Authorization": "Bearer token"},
        )
    assert health.json()["status"] == "ok"
    assert health.json()["oauth"] == {"status": "ready"}
    assert forwarded.status_code == 200
    assert forwarded.headers.get_list("set-cookie") == ["a=1", "b=2"]
    assert protected.status_code == 200
    assert protected.json()["result"]["isError"] is True


@pytest.mark.asyncio
async def test_stable_oauth_resource_succeeds_and_retired_route_fails_closed(tmp_path):
    config, registry, connector_slug, _current = _context(tmp_path)
    stable = build_route_url(config.public_base_url, "combined", connector_slug)
    oauth = FakeOAuthClient(stable)
    app = create_gateway_app(config, registry, oauth_client=oauth)
    request = {
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}},
    }
    headers = {"Authorization": "Bearer token"}

    async with await _client(app) as client:
        stable_response = await client.post(
            f"/mcp/connectors/{connector_slug}/mcp", json=request, headers=headers
        )
        retired = await client.post(
            f"/mcp/connectors/{connector_slug}/mcp/v3", json=request, headers=headers
        )

    assert stable_response.status_code == 200
    assert retired.status_code == 404


@pytest.mark.asyncio
async def test_gateway_unavailable_fails_closed_without_invalid_token_challenge(tmp_path):
    config, registry, connector_id, resource = _context(tmp_path)
    oauth = FakeOAuthClient(resource, ready=False)
    app = create_gateway_app(config, registry, oauth_client=oauth)
    async with await _client(app) as client:
        health = await client.get("/healthz")
        protected = await client.post(f"/mcp/connectors/{connector_id}/mcp/v5", json={})
        forwarded = await client.post("/oauth/token", data={"grant_type": "authorization_code"})
    assert health.status_code == 200
    assert health.json()["status"] == "degraded"
    assert health.json()["oauth"]["status"] == "unavailable"
    assert protected.status_code == 503
    assert "www-authenticate" not in protected.headers
    assert forwarded.status_code == 503
    assert forwarded.json() == {"error": "temporarily_unavailable"}


@pytest.mark.asyncio
async def test_gateway_rate_limits_registration_before_forwarding(tmp_path):
    config, registry, _connector_id, resource = _context(tmp_path)
    oauth = FakeOAuthClient(resource)
    app = create_gateway_app(config, registry, oauth_client=oauth)
    async with await _client(app) as client:
        responses = [
            await client.post(
                "/oauth/register",
                json={"redirect_uris": ["https://chatgpt.com/callback"]},
                headers={"CF-Connecting-IP": "203.0.113.40"},
            )
            for _ in range(11)
        ]
    assert [response.status_code for response in responses[:10]] == [200] * 10
    assert responses[10].status_code == 429
    assert responses[10].json()["error"] == "temporarily_unavailable"


@pytest.mark.asyncio
async def test_admin_projects_remain_usable_and_surface_oauth_outage(tmp_path):
    config, registry, _connector_id, resource = _context(tmp_path)
    oauth = FakeOAuthClient(resource, ready=True)
    app = create_admin_app(config, registry, oauth_client=oauth)
    async with await _client(app) as client:
        projects = await client.get("/api/projects")
        grants = await client.get("/api/oauth/grants")
        oauth.unavailable = True
        oauth.supervisor.snapshot.state = "unavailable"
        degraded_projects = await client.get("/api/projects")
        unavailable_grants = await client.get("/api/oauth/grants")
        status = await client.get("/api/oauth/status")
    assert projects.status_code == 200
    assert projects.json()["oauth_status"] == "ready"
    assert projects.json()["projects"][0]["connected_clients"] == 1
    assert grants.status_code == 200
    assert grants.json()["grants"][0]["id"] == "connection-1"
    assert degraded_projects.status_code == 200
    assert degraded_projects.json()["oauth_status"] == "unavailable"
    assert degraded_projects.json()["projects"][0]["connected_clients"] is None
    assert unavailable_grants.status_code == 503
    assert unavailable_grants.json()["detail"] == "OAuth service unavailable"
    assert status.json()["status"] == "unavailable"


def test_oauth_service_key_is_binary_safe_and_stable(tmp_path):
    config = CognitaConfig(data_root=tmp_path / "data")
    first = ensure_oauth_service_key(config)
    second = ensure_oauth_service_key(config)
    assert len(first) == 32
    assert second == first
    assert oauth_service_key_path(config).read_bytes() == first
    oauth_service_key_path(config).write_bytes(b"short")
    with pytest.raises(RuntimeError, match="exactly 32 bytes"):
        ensure_oauth_service_key(config)

def test_oauth_service_readiness_uses_child_key_without_legacy_secret(tmp_path, monkeypatch):
    config = CognitaConfig(
        data_root=tmp_path / "data",
        public_base_url="http://127.0.0.1:8675",
        admin_password_hash=PasswordHasher().hash("synthetic-admin-password"),
    )
    monkeypatch.setattr(config_module, "ensure_oauth_service_key", lambda _config: b"k" * 32)
    assert oauth_service_readiness(config) == []
    assert not (config.data_root / ".oauth-secret").exists()


def test_oauth_shutdown_timeout_is_bounded(tmp_path):
    with pytest.raises(ValueError, match="must not exceed 2"):
        CognitaConfig(data_root=tmp_path / "data", oauth_service_shutdown_timeout_s=2.01)

def test_parent_cli_exposes_one_shot_revocation_without_persistence(monkeypatch):
    seen = {}

    def capture(args):
        seen.update(vars(args))
        return 0

    monkeypatch.setattr(main_module, "cmd_serve", capture)
    monkeypatch.setattr(main_module, "_setup_logging", lambda _config: None)

    assert main_module.main(["serve", "--revoke-oauth-tokens-on-start"]) == 0
    assert seen["revoke_oauth_tokens_on_start"] is True
    assert not hasattr(CognitaConfig(), "revoke_oauth_tokens_on_start")

    assert main_module.main(["serve"]) == 0
    assert seen["revoke_oauth_tokens_on_start"] is False


def test_serve_passes_environment_selected_config_path_to_oauth_child(tmp_path, monkeypatch):
    config_path = tmp_path / "mounted" / "cognita.yaml"
    config_path.parent.mkdir()
    config_path.write_text("{}\n", encoding="utf-8")
    monkeypatch.setenv("COGNITA_CONFIG_PATH", str(config_path))
    monkeypatch.delenv("COGNITA_CONFIG_ROOT", raising=False)
    seen = {}

    async def capture(config, *, config_path=None, revoke_oauth_tokens_on_start=False):
        seen["config_path"] = config_path
        seen["revoke_oauth_tokens_on_start"] = revoke_oauth_tokens_on_start

    monkeypatch.setattr(main_module, "_serve_async", capture)
    args = SimpleNamespace(
        stdio=False,
        project=None,
        test=False,
        revoke_oauth_tokens_on_start=False,
    )

    assert main_module.cmd_serve(args) == 0
    assert seen["config_path"] == config_path


@pytest.mark.asyncio
async def test_healthz_checks_readiness_on_demand_without_background_polling(tmp_path):
    config, registry, _connector_id, resource = _context(tmp_path)
    oauth = FakeOAuthClient(resource)
    app = create_gateway_app(config, registry, oauth_client=oauth)
    async with await _client(app) as client:
        health = await client.get("/healthz")
        observed = oauth.readiness_checks
        # Instead of napping 30ms, hand the loop a bounded number of turns: any
        # task the request scheduled (a background poller's first check runs on
        # its first turn) gets to run, with no dependence on elapsed time.
        for _ in range(20):
            await asyncio.sleep(0)
    assert health.json()["oauth"]["status"] == "ready"
    assert observed == 1
    assert oauth.readiness_checks == observed
