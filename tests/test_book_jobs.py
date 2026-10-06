from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

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
        del args, kwargs
        return ProcessResult(0, b"not-json", b"", False, False, 0.01, False, False)

    monkeypatch.setattr("cognita.books.jobs.run_process", fake_run_process)
    with pytest.raises(ProcessRunnerError, match="valid JSON"):
        await ffprobe_json(
            _python(),
            media,
            timeout_seconds=3,
        )
