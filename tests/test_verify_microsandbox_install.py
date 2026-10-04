from __future__ import annotations

import base64
import csv
import hashlib
from io import StringIO
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from scripts.verify_microsandbox_install import _verify_record


def _record_row(*fields: str) -> str:
    stream = StringIO(newline="")
    csv.writer(stream, lineterminator="\n").writerow(fields)
    return stream.getvalue()


def _record_fixture(rows: str, content: bytes) -> tuple[MagicMock, MagicMock, MagicMock]:
    record = MagicMock(spec=Path)
    record.open.return_value.__enter__.return_value = StringIO(rows)
    location = MagicMock(spec=Path)
    member = MagicMock(spec=Path)
    member.is_file.return_value = True
    member.read_bytes.return_value = content
    location.__truediv__.return_value = member
    return record, location, member


def test_record_parser_verifies_pep376_digest_and_size_fields() -> None:
    content = b"x" * 216
    encoded = base64.urlsafe_b64encode(hashlib.sha256(content).digest()).rstrip(b"=").decode()
    record, location, member = _record_fixture(
        _record_row("microsandbox/runtime,member.bin", f"sha256={encoded}", "216")
        + _record_row("microsandbox-0.7.0.dist-info/RECORD", "", ""),
        content,
    )

    _verify_record(record, location)

    member.read_bytes.return_value = b"tampered"
    record.open.return_value.__enter__.return_value.seek(0)
    with pytest.raises(SystemExit, match="integrity verification failed"):
        _verify_record(record, location)


@pytest.mark.parametrize(
    "hash_field,size_field,error",
    (
        ("sha256", "216", "digest is malformed"),
        ("unsupported=YWJj", "216", "algorithm is unsupported"),
        ("sha256=YWJj", "not-a-size", "size is malformed"),
        ("", "216", "entry is malformed"),
    ),
)
def test_record_parser_fails_closed_on_malformed_hashed_rows(
    hash_field: str, size_field: str, error: str,
) -> None:
    record, location, _member = _record_fixture(
        _record_row("member.bin", hash_field, size_field),
        b"x" * 216,
    )

    with pytest.raises(SystemExit, match=error):
        _verify_record(record, location)
