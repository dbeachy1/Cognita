"""SQLite metadata ownership for Workspace records and installation settings.

The Workspace manager coordinates runtime effects; this module owns only the
metadata layout and its transactional operations.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import uuid
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .release_identity import WORKSPACE_SCHEMA_RESET_REQUIRED, WORKSPACE_SCHEMA_VERSION

log = logging.getLogger("cognita.workspace")
CAPACITY_RESERVATION_TTL_SECONDS = 15 * 60


class WorkspaceError(RuntimeError):
    """A bounded, client-safe Workspace failure."""

    def __init__(self, reason: str, message: str = "Workspace operation failed", **fields: Any):
        super().__init__(message)
        self.reason = reason
        self.fields = fields


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


@dataclass(frozen=True, slots=True)
class WorkspaceRecord:
    workspace_id: str
    principal_id: str
    connector_id: str | None
    display_label: str
    state: str
    desired_state: str
    created_at: str
    last_accessed_at: str
    stopped_at: str | None
    pinned: bool
    retention_days: int | None
    deletion_due_at: str | None
    quota_bytes: int
    measured_allocated_bytes: int | None
    measured_apparent_bytes: int | None
    runtime_name: str
    runtime_generation: int
    last_error_code: str | None
    last_error_at: str | None
    revision: int
    credential_id: str | None = None
    surface_name: str | None = None
    owner_status: str = "active"
    usage_status: str = "unknown"
    measured_at: str | None = None
    host_path: str | None = None
    path_status: str = "not_reported"
    volume_name: str | None = None
    deletion_intent: str | None = None
    deletion_requested_at: str | None = None
    # A4 (DESIGN-12.18 SS3.4): who/what most recently changed this
    # Workspace's running state without a direct caller request -- set by
    # stop_idle ("idle_stop"), Admin stop ("admin_stop"/"emergency_stop"),
    # and the runtime-recovery path in start() ("vm_recovered"). Retention
    # delete removes the row entirely, so it records nothing there.
    last_auto_action: str | None = None
    last_auto_action_at: str | None = None

    def as_dict(self) -> dict[str, Any]:
        result = {name: getattr(self, name) for name in self.__dataclass_fields__}
        result["pinned"] = bool(result["pinned"])
        return result


class WorkspaceStateIncompatible(RuntimeError):
    """The Workspace metadata file on disk is not the layout this build owns.

    13.0 (DESIGN-13.0-DOCKER-REWRITE.md sections 5 and 8): the Workspace rows
    are disposable, so this store no longer migrates an older layout into the
    current one.  It is raised before any write, carries the exact reset
    command, and leaves the file exactly as it was found.
    """


def workspace_reset_command(target: str | None = None) -> str:
    """The command that discards and regenerates the Workspace state.

    The same image serves main, beta and test, so the deployment name comes
    from the environment the release tool writes into the target's env file
    (``COGNITA_RELEASE_TARGET``).  An unset value costs only the accuracy of
    this one sentence, so it degrades to a placeholder rather than naming the
    wrong deployment -- the same rule store.py applies to the index command.
    """
    name = target or os.environ.get("COGNITA_RELEASE_TARGET") or "<your target>"
    if name == "local":
        # Installer design 9: an install made by `./cognita install` uses the launcher.  19.6: named by
        # COGNITA_COMMAND (a Windows install types `cognita`); unset means `./cognita`.
        return f"{os.environ.get('COGNITA_COMMAND') or './cognita'} reset workspaces"
    return (
        f"python3 scripts/reset_disposable_state.py --target {name} "
        "--scope workspaces --apply"
    )


# Every table this file owns, and the one that is NOT disposable.
# `workspace_settings` holds the installation's Workspace policy (retention,
# quota, network mode, reserve) and survives a reset; DESIGN-13.0 section 5
# lists it under "Installation".  scripts/reset_disposable_state.py drops
# exactly DISPOSABLE_TABLES and nothing else; tests/test_reset_disposable_state
# .py asserts the two lists still agree.
DISPOSABLE_TABLES: tuple[str, ...] = (
    "workspaces",
    "workspace_leases",
    "workspace_jobs",
    "workspace_idempotency",
    "workspace_admin_idempotency",
    "workspace_growth_reservations",
    "workspace_delete_previews",
    "workspace_delete_apply_items",
    # 13.2.0: the layout stamp goes with the tables it describes. A reset drops
    # it and the next start writes a fresh one for the tables it just created.
    "workspace_schema",
)
PRESERVED_TABLES: tuple[str, ...] = ("workspace_settings",)


class WorkspaceMetadataStore:
    """SQLite metadata owner.  Guest contents remain solely runtime-owned."""

    # The whole layout, in one place, so the create-if-absent path and the
    # structural check below cannot drift apart: the check builds this same
    # script in a throwaway in-memory database and compares column sets.
    #
    # No SQL comments inside a CREATE TABLE: SQLite stores the statement text
    # verbatim and a later ALTER TABLE DROP COLUMN rewrites it, which fails
    # with 'incomplete input' when a trailing `--` comment is left behind
    # (found by the 13.2.0 in-place upgrade tests). Column notes go here:
    # `workspaces.last_auto_action` / `.last_auto_action_at` (13.1.0, A4,
    # DESIGN-12.18 section 3.4) are nullable with no default -- a Workspace
    # never auto-stopped or auto-recovered has neither.
    SCHEMA_SQL = """
        CREATE TABLE IF NOT EXISTS workspaces (
          workspace_id TEXT PRIMARY KEY, principal_id TEXT NOT NULL UNIQUE,
          connector_id TEXT, display_label TEXT NOT NULL, state TEXT NOT NULL,
          desired_state TEXT NOT NULL, created_at TEXT NOT NULL,
          last_accessed_at TEXT NOT NULL, stopped_at TEXT, pinned INTEGER NOT NULL DEFAULT 0,
          retention_days INTEGER, deletion_due_at TEXT, quota_bytes INTEGER NOT NULL,
          measured_allocated_bytes INTEGER NOT NULL DEFAULT 0,
          measured_apparent_bytes INTEGER NOT NULL DEFAULT 0, runtime_name TEXT NOT NULL,
          runtime_generation INTEGER NOT NULL DEFAULT 0, last_error_code TEXT,
          last_error_at TEXT, revision INTEGER NOT NULL DEFAULT 0,
          credential_id TEXT, surface_name TEXT, owner_status TEXT NOT NULL DEFAULT 'active',
          usage_status TEXT NOT NULL DEFAULT 'unknown', measured_at TEXT,
          host_path TEXT, path_status TEXT NOT NULL DEFAULT 'not_reported',
          volume_name TEXT, deletion_intent TEXT, deletion_requested_at TEXT,
          last_auto_action TEXT, last_auto_action_at TEXT
        );
        CREATE TABLE IF NOT EXISTS workspace_leases (
          workspace_id TEXT NOT NULL, lease_id TEXT PRIMARY KEY, kind TEXT NOT NULL,
          owner TEXT NOT NULL DEFAULT 'cognita', generation INTEGER NOT NULL DEFAULT 0,
          expires_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS workspace_jobs (
          job_id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL, request_digest TEXT NOT NULL,
          state TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
          runtime_job_id TEXT, UNIQUE(workspace_id, request_digest)
        );
        CREATE TABLE IF NOT EXISTS workspace_idempotency (
          workspace_id TEXT NOT NULL, operation_key TEXT NOT NULL, request_digest TEXT NOT NULL,
          response_json TEXT NOT NULL, created_at TEXT NOT NULL,
          PRIMARY KEY(workspace_id, operation_key)
        );
        CREATE TABLE IF NOT EXISTS workspace_settings (
          singleton INTEGER PRIMARY KEY CHECK(singleton=1), revision INTEGER NOT NULL,
          retention_days INTEGER NOT NULL, quota_bytes INTEGER NOT NULL,
          idle_stop_seconds INTEGER NOT NULL, host_reserve_bytes INTEGER NOT NULL,
          network_mode TEXT NOT NULL, network_rules_json TEXT NOT NULL,
          brave_enabled INTEGER NOT NULL, warning_threshold_percent INTEGER NOT NULL,
          max_running_workspaces INTEGER NOT NULL
        );
        INSERT OR IGNORE INTO workspace_settings VALUES(
          1,0,30,4294967296,1800,21474836480,'off','[]',0,80,4
        );
        CREATE TABLE IF NOT EXISTS workspace_admin_idempotency (
          operation_key TEXT PRIMARY KEY, request_digest TEXT NOT NULL,
          response_json TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS workspace_growth_reservations (
          reservation_id TEXT PRIMARY KEY, workspace_id TEXT, growth_bytes INTEGER NOT NULL,
          covered_active_growth_bytes INTEGER NOT NULL DEFAULT 0,
          state TEXT NOT NULL, created_at TEXT NOT NULL, expires_at TEXT NOT NULL,
          request_key TEXT UNIQUE
        );
        CREATE TABLE IF NOT EXISTS workspace_delete_previews (
          token_hash TEXT PRIMARY KEY, workspace_ids_json TEXT NOT NULL,
          revisions_json TEXT NOT NULL, targets_json TEXT NOT NULL,
          reclaim_bytes INTEGER, created_at TEXT NOT NULL, expires_at TEXT NOT NULL,
          state TEXT NOT NULL DEFAULT 'open'
        );
        CREATE TABLE IF NOT EXISTS workspace_delete_apply_items (
          operation_key TEXT NOT NULL, workspace_id TEXT NOT NULL,
          outcome_json TEXT NOT NULL, created_at TEXT NOT NULL,
          PRIMARY KEY(operation_key, workspace_id)
        );
        CREATE TABLE IF NOT EXISTS workspace_schema (
          singleton INTEGER PRIMARY KEY CHECK(singleton=1),
          version INTEGER NOT NULL, stamped_at TEXT NOT NULL
        );
    """

    def __init__(self, path: str | Path, *, read_only: bool = False):
        self.path = Path(path)
        if not read_only:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(
            self.path.as_uri() + "?mode=ro" if read_only else self.path,
            timeout=30, check_same_thread=False, uri=read_only,
        )
        self._db.row_factory = sqlite3.Row
        if read_only:
            self._db.execute("PRAGMA query_only=ON")
            return
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA foreign_keys=ON")
        self._db.execute("PRAGMA busy_timeout=30000")
        # Every statement is CREATE TABLE IF NOT EXISTS / INSERT OR IGNORE, so
        # this creates a fresh file and leaves an existing matching one alone.
        # It cannot repair a table that exists with the wrong columns, which is
        # what the reconciliation immediately below is for.
        #
        # History. Up to 12.x this constructor ran open-ended schema upgrades
        # (ALTER TABLE branches for a dozen columns plus repair UPDATEs) on
        # rows a reset discards in a second. 13.0 (DESIGN-13.0 section 8)
        # removed all of it: an older layout was reported, never converted.
        # 13.2.0 (Doug, 2026-09-22, after the first real layout change cost
        # a reset for two nullable columns) adds the stamp: the file records
        # its schema version, a build that says its change is additive may
        # add the missing columns in place, and everything else still resets.
        self._reconcile_layout()
        self._db.executescript(self.SCHEMA_SQL)
        self._stamp()
        self._db.commit()

    @classmethod
    def expected_layout(cls) -> dict[str, dict[str, tuple[str, bool, str | None, bool]]]:
        """This build's layout, read back from SCHEMA_SQL run into memory.

        {table: {column: (type, not_null, default_sql, primary_key)}}.  Built
        from the DDL itself so the check and the create path cannot drift.
        """
        probe = sqlite3.connect(":memory:")
        try:
            probe.executescript(cls.SCHEMA_SQL)
            return {
                str(row[0]): {
                    str(column[1]): (str(column[2]), bool(column[3]), column[4], bool(column[5]))
                    for column in probe.execute(f"PRAGMA table_info({row[0]})")
                }
                for row in probe.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
        finally:
            probe.close()

    def stamped_version(self) -> int | None:
        """The schema version the file records, or None for a file from before
        13.2.0 (or an empty one)."""
        row = self._db.execute(
            "SELECT version FROM workspace_schema WHERE singleton=1"
        ).fetchone() if self._db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='workspace_schema'"
        ).fetchone() else None
        return int(row[0]) if row is not None else None

    def _stamp(self) -> None:
        self._db.execute(
            "INSERT INTO workspace_schema(singleton, version, stamped_at) VALUES(1, ?, ?) "
            "ON CONFLICT(singleton) DO UPDATE SET version=excluded.version, stamped_at=excluded.stamped_at",
            (WORKSPACE_SCHEMA_VERSION, datetime.now(UTC).isoformat(timespec="seconds")),
        )

    def _refuse(self, detail: str) -> None:
        log.error(
            "workspace metadata layout is not this build's",
            extra={"event": "workspace_store_layout_incompatible", "path": str(self.path),
                   "problems": detail},
        )
        raise WorkspaceStateIncompatible(
            f"The Workspace metadata at {self.path} was written by a different build "
            f"({detail}). Workspace state is disposable and this build does not migrate "
            f"it. Discard and regenerate it with: {workspace_reset_command()}"
        )

    def _reconcile_layout(self) -> None:
        """Decide what an existing file gets: nothing, added columns, or a reset.

        Runs BEFORE the create-if-absent script, so a refused file is reported
        without a single table having been touched.  The rule (Doug,
        2026-09-22): a file at this build's version is left alone; a file at a
        higher version came from a newer build and is refused; an older file
        -- older stamp, or no stamp and columns missing -- is RESET unless
        ``WORKSPACE_SCHEMA_RESET_REQUIRED`` is explicitly False, in which
        case the missing columns are added in place and the file is
        restamped.  A file with no stamp whose tables already match (13.1.0
        wrote exactly this layout, minus the stamp) is simply stamped.  A
        missing table is never a problem: the script that follows creates it.

        The in-place path can only add a column SQLite can add: nullable, or
        NOT NULL with a default, and never a primary key.  A build that claims
        "no reset" for a change SQLite cannot make in place is refused with a
        message that says the flag is wrong, rather than half-upgrading.
        """
        expected = self.expected_layout()
        stamped = self.stamped_version()
        missing: dict[str, list[str]] = {}
        for table in sorted(expected):
            found = {str(row[1]) for row in self._db.execute(f"PRAGMA table_info({table})")}
            if not found:
                continue
            absent = sorted(set(expected[table]) - found)
            if absent:
                missing[table] = absent
        detail = "; ".join(f"{table}: missing column(s) {', '.join(columns)}" for table, columns in missing.items())
        if stamped is not None and stamped > WORKSPACE_SCHEMA_VERSION:
            self._refuse(f"schema version {stamped} is newer than this build's {WORKSPACE_SCHEMA_VERSION}")
        if stamped == WORKSPACE_SCHEMA_VERSION:
            if missing:
                self._refuse(f"schema version {stamped} but {detail}")
            log.debug(
                "workspace metadata layout verified",
                extra={"event": "workspace_store_layout_ok", "path": str(self.path),
                       "tables": len(expected), "schema_version": stamped},
            )
            return
        if not missing:
            log.info(
                "workspace metadata layout matches; stamping schema version %s (file had %s)",
                WORKSPACE_SCHEMA_VERSION, "no stamp" if stamped is None else stamped,
                extra={"event": "workspace_store_layout_stamped", "path": str(self.path),
                       "schema_version": WORKSPACE_SCHEMA_VERSION, "previous": stamped},
            )
            return
        older = "no stamp" if stamped is None else f"schema version {stamped}"
        if WORKSPACE_SCHEMA_RESET_REQUIRED:
            self._refuse(f"{older}, this build is {WORKSPACE_SCHEMA_VERSION} and requires a reset; {detail}")
        for table, columns in missing.items():
            for column in columns:
                column_type, not_null, default, primary = expected[table][column]
                if primary or (not_null and default is None):
                    self._refuse(
                        f"{older}, this build is {WORKSPACE_SCHEMA_VERSION} and says no reset is "
                        f"required, but {table}.{column} cannot be added in place (a primary key or "
                        f"NOT NULL without a default); WORKSPACE_SCHEMA_RESET_REQUIRED is wrong for "
                        f"this change"
                    )
                ddl = f"ALTER TABLE {table} ADD COLUMN {column} {column_type}".rstrip()
                if not_null:
                    ddl += " NOT NULL"
                if default is not None:
                    ddl += f" DEFAULT {default}"
                self._db.execute(ddl)
                log.info(
                    "workspace metadata layout upgraded in place: %s", ddl,
                    extra={"event": "workspace_store_layout_upgraded", "path": str(self.path),
                           "table": table, "column": column, "from": stamped,
                           "schema_version": WORKSPACE_SCHEMA_VERSION},
                )
        log.info(
            "workspace metadata layout upgraded in place from %s to schema version %s (%s)",
            older, WORKSPACE_SCHEMA_VERSION, detail,
            extra={"event": "workspace_store_layout_upgraded_done", "path": str(self.path),
                   "previous": stamped, "schema_version": WORKSPACE_SCHEMA_VERSION},
        )

    def close(self) -> None:
        with self._lock:
            self._db.close()

    @contextmanager
    def transaction(self):
        with self._lock:
            try:
                self._db.execute("BEGIN IMMEDIATE")
                yield self._db
                self._db.commit()
            except Exception:
                self._db.rollback()
                raise

    @staticmethod
    def _record(row: sqlite3.Row | None) -> WorkspaceRecord | None:
        if row is None:
            return None
        values = dict(row)
        if values.get("usage_status", "unknown") in {"unknown", "error", "stale"}:
            if values.get("measured_allocated_bytes") == 0:
                values["measured_allocated_bytes"] = None
            if values.get("measured_apparent_bytes") == 0:
                values["measured_apparent_bytes"] = None
        return WorkspaceRecord(**values)

    def get_by_principal(self, principal_id: str) -> WorkspaceRecord | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM workspaces WHERE principal_id=?", (principal_id,)).fetchone()
        return self._record(row)

    def get(self, workspace_id: str) -> WorkspaceRecord | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM workspaces WHERE workspace_id=?", (workspace_id,)).fetchone()
        return self._record(row)

    def create(self, principal_id: str, connector_id: str | None, label: str, *, now: str, quota_bytes: int, retention_days: int) -> WorkspaceRecord:
        wid = str(uuid.uuid4())
        due = (datetime.fromisoformat(now) + timedelta(days=retention_days)).isoformat(timespec="seconds")
        with self.transaction() as db:
            db.execute("""INSERT INTO workspaces
                (workspace_id,principal_id,connector_id,display_label,state,desired_state,created_at,last_accessed_at,
                 retention_days,deletion_due_at,quota_bytes,runtime_name)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (wid, principal_id, connector_id, label, "creating", "running", now, now,
                 retention_days, due, quota_bytes, f"cognita-ws-{wid}"))
        return self.get(wid)  # type: ignore[return-value]

    def update(self, workspace_id: str, **values: Any) -> WorkspaceRecord:
        allowed = set(WorkspaceRecord.__dataclass_fields__) - {"workspace_id"}
        values = {key: value for key, value in values.items() if key in allowed}
        if not values:
            record = self.get(workspace_id)
            if record is None:
                raise WorkspaceError("path_unavailable", "Workspace was not found")
            return record
        with self.transaction() as db:
            db.execute(
                f"UPDATE workspaces SET {','.join(f'{key}=?' for key in values)},revision=revision+1 WHERE workspace_id=?",
                (*values.values(), workspace_id),
            )
        record = self.get(workspace_id)
        if record is None:
            raise WorkspaceError("path_unavailable", "Workspace was not found")
        return record

    def delete_if_revision(
        self, workspace_id: str, expected_revision: int, *,
        operation_key: str | None = None, request_digest: str | None = None,
        response: dict[str, Any] | None = None,
    ) -> bool:
        """Delete one metadata row, optionally recording its replay atomically.

        The lifecycle worker must never leave a successful runtime deletion with
        an unrecorded Admin idempotency result.  Keeping the row delete and the
        replay record in one SQLite transaction makes a crash before commit
        recoverable: either both are visible or neither is.
        """
        with self.transaction() as db:
            cursor = db.execute(
                "DELETE FROM workspaces WHERE workspace_id=? AND revision=?",
                (workspace_id, expected_revision),
            )
            if cursor.rowcount != 1:
                return False
            if operation_key is not None:
                if not request_digest or response is None:
                    raise WorkspaceError("invalid_arguments", "complete idempotency replay data is required")
                try:
                    # Different Workspace queues may race with the same Admin
                    # token. Never replace the first committed replay: a
                    # collision must also roll back this metadata deletion.
                    db.execute(
                        "INSERT INTO workspace_admin_idempotency "
                        "(operation_key,request_digest,response_json,created_at) VALUES(?,?,?,?)",
                        (operation_key, request_digest,
                         json.dumps(response, separators=(",", ":")), _utc_now()),
                    )
                except sqlite3.IntegrityError as exc:
                    raise WorkspaceError("path_conflict", "idempotency token was reused") from exc
        return True

    def list(self) -> list[WorkspaceRecord]:
        with self._lock:
            rows = self._db.execute("SELECT * FROM workspaces ORDER BY created_at, workspace_id").fetchall()
        return [self._record(row) for row in rows if row is not None]  # type: ignore[misc]

    def retention_candidates(self, *, now: str, after_workspace_id: str = "", limit: int = 32) -> list[WorkspaceRecord]:
        """Page only durable delete intents; ordinary idle deadlines are not authority."""
        if limit < 1 or limit > 256:
            raise WorkspaceError("invalid_arguments", "retention batch limit is invalid")
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM workspaces WHERE workspace_id>? AND "
                "(deletion_intent='delete_now' OR "
                "(deletion_intent='normal' AND owner_status='tombstoned' AND pinned=0 "
                "AND deletion_requested_at IS NOT NULL AND deletion_due_at<=?)) "
                "ORDER BY workspace_id LIMIT ?",
                (after_workspace_id, now, limit),
            ).fetchall()
        return [self._record(row) for row in rows if row is not None]  # type: ignore[misc]

    def update_measurement(
        self,
        workspace_id: str,
        *,
        allocated_bytes: int | None = None,
        apparent_bytes: int | None = None,
        measured_at: str | None = None,
        usage_status: str = "fresh",
        host_path: str | None = None,
        path_status: str = "not_reported",
        volume_name: str | None = None,
        bump_revision: bool = True,
    ) -> WorkspaceRecord:
        """Persist a bounded runtime measurement without treating unknown as 0."""
        if usage_status not in {"fresh", "stale", "unknown", "error"}:
            raise WorkspaceError("invalid_arguments", "invalid usage measurement status")
        for value in (allocated_bytes, apparent_bytes):
            if value is not None and (isinstance(value, bool) or value < 0):
                raise WorkspaceError("invalid_arguments", "usage bytes must be non-negative")
        if path_status not in {"verified", "absent", "not_reported", "stale"}:
            raise WorkspaceError("invalid_arguments", "invalid runtime path status")
        values: dict[str, Any] = {
            "usage_status": usage_status, "measured_at": measured_at or _utc_now(),
            "path_status": path_status,
        }
        # Preserve the previous numeric sample on an unknown probe.  The
        # projection uses usage_status to decide whether that sample is usable.
        if allocated_bytes is not None:
            values["measured_allocated_bytes"] = int(allocated_bytes)
        if apparent_bytes is not None:
            values["measured_apparent_bytes"] = int(apparent_bytes)
        if host_path is not None or path_status in {"absent", "not_reported"}:
            values["host_path"] = host_path
        if volume_name is not None:
            values["volume_name"] = volume_name
        if bump_revision:
            return self.update(workspace_id, **values)
        # Runtime usage/path observations are part of the preview snapshot but
        # are not lifecycle intent changes.  Keep the caller's expected
        # revision stable while writing the complete observation atomically;
        # preview/apply still compare every observed field before an effect.
        with self.transaction() as db:
            cursor = db.execute(
                f"UPDATE workspaces SET {','.join(f'{key}=?' for key in values)} WHERE workspace_id=?",
                (*values.values(), workspace_id),
            )
            if cursor.rowcount != 1:
                raise WorkspaceError("path_unavailable", "Workspace was not found")
        record = self.get(workspace_id)
        if record is None:
            raise WorkspaceError("path_unavailable", "Workspace was not found")
        return record

    def set_owner_status(self, workspace_id: str, status: str, *, error: str | None = None) -> WorkspaceRecord:
        if status not in {"active", "revoked", "tombstoned", "orphaned"}:
            raise WorkspaceError("invalid_arguments", "invalid Workspace owner status")
        values: dict[str, Any] = {"owner_status": status}
        if error is not None:
            values.update(last_error_code=error, last_error_at=_utc_now())
        return self.update(workspace_id, **values)

    def reserve_growth(
        self,
        growth_bytes: int,
        *,
        free_bytes_supplier: Callable[[], int],
        reserve_bytes: int,
        request_key: str | None = None,
        workspace_id: str | None = None,
        covered_active_growth_bytes: int = 0,
        active_growth_supplier: Callable[[], int] | None = None,
        active_growth_always: bool = False,
        ttl_seconds: int = CAPACITY_RESERVATION_TTL_SECONDS,
    ) -> str:
        """Atomically reserve possible growth under the mounted-filesystem cap."""
        if isinstance(growth_bytes, bool) or growth_bytes < 0:
            raise WorkspaceError("invalid_arguments", "growth reservation must be non-negative")
        if isinstance(covered_active_growth_bytes, bool) or covered_active_growth_bytes < 0:
            raise WorkspaceError("invalid_arguments", "covered active growth must be non-negative")
        if covered_active_growth_bytes > growth_bytes:
            raise WorkspaceError("invalid_arguments", "covered active growth exceeds reservation")
        if reserve_bytes < 0 or ttl_seconds <= 0:
            raise WorkspaceError("invalid_arguments", "invalid capacity reservation policy")
        now = _utc_now()
        expires = (datetime.now(UTC) + timedelta(seconds=ttl_seconds)).isoformat(timespec="seconds")
        with self.transaction() as db:
            db.execute("DELETE FROM workspace_growth_reservations WHERE state='held' AND expires_at < ?", (now,))
            if request_key:
                existing = db.execute(
                    "SELECT reservation_id FROM workspace_growth_reservations WHERE request_key=? AND state='held'",
                    (request_key,),
                ).fetchone()
                if existing is not None:
                    return str(existing[0])
            try:
                free = int(free_bytes_supplier())
            except Exception as exc:
                raise WorkspaceError("capacity_unavailable", "Workspace host capacity is unavailable") from exc
            committed = int(db.execute(
                "SELECT COALESCE(SUM(growth_bytes),0) FROM workspace_growth_reservations WHERE state='held'"
            ).fetchone()[0])
            uncovered_active_growth = 0
            if active_growth_supplier is not None:
                try:
                    active_growth = int(active_growth_supplier())
                except Exception as exc:
                    raise WorkspaceError(
                        "capacity_unavailable", "Workspace active growth is unavailable"
                    ) from exc
                if active_growth < 0:
                    raise WorkspaceError("capacity_unavailable", "Workspace active growth is invalid")
                already_covered = int(db.execute(
                    "SELECT COALESCE(SUM(covered_active_growth_bytes),0) "
                    "FROM workspace_growth_reservations WHERE state='held'"
                ).fetchone()[0])
                uncovered_active_growth = (
                    active_growth
                    if active_growth_always
                    else max(0, active_growth - already_covered)
                )
            requested_growth = growth_bytes + uncovered_active_growth
            covered_growth = covered_active_growth_bytes + uncovered_active_growth
            if free - reserve_bytes - committed - requested_growth < 0:
                raise WorkspaceError(
                    "capacity_busy", "Workspace host reserve cannot admit growth",
                    free_bytes=free, reserve_bytes=reserve_bytes,
                    committed_growth_bytes=committed, requested_growth_bytes=requested_growth,
                )
            reservation_id = str(uuid.uuid4())
            db.execute(
                "INSERT INTO workspace_growth_reservations(reservation_id,workspace_id,growth_bytes,covered_active_growth_bytes,state,created_at,expires_at,request_key) VALUES(?,?,?,?,?,?,?,?)",
                (reservation_id, workspace_id, requested_growth, covered_growth, "held", now, expires, request_key),
            )
            return reservation_id

    def release_growth(self, reservation_id: str) -> None:
        with self.transaction() as db:
            db.execute(
                "UPDATE workspace_growth_reservations SET state='released', request_key=NULL WHERE reservation_id=? AND state='held'",
                (reservation_id,),
            )

    def renew_growth(
        self,
        reservation_id: str,
        *,
        ttl_seconds: int = CAPACITY_RESERVATION_TTL_SECONDS,
    ) -> None:
        """Extend a live growth hold without reviving an expired reservation."""
        if not isinstance(reservation_id, str) or not reservation_id:
            raise WorkspaceError("invalid_arguments", "invalid capacity reservation")
        if ttl_seconds <= 0:
            raise WorkspaceError("invalid_arguments", "invalid capacity reservation policy")
        now = _utc_now()
        expires = (datetime.now(UTC) + timedelta(seconds=ttl_seconds)).isoformat(timespec="seconds")
        with self.transaction() as db:
            cursor = db.execute(
                "UPDATE workspace_growth_reservations SET expires_at=? "
                "WHERE reservation_id=? AND state='held' AND expires_at>=?",
                (expires, reservation_id, now),
            )
            if cursor.rowcount != 1:
                raise WorkspaceError("capacity_busy", "Workspace growth reservation expired")

    def held_growth_bytes(self) -> int:
        with self._lock:
            row = self._db.execute(
                "SELECT COALESCE(SUM(growth_bytes),0) FROM workspace_growth_reservations WHERE state='held' AND expires_at>=?",
                (_utc_now(),),
            ).fetchone()
        return int(row[0] if row else 0)

    def held_active_growth_bytes(self) -> int:
        """Return active-guest growth already included in held reservations."""
        with self._lock:
            row = self._db.execute(
                "SELECT COALESCE(SUM(covered_active_growth_bytes),0) "
                "FROM workspace_growth_reservations "
                "WHERE state='held' AND expires_at>=?",
                (_utc_now(),),
            ).fetchone()
        return int(row[0] if row else 0)

    def save_delete_preview(
        self,
        token_hash: str,
        workspace_ids: list[str],
        revisions: dict[str, int],
        targets: list[dict[str, Any]],
        reclaim_bytes: int | None,
        *,
        expires_at: str,
    ) -> None:
        with self.transaction() as db:
            db.execute(
                "INSERT INTO workspace_delete_previews(token_hash,workspace_ids_json,revisions_json,targets_json,reclaim_bytes,created_at,expires_at,state) VALUES(?,?,?,?,?,?,?,'open')",
                (token_hash, json.dumps(workspace_ids, separators=(",", ":")),
                 json.dumps(revisions, separators=(",", ":")),
                 json.dumps(targets, separators=(",", ":")), reclaim_bytes,
                 _utc_now(), expires_at),
            )

    def get_delete_preview(self, token_hash: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM workspace_delete_previews WHERE token_hash=?", (token_hash,)
            ).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["workspace_ids"] = json.loads(result.pop("workspace_ids_json"))
        result["revisions"] = json.loads(result.pop("revisions_json"))
        result["targets"] = json.loads(result.pop("targets_json"))
        return result

    def consume_delete_preview(self, token_hash: str) -> bool:
        with self.transaction() as db:
            cursor = db.execute(
                "UPDATE workspace_delete_previews SET state='consumed' WHERE token_hash=? AND state='open'",
                (token_hash,),
            )
        return cursor.rowcount == 1

    def save_delete_apply_item(self, operation_key: str, workspace_id: str,
                               outcome: dict[str, Any]) -> None:
        """Persist one bulk-delete outcome before the next destructive effect."""
        with self.transaction() as db:
            db.execute(
                "INSERT OR REPLACE INTO workspace_delete_apply_items "
                "(operation_key,workspace_id,outcome_json,created_at) VALUES(?,?,?,?)",
                (operation_key, workspace_id,
                 json.dumps(outcome, separators=(",", ":")), _utc_now()),
            )

    def delete_apply_items(self, operation_key: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute(
                "SELECT outcome_json FROM workspace_delete_apply_items "
                "WHERE operation_key=? ORDER BY created_at, workspace_id",
                (operation_key,),
            ).fetchall()
        return [json.loads(row[0]) for row in rows]

    def lease(self, workspace_id: str, kind: str, seconds: int = 120, *, owner: str = "cognita", generation: int = 0) -> str:
        now = datetime.now(UTC)
        lease_id = str(uuid.uuid4())
        expires = (now + timedelta(seconds=seconds)).isoformat(timespec="seconds")
        with self.transaction() as db:
            db.execute("DELETE FROM workspace_leases WHERE expires_at < ?", (now.isoformat(),))
            if db.execute("SELECT 1 FROM workspace_leases WHERE workspace_id=? AND kind=? LIMIT 1", (workspace_id, kind)).fetchone() is not None:
                raise WorkspaceError("capacity_busy", "Workspace has an active operation")
            db.execute("INSERT INTO workspace_leases(workspace_id,lease_id,kind,owner,generation,expires_at) VALUES(?,?,?,?,?,?)", (workspace_id, lease_id, kind, owner, generation, expires))
        return lease_id

    def release_lease(self, lease_id: str) -> None:
        with self.transaction() as db:
            db.execute("DELETE FROM workspace_leases WHERE lease_id=?", (lease_id,))

    def has_live_lease(self, workspace_id: str) -> bool:
        with self._lock:
            row = self._db.execute("SELECT 1 FROM workspace_leases WHERE workspace_id=? AND expires_at>=? LIMIT 1", (workspace_id, _utc_now())).fetchone()
        return row is not None

    def idempotent(self, workspace_id: str, key: str, digest: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute("SELECT request_digest,response_json FROM workspace_idempotency WHERE workspace_id=? AND operation_key=?", (workspace_id, key)).fetchone()
        if row is None:
            return None
        if row["request_digest"] != digest:
            raise WorkspaceError("path_conflict", "idempotency key was reused with different arguments")
        return json.loads(row["response_json"])

    def save_idempotent(self, workspace_id: str, key: str, digest: str, response: dict[str, Any]) -> None:
        with self.transaction() as db:
            db.execute("INSERT OR REPLACE INTO workspace_idempotency VALUES(?,?,?,?,?)", (workspace_id, key, digest, json.dumps(response, separators=(",", ":")), _utc_now()))

    def active_job(self, workspace_id: str) -> sqlite3.Row | None:
        with self._lock:
            return self._db.execute(
                "SELECT * FROM workspace_jobs WHERE workspace_id=? AND state IN ('queued','running') ORDER BY created_at DESC LIMIT 1",
                (workspace_id,),
            ).fetchone()

    def settings(self) -> dict[str, Any]:
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM workspace_settings WHERE singleton=1"
            ).fetchone()
        if row is None:
            raise WorkspaceError("internal_error", "Workspace settings are unavailable")
        result = dict(row)
        result.pop("singleton", None)
        result["network_rules"] = json.loads(result.pop("network_rules_json"))
        result["brave_enabled"] = bool(result["brave_enabled"])
        return result

    def update_settings(self, expected_revision: int, values: dict[str, Any]) -> dict[str, Any]:
        allowed = {
            "retention_days", "quota_bytes", "idle_stop_seconds", "host_reserve_bytes",
            "network_mode", "network_rules", "brave_enabled", "warning_threshold_percent",
            "max_running_workspaces",
        }
        if set(values) - allowed:
            raise WorkspaceError("invalid_arguments", "Workspace setting is unsupported")
        persisted = dict(values)
        if "network_rules" in persisted:
            persisted["network_rules_json"] = json.dumps(
                persisted.pop("network_rules"), separators=(",", ":"), ensure_ascii=False
            )
        if "brave_enabled" in persisted:
            persisted["brave_enabled"] = int(bool(persisted["brave_enabled"]))
        assignments = ",".join(f"{key}=?" for key in persisted)
        with self.transaction() as db:
            cursor = db.execute(
                f"UPDATE workspace_settings SET {assignments}{',' if assignments else ''}revision=revision+1 "
                "WHERE singleton=1 AND revision=?",
                (*persisted.values(), expected_revision),
            )
            if cursor.rowcount != 1:
                raise WorkspaceError("path_conflict", "Workspace settings revision changed")
        return self.settings()

    def admin_idempotent(self, key: str | None, digest: str) -> dict[str, Any] | None:
        if key is None:
            return None
        with self._lock:
            row = self._db.execute(
                "SELECT request_digest,response_json FROM workspace_admin_idempotency "
                "WHERE operation_key=?", (key,),
            ).fetchone()
        if row is None:
            return None
        if row["request_digest"] != digest:
            raise WorkspaceError("path_conflict", "idempotency token was reused")
        return json.loads(row["response_json"])

    def save_admin_idempotent(self, key: str | None, digest: str,
                              response: dict[str, Any]) -> None:
        if key is None:
            return
        with self.transaction() as db:
            db.execute(
                "INSERT INTO workspace_admin_idempotency VALUES(?,?,?,?)",
                (key, digest, json.dumps(response, separators=(",", ":")), _utc_now()),
            )

    def update_job_state(self, workspace_id: str, job_id: str, state: str) -> None:
        with self.transaction() as db:
            cursor = db.execute(
                "UPDATE workspace_jobs SET state=?,updated_at=? WHERE workspace_id=? AND job_id=?",
                (state, _utc_now(), workspace_id, job_id),
            )
            if cursor.rowcount != 1:
                raise WorkspaceError("path_unavailable", "Workspace job was not found")

    def cache_job_state(self, workspace_id: str, job_id: str, state: str) -> bool:
        """Cache a state observed after the broker authoritatively found a job.

        ``workspace_jobs`` also backs the one-active-job admission guard; it is
        not broker job history. A later identical command can replace an older
        terminal row because this disposable table uniquely indexes request
        digests. A missing cache row therefore cannot invalidate a broker
        result. Callers use this best-effort method only after a successful
        broker response; a false return means the observation was not cached.
        """
        with self.transaction() as db:
            cursor = db.execute(
                "UPDATE workspace_jobs SET state=?,updated_at=? WHERE workspace_id=? AND job_id=?",
                (state, _utc_now(), workspace_id, job_id),
            )
            return cursor.rowcount == 1
