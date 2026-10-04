"""The parent half of the GPU embedder (DESIGN-6.0 §8, §9.1, §10).

Owns worker processes, the process-wide lease, the canary, and the proof that
VRAM came back. Runs in the SERVICE's environment, which is CPU-only by
construction (§11): nothing here imports a GPU runtime, and it cannot — that
isolation is what turns a broken GPU wheel from "the service has no embedder at
all" into "the walk ran on CPU".

Read §10's table before changing anything here. Every failure in it ends the
same way: **the walk completes and the index is correct**, and the only variable
is how long it took. Any code path that can end otherwise is a bug.
"""

from __future__ import annotations

import collections
import contextvars
import json
import logging
import os
import re
import signal
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from .acceleration_profiles import (
    AccelerationProfile,
    current_profile,
    effective_max_busy_percent,
    gpu_settings_profile,
)
from .embed_telemetry import outside_the_job, record_batch
from .gpu_probe import (
    GpuDevice,
    GpuProbe,
    gate_devices,
    qualifying,
    resolve_cards,
    skipped_summary,
)
from .gpu_worker import pack_vectors, read_frame, unpack_vectors, write_frame  # noqa: F401

log = logging.getLogger("cognita.gpu")

# §9.1: one fixed string, embedded on both devices and compared. Kept here as
# well as in the worker so the parent does not import the worker's module at
# runtime for a constant.
CANARY_TEXT = "Cognita indexes documents and answers questions about them."

# 🔴 6.0.8 §14.8: the worker's exit codes, so a death report says which STAGE
# failed rather than only that the pipe closed. These are `gpu_worker.main()`'s
# returns and must be kept in step with them — the whole point is that the
# parent can name the failure without the child's stderr surviving.
_EXIT_MEANING = {
    0: "clean exit — stdin closed or shutdown requested",
    1: "unhandled exception in the worker entry point",
    2: "session construction failed — model load or provider init",
    3: "warmup embed failed — the FIRST inference, before any slice",
    4: "protocol error — undecodable or truncated request frame",
    5: "the slice itself failed — embed raised, or returned the wrong count",
}

# A signal is a native crash, and which one narrows it a long way.
_SIGNAL_MEANING = {
    "SIGSEGV": " — native memory fault inside HIP/MIGraphX/CUDA/ORT",
    "SIGABRT": " — abort(), typically a C++ throw or a vendor runtime fatal check",
    "SIGBUS": " — bad memory access, often a truncated mapped file",
    "SIGFPE": " — arithmetic fault in native code",
    "SIGKILL": " — killed from OUTSIDE: the OOM killer, or another process",
    "SIGTERM": " — terminated from outside, or by our own teardown",
}


class GpuUnavailable(RuntimeError):
    """No GPU work is possible. Always recoverable — the caller uses the CPU.

    15.0: `reason` is a bounded token the WORKER named for why it could not
    start (`driver_too_old`, `construction_failed`), read back from its stderr
    tail, or None when the worker said nothing bounded. The verifier maps it to
    an Admin reason; it is also in the message so a log line carries it.
    """

    def __init__(self, message: str = "", reason: str | None = None):
        super().__init__(message)
        self.reason = reason


# The line `gpu_worker.main()` writes when the session cannot be built:
# "worker construction failed reason=<token>". Matched on the whole prefix so an
# unrelated traceback line that happens to contain "reason=" is never mistaken
# for it.
_WORKER_REASON = re.compile(r"worker construction failed reason=([a-z0-9_]+)")


# The AMD service's model-cache bind is the only persistent, writable location
# for compiled MIGraphX programs.  A value copied from the pre-container host
# configuration can name a real host directory that does not exist in the
# container. Keep this contract next to the worker launch so a stale config
# cannot reach the provider and fail only on its first inference.
_CONTAINER_MODEL_CACHE_ROOT = Path("/var/lib/cognita/models")
_CONTAINER_PROGRAM_CACHE_NAME = "migraphx-cache"


def _running_in_container() -> bool:
    """Return whether the service has the conventional container marker."""
    return Path("/.dockerenv").is_file() or Path("/run/.containerenv").is_file()


def _prepare_program_cache_dir(config) -> str | None:
    """Validate the compiled-program cache before starting a GPU worker.

    Native/manual runs retain their configured path exactly.  In the
    container, only the persistent model-cache bind is valid: an absolute host
    path can exist on the host while remaining invisible to the worker. Such a
    path is redirected to the stable container-local cache, with a warning;
    inability to create and write that directory is a hard GPU-start failure
    so MIGraphX never receives a path that will fail during inference.
    """
    raw = str(getattr(config, "gpu_program_cache_dir", "") or "").strip()
    if not raw or not _running_in_container():
        return raw or None

    configured = Path(raw)
    try:
        model_root = _CONTAINER_MODEL_CACHE_ROOT.resolve()
        resolved = configured.resolve()
    except OSError as exc:
        raise GpuUnavailable(
            "cannot resolve the configured GPU program cache path in the container"
        ) from exc

    if not resolved.is_relative_to(model_root):
        effective = model_root / _CONTAINER_PROGRAM_CACHE_NAME
        log.warning(
            "gpu program cache path is outside the container model-cache bind; "
            "using container-local path=%s",
            effective,
        )
    else:
        effective = configured

    try:
        effective.mkdir(parents=True, exist_ok=True)
        # Check the same write permission the worker will have.  The marker is
        # removed in the finally block, so a failed or cancelled startup never
        # leaves task-owned probe files in the persistent cache.
        fd, marker = tempfile.mkstemp(
            prefix=".cognita-cache-write-", dir=str(effective)
        )
        os.close(fd)
        try:
            os.unlink(marker)
        finally:
            # ``unlink`` above is the normal path.  If it raised, do not hide
            # the original inability to clean the owned probe file.
            if os.path.exists(marker):
                os.unlink(marker)
    except OSError as exc:
        raise GpuUnavailable(
            f"GPU program cache is not writable at {effective}: {exc}"
        ) from exc
    return str(effective)


# --------------------------------------------------------------------------
# §8.7 — one walk at a time on the GPU
# --------------------------------------------------------------------------


