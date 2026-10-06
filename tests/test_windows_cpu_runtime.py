"""Focused Windows CPU runtime capability and source identity checks."""
from cognita.connectors import PUBLIC_CONTRACT_VERSION

import asyncio
import json
import threading
from types import SimpleNamespace

import pytest
from argon2 import PasswordHasher
from httpx import ASGITransport, AsyncClient

from cognita.auth_policy import AuthenticationPolicyStore, CredentialPolicyStore
from cognita.acceleration import AccelerationStore
from cognita.admin_api import create_admin_app
from cognita.assets.service import AssetService
from cognita.config import CognitaConfig
from cognita.connectors import ConnectorStore, WorkspaceConnectorStore
from cognita.gateway import BRIDGE_TOOL_NAMES, create_gateway_app
from cognita.proxy import public_tool_catalog
from cognita.retrieval import RetrievalCore
from cognita.registry import Project, Registry
from cognita.source_mount_guard import SourceMountGuard
from cognita.store import SourceInfo
from cognita.tokens import hash_token
from retrieval_fakes import HashEmbedder
from cognita.watcher import WatcherManager


@pytest.fixture
def core_gateway(tmp_path):
    registry = Registry(tmp_path / "registry.yaml")
    registry.add(Project(name="Knowledge", documents_dir=tmp_path / "docs", data_dir=tmp_path / "data"))
    connectors = ConnectorStore(tmp_path / "connectors.yaml")
    row = connectors.create(
        expected_revision=0, name="Windows", project_names=["Knowledge"],
        workspace_enabled=True,
    ).connectors[0]
    auth = AuthenticationPolicyStore(tmp_path / "authentication.yaml", project_names=["Knowledge"])
    token = auth.mutate_global(
        expected_revision=0, oauth_enabled=False, static_key_action="generate",
    )["generated_key"]
    app = create_gateway_app(
        CognitaConfig(connectors_path=connectors.path), registry,
        connector_store=connectors, authentication_store=auth,
    )
    return app, row.slug, token


@pytest.mark.anyio
async def test_core_catalog_filters_workspace_but_stale_and_unknown_calls_stay_distinct(
    core_gateway, monkeypatch,
):
    app, slug, token = core_gateway
    bridge_calls = []

    async def bridge_must_not_run(*_args, **_kwargs):
        bridge_calls.append(True)
        raise AssertionError("core mode must not contact the bridge adapter")

    monkeypatch.setattr("cognita.gateway.bridge_tool_result", bridge_must_not_run)
    headers = {"Authorization": f"Bearer {token}"}
    path = f"/mcp/connectors/{slug}/mcp/v{PUBLIC_CONTRACT_VERSION}"
    bridge_tool = next(iter(BRIDGE_TOOL_NAMES))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        catalog = await client.post(path, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, headers=headers)
        stale = await client.post(path, json={
            "jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": "workspace_info", "arguments": {}},
        }, headers=headers)
        stale_bridge = await client.post(path, json={
            "jsonrpc": "2.0", "id": 3, "method": "tools/call",
            "params": {"name": bridge_tool, "arguments": {}},
        }, headers=headers)
        unknown = await client.post(path, json={
            "jsonrpc": "2.0", "id": 4, "method": "tools/call",
            "params": {"name": "made_up_tool", "arguments": {}},
        }, headers=headers)
        health = await client.get("/healthz")

    names = {tool["name"] for tool in catalog.json()["result"]["tools"]}
    assert "workspace_info" not in names
    assert not any(name.startswith("bridge_") for name in names)
    assert stale.json()["result"]["structuredContent"]["reason"] == "runtime_unavailable"
    assert stale_bridge.json()["result"]["structuredContent"]["reason"] == "runtime_unavailable"
    assert unknown.json()["error"]["code"] == -32602
    assert "runtime_unavailable" not in unknown.text
    assert not bridge_calls
    assert health.json()["workspace"] == {"mode": "core", "status": "unconfigured"}


