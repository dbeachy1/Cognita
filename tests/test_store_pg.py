"""Integration tests for cognita.store against a real PostgreSQL + pgvector.

Activated by COGNITA_TEST_PG_DSN (DESIGN-4.0 §6 risk 5); skipped everywhere
else so the suite stays green on boxes without Postgres. On kei:

    COGNITA_TEST_PG_DSN=postgresql://postgres@127.0.0.1:5433/cognita \
        .venv/bin/python -m pytest tests/test_store_pg.py -v

Includes the M1 done-when gauntlet: kill a churning reindex process 20×, the
index must be consistent every time.
"""

import os
import random
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest

from cognita.store import ChunkRecord, DocumentRecord, Store, schema_for
from reindex_churn_child import make_doc

DSN = os.environ.get("COGNITA_TEST_PG_DSN", "")
pytestmark = pytest.mark.skipif(not DSN, reason="COGNITA_TEST_PG_DSN not set")

DIMS = 8  # small vectors keep the tests fast; the math is dimension-agnostic
CHILD = Path(__file__).parent / "reindex_churn_child.py"


def unique_project() -> str:
    return f"T{uuid.uuid4().hex[:10]}"


@pytest.fixture
async def store():
    s = Store(DSN, embedding_dimensions=DIMS)
    await s.connect()
    yield s
    await s.close()


@pytest.fixture
async def project(store):
    """A fresh schema per test, dropped afterwards."""
    name = unique_project()
    await store.ensure_project(name)
    yield name
    await store.drop_project(name)


def doc_with_chunks(source="a.md", version=1, n_chunks=3):
    doc = DocumentRecord(
        doc_id=f"{source}-v{version}",
        source=source,
        category="notes",
        format="markdown",
        keywords=["k1", "k2"],
        content_hash=f"v{version}",
        file_mtime=datetime(2026, 7, 9, 12, 0, tzinfo=timezone.utc),
        file_size=n_chunks,
    )
    chunks = [
        ChunkRecord(
            chunk_id=f"{source}-v{version}-c{i}",
            chunk_index=i,
            content=f"{source} v{version} chunk {i}",
            embedding=[float(version), float(i)] + [0.5] * (DIMS - 2),
            section=f"s{i}",
        )
        for i in range(n_chunks)
    ]
    return doc, chunks


# ---------- lifecycle + schema management ----------


async def test_ping(store):
    assert await store.ping() is True


async def test_ensure_project_is_idempotent(store):
    name = unique_project()
    try:
        assert not await store.has_project(name)
        await store.ensure_project(name)
        await store.ensure_project(name)  # second run must be a no-op, not an error
        assert await store.has_project(name)
    finally:
        await store.drop_project(name)
    assert not await store.has_project(name)


# ---------- transactional replace ----------


async def test_replace_and_read_back(store, project):
    doc, chunks = doc_with_chunks()
    await store.replace_document(project, doc, chunks)

    got = await store.get_document(project, "a.md")
    assert got is not None
    assert (got.doc_id, got.category, got.format) == (doc.doc_id, "notes", "markdown")
    assert got.keywords == ["k1", "k2"]
    assert got.content_hash == "v1"
    assert got.file_mtime == doc.file_mtime
    assert got.indexed_at is not None  # stamped by the database

    got_chunks = await store.get_chunks(project, "a.md")
    assert [c.chunk_index for c in got_chunks] == [0, 1, 2]
    assert got_chunks[1].content == "a.md v1 chunk 1"
    assert got_chunks[1].section == "s1"
    # 0.5 / 1.0 / small ints are float32-exact, so the roundtrip is equality
    assert got_chunks[1].embedding == chunks[1].embedding

    stats = await store.stats(project)
    assert (stats.documents, stats.chunks) == (1, 3)
    assert await store.check_consistency(project) == []


async def test_replace_swaps_versions_atomically(store, project):
    doc1, chunks1 = doc_with_chunks(version=1, n_chunks=3)
    await store.replace_document(project, doc1, chunks1)
    doc2, chunks2 = doc_with_chunks(version=2, n_chunks=5)
    await store.replace_document(project, doc2, chunks2)

    got = await store.get_document(project, "a.md")
    assert got.doc_id == "a.md-v2"
    got_chunks = await store.get_chunks(project, "a.md")
    assert len(got_chunks) == 5
    assert all("v2" in c.content for c in got_chunks)  # zero v1 residue
    stats = await store.stats(project)
    assert (stats.documents, stats.chunks) == (1, 5)


