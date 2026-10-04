"""Shared per-chunk indexing scheduler (DESIGN-10.0, sections 3--11).

The scheduler is deliberately independent of retrieval and storage.  It owns
only text chunks, queues, attempts, and device adapters.  A host constructs
one instance and gives it to every retrieval core in that host.  Real GPU
process plumbing can be supplied through :class:`DeviceHandler`; tests use
the same interface with deterministic fakes and clocks.

Device calls are never made while ``_lock`` is held.  A chunk has one owner at
all times (ready queue, one attempt, or its result future), and an attempt's
generation is checked before accepting a result.  This makes retry and
cancellation safe even when a late native/GPU call completes.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import math
import time
import uuid
from collections import defaultdict, deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from .embed_telemetry import current_job, outside_the_job

log = logging.getLogger("cognita.index_scheduler")


class SchedulerError(RuntimeError):
    """Base class for scheduler failures."""


class JobCanceled(SchedulerError):
    """Raised when a caller or host cancels an indexing job."""


class EmbeddingError(SchedulerError):
    """Raised when no valid device result can be produced for a chunk."""


class GpuContention(SchedulerError):
    """Raised when GPU qualification is temporarily blocked by external load.

    ``gpu_host.start_pool`` deliberately returns ``None`` when no card passes
    its safety gate.  That is an ordinary CPU-fallback decision when another
    workload owns the cards, not a per-card startup
    failure.  Keeping a distinct exception lets the scheduler suppress the
    warning/retry fan-out while retaining the normal qualification checks.
    """


class GpuQualificationFailure(SchedulerError):
    """Raised when a shared GPU runtime fails its startup qualification.

    The worker process may include host paths or provider stderr in its private
    diagnostics.  The scheduler carries only a bounded reason code across the
    adapter boundary, so a shared canary/startup failure can be quarantined
    without exposing that detail through health or logs.  A shared program
    cache prerequisite failure is retryable after its bounded cooldown, and so
    (15.0.2) is a worker that fails to start on a card that has already been
    ready in this process; other qualification failures remain terminal for the
    service lifetime.
    """

    def __init__(self, reason_code: str):
        self.reason_code = reason_code
        super().__init__(reason_code)


@dataclass(slots=True)
class SchedulerSettings:
    """Bounded queue and lifecycle settings.

    Values mirror the release design defaults.  The scheduler accepts a
    config-like object too, so deployments can keep their existing config
    model without importing it here.
    """

    max_chunks: int = 8192
    max_text_bytes: int = 16 * 1024 * 1024
    cpu_max_chunk_bytes: int = 1024
    gpu_min_chunks: int = 20
    gpu_slice_chunks: int = 512
    gpu_retry_cooldown_s: float = 30.0
    gpu_probe_interval_s: float = 5.0
    gpu_idle_linger_s: float = 30.0

    @classmethod
    def from_config(cls, config: Any | None) -> SchedulerSettings:
        if config is None:
            return cls()
        def value(name: str, default: Any) -> Any:
            return getattr(config, name, default)
        return cls(
            max_chunks=max(1, int(value("index_scheduler_max_chunks", 8192))),
            max_text_bytes=max(1, int(value("index_scheduler_max_text_bytes", 16 * 1024 * 1024))),
            cpu_max_chunk_bytes=max(1, int(value("index_cpu_max_chunk_bytes", 1024))),
            gpu_min_chunks=max(0, int(value("gpu_min_chunks", 20))),
            gpu_slice_chunks=max(1, int(value("gpu_slice_chunks", 512))),
            gpu_retry_cooldown_s=max(0.0, float(value("gpu_retry_cooldown_s", 30.0))),
            gpu_probe_interval_s=max(0.1, float(value("gpu_probe_interval_s", 5.0))),
            gpu_idle_linger_s=max(0.0, float(value("gpu_idle_linger_s", 30.0))),
        )


@dataclass
class DeviceHandler:
    """One independent CPU or GPU execution lane.

    ``embed`` may be synchronous or asynchronous.  ``start`` and ``stop``
    have the same flexibility.  A GPU handler is considered ready only after
    its caller has completed provider/canary qualification; a start callback
    may be used to perform that work asynchronously before setting it ready.
    """

    device_id: str
    embed: Callable[[list[str]], Any]
    kind: str = "gpu"
    max_batch: int = 512
    state: str = "ready"
    reason: str = ""
    generation: int = 0
    start: Callable[[], Any] | None = None
    stop: Callable[[], Any] | None = None
    available: bool = True
    completed: int = 0
    attempted: int = 0
    failures: int = 0
    _busy: bool = field(default=False, repr=False)
    _turn: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)
    _start_task: asyncio.Task | None = field(default=None, repr=False)
    _cooldown_until: float = field(default=0.0, repr=False)
    # Set the first time this card becomes ready in this process.  A card that
    # has worked before and fails to START again is retried, not quarantined
    # (see `start_gpu`).
    _was_ready: bool = field(default=False, repr=False)

    @property
    def ready(self) -> bool:
        return self.available and self.state == "ready" and self._cooldown_until <= time.monotonic()


class GpuHostRuntime:
    """Adapter for the existing isolated ``gpu_host`` worker protocol.

    ``gpu_host.start_pool`` performs the provider, VRAM, identity and canary
    checks and creates one worker per qualifying physical GPU.  This runtime
    calls it once (single-flight) and exposes each resulting worker as an
    independent scheduler lane.  No pool or lease is owned by a project/job.
    """

    def __init__(self, config: Any, probe: Any, cpu_embed: Callable[[list[str]], Any],
                 batch_ceiling_gb: float, holder: str,
                 clock: Callable[[], float] = time.monotonic):
        # ``clock`` times the qualification retry window.  Production keeps
        # ``time.monotonic``; tests inject a fake so a cache-repair retry is
        # decided by an advanced clock, never by a real sleep.
        self.clock = clock
        self.config = config
        self.probe = probe
        self.cpu_embed = cpu_embed
        self.batch_ceiling_gb = batch_ceiling_gb
        self.holder = holder
        self.pool: Any | None = None
        self._ensure_task: asyncio.Task | None = None
        self._lock = asyncio.Lock()
        self._workers: dict[str, Any] = {}
        self._qualification_failure_reason: str | None = None
        self._qualification_retry_at = 0.0

    def _qualification_retry_delay(self) -> float:
        """Return the bounded delay before retrying a cache prerequisite.

        The scheduler uses the same setting for its device cooldown.  Keep a
        small positive floor even when a test or deployment sets that setting
        to zero so a repaired cache cannot trigger a tight startup loop.
        """
        try:
            configured = float(getattr(self.config, "gpu_retry_cooldown_s", 30.0))
        except (TypeError, ValueError):
            configured = 30.0
        return max(0.01, configured)

    def _deferred_qualification_failure(self) -> GpuQualificationFailure | None:
        reason = self._qualification_failure_reason
        if reason is None:
            return None
        if self.clock() < self._qualification_retry_at:
            return GpuQualificationFailure(reason)
        self._qualification_failure_reason = None
        self._qualification_retry_at = 0.0
        return None

    async def _ensure(self) -> None:
        deferred = self._deferred_qualification_failure()
        if deferred is not None:
            raise deferred
        if self.pool is not None and self.pool.alive:
            return
        async with self._lock:
            deferred = self._deferred_qualification_failure()
            if deferred is not None:
                raise deferred
            if self.pool is not None and self.pool.alive:
                return
            if self._ensure_task is None:
                self._ensure_task = asyncio.create_task(self._start(),
                                                        name="cognita-gpu-runtime-start")
            task = self._ensure_task
        try:
            await task
        finally:
            if task.done():
                async with self._lock:
                    if self._ensure_task is task:
                        self._ensure_task = None

    async def _start(self) -> None:
        from . import gpu_host
        # 15.0.2: a pool whose every worker has gone (a card yielded to another
        # program, or a slice failed) still holds the process-wide LEASE until
        # `shutdown()` runs.  Starting a new pool over it asked for a lease this
        # runtime already held, got "busy", and fell back to the CPU on every
        # retry for the life of the process, however long ago the memory came
        # back (Maia's NVIDIA proof, 2026-09-30; the same path serves AMD).
        # §8.2: tear the dead one down, which releases the lease, then start.
        stale = self.pool
        if stale is not None and not stale.alive:
            log.info("gpu runtime: the previous pool has no live worker; tearing it down "
                     "to release the GPU lease before starting again")
            await asyncio.to_thread(stale.shutdown)
            if self.pool is stale:
                self.pool = None
            self._workers.clear()
        if (getattr(self.config, "gpu_enabled", False)
                and getattr(self.config, "gpu_venv_python", None)
                and gpu_host.gpu_settings_profile(
                    gpu_host.current_profile()
                ).program_cache):
            try:
                # start_pool treats all worker-start failures as CPU fallback.
                # Validate this shared deterministic prerequisite first so a
                # bad container cache is not misreported as card contention.
                await asyncio.to_thread(gpu_host._prepare_program_cache_dir, self.config)
            except gpu_host.GpuUnavailable as exc:
                reason = "program_cache_unavailable"
                self._qualification_failure_reason = reason
                self._qualification_retry_at = (
                    self.clock() + self._qualification_retry_delay()
                )
                log.warning("gpu runtime qualification failed reason=%s", reason)
                raise GpuQualificationFailure(reason) from exc
        pool = await asyncio.to_thread(
            gpu_host.start_pool, self.config, self.probe, self.holder,
            self.batch_ceiling_gb,
        )
        if pool is None:
            raise GpuContention("no qualifying GPU worker")
        self.pool = pool
        try:
            attempted_workers = list(pool.workers)
            passed = await asyncio.to_thread(
                gpu_host.check_canary, pool, self.cpu_embed,
                self.config.gpu_canary_tolerance,
            )
            pool.workers = passed
            self._workers = {w.device.sysfs_name: w for w in passed}
            if not self._workers:
                reason_codes = {
                    self._qualification_reason(worker)
                    for worker in attempted_workers
                }
                reason = (
                    "canary_failed"
                    if reason_codes and reason_codes <= {"canary_failed"}
                    else "worker_startup_failed"
                )
                log.warning(
                    "gpu runtime qualification failed devices=%s reasons=%s",
                    [worker.device.sysfs_name for worker in attempted_workers],
                    sorted(reason_codes) or [reason],
                )
                # Preserve the failed workers for the bounded teardown below;
                # check_canary replaces pool.workers with its passing subset.
                pool.workers = attempted_workers
                await asyncio.to_thread(pool.shutdown)
                self.pool = None
                raise GpuQualificationFailure(reason)
            self._qualification_failure_reason = None
            self._qualification_retry_at = 0.0
        except BaseException:
            if self.pool is not None:
                await asyncio.to_thread(self.pool.shutdown)
                self.pool = None
            raise

    @staticmethod
    def _qualification_reason(worker: Any) -> str:
        """Map private worker failure text to a safe bounded reason code."""
        raw = str(getattr(getattr(worker, "stats", None), "failed_reason", "") or "").lower()
        if raw.startswith("canary "):
            return "canary_failed"
        return "worker_startup_failed"

    async def ensure_device(self, device_id: str) -> None:
        """Qualify the shared pool and verify this card has a live worker.

        Pool qualification may legitimately return a strict subset of the
        probed cards when another workload occupies one of them.  The
        scheduler must not mark that card ready merely because a shared pool
        exists for a different card.
        """
        await self._ensure()
        if device_id not in self._workers:
            raise GpuContention(f"GPU worker {device_id} did not qualify")

    async def embed(self, device_id: str, texts: list[str]) -> list[list[float]]:
        await self._ensure()
        worker = self._workers.get(device_id)
        if worker is None or worker.proc is None:
            raise SchedulerError(f"GPU worker {device_id} is unavailable")
        ok, reason = await asyncio.to_thread(worker.still_qualifies)
        if not ok:
            worker.stats.yielded_reason = reason
            await asyncio.to_thread(worker.terminate)
            # Forgotten, so `ensure_device` reports this card as needing a real
            # start instead of handing out a worker that is gone (15.0.2).
            self._workers.pop(device_id, None)
            raise SchedulerError(f"GPU worker {device_id} yielded: {reason}")
        try:
            return await asyncio.to_thread(
                worker.embed, texts,
                timeout=float(getattr(self.config, "gpu_worker_slice_timeout_s", 120.0)),
            )
        except Exception:
            await asyncio.to_thread(worker.terminate)
            self._workers.pop(device_id, None)
            raise

    async def stop(self) -> None:
        task = self._ensure_task
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        pool = self.pool
        if pool is not None:
            await asyncio.to_thread(pool.shutdown)
            if self.pool is pool:
                self.pool = None
        self._workers.clear()


def gpu_handlers_from_host(config: Any, probe: Any, cpu_embed: Callable[[list[str]], Any],
                           batch_ceiling_gb: float, *, holder: str = "host",
                           clock: Callable[[], float] = time.monotonic) -> list[DeviceHandler]:
    """Create one lazy handler for each probed physical GPU identity.

    ``clock`` is passed to the shared :class:`GpuHostRuntime`; the default is
    ``time.monotonic`` and only tests override it.
    """
    try:
        devices = list(probe.devices())
    except Exception as exc:  # noqa: BLE001 - probe adapters are third-party seams
        log.warning("index scheduler GPU probe failed: %s", type(exc).__name__)
        return []
    runtime = GpuHostRuntime(config, probe, cpu_embed, batch_ceiling_gb, holder,
                             clock=clock)
    handlers: list[DeviceHandler] = []
    for device in devices:
        device_id = getattr(device, "sysfs_name", None) or getattr(device, "pci_address", None)
        if not device_id:
            continue

        async def embed(texts: list[str], identity: str = str(device_id)) -> Any:
            """Keep the async runtime visible to the scheduler's callback adapter."""
            return await runtime.embed(identity, texts)

        async def start(identity: str = str(device_id)) -> None:
            await runtime.ensure_device(identity)

        handlers.append(DeviceHandler(
            str(device_id),
            embed,
            kind="gpu",
            max_batch=max(1, int(getattr(config, "gpu_slice_chunks", 512))),
            # Cold adapters do not pay worker/model startup for a trivial job.
            # The scheduler promotes them when aggregate estimates, actual
            # ready work, binary evidence, or a CPU-ineligible chunk warrants
            # startup.  Once ready, they may take any compatible work.
            state="cold",
            start=start,
            stop=runtime.stop,
        ))
    return handlers