@pytest.mark.anyio
async def test_full_catalog_retains_workspace_schema_and_mode_health(
    tmp_path, full_mode_workspace_service,
):
    registry = Registry(tmp_path / "registry.yaml")
    registry.add(Project(name="Knowledge", documents_dir=tmp_path / "docs", data_dir=tmp_path / "data"))
    connectors = ConnectorStore(tmp_path / "connectors.yaml")
    row = connectors.create(
        expected_revision=0, name="Windows", project_names=["Knowledge"],
        workspace_enabled=True,
    ).connectors[0]
    auth = AuthenticationPolicyStore(tmp_path / "authentication.yaml", project_names=["Knowledge"])
    token = auth.mutate_global(
        expected_revision=0, oauth_enabled=False, static_key_action="generate",
    )["generated_key"]
    app = create_gateway_app(
        CognitaConfig(connectors_path=connectors.path), registry,
        connector_store=connectors, authentication_store=auth,
        workspace_service=full_mode_workspace_service,
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        catalog = await client.post(
            f"/mcp/connectors/{row.slug}/mcp/v{PUBLIC_CONTRACT_VERSION}",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            headers={"Authorization": f"Bearer {token}"},
        )
        health = await client.get("/healthz")

    tools = catalog.json()["result"]["tools"]
    workspace_info = next(tool for tool in tools if tool["name"] == "workspace_info")
    expected_workspace_info = next(
        tool for tool in public_tool_catalog() if tool["name"] == "workspace_info"
    )
    assert workspace_info == expected_workspace_info
    assert health.json()["workspace"] == {"mode": "full", "status": "configured"}


@pytest.mark.anyio
async def test_core_admin_exposes_requested_but_not_effective_and_rejects_new_enable(
    tmp_path,
):
    registry = Registry(tmp_path / "registry.yaml")
    store = WorkspaceConnectorStore(tmp_path / "workspace-connectors.yaml")
    enabled = store.create(
        expected_revision=0, display_name="Existing", enabled=True, slug="existing",
    )
    disabled = store.create(
        expected_revision=1, display_name="Queued", enabled=False, slug="queued",
    )
    config = CognitaConfig(
        registry_path=registry.path,
        acceleration_path=tmp_path / "acceleration.yaml",
        admin_allowed_hosts=["*"],
        admin_username="admin",
        admin_password_sha256=hash_token("password"),
    )
    acceleration_store = AccelerationStore(
        config.acceleration_path,
        legacy_config_path=tmp_path / "cognita.yaml",
    )
    app = create_admin_app(
        config, registry, workspace_connector_store=store,
        acceleration_store=acceleration_store,
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        login = await client.post("/api/login", json={"username": "admin", "password": "password"})
        assert login.status_code == 200
        client.headers["X-CSRF-Token"] = client.cookies.get("cognita_csrf")
        listing = await client.get("/api/workspace-connectors")
        rejected = await client.patch(
            f"/api/workspace-connectors/{disabled.id}",
            json={"expected_revision": 2, "enabled": True, "confirm_high_trust": True},
        )

    assert listing.status_code == 200
    records = {row["id"]: row for row in listing.json()["workspace_connectors"]}
    assert records[enabled.id]["workspace_requested"] is True
    assert records[enabled.id]["workspace_effective"] is False
    assert records[enabled.id]["workspace_reason"] == "host_workspace_disabled"
    assert rejected.status_code == 409
    assert rejected.json()["reason"] == "host_workspace_disabled"
    assert store.snapshot().revision == 2


@pytest.mark.anyio
async def test_workspace_only_route_reports_exact_core_mode_reason_without_adapter(
    tmp_path, monkeypatch,
):
    registry = Registry(tmp_path / "registry.yaml")
    registry.add(Project(name="Knowledge", documents_dir=tmp_path / "docs", data_dir=tmp_path / "data"))
    connectors = ConnectorStore(tmp_path / "connectors.yaml")
    workspace_connectors = WorkspaceConnectorStore(tmp_path / "workspace-connectors.yaml")
    surface = workspace_connectors.create(
        expected_revision=0, display_name="Runner", enabled=True, slug="runner",
    )
    credentials = CredentialPolicyStore(
        tmp_path / "credentials.json", master_key_dir=tmp_path / "keys",
        admin_password_hash=PasswordHasher().hash("admin-password"),
    )
    _record, token = credentials.add_credential(
        "workspace", surface.id, "test key", surface_slug=surface.slug,
        password="admin-password",
    )
    app = create_gateway_app(
        CognitaConfig(connectors_path=connectors.path), registry,
        connector_store=connectors, workspace_connector_store=workspace_connectors,
        credential_store=credentials,
    )
    adapter_calls = []

    def adapter_must_not_run(*_args, **_kwargs):
        adapter_calls.append(True)
        raise AssertionError("core mode must not contact the Workspace adapter")

    monkeypatch.setattr("cognita.gateway.workspace_tool_result", adapter_must_not_run)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            f"/mcp/workspace/{surface.slug}/mcp/v3",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                  "params": {"name": "workspace_info", "arguments": {}}},
            headers={"Authorization": f"Bearer {token}"},
        )
    assert response.json()["result"]["structuredContent"]["reason"] == "workspace_unavailable"
    assert not adapter_calls


