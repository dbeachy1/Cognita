from __future__ import annotations

import json

import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from cognita.books import (
    BOOK_OUTPUT_SCHEMAS,
    BOOK_TOOL_DEFS,
    BOOK_MUTATING_TOOLS,
    BOOK_TOOL_NAMES,
    InspectRequest,
    ListProjectFilesRequest,
    ReadProjectFileRequest,
    RequestSpec,
    SetFolderIndexingRequest,
    canonical_json_sha256,
    error_envelope,
    grapheme_boundaries,
    grapheme_spans,
    request_fingerprint,
    success_envelope,
    validate_grapheme_boundary,
)
from cognita.books.schemas import (
    BOOK_REQUEST_SCHEMAS,
    BOOK_RESULT_SCHEMAS,
    published_schema_document,
    validate_published_schema_document,
)


def test_all_normative_tools_have_strict_generated_request_and_result_schemas():
    assert len(BOOK_TOOL_NAMES) == 13
    assert tuple(BOOK_REQUEST_SCHEMAS) == BOOK_TOOL_NAMES
    assert tuple(BOOK_RESULT_SCHEMAS) == BOOK_TOOL_NAMES
    assert BOOK_MUTATING_TOOLS == {
        "audiobook_prepare_chapter",
        "audiobook_record_generation",
        "audiobook_import_audio",
        "audiobook_build",
        "audiobook_commit_build",
        "audiobook_cancel_job",
    }
    for schema in (*BOOK_REQUEST_SCHEMAS.values(), *BOOK_RESULT_SCHEMAS.values()):
        Draft202012Validator.check_schema(schema)
        assert schema["type"] == "object"
        assert schema["additionalProperties"] is False


def test_published_schema_artifact_is_generated_from_the_dto_registry():
    validate_published_schema_document()
    serialized = json.dumps(published_schema_document(), sort_keys=True, separators=(",", ":"))
    assert len(serialized.encode("utf-8")) < 2_000_000


def test_catalog_definitions_have_strict_success_and_error_envelopes():
    assert tuple(item["name"] for item in BOOK_TOOL_DEFS) == BOOK_TOOL_NAMES
    for name, schema in BOOK_OUTPUT_SCHEMAS.items():
        Draft202012Validator.check_schema(schema)
        assert len(schema["oneOf"]) == 2
        assert schema["oneOf"][0]["additionalProperties"] is False
        assert schema["oneOf"][1]["additionalProperties"] is False
        assert "outcome_unknown" in schema["oneOf"][1]["properties"]["operation_outcome"]["enum"]
    success = success_envelope(
        "set_folder_indexing",
        {"path": "", "indexed": False, "policy_revision": 1, "job_id": None},
        operation_id="op-1",
    )
    assert success["operation_id"] == "op-1"
    failure = error_envelope(
        "set_folder_indexing", reason="stale_manifest", message="stale",
        operation_outcome="not_applied", correlation_id="c1",
        details={"current_revision": 4},
    )
    assert failure["details"] == {"current_revision": 4}


def test_request_dto_rejects_unknown_keys_wrong_types_and_null_for_omittable_fields():
    good = {
        "project": "fixture",
        "chapter_id": "chapter-1",
        "prose_filepath": "Chapters/1/prose.docx",
        "tagged_filepath": "Chapters/1/tagged.docx",
    }
    assert InspectRequest.model_validate(good, strict=True).project == "fixture"
    for bad in (
        {**good, "unexpected": True},
        {**good, "max_characters": True},
        {**good, "base_document_view_id": None},
    ):
        with pytest.raises(ValidationError):
            InspectRequest.model_validate(bad, strict=True)


def test_json_object_is_the_only_open_record_and_preserves_exact_request_fields():
    spec = RequestSpec.model_validate({
        "provider": "provider",
        "route": "voice-route",
        "model_id": "model-a",
        "voice_id": "voice-a",
        "parameters": {"speed": 1.0, "nested": {"flag": True}},
        "context_fields": {"seed": "A"},
    }, strict=True)
    assert spec.parameters["nested"] == {"flag": True}
    with pytest.raises(ValidationError):
        RequestSpec.model_validate({
            **spec.model_dump(), "surprise": "not allowed",
        }, strict=True)


def test_request_fingerprint_is_stable_for_jcs_and_changes_with_actual_context():
    # This canonical byte sequence is the RFC 8785 package's documented example.
    value = {
        "key": "value",
        "another-key": 2,
        "a-third": [1, 2, 3, [4], [5, 6, "this works too"]],
        "more": [None, True, False],
    }
    assert canonical_json_sha256(value) == (
        "5c501fde87caad7c535344b17a3c460c85867fef1939e0e362251ca7b6178275"
    )
    spec = RequestSpec(
        provider="p", route="r", model_id="m", voice_id="v",
        parameters={"speed": 1}, context_fields={},
    )
    fingerprint = request_fingerprint("Hello 👋", spec)
    assert fingerprint == "42467fefefefe269d5e4b1cd6e0ad706e978c467bb047a31ba9ed373331931b7"
    assert request_fingerprint("Hello 👋", spec) == fingerprint
    changed = spec.model_copy(update={"context_fields": {"language": "en"}})
    assert request_fingerprint("Hello 👋", changed) != fingerprint
    with pytest.raises(UnicodeEncodeError):
        request_fingerprint("lone surrogate: \ud800", spec)


def test_maintained_grapheme_segmentation_rejects_mid_cluster_offsets():
    text = "Ame\u0301lie 👩\u200d🔬!"
    spans = grapheme_spans(text)
    boundaries = grapheme_boundaries(text)
    assert spans == ((0, 1), (1, 2), (2, 4), (4, 5), (5, 6), (6, 7), (7, 8), (8, 11), (11, 12))
    assert boundaries == frozenset({0, 1, 2, 4, 5, 6, 7, 8, 11, 12})
    validate_grapheme_boundary(text, 4)
    with pytest.raises(ValueError, match="grapheme"):
        validate_grapheme_boundary(text, 3)


def test_general_storage_requests_enforce_project_relative_paths_and_byte_range_guards():
    assert ListProjectFilesRequest(project="fixture", path="").path == ""
    with pytest.raises(ValidationError):
        ListProjectFilesRequest(project="fixture", path="folder/../private")
    with pytest.raises(ValidationError):
        ListProjectFilesRequest(project="fixture", path="folder", limit=501)
    with pytest.raises(ValidationError, match="expected_bytes_sha256"):
        ReadProjectFileRequest(project="fixture", path="excluded/audio.bin", offset=1)
    ReadProjectFileRequest(
        project="fixture", path="excluded/audio.bin", offset=1,
        expected_bytes_sha256="a" * 64, max_bytes=1_048_576,
    )
    with pytest.raises(ValidationError):
        ReadProjectFileRequest(
            project="fixture", path="excluded/audio.bin", max_bytes=1_048_577
        )
    with pytest.raises(ValidationError):
        SetFolderIndexingRequest(
            project="fixture", path="../outside", indexed=False, operation_id="op",
            expected_policy_revision=0,
        )
