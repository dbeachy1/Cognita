"""App-side tests for release 12.18.0 Release A (DESIGN-12.18-WORKSPACE-NEXT-FEATURES.md):

* A3 (§3.3): ``workspace_read_file`` tail/line-range reads (mapped to the
  broker's ``fs_lines`` operation) and byte-mode reads gaining
  ``total_bytes``/``has_more`` via an extra ``fs_stat`` call;
  ``workspace_get_job``'s ``tail_lines`` and the ``has_more_stdout``/
  ``has_more_stderr`` aliases.
* A4 (§3.4): ``quota_remaining_bytes``/``quota_warning`` on every response;
  measure-after-mutation staleness; ``stop_idle``/``stop`` recording
  ``last_auto_action``; ``workspace_info``'s ``usage_by_directory``.

These exercise ``WorkspaceManager`` directly against a fake ``RuntimeClient``,
the same layer and pattern ``tests/test_workspace.py`` and
``tests/test_workspace_wait.py`` already use -- no real broker or sandbox.
"""

from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from cognita.workspace import (
    MAX_FILE_BYTES,
    WorkspaceError,
    WorkspaceManager,
    WorkspaceMetadataStore,
)


class FakePrincipal:
    principal_id = "33333333-3333-4333-8333-333333333333"
    surface_id = "surface-release-a"


