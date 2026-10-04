"""6.2 — the warm GPU pool (DESIGN-6.2).

The behavior under test, in one sentence: **a pool that is already up takes the
work whatever its size, and a pool that is idle goes away on a timer.**

6.0 tore a pool down at the end of every job, so a 10-chunk edit arriving four
seconds after a rebuild finished ran on the CPU beside two cards that had been
loaded with the model until a moment earlier — and the next document large
enough to clear `gpu_min_chunks` paid the ~3s worker spin-up all over again.

Shape follows `test_gpu_failure_paths.py`: no GPU, no subprocess, fakes for
anything that would need a card. The `GpuPool` and `WarmPool` under test are the
real ones — only the workers are doubles, because the teardown contract
(terminate every worker, release the lease, report the device rows) is exactly
what several of these tests are about.
"""

from __future__ import annotations

import threading

import pytest

from cognita import gpu_host
from cognita import retrieval as retrieval_mod
from cognita.gpu_host import LEASE, GpuPool, WorkerStats
from cognita.gpu_warm import WarmPool

# --------------------------------------------------------------------------
# Doubles
# --------------------------------------------------------------------------


class Config:
    """A GPU config with 6.2's key set. Mirrors test_gpu_failure_paths.Config."""

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
    gpu_idle_linger_s = 30.0
    # 0 = 6.2.0's behavior (every chunk to a warm pool). The floor has its own
    # config below, so the tests that are about the CLAIM are not also about the
    # threshold.
    gpu_warm_min_chunks = 0
    embedding_model = "BAAI/bge-small-en-v1.5"
    embedding_dimensions = 384
    gpu_model_cache_dir = ""
    models_cache_dir = "/tmp/models"
    gpu_fixed_seq_len = 512
    gpu_program_cache_dir = ""


class _Proc:
    """Stands in for `subprocess.Popen`. `poll()` is what `_is_live` reads."""

    def __init__(self, alive: bool = True):
        self._alive = alive

    def poll(self):
        return None if self._alive else 1

    def die(self):
        self._alive = False


class FakeWorker:
    def __init__(self, name: str = "card1", chunks: int = 0):
        self.stats = WorkerStats(device=name, pci_address="0000:03:00.0")
        self.stats.chunks = chunks
        self.proc = _Proc()
        self.terminations = 0

    def terminate(self, grace=None):
        self.terminations += 1
        self.proc = None


def make_pool(holder: str = "test:job", workers=None, take_lease: bool = True) -> GpuPool:
    """A real GpuPool over fake workers, holding the real lease.

    The lease matters: half of what the reaper has to get right is releasing it,
    and a pool that never took it cannot demonstrate that.
    """
    if take_lease:
        LEASE.acquire(holder)
    return GpuPool(workers=list(workers or [FakeWorker()]), holder=holder)


@pytest.fixture(autouse=True)
def _free_lease():
    """No test may leak the process-wide lease into the next one."""
    yield
    held = LEASE.holder
    if held:
        LEASE.release(held)


class FakeTimer:
    """Stands in for `threading.Timer` behind `WarmPool`'s timer seam.

    Records what `park()` armed and never fires on its own: a test that wants
    the reaper's wake-up calls `fire()`, exactly as the timer thread would.
    """

    def __init__(self, interval: float, function):
        self.interval = interval
        self.function = function
        self.daemon = False
        self.name = ""
        self.started = False
        self.cancelled = False

    def start(self):
        self.started = True

    def cancel(self):
        self.cancelled = True

    def fire(self):
        if not self.cancelled:
            self.function()


@pytest.fixture
def warm():
    """A private WarmPool on a controllable clock.

    Private rather than the module-global `WARM` so tests cannot leak parked
    pools into each other, and so the clock can be moved without sleeping.
    Superseded: this fixture used to arm the real `threading.Timer` (30s
    intervals), which meant a test stalled for 30 real seconds could be reaped
    by a wall-clock timer behind its fake clock's back. It now injects
    `FakeTimer`, so nothing here fires unless a test says so; the one test that
    proves the REAL timer is armed and fires builds its own `WarmPool()`.
    `shutdown_now` in the teardown still cancels whatever is armed.
    """
    now = {"t": 1_000.0}
    pool = WarmPool(clock=lambda: now["t"], timer_factory=FakeTimer)
    pool.advance = lambda seconds: now.__setitem__("t", now["t"] + seconds)  # type: ignore[attr-defined]
    yield pool
    pool.shutdown_now("test_teardown")


