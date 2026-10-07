"""Focused tests for the 11.4 targeted retrieval reconciliation seam."""

from __future__ import annotations

import logging
import os
import time

import pytest

from cognita.retrieval import RetrievalCore
from cognita.store import SourceInfo
from retrieval_fakes import HashEmbedder


class ReconcileStore:
    """Small store double that retains document metadata and chunk writes."""

    def __init__(self):
        self.sources: dict[str, SourceInfo] = {}
        self.docs: dict[str, object] = {}
        self.touched: list[str] = []
        self.deleted: list[str] = []
        self.replacements: list[str] = []

    async def list_sources(self, project):
        return dict(self.sources)

    async def touch_document(self, project, source, mtime, size):
        self.touched.append(source)
        old = self.sources[source]
        self.sources[source] = SourceInfo(
            old.doc_id, old.content_hash, mtime, size, old.tier, old.category
        )

    async def replace_document(self, project, doc, chunks):
        self.replacements.append(doc.source)
        self.docs[doc.source] = (doc, list(chunks))
        self.sources[doc.source] = SourceInfo(
            doc.doc_id, doc.content_hash, doc.file_mtime, doc.file_size,
            doc.tier, doc.category,
        )

    async def delete_document(self, project, source):
        existed = source in self.sources
        self.deleted.append(source)
        self.sources.pop(source, None)
        self.docs.pop(source, None)
        return existed


def core_for(store):
    return RetrievalCore(store, HashEmbedder())


async def test_targeted_add_update_delete_and_cache_invalidation(tmp_path):
    store = ReconcileStore()
    core = core_for(store)
    (tmp_path / "note.md").write_text("alpha beta", encoding="utf-8")

    first = await core.reconcile_paths("P", tmp_path, ["note.md"])
    assert first["indexed"] == 1
    assert "note.md" in store.sources
    cache = core.query_cache("P")
    cache.put(("q",), [{"source": "note.md"}])

    (tmp_path / "note.md").write_text("changed gamma", encoding="utf-8")
    updated = await core.reconcile_paths("P", tmp_path, ["note.md"])
    assert updated["indexed"] == 1
    assert updated["removed"] == 0
    assert cache.stats()["size"] == 0

    (tmp_path / "note.md").unlink()
    removed = await core.reconcile_paths("P", tmp_path, ["note.md"])
    assert removed["removed"] == 1
    assert store.sources == {}


async def test_unchanged_stat_skips_and_touched_hash_refreshes_metadata(tmp_path):
    store = ReconcileStore()
    core = core_for(store)
    path = tmp_path / "note.md"
    path.write_text("same content", encoding="utf-8")
    await core.reconcile_paths("P", tmp_path, ["note.md"])
    before_replacements = len(store.replacements)

    skipped = await core.reconcile_paths("P", tmp_path, ["note.md"])
    assert skipped["skipped"] == 1
    assert len(store.replacements) == before_replacements

    old = store.sources["note.md"]
    # Linux filesystems expose nanosecond mtimes, while the persisted
    # ``SourceInfo`` timestamp is microsecond precision.  A bare ``touch`` can
    # therefore land inside the reconciliation short-circuit tolerance.  Move
    # the mtime forward by a deterministic margin so this test exercises the
    # metadata-refresh path rather than filesystem timestamp granularity.
    old_mtime_ns = path.stat().st_mtime_ns
    new_mtime_ns = max(time.time_ns(), old_mtime_ns + 2_000_000)
    os.utime(path, ns=(new_mtime_ns, new_mtime_ns))
    refreshed = await core.reconcile_paths("P", tmp_path, ["note.md"])
    assert refreshed["metadata_refreshed"] == 1
    assert refreshed["indexed"] == 0
    assert store.touched == ["note.md"]
    assert store.sources["note.md"].content_hash == old.content_hash