class GpuLease:
    """A process-wide lease with exactly one holder.

    Cognita can index two projects concurrently, and nothing in the gate stops
    two walks from each spawning a full set of workers against devices whose
    free VRAM was evaluated for one of them — doubling the footprint against a
    budget that admitted a single walk.

    🔴 **A job that cannot take the lease runs on CPU rather than queueing.**
    The CPU path is always available, and blocking an index behind an unrelated
    project's rebuild would be a worse outcome than running it slower.

    ⚠️ The lease is process-wide, NOT machine-wide. Two `cognita serve`
    instances would each spawn a full set of workers against the same devices.
    The project already requires one instance at a time, so this adds no new
    constraint — but the design must not be read as protecting against it. The
    per-device gate is the only thing standing between two instances and an
    out-of-memory, and §6.2 says why that is a bet rather than a guarantee.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._holder: str | None = None

    def acquire(self, holder: str) -> bool:
        with self._lock:
            if self._holder is not None:
                return False
            self._holder = holder
            return True

    def release(self, holder: str) -> None:
        with self._lock:
            if self._holder == holder:
                self._holder = None

    @property
    def holder(self) -> str | None:
        with self._lock:
            return self._holder


LEASE = GpuLease()


# --------------------------------------------------------------------------
# One worker
# --------------------------------------------------------------------------


@dataclass
class WorkerStats:
    """Everything §14.4's per-device row reports."""

    device: str
    pci_address: str
    batches: int = 0
    chunks: int = 0
    elapsed: float = 0.0
    spawn_s: float = 0.0
    session_build_s: float = 0.0
    canary_delta: float | None = None
    # Wall time of the canary embed. Held apart from `elapsed` because the
    # canary is the FIRST inference on a device and therefore pays the one-off
    # 25-38s MIGraphX shape compile (§5.1) — folding it into throughput made
    # `rate` understate real speed by ~3x on a short walk.
    canary_s: float = 0.0
    provider_active: str = ""
    vram_free_before: int = 0
    vram_free_after: int = 0
    peak_vram: int = 0
    yielded_reason: str | None = None
    failed_reason: str | None = None

    @property
    def released(self) -> str:
        """🔴 §8.8: the release proof, made falsifiable.

        A design document claiming memory is released is worth nothing; this
        repo has shipped that claim before and been wrong for five releases.
        The check is two file reads and it makes the guarantee testable.

        A 64 MB tolerance absorbs other processes' churn between the two
        samples — the gate is advisory (§6.2) and so is this.
        """
        if self.vram_free_before == 0:
            return "unknown"
        shortfall = self.vram_free_before - self.vram_free_after
        if shortfall <= 64 * 1024 * 1024:
            return "OK"
        return f"FAIL short by {shortfall / (1024 ** 3):.3f}GiB"


