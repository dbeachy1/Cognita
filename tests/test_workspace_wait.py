"""A1/A2 (DESIGN-12.18-WORKSPACE-NEXT-FEATURES.md §3.1, §3.2): workspace_start_job
and workspace_get_job's run-and-wait (`wait_ms`) and text/base64 job output
(`output_encoding`, `strip_ansi`).

Every wait test here injects both the clock and the sleep callable
(WorkspaceManager.__init__'s new ``sleep`` parameter), so the 0.5s poll
cadence never depends on a real timer: the fake sleep advances the fake
clock by exactly the amount it was asked to sleep, which is what lets the
``waited_ms`` arithmetic below be asserted exactly rather than approximately.
"""

from __future__ import annotations

import base64
import threading
import time
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
    principal_id = "22222222-2222-4222-8222-222222222222"
    surface_id = "surface-wait"


class FakeClock:
    """Paired clock + sleep: ``sleep(s)`` advances ``now`` by exactly ``s``
    seconds and records the call, so a test can assert both the elapsed
    ``waited_ms`` and the exact number/size of poll intervals _wait_for_job
    took, with no real time elapsing."""

    def __init__(self, start: datetime | None = None):
        self.now = start or datetime(2026, 1, 1, tzinfo=UTC)
        self.sleep_calls: list[float] = []

    def __call__(self) -> datetime:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleep_calls.append(seconds)
        self.now += timedelta(seconds=seconds)


class ScriptedRuntime:
    """job_start always answers "running"; job_get answers ``job_get_script``
    items in order (a dict result, or an Exception instance to raise). Any
    other operation (``ensure``, ``fs_read``, ...) answers a bland success so
    a test exercising a second tool alongside a wait does not need its own
    fake."""

    def __init__(self, job_get_script=None):
        self.calls: list[tuple] = []
        self.job_get_script = list(job_get_script or [])

    def call(self, workspace_id, operation, arguments, **kwargs):
        self.calls.append((workspace_id, operation, dict(arguments), kwargs))
        if operation == "job_start":
            return {"job_id": "job-1", "state": "running"}
        if operation == "job_get":
            if not self.job_get_script:
                raise AssertionError("job_get called more times than the test scripted")
            item = self.job_get_script.pop(0)
            if isinstance(item, Exception):
                raise item
            return dict(item)
        if operation == "fs_read":
            return {"content": "", "size_bytes": 0, "encoding": "text"}
        return {"ok": True}


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def test_start_job_wait_ms_returns_exited_with_two_polls(tmp_path: Path):
    """start_job argv=[...] wait_ms=10000 against a runtime scripted
    running, running, succeeded returns exited with waited_ms == 1000.

    Arithmetic: job_start's own immediate reply is already "running" -- that
    is the pseudocode's un-polled initial check in _wait_for_job, and it
    consumes no sleep. The wait loop then polls job_get twice at the fixed
    0.5s cadence: poll 1 answers "running" (clock 0 -> 500ms), poll 2
    answers "succeeded" (clock 500 -> 1000ms), so exactly two sleeps elapse
    before the terminal state is observed, for waited_ms == 500 + 500 ==
    1000. The scripted "running, running, succeeded" in the design's own
    prose is this same sequence counting the initial state once.
    """
    store = WorkspaceMetadataStore(tmp_path / "workspace.sqlite3")
    try:
        clock = FakeClock()
        runtime = ScriptedRuntime([
            {"state": "running", "stdout": "", "stderr": ""},
            {"state": "succeeded", "stdout": "", "stderr": "", "exit_code": 0},
        ])
        manager = WorkspaceManager(store, runtime, clock=clock, sleep=clock.sleep)
        principal = FakePrincipal()
        response = manager.execute(principal, "workspace_start_job", {"argv": ["sleep", "3"], "wait_ms": 10000})
        assert response["wake_reason"] == "exited"
        assert response["waited_ms"] == 1000
        assert response["job"]["state"] == "succeeded"
        assert clock.sleep_calls == [0.5, 0.5]
        job_get_calls = [c for c in runtime.calls if c[1] == "job_get"]
        assert len(job_get_calls) == 2
        row = store._db.execute(
            "SELECT state FROM workspace_jobs WHERE job_id=?", (response["job"]["job_id"],)
        ).fetchone()
        assert row["state"] == "succeeded"
    finally:
        store.close()