async def test_moved_file_retires_stale_row(store, project):
    """Same doc_id (content-addressed) at a new path: the old path's row dies
    with the move instead of colliding on the primary key."""
    doc, chunks = doc_with_chunks(source="old.md")
    await store.replace_document(project, doc, chunks)
    moved = DocumentRecord(doc_id=doc.doc_id, source="new.md", content_hash="v1")
    await store.replace_document(project, moved, chunks)

    assert await store.get_document(project, "old.md") is None
    assert (await store.get_document(project, "new.md")).doc_id == doc.doc_id
    assert (await store.stats(project)).documents == 1


async def test_failed_replace_rolls_back_completely(store, project):
    """The core 4.0 claim, in-process: an error mid-replace leaves the previous
    version fully intact — no residue, no partial state."""
    doc1, chunks1 = doc_with_chunks(version=1, n_chunks=3)
    await store.replace_document(project, doc1, chunks1)

    doc2, chunks2 = doc_with_chunks(version=2, n_chunks=4)
    chunks2[3].chunk_index = 1  # duplicate index → UNIQUE (doc_id, chunk_index) violation
    with pytest.raises(Exception) as exc_info:
        await store.replace_document(project, doc2, chunks2)
    assert "duplicate key" in str(exc_info.value)

    got = await store.get_document(project, "a.md")
    assert got.doc_id == "a.md-v1"  # v1 survived the failed v2 replace
    got_chunks = await store.get_chunks(project, "a.md")
    assert len(got_chunks) == 3
    assert all("v1" in c.content for c in got_chunks)
    assert await store.check_consistency(project) == []


async def test_delete_document_cascades(store, project):
    doc, chunks = doc_with_chunks()
    await store.replace_document(project, doc, chunks)
    assert await store.delete_document(project, "a.md") is True
    assert await store.get_document(project, "a.md") is None
    assert (await store.stats(project)).chunks == 0  # cascaded, no orphans
    assert await store.delete_document(project, "a.md") is False


# ---------- move (4.1: metadata-only relocate, no re-embed) ----------


async def test_move_document_preserves_chunks_and_embeddings(store, project):
    doc, chunks = doc_with_chunks(source="old/a.md", n_chunks=4)
    await store.replace_document(project, doc, chunks)
    before = await store.get_chunks(project, "old/a.md")

    moved = await store.move_document(project, "old/a.md", "new/b.md", "newid123")
    assert moved == 4

    assert await store.get_document(project, "old/a.md") is None      # old gone
    new_doc = await store.get_document(project, "new/b.md")
    assert new_doc is not None
    assert new_doc.doc_id == "newid123"                                # re-keyed
    assert new_doc.content_hash == doc.content_hash                    # content unchanged
    assert new_doc.category == "notes" and new_doc.keywords == ["k1", "k2"]  # metadata copied

    after = await store.get_chunks(project, "new/b.md")
    assert [c.chunk_index for c in after] == [0, 1, 2, 3]
    assert [c.chunk_id for c in after] == [f"newid123_{i}" for i in range(4)]  # re-keyed
    assert [c.content for c in after] == [c.content for c in before]   # not re-parsed
    assert [c.embedding for c in after] == [c.embedding for c in before]  # NOT re-embedded
    assert (await store.stats(project)).documents == 1                # no duplicate
    assert await store.check_consistency(project) == []


async def test_move_document_missing_source_returns_none(store, project):
    assert await store.move_document(project, "ghost.md", "x.md", "id") is None


async def test_move_document_rejects_occupied_destination(store, project):
    await store.replace_document(project, *doc_with_chunks(source="a.md"))
    await store.replace_document(project, *doc_with_chunks(source="b.md"))
    with pytest.raises(ValueError, match="already indexed"):
        await store.move_document(project, "a.md", "b.md", "id")
    # both survive the rejected move
    assert (await store.stats(project)).documents == 2


# ---------- isolation ----------


async def test_projects_are_isolated(store):
    a, b = unique_project(), unique_project()
    await store.ensure_project(a)
    await store.ensure_project(b)
    try:
        doc, chunks = doc_with_chunks(source="same-name.md")
        await store.replace_document(a, doc, chunks)
        assert (await store.stats(a)).documents == 1
        assert (await store.stats(b)).documents == 0  # same source name, zero bleed
        await store.drop_project(a)
        assert (await store.stats(b)).documents == 0  # dropping A can't touch B
        assert await store.has_project(b)
    finally:
        await store.drop_project(a)
        await store.drop_project(b)


