"""expected_sha256 on update_document and overwriting add_document (5.0 §8).

edit_document and edit_document_batch have had this guard since 2.7.
update_document — the one tool that rewrites an ENTIRE file — had none, and the
push path overwrites through add_document, so until 5.0 every push could clobber
a concurrent change on kei with only a backup to show for it. A backup is
recovery; this is prevention, and the difference is whether anyone notices.

The assertions that matter are the negative ones: on a stale hash the worker
must never be reached AND no backup may be taken, because a rejected write that
still snapshotted would quietly churn the retention window.
"""
from cognita.connectors import PUBLIC_CONTRACT_VERSION

import json

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from httpx import ASGITransport, AsyncClient

import cognita.gateway as gateway_mod
from cognita.backups import BACKUPS_DIRNAME, list_backup_entries
from cognita.connectors import ConnectorStore
from cognita.auth_policy import AuthenticationPolicyStore
from cognita.config import CognitaConfig
from cognita.editing import content_sha256
from cognita.gateway import create_gateway_app
from cognita.registry import Project, Registry
from engine_fakes import FakeEngineHost

DOC_TEXT = "# Note\n\nalpha line\nbeta line\n"


def make_fake_worker(seen: list) -> FastAPI:
    app = FastAPI()

    @app.post("/mcp")
    async def mcp(request: Request):
        msg = await request.json()
        seen.append(msg)
        args = ((msg.get("params") or {}).get("arguments")) or {}
        payload = {"status": "success", "filepath": args.get("filepath", ""),
                   "old_chunks_removed": 1, "new_chunks_added": 1}
        return JSONResponse({"jsonrpc": "2.0", "id": msg["id"], "result": {
            "content": [{"type": "text", "text": json.dumps(payload)}],
            "isError": False}})

    return app


@pytest.fixture
def env(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "note.md").write_bytes(DOC_TEXT.encode("utf-8"))
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
    seen: list = []
    config = CognitaConfig(
        registry_path=tmp_path / "registry.yaml",
        connectors_path=tmp_path / "connectors.yaml",
        data_root=tmp_path,
    )
    app = create_gateway_app(
        config, registry, engine=FakeEngineHost(make_fake_worker(seen)),
        connector_store=connector_store,
        authentication_store=auth,
    )
    connector = connector_store.snapshot().connectors[0]
    app.state.test_connector_id = connector.id
    app.state.test_connector_slug = connector.slug
    app.state.test_connector_store = connector_store
    return app, token, docs, seen


async def post(app, token, payload):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        return await c.post(f"/mcp/connectors/{app.state.test_connector_slug}/mcp/v{PUBLIC_CONTRACT_VERSION}", json=payload,
                            headers={"Authorization": f"Bearer {token}"})


def call(name, arguments, msg_id=7):
    arguments = {"project": "RW", **arguments}
    return {"jsonrpc": "2.0", "id": msg_id, "method": "tools/call",
            "params": {"name": name, "arguments": arguments}}


def payload_of(response):
    return json.loads(response.json()["result"]["content"][0]["text"])


CURRENT = content_sha256(DOC_TEXT)
STALE = "0" * 64


async def test_update_document_with_a_matching_hash_is_forwarded(env):
    app, token, _, seen = env
    r = await post(app, token, call("update_document",
                                    {"filepath": "note.md", "content": "new\n",
                                     "expected_sha256": CURRENT}))
    assert r.json()["result"]["isError"] is False
    assert len(seen) == 1 and seen[0]["params"]["name"] == "update_document"


async def test_update_document_with_a_stale_hash_never_reaches_the_worker(env):
    app, token, docs, seen = env
    r = await post(app, token, call("update_document",
                                    {"filepath": "note.md", "content": "clobbered",
                                     "expected_sha256": STALE}))
    body = payload_of(r)
    assert body["reason"] == "stale_file"
    assert body["actual_sha256"] == CURRENT
    assert r.json()["result"]["isError"] is True
    assert seen == []
    assert (docs / "note.md").read_bytes() == DOC_TEXT.encode("utf-8")


