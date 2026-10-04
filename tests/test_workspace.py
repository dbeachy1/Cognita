from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from cognita.runtime_broker.protocol import BrokerOperation
from cognita.runtime_broker.validation import normalize_rpc_arguments
from cognita.workspace import (
    BrokerRuntimeClient,
    WorkspaceError,
    WorkspaceManager,
    WorkspaceMetadataStore,
    normalize_path,
    workspace_tool_result,
)
from cognita.workspace_admin import WorkspaceAdminAdapter


class FakePrincipal:
    principal_id = "11111111-1111-4111-8111-111111111111"
    surface_id = "surface-1"


class FakeRuntime:
    def __init__(self):
        self.calls = []
        self.job_state = "succeeded"
        self.inspect_result = {}
        # 12.18.3: a realistic fs_usage response so _measure_after_mutation's
        # staleness gate (self._apparent_measured_at) gets primed by the
        # first mutation, same as the real broker's fs_usage/du -- otherwise
        # every test written against "one measurement per admission window"
        # would see an extra inspect+fs_usage pair on every later call.
        self.fs_usage_result = {"entries": [], "truncated": False, "total_bytes": 0}

    def call(self, workspace_id, operation, arguments, **kwargs):
        self.calls.append((workspace_id, operation, arguments, kwargs))
        if operation == "job_start":
            return {"job_id": "job-1", "state": "running"}
        if operation == "job_get":
            return {"state": self.job_state, "stdout": "", "stderr": ""}
        if operation == "inspect":
            return {"state": "running", **self.inspect_result}
        if operation == "fs_usage":
            return dict(self.fs_usage_result)
        return {"ok": True}


def test_paths_reject_escape_and_pseudo_filesystems():
    assert normalize_path("/workspace/src/main.py") == "src/main.py"
    assert normalize_path("relative/file") == "relative/file"
    for path in ("../secret", "/tmp/secret", "proc/self/status", "a\\b", "a//b"):
        try:
            normalize_path(path)
        except WorkspaceError:
            pass
        else:
            raise AssertionError(f"path unexpectedly accepted: {path}")


def test_workspace_is_principal_scoped_and_mutations_are_idempotent(tmp_path: Path):
    store = WorkspaceMetadataStore(tmp_path / "workspace.sqlite3")
    try:
        runtime = FakeRuntime()
        manager = WorkspaceManager(store, runtime)
        principal = FakePrincipal()
        first = manager.execute(principal, "workspace_write_file", {"path": "a", "text": "x", "idempotency_key": "write-1"})
        second = manager.execute(principal, "workspace_write_file", {"path": "a", "text": "x", "idempotency_key": "write-1"})
        assert second["replayed"] is True
        assert second["data"] == first["data"]
        fs_write_calls = [item for item in runtime.calls if item[1] == "fs_write"]
        assert len(fs_write_calls) == 1
        assert fs_write_calls[0][3]["request_id"] is not None
        assert manager.info(principal)["workspace"]["principal_id"] == principal.principal_id
    finally:
        store.close()


def test_workspace_runtime_errors_preserve_specific_reason_and_hash_diagnostics():
    class Response:
        def __init__(self, payload):
            self.payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self.payload

    class Client:
        def __init__(self, payload):
            self.payload = payload

        def post(self, *args, **kwargs):
            return Response(self.payload)

    stale = BrokerRuntimeClient(
        "http://runtime", "token",
        client=Client({
            "code": "conflict", "stage": "fs_write",
            "diagnostics": {"expected_sha256": "0" * 64, "actual_sha256": "1" * 64},
        }),
    )
    with pytest.raises(WorkspaceError) as stale_error:
        stale.call("workspace", "fs_write", {})
    assert stale_error.value.reason == "stale_file"
    assert stale_error.value.fields["expected_sha256"] == "0" * 64
    assert stale_error.value.fields["actual_sha256"] == "1" * 64

    search = BrokerRuntimeClient(
        "http://runtime", "token",
        client=Client({"code": "timeout", "stage": "fs_search"}),
    )
    with pytest.raises(WorkspaceError) as search_error:
        search.call("workspace", "fs_search", {})
    assert search_error.value.reason == "search_timeout"