def test_wait_ms_timeout_returns_running_state_and_job_id_is_usable(tmp_path: Path):
    store = WorkspaceMetadataStore(tmp_path / "workspace.sqlite3")
    try:
        clock = FakeClock()
        runtime = ScriptedRuntime([
            {"state": "running", "stdout": "", "stderr": ""},
            {"state": "running", "stdout": "", "stderr": ""},
            {"state": "running", "stdout": "", "stderr": ""},  # consumed by the follow-up get_job below
        ])
        manager = WorkspaceManager(store, runtime, clock=clock, sleep=clock.sleep)
        principal = FakePrincipal()
        response = manager.execute(principal, "workspace_start_job", {"argv": ["sleep", "3"], "wait_ms": 1000})
        assert response["wake_reason"] == "timeout"
        assert response["waited_ms"] == 1000
        assert response["job"]["state"] == "running"
        job_id = response["job"]["job_id"]
        row = store._db.execute("SELECT state FROM workspace_jobs WHERE job_id=?", (job_id,)).fetchone()
        assert row["state"] == "running"
        follow_up = manager.execute(principal, "workspace_get_job", {"job_id": job_id})
        assert follow_up["status"] == "success"
        assert follow_up["job"]["job_id"] == job_id
    finally:
        store.close()


def test_lock_is_free_during_wait(tmp_path: Path):
    """A plain read on the same Workspace, fired while a wait is in
    progress, returns immediately -- it does not queue behind the wait's
    per-Workspace lock. Proven with a real thread and a real
    threading.Event (the injected clock/sleep only cover _wait_for_job's own
    poll cadence; the blocking here happens inside the fake runtime's
    job_get, which execute() must have already released the lock before
    calling)."""
    store = WorkspaceMetadataStore(tmp_path / "workspace.sqlite3")
    try:
        block_job_get = threading.Event()
        entered_job_get = threading.Event()

        class BlockingRuntime:
            def __init__(self):
                self.calls = []

            def call(self, workspace_id, operation, arguments, **kwargs):
                self.calls.append((workspace_id, operation, dict(arguments), kwargs))
                if operation == "job_start":
                    return {"job_id": "job-1", "state": "running"}
                if operation == "job_get":
                    entered_job_get.set()
                    block_job_get.wait(5)
                    return {"state": "running", "stdout": "", "stderr": ""}
                if operation == "fs_read":
                    return {"content": "", "size_bytes": 0, "encoding": "text"}
                return {"ok": True}

        runtime = BlockingRuntime()
        # The fake clock/sleep are not under test here -- they only need to
        # let the wait loop converge fast in real time once unblocked, so
        # this test's own bounded joins are what is actually being checked
        # (a no-op sleep paired with the REAL wall clock would make the
        # loop's deadline check busy-spin for the full real wait_ms after
        # unblocking, which is unrelated to lock-freedom and just makes the
        # test slow/flaky).
        clock = FakeClock()
        manager = WorkspaceManager(store, runtime, clock=clock, sleep=clock.sleep)
        principal = FakePrincipal()

        result: dict = {}

        def run_wait():
            result["response"] = manager.execute(
                principal, "workspace_start_job", {"argv": ["sleep", "5"], "wait_ms": 5000},
            )

        waiter = threading.Thread(target=run_wait)
        waiter.start()
        assert entered_job_get.wait(2), "the wait never reached the blocking job_get call"

        started = time.monotonic()
        read_response = manager.execute(principal, "workspace_read_file", {"path": "a"})
        elapsed = time.monotonic() - started

        assert read_response["status"] == "success"
        assert elapsed < 1.0, f"workspace_read_file queued behind the wait ({elapsed:.3f}s)"

        block_job_get.set()
        waiter.join(5)
        assert not waiter.is_alive()
        assert result["response"]["wake_reason"] in {"exited", "timeout"}
    finally:
        store.close()


