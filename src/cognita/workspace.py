"""Workspace lifecycle management and public tool adapter.

The gateway deliberately knows nothing about guest files or runtime names.  This
module owns the principal-to-Workspace mapping, lifecycle admission, bounded
tool arguments, and the small client boundary used to talk to the private
runtime broker.  The broker implementation is intentionally elsewhere.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
import secrets
import sqlite3
import stat
import threading
import time
import unicodedata
import uuid
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

from .workspace_store import (
    CAPACITY_RESERVATION_TTL_SECONDS as CAPACITY_RESERVATION_TTL_SECONDS,
    DISPOSABLE_TABLES as DISPOSABLE_TABLES,
    PRESERVED_TABLES as PRESERVED_TABLES,
    WORKSPACE_SCHEMA_RESET_REQUIRED as WORKSPACE_SCHEMA_RESET_REQUIRED,
    WORKSPACE_SCHEMA_VERSION as WORKSPACE_SCHEMA_VERSION,
    WorkspaceError,
    WorkspaceMetadataStore,
    WorkspaceRecord,
    WorkspaceStateIncompatible as WorkspaceStateIncompatible,
    workspace_reset_command as workspace_reset_command,
)
from .auth_policy import (
    is_self_test_principal,
    self_test_principal_id,
    self_test_principal_matches,
)
from .runtime_broker.protocol import ErrorCode, RpcFailure

log = logging.getLogger("cognita.workspace")

MAX_FILE_BYTES = 1024 * 1024
MAX_PATH_BYTES = 4096
MAX_COMPONENT_BYTES = 255
MAX_LIST_ENTRIES = 2000
MAX_COPY_PATHS = 1000
MAX_SEARCH_ROOTS = 16
MAX_SEARCH_PATTERN_BYTES = 4096
MAX_SEARCH_PATHS = 2000
MAX_SEARCH_MATCHES = 10_000
MAX_JOB_ARGS = 256
MAX_JOB_ARG_BYTES = 256 * 1024
MAX_ENV_KEYS = 128
MAX_ENV_BYTES = 256 * 1024
MAX_JOB_TIMEOUT = 3600
# A1 (DESIGN-12.18 §3.1): the wait_ms bound shared by workspace_start_job and
# workspace_get_job, and the fixed poll cadence _wait_for_job sleeps between
# job_get polls (capped to whatever time remains before the deadline).
MAX_JOB_WAIT_MS = 55000
JOB_WAIT_POLL_SECONDS = 0.5
_WAIT_CAPABLE_TOOLS = frozenset({"workspace_start_job", "workspace_get_job"})
TERMINAL_JOB_STATES = frozenset({"succeeded", "failed", "canceled", "timed_out", "lost"})
# Reasons _wait_for_job treats as "go check whether the Workspace is still
# here" rather than surfacing directly: each can mean the runtime restarted
# under a stale generation as easily as it can mean the Workspace is gone.
_WAIT_RECHECK_REASONS = frozenset({"path_unavailable", "runtime_unavailable", "generation_conflict"})
# A2 (DESIGN-12.18 §3.2): ANSI CSI sequences (ESC [ ... final-byte) and the
# shorter two-character "Fe" escapes (ESC @ through ESC _). Copied verbatim
# from the design so the stripped set matches the documented one exactly.
_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b[@-Z\\-_]")
DEFAULT_QUOTA = 4 * 1024**3
DEFAULT_ROOT_QUOTA = 4 * 1024**3
DEFAULT_IDLE_SECONDS = 30 * 60
DEFAULT_RETENTION_DAYS = 30
DEFAULT_HOST_RESERVE_BYTES = 20 * 1024**3
DELETE_PREVIEW_TTL_SECONDS = 5 * 60
# A reclaim estimate is only truthful for a bounded interval.  This is kept at
# the same scale as the preview lifetime so an apply cannot consume a token
# backed by an arbitrarily old usage sample.
MEASUREMENT_FRESHNESS_SECONDS = DELETE_PREVIEW_TTL_SECONDS
# Measurements and identity/path fields in WorkspaceRecord are authoritative;
# raw broker `inspect` values must not overwrite them in `workspace_info`.
_INFO_MEASUREMENT_KEYS = frozenset({
    "measured_allocated_bytes", "measured_apparent_bytes", "usage_status",
    "measured_at", "host_path", "path_status", "volume_name",
})
_SHA256 = re.compile(r"\A[0-9a-f]{64}\Z")
_ENV_KEY = re.compile(r"\A[A-Za-z_][A-Za-z0-9_]*\Z")


def _safe_job_id(value: Any) -> str | None:
    if not isinstance(value, str) or len(value) > 64:
        return None
    try:
        return str(uuid.UUID(value))
    except (ValueError, TypeError, AttributeError):
        return None


def _stable_broker_request_id(
    workspace_id: str, idempotency_key: str, operation_digest: str,
) -> str:
    """Map an application idempotency key to the broker's UUID request ID.

    Admin tokens are intentionally opaque bounded strings.  The private broker
    protocol uses UUID request IDs for its journal, so pass a deterministic
    UUID5 at this boundary instead of leaking the Admin token into that
    stricter wire field.  Including the operation digest preserves the
    broker's different-content replay guard if a token is misused across
    operations or arguments.
    """
    return str(uuid.uuid5(
        uuid.NAMESPACE_URL,
        f"{workspace_id}:{idempotency_key}:{operation_digest}",
    ))


def _safe_workspace_failure_fields(
    manager: "WorkspaceManager | None", principal: Any, tool: str,
    arguments: Any, *, exception: BaseException, operation: str | None = None,
) -> dict[str, Any]:
    """Build bounded diagnostics without inspecting guest arguments or data."""
    fields: dict[str, Any] = {
        "event": "workspace_tool_failure",
        "tool": tool[:64],
        "exception_class": type(exception).__name__[:64],
        "category": getattr(exception, "reason", "internal_error"),
    }
    if operation:
        fields["operation"] = operation[:64]
    correlation_id = getattr(exception, "fields", {}).get("correlation_id")
    if isinstance(correlation_id, str) and len(correlation_id) <= 64:
        fields["correlation_id"] = correlation_id
    if tool in {"workspace_get_job", "workspace_cancel_job"} and isinstance(arguments, dict):
        job_id = _safe_job_id(arguments.get("job_id"))
        if job_id is not None:
            fields["job_id"] = job_id
    if manager is not None:
        principal_id = getattr(principal, "principal_id", None)
        if principal_id is not None:
            try:
                record = manager.metadata.get_by_principal(str(principal_id))
            except Exception:  # noqa: BLE001 - diagnostics must never affect the response
                record = None
            workspace_id = getattr(record, "workspace_id", None)
            if isinstance(workspace_id, str) and len(workspace_id) <= 64:
                fields["workspace_id"] = workspace_id
    return fields


class RuntimeClient(Protocol):
    def call(
        self, workspace_id: str, operation: str, arguments: dict[str, Any],
        *, expected_runtime_generation: int | None = None, request_id: str | None = None,
    ) -> dict[str, Any]: ...


@dataclass(frozen=True, slots=True)
class StorageSnapshot:
    """Server-owned capacity/measurement snapshot.

    Nullable byte fields are deliberately different from zero: ``None`` means
    the mounted filesystem probe could not establish a value.  The Admin layer
    can therefore render ``Unavailable`` instead of inventing ``0 B``.
    """

    host_root: str | None
    container_root: str | None
    filesystem_capacity_bytes: int | None
    filesystem_free_bytes: int | None
    reserve_bytes: int | None
    admissible_free_bytes: int | None
    workspace_allocated_bytes: int | None
    workspace_apparent_bytes: int | None
    measured_at: str | None
    measurement_status: str
    measurement_reason: str | None
    runtime_probe_status: str
    running_count: int
    running_capacity: int

    def as_dict(self) -> dict[str, Any]:
        return {name: getattr(self, name) for name in self.__dataclass_fields__}


class MountedFilesystemCapacity:
    """Read capacity through a migration-owned marker file mount only.

    The marker is a regular file on the same filesystem as the Workspace root.
    The adapter validates only its filesystem entry with ``lstat`` and then
    reads filesystem metadata with ``statvfs``; it never opens, walks, or
    reads guest Workspace files.
    """

    def __init__(self, root: str | Path):
        self.root = str(root)

    def snapshot(self) -> tuple[int, int]:
        marker = os.lstat(self.root)
        if stat.S_ISLNK(marker.st_mode) or not stat.S_ISREG(marker.st_mode):
            raise OSError(f"capacity marker is not a regular file: {self.root}")
        stats = os.statvfs(self.root)
        block_size = int(stats.f_frsize or stats.f_bsize)
        return int(stats.f_blocks * block_size), int(stats.f_bavail * block_size)


# Stable alias for integrations that prefer a provider-oriented name.
HostCapacityProvider = MountedFilesystemCapacity


class BrokerRuntimeClient:
    """HTTP client for the private broker's typed ``/v1/rpc`` endpoint."""

    def __init__(self, base_url: str, bearer: str, *, timeout: float = 30.0, client: Any = None):
        self.base_url = base_url.rstrip("/")
        self.bearer = bearer
        self.timeout = timeout
        self._client = client

    def call(self, workspace_id: str, operation: str, arguments: dict[str, Any], *, expected_runtime_generation: int | None = None, request_id: str | None = None) -> dict[str, Any]:
        import httpx
        client = self._client or httpx.Client(timeout=self.timeout)
        close = self._client is None
        correlation_id = str(request_id) if request_id else str(uuid.uuid4())
        try:
            body = {
                "request_id": correlation_id, "operation": operation,
                "workspace_id": workspace_id, "arguments": arguments,
            }
            if expected_runtime_generation is not None:
                body["expected_runtime_generation"] = expected_runtime_generation
            response = client.post(
                f"{self.base_url}{'/rpc' if self.base_url.endswith('/v1') else '/v1/rpc'}", json=body,
                headers={"Authorization": f"Bearer {self.bearer}"},
            )
            status_code = getattr(response, "status_code", None)
            if status_code is not None and status_code >= 400:
                # The RPC endpoint uses HTTP 400 only for bounded, typed
                # protocol failures. Authenticate/status-check every other
                # response before looking at its body so arbitrary 401/5xx
                # JSON cannot masquerade as a broker result.
                if status_code == 400:
                    try:
                        payload = RpcFailure.model_validate(response.json()).model_dump(
                            mode="json", exclude_none=True,
                        )
                    except Exception:
                        response.raise_for_status()
                        raise
                else:
                    response.raise_for_status()
            else:
                # Status-less response doubles are retained for existing unit
                # tests; real HTTP responses always expose status_code.
                response.raise_for_status()
                payload = response.json()
            if "code" in payload:
                code = str(payload.get("code"))
                stage = payload.get("stage")
                diagnostics = payload.get("diagnostics")
                diagnostics = diagnostics if isinstance(diagnostics, dict) else {}
                reason = {
                    "busy": "capacity_busy", "quota": "quota_exceeded",
                    "conflict": "path_conflict",
                    "timeout": "job_timeout", "not_found": "path_unavailable",
                    "capacity_unavailable": "capacity_unavailable",
                    "network_denied": "network_denied",
                    "generation_conflict": "generation_conflict",
                    "ownership_mismatch": "ownership_mismatch",
                    ErrorCode.INVALID_REQUEST.value: "invalid_arguments",
                }.get(code, "runtime_unavailable")
                # 13.2.5 (DESIGN-13.2 §4.2): the reason is IN the line — the
                # console formatter used to drop every one of these fields, so
                # one failing copy_to_workspace printed 114 identical warnings
                # that said nothing. And "not there yet" on a stat is the
                # normal answer during a transfer's inventory, not a warning.
                expected_miss = code == "not_found" and operation == "fs_stat"
                log.log(
                    logging.DEBUG if expected_miss else logging.WARNING,
                    "Workspace broker rejected operation=%s category=%s stage=%s reason=%s workspace_id=%s",
                    operation[:64], code[:64], str(stage)[:64] if stage is not None else "-",
                    reason, str(workspace_id)[:64],
                    extra={
                        "event": "workspace_broker_failure", "operation": operation[:64],
                        "stage": str(stage)[:64] if stage is not None else None,
                        "category": code[:64], "correlation_id": str(payload.get("correlation_id") or correlation_id)[:64],
                        "workspace_id": str(workspace_id)[:64],
                        **({"job_id": _safe_job_id(arguments.get("job_id"))} if operation in {"job_get", "job_cancel"} and _safe_job_id(arguments.get("job_id")) is not None else {}),
                    },
                )
                if (
                    code == "conflict"
                    and stage in {"fs_write", "fs_edit"}
                    and all(
                        isinstance(diagnostics.get(key), str)
                        and _SHA256.fullmatch(diagnostics[key])
                        for key in ("expected_sha256", "actual_sha256")
                    )
                ):
                    reason = "stale_file"
                elif code == "timeout" and stage == "fs_search":
                    reason = "search_timeout"
                raise WorkspaceError(
                    reason,
                    "Workspace runtime rejected the operation",
                    reset_runtime_generation=(code == "generation_conflict"),
                    broker_code=code,
                    broker_stage=stage,
                    correlation_id=payload.get("correlation_id"),
                    retryable=bool(payload.get("retryable", False)),
                    **{
                        key: diagnostics[key]
                        for key in ("expected_sha256", "actual_sha256")
                        if isinstance(diagnostics.get(key), str)
                        and _SHA256.fullmatch(diagnostics[key])
                    },
                )
            data = payload.get("data")
            if not isinstance(data, dict):
                raise WorkspaceError("runtime_unavailable", "Workspace runtime returned an invalid result")
            data = dict(data)
            data["_runtime_generation"] = payload.get("runtime_generation")
            return data
        except WorkspaceError:
            raise
        except Exception as exc:
            log.warning(
                "Workspace runtime call failed",
                extra={
                    "event": "workspace_runtime_failure", "operation": operation[:64],
                    "category": "transport_failure", "exception_class": type(exc).__name__[:64],
                    "correlation_id": correlation_id[:64], "workspace_id": str(workspace_id)[:64],
                    **({"job_id": _safe_job_id(arguments.get("job_id"))} if operation in {"job_get", "job_cancel"} and _safe_job_id(arguments.get("job_id")) is not None else {}),
                },
            )
            raise WorkspaceError("runtime_unavailable", "Workspace runtime is unavailable") from exc
        finally:
            if close:
                client.close()

    def health(self) -> dict[str, Any]:
        import httpx
        client = self._client or httpx.Client(timeout=self.timeout)
        close = self._client is None
        try:
            response = client.get(
                f"{self.base_url.removesuffix('/v1')}/healthz"
            )
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict):
                raise TypeError("invalid broker health response")
            return payload
        except Exception as exc:  # noqa: BLE001 - Admin readiness must degrade safely
            log.warning("Workspace runtime health failed reason=%s", type(exc).__name__)
            return {"status": "degraded", "runtime": "unavailable"}
        finally:
            if close:
                client.close()


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(UTC)


def _uuid(value: str, field: str = "id") -> str:
    try:
        parsed = uuid.UUID(str(value))
    except (ValueError, TypeError, AttributeError) as exc:
        raise WorkspaceError("invalid_arguments", f"{field} must be a UUID") from exc
    if parsed.version != 4:
        raise WorkspaceError("invalid_arguments", f"{field} must be a UUIDv4")
    return str(parsed)


