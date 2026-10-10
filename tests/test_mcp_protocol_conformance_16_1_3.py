"""16.1.3 (Part B): MCP protocol handling on both gateway routes and the engine.

Version negotiation, JSON-RPC message kinds decided by shape, replies without
an id member when the id cannot be known, bodies that must never produce HTTP
500, the RFC 6750 `invalid_token` challenge, and the Workspace-only route's
per-exchange log line.

The governing rule is that nothing that connects and works today stops working.
The regression the first tests pin: a client that probes with `server/discover`
and the header `MCP-Protocol-Version: 2026-07-28` must keep getting HTTP 200,
JSON-RPC -32601 and its own id (byte for byte), and then `initialize` works.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

import pytest
from argon2 import PasswordHasher
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from cognita.auth_policy import AuthenticationPolicyStore, CredentialPolicyStore
from cognita.config import CognitaConfig
from cognita.connectors import PUBLIC_CONTRACT_VERSION, ConnectorStore, WorkspaceConnectorStore
from cognita.engine_local import LocalEngineHost
from cognita.gateway import create_gateway_app
from cognita.mcp_protocol import (
    INVALID,
    LATEST_PROTOCOL_VERSION,
    NO_ID,
    NOTIFICATION,
    REQUEST,
    RESPONSE,
    SUPPORTED_PROTOCOL_VERSIONS,
    classify_message,
    error_body,
    negotiate_protocol_version,
)
from cognita.oauth_service_client import IntrospectionResult
from cognita.proxy import _handle_batch
from cognita.registry import Project, Registry
from cognita.retrieval import RetrievalCore
from cognita.store import Store
from cognita.tokens import generate_token, hash_token
from engine_fakes import FakeEngineHost
from retrieval_fakes import HashEmbedder, OverlapReranker

PING = {"jsonrpc": "2.0", "id": 7, "method": "ping"}
DEEP = 100_000


# --------------------------------------------------------------------- fixtures


class RecordingWorkspace:
    """A fake WorkspaceManager that records which tools were executed."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def execute(self, principal, tool, arguments, *, connector_id=None):
        self.calls.append(tool)
        return {"status": "success", "workspace": {}, "data": {}}


class ToolCallCounter(logging.Handler):
    """Counts the gateway's `tool call` lines: one per executed tools/call."""

    def __init__(self) -> None:
        super().__init__(level=logging.INFO)
        self.count = 0

    def emit(self, record: logging.LogRecord) -> None:
        if record.getMessage().startswith("tool call "):
            self.count += 1


@dataclass
class Surface:
    name: str
    app: FastAPI
    url: str
    token: str
    call_params: dict
    workspace: RecordingWorkspace | None = None
    counter: ToolCallCounter | None = None
    sent: list = field(default_factory=list)

    @property
    def executed(self) -> int:
        """How many tools/call requests actually ran."""
        return len(self.workspace.calls) if self.workspace is not None else self.counter.count

    async def post(self, body: Any, *, headers: dict | None = None, token: str | None = ""):
        sent = {} if token is None else {"Authorization": f"Bearer {self.token if token == '' else token}"}
        sent.update(headers or {})
        async with AsyncClient(transport=ASGITransport(app=self.app), base_url="http://test") as client:
            if isinstance(body, (bytes, bytearray)):
                return await client.post(self.url, content=bytes(body), headers={
                    "Content-Type": "application/json", **sent})
            return await client.post(self.url, json=body, headers=sent)


def _connector_surface(tmp_path) -> Surface:
    docs = tmp_path / "docs"
    docs.mkdir()
    registry = Registry(tmp_path / "registry.yaml")
    registry.add(Project(name="RW", documents_dir=docs, data_dir=tmp_path / "data"))
    auth = AuthenticationPolicyStore(tmp_path / "authentication.yaml", project_names=["RW"])
    token = auth.mutate_global(
        expected_revision=0, oauth_enabled=False, static_key_action="generate"
    )["generated_key"]
    store = ConnectorStore(tmp_path / "connectors.yaml")
    created = store.create(expected_revision=0, name="Conformance", project_names=["RW"])
    app = create_gateway_app(
        CognitaConfig(registry_path=registry.path, connectors_path=store.path, data_root=tmp_path),
        registry, engine=FakeEngineHost(FastAPI()), connector_store=store,
        authentication_store=auth,
    )
    counter = ToolCallCounter()
    logging.getLogger("cognita.gateway").addHandler(counter)
    logging.getLogger("cognita.gateway").setLevel(logging.INFO)
    slug = created.connectors[0].slug
    return Surface(
        "connector", app, f"/mcp/connectors/{slug}/mcp/v{PUBLIC_CONTRACT_VERSION}", token,
        {"name": "get_self_test_plan", "arguments": {"project": "RW", "section": "index"}},
        counter=counter,
    )


