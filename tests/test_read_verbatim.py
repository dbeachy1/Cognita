"""Reads are byte-verbatim (5.6) - the mirror of test_write_verbatim.py.

5.0 made the WRITE path byte-exact and stopped there, so for five releases
Cognita stored perfectly and answered wrong. Three read paths existed and two of
them altered the document on the way out:

1. `get_document` returned the INDEXER's extraction rather than the file. A
   .json came back through json.dumps(indent=2) - a compact array exploded
   across four lines and the trailing newline gone; a .md came back with its
   YAML frontmatter DELETED; a .csv came back as a " | "-joined table; anything
   with CRLF came back folded to LF. None of it was announced, and the other
   read paths (content_sha256, find_literal, list_documents) reported the truth
   about the same file the whole time - so the single tool a caller reaches for
   to READ a document was the one tool lying about it.
2. `read_document` folded CRLF/CR to LF and dropped the BOM. That one WAS
   documented, on the grounds that the edit matcher needs normalized anchors.
   The grounds were wrong: apply_edit normalizes old_str as well, so a CRLF
   anchor always matched a CRLF file. The folding bought nothing.
3. Editing wrote the normalized whole file back, so one word changed in a CRLF
   document re-flavored every line ending in it and deleted its BOM.

The rule these pin, with no carve-outs: what Cognita is handed is what Cognita
hands back. Extraction is for the index; it is never what a read returns.

The single assertion that catches this entire class is sha256 of the bytes read
back == sha256 of the bytes sent, per format, on payloads chosen so canonical
re-serialization and whitespace normalization cannot survive them.
"""

import hashlib

import pytest

from cognita.editing import apply_edit, restore_line_endings
from cognita.engine_local import _verbatim_text
from cognita.parsing import parse_file
from cognita.reading import read_slice

BOM = "﻿"

# One payload per format, each with non-canonical whitespace AND a trailing
# newline. The .json is the reported case: json.dumps(indent=2) reflows the
# inline array and drops the final newline, so byte equality fails twice over.
PAYLOADS = {
    "doc.json": '{\n  "k": ["a", "b"],\n  "n": 1\n}\n',
    "doc.md": "---\ntitle: T\n---\n\n#  Heading\n\n  indented body\n",
    "doc.py": "def f():\n    return  1\n\n\n",
    "doc.txt": "  leading\ttab\nsecond\n\n",
    "doc.csv": "a,b\n1,2\n",
    "doc.yml": "k:  v\nlist:\n  - a\n",
    "doc.xml": '<?xml version="1.0"?>\n<r><a/></r>\n',
}


@pytest.mark.parametrize("name", sorted(PAYLOADS))
def test_get_document_content_is_the_file(tmp_path, name):
    """sha256(content read back) == sha256(content sent), for every format.

    _verbatim_text is what _get_document now serves as `content`; parse_file is
    what it used to serve. Both are exercised so the test says which layer moved.
    """
    sent = PAYLOADS[name]
    target = tmp_path / name
    target.write_bytes(sent.encode("utf-8"))

    served = _verbatim_text(target)
    assert served == sent, f"{name}: read path altered the document"
    assert hashlib.sha256(served.encode("utf-8")).hexdigest() == (
        hashlib.sha256(sent.encode("utf-8")).hexdigest()
    )


@pytest.mark.parametrize("name", sorted(PAYLOADS))
def test_the_indexed_extraction_is_still_derived_separately(tmp_path, name):
    """The extraction is NOT being deleted - it is just no longer served.

    Pinned as its own assertion so a future change that "fixes" the read by
    breaking the indexer fails here instead of silently degrading retrieval.
    """
    target = tmp_path / name
    target.write_bytes(PAYLOADS[name].encode("utf-8"))
    doc = parse_file(target, tmp_path)
    assert doc is not None and doc.content


def test_json_is_not_reserialized(tmp_path):
    """The reported bug, named explicitly (2026-08-30).

    An inline array must not be exploded across lines and the trailing newline
    must survive - those are the two halves json.dumps(indent=2) broke.
    """
    sent = '{\n  "k": ["a", "b"],\n  "n": 1\n}\n'
    target = tmp_path / "p.json"
    target.write_bytes(sent.encode("utf-8"))
    served = _verbatim_text(target)
    assert '"k": ["a", "b"]' in served, "inline array was reflowed"
    assert served.endswith("}\n"), "trailing newline was dropped"
    assert served == sent


def test_markdown_frontmatter_survives_a_read(tmp_path):
    """The quietest half of the bug: frontmatter came back DELETED, not reformatted."""
    sent = "---\ntitle: T\nid: 7\n---\n\n# Body\n"
    target = tmp_path / "f.md"
    target.write_bytes(sent.encode("utf-8"))
    assert _verbatim_text(target) == sent


def test_crlf_and_bom_survive_a_read(tmp_path):
    sent = BOM + "a\r\nb\r\n"
    target = tmp_path / "w.md"
    target.write_bytes(sent.encode("utf-8"))
    assert _verbatim_text(target) == sent