# ---------- consistency check ----------


async def test_check_consistency_flags_chunkless_document(store, project):
    # The API can't create this state (constraints + zero-chunk guard), so
    # manufacture it with raw SQL to prove the check would catch it.
    await store.pool.execute(
        f'INSERT INTO "{schema_for(project)}".documents (doc_id, source, content_hash) '
        "VALUES ('ghost', 'ghost.md', 'x')"
    )
    violations = await store.check_consistency(project)
    assert len(violations) == 1 and "has no chunks" in violations[0]


# ---------- 4.4 registered tier ----------


def registered_doc(source="build.py", content="def build_worldbook():\n    return 1\n"):
    return DocumentRecord(
        doc_id=f"reg-{source}",
        source=source,
        category="general",
        format=Path(source).suffix,
        keywords=[],
        content_hash="rh1",
        file_mtime=datetime(2026, 7, 30, 12, 0, tzinfo=timezone.utc),
        file_size=len(content),
        tier="registered",
        content=content,
    )


async def test_registered_document_stores_whole_with_zero_chunks(store, project):
    doc = registered_doc()
    await store.replace_document(project, doc, [])

    got = await store.get_document(project, "build.py")
    assert got is not None
    assert got.tier == "registered" and got.is_registered
    assert got.content == doc.content  # stored whole
    assert await store.chunk_count(project, "build.py") == 0
    # The defining property: no vector exists anywhere for this document.
    assert await store.first_chunk_embedding(project, "build.py") is None


async def test_embedded_document_still_refuses_zero_chunks(store, project):
    doc, _ = doc_with_chunks()
    with pytest.raises(ValueError, match="zero chunks"):
        await store.replace_document(project, doc, [])


async def test_registered_document_refuses_chunks(store, project):
    _, chunks = doc_with_chunks()
    with pytest.raises(ValueError, match="never embedded"):
        await store.replace_document(project, registered_doc(), chunks)


async def test_registered_lexical_search_finds_literal_string(store, project):
    await store.replace_document(project, registered_doc(), [])
    hits = await store.registered_lexical_search(project, "build_worldbook", 10)
    assert [h.source for h in hits] == ["build.py"]
    assert hits[0].chunk_index == 0
    assert "build_worldbook" in hits[0].content


async def test_registered_document_is_findable_by_its_filename(store, project):
    """Regression, found live on kei against 4.4.0.

    The filename is the primary lookup key for this tier ("find me that
    script"), but Postgres parses 'build_worldbook.py' as a single `host`
    token, so a tsv built from content alone matched neither 'worldbook' nor
    'build_worldbook'. The path is now indexed raw AND separator-flattened.
    """
    doc = registered_doc(
        source="worldbook/build_worldbook.py",
        content="import argparse\n\ndef main():\n    return 0\n",  # name NOT in body
    )
    await store.replace_document(project, doc, [])

    for query in ("build_worldbook", "worldbook", "build_worldbook.py", "argparse"):
        hits = await store.registered_lexical_search(project, query, 10)
        assert [h.source for h in hits] == ["worldbook/build_worldbook.py"], query


async def test_registered_filename_match_outranks_a_body_mention(store, project):
    """Weight A (path) over weight B (content)."""
    await store.replace_document(
        project,
        registered_doc(source="deploy.sh", content="#!/bin/sh\necho deploying\n"),
        [],
    )
    await store.replace_document(
        project,
        registered_doc(source="notes.py", content="# see deploy.sh for the real thing\n"),
        [],
    )
    hits = await store.registered_lexical_search(project, "deploy", 10)
    assert hits[0].source == "deploy.sh"


async def test_registered_document_never_appears_in_dense_search(store, project):
    """The semantic half must not see this tier at any distance."""
    await store.replace_document(project, registered_doc(), [])
    doc, chunks = doc_with_chunks()
    await store.replace_document(project, doc, chunks)

    hits = await store.dense_search(project, [0.5] * DIMS, limit=50)
    assert hits, "sanity: the embedded document should still be found"
    assert all(h.source != "build.py" for h in hits)


