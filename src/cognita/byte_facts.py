"""Pure byte and text interpretation shared by reads, writes, and indexing.

The connector stores source bytes as its authority.  This module deliberately
does not perform file I/O or know about the engine: callers can therefore use
the same classification and readable/indexed views for a request read, a
write, a watcher update, and a reindex.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import re
from dataclasses import dataclass

from .editing import content_sha256

MAX_BASE64_DOCUMENT_BYTES = 8 * 1024 * 1024
MAX_BASE64_ATOMIC_SET_BYTES = 32 * 1024 * 1024
MAX_BINARY_SUSPECT_BYTES = 8
BINARY_SUSPECT_RATIO = 0.10

# These signatures are intentionally complete enough to avoid rejecting a
# text file on a short, coincidental prefix.  ZIP has three valid container
# headers because a stream may be empty or split/spanned.
_BINARY_SIGNATURES: tuple[bytes, ...] = (
    b"\x89PNG\r\n\x1a\n",
    b"\xff\xd8\xff",                 # JPEG SOI + marker
    b"GIF87a",
    b"GIF89a",
    b"%PDF-",
    b"PK\x03\x04",
    b"PK\x05\x06",
    b"PK\x07\x08",
    b"\x1f\x8b",                    # gzip
    b"\x7fELF",
)
_HEX64 = re.compile(r"^[0-9a-fA-F]{64}$")
_BASE64 = re.compile(r"^[A-Za-z0-9+/]*={0,2}$")
_CONTROL_BYTES = frozenset(range(0x00, 0x09)) | frozenset(range(0x0B, 0x0C))
_CONTROL_BYTES |= frozenset(range(0x0E, 0x20)) | frozenset((0x7F,))


@dataclass(frozen=True, slots=True)
class TextView:
    """The original bytes and their two intentionally different text views."""

    raw: bytes
    text: str
    indexed_text: str
    utf8_valid: bool
    decode_error_bytes: int
    content_is_lossy: bool
    index_text_sanitized: bool
    line_endings: str
    accepted: bool = True
    reason: str | None = None
    message: str | None = None

    @property
    def content_sha256(self) -> str | None:
        """The normalized text stamp, never a hash of replacement text."""
        if not self.utf8_valid:
            return None
        return content_sha256(self.text.lstrip("\ufeff"))

    def facts(self) -> dict:
        return {
            "bytes_sha256": hashlib.sha256(self.raw).hexdigest(),
            "size_bytes": len(self.raw),
            "line_endings": self.line_endings,
            "utf8_valid": self.utf8_valid,
            "decode_error_bytes": self.decode_error_bytes,
            "content_is_lossy": self.content_is_lossy,
            "index_text_sanitized": self.index_text_sanitized,
            "content_sha256": self.content_sha256,
        }


def line_ending_style(raw: bytes) -> str:
    """Return the exact line-ending style in *raw*."""
    crlf = raw.count(b"\r\n")
    cr = raw.count(b"\r") - crlf
    lf = raw.count(b"\n") - crlf
    styles = [name for name, count in (("crlf", crlf), ("cr", cr), ("lf", lf)) if count]
    return styles[0] if len(styles) == 1 else ("mixed" if styles else "none")


def _valid_pe_signature(raw: bytes) -> bool:
    """Recognize PE only when MZ points to an in-bounds PE header."""
    if not raw.startswith(b"MZ") or len(raw) < 0x40:
        return False
    pe_offset = int.from_bytes(raw[0x3C:0x40], "little")
    return pe_offset <= len(raw) - 4 and raw[pe_offset:pe_offset + 4] == b"PE\0\0"


def binary_signature(raw: bytes) -> str | None:
    """Return a stable binary reason, or ``None`` when no signature matches."""
    if _valid_pe_signature(raw):
        return "windows_pe"
    for sig in _BINARY_SIGNATURES:
        if raw.startswith(sig):
            return {
                b"\x89PNG\r\n\x1a\n": "png",
                b"\xff\xd8\xff": "jpeg",
                b"GIF87a": "gif",
                b"GIF89a": "gif",
                b"%PDF-": "pdf",
                b"PK\x03\x04": "zip",
                b"PK\x05\x06": "zip",
                b"PK\x07\x08": "zip",
                b"\x1f\x8b": "gzip",
                b"\x7fELF": "elf",
            }[sig]
    return None


def _decode_utf8_with_spans(raw: bytes) -> tuple[str, set[int]]:
    """Decode with U+FFFD and return the original byte indexes replaced."""
    parts: list[str] = []
    bad: set[int] = set()
    offset = 0
    while offset < len(raw):
        try:
            parts.append(raw[offset:].decode("utf-8"))
            break
        except UnicodeDecodeError as exc:
            start, end = offset + exc.start, offset + exc.end
            parts.append(raw[offset:start].decode("utf-8"))
            parts.append("\ufffd")
            bad.update(range(start, end))
            offset = end
    return "".join(parts), bad


def _indexed_view(text: str) -> tuple[str, bool]:
    """Strip BOM/newlines and replace PostgreSQL-unsafe controls for indexing."""
    normalized = text[1:] if text.startswith("\ufeff") else text
    normalized = normalized.replace("\r\n", "\n").replace("\r", "\n")
    sanitized = any(ord(ch) in _CONTROL_BYTES for ch in normalized)
    if sanitized:
        normalized = "".join("\ufffd" if ord(ch) in _CONTROL_BYTES else ch for ch in normalized)
    return normalized, sanitized


def classify_text_bytes(raw: bytes) -> TextView:
    """Classify bounded bytes according to the 9.2 conservative text rule."""
    raw = bytes(raw)
    style = line_ending_style(raw)
    if raw.startswith((b"\xff\xfe", b"\xfe\xff", b"\xff\xfe\x00\x00", b"\x00\x00\xfe\xff")):
        return TextView(raw, "", "", False, 0, False, False, style, False,
                        "unsupported_text_encoding",
                        "UTF-16 and UTF-32 are not accepted; convert the file to UTF-8.")
    sig = binary_signature(raw)
    if sig:
        return TextView(raw, "", "", False, 0, False, False, style, False,
                        "binary_content", f"recognized {sig} binary signature")
    text, bad = _decode_utf8_with_spans(raw)
    controls = {i for i, value in enumerate(raw) if value in _CONTROL_BYTES}
    suspect = bad | controls
    if len(suspect) >= MAX_BINARY_SUSPECT_BYTES and len(suspect) > len(raw) * BINARY_SUSPECT_RATIO:
        return TextView(raw, text, "", False, len(bad), bool(bad), False, style, False,
                        "binary_content", "binary controls or malformed UTF-8 exceed the text threshold")
    indexed, sanitized = _indexed_view(text)
    return TextView(raw, text, indexed, not bad, len(bad), bool(bad), sanitized, style)


def decode_base64(value: str, *, max_bytes: int = MAX_BASE64_DOCUMENT_BYTES) -> bytes:
    """Strict standard RFC 4648 base64; no whitespace or URL-safe guessing."""
    if not isinstance(value, str) or len(value) % 4:
        raise ValueError("content must be standard base64 with required padding")
    if not _BASE64.fullmatch(value) or ("=" in value[:-2]):
        raise ValueError("content must be standard base64 with required padding")
    # Bound the decoded allocation before asking binascii to create it.  The
    # canonical padding tells us the exact decoded size, so an oversized input
    # never needs to be materialized merely to discover that it is oversized.
    decoded_size = (len(value) // 4) * 3 - len(value) + len(value.rstrip("="))
    if decoded_size > max_bytes:
        raise ValueError(f"decoded content exceeds the {max_bytes}-byte limit")
    try:
        decoded = base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ValueError("content must be standard base64 with required padding") from exc
    if base64.b64encode(decoded).decode("ascii") != value:
        raise ValueError("content must be standard base64 with required padding")
    if len(decoded) > max_bytes:
        raise ValueError(f"decoded content exceeds the {max_bytes}-byte limit")
    return decoded


def byte_facts(raw: bytes, *, text: TextView | None = None) -> dict:
    """Receipt facts for committed bytes, suitable for all write families."""
    view = text or classify_text_bytes(raw)
    facts = view.facts()
    if not view.accepted:
        facts["line_endings"] = None
        facts["utf8_valid"] = None
        facts["decode_error_bytes"] = None
        facts["content_is_lossy"] = False
        facts["index_text_sanitized"] = False
        facts["content_sha256"] = None
    return facts


def validate_expected_bytes_sha256(value: object) -> str | None:
    """Normalize an exact-byte guard, returning ``None`` when omitted."""
    if value in (None, ""):
        return None
    normalized = str(value)
    if not _HEX64.fullmatch(normalized):
        raise ValueError("expected_bytes_sha256 must be exactly 64 hexadecimal characters")
    return normalized.lower()


def check_expected_bytes_sha256(raw: bytes | None, expected: object) -> dict | None:
    """Return an error payload on an exact-byte guard mismatch."""
    actual = hashlib.sha256(raw).hexdigest() if raw is not None else None
    return check_expected_bytes_sha256_digest(actual, expected)


def check_expected_bytes_sha256_digest(actual: str | None, expected: object) -> dict | None:
    """Return an exact-byte guard result from a precomputed streaming digest."""
    try:
        expected_normalized = validate_expected_bytes_sha256(expected)
    except ValueError as exc:
        return {"status": "error", "reason": "invalid", "message": str(exc),
                "guard": "expected_bytes_sha256"}
    if expected_normalized is None:
        return None
    if actual != expected_normalized:
        return {"status": "error", "reason": "stale_file",
                "guard": "expected_bytes_sha256", "expected_bytes_sha256": expected_normalized,
                "actual_bytes_sha256": actual,
                "message": "The file bytes changed since you read them; nothing was written."}
    return None