def test_workspace_failure_diagnostics_are_bounded_and_secret_safe(tmp_path: Path, caplog):
    store = WorkspaceMetadataStore(tmp_path / "workspace.sqlite3")
    try:
        manager = WorkspaceManager(store, FakeRuntime())

        def fail(*_args, **_kwargs):
            raise RuntimeError("secret command /workspace/private.txt")

        manager.execute = fail
        with caplog.at_level(logging.WARNING, logger="cognita.workspace"):
            result = workspace_tool_result(
                manager, FakePrincipal(), "workspace_cancel_job",
                {"job_id": "22222222-2222-4222-8222-222222222222", "token": "secret-token"},
            )
        assert result["reason"] == "internal_error"
        record = next(item for item in caplog.records if item.event == "workspace_tool_failure")
        assert record.tool == "workspace_cancel_job"
        assert record.job_id == "22222222-2222-4222-8222-222222222222"
        assert record.category == "internal_error"
        assert "secret-token" not in caplog.text
        assert "/workspace/private.txt" not in caplog.text
        assert "secret command" not in caplog.text
    finally:
        store.close()


def test_broker_failure_logs_stage_category_and_correlation_without_arguments(caplog):
    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "code": "runtime_failure", "stage": "job_cancel",
                "category": "runtime_failure", "correlation_id": "corr-1",
            }

    class Client:
        def post(self, *args, **kwargs):
            return Response()

    runtime = BrokerRuntimeClient("http://runtime", "secret-token", client=Client())
    with caplog.at_level(logging.WARNING, logger="cognita.workspace"):
        with pytest.raises(WorkspaceError):
            runtime.call(
                "33333333-3333-4333-8333-333333333333", "job_cancel",
                {"job_id": "44444444-4444-4444-8444-444444444444", "argv": ["secret-command"]},
                request_id="req-1",
            )
    record = next(item for item in caplog.records if item.event == "workspace_broker_failure")
    assert record.stage == "job_cancel"
    assert record.category == "runtime_failure"
    assert record.correlation_id == "corr-1"
    assert record.workspace_id == "33333333-3333-4333-8333-333333333333"
    assert record.job_id == "44444444-4444-4444-8444-444444444444"
    assert "secret-token" not in caplog.text
    assert "secret-command" not in caplog.text


def test_workspace_responses_retain_last_measured_usage(tmp_path: Path):
    store = WorkspaceMetadataStore(tmp_path / "workspace.sqlite3")
    try:
        runtime = FakeRuntime()
        # 12.18.3 (LIVE BUG): apparent bytes now come exclusively from
        # fs_usage (guest-side du) -- a plain inspect's own apparent value is
        # never trusted, since the SDK volume's used_bytes reads 0 on kei.
        # allocated bytes are unaffected and still come from inspect.
        runtime.inspect_result = {"measured_allocated_bytes": 17}
        runtime.fs_usage_result = {"entries": [], "truncated": False, "total_bytes": 23}
        manager = WorkspaceManager(store, runtime)
        principal = FakePrincipal()
        manager.execute(principal, "workspace_write_file", {
            "path": "a", "text": "x", "idempotency_key": "usage-write",
        })
        result = manager.execute(principal, "workspace_make_directory", {"path": "later"})
        assert result["workspace"]["usage_status"] == "fresh"
        assert result["workspace"]["measured_allocated_bytes"] == 17
        assert result["workspace"]["measured_apparent_bytes"] == 23
    finally:
        store.close()


