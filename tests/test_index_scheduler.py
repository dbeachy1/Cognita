"""Deterministic core checks for the shared indexing scheduler."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from cognita.index_scheduler import (
    DeviceHandler,
    GpuContention,
    IndexScheduler,
    JobCanceled,
    SchedulerError,
    SchedulerSettings,
    gpu_handlers_from_host,
)

# No test outcome here depends on wall-clock time.  Every wait is on a signal
# the code under test sends (an event, a future, a task it owns) and is
# guaranteed to arrive in a correct run; the bound on it is only a hang guard
# so a regression fails with a name instead of hanging the run.  Every timer
# the scheduler arms (re-probe, fallback deadline, idle linger) is either given
# a delay far beyond the test's life and fired by hand, or judged against an
# injected fake clock that only the test advances.
BOUND_S = 5.0


async def _bounded(awaitable, what: str, timeout: float = BOUND_S):
    """Hang guard on a guaranteed signal; a timeout fails naming ``what``."""
    try:
        return await asyncio.wait_for(awaitable, timeout)
    except TimeoutError:
        raise AssertionError(f"timed out after {timeout}s waiting for {what}") from None


async def _settle(scheduler: IndexScheduler, what: str) -> None:
    """Await every background task the scheduler owns, including any a task
    spawns while settling (a reconcile launches GPU start tasks).  Each one
    is a task the scheduler created and always finishes in a correct run."""
    while scheduler._background_tasks:
        await _bounded(
            asyncio.gather(*list(scheduler._background_tasks), return_exceptions=True),
            what,
        )


def _signal_reconcile_armed(scheduler: IndexScheduler) -> asyncio.Event:
    """Return an event the scheduler sets each time it arms a reconcile wake.

    Wraps the instance's ``_schedule_reconcile`` (the same instance-level
    instrumentation the quarantine test already uses for the reconcile
    itself), so the test waits on the scheduler's own action, not on time.
    """
    armed = asyncio.Event()
    original = scheduler._schedule_reconcile

    def arm(delay: float) -> None:
        original(delay)
        armed.set()

    scheduler._schedule_reconcile = arm
    return armed


async def _fire_reconcile(scheduler: IndexScheduler) -> None:
    """Stand in for the event-loop timer: run the pending reconcile now.

    The test has already made the real timer's delay far longer than the test
    can live, so this manual firing is the only one.  Returns once the
    reconcile and every GPU start it launched have settled.
    """
    timer = scheduler._reconcile_timer
    assert timer is not None, "expected a pending reconcile timer"
    timer.cancel()
    scheduler._reconcile_wakeup()
    await _settle(scheduler, "reconcile and its GPU start tasks to settle")


async def _stop(scheduler: IndexScheduler) -> None:
    await _bounded(scheduler.shutdown(), "scheduler shutdown")


@pytest.mark.asyncio
async def test_cpu_is_one_shared_lane_and_preserves_order():
    active = 0
    peak = 0
    calls: list[list[str]] = []

    def cpu(texts):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        calls.append(list(texts))
        active -= 1
        return [[float(len(text))] for text in texts]

    scheduler = IndexScheduler(cpu, dimensions=1)
    try:
        first = scheduler.open_job("A")
        second = scheduler.open_job("B")
        got = await _bounded(asyncio.gather(
            scheduler.embed(first, ["one", "two"]),
            scheduler.embed(second, ["four"]),
        ), "both CPU embeds")
        assert got == [[[3.0], [3.0]], [[4.0]]]
        assert peak == 1
        assert scheduler.snapshot()["cpu"]["completed"] == 3
    finally:
        await _stop(scheduler)


@pytest.mark.asyncio
async def test_each_job_is_credited_with_its_own_work_not_the_first_jobs():
    """15.0.2: the device workers are created inside the FIRST job's context and
    live on, so every later job's batches were credited to that first, already
    printed summary, and each later `embed.done` said chunks=0 (Maia's NVIDIA
    proof).  Each job must get exactly its own chunks, per device, including a
    GPU batch that mixes two projects."""
    from cognita.embed_telemetry import embed_job, record_batch

    async def gpu(texts):
        record_batch("gpu-1", len(texts), sum(map(len, texts)), 0.1)   # what a real adapter does
        return [[float(len(text) + 10)] for text in texts]

    def cpu(texts):
        record_batch("cpu", len(texts), sum(map(len, texts)), 0.1)
        return [[float(len(text))] for text in texts]

    scheduler = IndexScheduler(
        cpu,
        gpu_devices=[DeviceHandler("gpu-1", gpu, max_batch=2)],
        dimensions=1,
        settings=SchedulerSettings(cpu_max_chunk_bytes=1),
    )
    try:
        with embed_job("First", walk="project") as first:
            job = scheduler.open_job("First")          # the workers are created here
            await _bounded(scheduler.embed(job, ["long-a"]), "the first job")
            first.done()
        with embed_job("Second", walk="project") as second:
            b = scheduler.open_job("Second")
        with embed_job("Third", walk="project") as third:
            c = scheduler.open_job("Third")
        await _bounded(asyncio.gather(scheduler.embed(b, ["long-b", "long-c"]),
                                      scheduler.embed(c, ["long-d"])), "two later jobs")
        assert first.chunks == 1, "a later job's work was credited to the first job"
        assert (second.chunks, third.chunks) == (2, 1)
        assert set(second.devices) == {"gpu-1"} and set(third.devices) == {"gpu-1"}
    finally:
        await _stop(scheduler)


@pytest.mark.asyncio
async def test_a_failed_attempt_counts_as_time_spent_but_not_as_work():
    """The rule `Embedder.embed` keeps in its `finally`, now that the device call
    runs outside any job: the job is credited the attempt's time and no chunks."""
    from cognita.embed_telemetry import embed_job

    ticks = iter(float(n) for n in range(1000))

    def cpu(texts):
        raise RuntimeError("ORT failed")

    scheduler = IndexScheduler(cpu, dimensions=1, clock=lambda: next(ticks))
    try:
        with embed_job("Broken", walk="project") as summary:
            job = scheduler.open_job("Broken")
            with pytest.raises(Exception):
                await _bounded(scheduler.embed(job, ["x"]), "the failing embed")
        row = summary.devices["cpu"]
        assert row.chunks == 0 and row.elapsed > 0
    finally:
        await _stop(scheduler)


@pytest.mark.asyncio
async def test_gpu_batch_can_mix_projects_and_cpu_handles_small_chunks():
    gpu_calls: list[list[str]] = []

    async def gpu(texts):
        gpu_calls.append(list(texts))
        return [[float(len(text) + 10)] for text in texts]

    def cpu(texts):
        return [[float(len(text))] for text in texts]

    scheduler = IndexScheduler(
        cpu,
        gpu_devices=[DeviceHandler("gpu-1", gpu, max_batch=2)],
        dimensions=1,
        settings=SchedulerSettings(cpu_max_chunk_bytes=1),
    )
    try:
        a, b = scheduler.open_job("A"), scheduler.open_job("B")
        result = await _bounded(asyncio.gather(
            scheduler.embed(a, ["long-a", "long-b"]),
            scheduler.embed(b, ["long-c"]),
        ), "both GPU embeds")
        assert result == [[[16.0], [16.0]], [[16.0]]]
        assert sum(map(len, gpu_calls)) == 3
        assert scheduler.snapshot()["gpus"][0]["completed"] == 3
    finally:
        await _stop(scheduler)


@pytest.mark.asyncio
async def test_sync_adapter_returning_async_embedding_is_awaited():
    """Regression: the host GPU adapter once leaked its coroutine to validation."""
    calls: list[list[str]] = []

    async def runtime_embed(texts):
        calls.append(list(texts))
        return [[float(len(text))] for text in texts]

    def adapter(texts):
        return runtime_embed(texts)

    scheduler = IndexScheduler(
        lambda texts: [[-1.0] for _ in texts],
        gpu_devices=[DeviceHandler("gpu-1", adapter, max_batch=2)],
        dimensions=1,
        settings=SchedulerSettings(cpu_max_chunk_bytes=1),
    )
    try:
        job = scheduler.open_job("A")
        assert await _bounded(scheduler.embed(job, ["long-a", "long-b"]),
                              "adapter embed") == [[6.0], [6.0]]
        assert calls == [["long-a", "long-b"]]
        assert scheduler.snapshot("A")["jobs"][0]["device_completed"] == {"gpu-1": 2}
    finally:
        await _stop(scheduler)