def test_workspace_deleted_mid_wait_returns_gone_state_lost(tmp_path: Path):
    store = WorkspaceMetadataStore(tmp_path / "workspace.sqlite3")
    try:
        clock = FakeClock()
        deleted_once = {"done": False}

        class DeletingRuntime:
            def __init__(self):
                self.calls = []

            def call(self, workspace_id, operation, arguments, **kwargs):
                self.calls.append((workspace_id, operation, dict(arguments), kwargs))
                if operation == "job_start":
                    return {"job_id": "job-1", "state": "running"}
                if operation == "job_get":
                    if not deleted_once["done"]:
                        deleted_once["done"] = True
                        # Simulate an emergency stop/delete racing the wait:
                        # the row disappears out from under _wait_for_job.
                        with store.transaction() as db:
                            db.execute("DELETE FROM workspaces WHERE workspace_id=?", (workspace_id,))
                    raise WorkspaceError("path_unavailable", "Workspace was not found")
                return {"ok": True}

        runtime = DeletingRuntime()
        manager = WorkspaceManager(store, runtime, clock=clock, sleep=clock.sleep)
        principal = FakePrincipal()
        response = manager.execute(principal, "workspace_start_job", {"argv": ["sleep", "3"], "wait_ms": 5000})
        assert response["status"] == "success"  # no exception propagated
        assert response["wake_reason"] == "gone"
        assert response["job"]["state"] == "lost"
    finally:
        store.close()


def test_get_job_default_call_is_byte_identical_to_today(tmp_path: Path):
    """Neither wait_ms nor output_encoding/strip_ansi is given: the response
    and the runtime call reproduce exactly what _job_get returned before A1/
    A2 -- no waited_ms/wake_reason/_encoding/_lossy fields, streams untouched."""
    store = WorkspaceMetadataStore(tmp_path / "workspace.sqlite3")
    try:
        runtime = ScriptedRuntime([{"state": "running", "stdout": _b64(b"hi"), "stderr": _b64(b"")}])
        manager = WorkspaceManager(store, runtime)
        principal = FakePrincipal()
        response = manager.execute(principal, "workspace_get_job", {"job_id": "job-1"})
        assert response["job"] == {"job_id": "job-1", "state": "running", "stdout": _b64(b"hi"), "stderr": _b64(b"")}
        assert "waited_ms" not in response
        assert "wake_reason" not in response
        job_get_calls = [c for c in runtime.calls if c[1] == "job_get"]
        assert len(job_get_calls) == 1
        assert job_get_calls[0][2] == {
            "job_id": "job-1", "stdout_offset": 0, "stderr_offset": 0, "max_bytes": MAX_FILE_BYTES,
        }
    finally:
        store.close()


@pytest.mark.parametrize("bad_wait", [True, -1, 55001])
def test_wait_ms_bad_values_are_rejected(tmp_path: Path, bad_wait):
    store = WorkspaceMetadataStore(tmp_path / "workspace.sqlite3")
    try:
        runtime = ScriptedRuntime([])
        manager = WorkspaceManager(store, runtime)
        principal = FakePrincipal()
        with pytest.raises(WorkspaceError) as excinfo:
            manager.execute(principal, "workspace_start_job", {"argv": ["true"], "wait_ms": bad_wait})
        assert excinfo.value.reason == "invalid_arguments"
        with pytest.raises(WorkspaceError) as excinfo_get:
            manager.execute(principal, "workspace_get_job", {"job_id": "job-1", "wait_ms": bad_wait})
        assert excinfo_get.value.reason == "invalid_arguments"
    finally:
        store.close()


def test_replay_of_start_job_with_wait_ms_returns_receipt_without_waiting(tmp_path: Path):
    store = WorkspaceMetadataStore(tmp_path / "workspace.sqlite3")
    try:
        clock = FakeClock()
        runtime = ScriptedRuntime([{"state": "succeeded", "stdout": "", "stderr": "", "exit_code": 0}])
        manager = WorkspaceManager(store, runtime, clock=clock, sleep=clock.sleep)
        principal = FakePrincipal()
        args = {"argv": ["sleep", "3"], "wait_ms": 5000, "idempotency_key": "start-wait-1"}
        first = manager.execute(principal, "workspace_start_job", dict(args))
        assert first["wake_reason"] == "exited"  # the first call genuinely waited
        assert first["job"]["state"] == "succeeded"

        second = manager.execute(principal, "workspace_start_job", dict(args))
        assert second["replayed"] is True
        assert "waited_ms" not in second
        assert "wake_reason" not in second
        # The idempotency store holds the un-waited start receipt (§3.1), so
        # the replay reports "running" -- the state at start_job time, not
        # "succeeded", which call #1 only learned by polling afterward.
        assert second["job"]["state"] == "running"

        job_start_calls = [c for c in runtime.calls if c[1] == "job_start"]
        job_get_calls = [c for c in runtime.calls if c[1] == "job_get"]
        assert len(job_start_calls) == 1
        assert len(job_get_calls) == 1  # only call #1's single poll; the replay never waits
    finally:
        store.close()


