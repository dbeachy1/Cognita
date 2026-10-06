"""Offline M1 walkthrough: inspect, prepare, recover, and find synthetic text.

Run from the repository root with the service environment:
    $env:PYTHONPATH = "src"
    python examples/audiobook/synthetic_m1_walkthrough.py

The fixture is created under a temporary directory and removed on exit. This
script never contacts a TTS provider or an MCP server.
"""

from __future__ import annotations

import gc
import hashlib
import json
import tempfile
import wave
from datetime import datetime, timedelta, timezone
from pathlib import Path

from docx import Document

from cognita.books.models import FindChunkRequest, GetChapterRequest, InspectRequest, PrepareRequest
from cognita.books.media import inspect_media_file
from cognita.books.projection import project_docx_pair
from cognita.books.service import BookService


def write_docx(path: Path, text: str) -> bytes:
    document = Document()
    document.add_paragraph(text)
    document.save(path)
    return path.read_bytes()


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="cognita-book-example-") as temporary:
        root = Path(temporary)
        chapter_dir = root / "Chapters" / "1"
        source_dir = root / "Project Files" / "Source"
        chapter_dir.mkdir(parents=True)
        source_dir.mkdir(parents=True)

        prose_text = "The copper door opened [at noon]."
        tag_text = " softly"
        tagged_text = "The copper door opened softly [at noon]."
        prose = write_docx(source_dir / "source.docx", prose_text)
        (chapter_dir / "chapter.docx").write_bytes(prose)
        tagged = write_docx(chapter_dir / "tagged.docx", tagged_text)
        audio_path = root / "synthetic.wav"
        with wave.open(str(audio_path), "wb") as audio:
            audio.setnchannels(1)
            audio.setsampwidth(2)
            audio.setframerate(44_100)
            # Deterministic, quiet synthetic sample fixture; no speech content.
            audio.writeframes(b"\x00\x00" * 441)
        audio_facts = inspect_media_file(audio_path)
        assert audio_facts.media.frame_count == "441"
        assert audio_facts.media.codec == "pcm_s16le"
        # The square-bracketed words are part of the synthetic prose. Only the
        # caller-supplied span marks " softly" as an added spoken direction.
        tag_start = tagged_text.index(tag_text)
        chapter_state = {
            "schema_version": 1, "chapter_id": "ch1", "layout_revision": 1,
            "state_revision": 1, "editorial_status": "draft",
            "approval_binding": "prose_projection", "approved_source_raw_sha256": None,
            "approved_prose_projection_sha256": None, "approval_projection_version": None,
            "approval_provenance": None, "summary": None, "index_annotations": None,
        }
        (chapter_dir / "chapter.json").write_text(json.dumps(chapter_state), encoding="utf-8")
        now = datetime.now(timezone.utc)
        authorization = {
            "authorization_id": "synthetic-test-auth", "chapter_id": "ch1",
            "prose_filepath": "Chapters/1/chapter.docx",
            "tagged_filepath": "Chapters/1/tagged.docx",
            "allowed_paragraph_ordinals": [0],
            "source_raw_sha256": hashlib.sha256(prose).hexdigest(),
            "actor": "synthetic-example", "authorized_at": (now - timedelta(minutes=1)).isoformat(),
            "expires_at": (now + timedelta(hours=1)).isoformat(), "revoked": False,
        }
        layout = {
            "schema_version": 1, "layout_revision": 1, "book_id": "synthetic-book",
            "title": "Synthetic example", "chapter_order": ["ch1"],
            "chapters": [{
                "chapter_id": "ch1", "title": "Synthetic chapter",
                "chapter_state_filepath": "Chapters/1/chapter.json",
                "working_filepath": "Chapters/1/chapter.docx",
                "tagged_filepath": "Chapters/1/tagged.docx",
                "summary_filepath": None, "originals_root": "Chapters/1/Originals",
                "audio_root": "Audiobook/Chapters/1",
            }],
            "indexed_references": [], "indexed_instructions": [], "indexed_workflow_documents": [],
            "index_policy": {
                "default_unknown": "exclude", "tagged_copies": "exclude", "archives": "exclude",
                "media": "exclude", "duplicate_prose": "single_active_source",
            },
            "source_master_filepath": "Project Files/Source/source.docx",
            "shared_paths": {"production_settings_filepath": "Project Files/production-settings.json",
                              "book_audio_root": "Audiobook"},
            "storage": {"quota_bytes": 2_000_000_000, "reserve_bytes": 100_000_000,
                        "import_https_hosts": []},
            "production_authorization": None, "test_authorizations": [authorization],
        }
        (root / "Project Files" / "Book_Layout.json").write_text(json.dumps(layout), encoding="utf-8")

        settings = {
            "schema_version": 1, "target_codepoints": len(tagged_text) + 10,
            "request_limit": {"value": len(tagged_text) + 10,
                              "unit": "unicode_codepoints", "evidence": "synthetic fixture"},
            "request_spec": None, "production_target": None, "native_format_evidence": None,
        }
        settings_bytes = json.dumps(settings, separators=(",", ":")).encode("utf-8")
        settings_path = root / "Project Files" / "production-settings.json"
        settings_path.write_bytes(settings_bytes)
        layout["production_authorization"] = {
            "authorization_id": "synthetic-production-auth", "actor": "synthetic-example",
            "authorized_at": now.isoformat(), "completed_book": True, "revoked": False,
        }
        (root / "Project Files" / "Book_Layout.json").write_text(json.dumps(layout), encoding="utf-8")

        service = BookService(root, "synthetic-book")
        # IDs are opaque and bind both raw DOCX hashes; ask the service for the
        # current ID rather than deriving it in the example.
        preliminary = service.inspect(InspectRequest.model_validate({
            "project": "synthetic-book", "chapter_id": "ch1",
            "prose_filepath": "Chapters/1/chapter.docx",
            "tagged_filepath": "Chapters/1/tagged.docx",
        }, strict=True))
        paragraph_id = preliminary["paragraphs"][0]["paragraph_id"]
        projected = project_docx_pair(prose, tagged, explicit_tag_spans=[{
            "paragraph_id": paragraph_id, "start": tag_start,
            "end": tag_start + len(tag_text),
            "expected_text_sha256": hashlib.sha256(tag_text.encode("utf-8")).hexdigest(),
        }])
        chapter_state.update({
            "editorial_status": "approved",
            "approved_source_raw_sha256": projected.prose_sha256,
            "approved_prose_projection_sha256": projected.prose_projection_sha256,
            "approval_projection_version": projected.projection_version,
            "approval_provenance": {
                "actor": "synthetic-example", "approved_at": now.isoformat(),
                "source_raw_sha256": projected.prose_sha256,
                "prose_projection_sha256": projected.prose_projection_sha256,
                "projection_version": projected.projection_version,
            },
        })
        (chapter_dir / "chapter.json").write_text(json.dumps(chapter_state), encoding="utf-8")
        inspected = service.inspect(InspectRequest.model_validate({
            "project": "synthetic-book", "chapter_id": "ch1",
            "prose_filepath": "Chapters/1/chapter.docx",
            "tagged_filepath": "Chapters/1/tagged.docx",
            "explicit_tag_spans": [{
                "paragraph_id": paragraph_id, "start": tag_start,
                "end": tag_start + len(tag_text),
                "expected_text_sha256": hashlib.sha256(tag_text.encode("utf-8")).hexdigest(),
            }],
        }, strict=True))
        assert inspected["speech_text"] == tagged_text
        assert inspected["source_text_matches_without_tags"] is True
        assert inspected["speech_text_total_codepoints"] == len(tagged_text)

        prepared, replayed = service.prepare(PrepareRequest.model_validate({
            "project": "synthetic-book", "operation_id": "synthetic-prepare-1",
            "chapter_id": "ch1", "document_view_id": inspected["document_view_id"],
            "expected_prose_sha256": hashlib.sha256(prose).hexdigest(),
            "expected_tagged_sha256": hashlib.sha256(tagged).hexdigest(),
            "expected_manifest_revision": None,
            "scope": {"kind": "production"},
            "speech_selection_confirmed": True,
            "request_limit": {"value": len(tagged_text) + 10, "unit": "unicode_codepoints"},
            "expected_settings_sha256": hashlib.sha256(settings_bytes).hexdigest(),
            "production_target": None,
            "chunks": [{"chunk_id": "synthetic-chunk-1", "start": 0,
                        "end": len(tagged_text), "request_spec": None}],
            "publish_bookmarks_to_working_tagged_docx": False,
        }, strict=True), owner_key="principal:synthetic-example")
        assert not replayed

        # A new service instance reads the durable prepared snapshot. Replaying
        # the same successful operation ID and arguments returns its receipt.
        reopened = BookService(root, "synthetic-book")
        prepare_request = PrepareRequest.model_validate({
                "project": "synthetic-book", "operation_id": "synthetic-prepare-1",
                "chapter_id": "ch1", "document_view_id": inspected["document_view_id"],
                "expected_prose_sha256": hashlib.sha256(prose).hexdigest(),
                "expected_tagged_sha256": hashlib.sha256(tagged).hexdigest(),
                "expected_manifest_revision": None,
                "scope": {"kind": "production"},
                "speech_selection_confirmed": True,
                "request_limit": {"value": len(tagged_text) + 10, "unit": "unicode_codepoints"},
                "expected_settings_sha256": hashlib.sha256(settings_bytes).hexdigest(),
                "production_target": None,
                "chunks": [{"chunk_id": "synthetic-chunk-1", "start": 0,
                            "end": len(tagged_text), "request_spec": None}],
                "publish_bookmarks_to_working_tagged_docx": False,
            }, strict=True)
        replay_result, was_replayed = reopened.prepare(
            prepare_request, owner_key="principal:synthetic-example")
        assert was_replayed and replay_result["snapshot_id"] == prepared["snapshot_id"]

        chapter = reopened.get_chapter(GetChapterRequest.model_validate({
            "project": "synthetic-book", "chapter_id": "ch1",
            "scope": {"kind": "production"},
            "include_text": True,
        }, strict=True))
        match = reopened.find_chunk(FindChunkRequest.model_validate({
            "project": "synthetic-book", "chapter_id": "ch1",
            "query": {"kind": "quote", "text": "copper door"},
        }, strict=True))
        assert chapter["snapshot_id"] == prepared["snapshot_id"]
        assert match["matches"][0]["chunk_ids"] == ["synthetic-chunk-1"]
        assert not list(root.glob(".cognita-storage/**/*.mp3"))
        print("Synthetic M1 workflow passed: inspect -> prepare -> replay -> get -> find")
        print("Synthetic WAV facts verified locally; no audio was generated or imported.")
        print("The temporary project and WAV fixture are removed on exit.")
        # ProjectState uses short-lived SQLite connections. Drop both service
        # objects before TemporaryDirectory removes the database on Windows.
        del service, reopened
        gc.collect()


if __name__ == "__main__":
    main()
