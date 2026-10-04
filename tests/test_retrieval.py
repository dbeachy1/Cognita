"""Unit tests for cognita.retrieval — the fusion/rerank/MMR/expansion pipeline
against a stub store (no PostgreSQL, no models)."""

import pytest
from retrieval_fakes import BrokenReranker, HashEmbedder, OverlapReranker

from cognita.retrieval import RetrievalCore, _apply_mmr
from cognita.store import ChunkHit


def hit(chunk_id, content, source="doc.md", chunk_index=0, category="general"):
    return ChunkHit(
        chunk_id=chunk_id,
        doc_id=chunk_id.rsplit("_", 1)[0],
        chunk_index=chunk_index,
        content=content,
        section=None,
        source=source,
        category=category,
        keywords=[],
        score=0.1,
    )


class StubStore:
    """Canned search legs; records the filters it was called with."""

    def __init__(self, dense=(), lexical=(), adjacent=None, registered=()):
        self.dense = list(dense)
        self.lexical = list(lexical)
        self.registered = list(registered)
        self.adjacent = adjacent or {}
        self.calls = []

    async def dense_search(self, project, embedding, limit, category=None):
        self.calls.append(("dense", project, limit, category))
        return self.dense

    async def lexical_search(self, project, query, limit, category=None):
        self.calls.append(("lexical", project, limit, category))
        return self.lexical

    async def registered_lexical_search(self, project, query, limit, category=None):
        self.calls.append(("registered", project, limit, category))
        return self.registered

    async def adjacent_chunks(self, project, wanted):
        self.calls.append(("adjacent", project, tuple(wanted)))
        return self.adjacent


class FixedReranker:
    def __init__(self, scores):
        self.scores = scores

    def rerank(self, query, texts):
        return [self.scores[text] for text in texts]


def make_core(store, reranker=None, **kwargs):
    return RetrievalCore(store, HashEmbedder(), reranker, **kwargs)


# ---------- RRF fusion ----------


def test_rrf_fuse_math():
    a, b, c = hit("d_0", "alpha"), hit("d_1", "beta", chunk_index=1), hit("d_2", "gamma", chunk_index=2)
    fused = RetrievalCore._rrf_fuse([a, b], [b, c], alpha=0.5)
    scores = {e["hit"].chunk_id: e["rrf_score"] for e in fused}
    assert scores["d_0"] == pytest.approx(0.5 / 61 + 0.5 / 1060)
    assert scores["d_1"] == pytest.approx(0.5 / 62 + 0.5 / 61)  # both legs
    assert scores["d_2"] == pytest.approx(0.5 / 1060 + 0.5 / 62)
    assert fused[0]["hit"].chunk_id == "d_1"  # hybrid hit wins


def test_rrf_alpha_extremes():
    a, b = hit("d_0", "alpha"), hit("d_1", "beta", chunk_index=1)
    dense_only = RetrievalCore._rrf_fuse([a], [b], alpha=1.0)
    assert dense_only[0]["hit"].chunk_id == "d_0"
    lexical_only = RetrievalCore._rrf_fuse([a], [b], alpha=0.0)
    assert lexical_only[0]["hit"].chunk_id == "d_1"


# ---------- search pipeline ----------


async def test_search_shapes_and_methods():
    a = hit("dA_0", "alpha content", source="sub/a.md")
    b = hit("dB_0", "beta content", source="b.md")
    c = hit("dC_0", "gamma content", source="c.md")
    store = StubStore(dense=[a, b], lexical=[b, c])
    core = make_core(store)
    results = await core.search("P", "beta", max_results=3)

    # Default alpha=0.3 weights the lexical leg heavier: b (both legs) wins,
    # then c (lexical rank 2) over a (semantic rank 1).
    assert [r["source"] for r in results] == ["b.md", "c.md", "sub/a.md"]
    top = results[0]
    assert top["search_method"] == "hybrid"
    assert top["semantic_rank"] == 2 and top["bm25_rank"] == 1
    assert top["score"] == 1.0  # best normalized score
    assert top["filename"] == "b.md"
    assert results[1]["search_method"] == "keyword"
    assert results[2]["search_method"] == "semantic"
    assert all(r["routed_by"] == "none" for r in results)
    assert all("_doc_id" not in r for r in results)
    assert all(r["reranker_score"] is None for r in results)  # no reranker configured


