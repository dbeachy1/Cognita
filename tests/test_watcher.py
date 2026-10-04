"""Tests for the Cognita-side file watcher (4.0-M4, D4.9).

Real watchdog observers on tmp dirs, fake core — no PostgreSQL, no models.
Debounce is shortened so tests run in ~1s each; assertions poll rather than
assume exact timing (watchdog event delivery latency varies by platform).

Superseded (2026-09-22): the paragraph above described the old shape, which
bet on wall-clock time three ways — a real sleep for the native backend to arm
before the first write, real sleeps for the debounce window to elapse, and
real "long enough for a spurious flush" sleeps. None of those has a signal the
code under test sends, so they are gone:

- The watchdog observers are replaced by `FakeObserver` (patched over the
  module's `Observer`/`PollingObserver` names). The REAL `_ProjectEventHandler`
  is still what each event goes through, so filtering, exclusion and marking
  are exercised exactly as before; what is no longer exercised is the OS
  delivering the event, which has no "armed" signal to wait on.
- The watcher module's `time` is replaced by `FakeClock`, so the debounce
  window elapses only when a test advances it.
- The flush loop still runs on its own real interval; tests wait on
  `FakeClock.evaluations_after`, which resolves when the loop has actually
  read the clock and evaluated its dirty set, and on `FakeCore.synced`, which
  `index_project` sets. The timeouts on those waits are hang guards only: in a
  correct run the flush loop always evaluates again and a due batch always
  syncs, whatever the machine load.
"""

import asyncio
import sys
import time

import pytest
from watchdog.events import FileCreatedEvent, FileDeletedEvent, FileModifiedEvent

from cognita import watcher as watcher_module
from cognita.parsing import DEFAULT_POLICY
from cognita.registry import Project
from cognita.watcher import _ProjectEventHandler, WatcherManager

DEBOUNCE = 0.3
WAIT = 5.0  # hang guard only; every wait below is on a signal a correct run always sends


class FakeClock:
    """Stands in for the `time` module inside `cognita.watcher` only.

    `monotonic()` is the debounce clock; it moves only on `advance()`. A read
    made by `_flush_loop` counts as one evaluation of the dirty set: the loop
    reads the clock and then decides what is due without yielding, so by the
    time a waiter resumes, that evaluation (and the creation of any batch
    task) has finished.
    """

    def __init__(self):
        # Starts at zero so `t0 + DEBOUNCE - t0` is exactly DEBOUNCE in floating
        # point (1000.3 - 1000.0 is not 0.3).
        self.now = 0.0
        self.evaluations = 0
        self._waiters: list[tuple[int, asyncio.Future]] = []

    def monotonic(self) -> float:
        if sys._getframe(1).f_code.co_name == "_flush_loop":
            self.evaluations += 1
            for target, future in list(self._waiters):
                if self.evaluations >= target:
                    self._waiters.remove((target, future))
                    if not future.done():
                        future.set_result(None)
        return self.now

    @staticmethod
    def time() -> float:
        return time.time()  # wall time is only a health timestamp in the watcher

    def advance(self, seconds: float) -> None:
        self.now += seconds

    async def evaluations_after(self, count: int = 1) -> None:
        """Wait until the flush loop has evaluated `count` more times.

        Two evaluations after a clock move also guarantee that a batch task
        created by the first one has run: the task's first step is queued
        ahead of the loop's next sleep wake-up, and FakeCore never suspends.
        """
        future = asyncio.get_running_loop().create_future()
        self._waiters.append((self.evaluations + count, future))
        await asyncio.wait_for(future, timeout=WAIT)


class FakeObserver:
    """Stands in for a watchdog observer: records schedules, delivers on demand."""

    def __init__(self, *args, **kwargs):
        self.daemon = False
        self._alive = False
        self.handlers: dict[object, tuple[object, str]] = {}

    def schedule(self, handler, path, recursive=False):
        watch = object()
        self.handlers[watch] = (handler, str(path))
        return watch

    def unschedule(self, watch):
        self.handlers.pop(watch, None)

    def start(self):
        self._alive = True

    def stop(self):
        self._alive = False

    def join(self, timeout=None):
        return None

    def is_alive(self):
        return self._alive

    def deliver(self, event) -> None:
        """What the observer thread does: hand the event to every watch it is under."""
        for handler, root in list(self.handlers.values()):
            if str(event.src_path).startswith(root):
                handler.dispatch(event)