@dataclass(eq=False)
class _Chunk:
    job: IndexJob
    request_id: str
    offset: int
    text: str
    text_bytes: int
    gpu_failures: int = 0
    force_cpu: bool = False
    queued_at: float = field(default_factory=time.monotonic)


@dataclass
class _Attempt:
    chunk: _Chunk
    device_id: str
    generation: int
    attempt_id: int


class IndexJob:
    """Opaque handle returned by :meth:`IndexScheduler.open_job`."""

    def __init__(self, scheduler: IndexScheduler, project: str, kind: str,
                 estimated_chunks: int | None, binary_formats: int = 0,
                 context: Any = None):
        self.scheduler = scheduler
        self.job_id = uuid.uuid4().hex
        self.project = project
        self.kind = kind
        self.estimated_chunks = estimated_chunks
        self.binary_formats = binary_formats
        self.context = context
        self.state = "open"
        self.queued = 0
        self.in_flight = 0
        self.completed = 0
        self.failed = 0
        self.canceled = 0
        self.attempted = 0
        self.device_completed: dict[str, int] = defaultdict(int)
        # 15.0.2: the caller's `embed.done` summary, captured where the job is
        # opened.  The device workers are long-lived tasks created inside
        # whichever job came first, so their own context carried THAT job for
        # the life of the process: every later walk logged chunks=0 and its
        # batches were counted into a summary that had already printed (Maia
        # NVIDIA proof, 2026-09-30).  `_run_attempt` now records here.
        self.embed_job = current_job()
        self.waiting_reason = ""
        self._chunks: set[_Chunk] = set()
        self._requests: set[asyncio.Future] = set()

    def snapshot(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "project": self.project,
            "kind": self.kind,
            "state": self.state,
            "queued": self.queued,
            "in_flight": self.in_flight,
            "completed": self.completed,
            "failed": self.failed,
            "canceled": self.canceled,
            "attempted": self.attempted,
            "device_completed": dict(self.device_completed),
            "waiting_reason": self.waiting_reason or None,
        }


