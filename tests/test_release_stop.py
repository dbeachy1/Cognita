"""release.run's stop_check (DESIGN-WINDOWS-INSTALLER section 21.2): the Windows installer's "Skip
self-tests" button ends up here.

No test waits on the clock.  The fake child's output is a generator that blocks on an Event until the
fake ``terminate()`` or ``kill()`` sets it (the pipe closing under a real reader), ``stop_poll_s`` is 0,
and ``process.wait(timeout=10)`` on the fake answers at once.  A regression that never terminates the
child would block on that Event, so it carries a hang guard timeout that a correct run never reaches.
"""
from __future__ import annotations

import subprocess
import threading

import pytest
from test_release_py import release

HANG_GUARD_S = 30          # a guard for a signal that a correct run always sends; never a bet on time


class FakeStdout:
    """Yields ``lines``, then (``never_ends``) blocks until the child is stopped, as an open pipe does."""

    def __init__(self, lines, closed: threading.Event, never_ends: bool):
        self.lines, self.closed, self.never_ends = lines, closed, never_ends

    def __iter__(self):
        yield from self.lines
        if self.never_ends:
            assert self.closed.wait(timeout=HANG_GUARD_S), "the child was never terminated or killed"


class FakePopen:
    """Records terminate/kill/wait.  ``ignores_terminate``: wait(timeout=...) raises TimeoutExpired until kill()."""

    instances: list["FakePopen"] = []

    def __init__(self, command, *, lines=(), never_ends=False, ignores_terminate=False, exit_code=0, **kwargs):
        self.command, self.kwargs = command, kwargs
        self.closed = threading.Event()
        self.stdout = FakeStdout(list(lines), self.closed, never_ends)
        self.stdin = None
        self.calls: list = []
        self.ignores_terminate, self.exit_code = ignores_terminate, exit_code
        self.killed = False
        FakePopen.instances.append(self)

    def terminate(self):
        self.calls.append("terminate")
        if not self.ignores_terminate:
            self.closed.set()

    def kill(self):
        self.calls.append("kill")
        self.killed = True
        self.closed.set()

    def wait(self, timeout=None):
        self.calls.append(("wait", timeout))
        if timeout is not None and self.ignores_terminate and not self.killed:
            raise subprocess.TimeoutExpired(self.command, timeout)
        return -9 if self.calls.count("terminate") or self.killed else self.exit_code


@pytest.fixture
def log(tmp_path):
    return release.Log(tmp_path / "stop.log")


@pytest.fixture
def popen(monkeypatch):
    """Patch subprocess.Popen inside release.run with a factory the test configures."""
    FakePopen.instances = []
    config: dict = {}
    monkeypatch.setattr(release.subprocess, "Popen", lambda command, **kwargs: FakePopen(command, **kwargs, **config))
    return config


def log_text(log) -> str:
    return log.path.read_text(encoding="utf-8")


def test_a_stop_request_terminates_the_child_and_raises_stopped(popen, log):
    popen.update(never_ends=True)
    asked = []
    with pytest.raises(release.Stopped):
        release.run(["selftest", "live"], log=log, state="verify-failed",
                    stop_check=lambda: asked.append(1) or True, stop_poll_s=0)
    child = FakePopen.instances[0]
    assert child.calls == ["terminate", ("wait", 10)]          # terminated, waited up to 10 s, never killed
    assert asked == [1]                                        # true on the first call, so asked once
    text = log_text(log)
    assert "stopped on request: selftest" in text and "terminate sent to selftest" in text


def test_a_child_that_ignores_terminate_is_killed_after_the_ten_second_wait(popen, log):
    popen.update(never_ends=True, ignores_terminate=True)
    with pytest.raises(release.Stopped):
        release.run(["selftest"], log=log, state="verify-failed", stop_check=lambda: True, stop_poll_s=0)
    assert FakePopen.instances[0].calls == ["terminate", ("wait", 10), "kill", ("wait", None)]
    assert "still running after 10 s; killing it" in log_text(log)


def test_stopped_is_its_own_exception_not_a_release_error():
    assert not issubclass(release.Stopped, release.ReleaseError)


def test_a_stop_after_some_output_keeps_what_was_read_in_the_log(popen, log):
    popen.update(lines=["first\n", "second\n"], never_ends=True)
    seen = []

    def stop_check():
        seen.append(1)
        return "second" in log_text(log)          # true only once the second line was logged

    with pytest.raises(release.Stopped):
        release.run(["selftest"], log=log, state="verify-failed", stop_check=stop_check, stop_poll_s=0)
    assert "first" in log_text(log) and "second" in log_text(log)