def _workspace_surface(tmp_path) -> Surface:
    registry = Registry(tmp_path / "registry.yaml")
    registry.add(Project(name="Knowledge", documents_dir=tmp_path, data_dir=tmp_path))
    workspace_connectors = WorkspaceConnectorStore(tmp_path / "workspace-connectors.yaml")
    surface = workspace_connectors.create(
        expected_revision=0, display_name="Workspace", enabled=True, slug="workspace",
    )
    credentials = CredentialPolicyStore(
        tmp_path / "credentials-v2.json",
        master_key_dir=tmp_path / "master-keys",
        admin_password_hash=PasswordHasher().hash("correct horse"),
    )
    _row, secret = credentials.add_credential(
        "workspace", surface.id, "Runner", surface_slug=surface.slug, password="correct horse",
    )
    manager = RecordingWorkspace()
    app = create_gateway_app(
        CognitaConfig(registry_path=registry.path, public_base_url="https://example.test",
                      oauth_enabled=False),
        registry,
        connector_store=ConnectorStore(tmp_path / "connectors.yaml"),
        workspace_connector_store=workspace_connectors,
        credential_store=credentials,
        workspace_service=manager,
    )
    return Surface(
        "workspace", app, "/mcp/workspace/workspace/mcp/v3", secret,
        {"name": "workspace_info", "arguments": {}}, workspace=manager,
    )


@pytest.fixture(params=["connector", "workspace"])
def surface(request, tmp_path):
    made = _connector_surface(tmp_path) if request.param == "connector" else _workspace_surface(tmp_path)
    yield made
    if made.counter is not None:
        logging.getLogger("cognita.gateway").removeHandler(made.counter)


def _tool_call(surface: Surface, msg_id=None) -> dict:
    message = {"jsonrpc": "2.0", "method": "tools/call", "params": surface.call_params}
    if msg_id is not None:
        message["id"] = msg_id
    return message


# ------------------------------------------------------- B1: version negotiation


@pytest.mark.parametrize("version", SUPPORTED_PROTOCOL_VERSIONS)
async def test_supported_protocol_versions_are_echoed(surface, version):
    r = await surface.post({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                            "params": {"protocolVersion": version}})
    assert r.status_code == 200
    assert r.json()["result"]["protocolVersion"] == version


@pytest.mark.parametrize("requested", ["2026-07-28", "banana", "", "2025-11-26", 20250326, None,
                                       True, ["2025-03-26"], {"v": 1}])
async def test_unsupported_protocol_version_is_answered_with_the_newest(surface, requested):
    r = await surface.post({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                            "params": {"protocolVersion": requested}})
    assert r.status_code == 200
    assert r.json()["result"]["protocolVersion"] == "2025-11-25"


@pytest.mark.parametrize("params", [None, {}, {"clientInfo": {"name": "x"}}, [], "text"])
async def test_absent_protocol_version_keeps_the_legacy_answer(surface, params):
    message = {"jsonrpc": "2.0", "id": 1, "method": "initialize"}
    if params is not None:
        message["params"] = params
    r = await surface.post(message)
    assert r.status_code == 200
    assert r.json()["result"]["protocolVersion"] == "2025-03-26"


