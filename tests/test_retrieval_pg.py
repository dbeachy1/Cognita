"""Integration tests for the retrieval core against real PostgreSQL + pgvector.

Same activation as test_store_pg.py: COGNITA_TEST_PG_DSN (skipped without it).
Uses the deterministic HashEmbedder — no model downloads — so dense search is
real pgvector cosine math over meaningful (bag-of-words) vectors, and lexical
search is real Postgres FTS. This is the end-to-end M2 pipeline minus only the
production models, which the on-kei smoke run covers.
"""

import os
import uuid
from pathlib import Path

import pytest

from cognita.parsing import ExtensionPolicy
from cognita.retrieval import RetrievalCore
from cognita.store import Store
from retrieval_fakes import HashEmbedder, OverlapReranker

DSN = os.environ.get("COGNITA_TEST_PG_DSN", "")
pytestmark = pytest.mark.skipif(not DSN, reason="COGNITA_TEST_PG_DSN not set")

DIMS = 32


def write_corpus(root: Path) -> None:
    (root / "rocm.md").write_text(
        "# ROCm Build\n\n## Prerequisites\n\nInstall the amdgpu driver stack first. "
        "The ROCm build procedure needs cmake and ninja and takes about an hour.\n\n"
        "## Building\n\nClone the rocm repository and run the build script with gfx1201 targets.",
        encoding="utf-8",
    )
    (root / "pcie.md").write_text(
        "# Motherboard Layout\n\nThe board exposes three PCIe x16 slots; the top slot "
        "runs at Gen5 x16, the middle at Gen4 x4, and the bottom shares lanes with the M.2.",
        encoding="utf-8",
    )
    sub = root / "Project Files"
    sub.mkdir(exist_ok=True)
    (sub / "rebuild.md").write_text(
        "# NAS Rebuild Procedure\n\nBack up the pool, reinstall the OS from the USB stick, "
        "restore the ZFS pool, then re-run the ansible playbook to restore services.",
        encoding="utf-8",
    )
    backups = root / "backups"
    backups.mkdir(exist_ok=True)
    (backups / "stale.md").write_text("# Stale backup copy — must never be indexed", encoding="utf-8")


@pytest.fixture
async def store():
    s = Store(DSN, embedding_dimensions=DIMS)
    await s.connect()
    yield s
    await s.close()


@pytest.fixture
async def project(store):
    name = f"T{uuid.uuid4().hex[:10]}"
    await store.ensure_project(name)
    yield name
    await store.drop_project(name)


@pytest.fixture
def core(store):
    return RetrievalCore(store, HashEmbedder(DIMS), OverlapReranker())


