"""Bounded immutable-text and version-pinned cursor helpers for book reads."""
from __future__ import annotations

import base64
import hashlib
import json


class ReadCursorError(ValueError):
    pass


def text_page(text: str, start: int, maximum: int) -> dict[str, object]:
    if start < 0 or maximum < 0:
        raise ValueError("invalid text page bounds")
    end = min(len(text), start + maximum)
    return {
        "text": text[start:end], "returned_start": start, "returned_end": end,
        "total_codepoints": len(text), "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
    }


def spoken_interval(
    speech_text: str,
    spoken_text: str,
    start: int,
    end: int,
    tag_deletion_spans: list[list[int]] | tuple[tuple[int, int], ...] | None = None,
) -> str:
    """Return the frozen spoken projection for a speech-coordinate interval.

    Spans are the validated tag deletions captured when the snapshot was
    prepared. Older snapshots without spans are safe only when both frozen
    projections are identical; guessing alignment can move repeated text.
    """
    if not (0 <= start <= end <= len(speech_text)):
        raise ValueError("invalid frozen speech interval")
    if tag_deletion_spans is None:
        if speech_text != spoken_text:
            raise ValueError("frozen tag deletion spans are unavailable")
        return speech_text[start:end]

    previous_end = 0
    removed_total = 0
    for span in tag_deletion_spans:
        if (len(span) != 2 or any(isinstance(point, bool) or not isinstance(point, int) for point in span)):
            raise ValueError("frozen tag deletion span is malformed")
        left, right = span
        if left < previous_end or left < 0 or right <= left or right > len(speech_text):
            raise ValueError("frozen tag deletion spans are inconsistent")
        removed_total += right - left
        previous_end = right

    if len(speech_text) - removed_total != len(spoken_text):
        raise ValueError("frozen spoken projection does not match tag deletion spans")
    # Deletion spans are half-open. Boundaries inside a tag collapse to the
    # tag's spoken boundary, including intervals crossing its edge.
    removed_before_start = sum(max(0, min(right, start) - left)
                               for left, right in tag_deletion_spans if left < start)
    removed_before_end = sum(max(0, min(right, end) - left)
                             for left, right in tag_deletion_spans if left < end)
    mapped_start = start - removed_before_start
    mapped_end = end - removed_before_end
    return spoken_text[mapped_start:mapped_end]


def paired_text_page(prompt: str, spoken: str, offset: int, maximum: int) -> tuple[dict[str, object], dict[str, object], int]:
    """Page two logical fields through one shared code-point budget."""
    total = len(prompt) + len(spoken)
    if offset < 0 or offset > total or maximum < 1:
        raise ValueError("invalid paired text page bounds")
    prompt_start = min(offset, len(prompt))
    prompt_count = min(maximum, len(prompt) - prompt_start)
    spoken_start = max(0, offset - len(prompt))
    spoken_count = min(maximum - prompt_count, len(spoken) - spoken_start)
    next_offset = offset + prompt_count + spoken_count
    return (
        text_page(prompt, prompt_start, prompt_count),
        text_page(spoken, spoken_start, spoken_count),
        next_offset,
    )


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


__all__ = ["ReadCursorError", "paired_text_page", "parse_read_cursor", "read_cursor", "spoken_interval", "text_page"]
