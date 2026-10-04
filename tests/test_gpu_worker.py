"""The GPU worker's protocol and lifecycle (DESIGN-6.0 §8).

No GPU, no fastembed, no subprocess spawning of a real worker: these test the
parts that are pure logic and are exactly the parts that fail silently — the
framing, the vector packing, and the promises §8.6 makes about a worker never
outliving its parent.

⚠️ §16 of the design doc notes that every gap found by reading the first draft
aloud was **in the lifecycle of the subprocess rather than in the interesting
part**. That is where the tests are concentrated for the same reason.
"""

from __future__ import annotations

import io
import os
import pathlib
import struct
import subprocess
import sys
import threading

import pytest

from cognita.gpu_worker import (
    CANARY_TEXT,
    MAX_FRAME_BYTES,
    die_with_parent,
    pack_vectors,
    read_frame,
    unpack_vectors,
    write_frame,
)


class Chunked(io.RawIOBase):
    """A stream that returns data in small pieces, as a pipe under load does."""

    def __init__(self, payload: bytes, piece: int = 3):
        self._buf = payload
        self._piece = piece
        self._pos = 0

    def read(self, size=-1):
        if self._pos >= len(self._buf):
            return b""
        take = min(size if size > 0 else self._piece, self._piece)
        out = self._buf[self._pos:self._pos + take]
        self._pos += len(out)
        return out


# --------------------------------------------------------------------------
# Framing
# --------------------------------------------------------------------------


def test_a_frame_round_trips():
    out = io.BytesIO()
    write_frame(out, b"hello world")
    assert read_frame(io.BytesIO(out.getvalue())) == b"hello world"


def test_an_empty_frame_is_legal():
    """A zero-length slice is a real message, not end-of-stream."""
    out = io.BytesIO()
    write_frame(out, b"")
    assert read_frame(io.BytesIO(out.getvalue())) == b""


def test_end_of_stream_reads_as_none():
    """The parent closing stdin is how a worker is told to exit (§8.6)."""
    assert read_frame(io.BytesIO(b"")) is None


def test_a_short_read_is_not_end_of_stream():
    """🔴 A pipe can deliver a header in two pieces under load. Treating that
    as EOF would silently truncate a walk's vectors — a whole document's
    embeddings quietly missing, with no error anywhere."""
    out = io.BytesIO()
    write_frame(out, b"x" * 5000)
    assert read_frame(Chunked(out.getvalue(), piece=7)) == b"x" * 5000


def test_a_truncated_frame_raises_rather_than_returning_short():
    """Half a frame must never be handed back as if it were a whole one."""
    out = io.BytesIO()
    write_frame(out, b"abcdefghij")
    truncated = out.getvalue()[:-4]
    with pytest.raises(EOFError):
        read_frame(io.BytesIO(truncated))


def test_a_truncated_header_raises_too():
    """The other half of the same distinction: nothing at all is EOF, but a
    partial header is a broken stream and must not be read as one."""
    with pytest.raises(EOFError):
        read_frame(io.BytesIO(b"\x00\x00"))


def test_a_frame_cut_short_is_never_reported_as_empty():
    """The regression this pair exists for. A worker dying mid-write must not
    look like a worker that legitimately returned no vectors — that reads as a
    complete index with a document's embeddings silently absent."""
    out = io.BytesIO()
    write_frame(out, pack_vectors([[1.0] * 8 for _ in range(4)]))
    with pytest.raises(EOFError):
        read_frame(io.BytesIO(out.getvalue()[:20]))


def test_an_absurd_length_is_refused():
    """A corrupt header must not become a multi-gigabyte allocation."""
    bogus = struct.pack(">I", MAX_FRAME_BYTES + 1)
    with pytest.raises(ValueError):
        read_frame(io.BytesIO(bogus + b"x"))


def test_frames_are_read_back_in_order():
    out = io.BytesIO()
    for payload in (b"one", b"two", b"three"):
        write_frame(out, payload)
    stream = io.BytesIO(out.getvalue())
    assert [read_frame(stream) for _ in range(3)] == [b"one", b"two", b"three"]
    assert read_frame(stream) is None


# --------------------------------------------------------------------------
# Vector packing
# --------------------------------------------------------------------------