async def test_directory_expansion_preserves_category_and_removes_vanished_descendants(tmp_path):
    store = ReconcileStore()
    core = core_for(store)
    folder = tmp_path / "folder"
    folder.mkdir()
    (folder / "one.md").write_text("one", encoding="utf-8")
    (folder / "two.md").write_text("two", encoding="utf-8")
    await core.reconcile_paths("P", tmp_path, ["folder"])
    store.sources["folder/one.md"] = SourceInfo(
        store.sources["folder/one.md"].doc_id,
        store.sources["folder/one.md"].content_hash,
        store.sources["folder/one.md"].file_mtime,
        store.sources["folder/one.md"].file_size,
        store.sources["folder/one.md"].tier,
        "explicit",
    )
    (folder / "two.md").unlink()
    result = await core.reconcile_paths("P", tmp_path, ["folder", "folder/one.md"])
    assert result["expanded_paths"] == 1
    assert result["removed"] == 1
    assert store.sources["folder/one.md"].category == "explicit"


async def test_empty_parse_removes_stale_document(tmp_path):
    store = ReconcileStore()
    core = core_for(store)
    path = tmp_path / "empty.md"
    path.write_text("content", encoding="utf-8")
    await core.reconcile_paths("P", tmp_path, ["empty.md"])
    path.write_text("", encoding="utf-8")
    result = await core.reconcile_paths("P", tmp_path, ["empty.md"])
    assert result["removed"] == 1
    assert "empty.md" not in store.sources


async def test_missing_root_fails_closed_and_keeps_index(tmp_path):
    store = ReconcileStore()
    core = core_for(store)
    path = tmp_path / "note.md"
    path.write_text("content", encoding="utf-8")
    await core.reconcile_paths("P", tmp_path, ["note.md"])
    identity = core.attach_root("P", tmp_path)
    path.unlink()
    tmp_path.rmdir()
    result = await core.reconcile_paths("P", tmp_path, ["note.md"], root_identity=identity)
    assert result["failed"] == 1
    assert result["removed"] == 0
    assert "note.md" in store.sources
    assert result["failures"][0]["retryable"] is True


async def test_root_identity_change_blocks_destructive_reconciliation(tmp_path):
    store = ReconcileStore()
    core = core_for(store)
    path = tmp_path / "note.md"
    path.write_text("content", encoding="utf-8")
    identity = core.attach_root("P", tmp_path)
    await core.reconcile_paths("P", tmp_path, ["note.md"], root_identity=identity)
    replacement = tmp_path.with_name(tmp_path.name + "-replacement")
    replacement.mkdir()
    path.unlink()
    tmp_path.rmdir()
    replacement.rename(tmp_path)
    result = await core.reconcile_paths("P", tmp_path, ["note.md"], root_identity=identity)
    assert result["removed"] == 0
    assert result["failed"] == 1
    assert "note.md" in store.sources


@pytest.mark.parametrize("change", ["source_callback", "root_identity"])
async def test_post_embedding_root_and_source_guards_preserve_previous_rows(tmp_path, change):
    import asyncio
    import threading

    documents = tmp_path / "documents"
    documents.mkdir()
    note = documents / "note.md"
    vanished = documents / "vanished.md"
    note.write_text("previous indexed note", encoding="utf-8")
    vanished.write_text("previous indexed vanished source", encoding="utf-8")
    store = ReconcileStore()
    core = core_for(store)
    identity = core.attach_root("P", documents)
    initial = await core.reconcile_paths("P", documents, ["."], root_identity=identity)
    assert initial["indexed"] == 2
    previous_sources = dict(store.sources)
    previous_docs = dict(store.docs)
    previous_replacements = list(store.replacements)
    note.write_text("new changed content awaiting publication", encoding="utf-8")
    vanished.unlink()  # Also exercise a pending deletion in the same batch.
    started = threading.Event()
    release = threading.Event()
    source_safe = True

    class SuspendedEmbedder(HashEmbedder):
        def embed(self, texts):
            assert texts == ["new changed content awaiting publication"]
            started.set()
            if not release.wait(timeout=5):
                raise TimeoutError("synthetic embedding barrier was not released")
            return super().embed(texts)

    core.embedder = SuspendedEmbedder()
    task = asyncio.create_task(core.reconcile_paths(
        "P", documents, ["."], root_identity=identity, source_is_safe=lambda: source_safe,
    ))
    try:
        assert await asyncio.to_thread(started.wait, 5), "reconciliation did not reach actual embedding"
        # Planning and the pre-embedding safety check already passed. Change the
        # existing authority while the real _index_parsed await is suspended.
        if change == "source_callback":
            source_safe = False
        else:
            documents.rename(tmp_path / "original-documents")
            documents.mkdir()
            (documents / "note.md").write_text("replacement root bytes", encoding="utf-8")
            assert core.capture_root_identity(documents) != identity
    finally:
        # The owned worker thread and reconciliation task are always released
        # and awaited, including failed assertions inside this synthetic race.
        release.set()
        result = await asyncio.wait_for(task, timeout=10)

    assert result["indexed"] == result["removed"] == 0
    note_failure = next(failure for failure in result["failures"] if failure["path"] == "note.md")
    assert note_failure["retryable"] is True
    expected_error = "source_unavailable" if change == "source_callback" else "documents root identity changed"
    assert expected_error in note_failure["error"]
    assert "note.md" in result["retryable_failures"]
    assert store.sources == previous_sources and store.docs == previous_docs
    assert store.replacements == previous_replacements
    assert store.deleted == [] and store.touched == []