async def test_initialize_in_a_batch_is_negotiated_too(surface):
    r = await surface.post([
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2026-07-28"}},
        {"jsonrpc": "2.0", "id": 2, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
    ])
    assert [item["result"]["protocolVersion"] for item in r.json()] == ["2025-11-25", "2025-06-18"]


def test_negotiation_function_is_the_one_authority():
    assert SUPPORTED_PROTOCOL_VERSIONS == ("2025-11-25", "2025-06-18", "2025-03-26")
    assert LATEST_PROTOCOL_VERSION == "2025-11-25"
    assert negotiate_protocol_version({"protocolVersion": "2025-06-18"}) == "2025-06-18"
    assert negotiate_protocol_version({"protocolVersion": "2026-07-28"}) == "2025-11-25"
    assert negotiate_protocol_version({}) == "2025-03-26"
    assert negotiate_protocol_version(None) == "2025-03-26"


async def test_negotiation_downgrade_is_logged(surface, caplog):
    caplog.set_level(logging.INFO, logger="cognita.gateway")
    await surface.post({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                        "params": {"protocolVersion": "2026-07-28"}})
    assert "mcp version negotiated requested=2026-07-28 granted=2025-11-25" in caplog.text


# ----------------------------------- the server/discover probe must stay exactly


DISCOVER_HEADERS = {"MCP-Protocol-Version": "2026-07-28"}


@pytest.mark.parametrize("msg_id", [1, "disc-1", 0])
async def test_server_discover_probe_is_unchanged_byte_for_byte(surface, msg_id):
    r = await surface.post(
        {"jsonrpc": "2.0", "id": msg_id, "method": "server/discover"}, headers=DISCOVER_HEADERS)
    assert r.status_code == 200
    expected = {"jsonrpc": "2.0", "id": msg_id,
                "error": {"code": -32601, "message": "Method not found: server/discover"}}
    assert r.json() == expected
    assert r.content == json.dumps(expected, separators=(",", ":")).encode("utf-8")


async def test_initialize_after_the_probe_succeeds_with_the_newest_version(surface):
    probe = await surface.post(
        {"jsonrpc": "2.0", "id": 1, "method": "server/discover"}, headers=DISCOVER_HEADERS)
    assert probe.status_code == 200 and probe.json()["error"]["code"] == -32601
    init = await surface.post(
        {"jsonrpc": "2.0", "id": 2, "method": "initialize",
         "params": {"protocolVersion": "2025-11-25", "capabilities": {}}},
        headers=DISCOVER_HEADERS)
    assert init.status_code == 200
    assert init.json()["id"] == 2
    assert init.json()["result"]["protocolVersion"] == "2025-11-25"
    # The header is never validated or rejected, whatever it says.
    for header in ("2026-07-28", "banana", "2025-11-25", ""):
        again = await surface.post(PING, headers={"MCP-Protocol-Version": header})
        assert again.status_code == 200 and again.json()["id"] == 7


@pytest.mark.parametrize("method", ["server/discover", "no/such/method", "tools/frobnicate"])
async def test_unknown_method_with_a_valid_id_is_minus_32601_with_the_id(surface, method):
    r = await surface.post({"jsonrpc": "2.0", "id": "x-1", "method": method})
    assert r.status_code == 200
    assert r.json() == {"jsonrpc": "2.0", "id": "x-1",
                        "error": {"code": -32601, "message": f"Method not found: {method}"}}


# ------------------------------------------------------- B2: kinds decided by shape


@pytest.mark.parametrize("method", ["notifications/initialized", "notifications/cancelled",
                                    "notifications/anything"])
async def test_notifications_methods_are_202_never_executed(surface, method):
    r = await surface.post({"jsonrpc": "2.0", "method": method})
    assert r.status_code == 202
    assert r.content == b""
    assert surface.executed == 0


# 16.1.3 leniency (deliberate): JSON-RPC says a message without an id is a
# notification and must not be answered, but clients that omit the id (or send
# null, a bool, an object, an array) were answered before and must keep
# working. Any non-notifications/ string method is a request whatever its id,
# and the reply echoes the id the way 16.1.2 did.
@pytest.mark.parametrize("id_json,echoed", [
    (None, None), ("null", None), ("true", True), ("false", False), ("{}", {}), ("[]", []),
    ('{"a":1}', {"a": 1}), ("[1]", [1]), ('"a"', "a"), ("5", 5), ("0", 0), ("1.5", 1.5),
])
async def test_any_id_or_none_still_makes_a_string_method_a_request(surface, id_json, echoed):
    parts = ['"jsonrpc":"2.0"', '"method":"ping"']
    if id_json is not None:
        parts.append('"id":%s' % id_json)
    r = await surface.post(("{" + ",".join(parts) + "}").encode())
    assert r.status_code == 200
    # A missing id comes back as "id": null, byte for byte as before 16.1.3.
    assert r.content == json.dumps({"jsonrpc": "2.0", "id": echoed, "result": {}},
                                   separators=(",", ":")).encode()


@pytest.mark.parametrize("id_json", [None, "null", "true", "{}", "[]"])
async def test_unknown_method_without_a_usable_id_is_still_minus_32601_echoing_it(surface, id_json):
    parts = ['"jsonrpc":"2.0"', '"method":"server/discover"']
    if id_json is not None:
        parts.append('"id":%s' % id_json)
    r = await surface.post(("{" + ",".join(parts) + "}").encode())
    assert r.status_code == 200
    echoed = {"null": None, "true": True, "{}": {}, "[]": []}.get(id_json)
    assert r.json() == {"jsonrpc": "2.0", "id": echoed,
                        "error": {"code": -32601, "message": "Method not found: server/discover"}}


async def test_a_tool_call_without_an_id_is_still_executed_and_answered(surface):
    r = await surface.post(_tool_call(surface))  # no id member
    assert r.status_code == 200 and r.json()["id"] is None
    assert surface.executed == 1
    r = await surface.post(_tool_call(surface, 11))
    assert r.status_code == 200 and r.json()["id"] == 11
    assert surface.executed == 2


@pytest.mark.parametrize("msg_id", ["abc", "", 5, 0, -3, 1.5, 10**20])
async def test_string_integer_and_finite_number_ids_are_requests(surface, msg_id):
    r = await surface.post({"jsonrpc": "2.0", "id": msg_id, "method": "ping"})
    assert r.status_code == 200
    assert r.json() == {"jsonrpc": "2.0", "id": msg_id, "result": {}}


@pytest.mark.parametrize("name", ["notifications/initialized", "notifications/cancelled"])
async def test_notification_method_with_a_valid_id_keeps_todays_202(surface, name):
    r = await surface.post({"jsonrpc": "2.0", "id": 4, "method": name})
    assert r.status_code == 202 and r.content == b""


@pytest.mark.parametrize("id_json", ["null", "true", "NaN", "{}", "[1]"])
async def test_notification_method_with_an_invalid_id_stays_202(surface, id_json):
    # Permissive: every notifications/* message has always been answered 202,
    # and some JSON-RPC libraries write "id": null on notifications.
    raw = ('{"jsonrpc":"2.0","method":"notifications/initialized","id":%s}' % id_json).encode()
    r = await surface.post(raw)
    assert r.status_code == 202 and r.content == b""


@pytest.mark.parametrize("method", ["ping", "tools/call", "initialize", "no/such/method"])
@pytest.mark.parametrize("id_json", ["NaN", "Infinity", "-Infinity"])
async def test_a_non_finite_id_is_the_only_refused_id(surface, method, id_json):
    # These used to crash response encoding with HTTP 500. Refused, never run.
    params = (',"params":{"name":"%s","arguments":{}}' % surface.call_params["name"]
              if method == "tools/call" else "")
    raw = ('{"jsonrpc":"2.0","method":"%s","id":%s%s}' % (method, id_json, params)).encode()
    r = await surface.post(raw)
    assert r.status_code == 200
    assert r.json() == {"jsonrpc": "2.0", "error": {"code": -32600, "message": "Invalid request"}}
    assert surface.executed == 0


@pytest.mark.parametrize("raw", [b"5", b'"text"', b"true", b"null", b"{}", b'{"jsonrpc":"2.0"}',
                                 b'{"jsonrpc":"2.0","id":3,"params":{}}'])
async def test_anything_else_is_an_invalid_request(surface, raw):
    r = await surface.post(raw)
    assert r.status_code == 200
    body = r.json()
    assert body["error"]["code"] == -32600
    # A valid id the message carried is echoed; otherwise there is no id member.
    if b'"id":3' in raw:
        assert body["id"] == 3
    else:
        assert "id" not in body


async def test_a_non_string_method_is_an_invalid_request_that_echoes_a_valid_id(surface):
    r = await surface.post({"jsonrpc": "2.0", "id": 9, "method": 5})
    assert r.json() == {"jsonrpc": "2.0", "id": 9, "error": {"code": -32600, "message": "Invalid request"}}
    r = await surface.post({"jsonrpc": "2.0", "method": ["ping"]})
    assert r.json() == {"jsonrpc": "2.0", "error": {"code": -32600, "message": "Invalid request"}}


@pytest.mark.parametrize("message", [
    {"jsonrpc": "2.0", "id": 1, "result": {}},
    {"jsonrpc": "2.0", "id": 2, "result": {"roots": []}},
    {"jsonrpc": "2.0", "id": 3, "error": {"code": -32601, "message": "no"}},
    {"jsonrpc": "2.0", "result": {}},
])
async def test_a_client_response_is_accepted_with_202_and_no_body(surface, message):
    r = await surface.post(message)
    assert r.status_code == 202 and r.content == b""
    assert surface.executed == 0


async def test_method_wins_over_result_and_error(surface):
    r = await surface.post({"jsonrpc": "2.0", "id": 5, "method": "ping", "result": {}})
    assert r.status_code == 200 and r.json() == {"jsonrpc": "2.0", "id": 5, "result": {}}
    r = await surface.post({"jsonrpc": "2.0", "method": "notifications/initialized", "error": {}})
    assert r.status_code == 202


# ----------------------------------------------------------------- B2: batches


async def test_batch_members_are_classified_individually(surface):
    r = await surface.post(json.dumps([
        {"jsonrpc": "2.0", "id": 1, "method": "ping"},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},  # notification: no reply
        {"jsonrpc": "2.0", "id": 2, "result": {}},                  # client response: no reply
        {"jsonrpc": "2.0", "id": True, "method": "ping"},           # request, id echoed
        {"jsonrpc": "2.0", "id": [1], "method": "x/y"},             # request, id echoed
        {"jsonrpc": "2.0", "method": "ping"},                       # request without an id:
        {"jsonrpc": "2.0", "id": None, "method": "ping"},           # run, reply dropped (as before)
        {"jsonrpc": "2.0", "id": float("nan"), "method": "ping"},   # the one refused id
        "not an object",
        {"jsonrpc": "2.0", "id": 8, "method": 12},                  # invalid, valid id echoed
        {"jsonrpc": "2.0", "id": 3, "method": "ping"},
        {"jsonrpc": "2.0", "id": 4, "method": "server/discover"},
    ]).encode())  # json.dumps writes the NaN literal; httpx's json= refuses to
    assert r.status_code == 200
    invalid = {"code": -32600, "message": "Invalid request"}
    assert r.json() == [
        {"jsonrpc": "2.0", "id": 1, "result": {}},
        {"jsonrpc": "2.0", "id": True, "result": {}},
        {"jsonrpc": "2.0", "id": [1], "error": {"code": -32601, "message": "Method not found: x/y"}},
        {"jsonrpc": "2.0", "error": invalid},
        {"jsonrpc": "2.0", "error": invalid},
        {"jsonrpc": "2.0", "id": 8, "error": invalid},
        {"jsonrpc": "2.0", "id": 3, "result": {}},
        {"jsonrpc": "2.0", "id": 4,
         "error": {"code": -32601, "message": "Method not found: server/discover"}},
    ]


