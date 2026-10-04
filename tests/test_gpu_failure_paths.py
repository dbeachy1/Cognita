"""Regression suite for the 6.0.2 review: the GPU failure paths nothing drove.

🔴 **Every test here covers a defect that shipped in 6.0.0 and that the existing
suite could not see**, because no test in the range ever drove `index_project`
with a non-`None` pool and `still_qualifies` was stubbed on a fake worker in
every case that touched it. The bugs were real and two of them were confirmed by
executing the shipped code; these are the executable versions of that.

The shape follows `test_gpu_host.py`: no GPU, no subprocess, fakes for anything
that would need a card.
"""

from __future__ import annotations

import logging

import pytest

from cognita import gpu_host
from cognita.gpu_host import (
    LEASE,
    GpuPool,
    WorkerStats,
    check_canary,
    embed_with_pool,
)
from cognita.gpu_probe import GIB, GpuDevice


class Config:
    gpu_enabled = True
    gpu_venv_python = "/tmp/fake/bin/python"
    gpu_batch_size = 4
    gpu_slice_chunks = 8
    gpu_reserve_vram_gb = 4.0
    gpu_max_busy_percent = 20
    gpu_device_ids: list[str] = []
    gpu_provider = "migraphx"
    gpu_worker_shutdown_s = 1.0
    gpu_worker_slice_timeout_s = 5.0
    gpu_worker_startup_timeout_s = 300.0
    gpu_canary_tolerance = 1e-4
    # Fields GpuWorker.start() reads when it builds the worker's argv.
    embedding_model = "BAAI/bge-small-en-v1.5"
    embedding_dimensions = 384
    gpu_model_cache_dir = ""
    models_cache_dir = "/tmp/models"
    gpu_fixed_seq_len = 512
    gpu_program_cache_dir = ""


def cpu_embed(texts):
    return [[float(len(t)), 0.5, -0.25] for t in texts]


def device(name="card1", pci="0000:03:00.0", total=32, free=32):
    return GpuDevice(name, pci, "uid-1", name, total * GIB, int(free * GIB), 0)


class FakeProbe:
    """A probe whose reported free VRAM the test moves between calls."""

    def __init__(self, dev):
        self.dev = dev

    def devices(self):
        return [self.dev]


# --------------------------------------------------------------------------
# §6.4 — yielding the device. The old arithmetic could NEVER fire.
# --------------------------------------------------------------------------


def _settled_worker(free_after_settle=28.0):
    """A worker that has completed one slice, so it has a settled baseline."""
    dev = device(free=32)
    probe = FakeProbe(dev)
    worker = gpu_host.GpuWorker(dev, Config(), probe)
    # Stand in for "first slice finished": the model is resident and this
    # worker's own allocation is now steady.
    probe.dev = device(free=free_after_settle)
    worker._settled_free = int(free_after_settle * GIB)
    worker.stats.peak_vram = int((32 - free_after_settle) * GIB)
    return worker, probe


def test_a_worker_keeps_the_card_while_only_its_own_allocation_is_held():
    """Steady state must NOT read as someone else arriving, or a rebuild yields
    the card to itself on its second slice."""
    worker, _ = _settled_worker()
    assert worker.still_qualifies() == (True, "ok")


def test_a_worker_yields_when_another_process_takes_the_headroom():
    """🔴 THE BUG: this returned (True, "ok") for every realistic card.

    `peak_vram` is `vram_total - vram_free`, i.e. the CARD's usage including
    other processes, and it was SUBTRACTED from the threshold — so the more VRAM
    somebody else took, the more negative the bar became and the more certain
    this worker was to keep the card. §6.4's whole promise was unreachable.
    """
    worker, probe = _settled_worker(free_after_settle=28.0)
    # Another process takes 26 GiB: free falls to 2 GiB, below the 4 GiB reserve.
    probe.dev = device(free=2.0)
    worker.stats.peak_vram = int(30 * GIB)  # card-wide peak follows it up
    ok, reason = worker.still_qualifies()
    assert ok is False, "a 26GiB allocation by another process must yield the card"
    assert "vram_free=2.00GB" in reason


