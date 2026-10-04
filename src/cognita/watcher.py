"""Cognita-side file watcher (4.0-M4, DESIGN-4.0-vector-engine.md D4.9).

In 3.x each worker ran its own watchdog inside the engine; 4.0 absorbs it.
One watchdog observer serves every project: filesystem events (filtered to
supported formats, exclude patterns honored — backups/ writes never wake us)
mark the project dirty, and once the project has been QUIET for the debounce
window, the manager runs one smart sync — core.index_project — which is the
same write-locked, per-document-transactional path chat writes take. Bursts
(an editor's save dance, an OneDrive sync batch) coalesce into a single pass,
and the smart skip makes false wakeups nearly free (a stat per file).

Provenance: every flush logs which paths triggered it and what the sync did,
so "why did this reindex happen" is always answerable from cognita.log.

Gateway writes also land here (add_document writes the file → event fires),
but the flush finds mtime+size already matching the freshly indexed row and
skips — no double embedding.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import math
import os
import random
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer
from watchdog.observers.polling import PollingObserver

from .assets.publication import TEMP_PREFIX as ASSET_STAGING_PREFIX
from .parsing import DEFAULT_POLICY, ExtensionPolicy, _is_excluded, is_sync_conflict
from .registry import Project
from .retrieval import RetrievalCore

log = logging.getLogger("cognita.watcher")

DEFAULT_DEBOUNCE_S = 10.0  # matches the 3.x engine's watcher

# Pure-read inotify events: a file opened/closed WITHOUT modification. The
# OneDrive daemon's periodic hash scans emit these for the whole tree (seen
# live on kei within minutes of the first M4 deploy) — they must not wake the
# sync, or every scan costs a walk. Writes still arrive as created/modified/
# moved/deleted/closed(-write).
_READ_ONLY_EVENTS = frozenset({"opened", "closed_no_write"})


def _is_asset_staging_path(path: str) -> bool:
    """Return whether a relative event path belongs to asset publication staging.

    Publication renames a completed PNG into place, but its temporary files and
    directories are deliberately not project documents.  They can still emit
    create/delete events while the watcher is attached, so this boundary must
    be shared by native intake, polling intake, and stale queued batches.
    """
    return any(part.startswith(ASSET_STAGING_PREFIX) for part in Path(path).parts)


class _LegacyProjectEventHandler(FileSystemEventHandler):
    """Runs on watchdog's observer thread — must only mark state, never index."""

    def __init__(
        self,
        manager: WatcherManager,
        project_name: str,
        documents_dir: Path,
        policy: ExtensionPolicy,
    ):
        self._manager = manager
        self._name = project_name
        self._docs = documents_dir
        # Resolved once at watch time, not per event: this runs on watchdog's
        # observer thread for every filesystem event in the tree.
        self._extensions = policy.all_extensions
        self._extensions = frozenset(self._extensions) | {".png"}

    def on_any_event(self, event: FileSystemEvent) -> None:
        if event.is_directory or event.event_type in _READ_ONLY_EVENTS:
            return
        for raw in (getattr(event, "src_path", None), getattr(event, "dest_path", None)):
            if not raw:
                continue
            path = Path(str(raw))
            # Spans BOTH tiers: the watcher must wake for registered extensions
            # too, or an on-disk edit to a script would never reach the index.
            if path.suffix.lower() not in self._extensions:
                continue
            try:
                # Match LEXICALLY first, then fall back to resolve(). The walker
                # (iter_document_files) uses followlinks=True because OneDrive
                # trees on kei contain symlinks — so files under a symlinked
                # subdirectory ARE indexed. resolve() sends those to their real
                # location outside _docs, relative_to raised, and the event was
                # dropped: indexed once, then never refreshed on edit, with
                # search serving stale content and nothing saying so.
                try:
                    rel = path.relative_to(self._docs)
                except ValueError:
                    rel = path.resolve().relative_to(self._docs)
            except (ValueError, OSError):
                continue  # outside the documents dir (or unresolvable) — not ours
            if _is_asset_staging_path(rel.as_posix()):
                source_guard = getattr(self._manager, "source_guard", None)
                if not source_guard or not source_guard.enabled:
                    log.debug("Ignoring asset publication staging path: %s", rel.as_posix())
                continue
            if _is_excluded(rel, self._manager.exclude_patterns):
                continue  # backups/ etc. — the gateway's own snapshots land there
            if is_sync_conflict(path.name, self._manager.sync_conflict_patterns):
                # 5.0 §10: OneDrive dropping a conflict copy into a watched pack
                # directory must not index it. Without this the watcher is the
                # FASTEST path from a sync collision to a corrupted corpus — it
                # fires within the debounce window, long before anyone looks.
                source_guard = getattr(self._manager, "source_guard", None)
                if not source_guard or not source_guard.enabled:
                    log.info("Ignoring cloud-sync conflict copy: %s", rel.as_posix())
                continue
            self._manager._mark(self._name, rel.as_posix(), event.event_type)


