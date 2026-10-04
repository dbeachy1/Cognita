"""🔴 6.0.8 §14.8: a worker that dies must say WHY, in the log, every time.

The bug this suite exists for was not a crash — it was the crash being
*unreportable*. On 2026-09-01/02 three separate double-card failures on kei
logged exactly this and nothing else::

    ERROR cognita.gpu: gpu.worker[card1] canary could not run: worker exited mid-slice
    ERROR cognita.gpu: gpu.worker[card2] canary could not run: worker exited mid-slice

Two mechanisms threw the evidence away, and both are covered here:

1. **`embed()` never read the exit status.** `Popen.returncode` was sitting
   there, and `gpu_worker.main()`'s codes name the exact stage that failed —
   2 construction, 3 warmup, 5 the slice. "Exited mid-slice" is the parent's
   observation of a closed pipe, not a diagnosis, and it conflated a warmup
   failure (a slice that never began) with a slice that genuinely blew up.

2. **The stderr relay was cut off mid-read.** `check_canary` terminates a
   failed worker immediately, and `terminate()` closed the stderr pipe in its
   `finally` without ever joining the relay thread. So the worker's dying
   words — the one place the cause could have appeared — were discarded
   microseconds after being written.

These tests use a real subprocess deliberately. Fakes cannot prove anything
about exit codes or pipe teardown races, which is precisely where the defect
lived.
"""

from __future__ import annotations

import logging
import sys
import textwrap

import pytest

from cognita.gpu_host import GpuUnavailable, GpuWorker
from cognita.gpu_probe import GIB, GpuDevice


class Config:
    gpu_enabled = True
    gpu_batch_size = 4
    gpu_slice_chunks = 8
    gpu_reserve_vram_gb = 4.0
    gpu_max_busy_percent = 20
    gpu_device_ids: list[str] = []
    gpu_provider = "migraphx"
    gpu_worker_shutdown_s = 2.0
    gpu_worker_slice_timeout_s = 5.0
    gpu_worker_startup_timeout_s = 30.0
    gpu_canary_tolerance = 1e-4
    embedding_model = "BAAI/bge-small-en-v1.5"
    embedding_dimensions = 3
    gpu_model_cache_dir = ""
    models_cache_dir = "/tmp/models"
    gpu_fixed_seq_len = 512
    gpu_program_cache_dir = ""
    gpu_venv_python = sys.executable


class FakeProbe:
    def __init__(self, dev):
        self._dev = dev

    def devices(self):
        return [self._dev]


def a_device():
    return GpuDevice("card1", "0000:03:00.0", "uid-1", "card1",
                     32 * GIB, 32 * GIB, 0)


# A stand-in worker process speaking the real frame protocol. It handshakes,
# then does whatever the scenario asks on the first slice.
STUB = textwrap.dedent(
    """
    import json, os, sys
    sys.path.insert(0, {src!r})
    from cognita.gpu_worker import claim_stdout, read_frame, write_frame, log

    mode = os.environ["STUB_MODE"]
    out = claim_stdout()
    write_frame(out, json.dumps({{"event": "ready", "provider_active": ["Stub"],
                                 "session_build_s": 0.0,
                                 "model_file": "model.onnx"}}).encode())
    frame = read_frame(sys.stdin.buffer)
    if frame is None:
        sys.exit(0)
    if mode == "warmup":
        log("warmup failed: RuntimeError: MIGraphX said no", "ERROR")
        log("  Traceback (most recent call last):", "ERROR")
        log("  RuntimeError: MIGraphX said no", "ERROR")
        sys.exit(3)
    if mode == "slice":
        log("embed failed for 1 texts: RuntimeError: boom", "ERROR")
        sys.exit(5)
    if mode == "silent":
        os._exit(0)
    if mode == "crash":
        import signal
        os.kill(os.getpid(), signal.SIGSEGV)
    """
)


def make_worker(tmp_path, monkeypatch, mode):
    script = tmp_path / "stub_worker.py"
    src = str((__import__("pathlib").Path(__file__).resolve()
               .parents[1] / "src"))
    script.write_text(STUB.format(src=src), encoding="utf-8")
    monkeypatch.setenv("STUB_MODE", mode)
    dev = a_device()
    worker = GpuWorker(dev, Config(), FakeProbe(dev))
    # Point start() at the stub instead of the real gpu_worker.py.
    monkeypatch.setattr(
        "cognita.gpu_host.Path.with_name",
        lambda self, name: script,
    )
    worker.start()
    return worker


