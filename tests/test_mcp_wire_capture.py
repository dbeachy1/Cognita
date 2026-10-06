"""13.2.10: with `mcp_wire_capture: true`, every connector-route exchange is written
whole — request body, response body, headers with the token redacted — to
<log_dir>/mcp-wire/. Added on 2026-09-23 to make failed exchanges inspectable.
Off by default: the files contain whatever the tools returned.
"""

from __future__ import annotations

import json

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from cognita.auth_policy import AuthenticationPolicyStore
from cognita.config import CognitaConfig
from cognita.connectors import ConnectorStore
from cognita.gateway import create_gateway_app
import cognita.gateway as gateway
from cognita.registry import Project, Registry
from engine_fakes import FakeEngineHost


def _app(tmp_path, *, capture: bool):
    docs = tmp_path / "docs"
    docs.mkdir(exist_ok=True)
    registry = Registry(tmp_path / "registry.yaml")
    registry.add(Project(name="RW", documents_dir=docs, data_dir=tmp_path / "data"))
    auth = AuthenticationPolicyStore(tmp_path / "authentication.yaml", project_names=["RW"])
    token = auth.mutate_global(expected_revision=0, oauth_enabled=False,
                               static_key_action="generate")["generated_key"]
    store = ConnectorStore(tmp_path / "connectors.yaml")
    config = store.create(expected_revision=0, name="Wire", project_names=["RW"])
    app = create_gateway_app(
        CognitaConfig(registry_path=registry.path, connectors_path=store.path, data_root=tmp_path,
                      log_dir=tmp_path / "logs", mcp_wire_capture=capture),
        registry, engine=FakeEngineHost(FastAPI()), connector_store=store,
        authentication_store=auth,
    )
    return app, config.connectors[0].slug, token


async def _call_plan(app, slug, token, section):
    body = {"jsonrpc": "2.0", "id": 41, "method": "tools/call",
            "params": {"name": "get_self_test_plan", "arguments": {"project": "RW", "section": section}}}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        return await client.post(f"/mcp/connectors/{slug}/mcp/v{PUBLIC_CONTRACT_VERSION}", json=body,
                                 headers={"Authorization": f"Bearer {token}", "User-Agent": "node"})


@pytest.mark.asyncio
async def test_capture_writes_the_whole_exchange_with_the_token_redacted(tmp_path):
    app, slug, token = _app(tmp_path, capture=True)
    r = await _call_plan(app, slug, token, "index")
    assert r.status_code == 200

    files = sorted((tmp_path / "logs" / "mcp-wire").glob("*.json"))
    assert len(files) == 1 and f"-{slug}-int_41.json" in files[0].name
    record = json.loads(files[0].read_text(encoding="utf-8"))
    assert record["connector"] == slug and record["route"] == "v5"
    # The request, whole: the exact JSON-RPC the client sent, and its headers.
    assert json.loads(record["request"]["body"])["params"]["arguments"] == {"project": "RW", "section": "index"}
    assert record["request"]["headers"]["authorization"] == "<redacted>"
    assert record["request"]["headers"]["user-agent"] == "node"
    assert token not in files[0].read_text(encoding="utf-8")
    # The response, whole: byte-identical to what went on the wire.
    assert record["response"]["status"] == 200
    assert record["response"]["body"] == r.text
    assert json.loads(record["response"]["body"])["result"]["structuredContent"]["section"] == "index"


