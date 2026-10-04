from __future__ import annotations

import asyncio
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from cognita.oauth_service_process import OAuthServiceState, OAuthServiceSupervisor

# Hang guard on every await of a supervisor operation, task or event. Each
# wrapped wait ends on a signal a correct run always sends (a fake process, a
# fake clock, a cancellation, or a real child that exits on its own), so the
# bound is never a pass condition: a regression fails with the name of the
# wait instead of hanging the run.
BOUND_S = 5.0


# A real interpreter's startup is the one wait here whose duration grows with
# machine load, so its hang guard is generous.
REAL_CHILD_BOUND_S = 60.0


async def _bounded(awaitable, what: str, *, bound_s: float = BOUND_S):
    try:
        return await asyncio.wait_for(awaitable, timeout=bound_s)
    except TimeoutError:
        raise AssertionError(f"{what} did not finish within {bound_s}s") from None


class FakeProcess:
    _next_pid = 2000

    def __init__(self) -> None:
        self.pid = FakeProcess._next_pid
        FakeProcess._next_pid += 1
        self.returncode = None
        self.terminate_calls = 0
        self.kill_calls = 0
        self.stdin = self.stdout = self.stderr = None

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminate_calls += 1
        self.returncode = 0

    def kill(self):
        self.kill_calls += 1
        self.returncode = -9

    def wait(self, timeout=None):
        return self.returncode


@pytest.mark.asyncio
async def test_one_launch_and_recovery_of_same_live_child() -> None:
    root = Path(tempfile.mkdtemp(prefix="cognita-oauth8-test-"))
    try:
        process = FakeProcess()
        launches = []
        outcomes = iter([False, True, True])
        now = 0.0

        def popen(command, **kwargs):
            launches.append((command, kwargs))
            return process

        async def probe():
            try:
                return next(outcomes)
            except StopIteration:
                return True

        async def sleep(delay):
            nonlocal now
            now += delay

        supervisor = OAuthServiceSupervisor(
            port=8778,
            start_timeout_s=3.0,
            probe_interval_s=0.005,
            shutdown_timeout_s=0.05,
            oauth_connect_grace_s=6.0,
            config_path=root / "mounted" / "cognita.yaml",
            popen_factory=popen,
            probe=probe,
            clock=lambda: now,
            sleep=sleep,
        )
        snapshot = await _bounded(supervisor.start(), "supervisor.start")
        assert snapshot.state == OAuthServiceState.READY
        assert len(launches) == 1
        assert launches[0][0][-2:] == ["--config", str(root / "mounted" / "cognita.yaml")]
        assert await _bounded(supervisor.start(), "second supervisor.start") == snapshot
        await _bounded(supervisor.stop(), "supervisor.stop")
        assert process.terminate_calls == 1
        assert process.kill_calls == 0
        assert supervisor.snapshot.state == OAuthServiceState.STOPPED
    finally:
        shutil.rmtree(root)
        assert not root.exists()


@pytest.mark.asyncio
async def test_explicit_stop_allows_a_later_policy_enable_to_relaunch() -> None:
    processes = [FakeProcess(), FakeProcess()]
    launches = []

    def popen(command, **kwargs):
        launches.append((command, kwargs))
        return processes[len(launches) - 1]

    supervisor = OAuthServiceSupervisor(
        start_timeout_s=0.1,
        probe_interval_s=0.005,
        shutdown_timeout_s=0.05,
        popen_factory=popen,
        probe=lambda: True,
    )

    assert (await _bounded(supervisor.start(), "first start")).state == OAuthServiceState.READY
    await _bounded(supervisor.stop(), "first stop")
    assert (await _bounded(supervisor.start(), "relaunch start")).state == OAuthServiceState.READY
    assert len(launches) == 2
    await _bounded(supervisor.stop(), "relaunch stop")


