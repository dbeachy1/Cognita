"""Pure contract tests for the additive PNG OCR persistence layer."""

import pytest

from cognita.assets.ocr_store import (
    OcrResultFacts,
    OcrSearchChunk,
    OcrSourceSnapshot,
    OcrStore,
    normalize_languages_key,
    ocr_ddl,
)


DIGEST = "a" * 64


def test_language_identity_is_project_local_and_order_stable():
    assert normalize_languages_key(["EN", "fr"]) == "en,fr"
    assert normalize_languages_key(["fr", "en"]) == "en,fr"
    with pytest.raises(ValueError):
        normalize_languages_key(["en", "en"])


def test_facts_normalize_text_and_retain_bounded_json():
    result = OcrResultFacts(
        DIGEST, "b" * 64, ["en"], "easyocr", "1.7.2", "c" * 64, 1,
        640, 480, "text", "Cafe\u0301\r\n",
        [{"text": "Cafe", "order": 0}], [{"code": "low_confidence", "message": "check"}],
    )
    assert result.languages_key == "en"
    assert result.text == "Café\n"
    assert isinstance(result.regions, tuple)
    assert isinstance(result.warnings, tuple)


def test_no_text_result_cannot_publish_searchable_text():
    with pytest.raises(ValueError):
        OcrResultFacts(
            DIGEST, "b" * 64, "en", "easyocr", "1.7.2", "c" * 64, 1,
            1, 1, "no_text", "unexpected text",
        )


def test_source_and_chunks_validate_snapshot_contract():
    snapshot = OcrSourceSnapshot("screens/example.png", DIGEST, 100, 20, 10, "stat-token")
    result = OcrResultFacts(
        DIGEST, "b" * 64, "en", "easyocr", "1.7.2", "c" * 64, 1,
        20, 10, "text", "token",
    )
    # A searchable result requires vectors, but validation occurs before SQL.
    chunk = OcrSearchChunk(0, "token", [0.1, 0.2])
    store = OcrStore(None, "KEI", dimensions=2)
    store._validate_publication(snapshot, result, [chunk])
    with pytest.raises(ValueError):
        store._validate_publication(snapshot, result, [OcrSearchChunk(1, "token", [0.1, 0.2])])


def test_ddl_is_additive_and_project_qualified():
    # Renamed from ..._separately_versioned_... in 13.0: the OCR tables are
    # still additive, but they no longer carry a version of their own (§4.1).
    ddl = ocr_ddl("KEI", dimensions=8)
    assert '"proj_KEI".asset_ocr_results' in ddl
    assert '"proj_KEI".asset_ocr_sources' in ddl
    assert '"proj_KEI".asset_ocr_chunks' in ddl
    assert "asset_ocr_schema_meta" not in ddl
    assert "schema_version" not in ddl
    assert "PRIMARY KEY (source_sha256, pipeline_fingerprint, languages_key)" in ddl
    assert "ON DELETE CASCADE" in ddl
    assert "vector(8)" in ddl
