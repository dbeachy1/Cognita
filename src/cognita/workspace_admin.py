"""Admin-facing Workspace lifecycle contracts.

The Admin surface owns transport, CSRF, high-trust confirmation, and response
redaction.  Workspace state, leases, quotas, path accounting, and runtime
operations remain owned by the Workspace domain/runtime services.  This module
is deliberately a small seam between those layers so an unavailable domain
service fails closed instead of being silently replaced by browser logic.
"""

from __future__ import annotations

import base64
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from threading import RLock
from types import SimpleNamespace
from typing import Any, ClassVar, Literal, Protocol

from .runtime_broker.network import NETWORK_SCHEMES, BraveSearchService, NetworkPolicy
from .workspace import (
    WorkspaceError,
    WorkspaceManager,
    WorkspaceRecord,
    _digest,
    _parse_time,
)

WorkspaceAction = Literal[
    "start", "stop", "pin", "unpin", "remove", "reset", "diagnostics", "retry"
]


class WorkspaceNotFound(RuntimeError):
    pass


class WorkspaceRevisionConflict(RuntimeError):
    pass


class WorkspaceUnavailable(RuntimeError):
    pass


class WorkspaceAdminService(Protocol):
    """Server-side contract consumed by :func:`create_admin_app`.

    Implementations must return server-computed values.  In particular,
    ``actual_bytes``, ``apparent_bytes``, expiry/deletion timestamps, lease
    state, and health must never be derived from request or browser input.
    Methods may be synchronous or awaitable; the Admin adapter supports both.
    """

    def list_admin_workspaces(
        self,
        *,
        sort: str,
        direction: str,
        search: str,
        states: Sequence[str],
        pinned: bool | None,
        expired: bool | None,
        over_warning: bool | None,
        owner_status: str | None = None,
    ) -> Any: ...

    def runtime_health(self) -> Any: ...

    def workspace_action(
        self,
        action: WorkspaceAction,
        workspace_id: str,
        *,
        expected_revision: int,
        idempotency_token: str | None,
    ) -> Any: ...

    def bulk_workspace_action(
        self,
        action: Literal["remove", "reset"],
        workspace_ids: Sequence[str],
        *,
        expected_revisions: dict[str, int],
        idempotency_token: str | None,
    ) -> Any: ...

    def preview_bulk_workspace_action(
        self,
        action: Literal["remove"],
        workspace_ids: Sequence[str],
        *,
        expected_revisions: dict[str, int],
    ) -> Any: ...

    def apply_bulk_workspace_action(
        self,
        action: Literal["remove"],
        workspace_ids: Sequence[str],
        *,
        expected_revisions: dict[str, int],
        preview_token: str,
        idempotency_token: str | None,
        confirm_high_trust: bool = False,
    ) -> Any: ...

    def get_workspace_settings(self) -> Any: ...

    def preview_workspace_settings(self, values: dict[str, Any]) -> Any: ...

    def update_workspace_settings(
        self,
        values: dict[str, Any],
        *,
        expected_revision: int,
        confirm_high_trust: bool,
        idempotency_token: str | None,
    ) -> Any: ...

    def test_brave_search(self) -> Any: ...


# Explicit allowlists keep additions to the domain model from becoming
# accidental Admin disclosures.  Secret-looking fields are excluded even when
# a worker returns them by mistake.
WORKSPACE_FIELDS = frozenset(
    {
        "id", "workspace_id", "connector_id", "surface_id", "connector_name",
        "surface_name", "credential_id", "key_id", "credential_label", "label",
        "state", "status", "created_at", "last_activity_at", "last_accessed_at",
        "idle_duration_seconds", "deletion_due_at", "expiry_at", "actual_bytes",
        "apparent_bytes", "quota_bytes", "quota_percent", "used_bytes", "pinned",
        "retention", "host_path", "container_path", "runtime_health", "revision",
        "orphaned", "revoked", "expired", "over_warning", "warning", "lease_expires_at",
        "desired_state", "owner_status", "last_error_code", "last_error_at",
        "runtime_generation", "path_status", "volume_name", "measured_at", "usage_status",
    }
)
STATUS_FIELDS = frozenset(
    {
        "status", "runtime", "runtime_version", "sdk_version", "guest_agent_version",
        "toolbox_version", "kvm", "running_count", "running_capacity", "capacity",
        "host_root", "container_root", "filesystem_capacity_bytes", "usable_capacity_bytes",
        "actual_allocation_bytes", "apparent_allocation_bytes", "free_bytes",
        "reserved_bytes", "reserve_bytes", "filesystem_free_bytes", "admissible_free_bytes",
        "workspace_allocated_bytes", "workspace_apparent_bytes", "measurement_status",
        "measurement_reason", "measured_at", "measurement_source", "runtime_probe_status",
        "committed_growth_bytes",
        "revision", "message", "checked_at", "error_code", "stage", "category",
        "correlation_id", "retryable",
    }
)
PREVIEW_FIELDS = STATUS_FIELDS | frozenset({"network_mode", "network_rules", "brave_enabled", "brave_configured"})
SETTING_FIELDS = frozenset(
    {
        "retention_days", "quota_bytes", "idle_stop_seconds", "host_reserve_bytes",
        "network_mode", "network_rules", "brave_enabled", "brave_configured",
        "revision", "warning_threshold_percent", "max_running_workspaces",
    }
)