def test_a_worker_with_no_settled_baseline_does_not_yield():
    """Before the first slice there is nothing to compare against; yielding then
    would abandon the card before it had done any work at all."""
    dev = device(free=32)
    worker = gpu_host.GpuWorker(dev, Config(), FakeProbe(dev))
    assert worker._settled_free is None
    assert worker.still_qualifies() == (True, "ok")


def test_a_vanished_device_yields():
    dev = device()
    worker = gpu_host.GpuWorker(dev, Config(), FakeProbe(device(pci="other")))
    assert worker.still_qualifies() == (False, "device disappeared")


# --------------------------------------------------------------------------
# §9.1 — the canary must see a dimension mismatch
# --------------------------------------------------------------------------


class DimWorker:
    """A worker returning a vector of the wrong WIDTH but a plausible prefix."""

    def __init__(self, width):
        self.device = device()
        self.stats = WorkerStats(device="card1", pci_address="0000:03:00.0")
        self.proc = object()
        self.terminated = False
        self._width = width

    def embed(self, texts, timeout, record=True):
        return [cpu_embed(texts)[0][: self._width]]

    def terminate(self, grace=None):
        self.terminated = True
        self.proc = None


def test_the_canary_refuses_a_device_that_returns_a_short_vector(caplog):
    """🔴 `zip(reference, candidate)` TRUNCATES to the shorter side, so a device
    returning a prefix-compatible short vector scored a tiny delta over that
    prefix and PASSED — the loudest possible "this provider is not computing
    what we think", invisible to the one check that exists to catch it."""
    worker = DimWorker(width=2)  # the CPU reference is 3 wide
    pool = GpuPool(workers=[worker], holder="test")
    with caplog.at_level(logging.ERROR):
        survivors = check_canary(pool, cpu_embed, 1e-4)
    assert survivors == []
    assert worker.terminated
    assert "different vector width" in caplog.text


def test_the_canary_still_accepts_a_matching_device():
    worker = DimWorker(width=3)
    pool = GpuPool(workers=[worker], holder="test")
    assert check_canary(pool, cpu_embed, 1e-4) == [worker]


def test_the_canary_stays_out_of_the_jobs_throughput_figures():
    """🔴 The canary is the FIRST inference on a device, so it pays the one-off
    25-38s MIGraphX shape compile (§5.1) — and it was counted as ordinary work.
    On the first live GPU walk a card doing ~73 chunks/s reported ~28, because
    ~30s of compile and one canary chunk sat inside its `elapsed`. §14.4
    nominates `rate` as the number answering "is the GPU still worth it", so
    that is the field it corrupted.

    The CPU reference is out too: a pure-GPU walk was logging
    `device=cpu batches=1 chunks=1` for it, and those chunks reached `est_error`.
    """
    from cognita.embed_telemetry import embed_job

    recorded = []

    class TimedWorker(DimWorker):
        def embed(self, texts, timeout, record=True):
            recorded.append(record)
            return [cpu_embed(texts)[0]]

    worker = TimedWorker(width=3)
    pool = GpuPool(workers=[worker], holder="test")
    with embed_job("P", walk="project") as job:
        check_canary(pool, cpu_embed, 1e-4)

    assert recorded == [False], "the canary must not be recorded as throughput"
    assert job.chunks == 0, (
        "neither the GPU canary nor its CPU reference is corpus work, so "
        "neither belongs in the job's chunk total"
    )
    assert job.devices == {}, "and neither should invent a device row"


# --------------------------------------------------------------------------
# The drain threads' slice-ownership contract
# --------------------------------------------------------------------------


class ExplodingWorker:
    """Raises something OUTSIDE the GpuUnavailable contract, as `struct.error`
    from `unpack_vectors` and a sysfs decode error both do."""

    def __init__(self, name="card1"):
        self.device = device(name)
        self.stats = WorkerStats(device=name, pci_address="0000:03:00.0")
        self.proc = object()
        self.terminated = False

    def embed(self, texts, timeout, record=True):
        raise ValueError("frame desync")

    def still_qualifies(self):
        return True, "ok"

    def terminate(self, grace=None):
        self.terminated = True
        self.proc = None