@pytest.mark.asyncio
async def test_malformed_gpu_result_retries_then_cpu_once():
    count = 0

    def bad_gpu(texts):
        nonlocal count
        count += 1
        return []

    scheduler = IndexScheduler(
        lambda texts: [[1.0] for _ in texts],
        gpu_devices=[DeviceHandler("gpu-1", bad_gpu, max_batch=1),
                     DeviceHandler("gpu-2", bad_gpu, max_batch=1)],
        dimensions=1,
        settings=SchedulerSettings(cpu_max_chunk_bytes=0),
    )
    try:
        job = scheduler.open_job("A")
        assert await _bounded(scheduler.embed(job, ["x"]), "CPU retry embed") == [[1.0]]
        assert count == 2
        assert scheduler.snapshot("A")["jobs"][0]["device_completed"] == {"cpu": 1}
    finally:
        await _stop(scheduler)


@pytest.mark.asyncio
async def test_cancel_removes_queued_work_and_releases_capacity():
    entered = asyncio.Event()
    release = asyncio.Event()

    async def gpu(texts):
        entered.set()
        await _bounded(release.wait(), "test to release the GPU call")
        return [[1.0] for _ in texts]

    scheduler = IndexScheduler(
        lambda texts: [[1.0] for _ in texts],
        gpu_devices=[DeviceHandler("gpu-1", gpu, max_batch=1)],
        dimensions=1,
        settings=SchedulerSettings(max_chunks=1, cpu_max_chunk_bytes=1),
    )
    try:
        job = scheduler.open_job("A")
        task = asyncio.create_task(scheduler.embed(job, ["long"]))
        await _bounded(entered.wait(), "GPU call to start")
        await scheduler.cancel_job(job)
        release.set()
        with pytest.raises(JobCanceled):
            await _bounded(task, "canceled embed to raise")
        assert scheduler.snapshot()["queued"] == 0
        assert scheduler.snapshot()["in_flight"] == 0
    finally:
        release.set()
        await _stop(scheduler)


@pytest.mark.asyncio
async def test_two_gpu_handlers_overlap_and_preserve_mixed_order():
    started = asyncio.Barrier(3)
    release = asyncio.Event()
    calls: list[str] = []

    async def gpu(texts):
        calls.extend(texts)
        await _bounded(started.wait(), "both GPU calls to overlap")
        await _bounded(release.wait(), "test to release the GPU calls")
        return [[float(len(text))] for text in texts]

    scheduler = IndexScheduler(
        lambda texts: [[0.0] for _ in texts],
        gpu_devices=[DeviceHandler("gpu-1", gpu, max_batch=1),
                     DeviceHandler("gpu-2", gpu, max_batch=1)],
        dimensions=1,
        settings=SchedulerSettings(cpu_max_chunk_bytes=0),
    )
    try:
        a, b = scheduler.open_job("project-a"), scheduler.open_job("project-b")
        first = asyncio.create_task(scheduler.embed(a, ["alpha"]))
        second = asyncio.create_task(scheduler.embed(b, ["beta"]))
        await _bounded(started.wait(), "both GPU handlers to be inside a call")
        release.set()
        assert await _bounded(first, "project-a embed") == [[5.0]]
        assert await _bounded(second, "project-b embed") == [[4.0]]
        assert set(calls) == {"alpha", "beta"}
    finally:
        release.set()
        await _stop(scheduler)


@pytest.mark.asyncio
async def test_unavailable_gpu_joins_an_existing_job_after_reconcile():
    clock = [100.0]
    seen: list[str] = []
    gpu = DeviceHandler("gpu-late", lambda texts: seen.extend(texts) or [[2.0] for _ in texts],
                        max_batch=1, available=False, state="unavailable")
    scheduler = IndexScheduler(
        lambda texts: [[1.0] for _ in texts], gpu_devices=[gpu], dimensions=1,
        settings=SchedulerSettings(cpu_max_chunk_bytes=0), clock=lambda: clock[0],
    )
    try:
        job = scheduler.open_job("P")
        task = asyncio.create_task(scheduler.embed(job, ["large chunk"]))
        await asyncio.sleep(0)  # yield to admission/worker; no timing assertion
        assert not task.done()
        scheduler.mark_gpu_ready("gpu-late")
        assert await _bounded(task, "embed on the late GPU") == [[2.0]]
        assert seen == ["large chunk"]
    finally:
        await _stop(scheduler)


@pytest.mark.asyncio
async def test_project_round_robin_prevents_large_request_monopoly():
    order: list[str] = []

    def gpu(texts):
        order.extend(texts)
        return [[1.0] for _ in texts]

    scheduler = IndexScheduler(
        lambda texts: [[0.0] for _ in texts],
        gpu_devices=[DeviceHandler("gpu-1", gpu, max_batch=1)],
        dimensions=1, settings=SchedulerSettings(cpu_max_chunk_bytes=0),
    )
    try:
        a, b = scheduler.open_job("A"), scheduler.open_job("B")
        results = await _bounded(asyncio.gather(
            scheduler.embed(a, ["a1", "a2", "a3"]),
            scheduler.embed(b, ["b1"]),
        ), "both project embeds")
        assert results == [[ [1.0], [1.0], [1.0] ], [[1.0]]]
        assert order.index("b1") < order.index("a3")
    finally:
        await _stop(scheduler)


@pytest.mark.asyncio
async def test_jobs_within_one_project_are_round_robin_not_submission_fifo():
    order: list[str] = []

    def gpu(texts):
        order.extend(texts)
        return [[1.0] for _ in texts]

    scheduler = IndexScheduler(
        lambda texts: [[0.0] for _ in texts],
        gpu_devices=[DeviceHandler("gpu-1", gpu, max_batch=1)],
        dimensions=1, settings=SchedulerSettings(cpu_max_chunk_bytes=0),
    )
    try:
        first = scheduler.open_job("same-project")
        second = scheduler.open_job("same-project")
        results = await _bounded(asyncio.gather(
            scheduler.embed(first, ["a1", "a2", "a3"]),
            scheduler.embed(second, ["b1"]),
        ), "both same-project embeds")
        assert results == [[[1.0], [1.0], [1.0]], [[1.0]]]
        assert order.index("b1") < order.index("a3")
    finally:
        await _stop(scheduler)


@pytest.mark.asyncio
async def test_cold_gpu_starts_only_for_threshold_or_cpu_ineligible_work():
    starts = 0
    calls: list[str] = []

    async def start():
        nonlocal starts
        starts += 1

    gpu = DeviceHandler(
        "cold", lambda texts: calls.extend(texts) or [[2.0] for _ in texts],
        max_batch=2, state="cold", start=start,
    )
    # A fake clock that never moves: on the real clock, a machine stalled past
    # the 30 s CPU fallback deadline would hand the oversized chunk to the CPU.
    scheduler = IndexScheduler(
        lambda texts: [[1.0] for _ in texts], gpu_devices=[gpu], dimensions=1,
        settings=SchedulerSettings(gpu_min_chunks=3, cpu_max_chunk_bytes=8),
        clock=lambda: 100.0,
    )
    try:
        small = scheduler.open_job("small", estimated_chunks=1)
        assert await _bounded(scheduler.embed(small, ["tiny"]), "small CPU embed") == [[1.0]]
        assert starts == 0

        large = scheduler.open_job("large", estimated_chunks=1)
        assert await _bounded(scheduler.embed(large, ["larger-than-eight"]),
                              "oversized GPU embed") == [[2.0]]
        assert starts == 1 and calls == ["larger-than-eight"]
    finally:
        await _stop(scheduler)


