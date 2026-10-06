from cognita.connectors import PUBLIC_CONTRACT_VERSION
import json
import logging

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from httpx import ASGITransport, AsyncClient

from cognita.auth_policy import AuthenticationPolicyStore
from cognita.config import CognitaConfig
from cognita.connectors import ConnectorStore
from cognita.gateway import create_gateway_app
from cognita.public_url import PublicBaseURLStore
from cognita.registry import Project, Registry
from cognita.tokens import generate_token, hash_token
from engine_fakes import FakeEngineHost


@pytest.fixture
def app_and_token(tmp_path):
    # 13.0 §7.3: the `config.test_mode` registry-token fallback this fixture
    # used to authenticate through is deleted. It now does what production
    # does — a parent-owned policy store with a global static key — so these
    # transport and routing tests still exercise a real authenticated call.
    registry = Registry(tmp_path / "registry.yaml")
    registry.add(Project(name="KEI", documents_dir=tmp_path, data_dir=tmp_path))
    auth = AuthenticationPolicyStore(
        tmp_path / "authentication.yaml", project_names=["KEI"]
    )
    # OAuth off, so these tests keep asserting against static authentication
    # alone: with OAuth on and no OAuth child wired, every unknown bearer
    # answers 503 instead of 401 and /healthz reports degraded.
    token = auth.mutate_global(
        expected_revision=0, oauth_enabled=False, static_key_action="generate"
    )["generated_key"]
    store = ConnectorStore(tmp_path / "connectors.yaml")
    connector = store.create(expected_revision=0, name="Primary", project_names=["KEI"])
    config = CognitaConfig(registry_path=tmp_path / "registry.yaml",
                           connectors_path=store.path)
    app = create_gateway_app(
        config, registry, connector_store=store, authentication_store=auth,
    )
    app.state.test_connector_store = store
    return app, token, connector.connectors[0].slug


async def _client(app):
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def test_healthz_no_auth(app_and_token):
    app, _, _ = app_and_token
    async with await _client(app) as c:
        r = await c.get("/healthz")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


async def test_mcp_missing_token_401(app_and_token):
    app, _, connector_id = app_and_token
    async with await _client(app) as c:
        r = await c.post(f"/mcp/connectors/{connector_id}/mcp/v{PUBLIC_CONTRACT_VERSION}", json={})
    assert r.status_code == 401


async def test_mcp_bad_token_401(app_and_token):
    app, _, connector_id = app_and_token
    async with await _client(app) as c:
        r = await c.post(f"/mcp/connectors/{connector_id}/mcp/v{PUBLIC_CONTRACT_VERSION}", json={},
                         headers={"Authorization": "Bearer nope"})
    assert r.status_code == 401


async def test_production_static_rejections_are_indistinguishable(tmp_path):
    registry = Registry(tmp_path / "registry.yaml")
    registry.add(Project(name="KEI", documents_dir=tmp_path, data_dir=tmp_path))
    connectors = ConnectorStore(tmp_path / "connectors.yaml")
    connector = connectors.create(expected_revision=0, name="Primary", project_names=["KEI"])
    auth = AuthenticationPolicyStore(
        tmp_path / "authentication.yaml", project_names=["KEI"]
    )
    auth.mutate_global(expected_revision=0, static_key_action="generate")
    app = create_gateway_app(
        CognitaConfig(connectors_path=connectors.path), registry,
        connector_store=connectors, authentication_store=auth,
    )
    path = f"/mcp/connectors/{connector.connectors[0].slug}/mcp/v{PUBLIC_CONTRACT_VERSION}"
    async with await _client(app) as client:
        missing = await client.post(path, json={})
        malformed = await client.post(path, json={}, headers={"Authorization": "Basic nope"})
        invalid = await client.post(
            path, json={}, headers={"Authorization": "Bearer cog_sk_v1_bad"}
        )

    assert (missing.status_code, missing.text, missing.headers["www-authenticate"]) == (
        invalid.status_code, invalid.text, invalid.headers["www-authenticate"]
    )
    assert (malformed.status_code, malformed.text, malformed.headers["www-authenticate"]) == (
        invalid.status_code, invalid.text, invalid.headers["www-authenticate"]
    )