def test_a_warmup_failure_is_reported_as_a_warmup_failure(tmp_path, monkeypatch,
                                                          caplog):
    """🔴 THE LIVE BUG. The worker handshakes, then its warmup embed raises and
    it exits 3 — so the parent's already-sent canary request reads EOF and the
    only thing ever logged was "worker exited mid-slice", describing a slice
    that never ran. The exit code says which stage it really was."""
    worker = make_worker(tmp_path, monkeypatch, "warmup")
    with caplog.at_level(logging.ERROR):
        with pytest.raises(GpuUnavailable):
            worker.embed(["hello"], timeout=10, record=False)
    assert "embed.gpu.died" in caplog.text
    assert "exit code 3" in caplog.text
    assert "warmup embed failed" in caplog.text
    # And the worker's own words survived the teardown race.
    assert "MIGraphX said no" in caplog.text
    worker.terminate()


def test_a_failed_slice_names_the_slice(tmp_path, monkeypatch, caplog):
    worker = make_worker(tmp_path, monkeypatch, "slice")
    with caplog.at_level(logging.ERROR):
        with pytest.raises(GpuUnavailable):
            worker.embed(["hello"], timeout=10, record=False)
    assert "exit code 5" in caplog.text
    assert "the slice itself failed" in caplog.text
    worker.terminate()


@pytest.mark.skipif(sys.platform == "win32",
                    reason="POSIX signals; the deployment target is Linux")
def test_a_native_crash_is_reported_as_a_signal(tmp_path, monkeypatch, caplog):
    """A SIGSEGV inside HIP/MIGraphX/ORT is the most likely death of this
    process and Python's `except` cannot see it. The exit status can."""
    worker = make_worker(tmp_path, monkeypatch, "crash")
    with caplog.at_level(logging.ERROR):
        with pytest.raises(GpuUnavailable):
            worker.embed(["hello"], timeout=10, record=False)
    assert "SIGSEGV" in caplog.text
    assert "native memory fault" in caplog.text
    worker.terminate()


def test_a_silent_death_says_so_rather_than_saying_nothing(tmp_path, monkeypatch,
                                                           caplog):
    """⚠️ An empty stderr tail is itself a finding — it means a native crash
    with no fault handler, or a kill from outside. The report must SAY that
    instead of leaving a blank where the cause goes."""
    worker = make_worker(tmp_path, monkeypatch, "silent")
    with caplog.at_level(logging.ERROR):
        with pytest.raises(GpuUnavailable):
            worker.embed(["hello"], timeout=10, record=False)
    assert "wrote NOTHING to stderr" in caplog.text
    worker.terminate()


def test_the_stderr_relay_is_drained_before_the_pipe_is_closed(tmp_path,
                                                               monkeypatch,
                                                               caplog):
    """🔴 The teardown race, isolated. `check_canary` terminates a failed worker
    immediately; if `terminate()` closes stderr without joining the relay, the
    lines explaining the death are lost. Terminating straight after the failure
    must NOT cost the report."""
    worker = make_worker(tmp_path, monkeypatch, "warmup")
    with caplog.at_level(logging.ERROR):
        with pytest.raises(GpuUnavailable):
            worker.embed(["hello"], timeout=10, record=False)
        worker.terminate()
    assert "MIGraphX said no" in caplog.text


# --------------------------------------------------------------------------
# 15.0 (DESIGN-NVIDIA-ACCELERATION §6 item 4, §7): the reason TRAVELS.
#
# A worker whose session cannot be built writes `worker construction failed
# reason=<token>` to stderr and exits 2 BEFORE any handshake. `start()` reads the
# token out of the stderr tail and puts it on `GpuUnavailable.reason`, which the
# verifier maps to an Admin reason. A real subprocess again: what is being proved
# is that the line survives the pipe and the teardown race and comes out the
# other side.
# --------------------------------------------------------------------------

STARTUP_STUB = textwrap.dedent(
    """
    import sys
    for line in {lines!r}:
        sys.stderr.write(line + "\\n")
    sys.stderr.flush()
    sys.exit(2)
    """
)


