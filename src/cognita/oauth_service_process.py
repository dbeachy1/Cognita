"""Lifecycle supervision for Cognita's package-owned OAuth child process.

The supervisor owns exactly one ``Popen`` object for its parent lifetime.  It
may probe and report a live child, but the monitor is deliberately incapable of
launching a replacement process.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import os
import subprocess
import sys
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from .oauth_service_client import UNAVAILABLE_MESSAGE, OAuthServiceClient, OAuthServiceUnavailable

log = logging.getLogger("cognita.oauth_service_process")


class OAuthServiceState(StrEnum):
    STARTING = "starting"
    READY = "ready"
    UNAVAILABLE_LIVE = "unavailable_live"
    UNAVAILABLE_TERMINAL = "unavailable_terminal"
    STOPPING = "stopping"
    STOPPED = "stopped"


@dataclass(frozen=True, slots=True)
class OAuthServiceSnapshot:
    state: OAuthServiceState
    pid_present: bool
    last_transition: datetime
    reason: str | None = None

    @property
    def pid(self) -> int | None:
        """A presence-only compatibility view; no process details are exposed."""
        return None if not self.pid_present else 1


Probe = Callable[..., bool | Awaitable[bool]]


class OAuthServiceSupervisor:
    """Start, probe, and stop one authenticated loopback OAuth child."""

    def __init__(
        self,
        config: Any = None,
        *,
        port: int | None = None,
        start_timeout_s: float | None = None,
        probe_interval_s: float | None = None,
        request_timeout_s: float | None = None,
        shutdown_timeout_s: float | None = None,
        revoke_all_on_start: bool = False,
        config_path: str | Path | None = None,
        client: OAuthServiceClient | None = None,
        probe: Probe | None = None,
        popen_factory: Callable[..., Any] | None = None,
        executable: str | None = None,
        module: str = "cognita.oauth_service",
        mcp_port: int | None = None,
        admin_port: int | None = None,
        oauth_connect_grace_s: float | None = None,
        clock: Callable[[], float] | None = None,
        monotonic: Callable[[], float] | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        self.config = config
        self.port = int(port if port is not None else self._value(config, "oauth_service_port", 8778))
        self.start_timeout_s = float(start_timeout_s if start_timeout_s is not None else self._value(config, "oauth_service_start_timeout_s", 20.0))
        self.probe_interval_s = float(probe_interval_s if probe_interval_s is not None else self._value(config, "oauth_service_probe_interval_s", 5.0))
        self.request_timeout_s = float(request_timeout_s if request_timeout_s is not None else self._value(config, "oauth_service_request_timeout_s", 10.0))
        self.shutdown_timeout_s = float(shutdown_timeout_s if shutdown_timeout_s is not None else self._value(config, "oauth_service_shutdown_timeout_s", 2.0))
        self.oauth_connect_grace_s = float(oauth_connect_grace_s if oauth_connect_grace_s is not None else self._value(config, "oauth_connect_grace_s", 6.0))
        self.revoke_all_on_start = bool(revoke_all_on_start)
        self.config_path = Path(config_path or self._value(config, "path", "")) if (config_path or self._value(config, "path", "")) else None
        self.client = client
        self._probe_func = probe
        self._popen_factory = popen_factory or subprocess.Popen
        self.executable = executable or sys.executable
        self.module = module
        self.mcp_port = mcp_port if mcp_port is not None else self._value(config, "mcp_port", None)
        self.admin_port = admin_port if admin_port is not None else self._value(config, "admin_port", None)
        self._validate_config()
        self._process: Any = None
        self._launch_attempted = False
        self._monitor_task: asyncio.Task[None] | None = None
        self._readiness_task: asyncio.Task[bool] | None = None
        self._readiness_lock: asyncio.Lock | None = None
        self._stopping = False
        self._last_transition = datetime.now(UTC)
        self._state = OAuthServiceState.STOPPED
        self._reason: str | None = None
        self._last_error_key: str | None = None
        self._error_count = 0
        if clock is not None and monotonic is not None:
            raise ValueError("provide only one of clock or monotonic")
        self._clock = clock or monotonic or time.monotonic
        self._sleep = sleep or asyncio.sleep
        self._recovery_started_at: float | None = None
        self._recovery_error_reported = False
        self._recovery_attempt_count = 0
        self._probe_category = "unknown"

    @staticmethod
    def _value(config: Any, name: str, default: Any) -> Any:
        if config is None:
            return default
        return getattr(config, name, default)

    def _validate_config(self) -> None:
        if not 1 <= self.port <= 65535:
            raise ValueError("oauth_service_port must be between 1 and 65535")
        if self.mcp_port == self.port or self.admin_port == self.port:
            raise ValueError("oauth_service_port collides with a public port")
        for name, value in (("start_timeout_s", self.start_timeout_s), ("probe_interval_s", self.probe_interval_s), ("request_timeout_s", self.request_timeout_s), ("shutdown_timeout_s", self.shutdown_timeout_s)):
            if value <= 0:
                raise ValueError(f"{name} must be positive")
        if self.shutdown_timeout_s > 2.0:
            raise ValueError("shutdown_timeout_s must not exceed 2 seconds")
        if not 5.0 <= self.oauth_connect_grace_s <= 30.0:
            raise ValueError("oauth_connect_grace_s must be between 5 and 30 seconds")

    @property
    def snapshot(self) -> OAuthServiceSnapshot:
        return OAuthServiceSnapshot(self._state, self._process is not None, self._last_transition, self._reason)

    def _transition(self, state: OAuthServiceState, reason: str | None = None) -> None:
        if state == self._state and reason == self._reason:
            return
        self._state = state
        self._reason = reason
        self._last_transition = datetime.now(UTC)

    def _command(self) -> list[str]:
        command = [
            self.executable,
            "-m",
            self.module,
            "--port",
            str(self.port),
            "--parent-pid",
            str(os.getpid()),
        ]
        if self.revoke_all_on_start:
            command.append("--revoke-all-on-start")
        if self.config_path is not None:
            command += ["--config", str(self.config_path)]
        return command

    async def start(self) -> OAuthServiceSnapshot:
        """Launch once per enabled episode and await bounded readiness.

        A running/unavailable-live child is never duplicated.  An explicit
        policy-driven stop, however, resets the episode so OAuth can later be
        enabled again in the same parent process.
        """
        if self._launch_attempted:
            return self.snapshot
        self._stopping = False
        self._launch_attempted = True
        self._transition(OAuthServiceState.STARTING)
        try:
            self._process = self._popen_factory(
                self._command(), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True
            )
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            self._process = None
            self._transition(OAuthServiceState.UNAVAILABLE_TERMINAL, "launch_failed")
            self._report_error("launch", exc)
            return self.snapshot
        if not await self._wait_for_ready(self.start_timeout_s):
            if self._is_live():
                self._transition(OAuthServiceState.UNAVAILABLE_LIVE, "readiness_timeout")
                if not self._recovery_error_reported:
                    if self._recovery_started_at is not None and self._clock() < self._recovery_started_at + self.oauth_connect_grace_s:
                        log.debug("OAuth readiness caller deadline clipped connection grace phase=start")
                    else:
                        self._report_error("readiness", TimeoutError("initial readiness timeout"))
            else:
                self._transition(OAuthServiceState.UNAVAILABLE_TERMINAL, "child_exited")
                self._report_error("exit", RuntimeError("OAuth child exited during startup"))
        else:
            self._transition(OAuthServiceState.READY)
            log.info("OAuth child is ready pid_present=true")
        self.start_monitoring()
        return self.snapshot

    def start_monitoring(self) -> None:
        if self._monitor_task is None and self._process is not None and not self._stopping:
            self._monitor_task = asyncio.create_task(self._monitor(), name="cognita-oauth-monitor")

    async def _wait_for_ready(self, timeout: float) -> bool:
        # A later lifecycle probe starts a fresh episode after a prior one has
        # truthfully exhausted. Concurrent callers share the active task, so
        # this reset is only reached once the previous task has completed.
        if self._recovery_error_reported:
            self._recovery_started_at = None
            self._recovery_error_reported = False
        self._recovery_attempt_count = 0
        deadline = self._clock() + timeout
        next_slot: float | None = None
        while self._is_live():
            now = self._clock()
            if next_slot is not None:
                recovery_deadline = self._recovery_started_at + self.oauth_connect_grace_s
                effective_deadline = min(deadline, recovery_deadline)
                remaining = effective_deadline - now
                if remaining <= 0:
                    self._report_recovery_exhausted(now, deadline)
                    break
                wait_for = min(max(0.0, next_slot - now), remaining)
                if wait_for:
                    await self._sleep(wait_for)
                    continue
            attempt_timeout = None
            if next_slot is not None:
                attempt_timeout = min(1.0, max(0.0, remaining))
            ready, _retryable, terminal = await self._probe_once_diagnostic(attempt_timeout)
            now = self._clock()
            if ready:
                recovery_elapsed = None if self._recovery_started_at is None else now - self._recovery_started_at
                self._recovery_started_at = None
                self._recovery_error_reported = False
                if recovery_elapsed is not None:
                    log.info(
                        "OAuth recovery recovered phase=readiness attempt=%d elapsed=%.3fs "
                        "retry_budget=%.1fs child_state=%s",
                        self._recovery_attempt_count, recovery_elapsed,
                        self.oauth_connect_grace_s, self._state.value,
                    )
                return True
            self._recovery_attempt_count += 1
            if self._recovery_started_at is not None:
                log.debug(
                    "OAuth recovery attempt phase=readiness attempt=%d elapsed=%.3fs "
                    "retry_budget=%.1fs child_state=%s category=%s",
                    self._recovery_attempt_count, now - self._recovery_started_at,
                    self.oauth_connect_grace_s, self._state.value, self._probe_category,
                )
            if terminal:
                return False
            if self._recovery_started_at is None:
                self._recovery_started_at = now
                self._recovery_error_reported = False
                log.info("OAuth recovery started phase=readiness retry_budget=%.1fs", self.oauth_connect_grace_s)
                next_slot = now + 1.0
            elif next_slot is None:
                next_slot = self._recovery_started_at + 1.0
            recovery_deadline = self._recovery_started_at + self.oauth_connect_grace_s
            effective_deadline = min(deadline, recovery_deadline)
            remaining = effective_deadline - now
            if remaining <= 0:
                self._report_recovery_exhausted(now, deadline)
                break
            # Recovery attempts occur on monotonic one-second slots. A long
            # probe advances the cursor and missed slots are skipped; there is
            # never a catch-up burst or readiness-triggered retry.
            next_slot = self._next_recovery_slot(next_slot, now)
        return False

    async def _probe_once(self) -> bool:
        ready, _retryable, _terminal = await self._probe_once_diagnostic()
        return ready

    async def _probe_once_diagnostic(self, timeout: float | None = None) -> tuple[bool, bool, bool]:
        """Return ``ready, retryable, terminal`` for one bounded probe."""
        if not self._is_live():
            self._probe_category = "child_exited"
            return False, False, True
        try:
            if self._probe_func is not None:
                result = self._probe_func(self._process) if self._accepts_argument(self._probe_func) else self._probe_func()
                ready = bool(await result if inspect.isawaitable(result) else result)
                self._probe_category = "ready" if ready else "not_ready"
                return ready, not ready, False
            if self.client is not None:
                probe_once = getattr(self.client, "probe_once", None)
                if probe_once is None:
                    ready = await self.client.probe()
                    self._probe_category = "ready" if ready else "not_ready"
                    return ready, not ready, False
                response = await probe_once(timeout=timeout) if timeout is not None else await probe_once()
                if response.status_code == 200:
                    self._probe_category = "http_200"
                    return True, False, False
                # A live child may explicitly report that it is still
                # starting. Authentication/application responses are terminal
                # for this operation and must not be replayed.
                # The readiness endpoint uses 202/503 for its explicit "still
                # starting" response. This pollability is limited to this
                # readiness protocol; application requests never replay HTTP
                # errors, including 4xx/5xx.
                retryable = response.status_code in {202, 503}
                self._probe_category = f"http_{response.status_code}"
                return False, retryable, not retryable
            return False, True, False
        except OAuthServiceUnavailable as exc:
            self._probe_category = getattr(exc, "category", type(exc).__name__)
            return False, bool(getattr(exc, "retryable", False)), not bool(getattr(exc, "retryable", False))
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            # Custom probes represent readiness state; retain their existing
            # fail-closed behavior while keeping transient attempts quiet.
            self._probe_category = type(exc).__name__
            log.debug("OAuth readiness probe failed category=%s", type(exc).__name__)
            return False, False, True

    @staticmethod
    def _next_recovery_slot(slot: float, now: float) -> float:
        if now < slot:
            return slot
        missed = int((now - slot) // 1.0) + 1
        return slot + missed

    def _report_recovery_exhausted(self, now: float, caller_deadline: float) -> None:
        if self._recovery_error_reported or self._recovery_started_at is None:
            return
        if now >= self._recovery_started_at + self.oauth_connect_grace_s and caller_deadline > now:
            self._recovery_error_reported = True
            self._report_error("readiness", TimeoutError("connection grace exhausted"))

    async def check_readiness(self) -> bool:
        """Check this live child when an application path needs OAuth.

        This is intentionally the only post-start readiness entry point. A
        live but unready child may recover, while an exited child remains
        terminal until the parent is explicitly restarted.
        """
        if self._stopping or self._state in {
            OAuthServiceState.STOPPING,
            OAuthServiceState.STOPPED,
            OAuthServiceState.UNAVAILABLE_TERMINAL,
        }:
            return False
        if not self._is_live():
            self._transition(OAuthServiceState.UNAVAILABLE_TERMINAL, "child_exited")
            self._report_error("exit", RuntimeError("OAuth child exited"))
            return False
        if self._readiness_lock is None:
            self._readiness_lock = asyncio.Lock()
        async with self._readiness_lock:
            if not self._is_live():
                self._transition(OAuthServiceState.UNAVAILABLE_TERMINAL, "child_exited")
                self._report_error("exit", RuntimeError("OAuth child exited"))
                return False
            if self._readiness_task is None or self._readiness_task.done():
                self._readiness_task = asyncio.create_task(
                    self._wait_for_ready(self.oauth_connect_grace_s),
                    name="cognita-oauth-readiness-recovery",
                )
            readiness_task = self._readiness_task
        # A caller cancellation must not cancel the shared recovery episode;
        # another waiter may have a longer deadline and the child gets only one
        # cadence of probes for its generation.
        try:
            ready = await asyncio.shield(readiness_task)
        except asyncio.CancelledError:
            # Shutdown owns cancellation of the shared task. Report the
            # fail-closed state to an in-flight health/request caller, while a
            # caller-initiated cancellation still propagates normally.
            if self._stopping:
                return False
            raise
        async with self._readiness_lock:
            # Shutdown or child exit may race an in-flight HTTP probe. Do not
            # let a stale successful response make health report a stopped
            # service as ready.
            if self._stopping or self._state in {
                OAuthServiceState.STOPPING,
                OAuthServiceState.STOPPED,
                OAuthServiceState.UNAVAILABLE_TERMINAL,
            }:
                return False
            if not self._is_live():
                self._transition(OAuthServiceState.UNAVAILABLE_TERMINAL, "child_exited")
                self._report_error("exit", RuntimeError("OAuth child exited"))
                return False
            if ready:
                if self._state == OAuthServiceState.UNAVAILABLE_LIVE:
                    self._transition(OAuthServiceState.READY)
                    log.info("OAuth child recovered pid_present=true")
                return True
            if self._state == OAuthServiceState.READY and not self._recovery_error_reported:
                self._transition(OAuthServiceState.UNAVAILABLE_LIVE, "readiness_failed")
                self._report_error("readiness", RuntimeError("authenticated readiness probe failed"))
            elif self._state == OAuthServiceState.READY:
                self._transition(OAuthServiceState.UNAVAILABLE_LIVE, "readiness_timeout")
            return False

    @staticmethod
    def _accepts_argument(callback: Callable[..., Any]) -> bool:
        try:
            return len(inspect.signature(callback).parameters) > 0
        except (TypeError, ValueError):
            return True

    def _is_live(self) -> bool:
        return self._process is not None and self._process.poll() is None

    async def _monitor(self) -> None:
        process = self._process
        if process is None:
            return
        if isinstance(process, subprocess.Popen):
            await asyncio.to_thread(process.wait)
        else:
            # Test doubles and alternate process wrappers may not provide
            # a blocking wait that is safe to move to a worker thread.
            while not self._stopping and process.poll() is None:
                await asyncio.sleep(0.01)
        if not self._stopping and process is self._process and process.poll() is not None:
            self._transition(OAuthServiceState.UNAVAILABLE_TERMINAL, "child_exited")
            self._report_error("exit", RuntimeError("OAuth child exited"))

    def _report_error(self, phase: str, error: Exception) -> None:
        key = phase + ":" + type(error).__name__
        self._error_count += 1
        if key != self._last_error_key:
            self._last_error_key = key
            log.error("%s phase=%s cause=%s", UNAVAILABLE_MESSAGE, phase, type(error).__name__)
        elif self._error_count % 12 == 0:
            log.error("%s phase=%s repeated=%d", UNAVAILABLE_MESSAGE, phase, self._error_count)

    async def stop(self) -> OAuthServiceSnapshot:
        """Stop the child within one total deadline, forcing only our owned process."""
        if self._state == OAuthServiceState.STOPPED:
            return self.snapshot
        deadline = asyncio.get_running_loop().time() + self.shutdown_timeout_s
        self._stopping = True
        self._transition(OAuthServiceState.STOPPING)
        current = asyncio.current_task()
        if self._readiness_task is not None and self._readiness_task is not current:
            self._readiness_task.cancel()
            try:
                await self._readiness_task
            except asyncio.CancelledError:
                pass
            self._readiness_task = None
        if self._monitor_task is not None and self._monitor_task is not current:
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except asyncio.CancelledError:
                pass
            self._monitor_task = None
        process = self._process
        if process is not None and process.poll() is None:
            try:
                process.terminate()
                grace = min(1.0, max(0.0, self.shutdown_timeout_s / 2.0))
                await self._wait_process(process, min(grace, self._remaining(deadline)))
            except TimeoutError:
                log.error("OAuth child graceful shutdown timed out; forcing owned PID")
                if process.poll() is None:
                    process.kill()
                remaining = self._remaining(deadline)
                if remaining > 0:
                    try:
                        await self._wait_process(process, remaining)
                    except TimeoutError:
                        log.error("OAuth child did not exit before the total shutdown deadline")
        if process is not None:
            remaining = self._remaining(deadline)
            if process.poll() is None and remaining > 0:
                try:
                    await self._wait_process(process, remaining)
                except TimeoutError:
                    log.error("OAuth child reap exceeded the total shutdown deadline")
            for stream_name in ("stdin", "stdout", "stderr"):
                stream = getattr(process, stream_name, None)
                if stream is not None:
                    close = getattr(stream, "close", None)
                    if close:
                        close()
        self._transition(OAuthServiceState.STOPPED)
        self._process = None
        self._launch_attempted = False
        self._stopping = False
        # The CLI switch is a startup-only emergency action. Re-enabling OAuth
        # later must not unexpectedly revoke every surviving grant again.
        self.revoke_all_on_start = False
        return self.snapshot

    @staticmethod
    def _remaining(deadline: float) -> float:
        return max(0.0, deadline - asyncio.get_running_loop().time())

    @staticmethod
    async def _wait_process(process: Any, timeout: float) -> None:
        if timeout <= 0:
            raise TimeoutError
        try:
            await asyncio.wait_for(
                asyncio.to_thread(process.wait, timeout=timeout), timeout=timeout
            )
        except (subprocess.TimeoutExpired, TimeoutError) as exc:
            raise TimeoutError from exc


__all__ = ["OAuthServiceSnapshot", "OAuthServiceState", "OAuthServiceSupervisor"]
