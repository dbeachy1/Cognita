"""The walk's parse-ahead / embed-window pipeline (DESIGN-6.0 §4.4, §6.3).

`index_project` used to parse one document, embed its chunks, write it, and
move on. Two things were wrong with that, and only the second is obvious:

- every embed call was one document — a median of 12-20 chunks on the reference
  corpus, handed to a device that wants 64;
- at most ONE embed call could ever be in flight, so a second device could not
  have been fed no matter what the gate decided. The loop shape, not the gate,
  was the limit.

These tests pin the new shape AND every invariant it had to preserve while
changing it. The second group is the important one: batching across documents
is exactly the kind of change that silently widens a transaction or loses a
document, and the old sequential loop was the thing making those impossible.
"""

from __future__ import annotations

import asyncio
import os
import threading

import pytest

from cognita.retrieval import EMBED_WINDOW_CHUNKS, RetrievalCore
from retrieval_fakes import HashEmbedder


class RecordingEmbedder(HashEmbedder):
    """A HashEmbedder that remembers the SIZE of every call it was given."""

    def __init__(self, dimensions: int = 32):
        super().__init__(dimensions)
        self.calls: list[int] = []

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(len(texts))
        return super().embed(texts)


class FakeStore:
    """Enough Store for index_project, recording each write as one transaction."""

    def __init__(self):
        self.docs: dict[str, list] = {}
        self.writes: list[tuple[str, int]] = []
        self.touched: list[str] = []
        self.sources: dict = {}
        self.fail_on: set[str] = set()

    async def ensure_project(self, project):
        pass

    async def list_sources(self, project):
        return dict(self.sources)

    async def chunk_count(self, project, source):
        return len(self.docs.get(source, []))

    async def touch_document(self, project, source, mtime, size):
        self.touched.append(source)

    async def replace_document(self, project, doc, chunks):
        if doc.source in self.fail_on:
            raise RuntimeError("store refused")
        self.docs[doc.source] = list(chunks)
        self.writes.append((doc.source, len(chunks)))

    async def delete_documents_not_in(self, project, live):
        gone = [s for s in self.docs if s not in live]
        for s in gone:
            del self.docs[s]
        return len(gone)

    async def delete_all_documents(self, project):
        n = len(self.docs)
        self.docs.clear()
        return n

    async def get_document(self, project, source):
        return None


def write_corpus(tmp_path, spec: dict[str, int]):
    """spec: filename -> approximate chunk count wanted (1000 chars each)."""
    for name, chunks in spec.items():
        p = tmp_path / name
        p.parent.mkdir(parents=True, exist_ok=True)
        # advance is 800 chars/chunk, so this over-shoots slightly; the exact
        # count does not matter, only that documents differ in size.
        p.write_text("word " * (160 * chunks), encoding="utf-8")
    return tmp_path


def core_for(store, embedder):
    return RetrievalCore(store, embedder)


# --------------------------------------------------------------------------
# The new shape
# --------------------------------------------------------------------------


async def test_one_embed_call_covers_many_documents(tmp_path):
    """The whole point: chunks accumulate ACROSS documents before embedding.

    Twelve small documents used to mean twelve embed calls of a handful of
    chunks each. They must now arrive as far fewer, far larger calls.
    """
    write_corpus(tmp_path, {f"doc{i}.md": 2 for i in range(12)})
    store, emb = FakeStore(), RecordingEmbedder()
    summary = await core_for(store, emb).index_project("P", tmp_path)

    assert summary["indexed"] == 12
    assert len(emb.calls) < 12, f"still embedding per document: {emb.calls}"
    assert sum(emb.calls) == sum(n for _, n in store.writes)


async def test_a_window_flushes_at_the_chunk_threshold(tmp_path):
    """A big corpus is embedded in bounded windows, not one giant call.

    The window is a MEMORY bound — it is held in RAM until written — so it must
    actually bound. A single call covering the whole corpus would be the same
    bug as one call per document, in the other direction.
    """
    per_doc = 40
    docs = 20  # ~800 chunks, comfortably over one window
    write_corpus(tmp_path, {f"doc{i}.md": per_doc for i in range(docs)})
    store, emb = FakeStore(), RecordingEmbedder()
    await core_for(store, emb).index_project("P", tmp_path)

    assert len(emb.calls) > 1, "the whole corpus went into one call"
    # Each window is flushed once it CROSSES the threshold, so it may overshoot
    # by at most the last document's chunk count.
    assert max(emb.calls) <= EMBED_WINDOW_CHUNKS + per_doc * 2