class GpuWorker:
    """A single short-lived worker process bound to one device.

    Its lifetime is the walk (§8.2). Spawn is per walk, never per batch — a
    teardown per forward pass would pay spin-up every few hundred chunks and
    destroy the entire benefit.
    """

    def __init__(self, device: GpuDevice, config, probe: GpuProbe):
        self.device = device
        self.config = config
        self.probe = probe
        self.proc: subprocess.Popen | None = None
        self.stats = WorkerStats(device=device.sysfs_name,
                                 pci_address=device.pci_address)
        self._stderr_thread: threading.Thread | None = None
        # Held apart from `self.proc`, which `terminate()` clears — a death
        # report written after teardown still needs to name the process.
        self._pid: int | None = None
        self._last_returncode: int | None = None
        # 🔴 6.0.8: the worker's last words, kept so a DEATH REPORT can quote
        # them. The relay logs every line as it arrives, but a worker that dies
        # is torn down immediately by its caller, and `terminate()` closes this
        # pipe — so the lines explaining WHY it died are exactly the ones most
        # likely to be lost. Keeping a tail costs a few KB and means the report
        # carries the cause instead of pointing at a log line that never landed.
        self._stderr_tail: collections.deque[str] = collections.deque(maxlen=80)
        # Free VRAM once this worker's own allocation has settled — set after
        # its first successful slice, and the baseline `still_qualifies` uses to
        # tell "somebody else took the card" from "I am using the card".
        self._settled_free: int | None = None

    # -- lifecycle ------------------------------------------------------

    def start(self) -> None:
        """Spawn and handshake, or raise GpuUnavailable."""
        self.stats.vram_free_before = self._vram_free()
        # 15.0: every vendor-specific launch decision comes from the acceleration
        # profile (DESIGN-NVIDIA-ACCELERATION §3, §6). `gpu_settings_profile`
        # makes a `cpu` profile resolve as `amd`, so with no profile set this is
        # byte-for-byte the MIGraphX launch it always was.
        profile = gpu_settings_profile(current_profile())
        provider = _provider_name(self.config.gpu_provider, profile)
        configured_seq_len = getattr(self.config, "gpu_fixed_seq_len", -1)
        if configured_seq_len is None or configured_seq_len < 0:
            fixed_seq_len, seq_len_source = profile.fixed_seq_len_default, "profile"
        else:
            fixed_seq_len, seq_len_source = configured_seq_len, "config"
        # The MIGraphX compiled-program cache is meaningless on CUDA: the worker
        # is spawned without --program-cache-dir and never touches (or fails on)
        # the directory.
        if profile.program_cache:
            program_cache_dir = _prepare_program_cache_dir(self.config)
            cache_note = f"dir={program_cache_dir or 'none'}"
        else:
            program_cache_dir = None
            cache_note = f"skipped (profile {profile.name} has no program cache)"
        log.info(
            "gpu.worker[%s] launch profile=%s provider=%s fixed_seq_len=%s (%s) "
            "program_cache=%s device_env=%s batch=%s",
            self.device.sysfs_name, profile.name, provider, fixed_seq_len,
            seq_len_source, cache_note,
            profile.device_env if self.device.unique_id else "none (no unique_id)",
            self.config.gpu_batch_size,
        )
        argv = [
            self.config.gpu_venv_python,
            str(Path(__file__).with_name("gpu_worker.py")),
            "--model", self.config.embedding_model,
            "--cache-dir", str(self.config.gpu_model_cache_dir or self.config.models_cache_dir),
            "--provider", provider,
            "--pci-address", self.device.pci_address,
            "--batch-size", str(self.config.gpu_batch_size),
            "--fixed-seq-len", str(fixed_seq_len),
            "--dimensions", str(self.config.embedding_dimensions),
            # 🔴 6.0.9 §8.6: the worker watches THIS pid and exits when it goes.
            # It used to arm `PR_SET_PDEATHSIG` instead, which the kernel keys
            # to the spawning THREAD — and §6.3's concurrent spin-up spawns from
            # a thread that exits immediately, so the kernel SIGKILLed every
            # worker on every multi-card box. Passing the pid is what decouples
            # the guard from which thread happened to call Popen.
            "--parent-pid", str(os.getpid()),
        ]
        if program_cache_dir:
            argv += ["--program-cache-dir", program_cache_dir]

        env = dict(os.environ)
        # Scope the worker to exactly ONE device, by IDENTITY rather than by
        # ordinal (§12.3 correction 4). With a single visible device the
        # provider's device_id is always 0, so no ordinal is ever guessed — and
        # an out-of-memory on this card is structurally unable to reach the
        # other one.
        #
        # 🔴 The value is `GPU-<unique_id>`, NOT the PCI address. Measured on
        # the reference machine: the UUID form yields exactly one device whose
        # PCI address matches, the index form works but is an ordinal, and the
        # PCI-address form makes HIP report "no ROCm-capable device is detected"
        # (rc=100). The probe carries `unique_id` precisely for this.
        #
        # 15.0: the variable is the profile's (ROCR_VISIBLE_DEVICES for AMD,
        # CUDA_VISIBLE_DEVICES for NVIDIA, whose UUIDs also arrive here with the
        # `GPU-` prefix already stripped by NvmlProbe). NVIDIA cards always have
        # a UUID; the "no unique_id -> not scoped" branch is for AMD integrated
        # parts.
        if self.device.unique_id and profile.device_env:
            env[profile.device_env] = profile.device_env_value(self.device.unique_id)
        # The worker verifies what it actually bound against this and refuses a
        # mismatch, so the selection above is never trusted on its own.
        env.setdefault("PYTHONUNBUFFERED", "1")
        # The worker environment's own library path, written by the build script
        # beside its interpreter. It points at the MIGraphX libraries the wheel
        # ships and, where hipBLASLt is not installed system-wide, at a private
        # unpack of the SAME Ubuntu package. Kept out of the service's own
        # environment on purpose: nothing about the GPU runtime may leak into
        # the process that must keep working when the GPU does not (§11).
        extra = _worker_library_path(self.config.gpu_venv_python)
        if extra:
            existing = env.get("LD_LIBRARY_PATH", "")
            env["LD_LIBRARY_PATH"] = f"{extra}:{existing}" if existing else extra

        started = time.monotonic()
        try:
            self.proc = subprocess.Popen(
                argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, env=env, bufsize=0,
            )
        except OSError as exc:
            # §10: an unstartable interpreter is the ORDINARY case on a machine
            # with no GPU environment configured. INFO, once, and the walk uses
            # the CPU.
            raise GpuUnavailable(f"cannot start {self.config.gpu_venv_python}: {exc}") from exc

        self._pid = self.proc.pid
        log.debug("gpu.worker[%s] spawned pid=%s argv=%s",
                  self.device.sysfs_name, self._pid, " ".join(argv))
        self._start_stderr_relay()
        # 🔴 §10: A HANG NEEDS ITS OWN BOUND. `read_frame` is a blocking read on
        # a pipe and the worker only writes its handshake AFTER constructing the
        # session — model resolution (possibly a download), the HIP init, the
        # MIGraphX compile. Any of those can wedge: a model cache pointed
        # somewhere the artifacts are missing and a network that black-holes, or
        # a card in a bad state. Unbounded, that hangs `start_pool`, which hangs
        # `ensure_pool`, which hangs the walk — INSIDE the project write lock,
        # where concurrent writes are refused rather than delayed. The symptom
        # is a reindex stuck at `active: true` forever with no log line after
        # `embed.plan`, and every connector write to that project failing.
        # `embed()` has had a timeout since it shipped; `start()` had none.
        handshake_timeout = float(
            getattr(self.config, "gpu_worker_startup_timeout_s", 300.0)
        )
        frame_result: dict = {}

        def read_handshake() -> None:
            try:
                frame_result["frame"] = read_frame(self.proc.stdout)
            except Exception as exc:  # pragma: no cover - pipe teardown races
                frame_result["error"] = exc

        reader = threading.Thread(target=read_handshake, daemon=True)
        reader.start()
        reader.join(handshake_timeout)
        if reader.is_alive():
            self.stats.failed_reason = "handshake timeout"
            self.terminate()
            raise GpuUnavailable(
                f"worker did not complete its handshake within "
                f"{handshake_timeout:.0f}s (model load or session build wedged)"
            )
        if "error" in frame_result:
            # Report BEFORE terminate: terminate closes the stderr pipe.
            self.report_death("handshake", detail=str(frame_result["error"]))
            reason = self._worker_reason()
            self.terminate()
            raise GpuUnavailable(
                f"worker handshake failed: {frame_result['error']}"
                + (f" (reason={reason})" if reason else ""),
                reason=reason,
            ) from frame_result["error"]
        frame = frame_result.get("frame")
        if frame is None:
            # `report_death` drains the stderr relay first, so the tail below is
            # complete: the worker's `reason=<token>` line is among its last
            # words. It is read BEFORE terminate() closes the pipe.
            self.report_death("startup")
            reason = self._worker_reason()
            log.info("gpu.worker[%s] startup failed reason=%s",
                     self.device.sysfs_name, reason or "unnamed")
            self.terminate()
            raise GpuUnavailable(
                "worker exited during startup"
                + (f" (reason={reason})" if reason else ""),
                reason=reason,
            )
        try:
            ready = json.loads(frame)
        except Exception as exc:
            self.terminate()
            raise GpuUnavailable(f"undecodable handshake: {exc}") from exc

        self.stats.spawn_s = time.monotonic() - started
        self.stats.provider_active = ",".join(ready.get("provider_active") or [])
        self.stats.session_build_s = float(ready.get("session_build_s") or 0.0)
        log.info(
            "embed.gpu.init  device=%s pci=%s pid=%s vram_free_before=%.2fGB "
            "provider_requested=%s provider_active=%s spawn=%.2fs model_file=%s",
            self.device.sysfs_name, self.device.pci_address, self.proc.pid,
            self.stats.vram_free_before / (1024 ** 3),
            ready.get("provider_requested"), self.stats.provider_active,
            self.stats.spawn_s, ready.get("model_file"),
        )

    def _start_stderr_relay(self) -> None:
        """🔴 §14.6: a worker NEVER writes to the log file.

        It writes structured lines to stderr and the parent re-emits them
        through its own logger. Two reasons, and the second is the one that
        matters: `_RedactingFormatter` lives in the parent's logging setup, and
        a child writing directly to the file would bypass token redaction —
        a standing security invariant.
        """
        # 🔴 Bind the stream ONCE, here, rather than reading `self.proc` inside
        # the thread: `terminate()` sets `self.proc = None`, so a relay that
        # dereferenced it raced every teardown.
        stream = self.proc.stderr if self.proc is not None else None
        if stream is None:
            return

        def relay() -> None:
            for raw in stream:
                line = raw.decode("utf-8", "replace").rstrip()
                if not line:
                    continue
                self._stderr_tail.append(line)
                level, _, message = line.partition(" ")
                logger = getattr(log, level.lower(), None)
                prefix = f"gpu.worker[{self.device.sysfs_name}]"
                if callable(logger) and level in {"DEBUG", "INFO", "WARNING", "ERROR"}:
                    logger("%s %s", prefix, message)
                else:
                    log.info("%s %s", prefix, line)

        self._stderr_thread = threading.Thread(target=relay, daemon=True)
        self._stderr_thread.start()

    # -- 6.0.8: why did it die? -----------------------------------------

    @property
    def pid(self) -> int | None:
        """The worker's pid, still readable after teardown has cleared `proc`."""
        return self._pid

    def _drain_stderr(self, wait: float = 3.0) -> None:
        """Drain stderr before closing it so worker failure reasons reach the log."""
        thread = self._stderr_thread
        if thread is not None and thread.is_alive():
            thread.join(wait)

    def _worker_reason(self) -> str | None:
        """The bounded `reason=<token>` the worker wrote when it could not start.

        Newest line first: the worker writes it once, just before exiting 2, and
        anything after it is teardown noise. None when the tail holds no such
        line (a crash before the worker could speak, an older worker).
        """
        # A SNAPSHOT, never the live deque: the stderr relay thread can still be
        # appending (the worker may be alive on the handshake-error path), and
        # iterating a deque while it grows raises RuntimeError. That escaped
        # `start()` before `terminate()` ran and `_start_workers_concurrently`
        # catches only GpuUnavailable, so the worker was left holding VRAM
        # (15.0 review). `list(deque)` is one C call under the GIL. Nothing in
        # here may raise into `start()`: the reason is a label, not a gate.
        try:
            tail = list(self._stderr_tail)
        except Exception:  # pragma: no cover - defensive; list() of a deque
            log.debug("gpu.worker reason: could not snapshot stderr tail",
                      exc_info=True)
            return None
        for line in reversed(tail):
            match = _WORKER_REASON.search(line)
            if match:
                return match.group(1)
        return None

    def _request_stack_dump(self, settle: float = 2.0) -> None:
        """SIGUSR1 -> the worker dumps every thread's stack to stderr (6.0.8).

        For the WEDGED case only. A hung worker is killed by the slice timeout
        with nothing on record about where it hung, which makes a wedge the one
        failure mode that cannot be debugged after the fact.
        """
        proc = self.proc
        if proc is None or proc.poll() is not None:
            return
        if not hasattr(signal, "SIGUSR1"):
            return
        try:
            proc.send_signal(signal.SIGUSR1)
        except Exception:  # pragma: no cover - the process may exit under us
            return
        # The dump is written by the child and relayed by our thread; give both
        # a moment before the caller tears the pipe down.
        deadline = time.monotonic() + settle
        while time.monotonic() < deadline:
            time.sleep(0.1)

    def _exit_status(self, wait: float = 5.0) -> str:
        """A human sentence for how the process ended, waiting for it briefly.

        A worker that has closed its pipes has not necessarily been reaped yet,
        so `poll()` alone returns None on a process that is a millisecond from
        exiting — and "still running" is the least useful of the three answers.
        """
        proc = self.proc
        if proc is None:
            code = self._last_returncode
            if code is None:
                return "no process (already torn down)"
        else:
            code = proc.poll()
            if code is None:
                try:
                    code = proc.wait(timeout=wait)
                except subprocess.TimeoutExpired:
                    return "still running (did not exit)"
        if code < 0:
            name = getattr(signal.Signals(-code), "name", str(-code))
            note = _SIGNAL_MEANING.get(name, "")
            return f"killed by signal {-code} ({name}){note}"
        return f"exit code {code} ({_EXIT_MEANING.get(code, 'unrecognized')})"

    def report_death(self, stage: str, detail: str = "") -> None:
        """🔴 §14.8: one ERROR line that actually says what happened.

        The old report was `canary could not run: worker exited mid-slice` and
        nothing else — a message that names the SYMPTOM the parent observed and
        contains no information about the child at all, even though the exit
        code was sitting in `Popen.returncode` and says exactly which stage
        failed (2 construction, 3 warmup, 5 the slice itself).
        """
        self._drain_stderr()
        status = self._exit_status()
        log.error(
            "embed.gpu.died  device=%s pci=%s pid=%s stage=%s status=%s%s",
            self.device.sysfs_name, self.device.pci_address, self._pid, stage,
            status, f" detail={detail}" if detail else "",
        )
        tail = list(self._stderr_tail)[-25:]
        if tail:
            for line in tail:
                log.error("gpu.worker[%s] last words: %s",
                          self.device.sysfs_name, line)
        else:
            log.error(
                "gpu.worker[%s] the worker wrote NOTHING to stderr before "
                "dying — a native crash inside HIP/MIGraphX/CUDA/ORT with the "
                "faulthandler disabled, or a SIGKILL from outside the process.",
                self.device.sysfs_name,
            )

    def embed(self, texts: list[str], timeout: float,
              record: bool = True) -> list[list[float]]:
        """One slice. Raises GpuUnavailable if the worker died or wedged.

        `record=False` keeps the call out of this worker's throughput figures —
        used by the §9.1 canary, which is not corpus work AND is the first
        inference on the device, so it carries the one-off shape compile.
        """
        if self.proc is None or self.proc.poll() is not None:
            # 🔴 "worker is not running" was the whole report, for a process
            # whose exit status was right there. Observed live on 2026-09-01
            # 22:06:53 — card2 logged exactly that and nothing else.
            if self.proc is not None:
                self.report_death("pre-slice")
            raise GpuUnavailable("worker is not running")
        request = json.dumps({"op": "embed", "texts": texts}).encode()
        started = time.monotonic()
        # "send", not "out": the WORKER logs `slice in` / `slice out` from its
        # own point of view, so a parent line also called "out" put two
        # different events under one label in the same log, four lines apart.
        log.debug("gpu.worker[%s] slice send: pid=%s texts=%d chars=%d "
                  "bytes=%d timeout=%.0fs record=%s",
                  self.device.sysfs_name, self._pid, len(texts),
                  sum(len(t) for t in texts), len(request), timeout, record)
        result: dict = {}

        def run() -> None:
            try:
                write_frame(self.proc.stdin, request)
                frame = read_frame(self.proc.stdout)
                result["frame"] = frame
            except Exception as exc:  # pragma: no cover - pipe teardown races
                result["error"] = exc

        # ⚠️ §10: a HANG is a distinct failure from a crash and needs its own
        # timeout. A crashed worker is obvious — the pipe closes and the process
        # is gone. A wedged one holds its slice, holds its VRAM, and looks
        # identical to a slow one, so without a bound the walk never finishes.
        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        thread.join(timeout)
        if thread.is_alive():
            self.stats.failed_reason = "slice timeout"
            # A wedged worker still has stacks worth having, and the worker
            # dumps every thread on SIGUSR1 (6.0.8). Ask before killing: this
            # is the only chance to learn WHERE it hung, and `terminate()`
            # destroys the evidence a second later.
            self._request_stack_dump()
            self.report_death("slice timeout",
                              detail=f"no reply within {timeout:.0f}s")
            self.terminate()
            raise GpuUnavailable(f"worker wedged: no reply within {timeout}s")
        if "error" in result:
            self.stats.failed_reason = str(result["error"])
            self.report_death("slice protocol", detail=str(result["error"]))
            raise GpuUnavailable(f"worker protocol error: {result['error']}")
        frame = result.get("frame")
        if frame is None:
            self.stats.failed_reason = "worker exited mid-slice"
            # 🔴 The message that started all this. EOF at a frame boundary
            # means the child is gone; the exit code says whether it died in
            # the warmup (3) or in this slice (5), and those are very different
            # bugs. Reporting only "mid-slice" conflated them for four releases.
            self.report_death("mid-slice",
                              detail=f"texts={len(texts)} "
                                     f"waited={time.monotonic() - started:.2f}s")
            raise GpuUnavailable("worker exited mid-slice")
        vectors = unpack_vectors(frame)
        if len(vectors) != len(texts):
            self.stats.failed_reason = "count mismatch"
            raise GpuUnavailable(
                f"worker returned {len(vectors)} vectors for {len(texts)} texts"
            )
        elapsed = time.monotonic() - started
        if not record:
            # The canary. Timed separately so the compile it pays is VISIBLE
            # rather than smeared into the device's throughput.
            self.stats.canary_s = elapsed
            return vectors
        self.stats.batches += 1
        self.stats.chunks += len(texts)
        self.stats.elapsed += elapsed
        # ⚠️ CARD-WIDE, not this worker's own: `_vram_used()` is
        # `vram_total - vram_free`, which includes every other process on the
        # device. It is still the right number for §14.4's "how full did this
        # card get", but it must never be read as this worker's footprint, and
        # nothing may subtract it from a free-VRAM reading (see
        # `still_qualifies`, where doing exactly that made §6.4 unreachable).
        self.stats.peak_vram = max(self.stats.peak_vram, self._vram_used())
        # The first completed slice is when this worker's own allocation has
        # settled: the model is resident, the shape is compiled and the batch
        # buffers exist. That reading is the baseline §6.4 compares against.
        if self._settled_free is None:
            self._settled_free = self._vram_free()
        # 🔴 Report into the JOB as well as into this worker's own stats, or the
        # walk's summary describes only whatever the CPU happened to do. The
        # first successful GPU walk logged `chunks=1 ... device=cpu` for a run
        # that embedded 2,036 chunks across two cards: the per-device rows were
        # right and the line everyone reads first was wrong.
        #
        # record_batch is the same call the CPU embedder makes, so both paths
        # aggregate identically — which is the whole point of §14's "same field
        # names on both paths".
        record_batch(self.device.sysfs_name, chunks=len(texts),
                     chars=sum(len(t) for t in texts), elapsed=elapsed)
        return vectors

    def terminate(self, grace: float | None = None) -> None:
        """Stop the worker and sample the VRAM it gave back.

        `vram_free_after` is sampled after the process exits, not when a signal
        is sent, so it reflects VRAM actually returned by the worker.
        """
        proc, self.proc = self.proc, None
        if proc is None:
            return
        grace = self.config.gpu_worker_shutdown_s if grace is None else grace
        try:
            if proc.poll() is None and proc.stdin is not None:
                try:
                    # Closing stdin is the polite exit (§8.6 mechanism 2).
                    proc.stdin.close()
                except OSError:
                    pass
            try:
                proc.wait(timeout=grace)
            except subprocess.TimeoutExpired:
                proc.terminate()
                try:
                    proc.wait(timeout=grace)
                except subprocess.TimeoutExpired:
                    # A leaked GPU process holding VRAM is worse than an abrupt
                    # teardown, and §14.4 reports released=FAIL either way.
                    log.warning("gpu.worker[%s] will not exit; killing",
                                self.device.sysfs_name)
                    proc.kill()
                    proc.wait(timeout=grace)
        except Exception:  # pragma: no cover - never let teardown raise
            log.warning("gpu.worker[%s] teardown error", self.device.sysfs_name,
                        exc_info=True)
        finally:
            self._last_returncode = proc.poll()
            # 🔴 6.0.8: DRAIN BEFORE CLOSING. The relay is still reading the
            # worker's dying words; closing the pipe underneath it discards
            # them, which is why three GPU failures were logged with no cause.
            # The join is bounded — a relay that will not finish must not hold
            # up a teardown that is releasing VRAM.
            self._drain_stderr()
            log.debug("gpu.worker[%s] torn down pid=%s status=%s",
                      self.device.sysfs_name, self._pid,
                      self._exit_status(wait=0.0))
            for stream in (proc.stdin, proc.stdout, proc.stderr):
                try:
                    if stream is not None:
                        stream.close()
                except OSError:
                    pass
            self.stats.vram_free_after = self._vram_free_settled()
            if self.stats.released != "OK":
                log.warning(
                    "embed.gpu.release  device=%s vram_free_before=%.2fGB "
                    "vram_free_after=%.2fGB released=%s",
                    self.device.sysfs_name,
                    self.stats.vram_free_before / (1024 ** 3),
                    self.stats.vram_free_after / (1024 ** 3),
                    self.stats.released,
                )

    # -- device sampling -------------------------------------------------

    def _current(self) -> GpuDevice | None:
        for candidate in self.probe.devices():
            if candidate.pci_address == self.device.pci_address:
                return candidate
        return None

    def _vram_free(self) -> int:
        current = self._current()
        return current.vram_free if current else 0

    def _vram_free_settled(self, timeout: float = 5.0) -> int:
        """Free VRAM after the driver has finished reclaiming, not before.

        🔴 The process having exited is NOT the same as its VRAM being back.
        The kernel driver tears the allocation down asynchronously, so sampling
        the instant `wait()` returns reports a card that is still partly held.
        Observed live: `released=FAIL short by 0.452GiB` logged for a card that
        was at its 0.06 GiB baseline moments later.

        A false FAIL is not harmless — this line is the ONLY evidence for §8's
        central promise, and a check that cries wolf is one people stop reading.

        ⚠️ Bounded, and deliberately short. Polling until it succeeds would turn
        a real leak into a clean bill of health, which is the opposite failure
        and the worse one. If the memory is not back within `timeout`, it is
        reported as not back.
        """
        deadline = time.monotonic() + timeout
        best = self._vram_free()
        while time.monotonic() < deadline:
            if best >= self.stats.vram_free_before - (64 * 1024 * 1024):
                return best
            time.sleep(0.25)
            best = max(best, self._vram_free())
        return best

    def _vram_used(self) -> int:
        current = self._current()
        return (current.vram_total - current.vram_free) if current else 0

    def still_qualifies(self) -> tuple[bool, str]:
        """§6.4: re-check the gate before dispatching the next slice.

        If other GPU work starts partway through a rebuild, this worker finishes
        the slice in hand and is then TERMINATED, not merely idled. An idle
        worker still holds its weights, its arena and its driver context — the
        user asked for the card back and would get a process still sitting on
        gigabytes of it.
        """
        current = self._current()
        if current is None:
            return False, "device disappeared"
        # Compare current card headroom with the reserve. `peak_vram` is a
        # running maximum of card-wide usage, including other processes, so
        # subtracting it from free VRAM made the old threshold less restrictive
        # as competing usage grew. On a 32 GiB card, another process holding
        # 27 GiB still passed that check. The relevant question is whether
        # another process consumed headroom since
        # I settled?". This worker's own allocation is steady once its first
        # slice is done: on MIGraphX because the shape is pinned (§5.1), on CUDA
        # (dynamic shapes) because the worker's warm-up embeds one FULL batch of
        # full-context text before it reports ready, so the arena is already at
        # its high-water mark (DESIGN-NVIDIA §6 item 3). So its own settled
        # reading is the baseline and a drop below it is somebody else arriving. Both
        # terms are required: the drop says it was not us, and the reserve says
        # what is left is no longer enough to be a good neighbor.
        if self._settled_free is None:
            return True, "ok"  # nothing embedded yet — no baseline to compare
        taken = self._settled_free - current.vram_free
        if taken > 0 and current.vram_free_gb < self.config.gpu_reserve_vram_gb:
            return False, (
                f"vram_free={current.vram_free_gb:.2f}GB "
                f"(-{taken / (1024 ** 3):.2f}GB since this worker settled)"
            )
        return True, "ok"