@pytest.mark.asyncio
async def test_temporarily_unqualified_cold_gpu_is_reprobed_while_work_waits():
    attempts = 0
    first_attempt = asyncio.Event()

    async def start():
        nonlocal attempts
        attempts += 1
        first_attempt.set()
        if attempts == 1:
            raise SchedulerError("no qualifying GPU worker")

    gpu = DeviceHandler(
        "cold", lambda texts: [[2.0] for _ in texts],
        max_batch=1, state="cold", start=start,
    )
    # Probe interval and cooldown are far beyond the test's life so no loop
    # timer fires on its own; the fake clock never reaches the CPU fallback
    # deadline, so only the re-probe can finish this chunk.
    scheduler = IndexScheduler(
        lambda texts: [[1.0] for _ in texts], gpu_devices=[gpu], dimensions=1,
        settings=SchedulerSettings(
            cpu_max_chunk_bytes=0, gpu_min_chunks=1, gpu_probe_interval_s=3600.0,
            gpu_retry_cooldown_s=3600.0,
        ),
        clock=lambda: 100.0,
    )
    try:
        job = scheduler.open_job("P", estimated_chunks=1)
        task = asyncio.create_task(scheduler.embed(job, ["gpu-only"]))
        # Was a real 10 ms re-probe timer raced against a 1 s bound.  Now: the
        # oversized chunk's admission always launches the first start; wait
        # for it to run and for its task to finish (which arms the re-probe),
        # then fire that re-probe by hand.
        await _bounded(first_attempt.wait(), "first GPU start attempt")
        await _settle(scheduler, "failed first GPU start to finish")
        assert attempts == 1
        await _fire_reconcile(scheduler)
        assert await _bounded(task, "embed after the GPU re-probe") == [[2.0]]
        assert attempts == 2
    finally:
        await _stop(scheduler)


@pytest.mark.asyncio
async def test_gpu_contention_falls_back_once_without_startup_warning_flood(caplog):
    starts = 0

    async def start():
        nonlocal starts
        starts += 1
        raise GpuContention("no qualifying GPU worker")

    scheduler = IndexScheduler(
        lambda texts: [[1.0] for _ in texts],
        gpu_devices=[
            DeviceHandler(f"card-{i}", lambda texts: [], state="cold", start=start)
            for i in range(3)
        ],
        dimensions=1,
        settings=SchedulerSettings(
            # Probe interval far beyond the test's life: the re-probe fires
            # only when the test fires it.
            cpu_max_chunk_bytes=0, gpu_probe_interval_s=3600.0,
        ),
    )
    try:
        with caplog.at_level("INFO", logger="cognita.index_scheduler"):
            job = scheduler.open_job("contended")
            assert await _bounded(scheduler.embed(job, ["large"]),
                                  "CPU fallback embed") == [[1.0]]
            # Was a 40 ms real sleep so the 10 ms re-probe could fire.  Now:
            # await all three contended start tasks, then fire the pending
            # re-probe by hand; an idle scheduler must not restart.
            await _settle(scheduler, "contended GPU starts to finish")
            await _fire_reconcile(scheduler)
        assert starts == 3
        assert sum("GPU busy; falling back to CPU" in r.message for r in caplog.records) == 1
        assert not any("GPU startup" in r.message for r in caplog.records)
    finally:
        await _stop(scheduler)


@pytest.mark.asyncio
async def test_gpu_contention_is_idle_silent_until_a_request():
    starts = 0

    async def start():
        nonlocal starts
        starts += 1

    scheduler = IndexScheduler(
        lambda texts: [[1.0] for _ in texts],
        gpu_devices=[
            DeviceHandler(f"card-{i}", lambda texts: [], state="cold", start=start)
            for i in range(3)
        ],
        settings=SchedulerSettings(gpu_probe_interval_s=0.01),
    )
    try:
        # Was a 40 ms real sleep.  Now: yield once, then prove nothing is
        # pending that could ever start a card -- no reconcile or idle timer,
        # no background task, no scheduler task -- instead of hoping 40 ms
        # was long enough to notice one.
        await asyncio.sleep(0)
        assert scheduler._reconcile_timer is None
        assert scheduler._idle_timer is None
        assert not scheduler._background_tasks
        assert not [t for t in asyncio.all_tasks() if t.get_name().startswith("cognita-")]
        assert starts == 0
    finally:
        await _stop(scheduler)


@pytest.mark.asyncio
async def test_gpu_contention_reprobes_and_recovers_when_cards_free_up():
    starts = 0
    cpu_started = asyncio.Event()
    cpu_release = asyncio.Event()
    gpu_called = asyncio.Event()
    available = False

    async def start():
        nonlocal starts
        starts += 1
        if not available:
            raise GpuContention("no qualifying GPU worker")

    async def cpu(texts):
        cpu_started.set()
        await _bounded(cpu_release.wait(), "test to release the CPU call")
        return [[1.0] for _ in texts]

    async def gpu(texts):
        gpu_called.set()
        return [[2.0] for _ in texts]

    scheduler = IndexScheduler(
        cpu,
        gpu_devices=[
            DeviceHandler(f"card-{i}", gpu, max_batch=1, state="cold", start=start)
            for i in range(3)
        ],
        dimensions=1,
        # Probe interval far beyond the test's life: the re-probe fires only
        # when the test fires it.
        settings=SchedulerSettings(cpu_max_chunk_bytes=0, gpu_probe_interval_s=3600.0),
    )
    try:
        job = scheduler.open_job("recover")
        task = asyncio.create_task(scheduler.embed(job, ["first", "second"]))
        await _bounded(cpu_started.wait(), "CPU fallback to start")
        # Await all three contended start tasks (launched at admission, so
        # they already exist); the first to fail arms one re-probe.
        await _settle(scheduler, "contended GPU starts to finish")
        assert scheduler._reconcile_timer is not None
        assert starts == 3
        available = True
        # Was a real 10 ms re-probe timer racing the assignment above; the
        # test now fires the re-probe itself once the cards are free.
        await _fire_reconcile(scheduler)
        await _bounded(gpu_called.wait(), "recovered GPU to take work")
        cpu_release.set()
        assert await _bounded(task, "mixed CPU/GPU embed") == [[1.0], [2.0]]
        assert starts == 6
    finally:
        cpu_release.set()
        await _stop(scheduler)


@pytest.mark.asyncio
async def test_sustained_gpu_contention_uses_adaptive_probe_backoff(monkeypatch):
    plans = 0
    cpu_started = asyncio.Event()
    cpu_release = asyncio.Event()
    # The original real-time figures (10 ms probe, 40 ms cooldown, 120 ms
    # observation window) scaled x360000 so no loop timer can fire during the
    # test.  The test walks a virtual time line and fires each re-probe that
    # falls due inside the window.
    probe_s, cooldown_s, window_s = 3600.0, 14400.0, 43200.0

    from cognita import gpu_host

    def start_pool(*args, **kwargs):
        nonlocal plans
        plans += 1

    monkeypatch.setattr(gpu_host, "start_pool", start_pool)

    class Probe:
        def devices(self):
            return [
                SimpleNamespace(sysfs_name=f"card-{i}")
                for i in range(3)
            ]

    handlers = gpu_handlers_from_host(
        SimpleNamespace(gpu_slice_chunks=1, gpu_canary_tolerance=0.01),
        Probe(), lambda texts: [[1.0] for _ in texts], 1.0,
    )

    async def cpu(texts):
        cpu_started.set()
        await _bounded(cpu_release.wait(), "test to release the CPU call")
        return [[1.0] for _ in texts]

    scheduler = IndexScheduler(
        cpu,
        gpu_devices=handlers,
        dimensions=1,
        settings=SchedulerSettings(
            cpu_max_chunk_bytes=0, gpu_probe_interval_s=probe_s,
            gpu_retry_cooldown_s=cooldown_s,
        ),
    )
    try:
        job = scheduler.open_job("long-document")
        task = asyncio.create_task(scheduler.embed(job, [f"chunk-{i}" for i in range(12)]))
        await _bounded(cpu_started.wait(), "CPU fallback to start")
        # Was a 120 ms real sleep.  Now: each cycle awaits the contended start
        # tasks (the first to fail arms the next re-probe), then fires that
        # re-probe by hand while it is still due inside the virtual window.
        virtual_now = 0.0
        delays: list[float] = []
        while True:
            await _settle(scheduler, "contended GPU starts to finish")
            assert scheduler._reconcile_timer is not None
            delay = scheduler._gpu_contention_delay_s
            delays.append(delay)
            if virtual_now + delay > window_s:
                break
            virtual_now += delay
            await _fire_reconcile(scheduler)
        # A fixed 10ms poll would have planned dozens of pools.  Backoff keeps
        # the shared runtime bounded while work waits.
        assert plans <= 6
        assert delays == [probe_s, 2 * probe_s, cooldown_s, cooldown_s, cooldown_s]
        assert scheduler._gpu_contention_delay_s == pytest.approx(cooldown_s)
        cpu_release.set()
        assert await _bounded(task, "12-chunk CPU fallback embed") == [[1.0]] * 12
    finally:
        cpu_release.set()
        await _stop(scheduler)


