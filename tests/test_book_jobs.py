from __future__ import annotations

import asyncio
import shutil
import sys
import struct
from pathlib import Path

import pytest

from cognita.books import jobs as book_jobs
from cognita.books.jobs import ProcessResult, ProcessRunnerError, ffprobe_json, run_process


def _python() -> str:
    return str(Path(sys.executable).resolve())


@pytest.mark.asyncio
async def test_run_process_caps_output_but_drains_both_pipes() -> None:
    result = await run_process(
        [_python(), "-c", "import sys; sys.stdout.write('a'*100000); sys.stderr.write('b'*90000)"],
        timeout_seconds=5,
        max_output_bytes=17,
    )
    assert result.returncode == 0
    assert result.stdout == b"a" * 17
    assert result.stderr == b"b" * 17
    assert result.stdout_truncated and result.stderr_truncated
    assert not result.timed_out and not result.cancelled


@pytest.mark.asyncio
async def test_run_process_routes_stdout_to_open_file(tmp_path: Path) -> None:
    destination = tmp_path / "stdout.bin"
    with destination.open("wb") as output:
        result = await run_process(
            [_python(), "-c", "import sys; sys.stdout.buffer.write(b'owned bytes'); sys.stderr.write('diagnostic')"],
            timeout_seconds=5,
            stdout_file=output,
        )
        output.flush()
    assert result.returncode == 0
    assert result.stdout == b"" and not result.stdout_truncated
    assert result.stderr == b"diagnostic"
    assert destination.read_bytes() == b"owned bytes"


@pytest.mark.asyncio
async def test_run_process_timeout_awaits_owned_child_with_stdout_file(tmp_path: Path) -> None:
    destination = tmp_path / "timeout.bin"
    with destination.open("wb") as output:
        result = await run_process(
            [_python(), "-c", "import sys,time; sys.stdout.buffer.write(b'partial'); sys.stdout.flush(); time.sleep(20)"],
            timeout_seconds=0.1,
            stdout_file=output,
        )
        output.flush()
    assert result.timed_out and not result.cancelled
    assert result.returncode is not None
    assert destination.read_bytes() == b"partial"


@pytest.mark.asyncio
async def test_run_process_cancel_event_awaits_owned_child_with_stdout_file(tmp_path: Path) -> None:
    destination = tmp_path / "cancel.bin"
    cancel = asyncio.Event()
    with destination.open("wb") as output:
        task = asyncio.create_task(run_process(
            [_python(), "-c", "import sys,time; sys.stdout.buffer.write(b'partial'); sys.stdout.flush(); time.sleep(20)"],
            timeout_seconds=5,
            cancel_event=cancel,
            stdout_file=output,
        ))
        await asyncio.sleep(0.1)
        cancel.set()
        result = await task
        output.flush()
    assert result.cancelled and not result.timed_out
    assert result.returncode is not None
    assert destination.read_bytes() == b"partial"


@pytest.mark.asyncio
async def test_run_process_timeout_awaits_owned_child() -> None:
    result = await run_process(
        [_python(), "-c", "import time; time.sleep(20)"],
        timeout_seconds=0.1,
    )
    assert result.timed_out
    assert not result.cancelled
    assert result.returncode is not None
    assert result.elapsed_seconds < 3


@pytest.mark.asyncio
async def test_run_process_cancel_event_awaits_owned_child() -> None:
    cancel = asyncio.Event()
    task = asyncio.create_task(
        run_process(
            [_python(), "-c", "import time; time.sleep(20)"],
            timeout_seconds=5,
            cancel_event=cancel,
        )
    )
    await asyncio.sleep(0.1)
    cancel.set()
    result = await task
    assert result.cancelled
    assert not result.timed_out
    assert result.returncode is not None


@pytest.mark.parametrize("pipes_close_on_stop", [True, False])
@pytest.mark.asyncio
async def test_exited_leader_pipe_timeout_returns_result_and_awaits_drains(
    monkeypatch: pytest.MonkeyPatch, pipes_close_on_stop: bool,
) -> None:
    class ExitedProcess:
        returncode = 0
        pid = -1
        stdout = asyncio.StreamReader()
        stderr = asyncio.StreamReader()

        async def wait(self):
            return self.returncode

    process = ExitedProcess()
    process.stdout.feed_data(b"stdout before leader exit")
    process.stderr.feed_data(b"stderr before leader exit")
    stopped = []
    drains = []
    original_drain = book_jobs._drain_limited

    async def create_process(*args, **kwargs):
        return process

    async def stop_process(owned):
        assert owned is process
        stopped.append(owned)
        if pipes_close_on_stop:
            process.stdout.feed_eof()
            process.stderr.feed_eof()
        await process.wait()

    async def drain(reader, limit):
        drains.append(asyncio.current_task())
        return await original_drain(reader, limit)

    monkeypatch.setattr(book_jobs.asyncio, "create_subprocess_exec", create_process)
    monkeypatch.setattr(book_jobs, "_stop_owned_process", stop_process)
    monkeypatch.setattr(book_jobs, "_drain_limited", drain)
    result = await asyncio.wait_for(run_process([_python()], timeout_seconds=0.025), timeout=2)
    assert result.returncode == 0
    assert result.timed_out and not result.cancelled
    assert stopped == [process]
    assert len(drains) == 2 and all(task.done() for task in drains)
    assert result.elapsed_seconds < 1
    if pipes_close_on_stop:
        assert result.stdout == b"stdout before leader exit"
        assert result.stderr == b"stderr before leader exit"
        assert not result.stdout_truncated and not result.stderr_truncated
    else:
        assert result.stdout_truncated and result.stderr_truncated