def test_workspace_info_reports_persisted_usage_not_broker_vocabulary(tmp_path: Path):
    # 13.0.2: the broker's inspect dict says usage_status "measured" and
    # allocated None when the SDK has no such figure. workspace_info used to
    # merge that raw dict over the record, so it disagreed with every other
    # tool's workspace block ("fresh" / 0) for the same row.
    #
    # 13.1.0: the apparent figure is fed through fs_usage, the only sample
    # the manager trusts for that value since 12.18.3; inspect's own
    # apparent bytes (0 here, the 12.x SDK reading) are ignored, and the
    # response must still describe the record, not the broker vocabulary.
    store = WorkspaceMetadataStore(tmp_path / "workspace.sqlite3")
    try:
        runtime = FakeRuntime()
        runtime.inspect_result = {
            "measured_allocated_bytes": None,
            "measured_apparent_bytes": 0,
            "usage_status": "measured",
            "network_mode": "off",
        }
        runtime.fs_usage_result = {"entries": [], "truncated": False, "total_bytes": 6424}
        manager = WorkspaceManager(store, runtime)
        principal = FakePrincipal()
        manager.execute(principal, "workspace_write_file", {
            "path": "a", "text": "x", "idempotency_key": "usage-info",
        })
        info = manager.execute(principal, "workspace_info", {})["workspace"]
        assert info["usage_status"] == "fresh"
        assert info["measured_apparent_bytes"] == 6424
        assert info["runtime_state"] == "running"
        other = manager.execute(principal, "workspace_make_directory", {"path": "later"})["workspace"]
        assert (other["usage_status"], other["measured_apparent_bytes"]) == ("fresh", 6424)
        assert store.get_by_principal(principal.principal_id).measured_apparent_bytes == 6424
    finally:
        store.close()


def test_partial_usage_invalidates_current_quota_until_complete_measurement(tmp_path: Path):
    store = WorkspaceMetadataStore(tmp_path / "workspace.sqlite3")
    try:
        runtime = FakeRuntime()
        runtime.inspect_result = {"measured_allocated_bytes": 17, "measured_apparent_bytes": 0}
        runtime.fs_usage_result = {"entries": [], "truncated": False, "total_bytes": 700}
        manager = WorkspaceManager(store, runtime)
        principal = FakePrincipal()
        first = manager.execute(principal, "workspace_write_file", {
            "path": "a", "text": "x", "idempotency_key": "usage-transition",
        })["workspace"]
        workspace_id = first["workspace_id"]
        assert first["usage_status"] == "fresh"
        assert first["measured_apparent_bytes"] == 700
        assert first["quota_remaining_bytes"] == first["quota_bytes"] - 700
        trusted_at = manager._apparent_measured_at[workspace_id]

        runtime.fs_usage_result = {
            "entries": [{"path": "/workspace/a", "bytes": 1}],
            "truncated": True, "total_bytes": None,
        }
        partial = manager.refresh_usage(workspace_id, preserve_revision=True)
        assert partial["usage_status"] == "unknown"
        assert partial["measured_apparent_bytes"] == 700  # historical value retained
        assert workspace_id not in manager._apparent_measured_at
        assert manager._quota_fields(store.get(workspace_id)) == {
            "quota_remaining_bytes": None, "quota_warning": False,
        }

        # A later ordinary inspect can update allocated bytes, but must not
        # turn the failed apparent measurement or its quota back into current.
        inspected = manager._apply_runtime_measurement(
            store.get(workspace_id),
            {"measured_allocated_bytes": 31, "measured_apparent_bytes": 0},
            preserve_revision=True,
        )
        assert inspected.measured_allocated_bytes == 31
        assert inspected.measured_apparent_bytes == 700
        assert inspected.usage_status == "unknown"
        assert manager._quota_fields(inspected)["quota_remaining_bytes"] is None

        info = manager.info(principal)["workspace"]
        assert info["usage_by_directory"] == [{"path": "/workspace/a", "bytes": 1}]
        assert info["usage_status"] == "unknown"
        assert info["measured_apparent_bytes"] == 700
        assert info["quota_remaining_bytes"] is None
        assert info["quota_warning"] is False
        assert workspace_id not in manager._apparent_measured_at

        runtime.fs_usage_result = {"entries": [], "truncated": False, "total_bytes": 800}
        recovered = manager.info(principal)["workspace"]
        assert recovered["usage_status"] == "fresh"
        assert recovered["measured_apparent_bytes"] == 800
        assert recovered["quota_remaining_bytes"] == recovered["quota_bytes"] - 800
        assert manager._apparent_measured_at[workspace_id] >= trusted_at
    finally:
        store.close()


