"""Deterministic connector policy, migration, and Admin API contracts."""

from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError

from cognita.admin_api import create_admin_app
from cognita.config import CognitaConfig
from cognita.connectors import (
    PUBLIC_CONTRACT_VERSION,
    ConnectorDefinition,
    ConnectorPolicyError,
    ConnectorStore,
    PolicyUnavailable,
    RevisionConflict,
    build_connector_path,
    build_connector_url,
    is_supported_contract_version,
    is_published_contract_version,
    migrate_connectors,
    parse_connector_path,
    resolve_project_access,
    supported_contract_versions,
)
from cognita.registry import Project, Registry
from cognita.tokens import hash_token


def _registry(tmp_path: Path, *projects: tuple[str, bool]) -> Registry:
    reg = Registry(tmp_path / "registry.yaml")
    for name, writable in projects:
        reg.add(Project(name=name, documents_dir=tmp_path, data_dir=tmp_path / name, writable=writable))
    return reg


def test_effective_access_all_selected_and_future_projects(tmp_path):
    reg = _registry(tmp_path, ("KEI", True), ("ALTEA", False))
    store = ConnectorStore(tmp_path / "connectors.yaml")
    all_cfg = store.create(expected_revision=0, name="All", project_names=["KEI", "ALTEA"])
    all_id = all_cfg.connectors[0].id
    assert [p.project for p in store.accessible_projects(all_id, reg.projects)] == ["ALTEA", "KEI"]
    selected_cfg = store.create(
        expected_revision=1, name="Selected", project_mode="selected", default_access=None,
        project_access={"KEI": "read"}, project_names=["KEI", "ALTEA"],
    )
    selected_id = selected_cfg.connectors[-1].id
    assert [p.project for p in store.accessible_projects(selected_id, reg.projects)] == ["KEI"]
    reg.add(Project(name="NEW", documents_dir=tmp_path, data_dir=tmp_path / "NEW"))
    assert store.effective_access(all_id, "NEW", reg.projects).access == "write"


def test_default_access_exclusion_requires_explicit_override(tmp_path):
    ordinary = Project(name="Ordinary", documents_dir=tmp_path, data_dir=tmp_path)
    excluded = Project(
        name="Excluded", documents_dir=tmp_path, data_dir=tmp_path,
        exclude_from_default_permissions=True,
    )
    connector = ConnectorDefinition(
        id="2c520a44-2037-4bb5-a565-d88ec2bb02d1", name="Cognita",
        project_mode="all", default_access="write",
    )
    assert resolve_project_access(connector, ordinary) == "write"
    assert resolve_project_access(connector, excluded) is None
    assert resolve_project_access(
        connector, excluded, project_key_grant="Excluded"
    ) == "write"
    assert resolve_project_access(
        connector, excluded, project_key_grant="Other"
    ) is None
    connector.project_access[excluded.name] = "write"
    assert resolve_project_access(connector, excluded) == "write"
    connector.project_access.pop(excluded.name)
    assert resolve_project_access(connector, excluded) is None
    excluded.exclude_from_default_permissions = False
    assert resolve_project_access(connector, excluded) == "write"
    selected = ConnectorDefinition(
        id="475b7f84-cb80-421b-9cb0-09e0ad6e9f6f", name="Selected",
        project_mode="selected", default_access=None,
        project_access={ordinary.name: "read"},
    )
    assert resolve_project_access(selected, ordinary) == "read"
    assert resolve_project_access(selected, excluded) is None
    with pytest.raises(ConnectorPolicyError):
        resolve_project_access(connector, "Excluded")


def test_new_excluded_project_is_not_added_to_existing_all_connector(tmp_path):
    registry = _registry(tmp_path, ("Existing", True))
    store = ConnectorStore(tmp_path / "connectors.yaml")
    created = store.create(
        expected_revision=0, name="All", project_names=["Existing"]
    ).connectors[0]
    registry.add(
        Project(
            name="New", documents_dir=tmp_path, data_dir=tmp_path / "New",
            exclude_from_default_permissions=True,
        )
    )
    assert [item.project for item in store.accessible_projects(created.id, registry.projects)] == [
        "Existing"
    ]
    updated = store.update(
        created.id,
        expected_revision=1,
        project_names=["Existing", "New"],
        project_access={"New": "write"},
    )
    assert [item.project for item in store.accessible_projects(
        updated.connectors[0].id, registry.projects
    )] == ["Existing", "New"]


