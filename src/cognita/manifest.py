"""Disk-side stat + hash for the document manifest (5.0 §4).

`list_documents` returned index metadata and no content hash, so "which of
these 17 files differ from my local copy?" could only be answered by pulling
all 17. This module supplies the per-file facts that turn that into one call,
and it reads them from the FILE ON DISK at request time — never from the index
row. That is the whole point: a hash served out of index state cannot detect
the failure it exists to detect, which is the index and the disk disagreeing.

Two hashes are reported because two different questions get asked of them:

- `content_sha256` is the hash the rest of Cognita already means by that name
  (editing.content_sha256): sha256 of the UTF-8 text with any BOM dropped and
  CRLF/CR folded to LF. It is what `read_document` stamps, what
  `expected_sha256` compares against, and therefore the ONLY one of the two a
  write guard will accept. It is null for a file that is not UTF-8 text.
- `bytes_sha256` is sha256 of the raw file, byte for byte — what `sha256sum`
  prints. It is what a sync client should diff against its own copy, and it is
  defined for every file including PDFs and .docx.

On an LF-only text file with no BOM the two are identical, which is the case
almost every document is in; they diverge exactly where a naive comparison
would otherwise report a byte-exact file as a mismatch.
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from pathlib import Path

from .byte_facts import classify_text_bytes, line_ending_style as _line_ending_style

_HASH_CHUNK = 1 << 20  # 1 MiB — streamed, so a large file costs no extra memory


def bytes_sha256(target: Path) -> str:
    """sha256 of the file's raw bytes (streamed) — matches `sha256sum`."""
    digest = hashlib.sha256()
    with target.open("rb") as handle:
        while chunk := handle.read(_HASH_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def text_sha256(raw: bytes) -> str | None:
    """content_sha256 of `raw` decoded as UTF-8, or None if it is not text.

    BOM handling matches _load_document_text in proxy.py, so a hash from here
    and a hash from read_document are the same string for the same file.
    """
    view = classify_text_bytes(raw)
    return view.content_sha256 if view.accepted and view.utf8_valid else None


def line_ending_style(raw: bytes) -> str:
    """Which line endings a file actually uses: lf / crlf / cr / mixed / none.

    5.0.1: read_document returns NEWLINE-NORMALIZED text, because the edit
    matcher compares against normalized text and an anchor copied out of a
    read_document response has to match. That normalization was undocumented, so
    a client byte-comparing read_document's text against what it pushed saw a
    mismatch on a file the server had stored perfectly — the same "one signal,
    two states" failure the find_literal zero had. Reporting the style, next to a
    raw-bytes hash, is what makes the two states distinguishable in one call.
    """
    return _line_ending_style(raw)


# Files above this are hashed by streaming raw bytes only: decoding a 50 MB
# document into memory purely to answer "did it change?" is not worth it, and
# bytes_sha256 answers that question on its own.
TEXT_HASH_MAX_BYTES = 5_000_000


def file_facts(target: Path, *, hash_text: bool = True) -> dict:
    """Stat + hashes for one file, or {"on_disk": False} if it is not there.

    Never raises: a file that vanished, is locked, or is an unhydrated cloud
    placeholder is reported as such. A manifest that 500s because one of 319
    files was mid-sync would be useless exactly when it is most needed.

    The file is read ONCE when both hashes are wanted and it fits in memory —
    this runs per document on the manifest path, so hashing 338 files twice over
    would double the I/O of the call whose whole purpose is to be cheap enough to
    make a sync diff one request. Oversized files stream for the byte hash alone.

    hash_text=False skips the text hash entirely: the delete paths want the
    mtime and the byte hash for ghost forensics and have no use for a hash whose
    only consumer is expected_sha256.
    """
    try:
        stat = target.stat()
    except OSError as exc:
        return {"on_disk": False, "error": f"{type(exc).__name__}: {exc}"}
    facts: dict = {
        "on_disk": True,
        "size_bytes": stat.st_size,
        "mtime": datetime.fromtimestamp(stat.st_mtime).isoformat(sep=" "),
        # The epoch float alongside the readable stamp: drift is decided by
        # comparing against the index's stored mtime, and re-parsing a local
        # naive string to do that is wrong for one hour every autumn.
        "mtime_epoch": stat.st_mtime,
    }
    want_text = hash_text and stat.st_size <= TEXT_HASH_MAX_BYTES
    try:
        if want_text:
            raw = target.read_bytes()
            view = classify_text_bytes(raw)
            facts.update(view.facts())
            if not view.accepted:
                facts.update({"line_endings": None, "utf8_valid": None,
                              "decode_error_bytes": None, "content_is_lossy": False,
                              "index_text_sanitized": False, "content_sha256": None})
        else:
            facts["bytes_sha256"] = bytes_sha256(target)
            facts["content_sha256"] = None
            facts.update({"line_endings": None, "utf8_valid": None,
                          "decode_error_bytes": None, "content_is_lossy": False,
                          "index_text_sanitized": False})
    except OSError as exc:
        facts["bytes_sha256"] = None
        facts["content_sha256"] = None
        facts["error"] = f"{type(exc).__name__}: {exc}"
    return facts


def stat_drift(
    stat_facts: dict,
    known_size,
    known_mtime,
    *,
    indexed_hash: str | None = None,
    extracted_hash: str | None = None,
) -> bool:
    """Report content drift, using hashes when timestamps are unreliable.

    An extracted hash is authoritative for parsed content. A matching raw
    file hash can clear a timestamp-only change; a mismatch cannot prove
    drift for formats whose extraction differs from their bytes. Without
    conclusive hashes, fall back to size and mtime. This avoids false drift
    when a sync client rewrites timestamps without changing content.
    """
    if not stat_facts.get("on_disk"):
        return True
    if extracted_hash is not None and indexed_hash is not None:
        return extracted_hash != indexed_hash
    if known_size is None or known_mtime is None:
        return False  # nothing stored to compare against; not evidence of drift
    if (
        stat_facts.get("size_bytes") == known_size
        and abs(stat_facts.get("mtime_epoch", 0.0) - known_mtime.timestamp()) < 1e-3
    ):
        return False
    # The stat moved. Only the bytes can say whether anything did.
    if indexed_hash is not None and indexed_hash in (
        stat_facts.get("content_sha256"),
        stat_facts.get("bytes_sha256"),
    ):
        return False
    return True