# --------------------------------------------------------------------------
# park / claim — the state that did not exist before 6.2
# --------------------------------------------------------------------------


def test_a_finished_pool_is_parked_and_claimed_back_without_a_new_start(warm):
    """🔴 THE FEATURE. 6.0's three teardown sites all called `pool.shutdown()`
    unconditionally, so the next job — however soon it arrived — started from
    cold: subprocess, model load, ~33s shape compile, canary."""
    pool = make_pool()
    assert warm.park(pool, 30.0) is True
    warm.advance(4.0)  # the next connector edit, four seconds later

    claimed = warm.claim()

    assert claimed is pool, "the next job must ride the pool that is already up"
    assert pool.workers[0].terminations == 0, "nothing was torn down in between"
    assert LEASE.holder == "test:job", "the lease is held across the linger window"


def test_a_claim_removes_the_pool_so_two_jobs_cannot_share_it(warm):
    """🔴 `claim()` TAKES rather than lends. The lease makes 'one pool at a
    time' true and says nothing about who is embedding right now; two jobs
    holding one pool would interleave slices on workers that serialize them."""
    pool = make_pool()
    warm.park(pool, 30.0)

    assert warm.claim() is pool
    assert warm.claim() is None, "a claimed pool must not be handed out twice"


def test_a_claim_inside_the_window_resets_the_idle_clock(warm):
    """The burst is the case this exists for: work every 20s must keep the cards
    indefinitely, and only a genuinely idle period may reap them."""
    pool = make_pool()
    warm.park(pool, 30.0)
    warm.advance(20.0)
    warm.park(warm.claim(), 30.0)   # a second job, inside the window
    warm.advance(20.0)              # 40s after the FIRST park

    assert warm.reap_if_idle() is False, "the second park moved the deadline"
    assert warm.parked is pool

    warm.advance(11.0)              # now genuinely idle for 31s
    assert warm.reap_if_idle() is True


def test_a_stale_timer_cannot_reap_a_pool_that_was_re_parked(warm):
    """The timer armed by the FIRST park is still in flight when a claim-and-
    park moves the deadline. `reap_if_idle` re-checks the deadline rather than
    trusting the wake-up that called it — otherwise a busy service would lose
    its pool 30s after the first job regardless of what happened since."""
    pool = make_pool()
    warm.park(pool, 30.0)
    warm.advance(29.0)
    warm.park(warm.claim(), 30.0)

    warm.advance(2.0)               # the first timer's moment arrives
    warm._on_timer()                # exactly what that thread would run

    assert warm.parked is pool, "the stale timer reaped a pool that was in use"
    assert pool.workers[0].terminations == 0


# --------------------------------------------------------------------------
# The reaper — §8.2 is on a timer now, not relaxed
# --------------------------------------------------------------------------


def test_the_reaper_tears_the_pool_down_and_frees_the_lease(warm):
    """🔴 A parked pool that is never reaped is precisely the leak DESIGN-6.0
    §8.2 exists to prevent, and it would be INVISIBLE: the CPU fallback works,
    so the only symptom is that the GPU is never used again until restart."""
    worker = FakeWorker()
    pool = make_pool(workers=[worker])
    warm.park(pool, 30.0)

    warm.advance(30.1)
    assert warm.reap_if_idle() is True

    assert worker.terminations == 1, "the worker's VRAM must actually come back"
    assert LEASE.holder is None, "the next job must be able to start a pool"
    assert warm.parked is None


