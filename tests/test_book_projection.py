from __future__ import annotations

import hashlib
import io
import zipfile

import pytest

from cognita.books.projection import (
    ChunkRange,
    ExplicitTagSpan,
    ExcludedParagraph,
    ProjectionError,
    project_docx_pair,
    validate_chunk_ranges,
)


W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
XML = "http://www.w3.org/XML/1998/namespace"


def _t(value: str) -> str:
    return f'<w:t xml:space="preserve">{value}</w:t>'


def _run(value: str, props: str = "") -> str:
    return f"<w:r>{props}{_t(value)}</w:r>"


def _docx(paragraphs: list[str], *, extra_parts: dict[str, bytes] | None = None) -> bytes:
    body = "".join(f"<w:p>{value}</w:p>" for value in paragraphs)
    xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<w:document xmlns:w="{W}" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" '
        f'xmlns:xml="{XML}"><w:body>{body}<w:sectPr/></w:body></w:document>'
    ).encode()
    parts = {
        "[Content_Types].xml": b"<Types xmlns=\"http://schemas.openxmlformats.org/package/2006/content-types\"/>",
        "word/document.xml": xml,
        "word/styles.xml": (
            f'<w:styles xmlns:w="{W}"><w:style w:type="character" w:styleId="CognitaAudioTag">'
            '<w:name w:val="CognitaAudioTag"/></w:style></w:styles>'
        ).encode(),
        "word/media/preserve.bin": b"opaque-part-bytes\x00\xff",
    }
    parts.update(extra_parts or {})
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in parts.items():
            archive.writestr(name, data)
    return output.getvalue()


def _pair(source: list[str], marked: list[str]):
    return project_docx_pair(_docx(source), _docx(marked))


def test_only_explicit_audio_tag_style_is_removed_and_text_is_exact() -> None:
    green = '<w:rPr><w:highlight w:val="green"/></w:rPr>'
    italic = "<w:rPr><w:i/></w:rPr>"
    source_p = (
        _run("A [real prose] ", italic)
        + _run("green words", green)
        + '<w:r><w:tab/></w:r>'
        + _run("line")
        + '<w:r><w:br/></w:r>'
        + _run("next e\u0301 👩\u200d💻")
    )
    tagged_p = source_p + _run(" [SFX: pause]", '<w:rPr><w:rStyle w:val="CognitaAudioTag"/></w:rPr>')
    result = _pair([source_p], [tagged_p])

    assert result.paragraphs[0].text == "A [real prose] green words\tline\nnext e\u0301 👩\u200d💻 [SFX: pause]"
    assert result.paragraphs[0].tags == ((result.paragraphs[0].text.rindex(" [SFX:"), len(result.paragraphs[0].text)),)
    assert result.speech_text == result.paragraphs[0].text
    assert result.spoken_projection == "A [real prose] green words\tline\nnext e\u0301 👩\u200d💻"
    assert result.source_text_matches_without_tags is True
    assert "[real prose]" in result.spoken_projection and "green words" in result.spoken_projection
    assert result.paragraphs[0].style == "Normal"


def test_exact_caller_tag_span_is_pinned_by_text_hash() -> None:
    source = _docx([_run("before  after")])
    marked = _docx([_run("before [tag] after")])
    tag = "[tag]"
    # IDs are derived from the pinned source/tagged byte pair and paragraph ordinal.
    provisional = project_docx_pair(source, marked)
    span = ExplicitTagSpan(
        provisional.paragraph_ids[0], 7, 12, hashlib.sha256(tag.encode()).hexdigest()
    )
    result = project_docx_pair(source, marked, explicit_tag_spans=[span])
    assert result.spoken_projection == "before  after"
    assert result.source_text_matches_without_tags is True
    with pytest.raises(ProjectionError, match="expected hash"):
        project_docx_pair(source, marked, explicit_tag_spans=[ExplicitTagSpan(span.paragraph_id, 7, 12, "0" * 64)])


