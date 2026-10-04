from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
from threading import Barrier
from uuid import UUID

import httpx
import pytest

from cognita.workspace import (
    BrokerRuntimeClient,
    WorkspaceError,
    WorkspaceManager,
    WorkspaceMetadataStore,
    _digest,
    _stable_broker_request_id,
)
from cognita.workspace_admin import WorkspaceAdminAdapter, WorkspaceNotFound, WorkspaceRevisionConflict


PRINCIPAL = "11111111-1111-4111-8111-111111111111"


class Runtime:
    def __init__(self, inspect=None):
        self.inspect_result = inspect or {
            "workspace_id": "unused", "allocated_bytes": 0, "apparent_bytes": 0,
        }
        self.calls = []
        self.job_state = "running"
        # The broker supplies a complete fs_usage result separately from its
        # inspect response. Lifecycle tests that expect a fresh measurement
        # need that explicit evidence, including the configured path case.
        self.fs_usage_result = {
            "entries": [], "truncated": False,
            "total_bytes": self.inspect_result.get("apparent_bytes", 0),
        }

    def call(self, workspace_id, operation, arguments, **kwargs):
        self.calls.append((workspace_id, operation, arguments))
        if operation == "ensure":
            result = {"allocated_bytes": 0, "apparent_bytes": 0, "volume_name": "vol-1"}
            # The broker's ensure response is the evidence used by lifecycle
            # admission.  Include the explicit path only for this fixture when
            # the fake broker was configured with one; the production manager
            # still verifies containment before persisting it.
            if isinstance(self.inspect_result.get("host_path"), str):
                result["host_path"] = self.inspect_result["host_path"]
            return result
        if operation == "inspect":
            result = dict(self.inspect_result)
            result.setdefault("state", "running")
            if result.get("workspace_id") == "unused":
                result["workspace_id"] = workspace_id
            return result
        if operation == "job_get":
            return {"state": self.job_state}
        if operation == "fs_usage":
            return dict(self.fs_usage_result)
        return {"ok": True}


class MeasurementUnavailableRuntime(Runtime):
    def call(self, workspace_id, operation, arguments, **kwargs):
        if operation == "inspect":
            raise WorkspaceError("runtime_unavailable", "inspect unavailable")
        return super().call(workspace_id, operation, arguments, **kwargs)


def _principal():
    return type("P", (), {"principal_id": PRINCIPAL})()


def _sdk_shaped_broker_client(
    workspace_id: str, *, sandbox_name: str | None = None,
    runtime_alias: str | None = None, include_workspace_id: bool = False,
    include_volume_name: bool = True,
):
    runtime_name = f"cognita-ws-{workspace_id}"
    volume_name = f"cognita-ws-data-{workspace_id}"
    sandbox_name = sandbox_name or runtime_name
    calls = []

    def handler(request: httpx.Request):
        body = request.read()
        payload = json.loads(body)
        operation = payload["operation"]
        calls.append(operation)
        if operation == "inspect":
            data = {
                "state": "partial", "partial_state": "volume_only",
                "sandbox_name": sandbox_name,
                # SDK 0.7 does not guarantee a host backing path.  The exact
                # SDK identities still prove ownership for broker removal.
                "host_path": None, "path_status": "not_reported",
                "measured_apparent_bytes": 0,
            }
            if include_volume_name:
                data["volume_name"] = volume_name
            if runtime_alias is not None:
                data["runtime_name"] = runtime_alias
            if include_workspace_id:
                data["workspace_id"] = workspace_id
        elif operation == "ensure":
            data = {
                "state": "running", "sandbox_name": runtime_name,
                "volume_name": volume_name, "measured_apparent_bytes": 0,
            }
        elif operation == "remove":
            data = {"state": "absent", "sandbox_name": runtime_name, "volume_name": volume_name}
        elif operation == "stop":
            data = {"state": "stopped", "sandbox_name": runtime_name, "volume_name": volume_name}
        else:
            data = {}
        return httpx.Response(200, json={"request_id": payload["request_id"], "data": data})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    return BrokerRuntimeClient("http://broker", "test-secret", client=client), calls


def test_unknown_measurements_are_not_projected_as_zero(tmp_path: Path):
    store = WorkspaceMetadataStore(tmp_path / "workspace.sqlite3")
    try:
        row = store.create(PRINCIPAL, None, "Workspace", now="2026-09-19T00:00:00+00:00", quota_bytes=4, retention_days=30)
        assert row.measured_allocated_bytes is None
        assert row.measured_apparent_bytes is None
        assert row.usage_status == "unknown"
    finally:
        store.close()


def test_capacity_provider_uses_reserve_and_atomic_growth(tmp_path: Path):
    store = WorkspaceMetadataStore(tmp_path / "workspace.sqlite3")
    try:
        manager = WorkspaceManager(
            store, Runtime(), capacity_provider=type("Capacity", (), {"root": "/workspace", "snapshot": lambda self: (1000, 500)})(),
            strict_capacity=True, host_reserve_bytes=100,
        )
        assert manager.host_admission(additional_growth_bytes=399) is True
        assert manager.host_admission(additional_growth_bytes=401) is False
        token = store.reserve_growth(200, free_bytes_supplier=lambda: 500, reserve_bytes=100)
        with pytest.raises(WorkspaceError) as error:
            store.reserve_growth(201, free_bytes_supplier=lambda: 500, reserve_bytes=100)
        assert error.value.reason == "capacity_busy"
        store.release_growth(token)
    finally:
        store.close()


def test_r5_admission_reserves_separate_workspace_and_root_growth(tmp_path: Path):
    store = WorkspaceMetadataStore(tmp_path / "workspace.sqlite3")
    runtime = Runtime()
    try:
        manager = WorkspaceManager(store, runtime)
        assert manager._potential_growth(4 * 1024**3) == 8 * 1024**3
        manager.execute(_principal(), "workspace_write_file", {"path": "a", "text": "x"})
        ensure = next(arguments for _workspace, operation, arguments in runtime.calls if operation == "ensure")
        assert ensure["create_volume_if_absent"] is True
        assert ensure["quota_bytes"] == 4 * 1024**3
        assert ensure["root_quota_bytes"] == 4 * 1024**3
        assert ensure["require_writable_root_quota"] is True
        assert ensure["vcpus"] == 4
        assert ensure["memory_bytes"] == 8 * 1024**3
    finally:
        store.close()


def test_capacity_uses_historical_apparent_bytes_only_when_fresh(tmp_path: Path):
    store = WorkspaceMetadataStore(tmp_path / "workspace.sqlite3")
    try:
        manager = WorkspaceManager(store, Runtime(), strict_capacity=False)
        row = store.create(PRINCIPAL, None, "Workspace", now="2026-09-19T00:00:00+00:00",
                           quota_bytes=100, retention_days=30)
        store.update(row.workspace_id, state="running", desired_state="running",
                     measured_apparent_bytes=40, usage_status="fresh")
        assert manager._active_potential_growth() == manager.root_quota_bytes + 60
        store.update(row.workspace_id, usage_status="unknown")
        assert manager._active_potential_growth() == manager.root_quota_bytes + 100

        reservations = []

        def reserve(growth, **_kwargs):
            reservations.append(growth)
            return None

        manager._reserve_admission = reserve
        for status, expected in (("unknown", 100), ("fresh", 60)):
            store.update(row.workspace_id, state="stopped", desired_state="stopped",
                         usage_status=status)
            manager._ensure(PRINCIPAL, None)
            assert reservations[-1] == manager.root_quota_bytes + expected

        # Recovery has a separate admission branch for a row still marked
        # running while its owned runtime was observed stopped.
        manager._inspect_reconciled = lambda current: ({"state": "stopped"}, current)
        manager._running_recovery_proven = lambda _current, _observed: True
        for status, expected in (("unknown", 100), ("fresh", 60)):
            store.update(row.workspace_id, state="running", desired_state="running",
                         usage_status=status)
            manager._ensure(PRINCIPAL, None)
            assert reservations[-1] == manager.root_quota_bytes + expected
    finally:
        store.close()