def test_invalid_policy_fails_closed(tmp_path):
    path = tmp_path / "connectors.yaml"
    path.write_text("version: 99\nrevision: 1\nconnectors: []\n", encoding="utf-8")
    with pytest.raises(PolicyUnavailable):
        ConnectorStore(path).snapshot()


def test_revision_conflict_and_deletion_cleanup(tmp_path):
    reg = _registry(tmp_path, ("KEI", True))
    store = ConnectorStore(tmp_path / "connectors.yaml")
    cfg = store.create(expected_revision=0, name="C", project_mode="selected", default_access=None,
                       project_access={"KEI": "read"}, project_names=["KEI"])
    with pytest.raises(RevisionConflict):
        store.create(expected_revision=0, name="stale", project_names=["KEI"])
    assert store.remove_project_references("KEI").revision == 2
    assert store.effective_access(cfg.connectors[0].id, "KEI", reg.projects) is None


def test_migration_dry_run_and_retry_preserve_id(tmp_path):
    reg = _registry(tmp_path, ("KEI", True), ("ALTEA", False))
    store = ConnectorStore(tmp_path / "connectors.yaml")
    dry = migrate_connectors(reg, store, dry_run=True)
    assert dry.changed and not store.path.exists()
    applied = migrate_connectors(reg, store)
    retry = migrate_connectors(reg, store)
    assert applied.connector_id == retry.connector_id and not retry.changed
    assert store.effective_access(applied.connector_id, "ALTEA", reg.projects).access == "read"


def test_canonical_url_uses_only_origin():
    assert build_connector_url("https://cognita.example/", "cognita") == (
        f"https://cognita.example/mcp/connectors/cognita/mcp/v{PUBLIC_CONTRACT_VERSION}"
    )
    assert build_connector_path("cognita", 3).endswith("/mcp/v3")


def test_versioned_resource_parser_is_strict():
    connector_slug = "cognita"
    valid = f"/mcp/connectors/{connector_slug}/mcp/v12"
    parsed = parse_connector_path(valid)
    assert parsed and parsed.connector_slug == connector_slug and parsed.contract_version == 12
    for invalid in (
        f"/mcp/connectors/{connector_slug}",
        f"/mcp/connectors/{connector_slug}/mcp/v0",
        f"/mcp/connectors/{connector_slug}/mcp/v01",
        f"/mcp/connectors/{connector_slug.upper()}/mcp/v2",
        f"/mcp/connectors/{connector_slug}/mcp/v2/",
        f"/mcp/connectors/{connector_slug}/mcp/v2?x=1",
    ):
        assert parse_connector_path(invalid) is None
    assert PUBLIC_CONTRACT_VERSION >= 1
    assert not is_supported_contract_version(PUBLIC_CONTRACT_VERSION - 1)
    assert is_supported_contract_version(PUBLIC_CONTRACT_VERSION)
    assert not is_supported_contract_version(1)
    assert not is_supported_contract_version(PUBLIC_CONTRACT_VERSION + 1)
    assert not is_published_contract_version(PUBLIC_CONTRACT_VERSION - 1)
    assert not is_published_contract_version(1)
    assert is_published_contract_version(PUBLIC_CONTRACT_VERSION)
    assert not is_published_contract_version(PUBLIC_CONTRACT_VERSION + 1)
    assert supported_contract_versions(1) == (1,)
    assert supported_contract_versions(3) == (3,)
    assert supported_contract_versions(True) == ()
    assert is_supported_contract_version(1, current_version=1)
    assert not is_supported_contract_version(0, current_version=1)