@pytest.mark.asyncio
async def test_a_card_that_yielded_is_used_again_once_its_memory_is_back(monkeypatch):
    """15.0.2: a worker that yields (another program took the VRAM) leaves a pool
    with no live worker that still holds the process-wide GPU lease.  The next
    start asked for that lease, was told "busy", and the card was never used
    again until restart (Maia's NVIDIA proof).  The dead pool must be torn down,
    releasing the lease, before a new one starts."""
    from cognita import gpu_host
    from cognita.index_scheduler import GpuHostRuntime

    lease = {"held": False}

    class Worker:
        def __init__(self, qualifies):
            self.device = SimpleNamespace(sysfs_name="gpu0")
            self.stats = SimpleNamespace(yielded_reason="", failed_reason="")
            self.proc = object()
            self.qualifies = qualifies

        def still_qualifies(self):
            return (True, "ok") if self.qualifies else (False, "vram_free=0.07GB")

        def terminate(self):
            self.proc = None

        def embed(self, texts, timeout):
            return [[1.0] for _ in texts]

    class Pool:
        def __init__(self, worker):
            self.workers = [worker]

        @property
        def alive(self):
            return [w for w in self.workers if w.proc is not None]

        def shutdown(self):
            for w in self.workers:
                w.terminate()
            lease["held"] = False

    pools = iter([Pool(Worker(qualifies=False)), Pool(Worker(qualifies=True))])

    def start_pool(*args, **kwargs):
        if lease["held"]:
            return None                      # gpu_host: "embed.plan gpu_lease=busy"
        lease["held"] = True
        return next(pools)

    monkeypatch.setattr(gpu_host, "start_pool", start_pool)
    monkeypatch.setattr(gpu_host, "check_canary", lambda pool, *a, **k: list(pool.workers))
    runtime = GpuHostRuntime(SimpleNamespace(gpu_canary_tolerance=0.01), None,
                             lambda texts: [[1.0] for _ in texts], 1.0, "host:test")
    with pytest.raises(SchedulerError, match="yielded"):
        await _bounded(runtime.embed("gpu0", ["a"]), "the embed that yields")
    assert lease["held"], "the yielded pool still holds the lease (the precondition)"
    assert await _bounded(runtime.embed("gpu0", ["b"]), "the embed after the memory came back") == [[1.0]]
    await _bounded(runtime.stop(), "runtime stop")
    assert not lease["held"]


@pytest.mark.asyncio
async def test_a_yielded_card_beside_a_live_one_is_parked_not_retried_every_cooldown(monkeypatch):
    """15.0.2 final review: in a two-card pool, card A yields while card B keeps
    the pool alive.  A used to come back "ready" after each cooldown, take a
    batch, fail at once and push its chunks toward the CPU, forever.  Now it is
    started again, finds its worker gone, and is parked as contended while B
    does the work; it returns when the pool is reaped."""
    from cognita import gpu_host

    class Worker:
        def __init__(self, name, yields):
            self.device = SimpleNamespace(sysfs_name=name)
            self.stats = SimpleNamespace(yielded_reason="", failed_reason="")
            self.proc = object()
            self.yields = yields
            self.embeds = 0

        def still_qualifies(self):
            if self.yields:
                self.yields = False
                return False, "vram_free=0.07GB"
            return True, "ok"

        def terminate(self):
            self.proc = None

        def embed(self, texts, timeout):
            self.embeds += 1
            return [[1.0] for _ in texts]

    a, b = Worker("card-A", yields=True), Worker("card-B", yields=False)

    class Pool:
        workers = [a, b]

        @property
        def alive(self):
            return [w for w in self.workers if w.proc is not None]

        def shutdown(self):
            for w in self.workers:
                w.terminate()

    monkeypatch.setattr(gpu_host, "start_pool", lambda *args, **kwargs: Pool())
    monkeypatch.setattr(gpu_host, "check_canary", lambda pool, *a, **k: list(pool.workers))

    class Probe:
        def devices(self):
            return [SimpleNamespace(sysfs_name="card-A"), SimpleNamespace(sysfs_name="card-B")]

    now = [0.0]
    handlers = gpu_handlers_from_host(
        SimpleNamespace(gpu_slice_chunks=2, gpu_canary_tolerance=0.01), Probe(),
        lambda texts: [[1.0] for _ in texts], 1.0,
    )
    scheduler = IndexScheduler(
        lambda texts: [[1.0] for _ in texts], gpu_devices=handlers, dimensions=1,
        # Cooldowns and probes far beyond the test's life; the test moves the
        # clock and fires the reconcile itself.
        settings=SchedulerSettings(cpu_max_chunk_bytes=1, gpu_retry_cooldown_s=1000.0,
                                   gpu_probe_interval_s=1000.0),
        clock=lambda: now[0],
    )
    try:
        assert await _bounded(scheduler.start_gpu("card-A"), "card-A start")
        assert await _bounded(scheduler.start_gpu("card-B"), "card-B start")
        first = scheduler.open_job("P")
        await _bounded(scheduler.embed(first, ["xx"] * 6), "first job")        # A yields once; B finishes
        card_a = next(g for g in scheduler.gpus if g.device_id == "card-A")
        assert card_a.failures == 1 and card_a.state == "cooldown"
        now[0] += 1001.0                                                           # A's cooldown is over
        scheduler.reconcile_devices()
        second = scheduler.open_job("P")
        await _bounded(scheduler.embed(second, ["yy"] * 6), "second job")
        await _settle(scheduler, "A's restart attempt")
        assert card_a.failures == 1, "A took a batch again with no worker behind it"
        assert card_a.state == "contended"
        assert a.embeds == 0 and b.embeds > 0
    finally:
        await _stop(scheduler)


@pytest.mark.asyncio
async def test_a_restart_that_fails_to_start_a_worker_is_retried_not_quarantined():
    """15.0.2 final review: restarting a card that yielded may fail to start its
    worker while the other program's memory use is still moving.  A card that has
    already worked must go to cooldown and be retried; only a card that NEVER
    started is quarantined for that reason."""
    from cognita.index_scheduler import GpuQualificationFailure

    starts = {"worked": 0, "never": 0}

    async def worked_start():
        starts["worked"] += 1
        if starts["worked"] > 1:
            raise GpuQualificationFailure("worker_startup_failed")

    async def never_start():
        starts["never"] += 1
        raise GpuQualificationFailure("worker_startup_failed")

    now = [0.0]
    worked = DeviceHandler("card-A", lambda texts: [[1.0] for _ in texts], state="cold", start=worked_start)
    never = DeviceHandler("card-B", lambda texts: [[1.0] for _ in texts], state="cold", start=never_start)
    scheduler = IndexScheduler(
        lambda texts: [[1.0] for _ in texts], gpu_devices=[worked, never], dimensions=1,
        settings=SchedulerSettings(gpu_retry_cooldown_s=1000.0, gpu_probe_interval_s=1000.0),
        clock=lambda: now[0],
    )
    try:
        assert await _bounded(scheduler.start_gpu("card-A"), "first start of A")
        assert not await _bounded(scheduler.start_gpu("card-A"), "restart of A")
        assert worked.state == "cooldown" and worked.available
        assert not await _bounded(scheduler.start_gpu("card-B"), "first start of B")
        assert never.state == "quarantined" and not never.available
    finally:
        await _stop(scheduler)


@pytest.mark.asyncio
async def test_a_card_whose_cooldown_ends_with_work_queued_is_started_again():
    """The cooldown-elapsed branch for a startable card: it goes cold and, when
    work is waiting, a start is launched at once rather than on a later admission."""
    starts = []

    async def start():
        starts.append(1)

    now = [0.0]
    card = DeviceHandler("card-A", lambda texts: [[1.0] for _ in texts], state="cooldown", start=start)
    card._cooldown_until = 5.0
    scheduler = IndexScheduler(
        lambda texts: [[1.0] for _ in texts], gpu_devices=[card], dimensions=1,
        settings=SchedulerSettings(gpu_retry_cooldown_s=1000.0, gpu_probe_interval_s=1000.0),
        clock=lambda: now[0],
    )
    try:
        scheduler._project_queues["P"].append(object())          # work is waiting
        now[0] = 6.0
        scheduler.reconcile_devices()
        await _settle(scheduler, "the restart the reconcile launched")
        assert starts == [1] and card.state == "ready"
        scheduler._project_queues["P"].clear()
    finally:
        await _stop(scheduler)


