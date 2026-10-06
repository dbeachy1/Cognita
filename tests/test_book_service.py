from __future__ import annotations

import base64
import asyncio
import hashlib
import io
import json
import struct
import threading
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
from cognita.books.service import BookService, BookServiceError, _validate_durable_provider_facts
from cognita.books.config import BookLayout
from cognita.books.state import ProjectState, ProjectStateError
from cognita.books.media import inspect_media_file
from cognita.books.docx import FileLockedError, parse_docx
from cognita.books.projection import project_docx_pair
from cognita.books.jobs import PacketFact
from cognita.books.fingerprint import canonical_json_sha256, request_fingerprint
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


def _docx_paragraphs(*texts: str) -> bytes:
    body = "".join(f"<w:p><w:r><w:t>{text}</w:t></w:r></w:p>" for text in texts)
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<w:document xmlns:w="{W}"><w:body>{body}<w:sectPr/></w:body></w:document>'
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


def _prepare_test_plan(service, operation_id, prose, tagged, expected_revision, chunks, *, publish=False, authorization_id="test-auth", request_limit=100):
    inspected = _inspect(service)
    request = PrepareRequest.model_validate({
        "project": "fixture", "operation_id": operation_id, "chapter_id": "ch1",
        "document_view_id": inspected["document_view_id"],
        "expected_prose_sha256": hashlib.sha256(prose).hexdigest(),
        "expected_tagged_sha256": hashlib.sha256(tagged).hexdigest(),
        "expected_manifest_revision": expected_revision,
        "scope": {"kind": "test", "authorization_id": authorization_id},
        "speech_selection_confirmed": True,
        "request_limit": {"value": request_limit, "unit": "unicode_codepoints"},
        "expected_settings_sha256": None, "production_target": None,
        "chunks": chunks, "publish_bookmarks_to_working_tagged_docx": publish,
    })
    return service.prepare(request, owner_key="principal:fixture")[0]


def _authorized_alternate_pair(root: Path, *, authorization_id: str = "alternate-auth") -> tuple[bytes, bytes]:
    """Register a test-only pair without changing the production chapter paths."""
    prose = _docx("alternate prose")
    tagged = _docx("alternate prose")
    alternate = root / "Chapters/1/Test"
    alternate.mkdir(parents=True)
    (alternate / "chapter.docx").write_bytes(prose)
    (alternate / "chapter_audio-tags.docx").write_bytes(tagged)
    layout_path = root / "Project Files/Book_Layout.json"
    layout = json.loads(layout_path.read_text(encoding="utf-8"))
    now = datetime.now(timezone.utc)
    layout["test_authorizations"].append({
        "authorization_id": authorization_id, "chapter_id": "ch1",
        "prose_filepath": "Chapters/1/Test/chapter.docx",
        "tagged_filepath": "Chapters/1/Test/chapter_audio-tags.docx",
        "allowed_paragraph_ordinals": [0],
        "source_raw_sha256": hashlib.sha256(prose).hexdigest(),
        "actor": "fixture", "authorized_at": (now - timedelta(minutes=1)).isoformat(),
        "expires_at": (now + timedelta(hours=1)).isoformat(), "revoked": False,
    })
    layout_path.write_text(json.dumps(layout), encoding="utf-8")
    return prose, tagged


def _prepare_alternate_test_plan(
    service: BookService, prose: bytes, tagged: bytes, *, operation_id: str, publish: bool = False,
):
    initial = service.inspect(InspectRequest.model_validate({
        "project": "fixture", "chapter_id": "ch1",
        "prose_filepath": "Chapters/1/Test/chapter.docx",
        "tagged_filepath": "Chapters/1/Test/chapter_audio-tags.docx",
    }))
    refined = service.inspect(InspectRequest.model_validate({
        "project": "fixture", "chapter_id": "ch1",
        "prose_filepath": "Chapters/1/Test/chapter.docx",
        "tagged_filepath": "Chapters/1/Test/chapter_audio-tags.docx",
        "base_document_view_id": initial["document_view_id"],
        "speech_paragraph_ids": [initial["paragraphs"][0]["paragraph_id"]],
    }))
    return service.prepare(PrepareRequest.model_validate({
        "project": "fixture", "operation_id": operation_id, "chapter_id": "ch1",
        "document_view_id": refined["document_view_id"],
        "expected_prose_sha256": hashlib.sha256(prose).hexdigest(),
        "expected_tagged_sha256": hashlib.sha256(tagged).hexdigest(),
        "expected_manifest_revision": None,
        "scope": {"kind": "test", "authorization_id": "alternate-auth"},
        "speech_selection_confirmed": True,
        "request_limit": {"value": 100, "unit": "unicode_codepoints"},
        "expected_settings_sha256": None, "production_target": None,
        "chunks": [{"chunk_id": "alternate", "start": 0, "end": len(refined["speech_text"]),
                    "request_spec": {
                        "provider": "synthetic", "route": "fixture", "model_id": "model",
                        "voice_id": "alternate", "parameters": {}, "context_fields": {"language": "en"},
                    }}],
        "publish_bookmarks_to_working_tagged_docx": publish,
    }), owner_key="principal:fixture")[0]


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


def _accept_lookup_plan(service, prepared, *, prefix, expected_head):
    takes = [_import_native_take(service, prepared, chunk["chunk_id"], operation_prefix=f"{prefix}-{index}")
             for index, chunk in enumerate(prepared["chunks"])]
    queued, _ = service.build(BuildRequest.model_validate({
        "project": "fixture", "operation_id": f"{prefix}-build", "expected_head_revision": expected_head,
        "input": {"kind": "chapter", "chapter_id": "ch1", "snapshot_id": prepared["snapshot_id"],
                  "expected_manifest_revision": prepared["manifest_revision"],
                  "request_plan_sha256": prepared["request_plan_sha256"],
                  "takes": [{"chunk_id": take["chunk_id"], "take_id": take["take_id"],
                             "request_sha256": take["request_sha256"]} for take in takes]},
        "mode": "production_pcm", "outputs": {"master": True}, "gaps": [],
        "metadata": {"title": "Fixture", "author": "Fixture", "edition": "test"},
    }), owner_key="principal:fixture")
    service.run_build_job(queued["job_id"])
    job = service.get_job(GetJobRequest(project="fixture", job_id=queued["job_id"]))
    assert job["state"] == "succeeded", job
    head = _commit_book(service, f"{prefix}-accept", job["result"]["build_id"], expected_head, "accept_candidate")
    return head, takes


def _prepare_lookup_step(service, root, prose, operation, revision, chunks, *, spec=None, authorization_id="test-auth"):
    return _prepare_test_plan(
        service, operation, prose, (root / "Chapters/1/chapter_audio-tags.docx").read_bytes(), revision,
        [{**chunk, "request_spec": spec} for chunk in chunks], publish=True, authorization_id=authorization_id,
    )


def _quote_lookup(service, snapshot_id, text="hello", **arguments):
    return service.find_chunk(FindChunkRequest.model_validate({
        "project": "fixture", "chapter_id": "ch1", "query": {
            "kind": "quote", "text": text, "snapshot_id": snapshot_id,
        }, **arguments,
    }))


def test_lookup_traverses_real_split_merge_split_history_and_deduplicates_in_target_order(tmp_path):
    service, prose, _ = _fixture(tmp_path)
    first = _prepare_lookup_step(service, tmp_path, prose, "lookup-first", None,
                                 [{"chunk_id": "b", "start": 0, "end": 5}])
    split = _prepare_lookup_step(service, tmp_path, prose, "lookup-split", first["manifest_revision"], [
        {"chunk_id": "z-left", "start": 0, "end": 2, "replaces_chunk_ids": ["b"]},
        {"chunk_id": "a-right", "start": 2, "end": 5, "replaces_chunk_ids": ["b"]},
    ])
    merge = _prepare_lookup_step(service, tmp_path, prose, "lookup-merge", split["manifest_revision"], [
        {"chunk_id": "joined", "start": 0, "end": 5, "replaces_chunk_ids": ["z-left", "a-right"]},
    ])
    for historical in (first, split):
        match = _quote_lookup(service, historical["snapshot_id"])["matches"][0]
        assert match["current_chunk_ids"] == ["joined"] and match["current_mapping_status"] == "present"
    final = _prepare_lookup_step(service, tmp_path, prose, "lookup-final", merge["manifest_revision"], [
        {"chunk_id": "z-final", "start": 0, "end": 2, "replaces_chunk_ids": ["joined"]},
        {"chunk_id": "a-final", "start": 2, "end": 5, "replaces_chunk_ids": ["joined"]},
    ])
    for historical in (first, split, merge):
        match = _quote_lookup(service, historical["snapshot_id"])["matches"][0]
        assert match["snapshot_id"] == historical["snapshot_id"]
        assert match["current_chunk_ids"] == ["z-final", "a-final"]
        assert match["current_mapping_status"] == "ambiguous"
        assert all(item["current_chunk_ids"] == ["z-final", "a-final"] for item in match["lineage"])
    direct = _quote_lookup(service, final["snapshot_id"])["matches"][0]
    assert direct["current_chunk_ids"] == ["z-final", "a-final"]
    assert direct["lineage"] == [] and direct["current_mapping_status"] == "present"


def test_lookup_current_accepted_ancestor_and_timestamp_are_independent_of_prepared_navigation(tmp_path):
    service, prose, _ = _fixture(tmp_path)
    spec = {"provider": "synthetic", "route": "fixture", "model_id": "model", "voice_id": "voice",
            "parameters": {}, "context_fields": {}}
    first = _prepare_lookup_step(service, tmp_path, prose, "ancestor-first", None,
                                 [{"chunk_id": "b", "start": 0, "end": 5}], spec=spec)
    old_head, old_takes = _accept_lookup_plan(service, first, prefix="ancestor-old", expected_head=None)
    split = _prepare_lookup_step(service, tmp_path, prose, "ancestor-split", first["manifest_revision"], [
        {"chunk_id": "z-left", "start": 0, "end": 2, "replaces_chunk_ids": ["b"]},
        {"chunk_id": "a-right", "start": 2, "end": 5, "replaces_chunk_ids": ["b"]},
    ], spec=spec)
    split_head, split_takes = _accept_lookup_plan(service, split, prefix="ancestor-split", expected_head=1)
    state = service._state_required()
    old_timeline = tmp_path / state.build(old_head["accepted_build_id"])["result"]["timeline_filepath"]
    old_bytes = old_timeline.read_bytes()
    merge = _prepare_lookup_step(service, tmp_path, prose, "ancestor-merge", split["manifest_revision"], [
        {"chunk_id": "joined", "start": 0, "end": 5, "replaces_chunk_ids": ["z-left", "a-right"]},
    ], spec=spec)
    final = _prepare_lookup_step(service, tmp_path, prose, "ancestor-final", merge["manifest_revision"], [
        {"chunk_id": "z-final", "start": 0, "end": 2, "replaces_chunk_ids": ["joined"]},
        {"chunk_id": "a-final", "start": 2, "end": 5, "replaces_chunk_ids": ["joined"]},
    ], spec=spec)
    timestamp = {"project": "fixture", "query": {"kind": "timestamp", "build_id": old_head["accepted_build_id"], "seconds": 0.0}}
    for match in (_quote_lookup(service, first["snapshot_id"])["matches"][0],
                  service.find_chunk(FindChunkRequest.model_validate(timestamp))["matches"][0]):
        assert match["matched_build_id"] == old_head["accepted_build_id"]
        assert match["matched_take_ids"] == [old_takes[0]["take_id"]]
        assert match["current_take_ids"] == [take["take_id"] for take in split_takes]
        assert match["current_chunk_ids"] == ["z-final", "a-final"]
        assert match["current_mapping_status"] == "ambiguous"
    rollback = _commit_book(service, "ancestor-rollback", old_head["accepted_build_id"], 2, "rollback")
    assert rollback["head_revision"] == 3
    assert state.namespace("ch1", '{"kind":"test","authorization_id":"test-auth"}')["current_snapshot_id"] == final["snapshot_id"]
    current = _quote_lookup(service, final["snapshot_id"], "he")["matches"][0]
    assert current["current_chunk_ids"] == ["z-final"] and current["current_mapping_status"] == "present"
    assert current["current_take_ids"] == [old_takes[0]["take_id"]]
    old_split_timestamp = service.find_chunk(FindChunkRequest.model_validate({"project": "fixture", "query": {
        "kind": "timestamp", "build_id": split_head["accepted_build_id"], "seconds": 0.0,
    }}))["matches"][0]
    assert old_split_timestamp["matched_take_ids"] == [split_takes[0]["take_id"]]
    assert old_split_timestamp["current_take_ids"] == [old_takes[0]["take_id"]]
    assert old_timeline.read_bytes() == old_bytes


def test_lookup_deleted_terminal_does_not_guess_unrelated_current_text(tmp_path):
    service, prose, _ = _fixture(tmp_path)
    first = _prepare_lookup_step(service, tmp_path, prose, "deleted-first", None,
                                 [{"chunk_id": "old", "start": 0, "end": 5}])
    _prepare_lookup_step(service, tmp_path, prose, "deleted-later", first["manifest_revision"],
                         [{"chunk_id": "unrelated", "start": 0, "end": 5}])
    match = _quote_lookup(service, first["snapshot_id"])["matches"][0]
    assert match["chunk_ids"] == ["old"] and match["current_chunk_ids"] == []
    assert match["current_mapping_status"] == "missing"
    assert match["lineage"] == [{"old_chunk_id": "old", "current_chunk_ids": []}]


def test_lookup_accepted_ancestor_does_not_include_its_selected_sibling(tmp_path):
    service, prose, _ = _fixture(tmp_path)
    spec = {"provider": "synthetic", "route": "fixture", "model_id": "model", "voice_id": "voice",
            "parameters": {}, "context_fields": {}}
    first = _prepare_lookup_step(service, tmp_path, prose, "sibling-first", None,
                                 [{"chunk_id": "root", "start": 0, "end": 5}], spec=spec)
    split = _prepare_lookup_step(service, tmp_path, prose, "sibling-split", first["manifest_revision"], [
        {"chunk_id": "left", "start": 0, "end": 2, "replaces_chunk_ids": ["root"]},
        {"chunk_id": "right", "start": 2, "end": 5, "replaces_chunk_ids": ["root"]},
    ], spec=spec)
    head, takes = _accept_lookup_plan(service, split, prefix="sibling-head", expected_head=None)
    final = _prepare_lookup_step(service, tmp_path, prose, "sibling-final", split["manifest_revision"], [
        {"chunk_id": "left-child", "start": 0, "end": 1, "replaces_chunk_ids": ["left"]},
        {"chunk_id": "left-next", "start": 1, "end": 2, "replaces_chunk_ids": ["left"]},
        {"chunk_id": "right", "start": 2, "end": 5},
    ], spec=spec)
    child = _quote_lookup(service, final["snapshot_id"], "h")["matches"][0]
    assert child["current_chunk_ids"] == ["left-child"] and child["current_mapping_status"] == "present"
    assert child["current_take_ids"] == [takes[0]["take_id"]]
    assert takes[1]["take_id"] not in child["current_take_ids"]
    historical = service.find_chunk(FindChunkRequest.model_validate({"project": "fixture", "query": {
        "kind": "timestamp", "build_id": head["accepted_build_id"], "seconds": 0.0,
    }}))["matches"][0]
    assert historical["matched_take_ids"] == historical["current_take_ids"] == [takes[0]["take_id"]]
    assert historical["current_chunk_ids"] == ["left-child", "left-next"]
    assert historical["current_mapping_status"] == "ambiguous"