class FakeNativeObserver(FakeObserver):
    pass


class FakePollingObserver(FakeObserver):
    pass


class FakeCore:
    """Records index_project calls; the watcher needs nothing else."""

    def __init__(self, policy=None, clock=None):
        self.exclude_patterns = ["backups"]
        self.sync_conflict_patterns = None  # 5.0: None means the built-in globs
        self.calls: list[tuple[str, float]] = []
        self._policy = policy or DEFAULT_POLICY
        self._clock = clock or time
        # Set by every index_project call: the signal wait_for_sync waits on.
        self.synced = asyncio.Event()

    def policy_for(self, name):
        return self._policy

    async def index_project(self, name, docs_dir, *, force=False, progress=None):
        self.calls.append((name, self._clock.monotonic()))
        self.synced.set()
        return {"total_files": 1, "indexed": 1, "skipped": 0, "removed": 0, "errors": []}


@pytest.fixture
async def env(tmp_path, monkeypatch):
    docs = tmp_path / "docs"
    (docs / "backups").mkdir(parents=True)
    (docs / "a.md").write_text("seed", encoding="utf-8")
    project = Project(name="W", documents_dir=docs, data_dir=tmp_path / "data")
    clock = FakeClock()
    monkeypatch.setattr(watcher_module, "time", clock)
    monkeypatch.setattr(watcher_module, "Observer", FakeNativeObserver)
    monkeypatch.setattr(watcher_module, "PollingObserver", FakePollingObserver)
    core = FakeCore(clock=clock)
    manager = WatcherManager(core, debounce_s=DEBOUNCE)
    await manager.start([project])
    # Native backends may report the files already present when their recursive
    # watch is attached.  Drain that startup batch before yielding the fixture;
    # each test then observes only events caused by its own mutation.
    # Superseded: the observers are FakeObservers now, which report nothing on
    # attach, so there is no startup batch and the real 0.8s drain sleep that
    # used to sit here is gone.
    native = manager._observer
    assert isinstance(native, FakeNativeObserver)

    def emit(event_class, path):
        """Deliver one filesystem event for `path` exactly as the native observer would."""
        native.deliver(event_class(str(path.resolve())))

    yield manager, core, docs, clock, emit
    await asyncio.wait_for(manager.stop(), timeout=WAIT)


async def wait_for_sync(core: FakeCore, count: int = 1, timeout: float = WAIT) -> bool:
    """Wait for index_project to have been called `count` times.

    Waits on `FakeCore.synced`, which index_project sets — no polling. Every
    caller advances the fake clock past the debounce window first, so the sync
    is guaranteed in a correct run and the timeout is only a hang guard.
    """

    async def until_count() -> None:
        while len(core.calls) < count:
            # Clear-then-wait is race-free: index_project runs on this same
            # event loop and cannot interleave between these two lines.
            core.synced.clear()
            await core.synced.wait()

    try:
        await asyncio.wait_for(until_count(), timeout=timeout)
    except TimeoutError:
        return False
    return True


async def test_edit_triggers_one_sync_after_debounce(env):
    manager, core, docs, clock, emit = env
    t0 = clock.now
    (docs / "a.md").write_text("edited on disk", encoding="utf-8")
    emit(FileModifiedEvent, docs / "a.md")
    # No fake time has passed: the flush loop has looked at the fresh mark and
    # must have left it alone (this replaces relying on a real debounce delay).
    await clock.evaluations_after(1)
    assert core.calls == []
    clock.advance(DEBOUNCE)
    assert await wait_for_sync(core)
    assert core.calls[0][0] == "W"
    assert core.calls[0][1] - t0 >= DEBOUNCE  # not before the quiet window elapsed


