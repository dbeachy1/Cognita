"""The parent half of the GPU embedder — lease, canary, and every failure path.

🔴 **§10's table has one invariant: in EVERY row the walk completes and the
index is correct.** The only variable is how long it took. These tests are that
table, made executable — a device that dies, wedges, yields, fails its canary,
or was never there must all end with the right vectors and a finished walk.

No GPU and no subprocess: `GpuWorker` is replaced by fakes, because what needs
proving here is the parent's decision-making, and a test that needed a card
could never run in CI or on the dev box.
"""

from __future__ import annotations

import threading

import pytest

from cognita import gpu_host
from cognita.embed_telemetry import record_batch
from cognita.gpu_host import (
    LEASE,
    GpuLease,
    GpuPool,
    GpuUnavailable,
    WorkerStats,
    check_canary,
    embed_with_pool,
)
from cognita.gpu_probe import GIB, GpuDevice


class Config:
    gpu_enabled = True
    gpu_venv_python = "/tmp/fake/bin/python"
    gpu_batch_size = 4
    gpu_slice_chunks = 512
    gpu_reserve_vram_gb = 4.0
    gpu_max_busy_percent = 20
    gpu_device_ids: list[str] = []
    gpu_provider = "migraphx"
    gpu_worker_shutdown_s = 1.0
    gpu_worker_slice_timeout_s = 5.0
    gpu_canary_tolerance = 1e-4


def cpu_embed(texts):
    """A deterministic stand-in for the service's CPU embedder."""
    return [[float(len(t)), 0.5, -0.25] for t in texts]


class FakeWorker:
    """A worker whose behavior per slice is scripted."""

    def __init__(self, name="card1", pci="0000:03:00.0", behavior=None,
                 qualifies=True, drift=0.0, rendezvous=None):
        self.device = GpuDevice(name, pci, None, name, 32 * GIB, 32 * GIB, 0)
        self.stats = WorkerStats(device=name, pci_address=pci)
        self.proc = object()
        self._behavior = list(behavior or [])
        self._qualifies = qualifies
        self._drift = drift
        # A real worker blocks on a pipe for seconds. Without that, one thread
        # can drain the whole queue before another starts, which hides the very
        # thing the shared queue exists to provide. The stand-in for that
        # blocking is a RENDEZVOUS, not a sleep: on its first slice this worker
        # waits at a barrier that only the OTHER worker can release. So the
        # slice is held open until the peer is also mid-slice, and a pool that
        # ran its devices one after the other cannot pass it. No clock is read;
        # the barrier's timeout is a hang guard on a signal a correct pool
        # always delivers.
        self._rendezvous = rendezvous
        self.met_peer: bool | None = None  # None: never asked; False: never came
        self.terminated = False
        self.seen: list[list[str]] = []

    def embed(self, texts, timeout, record=True):
        self.seen.append(list(texts))
        if self._rendezvous is not None and self.met_peer is None:
            try:
                self._rendezvous.wait()
                self.met_peer = True
            except threading.BrokenBarrierError:
                self.met_peer = False
        action = self._behavior.pop(0) if self._behavior else "ok"
        if action == "die":
            self.proc = None
            raise GpuUnavailable("worker exited mid-slice")
        if action == "hang":
            raise GpuUnavailable("worker wedged")
        self.stats.chunks += len(texts)
        self.stats.batches += 1
        # Mirrors the real GpuWorker.embed, which reports into the open embed
        # job as well as its own stats. Without this the fake bypasses the
        # telemetry path entirely and any test of it is vacuous.
        record_batch(self.device.sysfs_name, chunks=len(texts),
                     chars=sum(len(t) for t in texts), elapsed=0.001)
        return [[v + self._drift for v in row] for row in cpu_embed(texts)]

    def still_qualifies(self):
        return (True, "ok") if self._qualifies else (False, "vram_free=1.00GB")

    def terminate(self, grace=None):
        self.terminated = True
        self.proc = None


def rendezvous_of_two():
    """A barrier two workers meet at while each holds a slice.

    The timeout is only a hang guard: it never fires when both devices really
    embed at once, and it is the failure (as `met_peer is False`) when they take
    turns, because the first worker then waits alone.
    """
    return threading.Barrier(2, timeout=10)


def pool_of(*workers):
    return GpuPool(workers=list(workers), holder="test")