def test_directional_lineage_mapping_never_hops_to_siblings_and_is_finite():
    from cognita.books.service import _lineage_targets

    records = [
        {"chunk_id": "root", "replaces_chunk_ids": [], "replaced_by_chunk_ids": ["left", "right"]},
        {"chunk_id": "left", "replaces_chunk_ids": ["root"], "replaced_by_chunk_ids": ["new-left"]},
        {"chunk_id": "right", "replaces_chunk_ids": ["root"], "replaced_by_chunk_ids": []},
        {"chunk_id": "new-left", "replaces_chunk_ids": ["left"], "replaced_by_chunk_ids": []},
    ]
    assert _lineage_targets(["new-left"], ["right"], records, allow_ancestors=True) == {"new-left": []}
    assert _lineage_targets(["new-left"], ["root"], records) == {"new-left": []}
    assert _lineage_targets(["new-left"], ["root"], records, allow_ancestors=True) == {"new-left": ["root"]}
    assert _lineage_targets(["left"], ["new-left", "left", "root"], records, allow_ancestors=True) == {"left": ["left"]}
    assert _lineage_targets(["root", "root"], ["right", "new-left", "right"], records) == {"root": ["right", "new-left"]}
    records.append({"chunk_id": "cycle", "replaces_chunk_ids": ["cycle"], "replaced_by_chunk_ids": ["cycle"]})
    assert _lineage_targets(["cycle"], ["missing"], records, allow_ancestors=True) == {"cycle": []}


def test_lookup_lineage_namespace_isolation_and_cursor_binds_retained_facts(tmp_path):
    service, prose, _ = _fixture(tmp_path)
    first = _prepare_lookup_step(service, tmp_path, prose, "isolated-first", None,
                                 [{"chunk_id": "same", "start": 0, "end": 5}])
    page = _quote_lookup(service, first["snapshot_id"], "l", limit=1)
    layout_path = tmp_path / "Project Files/Book_Layout.json"
    layout = json.loads(layout_path.read_text(encoding="utf-8"))
    layout["test_authorizations"].append({**layout["test_authorizations"][0], "authorization_id": "other"})
    layout_path.write_text(json.dumps(layout), encoding="utf-8")
    other = _prepare_lookup_step(service, tmp_path, prose, "isolated-other", None,
                                 [{"chunk_id": "same", "start": 0, "end": 5}], authorization_id="other")
    _prepare_lookup_step(service, tmp_path, prose, "isolated-other-change", other["manifest_revision"],
                         [{"chunk_id": "other-child", "start": 0, "end": 5, "replaces_chunk_ids": ["same"]}], authorization_id="other")
    # The other namespace changed actual working bytes, so the original cursor
    # expires even though its source names and logical targets remain isolated.
    with pytest.raises(BookServiceError) as working_changed:
        _quote_lookup(service, first["snapshot_id"], "l", limit=1, cursor=page["next_cursor"])
    assert working_changed.value.reason == "invalid_cursor"
    page = _quote_lookup(service, first["snapshot_id"], "l", limit=1)
    continuation = _quote_lookup(service, first["snapshot_id"], "l", limit=1, cursor=page["next_cursor"])
    assert continuation["matches"][0]["current_chunk_ids"] == ["same"]
    assert continuation["matches"][0]["current_mapping_status"] == "present"
    # Change only a retained record, preserving heads, prepared snapshot, text,
    # working bytes and the target result. The opaque cursor must still bind it.
    state = service._state_required()
    with state.transaction() as connection:
        connection.execute("UPDATE book_chunk_lineage SET retired=1 WHERE chapter_id=? AND scope_key=? AND chunk_id=?",
                           ("ch1", '{"kind":"test","authorization_id":"test-auth"}', "same"))
    with pytest.raises(BookServiceError) as stale:
        _quote_lookup(service, first["snapshot_id"], "l", limit=1, cursor=page["next_cursor"])
    assert stale.value.reason == "invalid_cursor"


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
    assert not eligible and reason == "settings_changed"

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


def test_production_refuses_unsupported_docx_structure_but_inspect_reports_warning(tmp_path):
    """Tables stay visible as diagnostics and cannot pass production equivalence."""
    service, state, stored, settings_path, _layout_path, _chapter_state_path, prose, tagged, settings = (
        _production_prepared_fixture(tmp_path)
    )

    def with_table(raw: bytes) -> bytes:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            parts = {name: archive.read(name) for name in archive.namelist()}
        document_xml = parts["word/document.xml"]
        table = (
            b'<w:tbl><w:tr><w:tc><w:p><w:r><w:t>'
            b'unrepresented spoken table text'
            b'</w:t></w:r></w:p></w:tc></w:tr></w:tbl>'
        )
        parts["word/document.xml"] = document_xml.replace(b"<w:sectPr/>", table + b"<w:sectPr/>")
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
            for name, content in parts.items():
                archive.writestr(name, content)
        return output.getvalue()

    changed_prose, changed_tagged = with_table(prose), with_table(tagged)
    assert project_docx_pair(changed_prose, changed_tagged).prose_projection_sha256 == (
        project_docx_pair(prose, tagged).prose_projection_sha256
    )
    (tmp_path / "Chapters/1/chapter.docx").write_bytes(changed_prose)
    (tmp_path / "Chapters/1/chapter_audio-tags.docx").write_bytes(changed_tagged)

    inspected = _inspect(service)
    assert inspected["warnings"] and inspected["warnings"][0]["code"] == "unsupported_structure"
    layout = service._enabled_layout()[2]
    chapter = service._chapter(layout, "ch1")
    assert service._production_snapshot_eligible(state, layout, chapter, stored) == (
        False, "unsupported_docx_structure",
    )
    with pytest.raises(BookServiceError) as denied:
        _prepare_production_context(
            service, stored, settings_path, settings, settings["request_spec"]["context_fields"],
        )
    assert denied.value.reason == "unsupported_docx_structure"


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


def _prepare_production_context(service, stored, settings_path, settings, context):
    settings_path.write_text(json.dumps(settings), encoding="utf-8")
    inspected = _inspect(service)
    spec = {**settings["request_spec"], "context_fields": context}
    return service.prepare(PrepareRequest.model_validate({
        "project": "fixture", "operation_id": "context-prepare", "chapter_id": "ch1",
        "document_view_id": inspected["document_view_id"],
        "expected_prose_sha256": inspected["prose_sha256"],
        "expected_tagged_sha256": inspected["tagged_sha256"],
        "expected_manifest_revision": stored["manifest_revision"],
        "scope": {"kind": "production"}, "speech_selection_confirmed": True,
        "request_limit": {"value": 100, "unit": "unicode_codepoints"},
        "expected_settings_sha256": hashlib.sha256(settings_path.read_bytes()).hexdigest(),
        "production_target": settings["production_target"],
        "chunks": [{"chunk_id": "main", "start": 0, "end": 5, "request_spec": spec}],
        "publish_bookmarks_to_working_tagged_docx": False,
    }), owner_key="principal:fixture")


def test_production_context_extras_are_frozen_fingerprinted_and_later_eligible(tmp_path):
    service, state, stored, settings_path, _layout_path, _chapter_path, _prose, _tagged, settings = (
        _production_prepared_fixture(tmp_path)
    )
    settings["request_spec"]["context_fields"]["flag"] = True
    context = {**settings["request_spec"]["context_fields"],
               "previous_text": "Exact previous text\n[tag] café", "next_text": None}
    prepared, replayed = _prepare_production_context(service, stored, settings_path, settings, context)
    assert not replayed
    chunk = prepared["chunks"][0]
    assert chunk["request_spec"]["context_fields"] == context
    assert chunk["request_sha256"] == request_fingerprint("hello", chunk["request_spec"])
    assert chunk["request_sha256"] != request_fingerprint("hello", settings["request_spec"])
    frozen = state.snapshot(prepared["snapshot_id"])
    assert frozen["payload"]["result"]["chunks"][0]["request_spec"]["context_fields"] == context
    layout = service._enabled_layout()[2]
    chapter = service._chapter(layout, "ch1")
    assert service._production_snapshot_eligible(state, layout, chapter, frozen) == (True, "eligible")
    changed = json.loads(json.dumps(frozen))
    changed["payload"]["result"]["chunks"][0]["request_spec"]["context_fields"]["previous_text"] = "changed"
    assert service._production_snapshot_eligible(state, layout, chapter, changed) == (False, "plan_ineligible")


@pytest.mark.parametrize(("field", "value"), [
    ("language", "fr"), ("flag", 1), ("nested", {"enabled": 1}),
    ("sequence", [1]), ("flag", None), ("missing_flag", None),
])
def test_production_common_context_must_match_exact_json_values(tmp_path, field, value):
    from cognita.books.config import validate_production_settings

    service, state, stored, settings_path, _layout_path, _chapter_path, _prose, _tagged, settings = (
        _production_prepared_fixture(tmp_path)
    )
    common = {"language": "en", "flag": True, "nested": {"enabled": True}, "sequence": [True]}
    settings["request_spec"]["context_fields"] = common
    context = {**common, "previous_text": "additional context"}
    if field == "missing_flag":
        context.pop("flag")
    else:
        context[field] = value
    with pytest.raises(BookServiceError) as refused:
        _prepare_production_context(service, stored, settings_path, settings, context)
    assert refused.value.reason == "settings_mismatch"
    assert state.namespace("ch1", stored["scope_key"])["manifest_revision"] == stored["manifest_revision"]
    # Check the later plan guard independently of fingerprint integrity: this
    # intentionally constructed plan has its own correct request fingerprint.
    payload = json.loads(json.dumps(stored["payload"]))
    chunk = payload["result"]["chunks"][0]
    chunk["request_spec"]["context_fields"] = context
    chunk["request_sha256"] = request_fingerprint("hello", chunk["request_spec"])
    assert not service._plan_requests_match_settings(
        payload, validate_production_settings(settings_path.read_bytes()),
    )


def _build_production_chapter_candidate(service, prepared, take, *, operation_prefix, expected_head):
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
    return build_id


def _build_and_accept_production_chapter(service, prepared, take, *, operation_prefix, expected_head):
    build_id = _build_production_chapter_candidate(
        service, prepared, take, operation_prefix=operation_prefix, expected_head=expected_head,
    )
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


def test_direct_take_associations_keep_history_separate_from_current_head(tmp_path):
    service, state, stored, settings_path, *_rest, settings = _production_prepared_fixture(tmp_path)
    old_plan = stored["payload"]["result"]
    old_take = _import_native_take(service, old_plan, "main", operation_prefix="association-old")
    old_head = _build_and_accept_production_chapter(
        service, old_plan, old_take, operation_prefix="association-old", expected_head=None,
    )
    new_plan, _ = _prepare_production_context(
        service, stored, settings_path, settings, {"language": "en", "previous_text": "changed context"},
    )
    new_take = _import_native_take(service, new_plan, "main", operation_prefix="association-new")
    new_head = _build_and_accept_production_chapter(
        service, new_plan, new_take, operation_prefix="association-new", expected_head=1,
    )
    unaccepted_take = _import_native_take(service, new_plan, "main", operation_prefix="association-unaccepted")
    query = {"project": "fixture", "chapter_id": "ch1", "query": {
        "kind": "quote", "text": "hello", "snapshot_id": old_plan["snapshot_id"],
    }}
    timestamp_query = {"project": "fixture", "query": {
        "kind": "timestamp", "build_id": old_head["accepted_build_id"], "seconds": 0.0,
    }}
    old_timeline = tmp_path / state.build(old_head["accepted_build_id"])["result"]["timeline_filepath"]
    timeline_before = old_timeline.read_bytes()
    for arguments in (query, timestamp_query):
        match = service.find_chunk(FindChunkRequest.model_validate(arguments))["matches"][0]
        assert match["snapshot_id"] == old_plan["snapshot_id"]
        assert match["matched_build_id"] == old_head["accepted_build_id"]
        assert match["matched_take_ids"] == [old_take["take_id"]]
        assert match["current_take_ids"] == [new_take["take_id"]]
        assert unaccepted_take["take_id"] not in match["current_take_ids"]
    for snapshot_id in (old_plan["snapshot_id"], new_plan["snapshot_id"]):
        chapter = service.get_chapter(GetChapterRequest.model_validate({
            "project": "fixture", "chapter_id": "ch1", "snapshot_id": snapshot_id, "limit": 1,
        }))
        assert chapter["takes"] == [] and chapter["chunks"][0]["take_ids"] == []
        assert chapter["chunks"][0]["accepted_take_id"] == new_take["take_id"]
    chapter_query = {"project": "fixture", "chapter_id": "ch1", "snapshot_id": old_plan["snapshot_id"], "limit": 1}
    chapter_cursor = service.get_chapter(GetChapterRequest.model_validate(chapter_query))["next_cursor"]
    repeated_query = {**query, "limit": 1, "query": {**query["query"], "text": "l"}}
    quote_cursor = service.find_chunk(FindChunkRequest.model_validate(repeated_query))["next_cursor"]
    rolled = _commit_book(service, "association-rollback", old_head["accepted_build_id"], 2, "rollback")
    assert rolled["head_revision"] == 3
    assert state.namespace("ch1", '{"kind":"production"}')["current_snapshot_id"] == new_plan["snapshot_id"]
    assert state.chapter_head("ch1", '{"kind":"production"}')["accepted_snapshot_id"] == old_plan["snapshot_id"]
    for arguments in (query, timestamp_query):
        match = service.find_chunk(FindChunkRequest.model_validate(arguments))["matches"][0]
        assert match["matched_take_ids"] == match["current_take_ids"] == [old_take["take_id"]]
    current = service.get_chapter(GetChapterRequest(project="fixture", chapter_id="ch1", limit=1))
    assert current["snapshot_id"] == new_plan["snapshot_id"]
    assert current["accepted_build_id"] == old_head["accepted_build_id"] != new_head["accepted_build_id"]
    assert current["chunks"][0]["accepted_take_id"] == old_take["take_id"]
    for method, arguments, cursor, model in (
        (service.get_chapter, chapter_query, chapter_cursor, GetChapterRequest),
        (service.find_chunk, repeated_query, quote_cursor, FindChunkRequest),
    ):
        assert cursor is not None
        with pytest.raises(BookServiceError) as stale:
            method(model.model_validate({**arguments, "cursor": cursor}))
        assert stale.value.reason == "invalid_cursor"
    assert old_timeline.read_bytes() == timeline_before


def _working_bookmark_fixture(root, *, publish=True):
    service, _, _ = _fixture(root)
    source = _docx_paragraphs("repeat α", "repeat β")
    (root / "Chapters/1/chapter.docx").write_bytes(source)
    (root / "Chapters/1/chapter_audio-tags.docx").write_bytes(source)
    layout_path = root / "Project Files/Book_Layout.json"
    layout = json.loads(layout_path.read_text(encoding="utf-8"))
    layout["test_authorizations"][0].update({
        "source_raw_sha256": hashlib.sha256(source).hexdigest(), "allowed_paragraph_ordinals": [0, 1],
    })
    layout_path.write_text(json.dumps(layout), encoding="utf-8")
    prepared = _prepare_test_plan(service, "working-bookmark-prepare", source, source, None, [
        {"chunk_id": "both", "start": 0, "end": len("repeat α\n\nrepeat β"), "request_spec": None},
    ], publish=publish)
    query = {"project": "fixture", "chapter_id": "ch1", "query": {
        "kind": "quote", "text": "repeat", "snapshot_id": prepared["snapshot_id"],
    }}
    return service, prepared, query


def _rewrite_docx_part(path, rewrite):
    from lxml import etree

    with zipfile.ZipFile(io.BytesIO(path.read_bytes())) as archive:
        parts = {name: archive.read(name) for name in archive.namelist()}
    document = etree.fromstring(parts["word/document.xml"])
    rewrite(document)
    parts["word/document.xml"] = etree.tostring(document)
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in parts.items():
            archive.writestr(name, content)
    path.write_bytes(output.getvalue())