async def test_policy_allowlists_filter_every_sql_leg_before_limit(store, project):
    """Excluded top-ranked rows must not consume the candidate limit.

    This exercises the actual PostgreSQL predicates, including the
    registered-only leg; a Python post-filter would return no result at limit 1.
    """
    query_vector = [1.0] + [0.0] * (DIMS - 1)
    private, private_chunks = doc_with_chunks("private.md", version=71, n_chunks=1)
    public, public_chunks = doc_with_chunks("public.md", version=72, n_chunks=1)
    private_chunks[0].embedding = query_vector
    public_chunks[0].embedding = [0.0, 1.0] + [0.0] * (DIMS - 2)
    private_chunks[0].content = "cognita " * 30
    public_chunks[0].content = "cognita eligible"
    await store.replace_document(project, private, private_chunks)
    await store.replace_document(project, public, public_chunks)

    assert (await store.dense_search(project, query_vector, 1))[0].source == "private.md"
    assert (await store.lexical_search(project, "cognita", 1))[0].source == "private.md"
    assert (await store.dense_search(
        project, query_vector, 1, include_sources=["public.md"],
        include_doc_ids=[public.doc_id],
    ))[0].source == "public.md"
    assert (await store.lexical_search(
        project, "cognita", 1, include_sources=["public.md"],
        include_doc_ids=[public.doc_id],
    ))[0].source == "public.md"
    assert await store.dense_search(
        project, query_vector, 1, include_sources=[], include_doc_ids=[],
    ) == []
    assert await store.lexical_search(
        project, "cognita", 1, include_sources=[], include_doc_ids=[],
    ) == []

    private_registered = registered_doc("private/hidden.py", "cognita " * 30)
    public_registered = registered_doc("public/allowed.py", "cognita eligible")
    await store.replace_document(project, private_registered, [])
    await store.replace_document(project, public_registered, [])
    assert (await store.registered_lexical_search(project, "cognita", 1))[0].source == "private/hidden.py"
    assert (await store.registered_lexical_search(
        project, "cognita", 1, include_sources=["public/allowed.py"],
        include_doc_ids=[public_registered.doc_id],
    ))[0].source == "public/allowed.py"
    assert await store.registered_lexical_search(
        project, "cognita", 1, include_sources=[], include_doc_ids=[],
    ) == []


async def test_registered_lexical_search_honors_category_filter(store, project):
    doc = registered_doc()
    doc.category = "scripts"
    await store.replace_document(project, doc, [])
    assert await store.registered_lexical_search(project, "build_worldbook", 10, "scripts")
    assert not await store.registered_lexical_search(project, "build_worldbook", 10, "other")


async def test_stats_report_tiers_separately(store, project):
    doc, chunks = doc_with_chunks(n_chunks=3)
    await store.replace_document(project, doc, chunks)
    await store.replace_document(project, registered_doc(), [])

    stats = await store.stats(project)
    assert stats.documents == 2
    assert stats.embedded_documents == 1
    assert stats.registered_documents == 1
    # Chunks/vectors belong to the embedded tier alone.
    assert stats.chunks == 3


async def test_check_consistency_accepts_chunkless_registered_document(store, project):
    await store.replace_document(project, registered_doc(), [])
    assert await store.check_consistency(project) == []


async def test_check_consistency_flags_registered_document_with_chunks(store, project):
    """Leftover vectors after a tier crossing are the violation for this tier."""
    doc, chunks = doc_with_chunks(source="build.py")
    await store.replace_document(project, doc, chunks)
    await store.pool.execute(
        f'UPDATE "{schema_for(project)}".documents SET tier = \'registered\' '
        "WHERE source = 'build.py'"
    )
    violations = await store.check_consistency(project)
    assert len(violations) == 1 and "never embedded" in violations[0]


async def test_replace_clears_chunks_when_crossing_into_registered(store, project):
    """notes.md -> notes.py: the old tier's vectors must not survive."""
    doc, chunks = doc_with_chunks(source="notes.md")
    await store.replace_document(project, doc, chunks)
    assert await store.chunk_count(project, "notes.md") == 3

    crossed = registered_doc(source="notes.md", content="print('now a script')\n")
    await store.replace_document(project, crossed, [])

    assert await store.chunk_count(project, "notes.md") == 0
    assert await store.first_chunk_embedding(project, "notes.md") is None
    assert await store.check_consistency(project) == []


async def test_move_document_preserves_tier_and_content(store, project):
    await store.replace_document(project, registered_doc(), [])
    moved = await store.move_document(project, "build.py", "lib/build.py", "reg-new")
    assert moved == 0  # nothing to re-key: registered documents have no chunks

    got = await store.get_document(project, "lib/build.py")
    assert got is not None and got.tier == "registered"
    assert "build_worldbook" in got.content
    # Still keyword-findable at its new home.
    hits = await store.registered_lexical_search(project, "build_worldbook", 10)
    assert [h.source for h in hits] == ["lib/build.py"]


