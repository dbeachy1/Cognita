"""Persistent project state shared by book preparation and folder policy.

The database is intentionally source-side state: it survives resets of the
disposable search database and is never initialized as a side effect of a read.
"""

from __future__ import annotations

import contextlib
import json
import os
import sqlite3
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from .config import normalize_project_path

STATE_DIRECTORY = ".cognita-storage"
DATABASE_FILENAME = "state.sqlite"
INITIALIZED_FILENAME = "initialized.json"
BOOTSTRAP_FILENAME = "bootstrap.json"
SCHEMA_VERSION = 1


class ProjectStateError(RuntimeError):
    """A persistent state root is damaged or cannot be safely opened."""


@dataclass(frozen=True)
class FolderPolicy:
    policy_revision: int
    rules: tuple[tuple[str, bool], ...]


@dataclass(frozen=True, slots=True)
class IndexedRoleProvenance:
    """Source/version/config bindings for one role-admitted book index row.

    This record is derived evidence, not an alternate layout or approval
    authority. Current source and config are revalidated by the service before
    a record is admitted to a query or refreshed during indexing.
    """

    source_path: str
    doc_id: str
    extracted_sha256: str
    raw_sha256: str
    extraction_version: str
    role: str
    chapter_id: str | None
    layout_sha256: str
    chapter_state_sha256: str | None
    annotations_sha256: str | None
    approval_source_raw_sha256: str | None
    approval_prose_projection_sha256: str | None
    approval_projection_version: str | None
    summary_raw_sha256: str | None
    summary_source_raw_sha256: str | None
    summary_source_prose_projection_sha256: str | None

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_path", normalize_project_path(self.source_path))
        for name in (
            "extracted_sha256", "raw_sha256", "layout_sha256",
            "chapter_state_sha256", "annotations_sha256",
            "approval_source_raw_sha256", "approval_prose_projection_sha256",
            "summary_raw_sha256", "summary_source_raw_sha256",
            "summary_source_prose_projection_sha256",
        ):
            value = getattr(self, name)
            if value is not None and (
                not isinstance(value, str)
                or len(value) != 64
                or any(char not in "0123456789abcdef" for char in value)
            ):
                raise ValueError(f"{name} must be lowercase SHA-256 hex or null")
        for name in (
            "doc_id", "extraction_version", "role", "layout_sha256",
        ):
            if not isinstance(getattr(self, name), str) or not getattr(self, name):
                raise ValueError(f"{name} must be a nonempty string")