def test_usage_command_failure_keeps_historical_sample_unavailable(tmp_path: Path):
    class FailingUsageRuntime(FakeRuntime):
        fail_usage = False

        def call(self, workspace_id, operation, arguments, **kwargs):
            if operation == "fs_usage" and self.fail_usage:
                raise WorkspaceError("runtime_unavailable", "usage unavailable")
            return super().call(workspace_id, operation, arguments, **kwargs)

    store = WorkspaceMetadataStore(tmp_path / "workspace.sqlite3")
    try:
        runtime = FailingUsageRuntime()
        runtime.inspect_result = {"measured_allocated_bytes": 42}
        runtime.fs_usage_result = {"entries": [], "truncated": False, "total_bytes": 900}
        manager = WorkspaceManager(store, runtime)
        principal = FakePrincipal()
        workspace_id = manager.execute(principal, "workspace_write_file", {
            "path": "a", "text": "x", "idempotency_key": "usage-error",
        })["workspace"]["workspace_id"]
        runtime.fail_usage = True
        refreshed = manager.refresh_usage(workspace_id)
        assert refreshed["usage_status"] == "unknown"
        assert refreshed["measured_apparent_bytes"] == 900
        info = manager.info(principal)["workspace"]
        assert info["usage_by_directory_error"] == "runtime_unavailable"
        assert info["usage_status"] == "unknown"
        assert info["quota_remaining_bytes"] is None
        assert info["quota_warning"] is False
    finally:
        store.close()


def test_job_bounds_and_lifecycle(tmp_path: Path):
    store = WorkspaceMetadataStore(tmp_path / "workspace.sqlite3")
    try:
        manager = WorkspaceManager(store, FakeRuntime())
        principal = FakePrincipal()
        started = manager.execute(principal, "workspace_start_job", {"argv": ["python", "-c", "print(1)"]})
        assert started["job"]["state"] == "running"
        assert manager.execute(principal, "workspace_get_job", {"job_id": "job-1"})["job"]["state"] == "succeeded"
        assert manager.execute(principal, "workspace_start_job", {"argv": ["true"]})["job"]["state"] == "running"
        manager.execute(principal, "workspace_get_job", {"job_id": "job-1"})
        assert manager.set_pinned(principal, True)["workspace"]["pinned"] is True
        assert manager.stop(principal)["workspace"]["state"] == "stopped"
    finally:
        store.close()


@pytest.mark.parametrize(
    ("cwd_argument", "expected_cwd"),
    [({}, "/workspace"), ({"cwd": "/workspace"}, "/workspace"),
     ({"cwd": "smoke"}, "/workspace/smoke")],
)
def test_job_cwd_is_valid_at_broker_boundary(tmp_path: Path, cwd_argument: dict, expected_cwd: str):
    store = WorkspaceMetadataStore(tmp_path / "workspace.sqlite3")
    try:
        runtime = FakeRuntime()
        manager = WorkspaceManager(store, runtime)
        manager.execute(FakePrincipal(), "workspace_start_job", {"argv": ["true"], **cwd_argument})
        # 12.18.3: this is the first-ever mutation for this principal, so
        # _measure_after_mutation appends its own inspect+fs_usage calls
        # right after job_start -- look it up by operation rather than
        # assuming it is the trailing call.
        job_start_call = next(call for call in runtime.calls if call[1] == "job_start")
        operation, arguments = job_start_call[1:3]
        assert operation == "job_start"
        assert normalize_rpc_arguments(BrokerOperation.JOB_START, arguments)["cwd"] == expected_cwd
    finally:
        store.close()


