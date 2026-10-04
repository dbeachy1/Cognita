"""Writes are byte-verbatim (5.0).

The bug this pins had two halves, and the second one only ever fired on Windows,
which is why prod never showed it:

1. `content.strip()` — leading whitespace and ALL trailing newlines were
   silently discarded. A trailing newline could not be stored at all, and a
   client that pushed a file and hashed it back saw a mismatch on a file it had
   sent correctly.
2. `Path.write_text` opens in text mode with newline=None, and in WRITE mode
   that translates every "\\n" to os.linesep. On Linux that is a no-op. On
   Windows "a\\nb" was stored as "a\\r\\nb", and an input that ALREADY held CRLF
   became "a\\r\\r\\nb" — corruption, not normalization.

The five payloads below are the ones Doug specified, and they are chosen so each
isolates one failure: plain LF, one trailing newline, two trailing newlines,
leading whitespace, and pre-existing CRLF. This suite runs on every platform on
purpose — half the bug is invisible on the one prod runs on.
"""

import pytest

from cognita.engine_local import LocalEngineHost

PAYLOADS = [
    b"a\nb",
    b"a\nb\n",
    b"a\nb\n\n",
    b" a\nb",
    b"a\r\nb\r\n",
]


@pytest.mark.parametrize("payload", PAYLOADS, ids=lambda p: repr(p))
def test_write_verbatim_is_byte_exact(tmp_path, payload):
    target = tmp_path / "doc.md"
    LocalEngineHost._write_verbatim(target, payload.decode("utf-8"))
    assert target.read_bytes() == payload


def test_write_verbatim_never_touches_line_endings(tmp_path):
    """Explicit anti-regression for the os.linesep translation.

    Asserted as an absence — no "\\r" may appear from an LF-only input, and no
    "\\r\\r" from a CRLF one — because that is the shape the corruption took,
    and an equality assertion alone would not say WHICH half broke.
    """
    lf = tmp_path / "lf.md"
    LocalEngineHost._write_verbatim(lf, "a\nb\n")
    assert b"\r" not in lf.read_bytes()

    crlf = tmp_path / "crlf.md"
    LocalEngineHost._write_verbatim(crlf, "a\r\nb\r\n")
    assert b"\r\r" not in crlf.read_bytes()
    assert crlf.read_bytes() == b"a\r\nb\r\n"


def test_write_verbatim_overwrites_completely(tmp_path):
    """A shorter second write must not leave the tail of the first behind."""
    target = tmp_path / "doc.md"
    LocalEngineHost._write_verbatim(target, "a much longer first document\n")
    LocalEngineHost._write_verbatim(target, "b\n")
    assert target.read_bytes() == b"b\n"


def test_write_verbatim_handles_non_ascii(tmp_path):
    target = tmp_path / "doc.md"
    LocalEngineHost._write_verbatim(target, "héllo — ünicode\n")
    assert target.read_bytes() == "héllo — ünicode\n".encode("utf-8")


# ---------------------------------------------- 5.1: atomicity and rollback


def test_write_verbatim_leaves_no_staging_file(tmp_path):
    """The write stages beside the target and os.replace()s it, so a reader
    never sees a prefix of the new content and a kill mid-write cannot leave a
    truncated document while the index still describes the old one. The staging
    file must not survive — and its name must not be indexable, or the walk
    would pick it up."""
    target = tmp_path / "doc.md"
    LocalEngineHost._write_verbatim(target, "hello\n")
    leftovers = [p.name for p in tmp_path.iterdir() if p.name != "doc.md"]
    assert leftovers == []
    assert target.read_bytes() == b"hello\n"


def test_write_verbatim_cleans_up_staging_on_failure(tmp_path, monkeypatch):
    """A failed write must not leave litter behind either."""
    target = tmp_path / "doc.md"

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr("os.replace", boom)
    with pytest.raises(OSError):
        LocalEngineHost._write_verbatim(target, "hello\n")
    assert list(tmp_path.iterdir()) == []


