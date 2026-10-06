"""Shell-free local subprocess lifecycle for media inspection workers."""

from __future__ import annotations

import asyncio
import json
import math
import os
import re
import signal
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence


class ProcessRunnerError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class ProcessResult:
    returncode: int
    stdout: bytes
    stderr: bytes
    stdout_truncated: bool
    stderr_truncated: bool
    elapsed_seconds: float
    timed_out: bool
    cancelled: bool


@dataclass(frozen=True)
class PacketFact:
    size_bytes: int
    data_sha256: str


_PACKET_SHA256 = re.compile(r"^(?:SHA256:)?([0-9a-fA-F]{64})$")


async def _drain_limited(reader: asyncio.StreamReader, limit: int) -> tuple[bytes, bool]:
    captured = bytearray()
    truncated = False
    while block := await reader.read(64 * 1024):
        remaining = limit - len(captured)
        if remaining > 0:
            captured.extend(block[:remaining])
        if len(block) > remaining:
            truncated = True
    return bytes(captured), truncated


async def _group_exists(process_id: int) -> bool:
    if os.name != "posix":
        return False
    try:
        os.killpg(process_id, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # A newly launched process group belongs to this service. Permission
        # failure still means the group exists; do not broaden process lookup.
        return True
    return True


async def _stop_owned_process(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        if os.name != "posix" or not await _group_exists(process.pid):
            return
    if os.name == "posix":
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        deadline = time.monotonic() + 0.4
        while time.monotonic() < deadline and await _group_exists(process.pid):
            await asyncio.sleep(0.025)
        if await _group_exists(process.pid):
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
    elif process.returncode is None:
        process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=0.4)
        except asyncio.TimeoutError:
            process.kill()
    await process.wait()


async def run_process(
    argv: Sequence[str],
    *,
    timeout_seconds: float,
    cwd: str | Path | None = None,
    max_output_bytes: int = 1_048_576,
    cancel_event: asyncio.Event | None = None,
) -> ProcessResult:
    """Run an exact argv, cap retained output while draining pipes, and await cleanup.

    POSIX children get a dedicated session so cancellation can stop only their
    process group. Windows cleanup signals only the exact process this call
    created; media tools in the supported Linux service use the group path.
    """
    if isinstance(argv, (str, bytes)) or not argv or any(not isinstance(item, str) for item in argv):
        raise ProcessRunnerError("invalid_process_request", "argv must be a nonempty sequence of strings")
    if not os.path.isabs(argv[0]):
        raise ProcessRunnerError("invalid_process_request", "Executable must be an explicit absolute path")
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise ProcessRunnerError("invalid_process_request", "timeout_seconds must be finite and positive")
    if isinstance(max_output_bytes, bool) or not isinstance(max_output_bytes, int) or max_output_bytes < 0:
        raise ProcessRunnerError("invalid_process_request", "max_output_bytes must be a nonnegative integer")
    started = time.monotonic()
    try:
        process = await asyncio.create_subprocess_exec(
            *argv,
            cwd=str(cwd) if cwd is not None else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=(os.name == "posix"),
        )
    except (OSError, ValueError) as exc:
        raise ProcessRunnerError("process_start_failed", f"Unable to start registered media tool: {exc}") from exc
    assert process.stdout is not None and process.stderr is not None
    stdout_task = asyncio.create_task(_drain_limited(process.stdout, max_output_bytes))
    stderr_task = asyncio.create_task(_drain_limited(process.stderr, max_output_bytes))
    wait_task = asyncio.create_task(process.wait())
    cancel_task = asyncio.create_task(cancel_event.wait()) if cancel_event is not None else None
    deadline = started + timeout_seconds
    timed_out = False
    cancelled = False
    try:
        waiters: set[asyncio.Task[object]] = {wait_task}  # type: ignore[arg-type]
        if cancel_task is not None:
            waiters.add(cancel_task)  # type: ignore[arg-type]
        done, _ = await asyncio.wait(
            waiters,
            timeout=max(0.0, deadline - time.monotonic()),
            return_when=asyncio.FIRST_COMPLETED,
        )
        if wait_task not in done:
            cancelled = cancel_task is not None and cancel_task in done
            timed_out = not cancelled
            await _stop_owned_process(process)
        # Some descendants can retain inherited pipes after the leader exits.
        # Bound that wait by the same task deadline, then stop the owned group.
        remaining = max(0.0, deadline - time.monotonic())
        try:
            stdout_result, stderr_result = await asyncio.wait_for(
                asyncio.gather(stdout_task, stderr_task), timeout=remaining
            )
        except asyncio.TimeoutError:
            timed_out = True
            await _stop_owned_process(process)
            stdout_result, stderr_result = await asyncio.gather(stdout_task, stderr_task)
        return ProcessResult(
            returncode=process.returncode if process.returncode is not None else -1,
            stdout=stdout_result[0],
            stderr=stderr_result[0],
            stdout_truncated=stdout_result[1],
            stderr_truncated=stderr_result[1],
            elapsed_seconds=max(0.0, time.monotonic() - started),
            timed_out=timed_out,
            cancelled=cancelled,
        )
    except asyncio.CancelledError:
        cancelled = True
        await _stop_owned_process(process)
        await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
        raise
    finally:
        if cancel_task is not None:
            cancel_task.cancel()
            await asyncio.gather(cancel_task, return_exceptions=True)
        if not wait_task.done():
            await wait_task


async def ffprobe_json(
    executable: str | Path,
    filepath: str | Path,
    *,
    timeout_seconds: float = 30.0,
    cancel_event: asyncio.Event | None = None,
) -> Mapping[str, object]:
    """Ask the registered local ffprobe binary for format and stream facts."""
    local_file = Path(filepath)
    if not local_file.is_absolute() or not local_file.is_file():
        raise ProcessRunnerError("invalid_process_request", "ffprobe input must be an existing absolute local file")
    result = await run_process(
        [
            str(executable), "-v", "error", "-protocol_whitelist", "file",
            "-format_whitelist", "wav,mp3", "-show_format", "-show_streams",
            "-of", "json", str(filepath),
        ],
        timeout_seconds=timeout_seconds,
        max_output_bytes=2_097_152,
        cancel_event=cancel_event,
    )
    if result.cancelled:
        raise ProcessRunnerError("cancelled", "ffprobe was cancelled")
    if result.timed_out:
        raise ProcessRunnerError("tool_timeout", "ffprobe exceeded its time limit")
    if result.returncode != 0:
        message = result.stderr.decode("utf-8", errors="replace")[-2_000:]
        raise ProcessRunnerError("tool_failed", f"ffprobe exited with status {result.returncode}: {message}")
    if result.stdout_truncated:
        raise ProcessRunnerError("tool_output_limit", "ffprobe JSON exceeded its output limit")
    try:
        value = json.loads(result.stdout)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ProcessRunnerError("invalid_tool_output", "ffprobe did not return valid JSON") from exc
    if not isinstance(value, dict):
        raise ProcessRunnerError("invalid_tool_output", "ffprobe JSON root must be an object")
    return value


async def ffprobe_packet_facts(
    executable: str | Path,
    filepath: str | Path,
    *,
    timeout_seconds: float = 60.0,
    cancel_event: asyncio.Event | None = None,
) -> tuple[PacketFact, ...]:
    """Read ordered audio-packet sizes and SHA-256 values for stream-copy proof."""
    local_file = Path(filepath)
    if not local_file.is_absolute() or not local_file.is_file():
        raise ProcessRunnerError("invalid_process_request", "Packet inspection requires an existing absolute local file")
    result = await run_process(
        [
            str(executable), "-v", "error", "-protocol_whitelist", "file",
            "-format_whitelist", "mp3", "-select_streams", "a:0", "-show_packets",
            "-show_entries", "packet=size,data_hash", "-show_data_hash", "sha256",
            "-of", "json", str(local_file),
        ],
        timeout_seconds=timeout_seconds,
        max_output_bytes=16 * 1024 * 1024,
        cancel_event=cancel_event,
    )
    if result.cancelled:
        raise ProcessRunnerError("cancelled", "ffprobe packet inspection was cancelled")
    if result.timed_out:
        raise ProcessRunnerError("tool_timeout", "ffprobe packet inspection exceeded its time limit")
    if result.returncode != 0:
        message = result.stderr.decode("utf-8", errors="replace")[-2_000:]
        raise ProcessRunnerError("tool_failed", f"ffprobe exited with status {result.returncode}: {message}")
    if result.stdout_truncated:
        raise ProcessRunnerError("tool_output_limit", "ffprobe packet facts exceeded the bounded output limit")
    try:
        value = json.loads(result.stdout)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ProcessRunnerError("invalid_tool_output", "ffprobe did not return valid packet JSON") from exc
    packets = value.get("packets") if isinstance(value, dict) else None
    if not isinstance(packets, list) or not packets:
        raise ProcessRunnerError("invalid_tool_output", "ffprobe did not return any audio packets")
    facts: list[PacketFact] = []
    for packet in packets:
        if not isinstance(packet, Mapping):
            raise ProcessRunnerError("invalid_tool_output", "ffprobe packet entry must be an object")
        size = packet.get("size")
        if isinstance(size, str) and size.isdecimal():
            size = int(size)
        if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
            raise ProcessRunnerError("invalid_tool_output", "ffprobe packet size must be a positive integer")
        raw_hash = packet.get("data_hash")
        match = _PACKET_SHA256.fullmatch(raw_hash) if isinstance(raw_hash, str) else None
        if match is None:
            raise ProcessRunnerError("invalid_tool_output", "ffprobe packet has no valid SHA-256 data hash")
        facts.append(PacketFact(size, match.group(1).lower()))
    return tuple(facts)