class _LegacyWatcherManager:
    """One watchdog observer + one flush loop for all watched projects."""

    def __init__(
        self,
        core: RetrievalCore,
        *,
        debounce_s: float = DEFAULT_DEBOUNCE_S,
        exclude_patterns: list[str] | None = None,
        sync_conflict_patterns: list[str] | None = None,
        asset_services: dict[str, object] | None = None,
    ):
        self.core = core
        self.debounce_s = debounce_s
        self.exclude_patterns = (
            core.exclude_patterns if exclude_patterns is None else exclude_patterns
        )
        self.sync_conflict_patterns = (
            core.sync_conflict_patterns
            if sync_conflict_patterns is None else sync_conflict_patterns
        )
        self._observer: Observer | None = None
        self._watches: dict[str, object] = {}  # project -> watchdog watch handle
        self._docs_dirs: dict[str, Path] = {}
        self._lock = threading.Lock()  # guards _dirty/_last_event (watchdog threads)
        self._dirty: dict[str, dict[str, str]] = {}  # project -> {relpath: event_type}
        self._last_event: dict[str, float] = {}
        self._flush_task: asyncio.Task | None = None
        self._syncing: set[str] = set()
        self.asset_services = asset_services or {}

    # ---------------- lifecycle ----------------

    async def start(self, projects: list[Project]) -> None:
        self._observer = Observer()
        self._observer.daemon = True
        for project in projects:
            if project.enabled:
                self.watch(project)
        self._observer.start()
        self._flush_task = asyncio.create_task(self._flush_loop())
        log.info("Watcher: %d project(s), debounce %.0fs", len(self._watches), self.debounce_s)

    async def stop(self) -> None:
        if self._flush_task is not None:
            self._flush_task.cancel()
            self._flush_task = None
        if self._observer is not None:
            self._observer.stop()
            self._observer.join(timeout=5)
            self._observer = None

    def watch(self, project: Project, *, asset_service: object | None = None) -> None:
        """Start watching a project's documents dir (admin add calls this too)."""
        if asset_service is not None:
            self.asset_services[project.name] = asset_service
        if self._observer is None or project.name in self._watches:
            return
        docs = Path(project.documents_dir).resolve()
        if not docs.is_dir():
            log.warning("Watcher: %s documents dir missing: %s", project.name, docs)
            return
        handler = _ProjectEventHandler(self, project.name, docs, self._policy(project.name))
        self._watches[project.name] = self._observer.schedule(handler, str(docs), recursive=True)
        self._docs_dirs[project.name] = Path(project.documents_dir)
        log.info("Watcher: watching %s (%s)", project.name, docs)

    def _policy(self, name: str) -> ExtensionPolicy:
        resolve = getattr(self.core, "policy_for", None)
        return resolve(name) if resolve else DEFAULT_POLICY

    def unwatch(self, name: str) -> None:
        watch = self._watches.pop(name, None)
        if watch is not None and self._observer is not None:
            self._observer.unschedule(watch)
        self._docs_dirs.pop(name, None)
        self.asset_services.pop(name, None)
        with self._lock:
            self._dirty.pop(name, None)
            self._last_event.pop(name, None)

    # ---------------- event intake (watchdog threads) ----------------

    def _mark(self, project: str, rel_path: str, event_type: str) -> None:
        with self._lock:
            self._dirty.setdefault(project, {})[rel_path] = event_type
            self._last_event[project] = time.monotonic()

    # ---------------- debounced flush (event loop) ----------------

    async def _flush_loop(self) -> None:
        poll = max(0.05, min(1.0, self.debounce_s / 4))
        while True:
            await asyncio.sleep(poll)
            now = time.monotonic()
            due: list[tuple[str, dict[str, str]]] = []
            with self._lock:
                for name, changes in list(self._dirty.items()):
                    if not changes or name in self._syncing:
                        continue
                    if now - self._last_event.get(name, 0) >= self.debounce_s:
                        due.append((name, changes))
                        self._dirty[name] = {}
            for name, changes in due:
                self._syncing.add(name)
                try:
                    await self._sync(name, changes)
                except Exception:
                    log.exception("Watcher: sync failed for %s", name)
                finally:
                    self._syncing.discard(name)

    async def _sync(self, name: str, changes: dict[str, str]) -> None:
        docs_dir = self._docs_dirs.get(name)
        if docs_dir is None:
            return
        shown = [f"{kind}:{path}" for path, kind in sorted(changes.items())]
        listed = ", ".join(shown[:10]) + (
            f", +{len(shown) - 10} more" if len(shown) > 10 else ""
        )
        log.info(
            "Watcher: %s — %d change(s) on disk [%s] -> smart sync",
            name, len(changes), listed,
        )
        started = time.monotonic()
        asset_paths = [path for path in changes if path.lower().endswith(".png")]
        asset_service = self.asset_services.get(name)
        if asset_service is not None and asset_paths:
            async with self.core.write_lock(name):
                await asset_service.reconcile_paths(asset_paths)
            changes = {path: kind for path, kind in changes.items() if path not in asset_paths}
            if not changes:
                log.info(
                    "Watcher: %s reconciled %d asset change(s) in %.1fs",
                    name, len(asset_paths), time.monotonic() - started,
                )
                return
        summary = await self.core.index_project(name, docs_dir)
        log.info(
            "Watcher: %s synced in %.1fs — indexed %d, skipped %d, removed %d%s",
            name,
            time.monotonic() - started,
            summary["indexed"],
            summary["skipped"],
            summary["removed"],
            f", errors: {summary['errors']}" if summary["errors"] else "",
        )


# ---------------------------------------------------------------------------
# 11.4 implementation.  Kept below the 4.0 compatibility implementation so
# downstream imports retain their names while the newer manager is selected.