def test_bridge_admission_accounts_for_active_guest_root_growth(tmp_path: Path):
    store = WorkspaceMetadataStore(tmp_path / "workspace.sqlite3")
    try:
        capacity = type(
            "Capacity", (),
            {"root": "/workspace", "snapshot": lambda self: (20 * 1024**3, 10 * 1024**3)},
        )()
        manager = WorkspaceManager(
            store, Runtime(), capacity_provider=capacity,
            strict_capacity=True, host_reserve_bytes=0,
        )
        row = store.create(
            PRINCIPAL, None, "Workspace", now="2026-09-19T00:00:00+00:00",
            quota_bytes=4 * 1024**3, retention_days=30,
        )
        store.update(row.workspace_id, state="running", desired_state="running")
        # 10 GiB free - 2 GiB reserve leaves 8 GiB; the active guest's
        # unmeasured 4+4 GiB potential leaves no room for a 1 GiB bridge.
        assert manager.host_admission(additional_growth_bytes=1024**3) is False
    finally:
        store.close()


def test_bridge_hold_does_not_hide_uncovered_active_growth(tmp_path: Path):
    store = WorkspaceMetadataStore(tmp_path / "workspace.sqlite3")
    try:
        capacity = type(
            "Capacity", (),
            {"root": "/workspace", "snapshot": lambda self: (20 * 1024**3, 10 * 1024**3)},
        )()
        manager = WorkspaceManager(
            store, Runtime(), capacity_provider=capacity,
            strict_capacity=True, host_reserve_bytes=0,
        )
        row = store.create(
            PRINCIPAL, None, "Workspace", now="2026-09-19T00:00:00+00:00",
            quota_bytes=4 * 1024**3, retention_days=30,
        )
        store.update(row.workspace_id, state="running", desired_state="running")
        token = store.reserve_growth(
            1 * 1024**3, free_bytes_supplier=lambda: 10 * 1024**3,
            reserve_bytes=2 * 1024**3,
        )
        assert manager.host_admission() is False
        store.release_growth(token)
    finally:
        store.close()


def test_bridge_refreshes_active_coverage_after_start_hold_releases(tmp_path: Path):
    store = WorkspaceMetadataStore(tmp_path / "workspace.sqlite3")
    try:
        capacity = type(
            "Capacity", (),
            {"root": "/workspace", "snapshot": lambda self: (40 * 1024**3, 40 * 1024**3)},
        )()
        manager = WorkspaceManager(
            store, Runtime(), capacity_provider=capacity,
            strict_capacity=True, host_reserve_bytes=0,
        )
        active = [8 * 1024**3]
        manager._active_potential_growth = lambda: active[0]
        first = manager._reserve_bridge_growth(1 * 1024**3, request_key="bridge:first")
        start = manager._reserve_admission(
            16 * 1024**3, request_key="start:guest-b",
            covered_active_growth_bytes=active[0],
        )
        assert first and start
        store.release_growth(start)
        active[0] = 16 * 1024**3
        second = manager._reserve_bridge_growth(1 * 1024**3, request_key="bridge:second")
        assert second
        assert store.held_growth_bytes() == 26 * 1024**3
        store.release_growth(first)
        assert manager.host_admission(additional_growth_bytes=19 * 1024**3) is True
        assert manager.host_admission(additional_growth_bytes=20 * 1024**3) is False
        store.release_growth(second)
    finally:
        store.close()


def test_overlapping_bridge_holds_serialize_fresh_active_baselines(tmp_path: Path):
    store = WorkspaceMetadataStore(tmp_path / "workspace.sqlite3")
    try:
        capacity = type(
            "Capacity", (),
            {"root": "/workspace", "snapshot": lambda self: (20 * 1024**3, 20 * 1024**3)},
        )()
        manager = WorkspaceManager(
            store, Runtime(), capacity_provider=capacity,
            strict_capacity=True, host_reserve_bytes=0,
        )
        manager._active_potential_growth = lambda: 8 * 1024**3
        with ThreadPoolExecutor(max_workers=2) as executor:
            tokens = list(executor.map(
                lambda key: manager._reserve_bridge_growth(
                    1 * 1024**3, request_key=f"bridge:{key}"
                ),
                ("first", "second"),
            ))
        assert all(tokens)
        # Each bridge hold carries a fresh active baseline. SQLite's immediate
        # transaction serializes the two admissions; both fit exactly below
        # the 2 GiB host reserve floor (20 - 2 = 18 GiB).
        assert store.held_growth_bytes() == 18 * 1024**3
        for token in tokens:
            store.release_growth(token)
    finally:
        store.close()


def test_storage_snapshot_counts_only_uncovered_active_growth(tmp_path: Path):
    store = WorkspaceMetadataStore(tmp_path / "workspace.sqlite3")
    try:
        capacity = type(
            "Capacity", (),
            {"root": "/workspace", "snapshot": lambda self: (20 * 1024**3, 20 * 1024**3)},
        )()
        manager = WorkspaceManager(
            store, Runtime(), capacity_provider=capacity,
            strict_capacity=True, host_reserve_bytes=0,
        )
        active = [8 * 1024**3]
        manager._active_potential_growth = lambda: active[0]
        token = store.reserve_growth(
            9 * 1024**3,
            free_bytes_supplier=lambda: 20 * 1024**3,
            reserve_bytes=2 * 1024**3,
            covered_active_growth_bytes=active[0],
        )
        # The held row already carries the 8 GiB active baseline; counting it
        # again in the summary would understate admissible capacity.
        assert manager.storage_snapshot().admissible_free_bytes == 9 * 1024**3
        active[0] = 16 * 1024**3
        assert manager.storage_snapshot().admissible_free_bytes == 1 * 1024**3
        store.release_growth(token)
    finally:
        store.close()


def test_growth_reservation_renewal_does_not_revive_expired_hold(tmp_path: Path):
    store = WorkspaceMetadataStore(tmp_path / "workspace.sqlite3")
    try:
        token = store.reserve_growth(
            200,
            free_bytes_supplier=lambda: 500,
            reserve_bytes=100,
            ttl_seconds=60,
        )
        store.renew_growth(token, ttl_seconds=60)
        assert store.held_growth_bytes() == 200
        with store.transaction() as db:
            db.execute(
                "UPDATE workspace_growth_reservations SET expires_at=? WHERE reservation_id=?",
                ("2000-01-01T00:00:00+00:00", token),
            )
        with pytest.raises(WorkspaceError) as error:
            store.renew_growth(token, ttl_seconds=60)
        assert error.value.reason == "capacity_busy"
    finally:
        store.close()


def test_runtime_measurement_accepts_verified_path_only(tmp_path: Path):
    store = WorkspaceMetadataStore(tmp_path / "workspace.sqlite3")
    try:
        runtime = Runtime({"workspace_id": "unused", "allocated_bytes": 9, "apparent_bytes": 11, "host_path": str(tmp_path / "owned"), "volume_name": "vol-1"})
        manager = WorkspaceManager(store, runtime, host_root=str(tmp_path), strict_capacity=False)
        runtime.inspect_result["workspace_id"] = "unused"
        # A direct metadata refresh cannot invent a row; first-use creation is
        # still the only operation that admits a Workspace.
        manager.execute(type("P", (), {"principal_id": PRINCIPAL})(), "workspace_write_file", {"path": "a", "text": "x"})
        row = store.get_by_principal(PRINCIPAL)
        assert row is not None and row.usage_status == "fresh"
        assert row.host_path == str(tmp_path / "owned")
        assert row.path_status == "verified"
    finally:
        store.close()


