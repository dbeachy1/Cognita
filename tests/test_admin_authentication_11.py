from types import SimpleNamespace

from httpx import ASGITransport, AsyncClient

from cognita.admin_api import create_admin_app
from cognita.auth_policy import AuthenticationPolicyStore
from cognita.config import CognitaConfig
from cognita.connectors import ConnectorStore
from cognita.registry import Project, Registry


class ReadySupervisor:
    def __init__(self):
        self.starts = 0
        self.stops = 0

    async def start(self):
        self.starts += 1
        return SimpleNamespace(state="ready")

    async def stop(self):
        self.stops += 1
        return SimpleNamespace(state="stopped")


async def test_authentication_api_generates_once_and_is_revision_checked(tmp_path):
    registry = Registry(tmp_path / "registry.yaml")
    docs = tmp_path / "docs"
    docs.mkdir()
    registry.add(Project(name="KEI", documents_dir=docs, data_dir=tmp_path / "data"))
    store = AuthenticationPolicyStore(tmp_path / "authentication.yaml", project_names=["KEI"])
    config = CognitaConfig(
        registry_path=registry.path, data_root=tmp_path / "data", authentication_path=store.path,
        admin_allowed_hosts=["*"], admin_password_sha256="",
    )
    app = create_admin_app(config, registry, authentication_store=store)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        initial = await client.get("/api/authentication")
        assert initial.status_code == 200
        assert initial.headers["cache-control"] == "no-store"
        assert "digest" not in initial.text
        generated = await client.patch(
            "/api/authentication/global",
            json={"expected_revision": 0, "static_key_action": "generate"},
        )
        assert generated.status_code == 200
        raw = generated.json()["generated_key"]
        assert raw.startswith("cog_sk_v1_")
        assert "digest" not in generated.text
        again = await client.get("/api/authentication")
        assert raw not in again.text
        stale = await client.patch(
            "/api/authentication/global",
            json={"expected_revision": 0, "static_key_action": "generate"},
        )
        assert stale.status_code == 409
        assert stale.json()["reason"] == "revision_conflict"


async def test_dedicated_static_key_lifecycle_is_immediate_and_scoped(tmp_path):
    registry = Registry(tmp_path / "registry.yaml")
    docs = tmp_path / "docs"
    docs.mkdir()
    registry.add(Project(name="KEI", documents_dir=docs, data_dir=tmp_path / "data"))
    store = AuthenticationPolicyStore(tmp_path / "authentication.yaml", project_names=["KEI"])
    config = CognitaConfig(
        registry_path=registry.path, data_root=tmp_path / "data", authentication_path=store.path,
        admin_allowed_hosts=["*"], admin_password_sha256="",
    )
    app = create_admin_app(config, registry, authentication_store=store)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        generated = await client.post(
            "/api/authentication/global/static-key/generate",
            json={"expected_revision": 0},
        )
        assert generated.status_code == 200
        payload = generated.json()
        assert payload["scope"] == "global"
        assert payload["generated_key"].startswith("cog_sk_v1_")
        assert payload["authentication"]["revision"] == 1
        assert generated.headers["cache-control"] == "no-store"
        raw = payload["generated_key"]

        project = await client.post(
            "/api/authentication/projects/KEI/static-key/generate",
            json={"expected_revision": 1},
        )
        assert project.status_code == 200
        assert project.json()["scope"] == "project"
        assert project.json()["project"] == "KEI"
        assert project.json()["generated_key"] != raw

        revoked = await client.post(
            "/api/authentication/projects/KEI/static-key/revoke",
            json={"expected_revision": 2, "confirm_lockout": False},
        )
        assert revoked.status_code == 200
        assert "warnings" in revoked.json()
        assert revoked.json()["projects"][0]["effective_static_key_source"] == "global"

        inherited = await client.post(
            "/api/authentication/projects/KEI/static-key/revoke",
            json={"expected_revision": 3, "confirm_lockout": False},
        )
        assert inherited.status_code == 200
        assert inherited.json()["revision"] == 3
        assert inherited.json()["global"]["static_key"]["configured"] is True


async def test_dedicated_static_key_requests_are_strict_and_revision_conflicts_are_secretless(tmp_path):
    registry = Registry(tmp_path / "registry.yaml")
    docs = tmp_path / "docs"
    docs.mkdir()
    registry.add(Project(name="KEI", documents_dir=docs, data_dir=tmp_path / "data"))
    store = AuthenticationPolicyStore(tmp_path / "authentication.yaml", project_names=["KEI"])
    config = CognitaConfig(
        registry_path=registry.path, data_root=tmp_path / "data", authentication_path=store.path,
        admin_allowed_hosts=["*"], admin_password_sha256="",
    )
    app = create_admin_app(config, registry, authentication_store=store)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        unknown = await client.post(
            "/api/authentication/global/static-key/generate",
            json={"expected_revision": 0, "static_key_action": "generate"},
        )
        assert unknown.status_code == 400
        invalid_type = await client.post(
            "/api/authentication/global/static-key/generate",
            json={"expected_revision": True},
        )
        assert invalid_type.status_code == 400
        generated = await client.post(
            "/api/authentication/global/static-key/generate",
            json={"expected_revision": 0},
        )
        raw = generated.json()["generated_key"]
        stale = await client.post(
            "/api/authentication/global/static-key/generate",
            json={"expected_revision": 0},
        )
        assert stale.status_code == 409
        assert stale.json()["reason"] == "revision_conflict"
        assert raw not in stale.text


