"""DESIGN-6.0 §12.1/§14: the embed path is instrumented, on the CPU, first.

These assert on the four records and on the two properties that make them
trustworthy rather than decorative:

- **One line per record.** A walk emits hundreds of ``embed.batch`` lines and
  the point of them is that ``grep embed.batch`` returns rows. A record that
  wrapped would be unparseable exactly when there is most of it.
- **A job is scoped to the task that opened it.** A search issued *during* a
  rebuild must not be counted into the rebuild — its chunks would inflate the
  walk's totals and its 1-chunk batches would wreck the rate. contextvars give
  that for free; this pins it, because "for free" is what quietly stops being
  true when a caller is refactored onto a different task.

And two behaviors that were previously silent and are now not:
``_construct_without_arena`` degrading a rung, and the reranker failing to load.
Both used to leave a process running in a materially different mode with no
trace at all — the shape of defect DESIGN-6.0 §14.5 exists to stop.
"""

from __future__ import annotations

import asyncio
import logging
import shlex
import sys
import types

import pytest

from cognita import embed_telemetry as tel
from cognita import embeddings
from cognita.embed_telemetry import (
    CPU_DEVICE,
    EmbedJob,
    current_job,
    embed_job,
    record_batch,
)
from cognita.embeddings import Embedder, Reranker

# The `fake_fastembed` fixture that installs these lives in conftest.py.
from fastembed_fakes import _FakeCrossEncoder, _FakeTextEmbedding


def _records(caplog, prefix: str) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.getMessage().startswith(prefix)]


def _fields(line: str) -> dict[str, str]:
    """The record's own fields, i.e. everything before the first device row.

    Device rows reuse the head's field names by design (§14.3), so a parser that
    swept the whole line would read the last device's `chunks` as the job's.
    That ambiguity is exactly what the " | " separator exists to remove, and
    reading it here is what proves the separator does its job.
    """
    head = line.split("  ", 1)[1].split(" | ", 1)[0]
    out: dict[str, str] = {}
    for token in shlex.split(head):
        if "=" in token:
            k, v = token.split("=", 1)
            out[k] = v
    return out


def _device_rows(line: str) -> list[dict[str, str]]:
    rows = line.split(" | ")[1:]
    return [dict(t.split("=", 1) for t in shlex.split(r) if "=" in t) for r in rows]


# --------------------------------------------------------------------------
# The records themselves
# --------------------------------------------------------------------------


def test_batch_is_debug_and_carries_the_work_it_was_given(caplog):
    with caplog.at_level(logging.DEBUG, logger="cognita.embed"):
        record_batch(CPU_DEVICE, chunks=18, chars=17402, elapsed=1.89)

    line = _records(caplog, "embed.batch")[0]
    fields = _fields(line)
    assert fields["device"] == "cpu"
    assert fields["chunks"] == "18"
    assert fields["chars"] == "17402"
    assert fields["rate"] == "9.5/s"


def test_batch_stays_off_info(caplog):
    """A 17,600-chunk walk is hundreds of these; INFO would drown the log."""
    with caplog.at_level(logging.INFO, logger="cognita.embed"):
        record_batch(CPU_DEVICE, chunks=4, chars=40, elapsed=0.5)
    assert _records(caplog, "embed.batch") == []


def test_every_record_is_a_single_line(caplog):
    with caplog.at_level(logging.DEBUG, logger="cognita.embed"):
        with embed_job("P", walk="project") as job:
            job.plan(files=3, force=True)
            record_batch(CPU_DEVICE, chunks=2, chars=20, elapsed=0.1)
            record_batch("gpu:0", chunks=5, chars=50, elapsed=0.2)
            job.done(indexed=3)
        embeddings.record_cpu_init(model="a b", provider_active="CPUExecutionProvider")

    emitted = [r.getMessage() for r in caplog.records]
    assert emitted, "nothing was logged"
    assert all("\n" not in m for m in emitted)