async def test_list_sources_reports_tier_for_change_detection(store, project):
    await store.replace_document(project, registered_doc(), [])
    doc, chunks = doc_with_chunks()
    await store.replace_document(project, doc, chunks)

    sources = await store.list_sources(project)
    assert sources["build.py"].tier == "registered"
    assert sources["a.md"].tier == "embedded"


# REMOVED in 13.0, with the code they covered (§4.1, §8):
#   test_ensure_project_rebuilds_the_4_4_0_content_only_tsv
#   test_ensure_project_migrates_a_pre_44_schema
# Both drove in-place migration branches in render_ddl — the 4.4.0 self-healing
# tsv DROP COLUMN and the 4.4 tier/content ALTERs. There is no in-place
# migration left to test: a database whose stamped schema version is not this
# image's is refused untouched and reset by the user. The behavior they were
# really protecting — a registered document being findable by its FILENAME, the
# live 4.4.0 failure on kei — is now proven directly against the final DDL by
# test_registered_document_is_findable_by_filename below and by
# tests/test_schema_version.py's fresh-database case.


async def test_registered_document_is_findable_by_filename(store, project):
    """The 4.4.1 tsv, asserted against the shape a fresh 13.0 install creates.

    Searching "build_worldbook" could not find build_worldbook.py on kei under
    4.4.0, whose documents.tsv indexed content alone. The three path forms in
    render_ddl are what fixed it, so each way of asking must still work.
    """
    doc = registered_doc(
        source="worldbook/build_worldbook.py",
        content="import argparse\n\ndef main():\n    return 0\n",
    )
    await store.replace_document(project, doc, [])
    for query in ("build_worldbook", "worldbook", "build_worldbook.py"):
        hits = await store.registered_lexical_search(project, query, 10)
        assert [h.source for h in hits] == ["worldbook/build_worldbook.py"], query


# ---------- the M1 done-when: kill-mid-reindex gauntlet ----------

KILLS = 20
N_DOCS = 6
N_CHUNKS = 40


async def verify_all_docs_consistent(store, project) -> set[int]:
    """Every doc must exist, be complete, and be single-version. Returns the
    set of versions seen (to prove the churn actually advanced)."""
    assert await store.check_consistency(project) == []
    docs = await store.list_documents(project)
    assert len(docs) == N_DOCS
    versions = set()
    for doc in docs:
        version = int(doc.content_hash.lstrip("v"))
        versions.add(version)
        assert doc.doc_id.endswith(f"-v{version}")  # doc row is self-consistent
        chunks = await store.get_chunks(project, doc.source)
        assert len(chunks) == doc.file_size == N_CHUNKS
        assert [c.chunk_index for c in chunks] == list(range(N_CHUNKS))
        for c in chunks:
            # THE atomicity assertion: every chunk belongs to the doc row's
            # version — a torn replace would mix versions or change the count.
            assert f" v{version} " in f" {c.content} ", (
                f"{doc.source}: chunk {c.chunk_index} is not from version {version}: "
                f"{c.content!r}"
            )
    return versions


async def test_kill_mid_reindex_gauntlet(store):
    """DESIGN-4.0 §9 M1: kill the process mid-reindex 20×, index always
    consistent. The child churns transactional replaces as fast as it can;
    we SIGKILL it at a random moment and audit every row."""
    project = unique_project()
    await store.ensure_project(project)
    try:
        # Seed version 0 so every doc exists before the first kill.
        for d in range(N_DOCS):
            doc, chunks = make_doc(d, 0, N_CHUNKS, DIMS)
            await store.replace_document(project, doc, chunks)

        max_versions = []
        for iteration in range(KILLS):
            proc = subprocess.Popen(
                [sys.executable, str(CHILD), DSN, project,
                 str(N_DOCS), str(N_CHUNKS), str(DIMS)],
                stdout=subprocess.PIPE,
                text=True,
            )
            try:
                assert proc.stdout.readline().strip() == "READY"
                time.sleep(random.uniform(0.05, 0.6))  # land the kill anywhere
            finally:
                proc.kill()
                proc.wait(timeout=30)

            versions = await verify_all_docs_consistent(store, project)
            max_versions.append(max(versions))

        # Prove the gauntlet exercised real writes: versions must have advanced
        # across the 20 rounds (each child resumes from version 1, so later
        # rounds re-cover earlier versions — total progress shows as movement).
        assert any(v > 0 for v in max_versions), f"no churn observed: {max_versions}"
    finally:
        await store.drop_project(project)