@pytest.mark.parametrize("change,expected", [
    ("published", "present"), ("unpublished", "missing"), ("remove_second", "missing"),
    ("range_second", "missing"), ("text_second", "missing"), ("formatting", "present"),
    ("missing_file", "missing"), ("malformed", "not_checked"), ("unreadable", "not_checked"),
])
def test_quote_present_requires_every_actual_working_bookmark(tmp_path, monkeypatch, change, expected):
    service, prepared, query = _working_bookmark_fixture(tmp_path, publish=change != "unpublished")
    working = tmp_path / "Chapters/1/chapter_audio-tags.docx"
    original = working.read_bytes()
    assert len(prepared["chunks"][0]["source_segments"]) == 2
    ns = {"w": W}

    def change_document(document):
        second = document.xpath("//w:body/w:p", namespaces=ns)[1]
        if change == "remove_second":
            for mark in second.xpath("w:bookmarkStart | w:bookmarkEnd", namespaces=ns):
                second.remove(mark)
        elif change == "range_second":
            end = second.find(f"{{{W}}}bookmarkEnd")
            second.remove(end)
            second.insert(1, end)  # Same name/text, but an empty actual Word range.
        elif change == "text_second":
            second.find(f".//{{{W}}}t").text = "repeat γ"
        elif change == "formatting":
            from lxml import etree
            second.insert(0, etree.Element(f"{{{W}}}pPr"))

    if change == "missing_file":
        working.unlink()
    elif change == "malformed":
        working.write_bytes(b"not a DOCX")
    elif change == "unreadable":
        read = service_module._read_bytes

        def unavailable_read(root, relative):
            if relative == "Chapters/1/chapter_audio-tags.docx":
                raise PermissionError("synthetic fixture read denied")
            return read(root, relative)

        monkeypatch.setattr(service_module, "_read_bytes", unavailable_read)
    elif change not in {"published", "unpublished"}:
        _rewrite_docx_part(working, change_document)
    found = service.find_chunk(FindChunkRequest.model_validate(query))
    assert len(found["matches"]) == 2
    assert {match["current_mapping_status"] for match in found["matches"]} == {expected}
    assert all(match["current_chunk_ids"] == ["both"] for match in found["matches"])
    assert all(match["snapshot_id"] == prepared["snapshot_id"] for match in found["matches"])
    if change == "formatting":
        assert working.read_bytes() != original


def test_quote_bookmark_proof_reads_once_and_pins_observed_hash_in_cursor(tmp_path, monkeypatch):
    service, _prepared, query = _working_bookmark_fixture(tmp_path)
    reads = []
    read = service_module._read_bytes

    def observed_read(root, relative):
        reads.append(relative)
        return read(root, relative)

    monkeypatch.setattr(service_module, "_read_bytes", observed_read)
    page = service.find_chunk(FindChunkRequest.model_validate({**query, "limit": 1}))
    working_path = "Chapters/1/chapter_audio-tags.docx"
    assert reads.count(working_path) == 1
    assert page["has_more"] and page["matches"][0]["current_mapping_status"] == "present"
    continuation = service.find_chunk(FindChunkRequest.model_validate({**query, "cursor": page["next_cursor"], "limit": 1}))
    assert continuation["matches"][0]["current_mapping_status"] == "present"
    assert reads.count(working_path) == 2
    _rewrite_docx_part(tmp_path / working_path, lambda document: document.set("formatting_fixture", "changed"))
    # Ranges/text and status remain identical; only observed raw bytes changed.
    assert service.find_chunk(FindChunkRequest.model_validate(query))["matches"][0]["current_mapping_status"] == "present"
    with pytest.raises(BookServiceError) as stale:
        service.find_chunk(FindChunkRequest.model_validate({**query, "cursor": page["next_cursor"], "limit": 1}))
    assert stale.value.reason == "invalid_cursor"


@pytest.mark.parametrize("artifact", ["tagged_filepath", "prose_filepath"])
def test_quote_bookmark_proof_refuses_changed_frozen_artifact(tmp_path, artifact):
    service, prepared, query = _working_bookmark_fixture(tmp_path)
    stored = service._state_required().snapshot(prepared["snapshot_id"])
    frozen = tmp_path / stored["payload"][artifact]
    frozen.write_bytes(_docx("different immutable source"))
    with pytest.raises(BookServiceError) as corrupt:
        service.find_chunk(FindChunkRequest.model_validate(query))
    assert corrupt.value.reason == "state_unavailable"


def test_old_quote_uses_current_prepared_bookmarks_without_changing_history(tmp_path):
    service, first, query = _working_bookmark_fixture(tmp_path)
    source = (tmp_path / "Chapters/1/chapter.docx").read_bytes()
    second = _prepare_test_plan(
        service, "working-bookmark-second", source,
        (tmp_path / "Chapters/1/chapter_audio-tags.docx").read_bytes(), first["manifest_revision"],
        [{"chunk_id": "both", "start": 0, "end": len("repeat α\n\nrepeat β"), "request_spec": None}], publish=True,
    )
    second_name = second["chunks"][0]["source_segments"][1]["bookmark"]
    first_name = first["chunks"][0]["source_segments"][1]["bookmark"]
    assert second_name != first_name
    found = service.find_chunk(FindChunkRequest.model_validate(query))
    assert all(match["snapshot_id"] == first["snapshot_id"] and match["current_mapping_status"] == "present"
               for match in found["matches"])

    def remove_current(document):
        starts = document.findall(f".//{{{W}}}bookmarkStart")
        selected = next(mark for mark in starts if mark.get(f"{{{W}}}name") == second_name)
        mark_id = selected.get(f"{{{W}}}id")
        selected.getparent().remove(selected)
        end = next(mark for mark in document.findall(f".//{{{W}}}bookmarkEnd") if mark.get(f"{{{W}}}id") == mark_id)
        end.getparent().remove(end)

    _rewrite_docx_part(tmp_path / "Chapters/1/chapter_audio-tags.docx", remove_current)
    found = service.find_chunk(FindChunkRequest.model_validate(query))
    assert all(match["snapshot_id"] == first["snapshot_id"] and match["current_mapping_status"] == "missing"
               for match in found["matches"])


def test_quote_working_bookmark_proof_uses_authorized_alternate_test_path(tmp_path):
    service, _, production_tagged = _fixture(tmp_path)
    prose, tagged = _authorized_alternate_pair(tmp_path)
    prepared = _prepare_alternate_test_plan(service, prose, tagged, operation_id="alternate-lookup", publish=True)
    query = {"project": "fixture", "chapter_id": "ch1", "query": {
        "kind": "quote", "text": "alternate", "snapshot_id": prepared["snapshot_id"],
    }}
    assert service.find_chunk(FindChunkRequest.model_validate(query))["matches"][0]["current_mapping_status"] == "present"
    assert (tmp_path / "Chapters/1/chapter_audio-tags.docx").read_bytes() == production_tagged
    (tmp_path / "Chapters/1/Test/chapter_audio-tags.docx").write_bytes(tagged)
    assert service.find_chunk(FindChunkRequest.model_validate(query))["matches"][0]["current_mapping_status"] == "missing"


def test_selected_build_association_allows_take_reused_from_older_snapshot(tmp_path):
    service, state, stored, settings_path, *_rest, settings = _production_prepared_fixture(tmp_path)
    first = stored["payload"]["result"]
    take = _import_native_take(service, first, "main", operation_prefix="association-reuse")
    _build_and_accept_production_chapter(service, first, take, operation_prefix="association-reuse-first", expected_head=None)
    second, _ = _prepare_production_context(service, stored, settings_path, settings, settings["request_spec"]["context_fields"])
    assert second["snapshot_id"] != take["snapshot_id"]
    assert second["chunks"][0]["reusable_take_ids"] == [take["take_id"]]
    committed = _build_and_accept_production_chapter(
        service, second, take, operation_prefix="association-reuse-second", expected_head=1,
    )
    match = service.find_chunk(FindChunkRequest.model_validate({"project": "fixture", "query": {
        "kind": "timestamp", "build_id": committed["accepted_build_id"], "seconds": 0.0,
    }}))["matches"][0]
    assert match["snapshot_id"] == second["snapshot_id"]
    assert match["matched_take_ids"] == match["current_take_ids"] == [take["take_id"]]
    assert service.get_chapter(GetChapterRequest(project="fixture", chapter_id="ch1"))["chunks"][0]["accepted_take_id"] == take["take_id"]


@pytest.mark.parametrize("field,value", [
    ("chapter_id", "different-chapter"),
    ("namespace", {"kind": "test", "authorization_id": "different-namespace"}),
    ("chunk_id", "different-chunk"),
])
def test_selected_build_association_rejects_take_outside_frozen_membership(tmp_path, monkeypatch, field, value):
    service, state, stored, *_rest = _production_prepared_fixture(tmp_path)
    prepared = stored["payload"]["result"]
    take = _import_native_take(service, prepared, "main", operation_prefix="association-membership")
    build_id = _build_production_chapter_candidate(
        service, prepared, take, operation_prefix="association-membership", expected_head=None,
    )
    monkeypatch.setattr(state, "take", lambda _take_id: {**take, field: value})
    with pytest.raises(BookServiceError) as invalid:
        service._selected_chunk_takes(state, state.build(build_id), chapter_id="ch1", scope_key='{"kind":"production"}')
    assert invalid.value.reason == "state_unavailable"


def _acceptance_events(state):
    with state._connect() as connection:
        return [dict(row) for row in connection.execute(
            "SELECT * FROM book_acceptance_events ORDER BY rowid",
        ).fetchall()]


@pytest.mark.parametrize("scope", ["chapter", "book"])
def test_acceptance_events_are_atomic_replay_safe_and_persist_reviewer_facts(tmp_path, scope):
    service, state, stored, *_rest = _production_prepared_fixture(tmp_path)
    prepared = stored["payload"]["result"]
    take = _import_native_take(service, prepared, "main", operation_prefix="event-take")
    build_id = _build_production_chapter_candidate(
        service, prepared, take, operation_prefix="event-chapter", expected_head=None,
    )
    layout = service._enabled_layout()[2]
    if scope == "book":
        _commit_book(service, "event-baseline-chapter", build_id, None, "accept_candidate")
        build_id = _reserve_synthetic_book_build(
            service, state, layout, operation_prefix="event-book", expected_book_head=None,
        )
    def head():
        return (state.chapter_head("ch1", '{"kind":"production"}') if scope == "chapter"
                else state.book_head(layout.book_id))
    acceptance = {"actor": "asserted reviewer", "accepted_at": "2001-02-03T04:05:06-05:00",
                  "listening_review": "explicitly_waived", "notes": ["literal café\n[tag]", "same", "same"]}
    arguments = {"project": "fixture", "operation_id": "event-accept", "build_id": build_id,
                 "expected_head_revision": None, "intent": "accept_candidate", "acceptance": acceptance}
    request = CommitBuildRequest.model_validate(arguments)
    files_before = {path: path.read_bytes() for path in (tmp_path / "Audiobook").rglob("*") if path.is_file()}
    events_before = _acceptance_events(state)
    with pytest.raises(BookServiceError) as never_accepted:
        service.commit_build(CommitBuildRequest.model_validate({**arguments, "operation_id": "event-invalid-rollback",
                             "intent": "rollback"}), owner_key="principal:fixture")
    assert never_accepted.value.reason == "stale_dependency"
    assert _acceptance_events(state) == events_before and head() is None
    before_time = datetime.now(timezone.utc)
    committed, replayed = service.commit_build(request, owner_key="principal:fixture")
    after_time = datetime.now(timezone.utc)
    assert not replayed and committed["head_revision"] == 1
    events = _acceptance_events(state)
    assert events[:-1] == events_before
    event = events[-1]
    assert event["owner_key"] == "principal:fixture" and event["project"] == "fixture"
    assert event["operation_id"] == "event-accept"
    server_time = datetime.fromisoformat(event["committed_at"].replace("Z", "+00:00"))
    # SQLite and Python may use differently rounded Windows clock APIs.
    assert before_time - timedelta(seconds=1) <= server_time <= after_time + timedelta(seconds=1)
    payload = json.loads(event["payload_json"])
    assert payload["scope"] == scope and payload["namespace"] == {"kind": "production"}
    assert payload["chapter_id" if scope == "chapter" else "book_id"] == ("ch1" if scope == "chapter" else layout.book_id)
    assert payload["build_id"] == build_id and payload["intent"] == "accept_candidate"
    assert payload["acceptance"] == acceptance
    assert payload["previous_head"] is None and payload["head_revision"] == 1
    assert service.commit_build(request, owner_key="principal:fixture") == (committed, True)
    transaction_arguments = {
        "build_id": build_id, "expected_head_revision": None, "intent": "accept_candidate",
        "owner_key": "principal:fixture", "project": "fixture", "operation_id": "event-accept",
        "args_sha256": canonical_json_sha256(arguments), "result": committed,
        "plan_matches_prepared": True, "acceptance": acceptance,
    }
    if scope == "chapter":
        commit_transaction = state.commit_chapter_build
        transaction_arguments.update(chapter_id="ch1", scope_key='{"kind":"production"}')
    else:
        commit_transaction = state.commit_book_build
        transaction_arguments.update(book_id=layout.book_id)
    assert commit_transaction(**transaction_arguments) == ("replay", committed)
    with pytest.raises(ProjectStateError, match="operation_id_conflict"):
        commit_transaction(**{**transaction_arguments, "args_sha256": "0" * 64})
    with pytest.raises(BookServiceError) as conflict:
        service.commit_build(CommitBuildRequest.model_validate({**arguments, "acceptance": {**acceptance, "notes": ["changed"]}}),
                             owner_key="principal:fixture")
    assert conflict.value.reason == "operation_id_conflict"
    with pytest.raises(BookServiceError) as stale:
        service.commit_build(CommitBuildRequest.model_validate({**arguments, "operation_id": "event-stale"}),
                             owner_key="principal:fixture")
    assert stale.value.reason == "stale_head"
    assert _acceptance_events(state) == events
    previous_head = head()
    rollback_acceptance = {**acceptance, "actor": "rollback reviewer", "listening_review": "passed"}
    rollback = CommitBuildRequest.model_validate({**arguments, "operation_id": "event-rollback",
        "expected_head_revision": 1, "intent": "rollback", "acceptance": rollback_acceptance})
    rolled, replayed = service.commit_build(rollback, owner_key="principal:fixture")
    assert not replayed and rolled["head_revision"] == 2
    rollback_event = _acceptance_events(state)[-1]
    rollback_payload = json.loads(rollback_event["payload_json"])
    assert rollback_payload["acceptance"] == rollback_acceptance and rollback_payload["intent"] == "rollback"
    assert rollback_payload["previous_head"] == previous_head and rollback_payload["head_revision"] == 2
    assert service.commit_build(rollback, owner_key="principal:fixture") == (rolled, True)
    persisted = _acceptance_events(state)
    assert len(persisted) == len(events_before) + 2
    reopened = BookService(tmp_path, "fixture")
    assert _acceptance_events(reopened._state_required()) == persisted
    assert reopened.commit_build(request, owner_key="principal:fixture") == (committed, True)
    assert _acceptance_events(reopened._state_required()) == persisted
    assert {path: path.read_bytes() for path in files_before} == files_before