@pytest.mark.asyncio
async def test_shared_gpu_pool_does_not_mark_unqualified_card_ready(monkeypatch):
    from cognita import gpu_host

    class Pool:
        def __init__(self):
            self.alive = True
            self.workers = []

        def shutdown(self):
            self.alive = False

    pool = Pool()
    worker = SimpleNamespace(device=SimpleNamespace(sysfs_name="card-0"))
    monkeypatch.setattr(gpu_host, "start_pool", lambda *args, **kwargs: pool)
    monkeypatch.setattr(gpu_host, "check_canary", lambda *args, **kwargs: [worker])

    class Probe:
        def devices(self):
            return [SimpleNamespace(sysfs_name="card-0"), SimpleNamespace(sysfs_name="card-1")]

    config = SimpleNamespace(gpu_slice_chunks=1, gpu_canary_tolerance=0.01)
    handlers = gpu_handlers_from_host(
        config, Probe(), lambda texts: [[1.0] for _ in texts], 1.0,
    )
    scheduler = IndexScheduler(
        lambda texts: [[1.0] for _ in texts], gpu_devices=handlers,
        settings=SchedulerSettings(gpu_probe_interval_s=0.01),
    )
    try:
        assert await _bounded(scheduler.start_gpu("card-0"), "card-0 start")
        assert not await _bounded(scheduler.start_gpu("card-1"), "card-1 start")
        states = {row["device"]: row["state"] for row in scheduler.snapshot()["gpus"]}
        assert states == {"card-0": "ready", "card-1": "contended"}
    finally:
        await _stop(scheduler)


@pytest.mark.asyncio
async def test_shared_gpu_startup_failure_is_quarantined_with_safe_reason(monkeypatch):
    from cognita import gpu_host

    class Worker:
        def __init__(self):
            self.device = SimpleNamespace(sysfs_name="card-0")
            self.stats = SimpleNamespace(
                failed_reason=(
                    "worker protocol error: migraphx_save path=/home/private/cache"
                ),
            )
            self.proc = object()

        def terminate(self):
            self.proc = None

    class Pool:
        def __init__(self, worker):
            self.workers = [worker]
            self.alive = True
            self.shutdown_calls = 0

        def shutdown(self):
            self.shutdown_calls += 1
            self.alive = False
            for worker in self.workers:
                worker.terminate()

    worker = Worker()
    pool = Pool(worker)
    monkeypatch.setattr(gpu_host, "start_pool", lambda *args, **kwargs: pool)
    monkeypatch.setattr(gpu_host, "check_canary", lambda *args, **kwargs: [])

    config = SimpleNamespace(gpu_slice_chunks=1, gpu_canary_tolerance=0.01)
    handlers = gpu_handlers_from_host(
        config,
        SimpleNamespace(devices=lambda: [SimpleNamespace(sysfs_name="card-0")]),
        lambda texts: [[1.0] for _ in texts],
        1.0,
    )
    scheduler = IndexScheduler(
        lambda texts: [[1.0] for _ in texts],
        gpu_devices=handlers,
        settings=SchedulerSettings(gpu_probe_interval_s=0.01),
    )
    try:
        assert not await _bounded(scheduler.start_gpu("card-0"), "card-0 start")
        row = scheduler.snapshot()["gpus"][0]
        assert row["state"] == "quarantined"
        assert row["reason"] == "startup failed: worker_startup_failed"
        assert not handlers[0].available
        assert scheduler._reconcile_timer is None
        assert pool.shutdown_calls == 1
    finally:
        await _stop(scheduler)


@pytest.mark.asyncio
async def test_shared_program_cache_repair_requalifies_after_cooldown(monkeypatch, caplog):
    from cognita import gpu_host

    monkeypatch.setenv("COGNITA_ACCELERATION_PROFILE", "amd")
    prepared = []
    pool_starts = []
    worker = SimpleNamespace(device=SimpleNamespace(sysfs_name="card-0"))
    # One fake clock drives both the shared runtime's qualification retry
    # window and the scheduler's device cooldown.
    clock = [1000.0]

    class Pool:
        def __init__(self):
            self.workers = [worker]
            self.alive = True

        def shutdown(self):
            self.alive = False

    def prepare(config):
        prepared.append(config)
        if len(prepared) == 1:
            raise gpu_host.GpuUnavailable("private host path must not reach diagnostics")

    def start_pool(*args, **kwargs):
        pool_starts.append(args)
        return Pool()

    monkeypatch.setattr(gpu_host, "_prepare_program_cache_dir", prepare)
    monkeypatch.setattr(gpu_host, "start_pool", start_pool)
    monkeypatch.setattr(gpu_host, "check_canary", lambda *args, **kwargs: [worker])

    config = SimpleNamespace(
        gpu_enabled=True, gpu_venv_python="/opt/cognita-runtimes/embed/bin/python",
        gpu_program_cache_dir="/unavailable", gpu_slice_chunks=1,
        gpu_canary_tolerance=0.01, gpu_retry_cooldown_s=0.01,
    )
    probe = SimpleNamespace(devices=lambda: [
        SimpleNamespace(sysfs_name="card-0"), SimpleNamespace(sysfs_name="card-1"),
    ])
    handlers = gpu_handlers_from_host(config, probe, lambda texts: [[1.0] for _ in texts], 1.0,
                                      clock=lambda: clock[0])
    scheduler = IndexScheduler(
        lambda texts: [[1.0] for _ in texts], gpu_devices=handlers,
        settings=SchedulerSettings(gpu_probe_interval_s=0.01, gpu_retry_cooldown_s=0.01),
        clock=lambda: clock[0],
    )
    try:
        assert not await _bounded(scheduler.start_gpu("card-0"), "card-0 first start")
        assert not await _bounded(scheduler.start_gpu("card-1"), "card-1 start")
        assert not await _bounded(scheduler.start_gpu("card-0"), "card-0 second start")
        rows = scheduler.snapshot()["gpus"]
        assert all(row["state"] == "cooldown" for row in rows)
        assert all(row["reason"] == "startup failed: program_cache_unavailable" for row in rows)
        assert all(handler.available for handler in handlers)
        assert len(prepared) == 1
        assert not pool_starts
        assert sum("gpu runtime qualification failed reason=program_cache_unavailable" in record.message
                   for record in caplog.records) == 1
        assert not any("index scheduler GPU startup" in record.message for record in caplog.records)
        assert "private host path" not in caplog.text

        # The first cache failure is retried only after the shared cooldown.
        # Was a 30 ms real sleep; now the fake clock advances past the 10 ms
        # cooldown, which is what both retry checks read.
        clock[0] += 0.03
        assert await _bounded(scheduler.start_gpu("card-0"), "card-0 start after cooldown")
        assert len(prepared) == 2
        assert len(pool_starts) == 1
        assert scheduler.snapshot()["gpus"][0]["state"] == "ready"
    finally:
        await _stop(scheduler)


@pytest.mark.asyncio
async def test_nvidia_startup_skips_migraphx_program_cache(monkeypatch):
    from cognita import gpu_host

    monkeypatch.setenv("COGNITA_ACCELERATION_PROFILE", "nvidia")
    prepared = []
    pool_starts = []
    worker = SimpleNamespace(device=SimpleNamespace(sysfs_name="card-0"))

    class Pool:
        workers = [worker]
        alive = True

        def shutdown(self):
            self.alive = False

    def prepare(config):
        prepared.append(config)
        raise gpu_host.GpuUnavailable("unwritable configured path")

    def start_pool(*args, **kwargs):
        pool_starts.append(args)
        return Pool()

    monkeypatch.setattr(gpu_host, "_prepare_program_cache_dir", prepare)
    monkeypatch.setattr(gpu_host, "start_pool", start_pool)
    monkeypatch.setattr(gpu_host, "check_canary", lambda *args, **kwargs: [worker])

    config = SimpleNamespace(
        gpu_enabled=True, gpu_venv_python="/opt/cognita-runtimes/embed/bin/python",
        gpu_program_cache_dir="/unavailable", gpu_slice_chunks=1,
        gpu_canary_tolerance=0.01, gpu_retry_cooldown_s=0.01,
    )
    probe = SimpleNamespace(devices=lambda: [SimpleNamespace(sysfs_name="card-0")])
    handlers = gpu_handlers_from_host(config, probe, lambda texts: [[1.0] for _ in texts], 1.0)
    scheduler = IndexScheduler(
        lambda texts: [[1.0] for _ in texts], gpu_devices=handlers,
        settings=SchedulerSettings(gpu_probe_interval_s=3600),
    )
    try:
        assert await _bounded(scheduler.start_gpu("card-0"), "NVIDIA GPU start")
        assert not prepared
        assert len(pool_starts) == 1
        assert scheduler.snapshot()["gpus"][0]["state"] == "ready"
    finally:
        await _stop(scheduler)


