"""Deterministic DOCX-to-speech projections and explicit range validation."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Mapping, Sequence

try:
    import regex as _regex
except ImportError as exc:  # regex is a required pinned service dependency.
    raise RuntimeError("cognita.books requires the maintained 'regex' package") from exc

from .docx import (
    PROJECTION_VERSION,
    DocxProjection,
    UnsupportedLocation,
    parse_docx,
)


class ProjectionError(ValueError):
    def __init__(self, code: str, message: str, paragraph_id: str | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.paragraph_id = paragraph_id


_MAX_SAFE_INTEGER = (1 << 53) - 1


def _safe_int(value: object, *, positive: bool = False) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ProjectionError("invalid_integer", "Offsets and limits must be safe JSON integers")
    if value < (1 if positive else 0) or value > _MAX_SAFE_INTEGER:
        raise ProjectionError("invalid_integer", "Offsets and limits must be safe nonnegative JSON integers")
    return value


@dataclass(frozen=True)
class ExplicitTagSpan:
    paragraph_id: str
    start: int
    end: int
    expected_text_sha256: str


@dataclass(frozen=True)
class ExcludedParagraph:
    paragraph_id: str
    reason: str


@dataclass(frozen=True)
class ParagraphProjection:
    paragraph_id: str
    source_ordinal: int
    text: str
    style: str
    speech_start: int | None
    speech_end: int | None
    tags: tuple[tuple[int, int], ...]
    bookmarks: tuple[str, ...]


@dataclass(frozen=True)
class SourceSegment:
    paragraph_id: str
    start: int
    end: int
    bookmark: str | None


@dataclass(frozen=True)
class SeparatorMapping:
    start: int
    end: int
    before_paragraph_id: str
    after_paragraph_id: str


@dataclass(frozen=True)
class MappedSegment:
    speech_start: int
    speech_end: int
    paragraph_id: str
    paragraph_start: int
    paragraph_end: int
    bookmark: str | None


@dataclass(frozen=True)
class ProjectedDocument:
    document_view_id: str
    prose_sha256: str
    tagged_sha256: str
    projection_version: str
    prose_projection: str
    prose_projection_sha256: str
    spoken_projection: str
    spoken_projection_sha256: str
    speech_text: str
    speech_text_sha256: str
    paragraph_ids: tuple[str, ...]
    paragraphs: tuple[ParagraphProjection, ...]
    excluded_paragraphs: tuple[ExcludedParagraph, ...]
    segments: tuple[MappedSegment, ...]
    separators: tuple[SeparatorMapping, ...]
    source_text_matches_without_tags: bool
    headers_footers: tuple[str, ...]
    unsupported: tuple[UnsupportedLocation, ...]


@dataclass(frozen=True)
class ChunkRange:
    chunk_id: str
    start: int
    end: int


@dataclass(frozen=True)
class ValidatedChunkRange:
    chunk_id: str
    start: int
    end: int
    codepoint_count: int
    limit_count: int
    prompt_sha256: str
    source_segments: tuple[SourceSegment, ...]
    separator_mappings: tuple[SeparatorMapping, ...]


@dataclass(frozen=True)
class Coverage:
    speech_codepoints: int
    covered_codepoints: int
    gaps: int
    overlaps: int


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _without_spans(text: str, spans: Sequence[tuple[int, int]]) -> str:
    previous = 0
    chunks: list[str] = []
    for start, end in spans:
        if start < previous or end <= start or end > len(text):
            raise ProjectionError("invalid_tag_span", "Tag spans must be nonempty, ordered, and in range")
        chunks.append(text[previous:start])
        previous = end
    chunks.append(text[previous:])
    return "".join(chunks)


def _coerce_span(item: ExplicitTagSpan | Mapping[str, object]) -> ExplicitTagSpan:
    if isinstance(item, ExplicitTagSpan):
        span = item
        paragraph_id, expected_hash = span.paragraph_id, span.expected_text_sha256
        start, end = _safe_int(span.start), _safe_int(span.end, positive=True)
    elif all(hasattr(item, field) for field in ("paragraph_id", "start", "end", "expected_text_sha256")):
        paragraph_id = item.paragraph_id
        expected_hash = item.expected_text_sha256
        start, end = _safe_int(item.start), _safe_int(item.end, positive=True)
    else:
        try:
            paragraph_id = item["paragraph_id"]
            expected_hash = item["expected_text_sha256"]
            start, end = _safe_int(item["start"]), _safe_int(item["end"], positive=True)
        except (KeyError, TypeError, ValueError) as exc:
            raise ProjectionError("invalid_tag_span", "Malformed explicit tag span") from exc
    if not isinstance(paragraph_id, str) or not isinstance(expected_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_hash):
        raise ProjectionError("invalid_tag_span", "Tag span ID and SHA-256 must be valid strings")
    if end <= start:
        raise ProjectionError("invalid_tag_span", "Tag span must be nonempty")
    return ExplicitTagSpan(paragraph_id, start, end, expected_hash)


def _coerce_exclusion(item: ExcludedParagraph | Mapping[str, object]) -> ExcludedParagraph:
    if isinstance(item, ExcludedParagraph):
        paragraph_id, reason = item.paragraph_id, item.reason
    elif hasattr(item, "paragraph_id") and hasattr(item, "reason"):
        paragraph_id, reason = item.paragraph_id, item.reason
    else:
        try:
            paragraph_id = item["paragraph_id"]
            reason = item["reason"]
        except (KeyError, TypeError, ValueError) as exc:
            raise ProjectionError("invalid_exclusion", "Malformed paragraph exclusion") from exc
    if not isinstance(paragraph_id, str) or not isinstance(reason, str):
        raise ProjectionError("invalid_exclusion", "Paragraph ID and exclusion reason must be strings")
    return ExcludedParagraph(paragraph_id, reason)


def _docx_tags(
    tagged: DocxProjection,
    paragraph_ids: Sequence[str],
    explicit: Sequence[ExplicitTagSpan | Mapping[str, object]],
) -> dict[str, tuple[tuple[int, int], ...]]:
    by_id = {pid: p for pid, p in zip(paragraph_ids, tagged.paragraphs, strict=True)}
    tags: dict[str, list[tuple[int, int]]] = {pid: [] for pid in paragraph_ids}
    for pid, paragraph in by_id.items():
        for start, end, style in paragraph.styled_runs:
            if style == "CognitaAudioTag":
                tags[pid].append((start, end))
    for supplied in explicit:
        span = _coerce_span(supplied)
        paragraph = by_id.get(span.paragraph_id)
        if paragraph is None:
            raise ProjectionError("invalid_tag_span", "Tag span references a paragraph outside this view", span.paragraph_id)
        if span.start < 0 or span.end <= span.start or span.end > len(paragraph.text):
            raise ProjectionError("invalid_tag_span", "Tag span is outside paragraph text", span.paragraph_id)
        exact = paragraph.text[span.start:span.end]
        if sha256_text(exact) != span.expected_text_sha256:
            raise ProjectionError("tag_text_mismatch", "Tag text does not match its expected hash", span.paragraph_id)
        tags[span.paragraph_id].append((span.start, span.end))
    result: dict[str, tuple[tuple[int, int], ...]] = {}
    for pid, spans in tags.items():
        ordered = sorted(spans)
        merged: list[tuple[int, int]] = []
        for start, end in ordered:
            if merged and start < merged[-1][1]:
                if (start, end) == merged[-1]:
                    continue
                raise ProjectionError("overlapping_tag_spans", "Audio tag spans overlap", pid)
            if merged and start == merged[-1][1]:
                merged[-1] = (merged[-1][0], end)
            else:
                merged.append((start, end))
        result[pid] = tuple(merged)
    return result


def project_docx_pair(
    prose_bytes: bytes,
    tagged_bytes: bytes,
    *,
    speech_paragraph_ids: Sequence[str] | None = None,
    excluded_paragraphs: Sequence[ExcludedParagraph | Mapping[str, object]] = (),
    explicit_tag_spans: Sequence[ExplicitTagSpan | Mapping[str, object]] = (),
) -> ProjectedDocument:
    """Build one view from a pinned prose/tagged byte pair, preserving exact text."""
    prose = parse_docx(prose_bytes)
    tagged = parse_docx(tagged_bytes)
    if len(prose.paragraphs) != len(tagged.paragraphs):
        raise ProjectionError("paragraph_mismatch", "Prose and tagged DOCX paragraph counts differ")
    # IDs bind ordinal, both raw identities, and the projection policy.
    raw_pair = f"{prose.raw_sha256}\0{tagged.raw_sha256}\0{PROJECTION_VERSION}"
    pair_binding = hashlib.sha256(raw_pair.encode("ascii")).hexdigest()
    paragraph_ids = tuple(
        f"p{index:04d}-{hashlib.sha256(f'{pair_binding}\0{index}\0{pp.text}'.encode('utf-8')).hexdigest()[:16]}"
        for index, pp in enumerate(prose.paragraphs)
    )
    tags = _docx_tags(tagged, paragraph_ids, explicit_tag_spans)
    source_matches = True
    for index, (source, marked) in enumerate(zip(prose.paragraphs, tagged.paragraphs, strict=True)):
        pid = paragraph_ids[index]
        if _without_spans(marked.text, tags[pid]) != source.text:
            source_matches = False
    exclusions = tuple(_coerce_exclusion(item) for item in excluded_paragraphs)
    exclusion_by_id: dict[str, ExcludedParagraph] = {}
    for exclusion in exclusions:
        if exclusion.paragraph_id not in paragraph_ids or not exclusion.reason:
            raise ProjectionError("invalid_exclusion", "Each excluded paragraph needs an in-view ID and reason", exclusion.paragraph_id)
        if exclusion.paragraph_id in exclusion_by_id:
            raise ProjectionError("duplicate_exclusion", "Paragraph has more than one exclusion", exclusion.paragraph_id)
        exclusion_by_id[exclusion.paragraph_id] = exclusion
    selected = tuple(speech_paragraph_ids) if speech_paragraph_ids is not None else tuple(
        pid for pid in paragraph_ids if pid not in exclusion_by_id
    )
    if any(not isinstance(pid, str) for pid in selected):
        raise ProjectionError("invalid_paragraph_id", "Speech paragraph IDs must be strings")
    if len(set(selected)) != len(selected):
        raise ProjectionError("duplicate_paragraph_id", "Speech selection contains a duplicate paragraph ID")
    ordinal_by_id = {pid: index for index, pid in enumerate(paragraph_ids)}
    if any(pid not in ordinal_by_id for pid in selected):
        raise ProjectionError("invalid_paragraph_id", "Speech selection references a paragraph outside this view")
    if list(selected) != sorted(selected, key=ordinal_by_id.__getitem__):
        raise ProjectionError("invalid_paragraph_order", "Speech paragraphs must retain original document order")
    omitted = set(paragraph_ids) - set(selected)
    if omitted != set(exclusion_by_id):
        raise ProjectionError("missing_exclusion_reason", "Every omitted candidate paragraph needs exactly one exclusion reason")

    prose_text = "\n\n".join(p.text for p in prose.paragraphs)
    speech_parts: list[str] = []
    spoken_parts: list[str] = []
    selected_offsets: dict[str, tuple[int, int]] = {}
    segments: list[MappedSegment] = []
    separators: list[SeparatorMapping] = []
    global_pos = 0
    for selected_index, pid in enumerate(selected):
        ordinal = ordinal_by_id[pid]
        marked = tagged.paragraphs[ordinal]
        paragraph_tags = tags[pid]
        speech_parts.append(marked.text)
        spoken_parts.append(_without_spans(marked.text, paragraph_tags))
        selected_offsets[pid] = (global_pos, global_pos + len(marked.text))
        if marked.text:
            segments.append(MappedSegment(
                global_pos, global_pos + len(marked.text), pid, 0, len(marked.text),
                _bookmark_for_interval(tagged, ordinal, 0, len(marked.text)),
            ))
        global_pos += len(marked.text)
        if selected_index + 1 < len(selected):
            next_pid = selected[selected_index + 1]
            speech_parts.append("\n\n")
            spoken_parts.append("\n\n")
            separators.append(SeparatorMapping(global_pos, global_pos + 2, pid, next_pid))
            global_pos += 2
    paragraphs = tuple(
        ParagraphProjection(
            pid,
            ordinal,
            tagged.paragraphs[ordinal].text,
            tagged.paragraphs[ordinal].style,
            selected_offsets[pid][0] if pid in selected_offsets else None,
            selected_offsets[pid][1] if pid in selected_offsets else None,
            tags[pid],
            tagged.paragraphs[ordinal].bookmarks,
        )
        for ordinal, pid in enumerate(paragraph_ids)
    )
    speech = "".join(speech_parts)
    spoken = "".join(spoken_parts)
    # Speech offsets include identified tag text; the spoken projection removes
    # only those spans and keeps the same paragraph separators.
    view_id = hashlib.sha256(
        (pair_binding + "\0" + "\0".join(selected) + "\0" + repr(tuple(tags.items())) + "\0" + repr(exclusions)).encode("utf-8")
    ).hexdigest()
    return ProjectedDocument(
        view_id, prose.raw_sha256, tagged.raw_sha256, PROJECTION_VERSION,
        prose_text, sha256_text(prose_text), spoken, sha256_text(spoken),
        speech, sha256_text(speech), paragraph_ids, paragraphs, exclusions,
        tuple(segments), tuple(separators), source_matches,
        tuple((*prose.headers_footers, *tagged.headers_footers)),
        tuple((*prose.unsupported, *tagged.unsupported)),
    )


def _bookmark_for_interval(document: DocxProjection, ordinal: int, start: int, end: int) -> str | None:
    covering = [
        mark.name for mark in document.bookmarks
        if (mark.paragraph_ordinal, mark.offset) <= (ordinal, start)
        and (mark.end_paragraph_ordinal, mark.end_offset) >= (ordinal, end)
    ]
    return covering[0] if len(covering) == 1 else None


def _codepoint_to_limit_count(text: str, unit: str) -> int:
    if unit == "unicode_codepoints":
        return len(text)
    if unit == "utf16_units":
        return len(text.encode("utf-16-le", errors="strict")) // 2
    raise ProjectionError("invalid_request_limit", "Count unit must be unicode_codepoints or utf16_units")


def _grapheme_boundaries(text: str) -> set[int]:
    result = {0, len(text)}
    for match in _regex.finditer(r"\X", text):
        result.add(match.start())
        result.add(match.end())
    return result


def validate_chunk_ranges(
    document: ProjectedDocument,
    chunks: Sequence[ChunkRange | Mapping[str, object]],
    *,
    limit: int,
    unit: str = "unicode_codepoints",
) -> tuple[tuple[ValidatedChunkRange, ...], Coverage]:
    """Validate caller-chosen ranges as a once-only partition without rechunking."""
    limit = _safe_int(limit, positive=True)
    ranges: list[ChunkRange] = []
    for item in chunks:
        if isinstance(item, ChunkRange) or all(hasattr(item, field) for field in ("chunk_id", "start", "end")):
            chunk_id = item.chunk_id
            if not isinstance(chunk_id, str) or not chunk_id:
                raise ProjectionError("invalid_chunk_range", "Chunk ID must be a nonempty string")
            ranges.append(ChunkRange(chunk_id, _safe_int(item.start), _safe_int(item.end, positive=True)))
        else:
            try:
                chunk_id = item["chunk_id"]
                if not isinstance(chunk_id, str) or not chunk_id:
                    raise TypeError("chunk_id must be a nonempty string")
                ranges.append(ChunkRange(chunk_id, _safe_int(item["start"]), _safe_int(item["end"], positive=True)))
            except (KeyError, TypeError, ValueError) as exc:
                raise ProjectionError("invalid_chunk_range", "Malformed explicit chunk range") from exc
    ids = [item.chunk_id for item in ranges]
    if len(ids) != len(set(ids)):
        raise ProjectionError("duplicate_chunk_id", "Chunk IDs must be unique in this explicit plan")
    text = document.speech_text
    graphemes = _grapheme_boundaries(text)
    forbidden = {
        paragraph.speech_start + position
        for paragraph in document.paragraphs
        if paragraph.speech_start is not None
        for start, end in paragraph.tags
        for position in range(start + 1, end)
    }
    separator_middle = {separator.start + 1 for separator in document.separators}
    cursor = 0
    covered = 0
    validated: list[ValidatedChunkRange] = []
    gaps = overlaps = 0
    for chunk in ranges:
        if chunk.start < cursor:
            overlaps += max(0, min(cursor, chunk.end) - chunk.start)
        elif chunk.start > cursor:
            gaps += chunk.start - cursor
        if chunk.start < 0 or chunk.end > len(text) or chunk.end <= chunk.start:
            raise ProjectionError("invalid_chunk_range", f"Chunk {chunk.chunk_id} must be nonempty and within speech_text")
        if chunk.start not in graphemes or chunk.end not in graphemes:
            raise ProjectionError("grapheme_boundary", f"Chunk {chunk.chunk_id} splits a Unicode grapheme sequence")
        if chunk.start in forbidden or chunk.end in forbidden:
            raise ProjectionError("tag_boundary", f"Chunk {chunk.chunk_id} splits an identified audio tag")
        if chunk.start in separator_middle or chunk.end in separator_middle:
            raise ProjectionError("separator_boundary", f"Chunk {chunk.chunk_id} splits a two-LF paragraph separator")
        if chunk.start != cursor:
            raise ProjectionError("coverage_error", f"Chunk {chunk.chunk_id} does not continue exact ordered coverage")
        value = text[chunk.start:chunk.end]
        count = _codepoint_to_limit_count(value, unit)
        if count > limit:
            raise ProjectionError("request_limit_exceeded", f"Chunk {chunk.chunk_id} has {count} {unit}; limit is {limit}")
        source_segments = _source_segments(document, chunk.start, chunk.end)
        validated.append(ValidatedChunkRange(
            chunk.chunk_id, chunk.start, chunk.end, len(value), count,
            sha256_text(value), source_segments,
            tuple(separator for separator in document.separators
                  if chunk.start <= separator.start and separator.end <= chunk.end),
        ))
        cursor = chunk.end
        covered += len(value)
    if cursor < len(text):
        gaps += len(text) - cursor
    if not ranges or cursor != len(text) or gaps or overlaps:
        raise ProjectionError("coverage_error", "Explicit chunk ranges must partition speech_text exactly once")
    return tuple(validated), Coverage(len(text), covered, gaps, overlaps)


def _source_segments(document: ProjectedDocument, start: int, end: int) -> tuple[SourceSegment, ...]:
    result: list[SourceSegment] = []
    for segment in document.segments:
        left = max(start, segment.speech_start)
        right = min(end, segment.speech_end)
        if left < right:
            result.append(SourceSegment(
                segment.paragraph_id,
                segment.paragraph_start + left - segment.speech_start,
                segment.paragraph_start + right - segment.speech_start,
                segment.bookmark,
            ))
    return tuple(result)
