"""Collect one-line CPU and GPU embedding measurements per indexing job.

A job spans the files one caller indexes; nested calls join that job. Its
context follows spawned work without absorbing unrelated server requests.
Records cover model initialization, job plan, individual batches, and job
totals. Elapsed sums batch durations, wall measures the whole job, and
embed_wall measures the union of busy intervals. Throughput divides by
embed_wall so concurrent devices are not counted as slower.
"""

from __future__ import annotations

import contextvars
import logging
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Iterator

log = logging.getLogger("cognita.embed")

CPU_DEVICE = "cpu"

# The job in scope for the current task. asyncio.to_thread runs its target
# through contextvars.copy_context(), so a job opened in the walk's task is
# visible to the embedder running in the thread pool — and is NOT visible to a
# concurrent search, which runs in a different task carrying its own context.
# That is exactly the separation §3 asks for: a query issued during a rebuild is
# never attributed to it.
_current_job: contextvars.ContextVar["EmbedJob | None"] = contextvars.ContextVar(
    "cognita_embed_job", default=None
)


def _fmt(value: Any) -> str:
    """Render one field value: floats short, strings quoted only when needed."""
    if isinstance(value, float):
        return f"{value:.3f}"
    text = str(value)
    if text == "" or any(c.isspace() for c in text):
        return f'"{text}"'
    return text


def _line(record: str, fields: dict[str, Any]) -> str:
    parts = [f"{k}={_fmt(v)}" for k, v in fields.items() if v is not None]
    return f"{record}  " + " ".join(parts)


def _rate(chunks: int, elapsed: float) -> float:
    return round(chunks / elapsed, 1) if elapsed > 0 else 0.0


@dataclass
class DeviceTotals:
    """Per-device accumulator. One row per device on the ``embed.done`` line.

    The CPU path produces exactly one of these; the GPU path will produce one
    per worker, in the same shape, which is what makes the two comparable.
    """

    device: str
    batches: int = 0
    chunks: int = 0
    chars: int = 0
    elapsed: float = 0.0

    def row(self) -> str:
        return _line(
            "",
            {
                "device": self.device,
                "batches": self.batches,
                "chunks": self.chunks,
                "elapsed": self.elapsed,
                "rate": f"{_rate(self.chunks, self.elapsed)}/s",
            },
        ).strip()