DEFAULT_POLL_INTERVAL_S = 5.0
DEFAULT_MAX_PENDING_PATHS = 100_000
DEFAULT_RETRY_INITIAL_S = 5.0
DEFAULT_RETRY_MAX_S = 300.0
_ROOT_MARKER = "."


@dataclass
class _DirtyPath:
    path: str
    is_directory: bool = False
    first_seen: float = 0.0
    last_seen: float = 0.0
    attempts: int = 0
    due_at: float = 0.0
    sources: set[str] = field(default_factory=set)
    event_types: set[str] = field(default_factory=set)


@dataclass
class _ProjectState:
    dirty: dict[str, _DirtyPath] = field(default_factory=dict)
    task: asyncio.Task | None = None
    retry_at: float = 0.0
    attached_root: Path | None = None
    root_identity: tuple[int, int] | None = None
    retrieval_root_identity: Any | None = None
    degraded: bool = False
    health: dict[str, Any] = field(default_factory=dict)


def _bounded_exception(exc: object, limit: int = 240) -> str:
    return " ".join(str(exc).splitlines())[:limit]


class _ProjectEventHandler(FileSystemEventHandler):
    """Translate observer hints to normalized, project-owned dirty paths."""

    def __init__(self, manager: WatcherManager, project_name: str, documents_dir: Path,
                 policy: ExtensionPolicy, source: str = "native"):
        self._manager = manager
        self._name = project_name
        self._docs = documents_dir
        self._source = source
        self._extensions = frozenset(policy.all_extensions) | {".png"}

    def on_any_event(self, event: FileSystemEvent) -> None:
        if event.event_type in _READ_ONLY_EVENTS:
            return
        directory = bool(getattr(event, "is_directory", False))
        for raw in (getattr(event, "src_path", None), getattr(event, "dest_path", None)):
            if not raw:
                continue
            path = Path(str(raw))
            if not directory and path.suffix.lower() not in self._extensions:
                continue
            try:
                try:
                    rel = path.relative_to(self._docs)
                except ValueError:
                    # Follow-links policy is owned by the existing walker; the
                    # lexical path remains the first and safest interpretation.
                    rel = path.resolve().relative_to(self._docs)
            except (OSError, ValueError):
                continue
            name = rel.as_posix() if str(rel) not in ("", ".") else _ROOT_MARKER
            if _is_asset_staging_path(name):
                source_guard = getattr(self._manager, "source_guard", None)
                if not source_guard or not source_guard.enabled:
                    log.debug("Ignoring asset publication staging path: %s", name)
                continue
            if _is_excluded(rel, self._manager.exclude_patterns):
                continue
            if not directory and is_sync_conflict(path.name, self._manager.sync_conflict_patterns):
                source_guard = getattr(self._manager, "source_guard", None)
                if not source_guard or not source_guard.enabled:
                    log.info("Ignoring cloud-sync conflict copy: %s", name)
                continue
            self._mark(name, event.event_type, directory)

    def _mark(self, path: str, event_type: str, is_directory: bool) -> None:
        """Call both the 11.4 and legacy manager marker seams safely.

        Older embedders supplied a three-argument ``_mark`` callback.  Keep
        that callback valid while passing directory/source provenance to the
        current WatcherManager, which needs it for targeted asset prefixes.
        """
        marker = self._manager._mark
        try:
            parameters = inspect.signature(marker).parameters.values()
            names = {parameter.name for parameter in parameters}
            accepts_kwargs = any(
                parameter.kind == inspect.Parameter.VAR_KEYWORD
                for parameter in parameters
            )
        except (TypeError, ValueError):
            names, accepts_kwargs = set(), False
        kwargs: dict[str, Any] = {}
        if accepts_kwargs or "is_directory" in names:
            kwargs["is_directory"] = is_directory
        if accepts_kwargs or "source" in names:
            kwargs["source"] = self._source
        marker(self._name, path, event_type, **kwargs)