def test_without_a_stop_request_the_output_is_read_to_the_end_and_the_exit_code_returned(popen, log):
    popen.update(lines=["a\n", "b\n", "c\n"], exit_code=0)
    asked = []
    code, tail = release.run(["selftest"], log=log, state="verify-failed",
                             stop_check=lambda: asked.append(1) or False, stop_poll_s=0)
    assert (code, tail) == (0, "a\nb\nc")
    assert len(asked) >= 3                                     # once per line at least (timeouts add more)
    assert FakePopen.instances[0].calls == [("wait", None)]    # no terminate, no kill


def test_a_failing_child_still_raises_release_error_with_a_stop_check(popen, log):
    popen.update(lines=["boom\n"], exit_code=3)
    with pytest.raises(release.ReleaseError) as info:
        release.run(["selftest"], log=log, state="verify-failed", stop_check=lambda: False, stop_poll_s=0)
    assert info.value.state == "verify-failed" and "boom" in str(info.value)


def test_a_stop_check_that_raises_is_logged_once_and_the_run_goes_on(popen, log):
    popen.update(lines=["a\n", "b\n"])

    def broken():
        raise RuntimeError("no skip file share")

    code, tail = release.run(["selftest"], log=log, state="verify-failed", stop_check=broken, stop_poll_s=0)
    assert (code, tail) == (0, "a\nb")
    assert log_text(log).count("stop_check raised RuntimeError: no skip file share") == 1


def test_without_stop_check_run_is_the_unchanged_path_and_starts_no_reader_thread(popen, log, monkeypatch):
    popen.update(lines=["x\n"])
    started = []
    monkeypatch.setattr(release.threading, "Thread", lambda *a, **k: started.append(1))
    code, tail = release.run(["selftest"], log=log, state="verify-failed")
    assert (code, tail) == (0, "x") and started == []
    assert FakePopen.instances[0].calls == [("wait", None)]


# --------------------------------------------------------------------------
# qa_release / run_live_selftest pass it through; test mode is restored on a stop
# --------------------------------------------------------------------------


def _qa_harness(monkeypatch, events):
    monkeypatch.setattr(release, "self_test_key", lambda repo: "synthetic-key")
    monkeypatch.setattr(release, "check_healthz", lambda *a, **k: events.append(("health", k.get("expect_test_mode"))) or {})
    monkeypatch.setattr(release, "expect_unauthorized", lambda *a: events.append(("key-rejected", None)))

    def fake_run(command, **kwargs):
        events.append(("compose", "test" if any("test-mode.override.yaml" in str(p) for p in command) else "normal"))
        return 0, ""

    monkeypatch.setattr(release, "run", fake_run)


def test_qa_restores_normal_mode_when_the_selftest_is_stopped(monkeypatch, tmp_path, log):
    events: list = []
    _qa_harness(monkeypatch, events)
    handed = {}

    def stopped_selftest(**kwargs):
        handed.update(kwargs)
        events.append(("selftest", None))
        raise release.Stopped("selftest was stopped on request")

    monkeypatch.setattr(release, "run_live_selftest", stopped_selftest)
    check = lambda: True                                                       # noqa: E731
    with pytest.raises(release.Stopped):
        release.qa_release(release.REPO_ROOT, release.TARGETS["test"], "13.3.0", tmp_path, log, stop_check=check)
    assert handed["stop_check"] is check
    assert events == [("health", None), ("compose", "test"), ("health", True), ("selftest", None),
                      ("compose", "normal"), ("health", False), ("key-rejected", None)]
    assert not (tmp_path / "test-mode.override.yaml").exists()


def test_qa_without_stop_check_hands_none_on(monkeypatch, tmp_path, log):
    events: list = []
    _qa_harness(monkeypatch, events)
    handed = {}
    monkeypatch.setattr(release, "run_live_selftest", lambda **kwargs: handed.update(kwargs))
    release.qa_release(release.REPO_ROOT, release.TARGETS["test"], "13.3.0", tmp_path, log)
    assert handed["stop_check"] is None


def test_the_live_selftest_hands_stop_check_to_run_only_when_there_is_one(monkeypatch, tmp_path, log):
    seen: list = []
    receipt = ('selftest_receipt={"schema": 1, "mode": "full", "result": "passed", "mandatory_ocr": "passed", '
               '"missing_file_parity": "passed", "canonical_log": "x.log"}')
    monkeypatch.setattr(release, "run", lambda command, **kwargs: seen.append(kwargs) or (0, receipt))
    common = dict(repo=tmp_path, target=release.TARGETS["test"], compose_files=[], key="k", log=log,
                  state="verify-failed", log_dir=tmp_path)
    release.run_live_selftest(**common)
    check = lambda: False                                                      # noqa: E731
    release.run_live_selftest(**common, stop_check=check)
    assert "stop_check" not in seen[0] and seen[1]["stop_check"] is check
