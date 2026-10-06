"""Bounded immutable-text and version-pinned cursor helpers for book reads."""
from __future__ import annotations

import base64
import hashlib
import json


class ReadCursorError(ValueError):
    pass


def text_page(text: str, start: int, maximum: int) -> dict[str, object]:
    if start < 0 or maximum < 1:
        raise ValueError("invalid text page bounds")
    end = min(len(text), start + maximum)
    return {
        "text": text[start:end], "returned_start": start, "returned_end": end,
        "total_codepoints": len(text), "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
    }


def read_cursor(view: str, record_offset: int, text_offset: int = 0) -> str:
    raw = json.dumps(
        {"v": 2, "view": view, "record_offset": record_offset, "text_offset": text_offset},
        separators=(",", ":"),
    ).encode("utf-8")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def parse_read_cursor(cursor: str, view: str) -> tuple[int, int]:
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        value = json.loads(raw)
        record_offset, text_offset = value["record_offset"], value["text_offset"]
        if (value != {"v": 2, "view": view, "record_offset": record_offset, "text_offset": text_offset}
                or any(isinstance(item, bool) or not isinstance(item, int) or item < 0
                       for item in (record_offset, text_offset))):
            raise ValueError
        return record_offset, text_offset
    except (ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
        raise ReadCursorError("cursor does not belong to this immutable read") from exc


__all__ = ["ReadCursorError", "parse_read_cursor", "read_cursor", "text_page"]
