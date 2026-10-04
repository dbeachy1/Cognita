"""Boundary fixtures for the 9.2 exact-byte/text contract."""

import base64
import hashlib

import pytest

from cognita.byte_facts import (
    byte_facts,
    check_expected_bytes_sha256,
    check_expected_bytes_sha256_digest,
    classify_text_bytes,
    decode_base64,
)


def test_empty_and_newline_facts_are_exact():
    assert classify_text_bytes(b"").facts()["utf8_valid"] is True
    view = classify_text_bytes(b"a\r\nb\r\nc\n")
    assert view.line_endings == "mixed"
    assert view.indexed_text == "a\nb\nc\n"


def test_one_malformed_byte_is_readable_but_lossy():
    view = classify_text_bytes(b"prefix\xffsuffix")
    assert view.accepted
    assert view.text == "prefix\ufffdsuffix"
    assert view.content_is_lossy
    assert view.decode_error_bytes == 1
    assert view.content_sha256 is None


def test_literal_replacement_is_not_a_decode_error():
    view = classify_text_bytes("literal � replacement".encode())
    assert view.utf8_valid and view.decode_error_bytes == 0
    assert not view.content_is_lossy


def test_control_threshold_counts_unique_source_bytes():
    assert classify_text_bytes(b"x\0").accepted
    rejected = classify_text_bytes(b"a" * 80 + b"\0" * 9)
    assert rejected.reason == "binary_content"
    # One invalid span and a control byte are counted as source bytes, not two
    # independent diagnostics for the same byte.
    assert classify_text_bytes(b"a\xff\0").accepted


@pytest.mark.parametrize("signature", [
    b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff", b"GIF89a", b"%PDF-1.7",
    b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08", b"\x1f\x8b", b"\x7fELF",
])
def test_binary_signatures_are_rejected(signature):
    assert classify_text_bytes(signature + b"payload").reason == "binary_content"


def test_utf16_and_utf32_are_not_guessed():
    assert classify_text_bytes(b"\xff\xfea\x00").reason == "unsupported_text_encoding"
    assert classify_text_bytes(b"\xff\xfe\x00\x00a\x00\x00\x00").reason == "unsupported_text_encoding"


@pytest.mark.parametrize("value", ["YWJj\n", "YWJj", "YW-J", "YQ==="])
def test_base64_input_is_strict(value):
    # YWJj is valid; the parametrization keeps the exact accepted boundary
    # visible while all malformed variants are rejected.
    if value == "YWJj":
        assert decode_base64(value) == b"abc"
    else:
        with pytest.raises(ValueError):
            decode_base64(value)


def test_base64_size_is_rejected_before_decoder_allocation(monkeypatch):
    called = False

    def should_not_decode(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("oversized input reached the decoder")

    monkeypatch.setattr(base64, "b64decode", should_not_decode)
    with pytest.raises(ValueError, match="exceeds"):
        decode_base64("YWE=", max_bytes=1)
    assert called is False


def test_byte_guard_is_exact_and_case_insensitive():
    raw = b"bytes\r\n"
    digest = hashlib.sha256(raw).hexdigest().upper()
    assert check_expected_bytes_sha256(raw, digest) is None
    mismatch = check_expected_bytes_sha256(raw, "0" * 64)
    assert mismatch["reason"] == "stale_file"
    assert mismatch["guard"] == "expected_bytes_sha256"
    assert check_expected_bytes_sha256_digest(digest.lower(), digest) is None


def test_rejected_binary_receipts_do_not_claim_text_facts():
    facts = byte_facts(b"%PDF-1.7")
    assert facts["bytes_sha256"] == hashlib.sha256(b"%PDF-1.7").hexdigest()
    assert facts["line_endings"] is None
    assert facts["utf8_valid"] is None
