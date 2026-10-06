"""Project-scoped, durable book service for the first preparation workflow.

The project files and this module's small SQLite state are the source-side
authority. PostgreSQL is deliberately not consulted here, so inspection,
preparation and project-file reads continue during an index outage.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import stat
import uuid
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from . import models as dto
from .config import (
    BookBinding, BookLayout, validate_chapter_state,
    validate_production_settings,
)
from .configuration import (
    BINDING_PATH, LAYOUT_PATH, STATE_ROOT, BookConfigSnapshot, load_book_config,
)
from .fingerprint import canonical_json_sha256, request_fingerprint
from .media import MediaValidationError, inspect_media_file
from .projection import ProjectionError, project_docx_pair, validate_chunk_ranges
from .state import ProjectState, ProjectStateError
from .storage import ProjectFileError, list_project_files, read_project_file
from ..parsing import compute_doc_id

VIEW_TTL = timedelta(hours=24)
MAX_DOCX_BYTES = 256 * 1024 * 1024


class BookServiceError(ValueError):
    def __init__(self, reason: str, message: str, *, outcome: str = "not_applied"):
        super().__init__(message)
        self.reason = reason
        self.outcome = outcome


def _path(root: Path, relative: str, *, allow_missing: bool = False) -> Path:
    if not isinstance(relative, str) or not relative or "\\" in relative or "\x00" in relative:
        raise BookServiceError("validation_failed", "A normalized project-relative path is required.")
    pure = PurePosixPath(relative)
    if pure.is_absolute() or any(part in {"", ".", ".."} for part in pure.parts):
        raise BookServiceError("permission_denied", "The path must remain inside the project.")
    root = Path(root).resolve(strict=True)
    target = root
    for part in pure.parts:
        target = target / part
        try:
            facts = target.lstat()
        except FileNotFoundError:
            if allow_missing:
                continue
            raise BookServiceError("file_not_found", "A registered project file is missing.")
        except OSError as exc:
            raise BookServiceError("source_unavailable", "A project path could not be inspected safely.") from exc
        if stat.S_ISLNK(facts.st_mode):
            raise BookServiceError("permission_denied", "Project symbolic links are not followed.")
    try:
        target.resolve(strict=False).relative_to(root)
    except ValueError as exc:
        raise BookServiceError("permission_denied", "The path resolves outside the project.") from exc
    return target


def _read_bytes(root: Path, relative: str) -> bytes:
    target = _path(root, relative)
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(target, flags)
        with os.fdopen(fd, "rb") as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_DOCX_BYTES:
                raise BookServiceError("validation_failed", "The DOCX file is not a supported regular file size.")
            content = stream.read(MAX_DOCX_BYTES + 1)
            after = os.fstat(stream.fileno())
    except BookServiceError:
        raise
    except OSError as exc:
        raise BookServiceError("source_unavailable", "The registered source could not be read safely.") from exc
    if len(content) > MAX_DOCX_BYTES or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
        after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns
    ) or len(content) != before.st_size:
        raise BookServiceError("stale_source", "The registered source changed during reading.")
    return content


def _cursor(view_id: str, offset: int) -> str:
    raw = json.dumps({"v": 1, "view": view_id, "offset": offset}, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _cursor_offset(cursor: str, view_id: str) -> int:
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        value = json.loads(raw)
        offset = value["offset"]
        if value != {"v": 1, "view": view_id, "offset": offset} or isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise ValueError
        return offset
    except (ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
        raise BookServiceError("invalid_cursor", "The text cursor is invalid or belongs to another view.") from exc


def _data(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json", exclude_unset=True)
    if hasattr(value, "__dataclass_fields__"):
        return asdict(value)
    if isinstance(value, tuple):
        return [_data(item) for item in value]
    if isinstance(value, list):
        return [_data(item) for item in value]
    if isinstance(value, dict):
        return {key: _data(item) for key, item in value.items()}
    return value


class BookService:
    """Operations for one project. Create with its documents directory."""

    def __init__(self, project_root: Path, project_name: str, *, state: ProjectState | None = None):
        self.root = Path(project_root).resolve(strict=True)
        self.project_name = project_name
        self.state = state
        self._pending_views: dict[str, dict[str, Any]] = {}
        # Runtime ownership only. Durable rows remain the restart authority;
        # an unfinished row absent from this set is never assumed to have a
        # surviving worker.
        self._active_import_jobs: set[str] = set()

    def discover_state(self) -> ProjectState | None:
        if self.state is None:
            self.state = ProjectState.discover(self.root)
        return self.state

    def config(self) -> BookConfigSnapshot:
        return load_book_config(self.root, self.discover_state())

    def _registered_role(self, layout: BookLayout, source_path: str):
        for item in layout.indexed_references:
            if item.filepath == source_path:
                return item.role, None, None
        for item in layout.indexed_instructions:
            if item.filepath == source_path:
                return item.role, None, None
        for item in layout.indexed_workflow_documents:
            if item.filepath == source_path:
                return item.role, None, None
        for chapter in layout.chapters:
            if chapter.working_filepath == source_path:
                return "chapter_working", chapter, "working"
            if chapter.summary_filepath == source_path:
                return "chapter_summary", chapter, "summary"
        return None

    def index_provenance_for(
        self, source_path: str, doc_id: str, extracted_sha256: str,
        raw_sha256: str, extraction_version: str,
    ):
        """Build current role provenance only for a registered, eligible source."""
        from .state import IndexedRoleProvenance
        from .fingerprint import canonical_json_sha256
        from .config import validate_chapter_state
        state = self.discover_state()
        config = load_book_config(self.root, state)
        if state is None or config.config_state != "enabled" or config.layout is None:
            return None
        if compute_doc_id(source_path, extracted_sha256) != doc_id:
            return None
        registered = self._registered_role(config.layout, source_path)
        if registered is None:
            return None
        role, chapter, chapter_kind = registered
        try:
            raw = _read_bytes(self.root, source_path)
        except BookServiceError:
            return None
        if hashlib.sha256(raw).hexdigest() != raw_sha256:
            return None
        values: dict[str, Any] = {
            "source_path": source_path, "doc_id": doc_id,
            "extracted_sha256": extracted_sha256, "raw_sha256": raw_sha256,
            "extraction_version": extraction_version, "role": role,
            "chapter_id": chapter.chapter_id if chapter is not None else None,
            "layout_sha256": config.layout_sha256,
            "chapter_state_sha256": None, "annotations_sha256": None,
            "approval_source_raw_sha256": None,
            "approval_prose_projection_sha256": None,
            "approval_projection_version": None,
            "summary_raw_sha256": None, "summary_source_raw_sha256": None,
            "summary_source_prose_projection_sha256": None,
        }
        if chapter is not None:
            try:
                chapter_state_raw = _read_bytes(self.root, chapter.chapter_state_filepath)
                chapter_state = validate_chapter_state(chapter_state_raw)
                if chapter_state.chapter_id != chapter.chapter_id or chapter_state.layout_revision != config.layout.layout_revision:
                    return None
                values["chapter_state_sha256"] = hashlib.sha256(chapter_state_raw).hexdigest()
                if chapter_state.index_annotations is not None:
                    values["annotations_sha256"] = canonical_json_sha256(
                        chapter_state.index_annotations.model_dump(mode="json", exclude_unset=True)
                    )
                if chapter_kind == "working":
                    tagged = _read_bytes(self.root, chapter.tagged_filepath)
                    pair = project_docx_pair(raw, tagged)
                    approval = chapter_state.approval_provenance
                    approval_is_current = (
                        chapter_state.editorial_status == "approved" and approval is not None
                        and chapter_state.approved_source_raw_sha256 == raw_sha256
                        and approval.source_raw_sha256 == raw_sha256
                        and approval.prose_projection_sha256 == pair.prose_projection_sha256
                        and approval.projection_version == pair.projection_version
                        and chapter_state.approved_prose_projection_sha256 == pair.prose_projection_sha256
                        and chapter_state.approval_projection_version == pair.projection_version
                    )
                    if chapter_state.editorial_status == "approved" and not approval_is_current:
                        return None
                    if approval_is_current:
                        values["approval_source_raw_sha256"] = approval.source_raw_sha256
                        values["approval_prose_projection_sha256"] = approval.prose_projection_sha256
                        values["approval_projection_version"] = approval.projection_version
                else:
                    summary = chapter_state.summary
                    if (summary is None or not summary.approved or summary.filepath != source_path
                            or summary.summary_raw_sha256 != raw_sha256):
                        return None
                    prose = _read_bytes(self.root, chapter.working_filepath)
                    tagged = _read_bytes(self.root, chapter.tagged_filepath)
                    pair = project_docx_pair(prose, tagged)
                    if (summary.source_raw_sha256 != hashlib.sha256(prose).hexdigest()
                            or summary.source_prose_projection_sha256 != pair.prose_projection_sha256):
                        return None
                    values["summary_raw_sha256"] = summary.summary_raw_sha256
                    values["summary_source_raw_sha256"] = summary.source_raw_sha256
                    values["summary_source_prose_projection_sha256"] = summary.source_prose_projection_sha256
            except (OSError, ValueError, ProjectFileError, ProjectionError):
                return None
        return IndexedRoleProvenance(**values)

    def index_provenance_is_current(self, record) -> bool:
        current = self.index_provenance_for(
            record.source_path, record.doc_id, record.extracted_sha256,
            record.raw_sha256, record.extraction_version,
        )
        return current == record

    def record_index_provenance(
        self, source_path: str, doc_id: str, extracted_sha256: str,
        raw_sha256: str, extraction_version: str,
    ):
        """Persist derived index evidence only while its source bindings remain current."""
        state = self.discover_state()
        if state is None:
            return None
        record = self.index_provenance_for(
            source_path, doc_id, extracted_sha256, raw_sha256, extraction_version,
        )
        if record is None:
            return None
        try:
            state.put_indexed_role_provenance(record)
        except ProjectStateError as exc:
            raise BookServiceError("state_unavailable", "Index provenance could not be persisted.") from exc
        return record if self.index_provenance_is_current(record) else None

    def index_admitted_doc_ids(
        self, sources, retrieval_profile: str | None = None,
    ) -> dict[str, dict[str, Any]]:
        state = self.discover_state()
        config = load_book_config(self.root, state)
        if state is None or config.config_state != "enabled" or config.layout is None:
            return {}
        profile = retrieval_profile or "canon"
        if profile not in {"editing", "canon", "instructions", "workflow"}:
            return {}
        admitted: dict[str, dict[str, Any]] = {}
        for source in sources:
            source_path = source.source
            registered = self._registered_role(config.layout, source_path)
            if registered is None:
                continue
            record = state.indexed_role_provenance(source_path)
            if (record is None or record.doc_id != source.doc_id
                    or record.extracted_sha256 != source.content_hash
                    or not self.index_provenance_is_current(record)):
                continue
            role, chapter, chapter_kind = registered
            if chapter is None and source_path in {
                item.filepath for item in config.layout.indexed_instructions
            }:
                allowed = profile == "instructions"
            elif chapter is None and source_path in {
                item.filepath for item in config.layout.indexed_workflow_documents
            }:
                allowed = profile == "workflow"
            elif chapter is None:
                allowed = profile in {"editing", "canon"}
            elif chapter_kind == "working":
                allowed = profile == "editing" or (
                    record.approval_source_raw_sha256 is not None
                    and record.approval_prose_projection_sha256 is not None
                    and record.approval_projection_version is not None
                )
            elif chapter_kind == "summary":
                # index_provenance_for only returns summaries whose approved
                # source/projection binding remains current.
                allowed = True
            else:
                allowed = profile in {"editing", "canon"}
            if allowed:
                editorial_status = None
                summary_freshness = "not_applicable"
                if chapter is not None:
                    # A concurrently edited or malformed chapter-state file must
                    # not turn a read-only search into an index-authority error.
                    # The persisted provenance no longer proves the returned hit
                    # is current, so fail closed for that source instead.
                    try:
                        chapter_state = validate_chapter_state(
                            _read_bytes(self.root, chapter.chapter_state_filepath)
                        )
                    except (BookServiceError, ValueError, ProjectionError):
                        continue
                    editorial_status = chapter_state.editorial_status
                    if chapter_kind == "summary":
                        summary_freshness = "fresh"
                admitted[record.doc_id] = {
                    "source_path": record.source_path,
                    "role": record.role,
                    "chapter_id": record.chapter_id,
                    "editorial_status": editorial_status,
                    "summary_freshness": summary_freshness,
                    "provenance": record,
                }
        return admitted

    def begin_managed_write(
        self, project: Any, path: str, bytes_sha256: str,
        operation_id: str | None = None,
    ) -> str | None:
        """Record post-publication derived-index work for a registered source.

        This runs only after the generic writer has durably published source
        bytes. Unregistered paths retain the legacy write/index behavior.
        """
        from .storage import normalize_project_path
        normalized = normalize_project_path(path)
        state, config, layout = self._enabled_layout()
        if self._registered_role(layout, normalized) is None:
            return None
        if (not isinstance(bytes_sha256, str) or len(bytes_sha256) != 64
                or any(char not in "0123456789abcdef" for char in bytes_sha256)):
            raise BookServiceError("validation_failed", "A lowercase SHA-256 is required for the published bytes.")
        if operation_id is not None and (not isinstance(operation_id, str) or not operation_id):
            raise BookServiceError("validation_failed", "A non-empty operation ID is required when supplied.")
        job_id = str(uuid.uuid4())
        try:
            _disposition, saved_job_id = state.begin_managed_write(
                job_id=job_id, source_path=normalized, bytes_sha256=bytes_sha256,
                operation_id=operation_id,
            )
        except ProjectStateError as exc:
            if "operation_conflict" in str(exc):
                raise BookServiceError("operation_id_conflict", "This operation ID was used for different published bytes.") from exc
            raise BookServiceError("state_unavailable", "Managed indexing status could not be recorded.") from exc
        return saved_job_id

    def finish_managed_write(
        self, project: Any, job_id: str, state: str,
        error: dict | None = None, doc_id: str | None = None,
        extracted_sha256: str | None = None,
    ) -> dict[str, Any]:
        if state not in {"pending", "indexed", "stale", "excluded", "blocked", "failed"}:
            raise BookServiceError("validation_failed", "The indexing status is not supported.")
        if error is not None:
            if (not isinstance(error, dict) or set(error) != {"code", "message"}
                    or not all(isinstance(error[key], str) for key in ("code", "message"))):
                raise BookServiceError("validation_failed", "The indexing failure must use the public error shape.")
            error = {"code": error["code"][:128], "message": error["message"][:512]}
        try:
            return self._state_required().finish_managed_write(
                job_id=job_id, state=state, error=error, doc_id=doc_id,
                extracted_sha256=extracted_sha256,
            )
        except ProjectStateError as exc:
            raise BookServiceError("state_unavailable", "Managed indexing status could not be updated.") from exc

    def get_managed_write_status(self, project: Any, path: str) -> dict[str, Any] | None:
        from .storage import normalize_project_path
        normalized = normalize_project_path(path)
        state = self.discover_state()
        if state is None:
            return None
        try:
            return state.managed_write_status(normalized)
        except ProjectStateError as exc:
            raise BookServiceError("state_unavailable", "Managed indexing status could not be read.") from exc

    def _state_required(self) -> ProjectState:
        state = self.discover_state()
        if state is None:
            raise BookServiceError("configuration_conflict", "Project state is not initialized.")
        return state

    def _enabled_layout(self) -> tuple[ProjectState, BookConfigSnapshot, BookLayout]:
        state = self.discover_state()
        config = load_book_config(self.root, state)
        if state is None or config.config_state != "enabled" or config.layout is None:
            raise BookServiceError("configuration_conflict", "Book configuration is not enabled and valid.")
        return state, config, config.layout

    def _chapter(self, layout: BookLayout, chapter_id: str):
        for chapter in layout.chapters:
            if chapter.chapter_id == chapter_id:
                return chapter
        raise BookServiceError("chapter_not_found", "The requested chapter is not registered.")

    def _first_original_path(self, chapter) -> str:
        return f"{chapter.originals_root}/{chapter.chapter_id}/first-original.docx"

    def original_filepath_for(self, project: Any, chapter_id: str) -> str:
        _state, _config, layout = self._enabled_layout()
        return self._first_original_path(self._chapter(layout, chapter_id))

    def original_exists(self, project: Any, chapter_id: str) -> bool:
        try:
            relative = self.original_filepath_for(project, chapter_id)
            target = _path(self.root, relative)
            return target.is_file() and not target.is_symlink()
        except (BookServiceError, OSError):
            return False

    def ensure_original(
        self, project: Any, chapter_id: str, source_bytes: bytes,
        expected_source_sha256: str,
    ) -> dict[str, str]:
        """Publish the immutable first working DOCX before a generic write.

        The deterministic create-only filename and content digest make a retry
        after a process restart recognizable. The source and destination are
        both rechecked; this function never replaces an existing original.
        The generic write caller must already hold the project write lock.
        """
        state, _config, layout = self._enabled_layout()
        chapter = self._chapter(layout, chapter_id)
        if hashlib.sha256(source_bytes).hexdigest() != expected_source_sha256:
            raise BookServiceError("stale_file", "The source bytes do not match their expected hash.")
        current = _read_bytes(self.root, chapter.working_filepath)
        if hashlib.sha256(current).hexdigest() != expected_source_sha256 or current != source_bytes:
            raise BookServiceError("stale_file", "The registered working source changed before preservation.")
        relative = self._first_original_path(chapter)
        target = _path(self.root, relative, allow_missing=True)
        _mkdir_safe(self.root, PurePosixPath(relative).parent.as_posix())
        digest = expected_source_sha256
        journal_id = f"first-original:{chapter_id}"
        journal_payload = json.dumps(
            {"filepath": relative, "bytes_sha256": digest, "size_bytes": len(source_bytes)},
            sort_keys=True, separators=(",", ":"),
        )
        with state.transaction() as connection:
            row = connection.execute(
                "SELECT kind,phase,payload_json FROM publication_journal WHERE journal_id=?",
                (journal_id,),
            ).fetchone()
            if row is None:
                connection.execute(
                    "INSERT INTO publication_journal(journal_id,kind,phase,payload_json) VALUES(?,?,?,?)",
                    (journal_id, "first_original", "publishing", journal_payload),
                )
            elif row["kind"] != "first_original" or row["payload_json"] != journal_payload:
                # Existing journal evidence may reference the earlier source
                # version. It is deliberately immutable and cannot be rebound.
                prior_target = self.root / json.loads(row["payload_json"])["filepath"]
                if prior_target.is_file() and hashlib.sha256(prior_target.read_bytes()).hexdigest() == json.loads(row["payload_json"])["bytes_sha256"]:
                    return {"filepath": json.loads(row["payload_json"])["filepath"],
                            "bytes_sha256": json.loads(row["payload_json"])["bytes_sha256"]}
                raise BookServiceError("publication_conflict", "Existing first-original evidence is inconsistent.")
        if target.exists():
            existing = _read_bytes(self.root, relative)
            if hashlib.sha256(existing).hexdigest() != digest or existing != source_bytes:
                raise BookServiceError("publication_conflict", "The immutable first-original path contains different bytes.")
        else:
            temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
            try:
                fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                with os.fdopen(fd, "wb") as stream:
                    stream.write(source_bytes)
                    stream.flush()
                    os.fsync(stream.fileno())
                # Hard-link gives create-only publication without exposing a
                # partially written destination or replacing raced evidence.
                os.link(temporary, target)
            except FileExistsError:
                existing = _read_bytes(self.root, relative)
                if hashlib.sha256(existing).hexdigest() != digest:
                    raise BookServiceError("publication_conflict", "First-original publication raced different bytes.")
            except OSError as exc:
                raise BookServiceError("publication_failed", "The first-original copy could not be published.", outcome="outcome_unknown") from exc
            finally:
                temporary.unlink(missing_ok=True)
        with state.transaction() as connection:
            connection.execute(
                "UPDATE publication_journal SET phase='published',updated_at=CURRENT_TIMESTAMP WHERE journal_id=?",
                (journal_id,),
            )
        return {"filepath": relative, "bytes_sha256": digest}

    def inspect(self, request: dto.InspectRequest) -> dict[str, Any]:
        state = self.discover_state()
        config = load_book_config(self.root, state)
        # A valid unbound layout is inspectable so the first explicit prepare
        # can complete the create-only binding bootstrap. Conflicting or
        # malformed documents remain fail-closed.
        if config.config_state not in {"enabled", "bootstrap_pending"} or config.layout is None:
            raise BookServiceError("configuration_conflict", "Book configuration is not valid for inspection.")
        chapter = self._chapter(config.layout, request.chapter_id)
        if (request.prose_filepath, request.tagged_filepath) != (chapter.working_filepath, chapter.tagged_filepath):
            raise BookServiceError("permission_denied", "Inspection paths must match the registered chapter sources.")
        prose_bytes = _read_bytes(self.root, chapter.working_filepath)
        tagged_bytes = _read_bytes(self.root, chapter.tagged_filepath)
        projected = project_docx_pair(
            prose_bytes, tagged_bytes,
            speech_paragraph_ids=(list(request.speech_paragraph_ids) if "speech_paragraph_ids" in request.model_fields_set else None),
            excluded_paragraphs=(list(request.excluded_paragraphs) if "excluded_paragraphs" in request.model_fields_set else ()),
            explicit_tag_spans=(list(request.explicit_tag_spans) if "explicit_tag_spans" in request.model_fields_set else ()),
        )
        view_id = projected.document_view_id
        page_size = request.max_characters if "max_characters" in request.model_fields_set else 40000
        offset = _cursor_offset(request.cursor, view_id) if "cursor" in request.model_fields_set else 0
        if offset > len(projected.speech_text):
            raise BookServiceError("invalid_cursor", "The text cursor is outside the pinned view.")
        end = min(len(projected.speech_text), offset + page_size)
        from .projection import PROJECTION_VERSION
        view_payload = {
            "projection": _data(projected),
            "prose_filepath": chapter.working_filepath,
            "tagged_filepath": chapter.tagged_filepath,
            "chapter_id": chapter.chapter_id,
            "layout_revision": config.layout.layout_revision,
            "speech_paragraph_ids": list(projected.paragraph_ids) if "speech_paragraph_ids" not in request.model_fields_set else list(request.speech_paragraph_ids),
            "explicit_tag_spans": _data(request.explicit_tag_spans) if "explicit_tag_spans" in request.model_fields_set else [],
            "excluded_paragraphs": _data(request.excluded_paragraphs) if "excluded_paragraphs" in request.model_fields_set else [],
        }
        if state is not None:
            try:
                state.save_view(
                    view_id=view_id, chapter_id=request.chapter_id,
                    scope_json="{}", payload=view_payload,
                    expires_at=(datetime.now(timezone.utc) + VIEW_TTL).isoformat(),
                )
            except ProjectStateError as exc:
                raise BookServiceError("state_unavailable", "Pinned project state is unavailable.") from exc
        else:
            # Read-only inspection of a pristine bootstrap-pending layout must
            # not create the persistent state tree. Keep its short-lived view
            # only in this service process; callers can re-inspect after restart.
            self._pending_views[view_id] = {
                "chapter_id": request.chapter_id,
                "scope_json": "{}", "payload": view_payload,
                "expires_at": (datetime.now(timezone.utc) + VIEW_TTL).isoformat(),
            }
        paragraph_results = []
        for paragraph in projected.paragraphs:
            local_start = local_end = 0
            if paragraph.speech_start is not None:
                local_start = max(0, offset - paragraph.speech_start)
                local_end = min(len(paragraph.text), end - paragraph.speech_start)
                if local_end <= local_start:
                    local_start = local_end = min(len(paragraph.text), max(0, local_start))
            visible = paragraph.text[local_start:local_end]
            paragraph_results.append({
                "paragraph_id": paragraph.paragraph_id,
                "source_ordinal": paragraph.source_ordinal,
                "text": visible,
                "paragraph_returned_start": local_start,
                "paragraph_returned_end": local_end,
                "paragraph_total_codepoints": len(paragraph.text),
                "style": paragraph.style,
                "speech_start": paragraph.speech_start,
                "speech_end": paragraph.speech_end,
                "tags": [
                    {"start": max(a, local_start) - local_start,
                     "end": min(b, local_end) - local_start}
                    for a, b in paragraph.tags
                    if max(a, local_start) < min(b, local_end)
                ],
                "bookmarks": list(paragraph.bookmarks),
            })
        return {
            "document_view_id": view_id,
            "prose_sha256": projected.prose_sha256,
            "tagged_sha256": projected.tagged_sha256,
            "projection_version": projected.projection_version or PROJECTION_VERSION,
            "prose_projection_sha256": projected.prose_projection_sha256,
            "spoken_projection_sha256": projected.spoken_projection_sha256,
            "speech_text_sha256": projected.speech_text_sha256,
            "speech_text_total_codepoints": len(projected.speech_text),
            "speech_text": projected.speech_text[offset:end],
            "returned_start": offset,
            "returned_end": end,
            "paragraphs": paragraph_results,
            "excluded_paragraphs": _data(projected.excluded_paragraphs),
            "source_text_matches_without_tags": projected.source_text_matches_without_tags,
            "warnings": [
                {"code": "unsupported_structure",
                 "message": f"{item.part}:{item.location}: {item.detail}"}
                for item in projected.unsupported
            ],
            "has_more": end < len(projected.speech_text),
            "next_cursor": _cursor(view_id, end) if end < len(projected.speech_text) else None,
        }

    def prepare(self, request: dto.PrepareRequest, *, owner_key: str) -> tuple[dict[str, Any], bool]:
        # Mutation is the only path that creates state. The caller owns the
        # per-project write lock and permission check.
        state = self.discover_state()
        pending_view = self._pending_views.get(request.document_view_id) if state is None else None
        config = load_book_config(self.root, state)
        if config.config_state != "enabled" or config.layout is None:
            if config.config_state != "bootstrap_pending" or config.layout is None:
                raise BookServiceError("configuration_conflict", "Book configuration is not enabled and valid.")
        layout = config.layout
        chapter = self._chapter(layout, request.chapter_id)
        view_row = (state.load_view(request.document_view_id) if state is not None else None) or pending_view
        if view_row is None or view_row["chapter_id"] != request.chapter_id:
            raise BookServiceError("view_not_found", "The document view has expired or does not belong to this chapter.")
        if datetime.fromisoformat(view_row["expires_at"]) <= datetime.now(timezone.utc):
            raise BookServiceError("view_expired", "The pinned document view has expired.")
        payload = view_row["payload"]
        projected_data = payload["projection"]
        if payload["layout_revision"] != layout.layout_revision:
            raise BookServiceError("stale_configuration", "The book layout changed after inspection.")
        args_sha = canonical_json_sha256(request.model_dump(mode="json", exclude_unset=True))
        prior = (state.receipt(owner_key=owner_key, project=self.project_name,
                               tool="audiobook_prepare_chapter", operation_id=request.operation_id)
                 if state is not None else None)
        if prior is not None:
            if prior[0] != args_sha:
                raise BookServiceError("operation_id_conflict", "This operation ID was used for different content.")
            return prior[1], True
        prose_bytes = _read_bytes(self.root, chapter.working_filepath)
        tagged_bytes = _read_bytes(self.root, chapter.tagged_filepath)
        if (hashlib.sha256(prose_bytes).hexdigest() != request.expected_prose_sha256
                or hashlib.sha256(tagged_bytes).hexdigest() != request.expected_tagged_sha256
                or request.expected_prose_sha256 != projected_data["prose_sha256"]
                or request.expected_tagged_sha256 != projected_data["tagged_sha256"]):
            raise BookServiceError("stale_source", "The registered source changed after inspection.")
        projected = project_docx_pair(
            prose_bytes, tagged_bytes,
            speech_paragraph_ids=payload["speech_paragraph_ids"],
            excluded_paragraphs=payload["excluded_paragraphs"],
            explicit_tag_spans=payload["explicit_tag_spans"],
        )
        if projected.document_view_id != request.document_view_id:
            raise BookServiceError("stale_view", "The current sources no longer match the inspected view.")
        if not projected.source_text_matches_without_tags:
            raise BookServiceError("tagged_source_mismatch", "Tagged speech text does not match the registered prose source.")
        chapter_state_bytes = _read_bytes(self.root, chapter.chapter_state_filepath)
        try:
            chapter_state = validate_chapter_state(chapter_state_bytes)
        except Exception as exc:
            raise BookServiceError("configuration_conflict", "Chapter editorial state is invalid.") from exc
        if chapter_state.chapter_id != chapter.chapter_id or chapter_state.layout_revision != layout.layout_revision:
            raise BookServiceError("stale_configuration", "Chapter state is bound to another layout revision.")
        if isinstance(request.scope, dto.ProductionScope):
            authorization = layout.production_authorization
            if authorization is None or authorization.revoked or not authorization.completed_book:
                raise BookServiceError("production_not_authorized", "Production requires an active completed-book authorization.")
            approval = chapter_state.approval_provenance
            if (chapter_state.editorial_status != "approved" or approval is None
                    or chapter_state.approved_source_raw_sha256 != projected.prose_sha256
                    or chapter_state.approved_prose_projection_sha256 != projected.prose_projection_sha256
                    or chapter_state.approval_projection_version != projected.projection_version):
                raise BookServiceError("chapter_not_approved", "The current prose revision lacks matching approval provenance.")
            settings_path = layout.shared_paths.production_settings_filepath
            settings_bytes = _read_bytes(self.root, settings_path)
            settings_sha = hashlib.sha256(settings_bytes).hexdigest()
            if request.expected_settings_sha256 is None or request.expected_settings_sha256 != settings_sha:
                raise BookServiceError("stale_settings", "Production settings changed or their expected hash is missing.")
            try:
                settings = validate_production_settings(settings_bytes)
            except Exception as exc:
                raise BookServiceError("configuration_conflict", "Production settings are invalid.") from exc
            if (request.production_target != settings.production_target
                    or request.request_limit.value != settings.request_limit.value
                    or request.request_limit.unit != settings.request_limit.unit):
                raise BookServiceError("settings_mismatch", "The requested production settings differ from the validated book settings.")
        else:
            auth = next((item for item in layout.test_authorizations
                         if item.authorization_id == request.scope.authorization_id), None)
            now = datetime.now(timezone.utc)
            if (auth is None or auth.revoked or auth.chapter_id != chapter.chapter_id
                    or auth.prose_filepath != chapter.working_filepath
                    or auth.tagged_filepath != chapter.tagged_filepath
                    or auth.source_raw_sha256 != projected.prose_sha256
                    or now < datetime.fromisoformat(auth.authorized_at.replace("Z", "+00:00"))
                    or now >= datetime.fromisoformat(auth.expires_at.replace("Z", "+00:00"))):
                raise BookServiceError("test_scope_not_authorized", "The test authorization is absent, stale, revoked, or expired.")
            selected_ordinals = {p.source_ordinal for p in projected.paragraphs if p.speech_start is not None}
            if not selected_ordinals.issubset(set(auth.allowed_paragraph_ordinals)):
                raise BookServiceError("test_scope_not_authorized", "The selected paragraphs exceed the test authorization.")
        ranges, coverage = validate_chunk_ranges(
            projected, request.chunks,
            limit=request.request_limit.value, unit=request.request_limit.unit,
        )
        # Do not create a state tree or bind a layout until the source pair,
        # authorization and plan have passed validation. Both operations are
        # create-only; a crash after DB creation remains bootstrap_pending and
        # the same prepare can finish the fixed binding on retry.
        if state is None:
            state = ProjectState.initialize(self.root)
            self.state = state
            if pending_view is not None:
                state.save_view(
                    view_id=request.document_view_id,
                    chapter_id=pending_view["chapter_id"],
                    scope_json=pending_view["scope_json"],
                    payload=pending_view["payload"],
                    expires_at=pending_view["expires_at"],
                )
        if config.config_state == "bootstrap_pending":
            binding = BookBinding(
                schema_version=1, book_id=layout.book_id,
                layout_filepath=LAYOUT_PATH, state_root=STATE_ROOT,
            )
            binding_path = self.root / BINDING_PATH
            binding_bytes = binding.model_dump_json(exclude_unset=True).encode("utf-8")
            try:
                descriptor = os.open(binding_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(binding_bytes)
                    stream.flush()
                    os.fsync(stream.fileno())
            except FileExistsError:
                pass
            except OSError as exc:
                raise BookServiceError("state_unavailable", "The fixed book binding could not be published.") from exc
            config = load_book_config(self.root, state)
            if config.config_state != "enabled":
                raise BookServiceError("configuration_conflict", "Book binding publication did not validate.")
        prior = state.receipt(owner_key=owner_key, project=self.project_name,
                              tool="audiobook_prepare_chapter", operation_id=request.operation_id)
        if prior is not None:
            if prior[0] != args_sha:
                raise BookServiceError("operation_id_conflict", "This operation ID was used for different content.")
            return prior[1], True
        if request.expected_manifest_revision != (state.namespace(request.chapter_id, request.scope.model_dump_json()) or {}).get("manifest_revision"):
            raise BookServiceError("stale_manifest", "The chapter manifest changed; inspect and prepare again.")
        snapshot_id = str(uuid.uuid4())
        scope_path = hashlib.sha256(request.scope.model_dump_json().encode()).hexdigest()[:24]
        snapshot_dir = self.root / STATE_ROOT / "snapshots" / layout.book_id / request.chapter_id / scope_path
        _mkdir_safe(self.root, snapshot_dir.relative_to(self.root).as_posix())
        prose_path = snapshot_dir / f"{snapshot_id}-prose.docx"
        tagged_path = snapshot_dir / f"{snapshot_id}-tagged.docx"
        for path, content in ((prose_path, prose_bytes), (tagged_path, tagged_bytes)):
            try:
                fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                with os.fdopen(fd, "wb") as stream:
                    stream.write(content)
                    stream.flush()
                    os.fsync(stream.fileno())
            except OSError as exc:
                raise BookServiceError("publication_failed", "The immutable source snapshot could not be staged.") from exc
        chunks: list[dict[str, Any]] = []
        plan_rows: list[dict[str, Any]] = []
        requested_chunks = {item.chunk_id: item for item in request.chunks}
        for order, item in enumerate(ranges):
            text = projected.speech_text[item.start:item.end]
            requested = requested_chunks[item.chunk_id]
            request_spec = (
                _data(requested.request_spec)
                if requested.request_spec is not None else None
            )
            chunk = {
                "chunk_id": item.chunk_id, "snapshot_id": snapshot_id, "order": order,
                "start": item.start, "end": item.end, "bookmark": f"cognita_{item.chunk_id}",
                "source_segments": _data(item.source_segments),
                "codepoint_count": item.codepoint_count, "limit_count": item.limit_count,
                "prompt_sha256": item.prompt_sha256,
                "spoken_text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                "request_sha256": (
                    request_fingerprint(text, requested.request_spec)
                    if requested.request_spec is not None else None
                ),
                "request_spec": request_spec,
                "replaces_chunk_ids": [], "replaced_by_chunk_ids": [],
                "opening_phrase": text[:120], "closing_phrase": text[-120:],
                "take_ids": [], "accepted_take_id": None,
                "reuse_status": "new", "reusable_take_ids": [],
            }
            chunks.append(chunk)
            plan_rows.append({"chunk_id": chunk["chunk_id"], "start": item.start,
                              "end": item.end, "prompt_sha256": item.prompt_sha256})
        plan_sha = canonical_json_sha256(plan_rows)
        result = {
            "snapshot_id": snapshot_id,
            "manifest_revision": 1,
            "input_tagged_sha256": projected.tagged_sha256,
            "snapshot_tagged_sha256": projected.tagged_sha256,
            "snapshot_prose_sha256": projected.prose_sha256,
            "prose_projection_sha256": projected.prose_projection_sha256,
            "spoken_projection_sha256": projected.spoken_projection_sha256,
            "request_plan_sha256": plan_sha,
            "snapshot_filepath": prose_path.relative_to(self.root).as_posix(),
            "working_tagged_filepath": chapter.tagged_filepath,
            "working_tagged_updated": False,
            "chunks": chunks, "retired_chunk_ids": [],
            "coverage": _data(coverage), "current_outputs_stale": True,
        }
        snapshot_payload = {
            "result": result, "prose_filepath": prose_path.relative_to(self.root).as_posix(),
            "tagged_filepath": tagged_path.relative_to(self.root).as_posix(),
            "speech_text": projected.speech_text,
            "spoken_projection": projected.spoken_projection,
            "prose_projection": projected.prose_projection,
        }
        try:
            status, revision, committed = state.commit_snapshot(
                snapshot_id=snapshot_id, chapter_id=request.chapter_id,
                scope_key=request.scope.model_dump_json(),
                expected_manifest_revision=request.expected_manifest_revision,
                payload=snapshot_payload, request_plan_sha256=plan_sha,
                receipt_owner_key=owner_key, project=self.project_name,
                tool="audiobook_prepare_chapter", operation_id=request.operation_id,
                args_sha256=args_sha, result=result,
            )
        except ProjectStateError as exc:
            raise BookServiceError("stale_manifest" if "stale_manifest" in str(exc) else "state_unavailable",
                                   "Snapshot publication could not be committed.") from exc
        if status == "replay":
            return committed, True
        self._pending_views.pop(request.document_view_id, None)
        result = committed
        result["manifest_revision"] = revision
        return result, False

    def get_chapter(self, request: dto.GetChapterRequest) -> dict[str, Any]:
        state, _, layout = self._enabled_layout()
        chapter = self._chapter(layout, request.chapter_id)
        scope_key = request.scope.model_dump_json() if "scope" in request.model_fields_set else None
        namespace = state.namespace(chapter.chapter_id, scope_key) if scope_key else None
        snapshot_id = request.snapshot_id if "snapshot_id" in request.model_fields_set else (
            namespace.get("current_snapshot_id") if namespace else None
        )
        stored = state.snapshot(snapshot_id) if snapshot_id else None
        if stored is not None and stored["chapter_id"] != chapter.chapter_id:
            raise BookServiceError("snapshot_not_found", "The snapshot does not belong to this chapter.")
        snap = stored["payload"] if stored else {}
        result_data = snap.get("result", {})
        chunks = result_data.get("chunks", [])
        if "chunk_ids" in request.model_fields_set:
            wanted = set(request.chunk_ids)
            chunks = [item for item in chunks if item["chunk_id"] in wanted]
        return {
            "chapter_id": chapter.chapter_id,
            "namespace": request.scope.model_dump(mode="json") if scope_key else {"kind": "production"},
            "manifest_revision": namespace.get("manifest_revision") if namespace else None,
            "media_revision": namespace.get("media_revision", 0) if namespace else 0,
            "head_revision": namespace.get("head_revision") if namespace else None,
            "snapshot_id": snapshot_id,
            "prose_sha256": result_data.get("snapshot_prose_sha256"),
            "tagged_sha256": result_data.get("snapshot_tagged_sha256"),
            "spoken_projection_sha256": result_data.get("spoken_projection_sha256"),
            "request_plan_sha256": result_data.get("request_plan_sha256"),
            "accepted_build_id": None, "accepted_snapshot_id": None,
            "accepted_request_plan_sha256": None, "production_settings_sha256": None,
            "candidate_build_ids": [], "accepted_plan_matches_prepared": None,
            "current_outputs_stale": bool(snapshot_id),
            "source_status": "eligible" if snapshot_id else "not_prepared",
            "chunks": chunks, "takes": [], "returned_texts": [],
            "has_more": False, "next_cursor": None,
        }

    def find_chunk(self, request: dto.FindChunkRequest) -> dict[str, Any]:
        state = self.discover_state()
        matches: list[dict[str, Any]] = []
        searched_snapshots: list[str] = []
        query_value = request.query.model_dump(mode="json", exclude_unset=True)
        if isinstance(request.query, dto.TimestampQuery):
            # No build timeline exists in M1; timestamp searches are valid and
            # return an empty, explicit result until the build worker lands.
            version = "book-state-v1"
        else:
            version = "spoken-text-v1"
            chapter_ids = [request.chapter_id] if "chapter_id" in request.model_fields_set else []
            if not chapter_ids and state is not None:
                cfg = self.config()
                chapter_ids = [chapter.chapter_id for chapter in cfg.layout.chapters] if cfg.layout else []
            query = request.query.text
            before = request.query.before_text if "before_text" in request.query.model_fields_set else None
            after = request.query.after_text if "after_text" in request.query.model_fields_set else None
            for chapter_id in chapter_ids:
                scope_key = dto.ProductionScope(kind="production").model_dump_json()
                namespace = state.namespace(chapter_id, scope_key) if state else None
                snapshot_id = request.query.snapshot_id if "snapshot_id" in request.query.model_fields_set else (
                    namespace.get("current_snapshot_id") if namespace else None
                )
                if not snapshot_id or state is None:
                    continue
                stored = state.snapshot(snapshot_id)
                if not stored or stored["chapter_id"] != chapter_id:
                    continue
                searched_snapshots.append(snapshot_id)
                snapshot = stored["payload"]
                text = snapshot.get("spoken_projection", "")
                start = 0
                while query and (at := text.find(query, start)) >= 0:
                    end = at + len(query)
                    if ((before is None or text[max(0, at-len(before)):at] == before)
                            and (after is None or text[end:end+len(after)] == after)):
                        chunks = snapshot.get("result", {}).get("chunks", [])
                        overlapping = [c["chunk_id"] for c in chunks if c["start"] < end and c["end"] > at]
                        matches.append({
                            "chapter_id": chapter_id, "snapshot_id": snapshot_id,
                            "chunk_ids": overlapping, "occurrence_start": at,
                            "occurrence_end": end, "coordinate_projection": "spoken_text_codepoints",
                            "excerpt": text[max(0, at-80):min(len(text), end+80)],
                            "matched_build_id": None, "matched_take_ids": [],
                            "segment_kind": "speech", "current_chunk_ids": overlapping,
                            "current_take_ids": [], "lineage": [],
                            "current_mapping_status": "not_checked", "match_mode": "literal",
                        })
                    start = at + max(1, len(query))
        cursor_view = canonical_json_sha256({
            "version": version, "query": query_value,
            "chapter_ids": chapter_ids if not isinstance(request.query, dto.TimestampQuery) else [],
            "snapshot_ids": searched_snapshots,
        })
        limit = request.limit if "limit" in request.model_fields_set else 100
        offset = _cursor_offset(request.cursor, cursor_view) if "cursor" in request.model_fields_set else 0
        page = matches[offset:offset+limit]
        next_offset = offset + len(page)
        return {
            "matches": page, "ambiguous": len(matches) > 1, "searched_version": version,
            "has_more": next_offset < len(matches),
            "next_cursor": _cursor(cursor_view, next_offset) if next_offset < len(matches) else None,
        }

    def list_files(self, path: str, *, recursive: bool = False, cursor: str | None = None,
                   limit: int = 100, effective_index=None) -> dict[str, Any]:
        state = self.discover_state()
        rules = state.folder_policy() if state else None
        return list_project_files(
            self.root, path, recursive=recursive, cursor=cursor, limit=limit,
            policy_revision=rules.policy_revision if rules else 0,
            folder_rules=rules.rules if rules else (),
            effective_index=(lambda rel: (
                (decision := effective_index.decision(rel)).indexed,
                None if decision.indexed else f"{decision.reason}:{decision.matched_path or ''}",
            )) if effective_index is not None else None,
        )

    def read_file(self, path: str, *, offset: int = 0, max_bytes: int = 262144,
                  expected_bytes_sha256: str | None = None) -> dict[str, Any]:
        result = read_project_file(
            self.root, path, offset=offset, max_bytes=max_bytes,
            expected_bytes_sha256=expected_bytes_sha256,
        )
        if result["next_offset"] is None:
            result["next_offset"] = offset + len(base64.b64decode(result["content_base64"]))
        return result

    def record_generation(
        self, request: dto.RecordGenerationRequest, *, owner_key: str,
    ) -> tuple[dict[str, Any], bool]:
        """Persist generation evidence; this method never calls a provider."""
        state, _, layout = self._enabled_layout()
        change = request.change
        args_sha256 = canonical_json_sha256({
            "project": request.project,
            "change": _data(change),
        })
        if isinstance(change, dto.UpdateGenerationChange):
            return self._update_generation(change, state, request, owner_key, args_sha256)
        chapter = self._chapter(layout, change.chapter_id)
        stored = state.snapshot(change.snapshot_id)
        if stored is None or stored["chapter_id"] != chapter.chapter_id:
            raise BookServiceError("not_prepared", "The requested frozen chapter snapshot is unavailable.")
        if stored["manifest_revision"] != change.expected_manifest_revision:
            raise BookServiceError("stale_manifest", "The chapter manifest changed before generation reservation.")
        snapshot = stored["payload"]
        chunk = next(
            (item for item in snapshot.get("result", {}).get("chunks", [])
             if item.get("chunk_id") == change.chunk_id),
            None,
        )
        if chunk is None:
            raise BookServiceError("validation_failed", "The chunk is not part of the frozen snapshot.")
        supplied_request = _data(change.request)
        if (chunk.get("prompt_sha256") != change.request.prompt_sha256
                or chunk.get("request_sha256") is None
                or chunk.get("request_spec") != supplied_request["spec"]):
            raise BookServiceError(
                "request_hash_mismatch",
                "The generation request must exactly match the frozen chunk prompt and request specification.",
            )
        try:
            scope = json.loads(stored["scope_key"])
        except (KeyError, TypeError, ValueError) as exc:
            raise BookServiceError("state_unavailable", "The frozen snapshot has an invalid scope binding.") from exc
        now = datetime.now(timezone.utc).isoformat()
        generation = dto.GenerationRecord.model_validate({
            "generation_record_id": str(uuid.uuid4()), "scope": scope,
            "chapter_id": chapter.chapter_id, "snapshot_id": change.snapshot_id,
            "chunk_id": change.chunk_id, "generation_revision": 1, "state": "reserved",
            "request_sha256": chunk["request_sha256"], "request": supplied_request,
            "provider_ids": {}, "provider_response_metadata": {}, "cost": None,
            "failure": None, "media_registered": False, "take_id": None,
            "import_job_id": None, "created_at": now, "updated_at": now,
        }, strict=True).model_dump(mode="json", exclude_unset=True)
        record = {
            "generation_record_id": generation["generation_record_id"],
            "chapter_id": chapter.chapter_id, "scope_key": stored["scope_key"],
            "snapshot_id": change.snapshot_id, "chunk_id": change.chunk_id,
            "generation_revision": generation["generation_revision"],
            "state": generation["state"], "request_sha256": generation["request_sha256"],
            "payload_json": json.dumps(generation, ensure_ascii=False, separators=(",", ":")),
            "created_at": now, "updated_at": now,
        }
        try:
            disposition, result = state.reserve_generation(
                record=record, owner_key=owner_key, project=self.project_name,
                tool="audiobook_record_generation", operation_id=request.operation_id,
                args_sha256=args_sha256,
            )
        except ProjectStateError as exc:
            if "operation_id_conflict" in str(exc):
                raise BookServiceError("operation_id_conflict", "The operation ID was used with different arguments.") from exc
            raise BookServiceError("state_unavailable", "Generation state could not be persisted.") from exc
        return result, disposition == "replay"

    def _update_generation(self, change, state, request, owner_key: str, args_sha256: str) -> tuple[dict[str, Any], bool]:
        prior = state.receipt(
            owner_key=owner_key, project=self.project_name,
            tool="audiobook_record_generation", operation_id=request.operation_id,
        )
        if prior is not None:
            if prior[0] != args_sha256:
                raise BookServiceError("operation_id_conflict", "The operation ID was used with different arguments.")
            return prior[1], True
        existing = state.generation(change.generation_record_id)
        if existing is None:
            raise BookServiceError("file_not_found", "The generation record does not exist.")
        if existing["generation_revision"] != change.expected_generation_revision:
            raise BookServiceError("stale_generation", "The generation evidence has changed.")
        transitions = {
            "reserved": {"reserved", "submitted", "failed", "outcome_unknown"},
            "submitted": {"submitted", "running", "completed", "failed", "outcome_unknown"},
            "running": {"running", "completed", "failed", "outcome_unknown"},
            "outcome_unknown": {"outcome_unknown", "submitted", "running", "completed", "failed"},
            "completed": {"completed"}, "failed": {"failed"},
        }
        if change.state not in transitions[existing["state"]]:
            raise BookServiceError("stale_generation", "The requested generation state transition is not allowed.")
        supplied_ids = _data(change.provider_ids) if "provider_ids" in change.model_fields_set else {}
        previous_ids = dict(existing["provider_ids"])
        merged_ids = dict(previous_ids)
        for name, value in supplied_ids.items():
            if isinstance(value, list):
                merged_ids[name] = list(dict.fromkeys([*previous_ids.get(name, []), *value]))
            elif value is not None:
                merged_ids[name] = value
        if existing["state"] == "outcome_unknown" and change.state != "outcome_unknown" and not merged_ids:
            raise BookServiceError("generation_outcome_unknown", "Provider evidence is required before resolving an unknown outcome.")
        value = dict(existing)
        value["generation_revision"] += 1
        value["state"] = change.state
        value["provider_ids"] = merged_ids
        if "provider_response_metadata" in change.model_fields_set:
            value["provider_response_metadata"] = _data(change.provider_response_metadata)
        if "cost" in change.model_fields_set:
            value["cost"] = _data(change.cost)
        if "failure" in change.model_fields_set:
            value["failure"] = _data(change.failure)
        value["updated_at"] = datetime.now(timezone.utc).isoformat()
        try:
            value = dto.GenerationRecord.model_validate(value, strict=True).model_dump(mode="json", exclude_unset=True)
            disposition, result = state.update_generation(
                generation_record_id=change.generation_record_id,
                expected_revision=change.expected_generation_revision, payload=value,
                owner_key=owner_key, project=self.project_name, tool="audiobook_record_generation",
                operation_id=request.operation_id, args_sha256=args_sha256,
            )
        except ProjectStateError as exc:
            if "operation_id_conflict" in str(exc):
                raise BookServiceError("operation_id_conflict", "The operation ID was used with different arguments.") from exc
            if "stale_generation" in str(exc):
                raise BookServiceError("stale_generation", "The generation evidence has changed.") from exc
            raise BookServiceError("state_unavailable", "Generation state could not be persisted.") from exc
        return result, disposition == "replay"

    def get_generations(self, request: dto.GetGenerationsRequest) -> dict[str, Any]:
        state = self._state_required()
        if isinstance(request.query, dto.RecordGenerationQuery):
            records = state.generations(generation_record_id=request.query.generation_record_id)
        else:
            self._chapter(self._enabled_layout()[2], request.query.chapter_id)
            records = state.generations(chapter_id=request.query.chapter_id)
            if "states" in request.query.model_fields_set:
                records = [item for item in records if item["state"] in request.query.states]
        view = canonical_json_sha256({"query": _data(request.query), "records": [r["generation_record_id"] for r in records]})
        limit = request.limit if "limit" in request.model_fields_set else 100
        offset = _cursor_offset(request.cursor, view) if "cursor" in request.model_fields_set else 0
        page = records[offset:offset + limit]
        prompts = []
        if "include_prompt" in request.model_fields_set and request.include_prompt:
            for record in page:
                stored = state.snapshot(record["snapshot_id"])
                if stored is None:
                    continue
                chunk = next((c for c in stored["payload"]["result"]["chunks"] if c["chunk_id"] == record["chunk_id"]), None)
                if chunk is None:
                    continue
                text = stored["payload"]["speech_text"][chunk["start"]:chunk["end"]]
                prompts.append({"generation_record_id": record["generation_record_id"], "prompt": {
                    "text": text, "returned_start": 0, "returned_end": len(text),
                    "total_codepoints": len(text), "text_sha256": hashlib.sha256(text.encode()).hexdigest(),
                }})
        end = offset + len(page)
        return {"generations": page, "prompts": prompts, "has_more": end < len(records),
                "next_cursor": _cursor(view, end) if end < len(records) else None}

    @staticmethod
    def _metadata_contains(value: object, expected: str) -> bool:
        if isinstance(value, str):
            return value == expected
        if isinstance(value, dict):
            return any(BookService._metadata_contains(item, expected) for item in value.values())
        if isinstance(value, list):
            return any(BookService._metadata_contains(item, expected) for item in value)
        return False

    def _import_paths(self, layout, chapter, job_id: str, take_id: str, *, raw_pcm: bool) -> tuple[Path, Path, str]:
        staging_root = self.root / STATE_ROOT / "audiobook-staging"
        staging_root.mkdir(mode=0o700, exist_ok=True)
        staging = staging_root / f"{job_id}.part"
        extension = "pcm" if raw_pcm else "wav"
        relative = f"{chapter.audio_root}/takes/{take_id}/native.{extension}"
        target = _path(self.root, relative, allow_missing=True)
        return staging, target, relative

    def _available_import_space(self, layout, chapter, source_size: int) -> None:
        audio_root = _path(self.root, layout.shared_paths.book_audio_root, allow_missing=True)
        usage = shutil.disk_usage(audio_root if audio_root.exists() else self.root)
        # The job temporarily owns both a staged source and its immutable take.
        required = source_size * 2 + layout.storage.reserve_bytes
        if usage.free < required:
            raise BookServiceError("insufficient_storage", "The configured media reserve leaves insufficient free space.")
        if source_size * 2 > layout.storage.quota_bytes:
            raise BookServiceError("insufficient_storage", "The source exceeds the configured audiobook media quota.")

    def import_audio(self, request: dto.ImportAudioRequest, *, owner_key: str) -> tuple[dict[str, Any], bool]:
        """Reserve a durable project-file PCM import; worker execution is separate.

        URL and workspace bytes are intentionally not accepted until their
        respective guarded transfer boundaries are installed.  In particular,
        no signed URL is placed into a durable job payload.
        """
        state, _, layout = self._enabled_layout()
        if not isinstance(request.source, dto.ProjectAudioSource):
            raise BookServiceError("source_unavailable", "This installation currently supports authorized project-file audio imports only.")
        if "source_format" not in request.model_fields_set:
            raise BookServiceError("native_pcm_required", "Headerless raw PCM imports require an explicit RawFormat.")
        raw_format = request.source_format
        args_sha256 = canonical_json_sha256({
            "project": request.project, "generation_record_id": request.generation_record_id,
            "expected_generation_revision": request.expected_generation_revision,
            "source": _data(request.source), "provenance": request.provenance,
            "source_format": _data(raw_format),
        })
        prior = state.receipt(
            owner_key=owner_key, project=self.project_name,
            tool="audiobook_import_audio", operation_id=request.operation_id,
        )
        if prior is not None:
            if prior[0] != args_sha256:
                raise BookServiceError("operation_id_conflict", "The operation ID was used with different arguments.")
            return prior[1], True
        generation = state.generation(request.generation_record_id)
        if generation is None:
            raise BookServiceError("file_not_found", "The generation record does not exist.")
        if generation["generation_revision"] != request.expected_generation_revision:
            raise BookServiceError("stale_generation", "The generation evidence has changed.")
        chapter = self._chapter(layout, generation["chapter_id"])
        # Validate the source guard before creating any durable reservation.
        source_path = _path(self.root, request.source.filepath)
        facts = source_path.stat(follow_symlinks=False)
        if not stat.S_ISREG(facts.st_mode):
            raise BookServiceError("source_unavailable", "The project-file source is not a regular file.")
        if not self._metadata_contains(generation["provider_response_metadata"], raw_format.provider_format_evidence):
            raise BookServiceError("media_mismatch", "RawFormat provider evidence is not present in saved generation evidence.")
        job_id, take_id = str(uuid.uuid4()), str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()
        # This deliberately contains no source URL and pins only the guarded
        # project path and immutable generation/request facts needed by worker.
        pinned = {
            "generation_record_id": generation["generation_record_id"],
            "generation_revision": generation["generation_revision"],
            "chapter_id": generation["chapter_id"], "snapshot_id": generation["snapshot_id"],
            "chunk_id": generation["chunk_id"], "request_sha256": generation["request_sha256"],
            "source_kind": "project_file", "source_filepath": request.source.filepath,
            "expected_sha256": request.source.expected_sha256, "provenance": request.provenance,
            "source_format": _data(raw_format), "take_id": take_id, "created_at": now,
        }
        pinned_sha256 = canonical_json_sha256(pinned)
        try:
            disposition, result = state.reserve_import_job(
                generation_record_id=request.generation_record_id,
                expected_generation_revision=request.expected_generation_revision,
                owner_key=owner_key, project=self.project_name, operation_id=request.operation_id,
                args_sha256=args_sha256, job_id=job_id, pinned_inputs_sha256=pinned_sha256,
                payload=pinned,
            )
        except ProjectStateError as exc:
            code = str(exc)
            reasons = {
                "operation_id_conflict": "operation_id_conflict", "stale_generation": "stale_generation",
                "generation_not_found": "file_not_found", "generation_not_completed": "generation_outcome_unknown",
                "import_already_registered": "import_already_registered", "import_in_progress": "import_in_progress",
            }
            raise BookServiceError(reasons.get(code, "state_unavailable"), "The import reservation could not be persisted.") from exc
        return result, disposition == "replay"

    def _stream_project_audio(self, source_relative: str, staging: Path, expected_sha256: str,
                              *, source_size: int, job_id: str) -> None:
        source = _path(self.root, source_relative)
        before = source.stat(follow_symlinks=False)
        digest = hashlib.sha256()
        copied = 0
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(source, flags)
            with os.fdopen(descriptor, "rb") as input_stream, staging.open("xb") as output_stream:
                while block := input_stream.read(1024 * 1024):
                    current = self._state_required().import_job(job_id)
                    if current is None or current["state"] == "cancel_requested":
                        raise BookServiceError("cancelled", "The import was cancelled.")
                    digest.update(block)
                    copied += len(block)
                    output_stream.write(block)
                output_stream.flush()
                os.fsync(output_stream.fileno())
        except FileExistsError as exc:
            raise BookServiceError("job_failed", "The owned import staging file already exists.") from exc
        after = source.stat(follow_symlinks=False)
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
            after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns
        ) or copied != source_size:
            raise BookServiceError("stale_file", "The guarded source changed while it was imported.")
        if digest.hexdigest() != expected_sha256:
            raise BookServiceError("media_mismatch", "The imported bytes do not match expected_sha256.")

    def run_import_job(self, job_id: str) -> None:
        """Run one owned import outside SQLite transactions and finalize atomically."""
        state = self._state_required()
        claimed = state.claim_import_job(job_id)
        if claimed is None:
            pending = state.import_job(job_id)
            if pending is not None and pending["state"] == "cancel_requested":
                state.finish_import_failure(job_id=job_id, reason="cancelled",
                                            message="The import was cancelled before it started.", cancelled=True)
            return
        self._active_import_jobs.add(job_id)
        staging: Path | None = None
        try:
            pinned = claimed["payload"]
            _, _, layout = self._enabled_layout()
            generation = state.generation(pinned["generation_record_id"])
            if generation is None:
                raise BookServiceError("job_failed", "The pinned generation record is unavailable.")
            chapter = self._chapter(layout, pinned["chapter_id"])
            source = _path(self.root, pinned["source_filepath"])
            source_size = source.stat(follow_symlinks=False).st_size
            self._available_import_space(layout, chapter, source_size)
            staging, target, relative = self._import_paths(layout, chapter, job_id, pinned["take_id"], raw_pcm=True)
            self._stream_project_audio(pinned["source_filepath"], staging, pinned["expected_sha256"],
                                       source_size=source_size, job_id=job_id)
            inspection = inspect_media_file(
                staging, raw_format=pinned["source_format"],
                provider_format_evidence=pinned["source_format"]["provider_format_evidence"],
            )
            if not inspection.native_pcm:
                raise BookServiceError("native_pcm_required", "The imported media is not verified native PCM.")
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            if target.exists():
                raise BookServiceError("job_failed", "The immutable take path already exists.")
            os.replace(staging, target)
            staging = None
            now = datetime.now(timezone.utc).isoformat()
            take = dto.TakeRecord.model_validate({
                "take_id": pinned["take_id"], "namespace": generation["scope"],
                "chapter_id": pinned["chapter_id"], "snapshot_id": pinned["snapshot_id"],
                "chunk_id": pinned["chunk_id"], "generation_record_id": pinned["generation_record_id"],
                "request_sha256": pinned["request_sha256"], "filepath": relative,
                "bytes_sha256": inspection.bytes_sha256, "size_bytes": inspection.size_bytes,
                "media": inspection.media.model_dump(mode="json"), "provenance": pinned["provenance"],
                "assembly_derivative": None,
            }, strict=True).model_dump(mode="json", exclude_unset=True)
            completed = dict(generation)
            completed["updated_at"] = now
            completed["state"] = "completed"
            state.finish_import_success(job_id=job_id, generation_payload=completed, take_payload=take)
        except (BookServiceError, MediaValidationError, OSError, ProjectStateError) as exc:
            if isinstance(exc, MediaValidationError):
                reason, message = exc.code, exc.message
            elif isinstance(exc, BookServiceError):
                reason, message = exc.reason, str(exc)
            elif isinstance(exc, ProjectStateError) and str(exc) == "cancel_requested":
                reason, message = "cancelled", "The import was cancelled before it could be finalized."
            else:
                reason, message = "job_failed", "The local import could not complete safely."
            state.finish_import_failure(job_id=job_id, reason=reason, message=message,
                                        cancelled=reason == "cancelled")
        finally:
            self._active_import_jobs.discard(job_id)
            if staging is not None:
                try:
                    staging.unlink(missing_ok=True)
                except OSError:
                    pass

    def mark_import_worker_started(self, job_id: str) -> None:
        """Record in-process ownership before the event loop yields to its worker."""
        self._active_import_jobs.add(job_id)

    def mark_import_worker_finished(self, job_id: str) -> None:
        """Clear pre-start ownership when task scheduling itself is cancelled."""
        self._active_import_jobs.discard(job_id)

    def get_job(self, request: dto.GetJobRequest) -> dict[str, Any]:
        state = self._state_required()
        job = state.import_job(request.job_id)
        if job is None:
            raise BookServiceError("file_not_found", "The durable job does not exist.")
        if job["state"] in {"queued", "running"} and request.job_id not in self._active_import_jobs:
            # A fresh service instance proves no worker survived restart.  Do
            # not silently repeat local work or reuse a transient source.
            state.finish_import_failure(job_id=request.job_id, reason="job_failed",
                                        message="The unfinished import was interrupted by restart.")
            job = state.import_job(request.job_id)
            assert job is not None
        phase = "completed" if job["state"] == "succeeded" else (
            "cancel_requested" if job["state"] == "cancel_requested" else job["state"]
        )
        public_state = "running" if job["state"] == "cancel_requested" else job["state"]
        value = {
            "job_id": job["job_id"], "operation_id": job["operation_id"],
            "job_revision": job["job_revision"], "state": public_state, "phase": phase,
            "progress": {"completed_units": 1 if job["state"] == "succeeded" else 0, "total_units": 1},
            "poll_after_seconds": 1 if job["state"] in {"queued", "running", "cancel_requested"} else None,
            "result": job["result"], "error": job["error"],
        }
        return dto.GetJobResult.model_validate(value, strict=True).model_dump(mode="json", exclude_unset=True)

    def cancel_job(self, request: dto.CancelJobRequest, *, owner_key: str) -> tuple[dict[str, Any], bool]:
        state = self._state_required()
        args_sha256 = canonical_json_sha256({"project": request.project, "job_id": request.job_id,
                                             "expected_job_revision": request.expected_job_revision})
        try:
            disposition, result = state.request_import_cancellation(
                job_id=request.job_id, expected_job_revision=request.expected_job_revision,
                owner_key=owner_key, project=self.project_name, operation_id=request.operation_id,
                args_sha256=args_sha256,
            )
        except ProjectStateError as exc:
            reason = {"operation_id_conflict": "operation_id_conflict", "job_not_found": "file_not_found",
                      "stale_job": "stale_generation"}.get(str(exc), "state_unavailable")
            raise BookServiceError(reason, "The import cancellation could not be persisted.") from exc
        return result, disposition == "replay"

    def set_folder_indexing(
        self, *, path: str, indexed: bool, operation_id: str,
        expected_policy_revision: int, owner_key: str,
    ) -> tuple[dict[str, Any], bool]:
        from .storage import normalize_project_path
        normalized = normalize_project_path(path, allow_root=True)
        if normalized == ".cognita-storage" or normalized.startswith(".cognita-storage/"):
            raise BookServiceError("permission_denied", "Managed Cognita state cannot be indexed.")
        target = _path(self.root, normalized, allow_missing=False) if normalized else self.root
        try:
            facts = target.stat(follow_symlinks=False)
        except OSError as exc:
            raise BookServiceError("source_unavailable", "The folder policy target cannot be checked.") from exc
        if not stat.S_ISDIR(facts.st_mode) or stat.S_ISLNK(facts.st_mode):
            raise BookServiceError("validation_failed", "Folder indexing rules require an existing directory.")
        state = self.discover_state() or ProjectState.initialize(self.root)
        self.state = state
        operation_args = {"path": normalized, "indexed": indexed,
                          "expected_policy_revision": expected_policy_revision}
        args_sha = canonical_json_sha256(operation_args)
        job_id = str(uuid.uuid4())
        result = {"path": normalized, "indexed": indexed,
                  "policy_revision": expected_policy_revision + 1, "job_id": job_id}
        try:
            disposition, saved, revision = state.set_folder_rule(
                normalized, indexed, expected_policy_revision,
                owner_key=owner_key, project=self.project_name,
                tool="set_folder_indexing", operation_id=operation_id,
                args_sha256=args_sha, result=result, job_id=job_id,
            )
        except ProjectStateError as exc:
            raise BookServiceError("state_unavailable", "Folder indexing state could not be committed.") from exc
        if disposition == "conflict":
            raise BookServiceError("operation_id_conflict", "This operation ID was used for different content.")
        if disposition == "stale":
            raise BookServiceError("stale_policy_revision", "Folder policy changed; read it and retry.")
        saved["policy_revision"] = revision if disposition == "committed" else saved["policy_revision"]
        return saved, disposition == "replay"

    def update_folder_policy_job(self, job_id: str, state: str, details: dict[str, Any]) -> None:
        """Record derived-index progress after the source-side rule committed."""
        authority = self.discover_state()
        if authority is None:
            raise BookServiceError("state_unavailable", "Folder policy state is unavailable.")
        try:
            authority.update_policy_job(job_id, state, details)
        except ProjectStateError as exc:
            raise BookServiceError("state_unavailable", "Folder policy job state could not be recorded.") from exc


def _mkdir_safe(root: Path, relative: str) -> None:
    target = root
    for part in PurePosixPath(relative).parts:
        target = target / part
        try:
            facts = target.lstat()
        except FileNotFoundError:
            try:
                target.mkdir(mode=0o700)
            except FileExistsError:
                pass
            facts = target.lstat()
        if stat.S_ISLNK(facts.st_mode) or not stat.S_ISDIR(facts.st_mode):
            raise BookServiceError("permission_denied", "Managed snapshot directories cannot contain links.")