def test_pool_shutdown_remains_retryable_after_teardown_raises(monkeypatch):
    holder = "retryable-shutdown-test"
    attempts = 0

    class FlakyWorker:
        proc = object()

        def terminate(self):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RuntimeError("synthetic teardown failure")
            self.proc = None

    pool = GpuPool(workers=[FlakyWorker()], holder=holder)
    monkeypatch.setattr(gpu_host, "log_device_rows", lambda _pool: None)
    assert LEASE.acquire(holder)
    try:
        with pytest.raises(RuntimeError, match="synthetic teardown failure"):
            pool.shutdown()
        assert not pool._down
        assert LEASE.holder == holder

        pool.shutdown()
        assert pool._down
        assert attempts == 2
        assert LEASE.holder is None
    finally:
        LEASE.release(holder)


def test_native_program_cache_path_is_preserved(monkeypatch, tmp_path):
    """Manual/native deployments retain their explicitly configured cache."""
    monkeypatch.setattr(gpu_host, "_running_in_container", lambda: False)
    configured = tmp_path / "native-migraphx-cache"
    cfg = Config()
    cfg.gpu_program_cache_dir = str(configured)

    assert gpu_host._prepare_program_cache_dir(cfg) == str(configured)
    assert not configured.exists(), "native validation must not create a host path"


def test_container_program_cache_remaps_legacy_host_path_and_probes_writes(
    monkeypatch, tmp_path
):
    """A host-only path must never reach a container MIGraphX worker."""
    model_root = tmp_path / "models"
    monkeypatch.setattr(gpu_host, "_CONTAINER_MODEL_CACHE_ROOT", model_root)
    monkeypatch.setattr(gpu_host, "_running_in_container", lambda: True)
    cfg = Config()
    cfg.gpu_program_cache_dir = "/home/tester/Cognita/migraphx-cache"

    effective = gpu_host._prepare_program_cache_dir(cfg)

    assert effective == str(model_root / "migraphx-cache")
    assert (model_root / "migraphx-cache").is_dir()
    assert not list((model_root / "migraphx-cache").glob(".cognita-cache-write-*"))


def test_container_program_cache_fails_before_worker_spawn_when_unwritable(
    monkeypatch, tmp_path
):
    model_root = tmp_path / "models-file"
    model_root.write_text("not a directory", encoding="utf-8")
    monkeypatch.setattr(gpu_host, "_CONTAINER_MODEL_CACHE_ROOT", model_root)
    monkeypatch.setattr(gpu_host, "_running_in_container", lambda: True)
    cfg = Config()
    cfg.gpu_program_cache_dir = "/home/tester/Cognita/migraphx-cache"

    with pytest.raises(GpuUnavailable, match="GPU program cache is not writable"):
        gpu_host._prepare_program_cache_dir(cfg)


# --------------------------------------------------------------------------
# §8.7 the lease
# --------------------------------------------------------------------------


def test_only_one_holder_at_a_time():
    lease = GpuLease()
    assert lease.acquire("walk-a") is True
    assert lease.acquire("walk-b") is False
    lease.release("walk-a")
    assert lease.acquire("walk-b") is True


def test_releasing_from_a_non_holder_does_nothing():
    """Otherwise a finishing walk could free a lease another walk now owns, and
    two pools would run against a budget that admitted one."""
    lease = GpuLease()
    lease.acquire("walk-a")
    lease.release("walk-b")
    assert lease.holder == "walk-a"


def test_a_job_that_cannot_take_the_lease_gets_no_pool(monkeypatch):
    """🔴 §8.7: it runs on CPU rather than queueing behind the holder. The CPU
    path is always available, and blocking an index behind an unrelated
    project's rebuild would be a worse outcome than running it slower."""
    assert LEASE.acquire("someone-else")
    try:
        assert gpu_host.start_pool(Config(), object(), "me", 3.62) is None
    finally:
        LEASE.release("someone-else")


# --------------------------------------------------------------------------
# The ordinary "use the CPU" answers — none of these is an error
# --------------------------------------------------------------------------


def test_gpu_disabled_returns_no_pool():
    cfg = Config()
    cfg.gpu_enabled = False
    assert gpu_host.start_pool(cfg, object(), "job", 3.62) is None


def test_no_worker_environment_returns_no_pool():
    """gpu_venv_python is the real master switch: unset on every machine that
    has not deliberately built one, which makes "no GPU" the default everywhere
    without anyone configuring anything."""
    cfg = Config()
    cfg.gpu_venv_python = ""
    assert gpu_host.start_pool(cfg, object(), "job", 3.62) is None