async def test_a_rejected_write_takes_no_backup(env):
    """A refusal that still snapshotted would churn the retention window and
    push real recovery points out of it."""
    app, token, docs, _ = env
    await post(app, token, call("update_document",
                                {"filepath": "note.md", "content": "x",
                                 "expected_sha256": STALE}))
    assert not (docs / BACKUPS_DIRNAME).exists()
    assert list_backup_entries(docs, "note.md") == []


async def test_add_document_overwriting_an_existing_path_is_guarded(env):
    app, token, docs, seen = env
    r = await post(app, token, call("add_document",
                                    {"filepath": "note.md", "content": "clobbered",
                                     "expected_sha256": STALE}))
    assert payload_of(r)["reason"] == "stale_file"
    assert seen == []
    assert (docs / "note.md").read_bytes() == DOC_TEXT.encode("utf-8")


async def test_expected_sha256_on_a_path_with_no_file_is_rejected(env):
    """You named a version to replace; "it is gone" is exactly the concurrent
    change the guard exists to catch, so it is a rejection, not a pass."""
    app, token, _, seen = env
    r = await post(app, token, call("add_document",
                                    {"filepath": "brand-new.md", "content": "x",
                                     "expected_sha256": CURRENT}))
    body = payload_of(r)
    assert body["reason"] == "stale_file" and body["actual_sha256"] is None
    assert seen == []


async def test_a_prefix_hash_is_accepted(env):
    app, token, _, seen = env
    r = await post(app, token, call("update_document",
                                    {"filepath": "note.md", "content": "new\n",
                                     "expected_sha256": CURRENT[:12]}))
    assert r.json()["result"]["isError"] is False
    assert len(seen) == 1


async def test_too_short_a_prefix_is_refused_as_invalid(env):
    app, token, _, seen = env
    r = await post(app, token, call("update_document",
                                    {"filepath": "note.md", "content": "new\n",
                                     "expected_sha256": CURRENT[:6]}))
    assert payload_of(r)["reason"] == "invalid"
    assert seen == []


async def test_no_hash_still_writes_unguarded(env):
    """The guard is opt-in: omitting it must not start refusing writes."""
    app, token, _, seen = env
    r = await post(app, token, call("update_document",
                                    {"filepath": "note.md", "content": "new\n"}))
    assert r.json()["result"]["isError"] is False
    assert len(seen) == 1


async def test_add_document_to_a_new_path_without_a_hash_is_unaffected(env):
    app, token, _, seen = env
    r = await post(app, token, call("add_document",
                                    {"filepath": "fresh.md", "content": "x"}))
    assert r.json()["result"]["isError"] is False
    assert len(seen) == 1


# --------------------------------------------------------------------- 5.0.1
# read_document carries the FILE's byte facts, so a byte-fidelity check needs no
# second call — and states that its own `text` is normalized.

CRLF_TEXT = b"a\r\nb\r\n"


@pytest.fixture
def crlf_env(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "crlf.md").write_bytes(CRLF_TEXT)
    (docs / "lf.md").write_bytes(b"a\nb\n")
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
    config = CognitaConfig(
        registry_path=tmp_path / "registry.yaml",
        connectors_path=tmp_path / "connectors.yaml",
        data_root=tmp_path,
    )
    app = create_gateway_app(
        config, registry, engine=FakeEngineHost(make_fake_worker([])),
        connector_store=connector_store,
        authentication_store=auth,
    )
    connector = connector_store.snapshot().connectors[0]
    app.state.test_connector_id = connector.id
    app.state.test_connector_slug = connector.slug
    return app, token, docs


async def test_read_document_reports_the_files_own_byte_hash(crlf_env):
    """The 2026-08-29 self-test read this as a FAILED byte-verbatim write. The
    write was perfect; the read was normalizing and not saying so."""
    import hashlib

    app, token, _ = crlf_env
    r = await post(app, token, call("read_document", {"filepath": "crlf.md"}))
    payload = json.loads(r.json()["result"]["content"][0]["text"])
    # The file, byte for byte — this is what a byte-fidelity assertion uses.
    assert payload["bytes_sha256"] == hashlib.sha256(CRLF_TEXT).hexdigest()
    assert payload["size_bytes"] == len(CRLF_TEXT)
    # 5.6: ...and its text IS those bytes. Until 5.6.0 the response merely
    # DECLARED the divergence (normalized_line_endings=true) and this test
    # asserted the declaration — which is how a read that altered the document
    # survived five releases with a passing guard sitting on top of it.
    assert payload["line_endings"] == "crlf"
    assert payload["normalized_line_endings"] is False
    assert payload["text"].encode("utf-8") == CRLF_TEXT
    assert hashlib.sha256(payload["text"].encode("utf-8")).hexdigest() == \
        payload["bytes_sha256"]
    # Nothing is folded away any more: the trailing newline AND the CR both
    # survive. content_sha256 is still the LF-folded write-guard stamp, and is
    # the only value here that is deliberately not a description of the bytes.
    assert payload["text"] == "a\r\nb\r\n"
    assert payload["content_sha256"] != payload["bytes_sha256"]
    assert "VERBATIM" in payload["content_note"]