async def test_batch_tool_call_without_an_id_is_executed_and_its_reply_dropped(surface):
    r = await surface.post([_tool_call(surface), {"jsonrpc": "2.0", "id": 5, "method": "ping"}])
    assert r.json() == [{"jsonrpc": "2.0", "id": 5, "result": {}}]
    assert surface.executed == 1


async def test_batch_with_no_replies_is_202(surface):
    r = await surface.post([
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 4, "method": "notifications/cancelled"},
        {"jsonrpc": "2.0", "id": 1, "result": {}},
        {"jsonrpc": "2.0", "method": "ping"},  # a request without an id: run, no reply
    ])
    assert r.status_code == 202 and r.content == b""


async def test_empty_batch_is_minus_32600_without_an_id_member(surface):
    r = await surface.post([])
    assert r.status_code == 200
    body = r.json()
    assert body["error"]["code"] == -32600
    assert "id" not in body


async def test_batch_executes_a_tool_call_that_has_an_id(surface):
    r = await surface.post([_tool_call(surface, 21), {"jsonrpc": "2.0", "id": 22, "method": "ping"}])
    assert [item["id"] for item in r.json()] == [21, 22]
    assert surface.executed == 1


# ---------------------------------------- B3: parse errors, never an HTTP 500


def _deeply_nested_object() -> bytes:
    return (b'{"jsonrpc":"2.0","id":1,"method":"ping","params":' + b"[" * DEEP + b"]" * DEEP + b"}")