@dataclass
class EmbedJob:
    """One caller's indexing work, from the decision to the totals.

    Mutated from the thread pool (``Embedder.embed`` runs under
    ``asyncio.to_thread``) and read on the event loop, so every accumulator
    touch takes the lock. The counters are small and uncontended; correctness
    here is worth more than the nanoseconds.
    """

    project: str
    walk: str
    decision: str = CPU_DEVICE
    started: float = field(default_factory=time.monotonic)
    devices: dict[str, DeviceTotals] = field(default_factory=dict)
    # 🔴 REENTRANT, and 6.0.11 is why. `done()` builds its fields inside the
    # lock, and one of them now comes from `embed_wall`, which locks to snapshot
    # `_spans` — a plain Lock deadlocked the walk's own summary, i.e. every
    # index would have hung at the finish line, forever, holding the project
    # write lock. The other accumulator properties (`chunks`, `batches`,
    # `embed_elapsed`) read WITHOUT locking, so this was the first re-entrant
    # read in the class and nothing existing had reason to catch it.
    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False)
    # Set by the estimator when it lands (§4.3). Absent until then rather than
    # guessed: a fabricated est_chunks would grade itself perfectly for ever.
    est_chunks: int | None = None
    threshold: int | None = None
    binary_formats: int | None = None
    # 6.2: "warm" when this job rode a pool a previous job left running, "cold"
    # when it paid for its own start-up. Empty while no GPU is involved.
    #
    # Without this the two are indistinguishable in the log, and they are the
    # difference between a 4-second job and a 37-second one — so the field that
    # says whether the warm pool is EARNING anything would otherwise have to be
    # reconstructed by correlating timestamps across jobs.
    pool: str = ""
    _done: bool = False
    # How many callers have JOINED this job on top of the one that opened it.
    # `done()` from a joiner is ignored: see the comment there.
    _joined: int = 0
    # 🔴 6.0.11: every batch's (start, end) on the monotonic clock, so `rate`
    # can divide by the time SOMETHING WAS EMBEDDING rather than by a sum that
    # double-counts concurrency. See `embed_wall`.
    _spans: list[tuple[float, float]] = field(default_factory=list, repr=False)
    # The clock `record()` stamps spans with. A seam, not configuration: the
    # union math is only testable if a test can say when each batch ran, and
    # patching `time.monotonic` globally to do that reaches into pytest's own
    # internals. Nothing outside tests ever sets it.
    _clock: Any = field(default=time.monotonic, repr=False)

    @property
    def chunks(self) -> int:
        return sum(d.chunks for d in self.devices.values())

    @property
    def batches(self) -> int:
        return sum(d.batches for d in self.devices.values())

    @property
    def embed_elapsed(self) -> float:
        return float(sum(d.elapsed for d in self.devices.values()))

    @property
    def embed_wall(self) -> float:
        """🔴 6.0.11: wall-clock time during which SOMETHING was embedding.

        The union of every batch's busy interval, merged. This is the honest
        denominator for the job's `rate`, and `embed_elapsed` is not:

        - **On one device they are the same**, because the intervals are
          sequential and disjoint, so their union IS their sum. The CPU path's
          reported rate does not move by a digit.
        - **On N devices working concurrently, the sum is up to N times the
          union**, so `chunks / embed_elapsed` understates real throughput by
          roughly the device count. Measured on kei's first two-card rebuild:
          12,589 chunks over card1 430.3s + card2 413.1s reported **14.9/s**
          for a walk that genuinely did **~29/s**.

        ⚠️ **A UNION, NOT A SPAN, AND THE DIFFERENCE IS THE WHOLE POINT.**
        `last_end - first_start` would sweep in the gaps between windows where
        the walk is parsing and writing to Postgres — which is most of a CPU
        walk's clock. Using it would make the CPU path report a rate far below
        what it actually embeds at, and §14.4 nominates `rate` as the number
        that answers "is the GPU worth it" BY COMPARING THE TWO PATHS. Breaking
        the baseline to fix the accelerator would be a worse bug than this one.
        `wall` remains on the line for anyone wanting the gaps included.
        """
        with self._lock:
            spans = sorted(self._spans)
        total = 0.0
        current_start: float | None = None
        current_end = 0.0
        for start, end in spans:
            if current_start is None:
                current_start, current_end = start, end
            elif start <= current_end:          # overlaps — extend
                current_end = max(current_end, end)
            else:                               # a genuine idle gap
                total += current_end - current_start
                current_start, current_end = start, end
        if current_start is not None:
            total += current_end - current_start
        return total

    def record(self, device: str, chunks: int, chars: int, elapsed: float) -> None:
        # Outside the lock: `monotonic()` is cheap and this is the batch's own
        # end, not shared state. `elapsed` is what the caller measured around
        # the embed, so end-minus-elapsed reconstructs its start.
        ended = self._clock()
        with self._lock:
            totals = self.devices.get(device)
            if totals is None:
                totals = self.devices[device] = DeviceTotals(device)
            totals.batches += 1
            totals.chunks += chunks
            totals.chars += chars
            totals.elapsed += elapsed
            self._spans.append((ended - elapsed, ended))

    def plan(self, **extra: Any) -> None:
        log.info(
            _line(
                "embed.plan",
                {
                    "project": self.project,
                    "walk": self.walk,
                    **extra,
                    "est_chunks": self.est_chunks,
                    "threshold": self.threshold,
                    "binary_formats": self.binary_formats,
                    "decision": self.decision,
                    "pool": self.pool or None,
                },
            )
        )

    def done(self, **extra: Any) -> None:
        """Emit the summary. Idempotent — the FIRST call wins.

        Called twice on purpose: once by the walk on its way out with the real
        counts, and once from ``index_project``'s ``finally`` so a walk that
        raised still reports what it embedded before it died. Whichever runs
        first has the better information; the second is the safety net.

        🔴 **A JOINED caller's ``done()`` is IGNORED.** ``embed_job`` nests by
        joining, and ``index_file`` closes its job unconditionally — so the
        moment a bulk caller wraps a loop over ``index_file`` (which §4.4
        requires, and which ``copy_directory`` now does), the FIRST file's
        ``done(files=1, indexed=1)`` would latch ``_done`` and permanently close
        the enclosing job. The remaining files' chunks would then accumulate
        into a job whose summary had already printed, and a 500-document copy
        would be logged as having cost six chunks. First-call-wins is right
        WITHIN a job and catastrophic ACROSS a nesting boundary; the depth is
        what tells the two apart.
        """
        if self._joined or self._done:
            return
        self._done = True
        wall = time.monotonic() - self.started
        with self._lock:
            rows = [d.row() for d in self.devices.values()]
            # 🔴 15.0.1: `decision` says what HAPPENED, so a card that embedded
            # anything makes this a GPU job. The walk sets "gpu" only when it
            # starts or claims a pool itself; since the shared scheduler took
            # over the cards (`RetrievalCore.scheduler`), nothing on that path
            # set it, and Maia's first NVIDIA index logged `decision=cpu` over a
            # gpu0 row of 3,589 chunks beside a cpu row of 96. Only ever
            # upgraded here: the walk's own correction to "cpu" for a pool that
            # embedded nothing stays where it is.
            if self.decision == CPU_DEVICE and any(
                name != CPU_DEVICE and totals.chunks > 0
                for name, totals in self.devices.items()
            ):
                self.decision = "gpu"
            fields = {
                "project": self.project,
                "walk": self.walk,
                "decision": self.decision,
                # 6.2. `None` rather than "" so `_line` drops it entirely on a
                # CPU job: a `pool=` field on every CPU line would be noise in
                # the one record §14.3 asks to stay greppable.
                "pool": self.pool or None,
                "chunks": self.chunks,
                "batches": self.batches,
                "elapsed": self.embed_elapsed,
                # 🔴 6.0.11: divided by `embed_wall`, NOT by `elapsed`. On one
                # device they are identical; on two concurrent cards `elapsed`
                # is the sum of both, so the old rate halved every multi-card
                # walk. `elapsed` stays on the line as device-seconds, which is
                # the right number for "what did this cost in hardware time".
                "rate": f"{_rate(self.chunks, self.embed_wall)}/s",
                "embed_wall": self.embed_wall,
                "wall": wall,
            }
            if self.est_chunks is not None:
                fields["est_chunks"] = self.est_chunks
                # 🔴 §4.2 EXCLUDES BINARY FORMATS FROM `est_chunks` BY DESIGN —
                # a PDF's size says nothing about its text, so the estimator
                # counts it as a reason to use the GPU and contributes zero
                # chunks for it. Those chunks DO appear in the actual, so
                # `est_error` on any corpus containing one is a large positive
                # number for ever, regardless of whether the §4.1 formula is any
                # good. §14.4 nominates est_error as "the estimator grading
                # itself... the only place a drift would show up", so publishing
                # a permanently-wrong value there is worse than publishing none:
                # it reads as a signal and cannot move.
                if self.binary_formats:
                    # Two short unquoted tokens rather than a sentence: the
                    # formatter quotes anything containing spaces, and a quoted
                    # phrase in the middle of a line built for grepping is worse
                    # than no field at all.
                    fields["est_error"] = "n/a"
                    fields["est_error_reason"] = "binary_formats"
                elif self.est_chunks:
                    err = (self.chunks - self.est_chunks) / self.est_chunks * 100
                    fields["est_error"] = f"{err:+.1f}%"
            fields.update(extra)
        # Device rows are separated by " | " and reuse the head's field names on
        # purpose (§14.3: one grep gives both paths in the same shape). The
        # separator is what keeps that from being ambiguous — without it a
        # reader cannot tell the job's `chunks` from the last device's, and the
        # design's own example dodges the problem by wrapping onto extra lines,
        # which costs the grep.
        log.info(" | ".join([_line("embed.done", fields), *rows]))