@pytest.mark.parametrize("scope", ["chapter", "book"])
def test_acceptance_event_insert_failure_rolls_back_head_build_and_receipt(tmp_path, scope):
    service, state, stored, *_rest = _production_prepared_fixture(tmp_path)
    prepared = stored["payload"]["result"]
    take = _import_native_take(service, prepared, "main", operation_prefix="failed-event-take")
    build_id = _build_production_chapter_candidate(
        service, prepared, take, operation_prefix="failed-event-chapter", expected_head=None,
    )
    layout = service._enabled_layout()[2]
    if scope == "book":
        _commit_book(service, "failed-event-baseline-chapter", build_id, None, "accept_candidate")
        build_id = _reserve_synthetic_book_build(
            service, state, layout, operation_prefix="failed-event-book", expected_book_head=None,
        )
    events_before = _acceptance_events(state)
    build_before = state.build(build_id)
    assert not build_before["was_accepted"]
    with state.transaction() as connection:
        connection.execute("CREATE TRIGGER reject_acceptance_event BEFORE INSERT ON book_acceptance_events "
                           "BEGIN SELECT RAISE(ABORT, 'synthetic event insert failure'); END")
    request = CommitBuildRequest.model_validate({
        "project": "fixture", "operation_id": "failed-event-accept", "build_id": build_id,
        "expected_head_revision": None, "intent": "accept_candidate",
        "acceptance": {"actor": "asserted reviewer", "accepted_at": "2001-02-03T04:05:06Z",
                       "listening_review": "passed", "notes": []},
    })
    with pytest.raises(BookServiceError) as failed:
        service.commit_build(request, owner_key="principal:fixture")
    assert failed.value.reason == "state_unavailable"
    reopened = BookService(tmp_path, "fixture")._state_required()
    assert (reopened.chapter_head("ch1", '{"kind":"production"}') if scope == "chapter"
            else reopened.book_head(layout.book_id)) is None
    assert reopened.build(build_id) == build_before
    assert _acceptance_events(reopened) == events_before
    assert reopened.receipt(owner_key="principal:fixture", project="fixture",
                            tool="audiobook_commit_build", operation_id="failed-event-accept") is None
    with state.transaction() as connection:
        connection.execute("DROP TRIGGER reject_acceptance_event")
    committed, replayed = service.commit_build(request, owner_key="principal:fixture")
    assert not replayed and committed["head_revision"] == 1
    assert len(_acceptance_events(state)) == len(events_before) + 1


def _assert_completed_build_replay_survives_changed_guards(service, request, original_job):
    state = service._state_required()
    assert state.build_job(original_job["job_id"])["state"] == "succeeded"
    assert service.build(request, owner_key="principal:fixture") == (original_job, True)
    with pytest.raises(BookServiceError) as other_owner:
        service.build(request, owner_key="principal:other")
    assert other_owner.value.reason in {"stale_manifest", "stale_head", "stale_dependency"}
    changed = request.model_dump(mode="json", exclude_unset=True)
    changed["metadata"]["edition"] = "different arguments"
    with pytest.raises(BookServiceError) as conflict:
        service.build(BuildRequest.model_validate(changed), owner_key="principal:fixture")
    assert conflict.value.reason == "operation_id_conflict"

    # Replays do not reread mutable prose or immutable input media. The current
    # layout/dispatch authority must nevertheless remain valid on every call.
    source = service.root / "Chapters/1/chapter.docx"
    original_source = source.read_bytes()
    source.write_bytes(b"source changed after accepted build")
    try:
        assert service.build(request, owner_key="principal:fixture") == (original_job, True)
        with pytest.raises(BookServiceError) as changed_conflict:
            service.build(BuildRequest.model_validate(changed), owner_key="principal:fixture")
        assert changed_conflict.value.reason == "operation_id_conflict"
    finally:
        source.write_bytes(original_source)
    layout_path = service.root / "Project Files/Book_Layout.json"
    original_layout = layout_path.read_bytes()
    layout_path.write_bytes(b"{}")
    try:
        with pytest.raises(BookServiceError) as invalid_authority:
            service.build(request, owner_key="principal:fixture")
        assert invalid_authority.value.reason == "configuration_conflict"
    finally:
        layout_path.write_bytes(original_layout)


def test_completed_book_build_replay_survives_acceptance_and_changed_plan(tmp_path):
    service, state, stored, settings_path, _layout_path, _chapter_path, _prose, _tagged, settings = (
        _production_prepared_fixture(tmp_path)
    )
    prepared = stored["payload"]["result"]
    take = _import_native_take(service, prepared, "main", operation_prefix="book-replay-take")
    _build_and_accept_production_chapter(
        service, prepared, take, operation_prefix="book-replay-chapter", expected_head=None,
    )
    layout = service._enabled_layout()[2]
    build_id = _reserve_synthetic_book_build(
        service, state, layout, operation_prefix="book-replay", expected_book_head=None,
    )
    build = state.build(build_id)
    request = BuildRequest.model_validate({
        "project": "fixture", "operation_id": "book-replay-reserve", "expected_head_revision": None,
        "input": {"kind": "book", "book_id": layout.book_id,
                  "expected_layout_revision": layout.layout_revision, "chapters": build["dependencies"]},
        "mode": "production_pcm", "outputs": {"master": True, "mp3_bitrate_kbps": 192},
        "gaps": [], "metadata": {"title": "Fixture", "author": "Fixture", "edition": "test"},
    })
    original_job = state.receipt(owner_key="principal:fixture", project="fixture",
                                 tool="audiobook_build", operation_id=request.operation_id)[1]
    committed = _commit_book(service, "book-replay-accept", build_id, None, "accept_candidate")
    assert committed["head_revision"] == 1
    assert service.build(request, owner_key="principal:fixture") == (original_job, True)
    changed_plan, _ = _prepare_production_context(
        service, stored, settings_path, settings,
        {**settings["request_spec"]["context_fields"], "previous_text": "new plan context"},
    )
    assert changed_plan["request_plan_sha256"] != prepared["request_plan_sha256"]
    _assert_completed_build_replay_survives_changed_guards(service, request, original_job)


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


def _collect_chapter_text_pages(service, query, *, expected_texts, cap):
    pages = []
    collected = {chunk_id: {name: "" for name in ("prompt", "spoken_text")} for chunk_id in expected_texts}
    cursor = None
    for _ in range(100):
        arguments = dict(query)
        if cursor is not None:
            arguments["cursor"] = cursor
        page = service.get_chapter(GetChapterRequest.model_validate(arguments))
        pages.append(page)
        assert sum(len(item[name]["text"]) for item in page["returned_texts"]
                   for name in ("prompt", "spoken_text")) <= cap
        assert len(page["chunks"]) + len(page["takes"]) + len(page["candidate_build_ids"]) <= query.get("limit", 100)
        page_take_ids = {take["take_id"] for take in page["takes"]}
        for chunk in page["chunks"]:
            assert set(chunk["take_ids"]) <= page_take_ids
            assert set(chunk["reusable_take_ids"]) <= page_take_ids
        for item in page["returned_texts"]:
            for name in ("prompt", "spoken_text"):
                field = item[name]
                expected = expected_texts[item["chunk_id"]][name]
                prior = collected[item["chunk_id"]][name]
                assert field["returned_start"] == len(prior)
                assert field["returned_end"] == len(prior) + len(field["text"])
                assert field["total_codepoints"] == len(expected)
                assert field["text_sha256"] == hashlib.sha256(expected.encode()).hexdigest()
                collected[item["chunk_id"]][name] += field["text"]
        if not page["has_more"]:
            assert page["next_cursor"] is None
            break
        assert page["next_cursor"] is not None and page["next_cursor"] != cursor
        cursor = page["next_cursor"]
    else:
        pytest.fail("Chapter pagination did not terminate")
    assert collected == expected_texts
    return pages


@pytest.mark.parametrize("limit", [1, 100])
@pytest.mark.parametrize("cap", [10, 11])
def test_chapter_text_budget_keeps_later_chunks_takes_and_candidates_reachable(tmp_path, limit, cap):
    service, _prose, _tagged = _fixture(tmp_path)
    prose = tagged = _docx("hello world")
    (tmp_path / "Chapters/1/chapter.docx").write_bytes(prose)
    (tmp_path / "Chapters/1/chapter_audio-tags.docx").write_bytes(tagged)
    layout_path = tmp_path / "Project Files/Book_Layout.json"
    layout = json.loads(layout_path.read_text(encoding="utf-8"))
    layout["test_authorizations"][0]["source_raw_sha256"] = hashlib.sha256(prose).hexdigest()
    layout_path.write_text(json.dumps(layout), encoding="utf-8")
    spec = {"provider": "synthetic", "route": "fixture", "model_id": "model",
            "voice_id": "voice", "parameters": {}, "context_fields": {}}
    chunks = [{"chunk_id": "a", "start": 0, "end": 5, "request_spec": spec},
              {"chunk_id": "b", "start": 5, "end": 11, "request_spec": spec}]
    prepared = _prepare_test_plan(service, "paging-first", prose, tagged, None, chunks)
    takes = [_import_native_take(service, prepared, chunk_id, operation_prefix=f"paging-{chunk_id}")
             for chunk_id in ("a", "b")]
    prepared = _prepare_test_plan(service, "paging-reuse", prose, tagged, prepared["manifest_revision"], chunks)
    request = BuildRequest.model_validate({
        "project": "fixture", "operation_id": "paging-build", "expected_head_revision": None,
        "input": {"kind": "chapter", "chapter_id": "ch1", "snapshot_id": prepared["snapshot_id"],
                  "expected_manifest_revision": prepared["manifest_revision"],
                  "request_plan_sha256": prepared["request_plan_sha256"],
                  "takes": [{"chunk_id": take["chunk_id"], "take_id": take["take_id"],
                             "request_sha256": take["request_sha256"]} for take in takes]},
        "mode": "production_pcm", "outputs": {"master": True}, "gaps": [],
        "metadata": {"title": "Fixture", "author": "Fixture", "edition": "test"},
    })
    job, _ = service.build(request, owner_key="principal:fixture")
    service.run_build_job(job["job_id"])
    built = service.get_job(GetJobRequest(project="fixture", job_id=job["job_id"]))
    assert built["state"] == "succeeded", built
    query = {"project": "fixture", "chapter_id": "ch1",
             "scope": {"kind": "test", "authorization_id": "test-auth"},
             "snapshot_id": prepared["snapshot_id"], "include_text": True, "max_characters": cap, "limit": limit}
    expected = {"a": {"prompt": "hello", "spoken_text": "hello"},
                "b": {"prompt": " world", "spoken_text": " world"}}
    pages = _collect_chapter_text_pages(service, query, expected_texts=expected, cap=cap)
    assert [item["chunk_id"] for item in pages[0]["returned_texts"]] == ["a"]
    assert pages[0]["has_more"]
    returned_take_ids = [take["take_id"] for page in pages for take in page["takes"]]
    assert sorted(returned_take_ids) == sorted(take["take_id"] for take in takes)
    assert [build_id for page in pages for build_id in page["candidate_build_ids"]] == [built["result"]["build_id"]]
    if limit == 100:
        final_chunk = next(chunk for chunk in pages[-1]["chunks"] if chunk["chunk_id"] == "b")
        assert final_chunk["take_ids"] == final_chunk["reusable_take_ids"] == [takes[1]["take_id"]]
    one_page_query = {**query, "max_characters": 40, "limit": 100}
    one_page = _collect_chapter_text_pages(service, one_page_query, expected_texts=expected, cap=40)
    assert len(one_page) == 1


def test_chapter_text_default_is_12000_with_shared_prompt_spoken_budget(tmp_path):
    service, _prose, _tagged = _fixture(tmp_path)
    text = "x" * 7000
    prose = tagged = _docx(text)
    (tmp_path / "Chapters/1/chapter.docx").write_bytes(prose)
    (tmp_path / "Chapters/1/chapter_audio-tags.docx").write_bytes(tagged)
    layout_path = tmp_path / "Project Files/Book_Layout.json"
    layout = json.loads(layout_path.read_text(encoding="utf-8"))
    layout["test_authorizations"][0]["source_raw_sha256"] = hashlib.sha256(prose).hexdigest()
    layout_path.write_text(json.dumps(layout), encoding="utf-8")
    prepared = _prepare_test_plan(service, "paging-default", prose, tagged, None, [
        {"chunk_id": "long", "start": 0, "end": len(text), "request_spec": None},
    ], request_limit=8000)
    query = {"project": "fixture", "chapter_id": "ch1",
             "scope": {"kind": "test", "authorization_id": "test-auth"},
             "snapshot_id": prepared["snapshot_id"], "include_text": True}
    expected = {"long": {"prompt": text, "spoken_text": text}}
    pages = _collect_chapter_text_pages(service, query, expected_texts=expected, cap=12000)
    assert len(pages) == 2
    assert len(pages[0]["returned_texts"][0]["prompt"]["text"]) + len(pages[0]["returned_texts"][0]["spoken_text"]["text"]) == 12000
    assert len(_collect_chapter_text_pages(service, {**query, "max_characters": 40000},
                                         expected_texts=expected, cap=40000)) == 1


@pytest.mark.parametrize("cap", [7, 18, 40])
def test_prepare_freezes_exact_tag_spans_and_chunk_spoken_facts(tmp_path, cap):
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
    query = {"project": "fixture", "chapter_id": "ch1",
             "scope": {"kind": "test", "authorization_id": "test-auth"},
             "snapshot_id": prepared["snapshot_id"], "include_text": True, "max_characters": cap, "limit": 1}
    _collect_chapter_text_pages(service, query, cap=cap,
                               expected_texts={"repeated": {"prompt": "repeat [tag] repeat", "spoken_text": spoken}})


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


def test_inspect_refinement_uses_a_pinned_base_and_detects_each_source_drift(tmp_path):
    service, prose, tagged = _fixture(tmp_path, bound=True)
    prose = _docx_paragraphs("first", "second")
    tagged = _docx_paragraphs("first", "second")
    (tmp_path / "Chapters/1/chapter.docx").write_bytes(prose)
    (tmp_path / "Chapters/1/chapter_audio-tags.docx").write_bytes(tagged)
    ProjectState.initialize(tmp_path)
    initial = _inspect(service)
    paragraph_ids = [item["paragraph_id"] for item in initial["paragraphs"]]
    state = service._state_required()
    payload = state.load_view(initial["document_view_id"])["payload"]
    assert base64.b64decode(payload["pinned_prose_base64"]) == prose
    assert base64.b64decode(payload["pinned_tagged_base64"]) == tagged

    with pytest.raises(BookServiceError) as no_base:
        service.inspect(InspectRequest.model_validate({
            "project": "fixture", "chapter_id": "ch1",
            "prose_filepath": "Chapters/1/chapter.docx",
            "tagged_filepath": "Chapters/1/chapter_audio-tags.docx",
            "speech_paragraph_ids": paragraph_ids, "excluded_paragraphs": [],
        }))
    assert no_base.value.reason == "validation_failed"

    refined = service.inspect(InspectRequest.model_validate({
        "project": "fixture", "chapter_id": "ch1",
        "prose_filepath": "Chapters/1/chapter.docx",
        "tagged_filepath": "Chapters/1/chapter_audio-tags.docx",
        "base_document_view_id": initial["document_view_id"],
        "speech_paragraph_ids": [paragraph_ids[0]],
        "excluded_paragraphs": [{"paragraph_id": paragraph_ids[1], "reason": "editorial_direction"}],
    }))
    assert refined["document_view_id"] != initial["document_view_id"]
    assert refined["speech_text"] == "first"
    assert [item["paragraph_id"] for item in refined["paragraphs"]] == paragraph_ids

    prose_path = tmp_path / "Chapters/1/chapter.docx"
    tagged_path = tmp_path / "Chapters/1/chapter_audio-tags.docx"
    prose_path.write_bytes(_docx("changed prose"))
    with pytest.raises(BookServiceError) as prose_drift:
        service.inspect(InspectRequest.model_validate({
            "project": "fixture", "chapter_id": "ch1",
            "prose_filepath": "Chapters/1/chapter.docx",
            "tagged_filepath": "Chapters/1/chapter_audio-tags.docx",
            "base_document_view_id": initial["document_view_id"],
            "speech_paragraph_ids": [paragraph_ids[0]],
            "excluded_paragraphs": [{"paragraph_id": paragraph_ids[1], "reason": "editorial_direction"}],
        }))
    assert prose_drift.value.reason == "stale_file"
    prose_path.write_bytes(prose)
    tagged_path.write_bytes(_docx("changed tagged"))
    with pytest.raises(BookServiceError) as tagged_drift:
        service.inspect(InspectRequest.model_validate({
            "project": "fixture", "chapter_id": "ch1",
            "prose_filepath": "Chapters/1/chapter.docx",
            "tagged_filepath": "Chapters/1/chapter_audio-tags.docx",
            "base_document_view_id": initial["document_view_id"],
            "speech_paragraph_ids": [paragraph_ids[0]],
            "excluded_paragraphs": [{"paragraph_id": paragraph_ids[1], "reason": "editorial_direction"}],
        }))
    assert tagged_drift.value.reason == "stale_file"


