"""Per-worker write serialization (3.0 index-churn fix).

The engine's single-doc write paths (add/update/remove_document) take no lock, so
concurrent writes drive concurrent SQLite writes -> corruption. The gateway now
holds a per-project write lock for the full duration of every mutating call, so
no two writes to one worker overlap — even to *different* files (which the older
per-file _edit_lock did not serialize).

A fake ASGI worker tracks how many requests are in-flight at once; with the lock
the max is 1.

14.0: the fake used to hold each write open with a real 50 ms sleep so overlap
was "observable" — a bet on the clock, and the read-overlap test could lose it on
a loaded box (a search that finished before the write arrived saw max == 1). The
fake now holds a write open until the test releases it, and a probe lock
announces the moment a second writer is queued on the gateway's lock. Every wait
is on a signal a correct run is guaranteed to send; the timeout is a hang guard.
"""
from cognita.connectors import PUBLIC_CONTRACT_VERSION

import asyncio
import json

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from httpx import ASGITransport, AsyncClient

from cognita.connectors import ConnectorStore
from cognita.auth_policy import AuthenticationPolicyStore
from cognita.config import CognitaConfig
from cognita.gateway import create_gateway_app
from cognita.registry import Project, Registry
from engine_fakes import FakeEngineHost

ENGINE_TOOLS = [
    {"name": n, "description": n, "inputSchema": {"type": "object"}}
    for n in ["search_knowledge", "add_document", "update_document",
              "remove_document", "reindex_documents"]
]


HANG_GUARD_S = 5  # only for signals a correct run always sends
READ_TOOLS = {"search_knowledge"}


class Tracker:
    """Counts concurrent in-flight requests at the fake worker.

    A write stays in flight until `release` is set, so the test decides how long
    the overlap window is instead of the clock. `write_entered` fires when the
    first write is inside the worker.
    """

    def __init__(self):
        self.cur = 0
        self.max = 0
        self.release = asyncio.Event()
        self.write_entered = asyncio.Event()


class ProbeLock(asyncio.Lock):
    """The gateway's per-project write lock, reporting when a second caller
    has to wait for it — proof that the lock, not the fake, held it back."""

    def __init__(self):
        super().__init__()
        self.contended = asyncio.Event()

    async def acquire(self):
        if self.locked():
            self.contended.set()
        return await super().acquire()


@pytest.fixture
def probe_lock(monkeypatch):
    import cognita.proxy as p

    lock = ProbeLock()
    monkeypatch.setattr(p, "_worker_write_lock", lambda key: lock)
    return lock


async def _signal(event: asyncio.Event) -> None:
    await asyncio.wait_for(event.wait(), timeout=HANG_GUARD_S)


def make_fake_worker(tracker: Tracker) -> FastAPI:
    app = FastAPI()

    @app.post("/mcp")
    async def mcp(request: Request):
        msg = await request.json()
        if msg.get("method") == "tools/list":
            return JSONResponse({"jsonrpc": "2.0", "id": msg["id"],
                                 "result": {"tools": ENGINE_TOOLS}})
        params = msg.get("params") or {}
        tracker.cur += 1
        tracker.max = max(tracker.max, tracker.cur)
        try:
            if params.get("name") not in READ_TOOLS:
                # Hold the "write" open until the test releases it.
                tracker.write_entered.set()
                await _signal(tracker.release)
        finally:
            tracker.cur -= 1
        args = params.get("arguments") or {}
        engine = {"status": "success", "filepath": args.get("filepath", "")}
        return JSONResponse({"jsonrpc": "2.0", "id": msg["id"], "result": {
            "content": [{"type": "text", "text": json.dumps(engine)}], "isError": False}})

    return app


@pytest.fixture(autouse=True)
def _fresh_locks():
    """Lock caches hold asyncio.Locks bound to the loop that created them; pytest
    gives each test a new loop, so clear them so a stale cross-loop lock can't
    leak between tests. (Production runs one loop for the whole process life.)"""
    import cognita.proxy as p

    p._WORKER_WRITE_LOCKS.clear()
    p._EDIT_LOCKS.clear()
    yield