async def test_read_document_on_an_lf_file_says_the_text_is_the_bytes(crlf_env):
    import hashlib

    app, token, _ = crlf_env
    r = await post(app, token, call("read_document", {"filepath": "lf.md"}))
    payload = json.loads(r.json()["result"]["content"][0]["text"])
    assert payload["line_endings"] == "lf"
    assert payload["normalized_line_endings"] is False
    assert payload["bytes_sha256"] == hashlib.sha256(b"a\nb\n").hexdigest()
    assert payload["bytes_sha256"] == payload["content_sha256"]
    assert "VERBATIM" in payload["content_note"]


async def test_a_ranged_read_still_describes_the_whole_file(crlf_env):
    """size_bytes/bytes_sha256 are whole-file facts, like content_sha256 already
    was — otherwise a ranged read would hand back a hash of nothing in
    particular."""
    import hashlib

    app, token, _ = crlf_env
    r = await post(app, token, call("read_document",
                                    {"filepath": "crlf.md", "start_line": 1,
                                     "end_line": 1}))
    payload = json.loads(r.json()["result"]["content"][0]["text"])
    assert payload["text"] == "a"
    assert payload["bytes_sha256"] == hashlib.sha256(CRLF_TEXT).hexdigest()
    assert payload["size_bytes"] == len(CRLF_TEXT)


# -------------------------- 5.2: the stale check runs at the gateway, before the forward
# (14.0.0: the three tests that covered the 3.x rollback engine's smaller
# argument surface — _legacy_engine_guard and _strip_legacy_arguments — were
# deleted with `engine: workers`. This one stays: it pins the stale check itself.)


async def test_a_stale_write_is_refused_before_it_reaches_the_engine(env):
    """The gateway's _stale_check reads expected_sha256 and refuses BEFORE the forward.

    History (5.2, superseded by 14.0.0): this was
    test_the_write_guard_still_fires_before_the_strip. Stripping expected_sha256
    in _intercept for the 3.x engine removed it before _handle_engine_write's own
    _stale_check could read it, silently disabling the write guard. The strip is
    gone with the 3.x engine; the guarantee it once endangered is unchanged.
    """
    app, tok, _docs, seen = env
    r = await post(app, tok, call("update_document", {
        "filepath": "note.md", "content": "x", "expected_sha256": "0" * 64}))
    p = payload_of(r)
    assert p["status"] == "error"
    assert p["reason"] == "stale_file"
    assert seen == [], "a stale write must not reach the engine"


async def test_write_policy_is_rechecked_at_execution_admission(env, monkeypatch):
    app, token, docs, seen = env
    original_proxy = gateway_mod.proxy_mcp

    async def change_policy_before_proxy(*args, **kwargs):
        store = app.state.test_connector_store
        store.update(
            app.state.test_connector_id,
            expected_revision=1,
            project_names=["RW"],
            default_access="read",
        )
        return await original_proxy(*args, **kwargs)

    monkeypatch.setattr(gateway_mod, "proxy_mcp", change_policy_before_proxy)
    response = await post(
        app,
        token,
        call("update_document", {"filepath": "note.md", "content": "blocked"}),
    )

    payload = payload_of(response)
    assert payload["status"] == "error"
    assert payload["reason"] == "read_only"
    assert seen == []
    assert (docs / "note.md").read_text(encoding="utf-8") == DOC_TEXT
    assert not (docs / BACKUPS_DIRNAME).exists()