def test_a_value_with_spaces_is_quoted(caplog):
    with caplog.at_level(logging.INFO, logger="cognita.embed"):
        tel.record_cpu_init(name="Radeon Pro W7900", dims=1024)
    line = _records(caplog, "embed.cpu.init")[0]
    assert 'name="Radeon Pro W7900"' in line
    assert _fields(line)["name"] == "Radeon Pro W7900"


# --------------------------------------------------------------------------
# Accumulation
# --------------------------------------------------------------------------


def test_done_totals_every_device_and_keeps_a_row_each(caplog):
    with caplog.at_level(logging.DEBUG, logger="cognita.embed"):
        with embed_job("altea", walk="project") as job:
            record_batch("gpu:0", chunks=100, chars=1000, elapsed=1.0)
            record_batch("gpu:1", chunks=50, chars=500, elapsed=0.5)
            record_batch("gpu:0", chunks=100, chars=1000, elapsed=1.0)
            job.done()

    line = _records(caplog, "embed.done")[0]
    fields = _fields(line)
    assert fields["chunks"] == "250"
    assert fields["batches"] == "3"
    assert float(fields["elapsed"]) == pytest.approx(2.5)
    # One row per device, on the same line, and unambiguously separable from
    # the job's own totals despite sharing their field names.
    rows = _device_rows(line)
    assert [r["device"] for r in rows] == ["gpu:0", "gpu:1"]
    assert [r["chunks"] for r in rows] == ["200", "50"]
    assert [r["batches"] for r in rows] == ["2", "1"]


def test_a_card_that_embedded_makes_the_decision_gpu(caplog):
    """15.0.1: the scheduler path never set `decision`, so Maia's first NVIDIA
    index logged decision=cpu over a gpu0 row of 3,589 chunks."""
    with caplog.at_level(logging.INFO, logger="cognita.embed"):
        with embed_job("maia", walk="project") as job:
            record_batch("cpu", chunks=96, chars=960, elapsed=26.9)
            record_batch("gpu0", chunks=3589, chars=35890, elapsed=32.0)
            job.done()
    assert _fields(_records(caplog, "embed.done")[0])["decision"] == "gpu"


def test_a_cpu_only_job_stays_cpu(caplog):
    with caplog.at_level(logging.INFO, logger="cognita.embed"):
        with embed_job("maia", walk="project") as job:
            record_batch("cpu", chunks=10, chars=100, elapsed=1.0)
            record_batch("gpu0", chunks=0, chars=0, elapsed=0.1)
            job.done()
    assert _fields(_records(caplog, "embed.done")[0])["decision"] == "cpu"


def test_wall_and_elapsed_are_different_numbers(caplog):
    """The gap between them is everything that is NOT embedding.

    That ratio is the measurement DESIGN-6.0 §1 rests on — if parse, chunk and
    the Postgres inserts already dominate, moving the embed step buys less than
    the arithmetic says. Reporting only one of the two hides it.
    """
    with caplog.at_level(logging.DEBUG, logger="cognita.embed"):
        with embed_job("P", walk="project") as job:
            record_batch(CPU_DEVICE, chunks=10, chars=100, elapsed=0.25)
            job.done()

    fields = _fields(_records(caplog, "embed.done")[0])
    assert float(fields["elapsed"]) == pytest.approx(0.25)
    assert float(fields["wall"]) >= 0.0
    assert "wall" in fields and "elapsed" in fields


class FakeClock:
    """A clock a test can drive, so the union math is pinned and not raced."""

    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def at(self, when):
        self.now = when
        return self


def _job_with(clock):
    return EmbedJob(project="P", walk="project", _clock=clock)


