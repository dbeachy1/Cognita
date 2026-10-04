"""6.0.13 — `write_documents` writes N documents as one unit, or writes none.

🔴 **The incident this exists for.** A client pushing a logical SET issues N
separate `update_document` calls, and each one writes its file and then spends
~6 seconds embedding. A restart anywhere in that ~80-second window leaves some
files new and some old — and **every file is individually valid**, so nothing
downstream can tell the set is inconsistent. On 2026-09-02 a restart cut a live
13-section worldbook push: four sections had the new shape, nine did not, and
the compiled artifact was built from the nine. Nothing looked broken.

6.0.12 made each CALL honest (a canceled write no longer leaves bytes on disk
while reporting failure). It could not make the SEQUENCE atomic, because
Cognita never knew a sequence existed. This is the caller telling it.

⚠️ **What is asserted here is what the tool actually promises**, which is not
"a partial set is impossible":

- a rejection anywhere writes NOTHING,
- the slow work all happens before any target changes,
- an indexing failure restores EVERY file,
- and the publish window is N renames rather than N embeds.

A SIGKILL landing between two renames still splits the set. The window shrinks
by orders of magnitude; it does not close. Tests that claimed otherwise would
be worse than no tests.
"""

from __future__ import annotations

import asyncio
import os
import uuid

import pytest

from cognita.config import CognitaConfig
from cognita.engine_local import LocalEngineHost
from cognita.registry import Project, Registry
from cognita.retrieval import RetrievalCore
from cognita.store import Store
from cognita.tokens import generate_token, hash_token
from retrieval_fakes import HashEmbedder, OverlapReranker

DSN = os.environ.get("COGNITA_TEST_PG_DSN", "")
DIMS = 32
pg = pytest.mark.skipif(not DSN, reason="COGNITA_TEST_PG_DSN not set")


# --------------------------------------------------------------------------
# The staging primitives — no database, no index, pure file mechanics
# --------------------------------------------------------------------------


def test_staging_does_not_touch_the_target(tmp_path):
    """🔴 Phase 2 must be invisible. Everything slow and failure-prone happens
    before ANY target changes, which is the whole basis of the atomicity claim."""
    target = tmp_path / "doc.md"
    target.write_bytes(b"original\n")
    tmp = LocalEngineHost._stage_verbatim(target, "replacement\n")

    assert target.read_bytes() == b"original\n", "staging published early"
    assert tmp.exists() and tmp != target
    assert tmp.read_bytes() == b"replacement\n"


def test_publish_flips_every_staged_file(tmp_path):
    targets = []
    staged = []
    for i in range(5):
        t = tmp_path / f"doc{i}.md"
        t.write_bytes(f"old-{i}\n".encode())
        targets.append(t)
        staged.append((LocalEngineHost._stage_verbatim(t, f"new-{i}\n"), t))

    # Nothing published yet.
    assert [t.read_bytes() for t in targets] == [f"old-{i}\n".encode() for i in range(5)]
    LocalEngineHost._publish_staged(staged)
    assert [t.read_bytes() for t in targets] == [f"new-{i}\n".encode() for i in range(5)]
    assert not any(tmp.exists() for tmp, _ in staged), "staging files left behind"


def test_staging_is_byte_verbatim(tmp_path):
    """The 5.0 invariant survives the new path: no strip, no EOL translation.
    A batch write must not quietly become the one write that normalizes."""
    target = tmp_path / "doc.md"
    payload = " lead\r\nkeep\r\n\n"
    tmp = LocalEngineHost._stage_verbatim(target, payload)
    assert tmp.read_bytes() == payload.encode("utf-8")


def test_undo_batch_restores_everything_including_creations(tmp_path):
    """A batch that created new files must undo to their ABSENCE, not to empty
    files — otherwise a rolled-back batch leaves orphans nothing will index."""
    existing = tmp_path / "existing.md"
    existing.write_bytes(b"before\n")
    created = tmp_path / "created.md"
    created.write_bytes(b"new content\n")

    host = LocalEngineHost.__new__(LocalEngineHost)
    host._undo_batch([(existing, b"before\n"), (created, None)])

    assert existing.read_bytes() == b"before\n"
    assert not created.exists()


# --------------------------------------------------------------------------
# Validation — a rejection anywhere means NOTHING was written
# --------------------------------------------------------------------------