def normalize_path(value: str) -> str:
    """Normalize a guest path to a relative path below ``/workspace``."""
    if not isinstance(value, str) or not value or "\\" in value:
        raise WorkspaceError("invalid_arguments", "path must be a non-empty POSIX path")
    if len(value.encode("utf-8")) > MAX_PATH_BYTES or "\x00" in value:
        raise WorkspaceError("invalid_arguments", "path exceeds the Workspace path limit")
    if unicodedata.normalize("NFC", value) != value:
        raise WorkspaceError("invalid_arguments", "path must use NFC Unicode normalization")
    if value.startswith("/workspace/"):
        value = value[len("/workspace/"):]
    elif value == "/workspace" or value == "/":
        return ""
    elif value.startswith("/"):
        raise WorkspaceError("path_unavailable", "path is outside /workspace")
    parts = value.split("/")
    if any(not part or part in {".", ".."} for part in parts):
        raise WorkspaceError("invalid_arguments", "path contains an invalid component")
    for part in parts:
        if len(part.encode("utf-8")) > MAX_COMPONENT_BYTES or any(
            unicodedata.category(char).startswith("C") for char in part
        ):
            raise WorkspaceError("invalid_arguments", "path contains an invalid component")
    if parts and parts[0] in {"proc", "sys", "dev", "etc", ".cognita"}:
        # /.cognita is runtime metadata, and host pseudo-filesystems are never public.
        raise WorkspaceError("path_unavailable", "path is not available to Workspace tools")
    return "/".join(parts)


def _bounded_string(value: Any, name: str, limit: int = MAX_PATH_BYTES) -> str:
    if not isinstance(value, str) or len(value.encode("utf-8")) > limit:
        raise WorkspaceError("invalid_arguments", f"{name} exceeds its bound")
    return value



def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