@pytest.mark.parametrize("raw", [
    b"", b"{", b"not json", b'{"jsonrpc":"2.0",', b"\xff\xfe\xfd", b'{"a":"\xc3\x28"}',
    b"\x80\x81", b"[" * DEEP, _deeply_nested_object(),
], ids=["empty", "open-brace", "text", "truncated", "invalid-utf8-bom", "invalid-utf8-string",
        "invalid-utf8-high", "deep-array", "deep-params"])
async def test_unparseable_bodies_are_minus_32700_without_an_id_member(surface, raw):
    r = await surface.post(raw)
    assert r.status_code == 200
    assert r.json() == {"jsonrpc": "2.0", "error": {"code": -32700, "message": "Parse error"}}


async def test_unparseable_bodies_still_get_an_exchange_line(surface, caplog):
    caplog.set_level(logging.INFO, logger="cognita.gateway")
    r = await surface.post(b"\xff\xfe\xfd")
    assert r.status_code == 200
    exchange = [m for m in caplog.messages if m.startswith("mcp exchange ")]
    assert len(exchange) == 1 and "methods=unparsed" in exchange[0] and "-> http=200" in exchange[0]
    assert any(m.startswith("mcp parse error ") and "cause=invalid_utf8" in m for m in caplog.messages)
    caplog.clear()
    await surface.post(b"[" * DEEP)
    assert any(m.startswith("mcp parse error ") and "cause=nested_too_deeply" in m for m in caplog.messages)


def _nested_bodies(depth: int, name: str) -> list[bytes]:
    nested = b"[" * depth + b"]" * depth
    args = b'{"project":"RW","x":' + nested + b"}"
    call = (b'{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"%s","arguments":%s}}'
            % (name.encode(), args))
    return [
        b'{"jsonrpc":"2.0","id":' + nested + b',"method":"ping"}',
        b'{"jsonrpc":"2.0","id":1,"method":' + nested + b"}",
        b'{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":' + nested + b"}}",
        call,
        b"[" + call + b"]",
    ]


@pytest.mark.parametrize("depth", [200, 600, 990, 5000])
async def test_a_parseable_but_over_deep_body_is_a_parse_error_not_a_500(surface, depth):
    # Measured 2026-10-10: nested `arguments` that json.loads accepted (600 and
    # more levels) raised RecursionError in the gateway's recursive project
    # check, an HTTP 500. Past MAX_NESTING_DEPTH the body is a parse error.
    for raw in _nested_bodies(depth, surface.call_params["name"]):
        r = await surface.post(raw)
        assert r.status_code == 200, raw[:60]
        assert r.json() == {"jsonrpc": "2.0", "error": {"code": -32700, "message": "Parse error"}}
    assert surface.executed == 0