def test_the_reaper_fires_on_its_own_timer_without_anyone_asking(caplog):
    """The deadline arithmetic above is driven by a fake clock; this proves the
    timer that has to notice it is really armed. A reaper that only works when
    someone calls it is not a reaper."""
    # The REAL `threading.Timer` (default factory) on a clock that reads the
    # park moment once and "expired" ever after. The old version used the real
    # clock and polled `parked` with sleeps; on Windows a timer can wake a hair
    # before the real deadline, so the re-check in `reap_if_idle` could decline
    # and the poll would run out. Now the reap is certain whenever the timer
    # fires, and the test waits on the timer thread itself finishing.
    readings = [1_000.0]
    warm = WarmPool(clock=lambda: readings.pop(0) if readings else 1_000_000.0)
    worker = FakeWorker()
    pool = make_pool(workers=[worker])
    # Park under the pool's own (re-entrant) lock so the timer cannot reap and
    # clear `_timer` before the test has taken a reference to it.
    with warm._lock:
        warm.park(pool, 0.001)
        timer = warm._timer
    assert isinstance(timer, threading.Timer), "park() must arm a real threading.Timer"

    # Hang guard only: a started threading.Timer always runs its function and
    # exits, whatever the machine load.
    timer.join(timeout=5.0)
    assert not timer.is_alive(), "the linger timer never fired"

    assert warm.parked is None, "the linger timer never fired"
    assert worker.terminations == 1
    assert LEASE.holder is None


def test_shutdown_now_tears_down_whatever_is_parked_and_is_idempotent(warm):
    """Wired into `LocalEngineHost.shutdown` and into `atexit`, so it runs on
    paths where raising would be invisible or would print a shutdown-time
    traceback."""
    worker = FakeWorker()
    warm.park(make_pool(workers=[worker]), 30.0)

    assert warm.shutdown_now("service_shutdown") is True
    assert warm.shutdown_now("service_shutdown") is False, "nothing left to do"
    assert worker.terminations == 1
    assert LEASE.holder is None


# --------------------------------------------------------------------------
# What must never be parked, and what must never be handed out
# --------------------------------------------------------------------------


def test_a_pool_whose_workers_died_while_parked_is_never_handed_out(warm, caplog):
    """🔴 Nobody is embedding during the linger window, so a worker that is OOM-
    killed or lost to a driver reset dies QUIETLY — only `poll()` says so. Handed
    out, it would drain the whole window on the CPU while reporting a GPU job,
    and its lease would stay held by a corpse for the life of the process."""
    worker = FakeWorker()
    pool = make_pool(workers=[worker])
    warm.park(pool, 30.0)
    worker.proc.die()

    claimed = warm.claim()

    assert claimed is None, "a dead pool must never be offered to a job"
    assert LEASE.holder is None, "and it must not keep the lease"
    assert "workers_died" in caplog.text


def test_linger_zero_reproduces_the_6_1_teardown(warm):
    """The escape hatch has to be a real one: `gpu_idle_linger_s: 0` must tear
    the pool down at the end of every job, exactly as 6.1 did."""
    worker = FakeWorker()
    pool = make_pool(workers=[worker])

    assert warm.park(pool, 0.0) is False
    assert warm.parked is None
    assert worker.terminations == 1
    assert LEASE.holder is None


def test_a_pool_with_no_live_workers_is_not_parked(warm):
    """Parking one would advertise a GPU that cannot embed, and every claim for
    the next 30 seconds would fall back to the CPU after paying for the claim."""
    worker = FakeWorker()
    worker.proc.die()
    pool = make_pool(workers=[worker])

    assert warm.park(pool, 30.0) is False
    assert warm.parked is None


def test_parking_a_second_pool_evicts_the_first_rather_than_leaking_it(warm):
    """The lease should make this unreachable. If it is ever reached, two live
    pools is the one state that costs real VRAM, so the older one goes NOW —
    never 'whichever the timer gets to first'."""
    first_worker, second_worker = FakeWorker("card1"), FakeWorker("card2")
    first = make_pool("first", [first_worker])
    second = GpuPool(workers=[second_worker], holder="second")
    warm.park(first, 30.0)

    assert warm.park(second, 30.0) is True
    assert warm.parked is second
    assert first_worker.terminations == 1, "the evicted pool must be torn down"
    assert second_worker.terminations == 0


def test_status_reports_the_warm_state_for_healthz(warm):
    """A held lease no longer means 'a job is running' (6.2), so /healthz has to
    be able to say which of the two it is."""
    assert warm.status() == {"warm": False}
    warm.park(make_pool(workers=[FakeWorker("card1")]), 30.0)
    warm.advance(4.0)

    status = warm.status()

    assert status["warm"] is True
    assert status["devices"] == ["card1"]
    assert status["idle_s"] == 4.0
    assert status["expires_in_s"] == 26.0