class WorkspaceManager:
    """Principal-scoped Workspace lifecycle and strict public operation facade."""

    def __init__(self, metadata: WorkspaceMetadataStore, runtime: RuntimeClient | None = None, *, quota_bytes: int = DEFAULT_QUOTA, root_quota_bytes: int = DEFAULT_ROOT_QUOTA, idle_seconds: int = DEFAULT_IDLE_SECONDS, retention_days: int = DEFAULT_RETENTION_DAYS, host_reserve_bytes: int = DEFAULT_HOST_RESERVE_BYTES, host_free_bytes: Callable[[], int] | None = None, capacity_provider: MountedFilesystemCapacity | None = None, host_root: str | None = None, container_root: str | None = "/root/.microsandbox", strict_capacity: bool = False, clock: Callable[[], datetime] | None = None, sleep: Callable[[float], None] | None = None, network_policy: dict[str, Any] | None = None, max_running_workspaces: int = 4, brave_search: Any = None, credential_is_active: Callable[[str], bool | None] | None = None):
        self.metadata = metadata
        self.runtime = runtime
        self.quota_bytes = quota_bytes
        self.root_quota_bytes = root_quota_bytes
        self.idle_seconds = idle_seconds
        self.retention_days = retention_days
        self.host_reserve_bytes = host_reserve_bytes
        self.host_free_bytes = host_free_bytes
        self.capacity_provider = capacity_provider
        self.host_root = host_root or (str(capacity_provider.root) if capacity_provider is not None else None)
        self.container_root = container_root
        self.strict_capacity = strict_capacity
        self.clock = clock or (lambda: datetime.now(UTC))
        # A1 (DESIGN-12.18 §3.1): _wait_for_job's poll cadence, injected so
        # tests never depend on a real timer. Defaults to the real time.sleep.
        self.sleep = sleep or time.sleep
        self.network_policy = network_policy or {"mode": "off", "rules": [], "explicit_confirmation": False}
        self.max_running_workspaces = max_running_workspaces
        self.brave_search = brave_search
        # The credential policy remains the owner of tombstones; this narrow
        # read-only callback lets lazy admission honor that durable state.
        self.credential_is_active = credential_is_active
        self._queues: dict[str, threading.RLock] = {}
        self._principal_queues: dict[str, threading.RLock] = {}
        self._queues_lock = threading.Lock()
        self._capacity_lock = threading.RLock()
        # Track the last trusted fs_usage sample separately: record.measured_at
        # is refreshed by broker inspect on admitted calls and cannot determine
        # whether the apparent-bytes sample itself is older than 60 seconds.
        # After restart, the next mutation measures again.
        self._apparent_measured_at: dict[str, datetime] = {}

    def _capacity_stats(self) -> tuple[int | None, int | None]:
        if self.capacity_provider is not None:
            try:
                capacity, free = self.capacity_provider.snapshot()
                if capacity < 0 or free < 0 or free > capacity:
                    raise ValueError("invalid filesystem capacity")
                return capacity, free
            except Exception as exc:
                if self.strict_capacity:
                    raise WorkspaceError("capacity_unavailable", "Workspace host capacity is unavailable") from exc
                return None, None
        if self.host_free_bytes is not None:
            try:
                return None, int(self.host_free_bytes())
            except Exception as exc:
                raise WorkspaceError("capacity_unavailable", "Workspace host capacity is unavailable") from exc
        if self.strict_capacity:
            raise WorkspaceError("capacity_unavailable", "Workspace host capacity is unavailable")
        return None, None

    def _effective_reserve(self, capacity: int | None) -> int | None:
        if capacity is None:
            return self.host_reserve_bytes if not self.strict_capacity else None
        # ``host_reserve_bytes`` is the configured policy floor.  The default
        # value is already ``DEFAULT_HOST_RESERVE_BYTES``; adding that constant
        # here as a second floor would silently override an explicitly smaller
        # reserve used for a bounded filesystem (and make the 10% policy
        # impossible to exercise).  A configured reserve is still raised to
        # 10% of the mounted filesystem, as required by the host-admission
        # contract.
        return max(self.host_reserve_bytes, capacity // 10)

    def storage_snapshot(self) -> StorageSnapshot:
        """Return one consistent server-owned storage/runtime snapshot."""
        now = self.clock().isoformat(timespec="seconds")
        capacity: int | None = None
        free: int | None = None
        reason: str | None = None
        status = "unknown"
        try:
            capacity, free = self._capacity_stats()
            status = "fresh" if free is not None else "unknown"
            if status == "unknown" and self.host_root:
                reason = "capacity_provider_unavailable"
        except WorkspaceError as exc:
            reason, status = exc.reason, "error"
        reserve = self._effective_reserve(capacity)
        records = self.metadata.list()
        try:
            committed = self._committed_growth_bytes()
        except Exception:
            committed = None
        admissible = (
            None
            if free is None or reserve is None or committed is None
            else max(0, free - reserve - committed)
        )
        measured = [row for row in records if row.usage_status == "fresh"]
        allocated = None if len(measured) != len(records) else sum(row.measured_allocated_bytes or 0 for row in measured)
        apparent = None if len(measured) != len(records) else sum(row.measured_apparent_bytes or 0 for row in measured)
        running = sum(row.state in {"creating", "starting", "running"} for row in records)
        runtime_status = "unknown"
        if self.runtime is not None and hasattr(self.runtime, "health"):
            try:
                health = self.runtime.health()
                runtime_status = str(health.get("status", "unknown")) if isinstance(health, dict) else "unknown"
            except Exception:
                runtime_status = "unavailable"
        return StorageSnapshot(
            self.host_root, self.container_root, capacity, free, reserve, admissible,
            allocated, apparent, now if status == "fresh" else None, status, reason,
            runtime_status, running, self.max_running_workspaces,
        )

    def host_admission(self, *, additional_growth_bytes: int = 0, outstanding_transfer_growth: int = 0) -> bool:
        """Apply the documented host-reserve equation when capacity is configured.

        Production managers set ``strict_capacity`` and fail closed when the
        mounted-filesystem probe is absent or stale.  The permissive default is
        retained for lightweight unit callers that do not configure a host root.
        """
        if additional_growth_bytes < 0 or outstanding_transfer_growth < 0:
            raise WorkspaceError("invalid_arguments", "growth reservations cannot be negative")
        capacity, free = self._capacity_stats()
        reserve = self._effective_reserve(capacity)
        if free is None or reserve is None:
            return True  # legacy/unit-test mode; strict production mode fails closed above
        # Lifecycle and bridge reservations mark the active potential they
        # cover. Any current active potential beyond that marker remains a
        # durable commitment even while another hold is present.
        committed = self._committed_growth_bytes()
        return free - outstanding_transfer_growth - additional_growth_bytes - committed >= reserve

    def _committed_growth_bytes(self) -> int:
        """Return held growth plus active capacity not covered by those holds."""
        return self.metadata.held_growth_bytes() + self._uncovered_active_growth()

    def _uncovered_active_growth(self) -> int:
        active = self._active_potential_growth()
        covered = min(active, self.metadata.held_active_growth_bytes())
        return max(0, active - covered)

    def _reserve_admission(
        self,
        growth_bytes: int,
        *,
        request_key: str,
        covered_active_growth_bytes: int = 0,
        active_growth_supplier: Callable[[], int] | None = None,
        active_growth_always: bool = False,
    ) -> str | None:
        if self.capacity_provider is None and self.host_free_bytes is None:
            if self.strict_capacity:
                raise WorkspaceError("capacity_unavailable", "Workspace host capacity is unavailable")
            return None
        capacity, _free = self._capacity_stats()
        reserve = self._effective_reserve(capacity)
        if reserve is None:
            raise WorkspaceError("capacity_unavailable", "Workspace host capacity is unavailable")
        if self.capacity_provider is not None:
            def free_supplier() -> int:
                return int(self.capacity_provider.snapshot()[1])
        else:
            def free_supplier() -> int:
                return int(self.host_free_bytes())
        return self.metadata.reserve_growth(
            growth_bytes, free_bytes_supplier=free_supplier, reserve_bytes=reserve,
            request_key=request_key,
            covered_active_growth_bytes=covered_active_growth_bytes,
            active_growth_supplier=active_growth_supplier,
            active_growth_always=active_growth_always,
        )

    def _reserve_bridge_growth(self, growth_bytes: int, *, request_key: str) -> str | None:
        """Reserve transfer bytes and a fresh active-guest baseline."""
        return self._reserve_admission(
            growth_bytes,
            request_key=request_key,
            active_growth_supplier=self._active_potential_growth,
            active_growth_always=True,
        )

    def _queue(self, workspace_id: str) -> threading.RLock:
        with self._queues_lock:
            return self._queues.setdefault(workspace_id, threading.RLock())

    def _principal_queue(self, principal_id: str) -> threading.RLock:
        with self._queues_lock:
            return self._principal_queues.setdefault(principal_id, threading.RLock())

    @contextmanager
    def credential_deletion_gate(self, principal_id: str):
        """Serialize credential target validation with first-use admission.

        Credential policy owns the tombstone transaction, while this gate owns
        the shared principal admission lock. Keeping both operations inside the
        same lock closes the validation-to-tombstone gap without introducing a
        second lifecycle queue.
        """
        with self._principal_queue(str(principal_id)):
            yield

    def _inspect_reconciled(self, record: WorkspaceRecord) -> tuple[dict[str, Any], WorkspaceRecord]:
        """Inspect the workspace, retrying ONCE after a broker generation change.

        A generation_conflict means the broker restarted since this row last
        talked to it; _runtime_call has already reset the row's expectation, so
        the retry carries the current generation. Never loops: a second
        conflict is a real disagreement and propagates. Returns the observation
        and the reloaded row (its runtime_generation moved).
        """
        for attempt in range(2):
            try:
                return self._runtime_call(record, "inspect", {}), record
            except WorkspaceError as exc:
                if exc.reason == "generation_conflict" and attempt == 0:
                    log.info("Workspace inspect retried once after broker generation change workspace_id=%s",
                             record.workspace_id[:64])
                    record = self.metadata.get(record.workspace_id) or record
                    continue
                raise
        raise AssertionError("unreachable")  # pragma: no cover

    def _runtime_call(self, record: WorkspaceRecord, operation: str, arguments: dict[str, Any], *, request_id: str | None = None) -> dict[str, Any]:
        if self.runtime is None:
            raise WorkspaceError("runtime_unavailable", "Workspace runtime is not configured")
        try:
            result = self.runtime.call(
                record.workspace_id,
                operation,
                arguments,
                expected_runtime_generation=(record.runtime_generation or None),
                request_id=request_id,
            )
            observed_generation = result.pop("_runtime_generation", None)
            if isinstance(observed_generation, int) and observed_generation >= 0 and observed_generation != record.runtime_generation:
                self.metadata.update(record.workspace_id, runtime_generation=observed_generation)
            return result
        except WorkspaceError as exc:
            if exc.fields.pop("reset_runtime_generation", False):
                self.metadata.update(record.workspace_id, runtime_generation=0)
            broker_stage = str(exc.fields.get("broker_stage") or operation)[:64]
            broker_category = str(exc.fields.get("broker_code") or exc.reason)[:64]
            # 13.2.5: same rule as RuntimeClient.call — the reason is in the
            # line, and an expected miss on a stat is DEBUG, not a warning.
            expected_miss = exc.reason == "path_unavailable" and operation == "fs_stat"
            log.log(
                logging.DEBUG if expected_miss else logging.WARNING,
                "Workspace runtime operation failed operation=%s reason=%s category=%s stage=%s workspace_id=%s",
                operation[:64], exc.reason, broker_category, broker_stage, record.workspace_id[:64],
                extra=_safe_workspace_failure_fields(
                    self, None, f"workspace_{operation}",
                    {"job_id": arguments.get("job_id")} if operation in {"job_get", "job_cancel"} else {},
                    exception=exc, operation=operation,
                ) | {
                    "workspace_id": record.workspace_id[:64],
                    "broker_stage": broker_stage,
                    "broker_category": broker_category,
                },
            )
            raise
        except Exception as exc:
            log.warning(
                "Workspace operation failed",
                extra=_safe_workspace_failure_fields(
                    self, None, f"workspace_{operation}",
                    {"job_id": arguments.get("job_id")} if operation in {"job_get", "job_cancel"} else {},
                    exception=exc, operation=operation,
                ) | {"workspace_id": record.workspace_id[:64], "category": "runtime_failure"},
            )
            raise WorkspaceError("runtime_unavailable", "Workspace runtime is unavailable") from exc

    def _active_job_after_reconcile(self, record: WorkspaceRecord) -> sqlite3.Row | None:
        """Check the broker before using an application job row as an admission lock.

        On September 20, 2026, an interrupted client stopped polling a job that
        had already succeeded.  Its application row stayed ``running`` across a
        deployment and blocked every later mutation.  The broker and guest own
        job completion; the application row is only a cached observation.
        """
        active = self.metadata.active_job(record.workspace_id)
        if active is None:
            return None
        job_id = str(active["job_id"])
        args = {"job_id": job_id, "stdout_offset": 0, "stderr_offset": 0, "max_bytes": 1}
        for attempt in range(2):
            try:
                observed = self._runtime_call(record, "job_get", args)
                break
            except WorkspaceError as exc:
                if exc.reason == "generation_conflict" and attempt == 0:
                    # _runtime_call cleared the obsolete expectation. Retry
                    # once against the current broker generation; never loop.
                    record = self.metadata.get(record.workspace_id) or record
                    continue
                exc.fields.setdefault("job_id", job_id)
                raise
        state = observed.get("state")
        if state in TERMINAL_JOB_STATES:
            self.metadata.update_job_state(record.workspace_id, job_id, state)
            log.info("Workspace job lock reconciled workspace_id=%s job_id=%s state=%s",
                     record.workspace_id, job_id, state)
            # If an older active row survived an interrupted transition, keep
            # admission closed until a later bounded check reconciles it too.
            return self.metadata.active_job(record.workspace_id)
        if state not in {"queued", "running"}:
            raise WorkspaceError("runtime_unavailable", "Workspace job state is unavailable",
                                 job_id=job_id, retryable=True)
        if state != active["state"]:
            self.metadata.update_job_state(record.workspace_id, job_id, state)
            active = self.metadata.active_job(record.workspace_id) or active
        return active

    def _job_running_error(self, record: WorkspaceRecord, active: sqlite3.Row) -> WorkspaceError:
        """Give the authenticated caller enough identity to inspect or cancel its job."""
        correlation_id = str(uuid.uuid4())
        log.info("Workspace job blocks operation workspace_id=%s job_id=%s correlation_id=%s",
                 record.workspace_id, active["job_id"], correlation_id)
        return WorkspaceError(
            "job_running", "a Workspace job is already running",
            job_id=str(active["job_id"]), state=str(active["state"]),
            started_at=str(active["created_at"]),
            runtime_generation=(self.metadata.get(record.workspace_id) or record).runtime_generation,
            correlation_id=correlation_id, retryable=True,
        )

    def _cache_observed_job_state(self, workspace_id: str, job_id: str, state: str) -> None:
        """Cache a terminal state without overriding the broker's job result."""
        if not self.metadata.cache_job_state(workspace_id, job_id, state):
            log.debug(
                "Workspace job state was not cached because its admission row is absent workspace_id=%s job_id=%s",
                workspace_id, job_id,
            )

    def active_job_after_reconcile(self, workspace_id: str) -> sqlite3.Row | None:
        """Expose a bounded, workspace-scoped job check to bridge admission."""
        with self._queue(workspace_id):
            record = self.metadata.get(workspace_id)
            if record is None:
                raise WorkspaceError("path_unavailable", "Workspace was not found")
            return self._active_job_after_reconcile(record)

    def _verified_runtime_path(self, value: Any) -> tuple[str | None, str]:
        if not isinstance(value, str) or not value or self.host_root is None:
            return None, "not_reported"
        try:
            # Resolve the broker-reported path before containment checking so
            # a junction/symlink cannot make an outside target look rooted.
            root = os.path.normcase(os.path.realpath(self.host_root))
            path = os.path.normcase(os.path.realpath(value))
            if os.path.commonpath((root, path)) != root:
                return None, "not_reported"
            return os.path.realpath(value), "verified"
        except (OSError, ValueError):
            return None, "not_reported"

    def _apply_runtime_measurement(
        self, record: WorkspaceRecord, observed: dict[str, Any], *, preserve_revision: bool = False,
    ) -> WorkspaceRecord:
        allocated = observed.get("allocated_bytes")
        if allocated is None:
            allocated = observed.get("actual_bytes")
        if allocated is None:
            allocated = observed.get("measured_allocated_bytes")
        # Only guest-side fs_usage provides a trusted apparent-bytes sample.
        # Broker inspect may report zero despite files being present, so reject
        # its apparent value. Other callers preserve the stored sample by
        # passing None; allocated-byte measurements are handled separately.
        apparent = None
        if observed.get("apparent_source") == "fs_usage":
            apparent = observed.get("apparent_bytes")
            if apparent is None:
                apparent = observed.get("used_bytes")
            if apparent is None:
                apparent = observed.get("measured_apparent_bytes")
        if not all(value is None or isinstance(value, int) and not isinstance(value, bool) and value >= 0 for value in (allocated, apparent)):
            allocated = apparent = None
        reported_path = observed.get("host_path")
        path, path_status = self._verified_runtime_path(reported_path)
        if "host_path" not in observed:
            path, path_status = record.host_path, record.path_status
        unavailable = observed.get("apparent_source") == "unavailable"
        if allocated is None and apparent is None:
            # An inspect response without a usable allocated sample -- and no
            # TRUSTED apparent sample, per the apparent_source gate above --
            # cannot refresh a reclaim estimate.  Preserve identity/path
            # evidence, but mark the cached byte values unusable instead of
            # extending their lifetime.
            #
            # A failed refresh cannot extend sample freshness. Preserve the
            # status when a trusted apparent-bytes sample remains stored;
            # otherwise report usage as unknown. None byte values leave stored
            # measurements unchanged.
            preserved_usage_status = record.usage_status if record.measured_apparent_bytes is not None else "unknown"
            if unavailable:
                preserved_usage_status = "unknown"
            return self.metadata.update_measurement(
                record.workspace_id, measured_at=self.clock().isoformat(timespec="seconds"),
                usage_status=preserved_usage_status, host_path=path, path_status=path_status,
                volume_name=observed.get("volume_name") if isinstance(observed.get("volume_name"), str) else None,
                bump_revision=not preserve_revision,
            )
        usage_status = record.usage_status
        if unavailable:
            usage_status = "unknown"
        elif apparent is not None or record.usage_status == "fresh":
            usage_status = "fresh"
        return self.metadata.update_measurement(
            record.workspace_id, allocated_bytes=allocated, apparent_bytes=apparent,
            measured_at=self.clock().isoformat(timespec="seconds"),
            # Allocated bytes from an ordinary inspect cannot revalidate an
            # apparent sample whose last fs_usage attempt was incomplete.
            usage_status=usage_status,
            host_path=path, path_status=path_status,
            volume_name=observed.get("volume_name") if isinstance(observed.get("volume_name"), str) else None,
            bump_revision=not preserve_revision,
        )

    def _note_fs_usage_evidence(
        self, workspace_id: str, observed: dict[str, Any], usage: dict[str, Any] | None,
    ) -> int | None:
        """Admit only a complete broker total as a current apparent sample."""
        total = usage.get("total_bytes") if isinstance(usage, dict) else None
        if isinstance(total, int) and not isinstance(total, bool) and total >= 0:
            observed["measured_apparent_bytes"] = total
            observed["apparent_source"] = "fs_usage"
            self._apparent_measured_at[workspace_id] = self.clock()
            return total
        observed["apparent_source"] = "unavailable"
        self._apparent_measured_at.pop(workspace_id, None)
        return None

    def refresh_usage(self, workspace_id: str, *, preserve_revision: bool = False) -> dict[str, Any]:
        """Refresh only bounded runtime metadata; never read guest file contents."""
        record = self.metadata.get(workspace_id)
        if record is None:
            raise WorkspaceError("path_unavailable", "Workspace was not found")
        try:
            observed = self._runtime_call(record, "inspect", {})
            if observed.get("state") == "running":
                # Refresh apparent usage from guest-side fs_usage rather than
                # the SDK volume counter, which can omit files. A failed or
                # incomplete result leaves the stored number historical and
                # marks current quota evidence unavailable.
                try:
                    usage = self._runtime_call(record, "fs_usage", {"path": "/workspace"})
                except WorkspaceError as exc:
                    log.info(
                        "Workspace refresh_usage fs_usage unavailable workspace_id=%s reason=%s",
                        workspace_id, exc.reason,
                    )
                    self._note_fs_usage_evidence(workspace_id, observed, None)
                else:
                    inspected_apparent = observed.get("measured_apparent_bytes")
                    total = self._note_fs_usage_evidence(workspace_id, observed, usage)
                    if total is not None:
                        log.info(
                            "Workspace refresh_usage fs_usage total_bytes=%d replaces inspect measured_apparent_bytes=%s workspace_id=%s",
                            total, inspected_apparent, workspace_id,
                        )
                    else:
                        log.info(
                            "Workspace refresh_usage fs_usage returned no usable total_bytes workspace_id=%s total_bytes=%r",
                            workspace_id, total,
                        )
            updated = self._apply_runtime_measurement(record, observed, preserve_revision=preserve_revision)
            return updated.as_dict()
        except WorkspaceError as exc:
            self.metadata.update_measurement(
                workspace_id, usage_status="error", measured_at=self.clock().isoformat(timespec="seconds"),
                bump_revision=not preserve_revision,
            )
            raise WorkspaceError(exc.reason, "Workspace measurement is unavailable", **exc.fields) from exc

    def _measure_after_mutation(self, workspace_id: str) -> None:
        """Refresh stale usage after mutations without failing the mutation.

        Only a trusted fs_usage sample advances the freshness clock.
        Ordinary inspection updates the stored measurement timestamp, so
        that field cannot decide whether this response needs a refresh.
        A failed sample leaves prior byte counts historical and current
        quota availability unknown.
        """
        current = self.metadata.get(workspace_id)
        if current is None:
            return
        last_measured = self._apparent_measured_at.get(workspace_id)
        stale = last_measured is None
        if not stale:
            stale = (self.clock() - last_measured).total_seconds() >= 60
        if not stale:
            return
        try:
            self.refresh_usage(workspace_id, preserve_revision=True)
        except WorkspaceError as exc:
            log.info(
                "Workspace usage measurement after mutation failed workspace_id=%s reason=%s",
                workspace_id, exc.reason,
            )

    def _measurement_is_fresh(self, record: WorkspaceRecord) -> bool:
        """Return whether a byte sample is recent enough for reclaim work."""
        if record.usage_status != "fresh" or (
            record.measured_allocated_bytes is None and record.measured_apparent_bytes is None
        ) or not record.measured_at:
            return False
        try:
            age = (self.clock() - _parse_time(record.measured_at)).total_seconds()
        except (TypeError, ValueError, OverflowError):
            return False
        return 0 <= age <= MEASUREMENT_FRESHNESS_SECONDS

    def _fresh_delete_measurement(self, record: WorkspaceRecord) -> WorkspaceRecord:
        """Probe bounded runtime metadata before previewing or applying remove."""
        try:
            values = self.refresh_usage(record.workspace_id, preserve_revision=True)
        except WorkspaceError as exc:
            raise WorkspaceError(
                "measurement_unavailable", "Workspace usage measurement is unavailable",
                broker_reason=exc.reason,
            ) from exc
        updated = self.metadata.get(record.workspace_id)
        if updated is None or not self._measurement_is_fresh(updated):
            raise WorkspaceError("measurement_unavailable", "Workspace usage measurement is stale or unavailable")
        # ``values`` is intentionally not trusted as a detached object; reread
        # the row so the exact persisted path/identity snapshot is compared by
        # the caller immediately before it binds a destructive operation.
        _ = values
        return updated

    def _ownership_proven(self, record: WorkspaceRecord) -> bool:
        """Require broker identity evidence before retrying a failed object."""
        if self.runtime is None:
            return False
        try:
            observed = self._runtime_call(record, "inspect", {})
        except WorkspaceError:
            return False
        if observed.get("state") == "absent":
            return False
        if observed.get("state") == "partial" and (
            observed.get("partial_state") != "volume_only"
            or not isinstance(record.volume_name, str) or not record.volume_name
            or not isinstance(observed.get("volume_name"), str) or not observed.get("volume_name")
            or observed.get("volume_name") != record.volume_name
        ):
            return False
        return self._runtime_identity_matches(record, observed)

    @classmethod
    def _running_recovery_proven(cls, record: WorkspaceRecord, observed: dict[str, Any]) -> bool:
        """Prove a degraded running row may recreate only its existing runtime."""
        state = observed.get("state")
        if state == "absent":
            return cls._runtime_absence_proven(record, observed)
        if state == "partial":
            if observed.get("partial_state") != "volume_only":
                return False
            if not isinstance(record.volume_name, str) or not record.volume_name:
                return False
            if observed.get("volume_name") != record.volume_name:
                return False
        elif state not in {"stopped", "failed"}:
            return False
        if not cls._runtime_identity_matches(record, observed):
            return False
        observed_volume = observed.get("volume_name")
        return observed_volume == record.volume_name if record.volume_name else False

    @staticmethod
    def _runtime_identity_matches(record: WorkspaceRecord, observed: dict[str, Any]) -> bool:
        """Accept only an exact broker/SDK runtime identity.

        Microsandbox 0.7 reports the sandbox as ``sandbox_name``.  Older test
        doubles used ``runtime_name`` or ``workspace_id``; those aliases are
        retained only as exact-value compatibility, never by deriving an
        identity from a volume name or another arbitrary field.  A supplied
        sandbox name is authoritative: a conflicting value must fail closed
        even if a legacy alias happens to match.
        """
        aliases = (
            ("sandbox_name", record.runtime_name),
            ("runtime_name", record.runtime_name),
            ("workspace_id", record.workspace_id),
        )
        supplied = False
        for field, expected in aliases:
            value = observed.get(field)
            if value is None:
                continue
            supplied = True
            if not isinstance(value, str) or value != expected:
                return False
        if not supplied:
            return False
        if observed.get("sandbox_name") is not None:
            return observed.get("sandbox_name") == record.runtime_name
        return any(observed.get(field) == expected for field, expected in aliases[1:])

    @classmethod
    def _runtime_absence_proven(cls, record: WorkspaceRecord, observed: dict[str, Any]) -> bool:
        """Require complete broker proof before metadata-only cleanup.

        A failed first-use admission can leave no persisted volume name even
        though the broker can independently prove that the deterministic
        sandbox and volume names are absent.  That proof is safe only when the
        broker supplies both canonical names and explicit null object IDs;
        metadata must not be repaired from an underspecified ``absent`` state.
        """
        required = {"sandbox_name", "volume_name", "runtime_id", "volume_id"}
        if observed.get("state") != "absent" or not required.issubset(observed):
            return False
        expected_sandbox = f"cognita-ws-{record.workspace_id}"
        expected_volume = f"cognita-ws-data-{record.workspace_id}"
        if record.runtime_name != expected_sandbox:
            return False
        if not cls._runtime_identity_matches(record, observed):
            return False
        if observed.get("sandbox_name") != expected_sandbox:
            return False
        if observed.get("volume_name") != expected_volume:
            return False
        if record.volume_name is not None and record.volume_name != expected_volume:
            return False
        return observed.get("runtime_id") is None and observed.get("volume_id") is None

    def _potential_growth(self, quota_bytes: int, measured_apparent_bytes: int | None = None) -> int:
        """Reserve independent /workspace and managed-root growth ceilings.

        Runtime inspection currently measures the persistent /workspace volume;
        the managed root's package/cache/log allocation is not separately
        reported.  Subtract only measured /workspace bytes and conservatively
        reserve the full remaining 4 GiB root ceiling until that evidence is
        available.  This avoids reviving the old shared-total 4 GiB contract.
        """
        workspace_remaining = max(0, quota_bytes - (measured_apparent_bytes or 0))
        return workspace_remaining + self.root_quota_bytes

    @staticmethod
    def _current_apparent_bytes(record: WorkspaceRecord) -> int | None:
        """Use stored bytes only while their existing status says current."""
        return record.measured_apparent_bytes if record.usage_status == "fresh" else None

    def _active_potential_growth(self, *, exclude_workspace_id: str | None = None) -> int:
        """Return remaining growth for every active guest on this filesystem."""
        return sum(
            self._potential_growth(row.quota_bytes, self._current_apparent_bytes(row))
            for row in self.metadata.list()
            if row.state in {"creating", "starting", "running"}
            and row.workspace_id != exclude_workspace_id
        )

    def _ensure(self, principal_id: str, connector_id: str | None, label: str | None = None, *, explicit_retry: bool = False) -> WorkspaceRecord:
        # 13.0 §7.3: the built-in test principal's ID is a deterministic
        # UUIDv5 derived from the connector, so a Workspace left behind by a
        # killed run is found and reused by the next one. Every OTHER caller
        # still has to present a v4 credential ID: the exception is recomputed
        # from the connector here rather than being a general "v5 is fine".
        if connector_id and str(principal_id) == self_test_principal_id(str(connector_id)):
            log.debug(
                "admitting the built-in test principal's Workspace connector_id=%s",
                connector_id,
            )
            principal_id = str(principal_id)
        else:
            principal_id = _uuid(principal_id, "principal_id")
        if self.credential_is_active is not None:
            try:
                active = self.credential_is_active(principal_id)
            except Exception as exc:
                raise WorkspaceError("runtime_unavailable", "Credential policy is unavailable") from exc
            if active is False:
                raise WorkspaceError("credential_inactive", "Credential is no longer admitted")
        record = self.metadata.get_by_principal(principal_id)
        created_now = record is None
        was_failed = bool(record is not None and record.state == "failed")
        # A failed runtime may be repaired by the next ordinary caller only
        # after a broker inspection proves the exact owned sandbox or the
        # exact owned volume-only residual.  The adapter then performs
        # sandbox-only replacement around that verified volume.  Unknown,
        # absent, foreign, and mismatched objects remain Admin-repair cases.
        if was_failed and not self._ownership_proven(record):
            raise WorkspaceError("retry_requires_ownership", "Workspace identity could not be proven safely")
        reservation_id: str | None = None
        if record is None:
            running = [row for row in self.metadata.list() if row.state in {"creating", "starting", "running"}]
            if len(running) >= self.max_running_workspaces:
                raise WorkspaceError("capacity_busy", "Workspace running capacity is full")
            growth = self._potential_growth(self.quota_bytes)
            reservation_id = self._reserve_admission(
                growth,
                request_key=f"admit:{principal_id}",
                active_growth_supplier=self._active_potential_growth,
            )
            try:
                record = self.metadata.create(principal_id, connector_id, label or "Workspace", now=self.clock().isoformat(timespec="seconds"), quota_bytes=self.quota_bytes, retention_days=self.retention_days)
            except Exception:
                if reservation_id:
                    self.metadata.release_growth(reservation_id)
                raise
        if record.state == "deleting" or record.desired_state == "absent":
            raise WorkspaceError("workspace_deleting", "Workspace is being deleted")
        recovering_running = False
        if record.state == "running" and self.runtime is not None:
            # 13.2.6 (DESIGN-13.2 §6): every deploy restarts the broker and the
            # broker's generation moves (prod: 7 → 8 → 9 across two deploys),
            # so the FIRST call for every workspace that existed before the
            # deploy is rejected with generation_conflict. _runtime_call clears
            # the stale expectation on that rejection; the job and fs paths
            # already retry once against the current generation, and admission
            # did not — so the first bridge call after a deploy failed, as an
            # internal_error, for every existing workspace. Same one-retry rule.
            observed, record = self._inspect_reconciled(record)
            runtime_state = observed.get("state")
            if runtime_state == "running":
                record = self._apply_runtime_measurement(record, observed)
            elif runtime_state in {"stopped", "failed", "partial", "absent"}:
                if not self._running_recovery_proven(record, observed):
                    raise WorkspaceError("retry_requires_ownership", "Workspace identity could not be proven safely")
                recovering_running = True
        if record.state in {"stopped", "failed"}:
            running = [row for row in self.metadata.list() if row.state in {"creating", "starting", "running"} and row.workspace_id != record.workspace_id]
            if len(running) >= self.max_running_workspaces:
                raise WorkspaceError("capacity_busy", "Workspace running capacity is full")
            growth = self._potential_growth(record.quota_bytes, self._current_apparent_bytes(record))
            reservation_id = self._reserve_admission(
                growth,
                request_key=f"start:{record.workspace_id}:{record.revision}",
                active_growth_supplier=self._active_potential_growth,
            )
            self.metadata.update(record.workspace_id, state="starting", desired_state="running")
            record = self.metadata.get(record.workspace_id) or record
        elif recovering_running:
            running = [row for row in self.metadata.list() if row.state in {"creating", "starting", "running"} and row.workspace_id != record.workspace_id]
            if len(running) >= self.max_running_workspaces:
                raise WorkspaceError("capacity_busy", "Workspace running capacity is full")
            growth = self._potential_growth(record.quota_bytes, self._current_apparent_bytes(record))
            reservation_id = self._reserve_admission(
                growth,
                request_key=f"recover:{record.workspace_id}:{record.revision}",
                active_growth_supplier=lambda: self._active_potential_growth(exclude_workspace_id=record.workspace_id),
            )
            self.metadata.update(record.workspace_id, state="starting", desired_state="running")
            record = self.metadata.get(record.workspace_id) or record
        try:
            if record.state in {"creating", "starting"}:
                observed = self._runtime_call(record, "ensure", {
                    "create_volume_if_absent": created_now and not recovering_running,
                    "quota_bytes": record.quota_bytes,
                    "root_quota_bytes": self.root_quota_bytes,
                    "vcpus": 4,
                    "memory_bytes": 8 * 1024**3,
                    "require_writable_root_quota": True,
                    "network": self.network_policy,
                })
                record = self.metadata.update(record.workspace_id, state="running", desired_state="running", stopped_at=None, last_error_code=None, last_error_at=None)
                record = self._apply_runtime_measurement(record, observed)
        except WorkspaceError as exc:
            self.metadata.update(record.workspace_id, state="failed", desired_state="running", last_error_code=exc.reason, last_error_at=self.clock().isoformat(timespec="seconds"))
            raise
        finally:
            if reservation_id:
                self.metadata.release_growth(reservation_id)
        return record

    def _admit(self, principal: Any, connector_id: str | None = None, *, explicit_retry: bool = False) -> WorkspaceRecord:
        if isinstance(principal, str):
            with self._principal_queue(principal):
                return self._ensure(principal, connector_id, explicit_retry=explicit_retry)
        pid = getattr(principal, "principal_id", None) if principal is not None else None
        if not pid and isinstance(principal, dict):
            pid = principal.get("principal_id")
        if not pid:
            raise WorkspaceError("runtime_unavailable", "Workspace requires a durable authenticated principal")
        with self._principal_queue(str(pid)):
            return self._ensure(str(pid), connector_id or getattr(principal, "surface_id", None), explicit_retry=explicit_retry)

    def info(self, principal: Any, *, connector_id: str | None = None) -> dict[str, Any]:
        pid = getattr(principal, "principal_id", None) if principal is not None else None
        if not pid and isinstance(principal, dict):
            pid = principal.get("principal_id")
        if not pid:
            raise WorkspaceError("runtime_unavailable", "Workspace requires a durable authenticated principal")
        record = self.metadata.get_by_principal(str(pid))
        if record is None:
            return {"status": "success", "workspace": None}
        runtime = {"runtime_available": False,
                   "operational_state": "degraded" if record.state == "running" else record.state}
        observed: dict[str, Any] | None = None
        if self.runtime is not None and record.state == "running":
            try:
                # 13.2.6: same one-retry rule as admission, or workspace_info
                # reported "degraded" once after every deploy.
                observed, record = self._inspect_reconciled(record)
                runtime.update(observed)
                runtime["runtime_state"] = runtime.pop("state", None)
                runtime["runtime_available"] = runtime["runtime_state"] == "running"
                runtime["operational_state"] = "running" if runtime["runtime_available"] else "degraded"
            except WorkspaceError as exc:
                runtime = {"runtime_available": False, "operational_state": "degraded",
                           "runtime_error_reason": exc.reason}
        # A4 (DESIGN-12.18 §3.4): usage_by_directory is its own bounded call
        # (broker fs_usage), independent of whether the inspect above
        # succeeded -- a failure here never fails workspace_info, it just
        # omits the key and reports why.
        #
        # Reuse this fs_usage call for directory details and the same
        # measure-and-persist path as refresh_usage(), keeping workspace_info
        # consistent with mutation receipts and the stored record.
        usage_extra: dict[str, Any] = {}
        if self.runtime is not None and observed is not None and observed.get("state") == "running":
            usage_started = time.monotonic()
            try:
                usage_result = self._runtime_call(record, "fs_usage", {"path": "/workspace"})
                usage_extra["usage_by_directory"] = usage_result.get("entries")
                total = self._note_fs_usage_evidence(record.workspace_id, observed, usage_result)
                # preserve_revision=True: workspace_info is a read and must
                # never advance the row's optimistic-concurrency revision, the
                # same reason _fresh_delete_measurement and
                # _measure_after_mutation both pass it.
                record = self._apply_runtime_measurement(record, observed, preserve_revision=True)
                log.info(
                    "Workspace fs_usage completed workspace_id=%s path=/workspace elapsed_ms=%d entry_count=%d truncated=%s total_bytes=%s",
                    record.workspace_id, int((time.monotonic() - usage_started) * 1000),
                    len(usage_result.get("entries") or []), usage_result.get("truncated"), total,
                )
            except WorkspaceError as exc:
                self._note_fs_usage_evidence(record.workspace_id, observed, None)
                record = self._apply_runtime_measurement(record, observed, preserve_revision=True)
                usage_extra["usage_by_directory_error"] = exc.reason
                log.info(
                    "Workspace fs_usage failed workspace_id=%s path=/workspace elapsed_ms=%d reason=%s",
                    record.workspace_id, int((time.monotonic() - usage_started) * 1000), exc.reason,
                )
        # The stored record (just refreshed above, if the runtime is up) is
        # the truth for every measurement field -- exclude those keys from
        # the raw broker `runtime` dict before merging so the response can
        # never again show a receipt-disagreeing pair of values.
        runtime = {key: value for key, value in runtime.items() if key not in _INFO_MEASUREMENT_KEYS}
        return {"status": "success", "workspace": {
            **record.as_dict(), **runtime, "network_mode": runtime.get("network_mode", "off"),
            **self._quota_fields(record), **usage_extra,
        }}

    def execute(self, principal: Any, tool: str, arguments: dict[str, Any], *, connector_id: str | None = None) -> dict[str, Any]:
        if not isinstance(arguments, dict):
            raise WorkspaceError("invalid_arguments", "arguments must be an object")
        if tool == "workspace_info":
            if set(arguments) - {"idempotency_key"}:
                raise WorkspaceError("invalid_arguments", "workspace_info accepts no arguments")
            return self.info(principal, connector_id=connector_id)
        record = self._admit(principal, connector_id)
        # A1 (DESIGN-12.18 §3.1): wait_ms is validated and popped here, before
        # the per-Workspace lock, on the two tools that support it.  It is
        # deliberately never added to ``clean_arguments`` for those two tools,
        # so it takes no part in the idempotency digest and never reaches
        # ``_job_start``/``_job_get``'s own "unknown argument" checks -- a
        # ``wait_ms`` sent to any OTHER tool is left in place and rejected
        # there as an unknown argument, per the frozen-surface rule that an
        # argument this manager does not understand is never silently dropped.
        wait_ms = 0
        if tool in _WAIT_CAPABLE_TOOLS and "wait_ms" in arguments:
            raw_wait = arguments["wait_ms"]
            if isinstance(raw_wait, bool) or not isinstance(raw_wait, int) or not 0 <= raw_wait <= MAX_JOB_WAIT_MS:
                raise WorkspaceError("invalid_arguments", "wait_ms must be an integer between 0 and 55000")
            wait_ms = raw_wait
        # A2 (DESIGN-12.18 §3.2): output_encoding/strip_ansi change the shape
        # of workspace_start_job's own immediate receipt only when it goes on
        # to wait for output; without wait_ms the receipt never carries a
        # stream to encode, so reject rather than silently ignore.
        if tool == "workspace_start_job" and wait_ms == 0 and ({"output_encoding", "strip_ansi"} & set(arguments)):
            raise WorkspaceError("invalid_arguments", "output_encoding and strip_ansi require wait_ms on workspace_start_job")
        with self._queue(record.workspace_id):
            operation_key = arguments.get("idempotency_key")
            strip_keys = {"idempotency_key"}
            if tool in _WAIT_CAPABLE_TOOLS:
                strip_keys.add("wait_ms")
            clean_arguments = {key: value for key, value in arguments.items() if key not in strip_keys}
            operation_digest = _digest({"tool": tool, "arguments": clean_arguments})
            broker_request_id = None
            if operation_key is not None:
                operation_key = _bounded_string(operation_key, "idempotency_key", 128)
                replay = self.metadata.idempotent(record.workspace_id, operation_key, operation_digest)
                if replay is not None:
                    # The replay always returns the receipt saved below, never
                    # a waited result (§3.1: wait responses are never written
                    # to the idempotency store), so a replayed start_job call
                    # never waits even if it carries wait_ms again.
                    return {**replay, "idempotent_replay": True, "replayed": True}
                broker_request_id = _stable_broker_request_id(
                    record.workspace_id, operation_key, operation_digest,
                )
            if tool.startswith("workspace_get_job"):
                response = self._job_get(record, clean_arguments)
            elif tool == "workspace_cancel_job":
                response = self._job_cancel(record, clean_arguments, request_id=broker_request_id)
            elif tool == "workspace_start_job":
                response = self._job_start(record, clean_arguments, request_id=broker_request_id)
                # A4 (DESIGN-12.18 SS3.4): job_start counts as a mutation for
                # usage-visibility purposes -- the job it launches typically
                # writes bytes. Measure, then rebuild the summary from the
                # (possibly just-refreshed) row so this receipt's
                # quota_remaining_bytes/quota_warning reflect it.
                self._measure_after_mutation(record.workspace_id)
                response = {**response, "workspace": self._summary(self.metadata.get(record.workspace_id) or record)}
            elif tool == "workspace_web_search":
                unknown = set(clean_arguments) - {"query", "result_count"}
                if unknown:
                    raise WorkspaceError("invalid_arguments", "unknown web-search argument")
                query = _bounded_string(clean_arguments.get("query"), "query", 4096)
                count = clean_arguments.get("result_count", 5)
                if not query.strip() or isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= 20:
                    raise WorkspaceError("invalid_arguments", "web-search arguments are invalid")
                if self.brave_search is None:
                    raise WorkspaceError("network_denied", "web search is not configured")
                result = self.brave_search.search(query, count=count)
                if result.get("status") != "success":
                    raise WorkspaceError("network_denied", "web search is unavailable", category=result.get("reason"))
                now = self.clock().isoformat(timespec="seconds")
                self.metadata.update(record.workspace_id, last_accessed_at=now, deletion_due_at=(self.clock() + timedelta(days=record.retention_days or self.retention_days)).isoformat(timespec="seconds"))
                response = {"status": "success", "workspace": self._summary(record), "data": result}
            else:
                operation, fs_args = self._filesystem_args(tool, clean_arguments)
                lease = self.metadata.lease(record.workspace_id, "operation")
                try:
                    is_mutation = operation in {"fs_write", "fs_edit", "fs_mkdir", "fs_copy", "fs_move", "fs_remove"}
                    if is_mutation:
                        active = self._active_job_after_reconcile(record)
                        if active is not None:
                            raise self._job_running_error(record, active)
                    runtime_call_started = time.monotonic()
                    result = self._runtime_call(record, operation, fs_args, request_id=broker_request_id)
                    # A3 (DESIGN-12.18 SS3.3): fs_lines always returns base64
                    # content_b64 with no "binary" argument of its own --
                    # decode it here into the content/encoding shape fs_read
                    # already returns. A byte-mode read (fs_read) gains
                    # total_bytes/has_more via one extra fs_stat call; a
                    # line-mode read already carries both from the broker.
                    if operation == "fs_lines":
                        result = self._decode_lines_result(result, clean_arguments.get("encoding", "text"))
                        log.info(
                            "Workspace fs_lines completed workspace_id=%s path=%s elapsed_ms=%d bytes=%d has_more=%s",
                            record.workspace_id, fs_args["path"],
                            int((time.monotonic() - runtime_call_started) * 1000),
                            result.get("bytes", 0), result.get("has_more"),
                        )
                    elif operation == "fs_read":
                        stat_result = self._runtime_call(record, "fs_stat", {"path": fs_args["path"]})
                        total_bytes = int(stat_result.get("size", 0) or 0)
                        result = {
                            **result, "total_bytes": total_bytes,
                            "has_more": fs_args.get("offset", 0) + int(result.get("bytes", 0) or 0) < total_bytes,
                        }
                    self.metadata.update(record.workspace_id, last_accessed_at=self.clock().isoformat(timespec="seconds"), deletion_due_at=(self.clock() + timedelta(days=record.retention_days or self.retention_days)).isoformat(timespec="seconds"))
                    # A4 (DESIGN-12.18 SS3.4): reads never measure -- only the
                    # six mutating filesystem operations above do.
                    if is_mutation:
                        self._measure_after_mutation(record.workspace_id)
                    response = {"status": "success", "workspace": self._summary(self.metadata.get(record.workspace_id) or record), "data": result}
                finally:
                    self.metadata.release_lease(lease)
            # get_job responses are not saved at all when wait_ms > 0 (§3.1);
            # start_job's own receipt is still saved so a replay returns it.
            if operation_key is not None and not (tool == "workspace_get_job" and wait_ms > 0):
                self.metadata.save_idempotent(record.workspace_id, operation_key, operation_digest, response)
        # _wait_for_job runs outside the per-Workspace lock (§3.1) so a plain
        # read on the same Workspace from another caller never queues behind
        # a long wait.
        if wait_ms == 0 or tool not in _WAIT_CAPABLE_TOOLS:
            return response
        job_id = str((response.get("job") or {}).get("job_id"))
        if tool == "workspace_get_job":
            output_args = {
                "job_id": job_id,
                "stdout_offset": clean_arguments.get("stdout_offset", 0),
                "stderr_offset": clean_arguments.get("stderr_offset", 0),
                "max_bytes": clean_arguments.get("max_bytes", MAX_FILE_BYTES),
            }
            # A3 (DESIGN-12.18 §3.3): tail_lines was already bounds-checked by
            # the _job_get call that built ``response`` above; carry it into
            # every poll too so a caller combining tail_lines with wait_ms is
            # never silently downgraded to offset-based polling mid-wait.
            if "tail_lines" in clean_arguments:
                output_args["tail_lines"] = clean_arguments["tail_lines"]
        else:
            output_args = {"job_id": job_id, "stdout_offset": 0, "stderr_offset": 0, "max_bytes": MAX_FILE_BYTES}
        apply_encoding = bool({"output_encoding", "strip_ansi"} & set(clean_arguments))
        encoding = clean_arguments.get("output_encoding", "base64")
        strip_ansi_flag = bool(clean_arguments.get("strip_ansi", False))
        return self._wait_for_job(
            record, job_id, output_args, wait_ms, response,
            encoding=encoding, strip_ansi=strip_ansi_flag, apply_encoding=apply_encoding,
        )

    def _wait_for_job(
        self, record: WorkspaceRecord, job_id: str, output_args: dict[str, Any],
        wait_ms: int, response: dict[str, Any], *, encoding: str, strip_ansi: bool, apply_encoding: bool,
    ) -> dict[str, Any]:
        """A1 (DESIGN-12.18 §3.1): poll job_get outside the per-Workspace lock.

        Runs after ``execute()`` has released ``self._queue(...)``, so a
        concurrent plain read (or another caller's mutation) on the same
        Workspace is never queued behind a long wait.  Polls at a fixed
        ``JOB_WAIT_POLL_SECONDS`` cadence through the injected clock/sleep so
        tests never depend on a real timer.  The final terminal-state save
        takes the lock only briefly, for ordering with a concurrent
        ``_active_job_after_reconcile`` -- the metadata store is already
        thread-safe on its own.
        """
        started = self.clock()
        deadline = started + timedelta(milliseconds=wait_ms)
        job = dict(response.get("job") or {})
        while True:
            if job.get("state") in TERMINAL_JOB_STATES:
                waited_ms = int((self.clock() - started).total_seconds() * 1000)
                log.info("Workspace job wait ended workspace_id=%s job_id=%s waited_ms=%d wake_reason=exited",
                         record.workspace_id, job_id, waited_ms)
                return {**response, "job": job, "waited_ms": waited_ms, "wake_reason": "exited"}
            now = self.clock()
            if now >= deadline:
                waited_ms = int((now - started).total_seconds() * 1000)
                log.info("Workspace job wait ended workspace_id=%s job_id=%s waited_ms=%d wake_reason=timeout",
                         record.workspace_id, job_id, waited_ms)
                return {**response, "job": job, "waited_ms": waited_ms, "wake_reason": "timeout"}
            self.sleep(min(JOB_WAIT_POLL_SECONDS, (deadline - now).total_seconds()))
            try:
                job = self._runtime_call(record, "job_get", output_args)
            except WorkspaceError as exc:
                if exc.reason == "generation_conflict":
                    # The broker restarted under the wait and advanced its
                    # runtime generation -- a transient event, not a reason
                    # to fail a call that could simply keep waiting.
                    # _runtime_call already reset the stale expectation on
                    # this reason; re-read the record and retry the SAME
                    # poll once, immediately (no extra sleep), against the
                    # broker's current generation. Same one-retry pattern as
                    # _active_job_after_reconcile: never loop past one retry.
                    record = self.metadata.get(record.workspace_id) or record
                    try:
                        job = self._runtime_call(record, "job_get", output_args)
                    except WorkspaceError as retry_exc:
                        exc = retry_exc
                    else:
                        exc = None
                if exc is not None:
                    if exc.reason in _WAIT_RECHECK_REASONS:
                        current = self.metadata.get(record.workspace_id)
                        if current is None or current.state != "running":
                            waited_ms = int((self.clock() - started).total_seconds() * 1000)
                            log.info("Workspace job wait ended workspace_id=%s job_id=%s waited_ms=%d wake_reason=gone",
                                     record.workspace_id, job_id, waited_ms)
                            return {
                                "status": "success", "workspace": self._summary(current or record),
                                "job": {"job_id": job_id, "state": "lost"},
                                "waited_ms": waited_ms, "wake_reason": "gone",
                            }
                    # Any other reason (or a still-running, still-present
                    # Workspace hitting a transient recheck reason, including
                    # a persistent generation_conflict that survived the
                    # retry above) surfaces exactly as it would from a plain,
                    # non-waiting job_get call.
                    raise exc
            job["job_id"] = job_id
            if apply_encoding:
                self._apply_stream_encoding(job, encoding, strip_ansi)
            # A3 (DESIGN-12.18 §3.3): a polled job_get result gets the same
            # has_more_stdout/has_more_stderr aliasing a non-waiting
            # workspace_get_job call gets from _job_get -- this loop talks to
            # the broker directly and would otherwise skip it.
            self._alias_job_has_more(job)
            if job.get("state") in TERMINAL_JOB_STATES:
                with self._queue(record.workspace_id):
                    self._cache_observed_job_state(record.workspace_id, job_id, job["state"])

    @staticmethod
    def _alias_job_has_more(job: dict[str, Any]) -> None:
        """A3 (DESIGN-12.18 SS3.3): expose the broker's stdout_has_more/
        stderr_has_more under the requirement's has_more_stdout/
        has_more_stderr names too (both keys, same value), on every job_get
        result -- waited or not.
        """
        for stream in ("stdout", "stderr"):
            broker_key = f"{stream}_has_more"
            if broker_key in job:
                job[f"has_more_{stream}"] = job[broker_key]

    def _apply_stream_encoding(self, job: dict[str, Any], encoding: str, strip_ansi: bool) -> None:
        """A2 (DESIGN-12.18 §3.2): recode a job_get result's stdout/stderr in place.

        The broker always returns each stream as base64.  ``base64`` (the
        default) leaves the bytes untouched -- a slice boundary can split a
        multi-byte character, which is exactly what the ``auto`` fallback and
        ``_lossy`` report; this never tries to realign offsets.
        """
        for stream in ("stdout", "stderr"):
            raw = job.get(stream)
            if not isinstance(raw, str):
                continue
            try:
                # binascii.Error (a ValueError) for malformed base64, TypeError
                # for a non-bytes-like argument -- not expected from the
                # broker, which is why this only logs and leaves the stream
                # untouched rather than failing the whole call.
                raw_bytes = base64.b64decode(raw, validate=True)
            except (ValueError, TypeError):
                log.warning("Workspace job stream decode failed stream=%s", stream)
                continue
            if encoding == "base64":
                job[f"{stream}_encoding"] = "base64"
                continue
            try:
                text = raw_bytes.decode("utf-8")
                lossy = False
            except UnicodeDecodeError:
                if encoding != "text":
                    # auto: the slice cannot be decoded cleanly, so it stays
                    # base64 and reports that instead of guessing.
                    job[f"{stream}_encoding"] = "base64"
                    continue
                text = raw_bytes.decode("utf-8", errors="replace")
                lossy = True
            if strip_ansi:
                text = _ANSI_ESCAPE_RE.sub("", text)
            job[stream] = text
            job[f"{stream}_encoding"] = "text"
            if encoding == "text":
                job[f"{stream}_lossy"] = lossy

    @staticmethod
    def _decode_lines_result(result: dict[str, Any], encoding: str) -> dict[str, Any]:
        """A3 (DESIGN-12.18 SS3.3): turn ``fs_lines``' always-base64
        ``content_b64`` into the ``content``/``encoding`` shape ``fs_read``
        already returns, per the caller's requested encoding.  ``fs_lines``
        has no ``binary`` argument of its own (unlike ``fs_read``), so this
        decode step happens here at the manager instead of at the broker.
        """
        if encoding == "base64":
            content = result["content_b64"]
        else:
            content = base64.b64decode(result["content_b64"]).decode("utf-8", "replace")
        return {
            "path": result["path"], "content": content, "encoding": encoding,
            "bytes": result["bytes"], "start_line": result["start_line"], "end_line": result["end_line"],
            "total_lines": result["total_lines"], "total_bytes": result["total_bytes"],
            "has_more": result["has_more"],
        }

    def _quota_fields(self, record: WorkspaceRecord) -> dict[str, Any]:
        """A4 (DESIGN-12.18 SS3.4): quota_remaining_bytes/quota_warning ride
        on every response, including ``workspace_info`` (which does not go
        through ``_summary`` -- it builds its own dict from
        ``record.as_dict()``). An unknown measurement may retain historical
        numeric bytes, but only a fresh sample can produce a quota result.
        """
        apparent = self._current_apparent_bytes(record)
        quota_remaining_bytes = None if apparent is None else max(0, record.quota_bytes - apparent)
        quota_warning = False
        if apparent is not None:
            warning_threshold_percent = self.metadata.settings()["warning_threshold_percent"]
            quota_warning = record.quota_bytes > 0 and apparent * 100 >= record.quota_bytes * warning_threshold_percent
        return {"quota_remaining_bytes": quota_remaining_bytes, "quota_warning": quota_warning}

    def _summary(self, record: WorkspaceRecord) -> dict[str, Any]:
        return {
            "workspace_id": record.workspace_id, "state": record.state,
            "desired_state": record.desired_state, "quota_bytes": record.quota_bytes,
            "measured_allocated_bytes": record.measured_allocated_bytes,
            "measured_apparent_bytes": record.measured_apparent_bytes,
            "usage_status": record.usage_status, "measured_at": record.measured_at,
            "owner_status": record.owner_status, "path_status": record.path_status,
            **self._quota_fields(record),
        }

    def start(self, principal: Any, *, connector_id: str | None = None) -> dict[str, Any]:
        # The application metadata can survive a Cognita restart while the
        # runtime sandbox crashes independently.  Do one bounded runtime
        # inspection for a row that still says ``running``; otherwise this
        # Admin start operation would return success without starting
        # anything.  Restart the owned sandbox in place so its named volume
        # and /workspace contents remain attached.  The adapter validates the
        # persisted configuration and ownership before performing the start.
        pid = getattr(principal, "principal_id", principal)
        record = self.metadata.get_by_principal(str(pid))
        if record is not None and record.state == "running" and self.runtime is not None:
            with self._principal_queue(str(pid)):
                current = self.metadata.get_by_principal(str(pid))
                if current is not None and current.state == "running":
                    try:
                        for attempt in range(2):
                            try:
                                observed = self._runtime_call(current, "inspect", {})
                                break
                            except WorkspaceError as exc:
                                if exc.reason == "generation_conflict" and attempt == 0:
                                    # The broker generation advances on its own
                                    # restart.  _runtime_call clears the stale
                                    # expectation; reread the row and retry once
                                    # against the broker's advertised generation.
                                    current = self.metadata.get(current.workspace_id) or current
                                    continue
                                raise
                        runtime_state = observed.get("state")
                        if runtime_state == "running":
                            current = self._apply_runtime_measurement(current, observed)
                            return {"status": "success", "workspace": self._summary(current)}
                        if runtime_state not in {"stopped", "failed"}:
                            raise WorkspaceError(
                                "runtime_unavailable",
                                "Workspace runtime state cannot be restarted safely",
                            )
                        for attempt in range(2):
                            try:
                                restarted = self._runtime_call(current, "start", {})
                                break
                            except WorkspaceError as exc:
                                if exc.reason == "generation_conflict" and attempt == 0:
                                    current = self.metadata.get(current.workspace_id) or current
                                    continue
                                raise
                        if restarted.get("state") != "running":
                            raise WorkspaceError(
                                "runtime_unavailable",
                                "Workspace runtime did not reach running state",
                            )
                        # A4 (DESIGN-12.18 SS3.4): this branch is the actual
                        # recovery -- the runtime was stopped/failed and this
                        # call brought it back running in place. The earlier
                        # "already running" return above takes no action and
                        # records nothing.
                        recovered_at = self.clock().isoformat(timespec="seconds")
                        current = self.metadata.update(
                            current.workspace_id,
                            state="running", desired_state="running",
                            stopped_at=None, last_error_code=None, last_error_at=None,
                            last_auto_action="vm_recovered", last_auto_action_at=recovered_at,
                        )
                        current = self._apply_runtime_measurement(current, restarted)
                        log.info(
                            "Workspace runtime recovered through Admin start",
                            extra={"event": "workspace_runtime_recovered"},
                        )
                        return {"status": "success", "workspace": self._summary(current)}
                    except WorkspaceError as exc:
                        self.metadata.update(
                            current.workspace_id,
                            state="failed", desired_state="running",
                            last_error_code=exc.reason,
                            last_error_at=self.clock().isoformat(timespec="seconds"),
                        )
                        raise
        record = self._admit(principal, connector_id, explicit_retry=True)
        return {"status": "success", "workspace": self._summary(record)}

    def stop(self, principal: Any, *, connector_id: str | None = None, emergency: bool = False) -> dict[str, Any]:
        record = self.metadata.get_by_principal(str(getattr(principal, "principal_id", principal)))
        if record is None:
            return {"status": "success", "workspace": None}
        with self._queue(record.workspace_id):
            if not emergency:
                if self.metadata.has_live_lease(record.workspace_id):
                    raise WorkspaceError("capacity_busy", "Workspace has an active operation")
                active = self._active_job_after_reconcile(record)
                if active is not None:
                    raise self._job_running_error(record, active)
            if record.state == "running":
                self._runtime_call(record, "stop", {"force": emergency})
            # A4 (DESIGN-12.18 SS3.4): this is always an Admin-initiated stop
            # (no ordinary Workspace tool calls this method), so it always
            # records an auto-action; which one depends only on emergency.
            now = self.clock().isoformat(timespec="seconds")
            action = "emergency_stop" if emergency else "admin_stop"
            updated = self.metadata.update(record.workspace_id, state="stopped", desired_state="stopped", stopped_at=now, last_auto_action=action, last_auto_action_at=now)
            log.info("Workspace stopped workspace_id=%s emergency=%s action=%s", record.workspace_id, emergency, action)
            return {"status": "success", "workspace": self._summary(updated)}

    def set_pinned(self, principal: Any, pinned: bool, *, connector_id: str | None = None) -> dict[str, Any]:
        if not isinstance(pinned, bool):
            raise WorkspaceError("invalid_arguments", "pinned must be boolean")
        record = self.metadata.get_by_principal(str(getattr(principal, "principal_id", principal)))
        if record is None:
            raise WorkspaceError("path_unavailable", "Workspace was not found")
        with self._queue(record.workspace_id):
            due = None if pinned else (self.clock() + timedelta(days=record.retention_days or self.retention_days)).isoformat(timespec="seconds")
            updated = self.metadata.update(record.workspace_id, pinned=int(pinned), deletion_due_at=due)
            return {"status": "success", "workspace": updated.as_dict()}

    def validate_credential_workspace(
        self, *, credential_id: str, workspace_id: str | None,
        workspace_revision: int | None,
    ) -> dict[str, Any]:
        """Revalidate the Workspace target bound by credential deletion.

        The Admin confirmation reads this identity and revision before the
        destructive request.  An explicit null/null pair means the credential
        had no lazily-created Workspace; any appearance, replacement, or
        metadata mutation therefore fails closed before the credential tombstone
        is committed.
        """
        if workspace_id is None:
            if workspace_revision is not None:
                raise WorkspaceError("path_conflict", "Workspace binding is invalid")
            if self.metadata.get_by_principal(str(credential_id)) is not None:
                raise WorkspaceError("path_conflict", "Workspace target appeared")
            return {"workspace_id": None, "workspace_revision": None}
        if not isinstance(workspace_revision, int) or isinstance(workspace_revision, bool) or workspace_revision < 0:
            raise WorkspaceError("path_conflict", "Workspace binding revision is invalid")
        record = self.metadata.get_by_principal(str(credential_id))
        if record is None or record.workspace_id != workspace_id or record.revision != workspace_revision:
            raise WorkspaceError("path_conflict", "Workspace target revision changed")
        return {"workspace_id": record.workspace_id, "workspace_revision": record.revision}

    def reconcile_credential(
        self, *, credential_id: str, owner_status: str,
        retention: str = "normal", deleted_at: str | None = None,
    ) -> dict[str, Any]:
        """Apply durable credential revoke/delete intent to its Workspace row."""
        if owner_status not in {"revoked", "tombstoned"} or retention not in {"keep", "delete_now", "normal"}:
            raise WorkspaceError("invalid_arguments", "invalid credential lifecycle state")
        requested_at = deleted_at if isinstance(deleted_at, str) else self.clock().isoformat(timespec="seconds")
        try:
            # Parsed for validation only: an unparseable caller-supplied time
            # falls back to now.  (Superseded: the parsed value was also bound
            # to `requested_time`, which nothing below ever read.)
            _parse_time(requested_at)
        except (TypeError, ValueError):
            requested_at = self.clock().isoformat(timespec="seconds")
        record = self.metadata.get_by_principal(str(credential_id))
        if record is None:
            return {"complete": True, "workspace_deleted": True}
        if owner_status == "revoked":
            updated = self.metadata.set_owner_status(record.workspace_id, "revoked")
            return {"complete": True, "workspace_deleted": False, "workspace_id": updated.workspace_id}
        if retention == "keep":
            updated = self.metadata.update(
                record.workspace_id, owner_status="tombstoned", pinned=1,
                deletion_intent="keep", deletion_requested_at=requested_at,
                deletion_due_at=None,
            )
            return {"complete": True, "workspace_deleted": False, "workspace_id": updated.workspace_id}
        if retention == "normal":
            # Replay must not turn a fixed 30-day retention window into a
            # sliding one. Prefer the durable credential deletion time; keep
            # an already-recorded due date when older callers lack it.
            if deleted_at is not None:
                try:
                    deletion_time = _parse_time(deleted_at)
                except (TypeError, ValueError) as exc:
                    raise WorkspaceError("invalid_arguments", "credential deletion time is invalid") from exc
                due = (deletion_time + timedelta(days=DEFAULT_RETENTION_DAYS)).isoformat(timespec="seconds")
                requested_at = deletion_time.isoformat(timespec="seconds")
            elif record.deletion_intent == "normal" and record.deletion_due_at:
                due = record.deletion_due_at
                requested_at = record.deletion_requested_at or self.clock().isoformat(timespec="seconds")
            else:
                requested_at = record.deletion_requested_at or self.clock().isoformat(timespec="seconds")
                due = (_parse_time(requested_at) + timedelta(days=DEFAULT_RETENTION_DAYS)).isoformat(timespec="seconds")
            if (record.owner_status == "tombstoned"
                    and record.deletion_intent == "normal"
                    and record.deletion_requested_at == requested_at
                    and (record.deletion_due_at == due or (record.pinned and record.deletion_due_at is None))):
                # A later operator Pin is authoritative after the credential
                # choice has been reconciled; a replay must not clear it.
                return {"complete": False, "workspace_deleted": False, "workspace_id": record.workspace_id}
            updated = self.metadata.update(
                record.workspace_id, owner_status="tombstoned", pinned=0, deletion_intent="normal",
                deletion_requested_at=requested_at, deletion_due_at=due,
            )
            # Normal retention is fulfilled by the durable deadline/scavenger,
            # not by silently deleting data while processing credential delete.
            return {"complete": False, "workspace_deleted": False, "workspace_id": updated.workspace_id}
        updated = self.metadata.update(
            record.workspace_id, owner_status="tombstoned", deletion_intent="delete_now",
            deletion_requested_at=requested_at, desired_state="absent",
        )
        try:
            self.remove(
                self._principal_for_record(updated), connector_id=updated.connector_id,
                expected_revision=updated.revision,
                idempotency_key=f"credential-delete:{credential_id}:{updated.revision}",
                # The user explicitly selected delete-now.  An inspect result
                # proving the runtime object absent is therefore a safe,
                # metadata-only repair; an unavailable/ambiguous broker still
                # fails closed in ``_delete_target_state``.
                allow_absent_cleanup=True,
            )
            return {"complete": True, "workspace_deleted": True, "workspace_id": updated.workspace_id}
        except WorkspaceError as exc:
            # Leases, jobs, stale ownership, and broker outages remain durable
            # pending state for the next reconciliation pass.
            current = self.metadata.get(updated.workspace_id)
            if current is not None:
                self.metadata.update(
                    current.workspace_id, owner_status="tombstoned", desired_state="absent",
                    deletion_intent="delete_now", last_error_code=exc.reason,
                    last_error_at=self.clock().isoformat(timespec="seconds"),
                )
            return {
                "complete": False, "workspace_deleted": False,
                "workspace_id": updated.workspace_id, "pending": True,
                "reason": exc.reason,
            }

    def _delete_target_state(self, record: WorkspaceRecord) -> str:
        """Classify a runtime target without ever guessing its ownership.

        ``absent`` is an explicit broker proof and is intentionally different
        from an unavailable broker.  A reported path must be below the
        configured root and, when metadata already has a verified path, must
        match it exactly.  The lifecycle caller decides separately whether an
        absent object has received the explicit metadata-only confirmation.
        """
        if self.runtime is None:
            raise WorkspaceError("ownership_unproven", "Workspace runtime identity could not be proven")
        try:
            observed = self._runtime_call(record, "inspect", {})
        except WorkspaceError as exc:
            raise WorkspaceError(
                "ownership_unproven", "Workspace runtime identity could not be proven",
                broker_reason=exc.reason,
            ) from exc
        if observed.get("state") == "absent":
            if not self._runtime_absence_proven(record, observed):
                raise WorkspaceError("ownership_unproven", "Workspace runtime absence could not be proven")
            return "absent"

        if not self._runtime_identity_matches(record, observed):
            raise WorkspaceError("ownership_unproven", "Workspace runtime identity could not be proven")
        observed_volume = observed.get("volume_name")
        if record.volume_name and observed_volume and observed_volume != record.volume_name:
            raise WorkspaceError("ownership_unproven", "Workspace runtime volume identity could not be proven")
        partial_state = observed.get("partial_state")
        if observed.get("state") == "partial":
            if (
                partial_state != "volume_only"
                or not isinstance(record.volume_name, str) or not record.volume_name
                or not isinstance(observed_volume, str) or not observed_volume
                or observed_volume != record.volume_name
            ):
                raise WorkspaceError("ownership_unproven", "Workspace runtime partial state could not be proven")

        reported_path = observed.get("host_path")
        if reported_path is not None:
            path, path_status = self._verified_runtime_path(reported_path)
            if path_status != "verified" or path is None:
                raise WorkspaceError("ownership_unproven", "Workspace runtime backing path could not be proven")
            if record.path_status == "verified" and record.host_path:
                if os.path.normcase(os.path.realpath(path)) != os.path.normcase(os.path.realpath(record.host_path)):
                    raise WorkspaceError("ownership_unproven", "Workspace runtime backing path changed")
        elif record.path_status == "verified" and record.host_path:
            # A previously verified path cannot silently become an opaque
            # identity during a destructive operation.
            raise WorkspaceError("ownership_unproven", "Workspace runtime backing path was not reported")
        return "partial" if partial_state == "volume_only" else "owned"

    def _delete_ownership_proof(self, record: WorkspaceRecord) -> bool:
        try:
            return self._delete_target_state(record) in {"owned", "partial", "absent"}
        except WorkspaceError:
            return False

    @staticmethod
    def _principal_for_record(record: WorkspaceRecord) -> Any:
        """Build the narrow principal shape accepted by lifecycle operations."""
        return type("WorkspacePrincipal", (), {"principal_id": record.principal_id})()

    def remove(
        self, principal: Any, *, connector_id: str | None = None,
        expected_revision: int | None = None, idempotency_key: str | None = None,
        idempotency_digest: str | None = None,
        allow_absent_cleanup: bool = False, preserve_retention_intent: bool = False,
    ) -> dict[str, Any]:
        record = self.metadata.get_by_principal(str(getattr(principal, "principal_id", principal)))
        if record is None:
            return {"status": "success", "workspace": None}
        if connector_id is not None and record.connector_id not in {None, connector_id}:
            raise WorkspaceError("ownership_unproven", "Workspace owner could not be proven")
        with self._queue(record.workspace_id):
            if expected_revision is not None and record.revision != expected_revision:
                raise WorkspaceError("path_conflict", "Workspace revision changed")
            digest = idempotency_digest or _digest({
                "operation": "remove", "workspace_id": record.workspace_id,
                "revision": record.revision,
            })
            broker_request_id = None
            if idempotency_key:
                replay = self.metadata.admin_idempotent(idempotency_key, digest)
                if replay is not None:
                    return replay
                replay = self.metadata.idempotent(record.workspace_id, idempotency_key, digest)
                if replay is not None:
                    return replay
                broker_request_id = _stable_broker_request_id(
                    record.workspace_id, idempotency_key, digest,
                )
            if self.metadata.has_live_lease(record.workspace_id):
                raise WorkspaceError("capacity_busy", "Workspace has an active operation")
            active = self._active_job_after_reconcile(record)
            if active is not None:
                raise self._job_running_error(record, active)
            retention = (
                {"deletion_intent": record.deletion_intent,
                 "deletion_requested_at": record.deletion_requested_at}
                if preserve_retention_intent else
                {"deletion_intent": "delete_now",
                 "deletion_requested_at": self.clock().isoformat(timespec="seconds")}
            )
            current = self.metadata.update(
                record.workspace_id, state="deleting", desired_state="absent", **retention,
            )
            try:
                target_state = self._delete_target_state(current)
            except WorkspaceError as exc:
                self.metadata.update(current.workspace_id, state="failed", desired_state="absent", last_error_code=exc.reason, last_error_at=self.clock().isoformat(timespec="seconds"))
                raise
            if target_state == "absent" and not allow_absent_cleanup:
                self.metadata.update(
                    current.workspace_id, state="failed", desired_state="absent",
                    last_error_code="absence_confirmation_required",
                    last_error_at=self.clock().isoformat(timespec="seconds"),
                )
                raise WorkspaceError(
                    "absence_confirmation_required",
                    "The runtime object is absent; explicit metadata-only cleanup is required",
                )
            try:
                # The broker owns the runtime teardown sequence.  In
                # particular, it can remove a failed sandbox whose named
                # volume is already missing; an app-level stop would try to
                # reconnect through that missing volume and prevent cleanup.
                # The broker remove operation validates ownership, stops a
                # running sandbox when possible, removes both SDK objects,
                # and proves their absence.
                if target_state in {"owned", "partial"}:
                    self._runtime_call(current, "remove", {}, request_id=broker_request_id)
            except WorkspaceError as exc:
                self.metadata.update(current.workspace_id, state="deleting", desired_state="absent", last_error_code=exc.reason, last_error_at=self.clock().isoformat(timespec="seconds"))
                raise
            response = {"status": "success", "workspace": None, "deleted_workspace_id": current.workspace_id}
            latest = self.metadata.get(current.workspace_id)
            if latest is None or not self.metadata.delete_if_revision(
                current.workspace_id, latest.revision,
                operation_key=idempotency_key, request_digest=digest, response=response,
            ):
                raise WorkspaceError("path_conflict", "Workspace changed during deletion")
            return response

    def preview_bulk_delete(
        self, workspace_ids: list[str], expected_revisions: dict[str, int], *,
        ttl_seconds: int = DELETE_PREVIEW_TTL_SECONDS,
    ) -> dict[str, Any]:
        """Create a short-lived, revision-bound lifecycle preview."""
        if not workspace_ids or len(workspace_ids) > 256 or len(set(workspace_ids)) != len(workspace_ids):
            raise WorkspaceError("invalid_arguments", "bulk lifecycle targets must be unique")
        if ttl_seconds <= 0 or ttl_seconds > 3600:
            raise WorkspaceError("invalid_arguments", "invalid lifecycle preview lifetime")
        if set(expected_revisions) != set(workspace_ids):
            raise WorkspaceError("invalid_arguments", "expected revisions must match the selected Workspaces")
        records: list[WorkspaceRecord] = []
        for workspace_id in workspace_ids:
            record = self.metadata.get(workspace_id)
            if record is None:
                raise WorkspaceError("path_unavailable", "Workspace target was not found")
            expected = expected_revisions.get(workspace_id)
            if expected is None or record.revision != expected:
                raise WorkspaceError("path_conflict", "Workspace target revision changed")
            if record.state == "deleting" or record.desired_state == "absent":
                raise WorkspaceError("workspace_deleting", "Workspace target is already pending lifecycle change")
            # Never issue a reclaim estimate from an unbounded cached sample.
            # The refresh preserves the lifecycle revision; the measurement
            # and exact path/identity fields are still bound into the token.
            record = self._fresh_delete_measurement(record)
            # Preview summaries are destructive-target evidence, not merely a
            # UI listing.  Require the broker to prove the exact runtime and
            # previously reported path before issuing a token.
            if self._delete_target_state(record) not in {"owned", "partial"}:
                raise WorkspaceError("ownership_unproven", "Workspace runtime identity could not be proven")
            records.append(record)
        targets = [{
            "workspace_id": row.workspace_id, "principal_id": row.principal_id,
            "runtime_name": row.runtime_name, "volume_name": row.volume_name,
            "revision": row.revision,
            "state": row.state, "owner_status": row.owner_status,
            "actual_bytes": row.measured_allocated_bytes,
            "apparent_bytes": row.measured_apparent_bytes,
            "usage_status": row.usage_status, "path_status": row.path_status,
            "host_path": row.host_path,
        } for row in records]
        reclaim = None if any(row.measured_allocated_bytes is None for row in records) else sum(row.measured_allocated_bytes or 0 for row in records)
        token = secrets.token_urlsafe(32)
        expires = (self.clock() + timedelta(seconds=ttl_seconds)).isoformat(timespec="seconds")
        self.metadata.save_delete_preview(
            hashlib.sha256(token.encode("ascii")).hexdigest(), list(workspace_ids),
            {key: int(value) for key, value in expected_revisions.items()}, targets,
            reclaim, expires_at=expires,
        )
        return {"token": token, "expires_at": expires, "targets": targets, "reclaim_bytes": reclaim}

    def apply_bulk_delete(
        self,
        workspace_ids: list[str],
        expected_revisions: dict[str, int],
        *,
        preview_token: str,
        confirm_high_trust: bool,
        idempotency_token: str,
        allow_absent_cleanup: bool = False,
    ) -> dict[str, Any]:
        """Apply a revision-bound preview with durable per-item outcomes.

        This method deliberately returns partial results.  A lease, stale
        measurement, or ownership mismatch must not turn successful earlier
        items into an all-or-nothing fiction, and a caller can inspect the
        durable results before issuing a fresh preview for failed items.
        """
        if not confirm_high_trust:
            raise WorkspaceError("high_trust_confirmation_required", "explicit high-trust confirmation is required")
        if not isinstance(idempotency_token, str) or not idempotency_token or len(idempotency_token) > 128:
            raise WorkspaceError("invalid_arguments", "idempotency token is required")
        if not isinstance(preview_token, str) or not preview_token or len(preview_token) > 256:
            raise WorkspaceError("invalid_arguments", "preview token is invalid")
        if not workspace_ids or len(workspace_ids) > 256 or len(set(workspace_ids)) != len(workspace_ids):
            raise WorkspaceError("invalid_arguments", "bulk lifecycle targets must be unique")
        if set(expected_revisions) != set(workspace_ids):
            raise WorkspaceError("invalid_arguments", "expected revisions must match the selected Workspaces")

        digest = _digest({
            "operation": "bulk_delete", "workspace_ids": list(workspace_ids),
            "expected_revisions": expected_revisions, "preview_token": preview_token,
        })
        replay = self.metadata.admin_idempotent(idempotency_token, digest)
        if replay is not None:
            return {**replay, "idempotency_replayed": True}
        prior = {item.get("workspace_id"): item for item in self.metadata.delete_apply_items(idempotency_token)}

        preview = self.metadata.get_delete_preview(hashlib.sha256(preview_token.encode("ascii")).hexdigest())
        if preview is None or preview.get("state") not in {"open", "consumed"}:
            raise WorkspaceError("path_conflict", "bulk deletion preview is invalid or already consumed")
        preview_consumed = preview.get("state") == "consumed"
        if _parse_time(preview["expires_at"]) <= self.clock() and not (preview_consumed and prior):
            raise WorkspaceError("path_conflict", "bulk deletion preview is expired")
        if preview["workspace_ids"] != list(workspace_ids) or preview["revisions"] != expected_revisions:
            raise WorkspaceError("path_conflict", "bulk deletion preview does not match the selected Workspaces")

        # Recheck the complete preview snapshot before consuming its one-shot
        # token.  This gives the caller a clean failure with no effects when a
        # row changed between preview and apply.
        records: list[WorkspaceRecord] = []
        target_by_id = {item["workspace_id"]: item for item in preview["targets"]}
        for workspace_id in workspace_ids:
            # A process may have committed an item (including its per-item
            # idempotency replay) after consuming the preview and before the
            # aggregate response was recorded.  Its durable outcome is enough
            # to replay it; do not require the deleted metadata row to remain.
            if workspace_id in prior:
                continue
            record = self.metadata.get(workspace_id)
            target = target_by_id.get(workspace_id)
            if record is None or target is None or record.revision != expected_revisions[workspace_id]:
                raise WorkspaceError("path_conflict", "bulk deletion target changed")
            # Re-probe each item immediately before consuming the one-shot
            # preview.  A valid token does not authorize use of an aged sample.
            record = self._fresh_delete_measurement(record)
            for field in ("principal_id", "runtime_name", "volume_name", "owner_status", "actual_bytes", "apparent_bytes", "usage_status", "path_status", "host_path"):
                if getattr(record, {"actual_bytes": "measured_allocated_bytes", "apparent_bytes": "measured_apparent_bytes"}.get(field, field), None) != target.get(field):
                    raise WorkspaceError("path_conflict", "bulk deletion measurement or ownership changed")
            if record.state == "deleting" or record.desired_state == "absent":
                raise WorkspaceError("workspace_deleting", "Workspace target is already pending lifecycle change")
            records.append(record)
        if not preview_consumed and not self.metadata.consume_delete_preview(preview["token_hash"]):
            raise WorkspaceError("path_conflict", "bulk deletion preview is no longer available")

        # Preserve durable outcomes for items already committed before a
        # process crash.  ``records`` intentionally excludes those rows
        # because their metadata may have been deleted already.
        results: list[dict[str, Any]] = [
            prior[workspace_id] for workspace_id in workspace_ids if workspace_id in prior
        ]
        for record in records:
            # An unknown/stale measurement is not a safe reclaim target.  It is
            # recorded as a durable failure before any runtime effect.
            if not self._measurement_is_fresh(record):
                outcome = {"workspace_id": record.workspace_id, "status": "failed", "reason": "measurement_unavailable"}
                self.metadata.save_delete_apply_item(idempotency_token, record.workspace_id, outcome)
                results.append(outcome)
                continue
            try:
                result = self.remove(
                    self._principal_for_record(record), connector_id=record.connector_id,
                    expected_revision=record.revision,
                    idempotency_key=f"{idempotency_token}:{record.workspace_id}",
                    allow_absent_cleanup=allow_absent_cleanup,
                )
                outcome = {"workspace_id": record.workspace_id, "status": "committed", "result": result}
            except WorkspaceError as exc:
                outcome = {"workspace_id": record.workspace_id, "status": "failed", "reason": exc.reason}
            self.metadata.save_delete_apply_item(idempotency_token, record.workspace_id, outcome)
            results.append(outcome)

        response = {
            "status": "success" if all(item["status"] == "committed" for item in results) else "partial",
            "results": results,
            "removed": [item["workspace_id"] for item in results if item["status"] == "committed"],
        }
        self.metadata.save_admin_idempotent(idempotency_token, digest, response)
        return response

    def preview_bulk_workspace_action(
        self, action: str, workspace_ids: list[str], *,
        expected_revisions: dict[str, int],
    ) -> dict[str, Any]:
        """Named adapter for Admin/domain integrations using action vocabulary."""
        if action != "remove":
            raise WorkspaceError("invalid_arguments", "bulk action must be remove")
        preview = self.preview_bulk_delete(workspace_ids, expected_revisions)
        return {
            "preview_token": preview["token"], "expires_at": preview["expires_at"],
            "targets": preview["targets"],
            "reclaim_estimate_bytes": preview["reclaim_bytes"],
            "reclaim_estimate_status": "verified" if preview["reclaim_bytes"] is not None else "unknown",
        }

    def apply_bulk_workspace_action(
        self, action: str, workspace_ids: list[str], *,
        expected_revisions: dict[str, int], preview_token: str,
        idempotency_token: str, confirm_high_trust: bool = False,
    ) -> dict[str, Any]:
        if action != "remove":
            raise WorkspaceError("invalid_arguments", "bulk action must be remove")
        return self.apply_bulk_delete(
            workspace_ids, expected_revisions, preview_token=preview_token,
            confirm_high_trust=confirm_high_trust, idempotency_token=idempotency_token,
        )

    def _filesystem_args(self, tool: str, args: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        mapping = {
            "workspace_list_files": "fs_list", "workspace_stat": "fs_stat", "workspace_read_file": "fs_read",
            "workspace_write_file": "fs_write", "workspace_edit_file": "fs_edit", "workspace_make_directory": "fs_mkdir",
            "workspace_copy_paths": "fs_copy", "workspace_move_paths": "fs_move", "workspace_remove_paths": "fs_remove",
            "workspace_search": "fs_search",
        }
        if tool not in mapping:
            raise WorkspaceError("invalid_arguments", "unknown Workspace tool")
        # A3 (DESIGN-12.18 SS3.3): overridden to "fs_lines" below when
        # workspace_read_file carries a line argument; every other tool
        # keeps its fixed mapping entry.
        operation = mapping[tool]
        allowed = {
            "workspace_list_files": {"path", "recursive", "max_entries"},
            "workspace_stat": {"path", "include_hash"},
            "workspace_read_file": {"path", "offset", "max_bytes", "encoding", "start_line", "end_line", "tail_lines"},
            "workspace_write_file": {"path", "text", "base64", "create_policy", "expected_sha256"},
            "workspace_edit_file": {"path", "edits", "expected_sha256"},
            "workspace_make_directory": {"path", "parents"},
            "workspace_copy_paths": {"sources", "destination", "conflict_policy"},
            "workspace_move_paths": {"sources", "destination", "conflict_policy"},
            "workspace_remove_paths": {"paths", "recursive", "expected_hashes"},
            "workspace_search": {"roots", "pattern", "mode", "max_paths", "max_matches"},
        }[tool] | {"idempotency_key"}
        unknown = set(args) - allowed
        if unknown:
            raise WorkspaceError("invalid_arguments", f"unknown argument(s): {', '.join(sorted(unknown))}")
        result = dict(args)
        if "path" in result:
            result["path"] = normalize_path(result["path"])
            # ``normalize_path`` deliberately uses the public relative-path
            # representation, where the Workspace root is ``""``.  The
            # broker protocol requires its explicit confined root spelling.
            if result["path"] == "" and tool in {"workspace_list_files", "workspace_stat"}:
                result["path"] = "/workspace"
        for key in ("destination",):
            if key in result:
                result[key] = normalize_path(result[key])
        for key in ("sources", "paths", "roots"):
            if key in result:
                values = result[key]
                if not isinstance(values, list) or not values or len(values) > (MAX_SEARCH_ROOTS if key == "roots" else MAX_COPY_PATHS):
                    raise WorkspaceError("invalid_arguments", f"{key} exceeds its bound")
                result[key] = [normalize_path(item) for item in values]
                if key == "roots":
                    result[key] = [item or "/workspace" for item in result[key]]
        if tool == "workspace_list_files":
            if not isinstance(result.get("recursive", False), bool):
                raise WorkspaceError("invalid_arguments", "recursive must be boolean")
            result.setdefault("recursive", False)
            entries = result.get("max_entries", 200)
            if isinstance(entries, bool) or not isinstance(entries, int) or not 1 <= entries <= MAX_LIST_ENTRIES:
                raise WorkspaceError("invalid_arguments", "max_entries must be between 1 and 2000")
            result["max_entries"] = entries
        if tool == "workspace_read_file":
            # A3 (DESIGN-12.18 SS3.3): start_line/end_line/tail_lines are
            # mutually exclusive with offset and with each other as a pair
            # (start_line/end_line together form one "range" mode).  encoding
            # is popped up front either way -- fs_lines has no "binary"
            # argument of its own, so a line-mode read's caller-requested
            # encoding is consumed at the manager (execute()'s
            # clean_arguments.get("encoding", ...)) via _decode_lines_result,
            # not here.
            has_start, has_end, has_tail = "start_line" in result, "end_line" in result, "tail_lines" in result
            has_lines = has_start or has_end or has_tail
            has_offset = "offset" in result
            encoding = result.pop("encoding", "text")
            if encoding not in {"text", "base64"}:
                raise WorkspaceError("invalid_arguments", "encoding must be text or base64")
            if has_lines:
                if has_offset:
                    raise WorkspaceError("invalid_arguments", "offset is mutually exclusive with start_line, end_line, and tail_lines")
                if has_tail and (has_start or has_end):
                    raise WorkspaceError("invalid_arguments", "tail_lines is mutually exclusive with start_line and end_line")
                lines_args: dict[str, Any] = {"path": result["path"]}
                if has_tail:
                    tail_lines = result["tail_lines"]
                    if isinstance(tail_lines, bool) or not isinstance(tail_lines, int) or not 1 <= tail_lines <= 10_000:
                        raise WorkspaceError("invalid_arguments", "tail_lines must be an integer between 1 and 10000")
                    lines_args["tail_lines"] = tail_lines
                else:
                    # end_line without start_line means from line 1;
                    # start_line without end_line means to the end -- both
                    # are simply omitted from lines_args and the broker's own
                    # fs_lines default handles the open end.
                    if has_start:
                        start_line = result["start_line"]
                        if isinstance(start_line, bool) or not isinstance(start_line, int) or start_line < 1:
                            raise WorkspaceError("invalid_arguments", "start_line must be an integer >= 1")
                        lines_args["start_line"] = start_line
                    if has_end:
                        end_line = result["end_line"]
                        if isinstance(end_line, bool) or not isinstance(end_line, int) or end_line < 1:
                            raise WorkspaceError("invalid_arguments", "end_line must be an integer >= 1")
                        lines_args["end_line"] = end_line
                    if has_start and has_end and lines_args["end_line"] < lines_args["start_line"]:
                        raise WorkspaceError("invalid_arguments", "end_line must not be before start_line")
                max_bytes = result.get("max_bytes", MAX_FILE_BYTES)
                if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or not 1 <= max_bytes <= MAX_FILE_BYTES:
                    raise WorkspaceError("invalid_arguments", "max_bytes is invalid")
                lines_args["max_bytes"] = max_bytes
                operation = "fs_lines"
                result = lines_args
            else:
                offset, max_bytes = result.get("offset", 0), result.get("max_bytes", MAX_FILE_BYTES)
                if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0 or isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or not 1 <= max_bytes <= MAX_FILE_BYTES:
                    raise WorkspaceError("invalid_arguments", "read offset or max_bytes is invalid")
                result["offset"], result["max_bytes"] = offset, max_bytes
                result["binary"] = encoding == "base64"
        if tool == "workspace_write_file":
            has_text, has_base64 = "text" in result, "base64" in result
            if has_text == has_base64:
                raise WorkspaceError("invalid_arguments", "write requires exactly one of text or base64")
            if has_text:
                content = _bounded_string(result["text"], "text", MAX_FILE_BYTES)
                result["text"] = content
            else:
                try:
                    decoded = base64.b64decode(result["base64"], validate=True)
                except Exception as exc:
                    raise WorkspaceError("invalid_arguments", "base64 content is invalid") from exc
                if len(decoded) > MAX_FILE_BYTES:
                    raise WorkspaceError("invalid_arguments", "file content exceeds 1 MiB")
            policy = result.get("create_policy", "parents")
            if policy not in {"parents", "existing", "fail"}:
                raise WorkspaceError("invalid_arguments", "invalid create_policy")
            result["create_parents"] = policy == "parents"
            result.pop("create_policy", None)
        if tool == "workspace_edit_file":
            edits = result.get("edits")
            if not isinstance(edits, list) or not edits or len(edits) > 256:
                raise WorkspaceError("invalid_arguments", "edits must contain 1-256 entries")
            encoded = len(json.dumps(edits, ensure_ascii=False).encode())
            if encoded > MAX_FILE_BYTES:
                raise WorkspaceError("invalid_arguments", "edits exceed 1 MiB")
        if tool in {"workspace_copy_paths", "workspace_move_paths"}:
            policy = result.get("conflict_policy", "fail")
            if policy not in {"fail", "skip", "replace", "rename"}:
                raise WorkspaceError("invalid_arguments", "invalid conflict_policy")
        if tool == "workspace_remove_paths" and not isinstance(result.get("recursive", False), bool):
            raise WorkspaceError("invalid_arguments", "recursive must be boolean")
        if tool == "workspace_remove_paths":
            expected = result.pop("expected_hashes", {})
            if not isinstance(expected, dict) or len(expected) > MAX_COPY_PATHS:
                raise WorkspaceError("invalid_arguments", "expected_hashes is invalid")
            result["expected_hashes"] = {
                normalize_path(path): digest
                for path, digest in expected.items()
                if isinstance(path, str) and isinstance(digest, str) and _SHA256.fullmatch(digest)
            }
            if len(result["expected_hashes"]) != len(expected):
                raise WorkspaceError("invalid_arguments", "expected_hashes must map paths to SHA-256 digests")
        if tool == "workspace_make_directory" and not isinstance(result.get("parents", False), bool):
            raise WorkspaceError("invalid_arguments", "parents must be boolean")
        if tool in {"workspace_stat", "workspace_read_file"} and "include_hash" in result and not isinstance(result["include_hash"], bool):
            raise WorkspaceError("invalid_arguments", "include_hash must be boolean")
        for key in ("expected_sha256",):
            if key in result and (not isinstance(result[key], str) or not _SHA256.fullmatch(result[key])):
                raise WorkspaceError("invalid_arguments", f"{key} must be a SHA-256 digest")
        if tool == "workspace_search":
            pattern = _bounded_string(result.get("pattern", ""), "pattern", MAX_SEARCH_PATTERN_BYTES)
            if not pattern:
                raise WorkspaceError("invalid_arguments", "search pattern is required")
            result.setdefault("mode", "glob")
            if result["mode"] not in {"glob", "text", "regex"}:
                raise WorkspaceError("invalid_arguments", "invalid search mode")
            if result["mode"] == "regex":
                try:
                    re.compile(pattern)
                except re.error as exc:
                    raise WorkspaceError("invalid_arguments", "invalid regular expression") from exc
            max_paths, max_matches = result.get("max_paths", MAX_SEARCH_PATHS), result.get("max_matches", MAX_SEARCH_MATCHES)
            if isinstance(max_paths, bool) or not isinstance(max_paths, int) or not 1 <= max_paths <= MAX_SEARCH_PATHS or isinstance(max_matches, bool) or not isinstance(max_matches, int) or not 1 <= max_matches <= MAX_SEARCH_MATCHES:
                raise WorkspaceError("invalid_arguments", "search result bounds are invalid")
            result["max_paths"], result["max_matches"] = max_paths, max_matches
            result["timeout_seconds"] = 30
        return operation, result

    def _job_start(self, record: WorkspaceRecord, args: dict[str, Any], *, request_id: str | None = None) -> dict[str, Any]:
        # output_encoding/strip_ansi are validated here (they are part of the
        # idempotency digest like any other argument) but not otherwise used
        # by this method: the start receipt itself carries no stream to
        # encode.  execute() already refused them when wait_ms == 0 (A2,
        # §3.2), and consumes them itself when building the waited result.
        unknown = set(args) - {"argv", "shell_script", "cwd", "timeout", "env", "output_encoding", "strip_ansi"}
        if unknown:
            raise WorkspaceError("invalid_arguments", f"unknown argument(s): {', '.join(sorted(unknown))}")
        if "output_encoding" in args and args["output_encoding"] not in {"auto", "text", "base64"}:
            raise WorkspaceError("invalid_arguments", "output_encoding must be auto, text, or base64")
        if "strip_ansi" in args and not isinstance(args["strip_ansi"], bool):
            raise WorkspaceError("invalid_arguments", "strip_ansi must be boolean")
        argv, script = args.get("argv"), args.get("shell_script")
        if (argv is None) == (script is None):
            raise WorkspaceError("invalid_arguments", "provide exactly one of argv or shell_script")
        if argv is not None:
            if not isinstance(argv, list) or not 1 <= len(argv) <= MAX_JOB_ARGS or not all(isinstance(item, str) for item in argv):
                raise WorkspaceError("invalid_arguments", "argv must contain 1-256 strings")
            if sum(len(item.encode()) for item in argv) > MAX_JOB_ARG_BYTES:
                raise WorkspaceError("invalid_arguments", "argv exceeds the job argument limit")
        else:
            script = _bounded_string(script, "shell_script", MAX_JOB_ARG_BYTES)
        # The public root normalizes to "", but the broker requires a real guest path.
        cwd = normalize_path(args.get("cwd", "/workspace") or "/workspace") or "/workspace"
        timeout = args.get("timeout", 600)
        if not isinstance(timeout, int) or not 1 <= timeout <= MAX_JOB_TIMEOUT:
            raise WorkspaceError("invalid_arguments", "timeout must be between 1 and 3600 seconds")
        env = args.get("env", {})
        if not isinstance(env, dict) or len(env) > MAX_ENV_KEYS or any(not isinstance(k, str) or not _ENV_KEY.fullmatch(k) or not isinstance(v, str) for k, v in env.items()) or sum(len(k.encode()) + len(v.encode()) for k, v in env.items()) > MAX_ENV_BYTES:
            raise WorkspaceError("invalid_arguments", "environment exceeds its bounds")
        request = {"cwd": cwd, "timeout_seconds": timeout, "env": env, "async": True}
        request["argv" if argv is not None else "shell_script"] = argv if argv is not None else script
        request_digest = _digest(request)
        active = self._active_job_after_reconcile(record)
        if active is not None:
            raise self._job_running_error(record, active)
        # A foreground job is represented by an active runtime job; the broker
        # remains authoritative after Cognita restart.
        result = self._runtime_call(record, "job_start", request, request_id=request_id)
        job_id = str(result.get("job_id") or uuid.uuid4())
        now = self.clock().isoformat(timespec="seconds")
        with self.metadata.transaction() as db:
            db.execute("INSERT OR REPLACE INTO workspace_jobs(job_id,workspace_id,request_digest,state,created_at,updated_at,runtime_job_id) VALUES(?,?,?,?,?,?,?)", (job_id, record.workspace_id, request_digest, result.get("state", "running"), now, now, result.get("job_id", job_id)))
        self.metadata.update(record.workspace_id, last_accessed_at=now, deletion_due_at=(self.clock() + timedelta(days=record.retention_days or self.retention_days)).isoformat(timespec="seconds"))
        return {"status": "success", "workspace": self._summary(record), "job": {"job_id": job_id, "state": result.get("state", "running")}}

    def _job_get(self, record: WorkspaceRecord, args: dict[str, Any]) -> dict[str, Any]:
        unknown = set(args) - {"job_id", "stdout_offset", "stderr_offset", "max_bytes", "output_encoding", "strip_ansi", "tail_lines"}
        if unknown:
            raise WorkspaceError("invalid_arguments", f"unknown argument(s): {', '.join(sorted(unknown))}")
        job_id = _bounded_string(args.get("job_id"), "job_id", 64)
        stdout_offset = args.get("stdout_offset", 0)
        stderr_offset = args.get("stderr_offset", 0)
        max_bytes = args.get("max_bytes", MAX_FILE_BYTES)
        if any(isinstance(value, bool) or not isinstance(value, int) for value in (stdout_offset, stderr_offset, max_bytes)) or stdout_offset < 0 or stderr_offset < 0 or not 1 <= max_bytes <= MAX_FILE_BYTES:
            raise WorkspaceError("invalid_arguments", "job output bounds are invalid")
        # A2 (DESIGN-12.18 §3.2): output_encoding/strip_ansi are only applied
        # when the caller actually asked for one of them, so a call that asks
        # for neither reproduces today's response byte-for-byte (no new
        # ``_encoding``/``_lossy`` fields appended).
        explicit_encoding = "output_encoding" in args
        encoding = args.get("output_encoding", "base64")
        if encoding not in {"auto", "text", "base64"}:
            raise WorkspaceError("invalid_arguments", "output_encoding must be auto, text, or base64")
        explicit_strip_ansi = "strip_ansi" in args
        strip_ansi_flag = args.get("strip_ansi", False)
        if not isinstance(strip_ansi_flag, bool):
            raise WorkspaceError("invalid_arguments", "strip_ansi must be boolean")
        # A3 (DESIGN-12.18 §3.3): tail_lines is only added to the broker
        # request when the caller actually asked for it, so a plain call
        # (no tail_lines) reproduces today's request byte-for-byte.
        call_args = {"job_id": job_id, "stdout_offset": stdout_offset, "stderr_offset": stderr_offset, "max_bytes": max_bytes}
        if "tail_lines" in args:
            tail_lines = args["tail_lines"]
            if isinstance(tail_lines, bool) or not isinstance(tail_lines, int) or not 1 <= tail_lines <= 10_000:
                raise WorkspaceError("invalid_arguments", "tail_lines must be an integer between 1 and 10000")
            call_args["tail_lines"] = tail_lines
        result = self._runtime_call(record, "job_get", call_args)
        result["job_id"] = job_id
        state = result.get("state")
        if state in {"succeeded", "failed", "canceled", "timed_out", "lost"}:
            self._cache_observed_job_state(record.workspace_id, job_id, state)
        if explicit_encoding or explicit_strip_ansi:
            self._apply_stream_encoding(result, encoding, strip_ansi_flag)
        # A3 (DESIGN-12.18 §3.3): has_more_stdout/has_more_stderr alias the
        # broker's stdout_has_more/stderr_has_more unconditionally -- both
        # keys, same value -- regardless of whether tail_lines was asked for.
        self._alias_job_has_more(result)
        return {"status": "success", "workspace": self._summary(record), "job": result}

    def _job_cancel(self, record: WorkspaceRecord, args: dict[str, Any], *, request_id: str | None = None) -> dict[str, Any]:
        unknown = set(args) - {"job_id"}
        if unknown:
            raise WorkspaceError("invalid_arguments", f"unknown argument(s): {', '.join(sorted(unknown))}")
        job_id = _bounded_string(args.get("job_id"), "job_id", 64)
        result = self._runtime_call(record, "job_cancel", {"job_id": job_id}, request_id=request_id)
        state = result.get("state", "canceled")
        if state in {"succeeded", "failed", "canceled", "timed_out", "lost"}:
            self._cache_observed_job_state(record.workspace_id, job_id, state)
        return {"status": "success", "workspace": self._summary(record), "job": {"job_id": job_id, **result}}

    def stop_idle(self, *, now: datetime | None = None) -> list[str]:
        now = now or self.clock()
        stopped: list[str] = []
        for record in self.metadata.list():
            with self._queue(record.workspace_id):
                current = self.metadata.get(record.workspace_id)
                if current is None or current.state != "running" or self.metadata.has_live_lease(record.workspace_id):
                    continue
                try:
                    if self._active_job_after_reconcile(current) is not None:
                        continue
                except WorkspaceError:
                    # A failed status probe cannot prove that a guest job has
                    # stopped.  Leave the Workspace in place for a later pass.
                    continue
                if (now - _parse_time(current.last_accessed_at)).total_seconds() < self.idle_seconds:
                    continue
                try:
                    self._runtime_call(current, "stop", {})
                    # A4 (DESIGN-12.18 SS3.4): idle cleanup is the only path
                    # that stops a Workspace nobody asked to stop, so record it.
                    self.metadata.update(record.workspace_id, state="stopped", desired_state="stopped", stopped_at=now.isoformat(timespec="seconds"), last_auto_action="idle_stop", last_auto_action_at=now.isoformat(timespec="seconds"))
                    log.info("Workspace idle-stopped workspace_id=%s idle_seconds=%d", record.workspace_id, self.idle_seconds)
                    stopped.append(record.workspace_id)
                except WorkspaceError as exc:
                    self.metadata.update(record.workspace_id, state="failed", last_error_code=exc.reason, last_error_at=now.isoformat(timespec="seconds"))
        return stopped

    def scavenge(self, *, now: datetime | None = None) -> list[str]:
        """Compatibility entry point for one bounded retention pass."""
        return [item["workspace_id"] for item in self.cleanup_retention(now=now, apply=True)["items"]
                if item["status"] == "deleted"]

    @staticmethod
    def _retention_due(record: WorkspaceRecord, now: datetime) -> bool:
        if record.deletion_intent == "delete_now":
            return True  # Explicit credential deletion supersedes Pin.
        if record.deletion_intent != "normal" or record.owner_status != "tombstoned" or record.pinned:
            return False
        if not record.deletion_requested_at or not record.deletion_due_at:
            return False
        try:
            requested = _parse_time(record.deletion_requested_at)
            due = _parse_time(record.deletion_due_at)
        except (TypeError, ValueError):
            return False
        # A shortened/corrupt deadline is not authority to delete early.
        return due == requested + timedelta(days=DEFAULT_RETENTION_DAYS) and due <= now

    def cleanup_retention(
        self, *, now: datetime | None = None, apply: bool = False,
        limit: int = 32, after_workspace_id: str = "",
    ) -> dict[str, Any]:
        """Preview or apply a bounded page of recorded Workspace deletion intents.

        A preview is metadata-only. Apply repeats revision, identity, intent,
        Pin, lease, and job checks under the per-Workspace queue immediately
        before the existing ownership-verified remove path.
        """
        now = now or self.clock()
        candidates = self.metadata.retention_candidates(
            now=now.isoformat(timespec="seconds"), after_workspace_id=after_workspace_id, limit=limit,
        )
        items: list[dict[str, Any]] = []
        for snapshot in candidates:
            item: dict[str, Any] = {
                "workspace_id": snapshot.workspace_id, "revision": snapshot.revision,
                "intent": snapshot.deletion_intent, "pinned": bool(snapshot.pinned),
                "pin_overridden": bool(snapshot.pinned and snapshot.deletion_intent == "delete_now"),
                "deletion_due_at": snapshot.deletion_due_at,
            }
            with self._queue(snapshot.workspace_id):
                current = self.metadata.get(snapshot.workspace_id)
                def identity(row: Any) -> tuple:
                    return (
                        row.workspace_id, row.principal_id, row.connector_id, row.runtime_name,
                        row.volume_name, row.credential_id, row.owner_status, row.deletion_intent,
                        row.deletion_requested_at, row.deletion_due_at, row.pinned, row.revision,
                        row.state, row.desired_state,
                    )
                if current is None or identity(current) != identity(snapshot) or not self._retention_due(current, now):
                    item["status"] = "changed"
                elif self.metadata.has_live_lease(current.workspace_id):
                    item["status"] = "busy"
                elif not apply:
                    # Preview never calls the broker or changes cached job state.
                    item["status"] = "busy" if self.metadata.active_job(current.workspace_id) is not None else "due"
                else:
                    try:
                        if self._active_job_after_reconcile(current) is not None:
                            item["status"] = "busy"
                            items.append(item)
                            continue
                        self.remove(
                            self._principal_for_record(current), connector_id=current.connector_id,
                            expected_revision=current.revision,
                            idempotency_key=f"retention:{current.workspace_id}:{current.revision}",
                            allow_absent_cleanup=True,
                            preserve_retention_intent=current.deletion_intent == "normal",
                        )
                        item["status"] = "deleted"
                    except WorkspaceError as exc:
                        # Leave a failed normal intent normal: a subsequent Pin
                        # must still beat an automatic expiration retry.
                        item["status"] = "deferred"
                        item["reason"] = exc.reason
            items.append(item)
        return {
            "items": items,
            "next_cursor": candidates[-1].workspace_id if len(candidates) == limit else "",
        }


def workspace_tool_result(manager: WorkspaceManager | None, principal: Any, tool: str, arguments: dict[str, Any], *, connector_id: str | None = None) -> dict[str, Any]:
    """Adapter used by both public route families; never exposes raw broker errors."""
    try:
        if manager is None:
            raise WorkspaceError("runtime_unavailable", "Workspace runtime is not configured")
        if is_self_test_principal(principal) and not self_test_principal_matches(
            principal, connector_id
        ):
            # 13.0 §7.3: a test principal owns exactly one Workspace, the one
            # its own connector derives. Re-deriving the ID here means a
            # principal handle forged for — or carried over from — another
            # connector cannot reach a Workspace, including on the
            # Workspace-only route family, which never admits the test key at
            # all and so must never see one of these principals.
            log.warning(
                "Workspace call refused for a self-test principal bound to another "
                "connector connector_id=%s tool=%s", connector_id, tool,
            )
            raise WorkspaceError("unauthorized", "Workspace requires a durable authenticated principal")
        return manager.execute(principal, tool, arguments, connector_id=connector_id)
    except WorkspaceError as exc:
        log.warning(
            "Workspace tool failed",
            extra=_safe_workspace_failure_fields(
                manager, principal, tool, arguments, exception=exc,
            ),
        )
        return {"status": "error", "reason": exc.reason, "message": str(exc), **exc.fields}
    except Exception as exc:
        log.warning(
            "Unexpected Workspace tool failure",
            extra=_safe_workspace_failure_fields(
                manager, principal, tool, arguments, exception=exc,
            ),
        )
        return {"status": "error", "reason": "internal_error", "message": "Workspace operation failed"}


# Descriptive aliases keep the domain seam easy to consume from service wiring
# and make the ownership boundary explicit without duplicating implementations.
WorkspaceStore = WorkspaceMetadataStore
WorkspaceService = WorkspaceManager
WorkspaceRuntimeClient = RuntimeClient