@contextmanager
def embed_job(project: str, walk: str) -> Iterator[EmbedJob]:
    """Open (or join) the job in scope for this caller.

    🔴 Nesting JOINS rather than opening a second job. A bulk caller that loops
    over a single-document indexing path — ``copy_directory`` is the live
    example — must appear as one job, or the log describes 500 tiny jobs while
    the thing anybody wants to know is what the copy cost. The same nesting is
    what will later hold one GPU lease across the whole loop instead of
    reconsidering it per file (§4.4).

    ``plan()`` and ``done()`` are NOT emitted here. The caller decides when it
    knows enough to say what it decided — a walk cannot state its file count
    until it has walked — and the job has to exist before that point so the
    enclosing ``finally`` has something to close.
    """
    existing = _current_job.get()
    if existing is not None:
        # Track the nesting so `done()` can tell the OPENER from a joiner.
        existing._joined += 1
        try:
            yield existing
        finally:
            existing._joined -= 1
        return
    job = EmbedJob(project=project, walk=walk)
    token = _current_job.set(job)
    try:
        yield job
    finally:
        _current_job.reset(token)


def current_job() -> EmbedJob | None:
    """The job in scope for this task, or None outside one.

    6.2 leans on this: `_index_parsed` runs several frames below the
    `embed_job` that `index_file` opened, so stamping `pool=warm` on the right
    job would otherwise mean threading a telemetry argument through the
    signatures of the indexing path itself.
    """
    return _current_job.get()