def test_legacy_contract_version_is_removed_on_read_and_never_enters_model(tmp_path):
    connector_id = "2c520a44-2037-4bb5-a565-d88ec2bb02d1"
    path = tmp_path / "connectors.yaml"
    path.write_text(
        f"version: 1\nrevision: 1\nconnectors:\n  - id: {connector_id}\n"
        "    name: C\n    slug: c\n    enabled: true\n    project_mode: all\n"
        "    default_access: write\n    project_access: {}\n    contract_version: 9\n",
        encoding="utf-8",
    )
    store = ConnectorStore(path)
    snapshot = store.snapshot()
    assert snapshot.revision == 1
    assert not hasattr(snapshot.connectors[0], "contract_version")
    assert "contract_version:" not in path.read_text(encoding="utf-8")


def test_legacy_missing_slugs_are_migrated_atomically_and_collisions_are_stable(tmp_path):
    first_id = "2c520a44-2037-4bb5-a565-d88ec2bb02d1"
    second_id = "c7e7c3fd-ea8c-4550-a9c5-cf97a2b18c4b"
    path = tmp_path / "connectors.yaml"
    path.write_text(
        "version: 1\nrevision: 4\nconnectors:\n"
        f"  - id: {first_id}\n    name: Cognita\n    enabled: true\n"
        "    project_mode: all\n    default_access: write\n    project_access: {}\n"
        f"  - id: {second_id}\n    name: Cognita!\n    enabled: true\n"
        "    project_mode: all\n    default_access: write\n    project_access: {}\n",
        encoding="utf-8",
    )

    store = ConnectorStore(path)
    snapshot = store.snapshot()

    assert snapshot.revision == 4
    assert [item.slug for item in snapshot.connectors] == ["cognita", "cognita-2"]
    persisted = path.read_text(encoding="utf-8")
    assert "slug: cognita\n" in persisted
    assert "slug: cognita-2\n" in persisted
    restarted = ConnectorStore(path).snapshot()
    assert [item.slug for item in restarted.connectors] == ["cognita", "cognita-2"]


def test_read_only_store_derives_legacy_slug_without_replacing_parent_policy(tmp_path):
    connector_id = "2c520a44-2037-4bb5-a565-d88ec2bb02d1"
    path = tmp_path / "connectors.yaml"
    original = (
        "version: 1\nrevision: 4\nconnectors:\n"
        f"  - id: {connector_id}\n    name: Cognita\n    enabled: true\n"
        "    project_mode: all\n    default_access: write\n    project_access: {}\n"
    )
    path.write_text(original, encoding="utf-8")

    snapshot = ConnectorStore(path, persist_migrations=False).snapshot()

    assert snapshot.connectors[0].slug == "cognita"
    assert path.read_text(encoding="utf-8") == original


def test_legacy_persisted_generation_never_changes_code_owned_current_version(tmp_path):
    connector_id = "2c520a44-2037-4bb5-a565-d88ec2bb02d1"
    path = tmp_path / "connectors.yaml"
    path.write_text(
        f"version: 1\nrevision: 7\nconnectors:\n  - id: {connector_id}\n"
        "    name: C\n    enabled: true\n    project_mode: all\n"
        "    default_access: write\n    project_access: {}\n    contract_version: garbage\n",
        encoding="utf-8",
    )

    snapshot = ConnectorStore(path).snapshot()

    assert snapshot.revision == 7
    assert snapshot.connectors[0].slug == "c"
    assert not hasattr(snapshot.connectors[0], "contract_version")
    assert PUBLIC_CONTRACT_VERSION >= 1
    assert "contract_version:" not in path.read_text(encoding="utf-8")


def test_contract_version_is_not_a_connector_definition_field():
    connector_id = "2c520a44-2037-4bb5-a565-d88ec2bb02d1"
    assert not hasattr(ConnectorDefinition(id=connector_id, name="C"), "contract_version")
    with pytest.raises(ValidationError):
        ConnectorDefinition(id=connector_id, name="C", contract_version=2)