def test_initial_inspect_recreates_a_legacy_unpinned_view(tmp_path):
    service, prose, tagged = _fixture(tmp_path, bound=True)
    state = ProjectState.initialize(tmp_path)
    legacy = project_docx_pair(prose, tagged)
    state.save_view(
        view_id=legacy.document_view_id, chapter_id="ch1", scope_json="{}",
        payload={"projection": {"legacy": True}},
        expires_at=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
    )
    recreated = _inspect(service)
    assert recreated["document_view_id"] != legacy.document_view_id
    payload = state.load_view(recreated["document_view_id"])["payload"]
    assert base64.b64decode(payload["pinned_prose_base64"]) == prose
    assert base64.b64decode(payload["pinned_tagged_base64"]) == tagged


def test_inspect_view_identity_binds_identical_pairs_to_chapter_paths_and_layout(tmp_path):
    service, prose, tagged = _fixture(tmp_path, bound=True)
    ProjectState.initialize(tmp_path)
    chapter_two = tmp_path / "Chapters/2"
    chapter_two.mkdir()
    (chapter_two / "chapter.docx").write_bytes(prose)
    (chapter_two / "chapter_audio-tags.docx").write_bytes(tagged)
    state = json.loads((tmp_path / "Chapters/1/chapter.json").read_text(encoding="utf-8"))
    state["chapter_id"] = "ch2"
    (chapter_two / "chapter.json").write_text(json.dumps(state), encoding="utf-8")
    layout_path = tmp_path / "Project Files/Book_Layout.json"
    layout = json.loads(layout_path.read_text(encoding="utf-8"))
    chapter = dict(layout["chapters"][0])
    chapter.update({
        "chapter_id": "ch2", "title": "Chapter 2", "chapter_state_filepath": "Chapters/2/chapter.json",
        "working_filepath": "Chapters/2/chapter.docx", "tagged_filepath": "Chapters/2/chapter_audio-tags.docx",
        "originals_root": "Chapters/2/Originals", "audio_root": "Audiobook/Chapters/2",
    })
    layout["chapters"].append(chapter)
    layout["chapter_order"].append("ch2")
    layout_path.write_text(json.dumps(layout), encoding="utf-8")
    first = _inspect(service)
    second = service.inspect(InspectRequest.model_validate({
        "project": "fixture", "chapter_id": "ch2",
        "prose_filepath": "Chapters/2/chapter.docx",
        "tagged_filepath": "Chapters/2/chapter_audio-tags.docx",
    }))
    assert first["document_view_id"] != second["document_view_id"]
    layout["layout_revision"] = 2
    layout_path.write_text(json.dumps(layout), encoding="utf-8")
    revised = _inspect(service)
    assert revised["document_view_id"] != first["document_view_id"]
    assert service._state_required().load_view(first["document_view_id"]) is not None
    assert service._state_required().load_view(second["document_view_id"]) is not None
    assert service._state_required().load_view(revised["document_view_id"]) is not None


def test_inspect_pagination_budgets_speech_and_all_paragraph_text_without_omission(tmp_path):
    service, _prose, _tagged = _fixture(tmp_path, bound=True)
    prose = _docx_paragraphs("a" * 7000, "b" * 7000)
    tagged = _docx_paragraphs("a" * 7000, "b" * 7000)
    (tmp_path / "Chapters/1/chapter.docx").write_bytes(prose)
    (tmp_path / "Chapters/1/chapter_audio-tags.docx").write_bytes(tagged)
    ProjectState.initialize(tmp_path)
    initial = _inspect(service)
    ids = [item["paragraph_id"] for item in initial["paragraphs"]]
    page = service.inspect(InspectRequest.model_validate({
        "project": "fixture", "chapter_id": "ch1",
        "prose_filepath": "Chapters/1/chapter.docx",
        "tagged_filepath": "Chapters/1/chapter_audio-tags.docx",
        "base_document_view_id": initial["document_view_id"],
        "speech_paragraph_ids": [ids[0]],
        "excluded_paragraphs": [{"paragraph_id": ids[1], "reason": "editorial_note"}],
    }))
    assert page["speech_text_total_codepoints"] == 7000
    assert sum(len(item["text"]) for item in page["paragraphs"]) + len(page["speech_text"]) <= 12000
    assert page["has_more"]

    speech = []
    paragraphs = {item: [] for item in ids}
    local_ends = {item: 0 for item in ids}
    while True:
        assert sum(len(item["text"]) for item in page["paragraphs"]) + len(page["speech_text"]) <= 12000
        speech.append(page["speech_text"])
        for item in page["paragraphs"]:
            assert item["paragraph_returned_start"] == local_ends[item["paragraph_id"]]
            local_ends[item["paragraph_id"]] = item["paragraph_returned_end"]
            paragraphs[item["paragraph_id"]].append(item["text"])
        if not page["has_more"]:
            break
        page = service.inspect(InspectRequest.model_validate({
            "project": "fixture", "chapter_id": "ch1",
            "prose_filepath": "Chapters/1/chapter.docx",
            "tagged_filepath": "Chapters/1/chapter_audio-tags.docx",
            "cursor": page["next_cursor"],
        }))
    assert "".join(speech) == "a" * 7000
    assert "".join(paragraphs[ids[0]]) == "a" * 7000
    assert "".join(paragraphs[ids[1]]) == "b" * 7000


def test_inspect_pagination_tiny_budget_never_advances_past_unreturned_fields(tmp_path):
    service, _prose, _tagged = _fixture(tmp_path, bound=True)
    prose = _docx_paragraphs("alpha", "bravocharlie")
    tagged = _docx_paragraphs("alpha", "bravocharlie")
    (tmp_path / "Chapters/1/chapter.docx").write_bytes(prose)
    (tmp_path / "Chapters/1/chapter_audio-tags.docx").write_bytes(tagged)
    ProjectState.initialize(tmp_path)
    page = service.inspect(InspectRequest.model_validate({
        "project": "fixture", "chapter_id": "ch1",
        "prose_filepath": "Chapters/1/chapter.docx",
        "tagged_filepath": "Chapters/1/chapter_audio-tags.docx",
        "max_characters": 3,
    }))
    speech, paragraphs = [], {}
    while True:
        assert sum(len(item["text"]) for item in page["paragraphs"]) + len(page["speech_text"]) <= 3
        speech.append(page["speech_text"])
        for item in page["paragraphs"]:
            paragraphs.setdefault(item["paragraph_id"], []).append(item["text"])
        if not page["has_more"]:
            break
        page = service.inspect(InspectRequest.model_validate({
            "project": "fixture", "chapter_id": "ch1",
            "prose_filepath": "Chapters/1/chapter.docx",
            "tagged_filepath": "Chapters/1/chapter_audio-tags.docx",
            "cursor": page["next_cursor"], "max_characters": 3,
        }))
    assert "".join(speech) == "alpha\n\nbravocharlie"
    assert sorted("".join(value) for value in paragraphs.values()) == ["alpha", "bravocharlie"]


def test_inspect_cursor_uses_frozen_view_and_rejects_changed_arguments(tmp_path):
    service, prose, tagged = _fixture(tmp_path, bound=True)
    ProjectState.initialize(tmp_path)
    first = service.inspect(InspectRequest.model_validate({
        "project": "fixture", "chapter_id": "ch1",
        "prose_filepath": "Chapters/1/chapter.docx",
        "tagged_filepath": "Chapters/1/chapter_audio-tags.docx",
        "max_characters": 2,
    }))
    assert first["speech_text"] == "he" and first["has_more"]
    (tmp_path / "Chapters/1/chapter.docx").write_bytes(_docx("changed prose"))
    (tmp_path / "Chapters/1/chapter_audio-tags.docx").write_bytes(_docx("changed tagged"))
    second = service.inspect(InspectRequest.model_validate({
        "project": "fixture", "chapter_id": "ch1",
        "prose_filepath": "Chapters/1/chapter.docx",
        "tagged_filepath": "Chapters/1/chapter_audio-tags.docx",
        "cursor": first["next_cursor"], "max_characters": 2,
    }))
    assert second["speech_text"] == "ll"
    with pytest.raises(BookServiceError) as changed_path:
        service.inspect(InspectRequest.model_validate({
            "project": "fixture", "chapter_id": "ch1",
            "prose_filepath": "Chapters/1/other.docx",
            "tagged_filepath": "Chapters/1/chapter_audio-tags.docx",
            "cursor": first["next_cursor"],
        }))
    assert changed_path.value.reason == "invalid_cursor"
    with pytest.raises(BookServiceError) as changed_selection:
        service.inspect(InspectRequest.model_validate({
            "project": "fixture", "chapter_id": "ch1",
            "prose_filepath": "Chapters/1/chapter.docx",
            "tagged_filepath": "Chapters/1/chapter_audio-tags.docx",
            "cursor": first["next_cursor"], "speech_paragraph_ids": [],
        }))
    assert changed_selection.value.reason == "invalid_cursor"
    with pytest.raises(BookServiceError) as changed_tags:
        service.inspect(InspectRequest.model_validate({
            "project": "fixture", "chapter_id": "ch1",
            "prose_filepath": "Chapters/1/chapter.docx",
            "tagged_filepath": "Chapters/1/chapter_audio-tags.docx",
            "cursor": first["next_cursor"],
            "explicit_tag_spans": [{
                "paragraph_id": first["paragraphs"][0]["paragraph_id"], "start": 0, "end": 1,
                "expected_text_sha256": hashlib.sha256(b"h").hexdigest(),
            }],
        }))
    assert changed_tags.value.reason == "invalid_cursor"
    with pytest.raises(BookServiceError) as changed_base:
        service.inspect(InspectRequest.model_validate({
            "project": "fixture", "chapter_id": "ch1",
            "prose_filepath": "Chapters/1/chapter.docx",
            "tagged_filepath": "Chapters/1/chapter_audio-tags.docx",
            "cursor": first["next_cursor"], "base_document_view_id": "0" * 64,
        }))
    assert changed_base.value.reason == "invalid_cursor"


def test_inspect_view_expiry_and_prepare_current_source_guard(tmp_path):
    service, prose, tagged = _fixture(tmp_path, bound=True)
    ProjectState.initialize(tmp_path)
    inspected = _inspect(service)
    state = service._state_required()
    row = state.load_view(inspected["document_view_id"])
    state.save_view(
        view_id=inspected["document_view_id"], chapter_id="ch1", scope_json="{}",
        payload=row["payload"], expires_at=(datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),
    )
    with pytest.raises(BookServiceError) as expired:
        service.inspect(InspectRequest.model_validate({
            "project": "fixture", "chapter_id": "ch1",
            "prose_filepath": "Chapters/1/chapter.docx",
            "tagged_filepath": "Chapters/1/chapter_audio-tags.docx",
            "base_document_view_id": inspected["document_view_id"],
        }))
    assert expired.value.reason == "view_expired"

    current = _inspect(service)
    (tmp_path / "Chapters/1/chapter.docx").write_bytes(_docx("changed prose"))
    with pytest.raises(BookServiceError) as stale_prepare:
        service.prepare(PrepareRequest.model_validate({
            "project": "fixture", "operation_id": "pinned-stale-prepare", "chapter_id": "ch1",
            "document_view_id": current["document_view_id"],
            "expected_prose_sha256": hashlib.sha256(prose).hexdigest(),
            "expected_tagged_sha256": hashlib.sha256(tagged).hexdigest(),
            "expected_manifest_revision": None, "scope": {"kind": "test", "authorization_id": "test-auth"},
            "speech_selection_confirmed": True,
            "request_limit": {"value": 100, "unit": "unicode_codepoints"},
            "expected_settings_sha256": None, "production_target": None,
            "chunks": [{"chunk_id": "pinned", "start": 0, "end": 5, "request_spec": None}],
            "publish_bookmarks_to_working_tagged_docx": False,
        }), owner_key="principal:fixture")
    assert stale_prepare.value.reason == "stale_source"


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


def test_captured_chapter_index_projection_removes_only_verified_spans_and_refuses_config_rebind(tmp_path):
    from cognita.parsing import compute_doc_id, parse_file

    service, _prose, _tagged = _fixture(tmp_path, bound=True)
    ProjectState.initialize(tmp_path)
    raw = _docx_paragraphs("keep [literal] delete-me", "retain second paragraph")
    source = tmp_path / "Chapters/1/chapter.docx"
    source.write_bytes(raw)
    (tmp_path / "Chapters/1/chapter_audio-tags.docx").write_bytes(raw)
    projection = parse_docx(raw)
    first = projection.paragraphs[0]
    start = first.text.index("delete-me")
    chapter_state_path = tmp_path / "Chapters/1/chapter.json"
    chapter_state = json.loads(chapter_state_path.read_text(encoding="utf-8"))
    chapter_state["index_annotations"] = {
        "source_raw_sha256": hashlib.sha256(raw).hexdigest(),
        "extraction_version": "cognita-docx-v1",
        "spans": [{
            "paragraph_id": first.paragraph_id, "start": start, "end": start + len("delete-me"),
            "expected_text_sha256": hashlib.sha256(b"delete-me").hexdigest(), "reason": "editorial",
        }],
    }
    chapter_state_path.write_text(json.dumps(chapter_state), encoding="utf-8")

    parsed = parse_file(
        source, tmp_path,
        captured_content=lambda path, suffix, captured: service.index_captured_content(path, suffix, captured),
    )
    assert parsed is not None
    assert parsed.content == "keep [literal] \n\nretain second paragraph"
    assert "delete-me" not in parsed.content
    assert parsed.book_index_context is not None
    assert parsed.captured_raw is None
    captured = service.capture_index_document(parsed)
    record = captured.book_index_record
    assert record is not None
    assert record.extraction_version == "cognita-docx-v1"
    assert record.doc_id == compute_doc_id(record.source_path, captured.content_hash)

    # A new configuration cannot relabel the already projected source bytes.
    layout_path = tmp_path / "Project Files/Book_Layout.json"
    layout = json.loads(layout_path.read_text(encoding="utf-8"))
    layout["chapters"] = []
    layout["chapter_order"] = []
    layout_path.write_text(json.dumps(layout), encoding="utf-8")
    assert not service.index_provenance_is_current(record)


