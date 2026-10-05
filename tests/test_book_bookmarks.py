from __future__ import annotations

import io
import zipfile
from xml.etree import ElementTree as ET

import pytest

from cognita.books.docx import (
    BookmarkLocation,
    BookmarkPlacement,
    MalformedBookmarks,
    add_bookmarks,
    parse_docx,
)
from cognita.books.projection import project_docx_pair


W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
NS = {"w": W}


def _package(document: str, *, extra: dict[str, bytes] | None = None) -> bytes:
    parts = {
        "[Content_Types].xml": b"<Types xmlns=\"http://schemas.openxmlformats.org/package/2006/content-types\"/>",
        "word/document.xml": document.encode(),
        "word/styles.xml": f'<w:styles xmlns:w="{W}"/>'.encode(),
        "custom/opaque.bin": b"must-remain-byte-identical\x00\xfe",
    }
    parts.update(extra or {})
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in parts.items():
            archive.writestr(name, content)
    return output.getvalue()


def _document(paragraph: str) -> str:
    return f'<w:document xmlns:w="{W}"><w:body><w:p>{paragraph}</w:p><w:sectPr/></w:body></w:document>'


def _parts(raw: bytes) -> dict[str, bytes]:
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        return {name: archive.read(name) for name in archive.namelist()}


def _style_at(projection, index: int) -> str | None:
    return next((style for start, end, style in projection.paragraphs[0].styled_runs if start <= index < end), None)


def test_bookmark_insertion_preserves_text_properties_and_other_package_parts() -> None:
    p = (
        '<w:r><w:rPr><w:i/></w:rPr><w:t xml:space="preserve">Italic text</w:t></w:r>'
        '<w:r><w:rPr><w:b/></w:rPr><w:t xml:space="preserve"> and bold</w:t></w:r>'
        '<w:r><w:tab/></w:r><w:r><w:br/></w:r>'
        '<w:r><w:rPr><w:color w:val="008000"/></w:rPr><w:t>ending</w:t></w:r>'
    )
    raw = _package(_document(p))
    projection = parse_docx(raw)
    text = projection.paragraphs[0].text
    result = add_bookmarks(
        raw,
        projection,
        [BookmarkPlacement("audio_ch001_chunk0001", BookmarkLocation(projection.paragraphs[0].paragraph_id, 3), BookmarkLocation(projection.paragraphs[0].paragraph_id, len(text) - 2))],
    )
    updated = parse_docx(result)
    assert updated.paragraphs[0].text == text
    assert [(b.name, b.paragraph_ordinal, b.offset, b.end_paragraph_ordinal, b.end_offset) for b in updated.bookmarks] == [
        ("audio_ch001_chunk0001", 0, 3, 0, len(text) - 2)
    ]
    original_parts = _parts(raw)
    changed_parts = _parts(result)
    for name, content in original_parts.items():
        if name != "word/document.xml":
            assert changed_parts[name] == content
    for index in range(len(text)):
        assert _style_at(updated, index) == _style_at(projection, index)
    old_root = ET.fromstring(original_parts["word/document.xml"])
    new_root = ET.fromstring(changed_parts["word/document.xml"])
    assert old_root.find(".//w:pPr", NS) == new_root.find(".//w:pPr", NS)


def test_existing_bookmark_pairs_are_preserved_and_malformed_pairs_fail() -> None:
    p = (
        '<w:bookmarkStart w:id="7" w:name="existing"/>'
        '<w:r><w:t>hello world</w:t></w:r>'
        '<w:bookmarkEnd w:id="7"/>'
    )
    raw = _package(_document(p))
    projection = parse_docx(raw)
    updated = add_bookmarks(
        raw, projection,
        [BookmarkPlacement("audio_chunk_1", BookmarkLocation(projection.paragraphs[0].paragraph_id, 1), BookmarkLocation(projection.paragraphs[0].paragraph_id, 5))],
    )
    names = {bookmark.name for bookmark in parse_docx(updated).bookmarks}
    assert names == {"existing", "audio_chunk_1"}
    malformed = _package(_document('<w:bookmarkStart w:id="1" w:name="orphan"/><w:r><w:t>x</w:t></w:r>'))
    with pytest.raises(MalformedBookmarks):
        parse_docx(malformed)
    duplicate = _package(_document(
        '<w:bookmarkStart w:id="1" w:name="same"/><w:bookmarkEnd w:id="1"/>'
        '<w:bookmarkStart w:id="2" w:name="same"/><w:bookmarkEnd w:id="2"/>'
    ))
    with pytest.raises(MalformedBookmarks, match="duplicated"):
        parse_docx(duplicate)


def test_new_bookmark_name_is_validated() -> None:
    raw = _package(_document('<w:r><w:t>text</w:t></w:r>'))
    projection = parse_docx(raw)
    for name in ("", "1invalid", "bad-name", "x" * 41):
        with pytest.raises(MalformedBookmarks):
            add_bookmarks(
                raw, projection,
                [BookmarkPlacement(name, BookmarkLocation(projection.paragraphs[0].paragraph_id, 0), BookmarkLocation(projection.paragraphs[0].paragraph_id, 1))],
            )


def test_unsupported_main_body_and_header_footer_are_located_and_separate() -> None:
    raw = _package(
        f'<w:document xmlns:w="{W}"><w:body><w:p><w:r><w:t>spoken</w:t></w:r></w:p>'
        '<w:tbl><w:tr><w:tc><w:p><w:r><w:t>table</w:t></w:r></w:p></w:tc></w:tr></w:tbl>'
        '<w:sectPr/></w:body></w:document>',
        extra={"word/header1.xml": f'<w:hdr xmlns:w="{W}"><w:p><w:r><w:t>running head</w:t></w:r></w:p></w:hdr>'.encode()},
    )
    projection = parse_docx(raw)
    assert projection.paragraphs[0].text == "spoken"
    assert projection.headers_footers == ("word/header1.xml:p[0]: running head",)
    assert len(projection.unsupported) == 1
    assert projection.unsupported[0].location == "/body/tbl[1]"


def test_pair_projection_flags_tag_mismatch_without_guessing() -> None:
    source = _package(_document('<w:r><w:t>source prose</w:t></w:r>'))
    marked = _package(_document('<w:r><w:t>different prose</w:t></w:r>'))
    result = project_docx_pair(source, marked)
    assert result.source_text_matches_without_tags is False
    assert result.spoken_projection == "different prose"