@pytest.fixture
def env(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "a.md").write_text("aaa", encoding="utf-8")
    (docs / "b.md").write_text("bbb", encoding="utf-8")

    registry = Registry(tmp_path / "registry.yaml")
    registry.add(Project(name="RW", documents_dir=docs, data_dir=tmp_path / "d1"))
    # 13.0 §7.3: the `config.test_mode` registry-token fallback this fixture
    # authenticated through is deleted; it now uses the production path, a
    # global static key from the parent-owned policy store. OAuth is off so an
    # unknown bearer stays a 401 rather than a 503 from the absent OAuth child.
    auth = AuthenticationPolicyStore(
        tmp_path / "authentication.yaml", project_names=["RW"]
    )
    tok = auth.mutate_global(
        expected_revision=0, oauth_enabled=False, static_key_action="generate"
    )["generated_key"]
    connector_store = ConnectorStore(tmp_path / "connectors.yaml")
    connector_store.create(expected_revision=0, name="Test connector", project_names=["RW"])
    tracker = Tracker()
    worker = make_fake_worker(tracker)
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
    return app, tok, tracker


def call(name, arguments, msg_id=1):
    arguments = {"project": "RW", **arguments}
    return {"jsonrpc": "2.0", "id": msg_id, "method": "tools/call",
            "params": {"name": name, "arguments": arguments}}


async def post(app, token, payload):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        return await c.post(f"/mcp/connectors/{app.state.test_connector_slug}/mcp/v{PUBLIC_CONTRACT_VERSION}", json=payload,
                            headers={"Authorization": f"Bearer {token}"})


async def _second_writer_waits_on_the_lock(app, tok, tracker, probe_lock, first, second):
    """Start `first`, then `second`; the second must queue on the gateway's lock
    while the first is inside the worker, and never reach the worker alongside it."""
    t1 = asyncio.create_task(post(app, tok, first))
    await _signal(tracker.write_entered)
    t2 = asyncio.create_task(post(app, tok, second))
    await _signal(probe_lock.contended)
    assert tracker.cur == 1  # the second write is held at the lock, not in the worker
    tracker.release.set()
    r1, r2 = await asyncio.gather(t1, t2)
    assert r1.status_code == 200 and r2.status_code == 200
    assert tracker.max == 1  # serialized by the per-project write lock


async def test_concurrent_writes_to_different_files_serialized(env, probe_lock):
    """Two writes to DIFFERENT files in one project must not overlap."""
    app, tok, tracker = env
    await _second_writer_waits_on_the_lock(
        app, tok, tracker, probe_lock,
        call("add_document", {"filepath": "a.md", "content": "x"}, 1),
        call("add_document", {"filepath": "b.md", "content": "y"}, 2),
    )


async def test_reindex_serialized_with_write(env, probe_lock):
    """reindex_documents must not overlap a concurrent add_document."""
    app, tok, tracker = env
    await _second_writer_waits_on_the_lock(
        app, tok, tracker, probe_lock,
        call("reindex_documents", {}, 1),
        call("add_document", {"filepath": "a.md", "content": "x"}, 2),
    )


async def test_reads_not_blocked_by_writes(env):
    """Read tools must NOT take the write lock (a search may run during a write)."""
    app, tok, tracker = env
    write = asyncio.create_task(
        post(app, tok, call("add_document", {"filepath": "a.md", "content": "x"}, 1))
    )
    await _signal(tracker.write_entered)
    # The write is still inside the worker; the search must get through anyway.
    read = await post(app, tok, call("search_knowledge", {"query": "hi"}, 2))
    assert read.status_code == 200
    assert tracker.max == 2  # read + write overlapped
    tracker.release.set()
    assert (await write).status_code == 200
