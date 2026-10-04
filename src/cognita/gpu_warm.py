"""Keep an idle GPU pool briefly available between indexing jobs.

A claim removes the pool from the shared slot, giving one caller exclusive
ownership. Parking arms a timer under the same lock; shutdown also closes
the pool at process exit. The timer performs blocking worker teardown off
the event loop, so claim callers wait through any concurrent reap.
"""

from __future__ import annotations

import atexit
import logging
import threading
import time
from typing import Any, Callable

log = logging.getLogger("cognita.gpu")


def _is_live(worker: Any) -> bool:
    """True if this worker's subprocess is still running.

    `proc is None` means terminated. `poll()` is what catches the case the whole
    warm path is exposed to and the per-job path was not: a worker that died
    QUIETLY while nobody was embedding — an OOM kill, a driver fault, a ROCm
    reset. Its process object is still there, and only its exit status says so.
    """
    proc = getattr(worker, "proc", None)
    if proc is None:
        return False
    poll = getattr(proc, "poll", None)
    if poll is None:  # a test double standing in for a live process
        return True
    return poll() is None


def _devices(pool: Any) -> list[str]:
    return [getattr(w.stats, "device", "?") for w in getattr(pool, "workers", [])]


def total_chunks(pool: Any) -> int:
    """Chunks every worker in this pool has embedded over its WHOLE life.

    🔴 Pool-lifetime, not job-lifetime, and the difference is new in 6.2. A
    warm pool spans jobs, so `any(w.stats.chunks)` — which is how the walk
    corrected a `decision=gpu` that had embedded nothing — becomes true for
    every later job on the strength of an earlier one's work. Callers take this
    before and after and compare the DELTA.
    """
    return sum(int(getattr(w.stats, "chunks", 0) or 0) for w in getattr(pool, "workers", []))