def test_preview_rejects_stale_revision(tmp_path: Path):
    store = WorkspaceMetadataStore(tmp_path / "workspace.sqlite3")
    try:
        manager = WorkspaceManager(store, Runtime())
        manager.execute(type("P", (), {"principal_id": PRINCIPAL})(), "workspace_write_file", {"path": "a", "text": "x"})
        row = store.get_by_principal(PRINCIPAL)
        assert row is not None
        preview = manager.preview_bulk_delete([row.workspace_id], {row.workspace_id: row.revision})
        assert preview["token"] and preview["targets"][0]["usage_status"] == "fresh"
        store.update(row.workspace_id, owner_status="revoked")
        with pytest.raises(WorkspaceError) as error:
            manager.preview_bulk_delete([row.workspace_id], {row.workspace_id: row.revision})
        assert error.value.reason == "path_conflict"
    finally:
        store.close()


def test_remove_fails_closed_on_unproven_identity(tmp_path: Path):
    store = WorkspaceMetadataStore(tmp_path / "workspace.sqlite3")
    runtime = Runtime({"workspace_id": "not-the-workspace"})
    try:
        manager = WorkspaceManager(store, runtime)
        manager.execute(_principal(), "workspace_write_file", {"path": "a", "text": "x"})
        row = store.get_by_principal(PRINCIPAL)
        assert row is not None
        with pytest.raises(WorkspaceError) as error:
            manager.remove(_principal(), expected_revision=row.revision, idempotency_key="remove-1")
        assert error.value.reason == "ownership_unproven"
        assert store.get(row.workspace_id) is not None
    finally:
        store.close()


def test_retry_accepts_exact_sdk_sandbox_name_for_volume_only_residual(tmp_path: Path):
    store = WorkspaceMetadataStore(tmp_path / "workspace.sqlite3")
    workspace_id = None
    runtime = None
    try:
        row = store.create(PRINCIPAL, None, "Workspace", now="2026-09-19T00:00:00+00:00", quota_bytes=4, retention_days=30)
        workspace_id = row.workspace_id
        store.update(
            workspace_id, state="failed", desired_state="running",
            volume_name=f"cognita-ws-data-{workspace_id}",
        )
        runtime, calls = _sdk_shaped_broker_client(workspace_id)
        manager = WorkspaceManager(store, runtime)
        result = manager.start(_principal())
        assert result["status"] == "success"
        assert store.get(workspace_id).state == "running"
        assert calls == ["inspect", "ensure"]
    finally:
        if runtime is not None:
            runtime._client.close()
        store.close()


@pytest.mark.parametrize(
    "observed",
    [
        {
            "state": "failed",
            "sandbox_name": f"cognita-ws-{PRINCIPAL}",
            "volume_name": f"cognita-ws-data-{PRINCIPAL}",
        },
        {
            "state": "partial",
            "partial_state": "volume_only",
            "sandbox_name": f"cognita-ws-{PRINCIPAL}",
            "volume_name": f"cognita-ws-data-{PRINCIPAL}",
        },
    ],
)
def test_failed_workspace_ordinary_admission_repairs_only_after_exact_runtime_proof(tmp_path: Path, observed):
    store = WorkspaceMetadataStore(tmp_path / "workspace.sqlite3")

    class Runtime:
        def __init__(self):
            self.calls = []
            self.ensure_args = None

        def call(self, workspace_id, operation, arguments, **kwargs):
            self.calls.append(operation)
            if operation == "inspect":
                return {**observed, "sandbox_name": f"cognita-ws-{workspace_id}",
                        "volume_name": f"cognita-ws-data-{workspace_id}"}
            if operation == "ensure":
                self.ensure_args = arguments
                return {"state": "running", "sandbox_name": f"cognita-ws-{workspace_id}",
                        "volume_name": f"cognita-ws-data-{workspace_id}",
                        "measured_apparent_bytes": 0}
            raise AssertionError(f"unexpected operation: {operation}")

    try:
        row = store.create(PRINCIPAL, None, "Workspace", now="2026-09-19T00:00:00+00:00", quota_bytes=4, retention_days=30)
        store.update(row.workspace_id, state="failed", desired_state="running",
                     volume_name=f"cognita-ws-data-{row.workspace_id}")
        runtime = Runtime()
        manager = WorkspaceManager(store, runtime)

        admitted = manager._admit(_principal())

        assert admitted.state == "running"
        assert runtime.calls == ["inspect", "ensure"]
        assert runtime.ensure_args["create_volume_if_absent"] is False
    finally:
        store.close()


@pytest.mark.parametrize(
    "observed",
    [
        {"state": "absent", "sandbox_name": "cognita-ws-foreign", "volume_name": "other"},
        {"state": "partial", "partial_state": "volume_only", "sandbox_name": "cognita-ws-foreign",
         "volume_name": f"cognita-ws-data-{PRINCIPAL}"},
        {"state": "partial", "partial_state": "volume_only",
         "sandbox_name": f"cognita-ws-{PRINCIPAL}"},
    ],
)
def test_failed_workspace_denies_admission_without_exact_runtime_proof(tmp_path: Path, observed):
    store = WorkspaceMetadataStore(tmp_path / "workspace.sqlite3")

    class Runtime:
        def __init__(self):
            self.calls = []

        def call(self, _workspace_id, operation, _arguments, **kwargs):
            self.calls.append(operation)
            assert operation == "inspect"
            return observed

    try:
        row = store.create(PRINCIPAL, None, "Workspace", now="2026-09-19T00:00:00+00:00", quota_bytes=4, retention_days=30)
        store.update(row.workspace_id, state="failed", desired_state="running",
                     volume_name=f"cognita-ws-data-{row.workspace_id}")
        runtime = Runtime()
        manager = WorkspaceManager(store, runtime)

        with pytest.raises(WorkspaceError) as error:
            manager._admit(_principal())

        assert error.value.reason == "retry_requires_ownership"
        assert runtime.calls == ["inspect"]
        assert store.get(row.workspace_id).state == "failed"
    finally:
        store.close()


def test_running_workspace_recreates_absent_sandbox_without_volume_creation(tmp_path: Path):
    store = WorkspaceMetadataStore(tmp_path / "workspace.sqlite3")
    try:
        row = store.create(PRINCIPAL, None, "Workspace", now="2026-09-19T00:00:00+00:00", quota_bytes=4, retention_days=30)
        volume_name = f"cognita-ws-data-{row.workspace_id}"
        sandbox_name = f"cognita-ws-{row.workspace_id}"
        store.update(row.workspace_id, state="running", desired_state="running", volume_name=volume_name)

        class Runtime:
            def __init__(self):
                self.calls = []
                self.ensure_arguments = None

            def call(self, workspace_id, operation, arguments, **_kwargs):
                self.calls.append(operation)
                if operation == "inspect":
                    return {
                        "state": "absent", "sandbox_name": sandbox_name,
                        "volume_name": volume_name, "runtime_id": None, "volume_id": None,
                    }
                if operation == "ensure":
                    self.ensure_arguments = arguments
                    return {
                        "state": "running", "sandbox_name": sandbox_name,
                        "volume_name": volume_name, "measured_apparent_bytes": 45,
                    }
                raise AssertionError(f"unexpected operation: {operation}")

        runtime = Runtime()
        manager = WorkspaceManager(store, runtime)
        admitted = manager._admit(_principal())

        assert admitted.state == "running"
        assert runtime.calls == ["inspect", "ensure"]
        assert runtime.ensure_arguments["create_volume_if_absent"] is False
    finally:
        store.close()