def test_one_device_reports_the_same_rate_as_the_summed_elapsed(caplog):
    """🔴 THE BASELINE THAT MUST NOT MOVE. §14.4 nominates `rate` as the number
    answering "is the GPU worth it" BY COMPARING THE CPU LINE TO THE GPU LINE,
    so a fix to the GPU side that shifted the CPU side would destroy the
    comparison it was meant to repair.

    One device embeds sequentially, so its busy intervals are disjoint and
    their union IS their sum. `embed_wall == elapsed`, and the reported rate is
    identical to what 6.0.10 printed."""
    clock = FakeClock()
    job = _job_with(clock)
    # Three back-to-back batches with real gaps between them for parsing.
    job.record(CPU_DEVICE, chunks=10, chars=100, elapsed=1.0)   # 999-1000
    clock.at(1005.0)
    job.record(CPU_DEVICE, chunks=10, chars=100, elapsed=1.0)   # 1004-1005
    clock.at(1010.0)
    job.record(CPU_DEVICE, chunks=10, chars=100, elapsed=1.0)   # 1009-1010

    assert job.embed_elapsed == pytest.approx(3.0)
    assert job.embed_wall == pytest.approx(3.0), (
        "the idle gaps between batches must NOT be counted as embedding time"
    )


def test_two_concurrent_cards_no_longer_halve_their_own_rate():
    """🔴 THE 6.0.11 BUG. `rate` divided by the SUM of per-device elapsed, so
    two cards working at the same time reported half the throughput they
    achieved — and it got worse in proportion to the number of cards, i.e. the
    better the hardware, the bigger the lie.

    Numbers are kei's first successful two-card rebuild: 12,589 chunks over
    card1 430.3s and card2 413.1s, overlapping almost completely. The old
    denominator was 843.4s (14.9/s); the honest one is ~430s (~29/s)."""
    clock = FakeClock()
    job = _job_with(clock)
    clock.at(1000.0 + 430.3)
    job.record("card1", chunks=6571, chars=0, elapsed=430.3)
    clock.at(1000.0 + 17.2 + 413.1)     # started ~17s later, finished with it
    job.record("card2", chunks=6018, chars=0, elapsed=413.1)

    assert job.chunks == 12589
    assert job.embed_elapsed == pytest.approx(843.4, abs=0.1)
    # The union: card1 covers 1000.0-1430.3, card2 sits inside it.
    assert job.embed_wall == pytest.approx(430.3, abs=0.5)
    honest = job.chunks / job.embed_wall
    stale = job.chunks / job.embed_elapsed
    assert honest == pytest.approx(29.3, abs=0.5)
    assert stale == pytest.approx(14.9, abs=0.5)
    assert honest > stale * 1.9, "the multi-card understatement is back"


def test_a_gap_between_two_cards_is_not_counted_as_embedding():
    """⚠️ A union, not `last_end - first_start`. If both cards stop for a
    minute while the walk parses, that minute is not embedding time and must
    not dilute the rate — `wall` is the field that includes it."""
    clock = FakeClock()
    job = _job_with(clock)
    clock.at(1010.0)
    job.record("card1", chunks=100, chars=0, elapsed=10.0)   # 1000-1010
    clock.at(1010.0)
    job.record("card2", chunks=100, chars=0, elapsed=10.0)   # 1000-1010
    clock.at(1080.0)                                          # 60s idle gap
    job.record("card1", chunks=100, chars=0, elapsed=10.0)   # 1070-1080
    clock.at(1080.0)
    job.record("card2", chunks=100, chars=0, elapsed=10.0)   # 1070-1080

    assert job.embed_elapsed == pytest.approx(40.0)   # four batches of 10
    assert job.embed_wall == pytest.approx(20.0)      # two overlapping windows


def test_the_done_line_carries_embed_wall_and_rates_from_it(caplog):
    """The fix has to reach the LINE, not just the property — the whole defect
    was a correct per-device row beside a wrong job-level number."""
    clock = FakeClock()
    job = _job_with(clock)
    clock.at(1010.0)
    job.record("card1", chunks=500, chars=0, elapsed=10.0)
    clock.at(1010.0)
    job.record("card2", chunks=500, chars=0, elapsed=10.0)
    with caplog.at_level(logging.DEBUG, logger="cognita.embed"):
        job.done()

    fields = _fields(_records(caplog, "embed.done")[0])
    assert float(fields["elapsed"]) == pytest.approx(20.0)
    assert float(fields["embed_wall"]) == pytest.approx(10.0)
    assert fields["rate"] == "100.0/s"      # 1000 chunks / 10s, not / 20s