def test_source_guard_is_inert_without_windows_projection(monkeypatch):
    monkeypatch.delenv("COGNITA_SOURCE_IDENTITIES_FILE", raising=False)
    guard = SourceMountGuard()
    assert not guard.enabled
    assert guard.check("/anything").state == "available"


def _valid_source_projection():
    return {
        "schema": 1,
        "sources": [{
            "alias": "synthetic",
            "source_kind": "installation_ext4",
            "observation": "available",
            "identity": {"device": 1, "inode": 2},
        }],
    }


@pytest.mark.parametrize("mutate", [
    lambda payload: payload.pop("schema"),
    lambda payload: payload.update(extra=True),
    lambda payload: payload.update(schema=True),
    lambda payload: payload.update(schema=2),
    lambda payload: payload.update(schema=1.0),
    lambda payload: payload.pop("sources"),
    lambda payload: payload.update(sources={}),
    lambda payload: payload["sources"][0].update(extra=True),
    lambda payload: payload["sources"][0].update(status="available"),
    lambda payload: payload["sources"][0].pop("alias"),
    lambda payload: payload["sources"][0].update(alias=1),
    lambda payload: payload["sources"][0].pop("observation"),
    lambda payload: payload["sources"][0].update(source_kind=[]),
    lambda payload: payload["sources"][0].update(source_kind="windows"),
    lambda payload: payload["sources"][0].update(observation=None),
    lambda payload: payload["sources"][0].update(observation="configured"),
    lambda payload: payload["sources"][0]["identity"].update(st_dev=1),
    lambda payload: payload["sources"][0]["identity"].pop("inode"),
    lambda payload: payload["sources"][0].update(identity=[]),
    lambda payload: payload["sources"][0]["identity"].update(device=-1),
    lambda payload: payload["sources"][0]["identity"].update(inode=True),
    lambda payload: payload["sources"][0]["identity"].update(device=1.0),
    lambda payload: payload["sources"][0]["identity"].update(inode="2"),
    lambda payload: payload["sources"].append(dict(payload["sources"][0])),
    lambda payload: payload["sources"].append({
        **payload["sources"][0], "alias": "SYNTHETIC",
    }),
])
def test_source_guard_rejects_non_schema_one_projection_shapes(tmp_path, mutate):
    payload = _valid_source_projection()
    mutate(payload)
    projection = tmp_path / "source-identities.json"
    projection.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(RuntimeError, match="projection"):
        SourceMountGuard(projection)


@pytest.mark.parametrize("contents", [
    '{"schema":1,"schema":1,"sources":[]}',
    '{"schema":1,"sources":[{"alias":"x","alias":"y",'
    '"source_kind":"ntfs","observation":"available",'
    '"identity":{"device":1,"inode":2}}]}',
    '{"schema":1,"sources":[{"alias":"x","source_kind":"ntfs",'
    '"observation":"available","identity":{"device":1,"device":1,"inode":2}}]}',
])
def test_source_guard_rejects_duplicate_json_members(tmp_path, contents):
    projection = tmp_path / "source-identities.json"
    projection.write_text(contents, encoding="utf-8")

    with pytest.raises(RuntimeError, match="projection"):
        SourceMountGuard(projection)


