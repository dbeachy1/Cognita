"""Effective project policy stays authoritative across indexing and search."""

from __future__ import annotations

import asyncio

from cognita.books.config import FolderRule
from cognita.books.policy import EffectiveIndexPolicy
from cognita.books.config import BookLayout
from cognita.books.state import IndexedRoleProvenance, ProjectState
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


def test_real_book_layout_fails_closed_until_durable_source_provenance_matches(tmp_path):
    import hashlib

    from test_book_config import _layout

    source = "Chapters/1/chapter.docx"
    target = tmp_path / source
    target.parent.mkdir(parents=True)
    target.write_bytes(b"current source bytes")
    raw_sha = hashlib.sha256(target.read_bytes()).hexdigest()
    extracted_sha = "a" * 64
    doc = hit("chapter-current", "chapter phrase", source=source)
    store = PolicyStore(dense=[doc], lexical=[doc])
    store.source_records = {
        source: SourceInfo("chapter-current", extracted_sha, 0, 0, "embedded", "general")
    }
    store.list_sources = lambda _project: asyncio.sleep(0, result=store.source_records)
    # Use an actual validated layout and role-aware policy, not a policy stub.
    layout = BookLayout.model_validate(_layout(), strict=True)
    effective = EffectiveIndexPolicy([], book_layout=layout)
    core = RetrievalCore(store, HashEmbedder())
    core.set_effective_index_policy_provider(lambda _project: effective)

    # Without the service's durable provenance join, role membership alone
    # must not expose the chapter.
    assert run(core.search("P", "phrase", max_results=5)) == []

    state = ProjectState.initialize(tmp_path)
    record = IndexedRoleProvenance(
        source_path=source, doc_id="chapter-current", extracted_sha256=extracted_sha,
        raw_sha256=raw_sha, extraction_version="cognita-docx-v1",
        role="chapter_working", chapter_id="ch1", layout_sha256="c" * 64,
        chapter_state_sha256="d" * 64, annotations_sha256=None,
        approval_source_raw_sha256=raw_sha,
        approval_prose_projection_sha256="e" * 64,
        approval_projection_version="cognita-docx-v1", summary_raw_sha256=None,
        summary_source_raw_sha256=None,
        summary_source_prose_projection_sha256=None,
    )

    def admitted(_project, sources, _retrieval_profile):
        allowed = {}
        for info in sources:
            evidence = state.indexed_role_provenance(info.source)
            if (evidence is not None and evidence.doc_id == info.doc_id
                    and evidence.extracted_sha256 == info.content_hash
                    and evidence.raw_sha256 == hashlib.sha256(
                        (tmp_path / info.source).read_bytes()
                    ).hexdigest()):
                allowed[evidence.doc_id] = {
                    "source_path": evidence.source_path,
                    "role": evidence.role,
                    "chapter_id": evidence.chapter_id,
                    "editorial_status": "approved",
                    "summary_freshness": "not_applicable",
                    "provenance": evidence,
                }
        return allowed

    core.set_book_index_admission_provider(admitted)
    assert run(core.search("P", "phrase", max_results=5)) == []  # no record yet

    # A persisted record with the current source SHA but a mismatched extracted
    # hash/doc id still cannot authorize the indexed row.
    state.put_indexed_role_provenance(record)
    store.source_records = {
        source: SourceInfo("obsolete-doc", "b" * 64, 0, 0, "embedded", "general")
    }
    assert run(core.search("P", "phrase", max_results=5)) == []

    store.source_records = {
        source: SourceInfo("chapter-current", extracted_sha, 0, 0, "embedded", "general")
    }
    assert [item["source"] for item in run(core.search("P", "phrase", max_results=5))] == [source]

    # An external Word save invalidates the raw-source join immediately, even
    # while the old extracted row and provenance record remain in the stores.
    target.write_bytes(b"externally saved newer bytes")
    assert run(core.search("P", "phrase", max_results=5)) == []


def test_retrieval_profiles_are_forwarded_and_keep_roles_separate():
    from test_book_config import _layout

    chapter = hit("draft_0", "shared phrase", source="Chapters/1/chapter.docx")
    reference = hit("reference_0", "shared phrase", source="Project Files/ref.docx")
    guide = hit("guide_0", "shared phrase", source="Project Files/guide.md")
    workflow = hit("workflow_0", "shared phrase", source="Project Files/workflow.docx")
    records = [chapter, reference, guide, workflow]
    store = PolicyStore(dense=records, lexical=records)
    layout = BookLayout.model_validate(_layout(), strict=True)
    effective = EffectiveIndexPolicy([], book_layout=layout)
    core = RetrievalCore(store, HashEmbedder())
    core.set_effective_index_policy_provider(lambda _project: effective)
    roles = {
        chapter.doc_id: ("chapter_working", "draft"),
        reference.doc_id: ("reference", None),
        guide.doc_id: ("instructions", None),
        workflow.doc_id: ("workflow", None),
    }
    seen_profiles = []
    provenance = {
        item.doc_id: IndexedRoleProvenance(
            source_path=item.source, doc_id=item.doc_id,
            extracted_sha256="0" * 64, raw_sha256="b" * 64,
            extraction_version="cognita-docx-v1", role=roles[item.doc_id][0],
            chapter_id="ch1" if item is chapter else None,
            layout_sha256="c" * 64, chapter_state_sha256=None,
            annotations_sha256=None, approval_source_raw_sha256=None,
            approval_prose_projection_sha256=None,
            approval_projection_version=None, summary_raw_sha256=None,
            summary_source_raw_sha256=None,
            summary_source_prose_projection_sha256=None,
        )
        for item in records
    }

    def admission(_project, sources, profile):
        normalized = profile or "canon"
        seen_profiles.append(normalized)
        accepted = {}
        for source in sources:
            role, editorial_status = roles[source.doc_id]
            if normalized == "editing" and role in {
                "chapter_working", "reference", "chapter_summary",
            }:
                pass
            elif normalized == "canon" and role == "reference":
                pass
            elif normalized == "instructions" and role == "instructions":
                pass
            elif normalized == "workflow" and role == "workflow":
                pass
            else:
                continue
            record = provenance[source.doc_id]
            accepted[source.doc_id] = {
                "source_path": source.source,
                "role": role,
                "chapter_id": record.chapter_id,
                "editorial_status": editorial_status,
                "summary_freshness": "not_applicable",
                "provenance": record,
            }
        return accepted

    core.set_book_index_admission_provider(admission)
    editing = run(core.search("P", "phrase", max_results=10, retrieval_profile="editing"))
    canon = run(core.search("P", "phrase", max_results=10, retrieval_profile="canon"))
    instructions = run(core.search("P", "phrase", max_results=10, retrieval_profile="instructions"))
    workflow_results = run(core.search("P", "phrase", max_results=10, retrieval_profile="workflow"))

    assert {item["source"] for item in editing} == {chapter.source, reference.source}
    assert {item["source"] for item in canon} == {reference.source}
    assert {item["source"] for item in instructions} == {guide.source}
    assert {item["source"] for item in workflow_results} == {workflow.source}
    assert all(profile in seen_profiles for profile in ("editing", "canon", "instructions", "workflow"))


def run(awaitable):
    import asyncio

    return asyncio.run(awaitable)
