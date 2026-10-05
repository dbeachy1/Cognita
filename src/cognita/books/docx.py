"""Lossless, deliberately narrow DOCX inspection and bookmark editing.

The parser reads package bytes directly.  It does not save a ``python-docx``
Document, which would rewrite package parts unrelated to the requested edit.
"""

from __future__ import annotations

import ctypes
import hashlib
import io
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence
from lxml import etree as ET


W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
XML = "http://www.w3.org/XML/1998/namespace"
NS = {"w": W}
PROJECTION_VERSION = "cognita-docx-v1"
_BOOKMARK_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,39}$")
_UNSUPPORTED_RUN_CHILDREN = {
    "drawing", "pict", "object", "fldChar", "fldSimple", "instrText",
    "delText", "del", "ins", "moveFrom", "moveTo", "footnoteReference",
    "endnoteReference", "commentReference", "sym", "ruby", "altChunk",
}


class DocxProjectionError(ValueError):
    """Base error carrying a stable reason and exact package location."""

    def __init__(self, code: str, message: str, location: str | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.location = location


class UnsupportedDocxStructure(DocxProjectionError):
    def __init__(self, location: str, detail: str):
        super().__init__("unsupported_docx_structure", detail, location)


class MalformedBookmarks(DocxProjectionError):
    def __init__(self, location: str, detail: str):
        super().__init__("malformed_bookmark", detail, location)


class FileLockedError(OSError):
    def __init__(self, path: str | Path):
        super().__init__("file_locked", f"The DOCX is locked by another application: {path}")
        self.code = "file_locked"
        self.path = str(path)


@dataclass(frozen=True)
class UnsupportedLocation:
    part: str
    location: str
    detail: str


@dataclass(frozen=True)
class Bookmark:
    bookmark_id: str
    name: str
    paragraph_ordinal: int
    offset: int
    end_paragraph_ordinal: int
    end_offset: int


@dataclass(frozen=True)
class Paragraph:
    ordinal: int
    text: str
    style: str
    paragraph_id: str
    bookmarks: tuple[str, ...]
    # (start, end, character style) intervals, code-point coordinates.
    styled_runs: tuple[tuple[int, int, str | None], ...]


@dataclass(frozen=True)
class DocxProjection:
    raw_sha256: str
    paragraphs: tuple[Paragraph, ...]
    bookmarks: tuple[Bookmark, ...]
    headers_footers: tuple[str, ...]
    unsupported: tuple[UnsupportedLocation, ...]
    package_parts: tuple[tuple[str, bytes], ...]


def _tag(local: str) -> str:
    return f"{{{W}}}{local}"


def _parse_xml(data: bytes) -> ET._Element:
    parser = ET.XMLParser(resolve_entities=False, no_network=True, remove_comments=False, remove_pis=False)
    return ET.fromstring(data, parser=parser)


def _attr(element: ET.Element, local: str) -> str | None:
    return element.get(_tag(local))


def _style_names(parts: dict[str, bytes]) -> dict[str, str]:
    data = parts.get("word/styles.xml")
    if data is None:
        return {}
    try:
        root = _parse_xml(data)
    except ET.XMLSyntaxError as exc:
        raise DocxProjectionError("invalid_docx", "Malformed word/styles.xml", "word/styles.xml") from exc
    result: dict[str, str] = {}
    for style in root.findall("w:style", NS):
        style_id = _attr(style, "styleId")
        name = style.find("w:name", NS)
        if style_id and name is not None and _attr(name, "val"):
            result[style_id] = _attr(name, "val") or ""
    return result


def _run_style(run: ET.Element, style_names: dict[str, str]) -> str | None:
    props = run.find("w:rPr", NS)
    style = props.find("w:rStyle", NS) if props is not None else None
    style_id = _attr(style, "val") if style is not None else None
    return style_names.get(style_id, style_id) if style_id else None


def _paragraph_style(paragraph: ET.Element, style_names: dict[str, str]) -> str:
    props = paragraph.find("w:pPr", NS)
    style = props.find("w:pStyle", NS) if props is not None else None
    style_id = _attr(style, "val") if style is not None else None
    return style_names.get(style_id, style_id) if style_id else "Normal"


def _append_text(child: ET.Element, output: list[str]) -> None:
    local = child.tag.rsplit("}", 1)[-1]
    if local == "t":
        output.append(child.text or "")
    elif local == "tab":
        output.append("\t")
    elif local in {"br", "cr"}:
        output.append("\n")


def _read_paragraph(
    paragraph: ET.Element,
    ordinal: int,
    part: str,
    location: str,
    style_names: dict[str, str],
) -> tuple[str, str, tuple[tuple[int, int, str | None], ...], list[tuple[str, str, str, int]]]:
    text: list[str] = []
    spans: list[tuple[int, int, str | None]] = []
    marks: list[tuple[str, str, str, int]] = []
    position = 0
    child_index = 0
    for child in paragraph:
        if not isinstance(child.tag, str):
            continue
        local = child.tag.rsplit("}", 1)[-1]
        loc = f"{location}/{local}[{child_index}]"
        child_index += 1
        if child.tag == _tag("pPr"):
            continue
        if child.tag == _tag("r"):
            run_text: list[str] = []
            for component in child:
                if not isinstance(component.tag, str):
                    continue
                name = component.tag.rsplit("}", 1)[-1]
                if name == "rPr":
                    continue
                if name in _UNSUPPORTED_RUN_CHILDREN:
                    raise UnsupportedDocxStructure(f"{part}:{loc}/{name}", f"Unsupported potentially spoken {name} element")
                if name == "br" and _attr(component, "type") not in {None, "textWrapping"}:
                    raise UnsupportedDocxStructure(
                        f"{part}:{loc}/br", "Page/column breaks are outside the supported line-break projection"
                    )
                if name in {"t", "tab", "br", "cr"}:
                    _append_text(component, run_text)
                else:
                    raise UnsupportedDocxStructure(f"{part}:{loc}/{name}", f"Unsupported run content {name}")
            value = "".join(run_text)
            text.append(value)
            if value:
                spans.append((position, position + len(value), _run_style(child, style_names)))
                position += len(value)
            continue
        if child.tag in {_tag("bookmarkStart"), _tag("bookmarkEnd")}:
            kind = "start" if child.tag == _tag("bookmarkStart") else "end"
            mark_id = _attr(child, "id")
            name = (_attr(child, "name") or "") if kind == "start" else ""
            marks.append((kind, mark_id or "", name, position))
            continue
        # These range markers do not contribute text and are retained by edits.
        if local in {"proofErr", "commentRangeStart", "commentRangeEnd", "permStart", "permEnd"}:
            continue
        raise UnsupportedDocxStructure(f"{part}:{loc}", f"Unsupported paragraph child {local}")
    return "".join(text), _paragraph_style(paragraph, style_names), tuple(spans), marks


def _validate_bookmarks(all_marks: list[tuple[str, str, str, int, int]]) -> tuple[Bookmark, ...]:
    starts: dict[str, tuple[str, int, int]] = {}
    ends: dict[str, tuple[int, int]] = {}
    names: set[str] = set()
    for kind, mark_id, name, ordinal, offset in all_marks:
        if not mark_id:
            raise MalformedBookmarks(f"word/document.xml:p[{ordinal}]:bookmark", "Bookmark has no ID")
        if not mark_id.isdecimal():
            raise MalformedBookmarks(
                f"word/document.xml:p[{ordinal}]:bookmark[{mark_id}]", "Bookmark ID is not a nonnegative integer"
            )
        if kind == "start":
            if mark_id in starts:
                raise MalformedBookmarks(f"word/document.xml:p[{ordinal}]:bookmarkStart[{mark_id}]", "Duplicate bookmark start ID")
            if not name or not _BOOKMARK_NAME.fullmatch(name):
                raise MalformedBookmarks(f"word/document.xml:p[{ordinal}]:bookmarkStart[{mark_id}]", "Bookmark name is invalid")
            if name in names:
                raise MalformedBookmarks(f"word/document.xml:p[{ordinal}]:bookmarkStart[{mark_id}]", "Bookmark name is duplicated")
            names.add(name)
            starts[mark_id] = (name, ordinal, offset)
        else:
            if mark_id in ends:
                raise MalformedBookmarks(f"word/document.xml:p[{ordinal}]:bookmarkEnd[{mark_id}]", "Duplicate bookmark end ID")
            ends[mark_id] = (ordinal, offset)
    for mark_id in starts.keys() ^ ends.keys():
        raise MalformedBookmarks(f"word/document.xml:bookmark[{mark_id}]", "Bookmark start/end pair is incomplete")
    result: list[Bookmark] = []
    for mark_id, (name, ordinal, offset) in starts.items():
        end_ordinal, end_offset = ends[mark_id]
        if (end_ordinal, end_offset) < (ordinal, offset):
            raise MalformedBookmarks(f"word/document.xml:bookmark[{mark_id}]", "Bookmark end precedes its start")
        result.append(Bookmark(mark_id, name, ordinal, offset, end_ordinal, end_offset))
    return tuple(result)


def parse_docx(raw: bytes) -> DocxProjection:
    """Parse supported main-body prose and report unsupported content locations."""
    digest = hashlib.sha256(raw).hexdigest()
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as package:
            parts = {name: package.read(name) for name in package.namelist()}
    except (zipfile.BadZipFile, OSError, KeyError) as exc:
        raise DocxProjectionError("invalid_docx", "The file is not a readable DOCX package") from exc
    doc_data = parts.get("word/document.xml")
    if doc_data is None:
        raise DocxProjectionError("invalid_docx", "DOCX has no word/document.xml part", "word/document.xml")
    try:
        root = _parse_xml(doc_data)
    except ET.XMLSyntaxError as exc:
        raise DocxProjectionError("invalid_docx", "Malformed word/document.xml", "word/document.xml") from exc
    styles = _style_names(parts)
    body = root.find("w:body", NS)
    if body is None:
        raise DocxProjectionError("invalid_docx", "DOCX has no document body", "word/document.xml")

    paragraphs: list[Paragraph] = []
    marks: list[tuple[str, str, str, int, int]] = []
    unsupported: list[UnsupportedLocation] = []
    p_ordinal = 0
    body_child = 0
    for element in body:
        if not isinstance(element.tag, str):
            body_child += 1
            continue
        local = element.tag.rsplit("}", 1)[-1]
        if element.tag == _tag("sectPr"):
            continue
        if element.tag != _tag("p"):
            unsupported.append(UnsupportedLocation("word/document.xml", f"/body/{local}[{body_child}]", f"Unsupported body structure {local}"))
            body_child += 1
            continue
        location = f"/body/p[{p_ordinal}]"
        try:
            value, style, run_spans, local_marks = _read_paragraph(
                element, p_ordinal, "word/document.xml", location, styles
            )
        except UnsupportedDocxStructure as exc:
            unsupported.append(UnsupportedLocation(exc.location.split(":", 1)[0], exc.location, exc.message))
            value, style, run_spans, local_marks = "", _paragraph_style(element, styles), (), []
        pid = f"p{p_ordinal:04d}-{hashlib.sha256((PROJECTION_VERSION + chr(0) + digest + chr(0) + value).encode('utf-8')).hexdigest()[:16]}"
        paragraphs.append(Paragraph(p_ordinal, value, style, pid, (), run_spans))
        for kind, mark_id, name, offset in local_marks:
            marks.append((kind, mark_id, name, p_ordinal, offset))
        p_ordinal += 1
        body_child += 1

    # Header/footer text is reported independently and never joins speech.
    header_footer: list[str] = []
    for name, data in parts.items():
        if not re.fullmatch(r"word/(?:header|footer)[^/]*\.xml", name):
            continue
        try:
            part_root = _parse_xml(data)
        except ET.XMLSyntaxError as exc:
            raise DocxProjectionError("invalid_docx", f"Malformed {name}", name) from exc
        for index, p in enumerate(part_root.findall(".//w:p", NS)):
            try:
                value, _, _, _ = _read_paragraph(p, index, name, f"/p[{index}]", styles)
            except UnsupportedDocxStructure as exc:
                unsupported.append(UnsupportedLocation(name, exc.location, exc.message))
                continue
            if value:
                header_footer.append(f"{name}:p[{index}]: {value}")

    # Other parts that can carry spoken text are not silently ignored.
    for name in parts:
        if re.fullmatch(r"word/(?:footnotes|endnotes)\.xml", name):
            unsupported.append(UnsupportedLocation(name, "/", "Footnotes/endnotes are outside the supported speech projection"))
    validated = _validate_bookmarks(marks)
    names_by_paragraph: dict[int, list[str]] = {}
    for bookmark in validated:
        names_by_paragraph.setdefault(bookmark.paragraph_ordinal, []).append(bookmark.name)
    paragraphs = [
        Paragraph(p.ordinal, p.text, p.style, p.paragraph_id,
                  tuple(names_by_paragraph.get(p.ordinal, ())), p.styled_runs)
        for p in paragraphs
    ]
    return DocxProjection(
        digest, tuple(paragraphs), validated, tuple(header_footer), tuple(unsupported),
        tuple((name, value) for name, value in parts.items()),
    )


def is_file_locked(path: str | Path) -> bool:
    """Detect a native Windows sharing lock for this path; never changes sharing."""
    target = Path(path)
    # Word's owner file is a direct, scoped signal for an open document.
    if target.parent.joinpath(f"~${target.name}").exists():
        return True
    if not hasattr(ctypes, "windll"):
        return False
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
    create_file = kernel32.CreateFileW
    create_file.restype = ctypes.c_void_p
    create_file.argtypes = [
        ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p,
        ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p,
    ]
    handle = create_file(str(target), 0x80000000, 0, None, 3, 0x80, None)
    invalid = ctypes.c_void_p(-1).value
    if handle == invalid:
        error = ctypes.get_last_error()
        return error in {32, 33}  # sharing violation or lock violation
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [ctypes.c_void_p]
    close_handle.restype = ctypes.c_int
    close_handle(ctypes.c_void_p(handle))
    return False


def require_unlocked(path: str | Path) -> None:
    if is_file_locked(path):
        raise FileLockedError(path)


@dataclass(frozen=True)
class BookmarkLocation:
    paragraph_id: str
    offset: int


@dataclass(frozen=True)
class BookmarkPlacement:
    name: str
    start: BookmarkLocation
    end: BookmarkLocation


def add_bookmarks(raw: bytes, projection: DocxProjection, placements: Sequence[BookmarkPlacement]) -> bytes:
    """Add paired bookmarks to document.xml while preserving every other ZIP part."""
    if not placements:
        return raw
    if hashlib.sha256(raw).hexdigest() != projection.raw_sha256:
        raise DocxProjectionError("stale_file", "DOCX bytes differ from the pinned bookmark projection")
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as source:
            items = [(info, source.read(info.filename)) for info in source.infolist()]
    except (zipfile.BadZipFile, OSError) as exc:
        raise DocxProjectionError("invalid_docx", "The file is not a readable DOCX package") from exc
    doc_index = next((i for i, (info, _) in enumerate(items) if info.filename == "word/document.xml"), None)
    if doc_index is None:
        raise DocxProjectionError("invalid_docx", "DOCX has no word/document.xml part", "word/document.xml")
    try:
        root = _parse_xml(items[doc_index][1])
    except ET.XMLSyntaxError as exc:
        raise DocxProjectionError("invalid_docx", "Malformed word/document.xml", "word/document.xml") from exc
    body = root.find("w:body", NS)
    if body is None:
        raise DocxProjectionError("invalid_docx", "DOCX has no document body", "word/document.xml")
    paragraphs = body.findall("w:p", NS)
    by_id = {p.paragraph_id: (paragraphs[p.ordinal], p) for p in projection.paragraphs}
    all_names = {item.name for item in projection.bookmarks}
    ids = [int(bookmark.bookmark_id) for bookmark in projection.bookmarks if bookmark.bookmark_id.isdecimal()]
    next_id = max(ids, default=-1) + 1
    seen: set[str] = set()
    edits: list[tuple[ET.Element, int, str, int, str]] = []
    for placement in placements:
        if not _BOOKMARK_NAME.fullmatch(placement.name) or placement.name in all_names or placement.name in seen:
            raise MalformedBookmarks("word/document.xml", f"Invalid or duplicate new bookmark name: {placement.name!r}")
        seen.add(placement.name)
        if placement.start.paragraph_id not in by_id or placement.end.paragraph_id not in by_id:
            raise DocxProjectionError("invalid_bookmark_range", "Bookmark location is outside this pinned projection")
        start_xml, start_para = by_id[placement.start.paragraph_id]
        end_xml, end_para = by_id[placement.end.paragraph_id]
        if placement.start.offset < 0 or placement.start.offset > len(start_para.text):
            raise DocxProjectionError("invalid_bookmark_range", "Bookmark start is outside paragraph text")
        if placement.end.offset < 0 or placement.end.offset > len(end_para.text):
            raise DocxProjectionError("invalid_bookmark_range", "Bookmark end is outside paragraph text")
        if (start_para.ordinal, placement.start.offset) >= (end_para.ordinal, placement.end.offset):
            raise DocxProjectionError("invalid_bookmark_range", "Bookmark range must be nonempty and ordered")
        edits.append((start_xml, placement.start.offset, "start", next_id, placement.name))
        edits.append((end_xml, placement.end.offset, "end", next_id, placement.name))
        next_id += 1
    # Apply from right to left per paragraph so earlier offsets remain valid.
    grouped: dict[ET.Element, list[tuple[int, str, int, str]]] = {}
    for para, offset, kind, mark_id, name in edits:
        grouped.setdefault(para, []).append((offset, kind, mark_id, name))
    for para, marks in grouped.items():
        for offset, kind, mark_id, name in sorted(
            marks, key=lambda item: (item[0], item[1] == "start"), reverse=True
        ):
            _insert_marker(para, offset, kind, mark_id, name)
    items[doc_index] = (items[doc_index][0], ET.tostring(root, encoding="utf-8", xml_declaration=True))
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as target:
        for info, data in items:
            target.writestr(info, data)
    return out.getvalue()


def _text_components(run: ET.Element) -> list[tuple[ET.Element, str]]:
    result: list[tuple[ET.Element, str]] = []
    for child in run:
        if not isinstance(child.tag, str):
            continue
        local = child.tag.rsplit("}", 1)[-1]
        if local == "t":
            result.append((child, child.text or ""))
        elif local == "tab":
            result.append((child, "\t"))
        elif local in {"br", "cr"}:
            result.append((child, "\n"))
    return result


def _clone_run(run: ET.Element, children: Iterable[ET.Element]) -> ET.Element:
    result = ET.Element(run.tag, run.attrib)
    for child in children:
        result.append(ET.fromstring(ET.tostring(child)))
    return result


def _insert_marker(paragraph: ET.Element, offset: int, kind: str, mark_id: int, name: str) -> None:
    current = 0
    for run in list(paragraph):
        if run.tag != _tag("r"):
            continue
        parts = _text_components(run)
        run_text = "".join(value for _, value in parts)
        if not run_text:
            continue
        if current <= offset <= current + len(run_text):
            local_offset = offset - current
            children = list(run)
            text_children = [c for c in children if c.tag != _tag("rPr")]
            rpr = [c for c in children if c.tag == _tag("rPr")]
            before: list[ET.Element] = []
            after: list[ET.Element] = []
            consumed = 0
            split_done = False
            for child in text_children:
                if not isinstance(child.tag, str):
                    (after if split_done else before).append(child)
                    continue
                local_name = child.tag.rsplit("}", 1)[-1]
                value = child.text or "" if local_name == "t" else "\t" if local_name == "tab" else "\n" if local_name in {"br", "cr"} else ""
                if not split_done and consumed <= local_offset <= consumed + len(value) and value:
                    inner = local_offset - consumed
                    if inner == 0:
                        after.append(child)
                    elif inner == len(value):
                        before.append(child)
                    elif local_name == "t":
                        left = ET.Element(child.tag, child.attrib)
                        left.text = value[:inner]
                        right = ET.Element(child.tag, child.attrib)
                        right.text = value[inner:]
                        before.append(left)
                        after.append(right)
                    else:
                        raise DocxProjectionError("invalid_bookmark_range", "Cannot split non-text run content")
                    split_done = True
                elif not split_done and consumed + len(value) <= local_offset:
                    before.append(child)
                else:
                    after.append(child)
                consumed += len(value)
            if not split_done:
                before = text_children
            index = list(paragraph).index(run)
            prefix = _clone_run(run, [*rpr, *before]) if before else None
            suffix = _clone_run(run, [*rpr, *after]) if after else None
            paragraph.remove(run)
            marker = ET.Element(_tag("bookmarkStart" if kind == "start" else "bookmarkEnd"))
            marker.set(_tag("id"), str(mark_id))
            if kind == "start":
                marker.set(_tag("name"), name)
            insert_at = index
            if prefix is not None:
                paragraph.insert(insert_at, prefix)
                insert_at += 1
            paragraph.insert(insert_at, marker)
            insert_at += 1
            if suffix is not None:
                paragraph.insert(insert_at, suffix)
            return
        current += len(run_text)
    # Empty paragraph or exact end: append without changing paragraph properties.
    marker = ET.Element(_tag("bookmarkStart" if kind == "start" else "bookmarkEnd"))
    marker.set(_tag("id"), str(mark_id))
    if kind == "start":
        marker.set(_tag("name"), name)
    paragraph.append(marker)