class ProjectState:
    """SQLite authority stored at ``<project>/.cognita-storage/state.sqlite``."""

    def __init__(self, project_root: Path, *, timeout: float = 10.0):
        self.project_root = Path(project_root).resolve(strict=True)
        if not self.project_root.is_dir():
            raise ProjectStateError("project source is not a directory")
        self.root = self.project_root / STATE_DIRECTORY
        self.database = self.root / DATABASE_FILENAME
        self.timeout = timeout

    @classmethod
    def discover(cls, project_root: Path, *, timeout: float = 10.0) -> ProjectState | None:
        """Open existing authority, return ``None`` for a pristine project.

        Any nonempty state directory is evidence that initialization happened;
        a missing marker or database in that case is a hard failure, never a
        reason to create an empty policy database.
        """
        project_root = Path(project_root)
        state_root = project_root / STATE_DIRECTORY
        try:
            state_facts = state_root.lstat()
        except FileNotFoundError:
            return None
        try:
            if stat.S_ISLNK(state_facts.st_mode) or not stat.S_ISDIR(state_facts.st_mode):
                raise ProjectStateError("reserved project state path is not a directory")
            entries = list(state_root.iterdir())
            if not entries:
                return None
            marker = state_root / INITIALIZED_FILENAME
            database = state_root / DATABASE_FILENAME
            bootstrap = state_root / BOOTSTRAP_FILENAME
            if not marker.exists() and bootstrap.exists():
                # Only this create-only, versioned intent can authorize finish
                # of an interrupted first creation. No prior policy can exist.
                cls._validate_bootstrap(bootstrap)
                return cls._finish_bootstrap(project_root, timeout=timeout)
            marker_facts = marker.lstat() if marker.exists() else None
            database_facts = database.lstat() if database.exists() else None
            if (marker_facts is None or database_facts is None
                    or stat.S_ISLNK(marker_facts.st_mode) or stat.S_ISLNK(database_facts.st_mode)
                    or not stat.S_ISREG(marker_facts.st_mode) or not stat.S_ISREG(database_facts.st_mode)):
                raise ProjectStateError("initialized project state is missing its marker or database")
            state = cls(project_root, timeout=timeout)
            state._validate_marker()
            state._validate_database()
            state._ensure_indexed_role_schema()
            state._ensure_generation_schema()
            state._ensure_import_schema()
            return state
        except ProjectStateError:
            raise
        except OSError as exc:
            raise ProjectStateError("project state is unreadable") from exc

    @classmethod
    def initialize(cls, project_root: Path, *, timeout: float = 10.0) -> ProjectState:
        """Create state after the caller has acquired the project write lock."""
        project_root = Path(project_root).resolve(strict=True)
        state_root = project_root / STATE_DIRECTORY
        if state_root.is_symlink():
            raise ProjectStateError("reserved project state path cannot be a symlink")
        state_root.mkdir(exist_ok=True)
        marker = state_root / INITIALIZED_FILENAME
        database = state_root / DATABASE_FILENAME
        bootstrap = state_root / BOOTSTRAP_FILENAME
        if marker.exists() or database.exists():
            existing = cls.discover(project_root, timeout=timeout)
            if existing is None:
                raise ProjectStateError("project state initialization is inconsistent")
            return existing
        if any(state_root.iterdir()) and not bootstrap.exists():
            raise ProjectStateError("reserved project state contains unrecognized files")
        if not bootstrap.exists():
            payload = {"schema_version": SCHEMA_VERSION, "database": DATABASE_FILENAME}
            cls._write_create_only(bootstrap, json.dumps(payload, separators=(",", ":")).encode())
        else:
            cls._validate_bootstrap(bootstrap)
        return cls._finish_bootstrap(project_root, timeout=timeout)

    @classmethod
    def _finish_bootstrap(cls, project_root: Path, *, timeout: float) -> ProjectState:
        state = cls(project_root, timeout=timeout)
        state._create_schema()
        state._ensure_indexed_role_schema()
        state._ensure_generation_schema()
        state._ensure_import_schema()
        marker = {
            "schema_version": SCHEMA_VERSION,
            "database": DATABASE_FILENAME,
        }
        cls._write_atomic(
            state.root / INITIALIZED_FILENAME,
            json.dumps(marker, sort_keys=True, separators=(",", ":")).encode(),
        )
        try:
            (state.root / BOOTSTRAP_FILENAME).unlink(missing_ok=True)
        except OSError as exc:
            raise ProjectStateError("could not finalize project state bootstrap") from exc
        return state

    @staticmethod
    def _write_create_only(path: Path, data: bytes) -> None:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        descriptor = os.open(path, flags, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())

    @staticmethod
    def _write_atomic(path: Path, data: bytes) -> None:
        fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            Path(temporary).unlink(missing_ok=True)

    @staticmethod
    def _validate_bootstrap(path: Path) -> None:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ProjectStateError("project state bootstrap journal is unreadable") from exc
        if value != {"schema_version": SCHEMA_VERSION, "database": DATABASE_FILENAME}:
            raise ProjectStateError("project state bootstrap journal is invalid")

    def _validate_marker(self) -> None:
        try:
            value = json.loads((self.root / INITIALIZED_FILENAME).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ProjectStateError("project state marker is unreadable") from exc
        if value != {"schema_version": SCHEMA_VERSION, "database": DATABASE_FILENAME}:
            raise ProjectStateError("project state marker has an unsupported schema")

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database, timeout=self.timeout, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA journal_mode=DELETE")
        return connection

    def _create_schema(self) -> None:
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS state_meta (
                        key TEXT PRIMARY KEY,
                        value TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS folder_policy (
                        singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                        policy_revision INTEGER NOT NULL CHECK(policy_revision>=0)
                    );
                    CREATE TABLE IF NOT EXISTS folder_rules (
                        path TEXT PRIMARY KEY,
                        indexed INTEGER NOT NULL CHECK(indexed IN (0,1))
                    );
                    CREATE TABLE IF NOT EXISTS operation_receipts (
                        owner_key TEXT NOT NULL,
                        project TEXT NOT NULL,
                        tool TEXT NOT NULL,
                        operation_id TEXT NOT NULL,
                        args_sha256 TEXT NOT NULL,
                        payload_json TEXT NOT NULL,
                        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        PRIMARY KEY(owner_key, project, tool, operation_id)
                    );
                    CREATE TABLE IF NOT EXISTS policy_jobs (
                        job_id TEXT PRIMARY KEY,
                        revision INTEGER NOT NULL,
                        state TEXT NOT NULL,
                        details_json TEXT NOT NULL,
                        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                    );
                    CREATE TABLE IF NOT EXISTS publication_journal (
                        journal_id TEXT PRIMARY KEY,
                        kind TEXT NOT NULL,
                        phase TEXT NOT NULL,
                        payload_json TEXT NOT NULL,
                        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                    );
                    CREATE TABLE IF NOT EXISTS managed_write_jobs (
                        job_id TEXT PRIMARY KEY,
                        source_path TEXT NOT NULL,
                        bytes_sha256 TEXT NOT NULL,
                        operation_id TEXT,
                        state TEXT NOT NULL,
                        error_json TEXT,
                        doc_id TEXT,
                        extracted_sha256 TEXT,
                        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        UNIQUE(source_path, operation_id)
                    );
                    CREATE INDEX IF NOT EXISTS managed_write_jobs_path_updated
                        ON managed_write_jobs(source_path, updated_at DESC);
                    CREATE TABLE IF NOT EXISTS book_config (
                        singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                        book_id TEXT NOT NULL,
                        layout_filepath TEXT NOT NULL,
                        layout_revision INTEGER NOT NULL,
                        layout_sha256 TEXT NOT NULL,
                        config_generation INTEGER NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS book_views (
                        view_id TEXT PRIMARY KEY,
                        chapter_id TEXT NOT NULL,
                        scope_json TEXT NOT NULL,
                        payload_json TEXT NOT NULL,
                        expires_at TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS book_namespaces (
                        chapter_id TEXT NOT NULL,
                        scope_key TEXT NOT NULL,
                        manifest_revision INTEGER,
                        media_revision INTEGER NOT NULL DEFAULT 0,
                        head_revision INTEGER,
                        current_snapshot_id TEXT,
                        current_plan_sha256 TEXT,
                        PRIMARY KEY(chapter_id, scope_key)
                    );
                    CREATE TABLE IF NOT EXISTS book_snapshots (
                        snapshot_id TEXT PRIMARY KEY,
                        chapter_id TEXT NOT NULL,
                        scope_key TEXT NOT NULL,
                        manifest_revision INTEGER NOT NULL,
                        payload_json TEXT NOT NULL,
                        UNIQUE(chapter_id, scope_key, manifest_revision)
                    );
                    """
                )
                connection.execute(
                    "INSERT OR IGNORE INTO state_meta(key,value) VALUES('schema_version',?)",
                    (str(SCHEMA_VERSION),),
                )
                connection.execute(
                    "INSERT OR IGNORE INTO folder_policy(singleton,policy_revision) VALUES(1,0)"
                )
                version = connection.execute(
                    "SELECT value FROM state_meta WHERE key='schema_version'"
                ).fetchone()
                if version is None or version["value"] != str(SCHEMA_VERSION):
                    raise ProjectStateError("project state database has an unsupported schema")
                connection.commit()
        except (sqlite3.DatabaseError, OSError) as exc:
            raise ProjectStateError("project state database could not be initialized") from exc

    def _validate_database(self) -> None:
        try:
            with self._connect() as connection:
                result = connection.execute("PRAGMA quick_check").fetchone()
                version = connection.execute(
                    "SELECT value FROM state_meta WHERE key='schema_version'"
                ).fetchone()
                if result is None or result[0] != "ok" or version is None:
                    raise ProjectStateError("project state database is corrupt or incomplete")
                if version["value"] != str(SCHEMA_VERSION):
                    raise ProjectStateError("project state database has an unsupported schema")
                connection.execute("SELECT policy_revision FROM folder_policy WHERE singleton=1")
        except ProjectStateError:
            raise
        except sqlite3.DatabaseError as exc:
            raise ProjectStateError("project state database is unreadable or corrupt") from exc

    def _ensure_indexed_role_schema(self) -> None:
        """Add the optional derived-provenance table to existing state DBs."""
        try:
            with self.transaction() as connection:
                connection.execute(
                    """CREATE TABLE IF NOT EXISTS indexed_role_provenance (
                        source_path TEXT PRIMARY KEY,
                        doc_id TEXT NOT NULL,
                        extracted_sha256 TEXT NOT NULL,
                        raw_sha256 TEXT NOT NULL,
                        extraction_version TEXT NOT NULL,
                        role TEXT NOT NULL,
                        chapter_id TEXT,
                        layout_sha256 TEXT NOT NULL,
                        chapter_state_sha256 TEXT,
                        annotations_sha256 TEXT,
                        approval_source_raw_sha256 TEXT,
                        approval_prose_projection_sha256 TEXT,
                        approval_projection_version TEXT,
                        summary_raw_sha256 TEXT,
                        summary_source_raw_sha256 TEXT,
                        summary_source_prose_projection_sha256 TEXT,
                        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                    )"""
                )
        except (sqlite3.DatabaseError, OSError) as exc:
            raise ProjectStateError("indexed role provenance schema is unavailable") from exc

    def _ensure_generation_schema(self) -> None:
        """Create the append-only generation authority for existing book state."""
        try:
            with self.transaction() as connection:
                connection.execute(
                    """CREATE TABLE IF NOT EXISTS book_generations (
                        generation_record_id TEXT PRIMARY KEY,
                        chapter_id TEXT NOT NULL,
                        scope_key TEXT NOT NULL,
                        snapshot_id TEXT NOT NULL,
                        chunk_id TEXT NOT NULL,
                        generation_revision INTEGER NOT NULL,
                        state TEXT NOT NULL,
                        request_sha256 TEXT NOT NULL,
                        payload_json TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    )"""
                )
                connection.execute(
                    "CREATE INDEX IF NOT EXISTS book_generations_chapter_updated "
                    "ON book_generations(chapter_id, updated_at, generation_record_id)"
                )
        except (sqlite3.DatabaseError, OSError) as exc:
            raise ProjectStateError("generation state schema is unavailable") from exc

    def _ensure_import_schema(self) -> None:
        """Create durable local-import jobs and immutable take facts.

        These records deliberately live beside generation evidence rather than
        the disposable workspace-job database.  The JSON payloads are the
        authoritative wire facts; indexed columns only support ownership and
        conflict checks while a short transaction is open.
        """
        try:
            with self.transaction() as connection:
                connection.execute(
                    """CREATE TABLE IF NOT EXISTS book_import_jobs (
                        job_id TEXT PRIMARY KEY,
                        generation_record_id TEXT NOT NULL,
                        owner_key TEXT NOT NULL,
                        operation_id TEXT NOT NULL,
                        job_revision INTEGER NOT NULL,
                        state TEXT NOT NULL,
                        payload_json TEXT NOT NULL,
                        result_json TEXT,
                        error_json TEXT,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    )"""
                )
                connection.execute(
                    "CREATE UNIQUE INDEX IF NOT EXISTS book_import_jobs_active_generation "
                    "ON book_import_jobs(generation_record_id) "
                    "WHERE state IN ('queued','running','cancel_requested')"
                )
                connection.execute(
                    """CREATE TABLE IF NOT EXISTS book_takes (
                        take_id TEXT PRIMARY KEY,
                        generation_record_id TEXT NOT NULL UNIQUE,
                        chapter_id TEXT NOT NULL,
                        media_revision INTEGER NOT NULL,
                        payload_json TEXT NOT NULL,
                        created_at TEXT NOT NULL
                    )"""
                )
                connection.execute(
                    """CREATE TABLE IF NOT EXISTS book_chapter_media (
                        chapter_id TEXT PRIMARY KEY,
                        media_revision INTEGER NOT NULL
                    )"""
                )
        except (sqlite3.DatabaseError, OSError) as exc:
            raise ProjectStateError("import job state schema is unavailable") from exc

    def reserve_generation(
        self, *, record: dict, owner_key: str, project: str, tool: str,
        operation_id: str, args_sha256: str,
    ) -> tuple[str, dict]:
        """Reserve an immutable provider request with receipt-first replay."""
        with self.transaction() as connection:
            prior = connection.execute(
                "SELECT args_sha256,payload_json FROM operation_receipts "
                "WHERE owner_key=? AND project=? AND tool=? AND operation_id=?",
                (owner_key, project, tool, operation_id),
            ).fetchone()
            if prior is not None:
                if prior["args_sha256"] != args_sha256:
                    raise ProjectStateError("operation_id_conflict")
                return "replay", json.loads(prior["payload_json"])
            connection.execute(
                "INSERT INTO book_generations(generation_record_id,chapter_id,scope_key,snapshot_id,"
                "chunk_id,generation_revision,state,request_sha256,payload_json,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (record["generation_record_id"], record["chapter_id"], record["scope_key"],
                 record["snapshot_id"], record["chunk_id"], record["generation_revision"],
                 record["state"], record["request_sha256"], record["payload_json"],
                 record["created_at"], record["updated_at"]),
            )
            result = {"generation": json.loads(record["payload_json"])}
            connection.execute(
                "INSERT INTO operation_receipts(owner_key,project,tool,operation_id,args_sha256,payload_json) "
                "VALUES(?,?,?,?,?,?)",
                (owner_key, project, tool, operation_id, args_sha256,
                 json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":"))),
            )
            return "committed", result

    def generation(self, generation_record_id: str) -> dict | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload_json FROM book_generations WHERE generation_record_id=?",
                (generation_record_id,),
            ).fetchone()
        return None if row is None else json.loads(row["payload_json"])

    def update_generation(
        self, *, generation_record_id: str, expected_revision: int, payload: dict,
        owner_key: str, project: str, tool: str, operation_id: str, args_sha256: str,
    ) -> tuple[str, dict]:
        with self.transaction() as connection:
            prior = connection.execute(
                "SELECT args_sha256,payload_json FROM operation_receipts WHERE owner_key=? AND project=? AND tool=? AND operation_id=?",
                (owner_key, project, tool, operation_id),
            ).fetchone()
            if prior is not None:
                if prior["args_sha256"] != args_sha256:
                    raise ProjectStateError("operation_id_conflict")
                return "replay", json.loads(prior["payload_json"])
            row = connection.execute(
                "SELECT generation_revision FROM book_generations WHERE generation_record_id=?",
                (generation_record_id,),
            ).fetchone()
            if row is None:
                raise ProjectStateError("generation_not_found")
            if int(row["generation_revision"]) != expected_revision:
                raise ProjectStateError("stale_generation")
            connection.execute(
                "UPDATE book_generations SET generation_revision=?,state=?,payload_json=?,updated_at=? WHERE generation_record_id=?",
                (payload["generation_revision"], payload["state"],
                 json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                 payload["updated_at"], generation_record_id),
            )
            result = {"generation": payload}
            connection.execute(
                "INSERT INTO operation_receipts(owner_key,project,tool,operation_id,args_sha256,payload_json) VALUES(?,?,?,?,?,?)",
                (owner_key, project, tool, operation_id, args_sha256,
                 json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":"))),
            )
            return "committed", result

    def generations(self, *, generation_record_id: str | None = None, chapter_id: str | None = None) -> list[dict]:
        with self._connect() as connection:
            if generation_record_id is not None:
                rows = connection.execute("SELECT payload_json FROM book_generations WHERE generation_record_id=?", (generation_record_id,)).fetchall()
            elif chapter_id is not None:
                rows = connection.execute("SELECT payload_json FROM book_generations WHERE chapter_id=? ORDER BY created_at,generation_record_id", (chapter_id,)).fetchall()
            else:
                rows = []
        return [json.loads(row["payload_json"]) for row in rows]

    @staticmethod
    def _receipt_in(
        connection: sqlite3.Connection, *, owner_key: str, project: str,
        tool: str, operation_id: str, args_sha256: str,
    ) -> dict | None:
        row = connection.execute(
            "SELECT args_sha256,payload_json FROM operation_receipts "
            "WHERE owner_key=? AND project=? AND tool=? AND operation_id=?",
            (owner_key, project, tool, operation_id),
        ).fetchone()
        if row is None:
            return None
        if row["args_sha256"] != args_sha256:
            raise ProjectStateError("operation_id_conflict")
        return json.loads(row["payload_json"])

    @staticmethod
    def _put_receipt_in(
        connection: sqlite3.Connection, *, owner_key: str, project: str,
        tool: str, operation_id: str, args_sha256: str, result: dict,
    ) -> None:
        connection.execute(
            "INSERT INTO operation_receipts(owner_key,project,tool,operation_id,args_sha256,payload_json) "
            "VALUES(?,?,?,?,?,?)",
            (owner_key, project, tool, operation_id, args_sha256,
             json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":"))),
        )

    def reserve_import_job(
        self, *, generation_record_id: str, expected_generation_revision: int,
        owner_key: str, project: str, operation_id: str, args_sha256: str,
        job_id: str, pinned_inputs_sha256: str, payload: dict,
    ) -> tuple[str, dict]:
        """Reserve one local import and its receipt in the same transaction."""
        now = payload["created_at"]
        with self.transaction() as connection:
            prior = self._receipt_in(
                connection, owner_key=owner_key, project=project,
                tool="audiobook_import_audio", operation_id=operation_id,
                args_sha256=args_sha256,
            )
            if prior is not None:
                return "replay", prior
            row = connection.execute(
                "SELECT payload_json,generation_revision FROM book_generations WHERE generation_record_id=?",
                (generation_record_id,),
            ).fetchone()
            if row is None:
                raise ProjectStateError("generation_not_found")
            generation = json.loads(row["payload_json"])
            if int(row["generation_revision"]) != expected_generation_revision:
                raise ProjectStateError("stale_generation")
            if generation.get("state") != "completed":
                raise ProjectStateError("generation_not_completed")
            if generation.get("media_registered"):
                raise ProjectStateError("import_already_registered")
            active = connection.execute(
                "SELECT job_id FROM book_import_jobs WHERE generation_record_id=? "
                "AND state IN ('queued','running','cancel_requested')",
                (generation_record_id,),
            ).fetchone()
            if active is not None:
                raise ProjectStateError("import_in_progress")
            generation["generation_revision"] = expected_generation_revision + 1
            generation["import_job_id"] = job_id
            generation["updated_at"] = now
            connection.execute(
                "UPDATE book_generations SET generation_revision=?,payload_json=?,updated_at=? WHERE generation_record_id=?",
                (generation["generation_revision"], json.dumps(generation, ensure_ascii=False, separators=(",", ":")),
                 now, generation_record_id),
            )
            connection.execute(
                "INSERT INTO book_import_jobs(job_id,generation_record_id,owner_key,operation_id,job_revision,state,payload_json,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (job_id, generation_record_id, owner_key, operation_id, 1, "queued",
                 json.dumps(payload, ensure_ascii=False, separators=(",", ":")), now, now),
            )
            result = {"job_id": job_id, "job_revision": 1, "state": "queued",
                      "poll_after_seconds": 1, "pinned_inputs_sha256": pinned_inputs_sha256}
            self._put_receipt_in(
                connection, owner_key=owner_key, project=project,
                tool="audiobook_import_audio", operation_id=operation_id,
                args_sha256=args_sha256, result=result,
            )
            return "committed", result

    def import_job(self, job_id: str) -> dict | None:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM book_import_jobs WHERE job_id=?", (job_id,)).fetchone()
        if row is None:
            return None
        payload = json.loads(row["payload_json"])
        return {
            "job_id": row["job_id"], "generation_record_id": row["generation_record_id"],
            "owner_key": row["owner_key"], "operation_id": row["operation_id"],
            "job_revision": int(row["job_revision"]), "state": row["state"],
            "payload": payload, "result": json.loads(row["result_json"]) if row["result_json"] else None,
            "error": json.loads(row["error_json"]) if row["error_json"] else None,
            "created_at": row["created_at"], "updated_at": row["updated_at"],
        }

    def claim_import_job(self, job_id: str) -> dict | None:
        with self.transaction() as connection:
            row = connection.execute("SELECT * FROM book_import_jobs WHERE job_id=?", (job_id,)).fetchone()
            if row is None or row["state"] != "queued":
                return None
            connection.execute(
                "UPDATE book_import_jobs SET state='running',job_revision=job_revision+1,updated_at=CURRENT_TIMESTAMP WHERE job_id=?",
                (job_id,),
            )
        return self.import_job(job_id)

    def request_import_cancellation(
        self, *, job_id: str, expected_job_revision: int, owner_key: str,
        project: str, operation_id: str, args_sha256: str,
    ) -> tuple[str, dict]:
        with self.transaction() as connection:
            prior = self._receipt_in(
                connection, owner_key=owner_key, project=project,
                tool="audiobook_cancel_job", operation_id=operation_id,
                args_sha256=args_sha256,
            )
            if prior is not None:
                return "replay", prior
            row = connection.execute("SELECT job_revision,state FROM book_import_jobs WHERE job_id=?", (job_id,)).fetchone()
            if row is None:
                raise ProjectStateError("job_not_found")
            if int(row["job_revision"]) != expected_job_revision:
                raise ProjectStateError("stale_job")
            state = row["state"]
            if state in {"succeeded", "failed", "cancelled"}:
                result = {"job_id": job_id, "job_revision": int(row["job_revision"]), "state": state}
            else:
                connection.execute(
                    "UPDATE book_import_jobs SET state='cancel_requested',job_revision=job_revision+1,updated_at=CURRENT_TIMESTAMP WHERE job_id=?",
                    (job_id,),
                )
                result = {"job_id": job_id, "job_revision": int(row["job_revision"]) + 1, "state": "cancel_requested"}
            self._put_receipt_in(
                connection, owner_key=owner_key, project=project,
                tool="audiobook_cancel_job", operation_id=operation_id,
                args_sha256=args_sha256, result=result,
            )
            return "committed", result

    def finish_import_failure(self, *, job_id: str, reason: str, message: str,
                              cancelled: bool = False) -> dict | None:
        """Terminally fail an unregistered import and release only its reservation."""
        with self.transaction() as connection:
            row = connection.execute("SELECT * FROM book_import_jobs WHERE job_id=?", (job_id,)).fetchone()
            if row is None or row["state"] == "succeeded":
                return None
            if row["state"] in {"failed", "cancelled"}:
                payload = json.loads(row["payload_json"])
                return {
                    "job_id": row["job_id"], "generation_record_id": row["generation_record_id"],
                    "owner_key": row["owner_key"], "operation_id": row["operation_id"],
                    "job_revision": int(row["job_revision"]), "state": row["state"],
                    "payload": payload,
                    "result": json.loads(row["result_json"]) if row["result_json"] else None,
                    "error": json.loads(row["error_json"]) if row["error_json"] else None,
                    "created_at": row["created_at"], "updated_at": row["updated_at"],
                }
            terminal = "cancelled" if cancelled or row["state"] == "cancel_requested" else "failed"
            error = {"reason": reason, "message": message, "operation_outcome": "not_applied", "correlation_id": None}
            connection.execute(
                "UPDATE book_import_jobs SET state=?,job_revision=job_revision+1,error_json=?,updated_at=CURRENT_TIMESTAMP WHERE job_id=?",
                (terminal, json.dumps(error, separators=(",", ":")), job_id),
            )
            grow = connection.execute("SELECT payload_json FROM book_generations WHERE generation_record_id=?", (row["generation_record_id"],)).fetchone()
            if grow is not None:
                generation = json.loads(grow["payload_json"])
                if generation.get("import_job_id") == job_id and not generation.get("media_registered"):
                    generation["import_job_id"] = None
                    generation["generation_revision"] += 1
                    generation["updated_at"] = row["updated_at"]
                    connection.execute(
                        "UPDATE book_generations SET generation_revision=?,payload_json=?,updated_at=? WHERE generation_record_id=?",
                        (generation["generation_revision"], json.dumps(generation, ensure_ascii=False, separators=(",", ":")),
                         generation["updated_at"], row["generation_record_id"]),
                    )
        return self.import_job(job_id)

    def finish_import_success(self, *, job_id: str, generation_payload: dict,
                              take_payload: dict) -> dict:
        """Register an immutable take and terminal job under one short transaction."""
        with self.transaction() as connection:
            row = connection.execute("SELECT * FROM book_import_jobs WHERE job_id=?", (job_id,)).fetchone()
            if row is None:
                raise ProjectStateError("job_not_found")
            if row["state"] == "succeeded":
                return json.loads(row["result_json"])
            if row["state"] == "cancel_requested":
                raise ProjectStateError("cancel_requested")
            if row["state"] not in {"queued", "running"}:
                raise ProjectStateError("job_not_active")
            current = connection.execute("SELECT payload_json FROM book_generations WHERE generation_record_id=?", (row["generation_record_id"],)).fetchone()
            if current is None:
                raise ProjectStateError("generation_not_found")
            generation = json.loads(current["payload_json"])
            if generation.get("import_job_id") != job_id or generation.get("media_registered"):
                raise ProjectStateError("import_reservation_lost")
            media_row = connection.execute("SELECT media_revision FROM book_chapter_media WHERE chapter_id=?", (take_payload["chapter_id"],)).fetchone()
            media_revision = 1 if media_row is None else int(media_row["media_revision"]) + 1
            # Import work can take minutes.  Preserve any independently
            # recorded provider evidence that arrived while the source was
            # streamed; immutable request identity remains the row authority.
            completed_at = generation_payload["updated_at"]
            generation_payload = dict(generation)
            generation_payload["generation_revision"] = int(generation["generation_revision"]) + 1
            generation_payload["media_registered"] = True
            generation_payload["take_id"] = take_payload["take_id"]
            generation_payload["import_job_id"] = job_id
            generation_payload["updated_at"] = completed_at
            result = {"kind": "import", "generation_record_id": row["generation_record_id"],
                      "generation_revision": generation_payload["generation_revision"],
                      "media_revision": media_revision, "take": take_payload}
            connection.execute(
                "UPDATE book_generations SET generation_revision=?,payload_json=?,updated_at=? WHERE generation_record_id=?",
                (generation_payload["generation_revision"], json.dumps(generation_payload, ensure_ascii=False, separators=(",", ":")),
                 generation_payload["updated_at"], row["generation_record_id"]),
            )
            connection.execute(
                "INSERT INTO book_takes(take_id,generation_record_id,chapter_id,media_revision,payload_json,created_at) VALUES(?,?,?,?,?,?)",
                (take_payload["take_id"], row["generation_record_id"], take_payload["chapter_id"], media_revision,
                 json.dumps(take_payload, ensure_ascii=False, separators=(",", ":")), generation_payload["updated_at"]),
            )
            connection.execute(
                "INSERT INTO book_chapter_media(chapter_id,media_revision) VALUES(?,?) "
                "ON CONFLICT(chapter_id) DO UPDATE SET media_revision=excluded.media_revision",
                (take_payload["chapter_id"], media_revision),
            )
            connection.execute(
                "UPDATE book_import_jobs SET state='succeeded',job_revision=job_revision+1,result_json=?,updated_at=CURRENT_TIMESTAMP WHERE job_id=?",
                (json.dumps(result, ensure_ascii=False, separators=(",", ":")), job_id),
            )
            return result

    def put_indexed_role_provenance(self, record: IndexedRoleProvenance) -> None:
        """Atomically replace the derived admission facts for one source path."""
        columns = (
            "source_path", "doc_id", "extracted_sha256", "raw_sha256",
            "extraction_version", "role", "chapter_id", "layout_sha256",
            "chapter_state_sha256", "annotations_sha256",
            "approval_source_raw_sha256", "approval_prose_projection_sha256",
            "approval_projection_version", "summary_raw_sha256",
            "summary_source_raw_sha256", "summary_source_prose_projection_sha256",
        )
        values = tuple(getattr(record, column) for column in columns)
        assignments = ",".join(f"{column}=excluded.{column}" for column in columns[1:])
        with self.transaction() as connection:
            connection.execute(
                f"INSERT INTO indexed_role_provenance ({','.join(columns)}) "
                f"VALUES ({','.join('?' for _ in columns)}) "
                f"ON CONFLICT(source_path) DO UPDATE SET {assignments}, "
                "updated_at=CURRENT_TIMESTAMP",
                values,
            )

    def indexed_role_provenance(self, source_path: str) -> IndexedRoleProvenance | None:
        source_path = normalize_project_path(source_path)
        try:
            with self._connect() as connection:
                row = connection.execute(
                    "SELECT * FROM indexed_role_provenance WHERE source_path=?",
                    (source_path,),
                ).fetchone()
        except sqlite3.DatabaseError as exc:
            raise ProjectStateError("indexed role provenance is unreadable") from exc
        if row is None:
            return None
        columns = (
            "source_path", "doc_id", "extracted_sha256", "raw_sha256",
            "extraction_version", "role", "chapter_id", "layout_sha256",
            "chapter_state_sha256", "annotations_sha256",
            "approval_source_raw_sha256", "approval_prose_projection_sha256",
            "approval_projection_version", "summary_raw_sha256",
            "summary_source_raw_sha256", "summary_source_prose_projection_sha256",
        )
        try:
            return IndexedRoleProvenance(**{column: row[column] for column in columns})
        except (TypeError, ValueError) as exc:
            raise ProjectStateError("indexed role provenance is invalid") from exc

    def delete_indexed_role_provenance(self, source_path: str) -> bool:
        source_path = normalize_project_path(source_path)
        with self.transaction() as connection:
            cursor = connection.execute(
                "DELETE FROM indexed_role_provenance WHERE source_path=?", (source_path,)
            )
        return cursor.rowcount > 0

    @contextlib.contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Run one short rollback-journal transaction."""
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def folder_policy(self) -> FolderPolicy:
        with self._connect() as connection:
            revision = connection.execute(
                "SELECT policy_revision FROM folder_policy WHERE singleton=1"
            ).fetchone()
            rules = connection.execute(
                "SELECT path,indexed FROM folder_rules ORDER BY path"
            ).fetchall()
        if revision is None:
            raise ProjectStateError("folder policy state is missing")
        return FolderPolicy(
            int(revision["policy_revision"]),
            tuple((row["path"], bool(row["indexed"])) for row in rules),
        )

    def publication(self, journal_id: str) -> dict | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT kind,phase,payload_json FROM publication_journal WHERE journal_id=?", (journal_id,)
            ).fetchone()
        return None if row is None else {"kind": row["kind"], "phase": row["phase"],
                                         "payload": json.loads(row["payload_json"])}

    def begin_publication(self, journal_id: str, kind: str, payload: dict) -> None:
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        with self.transaction() as connection:
            row = connection.execute("SELECT kind,payload_json FROM publication_journal WHERE journal_id=?", (journal_id,)).fetchone()
            if row is None:
                connection.execute("INSERT INTO publication_journal(journal_id,kind,phase,payload_json) VALUES(?,?,?,?)",
                                   (journal_id, kind, "prepared", encoded))
            elif row["kind"] != kind or row["payload_json"] != encoded:
                raise ProjectStateError("publication journal conflicts with requested move")

    def advance_publication(self, journal_id: str, phase: str) -> None:
        with self.transaction() as connection:
            cursor = connection.execute("UPDATE publication_journal SET phase=?,updated_at=CURRENT_TIMESTAMP WHERE journal_id=?", (phase, journal_id))
            if cursor.rowcount != 1:
                raise ProjectStateError("publication journal not found")

    def pending_publications(self, kind: str) -> list[dict]:
        """Return unfinished source-side publications in their durable order."""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT journal_id,phase,payload_json FROM publication_journal "
                "WHERE kind=? AND phase<>? ORDER BY updated_at,journal_id",
                (kind, "committed"),
            ).fetchall()
        return [
            {"journal_id": row["journal_id"], "phase": row["phase"],
             "payload": json.loads(row["payload_json"])}
            for row in rows
        ]

    def delete_publication(self, journal_id: str) -> None:
        """Retire a recovered publication only after its owned facts are restored."""
        with self.transaction() as connection:
            cursor = connection.execute(
                "DELETE FROM publication_journal WHERE journal_id=?", (journal_id,)
            )
            if cursor.rowcount != 1:
                raise ProjectStateError("publication journal not found")

    def set_folder_rule(
        self, path: str, indexed: bool, expected_revision: int,
        *, owner_key: str, project: str, tool: str, operation_id: str,
        args_sha256: str, result: dict, job_id: str | None = None,
    ) -> tuple[str, dict, int]:
        """Apply a rule with replay-before-CAS and record receipt atomically."""
        payload = json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        with self.transaction() as connection:
            receipt = connection.execute(
                "SELECT args_sha256,payload_json FROM operation_receipts "
                "WHERE owner_key=? AND project=? AND tool=? AND operation_id=?",
                (owner_key, project, tool, operation_id),
            ).fetchone()
            if receipt is not None:
                if receipt["args_sha256"] != args_sha256:
                    return "conflict", {}, -1
                return "replay", json.loads(receipt["payload_json"]), -1
            row = connection.execute(
                "SELECT policy_revision FROM folder_policy WHERE singleton=1"
            ).fetchone()
            if row is None:
                raise ProjectStateError("folder policy state is missing")
            current = int(row["policy_revision"])
            if current != expected_revision:
                return "stale", {}, current
            next_revision = current + 1
            connection.execute(
                "INSERT INTO folder_rules(path,indexed) VALUES(?,?) "
                "ON CONFLICT(path) DO UPDATE SET indexed=excluded.indexed",
                (path, int(indexed)),
            )
            connection.execute(
                "UPDATE folder_policy SET policy_revision=? WHERE singleton=1",
                (next_revision,),
            )
            if job_id is not None:
                connection.execute(
                    "INSERT INTO policy_jobs(job_id,revision,state,details_json) VALUES(?,?,?,?)",
                    (job_id, next_revision, "queued", json.dumps(
                        {"path": path, "indexed": indexed}, sort_keys=True, separators=(",", ":")
                    )),
                )
            value = dict(result)
            value["policy_revision"] = next_revision
            connection.execute(
                "INSERT INTO operation_receipts(owner_key,project,tool,operation_id,args_sha256,payload_json) "
                "VALUES(?,?,?,?,?,?)",
                (owner_key, project, tool, operation_id, args_sha256,
                 json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))),
            )
            return "committed", value, next_revision

    def rebase_folder_rules(
        self, old_prefix: str, new_prefix: str, expected_revision: int,
        *, owner_key: str, project: str, tool: str, operation_id: str,
        args_sha256: str, result: dict, job_id: str | None = None,
    ) -> tuple[str, dict, int]:
        """Atomically carry explicit folder rules across one directory rename.

        Inherited policy is intentionally not copied: it is evaluated at the
        destination.  Only explicit rules at the moved directory and its
        descendants move, preserving both explicit inclusions and exclusions.
        The receipt/CAS shape mirrors ``set_folder_rule`` so a retry after the
        source directory is gone is still safely replayable.
        """
        old_prefix = normalize_project_path(old_prefix)
        new_prefix = normalize_project_path(new_prefix)
        old_marker = old_prefix + "/"
        with self.transaction() as connection:
            receipt = connection.execute(
                "SELECT args_sha256,payload_json FROM operation_receipts "
                "WHERE owner_key=? AND project=? AND tool=? AND operation_id=?",
                (owner_key, project, tool, operation_id),
            ).fetchone()
            if receipt is not None:
                if receipt["args_sha256"] != args_sha256:
                    return "conflict", {}, -1
                return "replay", json.loads(receipt["payload_json"]), -1
            row = connection.execute(
                "SELECT policy_revision FROM folder_policy WHERE singleton=1"
            ).fetchone()
            if row is None:
                raise ProjectStateError("folder policy state is missing")
            current = int(row["policy_revision"])
            if current != expected_revision:
                return "stale", {}, current
            rules = connection.execute(
                "SELECT path,indexed FROM folder_rules ORDER BY path"
            ).fetchall()
            moving = {
                item["path"]: new_prefix + item["path"][len(old_prefix):]
                for item in rules
                if item["path"] == old_prefix or item["path"].startswith(old_marker)
            }
            destinations = set(moving.values())
            existing_destinations = {
                item["path"] for item in rules
                if item["path"] not in moving and item["path"] in destinations
            }
            if existing_destinations:
                raise ProjectStateError("directory move conflicts with destination folder rules")
            for source in moving:
                connection.execute("DELETE FROM folder_rules WHERE path=?", (source,))
            for item in rules:
                destination = moving.get(item["path"])
                if destination is not None:
                    connection.execute(
                        "INSERT INTO folder_rules(path,indexed) VALUES(?,?)",
                        (destination, item["indexed"]),
                    )
            next_revision = current + 1
            connection.execute(
                "UPDATE folder_policy SET policy_revision=? WHERE singleton=1",
                (next_revision,),
            )
            if job_id is not None:
                connection.execute(
                    "INSERT INTO policy_jobs(job_id,revision,state,details_json) VALUES(?,?,?,?)",
                    (job_id, next_revision, "queued", "{}"),
                )
            value = dict(result)
            value["policy_revision"] = next_revision
            connection.execute(
                "INSERT INTO operation_receipts(owner_key,project,tool,operation_id,args_sha256,payload_json) "
                "VALUES(?,?,?,?,?,?)",
                (owner_key, project, tool, operation_id, args_sha256,
                 json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))),
            )
            return "committed", value, next_revision

    def update_policy_job(self, job_id: str, state: str, details: dict) -> None:
        with self.transaction() as connection:
            cursor = connection.execute(
                "UPDATE policy_jobs SET state=?,details_json=?,updated_at=CURRENT_TIMESTAMP "
                "WHERE job_id=?",
                (state, json.dumps(details, sort_keys=True, separators=(",", ":")), job_id),
            )
            if cursor.rowcount != 1:
                raise ProjectStateError("policy_job_not_found")

    def policy_job(self, job_id: str) -> dict | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT job_id,revision,state,details_json,updated_at FROM policy_jobs WHERE job_id=?",
                (job_id,),
            ).fetchone()
        if row is None:
            return None
        return {
            "job_id": row["job_id"], "policy_revision": int(row["revision"]),
            "state": row["state"], "details": json.loads(row["details_json"]),
            "updated_at": row["updated_at"],
        }

    def pending_policy_jobs(self) -> list[dict]:
        """Durable derived-index work that must run before watcher publication."""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT job_id,revision,state,details_json,updated_at FROM policy_jobs "
                "WHERE state IN ('queued','running','pending') ORDER BY updated_at,job_id"
            ).fetchall()
        return [{
            "job_id": row["job_id"], "policy_revision": int(row["revision"]),
            "state": row["state"], "details": json.loads(row["details_json"]),
            "updated_at": row["updated_at"],
        } for row in rows]

    def begin_managed_write(
        self, *, job_id: str, source_path: str, bytes_sha256: str,
        operation_id: str | None,
    ) -> tuple[str, str]:
        """Persist pending derived-index status after source bytes are published.

        A supplied operation ID deduplicates retries for one path and payload;
        callers without one get a new durable job for each published write.
        """
        with self.transaction() as connection:
            if operation_id is not None:
                prior = connection.execute(
                    "SELECT job_id,bytes_sha256 FROM managed_write_jobs "
                    "WHERE source_path=? AND operation_id=?",
                    (source_path, operation_id),
                ).fetchone()
                if prior is not None:
                    if prior["bytes_sha256"] != bytes_sha256:
                        raise ProjectStateError("managed_write_operation_conflict")
                    return "replay", prior["job_id"]
            connection.execute(
                "INSERT INTO managed_write_jobs(job_id,source_path,bytes_sha256,operation_id,state) "
                "VALUES(?,?,?,?, 'pending')",
                (job_id, source_path, bytes_sha256, operation_id),
            )
            return "created", job_id

    def finish_managed_write(
        self, *, job_id: str, state: str, error: dict | None,
        doc_id: str | None, extracted_sha256: str | None,
    ) -> dict:
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT job_id,state,error_json,doc_id,extracted_sha256 "
                "FROM managed_write_jobs WHERE job_id=?", (job_id,),
            ).fetchone()
            if row is None:
                raise ProjectStateError("managed_write_job_not_found")
            if row["state"] != "pending":
                # Completion is idempotent only for the same terminal facts.
                prior_error = json.loads(row["error_json"]) if row["error_json"] else None
                if (row["state"], prior_error, row["doc_id"], row["extracted_sha256"]) != (
                    state, error, doc_id, extracted_sha256
                ):
                    raise ProjectStateError("managed_write_result_conflict")
            else:
                connection.execute(
                    "UPDATE managed_write_jobs SET state=?,error_json=?,doc_id=?,extracted_sha256=?,"
                    "updated_at=CURRENT_TIMESTAMP WHERE job_id=?",
                    (state, json.dumps(error, sort_keys=True, separators=(",", ":")) if error else None,
                     doc_id, extracted_sha256, job_id),
                )
            return {"state": state, "job_id": job_id, "error": error}

    def managed_write_status(self, source_path: str) -> dict | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT state,job_id,error_json FROM managed_write_jobs "
                "WHERE source_path=? ORDER BY updated_at DESC,rowid DESC LIMIT 1",
                (source_path,),
            ).fetchone()
        if row is None:
            return None
        return {
            "state": row["state"], "job_id": row["job_id"],
            "error": json.loads(row["error_json"]) if row["error_json"] else None,
        }

    def receipt(
        self, *, owner_key: str, project: str, tool: str, operation_id: str,
    ) -> tuple[str, dict] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT args_sha256,payload_json FROM operation_receipts "
                "WHERE owner_key=? AND project=? AND tool=? AND operation_id=?",
                (owner_key, project, tool, operation_id),
            ).fetchone()
        return None if row is None else (row["args_sha256"], json.loads(row["payload_json"]))

    def save_view(
        self, *, view_id: str, chapter_id: str, scope_json: str,
        payload: dict, expires_at: str,
    ) -> None:
        with self.transaction() as connection:
            connection.execute(
                "INSERT INTO book_views(view_id,chapter_id,scope_json,payload_json,expires_at) "
                "VALUES(?,?,?,?,?)",
                (view_id, chapter_id, scope_json,
                 json.dumps(payload, ensure_ascii=False, separators=(",", ":")), expires_at),
            )

    def load_view(self, view_id: str) -> dict | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT chapter_id,scope_json,payload_json,expires_at "
                "FROM book_views WHERE view_id=?",
                (view_id,),
            ).fetchone()
        if row is None:
            return None
        return {
            "chapter_id": row["chapter_id"], "scope_json": row["scope_json"],
            "payload": json.loads(row["payload_json"]), "expires_at": row["expires_at"],
        }

    def namespace(self, chapter_id: str, scope_key: str) -> dict | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT manifest_revision,media_revision,head_revision,current_snapshot_id,"
                "current_plan_sha256 FROM book_namespaces WHERE chapter_id=? AND scope_key=?",
                (chapter_id, scope_key),
            ).fetchone()
        return None if row is None else dict(row)

    def snapshot(self, snapshot_id: str) -> dict | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT chapter_id,scope_key,manifest_revision,payload_json "
                "FROM book_snapshots WHERE snapshot_id=?",
                (snapshot_id,),
            ).fetchone()
        if row is None:
            return None
        return {
            "chapter_id": row["chapter_id"], "scope_key": row["scope_key"],
            "manifest_revision": int(row["manifest_revision"]),
            "payload": json.loads(row["payload_json"]),
        }

    def commit_snapshot(
        self, *, snapshot_id: str, chapter_id: str, scope_key: str,
        expected_manifest_revision: int | None, payload: dict,
        request_plan_sha256: str, receipt_owner_key: str, project: str,
        tool: str, operation_id: str, args_sha256: str, result: dict,
    ) -> tuple[str, int, dict]:
        """Commit one immutable snapshot reference under the chapter namespace CAS."""
        with self.transaction() as connection:
            prior = connection.execute(
                "SELECT args_sha256,payload_json FROM operation_receipts "
                "WHERE owner_key=? AND project=? AND tool=? AND operation_id=?",
                (receipt_owner_key, project, tool, operation_id),
            ).fetchone()
            if prior is not None:
                if prior["args_sha256"] != args_sha256:
                    raise ProjectStateError("operation_id_conflict")
                return "replay", -1, json.loads(prior["payload_json"])
            row = connection.execute(
                "SELECT manifest_revision FROM book_namespaces "
                "WHERE chapter_id=? AND scope_key=?",
                (chapter_id, scope_key),
            ).fetchone()
            current = None if row is None else row["manifest_revision"]
            if current != expected_manifest_revision:
                raise ProjectStateError("stale_manifest")
            next_revision = 1 if current is None else int(current) + 1
            committed_result = dict(result)
            committed_result["manifest_revision"] = next_revision
            connection.execute(
                "INSERT INTO book_snapshots(snapshot_id,chapter_id,scope_key,manifest_revision,payload_json) "
                "VALUES(?,?,?,?,?)",
                (snapshot_id, chapter_id, scope_key, next_revision,
                 json.dumps(payload, ensure_ascii=False, separators=(",", ":"))),
            )
            connection.execute(
                "INSERT INTO book_namespaces(chapter_id,scope_key,manifest_revision,media_revision,"
                "head_revision,current_snapshot_id,current_plan_sha256) VALUES(?,?,?,0,NULL,?,?) "
                "ON CONFLICT(chapter_id,scope_key) DO UPDATE SET "
                "manifest_revision=excluded.manifest_revision,current_snapshot_id=excluded.current_snapshot_id,"
                "current_plan_sha256=excluded.current_plan_sha256",
                (chapter_id, scope_key, next_revision, snapshot_id, request_plan_sha256),
            )
            connection.execute(
                "INSERT INTO operation_receipts(owner_key,project,tool,operation_id,args_sha256,payload_json) "
                "VALUES(?,?,?,?,?,?)",
                (receipt_owner_key, project, tool, operation_id, args_sha256,
                 json.dumps(committed_result, ensure_ascii=False, separators=(",", ":"))),
            )
            return "committed", next_revision, committed_result

__all__ = [
    "STATE_DIRECTORY", "DATABASE_FILENAME", "INITIALIZED_FILENAME",
    "BOOTSTRAP_FILENAME", "SCHEMA_VERSION", "ProjectStateError", "FolderPolicy",
    "IndexedRoleProvenance", "ProjectState",
]
