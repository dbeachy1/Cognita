"""Effective project policy stays authoritative across indexing and search."""

from __future__ import annotations

from cognita.books.config import FolderRule
from cognita.books.policy import EffectiveIndexPolicy
from cognita.retrieval import RetrievalCore
from cognita.store import SourceInfo
from retrieval_fakes import HashEmbedder
from test_retrieval import StubStore, hit
from test_retrieval_reconciliation import ReconcileStore


def _effective(*rules: tuple[str, bool]) -> EffectiveIndexPolicy:
    return EffectiveIndexPolicy(
        [FolderRule(path=path, indexed=indexed) for path, indexed in rules]
    )


class PolicyStore(StubStore):
    async def list_sources(self, _project):
        hits = [*self.dense, *self.lexical, *self.registered]
        return {
            hit.source: SourceInfo(
                hit.doc_id, "0" * 64, 0, 0,
                "registered" if hit.source.endswith(".py") else "embedded", "general",
            )
            for hit in hits
        }

    async def _filtered(self, hits, limit, include_sources=None, include_doc_ids=None):
        return [
            item for item in hits
            if (include_sources is None or item.source in include_sources)
            and (include_doc_ids is None or item.doc_id in include_doc_ids)
        ][:limit]

    async def dense_search(self, _project, _vector, limit, _category=None, **kwargs):
        return await self._filtered(self.dense, limit, **kwargs)

    async def lexical_search(self, _project, _query, limit, _category=None, **kwargs):
        return await self._filtered(self.lexical, limit, **kwargs)

    async def registered_lexical_search(self, _project, _query, limit, _category=None, **kwargs):
        return await self._filtered(self.registered, limit, **kwargs)


def test_watcher_retires_excluded_rows_but_indexes_visible_siblings(tmp_path):
    store = ReconcileStore()
    core = RetrievalCore(store, HashEmbedder())
    policy = _effective(("excluded", False))
    core.set_effective_index_policy_provider(lambda _project: policy)
    excluded = tmp_path / "excluded" / "secret.md"
    excluded.parent.mkdir()
    excluded.write_text("distinctive excluded phrase", encoding="utf-8")
    visible = tmp_path / "visible.md"
    visible.write_text("distinctive visible phrase", encoding="utf-8")

    store.sources["excluded/secret.md"] = SourceInfo(
        "old", "0" * 64, 0, 0, "embedded", "general"
    )
    result = run(core.reconcile_paths("P", tmp_path, ["."]))

    assert result["removed"] == 1
    assert store.sources.keys() == {"visible.md"}
    assert "excluded/secret.md" in store.deleted


def test_single_file_index_removes_stale_row_without_re_admitting_folder_exclusion(tmp_path):
    store = ReconcileStore()
    core = RetrievalCore(store, HashEmbedder())
    core.set_effective_index_policy_provider(
        lambda _project: _effective(("excluded", False))
    )
    target = tmp_path / "excluded" / "note.md"
    target.parent.mkdir()
    target.write_text("new bytes remain available to exact readers", encoding="utf-8")
    store.sources["excluded/note.md"] = SourceInfo(
        "old", "0" * 64, 0, 0, "embedded", "general"
    )

    outcome = run(core.index_file("P", tmp_path, target))

    assert outcome is not None and not outcome.indexed
    assert outcome.exclusion_reason == "folder_exclusion"
    assert store.sources == {}
    assert store.replacements == []


def test_search_filters_live_policy_from_fresh_and_cached_results():
    excluded = hit("secret_0", "private text", source="private/note.md")
    visible = hit("public_0", "ordinary text", source="public/note.md")
    store = PolicyStore(dense=[excluded, visible], lexical=[excluded, visible])
    core = RetrievalCore(store, HashEmbedder())
    current = {"value": _effective(("private", False))}
    core.set_effective_index_policy_provider(lambda _project: current["value"])

    first = run(core.search("P", "text", max_results=5))
    assert [item["source"] for item in first] == ["public/note.md"]

    # A policy revision with a different admitted source set cannot hit the
    # old cache key; the formerly hidden source becomes searchable immediately.
    current["value"] = _effective(("public", False), ("private", True))
    assert [item["source"] for item in run(core.search("P", "text", max_results=5))] == [
        "private/note.md"
    ]


def test_excluded_high_ranked_rows_are_filtered_before_sql_leg_limit():
    ranked = [
        hit(f"private-{index}_0", "private phrase", source=f"private/{index}.md")
        for index in range(3)
    ] + [hit("public_0", "public phrase", source="public/note.md")]
    registered = [
        hit(f"private-code-{index}_0", "private phrase", source=f"private/{index}.py")
        for index in range(3)
    ] + [hit("public-code_0", "public phrase", source="public/script.py")]
    store = PolicyStore(dense=ranked, lexical=ranked, registered=registered)
    # Every source remains a SQL candidate; policy filtering must be passed
    # down before each leg's candidate LIMIT.
    core = RetrievalCore(store, HashEmbedder())
    core.set_effective_index_policy_provider(
        lambda _project: _effective(("private", False))
    )

    results = run(core.search("P", "phrase", max_results=1, hybrid_alpha=0.5))

    assert [item["source"] for item in results] == ["public/note.md"]


def test_policy_is_rechecked_after_reranking_before_results_are_published():
    excluded = hit("private_0", "private phrase", source="private/note.md")

    class ExcludingReranker:
        def rerank(self, _query, documents):
            current["value"] = _effective(("private", False))
            return [1.0] * len(documents)

        def state(self):
            return "available"

    store = PolicyStore(dense=[excluded], lexical=[excluded])
    current = {"value": _effective()}
    core = RetrievalCore(store, HashEmbedder(), reranker=ExcludingReranker())
    core.set_effective_index_policy_provider(lambda _project: current["value"])

    assert run(core.search("P", "phrase", max_results=1)) == []


def run(awaitable):
    import asyncio

    return asyncio.run(awaitable)