def make_host(tmp_path, docs_dir, name) -> LocalEngineHost:
    registry = Registry(tmp_path / "registry.yaml")
    registry.add(Project(name=name, documents_dir=docs_dir,
                         data_dir=tmp_path / "data",
                         token_sha256=hash_token(generate_token())))
    store = Store(DSN or "postgresql://nowhere/none", embedding_dimensions=DIMS)
    core = RetrievalCore(store, HashEmbedder(DIMS), OverlapReranker())
    return LocalEngineHost(CognitaConfig(), registry, core)


@pytest.fixture
def offline(tmp_path):
    """A host with no database. Enough for every all-or-nothing rejection,
    because validation completes before a single byte is staged."""
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "a.md").write_text("# A\n\noriginal a\n", encoding="utf-8")
    (docs / "b.md").write_text("# B\n\noriginal b\n", encoding="utf-8")
    name = f"T{uuid.uuid4().hex[:10]}"
    host = make_host(tmp_path, docs, name)
    return host, host.registry.get(name), docs


async def test_one_bad_document_writes_none_of_them(offline):
    """🔴 THE CENTRAL PROMISE. The second document is unindexable, so the FIRST
    one — perfectly valid, and first in the list — must not land either."""
    host, project, docs = offline
    before = (docs / "a.md").read_bytes()

    out = await host._write_documents(project, {"documents": [
        {"filepath": "a.md", "content": "# A\n\nrewritten a\n"},
        {"filepath": "nope.exe", "content": "binary-ish"},
    ]})

    assert out["status"] == "error"
    assert out["documents_written"] == 0
    assert "NOTHING was written" in out["message"]
    assert (docs / "a.md").read_bytes() == before, "a valid document leaked through"
    assert not (docs / "nope.exe").exists()


async def test_a_stale_hash_on_any_document_stops_the_whole_batch(offline):
    host, project, docs = offline
    before_a = (docs / "a.md").read_bytes()
    before_b = (docs / "b.md").read_bytes()

    out = await host._write_documents(project, {"documents": [
        {"filepath": "a.md", "content": "# A\n\nnew a\n"},
        {"filepath": "b.md", "content": "# B\n\nnew b\n",
         "expected_sha256": "0" * 64},
    ]})

    assert out["status"] == "error"
    assert out["documents_written"] == 0
    assert (docs / "a.md").read_bytes() == before_a
    assert (docs / "b.md").read_bytes() == before_b


async def test_a_duplicate_filepath_is_refused_rather_than_resolved(offline):
    """Two writes to one path in one batch make the result order-dependent —
    the second silently wins and the first is lost. Refuse instead of picking."""
    host, project, docs = offline
    before = (docs / "a.md").read_bytes()

    out = await host._write_documents(project, {"documents": [
        {"filepath": "a.md", "content": "# A\n\nfirst\n"},
        {"filepath": "a.md", "content": "# A\n\nsecond\n"},
    ]})

    assert out["status"] == "error"
    assert "more than once" in out["message"]
    assert (docs / "a.md").read_bytes() == before


async def test_an_empty_batch_is_an_error_not_a_no_op(offline):
    host, project, _docs = offline
    out = await host._write_documents(project, {"documents": []})
    assert out["status"] == "error"
    assert out["reason"] == "invalid"


async def test_an_unknown_per_document_key_is_refused(offline):
    """5.0 §2 applies inside the batch too: a silently ignored per-document
    filter returns a wrong answer that looks like a right one."""
    host, project, docs = offline
    before = (docs / "a.md").read_bytes()
    out = await host._write_documents(project, {"documents": [
        {"filepath": "a.md", "content": "x\n", "catgory": "typo"},
    ]})
    assert out["status"] == "error"
    assert out["reason"] == "unknown_argument"
    assert (docs / "a.md").read_bytes() == before


async def test_a_path_escaping_the_project_stops_the_batch(offline):
    host, project, docs = offline
    before = (docs / "a.md").read_bytes()
    out = await host._write_documents(project, {"documents": [
        {"filepath": "a.md", "content": "# A\n\nnew\n"},
        {"filepath": "../escape.md", "content": "nope\n"},
    ]})
    assert out["status"] == "error"
    assert out["reason"] == "invalid_path"
    assert (docs / "a.md").read_bytes() == before


# --------------------------------------------------------------------------
# The indexed path — needs PostgreSQL
# --------------------------------------------------------------------------


@pytest.fixture
async def env(tmp_path):
    name = f"T{uuid.uuid4().hex[:10]}"
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "a.md").write_text("# A\n\noriginal a\n", encoding="utf-8")
    (docs / "b.md").write_text("# B\n\noriginal b\n", encoding="utf-8")
    host = make_host(tmp_path, docs, name)
    await host.store.connect()
    await host.store.ensure_project(name)
    await host.core.index_project(name, docs)
    yield host, host.registry.get(name), docs
    await host.store.drop_project(name)
    await host.store.close()


