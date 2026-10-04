"""13.2.4: the connector MCP route logs every exchange, and never a secret.

On 2026-09-22, a client showed "[No content]" for a 218 KB tool result.
These tests pin what each line carries — the request framing, the
client's headers, the reply's shape and sizes — and what it must never carry:
the bearer token, argument values, or content.
"""

from __future__ import annotations

import logging

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from cognita.auth_policy import AuthenticationPolicyStore
from cognita.config import CognitaConfig
from cognita.connectors import ConnectorStore
from cognita.gateway import create_gateway_app
from cognita.registry import Project, Registry
from engine_fakes import FakeEngineHost


@pytest.fixture
def env(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    registry = Registry(tmp_path / "registry.yaml")
    registry.add(Project(name="RW", documents_dir=docs, data_dir=tmp_path / "data"))
    auth = AuthenticationPolicyStore(tmp_path / "authentication.yaml", project_names=["RW"])
    token = auth.mutate_global(
        expected_revision=0, oauth_enabled=False, static_key_action="generate"
    )["generated_key"]
    store = ConnectorStore(tmp_path / "connectors.yaml")
    config = store.create(expected_revision=0, name="Logged", project_names=["RW"])
    app = create_gateway_app(
        CognitaConfig(registry_path=registry.path, connectors_path=store.path,
                      data_root=tmp_path),
        registry, engine=FakeEngineHost(FastAPI()), connector_store=store,
        authentication_store=auth,
    )
    return app, config.connectors[0].slug, token


async def _post(app, slug, token, body, *, headers=None):
    sent = {"Authorization": f"Bearer {token}", **(headers or {})}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        return await client.post(f"/mcp/connectors/{slug}/mcp/v5", json=body, headers=sent)


def _lines(caplog, prefix):
    return [r.getMessage() for r in caplog.records
            if r.name == "cognita.gateway" and r.getMessage().startswith(prefix)]


@pytest.mark.asyncio
async def test_tool_call_exchange_logs_framing_sizes_and_headers(env, caplog):
    app, slug, token = env
    caplog.set_level(logging.INFO, logger="cognita.gateway")
    r = await _post(app, slug, token, {
        "jsonrpc": "2.0", "id": "st-7", "method": "tools/call",
        "params": {"name": "get_self_test_plan",
                   "arguments": {"project": "RW", "section": "index"}},
    }, headers={"Accept": "application/json, text/event-stream",
                "MCP-Protocol-Version": "2025-06-18",
                "User-Agent": "FixtureClient/9.9"})
    assert r.status_code == 200, r.text
    result = r.json()["result"]

    [exchange] = _lines(caplog, "mcp exchange ")
    assert f"connector={slug} route=v5 http_method=POST methods=tools/call ids=str:st-7 batch=no" in exchange
    assert "accept=application/json, text/event-stream" in exchange
    assert "content_type=application/json" in exchange
    assert "protocol_version=2025-06-18 session_id=absent user_agent=FixtureClient/9.9" in exchange
    assert f"-> http=200 media_type=application/json response_bytes={len(r.content)}" in exchange

    [call] = _lines(caplog, "tool call ")
    # 13.2.9: identifier-like argument VALUES are logged (project, section);
    # content-like ones never are (see the write test below).
    assert "tool=get_self_test_plan id=str:st-7 arguments=project=RW,section=index status=success reason=-" in call
    assert "is_error=False content_blocks=1 block_types=text" in call
    assert f"text_chars={len(result['content'][0]['text'])}" in call
    assert "structured_bytes=" in call and f"bytes={len(r.content)}" in call
    assert "structured_keys=plan_version,section,sections,server_version,status" in call
    assert "text_sha=" in call and "non_ascii=" in call and "control_chars=0 lines=" in call
    assert "content_type=application/json" in call

    # No line — not the exchange, not the call — carries the bearer token.
    assert token not in caplog.text


@pytest.mark.asyncio
async def test_initialize_logs_the_client_identity(env, caplog):
    app, slug, token = env
    caplog.set_level(logging.INFO, logger="cognita.gateway")
    r = await _post(app, slug, token, {
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-03-26",
                   "clientInfo": {"name": "SillyTavern", "version": "1.13.0"},
                   "capabilities": {"roots": {}, "sampling": {}}},
    })
    assert r.status_code == 200
    [init] = _lines(caplog, "mcp initialize ")
    assert init == ("mcp initialize id=int:1 client=SillyTavern client_version=1.13.0 "
                    "protocol=2025-03-26 capabilities=roots,sampling")
    [exchange] = _lines(caplog, "mcp exchange ")
    assert "methods=initialize ids=int:1 batch=no" in exchange
    assert token not in caplog.text