@pytest.mark.asyncio
async def test_gpu_canary_startup_failure_is_quarantined():
    async def start():
        raise RuntimeError("canary mismatch")

    gpu = DeviceHandler("cold", lambda texts: [], state="cold", start=start)
    scheduler = IndexScheduler(
        lambda texts: [], gpu_devices=[gpu],
        settings=SchedulerSettings(gpu_retry_cooldown_s=0.01),
    )
    try:
        assert not await _bounded(scheduler.start_gpu("cold"), "canary-failing start")
        assert gpu.state == "quarantined" and not gpu.available
        assert scheduler._reconcile_timer is None
    finally:
        await _stop(scheduler)


@pytest.mark.asyncio
async def test_quarantined_gpus_wake_oversized_cpu_fallback_once(caplog):
    gpu_calls = []
    clock = [100.0]
    cards = [
        DeviceHandler("g1", lambda texts: gpu_calls.append(texts), state="quarantined", available=False),
        DeviceHandler("g2", lambda texts: gpu_calls.append(texts), state="quarantined", available=False),
    ]
    # Cooldown far beyond the test's life so the real deadline wake cannot
    # fire; the fake clock decides when the fallback deadline has passed.
    cooldown_s = 3600.0
    scheduler = IndexScheduler(
        lambda texts: [[float(len(text))] for text in texts],
        gpu_devices=cards, dimensions=1,
        settings=SchedulerSettings(cpu_max_chunk_bytes=1, gpu_retry_cooldown_s=cooldown_s),
        clock=lambda: clock[0],
    )
    reconciles = 0
    original = scheduler._reconcile_queued_work

    async def counted_reconcile():
        nonlocal reconciles
        reconciles += 1
        await original()

    scheduler._reconcile_queued_work = counted_reconcile
    armed = _signal_reconcile_armed(scheduler)
    try:
        job = scheduler.open_job("quarantined")
        task = asyncio.create_task(scheduler.embed(job, ["oversized"]))
        # Was a real 30 ms deadline raced against a 0.5 s bound.  Now: wait for
        # the CPU worker to decline the chunk and arm its deadline wake (with
        # every GPU quarantined it always does), move the fake clock to the
        # deadline, and fire that one wake by hand.
        await _bounded(armed.wait(), "CPU worker to arm the fallback deadline wake")
        assert not task.done()
        clock[0] += cooldown_s
        await _fire_reconcile(scheduler)
        assert await _bounded(task, "oversized CPU fallback") == [[9.0]]
        assert job.device_completed == {"cpu": 1}
        assert gpu_calls == []
        assert reconciles == 1
        assert not [row for row in caplog.records if row.levelno >= 30]
    finally:
        await _stop(scheduler)


@pytest.mark.asyncio
async def test_ready_gpu_wins_before_oversized_cpu_fallback_deadline():
    gpu = DeviceHandler("g1", lambda texts: [[99.0] for _ in texts], state="quarantined", available=False)
    # The fake clock never moves, so the 0.2 s CPU fallback deadline can never
    # arrive no matter how slow the machine is.
    scheduler = IndexScheduler(
        lambda texts: [[1.0] for _ in texts], gpu_devices=[gpu], dimensions=1,
        settings=SchedulerSettings(cpu_max_chunk_bytes=1, gpu_retry_cooldown_s=0.2),
        clock=lambda: 100.0,
    )
    armed = _signal_reconcile_armed(scheduler)
    try:
        job = scheduler.open_job("recovered")
        task = asyncio.create_task(scheduler.embed(job, ["oversized"]))
        # Was a 10 ms real sleep.  Now waits for the CPU worker to decline the
        # oversized chunk and arm its fallback deadline wake, which it always
        # does while the only GPU is quarantined.
        await _bounded(armed.wait(), "CPU worker to decline the oversized chunk")
        assert not task.done()
        scheduler.mark_gpu_ready("g1")
        assert await _bounded(task, "embed on the recovered GPU") == [[99.0]]
        assert job.device_completed == {"g1": 1}
    finally:
        await _stop(scheduler)


@pytest.mark.asyncio
async def test_ready_gpu_is_reaped_only_after_queue_drains_and_linger_expires():
    stopped = asyncio.Event()

    async def stop():
        stopped.set()

    gpu = DeviceHandler(
        "warm", lambda texts: [[2.0] for _ in texts],
        max_batch=1, stop=stop,
    )
    # Linger far beyond the test's life: the idle timer fires only when the
    # test fires it.
    scheduler = IndexScheduler(
        lambda texts: [[1.0] for _ in texts], gpu_devices=[gpu], dimensions=1,
        settings=SchedulerSettings(
            cpu_max_chunk_bytes=0, gpu_idle_linger_s=3600.0,
        ),
    )
    # Signal from the scheduler itself each time it arms the linger timer.
    linger_armed = asyncio.Event()
    original_schedule = scheduler._schedule_idle_reap_locked

    def schedule_idle_reap_locked() -> None:
        original_schedule()
        if scheduler._idle_timer is not None:
            linger_armed.set()

    scheduler._schedule_idle_reap_locked = schedule_idle_reap_locked
    try:
        job = scheduler.open_job("P")
        assert await _bounded(scheduler.embed(job, ["large"]), "GPU embed") == [[2.0]]
        # Was a real 10 ms linger timer inside a 1 s bound.  Now: wait for the
        # drained queue to arm the idle timer (a finished attempt on a ready,
        # stoppable GPU always does), prove nothing was reaped before it
        # fires, then fire it by hand and await the reap task it starts.
        await _bounded(linger_armed.wait(), "drained queue to arm the idle linger timer")
        assert gpu.state == "ready" and not stopped.is_set()
        scheduler._idle_timer.cancel()
        scheduler._start_idle_reap()
        reap = scheduler._idle_reap_task
        assert reap is not None
        await _bounded(stopped.wait(), "idle reap to stop the GPU")
        await _bounded(reap, "idle reap task to finish")
        assert gpu.state == "cold" and gpu.reason == "idle linger elapsed"
    finally:
        await _stop(scheduler)


@pytest.mark.asyncio
async def test_cold_gpu_startup_arms_idle_reap_after_cpu_drains_queue():
    startup_entered = asyncio.Event()
    release_startup = asyncio.Event()
    linger_armed = asyncio.Event()
    stopped = asyncio.Event()
    stop_calls = 0

    async def start():
        startup_entered.set()
        await _bounded(release_startup.wait(), "test to release GPU startup")

    async def stop():
        nonlocal stop_calls
        stop_calls += 1
        stopped.set()

    gpu = DeviceHandler(
        "cold", lambda texts: [[2.0] for _ in texts],
        state="cold", max_batch=1, start=start, stop=stop,
    )
    scheduler = IndexScheduler(
        lambda texts: [[1.0] for _ in texts], gpu_devices=[gpu], dimensions=1,
        settings=SchedulerSettings(
            gpu_min_chunks=1, cpu_max_chunk_bytes=1024,
            gpu_idle_linger_s=3600.0,
        ),
    )
    original_schedule = scheduler._schedule_idle_reap_locked

    def schedule_idle_reap_locked() -> None:
        original_schedule()
        if scheduler._idle_timer is not None:
            linger_armed.set()

    scheduler._schedule_idle_reap_locked = schedule_idle_reap_locked
    try:
        job = scheduler.open_job("cold-start", estimated_chunks=1)
        embed = asyncio.create_task(scheduler.embed(job, ["small"]))
        await _bounded(startup_entered.wait(), "GPU startup to block")

        # CPU fallback completes the only queued chunk while the cold GPU is
        # still starting.  No GPU attempt will run after startup finishes.
        assert await _bounded(embed, "CPU fallback to drain the queue") == [[1.0]]
        assert gpu.state == "starting"
        assert scheduler._idle_timer is None

        release_startup.set()
        await _bounded(linger_armed.wait(), "cold startup to arm idle linger")
        await _settle(scheduler, "cold GPU startup to finish")
        assert gpu.state == "ready" and scheduler._idle_timer is not None

        scheduler._idle_timer.cancel()
        scheduler._start_idle_reap()
        reap = scheduler._idle_reap_task
        assert reap is not None
        await _bounded(stopped.wait(), "idle reap to stop unused cold-start GPU")
        await _bounded(reap, "cold-start idle reap task to finish")
        assert stop_calls == 1
        assert gpu.state == "cold" and gpu.reason == "idle linger elapsed"
    finally:
        release_startup.set()
        await _stop(scheduler)