@pytest.fixture
def corpus(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    write_corpus(docs)
    return docs


# ---------- indexing ----------


async def test_index_project_from_scratch(core, project, corpus):
    summary = await core.index_project(project, corpus)
    assert summary["total_files"] == 3  # backups/ excluded
    assert summary["indexed"] == 3
    assert (summary["skipped"], summary["removed"], summary["errors"]) == (0, 0, [])

    docs = await core.store.list_documents(project)
    # set, not list: ORDER BY source is locale-collated (en_US sorts "pcie"
    # before "Project"), and ordering isn't what this test is proving
    assert {d.source for d in docs} == {"Project Files/rebuild.md", "pcie.md", "rocm.md"}
    assert await core.store.check_consistency(project) == []
    # The backups file is nowhere in the index
    assert all("stale" not in d.source for d in docs)


async def test_reindex_skips_unchanged_files(core, project, corpus):
    await core.index_project(project, corpus)
    summary = await core.index_project(project, corpus)
    assert summary["indexed"] == 0
    assert summary["skipped"] == 3  # mtime+size short-circuit — nothing re-parsed


async def test_reindex_picks_up_changed_file(core, project, corpus):
    await core.index_project(project, corpus)
    (corpus / "pcie.md").write_text(
        "# Motherboard Layout\n\nNow with four PCIe slots after the riser mod.",
        encoding="utf-8",
    )
    summary = await core.index_project(project, corpus)
    assert summary["indexed"] == 1 and summary["skipped"] == 2
    results = await core.search(project, "pcie slots", hybrid_alpha=0.0)
    assert "four PCIe slots" in results[0]["content"]


async def test_reindex_removes_vanished_files(core, project, corpus):
    await core.index_project(project, corpus)
    (corpus / "pcie.md").unlink()
    summary = await core.index_project(project, corpus)
    assert summary["removed"] == 1
    assert (await core.store.stats(project)).documents == 2


async def test_force_reindexes_everything(core, project, corpus):
    await core.index_project(project, corpus)
    summary = await core.index_project(project, corpus, force=True)
    assert summary["indexed"] == 3 and summary["skipped"] == 0
    # No duplicates: force replaced rows transactionally
    assert (await core.store.stats(project)).documents == 3


async def test_index_file_and_remove_file(core, project, corpus):
    await core.index_project(project, corpus)
    new = corpus / "new-note.md"
    new.write_text("# New Note\n\nfreshly added content about the build", encoding="utf-8")
    doc_id = await core.index_file(project, corpus, new)
    assert doc_id is not None
    assert (await core.store.stats(project)).documents == 4
    assert await core.remove_file(project, "new-note.md") is True
    assert (await core.store.stats(project)).documents == 3


async def test_imperfect_utf8_indexes_without_rewriting_source_bytes(core, project, corpus):
    """Accepted NUL/malformed text is searchable while its source stays exact."""
    target = corpus / "imperfect.md"
    raw = b"# Byte probe\n\nleft\x00 intact searchablemarker \xff right\n"
    target.write_bytes(raw)

    assert await core.index_file(project, corpus, target) is not None
    assert target.read_bytes() == raw

    results = await core.search(project, "searchablemarker", hybrid_alpha=0.0)
    assert any(result["source"] == "imperfect.md" for result in results)


# ---------- search (known-answer on the fixture corpus) ----------


async def test_known_answer_queries(core, project, corpus):
    await core.index_project(project, corpus)
    cases = {
        "rocm build": "rocm.md",
        "pcie slots": "pcie.md",
        "rebuild procedure": "Project Files/rebuild.md",
    }
    for query, expected_source in cases.items():
        for alpha in (0.0, 0.3, 1.0):  # lexical-only, hybrid, dense-only
            results = await core.search(project, query, hybrid_alpha=alpha)
            assert results, f"{query!r} (alpha={alpha}) returned nothing"
            assert results[0]["source"] == expected_source, (
                f"{query!r} (alpha={alpha}) top hit was {results[0]['source']}"
            )


async def test_search_result_shape(core, project, corpus):
    await core.index_project(project, corpus)
    results = await core.search(project, "rocm build")
    r = results[0]
    expected_keys = {
        "content", "source", "filename", "category", "chunk_index", "score",
        "raw_rrf_score", "reranker_score", "semantic_rank", "bm25_rank",
        "search_method", "keywords", "routed_by",
    }
    assert expected_keys <= set(r)
    assert r["filename"] == "rocm.md"
    assert r["category"] == "general"
    assert 0.0 <= r["score"] <= 1.0


async def test_adjacent_expansion_on_real_chunks(core, project, corpus):
    await core.index_project(project, corpus)
    # rocm.md chunks into multiple sections; a middle hit should expand
    results = await core.search(project, "rocm repository build script gfx1201", hybrid_alpha=0.0)
    top = results[0]
    if top.get("context_expanded"):
        assert "Prerequisites" in top["content"] or "ROCm Build" in top["content"]


# ---------- isolation (the M2 spot-check shape, on fixtures) ----------


async def test_cross_project_isolation(core, store, corpus, tmp_path):
    a = f"T{uuid.uuid4().hex[:10]}"
    b = f"T{uuid.uuid4().hex[:10]}"
    await store.ensure_project(a)
    await store.ensure_project(b)
    try:
        b_docs = tmp_path / "b_docs"
        b_docs.mkdir()
        (b_docs / "altea.md").write_text(
            "# Altea Aerospace Rebuild\n\nTodo list for the altea aerospace website rebuild.",
            encoding="utf-8",
        )
        await core.index_project(a, corpus)
        await core.index_project(b, b_docs)

        # A never sees B's doc, in any search mode
        for alpha in (0.0, 1.0):
            a_hits = await core.search(a, "altea aerospace", hybrid_alpha=alpha)
            assert all("altea" not in r["source"] for r in a_hits)
        # B never sees A's docs
        for alpha in (0.0, 1.0):
            b_hits = await core.search(b, "rocm build", hybrid_alpha=alpha)
            assert all(r["source"] == "altea.md" for r in b_hits)
    finally:
        await store.drop_project(a)
        await store.drop_project(b)


# ---------- 4.4 registered tier (end-to-end over real Postgres) ----------


SCRIPT = (
    "#!/usr/bin/env python3\n"
    "import argparse\n\n"
    "def build_worldbook(sections_dir):\n"
    "    '''Assemble the worldbook from --sections-dir.'''\n"
    "    return sorted(sections_dir.glob('*.md'))\n"
)


@pytest.fixture
def corpus_with_script(corpus):
    (corpus / "build_worldbook.py").write_text(SCRIPT, encoding="utf-8")
    return corpus


async def test_registered_file_is_indexed_with_no_chunks_or_vectors(
    core, project, corpus_with_script
):
    summary = await core.index_project(project, corpus_with_script)
    assert summary["total_files"] == 4  # the .py is collected, not ignored

    doc = await core.store.get_document(project, "build_worldbook.py")
    assert doc is not None and doc.tier == "registered"
    assert await core.store.chunk_count(project, "build_worldbook.py") == 0
    assert await core.store.first_chunk_embedding(project, "build_worldbook.py") is None
    assert await core.store.check_consistency(project) == []

    stats = await core.store.stats(project)
    assert (stats.embedded_documents, stats.registered_documents) == (3, 1)


async def test_registered_file_found_by_literal_string(core, project, corpus_with_script):
    await core.index_project(project, corpus_with_script)
    results = await core.search(project, "build_worldbook", max_results=5)
    sources = [r["source"] for r in results]
    assert "build_worldbook.py" in sources
    top = next(r for r in results if r["source"] == "build_worldbook.py")
    assert top["tier"] == "registered"
    assert top["semantic_searchable"] is False
    assert "build_worldbook" in top["content"]


async def test_registered_file_absent_from_semantic_only_search(
    core, project, corpus_with_script
):
    await core.index_project(project, corpus_with_script)
    results = await core.search(project, "build_worldbook", max_results=5, hybrid_alpha=1.0)
    assert all(r["source"] != "build_worldbook.py" for r in results)


async def test_registered_file_excluded_when_include_registered_false(
    core, project, corpus_with_script
):
    await core.index_project(project, corpus_with_script)
    results = await core.search(
        project, "build_worldbook", max_results=5, include_registered=False
    )
    assert all(r["source"] != "build_worldbook.py" for r in results)


async def test_registered_reindex_never_calls_the_embedder(core, project, corpus_with_script):
    """'Genuinely skip, not embed-and-discard': reindex cost for this tier is
    filesystem-only."""
    await core.index_project(project, corpus_with_script)

    class ExplodingEmbedder:
        def embed(self, texts):
            raise AssertionError("the registered tier must never be embedded")

    core.embedder = ExplodingEmbedder()
    (corpus_with_script / "build_worldbook.py").write_text(
        SCRIPT + "\n# touched\n", encoding="utf-8"
    )
    doc_id, chunks = await core.index_file(
        project, corpus_with_script, corpus_with_script / "build_worldbook.py"
    )
    assert chunks == 0
    assert await core.store.chunk_count(project, "build_worldbook.py") == 0


async def test_move_from_embedded_to_registered_drops_chunks_and_vectors(
    core, project, corpus
):
    await core.index_project(project, corpus)
    assert await core.store.chunk_count(project, "rocm.md") > 0

    (corpus / "rocm.md").rename(corpus / "rocm.py")
    doc_id, chunks = await core.move_file(project, corpus, "rocm.md", "rocm.py")

    assert chunks == 0
    assert await core.store.get_document(project, "rocm.md") is None
    moved = await core.store.get_document(project, "rocm.py")
    assert moved is not None and moved.tier == "registered"
    assert await core.store.chunk_count(project, "rocm.py") == 0
    assert await core.store.first_chunk_embedding(project, "rocm.py") is None
    assert await core.store.check_consistency(project) == []


async def test_move_from_registered_to_embedded_creates_chunks_and_vectors(
    core, project, corpus_with_script
):
    await core.index_project(project, corpus_with_script)
    assert await core.store.chunk_count(project, "build_worldbook.py") == 0

    (corpus_with_script / "build_worldbook.py").rename(corpus_with_script / "build.md")
    doc_id, chunks = await core.move_file(
        project, corpus_with_script, "build_worldbook.py", "build.md"
    )

    assert chunks > 0
    moved = await core.store.get_document(project, "build.md")
    assert moved is not None and moved.tier == "embedded"
    assert await core.store.first_chunk_embedding(project, "build.md") is not None
    assert await core.store.check_consistency(project) == []


async def test_reindex_retiers_when_policy_changes(core, project, corpus_with_script):
    """The 4.3 -> 4.4 upgrade shape: a file whose bytes never change but whose
    tier does must be rewritten, not skipped by the mtime/size short-circuit."""
    embed_py = ExtensionPolicy.build([".md", ".py"], [".sh"])
    core.set_policy(project, embed_py)
    await core.index_project(project, corpus_with_script)
    assert await core.store.chunk_count(project, "build_worldbook.py") > 0

    core.set_policy(project, ExtensionPolicy.build([".md"], [".py"]))
    summary = await core.index_project(project, corpus_with_script)

    assert summary["indexed"] == 1  # the .py alone was rewritten
    doc = await core.store.get_document(project, "build_worldbook.py")
    assert doc.tier == "registered"
    assert await core.store.chunk_count(project, "build_worldbook.py") == 0
    assert await core.store.check_consistency(project) == []


async def test_per_project_policy_scoping(core, store, corpus_with_script, tmp_path):
    """One connector registering .py must not make another collect it."""
    prose = f"T{uuid.uuid4().hex[:10]}"
    scripts = f"T{uuid.uuid4().hex[:10]}"
    await store.ensure_project(prose)
    await store.ensure_project(scripts)
    try:
        core.set_policy(prose, ExtensionPolicy.build([".md"], []))
        core.set_policy(scripts, ExtensionPolicy.build([".md"], [".py"]))

        await core.index_project(prose, corpus_with_script)
        await core.index_project(scripts, corpus_with_script)

        prose_sources = {d.source for d in await store.list_documents(prose)}
        script_sources = {d.source for d in await store.list_documents(scripts)}
        assert "build_worldbook.py" not in prose_sources
        assert "build_worldbook.py" in script_sources
    finally:
        await store.drop_project(prose)
        await store.drop_project(scripts)
