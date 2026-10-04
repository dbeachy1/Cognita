"""Durable broker-owned lifecycle, leases, and job intent state.

Microsandbox owns guest state.  This database intentionally stores only
identity, lifecycle intent, bounded observations, and job control metadata.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from threading import RLock
from typing import Any, Iterator
from uuid import UUID, uuid4

log = logging.getLogger("cognita.runtime_broker.state")


@dataclass(frozen=True)
class WorkspaceRecord:
    workspace_id: UUID
    state: str
    desired_state: str
    runtime_generation: int
    quota_bytes: int
    measured_allocated_bytes: int
    measured_apparent_bytes: int
    pinned: bool
    lease_owner: str | None = None
    lease_expires_at: float | None = None
    last_error_code: str | None = None


@dataclass(frozen=True)
class JobRecord:
    job_id: UUID
    workspace_id: UUID
    state: str
    pid: int | None
    process_start_token: str | None
    process_group_id: int | None
    deadline: float
    output_first_stdout: int
    output_first_stderr: int
    output_total_stdout: int
    output_total_stderr: int
    metadata: dict[str, Any]


class RuntimeStateIncompatible(RuntimeError):
    """The broker state file on disk is not the layout this build owns.

    13.0 (DESIGN-13.0-DOCKER-REWRITE.md sections 5 and 8): everything in this
    database is disposable, so the broker no longer migrates an older layout.
    Raised before any table is created or altered, and carries the exact reset
    command.
    """


def reset_command(target: str | None = None) -> str:
    """The command that discards and regenerates the Workspace state.

    The broker container is the same image for main, beta and test, so the
    deployment name comes from ``COGNITA_RELEASE_TARGET`` in its environment.
    An unset value costs only this sentence's accuracy, so it degrades to a
    placeholder rather than naming the wrong deployment.
    """
    name = target or os.environ.get("COGNITA_RELEASE_TARGET") or "<your target>"
    if name == "local":
        # 19.6: an install made by the ./cognita installer has no scripts path to remember, and the
        # folders fragment now passes this container COGNITA_RELEASE_TARGET and COGNITA_COMMAND (a
        # Windows install types `cognita`).  Same wording as the app's own hint (workspace_store.py).
        return f"{os.environ.get('COGNITA_COMMAND') or './cognita'} reset workspaces"
    return (
        f"python3 scripts/reset_disposable_state.py --target {name} "
        "--scope workspaces --apply"
    )


class RuntimeStateStore:
    """SQLite WAL state with short transactions and reclaimable leases."""

    # One copy of the layout, so the create-if-absent path and the structural
    # check cannot drift apart: the check runs this same script into a
    # throwaway in-memory database and compares the column sets it produces.
    SCHEMA_SQL = """
            CREATE TABLE IF NOT EXISTS workspaces (
                workspace_id TEXT PRIMARY KEY,
                state TEXT NOT NULL,
                desired_state TEXT NOT NULL,
                runtime_generation INTEGER NOT NULL DEFAULT 0,
                quota_bytes INTEGER NOT NULL,
                measured_allocated_bytes INTEGER NOT NULL DEFAULT 0,
                measured_apparent_bytes INTEGER NOT NULL DEFAULT 0,
                pinned INTEGER NOT NULL DEFAULT 0,
                volume_creation_attempted INTEGER NOT NULL DEFAULT 0,
                lease_owner TEXT,
                lease_expires_at REAL,
                last_error_code TEXT
            );
            CREATE TABLE IF NOT EXISTS broker_metadata (
                key TEXT PRIMARY KEY,
                value INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS jobs (
                job_id TEXT PRIMARY KEY,
                workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id),
                state TEXT NOT NULL,
                pid INTEGER,
                process_start_token TEXT,
                process_group_id INTEGER,
                deadline REAL NOT NULL,
                stdout_first INTEGER NOT NULL DEFAULT 0,
                stderr_first INTEGER NOT NULL DEFAULT 0,
                stdout_total INTEGER NOT NULL DEFAULT 0,
                stderr_total INTEGER NOT NULL DEFAULT 0,
                metadata TEXT NOT NULL DEFAULT '{}'
            );
            CREATE INDEX IF NOT EXISTS jobs_workspace_state ON jobs(workspace_id, state);
    """

    def __init__(self, path: str = ":memory:") -> None:
        self.path = path
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._lock = RLock()
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA foreign_keys=ON")
        self._db.execute("PRAGMA busy_timeout=5000")
        # Superseded history (13.0, DESIGN-13.0 section 8): up to 12.x this
        # constructor followed the create-if-absent script with a schema
        # upgrade that added `workspaces.volume_creation_attempted` with a
        # DEFAULT of 1, because an older row might already own a volume even
        # while its last observed sandbox state was `absent`, and recovery
        # must not create an empty replacement volume underneath it.  Broker
        # state is disposable, so an older file is now reported rather than
        # converted; `claim_initial_volume_creation` still enforces the
        # one-shot rule for every row this build writes.
        self._verify_layout()
        self._db.executescript(self.SCHEMA_SQL)

    def _verify_layout(self) -> None:
        """Refuse an existing file whose tables are not this build's layout.

        Runs BEFORE the create-if-absent script, so an incompatible file is
        reported with nothing created or altered.  A table this file does not
        have yet is created empty below and is not a problem; a table that is
        present with columns missing predates this build, and every row here
        is disposable, so the answer is the reset command.
        """
        probe = sqlite3.connect(":memory:")
        try:
            probe.executescript(self.SCHEMA_SQL)
            expected = {
                str(row[0]): {str(column[1]) for column in probe.execute(f"PRAGMA table_info({row[0]})")}
                for row in probe.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
        finally:
            probe.close()
        problems: list[str] = []
        for table in sorted(expected):
            found = {str(row[1]) for row in self._db.execute(f"PRAGMA table_info({table})")}
            if not found:
                continue
            missing = sorted(expected[table] - found)
            if missing:
                problems.append(f"{table}: missing column(s) {', '.join(missing)}")
        if not problems:
            log.debug(
                "broker state layout verified",
                extra={"event": "broker_state_layout_ok", "path": self.path, "tables": len(expected)},
            )
            return
        detail = "; ".join(problems)
        log.error(
            "broker state layout is not this build's",
            extra={"event": "broker_state_layout_incompatible", "path": self.path,
                   "problems": detail},
        )
        raise RuntimeStateIncompatible(
            f"The broker state at {self.path} was written by a different build ({detail}). "
            "Workspace state is disposable and this build does not migrate it. Discard and "
            f"regenerate it with: {reset_command()}"
        )

    def advance_runtime_generation(self) -> int:
        """Persist a monotonically increasing generation for each broker process."""
        with self.transaction() as db:
            row = db.execute(
                "SELECT value FROM broker_metadata WHERE key='runtime_generation'"
            ).fetchone()
            generation = 0 if row is None else int(row["value"]) + 1
            db.execute(
                "INSERT INTO broker_metadata(key,value) VALUES('runtime_generation',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (generation,),
            )
        return generation

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                yield self._db
            except BaseException:
                self._db.rollback()
                raise
            else:
                self._db.commit()

    def ensure_workspace(self, workspace_id: UUID, *, quota_bytes: int) -> WorkspaceRecord:
        with self.transaction() as db:
            db.execute(
                "INSERT OR IGNORE INTO workspaces("
                "workspace_id,state,desired_state,quota_bytes,volume_creation_attempted) "
                "VALUES (?, 'absent', 'stopped', ?, 0)", (str(workspace_id), quota_bytes)
            )
        return self.workspace(workspace_id)

    def claim_initial_volume_creation(self, workspace_id: UUID) -> bool:
        """Allow the first attempt once, before any SDK side effect occurs."""
        with self.transaction() as db:
            cursor = db.execute(
                "UPDATE workspaces SET volume_creation_attempted=1 "
                "WHERE workspace_id=? AND volume_creation_attempted=0 AND state='absent'",
                (str(workspace_id),),
            )
            return cursor.rowcount == 1

    def workspace(self, workspace_id: UUID) -> WorkspaceRecord:
        with self._lock:
            row = self._db.execute("SELECT * FROM workspaces WHERE workspace_id=?", (str(workspace_id),)).fetchone()
        if row is None:
            raise KeyError(workspace_id)
        return WorkspaceRecord(
            UUID(row["workspace_id"]), row["state"], row["desired_state"], row["runtime_generation"],
            row["quota_bytes"], row["measured_allocated_bytes"], row["measured_apparent_bytes"],
            bool(row["pinned"]), row["lease_owner"], row["lease_expires_at"], row["last_error_code"]
        )

    def workspaces(self) -> list[WorkspaceRecord]:
        with self._lock:
            rows = self._db.execute("SELECT * FROM workspaces ORDER BY workspace_id").fetchall()
        return [
            WorkspaceRecord(
                UUID(row["workspace_id"]), row["state"], row["desired_state"], row["runtime_generation"],
                row["quota_bytes"], row["measured_allocated_bytes"], row["measured_apparent_bytes"],
                bool(row["pinned"]), row["lease_owner"], row["lease_expires_at"], row["last_error_code"]
            )
            for row in rows
        ]

    def update_workspace(self, workspace_id: UUID, **values: Any) -> WorkspaceRecord:
        allowed = {"state", "desired_state", "runtime_generation", "quota_bytes",
                   "measured_allocated_bytes", "measured_apparent_bytes", "pinned",
                   "lease_owner", "lease_expires_at", "last_error_code"}
        if not values or set(values) - allowed:
            raise ValueError("invalid workspace state update")
        assignments = ", ".join(f"{key}=?" for key in values)
        with self.transaction() as db:
            cursor = db.execute(f"UPDATE workspaces SET {assignments} WHERE workspace_id=?",
                                (*values.values(), str(workspace_id)))
            if cursor.rowcount != 1:
                raise KeyError(workspace_id)
        return self.workspace(workspace_id)

    def acquire_lease(self, workspace_id: UUID, owner: str | None = None,
                      ttl_seconds: float = 60.0) -> str:
        if ttl_seconds <= 0 or ttl_seconds > 3600:
            raise ValueError("lease TTL is outside the supported range")
        owner = owner or str(uuid4())
        now = time.time()
        with self.transaction() as db:
            row = db.execute("SELECT lease_owner, lease_expires_at FROM workspaces WHERE workspace_id=?",
                             (str(workspace_id),)).fetchone()
            if row is None:
                raise KeyError(workspace_id)
            if row["lease_owner"] and (row["lease_expires_at"] or 0) > now and row["lease_owner"] != owner:
                raise RuntimeError("workspace lease is busy")
            db.execute("UPDATE workspaces SET lease_owner=?, lease_expires_at=? WHERE workspace_id=?",
                        (owner, now + ttl_seconds, str(workspace_id)))
        return owner

    def renew_lease(self, workspace_id: UUID, owner: str, ttl_seconds: float = 60.0) -> None:
        with self.transaction() as db:
            cursor = db.execute(
                "UPDATE workspaces SET lease_expires_at=? WHERE workspace_id=? AND lease_owner=?",
                (time.time() + ttl_seconds, str(workspace_id), owner),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("workspace lease is not owned by caller")

    def release_lease(self, workspace_id: UUID, owner: str) -> None:
        with self.transaction() as db:
            db.execute("UPDATE workspaces SET lease_owner=NULL, lease_expires_at=NULL "
                       "WHERE workspace_id=? AND lease_owner=?", (str(workspace_id), owner))

    def reclaim_expired_leases(self, now: float | None = None) -> int:
        with self.transaction() as db:
            cursor = db.execute("UPDATE workspaces SET lease_owner=NULL, lease_expires_at=NULL "
                               "WHERE lease_owner IS NOT NULL AND lease_expires_at <= ?",
                               (time.time() if now is None else now,))
            return cursor.rowcount

    def active_job(self, workspace_id: UUID) -> JobRecord | None:
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM jobs WHERE workspace_id=? AND state IN ('queued','running') "
                "ORDER BY rowid DESC LIMIT 1", (str(workspace_id),)
            ).fetchone()
        return None if row is None else self._job(row)

    def create_job(self, workspace_id: UUID, *, deadline: float, metadata: dict[str, Any],
                   job_id: UUID | None = None) -> JobRecord:
        job_id = job_id or uuid4()
        with self.transaction() as db:
            db.execute(
                "INSERT INTO jobs(job_id,workspace_id,state,deadline,metadata) VALUES (?,?,'queued',?,?)",
                (str(job_id), str(workspace_id), deadline, json.dumps(metadata, separators=(",", ":"))),
            )
        return self.job(job_id)

    def job(self, job_id: UUID) -> JobRecord:
        with self._lock:
            row = self._db.execute("SELECT * FROM jobs WHERE job_id=?", (str(job_id),)).fetchone()
        if row is None:
            raise KeyError(job_id)
        return self._job(row)

    @staticmethod
    def _job(row: sqlite3.Row) -> JobRecord:
        return JobRecord(UUID(row["job_id"]), UUID(row["workspace_id"]), row["state"], row["pid"],
                         row["process_start_token"], row["process_group_id"], row["deadline"],
                         row["stdout_first"], row["stderr_first"], row["stdout_total"], row["stderr_total"],
                         json.loads(row["metadata"]))

    def update_job(self, job_id: UUID, **values: Any) -> JobRecord:
        allowed = {"state", "pid", "process_start_token", "process_group_id", "deadline",
                   "stdout_first", "stderr_first", "stdout_total", "stderr_total", "metadata"}
        if not values or set(values) - allowed:
            raise ValueError("invalid job update")
        if "metadata" in values and not isinstance(values["metadata"], str):
            values["metadata"] = json.dumps(values["metadata"], separators=(",", ":"))
        assignments = ", ".join(f"{key}=?" for key in values)
        with self.transaction() as db:
            cursor = db.execute(f"UPDATE jobs SET {assignments} WHERE job_id=?",
                                (*values.values(), str(job_id)))
            if cursor.rowcount != 1:
                raise KeyError(job_id)
        return self.job(job_id)

    def recover_jobs(self) -> list[JobRecord]:
        """Mark queued/running jobs as lost after broker restart.

        A real adapter can subsequently prove a guest supervisor-owned job is
        still alive; until that explicit proof exists, replay is unsafe.
        """
        with self.transaction() as db:
            db.execute("UPDATE jobs SET state='lost' WHERE state IN ('queued','running')")
        with self._lock:
            rows = self._db.execute("SELECT * FROM jobs WHERE state='lost'").fetchall()
        return [self._job(row) for row in rows]

    def active_jobs(self) -> list[JobRecord]:
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM jobs WHERE state IN ('queued','running') ORDER BY rowid"
            ).fetchall()
        return [self._job(row) for row in rows]

    def close(self) -> None:
        with self._lock:
            self._db.close()
