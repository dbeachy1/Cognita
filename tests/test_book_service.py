from __future__ import annotations

import hashlib
import io
import json
import struct
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from cognita.books.models import (
    CancelJobRequest,
    BuildRequest,
    CommitBuildRequest,
    GetGenerationsRequest,
    GetChapterRequest,
    GetBookRequest,
    FindChunkRequest,
    GetJobRequest,
    ImportAudioRequest,
    IndexStatusRequest,
    InspectRequest,
    PrepareRequest,
    RecordGenerationRequest,
)
import cognita.books.service as service_module
from cognita.books.service import BookService, BookServiceError
from cognita.books.config import BookLayout
from cognita.books.state import ProjectState, ProjectStateError
from cognita.books.media import inspect_media_file
from cognita.books.docx import FileLockedError, parse_docx
from cognita.books.projection import project_docx_pair
from cognita.books.jobs import PacketFact
from cognita.books.fingerprint import canonical_json_sha256
import cognita.books.service as service_module


W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def _docx(text: str) -> bytes:
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<w:document xmlns:w="{W}"><w:body><w:p><w:r><w:t>{text}</w:t></w:r>'
        '</w:p><w:sectPr/></w:body></w:document>'
    ).encode()
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", b'<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>')
        archive.writestr("word/document.xml", xml)
    return output.getvalue()


def _tagged_docx_with_audio_tag() -> bytes:
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<w:document xmlns:w="{W}"><w:body><w:p>'
        '<w:r><w:t>repeat </w:t></w:r>'
        '<w:r><w:rPr><w:rStyle w:val="CognitaAudioTag"/></w:rPr><w:t>[tag]</w:t></w:r>'
        '<w:r><w:t> repeat</w:t></w:r>'
        '</w:p><w:sectPr/></w:body></w:document>'
    ).encode()
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", b'<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>')
        archive.writestr("word/document.xml", xml)
        archive.writestr("word/styles.xml", f'<w:styles xmlns:w="{W}"><w:style w:type="character" w:styleId="CognitaAudioTag"/></w:styles>')
    return output.getvalue()


def _wav_pcm(samples: bytes, *, channels: int = 1, rate: int = 8000) -> bytes:
    """Small native WAVE fixture; the service must retain these bytes verbatim."""
    bits = 16
    frame_bytes = channels * bits // 8
    fmt = struct.pack("<HHIIHH", 1, channels, rate, rate * frame_bytes, frame_bytes, bits)
    body = b"WAVE" + b"fmt " + struct.pack("<I", len(fmt)) + fmt
    body += b"data" + struct.pack("<I", len(samples)) + samples
    if len(samples) & 1:
        body += b"\x00"
    return b"RIFF" + struct.pack("<I", len(body)) + body


def _fixture(root, *, bound: bool = False) -> tuple[BookService, bytes, bytes]:
    prose = _docx("hello")
    tagged = _docx("hello")
    (root / "Chapters/1").mkdir(parents=True)
    (root / "Project Files/Source").mkdir(parents=True)
    (root / "Project Files/Source/Version1.docx").write_bytes(prose)
    (root / "Project Files/ref.md").write_text("Reference facts", encoding="utf-8")
    (root / "Project Files/guide.md").write_text("Instructions", encoding="utf-8")
    (root / "Project Files/workflow.md").write_text("Workflow", encoding="utf-8")
    (root / "Chapters/1/chapter.docx").write_bytes(prose)
    (root / "Chapters/1/chapter_audio-tags.docx").write_bytes(tagged)
    chapter_state = {
        "schema_version": 1, "chapter_id": "ch1", "layout_revision": 1,
        "state_revision": 1, "editorial_status": "draft",
        "approval_binding": "prose_projection", "approved_source_raw_sha256": None,
        "approved_prose_projection_sha256": None, "approval_projection_version": None,
        "approval_provenance": None, "summary": None, "index_annotations": None,
    }
    (root / "Chapters/1/chapter.json").write_text(json.dumps(chapter_state), encoding="utf-8")
    now = datetime.now(timezone.utc)
    authorization = {
        "authorization_id": "test-auth", "chapter_id": "ch1",
        "prose_filepath": "Chapters/1/chapter.docx",
        "tagged_filepath": "Chapters/1/chapter_audio-tags.docx",
        "allowed_paragraph_ordinals": [0],
        "source_raw_sha256": hashlib.sha256(prose).hexdigest(),
        "actor": "fixture", "authorized_at": (now - timedelta(minutes=1)).isoformat(),
        "expires_at": (now + timedelta(hours=1)).isoformat(), "revoked": False,
    }
    layout = {
        "schema_version": 1, "layout_revision": 1, "book_id": "fixture-book",
        "title": "Fixture", "chapter_order": ["ch1"],
        "chapters": [{
            "chapter_id": "ch1", "title": "Chapter 1",
            "chapter_state_filepath": "Chapters/1/chapter.json",
            "working_filepath": "Chapters/1/chapter.docx",
            "tagged_filepath": "Chapters/1/chapter_audio-tags.docx",
            "summary_filepath": None, "originals_root": "Chapters/1/Originals",
            "audio_root": "Audiobook/Chapters/1",
        }],
        "indexed_references": [{"filepath": "Project Files/ref.md", "role": "reference"}],
        "indexed_instructions": [{"filepath": "Project Files/guide.md", "role": "instructions"}],
        "indexed_workflow_documents": [{"filepath": "Project Files/workflow.md", "role": "workflow"}],
        "index_policy": {
            "default_unknown": "exclude", "tagged_copies": "exclude", "archives": "exclude",
            "media": "exclude", "duplicate_prose": "single_active_source",
        },
        "source_master_filepath": "Project Files/Source/Version1.docx",
        "shared_paths": {"production_settings_filepath": "Project Files/production-settings.json", "book_audio_root": "Audiobook"},
        "storage": {"quota_bytes": 2_000_000_000, "reserve_bytes": 100_000_000, "import_https_hosts": []},
        "production_authorization": None, "test_authorizations": [authorization],
    }
    (root / "Project Files/Book_Layout.json").write_text(json.dumps(layout), encoding="utf-8")
    if bound:
        (root / ".cognita-book-binding.json").write_text(json.dumps({
            "schema_version": 1, "book_id": "fixture-book",
            "layout_filepath": "Project Files/Book_Layout.json", "state_root": ".cognita-storage",
        }), encoding="utf-8")
    return BookService(root, "fixture"), prose, tagged


def _inspect(service: BookService) -> dict:
    request = InspectRequest.model_validate({
        "project": "fixture", "chapter_id": "ch1",
        "prose_filepath": "Chapters/1/chapter.docx",
        "tagged_filepath": "Chapters/1/chapter_audio-tags.docx",
    })
    return service.inspect(request)


def _prepare_test_plan(service, operation_id, prose, tagged, expected_revision, chunks, *, publish=False, authorization_id="test-auth"):
    inspected = _inspect(service)
    request = PrepareRequest.model_validate({
        "project": "fixture", "operation_id": operation_id, "chapter_id": "ch1",
        "document_view_id": inspected["document_view_id"],
        "expected_prose_sha256": hashlib.sha256(prose).hexdigest(),
        "expected_tagged_sha256": hashlib.sha256(tagged).hexdigest(),
        "expected_manifest_revision": expected_revision,
        "scope": {"kind": "test", "authorization_id": authorization_id},
        "speech_selection_confirmed": True,
        "request_limit": {"value": 100, "unit": "unicode_codepoints"},
        "expected_settings_sha256": None, "production_target": None,
        "chunks": chunks, "publish_bookmarks_to_working_tagged_docx": publish,
    })
    return service.prepare(request, owner_key="principal:fixture")[0]


def _import_native_take(service, prepared, chunk_id, *, operation_prefix):
    chunk = next(item for item in prepared["chunks"] if item["chunk_id"] == chunk_id)
    spec = chunk["request_spec"]
    reserved, _ = service.record_generation(RecordGenerationRequest.model_validate({
        "project": "fixture", "operation_id": f"{operation_prefix}-reserve", "change": {
            "kind": "reserve", "chapter_id": "ch1", "snapshot_id": prepared["snapshot_id"],
            "chunk_id": chunk_id, "expected_manifest_revision": prepared["manifest_revision"],
            "request": {"prompt_sha256": chunk["prompt_sha256"], "spec": spec},
        },
    }), owner_key="principal:fixture")
    generation_id = reserved["generation"]["generation_record_id"]
    submitted, _ = service.record_generation(RecordGenerationRequest.model_validate({
        "project": "fixture", "operation_id": f"{operation_prefix}-submit", "change": {
            "kind": "update", "generation_record_id": generation_id, "expected_generation_revision": 1,
            "state": "submitted", "provider_ids": {"generation_ids": [f"{operation_prefix}-provider"]},
            "provider_response_metadata": {"format": "synthetic local fixture"},
        },
    }), owner_key="principal:fixture")
    service.record_generation(RecordGenerationRequest.model_validate({
        "project": "fixture", "operation_id": f"{operation_prefix}-complete", "change": {
            "kind": "update", "generation_record_id": generation_id,
            "expected_generation_revision": submitted["generation"]["generation_revision"],
            "state": "completed", "provider_ids": {"generation_ids": [f"{operation_prefix}-provider"]},
        },
    }), owner_key="principal:fixture")
    samples = b"\x00\x00\x01\x00\xff\xff\x02\x00"
    relative = f"Audiobook/Chapters/1/{operation_prefix}.pcm"
    source = service.root / relative
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(samples)
    imported, _ = service.import_audio(ImportAudioRequest.model_validate({
        "project": "fixture", "operation_id": f"{operation_prefix}-import",
        "generation_record_id": generation_id,
        "expected_generation_revision": submitted["generation"]["generation_revision"] + 1,
        "source": {"kind": "project_file", "filepath": relative,
                   "expected_sha256": hashlib.sha256(samples).hexdigest()},
        "provenance": "native_generation",
        "source_format": {
            "container": "raw_pcm", "encoding": "signed_integer", "sample_rate_hz": 8000,
            "channels": 1, "storage_bits": 16, "valid_bits": 16, "endianness": "little",
            "interleaving": "interleaved", "provider_format_evidence": "synthetic local fixture",
        },
    }), owner_key="principal:fixture")
    service.run_import_job(imported["job_id"])
    return service.get_job(GetJobRequest(project="fixture", job_id=imported["job_id"]))["result"]["take"]


def test_chunk_lineage_split_merge_keeps_later_ids_and_permanently_retires_ids(tmp_path):
    service, prose, tagged = _fixture(tmp_path)
    first = _prepare_test_plan(service, "lineage-first", prose, tagged, None, [
        {"chunk_id": "a", "start": 0, "end": 1, "request_spec": None},
        {"chunk_id": "b", "start": 1, "end": 3, "request_spec": None},
        {"chunk_id": "c", "start": 3, "end": 4, "request_spec": None},
        {"chunk_id": "d", "start": 4, "end": 5, "request_spec": None},
    ])
    split = _prepare_test_plan(service, "lineage-split", prose, tagged, first["manifest_revision"], [
        {"chunk_id": "a", "start": 0, "end": 1, "request_spec": None},
        {"chunk_id": "b-left", "start": 1, "end": 2, "replaces_chunk_ids": ["b"], "request_spec": None},
        {"chunk_id": "b-right", "start": 2, "end": 3, "replaces_chunk_ids": ["b"], "request_spec": None},
        {"chunk_id": "c", "start": 3, "end": 4, "request_spec": None},
        {"chunk_id": "d", "start": 4, "end": 5, "request_spec": None},
    ])
    assert split["retired_chunk_ids"] == ["b"]
    assert [item["chunk_id"] for item in split["chunks"]][-2:] == ["c", "d"]
    merge = _prepare_test_plan(service, "lineage-merge", prose, tagged, split["manifest_revision"], [
        {"chunk_id": "a", "start": 0, "end": 1, "request_spec": None},
        {"chunk_id": "b-merged", "start": 1, "end": 3,
         "replaces_chunk_ids": ["b-left", "b-right"], "request_spec": None},
        {"chunk_id": "c", "start": 3, "end": 4, "request_spec": None},
        {"chunk_id": "d", "start": 4, "end": 5, "request_spec": None},
    ])
    state = ProjectState.discover(tmp_path)
    assert state is not None
    lineage = {item["chunk_id"]: item for item in state.chunk_lineage(
        chapter_id="ch1", scope_key='{"kind":"test","authorization_id":"test-auth"}')}
    assert lineage["b"]["retired"] is True
    assert lineage["b"]["replaced_by_chunk_ids"] == ["b-left", "b-right"]
    assert lineage["b-left"]["replaced_by_chunk_ids"] == ["b-merged"]
    assert lineage["d"]["retired"] is False
    with pytest.raises(BookServiceError) as recycled:
        _prepare_test_plan(service, "lineage-recycled", prose, tagged, merge["manifest_revision"], [
            {"chunk_id": "a", "start": 0, "end": 1, "request_spec": None},
            {"chunk_id": "b", "start": 1, "end": 3, "request_spec": None},
            {"chunk_id": "c", "start": 3, "end": 4, "request_spec": None},
            {"chunk_id": "d", "start": 4, "end": 5, "request_spec": None},
        ])
    assert recycled.value.reason == "duplicate_or_recycled_chunk_id"