class IndexScheduler:
    """One fair, bounded scheduler for every project in a host."""

    def __init__(self, cpu_embed: Callable[[list[str]], Any], *,
                 gpu_devices: Iterable[DeviceHandler] = (),
                 settings: SchedulerSettings | Any | None = None,
                 dimensions: int | None = None,
                 clock: Callable[[], float] = time.monotonic):
        self.cpu_embed = cpu_embed
        self.settings = (settings if isinstance(settings, SchedulerSettings)
                         else SchedulerSettings.from_config(settings))
        self.dimensions = dimensions
        self.clock = clock
        self.gpus = list(gpu_devices)
        for gpu in self.gpus:
            gpu.kind = "gpu"
            gpu.max_batch = max(1, gpu.max_batch)
        self.cpu = DeviceHandler("cpu", cpu_embed, kind="cpu", max_batch=1)
        self._lock = asyncio.Lock()
        self._wake = asyncio.Event()
        self._capacity = asyncio.Condition(self._lock)
        self._project_queues: dict[str, deque[_Chunk]] = defaultdict(deque)
        self._projects: deque[str] = deque()
        self._project_jobs: dict[str, deque[str]] = defaultdict(deque)
        self._jobs: dict[str, IndexJob] = {}
        self._workers: dict[str, asyncio.Task] = {}
        self._background_tasks: set[asyncio.Task] = set()
        self._device_wakes: dict[str, asyncio.Event] = {}
        self._active: dict[tuple[str, int], _Attempt] = {}
        self._next_attempt = 0
        self._admitted_chunks = 0
        self._admitted_bytes = 0
        self._shutdown = False
        self._probe_task: asyncio.Task | None = None
        self._reconcile_timer: asyncio.TimerHandle | None = None
        self._reconcile_due = 0.0
        self._idle_timer: asyncio.TimerHandle | None = None
        self._idle_reap_task: asyncio.Task | None = None
        self._idle_reap_pending = False
        self._generation = 0
        # Every physical-card handler shares one host runtime.  A temporary
        # qualification miss is therefore one accelerator state, not one
        # independent startup failure per card.
        self._gpu_contention_active = False
        self._gpu_contention_logged = False
        self._gpu_contention_delay_s = 0.0
        self._gpu_contention_probe_pending = False

    def open_job(self, project: str, kind: str = "index", estimated_chunks: int | None = None,
                 binary_formats: int = 0, context: Any = None) -> IndexJob:
        if self._shutdown:
            raise SchedulerError("index scheduler is shut down")
        job = IndexJob(self, project, kind, estimated_chunks, binary_formats, context)
        self._jobs[job.job_id] = job
        self._ensure_workers()
        return job

    def _ensure_workers(self) -> None:
        """Start only owned asyncio workers; no device call occurs here."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        if "cpu" not in self._workers:
            self._device_wakes["cpu"] = asyncio.Event()
            self._workers["cpu"] = loop.create_task(self._worker_loop(self.cpu), name="cognita-index-cpu")
        for gpu in self.gpus:
            if gpu.device_id not in self._workers:
                self._device_wakes[gpu.device_id] = asyncio.Event()
                self._workers[gpu.device_id] = loop.create_task(self._worker_loop(gpu),
                                                                  name=f"cognita-index-{gpu.device_id}")

    async def embed(self, job: IndexJob, texts: list[str]) -> list[list[float]]:
        """Queue ``texts`` and return vectors in exactly input order."""
        if not texts:
            return []
        if job.scheduler is not self or job.state not in {"open", "producing", "draining"}:
            raise SchedulerError("job is not open")
        self._ensure_workers()
        request_id = uuid.uuid4().hex
        result: list[list[float] | None] = [None] * len(texts)
        futures: list[asyncio.Future] = []
        job.state = "producing"
        try:
            for offset, text in enumerate(texts):
                if not isinstance(text, str):
                    raise TypeError("index chunks must be strings")
                future = asyncio.get_running_loop().create_future()
                # Stamp with the scheduler's own clock: the CPU-fallback
                # deadline compares ``self.clock() - queued_at``, so mixing
                # an injected clock with ``time.monotonic`` made the deadline
                # meaningless.  Production passes no clock, so this is the
                # same ``time.monotonic`` stamp as the dataclass default.
                chunk = _Chunk(job, request_id, offset, text, len(text.encode("utf-8")),
                               queued_at=self.clock())
                _mark_future(future, chunk)
                await self._admit(chunk, future)
                futures.append(future)
            vectors = await asyncio.gather(*futures)
            for i, vector in enumerate(vectors):
                result[i] = vector
            return [v for v in result if v is not None]
        except asyncio.CancelledError:
            await self.cancel_job(job)
            raise
        except Exception:
            await self.cancel_job(job)
            async with self._capacity:
                job.state = "failed"
            raise

    async def _admit(self, chunk: _Chunk, future: asyncio.Future) -> None:
        async with self._capacity:
            while True:
                if self._shutdown or chunk.job.state in {"canceled", "failed"}:
                    raise JobCanceled("index job canceled")
                oversized = chunk.text_bytes > self.settings.max_text_bytes
                fits = (self._admitted_chunks < self.settings.max_chunks and
                        self._admitted_bytes + chunk.text_bytes <= self.settings.max_text_bytes)
                if (fits or (self._admitted_chunks == 0 and oversized)):
                    self._admitted_chunks += 1
                    self._admitted_bytes += chunk.text_bytes
                    chunk.job._chunks.add(chunk)
                    chunk.job._requests.add(future)
                    chunk.job.queued += 1
                    queue = self._project_queues[chunk.job.project]
                    queue.append(chunk)
                    if chunk.job.project not in self._projects:
                        self._projects.append(chunk.job.project)
                    jobs = self._project_jobs[chunk.job.project]
                    if chunk.job.job_id not in jobs:
                        jobs.append(chunk.job.job_id)
                    self._cancel_idle_reap_locked()
                    self._schedule_gpu_starts_locked(chunk)
                    self._wake_all()
                    return
                chunk.job.waiting_reason = "scheduler_capacity"
                await self._capacity.wait()

    async def cancel_job(self, job: IndexJob) -> None:
        """Cancel queued work and invalidate active attempts for ``job``."""
        async with self._capacity:
            if job.state in {"completed", "failed", "canceled"}:
                return
            job.state = "canceled"
            for queue in self._project_queues.values():
                kept = deque()
                for chunk in queue:
                    if chunk.job is job:
                        self._release(chunk)
                        job.queued = max(0, job.queued - 1)
                        job.canceled += 1
                    else:
                        kept.append(chunk)
                queue.clear()
                queue.extend(kept)
            for future in list(job._requests):
                if not future.done():
                    future.set_exception(JobCanceled("index job canceled"))
            self._capacity.notify_all()
            self._wake_all()
            self._schedule_idle_reap_locked()
        log.info("index scheduler canceled job project=%r", job.project)

    async def close_job(self, job: IndexJob, *, outcome: str = "completed") -> None:
        if outcome == "canceled":
            await self.cancel_job(job)
            return
        async with self._capacity:
            if job.state not in {"canceled", "failed"}:
                job.state = "completed" if outcome == "completed" else outcome
            self._jobs.pop(job.job_id, None)
            self._capacity.notify_all()

    def _release(self, chunk: _Chunk) -> None:
        self._admitted_chunks = max(0, self._admitted_chunks - 1)
        self._admitted_bytes = max(0, self._admitted_bytes - chunk.text_bytes)
        chunk.job._chunks.discard(chunk)

    def _schedule_gpu_starts_locked(self, newest: _Chunk) -> None:
        """Start cold GPUs only when shared demand proves startup worthwhile."""
        ready = sum(
            1 for queue in self._project_queues.values()
            for chunk in queue if not chunk.force_cpu
        )
        estimated = sum(
            max(0, int(job.estimated_chunks or 0) - job.completed)
            for job in self._jobs.values()
            if job.state not in {"completed", "failed", "canceled"}
        )
        binary = any(
            job.binary_formats > 0
            for job in self._jobs.values()
            if job.state not in {"completed", "failed", "canceled"}
        )
        warranted = (
            binary
            or ready >= self.settings.gpu_min_chunks
            or estimated >= self.settings.gpu_min_chunks
            or newest.text_bytes > self.settings.cpu_max_chunk_bytes
        )
        if not warranted:
            return
        loop = asyncio.get_running_loop()
        for device in self.gpus:
            if device.available and device.state == "cold" and device._start_task is None:
                task = loop.create_task(
                    self.start_gpu(device.device_id),
                    name=f"cognita-gpu-admission-{device.device_id}",
                )
                self._track_background(task)

    def _track_background(self, task: asyncio.Task) -> None:
        self._background_tasks.add(task)
        task.add_done_callback(self._background_task_done)

    def _background_task_done(self, task: asyncio.Task) -> None:
        self._background_tasks.discard(task)
        if not task.cancelled() and (error := task.exception()) is not None:
            log.error("index scheduler background task failed", exc_info=error)

    def _select_project_chunk(
        self, project: str, predicate: Callable[[_Chunk], bool],
    ) -> _Chunk | None:
        """Select project -> job round-robin -> request FIFO."""
        queue = self._project_queues[project]
        present = {chunk.job.job_id for chunk in queue}
        jobs = self._project_jobs[project]
        if jobs:
            jobs = deque(job_id for job_id in jobs if job_id in present)
            self._project_jobs[project] = jobs
        for chunk in queue:
            if chunk.job.job_id not in jobs:
                jobs.append(chunk.job.job_id)
        for _ in range(len(jobs)):
            job_id = jobs[0]
            jobs.rotate(-1)
            candidate = next(
                (chunk for chunk in queue
                 if chunk.job.job_id == job_id and predicate(chunk)),
                None,
            )
            if candidate is not None:
                return candidate
        return None

    async def _worker_loop(self, device: DeviceHandler) -> None:
        wake = self._device_wakes.setdefault(device.device_id, asyncio.Event())
        while not self._shutdown:
            await wake.wait()
            wake.clear()
            while not self._shutdown:
                async with self._capacity:
                    batch = self._claim(device)
                if not batch:
                    # Do not lose a wake-up between a failed device requeue
                    # and this worker reaching its next wait.  Another worker
                    # may have consumed the Event while this one was between
                    # the lock and ``await``; a pending compatible queue is a
                    # sufficient reason to make one more claim pass.
                    async with self._capacity:
                        pending = any(
                            any((device.kind == "cpu" and
                                 (c.force_cpu or c.text_bytes <= self.settings.cpu_max_chunk_bytes or
                                  not self.gpus or
                                  (self._gpu_contention_active and not self._gpu_available()) or
                                  (not self._gpu_available() and
                                   self.clock() - c.queued_at >= self.settings.gpu_retry_cooldown_s))) or
                                (device.kind == "gpu" and self._device_ready(device) and not c.force_cpu)
                                for c in queue)
                            for queue in self._project_queues.values()
                        )
                    if pending:
                        await asyncio.sleep(0)
                        continue
                    break
                await self._run_attempt(device, batch)

    def _gpu_available(self) -> bool:
        return any(g.available and g.state not in {
                       "cold", "contended", "quarantined", "stopping",
                   }
                   and g._cooldown_until <= self.clock() for g in self.gpus)

    def _device_ready(self, device: DeviceHandler) -> bool:
        """Evaluate readiness against the scheduler's injected clock."""
        return (device.available and device.state == "ready" and
                device._cooldown_until <= self.clock())

    def _cooldown_elapsed_locked(self, device: DeviceHandler) -> None:
        """A GPU's failure cooldown is over: make it usable again, correctly.

        15.0.2: a card with a start callback is backed by a worker process, and
        every failed slice terminates that worker, so marking it "ready" handed
        out a card with nothing behind it.  In a two-card pool the dead card then
        took a batch, failed at once, pushed its chunks toward the CPU and went
        back into cooldown, every `gpu_retry_cooldown_s`, for as long as its
        sibling kept the pool alive (final review of 15.0.2).  It goes back to
        "cold" and is STARTED again: that restarts a dead pool, or parks the card
        as contended, without charging any chunk, while its sibling works on.
        A handler with no start callback (an in-process adapter) is simply ready.
        """
        if device.start is None:
            device.state = "ready"
            device.reason = "cooldown elapsed"
            return
        device.state = "cold"
        device.reason = "cooldown elapsed; restart needed"
        if device._start_task is None and any(self._project_queues.values()):
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                return
            self._track_background(loop.create_task(
                self.start_gpu(device.device_id),
                name=f"cognita-gpu-restart-{device.device_id}",
            ))

    def _claim(self, device: DeviceHandler) -> list[_Chunk]:
        if device.kind == "gpu" and device.state == "cooldown" and device._cooldown_until <= self.clock():
            self._cooldown_elapsed_locked(device)
        if device._busy or not device.available or device.state != "ready":
            return []
        if device.kind == "cpu":
            # CPU may take one small chunk while a GPU is ready.  During the
            # explicit all-GPU fallback window it may take any valid chunk.
            for _ in range(len(self._projects)):
                project = self._projects[0]
                queue = self._project_queues[project]
                def cpu_allowed(c: _Chunk) -> bool:
                    if c.force_cpu or c.text_bytes <= self.settings.cpu_max_chunk_bytes:
                        return True
                    return (not self.gpus or
                            (self._gpu_contention_active and not self._gpu_available()) or
                            (not self._gpu_available() and
                             self.clock() - c.queued_at >= self.settings.gpu_retry_cooldown_s))
                selected = self._select_project_chunk(project, cpu_allowed)
                if selected is not None:
                    queue.remove(selected)
                    self._projects.rotate(-1)
                    selected.job.queued -= 1
                    selected.job.in_flight += 1
                    if (self._gpu_contention_active and
                            not self._gpu_contention_logged):
                        self._gpu_contention_logged = True
                        log.info("index scheduler GPU busy; falling back to CPU")
                    self._next_attempt += 1
                    self._active[(selected.request_id, selected.offset)] = _Attempt(
                        selected, device.device_id, device.generation, self._next_attempt)
                    return [selected]
                self._projects.rotate(-1)
            # A queued oversized chunk can become CPU-eligible only when its
            # fallback cooldown expires. If every GPU is unavailable, no card
            # event may arrive to wake this worker (notably after quarantine
            # at startup). Schedule one coalesced wake at the earliest such
            # deadline instead of leaving the job producing indefinitely.
            if self.gpus and not self._gpu_available():
                now = self.clock()
                remaining = [
                    self.settings.gpu_retry_cooldown_s - (now - chunk.queued_at)
                    for queue in self._project_queues.values() for chunk in queue
                    if not chunk.force_cpu and chunk.text_bytes > self.settings.cpu_max_chunk_bytes
                ]
                pending = [delay for delay in remaining if delay > 0]
                if pending:
                    self._schedule_reconcile(min(pending))
            return []
        if not self._device_ready(device):
            return []
        # Project round-robin, FIFO within a project.  A batch can include
        # different jobs/projects, but each selection advances the project
        # cursor exactly once.
        batch: list[_Chunk] = []
        # Round-robin every chunk selection, including selections that are
        # assembled into one GPU request.  Taking a whole project's FIFO here
        # would let a large document or many jobs in one project monopolize a
        # wide GPU batch and violate the shared fairness guarantee.
        attempts = 0
        max_attempts = max(1, len(self._projects)) * device.max_batch
        while self._projects and len(batch) < device.max_batch and attempts < max_attempts:
            attempts += 1
            project = self._projects[0]
            queue = self._project_queues[project]
            candidate = self._select_project_chunk(project, lambda c: not c.force_cpu)
            self._projects.rotate(-1)
            if candidate is None:
                continue
            queue.remove(candidate)
            batch.append(candidate)
            candidate.job.queued -= 1
            candidate.job.in_flight += 1
        if batch:
            for chunk in batch:
                self._next_attempt += 1
                self._active[(chunk.request_id, chunk.offset)] = _Attempt(
                    chunk, device.device_id, device.generation, self._next_attempt)
            return batch
        return []

    async def _invoke(self, callback: Callable[[list[str]], Any], texts: list[str]) -> Any:
        if inspect.iscoroutinefunction(callback):
            return await callback(texts)
        result = await asyncio.to_thread(callback, texts)
        # Decorators and adapters can conceal an asynchronous callback behind a
        # regular function.  Never pass its coroutine object to vector validation.
        if inspect.isawaitable(result):
            return await result
        return result

    @staticmethod
    def _validate_vectors(vectors: Any, count: int, dimensions: int | None) -> list[list[float]]:
        if not isinstance(vectors, (list, tuple)) or len(vectors) != count:
            raise EmbeddingError(f"device returned {len(vectors) if isinstance(vectors, (list, tuple)) else 'invalid'} vectors for {count} chunks")
        out: list[list[float]] = []
        for vector in vectors:
            if not isinstance(vector, (list, tuple)) or not vector:
                raise EmbeddingError("device returned an empty vector")
            if dimensions is not None and len(vector) != dimensions:
                raise EmbeddingError("device returned an incompatible vector dimension")
            values = [float(x) for x in vector]
            if not all(math.isfinite(x) for x in values):
                raise EmbeddingError("device returned a non-finite vector")
            out.append(values)
        return out

    async def _run_attempt(self, device: DeviceHandler, batch: list[_Chunk]) -> None:
        device._busy = True
        device.state = "busy"
        device.attempted += len(batch)
        for chunk in batch:
            chunk.job.attempted += 1
        try:
            # Keep ownership of a native CPU call even if the worker task is
            # canceled during host shutdown.  Canceling the asyncio wrapper
            # alone does not stop the executor thread.
            async def invoke_owned() -> Any:
                # Outside any job: the callback's own `record_batch` would
                # otherwise credit whatever job this worker task was created
                # in.  The batch is credited to each chunk's job below.
                with outside_the_job():
                    async with device._turn:
                        timing["start"] = self.clock()     # after the turn: waiting is not embedding
                        try:
                            return await self._invoke(device.embed, [c.text for c in batch])
                        finally:
                            timing["end"] = self.clock()

            timing: dict[str, float] = {}

            call = asyncio.create_task(invoke_owned())
            try:
                vectors = await call
            except asyncio.CancelledError:
                await asyncio.shield(call)
                raise
            vectors = self._validate_vectors(vectors, len(batch), self.dimensions)
        except asyncio.CancelledError:
            # A native CPU call is owned by this task and is awaited by the
            # cancellation path before host teardown; do not release ownership
            # or capacity here.
            raise
        except Exception as exc:  # noqa: BLE001 - adapter failures are retriable
            # A failed attempt is TIME SPENT and no work done, the rule
            # `Embedder.embed` keeps by recording in a `finally`.  The device
            # call runs outside any job, so the time is credited here, per job.
            if timing:
                spent = max(0.0, timing.get("end", timing["start"]) - timing["start"])
                shares: dict[int, list[Any]] = {}
                for chunk in batch:
                    if chunk.job.embed_job is not None:
                        shares.setdefault(id(chunk.job.embed_job), [chunk.job.embed_job, 0])[1] += 1
                for embed_job, count in shares.values():
                    embed_job.record(device.device_id, 0, 0, spent * count / len(batch))
            await self._failed_attempt(device, batch, exc)
        else:
            elapsed = max(0.0, timing.get("end", 0.0) - timing.get("start", 0.0))
            credited: dict[int, list[Any]] = {}
            async with self._capacity:
                for chunk, vector in zip(batch, vectors):
                    attempt = self._active.pop((chunk.request_id, chunk.offset), None)
                    if (attempt is None or attempt.generation != device.generation or
                            chunk.job.state == "canceled"):
                        if chunk.job.state == "canceled":
                            self._release(chunk)
                        elif attempt is not None and attempt.generation != device.generation:
                            # A stale worker result cannot be accepted.  Put
                            # the range back under the new generation; no
                            # vector is silently dropped or allowed to resolve
                            # a request twice.
                            chunk.job.in_flight = max(0, chunk.job.in_flight - 1)
                            if device.kind == "gpu":
                                chunk.gpu_failures += 1
                                chunk.force_cpu = chunk.gpu_failures >= 2
                                self._project_queues[chunk.job.project].appendleft(chunk)
                                chunk.job.queued += 1
                                if chunk.job.project not in self._projects:
                                    self._projects.append(chunk.job.project)
                            else:
                                chunk.job.failed += 1
                                self._release(chunk)
                        continue
                    chunk.job.in_flight = max(0, chunk.job.in_flight - 1)
                    chunk.job.completed += 1
                    chunk.job.device_completed[device.device_id] += 1
                    if chunk.job.embed_job is not None:
                        row = credited.setdefault(id(chunk.job.embed_job), [chunk.job.embed_job, 0, 0])
                        row[1] += 1
                        row[2] += len(chunk.text)
                    device.completed += 1
                    self._release(chunk)
                    for future in list(chunk.job._requests):
                        # Futures are one per chunk; the identity is retained
                        # only to avoid putting user data in scheduler state.
                        if not future.done():
                            # Set by offset in the request-local closure below.
                            marker = getattr(future, "_cognita_chunk", None)
                            if marker is chunk:
                                future.set_result(vector)
                                chunk.job._requests.discard(future)
                                break
                self._capacity.notify_all()
            # A batch may mix projects (fairness); each job gets its own chunks
            # and its share of the batch's time.
            for embed_job, chunks, chars in credited.values():
                embed_job.record(device.device_id, chunks, chars, elapsed * chunks / len(batch))
        finally:
            device._busy = False
            if device.state == "busy":
                device.state = "ready"
            async with self._capacity:
                self._schedule_idle_reap_locked()
            self._wake_all()

    def _wake_all(self) -> None:
        self._wake.set()
        for wake in self._device_wakes.values():
            wake.set()

    async def external_turn_started(self) -> None:
        """Suspend idle reaping while another adapter owns a compute turn."""
        async with self._capacity:
            self._cancel_idle_reap_locked()

    async def external_turn_finished(self) -> None:
        """Restart idle accounting after another adapter releases its turn."""
        async with self._capacity:
            self._schedule_idle_reap_locked()

    def _cancel_idle_reap_locked(self) -> None:
        canceled = self._idle_reap_pending
        self._idle_reap_pending = False
        if self._idle_timer is not None:
            self._idle_timer.cancel()
            self._idle_timer = None
            canceled = True
        if canceled:
            log.debug("index scheduler GPU idle reap timer canceled")

    def _schedule_idle_reap_locked(self) -> None:
        if self._shutdown or self.settings.gpu_idle_linger_s < 0:
            return
        if (any(self._project_queues.values())
                or any(gpu._busy or gpu._turn.locked() for gpu in self.gpus)):
            self._cancel_idle_reap_locked()
            return
        if not any(gpu.state == "ready" and gpu.stop is not None for gpu in self.gpus):
            return
        self._cancel_idle_reap_locked()
        loop = asyncio.get_running_loop()
        self._idle_timer = loop.call_later(
            self.settings.gpu_idle_linger_s,
            self._start_idle_reap,
        )
        log.debug(
            "index scheduler GPU idle reap timer armed delay_s=%s devices=%s",
            self.settings.gpu_idle_linger_s,
            ",".join(
                gpu.device_id
                for gpu in self.gpus
                if gpu.state == "ready" and gpu.stop is not None
            ),
        )

    def _start_idle_reap(self) -> None:
        self._idle_timer = None
        if self._idle_reap_task is not None and not self._idle_reap_task.done():
            # A different device may have become idle while the current reap
            # waits for a slow stop callback.  Remember that its full linger
            # has elapsed so completion can immediately re-evaluate it.
            self._idle_reap_pending = True
            return
        task = asyncio.create_task(
            self._reap_idle_gpus(), name="cognita-gpu-idle-reap",
        )
        self._idle_reap_task = task
        task.add_done_callback(self._idle_reap_done)

    def _idle_reap_done(self, task: asyncio.Task) -> None:
        if self._idle_reap_task is task:
            self._idle_reap_task = None
        if not task.cancelled() and (error := task.exception()) is not None:
            log.error("index scheduler idle reap task failed", exc_info=error)
        if self._idle_reap_pending and not self._shutdown:
            self._idle_reap_pending = False
            self._start_idle_reap()

    async def _reap_idle_gpus(self) -> None:
        async with self._capacity:
            self._idle_timer = None
            if (self._shutdown or any(self._project_queues.values())
                    or any(gpu._busy or gpu._turn.locked() for gpu in self.gpus)):
                return
            retiring = [
                gpu for gpu in self.gpus
                if gpu.state == "ready" and gpu.stop is not None
            ]
            for gpu in retiring:
                gpu.state = "stopping"
                gpu.generation += 1
        failed: dict[str, str] = {}
        for gpu in retiring:
            await gpu._turn.acquire()
            try:
                if inspect.iscoroutinefunction(gpu.stop):
                    await gpu.stop()
                else:
                    await asyncio.to_thread(gpu.stop)
            except Exception as exc:  # noqa: BLE001 - runtime adapter boundary
                failed[gpu.device_id] = type(exc).__name__
                log.exception("index scheduler GPU idle reap failed device=%s", gpu.device_id)
            finally:
                gpu._turn.release()
        async with self._capacity:
            reaped = []
            for gpu in retiring:
                if gpu.state == "stopping":
                    if gpu.device_id in failed:
                        # A failed stop does not prove the runtime released its
                        # allocation.  Keep it usable and retry after another
                        # linger instead of reporting a false cold state.
                        gpu.state = "ready"
                        gpu.reason = f"idle reap failed: {failed[gpu.device_id]}"
                    else:
                        gpu.state = "cold"
                        gpu.reason = "idle linger elapsed"
                        reaped.append(gpu.device_id)
            # A different GPU may have become ready while a retiring device's
            # turn was locked.  Its earlier scheduling attempt correctly did
            # nothing; now that every stop released its turn, evaluate again.
            self._schedule_idle_reap_locked()
            self._wake_all()
        if reaped:
            log.info(
                "index scheduler GPU idle reap completed devices=%s",
                ",".join(reaped),
            )

    async def _failed_attempt(self, device: DeviceHandler, batch: list[_Chunk], exc: Exception) -> None:
        async with self._capacity:
            device.failures += 1
            if device.kind == "gpu":
                device.state = "cooldown"
                device.reason = f"dispatch failure: {type(exc).__name__}"
                device._cooldown_until = self.clock() + self.settings.gpu_retry_cooldown_s
                device.generation += 1
            for chunk in batch:
                self._active.pop((chunk.request_id, chunk.offset), None)
                chunk.job.in_flight = max(0, chunk.job.in_flight - 1)
                if chunk.job.state == "canceled":
                    self._release(chunk)
                    continue
                if device.kind == "gpu":
                    chunk.gpu_failures += 1
                    if chunk.gpu_failures >= 2:
                        chunk.force_cpu = True
                    self._project_queues[chunk.job.project].appendleft(chunk)
                    chunk.job.queued += 1
                    if chunk.job.project not in self._projects:
                        self._projects.append(chunk.job.project)
                else:
                    chunk.job.failed += 1
                    self._release(chunk)
                    for future in list(chunk.job._requests):
                        marker = getattr(future, "_cognita_chunk", None)
                        if marker is chunk and not future.done():
                            future.set_exception(EmbeddingError(str(exc)))
                            chunk.job._requests.discard(future)
                            break
            self._capacity.notify_all()
        detail = " ".join(str(exc).splitlines())[:240]
        log.warning(
            "index scheduler device=%s failed batch=%d reason=%s detail=%r",
            device.device_id,
            len(batch),
            type(exc).__name__,
            detail,
        )
        if device.kind == "gpu" and self.settings.gpu_retry_cooldown_s > 0:
            # Wake the worker when this device's restart cooldown expires.  The
            # coalesced handle is owned and removed on shutdown; no orphan may
            # revive a stopped host.
            self._schedule_reconcile(self.settings.gpu_retry_cooldown_s)

    def _schedule_reconcile(self, delay: float) -> None:
        """Coalesce device retry signals and retain ownership through shutdown."""
        loop = asyncio.get_running_loop()
        due = loop.time() + max(0.0, delay)
        if (self._reconcile_timer is not None
                and not self._reconcile_timer.cancelled()
                and self._reconcile_due <= due):
            return
        if self._reconcile_timer is not None:
            self._reconcile_timer.cancel()
        self._reconcile_due = due
        self._reconcile_timer = loop.call_at(due, self._reconcile_wakeup)

    def _reconcile_wakeup(self) -> None:
        self._reconcile_timer = None
        self._reconcile_due = 0.0
        if self._shutdown:
            return
        task = asyncio.create_task(
            self._reconcile_queued_work(), name="cognita-gpu-reconcile",
        )
        self._track_background(task)

    async def _reconcile_queued_work(self) -> None:
        async with self._capacity:
            self.reconcile_devices()
            newest = next(
                (chunk for queue in self._project_queues.values() for chunk in queue),
                None,
            )
            if newest is not None:
                self._schedule_gpu_starts_locked(newest)
            elif self._gpu_contention_active:
                # A completed CPU fallback ends this contention window.  The
                # next request gets one fresh probe, while an idle scheduler
                # does not inherit an ever-growing retry delay or log state.
                self._gpu_contention_active = False
                self._gpu_contention_logged = False
                self._gpu_contention_delay_s = 0.0
            self._wake_all()

    def mark_gpu_ready(self, device_id: str, *, generation: int | None = None) -> None:
        for device in self.gpus:
            if device.device_id == device_id:
                if generation is not None:
                    device.generation = generation
                device.available = True
                device.state = "ready"
                device.reason = ""
                device._cooldown_until = 0.0
                self._wake_all()
                return

    def add_gpu(self, device: DeviceHandler) -> None:
        """Register a physical GPU handler before or during host startup."""
        device.kind = "gpu"
        device.max_batch = max(1, device.max_batch)
        self.gpus.append(device)
        self._ensure_workers()
        self._wake_all()

    async def start_gpu(self, device_id: str) -> bool:
        """Run one GPU's provider/canary startup callback (single-flight)."""
        device = next((g for g in self.gpus if g.device_id == device_id), None)
        if device is None or device.available is False or device.state == "quarantined":
            return False
        if device._start_task is not None:
            return await device._start_task
        if device.start is None:
            async with self._capacity:
                device.state = "ready"
                self._schedule_idle_reap_locked()
            self._wake_all()
            return True

        async def run() -> bool:
            device.state = "starting"
            device.generation += 1
            try:
                if inspect.iscoroutinefunction(device.start):
                    await device.start()
                else:
                    await asyncio.to_thread(device.start)
            except Exception as exc:  # noqa: BLE001 - startup adapter boundary
                # A shared cache prerequisite failure is temporary and follows
                # the same bounded cooldown as the scheduler lane. Other
                # shared qualification failures remain terminal; isolated
                # handler startup failures remain retryable.
                category = f"{type(exc).__name__}: {exc}".lower()
                reason_code = getattr(exc, "reason_code", None)
                device.reason = f"startup failed: {reason_code or type(exc).__name__}"
                cache_failure = (isinstance(exc, GpuQualificationFailure)
                                 and exc.reason_code == "program_cache_unavailable")
                # 15.0.2: a card that has already worked in this process and now
                # fails to start a worker is being RESTARTED, typically after it
                # yielded to another program whose memory use is still moving
                # (ComfyUI, llama.cpp).  That is contention, not a broken runtime:
                # quarantining it would switch the card off for the life of the
                # process, silently, behind the CPU fallback.  Retry it after the
                # cooldown.  A card that has NEVER started stays terminal, and a
                # canary, provider or identity failure is terminal either way.
                restart_failure = (isinstance(exc, GpuQualificationFailure)
                                   and exc.reason_code == "worker_startup_failed"
                                   and device._was_ready)
                if cache_failure or restart_failure:
                    retry_delay = max(0.01, self.settings.gpu_retry_cooldown_s)
                    device.state = "cooldown"
                    device._cooldown_until = self.clock() + retry_delay
                    self._schedule_reconcile(retry_delay)
                elif isinstance(exc, GpuQualificationFailure) or "canary" in category or "provider" in category or "identity" in category:
                    device.available = False
                    device.state = "quarantined"
                elif isinstance(exc, GpuContention):
                    # All physical-card handlers share one host runtime.  A
                    # contention result is therefore a single accelerator
                    # state, not three card startup failures.  Mark this lane
                    # unavailable to CPU admission immediately, while the
                    # coalesced reconcile timer provides a bounded re-probe.
                    device.state = "contended"
                    device._cooldown_until = 0.0
                    self._gpu_contention_active = True
                    if not self._gpu_contention_probe_pending:
                        self._gpu_contention_probe_pending = True
                        if self._gpu_contention_delay_s <= 0:
                            self._gpu_contention_delay_s = max(
                                0.001, self.settings.gpu_probe_interval_s,
                            )
                        else:
                            self._gpu_contention_delay_s = min(
                                max(self.settings.gpu_probe_interval_s,
                                    self.settings.gpu_retry_cooldown_s),
                                max(0.001, self._gpu_contention_delay_s * 2),
                            )
                        self._schedule_reconcile(self._gpu_contention_delay_s)
                elif "no qualifying gpu worker" in category:
                    # Preserve the adapter seam's historical retry behavior.
                    # The host runtime raises ``GpuContention`` above so only
                    # that shared path takes immediate CPU fallback semantics.
                    device.state = "cold"
                    self._schedule_reconcile(self.settings.gpu_probe_interval_s)
                else:
                    device.state = "cooldown"
                    device._cooldown_until = self.clock() + self.settings.gpu_retry_cooldown_s
                    self._schedule_reconcile(self.settings.gpu_retry_cooldown_s)
                # The shared cache prerequisite already logged its one bounded
                # failure above; repeated card callbacks must not echo it.
                if restart_failure:
                    log.warning("index scheduler GPU restart device=%s failed reason=%s; "
                                "retrying after the cooldown", device.device_id, exc.reason_code)
                elif not isinstance(exc, GpuContention) and not cache_failure:
                    log.warning("index scheduler GPU startup device=%s reason=%s", device.device_id, type(exc).__name__)
                self._wake_all()
                return False
            # Startup can outlive the work that justified it.  Serialize the
            # ready transition with admission and external compute turns so a
            # now-unused runtime always receives an idle deadline, while new
            # work can cancel that deadline through the normal admission path.
            async with self._capacity:
                device.state = "ready"
                device.reason = ""
                device._cooldown_until = 0.0
                device._was_ready = True
                self._gpu_contention_active = False
                self._gpu_contention_logged = False
                self._gpu_contention_delay_s = 0.0
                self._gpu_contention_probe_pending = False
                self._schedule_idle_reap_locked()
            self._wake_all()
            log.info("index scheduler GPU ready device=%s generation=%d", device.device_id, device.generation)
            return True

        device._start_task = asyncio.create_task(run(), name=f"cognita-gpu-start-{device_id}")
        try:
            return await device._start_task
        finally:
            device._start_task = None

    def quarantine_gpu(self, device_id: str, reason: str) -> None:
        for device in self.gpus:
            if device.device_id == device_id:
                device.available = False
                device.state = "quarantined"
                device.reason = reason
                device.generation += 1
                self._wake_all()
                return

    def reconcile_devices(self) -> None:
        """Reconcile cooldowns/probe results without blocking the event loop.

        Hosts call this on admission and their existing health/device-change
        signal.  There is intentionally no polling task while the ready queues
        are empty.
        """
        now = self.clock()
        changed = False
        for device in self.gpus:
            if device.state == "contended":
                # A contention retry is deliberately stateful: once the
                # coalesced probe is due, a new request may start it again,
                # while an idle scheduler performs no further probes.
                device.state = "cold"
                device.reason = "contention probe due"
                changed = True
            elif device.state == "cooldown" and device._cooldown_until <= now:
                self._cooldown_elapsed_locked(device)
                changed = True
        if changed and any(device.state == "cold" for device in self.gpus):
            self._gpu_contention_probe_pending = False
        if changed:
            self._wake_all()

    def snapshot(self, project: str | None = None) -> dict[str, Any]:
        # The host/public health snapshot is intentionally aggregate-only.  A
        # caller must name its authorized project to receive job accounting;
        # project identifiers and request state never leave the public seam.
        all_jobs = [j.snapshot() for j in self._jobs.values()]
        jobs = ([job for job in all_jobs if job["project"] == project]
                if project is not None else [])
        accounting = jobs if project is not None else all_jobs
        return {
            "queued": sum(j["queued"] for j in accounting),
            "in_flight": sum(j["in_flight"] for j in accounting),
            "cpu": {"state": "busy" if self.cpu._busy else "idle", "completed": self.cpu.completed},
            "gpus": [{"device": g.device_id, "state": g.state, "reason": g.reason or None,
                       "generation": g.generation, "completed": g.completed,
                       "cooldown": max(0.0, g._cooldown_until - self.clock())}
                      for g in self.gpus],
            "jobs": jobs,
        }

    def card_states(self) -> list[dict[str, Any]]:
        """Each card's state and reason, for Admin's acceleration status.

        Called from the Admin app, not the scheduler's loop, so it reads only
        per-handler attributes (never the job maps `snapshot` walks).
        `has_run` says the card passed its own startup canary in this process
        at least once, which is proof the service can use it.
        """
        return [{"device": g.device_id, "state": g.state, "reason": g.reason or None,
                 "has_run": g._was_ready}
                for g in list(self.gpus)]

    async def shutdown(self) -> None:
        """Stop admission, cancel queued work, and await owned calls."""
        self._shutdown = True
        self._idle_reap_pending = False
        if self._idle_timer is not None:
            self._idle_timer.cancel()
            self._idle_timer = None
        if self._idle_reap_task is not None:
            self._idle_reap_task.cancel()
            await asyncio.gather(self._idle_reap_task, return_exceptions=True)
            self._idle_reap_task = None
        if self._reconcile_timer is not None:
            self._reconcile_timer.cancel()
            self._reconcile_timer = None
            self._reconcile_due = 0.0
        background = list(self._background_tasks)
        for task in background:
            task.cancel()
        if background:
            await asyncio.gather(*background, return_exceptions=True)
        self._background_tasks.clear()
        jobs = list(self._jobs.values())
        for job in jobs:
            await self.cancel_job(job)
        self._wake_all()
        tasks = list(self._workers.values())
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        for device in [self.cpu, *self.gpus]:
            if device.stop is not None:
                try:
                    if inspect.iscoroutinefunction(device.stop):
                        await device.stop()
                    else:
                        await asyncio.to_thread(device.stop)
                except Exception:
                    log.exception("index scheduler device=%s shutdown failed", device.device_id)
            device.state = "stopping"
        self._workers.clear()
        self._device_wakes.clear()


def _mark_future(future: asyncio.Future, chunk: _Chunk) -> asyncio.Future:
    """Attach private identity for result routing without storing text."""
    future._cognita_chunk = chunk
    return future