def start_failing_worker(tmp_path, monkeypatch, lines):
    script = tmp_path / "startup_stub.py"
    script.write_text(STARTUP_STUB.format(lines=list(lines)), encoding="utf-8")
    dev = a_device()
    worker = GpuWorker(dev, Config(), FakeProbe(dev))
    monkeypatch.setattr(
        "cognita.gpu_host.Path.with_name",
        lambda self, name: script,
    )
    with pytest.raises(GpuUnavailable) as caught:
        worker.start()
    return caught.value


def test_a_driver_too_old_reason_reaches_the_exception(tmp_path, monkeypatch):
    error = start_failing_worker(tmp_path, monkeypatch, [
        "ERROR worker construction failed reason=driver_too_old",
        "ERROR   Traceback (most recent call last):",
        "ERROR   RuntimeError: CUDA driver version is insufficient for CUDA runtime version",
    ])
    assert error.reason == "driver_too_old"
    assert "reason=driver_too_old" in str(error), (
        "the message carries it too, so a log line does"
    )


def test_a_generic_construction_failure_reason_reaches_the_exception(
        tmp_path, monkeypatch):
    error = start_failing_worker(tmp_path, monkeypatch, [
        "ERROR worker construction failed reason=construction_failed",
    ])
    assert error.reason == "construction_failed"


def test_no_reason_line_means_no_reason(tmp_path, monkeypatch):
    """An older worker, or one that died before it could speak, names nothing;
    the caller keeps its existing categories."""
    error = start_failing_worker(tmp_path, monkeypatch, [
        "ERROR something else went wrong",
    ])
    assert error.reason is None
    assert "reason=" not in str(error)


def test_a_traceback_line_containing_reason_is_not_mistaken_for_the_token(
        tmp_path, monkeypatch):
    """Only the worker's own `worker construction failed reason=` line counts. A
    library's exception text that happens to say `reason=` must not become a
    bounded category."""
    error = start_failing_worker(tmp_path, monkeypatch, [
        "ERROR   RuntimeError: request rejected reason=quota_exceeded",
    ])
    assert error.reason is None


def test_the_reason_is_logged_with_the_death_report(tmp_path, monkeypatch, caplog):
    with caplog.at_level(logging.INFO, logger="cognita.gpu"):
        start_failing_worker(tmp_path, monkeypatch, [
            "ERROR worker construction failed reason=driver_too_old",
        ])
    assert "embed.gpu.died" in caplog.text
    assert "stage=startup" in caplog.text
    assert "exit code 2" in caplog.text
    assert "startup failed reason=driver_too_old" in caplog.text


def test_a_native_crash_names_cuda_among_the_runtimes():
    """The SIGSEGV report used to say HIP/MIGraphX/ORT, which misdirects a CUDA
    worker's death."""
    from cognita import gpu_host

    assert "CUDA" in gpu_host._SIGNAL_MEANING["SIGSEGV"]


def test_reading_the_reason_scans_a_snapshot_and_never_raises_into_start():
    """15.0 review: `_worker_reason` iterated the LIVE stderr deque while the relay
    thread could still append to it; "deque mutated during iteration" escaped
    `start()` before `terminate()` ran and the worker kept its VRAM. It now reads
    a snapshot, and a tail it cannot read at all yields no reason, not an error."""
    import collections

    dev = a_device()
    worker = GpuWorker(dev, Config(), FakeProbe(dev))

    class MutatedMidScan(collections.deque):
        """A LIVE reverse walk fails the way a real deque does when the relay
        thread appends during it: after the first item, "deque mutated during
        iteration". A snapshot (`list(...)`) is one C call and is unaffected, so
        the old live loop raises here and the fixed code finds the reason."""

        def __reversed__(self):
            live = super().__reversed__()
            yield next(live)
            raise RuntimeError("deque mutated during iteration")

    worker._stderr_tail = MutatedMidScan(
        ["ERROR worker construction failed reason=driver_too_old", "ERROR later"])
    assert worker._worker_reason() == "driver_too_old"

    class Unreadable:
        def __iter__(self):
            raise RuntimeError("deque mutated during iteration")

        def __reversed__(self):
            raise RuntimeError("deque mutated during iteration")

    worker._stderr_tail = Unreadable()
    assert worker._worker_reason() is None