def test_domain_translates_public_arguments_to_strict_broker_contract(tmp_path: Path):
    store = WorkspaceMetadataStore(tmp_path / "workspace.sqlite3")
    try:
        runtime = FakeRuntime()
        manager = WorkspaceManager(store, runtime)
        principal = FakePrincipal()
        manager.execute(principal, "workspace_write_file", {
            "path": "notes.txt", "text": "one\ntwo\n", "create_policy": "parents",
        })
        # 12.18.3: this is the first-ever mutation for this principal, so
        # _measure_after_mutation's staleness gate (self._apparent_measured_at)
        # is empty and it appends its own inspect+fs_usage calls right after
        # fs_write -- look up the fs_write call by operation rather than
        # assuming it is the trailing one.
        fs_write_call = next(call for call in runtime.calls if call[1] == "fs_write")
        assert fs_write_call[2] == {
            "path": "notes.txt", "text": "one\ntwo\n", "create_parents": True,
        }
        manager.execute(principal, "workspace_read_file", {
            "path": "notes.txt", "encoding": "base64",
        })
        # A3 (DESIGN-12.18 §3.3): a byte-mode read now makes one extra
        # fs_stat call afterward for total_bytes/has_more, so the fs_read
        # call itself is the second-to-last, not the last.
        assert runtime.calls[-2][1] == "fs_read"
        assert runtime.calls[-2][2]["binary"] is True
        assert runtime.calls[-1][1] == "fs_stat"
        assert runtime.calls[-1][2]["path"] == "notes.txt"
        manager.execute(principal, "workspace_search", {
            "roots": ["src"], "pattern": "TODO", "mode": "text",
        })
        assert runtime.calls[-1][2]["timeout_seconds"] == 30
        manager.execute(principal, "workspace_list_files", {
            "path": "/workspace", "recursive": False,
        })
        assert runtime.calls[-1][1] == "fs_list"
        assert runtime.calls[-1][2]["path"] == "/workspace"
        manager.execute(principal, "workspace_stat", {"path": "/workspace"})
        assert runtime.calls[-1][1] == "fs_stat"
        assert runtime.calls[-1][2]["path"] == "/workspace"
        manager.execute(principal, "workspace_search", {
            "roots": ["/workspace"], "pattern": "*.txt", "mode": "glob",
        })
        assert runtime.calls[-1][1] == "fs_search"
        assert runtime.calls[-1][2]["roots"] == ["/workspace"]
        with pytest.raises(WorkspaceError, match="invalid regular expression"):
            manager.execute(principal, "workspace_search", {
                "roots": ["src"], "pattern": "(", "mode": "regex",
            })
        manager.execute(principal, "workspace_remove_paths", {
            "paths": ["notes.txt"], "expected_hashes": {"notes.txt": "a" * 64},
        })
        assert runtime.calls[-1][2]["expected_hashes"] == {"notes.txt": "a" * 64}
    finally:
        store.close()


def test_running_job_blocks_mutations_and_idle_stop_but_pin_does_not(tmp_path: Path):
    store = WorkspaceMetadataStore(tmp_path / "workspace.sqlite3")
    now = datetime(2026, 9, 17, tzinfo=UTC)
    try:
        runtime = FakeRuntime()
        runtime.job_state = "running"
        manager = WorkspaceManager(store, runtime, idle_seconds=1, clock=lambda: now)
        principal = FakePrincipal()
        manager.execute(principal, "workspace_start_job", {"argv": ["sleep", "10"]})
        with pytest.raises(WorkspaceError, match="already running"):
            manager.execute(principal, "workspace_write_file", {"path": "x", "text": "y"})
        assert manager.stop_idle(now=now + timedelta(seconds=2)) == []
        runtime.job_state = "succeeded"
        manager.set_pinned(principal, True)
        workspace_id = store.get_by_principal(principal.principal_id).workspace_id
        # Background idle teardown also reconciles a completed job whose
        # client never polled for its terminal state.
        assert manager.stop_idle(now=now + timedelta(seconds=2)) == [workspace_id]
    finally:
        store.close()


def test_completed_job_releases_application_lock_without_client_poll(tmp_path: Path):
    store = WorkspaceMetadataStore(tmp_path / "workspace.sqlite3")
    try:
        runtime = FakeRuntime()
        manager = WorkspaceManager(store, runtime)
        principal = FakePrincipal()
        started = manager.execute(principal, "workspace_start_job", {"argv": ["true"]})
        workspace_id = started["workspace"]["workspace_id"]
        assert store.active_job(workspace_id)["job_id"] == "job-1"

        # The client never calls workspace_get_job. Admission must ask the
        # broker rather than trust a cached row left behind by a completed job.
        result = manager.execute(principal, "workspace_write_file", {"path": "next.txt", "text": "ok"})
        assert result["status"] == "success"
        assert store.active_job(workspace_id) is None
        assert [call[1] for call in runtime.calls][-2:] == ["job_get", "fs_write"]
    finally:
        store.close()


