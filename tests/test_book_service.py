from __future__ import annotations

import hashlib
import io
import json
import zipfile
from datetime import datetime, timedelta, timezone

import pytest

from cognita.books.models import GetGenerationsRequest, InspectRequest, PrepareRequest, RecordGenerationRequest
from cognita.books.service import BookService, BookServiceError
from cognita.books.state import ProjectState, ProjectStateError
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