async def test_policy_snapshot_drives_inherited_oauth_and_static_health(tmp_path):
    registry = Registry(tmp_path / "registry.yaml")
    registry.add(Project(name="KEI", documents_dir=tmp_path, data_dir=tmp_path))
    connectors = ConnectorStore(tmp_path / "connectors.yaml")
    connector = connectors.create(expected_revision=0, name="Primary", project_names=["KEI"])
    auth = AuthenticationPolicyStore(
        tmp_path / "authentication.yaml", project_names=["KEI"]
    )
    auth.mutate_project("KEI", expected_revision=0, static_key_action="generate")
    app = create_gateway_app(
        CognitaConfig(
            connectors_path=connectors.path,
            oauth_enabled=False,
            public_base_url="https://cognita.example",
        ),
        registry,
        connector_store=connectors,
        authentication_store=auth,
    )
    slug = connector.connectors[0].slug
    async with await _client(app) as client:
        metadata = await client.get(
            f"/.well-known/oauth-protected-resource/mcp/connectors/{slug}/mcp/v{PUBLIC_CONTRACT_VERSION}"
        )
        health = await client.get("/healthz")

    assert metadata.status_code == 200
    assert health.json()["authentication"]["static_keys_configured"] is True


async def test_excluded_project_has_generic_remote_unavailable_response(tmp_path):
    registry = Registry(tmp_path / "registry.yaml")
    registry.add(Project(name="Existing", documents_dir=tmp_path, data_dir=tmp_path))
    connectors = ConnectorStore(tmp_path / "connectors.yaml")
    connector = connectors.create(
        expected_revision=0, name="Primary", project_names=["Existing"]
    ).connectors[0]
    registry.add(
        Project(
            name="Excluded", documents_dir=tmp_path, data_dir=tmp_path,
            exclude_from_default_permissions=True,
        )
    )
    auth = AuthenticationPolicyStore(
        tmp_path / "authentication.yaml", project_names=["Existing", "Excluded"]
    )
    generated = auth.mutate_global(expected_revision=0, static_key_action="generate")
    key = generated["generated_key"]
    app = create_gateway_app(
        CognitaConfig(connectors_path=connectors.path, oauth_enabled=False),
        registry, connector_store=connectors, authentication_store=auth,
    )
    path = f"/mcp/connectors/{connector.slug}/mcp/v{PUBLIC_CONTRACT_VERSION}"
    request = {
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "list_assets", "arguments": {"project": "Excluded"}},
    }
    async with await _client(app) as client:
        response = await client.post(
            path, json=request, headers={"Authorization": f"Bearer {key}"}
        )
    assert response.status_code == 200
    result = response.json()["result"]["structuredContent"]
    assert result == {
        "status": "error",
        "reason": "project_unavailable",
        "message": "The requested project is unavailable through this connector.",
    }


async def test_project_key_explicitly_grants_excluded_project_and_infers_it(
    tmp_path, monkeypatch,
):
    registry = Registry(tmp_path / "registry.yaml")
    registry.add(Project(name="Other", documents_dir=tmp_path, data_dir=tmp_path))
    registry.add(Project(
        name="DeepSeek", documents_dir=tmp_path, data_dir=tmp_path,
        exclude_from_default_permissions=True,
    ))
    connectors = ConnectorStore(tmp_path / "connectors.yaml")
    connector = connectors.create(
        expected_revision=0, name="Cognita", project_names=["Other", "DeepSeek"]
    ).connectors[0]
    auth = AuthenticationPolicyStore(
        tmp_path / "authentication.yaml", project_names=["Other", "DeepSeek"]
    )
    generated = auth.mutate_project(
        "DeepSeek", expected_revision=0, static_key_action="generate"
    )
    seen = {}

    async def fake_proxy(_client, _request, _worker_url, **kwargs):
        seen.update(kwargs)
        seen["forwarded"] = json.loads(kwargs["body_override"])
        return JSONResponse({
            "jsonrpc": "2.0", "id": 3,
            "result": {"content": [], "isError": False},
        })

    monkeypatch.setattr("cognita.gateway.proxy_mcp", fake_proxy)
    app = create_gateway_app(
        CognitaConfig(connectors_path=connectors.path, oauth_enabled=False),
        # proxy_mcp is faked above, so the engine's app is never reached.
        registry, engine=FakeEngineHost(FastAPI()), connector_store=connectors,
        authentication_store=auth,
    )
    path = f"/mcp/connectors/{connector.slug}/mcp/v{PUBLIC_CONTRACT_VERSION}"
    headers = {"Authorization": f"Bearer {generated['generated_key']}"}
    async with await _client(app) as client:
        initialized = await client.post(path, headers=headers, json={
            "jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {},
        })
        listed = await client.post(path, headers=headers, json={
            "jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": "list_projects", "arguments": {}},
        })
        called = await client.post(path, headers=headers, json={
            "jsonrpc": "2.0", "id": 3, "method": "tools/call",
            "params": {"name": "list_assets", "arguments": {}},
        })

    assert initialized.status_code == 200
    assert listed.json()["result"]["structuredContent"]["projects"] == [
        {"name": "DeepSeek", "access": "write"}
    ]
    assert called.status_code == 200
    assert seen["project_name"] == "DeepSeek"
    assert seen["forwarded"]["params"]["arguments"] == {}