def test_running_workspace_missing_volume_fails_closed_without_creating_one(tmp_path: Path):
    store = WorkspaceMetadataStore(tmp_path / "workspace.sqlite3")
    try:
        row = store.create(PRINCIPAL, None, "Workspace", now="2026-09-19T00:00:00+00:00", quota_bytes=4, retention_days=30)
        volume_name = f"cognita-ws-data-{row.workspace_id}"
        sandbox_name = f"cognita-ws-{row.workspace_id}"
        store.update(row.workspace_id, state="running", desired_state="running", volume_name=volume_name)

        class Runtime:
            def __init__(self):
                self.calls = []
                self.ensure_arguments = None

            def call(self, workspace_id, operation, arguments, **_kwargs):
                self.calls.append(operation)
                if operation == "inspect":
                    return {
                        "state": "absent", "sandbox_name": sandbox_name,
                        "volume_name": volume_name, "runtime_id": None, "volume_id": None,
                    }
                if operation == "ensure":
                    self.ensure_arguments = arguments
                    raise WorkspaceError("path_unavailable", "named volume is missing")
                raise AssertionError(f"unexpected operation: {operation}")

        runtime = Runtime()
        manager = WorkspaceManager(store, runtime)
        with pytest.raises(WorkspaceError) as error:
            manager._admit(_principal())

        assert error.value.reason == "path_unavailable"
        assert runtime.calls == ["inspect", "ensure"]
        assert runtime.ensure_arguments["create_volume_if_absent"] is False
        assert store.get(row.workspace_id).state == "failed"
    finally:
        store.close()


def test_public_start_recovers_crashed_running_runtime_in_place(tmp_path: Path):
    store = WorkspaceMetadataStore(tmp_path / "workspace.sqlite3")

    class CrashedRuntime:
        def __init__(self):
            self.calls = []

        def call(self, workspace_id, operation, arguments, **kwargs):
            self.calls.append(operation)
            row = store.get(workspace_id)
            assert row is not None
            if operation == "inspect":
                return {
                    "state": "failed",
                    "sandbox_name": row.runtime_name,
                    "volume_name": row.volume_name,
                    "runtime_id": "sandbox-id",
                    "volume_id": "volume-id",
                    "measured_apparent_bytes": 12,
                }
            if operation == "start":
                return {
                    "state": "running",
                    "sandbox_name": row.runtime_name,
                    "volume_name": row.volume_name,
                    "runtime_id": "sandbox-id",
                    "volume_id": "volume-id",
                    "measured_apparent_bytes": 12,
                }
            raise AssertionError(f"unexpected runtime operation: {operation}")

    runtime = CrashedRuntime()
    try:
        row = store.create(
            PRINCIPAL, None, "Workspace", now="2026-09-19T00:00:00+00:00",
            quota_bytes=4, retention_days=30,
        )
        volume_name = f"cognita-ws-data-{row.workspace_id}"
        store.update(row.workspace_id, state="running", desired_state="running", volume_name=volume_name)
        manager = WorkspaceManager(store, runtime)

        result = manager.start(_principal())

        assert result["status"] == "success"
        assert store.get(row.workspace_id).state == "running"
        assert runtime.calls == ["inspect", "start"]
    finally:
        store.close()


def test_conflicting_sdk_sandbox_name_overrides_legacy_workspace_id(tmp_path: Path):
    store = WorkspaceMetadataStore(tmp_path / "workspace.sqlite3")
    runtime = None
    try:
        row = store.create(PRINCIPAL, None, "Workspace", now="2026-09-19T00:00:00+00:00", quota_bytes=4, retention_days=30)
        store.update(row.workspace_id, state="stopped", desired_state="stopped", volume_name=f"cognita-ws-data-{row.workspace_id}")
        runtime, calls = _sdk_shaped_broker_client(
            row.workspace_id, sandbox_name="cognita-ws-foreign", include_workspace_id=True,
        )
        manager = WorkspaceManager(store, runtime)
        with pytest.raises(WorkspaceError) as error:
            manager.remove(_principal(), expected_revision=store.get(row.workspace_id).revision, allow_absent_cleanup=True)
        assert error.value.reason == "ownership_unproven"
        assert calls == ["inspect"]
        assert store.get(row.workspace_id) is not None
    finally:
        if runtime is not None:
            runtime._client.close()
        store.close()


def test_conflicting_legacy_runtime_alias_is_not_hidden_by_sdk_name(tmp_path: Path):
    store = WorkspaceMetadataStore(tmp_path / "workspace.sqlite3")
    runtime = None
    try:
        row = store.create(PRINCIPAL, None, "Workspace", now="2026-09-19T00:00:00+00:00", quota_bytes=4, retention_days=30)
        store.update(row.workspace_id, state="stopped", desired_state="stopped", volume_name=f"cognita-ws-data-{row.workspace_id}")
        runtime, calls = _sdk_shaped_broker_client(
            row.workspace_id, runtime_alias="cognita-ws-foreign",
        )
        manager = WorkspaceManager(store, runtime)
        with pytest.raises(WorkspaceError) as error:
            manager.remove(_principal(), expected_revision=store.get(row.workspace_id).revision, allow_absent_cleanup=True)
        assert error.value.reason == "ownership_unproven"
        assert calls == ["inspect"]
    finally:
        if runtime is not None:
            runtime._client.close()
        store.close()


def test_volume_only_state_requires_nonempty_saved_and_observed_volume_identity(tmp_path: Path):
    store = WorkspaceMetadataStore(tmp_path / "workspace.sqlite3")
    runtime = None
    try:
        row = store.create(PRINCIPAL, None, "Workspace", now="2026-09-19T00:00:00+00:00", quota_bytes=4, retention_days=30)
        store.update(row.workspace_id, state="stopped", desired_state="stopped", volume_name=f"cognita-ws-data-{row.workspace_id}")
        runtime, calls = _sdk_shaped_broker_client(row.workspace_id, include_volume_name=False)
        manager = WorkspaceManager(store, runtime)
        with pytest.raises(WorkspaceError) as error:
            manager.remove(_principal(), expected_revision=store.get(row.workspace_id).revision, allow_absent_cleanup=True)
        assert error.value.reason == "ownership_unproven"
        assert calls == ["inspect"]
    finally:
        if runtime is not None:
            runtime._client.close()
        store.close()


def test_metadata_only_cleanup_requires_complete_both_object_absence_proof(tmp_path: Path):
    store = WorkspaceMetadataStore(tmp_path / "workspace.sqlite3")
    try:
        row = store.create(PRINCIPAL, None, "Workspace", now="2026-09-19T00:00:00+00:00", quota_bytes=4, retention_days=30)
        volume_name = f"cognita-ws-data-{row.workspace_id}"
        store.update(row.workspace_id, state="stopped", desired_state="stopped", volume_name=volume_name)

        class NakedAbsentRuntime:
            def call(self, _workspace_id, operation, _arguments, **_kwargs):
                assert operation == "inspect"
                return {"state": "absent"}

        manager = WorkspaceManager(store, NakedAbsentRuntime())
        with pytest.raises(WorkspaceError) as error:
            manager.remove(_principal(), expected_revision=store.get(row.workspace_id).revision, allow_absent_cleanup=True)
        assert error.value.reason == "ownership_unproven"
        assert store.get(row.workspace_id) is not None
    finally:
        store.close()


def test_complete_both_object_absence_proof_allows_explicit_metadata_cleanup(tmp_path: Path):
    store = WorkspaceMetadataStore(tmp_path / "workspace.sqlite3")
    try:
        row = store.create(PRINCIPAL, None, "Workspace", now="2026-09-19T00:00:00+00:00", quota_bytes=4, retention_days=30)
        volume_name = f"cognita-ws-data-{row.workspace_id}"
        store.update(row.workspace_id, state="stopped", desired_state="stopped", volume_name=volume_name)

        class CompleteAbsentRuntime:
            def call(self, workspace_id, operation, _arguments, **_kwargs):
                assert operation == "inspect"
                return {
                    "state": "absent", "sandbox_name": f"cognita-ws-{workspace_id}",
                    "volume_name": volume_name, "runtime_id": None, "volume_id": None,
                    "host_path": None, "path_status": "absent",
                }

        manager = WorkspaceManager(store, CompleteAbsentRuntime())
        result = manager.remove(_principal(), expected_revision=store.get(row.workspace_id).revision, allow_absent_cleanup=True)
        assert result["status"] == "success"
        assert store.get(row.workspace_id) is None
    finally:
        store.close()