def test_excluded_paragraph_is_not_spoken_and_source_map_keeps_separator_separate() -> None:
    paras = [_run("first"), _run("exclude me"), _run("last")]
    result = _pair(paras, paras)
    pids = result.paragraph_ids
    result = project_docx_pair(
        _docx(paras), _docx(paras),
        speech_paragraph_ids=[pids[0], pids[2]],
        excluded_paragraphs=[ExcludedParagraph(pids[1], "scene instruction")],
    )
    assert result.speech_text == "first\n\nlast"
    assert result.spoken_projection == result.speech_text
    assert result.paragraphs[1].speech_start is None
    assert [(s.start, s.end) for s in result.separators] == [(5, 7)]
    chunks, coverage = validate_chunk_ranges(
        result, [ChunkRange("c1", 0, 7), ChunkRange("c2", 7, 11)], limit=20
    )
    assert [(x.paragraph_id, x.start, x.end) for x in chunks[0].source_segments] == [(pids[0], 0, 5)]
    assert [(x.paragraph_id, x.start, x.end) for x in chunks[1].source_segments] == [(pids[2], 0, 4)]
    assert [(x.start, x.end) for x in chunks[0].separator_mappings] == [(5, 7)]
    assert chunks[1].separator_mappings == ()
    assert coverage.speech_codepoints == coverage.covered_codepoints == 11


def test_chunk_ranges_reject_gaps_overlaps_grapheme_tags_separators_and_limit() -> None:
    result = _pair([_run("A e\u0301 👩\u200d💻")], [_run("A e\u0301 👩\u200d💻 [tag]")])
    tag_start = result.speech_text.index("[tag]")
    tagged = project_docx_pair(
        _docx([_run("A e\u0301 👩\u200d💻")]),
        _docx([_run("A e\u0301 👩\u200d💻 [tag]")]),
        explicit_tag_spans=[ExplicitTagSpan(result.paragraph_ids[0], tag_start, len(result.speech_text), hashlib.sha256(b"[tag]").hexdigest())],
    )
    with pytest.raises(ProjectionError, match="exact ordered coverage"):
        validate_chunk_ranges(tagged, [ChunkRange("c", 1, len(tagged.speech_text))], limit=100)
    with pytest.raises(ProjectionError, match="grapheme"):
        validate_chunk_ranges(tagged, [ChunkRange("c1", 0, 3), ChunkRange("c2", 3, len(tagged.speech_text))], limit=100)
    with pytest.raises(ProjectionError, match="tag"):
        validate_chunk_ranges(tagged, [ChunkRange("c1", 0, tag_start + 2), ChunkRange("c2", tag_start + 2, len(tagged.speech_text))], limit=100)
    with pytest.raises(ProjectionError, match="limit"):
        validate_chunk_ranges(tagged, [ChunkRange("c", 0, len(tagged.speech_text))], limit=2)


def test_utf16_limit_count_does_not_change_codepoint_offsets() -> None:
    text = "A😀B"
    result = _pair([_run(text)], [_run(text)])
    chunks, _ = validate_chunk_ranges(result, [ChunkRange("c", 0, len(text))], limit=4, unit="utf16_units")
    assert chunks[0].start == 0 and chunks[0].end == 3
    assert chunks[0].codepoint_count == 3 and chunks[0].limit_count == 4


def test_each_omitted_paragraph_needs_a_reason_and_ranges_cannot_split_lf_pair() -> None:
    paras = [_run("one"), _run("two")]
    base = _pair(paras, paras)
    with pytest.raises(ProjectionError, match="exclusion reason"):
        project_docx_pair(
            _docx(paras), _docx(paras), speech_paragraph_ids=[base.paragraph_ids[0]]
        )
    with pytest.raises(ProjectionError, match="separator"):
        validate_chunk_ranges(
            base, [ChunkRange("c1", 0, 4), ChunkRange("c2", 4, len(base.speech_text))], limit=20
        )


def test_zwj_grapheme_cannot_be_split_and_chunks_must_cover_once() -> None:
    value = "x👩\u200d💻y"
    result = _pair([_run(value)], [_run(value)])
    with pytest.raises(ProjectionError, match="grapheme"):
        validate_chunk_ranges(
            result, [ChunkRange("c1", 0, 2), ChunkRange("c2", 2, len(value))], limit=20
        )
    ascii_doc = _pair([_run("abc")], [_run("abc")])
    with pytest.raises(ProjectionError, match="coverage"):
        validate_chunk_ranges(
            ascii_doc, [ChunkRange("c1", 0, 1), ChunkRange("c2", 2, 3)], limit=20
        )