class WatcherManager:
    """Dual-observer discovery with one tracked async task per project."""

    def __init__(self, core: RetrievalCore, *, debounce_s: float = DEFAULT_DEBOUNCE_S,
                 poll_interval_s: float = DEFAULT_POLL_INTERVAL_S,
                 max_pending_paths: int = DEFAULT_MAX_PENDING_PATHS,
                 retry_initial_s: float = DEFAULT_RETRY_INITIAL_S,
                 retry_max_s: float = DEFAULT_RETRY_MAX_S,
                 exclude_patterns: list[str] | None = None,
                 sync_conflict_patterns: list[str] | None = None,
                 asset_services: dict[str, object] | None = None,
                 source_guard: object | None = None):
        self.core = core
        self.debounce_s = debounce_s
        self.poll_interval_s = poll_interval_s
        self.max_pending_paths = max_pending_paths
        self.retry_initial_s = retry_initial_s
        self.retry_max_s = retry_max_s
        self.exclude_patterns = core.exclude_patterns if exclude_patterns is None else exclude_patterns
        self.sync_conflict_patterns = core.sync_conflict_patterns if sync_conflict_patterns is None else sync_conflict_patterns
        self.asset_services = asset_services or {}
        self.source_guard = source_guard
        self._observer: Observer | None = None
        self._polling_observer: PollingObserver | None = None
        self._watches: dict[str, object] = {}
        self._polling_watches: dict[str, object] = {}
        self._docs_dirs: dict[str, Path] = {}
        self._states: dict[str, _ProjectState] = {}
        self._projects: dict[str, Project] = {}
        self._lock = threading.RLock()
        self._flush_task: asyncio.Task | None = None
        self._stopping = False
        self._active_source = "native"
        self._collapse_logged: set[str] = set()
        self._dead_observers_logged: set[str] = set()

    async def start(self, projects: list[Project]) -> None:
        if self._flush_task is not None:
            return
        self._stopping = False
        self._projects = {project.name: project for project in projects if project.enabled}
        self._observer = Observer()
        self._observer.daemon = True
        if self.poll_interval_s > 0:
            self._polling_observer = PollingObserver(timeout=self.poll_interval_s)
            self._polling_observer.daemon = True
        for project in projects:
            if project.enabled:
                self.watch(project)
                if self.source_guard and self.source_guard.enabled and project.name not in self._watches:
                    # A configured source may be absent at cold start. Keep it
                    # in the existing flush loop so a later remount can attach
                    # the observer and trigger full reconciliation.
                    with self._lock:
                        self._docs_dirs[project.name] = Path(project.documents_dir)
                        state = self._states.setdefault(project.name, _ProjectState())
                        state.degraded = True
                        state.health.update({
                            "active": False, "degraded": True,
                            "last_error": "source_unavailable",
                        })
        self._observer.start()
        if self._polling_observer is not None:
            self._polling_observer.start()
        self._flush_task = asyncio.create_task(self._flush_loop(), name="cognita-watcher-flush")
        log.info("Watcher: projects=%d native=%s polling=%s poll_interval=%.3fs debounce=%.3fs",
                 len(self._watches), type(self._observer).__name__,
                 type(self._polling_observer).__name__ if self._polling_observer else "disabled",
                 self.poll_interval_s, self.debounce_s)
        if self.poll_interval_s == 0:
            log.warning("Watcher polling disabled; native events alone are not a reliable correctness boundary")

    async def stop(self) -> None:
        self._stopping = True
        flush = self._flush_task
        self._flush_task = None
        if flush is not None:
            flush.cancel()
            await asyncio.gather(flush, return_exceptions=True)
        with self._lock:
            tasks = [s.task for s in self._states.values() if s.task is not None and not s.task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        for observer, label in ((self._observer, "native"), (self._polling_observer, "polling")):
            if observer is None:
                continue
            observer.stop()
            observer.join(timeout=5)
            if observer.is_alive():
                log.error("Watcher %s observer thread failed to stop", label)
        self._observer = self._polling_observer = None
        with self._lock:
            for state in self._states.values():
                state.task = None

    def watch(self, project: Project, *, asset_service: object | None = None) -> None:
        if asset_service is not None:
            self.asset_services[project.name] = asset_service
        if self._stopping:
            return
        self._projects[project.name] = project
        with self._lock:
            if self._observer is None or project.name in self._watches:
                return
        docs = Path(project.documents_dir).resolve()
        try:
            stat = docs.stat()
            if not docs.is_dir() or not os.access(docs, os.R_OK):
                raise OSError("root is not readable")
            identity = (int(getattr(stat, "st_dev", 0)), int(getattr(stat, "st_ino", 0)))
        except OSError as exc:
            log.warning("Watcher: project=%s root attachment failed reason=%s", project.name, type(exc).__name__)
            return
        retrieval_identity: Any | None = None
        attach_root = getattr(self.core, "attach_root", None)
        if callable(attach_root):
            try:
                retrieval_identity = attach_root(project.name, docs)
            except (OSError, RuntimeError, TypeError, ValueError) as exc:
                log.warning(
                    "Watcher: project=%s retrieval root attachment failed reason=%s",
                    project.name, type(exc).__name__,
                )
                return
        policy = self._policy(project.name)
        native = None
        try:
            native = self._observer.schedule(
                _ProjectEventHandler(self, project.name, docs, policy, "native"),
                str(docs), recursive=True,
            )
            polling = self._polling_observer.schedule(
                _ProjectEventHandler(self, project.name, docs, policy, "poll"),
                str(docs), recursive=True,
            ) if self._polling_observer else None
        except Exception as exc:  # watchdog backend boundary
            if native is not None:
                try:
                    self._observer.unschedule(native)
                except Exception:  # best-effort rollback of a partial attachment
                    pass
            detach_root = getattr(self.core, "detach_root", None)
            if callable(detach_root):
                try:
                    detach_root(project.name)
                except (OSError, RuntimeError, TypeError, ValueError):
                    log.warning(
                        "Watcher: project=%s retrieval root rollback failed",
                        project.name,
                    )
            log.error(
                "Watcher: project=%s observer scheduling failed reason=%s",
                project.name, type(exc).__name__,
            )
            return
        with self._lock:
            self._watches[project.name] = native
            if polling is not None:
                self._polling_watches[project.name] = polling
            self._docs_dirs[project.name] = Path(project.documents_dir)
            state = self._states.setdefault(project.name, _ProjectState())
            state.attached_root = docs
            state.root_identity = identity
            state.retrieval_root_identity = retrieval_identity
            state.degraded = False
            state.health.update({"active": False, "degraded": False, "last_event": None,
                                 "last_successful_reconciliation": None,
                                 "last_summary": None, "last_error": None,
                                 "attached_backends": ["native"] + (["polling"] if polling else [])})
        if self.source_guard and self.source_guard.enabled:
            log.info("Watcher: watching project=%s source=mounted backends=%s",
                     project.name, state.health["attached_backends"])
        else:
            log.info("Watcher: watching project=%s root=%s backends=%s", project.name, docs,
                     state.health["attached_backends"])

    def _policy(self, name: str) -> ExtensionPolicy:
        resolver = getattr(self.core, "policy_for", None)
        return resolver(name) if resolver else DEFAULT_POLICY

    def unwatch(self, name: str) -> asyncio.Task | None:
        with self._lock:
            native, polling = self._watches.pop(name, None), self._polling_watches.pop(name, None)
            state = self._states.pop(name, None)
            self._docs_dirs.pop(name, None)
            self._projects.pop(name, None)
            self.asset_services.pop(name, None)
        if native is not None and self._observer is not None:
            self._observer.unschedule(native)
        if polling is not None and self._polling_observer is not None:
            self._polling_observer.unschedule(polling)
        detach_root = getattr(self.core, "detach_root", None)
        if callable(detach_root):
            detach_root(name)
        if state and state.task and not state.task.done():
            state.task.cancel()
            return state.task
        return None

    def _mark(self, project: str, rel_path: str, event_type: str, *, is_directory: bool = False,
              source: str = "native") -> None:
        if self._stopping:
            return
        if _is_asset_staging_path(rel_path):
            if not self.source_guard or not self.source_guard.enabled:
                log.debug("Ignoring asset publication staging path: %s", rel_path)
            return
        now = time.monotonic()
        with self._lock:
            state = self._states.get(project)
            if state is None:
                return
            item = state.dirty.get(rel_path)
            if item is None and len(state.dirty) >= self.max_pending_paths:
                state.dirty.clear()
                item = _DirtyPath(_ROOT_MARKER, True, now, now)
                state.dirty[_ROOT_MARKER] = item
                if project not in self._collapse_logged:
                    self._collapse_logged.add(project)
                    log.warning("Watcher: project=%s pending path queue collapsed to root marker", project)
            elif item is None:
                item = _DirtyPath(rel_path, is_directory, now, now)
                state.dirty[rel_path] = item
            item.is_directory |= is_directory
            item.last_seen, item.due_at = now, now + self.debounce_s
            item.sources.add(source)
            item.event_types.add(event_type)
            state.health["last_event"] = {"path": rel_path, "source": source, "type": event_type}

    async def _flush_loop(self) -> None:
        interval = max(0.05, min(1.0, self.debounce_s / 4))
        while not self._stopping:
            await asyncio.sleep(interval)
            for observer, label in (
                (self._observer, "native"),
                (self._polling_observer, "polling"),
            ):
                if observer is not None and not observer.is_alive() \
                        and label not in self._dead_observers_logged:
                    self._dead_observers_logged.add(label)
                    log.error("Watcher %s observer thread died", label)
            now = time.monotonic()
            with self._lock:
                for name, state in list(self._states.items()):
                    if state.task is not None and not state.task.done():
                        continue
                    if not state.dirty and self.source_guard and self.source_guard.enabled:
                        # DrvFS may miss the event that follows a bind remount.
                        # Recheck the recorded identity from the existing
                        # bounded flush loop so recovery does not depend on a
                        # native filesystem notification.
                        docs = self._docs_dirs.get(name)
                        if docs is not None:
                            source_state = self.source_guard.check(docs)
                            if source_state.state == "reconnected":
                                state.dirty[_ROOT_MARKER] = _DirtyPath(
                                    _ROOT_MARKER, True, now, now,
                                )
                    if not state.dirty:
                        continue
                    # The debounce is a project quiet window, not a per-path
                    # earliest-deadline queue. A later event anywhere in the
                    # project keeps the whole coherent batch deferred.
                    if max(item.due_at for item in state.dirty.values()) > now or state.retry_at > now:
                        continue
                    batch, state.dirty = state.dirty, {}
                    state.retry_at = 0.0
                    state.task = asyncio.create_task(self._run_batch(name, batch), name=f"cognita-watcher:{name}")

    async def _run_batch(self, name: str, batch: dict[str, _DirtyPath]) -> None:
        started = time.monotonic()
        state, docs = self._states.get(name), self._docs_dirs.get(name)
        if state is None or docs is None:
            return
        ignored = [path for path in batch if _is_asset_staging_path(path)]
        if ignored:
            with self._lock:
                for path in ignored:
                    state.dirty.pop(path, None)
            batch = {path: item for path, item in batch.items()
                     if not _is_asset_staging_path(path)}
            log.debug(
                "Watcher: project=%s dropped %d stale asset staging path(s) before reconciliation",
                name, len(ignored),
            )
        if not batch:
            state.health.update({"active": False, "last_error": None,
                                 "last_summary": {"ignored_staging_paths": len(ignored)}})
            return
        state.health["active"] = True
        source_state = self.source_guard.check(docs) if self.source_guard else None
        if source_state is not None and source_state.state == "unavailable":
            state.health.update({"active": False, "degraded": True,
                                 "last_error": "source_unavailable"})
            log.warning("Watcher: project=%s source unavailable reason=%s", name, source_state.reason)
            return
        if source_state is not None and source_state.state == "reconnected":
            source_state = self.source_guard.check(docs, validate_names=True)
            if source_state.state == "unavailable":
                state.health.update({"active": False, "degraded": True,
                                     "last_error": source_state.reason or "source_unavailable"})
                log.warning("Watcher: project=%s source unavailable reason=%s",
                            name, source_state.reason)
                return
            try:
                project = self._projects.get(name)
                if project is None:
                    raise RuntimeError("project configuration is unavailable")
                self._reattach(project)
                if name not in self._watches:
                    raise RuntimeError("watcher attachment failed")
                state.attached_root = docs.resolve()
                stat = docs.stat()
                state.root_identity = (int(stat.st_dev), int(stat.st_ino))
                attach_root = getattr(self.core, "attach_root", None)
                if callable(attach_root):
                    state.retrieval_root_identity = attach_root(name, docs)
                async with self.core.write_lock(name):
                    index_method = self.core.index_project
                    index_kwargs: dict[str, Any] = {}
                    try:
                        parameters = inspect.signature(index_method).parameters.values()
                    except (TypeError, ValueError):
                        parameters = ()
                    if any(parameter.name == "before_removal" or
                           parameter.kind == inspect.Parameter.VAR_KEYWORD
                           for parameter in parameters):
                        index_kwargs["before_removal"] = lambda: self.source_guard.check(
                            docs,
                        ).state in {"available", "reconnected"}
                    result = index_method(name, docs, **index_kwargs)
                    summary = await result if hasattr(result, "__await__") else result
                    service = self.asset_services.get(name)
                    if service is not None:
                        reconcile_all = service.reconcile_all
                        try:
                            parameters = inspect.signature(reconcile_all).parameters.values()
                        except (TypeError, ValueError):
                            parameters = ()
                        if any(parameter.name == "source_is_safe" or
                               parameter.kind == inspect.Parameter.VAR_KEYWORD
                               for parameter in parameters):
                            result_assets = reconcile_all(source_is_safe=lambda: self.source_guard.check(
                                docs,
                            ).state in {"available", "reconnected"})
                            if hasattr(result_assets, "__await__"):
                                await result_assets
                        else:
                            await reconcile_all()
                reconciled_source = self.source_guard.mark_reconciled(docs)
                if reconciled_source.state == "unavailable":
                    raise RuntimeError(reconciled_source.reason or "source_unavailable")
                state.health.update({"active": False, "degraded": False,
                                     "last_error": None,
                                     "last_successful_reconciliation": time.time(),
                                     "last_summary": summary})
                log.info("Watcher: project=%s source reconnected; full reconciliation complete", name)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                state.health.update({"active": False, "degraded": True,
                                     "last_error": type(exc).__name__})
                if self.source_guard and self.source_guard.enabled:
                    log.error("Watcher: project=%s reconnect reconciliation failed reason=%s",
                              name, type(exc).__name__)
                else:
                    log.exception("Watcher: project=%s reconnect reconciliation failed", name)
            return
        if self.source_guard and self.source_guard.enabled:
            # A source can disappear between the batch-level check and either
            # reconciliation boundary. Never read or mutate the index against
            # a different mount instance.
            source_state = self.source_guard.check(docs)
            if source_state.state != "available":
                state.health.update({"active": False, "degraded": True,
                                     "last_error": source_state.reason or "source_unavailable"})
                log.warning("Watcher: project=%s source unavailable reason=%s",
                            name, source_state.reason or "source_unavailable")
                return
        if not self._root_is_safe(name):
            self._retry(name, batch, "root identity check failed")
            return
        try:
            summary = await self._reconcile(name, docs, batch)
        except asyncio.CancelledError:
            self._merge_batch(name, batch)
            raise
        except Exception as exc:  # noqa: BLE001 - reconciliation adapter boundary
            self._retry(
                name, batch,
                type(exc).__name__ if self.source_guard and self.source_guard.enabled
                else _bounded_exception(exc),
            )
            return
        if self.source_guard and self.source_guard.enabled:
            source_state = self.source_guard.check(docs)
            if source_state.state != "available":
                state.health.update({"active": False, "degraded": True,
                                     "last_error": source_state.reason or "source_unavailable"})
                log.warning("Watcher: project=%s source unavailable reason=%s",
                            name, source_state.reason or source_state.state)
                return
        failures = self._failed_paths(summary, batch)
        if failures:
            self._retry(name, {p: batch[p] for p in failures}, "per-path reconciliation failure")
        else:
            recovered = any(item.attempts for item in batch.values())
            for item in batch.values():
                item.attempts = 0
            state.health["last_successful_reconciliation"] = time.time()
            state.health["last_error"] = None
            if recovered:
                log.info("Watcher: project=%s retry recovered", name)
        state.health["active"] = False
        state.health["last_summary"] = summary if isinstance(summary, dict) else {"result": "ok"}
        if self.source_guard and self.source_guard.enabled:
            log.info("Watcher: project=%s dirty=%d expanded=%s indexed=%s metadata_refreshed=%s skipped=%s removed=%s failed=%d elapsed=%.3fs",
                     name, len(batch), bool(isinstance(summary, dict) and summary.get("expanded_paths")),
                     self._value(summary, "indexed"), self._value(summary, "metadata_refreshed"),
                     self._value(summary, "skipped"), self._value(summary, "removed"), len(failures),
                     time.monotonic() - started)
        else:
            paths = sorted(batch)
            log.info("Watcher: project=%s sources=%s dirty=%d expanded=%s indexed=%s metadata_refreshed=%s skipped=%s removed=%s failed=%d elapsed=%.3fs paths=%s",
                     name, ",".join(sorted({x for i in batch.values() for x in i.sources})) or "unknown",
                     len(batch), bool(isinstance(summary, dict) and summary.get("expanded_paths")),
                     self._value(summary, "indexed"), self._value(summary, "metadata_refreshed"),
                     self._value(summary, "skipped"), self._value(summary, "removed"), len(failures),
                     time.monotonic() - started,
                     ",".join(paths[:10]) + (f", +{len(paths)-10} omitted" if len(paths) > 10 else ""))

    async def _reconcile(self, name: str, docs: Path, batch: dict[str, _DirtyPath]) -> Any:
        # PNG files belong exclusively to AssetService; text files belong to
        # RetrievalCore.  A directory is sent to both owners because it may
        # contain either kind, while the project lock serializes the two
        # targeted passes with all other project writes.
        asset_paths: list[str] = []
        asset_prefixes: list[str] = []
        text_paths: list[str] = []
        for path, item in batch.items():
            if item.is_directory:
                prefix = "" if path == _ROOT_MARKER else path
                asset_prefixes.append(prefix)
                text_paths.append(path)
            elif path.lower().endswith(".png"):
                asset_paths.append(path)
            else:
                text_paths.append(path)

        asset_summary: Any = None
        asset_service = self.asset_services.get(name)
        if asset_service is not None and (asset_paths or asset_prefixes):
            if self.source_guard and self.source_guard.enabled:
                source_state = self.source_guard.check(docs)
                if source_state.state != "available":
                    raise RuntimeError("source_unavailable")
            async with self.core.write_lock(name):
                asset_summary = await self._asset_reconcile(
                    asset_service, asset_paths, asset_prefixes,
                    self._states[name].root_identity, docs,
                )

        text_summary: Any = None
        method = getattr(self.core, "reconcile_paths", None)
        if text_paths and callable(method):
            if self.source_guard and self.source_guard.enabled:
                source_state = self.source_guard.check(docs)
                if source_state.state != "available":
                    raise RuntimeError("source_unavailable")
            kwargs: dict[str, Any] = {}
            try:
                parameters = inspect.signature(method).parameters.values()
            except (TypeError, ValueError):
                parameters = ()
            if any(parameter.name == "root_identity" or
                   parameter.kind == inspect.Parameter.VAR_KEYWORD
                   for parameter in parameters):
                kwargs["root_identity"] = self._states[name].retrieval_root_identity
            if self.source_guard and self.source_guard.enabled and any(
                parameter.name == "diagnostic_redacted" or
                parameter.kind == inspect.Parameter.VAR_KEYWORD
                for parameter in parameters
            ):
                kwargs["diagnostic_redacted"] = True
            if self.source_guard and self.source_guard.enabled and any(
                parameter.name == "source_is_safe" or
                parameter.kind == inspect.Parameter.VAR_KEYWORD
                for parameter in parameters
            ):
                kwargs["source_is_safe"] = lambda: self.source_guard.check(
                    docs,
                ).state == "available"
            result = method(name, docs, text_paths, **kwargs)
            text_summary = await result if hasattr(result, "__await__") else result
        elif text_paths:
            if self.source_guard and self.source_guard.enabled:
                source_state = self.source_guard.check(docs)
                if source_state.state != "available":
                    raise RuntimeError("source_unavailable")
            text_summary = await self.core.index_project(name, docs)

        summaries = [value for value in (text_summary, asset_summary)
                     if isinstance(value, dict)]
        if not summaries:
            return {"indexed": 0, "metadata_refreshed": 0, "skipped": 0,
                    "removed": 0, "failed": 0, "expanded_paths": 0,
                    "retryable_failures": [], "failures": []}
        merged: dict[str, Any] = {
            key: sum(int(value.get(key, 0) or 0) for value in summaries)
            for key in ("indexed", "metadata_refreshed", "skipped", "removed",
                        "failed", "expanded_paths")
        }
        merged["failures"] = [failure for value in summaries
                               for failure in (value.get("failures") or [])]
        merged["retryable_failures"] = [path for value in summaries
                                         for path in (value.get("retryable_failures")
                                                      or value.get("failed_paths") or [])]
        # Preserve component summaries for diagnostics without changing the
        # top-level watcher contract consumed by _run_batch.
        merged["text"] = text_summary
        merged["assets"] = asset_summary
        return merged

    def _reattach(self, project: Project) -> None:
        """Replace OS watches that may still point at the pre-outage mount."""
        with self._lock:
            native = self._watches.pop(project.name, None)
            polling = self._polling_watches.pop(project.name, None)
        if native is not None and self._observer is not None:
            self._observer.unschedule(native)
        if polling is not None and self._polling_observer is not None:
            self._polling_observer.unschedule(polling)
        detach_root = getattr(self.core, "detach_root", None)
        if callable(detach_root):
            detach_root(project.name)
        self.watch(project)

    async def _asset_reconcile(
        self,
        service: object, paths: list[str], prefixes: list[str],
        root_identity: tuple[int, int] | None, documents_dir: Path,
    ) -> Any:
        method = getattr(service, "reconcile_paths")
        try:
            parameters = inspect.signature(method).parameters.values()
        except (TypeError, ValueError):
            parameters = ()
        accepts_kwargs = any(
            parameter.kind == inspect.Parameter.VAR_KEYWORD for parameter in parameters
        )
        names = {parameter.name for parameter in parameters}
        kwargs: dict[str, Any] = {}
        if accepts_kwargs or "dirty_prefixes" in names:
            kwargs["dirty_prefixes"] = prefixes
        if accepts_kwargs or "root_identity" in names:
            kwargs["root_identity"] = root_identity
        if self.source_guard and self.source_guard.enabled and (
            accepts_kwargs or "source_is_safe" in names
        ):
            kwargs["source_is_safe"] = lambda: self.source_guard.check(
                documents_dir,
            ).state == "available"
        result = method(paths, **kwargs)
        return await result if hasattr(result, "__await__") else result

    async def _sync(self, name: str, changes: dict[str, str]) -> Any:
        """Compatibility seam for callers of the pre-11.4 manager.

        The active manager uses tracked ``_DirtyPath`` batches, but a few
        maintenance integrations still invoke ``_sync`` directly.  Keep that
        call path routed through the same asset/text separation and lock.
        """
        now = time.monotonic()
        batch = {path: _DirtyPath(path, False, now, now)
                 for path in changes}
        if name not in self._states:
            self._states[name] = _ProjectState()
        for path, kind in changes.items():
            batch[path].event_types.add(kind)
            batch[path].sources.add("native")
        return await self._reconcile(name, self._docs_dirs[name], batch)

    def _root_is_safe(self, name: str) -> bool:
        state = self._states.get(name)
        if state is None or state.attached_root is None:
            return False
        try:
            stat = state.attached_root.stat()
            if not state.attached_root.is_dir() or not os.access(state.attached_root, os.R_OK):
                raise OSError("root unavailable")
            identity = (int(getattr(stat, "st_dev", 0)), int(getattr(stat, "st_ino", 0)))
            if state.root_identity is not None and identity != state.root_identity:
                raise OSError("root identity changed")
        except OSError as exc:
            state.degraded = True
            state.health.update({"degraded": True, "last_error": _bounded_exception(exc)})
            log.error("Watcher: project=%s root safety check failed reason=%s", name, type(exc).__name__)
            return False
        state.degraded, state.health["degraded"] = False, False
        return True

    def _retry(self, name: str, batch: dict[str, _DirtyPath], reason: str) -> None:
        for item in batch.values():
            item.attempts += 1
        self._merge_batch(name, batch)
        state = self._states.get(name)
        if state is None:
            return
        attempt = max((item.attempts for item in batch.values()), default=1)
        ratio = self.retry_max_s / self.retry_initial_s
        max_exponent = max(0, math.ceil(math.log2(ratio))) if ratio > 1 else 0
        exponent = min(max(0, attempt - 1), max_exponent)
        delay = min(self.retry_max_s, self.retry_initial_s * (2 ** exponent))
        delay = min(self.retry_max_s, delay * random.uniform(0.9, 1.1))
        state.retry_at = time.monotonic() + delay
        state.health.update({"active": False, "last_error": reason})
        log.warning("Watcher: project=%s retry scheduled attempt=%d delay=%.2fs reason=%s", name, attempt, delay, reason)

    def _merge_batch(self, name: str, batch: dict[str, _DirtyPath]) -> None:
        with self._lock:
            state = self._states.get(name)
            if state is None:
                return
            now = time.monotonic()
            for path, item in batch.items():
                current = state.dirty.get(path)
                if current is None:
                    item.last_seen, item.due_at = now, now + self.debounce_s
                    state.dirty[path] = item
                else:
                    current.is_directory |= item.is_directory
                    current.attempts = max(current.attempts, item.attempts)
                    current.sources.update(item.sources)
                    current.event_types.update(item.event_types)
                    current.last_seen, current.due_at = now, now + self.debounce_s

    @staticmethod
    def _failed_paths(summary: Any, batch: dict[str, _DirtyPath]) -> set[str]:
        if not isinstance(summary, dict):
            return set()
        failures = summary.get("failed_paths") or summary.get("retryable_failures") or []
        if isinstance(failures, dict):
            failures = failures.keys()
        failed: set[str] = set()
        for value in failures:
            path = str(value)
            if path in batch:
                failed.add(path)
                continue
            # A directory reconciliation may report a descendant failure;
            # retry the owning directory marker so the next pass re-expands it.
            if any(item.is_directory and (prefix == _ROOT_MARKER or
                                          path.startswith(prefix + "/"))
                   for prefix, item in batch.items()):
                failed.update(prefix for prefix, item in batch.items()
                              if item.is_directory and
                              (prefix == _ROOT_MARKER or path.startswith(prefix + "/")))
        return failed

    @staticmethod
    def _value(summary: Any, key: str) -> object:
        return summary.get(key, 0) if isinstance(summary, dict) else 0

    def health(self, project: str | None = None) -> dict[str, Any] | dict[str, dict[str, Any]]:
        with self._lock:
            names = [project] if project else list(self._states)
            result = {}
            for name in names:
                state = self._states.get(name)
                if state is None:
                    continue
                out = dict(state.health)
                native_alive = bool(self._observer and self._observer.is_alive())
                polling_alive = bool(self._polling_observer and self._polling_observer.is_alive())
                out.update({"pending_paths": len(state.dirty), "active": bool(state.task and not state.task.done()),
                            "degraded": state.degraded, "native_alive": native_alive,
                            "polling_alive": polling_alive, "native_observer_alive": native_alive,
                            "polling_observer_alive": polling_alive,
                            "attached_backends": ["native"] + (["polling"] if name in self._polling_watches else [])})
                result[name] = out
            return result.get(project, {}) if project else result

    def health_snapshot(self, project: str | None = None) -> dict[str, Any] | dict[str, dict[str, Any]]:
        return self.health(project)
