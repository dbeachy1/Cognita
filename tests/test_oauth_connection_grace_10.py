"""Deterministic section-20 startup grace coverage."""

from __future__ import annotations

import logging

import pytest

from cognita.oauth_service_process import OAuthServiceState, OAuthServiceSupervisor


class FakeProcess:
    def poll(self):
        return None

    def terminate(self):
        pass

    def kill(self):
        pass

    def wait(self, timeout=None):
        return 0

    stdin = stdout = stderr = None


class Clock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def _supervisor(clock: Clock, probe):
    return OAuthServiceSupervisor(
        start_timeout_s=30,
        oauth_connect_grace_s=6,
        popen_factory=lambda *_args, **_kwargs: FakeProcess(),
        probe=probe,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )


@pytest.mark.asyncio
async def test_startup_grace_uses_one_second_slots_and_recovers_without_error(caplog):
    clock = Clock()
    attempts: list[float] = []

    async def probe():
        attempts.append(clock.now)
        # The final one-second connect attempt starts at t=5 and completes
        # when the child begins listening at t=6 (the reported request-relative
        # startup race), without starting a request at the zero-budget boundary.
        if clock.now == 5.0:
            clock.now = 6.0
            return True
        return False

    supervisor = _supervisor(clock, probe)
    with caplog.at_level(logging.ERROR, logger="cognita.oauth_service_process"):
        snapshot = await supervisor.start()
    assert snapshot.state == OAuthServiceState.READY
    assert attempts == [0, 1, 2, 3, 4, 5]
    assert not [record for record in caplog.records if record.levelno >= logging.ERROR]
    await supervisor.stop()


@pytest.mark.asyncio
async def test_exhausted_grace_reports_one_error_and_never_retries_between_slots(caplog):
    clock = Clock()
    attempts: list[float] = []

    async def probe():
        attempts.append(clock.now)
        return False

    supervisor = _supervisor(clock, probe)
    with caplog.at_level(logging.ERROR, logger="cognita.oauth_service_process"):
        snapshot = await supervisor.start()
    assert snapshot.state == OAuthServiceState.UNAVAILABLE_LIVE
    assert attempts == [0, 1, 2, 3, 4, 5]
    errors = [record for record in caplog.records if record.levelno >= logging.ERROR]
    assert len(errors) == 1
    await supervisor.stop()


def test_grace_validation_enforces_five_to_thirty_seconds():
    with pytest.raises(ValueError, match="oauth_connect_grace_s"):
        OAuthServiceSupervisor(oauth_connect_grace_s=4.99)
    with pytest.raises(ValueError, match="oauth_connect_grace_s"):
        OAuthServiceSupervisor(oauth_connect_grace_s=30.01)