@pytest.mark.asyncio
async def test_batch_and_rejected_requests_are_logged_too(env, caplog):
    app, slug, token = env
    caplog.set_level(logging.INFO, logger="cognita.gateway")
    r = await _post(app, slug, token, [
        {"jsonrpc": "2.0", "id": 1, "method": "ping"},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
    ])
    assert r.status_code == 200
    [exchange] = _lines(caplog, "mcp exchange ")
    assert "methods=ping,notifications/initialized ids=int:1,null batch=2" in exchange

    caplog.clear()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        r = await client.post(f"/mcp/connectors/{slug}/mcp/v5", json={
            "jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            headers={"Authorization": "Bearer not-the-key"})
    assert r.status_code == 401
    [exchange] = _lines(caplog, "mcp exchange ")
    assert "methods=- ids=- batch=- request_bytes=unknown" in exchange
    assert "-> http=401" in exchange
    assert "not-the-key" not in caplog.text


@pytest.mark.asyncio
async def test_rejected_streaming_post_is_not_read_for_logging(env, caplog):
    app, slug, _token = env
    caplog.set_level(logging.INFO, logger="cognita.gateway")
    sent = []
    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
        "method": "POST", "scheme": "http", "root_path": "",
        "path": f"/mcp/connectors/{slug}/mcp/v5",
        "raw_path": f"/mcp/connectors/{slug}/mcp/v5".encode(),
        "query_string": b"", "server": ("test", 80), "client": ("test", 1234),
        "headers": [(b"authorization", b"Bearer invalid"),
                    (b"content-length", b"999999999")],
    }

    async def unread_body():
        raise AssertionError("rejected request body was consumed")

    async def send(message):
        sent.append(message)

    await app(scope, unread_body, send)
    assert next(message["status"] for message in sent if message["type"] == "http.response.start") == 401
    [exchange] = _lines(caplog, "mcp exchange ")
    assert "methods=- ids=- batch=- request_bytes=unknown" in exchange
    assert "999999999" not in exchange  # declared length is not an observed size


@pytest.mark.asyncio
async def test_content_arguments_are_named_but_never_printed(env, caplog):
    app, slug, token = env
    caplog.set_level(logging.INFO, logger="cognita.gateway")
    await _post(app, slug, token, {
        "jsonrpc": "2.0", "id": 9, "method": "tools/call",
        "params": {"name": "add_document",
                   "arguments": {"project": "RW", "filepath": "notes/x.md",
                                 "content": "SECRET-BODY-7f3a do not log me"}},
    })
    [call] = _lines(caplog, "tool call ")
    # Identifiers in, content out: the name says it was sent, the value never appears.
    assert "arguments=content,filepath=notes/x.md,project=RW" in call
    assert "SECRET-BODY" not in caplog.text and "do not log me" not in caplog.text


@pytest.mark.asyncio
async def test_tools_list_is_logged_with_its_catalog_digest(env, caplog):
    app, slug, token = env
    caplog.set_level(logging.INFO, logger="cognita.gateway")
    r = await _post(app, slug, token, {"jsonrpc": "2.0", "id": 3, "method": "tools/list"})
    tools = r.json()["result"]["tools"]
    [line] = _lines(caplog, "tools list ")
    assert f"tools list connector_id={slug} id=int:3 contract=v" in line
    assert f"tools={len(tools)} catalog_sha=" in line
    assert token not in caplog.text