async def test_registered_reference_reconcile_persists_refreshed_captured_provenance(tmp_path):
    from cognita.retrieval import RetrievalCore
    from retrieval_fakes import HashEmbedder
    from test_retrieval_reconciliation import ReconcileStore

    service, _prose, _tagged = _fixture(tmp_path, bound=True)
    ProjectState.initialize(tmp_path)
    store = ReconcileStore()
    async def no_existing_document(_project, _source):
        return None
    store.get_document = no_existing_document
    core = RetrievalCore(store, HashEmbedder())
    core.set_book_index_content_provider(
        lambda _project, source, suffix, raw: service.index_captured_content(source, suffix, raw)
    )
    core.set_book_index_capture_provider(lambda _project, document: service.capture_index_document(document))
    core.set_book_index_currentness_provider(lambda _project, record: service.index_provenance_is_current(record))
    core.set_book_index_provenance_recorder(lambda _project, record: service.record_index_provenance(record))
    source = tmp_path / "Project Files/ref.md"

    first = await core.index_file("fixture", tmp_path, source)
    assert first is not None and first.indexed
    state = ProjectState.discover(tmp_path)
    assert state is not None
    initial = state.indexed_role_provenance("Project Files/ref.md")
    assert initial is not None

    source.write_text("Reference facts changed externally", encoding="utf-8")
    reconciled = await core.reconcile_paths("fixture", tmp_path, ["Project Files/ref.md"])
    refreshed = state.indexed_role_provenance("Project Files/ref.md")
    assert reconciled["indexed"] == 1
    assert refreshed is not None and refreshed.raw_sha256 != initial.raw_sha256
    assert store.sources["Project Files/ref.md"].doc_id == refreshed.doc_id
    candidate = SimpleNamespace(
        source="Project Files/ref.md", doc_id=refreshed.doc_id,
        content_hash=refreshed.extracted_sha256,
    )
    assert set(service.index_admitted_doc_ids([candidate])) == {refreshed.doc_id}


@pytest.mark.parametrize("drift", ["source", "layout"])
async def test_captured_book_index_refuses_source_or_config_drift_during_embedding(tmp_path, drift):
    from cognita.retrieval import RetrievalCore
    from retrieval_fakes import HashEmbedder
    from test_retrieval_reconciliation import ReconcileStore

    class BlockingEmbedder(HashEmbedder):
        def __init__(self):
            super().__init__()
            self.started, self.release = threading.Event(), threading.Event()

        def embed(self, texts):
            self.started.set()
            assert self.release.wait(5)
            return super().embed(texts)

    service, _prose, _tagged = _fixture(tmp_path, bound=True)
    ProjectState.initialize(tmp_path)
    store, embedder = ReconcileStore(), BlockingEmbedder()
    async def no_existing_document(_project, _source):
        return None
    store.get_document = no_existing_document
    core = RetrievalCore(store, embedder)
    core.set_book_index_content_provider(lambda _p, source, suffix, raw: service.index_captured_content(source, suffix, raw))
    core.set_book_index_capture_provider(lambda _p, document: service.capture_index_document(document))
    core.set_book_index_currentness_provider(lambda _p, record: service.index_provenance_is_current(record))
    core.set_book_index_provenance_recorder(lambda _p, record: service.record_index_provenance(record))
    source = tmp_path / "Project Files/ref.md"
    task = asyncio.create_task(core.index_file("fixture", tmp_path, source))
    await asyncio.to_thread(embedder.started.wait, 5)
    if drift == "source":
        source.write_text("changed after capture", encoding="utf-8")
    else:
        layout_path = tmp_path / "Project Files/Book_Layout.json"
        layout = json.loads(layout_path.read_text(encoding="utf-8"))
        layout["title"] = "changed after capture"
        layout_path.write_text(json.dumps(layout), encoding="utf-8")
    embedder.release.set()
    outcome = await task

    assert outcome is not None and not outcome.indexed
    assert outcome.exclusion_reason == "source_provenance_changed"
    assert store.replacements == []


def test_invalid_captured_chapter_annotation_blocks_before_legacy_docx_extraction(tmp_path):
    from cognita.parsing import parse_file

    service, prose, _tagged = _fixture(tmp_path, bound=True)
    ProjectState.initialize(tmp_path)
    projection = parse_docx(prose)
    chapter_state_path = tmp_path / "Chapters/1/chapter.json"
    chapter_state = json.loads(chapter_state_path.read_text(encoding="utf-8"))
    chapter_state["index_annotations"] = {
        "source_raw_sha256": "0" * 64,
        "extraction_version": "cognita-docx-v1",
        "spans": [],
    }
    chapter_state_path.write_text(json.dumps(chapter_state), encoding="utf-8")

    with pytest.raises(BookServiceError, match="index_annotations.source_raw_sha256") as raised:
        parse_file(
            tmp_path / "Chapters/1/chapter.docx", tmp_path,
            captured_content=lambda path, suffix, raw: service.index_captured_content(path, suffix, raw),
        )
    assert raised.value.reason == "validation_failed"
    assert projection.paragraphs  # Source projection was available; binding blocked publication.


def test_captured_chapter_annotation_errors_name_original_source_locations(tmp_path):
    """Annotation validation reports the caller's exact binding field."""
    from cognita.parsing import parse_file

    service, _prose, _tagged = _fixture(tmp_path, bound=True)
    ProjectState.initialize(tmp_path)
    raw = _docx_paragraphs("alpha bravo charlie")
    source = tmp_path / "Chapters/1/chapter.docx"
    source.write_bytes(raw)
    (tmp_path / "Chapters/1/chapter_audio-tags.docx").write_bytes(raw)
    projection = parse_docx(raw)
    paragraph = projection.paragraphs[0]
    state_path = tmp_path / "Chapters/1/chapter.json"
    base = json.loads(state_path.read_text(encoding="utf-8"))

    def checked_span(start: int, end: int) -> dict:
        return {
            "paragraph_id": paragraph.paragraph_id, "start": start, "end": end,
            "expected_text_sha256": hashlib.sha256(paragraph.text[start:end].encode("utf-8")).hexdigest(),
            "reason": "editorial",
        }

    cases = [
        ([{**checked_span(0, 5), "paragraph_id": "missing"}], "index_annotations.spans[0].paragraph_id"),
        ([checked_span(0, len(paragraph.text) + 1)], "index_annotations.spans[0].start/end"),
        ([{**checked_span(0, 5), "expected_text_sha256": "0" * 64}],
         "index_annotations.spans[0].expected_text_sha256"),
        ([checked_span(2, 5), checked_span(1, 4)],
         f"index_annotations.spans[0] overlaps {paragraph.paragraph_id}:2-5"),
    ]
    for spans, expected in cases:
        state = dict(base)
        state["index_annotations"] = {
            "source_raw_sha256": hashlib.sha256(raw).hexdigest(),
            "extraction_version": "cognita-docx-v1", "spans": spans,
        }
        state_path.write_text(json.dumps(state), encoding="utf-8")
        with pytest.raises(BookServiceError, match=expected.replace("[", r"\[").replace("]", r"\]")):
            parse_file(
                source, tmp_path,
                captured_content=lambda path, suffix, captured: service.index_captured_content(path, suffix, captured),
            )


@pytest.mark.parametrize("editorial_status", ["draft", "approved"])
def test_profile_admission_separates_drafts_canon_instructions_and_workflow(tmp_path, editorial_status):
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
    summary_path = "Chapters/1/summary.md"
    (tmp_path / summary_path).write_text("Approved summary", encoding="utf-8")
    layout["chapters"][0]["summary_filepath"] = summary_path
    layout_path.write_text(json.dumps(layout), encoding="utf-8")
    chapter_state_path = tmp_path / "Chapters/1/chapter.json"
    chapter_state = json.loads(chapter_state_path.read_text(encoding="utf-8"))
    source_sha = hashlib.sha256(valid_prose).hexdigest()
    projected = project_docx_pair(valid_prose, valid_prose)
    if editorial_status == "approved":
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
    chapter_state["summary"] = {
        "filepath": summary_path, "source_raw_sha256": source_sha,
        "source_prose_projection_sha256": projected.prose_projection_sha256,
        "summary_raw_sha256": hashlib.sha256((tmp_path / summary_path).read_bytes()).hexdigest(),
        "approved": True, "actor": "fixture", "approved_at": datetime.now(timezone.utc).isoformat(),
    }
    chapter_state_path.write_text(json.dumps(chapter_state), encoding="utf-8")
    ProjectState.initialize(tmp_path)
    candidates = []
    paths = [
        "Project Files/ref.md", "Project Files/guide.md",
        "Project Files/workflow.md", "Chapters/1/chapter.docx", summary_path,
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
        ids["Project Files/ref.md"], ids["Chapters/1/chapter.docx"], ids[summary_path],
    }
    canon_ids = {ids["Project Files/ref.md"], ids[summary_path]}
    if editorial_status == "approved":
        canon_ids.add(ids["Chapters/1/chapter.docx"])
    assert set(service.index_admitted_doc_ids(candidates, "canon")) == canon_ids
    assert set(service.index_admitted_doc_ids(candidates, "instructions")) == {
        ids["Project Files/guide.md"],
    }
    assert set(service.index_admitted_doc_ids(candidates, "workflow")) == {
        ids["Project Files/workflow.md"],
    }
    chapter_state["summary"]["source_raw_sha256"] = "0" * 64
    chapter_state_path.write_text(json.dumps(chapter_state), encoding="utf-8")
    for profile in ("editing", "canon", "instructions", "workflow"):
        assert ids[summary_path] not in service.index_admitted_doc_ids(candidates, profile)


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


@pytest.mark.parametrize("facts", [
    {"headers": {"Authorization": "Bearer synthetic_secret"}},
    {"download_url": "https://media.example/audio?signature=synthetic_secret"},
    {"endpoint": "https://user:synthetic_secret@media.example/audio"},
    {"nested": {"x-api-key": "synthetic_secret"}},
    {"links": ["https://example.test/audio?sig=synthetic_fixture"]},
    {"href": "https://fixture_user:synthetic_fixture@example.test/audio"},
    {"nested": [{"location": "HTTPS://example.test/audio?X-Amz-Credential=synthetic_fixture"}]},
    {"href": " https://example.test/audio?sig=synthetic_fixture \n"},
])
def test_durable_provider_facts_refuse_explicit_transport_credentials(facts):
    with pytest.raises(BookServiceError) as rejected:
        _validate_durable_provider_facts(facts)
    assert rejected.value.reason == "validation_failed"
    assert "synthetic_secret" not in str(rejected.value)


def test_durable_provider_facts_preserve_public_nonsecret_structures():
    facts = {
        "provider_id": "provider-7", "public_url": "https://example.org/evidence",
        "links": ["https://example.org/audio?format=pcm", {"href": "https://example.org/evidence"}],
        "previous_text": "https://fixture_user:synthetic_fixture@example.test/audio",
        "prompt": "https://example.test/audio?sig=synthetic_fixture",
        "next_text": "Literal prose https://example.test/audio?sig=synthetic_fixture",
        "nested": {"enabled": True, "attempts": [1, 2, {"format": "pcm"}]},
    }
    _validate_durable_provider_facts(facts)


@pytest.mark.parametrize("metadata", [
    {"nested": {"Authorization": "Bearer synthetic_secret"}},
    {"download_url": "https://media.example/audio?signature=synthetic_secret"},
    {"download_url": "https://example.test/audio?api_key=synthetic_fixture"},
    {"download_url": "https://example.test/audio?X-Amz-Credential=synthetic_fixture"},
    {"signed_download_url": "https://example.test/audio?sig=synthetic_fixture"},
    {"links": ["https://example.test/audio?sig=synthetic_fixture"]},
    {"href": "https://fixture_user:synthetic_fixture@example.test/audio"},
    {"nested": [{"location": "https://example.test/audio?access_token=synthetic_fixture"}]},
    {"href": " https://example.test/audio?sig=synthetic_fixture \n"},
])
def test_generation_update_rejects_credentials_before_sqlite_mutation(tmp_path, metadata):
    service, _prose, _tagged, _spec, _prepared, request = _generation_cas_fixture(tmp_path)
    reserved, replayed = service.record_generation(request, owner_key="principal:fixture")
    assert not replayed
    state = service._state_required()
    generation = reserved["generation"]
    before = state.generation(generation["generation_record_id"])
    operation_id = "credential-update-" + str(len(metadata))

    with pytest.raises(BookServiceError) as rejected:
        service.record_generation(RecordGenerationRequest.model_validate({
            "project": "fixture", "operation_id": operation_id, "change": {
                "kind": "update", "generation_record_id": generation["generation_record_id"],
                "expected_generation_revision": generation["generation_revision"],
                "state": "reserved", "provider_response_metadata": metadata,
            },
        }), owner_key="principal:fixture")

    assert rejected.value.reason == "validation_failed"
    assert "synthetic_secret" not in str(rejected.value)
    assert state.generation(generation["generation_record_id"]) == before
    assert state.receipt(
        owner_key="principal:fixture", project="fixture",
        tool="audiobook_record_generation", operation_id=operation_id,
    ) is None
    reopened_state = BookService(tmp_path, "fixture")._state_required()
    assert reopened_state.generation(generation["generation_record_id"]) == before
    assert reopened_state.receipt(
        owner_key="principal:fixture", project="fixture",
        tool="audiobook_record_generation", operation_id=operation_id,
    ) is None


def test_generation_allows_nonsecret_evidence_and_reopens_exact_json(tmp_path):
    service, _prose, _tagged, _spec, _prepared, request = _generation_cas_fixture(tmp_path)
    reserved, replayed = service.record_generation(request, owner_key="principal:fixture")
    assert not replayed
    generation = reserved["generation"]
    provider_ids = {"flow_id": "flow-public", "generation_ids": ["provider-7"]}
    metadata = {
        "public_url": "https://example.org/evidence", "provider_id": "provider-7",
        "links": ["https://example.org/audio?format=pcm", {"href": "https://example.org/evidence"}],
        "nested": {"attempt": 1, "formats": ["pcm", "wav"]},
    }
    updated, replayed = service.record_generation(RecordGenerationRequest.model_validate({
        "project": "fixture", "operation_id": "public-evidence-update", "change": {
            "kind": "update", "generation_record_id": generation["generation_record_id"],
            "expected_generation_revision": generation["generation_revision"],
            "state": "submitted", "provider_ids": provider_ids,
            "provider_response_metadata": metadata,
        },
    }), owner_key="principal:fixture")
    assert not replayed
    assert updated["generation"]["provider_ids"] == provider_ids
    assert updated["generation"]["provider_response_metadata"] == metadata

    reopened = BookService(tmp_path, "fixture")
    persisted = reopened._state_required().generation(generation["generation_record_id"])
    assert persisted is not None
    assert persisted["provider_ids"] == provider_ids
    assert persisted["provider_response_metadata"] == metadata


