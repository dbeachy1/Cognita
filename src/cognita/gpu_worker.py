"""The GPU embedding worker — a short-lived subprocess (DESIGN-6.0 §8).

🔴 **This module runs in the WORKER's virtual environment, not the service's.**
It must import nothing from `cognita`: the worker venv holds fastembed and a GPU
onnxruntime and knows nothing about Postgres, the config, or the store (§11).
Keep it dependency-free apart from the standard library and fastembed, or the
isolation that makes a broken GPU wheel a subprocess failure instead of an
outage stops being real.

**Why a subprocess at all.** VRAM must be released COMPLETELY when the work
finishes — not most of it, all of it. Destroying an in-process ORT session frees
the weights and the activation arena, but not the **driver context**: a
per-process, per-device allocation of 100-300 MB created the first time a
process touches a GPU, which lives until the process exits and which neither ORT
nor fastembed exposes a way to reclaim. Process exit is the only mechanism that
returns everything, so the GPU work happens in a process whose lifetime is the
walk. Cognita's VRAM usage between walks is zero, not "small".

**stdout is DATA ONLY.** Vectors go out as length-prefixed binary frames; a
stray `print()` would corrupt the stream. Every diagnostic goes to stderr, which
the parent relays through its own logger — the parent's `_RedactingFormatter` is
where token redaction lives, and a child writing to the log file directly would
bypass it (§14.6).

**Shape discipline (§5.1, measured in §12.3).** MIGraphX compiles per tensor
shape at 25-38 seconds a time. Compilation is keyed on shape and never on
content, so this worker pads every batch to ONE fixed shape and lets the
compiled program be cached to disk. Without that a walk presenting three batch
sizes paid three compiles and ran at 6.6 chunks/s — slower than the CPU it was
supposed to replace.
"""

from __future__ import annotations

import argparse
import faulthandler
import json
import os
import signal
import struct
import sys
import threading
import time
import traceback

# Frame: 4-byte big-endian length, then the payload. Same shape in both
# directions so one reader implementation serves both ends.
_HEADER = struct.Struct(">I")
MAX_FRAME_BYTES = 512 * 1024 * 1024  # a sanity bound, never a working limit

CANARY_TEXT = "Cognita indexes documents and answers questions about them."

# 15.0 (DESIGN-NVIDIA-ACCELERATION §6 item 3): the warm-up embeds one FULL batch
# of this text, not the ~12-token canary. It is a fixed filler of well over 600
# tokens (every word is at least one wordpiece, and this is 840 words), so it
# fills the model's whole 512-token context: the worst case a real slice can
# present. On CUDA that grows the memory arena to its high-water mark BEFORE the
# parent takes its `_settled_free` baseline from the first slice — measured at 1
# GB of growth at batch 16 on the first long batch — so the worker's own
# growth can never read as "somebody else took the headroom" and make
# `still_qualifies` yield the card for the rest of the walk (index correct,
# speed-up silently lost). On MIGraphX the shape is pinned, so this is the same
# shape as every slice: one batch, no extra compile.
WARMUP_TEXT = " ".join(
    ["the quick brown fox jumps over the lazy dog while a small cat watches"] * 60
)

# What ORT says when the installed NVIDIA driver is older than the CUDA runtime
# the wheels were built against. Matched case-insensitively.
_DRIVER_TOO_OLD_TEXT = "driver version is insufficient"


def construction_failure_reason(exc: BaseException) -> str:
    """The bounded reason token for a failed session build.

    `driver_too_old` when ORT's own text says the driver is insufficient for the
    CUDA runtime (walked along the exception chain, since fastembed wraps ORT's
    error); anything else is `construction_failed`. The parent scans the worker's
    stderr for `reason=<token>` and carries it into `GpuUnavailable.reason`.
    """
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if _DRIVER_TOO_OLD_TEXT in str(current).lower():
            return "driver_too_old"
        current = current.__cause__ or current.__context__
    return "construction_failed"


