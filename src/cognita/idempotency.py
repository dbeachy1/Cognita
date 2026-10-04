"""Replay a retried write instead of re-running it (5.2, review finding C6).

The problem, in the exact shape a client hits it. Every mutating call is
serialized behind the per-project write lock, and some of them are slow — a
300-file `remove_directory` backs up and unlinks 300 files, `reindex_documents`
walks a corpus. A connector that times out client-side and retries does NOT
cancel the first attempt: it is still running, still holding the lock. The retry
queues behind it and then executes against the state the first attempt already
produced. So the caller is told:

| retried call | what it is told |
|---|---|
| `add_document` / `update_document` with `expected_sha256` | `stale_file` — the guard firing against the client's OWN successful write |
| `remove_document(delete_file=true)` | `not_found` |
| `move_document` | `not_found` or `destination_exists` |
| `copy_directory` (overwrite=false) | `destination_exists`, listing every file it just correctly wrote |

Every one of those is a confidently wrong answer for an operation that
SUCCEEDED. It is the same class of defect as 5.0.2's "one observation, two
possible states" — the client's only reasonable reading is that the write
failed, and a model acting on that will try to "fix" a corpus that is already
correct. Extra `add_document` retries also churn `backups/`.

The fix is an optional `operation_id` the caller generates per logical write. The
first call with a given id runs and its result is remembered; any later call with
the same id returns that stored result verbatim plus `replayed: true`, without
touching the disk or the index again.

Design notes that are load-bearing:

- **Opt-in, and additive.** A call with no `operation_id` behaves exactly as
  before, so nothing that works today changes. Adding an OPTIONAL argument is
  explicitly allowed against the frozen wire contract (CLAUDE.md); renaming or
  requiring one would not be.
- **Keyed per connector and project.** Two connectors may expose the same
  project, and two projects may legitimately use the same id; neither case may
  replay a result across the other identity.
- **The result is stored, not the fact of success.** Replaying "it worked" would
  leave the caller without the `chunks_added` / `backup_ids` / `deleted_mtime`
  it needs, which is half of why the retry hurt in the first place.
- **Errors are remembered too.** A retry of a call that genuinely failed must not
  silently re-attempt a write the caller believes did not happen.
- **In-memory, bounded, TTL'd.** A retry follows its original within seconds to
  minutes; persisting this across restarts would be a database for a problem
  that does not outlive a process. It is honest about that rather than pretending
  to a durability it does not have.
"""

from __future__ import annotations

import json
import logging
import time
from hashlib import sha256

log = logging.getLogger("cognita.idempotency")

# A client retry follows its original within one request timeout. An hour is
# generous for that and short enough that the map cannot become a memory leak.
DEFAULT_TTL_S = 3600.0
# Ceiling on remembered operations. Far above any real burst; the eviction below
# is what stops an adversarial client growing this without bound.
MAX_ENTRIES = 2048

# The argument a caller sends, and the marker that comes back on a replay.
OPERATION_ID_ARG = "operation_id"
REPLAY_MARKER = "replayed"

OPERATION_ID_PROPERTY: dict = {
    "type": "string",
    "description": (
        "Optional client-generated id for THIS logical write, so a retry is safe. "
        "If a call with the same operation_id has already completed on this server, "
        "the stored result is returned verbatim with replayed=true and nothing is "
        "written a second time. Use it when a timeout leaves you unsure whether a "
        "write landed: without it, retrying a write that actually SUCCEEDED reports "
        "stale_file / not_found / destination_exists, which reads as a failure. Any "
        "unique string (a UUID); reuse it only for a genuine retry of the same call."
    ),
}

MAX_OPERATION_ID_LEN = 200


