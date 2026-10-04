"""Focused Milestone-2 Admin surface contracts.

The worker implementations are intentionally replaced with tiny fakes here.
These tests verify that the HTTP layer keeps surfaces independent, returns only
nonsecret list metadata, gates high-trust mutations, and delegates encryption /
password proof / setup rendering to the credential worker.
"""

from __future__ import annotations


import pytest
from httpx import ASGITransport, AsyncClient

from cognita.admin_api import create_admin_app
from cognita.config import CognitaConfig
from cognita.registry import Registry
from cognita.tokens import hash_token


class WorkspaceStore:
    def __init__(self):
        self.revision = 0
        self.rows = []

    def snapshot(self):
        return {"revision": self.revision, "workspace_connectors": list(self.rows)}

    def create(self, *, expected_revision, display_name, enabled=False, slug=None):
        assert expected_revision == self.revision
        row = {"id": "ws-1", "slug": slug or "workspace", "display_name": display_name,
               "enabled": enabled, "revision": 1}
        self.rows.append(row)
        self.revision += 1
        return row

    def delete(self, surface_id, *, expected_revision, confirm):
        assert confirm and expected_revision == self.revision
        self.rows = [row for row in self.rows if row["id"] != surface_id]
        self.revision += 1
        return {"revision": self.revision}


class CredentialStore:
    def __init__(self):
        self.rows = {}

    def list_credentials(self, *, surface_kind, surface_id):
        return {"revision": 4, "credentials": self.rows.get((surface_kind, surface_id), [])}

    def create_credential(self, *, surface_kind, surface_id, expected_revision, label):
        row = {"credential_id": "cred-1", "key_id": "key-1", "label": label,
               "status": "active", "secret": "cog_sk_v2_cred.secret"}
        self.rows.setdefault((surface_kind, surface_id), []).append(row)
        return {"revision": expected_revision + 1, "credential": row,
                "secret": row["secret"]}

    def reveal_credential(self, *, surface_kind, surface_id, credential_id,
                          expected_revision, current_password):
        assert current_password == "current"
        return {"revision": expected_revision + 1, "secret": "cog_sk_v2_cred.secret"}

    def setup_material(self, **kwargs):
        return {"url": "https://example.test/mcp/workspace/ws/mcp",
                "sillytavern": {"type": "streamable-http", "headers": {
                    "Authorization": "Bearer cog_sk_v2_cred.secret"}}}


@pytest.fixture
def context(tmp_path):
    registry = Registry(tmp_path / "registry.yaml")
    config = CognitaConfig(
        registry_path=registry.path,
        data_root=tmp_path / "data",
        public_base_url="https://example.test",
        admin_allowed_hosts=["*"],
        admin_username="admin",
        admin_password_sha256=hash_token("password"),
    )
    workspace = WorkspaceStore()
    credentials = CredentialStore()
    app = create_admin_app(config, registry, workspace_connector_store=workspace,
                           credential_store=credentials)
    return app


async def login(client: AsyncClient):
    response = await client.post("/api/login", json={"username": "admin", "password": "password"})
    assert response.status_code == 200
    client.headers["X-CSRF-Token"] = client.cookies.get("cognita_csrf")


@pytest.mark.anyio
async def test_workspace_surface_is_independent_and_high_trust_confirmed(context):
    async with AsyncClient(transport=ASGITransport(app=context), base_url="http://test") as client:
        await login(client)
        denied = await client.post("/api/workspace-connectors", json={"expected_revision": 0, "display_name": "WS", "confirm_high_trust": False})
        assert denied.status_code == 400
        created = await client.post("/api/workspace-connectors", json={"expected_revision": 0, "display_name": "WS", "slug": "ws", "confirm_high_trust": True})
        assert created.status_code == 201
        listed = await client.get("/api/workspace-connectors")
        assert listed.json()["workspace_connectors"][0]["id"] == "ws-1"
        assert listed.json()["workspace_connectors"][0]["revision"] == 1


@pytest.mark.anyio
async def test_credential_list_is_nonsecret_and_setup_is_server_computed(context):
    async with AsyncClient(transport=ASGITransport(app=context), base_url="http://test") as client:
        await login(client)
        created = await client.post("/api/workspace-connectors", json={"expected_revision": 0, "display_name": "WS", "slug": "ws", "confirm_high_trust": True})
        assert created.status_code == 201
        added = await client.post("/api/workspace-connectors/ws-1/credentials", json={"expected_revision": 0, "label": "SillyTavern"})
        assert added.status_code == 201
        assert added.json()["secret"].startswith("cog_sk_v2_")
        listed = await client.get("/api/workspace-connectors/ws-1/credentials")
        assert "secret" not in listed.text
        revealed = await client.post("/api/workspace-connectors/ws-1/credentials/cred-1/reveal", json={"expected_revision": 4, "current_password": "current"})
        assert revealed.status_code == 200
        assert revealed.headers["cache-control"] == "no-store"
        setup = await client.post("/api/workspace-connectors/ws-1/credentials/cred-1/setup", json={"route_strategy": "stable", "provider": "sillytavern", "current_password": "current"})
        assert setup.status_code == 200
        assert setup.json()["sillytavern"]["type"] == "streamable-http"