def test_est_error_is_absent_until_an_estimate_exists(caplog):
    """No estimator has landed yet, so nothing may pretend to have estimated.

    A fabricated est_chunks would grade itself perfectly for ever, which is
    worse than no field at all: §14.4 uses est_error to decide whether §4.1's
    formula needs revisiting.
    """
    with caplog.at_level(logging.DEBUG, logger="cognita.embed"):
        with embed_job("P", walk="project") as job:
            job.done()
    assert "est_error" not in _fields(_records(caplog, "embed.done")[0])

    caplog.clear()
    with caplog.at_level(logging.DEBUG, logger="cognita.embed"):
        with embed_job("P", walk="project") as job:
            job.est_chunks = 100
            record_batch(CPU_DEVICE, chunks=123, chars=1, elapsed=1.0)
            job.done()
    assert _fields(_records(caplog, "embed.done")[0])["est_error"] == "+23.0%"


def test_done_is_idempotent(caplog):
    """index_project's finally is a safety net, not a second report."""
    with caplog.at_level(logging.DEBUG, logger="cognita.embed"):
        with embed_job("P", walk="project") as job:
            job.done(outcome="ok")
            job.done(outcome="aborted")
    lines = _records(caplog, "embed.done")
    assert len(lines) == 1
    assert "outcome=ok" in lines[0]


# --------------------------------------------------------------------------
# Job scope
# --------------------------------------------------------------------------


def test_a_nested_job_joins_the_outer_one(caplog):
    """§4.4: copy_directory copying 500 files is ONE job, not 500 tiny ones.

    A per-file job is correct on every individual decision and wrong in
    aggregate — it is the exact bug §4.4 was added to the design to prevent, and
    it presents as "the GPU feature doesn't seem to do much" rather than as an
    error. Nesting has to join by default or every future bulk caller inherits
    it.
    """
    with caplog.at_level(logging.DEBUG, logger="cognita.embed"):
        with embed_job("P", walk="copy_directory") as outer:
            outer.plan(files=500)
            for _ in range(3):
                with embed_job("P", walk="single") as inner:
                    assert inner is outer
                    record_batch(CPU_DEVICE, chunks=4, chars=40, elapsed=0.1)
            outer.done()

    assert len(_records(caplog, "embed.plan")) == 1
    assert len(_records(caplog, "embed.done")) == 1
    assert _fields(_records(caplog, "embed.done")[0])["chunks"] == "12"


def test_a_nested_job_does_not_close_the_outer_one(caplog):
    """Leaving the inner block must not unset the job the outer block owns."""
    with embed_job("P", walk="outer") as outer:
        with embed_job("P", walk="inner"):
            pass
        assert current_job() is outer
    assert current_job() is None


@pytest.mark.asyncio
async def test_a_search_running_beside_a_walk_is_not_counted_into_it():
    """The real shape: two SIBLING tasks, as the server creates them.

    A rebuild runs in its own task and a search arrives on the handler's; each
    inherits the context of whatever created it, and neither is inside the
    other. DESIGN-6.0 §3 leans on the separation — queries never touch the GPU,
    so a search during a GPU rebuild does not contend with it — and the
    accounting has to match, or a one-chunk query batch is averaged into the
    walk's rate.

    Written as siblings deliberately. A first draft awaited the search from
    *inside* the walk, which inherits the job and counts it; that draft was
    testing the wrong topology, not finding a bug.
    """
    started = asyncio.Event()
    seen: list[EmbedJob | None] = []

    async def a_search() -> None:
        await started.wait()
        seen.append(current_job())
        record_batch(CPU_DEVICE, chunks=1, chars=30, elapsed=0.01)

    async def a_walk() -> EmbedJob:
        with embed_job("P", walk="project") as job:
            record_batch(CPU_DEVICE, chunks=40, chars=400, elapsed=1.0)
            started.set()
            await asyncio.sleep(0)
            return job

    job, _ = await asyncio.gather(a_walk(), a_search())
    assert seen == [None]
    assert job.chunks == 40, "the concurrent query leaked into the walk's totals"