async def test_nesting_below_the_limit_is_still_handled_normally(surface):
    from cognita.mcp_protocol import MAX_NESTING_DEPTH

    depth = MAX_NESTING_DEPTH - 3  # leaves room for the envelope's own levels
    ping_with_params = b'{"jsonrpc":"2.0","id":1,"method":"ping","params":' + (
        b"[" * depth + b"]" * depth) + b"}"
    r = await surface.post(ping_with_params)
    assert r.status_code == 200 and r.json() == {"jsonrpc": "2.0", "id": 1, "result": {}}
    # Exactly at / one past the limit: the envelope object is level 1, so a
    # params array nested MAX-1 deep makes the body MAX levels deep.
    at_limit = b'{"jsonrpc":"2.0","id":1,"method":"ping","params":' + (
        b"[" * (MAX_NESTING_DEPTH - 1) + b"]" * (MAX_NESTING_DEPTH - 1)) + b"}"
    over = b'{"jsonrpc":"2.0","id":1,"method":"ping","params":' + (
        b"[" * MAX_NESTING_DEPTH + b"]" * MAX_NESTING_DEPTH) + b"}"
    assert (await surface.post(at_limit)).json()["result"] == {}
    assert (await surface.post(over)).json()["error"]["code"] == -32700


# ------------------------------------------------------ B4: 401 challenges


async def test_a_rejected_bearer_names_invalid_token_and_no_credential_does_not(surface):
    missing = await surface.post(PING, token=None)
    bad = await surface.post(PING, token="not-the-key")
    assert missing.status_code == bad.status_code == 401
    assert 'error="invalid_token"' not in missing.headers["www-authenticate"]
    assert bad.headers["www-authenticate"] == (
        missing.headers["www-authenticate"] + ', error="invalid_token"')
    assert bad.text == "Invalid or expired credential"  # the body never changed


async def test_a_non_bearer_authorization_header_keeps_the_plain_challenge(surface):
    plain = await surface.post(PING, token=None)
    basic = await surface.post(PING, token=None, headers={"Authorization": "Basic Zm9vOmJhcg=="})
    assert basic.status_code == 401
    assert basic.headers["www-authenticate"] == plain.headers["www-authenticate"]


async def test_oauth_challenge_keeps_resource_metadata_and_scope_then_adds_the_error(tmp_path):
    from test_oauth_parent_integration import FakeOAuthClient

    docs = tmp_path / "docs"
    docs.mkdir()
    registry = Registry(tmp_path / "registry.yaml")
    registry.add(Project(name="RW", documents_dir=docs, data_dir=tmp_path / "data"))
    auth = AuthenticationPolicyStore(tmp_path / "authentication.yaml", project_names=["RW"])
    auth.mutate_global(expected_revision=0, oauth_enabled=True)
    store = ConnectorStore(tmp_path / "connectors.yaml")
    slug = store.create(expected_revision=0, name="OAuth", project_names=["RW"]).connectors[0].slug
    config = CognitaConfig(registry_path=registry.path, connectors_path=store.path,
                           data_root=tmp_path, public_base_url="https://cognita.example",
                           oauth_enabled=True)
    oauth = FakeOAuthClient("https://cognita.example/unrelated")
    oauth.introspection = IntrospectionResult(False)
    app = create_gateway_app(config, registry, engine=FakeEngineHost(FastAPI()),
                             connector_store=store, authentication_store=auth, oauth_client=oauth)
    url = f"/mcp/connectors/{slug}/mcp/v{PUBLIC_CONTRACT_VERSION}"
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://cognita.example") as client:
        missing = await client.post(url, json=PING)
        bad = await client.post(url, json=PING, headers={"Authorization": "Bearer expired-token"})
    assert missing.status_code == bad.status_code == 401
    plain = missing.headers["www-authenticate"]
    assert plain.startswith('Bearer resource_metadata="https://cognita.example/') and 'scope="' in plain
    assert 'error="invalid_token"' not in plain
    assert bad.headers["www-authenticate"] == plain + ', error="invalid_token"'


# --------------------------------------------- B5: Workspace-only exchange line


async def test_workspace_route_logs_one_exchange_line_like_the_connector_route(tmp_path, caplog):
    made = _workspace_surface(tmp_path)
    caplog.set_level(logging.INFO, logger="cognita.gateway")
    r = await made.post(
        {"jsonrpc": "2.0", "id": "ws-1", "method": "ping"},
        headers={"Accept": "application/json", "MCP-Protocol-Version": "2025-06-18",
                 "User-Agent": "WorkspaceClient/1.2"})
    assert r.status_code == 200
    [exchange] = [m for m in caplog.messages if m.startswith("mcp exchange ")]
    assert "connector=workspace route=v3 http_method=POST methods=ping ids=str:ws-1 batch=no" in exchange
    assert "accept=application/json content_type=application/json protocol_version=2025-06-18 " in exchange
    assert "session_id=absent user_agent=WorkspaceClient/1.2" in exchange
    assert f"-> http=200 media_type=application/json response_bytes={len(r.content)}" in exchange
    assert made.token not in caplog.text