def test_admin_remove_cleans_failed_first_use_row_after_exact_absence_proof(tmp_path: Path):
    """Admin repair may remove a row whose first-use volume was never saved."""
    store = WorkspaceMetadataStore(tmp_path / "workspace.sqlite3")
    try:
        row = store.create(
            PRINCIPAL, None, "Workspace", now="2026-09-19T00:00:00+00:00",
            quota_bytes=4, retention_days=30,
        )
        row = store.update(row.workspace_id, state="failed", desired_state="running")

        class ExactAbsentRuntime:
            def __init__(self):
                self.calls = []

            def call(self, workspace_id, operation, _arguments, **_kwargs):
                self.calls.append(operation)
                assert operation == "inspect"
                return {
                    "state": "absent",
                    "sandbox_name": f"cognita-ws-{workspace_id}",
                    "volume_name": f"cognita-ws-data-{workspace_id}",
                    "runtime_id": None,
                    "volume_id": None,
                }

        runtime = ExactAbsentRuntime()
        adapter = WorkspaceAdminAdapter(WorkspaceManager(store, runtime))
        result = adapter.workspace_action(
            "remove", row.workspace_id, expected_revision=row.revision,
            idempotency_token="admin-remove-failed-first-use",
        )

        assert result == {
            "revision": row.revision + 1, "workspace": None, "status": "success",
        }
        assert store.get(row.workspace_id) is None
        assert runtime.calls == ["inspect"]
    finally:
        store.close()


def test_workspace_diagnostics_uses_post_probe_revision_for_guarded_remove(tmp_path: Path):
    store = WorkspaceMetadataStore(tmp_path / "workspace.sqlite3")
    try:
        row = store.create(
            PRINCIPAL, None, "Workspace", now="2026-09-19T00:00:00+00:00",
            quota_bytes=4, retention_days=30,
        )
        row = store.update(row.workspace_id, state="failed", desired_state="running")

        class Runtime:
            def __init__(self):
                self.inspect_calls = 0

            def call(self, workspace_id, operation, _arguments, **_kwargs):
                assert operation == "inspect"
                self.inspect_calls += 1
                result = {
                    "state": "absent",
                    "sandbox_name": f"cognita-ws-{workspace_id}",
                    "volume_name": f"cognita-ws-data-{workspace_id}",
                    "runtime_id": None,
                    "volume_id": None,
                }
                if self.inspect_calls == 1:
                    result["_runtime_generation"] = 8
                return result

        runtime = Runtime()
        adapter = WorkspaceAdminAdapter(WorkspaceManager(store, runtime))
        diagnostics = adapter.workspace_diagnostics(row.workspace_id)
        current = store.get(row.workspace_id)

        assert current is not None
        assert diagnostics["revision"] == current.revision == row.revision + 1
        assert diagnostics["workspace"]["workspace_id"] == row.workspace_id
        assert diagnostics["workspace"]["revision"] == current.revision
        assert diagnostics["workspace"]["runtime_generation"] == 8

        removed = adapter.workspace_action(
            "remove", row.workspace_id, expected_revision=diagnostics["revision"],
            idempotency_token="admin-remove-after-diagnostics",
        )
        assert removed["status"] == "success"
        assert store.get(row.workspace_id) is None
        assert runtime.inspect_calls == 2
    finally:
        store.close()


def test_intervening_metadata_mutation_rejects_diagnostics_revision_without_delete(tmp_path: Path):
    store = WorkspaceMetadataStore(tmp_path / "workspace.sqlite3")
    try:
        row = store.create(
            PRINCIPAL, None, "Workspace", now="2026-09-19T00:00:00+00:00",
            quota_bytes=4, retention_days=30,
        )
        row = store.update(row.workspace_id, state="failed", desired_state="running")

        class Runtime:
            def call(self, workspace_id, operation, _arguments, **_kwargs):
                assert operation == "inspect"
                return {
                    "state": "absent",
                    "sandbox_name": f"cognita-ws-{workspace_id}",
                    "volume_name": f"cognita-ws-data-{workspace_id}",
                    "runtime_id": None,
                    "volume_id": None,
                }

        adapter = WorkspaceAdminAdapter(WorkspaceManager(store, Runtime()))
        diagnostics = adapter.workspace_diagnostics(row.workspace_id)
        store.update(row.workspace_id, last_error_code="intervening_mutation")

        with pytest.raises(WorkspaceRevisionConflict):
            adapter.workspace_action(
                "remove", row.workspace_id, expected_revision=diagnostics["revision"],
                idempotency_token="admin-remove-stale-diagnostics",
            )
        assert store.get(row.workspace_id) is not None
    finally:
        store.close()


def test_workspace_diagnostics_refreshes_revision_after_handled_probe_error(tmp_path: Path):
    store = WorkspaceMetadataStore(tmp_path / "workspace.sqlite3")
    try:
        row = store.create(
            PRINCIPAL, None, "Workspace", now="2026-09-19T00:00:00+00:00",
            quota_bytes=4, retention_days=30,
        )
        row = store.update(
            row.workspace_id, state="failed", desired_state="running", runtime_generation=7,
        )

        class Runtime:
            def call(self, _workspace_id, operation, _arguments, **_kwargs):
                assert operation == "inspect"
                raise WorkspaceError(
                    "generation_conflict", "stale generation",
                    reset_runtime_generation=True,
                )

        adapter = WorkspaceAdminAdapter(WorkspaceManager(store, Runtime()))
        diagnostics = adapter.workspace_diagnostics(row.workspace_id)
        current = store.get(row.workspace_id)

        assert current is not None
        assert diagnostics["runtime"] == {
            "status": "unavailable", "error_code": "generation_conflict",
        }
        assert diagnostics["revision"] == current.revision == row.revision + 1
        assert diagnostics["workspace"]["revision"] == current.revision
        assert diagnostics["workspace"]["runtime_generation"] == 0
    finally:
        store.close()


def test_workspace_diagnostics_uses_existing_not_found_after_probe_disappearance(tmp_path: Path):
    store = WorkspaceMetadataStore(tmp_path / "workspace.sqlite3")
    try:
        row = store.create(
            PRINCIPAL, None, "Workspace", now="2026-09-19T00:00:00+00:00",
            quota_bytes=4, retention_days=30,
        )
        row = store.update(row.workspace_id, state="failed", desired_state="running")

        class Runtime:
            def call(self, workspace_id, operation, _arguments, **_kwargs):
                assert operation == "inspect"
                assert store.delete_if_revision(workspace_id, row.revision)
                raise WorkspaceError("runtime_unavailable", "broker unavailable")

        adapter = WorkspaceAdminAdapter(WorkspaceManager(store, Runtime()))
        with pytest.raises(WorkspaceNotFound):
            adapter.workspace_diagnostics(row.workspace_id)
        assert store.get(row.workspace_id) is None
    finally:
        store.close()


