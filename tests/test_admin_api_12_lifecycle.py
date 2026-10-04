"""Focused Admin lifecycle/settings seam tests for Cognita 12 milestone 3."""

from __future__ import annotations

from httpx import ASGITransport, AsyncClient

from cognita.admin_api import create_admin_app
from cognita.config import CognitaConfig
from cognita.registry import Registry
from cognita.tokens import hash_token


class LifecycleService:
    def __init__(self):
        self.rows = [{
            "workspace_id": "workspace-1", "credential_id": "cred-1", "key_id": "key-1",
            "credential_label": "SillyTavern", "connector_name": "WS", "state": "stopped",
            "created_at": "2026-09-17T00:00:00Z", "last_activity_at": "2026-09-17T01:00:00Z",
            "deletion_due_at": "2026-10-17T01:00:00Z", "actual_bytes": 123,
            "apparent_bytes": 456, "quota_bytes": 4 * 1024**3, "quota_percent": 0,
            "host_path": "/data/workspaces/cred-1", "container_path": "/var/lib/cognita/workspaces/cred-1",
            "pinned": False, "revision": 3,
        }]
        self.bulk_calls = []
        self.settings = {"revision": 7, "retention_days": 30, "quota_bytes": 4 * 1024**3,
                         "idle_stop_seconds": 1800, "host_reserve_bytes": 1024,
                         "network_mode": "off", "network_rules": [], "brave_enabled": False,
                         "brave_configured": False}

    def list_admin_workspaces(self, **kwargs):
        assert kwargs["sort"] == "actual_allocation"
        return {"revision": 3, "workspaces": self.rows,
                "storage": {"host_root": "/data/workspaces", "usable_capacity_bytes": 1000},
                "runtime": {"status": "ok", "running_count": 0, "capacity": 4}}

    def runtime_health(self):
        return {"status": "ok", "runtime": "ready", "runtime_version": "0.7.0", "running_count": 0}

    def workspace_action(self, action, workspace_id, **kwargs):
        assert workspace_id == "workspace-1"
        return {"revision": 4, "workspace": {**self.rows[0], "state": action}}

    def bulk_workspace_action(self, action, workspace_ids, **kwargs):
        self.bulk_calls.append((action, workspace_ids, kwargs))
        assert action == "remove" and workspace_ids == ["workspace-1"]
        return {"revision": 5, "removed": workspace_ids}

    def preview_bulk_workspace_action(self, action, workspace_ids, **kwargs):
        return {
            "preview_token": "preview-1", "expires_at": "2026-09-19T01:00:00Z",
            "targets": self.rows, "reclaim_estimate_bytes": 123,
            "reclaim_estimate_status": "verified",
        }

    def apply_bulk_workspace_action(self, action, workspace_ids, **kwargs):
        self.bulk_calls.append((action, workspace_ids, kwargs))
        assert kwargs["preview_token"] == "preview-1"
        return {"revision": 5, "removed": workspace_ids}

    def get_workspace_settings(self):
        return self.settings

    def preview_workspace_settings(self, values):
        return {
            "status": "valid",
            "network_mode": values.get("network_mode"),
            "brave_enabled": values.get("brave_enabled"),
        }

    def update_workspace_settings(self, values, **kwargs):
        assert values.pop("brave_api_key") == "secret-value"
        self.settings.update(values)
        self.settings["revision"] += 1
        return self.settings

    def test_brave_search(self):
        return {"ok": True, "category": "success", "api_key": "must-not-escape"}


async def _context(tmp_path):
    registry = Registry(tmp_path / "registry.yaml")
    config = CognitaConfig(registry_path=registry.path, data_root=tmp_path / "data",
                           public_base_url="https://example.test", admin_allowed_hosts=["*"],
                           admin_username="admin", admin_password_sha256=hash_token("password"))
    service = LifecycleService()
    return create_admin_app(config, registry, workspace_service=service), service


async def _login(client: AsyncClient):
    response = await client.post("/api/login", json={"username": "admin", "password": "password"})
    assert response.status_code == 200
    client.headers["X-CSRF-Token"] = client.cookies.get("cognita_csrf")


async def test_admin_lifecycle_is_server_computed_and_secret_free(tmp_path):
    app, service = await _context(tmp_path)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        await _login(client)
        listed = await client.get("/api/workspaces?sort=actual_allocation&direction=desc")
        assert listed.status_code == 200
        assert listed.headers["cache-control"] == "no-store"
        assert listed.json()["workspaces"][0]["actual_bytes"] == 123
        assert listed.json()["storage"]["host_root"] == "/data/workspaces"
        runtime = await client.get("/api/workspaces/health")
        assert runtime.json()["runtime"]["runtime_version"] == "0.7.0"
        started = await client.post("/api/workspaces/workspace-1/start", json={"expected_revision": 3})
        assert started.status_code == 200
        assert started.json()["workspace"]["state"] == "start"


async def test_admin_lifecycle_destructive_and_settings_gates(tmp_path):
    app, service = await _context(tmp_path)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        await _login(client)
        denied = await client.post("/api/workspaces/workspace-1/remove", json={"expected_revision": 3})
        assert denied.status_code == 400
        denied_bulk = await client.post("/api/workspaces/bulk", json={
            "action": "remove", "workspace_ids": ["workspace-1"],
            "expected_revisions": {"workspace-1": 3}, "confirm": True,
        })
        assert denied_bulk.status_code == 400
        assert service.bulk_calls == []
        preview = await client.post("/api/workspaces/bulk/preview", json={
            "action": "remove", "workspace_ids": ["workspace-1"],
            "expected_revisions": {"workspace-1": 3},
        })
        assert preview.status_code == 200
        removed = await client.post("/api/workspaces/bulk", json={
            "action": "remove", "workspace_ids": ["workspace-1"],
            "expected_revisions": {"workspace-1": 3}, "confirm": True,
            "preview_token": preview.json()["preview_token"],
        })
        assert removed.status_code == 200
        assert len(service.bulk_calls) == 1
        preview = await client.post("/api/workspace-settings/preview", json={
            "expected_revision": 7, "network_mode": "unrestricted_public",
            "confirm_high_trust": False,
        })
        assert preview.status_code == 400
        saved = await client.patch("/api/workspace-settings", json={
            "expected_revision": 7, "network_mode": "off", "brave_enabled": False,
            "brave_api_key": "secret-value", "confirm_high_trust": True,
        })
        assert saved.status_code == 200
        assert "brave_api_key" not in saved.text
        tested = await client.post("/api/workspace-settings/test-brave", json={})
        assert tested.status_code == 200
        assert "must-not-escape" not in tested.text
