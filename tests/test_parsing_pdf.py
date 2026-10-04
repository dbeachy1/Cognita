"""PDF extraction through pypdfium2 (DESIGN-14.0 §1, §8 "PDF").

Fixtures are minimal valid PDFs built in tmp_path by `build_pdf` (Helvetica, one
content stream per page, a correct xref). The one committed binary is the encrypted
fixture in tests/fixtures/pdf/. No network, no sleeps, no clock.
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

import pypdfium2 as pdfium
import pytest

from cognita.parsing import _extract_pdf, parse_file

FIXTURES = Path(__file__).parent / "fixtures" / "pdf"
ENCRYPTED = FIXTURES / "encrypted-user-password.pdf"


def build_pdf(pages: list[bytes | None]) -> bytes:
    """A valid PDF whose page N draws pages[N] (None = a page with no /Contents)."""
    objects: list[bytes] = []  # object number = index + 1

    def add(body: bytes) -> int:
        objects.append(body)
        return len(objects)

    add(b"<< /Type /Catalog /Pages 2 0 R >>")  # 1
    add(b"")  # 2, filled in once the page objects exist
    add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")  # 3
    kids = []
    for stream in pages:
        contents = b""
        if stream is not None:
            n = add(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream")
            contents = b" /Contents %d 0 R" % n
        kids.append(add(
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Resources << /Font << /F1 3 0 R >> >>" + contents + b" >>"
        ))
    kid_refs = b" ".join(b"%d 0 R" % k for k in kids)
    objects[1] = b"<< /Type /Pages /Kids [%s] /Count %d >>" % (kid_refs, len(kids))

    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + body + b"\nendobj\n"
    xref_at = len(out)
    out += b"xref\n0 %d\n" % (len(objects) + 1)
    out += b"0000000000 65535 f \n"
    for off in offsets:
        out += b"%010d 00000 n \n" % off
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objects) + 1, xref_at)
    return bytes(out)


def text_stream(*lines: str) -> bytes:
    """Content stream that draws the lines, one per T*."""
    body = b"BT /F1 12 Tf 14 TL 72 700 Td\n"
    for i, line in enumerate(lines):
        if i:
            body += b"T*\n"
        body += b"(" + line.encode("ascii") + b") Tj\n"
    return body + b"ET"


def write_pdf(tmp_path: Path, pages: list[bytes | None], name: str = "doc.pdf") -> Path:
    path = tmp_path / name
    path.write_bytes(build_pdf(pages))
    return path


def test_two_pages_exact_output(tmp_path):
    path = write_pdf(tmp_path, [text_stream("first page"), text_stream("second page")])
    assert _extract_pdf(path) == "[Page 1]\nfirst page\n\n[Page 2]\nsecond page"


def test_multiline_page_has_no_carriage_returns(tmp_path):
    path = write_pdf(tmp_path, [text_stream("line one", "line two", "line three")])
    out = _extract_pdf(path)
    assert "\r" not in out
    assert out == "[Page 1]\nline one\nline two\nline three"


def test_blank_page_is_skipped_and_numbering_is_kept(tmp_path):
    path = write_pdf(tmp_path, [text_stream("alpha"), None, text_stream("gamma")])
    out = _extract_pdf(path)
    assert out == "[Page 1]\nalpha\n\n[Page 3]\ngamma"
    assert "[Page 2]" not in out


def test_image_only_page_extracts_nothing_and_parse_file_returns_none(tmp_path):
    inline_image = b"q 100 0 0 100 72 600 cm BI /W 1 /H 1 /CS /G /BPC 8 ID \x80 EI Q"
    path = write_pdf(tmp_path, [inline_image])
    assert _extract_pdf(path) == ""
    assert parse_file(path, tmp_path) is None


def test_user_password_pdf_raises_value_error_with_pymupdf_message(tmp_path, caplog):
    with caplog.at_level(logging.WARNING, logger="cognita.parsing"):
        with pytest.raises(ValueError) as info:
            _extract_pdf(ENCRYPTED)
    assert str(info.value) == "document closed or encrypted"
    # Chained from the original PdfiumError, whose err_code is the password error.
    assert isinstance(info.value.__cause__, pdfium.PdfiumError)
    assert info.value.__cause__.err_code == pdfium.raw.FPDF_ERR_PASSWORD
    # One WARNING naming the file, the exception class and the code; never text.
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "encrypted-user-password.pdf" in warnings[0]
    assert "PdfiumError" in warnings[0]
    assert f"err_code={pdfium.raw.FPDF_ERR_PASSWORD}" in warnings[0]
    assert "cognita fixture" not in warnings[0]


def test_parse_file_propagates_the_encrypted_value_error(tmp_path):
    with pytest.raises(ValueError, match="document closed or encrypted"):
        parse_file(ENCRYPTED, ENCRYPTED.parent)


async def test_get_document_on_encrypted_pdf_answers_unsupported_format(tmp_path):
    # Reuses the protocol-level engine host of test_engine_local (no PostgreSQL is
    # touched: the error is returned before any store call).
    from test_engine_local import call, make_host

    docs = tmp_path / "docs"
    docs.mkdir()
    shutil.copy(ENCRYPTED, docs / "secret.pdf")
    host = make_host(tmp_path, docs, "PDFPROJ")
    payload = await call(host, "PDFPROJ", "get_document", {"filepath": "secret.pdf"})
    assert payload["status"] == "error"
    assert payload["reason"] == "unsupported_format"
    assert payload["message"] == "document closed or encrypted"


def test_corrupt_bytes_raise_runtime_error_not_value_error(tmp_path):
    path = tmp_path / "corrupt.pdf"
    path.write_bytes(b"this is not a pdf, just some bytes\n" * 20)
    with pytest.raises(RuntimeError) as info:
        _extract_pdf(path)
    assert not isinstance(info.value, ValueError)


def test_zero_byte_pdf_raises_runtime_error_not_value_error(tmp_path):
    path = tmp_path / "empty.pdf"
    path.write_bytes(b"")
    with pytest.raises(RuntimeError) as info:
        _extract_pdf(path)
    assert not isinstance(info.value, ValueError)


def test_zero_page_pdf_returns_empty_and_releases_the_file_handle(tmp_path):
    path = write_pdf(tmp_path, [])
    assert _extract_pdf(path) == ""
    assert parse_file(path, tmp_path) is None
    # On Windows an open handle blocks a rename or a delete, so this proves the
    # file object was closed on the zero-page path.
    renamed = path.rename(tmp_path / "renamed.pdf")
    renamed.unlink()
    assert not renamed.exists()


def test_zero_page_pdf_after_a_corrupt_one_still_returns_empty(tmp_path):
    """PDFium does not reset its last-error code on a successful load. On kei the
    zero-page test ran right after the corrupt-bytes test and was reported as the
    corrupt file's "data format error". The order is forced here, in one test."""
    corrupt = tmp_path / "corrupt.pdf"
    corrupt.write_bytes(b"%PDF-1.7\n not a pdf at all")
    with pytest.raises(RuntimeError):
        _extract_pdf(corrupt)
    assert _extract_pdf(write_pdf(tmp_path, [])) == ""