@pytest.mark.parametrize(
    "change",
    [
        lambda observed: observed.pop("runtime_id"),
        lambda observed: observed.update(volume_name="cognita-ws-data-foreign"),
        lambda observed: observed.update(runtime_id="sandbox-still-present"),
        lambda observed: observed.update(workspace_id="foreign-workspace"),
    ],
)
def test_failed_first_use_admin_remove_rejects_incomplete_absence_proof(tmp_path: Path, change):
    store = WorkspaceMetadataStore(tmp_path / "workspace.sqlite3")
    try:
        row = store.create(
            PRINCIPAL, None, "Workspace", now="2026-09-19T00:00:00+00:00",
            quota_bytes=4, retention_days=30,
        )
        row = store.update(row.workspace_id, state="failed", desired_state="running")
        observed = {
            "state": "absent",
            "sandbox_name": f"cognita-ws-{row.workspace_id}",
            "volume_name": f"cognita-ws-data-{row.workspace_id}",
            "runtime_id": None,
            "volume_id": None,
        }
        change(observed)

        class Runtime:
            def call(self, _workspace_id, operation, _arguments, **_kwargs):
                assert operation == "inspect"
                return observed

        adapter = WorkspaceAdminAdapter(WorkspaceManager(store, Runtime()))
        with pytest.raises(WorkspaceError) as error:
            adapter.workspace_action(
                "remove", row.workspace_id, expected_revision=row.revision,
                idempotency_token="admin-remove-incomplete-proof",
            )
        assert error.value.reason == "ownership_unproven"
        assert store.get(row.workspace_id) is not None
    finally:
        store.close()


def test_delete_now_removes_volume_only_residual_without_stop(tmp_path: Path):
    store = WorkspaceMetadataStore(tmp_path / "workspace.sqlite3")
    runtime = None
    try:
        row = store.create(PRINCIPAL, None, "Workspace", now="2026-09-19T00:00:00+00:00", quota_bytes=4, retention_days=30)
        store.update(
            row.workspace_id, state="running", desired_state="running",
            volume_name=f"cognita-ws-data-{row.workspace_id}",
        )
        runtime, calls = _sdk_shaped_broker_client(row.workspace_id)
        manager = WorkspaceManager(store, runtime)
        result = manager.reconcile_credential(
            credential_id=PRINCIPAL, owner_status="tombstoned", retention="delete_now",
        )
        assert result["complete"] is True
        assert store.get(row.workspace_id) is None
        assert calls == ["inspect", "remove"]
    finally:
        if runtime is not None:
            runtime._client.close()
        store.close()


def test_remove_failed_owned_sandbox_delegates_teardown_to_broker(tmp_path: Path):
    """A failed sandbox with a missing volume must not be stopped by the app.

    The broker adapter has the ownership proof and removal sequence needed to
    clean this broken state.  Calling the app-level stop first attempts a
    reconnect through the missing volume and leaves the metadata row stuck in
    deleting.
    """
    store = WorkspaceMetadataStore(tmp_path / "workspace.sqlite3")
    try:
        row = store.create(
            PRINCIPAL, None, "Workspace", now="2026-09-19T00:00:00+00:00",
            quota_bytes=4, retention_days=30,
        )
        volume_name = f"cognita-ws-data-{row.workspace_id}"
        store.update(
            row.workspace_id, state="running", desired_state="running",
            volume_name=volume_name,
        )

        class FailedSandboxRuntime:
            def __init__(self):
                self.calls = []
                self.request_ids = []

            def call(self, workspace_id, operation, _arguments, **kwargs):
                self.calls.append(operation)
                self.request_ids.append(kwargs.get("request_id"))
                if operation == "inspect":
                    return {
                        "state": "failed",
                        "sandbox_name": f"cognita-ws-{workspace_id}",
                        "volume_name": volume_name,
                        "runtime_id": f"sandbox-{workspace_id}",
                        # The named volume is missing in the broken runtime.
                        "volume_id": None,
                        "host_path": None,
                        "path_status": "not_reported",
                    }
                if operation == "stop":
                    raise AssertionError("remove must not stop through a missing volume")
                if operation == "remove":
                    return {
                        "state": "absent",
                        "sandbox_name": f"cognita-ws-{workspace_id}",
                        "volume_name": volume_name,
                    }
                raise AssertionError(f"unexpected runtime operation: {operation}")

        runtime = FailedSandboxRuntime()
        manager = WorkspaceManager(store, runtime)
        result = manager.remove(
            _principal(), expected_revision=store.get(row.workspace_id).revision,
            idempotency_key="upgrade-admin-remove",
        )

        assert result["status"] == "success"
        assert store.get(row.workspace_id) is None
        assert runtime.calls == ["inspect", "remove"]
        # The Admin token is intentionally not a UUID.  The app boundary must
        # derive the broker's UUID-only request ID before sending remove.
        assert isinstance(UUID(runtime.request_ids[-1]), UUID)
    finally:
        store.close()


def test_admin_idempotency_key_maps_to_stable_broker_uuid():
    first = _stable_broker_request_id("workspace-1", "admin-token", "remove-digest")
    replay = _stable_broker_request_id("workspace-1", "admin-token", "remove-digest")
    changed = _stable_broker_request_id("workspace-1", "other-token", "remove-digest")

    assert UUID(first).version == 5
    assert replay == first
    assert changed != first


def test_workspace_admin_remove_owns_atomic_replay_after_metadata_delete(tmp_path: Path):
    store = WorkspaceMetadataStore(tmp_path / "workspace.sqlite3")
    try:
        row = store.create(
            PRINCIPAL, None, "Workspace", now="2026-09-19T00:00:00+00:00",
            quota_bytes=4, retention_days=30,
        )
        volume_name = f"cognita-ws-data-{row.workspace_id}"
        row = store.update(
            row.workspace_id, state="running", desired_state="running",
            volume_name=volume_name,
        )

        class Runtime:
            def __init__(self):
                self.calls = []

            def call(self, workspace_id, operation, _arguments, **_kwargs):
                self.calls.append(operation)
                if operation == "inspect":
                    return {
                        "state": "failed",
                        "sandbox_name": f"cognita-ws-{workspace_id}",
                        "volume_name": volume_name,
                        "runtime_id": f"sandbox-{workspace_id}",
                        "volume_id": None,
                        "host_path": None,
                        "path_status": "not_reported",
                    }
                if operation == "remove":
                    return {
                        "state": "absent",
                        "sandbox_name": f"cognita-ws-{workspace_id}",
                        "volume_name": volume_name,
                    }
                raise AssertionError(f"unexpected operation: {operation}")

        runtime = Runtime()
        adapter = WorkspaceAdminAdapter(WorkspaceManager(store, runtime))
        first = adapter.workspace_action(
            "remove", row.workspace_id, expected_revision=row.revision,
            idempotency_token="admin-remove-token",
        )
        assert first == {
            "revision": row.revision + 1, "workspace": None, "status": "success",
        }
        assert store.get(row.workspace_id) is None

        replay = adapter.workspace_action(
            "remove", row.workspace_id, expected_revision=row.revision,
            idempotency_token="admin-remove-token",
        )
        assert replay == {**first, "idempotency_replayed": True}
        assert runtime.calls == ["inspect", "remove"]

        with pytest.raises(WorkspaceRevisionConflict):
            adapter.workspace_action(
                "reset", row.workspace_id, expected_revision=row.revision,
                idempotency_token="admin-remove-token",
            )
    finally:
        store.close()