def test_status_never_blocks_behind_a_teardown(warm):
    """`pool.shutdown()` waits on process exits and then polls sysfs for the
    VRAM to come back — seconds, under the lock. Liveness must not queue behind
    it: /healthz answering is worth more than /healthz being complete."""
    warm.park(make_pool(workers=[FakeWorker()]), 30.0)
    released = threading.Event()
    entered = threading.Event()

    def hold_the_lock():
        with warm._lock:
            entered.set()
            # No timeout here on purpose: a 5s cap let the holder drop the lock
            # on its own if the test thread stalled, turning a stall into a
            # wrong status. `released` is always set in the test's `finally`,
            # and the thread is a daemon, so this cannot hang the run.
            released.wait()

    holder = threading.Thread(target=hold_the_lock, daemon=True)
    holder.start()
    # Hang guards only: the holder always sets `entered` once it has the lock,
    # and always exits once `released` is set.
    assert entered.wait(5.0), "the lock holder never took the lock"
    try:
        assert warm.status() == {"warm": "reaping"}
    finally:
        released.set()
        holder.join(5.0)
    assert not holder.is_alive(), "the lock holder never let go"


# --------------------------------------------------------------------------
# The indexing paths — claim BEFORE the threshold, at all three sites
# --------------------------------------------------------------------------


def _core(store, embedder, warm_pool, **kw):
    """A GPU-capable core whose warm slot is the test's private one."""
    core = retrieval_mod.RetrievalCore(store, embedder, gpu_config=Config(), **kw)
    return core


@pytest.fixture
def gpu_calls(warm, monkeypatch):
    """Patch the two hardware seams and record what the core reached for.

    `embed_with_pool` returns the CPU embedder's own vectors, so the shapes are
    right and every downstream assertion about the INDEX still holds — the
    question these tests ask is only which device was asked.
    """
    monkeypatch.setattr(retrieval_mod, "WARM", warm)
    calls = {"start_pool": 0, "pooled_embeds": []}

    def fake_embed_with_pool(pool, texts, config, cpu_embed):
        calls["pooled_embeds"].append(len(texts))
        for w in pool.workers:
            w.stats.chunks += len(texts)
        return cpu_embed(texts)

    def fake_start_pool(*args, **kwargs):
        calls["start_pool"] += 1
        return make_pool(f"started:{calls['start_pool']}", [FakeWorker()])

    monkeypatch.setattr(gpu_host, "embed_with_pool", fake_embed_with_pool)
    monkeypatch.setattr(gpu_host, "start_pool", fake_start_pool)
    monkeypatch.setattr(gpu_host, "check_canary", lambda pool, *a, **kw: pool.workers)
    return calls


async def test_a_small_document_rides_a_warm_pool(tmp_path, warm, gpu_calls):
    """🔴 THE HEADLINE, and the thing Cognita could not do before 6.2.

    `gpu_min_chunks` (300) answers "is this worth a ~3s cold start?". It is the
    right question when the answer costs a start-up and the WRONG question when
    the cards are already loaded — there the comparison is a pipe round-trip
    against a CPU embed. This document is nowhere near the threshold and must
    still land on the GPU."""
    from tests.test_index_pipeline import FakeStore, RecordingEmbedder, write_corpus

    write_corpus(tmp_path, {"note.md": 2})
    core = _core(FakeStore(), RecordingEmbedder(), warm, gpu_min_chunks=300)
    warm.park(make_pool("previous:job", [FakeWorker()]), 30.0)

    await core.index_file("P", tmp_path, tmp_path / "note.md")

    assert gpu_calls["pooled_embeds"], (
        "a small write ran on the CPU while a pool sat warm with the model loaded"
    )
    assert gpu_calls["start_pool"] == 0, "it must not have started a second pool"