def _worker_library_path(venv_python: str) -> str:
    """The `.ld_library_path` the worker environment's build script left behind.

    A file rather than a config key because it is a property of how that
    environment was BUILT, not a choice an operator makes: rebuild the venv and
    the path is regenerated with it. Missing or unreadable is fine — a worker
    environment whose libraries are all installed system-wide needs nothing.

    🔴 **`.resolve()` must NOT be used here.** A uv-created venv's `bin/python`
    is a SYMLINK to a shared interpreter (under `~/.local/share/uv/python/...`),
    so resolving it walks out of the virtual environment entirely and looks for
    the marker beside uv's copy of Python. The failure is silent in the worst
    way: the worker starts, cannot load `libmigraphx_c.so.3`, ORT falls back to
    CPU, and only the "never run silently on CPU" guard turns it into a visible
    error instead of a slow walk nobody investigates.
    """
    try:
        # Deliberately unresolved: the venv layout is what matters, not where
        # the interpreter it links to happens to live.
        root = Path(venv_python).parent.parent
        marker = root / ".ld_library_path"
        if marker.is_file():
            return marker.read_text().strip()
    except OSError:
        pass
    return ""


def _provider_name(configured: str, profile: AccelerationProfile | None = None) -> str:
    """Map the config's short name onto the execution provider.

    🔴 `rocm` is dead on ROCm 7.1+ — the ROCm EP was REMOVED in ONNX Runtime
    1.23 (§5). It is kept here only so an existing config does not fail to load;
    it will not produce a working worker on a current stack.

    15.0: an empty name (the shipped default) means the acceleration profile's
    provider — CUDA on `nvidia`, MIGraphX on `amd` and on `cpu` (which resolves
    GPU settings as amd). An explicit `migraphx`/`rocm`/`cuda` is honored. The
    config validator refuses any other value at load, so the unknown branch is
    reachable only from a caller that skipped validation; it warns and falls back
    to the profile's provider rather than guessing MIGraphX on a CUDA image.
    """
    settings = gpu_settings_profile(profile if profile is not None else current_profile())
    name = (configured or "").strip().lower()
    if not name:
        return settings.embed_provider
    known = {
        "migraphx": "MIGraphXExecutionProvider",
        "rocm": "ROCMExecutionProvider",
        "cuda": "CUDAExecutionProvider",
    }
    mapped = known.get(name)
    if mapped is None:
        log.warning("gpu_provider %r is not one of %s; using the %s profile's %s",
                    configured, sorted(known), settings.name, settings.embed_provider)
        return settings.embed_provider
    return mapped