def test_identical_jobs_remain_broker_queryable_after_metadata_reopen(tmp_path: Path, caplog):
    """The job table is an admission/cache record, not runtime job history.

    A second identical command with a different idempotency key replaces the
    first terminal row under the legacy digest uniqueness constraint. Both
    broker jobs must still be readable, and a cache miss must not weaken the
    active-job guard or make an unknown broker job appear valid.
    """
    path = tmp_path / "workspace.sqlite3"

    class BrokerJobs:
        def __init__(self):
            self.calls = []
            self.jobs: dict[str, str] = {}

        def call(self, workspace_id, operation, arguments, **kwargs):
            self.calls.append((workspace_id, operation, dict(arguments), kwargs))
            if operation == "job_start":
                job_id = f"job-{len(self.jobs) + 1}"
                self.jobs[job_id] = "running"
                return {"job_id": job_id, "state": "running"}
            if operation == "job_get":
                job_id = arguments["job_id"]
                if job_id not in self.jobs:
                    raise WorkspaceError("path_unavailable", "broker job was not found")
                return {"job_id": job_id, "state": self.jobs[job_id], "stdout": "", "stderr": ""}
            if operation == "job_cancel":
                job_id = arguments["job_id"]
                if job_id not in self.jobs:
                    raise WorkspaceError("path_unavailable", "broker job was not found")
                return {"state": self.jobs[job_id]}
            if operation == "fs_usage":
                return {"entries": [], "truncated": False, "total_bytes": 0}
            return {"ok": True}

    runtime = BrokerJobs()
    store = WorkspaceMetadataStore(path)
    manager = WorkspaceManager(store, runtime)
    principal = FakePrincipal()
    command = {"argv": ["printf", "same command"]}

    first = manager.execute(
        principal, "workspace_start_job", {**command, "idempotency_key": "job-a-key"},
    )
    first_id = first["job"]["job_id"]
    replay = manager.execute(
        principal, "workspace_start_job", {**command, "idempotency_key": "job-a-key"},
    )
    assert replay["replayed"] is True
    assert replay["job"]["job_id"] == first_id
    assert len([call for call in runtime.calls if call[1] == "job_start"]) == 1

    # A genuinely active command still prevents another launch.
    with pytest.raises(WorkspaceError, match="already running"):
        manager.execute(
            principal, "workspace_start_job", {**command, "idempotency_key": "blocked-key"},
        )
    assert len([call for call in runtime.calls if call[1] == "job_start"]) == 1

    runtime.jobs[first_id] = "succeeded"
    second = manager.execute(
        principal, "workspace_start_job", {**command, "idempotency_key": "job-b-key"},
    )
    second_id = second["job"]["job_id"]
    assert second_id != first_id
    assert runtime.jobs[first_id] == "succeeded"

    # Reopening the metadata file retains the second receipt's idempotency
    # mapping, while the broker remains the source of both job results.
    workspace_id = second["workspace"]["workspace_id"]
    store.close()
    store = WorkspaceMetadataStore(path)
    manager = WorkspaceManager(store, runtime)
    try:
        replay_after_reopen = manager.execute(
            principal, "workspace_start_job", {**command, "idempotency_key": "job-b-key"},
        )
        assert replay_after_reopen["replayed"] is True
        assert replay_after_reopen["job"]["job_id"] == second_id
        assert len([call for call in runtime.calls if call[1] == "job_start"]) == 2

        with pytest.raises(WorkspaceError, match="already running"):
            manager.execute(
                principal, "workspace_start_job", {**command, "idempotency_key": "job-c-key"},
            )
        assert len([call for call in runtime.calls if call[1] == "job_start"]) == 2

        runtime.jobs[second_id] = "succeeded"
        with caplog.at_level("DEBUG", logger="cognita.workspace"):
            first_read = manager.execute(principal, "workspace_get_job", {"job_id": first_id})
            first_cancel = manager.execute(principal, "workspace_cancel_job", {"job_id": first_id})
        second_read = manager.execute(principal, "workspace_get_job", {"job_id": second_id})
        assert first_read["job"]["state"] == "succeeded"
        assert first_cancel["job"]["state"] == "succeeded"
        assert second_read["job"]["state"] == "succeeded"
        assert any(
            f"workspace_id={workspace_id}" in entry.getMessage()
            and f"job_id={first_id}" in entry.getMessage()
            for entry in caplog.records
        )

        for tool, arguments in (
            ("workspace_get_job", {"job_id": "unknown-job"}),
            ("workspace_cancel_job", {"job_id": "unknown-job"}),
        ):
            with pytest.raises(WorkspaceError) as excinfo:
                manager.execute(principal, tool, arguments)
            assert excinfo.value.reason == "path_unavailable"
    finally:
        store.close()