def test_source_guard_requires_reconciliation_after_startup_unavailable_observation(
    tmp_path, monkeypatch,
):
    source_root = tmp_path / "sources"
    alias_root = source_root / "synthetic"
    alias_root.mkdir(parents=True)
    project_root = alias_root
    stat = alias_root.stat()
    projection = tmp_path / "source-identities.json"
    projection.write_text(json.dumps({"schema": 1, "sources": [{
            "source_kind": "installation_ext4",
            "alias": "synthetic",
            "observation": "unavailable",
            "identity": {"device": stat.st_dev, "inode": stat.st_ino},
        }]}), encoding="utf-8")
    monkeypatch.setenv("COGNITA_PROJECTS_ROOT", str(source_root))
    monkeypatch.setenv("COGNITA_SOURCE_IDENTITIES_FILE", str(projection))
    monkeypatch.setattr(SourceMountGuard, "_mount_id", staticmethod(lambda _path: 7))

    guard = SourceMountGuard()
    assert guard.check(project_root).state == "unavailable"
    assert guard.check(project_root).state == "reconnected"
    assert guard.check(project_root).state == "reconnected"
    assert guard.mark_reconciled(project_root).state == "available"


def test_source_guard_fails_closed_when_runtime_identity_changes(tmp_path, monkeypatch):
    source_root = tmp_path / "sources"
    alias_root = source_root / "synthetic"
    alias_root.mkdir(parents=True)
    projection = tmp_path / "source-identities.json"
    projection.write_text(json.dumps({"schema": 1, "sources": [{
            "alias": "synthetic",
            "source_kind": "installation_ext4",
            "observation": "available",
            "identity": {"device": alias_root.stat().st_dev, "inode": alias_root.stat().st_ino + 1},
        }]}), encoding="utf-8")
    monkeypatch.setenv("COGNITA_PROJECTS_ROOT", str(source_root))
    monkeypatch.setenv("COGNITA_SOURCE_IDENTITIES_FILE", str(projection))
    monkeypatch.setattr(SourceMountGuard, "_mount_id", staticmethod(lambda _path: 7))

    assert SourceMountGuard().check(alias_root).state == "unavailable"


def test_source_guard_marks_same_identity_reconnected_and_waits_for_reconciliation(
    tmp_path, monkeypatch,
):
    source_root = tmp_path / "sources"
    alias_root = source_root / "synthetic"
    alias_root.mkdir(parents=True)
    projection = tmp_path / "source-identities.json"
    projection.write_text(json.dumps({"schema": 1, "sources": [{
            "alias": "synthetic",
            "source_kind": "ntfs",
            "observation": "available",
            "identity": {"device": alias_root.stat().st_dev, "inode": alias_root.stat().st_ino},
        }]}), encoding="utf-8")
    mount = {"id": 7}
    monkeypatch.setenv("COGNITA_PROJECTS_ROOT", str(source_root))
    monkeypatch.setenv("COGNITA_SOURCE_IDENTITIES_FILE", str(projection))
    monkeypatch.setattr(
        SourceMountGuard, "_mount_id",
        staticmethod(lambda _path: mount["id"]),
    )

    guard = SourceMountGuard()
    assert guard.check(alias_root).state == "available"
    mount["id"] = None
    assert guard.check(alias_root).state == "unavailable"
    mount["id"] = 8
    assert guard.check(alias_root).state == "reconnected"
    assert guard.check(alias_root).state == "reconnected"
    assert guard.mark_reconciled(alias_root).state == "available"
    assert guard.check(alias_root).state == "available"


@pytest.mark.anyio
async def test_watcher_schedules_reconciliation_when_reconnect_has_no_filesystem_event(
    tmp_path, monkeypatch,
):
    original_sleep = asyncio.sleep

    async def yield_to_ready_tasks(_delay):
        await original_sleep(0)

    monkeypatch.setattr("cognita.watcher.asyncio.sleep", yield_to_ready_tasks)
    monkeypatch.setattr("cognita.watcher.time.monotonic", lambda: 10.0)

    class Guard:
        enabled = True
        state = "reconnected"

        def check(self, _path):
            return SimpleNamespace(state=self.state)

        def mark_reconciled(self, _path):
            self.state = "available"
            return SimpleNamespace(state=self.state)

    state = SimpleNamespace(task=None, dirty={}, retry_at=0.0)
    manager = object.__new__(WatcherManager)
    manager._stopping = False
    manager._states = {"Knowledge": state}
    manager._docs_dirs = {"Knowledge": tmp_path}
    manager._observer = None
    manager._polling_observer = None
    manager._dead_observers_logged = set()
    manager._lock = threading.Lock()
    manager.debounce_s = 0.05
    manager.source_guard = Guard()
    reconciliations = []

    async def record_reconciliation(project, batch):
        reconciliations.append((project, tuple(batch)))
        manager.source_guard.mark_reconciled(tmp_path)
        manager._stopping = True

    manager._run_batch = record_reconciliation
    await manager._flush_loop()
    if state.task is not None:
        await state.task

    assert reconciliations == [("Knowledge", (".",))]