def test_no_qualifying_device_returns_no_pool_and_frees_the_lease():
    """A held lease after a failed start would lock every later walk out of the
    GPU for the life of the process."""
    class Probe:
        def devices(self):
            return [GpuDevice("card3", "0000:7e:00.0", None, "igpu",
                              2 * GIB, 1 * GIB, 0)]

    assert gpu_host.start_pool(Config(), Probe(), "job", 3.62) is None
    assert LEASE.holder is None


# --------------------------------------------------------------------------
# §9.1 the canary
# --------------------------------------------------------------------------


def test_a_matching_canary_passes():
    worker = FakeWorker()
    pool = pool_of(worker)
    assert check_canary(pool, cpu_embed, 1e-4) == [worker]
    assert worker.terminated is False
    assert worker.stats.canary_delta == pytest.approx(0.0)


def test_a_drifting_canary_terminates_the_device():
    """🔴 The spike caught a provider that loaded, reported itself active, ran
    at 8.6x and returned vectors with a cosine of 0.536 against the CPU. Without
    this check that ships a fast, silent index corruption that count-based
    checks cannot see."""
    worker = FakeWorker(drift=0.01)
    pool = pool_of(worker)
    assert check_canary(pool, cpu_embed, 1e-4) == []
    assert worker.terminated is True
    assert "canary" in worker.stats.failed_reason


def test_one_bad_device_does_not_cost_the_good_one():
    good = FakeWorker(name="card1")
    bad = FakeWorker(name="card2", pci="0000:07:00.0", drift=0.5)
    pool = pool_of(good, bad)
    assert [w.device.sysfs_name for w in check_canary(pool, cpu_embed, 1e-4)] == ["card1"]
    assert bad.terminated and not good.terminated


def test_the_reference_is_computed_not_stored():
    """§9.1: compute the reference LOCALLY, never ship it as a constant.
    Different CPU microarchitectures take different ORT kernel paths, so a
    stored vector would fail spuriously on hardware that is working perfectly."""
    calls = []

    def recording_cpu_embed(texts):
        calls.append(texts)
        return cpu_embed(texts)

    check_canary(pool_of(FakeWorker()), recording_cpu_embed, 1e-4)
    assert calls, "the canary must ask THIS machine's CPU embedder"


def test_a_canary_that_cannot_run_drops_the_device():
    worker = FakeWorker(behavior=["die"])
    assert check_canary(pool_of(worker), cpu_embed, 1e-4) == []
    assert worker.terminated


# --------------------------------------------------------------------------
# §10 — every row ends with the walk completing
# --------------------------------------------------------------------------


def test_vectors_come_back_in_input_order():
    """Order IS chunk identity — the worker never sees documents. Reordering
    attaches every vector to the wrong chunk, producing an index that looks
    complete and answers wrongly."""
    texts = [f"text-{i}" for i in range(10)]
    out = embed_with_pool(pool_of(FakeWorker()), texts, Config(), cpu_embed)
    assert out == cpu_embed(texts)


def test_work_is_spread_across_devices():
    meet = rendezvous_of_two()
    a = FakeWorker("card1", rendezvous=meet)
    b = FakeWorker("card2", "0000:07:00.0", rendezvous=meet)
    cfg = Config()
    cfg.gpu_slice_chunks = 4
    texts = [f"t{i}" for i in range(16)]
    embed_with_pool(pool_of(a, b), texts, cfg, cpu_embed)
    assert a.stats.chunks and b.stats.chunks, "one device did all the work"
    assert a.stats.chunks + b.stats.chunks == 16


def test_devices_embed_CONCURRENTLY_not_in_turns():
    """🔴 The bug this catches shipped and was invisible to every other test.

    A sequential `for slice: pick a worker; wait` loop produces correct vectors,
    spreads work across devices, and leaves balanced per-device counters — so
    results, distribution and telemetry all look right. The cards simply TAKE
    TURNS, and two devices deliver one device's throughput. Measured live as
    79.1s on one card against 25.2s on the other for an identical 1018 chunks.

    Nothing here reads a clock. Each fake worker, on its first slice, waits at a
    barrier that only the OTHER worker can release. If the two devices embed at
    the same time, each finds the other mid-slice and both pass. If they take
    turns, the first one waits alone, the barrier's hang guard breaks it, and
    `met_peer` is False — a failure by a signal that a correct pool always
    delivers, not by measuring elapsed time.
    """
    meet = rendezvous_of_two()
    a = FakeWorker("card1", rendezvous=meet)
    b = FakeWorker("card2", "0000:07:00.0", rendezvous=meet)
    cfg = Config()
    cfg.gpu_slice_chunks = 4
    texts = [f"t{i}" for i in range(16)]  # 4 slices

    out = embed_with_pool(pool_of(a, b), texts, cfg, cpu_embed)

    assert out == cpu_embed(texts), "concurrency must not disturb order"
    assert a.met_peer is True and b.met_peer is True, (
        f"the devices never held a slice at the same time "
        f"(card1 met_peer={a.met_peer}, card2 met_peer={b.met_peer}) — "
        "they are taking turns"
    )