def test_credential_metadata_reads_the_real_slotted_record():
    """13.2.1 (LIVE BUG): the store hands the route CredentialRecord objects,
    a slots=True dataclass with no __dict__; the list rendered every key as
    "Unnamed" with no ID and every row action answered not found."""
    from cognita.admin_api import _credential_metadata
    from cognita.auth_policy import CredentialRecord

    record = CredentialRecord(
        credential_id="e0a1315b-194a-4020-ad43-9ed5b2ae55c2", principal_id="p-1",
        surface_kind="combined", surface_id="9b6812eb-bb42-4e4b-b1b0-e837fcc552d0",
        label="operator", key_id="k-1", digest="d" * 64, created_at="2026-09-23T01:30:00+00:00",
        status="active", encrypted_secret={"ciphertext": "never-shown"},
    )
    out = _credential_metadata(record)
    assert out["label"] == "operator"
    assert out["credential_id"] == "e0a1315b-194a-4020-ad43-9ed5b2ae55c2"
    assert out["status"] == "active"
    assert out["key_id"] == "k-1"
    for secret in ("encrypted_secret", "digest", "ciphertext", "never-shown"):
        assert secret not in repr(out)


@pytest.mark.anyio
async def test_private_bearer_token_key_panel_works_end_to_end_on_the_real_store(tmp_path):
    """13.2.1: the whole key panel through the REAL store, not a dict fake.

    Doug, 2026-09-22, after Add key answered 409 for the life of the feature
    and then a created key listed as "Unnamed" with no ID so Reveal said
    "not found" and Delete said "method not allowed": "Dude, you should have
    a spec for this." This is it: list, create, list again with label and
    ID, reveal, rotate, revoke, delete -- every request the page makes, with
    the revision the page reads from the previous response.
    """
    from contextlib import nullcontext
    from types import SimpleNamespace

    from argon2 import PasswordHasher

    from cognita.auth_policy import CredentialAdminService, CredentialPolicyStore

    connector_id = "9b6812eb-bb42-4e4b-b1b0-e837fcc552d0"
    store = CredentialPolicyStore(
        tmp_path / "credentials-v2.json", master_key_dir=tmp_path / "keys",
        admin_password_hash=PasswordHasher().hash("password"),
    )
    lifecycle = SimpleNamespace(
        credential_deletion_gate=lambda _credential_id: nullcontext(),
        validate_credential_workspace=lambda **kwargs: None,
        reconcile_credential=lambda **kwargs: {"complete": True, "workspace_deleted": False},
    )
    service = CredentialAdminService(
        store,
        surface_resolver=lambda kind, identifier: {"id": connector_id, "slug": "cognita"}
        if (kind, identifier) == ("combined", connector_id) else None,
        public_base_url="https://example.test", workspace_lifecycle=lifecycle,
    )
    registry = Registry(tmp_path / "registry.yaml")
    config = CognitaConfig(
        registry_path=registry.path, data_root=tmp_path / "data",
        public_base_url="https://example.test", admin_allowed_hosts=["*"],
        admin_username="admin", admin_password_sha256=hash_token("password"),
    )
    app = create_admin_app(config, registry, workspace_connector_store=WorkspaceStore(),
                           credential_store=service)
    base = f"/api/connectors/{connector_id}/credentials"
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        await login(client)

        listed = await client.get(base)
        assert listed.status_code == 200, listed.text
        assert listed.json()["credentials"] == []
        revision = listed.json()["revision"]

        created = await client.post(base, json={
            "expected_revision": revision, "label": "operator", "current_password": "password",
        })
        assert created.status_code == 201, created.text
        body = created.json()
        assert body["secret"].startswith("cog_sk_v2_")
        assert body["credential"]["label"] == "operator"
        credential_id = body["credential"]["credential_id"]
        assert credential_id and credential_id != "None"

        listed = await client.get(base)
        rows = listed.json()["credentials"]
        assert [(row["label"], row["credential_id"], row["status"]) for row in rows] == [
            ("operator", credential_id, "active"),
        ]
        assert "secret" not in listed.text and "encrypted_secret" not in listed.text
        revision = listed.json()["revision"]

        revealed = await client.post(f"{base}/{credential_id}/reveal", json={
            "expected_revision": revision, "current_password": "password",
        })
        assert revealed.status_code == 200, revealed.text
        assert revealed.json()["secret"] == body["secret"]
        revision = revealed.json().get("revision", revision)

        rotated = await client.post(f"{base}/{credential_id}/rotate", json={
            "expected_revision": revision, "current_password": "password",
        })
        assert rotated.status_code == 200, rotated.text
        assert rotated.json()["secret"].startswith("cog_sk_v2_") and rotated.json()["secret"] != body["secret"]
        revision = rotated.json()["revision"]

        revoked = await client.post(f"{base}/{credential_id}/revoke", json={
            "expected_revision": revision, "confirm": False,
        })
        assert revoked.status_code == 200, revoked.text
        revision = revoked.json()["revision"]
        listed = await client.get(base)
        assert listed.json()["credentials"][0]["status"] == "revoked"

        deleted = await client.request("DELETE", f"{base}/{credential_id}", json={
            "expected_revision": revision, "confirm": True, "retention": "normal",
            "workspace_id": None, "workspace_revision": None,
        })
        assert deleted.status_code == 200, deleted.text
        listed = await client.get(base)
        assert listed.json()["credentials"] == []