async def test_workspace_exchange_line_for_stable_route_rejections_and_batches(tmp_path, caplog):
    made = _workspace_surface(tmp_path)
    caplog.set_level(logging.INFO, logger="cognita.gateway")
    made.url = "/mcp/workspace/workspace/mcp"
    r = await made.post([PING, {"jsonrpc": "2.0", "method": "notifications/initialized"}])
    assert r.status_code == 200
    [exchange] = [m for m in caplog.messages if m.startswith("mcp exchange ")]
    assert "route=stable" in exchange and "methods=ping,notifications/initialized" in exchange
    assert "ids=int:7,null batch=2" in exchange
    caplog.clear()
    rejected = await made.post(PING, token="not-the-key")
    assert rejected.status_code == 401
    [exchange] = [m for m in caplog.messages if m.startswith("mcp exchange ")]
    assert "methods=- ids=- batch=- request_bytes=unknown" in exchange and "-> http=401" in exchange
    assert "not-the-key" not in caplog.text and made.token not in caplog.text


async def test_workspace_exchange_line_never_carries_arguments(tmp_path, caplog):
    made = _workspace_surface(tmp_path)
    caplog.set_level(logging.INFO, logger="cognita.gateway")
    r = await made.post({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {
        "name": "workspace_info", "arguments": {"secret_marker": "TOPSECRET-VALUE"}}})
    assert r.status_code == 200
    assert "TOPSECRET-VALUE" not in caplog.text


# ---------------------------------------------- the shared routines, directly


def test_classify_message_table():
    def kind(message):
        return classify_message(message)

    # Any non-notifications/ string method is a request, whatever its id.
    assert kind({"method": "x"}).kind == REQUEST
    assert kind({"method": "x", "id": 1}).kind == REQUEST
    assert kind({"method": "x", "id": "a"}).reply_id == "a"
    assert kind({"method": "x"}).reply_id is None  # echoed as "id": null, as before
    for odd_id in (None, True, False, {}, [], [1], {"a": 1}, 0, 1.5, ""):
        verdict = kind({"method": "x", "id": odd_id})
        assert verdict.kind == REQUEST and verdict.reply_id == odd_id and verdict.reply_id is not NO_ID
    # The ONLY refused id is a non-finite float.
    for bad_id in (float("nan"), float("inf"), float("-inf")):
        verdict = kind({"method": "x", "id": bad_id})
        assert verdict.kind == INVALID and verdict.reply_id is NO_ID and verdict.reason == "non_finite_id"
    # notifications/* is a notification whatever the id.
    for any_id in (None, True, float("nan"), {}, [], 4, "a"):
        assert kind({"method": "notifications/initialized", "id": any_id}).kind == NOTIFICATION
    assert kind({"method": "notifications/initialized"}).kind == NOTIFICATION
    assert kind({"result": {}}).kind == RESPONSE
    assert kind({"error": {}}).kind == RESPONSE
    assert kind({"method": "x", "result": {}}).kind == REQUEST
    assert kind({"method": "x", "id": 1, "error": {}}).kind == REQUEST
    assert kind({"method": "notifications/x", "result": {}}).kind == NOTIFICATION
    assert kind({"id": 4}).kind == INVALID and kind({"id": 4}).reply_id == 4
    for not_object in (5, "x", None, [], True):
        assert kind(not_object).kind == INVALID and kind(not_object).reply_id is NO_ID
    assert kind({"method": 5, "id": 2}).reply_id == 2
    assert kind({"method": 5}).reply_id is NO_ID


def test_error_body_omits_only_a_no_id_reply_and_keeps_key_order():
    assert json.dumps(error_body(NO_ID, -32700, "Parse error")) == (
        '{"jsonrpc": "2.0", "error": {"code": -32700, "message": "Parse error"}}')
    assert json.dumps(error_body(3, -32601, "m")) == (
        '{"jsonrpc": "2.0", "id": 3, "error": {"code": -32601, "message": "m"}}')
    # A request that sent no id (or null) has always been answered "id": null.
    assert json.dumps(error_body(None, -32601, "m")) == (
        '{"jsonrpc": "2.0", "id": null, "error": {"code": -32601, "message": "m"}}')


# ---------------------------------------------------- proxy batch (inner copy)


async def _proxy_batch(messages):
    response = await _handle_batch(
        None, None, "http://worker.invalid/mcp", messages, readonly=True,
        documents_dir=None, backup_keep=0, project_name="P",
    )
    return response.status_code, (json.loads(response.body) if response.body else None)


async def test_proxy_batch_follows_the_same_rules():
    invalid = {"code": -32600, "message": "Invalid request"}
    assert await _proxy_batch([]) == (200, {"jsonrpc": "2.0", "error": {
        "code": -32600, "message": "Invalid request: empty batch"}})
    assert await _proxy_batch(["x", 5, {"method": 3, "id": 6}, {"id": float("nan"), "method": "ping"}]) == (200, [
        {"jsonrpc": "2.0", "error": invalid},
        {"jsonrpc": "2.0", "error": invalid},
        {"jsonrpc": "2.0", "id": 6, "error": invalid},
        {"jsonrpc": "2.0", "error": invalid},
    ])
    # notifications/* (whatever the id) and client responses add no reply, so
    # the batch is 202 with no body. (Request members need a worker; the
    # gateway tests above cover them.)
    assert await _proxy_batch([
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 1, "result": {}},
        {"jsonrpc": "2.0", "id": 2, "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": None, "method": "notifications/cancelled"},
    ]) == (202, None)