@pytest.mark.asyncio
async def test_capture_redacts_credential_headers_on_both_sides_and_keeps_payload(tmp_path, monkeypatch):
    original_response = gateway.JSONResponse

    class CredentialHeaderResponse(original_response):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.headers.update({
                "Authorization": "response-auth-secret",
                "Set-Cookie": "response-cookie-secret",
                "Cookie": "response-request-cookie-secret",
                "X-API-Key": "response-api-secret",
                "API-Key": "response-key-secret",
                "Proxy-Authorization": "response-proxy-secret",
            })

    monkeypatch.setattr(gateway, "JSONResponse", CredentialHeaderResponse)
    app, slug, token = _app(tmp_path, capture=True)
    request_body = {"jsonrpc": "2.0", "id": "headers", "method": "tools/list"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            f"/mcp/connectors/{slug}/mcp/v{PUBLIC_CONTRACT_VERSION}", json=request_body,
            headers={
                "Authorization": f"Bearer {token}",
                "Proxy-Authorization": "request-proxy-secret",
                "Cookie": "session=request-cookie-secret",
                "Set-Cookie": "request-set-cookie-secret",
                "X-API-Key": "request-api-secret",
                "API-Key": "request-key-secret",
                "User-Agent": "FixtureClient/9.9",
            },
        )
    assert response.status_code == 200
    [capture] = list((tmp_path / "logs" / "mcp-wire").glob("*.json"))
    raw = capture.read_text(encoding="utf-8")
    for secret in (
        token, "request-proxy-secret", "request-cookie-secret", "request-set-cookie-secret",
        "request-api-secret", "request-key-secret", "response-auth-secret",
        "response-cookie-secret", "response-request-cookie-secret", "response-api-secret",
        "response-key-secret", "response-proxy-secret",
    ):
        assert secret not in raw
    record = json.loads(raw)
    for name in ("authorization", "proxy-authorization", "cookie", "set-cookie", "x-api-key", "api-key"):
        assert record["request"]["headers"][name] == "<redacted>"
    for name in ("authorization", "proxy-authorization", "cookie", "set-cookie", "x-api-key", "api-key"):
        assert record["response"]["headers"][name] == "<redacted>"
    assert record["request"]["headers"]["user-agent"] == "FixtureClient/9.9"
    assert json.loads(record["request"]["body"]) == request_body
    assert record["response"]["body"] == response.text


@pytest.mark.asyncio
async def test_capture_does_not_read_rejected_streaming_body(tmp_path):
    app, slug, _token = _app(tmp_path, capture=True)
    sent = []
    path = f"/mcp/connectors/{slug}/mcp/v{PUBLIC_CONTRACT_VERSION}"
    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
        "method": "POST", "scheme": "http", "root_path": "",
        "path": path, "raw_path": path.encode(), "query_string": b"",
        "server": ("test", 80), "client": ("test", 1234),
        "headers": [(b"authorization", b"Bearer invalid"),
                    (b"content-length", b"999999999")],
    }

    async def unread_body():
        raise AssertionError("rejected request body was consumed")

    async def send(message):
        sent.append(message)

    await app(scope, unread_body, send)
    assert next(message["status"] for message in sent if message["type"] == "http.response.start") == 401
    [capture] = list((tmp_path / "logs" / "mcp-wire").glob("*.json"))
    record = json.loads(capture.read_text(encoding="utf-8"))
    assert record["request"]["body"] is None
    assert record["request"]["headers"]["authorization"] == "<redacted>"
    assert record["response"]["status"] == 401


@pytest.mark.asyncio
async def test_capture_prunes_to_the_configured_keep(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir(exist_ok=True)
    registry = Registry(tmp_path / "registry.yaml")
    registry.add(Project(name="RW", documents_dir=docs, data_dir=tmp_path / "data"))
    auth = AuthenticationPolicyStore(tmp_path / "authentication.yaml", project_names=["RW"])
    token = auth.mutate_global(expected_revision=0, oauth_enabled=False,
                               static_key_action="generate")["generated_key"]
    store = ConnectorStore(tmp_path / "connectors.yaml")
    config = store.create(expected_revision=0, name="Wire", project_names=["RW"])
    app = create_gateway_app(
        CognitaConfig(registry_path=registry.path, connectors_path=store.path, data_root=tmp_path,
                      log_dir=tmp_path / "logs", mcp_wire_capture=True, mcp_wire_capture_keep=2),
        registry, engine=FakeEngineHost(FastAPI()), connector_store=store,
        authentication_store=auth,
    )
    slug = config.connectors[0].slug
    for section in ("1", "index", "1"):
        assert (await _call_plan(app, slug, token, section)).status_code == 200
    files = sorted((tmp_path / "logs" / "mcp-wire").glob("*.json"))
    assert len(files) == 2


@pytest.mark.asyncio
async def test_capture_is_off_by_default(tmp_path):
    app, slug, token = _app(tmp_path, capture=False)
    assert (await _call_plan(app, slug, token, "index")).status_code == 200
    assert not (tmp_path / "logs" / "mcp-wire").exists()