def test_admin_cannot_mutate_code_owned_contract_version(tmp_path):
    store = ConnectorStore(tmp_path / "connectors.yaml")
    created = store.create(expected_revision=0, name="C")
    connector_id = created.connectors[0].id
    with pytest.raises(ConnectorPolicyError, match="owned by the deployed MCP schema"):
        store.update(connector_id, expected_revision=1, contract_version=99)
    current = store.snapshot()
    assert current.revision == 1
    assert not hasattr(current.connectors[0], "contract_version")


@pytest.fixture
def admin_ctx(tmp_path):
    reg = _registry(tmp_path, ("KEI", True))
    cfg = CognitaConfig(
        registry_path=reg.path, connectors_path=tmp_path / "connectors.yaml",
        data_root=tmp_path / "data", public_base_url="https://cognita.example",
        admin_allowed_hosts=["*"],
    )
    return create_admin_app(cfg, reg), reg


async def test_admin_connector_crud_auth_unknown_fields_and_get_is_read_only(admin_ctx):
    app, _ = admin_ctx
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        listed = await client.get("/api/connectors")
        assert listed.status_code == 200 and listed.json() == {"revision": 0, "connectors": []}
        bad = await client.post("/api/connectors", json={"expected_revision": 0, "name": "C", "bogus": 1})
        assert bad.status_code == 400 and bad.json()["reason"] == "invalid_connector"
        created = await client.post(
            "/api/connectors",
            json={"expected_revision": 0, "name": "C", "enabled": True,
                  "project_mode": "all", "default_access": "write", "project_access": {}},
        )
        assert created.status_code == 201
        connector = created.json()["connector"]
        assert connector["contract_version"] == PUBLIC_CONTRACT_VERSION
        assert connector["slug"] == "c"
        assert connector["path"] == "/mcp/connectors/c/mcp"
        assert connector["url"] == "https://cognita.example/mcp/connectors/c/mcp"
        assert connector["stable_url"] == connector["url"]
        assert connector["current_url"] == f"https://cognita.example/mcp/connectors/c/mcp/v{PUBLIC_CONTRACT_VERSION}"
        retired_path = f"/v{PUBLIC_CONTRACT_VERSION - 1}"
        assert retired_path not in connector["path"]
        assert connector["current_url"].count(f"/v{PUBLIC_CONTRACT_VERSION}") == 1
        obsolete_publish = await client.post(
            f"/api/connectors/{connector['id']}/contract-version",
            json={"expected_revision": 1},
        )
        assert obsolete_publish.status_code == 404
        stale = await client.patch(f"/api/connectors/{connector['id']}", json={"expected_revision": 0, "enabled": False})
        assert stale.status_code == 409 and stale.json()["reason"] == "revision_conflict"
        deleted = await client.delete(f"/api/connectors/{connector['id']}?expected_revision=1")
        assert deleted.status_code == 200


async def test_admin_connector_workspace_enable_requires_admin_password(admin_ctx):
    app, _ = admin_ctx
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        denied = await client.post(
            "/api/connectors",
            json={"expected_revision": 0, "name": "C", "workspace_enabled": True},
        )
        assert denied.status_code == 503
        assert denied.json()["reason"] == "admin_password_required"
        listed = await client.get("/api/connectors")
        assert listed.json() == {"revision": 0, "connectors": []}


async def test_admin_connector_mutations_require_csrf_when_authenticated(tmp_path):
    reg = _registry(tmp_path, ("KEI", True))
    cfg = CognitaConfig(
        registry_path=reg.path, connectors_path=tmp_path / "connectors.yaml",
        data_root=tmp_path / "data", admin_allowed_hosts=["*"],
        admin_password_sha256=hash_token("secret"),
    )
    app = create_admin_app(cfg, reg)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.post("/api/login", json={"username": "admin", "password": "secret"})).status_code == 200
        blocked = await client.post("/api/connectors", json={"expected_revision": 0, "name": "C"})
        assert blocked.status_code == 403 and blocked.json()["reason"] == "csrf_failed"
        csrf = client.cookies.get("cognita_csrf")
        allowed = await client.post("/api/connectors", headers={"X-CSRF-Token": csrf}, json={"expected_revision": 0, "name": "C"})
        assert allowed.status_code == 201