async def test_empty_query_returns_nothing():
    store = StubStore()
    core = make_core(store)
    assert await core.search("P", "   ") == []
    assert store.calls == []


async def test_index_scheduler_seam_bypasses_per_job_gpu_lease():
    class RecordingScheduler:
        def __init__(self):
            self.calls = []

        def open_job(self, project, kind, estimated_chunks):
            job = type("Job", (), {"state": "open"})()
            self.calls.append(("open", project, kind, estimated_chunks, job))
            return job

        async def embed(self, job, texts):
            self.calls.append(("embed", list(texts)))
            return [[float(len(text))] for text in texts]

        async def close_job(self, job, *, outcome):
            self.calls.append(("close", outcome))

    scheduler = RecordingScheduler()
    core = make_core(StubStore(), scheduler=scheduler)

    async def no_legacy_gpu(*args, **kwargs):
        raise AssertionError("scheduler-backed indexing must not acquire a per-job GPU lease")

    core._embed_on_gpu = no_legacy_gpu
    assert await core._scheduler_embed("project", ["one", "two"], "walk") == [[3.0], [3.0]]
    assert [call[0] for call in scheduler.calls] == ["open", "embed", "close"]
    assert scheduler.calls[-1][1] == "completed"


async def test_alpha_zero_skips_embedding_entirely():
    class ExplodingEmbedder:
        def embed(self, texts):
            raise AssertionError("embedder must not be called for alpha=0")

    store = StubStore(lexical=[hit("d_0", "x")])
    core = RetrievalCore(store, ExplodingEmbedder())
    results = await core.search("P", "x", hybrid_alpha=0.0)
    assert len(results) == 1
    # Both keyword legs run (registered documents are keyword-searchable);
    # the dense leg — and therefore the embedder — is never touched.
    assert [c[0] for c in store.calls if c[0] != "adjacent"] == ["lexical", "registered"]


async def test_reranker_reorders_results():
    a = hit("dA_0", "nothing relevant here")
    b = hit("dB_0", "rocm build instructions")
    store = StubStore(dense=[a, b])  # dense leg puts the wrong one first
    core = make_core(store, reranker=OverlapReranker())
    results = await core.search("P", "rocm build", hybrid_alpha=1.0)
    assert results[0]["source"] == "doc.md"
    assert results[0]["content"] == "rocm build instructions"
    assert results[0]["reranker_score"] is not None


async def test_broken_reranker_falls_back_to_rrf_order():
    a, b = hit("dA_0", "first"), hit("dB_0", "second")
    store = StubStore(dense=[a, b])
    core = make_core(store, reranker=BrokenReranker())
    results = await core.search("P", "anything", hybrid_alpha=1.0)
    assert [r["content"] for r in results] == ["first", "second"]
    assert all(r["reranker_score"] is None for r in results)


async def test_category_filter_and_keyword_routing():
    store = StubStore(dense=[hit("d_0", "x")], lexical=[])
    core = make_core(store, keyword_routes={"hardware": ["pcie", "gpu"]})
    await core.search("P", "how many pcie slots", category=None)
    # Routed: both legs filtered to the routed category, results flagged
    assert all(call[3] == "hardware" for call in store.calls if call[0] in ("dense", "lexical"))

    store.calls.clear()
    await core.search("P", "how many pcie slots", category="explicit")
    # An explicit category disables routing
    assert all(call[3] == "explicit" for call in store.calls if call[0] in ("dense", "lexical"))


async def test_routed_by_field_set():
    store = StubStore(dense=[hit("d_0", "pcie layout")])
    core = make_core(store, keyword_routes={"hardware": ["pcie"]})
    results = await core.search("P", "pcie slots", hybrid_alpha=1.0)
    assert results[0]["routed_by"] == "hardware"