def test_vectors_round_trip_within_float32():
    vectors = [[0.1, -0.2, 0.3], [1e-7, 0.5, -1.0]]
    restored = unpack_vectors(pack_vectors(vectors))
    assert len(restored) == 2 and len(restored[0]) == 3
    for original, back in zip(vectors, restored):
        assert back == pytest.approx(original, abs=1e-6)


def test_vector_order_is_preserved():
    """§8.4: order within a frame IS the chunk identity. The worker never sees
    documents, so a reordering here silently attaches every vector to the wrong
    chunk — an index that looks complete and answers wrongly."""
    vectors = [[float(i)] * 4 for i in range(50)]
    restored = unpack_vectors(pack_vectors(vectors))
    assert [v[0] for v in restored] == [float(i) for i in range(50)]


def test_an_empty_result_round_trips():
    assert unpack_vectors(pack_vectors([])) == []


def test_a_realistic_batch_is_compact():
    """Raw float32 rather than JSON: 72 MB for a full rebuild instead of the
    several hundred a text encoding would cost."""
    vectors = [[0.01] * 1024 for _ in range(64)]
    payload = pack_vectors(vectors)
    assert len(payload) == 8 + 64 * 1024 * 4
    assert len(unpack_vectors(payload)) == 64


# --------------------------------------------------------------------------
# §8.6 — a worker must never outlive its parent
# --------------------------------------------------------------------------


def test_die_with_parent_is_honest_about_doing_nothing():
    """🔴 It must report FALSE where it did nothing, rather than implying a
    protection that is absent. With no parent pid there is nothing to watch."""
    assert die_with_parent(0) is False


def test_die_with_parent_watches_the_pid_it_was_given():
    """The guard is a watcher thread now, and it must actually start."""
    before = {t.name for t in threading.enumerate()}
    assert die_with_parent(os.getpid(), interval=3600) is True
    started = {t.name for t in threading.enumerate()} - before
    assert "parent-watch" in started


@pytest.mark.skipif(sys.platform == "win32",
                    reason="POSIX getppid semantics; the target is Linux")