def _mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    as_dict = getattr(value, "as_dict", None)
    if callable(as_dict):
        return dict(as_dict())
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        return dict(dump(mode="json"))
    return dict(getattr(value, "__dict__", {}))


def _safe_mapping(value: Any, fields: frozenset[str]) -> dict[str, Any]:
    row = _mapping(value)
    return {key: item for key, item in row.items() if key in fields}


def workspace_view(value: Any) -> dict[str, Any]:
    """Normalize one nonsecret, server-computed Workspace summary."""
    output = _safe_mapping(value, WORKSPACE_FIELDS)
    if "runtime_health" in output:
        output["runtime_health"] = status_view(output["runtime_health"])
    return output


def status_view(value: Any) -> dict[str, Any]:
    return _safe_mapping(value, STATUS_FIELDS)


def preview_view(value: Any) -> dict[str, Any]:
    return _safe_mapping(value, PREVIEW_FIELDS)


def settings_view(value: Any) -> dict[str, Any]:
    output = _safe_mapping(value, SETTING_FIELDS)
    raw_rules = output.get("network_rules")
    if isinstance(raw_rules, list):
        output["network_rules"] = [_network_rule_view(rule) for rule in raw_rules]
    return output


def _network_rule_view(value: Any) -> dict[str, Any]:
    """Expose domain/port rules with an explicit legacy-availability marker."""
    rule = _mapping(value)
    protocols = rule.get("protocols", list(NETWORK_SCHEMES))
    if not isinstance(protocols, list):
        protocols = list(protocols) if isinstance(protocols, tuple) else []
    available = tuple(sorted(set(protocols))) == NETWORK_SCHEMES
    output = {
        "domain": rule.get("domain", ""),
        "suffix": bool(rule.get("suffix", False)),
        "ports": list(rule.get("ports", [])),
        "protocols": protocols,
        "availability": "available" if available else "unavailable",
    }
    if not available:
        output["availability_reason"] = "Edit this rule to apply it to both HTTP and HTTPS."
    return output


def admin_payload(value: Any, *, collection_key: str = "workspaces") -> dict[str, Any]:
    """Normalize list responses without passing through secrets or objects."""
    row = _mapping(value)
    records = row.get(collection_key, row.get("records", row.get("items", [])))
    if isinstance(value, (list, tuple)):
        records = value
        row = {}
    output = {key: item for key, item in row.items() if key in {"revision", "storage", "runtime", "settings", "next_cursor"}}
    output[collection_key] = [workspace_view(item) for item in (records or [])]
    if "storage" in row:
        output["storage"] = status_view(row["storage"])
    if "runtime" in row:
        output["runtime"] = status_view(row["runtime"])
    if "settings" in row:
        output["settings"] = settings_view(row["settings"])
    return output


