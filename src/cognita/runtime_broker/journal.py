"""Small durable idempotency journal for broker mutating requests."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from typing import Any
from uuid import UUID


def request_digest(request: dict[str, Any]) -> str:
    """Hash the operation, workspace, generation, and arguments canonically."""

    payload = json.dumps(request, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class JournalEntry:
    request_id: UUID
    digest: str
    response: dict[str, Any]


class RequestJournal:
    """Persist request results without ever storing bearer credentials.

    The database path is supplied by the runtime container.  No caller field is
    used as a path, and the journal stores only bounded JSON responses.
    """

    def __init__(self, path: str = ":memory:") -> None:
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS broker_requests ("
            "request_id TEXT PRIMARY KEY, digest TEXT NOT NULL, response TEXT NOT NULL)"
        )
        self._db.commit()

    def get(self, request_id: UUID) -> JournalEntry | None:
        row = self._db.execute(
            "SELECT digest, response FROM broker_requests WHERE request_id = ?", (str(request_id),)
        ).fetchone()
        if row is None:
            return None
        return JournalEntry(request_id, row[0], json.loads(row[1]))

    def put(self, request_id: UUID, digest: str, response: dict[str, Any]) -> None:
        """Insert a fully completed result (primarily for tests/imports)."""
        encoded = json.dumps(response, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        self._db.execute(
            "INSERT INTO broker_requests(request_id, digest, response) VALUES (?, ?, ?)",
            (str(request_id), digest, encoded),
        )
        self._db.commit()

    def reserve(self, request_id: UUID, digest: str) -> None:
        """Durably claim a mutating request before its external side effect."""

        self.put(request_id, digest, {"_pending": True})

    def complete(self, request_id: UUID, response: dict[str, Any]) -> None:
        encoded = json.dumps(response, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        cursor = self._db.execute(
            "UPDATE broker_requests SET response = ? WHERE request_id = ?",
            (encoded, str(request_id)),
        )
        if cursor.rowcount != 1:
            self._db.rollback()
            raise RuntimeError("broker request reservation disappeared")
        self._db.commit()

    def close(self) -> None:
        self._db.close()