async def test_a_document_larger_than_a_window_is_still_one_document(tmp_path):
    """A single document bigger than the window must not be split or dropped."""
    write_corpus(tmp_path, {"huge.md": 900})
    store, emb = FakeStore(), RecordingEmbedder()
    summary = await core_for(store, emb).index_project("P", tmp_path)

    assert summary["indexed"] == 1
    assert len(store.writes) == 1
    assert store.writes[0][1] > EMBED_WINDOW_CHUNKS


# --------------------------------------------------------------------------
# What the window must NOT have changed
# --------------------------------------------------------------------------


async def test_each_document_is_still_its_own_transaction(tmp_path):
    """🔴 D4.0: one document, one replace_document. The window batches the
    EMBED, never the write — widening the transaction would mean a failure on
    one document rolling back neighbors that were fine."""
    write_corpus(tmp_path, {f"doc{i}.md": 3 for i in range(9)})
    store, emb = FakeStore(), RecordingEmbedder()
    await core_for(store, emb).index_project("P", tmp_path)

    assert len(store.writes) == 9
    assert sorted(s for s, _ in store.writes) == sorted(f"doc{i}.md" for i in range(9))


async def test_a_store_failure_costs_only_its_own_document(tmp_path):
    """The neighbors in the window must still land."""
    write_corpus(tmp_path, {f"doc{i}.md": 3 for i in range(6)})
    store, emb = FakeStore(), RecordingEmbedder()
    store.fail_on = {"doc3.md"}
    summary = await core_for(store, emb).index_project("P", tmp_path)

    assert summary["indexed"] == 5
    assert len(summary["errors"]) == 1
    assert "doc3.md" in summary["errors"][0]
    assert "doc3.md" not in store.docs
    assert "doc4.md" in store.docs


async def test_an_embed_failure_is_attributed_to_a_document_not_a_window(tmp_path):
    """One embed call now spans documents, so a naive failure path would report
    the error against a batch and lose every document in it. The retry exists so
    the error lands on the file that caused it."""
    write_corpus(tmp_path, {f"doc{i}.md": 2 for i in range(5)})

    class PoisonEmbedder(HashEmbedder):
        def embed(self, texts):
            if any("poison" in t for t in texts):
                raise RuntimeError("bad chunk")
            return super().embed(texts)

    (tmp_path / "doc2.md").write_text("poison " * 400, encoding="utf-8")
    store = FakeStore()
    summary = await core_for(store, PoisonEmbedder()).index_project("P", tmp_path)

    # Every healthy document still indexed; only the poisoned one failed.
    assert summary["indexed"] == 4
    assert len(summary["errors"]) == 1
    assert "doc2.md" in summary["errors"][0]


async def test_unchanged_files_are_still_skipped_without_parsing(tmp_path):
    """The mtime/hash skip is in the producer now; it must still skip."""
    write_corpus(tmp_path, {f"doc{i}.md": 2 for i in range(5)})
    store, emb = FakeStore(), RecordingEmbedder()
    core = core_for(store, emb)
    first = await core.index_project("P", tmp_path)
    assert first["indexed"] == 5

    # Feed the stored rows back as `existing` so the second walk sees them.
    class Known:
        def __init__(self, source, doc_id, tier, mtime, size, category="general"):
            self.source, self.doc_id, self.tier = source, doc_id, tier
            self.file_mtime, self.file_size, self.category = mtime, size, category

    import datetime

    for source in list(store.docs):
        st = (tmp_path / source).stat()
        store.sources[source] = Known(
            source, "x", "embedded",
            datetime.datetime.fromtimestamp(st.st_mtime, datetime.timezone.utc),
            st.st_size,
        )
    emb.calls.clear()
    second = await core.index_project("P", tmp_path)

    assert second["skipped"] == 5
    assert second["indexed"] == 0
    assert emb.calls == [], "a skipped file must not reach the embedder"