async def test_a_small_document_with_nothing_warm_still_uses_the_cpu(
    tmp_path, warm, gpu_calls
):
    """The other side of the rule, and the one that keeps 6.2 honest: with no
    pool up, `gpu_min_chunks` means exactly what it always meant. A two-chunk
    edit must not start hardware."""
    from tests.test_index_pipeline import FakeStore, RecordingEmbedder, write_corpus

    write_corpus(tmp_path, {"note.md": 2})
    core = _core(FakeStore(), RecordingEmbedder(), warm, gpu_min_chunks=300)

    await core.index_file("P", tmp_path, tmp_path / "note.md")

    assert gpu_calls["start_pool"] == 0, "a two-chunk edit paid for a cold start"
    assert gpu_calls["pooled_embeds"] == []


async def test_a_document_that_starts_a_pool_parks_it_for_the_next_edit(
    tmp_path, warm, gpu_calls
):
    """The first half of the burst. Without this the ~33s start-up the big
    document just paid is thrown away, and the next one pays it again."""
    from tests.test_index_pipeline import FakeStore, RecordingEmbedder, write_corpus

    write_corpus(tmp_path, {"manual.md": 40})
    core = _core(FakeStore(), RecordingEmbedder(), warm, gpu_min_chunks=1)

    await core.index_file("P", tmp_path, tmp_path / "manual.md")

    assert gpu_calls["start_pool"] == 1
    assert warm.parked is not None, "the pool it paid for was thrown away"
    assert warm.parked.workers[0].terminations == 0


async def test_the_walk_claims_a_warm_pool_even_when_the_estimate_said_cpu(
    tmp_path, warm, gpu_calls
):
    """`index_project`'s gate is `estimate.use_gpu` AND 300 accumulated real
    chunks. Both exist to decide whether to PAY for a pool; neither applies to
    one that is already running."""
    from tests.test_index_pipeline import FakeStore, RecordingEmbedder, write_corpus

    write_corpus(tmp_path, {"a.md": 1, "b.md": 1})
    core = _core(FakeStore(), RecordingEmbedder(), warm, gpu_min_chunks=300)
    warm.park(make_pool("previous:job", [FakeWorker()]), 30.0)

    summary = await core.index_project("P", tmp_path)

    assert summary["indexed"] == 2
    assert gpu_calls["pooled_embeds"], "the walk ignored a pool that was already up"
    assert gpu_calls["start_pool"] == 0


async def test_a_bulk_job_claims_a_warm_pool_instead_of_starting_its_own(
    tmp_path, warm, gpu_calls
):
    """§4.4's bulk seam gets the same treatment: a copy of 20 small files is
    exactly the job the threshold is right to refuse a cold start for, and
    exactly the job that should ride cards a rebuild left running."""
    from tests.test_index_pipeline import FakeStore, RecordingEmbedder, write_corpus

    write_corpus(tmp_path, {"a.md": 1, "b.md": 1})
    core = _core(FakeStore(), RecordingEmbedder(), warm, gpu_min_chunks=300)
    warm.park(make_pool("previous:job", [FakeWorker()]), 30.0)
    files = [tmp_path / "a.md", tmp_path / "b.md"]

    async with core.bulk_gpu_job("P", "copy_directory", files, tmp_path) as pool:
        assert pool is not None, "the bulk job left a warm pool idle"
        for f in files:
            await core.index_file("P", tmp_path, f)

    assert gpu_calls["start_pool"] == 0
    assert gpu_calls["pooled_embeds"], "the documents did not reach the pool"
    assert warm.parked is not None, "and the bulk job must hand it back"


async def test_a_pool_whose_embed_raised_is_never_parked(tmp_path, warm, monkeypatch):
    """🔴 `embed_with_pool` absorbs every failure it knows about, so an exception
    out of it means something outside that contract broke. Parking such a pool
    would offer it to every small write for the rest of the linger window —
    spreading one job's fault across all of them, and each one paying a claim to
    discover it."""
    from tests.test_index_pipeline import FakeStore, RecordingEmbedder, write_corpus

    monkeypatch.setattr(retrieval_mod, "WARM", warm)

    def exploding(pool, texts, config, cpu_embed):
        raise RuntimeError("frame desync")

    monkeypatch.setattr(gpu_host, "embed_with_pool", exploding)
    write_corpus(tmp_path, {"note.md": 2})
    store = FakeStore()
    core = _core(store, RecordingEmbedder(), warm, gpu_min_chunks=300)
    worker = FakeWorker()
    warm.park(make_pool("previous:job", [worker]), 30.0)

    await core.index_file("P", tmp_path, tmp_path / "note.md")

    assert warm.parked is None, "a pool that failed was offered to the next job"
    assert worker.terminations == 1
    # §10: the accelerator fault costs time, never the document.
    assert store.docs["note.md"], "the document must still be indexed, on the CPU"