def test_an_unexpected_worker_error_does_not_lose_its_slice(caplog):
    """🔴 THE BUG: `drain` caught only GpuUnavailable, so the thread died via
    threading.excepthook — traceback to stderr, never to cognita.log — carrying
    a slice that was in neither `pending` nor `results`. The merge then raised
    `KeyError: 0` and the caller re-embedded the whole window per document."""
    texts = [f"text-{i}" for i in range(6)]
    pool = GpuPool(workers=[ExplodingWorker()], holder="test")
    with caplog.at_level(logging.ERROR):
        out = embed_with_pool(pool, texts, Config(), cpu_embed)
    assert out == cpu_embed(texts), "the vectors must still be correct and in order"
    assert pool.workers[0].terminated, "a broken worker must not get the next window"
    assert "slice failed unexpectedly" in caplog.text


def test_a_lost_slice_is_embedded_rather_than_misaligning_the_window(caplog):
    """Belt and braces behind the requeue contract: a missing index must never
    return short, because every vector after it would then belong to the wrong
    chunk."""
    texts = [f"text-{i}" for i in range(4)]
    pool = GpuPool(workers=[], holder="test")  # no workers: nothing can run
    out = embed_with_pool(pool, texts, Config(), cpu_embed)
    assert out == cpu_embed(texts)


# --------------------------------------------------------------------------
# The lease
# --------------------------------------------------------------------------


def test_shutting_a_pool_down_twice_cannot_release_someone_elses_lease():
    """🔴 `LEASE.release` matches on the HOLDER STRING and holder strings are not
    unique — every single-document write derives one from the project name. A
    second shutdown of an already-released pool would therefore free whichever
    pool holds the lease NOW, putting two jobs on the GPU at once. Teardown is
    reachable from the canary branch, the walk's `finally` and `start_pool`'s own
    error handler, so "called twice" is a matter of time."""
    holder = "proj:document"
    assert LEASE.acquire(holder)
    try:
        pool = GpuPool(workers=[], holder=holder)
        pool.shutdown()
        assert LEASE.holder is None
        other = "regression:second-job"
        assert LEASE.acquire(other)
        pool.shutdown()
        assert LEASE.holder == other
    finally:
        LEASE.release(LEASE.holder or "")


# --------------------------------------------------------------------------
# §8.2 — the walk must never leak a pool, however the canary fails
# --------------------------------------------------------------------------


class _StubPool:
    """A pool the walk can tear down, standing in for live workers."""

    def __init__(self):
        self.workers: list = []
        self.cpu_fallback_chunks = 0
        self.shutdowns = 0

    def shutdown(self):
        self.shutdowns += 1


def _gpu_core(store, emb, **kw):
    from cognita.retrieval import RetrievalCore

    return RetrievalCore(store, emb, gpu_config=Config(), **kw)


async def test_a_canary_that_raises_still_tears_the_pool_down(tmp_path):
    """🔴 THE BUG: `pool_holder["pool"]` was assigned AFTER `check_canary`, so
    anything raising in between left a live pool invisible to the walk's
    `finally` — worker subprocesses keeping their VRAM until the service exits,
    and a process-wide lease held forever, silently forcing every later walk and
    every later document write onto the CPU for the life of the service."""
    from tests.test_index_pipeline import FakeStore, RecordingEmbedder, write_corpus

    write_corpus(tmp_path, {"a.md": 4})
    pool = _StubPool()

    def boom(*args, **kwargs):
        raise RuntimeError("the CPU reference embedder is unavailable")

    store = FakeStore()
    core = _gpu_core(store, RecordingEmbedder(), gpu_min_chunks=1)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(gpu_host, "start_pool", lambda *a, **kw: pool)
        mp.setattr(gpu_host, "check_canary", boom)
        summary = await core.index_project("P", tmp_path)

    assert pool.shutdowns == 1, (
        "a pool whose canary raised was never torn down: its workers keep their "
        "VRAM and its lease is held for the life of the process"
    )
    # §10: the walk COMPLETES. A fault in the optional accelerator must never
    # fail an index the CPU could have finished on its own.
    assert summary["indexed"] == 1
    assert store.docs["a.md"], "the document must still be embedded, on the CPU"