@pytest.mark.asyncio
async def test_linger_expiring_during_another_gpu_reap_is_not_lost():
    first_stop_entered = asyncio.Event()
    release_first_stop = asyncio.Event()
    second_stopped = asyncio.Event()
    second_stop_calls = 0

    async def stop_first():
        first_stop_entered.set()
        await _bounded(release_first_stop.wait(), "test to release first GPU stop")

    async def start_second():
        return None

    async def stop_second():
        nonlocal second_stop_calls
        second_stop_calls += 1
        second_stopped.set()

    first = DeviceHandler(
        "first", lambda texts: [[1.0] for _ in texts], stop=stop_first,
    )
    second = DeviceHandler(
        "second", lambda texts: [[2.0] for _ in texts],
        state="cold", start=start_second, stop=stop_second,
    )
    scheduler = IndexScheduler(
        lambda texts: [[0.0] for _ in texts], gpu_devices=[first, second],
        dimensions=1, settings=SchedulerSettings(gpu_idle_linger_s=3600.0),
    )
    try:
        scheduler._start_idle_reap()
        first_reap = scheduler._idle_reap_task
        assert first_reap is not None
        await _bounded(first_stop_entered.wait(), "first GPU idle stop to block")

        assert await _bounded(scheduler.start_gpu("second"), "second GPU startup")
        assert second.state == "ready" and scheduler._idle_timer is None

        # The first GPU's locked turn prevents B from arming its timer while
        # A is stopping.  Completion must re-evaluate B after releasing A's
        # turn instead of leaving the newly ready device allocated forever.
        release_first_stop.set()
        await _bounded(first_reap, "first GPU idle reap to finish")
        assert scheduler._idle_timer is not None
        scheduler._idle_timer.cancel()
        scheduler._start_idle_reap()
        await _bounded(second_stopped.wait(), "deferred second GPU idle reap")
        second_reap = scheduler._idle_reap_task
        assert second_reap is not None
        await _bounded(second_reap, "second GPU idle reap to finish")
        assert second_stop_calls == 1
        assert second.state == "cold" and second.reason == "idle linger elapsed"
        assert scheduler._idle_timer is None and not scheduler._idle_reap_pending
    finally:
        release_first_stop.set()
        await _stop(scheduler)


@pytest.mark.asyncio
async def test_failed_idle_stop_stays_ready_and_retries_without_false_success(caplog):
    stop_calls = 0

    async def stop():
        nonlocal stop_calls
        stop_calls += 1
        if stop_calls == 1:
            raise RuntimeError("synthetic stop failure")

    gpu = DeviceHandler(
        "retry-stop", lambda texts: [[1.0] for _ in texts], stop=stop,
    )
    scheduler = IndexScheduler(
        lambda texts: [[0.0] for _ in texts], gpu_devices=[gpu], dimensions=1,
        settings=SchedulerSettings(gpu_idle_linger_s=3600.0),
    )
    caplog.set_level("INFO")
    try:
        scheduler._start_idle_reap()
        first_reap = scheduler._idle_reap_task
        assert first_reap is not None
        await _bounded(first_reap, "failed idle stop attempt to finish")
        assert stop_calls == 1
        assert gpu.state == "ready" and gpu.reason == "idle reap failed: RuntimeError"
        assert scheduler._idle_timer is not None
        assert not any(
            "GPU idle reap completed" in record.message
            for record in caplog.records
        )

        scheduler._idle_timer.cancel()
        scheduler._start_idle_reap()
        second_reap = scheduler._idle_reap_task
        assert second_reap is not None
        await _bounded(second_reap, "successful idle stop retry to finish")
        assert stop_calls == 2
        assert gpu.state == "cold" and gpu.reason == "idle linger elapsed"
        assert sum(
            "GPU idle reap completed" in record.message
            for record in caplog.records
        ) == 1
    finally:
        await _stop(scheduler)


@pytest.mark.asyncio
async def test_production_host_stop_keeps_pool_owned_until_retry_succeeds(monkeypatch):
    from cognita import gpu_host

    shutdown_calls = 0
    worker = SimpleNamespace(
        device=SimpleNamespace(sysfs_name="card-0"),
        proc=object(),
    )

    class Pool:
        def __init__(self):
            self.workers = [worker]

        @property
        def alive(self):
            return [item for item in self.workers if item.proc is not None]

        def shutdown(self):
            nonlocal shutdown_calls
            shutdown_calls += 1
            if shutdown_calls == 1:
                raise RuntimeError("synthetic pool shutdown failure")
            worker.proc = None

    pool = Pool()
    monkeypatch.setattr(gpu_host, "start_pool", lambda *args, **kwargs: pool)
    monkeypatch.setattr(gpu_host, "check_canary", lambda *args, **kwargs: [worker])
    handlers = gpu_handlers_from_host(
        SimpleNamespace(gpu_slice_chunks=1, gpu_canary_tolerance=0.01),
        SimpleNamespace(devices=lambda: [SimpleNamespace(sysfs_name="card-0")]),
        lambda texts: [[0.0] for _ in texts],
        1.0,
    )
    runtime = handlers[0].stop.__self__
    scheduler = IndexScheduler(
        lambda texts: [[0.0] for _ in texts], gpu_devices=handlers, dimensions=1,
        settings=SchedulerSettings(gpu_idle_linger_s=3600.0),
    )
    try:
        assert await _bounded(scheduler.start_gpu("card-0"), "production GPU start")
        assert runtime.pool is pool

        scheduler._idle_timer.cancel()
        scheduler._start_idle_reap()
        first_reap = scheduler._idle_reap_task
        assert first_reap is not None
        await _bounded(first_reap, "failed production idle stop")
        assert shutdown_calls == 1
        assert runtime.pool is pool
        assert handlers[0].state == "ready"
        assert scheduler._idle_timer is not None

        scheduler._idle_timer.cancel()
        scheduler._start_idle_reap()
        second_reap = scheduler._idle_reap_task
        assert second_reap is not None
        await _bounded(second_reap, "successful production idle stop retry")
        assert shutdown_calls == 2
        assert runtime.pool is None
        assert handlers[0].state == "cold"
    finally:
        await _stop(scheduler)


@pytest.mark.asyncio
async def test_stale_generation_requeues_once_and_accepts_only_current_attempt():
    generation_changed = False
    device = DeviceHandler("gpu-1", None, max_batch=1)  # type: ignore[arg-type]
    def gpu(texts):
        nonlocal generation_changed
        if not generation_changed:
            generation_changed = True
            device.generation += 1
        return [[3.0] for _ in texts]
    device.embed = gpu
    scheduler = IndexScheduler(
        lambda texts: [[0.0] for _ in texts], gpu_devices=[device], dimensions=1,
        settings=SchedulerSettings(cpu_max_chunk_bytes=0),
    )
    try:
        job = scheduler.open_job("P")
        assert await _bounded(scheduler.embed(job, ["chunk"]), "stale-generation embed") == [[3.0]]
        assert job.attempted == 2 and job.completed == 1
    finally:
        await _stop(scheduler)


@pytest.mark.asyncio
async def test_gpu_failure_requeues_to_other_gpu_and_quarantine_is_terminal():
    def broken(texts):
        raise RuntimeError("synthetic device failure")
    scheduler = IndexScheduler(
        lambda texts: [[9.0] for _ in texts],
        gpu_devices=[DeviceHandler("bad", broken, max_batch=1),
                     DeviceHandler("good", lambda texts: [[4.0] for _ in texts], max_batch=1)],
        dimensions=1, settings=SchedulerSettings(cpu_max_chunk_bytes=0),
        # A fake clock that never moves: on the real clock, a machine stalled
        # past the 30 s cooldown could let "bad" retake the chunk and fail it
        # onto the CPU.
        clock=lambda: 100.0,
    )
    try:
        job = scheduler.open_job("P")
        assert await _bounded(scheduler.embed(job, ["large"]), "requeued GPU embed") == [[4.0]]
        scheduler.quarantine_gpu("bad", "provider mismatch")
        gpu = scheduler.snapshot()["gpus"][0]
        assert gpu["state"] == "quarantined" and gpu["reason"] == "provider mismatch"
    finally:
        await _stop(scheduler)