async def test_burst_coalesces_into_one_sync(env):
    manager, core, docs, clock, emit = env
    for i in range(8):  # an editor's save dance / a sync batch
        (docs / f"note{i}.md").write_text(f"v{i}", encoding="utf-8")
        emit(FileCreatedEvent, docs / f"note{i}.md")
        # Was a real 20 ms sleep between writes. Now half the quiet window
        # passes on the fake clock between events and the flush loop is made
        # to look each time: the burst spans more than one window in total,
        # so only the per-event extension of the window can be holding it.
        clock.advance(DEBOUNCE / 2)
        await clock.evaluations_after(1)
        assert core.calls == []
    clock.advance(DEBOUNCE)
    assert await wait_for_sync(core)
    # long enough for any spurious second flush: the fake clock moves well past
    # another window and the loop evaluates twice (instead of a real sleep).
    clock.advance(DEBOUNCE * 3)
    await clock.evaluations_after(2)
    assert len(core.calls) == 1


async def test_backups_writes_are_ignored(env):
    manager, core, docs, clock, emit = env
    (docs / "backups" / "a.20260709-120000.md").write_text("snapshot", encoding="utf-8")
    emit(FileCreatedEvent, docs / "backups" / "a.20260709-120000.md")
    # Was a real sleep of three debounce windows; now three windows pass on the
    # fake clock and the flush loop evaluates twice with nothing due.
    clock.advance(DEBOUNCE * 3)
    await clock.evaluations_after(2)
    assert core.calls == []


def test_unsupported_suffix_is_ignored(tmp_path):
    """Unsupported files never reach the manager's dirty-path seam.

    Exercise the event filter directly so observer startup events and the
    asynchronous debounce loop cannot race the assertion.  The neighboring
    tests cover delivery through the real observer and flush task.
    (Corrected 2026-09-22: the neighboring tests now deliver through a
    FakeObserver into the real handler and the real flush task; see the
    module docstring.)
    """
    docs = tmp_path / "docs"
    docs.mkdir()

    class Marker:
        exclude_patterns = ["backups"]
        sync_conflict_patterns = None

        def __init__(self):
            self.marked = []

        def _mark(self, project, rel_path, event_type):
            self.marked.append((project, rel_path, event_type))

    class Event:
        is_directory = False
        event_type = "created"

        def __init__(self, path):
            self.src_path = str(path)
            self.dest_path = None

    marker = Marker()
    handler = _ProjectEventHandler(marker, "W", docs.resolve(), DEFAULT_POLICY)
    handler.on_any_event(Event(docs / "junk.tmp"))
    handler.on_any_event(Event(docs / "video.mkv"))
    assert marker.marked == []


async def test_delete_triggers_sync(env):
    manager, core, docs, clock, emit = env
    (docs / "a.md").unlink()
    emit(FileDeletedEvent, docs / "a.md")
    clock.advance(DEBOUNCE)
    assert await wait_for_sync(core)


async def test_unwatch_stops_events(env):
    manager, core, docs, clock, emit = env
    manager.unwatch("W")
    (docs / "a.md").write_text("edited after unwatch", encoding="utf-8")
    emit(FileModifiedEvent, docs / "a.md")
    # Was a real sleep of three debounce windows; see test_backups_writes_are_ignored.
    clock.advance(DEBOUNCE * 3)
    await clock.evaluations_after(2)
    assert core.calls == []


async def test_watch_added_project(env, tmp_path):
    manager, core, _, clock, emit = env
    other_docs = tmp_path / "other"
    other_docs.mkdir()
    manager.watch(Project(name="W2", documents_dir=other_docs, data_dir=tmp_path / "d2"))
    # The real 0.2s sleep that let the new native watch arm is gone: scheduling
    # on the FakeObserver is synchronous, so the watch is live on return.
    (other_docs / "new.md").write_text("hello", encoding="utf-8")
    emit(FileCreatedEvent, other_docs / "new.md")
    clock.advance(DEBOUNCE)
    assert await wait_for_sync(core)
    assert core.calls[0][0] == "W2"