async def test_trivial_real_work_never_pays_for_a_pool(tmp_path):
    """🔴 The estimate is a LOWER BOUND FROM FILE SIZES and cannot tell that a
    file is byte-identical. 5.20.0 moved the spin-up to the first window, which
    fixed the zero-work case and left this one: a big file is rewritten with the
    same bytes (so the estimator counts all its chunks and the walk then only
    touches its stat row) while one small note genuinely changed. Real work: a
    couple of chunks. Cost before this: the full ~33s spin-up, under the project
    write lock, refusing connector writes throughout."""
    from tests.test_index_pipeline import FakeStore, RecordingEmbedder, write_corpus

    write_corpus(tmp_path, {"small.md": 1})
    store, emb = FakeStore(), RecordingEmbedder()
    core = _gpu_core(store, emb, gpu_min_chunks=300)
    starts = []
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(gpu_host, "start_pool", lambda *a, **kw: starts.append(a) or None)
        summary = await core.index_project("P", tmp_path)

    assert summary["indexed"] == 1
    assert starts == [], (
        "a walk whose real work is a couple of chunks spun up the GPU anyway"
    )


async def test_a_walk_with_real_work_still_starts_the_pool(tmp_path):
    """The other side of the threshold: this must not have turned the GPU off."""
    from tests.test_index_pipeline import FakeStore, RecordingEmbedder, write_corpus

    write_corpus(tmp_path, {f"doc{i}.md": 40 for i in range(8)})
    core = _gpu_core(FakeStore(), RecordingEmbedder(), gpu_min_chunks=100)
    starts = []
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(gpu_host, "start_pool", lambda *a, **kw: starts.append(a) or None)
        await core.index_project("P", tmp_path)

    assert len(starts) == 1, "real work must still reach the GPU exactly once"


async def test_a_failed_producer_never_triggers_the_removal_sweep(tmp_path):
    """🔴 WHOLE-INDEX BLAST RADIUS. `_parse_ahead`'s `finally` always enqueues the
    sentinel, so a producer that died after N of M files looks EXACTLY like one
    that finished: `live_sources` holds N entries and the sweep deletes the other
    M-N — documents that are present, readable, and simply never reached. The
    walk returned outcome="ok", errors=[] and a large `removed` count."""
    from tests.test_index_pipeline import FakeStore, RecordingEmbedder, write_corpus
    from cognita import retrieval as retrieval_mod

    write_corpus(tmp_path, {f"doc{i}.md": 1 for i in range(6)})
    store, emb = FakeStore(), RecordingEmbedder()
    core = _gpu_core(store, emb, gpu_min_chunks=1)
    await core.index_project("P", tmp_path)
    assert len(store.docs) == 6
    store.sources = {s: None for s in store.docs}

    original = retrieval_mod.RetrievalCore._parse_ahead

    async def dying_producer(self, queue, *args, **kwargs):
        # Mirrors the real producer's `finally`, which ALWAYS enqueues the
        # sentinel — that is precisely why a dead producer is indistinguishable
        # from a finished one to the consumer, and why the sweep needed a
        # positive completion signal of its own.
        await queue.put(None)
        raise OSError("the documents mount went away mid-walk")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(retrieval_mod.RetrievalCore, "_parse_ahead", dying_producer)
        summary = await core.index_project("P", tmp_path)

    assert summary["removed"] == 0, (
        "the removal sweep ran on an incomplete live-document set and deleted "
        "documents that were never visited"
    )
    assert len(store.docs) == 6, "the corpus must survive a producer failure"
    assert any("removal sweep skipped" in e for e in summary["errors"])
    assert retrieval_mod.RetrievalCore._parse_ahead is original


# --------------------------------------------------------------------------
# §6.3 — spin-up is concurrent across devices
# --------------------------------------------------------------------------


class SlowCanaryWorker:
    """A worker whose canary holds its device open until every peer is in theirs.

    15.0: this used to `time.sleep(0.30)` and the test asserted the four
    canaries finished in under 0.9s of wall clock, a bet that the machine is
    fast enough right now (CLAUDE.md: no wall-clock tests). A barrier proves the
    same thing without a clock: all four canaries can only get past it if all
    four are running at once. Run in sequence, the first waits alone, the
    barrier breaks, and `met_peers` is False.
    """

    def __init__(self, name, barrier=None):
        self.device = device(name, pci=f"0000:0{name[-1]}:00.0")
        self.stats = WorkerStats(device=name, pci_address=self.device.pci_address)
        self.proc = object()
        self.terminated = False
        self._barrier = barrier
        self.met_peers = None

    def embed(self, texts, timeout, record=True):
        if self._barrier is not None:
            import threading as _th
            try:
                # The timeout is a hang guard only: in a correct run every peer
                # arrives, so the barrier always releases.
                self._barrier.wait()
                self.met_peers = True
            except _th.BrokenBarrierError:
                self.met_peers = False
        return cpu_embed(texts)

    def terminate(self, grace=None):
        self.terminated = True
        self.proc = None