def test_one_chunk_retakes_while_exact_unchanged_chunk_reuses_take_after_tagged_refresh(tmp_path):
    service, prose, tagged = _fixture(tmp_path)
    voice_a = {"provider": "synthetic", "route": "fixture", "model_id": "model",
               "voice_id": "voice-a", "parameters": {}, "context_fields": {"language": "en"}}
    voice_b = {**voice_a, "voice_id": "voice-b"}
    first = _prepare_test_plan(service, "retake-first", prose, tagged, None, [
        {"chunk_id": "opening", "start": 0, "end": 2, "request_spec": voice_a},
        {"chunk_id": "ending", "start": 2, "end": 5, "request_spec": voice_a},
    ])
    take = _import_native_take(service, first, "ending", operation_prefix="ending-take")
    tagged_refresh = tagged.replace(b"<w:p>", b'<w:p w:rsidR="00000001">')
    (tmp_path / "Chapters/1/chapter_audio-tags.docx").write_bytes(tagged_refresh)
    second = _prepare_test_plan(service, "retake-second", prose, tagged_refresh,
                                first["manifest_revision"], [
        {"chunk_id": "opening", "start": 0, "end": 2, "request_spec": voice_b},
        {"chunk_id": "ending", "start": 2, "end": 5, "request_spec": voice_a},
    ])
    chunks = {item["chunk_id"]: item for item in second["chunks"]}
    assert chunks["opening"]["reuse_status"] == "changed"
    assert chunks["opening"]["reusable_take_ids"] == []
    assert chunks["ending"]["reuse_status"] == "reusable"
    assert chunks["ending"]["reusable_take_ids"] == [take["take_id"]]
    assert chunks["ending"]["accepted_take_id"] is None


def test_duplicate_and_cross_namespace_chunk_take_reuse_are_rejected(tmp_path):
    service, prose, tagged = _fixture(tmp_path)
    duplicate = [
        {"chunk_id": "same", "start": 0, "end": 2, "request_spec": None},
        {"chunk_id": "same", "start": 2, "end": 5, "request_spec": None},
    ]
    with pytest.raises(BookServiceError) as duplicate_error:
        _prepare_test_plan(service, "duplicate-chunks", prose, tagged, None, duplicate)
    assert duplicate_error.value.reason == "duplicate_or_recycled_chunk_id"

    spec = {"provider": "synthetic", "route": "fixture", "model_id": "model",
            "voice_id": "voice", "parameters": {}, "context_fields": {}}
    first = _prepare_test_plan(service, "namespace-first", prose, tagged, None, [
        {"chunk_id": "shared-name", "start": 0, "end": 5, "request_spec": spec},
    ])
    take = _import_native_take(service, first, "shared-name", operation_prefix="namespace-take")
    layout_path = tmp_path / "Project Files/Book_Layout.json"
    layout = json.loads(layout_path.read_text(encoding="utf-8"))
    layout["test_authorizations"].append({
        **layout["test_authorizations"][0], "authorization_id": "test-auth-second",
    })
    layout_path.write_text(json.dumps(layout), encoding="utf-8")
    second = _prepare_test_plan(service, "namespace-second", prose, tagged, None, [
        {"chunk_id": "shared-name", "start": 0, "end": 5, "request_spec": spec},
    ], authorization_id="test-auth-second")
    assert second["chunks"][0]["reusable_take_ids"] == []
    assert take["namespace"] == {"kind": "test", "authorization_id": "test-auth"}


def test_prepare_bookmarks_snapshot_and_guarded_working_publication(tmp_path):
    service, prose, tagged = _fixture(tmp_path)
    result = _prepare_test_plan(service, "bookmark-prepare", prose, tagged, None, [
        {"chunk_id": "part", "start": 0, "end": 5, "request_spec": None},
    ], publish=True)
    working = (tmp_path / "Chapters/1/chapter_audio-tags.docx").read_bytes()
    assert result["working_tagged_updated"] is True
    assert result["input_tagged_sha256"] == hashlib.sha256(tagged).hexdigest()
    assert result["snapshot_tagged_sha256"] == hashlib.sha256(working).hexdigest()
    assert result["snapshot_tagged_sha256"] != result["input_tagged_sha256"]
    assert len(parse_docx(working).bookmarks) == 1
    state = ProjectState.discover(tmp_path)
    assert state is not None
    snapshot = state.snapshot(result["snapshot_id"])
    assert snapshot is not None
    assert (tmp_path / snapshot["payload"]["input_tagged_filepath"]).read_bytes() == tagged
    assert state.publications("chapter_bookmark_prepare") == []


def test_locked_working_tagged_file_fails_before_snapshot_artifacts(tmp_path, monkeypatch):
    service, prose, tagged = _fixture(tmp_path)

    def locked(_path):
        raise FileLockedError("fixture lock")

    monkeypatch.setattr(service_module, "require_unlocked", locked)
    with pytest.raises(BookServiceError) as failure:
        _prepare_test_plan(service, "bookmark-locked", prose, tagged, None, [
            {"chunk_id": "part", "start": 0, "end": 5, "request_spec": None},
        ], publish=True)
    assert failure.value.reason == "file_locked"
    assert not (tmp_path / service_module.STATE_ROOT / "snapshots").exists()
    assert (tmp_path / "Chapters/1/chapter_audio-tags.docx").read_bytes() == tagged


def test_prepare_bookmark_false_keeps_working_bytes_unchanged(tmp_path):
    service, prose, tagged = _fixture(tmp_path)
    before = (tmp_path / "Chapters/1/chapter_audio-tags.docx").read_bytes()
    result = _prepare_test_plan(service, "bookmark-no-publish", prose, tagged, None, [
        {"chunk_id": "part", "start": 0, "end": 5, "request_spec": None},
    ])
    assert result["working_tagged_updated"] is False
    assert (tmp_path / "Chapters/1/chapter_audio-tags.docx").read_bytes() == before
    assert result["snapshot_tagged_sha256"] != result["input_tagged_sha256"]
    state = ProjectState.discover(tmp_path)
    assert state is not None
    snapshot = state.snapshot(result["snapshot_id"])
    assert snapshot is not None
    frozen_tagged = (tmp_path / snapshot["payload"]["tagged_filepath"]).read_bytes()
    assert parse_docx(frozen_tagged).bookmarks[0].name == result["chunks"][0]["bookmark"]


def test_bookmark_publication_restart_restores_uncommitted_owned_bytes(tmp_path, monkeypatch):
    service, prose, tagged = _fixture(tmp_path)
    initial = _prepare_test_plan(service, "bookmark-before", prose, tagged, None, [
        {"chunk_id": "part", "start": 0, "end": 5, "request_spec": None},
    ])
    inspect_before = _inspect(service)
    original_commit = service._state_required().commit_snapshot

    def fail_commit(**_kwargs):
        raise ProjectStateError("stale_manifest")

    monkeypatch.setattr(service._state_required(), "commit_snapshot", fail_commit)
    monkeypatch.setattr(service, "recover_bookmark_publications", lambda: None)
    request = PrepareRequest.model_validate({
        "project": "fixture", "operation_id": "bookmark-interrupted", "chapter_id": "ch1",
        "document_view_id": inspect_before["document_view_id"],
        "expected_prose_sha256": hashlib.sha256(prose).hexdigest(),
        "expected_tagged_sha256": hashlib.sha256(tagged).hexdigest(),
        "expected_manifest_revision": initial["manifest_revision"],
        "scope": {"kind": "test", "authorization_id": "test-auth"},
        "speech_selection_confirmed": True, "request_limit": {"value": 100, "unit": "unicode_codepoints"},
        "expected_settings_sha256": None, "production_target": None,
        "chunks": [{"chunk_id": "part", "start": 0, "end": 5, "request_spec": None}],
        "publish_bookmarks_to_working_tagged_docx": True,
    })
    with pytest.raises(BookServiceError):
        service.prepare(request, owner_key="principal:fixture")
    published = (tmp_path / "Chapters/1/chapter_audio-tags.docx").read_bytes()
    assert hashlib.sha256(published).hexdigest() != hashlib.sha256(tagged).hexdigest()
    restarted = BookService(tmp_path, "fixture")
    restarted.recover_bookmark_publications()
    assert (tmp_path / "Chapters/1/chapter_audio-tags.docx").read_bytes() == tagged
    assert ProjectState.discover(tmp_path).publications("chapter_bookmark_prepare") == []
    monkeypatch.setattr(service._state_required(), "commit_snapshot", original_commit)


def test_bookmark_recovery_preserves_external_third_hash(tmp_path, monkeypatch):
    service, prose, tagged = _fixture(tmp_path)
    initial = _prepare_test_plan(service, "bookmark-conflict-before", prose, tagged, None, [
        {"chunk_id": "part", "start": 0, "end": 5, "request_spec": None},
    ])
    inspected = _inspect(service)
    outside = _docx("other")

    def external_edit_then_fail(**_kwargs):
        (tmp_path / "Chapters/1/chapter_audio-tags.docx").write_bytes(outside)
        raise ProjectStateError("stale_manifest")

    monkeypatch.setattr(service._state_required(), "commit_snapshot", external_edit_then_fail)
    monkeypatch.setattr(service, "recover_bookmark_publications", lambda: None)
    request = PrepareRequest.model_validate({
        "project": "fixture", "operation_id": "bookmark-external-conflict", "chapter_id": "ch1",
        "document_view_id": inspected["document_view_id"],
        "expected_prose_sha256": hashlib.sha256(prose).hexdigest(),
        "expected_tagged_sha256": hashlib.sha256(tagged).hexdigest(),
        "expected_manifest_revision": initial["manifest_revision"],
        "scope": {"kind": "test", "authorization_id": "test-auth"},
        "speech_selection_confirmed": True, "request_limit": {"value": 100, "unit": "unicode_codepoints"},
        "expected_settings_sha256": None, "production_target": None,
        "chunks": [{"chunk_id": "part", "start": 0, "end": 5, "request_spec": None}],
        "publish_bookmarks_to_working_tagged_docx": True,
    })
    with pytest.raises(BookServiceError):
        service.prepare(request, owner_key="principal:fixture")
    restarted = BookService(tmp_path, "fixture")
    with pytest.raises(BookServiceError) as conflict:
        restarted.recover_bookmark_publications()
    assert conflict.value.reason == "publication_conflict"
    assert (tmp_path / "Chapters/1/chapter_audio-tags.docx").read_bytes() == outside
    assert ProjectState.discover(tmp_path).publications("chapter_bookmark_prepare")


def _production_prepared_fixture(root):
    service, prose, tagged = _fixture(root)
    projected = project_docx_pair(prose, tagged)
    chapter_state_path = root / "Chapters/1/chapter.json"
    chapter_state = json.loads(chapter_state_path.read_text(encoding="utf-8"))
    source_sha = hashlib.sha256(prose).hexdigest()
    chapter_state.update({
        "editorial_status": "approved", "approved_source_raw_sha256": source_sha,
        "approved_prose_projection_sha256": projected.prose_projection_sha256,
        "approval_projection_version": projected.projection_version,
        "approval_provenance": {
            "actor": "fixture", "approved_at": datetime.now(timezone.utc).isoformat(),
            "source_raw_sha256": source_sha,
            "prose_projection_sha256": projected.prose_projection_sha256,
            "projection_version": projected.projection_version,
        },
    })
    chapter_state_path.write_text(json.dumps(chapter_state), encoding="utf-8")
    layout_path = root / "Project Files/Book_Layout.json"
    layout = json.loads(layout_path.read_text(encoding="utf-8"))
    layout["production_authorization"] = {
        "authorization_id": "production-auth", "actor": "fixture",
        "authorized_at": datetime.now(timezone.utc).isoformat(),
        "completed_book": True, "revoked": False,
    }
    layout_path.write_text(json.dumps(layout), encoding="utf-8")
    settings = {
        "schema_version": 1, "target_codepoints": 100,
        "request_limit": {"value": 100, "unit": "unicode_codepoints", "evidence": "fixture"},
        "request_spec": {
            "provider": "fixture-provider", "route": "fixture-route", "model_id": "model-a",
            "voice_id": "voice-a", "parameters": {"stability": 0.4},
            "context_fields": {"language": "en"},
        },
        "production_target": {
            "sample_rate_hz": 8000, "channels": 1, "encoding": "signed_integer",
            "storage_bits": 16, "valid_bits": 16, "mp3_bitrate_kbps": 192,
        },
        "native_format_evidence": "synthetic local fixture",
    }
    settings_path = root / "Project Files/production-settings.json"
    settings_path.write_text(json.dumps(settings, separators=(",", ":")), encoding="utf-8")
    inspected = _inspect(service)
    request = PrepareRequest.model_validate({
        "project": "fixture", "operation_id": "production-prepare", "chapter_id": "ch1",
        "document_view_id": inspected["document_view_id"],
        "expected_prose_sha256": hashlib.sha256(prose).hexdigest(),
        "expected_tagged_sha256": hashlib.sha256(tagged).hexdigest(),
        "expected_manifest_revision": None,
        "scope": {"kind": "production"}, "speech_selection_confirmed": True,
        "request_limit": {"value": 100, "unit": "unicode_codepoints"},
        "expected_settings_sha256": hashlib.sha256(settings_path.read_bytes()).hexdigest(),
        "production_target": settings["production_target"],
        "chunks": [{"chunk_id": "main", "start": 0, "end": 5,
                    "request_spec": settings["request_spec"]}],
        "publish_bookmarks_to_working_tagged_docx": False,
    })
    prepared, _ = service.prepare(request, owner_key="principal:fixture")
    state = ProjectState.discover(root)
    assert state is not None
    stored = state.snapshot(prepared["snapshot_id"])
    assert stored is not None
    return service, state, stored, settings_path, layout_path, chapter_state_path, prose, tagged, settings