async def test_path_escape_is_terminal_and_does_not_touch_store(tmp_path):
    store = ReconcileStore()
    result = await core_for(store).reconcile_paths("P", tmp_path, ["../outside.md"])
    assert result["failed"] == 1
    assert result["failures"][0]["retryable"] is False
    assert result["retryable_failures"] == []
    assert store.replacements == []


async def test_drive_relative_path_is_terminal_and_does_not_touch_store(tmp_path):
    store = ReconcileStore()
    result = await core_for(store).reconcile_paths("P", tmp_path, ["C:outside.md"])
    assert result["failed"] == 1
    assert result["failures"][0]["retryable"] is False
    assert result["retryable_failures"] == []
    assert store.replacements == []


async def test_invalid_windows_directory_marker_does_not_block_valid_paths(tmp_path):
    store = ReconcileStore()
    valid = tmp_path / "note.md"
    valid.write_text("valid same-batch document", encoding="utf-8")
    invalid = ".::TMPNAME:D:3387398%9918639760575770279:New folder"

    result = await core_for(store).reconcile_paths("P", tmp_path, [invalid, "note.md"])

    assert result["indexed"] == 1
    assert result["failed"] == 1
    assert result["failures"] == [{
        "path": invalid,
        "error": f"dirty path has a drive-relative anchor: {invalid!r}",
        "retryable": False,
    }]
    assert result["retryable_failures"] == []
    assert "note.md" in store.sources


@pytest.mark.parametrize("failure", ["source_unavailable", "root_unavailable"])
async def test_mixed_invalid_paths_and_safety_failures_retain_complete_count(tmp_path, failure):
    store = ReconcileStore()
    core = core_for(store)
    documents = tmp_path / "documents"
    documents.mkdir()
    kwargs = {}
    if failure == "source_unavailable":
        kwargs["source_is_safe"] = lambda: False
    else:
        documents.rmdir()

    result = await core.reconcile_paths(
        "P", documents, ["C:outside.md", "note.md"], **kwargs,
    )

    assert result["failed"] == len(result["failures"]) == 2
    assert [entry["retryable"] for entry in result["failures"]] == [False, True]
    assert result["retryable_failures"] == ["note.md"]
    assert store.replacements == store.deleted == store.touched == []


async def test_unreadable_dirty_subtree_preserves_existing_rows(tmp_path, monkeypatch):
    store = ReconcileStore()
    core = core_for(store)
    folder = tmp_path / "folder"
    folder.mkdir()
    path = folder / "known.md"
    path.write_text("committed content", encoding="utf-8")
    await core.reconcile_paths("P", tmp_path, ["folder"])

    def fail_walk(*_args, **_kwargs):
        raise PermissionError("transient subtree access failure")

    monkeypatch.setattr("cognita.retrieval.iter_document_files", fail_walk)
    result = await core.reconcile_paths("P", tmp_path, ["folder"])

    assert result["failed"] == 1
    assert result["retryable_failures"] == ["folder"]
    assert result["removed"] == 0
    assert "folder/known.md" in store.sources