@pg
async def test_a_good_batch_writes_and_indexes_every_document(env):
    host, project, docs = env
    out = await host._write_documents(project, {"documents": [
        {"filepath": "a.md", "content": "# A\n\nrewritten a\n"},
        {"filepath": "b.md", "content": "# B\n\nrewritten b\n"},
        {"filepath": "c.md", "content": "# C\n\nbrand new c\n"},
    ]})

    assert out["status"] == "success"
    assert out["documents_written"] == 3
    assert out["chunks_indexed"] > 0
    assert (docs / "a.md").read_text(encoding="utf-8") == "# A\n\nrewritten a\n"
    assert (docs / "b.md").read_text(encoding="utf-8") == "# B\n\nrewritten b\n"
    assert (docs / "c.md").read_text(encoding="utf-8") == "# C\n\nbrand new c\n"


@pg
async def test_an_indexing_failure_rolls_the_WHOLE_batch_back(env, monkeypatch):
    """🔴 Phase 4. Two documents index fine and the third blows up — all three
    files must go back, including the one that was CREATED by this batch."""
    host, project, docs = env
    before_a = (docs / "a.md").read_bytes()
    before_b = (docs / "b.md").read_bytes()
    real = host.core.index_file
    calls = {"n": 0}

    async def flaky(*a, **k):
        calls["n"] += 1
        if calls["n"] == 3:
            raise RuntimeError("index blew up on the third document")
        return await real(*a, **k)

    monkeypatch.setattr(host.core, "index_file", flaky)

    with pytest.raises(RuntimeError):
        await host._write_documents(project, {"documents": [
            {"filepath": "a.md", "content": "# A\n\nrewritten a\n"},
            {"filepath": "b.md", "content": "# B\n\nrewritten b\n"},
            {"filepath": "c.md", "content": "# C\n\nbrand new c\n"},
        ]})

    assert (docs / "a.md").read_bytes() == before_a
    assert (docs / "b.md").read_bytes() == before_b
    assert not (docs / "c.md").exists(), "a created file survived the rollback"


@pg
async def test_a_CANCELED_batch_rolls_back_too(env, monkeypatch):
    """🔴 6.0.12's rule applied to the set. CancelledError is a BaseException,
    and a shutdown mid-batch is the exact scenario the tool exists for — if it
    escaped the rollback, the batch would leave the split set it was built to
    prevent."""
    host, project, docs = env
    before_a = (docs / "a.md").read_bytes()
    real = host.core.index_file
    calls = {"n": 0}

    async def canceled(*a, **k):
        calls["n"] += 1
        if calls["n"] == 2:
            raise asyncio.CancelledError()
        return await real(*a, **k)

    monkeypatch.setattr(host.core, "index_file", canceled)

    with pytest.raises(asyncio.CancelledError):
        await host._write_documents(project, {"documents": [
            {"filepath": "a.md", "content": "# A\n\nrewritten a\n"},
            {"filepath": "b.md", "content": "# B\n\nrewritten b\n"},
        ]})

    assert (docs / "a.md").read_bytes() == before_a
    assert (docs / "b.md").read_bytes() == b"# B\n\noriginal b\n"


@pg
async def test_the_files_are_all_published_before_any_indexing_starts(env):
    """🔴 THE POINT OF THE WHOLE DESIGN, made observable. In the old
    per-document loop, document N+1 was still on disk in its OLD form while
    document N was being embedded — that gap is where the split set came from.
    Here, by the time the first index call runs, every file already holds its
    new content."""
    host, project, docs = env
    seen: list[dict] = []
    real = host.core.index_file

    async def observe(*a, **k):
        seen.append({p.name: p.read_text(encoding="utf-8")
                     for p in sorted(docs.glob("*.md"))})
        return await real(*a, **k)

    host.core.index_file = observe
    try:
        out = await host._write_documents(project, {"documents": [
            {"filepath": "a.md", "content": "# A\n\nrewritten a\n"},
            {"filepath": "b.md", "content": "# B\n\nrewritten b\n"},
        ]})
    finally:
        host.core.index_file = real

    assert out["status"] == "success"
    # The FIRST index call already sees both files in their new state.
    assert seen[0]["a.md"] == "# A\n\nrewritten a\n"
    assert seen[0]["b.md"] == "# B\n\nrewritten b\n"