@contextmanager
def outside_the_job() -> Iterator[None]:
    """Run work that is NOT the caller's corpus, so it stays out of the totals.

    The §9.1 canary is the case this exists for. It embeds one fixed string per
    device plus one CPU reference, and none of that is the user's data — but it
    ran inside the open job, so a pure-GPU walk logged `device=cpu chunks=1`,
    the canary's chunks landed in `est_error`, and, far worse, the canary is the
    FIRST inference on a device and therefore carries the whole 25-38s MIGraphX
    shape compile. That compile sat inside the device's `elapsed`, which made
    `rate` — the field §14.4 nominates as "is the GPU still worth it" —
    understate real throughput by roughly 3x on a short walk.
    """
    token = _current_job.set(None)
    try:
        yield
    finally:
        _current_job.reset(token)


def record_batch(device: str, chunks: int, chars: int, elapsed: float) -> None:
    """One ``embed()`` call. DEBUG, because a walk is hundreds of these.

    Emitted whether or not a job is open: an embed with no job is a query (or a
    caller that has not been job-scoped yet), and "how long does a query embed
    take" is worth having in the same grep as everything else.
    """
    log.debug(
        _line(
            "embed.batch",
            {
                "device": device,
                "chunks": chunks,
                "chars": chars,
                "elapsed": elapsed,
                "rate": f"{_rate(chunks, elapsed)}/s",
            },
        )
    )
    job = _current_job.get()
    if job is not None:
        job.record(device, chunks, chars, elapsed)


def record_cpu_init(**fields: Any) -> None:
    """The CPU embedder finished loading. Once per process.

    ``provider_active`` is read back from the live session by the caller, never
    assumed from what was requested (§14.2) — a runtime that loads and then
    silently serves a different provider is invisible in every other signal.
    ``model_file`` is logged for the same reason: fastembed selects an ONNX
    artifact by model name and ships quantized variants for some models, so
    which file is actually on disk is a fact worth having rather than an
    assumption to re-derive later (§9.1).
    """
    log.info(_line("embed.cpu.init", fields))
