"""Focused 12.6 Admin-to-Workspace domain wiring tests."""

from __future__ import annotations

from contextlib import nullcontext
from pathlib import Path
import threading
from types import SimpleNamespace

import pytest
from argon2 import PasswordHasher
from httpx import ASGITransport, AsyncClient

from cognita.admin_api import create_admin_app
from cognita.auth_policy import CredentialAdminService, CredentialPolicyStore, CredentialWorkspaceConflict
from cognita.config import CognitaConfig
from cognita.localization import load_catalog
from cognita.oauth_service.principal import OAuthPrincipalStore
from cognita.registry import Registry
from cognita.tokens import hash_token
from cognita.workspace import WorkspaceError, WorkspaceManager, WorkspaceMetadataStore, _digest
from cognita.workspace_admin import WorkspaceAdminAdapter, settings_view


def test_admin_domain_editor_normalizes_new_rules_and_marks_legacy_rules():
    current = {"network_mode": "allowlist", "network_rules": [], "revision": 4}
    new_policy = WorkspaceAdminAdapter._normalized_policy({
        "network_mode": "allowlist",
        "network_rules": [{"domain": "example.com", "ports": [443]}],
    }, current)
    assert new_policy.rules[0].protocols == ("http", "https")
    shorthand = WorkspaceAdminAdapter._normalized_policy({
        "network_mode": "allowlist", "network_rules": ["example.com"],
    }, current)
    assert shorthand.rules[0].ports == (80, 443)
    legacy_policy = WorkspaceAdminAdapter._normalized_policy({
        "network_mode": "allowlist",
        "network_rules": [{
            "domain": "legacy.example", "ports": [443], "protocols": ["https"],
        }],
    }, current)
    assert legacy_policy.rules[0].protocols == ("https",)
    visible = settings_view({
        "network_mode": "allowlist",
        "network_rules": [legacy_policy.rules[0].to_mapping()],
    })
    assert visible["network_rules"][0]["availability"] == "unavailable"
    assert "Edit this rule" in visible["network_rules"][0]["availability_reason"]


def test_admin_network_editor_has_domain_port_controls_without_scheme_selector():
    index = Path("src/cognita/web/index.html").read_text(encoding="utf-8")
    app = Path("src/cognita/web/app.js").read_text(encoding="utf-8")
    assert "Allowed outbound domains and ports" in index
    assert "data-network-suffix" in app
    assert "data-network-ports" in app
    assert "data-network-enable-legacy" in app
    assert 't("admin.workspace.network.enable_http_https")' in app
    assert 't("admin.workspace.network.legacy_rule_unavailable")' in app
    for key in (
        "domain", "match", "exact_domain", "domain_and_subdomains", "tcp_ports", "remove",
    ):
        assert f't("admin.workspace.network.{key}")' in app
    assert "ports: [80, 443]" in app
    assert "workspaceNetworkLegacyOverrides" in app
    assert "explicitlyEnabled" in app
    assert 'protocols: ["http", "https"]' in app
    assert "legacy.protocols.slice()" in app
    assert "workspaceNetworkPayload" in app
    assert 't("admin.workspace.network.ports_integers"' in app
    assert 't("admin.workspace.network.ports_range"' in app
    assert 't("admin.status.unknown")' in app
    assert "must be comma-separated integers" not in app
    assert "between 1 and 65535" not in app
    assert ".filter(Number.isFinite)" not in app
    assert 'protocols:["https"]' not in index