@pytest.mark.asyncio
async def test_failed_readiness_does_not_respawn_and_exit_is_terminal() -> None:
    process = FakeProcess()
    launch_count = 0

    def popen(command, **kwargs):
        nonlocal launch_count
        launch_count += 1
        return process

    async def probe():
        return False

    # Injected clock: the 5ms readiness budget is spent on a fake clock that
    # only the supervisor's own sleep advances, not on real time.
    now = 0.0

    async def sleep(delay):
        nonlocal now
        now += delay
        await asyncio.sleep(0)

    supervisor = OAuthServiceSupervisor(
        start_timeout_s=0.005,
        probe_interval_s=0.003,
        popen_factory=popen,
        probe=probe,
        clock=lambda: now,
        sleep=sleep,
    )
    snapshot = await _bounded(supervisor.start(), "supervisor.start")
    assert snapshot.state == OAuthServiceState.UNAVAILABLE_LIVE
    assert launch_count == 1
    await _bounded(supervisor.start(), "second supervisor.start")
    assert launch_count == 1
    process.returncode = 1
    # Wait for the monitor task itself to observe the exit and finish, instead
    # of napping 10ms and hoping it has.
    assert supervisor._monitor_task is not None
    await _bounded(supervisor._monitor_task, "monitor observing child exit")
    assert supervisor.snapshot.state == OAuthServiceState.UNAVAILABLE_TERMINAL
    await _bounded(supervisor.stop(), "supervisor.stop")


class StubbornProcess(FakeProcess):
    def terminate(self):
        self.terminate_calls += 1

    def wait(self, timeout=None):
        if self.returncode is None:
            raise subprocess.TimeoutExpired("oauth-child", timeout)
        return self.returncode


@pytest.mark.asyncio
async def test_stop_forces_owned_child_after_bounded_graceful_wait() -> None:
    process = StubbornProcess()

    def popen(command, **kwargs):
        return process

    async def probe():
        return True

    supervisor = OAuthServiceSupervisor(
        start_timeout_s=0.01,
        probe_interval_s=0.005,
        shutdown_timeout_s=0.01,
        popen_factory=popen,
        probe=probe,
    )
    await _bounded(supervisor.start(), "supervisor.start")
    await _bounded(supervisor.stop(), "supervisor.stop")
    assert process.terminate_calls == 1
    assert process.kill_calls == 1
    assert supervisor.snapshot.state == OAuthServiceState.STOPPED


# Deleted 2026-09-22: test_real_idle_child_is_reaped_well_under_one_second and
# test_real_stubborn_child_is_force_reaped_within_two_seconds. Both asserted
# how fast a real child died against stop()'s real-time budget (1s graceful,
# 2s total), so their pass depended on machine load. The kill decision they
# guarded is covered deterministically with fake processes:
# test_one_launch_and_recovery_of_same_live_child (graceful: terminate, no
# kill) and test_stop_forces_owned_child_after_bounded_graceful_wait (forced).


def test_private_start_revocation_argv_is_opt_in():
    ordinary = OAuthServiceSupervisor(executable="python", module="cognita.oauth_service")
    emergency = OAuthServiceSupervisor(
        executable="python",
        module="cognita.oauth_service",
        revoke_all_on_start=True,
    )

    assert "--revoke-all-on-start" not in ordinary._command()
    assert emergency._command().count("--revoke-all-on-start") == 1


@pytest.mark.asyncio
async def test_post_start_watcher_does_not_poll_readiness() -> None:
    class PollWatchedProcess(FakeProcess):
        """Signals once the post-start watcher has polled the child N times."""

        def __init__(self) -> None:
            super().__init__()
            self.polls = 0
            self.watch_from: int | None = None
            self.watched = asyncio.Event()

        def poll(self):
            self.polls += 1
            if self.watch_from is not None and self.polls - self.watch_from >= 3:
                self.watched.set()
            return super().poll()

    process = PollWatchedProcess()
    calls = 0

    def popen(command, **kwargs):
        return process

    async def probe():
        nonlocal calls
        calls += 1
        return True

    supervisor = OAuthServiceSupervisor(
        start_timeout_s=0.02,
        probe_interval_s=0.005,
        popen_factory=popen,
        probe=probe,
    )
    await _bounded(supervisor.start(), "supervisor.start")
    startup_calls = calls
    # Instead of napping 30ms, wait until the watcher has provably run: three
    # of its own liveness polls after startup. A watcher that also probed
    # readiness would have done so by then.
    process.watch_from = process.polls
    await _bounded(process.watched.wait(), "three post-start watcher polls")
    assert calls == startup_calls
    await _bounded(supervisor.stop(), "supervisor.stop")


