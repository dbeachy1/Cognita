"""The 4.0 retrieval core (DESIGN-4.0-vector-engine.md §4, D4.3/D4.7).

Owns what the retired engine did worst: index (parse → chunk → embed → store,
one transaction per document) and search (dense pgvector + lexical FTS run
concurrently → RRF fusion → cross-encoder rerank → MMR diversification →
adjacent-chunk expansion).

The search pipeline's constants and result shape deliberately mirror 3.x
(mcp_server/server.py query()) so M3 can serve the same MCP tool responses
byte-for-byte-shaped: RRF k=60 with alpha weighting, missing-leg rank 1000,
rerank pool = 3x max_results, MMR lambda 0.7, expansion window 1. The lexical
leg is Postgres FTS rather than BM25, so hits carry the same "bm25_rank" key
by wire-compat design (D4.4).

One shared write lock per project serializes indexing (the 3.x
write-serialization invariant, now guarding cheap DB transactions).
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import copy
import logging
import os
import re
import stat as stat_module
import time
from collections import OrderedDict
from dataclasses import replace
from collections.abc import Callable, Iterable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import Any

from . import gpu_host
from .chunking import DEFAULT_CHUNK_OVERLAP, DEFAULT_CHUNK_SIZE
from .deindexed import DeindexedPaths
from .document_roots import display_for
from .embed_telemetry import EmbedJob, current_job, embed_job
from .embeddings import Embedder, Reranker, release_to_os
from .estimate import DEFAULT_MIN_CHUNKS, estimate_job
from .gpu_probe import GpuProbe, NullProbe, batch_ceiling_gb
from .gpu_warm import WARM
from .gpu_warm import total_chunks as pool_chunks
from .index_scheduler import IndexScheduler
from .books.policy import EffectiveIndexPolicy, IndexDecision
from .parsing import (
    DEFAULT_POLICY,
    TIER_EMBEDDED,
    TIER_REGISTERED,
    ExtensionPolicy,
    ParsedDocument,
    _is_excluded,
    compute_doc_id,
    is_sync_conflict,
    iter_document_files,
    parse_file,
    partition_sync_conflicts,
)
from .store import ChunkHit, ChunkRecord, DocumentRecord, Store

log = logging.getLogger("cognita.retrieval")

# Parse ahead into a bounded queue and embed chunks across documents in
# windows. A prior per-document pipeline left devices idle: the reference
# walk had 587 documents and 15,630 chunks, usually 12-20 per call. Each
# document still writes in its own transaction; only embedding is batched.
# A window flushes after crossing the threshold and may overshoot by one
# document rather than hold half of its vectors for another window.
EMBED_WINDOW_CHUNKS = 512
EMBED_WINDOW_DOCS = 64
# How many parsed documents may wait in front of the embedder. Small on purpose:
# each item holds a document's full text and its chunks, so this is the pipeline's
# real memory ceiling. Two is enough to keep the embedder fed.
PARSE_QUEUE_DEPTH = 4

DEFAULT_RESULTS = 5
MAX_RESULTS = 20
RRF_K = 60
MISSING_RANK = 1000  # rank assigned to a chunk absent from one search leg
RERANK_MULTIPLIER = 3
MMR_LAMBDA = 0.7
EXPANSION_WINDOW = 1
DEFAULT_EXCLUDE_PATTERNS = ["backups"]  # backups/ is never indexed (house rule)


@dataclass
class _Parsed:
    """One file the producer has already read, parsed and chunked.

    Carries the skip inputs (`known`, `retier`) rather than the skip DECISION,
    because acting on them needs the store and the store stays on the consumer's
    task — the project write lock is owned by task, so letting a second task
    issue writes under it would put work outside the section that is supposed to
    serialize it.
    """

    source: str
    doc: ParsedDocument
    chunks: list
    known: Any | None
    retier: bool
    new_tier: str


@dataclass(frozen=True, slots=True)
class RootIdentity:
    """Stable facts for a watched documents root.

    The watcher owns when this is captured (at attachment), while the
    retrieval core owns checking it before a targeted operation can remove a
    row.  ``st_dev``/``st_ino`` are intentionally kept alongside the resolved
    path: a remounted or replaced directory can retain the same pathname.
    """

    resolved: str
    st_dev: int
    st_ino: int


@dataclass(frozen=True, slots=True)
class IndexFileOutcome:
    """Result for a parsed write that may be intentionally outside the index.

    The first two values retain existing internal unpack/index behavior;
    ``indexed`` distinguishes an effective-policy exclusion from an empty or
    unsupported parse (which remains ``None`` at the call boundary).
    """

    doc_id: str | None
    chunks: int
    indexed: bool
    exclusion_reason: str | None = None
    extracted_sha256: str | None = None
    provenance_current: bool | None = None

    def __iter__(self):
        yield self.doc_id
        yield self.chunks

    def __getitem__(self, index: int):
        if index == 0:
            return self.doc_id
        if index == 1:
            return self.chunks
        raise IndexError(index)

    def __bool__(self) -> bool:
        return self.indexed


class QueryCache:
    """3.x-parity search cache: TTL-LRU, invalidated on every write to the
    project. Entries are deep-copied on put AND get — the tool layer mutates
    results in place (snippeting, path absolutizing), and a mutated cache
    entry would silently corrupt every later hit."""

    def __init__(self, max_size: int = 100, ttl_s: float = 300.0):
        self.max_size = max_size
        self.ttl_s = ttl_s
        self._entries: OrderedDict[tuple, tuple[float, list]] = OrderedDict()
        self.hits = 0
        self.misses = 0

    def get(self, key: tuple) -> list | None:
        entry = self._entries.get(key)
        if entry is None or entry[0] < time.monotonic():
            self._entries.pop(key, None)
            self.misses += 1
            return None
        self._entries.move_to_end(key)
        self.hits += 1
        return copy.deepcopy(entry[1])

    def put(self, key: tuple, results: list) -> None:
        self._entries[key] = (time.monotonic() + self.ttl_s, copy.deepcopy(results))
        self._entries.move_to_end(key)
        while len(self._entries) > self.max_size:
            self._entries.popitem(last=False)

    def invalidate(self) -> None:
        self._entries.clear()

    def stats(self) -> dict[str, Any]:
        total = self.hits + self.misses
        return {
            "size": len(self._entries),
            "max_size": self.max_size,
            "hits": self.hits,
            "misses": self.misses,
            "hit_rate": round(self.hits / total, 4) if total else 0.0,
        }


class _ProjectWriteLock:
    """A per-project write lock one task may take more than once.

    The write section is claimed at two levels: RetrievalCore's own
    single-document paths take it per call, and the engine's bulk paths hold it
    across a whole operation (see RetrievalCore.write_lock). Without
    re-entrancy the outer claim would deadlock against the inner one, and the
    alternative — an unlocked twin of every write method — is two code paths
    that have to be kept in step forever.

    Ownership is by TASK, not by thread: asyncio runs one task at a time on the
    loop, so an inner acquire is only ever the same task that already holds it.
    A different task still blocks, which is the whole point.
    """

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._owner: asyncio.Task | None = None
        self._depth = 0

    async def __aenter__(self) -> _ProjectWriteLock:
        task = asyncio.current_task()
        if self._depth and task is not None and task is self._owner:
            self._depth += 1
            return self
        await self._lock.acquire()
        self._owner = task
        self._depth = 1
        return self

    async def acquire_within(self, timeout: float) -> bool:
        """Take the lock, or return False if `timeout` elapses first.

        Exists because `index_project` holds this for a WHOLE corpus walk. A
        mutating call arriving during a full rebuild used to wait for the entire
        remainder of it with nothing bounding the wait — the gateway's read
        timeout is None, so the caller hung until its own client gave up.
        Measured on a 150-document synthetic corpus: a concurrent remove_file
        waited 1.42s, which was exactly the rebuild's remaining duration. On a
        real corpus with a real embedder that is minutes.

        Reporting `busy` quickly is strictly better than hanging: the caller
        learns what is happening and can retry, and since 5.2.0 that retry is
        safe (`operation_id`). It also leaves the reindex's own guarantee alone —
        the walk still holds the lock for its whole duration, which is what makes
        it all-or-nothing. Loosening THAT is the change that would cost
        something real.
        """
        task = asyncio.current_task()
        if self._depth and task is not None and task is self._owner:
            self._depth += 1
            return True
        try:
            await asyncio.wait_for(self._lock.acquire(), timeout)
        except (TimeoutError, asyncio.TimeoutError):
            return False
        self._owner = task
        self._depth = 1
        return True

    async def release(self) -> None:
        """Counterpart to a successful acquire_within."""
        await self.__aexit__()

    async def __aexit__(self, *exc_info: object) -> bool:
        self._depth -= 1
        if self._depth <= 0:
            self._depth = 0
            self._owner = None
            self._lock.release()
        return False

    def locked(self) -> bool:
        return self._lock.locked()


class RetrievalCore:
    """Indexing + hybrid search over the Postgres store, all projects, one
    shared model instance (D4.6)."""

    def __init__(
        self,
        store: Store,
        embedder: Embedder,
        reranker: Reranker | None = None,
        *,
        chunk_size: int = DEFAULT_CHUNK_SIZE,
        chunk_overlap: int = DEFAULT_CHUNK_OVERLAP,
        exclude_patterns: list[str] | None = None,
        sync_conflict_patterns: list[str] | None = None,
        category_mappings: dict[str, str] | None = None,
        keyword_routes: dict[str, list[str]] | None = None,
        default_policy: ExtensionPolicy | None = None,
        gpu_min_chunks: int = DEFAULT_MIN_CHUNKS,
        gpu_config=None,
        gpu_probe: GpuProbe | None = None,
        gpu_batch_ceiling_gb: float | None = None,
        scheduler: IndexScheduler | None = None,
    ):
        self.store = store
        self.embedder = embedder
        self.reranker = reranker
        self.chunk_size = chunk_size
        self.chunk_overlap = chunk_overlap
        # DESIGN-6.0 §4.3: estimated chunks below which a job stays on the CPU,
        # because spin-up would dominate. Held here rather than read from config
        # at each walk so the estimator stays a pure function of its arguments.
        self.gpu_min_chunks = gpu_min_chunks
        # None means the GPU path is not even considered. The probe
        # defaults to Null so a core built without one never discovers
        # a device by accident.
        self.gpu_config = gpu_config
        self.gpu_probe = gpu_probe or NullProbe()
        # The host normally supplies one scheduler shared by all projects.
        # Keeping this seam optional preserves standalone/test construction and
        # lets older callers continue to use the proven direct GPU path until a
        # host has explicitly adopted the shared scheduler.
        self.scheduler = scheduler
        # §4.4: set by `bulk_gpu_job` while a bulk caller owns a pool and the
        # lease for a whole job. `_index_parsed` reuses it instead of starting
        # (and tearing down) its own per file. None whenever no bulk job is open,
        # which is every ordinary single write.
        self._bulk_pool = None
        # 6.2: cleared to False by any document whose pooled embed raised, so
        # `bulk_gpu_job` tears that pool down instead of parking it warm for the
        # next job to inherit the fault.
        self._bulk_pool_healthy = True
        # §7, corrected by measurement in §12.3: 3.62 GiB peak at batch 64, not
        # the 2.1 GB the arithmetic predicted. DERIVED from the configured batch
        # size rather than pinned — the literal only applies to batch 64, and a
        # gate sized for 64 admits a card that a batch-256 worker then OOMs on.
        # An explicit argument still wins, for tests and for a caller that has
        # measured its own hardware.
        self._gpu_batch_ceiling_override = gpu_batch_ceiling_gb
        self.exclude_patterns = (
            DEFAULT_EXCLUDE_PATTERNS if exclude_patterns is None else exclude_patterns
        )
        # None means "the built-in list"; an explicit [] switches the filter off
        # (the escape hatch for a corpus whose real filenames trip the globs).
        self.sync_conflict_patterns = sync_conflict_patterns
        # Conflict copies seen by the last walk, per project — surfaced through
        # get_index_stats so an exclusion is never silent (5.0 §10).
        self.sync_conflicts: dict[str, list[str]] = {}
        self.category_mappings = category_mappings or {}
        self.keyword_routes = keyword_routes or {}
        self.default_policy = default_policy or DEFAULT_POLICY
        self._policies: dict[str, ExtensionPolicy] = {}
        # Paths deliberately NOT indexed, per project (5.7). Registered by the
        # engine, which is the layer that knows each project's data_dir.
        self._deindexed: dict[str, DeindexedPaths] = {}
        self._effective_index_policy_provider: Callable[[str], EffectiveIndexPolicy] | None = None
        self._book_index_admission_provider: Callable[..., Any] | None = None
        self._book_index_provenance_recorder: Callable[..., Any] | None = None
        self._write_locks: dict[str, _ProjectWriteLock] = {}
        self._caches: dict[str, QueryCache] = {}
        # Set by the watcher when a project is attached.  A first targeted
        # call also records the identity for callers that do not have an
        # attachment lifecycle (notably focused tests and embedded hosts).
        self._root_identities: dict[str, RootIdentity] = {}

    # ---- the warm pool (6.2, DESIGN-6.2) ---------------------------------

    @property
    def gpu_idle_linger_s(self) -> float:
        """How long a finished pool stays warm. 0 is the 6.1 behavior.

        Read through `getattr` so a config object predating 6.2 — every fake in
        the suite, and any pinned deployment config — simply gets the old
        tear-down-per-job path rather than an AttributeError deep in a walk.
        """
        if self.gpu_config is None:
            return 0.0
        try:
            return max(0.0, float(getattr(self.gpu_config, "gpu_idle_linger_s", 0.0) or 0.0))
        except (TypeError, ValueError):
            return 0.0

    @property
    def gpu_warm_min_chunks(self) -> int:
        """Chunks below which even a WARM pool is not worth claiming (6.2.1).

        Measured, not tuned — see the config key for the arithmetic. Defaults to
        0 (no floor) when the config predates the key, which is what 6.2.0 did
        and what every fake config in the suite gets.
        """
        if self.gpu_config is None:
            return 0
        try:
            return max(0, int(getattr(self.gpu_config, "gpu_warm_min_chunks", 0) or 0))
        except (TypeError, ValueError):
            return 0

    async def _claim_warm_pool(self, job: EmbedJob | None = None):
        """A warm pool accepts work regardless of job size.

        `gpu_min_chunks` answers "is this job worth a ~3s cold start?" and it is
        the right question when the answer costs a start-up. It is the WRONG
        question when the cards are already loaded with the model. So every call
        site claims BEFORE it consults that estimate, and a claim that succeeds
        bypasses it entirely.

        A padded batch of 64 made a one-chunk round-trip take 1.83s (about
        0.0286s per padded sequence); at the shipped batch size of 4 it takes
        0.081s. The warm-pool minimum is therefore 0: once startup is paid,
        accept any job size. Callers apply the cold-start threshold separately.

        `to_thread` because `claim()` takes a lock the reaper holds across a
        blocking `pool.shutdown()` — see gpu_warm's module docstring.
        """
        if self.gpu_config is None or self.gpu_idle_linger_s <= 0:
            return None
        pool = await asyncio.to_thread(WARM.claim)
        if pool is not None:
            stamp = job if job is not None else current_job()
            if stamp is not None:
                stamp.decision = "gpu"
                stamp.pool = "warm"
        return pool

    async def _park_or_shutdown(self, pool, *, healthy: bool = True) -> None:
        """Hand a finished pool to the warm slot, or tear it down.

        🔴 **`healthy=False` means TEAR DOWN, and it is not a nicety.** Parking a
        pool whose embed raised would offer the next job — including a small one
        that would otherwise have used the CPU quite happily — a pool that has
        already demonstrated it cannot embed. The failure would then repeat per
        job for the whole linger window instead of being confined to the job
        that hit it.
        """
        if pool is None:
            return
        if not healthy or self.gpu_idle_linger_s <= 0:
            await asyncio.to_thread(pool.shutdown)
            return
        # park() shuts the pool down itself when it refuses to park it, so the
        # pool is disposed of exactly once either way.
        await asyncio.to_thread(WARM.park, pool, self.gpu_idle_linger_s)

    def _write_lock(self, project: str) -> _ProjectWriteLock:
        return self._write_locks.setdefault(project, _ProjectWriteLock())

    def write_lock(self, project: str) -> _ProjectWriteLock:
        """The project write section, for a caller that needs to hold it across
        a whole multi-step operation.

        5.0.2. The single-document paths below take this per call, which is
        enough while the only writers are single documents. It is NOT enough for
        a bulk operation: remove_directory de-indexes N documents and then
        deletes N files, and between any two of those awaits the watcher's
        debounced whole-tree sync (index_project, which takes this same lock)
        can run, walk a tree where the files still exist, and index a row this
        call just deleted. The caller then gets documents_removed: 3 and an
        immediate list_documents showing one of them still there — observed live
        on the 2026-08-29 self-test run. Holding this for the whole operation is
        what makes the result and the state agree.
        """
        return self._write_lock(project)

    # ---- per-project extension policy (D4.4-2) --------------------------
    # One core serves every project, so the tier map has to be looked up by
    # project rather than held as a single instance attribute. Registered once
    # at startup; every index path resolves through policy_for().

    def set_policy(self, project: str, policy: ExtensionPolicy) -> None:
        self._policies[project] = policy

    @property
    def gpu_batch_ceiling_gb(self) -> float:
        """The gate's VRAM ceiling, for THIS deployment's batch size (§7)."""
        if self._gpu_batch_ceiling_override is not None:
            return self._gpu_batch_ceiling_override
        batch = getattr(self.gpu_config, "gpu_batch_size", 64) or 64
        return batch_ceiling_gb(batch)

    def policy_for(self, project: str) -> ExtensionPolicy:
        return self._policies.get(project, self.default_policy)

    def query_cache(self, project: str) -> QueryCache:
        return self._caches.setdefault(project, QueryCache())

    @staticmethod
    def capture_root_identity(documents_dir: Path) -> RootIdentity:
        """Capture the resolved path and filesystem identity of a watch root.

        This is the watcher-facing attachment seam.  It performs no indexing
        and raises when the root is not a readable directory, allowing the
        watcher to leave a project unattached instead of treating an absent
        mount as an empty corpus.
        """
        root = Path(documents_dir)
        resolved = root.resolve(strict=True)
        stat = resolved.stat()
        if not resolved.is_dir() or not os.access(resolved, os.R_OK):
            raise NotADirectoryError(f"documents root is not readable: {root}")
        return RootIdentity(str(resolved), int(stat.st_dev), int(stat.st_ino))

    def attach_root(self, project: str, documents_dir: Path) -> RootIdentity:
        """Record the root identity observed when a watcher attaches."""
        identity = self.capture_root_identity(documents_dir)
        self._root_identities[project] = identity
        return identity

    def detach_root(self, project: str) -> None:
        """Forget an attachment identity when a project is unwatched."""
        self._root_identities.pop(project, None)

    @staticmethod
    def _bounded_error(exc: BaseException) -> str:
        text = " ".join(str(exc).split()) or type(exc).__name__
        return text[:500]

    @classmethod
    def _record_reconcile_failure(
        cls, summary: dict[str, Any], path: str, exc: BaseException | str
    ) -> None:
        """Record watcher-compatible path retry plus bounded diagnostics."""
        error = cls._bounded_error(exc) if isinstance(exc, BaseException) else str(exc)[:500]
        summary["retryable_failures"].append(path)
        summary["failures"].append({"path": path, "error": error, "retryable": True})

    @staticmethod
    def _relative_dirty_path(path: str | Path, documents_dir: Path) -> str:
        """Normalize a watcher path lexically, rejecting root escapes."""
        raw = str(path).replace("\\", "/")
        candidate = Path(raw)
        root = Path(documents_dir)
        # ``Path`` follows the host OS.  On POSIX it treats a Windows
        # drive-relative spelling (``C:outside.md``) as an ordinary relative
        # path, so check the foreign syntax explicitly before joining it to
        # the documents root.
        if PureWindowsPath(raw).drive and not candidate.is_absolute():
            raise ValueError(f"dirty path has a drive-relative anchor: {path!r}")
        if candidate.is_absolute():
            candidate = candidate.resolve(strict=False)
            try:
                rel = candidate.relative_to(root.resolve(strict=False))
            except ValueError as exc:
                raise ValueError(f"dirty path escapes documents root: {path!r}") from exc
        else:
            # ``Path('.')`` is the root marker.  Purely lexical normalization
            # keeps a vanished directory representable for deletion planning.
            rel = Path(raw)
        rel_text = rel.as_posix().strip("/")
        if not rel_text or rel_text == ".":
            return "."
        parts = [part for part in rel_text.split("/") if part not in ("", ".")]
        if any(part == ".." for part in parts):
            raise ValueError(f"dirty path escapes documents root: {path!r}")
        return "/".join(parts)

    @staticmethod
    def _is_descendant(source: str, prefix: str) -> bool:
        return prefix == "." or source == prefix or source.startswith(prefix + "/")

    @staticmethod
    def _stat_identity(path: Path) -> tuple[int, int, int, int]:
        stat = path.stat()
        return int(stat.st_dev), int(stat.st_ino), int(stat.st_size), int(stat.st_mtime_ns)

    def _root_is_safe(
        self, project: str, documents_dir: Path, expected: RootIdentity | None
    ) -> RootIdentity:
        """Verify the attached root before any targeted destructive action."""
        current = self.capture_root_identity(documents_dir)
        attached = expected or self._root_identities.get(project)
        if attached is None:
            self._root_identities[project] = current
            return current
        if current != attached:
            raise RuntimeError(
                f"documents root identity changed for project {project!r}; "
                "reattach or run a safe full smart reconciliation"
            )
        return current

    async def reconcile_paths(
        self,
        project: str,
        documents_dir: Path,
        dirty_paths: Iterable[str | Path],
        *,
        root_identity: RootIdentity | None = None,
        diagnostic_redacted: bool = False,
        source_is_safe: Callable[[], bool] | None = None,
        walk: str = "watcher",
    ) -> dict[str, Any]:
        """Reconcile only watcher-dirty paths against the current filesystem.

        Public-internal watcher contract (async):
        ``await core.reconcile_paths(project, documents_dir, dirty_paths,`
        ``root_identity=core.attach_root(project, documents_dir))``.
        ``dirty_paths`` contains normalized relative paths (absolute paths are
        accepted for defensive compatibility).  The result has integer keys
        ``indexed``, ``metadata_refreshed``, ``skipped``, ``removed``,
        ``failed``, and ``expanded_paths`` plus ``failures`` — a list of
        ``{"path": str, "error": str, "retryable": True}`` records.

        All planning and writes occur under one project write lock.  A failed
        parse/read/embedding/store operation leaves the previous row intact
        and is returned as a retryable path.  Root failures fail closed: no
        deletion is attempted.
        """
        summary: dict[str, Any] = {
            "indexed": 0,
            "metadata_refreshed": 0,
            "skipped": 0,
            "removed": 0,
            "failed": 0,
            "expanded_paths": 0,
            "failures": [],
            # WatcherManager consumes paths, while ``failures`` retains the
            # bounded per-path diagnostics useful to callers and health views.
            "retryable_failures": [],
        }
        raw_dirty_paths = list(dirty_paths)
        try:
            normalized = sorted({
                self._relative_dirty_path(path, documents_dir)
                for path in raw_dirty_paths
            })
        except Exception as exc:
            summary["failed"] = 1
            self._record_reconcile_failure(
                summary, str(raw_dirty_paths[0] if raw_dirty_paths else "."), exc
            )
            return summary
        if not normalized:
            return summary

        # 15.0.3: the watcher is how most everyday edits reach the index, and it
        # wrote no `embed.done` at all: `_index_parsed` ran with no job open, so
        # what the watcher cost, and on which device, was invisible (Maia's
        # NVIDIA proof, 2026-09-30).  One job per watcher batch; the summary is
        # written only when the batch embedded something (an embedder recorded
        # a batch, even a failed one), so metadata-only batches (touches,
        # OneDrive mtime rewrites) stay quiet.  `walk` names the caller: the
        # Workspace bridge's copies say so instead of looking like the watcher.
        completed = False
        with embed_job(project, walk=walk) as job:
            try:
                async with self._write_lock(project):
                    source_changed = False

                    def source_is_current() -> bool:
                        nonlocal source_changed
                        if source_changed:
                            return False
                        if source_is_safe is None:
                            return True
                        try:
                            source_changed = not source_is_safe()
                        except Exception:
                            source_changed = True
                        return not source_changed

                    if not source_is_current():
                        for source in normalized:
                            self._record_reconcile_failure(summary, source, "source_unavailable")
                        summary["failed"] = len(normalized)
                        return summary
                    try:
                        self._root_is_safe(project, documents_dir, root_identity)
                    except Exception as exc:
                        error = self._bounded_error(exc)
                        for source in normalized:
                            self._record_reconcile_failure(summary, source, error)
                        summary["failed"] = len(normalized)
                        if diagnostic_redacted:
                            log.error(
                                "Targeted reconciliation root safety failure project=%s reason=%s",
                                project, type(exc).__name__,
                            )
                        else:
                            log.error("Targeted reconciliation root safety failure for %s: %s", project, error)
                        return summary

                    policy = self.policy_for(project)
                    effective_policy = self.effective_index_policy_for(project)
                    existing = await self.store.list_sources(project)
                    # Collapse lexical descendants before touching disk.  A directory
                    # event may be the only signal for a populated copy, and a vanished
                    # directory is still discoverable from indexed sources.
                    prefixes: list[str] = []
                    direct: list[str] = []
                    for source in normalized:
                        path = documents_dir if source == "." else documents_dir / source
                        is_dir = False
                        try:
                            is_dir = path.is_dir()
                        except OSError:
                            pass
                        # Equality means this is an already-indexed file, not a
                        # directory prefix.  Only a strictly nested indexed source
                        # proves that a vanished path may have been a directory.
                        has_indexed_descendants = any(
                            indexed != source and self._is_descendant(indexed, source)
                            for indexed in existing
                        )
                        if is_dir or has_indexed_descendants or source == ".":
                            prefixes.append(source)
                            summary["expanded_paths"] += 1
                        else:
                            direct.append(source)
                    prefixes = [p for p in prefixes if not any(
                        p != ancestor and self._is_descendant(p, ancestor)
                        for ancestor in prefixes
                    )]

                    current_files: dict[str, Path] = {}
                    failed_prefixes: set[str] = set()
                    for prefix in prefixes:
                        root = documents_dir if prefix == "." else documents_dir / prefix
                        try:
                            root_facts = root.stat()
                        except FileNotFoundError:
                            continue
                        except OSError as exc:
                            summary["failed"] += 1
                            self._record_reconcile_failure(summary, prefix, exc)
                            failed_prefixes.add(prefix)
                            continue
                        if not stat_module.S_ISDIR(root_facts.st_mode):
                            continue
                        try:
                            files = await asyncio.to_thread(
                                iter_document_files, root, self.exclude_patterns, policy,
                                raise_on_error=True,
                            )
                        except Exception as exc:
                            summary["failed"] += 1
                            self._record_reconcile_failure(summary, prefix, exc)
                            failed_prefixes.add(prefix)
                            continue
                        for file_path in files:
                            try:
                                source = file_path.relative_to(documents_dir).as_posix()
                            except ValueError:
                                continue
                            if _is_excluded(Path(source), self.exclude_patterns):
                                continue
                            current_files[source] = file_path

                    for source in direct:
                        path = documents_dir / source
                        try:
                            facts = path.stat()
                        except FileNotFoundError:
                            continue
                        except OSError as exc:
                            summary["failed"] += 1
                            self._record_reconcile_failure(summary, source, exc)
                            failed_prefixes.add(source)
                            continue
                        if stat_module.S_ISREG(facts.st_mode):
                            current_files[source] = path

                    # Sources below every dirty prefix are candidates for retirement,
                    # including unsupported/excluded files that used to be indexed.
                    removal_candidates = {
                        source for source in existing
                        if any(self._is_descendant(source, prefix) for prefix in prefixes)
                        and not any(self._is_descendant(source, prefix)
                                    for prefix in failed_prefixes)
                    }
                    removal_candidates.update(
                        source for source in direct
                        if source in existing and source not in failed_prefixes
                    )
                    forced_removals: set[str] = set()
                    suppressed = self.deindexed_paths(project)
                    # A file that is still present can nevertheless have become
                    # unsupported, excluded, conflicted, or explicitly de-indexed.
                    # Such a row is a policy retirement, not an absence, so the
                    # deletion pass must not let ``Path.exists()`` protect it.
                    for source in removal_candidates:
                        filepath = documents_dir / source
                        if (policy.tier_for(filepath.suffix) is None
                            or source in suppressed
                            or _is_excluded(Path(source), self.exclude_patterns)
                            or is_sync_conflict(filepath.name, self.sync_conflict_patterns)
                            or not self._policy_allows_source(
                                effective_policy, source,
                                globally_eligible=policy.tier_for(filepath.suffix) is not None,
                            )
                        ):
                            forced_removals.add(source)
                    changed = False

                    for source, filepath in sorted(current_files.items()):
                        known = existing.get(source)
                        tier = policy.tier_for(filepath.suffix)
                        if tier is None or source in suppressed \
                                or _is_excluded(Path(source), self.exclude_patterns) \
                                or is_sync_conflict(filepath.name, self.sync_conflict_patterns):
                            if known is not None:
                                removal_candidates.add(source)
                                forced_removals.add(source)
                            continue
                        if not self._policy_allows_source(
                            effective_policy, source, globally_eligible=True,
                        ):
                            if known is not None:
                                removal_candidates.add(source)
                                forced_removals.add(source)
                            continue
                        removal_candidates.discard(source)
                        try:
                            # The immediate stat is both the cheap skip and the first
                            # half of the change-during-read guard.
                            if known is not None and known.tier == tier \
                                    and self._stat_matches(filepath, known):
                                summary["skipped"] += 1
                                continue
                            before = await asyncio.to_thread(self._stat_identity, filepath)
                            doc = await asyncio.to_thread(self._parse, filepath, documents_dir, policy)
                            after = await asyncio.to_thread(self._stat_identity, filepath)
                            if before != after:
                                raise RuntimeError("file changed during reconciliation read")
                            if doc is None:
                                self._root_is_safe(project, documents_dir, root_identity)
                                if not source_is_current():
                                    raise RuntimeError("source_unavailable")
                                if known is not None and await self.store.delete_document(project, source):
                                    summary["removed"] += 1
                                    changed = True
                                continue
                            if known is not None:
                                doc.category = known.category
                            if known is not None and known.tier == tier \
                                    and known.content_hash == doc.content_hash:
                                self._root_is_safe(project, documents_dir, root_identity)
                                if not source_is_current():
                                    raise RuntimeError("source_unavailable")
                                await self.store.touch_document(
                                    project, source, doc.file_mtime, doc.file_size
                                )
                                summary["metadata_refreshed"] += 1
                                changed = True
                                continue
                            self._root_is_safe(project, documents_dir, root_identity)
                            if not source_is_current():
                                raise RuntimeError("source_unavailable")
                            await self._index_parsed(project, doc)
                            summary["indexed"] += 1
                            changed = True
                        except FileNotFoundError:
                            # Vanishing after planning is a deletion candidate, handled
                            # below only if the root remains safe.
                            if known is not None:
                                removal_candidates.add(source)
                        except Exception as exc:
                            summary["failed"] += 1
                            self._record_reconcile_failure(summary, source, exc)

                    # Verify the root again immediately before destructive operations.
                    # A remount during parsing must preserve all old searchable state.
                    try:
                        self._root_is_safe(project, documents_dir, root_identity)
                    except Exception as exc:
                        error = self._bounded_error(exc)
                        for source in sorted(removal_candidates):
                            summary["failed"] += 1
                            self._record_reconcile_failure(summary, source, error)
                        removal_candidates.clear()
                        if diagnostic_redacted:
                            log.error(
                                "Targeted reconciliation removal safety failure project=%s reason=%s",
                                project, type(exc).__name__,
                            )
                        else:
                            log.error("Targeted reconciliation removal safety failure for %s: %s", project, error)

                    if not source_is_current():
                        removal_candidates.clear()
                        if diagnostic_redacted:
                            log.error(
                                "Targeted reconciliation removal safety failure project=%s "
                                "reason=source_unavailable",
                                project,
                            )

                    for source in sorted(removal_candidates):
                        filepath = documents_dir / source
                        # A source can be restored while this batch is parsing.  Only a
                        # proven absence is a deletion; existing files get another pass.
                        if source not in forced_removals:
                            try:
                                filepath.stat()
                            except FileNotFoundError:
                                pass
                            except OSError as exc:
                                summary["failed"] += 1
                                self._record_reconcile_failure(summary, source, exc)
                                continue
                        try:
                            if not source_is_current():
                                break
                            if await self.store.delete_document(project, source):
                                summary["removed"] += 1
                                changed = True
                        except Exception as exc:
                            summary["failed"] += 1
                            self._record_reconcile_failure(summary, source, exc)
                    if changed:
                        self.query_cache(project).invalidate()
                completed = True
            finally:
                if job.devices:
                    job.done(
                        files=len(normalized), indexed=summary["indexed"],
                        metadata_refreshed=summary["metadata_refreshed"],
                        removed=summary["removed"], errors=summary["failed"],
                        outcome=("aborted" if not completed
                                 else "ok" if not summary["failed"] else "partial"),
                    )
        return summary

    # ---- de-indexed paths (5.7) -----------------------------------------
    # Resolved by project for the same reason the tier map is: one core serves
    # every project, and the list lives in each project's own data_dir.

    def set_deindexed(self, project: str, paths: DeindexedPaths) -> None:
        self._deindexed[project] = paths

    def set_effective_index_policy_provider(
        self, provider: Callable[[str], EffectiveIndexPolicy] | None,
    ) -> None:
        """Install the host-owned durable policy resolver for this core.

        The host resolves project state and validated book configuration; the
        retrieval layer only applies the resulting pure decision. Standalone
        cores keep their legacy behavior when no provider is installed.
        """
        self._effective_index_policy_provider = provider

    def effective_index_policy_for(self, project: str) -> EffectiveIndexPolicy | None:
        provider = self._effective_index_policy_provider
        return provider(project) if provider is not None else None

    def set_book_index_admission_provider(self, provider: Callable[..., Any] | None) -> None:
        """Install the host's current-source/doc-id approval admission check."""
        self._book_index_admission_provider = provider

    def set_book_index_provenance_recorder(self, provider: Callable[..., Any] | None) -> None:
        """Install the host-owned writer for source-bound role provenance."""
        self._book_index_provenance_recorder = provider

    @staticmethod
    def _policy_allows_source(
        policy: EffectiveIndexPolicy | None, source: str, *, globally_eligible: bool = True,
    ) -> bool:
        return policy is None or policy.decision(
            source, globally_eligible=globally_eligible,
        ).indexed

    def index_decision_for(self, project: str, source: str) -> IndexDecision | None:
        """Return the live effective decision, or ``None`` for legacy cores."""
        policy = self.effective_index_policy_for(project)
        if policy is None:
            return None
        extension_policy = self.policy_for(project)
        return policy.decision(
            source,
            globally_eligible=extension_policy.tier_for(Path(source).suffix) is not None,
        )

    async def _effective_indexed_sources(
        self, project: str, retrieval_profile: str | None = None,
    ) -> tuple[list[str] | None, list[str] | None, dict[str, dict[str, Any]]]:
        """Return policy-admitted indexed sources for SQL candidate filtering.

        Every SQL search leg must apply exclusions before LIMIT, otherwise
        excluded high-ranked rows can consume the whole candidate budget. The
        final publication still rechecks the live policy because folder rules
        can change while embedding/reranking runs.
        """
        effective = self.effective_index_policy_for(project)
        if effective is None:
            return None, None, {}
        extension_policy = self.policy_for(project)
        source_info = await self.store.list_sources(project)
        admitted = {
            source: info for source, info in source_info.items()
            if self._policy_allows_source(
                effective, source,
                globally_eligible=extension_policy.tier_for(Path(source).suffix) is not None,
            )
        }
        if getattr(effective, "layout", None) is not None:
            profile = retrieval_profile or "canon"
            provider = self._book_index_admission_provider
            if provider is None:
                # An enabled layout without provenance verification is not a
                # path-only allow decision. Fail closed until the host wires it.
                return [], [], {}
            book_sources = tuple(
                replace(info, source=source)
                if getattr(info, "source", None) != source else info
                for source, info in admitted.items()
            )
            book_ids = provider(project, book_sources, profile)
            if inspect.isawaitable(book_ids):
                book_ids = await book_ids
            allowed_ids: set[str] = set()
            labels: dict[str, dict[str, Any]] = {}
            infos_by_id = {info.doc_id: (source, info) for source, info in admitted.items()}
            if isinstance(book_ids, dict):
                for key, value in book_ids.items():
                    if not isinstance(key, str) or not isinstance(value, dict):
                        continue
                    if set(value) != {
                        "source_path", "role", "chapter_id", "editorial_status",
                        "summary_freshness", "provenance",
                    }:
                        continue
                    source_info = infos_by_id.get(key)
                    provenance = value["provenance"]
                    if source_info is None or not all(hasattr(provenance, name) for name in (
                        "source_path", "doc_id", "extracted_sha256", "raw_sha256",
                        "extraction_version", "layout_sha256",
                    )):
                        continue
                    source, info = source_info
                    if (
                        value["source_path"] != source
                        or provenance.source_path != source
                        or provenance.doc_id != key
                        or provenance.extracted_sha256 != info.content_hash
                        or value["role"] != provenance.role
                        or value["chapter_id"] != provenance.chapter_id
                        or value["editorial_status"] not in (None, "draft", "approved")
                        or value["summary_freshness"] not in (
                            "fresh", "stale", "unapproved", "not_applicable",
                        )
                    ):
                        continue
                    allowed_ids.add(key)
                    labels[key] = value
            else:
                # A set of IDs is insufficient to label an editing result or
                # prove which provenance record admitted it. Fail closed.
                return [], [], {}
            admitted = {
                source: info for source, info in admitted.items()
                if info.doc_id in allowed_ids
            }
            return list(admitted), [info.doc_id for info in admitted.values()], labels
        return list(admitted), None, {}

    def deindexed_for(self, project: str) -> DeindexedPaths | None:
        return self._deindexed.get(project)

    def deindexed_paths(self, project: str) -> set[str]:
        """The project's suppressed sources — empty when none is registered."""
        registered = self._deindexed.get(project)
        return registered.paths() if registered is not None else set()

    def readmit(self, project: str, source: str) -> bool:
        """Drop `source` from the de-index list. True if it had been listed.

        Every explicit write through the tool surface calls this, and it is a
        CORRECTNESS requirement rather than a courtesy: indexing a file the walk
        is told to skip produces a row that the very next index_project sweeps
        away, because a suppressed file never reaches live_sources. Without this
        an add_document onto a de-indexed path would look like it worked and
        then quietly lose its row.
        """
        registered = self._deindexed.get(project)
        if registered is None:
            return False
        if registered.discard(source):
            log.info("Re-admitted %s/%s to the index (explicit write)", project, source)
            return True
        return False

    # ------------------------------------------------------------------
    # Indexing
    # ------------------------------------------------------------------

    async def index_project(
        self,
        project: str,
        documents_dir: Path,
        *,
        force: bool = False,
        progress: Callable[[dict[str, Any]], None] | None = None,
        before_removal: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        """(Re)index every supported file under documents_dir.

        Change detection without churn: unchanged mtime+size skips the file
        without parsing; unchanged content hash (a touched file) refreshes
        metadata without re-embedding. Every actual write is one transaction.
        Files that vanished from disk lose their rows at the end.
        """
        # DESIGN-6.0 §14: the walk is one embed JOB, and this is the seam its
        # accounting hangs off — the same "bulk work is over" boundary that
        # already owns release_to_os(), and the one §8.2 will later hang the GPU
        # worker teardown off. The job is opened here rather than inside the
        # lock so it survives every exit path, including the exceptional one.
        with embed_job(project, walk="project") as job:
            try:
                return await self._index_project_locked(
                    project, documents_dir, force=force, progress=progress, job=job,
                    before_removal=before_removal,
                )
            finally:
                # done() is idempotent and the walk normally emits it itself,
                # with the real counts. This is the safety net: a walk that
                # raised still says how much it embedded before it died, which
                # is the only place that information exists.
                job.done(outcome="aborted")
                # 5.8: a whole-corpus walk is the single biggest ratchet on the
                # allocator arena — the per-document trim in Embedder.embed()
                # keeps the steady state honest, but a full_rebuild over hundreds
                # of documents leaves the largest high-water mark of all. Trim
                # once more when the walk is over, so RSS comes back down instead
                # of parking at the peak for the life of the process.
                release_to_os()

    async def _index_project_locked(
        self,
        project: str,
        documents_dir: Path,
        *,
        force: bool = False,
        progress: Callable[[dict[str, Any]], None] | None = None,
        job: EmbedJob | None = None,
        before_removal: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        async with self._write_lock(project):
            await self.store.ensure_project(project)
            policy = self.policy_for(project)
            files = await asyncio.to_thread(
                iter_document_files, documents_dir, self.exclude_patterns, policy
            )
            walk_found_files = bool(files)
            effective_policy = self.effective_index_policy_for(project)
            policy_excluded = 0
            if effective_policy is not None:
                eligible_files: list[Path] = []
                for filepath in files:
                    source = filepath.relative_to(documents_dir).as_posix()
                    if self._policy_allows_source(effective_policy, source):
                        eligible_files.append(filepath)
                    else:
                        policy_excluded += 1
                files = eligible_files
            # 5.0 §10: drop cloud-sync conflict copies BEFORE anything indexes
            # them, and remember which ones so get_index_stats can say so out
            # loud. Indexing a conflict copy is worse than not indexing it: the
            # corpus then holds two near-identical documents and every search,
            # glob and pack build silently sees both.
            files, conflicts = partition_sync_conflicts(files, self.sync_conflict_patterns)
            self.sync_conflicts[project] = [
                c.relative_to(documents_dir).as_posix() for c in conflicts
            ]
            # Whether the WALK found anything, recorded before any deliberate
            # filtering. The removal-sweep guard below turns on this and not on
            # len(files): a project whose every file is suppressed or a conflict
            # copy is a correct empty corpus, while a walk that found nothing at
            # all is an unmounted documents_dir.
            walk_found_files = walk_found_files or bool(conflicts)
            # Set when the parse-ahead task raises or is canceled: the live-
            # document set is then incomplete and the removal sweep must not run.
            producer_failed: str = ""
            # 5.7: paths a caller de-indexed on purpose (remove_document with
            # delete_file=false). Dropping them here — before the loop, so they
            # never reach live_sources — is what makes that removal DURABLE:
            # they are not re-indexed, and any row left over from before the
            # suppression is swept away by delete_documents_not_in, exactly as
            # if the file had been deleted from disk.
            suppressed = self.deindexed_paths(project)
            deindexed_skipped = 0
            if suppressed:
                kept = []
                for filepath in files:
                    if filepath.relative_to(documents_dir).as_posix() in suppressed:
                        deindexed_skipped += 1
                    else:
                        kept.append(filepath)
                if deindexed_skipped:
                    log.info(
                        "Skipping %d de-indexed file(s) in %s (remove_document "
                        "delete_file=false); get_index_stats lists them",
                        deindexed_skipped, project,
                    )
                files = kept
            if before_removal is None:
                for conflict in self.sync_conflicts[project]:
                    log.warning(
                        "Skipping cloud-sync conflict copy (not indexed): %s/%s",
                        project, conflict,
                    )
            elif conflicts:
                log.warning(
                    "Skipping cloud-sync conflict copies project=%s count=%d",
                    project, len(conflicts),
                )
            existing = await self.store.list_sources(project)
            # §4: estimate the work ONCE, up front, over the whole job — before
            # anything is parsed, because parsing the corpus to find out how
            # much parsing to do defeats the purpose. `existing` has to be
            # fetched first: the estimate is over the files that will actually
            # be embedded, and most walks skip nearly all of them.
            estimate = await asyncio.to_thread(
                estimate_job,
                files,
                documents_dir,
                chunk_size=self.chunk_size,
                chunk_overlap=self.chunk_overlap,
                known=existing,
                force=force,
                suppressed=suppressed,
                policy=policy,
                threshold=self.gpu_min_chunks,
            )
            if job is not None:
                job.est_chunks = estimate.est_chunks
                job.threshold = estimate.threshold
                job.binary_formats = estimate.binary_formats
                # 🔴 `decision` is set here, from the ESTIMATE, and corrected
                # below if a pool could not actually be started. The plan line
                # itself is emitted AFTER that, because a plan line reporting
                # decision=cpu on a walk that then ran on two GPUs is worse than
                # no plan line at all — it is the field the whole record is
                # keyed on, and it was wrong on the first live GPU run.
                job.decision = "gpu" if estimate.use_gpu else "cpu"
            summary = {
                "total_files": len(files),
                "indexed": 0,
                "skipped": 0,
                "removed": 0,
                "errors": [],
                # 4.4 migration accounting. Moving an extension from embedded to
                # registered INVALIDATES that document's existing chunks and
                # vectors, so the first reindex after upgrade is destructive, not
                # additive. A silent purge is exactly the bug this feature exists
                # to prevent — a stale vector still answering semantic queries for
                # a document that is supposed to have none — so the counts are
                # reported. Non-zero on the first run after upgrade, zero after.
                "tier_changed": 0,
                "chunks_purged": 0,
                "sync_conflicts_skipped": len(conflicts),
                "deindexed_skipped": deindexed_skipped,
                "policy_excluded_skipped": policy_excluded,
            }
            scheduler_job = None
            if self.scheduler is not None:
                scheduler_job = self.scheduler.open_job(
                    project,
                    "walk",
                    estimated_chunks=estimate.est_chunks,
                    binary_formats=estimate.binary_formats,
                )
            live_sources: list[str] = []
            # Parsing runs ahead of embedding on its own task; the queue is the
            # backpressure. See EMBED_WINDOW_CHUNKS above for why.
            queue: asyncio.Queue = asyncio.Queue(maxsize=PARSE_QUEUE_DEPTH)
            producer = asyncio.create_task(
                self._parse_ahead(queue, files, documents_dir, policy, existing, force)
            )
            window: list[_Parsed] = []
            window_chunks = 0
            processed = 0

            # Start hardware only when parsed chunks need embedding. A file
            # size estimate once predicted 387 chunks for a three-chunk walk,
            # holding the project write lock through 60-135 s of startup.
            # For a warm pool, baseline is its prior lifetime chunk count;
            # teardown compares the delta from this walk.
            pool_holder: dict = {"pool": None, "started": False, "baseline": 0,
                                 "healthy": True}
            # Real chunks the walk has actually put in front of an embedder.
            # The ESTIMATE decided a GPU is worth considering; this decides it is
            # worth PAYING FOR, which is a different question and only answerable
            # once the parsing has happened.
            real_chunks_seen = 0

            async def ensure_pool(pending_chunks: int):
                """Start the GPU pool once the REAL work justifies it."""
                nonlocal real_chunks_seen
                if self.scheduler is not None:
                    # GPU lifecycle is owned by the shared scheduler.  Do not
                    # create a legacy per-job pool just because this walk has
                    # an estimate; that would reintroduce the contention bug.
                    return None
                if pool_holder["started"]:
                    return pool_holder["pool"]
                if self.gpu_config is None:
                    pool_holder["started"] = True
                    return None
                # Accumulated FIRST now (6.2.1). It used to sit below the
                # `use_gpu` gate, which was fine while the only question was
                # whether to pay for a cold start; the warm claim needs the same
                # running total on a walk the estimator wrote off.
                real_chunks_seen += pending_chunks
                # 🔴 6.2, AND IT IS TESTED BEFORE THE ESTIMATE ON PURPOSE. Every
                # gate below — `use_gpu`, `gpu_min_chunks` — exists to answer ONE
                # question: is this walk worth a cold start? If a previous job
                # left cards up with the model loaded, that question is not being
                # asked, and `gpu_min_chunks` (20) has no business gating work
                # that has no start-up to amortize.
                #
                # 6.2.1 puts the much lower MEASURED floor in its place: a warm
                # claim still costs ~1.8s of round-trip, so it is worth making
                # only once there are enough chunks to repay it.
                if real_chunks_seen >= self.gpu_warm_min_chunks:
                    warm = await self._claim_warm_pool(job)
                    if warm is not None:
                        pool_holder["started"] = True
                        pool_holder["pool"] = warm
                        pool_holder["baseline"] = pool_chunks(warm)
                        return warm
                if not estimate.use_gpu:
                    # 🔴 Latch ONLY when nothing can change the answer later.
                    # With lingering on, a pool may be parked by another job at
                    # any point in this walk, and the accumulated chunk count is
                    # still rising — so a walk the estimator wrote off must keep
                    # asking. Latching here (which is what 6.2.0 did, inherited
                    # from 6.1 where it was correct) makes a long CPU walk unable
                    # to ever pick up a card that became free.
                    if self.gpu_idle_linger_s <= 0:
                        pool_holder["started"] = True
                    return None
                # 🔴 THE ESTIMATE IS A LOWER BOUND FROM FILE SIZES AND IT CANNOT
                # SEE THAT A FILE IS BYTE-IDENTICAL. 5.20.0 moved the spin-up
                # from "up front" to "on the first window", which fixed the
                # zero-work case and left the case that actually happened: an
                # application rewrites a large file with the same bytes, the
                # estimator counts all 687 of its chunks, the producer parses it
                # and then only touches its stat row — and one genuinely-changed
                # note flushes a window of 2 chunks, which paid the full ~33s
                # spin-up under the project write lock, refusing connector
                # writes for the duration. "Real work" meant one chunk.
                #
                # Accumulating instead means a small walk never starts a pool at
                # all, and a large one starts after ~20 chunks of CPU — a
                # fraction of a second — and runs the remaining 99% on the GPU.
                if real_chunks_seen < self.gpu_min_chunks:
                    return None
                pool_holder["started"] = True
                try:
                    return await start_and_prove()
                except Exception:
                    # 🔴 §10: THE WALK ALWAYS COMPLETES. Every GPU failure is an
                    # ordinary outcome that costs time, never the walk. Without
                    # this, a `check_canary` that raised propagated out of
                    # `flush()` — and out of `index_project` when it was the
                    # final flush — so a fault in the OPTIONAL accelerator failed
                    # an index that the CPU path could have finished. The
                    # single-document twin (`_embed_on_gpu`) has always had this
                    # guard; the walk did not.
                    log.warning(
                        "GPU pool startup failed for project %r; the walk "
                        "continues on the CPU", project, exc_info=True,
                    )
                    pool = pool_holder["pool"]
                    if pool is not None:
                        await asyncio.to_thread(pool.shutdown)
                        pool_holder["pool"] = None
                    return None

            async def start_and_prove():
                pool = await asyncio.to_thread(
                    gpu_host.start_pool, self.gpu_config, self.gpu_probe,
                    f"{project}:{id(job)}", self.gpu_batch_ceiling_gb,
                )
                # 🔴 REGISTER THE POOL BEFORE THE CANARY, NOT AFTER IT. The walk's
                # teardown reads `pool_holder["pool"]`, so anything that raises
                # between here and that assignment left a LIVE pool invisible to
                # the `finally`: N worker subprocesses keeping their VRAM until
                # the service exits, and — because `LEASE` is process-wide with
                # no timeout — every later walk and every later document write
                # logging `gpu_lease=busy decision=cpu` forever. `check_canary`
                # only catches GpuUnavailable, so a broken pipe, a struct error,
                # a sticky EmbeddingUnavailable from the CPU reference, or a
                # cancellation on either await all reached it.
                pool_holder["pool"] = pool
                if pool is not None:
                    # §9.1: prove this machine's GPU agrees with this machine's
                    # CPU before trusting a single vector from it. Workers that
                    # fail are terminated here, not used and reconciled later.
                    survivors = await asyncio.to_thread(
                        gpu_host.check_canary, pool,
                        self.embedder.embed, self.gpu_config.gpu_canary_tolerance,
                    )
                    if not survivors:
                        # to_thread: shutdown waits on process exits (up to
                        # gpu_worker_shutdown_s each) and then polls sysfs for
                        # the VRAM to come back. Run inline it blocks the event
                        # loop — every MCP request and /healthz with it — for
                        # tens of seconds, on the §9.1 failure path, which is the
                        # branch a new GPU stack is most likely to take.
                        await asyncio.to_thread(pool.shutdown)
                        pool = None
                        pool_holder["pool"] = None
                if job is not None and pool is not None:
                    job.decision = "gpu"
                    job.pool = "cold"
                return pool

            if job is not None:
                # §14.1. `planned` is the ESTIMATE's intent; `decision` on the
                # embed.done line is what actually happened. They differ
                # legitimately whenever a walk turns out to have no work to do,
                # and conflating them is how a walk that embedded nothing came
                # to be logged as a GPU walk.
                job.decision = "cpu"
                job.plan(
                    files=len(files),
                    force=force,
                    deindexed_skipped=deindexed_skipped,
                    sync_conflicts=len(conflicts),
                    skipped_unchanged_est=estimate.skipped_unchanged,
                    registered=estimate.skipped_registered,
                    planned="gpu" if estimate.use_gpu else "cpu",
                )

            async def flush() -> None:
                nonlocal window, window_chunks
                if window:
                    pool = await ensure_pool(window_chunks)
                    ok = await self._embed_and_store_window(
                        project, window, summary, pool, scheduler_job=scheduler_job,
                    )
                    if not ok:
                        # 6.2: the pool misbehaved. It still gets torn down by the
                        # `finally` — it just does not get to linger.
                        pool_holder["healthy"] = False
                    window = []
                    window_chunks = 0

            try:
                while True:
                    item = await queue.get()
                    if item is None:  # producer finished
                        break
                    processed += 1
                    kind, source, payload = item
                    try:
                        if kind == "error":
                            if source is not None:
                                live_sources.append(source)
                            summary["errors"].append(payload)
                        elif kind == "empty":
                            pass  # empty file — nothing to index, and no row to keep
                        elif kind == "skip":
                            live_sources.append(source)
                            summary["skipped"] += 1
                        else:  # "parsed"
                            live_sources.append(source)
                            parsed: _Parsed = payload
                            if parsed.retier:
                                summary["tier_changed"] += 1
                                if parsed.new_tier == TIER_REGISTERED:
                                    # Count what the rewrite is about to drop. The
                                    # DELETE inside replace_document cascades these
                                    # away; this is purely so the purge is visible
                                    # in the reindex report.
                                    summary["chunks_purged"] += await self.store.chunk_count(
                                        project, source
                                    )
                            known = parsed.known
                            if (not force and not parsed.retier and known
                                    and known.doc_id == parsed.doc.doc_id):
                                # Touched but content-identical: refresh stat
                                # metadata only.
                                await self.store.touch_document(
                                    project, source, parsed.doc.file_mtime,
                                    parsed.doc.file_size,
                                )
                                summary["skipped"] += 1
                            else:
                                # Preserve an explicitly-chosen category across a
                                # rewrite. force=True (full_rebuild) is the escape
                                # hatch: it re-derives from category_mappings, so
                                # editing that config and rebuilding actually
                                # applies the new mapping.
                                if not force and known is not None:
                                    parsed.doc.category = known.category
                                if not parsed.chunks:
                                    # Registered tier, or a document that chunked
                                    # empty: no embedding, so it never joins a
                                    # window and is written on the spot.
                                    await self._store_unembedded(project, parsed.doc)
                                    summary["indexed"] += 1
                                else:
                                    window.append(parsed)
                                    window_chunks += len(parsed.chunks)
                                    if (window_chunks >= EMBED_WINDOW_CHUNKS
                                            or len(window) >= EMBED_WINDOW_DOCS):
                                        await flush()
                    except Exception as exc:
                        summary["errors"].append(f"{Path(source).name}: {exc}")
                    if progress:
                        progress({"processed": processed, **summary})
                await flush()
            finally:
                # §8.2: worker teardown is unconditional and goes HERE, beside
                # release_to_os() — the same 'bulk work is over' seam. A leaked
                # GPU process holding VRAM is worse than a slow shutdown, and
                # this runs on success, failure and cancellation alike.
                pool = pool_holder["pool"]
                if pool is not None:
                    # `decision` was set to "gpu" when the pool STARTED, which is
                    # not the same as the GPU having done anything. If every
                    # device then yielded (§6.4) or died and `embed_with_pool`
                    # drained the remainder on the CPU, the summary line — the
                    # one 5.18.3 exists to make truthful — claimed a GPU walk
                    # while its own device rows said zero chunks. Correct it
                    # from what the workers actually embedded.
                    #
                    # 🔴 6.2: a DELTA, not a total. A warm pool arrives with a
                    # previous job's chunks already on its counters, so
                    # `any(w.stats.chunks)` is true from the moment it is
                    # claimed — and this correction, whose entire purpose is to
                    # catch a job that decided GPU and embedded nothing on it,
                    # would never fire again on any pool after the first.
                    embedded = pool_chunks(pool) - pool_holder["baseline"]
                    if job is not None and embedded <= 0:
                        job.decision = "cpu"
                    # §8.2 is unchanged: the pool is disposed of on success,
                    # failure and cancellation alike. 6.2 only adds a second
                    # kind of disposal — parked, on a timer, still holding the
                    # lease — and `_park_or_shutdown` always does exactly one of
                    # the two. The per-device rows now come out of the teardown
                    # itself (gpu_host.log_device_rows), so a parked pool reports
                    # them when it is finally reaped rather than claiming a
                    # release that has not happened.
                    await self._park_or_shutdown(
                        pool, healthy=pool_holder["healthy"]
                    )
                # The producer must never outlive the walk — a canceled or failed
                # consumer would otherwise leave a task parsing files into a queue
                # nobody reads, holding a document's worth of memory each.
                if not producer.done():
                    producer.cancel()
                try:
                    await producer
                except asyncio.CancelledError:
                    producer_failed = "the walk was canceled"
                except Exception as exc:
                    # 🔴 A PRODUCER THAT DIED IS NOT A PRODUCER THAT FINISHED, AND
                    # THE REMOVAL SWEEP CANNOT TELL THEM APART ON ITS OWN.
                    # `_parse_ahead`'s `finally` always enqueues the sentinel, so
                    # the consumer sees a clean end-of-stream either way. If it
                    # died after N of M files, `live_sources` holds N entries and
                    # `delete_documents_not_in` removes the OTHER M-N rows —
                    # documents that are present, readable and simply never
                    # reached. The walk then returns outcome="ok", errors=[] and
                    # a large `removed` count. Swallowing the exception here is
                    # what made that indistinguishable from success.
                    if before_removal is None:
                        producer_failed = f"{type(exc).__name__}: {exc}"
                        log.exception(
                            "The parse-ahead producer for project %r failed; the "
                            "removal sweep will be SKIPPED because the set of live "
                            "documents is incomplete", project,
                        )
                    else:
                        producer_failed = type(exc).__name__
                        log.error(
                            "Index producer failed project=%s reason=%s; "
                            "removal sweep skipped because the live-document set is incomplete",
                            project, type(exc).__name__,
                        )
                if scheduler_job is not None:
                    outcome = (
                        "canceled" if scheduler_job.state == "canceled"
                        else "failed" if scheduler_job.state == "failed"
                        else "completed"
                    )
                    await self.scheduler.close_job(scheduler_job, outcome=outcome)
            if progress:
                progress({"processed": processed, **summary})
            # Defense in depth for the removal sweep. `source <> ALL('{}')` is
            # vacuously TRUE, so an EMPTY live_sources deletes every row in the
            # project — a whole index destroyed by a walk that came back empty.
            # iter_document_files now raises when the root is unreadable, but any
            # other route to "zero files found" (an emptied mount, a policy that
            # matches nothing) must not be allowed to mean "delete everything".
            # Refusing costs one stale row; obeying costs the entire index.
            source_safe = True
            if before_removal is not None:
                try:
                    source_safe = bool(before_removal())
                except Exception:
                    source_safe = False
            if not source_safe:
                summary["removed"] = 0
                summary["errors"].append(
                    "removal sweep skipped: project source identity changed or became unavailable"
                )
            elif producer_failed:
                # Same trade as the empty-walk guard below: a stale row costs
                # one wrong search hit, a wrong sweep costs the corpus.
                summary["removed"] = 0
                summary["errors"].append(
                    f"removal sweep skipped: the parse-ahead producer failed "
                    f"({producer_failed}), so the live-document set is incomplete"
                )
            elif not live_sources and not walk_found_files:
                if existing:
                    if before_removal is None:
                        log.error(
                            "Refusing to remove all %d documents from project %r: the "
                            "filesystem walk of %s found no indexable files at all. This is "
                            "almost always an unmounted or renamed documents_dir, not a "
                            "genuinely emptied corpus. Nothing was removed; fix the path and "
                            "reindex, or delete the project if the corpus really is empty.",
                            len(existing), project, documents_dir,
                        )
                    else:
                        log.error(
                            "Refusing empty-walk removal project=%s reason=source_empty_walk "
                            "indexed_documents=%d",
                            project, len(existing),
                        )
                    summary["removed"] = 0
                    summary["errors"].append(
                        f"removal sweep skipped: walk found 0 files but the index holds "
                        f"{len(existing)} documents ({documents_dir})"
                    )
                    # Installer design 7.3 (C10): the refusal is safe but was invisible.
                    # A missing bind-mounted folder comes back empty, and the log line
                    # above is the only trace. The engine copies this plain sentence
                    # into the project's reindex progress error, which Admin's project
                    # view shows. The log.error calls above stay as they are.
                    summary["empty_root_message"] = (
                        f"The documents folder {display_for(str(documents_dir))} is empty or not mounted. "
                        "Nothing was removed. When the files are back, restart Cognita "
                        "or reindex the project."
                    )
                    log.info(
                        "Empty-walk refusal surfaced as reindex error project=%s path=%s "
                        "indexed_documents=%d", project, documents_dir, len(existing),
                    )
                else:
                    summary["removed"] = 0
            elif not live_sources:
                # The walk DID see files; every one of them was suppressed, a
                # conflict copy, or empty. That is a correct empty corpus, so the
                # rows must go — but not through delete_documents_not_in, whose
                # `source <> ALL($1)` is vacuously true for an empty array. The
                # guard above exists precisely because that quirk once destroyed
                # an index, and routing this case through it would be relying on
                # the accident rather than saying what is meant.
                summary["removed"] = await self.store.delete_all_documents(project)
                if summary["removed"]:
                    # Clearing a whole project index is never silent, even when it
                    # is correct: this is the one branch that can empty a corpus in
                    # one step, and "why did everything vanish" has to be
                    # answerable from cognita.log alone.
                    log.warning(
                        "Cleared all %d document(s) from %r: the walk found "
                        "%d file(s) and every one was excluded (%d de-indexed, "
                        "%d sync-conflict copies) or parsed empty.",
                        summary["removed"], project,
                        deindexed_skipped + len(conflicts) + len(files),
                        deindexed_skipped, len(conflicts),
                    )
            else:
                summary["removed"] = await self.store.delete_documents_not_in(
                    project, live_sources
                )
            if summary["indexed"] or summary["removed"]:
                self.query_cache(project).invalidate()
            if job is not None:
                job.done(
                    files=summary["total_files"],
                    indexed=summary["indexed"],
                    skipped_unchanged=summary["skipped"],
                    removed=summary["removed"],
                    errors=len(summary["errors"]),
                    outcome="ok",
                )
            return summary

    async def _parse_ahead(
        self,
        queue: asyncio.Queue,
        files: list[Path],
        documents_dir: Path,
        policy: ExtensionPolicy,
        existing: dict,
        force: bool,
    ) -> None:
        """Read, parse and chunk every file, one ahead of the embedder.

        Deliberately store-free. It decides only what can be decided from the
        filesystem and the `existing` snapshot it was handed, and hands the rest
        to the consumer — the project write lock is owned by TASK, so a second
        task issuing store writes underneath it would be doing writes outside the
        section that exists to serialize them.

        Always terminates the queue with None, on every path including failure,
        or the consumer waits for a producer that has already gone.
        """
        try:
            for filepath in files:
                source = None
                try:
                    source = filepath.relative_to(documents_dir).as_posix()
                    known = existing.get(source)
                    # A tier change (config edit, or the 4.3 -> 4.4 demotion of
                    # code to registered) must beat every skip below: the file on
                    # disk is untouched, so only the tier comparison can tell us
                    # the stored row is now the wrong shape.
                    new_tier = policy.tier_for(filepath.suffix)
                    retier = known is not None and known.tier != new_tier
                    if (not force and not retier and known
                            and self._stat_matches(filepath, known)):
                        await queue.put(("skip", source, None))
                        continue
                    # Parse AND chunk in the worker thread. Chunking is pure CPU
                    # over the extracted text and belongs on the same side of the
                    # boundary as the parse, not on the event loop.
                    #
                    # Avoid chunking unchanged files. The stat check can miss a
                    # touched but byte-identical file, so the worker compares
                    # document identity before chunking. Unconditional chunking
                    # once caused a 64-second sync that indexed nothing.
                    #
                    # The producer can make this call itself — it already holds
                    # `known` — and only the resulting STORE write has to wait
                    # for the consumer.
                    want_chunks = force or retier or known is None
                    parsed = await asyncio.to_thread(
                        self._parse_and_chunk, filepath, documents_dir, policy,
                        known=known, want_chunks=want_chunks,
                    )
                    if parsed is None:  # empty file — nothing to index
                        await queue.put(("empty", source, None))
                        continue
                    doc, chunks = parsed
                    await queue.put((
                        "parsed", source,
                        _Parsed(source, doc, chunks, known, retier, new_tier),
                    ))
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    await queue.put(("error", source, f"{filepath.name}: {exc}"))
        finally:
            # Not in the try above: a canceled producer must still release a
            # consumer that is blocked on get(). If the queue is full the put
            # would block forever, so this is best-effort.
            try:
                queue.put_nowait(None)
            except asyncio.QueueFull:
                await queue.put(None)

    def _parse_and_chunk(
        self,
        filepath: Path,
        documents_dir: Path,
        policy: ExtensionPolicy,
        *,
        known=None,
        want_chunks: bool = True,
    ) -> tuple[ParsedDocument, list] | None:
        """parse_file + chunking, together, off the event loop.

        🔴 Chunking is SKIPPED when the parse proves the document is unchanged.
        `content_hash` is computed by the parse, so by the time we are here the
        answer to "is this the same document we already indexed?" is known — and
        chunking a document we are about to skip is pure waste. It is not small
        waste: a live application that rewrites a large file without changing it
        makes this the dominant cost of every watcher sync.

        `want_chunks` is forced True by the caller for a forced rebuild, a tier
        change, or a document the store has never seen, because in those cases
        the doc_id comparison cannot authorize a skip.
        """
        doc = self._parse(filepath, documents_dir, policy)
        if doc is None:
            return None
        if doc.is_registered:
            return doc, []
        if not want_chunks and known is not None and known.doc_id == doc.doc_id:
            return doc, []
        return doc, doc.chunks(self.chunk_size, self.chunk_overlap)

    async def _store_unembedded(self, project: str, doc: ParsedDocument) -> None:
        """Write a document that has no vectors: registered tier, or chunked empty.

        Registered documents short-circuit BEFORE the embedder is touched — the
        spec's "genuinely skip, not embed-and-discard", so their reindex cost is
        filesystem plus one INSERT.
        """
        content = doc.content if doc.is_registered else None
        await self.store.replace_document(
            project, self._document_record(doc, content=content), []
        )

    async def _embed_and_store_window(
        self, project: str, window: list[_Parsed], summary: dict, pool=None,
        *, scheduler_job=None,
    ) -> bool:
        """Embed a window's chunks in ONE call, then write each document alone.

        🔴 The window is an embed batch, never a write batch. Every document
        still gets its own `replace_document` transaction (D4.0), so a store
        failure on one cannot roll back its neighbors.

        Returns False if the POOLED embed raised — 6.2's caller uses that to
        refuse to park the pool afterwards. `embed_with_pool` already absorbs
        every failure it knows about (a dead device, a wedged one, a yield), so
        an exception out of it means something outside that contract went wrong,
        and offering such a pool to the next small write for the rest of the
        linger window would spread one job's fault across all of them.
        """
        texts = [c.content for p in window for c in p.chunks]
        used_pool = False
        try:
            if self.scheduler is not None:
                vectors = await self._scheduler_embed(
                    project, texts, "walk", job=scheduler_job,
                )
            elif pool is not None and pool.alive:
                used_pool = True
                # §10: every failure inside here is handled and ends with the
                # vectors produced — a device that dies, wedges or yields is
                # dropped and its slice retried elsewhere, and if every device
                # drops out the remainder drains on the CPU embedder, which was
                # never unloaded (§8.5).
                vectors = await asyncio.to_thread(
                    gpu_host.embed_with_pool, pool, texts, self.gpu_config,
                    self.embedder.embed,
                )
            else:
                vectors = await asyncio.to_thread(self.embedder.embed, texts)
        except Exception:
            # One embed call now covers many documents, so a failure here would
            # otherwise be reported against a window rather than a file. Retry
            # document by document to put the error back on the document that
            # caused it — and to let the other documents in the window succeed,
            # which is what would have happened before the pipeline existed.
            log.warning(
                "Batched embed of %d chunks across %d document(s) failed; "
                "retrying per document to attribute the failure",
                len(texts), len(window), exc_info=True,
            )
            for parsed in window:
                try:
                    await self._index_parsed(project, parsed.doc)
                    summary["indexed"] += 1
                except Exception as exc:
                    summary["errors"].append(f"{Path(parsed.source).name}: {exc}")
            return not used_pool
        offset = 0
        for parsed in window:
            count = len(parsed.chunks)
            slice_ = vectors[offset:offset + count]
            offset += count
            try:
                await self.store.replace_document(
                    project,
                    self._document_record(parsed.doc),
                    self._chunk_records(parsed.doc, parsed.chunks, slice_),
                )
                summary["indexed"] += 1
            except Exception as exc:
                summary["errors"].append(f"{Path(parsed.source).name}: {exc}")
        return True

    async def _scheduler_embed(
        self, project: str, texts: list[str], kind: str, *, job=None,
    ) -> list[list[float]]:
        """Submit text through the host-owned scheduler and close its job.

        Retrieval retains document ordering and transaction boundaries; the
        scheduler only owns chunk dispatch and returns an ordered vector list.
        """
        owned = job is None
        if owned:
            job = self.scheduler.open_job(project, kind, estimated_chunks=len(texts))
        try:
            return await self.scheduler.embed(job, texts)
        finally:
            if owned:
                outcome = (
                    "canceled" if job.state == "canceled"
                    else "failed" if job.state == "failed"
                    else "completed"
                )
                await self.scheduler.close_job(job, outcome=outcome)

    async def index_file(
        self,
        project: str,
        documents_dir: Path,
        filepath: Path,
        *,
        category_override: str | None = None,
    ) -> IndexFileOutcome | None:
        """Index/refresh a single file (chat writes, M4 watcher). Returns
        (doc_id, chunks_indexed), or None if the file parsed empty.

        Category resolution is override > stored > detected. category_override
        preserves the 3.x add_document semantics (an explicit category argument
        beats path-mapping detection); the STORED fallback is what stops a
        rewrite from silently resetting the category, since detect_category
        returns "general" whenever no mapping matches — and every edit tool
        funnels through update_document, which passes no override."""
        # Treat a single-document write as a one-item indexing job. Large edits
        # re-embed the whole file, and job scoping ensures the work is measured
        # by the same scheduler and diagnostics as walks. `embed_job` joins an
        # enclosing job, so bulk callers still report one job.
        with embed_job(project, walk="document") as job:
            outcome = await self._index_file_locked(
                project, documents_dir, filepath, category_override=category_override
            )
            job.done(files=1, indexed=int(bool(outcome)),
                     outcome="ok" if outcome is not None else "empty")
            return outcome

    async def _index_file_locked(
        self,
        project: str,
        documents_dir: Path,
        filepath: Path,
        *,
        category_override: str | None = None,
    ) -> IndexFileOutcome | None:
        async with self._write_lock(project):
            extension_policy = self.policy_for(project)
            effective_policy = self.effective_index_policy_for(project)
            if effective_policy is not None:
                source = self._relative_dirty_path(filepath, documents_dir)
                decision = effective_policy.decision(
                    source,
                    globally_eligible=extension_policy.tier_for(Path(source).suffix) is not None,
                )
                # Explicit writes have historically re-admitted a per-file
                # de-indexed path. Keep that narrow behavior; folder and book
                # exclusions remain authoritative across writes and copies.
                if not decision.indexed and decision.reason == "per_file_exclusion":
                    self.readmit(project, source)
                    effective_policy = self.effective_index_policy_for(project)
                    decision = (
                        effective_policy.decision(source)
                        if effective_policy is not None else decision
                    )
                if not decision.indexed:
                    removed = await self.store.delete_document(project, source)
                    if removed:
                        self.query_cache(project).invalidate()
                    return IndexFileOutcome(
                        None, 0, False, decision.reason,
                    )
            provenance_recorder = self._book_index_provenance_recorder
            provenance_enabled = (
                provenance_recorder is not None
                and effective_policy is not None
                and getattr(effective_policy, "layout", None) is not None
            )
            raw_sha256 = None
            if provenance_enabled:
                raw_sha256 = await asyncio.to_thread(
                    lambda: hashlib.sha256(filepath.read_bytes()).hexdigest()
                )
            doc = await asyncio.to_thread(
                self._parse, filepath, documents_dir, extension_policy
            )
            if doc is None:
                return None
            if provenance_enabled:
                parsed_source_sha = await asyncio.to_thread(
                    lambda: hashlib.sha256(filepath.read_bytes()).hexdigest()
                )
                if parsed_source_sha != raw_sha256:
                    # A Word save raced the parser. Do not attach approval
                    # provenance to content whose exact source bytes are
                    # unknown; a later reconcile can index the stable version.
                    return IndexFileOutcome(
                        None, 0, False, "source_changed_during_indexing",
                        doc.content_hash, False,
                    )
            if category_override:
                doc.category = category_override
            else:
                existing = await self.store.get_document(project, doc.source)
                if existing is not None:
                    doc.category = existing.category
            chunks = await self._index_parsed(project, doc)
            # An explicit write re-admits the path (5.7). Not optional: the walk
            # skips suppressed sources, so a row indexed here for a still-listed
            # path would be swept away by the next index_project — a write that
            # reported success and then silently lost its document.
            self.readmit(project, doc.source)
            self.query_cache(project).invalidate()
            provenance_current = None
            if provenance_enabled:
                current_raw_sha = await asyncio.to_thread(
                    lambda: hashlib.sha256(filepath.read_bytes()).hexdigest()
                )
                if current_raw_sha == raw_sha256:
                    from .books.docx import PROJECTION_VERSION

                    record = provenance_recorder(
                        project, doc.source, doc.doc_id, doc.content_hash,
                        raw_sha256, PROJECTION_VERSION,
                    )
                    if inspect.isawaitable(record):
                        record = await record
                    provenance_current = record is not None
                else:
                    provenance_current = False
            return IndexFileOutcome(
                doc.doc_id, chunks, provenance_current is not False,
                None if provenance_current is not False else "provenance_unavailable",
                extracted_sha256=doc.content_hash,
                provenance_current=provenance_current,
            )

    async def remove_file(self, project: str, source: str) -> bool:
        async with self._write_lock(project):
            removed = await self.store.delete_document(project, source)
            if removed:
                self.query_cache(project).invalidate()
            return removed

    async def move_file(
        self, project: str, documents_dir: Path, old_source: str, new_source: str
    ) -> tuple[str, int]:
        """Re-point the index from old_source to new_source (the file is already
        moved on disk by the caller). Metadata-only when old_source was indexed —
        no re-embed (4.1, DESIGN-4.1-move-document.md); otherwise indexes the moved
        file fresh at its new home. Always returns (doc_id, chunks)."""
        async with self._write_lock(project):
            effective_policy = self.effective_index_policy_for(project)
            extension_policy = self.policy_for(project)
            if effective_policy is not None:
                destination = effective_policy.decision(
                    new_source,
                    globally_eligible=extension_policy.tier_for(
                        Path(new_source).suffix
                    ) is not None,
                )
                if not destination.indexed and destination.reason == "per_file_exclusion":
                    self.readmit(project, new_source)
                    effective_policy = self.effective_index_policy_for(project)
                    destination = (
                        effective_policy.decision(new_source)
                        if effective_policy is not None else destination
                    )
                if not destination.indexed:
                    removed_old = await self.store.delete_document(project, old_source)
                    removed_new = await self.store.delete_document(project, new_source)
                    if removed_old or removed_new:
                        self.query_cache(project).invalidate()
                    return "", 0
            # 5.7: a move re-admits BOTH ends. The destination is indexed below,
            # so leaving it suppressed would cost it its row at the next walk;
            # the source no longer holds a file, so an entry left there could
            # only ever suppress some unrelated file written to that path later.
            self.readmit(project, old_source)
            self.readmit(project, new_source)
            policy = extension_policy
            old_doc = await self.store.get_document(project, old_source)
            # The metadata-only fast path is valid only while the tier holds.
            # Crossing a boundary (notes.md -> notes.py) changes the required
            # SHAPE of the row, so it falls through to the reindex below: the
            # DELETE drops the old row, chunks cascade, and the file is stored
            # fresh in its new tier. That is what stops orphaned vectors.
            same_tier = (old_doc is not None
                         and old_doc.tier == policy.tier_for(Path(new_source).suffix))
            if old_doc is not None and same_tier:
                new_doc_id = compute_doc_id(new_source, old_doc.content_hash)
                try:
                    moved = await self.store.move_document(
                        project, old_source, new_source, new_doc_id
                    )
                except Exception:
                    # destination row clash / transient — fall back to a fresh index
                    log.warning("move_document fast path failed for %s -> %s; reindexing",
                                old_source, new_source, exc_info=True)
                    moved = None
                if moved is not None:
                    self.query_cache(project).invalidate()
                    return new_doc_id, moved
            # not indexed, or the fast path fell through: index the moved file fresh
            # and make sure the old source leaves the index.
            await self.store.delete_document(project, old_source)
            doc = await asyncio.to_thread(
                self._parse, documents_dir / new_source, documents_dir, policy
            )
            if doc is None:
                self.query_cache(project).invalidate()
                return "", 0
            chunks = await self._index_parsed(project, doc)
            self.query_cache(project).invalidate()
            return doc.doc_id, chunks

    def _parse(
        self, filepath: Path, documents_dir: Path, policy: ExtensionPolicy | None = None
    ) -> ParsedDocument | None:
        return parse_file(
            filepath,
            documents_dir,
            category_mappings=self.category_mappings,
            keyword_routes=self.keyword_routes,
            policy=policy,
        )

    async def _index_parsed(self, project: str, doc: ParsedDocument) -> int:
        """Embed + transactionally store one parsed document. Returns chunk count.

        Registered documents short-circuit BEFORE the embedder is touched — the
        spec's "genuinely skip, not embed-and-discard", so their reindex cost is
        filesystem plus one INSERT.
        """
        if doc.is_registered:
            await self.store.replace_document(
                project, self._document_record(doc, content=doc.content), []
            )
            return 0
        text_chunks = doc.chunks(self.chunk_size, self.chunk_overlap)
        if not text_chunks:
            return 0
        texts = [c.content for c in text_chunks]
        # 🔴 §4.3: THERE IS NO SPECIAL CASE FOR A SINGLE-DOCUMENT WRITE. An edit
        # re-embeds the WHOLE file, so "one document" does not mean "small
        # work" — one line changed in a large manual is thousands of chunks,
        # and that is precisely the expensive case a GPU exists for.
        #
        # This path used to hardcode the CPU, so the rule existed in
        # index_project and the path every connector edit actually takes never
        # consulted it. Observed live: a 341-chunk update_document taking 64
        # seconds on a saturated CPU with two idle cards watching.
        #
        # The chunk count here is EXACT, not the §4.1 estimate — the document is
        # already parsed and chunked, so there is nothing to predict.
        vectors = None
        if self.scheduler is not None:
            vectors = await self._scheduler_embed(project, texts, "document")
        elif self._bulk_pool is not None:
            # §4.4: a bulk caller already owns a pool and the lease for this
            # whole job. Reusing it is the entire point — starting a second one
            # per file is N spin-ups of ~3s each, and the lease is held anyway.
            try:
                vectors = await asyncio.to_thread(
                    gpu_host.embed_with_pool, self._bulk_pool, texts,
                    self.gpu_config, self.embedder.embed,
                )
            except Exception:
                log.warning("GPU embed failed for a document in a bulk job; "
                            "using the CPU", exc_info=True)
                self._bulk_pool_healthy = False
                vectors = None
        elif self.gpu_config is not None:
            # 🔴 6.2: THE THRESHOLD MOVED INSIDE `_embed_on_gpu`, and that is the
            # whole feature. It used to be tested here, so a 40-chunk edit could
            # not reach the GPU path even to ASK whether a card was already up —
            # and four seconds after a rebuild finished, two loaded cards sat
            # idle while the CPU did the work. `gpu_min_chunks` still governs
            # whether a document is worth a COLD start; it does not govern
            # whether it may use hardware that is already running.
            vectors = await self._embed_on_gpu(project, texts)
        if vectors is None:
            vectors = await asyncio.to_thread(self.embedder.embed, texts)
        records = self._chunk_records(doc, text_chunks, vectors)
        await self.store.replace_document(project, self._document_record(doc), records)
        return len(records)

    @asynccontextmanager
    async def bulk_gpu_job(
        self,
        project: str,
        walk: str,
        files: list[Path],
        documents_dir: Path,
    ):
        """🔴 §4.4: ONE decision, ONE pool and ONE lease for a whole bulk job.

        For a caller that loops over `index_file` — `copy_directory` is the live
        example the design names twice — this is the difference between the
        feature working and the feature being actively harmful.

        Without it every file decides for itself, and BOTH outcomes are wrong:

        - 500 modest documents each fall under `gpu_min_chunks`, so every
          decision is individually correct and the GPU is never used once, on
          precisely the workload it exists for. That is §4.4's original point.
        - 500 LARGE documents each clear it, so every file pays a full pool
          spin-up — subprocess, model load, a ~25-38s MIGraphX shape compile
          (§5.2, and the program cache is off by default), canary, teardown.
          That is far slower than simply staying on the CPU, and it is what
          shipped once `index_file` became GPU-capable in 5.21.0.

        The estimate is taken over the whole file list, exactly as
        `index_project` does, so the answer is about the JOB. If it says CPU, or
        no device qualifies, or the canary fails, `_bulk_pool` stays None and
        every file simply uses the CPU — the ordinary outcome, not an error.
        """
        with embed_job(project, walk) as job:
            pool = None
            baseline = 0
            self._bulk_pool_healthy = True
            try:
                if self.gpu_config is not None and files and self.scheduler is None:
                    estimate = await asyncio.to_thread(
                        estimate_job,
                        files,
                        documents_dir,
                        chunk_size=self.chunk_size,
                        chunk_overlap=self.chunk_overlap,
                        policy=self.policy_for(project),
                        threshold=self.gpu_min_chunks,
                    )
                    job.est_chunks = estimate.est_chunks
                    job.threshold = estimate.threshold
                    job.binary_formats = estimate.binary_formats
                    # 🔴 6.2: warm first, estimate second. A copy of 20 small
                    # files is exactly the job the threshold is right to refuse a
                    # cold start for and exactly the job that should ride cards
                    # a rebuild left running a moment ago. 6.2.1's floor is
                    # applied to the ESTIMATE here — the same lower bound from
                    # file sizes the cold decision uses, which is the only figure
                    # available before anything is parsed.
                    if estimate.est_chunks >= self.gpu_warm_min_chunks:
                        pool = await self._claim_warm_pool(job)
                    if pool is None and estimate.use_gpu:
                        pool = await self._start_proven_pool(
                            project, f"{project}:{walk}:{id(job)}"
                        )
                        if pool is not None:
                            job.decision = "gpu"
                            job.pool = "cold"
                    baseline = pool_chunks(pool) if pool is not None else 0
                    # After the pool decision, so `decision=` and `pool=` on the
                    # plan line describe what this job will actually do rather
                    # than what the estimator hoped for. `planned=` keeps the
                    # estimator's own answer, which is what grades it.
                    job.plan(files=len(files), planned=estimate.decision)
                self._bulk_pool = pool
                yield pool
            finally:
                self._bulk_pool = None
                if pool is not None:
                    # 6.2: the DELTA, for the same reason the walk uses one — a
                    # claimed pool arrives carrying a previous job's chunks.
                    if pool_chunks(pool) - baseline <= 0:
                        job.decision = "cpu"
                    await self._park_or_shutdown(
                        pool, healthy=self._bulk_pool_healthy
                    )
                job.done(files=len(files))

    async def _start_proven_pool(self, project: str, holder: str):
        """Start a pool and prove it against this machine's CPU (§9.1).

        Returns None for every ordinary "no GPU" reason, and never raises: §10's
        rule is that the caller always completes, and an accelerator fault must
        not fail work the CPU can do.
        """
        pool = None
        try:
            pool = await asyncio.to_thread(
                gpu_host.start_pool, self.gpu_config, self.gpu_probe,
                holder, self.gpu_batch_ceiling_gb,
            )
            if pool is None:
                return None
            survivors = await asyncio.to_thread(
                gpu_host.check_canary, pool, self.embedder.embed,
                self.gpu_config.gpu_canary_tolerance,
            )
            if not survivors:
                await asyncio.to_thread(pool.shutdown)
                return None
            return pool
        except Exception:
            log.warning("GPU pool startup failed for project %r; continuing on "
                        "the CPU", project, exc_info=True)
            if pool is not None:
                await asyncio.to_thread(pool.shutdown)
            return None

    async def _embed_on_gpu(self, project: str, texts: list[str]) -> list[list[float]] | None:
        """One document's chunks on the GPU, or None to use the CPU.

        Returns None for every ordinary "not available" reason — no GPU
        configured, no device qualifying, the document too small to justify a
        cold start, the lease already held by a walk — and the caller falls back
        without ceremony (§10: the walk always completes; the only variable is
        how long it took).

        Check for a warm card first using `gpu_warm_min_chunks` (0): startup is
        already paid, so any job size can use it. If no card is warm,
        `gpu_min_chunks` (20) decides whether the document justifies about 3s
        of worker startup plus a canary.

        These thresholds answer different questions and should remain separate:
        one governs warm-pool admission, the other pays for cold startup.

        The pool is handed to the warm slot on the way out rather than torn
        down, which is what makes the NEXT small edit in the burst free. §8.2's
        rule that VRAM must come back is not weakened: it is now on
        `gpu_idle_linger_s`'s timer instead of on this `finally`, and the pool
        is still torn down here whenever lingering is off or the embed failed.
        """
        pool = None
        if len(texts) >= self.gpu_warm_min_chunks:
            pool = await self._claim_warm_pool()
        if pool is None:
            if len(texts) < self.gpu_min_chunks:
                return None
            pool = await self._start_proven_pool(
                project,
                # 🔴 UNIQUE PER CALL. `LEASE.release` matches on the holder
                # STRING, so a constant `f"{project}:document"` gave every
                # concurrent document write in a project the same identity —
                # and under the load that actually reproduces GPU faults here
                # (a burst of connector edits), one write's teardown could
                # release a lease another write was holding. The lease's own
                # acquire happens to serialize these today, so this is a latent
                # hazard rather than an observed bug; it is one f-string to
                # remove and the failure it enables is a silent double-spawn.
                f"{project}:document:{id(texts)}",
            )
            if pool is None:
                return None
            job = current_job()
            if job is not None:
                job.decision = "gpu"
                job.pool = "cold"
        healthy = True
        try:
            return await asyncio.to_thread(
                gpu_host.embed_with_pool, pool, texts, self.gpu_config,
                self.embedder.embed,
            )
        except Exception:
            healthy = False
            log.warning("GPU embed failed for a document write; using the CPU",
                        exc_info=True)
            return None
        finally:
            await self._park_or_shutdown(pool, healthy=healthy)

    @staticmethod
    def _chunk_records(doc: ParsedDocument, text_chunks: list, vectors: list) -> list[ChunkRecord]:
        """Pair chunks with their vectors. Shared by the single-document path and
        the walk's window, so the two cannot drift into building different rows."""
        return [
            ChunkRecord(
                chunk_id=f"{doc.doc_id}_{c.index}",  # 3.x id scheme, kept for shape-compat
                chunk_index=c.index,
                content=c.content,
                embedding=vec,
                section=c.section,
            )
            for c, vec in zip(text_chunks, vectors)
        ]

    @staticmethod
    def _document_record(doc: ParsedDocument, content: str | None = None) -> DocumentRecord:
        return DocumentRecord(
            doc_id=doc.doc_id,
            source=doc.source,
            category=doc.category,
            format=doc.format,
            keywords=doc.keywords,
            content_hash=doc.content_hash,
            file_mtime=doc.file_mtime,
            file_size=doc.file_size,
            tier=doc.tier,
            content=content,
        )

    @staticmethod
    def _stat_matches(filepath: Path, known) -> bool:
        """mtime+size short-circuit: skip parsing files whose stat is unchanged."""
        if known.file_mtime is None or known.file_size is None:
            return False
        stat = filepath.stat()
        return (
            stat.st_size == known.file_size
            and abs(stat.st_mtime - known.file_mtime.timestamp()) < 1e-3
        )

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    async def search(
        self,
        project: str,
        query: str,
        *,
        max_results: int = DEFAULT_RESULTS,
        category: str | None = None,
        hybrid_alpha: float = 0.3,
        include_registered: bool = True,
        retrieval_profile: str | None = None,
    ) -> list[dict[str, Any]]:
        """Hybrid search; returns 3.x-shaped result dicts (full content — the
        tool layer owns snippeting and min_score filtering).

        Registered documents ride the KEYWORD leg only, at any hybrid_alpha
        (D4.4-5). The consequence worth knowing: at hybrid_alpha=1.0 the
        keyword leg is switched off entirely, so a registered document can
        never appear — semantic-only means embedded-only.

        include_registered=False drops the tier altogether; evaluate_retrieval
        uses it, since retrieval quality is meaningless for documents that were
        deliberately excluded from retrieval.
        """
        query = query.strip()
        if not query:
            return []
        max_results = max(1, min(max_results, MAX_RESULTS))
        hybrid_alpha = max(0.0, min(hybrid_alpha, 1.0))
        cache = self.query_cache(project)
        admitted_sources, admitted_doc_ids, admission_metadata = await self._effective_indexed_sources(
            project, retrieval_profile,
        )
        admission_fingerprint = (
            hashlib.sha256(
                "\0".join(sorted(admitted_sources)).encode("utf-8")
                + b"\1"
                + "\0".join(sorted(admitted_doc_ids or ())).encode("utf-8")
                + b"\2"
                + repr([
                    (key, admission_metadata[key]["role"],
                     admission_metadata[key]["editorial_status"],
                     admission_metadata[key]["summary_freshness"])
                    for key in sorted(admission_metadata)
                ]).encode("utf-8")
            ).hexdigest()
            if admitted_sources is not None else None
        )
        cache_key = (
            query, max_results, category, hybrid_alpha, include_registered,
            retrieval_profile,
            admission_fingerprint,
        )
        cached = cache.get(cache_key)
        if cached is not None:
            cached = await self._filter_current_book_admission(
                project, cached, retrieval_profile,
            )
            cached = self._filter_search_results(project, cached)
            for result in cached:
                result.pop("_doc_id", None)
            return cached
        n_candidates = min(max_results * 3, MAX_RESULTS)

        routed_category = self._route_by_keywords(query) if not category else None
        effective_category = category or routed_category
        admission_kwargs = (
            {"include_sources": admitted_sources, "include_doc_ids": admitted_doc_ids}
            if self.effective_index_policy_for(project) is not None else {}
        )

        async def dense_leg() -> list[ChunkHit]:
            if hybrid_alpha <= 0:
                return []
            qvec = (await asyncio.to_thread(self.embedder.embed, [query]))[0]
            return await self.store.dense_search(
                project, qvec, n_candidates, effective_category, **admission_kwargs
            )

        async def lexical_leg() -> list[ChunkHit]:
            if hybrid_alpha >= 1.0:
                return []
            return await self.store.lexical_search(
                project, query, n_candidates, effective_category, **admission_kwargs
            )

        async def registered_leg() -> list[ChunkHit]:
            if hybrid_alpha >= 1.0 or not include_registered:
                return []
            return await self.store.registered_lexical_search(
                project, query, n_candidates, effective_category, **admission_kwargs
            )

        dense_hits, lexical_hits, registered_hits = await asyncio.gather(
            dense_leg(), lexical_leg(), registered_leg()
        )
        effective_policy = self.effective_index_policy_for(project)
        if effective_policy is not None:
            extension_policy = self.policy_for(project)

            def eligible(hit: ChunkHit) -> bool:
                return self._policy_allows_source(
                    effective_policy, hit.source,
                    globally_eligible=extension_policy.tier_for(
                        Path(hit.source).suffix
                    ) is not None,
                )

            dense_hits = [hit for hit in dense_hits if eligible(hit)]
            lexical_hits = [hit for hit in lexical_hits if eligible(hit)]
            registered_hits = [hit for hit in registered_hits if eligible(hit)]
        registered_ids = {hit.doc_id for hit in registered_hits}
        if registered_hits:
            # A registered hit carries the WHOLE file. Narrow it to a window
            # around the match before anything downstream sees it, so the
            # reranker, MMR and the result payload all work on chunk-sized text
            # like every other hit. get_document remains the way to read it all.
            for hit in registered_hits:
                hit.content = excerpt_around_match(hit.content, query, self.chunk_size)
            # Both legs are ts_rank_cd over the same tsquery, so the scores are
            # directly comparable: merge into ONE keyword ranking rather than
            # fusing a third leg. Registered documents therefore compete on the
            # keyword side normally — no bonus, and no penalty invented to
            # compensate for the semantic component they do not have.
            lexical_hits = sorted(
                [*lexical_hits, *registered_hits], key=lambda h: -h.score
            )[:n_candidates]

        fused = self._rrf_fuse(dense_hits, lexical_hits, hybrid_alpha)
        if not fused:
            return []

        # Rerank a 3x-deep pool; fall back to RRF order if the model is unavailable.
        pool_size = max_results * RERANK_MULTIPLIER if self.reranker else max_results
        pool = fused[:pool_size]
        unreranked = False
        if self.reranker and pool:
            scores = await asyncio.to_thread(
                self.reranker.rerank, query, [c["hit"].content for c in pool]
            )
            # Skip the cache only while scores may still arrive. A reranker that
            # has failed for the life of the process will never score, so its RRF
            # results are as final as 13.x's were and caching them is right.
            state = getattr(self.reranker, "state", None)
            unreranked = scores is None and (state is None or state() != "failed")
            if scores is not None:
                for candidate, score in zip(pool, scores):
                    candidate["reranker_score"] = score
                pool.sort(key=lambda c: c.get("reranker_score", 0.0), reverse=True)

        # Normalize display scores over the pool, as 3.x did.
        raw_scores = [c.get("reranker_score", c["rrf_score"]) for c in pool]
        lo, hi = min(raw_scores), max(raw_scores)
        span = hi - lo

        if len(pool) > max_results:
            pool = _apply_mmr(pool, max_results)

        results = []
        for candidate in pool[:max_results]:
            hit: ChunkHit = candidate["hit"]
            s_rank = candidate.get("semantic_rank")
            b_rank = candidate.get("bm25_rank")
            raw = candidate.get("reranker_score", candidate["rrf_score"])
            registered = hit.doc_id in registered_ids
            results.append(
                {
                    "content": hit.content,
                    "source": hit.source,
                    "filename": hit.source.rsplit("/", 1)[-1],
                    "category": hit.category,
                    "chunk_index": hit.chunk_index,
                    # Tier markers (same pair list_documents uses). They tell a
                    # caller that get_document is the right follow-up here, not
                    # "fetch the neighboring chunks" — there are none.
                    "tier": TIER_REGISTERED if registered else TIER_EMBEDDED,
                    "semantic_searchable": not registered,
                    "score": round((raw - lo) / span if span > 0 else 1.0, 4),
                    "raw_rrf_score": round(candidate["rrf_score"], 6),
                    "reranker_score": (
                        round(candidate["reranker_score"], 6)
                        if "reranker_score" in candidate
                        else None
                    ),
                    "semantic_rank": s_rank,
                    "bm25_rank": b_rank,
                    "search_method": (
                        "hybrid" if s_rank and b_rank else "semantic" if s_rank else "keyword"
                    ),
                    "keywords": hit.keywords,
                    "routed_by": routed_category or "none",
                    "_doc_id": hit.doc_id,  # internal, for expansion; stripped below
                }
            )

        results = await self._expand_with_adjacent_chunks(project, results)
        # MMR deliberately chooses a diverse sequence, so its selection order
        # is not necessarily the normalized relevance order. Context expansion
        # is also complete by this point; publish the final page in descending
        # score order and keep that order in the query cache.
        results.sort(key=lambda r: r["score"], reverse=True)
        # Policy may change while embedding, retrieving, or reranking. Re-read
        # the host-owned snapshot immediately before publishing/cacheing the
        # response so a result that became excluded mid-query cannot escape.
        results = await self._filter_current_book_admission(
            project, results, retrieval_profile,
        )
        results = self._filter_search_results(project, results)
        # 14.0 §2.4: a result the reranker was configured for but could not
        # score while it may still become ready (loading, or a one-off scoring
        # fault) is RRF order. Caching it would keep serving `reranker_score:
        # null` for the 300 s TTL after the model becomes ready. A reranker that
        # failed for good, or none configured at all, caches as ever.
        if unreranked:
            log.debug(
                "search cache skipped project=%s results=%d: reranker returned no scores",
                project, len(results),
            )
        else:
            cache.put(cache_key, results)
        for result in results:
            result.pop("_doc_id", None)
        return results

    async def _filter_current_book_admission(
        self, project: str, results: list[dict[str, Any]],
        retrieval_profile: str | None = None,
    ) -> list[dict[str, Any]]:
        """Recheck raw-source/doc-ID provenance after work that can race a save."""
        policy = self.effective_index_policy_for(project)
        if policy is None or getattr(policy, "layout", None) is None:
            return results
        sources, doc_ids, metadata = await self._effective_indexed_sources(
            project, retrieval_profile,
        )
        admitted = set(zip(sources or (), doc_ids or ()))
        filtered = []
        for result in results:
            source = str(result.get("source", ""))
            doc_id = str(result.get("_doc_id", ""))
            if (source, doc_id) not in admitted:
                continue
            admission = metadata.get(doc_id)
            if admission is not None:
                result["book_role"] = admission["role"]
                result["chapter_id"] = admission["chapter_id"]
                result["editorial_status"] = admission["editorial_status"]
                result["summary_freshness"] = admission["summary_freshness"]
            filtered.append(result)
        return filtered

    def _filter_search_results(
        self, project: str, results: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        effective_policy = self.effective_index_policy_for(project)
        if effective_policy is None:
            return results
        extension_policy = self.policy_for(project)
        return [
            result for result in results
            if self._policy_allows_source(
                effective_policy,
                str(result.get("source", "")),
                globally_eligible=extension_policy.tier_for(
                    Path(str(result.get("source", ""))).suffix
                ) is not None,
            )
        ]

    @staticmethod
    def _rrf_fuse(
        dense_hits: list[ChunkHit], lexical_hits: list[ChunkHit], alpha: float
    ) -> list[dict[str, Any]]:
        """Reciprocal Rank Fusion over the two legs (3.x formula, k=60)."""
        candidates: dict[str, dict[str, Any]] = {}
        for rank, hit in enumerate(dense_hits, start=1):
            candidates[hit.chunk_id] = {"hit": hit, "semantic_rank": rank, "bm25_rank": None}
        for rank, hit in enumerate(lexical_hits, start=1):
            entry = candidates.setdefault(
                hit.chunk_id, {"hit": hit, "semantic_rank": None, "bm25_rank": None}
            )
            entry["bm25_rank"] = rank
        for entry in candidates.values():
            s_rank = entry["semantic_rank"] or MISSING_RANK
            b_rank = entry["bm25_rank"] or MISSING_RANK
            entry["rrf_score"] = alpha * (1 / (RRF_K + s_rank)) + (1 - alpha) * (
                1 / (RRF_K + b_rank)
            )
        return sorted(candidates.values(), key=lambda e: e["rrf_score"], reverse=True)

    def _route_by_keywords(self, query: str) -> str | None:
        """Weighted keyword→category routing with word boundaries (3.x port)."""
        query_lower = query.lower()
        best: tuple[int, str] | None = None
        for category, keywords in self.keyword_routes.items():
            matches = 0
            for keyword in keywords:
                kw = keyword.lower()
                if " " in kw:
                    if kw in query_lower:
                        matches += 1
                elif re.search(r"\b" + re.escape(kw) + r"\b", query_lower):
                    matches += 1
            if matches and (best is None or matches > best[0]):
                best = (matches, category)
        return best[1] if best else None

    async def _expand_with_adjacent_chunks(
        self, project: str, results: list[dict[str, Any]], window: int = EXPANSION_WINDOW
    ) -> list[dict[str, Any]]:
        """Merge each hit with its neighbor chunks for fuller context (3.x port)."""
        wanted: list[tuple[str, int]] = []
        for r in results:
            if r.get("tier") == TIER_REGISTERED:
                continue  # stored whole: there are no neighboring chunks
            for offset in range(-window, window + 1):
                if offset and r["chunk_index"] + offset >= 0:
                    wanted.append((r["_doc_id"], r["chunk_index"] + offset))
        if not wanted:
            return results
        fetched = await self.store.adjacent_chunks(project, wanted)
        for r in results:
            before = [
                fetched[(r["_doc_id"], i)]
                for i in range(r["chunk_index"] - window, r["chunk_index"])
                if (r["_doc_id"], i) in fetched
            ]
            after = [
                fetched[(r["_doc_id"], i)]
                for i in range(r["chunk_index"] + 1, r["chunk_index"] + window + 1)
                if (r["_doc_id"], i) in fetched
            ]
            if before or after:
                r["content"] = "\n\n".join(before + [r["content"]] + after)
                r["context_expanded"] = True
        return results


_WORD_RE = re.compile(r"[A-Za-z0-9_]+")


def excerpt_around_match(content: str, query: str, width: int) -> str:
    """A ~width-char window of `content` centered on the first query-term match.

    Registered documents are stored whole, so a keyword hit on one would
    otherwise drag an entire source file into a search result. This narrows it
    to the neighborhood of the match — the same job chunking does for the
    embedded tier, done at read time instead of write time. Snaps to line
    boundaries so code stays readable, and marks elisions with an ellipsis so
    the text is never mistaken for the complete file.
    """
    if len(content) <= width:
        return content
    terms = [t.lower() for t in _WORD_RE.findall(query)]
    lowered = content.lower()
    pos = next((p for t in terms if (p := lowered.find(t)) >= 0), -1)
    if pos < 0:
        head = content[:width]
        cut = head.rfind("\n")
        return (head[:cut] if cut > width // 2 else head).rstrip() + "\n…"
    start = max(0, pos - width // 2)
    end = min(len(content), start + width)
    if (nl := content.find("\n", start)) != -1 and nl < pos:
        start = nl + 1
    if (nl := content.rfind("\n", pos, end)) > start:
        end = nl
    return ("…\n" if start > 0 else "") + content[start:end].strip() + ("\n…" if end < len(content) else "")


def _apply_mmr(
    candidates: list[dict[str, Any]], top_k: int, lambda_param: float = MMR_LAMBDA
) -> list[dict[str, Any]]:
    """Maximal Marginal Relevance over token-set Jaccard similarity (3.x port):
    relevance-heavy diversification of the final result page."""
    if len(candidates) <= top_k:
        return candidates

    def jaccard(a: str, b: str) -> float:
        ta, tb = set(a.lower().split()), set(b.lower().split())
        if not ta or not tb:
            return 0.0
        return len(ta & tb) / len(ta | tb)

    selected = [candidates[0]]
    remaining = list(candidates[1:])
    while len(selected) < top_k and remaining:
        best_idx = 0
        best_mmr = float("-inf")
        for i, candidate in enumerate(remaining):
            relevance = candidate.get("reranker_score", candidate["rrf_score"])
            max_sim = max(
                jaccard(candidate["hit"].content, chosen["hit"].content) for chosen in selected
            )
            mmr = lambda_param * relevance - (1 - lambda_param) * max_sim
            if mmr > best_mmr:
                best_mmr = mmr
                best_idx = i
        selected.append(remaining.pop(best_idx))
    return selected
