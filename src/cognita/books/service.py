"""Project-scoped, durable book service for the first preparation workflow.

The project files and this module's small SQLite state are the source-side
authority. PostgreSQL is deliberately not consulted here, so inspection,
preparation and project-file reads continue during an index outage.
"""

from __future__ import annotations

import base64
import asyncio
import hashlib
import json
import os
import shutil
import stat
import uuid
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Callable

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
from .assembly import AssemblyError, PcmSource, assemble_pcm_stream, wrap_pcm_as_wave, production_mp3_argv
from .jobs import ProcessRunnerError, ffprobe_json, run_process
from .projection import ProjectionError, project_docx_pair, validate_chunk_ranges
from .read_helpers import ReadCursorError, parse_read_cursor, read_cursor, spoken_interval, text_page
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

    def __init__(self, project_root: Path, project_name: str, *, state: ProjectState | None = None,
                 ffmpeg_executable: Path | None = None, ffprobe_executable: Path | None = None):
        self.root = Path(project_root).resolve(strict=True)
        self.project_name = project_name
        self.state = state
        self.ffmpeg_executable = Path(ffmpeg_executable).resolve() if ffmpeg_executable is not None else None
        self.ffprobe_executable = Path(ffprobe_executable).resolve() if ffprobe_executable is not None else None
        self._pending_views: dict[str, dict[str, Any]] = {}
        # Runtime ownership only. Durable rows remain the restart authority;
        # an unfinished row absent from this set is never assumed to have a
        # surviving worker.
        self._active_import_jobs: set[str] = set()
        self._active_build_jobs: set[str] = set()

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

    def index_status(
        self,
        request: dto.IndexStatusRequest,
        *,
        indexed_sources: set[str] | None,
        effective_index=None,
    ) -> dict[str, Any]:
        """Read registered source/index facts without starting index work.

        The page cursor binds the structural catalog, current layout, and effective
        folder policy.  A caller must refresh instead of combining two revisions.
        """
        state = self.discover_state()
        config = self.config()
        if state is None or config.config_state != "enabled" or config.layout is None:
            raise BookServiceError("configuration_conflict", "Book configuration is not enabled.")
        layout = config.layout
        rows: list[tuple[str, str, Any, str]] = []
        for item in layout.indexed_references:
            rows.append((item.filepath, item.role, None, "not_applicable"))
        for item in layout.indexed_instructions:
            rows.append((item.filepath, item.role, None, "not_applicable"))
        for item in layout.indexed_workflow_documents:
            rows.append((item.filepath, item.role, None, "not_applicable"))
        for chapter in layout.chapters:
            rows.append((chapter.working_filepath, "chapter_working", chapter, "not_applicable"))
            if chapter.summary_filepath:
                rows.append((chapter.summary_filepath, "chapter_summary", chapter, "unapproved"))
        if "chapter_id" in request.model_fields_set:
            rows = [row for row in rows if row[2] is not None and row[2].chapter_id == request.chapter_id]
        if "filepath" in request.model_fields_set:
            rows = [row for row in rows if row[0] == request.filepath]
        rows.sort(key=lambda row: row[0].casefold())

        policy = state.folder_policy()
        pending_jobs = state.pending_policy_jobs()
        entries: list[dict[str, Any]] = []
        for path, role, chapter, default_freshness in rows:
            index_decision = effective_index.decision(path) if effective_index is not None else None
            effective_rule = "global_inclusion" if index_decision is None else index_decision.reason
            if index_decision is not None and index_decision.matched_path is not None:
                effective_rule = f"{effective_rule}:{index_decision.matched_path}"
            try:
                raw = _read_bytes(self.root, path)
                raw_sha = hashlib.sha256(raw).hexdigest()
                source_error = None
            except BookServiceError as exc:
                raw_sha = None
                source_error = {"code": exc.reason, "message": "registered source is unreadable"}
            record = state.indexed_role_provenance(path)
            managed_write = state.managed_write_status(path)
            chapter_state = None
            if chapter is not None and raw_sha is not None:
                try:
                    chapter_state = validate_chapter_state(
                        _read_bytes(self.root, chapter.chapter_state_filepath)
                    )
                except (BookServiceError, ValueError, ProjectionError):
                    chapter_state = None
            editorial_status = chapter_state.editorial_status if chapter_state is not None else None
            summary_freshness = default_freshness
            if role == "chapter_summary":
                if chapter_state is not None and chapter_state.summary is not None and chapter_state.summary.approved:
                    summary_freshness = "fresh" if record is not None and self.index_provenance_is_current(record) else "stale"
                elif raw_sha is not None:
                    summary_freshness = "unapproved"
                else:
                    summary_freshness = "stale"
            pending = next((job for job in pending_jobs if (
                isinstance(job["details"].get("path"), str)
                and (not job["details"]["path"] or path == job["details"]["path"]
                     or path.startswith(job["details"]["path"] + "/"))
            )), None)
            if source_error is not None:
                status, error = "blocked", source_error
            elif index_decision is not None and not index_decision.indexed:
                status, error = "excluded", None
            elif pending is not None:
                status, error = "pending", {
                    "code": "index_cleanup_pending",
                    "message": "derived index cleanup is pending",
                }
            elif (managed_write is not None
                  and managed_write["state"] in {"pending", "blocked", "failed", "stale", "excluded"}):
                # Publication already saved the source bytes.  Its durable
                # indexing receipt is more specific than an absent provenance
                # record and survives an index-store restart.
                status, error = managed_write["state"], managed_write["error"]
            elif indexed_sources is None:
                status, error = "blocked", {
                    "code": "index_unavailable", "message": "derived index is unavailable",
                }
            elif record is None:
                status, error = "pending", None
            elif not self.index_provenance_is_current(record):
                status, error = "stale", {
                    "code": "source_changed", "message": "indexed source facts are stale",
                }
            elif path in indexed_sources:
                status, error = "indexed", None
            else:
                status, error = "pending", None
            entries.append({
                "filepath": path, "chapter_id": chapter.chapter_id if chapter else None,
                "role": role, "index_state": status, "effective_rule": effective_rule,
                "source_raw_sha256": raw_sha,
                "indexed_source_raw_sha256": record.raw_sha256 if record else None,
                "extracted_text_sha256": record.extracted_sha256 if record else None,
                "source_revision": config.layout_sha256,
                "indexed_revision": record.layout_sha256 if record else None,
                "extraction_version": record.extraction_version if record else None,
                "editorial_status": editorial_status, "summary_freshness": summary_freshness,
                "last_indexed_at": None, "error": error,
            })

        cursor_view = canonical_json_sha256({
            "layout_sha256": config.layout_sha256,
            "policy_revision": policy.policy_revision,
            "chapter_id": request.chapter_id if "chapter_id" in request.model_fields_set else None,
            "filepath": request.filepath if "filepath" in request.model_fields_set else None,
            "catalog": entries,
        })
        limit = request.limit if "limit" in request.model_fields_set else 100
        offset = _cursor_offset(request.cursor, cursor_view) if "cursor" in request.model_fields_set else 0
        if offset > len(entries):
            raise BookServiceError("invalid_cursor", "The status cursor is outside the current catalog.")
        page = entries[offset:offset + limit]
        next_offset = offset + len(page)
        return {
            "policy_revision": policy.policy_revision + 1,
            "catalog_revision": layout.layout_revision,
            "entries": page,
            "has_more": next_offset < len(entries),
            "next_cursor": _cursor(cursor_view, next_offset) if next_offset < len(entries) else None,
        }

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

    def _authorize_snapshot_read(self, stored: dict[str, Any], chapter_id: str) -> None:
        """Permit production history or a still-active bound test namespace."""
        try:
            scope = json.loads(stored["scope_key"])
        except (TypeError, json.JSONDecodeError) as exc:
            raise BookServiceError("state_unavailable", "The stored snapshot namespace is malformed.") from exc
        if scope == {"kind": "production"}:
            return
        authorization_id = scope.get("authorization_id") if scope.get("kind") == "test" else None
        config = self.config()
        authorization = next((item for item in (config.layout.test_authorizations if config.layout else [])
                              if item.authorization_id == authorization_id and item.chapter_id == chapter_id), None)
        if authorization is None or authorization.revoked:
            raise BookServiceError("not_authorized", "The requested test snapshot is no longer authorized.")
        try:
            expires_at = datetime.fromisoformat(str(authorization.expires_at).replace("Z", "+00:00"))
        except ValueError as exc:
            raise BookServiceError("state_unavailable", "The test authorization expiry is malformed.") from exc
        if expires_at <= datetime.now(timezone.utc):
            raise BookServiceError("not_authorized", "The requested test snapshot is no longer authorized.")

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
        production_settings_sha256: str | None = None
        prepared_production_target: dict[str, Any] | None = None
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
            production_settings_sha256 = settings_sha
            prepared_production_target = _data(settings.production_target)
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
            "production_settings_sha256": production_settings_sha256,
            "production_target": prepared_production_target,
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
        scope = request.scope if "scope" in request.model_fields_set else dto.ProductionScope(kind="production")
        scope_key = scope.model_dump_json()
        namespace = state.namespace(chapter.chapter_id, scope_key)
        snapshot_id = request.snapshot_id if "snapshot_id" in request.model_fields_set else (
            namespace.get("current_snapshot_id") if namespace else None
        )
        stored = state.snapshot(snapshot_id) if snapshot_id else None
        if stored is not None and (stored["chapter_id"] != chapter.chapter_id or stored["scope_key"] != scope_key):
            raise BookServiceError("snapshot_not_found", "The snapshot does not belong to this chapter.")
        snap = stored["payload"] if stored else {}
        result_data = snap.get("result", {})
        chunks = [dict(item) for item in result_data.get("chunks", [])]
        takes = state.takes(chapter_id=chapter.chapter_id, snapshot_id=snapshot_id) if snapshot_id else []
        take_ids_by_chunk: dict[str, list[str]] = {}
        for take in takes:
            take_ids_by_chunk.setdefault(take["chunk_id"], []).append(take["take_id"])
        for chunk in chunks:
            chunk["take_ids"] = take_ids_by_chunk.get(chunk["chunk_id"], [])
        if "chunk_ids" in request.model_fields_set:
            wanted = set(request.chunk_ids)
            chunks = [item for item in chunks if item["chunk_id"] in wanted]
            takes = [item for item in takes if item["chunk_id"] in wanted]
        candidates = state.builds(chapter_id=chapter.chapter_id, scope_key=scope_key)
        head = state.chapter_head(chapter.chapter_id, scope_key)
        source_status = "not_prepared"
        if snapshot_id:
            try:
                current_raw = hashlib.sha256(_read_bytes(self.root, chapter.working_filepath)).hexdigest()
                if current_raw != result_data.get("snapshot_prose_sha256"):
                    source_status = "changed"
                elif isinstance(scope, dto.ProductionScope):
                    chapter_state = validate_chapter_state(_read_bytes(self.root, chapter.chapter_state_filepath))
                    source_status = "eligible" if (
                        chapter_state.editorial_status == "approved"
                        and chapter_state.approved_source_raw_sha256 == current_raw
                    ) else "unapproved"
                else:
                    source_status = "eligible"
            except (BookServiceError, ValueError):
                source_status = "blocked"
        metadata: list[tuple[str, Any]] = [
            *(("chunk", item) for item in chunks), *(("take", item) for item in takes),
            *(("candidate", item["build_id"]) for item in candidates),
        ]
        view = canonical_json_sha256({
            "chapter": chapter.chapter_id, "scope": scope_key, "snapshot": snapshot_id,
            "manifest": namespace.get("manifest_revision") if namespace else None,
            "media": namespace.get("media_revision") if namespace else 0,
            "head": namespace.get("head_revision") if namespace else None,
            "metadata": [(kind, item if isinstance(item, str) else item.get("chunk_id", item.get("take_id")))
                         for kind, item in metadata],
        })
        limit = request.limit if "limit" in request.model_fields_set else 100
        offset = _cursor_offset(request.cursor, view) if "cursor" in request.model_fields_set else 0
        if offset > len(metadata):
            raise BookServiceError("invalid_cursor", "The chapter cursor is invalid or stale.")
        page_metadata = metadata[offset:offset + limit]
        page_chunks = [dict(item) for kind, item in page_metadata if kind == "chunk"]
        page_takes = [item for kind, item in page_metadata if kind == "take"]
        page_take_ids = {item["take_id"] for item in page_takes}
        for item in page_chunks:
            item["take_ids"] = [take_id for take_id in item.get("take_ids", []) if take_id in page_take_ids]
            item["reusable_take_ids"] = [take_id for take_id in item.get("reusable_take_ids", []) if take_id in page_take_ids]
        page_candidates = [item for kind, item in page_metadata if kind == "candidate"]
        returned_texts: list[dict[str, Any]] = []
        if "include_text" in request.model_fields_set and request.include_text and stored is not None:
            speech_text = snap.get("speech_text", "")
            spoken_text = snap.get("spoken_projection", "")
            cap = request.max_characters if "max_characters" in request.model_fields_set else 40000
            remaining = cap
            for chunk in page_chunks:
                prompt = speech_text[chunk["start"]:chunk["end"]]
                try:
                    spoken = spoken_interval(speech_text, spoken_text, chunk["start"], chunk["end"])
                except ValueError as exc:
                    raise BookServiceError("state_unavailable", "Frozen speech projection is malformed.") from exc
                if len(prompt) + len(spoken) > remaining:
                    break
                returned_texts.append({"chunk_id": chunk["chunk_id"],
                                       "prompt": text_page(prompt, 0, len(prompt)),
                                       "spoken_text": text_page(spoken, 0, len(spoken))})
                remaining -= len(prompt) + len(spoken)
        next_offset = offset + len(page_metadata)
        return {
            "chapter_id": chapter.chapter_id,
            "namespace": scope.model_dump(mode="json"),
            "manifest_revision": namespace.get("manifest_revision") if namespace else None,
            "media_revision": namespace.get("media_revision", 0) if namespace else 0,
            "head_revision": namespace.get("head_revision") if namespace else None,
            "snapshot_id": snapshot_id,
            "prose_sha256": result_data.get("snapshot_prose_sha256"),
            "tagged_sha256": result_data.get("snapshot_tagged_sha256"),
            "spoken_projection_sha256": result_data.get("spoken_projection_sha256"),
            "request_plan_sha256": result_data.get("request_plan_sha256"),
            "accepted_build_id": head["accepted_build_id"] if head else None,
            "accepted_snapshot_id": head["accepted_snapshot_id"] if head else None,
            "accepted_request_plan_sha256": head["accepted_plan_sha256"] if head else None,
            "production_settings_sha256": snap.get("production_settings_sha256"),
            "candidate_build_ids": page_candidates,
            "accepted_plan_matches_prepared": (bool(head["accepted_plan_matches_prepared"]) if head else None),
            "current_outputs_stale": bool(head and (head["accepted_snapshot_id"] != snapshot_id or not head["accepted_plan_matches_prepared"])),
            "source_status": source_status,
            "chunks": page_chunks, "takes": page_takes, "returned_texts": returned_texts,
            "has_more": next_offset < len(metadata),
            "next_cursor": _cursor(view, next_offset) if next_offset < len(metadata) else None,
        }

    def find_chunk(self, request: dto.FindChunkRequest) -> dict[str, Any]:
        state = self.discover_state()
        matches: list[dict[str, Any]] = []
        searched_snapshots: list[str] = []
        query_value = request.query.model_dump(mode="json", exclude_unset=True)
        if isinstance(request.query, dto.TimestampQuery):
            build = state.build(request.query.build_id) if state is not None else None
            if build is None:
                raise BookServiceError("file_not_found", "The requested immutable build does not exist.")
            result = build.get("result", {})
            timeline_path = result.get("timeline_filepath")
            if not isinstance(timeline_path, str):
                raise BookServiceError("state_unavailable", "The build lacks a durable timeline reference.")
            try:
                timeline = json.loads(_path(self.root, timeline_path).read_text(encoding="utf-8"))
                rate = int(timeline["sample_rate_hz"])
                frame = int(request.query.seconds * rate)
                entries = timeline["entries"]
            except (BookServiceError, OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
                raise BookServiceError("state_unavailable", "The immutable build timeline could not be read.") from exc
            version = canonical_json_sha256({"build_id": request.query.build_id, "timeline": timeline})
            if frame >= int(timeline["frame_count"]):
                raise BookServiceError("past_end", "The timestamp is at or after the immutable timeline end.")
            for position, entry in enumerate(entries):
                start_frame, end_frame = int(entry["start_frame"]), int(entry["end_frame"])
                if start_frame <= frame < end_frame:
                    source_id = entry["source_id"]
                    chapter_id = build.get("chapter_id") or source_id
                    if "chapter_id" in request.model_fields_set and request.chapter_id != chapter_id:
                        continue
                    source_ids = [source_id]
                    if entry["kind"] == "silence":
                        source_ids = [candidate["source_id"] for candidate in (
                            entries[max(0, position - 1):position] + entries[position + 1:position + 2]
                        ) if candidate.get("kind") == "audio"]
                    take_ids: list[str] = []
                    if build.get("scope") == "chapter":
                        for take_id in build.get("input_take_ids", []):
                            take = state.take(take_id) if state else None
                            if take is not None and take.get("chunk_id") in source_ids:
                                take_ids.append(take_id)
                    matches.append({
                        "chapter_id": chapter_id, "snapshot_id": (
                            build.get("snapshot_id") if build.get("scope") == "chapter" else None
                        ) or "book-build",
                        "chunk_ids": source_ids if build.get("scope") == "chapter" else [],
                        "occurrence_start": None, "occurrence_end": None,
                        "coordinate_projection": "timeline", "excerpt": f"{entry['kind']}:{source_id}",
                        "matched_build_id": request.query.build_id, "matched_take_ids": take_ids,
                        "segment_kind": "silence" if entry["kind"] == "silence" else "speech",
                        "current_chunk_ids": source_ids if take_ids else [],
                        "current_take_ids": take_ids, "lineage": [], "current_mapping_status": "not_checked",
                        "match_mode": "timestamp",
                    })
        else:
            if "chapter_id" not in request.model_fields_set:
                raise BookServiceError("validation_failed", "Quote lookup requires an explicit chapter ID.")
            chapter_ids = [request.chapter_id]
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
                    raise BookServiceError("not_prepared", "The chapter has no prepared snapshot to search.")
                stored = state.snapshot(snapshot_id)
                if not stored or stored["chapter_id"] != chapter_id:
                    raise BookServiceError("snapshot_not_found", "The requested snapshot is unavailable for this chapter.")
                self._authorize_snapshot_read(stored, chapter_id)
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
                        search_scope_key = stored["scope_key"]
                        historical_builds = state.builds(chapter_id=chapter_id, scope_key=search_scope_key)
                        matched = next((build for build in historical_builds
                                        if build.get("snapshot_id") == snapshot_id and build.get("was_accepted")), None)
                        matched_build_id = matched.get("build_id") if matched else None
                        current_namespace = state.namespace(chapter_id, scope_key) if state else None
                        current_snapshot = current_namespace.get("current_snapshot_id") if current_namespace else None
                        current_stored = state.snapshot(current_snapshot) if current_snapshot else None
                        current_chunks = current_stored["payload"].get("result", {}).get("chunks", []) if current_stored else []
                        lineage = []
                        current_ids: list[str] = []
                        for old_id in overlapping:
                            mapped = [item["chunk_id"] for item in current_chunks if (
                                item.get("chunk_id") == old_id or old_id in item.get("replaces_chunk_ids", [])
                            )]
                            if mapped != [old_id]:
                                lineage.append({"old_chunk_id": old_id, "current_chunk_ids": mapped})
                            current_ids.extend(mapped)
                        current_ids = list(dict.fromkeys(current_ids))
                        current_takes = state.takes(chapter_id=chapter_id, snapshot_id=current_snapshot) if current_snapshot else []
                        matching_takes = [take["take_id"] for take in current_takes
                                          if take.get("chunk_id") in current_ids]
                        mapping_status = ("present" if all(len(item["current_chunk_ids"]) == 1 for item in lineage)
                                          and current_ids else "missing")
                        if any(len(item["current_chunk_ids"]) > 1 for item in lineage):
                            mapping_status = "ambiguous"
                        matches.append({
                            "chapter_id": chapter_id, "snapshot_id": snapshot_id,
                            "chunk_ids": overlapping, "occurrence_start": at,
                            "occurrence_end": end, "coordinate_projection": "spoken_text_codepoints",
                            "excerpt": text[max(0, at-80):min(len(text), end+80)],
                            "matched_build_id": matched_build_id, "matched_take_ids": matching_takes,
                            "segment_kind": "speech", "current_chunk_ids": current_ids,
                            "current_take_ids": matching_takes, "lineage": lineage,
                            "current_mapping_status": mapping_status, "match_mode": "literal",
                        })
                    start = at + max(1, len(query))
        if not isinstance(request.query, dto.TimestampQuery):
            # Quote cursors are pinned to one immutable projection.  Returning
            # its actual ID lets callers distinguish history from current Word.
            version = searched_snapshots[0]
        cursor_view = canonical_json_sha256({
            "version": version, "query": query_value,
            "chapter_ids": chapter_ids if not isinstance(request.query, dto.TimestampQuery) else [],
            "snapshot_ids": searched_snapshots,
        })
        limit = request.limit if "limit" in request.model_fields_set else 50
        offset = _cursor_offset(request.cursor, cursor_view) if "cursor" in request.model_fields_set else 0
        page = matches[offset:offset+limit]
        next_offset = offset + len(page)
        return {
            "matches": page, "ambiguous": len(matches) > 1, "searched_version": version,
            "has_more": next_offset < len(matches),
            "next_cursor": _cursor(cursor_view, next_offset) if next_offset < len(matches) else None,
        }

    def list_files(self, path: str, *, recursive: bool = False, cursor: str | None = None,
                   limit: int = 100, effective_index=None,
                   effective_read_only: Callable[[str], bool] | None = None,
                   index_state: Callable[[str], tuple[str, dict[str, str] | None]] | None = None,
                   ) -> dict[str, Any]:
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
            effective_read_only=effective_read_only,
            index_state=index_state,
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
        """Read durable generation evidence and frozen prompt pages only."""
        state = self._state_required()
        if isinstance(request.query, dto.RecordGenerationQuery):
            records = state.generations(generation_record_id=request.query.generation_record_id)
        else:
            self._chapter(self._enabled_layout()[2], request.query.chapter_id)
            records = state.generations(chapter_id=request.query.chapter_id)
            if "states" in request.query.model_fields_set:
                records = [item for item in records if item["state"] in request.query.states]
        view = canonical_json_sha256({
            "query": _data(request.query),
            "records": [(record["generation_record_id"], record["generation_revision"])
                        for record in records],
        })
        limit = request.limit if "limit" in request.model_fields_set else 50
        include_prompt = "include_prompt" in request.model_fields_set and request.include_prompt
        try:
            offset, prompt_offset = (
                parse_read_cursor(request.cursor, view)
                if "cursor" in request.model_fields_set else (0, 0)
            )
        except ReadCursorError as exc:
            raise BookServiceError("invalid_cursor", "The generation cursor is invalid or stale.") from exc
        if offset > len(records) or (not include_prompt and prompt_offset):
            raise BookServiceError("invalid_cursor", "The generation cursor is invalid or stale.")

        page: list[dict[str, Any]] = []
        prompts: list[dict[str, Any]] = []
        remaining = 40_000
        index = offset
        next_prompt_offset = 0
        while index < len(records) and len(page) < limit:
            record = records[index]
            page.append(record)
            if include_prompt:
                stored = state.snapshot(record["snapshot_id"])
                chunk = (next((item for item in stored["payload"].get("result", {}).get("chunks", [])
                               if item.get("chunk_id") == record["chunk_id"]), None)
                         if stored is not None else None)
                if chunk is not None:
                    prompt = stored["payload"].get("speech_text", "")
                    prompt = prompt[chunk["start"]:chunk["end"]]
                    start = prompt_offset if index == offset else 0
                    if start > len(prompt):
                        raise BookServiceError("invalid_cursor", "The generation cursor is invalid or stale.")
                    if remaining == 0:
                        page.pop()
                        break
                    item = text_page(prompt, start, remaining)
                    prompts.append({"generation_record_id": record["generation_record_id"], "prompt": item})
                    consumed = int(item["returned_end"]) - start
                    remaining -= consumed
                    if int(item["returned_end"]) < len(prompt):
                        next_prompt_offset = int(item["returned_end"])
                        break
            index += 1
            prompt_offset = 0
            if include_prompt and remaining == 0:
                break
        if next_prompt_offset:
            next_index = index
        else:
            next_index = index
        has_more = next_index < len(records)
        return {
            "generations": page, "prompts": prompts, "has_more": has_more,
            "next_cursor": (read_cursor(view, next_index, next_prompt_offset) if has_more else None),
        }

    @staticmethod
    def _metadata_contains(value: object, expected: str) -> bool:
        if isinstance(value, str):
            return value == expected
        if isinstance(value, dict):
            return any(BookService._metadata_contains(item, expected) for item in value.values())
        if isinstance(value, list):
            return any(BookService._metadata_contains(item, expected) for item in value)
        return False

    def _import_paths(self, layout, chapter, job_id: str, take_id: str, *, media_kind: str) -> tuple[Path, Path, str]:
        staging_root = self.root / STATE_ROOT / "audiobook-staging"
        staging_root.mkdir(mode=0o700, exist_ok=True)
        staging = staging_root / f"{job_id}.part"
        extensions = {"raw_pcm": "pcm", "headered_pcm": "wav", "mp3": "mp3"}
        try:
            extension = extensions[media_kind]
        except KeyError as exc:
            raise BookServiceError("unsupported_media", "The imported media kind is not supported.") from exc
        relative = f"{chapter.audio_root}/takes/{take_id}/native.{extension}"
        target = _path(self.root, relative, allow_missing=True)
        return staging, target, relative

    def _available_import_space(self, layout, chapter, source_size: int) -> None:
        audio_root = _path(self.root, layout.shared_paths.book_audio_root, allow_missing=True)
        usage = shutil.disk_usage(audio_root if audio_root.exists() else self.root)
        retained_bytes = 0
        if audio_root.exists():
            for current, directories, filenames in os.walk(audio_root, followlinks=False):
                current_path = Path(current)
                directories[:] = [name for name in directories if not (current_path / name).is_symlink()]
                for name in filenames:
                    candidate = current_path / name
                    try:
                        facts = candidate.lstat()
                    except OSError as exc:
                        raise BookServiceError("source_unavailable", "Existing audiobook storage could not be inventoried.") from exc
                    if stat.S_ISLNK(facts.st_mode):
                        continue
                    if stat.S_ISREG(facts.st_mode):
                        retained_bytes += facts.st_size
        # The final take retains native PCM and an exact WAVE wrapper. Before
        # publication, staging additionally holds a raw copy and canonical PCM.
        required = source_size * 3 + layout.storage.reserve_bytes
        if usage.free < required:
            raise BookServiceError("insufficient_storage", "The configured media reserve leaves insufficient free space.")
        if retained_bytes + source_size * 2 > layout.storage.quota_bytes:
            raise BookServiceError("insufficient_storage", "The import would exceed the configured audiobook media quota.")

    def import_audio(self, request: dto.ImportAudioRequest, *, owner_key: str) -> tuple[dict[str, Any], bool]:
        """Reserve a durable guarded import; worker execution is separate.

        URL and workspace bytes are intentionally not accepted until their
        respective guarded transfer boundaries are installed.  In particular,
        no signed URL is placed into a durable job payload.
        """
        state, _, layout = self._enabled_layout()
        if not isinstance(request.source, dto.ProjectAudioSource):
            raise BookServiceError("source_unavailable", "This installation currently supports authorized project-file audio imports only.")
        raw_format = request.source_format if "source_format" in request.model_fields_set else None
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
        stored_scope = generation["scope"]
        scope = json.loads(stored_scope) if isinstance(stored_scope, str) else stored_scope
        if scope.get("kind") == "production" and request.provenance != "native_generation":
            raise BookServiceError("native_pcm_required", "Production imports require verified native-generation PCM.")
        if request.provenance in {"test_mp3", "derived_audio"} and scope.get("kind") != "test":
            raise BookServiceError("permission_denied", "Lossy and derived media are available only in an authorized test namespace.")
        if raw_format is not None and not self._metadata_contains(
            generation["provider_response_metadata"], raw_format.provider_format_evidence
        ):
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
            "source_format": _data(raw_format) if raw_format is not None else None,
            "take_id": take_id, "created_at": now,
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

    def _discard_unregistered_import_artifacts(self, job: dict[str, Any]) -> None:
        """Remove only this job's UUID-owned publication paths after a failed import.

        A succeeded job is its own commit marker.  For all other states the
        deterministic take directory is not an authority, so crash recovery
        removes it before releasing the generation reservation.
        """
        pinned = job["payload"]
        try:
            _, _, layout = self._enabled_layout()
            chapter = self._chapter(layout, pinned["chapter_id"])
            take_root = _path(self.root, f"{chapter.audio_root}/takes/{pinned['take_id']}", allow_missing=True)
            for artifact in (*take_root.glob("native.*"), take_root / "take.json"):
                artifact.unlink(missing_ok=True)
        except (BookServiceError, OSError, KeyError, TypeError):
            # The subsequent durable failure remains authoritative. Never
            # broaden a cleanup path when the registered configuration cannot
            # be read safely.
            return

    def run_import_job(
        self, job_id: str, *, before_finalize: Callable[[], None] | None = None,
        after_finalize: Callable[[], None] | None = None,
    ) -> None:
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
        published_paths: list[Path] = []
        registered = False
        finalization_started = False
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
            # The staging object is deliberately independent of its eventual
            # extension; detected facts below choose the immutable take name.
            staging, _, _ = self._import_paths(
                layout, chapter, job_id, pinned["take_id"], media_kind="raw_pcm"
            )
            self._stream_project_audio(pinned["source_filepath"], staging, pinned["expected_sha256"],
                                       source_size=source_size, job_id=job_id)
            raw_format = pinned.get("source_format")
            probe = None
            with staging.open("rb") as staged_input:
                header = staged_input.read(12)
            is_wave = len(header) >= 12 and header[8:12] == b"WAVE" and header[:4] in {b"RIFF", b"RIFX", b"RF64"}
            # Headered PCM is verified by the bounded WAVE parser. Every
            # compressed or derived candidate, including one falsely labelled
            # native_generation, must instead have actual FFprobe facts.
            if pinned["provenance"] in {"test_mp3", "derived_audio"} or (raw_format is None and not is_wave):
                _, ffprobe = self._registered_media_executables()
                probe = asyncio.run(ffprobe_json(ffprobe, staging, timeout_seconds=60.0))
            inspection = inspect_media_file(
                staging, ffprobe=probe, raw_format=raw_format,
                provider_format_evidence=None if raw_format is None else raw_format["provider_format_evidence"],
            )
            # ``native_generation`` identifies the provider-originated file;
            # it does not turn a detected compressed object into PCM.  Keep
            # that immutable original for test/audit reads, while production
            # assembly below requires verified native PCM facts.
            if pinned["provenance"] == "test_mp3" and (
                inspection.native_pcm or inspection.media.codec != "mp3" or inspection.media.encoding != "compressed"
            ):
                raise BookServiceError("media_mismatch", "test_mp3 provenance requires actual FFprobe-verified MP3 media.")
            if pinned["provenance"] == "derived_audio" and inspection.native_pcm:
                raise BookServiceError("media_mismatch", "derived_audio provenance requires detected derived media, not caller labels.")
            if inspection.native_pcm:
                media_kind = "raw_pcm" if inspection.media.container == "raw_pcm" else "headered_pcm"
            elif inspection.media.codec == "mp3" and inspection.media.encoding == "compressed":
                media_kind = "mp3"
            else:
                raise BookServiceError("unsupported_media", "Only verified PCM WAVE/RF64 or MP3 media is supported by this import.")
            staging, target, relative = self._import_paths(
                layout, chapter, job_id, pinned["take_id"], media_kind=media_kind
            )
            wrapper_staging: Path | None = None
            canonical_staging: Path | None = None
            wrapper_target: Path | None = None
            wrapper_relative: str | None = None
            wrapped = None
            if inspection.native_pcm and inspection.media.container == "raw_pcm":
                wrapper_staging = staging.with_suffix(".wav.part")
                canonical_staging = staging.with_suffix(".canonical.pcm")
                wrapper_target = target.with_suffix(".wav")
                wrapper_relative = f"{chapter.audio_root}/takes/{pinned['take_id']}/native.wav"
                raw = inspection.media.model_dump(mode="json")
                target_format = dto.ProductionTarget.model_validate({
                    "sample_rate_hz": raw["sample_rate_hz"], "channels": raw["channels"],
                    "encoding": raw["encoding"], "storage_bits": raw["storage_bits"],
                    "valid_bits": raw["valid_bits"], "mp3_bitrate_kbps": 1,
                }, strict=True)
                with canonical_staging.open("xb") as canonical_output:
                    assembled = assemble_pcm_stream(
                        [PcmSource(pinned["take_id"], staging, inspection)], [], target_format, canonical_output,
                    )
                    canonical_output.flush()
                    os.fsync(canonical_output.fileno())
                wrapped = wrap_pcm_as_wave(canonical_staging, wrapper_staging, target_format, assembled)
                canonical_staging.unlink()
            if before_finalize is not None:
                before_finalize()
                finalization_started = True
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            if target.exists() or (wrapper_target is not None and wrapper_target.exists()):
                raise BookServiceError("job_failed", "The immutable take path already exists.")
            os.replace(staging, target)
            staging = None
            published_paths.append(target)
            if wrapper_staging is not None and wrapper_target is not None:
                os.replace(wrapper_staging, wrapper_target)
                published_paths.append(wrapper_target)
            now = datetime.now(timezone.utc).isoformat()
            take = dto.TakeRecord.model_validate({
                "take_id": pinned["take_id"], "namespace": generation["scope"],
                "chapter_id": pinned["chapter_id"], "snapshot_id": pinned["snapshot_id"],
                "chunk_id": pinned["chunk_id"], "generation_record_id": pinned["generation_record_id"],
                "request_sha256": pinned["request_sha256"], "filepath": relative,
                "bytes_sha256": inspection.bytes_sha256, "size_bytes": inspection.size_bytes,
                "media": inspection.media.model_dump(mode="json"), "provenance": pinned["provenance"],
                "assembly_derivative": None if wrapped is None or wrapper_relative is None else {
                    "filepath": wrapper_relative, "bytes_sha256": wrapped.bytes_sha256,
                    "media": wrapped.media.model_dump(mode="json"),
                },
            }, strict=True).model_dump(mode="json", exclude_unset=True)
            take_fact = target.parent / "take.json"
            _write_immutable_json(take_fact, {"schema_version": 1, "take": take})
            published_paths.append(take_fact)
            completed = dict(generation)
            completed["updated_at"] = now
            completed["state"] = "completed"
            state.finish_import_success(job_id=job_id, generation_payload=completed, take_payload=take)
            registered = True
        except (BookServiceError, MediaValidationError, AssemblyError, ProcessRunnerError, OSError, ProjectStateError) as exc:
            if isinstance(exc, MediaValidationError):
                reason, message = exc.code, exc.message
            elif isinstance(exc, BookServiceError):
                reason, message = exc.reason, str(exc)
            elif isinstance(exc, ProjectStateError) and str(exc) == "cancel_requested":
                reason, message = "cancelled", "The import was cancelled before it could be finalized."
            elif isinstance(exc, ProcessRunnerError):
                reason, message = exc.code, exc.message
            else:
                reason, message = "job_failed", "The local import could not complete safely."
            if not registered:
                for artifact in published_paths:
                    try:
                        artifact.unlink(missing_ok=True)
                    except OSError:
                        pass
            state.finish_import_failure(job_id=job_id, reason=reason, message=message,
                                        cancelled=reason == "cancelled")
        finally:
            if finalization_started and after_finalize is not None:
                try:
                    after_finalize()
                except Exception:
                    # The final state has already been made durable. A failed
                    # local lock cleanup must not turn it into a false failure.
                    pass
            self._active_import_jobs.discard(job_id)
            if staging is not None:
                try:
                    staging.unlink(missing_ok=True)
                except OSError:
                    pass
            for transient in (locals().get("canonical_staging"), locals().get("wrapper_staging")):
                if isinstance(transient, Path):
                    try:
                        transient.unlink(missing_ok=True)
                    except OSError:
                        pass

    def mark_import_worker_started(self, job_id: str) -> None:
        """Record in-process ownership before the event loop yields to its worker."""
        self._active_import_jobs.add(job_id)

    def mark_import_worker_finished(self, job_id: str) -> None:
        """Clear pre-start ownership when task scheduling itself is cancelled."""
        self._active_import_jobs.discard(job_id)

    def build(self, request: dto.BuildRequest, *, owner_key: str) -> tuple[dict[str, Any], bool]:
        """Reserve a pinned chapter assembly candidate.

        Candidate assembly deliberately has no side effect on the accepted
        chapter head.  The gateway schedules ``run_build_job`` outside its
        admission lock and a later, explicit commit performs the head CAS.
        """
        if isinstance(request.input, dto.BookBuildInput):
            return self._reserve_book_build(request, owner_key=owner_key)
        if not isinstance(request.input, dto.ChapterBuildInput):
            raise BookServiceError("unsupported_build_mode", "The requested assembly input is unavailable.")
        if request.mode != "production_pcm" or not request.outputs.master:
            raise BookServiceError("unsupported_build_mode", "This checkpoint assembles retained chapter PCM masters only.")
        state, _, layout = self._enabled_layout()
        item = request.input
        chapter = self._chapter(layout, item.chapter_id)
        stored = state.snapshot(item.snapshot_id)
        if stored is None or stored["chapter_id"] != chapter.chapter_id:
            raise BookServiceError("not_prepared", "The requested frozen chapter snapshot is unavailable.")
        if stored["manifest_revision"] != item.expected_manifest_revision:
            raise BookServiceError("stale_manifest", "The requested snapshot is not at the expected manifest revision.")
        namespace = state.namespace(chapter.chapter_id, stored["scope_key"])
        if namespace is None or namespace.get("manifest_revision") != item.expected_manifest_revision:
            raise BookServiceError("stale_manifest", "The chapter manifest changed before build admission.")
        if namespace.get("head_revision") != request.expected_head_revision:
            raise BookServiceError("stale_head", "The chapter head changed before build admission.")
        snapshot = stored["payload"]
        snapshot_result = snapshot.get("result", {})
        if snapshot_result.get("request_plan_sha256") != item.request_plan_sha256:
            raise BookServiceError("request_hash_mismatch", "The requested build plan does not match the frozen snapshot.")
        scope = json.loads(stored["scope_key"])
        chunks = snapshot_result.get("chunks", [])
        if [value.chunk_id for value in item.takes] != [value.get("chunk_id") for value in chunks]:
            raise BookServiceError("coverage_incomplete", "Build takes must cover frozen chunks exactly once in frozen order.")
        source_take_ids: list[str] = []
        sources: list[PcmSource] = []
        target: dto.ProductionTarget | None = None
        for supplied, frozen in zip(item.takes, chunks, strict=True):
            take = state.take(supplied.take_id)
            if take is None or (
                take.get("chapter_id") != chapter.chapter_id or take.get("snapshot_id") != item.snapshot_id
                or take.get("chunk_id") != supplied.chunk_id or take.get("request_sha256") != supplied.request_sha256
                or frozen.get("request_sha256") != supplied.request_sha256
                or take.get("provenance") != "native_generation"
            ):
                raise BookServiceError("stale_dependency", "A selected take is not a matching native frozen chunk take.")
            media = take.get("media", {})
            if media.get("encoding") not in {"signed_integer", "float"}:
                raise BookServiceError("native_pcm_required", "Production assembly requires verified native PCM takes.")
            current_target = dto.ProductionTarget.model_validate({
                "sample_rate_hz": media.get("sample_rate_hz"), "channels": media.get("channels"),
                "encoding": media.get("encoding"), "storage_bits": media.get("storage_bits"),
                "valid_bits": media.get("valid_bits"), "mp3_bitrate_kbps": 1,
            }, strict=True)
            if target is None:
                target = current_target
            elif target.model_dump(exclude={"mp3_bitrate_kbps"}) != current_target.model_dump(exclude={"mp3_bitrate_kbps"}):
                raise BookServiceError("media_mismatch", "Selected native takes have different PCM formats.")
            source_path = _path(self.root, take["filepath"])
            inspection = inspect_media_file(source_path, raw_format={
                "container": "raw_pcm", "encoding": media["encoding"],
                "sample_rate_hz": media["sample_rate_hz"], "channels": media["channels"],
                "storage_bits": media["storage_bits"], "valid_bits": media["valid_bits"],
                "endianness": media["endianness"], "interleaving": "interleaved",
                "provider_format_evidence": "retained-native-pcm",
            }, provider_format_evidence="retained-native-pcm")
            if inspection.bytes_sha256 != take.get("bytes_sha256"):
                raise BookServiceError("stale_media", "An immutable take no longer matches its retained byte hash.")
            sources.append(PcmSource(supplied.chunk_id, source_path, inspection))
            source_take_ids.append(supplied.take_id)
        assert target is not None
        if scope.get("kind") == "production":
            configured = snapshot.get("production_target")
            if configured is None:
                raise BookServiceError("stale_settings", "The production snapshot lacks its pinned target settings.")
            configured_target = dto.ProductionTarget.model_validate(configured, strict=True)
            if "mp3_bitrate_kbps" not in request.outputs.model_fields_set or request.outputs.mp3_bitrate_kbps != configured_target.mp3_bitrate_kbps:
                raise BookServiceError("settings_mismatch", "Production builds require the configured MP3 listening download bitrate.")
            if target.model_dump(exclude={"mp3_bitrate_kbps"}) != configured_target.model_dump(exclude={"mp3_bitrate_kbps"}):
                raise BookServiceError("media_mismatch", "Native takes do not match the frozen production target.")
            target = configured_target
        args_sha256 = canonical_json_sha256(_data(request))
        pinned = {
            "chapter_id": chapter.chapter_id, "scope_key": stored["scope_key"],
            "snapshot_id": item.snapshot_id, "manifest_revision": item.expected_manifest_revision,
            "request_plan_sha256": item.request_plan_sha256, "take_ids": source_take_ids,
            "chunk_ids": [source.source_id for source in sources], "target": _data(target),
            "gaps": _data(request.gaps), "metadata": _data(request.metadata),
            "emit_mp3": scope.get("kind") == "production",
        }
        now = datetime.now(timezone.utc).isoformat()
        job_id = str(uuid.uuid4())
        try:
            disposition, result = state.reserve_build_job(
                job={"job_id": job_id, "payload": pinned, "created_at": now,
                     "pinned_inputs_sha256": canonical_json_sha256(pinned)},
                owner_key=owner_key, project=self.project_name, operation_id=request.operation_id,
                args_sha256=args_sha256,
            )
        except ProjectStateError as exc:
            if str(exc) == "operation_id_conflict":
                raise BookServiceError("operation_id_conflict", "This operation ID was used with different arguments.") from exc
            raise BookServiceError("state_unavailable", "The build reservation could not be persisted.") from exc
        return result, disposition == "replay"

    def _reserve_book_build(self, request: dto.BuildRequest, *, owner_key: str) -> tuple[dict[str, Any], bool]:
        """Pin every current accepted production chapter before book assembly."""
        if request.mode != "production_pcm" or not request.outputs.master:
            raise BookServiceError("unsupported_build_mode", "Book builds require retained production PCM masters.")
        state, _, layout = self._enabled_layout()
        item = request.input
        assert isinstance(item, dto.BookBuildInput)
        if item.book_id != layout.book_id or item.expected_layout_revision != layout.layout_revision:
            raise BookServiceError("stale_dependency", "The registered book layout changed before build admission.")
        settings = validate_production_settings(_read_bytes(self.root, layout.shared_paths.production_settings_filepath))
        target = settings.production_target
        if target is None or "mp3_bitrate_kbps" not in request.outputs.model_fields_set or request.outputs.mp3_bitrate_kbps != target.mp3_bitrate_kbps:
            raise BookServiceError("settings_mismatch", "Book builds require the configured production MP3 target.")
        head = state.book_head(layout.book_id)
        if (None if head is None else int(head["head_revision"])) != request.expected_head_revision:
            raise BookServiceError("stale_head", "The accepted book head changed before build admission.")
        if [value.chapter_id for value in item.chapters] != layout.chapter_order:
            raise BookServiceError("coverage_incomplete", "Book dependencies must cover registered chapter order exactly once.")
        production_key = dto.ProductionScope(kind="production").model_dump_json()
        inputs: list[dict[str, Any]] = []
        for dependency in item.chapters:
            chapter_head = state.chapter_head(dependency.chapter_id, production_key)
            if chapter_head is None or (
                chapter_head["accepted_build_id"] != dependency.chapter_build_id
                or int(chapter_head["head_revision"]) != dependency.chapter_head_revision
                or chapter_head["accepted_snapshot_id"] != dependency.snapshot_id
                or chapter_head["accepted_plan_sha256"] != dependency.request_plan_sha256
            ):
                raise BookServiceError("stale_dependency", "A chapter dependency is not its current accepted production head.")
            build = state.build(dependency.chapter_build_id)
            if build is None:
                raise BookServiceError("stale_dependency", "A current chapter build is unavailable.")
            output = next((value for value in build["result"]["outputs"] if value["kind"] == "pcm_master"), None)
            if output is None or output["media"].get("encoding") not in {"signed_integer", "float"}:
                raise BookServiceError("media_mismatch", "A chapter head lacks a retained native PCM master.")
            if output["media"].get("sample_rate_hz") != target.sample_rate_hz or output["media"].get("channels") != target.channels or output["media"].get("storage_bits") != target.storage_bits or output["media"].get("encoding") != target.encoding:
                raise BookServiceError("media_mismatch", "Accepted chapter masters do not match the configured book target.")
            inputs.append({"dependency": _data(dependency), "output": output})
        pinned = {"scope": "book", "book_id": layout.book_id, "layout_revision": layout.layout_revision,
                  "target": _data(target), "chapters": inputs, "gaps": _data(request.gaps),
                  "metadata": _data(request.metadata), "emit_mp3": True}
        now = datetime.now(timezone.utc).isoformat()
        try:
            disposition, result = state.reserve_build_job(
                job={"job_id": str(uuid.uuid4()), "payload": pinned, "created_at": now,
                     "pinned_inputs_sha256": canonical_json_sha256(pinned)},
                owner_key=owner_key, project=self.project_name, operation_id=request.operation_id,
                args_sha256=canonical_json_sha256(_data(request)),
            )
        except ProjectStateError as exc:
            if str(exc) == "operation_id_conflict":
                raise BookServiceError("operation_id_conflict", "This operation ID was used with different arguments.") from exc
            raise BookServiceError("state_unavailable", "The book-build reservation could not be persisted.") from exc
        return result, disposition == "replay"

    def run_build_job(self, job_id: str) -> None:
        """Assemble a single immutable PCM candidate from a durable pinned job."""
        state = self._state_required()
        claimed = state.claim_build_job(job_id)
        if claimed is None:
            return
        self._active_build_jobs.add(job_id)
        staged: list[Path] = []
        try:
            pinned = claimed["payload"]
            if pinned.get("scope") == "book":
                self._run_book_build(job_id, pinned)
                return
            _, _, layout = self._enabled_layout()
            chapter = self._chapter(layout, pinned["chapter_id"])
            target = dto.ProductionTarget.model_validate(pinned["target"], strict=True)
            takes = [state.take(take_id) for take_id in pinned["take_ids"]]
            if any(take is None for take in takes):
                raise BookServiceError("stale_dependency", "A pinned take was not retained.")
            sources: list[PcmSource] = []
            for chunk_id, take in zip(pinned["chunk_ids"], takes, strict=True):
                assert take is not None
                media = take["media"]
                source = _path(self.root, take["filepath"])
                inspection = inspect_media_file(source, raw_format={
                    "container": "raw_pcm", "encoding": media["encoding"],
                    "sample_rate_hz": media["sample_rate_hz"], "channels": media["channels"],
                    "storage_bits": media["storage_bits"], "valid_bits": media["valid_bits"],
                    "endianness": media["endianness"], "interleaving": "interleaved",
                    "provider_format_evidence": "retained-native-pcm",
                }, provider_format_evidence="retained-native-pcm")
                if inspection.bytes_sha256 != take["bytes_sha256"]:
                    raise BookServiceError("stale_media", "A pinned take changed before assembly.")
                sources.append(PcmSource(chunk_id, source, inspection))
            build_id = str(uuid.uuid4())
            build_relative = f"{chapter.audio_root}/builds/{build_id}"
            _mkdir_safe(self.root, build_relative)
            build_dir = _path(self.root, build_relative)
            pcm = build_dir / "master.pcm"
            pcm_stage = build_dir / "master.pcm.part"
            timeline = build_dir / "timeline.json"
            timeline_stage = build_dir / "timeline.json.part"
            staged.extend([pcm_stage, timeline_stage])
            with pcm_stage.open("xb") as output:
                assembled = assemble_pcm_stream(sources, [dto.SilenceGap.model_validate(g, strict=True) for g in pinned["gaps"]], target, output)
                output.flush()
                os.fsync(output.fileno())
            media = dto.MediaProperties.model_validate({
                "codec": ("pcm_f" if target.encoding == "float" else "pcm_s") + f"{target.storage_bits}le",
                "container": "raw_pcm", "sample_rate_hz": target.sample_rate_hz, "channels": target.channels,
                "encoding": target.encoding, "storage_bits": target.storage_bits, "valid_bits": target.valid_bits,
                "endianness": "little", "bitrate_bps": None, "frame_count": assembled.frame_count,
                "duration_seconds": int(assembled.frame_count) / target.sample_rate_hz,
                "canonical_sample_sha256": assembled.samples_sha256,
            }, strict=True).model_dump(mode="json")
            timeline_value = {
                "sample_rate_hz": assembled.timeline.sample_rate_hz, "channels": assembled.timeline.channels,
                "encoding": assembled.timeline.encoding, "storage_bits": assembled.timeline.storage_bits,
                "frame_count": assembled.timeline.frame_count, "entries": _data(assembled.timeline.entries),
            }
            with timeline_stage.open("x", encoding="utf-8") as output:
                json.dump(timeline_value, output, ensure_ascii=False, separators=(",", ":"))
                output.flush()
                os.fsync(output.fileno())
            os.replace(pcm_stage, pcm)
            os.replace(timeline_stage, timeline)
            staged.clear()
            output_data = {
                "kind": "pcm_master", "filepath": f"{build_relative}/master.pcm",
                "bytes_sha256": assembled.bytes_sha256, "size_bytes": assembled.sample_bytes, "media": media,
            }
            outputs = [output_data]
            if pinned.get("emit_mp3"):
                ffmpeg, ffprobe = self._registered_media_executables()
                mp3 = build_dir / "listening.mp3"
                argv = production_mp3_argv(ffmpeg, pcm, mp3, target,
                                           dto.BuildMetadata.model_validate(pinned["metadata"], strict=True))
                process = asyncio.run(run_process(argv, timeout_seconds=1800.0, cwd=build_dir))
                if process.cancelled:
                    raise BookServiceError("cancelled", "The MP3 encoder was cancelled.")
                if process.timed_out:
                    raise BookServiceError("tool_timeout", "The MP3 encoder exceeded its bounded runtime.")
                if process.returncode != 0:
                    raise BookServiceError("tool_failed", "The registered MP3 encoder failed.")
                probe = asyncio.run(ffprobe_json(ffprobe, mp3, timeout_seconds=60.0))
                encoded = inspect_media_file(mp3, ffprobe=probe)
                if encoded.media.codec != "mp3" or encoded.media.encoding != "compressed":
                    raise BookServiceError("media_mismatch", "The encoder output was not verified as MP3 audio.")
                outputs.append({
                    "kind": "mp3_download", "filepath": f"{build_relative}/listening.mp3",
                    "bytes_sha256": encoded.bytes_sha256, "size_bytes": encoded.size_bytes,
                    "media": encoded.media.model_dump(mode="json"),
                })
            recipe = canonical_json_sha256({"pinned": pinned, "timeline": timeline_value})
            result = dto.BuildResult.model_validate({
                "kind": "build", "build_id": build_id, "scope": "chapter",
                "namespace": json.loads(pinned["scope_key"]), "source_snapshot_ids": [pinned["snapshot_id"]],
                "input_take_ids": pinned["take_ids"], "chapter_dependencies": [],
                "request_plan_sha256": pinned["request_plan_sha256"], "outputs": outputs,
                "timeline_filepath": f"{build_relative}/timeline.json", "recipe_sha256": recipe,
                "validation": {"complete": True, "media_integrity": True, "coverage": True,
                               "sample_or_packet_verification": True, "errors": []},
                "needs_listening_review": True,
            }, strict=True).model_dump(mode="json", exclude_unset=True)
            _write_immutable_json(build_dir / "build.json", {"schema_version": 1, "build": result})
            state.finish_build_success(job_id=job_id, build={
                "scope": "chapter", "build_id": build_id, "chapter_id": chapter.chapter_id, "scope_key": pinned["scope_key"],
                "snapshot_id": pinned["snapshot_id"], "request_plan_sha256": pinned["request_plan_sha256"],
                "input_take_ids": pinned["take_ids"], "created_at": datetime.now(timezone.utc).isoformat(),
                "was_accepted": False, "result": result,
            })
        except (BookServiceError, MediaValidationError, AssemblyError, OSError, ProjectStateError) as exc:
            reason = exc.reason if isinstance(exc, BookServiceError) else (
                exc.code if isinstance(exc, (MediaValidationError, AssemblyError)) else (
                    "cancelled" if isinstance(exc, ProjectStateError) and str(exc) == "cancel_requested" else "job_failed"
                )
            )
            state.finish_build_failure(job_id=job_id, reason=reason,
                                       message=str(exc) if isinstance(exc, (BookServiceError, MediaValidationError, AssemblyError, ProjectStateError)) else "The local build could not complete safely.",
                                       cancelled=reason == "cancelled")
        finally:
            for path in staged:
                path.unlink(missing_ok=True)
            self._active_build_jobs.discard(job_id)

    def _registered_media_executables(self) -> tuple[Path, Path]:
        """Return the two explicitly configured local media tools for production."""
        ffmpeg, ffprobe = self.ffmpeg_executable, self.ffprobe_executable
        if ffmpeg is None or ffprobe is None:
            raise BookServiceError("media_tool_unavailable", "Production MP3 output requires configured FFmpeg and ffprobe executables.")
        for executable in (ffmpeg, ffprobe):
            try:
                facts = executable.stat()
            except OSError as exc:
                raise BookServiceError("media_tool_unavailable", "A configured media executable is unavailable.") from exc
            if not executable.is_absolute() or not stat.S_ISREG(facts.st_mode):
                raise BookServiceError("media_tool_unavailable", "Configured media executables must be regular absolute files.")
        return ffmpeg, ffprobe

    def _run_book_build(self, job_id: str, pinned: dict[str, Any]) -> None:
        """Assemble one continuous book candidate from pinned chapter heads."""
        state, _, layout = self._enabled_layout()
        target = dto.ProductionTarget.model_validate(pinned["target"], strict=True)
        build_id = str(uuid.uuid4())
        root_relative = f"{layout.shared_paths.book_audio_root}/builds/{build_id}"
        _mkdir_safe(self.root, root_relative)
        build_dir = _path(self.root, root_relative)
        pcm, pcm_stage = build_dir / "master.pcm", build_dir / "master.pcm.part"
        timeline, timeline_stage = build_dir / "timeline.json", build_dir / "timeline.json.part"
        sources: list[PcmSource] = []
        source_snapshots: list[str] = []
        input_take_ids: list[str] = []
        dependencies = [entry["dependency"] for entry in pinned["chapters"]]
        for entry in pinned["chapters"]:
            output = entry["output"]
            media = output["media"]
            path = _path(self.root, output["filepath"])
            inspected = inspect_media_file(path, raw_format={
                "container": "raw_pcm", "encoding": media["encoding"],
                "sample_rate_hz": media["sample_rate_hz"], "channels": media["channels"],
                "storage_bits": media["storage_bits"], "valid_bits": media["valid_bits"],
                "endianness": media["endianness"], "interleaving": "interleaved",
                "provider_format_evidence": "retained-book-master",
            }, provider_format_evidence="retained-book-master")
            if inspected.bytes_sha256 != output["bytes_sha256"]:
                raise BookServiceError("stale_media", "A pinned chapter PCM master changed before book assembly.")
            dependency = entry["dependency"]
            sources.append(PcmSource(dependency["chapter_id"], path, inspected))
            source_snapshots.append(dependency["snapshot_id"])
            chapter_build = state.build(dependency["chapter_build_id"])
            if chapter_build is not None:
                input_take_ids.extend(chapter_build.get("input_take_ids", []))
        with pcm_stage.open("xb") as output:
            assembled = assemble_pcm_stream(
                sources, [dto.SilenceGap.model_validate(item, strict=True) for item in pinned["gaps"]], target, output,
            )
            output.flush()
            os.fsync(output.fileno())
        media = dto.MediaProperties.model_validate({
            "codec": ("pcm_f" if target.encoding == "float" else "pcm_s") + f"{target.storage_bits}le",
            "container": "raw_pcm", "sample_rate_hz": target.sample_rate_hz, "channels": target.channels,
            "encoding": target.encoding, "storage_bits": target.storage_bits, "valid_bits": target.valid_bits,
            "endianness": "little", "bitrate_bps": None, "frame_count": assembled.frame_count,
            "duration_seconds": int(assembled.frame_count) / target.sample_rate_hz,
            "canonical_sample_sha256": assembled.samples_sha256,
        }, strict=True).model_dump(mode="json")
        timeline_value = {"sample_rate_hz": assembled.timeline.sample_rate_hz,
                          "channels": assembled.timeline.channels, "encoding": assembled.timeline.encoding,
                          "storage_bits": assembled.timeline.storage_bits, "frame_count": assembled.timeline.frame_count,
                          "entries": _data(assembled.timeline.entries)}
        with timeline_stage.open("x", encoding="utf-8") as output:
            json.dump(timeline_value, output, ensure_ascii=False, separators=(",", ":"))
            output.flush()
            os.fsync(output.fileno())
        os.replace(pcm_stage, pcm)
        os.replace(timeline_stage, timeline)
        outputs = [{"kind": "pcm_master", "filepath": f"{root_relative}/master.pcm",
                    "bytes_sha256": assembled.bytes_sha256, "size_bytes": assembled.sample_bytes, "media": media}]
        ffmpeg, ffprobe = self._registered_media_executables()
        mp3 = build_dir / "listening.mp3"
        process = asyncio.run(run_process(
            production_mp3_argv(ffmpeg, pcm, mp3, target,
                                dto.BuildMetadata.model_validate(pinned["metadata"], strict=True)),
            timeout_seconds=1800.0, cwd=build_dir,
        ))
        if process.cancelled or process.timed_out or process.returncode != 0:
            raise BookServiceError("tool_failed", "The registered book MP3 encoder did not complete successfully.")
        encoded = inspect_media_file(mp3, ffprobe=asyncio.run(ffprobe_json(ffprobe, mp3, timeout_seconds=60.0)))
        if encoded.media.codec != "mp3" or encoded.media.encoding != "compressed":
            raise BookServiceError("media_mismatch", "The book encoder output was not verified as MP3 audio.")
        outputs.append({"kind": "mp3_download", "filepath": f"{root_relative}/listening.mp3",
                        "bytes_sha256": encoded.bytes_sha256, "size_bytes": encoded.size_bytes,
                        "media": encoded.media.model_dump(mode="json")})
        recipe = canonical_json_sha256({"pinned": pinned, "timeline": timeline_value})
        result = dto.BuildResult.model_validate({
            "kind": "build", "build_id": build_id, "scope": "book", "namespace": {"kind": "production"},
            "source_snapshot_ids": source_snapshots, "input_take_ids": input_take_ids,
            "chapter_dependencies": dependencies, "request_plan_sha256": None, "outputs": outputs,
            "timeline_filepath": f"{root_relative}/timeline.json", "recipe_sha256": recipe,
            "validation": {"complete": True, "media_integrity": True, "coverage": True,
                           "sample_or_packet_verification": True, "errors": []},
            "needs_listening_review": True,
        }, strict=True).model_dump(mode="json", exclude_unset=True)
        _write_immutable_json(build_dir / "build.json", {"schema_version": 1, "build": result})
        state.finish_build_success(job_id=job_id, build={
            "scope": "book", "build_id": build_id, "book_id": layout.book_id,
            "created_at": datetime.now(timezone.utc).isoformat(), "was_accepted": False,
            "dependencies": dependencies, "result": result,
        })

    def commit_build(self, request: dto.CommitBuildRequest, *, owner_key: str) -> tuple[dict[str, Any], bool]:
        state, _, layout = self._enabled_layout()
        args_sha256 = canonical_json_sha256(_data(request))
        prior = state.receipt(owner_key=owner_key, project=self.project_name,
                              tool="audiobook_commit_build", operation_id=request.operation_id)
        if prior is not None:
            if prior[0] != args_sha256:
                raise BookServiceError("operation_id_conflict", "This operation ID was used with different arguments.")
            return prior[1], True
        build = state.build(request.build_id)
        if build is None:
            raise BookServiceError("file_not_found", "The requested build candidate does not exist.")
        if build.get("scope") == "book":
            return self._commit_book_build(request, state, layout, build, owner_key, args_sha256)
        result = build["result"]
        validation = result["validation"]
        if not all(validation[key] for key in ("complete", "media_integrity", "coverage", "sample_or_packet_verification")):
            raise BookServiceError("validation_failed", "The candidate did not pass its durable assembly checks.")
        chapter = self._chapter(layout, build["chapter_id"])
        stored = state.snapshot(build["snapshot_id"])
        if stored is None:
            raise BookServiceError("stale_dependency", "The build snapshot is unavailable.")
        snap_result = stored["payload"].get("result", {})
        namespace = state.namespace(chapter.chapter_id, build["scope_key"])
        current_plan = namespace.get("current_plan_sha256") if namespace else None
        current_snapshot = namespace.get("current_snapshot_id") if namespace else None
        plan_matches = current_snapshot == build["snapshot_id"] and current_plan == build["request_plan_sha256"]
        if request.intent == "accept_candidate" and not plan_matches:
            raise BookServiceError("stale_dependency", "The candidate no longer matches the current prepared chapter plan.")
        # A rollback selects prior acceptance only when its frozen prose still
        # equals current registered prose; it never rewrites working documents.
        prose = _read_bytes(self.root, chapter.working_filepath)
        tagged = _read_bytes(self.root, chapter.tagged_filepath)
        projected = project_docx_pair(prose, tagged)
        if projected.prose_projection_sha256 != snap_result.get("prose_projection_sha256"):
            raise BookServiceError("stale_source", "The working prose no longer matches this build snapshot.")
        scope = json.loads(build["scope_key"])
        if scope.get("kind") == "test":
            auth = next((entry for entry in layout.test_authorizations
                         if entry.authorization_id == scope.get("authorization_id")), None)
            now = datetime.now(timezone.utc)
            if auth is None or auth.revoked or auth.chapter_id != chapter.chapter_id or auth.source_raw_sha256 != hashlib.sha256(prose).hexdigest() or now >= datetime.fromisoformat(auth.expires_at.replace("Z", "+00:00")):
                raise BookServiceError("test_scope_not_authorized", "The test authorization is no longer active for this source.")
        elif scope.get("kind") == "production":
            state_bytes = _read_bytes(self.root, chapter.chapter_state_filepath)
            chapter_state = validate_chapter_state(state_bytes)
            if chapter_state.editorial_status != "approved" or chapter_state.approved_prose_projection_sha256 != projected.prose_projection_sha256:
                raise BookServiceError("chapter_not_approved", "Current production prose is no longer approved.")
            expected_settings = stored["payload"].get("production_settings_sha256")
            settings_bytes = _read_bytes(self.root, layout.shared_paths.production_settings_filepath)
            if expected_settings is None or hashlib.sha256(settings_bytes).hexdigest() != expected_settings:
                raise BookServiceError("stale_settings", "Production settings changed after the candidate was prepared.")
        exports = [{key: output[key] for key in ("kind", "filepath", "bytes_sha256")} for output in result["outputs"]]
        try:
            disposition, committed = state.commit_chapter_build(
                build_id=request.build_id, chapter_id=chapter.chapter_id, scope_key=build["scope_key"],
                expected_head_revision=request.expected_head_revision, intent=request.intent,
                plan_matches_prepared=plan_matches, owner_key=owner_key, project=self.project_name,
                operation_id=request.operation_id, args_sha256=args_sha256,
                result={"accepted_build_id": request.build_id, "head_revision": 1, "previous_build_id": None,
                        "accepted_plan_matches_prepared": plan_matches, "exports": exports,
                        "dependent_book_ids_marked_stale": [], "rollback_available": True},
            )
        except ProjectStateError as exc:
            reason = {"stale_head": "stale_head", "build_not_found": "file_not_found",
                      "rollback_not_accepted": "stale_dependency"}.get(str(exc), "state_unavailable")
            raise BookServiceError(reason, "The chapter build could not be committed.") from exc
        return committed, disposition == "replay"

    def _commit_book_build(self, request: dto.CommitBuildRequest, state: ProjectState,
                           layout: BookLayout, build: dict[str, Any], owner_key: str,
                           args_sha256: str) -> tuple[dict[str, Any], bool]:
        if build.get("book_id") != layout.book_id:
            raise BookServiceError("stale_dependency", "The candidate belongs to a different book layout.")
        if request.intent == "rollback" and not build.get("was_accepted"):
            raise BookServiceError("stale_dependency", "Only an earlier accepted book build can be restored.")
        production_key = dto.ProductionScope(kind="production").model_dump_json()
        for dependency in build.get("dependencies", []):
            current = state.chapter_head(dependency["chapter_id"], production_key)
            if current is None or current["accepted_build_id"] != dependency["chapter_build_id"] or int(current["head_revision"]) != dependency["chapter_head_revision"]:
                raise BookServiceError("stale_dependency", "A chapter head changed after this book candidate was assembled.")
        exports = [{key: output[key] for key in ("kind", "filepath", "bytes_sha256")}
                   for output in build["result"]["outputs"]]
        try:
            disposition, committed = state.commit_book_build(
                build_id=request.build_id, book_id=layout.book_id,
                expected_head_revision=request.expected_head_revision,
                owner_key=owner_key, project=self.project_name, operation_id=request.operation_id,
                args_sha256=args_sha256,
                result={"accepted_build_id": request.build_id, "head_revision": 1, "previous_build_id": None,
                        "accepted_plan_matches_prepared": True, "exports": exports,
                        "dependent_book_ids_marked_stale": [], "rollback_available": True},
            )
        except ProjectStateError as exc:
            raise BookServiceError("stale_head" if str(exc) == "stale_head" else "state_unavailable",
                                   "The book build could not be committed.") from exc
        return committed, disposition == "replay"

    def get_book(self, request: dto.GetBookRequest) -> dict[str, Any]:
        state, _, layout = self._enabled_layout()
        if request.book_id != layout.book_id:
            raise BookServiceError("file_not_found", "The requested book is not registered.")
        production_key = dto.ProductionScope(kind="production").model_dump_json()
        dependencies: list[dict[str, Any]] = []
        not_ready: list[dict[str, Any]] = []
        for chapter_id in layout.chapter_order:
            head = state.chapter_head(chapter_id, production_key)
            if head is None:
                not_ready.append({"chapter_id": chapter_id, "reason": "no_accepted_production_head"})
                continue
            accepted_chapter = state.build(head["accepted_build_id"])
            outputs = (accepted_chapter or {}).get("result", {}).get("outputs", [])
            if not outputs or any(item.get("media", {}).get("encoding") == "compressed" for item in outputs):
                not_ready.append({"chapter_id": chapter_id, "reason": "ineligible_accepted_production_head"})
                continue
            dependencies.append({"chapter_id": chapter_id, "chapter_build_id": head["accepted_build_id"],
                                 "chapter_head_revision": int(head["head_revision"]),
                                 "snapshot_id": head["accepted_snapshot_id"],
                                 "request_plan_sha256": head["accepted_plan_sha256"]})
        head = state.book_head(layout.book_id)
        accepted = state.build(head["accepted_build_id"]) if head else None
        candidates = state.book_builds(layout.book_id)
        accepted_dependencies = accepted.get("dependencies", []) if accepted else []
        metadata: list[tuple[str, Any]] = [
            *(("chapter_order", chapter_id) for chapter_id in layout.chapter_order),
            *(("current_dependency", dependency) for dependency in dependencies),
            *(("accepted_dependency", dependency) for dependency in accepted_dependencies),
            *(("not_ready", value) for value in not_ready),
            *(("candidate", value["build_id"]) for value in candidates),
        ]
        limit = request.limit if "limit" in request.model_fields_set else 100
        view = canonical_json_sha256({"book": layout.book_id, "layout_revision": layout.layout_revision,
                                      "head": head["head_revision"] if head else None,
                                      "metadata": [(kind, value if isinstance(value, str) else value.get("chapter_id", value.get("build_id")))
                                                   for kind, value in metadata]})
        offset = _cursor_offset(request.cursor, view) if "cursor" in request.model_fields_set else 0
        if offset > len(metadata):
            raise BookServiceError("invalid_cursor", "The book cursor is invalid or stale.")
        page = metadata[offset:offset + limit]
        next_offset = offset + len(page)
        return {
            "book_id": layout.book_id, "layout_revision": layout.layout_revision,
            "head_revision": int(head["head_revision"]) if head else None,
            "accepted_build_id": head["accepted_build_id"] if head else None,
            "candidate_build_ids": [value for kind, value in page if kind == "candidate"],
            "current_outputs_stale": bool(head and accepted_dependencies != dependencies),
            "accepted_plan_matches_prepared": bool(head["accepted_plan_matches_prepared"]) if head else None,
            "chapter_order": [value for kind, value in page if kind == "chapter_order"],
            "current_chapter_dependencies": [value for kind, value in page if kind == "current_dependency"],
            "accepted_book_dependencies": [value for kind, value in page if kind == "accepted_dependency"],
            "chapters_not_ready": [value for kind, value in page if kind == "not_ready"],
            "exports": accepted["result"]["outputs"] if accepted else [],
            "has_more": next_offset < len(metadata),
            "next_cursor": _cursor(view, next_offset) if next_offset < len(metadata) else None,
        }

    def get_job(self, request: dto.GetJobRequest) -> dict[str, Any]:
        state = self._state_required()
        job = state.import_job(request.job_id)
        is_build = False
        if job is None:
            job = state.build_job(request.job_id)
            is_build = job is not None
        if job is None:
            raise BookServiceError("file_not_found", "The durable job does not exist.")
        active = self._active_build_jobs if is_build else self._active_import_jobs
        if job["state"] in {"queued", "running"} and request.job_id not in active:
            # A fresh service instance proves no worker survived restart.  Do
            # not silently repeat local work or reuse a transient source.
            if is_build:
                state.finish_build_failure(job_id=request.job_id, reason="job_failed",
                                           message="The unfinished build was interrupted by restart.")
                job = state.build_job(request.job_id)
            else:
                self._discard_unregistered_import_artifacts(job)
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
            if state.import_job(request.job_id) is not None:
                disposition, result = state.request_import_cancellation(
                    job_id=request.job_id, expected_job_revision=request.expected_job_revision,
                    owner_key=owner_key, project=self.project_name, operation_id=request.operation_id,
                    args_sha256=args_sha256,
                )
            else:
                disposition, result = state.request_build_cancellation(
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


def _write_immutable_json(path: Path, value: dict[str, Any]) -> None:
    """Create one durable fact file without making it a second authority."""
    try:
        with path.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
    except FileExistsError as exc:
        raise BookServiceError("publication_conflict", "The immutable media fact path already exists.") from exc
    except OSError as exc:
        raise BookServiceError("publication_failed", "The immutable media fact could not be published.") from exc
