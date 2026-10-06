from __future__ import annotations

import hashlib
import io
import json
import struct
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

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
from cognita.books.service import BookService, BookServiceError
from cognita.books.state import ProjectState, ProjectStateError
from cognita.books.media import inspect_media_file
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


def test_native_generation_label_cannot_admit_detected_compressed_mp3(tmp_path, monkeypatch):
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
    assert result["state"] == "failed" and result["error"]["reason"] == "native_pcm_required"
    assert source.read_bytes() == original

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
    assert chapter["takes"][0]["take_id"] == take["take_id"]
    assert chapter["returned_texts"][0]["spoken_text"]["text"] == "hello"
    located = service.find_chunk(FindChunkRequest.model_validate({
        "project": "fixture", "query": {"kind": "timestamp", "build_id": candidate["build_id"], "seconds": 0.0},
    }))
    assert located["matches"][0]["matched_take_ids"] == [take["take_id"]]
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