class WarmPool:
    """The single warm slot. One process, one lease, one parked pool."""

    def __init__(
        self,
        clock: Callable[[], float] = time.monotonic,
        timer_factory: Callable[[float, Callable[[], None]], Any] = threading.Timer,
    ) -> None:
        self._lock = threading.RLock()
        self._clock = clock
        # Test seam only: production always arms a real `threading.Timer`. A
        # test passes a factory whose timer it fires by hand, so reaper tests
        # never depend on a real interval elapsing (or not elapsing).
        self._timer_factory = timer_factory
        self._pool: Any | None = None
        self._parked_at: float = 0.0
        self._deadline: float = 0.0
        self._linger_s: float = 0.0
        self._timer: threading.Timer | None = None
        # Cheap counters, for the log line and for /healthz. Not accuracy-
        # critical, so they are read outside the lock.
        self.parks = 0
        self.claims = 0
        self.reaps = 0

    # -- the two calls a job makes ------------------------------------------

    def claim(self) -> Any | None:
        """Take the warm pool for a job, or None if there is nothing warm.

        Blocks while a reap is in progress — see the module docstring. Never
        raises: every failure here is "no warm pool", which is an ordinary
        outcome that costs a start-up, never an error.
        """
        with self._lock:
            pool = self._pool
            if pool is None:
                return None
            self._cancel_timer()
            live = [w for w in pool.workers if _is_live(w)]
            if not live:
                # 🔴 A pool whose workers died while parked must never be handed
                # out: `embed_with_pool` would find no worker, fall back to the
                # CPU for the whole window, and report a GPU job that embedded
                # nothing — while the lease stayed held by a corpse, disabling
                # the GPU for every later job in the process.
                self._pool = None
                log.warning(
                    "embed.gpu.warm  event=discard reason=workers_died "
                    "devices=%s idle=%.1f", _devices(pool), self._idle(),
                )
                self._shutdown(pool)
                return None
            idle = self._idle()
            self._pool = None
            self.claims += 1
            log.info(
                "embed.gpu.warm  event=claim devices=%s idle=%.1f holder=%s",
                [getattr(w.stats, "device", "?") for w in live], idle,
                getattr(pool, "holder", ""),
            )
            return pool

    def park(self, pool: Any, linger_s: float) -> bool:
        """Hand a finished pool back. True if it is now warm, False if it was
        torn down instead — in which case the caller owes it nothing more.

        Returns False, having shut the pool down, for every reason a pool must
        not linger: lingering is switched off, it has no live workers left, or
        something is already parked (which the lease should make impossible, and
        which must therefore never be resolved by leaking one of them).
        """
        if pool is None:
            return False
        with self._lock:
            if linger_s <= 0:
                self._shutdown(pool)
                return False
            if not any(_is_live(w) for w in pool.workers):
                log.info(
                    "embed.gpu.warm  event=no_park reason=no_live_workers devices=%s",
                    _devices(pool),
                )
                self._shutdown(pool)
                return False
            if self._pool is not None and self._pool is not pool:
                # Belt and braces behind the lease. Two live pools is the one
                # state that costs real VRAM, so the older one goes NOW rather
                # than waiting for a timer it is about to lose.
                log.warning(
                    "embed.gpu.warm  event=evict reason=second_pool devices=%s",
                    _devices(self._pool),
                )
                stale, self._pool = self._pool, None
                self._cancel_timer()
                self._shutdown(stale)
            self._pool = pool
            self._linger_s = linger_s
            self._parked_at = self._clock()
            self._deadline = self._parked_at + linger_s
            self.parks += 1
            self._arm(linger_s)
            log.info(
                "embed.gpu.warm  event=park devices=%s linger=%.1f chunks=%d holder=%s",
                _devices(pool), linger_s, total_chunks(pool),
                getattr(pool, "holder", ""),
            )
            return True

    # -- the reaper ---------------------------------------------------------

    def reap_if_idle(self, now: float | None = None) -> bool:
        """Tear the parked pool down if its idle period has expired.

        Re-checks the deadline rather than trusting the timer that woke it: a
        claim-then-park cycle inside the linger window moves the deadline, and
        the old timer may already have been scheduled. Returns True if a pool
        was actually reaped.
        """
        with self._lock:
            pool = self._pool
            if pool is None:
                return False
            when = self._clock() if now is None else now
            if when < self._deadline:
                # Re-armed while this timer was in flight; wait for the new one.
                return False
            self._pool = None
            self._cancel_timer()
            self.reaps += 1
            log.info(
                "embed.gpu.warm  event=reap devices=%s idle=%.1f chunks=%d",
                _devices(pool), when - self._parked_at, total_chunks(pool),
            )
            self._shutdown(pool)
            return True

    def shutdown_now(self, reason: str = "shutdown") -> bool:
        """Tear down whatever is parked, immediately. Idempotent.

        Wired into the service's own shutdown AND into `atexit`, because the
        reaper is a daemon timer: an interpreter that exits inside the linger
        window would otherwise leave worker subprocesses to be reparented and
        their VRAM to be reclaimed by the kernel at some later point of the OS's
        choosing.
        """
        with self._lock:
            pool, self._pool = self._pool, None
            self._cancel_timer()
            if pool is None:
                return False
            log.info(
                "embed.gpu.warm  event=shutdown reason=%s devices=%s idle=%.1f",
                reason, _devices(pool), self._idle(),
            )
            self._shutdown(pool)
            return True

    # -- introspection (/healthz, §5) ---------------------------------------

    def status(self) -> dict:
        """A snapshot for /healthz. NEVER blocks — liveness must not wait on a
        multi-second teardown holding the lock, so a failed try-lock reports the
        state it can see rather than queueing behind it."""
        if not self._lock.acquire(blocking=False):
            return {"warm": "reaping"}
        try:
            if self._pool is None:
                return {"warm": False}
            now = self._clock()
            return {
                "warm": True,
                "devices": _devices(self._pool),
                "idle_s": round(now - self._parked_at, 1),
                "expires_in_s": round(max(0.0, self._deadline - now), 1),
                "linger_s": self._linger_s,
            }
        finally:
            self._lock.release()

    @property
    def parked(self) -> Any | None:
        """The parked pool, for tests and for the shutdown wiring. Not a claim."""
        return self._pool

    # -- internals ----------------------------------------------------------

    def _idle(self) -> float:
        return max(0.0, self._clock() - self._parked_at)

    def _arm(self, linger_s: float) -> None:
        timer = self._timer_factory(linger_s, self._on_timer)
        timer.daemon = True
        timer.name = "cognita-gpu-warm-reaper"
        self._timer = timer
        timer.start()

    def _cancel_timer(self) -> None:
        timer, self._timer = self._timer, None
        if timer is not None:
            timer.cancel()

    def _on_timer(self) -> None:
        try:
            self.reap_if_idle()
        except Exception:  # pragma: no cover - a timer thread must not die loudly
            log.exception("embed.gpu.warm  event=reap_failed")

    @staticmethod
    def _shutdown(pool: Any) -> None:
        """`GpuPool.shutdown` is idempotent and releases the lease. It must not
        raise here: this runs on a timer thread and inside `atexit`, where an
        exception is either invisible or a shutdown-time traceback."""
        try:
            pool.shutdown()
        except Exception:  # pragma: no cover - teardown never propagates
            log.exception("embed.gpu.warm  event=shutdown_failed")


WARM = WarmPool()

# 15.0: import the probe BEFORE registering, so its NVML shutdown hook (registered
# when gpu_probe is imported) is older than this one and `atexit`, which runs
# newest first, reaps a parked pool while NVML can still read the cards. See
# `gpu_probe._shutdown_shared_session_at_exit`.
from . import gpu_probe  # noqa: E402,F401

atexit.register(WARM.shutdown_now, "interpreter_exit")