@pytest.mark.asyncio
async def test_admission_budget_cancellation_releases_waiter_and_snapshot_is_private():
    entered = asyncio.Event()
    release = asyncio.Event()

    async def gpu(texts):
        entered.set()
        await _bounded(release.wait(), "test to release the GPU call")
        return [[1.0] for _ in texts]

    scheduler = IndexScheduler(
        lambda texts: [[1.0] for _ in texts],
        gpu_devices=[DeviceHandler("gpu-1", gpu, max_batch=1)], dimensions=1,
        settings=SchedulerSettings(max_chunks=1, max_text_bytes=4,
                                   cpu_max_chunk_bytes=0),
    )
    try:
        first_job, blocked_job = scheduler.open_job("P1"), scheduler.open_job("P2")
        first = asyncio.create_task(scheduler.embed(first_job, ["long"]))
        await _bounded(entered.wait(), "first GPU call to start")
        blocked = asyncio.create_task(scheduler.embed(blocked_job, ["waiter"]))
        await asyncio.sleep(0)  # establish the bounded admission wait
        await scheduler.cancel_job(blocked_job)
        release.set()
        assert await _bounded(first, "first embed") == [[1.0]]
        with pytest.raises(JobCanceled):
            await _bounded(blocked, "canceled waiter to raise")
        snap = scheduler.snapshot()
        assert snap["queued"] == 0 and snap["in_flight"] == 0
        assert "waiter" not in repr(snap) and "P1" not in repr(snap)
    finally:
        release.set()
        await _stop(scheduler)


@pytest.mark.asyncio
async def test_five_staggered_projects_start_on_cpu_then_pick_up_gpu_work():
    cpu_entered = asyncio.Event()
    cpu_release = asyncio.Event()
    cpu_calls: list[list[str]] = []
    gpu_calls: list[list[str]] = []

    async def cpu(texts):
        cpu_calls.append(list(texts))
        cpu_entered.set()
        await _bounded(cpu_release.wait(), "test to release the CPU call")
        return [[1.0] for _ in texts]

    def gpu(texts):
        gpu_calls.append(list(texts))
        return [[2.0] for _ in texts]

    device = DeviceHandler("late-gpu", gpu, max_batch=1, available=False,
                           state="unavailable")
    # A fake clock that never moves: on the real clock, a machine stalled past
    # the 30 s CPU fallback deadline would hand a "large!" chunk to the CPU.
    scheduler = IndexScheduler(
        cpu, gpu_devices=[device], dimensions=1,
        settings=SchedulerSettings(cpu_max_chunk_bytes=2),
        clock=lambda: 100.0,
    )
    try:
        jobs = [scheduler.open_job(f"P{i}") for i in range(5)]
        tasks = [asyncio.create_task(scheduler.embed(job, ["x", "large!"],))
                 for job in jobs]
        await _bounded(cpu_entered.wait(), "first CPU call to start")
        scheduler.mark_gpu_ready("late-gpu")
        cpu_release.set()
        results = await _bounded(asyncio.gather(*tasks), "all five project embeds")
        assert cpu_calls and all(len(call) == 1 for call in cpu_calls)
        assert len(gpu_calls) >= 5
        assert all(result[0] in ([1.0], [2.0]) and result[1] == [2.0]
                   for result in results)
        assert sum(call == ["large!"] for call in gpu_calls) == 5
    finally:
        cpu_release.set()
        await _stop(scheduler)


@pytest.mark.asyncio
async def test_faster_gpu_handler_pulls_more_work_while_slow_handler_is_busy():
    slow_entered = asyncio.Event()
    slow_release = asyncio.Event()
    fast_threshold = asyncio.Event()
    fast_count = 0
    slow_count = 0

    async def slow(texts):
        nonlocal slow_count
        slow_count += len(texts)
        slow_entered.set()
        await _bounded(slow_release.wait(), "test to release the slow GPU call")
        return [[1.0] for _ in texts]

    def fast(texts):
        nonlocal fast_count
        fast_count += len(texts)
        if fast_count >= 5:
            fast_threshold.set()
        return [[2.0] for _ in texts]

    scheduler = IndexScheduler(
        lambda texts: [[0.0] for _ in texts],
        gpu_devices=[DeviceHandler("slow", slow, max_batch=1),
                     DeviceHandler("fast", fast, max_batch=1)],
        dimensions=1, settings=SchedulerSettings(cpu_max_chunk_bytes=0),
    )
    try:
        job = scheduler.open_job("throughput")
        task = asyncio.create_task(scheduler.embed(job, [f"chunk-{i}" for i in range(12)]))
        await _bounded(slow_entered.wait(), "slow GPU call to start")
        await _bounded(fast_threshold.wait(), "fast GPU to take five chunks")
        assert fast_count >= 5 and slow_count == 1
        slow_release.set()
        vectors = await _bounded(task, "12-chunk two-GPU embed")
        assert len(vectors) == 12 and all(vector in ([1.0], [2.0]) for vector in vectors)
    finally:
        slow_release.set()
        await _stop(scheduler)


@pytest.mark.asyncio
async def test_out_of_order_gpu_completions_keep_request_order_and_project_isolation():
    started = asyncio.Barrier(3)
    pending: list[tuple[list[str], asyncio.Future]] = []
    third_seen = asyncio.Event()
    fourth_seen = asyncio.Event()

    async def gpu(texts):
        future = asyncio.get_running_loop().create_future()
        pending.append((list(texts), future))
        if len(pending) >= 3:
            third_seen.set()
        if len(pending) <= 2:
            await _bounded(started.wait(), "both GPU calls to overlap")
        if len(pending) >= 4:
            fourth_seen.set()
        return await _bounded(future, "test to resolve this GPU call")

    scheduler = IndexScheduler(
        lambda texts: [[0.0] for _ in texts],
        gpu_devices=[DeviceHandler("g1", gpu, max_batch=1),
                     DeviceHandler("g2", gpu, max_batch=1)],
        dimensions=1, settings=SchedulerSettings(cpu_max_chunk_bytes=0),
    )
    try:
        a, b = scheduler.open_job("A"), scheduler.open_job("B")
        first = asyncio.create_task(scheduler.embed(a, ["a0", "a1"]))
        second = asyncio.create_task(scheduler.embed(b, ["b0", "b1"]))
        await _bounded(started.wait(), "both GPU handlers to be inside a call")
        # Resolve the second GPU call first, then the next call it enables;
        # result routing must still follow each request's original offsets.
        def value(text: str) -> float:
            return float(10 + int(text[1])) if text.startswith("a") else float(20 + int(text[1]))

        pending[1][1].set_result([[value(pending[1][0][0])]])
        await _bounded(third_seen.wait(), "third GPU call")
        pending[2][1].set_result([[value(pending[2][0][0])]])
        pending[0][1].set_result([[value(pending[0][0][0])]])
        await _bounded(fourth_seen.wait(), "fourth GPU call")
        pending[3][1].set_result([[value(pending[3][0][0])]])
        assert await _bounded(first, "project A embed") == [[10.0], [11.0]]
        assert await _bounded(second, "project B embed") == [[20.0], [21.0]]
        assert scheduler.snapshot("A")["jobs"][0]["device_completed"]
    finally:
        for _, future in pending:
            if not future.done():
                future.cancel()
        await _stop(scheduler)


@pytest.mark.asyncio
async def test_repeated_gpu_failures_force_one_cpu_terminal_attempt_and_cooldown():
    clock = [10.0]
    calls = 0

    def broken(texts):
        nonlocal calls
        calls += 1
        raise RuntimeError("synthetic failure")

    scheduler = IndexScheduler(
        lambda texts: [[9.0] for _ in texts],
        gpu_devices=[DeviceHandler("g1", broken, max_batch=1),
                     DeviceHandler("g2", broken, max_batch=1)],
        dimensions=1, clock=lambda: clock[0],
        settings=SchedulerSettings(cpu_max_chunk_bytes=0, gpu_retry_cooldown_s=30),
    )
    try:
        job = scheduler.open_job("fallback")
        assert await _bounded(scheduler.embed(job, ["large"]), "forced-CPU embed") == [[9.0]]
        assert calls == 2
        assert job.device_completed == {"cpu": 1}
        assert any(g.state == "cooldown" for g in scheduler.gpus)
        scheduler.quarantine_gpu("g1", "provider mismatch")
        assert scheduler.snapshot()["gpus"][0]["state"] == "quarantined"
    finally:
        await _stop(scheduler)