def test_the_canary_proves_every_device_at_once():
    """🔴 §6.3: "wall-clock start-up is that of one device regardless of how many
    join". Both per-device startup steps ran BACK TO BACK — measured live on the
    two reference cards as card1 ready at 22:59:05.78 and card2 at 22:59:08.78,
    then canaries two seconds apart again. The canary is the expensive one,
    because the first inference on a device pays the shape compile, and the cost
    scaled linearly with device count — all inside the project write lock."""
    import threading as _th

    barrier = _th.Barrier(4, timeout=30)
    workers = [SlowCanaryWorker(f"card{i}", barrier=barrier) for i in range(1, 5)]
    pool = GpuPool(workers=list(workers), holder="test")

    survivors = check_canary(pool, cpu_embed, 1e-4)

    assert len(survivors) == 4, "every device passed and must be kept"
    assert [w.device.sysfs_name for w in survivors] == [
        "card1", "card2", "card3", "card4"
    ], "device ORDER must be preserved, or the logs and §14.4 rows shuffle"
    assert all(w.met_peers is True for w in workers), (
        "the four canaries were not all running at once: they ran in "
        "sequence, which is the §6.3 violation this pins"
    )


def test_one_device_still_takes_the_simple_path():
    """A single device must not pay for a thread pool to do one thing."""
    worker = SlowCanaryWorker("card1")
    pool = GpuPool(workers=[worker], holder="test")
    assert check_canary(pool, cpu_embed, 1e-4) == [worker]


def test_a_device_failing_its_canary_concurrently_does_not_cost_the_others():
    """The whole reason failures are dropped rather than raised — now that they
    are dropped from several threads at once."""
    good = SlowCanaryWorker("card1")
    bad = DimWorker(width=2)
    bad.device = device("card2", pci="0000:07:00.0")
    pool = GpuPool(workers=[good, bad], holder="test")

    survivors = check_canary(pool, cpu_embed, 1e-4)
    assert survivors == [good]
    assert bad.terminated


# --------------------------------------------------------------------------
# §7 — the gate's ceiling must follow the configured batch size
# --------------------------------------------------------------------------


def test_the_batch_ceiling_reproduces_both_measured_points():
    """§12.3 measured batch 64 at 3.62 GiB and batch 256 at 8.2 GiB. Anything
    that claims to derive the ceiling must land on both."""
    from cognita.gpu_probe import batch_ceiling_gb

    assert batch_ceiling_gb(64) == 3.62
    assert batch_ceiling_gb(256) == 8.2


def test_the_gate_ceiling_follows_the_configured_batch_size():
    """🔴 THE BUG: a literal 3.62 in the gate default AND in /healthz. That is
    the measured figure for batch 64 and wrong for every other value. Raising
    `gpu_batch_size` to fastembed's own default of 256 — the obvious "make it
    faster" knob, unwarned — left the gate admitting a card with 7.7 GiB free to
    a worker that needs 8.2, which OOMs at inference on every device in turn."""
    from cognita.retrieval import RetrievalCore
    from tests.test_index_pipeline import FakeStore, RecordingEmbedder

    class Shipped(Config):
        gpu_batch_size = 64    # the shipped default

    class Big(Config):
        gpu_batch_size = 256   # fastembed's own default

    core = RetrievalCore(FakeStore(), RecordingEmbedder(), gpu_config=Shipped())
    assert core.gpu_batch_ceiling_gb == 3.62
    big = RetrievalCore(FakeStore(), RecordingEmbedder(), gpu_config=Big())
    assert big.gpu_batch_ceiling_gb == 8.2, (
        "a batch-256 worker needs 8.2GiB; a gate sized for 64 admits a card it "
        "will then OOM on"
    )