def test_waited_job_terminal_observation_is_best_effort_cache_update(tmp_path: Path):
    store = WorkspaceMetadataStore(tmp_path / "workspace.sqlite3")
    try:
        clock = FakeClock()
        runtime = ScriptedRuntime([{"state": "succeeded", "stdout": "", "stderr": ""}])
        manager = WorkspaceManager(store, runtime, clock=clock, sleep=clock.sleep)
        principal = FakePrincipal()
        started = manager.execute(principal, "workspace_start_job", {"argv": ["true"]})
        job_id = started["job"]["job_id"]
        record = store.get_by_principal(principal.principal_id)
        with store.transaction() as db:
            db.execute("DELETE FROM workspace_jobs WHERE workspace_id=? AND job_id=?", (record.workspace_id, job_id))

        response = manager._wait_for_job(
            record, job_id,
            {"job_id": job_id, "stdout_offset": 0, "stderr_offset": 0, "max_bytes": MAX_FILE_BYTES},
            1000, {"status": "success", "job": {"job_id": job_id, "state": "running"}},
            encoding="base64", strip_ansi=False, apply_encoding=False,
        )
        assert response["job"]["state"] == "succeeded"
        assert response["wake_reason"] == "exited"
    finally:
        store.close()


def test_start_job_output_encoding_without_wait_ms_is_rejected(tmp_path: Path):
    store = WorkspaceMetadataStore(tmp_path / "workspace.sqlite3")
    try:
        runtime = ScriptedRuntime([])
        manager = WorkspaceManager(store, runtime)
        principal = FakePrincipal()
        with pytest.raises(WorkspaceError) as excinfo:
            manager.execute(principal, "workspace_start_job", {"argv": ["true"], "output_encoding": "text"})
        assert excinfo.value.reason == "invalid_arguments"
        with pytest.raises(WorkspaceError) as excinfo2:
            manager.execute(principal, "workspace_start_job", {"argv": ["true"], "strip_ansi": True})
        assert excinfo2.value.reason == "invalid_arguments"
    finally:
        store.close()


def test_get_job_output_encoding_auto_falls_back_to_base64_only_for_the_invalid_stream(tmp_path: Path):
    store = WorkspaceMetadataStore(tmp_path / "workspace.sqlite3")
    try:
        stdout_bytes = b"\xff\xfe"  # not valid UTF-8
        stderr_bytes = b"clean"    # valid UTF-8
        # "running", not a terminal state: this job_id was never started via
        # workspace_start_job, so there is no workspace_jobs row for
        # update_job_state to find if _job_get saw a terminal state here.
        # Encoding is applied identically regardless of job state.
        runtime = ScriptedRuntime([{"state": "running", "stdout": _b64(stdout_bytes), "stderr": _b64(stderr_bytes)}])
        manager = WorkspaceManager(store, runtime)
        principal = FakePrincipal()
        response = manager.execute(principal, "workspace_get_job", {"job_id": "job-1", "output_encoding": "auto"})
        job = response["job"]
        assert job["stdout_encoding"] == "base64"
        assert job["stdout"] == _b64(stdout_bytes)  # base64 stream is never modified
        assert "stdout_lossy" not in job  # no _lossy field in auto mode
        assert job["stderr_encoding"] == "text"
        assert job["stderr"] == "clean"
    finally:
        store.close()