async def test_adjacent_expansion_merges_neighbors():
    center = hit("dA_1", "middle chunk", chunk_index=1)
    store = StubStore(
        dense=[center],
        adjacent={("dA", 0): "before chunk", ("dA", 2): "after chunk"},
    )
    core = make_core(store)
    results = await core.search("P", "middle", hybrid_alpha=1.0)
    assert results[0]["content"] == "before chunk\n\nmiddle chunk\n\nafter chunk"
    assert results[0]["context_expanded"] is True
    # chunk 0 has no negative neighbor request
    adjacent_call = next(c for c in store.calls if c[0] == "adjacent")
    assert ("dA", -1) not in adjacent_call[2]


async def test_mixed_expanded_results_are_sorted_by_normalized_score():
    """MMR can select a lower score before a higher score; the published page
    must restore descending normalized relevance after expansion is complete.
    """
    expanded = hit("expanded_1", "shared expanded", source="expanded.md", chunk_index=1)
    higher_unexpanded = hit("higher_0", "shared higher", source="higher.md")
    lower_unexpanded = hit("lower_0", "unique", source="lower.md")
    trailing = hit("trailing_0", "shared unique", source="trailing.md")
    store = StubStore(
        dense=[expanded, higher_unexpanded, lower_unexpanded, trailing],
        adjacent={("expanded", 0): "expanded context"},
    )
    core = make_core(
        store,
        reranker=FixedReranker({
            "shared expanded": 0.9,
            "shared higher": 0.4,
            "unique": 0.39,
            "shared unique": 0.1,
        }),
    )

    results = await core.search("P", "query", max_results=3, hybrid_alpha=1.0)

    assert any(result.get("context_expanded") for result in results)
    scores = [result["score"] for result in results]
    assert scores == sorted(scores, reverse=True)
    assert [result["source"] for result in results] == [
        "expanded.md", "higher.md", "lower.md"
    ]
    assert results[0]["content"].startswith("expanded context")
    assert results[1]["content"] == "shared higher"


# ---------- query cache (3.x parity, added in M5) ----------


async def test_search_results_are_cached_and_mutation_safe():
    store = StubStore(dense=[hit("d_0", "cached content")])
    core = make_core(store)
    first = await core.search("P", "cached", hybrid_alpha=1.0)
    calls_after_first = len(store.calls)
    second = await core.search("P", "cached", hybrid_alpha=1.0)
    assert len(store.calls) == calls_after_first  # served from cache, no store hit
    assert second == first
    # tool-layer mutation of a returned result must not poison the cache
    second[0]["content"] = "MUTATED"
    third = await core.search("P", "cached", hybrid_alpha=1.0)
    assert third[0]["content"] == "cached content"
    assert core.query_cache("P").stats()["hits"] == 2


async def test_different_params_miss_the_cache():
    store = StubStore(dense=[hit("d_0", "x")])
    core = make_core(store)
    await core.search("P", "q", hybrid_alpha=1.0, max_results=5)
    n = len(store.calls)
    await core.search("P", "q", hybrid_alpha=1.0, max_results=3)  # different key
    assert len(store.calls) > n


async def test_cache_invalidated_by_writes(tmp_path):
    store = StubStore(dense=[hit("d_0", "x")])
    core = make_core(store)
    await core.search("P", "q", hybrid_alpha=1.0)
    core.query_cache("P").put(("sentinel",), [])
    # remove_file is the cheapest write path to exercise without a real store
    class Deleting:
        async def delete_document(self, project, source):
            return True
    core.store.delete_document = Deleting().delete_document
    await core.remove_file("P", "gone.md")
    assert core.query_cache("P").get(("sentinel",)) is None  # invalidated


def test_query_cache_ttl_and_lru():
    from cognita.retrieval import QueryCache

    cache = QueryCache(max_size=2, ttl_s=0.0)  # instant expiry
    cache.put(("a",), [{"x": 1}])
    assert cache.get(("a",)) is None  # expired immediately
    cache = QueryCache(max_size=2, ttl_s=60)
    cache.put(("a",), [])
    cache.put(("b",), [])
    cache.put(("c",), [])  # evicts the oldest
    assert cache.get(("a",)) is None
    assert cache.get(("b",)) == [] and cache.get(("c",)) == []


# ---------- MMR ----------


