"""Strict tool-argument validation (5.0 §2).

The bug: `list_documents(path_prefix=...)` was accepted, the key was dropped,
all 319 documents came back, and the response said status:"success". The damage
is not the wasted call — it is a client that believes it filtered and did not,
and has no way to find out. These tests pin the refusal on BOTH tool layers,
because either one going lenient re-opens the hole for half the surface.
"""
from cognita.connectors import PUBLIC_CONTRACT_VERSION

import json

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from httpx import ASGITransport, AsyncClient

from cognita.auth_policy import AuthenticationPolicyStore
from cognita.config import CognitaConfig
from cognita.connectors import ConnectorStore
from cognita.engine_local import ENGINE_TOOL_DEFS_BY_NAME, LocalEngineHost
from cognita.gateway import create_gateway_app
from cognita.parsing import ExtensionPolicy
from cognita.proxy import GATEWAY_TOOL_DEFS
from cognita.registry import Project, Registry
from cognita.retrieval import RetrievalCore
from cognita.store import Store
from cognita.toolargs import accepted_arguments, reject_unknown_arguments, reject_wrong_types
from cognita.tokens import generate_token, hash_token
from engine_fakes import FakeEngineHost
from retrieval_fakes import HashEmbedder, OverlapReranker

DIMS = 32


# ---------------------------------------------------------------- unit level


def test_known_arguments_pass():
    assert reject_unknown_arguments(
        ENGINE_TOOL_DEFS_BY_NAME["list_documents"],
        {"category": "x", "prefix": "y/", "include_hashes": True},
    ) is None


def test_unknown_argument_is_named_and_alternatives_offered():
    payload = reject_unknown_arguments(
        ENGINE_TOOL_DEFS_BY_NAME["list_documents"], {"path_prefix": "y/"}
    )
    assert payload["status"] == "error"
    assert payload["reason"] == "unknown_argument"
    assert payload["rejected_arguments"] == ["path_prefix"]
    # The whole point of naming the accepted set: the retry is buildable without
    # a second round trip to tools/list.
    assert "prefix" in payload["accepted_arguments"]
    assert "path_prefix" in payload["message"]
    # Must state that nothing ran — otherwise a caller may assume a partial effect.
    assert "NOTHING was executed" in payload["message"]


def test_several_unknown_arguments_are_all_reported():
    payload = reject_unknown_arguments(
        ENGINE_TOOL_DEFS_BY_NAME["get_document"], {"filepath": "a.md", "zzz": 1, "aaa": 2}
    )
    assert payload["rejected_arguments"] == ["aaa", "zzz"]  # sorted, both present


def test_every_tool_on_both_layers_declares_its_arguments():
    """A tool whose schema has no properties would accept anything, silently.

    This is the check that keeps the guard honest as tools are added: the
    refusal is only as good as the schema it consults.
    """
    for name, tool_def in {**ENGINE_TOOL_DEFS_BY_NAME, **GATEWAY_TOOL_DEFS}.items():
        schema = tool_def.get("inputSchema") or {}
        assert isinstance(schema.get("properties"), dict), f"{name} has no properties map"
        for required in schema.get("required") or []:
            assert required in accepted_arguments(tool_def), (
                f"{name} requires {required!r} but does not declare it"
            )


# --------------------------------------------------------------- engine layer


def make_host(tmp_path, docs_dir, name) -> LocalEngineHost:
    registry = Registry(tmp_path / "registry.yaml")
    registry.add(Project(name=name, documents_dir=docs_dir, data_dir=tmp_path / "data",
                         token_sha256=hash_token(generate_token())))
    store = Store("postgresql://nowhere/none", embedding_dimensions=DIMS)
    core = RetrievalCore(store, HashEmbedder(DIMS), OverlapReranker())
    core.set_policy(name, ExtensionPolicy.build([".md"], [".txt"]))
    return LocalEngineHost(CognitaConfig(), registry, core)


async def test_engine_refuses_before_the_handler_runs(tmp_path):
    """The refusal must happen without touching Postgres.

    Proof the check is in front of the handler and not inside it: this host's
    store points at a DSN that does not resolve, so any call that reached
    _list_documents would raise instead of returning a clean payload.
    """
    docs = tmp_path / "docs"
    docs.mkdir()
    host = make_host(tmp_path, docs, "T1")
    project = host.registry.get("T1")
    payload = await host.call_tool(project, "list_documents", {"path_prefix": "x/"})
    assert payload["reason"] == "unknown_argument"
    assert payload["tool"] == "list_documents"