def test_a_dying_worker_has_its_slice_retried_elsewhere():
    dying = FakeWorker("card1", behavior=["die"])
    healthy = FakeWorker("card2", "0000:07:00.0")
    texts = [f"t{i}" for i in range(8)]
    out = embed_with_pool(pool_of(dying, healthy), texts, Config(), cpu_embed)

    assert out == cpu_embed(texts), "the walk must still produce every vector"
    assert dying.terminated
    assert healthy.stats.chunks == 8


def test_a_wedged_worker_is_dropped_and_the_walk_finishes():
    """⚠️ A hang is a DISTINCT failure from a crash. A crashed worker is obvious
    — the pipe closes. A wedged one holds its slice, holds its VRAM, and looks
    identical to a slow one, so without a bound the walk never finishes."""
    wedged = FakeWorker("card1", behavior=["hang"])
    texts = [f"t{i}" for i in range(8)]
    out = embed_with_pool(pool_of(wedged), texts, Config(), cpu_embed)

    assert out == cpu_embed(texts)
    assert wedged.terminated


def test_every_device_failing_drains_on_the_cpu():
    """§8.5: the CPU embedder is never unloaded, which is what makes fallback
    instant when every worker dies."""
    a = FakeWorker("card1", behavior=["die"])
    b = FakeWorker("card2", "0000:07:00.0", behavior=["die"])
    pool = pool_of(a, b)
    texts = [f"t{i}" for i in range(8)]
    out = embed_with_pool(pool, texts, Config(), cpu_embed)

    assert out == cpu_embed(texts)
    assert pool.cpu_fallback_chunks == 8


def test_a_yielding_device_is_terminated_not_idled():
    """🔴 §6.4: an idle worker still holds its weights, its arena and its driver
    context. The user asked for the card back and would get a process still
    sitting on gigabytes of it."""
    yielding = FakeWorker("card1", qualifies=False)
    other = FakeWorker("card2", "0000:07:00.0")
    texts = [f"t{i}" for i in range(8)]
    out = embed_with_pool(pool_of(yielding, other), texts, Config(), cpu_embed)

    assert yielding.terminated is True
    assert yielding.stats.yielded_reason
    assert out == cpu_embed(texts)


def test_an_empty_slice_list_is_not_an_error():
    assert embed_with_pool(pool_of(FakeWorker()), [], Config(), cpu_embed) == []


def test_a_slice_is_an_ipc_request_not_a_forward_pass():
    """🔴 gpu_slice_chunks and gpu_batch_size are DIFFERENT numbers.

    The batch size is the model's forward-pass width and therefore the compiled
    tensor shape — it must stay stable or MIGraphX recompiles. The slice is one
    request handed to a worker, which runs it as several forward passes.
    Conflating them measured at 33.5 chunks/s against a device that sustains far
    more, because a fixed per-call cost inside fastembed dominates a small
    request.
    """
    worker = FakeWorker()
    cfg = Config()
    cfg.gpu_batch_size = 4
    cfg.gpu_slice_chunks = 100
    embed_with_pool(pool_of(worker), [f"t{i}" for i in range(250)], cfg, cpu_embed)

    assert [len(s) for s in worker.seen] == [100, 100, 50]
    assert max(len(s) for s in worker.seen) > cfg.gpu_batch_size


def test_a_slice_shrinks_so_every_device_gets_work():
    """The other end of the trade. One enormous slice would hand the whole
    window to one card and idle the rest — the §6.3 failure this feature exists
    to avoid, arriving through the tuning knob rather than the gate."""
    meet = rendezvous_of_two()
    a = FakeWorker("card1", rendezvous=meet)
    b = FakeWorker("card2", "0000:07:00.0", rendezvous=meet)
    cfg = Config()
    cfg.gpu_slice_chunks = 10_000  # far larger than the work
    embed_with_pool(pool_of(a, b), [f"t{i}" for i in range(200)], cfg, cpu_embed)

    assert a.seen and b.seen, "one card took everything and the other idled"
    assert a.stats.chunks + b.stats.chunks == 200