def test_concurrent_admin_removes_cannot_replace_another_workspaces_replay(tmp_path: Path):
    store = WorkspaceMetadataStore(tmp_path / "workspace.sqlite3")
    try:
        rows = []
        for principal_id in (PRINCIPAL, "22222222-2222-4222-8222-222222222222"):
            row = store.create(
                principal_id, None, "Workspace", now="2026-09-19T00:00:00+00:00",
                quota_bytes=4, retention_days=30,
            )
            rows.append(store.update(
                row.workspace_id, state="running", desired_state="running",
                volume_name=f"cognita-ws-data-{row.workspace_id}",
            ))

        barrier = Barrier(2)

        class Runtime:
            def call(self, workspace_id, operation, _arguments, **_kwargs):
                if operation == "inspect":
                    return {
                        "state": "failed",
                        "sandbox_name": f"cognita-ws-{workspace_id}",
                        "volume_name": f"cognita-ws-data-{workspace_id}",
                        "runtime_id": f"sandbox-{workspace_id}",
                        "volume_id": None,
                        "host_path": None,
                    }
                if operation == "remove":
                    barrier.wait(timeout=5)
                    return {"state": "absent"}
                raise AssertionError(f"unexpected operation: {operation}")

        adapter = WorkspaceAdminAdapter(WorkspaceManager(store, Runtime()))

        def remove(row):
            try:
                return row.workspace_id, adapter.workspace_action(
                    "remove", row.workspace_id, expected_revision=row.revision,
                    idempotency_token="shared-admin-token",
                )
            except WorkspaceError as exc:
                return row.workspace_id, exc

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = dict(pool.map(remove, rows))

        successes = [workspace_id for workspace_id, outcome in outcomes.items()
                     if isinstance(outcome, dict)]
        conflicts = [workspace_id for workspace_id, outcome in outcomes.items()
                     if isinstance(outcome, WorkspaceError) and outcome.reason == "path_conflict"]
        assert len(successes) == len(conflicts) == 1
        assert store.get(successes[0]) is None
        # The losing transaction must retain its metadata row rather than
        # silently replacing the winner's durable replay with another target.
        assert store.get(conflicts[0]) is not None
        replay = store.admin_idempotent(
            "shared-admin-token",
            _digest({
                "operation": "workspace_action", "action": "remove",
                "workspace_id": successes[0],
                "expected_revision": next(row.revision for row in rows
                                          if row.workspace_id == successes[0]),
            }),
        )
        assert replay is not None and replay["deleted_workspace_id"] == successes[0]
    finally:
        store.close()


def test_scavenge_removes_volume_only_residual_without_metadata_shortcut(tmp_path: Path):
    store = WorkspaceMetadataStore(tmp_path / "workspace.sqlite3")
    runtime = None
    try:
        row = store.create(PRINCIPAL, None, "Workspace", now="2026-09-19T00:00:00+00:00", quota_bytes=4, retention_days=30)
        store.update(
            row.workspace_id, state="running", desired_state="running",
            volume_name=f"cognita-ws-data-{row.workspace_id}",
            owner_status="tombstoned", deletion_intent="normal",
            deletion_requested_at="2026-08-19T00:00:00+00:00",
            deletion_due_at="2026-09-18T00:00:00+00:00",
        )
        runtime, calls = _sdk_shaped_broker_client(row.workspace_id)
        manager = WorkspaceManager(store, runtime)
        assert manager.scavenge(now=datetime(2026, 9, 19, tzinfo=UTC)) == [row.workspace_id]
        assert store.get(row.workspace_id) is None
        assert calls == ["inspect", "remove"]
    finally:
        if runtime is not None:
            runtime._client.close()
        store.close()


def test_bulk_delete_is_revision_bound_and_persists_each_outcome(tmp_path: Path):
    store = WorkspaceMetadataStore(tmp_path / "workspace.sqlite3")
    runtime = Runtime()
    try:
        manager = WorkspaceManager(store, runtime)
        manager.execute(_principal(), "workspace_write_file", {"path": "a", "text": "x"})
        row = store.get_by_principal(PRINCIPAL)
        assert row is not None
        preview = manager.preview_bulk_delete([row.workspace_id], {row.workspace_id: row.revision})
        result = manager.apply_bulk_delete(
            [row.workspace_id], {row.workspace_id: row.revision},
            preview_token=preview["token"], confirm_high_trust=True,
            idempotency_token="bulk-1",
        )
        # The aggregate status describes completion of the apply operation;
        # durable per-target outcomes carry the committed/failed semantics.
        assert result["status"] == "success"
        assert len(result["results"]) == 1
        assert result["results"][0]["workspace_id"] == row.workspace_id
        assert result["results"][0]["status"] == "committed"
        assert result["removed"] == [row.workspace_id]
        assert store.get(row.workspace_id) is None
        replay = manager.apply_bulk_delete(
            [row.workspace_id], {row.workspace_id: row.revision},
            preview_token=preview["token"], confirm_high_trust=True,
            idempotency_token="bulk-1",
        )
        assert replay["idempotency_replayed"] is True
    finally:
        store.close()


def test_stale_cached_measurement_fails_closed_without_delete(tmp_path: Path):
    store = WorkspaceMetadataStore(tmp_path / "workspace.sqlite3")
    runtime = MeasurementUnavailableRuntime()
    try:
        manager = WorkspaceManager(store, runtime)
        manager.execute(_principal(), "workspace_write_file", {"path": "a", "text": "x"})
        row = store.get_by_principal(PRINCIPAL)
        assert row is not None
        store.update_measurement(
            row.workspace_id, allocated_bytes=12, apparent_bytes=12,
            measured_at="2020-01-01T00:00:00+00:00", usage_status="fresh",
        )
        stale = store.get(row.workspace_id)
        assert stale is not None
        with pytest.raises(WorkspaceError) as error:
            manager.preview_bulk_delete([stale.workspace_id], {stale.workspace_id: stale.revision})
        assert error.value.reason == "measurement_unavailable"
        assert store.get(stale.workspace_id) is not None
        assert not any(operation == "remove" for _, operation, _ in runtime.calls)
    finally:
        store.close()


def test_consumed_preview_replays_after_aggregate_crash(tmp_path: Path):
    store = WorkspaceMetadataStore(tmp_path / "workspace.sqlite3")
    runtime = Runtime()
    try:
        manager = WorkspaceManager(store, runtime)
        manager.execute(_principal(), "workspace_write_file", {"path": "a", "text": "x"})
        row = store.get_by_principal(PRINCIPAL)
        assert row is not None
        preview = manager.preview_bulk_delete([row.workspace_id], {row.workspace_id: row.revision})
        original_save = store.save_admin_idempotent

        def crash_once(*args, **kwargs):
            store.save_admin_idempotent = original_save
            raise RuntimeError("simulated aggregate persistence crash")

        store.save_admin_idempotent = crash_once
        with pytest.raises(RuntimeError):
            manager.apply_bulk_delete(
                [row.workspace_id], {row.workspace_id: row.revision},
                preview_token=preview["token"], confirm_high_trust=True,
                idempotency_token="bulk-crash-1",
            )
        assert store.get(row.workspace_id) is None
        replay = manager.apply_bulk_delete(
            [row.workspace_id], {row.workspace_id: row.revision},
            preview_token=preview["token"], confirm_high_trust=True,
            idempotency_token="bulk-crash-1",
        )
        assert replay.get("idempotency_replayed") is not True
        assert replay["status"] == "success"
        assert replay["removed"] == [row.workspace_id]
        assert [operation for _, operation, _ in runtime.calls].count("remove") == 1
    finally:
        store.close()


def test_delete_now_credential_reconciliation_removes_owned_workspace(tmp_path: Path):
    store = WorkspaceMetadataStore(tmp_path / "workspace.sqlite3")
    runtime = Runtime()
    try:
        manager = WorkspaceManager(store, runtime)
        manager.execute(_principal(), "workspace_write_file", {"path": "a", "text": "x"})
        result = manager.reconcile_credential(
            credential_id=PRINCIPAL, owner_status="tombstoned", retention="delete_now",
        )
        assert result["complete"] is True
        assert result["workspace_deleted"] is True
        assert store.get_by_principal(PRINCIPAL) is None
    finally:
        store.close()


def test_normal_credential_retention_unpins_and_scavenges_from_delete_time(tmp_path: Path):
    store = WorkspaceMetadataStore(tmp_path / "workspace.sqlite3")
    runtime = Runtime()
    now = [datetime(2026, 9, 19, tzinfo=UTC)]
    try:
        manager = WorkspaceManager(store, runtime, clock=lambda: now[0], retention_days=90)
        manager.execute(_principal(), "workspace_write_file", {"path": "a", "text": "x"})
        pinned = manager.set_pinned(_principal(), True)
        assert pinned["workspace"]["pinned"] is True
        row = store.get_by_principal(PRINCIPAL)
        assert row is not None

        result = manager.reconcile_credential(
            credential_id=PRINCIPAL, owner_status="tombstoned", retention="normal",
        )
        assert result["complete"] is False
        updated = store.get(row.workspace_id)
        assert updated is not None
        assert not updated.pinned
        # Credential deletion uses the contract's fixed 30-day clock even
        # when the active Workspace policy is configured to 90 days.
        assert updated.deletion_due_at == "2026-10-19T00:00:00+00:00"

        now[0] += timedelta(days=31)
        assert manager.scavenge(now=now[0]) == [row.workspace_id]
        assert store.get(row.workspace_id) is None
    finally:
        store.close()