def test_get_job_output_encoding_text_replaces_invalid_bytes_and_flags_lossy(tmp_path: Path):
    store = WorkspaceMetadataStore(tmp_path / "workspace.sqlite3")
    try:
        stdout_bytes = b"\xff\xfe"
        runtime = ScriptedRuntime([{"state": "running", "stdout": _b64(stdout_bytes), "stderr": _b64(b"")}])
        manager = WorkspaceManager(store, runtime)
        principal = FakePrincipal()
        response = manager.execute(principal, "workspace_get_job", {"job_id": "job-1", "output_encoding": "text"})
        job = response["job"]
        assert job["stdout_encoding"] == "text"
        assert job["stdout"] == stdout_bytes.decode("utf-8", errors="replace")
        assert job["stdout_lossy"] is True
    finally:
        store.close()


def test_get_job_strip_ansi_removes_escape_sequences(tmp_path: Path):
    store = WorkspaceMetadataStore(tmp_path / "workspace.sqlite3")
    try:
        stdout_bytes = b"\x1b[31mred\x1b[0m"
        runtime = ScriptedRuntime([{"state": "running", "stdout": _b64(stdout_bytes), "stderr": _b64(b"")}])
        manager = WorkspaceManager(store, runtime)
        principal = FakePrincipal()
        response = manager.execute(
            principal, "workspace_get_job",
            {"job_id": "job-1", "output_encoding": "text", "strip_ansi": True},
        )
        assert response["job"]["stdout"] == "red"
    finally:
        store.close()


def test_generation_conflict_is_retried_once_and_then_continues_waiting(tmp_path: Path):
    """A broker restart advances its runtime generation mid-wait.
    _runtime_call already resets the stale expectation on this reason, and
    _wait_for_job re-reads the record and retries the SAME poll once,
    immediately (no extra sleep) -- the established _active_job_after_reconcile
    pattern -- rather than turning a transient restart into an error on a
    call that could simply keep waiting."""
    store = WorkspaceMetadataStore(tmp_path / "workspace.sqlite3")
    try:
        clock = FakeClock()
        runtime = ScriptedRuntime([
            WorkspaceError("generation_conflict", "runtime generation advanced", reset_runtime_generation=True),
            {"state": "succeeded", "stdout": "", "stderr": "", "exit_code": 0},
        ])
        manager = WorkspaceManager(store, runtime, clock=clock, sleep=clock.sleep)
        principal = FakePrincipal()
        response = manager.execute(principal, "workspace_start_job", {"argv": ["sleep", "3"], "wait_ms": 10000})
        assert response["wake_reason"] == "exited"
        assert response["job"]["state"] == "succeeded"
        # Only one real sleep elapsed: the retry after generation_conflict
        # happens immediately and never sleeps again on its own.
        assert clock.sleep_calls == [0.5]
        job_get_calls = [c for c in runtime.calls if c[1] == "job_get"]
        assert len(job_get_calls) == 2  # the failed poll plus its one retry
        row = store._db.execute(
            "SELECT state FROM workspace_jobs WHERE job_id=?", (response["job"]["job_id"],)
        ).fetchone()
        assert row["state"] == "succeeded"
    finally:
        store.close()


def test_persistent_generation_conflict_surfaces_after_one_retry(tmp_path: Path):
    """A generation_conflict that survives the single retry is today's
    behavior for a persistent conflict: the row is still running (not a
    Workspace-gone condition), so it surfaces to the caller exactly as it
    would from a plain, non-waiting job_get call -- and the retry is never
    looped past once."""
    store = WorkspaceMetadataStore(tmp_path / "workspace.sqlite3")
    try:
        clock = FakeClock()
        runtime = ScriptedRuntime([
            WorkspaceError("generation_conflict", "runtime generation advanced", reset_runtime_generation=True),
            WorkspaceError("generation_conflict", "runtime generation advanced again", reset_runtime_generation=True),
        ])
        manager = WorkspaceManager(store, runtime, clock=clock, sleep=clock.sleep)
        principal = FakePrincipal()
        with pytest.raises(WorkspaceError) as excinfo:
            manager.execute(principal, "workspace_start_job", {"argv": ["sleep", "3"], "wait_ms": 10000})
        assert excinfo.value.reason == "generation_conflict"
        job_get_calls = [c for c in runtime.calls if c[1] == "job_get"]
        assert len(job_get_calls) == 2  # the original poll plus its one retry, never looped further
    finally:
        store.close()