async def test_valid_key_initializes_with_no_accessible_projects(tmp_path, caplog):
    registry = Registry(tmp_path / "registry.yaml")
    registry.add(Project(name="Private", documents_dir=tmp_path, data_dir=tmp_path))
    connectors = ConnectorStore(tmp_path / "connectors.yaml")
    connector = connectors.create(
        expected_revision=0, name="Selected", project_mode="selected",
        default_access=None, project_names=["Private"],
    ).connectors[0]
    auth = AuthenticationPolicyStore(
        tmp_path / "authentication.yaml", project_names=["Private"]
    )
    generated = auth.mutate_project(
        "Private", expected_revision=0, static_key_action="generate"
    )
    app = create_gateway_app(
        CognitaConfig(connectors_path=connectors.path, oauth_enabled=False),
        registry, connector_store=connectors, authentication_store=auth,
    )
    path = f"/mcp/connectors/{connector.slug}/mcp/v{PUBLIC_CONTRACT_VERSION}"
    headers = {"Authorization": f"Bearer {generated['generated_key']}"}
    with caplog.at_level(logging.WARNING, logger="cognita.gateway"):
        async with await _client(app) as client:
            initialized = await client.post(path, headers=headers, json={
                "jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {},
            })
            listed = await client.post(path, headers=headers, json={
                "jsonrpc": "2.0", "id": 2, "method": "tools/call",
                "params": {"name": "list_projects", "arguments": {}},
            })
            denied = await client.post(path, headers=headers, json={
                "jsonrpc": "2.0", "id": 3, "method": "tools/call",
                "params": {"name": "list_assets", "arguments": {}},
            })

    assert initialized.status_code == 200
    assert listed.json()["result"]["structuredContent"]["projects"] == []
    denied_result = denied.json()["result"]
    assert denied.status_code == 200
    assert denied_result["isError"] is True
    assert denied_result["structuredContent"] == {
        "status": "error",
        "reason": "project_unavailable",
        "message": "No projects are configured for this key.",
    }
    assert "connector authenticated without projects" in caplog.text


async def test_missing_and_duplicate_authorization_headers_are_distinct_rejections(
    app_and_token,
):
    app, token, connector_id = app_and_token
    path = f"/mcp/connectors/{connector_id}/mcp/v{PUBLIC_CONTRACT_VERSION}"
    async with await _client(app) as c:
        missing = await c.post(path, json={})
        duplicate = await c.post(
            path,
            json={},
            headers=[
                ("Authorization", f"Bearer {token}"),
                ("Authorization", f"Bearer {token}"),
            ],
        )
    # A duplicated header never authenticates, even when both copies carry a
    # VALID key. Under the policy store both answers are the generic
    # rejection, which is the production body: "Authorization required" was
    # the no-policy compatibility wording this fixture used before 13.0
    # rewired it (the deleted `config.test_mode` path).
    assert missing.status_code == 401
    assert missing.text == "Invalid or expired credential"
    assert duplicate.status_code == 401
    assert duplicate.text == "Invalid or expired credential"