def test_normal_tombstone_replay_keeps_original_deadline_and_revision(tmp_path: Path):
    store = WorkspaceMetadataStore(tmp_path / "workspace.sqlite3")
    runtime = Runtime()
    now = [datetime(2026, 9, 29, tzinfo=UTC)]
    deleted_at = "2026-09-19T00:00:00+00:00"
    try:
        manager = WorkspaceManager(store, runtime, clock=lambda: now[0])
        manager.execute(_principal(), "workspace_write_file", {"path": "a", "text": "x"})
        first = manager.reconcile_credential(
            credential_id=PRINCIPAL, owner_status="tombstoned", retention="normal",
            deleted_at=deleted_at,
        )
        assert first["complete"] is False
        row = store.get_by_principal(PRINCIPAL)
        assert row is not None
        assert row.deletion_requested_at == deleted_at
        assert row.deletion_due_at == "2026-10-19T00:00:00+00:00"
        revision = row.revision
        now[0] += timedelta(days=40)
        replay = manager.reconcile_credential(
            credential_id=PRINCIPAL, owner_status="tombstoned", retention="normal",
            deleted_at=deleted_at,
        )
        assert replay == first
        row = store.get_by_principal(PRINCIPAL)
        assert row is not None
        assert row.revision == revision
        assert row.deletion_due_at == "2026-10-19T00:00:00+00:00"
        # Legacy callers without deleted_at must also preserve a recorded due.
        manager.reconcile_credential(
            credential_id=PRINCIPAL, owner_status="tombstoned", retention="normal",
        )
        assert store.get_by_principal(PRINCIPAL).deletion_due_at == row.deletion_due_at
    finally:
        store.close()


def test_retention_cleanup_only_due_recorded_intents_and_dry_run_has_no_effects(tmp_path: Path):
    store = WorkspaceMetadataStore(tmp_path / "workspace.sqlite3")
    runtime = Runtime()
    now = datetime(2026, 9, 19, tzinfo=UTC)
    try:
        row = store.create(PRINCIPAL, None, "Workspace", now=now.isoformat(), quota_bytes=4, retention_days=30)
        manager = WorkspaceManager(store, runtime, clock=lambda: now)
        assert manager.cleanup_retention(apply=True)["items"] == []
        store.update(row.workspace_id, owner_status="tombstoned", deletion_intent="normal",
                     deletion_requested_at="2026-09-01T00:00:00+00:00",
                     deletion_due_at="2026-10-01T00:00:00+00:00")
        assert manager.cleanup_retention(apply=True)["items"] == []
        store.update(row.workspace_id, deletion_requested_at="2026-08-19T00:00:00+00:00",
                     deletion_due_at="2026-09-18T00:00:00+00:00")
        preview = manager.cleanup_retention()
        assert preview["items"][0]["status"] == "due"
        assert store.get(row.workspace_id) is not None
        assert runtime.calls == []
        manager.set_pinned(_principal(), True)
        assert manager.cleanup_retention(apply=True)["items"] == []
        assert runtime.calls == []
    finally:
        store.close()


def test_retention_delete_now_supersedes_pin_but_lease_blocks(tmp_path: Path):
    store = WorkspaceMetadataStore(tmp_path / "workspace.sqlite3")
    runtime = Runtime()
    try:
        row = store.create(PRINCIPAL, None, "Workspace", now="2026-09-19T00:00:00+00:00", quota_bytes=4, retention_days=30)
        store.update(row.workspace_id, owner_status="tombstoned", deletion_intent="delete_now", pinned=1)
        manager = WorkspaceManager(store, runtime)
        lease = store.lease(row.workspace_id, "test")
        assert manager.cleanup_retention(apply=True)["items"][0]["status"] == "busy"
        assert runtime.calls == []
        store.release_lease(lease)
        with store.transaction() as db:
            db.execute(
                "INSERT INTO workspace_jobs(job_id,workspace_id,request_digest,state,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?)",
                ("job-1", row.workspace_id, "digest", "running", "2026-09-19T00:00:00+00:00", "2026-09-19T00:00:00+00:00"),
            )
        assert manager.cleanup_retention(apply=True)["items"][0]["status"] == "busy"
        assert manager.cleanup_retention()["items"][0]["status"] == "busy"
        runtime.job_state = "succeeded"
        preview = manager.cleanup_retention()["items"][0]
        assert preview["pinned"] is True and preview["pin_overridden"] is True
        assert preview["status"] == "busy"  # Dry run does not reconcile.
        assert manager.cleanup_retention(apply=True)["items"][0]["status"] == "deleted"
        assert store.get(row.workspace_id) is None
        assert manager.cleanup_retention(apply=True)["items"] == []
    finally:
        store.close()


def test_retention_revision_change_and_unavailable_broker_fail_closed(tmp_path: Path):
    store = WorkspaceMetadataStore(tmp_path / "workspace.sqlite3")
    runtime = MeasurementUnavailableRuntime()
    now = datetime(2026, 10, 20, tzinfo=UTC)
    try:
        row = store.create(PRINCIPAL, None, "Workspace", now="2026-09-19T00:00:00+00:00", quota_bytes=4, retention_days=30)
        store.update(row.workspace_id, owner_status="tombstoned", deletion_intent="normal",
                     deletion_requested_at="2026-09-19T00:00:00+00:00",
                     deletion_due_at="2026-10-19T00:00:00+00:00")
        manager = WorkspaceManager(store, runtime, clock=lambda: now)
        original = store.retention_candidates
        def race(**kwargs):
            rows = original(**kwargs)
            store.update(row.workspace_id, display_label="changed")
            return rows
        store.retention_candidates = race
        assert manager.cleanup_retention(apply=True)["items"][0]["status"] == "changed"
        assert runtime.calls == []
        store.retention_candidates = original
        result = manager.cleanup_retention(apply=True)["items"][0]
        assert result["status"] == "deferred" and result["reason"] == "ownership_unproven"
        current = store.get(row.workspace_id)
        assert current.deletion_intent == "normal"
        assert current.deletion_due_at == "2026-10-19T00:00:00+00:00"
        manager.set_pinned(_principal(), True)
        manager.reconcile_credential(credential_id=PRINCIPAL, owner_status="tombstoned",
                                     retention="normal", deleted_at="2026-09-19T00:00:00+00:00")
        assert bool(store.get(row.workspace_id).pinned) is True
        assert manager.cleanup_retention(apply=True)["items"] == []
    finally:
        store.close()


def test_retention_preview_uses_read_only_metadata_connection(tmp_path: Path):
    path = tmp_path / "workspace.sqlite3"
    writable = WorkspaceMetadataStore(path)
    try:
        row = writable.create(PRINCIPAL, None, "Workspace", now="2026-09-19T00:00:00+00:00", quota_bytes=4, retention_days=30)
        writable.update(row.workspace_id, owner_status="tombstoned", deletion_intent="delete_now")
        readonly = WorkspaceMetadataStore(path, read_only=True)
        try:
            manager = WorkspaceManager(readonly, Runtime())
            preview = manager.cleanup_retention()
            assert preview["items"][0]["status"] == "due"
            assert writable.get(row.workspace_id) is not None
            with pytest.raises(Exception):
                readonly.update(row.workspace_id, display_label="must not write")
        finally:
            readonly.close()
    finally:
        writable.close()
