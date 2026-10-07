"""Focused 11.4 watcher/configuration contracts without PostgreSQL."""

import asyncio
import time
from contextlib import asynccontextmanager

import pytest
from watchdog.events import DirCreatedEvent, FileCreatedEvent

from cognita.config import CognitaConfig
from cognita.parsing import DEFAULT_POLICY
from cognita.registry import Project
from cognita.watcher import WatcherManager, _DirtyPath, _ProjectEventHandler, _ProjectState


class Core:
    exclude_patterns = ["backups"]
    sync_conflict_patterns = None

    def __init__(self):
        self.calls = []
        self.fail_once = False

    def policy_for(self, _name):
        return DEFAULT_POLICY

    @asynccontextmanager
    async def write_lock(self, _name):
        yield

    async def reconcile_paths(self, name, _docs, paths):
        self.calls.append((name, list(paths)))
        if self.fail_once:
            self.fail_once = False
            raise RuntimeError("temporary adapter failure")
        return {"indexed": len(paths), "skipped": 0, "removed": 0}


def test_watcher_configuration_defaults_and_validation():
    config = CognitaConfig()
    assert config.watch_poll_interval_s == 5.0
    assert config.watch_max_pending_paths == 100_000
    with pytest.raises(ValueError):
        CognitaConfig(watch_poll_interval_s=-1)
    with pytest.raises(ValueError):
        CognitaConfig(watch_debounce_s=float("nan"))
    with pytest.raises(ValueError):
        CognitaConfig(watch_retry_max_s=1, watch_retry_initial_s=2)