async def test_the_decision_correction_counts_this_jobs_chunks_not_the_pools(
    tmp_path, warm, gpu_calls, caplog, monkeypatch
):
    """🔴 A warm pool arrives carrying a previous job's chunk counters, so
    `any(w.stats.chunks for w in pool.workers)` — the check that corrects a
    `decision=gpu` which embedded nothing — is true from the moment it is
    claimed, and would never fire again on any pool after the first. The
    comparison has to be a DELTA against the baseline taken at acquisition."""
    import logging

    from tests.test_index_pipeline import FakeStore, RecordingEmbedder, write_corpus

    # The case the correction was written for: every device yields (§6.4) or
    # dies, and `embed_with_pool` drains the whole window on the CPU. The pool
    # embedded nothing, so the summary must not call this a GPU walk — and the
    # pool it claimed arrives with 5,000 chunks of a previous job's history.
    def every_device_yields(pool, texts, config, cpu_embed):
        pool.cpu_fallback_chunks += len(texts)
        return cpu_embed(texts)

    monkeypatch.setattr(gpu_host, "embed_with_pool", every_device_yields)
    write_corpus(tmp_path, {"a.md": 1})
    store = FakeStore()
    core = _core(store, RecordingEmbedder(), warm, gpu_min_chunks=300)
    warm.park(make_pool("previous:job", [FakeWorker(chunks=5000)]), 30.0)

    with caplog.at_level(logging.INFO, logger="cognita.embed"):
        await core.index_project("P", tmp_path)

    done = [r.message for r in caplog.records if r.message.startswith("embed.done")]
    assert done, "the walk must still emit its summary"
    assert "decision=cpu" in done[-1], (
        "a walk that embedded nothing reported decision=gpu on the strength of "
        "an earlier job's chunks"
    )


async def test_linger_off_tears_the_pool_down_at_the_end_of_the_job(
    tmp_path, warm, gpu_calls
):
    """End to end through the core: with `gpu_idle_linger_s: 0` the whole
    feature is off and 6.1's behavior is what remains."""
    from tests.test_index_pipeline import FakeStore, RecordingEmbedder, write_corpus

    class NoLinger(Config):
        gpu_idle_linger_s = 0.0

    write_corpus(tmp_path, {"manual.md": 40})
    core = retrieval_mod.RetrievalCore(
        FakeStore(), RecordingEmbedder(), gpu_config=NoLinger(), gpu_min_chunks=1
    )

    await core.index_file("P", tmp_path, tmp_path / "manual.md")

    assert gpu_calls["start_pool"] == 1, "the document still uses the GPU"
    assert warm.parked is None, "nothing may linger when the linger is off"


# --------------------------------------------------------------------------
# 6.2.1 — the warm floor. A claim is cheap, not free.
# --------------------------------------------------------------------------


class FlooredConfig(Config):
    """kei's shipped geometry: a warm claim is worth making from 16 chunks up."""

    gpu_warm_min_chunks = 16


async def test_a_trivial_write_leaves_the_warm_pool_alone(tmp_path, warm, gpu_calls):
    """🔴 6.2.0 SHIPPED THIS WRONG AND THE MEASUREMENT CAUGHT IT. "A pool that is
    up takes every chunk" assumed a claim is free. Measured on kei minutes after
    that deploy: four consecutive 1-chunk writes through a warm pool took
    1.813 / 1.787 / 1.791 / 1.812 s — flat, so it is per-call — against ~0.025s
    for the same chunk on the CPU. The round-trip is ~1.8s before a single
    vector is computed, and a one-paragraph note never repays it."""
    from tests.test_index_pipeline import FakeStore, RecordingEmbedder, write_corpus

    write_corpus(tmp_path, {"note.md": 2})   # ~2 chunks, far under the floor
    core = retrieval_mod.RetrievalCore(
        FakeStore(), RecordingEmbedder(), gpu_config=FlooredConfig(), gpu_min_chunks=300
    )
    warm.park(make_pool("previous:job", [FakeWorker()]), 30.0)

    await core.index_file("P", tmp_path, tmp_path / "note.md")

    assert gpu_calls["pooled_embeds"] == [], (
        "a 2-chunk write paid ~1.8s of GPU round-trip for work the CPU does in "
        "~0.025s"
    )
    assert warm.parked is not None, "and the pool must be left warm for real work"