def test_an_explicit_ceiling_still_wins():
    """Tests and a caller that measured its own hardware keep the override."""
    from cognita.retrieval import RetrievalCore
    from tests.test_index_pipeline import FakeStore, RecordingEmbedder

    core = RetrievalCore(FakeStore(), RecordingEmbedder(), gpu_config=Config(),
                         gpu_batch_ceiling_gb=1.5)
    assert core.gpu_batch_ceiling_gb == 1.5


# --------------------------------------------------------------------------
# §10 — a worker that wedges during construction must not hang the walk
# --------------------------------------------------------------------------


def test_a_worker_that_never_handshakes_times_out_rather_than_hanging():
    """🔴 THE BUG: `read_frame` on the startup handshake was an UNBOUNDED
    blocking read. The worker writes it only after model resolution, HIP init
    and the MIGraphX compile, any of which can wedge — and unbounded that hangs
    start_pool, ensure_pool and the walk, INSIDE the project write lock, where
    concurrent writes are REFUSED rather than delayed. `embed()` has always had
    a timeout; `start()` had none."""
    import subprocess
    import threading as _th

    class WedgedConfig(Config):
        # The real default is 300s — generous, because a first-run model
        # download plus a shape compile legitimately takes minutes. The point is
        # that it is BOUNDED, and the bound is configurable. 15.0: the test uses
        # 0, so `start()`'s join returns at once with the reader still blocked;
        # it used to be 0.5s of real waiting plus an elapsed-time assertion
        # (CLAUDE.md: no wall-clock tests). The reader blocks on an event that
        # only the test's cleanup sets, so no timing is involved at all.
        gpu_worker_startup_timeout_s = 0
        gpu_worker_shutdown_s = 0

    class SilentProc:
        """A worker that opens its pipes and then never answers."""

        stdin = stderr = stdout = None
        pid = -1

        def poll(self):
            return None

        def wait(self, timeout=None):
            return 0

        def terminate(self):
            pass

        def kill(self):
            pass

    dev = device()
    worker = gpu_host.GpuWorker(dev, WedgedConfig(), FakeProbe(dev))
    never_answers = _th.Event()
    try:
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(subprocess, "Popen", lambda *a, **kw: SilentProc())
            mp.setattr(gpu_host, "_worker_library_path", lambda *a, **kw: "")
            mp.setattr(gpu_host.GpuWorker, "_start_stderr_relay", lambda self: None)
            mp.setattr(gpu_host, "read_frame", lambda stream: never_answers.wait())
            # An unbounded read would block here forever, with `read_frame`
            # waiting on an event nothing sets until the `finally` below.
            # Returning at all is the proof that the read is bounded.
            with pytest.raises(gpu_host.GpuUnavailable) as exc:
                worker.start()
    finally:
        never_answers.set()  # release the abandoned reader thread

    assert "handshake" in str(exc.value)
    assert worker.stats.failed_reason == "handshake timeout"


# --------------------------------------------------------------------------
# §4.4 — one job, one pool, one lease for a whole bulk caller
# --------------------------------------------------------------------------


async def test_a_bulk_job_starts_exactly_one_pool_for_many_files(tmp_path):
    """🔴 THE §4.4 DEFECT, which the design names by its worked example. A bulk
    caller looping `index_file` decided per file, and BOTH outcomes were wrong:
    modest files each fell under `gpu_min_chunks` so the GPU was never used on
    the workload it exists for, and large files each paid a full pool spin-up —
    subprocess, model load, a ~25-38s MIGraphX shape compile, canary, teardown —
    which is far slower than just staying on the CPU."""
    from tests.test_index_pipeline import FakeStore, RecordingEmbedder, write_corpus

    write_corpus(tmp_path, {f"doc{i}.md": 5 for i in range(12)})
    files = sorted(tmp_path.glob("*.md"))
    core = _gpu_core(FakeStore(), RecordingEmbedder(), gpu_min_chunks=10)

    starts = []
    pool = _StubPool()
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(gpu_host, "start_pool", lambda *a, **kw: starts.append(a) or pool)
        mp.setattr(gpu_host, "check_canary", lambda p, *a, **kw: [object()])
        mp.setattr(gpu_host, "embed_with_pool",
                   lambda p, texts, cfg, cpu: cpu(texts))
        async with core.bulk_gpu_job("P", "copy_directory", files, tmp_path):
            for f in files:
                await core.index_file("P", tmp_path, f)

    assert len(starts) == 1, (
        f"a 12-file bulk job started {len(starts)} pools; §4.4 requires one"
    )
    assert pool.shutdowns == 1, "the pool must be torn down once, at the end"
    assert core._bulk_pool is None, "the bulk pool must not outlive the job"