class DomainSpy:
    def __init__(self) -> None:
        self.preview_calls: list[tuple] = []
        self.apply_calls: list[tuple] = []
        self.metadata = SimpleNamespace(settings=lambda: {
            "retention_days": 30, "quota_bytes": 4 * 1024**3,
            "idle_stop_seconds": 1800, "host_reserve_bytes": 1024,
            "network_mode": "off", "network_rules": [],
            "brave_enabled": False, "max_running_workspaces": 4,
        })

    def preview_bulk_workspace_action(self, action, workspace_ids, *, expected_revisions):
        self.preview_calls.append((action, list(workspace_ids), dict(expected_revisions)))
        return {
            "token": "domain-preview-token", "expires_at": "2026-09-19T01:00:00+00:00",
            "targets": [{"workspace_id": workspace_ids[0], "state": "stopped", "actual_bytes": 12}],
            "reclaim_bytes": 12,
        }

    def apply_bulk_workspace_action(self, action, workspace_ids, *, expected_revisions,
                                    preview_token, idempotency_token, confirm_high_trust=False):
        self.apply_calls.append((
            action, list(workspace_ids), dict(expected_revisions), preview_token,
            idempotency_token, confirm_high_trust,
        ))
        return {"status": "success", "removed": list(workspace_ids), "results": []}


def _record(workspace_id="ws-1", revision=4):
    return SimpleNamespace(
        workspace_id=workspace_id, principal_id="cred-1", connector_id="surface-1",
        display_label="Runner", state="failed", desired_state="running",
        created_at="2026-09-19T00:00:00+00:00", last_accessed_at="2026-09-19T00:00:00+00:00",
        deletion_due_at=None, measured_allocated_bytes=None, measured_apparent_bytes=None,
        quota_bytes=4 * 1024**3, pinned=False, retention_days=30,
        revision=revision, last_error_code="runtime_unavailable", last_error_at=None,
        runtime_generation=0, host_path=None, path_status="not_reported", volume_name=None,
        credential_id="cred-1", surface_name="Workspace", owner_status="active",
        usage_status="unknown", measured_at=None,
    )


def test_inventory_sort_and_warning_filter_keep_unknown_usage_distinct():
    unknown = _record("unknown")
    measured = _record("measured")
    measured.measured_allocated_bytes = 90
    measured.measured_apparent_bytes = 100
    measured.quota_bytes = 100
    measured.measured_at = "2026-09-19T00:00:00+00:00"
    metadata = SimpleNamespace(list=lambda: [unknown, measured])
    manager = SimpleNamespace(metadata=metadata, runtime=None, host_reserve_bytes=20)
    adapter = WorkspaceAdminAdapter.__new__(WorkspaceAdminAdapter)
    adapter.manager = manager
    adapter.host_root = None
    adapter.container_root = "/root/.microsandbox"
    adapter.max_running = 4

    result = adapter.list_admin_workspaces(
        sort="actual_allocation", direction="desc", search="", states=[],
        pinned=None, expired=None, over_warning=None,
    )
    assert [row["workspace_id"] for row in result["workspaces"]] == ["measured", "unknown"]
    assert result["workspaces"][1]["actual_bytes"] is None

    warned = adapter.list_admin_workspaces(
        sort="quota_percent", direction="desc", search="", states=[],
        pinned=None, expired=None, over_warning=True,
    )
    assert [row["workspace_id"] for row in warned["workspaces"]] == ["measured"]


def test_inventory_search_matches_credential_uuid_before_page_limit():
    rows = [_record(f"ws-{index}") for index in range(101)]
    rows[-1].workspace_id = "target-workspace"
    rows[-1].credential_id = "credential-uuid-visible"
    rows[-1].principal_id = rows[-1].credential_id
    manager = SimpleNamespace(
        metadata=SimpleNamespace(list=lambda: rows), runtime=None,
        host_reserve_bytes=20, warning_threshold_percent=80,
    )
    adapter = WorkspaceAdminAdapter.__new__(WorkspaceAdminAdapter)
    adapter.manager = manager
    adapter.host_root = None
    adapter.container_root = "/root/.microsandbox"
    adapter.max_running = 4

    result = adapter.list_admin_workspaces(
        sort="credential", direction="asc", search="credential-uuid-visible",
        states=[], pinned=None, expired=None, over_warning=None,
    )

    assert [row["workspace_id"] for row in result["workspaces"]] == ["target-workspace"]