async def test_a_document_above_the_floor_still_claims(tmp_path, warm, gpu_calls):
    """The other side, and the reason the floor is 16 and not 300: everything
    from a small document upward wins. Measured: 172 chunks is 5.0s warm against
    21.1s on the CPU."""
    from tests.test_index_pipeline import FakeStore, RecordingEmbedder, write_corpus

    write_corpus(tmp_path, {"note.md": 40})
    core = retrieval_mod.RetrievalCore(
        FakeStore(), RecordingEmbedder(), gpu_config=FlooredConfig(), gpu_min_chunks=300
    )
    warm.park(make_pool("previous:job", [FakeWorker()]), 30.0)

    await core.index_file("P", tmp_path, tmp_path / "note.md")

    assert gpu_calls["pooled_embeds"], (
        "a document well above the floor and well below gpu_min_chunks must "
        "still ride a pool that is already up — that is the whole feature"
    )


async def test_the_two_thresholds_are_not_the_same_threshold(tmp_path, warm, gpu_calls):
    """They price different things — a ~1.8s round-trip and a ~33s shape compile
    — so collapsing them would either make the warm path useless (at 300) or
    make cold starts fire on trivial writes (at 16). With NOTHING warm, a
    40-chunk document must stay on the CPU."""
    from tests.test_index_pipeline import FakeStore, RecordingEmbedder, write_corpus

    write_corpus(tmp_path, {"note.md": 40})
    core = retrieval_mod.RetrievalCore(
        FakeStore(), RecordingEmbedder(), gpu_config=FlooredConfig(), gpu_min_chunks=300
    )

    await core.index_file("P", tmp_path, tmp_path / "note.md")

    assert gpu_calls["start_pool"] == 0, (
        "the warm floor must never be used to justify a COLD start"
    )
    assert gpu_calls["pooled_embeds"] == []


async def test_a_walk_the_estimator_wrote_off_keeps_looking_for_a_warm_pool(
    tmp_path, warm, gpu_calls
):
    """🔴 6.1 latched 'no GPU for this walk' the first time the estimate said
    CPU, which was correct when the only question was paying for a cold start.
    With lingering on, a pool can be parked by another job at any point during a
    long walk — so a latched decision means a walk that could have picked up two
    idle cards never even asks."""
    from tests.test_index_pipeline import FakeStore, RecordingEmbedder, write_corpus

    class ParksMidWalk(FakeStore):
        """Another job finishes and parks its pool while this walk is running."""

        async def replace_document(self, project, doc, chunks):
            if not self.writes:
                warm.park(make_pool("another:job", [FakeWorker()]), 30.0)
            await super().replace_document(project, doc, chunks)

    # 70 documents: EMBED_WINDOW_DOCS is 64, so the walk flushes twice. The pool
    # appears during the first window, which the walk has already committed to
    # the CPU — the question is whether it asks again for the second.
    write_corpus(tmp_path, {f"doc{i}.md": 1 for i in range(70)})
    core = _core(ParksMidWalk(), RecordingEmbedder(), warm, gpu_min_chunks=100_000)

    summary = await core.index_project("P", tmp_path)

    assert summary["indexed"] == 70
    assert gpu_calls["start_pool"] == 0, "it must not start one — only claim one"
    assert gpu_calls["pooled_embeds"], (
        "the walk latched 'CPU' on its first window and never noticed the two "
        "cards that came free underneath it"
    )