@pytest.mark.asyncio
async def test_work_the_walk_fans_out_itself_is_counted_into_it():
    """The other half of the same rule, and the one the GPU path needs.

    §6.3 dispatches slices to one worker per device concurrently. Those are
    descendants of the walk's task, so they inherit its job and their chunks
    land in its totals — which is the only reason a multi-device `embed.done`
    can add up.
    """
    async def a_worker(chunks: int) -> None:
        record_batch(f"gpu:{chunks}", chunks=chunks, chars=chunks * 10, elapsed=1.0)

    with embed_job("P", walk="project") as job:
        await asyncio.gather(a_worker(100), a_worker(50))

    assert job.chunks == 150
    assert set(job.devices) == {"gpu:100", "gpu:50"}


@pytest.mark.asyncio
async def test_a_job_reaches_the_embedder_running_in_a_thread():
    """embed() runs under asyncio.to_thread; the job must follow it there."""
    with embed_job("P", walk="project") as job:
        await asyncio.to_thread(record_batch, CPU_DEVICE, 7, 70, 0.5)
    assert job.chunks == 7


def test_a_batch_outside_any_job_is_recorded_but_not_attributed(caplog):
    """The query path. It logs — "how long is a query embed" is worth having."""
    with caplog.at_level(logging.DEBUG, logger="cognita.embed"):
        record_batch(CPU_DEVICE, chunks=1, chars=30, elapsed=0.01)
    assert len(_records(caplog, "embed.batch")) == 1
    assert current_job() is None


# --------------------------------------------------------------------------
# The embedder end of it
# --------------------------------------------------------------------------


def test_a_real_embed_records_itself_onto_the_open_job(fake_fastembed, tmp_path, caplog):
    embedder = Embedder("fake-model", 4, tmp_path)
    with caplog.at_level(logging.DEBUG, logger="cognita.embed"):
        with embed_job("P", walk="project") as job:
            embedder.embed(["alpha", "bravo", "charlie"])

    assert job.chunks == 3
    assert job.batches == 1
    assert job.devices[CPU_DEVICE].chars == len("alpha") + len("bravo") + len("charlie")
    assert _fields(_records(caplog, "embed.batch")[0])["device"] == "cpu"


def test_a_failed_embed_counts_as_time_spent_but_not_as_work_done(
    fake_fastembed, tmp_path, monkeypatch
):
    """A walk slowed down by an embedder that keeps raising is still slow — and
    it still embedded nothing. Both halves matter and they are different fields.

    The attempt must be visible (`batches`, `elapsed`), because recording only
    successes shows that walk as having done no embedding at all, which is the
    reading least likely to lead anyone to the cause.

    🔴 But `chunks` is a WORK-COMPLETED count. This asserted `chunks == 2` for an
    embed that produced ZERO vectors, so a walk that aborted mid-window logged
    `chunks=512 ... indexed=0`, and §14.4 feeds that same field into `est_error`
    as the actual side. Anything summing `embed.done chunks=` counted vectors
    that are not in the index.
    """
    embedder = Embedder("fake-model", 4, tmp_path)

    def _boom(texts, **kwargs):
        raise RuntimeError("no")

    monkeypatch.setattr(_FakeTextEmbedding, "embed", _boom)
    with embed_job("P", walk="project") as job:
        with pytest.raises(embeddings.EmbeddingUnavailable):
            embedder.embed(["alpha", "bravo"])
    assert job.batches == 1, "the attempt must be visible"
    assert job.devices["cpu"].elapsed > 0, "the time spent must be visible"
    assert job.chunks == 0, "but nothing was embedded, so nothing was completed"