# --------------------------------------------------------------------------
# The pool
# --------------------------------------------------------------------------


@dataclass
class GpuPool:
    """Workers for one job, one per qualifying device."""

    workers: list[GpuWorker] = field(default_factory=list)
    holder: str = ""
    cpu_fallback_chunks: int = 0
    _down: bool = False

    @property
    def alive(self) -> list[GpuWorker]:
        return [w for w in self.workers if w.proc is not None]

    def shutdown(self) -> None:
        """Terminate every worker and release the lease. Safe to call twice.

        ⚠️ The idempotence is not politeness. `LEASE.release` matches on the
        HOLDER STRING, and holder strings are not unique — every single-document
        write uses one derived from the project name. A second shutdown of a
        pool that had already released would therefore release whichever pool
        holds the lease NOW, handing the GPU to two jobs at once. Teardown is
        reachable from several paths (the canary branch, the walk's `finally`,
        `start_pool`'s error handler), so "called twice" is a matter of time.
        """
        if self._down:
            return
        for worker in self.workers:
            worker.terminate()
        LEASE.release(self.holder)
        # Publish completion only after every owned resource was released.
        # A raised teardown leaves the pool retryable; setting this before the
        # loop would turn the next shutdown into a false-success no-op.
        self._down = True
        # 🔴 6.2: the per-device rows are emitted HERE, not by the caller.
        #
        # They were duplicated in two call sites (the walk's `finally` and the
        # single-document path), in two slightly different shapes, and both ran
        # immediately after `pool.shutdown()` because `vram_free_after` and
        # `released` are only meaningful once the process has actually exited.
        # The warm pool (6.2) breaks that arrangement: a pool now outlives the
        # job that used it, so a caller logging cumulative device totals at the
        # end of ITS job would report a later job's numbers as its own — and the
        # job that finally reaps the pool is a TIMER with no caller at all.
        # Binding the rows to the teardown they describe is what keeps them
        # honest, and it makes the two shapes one shape (§14.3).
        log_device_rows(self)