class FakeClock:
    """A settable clock, injected so measurement-staleness tests never
    depend on real wall-clock time."""

    def __init__(self, start: datetime | None = None):
        self.now = start or datetime(2026, 1, 1, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


class FakeRuntime:
    """Configurable fake broker: canned per-operation responses plus a full
    call log, mirroring ``tests/test_workspace.py``'s ``FakeRuntime`` but
    extended with the Release A operations (``fs_lines``, ``fs_usage``) and
    an ``inspect`` that can be told to fail."""

    def __init__(self):
        self.calls: list[tuple[str, str, dict, dict]] = []
        self.job_state = "succeeded"
        self.inspect_result: dict = {}
        self.inspect_raises = False
        self.fs_lines_result: dict | None = None
        self.fs_stat_result: dict = {"kind": "file", "size": 0}
        # 12.18.3: total_bytes primes self._apparent_measured_at (the new
        # staleness gate for _measure_after_mutation) the same way the real
        # broker's fs_usage/du response does -- a test that does not care
        # about this bug still gets "one real measurement per admission
        # window" instead of an inspect+fs_usage pair on every later call.
        self.fs_usage_result: dict = {"entries": [], "truncated": False, "total_bytes": 0}
        self.job_get_result: dict | None = None

    def call(self, workspace_id, operation, arguments, **kwargs):
        self.calls.append((workspace_id, operation, dict(arguments), kwargs))
        if operation == "job_start":
            return {"job_id": "job-1", "state": "running"}
        if operation == "job_get":
            if self.job_get_result is not None:
                return dict(self.job_get_result)
            return {"state": self.job_state, "stdout": "", "stderr": ""}
        if operation == "inspect":
            if self.inspect_raises:
                raise WorkspaceError("runtime_unavailable", "fake inspect failure")
            return {"state": "running", **self.inspect_result}
        if operation == "fs_lines":
            assert self.fs_lines_result is not None, "test must set fs_lines_result"
            return {"path": arguments["path"], **self.fs_lines_result}
        if operation == "fs_stat":
            return {"path": arguments.get("path"), **self.fs_stat_result}
        if operation == "fs_usage":
            return dict(self.fs_usage_result)
        return {"ok": True}

    def inspect_call_count(self) -> int:
        return len([call for call in self.calls if call[1] == "inspect"])


def _store(tmp_path: Path) -> WorkspaceMetadataStore:
    return WorkspaceMetadataStore(tmp_path / "workspace.sqlite3")


# ---------------------------------------------------------------------------
# A3: workspace_read_file tail/line-range reads
# ---------------------------------------------------------------------------


def test_read_file_tail_lines_returns_exactly_the_fakes_lines(tmp_path: Path):
    store = _store(tmp_path)
    try:
        runtime = FakeRuntime()
        runtime.fs_lines_result = {
            "content_b64": base64.b64encode(b"6\n7\n8\n9\n10\n").decode("ascii"),
            "bytes": 11, "start_line": 6, "end_line": 10,
            "total_lines": 10, "total_bytes": 20, "has_more": False,
        }
        manager = WorkspaceManager(store, runtime)
        principal = FakePrincipal()
        result = manager.execute(principal, "workspace_read_file", {"path": "big.txt", "tail_lines": 5})
        assert result["data"]["content"] == "6\n7\n8\n9\n10\n"
        assert result["data"]["encoding"] == "text"
        assert result["data"]["has_more"] is False
        assert result["data"]["total_lines"] == 10
        fs_lines_call = next(call for call in runtime.calls if call[1] == "fs_lines")
        assert fs_lines_call[2] == {"path": "big.txt", "tail_lines": 5, "max_bytes": MAX_FILE_BYTES}
        # A line-mode read never makes the extra fs_stat call byte-mode reads
        # do -- the broker's fs_lines response already carries total_bytes.
        assert not any(call[1] == "fs_stat" for call in runtime.calls)
    finally:
        store.close()


def test_read_file_start_line_end_line_sends_exact_broker_arguments(tmp_path: Path):
    store = _store(tmp_path)
    try:
        runtime = FakeRuntime()
        runtime.fs_lines_result = {
            "content_b64": base64.b64encode(b"3\n4\n5\n").decode("ascii"),
            "bytes": 6, "start_line": 3, "end_line": 5,
            "total_lines": 10, "total_bytes": 20, "has_more": False,
        }
        manager = WorkspaceManager(store, runtime)
        principal = FakePrincipal()
        result = manager.execute(principal, "workspace_read_file", {
            "path": "notes.txt", "start_line": 3, "end_line": 5,
        })
        fs_lines_call = next(call for call in runtime.calls if call[1] == "fs_lines")
        assert fs_lines_call[2] == {"path": "notes.txt", "start_line": 3, "end_line": 5, "max_bytes": MAX_FILE_BYTES}
        assert result["data"]["start_line"] == 3
        assert result["data"]["end_line"] == 5
    finally:
        store.close()


def test_read_file_offset_with_start_line_is_invalid_arguments(tmp_path: Path):
    store = _store(tmp_path)
    try:
        manager = WorkspaceManager(store, FakeRuntime())
        principal = FakePrincipal()
        with pytest.raises(WorkspaceError) as failure:
            manager.execute(principal, "workspace_read_file", {
                "path": "notes.txt", "offset": 0, "start_line": 1,
            })
        assert failure.value.reason == "invalid_arguments"
    finally:
        store.close()


def test_read_file_tail_lines_with_start_line_is_invalid_arguments(tmp_path: Path):
    store = _store(tmp_path)
    try:
        manager = WorkspaceManager(store, FakeRuntime())
        principal = FakePrincipal()
        with pytest.raises(WorkspaceError) as failure:
            manager.execute(principal, "workspace_read_file", {
                "path": "notes.txt", "tail_lines": 5, "start_line": 1,
            })
        assert failure.value.reason == "invalid_arguments"
    finally:
        store.close()


def test_read_file_plain_read_sends_todays_fs_read_arguments_and_adds_fs_stat(tmp_path: Path):
    store = _store(tmp_path)
    try:
        runtime = FakeRuntime()
        runtime.fs_stat_result = {"kind": "file", "size": 42}
        manager = WorkspaceManager(store, runtime)
        principal = FakePrincipal()
        manager.execute(principal, "workspace_read_file", {"path": "notes.txt"})
        fs_read_call = next(call for call in runtime.calls if call[1] == "fs_read")
        # Snapshot: the arguments sent to fs_read itself are unchanged by A3.
        assert fs_read_call[2] == {"path": "notes.txt", "offset": 0, "max_bytes": MAX_FILE_BYTES, "binary": False}
        fs_stat_call = next(call for call in runtime.calls if call[1] == "fs_stat")
        assert fs_stat_call[2] == {"path": "notes.txt"}
    finally:
        store.close()


# ---------------------------------------------------------------------------
# A3: workspace_get_job tail_lines and has_more_stdout/has_more_stderr
# ---------------------------------------------------------------------------


def test_get_job_tail_lines_sent_to_broker_and_response_aliases_has_more(tmp_path: Path):
    store = _store(tmp_path)
    try:
        runtime = FakeRuntime()
        runtime.job_get_result = {
            "state": "succeeded", "stdout": "", "stderr": "",
            "stdout_has_more": True, "stderr_has_more": False,
        }
        manager = WorkspaceManager(store, runtime)
        principal = FakePrincipal()
        manager.execute(principal, "workspace_start_job", {"argv": ["true"]})
        result = manager.execute(principal, "workspace_get_job", {"job_id": "job-1", "tail_lines": 2})
        job_get_call = next(call for call in runtime.calls if call[1] == "job_get")
        assert job_get_call[2]["tail_lines"] == 2
        assert result["job"]["stdout_has_more"] is True
        assert result["job"]["has_more_stdout"] is True
        assert result["job"]["stderr_has_more"] is False
        assert result["job"]["has_more_stderr"] is False
    finally:
        store.close()


# ---------------------------------------------------------------------------
# A4: quota_remaining_bytes / quota_warning
# ---------------------------------------------------------------------------


def test_summary_quota_fields_flip_at_the_configured_warning_threshold(tmp_path: Path):
    store = _store(tmp_path)
    try:
        runtime = FakeRuntime()
        # 12.18.3 (LIVE BUG): apparent bytes come exclusively from fs_usage
        # now; allocated is unaffected and still comes from inspect.
        runtime.inspect_result = {"measured_allocated_bytes": 100}
        runtime.fs_usage_result = {"entries": [], "truncated": False, "total_bytes": 100}
        manager = WorkspaceManager(store, runtime, quota_bytes=1000)
        principal = FakePrincipal()
        # 12.18.3: self._apparent_measured_at starts empty for a brand-new
        # Workspace, so the very first mutation's _measure_after_mutation
        # already measures through fs_usage -- no clock advance needed to
        # get past a stale admission-time value the way the old
        # record.measured_at-driven staleness check required.
        first = manager.execute(principal, "workspace_write_file", {"path": "a", "text": "x"})
        # Default warning_threshold_percent is 80: 100/1000 = 10% is not a warning.
        assert first["workspace"]["quota_remaining_bytes"] == 900
        assert first["workspace"]["quota_warning"] is False
        store.update_settings(0, {"warning_threshold_percent": 5})
        # _quota_fields reads the warning-threshold setting fresh on every
        # call, so a configuration change is visible immediately and does
        # not itself require a re-measurement.
        second = manager.execute(principal, "workspace_make_directory", {"path": "later"})
        # 100/1000 = 10% >= the now-configured 5% warning threshold.
        assert second["workspace"]["quota_remaining_bytes"] == 900
        assert second["workspace"]["quota_warning"] is True
    finally:
        store.close()


def test_summary_quota_fields_are_null_and_false_when_unmeasured(tmp_path: Path):
    store = _store(tmp_path)
    try:
        runtime = FakeRuntime()
        runtime.inspect_raises = True
        manager = WorkspaceManager(store, runtime, quota_bytes=1000)
        principal = FakePrincipal()
        result = manager.execute(principal, "workspace_write_file", {"path": "a", "text": "x"})
        assert result["workspace"]["measured_apparent_bytes"] is None
        assert result["workspace"]["quota_remaining_bytes"] is None
        assert result["workspace"]["quota_warning"] is False
    finally:
        store.close()


# ---------------------------------------------------------------------------
# A4: stop_idle / stop record last_auto_action
# ---------------------------------------------------------------------------


def test_stop_idle_records_idle_stop(tmp_path: Path):
    store = _store(tmp_path)
    clock = FakeClock()
    try:
        runtime = FakeRuntime()
        manager = WorkspaceManager(store, runtime, idle_seconds=1, clock=clock)
        principal = FakePrincipal()
        manager.execute(principal, "workspace_write_file", {"path": "a", "text": "x"})
        clock.now += timedelta(seconds=2)
        stopped = manager.stop_idle(now=clock.now)
        workspace_id = store.get_by_principal(principal.principal_id).workspace_id
        assert stopped == [workspace_id]
        record = store.get(workspace_id)
        assert record.last_auto_action == "idle_stop"
        assert record.last_auto_action_at is not None
    finally:
        store.close()


def test_stop_emergency_true_records_emergency_stop(tmp_path: Path):
    store = _store(tmp_path)
    try:
        runtime = FakeRuntime()
        manager = WorkspaceManager(store, runtime)
        principal = FakePrincipal()
        manager.execute(principal, "workspace_write_file", {"path": "a", "text": "x"})
        result = manager.stop(principal, emergency=True)
        assert result["workspace"]["state"] == "stopped"
        workspace_id = store.get_by_principal(principal.principal_id).workspace_id
        record = store.get(workspace_id)
        assert record.last_auto_action == "emergency_stop"
    finally:
        store.close()


def test_stop_without_emergency_records_admin_stop(tmp_path: Path):
    store = _store(tmp_path)
    try:
        runtime = FakeRuntime()
        manager = WorkspaceManager(store, runtime)
        principal = FakePrincipal()
        manager.execute(principal, "workspace_write_file", {"path": "a", "text": "x"})
        manager.stop(principal)
        workspace_id = store.get_by_principal(principal.principal_id).workspace_id
        record = store.get(workspace_id)
        assert record.last_auto_action == "admin_stop"
    finally:
        store.close()


# ---------------------------------------------------------------------------
# A4: measure-after-mutation
# ---------------------------------------------------------------------------


def _spy_on_measure_after_mutation(manager: WorkspaceManager) -> list[str]:
    """Record every call to ``_measure_after_mutation`` without disturbing
    its real behavior.  ``execute()`` already routes through the existing,
    unrelated per-call health check in ``_ensure`` (which calls "inspect" on
    every call for an already-running Workspace and refreshes measured_at
    from it, independent of A4) -- that pre-existing behavior confounds a
    raw broker "inspect" call count taken across a full ``execute()`` call,
    so the wiring itself is verified here by spying directly on the new
    hook, and the hook's own staleness logic is verified in isolation below.
    """
    calls: list[str] = []
    original = manager._measure_after_mutation

    def spy(workspace_id: str) -> None:
        calls.append(workspace_id)
        original(workspace_id)

    manager._measure_after_mutation = spy  # type: ignore[method-assign]
    return calls


def test_execute_calls_measure_after_mutation_once_for_a_write(tmp_path: Path):
    store = _store(tmp_path)
    try:
        manager = WorkspaceManager(store, FakeRuntime())
        principal = FakePrincipal()
        calls = _spy_on_measure_after_mutation(manager)
        manager.execute(principal, "workspace_write_file", {"path": "a", "text": "x"})
        assert len(calls) == 1
    finally:
        store.close()


def test_execute_calls_measure_after_mutation_once_for_job_start(tmp_path: Path):
    store = _store(tmp_path)
    try:
        manager = WorkspaceManager(store, FakeRuntime())
        principal = FakePrincipal()
        calls = _spy_on_measure_after_mutation(manager)
        manager.execute(principal, "workspace_start_job", {"argv": ["true"]})
        assert len(calls) == 1
    finally:
        store.close()


def test_execute_never_calls_measure_after_mutation_for_a_read(tmp_path: Path):
    store = _store(tmp_path)
    try:
        manager = WorkspaceManager(store, FakeRuntime())
        principal = FakePrincipal()
        # Admit the Workspace first (an ordinary write) with no spy attached,
        # then attach the spy and prove the read that follows never calls it.
        manager.execute(principal, "workspace_write_file", {"path": "a", "text": "x"})
        calls = _spy_on_measure_after_mutation(manager)
        manager.execute(principal, "workspace_read_file", {"path": "a"})
        assert calls == []
    finally:
        store.close()


def test_measure_after_mutation_with_stale_measurement_calls_inspect_once(tmp_path: Path):
    # Unit-tests _measure_after_mutation directly against a metadata row
    # created without going through _admit/_ensure, so the assertion is
    # isolated from _ensure's own unrelated per-call "inspect" health check.
    store = _store(tmp_path)
    clock = FakeClock()
    try:
        runtime = FakeRuntime()
        # 12.18.3 (LIVE BUG): apparent bytes come exclusively from fs_usage;
        # allocated is unaffected and still comes from inspect.
        runtime.inspect_result = {"measured_allocated_bytes": 10}
        runtime.fs_usage_result = {"entries": [], "truncated": False, "total_bytes": 10}
        manager = WorkspaceManager(store, runtime, clock=clock)
        record = store.create("p1", None, "Workspace", now=clock.now.isoformat(timespec="seconds"), quota_bytes=1000, retention_days=30)
        assert record.measured_at is None
        # 12.18.3: staleness is decided from self._apparent_measured_at, not
        # record.measured_at -- a workspace never measured through fs_usage
        # is stale regardless of what the record's own measured_at says.
        assert manager._apparent_measured_at.get(record.workspace_id) is None
        manager._measure_after_mutation(record.workspace_id)
        assert runtime.inspect_call_count() == 1
        updated = store.get(record.workspace_id)
        assert updated.measured_apparent_bytes == 10
        assert updated.usage_status == "fresh"
    finally:
        store.close()


def test_measure_after_mutation_with_fresh_measurement_calls_inspect_zero_times(tmp_path: Path):
    store = _store(tmp_path)
    clock = FakeClock()
    try:
        runtime = FakeRuntime()
        runtime.inspect_result = {"measured_allocated_bytes": 10}
        runtime.fs_usage_result = {"entries": [], "truncated": False, "total_bytes": 10}
        manager = WorkspaceManager(store, runtime, clock=clock)
        record = store.create("p1", None, "Workspace", now=clock.now.isoformat(timespec="seconds"), quota_bytes=1000, retention_days=30)
        manager._measure_after_mutation(record.workspace_id)
        assert runtime.inspect_call_count() == 1
        # 59s later is still inside the 60s freshness window (measured from
        # self._apparent_measured_at, the last time fs_usage actually
        # supplied a trusted apparent-bytes sample).
        clock.now += timedelta(seconds=59)
        manager._measure_after_mutation(record.workspace_id)
        assert runtime.inspect_call_count() == 1
        # 61s later (>= 60s) is stale again.
        clock.now += timedelta(seconds=2)
        manager._measure_after_mutation(record.workspace_id)
        assert runtime.inspect_call_count() == 2
    finally:
        store.close()


def test_measure_after_mutation_inspect_failure_is_logged_and_never_raises(tmp_path: Path):
    store = _store(tmp_path)
    clock = FakeClock()
    try:
        runtime = FakeRuntime()
        runtime.inspect_raises = True
        manager = WorkspaceManager(store, runtime, clock=clock)
        record = store.create("p1", None, "Workspace", now=clock.now.isoformat(timespec="seconds"), quota_bytes=1000, retention_days=30)
        # Must not raise -- a measurement failure never fails the caller.
        manager._measure_after_mutation(record.workspace_id)
        assert runtime.inspect_call_count() == 1
    finally:
        store.close()


# ---------------------------------------------------------------------------
# A4: workspace_info usage_by_directory
# ---------------------------------------------------------------------------


def test_workspace_info_includes_usage_by_directory_from_fs_usage(tmp_path: Path):
    store = _store(tmp_path)
    try:
        runtime = FakeRuntime()
        runtime.fs_usage_result = {
            "entries": [{"path": "/workspace/big", "bytes": 5000}, {"path": "/workspace/small", "bytes": 10}],
            "truncated": False,
        }
        manager = WorkspaceManager(store, runtime)
        principal = FakePrincipal()
        # Admit the Workspace and bring it to "running" first.
        manager.execute(principal, "workspace_write_file", {"path": "a", "text": "x"})
        result = manager.info(principal)
        assert result["workspace"]["usage_by_directory"] == [
            {"path": "/workspace/big", "bytes": 5000}, {"path": "/workspace/small", "bytes": 10},
        ]
        assert "usage_by_directory_error" not in result["workspace"]
        fs_usage_call = next(call for call in runtime.calls if call[1] == "fs_usage")
        assert fs_usage_call[2] == {"path": "/workspace"}
    finally:
        store.close()


def test_workspace_info_reports_usage_by_directory_error_on_failure(tmp_path: Path):
    store = _store(tmp_path)
    try:
        class FailingUsageRuntime(FakeRuntime):
            def call(self, workspace_id, operation, arguments, **kwargs):
                if operation == "fs_usage":
                    self.calls.append((workspace_id, operation, dict(arguments), kwargs))
                    raise WorkspaceError("runtime_unavailable", "fake fs_usage failure")
                return super().call(workspace_id, operation, arguments, **kwargs)

        runtime = FailingUsageRuntime()
        manager = WorkspaceManager(store, runtime)
        principal = FakePrincipal()
        manager.execute(principal, "workspace_write_file", {"path": "a", "text": "x"})
        result = manager.info(principal)
        assert "usage_by_directory" not in result["workspace"]
        assert result["workspace"]["usage_by_directory_error"] == "runtime_unavailable"
    finally:
        store.close()


# ---------------------------------------------------------------------------
# 12.18.2 LIVE BUG FIXES: quota fields were computed from the SDK volume's
# used_bytes, which reads 0 on kei even with real files in the Workspace.
# fs_usage (guest-side `du`) is the truth; refresh_usage() and info() must
# both prefer it, and info() must never let the broker's raw inspect values
# overwrite the record refresh_usage() just measured.
# ---------------------------------------------------------------------------


def test_refresh_usage_prefers_fs_usage_total_over_inspect_used_bytes(tmp_path: Path):
    store = _store(tmp_path)
    try:
        runtime = FakeRuntime()
        # The SDK's used_bytes reads 0 -- exactly the live kei bug.
        runtime.inspect_result = {"measured_allocated_bytes": 0, "measured_apparent_bytes": 0}
        runtime.fs_usage_result = {
            "entries": [{"path": "/workspace/big", "bytes": 370000}],
            "truncated": False, "total_bytes": 370000,
        }
        manager = WorkspaceManager(store, runtime, quota_bytes=500000)
        record = store.create(
            "p1", None, "Workspace", now="2026-01-01T00:00:00+00:00",
            quota_bytes=500000, retention_days=30,
        )
        result = manager.refresh_usage(record.workspace_id)
        assert result["measured_apparent_bytes"] == 370000
        updated = store.get(record.workspace_id)
        assert updated.measured_apparent_bytes == 370000
        quota_fields = manager._quota_fields(updated)
        assert quota_fields["quota_remaining_bytes"] == 500000 - 370000
        fs_usage_call = next(call for call in runtime.calls if call[1] == "fs_usage")
        assert fs_usage_call[2] == {"path": "/workspace"}
    finally:
        store.close()


def test_refresh_usage_fs_usage_failure_keeps_the_previously_stored_value(tmp_path: Path):
    # 12.18.3 (LIVE BUG): a plain inspect's own apparent-bytes value is NEVER
    # trusted, whether or not fs_usage succeeds -- so "keeps the inspect
    # value" (12.18.2's naming) is no longer the right description. On an
    # fs_usage failure, refresh_usage must leave whatever was already
    # STORED untouched, never move to inspect's own number.
    store = _store(tmp_path)
    try:
        class FailingUsageRuntime(FakeRuntime):
            def __init__(self):
                super().__init__()
                self.fail_fs_usage = False

            def call(self, workspace_id, operation, arguments, **kwargs):
                if operation == "fs_usage" and self.fail_fs_usage:
                    self.calls.append((workspace_id, operation, dict(arguments), kwargs))
                    raise WorkspaceError("runtime_unavailable", "fake fs_usage failure")
                return super().call(workspace_id, operation, arguments, **kwargs)

        runtime = FailingUsageRuntime()
        runtime.inspect_result = {"measured_allocated_bytes": 42}
        runtime.fs_usage_result = {"entries": [], "truncated": False, "total_bytes": 42}
        manager = WorkspaceManager(store, runtime, quota_bytes=1000)
        record = store.create(
            "p1", None, "Workspace", now="2026-01-01T00:00:00+00:00",
            quota_bytes=1000, retention_days=30,
        )
        # First call: fs_usage succeeds and establishes a real stored value.
        first = manager.refresh_usage(record.workspace_id)
        assert first["measured_apparent_bytes"] == 42

        # Second call: fs_usage fails. Even though inspect's own
        # measured_apparent_bytes is not set on this fake, the point is that
        # it would be ignored either way -- the previously STORED value
        # (42) must survive untouched, and the call never raises out to the
        # caller of refresh_usage.
        runtime.fail_fs_usage = True
        second = manager.refresh_usage(record.workspace_id)
        assert second["measured_apparent_bytes"] == 42
        updated = store.get(record.workspace_id)
        assert updated.measured_apparent_bytes == 42
    finally:
        store.close()


def test_workspace_info_matches_the_record_it_just_measured_with_one_fs_usage_call(tmp_path: Path):
    store = _store(tmp_path)
    try:
        runtime = FakeRuntime()
        runtime.inspect_result = {"measured_allocated_bytes": 0, "measured_apparent_bytes": 0}
        runtime.fs_usage_result = {
            "entries": [{"path": "/workspace/big", "bytes": 370000}],
            "truncated": False, "total_bytes": 370000,
        }
        manager = WorkspaceManager(store, runtime, quota_bytes=500000)
        principal = FakePrincipal()
        # Admit the Workspace and bring it to "running" first.
        manager.execute(principal, "workspace_write_file", {"path": "a", "text": "x"})
        calls_before_info = len(runtime.calls)
        result = manager.info(principal)["workspace"]
        workspace_id = store.get_by_principal(principal.principal_id).workspace_id
        record = store.get(workspace_id)
        # info() must report exactly what the record now holds -- not the
        # broker's raw (and wrong) inspect values overwriting it.
        assert result["measured_apparent_bytes"] == record.measured_apparent_bytes == 370000
        assert result["usage_status"] == record.usage_status == "fresh"
        assert result["quota_remaining_bytes"] == 500000 - 370000
        # Reusing the one fs_usage call for both the measurement and
        # usage_by_directory means exactly one is made per info() call.
        new_calls = runtime.calls[calls_before_info:]
        fs_usage_calls = [call for call in new_calls if call[1] == "fs_usage"]
        assert len(fs_usage_calls) == 1
        assert result["usage_by_directory"] == [{"path": "/workspace/big", "bytes": 370000}]
    finally:
        store.close()


# ---------------------------------------------------------------------------
# 12.18.3 LIVE BUG FIX: _ensure() calls _apply_runtime_measurement with a
# plain broker inspect payload on EVERY admitted call, and inspect's
# apparent bytes come from the SDK volume's used_bytes, which is always 0 on
# kei. That overwrote a correct fs_usage-derived value with 0 on the very
# next tool call after workspace_info reported it correctly, and it also
# meant record.measured_at (stamped by every such call) never looked stale
# to _measure_after_mutation. _apply_runtime_measurement now only accepts an
# apparent-bytes sample when the caller marks it apparent_source="fs_usage";
# staleness is tracked separately in WorkspaceManager._apparent_measured_at.
# ---------------------------------------------------------------------------


def test_apply_runtime_measurement_ignores_untrusted_inspect_apparent_bytes(tmp_path: Path):
    # (a) A plain inspect payload (no apparent_source) reporting 0 must not
    # overwrite a stored apparent value of 331776 -- exactly the live bug:
    # _ensure() calling this on every admitted call zeroed out a correct
    # fs_usage-derived measurement on the very next tool call.
    store = _store(tmp_path)
    try:
        manager = WorkspaceManager(store, FakeRuntime(), quota_bytes=1000000)
        record = store.create(
            "p1", None, "Workspace", now="2026-01-01T00:00:00+00:00",
            quota_bytes=1000000, retention_days=30,
        )
        store.update_measurement(
            record.workspace_id, allocated_bytes=331776, apparent_bytes=331776,
            measured_at="2026-01-01T00:00:00+00:00", usage_status="fresh",
        )
        record = store.get(record.workspace_id)
        assert record.measured_apparent_bytes == 331776

        updated = manager._apply_runtime_measurement(record, {
            "state": "running", "measured_allocated_bytes": 0, "measured_apparent_bytes": 0,
        })
        assert updated.measured_apparent_bytes == 331776
    finally:
        store.close()


def test_apply_runtime_measurement_with_no_allocated_value_preserves_usage_status_when_apparent_is_already_stored(tmp_path: Path):
    # 12.18.4 (LIVE BUG): a plain inspect response carrying no allocated
    # value either used to stamp usage_status "unknown" (and, via the
    # response projection, a null measured_allocated_bytes) unconditionally
    # -- even when the record already holds a trusted (fs_usage-derived)
    # apparent sample, which 12.18.3 made the ordinary admitted-call case.
    # That took a receipt with correct, fresh apparent bytes and stamped it
    # unknown again one call later.
    store = _store(tmp_path)
    try:
        manager = WorkspaceManager(store, FakeRuntime(), quota_bytes=1000000)
        record = store.create(
            "p1", None, "Workspace", now="2026-01-01T00:00:00+00:00",
            quota_bytes=1000000, retention_days=30,
        )
        store.update_measurement(
            record.workspace_id, allocated_bytes=331776, apparent_bytes=331776,
            measured_at="2026-01-01T00:00:00+00:00", usage_status="fresh",
        )
        record = store.get(record.workspace_id)
        assert record.usage_status == "fresh"
        assert record.measured_apparent_bytes == 331776

        # No byte fields at all -- the exact shape a plain inspect with no
        # allocated value takes once the apparent_source gate has already
        # stripped its untrusted apparent value out.
        updated = manager._apply_runtime_measurement(record, {"state": "running"})
        assert updated.usage_status == "fresh"
        assert updated.measured_apparent_bytes == 331776

        # A record with no stored apparent value either still goes to
        # "unknown" -- there is genuinely nothing fresh to describe.
        other = store.create(
            "p2", None, "Workspace", now="2026-01-01T00:00:00+00:00",
            quota_bytes=1000000, retention_days=30,
        )
        assert other.measured_apparent_bytes is None
        updated_other = manager._apply_runtime_measurement(other, {"state": "running"})
        assert updated_other.usage_status == "unknown"
    finally:
        store.close()


def test_apply_runtime_measurement_accepts_apparent_bytes_marked_fs_usage(tmp_path: Path):
    # (b) An observed dict that DOES carry the trust marker is accepted.
    store = _store(tmp_path)
    try:
        manager = WorkspaceManager(store, FakeRuntime(), quota_bytes=1000000)
        record = store.create(
            "p1", None, "Workspace", now="2026-01-01T00:00:00+00:00",
            quota_bytes=1000000, retention_days=30,
        )
        updated = manager._apply_runtime_measurement(record, {
            "state": "running", "apparent_source": "fs_usage", "measured_apparent_bytes": 500,
        })
        assert updated.measured_apparent_bytes == 500
    finally:
        store.close()


def test_measure_after_mutation_through_execute_measures_once_per_60s_window(tmp_path: Path):
    # (c) Two mutations 10s apart through execute() make exactly one
    # fs_usage call between them (the first primes self._apparent_measured_at
    # and the second, still inside the 60s window, is not stale); a third
    # mutation at 70s (60s past the first measurement) makes a second.
    store = _store(tmp_path)
    clock = FakeClock()
    try:
        runtime = FakeRuntime()
        runtime.inspect_result = {"measured_allocated_bytes": 0}
        runtime.fs_usage_result = {"entries": [], "truncated": False, "total_bytes": 0}
        manager = WorkspaceManager(store, runtime, clock=clock)
        principal = FakePrincipal()

        manager.execute(principal, "workspace_write_file", {"path": "a", "text": "x"})
        fs_usage_calls_after_first = len([c for c in runtime.calls if c[1] == "fs_usage"])
        assert fs_usage_calls_after_first == 1

        clock.now += timedelta(seconds=10)
        manager.execute(principal, "workspace_make_directory", {"path": "b"})
        fs_usage_calls_after_second = len([c for c in runtime.calls if c[1] == "fs_usage"])
        assert fs_usage_calls_after_second == 1

        clock.now += timedelta(seconds=60)
        manager.execute(principal, "workspace_make_directory", {"path": "c"})
        fs_usage_calls_after_third = len([c for c in runtime.calls if c[1] == "fs_usage"])
        assert fs_usage_calls_after_third == 2
    finally:
        store.close()


def test_mutation_receipt_reports_fs_usage_total_even_though_ensure_inspected_zero(tmp_path: Path):
    # (d) _ensure()'s own admission-time inspect reports 0 (the SDK bug);
    # the mutation's receipt must still carry the fs_usage-derived total,
    # not the 0 that _ensure() saw moments earlier.
    store = _store(tmp_path)
    try:
        runtime = FakeRuntime()
        runtime.inspect_result = {"measured_allocated_bytes": 0, "measured_apparent_bytes": 0}
        runtime.fs_usage_result = {"entries": [], "truncated": False, "total_bytes": 331776}
        manager = WorkspaceManager(store, runtime, quota_bytes=1000000)
        principal = FakePrincipal()

        result = manager.execute(principal, "workspace_write_file", {"path": "a", "text": "x"})
        assert result["workspace"]["measured_apparent_bytes"] == 331776
        assert result["workspace"]["quota_remaining_bytes"] == 1000000 - 331776
    finally:
        store.close()