def test_completed_job_allows_next_job_without_client_poll(tmp_path: Path):
    store = WorkspaceMetadataStore(tmp_path / "workspace.sqlite3")
    try:
        runtime = FakeRuntime()
        manager = WorkspaceManager(store, runtime)
        principal = FakePrincipal()
        manager.execute(principal, "workspace_start_job", {"argv": ["true"]})
        assert manager.execute(principal, "workspace_start_job", {"argv": ["true"]})["status"] == "success"
        assert [call[1] for call in runtime.calls][-2:] == ["job_get", "job_start"]
    finally:
        store.close()


def test_live_job_error_identifies_job_and_unblocks_after_completion(tmp_path: Path):
    store = WorkspaceMetadataStore(tmp_path / "workspace.sqlite3")
    try:
        runtime = FakeRuntime()
        runtime.job_state = "running"
        manager = WorkspaceManager(store, runtime)
        principal = FakePrincipal()
        manager.execute(principal, "workspace_start_job", {"argv": ["sleep", "10"]})
        with pytest.raises(WorkspaceError) as blocked:
            manager.execute(principal, "workspace_make_directory", {"path": "later"})
        assert blocked.value.reason == "job_running"
        assert blocked.value.fields["job_id"] == "job-1"
        assert blocked.value.fields["state"] == "running"
        assert blocked.value.fields["started_at"]
        assert blocked.value.fields["correlation_id"]
        assert blocked.value.fields["retryable"] is True

        runtime.job_state = "succeeded"
        assert manager.execute(principal, "workspace_make_directory", {"path": "later"})["status"] == "success"
    finally:
        store.close()


def test_unavailable_runtime_does_not_clear_unproved_job_lock(tmp_path: Path):
    class UnavailableJobRuntime(FakeRuntime):
        def call(self, workspace_id, operation, arguments, **kwargs):
            if operation == "job_get":
                raise WorkspaceError("runtime_unavailable", "broker is unavailable")
            return super().call(workspace_id, operation, arguments, **kwargs)

    store = WorkspaceMetadataStore(tmp_path / "workspace.sqlite3")
    try:
        runtime = UnavailableJobRuntime()
        manager = WorkspaceManager(store, runtime)
        principal = FakePrincipal()
        started = manager.execute(principal, "workspace_start_job", {"argv": ["sleep", "10"]})
        with pytest.raises(WorkspaceError) as blocked:
            manager.execute(principal, "workspace_write_file", {"path": "later.txt", "text": "ok"})
        assert blocked.value.reason == "runtime_unavailable"
        assert blocked.value.fields["job_id"] == "job-1"
        assert store.active_job(started["workspace"]["workspace_id"]) is not None
    finally:
        store.close()


def test_job_check_retries_once_after_runtime_generation_change(tmp_path: Path):
    class ReplacedRuntime(FakeRuntime):
        def __init__(self):
            super().__init__()
            self.job_reads = 0

        def call(self, workspace_id, operation, arguments, **kwargs):
            if operation == "job_get":
                self.job_reads += 1
                if self.job_reads == 1:
                    raise WorkspaceError("generation_conflict", "runtime changed",
                                         reset_runtime_generation=True)
            return super().call(workspace_id, operation, arguments, **kwargs)

    store = WorkspaceMetadataStore(tmp_path / "workspace.sqlite3")
    try:
        runtime = ReplacedRuntime()
        manager = WorkspaceManager(store, runtime)
        principal = FakePrincipal()
        manager.execute(principal, "workspace_start_job", {"argv": ["true"]})
        assert manager.execute(principal, "workspace_make_directory", {"path": "after-restart"})["status"] == "success"
        assert runtime.job_reads == 2
    finally:
        store.close()


def test_workspace_info_marks_failed_runtime_inspection_degraded(tmp_path: Path):
    class FailedInspectRuntime(FakeRuntime):
        def call(self, workspace_id, operation, arguments, **kwargs):
            if operation == "inspect":
                raise WorkspaceError("runtime_unavailable", "broker is unavailable")
            return super().call(workspace_id, operation, arguments, **kwargs)

    store = WorkspaceMetadataStore(tmp_path / "workspace.sqlite3")
    try:
        manager = WorkspaceManager(store, FailedInspectRuntime())
        principal = FakePrincipal()
        manager.execute(principal, "workspace_make_directory", {"path": "test"})
        info = manager.info(principal)["workspace"]
        assert info["state"] == "running"  # Durable lifecycle record.
        assert info["runtime_available"] is False
        assert info["operational_state"] == "degraded"
        assert info["runtime_error_reason"] == "runtime_unavailable"
    finally:
        store.close()