async def test_a_touched_but_identical_file_is_never_chunked(tmp_path):
    """🔴 The 5.12.0 regression, found on a live box.

    The pre-pipeline loop checked `known.doc_id == doc.doc_id` BEFORE calling
    doc.chunks(), so a file whose mtime moved but whose bytes did not do zero
    chunking. Moving the chunk into the producer reintroduced that work on
    precisely the workload where it repeats forever: a live application
    rewriting files it has not actually changed, every watcher debounce.

    Asserted on the chunker rather than on timing, because a timing test would
    pass on a fast machine and prove nothing.
    """
    import datetime

    write_corpus(tmp_path, {"a.md": 6, "b.md": 6})
    store, emb = FakeStore(), RecordingEmbedder()
    core = core_for(store, emb)
    await core.index_project("P", tmp_path)

    class Known:
        def __init__(self, source, doc_id, tier, mtime, size):
            self.source, self.doc_id, self.tier = source, doc_id, tier
            self.file_mtime, self.file_size, self.category = mtime, size, "general"

    # Same content, but a NEW mtime — the "touched, not changed" case. The stat
    # short-circuit cannot help here; only the content hash can.
    for source in list(store.docs):
        path = tmp_path / source
        path.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
        os.utime(path, (1e9, 1e9))
        st = path.stat()
        real_doc = core._parse(path, tmp_path, core.policy_for("P"))
        store.sources[source] = Known(
            source, real_doc.doc_id, "embedded",
            datetime.datetime.fromtimestamp(st.st_mtime - 500, datetime.timezone.utc),
            st.st_size + 1,  # force the cheap stat check to MISS
        )

    chunked: list[str] = []
    real_chunks = type(real_doc).chunks

    def spy(self, size, overlap):
        chunked.append(self.source)
        return real_chunks(self, size, overlap)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(type(real_doc), "chunks", spy)
        summary = await core.index_project("P", tmp_path)

    assert summary["skipped"] == 2, "content-identical files must skip"
    assert summary["indexed"] == 0
    assert chunked == [], f"chunked a file it was about to skip: {chunked}"


async def test_a_walk_with_no_work_never_starts_the_gpu(tmp_path):
    """🔴 The regression that refused a connector's writes.

    `index_project` holds the project write lock for its whole duration, and
    the pool used to start up front on the strength of the ESTIMATE. The
    estimate is a lower bound from file sizes — it cannot know a file is
    byte-identical without parsing it — so a walk predicted at 387 chunks
    turned out to have 3, after spending 60-135 seconds spawning workers and
    compiling graphs while holding the lock. Three `update_document` calls
    arriving in that window were refused twenty seconds apart: a connector
    session failed to save its work, three times running.

    So the pool must start on the first window that genuinely has chunks, not
    on a prediction. A walk that turns out to have nothing to do must not touch
    a GPU at all.
    """
    import datetime

    from cognita import gpu_host

    write_corpus(tmp_path, {f"doc{i}.md": 3 for i in range(4)})
    store, emb = FakeStore(), RecordingEmbedder()

    class LoudConfig:
        """Any pool start at all is a failure here."""
        gpu_enabled = True
        gpu_venv_python = "/nonexistent/python"
        gpu_min_chunks = 1  # the estimate will WANT the GPU
        gpu_batch_size = 4
        gpu_slice_chunks = 512
        gpu_reserve_vram_gb = 4.0
        gpu_max_busy_percent = 20
        gpu_device_ids: list[str] = []
        gpu_provider = "migraphx"
        gpu_worker_shutdown_s = 1.0
        gpu_worker_slice_timeout_s = 5.0
        gpu_canary_tolerance = 1e-4

    core = RetrievalCore(store, emb, gpu_config=LoudConfig(), gpu_min_chunks=1)
    await core.index_project("P", tmp_path)

    # Everything is now indexed and byte-identical, so a second walk has real
    # files (the estimate will predict work) and zero actual chunks to embed.
    class Known:
        def __init__(self, source, doc_id, tier, mtime, size):
            self.source, self.doc_id, self.tier = source, doc_id, tier
            self.file_mtime, self.file_size, self.category = mtime, size, "general"

    for source in list(store.docs):
        path = tmp_path / source
        os.utime(path, (1e9, 1e9))  # touched, so the cheap stat check misses
        st = path.stat()
        doc = core._parse(path, tmp_path, core.policy_for("P"))
        store.sources[source] = Known(
            source, doc.doc_id, "embedded",
            datetime.datetime.fromtimestamp(st.st_mtime - 500, datetime.timezone.utc),
            st.st_size + 1,
        )

    starts = []
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(gpu_host, "start_pool",
                   lambda *a, **kw: starts.append(a) or None)
        summary = await core.index_project("P", tmp_path)

    assert summary["skipped"] == 4 and summary["indexed"] == 0
    assert starts == [], (
        "a walk with nothing to embed spun up the GPU — that is 60-135s of "
        "write lock held for no work, and it refuses concurrent writes"
    )