def test_est_error_refuses_to_grade_itself_on_a_corpus_with_binary_formats(caplog):
    """🔴 §4.2 excludes binary formats from `est_chunks` BY DESIGN — a PDF's size
    says nothing about its text, so it counts as a reason to use the GPU and
    contributes zero to the estimate. Its chunks still land in the actual, so
    `est_error` on any corpus holding one is a large positive number for ever,
    however good the §4.1 formula is.

    §14.4 nominates est_error as "the estimator grading itself... the only place
    a drift would show up", so a permanently-wrong value there is worse than no
    value: it reads as a signal and cannot move.
    """
    with caplog.at_level(logging.INFO):
        with embed_job("P", walk="project") as job:
            job.est_chunks = 100
            job.binary_formats = 2
            record_batch("cpu", chunks=400, chars=4000, elapsed=1.0)
            job.done()
    line = [r.getMessage() for r in caplog.records if "embed.done" in r.getMessage()][0]
    assert "est_error=n/a" in line
    assert "est_error_reason=binary_formats" in line


def test_est_error_is_still_reported_on_an_ordinary_corpus(caplog):
    """The signal must survive: with no binary formats it is a real percentage."""
    with caplog.at_level(logging.INFO):
        with embed_job("P", walk="project") as job:
            job.est_chunks = 100
            job.binary_formats = 0
            record_batch("cpu", chunks=110, chars=1100, elapsed=1.0)
            job.done()
    line = [r.getMessage() for r in caplog.records if "embed.done" in r.getMessage()][0]
    assert "est_error=+10.0%" in line


def test_init_reports_the_provider_read_back_from_the_session(
    fake_fastembed, tmp_path, caplog
):
    """§14.2: never assume the provider from what was requested.

    A runtime that loads and then silently serves a different provider is
    invisible in every other signal — the run is simply slower, and nothing
    prompts anyone to look.
    """

    class _Session:
        def get_providers(self):
            return ["SomethingElseExecutionProvider"]

    class _Inner:
        model = _Session()
        model_description = types.SimpleNamespace(
            model_file="model.onnx",
            sources=types.SimpleNamespace(hf="qdrant/bge-large-en-v1.5-onnx"),
        )

    def _init(self, **kwargs):
        self.init_kwargs = kwargs
        self.model = _Inner()

    with caplog.at_level(logging.INFO, logger="cognita.embed"):
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(_FakeTextEmbedding, "__init__", _init)
            Embedder("fake-model", 4, tmp_path)._ensure_model()

    fields = _fields(_records(caplog, "embed.cpu.init")[0])
    assert fields["provider_requested"] == "CPUExecutionProvider"
    assert fields["provider_active"] == "SomethingElseExecutionProvider"
    assert fields["model_file"] == "model.onnx"
    assert fields["model_source"] == "qdrant/bge-large-en-v1.5-onnx"
    assert fields["arena"] == "off"


def test_unreadable_session_internals_never_break_a_working_model(
    fake_fastembed, tmp_path, caplog
):
    """fastembed's internals are not a public API; the readback is best effort."""
    with caplog.at_level(logging.INFO, logger="cognita.embed"):
        embedder = Embedder("fake-model", 4, tmp_path)
        assert embedder.embed(["x"]) == [[0.0] * 4]
    fields = _fields(_records(caplog, "embed.cpu.init")[0])
    assert "provider_active" not in fields  # the stub has no session to read
    assert fields["arena"] == "off"


# --------------------------------------------------------------------------
# §14.5: a degradation is never silent
# --------------------------------------------------------------------------


def test_a_degraded_construction_says_so_at_warning(fake_fastembed, tmp_path, caplog):
    """Running one rung down means the ORT arena is ACTIVE — 5.10's whole point.

    It used to happen in silence, so a process holding the arena was
    indistinguishable from one that had disabled it. That is how a memory
    regression hides for five releases.
    """
    _FakeTextEmbedding.reject_session_options = True
    with caplog.at_level(logging.WARNING, logger="cognita.embeddings"):
        Embedder("fake-model", 4, tmp_path)._ensure_model()

    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("step 'arena-off' failed" in m for m in warnings)
    assert any("degraded step 'default'" in m and "arena is ACTIVE" in m for m in warnings)