# --------------------------------------------------------------------------
# Framing — shared with the parent, which imports it from here by PATH, never
# by package import (the parent cannot import this module's dependencies).
# --------------------------------------------------------------------------


def write_frame(stream, payload: bytes) -> None:
    """Write one frame, looping until every byte is gone.

    🔴 A SHORT WRITE IS NOT AN ERROR AND `write()` DOES NOT RETRY IT. Both ends
    of this protocol are RAW, unbuffered pipes (`Popen(..., bufsize=0)` in the
    parent, `buffering=0` in the worker), so `write()` is one `write(2)` syscall
    and returns the count it managed. On a blocking pipe POSIX guarantees the
    full count only on *normal* completion: a signal delivered after a partial
    transfer returns short, and PEP 475 only retries EINTR when NOTHING was
    written. Frames here are far past `PIPE_BUF` — roughly 512 KB per request
    and 2 MB per reply — so this is a real window, not a theoretical one.
    Dropping the remainder desyncs the stream: the peer reads a length that is
    actually payload bytes and then blocks for data that will never come, until
    the slice timeout kills a healthy worker.

    The read side has had `_read_exactly` for exactly this reason since it
    shipped; the write side was the asymmetry.
    """
    _write_all(stream, _HEADER.pack(len(payload)))
    _write_all(stream, payload)
    stream.flush()