@pytest.mark.parametrize("pipes_close_on_stop", [True, False])
@pytest.mark.asyncio
async def test_caller_task_cancellation_propagates_after_bounded_pipe_cleanup(
    monkeypatch: pytest.MonkeyPatch, pipes_close_on_stop: bool,
) -> None:
    class ExitedProcess:
        returncode = 0
        pid = -1
        stdout = asyncio.StreamReader()
        stderr = asyncio.StreamReader()

        async def wait(self):
            return self.returncode

    process = ExitedProcess()
    started = asyncio.Event()
    stopped = []
    drains = []
    original_drain = book_jobs._drain_limited

    async def create_process(*args, **kwargs):
        return process

    async def stop_process(owned):
        assert owned is process
        stopped.append(owned)
        if pipes_close_on_stop:
            process.stdout.feed_eof()
            process.stderr.feed_eof()
        await process.wait()

    async def drain(reader, limit):
        drains.append(asyncio.current_task())
        if len(drains) == 2:
            started.set()
        return await original_drain(reader, limit)

    monkeypatch.setattr(book_jobs.asyncio, "create_subprocess_exec", create_process)
    monkeypatch.setattr(book_jobs, "_stop_owned_process", stop_process)
    monkeypatch.setattr(book_jobs, "_drain_limited", drain)
    task = asyncio.create_task(run_process([_python()], timeout_seconds=5))
    await asyncio.wait_for(started.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=2)
    assert stopped == [process]
    assert task.cancelled()
    assert len(drains) == 2 and all(drain.done() for drain in drains)


@pytest.mark.asyncio
async def test_run_process_rejects_shell_and_relative_executables() -> None:
    with pytest.raises(ProcessRunnerError, match="argv"):
        await run_process("python -c pass", timeout_seconds=1)  # type: ignore[arg-type]
    with pytest.raises(ProcessRunnerError, match="absolute path"):
        await run_process(["python", "-c", "pass"], timeout_seconds=1)


@pytest.mark.asyncio
async def test_ffprobe_json_requires_existing_local_file(tmp_path: Path) -> None:
    with pytest.raises(ProcessRunnerError, match="absolute local file"):
        await ffprobe_json(_python(), tmp_path / "missing.wav")
    with pytest.raises(ProcessRunnerError, match="absolute local file"):
        await ffprobe_json(_python(), "https://example.invalid/input.mp3")


@pytest.mark.asyncio
async def test_ffprobe_json_validates_process_output(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    media = tmp_path / "input.wav"
    media.write_bytes(b"fixture")

    async def fake_run_process(*args: object, **kwargs: object) -> ProcessResult:
        argv = args[0]
        assert isinstance(argv, list)
        assert argv[argv.index("-protocol_whitelist") + 1] == "file"
        assert argv[argv.index("-format_whitelist") + 1] == "wav,mp3"
        return ProcessResult(0, b"not-json", b"", False, False, 0.01, False, False)

    monkeypatch.setattr("cognita.books.jobs.run_process", fake_run_process)
    with pytest.raises(ProcessRunnerError, match="valid JSON"):
        await ffprobe_json(
            _python(),
            media,
            timeout_seconds=3,
        )


@pytest.mark.skipif(shutil.which("ffprobe") is None, reason="ffprobe is available in the packaged Linux service")
@pytest.mark.asyncio
async def test_ffprobe_rejects_local_playlist_referencing_outside_media(tmp_path: Path) -> None:
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    outside_media = tmp_path / "outside.wav"
    fmt = struct.pack("<HHIIHH", 1, 1, 8_000, 16_000, 2, 16)
    samples = struct.pack("<hh", 100, -100)
    fmt_chunk = b"fmt " + struct.pack("<I", len(fmt)) + fmt
    data_chunk = b"data" + struct.pack("<I", len(samples)) + samples
    body = b"WAVE" + fmt_chunk + data_chunk
    outside_media.write_bytes(b"RIFF" + struct.pack("<I", len(body)) + body)
    playlist = allowed / "untrusted.m3u"
    playlist.write_text(f"#EXTM3U\n{outside_media}\n", encoding="utf-8")

    with pytest.raises(ProcessRunnerError, match="exited with status"):
        await ffprobe_json(shutil.which("ffprobe") or "", playlist, timeout_seconds=10)