def test_mmr_diversifies_near_duplicates():
    dup1 = {"hit": hit("a_0", "the exact same words repeated"), "rrf_score": 0.9}
    dup2 = {"hit": hit("b_0", "the exact same words repeated"), "rrf_score": 0.89}
    other = {"hit": hit("c_0", "completely different topic entirely"), "rrf_score": 0.5}
    picked = _apply_mmr([dup1, dup2, other], top_k=2)
    assert [p["hit"].chunk_id for p in picked] == ["a_0", "c_0"]


def test_mmr_noop_when_under_top_k():
    one = {"hit": hit("a_0", "x"), "rrf_score": 1.0}
    assert _apply_mmr([one], top_k=5) == [one]


# ---------- 4.4 registered tier ----------


def reg_hit(content, source="build.py", score=0.9):
    h = hit("regdoc_r0", content, source=source)
    h.score = score
    return h


async def test_registered_documents_join_the_keyword_leg():
    store = StubStore(
        lexical=[hit("d_0", "prose about builds")],
        registered=[reg_hit("def build_worldbook():\n    return 1")],
    )
    core = RetrievalCore(store, HashEmbedder())
    results = await core.search("P", "build_worldbook", hybrid_alpha=0.3)

    by_source = {r["source"]: r for r in results}
    assert "build.py" in by_source
    reg = by_source["build.py"]
    assert reg["tier"] == "registered"
    assert reg["semantic_searchable"] is False
    assert reg["search_method"] == "keyword"  # keyword leg only, never semantic
    # The embedded document keeps the opposite markers.
    assert by_source["doc.md"]["tier"] == "embedded"
    assert by_source["doc.md"]["semantic_searchable"] is True


async def test_registered_documents_absent_at_alpha_one():
    """Semantic-only means embedded-only: the keyword leg is off entirely."""
    store = StubStore(
        dense=[hit("d_0", "prose about builds")],
        registered=[reg_hit("def build_worldbook(): pass")],
    )
    core = RetrievalCore(store, HashEmbedder())
    results = await core.search("P", "build_worldbook", hybrid_alpha=1.0)

    assert [r["source"] for r in results] == ["doc.md"]
    assert ("registered", "P", 15, None) not in store.calls


async def test_include_registered_false_drops_the_tier():
    store = StubStore(
        lexical=[hit("d_0", "prose")],
        registered=[reg_hit("def build_worldbook(): pass")],
    )
    core = RetrievalCore(store, HashEmbedder())
    results = await core.search("P", "build", hybrid_alpha=0.3, include_registered=False)

    assert all(r["source"] != "build.py" for r in results)
    assert not [c for c in store.calls if c[0] == "registered"]


async def test_registered_hits_are_not_expanded_with_neighbors():
    store = StubStore(
        registered=[reg_hit("def build_worldbook(): pass")],
        adjacent={("regdoc", 1): "SHOULD NOT APPEAR"},
    )
    core = RetrievalCore(store, HashEmbedder())
    results = await core.search("P", "build_worldbook", hybrid_alpha=0.0)

    assert len(results) == 1
    assert "SHOULD NOT APPEAR" not in results[0]["content"]
    assert not results[0].get("context_expanded")


async def test_registered_hit_content_is_excerpted_not_whole_file():
    big = "\n".join(f"line {i} filler" for i in range(400)) + "\nNEEDLE here\n"
    store = StubStore(registered=[reg_hit(big)])
    core = RetrievalCore(store, HashEmbedder(), chunk_size=200)
    results = await core.search("P", "NEEDLE", hybrid_alpha=0.0)

    content = results[0]["content"]
    assert "NEEDLE" in content
    assert len(content) < len(big) / 2  # narrowed to the match neighborhood


async def test_cache_key_separates_include_registered():
    store = StubStore(lexical=[hit("d_0", "prose")], registered=[reg_hit("script")])
    core = RetrievalCore(store, HashEmbedder())
    with_reg = await core.search("P", "x", hybrid_alpha=0.0)
    without = await core.search("P", "x", hybrid_alpha=0.0, include_registered=False)
    assert len(with_reg) != len(without)