# ------------------------------------------------------------ the local engine


@pytest.fixture
def engine_host(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    registry = Registry(tmp_path / "registry.yaml")
    registry.add(Project(name="PROTO", documents_dir=docs, data_dir=tmp_path / "data",
                         token_sha256=hash_token(generate_token())))
    store = Store("postgresql://nowhere/none", embedding_dimensions=32)
    core = RetrievalCore(store, HashEmbedder(32), OverlapReranker())
    return LocalEngineHost(CognitaConfig(), registry, core)


async def _engine_post(host, body):
    async with AsyncClient(transport=ASGITransport(app=host.app), base_url="http://t") as client:
        if isinstance(body, (bytes, bytearray)):
            return await client.post("/engine/PROTO/mcp", content=bytes(body))
        return await client.post("/engine/PROTO/mcp", json=body)


@pytest.mark.parametrize("requested,expected", [
    ("2025-11-25", "2025-11-25"), ("2025-06-18", "2025-06-18"), ("2025-03-26", "2025-03-26"),
    ("2026-07-28", "2025-11-25"), ("banana", "2025-11-25"), (7, "2025-11-25"),
])
async def test_engine_negotiates_the_protocol_version(engine_host, requested, expected):
    r = await _engine_post(engine_host, {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                         "params": {"protocolVersion": requested}})
    assert r.json()["result"]["protocolVersion"] == expected
    r = await _engine_post(engine_host, {"jsonrpc": "2.0", "id": 1, "method": "initialize"})
    assert r.json()["result"]["protocolVersion"] == "2025-03-26"


async def test_engine_classifies_and_parses_like_the_gateway(engine_host):
    invalid = {"jsonrpc": "2.0", "error": {"code": -32600, "message": "Invalid request"}}
    # Lenient on purpose (16.1.3): a request without an id, or with an odd one,
    # is answered exactly as before, "id": null / the id echoed.
    for raw, echoed in ((b'{"jsonrpc":"2.0","method":"ping"}', None),
                        (b'{"jsonrpc":"2.0","id":null,"method":"ping"}', None),
                        (b'{"method":"ping","id":true}', True), (b'{"method":"ping","id":[1]}', [1])):
        r = await _engine_post(engine_host, raw)
        assert (r.status_code, r.json()) == (200, {"jsonrpc": "2.0", "id": echoed, "result": {}}), raw
    assert (await _engine_post(engine_host, {"jsonrpc": "2.0", "id": 1, "result": {}})).status_code == 202
    for notice in (b'{"method":"notifications/x"}', b'{"method":"notifications/x","id":null}',
                   b'{"method":"notifications/x","id":NaN}'):
        assert (await _engine_post(engine_host, notice)).status_code == 202
    assert (await _engine_post(engine_host, {"jsonrpc": "2.0", "id": 1, "method": "notifications/x"})
            ).status_code == 202
    ok = await _engine_post(engine_host, {"jsonrpc": "2.0", "id": "e1", "method": "ping"})
    assert ok.json() == {"jsonrpc": "2.0", "id": "e1", "result": {}}
    missing = await _engine_post(engine_host, {"jsonrpc": "2.0", "id": 5, "method": "no/such"})
    assert missing.json() == {"jsonrpc": "2.0", "id": 5,
                              "error": {"code": -32601, "message": "Method not found: no/such"}}
    for raw in (b'{"method":"ping","id":NaN}', b'{"method":"ping","id":-Infinity}',
                b"[1]", b"5", b"{}"):
        r = await _engine_post(engine_host, raw)
        assert (r.status_code, r.json()) == (200, invalid), raw
    parse_error = {"jsonrpc": "2.0", "error": {"code": -32700, "message": "Parse error"}}
    for raw in (b"{", b"\xff\xfe\xfd", b"[" * DEEP, _deeply_nested_object()):
        r = await _engine_post(engine_host, raw)
        assert (r.status_code, r.json()) == (200, parse_error), raw[:20]


def test_modules_agree_on_the_supported_versions_literal():
    # One authority: nothing else in the package spells out the revisions.
    import pathlib
    import re

    root = pathlib.Path(__file__).resolve().parent.parent / "src" / "cognita"
    offenders = [
        path.name for path in root.rglob("*.py")
        if path.name != "mcp_protocol.py"
        and re.search(r'"2025-(11-25|06-18)"', path.read_text(encoding="utf-8"))
        and "protocolVersion" in path.read_text(encoding="utf-8")
    ]
    assert offenders == [], offenders