def test_every_chunk_is_dispatched_exactly_once_when_slices_are_uneven():
    """⚠️ THIS TEST USED TO BE UNFALSIFIABLE, and it named the wrong property.

    It asserted `min(seen) >= min(64, 80) or sum(seen) == 80`. The second
    disjunct is true whenever every text is embedded exactly once — which
    another test already guarantees — so the first was never load-bearing. And
    the first was *false*: 80 chunks at a 64-wide forward pass dispatches as
    [64, 16], so the trailing slice IS below the compiled width.

    That turns out not to matter, which is the real point: `embed_batch` pads
    every slice UP to a multiple of `gpu_batch_size` and discards the padding
    (§5.1), so a 16-chunk slice is presented as one 64-wide shape and compiles
    nothing new. The shape guarantee lives in the worker's padding, not in the
    slicer — so what the slicer actually owes callers is exact, once-only
    coverage, which is what this now checks.
    """
    workers = [FakeWorker(f"card{i}", f"0000:0{i}:00.0") for i in range(4)]
    cfg = Config()
    cfg.gpu_batch_size = 64
    cfg.gpu_slice_chunks = 512
    texts = [f"t{i}" for i in range(80)]
    out = embed_with_pool(pool_of(*workers), texts, cfg, cpu_embed)

    dispatched = [t for w in workers for s in w.seen for t in s]
    assert sorted(dispatched) == sorted(texts), (
        "every chunk must be dispatched exactly once — no drops, no duplicates"
    )
    assert out == cpu_embed(texts), "and come back in input order"


# --------------------------------------------------------------------------
# §8.8 the release proof
# --------------------------------------------------------------------------


def test_released_reports_ok_when_vram_returns():
    stats = WorkerStats(device="card1", pci_address="x",
                        vram_free_before=32 * GIB, vram_free_after=32 * GIB)
    assert stats.released == "OK"


def test_released_names_the_shortfall():
    """🔴 §8.8: a design document claiming memory is released is worth nothing.
    This repo has shipped that claim and been wrong for five releases, so the
    check has to be falsifiable and it has to say the number."""
    stats = WorkerStats(device="card1", pci_address="x",
                        vram_free_before=32 * GIB,
                        vram_free_after=30 * GIB)
    assert stats.released.startswith("FAIL")
    assert "2.000GiB" in stats.released


def test_small_churn_is_tolerated():
    """The gate is advisory and so is this: another process may take a little
    memory between the two samples."""
    stats = WorkerStats(device="card1", pci_address="x",
                        vram_free_before=32 * GIB,
                        vram_free_after=32 * GIB - 8 * 1024 * 1024)
    assert stats.released == "OK"


def test_unknown_when_never_sampled():
    """A worker that never started must not claim its VRAM came back."""
    assert WorkerStats(device="card1", pci_address="x").released == "unknown"


def test_every_device_reports_into_the_job(tmp_path):
    """🔴 Worker threads must carry the embed job's context.

    `threading.Thread` starts with an EMPTY context — unlike `asyncio.to_thread`,
    which copies it — so `record_batch` inside a drain thread finds no active job
    and the walk's totals describe only what ran on the calling thread. Logged
    live as `chunks=3` for a walk that embedded 2,036.

    And the context must be copied PER THREAD: a Context cannot be entered twice
    at once, so one shared copy takes out every device but the first as soon as
    the second thread starts.
    """
    from cognita.embed_telemetry import embed_job

    meet = rendezvous_of_two()
    a = FakeWorker("card1", rendezvous=meet)
    b = FakeWorker("card2", "0000:07:00.0", rendezvous=meet)
    cfg = Config()
    cfg.gpu_slice_chunks = 4
    texts = [f"t{i}" for i in range(16)]

    with embed_job("P", walk="project") as job:
        out = embed_with_pool(pool_of(a, b), texts, cfg, cpu_embed)

    assert out == cpu_embed(texts)
    assert job.chunks == 16, (
        f"job saw {job.chunks} of 16 chunks — worker threads are not reporting "
        "into it"
    )
    assert set(job.devices) == {"card1", "card2"}, (
        f"only {set(job.devices)} reported; a shared context would leave just one"
    )