def test_binary_formats_report_no_verbatim_text(tmp_path):
    """A .pdf has no text on disk. None is the signal that makes _get_document
    say `content_is_extracted: true` instead of pretending."""
    target = tmp_path / "b.pdf"
    target.write_bytes(b"%PDF-1.4\n\xff\xfe\x00binary")
    assert _verbatim_text(target) is None


# --------------------------------------------------- a file that exists says so

def test_a_file_with_no_indexable_text_is_not_reported_as_missing(tmp_path):
    """get_document answered `not_found` for a file sitting on disk.

    parse_file returns None when a document extracts to nothing — a .md that is
    only YAML frontmatter, or a whitespace-only file — and _get_document turned
    that into "Document not found". Same class of untruth as the reformatted
    read: the caller is told the wrong thing about the bytes. And self-defeating,
    because read_document serves the file perfectly, so a caller believing
    not_found stops one call short of its own content.

    This pins the two halves that matter: parse_file still declines to index it
    (that part was correct), and the file is still readable verbatim.
    """
    from cognita.parsing import parse_file

    target = tmp_path / "front.md"
    sent = "---\ntitle: only frontmatter\n---\n"
    target.write_bytes(sent.encode("utf-8"))

    assert target.is_file() and target.stat().st_size == 32
    assert parse_file(target, tmp_path) is None  # correctly unindexable...
    assert _verbatim_text(target) == sent        # ...and still perfectly readable


def test_no_indexable_content_reason_is_preserved_for_clients():
    """The public error reason survives the gateway backstop unchanged."""
    from cognita.engine_local import _with_reason

    result = _with_reason(
        "get_document",
        {"status": "error", "reason": "no_indexable_content", "message": "No extractable text."},
    )
    assert result["reason"] == "no_indexable_content"


# --------------------------------------------------- read_document (read_slice)

def test_read_slice_returns_the_files_own_newlines():
    text = "alpha\r\nbeta\r\ngamma\r\n"
    assert read_slice(text)["text"] == text


def test_read_slice_ranged_read_keeps_crlf():
    payload = read_slice("a\r\nb\r\nc\r\nd\r\n", 2, 3)
    assert payload["text"] == "b\r\nc"
    assert (payload["start_line"], payload["end_line"]) == (2, 3)


def test_read_slice_line_numbers_are_flavor_independent():
    """Addressing stays normalized: the same read on LF and CRLF copies of one
    document must select the same lines, or every anchor workflow forks in two."""
    lf = read_slice("a\nb\nc\nd\n", 2, 3)
    crlf = read_slice("a\r\nb\r\nc\r\nd\r\n", 2, 3)
    assert lf["total_lines"] == crlf["total_lines"]
    assert (lf["start_line"], lf["end_line"]) == (crlf["start_line"], crlf["end_line"])
    assert lf["content_sha256"] == crlf["content_sha256"]  # the write-guard stamp


def test_read_slice_content_sha256_ignores_the_bom():
    """content_sha256 is the write-guard currency and must agree with
    manifest.text_sha256 and list_documents, which both drop the BOM. The BOM
    still rides in `text` - the hash is a stamp, not a description of the text."""
    with_bom = read_slice(BOM + "a\nb\n")
    without = read_slice("a\nb\n")
    assert with_bom["content_sha256"] == without["content_sha256"]
    assert with_bom["text"].startswith(BOM)


def test_read_slice_preserves_mixed_endings():
    text = "a\r\nb\nc\r\n"
    assert read_slice(text)["text"] == text


# --------------------------------------------------- the edit write-back

def test_an_edit_does_not_reflavour_the_rest_of_the_file():
    original = "one\r\ntwo\r\nthree\r\n"
    out = apply_edit(original, "two", "TWO")
    assert restore_line_endings(original, out.new_content) == "one\r\nTWO\r\nthree\r\n"


def test_an_edit_keeps_the_bom():
    original = BOM + "alpha\nbeta\n"
    out = apply_edit(original, "beta", "BETA")
    assert restore_line_endings(original, out.new_content) == BOM + "alpha\nBETA\n"


def test_an_edit_leaves_untouched_mixed_endings_alone():
    """Byte-exact away from the edit even where the file is inconsistent -
    a whole-file re-flavor would be the change nobody asked for."""
    original = "a\r\nb\nc\r\nd\n"
    out = apply_edit(original, "c", "C")
    assert restore_line_endings(original, out.new_content) == "a\r\nb\nC\r\nd\n"


def test_new_lines_take_the_files_dominant_ending():
    original = "a\r\nb\r\n"
    out = apply_edit(original, "b", "b\nextra")
    assert restore_line_endings(original, out.new_content) == "a\r\nb\r\nextra\r\n"


def test_an_lf_only_file_is_untouched_by_the_restore():
    original = "a\nb\n"
    out = apply_edit(original, "b", "B")
    assert restore_line_endings(original, out.new_content) == "a\nB\n"


def test_an_anchor_carrying_a_bom_still_matches():
    """read_document now returns line 1 with its BOM, so an anchor copied from
    it carries a character the caller cannot see. It must not cause not_found."""
    out = apply_edit(BOM + "alpha\nbeta\n", BOM + "alpha", "ALPHA")
    assert out.replacements == 1
    assert "ALPHA" in out.new_content