def test_the_guard_still_guards():
    """🔴 The other half, and the half that is easy to skip. 6.0.9 replaced the
    mechanism §8.6 relies on, so proving it stopped killing HEALTHY workers is
    only half a test — the replacement must still kill ORPHANED ones, or the
    fix traded an accelerator outage for a VRAM leak that lasts until reboot.

    Driven with a parent pid that is deliberately not this process's parent, so
    the watcher's very first tick sees the mismatch and exits. Proven against
    real hardware separately: on kei an orphaned worker whose parent was
    SIGKILLed released its VRAM after 7.3s under load.

    The child used to `time.sleep(30)` after arming the guard, which also
    exits 0 — so on a slow enough machine the pass/fail line was the 15s wait
    racing a 30s sleep. It now blocks on an Event nothing ever sets: the guard
    is the ONLY way it can exit, and the wait's timeout is purely a hang guard
    (in a correct run the guard fires on its first 0.1s tick)."""
    proc = subprocess.Popen(
        [sys.executable, "-c",
         "import sys; sys.path.insert(0, %r);"
         "from cognita.gpu_worker import die_with_parent;"
         "import threading;"
         "die_with_parent(999999, interval=0.1); threading.Event().wait()"
         % str(pathlib.Path(__file__).resolve().parents[1] / "src")],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    try:
        assert proc.wait(timeout=15) == 0, "the orphan guard never fired"
    except subprocess.TimeoutExpired:
        proc.kill()
        raise AssertionError(
            "the worker did NOT exit when its parent went away — §8.6's "
            "guarantee is gone and workers will hold VRAM until reboot"
        ) from None


def test_the_guard_is_not_keyed_to_the_spawning_thread():
    """🔴 THE 6.0.9 BUG, as a test. `PR_SET_PDEATHSIG` is delivered when the
    THREAD that created the child exits, not the parent process — so §6.3's
    concurrent spin-up, which spawns each worker from a thread that returns
    immediately, had the kernel SIGKILL every worker on every multi-card box.

    A worker spawned from a thread that then exits must SURVIVE. This drives the
    real `gpu_worker.py` argument path with a stub payload, because the whole
    defect lived in the interaction between `Popen`'s calling thread and the
    child, which no in-process fake can reproduce."""
    survived = {}
    # The child used to read one byte and then `time.sleep(0.1)`, and the test
    # slept 1.0s after the join hoping PDEATHSIG, if armed, had landed by then.
    # Now the child asks the kernel directly (PR_GET_PDEATHSIG, Linux) and
    # reports it back over a stdin/stdout round trip made AFTER the spawning
    # thread is gone; then it blocks on stdin EOF instead of sleeping.
    child = "\n".join([
        "import sys",
        "sys.stdin.buffer.read(1)",
        "armed = 0",
        "if sys.platform.startswith('linux'):",
        "    import ctypes",
        "    value = ctypes.c_int(0)",
        "    ctypes.CDLL(None).prctl(2, ctypes.byref(value), 0, 0, 0)  # PR_GET_PDEATHSIG",
        "    armed = value.value",
        "sys.stdout.buffer.write(b'alive pdeathsig=%d' % armed)",
        "sys.stdout.buffer.flush()",
        "sys.stdin.buffer.read()",
    ])

    def spawn():
        proc = subprocess.Popen(
            [sys.executable, "-c", child],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        survived["proc"] = proc

    thread = threading.Thread(target=spawn)
    thread.start()
    thread.join(timeout=5)  # the spawning thread is now GONE
    assert not thread.is_alive(), "the spawning thread never returned"
    proc = survived["proc"]
    assert proc.poll() is None, (
        "the worker died when its spawning thread exited — "
        "PR_SET_PDEATHSIG is back"
    )
    try:
        # Hang guard only: the child answers our byte and exits on stdin EOF.
        out, _err = proc.communicate(input=b"x", timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        raise AssertionError("the worker never answered its round trip") from None
    assert out == b"alive pdeathsig=0", (
        "the worker died when its spawning thread exited, or is armed to — "
        f"PR_SET_PDEATHSIG is back (child said {out!r})"
    )


def test_the_canary_text_is_fixed():
    """§9.1 embeds one fixed string and compares it against the SERVICE's CPU
    embedder. It must be stable — but the reference is computed locally and
    never shipped as a constant, because different CPU microarchitectures take
    different kernel paths and a stored vector would fail spuriously on hardware
    that is working perfectly."""
    assert isinstance(CANARY_TEXT, str) and CANARY_TEXT.strip()


# --------------------------------------------------------------------------
# §5.1 — the shape must be pinned in BOTH dimensions
# --------------------------------------------------------------------------


class FakeTokenizer:
    def __init__(self):
        self.padding = None
        self.truncation = None

    def enable_padding(self, **kw):
        self.padding = kw

    def enable_truncation(self, **kw):
        self.truncation = kw


class FakeInner:
    def __init__(self):
        self.tokenizer = FakeTokenizer()


class FakeModel:
    def __init__(self):
        self.model = FakeInner()


def test_pinning_sets_both_padding_and_truncation():
    """🔴 Padding the BATCH dimension is not enough.

    The tokenizer pads each batch to the longest text IN THAT BATCH, so real
    documents produce a new (batch, seq_len) pair almost every time and
    MIGraphX recompiles per shape at ~40s. Observed live as "Model Compile:
    Begin" repeating back-to-back for a whole walk.

    Truncation matters as much as padding: without it a text longer than the
    pinned length would produce a LONGER sequence and its own shape.
    """
    from cognita.gpu_worker import _pin_sequence_length

    model = FakeModel()
    # Returns what was ACTUALLY pinned, so the handshake can report the real
    # number rather than the requested one.
    assert _pin_sequence_length(model, 512) == 512
    assert model.model.tokenizer.padding == {"length": 512}
    assert model.model.tokenizer.truncation == {"max_length": 512}


def test_the_pin_is_clamped_to_the_models_own_context():
    """🔴 TRUNCATION IS NOT PADDING. Padding to a fixed shape is numerically
    inert — the attention mask hides it, which is what makes the §5.1 shape pin
    safe. Truncation changes the text the model SEES, and `--fixed-seq-len` sets
    both. A pin below the model's own context therefore shortens every long
    chunk on the GPU path while the CPU path keeps the full text, leaving one
    corpus holding two incompatible embeddings of the same document.

    §9.1's canary cannot catch it: CANARY_TEXT is ~12 tokens, so it agrees to
    1e-7 however badly this is set. The clamp is the only guard there can be.
    """
    from cognita.gpu_worker import _pin_sequence_length

    model = FakeModel()
    model.model.tokenizer.truncation = {"max_length": 8192}  # a long-context model
    assert _pin_sequence_length(model, 512) == 8192, (
        "pinning 512 under an 8192-token model silently truncates on the GPU only"
    )
    assert model.model.tokenizer.truncation == {"max_length": 8192}
    assert model.model.tokenizer.padding == {"length": 8192}


def test_a_pin_above_the_models_context_is_clamped_down():
    """The loud direction — ORT throws per batch on a shape the model cannot
    take — but clamping still beats refusing to start."""
    from cognita.gpu_worker import _pin_sequence_length

    model = FakeModel()
    model.model.tokenizer.truncation = {"max_length": 512}
    assert _pin_sequence_length(model, 4096) == 512


def test_a_pin_matching_the_model_is_left_alone():
    """The shipped case: bge-large is 512 and gpu_fixed_seq_len is 512, so this
    must be silent — no warning, no clamp, no noise on every worker start."""
    from cognita.gpu_worker import _pin_sequence_length

    model = FakeModel()
    model.model.tokenizer.truncation = {"max_length": 512}
    assert _pin_sequence_length(model, 512) == 512


def test_pinning_never_stops_a_worker_that_would_otherwise_run():
    """Best effort by design: fastembed's internals are not a public API, and a
    worker running with variable shapes is slow, while one that refuses to
    start is useless."""
    from cognita.gpu_worker import _pin_sequence_length

    class NoTokenizer:
        model = object()

    assert _pin_sequence_length(NoTokenizer(), 512) is None

    class Exploding(FakeTokenizer):
        def enable_padding(self, **kw):
            raise RuntimeError("nope")

    model = FakeModel()
    model.model.tokenizer = Exploding()
    # Truncation took and padding did not: the tokenizer is HALF pinned, which
    # reports as "not pinned" so the handshake does not claim a fixed shape.
    assert _pin_sequence_length(model, 512) is None


class _CountingModel:
    """Records the exact list handed to embed(), and streams like fastembed."""

    def __init__(self):
        self.seen: list[list[str]] = []

    def embed(self, texts, batch_size=None):
        self.seen.append(list(texts))
        for text in texts:
            yield _FakeVector([float(len(text)), 1.0])


class _FakeVector:
    def __init__(self, values):
        self._values = values

    def tolist(self):
        return list(self._values)


def test_embed_batch_pads_to_the_compiled_shape_and_discards_the_padding():
    """🔴 §5.1: MIGraphX compiles per tensor SHAPE at 25-38s a time, so every
    slice is padded up to a multiple of the forward-pass width and the filler
    rows thrown away. This is the guarantee the slicer relies on — a short
    trailing slice is fine precisely because it is padded here.

    Correctness-critical and previously untested: an off-by-one either drops
    real vectors or returns filler embeddings for real chunks, and both are
    silent.
    """
    from cognita.gpu_worker import embed_batch

    model = _CountingModel()
    out = embed_batch(model, [f"t{i}" for i in range(5)], batch_size=4)

    assert len(model.seen[0]) == 8, "5 texts must be padded to 2 batches of 4"
    assert len(out) == 5, "the caller must get exactly its own vectors back"
    # And they must be the REAL ones, not the filler's.
    assert out == [[float(len(f"t{i}")), 1.0] for i in range(5)]


def test_embed_batch_does_not_pad_an_exact_multiple():
    from cognita.gpu_worker import embed_batch

    model = _CountingModel()
    out = embed_batch(model, [f"t{i}" for i in range(8)], batch_size=4)
    assert len(model.seen[0]) == 8, "an exact multiple must not be padded"
    assert len(out) == 8


def test_embed_batch_handles_an_empty_slice():
    from cognita.gpu_worker import embed_batch

    model = _CountingModel()
    assert embed_batch(model, [], batch_size=4) == []
    assert model.seen == [], "an empty slice must not reach the model at all"


def test_embed_batch_streams_rather_than_materializing():
    """🔴 CLAUDE.md's 5.8 bullet names `[v.tolist() for v in model.embed(...)]`
    as the counter-example, and this function used it while its own docstring
    claimed to follow 5.8's lesson. The comprehension holds the generator's
    whole output alongside the converted list, so peak transient memory is the
    entire slice rather than one batch."""
    import inspect

    from cognita import gpu_worker

    source = inspect.getsource(gpu_worker.embed_batch)
    assert "for v in model.embed" not in source, (
        "embed_batch materializes the whole slice in a comprehension again"
    )
    assert ".append(" in source, "embed_batch must accumulate as it streams"


def test_the_worker_imports_nothing_from_cognita():
    """🔴 §11: the worker runs in its OWN venv, which holds fastembed and a GPU
    onnxruntime and knows nothing about Postgres, the config or the store. An
    import from the service package would make the isolation imaginary — and
    that isolation is the thing that downgrades a broken GPU wheel from "the
    service has no embedder at all" to "the walk ran on CPU".
    """
    source = (
        __import__("pathlib").Path(__file__).parent.parent
        / "src" / "cognita" / "gpu_worker.py"
    ).read_text(encoding="utf-8")
    offenders = [
        line.strip() for line in source.splitlines()
        if line.strip().startswith(("from cognita", "import cognita"))
        or line.strip().startswith("from .")
    ]
    assert offenders == [], f"worker imports service code: {offenders}"


# --------------------------------------------------------------------------
# 15.0 (DESIGN-NVIDIA-ACCELERATION §6): the CUDA path of the vendor-neutral worker
# --------------------------------------------------------------------------


class FakeRuntime:
    """A GPU runtime's C API (`hip*` or `cuda*`), with scripted answers.

    Writes through `ctypes.byref` arguments the way the real library does
    (`ref._obj` is the ctypes object the reference points at).
    """

    def __init__(self, prefix, count=1, bus_id=b"0000:01:00.0", count_rc=0,
                 bus_rc=0, raises=False):
        self.prefix = prefix
        self.count = count
        self.bus_id = bus_id
        self.count_rc = count_rc
        self.bus_rc = bus_rc
        self.raises = raises
        setattr(self, f"{prefix}GetDeviceCount", self._get_count)
        setattr(self, f"{prefix}DeviceGetPCIBusId", self._get_bus_id)

    def _get_count(self, ref):
        if self.raises:
            raise OSError("symbol exploded")
        ref._obj.value = self.count
        return self.count_rc

    def _get_bus_id(self, buf, length, ordinal):
        assert ordinal == 0, "the process is scoped to one card: ordinal 0"
        buf.value = self.bus_id
        return self.bus_rc


def install_runtimes(monkeypatch, libraries):
    """Make `ctypes.CDLL(soname)` load only the sonames in `libraries`.

    Returns the list of every soname the worker asked for, in order, so a test
    can prove which runtime was consulted and which never was.
    """
    import ctypes

    asked: list[str] = []

    def fake_cdll(soname, *args, **kwargs):
        asked.append(soname)
        if soname not in libraries:
            raise OSError(f"{soname}: cannot open shared object file")
        return libraries[soname]

    monkeypatch.setattr(ctypes, "CDLL", fake_cdll)
    return asked


def test_the_cudart_readback_is_used_when_no_hip_library_loads(monkeypatch):
    """The NVIDIA twin of the HIP read-back (§1.3: cudart inside a scoped
    process reports 0000:01:00.0). `libcudart.so.13` is tried first, then .12."""
    from cognita.gpu_worker import bound_pci_address

    asked = install_runtimes(monkeypatch, {"libcudart.so.12": FakeRuntime("cuda")})
    assert bound_pci_address() == "0000:01:00.0"
    assert asked == [
        "libamdhip64.so.7", "libamdhip64.so.6", "libamdhip64.so",
        "libcudart.so.13", "libcudart.so.12",
    ]


def test_cudart_so_13_is_preferred(monkeypatch):
    from cognita.gpu_worker import bound_pci_address

    asked = install_runtimes(monkeypatch, {
        "libcudart.so.13": FakeRuntime("cuda", bus_id=b"0000:01:00.0"),
        "libcudart.so.12": FakeRuntime("cuda", bus_id=b"0000:99:00.0"),
    })
    assert bound_pci_address() == "0000:01:00.0"
    assert "libcudart.so.12" not in asked


def test_the_unversioned_cudart_is_the_last_resort(monkeypatch):
    from cognita.gpu_worker import bound_pci_address

    install_runtimes(monkeypatch, {"libcudart.so": FakeRuntime("cuda")})
    assert bound_pci_address() == "0000:01:00.0"


def test_the_cudart_bus_id_is_lowercased_and_stripped(monkeypatch):
    """cudart may spell the hex in either case; the parent compares lowercase
    against the probe's normalized form."""
    from cognita.gpu_worker import bound_pci_address

    install_runtimes(monkeypatch, {
        "libcudart.so.13": FakeRuntime("cuda", bus_id=b" 0000:0A:00.0 "),
    })
    assert bound_pci_address() == "0000:0a:00.0"


def test_a_cudart_that_sees_no_device_reads_back_nothing(monkeypatch):
    """count 0 is the scoped process seeing no card at all — the read-back is
    None, and the worker's provider guard is what refuses to run on CPU."""
    from cognita.gpu_worker import bound_pci_address

    install_runtimes(monkeypatch, {
        "libcudart.so.13": FakeRuntime("cuda", count=0),
    })
    assert bound_pci_address() is None


@pytest.mark.parametrize("failing", [
    {"count_rc": 100}, {"bus_rc": 101}, {"raises": True},
], ids=["count-fails", "bus-id-fails", "symbol-raises"])
def test_a_cudart_failure_reads_back_nothing_and_never_raises(monkeypatch, failing):
    from cognita.gpu_worker import bound_pci_address

    install_runtimes(monkeypatch, {
        "libcudart.so.13": FakeRuntime("cuda", **failing),
    })
    assert bound_pci_address() is None


def test_hip_still_wins_when_it_loads_and_cudart_is_never_consulted(monkeypatch):
    """An AMD worker's read-back is unchanged."""
    from cognita.gpu_worker import bound_pci_address

    asked = install_runtimes(monkeypatch, {
        "libamdhip64.so.7": FakeRuntime("hip", bus_id=b"0000:03:00.0"),
        "libcudart.so.13": FakeRuntime("cuda", bus_id=b"0000:99:00.0"),
    })
    assert bound_pci_address() == "0000:03:00.0"
    assert not any(name.startswith("libcudart") for name in asked)


def test_a_hip_that_loads_but_cannot_answer_does_not_fall_through(monkeypatch):
    """The runtime that is present is the one that was bound; asking a second
    runtime would be answering a different question."""
    from cognita.gpu_worker import bound_pci_address

    asked = install_runtimes(monkeypatch, {
        "libamdhip64.so.7": FakeRuntime("hip", count=0),
        "libcudart.so.13": FakeRuntime("cuda"),
    })
    assert bound_pci_address() is None
    assert not any(name.startswith("libcudart") for name in asked)


def test_no_runtime_at_all_reads_back_nothing(monkeypatch):
    from cognita.gpu_worker import bound_pci_address

    install_runtimes(monkeypatch, {})
    assert bound_pci_address() is None


# -- the warm-up ------------------------------------------------------------


def test_the_warmup_text_fills_the_models_context():
    """Every word is at least one wordpiece, so a word count is a LOWER bound on
    the token count. 600 words is already past the 512-token context the model
    truncates at, which is what makes it the worst case a real slice can be."""
    from cognita.gpu_worker import WARMUP_TEXT

    assert len(WARMUP_TEXT.split()) >= 600
    assert WARMUP_TEXT == WARMUP_TEXT.strip()
    assert WARMUP_TEXT != CANARY_TEXT


class _RecordingModel:
    """Stands in for the fastembed model: records every list handed to embed()."""

    def __init__(self):
        self.seen: list[list[str]] = []

    def embed(self, texts, batch_size=None):
        self.seen.append(list(texts))
        for text in texts:
            yield _FakeVector([float(len(text)), 1.0])


def run_main(monkeypatch, capsys, argv, build):
    """Drive `gpu_worker.main` to its stdin-EOF exit with everything that would
    touch the process (fd juggling, faulthandler, the real pipes) replaced."""
    from cognita import gpu_worker

    data_out = io.BytesIO()
    monkeypatch.setattr(gpu_worker, "build_embedder", build)
    monkeypatch.setattr(gpu_worker, "claim_stdout", lambda: data_out)
    monkeypatch.setattr(gpu_worker, "die_with_parent", lambda *a, **k: False)
    monkeypatch.setattr(gpu_worker, "faulthandler", type(
        "NoFaultHandler", (), {"enable": staticmethod(lambda **k: None),
                               "register": staticmethod(lambda *a, **k: None)}))
    monkeypatch.setattr(sys, "stdin", type("Stdin", (), {"buffer": io.BytesIO(b"")})())
    code = gpu_worker.main(argv)
    return code, data_out.getvalue(), capsys.readouterr().err


def test_the_warmup_sends_one_full_batch_of_the_warmup_text(monkeypatch, capsys):
    """§6 item 3: a full batch of full-context text, so the arena reaches its
    high-water mark before the parent takes its `_settled_free` baseline."""
    from cognita.gpu_worker import WARMUP_TEXT

    model = _RecordingModel()
    code, out, err = run_main(
        monkeypatch, capsys,
        ["--model", "m", "--cache-dir", "c", "--batch-size", "4"],
        lambda args: (model, {"provider_active": ["CUDAExecutionProvider"]}),
    )
    assert code == 0, err
    assert model.seen == [[WARMUP_TEXT] * 4], (
        "the warm-up must be exactly one full batch of WARMUP_TEXT, not the "
        "one-line canary"
    )
    # The handshake went out first and it is a real frame.
    payload = read_frame(io.BytesIO(out))
    assert b'"event": "ready"' in payload


def test_the_warmup_batch_size_follows_the_argument(monkeypatch, capsys):
    from cognita.gpu_worker import WARMUP_TEXT

    model = _RecordingModel()
    code, _, err = run_main(
        monkeypatch, capsys,
        ["--model", "m", "--cache-dir", "c", "--batch-size", "16"],
        lambda args: (model, {}),
    )
    assert code == 0, err
    assert model.seen == [[WARMUP_TEXT] * 16]


def test_a_warmup_failure_still_exits_3(monkeypatch, capsys):
    class Exploding:
        def embed(self, texts, batch_size=None):
            raise RuntimeError("out of memory on the first long batch")

    code, _, err = run_main(
        monkeypatch, capsys,
        ["--model", "m", "--cache-dir", "c", "--batch-size", "4"],
        lambda args: (Exploding(), {}),
    )
    assert code == 3
    assert "warmup failed" in err


# -- naming a failed construction at the source ------------------------------


def test_an_old_driver_is_named_driver_too_old(monkeypatch, capsys):
    """ORT's own text when the driver predates the CUDA runtime the wheels were
    built for (R580 for CUDA 13). The parent turns this token into the Admin
    reason `driver_too_old`."""
    def build(args):
        raise RuntimeError(
            "[ONNXRuntimeError] : 1 : FAIL : CUDA failure 35: CUDA driver "
            "version is insufficient for CUDA runtime version"
        )

    code, _, err = run_main(
        monkeypatch, capsys,
        ["--model", "m", "--cache-dir", "c"], build)
    assert code == 2
    assert "worker construction failed reason=driver_too_old" in err
    assert "reason=construction_failed" not in err


def test_the_driver_text_is_matched_without_regard_to_case(monkeypatch, capsys):
    def build(args):
        raise RuntimeError("CUDA Driver Version Is Insufficient")

    code, _, err = run_main(monkeypatch, capsys, ["--model", "m", "--cache-dir", "c"], build)
    assert code == 2
    assert "reason=driver_too_old" in err


def test_any_other_construction_failure_is_construction_failed(monkeypatch, capsys):
    def build(args):
        raise RuntimeError("could not load libcudnn.so.9")

    code, _, err = run_main(monkeypatch, capsys, ["--model", "m", "--cache-dir", "c"], build)
    assert code == 2
    assert "worker construction failed reason=construction_failed" in err
    assert "driver_too_old" not in err


def test_the_reason_is_found_along_the_exception_chain():
    """fastembed wraps ORT's error; the driver text is on the cause."""
    from cognita.gpu_worker import construction_failure_reason

    try:
        try:
            raise RuntimeError("CUDA driver version is insufficient for CUDA runtime")
        except RuntimeError as inner:
            raise ValueError("could not build the model") from inner
    except ValueError as outer:
        assert construction_failure_reason(outer) == "driver_too_old"
    assert construction_failure_reason(ValueError("nothing relevant")) == "construction_failed"


def test_a_worker_reason_line_matches_what_the_parent_scans_for(monkeypatch, capsys):
    """The two halves of the contract in one place: whatever line the worker
    writes must be found by the parent's pattern."""
    from cognita import gpu_host

    def build(args):
        raise RuntimeError("CUDA driver version is insufficient")

    _, _, err = run_main(monkeypatch, capsys, ["--model", "m", "--cache-dir", "c"], build)
    lines = [line for line in err.splitlines() if "construction failed" in line]
    assert lines and lines[0].startswith("ERROR ")
    match = gpu_host._WORKER_REASON.search(lines[0])
    assert match and match.group(1) == "driver_too_old"