@pytest.mark.parametrize("request_spec", [
    {
        "provider": "synthetic", "route": "fixture", "model_id": "model", "voice_id": "voice",
        "parameters": {"nested": {"Authorization": "Bearer synthetic_secret"}}, "context_fields": {},
    },
    {
        "provider": "synthetic", "route": "fixture", "model_id": "model", "voice_id": "voice",
        "parameters": {},
        "context_fields": {"download_url": "https://media.example/audio?signature=synthetic_secret"},
    },
    {
        "provider": "synthetic", "route": "fixture", "model_id": "model", "voice_id": "voice",
        "parameters": {"links": ["https://example.test/audio?sig=synthetic_fixture"]},
        "context_fields": {},
    },
    {
        "provider": "synthetic", "route": "fixture", "model_id": "model", "voice_id": "voice",
        "parameters": {},
        "context_fields": {"href": "https://fixture_user:synthetic_fixture@example.test/audio"},
    },
])
def test_prepare_rejects_credential_request_fields_before_snapshot_or_receipt(tmp_path, request_spec):
    service, prose, tagged = _fixture(tmp_path)
    inspected = _inspect(service)
    with pytest.raises(BookServiceError) as rejected:
        service.prepare(PrepareRequest.model_validate({
            "project": "fixture", "operation_id": "credential-prepare", "chapter_id": "ch1",
            "document_view_id": inspected["document_view_id"],
            "expected_prose_sha256": hashlib.sha256(prose).hexdigest(),
            "expected_tagged_sha256": hashlib.sha256(tagged).hexdigest(),
            "expected_manifest_revision": None, "scope": {"kind": "test", "authorization_id": "test-auth"},
            "speech_selection_confirmed": True,
            "request_limit": {"value": 100, "unit": "unicode_codepoints"},
            "expected_settings_sha256": None, "production_target": None,
            "chunks": [{"chunk_id": "guarded", "start": 0, "end": 5, "request_spec": request_spec}],
            "publish_bookmarks_to_working_tagged_docx": False,
        }), owner_key="principal:fixture")

    assert rejected.value.reason == "validation_failed"
    assert "synthetic_secret" not in str(rejected.value)
    assert not (tmp_path / ".cognita-storage").exists()


@pytest.mark.parametrize("previous_text", [
    "Prior prose with a literal https://example.test/audio?api_key=synthetic_fixture",
    "https://example.test/audio?sig=synthetic_fixture",
])
def test_prepare_keeps_nonsecret_context_fingerprinted_and_retained(tmp_path, previous_text):
    service, prose, tagged = _fixture(tmp_path)
    spec = {
        "provider": "synthetic", "route": "fixture", "model_id": "model", "voice_id": "voice",
        "parameters": {"format": "pcm", "public_url": "https://example.org/reference"},
        "context_fields": {
            "previous_text": previous_text,
            "additional": {"provider_id": "provider-7", "attempt": 1},
        },
    }
    prepared = _prepare_test_plan(service, "safe-context-prepare", prose, tagged, None, [
        {"chunk_id": "safe-context", "start": 0, "end": 5, "request_spec": spec},
    ])
    chunk = prepared["chunks"][0]
    assert chunk["request_spec"] == spec
    assert chunk["request_sha256"] == request_fingerprint("hello", spec)
    frozen = service._state_required().snapshot(prepared["snapshot_id"])
    assert frozen["payload"]["result"]["chunks"][0]["request_spec"] == spec
    reopened = BookService(tmp_path, "fixture")._state_required().snapshot(prepared["snapshot_id"])
    assert reopened["payload"]["result"]["chunks"][0]["request_spec"] == spec


def test_generation_reservation_rejects_credential_request_before_sqlite_mutation(tmp_path):
    service, prose, tagged = _fixture(tmp_path)
    spec = {
        "provider": "synthetic", "route": "fixture", "model_id": "model", "voice_id": "voice",
        "parameters": {}, "context_fields": {},
    }
    prepared = _prepare_test_plan(service, "guard-reserve-prepare", prose, tagged, None, [
        {"chunk_id": "guard-reserve", "start": 0, "end": 5, "request_spec": spec},
    ])
    state = service._state_required()
    chunk = prepared["chunks"][0]
    with pytest.raises(BookServiceError) as rejected:
        service.record_generation(RecordGenerationRequest.model_validate({
            "project": "fixture", "operation_id": "guard-reserve", "change": {
                "kind": "reserve", "chapter_id": "ch1", "snapshot_id": prepared["snapshot_id"],
                "chunk_id": "guard-reserve", "expected_manifest_revision": prepared["manifest_revision"],
                "request": {
                    "prompt_sha256": chunk["prompt_sha256"],
                    "spec": {**spec, "parameters": {"Authorization": "Bearer synthetic_secret"}},
                },
            },
        }), owner_key="principal:fixture")

    assert rejected.value.reason == "validation_failed"
    assert "synthetic_secret" not in str(rejected.value)
    assert state.generations(chapter_id="ch1") == []
    assert state.receipt(
        owner_key="principal:fixture", project="fixture",
        tool="audiobook_record_generation", operation_id="guard-reserve",
    ) is None


def _generation_cas_fixture(tmp_path):
    service, prose, tagged = _fixture(tmp_path)
    spec = {"provider": "synthetic", "route": "fixture", "model_id": "model",
            "voice_id": "voice", "parameters": {}, "context_fields": {}}
    prepared = _prepare_test_plan(service, "cas-prepare-1", prose, tagged, None, [
        {"chunk_id": "cas-chunk", "start": 0, "end": 5, "request_spec": spec},
    ])
    request = RecordGenerationRequest.model_validate({
        "project": "fixture", "operation_id": "cas-reserve-old", "change": {
            "kind": "reserve", "chapter_id": "ch1", "snapshot_id": prepared["snapshot_id"],
            "chunk_id": "cas-chunk", "expected_manifest_revision": prepared["manifest_revision"],
            "request": {"prompt_sha256": prepared["chunks"][0]["prompt_sha256"], "spec": spec},
        },
    })
    return service, prose, tagged, spec, prepared, request


def test_generation_current_manifest_cas_preserves_old_replay_updates_and_late_import(tmp_path):
    service, prose, tagged, spec, old_plan, old_request = _generation_cas_fixture(tmp_path)
    original, replayed = service.record_generation(old_request, owner_key="principal:fixture")
    assert not replayed
    new_plan = _prepare_test_plan(service, "cas-prepare-2", prose, tagged, old_plan["manifest_revision"], [
        {"chunk_id": "cas-chunk", "start": 0, "end": 5,
         "request_spec": {**spec, "context_fields": {"previous_text": "new context"}}},
    ])
    assert old_plan["manifest_revision"] == 1 and new_plan["manifest_revision"] == 2
    stale_args = old_request.model_dump(mode="json", exclude_unset=True)
    stale_args["operation_id"] = "cas-new-stale-reservation"
    with pytest.raises(BookServiceError) as stale:
        service.record_generation(RecordGenerationRequest.model_validate(stale_args), owner_key="principal:fixture")
    assert stale.value.reason == "stale_manifest"
    state = service._state_required()
    assert len(state.generations(chapter_id="ch1")) == 1
    assert state.receipt(owner_key="principal:fixture", project="fixture",
                         tool="audiobook_record_generation", operation_id=stale_args["operation_id"]) is None
    assert service.record_generation(old_request, owner_key="principal:fixture") == (original, True)

    generation = original["generation"]
    for status in ("submitted", "completed"):
        updated, replayed = service.record_generation(RecordGenerationRequest.model_validate({
            "project": "fixture", "operation_id": f"cas-old-{status}", "change": {
                "kind": "update", "generation_record_id": generation["generation_record_id"],
                "expected_generation_revision": generation["generation_revision"], "state": status,
                "provider_ids": {"generation_ids": ["synthetic-cas-provider"]},
                "provider_response_metadata": {"format": "synthetic raw s16le"},
            },
        }), owner_key="principal:fixture")
        assert not replayed
        generation = updated["generation"]
    samples = b"\x00\x00\x01\x00"
    source = tmp_path / "Audiobook/Chapters/1/cas-old.pcm"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(samples)
    job, _ = service.import_audio(ImportAudioRequest.model_validate({
        "project": "fixture", "operation_id": "cas-late-import",
        "generation_record_id": generation["generation_record_id"],
        "expected_generation_revision": generation["generation_revision"],
        "source": {"kind": "project_file", "filepath": "Audiobook/Chapters/1/cas-old.pcm",
                   "expected_sha256": hashlib.sha256(samples).hexdigest()},
        "provenance": "native_generation", "source_format": {
            "container": "raw_pcm", "encoding": "signed_integer", "sample_rate_hz": 8000,
            "channels": 1, "storage_bits": 16, "valid_bits": 16, "endianness": "little",
            "interleaving": "interleaved", "provider_format_evidence": "synthetic raw s16le",
        },
    }), owner_key="principal:fixture")
    service.run_import_job(job["job_id"])
    completed = service.get_job(GetJobRequest(project="fixture", job_id=job["job_id"]))
    assert completed["state"] == "succeeded", completed
    assert completed["result"]["take"]["snapshot_id"] == old_plan["snapshot_id"]
    # Use the authoritative stored scope spelling, independent of key order.
    namespace = state.namespace("ch1", state.snapshot(old_plan["snapshot_id"])["scope_key"])
    assert namespace["current_snapshot_id"] == new_plan["snapshot_id"]
    assert namespace["head_revision"] is None


@pytest.mark.parametrize("changed_field", [
    "manifest_revision", "current_snapshot_id", "current_plan_sha256", "missing_namespace",
])
def test_generation_cas_rechecks_namespace_inside_receipt_transaction(tmp_path, monkeypatch, changed_field):
    service, _prose, _tagged, _spec, prepared, request = _generation_cas_fixture(tmp_path)
    state = service._state_required()
    scope_key = state.snapshot(prepared["snapshot_id"])["scope_key"]
    original_reserve = state.reserve_generation

    def race_before_transaction(**kwargs):
        with state.transaction() as connection:
            if changed_field == "missing_namespace":
                connection.execute("DELETE FROM book_namespaces WHERE chapter_id=? AND scope_key=?", ("ch1", scope_key))
            else:
                # Column names are fixed test parameters, never request input.
                value = 2 if changed_field == "manifest_revision" else "0" * 64
                connection.execute(f"UPDATE book_namespaces SET {changed_field}=? WHERE chapter_id=? AND scope_key=?",
                                   (value, "ch1", scope_key))
        return original_reserve(**kwargs)

    monkeypatch.setattr(state, "reserve_generation", race_before_transaction)
    with pytest.raises(BookServiceError) as stale:
        service.record_generation(request, owner_key="principal:fixture")
    assert stale.value.reason == "stale_manifest"
    assert state.generations(chapter_id="ch1") == []
    assert state.receipt(owner_key="principal:fixture", project="fixture",
                         tool="audiobook_record_generation", operation_id=request.operation_id) is None


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

    # Staging and retained publication are distinct. The source is retained
    # once; conservative double-counting would reject this valid admission.
    layout["storage"]["quota_bytes"] = 1_000_015
    layout_path.write_text(json.dumps(layout), encoding="utf-8")
    sibling_audio = tmp_path / "Audiobook/Other"
    sibling_audio.mkdir(parents=True)
    (sibling_audio / "retained.mp3").write_bytes(b"abcdefghij")
    current_layout = BookLayout.model_validate(layout, strict=True)
    assert len(service._registered_audio_roots(current_layout)) == 1
    assert service._retained_audio_bytes(service._registered_audio_roots(current_layout)) == 20
    service._available_import_space(current_layout, service._chapter(current_layout, "ch1"), 500_000)
    layout["storage"]["quota_bytes"] = 500_019
    layout_path.write_text(json.dumps(layout), encoding="utf-8")
    current_layout = BookLayout.model_validate(layout, strict=True)
    with pytest.raises(BookServiceError) as quota:
        service._available_import_space(current_layout, service._chapter(current_layout, "ch1"), 500_000)
    assert quota.value.reason == "insufficient_storage"


def test_import_final_budget_counts_native_wrapper_and_fact_after_policy_change(tmp_path):
    service, prose, tagged = _fixture(tmp_path)
    generation, raw_format = _completed_raw_generation(service, prose, tagged)
    samples = struct.pack("<hhhh", 1, -2, 3, -4)
    source = tmp_path / "Audiobook/Chapters/1/final-budget.pcm"
    source.parent.mkdir(parents=True)
    source.write_bytes(samples)
    request = ImportAudioRequest.model_validate({
        "project": "fixture", "operation_id": "final-budget-import",
        "generation_record_id": generation["generation_record_id"],
        "expected_generation_revision": generation["generation_revision"],
        "source": {"kind": "project_file", "filepath": "Audiobook/Chapters/1/final-budget.pcm",
                   "expected_sha256": hashlib.sha256(samples).hexdigest()},
        "provenance": "native_generation", "source_format": raw_format,
    })
    queued, _ = service.import_audio(request, owner_key="principal:fixture")
    layout_path = tmp_path / "Project Files/Book_Layout.json"

    def lower_quota_under_finalization():
        layout = json.loads(layout_path.read_text(encoding="utf-8"))
        # The initial source stage is admitted at the normal fixture quota;
        # this concurrent policy revision must count raw + WAV + take.json.
        layout["storage"]["quota_bytes"] = len(samples) * 2
        layout["storage"]["reserve_bytes"] = 0
        layout_path.write_text(json.dumps(layout), encoding="utf-8")

    service.run_import_job(queued["job_id"], before_finalize=lower_quota_under_finalization)
    completed = service.get_job(GetJobRequest(project="fixture", job_id=queued["job_id"]))
    assert completed["state"] == "failed"
    assert completed["error"]["reason"] == "insufficient_storage"
    state = service.discover_state()
    assert state.generation(generation["generation_record_id"])["take_id"] is None
    take_id = state.import_job(queued["job_id"])["payload"]["take_id"]
    take_dir = tmp_path / "Audiobook/Chapters/1/takes" / take_id
    assert not (take_dir / "native.pcm").exists()
    assert not (take_dir / "native.wav").exists()
    assert not (take_dir / "take.json").exists()


def test_pcm_build_budget_refuses_before_assembly(tmp_path, monkeypatch):
    service, prose, tagged = _fixture(tmp_path)
    spec = {"provider": "synthetic", "route": "fixture", "model_id": "model",
            "voice_id": "voice", "parameters": {}, "context_fields": {}}
    prepared = _prepare_test_plan(service, "budget-build-prepare", prose, tagged, None, [
        {"chunk_id": "budget", "start": 0, "end": 5, "request_spec": spec},
    ])
    take = _import_native_take(service, prepared, "budget", operation_prefix="budget-build-take")
    build_request = BuildRequest.model_validate({
        "project": "fixture", "operation_id": "budget-build", "expected_head_revision": None,
        "input": {"kind": "chapter", "chapter_id": "ch1", "snapshot_id": prepared["snapshot_id"],
                  "expected_manifest_revision": prepared["manifest_revision"],
                  "request_plan_sha256": prepared["request_plan_sha256"],
                  "takes": [{"chunk_id": "budget", "take_id": take["take_id"],
                             "request_sha256": take["request_sha256"]}]},
        "mode": "production_pcm", "outputs": {"master": True}, "gaps": [],
        "metadata": {"title": "Fixture", "author": "Fixture", "edition": "test"},
    })
    queued, _ = service.build(build_request, owner_key="principal:fixture")
    layout_path = tmp_path / "Project Files/Book_Layout.json"
    layout = json.loads(layout_path.read_text(encoding="utf-8"))
    layout["storage"]["quota_bytes"] = 1
    layout["storage"]["reserve_bytes"] = 0
    layout_path.write_text(json.dumps(layout), encoding="utf-8")

    def assembly_must_not_run(*_args, **_kwargs):
        raise AssertionError("assembly ran despite preflight quota refusal")

    monkeypatch.setattr(service_module, "assemble_pcm_stream", assembly_must_not_run)
    service.run_build_job(queued["job_id"])
    result = service.get_job(GetJobRequest(project="fixture", job_id=queued["job_id"]))
    assert result["state"] == "failed"
    assert result["error"]["reason"] == "insufficient_storage"
    builds = tmp_path / "Audiobook/Chapters/1/builds"
    assert not builds.exists() or list(builds.iterdir()) == []