def test_undo_write_restores_previous_bytes(tmp_path):
    """The rollback used when indexing fails after the bytes have landed."""
    target = tmp_path / "doc.md"
    LocalEngineHost._write_verbatim(target, "original\r\n")
    previous = target.read_bytes()
    LocalEngineHost._write_verbatim(target, "replacement\n")

    LocalEngineHost._undo_write(target, previous)
    assert target.read_bytes() == b"original\r\n"  # byte-exact, CRLF intact


def test_undo_write_removes_a_file_that_did_not_exist_before(tmp_path):
    """add_document's case: the orphan file 4.6.0 exists to prevent, reached
    through content rather than through the extension."""
    target = tmp_path / "new.md"
    LocalEngineHost._write_verbatim(target, "---\ntitle: x\n---\n")
    LocalEngineHost._undo_write(target, None)
    assert not target.exists()


# --------------------------------------------------------------------------
# 6.0.12 — the rollback must cover CANCELLATION, and must not itself corrupt
# --------------------------------------------------------------------------


def test_undo_write_leaves_no_temp_file_behind(tmp_path):
    """The restore stages beside the target like the write does, so the staging
    file must not survive a successful rollback."""
    target = tmp_path / "doc.md"
    LocalEngineHost._write_verbatim(target, "original\n")
    previous = target.read_bytes()
    LocalEngineHost._write_verbatim(target, "replacement\n")
    LocalEngineHost._undo_write(target, previous)
    assert [p.name for p in tmp_path.iterdir()] == ["doc.md"]


def test_a_failed_rollback_does_not_destroy_the_document(tmp_path, monkeypatch):
    """🔴 A RECOVERY PATH THAT CAN ITSELF LEAVE A HALF-WRITTEN FILE IS NOT ONE.

    `_undo_write` used to call `write_bytes`, which TRUNCATES IN PLACE. Interrupt
    the rollback and the document is destroyed by the very code protecting it —
    and by then the original bytes exist nowhere else to retry from. Staging and
    `os.replace` means an interrupted restore leaves the file exactly as it was.
    """
    target = tmp_path / "doc.md"
    LocalEngineHost._write_verbatim(target, "original\n")
    previous = target.read_bytes()
    LocalEngineHost._write_verbatim(target, "replacement\n")

    def boom(*a, **k):
        raise OSError("no space left on device")

    monkeypatch.setattr("os.replace", boom)
    LocalEngineHost._undo_write(target, previous)   # logged, never raised
    # The rollback failed, but the document is still a whole, valid file — the
    # NEW content rather than the old, which is recoverable. A truncated file
    # would not be.
    assert target.read_bytes() == b"replacement\n"
    assert [p.name for p in tmp_path.iterdir()] == ["doc.md"]


async def test_a_canceled_write_rolls_the_document_back(tmp_path, monkeypatch):
    """🔴 THE 6.0.12 BUG, and the one that actually happened.

    `asyncio.CancelledError` derives from `BaseException`, so the `except
    Exception` guarding the index step caught every failure EXCEPT the one
    production produces: uvicorn canceling an in-flight request at shutdown.
    The bytes land before the ~6s embed, so the caller was told its write had
    failed while the new content sat on disk — "the call errored, therefore
    nothing changed" was FALSE, which is how a truncated multi-document push
    left every file individually valid and the set inconsistent.
    """
    import asyncio

    from cognita.engine_local import LocalEngineHost as Host

    target = tmp_path / "doc.md"
    Host._write_verbatim(target, "the good version\n")
    previous = target.read_bytes()

    async def index_that_gets_canceled(*a, **k):
        raise asyncio.CancelledError()

    # The shape of the real handler: write, then index, then undo on ANY
    # fatal path, then re-raise so the caller still sees a failure.
    async def write_then_index():
        await asyncio.to_thread(Host._write_verbatim, target, "half-pushed\n")
        try:
            await index_that_gets_canceled()
        except BaseException:
            Host._undo_write(target, previous)
            raise

    with pytest.raises(asyncio.CancelledError):
        await write_then_index()

    assert target.read_bytes() == b"the good version\n", (
        "a canceled write left its content on disk while telling the caller "
        "it had failed — the 6.0.12 regression"
    )