def test_production_eligibility_keeps_formatting_equivalence_and_rejects_changed_facts(tmp_path):
    service, state, stored, settings_path, layout_path, _chapter_state_path, prose, _tagged, settings = (
        _production_prepared_fixture(tmp_path)
    )
    original_raw = hashlib.sha256(prose).hexdigest()
    (tmp_path / "Chapters/1/chapter.docx").write_bytes(
        prose.replace(b"<w:p>", b'<w:p w:rsidR="00000001">')
    )
    settings_path.write_text(json.dumps(settings, indent=2), encoding="utf-8")
    layout = service._enabled_layout()[2]
    chapter = service._chapter(layout, "ch1")
    eligible, reason = service._production_snapshot_eligible(state, layout, chapter, stored)
    assert eligible, reason
    audit = stored["payload"]["approval_equivalence"]
    assert audit["approval_source_raw_sha256"] == original_raw
    assert audit["observed_prose_raw_sha256"] == original_raw
    assert audit["equivalent"] is True

    changed_settings = json.loads(settings_path.read_text(encoding="utf-8"))
    changed_settings["request_spec"]["voice_id"] = "voice-b"
    settings_path.write_text(json.dumps(changed_settings), encoding="utf-8")
    eligible, reason = service._production_snapshot_eligible(state, layout, chapter, stored)
    assert not eligible and reason == "plan_ineligible"

    settings_path.write_text(json.dumps(settings), encoding="utf-8")
    changed_settings = json.loads(json.dumps(settings))
    changed_settings["request_spec"]["context_fields"]["language"] = "fr"
    settings_path.write_text(json.dumps(changed_settings), encoding="utf-8")
    eligible, reason = service._production_snapshot_eligible(state, layout, chapter, stored)
    assert not eligible and reason == "plan_ineligible"

    changed_settings["request_spec"]["context_fields"] = {}
    settings_path.write_text(json.dumps(changed_settings), encoding="utf-8")
    eligible, reason = service._production_snapshot_eligible(state, layout, chapter, stored)
    assert not eligible and reason == "plan_ineligible"

    settings_path.write_text(json.dumps(settings), encoding="utf-8")
    changed_settings = dict(settings)
    changed_settings["production_target"] = {**settings["production_target"], "sample_rate_hz": 16000}
    settings_path.write_text(json.dumps(changed_settings), encoding="utf-8")
    eligible, reason = service._production_snapshot_eligible(state, layout, chapter, stored)
    assert not eligible and reason == "target_changed"

    layout_data = json.loads(layout_path.read_text(encoding="utf-8"))
    layout_data["production_authorization"]["revoked"] = True
    layout_path.write_text(json.dumps(layout_data), encoding="utf-8")
    revoked_layout = service._enabled_layout()[2]
    eligible, reason = service._production_snapshot_eligible(
        state, revoked_layout, service._chapter(revoked_layout, "ch1"), stored,
    )
    assert not eligible and reason == "production_not_authorized"


def test_production_eligibility_rejects_changed_approved_prose(tmp_path):
    service, state, stored, _settings_path, _layout_path, _chapter_state_path, _prose, _tagged, _settings = (
        _production_prepared_fixture(tmp_path)
    )
    changed = _docx("world")
    (tmp_path / "Chapters/1/chapter.docx").write_bytes(changed)
    (tmp_path / "Chapters/1/chapter_audio-tags.docx").write_bytes(changed)
    layout = service._enabled_layout()[2]
    chapter = service._chapter(layout, "ch1")
    eligible, reason = service._production_snapshot_eligible(state, layout, chapter, stored)
    assert not eligible and reason == "chapter_not_approved"
    chunk = stored["payload"]["result"]["chunks"][0]
    with pytest.raises(BookServiceError) as denied:
        service.build(BuildRequest.model_validate({
            "project": "fixture", "operation_id": "changed-prose-build", "expected_head_revision": None,
            "input": {"kind": "chapter", "chapter_id": "ch1", "snapshot_id": stored["payload"]["result"]["snapshot_id"],
                      "expected_manifest_revision": stored["manifest_revision"],
                      "request_plan_sha256": stored["payload"]["result"]["request_plan_sha256"],
                      "takes": [{"chunk_id": "main", "take_id": "unavailable", "request_sha256": chunk["request_sha256"]}]},
            "mode": "production_pcm", "outputs": {"master": True}, "gaps": [],
            "metadata": {"title": "Fixture", "author": "Fixture", "edition": "test"},
        }), owner_key="principal:fixture")
    assert denied.value.reason == "stale_dependency"