class OperationLog:
    """Remembers completed operations by connector, project, tool, and ID.

    A project can be exposed by several connectors with different permissions.
    Keeping the connector in this identity prevents a replay recorded through
    one connector from becoming a result oracle for another connector.
    """

    def __init__(self, ttl_s: float = DEFAULT_TTL_S, max_entries: int = MAX_ENTRIES):
        self._ttl = ttl_s
        self._max = max_entries
        # (connector, project, tool, operation_id) -> (stored_at, payload, digest)
        self._done: dict[tuple[str, str, str, str], tuple[float, dict, str | None]] = {}

    def _evict(self) -> None:
        now = time.monotonic()
        for key, (stored_at, _payload, _digest) in list(self._done.items()):
            if now - stored_at > self._ttl:
                del self._done[key]
        if len(self._done) > self._max:
            # Oldest first. A burst of unique ids from one client must not push
            # out the entry a slower client is about to retry against, but there
            # is no way to tell those apart — so drop by age, which is at least
            # predictable and matches the TTL's intent.
            for key, _ in sorted(self._done.items(), key=lambda kv: kv[1][0])[
                : len(self._done) - self._max
            ]:
                del self._done[key]

    def get(
        self, connector: str, project: str, tool: str | None = None,
        operation_id: str | None = None,
    ) -> dict | None:
        """The stored result for a completed operation, or None."""
        # Keep the small unit-test/library API source-compatible for callers
        # that predate connector-scoped replay. Gateway callers always provide
        # the four-part identity.
        if operation_id is None:
            operation_id = tool
            tool = project
            project = connector
            connector = "legacy"
        assert project is not None and tool is not None and operation_id is not None
        entry = self._done.get((connector, project, tool, operation_id))
        if entry is None:
            return None
        stored_at, payload, _digest = entry
        if time.monotonic() - stored_at > self._ttl:
            del self._done[(connector, project, tool, operation_id)]
            return None
        return payload

    def remember(self, connector: str, project: str, tool: str,
                 operation_id: str | dict, payload: dict | None = None,
                 request_digest: str | None = None) -> None:
        if payload is None:
            payload = operation_id
            operation_id = tool
            tool = project
            project = connector
            connector = "legacy"
        assert isinstance(operation_id, str) and payload is not None
        # Insert THEN evict: evicting first trims to _max and the insert that
        # follows takes it to _max + 1, so the stated bound would never actually
        # hold. Cheap to get wrong, and the only thing standing between an
        # adversarial client and unbounded growth.
        self._done[(connector, project, tool, operation_id)] = (
            time.monotonic(), payload, request_digest
        )
        self._evict()

    def lookup(
        self, connector: str, project: str, tool: str, operation_id: str,
        request_digest: str,
    ) -> tuple[str, dict | None]:
        """Return ``(missing|replay|conflict, payload)`` for a retried call."""
        key = (connector, project, tool, operation_id)
        entry = self._done.get(key)
        if entry is None:
            return "missing", None
        stored_at, payload, saved_digest = entry
        if time.monotonic() - stored_at > self._ttl:
            del self._done[key]
            return "missing", None
        if saved_digest is not None and saved_digest != request_digest:
            return "conflict", None
        return "replay", payload

    def clear(self) -> None:
        self._done.clear()


def operation_request_digest(message: dict) -> str:
    """Hash the logical tool call, excluding JSON-RPC transport metadata.

    MCP request IDs and ``params._meta`` fields such as progress tokens belong
    to an individual transport attempt. The replay identity is the method,
    tool name, and tool arguments; connector, project, and operation ID are
    already part of the operation-log key.
    """
    params = message.get("params")
    if isinstance(params, dict):
        content = {
            "method": message.get("method"),
            "name": params.get("name"),
            "arguments": params.get("arguments", {}),
        }
    else:
        # Preserve deterministic conflict behavior for malformed/non-tool
        # messages should a future caller use this helper outside tools/call.
        content = {key: value for key, value in message.items() if key != "id"}
    return sha256(json.dumps(content, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=False).encode("utf-8")).hexdigest()

def normalize_operation_id(raw) -> tuple[str | None, dict | None]:
    """(operation_id, None) or (None, error payload).

    Absent is the ordinary case and is not an error — the argument is optional
    and everything works without it.
    """
    if raw is None or raw == "":
        return None, None
    if not isinstance(raw, str):
        return None, {
            "status": "error", "reason": "invalid",
            "message": f"{OPERATION_ID_ARG} must be a string; got {type(raw).__name__}.",
        }
    value = raw.strip()
    if not value:
        return None, None
    if len(value) > MAX_OPERATION_ID_LEN:
        return None, {
            "status": "error", "reason": "invalid",
            "message": (f"{OPERATION_ID_ARG} is longer than {MAX_OPERATION_ID_LEN} "
                        "characters. Use a UUID."),
        }
    return value, None


def with_operation_id_argument(tool_def: dict) -> dict:
    """A copy of `tool_def` advertising the optional operation_id argument.

    The schema has to say so, or a caller cannot discover the argument and the
    strict-argument gate would refuse it — which would make the feature
    unreachable and, worse, look like the "silently ignored filter" failure 5.0
    §2 exists to prevent, inverted.
    """
    out = dict(tool_def)
    schema = dict(out.get("inputSchema") or {})
    properties = dict(schema.get("properties") or {})
    properties.setdefault(OPERATION_ID_ARG, OPERATION_ID_PROPERTY)
    schema["properties"] = properties
    out["inputSchema"] = schema
    return out
