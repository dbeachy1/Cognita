import logging

import pytest
from httpx import ASGITransport, AsyncClient

from cognita.config import CognitaConfig
from cognita.gateway import _is_expected_discovery_probe, create_gateway_app
from cognita.registry import Registry

CONNECTOR_ID = "9b6812eb-bb42-4e4b-b1b0-e837fcc552d0"


@pytest.fixture
def app(tmp_path):
    registry = Registry(tmp_path / "registry.yaml")
    config = CognitaConfig(registry_path=tmp_path / "registry.yaml")
    return create_gateway_app(config, registry)


async def test_unmatched_route_logs_hint_and_redacts_token(app, caplog):
    caplog.set_level(logging.WARNING, logger="cognita.gateway")
    # Simulate the classic mistake: token pasted without /mcp/
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.post("/OwyQvlUamcYy46oY3pXZeBGOmmSMAdzy89NZsu_Lj5w", json={})
    assert r.status_code == 404
    assert "/mcp/connectors/<connector-slug>/mcp/v<generation>" in r.text
    logged = "\n".join(caplog.messages)
    assert "no matching route" in logged
    # the token must NOT appear in logs; it should be redacted
    assert "OwyQvlUamcYy46oY3pXZeBGOmmSMAdzy89NZsu_Lj5w" not in logged
    assert "<token>" in logged


async def test_oidc_discovery_miss_is_debug_and_redacts_token(app, caplog):
    caplog.set_level(logging.DEBUG, logger="cognita.gateway")
    paths = [
        "/.well-known/openid-configuration",
        f"/.well-known/openid-configuration/mcp/connectors/{CONNECTOR_ID}/mcp/v2",
        "/.well-known/oauth-authorization-server/issuer",
        f"/mcp/connectors/{CONNECTOR_ID}/mcp/v2/.well-known/openid-configuration",
        f"/.well-known/oauth-protected-resource/mcp/connectors/{CONNECTOR_ID}/mcp/v2/.well-known/oauth-authorization-server",
    ]
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        responses = [await c.get(path) for path in paths]
    assert all(response.status_code == 404 for response in responses)
    records = [record for record in caplog.records if record.name == "cognita.gateway"]
    assert len(records) == len(paths)
    assert all(record.levelno < logging.WARNING for record in records)
    logged = "\n".join(record.getMessage() for record in records)
    assert CONNECTOR_ID not in logged
    assert "<token>" in records[1].getMessage()


async def test_oidc_discovery_lookalike_still_warns_and_redacts_token(app, caplog):
    caplog.set_level(logging.WARNING, logger="cognita.gateway")
    secret = "OwyQvlUamcYy46oY3pXZeBGOmmSMAdzy89"
    path = f"/.well-known/openid-configuration-malformed/{secret}"
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        response = await c.get(path)
    assert response.status_code == 404
    assert len(caplog.records) == 1
    assert caplog.records[0].levelno == logging.WARNING
    assert secret not in caplog.records[0].getMessage()
    assert "<token>" in caplog.records[0].getMessage()


def test_discovery_probe_predicate_rejects_malformed_and_unknown_shapes():
    assert _is_expected_discovery_probe("/.well-known/openid-configuration")
    assert _is_expected_discovery_probe("/.well-known/oauth-authorization-server/issuer")
    assert _is_expected_discovery_probe(
        f"/.well-known/openid-configuration/mcp/connectors/{CONNECTOR_ID}/mcp/v2"
    )
    assert _is_expected_discovery_probe(
        f"/mcp/connectors/{CONNECTOR_ID}/mcp/v2/.well-known/oauth-authorization-server"
    )
    for path in (
        "/.well-known/other-metadata",
        "/.well-known/openid-configuration-malformed",
        "/.well-known/oauth-authorization-server.evil",
        "/.well-known/openid-configuration/mcp/",
        "/.well-known/openid-configuration/mcp//token",
        "/.well-known/openid-configuration/mcp/legacy-project",
        "/.well-known/openid-configuration/mcp/connectors/not-a-uuid",
        "/.well-known/openid-configuration/issuer//child",
        "/.well-known/openid-configuration/issuer/../child",
        "/mcp//.well-known/openid-configuration",
        "/mcp/legacy-project/.well-known/openid-configuration",
        "/mcp/connectors/not-a-uuid/.well-known/openid-configuration",
        f"/mcp/connectors/{CONNECTOR_ID}/extra/.well-known/openid-configuration",
        "/other-resource/.well-known/openid-configuration",
    ):
        assert not _is_expected_discovery_probe(path), path
