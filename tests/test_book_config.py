from __future__ import annotations

import pytest
from pydantic import ValidationError

from cognita.books.config import (
    BookBinding,
    BookLayout,
    ChapterState,
    FolderPolicyState,
    ProductionSettings,
    classify_book_config,
    parse_config,
    validate_book_layout,
)


def _layout() -> dict:
    return {
        "schema_version": 1,
        "layout_revision": 1,
        "book_id": "fixture-book",
        "title": "Fixture",
        "chapter_order": ["ch1"],
        "chapters": [{
            "chapter_id": "ch1",
            "title": "Chapter 1",
            "chapter_state_filepath": "Chapters/1/chapter.json",
            "working_filepath": "Chapters/1/chapter.docx",
            "tagged_filepath": "Chapters/1/chapter_audio-tags.docx",
            "summary_filepath": None,
            "originals_root": "Chapters/1/Originals",
            "audio_root": "Audiobook/Chapters/1",
        }],
        "indexed_references": [{"filepath": "Project Files/ref.docx", "role": "reference"}],
        "indexed_instructions": [{"filepath": "Project Files/guide.md", "role": "instructions"}],
        "indexed_workflow_documents": [{"filepath": "Project Files/workflow.docx", "role": "workflow"}],
        "index_policy": {
            "default_unknown": "exclude",
            "tagged_copies": "exclude",
            "archives": "exclude",
            "media": "exclude",
            "duplicate_prose": "single_active_source",
        },
        "source_master_filepath": "Project Files/Source/Version1.docx",
        "shared_paths": {
            "production_settings_filepath": "Project Files/production-settings.json",
            "book_audio_root": "Audiobook",
        },
        "storage": {
            "quota_bytes": 2_000_000_000,
            "reserve_bytes": 100_000_000,
            "import_https_hosts": ["media.example.test"],
        },
        "production_authorization": None,
        "test_authorizations": [],
    }


def test_book_layout_uses_required_nullable_top_level_source_master():
    layout = validate_book_layout(_layout())
    assert layout.source_master_filepath == "Project Files/Source/Version1.docx"
    assert "source_master_filepath" in BookLayout.model_json_schema()["required"]
    assert "master_manuscript_filepath" not in str(BookLayout.model_json_schema())
    with pytest.raises(ValidationError):
        validate_book_layout({k: v for k, v in _layout().items() if k != "source_master_filepath"})


def test_layout_rejects_unknown_fields_invalid_versions_duplicate_ids_and_path_traversal():
    with pytest.raises(ValidationError):
        validate_book_layout({**_layout(), "future_field": True})
    with pytest.raises(ValidationError):
        validate_book_layout({**_layout(), "schema_version": 2})
    duplicate = _layout()
    duplicate["chapter_order"] = ["ch1", "ch1"]
    with pytest.raises(ValidationError):
        validate_book_layout(duplicate)
    escaped = _layout()
    escaped["chapters"][0]["working_filepath"] = "../outside.docx"
    with pytest.raises(ValidationError):
        validate_book_layout(escaped)


def test_configuration_json_rejects_duplicate_keys_and_nonstandard_numbers():
    with pytest.raises(ValueError, match="duplicate"):
        parse_config(BookBinding, '{"schema_version":1,"schema_version":1}')
    with pytest.raises(ValueError, match="invalid JSON constant"):
        parse_config(FolderPolicyState, '{"policy_revision":NaN,"rules":[]}')


def test_chapter_state_and_settings_preserve_explicit_null_fields():
    state = {
        "schema_version": 1,
        "chapter_id": "ch1",
        "layout_revision": 1,
        "state_revision": 1,
        "editorial_status": "draft",
        "approval_binding": "prose_projection",
        "approved_source_raw_sha256": None,
        "approved_prose_projection_sha256": None,
        "approval_projection_version": None,
        "approval_provenance": None,
        "summary": None,
        "index_annotations": None,
    }
    assert ChapterState.model_validate(state, strict=True).editorial_status == "draft"
    settings = {
        "schema_version": 1,
        "target_codepoints": 5000,
        "request_limit": {"value": 5000, "unit": "unicode_codepoints", "evidence": "fixture"},
        "request_spec": None,
        "production_target": None,
        "native_format_evidence": None,
    }
    assert ProductionSettings.model_validate(settings, strict=True).request_spec is None


def test_binding_has_a_fixed_discoverable_layout_and_state_root():
    binding = BookBinding.model_validate({
        "schema_version": 1,
        "book_id": "fixture-book",
        "layout_filepath": "Project Files/Book_Layout.json",
        "state_root": ".cognita-storage",
    }, strict=True)
    assert binding.state_root == ".cognita-storage"
    with pytest.raises(ValidationError):
        BookBinding.model_validate({**binding.model_dump(), "state_root": "Audiobook/state"})


def test_config_classification_distinguishes_pristine_bootstrap_and_damage():
    classify = classify_book_config
    assert classify(
        binding_present=False, layout_present=False, binding_valid=False, layout_valid=False
    ) == "never_enabled"
    assert classify(
        binding_present=False, layout_present=True, binding_valid=False, layout_valid=True
    ) == "bootstrap_pending"
    assert classify(
        binding_present=True, layout_present=False, binding_valid=True, layout_valid=False
    ) == "configuration_conflict"
    assert classify(
        binding_present=True, layout_present=True, binding_valid=True, layout_valid=True,
        binding_book_id="a", layout_book_id="b",
        binding_layout_filepath="Project Files/Book_Layout.json",
    ) == "configuration_conflict"
    assert classify(
        binding_present=True, layout_present=True, binding_valid=True, layout_valid=True,
        binding_book_id="a", layout_book_id="a",
        binding_layout_filepath="Project Files/Book_Layout.json",
    ) == "enabled"