def _write_all(stream, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = stream.write(view)
        if not written:  # None (rare) or 0: nothing moved, so don't spin
            raise OSError("pipe accepted no bytes; the peer is gone")
        view = view[written:]


def read_frame(stream) -> bytes | None:
    """Read one frame, or None at clean end-of-stream.

    🔴 A SHORT read is not end-of-stream — a pipe can deliver a header in two
    pieces under load, and treating that as EOF would silently truncate a walk's
    vectors. Only a read of zero bytes at a frame boundary means the peer is
    gone.
    """
    header = _read_exactly(stream, _HEADER.size)
    if header is None:
        return None
    (length,) = _HEADER.unpack(header)
    if length > MAX_FRAME_BYTES:
        raise ValueError(f"frame of {length} bytes exceeds the {MAX_FRAME_BYTES} cap")
    body = _read_exactly(stream, length)
    if body is None:
        raise EOFError("stream closed mid-frame")
    return body


def _read_exactly(stream, count: int) -> bytes | None:
    """Exactly `count` bytes, None at a CLEAN boundary EOF, raise on a partial.

    🔴 The three outcomes must stay distinct. An earlier version returned `b""`
    for a partial read, which `read_frame` then handed back as a valid EMPTY
    frame — so a vector frame cut short by a dying worker would have been read
    as "this slice produced no vectors" and the walk would have carried on with
    a document's embeddings silently missing. Nothing would have raised, and the
    index would have looked complete.
    """
    chunks: list[bytes] = []
    remaining = count
    while remaining:
        chunk = stream.read(remaining)
        if not chunk:
            if remaining == count:
                return None  # nothing at all: the peer closed cleanly
            raise EOFError(
                f"stream closed after {count - remaining} of {count} bytes"
            )
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


# --------------------------------------------------------------------------
# Lifecycle — a worker must never outlive its parent (§8.6)
# --------------------------------------------------------------------------


def die_with_parent(parent_pid: int, interval: float = 1.0) -> bool:
    """Exit when the parent process is gone. Watches `getppid()`, in a thread.

    🔴 **On Linux a child does NOT die with its parent.** If `cognita serve` is
    killed with SIGKILL or segfaults, its `finally` never runs, this process is
    reparented, and it sits there holding VRAM indefinitely — exactly the
    failure the whole of §8 exists to prevent, arriving by the one route the
    happy path cannot cover.

    🔴 **THIS USED TO BE `prctl(PR_SET_PDEATHSIG, SIGKILL)` AND THAT WAS A LIVE
    BUG THAT DISABLED THE GPU ENTIRELY ON EVERY MULTI-CARD MACHINE.** Read the
    man page carefully: the death signal is delivered when **the THREAD that
    created this process terminates**, not when the parent PROCESS exits. 6.0.5
    made spin-up concurrent (§6.3), so from then on every worker was spawned
    from a short-lived thread that returned the instant the handshake landed —
    and the kernel SIGKILLed the worker a moment later. Both cards died within
    milliseconds of each other, before the canary could complete, and nothing
    was logged because SIGKILL is unblockable and instant.

    It hid for four releases behind three coincidences: `_run_per_worker` runs a
    SINGLE device inline on the caller's thread, so one-card boxes were fine and
    every manual one-card reproduction passed; the fallback to CPU is silent and
    correct, so the only symptom was a slower walk; and the parent never read
    the exit status, so `killed by signal 9` never reached the log (§14.8).
    Proven on kei 2026-09-02 by spawning the same two workers from the main
    thread (both canaries pass) and from a thread that exits (both SIGKILLed) —
    one variable, opposite outcomes.

    **Watching `getppid()` has none of that coupling.** It does not care which
    thread called `Popen`, it needs no `ctypes`, and it is correct on every
    POSIX platform. The cost is up to `interval` seconds of extra lifetime for
    an orphan, which is nothing against a walk, and stdin-EOF (§8.6 mechanism 2)
    usually gets there first anyway: when the parent dies its pipe fds close,
    which is what the worker is normally blocked on. This watcher is the guard
    for the case that mechanism cannot cover — an orphan in the middle of a long
    embed, not waiting on stdin at all.
    """
    if parent_pid <= 0:
        return False

    def watch() -> None:
        while True:
            time.sleep(interval)
            current = os.getppid()
            if current != parent_pid:
                # os._exit, not sys.exit: this is the orphan path, the parent is
                # already gone, and releasing the driver context by dying is the
                # entire point. An orderly unwind would only risk blocking.
                log(f"parent {parent_pid} is gone (ppid={current}); exiting to "
                    f"release VRAM", "WARNING")
                sys.stderr.flush()
                os._exit(0)

    threading.Thread(target=watch, name="parent-watch", daemon=True).start()
    return True


# --------------------------------------------------------------------------
# The model side
# --------------------------------------------------------------------------


def _read_bus_id(runtime, count_fn: str, bus_id_fn: str) -> str | None:
    """Ordinal 0's PCI bus id from one runtime's C API, or None on any failure.

    HIP and cudart spell the two calls differently (`hipGetDeviceCount` /
    `hipDeviceGetPCIBusId`, `cudaGetDeviceCount` / `cudaDeviceGetPCIBusId`) but
    have the same shape, and both return the 4-digit-domain form
    `0000:01:00.0` that the parent's probes also produce, so the two sides agree
    without any special-casing.
    """
    import ctypes

    try:
        count = ctypes.c_int(0)
        if getattr(runtime, count_fn)(ctypes.byref(count)) != 0 or count.value < 1:
            return None
        buf = ctypes.create_string_buffer(64)
        if getattr(runtime, bus_id_fn)(buf, 64, 0) != 0:
            return None
        return buf.value.decode().strip().lower()
    except Exception:
        return None


def bound_pci_address() -> str | None:
    """Which card did the GPU runtime actually give us? Read back, never assumed.

    The parent scopes this process to one device with the profile's device
    variable (`ROCR_VISIBLE_DEVICES` on AMD, `CUDA_VISIBLE_DEVICES` on NVIDIA),
    so ordinal 0 should be the card it meant. "Should be" is the problem: the
    sysfs card number is not the HIP ordinal (§12.3 correction 4), and an
    identifier that was assumed rather than confirmed is exactly how a run
    silently uses the wrong card while the log names the right one.

    HIP is tried first, as it always was. If no HIP library loads at all (an
    NVIDIA worker), cudart is read the same way: `libcudart.so.13`, then `.12`,
    then the unversioned name, found through the library path the parent's
    `.ld_library_path` marker applies (DESIGN-NVIDIA-ACCELERATION §6 item 1).
    A HIP library that loads but cannot answer returns None rather than falling
    through: the runtime that is present is the one that was bound.

    Best effort — a machine where the soname differs still works, it just
    cannot self-verify, and a diagnostic must never be the thing that stops a
    worker from running.
    """
    import ctypes

    for soname in ("libamdhip64.so.7", "libamdhip64.so.6", "libamdhip64.so"):
        try:
            hip = ctypes.CDLL(soname)
        except OSError:
            continue
        return _read_bus_id(hip, "hipGetDeviceCount", "hipDeviceGetPCIBusId")
    for soname in ("libcudart.so.13", "libcudart.so.12", "libcudart.so"):
        try:
            cudart = ctypes.CDLL(soname)
        except OSError:
            continue
        return _read_bus_id(cudart, "cudaGetDeviceCount", "cudaDeviceGetPCIBusId")
    return None


def _model_max_tokens(tokenizer) -> int | None:
    """What the model truncates at, read BEFORE we override it.

    fastembed derives this from the model's own `tokenizer_config.json` as
    `min(model_max_length, max_length)`. Reading it back is the only way to know
    whether a configured pin is inside the model's context or is quietly
    shortening it.
    """
    truncation = getattr(tokenizer, "truncation", None)
    if isinstance(truncation, dict):
        value = truncation.get("max_length")
        if isinstance(value, int) and value > 0:
            return value
    return None


def _pin_sequence_length(model, length: int) -> int | None:
    """Pad every batch to one fixed token length. Returns what was ACTUALLY
    pinned, or None if nothing was.

    Best effort and deliberately non-fatal: fastembed's internals are not a
    public API, and a worker that runs with variable shapes is slow, while a
    worker that refuses to start is useless.

    🔴 **TRUNCATION IS NOT PADDING, AND THE CANARY CANNOT SEE THE DIFFERENCE.**
    Padding to a fixed shape is numerically inert — the attention mask hides it,
    which is what makes the §5.1 shape pin safe. Truncation is not: it changes
    the text the model sees. `--fixed-seq-len` sets BOTH, so a pin below the
    model's own context silently shortens every chunk on the GPU path while the
    CPU path keeps the full text, and one corpus ends up holding two
    incompatible embeddings of the same document. §9.1's canary is ~12 tokens
    long, so it agrees to 1e-7 no matter how badly this is set — the one
    mechanism designed to catch device divergence is structurally blind to it.
    Hence: clamp to the model's own maximum, and say so out loud when the
    configuration would have cut into it.
    """
    for holder in (getattr(model, "model", None), model):
        tokenizer = getattr(holder, "tokenizer", None)
        if tokenizer is None or not hasattr(tokenizer, "enable_padding"):
            continue
        model_max = _model_max_tokens(tokenizer)
        pinned = length
        if model_max and length > model_max:
            # The loud direction — ORT throws per batch on a shape the model
            # cannot take — but clamping is still better than failing to start.
            pinned = model_max
            log(f"fixed_seq_len {length} exceeds this model's maximum context "
                f"{model_max}; pinning to {model_max}", "WARNING")
        elif model_max and length < model_max:
            # The SILENT direction, and the one that corrupts a corpus.
            log(f"fixed_seq_len {length} is BELOW this model's context of "
                f"{model_max}: every chunk longer than {length} tokens would be "
                f"truncated on the GPU but not on the CPU, so the two paths "
                f"would embed the same text differently. Pinning to {model_max} "
                f"instead. Lower gpu_fixed_seq_len only if the CPU path is "
                f"configured to match.", "WARNING")
            pinned = model_max
        try:
            tokenizer.enable_truncation(max_length=pinned)
        except Exception as exc:
            log(f"could not pin truncation to {pinned}: "
                f"{type(exc).__name__}: {exc}", "WARNING")
            return None
        try:
            tokenizer.enable_padding(length=pinned)
        except Exception as exc:
            # Truncation took and padding did not: the tokenizer is HALF pinned,
            # which is worth naming rather than reporting as "nothing happened".
            log(f"truncation pinned to {pinned} but padding did not: "
                f"{type(exc).__name__}: {exc} — shapes will still vary",
                "WARNING")
            return None
        return pinned
    log("no tokenizer found to pin sequence length on; shapes will vary (on "
        "MIGraphX that means a recompile per new shape; CUDA has no shape "
        "compile)", "WARNING")
    return None


def build_embedder(args) -> tuple[object, dict]:
    """Construct the GPU embedder, or exit non-zero with the reason.

    🔴 **The worker never falls back to a CPU provider internally.** A worker
    silently running on CPU is the worst outcome available: it consumes the
    spin-up, holds a process open, produces no speedup, and is the one failure
    the logging is designed to catch. Better to fail loudly and let the parent
    make the decision it already knows how to make — the CPU embedder in the
    service process was never unloaded (§8.5), so fallback is immediate.
    """
    import onnxruntime as ort
    from fastembed import TextEmbedding

    provider_options: dict = {}
    if args.pci_address:
        # Identity, not ordinal (§12.3 correction 4). ROCR_VISIBLE_DEVICES
        # scopes this process to exactly one card, so the provider's device_id
        # is always 0 and an out-of-memory on one card cannot reach the other.
        provider_options["device_id"] = 0
    if args.program_cache_dir:
        # 🔴 The compiled-program cache. Without it every worker start
        # recompiles the graph — measured at ~30s per shape, which turned a
        # 129 chunks/s device into 6.6 chunks/s, i.e. slower than the CPU.
        provider_options["migraphx_model_cache_dir"] = args.program_cache_dir

    kwargs = {
        "model_name": args.model,
        "cache_dir": args.cache_dir,
        "providers": [(args.provider, provider_options)],
    }
    model = TextEmbedding(**kwargs)

    facts = {
        "ort_version": ort.__version__,
        "provider_requested": args.provider,
        "available_providers": list(ort.get_available_providers()),
    }
    bound = bound_pci_address()
    facts["pci_bound"] = bound
    if bound and args.pci_address and bound != args.pci_address.strip().lower():
        # Refusing rather than warning: embedding on the wrong card is not a
        # cosmetic error. It means the gate's VRAM and utilization decisions
        # were made about a different device than the one doing the work, and
        # every §14.4 line would name the wrong one.
        raise RuntimeError(
            f"asked for {args.pci_address} but the GPU runtime bound {bound}; refusing to "
            "run on a device the parent did not gate"
        )
    # 🔴 Read the provider BACK from the live session, never assume it from
    # what was requested (§14.2). A GPU environment that loads and silently
    # falls back to CPU is invisible in every other signal — the run simply
    # takes longer, and nothing prompts anyone to look.
    session = getattr(getattr(model, "model", None), "model", None)
    active = list(session.get_providers()) if session is not None else []
    facts["provider_active"] = active
    if args.provider not in active:
        raise RuntimeError(
            f"{args.provider} was requested but the live session reports {active}. "
            "Refusing to run: a worker silently on CPU is worse than no worker."
        )
    # 🔴 PIN THE SEQUENCE LENGTH. This is the other half of the fixed shape,
    # and omitting it made the first live walk recompile on nearly every batch.
    #
    # Padding the batch dimension is not enough. The tokenizer pads each batch
    # to the longest text IN THAT BATCH, so a corpus of real documents produces
    # a new (batch, seq_len) pair almost every time — and MIGraphX compiles per
    # shape at ~40s. Observed on kei: "Model Compile: Begin" repeating
    # back-to-back for the whole walk, each one longer than the work it enabled.
    #
    # Forcing the tokenizer to pad to a FIXED length makes every batch the same
    # shape, so the graph compiles exactly once. It does not change any
    # embedding: padding tokens are masked out by the attention mask, which is
    # what the mask is for — the §9.1 canary is what proves that rather than
    # assuming it.
    if args.fixed_seq_len:
        # §8.4: report what ACTUALLY happened, not what was asked for. This
        # recorded the requested value unconditionally, so the handshake
        # asserted a pinned shape even when pinning had failed — and a worker
        # whose shapes vary recompiles per batch, which is the one thing the pin
        # exists to prevent. The parent now hears the real number.
        pinned = _pin_sequence_length(model, args.fixed_seq_len)
        if pinned:
            facts["fixed_seq_len"] = pinned

    description = getattr(getattr(model, "model", None), "model_description", None)
    if description is not None:
        facts["model_file"] = getattr(description, "model_file", None)
        source = getattr(description, "sources", None)
        facts["model_source"] = getattr(source, "hf", None) if source else None
    return model, facts


def embed_batch(model, texts: list[str], batch_size: int) -> list[list[float]]:
    """Embed one slice, streaming rather than materializing (5.8's lesson).

    🔴 The slice is padded to a FIXED batch size and the padding discarded
    (§5.1). On MIGraphX (the AMD profile) compiles happen per tensor shape at
    25-38 seconds a time — CUDA has no shape compile, but the padding is
    harmless there and keeps one code path — and
    compilation is keyed on shape, never on content — so a run that presents
    three different batch sizes pays three compiles and finishes SLOWER than
    the CPU it replaces. One shape means one compile for the life of the
    deployment, cached to disk.

    Padding with a repeat of the first text rather than an empty string: an
    empty input can take a different path through tokenization, and the padded
    rows are thrown away regardless, so the cheapest correct filler is one we
    already know embeds normally.
    """
    if not texts:
        return []
    wanted = len(texts)
    padded = list(texts)
    if len(padded) % batch_size:
        filler = padded[0]
        padded.extend([filler] * (batch_size - len(padded) % batch_size))
    # 🔴 STREAM, do not materialize. This said `[v.tolist() for v in ...]`,
    # which is the exact construct CLAUDE.md's 5.8 bullet names as the
    # counter-example — while the docstring above claimed 5.8's lesson was being
    # followed. The comprehension holds the generator's whole output alongside
    # the converted list, so peak transient memory is the entire slice rather
    # than one batch. The CPU embedder has used the append form since 5.8; the
    # worker was written to look like it did.
    vectors: list[list[float]] = []
    for vector in model.embed(padded, batch_size=batch_size):
        vectors.append(vector.tolist())
    return vectors[:wanted]


def pack_vectors(vectors: list[list[float]]) -> bytes:
    """Raw float32 back, not JSON.

    A 17,600-chunk rebuild returns about 72 MB, which is trivial over a pipe
    but wasteful to serialize as text. Vector order matches input order within a
    frame; chunk identity stays with the parent, so the worker never needs to
    understand documents, projects or the store (§8.4).
    """
    if not vectors:
        return _HEADER.pack(0) + _HEADER.pack(0)
    rows, cols = len(vectors), len(vectors[0])
    flat = [value for row in vectors for value in row]
    return _HEADER.pack(rows) + _HEADER.pack(cols) + struct.pack(f"<{len(flat)}f", *flat)


def unpack_vectors(payload: bytes) -> list[list[float]]:
    """The parent's half of pack_vectors."""
    rows = _HEADER.unpack_from(payload, 0)[0]
    cols = _HEADER.unpack_from(payload, _HEADER.size)[0]
    if rows == 0 or cols == 0:
        return []
    offset = _HEADER.size * 2
    flat = struct.unpack_from(f"<{rows * cols}f", payload, offset)
    return [list(flat[i * cols:(i + 1) * cols]) for i in range(rows)]


def claim_stdout():
    """Take private ownership of fd 1 and point the public one at stderr.

    🔴 **Not defensive coding — a fix for an observed corruption.** The module
    docstring says stdout is data only and a stray `print()` would corrupt the
    stream. That understates it: the noisiest writer is not Python at all.
    ONNX Runtime prints its provider banner —

        *************** EP Error ***************
        ... Falling back to ['CPUExecutionProvider'] and retrying.

    — to **stdout, from C++**, where no amount of Python discipline can stop it.
    Observed live: the parent read a frame header of 707406378 bytes, which is
    0x2A2A2A2A, i.e. four asterisks of that banner.

    So the worker dups fd 1 to a private descriptor, writes frames there, and
    redirects the real fd 1 to fd 2. Anything that prints to stdout — this
    process, a library, a C extension — lands harmlessly in the stderr stream
    the parent already relays to the log (§14.6), and the data channel cannot
    be corrupted by output at all.
    """
    private = os.dup(1)
    os.dup2(2, 1)
    return os.fdopen(private, "wb", buffering=0)


def log(message: str, level: str = "INFO") -> None:
    """Structured line to STDERR. The parent relays it through its own logger.

    Never to the log file: two processes appending interleave badly, the worker
    has no business knowing where the log lives, and `_RedactingFormatter` —
    which enforces a standing security invariant — is in the parent (§14.6).
    """
    sys.stderr.write(f"{level} {message}\n")
    sys.stderr.flush()


def log_exception(message: str) -> None:
    """🔴 6.0.8: the WHOLE traceback, one leveled line per frame.

    `f"{type(exc).__name__}: {exc}"` was all the worker ever said about a
    failure, which names the exception and hides every line of code that led to
    it. A `RuntimeError` from somewhere inside MIGraphX is not a diagnosis.

    Each line carries the ERROR prefix on purpose: the parent's relay levels a
    line by its first word, so a bare multi-line traceback would arrive as a
    stack of unlevelled INFO lines and be invisible at the default level —
    exactly when the process is dying and the lines matter most.
    """
    log(message, "ERROR")
    for line in traceback.format_exc().rstrip().splitlines():
        log(f"  {line}", "ERROR")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Cognita GPU embedding worker")
    ap.add_argument("--model", required=True)
    ap.add_argument("--cache-dir", required=True)
    ap.add_argument("--provider", default="MIGraphXExecutionProvider")
    ap.add_argument("--pci-address", default="")
    ap.add_argument("--program-cache-dir", default="")
    ap.add_argument("--batch-size", type=int, default=64)
    ap.add_argument("--fixed-seq-len", type=int, default=512)
    ap.add_argument("--dimensions", type=int, default=1024)
    # 6.0.9: whose child this is. See die_with_parent — the old
    # PR_SET_PDEATHSIG was keyed to the spawning THREAD and killed
    # healthy workers on every concurrent spin-up.
    ap.add_argument("--parent-pid", type=int, default=0)
    args = ap.parse_args(argv)

    # 🔴 6.0.8 §14.8: A NATIVE CRASH MUST LEAVE A STACK TRACE. Almost everything
    # this process does is C++ — HIP, MIGraphX, ONNX Runtime — so its most
    # likely death is a SIGSEGV or an abort() inside a shared library, and
    # Python's own `except` cannot see either. Without this the process simply
    # vanishes: the parent reads EOF, reports "worker exited mid-slice", and
    # there is NOTHING anywhere saying what faulted. Observed live on
    # 2026-09-01/02 — three separate double-card failures with no cause on
    # record.
    #
    # faulthandler writes the C-level stack of every thread to stderr, which the
    # parent already relays into the log. It costs nothing until something dies.
    faulthandler.enable(file=sys.stderr, all_threads=True)
    if hasattr(signal, "SIGUSR1"):
        # And the twin case: a WEDGED worker, which `embed()`'s slice timeout
        # kills without ever learning where it was stuck. `kill -USR1 <pid>`
        # now dumps every thread's stack before that happens.
        faulthandler.register(signal.SIGUSR1, file=sys.stderr,
                              all_threads=True, chain=False)

    die_with_parent(args.parent_pid)
    # Claim the data channel BEFORE constructing anything: the provider
    # banner is emitted during session construction, which is exactly when
    # the handshake frame is about to be written.
    data_out = claim_stdout()

    log(f"worker starting pid={os.getpid()} device={args.pci_address} "
        f"model={args.model} batch={args.batch_size} "
        f"seq_len={args.fixed_seq_len} provider={args.provider}", "DEBUG")

    build_started = time.monotonic()
    try:
        model, facts = build_embedder(args)
    except Exception as exc:
        # 15.0: NAME the failure at the source. The parent scans this line for
        # `reason=<token>` and carries it into GpuUnavailable.reason, which is how
        # an old NVIDIA driver becomes "driver_too_old" rather than a generic
        # "runtime missing" (DESIGN-NVIDIA-ACCELERATION §6 item 4, §7).
        log_exception(
            f"worker construction failed reason={construction_failure_reason(exc)}"
        )
        return 2
    log(f"session built in {time.monotonic() - build_started:.2f}s", "DEBUG")

    # The handshake carries everything §14.2's embed.gpu.init line reports.
    # Sent on stdout as a frame so the parent reads it with the same reader it
    # uses for vectors.
    write_frame(data_out, json.dumps({"event": "ready", **facts}).encode())

    # Warm the graph once so the first real slice does not pay compilation, and
    # so the canary below is measured against a warm session.
    #
    # ⚠️ THE HANDSHAKE HAS ALREADY GONE OUT, so the parent has logged
    # `embed.gpu.init` and may already have written its canary request into the
    # pipe while this runs. That is harmless when the warmup succeeds — the
    # request waits in the buffer — but it is why a warmup failure surfaces in
    # the parent as "worker exited mid-slice" for a slice that never started.
    # The exit code is what tells the two apart, which is why 6.0.8 makes the
    # parent read it.
    #
    # 15.0: the warm-up is one FULL batch of full-context text (`WARMUP_TEXT` x
    # batch_size), not the one-line canary, so the arena reaches its high-water
    # mark before the parent takes its settled-free baseline. See WARMUP_TEXT.
    warm_started = time.monotonic()
    try:
        embed_batch(model, [WARMUP_TEXT] * args.batch_size, args.batch_size)
    except Exception:
        log_exception("warmup failed")
        return 3
    log(f"warmup ok in {time.monotonic() - warm_started:.2f}s "
        f"({args.batch_size} x {len(WARMUP_TEXT)} chars); ready for slices",
        "DEBUG")

    while True:
        try:
            # Exit on stdin EOF (§8.6 mechanism 2). Covers a parent that dies
            # without signaling, on any platform, and costs one loop condition.
            frame = read_frame(sys.stdin.buffer)
        except Exception:
            log_exception("protocol error")
            return 4
        if frame is None:
            log("stdin closed; exiting", "DEBUG")
            return 0
        try:
            request = json.loads(frame)
        except Exception:
            log_exception("undecodable request")
            return 4
        if request.get("op") == "shutdown":
            log("shutdown requested", "DEBUG")
            return 0
        texts = request.get("texts") or []
        slice_started = time.monotonic()
        log(f"slice in: {len(texts)} texts, {sum(len(t) for t in texts)} chars",
            "DEBUG")
        try:
            vectors = embed_batch(model, texts, args.batch_size)
        except Exception:
            # A failed slice is the parent's to requeue; this process is no
            # longer trustworthy for GPU work, so it exits rather than
            # continuing in an unknown state.
            log_exception(f"embed failed for {len(texts)} texts")
            return 5
        if len(vectors) != len(texts):
            log(f"count mismatch: {len(texts)} in, {len(vectors)} out", "ERROR")
            return 5
        log(f"slice out: {len(vectors)} vectors in "
            f"{time.monotonic() - slice_started:.2f}s", "DEBUG")
        write_frame(data_out, pack_vectors(vectors))


if __name__ == "__main__":  # pragma: no cover - process entry point
    os.environ.setdefault("PYTHONUNBUFFERED", "1")
    sys.exit(main())