async def test_a_bulk_job_under_the_threshold_starts_no_pool(tmp_path):
    """The aggregate decides. A copy of genuinely tiny files stays on the CPU."""
    from tests.test_index_pipeline import FakeStore, RecordingEmbedder, write_corpus

    write_corpus(tmp_path, {f"doc{i}.md": 1 for i in range(3)})
    files = sorted(tmp_path.glob("*.md"))
    core = _gpu_core(FakeStore(), RecordingEmbedder(), gpu_min_chunks=300)

    starts = []
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(gpu_host, "start_pool", lambda *a, **kw: starts.append(a) or None)
        async with core.bulk_gpu_job("P", "copy_directory", files, tmp_path):
            for f in files:
                await core.index_file("P", tmp_path, f)

    assert starts == []


async def test_a_nested_caller_cannot_close_the_enclosing_jobs_summary(caplog):
    """🔴 THE TRAP THE §4.4 FIX WOULD OTHERWISE SPRING. `embed_job` nests by
    JOINING, and `index_file` closes its job unconditionally — so the first
    file's `done(files=1)` would latch the shared job and permanently close it.
    The other 499 files' chunks would accumulate into a job whose summary had
    already printed, and a 500-document copy would be logged as costing six
    chunks: §14.4's named failure, "the line everyone reads first was wrong"."""
    from cognita.embed_telemetry import embed_job, record_batch

    with caplog.at_level(logging.INFO):
        with embed_job("P", "copy_directory") as outer:
            with embed_job("P", "index_file") as inner:
                assert inner is outer, "nesting must JOIN, not open a second job"
                record_batch("cpu", chunks=4, chars=40, elapsed=0.01)
                inner.done(files=1, indexed=1)   # must be ignored
            record_batch("cpu", chunks=8, chars=80, elapsed=0.02)
            outer.done(files=500, indexed=500)

    lines = [r.getMessage() for r in caplog.records if "embed.done" in r.getMessage()]
    assert len(lines) == 1, f"expected one summary, got {len(lines)}: {lines}"
    assert "files=500" in lines[0]
    assert "chunks=12" in lines[0], (
        "the summary must cover the WHOLE job, not just its first file"
    )


# --------------------------------------------------------------------------
# The property the window rewrite could most plausibly have broken
# --------------------------------------------------------------------------


@pytest.mark.parametrize("spec", [
    {f"doc{i}.md": 1 for i in range(20)},          # many small, several per window
    {f"doc{i}.md": 3 for i in range(64)},          # exactly EMBED_WINDOW_DOCS
    {"huge.md": 40},                                # one document over the window
    {"a.md": 1, "b.md": 9, "c.md": 2, "d.md": 17},  # uneven
])
async def test_every_stored_chunk_carries_its_own_vector(tmp_path, spec):
    """🔴 The window flattens chunks from MANY documents into one embed call and
    then scatters the vectors back. An off-by-one there gives chunk N of document
    A the vector of document B — searchable garbage that looks completely fine
    from every count-based assertion, and the existing suite only checks counts
    and call sizes. `HashEmbedder` is a pure function of the text, so the whole
    property is one comparison per chunk."""
    from tests.test_index_pipeline import FakeStore, RecordingEmbedder, write_corpus

    write_corpus(tmp_path, spec)
    store, emb = FakeStore(), RecordingEmbedder()
    from cognita.retrieval import RetrievalCore

    await RetrievalCore(store, emb).index_project("P", tmp_path)

    checked = 0
    for source, chunks in store.docs.items():
        for chunk in chunks:
            expected = emb.embed([chunk.content])[0]
            assert chunk.embedding == expected, (
                f"{source} chunk {chunk.chunk_id} carries another chunk's vector"
            )
            checked += 1
    assert checked, "no chunks were stored, so this proved nothing"