def test_a_clean_construction_stays_quiet(fake_fastembed, tmp_path, caplog):
    with caplog.at_level(logging.WARNING, logger="cognita.embeddings"):
        Embedder("fake-model", 4, tmp_path)._ensure_model()
    assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []


def test_construct_reports_which_rung_it_landed_on(fake_fastembed, tmp_path):
    model, step = embeddings._construct_without_arena(
        _FakeTextEmbedding, {"model_name": "m"}, what="Embedder"
    )
    assert step == "arena-off"
    assert model.init_kwargs["extra_session_options"] == {"enable_cpu_mem_arena": False}

    _FakeCrossEncoder.reject_session_options = True
    _FakeCrossEncoder.reject_threads = True
    model, step = embeddings._construct_without_arena(
        _FakeCrossEncoder, {"model_name": "m", "threads": 4}, what="Reranker"
    )
    assert step == "no-threads"
    assert "threads" not in model.init_kwargs


def test_a_missing_reranker_is_announced_once(tmp_path, monkeypatch, caplog):
    """Search silently dropping to RRF order looks exactly like search working."""
    monkeypatch.setitem(sys.modules, "fastembed.rerank.cross_encoder", None)
    reranker = Reranker("fake-reranker", tmp_path)
    with caplog.at_level(logging.WARNING, logger="cognita.embeddings"):
        assert reranker.rerank("q", ["a"]) is None
        assert reranker.rerank("q", ["a"]) is None  # second call must not re-log

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "fall back to RRF order" in warnings[0].getMessage()


# --------------------------------------------------------------------------
# The walk
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_index_project_frames_the_walk_with_plan_and_done(tmp_path, caplog):
    from cognita import retrieval

    core = retrieval.RetrievalCore.__new__(retrieval.RetrievalCore)

    async def _locked(
        project, documents_dir, *, force=False, progress=None, job=None,
        before_removal=None,
    ):
        job.plan(files=7, force=force)
        record_batch(CPU_DEVICE, chunks=21, chars=210, elapsed=2.0)
        job.done(files=7, indexed=7, outcome="ok")
        return {"indexed": 7}

    with caplog.at_level(logging.DEBUG, logger="cognita.embed"):
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(core, "_index_project_locked", _locked)
            mp.setattr(retrieval, "release_to_os", lambda: None)
            assert await core.index_project("P", tmp_path, force=True) == {"indexed": 7}

    plan = _fields(_records(caplog, "embed.plan")[0])
    assert plan == {"project": "P", "walk": "project", "files": "7",
                    "force": "True", "decision": "cpu"}
    done = _fields(_records(caplog, "embed.done")[0])
    assert done["chunks"] == "21" and done["outcome"] == "ok"


@pytest.mark.asyncio
async def test_a_walk_that_raises_still_reports_what_it_embedded(tmp_path, caplog):
    """The only place that information exists is the job that just died."""
    from cognita import retrieval

    core = retrieval.RetrievalCore.__new__(retrieval.RetrievalCore)

    async def _boom(
        project, documents_dir, *, force=False, progress=None, job=None,
        before_removal=None,
    ):
        job.plan(files=7)
        record_batch(CPU_DEVICE, chunks=13, chars=130, elapsed=1.0)
        raise RuntimeError("walk exploded")

    with caplog.at_level(logging.DEBUG, logger="cognita.embed"):
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(core, "_index_project_locked", _boom)
            mp.setattr(retrieval, "release_to_os", lambda: None)
            with pytest.raises(RuntimeError, match="walk exploded"):
                await core.index_project("P", tmp_path)

    done = _fields(_records(caplog, "embed.done")[0])
    assert done["chunks"] == "13"
    assert done["outcome"] == "aborted"