def log_device_rows(pool: GpuPool) -> None:
    """§14.4's per-device hardware facts, once per pool, at teardown.

    🔴 NOT `embed.done`. That name belongs to the JOB summary, which already
    carries a `device=` row per worker — two different record shapes under one
    name made `grep 'embed.done' | sum chunks=` count a 2,036-chunk walk as
    6,108. These are per-DEVICE hardware facts and belong beside
    `embed.gpu.init` and `embed.gpu.canary`.

    Never raises: this runs from `shutdown()`, which is reachable from a timer
    thread and from `atexit`, and a teardown that throws while reporting is
    strictly worse than one that reports nothing.
    """
    try:
        for w in pool.workers:
            log.info(
                "embed.gpu.device  device=%s pci=%s chunks=%d batches=%d "
                "elapsed=%.2f canary_delta=%s canary_s=%.2f peak_vram=%.2fGB "
                "vram_free_before=%.2fGB vram_free_after=%.2fGB released=%s%s",
                w.stats.device, w.stats.pci_address, w.stats.chunks,
                w.stats.batches, w.stats.elapsed, w.stats.canary_delta,
                w.stats.canary_s, w.stats.peak_vram / (1024 ** 3),
                w.stats.vram_free_before / (1024 ** 3),
                w.stats.vram_free_after / (1024 ** 3), w.stats.released,
                f" yielded={w.stats.yielded_reason}" if w.stats.yielded_reason else "",
            )
        if pool.cpu_fallback_chunks:
            log.info("embed.gpu.fallback  cpu_fallback_chunks=%d",
                     pool.cpu_fallback_chunks)
    except Exception:  # pragma: no cover - reporting must not break teardown
        log.exception("failed to log the GPU device rows")