def test_the_worker_library_path_is_read_from_the_venv_not_the_interpreter(tmp_path):
    """🔴 A uv venv's bin/python is a SYMLINK to a shared interpreter.

    Resolving it walks out of the virtual environment and looks for the marker
    beside uv's own copy of Python, so LD_LIBRARY_PATH is never applied — and
    the failure is silent in the worst way: the worker starts, cannot load
    libmigraphx_c.so.3, ORT falls back to CPU. Only the "never run silently on
    CPU" guard turns that into a visible error rather than a slow walk nobody
    investigates. Observed exactly this way on the first live run.
    """
    venv = tmp_path / "gpu-venv"
    (venv / "bin").mkdir(parents=True)
    (venv / ".ld_library_path").write_text("/opt/migraphx/lib\n")

    real_python = tmp_path / "elsewhere" / "bin" / "python3.13"
    real_python.parent.mkdir(parents=True)
    real_python.write_text("#!/bin/sh\n")

    link = venv / "bin" / "python"
    try:
        link.symlink_to(real_python)
    except (OSError, NotImplementedError):  # Windows without developer mode
        pytest.skip("symlinks unavailable")

    assert gpu_host._worker_library_path(str(link)) == "/opt/migraphx/lib"


def test_a_missing_library_path_marker_is_not_an_error(tmp_path):
    """A worker environment whose libraries are all installed system-wide needs
    no marker, and must not be refused for lacking one."""
    venv = tmp_path / "plain"
    (venv / "bin").mkdir(parents=True)
    assert gpu_host._worker_library_path(str(venv / "bin" / "python")) == ""


def test_provider_name_mapping():
    """`rocm` maps to a provider that no longer exists on ROCm 7.1+; it is kept
    only so an existing config still loads (§5)."""
    assert gpu_host._provider_name("migraphx") == "MIGraphXExecutionProvider"
    assert gpu_host._provider_name("cuda") == "CUDAExecutionProvider"
    assert gpu_host._provider_name("") == "MIGraphXExecutionProvider"
    assert gpu_host._provider_name("nonsense") == "MIGraphXExecutionProvider"


# --------------------------------------------------------------------------
# 15.0 (DESIGN-NVIDIA-ACCELERATION §3, §6): the launch follows the profile
# --------------------------------------------------------------------------

_PROFILE_ENV = "COGNITA_ACCELERATION_PROFILE"


def test_provider_name_follows_the_profile_when_unset():
    """"" is the shipped default and means the profile's provider. The cpu
    profile (bare metal, nothing set) resolves as amd: it always was MIGraphX."""
    from cognita.acceleration_profiles import AMD, CPU, NVIDIA

    assert gpu_host._provider_name("", NVIDIA) == "CUDAExecutionProvider"
    assert gpu_host._provider_name("", AMD) == "MIGraphXExecutionProvider"
    assert gpu_host._provider_name("", CPU) == "MIGraphXExecutionProvider"
    assert gpu_host._provider_name("   ", NVIDIA) == "CUDAExecutionProvider"


def test_an_explicit_provider_wins_over_the_profile():
    from cognita.acceleration_profiles import AMD, NVIDIA

    assert gpu_host._provider_name("migraphx", NVIDIA) == "MIGraphXExecutionProvider"
    assert gpu_host._provider_name("CUDA", AMD) == "CUDAExecutionProvider"
    assert gpu_host._provider_name("rocm", AMD) == "ROCMExecutionProvider"


def test_an_unknown_provider_warns_and_takes_the_profiles(caplog):
    """The config validator refuses these at load, so this is reachable only by
    a caller that skipped it. On a CUDA image guessing MIGraphX would build a
    worker that can never start."""
    import logging

    from cognita.acceleration_profiles import NVIDIA

    with caplog.at_level(logging.WARNING, logger="cognita.gpu"):
        assert gpu_host._provider_name("tensorrt", NVIDIA) == "CUDAExecutionProvider"
    assert "tensorrt" in caplog.text


def test_provider_name_reads_the_environment_when_no_profile_is_given(monkeypatch):
    monkeypatch.setenv(_PROFILE_ENV, "nvidia")
    assert gpu_host._provider_name("") == "CUDAExecutionProvider"
    monkeypatch.delenv(_PROFILE_ENV)
    assert gpu_host._provider_name("") == "MIGraphXExecutionProvider"


class _LaunchConfig(Config):
    """Everything `GpuWorker.start()` reads, with the fields a launch test varies
    left to each test."""

    gpu_provider = ""
    embedding_model = "BAAI/bge-small-en-v1.5"
    embedding_dimensions = 384
    gpu_model_cache_dir = ""
    models_cache_dir = "/tmp/models"
    gpu_program_cache_dir = ""
    gpu_worker_startup_timeout_s = 5.0