def test_workspace_info_keeps_persisted_state_when_runtime_is_absent(tmp_path: Path):
    class AbsentInspectRuntime(FakeRuntime):
        def call(self, workspace_id, operation, arguments, **kwargs):
            if operation == "inspect":
                return {"state": "absent"}
            return super().call(workspace_id, operation, arguments, **kwargs)

    store = WorkspaceMetadataStore(tmp_path / "workspace.sqlite3")
    try:
        manager = WorkspaceManager(store, AbsentInspectRuntime())
        principal = FakePrincipal()
        manager.execute(principal, "workspace_make_directory", {"path": "test"})
        info = manager.info(principal)["workspace"]
        assert info["state"] == "running"
        assert info["runtime_state"] == "absent"
        assert info["runtime_available"] is False
        assert info["operational_state"] == "degraded"
    finally:
        store.close()


def test_idempotency_key_is_scoped_to_tool_and_arguments(tmp_path: Path):
    store = WorkspaceMetadataStore(tmp_path / "workspace.sqlite3")
    try:
        manager = WorkspaceManager(store, FakeRuntime())
        principal = FakePrincipal()
        manager.execute(principal, "workspace_make_directory", {
            "path": "same", "idempotency_key": "one-key",
        })
        with pytest.raises(WorkspaceError, match="reused"):
            manager.execute(principal, "workspace_remove_paths", {
                "paths": ["same"], "idempotency_key": "one-key",
            })
    finally:
        store.close()


def test_admin_adapter_persists_validated_settings_and_paginates(tmp_path: Path):
    class Secrets:
        def __init__(self):
            self.values = {}

        def store_trusted_secret(self, name, value):
            self.values[name] = value

        def trusted_secret(self, name):
            return self.values.get(name)

    class Brave:
        def configure(self, key, *, enabled):
            self.key, self.enabled = key, enabled

        def disable(self):
            self.enabled = False

        def search(self, query, *, count):
            return {"status": "success", "results": []}

    store = WorkspaceMetadataStore(tmp_path / "workspace.sqlite3")
    try:
        manager = WorkspaceManager(store, FakeRuntime())
        manager.execute(FakePrincipal(), "workspace_make_directory", {"path": "one"})
        secrets = Secrets()
        adapter = WorkspaceAdminAdapter(manager, trusted_secret_store=secrets, brave_search=Brave())
        request_values = {
            "retention_days": 90,
            "network_mode": "allowlist",
            "network_rules": ["example.com", "*.python.org"],
            "brave_enabled": True,
            "brave_api_key": "b" * 32,
        }
        updated = adapter.update_workspace_settings(
            request_values, expected_revision=0, confirm_high_trust=True,
            idempotency_token="settings-1",
        )
        assert updated["revision"] == 1
        assert updated["brave_configured"] is True
        assert manager.retention_days == 90
        assert manager.network_policy["mode"] == "allowlist"
        replay = adapter.update_workspace_settings(
            request_values, expected_revision=0, confirm_high_trust=True,
            idempotency_token="settings-1",
        )
        assert replay["idempotency_replayed"] is True
        page = adapter.list_admin_workspaces(
            sort="credential", direction="asc", search="", states=(), pinned=None,
            expired=None, over_warning=None, limit=1,
        )
        assert len(page["workspaces"]) == 1
    finally:
        store.close()


def test_adapter_returns_bounded_errors(tmp_path: Path):
    store = WorkspaceMetadataStore(tmp_path / "workspace.sqlite3")
    try:
        manager = WorkspaceManager(store, FakeRuntime())
        result = workspace_tool_result(manager, FakePrincipal(), "workspace_write_file", {"path": "a", "text": "x", "unexpected": True})
        assert result["status"] == "error"
        assert result["reason"] == "invalid_arguments"
    finally:
        store.close()