def test_a_config_predating_6_2_simply_gets_the_old_behavior():
    """Every fake config in this suite, and any pinned deployment config,
    predates `gpu_idle_linger_s`. Reading it through `getattr` is what turns
    that into 'no lingering' instead of an AttributeError deep inside a walk."""
    from tests.test_index_pipeline import FakeStore, RecordingEmbedder

    class Old:
        gpu_batch_size = 64

    core = retrieval_mod.RetrievalCore(
        FakeStore(), RecordingEmbedder(), gpu_config=Old()
    )
    assert core.gpu_idle_linger_s == 0.0

    no_gpu = retrieval_mod.RetrievalCore(FakeStore(), RecordingEmbedder())
    assert no_gpu.gpu_idle_linger_s == 0.0


# --------------------------------------------------------------------------
# 15.0 (DESIGN-NVIDIA-ACCELERATION §6 item 3): the worker's own warm-up growth
# must never read as "somebody else took the headroom".
#
# On CUDA with dynamic shapes the arena grows on the first full-length batch
# (measured: 1 GB at batch 16). The worker now warms up with a FULL batch of
# full-context text BEFORE the parent's first slice, and the parent's
# `_settled_free` baseline is taken after that slice — so the growth is already
# inside the baseline. These specs pin the parent's half of that contract: what
# `still_qualifies` does with a baseline that already contains the growth.
# --------------------------------------------------------------------------


_GIB = 1024 ** 3


class _MutableCard:
    """One card whose free VRAM the test moves by hand — no clock, no process."""

    def __init__(self, total_gb: float, free_gb: float):
        self.total = int(total_gb * _GIB)
        self.free = int(free_gb * _GIB)

    def set_free(self, free_gb: float) -> None:
        self.free = int(free_gb * _GIB)

    def devices(self):
        from cognita.gpu_probe import GpuDevice

        return [GpuDevice("gpu0", "0000:01:00.0", "uuid", "card", self.total,
                          self.free, 0)]


def _settled_worker(card: _MutableCard, reserve_gb: float = 4.0):
    """A real `GpuWorker` whose first slice has completed at the card's current
    free VRAM — exactly what `embed()` does after the first recorded slice."""
    from cognita.gpu_probe import GpuDevice

    class Cfg(Config):
        gpu_reserve_vram_gb = reserve_gb

    device = GpuDevice("gpu0", "0000:01:00.0", "uuid", "card", card.total,
                       card.free, 0)
    worker = gpu_host.GpuWorker(device, Cfg(), card)
    worker._settled_free = card.free
    return worker


def test_a_drop_equal_to_the_workers_own_warmup_growth_does_not_yield():
    """The scenario: 10 GB free at spawn; the worker's full-batch warm-up grows
    its arena by 1 GB (free 9 GB); the first slice then settles the baseline at
    9 GB. With a reserve of 9.5 GB the card is 'below reserve' from that moment
    — and must still NOT yield, because nothing was taken since the baseline."""
    card = _MutableCard(total_gb=24, free_gb=10.0)
    card.set_free(9.0)  # the warm-up growth, already absorbed
    worker = _settled_worker(card, reserve_gb=9.5)
    ok, reason = worker.still_qualifies()
    assert ok, f"the worker yielded on its own growth: {reason}"


def test_a_neighbors_drop_below_the_reserve_still_yields():
    card = _MutableCard(total_gb=24, free_gb=9.0)
    worker = _settled_worker(card, reserve_gb=4.0)
    assert worker.still_qualifies() == (True, "ok")
    card.set_free(3.0)  # somebody else took 6 GB
    ok, reason = worker.still_qualifies()
    assert not ok
    assert "vram_free=3.00GB" in reason
    assert "-6.00GB since this worker settled" in reason


def test_a_neighbors_smaller_drop_that_leaves_the_reserve_does_not_yield():
    """Yielding is 'a drop AND below the reserve'. A neighbor that takes 2 GB
    and leaves 7 free is a good neighbor."""
    card = _MutableCard(total_gb=24, free_gb=9.0)
    worker = _settled_worker(card, reserve_gb=4.0)
    card.set_free(7.0)
    assert worker.still_qualifies() == (True, "ok")


def test_no_baseline_yet_means_no_yield():
    """Before the first slice there is nothing to compare against — the warm-up
    growth in particular must not be judged."""
    card = _MutableCard(total_gb=24, free_gb=1.0)
    worker = _settled_worker(card, reserve_gb=4.0)
    worker._settled_free = None
    assert worker.still_qualifies() == (True, "ok")