async def test_a_large_document_write_uses_the_gpu(tmp_path):
    """🔴 §4.3: there is NO special case for a single-document write.

    An edit re-embeds the WHOLE file, so "one document" does not mean "small
    work" — one line changed in a large manual is thousands of chunks, and that
    is exactly the expensive case the GPU exists for. The rule lived in
    index_project while the path every connector edit takes hardcoded the CPU.
    Observed live: a 341-chunk update_document taking 64 seconds on a saturated
    CPU with two idle cards.
    """
    from cognita import gpu_host

    class Cfg:
        gpu_enabled = True
        gpu_venv_python = "/nonexistent/python"
        gpu_batch_size = 4
        gpu_slice_chunks = 512
        gpu_reserve_vram_gb = 4.0
        gpu_max_busy_percent = 20
        gpu_device_ids: list[str] = []
        gpu_provider = "migraphx"
        gpu_worker_shutdown_s = 1.0
        gpu_worker_slice_timeout_s = 5.0
        gpu_canary_tolerance = 1e-4

    write_corpus(tmp_path, {"big.md": 40})
    store, emb = FakeStore(), RecordingEmbedder()
    core = RetrievalCore(store, emb, gpu_config=Cfg(), gpu_min_chunks=10)

    starts = []
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(gpu_host, "start_pool", lambda *a, **kw: starts.append(a) or None)
        out = await core.index_file("P", tmp_path, tmp_path / "big.md")

    assert out is not None and out[1] > 10
    assert len(starts) == 1, "a large document write never consulted the GPU"
    # start_pool returned None (no device), so the CPU still produced the
    # vectors — §10: the write completes either way.
    assert store.docs["big.md"], "the document must be indexed regardless"


async def test_a_small_document_write_does_not_touch_the_gpu(tmp_path):
    """The other side of the same rule: small edits stay on the CPU, where
    spinning a card up would make them slower. Measured live at 4-19 chunks a
    piece, arriving in bursts from a connector session."""
    from cognita import gpu_host

    class Cfg:
        gpu_enabled = True
        gpu_venv_python = "/nonexistent/python"
        gpu_batch_size = 4
        gpu_slice_chunks = 512
        gpu_reserve_vram_gb = 4.0
        gpu_max_busy_percent = 20
        gpu_device_ids: list[str] = []
        gpu_provider = "migraphx"
        gpu_worker_shutdown_s = 1.0
        gpu_worker_slice_timeout_s = 5.0
        gpu_canary_tolerance = 1e-4

    write_corpus(tmp_path, {"small.md": 2})
    store, emb = FakeStore(), RecordingEmbedder()
    core = RetrievalCore(store, emb, gpu_config=Cfg(), gpu_min_chunks=300)

    starts = []
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(gpu_host, "start_pool", lambda *a, **kw: starts.append(a) or None)
        await core.index_file("P", tmp_path, tmp_path / "small.md")

    assert starts == [], "a tiny edit spun up a GPU it would only be slowed by"