def test_pcm_build_budget_peak_counts_same_filesystem_rename_once(tmp_path, monkeypatch):
    service, prose, tagged = _fixture(tmp_path)
    spec = {"provider": "synthetic", "route": "fixture", "model_id": "model",
            "voice_id": "voice", "parameters": {}, "context_fields": {}}
    prepared = _prepare_test_plan(service, "one-pcm-prepare", prose, tagged, None, [
        {"chunk_id": "one-pcm", "start": 0, "end": 5, "request_spec": spec},
    ])
    take = _import_native_take(service, prepared, "one-pcm", operation_prefix="one-pcm-take")
    queued, _ = service.build(BuildRequest.model_validate({
        "project": "fixture", "operation_id": "one-pcm-build", "expected_head_revision": None,
        "input": {"kind": "chapter", "chapter_id": "ch1", "snapshot_id": prepared["snapshot_id"],
                  "expected_manifest_revision": prepared["manifest_revision"],
                  "request_plan_sha256": prepared["request_plan_sha256"],
                  "takes": [{"chunk_id": "one-pcm", "take_id": take["take_id"],
                             "request_sha256": take["request_sha256"]}]},
        "mode": "production_pcm", "outputs": {"master": True}, "gaps": [],
        "metadata": {"title": "Fixture", "author": "Fixture", "edition": "test"},
    }), owner_key="principal:fixture")
    # Four mono 16-bit frames plus the fixed immutable-facts allowance.  This
    # fits one PCM master but would fail the former two-master estimate.
    _, _, layout = service._enabled_layout()
    monkeypatch.setattr(service, "_media_free_bytes", lambda _layout: layout.storage.reserve_bytes + 1_048_576 + 8)
    service.run_build_job(queued["job_id"])
    completed = service.get_job(GetJobRequest(project="fixture", job_id=queued["job_id"]))
    assert completed["state"] == "succeeded", completed
    assert completed["result"]["outputs"][0]["size_bytes"] == 8


def test_pcm_build_final_budget_recheck_cleans_unregistered_outputs(tmp_path):
    service, prose, tagged = _fixture(tmp_path)
    spec = {"provider": "synthetic", "route": "fixture", "model_id": "model",
            "voice_id": "voice", "parameters": {}, "context_fields": {}}
    prepared = _prepare_test_plan(service, "final-build-prepare", prose, tagged, None, [
        {"chunk_id": "final", "start": 0, "end": 5, "request_spec": spec},
    ])
    take = _import_native_take(service, prepared, "final", operation_prefix="final-build-take")
    queued, _ = service.build(BuildRequest.model_validate({
        "project": "fixture", "operation_id": "final-build", "expected_head_revision": None,
        "input": {"kind": "chapter", "chapter_id": "ch1", "snapshot_id": prepared["snapshot_id"],
                  "expected_manifest_revision": prepared["manifest_revision"],
                  "request_plan_sha256": prepared["request_plan_sha256"],
                  "takes": [{"chunk_id": "final", "take_id": take["take_id"],
                             "request_sha256": take["request_sha256"]}]},
        "mode": "production_pcm", "outputs": {"master": True}, "gaps": [],
        "metadata": {"title": "Fixture", "author": "Fixture", "edition": "test"},
    }), owner_key="principal:fixture")
    layout_path = tmp_path / "Project Files/Book_Layout.json"
    callbacks: list[str] = []

    def lower_quota_under_finalization():
        callbacks.append("acquired")
        layout = json.loads(layout_path.read_text(encoding="utf-8"))
        layout["storage"]["quota_bytes"] = 1
        layout["storage"]["reserve_bytes"] = 0
        layout_path.write_text(json.dumps(layout), encoding="utf-8")

    service.run_build_job(
        queued["job_id"], before_finalize=lower_quota_under_finalization,
        after_finalize=lambda: callbacks.append("released"),
    )
    result = service.get_job(GetJobRequest(project="fixture", job_id=queued["job_id"]))
    assert result["state"] == "failed"
    assert result["error"]["reason"] == "insufficient_storage"
    builds = tmp_path / "Audiobook/Chapters/1/builds"
    assert not builds.exists() or list(builds.iterdir()) == []
    assert callbacks == ["acquired", "released"]


def test_owned_build_cleanup_preserves_unexpected_files_and_refuses_replaced_directory(tmp_path, caplog):
    build_dir = tmp_path / "Audiobook/Chapters/1/builds/owned"
    build_dir.mkdir(parents=True)
    known = build_dir / "master.pcm"
    unexpected = build_dir / "operator-note.txt"
    known.write_bytes(b"known")
    unexpected.write_bytes(b"preserve")
    identity = service_module._owned_directory_identity(build_dir)
    with caplog.at_level("ERROR", logger="cognita.books"):
        service_module._cleanup_owned_build_files(
            tmp_path, build_dir, identity, ("master.pcm",),
        )
    assert not known.exists()
    assert unexpected.read_bytes() == b"preserve"
    assert "Owned build cleanup incomplete" in caplog.text
    assert str(unexpected) in caplog.text

    unexpected.unlink()
    build_dir.rmdir()
    build_dir.mkdir()
    replacement = build_dir / "master.pcm"
    replacement.write_bytes(b"replacement")
    with caplog.at_level("ERROR", logger="cognita.books"):
        service_module._cleanup_owned_build_files(
            tmp_path, build_dir, identity, ("master.pcm",),
        )
    assert replacement.read_bytes() == b"replacement"
    assert "Owned build cleanup refused" in caplog.text


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


def test_authorized_alternate_pair_drives_test_prepare_import_build_and_commit(tmp_path):
    service, production_prose, production_tagged = _fixture(tmp_path)
    alternate_prose, alternate_tagged = _authorized_alternate_pair(tmp_path)
    prepared = _prepare_alternate_test_plan(
        service, alternate_prose, alternate_tagged, operation_id="alternate-prepare",
    )
    assert prepared["working_tagged_filepath"] == "Chapters/1/Test/chapter_audio-tags.docx"
    state = ProjectState.discover(tmp_path)
    assert state is not None
    snapshot = state.snapshot(prepared["snapshot_id"])
    assert snapshot is not None
    assert snapshot["payload"]["working_prose_filepath"] == "Chapters/1/Test/chapter.docx"
    assert snapshot["payload"]["working_tagged_filepath"] == "Chapters/1/Test/chapter_audio-tags.docx"

    chapter = service.get_chapter(GetChapterRequest.model_validate({
        "project": "fixture", "chapter_id": "ch1",
        "scope": {"kind": "test", "authorization_id": "alternate-auth"},
        "snapshot_id": prepared["snapshot_id"],
    }))
    assert chapter["source_status"] == "eligible"
    take = _import_native_take(service, prepared, "alternate", operation_prefix="alternate-take")
    build_request = BuildRequest.model_validate({
        "project": "fixture", "operation_id": "alternate-build", "expected_head_revision": None,
        "input": {"kind": "chapter", "chapter_id": "ch1", "snapshot_id": prepared["snapshot_id"],
                  "expected_manifest_revision": prepared["manifest_revision"],
                  "request_plan_sha256": prepared["request_plan_sha256"],
                  "takes": [{"chunk_id": "alternate", "take_id": take["take_id"],
                             "request_sha256": take["request_sha256"]}]},
        "mode": "production_pcm", "outputs": {"master": True}, "gaps": [],
        "metadata": {"title": "Fixture", "author": "Fixture", "edition": "test"},
    })
    queued, replayed = service.build(build_request, owner_key="principal:fixture")
    assert not replayed
    service.run_build_job(queued["job_id"])
    candidate = service.get_job(GetJobRequest(project="fixture", job_id=queued["job_id"]))
    assert candidate["state"] == "succeeded", candidate
    committed, _ = service.commit_build(CommitBuildRequest.model_validate({
        "project": "fixture", "operation_id": "alternate-commit",
        "build_id": candidate["result"]["build_id"], "expected_head_revision": None,
        "intent": "accept_candidate", "acceptance": {
            "actor": "fixture", "accepted_at": datetime.now(timezone.utc).isoformat(),
            "listening_review": "passed", "notes": ["alternate pair"],
        },
    }), owner_key="principal:fixture")
    assert committed["head_revision"] == 1
    assert (tmp_path / "Chapters/1/chapter.docx").read_bytes() == production_prose
    assert (tmp_path / "Chapters/1/chapter_audio-tags.docx").read_bytes() == production_tagged


def test_alternate_bookmark_publication_targets_only_authorized_tagged_copy(tmp_path):
    service, _production_prose, production_tagged = _fixture(tmp_path)
    alternate_prose, alternate_tagged = _authorized_alternate_pair(tmp_path)
    prepared = _prepare_alternate_test_plan(
        service, alternate_prose, alternate_tagged,
        operation_id="alternate-bookmark-prepare", publish=True,
    )
    assert prepared["working_tagged_updated"] is True
    assert (tmp_path / "Chapters/1/chapter_audio-tags.docx").read_bytes() == production_tagged
    assert (tmp_path / "Chapters/1/Test/chapter_audio-tags.docx").read_bytes() != alternate_tagged


@pytest.mark.parametrize("change", ["revoked", "expired", "not_yet_active", "hash", "ordinals"])
def test_alternate_pair_authorization_denials_are_checked_before_prepare(tmp_path, change):
    service, _prose, _tagged = _fixture(tmp_path)
    alternate_prose, alternate_tagged = _authorized_alternate_pair(tmp_path)
    layout_path = tmp_path / "Project Files/Book_Layout.json"
    layout = json.loads(layout_path.read_text(encoding="utf-8"))
    authorization = next(item for item in layout["test_authorizations"] if item["authorization_id"] == "alternate-auth")
    now = datetime.now(timezone.utc)
    if change == "revoked":
        authorization["revoked"] = True
    elif change == "expired":
        authorization["expires_at"] = (now - timedelta(seconds=1)).isoformat()
    elif change == "not_yet_active":
        authorization["authorized_at"] = (now + timedelta(minutes=1)).isoformat()
    elif change == "hash":
        authorization["source_raw_sha256"] = "0" * 64
    else:
        authorization["allowed_paragraph_ordinals"] = [1]
    layout_path.write_text(json.dumps(layout), encoding="utf-8")
    with pytest.raises(BookServiceError) as denied:
        _prepare_alternate_test_plan(service, alternate_prose, alternate_tagged, operation_id=f"alternate-denied-{change}")
    assert denied.value.reason == "test_scope_not_authorized"


def test_alternate_cursor_and_historical_snapshot_keep_distinct_authorization_boundaries(tmp_path):
    service, _prose, _tagged = _fixture(tmp_path)
    alternate_prose, alternate_tagged = _authorized_alternate_pair(tmp_path)
    initial = service.inspect(InspectRequest.model_validate({
        "project": "fixture", "chapter_id": "ch1",
        "prose_filepath": "Chapters/1/Test/chapter.docx",
        "tagged_filepath": "Chapters/1/Test/chapter_audio-tags.docx",
        "max_characters": 1,
    }))
    assert initial["next_cursor"]
    prepared = _prepare_alternate_test_plan(
        service, alternate_prose, alternate_tagged, operation_id="alternate-historical-prepare",
    )
    # Current eligibility observes a changed alternate source, but frozen history
    # stays readable while its authorization still binds the captured raw bytes.
    changed = _docx("alternate prose changed")
    (tmp_path / "Chapters/1/Test/chapter.docx").write_bytes(changed)
    chapter = service.get_chapter(GetChapterRequest.model_validate({
        "project": "fixture", "chapter_id": "ch1",
        "scope": {"kind": "test", "authorization_id": "alternate-auth"},
        "snapshot_id": prepared["snapshot_id"],
    }))
    assert chapter["source_status"] == "blocked"
    layout_path = tmp_path / "Project Files/Book_Layout.json"
    layout = json.loads(layout_path.read_text(encoding="utf-8"))
    authorization = next(item for item in layout["test_authorizations"] if item["authorization_id"] == "alternate-auth")
    authorization["revoked"] = True
    layout_path.write_text(json.dumps(layout), encoding="utf-8")
    with pytest.raises(BookServiceError) as cursor_denied:
        service.inspect(InspectRequest.model_validate({
            "project": "fixture", "chapter_id": "ch1",
            "prose_filepath": "Chapters/1/Test/chapter.docx",
            "tagged_filepath": "Chapters/1/Test/chapter_audio-tags.docx",
            "cursor": initial["next_cursor"], "max_characters": 1,
        }))
    assert cursor_denied.value.reason == "test_scope_not_authorized"
    with pytest.raises(BookServiceError) as historical_denied:
        service.get_chapter(GetChapterRequest.model_validate({
            "project": "fixture", "chapter_id": "ch1",
            "scope": {"kind": "test", "authorization_id": "alternate-auth"},
            "snapshot_id": prepared["snapshot_id"],
        }))
    assert historical_denied.value.reason == "not_authorized"


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
    build_request = BuildRequest.model_validate({
        "project": "fixture", "operation_id": "test-mp3-build", "expected_head_revision": None,
        "input": {"kind": "chapter", "chapter_id": "ch1", "snapshot_id": take["snapshot_id"],
                  "expected_manifest_revision": snapshot["manifest_revision"], "request_plan_sha256": snapshot["payload"]["result"]["request_plan_sha256"],
                  "takes": [{"chunk_id": take["chunk_id"], "take_id": take["take_id"], "request_sha256": take["request_sha256"]}]},
        "mode": "test_mp3_stream_copy", "outputs": {"master": False}, "gaps": [],
        "metadata": {"title": "Fixture", "author": "Fixture", "edition": "test"},
    })
    build, replayed = service.build(build_request, owner_key="principal:fixture")
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
    assert service.build(build_request, owner_key="principal:fixture") == (build, True)
    changed_spec = json.loads(json.dumps(snapshot["payload"]["result"]["chunks"][0]["request_spec"]))
    changed_spec["context_fields"]["previous_text"] = "new plan context"
    changed_plan = _prepare_test_plan(
        service, "mp3-replay-changed-plan", prose, tagged, snapshot["manifest_revision"],
        [{"chunk_id": take["chunk_id"], "start": 0, "end": 5, "request_spec": changed_spec}],
    )
    assert changed_plan["request_plan_sha256"] != build_request.input.request_plan_sha256
    _assert_completed_build_replay_survives_changed_guards(service, build_request, build)

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
    factual_files = {path: path.read_bytes() for path in (tmp_path / "Audiobook").rglob("*") if path.is_file()}
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
    assert {path: path.read_bytes() for path in factual_files} == factual_files
    events = _acceptance_events(BookService(tmp_path, "fixture")._state_required())
    assert len(events) == 2
    assert [json.loads(event["payload_json"])["intent"] for event in events] == ["accept_candidate", "rollback"]
    assert all(json.loads(event["payload_json"])["namespace"] == {"kind": "test", "authorization_id": "test-auth"}
               for event in events)
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
    assert located["matches"][0]["current_take_ids"] == [take["take_id"]]
    test_quote = service.find_chunk(FindChunkRequest.model_validate({
        "project": "fixture", "chapter_id": "ch1", "query": {
            "kind": "quote", "text": "hello", "snapshot_id": snapshot_id,
        },
    }))["matches"][0]
    assert test_quote["matched_take_ids"] == test_quote["current_take_ids"] == [take["take_id"]]
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
    changed_spec = json.loads(json.dumps(snapshot["payload"]["result"]["chunks"][0]["request_spec"]))
    changed_spec["context_fields"]["previous_text"] = "new plan context"
    changed_plan = _prepare_test_plan(
        service, "pcm-replay-changed-plan", prose, tagged, snapshot["manifest_revision"],
        [{"chunk_id": take["chunk_id"], "start": 0, "end": 5, "request_spec": changed_spec}],
    )
    assert changed_plan["request_plan_sha256"] != build_request.input.request_plan_sha256
    _assert_completed_build_replay_survives_changed_guards(service, build_request, queued)

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