def test_directory_event_is_retained(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    core = Core()
    manager = WatcherManager(core, debounce_s=1, poll_interval_s=0)
    manager._states["P"] = _ProjectState()
    handler = _ProjectEventHandler(manager, "P", docs, DEFAULT_POLICY)
    handler.on_any_event(DirCreatedEvent(str(docs / "populated")))
    assert "populated" in manager._states["P"].dirty
    assert manager._states["P"].dirty["populated"].is_directory


def test_handler_preserves_directory_metadata_for_current_manager(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()

    class Manager:
        exclude_patterns = []
        sync_conflict_patterns = []

        def __init__(self):
            self.marked = []

        def _mark(self, project, path, event_type, *, is_directory, source):
            self.marked.append((project, path, event_type, is_directory, source))

    manager = Manager()
    handler = _ProjectEventHandler(manager, "P", docs, DEFAULT_POLICY, source="poll")
    handler.on_any_event(DirCreatedEvent(str(docs / "populated")))
    assert manager.marked == [("P", "populated", "created", True, "poll")]


def test_asset_staging_events_are_ignored_at_native_and_poll_intake(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    core = Core()
    manager = WatcherManager(core, debounce_s=1, poll_interval_s=0)
    manager._states["P"] = _ProjectState()
    native = _ProjectEventHandler(manager, "P", docs, DEFAULT_POLICY, source="native")
    polling = _ProjectEventHandler(manager, "P", docs, DEFAULT_POLICY, source="poll")

    native.on_any_event(FileCreatedEvent(str(docs / ".cognita-asset-transient" / "staged.png")))
    polling.on_any_event(FileCreatedEvent(str(docs / ".cognita-asset-transient" / "staged.png")))
    assert manager._states["P"].dirty == {}

    native.on_any_event(FileCreatedEvent(str(docs / "legitimate.png")))
    polling.on_any_event(FileCreatedEvent(str(docs / "legitimate.png")))
    dirty = manager._states["P"].dirty
    assert set(dirty) == {"legitimate.png"}
    assert dirty["legitimate.png"].sources == {"native", "poll"}


@pytest.mark.asyncio
async def test_disappeared_staging_batch_is_dropped_and_later_change_processes(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    core = Core()
    manager = WatcherManager(core, debounce_s=.03, poll_interval_s=0,
                             retry_initial_s=.03, retry_max_s=.1)
    project = Project(name="P", documents_dir=docs, data_dir=tmp_path / "data")
    await manager.start([project])
    try:
        # Simulate an event that was queued before the temporary publication
        # path disappeared. It must be discarded without an asset call or a
        # retry timer, even though it is no longer present on disk.
        now = time.monotonic()
        stale = _DirtyPath(".cognita-asset-transient/staged.png", first_seen=now,
                           last_seen=now, due_at=now)
        manager._states["P"].dirty = {stale.path: stale}
        await manager._run_batch("P", {stale.path: stale})
        assert core.calls == []
        assert manager._states["P"].retry_at == 0.0
        assert manager.health("P")["pending_paths"] == 0

        manager._mark("P", "legitimate.md", "modified", source="native")
        deadline = time.monotonic() + 2
        while not core.calls and time.monotonic() < deadline:
            await asyncio.sleep(.02)
        assert core.calls == [("P", ["legitimate.md"])]
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_invalid_directory_marker_is_retired_before_asset_and_text_passes(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    core = Core()

    class Assets:
        def __init__(self):
            self.calls = []

        async def reconcile_paths(self, paths, *, dirty_prefixes=None, **_kwargs):
            self.calls.append((list(paths), list(dirty_prefixes or [])))
            return {"indexed": len(paths), "skipped": 0, "removed": 0}

    assets = Assets()
    manager = WatcherManager(core, debounce_s=.03, poll_interval_s=0,
                             asset_services={"P": assets})
    project = Project(name="P", documents_dir=docs, data_dir=tmp_path / "data")
    await manager.start([project])
    try:
        now = time.monotonic()
        invalid = ".::TMPNAME:D:3387398%9918639760575770279:New folder"
        batch = {
            invalid: _DirtyPath(invalid, True, now, now),
            "note.md": _DirtyPath("note.md", False, now, now),
            "image.png": _DirtyPath("image.png", False, now, now),
        }

        await manager._run_batch("P", batch, manager._states["P"].queue_generation)

        assert core.calls == [("P", ["note.md"])]
        assert assets.calls == [(["image.png"], [])]
        summary = manager.health("P")["last_summary"]
        assert summary["failed"] == 1
        assert summary["failures"][0]["path"] == invalid
        assert summary["failures"][0]["retryable"] is False
        assert manager._states["P"].retry_at == 0.0
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_clear_queue_does_not_restore_cancelled_batch_and_later_events_run(tmp_path, monkeypatch):
    docs = tmp_path / "docs"
    docs.mkdir()
    core = Core()
    manager = WatcherManager(core, debounce_s=.03, poll_interval_s=0,
                             retry_initial_s=.03, retry_max_s=.1)
    project = Project(name="P", documents_dir=docs, data_dir=tmp_path / "data")
    await manager.start([project])
    started = asyncio.Event()
    calls = []

    async def reconcile(_name, _docs, paths):
        calls.append(sorted(paths))
        if "old.md" in paths:
            started.set()
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                # Model an adapter that reports a late failure as it unwinds.
                raise RuntimeError("active batch failed after clear")
        return {"indexed": len(paths), "skipped": 0, "removed": 0,
                "failed": 0, "retryable_failures": [], "failures": []}

    monkeypatch.setattr(manager, "_reconcile", reconcile)
    try:
        now = time.monotonic()
        state = manager._states["P"]
        old_batch = {"old.md": _DirtyPath("old.md", False, now, now)}
        state.task = asyncio.create_task(
            manager._run_batch("P", old_batch, state.queue_generation)
        )
        await asyncio.wait_for(started.wait(), timeout=2)

        result = await manager.clear_queue("P")

        assert result == {"project": "P", "cleared_paths": 0,
                          "active_cancelled": True}
        assert state.dirty == {}
        assert state.retry_at == 0.0
        assert state.queue_generation == 1
        assert state.task.done()

        manager._mark("P", "new.md", "created")
        deadline = time.monotonic() + 2
        while len(calls) < 2 and time.monotonic() < deadline:
            await asyncio.sleep(.02)
        assert calls == [["old.md"], ["new.md"]]
        assert state.dirty == {}
        assert state.retry_at == 0.0
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_duplicate_events_coalesce_and_retry_recovers(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    core = Core()
    manager = WatcherManager(core, debounce_s=.03, poll_interval_s=0,
                             retry_initial_s=.03, retry_max_s=.1)
    project = Project(name="P", documents_dir=docs, data_dir=tmp_path / "data")
    await manager.start([project])
    try:
        core.fail_once = True
        manager._mark("P", "a.md", "created", source="native")
        manager._mark("P", "a.md", "modified", source="poll")
        deadline = time.monotonic() + 3
        while len(core.calls) < 2 and time.monotonic() < deadline:
            await asyncio.sleep(.02)
        assert len(core.calls) == 2
        assert core.calls[0][1] == ["a.md"]
        assert manager.health("P")["last_successful_reconciliation"] is not None
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_watcher_passes_attachment_identity_to_text_reconciliation(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    sentinel = object()

    class AttachedCore(Core):
        def attach_root(self, name, attached_docs):
            assert name == "P"
            assert attached_docs == docs.resolve()
            return sentinel

        def detach_root(self, name):
            assert name == "P"

        async def reconcile_paths(self, name, _docs, paths, *, root_identity=None):
            self.calls.append((name, list(paths), root_identity))
            return {"indexed": len(paths), "skipped": 0, "removed": 0}

    core = AttachedCore()
    manager = WatcherManager(core, debounce_s=.03, poll_interval_s=0)
    project = Project(name="P", documents_dir=docs, data_dir=tmp_path / "data")
    await manager.start([project])
    try:
        manager._mark("P", "note.md", "created")
        deadline = time.monotonic() + 3
        while not core.calls and time.monotonic() < deadline:
            await asyncio.sleep(.02)
        assert core.calls == [("P", ["note.md"], sentinel)]
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_project_quiet_window_uses_latest_dirty_path_deadline(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    core = Core()
    manager = WatcherManager(core, debounce_s=.15, poll_interval_s=0)
    project = Project(name="P", documents_dir=docs, data_dir=tmp_path / "data")
    await manager.start([project])
    try:
        manager._mark("P", "first.md", "created")
        await asyncio.sleep(.10)
        manager._mark("P", "second.md", "created")
        await asyncio.sleep(.10)
        assert core.calls == []
        deadline = time.monotonic() + 2
        while not core.calls and time.monotonic() < deadline:
            await asyncio.sleep(.02)
        assert core.calls == [("P", ["first.md", "second.md"])]
    finally:
        await manager.stop()