async def test_a_walk_with_real_work_does_start_the_gpu(tmp_path):
    """The other half: lazy must not mean never."""
    from cognita import gpu_host

    write_corpus(tmp_path, {f"doc{i}.md": 3 for i in range(4)})
    store, emb = FakeStore(), RecordingEmbedder()

    class Cfg:
        gpu_enabled = True
        gpu_venv_python = "/nonexistent/python"
        gpu_batch_size = 4
        gpu_slice_chunks = 512
        gpu_reserve_vram_gb = 4.0
        gpu_max_busy_percent = 20
        gpu_device_ids: list[str] = []
        gpu_provider = "migraphx"
        gpu_worker_shutdown_s = 1.0
        gpu_worker_slice_timeout_s = 5.0
        gpu_canary_tolerance = 1e-4

    core = RetrievalCore(store, emb, gpu_config=Cfg(), gpu_min_chunks=1)
    starts = []
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(gpu_host, "start_pool",
                   lambda *a, **kw: starts.append(a) or None)
        summary = await core.index_project("P", tmp_path)

    assert summary["indexed"] == 4
    assert len(starts) == 1, "the pool must start exactly once, on first real work"


async def test_a_registered_document_never_reaches_the_embedder(tmp_path):
    """4.4: registered tier is genuinely skipped, not embedded-and-discarded."""
    (tmp_path / "notes.md").write_text("word " * 320, encoding="utf-8")
    (tmp_path / "script.py").write_text("print('hello')\n" * 50, encoding="utf-8")
    store, emb = FakeStore(), RecordingEmbedder()
    summary = await core_for(store, emb).index_project("P", tmp_path)

    assert summary["indexed"] == 2
    assert store.docs["script.py"] == [], "registered doc got chunks"
    assert sum(emb.calls) == len(store.docs["notes.md"])


async def test_single_file_book_capture_precedes_legacy_docx_and_records_same_facts(tmp_path):
    """A registered chapter source is selected from captured bytes before docx parsing."""
    source = tmp_path / "chapter.docx"
    source.write_bytes(b"not a python-docx package")
    store, embedder = FakeStore(), RecordingEmbedder()
    core = core_for(store, embedder)
    seen: list[tuple[str, object]] = []
    record = object()

    def content_provider(project, relative, suffix, raw):
        seen.append(("content", (project, relative, suffix, raw)))
        return "captured chapter prose", {"role": "chapter_working"}

    def capture_provider(project, document):
        assert project == "P"
        assert document.content == "captured chapter prose"
        assert document.captured_raw is None
        assert document.book_index_context == {"role": "chapter_working"}
        document.book_index_record = record
        document.book_index_context = None
        seen.append(("capture", document.doc_id))
        return document

    core.set_book_index_content_provider(content_provider)
    core.set_book_index_capture_provider(capture_provider)
    core.set_book_index_currentness_provider(lambda project, candidate: project == "P" and candidate is record)
    core.set_book_index_provenance_recorder(lambda project, candidate: seen.append(("record", candidate)) or candidate)

    outcome = await core.index_file("P", tmp_path, source)

    assert outcome is not None and outcome.indexed
    assert [item[0] for item in seen] == ["content", "capture", "record"]
    assert store.writes == [("chapter.docx", 1)]


async def test_single_file_final_policy_change_refuses_captured_publication(tmp_path):
    """A folder exclusion landing during embedding wins before store replacement."""
    from cognita.books.config import FolderRule
    from cognita.books.policy import EffectiveIndexPolicy

    class BlockingEmbedder(HashEmbedder):
        def __init__(self):
            super().__init__()
            self.started, self.release = threading.Event(), threading.Event()

        def embed(self, texts):
            self.started.set()
            assert self.release.wait(5)
            return super().embed(texts)

    path = tmp_path / "private" / "note.md"
    path.parent.mkdir()
    path.write_text("captured prose", encoding="utf-8")
    store, embedder = FakeStore(), BlockingEmbedder()
    core = core_for(store, embedder)
    current = {"policy": EffectiveIndexPolicy([])}
    core.set_effective_index_policy_provider(lambda _project: current["policy"])
    task = asyncio.create_task(core.index_file("P", tmp_path, path))
    await asyncio.to_thread(embedder.started.wait, 5)
    current["policy"] = EffectiveIndexPolicy([FolderRule(path="private", indexed=False)])
    embedder.release.set()
    outcome = await task

    assert outcome is not None and not outcome.indexed
    assert outcome.exclusion_reason == "policy_excluded"
    assert store.writes == []