class WorkspaceAdminAdapter:
    """Authenticated Admin projection over the Workspace domain service.

    This adapter deliberately identifies records by server-owned Workspace UUID
    and converts them back to their durable principal before invoking lifecycle
    operations. Public callers never provide a principal or runtime name.
    """

    _SORTS: ClassVar = {
        "credential": lambda row: row.display_label.casefold(),
        "connector": lambda row: (row.connector_id or "").casefold(),
        "state": lambda row: row.state,
        "created": lambda row: row.created_at,
        "last_accessed": lambda row: row.last_accessed_at,
        "deletion_due": lambda row: row.deletion_due_at or "",
        "actual_allocation": lambda row: row.measured_allocated_bytes or 0,
        "apparent_size": lambda row: row.measured_apparent_bytes or 0,
        "quota_percent": lambda row: (
            (row.measured_allocated_bytes or 0) / row.quota_bytes if row.quota_bytes else 0
        ),
    }

    def __init__(self, manager: WorkspaceManager, *, host_root: str | None = None,
                 container_root: str = "/root/.microsandbox", max_running: int = 4,
                 trusted_secret_store: Any = None,
                 brave_search: BraveSearchService | None = None) -> None:
        self.manager = manager
        self.host_root = host_root
        self.container_root = container_root
        self.max_running = max_running
        self.trusted_secret_store = trusted_secret_store
        self.brave_search = brave_search
        self._settings_lock = RLock()
        settings = self.manager.metadata.settings()
        self._apply_settings(settings)
        if self.brave_search is not None and self.trusted_secret_store is not None:
            secret = self.trusted_secret_store.trusted_secret("brave-search-api-key")
            if secret:
                self.brave_search.configure(secret, enabled=settings["brave_enabled"])

    def _apply_settings(self, settings: dict[str, Any]) -> None:
        self.manager.retention_days = settings["retention_days"]
        self.manager.quota_bytes = settings["quota_bytes"]
        self.manager.idle_seconds = settings["idle_stop_seconds"]
        self.manager.host_reserve_bytes = settings["host_reserve_bytes"]
        self.manager.max_running_workspaces = settings["max_running_workspaces"]
        # Keep the inventory filter tied to the persisted operator setting.
        # The browser sends only the boolean filter; it must not duplicate a
        # threshold that can be changed from the Admin settings panel.
        self.manager.warning_threshold_percent = settings.get("warning_threshold_percent", 80)
        self.max_running = settings["max_running_workspaces"]
        self.manager.network_policy = {
            "mode": settings["network_mode"],
            "rules": settings["network_rules"],
            "explicit_confirmation": settings["network_mode"] == "unrestricted_public",
        }

    def _record(self, workspace_id: str) -> WorkspaceRecord:
        record = self.manager.metadata.get(workspace_id)
        if record is None:
            raise WorkspaceNotFound("Workspace was not found")
        return record

    @staticmethod
    def _principal(record: WorkspaceRecord) -> Any:
        return SimpleNamespace(principal_id=record.principal_id, surface_id=record.connector_id)

    def _replay(self, token: str | None, payload: dict[str, Any]) -> tuple[str, dict[str, Any] | None]:
        digest = _digest(payload)
        try:
            replay = self.manager.metadata.admin_idempotent(token, digest)
        except WorkspaceError as exc:
            raise WorkspaceRevisionConflict("idempotency token conflict") from exc
        return digest, replay

    def _save_replay(self, token: str | None, digest: str,
                     response: dict[str, Any]) -> dict[str, Any]:
        self.manager.metadata.save_admin_idempotent(token, digest, response)
        return response

    @staticmethod
    def _remove_response(result: dict[str, Any], expected_revision: int) -> dict[str, Any]:
        """Adapt the manager's atomic remove result to the Admin response."""
        return {
            "revision": result.get("revision", expected_revision + 1),
            "workspace": None,
            "status": result.get("status", "success"),
        }

    @staticmethod
    def _view(record: WorkspaceRecord, now: datetime | None = None) -> dict[str, Any]:
        now = now or datetime.now(UTC)
        measured_at = getattr(record, "measured_at", None)
        actual = record.measured_allocated_bytes
        apparent = record.measured_apparent_bytes
        # Legacy rows use a zero default before a measurement exists. Expose
        # that state as unknown instead of making the Admin claim 0 bytes.
        if not measured_at and actual == 0 and apparent == 0:
            actual = apparent = None
        host_path = getattr(record, "host_path", None)
        path_status = getattr(record, "path_status", None) or (
            "verified" if host_path else "not_reported"
        )
        owner_status = getattr(record, "owner_status", None)
        if not owner_status:
            owner_status = "revoked" if getattr(record, "revoked", False) else (
                "orphaned" if getattr(record, "orphaned", False) else "active"
            )
        return {
            "workspace_id": record.workspace_id,
            "connector_id": record.connector_id,
            "credential_id": getattr(record, "credential_id", None) or record.principal_id,
            "surface_name": getattr(record, "surface_name", None),
            "credential_label": record.display_label,
            "state": record.state,
            "desired_state": record.desired_state,
            "owner_status": owner_status,
            "created_at": record.created_at,
            "last_accessed_at": record.last_accessed_at,
            "idle_duration_seconds": max(0, int((now - _parse_time(record.last_accessed_at)).total_seconds())),
            "deletion_due_at": record.deletion_due_at,
            "actual_bytes": actual,
            "apparent_bytes": apparent,
            "quota_bytes": record.quota_bytes,
            "quota_percent": round(actual * 100 / record.quota_bytes, 2) if actual is not None and record.quota_bytes else None,
            "pinned": record.pinned,
            "retention": "indefinite" if record.pinned else record.retention_days,
            "last_error_code": record.last_error_code,
            "last_error_at": record.last_error_at,
            "runtime_generation": record.runtime_generation,
            "host_path": host_path,
            "path_status": path_status,
            "volume_name": getattr(record, "volume_name", None),
            "measured_at": measured_at,
            "usage_status": getattr(record, "usage_status", None) or (
                "verified" if measured_at else "unknown"
            ),
            "revision": record.revision,
        }

    def _storage_snapshot(self, records: Sequence[WorkspaceRecord] | None = None) -> dict[str, Any]:
        """Return domain-owned host metrics, preserving unknown/error states.

        The Admin container does not receive the broker's Workspace root.  The
        domain manager owns the marker-backed filesystem probe and returns the
        configured root only as nonsecret display metadata.
        """
        records = list(records if records is not None else self.manager.metadata.list())
        # A total is only truthful when every inventory row has a fresh,
        # nonnegative sample.  Summing the rows that happen to have a positive
        # value would silently report a partial inventory as the host total.
        # Conversely, zero is a real measurement and must survive aggregation;
        # only the legacy/unmeasured state is unknown.
        def _measured_value(row: WorkspaceRecord, field: str) -> int | None:
            value = getattr(row, field, None)
            status = getattr(row, "usage_status", None)
            measured_at = getattr(row, "measured_at", None)
            if status in {"stale", "error"} or (
                status not in {"fresh", "verified"} and not measured_at
            ):
                return None
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                return None
            return value

        allocated_values = [_measured_value(row, "measured_allocated_bytes") for row in records]
        apparent_values = [_measured_value(row, "measured_apparent_bytes") for row in records]
        allocated_complete = len(allocated_values) == len(records) and all(
            value is not None for value in allocated_values
        )
        apparent_complete = len(apparent_values) == len(records) and all(
            value is not None for value in apparent_values
        )
        allocated_total = sum(value for value in allocated_values if value is not None) if allocated_complete else None
        apparent_total = sum(value for value in apparent_values if value is not None) if apparent_complete else None
        committed_growth_supplier = getattr(self.manager, "_committed_growth_bytes", None)
        if not callable(committed_growth_supplier):
            committed_growth_supplier = getattr(self.manager.metadata, "held_growth_bytes", None)
        try:
            committed_growth = committed_growth_supplier() if callable(committed_growth_supplier) else None
        except Exception:
            committed_growth = None
        if isinstance(committed_growth, bool) or not isinstance(committed_growth, int) or committed_growth < 0:
            committed_growth = None
        snapshot: dict[str, Any] = {
            "host_root": self.host_root,
            "container_root": self.container_root,
            "filesystem_capacity_bytes": None,
            "filesystem_free_bytes": None,
            "reserve_bytes": self.manager.host_reserve_bytes,
            "admissible_free_bytes": None,
            "workspace_allocated_bytes": allocated_total,
            "workspace_apparent_bytes": apparent_total,
            "actual_allocation_bytes": allocated_total,
            "apparent_allocation_bytes": apparent_total,
            "committed_growth_bytes": committed_growth,
            "measurement_status": "unknown",
            "measurement_source": "host filesystem",
            # Do not claim a measurement occurred when the root is absent or
            # the filesystem probe fails.  The timestamp is assigned only
            # after the corresponding probe succeeds below.
            "measured_at": None,
        }
        domain_snapshot = getattr(self.manager, "storage_snapshot", None)
        if not callable(domain_snapshot):
            snapshot["measurement_status"] = "unavailable"
            snapshot["measurement_reason"] = "Workspace capacity provider is not configured"
            return snapshot
        try:
            measured = _mapping(domain_snapshot())
        except Exception as exc:
            snapshot["measurement_status"] = "error"
            snapshot["measurement_reason"] = type(exc).__name__
            return snapshot
        for key in (
            "host_root", "container_root", "filesystem_capacity_bytes",
            "filesystem_free_bytes", "reserve_bytes", "admissible_free_bytes",
            "workspace_allocated_bytes", "workspace_apparent_bytes", "measured_at",
            "measurement_status", "measurement_reason", "measurement_source",
            "runtime_probe_status",
        ):
            if key in measured:
                snapshot[key] = measured[key]
        snapshot["actual_allocation_bytes"] = snapshot["workspace_allocated_bytes"]
        snapshot["apparent_allocation_bytes"] = snapshot["workspace_apparent_bytes"]
        snapshot["free_bytes"] = snapshot["filesystem_free_bytes"]
        snapshot["reserved_bytes"] = snapshot["reserve_bytes"]
        snapshot["usable_capacity_bytes"] = snapshot["admissible_free_bytes"]
        snapshot["measurement_source"] = measured.get("measurement_source") or "mounted filesystem marker"
        return snapshot

    def list_admin_workspaces(self, *, sort: str, direction: str, search: str,
                              states: Sequence[str], pinned: bool | None,
                              expired: bool | None, over_warning: bool | None,
                              owner_status: str | None = None,
                              cursor: str | None = None, limit: int = 100) -> dict[str, Any]:
        if sort not in self._SORTS or direction not in {"asc", "desc"}:
            raise ValueError("invalid sort")
        now = datetime.now(UTC)
        all_records = self.manager.metadata.list()
        records = list(all_records)
        needle = search.casefold().strip()
        if needle:
            records = [row for row in records if needle in row.display_label.casefold()
                       or needle in (row.connector_id or "").casefold()
                       or needle in (getattr(row, "credential_id", None) or row.principal_id or "").casefold()
                       or needle in (row.workspace_id or "").casefold()]
        if states:
            records = [row for row in records if row.state in states]
        if pinned is not None:
            records = [row for row in records if row.pinned is pinned]
        if expired is not None:
            records = [row for row in records if bool(
                row.deletion_due_at and _parse_time(row.deletion_due_at) <= now
            ) is expired]
        if over_warning is not None:
            warning_threshold = getattr(self.manager, "warning_threshold_percent", None)
            if isinstance(warning_threshold, bool) or not isinstance(warning_threshold, (int, float)):
                warning_threshold = 80
            # An unmeasured Workspace is neither above nor below the threshold.
            records = [row for row in records if bool(
                row.quota_bytes
                and isinstance(row.measured_allocated_bytes, int)
                and not isinstance(row.measured_allocated_bytes, bool)
                and (
                    getattr(row, "usage_status", None) in {"fresh", "verified"}
                    or (
                        getattr(row, "usage_status", None) in {None, "unknown"}
                        and getattr(row, "measured_at", None)
                    )
                )
                and row.measured_allocated_bytes * 100 / row.quota_bytes >= warning_threshold
            ) is over_warning]
        if owner_status is not None:
            records = [row for row in records if (
                getattr(row, "owner_status", None)
                or ("revoked" if getattr(row, "revoked", False) else
                    "orphaned" if getattr(row, "orphaned", False) else "active")
            ) == owner_status]
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 500:
            raise ValueError("invalid page limit")
        offset = 0
        if cursor:
            try:
                decoded = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)).decode("ascii")
                offset = int(decoded)
            except (ValueError, UnicodeError):
                raise ValueError("invalid page cursor") from None
            if offset < 0 or offset > len(records):
                raise ValueError("invalid page cursor")
        records.sort(key=self._SORTS[sort], reverse=direction == "desc")
        if sort in {"actual_allocation", "apparent_size", "quota_percent"}:
            field = "measured_apparent_bytes" if sort == "apparent_size" else "measured_allocated_bytes"
            records.sort(key=lambda row: getattr(row, field) is None)
        page = records[offset:offset + limit]
        views = [self._view(row, now) for row in page]
        next_offset = offset + len(page)
        next_cursor = None
        if next_offset < len(records):
            next_cursor = base64.urlsafe_b64encode(str(next_offset).encode("ascii")).decode("ascii").rstrip("=")
        health = self.runtime_health()
        # Storage is an inventory-wide summary, not a summary of the current
        # search/filter/page.  A page with one measured row must not hide an
        # unmeasured row elsewhere in the inventory.
        storage = self._storage_snapshot(all_records)
        storage.update({
            "runtime_probe_status": health.get("runtime_probe_status", health.get("status", "unknown")),
            "running_count": health.get("running_count"),
            "running_capacity": health.get("running_capacity"),
        })
        return {
            "revision": max((row.revision for row in records), default=0),
            "next_cursor": next_cursor,
            "workspaces": views,
            "storage": storage,
            "runtime": health,
        }

    def runtime_health(self) -> dict[str, Any]:
        health = (
            self.manager.runtime.health()
            if self.manager.runtime is not None and hasattr(self.manager.runtime, "health")
            else {"status": "degraded", "runtime": "unavailable"}
        )
        health = _mapping(health)
        running = sum(row.state == "running" for row in self.manager.metadata.list())
        return {**health, "running_count": running, "running_capacity": self.max_running,
                "runtime_probe_status": health.get("status", health.get("runtime", "unknown")),
                "storage": self._storage_snapshot()}

    def preview_bulk_workspace_action(self, action: Literal["remove"], workspace_ids: Sequence[str], *,
                                      expected_revisions: dict[str, int]) -> dict[str, Any]:
        if action != "remove" or not workspace_ids:
            raise ValueError("bulk preview supports remove for a nonempty selection")
        # Preview ownership, measurement freshness, exact revisions, and the
        # durable one-shot token belong to WorkspaceManager.  The Admin layer
        # only adapts its domain vocabulary to the HTTP contract.
        preview = self.manager.preview_bulk_workspace_action(
            action, list(workspace_ids), expected_revisions=dict(expected_revisions),
        )
        return {
            "preview_token": preview.get("preview_token", preview.get("token")),
            "expires_at": preview.get("expires_at"),
            "targets": [workspace_view(item) for item in preview.get("targets", [])],
            "reclaim_estimate_bytes": preview.get(
                "reclaim_estimate_bytes", preview.get("reclaim_bytes")
            ),
            "reclaim_estimate_status": preview.get(
                "reclaim_estimate_status",
                "verified" if preview.get("reclaim_bytes") is not None else "unknown",
            ),
        }

    def apply_bulk_workspace_action(self, action: Literal["remove"], workspace_ids: Sequence[str], *,
                                    expected_revisions: dict[str, int], preview_token: str,
                                    idempotency_token: str | None,
                                    confirm_high_trust: bool = False) -> dict[str, Any]:
        if not idempotency_token:
            raise ValueError("idempotency_token is required")
        # The domain owns preview expiry/replay, per-item leases and ownership
        # proofs.  Do not consume or recreate tokens in this transport adapter.
        return self.manager.apply_bulk_workspace_action(
            action, list(workspace_ids), expected_revisions=dict(expected_revisions),
            preview_token=preview_token, idempotency_token=idempotency_token,
            confirm_high_trust=confirm_high_trust,
        )

    def workspace_action(self, action: WorkspaceAction, workspace_id: str, *,
                         expected_revision: int, idempotency_token: str | None) -> dict[str, Any]:
        action_payload = {
            "operation": "workspace_action", "action": action,
            "workspace_id": workspace_id, "expected_revision": expected_revision,
        }
        digest, replay = self._replay(idempotency_token, action_payload)
        if action in {"remove", "reset"} and replay is not None:
            return {**self._remove_response(replay, expected_revision), "idempotency_replayed": True}
        if replay is not None:
            return {**replay, "idempotency_replayed": True}
        record = self._record(workspace_id)
        if record.revision != expected_revision:
            raise WorkspaceRevisionConflict("revision conflict")
        principal = self._principal(record)
        if action in {"start", "retry"}:
            result = self.manager.start(principal, connector_id=record.connector_id)
        elif action == "stop":
            result = self.manager.stop(principal, connector_id=record.connector_id)
        elif action in {"pin", "unpin"}:
            result = self.manager.set_pinned(principal, action == "pin", connector_id=record.connector_id)
        elif action in {"remove", "reset"}:
            result = self.manager.remove(
                principal, connector_id=record.connector_id,
                expected_revision=expected_revision,
                idempotency_key=idempotency_token,
                idempotency_digest=digest,
                # An explicit Admin removal may clear a metadata row after
                # the broker independently proves both deterministic SDK
                # objects are absent. Ordinary retry never takes this path.
                allow_absent_cleanup=True,
            )
        elif action == "diagnostics":
            try:
                diagnostics = self.manager._runtime_call(record, "inspect", {})
            except WorkspaceError as exc:
                # Diagnostics remains useful while the broker is offline. Do
                # not echo SDK exception text or any guest/runtime payload.
                diagnostics = {"status": "unavailable", "error_code": exc.reason}
            except Exception:
                diagnostics = {"status": "unavailable", "error_code": "runtime_unavailable"}
            # Runtime inspection may persist its observed generation or clear
            # a stale expectation.  Re-read the exact ID after the probe so
            # the guarded revision belongs to the response we just produced.
            current = self._record(workspace_id)
            return {"revision": current.revision, "workspace": self._view(current),
                    "runtime": diagnostics}
        else:
            raise ValueError("unsupported Workspace action")
        current = self.manager.metadata.get(workspace_id)
        response = {
            "revision": current.revision if current else expected_revision + 1,
            "workspace": self._view(current) if current else None,
            "status": result.get("status", "success"),
        }
        if action in {"remove", "reset"}:
            # WorkspaceManager persisted this response's replay atomically
            # with the metadata deletion. Saving it again here would race
            # that owner and turn a successful remove into an HTTP 400.
            return response
        return self._save_replay(idempotency_token, digest, response)

    def bulk_workspace_action(self, action: Literal["remove", "reset"],
                              workspace_ids: Sequence[str], *,
                              expected_revisions: dict[str, int],
                              idempotency_token: str | None) -> dict[str, Any]:
        digest, replay = self._replay(idempotency_token, {
            "operation": "bulk_workspace_action", "action": action,
            "workspace_ids": list(workspace_ids), "expected_revisions": expected_revisions,
        })
        if replay is not None:
            return {**replay, "idempotency_replayed": True}
        records = [self._record(workspace_id) for workspace_id in workspace_ids]
        if any(row.revision != expected_revisions.get(row.workspace_id) for row in records):
            raise WorkspaceRevisionConflict("revision conflict")
        # Keep the legacy adapter method available for callers that have not
        # adopted the preview route, but still delegate each guarded removal to
        # WorkspaceManager rather than duplicating its lifecycle algorithm.
        removed: list[str] = []
        results: list[dict[str, Any]] = []
        for record in records:
            try:
                result = self.manager.remove(
                    self._principal(record), connector_id=record.connector_id,
                    expected_revision=record.revision,
                    idempotency_key=(
                        f"{idempotency_token}:{record.workspace_id}"
                        if idempotency_token else None
                    ),
                )
                removed.append(record.workspace_id)
                results.append({
                    "workspace_id": record.workspace_id, "status": "committed", "result": result,
                })
            except WorkspaceError as exc:
                results.append({
                    "workspace_id": record.workspace_id, "status": "failed", "reason": exc.reason,
                })
        response = {
            "revision": max(expected_revisions.values(), default=0) + 1,
            "removed": removed, "results": results,
            "status": "success" if len(removed) == len(records) else "partial",
        }
        return self._save_replay(idempotency_token, digest, response)

    def set_workspace_retention(self, workspace_id: str, *, expected_revision: int,
                                pinned: bool, retention_days: int | None,
                                idempotency_token: str | None) -> dict[str, Any]:
        digest, replay = self._replay(idempotency_token, {
            "operation": "workspace_retention", "workspace_id": workspace_id,
            "expected_revision": expected_revision, "pinned": pinned,
            "retention_days": retention_days,
        })
        if replay is not None:
            return {**replay, "idempotency_replayed": True}
        record = self._record(workspace_id)
        if record.revision != expected_revision:
            raise WorkspaceRevisionConflict("revision conflict")
        if not isinstance(pinned, bool):
            raise ValueError("pinned must be boolean")
        if retention_days is not None and (
            isinstance(retention_days, bool) or not isinstance(retention_days, int)
            or not 7 <= retention_days <= 3650
        ):
            raise ValueError("retention_days is outside the supported range")
        days = retention_days if retention_days is not None else (record.retention_days or 30)
        due = None if pinned else (
            _parse_time(record.last_accessed_at) + timedelta(days=days)
        ).isoformat(timespec="seconds")
        updated = self.manager.metadata.update(
            workspace_id, pinned=int(pinned), retention_days=days, deletion_due_at=due
        )
        return self._save_replay(idempotency_token, digest, {
            "revision": updated.revision, "workspace": self._view(updated),
        })

    def workspace_diagnostics(self, workspace_id: str) -> dict[str, Any]:
        record = self._record(workspace_id)
        try:
            runtime = self.manager._runtime_call(record, "inspect", {})
        except WorkspaceError as exc:
            runtime = {"status": "unavailable", "error_code": exc.reason}
        except Exception:
            runtime = {"status": "unavailable", "error_code": "runtime_unavailable"}
        # _runtime_call may persist a new runtime generation, including while
        # translating a handled probe error.  The Admin response and the next
        # guarded mutation must use one fresh persisted row from this exact ID.
        current = self._record(workspace_id)
        return {"revision": current.revision, "workspace": self._view(current), "runtime": runtime}

    def get_workspace_settings(self) -> dict[str, Any]:
        settings = self.manager.metadata.settings()
        settings["brave_configured"] = bool(
            self.trusted_secret_store is not None
            and self.trusted_secret_store.trusted_secret("brave-search-api-key")
        )
        return settings

    @staticmethod
    def _normalized_policy(values: dict[str, Any], current: dict[str, Any]) -> NetworkPolicy:
        mode = values.get("network_mode", current["network_mode"])
        raw_rules = values.get("network_rules", current["network_rules"])
        if not isinstance(raw_rules, list):
            raise ValueError("network_rules must be a list")
        rules: list[dict[str, Any]] = []
        for raw in raw_rules:
            if isinstance(raw, str):
                suffix = raw.startswith("*.")
                domain = raw[2:] if suffix else raw
                rules.append({
                    "domain": domain, "suffix": suffix, "ports": [80, 443],
                    "protocols": list(NETWORK_SCHEMES),
                })
            elif isinstance(raw, dict):
                normalized = dict(raw)
                # A new domain/port editor has no scheme selector.  Missing
                # protocols therefore mean both schemes; an explicit legacy
                # single-scheme field is preserved and remains unavailable.
                normalized.setdefault("protocols", list(NETWORK_SCHEMES))
                rules.append(normalized)
            else:
                raise ValueError("network rule is invalid")
        return NetworkPolicy.from_mapping({
            "mode": mode,
            "rules": rules,
            "explicit_confirmation": mode == "unrestricted_public",
        })

    def preview_workspace_settings(self, values: dict[str, Any]) -> dict[str, Any]:
        policy = self._normalized_policy(values, self.manager.metadata.settings())
        return {"status": "valid", "network_mode": policy.mode,
                "network_rules": [
                    _network_rule_view(rule.to_mapping())
                    for rule in policy.rules
                ],
                "brave_enabled": bool(values.get("brave_enabled", False))}

    def update_workspace_settings(self, values: dict[str, Any], *, expected_revision: int,
                                  confirm_high_trust: bool,
                                  idempotency_token: str | None) -> dict[str, Any]:
        if not confirm_high_trust:
            raise ValueError("high-trust confirmation is required")
        digest, replay = self._replay(idempotency_token, {
            "operation": "workspace_settings", "expected_revision": expected_revision,
            "values": values,
        })
        if replay is not None:
            return {**replay, "idempotency_replayed": True}
        normalized = dict(values)
        api_key = normalized.pop("brave_api_key", None)
        integer_bounds = {
            "retention_days": (7, 3650),
            "quota_bytes": (1024**3, 1024**4),
            "idle_stop_seconds": (60, 86400),
            "host_reserve_bytes": (1024**3, 1024**5),
            "warning_threshold_percent": (1, 100),
            "max_running_workspaces": (1, 64),
        }
        for key, (low, high) in integer_bounds.items():
            if key in normalized and (
                isinstance(normalized[key], bool) or not isinstance(normalized[key], int)
                or not low <= normalized[key] <= high
            ):
                raise ValueError(f"{key} is outside the supported range")
        with self._settings_lock:
            current = self.manager.metadata.settings()
            if current["revision"] != expected_revision:
                raise WorkspaceRevisionConflict("revision conflict")
            policy = self._normalized_policy(values, current)
            normalized["network_mode"] = policy.mode
            normalized["network_rules"] = [
                rule.to_mapping()
                for rule in policy.rules
            ]
            if api_key is not None:
                if self.trusted_secret_store is None:
                    raise WorkspaceUnavailable("trusted secret storage is unavailable")
                self.trusted_secret_store.store_trusted_secret("brave-search-api-key", api_key)
            updated = self.manager.metadata.update_settings(expected_revision, normalized)
            self._apply_settings(updated)
            secret = None
            if self.trusted_secret_store is not None:
                secret = self.trusted_secret_store.trusted_secret("brave-search-api-key")
            if self.brave_search is not None:
                if secret:
                    self.brave_search.configure(secret, enabled=updated["brave_enabled"])
                else:
                    self.brave_search.disable()
            updated["brave_configured"] = bool(secret)
            return self._save_replay(idempotency_token, digest, updated)

    def test_brave_search(self) -> dict[str, Any]:
        if self.brave_search is None:
            return {"ok": False, "category": "not_configured"}
        result = self.brave_search.search("Cognita connectivity test", count=1)
        return {"ok": result.get("status") == "success",
                "category": result.get("reason", result.get("status", "unknown"))}