async def test_mcp_valid_token_routes(app_and_token):
    app, token, connector_id = app_and_token
    async with await _client(app) as c:
        r = await c.post(
            f"/mcp/connectors/{connector_id}/mcp/v{PUBLIC_CONTRACT_VERSION}",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                  "params": {"name": "search_knowledge",
                              "arguments": {"project": "KEI", "query": "x"}}},
            headers={"Authorization": f"Bearer {token}"},
        )
    assert r.status_code == 200
    assert "project_unavailable" in r.text


async def test_mcp_valid_token_in_path_routes(app_and_token):
    # claude.ai connectors carry the token in the URL path, not a header.
    app, token, _ = app_and_token
    async with await _client(app) as c:
        r = await c.post(f"/mcp/{token}", json={})
    assert r.status_code == 404


@pytest.mark.parametrize(
    "suffix",
    ["", "/mcp/v0", "/mcp/v01", "/mcp/v+1", "/mcp/v2/", "/mcp/v2?version=2"],
)
async def test_mcp_rejects_unversioned_and_noncanonical_generations(app_and_token, suffix):
    app, token, connector_id = app_and_token
    async with await _client(app) as c:
        r = await c.post(
            f"/mcp/connectors/{connector_id}{suffix}",
            json={},
            headers={"Authorization": f"Bearer {token}"},
        )
    assert r.status_code == 404


async def test_mcp_rejects_future_generation_after_authentication(app_and_token):
    app, token, connector_id = app_and_token
    async with await _client(app) as c:
        r = await c.post(
            f"/mcp/connectors/{connector_id}/mcp/v{PUBLIC_CONTRACT_VERSION + 1}",
            json={},
            headers={"Authorization": f"Bearer {token}"},
        )
    assert r.status_code == 404


async def test_mcp_rejects_retired_uuid_url_as_a_slug_alias(app_and_token):
    app, token, connector_slug = app_and_token
    connector_id = app.state.test_connector_store.snapshot().connectors[0].id
    assert connector_id != connector_slug
    async with await _client(app) as c:
        mcp = await c.post(
            f"/mcp/connectors/{connector_id}/mcp/v3",
            json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
            headers={"Authorization": f"Bearer {token}"},
        )
    assert mcp.status_code == 404


async def test_mcp_retires_previous_generations_and_serves_current(app_and_token):
    app, token, connector_slug = app_and_token
    async with await _client(app) as c:
        retired = await c.post(
            f"/mcp/connectors/{connector_slug}/mcp/v1",
            json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
            headers={"Authorization": f"Bearer {token}"},
        )
        retired_v3 = await c.post(
            f"/mcp/connectors/{connector_slug}/mcp/v3",
            json={"jsonrpc": "2.0", "id": 2, "method": "ping"},
            headers={"Authorization": f"Bearer {token}"},
        )
        retired_v4 = await c.post(
            f"/mcp/connectors/{connector_slug}/mcp/v4",
            json={"jsonrpc": "2.0", "id": 3, "method": "ping"},
            headers={"Authorization": f"Bearer {token}"},
        )
        current = await c.post(
            f"/mcp/connectors/{connector_slug}/mcp/v{PUBLIC_CONTRACT_VERSION}",
            json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
            headers={"Authorization": f"Bearer {token}"},
        )
        stable = await c.post(
            f"/mcp/connectors/{connector_slug}/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
            headers={"Authorization": f"Bearer {token}"},
        )
    assert retired.status_code == 404
    assert retired_v3.status_code == 404
    assert retired_v4.status_code == 404
    assert current.status_code == 200
    assert stable.status_code == 200
    assert stable.json() == current.json()