def _build_and_accept_production_chapter(service, prepared, take, *, operation_prefix, expected_head):
    chunk = prepared["chunks"][0]
    build_request = BuildRequest.model_validate({
        "project": "fixture", "operation_id": f"{operation_prefix}-build", "expected_head_revision": expected_head,
        "input": {"kind": "chapter", "chapter_id": "ch1", "snapshot_id": prepared["snapshot_id"],
                  "expected_manifest_revision": prepared["manifest_revision"],
                  "request_plan_sha256": prepared["request_plan_sha256"],
                  "takes": [{"chunk_id": chunk["chunk_id"], "take_id": take["take_id"],
                             "request_sha256": chunk["request_sha256"]}]},
        "mode": "production_pcm", "outputs": {"master": True, "mp3_bitrate_kbps": 192}, "gaps": [],
        "metadata": {"title": "Fixture", "author": "Fixture", "edition": "test"},
    })
    queued, _ = service.build(build_request, owner_key="principal:fixture")
    build_id = f"synthetic-{operation_prefix}"
    relative = f"Audiobook/Chapters/1/builds/{build_id}"
    directory = service.root / relative
    directory.mkdir(parents=True, exist_ok=False)
    samples = (service.root / take["filepath"]).read_bytes()
    pcm_path = directory / "master.pcm"
    pcm_path.write_bytes(samples)
    media = dict(take["media"])
    media.update({"container": "raw_pcm", "frame_count": len(samples) // 2,
                  "duration_seconds": len(samples) / 2 / 8000})
    output = {"kind": "pcm_master", "filepath": f"{relative}/master.pcm",
              "bytes_sha256": hashlib.sha256(samples).hexdigest(), "size_bytes": len(samples),
              "media": media}
    timeline_value = {"sample_rate_hz": 8000, "channels": 1, "encoding": "signed_integer",
                      "storage_bits": 16, "frame_count": len(samples) // 2,
                      "entries": [{"source_id": chunk["chunk_id"], "kind": "audio",
                                   "start_frame": "0", "end_frame": str(len(samples) // 2),
                                   "source_bytes_sha256": take["bytes_sha256"],
                                   "canonical_sample_sha256": media["canonical_sample_sha256"]}]}
    (directory / "timeline.json").write_text(json.dumps(timeline_value), encoding="utf-8")
    result = {
        "kind": "build", "build_id": build_id, "scope": "chapter", "namespace": {"kind": "production"},
        "source_snapshot_ids": [prepared["snapshot_id"]], "input_take_ids": [take["take_id"]],
        "chapter_dependencies": [], "request_plan_sha256": prepared["request_plan_sha256"],
        "outputs": [output], "timeline_filepath": f"{relative}/timeline.json",
        "recipe_sha256": "0" * 64,
        "validation": {"complete": True, "media_integrity": True, "coverage": True,
                       "sample_or_packet_verification": True, "errors": []},
        "needs_listening_review": True,
    }
    service._state_required().finish_build_success(job_id=queued["job_id"], build={
        "scope": "chapter", "build_id": build_id, "chapter_id": "ch1",
        "scope_key": '{"kind":"production"}', "snapshot_id": prepared["snapshot_id"],
        "request_plan_sha256": prepared["request_plan_sha256"], "input_take_ids": [take["take_id"]],
        "created_at": datetime.now(timezone.utc).isoformat(), "was_accepted": False, "result": result,
    })
    committed, _ = service.commit_build(CommitBuildRequest.model_validate({
        "project": "fixture", "operation_id": f"{operation_prefix}-accept",
        "build_id": build_id, "expected_head_revision": expected_head,
        "intent": "accept_candidate",
        "acceptance": {"actor": "fixture", "accepted_at": datetime.now(timezone.utc).isoformat(),
                       "listening_review": "passed", "notes": ["synthetic local PCM test"]},
    }), owner_key="principal:fixture")
    return committed


def _reserve_synthetic_book_build(service, state, layout, *, operation_prefix, expected_book_head):
    head = state.chapter_head("ch1", '{"kind":"production"}')
    assert head is not None
    dependency = {
        "chapter_id": "ch1", "chapter_build_id": head["accepted_build_id"],
        "chapter_head_revision": head["head_revision"], "snapshot_id": head["accepted_snapshot_id"],
        "request_plan_sha256": head["accepted_plan_sha256"],
    }
    request = BuildRequest.model_validate({
        "project": "fixture", "operation_id": f"{operation_prefix}-reserve",
        "expected_head_revision": expected_book_head,
        "input": {"kind": "book", "book_id": layout.book_id,
                  "expected_layout_revision": layout.layout_revision, "chapters": [dependency]},
        "mode": "production_pcm", "outputs": {"master": True, "mp3_bitrate_kbps": 192},
        "gaps": [], "metadata": {"title": "Fixture", "author": "Fixture", "edition": "test"},
    })
    queued, _ = service.build(request, owner_key="principal:fixture")
    job = state.build_job(queued["job_id"])
    assert job is not None
    pinned = job["payload"]
    dependencies = [entry["dependency"] for entry in pinned["chapters"]]
    chapter_build = state.build(dependency["chapter_build_id"])
    assert chapter_build is not None
    output = next(item for item in chapter_build["result"]["outputs"] if item["kind"] == "pcm_master")
    build_id = f"synthetic-book-{operation_prefix}"
    result = {
        "kind": "build", "build_id": build_id, "scope": "book", "namespace": {"kind": "production"},
        "source_snapshot_ids": [item["snapshot_id"] for item in dependencies],
        "input_take_ids": chapter_build["input_take_ids"], "chapter_dependencies": dependencies,
        "request_plan_sha256": None, "outputs": [output],
        "timeline_filepath": chapter_build["result"]["timeline_filepath"],
        "recipe_sha256": "0" * 64,
        "validation": {"complete": True, "media_integrity": True, "coverage": True,
                       "sample_or_packet_verification": True, "errors": []},
        "needs_listening_review": True,
    }
    state.finish_build_success(job_id=queued["job_id"], build={
        "scope": "book", "build_id": build_id, "book_id": layout.book_id,
        "created_at": datetime.now(timezone.utc).isoformat(), "was_accepted": False,
        "dependencies": dependencies, "result": result,
    })
    return build_id


def _commit_book(service, operation_id, build_id, expected_head, intent):
    return service.commit_build(CommitBuildRequest.model_validate({
        "project": "fixture", "operation_id": operation_id, "build_id": build_id,
        "expected_head_revision": expected_head, "intent": intent,
        "acceptance": {"actor": "fixture", "accepted_at": datetime.now(timezone.utc).isoformat(),
                       "listening_review": "passed", "notes": ["synthetic local PCM test"]},
    }), owner_key="principal:fixture")[0]


def test_same_prose_chapter_retake_stales_reserved_book_dependency(tmp_path):
    service, state, stored, _settings_path, _layout_path, _chapter_state_path, _prose, _tagged, _settings = (
        _production_prepared_fixture(tmp_path)
    )
    prepared = stored["payload"]["result"]
    first_take = _import_native_take(service, prepared, "main", operation_prefix="book-first-take")
    first = _build_and_accept_production_chapter(service, prepared, first_take,
                                                 operation_prefix="book-first", expected_head=None)
    service = BookService(tmp_path, "fixture")
    state = ProjectState.discover(tmp_path)
    assert state is not None
    layout = service._enabled_layout()[2]
    chapter_head = state.chapter_head("ch1", '{"kind":"production"}')
    assert chapter_head is not None
    dependency = {
        "chapter_id": "ch1", "chapter_build_id": chapter_head["accepted_build_id"],
        "chapter_head_revision": chapter_head["head_revision"],
        "snapshot_id": chapter_head["accepted_snapshot_id"],
        "request_plan_sha256": chapter_head["accepted_plan_sha256"],
    }
    book_request = BuildRequest.model_validate({
        "project": "fixture", "operation_id": "book-race-reserve", "expected_head_revision": None,
        "input": {"kind": "book", "book_id": layout.book_id,
                  "expected_layout_revision": layout.layout_revision, "chapters": [dependency]},
        "mode": "production_pcm", "outputs": {"master": True, "mp3_bitrate_kbps": 192},
        "gaps": [], "metadata": {"title": "Fixture", "author": "Fixture", "edition": "test"},
    })
    queued, _ = service.build(book_request, owner_key="principal:fixture")
    book_job = state.build_job(queued["job_id"])
    assert book_job is not None

    second_take = _import_native_take(service, prepared, "main", operation_prefix="book-second-take")
    second = _build_and_accept_production_chapter(service, prepared, second_take,
                                                  operation_prefix="book-second", expected_head=1)
    assert second["head_revision"] == 2
    with pytest.raises(BookServiceError) as chapter_cas:
        service.commit_build(CommitBuildRequest.model_validate({
            "project": "fixture", "operation_id": "competing-chapter-commit",
            "build_id": first["accepted_build_id"], "expected_head_revision": 1,
            "intent": "accept_candidate",
            "acceptance": {"actor": "fixture", "accepted_at": datetime.now(timezone.utc).isoformat(),
                           "listening_review": "passed", "notes": []},
        }), owner_key="principal:fixture")
    assert chapter_cas.value.reason == "stale_head"
    pinned = book_job["payload"]
    book_build_id = "synthetic-book-build"
    dependency_set = [entry["dependency"] for entry in pinned["chapters"]]
    state.finish_build_success(job_id=queued["job_id"], build={
        "scope": "book", "build_id": book_build_id, "book_id": layout.book_id,
        "created_at": datetime.now(timezone.utc).isoformat(), "was_accepted": False,
        "dependencies": dependency_set,
        "result": {
            "kind": "build", "build_id": book_build_id, "scope": "book",
            "namespace": {"kind": "production"},
            "source_snapshot_ids": [entry["snapshot_id"] for entry in dependency_set],
            "input_take_ids": [], "chapter_dependencies": dependency_set,
            "request_plan_sha256": None,
            "outputs": [first["exports"][0]], "timeline_filepath": first["exports"][0]["filepath"],
            "recipe_sha256": "0" * 64,
            "validation": {"complete": True, "media_integrity": True, "coverage": True,
                           "sample_or_packet_verification": True, "errors": []},
            "needs_listening_review": True,
        },
    })
    with pytest.raises(BookServiceError) as stale:
        service.commit_build(CommitBuildRequest.model_validate({
            "project": "fixture", "operation_id": "book-race-commit", "build_id": book_build_id,
            "expected_head_revision": None, "intent": "accept_candidate",
            "acceptance": {"actor": "fixture", "accepted_at": datetime.now(timezone.utc).isoformat(),
                           "listening_review": "passed", "notes": []},
        }), owner_key="principal:fixture")
    assert stale.value.reason == "stale_dependency"
    assert state.book_head(layout.book_id) is None


def test_chapter_rollback_allows_older_performance_after_tag_and_settings_refresh(tmp_path):
    service, state, stored, settings_path, _layout_path, _chapter_state_path, prose, tagged, settings = (
        _production_prepared_fixture(tmp_path)
    )
    prepared = stored["payload"]["result"]
    take = _import_native_take(service, prepared, "main", operation_prefix="historic-chapter-take")
    accepted = _build_and_accept_production_chapter(
        service, prepared, take, operation_prefix="historic-chapter", expected_head=None,
    )
    tagged_refresh = tagged.replace(b"<w:p>", b'<w:p w:rsidR="00000002">')
    prose_refresh = prose.replace(b"<w:p>", b'<w:p w:rsidR="00000003">')
    tagged_path = tmp_path / "Chapters/1/chapter_audio-tags.docx"
    prose_path = tmp_path / "Chapters/1/chapter.docx"
    tagged_path.write_bytes(tagged_refresh)
    prose_path.write_bytes(prose_refresh)
    current_settings = json.loads(json.dumps(settings))
    current_settings["request_spec"]["voice_id"] = "voice-b"
    settings_path.write_text(json.dumps(current_settings, indent=2), encoding="utf-8")
    inspected = _inspect(service)
    refreshed, _ = service.prepare(PrepareRequest.model_validate({
        "project": "fixture", "operation_id": "historic-chapter-refresh", "chapter_id": "ch1",
        "document_view_id": inspected["document_view_id"],
        "expected_prose_sha256": hashlib.sha256(prose_refresh).hexdigest(),
        "expected_tagged_sha256": hashlib.sha256(tagged_refresh).hexdigest(),
        "expected_manifest_revision": prepared["manifest_revision"],
        "scope": {"kind": "production"}, "speech_selection_confirmed": True,
        "request_limit": {"value": 100, "unit": "unicode_codepoints"},
        "expected_settings_sha256": hashlib.sha256(settings_path.read_bytes()).hexdigest(),
        "production_target": current_settings["production_target"],
        "chunks": [{"chunk_id": "main", "start": 0, "end": 5,
                    "request_spec": current_settings["request_spec"]}],
        "publish_bookmarks_to_working_tagged_docx": False,
    }), owner_key="principal:fixture")
    before = prose_path.read_bytes(), tagged_path.read_bytes()
    rolled_back, _ = service.commit_build(CommitBuildRequest.model_validate({
        "project": "fixture", "operation_id": "historic-chapter-rollback",
        "build_id": accepted["accepted_build_id"], "expected_head_revision": 1,
        "intent": "rollback",
        "acceptance": {"actor": "fixture", "accepted_at": datetime.now(timezone.utc).isoformat(),
                       "listening_review": "passed", "notes": ["same approved prose"]},
    }), owner_key="principal:fixture")
    assert rolled_back["accepted_plan_matches_prepared"] is False
    assert rolled_back["accepted_build_id"] == accepted["accepted_build_id"]
    assert refreshed["request_plan_sha256"] != prepared["request_plan_sha256"]
    assert (prose_path.read_bytes(), tagged_path.read_bytes()) == before


def test_book_accepts_current_chapter_rollback_and_historical_book_rollback(tmp_path):
    service, state, stored, _settings_path, _layout_path, _chapter_state_path, _prose, _tagged, _settings = (
        _production_prepared_fixture(tmp_path)
    )
    prepared = stored["payload"]["result"]
    first_take = _import_native_take(service, prepared, "main", operation_prefix="books-first-take")
    chapter_first = _build_and_accept_production_chapter(
        service, prepared, first_take, operation_prefix="books-first", expected_head=None,
    )
    second_take = _import_native_take(service, prepared, "main", operation_prefix="books-second-take")
    chapter_second = _build_and_accept_production_chapter(
        service, prepared, second_take, operation_prefix="books-second", expected_head=1,
    )
    rolled_chapter = service.commit_build(CommitBuildRequest.model_validate({
        "project": "fixture", "operation_id": "books-chapter-rollback",
        "build_id": chapter_first["accepted_build_id"], "expected_head_revision": 2,
        "intent": "rollback",
        "acceptance": {"actor": "fixture", "accepted_at": datetime.now(timezone.utc).isoformat(),
                       "listening_review": "passed", "notes": ["same request plan"]},
    }), owner_key="principal:fixture")[0]
    assert rolled_chapter["accepted_plan_matches_prepared"] is True
    layout = service._enabled_layout()[2]
    book_first_id = _reserve_synthetic_book_build(
        service, state, layout, operation_prefix="books-first", expected_book_head=None,
    )
    _commit_book(service, "books-first-book-accept", book_first_id, None, "accept_candidate")

    third_take = _import_native_take(service, prepared, "main", operation_prefix="books-third-take")
    chapter_third = _build_and_accept_production_chapter(
        service, prepared, third_take, operation_prefix="books-third", expected_head=3,
    )
    assert chapter_third["head_revision"] == 4
    book_second_id = _reserve_synthetic_book_build(
        service, state, layout, operation_prefix="books-second", expected_book_head=1,
    )
    _commit_book(service, "books-second-book-accept", book_second_id, 1, "accept_candidate")
    with pytest.raises(BookServiceError) as book_cas:
        _commit_book(service, "books-competing-book-commit", book_second_id, 1, "accept_candidate")
    assert book_cas.value.reason == "stale_head"

    working = (tmp_path / "Chapters/1/chapter.docx").read_bytes()
    tagged = (tmp_path / "Chapters/1/chapter_audio-tags.docx").read_bytes()
    rolled_book = _commit_book(service, "books-historic-rollback", book_first_id, 2, "rollback")
    assert rolled_book["accepted_build_id"] == book_first_id
    assert rolled_book["accepted_plan_matches_prepared"] is False
    assert (tmp_path / "Chapters/1/chapter.docx").read_bytes() == working
    assert (tmp_path / "Chapters/1/chapter_audio-tags.docx").read_bytes() == tagged

    fourth_take = _import_native_take(service, prepared, "main", operation_prefix="books-fourth-take")
    fourth = _build_and_accept_production_chapter(
        service, prepared, fourth_take, operation_prefix="books-fourth", expected_head=4,
    )
    assert fourth["head_revision"] == 5
    book_state = service.get_book(GetBookRequest.model_validate({
        "project": "fixture", "book_id": layout.book_id,
    }))
    assert book_state["accepted_build_id"] == book_first_id
    assert book_state["current_outputs_stale"] is True


def test_prepare_freezes_exact_tag_spans_and_chunk_spoken_facts(tmp_path):
    service, _, _ = _fixture(tmp_path)
    prose = _docx("repeat  repeat")
    tagged = _tagged_docx_with_audio_tag()
    (tmp_path / "Chapters/1/chapter.docx").write_bytes(prose)
    (tmp_path / "Chapters/1/chapter_audio-tags.docx").write_bytes(tagged)
    layout_path = tmp_path / "Project Files/Book_Layout.json"
    layout = json.loads(layout_path.read_text(encoding="utf-8"))
    layout["test_authorizations"][0]["source_raw_sha256"] = hashlib.sha256(prose).hexdigest()
    layout_path.write_text(json.dumps(layout), encoding="utf-8")
    inspected = _inspect(service)
    assert inspected["speech_text"] == "repeat [tag] repeat"
    request = PrepareRequest.model_validate({
        "project": "fixture", "operation_id": "prepare-exact-tags", "chapter_id": "ch1",
        "document_view_id": inspected["document_view_id"],
        "expected_prose_sha256": hashlib.sha256(prose).hexdigest(),
        "expected_tagged_sha256": hashlib.sha256(tagged).hexdigest(),
        "expected_manifest_revision": None, "scope": {"kind": "test", "authorization_id": "test-auth"},
        "speech_selection_confirmed": True,
        "request_limit": {"value": 100, "unit": "unicode_codepoints"},
        "expected_settings_sha256": None, "production_target": None,
        "chunks": [{"chunk_id": "repeated", "start": 0, "end": len(inspected["speech_text"]), "request_spec": None}],
        "publish_bookmarks_to_working_tagged_docx": False,
    })
    prepared, replayed = service.prepare(request, owner_key="principal:fixture")
    assert not replayed
    state = ProjectState.discover(tmp_path)
    assert state is not None
    snapshot = state.snapshot(prepared["snapshot_id"])
    assert snapshot is not None
    payload = snapshot["payload"]
    assert payload["tag_deletion_spans"] == [[7, 12]]
    chunk = payload["result"]["chunks"][0]
    spoken = "repeat  repeat"
    assert chunk["spoken_text_sha256"] == hashlib.sha256(spoken.encode()).hexdigest()
    assert chunk["opening_phrase"] == spoken and chunk["closing_phrase"] == spoken

    # Frozen read output remains tied to the prepared byte pair after Word
    # changes the live tagged file, and the deleted occurrence is exact.
    (tmp_path / "Chapters/1/chapter_audio-tags.docx").write_bytes(_docx("changed live copy"))
    chapter = service.get_chapter(GetChapterRequest.model_validate({
        "project": "fixture", "chapter_id": "ch1",
        "scope": {"kind": "test", "authorization_id": "test-auth"},
        "snapshot_id": prepared["snapshot_id"], "include_text": True,
    }))
    assert chapter["returned_texts"][0]["prompt"]["text"] == "repeat [tag] repeat"
    assert chapter["returned_texts"][0]["spoken_text"]["text"] == spoken


def test_find_chunk_maps_tagged_chunk_ranges_into_frozen_spoken_coordinates(tmp_path):
    service, _, _ = _fixture(tmp_path)
    prose = _docx("repeat  repeat")
    tagged = _tagged_docx_with_audio_tag()
    (tmp_path / "Chapters/1/chapter.docx").write_bytes(prose)
    (tmp_path / "Chapters/1/chapter_audio-tags.docx").write_bytes(tagged)
    layout_path = tmp_path / "Project Files/Book_Layout.json"
    layout = json.loads(layout_path.read_text(encoding="utf-8"))
    layout["test_authorizations"][0]["source_raw_sha256"] = hashlib.sha256(prose).hexdigest()
    layout_path.write_text(json.dumps(layout), encoding="utf-8")

    inspected = _inspect(service)
    assert inspected["speech_text"] == "repeat [tag] repeat"
    tag_start = inspected["speech_text"].index("[tag]")
    prepared = _prepare_test_plan(service, "prepare-quote-coordinates", prose, tagged, None, [
        {"chunk_id": "opening", "start": 0, "end": tag_start, "request_spec": None},
        {"chunk_id": "closing", "start": tag_start, "end": len(inspected["speech_text"]), "request_spec": None},
    ])

    repeated = service.find_chunk(FindChunkRequest.model_validate({
        "project": "fixture", "chapter_id": "ch1",
        "query": {"kind": "quote", "text": "repeat", "snapshot_id": prepared["snapshot_id"]},
    }))
    assert [match["chunk_ids"] for match in repeated["matches"]] == [
        [prepared["chunks"][0]["chunk_id"]],
        [prepared["chunks"][1]["chunk_id"]],
    ]

    across_boundary = service.find_chunk(FindChunkRequest.model_validate({
        "project": "fixture", "chapter_id": "ch1",
        "query": {"kind": "quote", "text": "t  r", "snapshot_id": prepared["snapshot_id"]},
    }))
    assert across_boundary["matches"][0]["chunk_ids"] == [
        prepared["chunks"][0]["chunk_id"], prepared["chunks"][1]["chunk_id"],
    ]


def test_bootstrap_inspect_is_read_only_and_prepare_receipt_survives_restart(tmp_path):
    service, prose, tagged = _fixture(tmp_path)
    inspected = _inspect(service)
    assert inspected["speech_text"] == "hello"
    assert not (tmp_path / ".cognita-storage").exists()
    assert not (tmp_path / ".cognita-book-binding.json").exists()

    request = PrepareRequest.model_validate({
        "project": "fixture", "operation_id": "prepare-once", "chapter_id": "ch1",
        "document_view_id": inspected["document_view_id"],
        "expected_prose_sha256": hashlib.sha256(prose).hexdigest(),
        "expected_tagged_sha256": hashlib.sha256(tagged).hexdigest(),
        "expected_manifest_revision": None, "scope": {"kind": "test", "authorization_id": "test-auth"},
        "speech_selection_confirmed": True,
        "request_limit": {"value": 100, "unit": "unicode_codepoints"},
        "expected_settings_sha256": None, "production_target": None,
        "chunks": [{"chunk_id": "chunk-1", "start": 0, "end": 5, "request_spec": None}],
        "publish_bookmarks_to_working_tagged_docx": False,
    })
    first, replayed = service.prepare(request, owner_key="principal:fixture")
    assert replayed is False
    assert first["coverage"]["covered_codepoints"] == 5
    assert (tmp_path / ".cognita-book-binding.json").is_file()
    assert ProjectState.discover(tmp_path) is not None

    reopened = BookService(tmp_path, "fixture")
    second, replayed = reopened.prepare(request, owner_key="principal:fixture")
    assert replayed is True
    assert second["snapshot_id"] == first["snapshot_id"]
    # A project-scoped service needs no Postgres connection to inspect, prepare,
    # and replay its immutable snapshot.
    assert not hasattr(reopened, "store")


def test_get_book_reports_registered_order_and_unready_production_heads(tmp_path):
    service, _prose, _tagged = _fixture(tmp_path, bound=True)
    ProjectState.initialize(tmp_path)
    value = service.get_book(GetBookRequest(project="fixture", book_id="fixture-book"))
    assert value["head_revision"] is None
    assert value["chapter_order"] == ["ch1"]
    assert value["chapters_not_ready"] == [{"chapter_id": "ch1", "reason": "no_accepted_production_head"}]
    first = service.get_book(GetBookRequest.model_validate({
        "project": "fixture", "book_id": "fixture-book", "limit": 1,
    }))
    assert first["chapter_order"] == ["ch1"] and first["chapters_not_ready"] == [] and first["has_more"]
    second = service.get_book(GetBookRequest.model_validate({
        "project": "fixture", "book_id": "fixture-book", "limit": 1, "cursor": first["next_cursor"],
    }))
    assert second["chapter_order"] == []
    assert second["chapters_not_ready"] == [{"chapter_id": "ch1", "reason": "no_accepted_production_head"}]


def test_invalid_binding_fails_closed_instead_of_reentering_bootstrap(tmp_path):
    service, _prose, _tagged = _fixture(tmp_path)
    (tmp_path / ".cognita-book-binding.json").write_text(
        '{"schema_version":1,"book_id":"different","layout_filepath":"Project Files/Book_Layout.json","state_root":".cognita-storage"}',
        encoding="utf-8",
    )
    with pytest.raises(BookServiceError) as error:
        _inspect(service)
    assert error.value.reason == "configuration_conflict"
    assert not (tmp_path / ".cognita-storage").exists()


def test_interrupted_first_binding_publication_retries_from_durable_view(tmp_path, monkeypatch):
    service, prose, tagged = _fixture(tmp_path)
    inspected = _inspect(service)
    request = PrepareRequest.model_validate({
        "project": "fixture", "operation_id": "prepare-after-restart", "chapter_id": "ch1",
        "document_view_id": inspected["document_view_id"],
        "expected_prose_sha256": hashlib.sha256(prose).hexdigest(),
        "expected_tagged_sha256": hashlib.sha256(tagged).hexdigest(),
        "expected_manifest_revision": None, "scope": {"kind": "test", "authorization_id": "test-auth"},
        "speech_selection_confirmed": True,
        "request_limit": {"value": 100, "unit": "unicode_codepoints"},
        "expected_settings_sha256": None, "production_target": None,
        "chunks": [{"chunk_id": "chunk-1", "start": 0, "end": 5, "request_spec": None}],
        "publish_bookmarks_to_working_tagged_docx": False,
    })
    original_open = service_module.os.open
    fail_binding = True

    def interrupted_open(path, flags, *args, **kwargs):
        nonlocal fail_binding
        if str(path).endswith(".cognita-book-binding.json") and fail_binding:
            fail_binding = False
            raise OSError("synthetic interruption during create-only binding")
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(service_module.os, "open", interrupted_open)
    with pytest.raises(BookServiceError) as error:
        service.prepare(request, owner_key="principal:fixture")
    assert error.value.reason == "state_unavailable"
    assert ProjectState.discover(tmp_path) is not None
    assert not (tmp_path / ".cognita-book-binding.json").exists()

    monkeypatch.setattr(service_module.os, "open", original_open)
    restarted = BookService(tmp_path, "fixture")
    result, replayed = restarted.prepare(request, owner_key="principal:fixture")
    assert not replayed
    assert result["manifest_revision"] == 1
    assert (tmp_path / ".cognita-book-binding.json").is_file()


def test_managed_indexing_receipt_is_durable_and_operation_bound(tmp_path):
    state = ProjectState.initialize(tmp_path)
    first = state.begin_managed_write(
        job_id="job-1", source_path="Chapters/1/chapter.docx",
        bytes_sha256="a" * 64, operation_id="write-1",
    )
    assert first == ("created", "job-1")
    assert state.managed_write_status("Chapters/1/chapter.docx") == {
        "state": "pending", "job_id": "job-1", "error": None,
    }
    assert state.begin_managed_write(
        job_id="unused", source_path="Chapters/1/chapter.docx",
        bytes_sha256="a" * 64, operation_id="write-1",
    ) == ("replay", "job-1")
    with pytest.raises(ProjectStateError, match="operation_conflict"):
        state.begin_managed_write(
            job_id="job-2", source_path="Chapters/1/chapter.docx",
            bytes_sha256="b" * 64, operation_id="write-1",
        )
    assert state.finish_managed_write(
        job_id="job-1", state="blocked", error={"code": "database_unavailable", "message": "Index unavailable."},
        doc_id=None, extracted_sha256=None,
    )["state"] == "blocked"
    reopened = ProjectState.discover(tmp_path)
    assert reopened is not None
    assert reopened.managed_write_status("Chapters/1/chapter.docx")["state"] == "blocked"


def test_registered_index_provenance_is_persisted_and_revalidated(tmp_path):
    from cognita.parsing import compute_doc_id, parse_file

    service, _prose, _tagged = _fixture(tmp_path, bound=True)
    ProjectState.initialize(tmp_path)
    path = "Project Files/ref.md"
    source = tmp_path / path
    parsed = parse_file(source, tmp_path)
    assert parsed is not None
    raw_sha = hashlib.sha256(source.read_bytes()).hexdigest()
    assert parsed.doc_id == compute_doc_id(path, parsed.content_hash)
    record = service.record_index_provenance(
        path, parsed.doc_id, parsed.content_hash, raw_sha, "fixture-extraction-v1",
    )
    assert record is not None and record.role == "reference"
    state = ProjectState.discover(tmp_path)
    assert state is not None
    assert state.indexed_role_provenance(path) == record
    from types import SimpleNamespace
    candidate = SimpleNamespace(source=path, doc_id=parsed.doc_id, content_hash=parsed.content_hash)
    assert set(service.index_admitted_doc_ids([candidate])) == {parsed.doc_id}

    source.write_text("Reference changed", encoding="utf-8")
    assert service.index_admitted_doc_ids([candidate]) == {}


def test_profile_admission_separates_drafts_canon_instructions_and_workflow(tmp_path):
    from cognita.parsing import parse_file
    from types import SimpleNamespace
    from docx import Document

    service, _prose, _tagged = _fixture(tmp_path, bound=True)
    document = Document()
    document.add_paragraph("hello")
    stream = io.BytesIO()
    document.save(stream)
    valid_prose = stream.getvalue()
    for path in (
        "Chapters/1/chapter.docx", "Chapters/1/chapter_audio-tags.docx",
        "Project Files/Source/Version1.docx",
    ):
        (tmp_path / path).write_bytes(valid_prose)
    layout_path = tmp_path / "Project Files/Book_Layout.json"
    layout = json.loads(layout_path.read_text(encoding="utf-8"))
    layout["test_authorizations"][0]["source_raw_sha256"] = hashlib.sha256(valid_prose).hexdigest()
    layout_path.write_text(json.dumps(layout), encoding="utf-8")
    ProjectState.initialize(tmp_path)
    candidates = []
    paths = [
        "Project Files/ref.md", "Project Files/guide.md",
        "Project Files/workflow.md", "Chapters/1/chapter.docx",
    ]
    for path in paths:
        source = tmp_path / path
        parsed = parse_file(source, tmp_path)
        assert parsed is not None
        raw_sha = hashlib.sha256(source.read_bytes()).hexdigest()
        assert service.record_index_provenance(
            path, parsed.doc_id, parsed.content_hash, raw_sha, "fixture-extraction-v1",
        ) is not None
        candidates.append(SimpleNamespace(
            source=path, doc_id=parsed.doc_id, content_hash=parsed.content_hash,
        ))

    ids = {item.source: item.doc_id for item in candidates}
    assert set(service.index_admitted_doc_ids(candidates, "editing")) == {
        ids["Project Files/ref.md"], ids["Chapters/1/chapter.docx"],
    }
    assert set(service.index_admitted_doc_ids(candidates, "canon")) == {
        ids["Project Files/ref.md"],
    }
    assert set(service.index_admitted_doc_ids(candidates, "instructions")) == {
        ids["Project Files/guide.md"],
    }
    assert set(service.index_admitted_doc_ids(candidates, "workflow")) == {
        ids["Project Files/workflow.md"],
    }


def test_generation_reservation_is_frozen_and_receipt_backed(tmp_path):
    service, prose, tagged = _fixture(tmp_path)
    inspected = _inspect(service)
    spec = {
        "provider": "synthetic", "route": "fixture", "model_id": "model",
        "voice_id": "voice", "parameters": {}, "context_fields": {},
    }
    prepared, replayed = service.prepare(PrepareRequest.model_validate({
        "project": "fixture", "operation_id": "prepare-generation", "chapter_id": "ch1",
        "document_view_id": inspected["document_view_id"],
        "expected_prose_sha256": hashlib.sha256(prose).hexdigest(),
        "expected_tagged_sha256": hashlib.sha256(tagged).hexdigest(),
        "expected_manifest_revision": None, "scope": {"kind": "test", "authorization_id": "test-auth"},
        "speech_selection_confirmed": True,
        "request_limit": {"value": 100, "unit": "unicode_codepoints"},
        "expected_settings_sha256": None, "production_target": None,
        "chunks": [{"chunk_id": "chunk-1", "start": 0, "end": 5, "request_spec": spec}],
        "publish_bookmarks_to_working_tagged_docx": False,
    }), owner_key="principal:fixture")
    assert not replayed
    change = {
        "kind": "reserve", "chapter_id": "ch1", "snapshot_id": prepared["snapshot_id"],
        "chunk_id": "chunk-1", "expected_manifest_revision": prepared["manifest_revision"],
        "request": {"prompt_sha256": prepared["chunks"][0]["prompt_sha256"], "spec": spec},
    }
    request = RecordGenerationRequest.model_validate({
        "project": "fixture", "operation_id": "reserve-generation", "change": change,
    })
    first, replayed = service.record_generation(request, owner_key="principal:fixture")
    assert not replayed
    generation = first["generation"]
    assert generation["state"] == "reserved" and generation["media_registered"] is False
    again, replayed = service.record_generation(request, owner_key="principal:fixture")
    assert replayed and again == first
    unknown = RecordGenerationRequest.model_validate({
        "project": "fixture", "operation_id": "unknown-generation", "change": {
            "kind": "update", "generation_record_id": generation["generation_record_id"],
            "expected_generation_revision": 1, "state": "outcome_unknown",
        },
    })
    unknown_result, replayed = service.record_generation(unknown, owner_key="principal:fixture")
    assert not replayed and unknown_result["generation"]["state"] == "outcome_unknown"
    assert unknown_result["generation"]["provider_ids"] == {}
    replay, replayed = service.record_generation(unknown, owner_key="principal:fixture")
    assert replayed and replay == unknown_result
    with pytest.raises(BookServiceError) as altered:
        service.record_generation(RecordGenerationRequest.model_validate({
            "project": "fixture", "operation_id": "unknown-generation", "change": {
                "kind": "update", "generation_record_id": generation["generation_record_id"],
                "expected_generation_revision": 2, "state": "failed",
            },
        }), owner_key="principal:fixture")
    assert altered.value.reason == "operation_id_conflict"
    with pytest.raises(BookServiceError, match="Provider evidence"):
        service.record_generation(RecordGenerationRequest.model_validate({
            "project": "fixture", "operation_id": "bad-resolution", "change": {
                "kind": "update", "generation_record_id": generation["generation_record_id"],
                "expected_generation_revision": 2, "state": "submitted",
            },
        }), owner_key="principal:fixture")
    submitted = RecordGenerationRequest.model_validate({
        "project": "fixture", "operation_id": "submitted-generation", "change": {
            "kind": "update", "generation_record_id": generation["generation_record_id"],
            "expected_generation_revision": 2, "state": "submitted",
            "provider_ids": {"generation_ids": ["provider-1"]},
        },
    })
    updated, replayed = service.record_generation(submitted, owner_key="principal:fixture")
    assert not replayed and updated["generation"]["generation_revision"] == 3
    with pytest.raises(BookServiceError) as stale:
        service.record_generation(submitted, owner_key="principal:other")
    assert stale.value.reason == "stale_generation"
    # Recovery reads derive the prompt only from the frozen snapshot, after a
    # later Word/source mutation has made the live file different.
    (tmp_path / "Chapters/1/chapter.docx").write_bytes(_docx("changed"))
    recovered = service.get_generations(GetGenerationsRequest.model_validate({
        "project": "fixture", "query": {"kind": "chapter", "chapter_id": "ch1"},
        "include_prompt": True, "limit": 1,
    }))
    assert recovered["prompts"][0]["prompt"]["text"] == "hello"
    # A continuation is pinned to the durable generation revision, rather
    # than silently mixing later provider evidence into an old listing.
    second, _ = service.record_generation(RecordGenerationRequest.model_validate({
        "project": "fixture", "operation_id": "reserve-generation-second", "change": {
            **change,
        },
    }), owner_key="principal:fixture")
    first_page = service.get_generations(GetGenerationsRequest.model_validate({
        "project": "fixture", "query": {"kind": "chapter", "chapter_id": "ch1"}, "limit": 1,
    }))
    assert first_page["has_more"]
    service.record_generation(RecordGenerationRequest.model_validate({
        "project": "fixture", "operation_id": "repeat-submitted-evidence", "change": {
            "kind": "update", "generation_record_id": generation["generation_record_id"],
            "expected_generation_revision": updated["generation"]["generation_revision"],
            "state": "submitted", "provider_ids": {"generation_ids": ["provider-1"]},
        },
    }), owner_key="principal:fixture")
    with pytest.raises(BookServiceError) as stale_cursor:
        service.get_generations(GetGenerationsRequest.model_validate({
            "project": "fixture", "query": {"kind": "chapter", "chapter_id": "ch1"},
            "cursor": first_page["next_cursor"], "limit": 1,
        }))
    assert stale_cursor.value.reason == "invalid_cursor"
    assert second["generation"]["snapshot_id"] == prepared["snapshot_id"]


def _completed_raw_generation(service: BookService, prose: bytes, tagged: bytes) -> tuple[dict, dict]:
    inspected = _inspect(service)
    spec = {
        "provider": "synthetic", "route": "fixture", "model_id": "model",
        "voice_id": "voice", "parameters": {}, "context_fields": {},
    }
    prepared, _ = service.prepare(PrepareRequest.model_validate({
        "project": "fixture", "operation_id": "prepare-import", "chapter_id": "ch1",
        "document_view_id": inspected["document_view_id"],
        "expected_prose_sha256": hashlib.sha256(prose).hexdigest(),
        "expected_tagged_sha256": hashlib.sha256(tagged).hexdigest(),
        "expected_manifest_revision": None, "scope": {"kind": "test", "authorization_id": "test-auth"},
        "speech_selection_confirmed": True,
        "request_limit": {"value": 100, "unit": "unicode_codepoints"},
        "expected_settings_sha256": None, "production_target": None,
        "chunks": [{"chunk_id": "chunk-raw", "start": 0, "end": 5, "request_spec": spec}],
        "publish_bookmarks_to_working_tagged_docx": False,
    }), owner_key="principal:fixture")
    reserved, _ = service.record_generation(RecordGenerationRequest.model_validate({
        "project": "fixture", "operation_id": "reserve-raw", "change": {
            "kind": "reserve", "chapter_id": "ch1", "snapshot_id": prepared["snapshot_id"],
            "chunk_id": "chunk-raw", "expected_manifest_revision": prepared["manifest_revision"],
            "request": {"prompt_sha256": prepared["chunks"][0]["prompt_sha256"], "spec": spec},
        },
    }), owner_key="principal:fixture")
    submitted, _ = service.record_generation(RecordGenerationRequest.model_validate({
        "project": "fixture", "operation_id": "submit-raw", "change": {
            "kind": "update", "generation_record_id": reserved["generation"]["generation_record_id"],
            "expected_generation_revision": 1, "state": "submitted",
            "provider_ids": {"generation_ids": ["synthetic-complete"]},
            "provider_response_metadata": {"format": "synthetic raw s16le"},
        },
    }), owner_key="principal:fixture")
    completed, _ = service.record_generation(RecordGenerationRequest.model_validate({
        "project": "fixture", "operation_id": "complete-raw", "change": {
            "kind": "update", "generation_record_id": reserved["generation"]["generation_record_id"],
            "expected_generation_revision": submitted["generation"]["generation_revision"], "state": "completed",
            "provider_ids": {"generation_ids": ["synthetic-complete"]},
        },
    }), owner_key="principal:fixture")
    raw_format = {
        "container": "raw_pcm", "encoding": "signed_integer", "sample_rate_hz": 8000,
        "channels": 1, "storage_bits": 16, "valid_bits": 16, "endianness": "little",
        "interleaving": "interleaved", "provider_format_evidence": "synthetic raw s16le",
    }
    return completed["generation"], raw_format


def test_https_import_receipt_binds_url_without_persisting_it(tmp_path):
    service, prose, tagged = _fixture(tmp_path)
    generation, raw_format = _completed_raw_generation(service, prose, tagged)
    layout_path = tmp_path / "Project Files/Book_Layout.json"
    layout = json.loads(layout_path.read_text(encoding="utf-8"))
    layout["storage"]["import_https_hosts"] = ["media.example.test"]
    layout_path.write_text(json.dumps(layout), encoding="utf-8")
    url = "https://media.example.test/audio.wav?signature=synthetic-secret"
    request = ImportAudioRequest.model_validate({
        "project": "fixture", "operation_id": "https-transient-receipt",
        "generation_record_id": generation["generation_record_id"],
        "expected_generation_revision": generation["generation_revision"],
        "source": {"kind": "https_url", "url": url},
        "provenance": "native_generation", "source_format": raw_format,
    })

    first, replayed = service.import_audio(request, owner_key="principal:fixture")
    assert not replayed
    durable = service.discover_state().import_job(first["job_id"])
    assert durable["payload"]["source_kind"] == "https_url"
    assert "url" not in durable["payload"]
    assert "signature=synthetic-secret" not in json.dumps(durable["payload"])

    second, replayed = service.import_audio(request, owner_key="principal:fixture")
    assert replayed and second == first
    changed = request.model_copy(update={
        "source": request.source.model_copy(update={
            "url": "https://media.example.test/audio.wav?signature=refreshed",
        }),
    })
    with pytest.raises(BookServiceError) as conflict:
        service.import_audio(changed, owner_key="principal:fixture")
    assert conflict.value.reason == "operation_id_conflict"


def test_startup_recovery_finishes_unpolled_transient_and_cancel_requested_jobs(tmp_path):
    service, prose, tagged = _fixture(tmp_path / "transient")
    generation, raw_format = _completed_raw_generation(service, prose, tagged)
    request = ImportAudioRequest.model_validate({
        "project": "fixture", "operation_id": "unpolled-workspace-import",
        "generation_record_id": generation["generation_record_id"],
        "expected_generation_revision": generation["generation_revision"],
        "source": {"kind": "workspace", "path": "exports/audio.pcm",
                   "expected_sha256": "a" * 64},
        "provenance": "native_generation", "source_format": raw_format,
    })
    queued, _ = service.import_audio(request, owner_key="principal:fixture")
    assert service.discover_state().unfinished_import_jobs()[0]["job_id"] == queued["job_id"]
    service.recover_interrupted_import_jobs()
    failed = service.discover_state().import_job(queued["job_id"])
    assert failed["state"] == "failed"
    assert failed["error"]["reason"] == "source_unavailable"
    failed_generation = service.discover_state().generation(generation["generation_record_id"])
    assert failed_generation["import_job_id"] is None

    cancel_service, cancel_prose, cancel_tagged = _fixture(tmp_path / "cancelled")
    cancel_generation, cancel_format = _completed_raw_generation(
        cancel_service, cancel_prose, cancel_tagged,
    )
    cancel_job, _ = cancel_service.import_audio(ImportAudioRequest.model_validate({
        "project": "fixture", "operation_id": "unpolled-cancelled-import",
        "generation_record_id": cancel_generation["generation_record_id"],
        "expected_generation_revision": cancel_generation["generation_revision"],
        "source": {"kind": "workspace", "path": "exports/audio.pcm",
                   "expected_sha256": "b" * 64},
        "provenance": "native_generation", "source_format": cancel_format,
    }), owner_key="principal:fixture")
    cancel_service.cancel_job(CancelJobRequest(
        project="fixture", operation_id="request-unpolled-cancel",
        job_id=cancel_job["job_id"], expected_job_revision=1,
    ), owner_key="principal:fixture")
    cancel_service.recover_interrupted_import_jobs()
    terminal = cancel_service.discover_state().import_job(cancel_job["job_id"])
    assert terminal["state"] == "cancelled"
    assert terminal["error"]["reason"] == "cancelled"


def test_import_quota_counts_registered_audio_union_once(tmp_path):
    service, _prose, _tagged = _fixture(tmp_path)
    layout_path = tmp_path / "Project Files/Book_Layout.json"
    layout = json.loads(layout_path.read_text(encoding="utf-8"))
    (tmp_path / "Audiobook").mkdir()
    (tmp_path / "Audiobook/existing.bin").write_bytes(b"1234567890")
    layout["storage"]["quota_bytes"] = 1_000_010
    layout["storage"]["reserve_bytes"] = 0
    layout_path.write_text(json.dumps(layout), encoding="utf-8")
    current_layout = BookLayout.model_validate(layout, strict=True)
    assert len(service._registered_audio_roots(current_layout)) == 1
    service._available_import_space(current_layout, service._chapter(current_layout, "ch1"), 500_000)

    layout["storage"]["quota_bytes"] = 1_000_015
    layout_path.write_text(json.dumps(layout), encoding="utf-8")
    sibling_audio = tmp_path / "Audiobook/Other"
    sibling_audio.mkdir(parents=True)
    (sibling_audio / "retained.mp3").write_bytes(b"abcdefghij")
    current_layout = BookLayout.model_validate(layout, strict=True)
    assert len(service._registered_audio_roots(current_layout)) == 1
    assert service._retained_audio_bytes(service._registered_audio_roots(current_layout)) == 20
    with pytest.raises(BookServiceError) as quota:
        service._available_import_space(current_layout, service._chapter(current_layout, "ch1"), 500_000)
    assert quota.value.reason == "insufficient_storage"


def test_raw_pcm_project_import_is_durable_idempotent_and_cancel_safe(tmp_path):
    service, prose, tagged = _fixture(tmp_path)
    generation, raw_format = _completed_raw_generation(service, prose, tagged)
    samples = b"\x00\x00\x01\x00\xff\xff\x02\x00"
    source = tmp_path / "Audiobook/Chapters/1/provider-output.pcm"
    source.parent.mkdir(parents=True)
    source.write_bytes(samples)
    digest = hashlib.sha256(samples).hexdigest()
    request = ImportAudioRequest.model_validate({
        "project": "fixture", "operation_id": "import-raw", "generation_record_id": generation["generation_record_id"],
        "expected_generation_revision": generation["generation_revision"],
        "source": {"kind": "project_file", "filepath": "Audiobook/Chapters/1/provider-output.pcm", "expected_sha256": digest},
        "provenance": "native_generation", "source_format": raw_format,
    })
    queued, replayed = service.import_audio(request, owner_key="principal:fixture")
    assert not replayed and queued["state"] == "queued"
    replay, replayed = service.import_audio(request, owner_key="principal:fixture")
    assert replayed and replay == queued
    with pytest.raises(BookServiceError) as altered:
        service.import_audio(ImportAudioRequest.model_validate({
            **request.model_dump(mode="json"),
            "source": {"kind": "project_file", "filepath": "Audiobook/Chapters/1/provider-output.pcm", "expected_sha256": "0" * 64},
        }), owner_key="principal:fixture")
    assert altered.value.reason == "operation_id_conflict"

    cancel, replayed = service.cancel_job(CancelJobRequest(
        project="fixture", operation_id="cancel-raw", job_id=queued["job_id"], expected_job_revision=1,
    ), owner_key="principal:fixture")
    assert not replayed and cancel["state"] == "cancel_requested"
    pending_cancel = service.get_job(GetJobRequest(project="fixture", job_id=queued["job_id"]))
    assert pending_cancel["state"] == "running" and pending_cancel["phase"] == "cancel_requested"
    service.run_import_job(queued["job_id"])
    assert service.get_job(GetJobRequest(project="fixture", job_id=queued["job_id"]))["state"] == "cancelled"
    retry_generation = service.get_generations(GetGenerationsRequest.model_validate({
        "project": "fixture", "query": {"kind": "record", "generation_record_id": generation["generation_record_id"]},
    }))["generations"][0]
    assert retry_generation["media_registered"] is False and retry_generation["import_job_id"] is None
    retry_request = request.model_copy(update={
        "operation_id": "import-raw-retry",
        "expected_generation_revision": retry_generation["generation_revision"],
    })
    queued, replayed = service.import_audio(retry_request, owner_key="principal:fixture")
    assert not replayed and queued["job_id"] != cancel["job_id"]

    # A fresh service instance opens the same FULL/rollback-journal authority;
    # it can complete a queued local job without reconstructing a provider call.
    restarted = BookService(tmp_path, "fixture")
    restarted.run_import_job(queued["job_id"])
    completed = restarted.get_job(GetJobRequest(project="fixture", job_id=queued["job_id"]))
    assert completed["state"] == "succeeded"
    take = completed["result"]["take"]
    assert take["media"]["container"] == "raw_pcm"
    assert take["media"]["canonical_sample_sha256"] == hashlib.sha256(samples).hexdigest()
    assert (tmp_path / take["filepath"]).read_bytes() == samples
    wrapper = take["assembly_derivative"]
    assert wrapper is not None and (tmp_path / wrapper["filepath"]).is_file()
    assert wrapper["media"]["canonical_sample_sha256"] == take["media"]["canonical_sample_sha256"]
    assert inspect_media_file(tmp_path / wrapper["filepath"]).media.canonical_sample_sha256 == hashlib.sha256(samples).hexdigest()
    generation_after = restarted.get_generations(GetGenerationsRequest.model_validate({
        "project": "fixture", "query": {"kind": "record", "generation_record_id": generation["generation_record_id"]},
    }))["generations"][0]
    assert generation_after["media_registered"] is True and generation_after["take_id"] == take["take_id"]


def test_import_preserves_provider_evidence_added_after_reservation(tmp_path):
    service, prose, tagged = _fixture(tmp_path)
    generation, raw_format = _completed_raw_generation(service, prose, tagged)
    samples = b"\x00\x00\x01\x00\xfe\xff\x02\x00"
    source = tmp_path / "Audiobook/Chapters/1/provider-output.pcm"
    source.parent.mkdir(parents=True)
    source.write_bytes(samples)
    request = ImportAudioRequest.model_validate({
        "project": "fixture", "operation_id": "import-late-evidence",
        "generation_record_id": generation["generation_record_id"],
        "expected_generation_revision": generation["generation_revision"],
        "source": {"kind": "project_file", "filepath": "Audiobook/Chapters/1/provider-output.pcm",
                   "expected_sha256": hashlib.sha256(samples).hexdigest()},
        "provenance": "native_generation", "source_format": raw_format,
    })
    queued, replayed = service.import_audio(request, owner_key="principal:fixture")
    assert not replayed

    def add_late_evidence():
        latest = service.discover_state().generation(generation["generation_record_id"])
        updated, replayed = service.record_generation(RecordGenerationRequest.model_validate({
            "project": "fixture", "operation_id": "late-provider-evidence", "change": {
                "kind": "update", "generation_record_id": generation["generation_record_id"],
                "expected_generation_revision": latest["generation_revision"], "state": "completed",
                "provider_ids": {"generation_ids": ["late-generation-id"]},
                "provider_response_metadata": {"observed_after_import_reservation": True},
            },
        }), owner_key="principal:fixture")
        assert not replayed
        return updated["generation"]

    late_evidence = {}

    def before_finalize():
        late_evidence["generation"] = add_late_evidence()

    service.run_import_job(queued["job_id"], before_finalize=before_finalize)
    completed = service.get_job(GetJobRequest(project="fixture", job_id=queued["job_id"]))
    assert completed["state"] == "succeeded", completed
    current = service.discover_state().generation(generation["generation_record_id"])
    assert current["generation_revision"] == late_evidence["generation"]["generation_revision"] + 1
    assert current["provider_ids"]["generation_ids"] == ["synthetic-complete", "late-generation-id"]
    assert current["provider_response_metadata"] == {"observed_after_import_reservation": True}
    assert current["media_registered"] is True and current["take_id"] == completed["result"]["take"]["take_id"]


def test_headered_pcm_import_detects_format_rejects_conflicting_rawformat_and_retains_bytes(tmp_path):
    service, prose, tagged = _fixture(tmp_path)
    generation, raw_format = _completed_raw_generation(service, prose, tagged)
    samples = struct.pack("<hhhh", 1, -2, 3, -4)
    original = _wav_pcm(samples)
    source = tmp_path / "Audiobook/Chapters/1/provider-output.wav"
    source.parent.mkdir(parents=True)
    source.write_bytes(original)
    digest = hashlib.sha256(original).hexdigest()
    conflicting = {**raw_format, "channels": 2}
    rejected = ImportAudioRequest.model_validate({
        "project": "fixture", "operation_id": "import-wav-conflict",
        "generation_record_id": generation["generation_record_id"],
        "expected_generation_revision": generation["generation_revision"],
        "source": {"kind": "project_file", "filepath": "Audiobook/Chapters/1/provider-output.wav", "expected_sha256": digest},
        "provenance": "native_generation", "source_format": conflicting,
    })
    failed_job, _ = service.import_audio(rejected, owner_key="principal:fixture")
    service.run_import_job(failed_job["job_id"])
    failed = service.get_job(GetJobRequest(project="fixture", job_id=failed_job["job_id"]))
    assert failed["state"] == "failed" and failed["error"]["reason"] == "media_mismatch"
    assert source.read_bytes() == original
    generation_after = service.get_generations(GetGenerationsRequest.model_validate({
        "project": "fixture", "query": {"kind": "record", "generation_record_id": generation["generation_record_id"]},
    }))["generations"][0]
    accepted = ImportAudioRequest.model_validate({
        "project": "fixture", "operation_id": "import-wav-detected",
        "generation_record_id": generation["generation_record_id"],
        "expected_generation_revision": generation_after["generation_revision"],
        "source": {"kind": "project_file", "filepath": "Audiobook/Chapters/1/provider-output.wav", "expected_sha256": digest},
        "provenance": "native_generation",
    })
    queued, _ = service.import_audio(accepted, owner_key="principal:fixture")
    service.run_import_job(queued["job_id"])
    completed = service.get_job(GetJobRequest(project="fixture", job_id=queued["job_id"]))
    assert completed["state"] == "succeeded", completed
    take = completed["result"]["take"]
    assert take["filepath"].endswith("/native.wav")
    assert take["media"]["container"] == "wav"
    assert take["media"]["canonical_sample_sha256"] == hashlib.sha256(samples).hexdigest()
    assert take["assembly_derivative"] is None
    assert (tmp_path / take["filepath"]).read_bytes() == original


@pytest.mark.parametrize("provenance", ["test_mp3", "derived_audio"])
def test_project_mp3_import_uses_detected_facts_and_preserves_original_bytes(tmp_path, monkeypatch, provenance):
    service, prose, tagged = _fixture(tmp_path)
    generation, _raw_format = _completed_raw_generation(service, prose, tagged)
    source = tmp_path / "Audiobook/Chapters/1/provider-output.mp3"
    original = b"ID3\x04\x00\x00synthetic-mp3-payload"
    source.parent.mkdir(parents=True)
    source.write_bytes(original)

    async def fake_probe(_executable, filepath, **_kwargs):
        assert Path(filepath).is_file()
        return {
            "streams": [{"codec_type": "audio", "codec_name": "mp3", "sample_rate": "44100",
                         "channels": 1, "duration": "0.25", "bit_rate": "96000", "nb_frames": "10"}],
            "format": {"format_name": "mp3", "duration": "0.25", "bit_rate": "96000"},
        }

    monkeypatch.setattr(service, "_registered_media_executables", lambda: (source, source))
    monkeypatch.setattr(service_module, "ffprobe_json", fake_probe)
    request = ImportAudioRequest.model_validate({
        "project": "fixture", "operation_id": f"import-{provenance}",
        "generation_record_id": generation["generation_record_id"],
        "expected_generation_revision": generation["generation_revision"],
        "source": {"kind": "project_file", "filepath": "Audiobook/Chapters/1/provider-output.mp3",
                   "expected_sha256": hashlib.sha256(original).hexdigest()},
        "provenance": provenance,
    })
    queued, _ = service.import_audio(request, owner_key="principal:fixture")
    service.run_import_job(queued["job_id"])
    completed = service.get_job(GetJobRequest(project="fixture", job_id=queued["job_id"]))
    assert completed["state"] == "succeeded", completed
    take = completed["result"]["take"]
    assert take["provenance"] == provenance
    assert take["filepath"].endswith("/native.mp3")
    assert take["media"]["codec"] == "mp3" and take["media"]["encoding"] == "compressed"
    assert take["assembly_derivative"] is None
    assert (tmp_path / take["filepath"]).read_bytes() == original
    assert source.read_bytes() == original


def test_native_generation_mp3_is_retained_but_production_pcm_build_rejects_it(tmp_path, monkeypatch):
    service, prose, tagged = _fixture(tmp_path)
    generation, _raw_format = _completed_raw_generation(service, prose, tagged)
    source = tmp_path / "Audiobook/Chapters/1/mislabelled.mp3"
    original = b"ID3\x04\x00\x00mislabelled-compressed"
    source.parent.mkdir(parents=True)
    source.write_bytes(original)

    async def fake_probe(_executable, _filepath, **_kwargs):
        return {"streams": [{"codec_type": "audio", "codec_name": "mp3", "sample_rate": "44100",
                              "channels": 1, "duration": "0.25", "bit_rate": "96000"}],
                "format": {"format_name": "mp3", "duration": "0.25"}}

    monkeypatch.setattr(service, "_registered_media_executables", lambda: (source, source))
    monkeypatch.setattr(service_module, "ffprobe_json", fake_probe)
    request = ImportAudioRequest.model_validate({
        "project": "fixture", "operation_id": "reject-mislabelled-mp3",
        "generation_record_id": generation["generation_record_id"],
        "expected_generation_revision": generation["generation_revision"],
        "source": {"kind": "project_file", "filepath": "Audiobook/Chapters/1/mislabelled.mp3",
                   "expected_sha256": hashlib.sha256(original).hexdigest()},
        "provenance": "native_generation",
    })
    queued, _ = service.import_audio(request, owner_key="principal:fixture")
    service.run_import_job(queued["job_id"])
    result = service.get_job(GetJobRequest(project="fixture", job_id=queued["job_id"]))
    assert result["state"] == "succeeded", result
    take = result["result"]["take"]
    assert take["media"]["encoding"] == "compressed" and take["provenance"] == "native_generation"
    assert source.read_bytes() == original
    state = ProjectState.discover(tmp_path)
    assert state is not None
    snapshot = state.snapshot(take["snapshot_id"])
    assert snapshot is not None
    with pytest.raises(BookServiceError) as rejected:
        service.build(BuildRequest.model_validate({
            "project": "fixture", "operation_id": "reject-compressed-production", "expected_head_revision": None,
            "input": {"kind": "chapter", "chapter_id": "ch1", "snapshot_id": take["snapshot_id"],
                      "expected_manifest_revision": snapshot["manifest_revision"],
                      "request_plan_sha256": snapshot["payload"]["result"]["request_plan_sha256"],
                      "takes": [{"chunk_id": take["chunk_id"], "take_id": take["take_id"], "request_sha256": take["request_sha256"]}]},
            "mode": "production_pcm", "outputs": {"master": True}, "gaps": [],
            "metadata": {"title": "Fixture", "author": "Fixture", "edition": "test"},
        }), owner_key="principal:fixture")
    assert rejected.value.reason == "native_pcm_required"


def test_test_mp3_stream_copy_build_is_candidate_until_explicit_commit(tmp_path, monkeypatch):
    service, prose, tagged = _fixture(tmp_path)
    generation, _ = _completed_raw_generation(service, prose, tagged)
    source = tmp_path / "Audiobook/Chapters/1/test.mp3"
    raw = b"ID3\x04\x00\x00packet-copy-fixture"
    source.parent.mkdir(parents=True)
    source.write_bytes(raw)
    probe = {"streams": [{"codec_type": "audio", "codec_name": "mp3", "sample_rate": "44100",
                          "channels": 1, "duration": "0.25", "bit_rate": "96000", "nb_frames": "2"}],
             "format": {"format_name": "mp3", "duration": "0.25", "bit_rate": "96000"}}
    async def fake_probe(_executable, _filepath, **_kwargs): return probe
    async def fake_packets(_executable, _filepath, **_kwargs): return (PacketFact(10, "a" * 64),)
    async def fake_run(argv, **_kwargs):
        if argv[-1] == "-version":
            return SimpleNamespace(cancelled=False, timed_out=False, returncode=0,
                                   stdout=b"ffmpeg version 7.1 fixture\n", stderr=b"", stdout_truncated=False)
        Path(argv[-1]).write_bytes(raw)
        return SimpleNamespace(cancelled=False, timed_out=False, returncode=0)
    from cognita.books.mp3_validation import (
        Mp3DecoderFileFacts, Mp3DecoderVerification, Mp3DecodedFrame,
        Mp3Packet, Mp3ToolInvocation,
    )
    async def fake_decoder(ffmpeg, ffprobe, source_paths, output_path, **_kwargs):
        packet = Mp3Packet(0, 10, "a" * 64, 0, 0)
        source_facts = Mp3DecoderFileFacts(
            str(source_paths[0]), "mp3", 44_100, 1, 1_152, 1_152 / 44_100,
            1, 0, 0, (packet,), (Mp3DecodedFrame(0, 0, 1_152),),
        )
        output_facts = Mp3DecoderFileFacts(
            str(output_path), "mp3", 44_100, 1, 1_152, 1_152 / 44_100,
            1, 0, 0, (packet,), (Mp3DecodedFrame(0, 0, 1_152),),
        )
        return Mp3DecoderVerification(
            "mp3-decoder-v1", "checked", True, True, True, (source_facts,), output_facts,
            (), (Mp3ToolInvocation("ffmpeg", "output:decode_to_null", (str(ffmpeg), "-version")),),
        )
    monkeypatch.setattr(service, "_registered_media_executables", lambda: (source, source))
    monkeypatch.setattr(service_module, "ffprobe_json", fake_probe)
    monkeypatch.setattr(service_module, "ffprobe_packet_facts", fake_packets)
    monkeypatch.setattr(service_module, "run_process", fake_run)
    monkeypatch.setattr(service_module, "verify_chapter_mp3_decoder", fake_decoder)
    imported, _ = service.import_audio(ImportAudioRequest.model_validate({
        "project": "fixture", "operation_id": "test-mp3-import", "generation_record_id": generation["generation_record_id"],
        "expected_generation_revision": generation["generation_revision"],
        "source": {"kind": "project_file", "filepath": "Audiobook/Chapters/1/test.mp3", "expected_sha256": hashlib.sha256(raw).hexdigest()},
        "provenance": "test_mp3",
    }), owner_key="principal:fixture")
    service.run_import_job(imported["job_id"])
    take = service.get_job(GetJobRequest(project="fixture", job_id=imported["job_id"]))["result"]["take"]
    state = ProjectState.discover(tmp_path)
    assert state is not None
    snapshot = state.snapshot(take["snapshot_id"])
    assert snapshot is not None
    build, replayed = service.build(BuildRequest.model_validate({
        "project": "fixture", "operation_id": "test-mp3-build", "expected_head_revision": None,
        "input": {"kind": "chapter", "chapter_id": "ch1", "snapshot_id": take["snapshot_id"],
                  "expected_manifest_revision": snapshot["manifest_revision"], "request_plan_sha256": snapshot["payload"]["result"]["request_plan_sha256"],
                  "takes": [{"chunk_id": take["chunk_id"], "take_id": take["take_id"], "request_sha256": take["request_sha256"]}]},
        "mode": "test_mp3_stream_copy", "outputs": {"master": False}, "gaps": [],
        "metadata": {"title": "Fixture", "author": "Fixture", "edition": "test"},
    }), owner_key="principal:fixture")
    assert not replayed
    service.run_build_job(build["job_id"])
    candidate = service.get_job(GetJobRequest(project="fixture", job_id=build["job_id"]))
    assert candidate["state"] == "succeeded", candidate
    result = candidate["result"]
    assert [item["kind"] for item in result["outputs"]] == ["mp3_download"]
    build_dir = tmp_path / result["outputs"][0]["filepath"]
    build_facts = json.loads((build_dir.parent / "build.json").read_text(encoding="utf-8"))
    timeline = json.loads((build_dir.parent / "timeline.json").read_text(encoding="utf-8"))
    assert build_facts["recipe"]["tools"]["ffmpeg"]["version"] == "ffmpeg version 7.1 fixture"
    assert build_facts["recipe"]["settings"]["stream_copy"] is True
    assert build_facts["recipe"]["processing_argv"]
    assert timeline["delay_padding_verified"] is True
    assert timeline["seam_quality_assessed"] is False
    assert timeline["sample_rate_hz"] == 44_100
    assert timeline["entries"] == [{"kind": "audio", "source_id": take["chunk_id"],
                                    "start_frame": 0, "end_frame": 1_152}]
    assert timeline["decoder_verification"]["output"]["frames"][0]["decoded_end_frame"] == 1152
    assert timeline["decoder_verification"]["output"]["packets"][0]["data_sha256"] == "a" * 64
    assert state.chapter_head("ch1", snapshot["scope_key"]) is None
    committed, _ = service.commit_build(CommitBuildRequest.model_validate({
        "project": "fixture", "operation_id": "test-mp3-commit", "build_id": result["build_id"],
        "expected_head_revision": None, "intent": "accept_candidate", "acceptance": {"actor": "fixture",
        "accepted_at": datetime.now(timezone.utc).isoformat(), "listening_review": "passed", "notes": ["packet proof"]},
    }), owner_key="principal:fixture")
    assert committed["head_revision"] == 1

def test_chapter_pcm_build_requires_explicit_current_head_commit_and_can_roll_back(tmp_path):
    service, prose, tagged = _fixture(tmp_path)
    generation, raw_format = _completed_raw_generation(service, prose, tagged)
    samples = b"\x00\x00\x01\x00\xff\xff\x02\x00"
    source = tmp_path / "Audiobook/Chapters/1/chapter-build-source.pcm"
    source.parent.mkdir(parents=True)
    source.write_bytes(samples)
    import_request = ImportAudioRequest.model_validate({
        "project": "fixture", "operation_id": "import-for-build",
        "generation_record_id": generation["generation_record_id"],
        "expected_generation_revision": generation["generation_revision"],
        "source": {"kind": "project_file", "filepath": "Audiobook/Chapters/1/chapter-build-source.pcm",
                   "expected_sha256": hashlib.sha256(samples).hexdigest()},
        "provenance": "native_generation", "source_format": raw_format,
    })
    import_job, _ = service.import_audio(import_request, owner_key="principal:fixture")
    service.run_import_job(import_job["job_id"])
    take = service.get_job(GetJobRequest(project="fixture", job_id=import_job["job_id"]))["result"]["take"]
    state = ProjectState.discover(tmp_path)
    assert state is not None
    generation_row = state.generation(generation["generation_record_id"])
    snapshot_id = generation_row["snapshot_id"]
    snapshot = state.snapshot(snapshot_id)
    assert snapshot is not None
    build_request = BuildRequest.model_validate({
        "project": "fixture", "operation_id": "build-one", "expected_head_revision": None,
        "input": {"kind": "chapter", "chapter_id": "ch1", "snapshot_id": snapshot_id,
                  "expected_manifest_revision": snapshot["manifest_revision"],
                  "request_plan_sha256": snapshot["payload"]["result"]["request_plan_sha256"],
                  "takes": [{"chunk_id": "chunk-raw", "take_id": take["take_id"],
                             "request_sha256": take["request_sha256"]}]},
        "mode": "production_pcm", "outputs": {"master": True}, "gaps": [],
        "metadata": {"title": "Fixture", "author": "Fixture", "edition": "test"},
    })
    queued, replayed = service.build(build_request, owner_key="principal:fixture")
    assert not replayed and queued["state"] == "queued"
    assert service.build(build_request, owner_key="principal:fixture") == (queued, True)
    service.run_build_job(queued["job_id"])
    built = service.get_job(GetJobRequest(project="fixture", job_id=queued["job_id"]))
    assert built["state"] == "succeeded", built
    candidate = built["result"]
    assert (tmp_path / candidate["outputs"][0]["filepath"]).read_bytes() == samples
    build_directory = (tmp_path / candidate["timeline_filepath"]).parent
    durable_build = json.loads((build_directory / "build.json").read_text(encoding="utf-8"))
    recipe = durable_build["recipe"]
    assert recipe["scope"] == "chapter" and recipe["mode"] == "production_pcm"
    assert recipe["processing_argv"] is None and recipe["tools"] == {}
    assert recipe["inputs"][0]["bytes_sha256"] == take["bytes_sha256"]
    assert recipe["timeline"] == json.loads((tmp_path / candidate["timeline_filepath"]).read_text(encoding="utf-8"))
    assert candidate["recipe_sha256"] == canonical_json_sha256(recipe)
    assert state.chapter_head("ch1", snapshot["scope_key"]) is None
    accepted_at = datetime.now(timezone.utc).isoformat()
    committed, replayed = service.commit_build(CommitBuildRequest.model_validate({
        "project": "fixture", "operation_id": "accept-one", "build_id": candidate["build_id"],
        "expected_head_revision": None, "intent": "accept_candidate",
        "acceptance": {"actor": "fixture", "accepted_at": accepted_at,
                       "listening_review": "passed", "notes": ["synthetic"]},
    }), owner_key="principal:fixture")
    assert not replayed and committed["head_revision"] == 1
    replay, replayed = service.commit_build(CommitBuildRequest.model_validate({
        "project": "fixture", "operation_id": "accept-one", "build_id": candidate["build_id"],
        "expected_head_revision": None, "intent": "accept_candidate",
        "acceptance": {"actor": "fixture", "accepted_at": accepted_at,
                       "listening_review": "passed", "notes": ["synthetic"]},
    }), owner_key="principal:fixture")
    assert replayed and replay == committed
    with pytest.raises(BookServiceError) as stale:
        service.commit_build(CommitBuildRequest.model_validate({
            "project": "fixture", "operation_id": "accept-stale", "build_id": candidate["build_id"],
            "expected_head_revision": None, "intent": "accept_candidate",
            "acceptance": {"actor": "fixture", "accepted_at": datetime.now(timezone.utc).isoformat(),
                           "listening_review": "passed", "notes": []},
        }), owner_key="principal:fixture")
    assert stale.value.reason == "stale_head"
    rolled, _ = service.commit_build(CommitBuildRequest.model_validate({
        "project": "fixture", "operation_id": "rollback-one", "build_id": candidate["build_id"],
        "expected_head_revision": 1, "intent": "rollback",
        "acceptance": {"actor": "fixture", "accepted_at": datetime.now(timezone.utc).isoformat(),
                       "listening_review": "passed", "notes": ["same prose"]},
    }), owner_key="principal:fixture")
    assert rolled["head_revision"] == 2 and rolled["accepted_plan_matches_prepared"] is True
    chapter = service.get_chapter(GetChapterRequest.model_validate({
        "project": "fixture", "chapter_id": "ch1", "scope": {"kind": "test", "authorization_id": "test-auth"},
        "snapshot_id": snapshot_id, "include_text": True,
    }))
    assert chapter["accepted_build_id"] == candidate["build_id"]
    assert chapter["source_status"] == "eligible"
    assert chapter["takes"][0]["take_id"] == take["take_id"]
    assert chapter["returned_texts"][0]["spoken_text"]["text"] == "hello"
    # One shared cap pages the frozen prompt and tag-removed spoken text
    # without switching to the current working DOCX or dropping a suffix.
    cursor = None
    prompt_pages, spoken_pages = [], []
    while True:
        arguments = {
            "project": "fixture", "chapter_id": "ch1",
            "scope": {"kind": "test", "authorization_id": "test-auth"},
            "snapshot_id": snapshot_id, "include_text": True, "max_characters": 1,
        }
        if cursor is not None:
            arguments["cursor"] = cursor
        text_page = service.get_chapter(GetChapterRequest.model_validate(arguments))
        for item in text_page["returned_texts"]:
            prompt_pages.append(item["prompt"]["text"])
            spoken_pages.append(item["spoken_text"]["text"])
        if not text_page["has_more"]:
            break
        cursor = text_page["next_cursor"]
    assert "".join(prompt_pages) == "hello"
    assert "".join(spoken_pages) == "hello"
    first_metadata_page = service.get_chapter(GetChapterRequest.model_validate({
        "project": "fixture", "chapter_id": "ch1",
        "scope": {"kind": "test", "authorization_id": "test-auth"},
        "snapshot_id": snapshot_id, "limit": 1,
    }))
    assert len(first_metadata_page["chunks"]) == 1 and first_metadata_page["has_more"]
    next_metadata_page = service.get_chapter(GetChapterRequest.model_validate({
        "project": "fixture", "chapter_id": "ch1",
        "scope": {"kind": "test", "authorization_id": "test-auth"},
        "snapshot_id": snapshot_id, "limit": 1, "cursor": first_metadata_page["next_cursor"],
    }))
    assert next_metadata_page["takes"] == [take]
    with pytest.raises(BookServiceError) as cross_namespace:
        service.get_chapter(GetChapterRequest.model_validate({
            "project": "fixture", "chapter_id": "ch1", "snapshot_id": snapshot_id,
        }))
    assert cross_namespace.value.reason == "snapshot_not_found"
    located = service.find_chunk(FindChunkRequest.model_validate({
        "project": "fixture", "query": {"kind": "timestamp", "build_id": candidate["build_id"], "seconds": 0.0},
    }))
    assert located["matches"][0]["matched_take_ids"] == [take["take_id"]]
    with pytest.raises(BookServiceError) as past_end:
        service.find_chunk(FindChunkRequest.model_validate({
            "project": "fixture", "query": {"kind": "timestamp", "build_id": candidate["build_id"], "seconds": 1.0},
        }))
    assert past_end.value.reason == "past_end"
    quote = service.find_chunk(FindChunkRequest.model_validate({
        "project": "fixture", "chapter_id": "ch1",
        "query": {"kind": "quote", "text": "hello", "snapshot_id": snapshot_id},
    }))
    assert quote["searched_version"] == snapshot_id
    assert quote["matches"][0]["occurrence_start"] == 0
    with pytest.raises(BookServiceError) as missing_chapter:
        service.find_chunk(FindChunkRequest.model_validate({
            "project": "fixture", "query": {"kind": "quote", "text": "hello"},
        }))
    assert missing_chapter.value.reason == "validation_failed"
    assert located["matches"][0]["coordinate_projection"] == "timeline"

def test_index_status_reports_pending_and_blocked_registered_sources(tmp_path):
    service, _prose, _tagged = _fixture(tmp_path, bound=True)
    ProjectState.initialize(tmp_path)
    request = IndexStatusRequest.model_validate({"project": "fixture"})
    pending = service.index_status(request, indexed_sources=set())
    assert pending["entries"]
    assert all(entry["index_state"] == "pending" for entry in pending["entries"])
    blocked = service.index_status(request, indexed_sources=None)
    assert all(entry["index_state"] == "blocked" for entry in blocked["entries"])
    assert all(entry["error"]["code"] == "index_unavailable" for entry in blocked["entries"])

    state = ProjectState.discover(tmp_path)
    assert state is not None
    state.begin_managed_write(
        job_id="failed-write", source_path="Project Files/ref.md", bytes_sha256="a" * 64,
        operation_id="write-ref",
    )
    state.finish_managed_write(
        job_id="failed-write", state="failed",
        error={"code": "index_failed", "message": "Synthetic index failure."},
        doc_id=None, extracted_sha256=None,
    )
    failed = service.index_status(
        IndexStatusRequest.model_validate({"project": "fixture", "filepath": "Project Files/ref.md"}),
        indexed_sources=set(),
    )
    assert failed["entries"][0]["index_state"] == "failed"
    assert failed["entries"][0]["error"]["code"] == "index_failed"


def test_index_status_binds_pages_and_reports_stale_and_excluded_sources(tmp_path):
    from cognita.books.policy import EffectiveIndexPolicy
    from cognita.parsing import parse_file

    service, _prose, _tagged = _fixture(tmp_path, bound=True)
    ProjectState.initialize(tmp_path)
    path = "Project Files/ref.md"
    parsed = parse_file(tmp_path / path, tmp_path)
    assert parsed is not None
    raw_sha = hashlib.sha256((tmp_path / path).read_bytes()).hexdigest()
    assert service.record_index_provenance(
        path, parsed.doc_id, parsed.content_hash, raw_sha, "fixture-extraction-v1",
    ) is not None

    first = service.index_status(
        IndexStatusRequest.model_validate({"project": "fixture", "limit": 1}),
        indexed_sources={path},
    )
    assert len(first["entries"]) == 1 and first["has_more"] is True
    second = service.index_status(
        IndexStatusRequest.model_validate({
            "project": "fixture", "limit": 1, "cursor": first["next_cursor"],
        }),
        indexed_sources={path},
    )
    assert second["entries"][0]["filepath"] != first["entries"][0]["filepath"]

    (tmp_path / path).write_text("Reference changed", encoding="utf-8")
    stale = service.index_status(
        IndexStatusRequest.model_validate({"project": "fixture", "filepath": path}),
        indexed_sources={path},
    )
    assert stale["entries"][0]["index_state"] == "stale"
    assert stale["entries"][0]["error"]["code"] == "source_changed"
    with pytest.raises(BookServiceError) as invalid:
        service.index_status(
            IndexStatusRequest.model_validate({
                "project": "fixture", "limit": 1, "cursor": first["next_cursor"],
            }),
            indexed_sources={path},
        )
    assert invalid.value.reason == "invalid_cursor"

    policy = EffectiveIndexPolicy(
        [{"path": "Project Files", "indexed": False}],
        book_layout=service.config().layout,
    )
    excluded = service.index_status(
        IndexStatusRequest.model_validate({"project": "fixture", "filepath": path}),
        indexed_sources={path}, effective_index=policy,
    )
    assert excluded["entries"][0]["index_state"] == "excluded"
    assert excluded["entries"][0]["effective_rule"] == "folder_exclusion:project files"