async def test_existing_excluded_source_is_retired_by_directory_reconciliation(tmp_path):
    store = ReconcileStore()
    core = core_for(store)
    backups = tmp_path / "backups"
    backups.mkdir()
    path = backups / "stale.md"
    path.write_text("must not remain searchable", encoding="utf-8")
    store.sources["backups/stale.md"] = SourceInfo(
        "stale", "old", path.stat().st_mtime, path.stat().st_size,
        "embedded", "general",
    )

    result = await core.reconcile_paths("P", tmp_path, ["."])

    assert result["removed"] == 1
    assert "backups/stale.md" not in store.sources


class RecordingEmbedder(HashEmbedder):
    """Records each batch as the real `Embedder.embed` does."""

    def embed(self, texts):
        from cognita.embed_telemetry import record_batch
        record_batch("cpu", len(texts), sum(map(len, texts)), 0.01)
        return super().embed(texts)


def _done_lines(caplog):
    return [r.getMessage() for r in caplog.records if r.getMessage().startswith("embed.done")]


async def test_a_watcher_batch_that_embeds_writes_one_summary_line(tmp_path, caplog):
    """15.0.3: watcher batches wrote no `embed.done` at all, so the path most
    everyday edits take was invisible in the log.  One line per batch that
    embedded something; a metadata-only batch stays quiet."""
    store = ReconcileStore()
    core = RetrievalCore(store, RecordingEmbedder())
    (tmp_path / "note.md").write_text("alpha beta", encoding="utf-8")
    with caplog.at_level(logging.INFO, logger="cognita.embed"):
        await core.reconcile_paths("P", tmp_path, ["note.md"])
    [done] = _done_lines(caplog)
    assert "walk=watcher" in done and "indexed=1" in done and "outcome=ok" in done
    assert "device=cpu" in done

    caplog.clear()
    os.utime(tmp_path / "note.md", (time.time() + 5, time.time() + 5))    # same bytes, new stat
    with caplog.at_level(logging.INFO, logger="cognita.embed"):
        refreshed = await core.reconcile_paths("P", tmp_path, ["note.md"])
    assert refreshed["metadata_refreshed"] == 1
    assert not _done_lines(caplog)


async def test_a_workspace_copy_names_itself_and_a_failed_write_is_still_reported(tmp_path, caplog):
    """The bridge's copies say walk=copy_from_workspace.  A file that embedded
    and then failed to store is exactly the batch worth seeing, so it is
    reported although nothing counts as indexed."""
    store = ReconcileStore()

    async def refuse(project, doc, chunks):
        raise RuntimeError("store unavailable")

    store.replace_document = refuse
    core = RetrievalCore(store, RecordingEmbedder())
    (tmp_path / "copied.md").write_text("gamma delta", encoding="utf-8")
    with caplog.at_level(logging.INFO, logger="cognita.embed"):
        summary = await core.reconcile_paths("P", tmp_path, ["copied.md"], walk="copy_from_workspace")
    assert summary["failed"] == 1 and summary["indexed"] == 0
    [done] = _done_lines(caplog)
    assert "walk=copy_from_workspace" in done and "outcome=partial" in done


async def test_a_cancelled_batch_is_reported_as_aborted(tmp_path, caplog):
    """The watcher cancels in-flight batches on stop; a batch that embedded
    before the cancel must not claim outcome=ok."""
    import asyncio

    import pytest

    class CancelledMidway(RecordingEmbedder):
        def embed(self, texts):
            super().embed(texts)
            raise asyncio.CancelledError()

    core = RetrievalCore(ReconcileStore(), CancelledMidway())
    (tmp_path / "note.md").write_text("alpha beta", encoding="utf-8")
    with caplog.at_level(logging.INFO, logger="cognita.embed"):
        with pytest.raises(asyncio.CancelledError):
            await core.reconcile_paths("P", tmp_path, ["note.md"])
    [done] = _done_lines(caplog)
    assert "outcome=aborted" in done