async def test_engine_over_the_wire_sets_is_error(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    host = make_host(tmp_path, docs, "T2")
    transport = ASGITransport(app=host.app)
    async with AsyncClient(transport=transport, base_url="http://t") as client:
        r = await client.post("/engine/T2/mcp", json={
            "jsonrpc": "2.0", "id": 3, "method": "tools/call",
            "params": {"name": "list_documents", "arguments": {"path_prefix": "x/"}},
        })
    body = r.json()
    assert r.status_code == 200
    assert body["result"]["isError"] is True  # 5.0 §11.1
    assert json.loads(body["result"]["content"][0]["text"])["reason"] == "unknown_argument"


# -------------------------------------------------------------- gateway layer


@pytest.fixture
def gw(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "note.md").write_text("# Note\n\nalpha\n", encoding="utf-8")
    registry = Registry(tmp_path / "registry.yaml")
    registry.add(Project(name="RW", documents_dir=docs, data_dir=tmp_path / "d"))
    # 13.0 §7.3: the `config.test_mode` registry-token fallback this fixture
    # authenticated through is deleted; it now uses the production path, a
    # global static key from the parent-owned policy store. OAuth is off so an
    # unknown bearer stays a 401 rather than a 503 from the absent OAuth child.
    auth = AuthenticationPolicyStore(
        tmp_path / "authentication.yaml", project_names=["RW"]
    )
    token = auth.mutate_global(
        expected_revision=0, oauth_enabled=False, static_key_action="generate"
    )["generated_key"]

    connector_store = ConnectorStore(tmp_path / "connectors.yaml")
    connector_store.create(expected_revision=0, name="Test connector", project_names=["RW"])

    app = FastAPI()

    @app.post("/mcp")
    async def mcp(request: Request):  # never reached by these tests
        msg = await request.json()
        return JSONResponse({"jsonrpc": "2.0", "id": msg.get("id"), "result": {
            "content": [{"type": "text", "text": json.dumps({"status": "success"})}],
            "isError": False}})

    config = CognitaConfig(
        registry_path=tmp_path / "registry.yaml",
        connectors_path=tmp_path / "connectors.yaml",
        data_root=tmp_path,
    )
    gateway = create_gateway_app(
        config, registry, engine=FakeEngineHost(app), connector_store=connector_store,
        authentication_store=auth,
    )
    connector = connector_store.snapshot().connectors[0]
    gateway.state.test_connector_id = connector.id
    gateway.state.test_connector_slug = connector.slug
    return gateway, token, docs


async def post(app, token, payload):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        return await c.post(f"/mcp/connectors/{app.state.test_connector_slug}/mcp/v{PUBLIC_CONTRACT_VERSION}", json=payload,
                            headers={"Authorization": f"Bearer {token}"})


async def test_gateway_tool_refuses_unknown_argument(gw):
    app, token, _ = gw
    r = await post(app, token, {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                                "params": {"name": "read_document",
                                           "arguments": {"project": "RW", "filepath": "note.md",
                                                         "lines": "1-2"}}})
    body = r.json()
    assert body["result"]["isError"] is True
    payload = json.loads(body["result"]["content"][0]["text"])
    assert payload["reason"] == "unknown_argument"
    assert payload["rejected_arguments"] == ["lines"]
    assert "start_line" in payload["accepted_arguments"]


async def test_gateway_known_arguments_still_work(gw):
    app, token, _ = gw
    r = await post(app, token, {"jsonrpc": "2.0", "id": 5, "method": "tools/call",
                                "params": {"name": "read_document",
                                           "arguments": {"project": "RW", "filepath": "note.md",
                                                         "start_line": 1, "end_line": 2}}})
    payload = json.loads(r.json()["result"]["content"][0]["text"])
    assert payload["status"] == "success"
    assert r.json()["result"]["isError"] is False


# ------------------------------------------------ 5.1: the declared TYPE is enforced


def test_wrong_type_is_a_named_refusal_not_an_internal_error():
    """Every argument declares a `type` and nothing enforced it.

    Handlers do int(args.get("max_results") or 5), so max_results="abc" raised
    ValueError out of the dispatcher and came back as reason "internal_error"
    with the raw exception string — the opposite of the contract 5.0 §2
    established for argument NAMES, which is that a refusal says what it
    rejected and why.
    """
    tool_def = ENGINE_TOOL_DEFS_BY_NAME["search_knowledge"]
    out = reject_wrong_types(tool_def, {"query": "x", "max_results": "abc"})
    assert out is not None
    assert out["reason"] == "invalid"
    assert out["invalid_arguments"] == ["max_results"]
    assert "expects integer, got str" in out["message"]
    assert "NOTHING was executed" in out["message"]


def test_correct_types_pass():
    tool_def = ENGINE_TOOL_DEFS_BY_NAME["search_knowledge"]
    assert reject_wrong_types(tool_def, {"query": "x", "max_results": 5}) is None
    assert reject_wrong_types(tool_def, {"query": "x", "hybrid_alpha": 0.3}) is None
    # an int is a valid JSON `number`
    assert reject_wrong_types(tool_def, {"query": "x", "hybrid_alpha": 1}) is None
    assert reject_wrong_types(tool_def, {"query": "x", "snippet_mode": True}) is None


def test_a_bool_is_not_an_integer():
    """True is an int in Python; a client sending true for max_results means
    something else entirely, and silently taking it as 1 is the class of wrong
    answer this module exists to stop."""
    tool_def = ENGINE_TOOL_DEFS_BY_NAME["search_knowledge"]
    out = reject_wrong_types(tool_def, {"query": "x", "max_results": True})
    assert out is not None
    assert out["invalid_arguments"] == ["max_results"]


def test_null_and_undeclared_keys_are_left_to_the_other_checks():
    """None is "omitted" over the wire, and an unknown NAME is
    reject_unknown_arguments' job — this check must not double-report it."""
    tool_def = ENGINE_TOOL_DEFS_BY_NAME["search_knowledge"]
    assert reject_wrong_types(tool_def, {"query": "x", "category": None}) is None
    assert reject_wrong_types(tool_def, {"query": "x", "nonsense": 1}) is None