async def test_protected_resource_metadata_is_versioned_and_published(tmp_path):
    registry = Registry(tmp_path / "registry.yaml")
    registry.add(Project(name="KEI", documents_dir=tmp_path, data_dir=tmp_path))
    store = ConnectorStore(tmp_path / "connectors.yaml")
    connector = store.create(expected_revision=0, name="Primary", project_names=["KEI"])
    connector_slug = connector.connectors[0].slug
    connector_id = connector.connectors[0].id
    config = CognitaConfig(
        registry_path=registry.path,
        connectors_path=store.path,
        public_base_url="https://cognita.example",
        oauth_enabled=True,
    )
    app = create_gateway_app(config, registry, connector_store=store)
    async with await _client(app) as c:
        metadata = await c.get(
            f"/.well-known/oauth-protected-resource/mcp/connectors/{connector_slug}/mcp/v{PUBLIC_CONTRACT_VERSION}"
        )
        stable_metadata = await c.get(
            f"/.well-known/oauth-protected-resource/mcp/connectors/{connector_slug}/mcp"
        )
        stable_metadata_trailing = await c.get(
            f"/.well-known/oauth-protected-resource/mcp/connectors/{connector_slug}/mcp/"
        )
        stable_metadata_alias = await c.get(
            f"/.well-known/oauth-protected-resource/mcp/connectors/{connector_slug}/mcp/stable"
        )
        encoded_metadata_alias = await c.get(
            f"/.well-known/oauth-protected-resource/mcp/connectors/{connector_slug}/mcp/%76%35"
        )
        unversioned = await c.get(
            f"/.well-known/oauth-protected-resource/mcp/connectors/{connector_slug}"
        )
        future = await c.get(
            f"/.well-known/oauth-protected-resource/mcp/connectors/{connector_slug}/mcp/v{PUBLIC_CONTRACT_VERSION + 1}"
        )
        retired_uuid = await c.get(
            f"/.well-known/oauth-protected-resource/mcp/connectors/{connector_id}/mcp/v{PUBLIC_CONTRACT_VERSION}"
        )
    assert metadata.status_code == 200
    assert stable_metadata.status_code == 200
    assert stable_metadata_trailing.status_code == 404
    assert stable_metadata_alias.status_code == 404
    assert encoded_metadata_alias.status_code == 404
    assert stable_metadata.json()["resource"] == (
        f"https://cognita.example/mcp/connectors/{connector_slug}/mcp"
    )
    assert metadata.json()["resource"] == (
        f"https://cognita.example/mcp/connectors/{connector_slug}/mcp/v{PUBLIC_CONTRACT_VERSION}"
    )
    assert unversioned.status_code == 404
    assert future.status_code == 404
    assert retired_uuid.status_code == 404
    async with await _client(app) as c:
        retired = await c.get(
            f"/.well-known/oauth-protected-resource/mcp/connectors/{connector_slug}/mcp/v1"
        )
        retired_v3 = await c.get(
            f"/.well-known/oauth-protected-resource/mcp/connectors/{connector_slug}/mcp/v3"
        )
    assert retired.status_code == 404
    assert retired_v3.status_code == 404


async def test_protected_resource_metadata_tracks_admin_public_url_without_restart(tmp_path):
    registry = Registry(tmp_path / "registry.yaml")
    registry.add(Project(name="KEI", documents_dir=tmp_path, data_dir=tmp_path))
    connectors = ConnectorStore(tmp_path / "connectors.yaml")
    snapshot = connectors.create(expected_revision=0, name="Primary", project_names=["KEI"])
    connector_slug = snapshot.connectors[0].slug
    config = CognitaConfig(
        data_root=tmp_path / "data",
        registry_path=registry.path,
        connectors_path=connectors.path,
        public_base_url="https://deploy.example",
        oauth_enabled=True,
    )
    public_urls = PublicBaseURLStore(config)
    app = create_gateway_app(
        config, registry, connector_store=connectors, public_url_store=public_urls
    )
    route = f"/.well-known/oauth-protected-resource/mcp/connectors/{connector_slug}/mcp/v{PUBLIC_CONTRACT_VERSION}"

    async with await _client(app) as client:
        initial = await client.get(route)
        public_urls.save("https://new-tunnel.example/prefix")
        changed = await client.get(route)

    assert initial.json() == {
        "resource": f"https://deploy.example/mcp/connectors/{connector_slug}/mcp/v{PUBLIC_CONTRACT_VERSION}",
        "authorization_servers": ["https://deploy.example"],
        "scopes_supported": ["cognita:access"],
        "bearer_methods_supported": ["header"],
    }
    assert changed.json() == {
        "resource": (
            f"https://new-tunnel.example/prefix/mcp/connectors/{connector_slug}/mcp/v{PUBLIC_CONTRACT_VERSION}"
        ),
        "authorization_servers": ["https://new-tunnel.example/prefix"],
        "scopes_supported": ["cognita:access"],
        "bearer_methods_supported": ["header"],
    }