# ------------------------------- 5.1: a filtered vector search must not come back short


async def _seed_split_corpus(store, project, n_noise=200, n_wanted=5):
    """A corpus where the WANTED category is uniformly FARTHEST from the query.

    That is the shape that exposes HNSW post-filtering: the nearest-N prefilter
    fills up entirely with the other category.

    Vectors are JITTERED rather than identical. A first version used one vector
    for every noise row and one for every wanted row; with 205 duplicate points
    the HNSW graph is degenerate and the search effectively visits everything, so
    the truncation never reproduced and the test could not fail.
    """
    rng = random.Random(20260829)

    def jitter(pole: int) -> list[float]:
        v = [rng.random() * 0.05 for _ in range(DIMS)]
        v[pole] = 1.0
        return v

    for i in range(n_noise):
        doc = DocumentRecord(doc_id=f"noise-{i}", source=f"noise/{i}.md",
                             category="noise", format="markdown", content_hash=f"h{i}")
        await store.replace_document(project, doc, [ChunkRecord(
            chunk_id=f"noise-{i}-0", chunk_index=0, content="noise",
            embedding=jitter(0))])
    for i in range(n_wanted):
        doc = DocumentRecord(doc_id=f"want-{i}", source=f"want/{i}.md",
                             category="wanted", format="markdown", content_hash=f"w{i}")
        await store.replace_document(project, doc, [ChunkRecord(
            chunk_id=f"want-{i}-0", chunk_index=0, content="wanted",
            embedding=jitter(DIMS - 1))])
    return [1.0] + [0.0] * (DIMS - 1)


# NOTE — why there is no unit test reproducing the TRUNCATION itself.
#
# The defect is real and was measured on production, but it does not reproduce at
# fixture scale and I could not make it. Two attempts, both recorded here so the
# next person does not repeat them:
#
#   1. 200 noise + 5 wanted rows with IDENTICAL vectors per group. With duplicate
#      points the HNSW graph is degenerate and the search effectively visits
#      everything, so nothing truncates.
#   2. 2000 noise + 5 wanted with jittered vectors around two poles, and
#      enable_seqscan=off to force the index. Still returns all 5 — at this size
#      the graph is small enough that the far cluster stays reachable within
#      ef_search.
#
# Both versions PASSED with the fix removed, which makes them worse than no test.
# The evidence that the defect is real is an EXPLAIN ANALYZE against an index
# with 11871 chunks and 73 documents in the filtered category on 2026-08-29:
#
#     Limit (actual rows=0.00)
#       -> Nested Loop (actual rows=0.00)
#            -> Index Scan using chunks_embedding_hnsw on chunks c
#                 (actual rows=40.00)          <- ef_search default, pre-filter
#
#   ...i.e. ZERO results for LIMIT 5 over a category holding 73 documents. With
#   hnsw.iterative_scan = strict_order the same query returns its 5 rows in 7ms.
#
# The two tests below therefore guard the API contract (a filtered search returns
# the full limit, an unfiltered one is untouched) rather than the index-scan
# mechanism. Reproducing the mechanism needs a corpus in the thousands-of-chunks
# range with a realistic distance distribution — a burn-in fixture, not a unit test.


async def test_filtered_dense_search_returns_the_full_limit(store, project):
    """The API contract: a category-filtered search returns the full limit.

    Passes with and without the iterative-scan settings at this corpus size (see
    the note above) — it guards the contract, not the mechanism.
    """
    query = await _seed_split_corpus(store, project)
    hits = await store.dense_search(project, query, 5, category="wanted")
    assert len(hits) == 5, f"filtered search came back short: {len(hits)} of 5"
    assert {h.source for h in hits} == {f"want/{i}.md" for i in range(5)}


async def test_unfiltered_dense_search_is_unchanged(store, project):
    """The iterative-scan settings apply ONLY to filtered queries: search_knowledge
    fuses chunk-level hits by rank, so changing the unfiltered path's scan
    behavior would move ranking under everything that already works."""
    doc = DocumentRecord(doc_id="a", source="a.md", category="notes",
                         format="markdown", content_hash="h")
    await store.replace_document(project, doc, [ChunkRecord(
        chunk_id="a-0", chunk_index=0, content="x",
        embedding=[1.0] + [0.0] * (DIMS - 1))])
    hits = await store.dense_search(project, [1.0] + [0.0] * (DIMS - 1), 5)
    assert [h.source for h in hits] == ["a.md"]
