"""6.1.0 — the GATEWAY half of `write_documents`.

🔴 **A mutating tool the gateway does not recognize degrades to unprotected
rather than refused, and that is silent.** `_handle_engine_write` is built
around a single `arguments.filepath`: hand it a batch and it takes the "no
filepath to lock/back up" branch, forwards the call, and the write succeeds
with **no backups and no edit lock**. Nothing errors. The only symptom is that
the undo points do not exist and the lost-update race is reopened — both
invisible until someone needs them.

So `write_documents` gets its own gateway path, and these tests pin the two
promises it has to keep for EVERY document in the call:

- every existing target is backed up before anything is written, and a failed
  backup aborts the whole call (`backups.py`'s standing invariant, applied to a
  set — backing up three of four and writing all four leaves one document with
  no undo point and the caller no way to know which);
- every target is locked, in a canonical order, so two concurrent batches
  sharing files cannot deadlock each other and hang the project's write lock.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from httpx import ASGITransport, AsyncClient

from cognita import proxy as proxy_mod
from cognita.connectors import ConnectorStore
from cognita.auth_policy import AuthenticationPolicyStore
from cognita.config import CognitaConfig
from cognita.byte_facts import byte_facts
from cognita.gateway import create_gateway_app
from cognita.registry import Project, Registry
from engine_fakes import FakeEngineHost


def make_worker(seen: list) -> FastAPI:
    app = FastAPI()

    @app.post("/mcp")
    async def mcp(request: Request):
        msg = await request.json()
        seen.append(msg)
        if msg.get("method") == "tools/list":
            return JSONResponse({"jsonrpc": "2.0", "id": msg["id"],
                                 "result": {"tools": []}})
        args = ((msg.get("params") or {}).get("arguments") or {})
        docs = args.get("documents") or []
        receipts = [
            {"filepath": document.get("filepath"),
             **byte_facts(document.get("content", "").encode("utf-8"))}
            for document in docs
        ]
        payload = {"status": "success", "documents_written": len(docs),
                   "chunks_indexed": 3 * len(docs), "receipts": receipts,
                   "filepaths": [d.get("filepath") for d in docs]}
        return JSONResponse({"jsonrpc": "2.0", "id": msg["id"], "result": {
            "content": [{"type": "text", "text": json.dumps(payload)}],
            "isError": False}})

    return app


@pytest.fixture
def env(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "one.md").write_bytes(b"# One\n\noriginal one\n")
    (docs / "two.md").write_bytes(b"# Two\n\noriginal two\n")

    registry = Registry(tmp_path / "registry.yaml")
    registry.add(Project(name="RW", documents_dir=docs, data_dir=tmp_path / "d1"))
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
    connector_store.create(
        expected_revision=0,
        name="Test connector",
        project_names=["RW"],
    )

    seen: list = []
    worker = make_worker(seen)
    config = CognitaConfig(
        registry_path=tmp_path / "registry.yaml",
        connectors_path=tmp_path / "connectors.yaml",
        data_root=tmp_path,
    )
    app = create_gateway_app(
        config, registry, engine=FakeEngineHost(worker), connector_store=connector_store,
        authentication_store=auth,
    )
    connector = connector_store.snapshot().connectors[0]
    app.state.test_connector_id = connector.id
    app.state.test_connector_slug = connector.slug
    return app, token, docs, seen


async def call_batch(app, token, documents):
    payload = {"jsonrpc": "2.0", "id": 7, "method": "tools/call",
               "params": {"name": "write_documents",
                          "arguments": {"project": "RW", "documents": documents}}}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.post(f"/mcp/connectors/{app.state.test_connector_slug}/mcp/v5", json=payload,
                         headers={"Authorization": f"Bearer {token}"})
    body = r.json()
    return json.loads(body["result"]["content"][0]["text"])


async def call_tool(app, token, name, arguments):
    payload = {"jsonrpc": "2.0", "id": 8, "method": "tools/call",
               "params": {"name": name,
                          "arguments": {"project": "RW", **arguments}}}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        response = await c.post(
            f"/mcp/connectors/{app.state.test_connector_slug}/mcp/v5", json=payload,
            headers={"Authorization": f"Bearer {token}"},
        )
    return json.loads(response.json()["result"]["content"][0]["text"])


async def test_every_document_in_a_batch_is_backed_up(env):
    """🔴 The invariant that would have been lost silently. Both existing files
    must have a snapshot before the worker is ever called."""
    app, token, docs, _seen = env
    out = await call_batch(app, token, [
        {"filepath": "one.md", "content": "# One\n\nnew one\n"},
        {"filepath": "two.md", "content": "# Two\n\nnew two\n"},
    ])

    assert out["status"] == "success"
    ids = out.get("previous_backup_ids") or {}
    assert set(ids) == {"one.md", "two.md"}, (
        "a batch write did not back up every document — the undo point for at "
        "least one file does not exist"
    )
    backups = list((docs / "backups").rglob("*.md"))
    assert len(backups) == 2


async def test_a_new_file_in_the_batch_needs_no_backup(env):
    """`backup_if_exists` is exactly that. A document being CREATED has no
    previous content, so it contributes no id — and must not fail the call."""
    app, token, _docs, _seen = env
    out = await call_batch(app, token, [
        {"filepath": "one.md", "content": "# One\n\nnew one\n"},
        {"filepath": "brand_new.md", "content": "# New\n\nfresh\n"},
    ])
    assert out["status"] == "success"
    assert set(out.get("previous_backup_ids") or {}) == {"one.md"}


async def test_a_failed_backup_aborts_the_whole_batch(env, monkeypatch):
    """🔴 All-or-nothing reaches the backup step too. Backing up three of four
    and writing all four leaves one document with no undo point, and the caller
    with no way to know which one."""
    app, token, _docs, seen = env
    calls = {"n": 0}
    real = proxy_mod.backup_if_exists

    def flaky(documents_dir, filepath, keep=None):
        calls["n"] += 1
        if calls["n"] == 2:
            raise proxy_mod.BackupError("disk full")
        return real(documents_dir, filepath, keep=keep)

    monkeypatch.setattr(proxy_mod, "backup_if_exists", flaky)

    out = await call_batch(app, token, [
        {"filepath": "one.md", "content": "# One\n\nnew one\n"},
        {"filepath": "two.md", "content": "# Two\n\nnew two\n"},
    ])

    assert out["status"] == "error"
    assert out["reason"] == "backup_failed"
    assert "No changes were made" in out["message"]
    # The decisive assertion: the worker was never asked to write anything.
    assert not [m for m in seen
                if (m.get("params") or {}).get("name") == "write_documents"]


async def test_a_path_escaping_the_project_is_refused_before_any_backup(env):
    app, token, docs, seen = env
    out = await call_batch(app, token, [
        {"filepath": "one.md", "content": "# One\n\nnew one\n"},
        {"filepath": "../escape.md", "content": "nope\n"},
    ])
    assert out["status"] == "error"
    assert out["reason"] == "invalid_path"
    assert "NOTHING was written" in out["message"]
    assert not (docs / "backups").exists(), "backed up a refused batch"
    assert not [m for m in seen
                if (m.get("params") or {}).get("name") == "write_documents"]


async def test_invalid_encoded_content_is_refused_before_any_backup(env):
    app, token, docs, seen = env
    out = await call_batch(app, token, [
        {"filepath": "one.md", "content": "not base64!", "content_encoding": "base64"},
    ])
    assert out["status"] == "error" and out["reason"] == "invalid"
    assert not (docs / "backups").exists()
    assert not [m for m in seen
                if (m.get("params") or {}).get("name") == "write_documents"]


async def test_invalid_single_write_content_is_refused_before_backup(env):
    app, token, docs, seen = env
    out = await call_tool(app, token, "update_document", {
        "filepath": "one.md", "content": "not base64!", "content_encoding": "base64",
    })
    assert out["status"] == "error" and out["reason"] == "invalid"
    assert not (docs / "backups").exists()
    assert not [m for m in seen
                if (m.get("params") or {}).get("name") == "update_document"]


async def test_atomic_base64_total_is_refused_before_backup(env, monkeypatch):
    app, token, docs, seen = env
    monkeypatch.setattr(proxy_mod, "MAX_BASE64_ATOMIC_SET_BYTES", 5)
    out = await call_batch(app, token, [
        {"filepath": "one.md", "content": "YWJj", "content_encoding": "base64"},
        {"filepath": "two.md", "content": "ZGVm", "content_encoding": "base64"},
    ])
    assert out["status"] == "error" and out["reason"] == "too_large"
    assert out["limit_bytes"] == 5
    assert not (docs / "backups").exists()
    assert not [m for m in seen
                if (m.get("params") or {}).get("name") == "write_documents"]


async def test_stale_exact_byte_member_is_refused_before_any_backup(env):
    app, token, docs, seen = env
    out = await call_batch(app, token, [{
        "filepath": "one.md", "content": "# replacement\n",
        "expected_bytes_sha256": "0" * 64,
    }])
    assert out["status"] == "error" and out["reason"] == "stale_file"
    assert not (docs / "backups").exists()
    assert not [m for m in seen
                if (m.get("params") or {}).get("name") == "write_documents"]


async def test_a_duplicate_path_does_not_deadlock_the_gateway(env):
    """🔴 `asyncio.Lock` is NOT reentrant. A batch naming one file twice would
    deadlock against itself and hang holding the project write lock — taking
    every other write to that project down with it, not just this call. The
    engine refuses duplicates, but the gateway locks BEFORE the engine sees the
    call, so it must not depend on that."""
    app, token, _docs, _seen = env
    out = await asyncio.wait_for(call_batch(app, token, [
        {"filepath": "one.md", "content": "# One\n\nfirst\n"},
        {"filepath": "one.md", "content": "# One\n\nsecond\n"},
    ]), timeout=10)
    assert out["status"] in {"success", "error"}  # answered at all is the point


async def test_two_overlapping_batches_do_not_deadlock(env):
    """The reason locks are taken in sorted order. Two batches naming the same
    two files in OPPOSITE orders is the textbook deadlock, and here it would
    hang the whole project rather than just the callers."""
    app, token, _docs, _seen = env
    a = call_batch(app, token, [
        {"filepath": "one.md", "content": "# One\n\nA\n"},
        {"filepath": "two.md", "content": "# Two\n\nA\n"},
    ])
    b = call_batch(app, token, [
        {"filepath": "two.md", "content": "# Two\n\nB\n"},
        {"filepath": "one.md", "content": "# One\n\nB\n"},
    ])
    out_a, out_b = await asyncio.wait_for(asyncio.gather(a, b), timeout=15)
    assert out_a["status"] == "success" and out_b["status"] == "success"