async def test_mcp_bad_token_in_path_401(app_and_token):
    app, _, connector_id = app_and_token
    async with await _client(app) as c:
        r = await c.post(f"/mcp/connectors/{connector_id}/mcp/v{PUBLIC_CONTRACT_VERSION}", json={},
                         headers={"Authorization": "Bearer nope"})
    assert r.status_code == 401


async def test_debug_tokens_mode_records_only_authenticated_tokens(tmp_path, caplog):
    registry = Registry(tmp_path / "registry.yaml")
    registry.add(Project(name="KEI", documents_dir=tmp_path, data_dir=tmp_path))
    auth = AuthenticationPolicyStore(
        tmp_path / "authentication.yaml", project_names=["KEI"]
    )
    token = auth.mutate_global(
        expected_revision=0, oauth_enabled=False, static_key_action="generate"
    )["generated_key"]
    store = ConnectorStore(tmp_path / "connectors.yaml")
    connector = store.create(expected_revision=0, name="Primary", project_names=["KEI"])
    config = CognitaConfig(registry_path=tmp_path / "registry.yaml",
                           connectors_path=store.path, debug_tokens_mode=True)
    app = create_gateway_app(
        config, registry, connector_store=store, authentication_store=auth,
    )
    with caplog.at_level(logging.WARNING, logger="cognita.gateway"):
        async with await _client(app) as c:
            bad = await c.post(f"/mcp/connectors/{connector.connectors[0].slug}/mcp/v{PUBLIC_CONTRACT_VERSION}", json={},
                               headers={"Authorization": "Bearer not-valid"})
            good = await c.post(f"/mcp/connectors/{connector.connectors[0].slug}/mcp/v{PUBLIC_CONTRACT_VERSION}", json={},
                                headers={"Authorization": f"Bearer {token}"})
    assert bad.status_code == 401
    assert good.status_code == 200
    messages = [r.getMessage() for r in caplog.records]
    assert not any(token in message for message in messages)


async def test_debug_tokens_mode_off_never_logs_valid_token(app_and_token, caplog):
    app, token, connector_id = app_and_token
    with caplog.at_level(logging.WARNING, logger="cognita.gateway"):
        async with await _client(app) as c:
            await c.post(f"/mcp/connectors/{connector_id}/mcp/v{PUBLIC_CONTRACT_VERSION}", json={},
                         headers={"Authorization": f"Bearer {token}"})
    assert token not in "\n".join(r.getMessage() for r in caplog.records)


async def test_release_mode_ignores_valid_static_keys_and_logs_no_secret(
    tmp_path, caplog, monkeypatch
):
    monkeypatch.setattr("cognita.gateway._STATIC_AUTH_ERROR_AT", 0.0)
    registry = Registry(tmp_path / "registry.yaml")
    token = generate_token()
    registry.add(Project(
        name="KEI", documents_dir=tmp_path, data_dir=tmp_path,
        token_sha256=hash_token(token),
    ))
    store = ConnectorStore(tmp_path / "connectors.yaml")
    connector = store.create(expected_revision=0, name="Primary", project_names=["KEI"])
    app = create_gateway_app(
        CognitaConfig(registry_path=registry.path, connectors_path=store.path),
        registry, connector_store=store,
    )
    with caplog.at_level(logging.ERROR, logger="cognita.gateway"):
        async with await _client(app) as c:
            header = await c.post(
                f"/mcp/connectors/{connector.connectors[0].slug}/mcp/v{PUBLIC_CONTRACT_VERSION}", json={},
                headers={"Authorization": f"Bearer {token}"}
            )
            path = await c.post(f"/mcp/{token}", json={})
    assert header.status_code == 401
    assert path.status_code == 404
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "RELEASE mode" in logged
    assert token not in logged