def test_source_guard_diagnostics_redact_host_paths_and_filenames(
    tmp_path, monkeypatch, caplog,
):
    source_root = tmp_path / "private-host-source-root"
    alias_root = source_root / "synthetic"
    alias_root.mkdir(parents=True)
    projection = tmp_path / "source-identities.json"
    projection.write_text(json.dumps({"schema": 1, "sources": [{
            "alias": "synthetic",
            "source_kind": "ntfs",
            "observation": "available",
            "identity": {"device": alias_root.stat().st_dev + 1,
                         "inode": alias_root.stat().st_ino},
        }]}), encoding="utf-8")
    monkeypatch.setenv("COGNITA_PROJECTS_ROOT", str(source_root))
    monkeypatch.setenv("COGNITA_SOURCE_IDENTITIES_FILE", str(projection))
    monkeypatch.setattr(SourceMountGuard, "_mount_id", staticmethod(lambda _path: 7))

    assert SourceMountGuard().check(alias_root).state == "unavailable"
    assert str(source_root) not in caplog.text
    assert "private-host-source-root" not in caplog.text


def test_casefold_collision_reports_only_relative_names(tmp_path, monkeypatch):
    root = tmp_path / "synthetic"
    root.mkdir()

    def fake_walk(walk_root, onerror=None):
        yield str(walk_root), ["Folder", "folder"], ["Notes.md"]

    monkeypatch.setattr("cognita.source_mount_guard.os.walk", fake_walk)
    assert SourceMountGuard.casefold_collision(root) == ("Folder", "folder")

    source_root = tmp_path / "sources"
    alias_root = source_root / "synthetic"
    alias_root.mkdir(parents=True)
    projection = tmp_path / "source-identities.json"
    projection.write_text(json.dumps({"schema": 1, "sources": [{
            "alias": "synthetic",
            "source_kind": "ntfs",
            "observation": "available",
            "identity": {"device": alias_root.stat().st_dev,
                         "inode": alias_root.stat().st_ino},
        }]}), encoding="utf-8")
    monkeypatch.setenv("COGNITA_PROJECTS_ROOT", str(source_root))
    monkeypatch.setenv("COGNITA_SOURCE_IDENTITIES_FILE", str(projection))
    monkeypatch.setattr(SourceMountGuard, "_mount_id", staticmethod(lambda _path: 7))
    status = SourceMountGuard().check(alias_root, validate_names=True)
    assert status.state == "unavailable"
    assert status.reason == "case_fold_collision"
    assert status.collision_paths == ("Folder", "folder")


@pytest.mark.anyio
async def test_targeted_reconciliation_keeps_index_when_source_changes_before_removal(
    tmp_path,
):
    class Store:
        def __init__(self):
            self.deleted = []

        async def list_sources(self, _project):
            return {"missing.md": SourceInfo(
                "doc-id", "content-hash", 0.0, 1, "registered", "general",
            )}

        async def delete_document(self, _project, source):
            self.deleted.append(source)
            return True

    store = Store()
    core = RetrievalCore(store, HashEmbedder())
    checks = iter((True, False))
    summary = await core.reconcile_paths(
        "Knowledge", tmp_path, ["missing.md"],
        source_is_safe=lambda: next(checks),
        diagnostic_redacted=True,
    )

    assert summary["removed"] == 0
    assert store.deleted == []


@pytest.mark.anyio
async def test_asset_backfill_keeps_catalog_when_source_changes_before_retirement(
    tmp_path,
):
    class Repository:
        def __init__(self):
            self.deleted = []

        async def list_sources(self, _prefix):
            return ["stale.png"]

        async def delete_source(self, source):
            self.deleted.append(source)

    repository = Repository()
    project = SimpleNamespace(
        name="Knowledge", documents_dir=tmp_path, data_dir=tmp_path / "data",
    )
    service = AssetService(project, repository)
    checks = iter((True, False))
    result = await service.reconcile_all(source_is_safe=lambda: next(checks))

    assert result["removed"] == 0
    assert repository.deleted == []