async def test_authentication_api_requires_lockout_confirmation_and_retires_legacy(tmp_path):
    registry = Registry(tmp_path / "registry.yaml")
    docs = tmp_path / "docs"
    docs.mkdir()
    registry.add(Project(name="KEI", documents_dir=docs, data_dir=tmp_path / "data"))
    store = AuthenticationPolicyStore(tmp_path / "authentication.yaml", project_names=["KEI"])
    config = CognitaConfig(
        registry_path=registry.path, data_root=tmp_path / "data", authentication_path=store.path,
        admin_allowed_hosts=["*"], admin_password_sha256="",
    )
    app = create_admin_app(config, registry, authentication_store=store)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        locked = await client.patch(
            "/api/authentication/projects/KEI",
            json={"expected_revision": 0, "oauth_mode": "disabled"},
        )
        assert locked.status_code == 409
        assert locked.json()["reason"] == "lockout_confirmation_required"
        confirmed = await client.patch(
            "/api/authentication/projects/KEI",
            json={"expected_revision": 0, "oauth_mode": "disabled", "confirm_lockout": True},
        )
        assert confirmed.status_code == 200
        assert confirmed.json()["projects"][0]["locked_out"] is True
        assert (await client.get("/api/debug-tokens-mode")).status_code == 410
        assert (await client.post("/api/projects/KEI/api-key")).status_code == 410


async def test_authentication_status_reports_connector_lockout_warning(tmp_path):
    registry = Registry(tmp_path / "registry.yaml")
    docs = tmp_path / "docs"
    docs.mkdir()
    registry.add(Project(name="KEI", documents_dir=docs, data_dir=tmp_path / "data"))
    authentication_store = AuthenticationPolicyStore(
        tmp_path / "authentication.yaml", project_names=["KEI"]
    )
    connector_store = ConnectorStore(tmp_path / "connectors.yaml")
    connector = connector_store.create(
        expected_revision=0, name="Primary", project_names=["KEI"]
    ).connectors[0]
    authentication_store.mutate_project(
        "KEI", expected_revision=0, oauth_mode="disabled", confirm_lockout=True
    )
    config = CognitaConfig(
        registry_path=registry.path,
        data_root=tmp_path / "data",
        authentication_path=authentication_store.path,
        admin_allowed_hosts=["*"],
        admin_password_sha256="",
    )
    app = create_admin_app(
        config,
        registry,
        authentication_store=authentication_store,
        connector_store=connector_store,
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/authentication")

    assert response.status_code == 200
    assert response.json()["warnings"] == {
        "locked_out_projects": ["KEI"],
        "locked_out_connectors": [connector.id],
    }


async def test_project_inherit_starts_oauth_before_committing(tmp_path):
    registry = Registry(tmp_path / "registry.yaml")
    registry.add(Project(name="KEI", documents_dir=tmp_path, data_dir=tmp_path))
    store = AuthenticationPolicyStore(tmp_path / "authentication.yaml", project_names=["KEI"])
    store.mutate_project(
        "KEI", expected_revision=0, oauth_mode="disabled", confirm_lockout=True
    )
    supervisor = ReadySupervisor()
    config = CognitaConfig(
        registry_path=registry.path, data_root=tmp_path / "data",
        authentication_path=store.path, admin_allowed_hosts=["*"],
        admin_password_sha256="",
    )
    app = create_admin_app(
        config, registry, authentication_store=store, oauth_supervisor=supervisor
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.patch(
            "/api/authentication/projects/KEI",
            json={"expected_revision": 1, "oauth_mode": "inherit"},
        )

    assert response.status_code == 200
    assert supervisor.starts == 1
    assert store.effective_oauth("KEI") is True


async def test_oauth_enable_failure_leaves_policy_unchanged(tmp_path):
    class FailedSupervisor(ReadySupervisor):
        async def start(self):
            self.starts += 1
            return SimpleNamespace(state="unavailable_terminal")

    registry = Registry(tmp_path / "registry.yaml")
    registry.add(Project(name="KEI", documents_dir=tmp_path, data_dir=tmp_path))
    store = AuthenticationPolicyStore(
        tmp_path / "authentication.yaml", project_names=["KEI"],
        legacy_oauth_enabled=False,
    )
    supervisor = FailedSupervisor()
    config = CognitaConfig(
        registry_path=registry.path, data_root=tmp_path / "data",
        authentication_path=store.path, admin_allowed_hosts=["*"],
        admin_password_sha256="",
    )
    app = create_admin_app(
        config, registry, authentication_store=store, oauth_supervisor=supervisor
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.patch(
            "/api/authentication/global",
            json={"expected_revision": 0, "oauth_enabled": True},
        )

    assert response.status_code == 503
    assert store.revision == 0
    assert store.effective_oauth("KEI") is False
    assert supervisor.starts == 1
    assert supervisor.stops == 1