def test_warning_filter_uses_persisted_threshold_not_eighty_percent():
    at_ninety = _record("ninety")
    at_ninety.measured_allocated_bytes = 90
    at_ninety.quota_bytes = 100
    at_ninety.measured_at = "2026-09-19T00:00:00+00:00"
    at_ninety_five = _record("ninety-five")
    at_ninety_five.measured_allocated_bytes = 95
    at_ninety_five.quota_bytes = 100
    at_ninety_five.measured_at = "2026-09-19T00:00:00+00:00"
    manager = SimpleNamespace(
        metadata=SimpleNamespace(list=lambda: [at_ninety, at_ninety_five]), runtime=None,
        host_reserve_bytes=20, warning_threshold_percent=90,
    )
    adapter = WorkspaceAdminAdapter.__new__(WorkspaceAdminAdapter)
    adapter.manager = manager
    adapter.host_root = None
    adapter.container_root = "/root/.microsandbox"
    adapter.max_running = 4

    warned_at_90 = adapter.list_admin_workspaces(
        sort="credential", direction="asc", search="", states=[], pinned=None,
        expired=None, over_warning=True,
    )
    assert {row["workspace_id"] for row in warned_at_90["workspaces"]} == {"ninety", "ninety-five"}

    manager.warning_threshold_percent = 95
    warned_at_95 = adapter.list_admin_workspaces(
        sort="credential", direction="asc", search="", states=[], pinned=None,
        expired=None, over_warning=True,
    )
    assert [row["workspace_id"] for row in warned_at_95["workspaces"]] == ["ninety-five"]


def test_workspace_admin_ui_exposes_runtime_provenance_and_cursor_search_contract():
    app = Path("src/cognita/web/app.js").read_text(encoding="utf-8")
    assert "measurement_reason" in app
    assert "measurement_source" in app
    assert 't("admin.workspace.metric.measured_at")' in app
    assert load_catalog("en-US")["admin.workspace.metric.measured_at"] == "Measured at"
    assert 'when(workspaceValue(storage, "measured_at"))' in app
    assert 'measurement_status: "stale"' in app
    assert "fetchCredentialWorkspaceTarget" in app
    assert "workspaceCursorStack" in app
    assert 'data-workspace-page="first"' in app
    assert 'data-workspace-page="previous"' in app
    assert 'data-workspace-page=\"next\"' in app
    assert 'search: String(credentialId)' in app
    assert "requestBody.workspace_id = workspaceId || null" in app
    assert "requestBody.workspace_revision = liveWorkspace ? workspaceRevision : null" in app


def test_admin_adapter_passes_revision_and_idempotency_to_single_domain_actions():
    record = _record()
    calls = []

    class Metadata:
        def get(self, workspace_id):
            return record

        def admin_idempotent(self, token, digest):
            return None

        def save_admin_idempotent(self, token, digest, response):
            return None

    class SingleDomain:
        metadata = Metadata()

        def remove(self, principal, **kwargs):
            calls.append(("remove", principal.principal_id, kwargs))
            return {"status": "success", "workspace": None}

        def start(self, principal, **kwargs):
            calls.append(("start", principal.principal_id, kwargs))
            return {"status": "success", "workspace": {"workspace_id": record.workspace_id}}

    adapter = WorkspaceAdminAdapter.__new__(WorkspaceAdminAdapter)
    adapter.manager = SingleDomain()
    removed = adapter.workspace_action(
        "remove", record.workspace_id, expected_revision=record.revision,
        idempotency_token="remove-1",
    )
    assert removed["status"] == "success"
    assert calls[0] == (
        "remove", "cred-1", {
            "connector_id": "surface-1", "expected_revision": 4,
            "idempotency_key": "remove-1",
            "idempotency_digest": _digest({
                "operation": "workspace_action", "action": "remove",
                "workspace_id": record.workspace_id, "expected_revision": record.revision,
            }),
            "allow_absent_cleanup": True,
        },
    )
    adapter.workspace_action(
        "retry", record.workspace_id, expected_revision=record.revision,
        idempotency_token="retry-1",
    )
    assert calls[1] == ("start", "cred-1", {"connector_id": "surface-1"})


