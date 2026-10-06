"""Bounded immutable-text and version-pinned cursor helpers for book reads."""
from __future__ import annotations

import base64
import difflib
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


def spoken_interval(speech_text: str, spoken_text: str, start: int, end: int) -> str:
    """Return a speech interval with only the frozen added-tag deletions removed.

    Prepared snapshots retain both projections.  Their relationship is a
    deletion-only transform, so equal blocks map exact code-point boundaries
    and deleted tag spans collapse at one spoken boundary.  A different
    transform is malformed historical evidence rather than a cue to read live
    Word bytes.
    """
    if not (0 <= start <= end <= len(speech_text)):
        raise ValueError("invalid frozen speech interval")
    boundaries: list[int | None] = [None] * (len(speech_text) + 1)
    matcher = difflib.SequenceMatcher(a=speech_text, b=spoken_text, autojunk=False)
    for kind, left_start, left_end, right_start, right_end in matcher.get_opcodes():
        if kind == "equal":
            for offset in range(left_end - left_start + 1):
                boundaries[left_start + offset] = right_start + offset
        elif kind == "delete":
            for offset in range(left_start, left_end + 1):
                boundaries[offset] = right_start
        else:
            raise ValueError("frozen spoken projection is not a tag deletion transform")
    if boundaries[start] is None or boundaries[end] is None:
        raise ValueError("frozen projection boundaries are unavailable")
    return spoken_text[int(boundaries[start]):int(boundaries[end])]


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


__all__ = ["ReadCursorError", "parse_read_cursor", "read_cursor", "spoken_interval", "text_page"]