class _OneCardProbe:
    def __init__(self, device):
        self._device = device

    def devices(self):
        return [self._device]


def _launch(monkeypatch, config, profile=None, unique_id="uid-1"):
    """Run `GpuWorker.start()` up to the spawn and hand back (argv, env).

    `Popen` is replaced by a recorder that raises OSError, which `start()`
    already turns into `GpuUnavailable` — so nothing is launched and the
    decisions under test are exactly what `start()` computed.
    """
    if profile is None:
        monkeypatch.delenv(_PROFILE_ENV, raising=False)
    else:
        monkeypatch.setenv(_PROFILE_ENV, profile)
    for name in ("ROCR_VISIBLE_DEVICES", "CUDA_VISIBLE_DEVICES"):
        monkeypatch.delenv(name, raising=False)
    # A native run keeps a configured cache path exactly; inside a container it
    # would be remapped (its own tests cover that), which is not under test here.
    monkeypatch.setattr(gpu_host, "_running_in_container", lambda: False)
    seen: dict = {}

    def fake_popen(argv, **kwargs):
        seen["argv"] = list(argv)
        seen["env"] = kwargs["env"]
        raise OSError("recorded, not launched")

    monkeypatch.setattr(gpu_host.subprocess, "Popen", fake_popen)
    device = GpuDevice("card1", "0000:03:00.0", unique_id, "card1",
                       32 * GIB, 32 * GIB, 0)
    worker = gpu_host.GpuWorker(device, config, _OneCardProbe(device))
    with pytest.raises(GpuUnavailable, match="recorded, not launched"):
        worker.start()
    return seen["argv"], seen["env"]


def _flag(argv, name):
    return argv[argv.index(name) + 1]


def test_the_nvidia_launch_scopes_by_cuda_and_does_not_pin_or_cache(
        monkeypatch, tmp_path):
    cache = tmp_path / "must-not-be-created"

    class Cfg(_LaunchConfig):
        gpu_program_cache_dir = str(cache)

    argv, env = _launch(monkeypatch, Cfg(), profile="nvidia",
                        unique_id="4cd28834-e5a4-6b4e-85aa-3e54bcbf0630")
    assert _flag(argv, "--provider") == "CUDAExecutionProvider"
    assert _flag(argv, "--fixed-seq-len") == "0", "dynamic shapes on CUDA"
    assert "--program-cache-dir" not in argv, "MIGraphX-only"
    assert not cache.exists(), "the cache directory is never even prepared"
    assert env["CUDA_VISIBLE_DEVICES"] == "GPU-4cd28834-e5a4-6b4e-85aa-3e54bcbf0630"
    assert "ROCR_VISIBLE_DEVICES" not in env


def test_an_explicit_sequence_length_wins_on_nvidia(monkeypatch):
    class Cfg(_LaunchConfig):
        gpu_fixed_seq_len = 256

    argv, _ = _launch(monkeypatch, Cfg(), profile="nvidia")
    assert _flag(argv, "--fixed-seq-len") == "256"


def test_an_explicit_zero_is_honored_on_amd(monkeypatch):
    """0 means "do not pin" and it was not sayable before 15.0 (it was refused
    as non-positive); it must survive the -1 = "the profile's" resolution."""
    class Cfg(_LaunchConfig):
        gpu_fixed_seq_len = 0

    argv, _ = _launch(monkeypatch, Cfg(), profile="amd")
    assert _flag(argv, "--fixed-seq-len") == "0"


def test_the_amd_launch_is_todays(monkeypatch, tmp_path):
    class Cfg(_LaunchConfig):
        gpu_program_cache_dir = str(tmp_path / "mxr")

    argv, env = _launch(monkeypatch, Cfg(), profile="amd", unique_id="0123abcd")
    assert _flag(argv, "--provider") == "MIGraphXExecutionProvider"
    assert _flag(argv, "--fixed-seq-len") == "512"
    assert _flag(argv, "--program-cache-dir") == str(tmp_path / "mxr")
    assert env["ROCR_VISIBLE_DEVICES"] == "GPU-0123abcd"
    assert "CUDA_VISIBLE_DEVICES" not in env