def test_admin_adapter_delegates_bulk_preview_and_apply_to_domain():
    domain = DomainSpy()
    adapter = WorkspaceAdminAdapter.__new__(WorkspaceAdminAdapter)
    adapter.manager = domain

    preview = adapter.preview_bulk_workspace_action(
        "remove", ["ws-1"], expected_revisions={"ws-1": 4},
    )
    assert preview["preview_token"] == "domain-preview-token"
    assert preview["reclaim_estimate_bytes"] == 12
    assert domain.preview_calls == [("remove", ["ws-1"], {"ws-1": 4})]

    result = adapter.apply_bulk_workspace_action(
        "remove", ["ws-1"], expected_revisions={"ws-1": 4},
        preview_token="domain-preview-token", idempotency_token="idem-1",
        confirm_high_trust=True,
    )
    assert result["removed"] == ["ws-1"]
    assert domain.apply_calls == [
        ("remove", ["ws-1"], {"ws-1": 4}, "domain-preview-token", "idem-1", True)
    ]


class BulkRouteService:
    def __init__(self) -> None:
        self.apply_kwargs = None

    def preview_bulk_workspace_action(self, action, workspace_ids, *, expected_revisions):
        return {
            "preview_token": "preview-1", "expires_at": "2026-09-19T01:00:00+00:00",
            "targets": [{"workspace_id": workspace_ids[0], "state": "stopped"}],
            "reclaim_estimate_bytes": None, "reclaim_estimate_status": "unknown",
        }

    def apply_bulk_workspace_action(self, action, workspace_ids, **kwargs):
        self.apply_kwargs = kwargs
        return {"status": "success", "removed": workspace_ids, "results": []}


async def test_bulk_route_passes_explicit_confirmation_to_domain_adapter(tmp_path: Path):
    registry = Registry(tmp_path / "registry.yaml")
    config = CognitaConfig(
        registry_path=registry.path, data_root=tmp_path / "data",
        public_base_url="https://example.test", admin_allowed_hosts=["*"],
        admin_username="admin", admin_password_sha256=hash_token("password"),
    )
    service = BulkRouteService()
    app = create_admin_app(config, registry, workspace_service=service)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        login = await client.post("/api/login", json={"username": "admin", "password": "password"})
        assert login.status_code == 200
        client.headers["X-CSRF-Token"] = client.cookies.get("cognita_csrf")
        preview = await client.post("/api/workspaces/bulk-delete/preview", json={
            "action": "remove", "workspace_ids": ["ws-1"],
            "expected_revisions": {"ws-1": 4},
        })
        assert preview.status_code == 200
        applied = await client.post("/api/workspaces/bulk-delete", json={
            "action": "remove", "workspace_ids": ["ws-1"],
            "expected_revisions": {"ws-1": 4}, "preview_token": "preview-1",
            "confirm": True, "idempotency_token": "idem-1",
        })
        assert applied.status_code == 200
    assert service.apply_kwargs == {
        "expected_revisions": {"ws-1": 4}, "preview_token": "preview-1",
        "idempotency_token": "idem-1", "confirm_high_trust": True,
    }


def test_credential_admin_delete_reconciles_workspace_retention(tmp_path: Path):
    store = CredentialPolicyStore(
        tmp_path / "credentials.json", master_key_dir=tmp_path / "keys",
        admin_password_hash=PasswordHasher().hash("password"),
    )
    calls = []
    lifecycle = SimpleNamespace(
        credential_deletion_gate=lambda _credential_id: nullcontext(),
        validate_credential_workspace=lambda **kwargs: None,
        reconcile_credential=lambda **kwargs: calls.append(kwargs) or {
            "complete": True, "workspace_deleted": False,
        },
    )
    surface_id = "00000000-0000-4000-8000-000000000001"
    service = CredentialAdminService(
        store, surface_resolver=lambda kind, identifier: {"id": surface_id, "slug": "ws"}
        if (kind, identifier) == ("workspace", surface_id) else None,
        public_base_url="https://example.test", workspace_lifecycle=lifecycle,
    )
    created = service.create_credential(
        surface_kind="workspace", surface_id=surface_id, expected_revision=0,
        label="Runner", current_password="password",
    )
    result = service.delete_credential(
        surface_kind="workspace", surface_id=surface_id,
        credential_id=created["credential"].credential_id, expected_revision=1,
        retention="keep", confirm=True, workspace_id=None, workspace_revision=None,
    )
    assert result["workspace_reconciliation"]["complete"] is True
    deleted_at = result["credential"].deleted_at
    assert deleted_at is not None
    assert calls == [{
        "credential_id": created["credential"].credential_id,
        "owner_status": "tombstoned", "retention": "keep",
        "deleted_at": deleted_at,
    }]