@pytest.mark.asyncio
async def test_on_demand_readiness_recovers_same_live_child() -> None:
    process = FakeProcess()
    outcomes = iter([False, False, True])

    def popen(command, **kwargs):
        return process

    async def probe():
        return next(outcomes)

    # Injected clock: the 5ms start budget and the one-second recovery slot
    # are spent on a fake clock advanced only by the supervisor's own sleep.
    # With the real clock this test used to wait out a real ~1s slot.
    now = 0.0

    async def sleep(delay):
        nonlocal now
        now += delay
        await asyncio.sleep(0)

    supervisor = OAuthServiceSupervisor(
        start_timeout_s=0.005,
        probe_interval_s=0.001,
        popen_factory=popen,
        probe=probe,
        clock=lambda: now,
        sleep=sleep,
    )
    snapshot = await _bounded(supervisor.start(), "supervisor.start")
    assert snapshot.state == OAuthServiceState.UNAVAILABLE_LIVE
    assert await _bounded(supervisor.check_readiness(), "check_readiness") is True
    assert supervisor.snapshot.state == OAuthServiceState.READY
    await _bounded(supervisor.stop(), "supervisor.stop")


@pytest.mark.asyncio
async def test_in_flight_readiness_cannot_outlive_shutdown() -> None:
    process = FakeProcess()
    probe_started = asyncio.Event()
    release_probe = asyncio.Event()
    calls = 0

    def popen(command, **kwargs):
        return process

    async def probe():
        nonlocal calls
        calls += 1
        if calls == 1:
            return True
        probe_started.set()
        # Hang guard only: in a correct run stop() cancels this wait, so the
        # signal is guaranteed. If it is neither cancelled nor released, fail
        # by name rather than park forever. AssertionError is deliberately not
        # one of the supervisor's caught probe errors.
        try:
            await asyncio.wait_for(release_probe.wait(), timeout=BOUND_S)
        except TimeoutError:
            raise AssertionError("in-flight probe was never cancelled or released") from None
        return True

    supervisor = OAuthServiceSupervisor(
        start_timeout_s=0.02,
        probe_interval_s=0.005,
        popen_factory=popen,
        probe=probe,
    )
    await _bounded(supervisor.start(), "supervisor.start")
    readiness = asyncio.create_task(supervisor.check_readiness())
    await _bounded(probe_started.wait(), "in-flight probe start")
    await _bounded(supervisor.stop(), "supervisor.stop")
    release_probe.set()

    assert await _bounded(readiness, "in-flight check_readiness") is False
    assert supervisor.snapshot.state == OAuthServiceState.STOPPED


@pytest.mark.asyncio
async def test_real_child_exit_is_reported_without_http_probe() -> None:
    root = Path(tempfile.mkdtemp(prefix="cognita-oauth8-exit-"))
    process = None
    try:
        script = root / "exit_child.py"
        script.write_text("raise SystemExit(7)\n", encoding="utf-8")

        def popen(command, **kwargs):
            nonlocal process
            process = subprocess.Popen(
                [sys.executable, str(script)],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                close_fds=True,
            )
            return process

        async def probe():
            return True

        supervisor = OAuthServiceSupervisor(
            start_timeout_s=0.2,
            probe_interval_s=0.01,
            popen_factory=popen,
            probe=probe,
        )
        await _bounded(supervisor.start(), "supervisor.start")
        # Instead of 50 x 10ms naps (a 0.5s bet on child startup), wait for the
        # monitor task, which finishes exactly when it has reaped the real
        # child and reported the exit. The child exits on its own, so the
        # signal is guaranteed; the bound is only a hang guard.
        assert supervisor._monitor_task is not None
        await _bounded(
            supervisor._monitor_task, "monitor reaping the real child",
            bound_s=REAL_CHILD_BOUND_S,
        )
        assert supervisor.snapshot.state == OAuthServiceState.UNAVAILABLE_TERMINAL
        assert process.poll() is not None
        await _bounded(supervisor.stop(), "supervisor.stop")
    finally:
        if process is not None and process.poll() is None:
            process.kill()
            process.wait(timeout=BOUND_S)
        shutil.rmtree(root)
        assert not root.exists()