def test_missing_file_still_raises_file_not_found(tmp_path):
    with pytest.raises(FileNotFoundError):
        _extract_pdf(tmp_path / "nope.pdf")


@pytest.fixture
def closes(monkeypatch):
    """Record every close() on documents, pages and text pages (class level), and
    every object those classes hand out, calling the real close each time."""
    seen: dict[str, list] = {"doc": [], "page": [], "text": []}
    closed: dict[str, list] = {"doc": [], "page": [], "text": []}

    for label, cls in (("doc", pdfium.PdfDocument), ("page", pdfium.PdfPage),
                       ("text", pdfium.PdfTextPage)):
        real_init = cls.__init__
        real_close = cls.close

        def make(label=label, real_init=real_init, real_close=real_close):
            def init(self, *a, **kw):
                real_init(self, *a, **kw)
                seen[label].append(self)

            def close(self, *a, **kw):
                # PdfDocument.close() closes still-open children itself and passes
                # _by_parent=True; only a close that is NOT that counts, because the
                # code under test must close its own pages and text pages.
                if not kw.get("_by_parent"):
                    closed[label].append(self)
                return real_close(self, *a, **kw)
            return init, close

        init, close = make()
        monkeypatch.setattr(cls, "__init__", init)
        monkeypatch.setattr(cls, "close", close)
    return seen, closed


def _all_closed(seen, closed, label) -> bool:
    closed_ids = {id(o) for o in closed[label]}
    return bool(seen[label]) and all(id(o) in closed_ids for o in seen[label])


def test_every_document_page_and_text_page_is_closed_on_success(tmp_path, closes):
    seen, closed = closes
    path = write_pdf(tmp_path, [text_stream("one"), text_stream("two")])
    assert _extract_pdf(path) == "[Page 1]\none\n\n[Page 2]\ntwo"
    assert len(seen["doc"]) == 1 and len(seen["page"]) == 2 and len(seen["text"]) == 2
    for label in ("doc", "page", "text"):
        assert _all_closed(seen, closed, label), label


def test_every_object_is_closed_when_page_two_text_read_raises(tmp_path, closes, monkeypatch):
    seen, closed = closes
    path = write_pdf(tmp_path, [text_stream("one"), text_stream("two"), text_stream("three")])
    real = pdfium.PdfTextPage.get_text_bounded
    calls = []

    def flaky(self, *a, **kw):
        calls.append(1)
        if len(calls) == 2:
            raise RuntimeError("boom on page 2")
        return real(self, *a, **kw)

    monkeypatch.setattr(pdfium.PdfTextPage, "get_text_bounded", flaky)
    with pytest.raises(RuntimeError, match="boom on page 2"):
        _extract_pdf(path)
    assert len(calls) == 2  # page 3 was never read
    assert len(seen["doc"]) == 1
    assert len(seen["page"]) == 2 and len(seen["text"]) == 2
    for label in ("doc", "page", "text"):
        assert _all_closed(seen, closed, label), label
    # The lock is free again after the failure: a following call must not deadlock.
    monkeypatch.setattr(pdfium.PdfTextPage, "get_text_bounded", real)
    assert _extract_pdf(path).startswith("[Page 1]\none")


def test_nul_is_stripped_from_page_text(tmp_path, monkeypatch):
    path = write_pdf(tmp_path, [text_stream("abc")])
    monkeypatch.setattr(pdfium.PdfTextPage, "get_text_bounded",
                        lambda self, *a, **kw: "a\x00b\r\nc\rd")
    assert _extract_pdf(path) == "[Page 1]\nab\nc\nd"