def _run_per_worker(items, work, what: str) -> list:
    """Run `work(item)` on every item at once, in item order (§6.3).

    Shared by spawn and the canary, which are the two per-device steps that used
    to run back to back. Both are dominated by waiting on a subprocess, so a
    thread each is the right shape and the GIL is not in the way.

    Returns only the items whose work returned something truthy; a failure is
    logged and dropped, because one device failing must never cost the others.
    """
    if not items:
        return []
    if len(items) == 1:  # don't pay for a pool to do one thing
        outcome = work(items[0])
        return [outcome] if outcome else []
    results: dict[int, object] = {}
    def run(index: int, item) -> None:
        # 6.0.8: the concurrent seam. Every observed double-card failure had
        # both devices arriving here within milliseconds of each other, while
        # every success had them ~1.9s apart — so the interleaving is evidence
        # and belongs in the log rather than in someone's reconstruction.
        started = time.monotonic()
        name = getattr(item, "sysfs_name", None) or getattr(
            getattr(item, "device", None), "sysfs_name", index)
        log.debug("gpu %s begin device=%s", what, name)
        try:
            outcome = work(item)
        except Exception:
            log.exception("gpu %s failed unexpectedly for one device", what)
            return
        log.debug("gpu %s end device=%s elapsed=%.2fs ok=%s",
                  what, name, time.monotonic() - started, bool(outcome))
        if outcome:
            results[index] = outcome
    threads = [
        threading.Thread(target=contextvars.copy_context().run,
                         args=(run, i, item), daemon=True)
        for i, item in enumerate(items)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    # Item order, not completion order: the logs and the §14.4 device rows read
    # far better when card1 is always before card2.
    return [results[i] for i in sorted(results)]


def _start_workers_concurrently(devices, config, probe: GpuProbe) -> list:
    def start_one(device: GpuDevice):
        worker = GpuWorker(device, config, probe)
        try:
            worker.start()
        except GpuUnavailable as exc:
            # One device failing to start must not cost the others.
            log.warning("gpu.worker[%s] did not start: %s", device.sysfs_name, exc)
            worker.terminate()
            return None
        return worker

    return _run_per_worker(list(devices), start_one, "spawn")


def start_pool(config, probe: GpuProbe, holder: str, batch_ceiling_gb: float) -> GpuPool | None:
    """Take the lease, gate the devices, spawn a worker for each that passes.

    Returns None whenever the answer is "use the CPU" — which is an ordinary
    outcome, not an error, and is the answer on every machine that has not
    deliberately built a worker environment.
    """
    if not getattr(config, "gpu_enabled", False):
        return None
    if not getattr(config, "gpu_venv_python", None):
        # The real master switch in practice: null on every machine that has
        # not built a worker environment, which makes "no GPU" the default
        # everywhere without anyone configuring anything.
        return None
    if not LEASE.acquire(holder):
        log.info("embed.plan  gpu_lease=busy holder=%s decision=cpu", LEASE.holder)
        return None

    pool = GpuPool(holder=holder)
    try:
        devices = probe.devices()
        selection = resolve_cards(
            devices,
            getattr(config, "gpu_cards", "all"),
            config.gpu_device_ids,
        )
        for warning in selection.warnings:
            # 🔴 WARNING, not debug. This is the branch where a config typo and
            # a GPU-less machine look identical from the outside, because the
            # fallback below is silent and correct. See `resolve_cards`.
            log.warning("embed.plan  gpu_cards=misconfigured  %s", warning)
        results = gate_devices(
            devices,
            batch_ceiling_gb=batch_ceiling_gb,
            reserve_vram_gb=config.gpu_reserve_vram_gb,
            max_busy_percent=effective_max_busy_percent(
                getattr(config, "gpu_max_busy_percent", None)
            ),
            pinned=selection.pinned,
            pinned_by=selection.source,
        )
        chosen = qualifying(results)
        log.info(
            "embed.plan  cards=%s devices_qualified=%s devices_skipped=%s",
            selection.source,
            [d.sysfs_name for d in chosen], skipped_summary(results),
        )
        if not chosen:
            LEASE.release(holder)
            return None
        # 🔴 §6.3: SPIN-UP IS CONCURRENT ACROSS DEVICES, so wall-clock start-up
        # is one device's regardless of how many join. This loop was sequential
        # and `start()` blocks on the worker's handshake, so N devices cost N
        # spin-ups back to back — all of it inside the project write lock, where
        # concurrent writes are refused rather than queued. Measured on the two
        # reference cards: card1 ready at 22:59:05.78 and card2 at 22:59:08.78,
        # three seconds apart for no reason, and it scales linearly with device
        # count. Threads are right here rather than processes because `start()`
        # spends its time blocked on a pipe read waiting for a subprocess.
        started_workers = _start_workers_concurrently(chosen, config, probe)
        pool.workers.extend(started_workers)
        if not pool.workers:
            LEASE.release(holder)
            return None
        return pool
    except Exception:
        log.warning("GPU pool startup failed; the walk will use the CPU", exc_info=True)
        pool.shutdown()
        return None


def check_canary(pool: GpuPool, cpu_embed, tolerance: float) -> list[GpuWorker]:
    """🔴 §9.1. Embed one fixed string on every worker and compare to the CPU.

    **The reference is computed LOCALLY and never shipped as a stored constant.**
    Different CPU microarchitectures take different kernel paths in ORT, so a
    vector computed on the machine that built the release can differ from the
    same vector on the deployment box by more than the FP32 noise floor — a
    stored constant would fail spuriously on hardware that is working perfectly.
    The service's CPU embedder is already loaded and is the correct reference by
    definition: the question is whether THIS machine's GPU agrees with THIS
    machine's CPU.

    This is not gold-plating. 3.8.1 shipped silent zero-vector embeddings that
    corrupted an index in a way count-based checks could not see, and a
    brand-new execution provider is precisely where that recurs. It has already
    earned its place once: during the §12.2 spike it caught a provider that
    loaded, reported itself active, ran at 8.6x, and returned vectors with a
    cosine of 0.536 against the CPU.

    Returns the workers that passed; failures are terminated.
    """
    # ONE reference for every device (§9.1), computed before the fan-out: it is
    # the same CPU vector for all of them, and embedding it per worker would
    # both waste CPU and add N canary chunks to the job's totals.
    #
    # 🔴 OUTSIDE THE JOB. None of the canary is the caller's corpus, and a
    # pure-GPU walk was logging `device=cpu batches=1 chunks=1` for this one
    # reference call — observed live. The canary's chunks also landed in
    # `est_error`, grading the estimator against work it never estimated.
    with outside_the_job():
        reference = cpu_embed([CANARY_TEXT])[0]

    def prove(worker: GpuWorker):
        started = time.monotonic()
        log.debug("gpu.worker[%s] canary starting pid=%s",
                  worker.device.sysfs_name, getattr(worker, "pid", None))
        try:
            # record=False: this is the FIRST inference on the device, so it
            # pays the one-off 25-38s MIGraphX shape compile. Counted as
            # throughput it made a card doing ~73 chunks/s report ~28.
            candidate = worker.embed([CANARY_TEXT], timeout=300, record=False)[0]
        except GpuUnavailable as exc:
            # `embed()` has already emitted the §14.8 death report with the
            # exit code and the worker's last stderr lines; this line is the
            # canary's own verdict, not the diagnosis.
            log.error("gpu.worker[%s] canary could not run after %.2fs: %s "
                      "(see the embed.gpu.died line above for the cause)",
                      worker.device.sysfs_name, time.monotonic() - started, exc)
            worker.terminate()
            return None
        # 🔴 DIMENSION FIRST, AND NOT WITH `zip`. `zip` stops at the shorter
        # sequence, so a worker returning a prefix-compatible SHORT vector — the
        # loudest possible "this provider is not computing what we think" —
        # scored a small delta over its prefix and PASSED the one check that
        # exists to catch exactly that. The CPU path already validates width
        # (`embeddings.py`); the GPU path checked only the vector COUNT.
        if len(candidate) != len(reference):
            log.error(
                "embed.gpu.canary  device=%s dim=%d expected=%d FAILED — the "
                "device returned a different vector width than the CPU. Storing "
                "these would corrupt the index.",
                worker.device.sysfs_name, len(candidate), len(reference),
            )
            worker.stats.failed_reason = (
                f"canary dim {len(candidate)} != {len(reference)}"
            )
            worker.terminate()
            return None
        delta = max(abs(a - b) for a, b in zip(reference, candidate))
        worker.stats.canary_delta = delta
        if delta > tolerance:
            log.error(
                "embed.gpu.canary  device=%s delta=%.3e tolerance=%.1e FAILED — "
                "refusing to use this device. A provider that loads and computes "
                "the wrong answer corrupts the index in a way count-based checks "
                "cannot see.",
                worker.device.sysfs_name, delta, tolerance,
            )
            worker.stats.failed_reason = f"canary delta {delta:.3e}"
            worker.terminate()
            return None
        log.info("embed.gpu.canary  device=%s delta=%.3e tolerance=%.1e OK",
                 worker.device.sysfs_name, delta, tolerance)
        return worker

    # §6.3 again: the canary is the OTHER per-device step that ran back to back,
    # and it is the expensive one — the first inference on a device pays the
    # shape compile. Measured sequentially at ~2s apart on the two reference
    # cards, on top of the spawn gap.
    passed = _run_per_worker(list(pool.workers), prove, "canary")
    pool.workers = passed
    return passed


def embed_with_pool(pool: GpuPool, texts: list[str], config, cpu_embed) -> list[list[float]]:
    """Embed `texts` across the pool, falling back to the CPU per slice.

    🔴 Every failure path here ends with the vectors produced and the walk
    continuing (§10). A device that dies, wedges, or yields is dropped and its
    slice is retried elsewhere; if every device drops out, the whole thing
    drains on the CPU embedder, which was never unloaded (§8.5).
    """
    if not texts:
        return []
    # 🔴 Slice by gpu_slice_chunks, NOT gpu_batch_size. The batch size is the
    # model's forward-pass width (and therefore the compiled tensor shape); the
    # slice is one IPC request, which the worker runs as several forward passes.
    # Measured: one request per 64 chunks gave 33.5 chunks/s because a fixed
    # per-call cost inside fastembed dominates a small request.
    #
    # The slice is also the unit of work-stealing across devices, so it is
    # shrunk when the work would not otherwise reach every worker — a single
    # enormous slice would hand the whole window to one card and idle the rest,
    # which is the §6.3 failure this feature exists to avoid.
    workers = max(1, len(pool.alive))
    slice_size = max(
        config.gpu_batch_size,
        min(getattr(config, "gpu_slice_chunks", 512),
            -(-len(texts) // workers)),  # ceil: at least one slice per worker
    )
    slices = [texts[i:i + slice_size] for i in range(0, len(texts), slice_size)]

    # 🔴 ONE SHARED QUEUE, WORKERS PULL CONCURRENTLY (§6.3). Each worker runs on
    # its own thread and takes the next slice when it is ready; the GIL is not a
    # problem because a thread spends its time blocked on a pipe read waiting
    # for a subprocess.
    #
    # This was a sequential `for slice in slices: pick a worker; wait` loop, and
    # the effect was invisible in every unit test: results were correct, work
    # was spread across devices by the least-loaded rule, and the per-device
    # counters looked balanced. But the cards TOOK TURNS — only one was ever
    # embedding — so two devices delivered one device's throughput. Measured as
    # 79.1s on card1 against 25.2s on card2 for an identical 1018 chunks, which
    # is what alternating looks like when only one side is timed at a time.
    #
    # Self-balancing needs no estimation: a device that finishes sooner simply
    # takes the next slice sooner.
    pending = list(enumerate(slices))
    pending.reverse()  # pop() from the end preserves slice order
    results: dict[int, list[list[float]]] = {}
    lock = threading.Lock()

    def requeue(index: int, chunk: list[str]) -> None:
        with lock:
            pending.append((index, chunk))  # someone else takes it

    def drain(worker: GpuWorker) -> None:
        while True:
            with lock:
                if not pending:
                    return
                index, chunk = pending.pop()
            # 🔴 FROM HERE THIS THREAD OWNS A SLICE THAT IS IN NEITHER `pending`
            # NOR `results`. Every exit must store a result or put the slice
            # back; an escape that does neither loses it, and the merge below
            # then raises KeyError for a slice nobody embedded.
            try:
                ok, reason = worker.still_qualifies()
                if not ok:
                    # §14.5: a device yielding logs at INFO, not WARNING. It is
                    # correct behavior — someone else wanted the card — not a
                    # fault.
                    log.info("gpu.worker[%s] yielding the device: %s",
                             worker.device.sysfs_name, reason)
                    worker.stats.yielded_reason = reason
                    worker.terminate()
                    requeue(index, chunk)
                    return
                vectors = worker.embed(
                    chunk, timeout=config.gpu_worker_slice_timeout_s
                )
            except GpuUnavailable as exc:
                log.warning("gpu.worker[%s] failed a slice (%s); "
                            "the slice will be retried elsewhere",
                            worker.device.sysfs_name, exc)
                worker.terminate()
                requeue(index, chunk)
                return
            except Exception:
                # 🔴 NOT DEFENSIVE PADDING. `still_qualifies` reads sysfs and
                # `unpack_vectors` parses a binary frame on THIS thread, so
                # struct.error and a decode error are both reachable outside the
                # GpuUnavailable contract. Without this clause the thread dies
                # via threading.excepthook — whose traceback goes to stderr and
                # never to cognita.log — carrying its slice with it, and the
                # walk reports `KeyError: 0` instead of a device fault. The
                # worker is terminated rather than left in `pool.alive`, or the
                # next window hands the same broken worker more slices.
                log.exception(
                    "gpu.worker[%s] slice failed unexpectedly; the slice will "
                    "be retried elsewhere and this device is being dropped",
                    worker.device.sysfs_name,
                )
                worker.stats.failed_reason = "unexpected slice error"
                worker.terminate()
                requeue(index, chunk)
                return
            with lock:
                results[index] = vectors

    # 🔴 Carry the context into the worker threads, or the job cannot see them.
    # `threading.Thread` starts with an EMPTY context — unlike
    # `asyncio.to_thread`, which copies it — so `record_batch` inside a drain
    # thread finds no active job and the walk's totals describe only whatever
    # ran on the calling thread. Observed as `chunks=3` for a walk that embedded
    # 2,036: the three were the canary calls, which do run on the caller's
    # thread. Same class of bug as the one this fixed a release ago, one layer
    # further in.
    # ⚠️ One copy PER THREAD. A `Context` cannot be entered twice at once —
    # sharing a single copy across the workers raises "cannot enter context:
    # ... is already entered" the moment the second thread starts, which would
    # take out every device but the first.
    threads = [
        threading.Thread(
            target=contextvars.copy_context().run, args=(drain, w), daemon=True
        )
        for w in pool.alive
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    # Anything left over — every device died or yielded — drains on the CPU
    # embedder, which was never unloaded (§8.5).
    for index, chunk in pending:
        pool.cpu_fallback_chunks += len(chunk)
        results[index] = cpu_embed(chunk)

    out: list[list[float]] = []
    for index in range(len(slices)):
        vectors = results.get(index)
        if vectors is None:
            # Belt and braces behind the drain threads' requeue contract: a
            # missing index means a slice was lost, and returning short (or
            # raising KeyError) would misalign every vector after it. Embedding
            # it here costs CPU time and keeps the answer correct.
            log.error("gpu slice %d was lost by its worker; embedding it on "
                      "the CPU so the window stays aligned", index)
            vectors = cpu_embed(slices[index])
            pool.cpu_fallback_chunks += len(slices[index])
        out.extend(vectors)
    return out