def test_credential_delete_fails_closed_when_workspace_binding_changes(tmp_path: Path):
    store = CredentialPolicyStore(
        tmp_path / "credentials.json", master_key_dir=tmp_path / "keys",
        admin_password_hash=PasswordHasher().hash("password"),
    )
    surface_id = "00000000-0000-4000-8000-000000000001"
    binding = {"workspace_id": None, "workspace_revision": None}

    def validate_credential_workspace(**request):
        if (request["workspace_id"], request["workspace_revision"]) != (
            binding["workspace_id"], binding["workspace_revision"]
        ):
            raise RuntimeError("Workspace target changed")

    lifecycle = SimpleNamespace(
        credential_deletion_gate=lambda _credential_id: nullcontext(),
        validate_credential_workspace=validate_credential_workspace,
        reconcile_credential=lambda **_kwargs: {"complete": False},
    )
    service = CredentialAdminService(
        store, surface_resolver=lambda kind, identifier: {"id": surface_id, "slug": "ws"}
        if (kind, identifier) == ("workspace", surface_id) else None,
        public_base_url="https://example.test", workspace_lifecycle=lifecycle,
    )
    created = service.create_credential(
        surface_kind="workspace", surface_id=surface_id, expected_revision=0,
        label="Runner", current_password="password",
    )
    credential_id = created["credential"].credential_id

    # The confirmation read proved explicit absence, but a Workspace appeared
    # before DELETE. The credential must remain active and no retention choice
    # may be committed.
    binding.update(workspace_id="workspace-1", workspace_revision=4)
    with pytest.raises(CredentialWorkspaceConflict, match="Workspace target changed"):
        service.delete_credential(
            surface_kind="workspace", surface_id=surface_id,
            credential_id=credential_id, expected_revision=1,
            retention="delete_now", confirm=True,
            workspace_id=None, workspace_revision=None,
        )
    assert store.snapshot()[0].status == "active"

    # A previously identified Workspace can also advance its metadata revision
    # while the confirmation dialog is open.
    binding.update(workspace_revision=5)
    with pytest.raises(CredentialWorkspaceConflict, match="Workspace target changed"):
        service.delete_credential(
            surface_kind="workspace", surface_id=surface_id,
            credential_id=credential_id, expected_revision=1,
            retention="keep", confirm=True,
            workspace_id="workspace-1", workspace_revision=4,
        )
    assert store.snapshot()[0].status == "active"