def test_the_cpu_profile_launches_exactly_as_amd_did(monkeypatch, tmp_path):
    """No profile set is the bare-metal `cognita serve` and every test that does
    not set one. Before profiles existed every GPU default was MIGraphX's."""
    class Cfg(_LaunchConfig):
        gpu_program_cache_dir = str(tmp_path / "mxr")

    argv, env = _launch(monkeypatch, Cfg(), profile=None, unique_id="0123abcd")
    assert _flag(argv, "--provider") == "MIGraphXExecutionProvider"
    assert _flag(argv, "--fixed-seq-len") == "512"
    assert _flag(argv, "--program-cache-dir") == str(tmp_path / "mxr")
    assert env["ROCR_VISIBLE_DEVICES"] == "GPU-0123abcd"


def test_an_explicit_provider_is_honored_whatever_the_profile(monkeypatch):
    class Cfg(_LaunchConfig):
        gpu_provider = "cuda"

    argv, _ = _launch(monkeypatch, Cfg(), profile="amd")
    assert _flag(argv, "--provider") == "CUDAExecutionProvider"


def test_a_card_with_no_unique_id_is_not_scoped(monkeypatch):
    """The AMD integrated-part branch, kept: no id, no scoping variable."""
    _, env = _launch(monkeypatch, _LaunchConfig(), profile="amd", unique_id=None)
    assert "ROCR_VISIBLE_DEVICES" not in env


def test_a_real_config_passes_its_values_through_to_the_worker_argv(monkeypatch):
    """The shipped defaults ("" and -1) resolve per profile, and explicit values
    reach the argv untouched — through the REAL config class, so a validator or
    default change cannot drift from what `start()` reads."""
    from cognita.config import CognitaConfig

    def config(**kw):
        return CognitaConfig(gpu_enabled=True, gpu_venv_python="/x/python", **kw)

    argv, _ = _launch(monkeypatch, config(), profile="nvidia")
    assert _flag(argv, "--provider") == "CUDAExecutionProvider"
    assert _flag(argv, "--fixed-seq-len") == "0"
    argv, _ = _launch(monkeypatch, config(), profile="amd")
    assert _flag(argv, "--provider") == "MIGraphXExecutionProvider"
    assert _flag(argv, "--fixed-seq-len") == "512"
    argv, _ = _launch(monkeypatch, config(gpu_provider="cuda", gpu_fixed_seq_len=128),
                      profile="amd")
    assert _flag(argv, "--provider") == "CUDAExecutionProvider"
    assert _flag(argv, "--fixed-seq-len") == "128"


def test_the_launch_decision_is_logged_with_its_reasons(monkeypatch, caplog):
    """Which profile, which provider, and WHY the program cache was skipped are
    the three facts a 'why is the CUDA worker slow / failing' report needs. The
    card's UUID is not among them."""
    import logging

    with caplog.at_level(logging.INFO, logger="cognita.gpu"):
        _launch(monkeypatch, _LaunchConfig(), profile="nvidia",
                unique_id="4cd28834-e5a4-6b4e-85aa-3e54bcbf0630")
    text = caplog.text
    assert "profile=nvidia" in text
    assert "provider=CUDAExecutionProvider" in text
    assert "fixed_seq_len=0 (profile)" in text
    assert "program_cache=skipped (profile nvidia has no program cache)" in text
    assert "device_env=CUDA_VISIBLE_DEVICES" in text
    assert "4cd28834" not in text


def test_the_ceiling_constants_follow_the_profile(monkeypatch):
    """Every caller of `batch_ceiling_gb` goes through the one function, which
    reads the profile from the environment when it is not told one."""
    from cognita.gpu_probe import batch_ceiling_gb

    monkeypatch.setenv(_PROFILE_ENV, "nvidia")
    assert batch_ceiling_gb(4) == 2.86
    monkeypatch.setenv(_PROFILE_ENV, "amd")
    assert batch_ceiling_gb(64) == 3.62
    monkeypatch.delenv(_PROFILE_ENV)
    assert batch_ceiling_gb(64) == 3.62


def test_the_death_wording_names_every_native_runtime():
    assert "HIP/MIGraphX/CUDA/ORT" in gpu_host._SIGNAL_MEANING["SIGSEGV"]
    assert "vendor runtime fatal check" in gpu_host._SIGNAL_MEANING["SIGABRT"]
    assert "ROCm" not in gpu_host._SIGNAL_MEANING["SIGABRT"]


def test_gpu_unavailable_carries_an_optional_reason():
    assert GpuUnavailable("plain").reason is None
    err = GpuUnavailable("no card (reason=driver_too_old)", reason="driver_too_old")
    assert err.reason == "driver_too_old"
    assert "driver_too_old" in str(err)