async def test_a_parse_failure_does_not_stop_the_walk(tmp_path):
    """The producer runs on its own task; an exception there must surface as one
    document's error, not as a hung consumer waiting on a dead producer."""
    write_corpus(tmp_path, {f"doc{i}.md": 2 for i in range(4)})
    store, emb = FakeStore(), RecordingEmbedder()
    core = core_for(store, emb)
    real = core._parse_and_chunk

    def boom(filepath, documents_dir, policy, **kw):
        if filepath.name == "doc1.md":
            raise RuntimeError("cannot parse")
        return real(filepath, documents_dir, policy, **kw)

    core._parse_and_chunk = boom
    summary = await asyncio.wait_for(core.index_project("P", tmp_path), timeout=30)

    assert summary["indexed"] == 3
    assert len(summary["errors"]) == 1
    assert "doc1.md" in summary["errors"][0]


async def test_a_failed_walk_does_not_leave_the_producer_running(tmp_path):
    """A producer outliving its walk would sit parsing into a queue nobody
    reads, holding a document's memory per slot."""
    write_corpus(tmp_path, {f"doc{i}.md": 2 for i in range(40)})
    store, emb = FakeStore(), RecordingEmbedder()
    core = core_for(store, emb)

    async def explode(*a, **kw):
        raise RuntimeError("store died")

    store.replace_document = explode
    await core.index_project("P", tmp_path)

    await asyncio.sleep(0)
    running = [t for t in asyncio.all_tasks()
               if "_parse_ahead" in repr(t.get_coro()) and not t.done()]
    assert running == [], f"producer still alive: {running}"


async def test_progress_still_reports_every_file(tmp_path):
    """get_reindex_status is driven by this; it must not stall or skip."""
    write_corpus(tmp_path, {f"doc{i}.md": 2 for i in range(7)})
    store, emb = FakeStore(), RecordingEmbedder()
    seen: list[int] = []
    await core_for(store, emb).index_project(
        "P", tmp_path, progress=lambda s: seen.append(s["processed"])
    )

    assert seen, "no progress was reported at all"
    assert seen[-1] == 7
    assert seen == sorted(seen), "progress went backwards"


async def test_the_removal_sweep_still_sees_every_live_source(tmp_path):
    """live_sources is assembled by the consumer now. A source missing from it
    is a row the sweep deletes — a document silently lost."""
    write_corpus(tmp_path, {f"doc{i}.md": 2 for i in range(5)})
    store, emb = FakeStore(), RecordingEmbedder()
    store.docs["gone.md"] = []
    summary = await core_for(store, emb).index_project("P", tmp_path)

    assert summary["removed"] == 1
    assert "gone.md" not in store.docs
    assert len(store.docs) == 5


async def test_an_empty_file_is_not_kept_alive(tmp_path):
    """An empty file has no row, so it must NOT be added to live_sources."""
    write_corpus(tmp_path, {"real.md": 2})
    (tmp_path / "empty.md").write_text("", encoding="utf-8")
    store, emb = FakeStore(), RecordingEmbedder()
    summary = await core_for(store, emb).index_project("P", tmp_path)

    assert summary["indexed"] == 1
    assert "empty.md" not in store.docs


@pytest.mark.parametrize("count", [1, 2, 63, 64, 65])
async def test_document_counts_around_the_window_boundary(tmp_path, count):
    """Off-by-one at a flush boundary would lose or duplicate a document."""
    write_corpus(tmp_path, {f"d{i}.md": 2 for i in range(count)})
    store, emb = FakeStore(), RecordingEmbedder()
    summary = await core_for(store, emb).index_project("P", tmp_path)

    assert summary["indexed"] == count
    assert len(store.docs) == count
    assert sum(emb.calls) == sum(len(c) for c in store.docs.values())