def test_credential_delete_gate_serializes_first_use_before_binding_check(tmp_path: Path):
    store = CredentialPolicyStore(
        tmp_path / "credentials.json", master_key_dir=tmp_path / "keys",
        admin_password_hash=PasswordHasher().hash("password"),
    )
    surface_id = "00000000-0000-4000-8000-000000000001"
    binding = {"workspace_id": None, "workspace_revision": None}
    gate = threading.RLock()
    first_use_started = threading.Event()
    allow_first_use = threading.Event()

    def first_use():
        with gate:
            binding.update(workspace_id="workspace-1", workspace_revision=4)
            first_use_started.set()
            allow_first_use.wait(timeout=5)

    def validate_credential_workspace(**request):
        if (request["workspace_id"], request["workspace_revision"]) != (
            binding["workspace_id"], binding["workspace_revision"]
        ):
            raise RuntimeError("Workspace target changed")

    lifecycle = SimpleNamespace(
        credential_deletion_gate=lambda _credential_id: gate,
        validate_credential_workspace=validate_credential_workspace,
        reconcile_credential=lambda **_kwargs: {"complete": False},
    )
    service = CredentialAdminService(
        store, surface_resolver=lambda kind, identifier: {"id": surface_id, "slug": "ws"}
        if (kind, identifier) == ("workspace", surface_id) else None,
        public_base_url="https://example.test", workspace_lifecycle=lifecycle,
    )
    created = service.create_credential(
        surface_kind="workspace", surface_id=surface_id, expected_revision=0,
        label="Runner", current_password="password",
    )
    credential_id = created["credential"].credential_id
    first_use = threading.Thread(target=first_use, daemon=True)
    first_use.start()
    assert first_use_started.wait(timeout=5)

    outcome = {}

    def delete():
        try:
            service.delete_credential(
                surface_kind="workspace", surface_id=surface_id,
                credential_id=credential_id, expected_revision=1,
                retention="keep", confirm=True,
                workspace_id=None, workspace_revision=None,
            )
        except Exception as exc:  # assertion below checks the typed failure
            outcome["error"] = exc

    deletion = threading.Thread(target=delete, daemon=True)
    deletion.start()
    # DELETE cannot validate while first-use owns the same principal gate.
    assert deletion.is_alive()
    allow_first_use.set()
    first_use.join(timeout=5)
    deletion.join(timeout=5)
    assert isinstance(outcome.get("error"), CredentialWorkspaceConflict)
    assert store.snapshot()[0].status == "active"


def test_workspace_admission_rejects_durable_credential_tombstone(tmp_path: Path):
    credential_store = CredentialPolicyStore(
        tmp_path / "credentials.json", master_key_dir=tmp_path / "keys",
        admin_password_hash=PasswordHasher().hash("password"),
    )
    surface_id = "00000000-0000-4000-8000-000000000001"
    created = credential_store.add_credential(
        "workspace", surface_id, "Runner", password="password",
    )
    credential = created[0]
    assert credential_store.admission_status(credential.credential_id) is True
    credential_store.delete(
        credential.credential_id, expected_revision=1, workspace_retention="keep",
    )
    assert credential_store.admission_status(credential.credential_id) is False
    manager = WorkspaceManager(
        WorkspaceMetadataStore(tmp_path / "workspace.sqlite3"),
        credential_is_active=credential_store.admission_status,
    )

    with pytest.raises(WorkspaceError, match="Credential is no longer admitted"):
        manager._admit(credential.credential_id, surface_id)

    def unavailable_checker(_principal_id):
        raise RuntimeError("policy unavailable")

    unavailable = WorkspaceManager(
        WorkspaceMetadataStore(tmp_path / "workspace-unavailable.sqlite3"),
        credential_is_active=unavailable_checker,
    )
    with pytest.raises(WorkspaceError, match="Credential policy is unavailable"):
        unavailable._admit(credential.credential_id, surface_id)


def test_oauth_principal_boundary_allows_active_and_rejects_revoked_grants(tmp_path: Path):
    oauth_store = OAuthPrincipalStore(tmp_path / "oauth.sqlite3")
    principal = oauth_store.create_principal("user-1", "app-1", "resource-1")
    oauth_store.bind_access_token(principal.principal_id, "access-1")
    assert oauth_store.principal_for_access_token("access-1") is not None

    # The parent Workspace admission callback returns None for IDs outside the
    # static store; the gateway has already authenticated this active OAuth
    # grant and therefore it must not be mistaken for a static tombstone.
    manager = WorkspaceManager(
        WorkspaceMetadataStore(tmp_path / "workspace.sqlite3"),
        credential_is_active=lambda _principal_id: None,
    )
    assert manager.credential_is_active(principal.principal_id) is None

    assert oauth_store.revoke_principal(principal.principal_id) is True
    assert oauth_store.principal_for_access_token("access-1") is None
