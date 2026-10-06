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
import logging
import os
import shutil
import stat
import uuid
from urllib.parse import parse_qsl, urlsplit
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Callable

from pydantic import ValidationError

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
from .sources import StagedAudioSource
from .assembly import (AssemblyError, Mp3Source, PcmSource, assemble_pcm_stream, wrap_pcm_as_wave,
                       production_mp3_argv, build_test_mp3_stream_copy_argv, verify_mp3_packet_copy,
                       write_ffconcat_manifest, plan_pcm_timeline)
from .jobs import ProcessRunnerError, ffprobe_json, ffprobe_packet_facts, run_process
from .mp3_validation import verify_chapter_mp3_decoder
from .projection import ProjectionError, project_docx_pair, validate_chunk_ranges
from .docx import (
    BookmarkLocation, BookmarkPlacement, FileLockedError, add_bookmarks,
    PROJECTION_VERSION, DocxProjectionError, parse_docx, require_unlocked,
)
from .read_helpers import (
    ReadCursorError, paired_text_page, parse_read_cursor, read_cursor,
    spoken_coordinates, spoken_interval, text_page,
)
from .state import ProjectState, ProjectStateError
from .storage import ProjectFileError, list_project_files, read_project_file
from ..parsing import compute_doc_id

VIEW_TTL = timedelta(hours=24)
MAX_DOCX_BYTES = 256 * 1024 * 1024
log = logging.getLogger("cognita.books")


class BookServiceError(ValueError):
    def __init__(self, reason: str, message: str, *, outcome: str = "not_applied"):
        super().__init__(message)
        self.reason = reason
        self.outcome = outcome


def _provider_fact_key(value: str) -> str:
    """Normalize conventional JSON/query key separators without matching substrings."""
    return "".join(character for character in value.casefold() if character.isalnum())


_SECRET_KEYS = frozenset(map(_provider_fact_key, {
    "authorization", "proxy_authorization", "cookie", "set_cookie", "x_api_key",
    "api_key", "apikey", "access_token", "refresh_token", "client_secret",
}))
_URL_KEYS = frozenset(map(_provider_fact_key, {
    "url", "uri", "endpoint",
    "download_url", "download_uri", "signed_url", "signed_uri",
    "signed_download_url", "signed_download_uri",
    "presigned_url", "presigned_uri",
    "presigned_download_url", "presigned_download_uri",
}))
_LITERAL_TEXT_KEYS = frozenset(map(_provider_fact_key, {
    "prompt", "previous_text", "next_text",
}))
_SIGNED_QUERY_KEYS = _SECRET_KEYS | frozenset(map(_provider_fact_key, {
    "signature", "sig", "token",
    "x_amz_signature", "x_amz_credential", "x_amz_security_token",
    "x_goog_signature", "x_goog_credential", "x_goog_security_token",
}))


def _validate_durable_provider_facts(value: Any, *, key: str | None = None) -> None:
    """Reject explicit transport credentials before provider facts become durable."""
    normalized = _provider_fact_key(key or "")
    if normalized in _SECRET_KEYS:
        raise BookServiceError("validation_failed", "Provider evidence cannot contain transport credentials.")
    if isinstance(value, dict):
        for child_key, child in value.items():
            _validate_durable_provider_facts(child, key=str(child_key))
        return
    if isinstance(value, list):
        for child in value:
            _validate_durable_provider_facts(child, key=key)
        return
    if isinstance(value, str) and normalized not in _LITERAL_TEXT_KEYS:
        # Provider JSON may put a transport URL under any key or in an array.
        # Recognize whole HTTP URLs, without scanning literal prose for links.
        candidate = value.strip()
        whole_url = (candidate.casefold().startswith(("http://", "https://"))
                     and not any(character.isspace() for character in candidate))
        if normalized not in _URL_KEYS and not whole_url:
            return
        try:
            parsed = urlsplit(candidate)
        except ValueError as exc:
            raise BookServiceError("validation_failed", "Provider evidence contains an invalid transport URL.") from exc
        if parsed.username is not None or parsed.password is not None:
            raise BookServiceError("validation_failed", "Provider evidence cannot contain credential URLs.")
        if any(_provider_fact_key(name) in _SIGNED_QUERY_KEYS
               for name, _item in parse_qsl(parsed.query, keep_blank_values=True)):
            raise BookServiceError("validation_failed", "Provider evidence cannot contain signed transport URLs.")


def _validate_request_spec_durable_fields(spec: dto.RequestSpec) -> None:
    """Check the JSON fields frozen with a generation request before persistence."""
    _validate_durable_provider_facts(_data(spec.parameters), key="parameters")
    _validate_durable_provider_facts(_data(spec.context_fields), key="context_fields")


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


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def _require_unlocked(path: Path) -> None:
    try:
        require_unlocked(path)
    except FileLockedError as exc:
        raise BookServiceError("file_locked", str(exc)) from exc


def _cursor(view_id: str, offset: int) -> str:
    raw = json.dumps({"v": 1, "view": view_id, "offset": offset}, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _cursor_view_and_offset(cursor: str) -> tuple[str, int]:
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        value = json.loads(raw)
        view_id, offset = value["view"], value["offset"]
        if (value != {"v": 1, "view": view_id, "offset": offset}
                or not isinstance(view_id, str) or not view_id
                or isinstance(offset, bool) or not isinstance(offset, int) or offset < 0):
            raise ValueError
    except (ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
        raise BookServiceError("invalid_cursor", "The text cursor is invalid or belongs to another view.") from exc
    return view_id, offset


def _cursor_offset(cursor: str, view_id: str) -> int:
    cursor_view_id, offset = _cursor_view_and_offset(cursor)
    if cursor_view_id != view_id:
        raise BookServiceError("invalid_cursor", "The text cursor is invalid or belongs to another view.")
    return offset


def _lineage_targets(
    seeds: list[str], targets: list[str], records: list[dict[str, Any]], *, allow_ancestors: bool = False,
) -> dict[str, list[str]]:
    """Follow each direction independently; an ancestor never grants a sibling."""
    ordered_targets = list(dict.fromkeys(targets))
    target_set = set(ordered_targets)
    by_id = {record["chunk_id"]: record for record in records}

    def reached(seed: str, edge: str) -> set[str]:
        pending = [seed]
        visited: set[str] = set()
        found: set[str] = set()
        while pending:
            chunk_id = pending.pop()
            if chunk_id in visited:
                continue
            visited.add(chunk_id)
            if chunk_id in target_set:
                found.add(chunk_id)
            else:
                pending.extend(by_id.get(chunk_id, {}).get(edge, []))
        return found

    mapped = {}
    for seed in dict.fromkeys(seeds):
        if seed in target_set:
            mapped[seed] = [seed]
            continue
        found = reached(seed, "replaced_by_chunk_ids")
        if allow_ancestors:
            found.update(reached(seed, "replaces_chunk_ids"))
        mapped[seed] = [target for target in ordered_targets if target in found]
    return mapped


def _bound_document_view_id(
    intrinsic_view_id: str, *, chapter_id: str, prose_filepath: str,
    tagged_filepath: str, layout_revision: int,
) -> str:
    """Bind one projection identity to the registered source context that owns it."""
    return canonical_json_sha256({
        "intrinsic_view_id": intrinsic_view_id,
        "chapter_id": chapter_id,
        "prose_filepath": prose_filepath,
        "tagged_filepath": tagged_filepath,
        "layout_revision": layout_revision,
    })


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


def _request_matches_registered_settings(spec: dto.RequestSpec, registered: dto.RequestSpec) -> bool:
    """Match registered common context while retaining exact per-chunk extras."""
    if not registered.context_fields.keys() <= spec.context_fields.keys():
        return False
    requested = _data(spec)
    requested["context_fields"] = {
        key: spec.context_fields[key] for key in registered.context_fields
    }
    # Python equality treats True as 1, including inside nested JSON. Use the
    # existing canonical JSON authority for all registered request values.
    return canonical_json_sha256(requested) == canonical_json_sha256(_data(registered))


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

    def index_captured_content(self, source_path: str, suffix: str, raw: bytes):
        """Capture registered role facts before selecting an extractor."""
        from .fingerprint import canonical_json_sha256
        config = self.config()
        if config.config_state != "enabled" or config.layout is None:
            return None, None
        registered = self._registered_role(config.layout, source_path)
        if registered is None:
            return None, None
        role, chapter, chapter_kind = registered
        raw_sha256 = hashlib.sha256(raw).hexdigest()
        values: dict[str, Any] = {
            "source_path": source_path, "raw_sha256": raw_sha256,
            "extraction_version": "legacy-file-v1", "role": role,
            "chapter_id": chapter.chapter_id if chapter is not None else None,
            "layout_sha256": config.layout_sha256, "chapter_state_sha256": None,
            "annotations_sha256": None, "approval_source_raw_sha256": None,
            "approval_prose_projection_sha256": None, "approval_projection_version": None,
            "summary_raw_sha256": None, "summary_source_raw_sha256": None,
            "summary_source_prose_projection_sha256": None,
        }
        chapter_state = None
        if chapter is not None:
            try:
                chapter_state_raw = _read_bytes(self.root, chapter.chapter_state_filepath)
                chapter_state = validate_chapter_state(chapter_state_raw)
            except (BookServiceError, ValueError, ProjectFileError) as exc:
                raise BookServiceError("validation_failed", "Chapter index configuration is unavailable.") from exc
            if chapter_state.chapter_id != chapter.chapter_id or chapter_state.layout_revision != config.layout.layout_revision:
                raise BookServiceError("stale_file", "Chapter index configuration is stale.")
            values["chapter_state_sha256"] = hashlib.sha256(chapter_state_raw).hexdigest()
            if chapter_state.index_annotations is not None:
                values["annotations_sha256"] = canonical_json_sha256(
                    chapter_state.index_annotations.model_dump(mode="json", exclude_unset=True)
                )
        context = {"values": values}
        if chapter is None or chapter_kind != "working":
            if chapter is not None and chapter_kind == "summary":
                assert chapter_state is not None
                summary = chapter_state.summary
                if (summary is None or not summary.approved or summary.filepath != source_path
                        or summary.summary_raw_sha256 != raw_sha256):
                    raise BookServiceError("stale_file", "Chapter summary approval does not match captured source bytes.")
                prose = _read_bytes(self.root, chapter.working_filepath)
                tagged = _read_bytes(self.root, chapter.tagged_filepath)
                pair = project_docx_pair(prose, tagged)
                if (summary.source_raw_sha256 != hashlib.sha256(prose).hexdigest()
                        or summary.source_prose_projection_sha256 != pair.prose_projection_sha256):
                    raise BookServiceError("stale_file", "Chapter summary source binding is stale.")
                values["summary_raw_sha256"] = summary.summary_raw_sha256
                values["summary_source_raw_sha256"] = summary.source_raw_sha256
                values["summary_source_prose_projection_sha256"] = summary.source_prose_projection_sha256
            return None, context
        if suffix != ".docx":
            raise BookServiceError("validation_failed", "A registered chapter source must be a DOCX document.")
        assert chapter_state is not None
        content = self._annotation_filtered_text(raw, chapter_state.index_annotations)
        values["extraction_version"] = PROJECTION_VERSION
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
            raise BookServiceError("stale_file", "Chapter approval does not match captured source bytes.")
        if approval_is_current:
            values["approval_source_raw_sha256"] = approval.source_raw_sha256
            values["approval_prose_projection_sha256"] = approval.prose_projection_sha256
            values["approval_projection_version"] = approval.projection_version
        return content, context

    @staticmethod
    def _annotation_filtered_text(raw: bytes, annotations) -> str:
        """Apply only fully verified source-projection deletion spans."""
        try:
            projection = parse_docx(raw)
        except DocxProjectionError as exc:
            location = f" at {exc.location}" if exc.location else ""
            raise BookServiceError(exc.code, f"Registered chapter source is invalid{location}: {exc.message}") from exc
        if projection.unsupported:
            item = projection.unsupported[0]
            raise BookServiceError(
                "unsupported_docx_structure",
                f"Registered chapter source has unsupported narrative content at {item.part}:{item.location}: {item.detail}",
            )
        if annotations is None:
            return "\n\n".join(item.text for item in projection.paragraphs)
        raw_sha256 = hashlib.sha256(raw).hexdigest()
        if annotations.source_raw_sha256 != raw_sha256:
            raise BookServiceError("validation_failed", "Invalid index_annotations.source_raw_sha256 for captured chapter source.")
        if annotations.extraction_version != PROJECTION_VERSION:
            raise BookServiceError("validation_failed", "Invalid index_annotations.extraction_version for captured chapter source.")
        paragraphs = {item.paragraph_id: item for item in projection.paragraphs}
        spans_by_paragraph: dict[str, list[tuple[int, Any]]] = {}
        for index, span in enumerate(annotations.spans):
            location = f"index_annotations.spans[{index}]"
            paragraph = paragraphs.get(span.paragraph_id)
            if paragraph is None:
                raise BookServiceError(
                    "validation_failed",
                    f"Invalid {location}.paragraph_id: {span.paragraph_id} is unknown.",
                )
            if span.start < 0 or span.end > len(paragraph.text):
                raise BookServiceError(
                    "validation_failed",
                    f"Invalid {location}.start/end for {span.paragraph_id}:{span.start}-{span.end}.",
                )
            actual = hashlib.sha256(paragraph.text[span.start:span.end].encode("utf-8")).hexdigest()
            if actual != span.expected_text_sha256:
                raise BookServiceError(
                    "validation_failed",
                    f"Invalid {location}.expected_text_sha256 for {span.paragraph_id}:{span.start}-{span.end}.",
                )
            spans_by_paragraph.setdefault(span.paragraph_id, []).append((index, span))
        retained: list[str] = []
        for paragraph in projection.paragraphs:
            spans = sorted(
                spans_by_paragraph.get(paragraph.paragraph_id, ()),
                key=lambda item: (item[1].start, item[1].end),
            )
            previous = 0
            pieces: list[str] = []
            for index, span in spans:
                if span.start < previous:
                    raise BookServiceError(
                        "validation_failed",
                        f"Invalid index_annotations.spans[{index}] overlaps {paragraph.paragraph_id}:{span.start}-{span.end}.",
                    )
                pieces.append(paragraph.text[previous:span.start])
                previous = span.end
            pieces.append(paragraph.text[previous:])
            retained.append("".join(pieces))
        return "\n\n".join(retained)

    def capture_index_document(self, document):
        """Bind a registered document's parsed text to the exact captured inputs."""
        from .state import IndexedRoleProvenance
        from .fingerprint import canonical_json_sha256
        from .config import validate_chapter_state

        captured_context = getattr(document, "book_index_context", None)
        if captured_context is not None:
            values = dict(captured_context["values"])
            values["doc_id"] = document.doc_id
            values["extracted_sha256"] = document.content_hash
            document.book_index_record = IndexedRoleProvenance(**values)
            document.book_index_context = None
            return document
        raw = getattr(document, "captured_raw", None)
        if raw is None:
            raise BookServiceError("source_unavailable", "Indexed source bytes were not captured.")
        state = self.discover_state()
        config = load_book_config(self.root, state)
        if state is None or config.config_state != "enabled" or config.layout is None:
            return document
        registered = self._registered_role(config.layout, document.source)
        if registered is None:
            return document
        role, chapter, chapter_kind = registered
        raw_sha256 = hashlib.sha256(raw).hexdigest()
        values: dict[str, Any] = {
            "source_path": document.source, "doc_id": document.doc_id,
            "extracted_sha256": document.content_hash, "raw_sha256": raw_sha256,
            "extraction_version": "legacy-file-v1", "role": role,
            "chapter_id": chapter.chapter_id if chapter is not None else None,
            "layout_sha256": config.layout_sha256, "chapter_state_sha256": None,
            "annotations_sha256": None, "approval_source_raw_sha256": None,
            "approval_prose_projection_sha256": None, "approval_projection_version": None,
            "summary_raw_sha256": None, "summary_source_raw_sha256": None,
            "summary_source_prose_projection_sha256": None,
        }
        if chapter is not None:
            try:
                chapter_state_raw = _read_bytes(self.root, chapter.chapter_state_filepath)
                chapter_state = validate_chapter_state(chapter_state_raw)
            except (BookServiceError, ValueError, ProjectFileError) as exc:
                raise BookServiceError("validation_failed", "Chapter index configuration is unavailable.") from exc
            if chapter_state.chapter_id != chapter.chapter_id or chapter_state.layout_revision != config.layout.layout_revision:
                raise BookServiceError("stale_file", "Chapter index configuration is stale.")
            values["chapter_state_sha256"] = hashlib.sha256(chapter_state_raw).hexdigest()
            if chapter_state.index_annotations is not None:
                values["annotations_sha256"] = canonical_json_sha256(
                    chapter_state.index_annotations.model_dump(mode="json", exclude_unset=True)
                )
            if chapter_kind == "working":
                document.content = self._annotation_filtered_text(raw, chapter_state.index_annotations)
                document.content_hash = hashlib.sha256(document.content.encode()).hexdigest()
                document.doc_id = compute_doc_id(document.source, document.content_hash)
                values.update({"doc_id": document.doc_id, "extracted_sha256": document.content_hash,
                               "extraction_version": PROJECTION_VERSION})
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
                    raise BookServiceError("stale_file", "Chapter approval does not match captured source bytes.")
                if approval_is_current:
                    values["approval_source_raw_sha256"] = approval.source_raw_sha256
                    values["approval_prose_projection_sha256"] = approval.prose_projection_sha256
                    values["approval_projection_version"] = approval.projection_version
        document.book_index_record = IndexedRoleProvenance(**values)
        return document

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
                    for field, expected in (("chapter_id", chapter.chapter_id), ("layout_revision", layout.layout_revision)):
                        if getattr(chapter_state, field) != expected:
                            raise BookServiceError("stale_file", f"{field}: Chapter index configuration is stale.")
                    if role == "chapter_working":
                        # External edits have no managed-write receipt.  Consume
                        # the same captured-source validator before reporting
                        # pending/stale/indexed; never bless malformed annotations.
                        self._annotation_filtered_text(raw, chapter_state.index_annotations)
                except BookServiceError as exc:
                    source_error = {
                        "code": exc.reason,
                        "message": f"{chapter.chapter_state_filepath}: {exc}"[:512],
                    }
                except ValidationError as exc:
                    location = ""
                    for part in exc.errors(include_url=False, include_context=False, include_input=False)[0]["loc"]:
                        location += f"[{part}]" if isinstance(part, int) else ("." if location else "") + str(part)
                    source_error = {
                        "code": "validation_failed",
                        "message": f"{chapter.chapter_state_filepath}:{location or '$'}: Invalid chapter index configuration."[:512],
                    }
                    chapter_state = None
                except ValueError as exc:
                    location = f"line {exc.lineno}, column {exc.colno}" if isinstance(exc, json.JSONDecodeError) else "$"
                    source_error = {
                        "code": "validation_failed",
                        "message": f"{chapter.chapter_state_filepath}:{location}: Invalid chapter index configuration."[:512],
                    }
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

    def record_index_provenance(self, record, *legacy):
        """Persist the exact captured record only while its bindings remain current."""
        state = self.discover_state()
        if state is None:
            return None
        if isinstance(record, str):
            if len(legacy) != 4:
                raise TypeError("legacy index provenance requires complete source facts")
            doc_id, extracted_sha256, raw_sha256, extraction_version = legacy
            record = self.index_provenance_for(
                record, doc_id, extracted_sha256, raw_sha256, extraction_version,
            )
            if record is None:
                return None
        if not self.index_provenance_is_current(record):
            return None
        try:
            state.put_indexed_role_provenance(record)
        except ProjectStateError as exc:
            raise BookServiceError("state_unavailable", "Index provenance could not be persisted.") from exc
        return record if self.index_provenance_is_current(record) else None

    def index_skip_is_current(
        self, source_path: str, doc_id: str, extracted_sha256: str,
    ) -> bool | None:
        """Return currentness for a registered source, else ``None`` for legacy.

        This deliberately does not apply retrieval-profile admission.  Draft
        chapters and instruction/workflow sources still need their captured
        evidence refreshed even where a particular search profile omits them.
        """
        state = self.discover_state()
        config = load_book_config(self.root, state)
        if state is None or config.config_state != "enabled" or config.layout is None:
            return None
        if self._registered_role(config.layout, source_path) is None:
            return None
        record = state.indexed_role_provenance(source_path)
        return bool(
            record is not None
            and record.doc_id == doc_id
            and record.extracted_sha256 == extracted_sha256
            and self.index_provenance_is_current(record)
        )

    def index_move_requires_capture(self, old_source: str, new_source: str) -> bool:
        """Keep book-derived identities out of the generic vector move fast path."""
        state = self.discover_state()
        config = load_book_config(self.root, state)
        if state is None or config.config_state != "enabled" or config.layout is None:
            return False
        return (
            self._registered_role(config.layout, old_source) is not None
            or self._registered_role(config.layout, new_source) is not None
            or state.indexed_role_provenance(old_source) is not None
            or state.indexed_role_provenance(new_source) is not None
        )

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
                    profile == "canon" and record.approval_source_raw_sha256 is not None
                    and record.approval_prose_projection_sha256 is not None
                    and record.approval_projection_version is not None
                )
            elif chapter_kind == "summary":
                # index_provenance_for only returns summaries whose approved
                # source/projection binding remains current.
                allowed = profile in {"editing", "canon"}
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
        layout = config.layout
        chapter = self._chapter(layout, chapter_id) if layout is not None else None
        payload = stored.get("payload", {})
        result = payload.get("result", {}) if isinstance(payload, dict) else {}
        if chapter is None or not isinstance(result, dict):
            raise BookServiceError("state_unavailable", "The stored test snapshot is malformed.")
        prose_filepath, tagged_filepath = self._snapshot_working_pair(payload, chapter)
        expected_raw_sha256 = result.get("snapshot_prose_sha256")
        self._active_test_authorization_for_hash(
            layout, chapter, authorization_id=authorization_id,
            prose_filepath=prose_filepath, tagged_filepath=tagged_filepath,
            prose_sha256=expected_raw_sha256,
            selected_ordinals=payload.get("selected_source_ordinals"),
            failure_reason="not_authorized",
        )

    def _chapter(self, layout: BookLayout, chapter_id: str):
        for chapter in layout.chapters:
            if chapter.chapter_id == chapter_id:
                return chapter
        raise BookServiceError("chapter_not_found", "The requested chapter is not registered.")

    def _active_test_authorization(
        self, layout: BookLayout, chapter, *, authorization_id: str | None,
        prose_filepath: str, tagged_filepath: str, prose_bytes: bytes,
        selected_ordinals: list[int] | None = None,
    ):
        """Resolve one currently active test authorization for its exact source pair."""
        return self._active_test_authorization_for_hash(
            layout, chapter, authorization_id=authorization_id,
            prose_filepath=prose_filepath, tagged_filepath=tagged_filepath,
            prose_sha256=hashlib.sha256(prose_bytes).hexdigest(),
            selected_ordinals=selected_ordinals,
        )

    @staticmethod
    def _active_test_authorization_for_hash(
        layout: BookLayout, chapter, *, authorization_id: str | None,
        prose_filepath: str, tagged_filepath: str, prose_sha256: str | None,
        selected_ordinals: list[int] | None = None, failure_reason: str = "test_scope_not_authorized",
    ):
        """Resolve current authorization against captured or live raw identity."""
        now = datetime.now(timezone.utc)
        for authorization in layout.test_authorizations:
            if (authorization_id is not None and authorization.authorization_id != authorization_id) or (
                authorization.chapter_id != chapter.chapter_id
                or authorization.prose_filepath != prose_filepath
                or authorization.tagged_filepath != tagged_filepath
                or authorization.revoked
                or authorization.source_raw_sha256 != prose_sha256
            ):
                continue
            try:
                authorized_at = datetime.fromisoformat(authorization.authorized_at.replace("Z", "+00:00"))
                expires_at = datetime.fromisoformat(authorization.expires_at.replace("Z", "+00:00"))
            except ValueError:
                continue
            if (authorized_at <= now < expires_at
                    and (selected_ordinals is None
                         or set(selected_ordinals).issubset(set(authorization.allowed_paragraph_ordinals)))):
                return authorization
        raise BookServiceError(failure_reason, "The test authorization is not active for this exact source pair.")

    @staticmethod
    def _snapshot_working_pair(snapshot_payload: dict[str, Any], chapter) -> tuple[str, str]:
        """Keep selected live files distinct from immutable snapshot artifact paths."""
        return (
            snapshot_payload.get("working_prose_filepath", chapter.working_filepath),
            snapshot_payload.get("working_tagged_filepath", chapter.tagged_filepath),
        )

    def _production_facts(self, layout: BookLayout, chapter, *, require_authorization: bool = True):
        """Resolve live approval, source and canonical production settings once."""
        authorization = layout.production_authorization
        if (require_authorization and (authorization is None or authorization.revoked
                                       or not authorization.completed_book)):
            raise BookServiceError("production_not_authorized", "Production requires an active completed-book authorization.")
        try:
            prose = _read_bytes(self.root, chapter.working_filepath)
            tagged = _read_bytes(self.root, chapter.tagged_filepath)
            projected = project_docx_pair(prose, tagged)
            if projected.unsupported:
                raise BookServiceError(
                    "unsupported_docx_structure",
                    "Production cannot use registered DOCX sources with unsupported structures.",
                )
            chapter_state = validate_chapter_state(_read_bytes(self.root, chapter.chapter_state_filepath))
            if chapter_state.chapter_id != chapter.chapter_id or chapter_state.layout_revision != layout.layout_revision:
                raise BookServiceError("configuration_conflict", "Chapter editorial state is bound to another layout revision.")
            approval = chapter_state.approval_provenance
            if (chapter_state.editorial_status != "approved" or approval is None
                    or chapter_state.approved_source_raw_sha256 != approval.source_raw_sha256
                    or chapter_state.approved_prose_projection_sha256 != approval.prose_projection_sha256
                    or chapter_state.approval_projection_version != approval.projection_version
                    or approval.prose_projection_sha256 != projected.prose_projection_sha256
                    or approval.projection_version != projected.projection_version
                    or not projected.source_text_matches_without_tags):
                raise BookServiceError("chapter_not_approved", "Current source prose does not match its asserted approval.")
            settings_bytes = _read_bytes(self.root, layout.shared_paths.production_settings_filepath)
            settings = validate_production_settings(settings_bytes)
        except BookServiceError:
            raise
        except (OSError, ValueError, ProjectionError) as exc:
            raise BookServiceError("configuration_conflict", "Current source or production settings cannot be verified.") from exc
        if settings.production_target is None or settings.request_spec is None:
            raise BookServiceError("settings_mismatch", "Production settings lack a resolved request specification or media target.")
        return {
            "prose": prose, "tagged": tagged, "projection": projected,
            "chapter_state": chapter_state, "settings": settings,
            "settings_raw_sha256": hashlib.sha256(settings_bytes).hexdigest(),
            "settings_digest_sha256": canonical_json_sha256(settings.model_dump(mode="json", exclude_unset=True)),
        }

    @staticmethod
    def _plan_requests_match_settings(
        snapshot_payload: dict[str, Any], settings, *, require_registered_match: bool = True,
    ) -> bool:
        result = snapshot_payload.get("result", {})
        speech = snapshot_payload.get("speech_text", "")
        registered = settings.request_spec
        if registered is None:
            return False
        for chunk in result.get("chunks", []):
            spec = chunk.get("request_spec")
            request_sha = chunk.get("request_sha256")
            if not isinstance(spec, dict) or not request_sha:
                return False
            try:
                start, end = int(chunk["start"]), int(chunk["end"])
                text = speech[start:end]
                request_spec = dto.RequestSpec.model_validate(spec, strict=True)
                if request_fingerprint(text, request_spec) != request_sha:
                    return False
            except (KeyError, TypeError, ValueError):
                return False
            if require_registered_match:
                if not _request_matches_registered_settings(request_spec, registered):
                    return False
        return bool(result.get("chunks"))

    def _production_snapshot_eligible(
        self, state: ProjectState, layout: BookLayout, chapter, snapshot: dict[str, Any], *,
        allow_historical_plan: bool = False,
    ) -> tuple[bool, str]:
        """Shared current-prose/settings authority for build, commit and getters."""
        if snapshot.get("chapter_id") != chapter.chapter_id or snapshot.get("scope_key") != dto.ProductionScope(kind="production").model_dump_json():
            return False, "wrong_namespace"
        try:
            facts = self._production_facts(layout, chapter)
        except BookServiceError as exc:
            return False, exc.reason
        payload = snapshot.get("payload", {})
        if payload.get("result", {}).get("prose_projection_sha256") != facts["projection"].prose_projection_sha256:
            return False, "source_changed"
        if not self._plan_requests_match_settings(
            payload, facts["settings"], require_registered_match=not allow_historical_plan,
        ):
            return False, "plan_ineligible"
        if (payload.get("production_target") is None
                or (not allow_historical_plan
                    and payload.get("production_target") != _data(facts["settings"].production_target))):
            return False, "target_changed"
        if (not allow_historical_plan
                and payload.get("production_settings_digest_sha256") != facts["settings_digest_sha256"]):
            return False, "settings_changed"
        return True, "eligible"

    def _accepted_chapter_build_eligible(
        self, state: ProjectState, layout: BookLayout, chapter, build: dict[str, Any], *,
        allow_historical_plan: bool,
    ) -> tuple[bool, str]:
        if (build.get("scope") != "chapter"
                or build.get("scope_key") != dto.ProductionScope(kind="production").model_dump_json()):
            return False, "wrong_namespace"
        stored = state.snapshot(build.get("snapshot_id", ""))
        if stored is None:
            return False, "snapshot_unavailable"
        if build.get("request_plan_sha256") != stored.get("payload", {}).get("result", {}).get("request_plan_sha256"):
            return False, "plan_mismatch"
        eligible, reason = self._production_snapshot_eligible(
            state, layout, chapter, stored, allow_historical_plan=allow_historical_plan,
        )
        if not eligible:
            return False, reason
        result = stored["payload"].get("result", {})
        chunks = result.get("chunks", [])
        by_id = {item.get("chunk_id"): item for item in chunks}
        take_ids = build.get("input_take_ids", [])
        takes = [state.take(take_id) for take_id in take_ids]
        if (not chunks or len(takes) != len(chunks) or any(take is None for take in takes)
                or [take.get("chunk_id") for take in takes if take is not None]
                != [item.get("chunk_id") for item in chunks]):
            return False, "take_unavailable"
        scope = dto.ProductionScope(kind="production").model_dump(mode="json")
        for take in takes:
            chunk = by_id.get(take.get("chunk_id"))
            if (chunk is None or take.get("chapter_id") != chapter.chapter_id
                    or take.get("namespace") != scope
                    or take.get("request_sha256") != chunk.get("request_sha256")
                    or take.get("provenance") != "native_generation"
                    or take.get("media", {}).get("encoding") not in {"signed_integer", "float"}):
                return False, "take_ineligible"
            try:
                if _file_sha256(_path(self.root, take["filepath"])) != take.get("bytes_sha256"):
                    return False, "take_changed"
            except (BookServiceError, OSError, KeyError):
                return False, "take_unavailable"
        outputs = build.get("result", {}).get("outputs", [])
        master = next((value for value in outputs if value.get("kind") == "pcm_master"), None)
        if master is None:
            return False, "master_unavailable"
        try:
            if _file_sha256(_path(self.root, master["filepath"])) != master.get("bytes_sha256"):
                return False, "master_changed"
        except (BookServiceError, OSError, KeyError):
            return False, "master_unavailable"
        return True, "eligible"

    def recover_bookmark_publications(self) -> None:
        """Recover/finalize guarded tagged-DOCX prepare publications under the project lock."""
        state = self.discover_state()
        if state is None:
            return
        publications = state.publications("chapter_bookmark_prepare")
        if not publications:
            return
        config = load_book_config(self.root, state)
        if config.config_state != "enabled" or config.layout is None:
            raise BookServiceError("publication_conflict", "A pending bookmark publication has no enabled registered layout.")
        layout = config.layout

        def owned_path(relative: str, expected_sha256: str | None) -> Path:
            path = _path(self.root, relative, allow_missing=True)
            if path.exists():
                if not path.is_file() or path.is_symlink():
                    raise BookServiceError("publication_conflict", "An owned bookmark publication path changed type.")
                if expected_sha256 is not None and _file_sha256(path) != expected_sha256:
                    raise BookServiceError("publication_conflict", "An owned bookmark publication artifact changed.")
            return path

        for entry in publications:
            payload = entry["payload"]
            try:
                chapter = self._chapter(layout, payload["chapter_id"])
                target_relative = payload["target_filepath"]
                if payload["book_id"] != layout.book_id or payload["kind"] != "chapter_bookmark_prepare":
                    raise ValueError("registration binding mismatch")
                if target_relative != chapter.tagged_filepath:
                    scope = json.loads(payload["scope_key"])
                    if scope.get("kind") != "test":
                        raise ValueError("alternate bookmark target lacks test scope")
                    prose_relative = payload["working_prose_filepath"]
                    snapshot = state.snapshot(payload["snapshot_id"])
                    if snapshot is None:
                        raise ValueError("alternate bookmark snapshot is unavailable")
                    self._active_test_authorization(
                        layout, chapter, authorization_id=scope.get("authorization_id"),
                        prose_filepath=prose_relative, tagged_filepath=target_relative,
                        prose_bytes=_read_bytes(self.root, prose_relative),
                        selected_ordinals=snapshot["payload"].get("selected_source_ordinals"),
                    )
                target = _path(self.root, target_relative)
                old_hash, new_hash = payload["old_sha256"], payload["new_sha256"]
                backup = owned_path(payload["backup_filepath"], old_hash)
                stage = owned_path(payload["stage_filepath"], new_hash)
                snapshot_tagged = owned_path(payload["snapshot_tagged_filepath"], new_hash)
                input_tagged = owned_path(payload["input_tagged_filepath"], old_hash)
                restore_stage = owned_path(payload["restore_stage_filepath"], old_hash)
                if entry["phase"] == "committed":
                    snapshot = state.snapshot(payload["snapshot_id"])
                    receipt = state.receipt(
                        owner_key=payload["owner_key"], project=payload["project"],
                        tool=payload["tool"], operation_id=payload["operation_id"],
                    )
                    if (snapshot is None or snapshot["chapter_id"] != chapter.chapter_id
                            or snapshot["scope_key"] != payload["scope_key"]
                            or receipt is None or receipt[0] != payload["args_sha256"]
                            or snapshot["payload"].get("result", {}).get("snapshot_id") != payload["snapshot_id"]):
                        raise ValueError("committed snapshot/receipt binding mismatch")
                    for path, expected in ((backup, old_hash), (stage, new_hash), (restore_stage, old_hash)):
                        if path.exists():
                            if _file_sha256(path) != expected:
                                raise ValueError("owned finalization artifact changed")
                            path.unlink()
                    state.delete_publication(entry["journal_id"])
                    continue

                current_hash = _file_sha256(target)
                if current_hash == new_hash:
                    if not backup.exists() or _file_sha256(backup) != old_hash:
                        raise ValueError("original backup is unavailable or changed")
                    _require_unlocked(target)
                    if restore_stage.exists():
                        if _file_sha256(restore_stage) != old_hash:
                            raise ValueError("restore stage changed")
                    else:
                        with backup.open("rb") as source, restore_stage.open("xb") as output:
                            shutil.copyfileobj(source, output)
                            output.flush()
                            os.fsync(output.fileno())
                    if _file_sha256(target) != new_hash:
                        raise BookServiceError("publication_conflict", "The working DOCX changed during bookmark recovery.")
                    os.replace(restore_stage, target)
                elif current_hash != old_hash:
                    raise BookServiceError("publication_conflict", "The working DOCX changed outside this bookmark publication.")
                # Remove only hash-verified files owned by this uncommitted operation.
                prose_snapshot = owned_path(payload["snapshot_prose_filepath"], payload["prose_sha256"])
                for path, expected in (
                    (backup, old_hash), (stage, new_hash), (snapshot_tagged, new_hash),
                    (input_tagged, old_hash), (restore_stage, old_hash),
                    (prose_snapshot, payload["prose_sha256"]),
                ):
                    if path.exists():
                        if _file_sha256(path) != expected:
                            raise BookServiceError("publication_conflict", "An uncommitted snapshot artifact changed.")
                        path.unlink()
                state.delete_publication(entry["journal_id"])
            except BookServiceError:
                raise
            except (KeyError, OSError, ValueError) as exc:
                raise BookServiceError("publication_conflict", "A bookmark publication could not be recovered safely.") from exc

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

    def _inspect_view_row(self, state: ProjectState | None, view_id: str) -> dict[str, Any] | None:
        return (state.load_view(view_id) if state is not None else None) or self._pending_views.get(view_id)

    @staticmethod
    def _pinned_view_bytes(payload: dict[str, Any]) -> tuple[bytes, bytes]:
        """Return the exact DOCX pair captured by an inspect view, never live files."""
        try:
            prose = base64.b64decode(payload["pinned_prose_base64"], validate=True)
            tagged = base64.b64decode(payload["pinned_tagged_base64"], validate=True)
            if (hashlib.sha256(prose).hexdigest() != payload["projection"]["prose_sha256"]
                    or hashlib.sha256(tagged).hexdigest() != payload["projection"]["tagged_sha256"]):
                raise ValueError
        except (KeyError, TypeError, ValueError) as exc:
            # Pre-capture views cannot truthfully become byte-pinned after the
            # fact. A caller must create a fresh view from current sources.
            raise BookServiceError("view_not_found", "The document view must be recreated before it can be read.") from exc
        return prose, tagged

    def _project_pinned_view(self, payload: dict[str, Any]):
        prose, tagged = self._pinned_view_bytes(payload)
        try:
            return project_docx_pair(
                prose, tagged,
                speech_paragraph_ids=payload["speech_paragraph_ids"],
                excluded_paragraphs=payload["excluded_paragraphs"],
                explicit_tag_spans=payload["explicit_tag_spans"],
            )
        except (KeyError, ProjectionError, ValueError) as exc:
            raise BookServiceError("state_unavailable", "The pinned document view is malformed.") from exc

    @staticmethod
    def _cursor_arguments_match(request: dto.InspectRequest, payload: dict[str, Any]) -> bool:
        """Bind optional refinement arguments when a caller repeats them on a cursor read."""
        binding = payload.get("cursor_binding")
        if not isinstance(binding, dict):
            return False
        if (request.chapter_id != payload.get("chapter_id")
                or request.prose_filepath != payload.get("prose_filepath")
                or request.tagged_filepath != payload.get("tagged_filepath")):
            return False
        for field in ("base_document_view_id", "speech_paragraph_ids", "excluded_paragraphs", "explicit_tag_spans"):
            if field in request.model_fields_set and _data(getattr(request, field)) != binding.get(field):
                return False
        return True

    @staticmethod
    def _inspect_page(projected, *, view_id: str, offset: int, page_size: int) -> dict[str, Any]:
        speech_length = len(projected.speech_text)
        paragraph_starts: list[int] = []
        field_end = speech_length
        for paragraph in projected.paragraphs:
            paragraph_starts.append(field_end)
            field_end += len(paragraph.text)
        if offset > field_end:
            raise BookServiceError("invalid_cursor", "The text cursor is outside the pinned view.")
        end = min(field_end, offset + page_size)
        speech_start = min(speech_length, offset)
        speech_end = min(speech_length, end)
        from .projection import PROJECTION_VERSION
        paragraph_results = []
        for paragraph, field_start in zip(projected.paragraphs, paragraph_starts, strict=True):
            local_start = min(len(paragraph.text), max(0, offset - field_start))
            local_end = min(len(paragraph.text), max(0, end - field_start))
            if local_end < local_start:
                local_end = local_start
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
            "speech_text": projected.speech_text[speech_start:speech_end],
            "returned_start": speech_start,
            "returned_end": speech_end,
            "paragraphs": paragraph_results,
            "excluded_paragraphs": _data(projected.excluded_paragraphs),
            "source_text_matches_without_tags": projected.source_text_matches_without_tags,
            "warnings": [
                {"code": "unsupported_structure",
                 "message": f"{item.part}:{item.location}: {item.detail}"}
                for item in projected.unsupported
            ],
            "has_more": end < field_end,
            "next_cursor": _cursor(view_id, end) if end < field_end else None,
        }

    def inspect(self, request: dto.InspectRequest) -> dict[str, Any]:
        state = self.discover_state()
        page_size = request.max_characters if "max_characters" in request.model_fields_set else 12000
        if "cursor" in request.model_fields_set:
            view_id, offset = _cursor_view_and_offset(request.cursor)
            view_row = self._inspect_view_row(state, view_id)
            if view_row is None:
                raise BookServiceError("invalid_cursor", "The text cursor is invalid or stale.")
            if datetime.fromisoformat(view_row["expires_at"]) <= datetime.now(timezone.utc):
                raise BookServiceError("view_expired", "The pinned document view has expired.")
            payload = view_row["payload"]
            if not self._cursor_arguments_match(request, payload):
                raise BookServiceError("invalid_cursor", "The text cursor arguments do not match its pinned view.")
            projected = self._project_pinned_view(payload)
            if (payload.get("document_view_id") != view_id
                    or payload.get("projection", {}).get("document_view_id") != projected.document_view_id):
                raise BookServiceError("invalid_cursor", "The text cursor is invalid or stale.")
            config = load_book_config(self.root, state)
            if config.layout is None:
                raise BookServiceError("configuration_conflict", "Book configuration is not valid for inspection.")
            chapter = self._chapter(config.layout, request.chapter_id)
            if (request.prose_filepath, request.tagged_filepath) != (
                chapter.working_filepath, chapter.tagged_filepath
            ):
                self._active_test_authorization_for_hash(
                    config.layout, chapter, authorization_id=None,
                    prose_filepath=request.prose_filepath, tagged_filepath=request.tagged_filepath,
                    prose_sha256=payload.get("projection", {}).get("prose_sha256"),
                    selected_ordinals=[
                        item.source_ordinal for item in projected.paragraphs
                        if item.speech_start is not None
                    ],
                )
            return self._inspect_page(projected, view_id=view_id, offset=offset, page_size=page_size)

        config = load_book_config(self.root, state)
        # A valid unbound layout is inspectable so the first explicit prepare
        # can complete the create-only binding bootstrap. Conflicting or
        # malformed documents remain fail-closed.
        if config.config_state not in {"enabled", "bootstrap_pending"} or config.layout is None:
            raise BookServiceError("configuration_conflict", "Book configuration is not valid for inspection.")
        chapter = self._chapter(config.layout, request.chapter_id)
        selected_authorization = None
        registered_pair = (chapter.working_filepath, chapter.tagged_filepath)
        requested_pair = (request.prose_filepath, request.tagged_filepath)
        if requested_pair != registered_pair:
            prose_for_admission = _read_bytes(self.root, request.prose_filepath)
            selected_authorization = self._active_test_authorization(
                config.layout, chapter, authorization_id=None,
                prose_filepath=request.prose_filepath, tagged_filepath=request.tagged_filepath,
                prose_bytes=prose_for_admission,
            )
        refinement_fields = {"speech_paragraph_ids", "excluded_paragraphs", "explicit_tag_spans"}
        refining = bool(refinement_fields & request.model_fields_set)
        if refining and "base_document_view_id" not in request.model_fields_set:
            raise BookServiceError("validation_failed", "Selection and tag refinements require base_document_view_id.")
        base_payload: dict[str, Any] | None = None
        if "base_document_view_id" in request.model_fields_set:
            base_row = self._inspect_view_row(state, request.base_document_view_id)
            if base_row is None:
                raise BookServiceError("view_not_found", "The base document view is unavailable.")
            if datetime.fromisoformat(base_row["expires_at"]) <= datetime.now(timezone.utc):
                raise BookServiceError("view_expired", "The base document view has expired.")
            base_payload = base_row["payload"]
            if (base_row["chapter_id"] != chapter.chapter_id
                    or base_payload.get("layout_revision") != config.layout.layout_revision
                    or base_payload.get("prose_filepath") != request.prose_filepath
                    or base_payload.get("tagged_filepath") != request.tagged_filepath):
                raise BookServiceError("view_not_found", "The base document view does not match this chapter and source pair.")
            prose_bytes, tagged_bytes = self._pinned_view_bytes(base_payload)
            current_prose = _read_bytes(self.root, request.prose_filepath)
            current_tagged = _read_bytes(self.root, request.tagged_filepath)
            if (hashlib.sha256(current_prose).hexdigest() != base_payload["projection"]["prose_sha256"]
                    or hashlib.sha256(current_tagged).hexdigest() != base_payload["projection"]["tagged_sha256"]):
                raise BookServiceError("stale_file", "The registered source changed after the base view was captured.")
        else:
            prose_bytes = _read_bytes(self.root, request.prose_filepath)
            tagged_bytes = _read_bytes(self.root, request.tagged_filepath)

        speech_ids = (
            list(request.speech_paragraph_ids) if "speech_paragraph_ids" in request.model_fields_set
            else (list(base_payload["speech_paragraph_ids"]) if base_payload is not None else None)
        )
        exclusions = (
            _data(request.excluded_paragraphs) if "excluded_paragraphs" in request.model_fields_set
            else (base_payload["excluded_paragraphs"] if base_payload is not None else [])
        )
        explicit_spans = (
            _data(request.explicit_tag_spans) if "explicit_tag_spans" in request.model_fields_set
            else (base_payload["explicit_tag_spans"] if base_payload is not None else [])
        )
        try:
            projected = project_docx_pair(
                prose_bytes, tagged_bytes,
                speech_paragraph_ids=speech_ids,
                excluded_paragraphs=exclusions,
                explicit_tag_spans=explicit_spans,
            )
        except ProjectionError as exc:
            raise BookServiceError(exc.code, str(exc)) from exc
        if selected_authorization is not None:
            selected_ordinals = {
                item.source_ordinal for item in projected.paragraphs if item.speech_start is not None
            }
            if not selected_ordinals.issubset(set(selected_authorization.allowed_paragraph_ordinals)):
                raise BookServiceError("test_scope_not_authorized", "The selected paragraphs exceed the active test authorization.")
        view_id = _bound_document_view_id(
            projected.document_view_id, chapter_id=chapter.chapter_id,
            prose_filepath=request.prose_filepath, tagged_filepath=request.tagged_filepath,
            layout_revision=config.layout.layout_revision,
        )
        view_payload = {
            "document_view_id": view_id,
            "projection": _data(projected),
            "prose_filepath": request.prose_filepath,
            "tagged_filepath": request.tagged_filepath,
            "chapter_id": chapter.chapter_id,
            "layout_revision": config.layout.layout_revision,
            "speech_paragraph_ids": list(projected.paragraph_ids) if speech_ids is None else speech_ids,
            "explicit_tag_spans": explicit_spans,
            "excluded_paragraphs": exclusions,
            "pinned_prose_base64": base64.b64encode(prose_bytes).decode("ascii"),
            "pinned_tagged_base64": base64.b64encode(tagged_bytes).decode("ascii"),
            "cursor_binding": {
                # An idempotent refinement has the base projection's exact
                # identity and must retain its payload so save_view can renew
                # expiry without treating pagination/refinement transport as
                # distinct content.
                "base_document_view_id": (
                    request.base_document_view_id
                    if base_payload is not None and view_id != request.base_document_view_id
                    else None
                ),
                "speech_paragraph_ids": list(projected.paragraph_ids) if speech_ids is None else speech_ids,
                "excluded_paragraphs": exclusions,
                "explicit_tag_spans": explicit_spans,
            },
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
        return self._inspect_page(projected, view_id=view_id, offset=0, page_size=page_size)

    def prepare(self, request: dto.PrepareRequest, *, owner_key: str) -> tuple[dict[str, Any], bool]:
        # Mutation is the only path that creates state. The caller owns the
        # per-project write lock and permission check.
        for item in request.chunks:
            if item.request_spec is not None:
                _validate_request_spec_durable_fields(item.request_spec)
        state = self.discover_state()
        args_sha = canonical_json_sha256(request.model_dump(mode="json", exclude_unset=True))
        if state is not None:
            self.recover_bookmark_publications()
            prior = state.receipt(owner_key=owner_key, project=self.project_name,
                                  tool="audiobook_prepare_chapter", operation_id=request.operation_id)
            if prior is not None:
                if prior[0] != args_sha:
                    raise BookServiceError("operation_id_conflict", "This operation ID was used for different content.")
                return prior[1], True
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
        working_prose_filepath = payload.get("prose_filepath")
        working_tagged_filepath = payload.get("tagged_filepath")
        if not isinstance(working_prose_filepath, str) or not isinstance(working_tagged_filepath, str):
            raise BookServiceError("view_not_found", "The document view must be recreated before preparation.")
        if payload["layout_revision"] != layout.layout_revision:
            raise BookServiceError("stale_configuration", "The book layout changed after inspection.")
        prose_bytes = _read_bytes(self.root, working_prose_filepath)
        tagged_bytes = _read_bytes(self.root, working_tagged_filepath)
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
        expected_view_id = _bound_document_view_id(
            projected.document_view_id, chapter_id=chapter.chapter_id,
            prose_filepath=working_prose_filepath, tagged_filepath=working_tagged_filepath,
            layout_revision=layout.layout_revision,
        )
        if (request.document_view_id != expected_view_id
                or payload.get("document_view_id") != expected_view_id
                or projected_data.get("document_view_id") != projected.document_view_id):
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
            if (working_prose_filepath, working_tagged_filepath) != (chapter.working_filepath, chapter.tagged_filepath):
                raise BookServiceError("production_not_authorized", "Production uses the registered chapter source pair only.")
            if projected.unsupported:
                raise BookServiceError(
                    "unsupported_docx_structure",
                    "Production cannot use registered DOCX sources with unsupported structures.",
                )
            authorization = layout.production_authorization
            if authorization is None or authorization.revoked or not authorization.completed_book:
                raise BookServiceError("production_not_authorized", "Production requires an active completed-book authorization.")
            approval = chapter_state.approval_provenance
            if (chapter_state.editorial_status != "approved" or approval is None
                    or chapter_state.approved_prose_projection_sha256 != projected.prose_projection_sha256
                    or chapter_state.approval_projection_version != projected.projection_version
                    or chapter_state.approved_source_raw_sha256 != approval.source_raw_sha256
                    or approval.prose_projection_sha256 != chapter_state.approved_prose_projection_sha256
                    or approval.projection_version != chapter_state.approval_projection_version):
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
            if settings.request_spec is None or settings.production_target is None:
                raise BookServiceError("settings_mismatch", "Production settings lack a resolved request specification or media target.")
            if (request.production_target != settings.production_target
                    or request.request_limit.value != settings.request_limit.value
                    or request.request_limit.unit != settings.request_limit.unit):
                raise BookServiceError("settings_mismatch", "The requested production settings differ from the validated book settings.")
            for item in request.chunks:
                spec = item.request_spec
                if spec is None:
                    raise BookServiceError("request_hash_mismatch", "Every production chunk needs its complete resolved request specification.")
                registered = settings.request_spec
                if not _request_matches_registered_settings(spec, registered):
                    raise BookServiceError("settings_mismatch", "A production chunk request differs from the registered provider settings.")
            production_settings_sha256 = settings_sha
            prepared_production_target = _data(settings.production_target)
        else:
            auth = self._active_test_authorization(
                layout, chapter, authorization_id=request.scope.authorization_id,
                prose_filepath=working_prose_filepath, tagged_filepath=working_tagged_filepath,
                prose_bytes=prose_bytes,
            )
            selected_ordinals = {p.source_ordinal for p in projected.paragraphs if p.speech_start is not None}
            if not selected_ordinals.issubset(set(auth.allowed_paragraph_ordinals)):
                raise BookServiceError("test_scope_not_authorized", "The selected paragraphs exceed the test authorization.")
        requested_chunks = {item.chunk_id: item for item in request.chunks}
        if len(requested_chunks) != len(request.chunks):
            raise BookServiceError("duplicate_or_recycled_chunk_id", "Chunk IDs must be unique in a prepared plan.")
        ranges, coverage = validate_chunk_ranges(
            projected, request.chunks,
            limit=request.request_limit.value, unit=request.request_limit.unit,
        )
        scope_key = request.scope.model_dump_json()
        prior_namespace = state.namespace(chapter.chapter_id, scope_key) if state is not None else None
        prior_snapshot = (state.snapshot(prior_namespace["current_snapshot_id"])
                          if state is not None and prior_namespace and prior_namespace.get("current_snapshot_id")
                          else None)
        previous_chunks = (prior_snapshot or {}).get("payload", {}).get("result", {}).get("chunks", [])
        previous_by_id = {value["chunk_id"]: value for value in previous_chunks}
        current_lineage = ({value["chunk_id"]: value for value in state.chunk_lineage(
            chapter_id=chapter.chapter_id, scope_key=scope_key)} if state is not None else {})
        lineage_rows: list[dict[str, Any]] = []
        predecessor_to_successors: dict[str, list[str]] = {}
        for item in request.chunks:
            raw_predecessors = item.replaces_chunk_ids if "replaces_chunk_ids" in item.model_fields_set else []
            if len(raw_predecessors) != len(set(raw_predecessors)) or item.chunk_id in raw_predecessors:
                raise BookServiceError("duplicate_or_recycled_chunk_id", "Chunk lineage contains a duplicate or self reference.")
            prior_lineage = current_lineage.get(item.chunk_id)
            if prior_lineage is not None and prior_lineage["retired"]:
                raise BookServiceError("duplicate_or_recycled_chunk_id", "A retired chunk ID cannot be reused.")
            if item.chunk_id in previous_by_id and raw_predecessors:
                raise BookServiceError("duplicate_or_recycled_chunk_id", "An unchanged logical chunk cannot also replace another chunk.")
            for predecessor in raw_predecessors:
                if predecessor not in previous_by_id or predecessor not in current_lineage or current_lineage[predecessor]["retired"]:
                    raise BookServiceError("duplicate_or_recycled_chunk_id", "Lineage may reference only active chunks in this chapter namespace.")
                predecessor_to_successors.setdefault(predecessor, []).append(item.chunk_id)
            lineage_rows.append({"chunk_id": item.chunk_id, "replaces_chunk_ids": list(raw_predecessors)})
        retired_chunk_ids = sorted(set(previous_by_id) - set(requested_chunks))
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
        prose_path = snapshot_dir / f"{snapshot_id}-prose.docx"
        tagged_path = snapshot_dir / f"{snapshot_id}-tagged.docx"
        input_tagged_path = snapshot_dir / f"{snapshot_id}-tagged-input.docx"

        # Freeze a separate navigation bookmark for every source segment. This
        # keeps a chunk split around excluded prose from becoming one enclosing
        # Word range that accidentally includes the excluded paragraph.
        input_tagged_projection = parse_docx(tagged_bytes)
        paragraph_by_id = {paragraph.paragraph_id: paragraph for paragraph in projected.paragraphs}
        placements: list[BookmarkPlacement] = []
        bookmark_names: dict[str, list[str]] = {}
        for chunk_range in ranges:
            names: list[str] = []
            for segment_index, segment in enumerate(chunk_range.source_segments):
                paragraph = paragraph_by_id[segment.paragraph_id]
                source_paragraph_id = input_tagged_projection.paragraphs[paragraph.source_ordinal].paragraph_id
                name = "cog_" + hashlib.sha256(
                    f"{snapshot_id}:{chunk_range.chunk_id}:{segment_index}".encode("utf-8")
                ).hexdigest()[:24]
                placements.append(BookmarkPlacement(
                    name,
                    BookmarkLocation(source_paragraph_id, segment.start),
                    BookmarkLocation(source_paragraph_id, segment.end),
                ))
                names.append(name)
            bookmark_names[chunk_range.chunk_id] = names
        tagged_snapshot_bytes = add_bookmarks(tagged_bytes, input_tagged_projection, placements)

        # Bookmark insertion changes raw pair-bound paragraph IDs, so remap the
        # exact selected/excluded paragraphs and explicit tag spans by ordinal.
        prebookmark_ordinal = {paragraph_id: index for index, paragraph_id in enumerate(projected.paragraph_ids)}
        bookmarked_pair = project_docx_pair(prose_bytes, tagged_snapshot_bytes)
        bookmarked_ids = bookmarked_pair.paragraph_ids
        selected_ids = [bookmarked_ids[prebookmark_ordinal[value]] for value in payload["speech_paragraph_ids"]]
        excluded_values = [
            {"paragraph_id": bookmarked_ids[prebookmark_ordinal[item["paragraph_id"]]], "reason": item["reason"]}
            for item in payload["excluded_paragraphs"]
        ]
        explicit_values = [
            {**item, "paragraph_id": bookmarked_ids[prebookmark_ordinal[item["paragraph_id"]]]}
            for item in payload["explicit_tag_spans"]
        ]
        projected = project_docx_pair(
            prose_bytes, tagged_snapshot_bytes,
            speech_paragraph_ids=selected_ids,
            excluded_paragraphs=excluded_values,
            explicit_tag_spans=explicit_values,
        )
        ranges, coverage = validate_chunk_ranges(
            projected, request.chunks,
            limit=request.request_limit.value, unit=request.request_limit.unit,
        )
        if request.publish_bookmarks_to_working_tagged_docx:
            _require_unlocked(_path(self.root, working_tagged_filepath))

        _mkdir_safe(self.root, snapshot_dir.relative_to(self.root).as_posix())
        for path, content in ((prose_path, prose_bytes), (tagged_path, tagged_snapshot_bytes),
                              (input_tagged_path, tagged_bytes)):
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
        tag_deletion_spans = [
            [paragraph.speech_start + start, paragraph.speech_start + end]
            for paragraph in projected.paragraphs
            if paragraph.speech_start is not None
            for start, end in paragraph.tags
        ]
        plan_rows: list[dict[str, Any]] = []
        for order, item in enumerate(ranges):
            text = projected.speech_text[item.start:item.end]
            spoken_text = spoken_interval(
                projected.speech_text, projected.spoken_projection,
                item.start, item.end, tag_deletion_spans,
            )
            requested = requested_chunks[item.chunk_id]
            request_spec = (
                _data(requested.request_spec)
                if requested.request_spec is not None else None
            )
            chunk = {
                "chunk_id": item.chunk_id, "snapshot_id": snapshot_id, "order": order,
                "start": item.start, "end": item.end,
                "bookmark": (bookmark_names[item.chunk_id][0] if bookmark_names[item.chunk_id] else ""),
                "source_segments": [
                    {"paragraph_id": segment.paragraph_id, "start": segment.start, "end": segment.end,
                     "bookmark": bookmark_names[item.chunk_id][segment_index]}
                    for segment_index, segment in enumerate(item.source_segments)
                ],
                "codepoint_count": item.codepoint_count, "limit_count": item.limit_count,
                "prompt_sha256": item.prompt_sha256,
                "spoken_text_sha256": hashlib.sha256(spoken_text.encode("utf-8")).hexdigest(),
                "request_sha256": (
                    request_fingerprint(text, requested.request_spec)
                    if requested.request_spec is not None else None
                ),
                "request_spec": request_spec,
                "replaces_chunk_ids": [], "replaced_by_chunk_ids": [],
                "opening_phrase": spoken_text[:120], "closing_phrase": spoken_text[-120:],
                "take_ids": [], "accepted_take_id": None,
                "reuse_status": "new", "reusable_take_ids": [],
            }
            if item.chunk_id in previous_by_id:
                chunk["replaced_by_chunk_ids"] = []
            chunkspec = requested_chunks[item.chunk_id]
            chunk["replaces_chunk_ids"] = list(
                chunkspec.replaces_chunk_ids if "replaces_chunk_ids" in chunkspec.model_fields_set else []
            )
            matching_takes: list[dict[str, Any]] = []
            if state is not None and chunk["request_sha256"] is not None and item.chunk_id in previous_by_id:
                for take in state.takes(chapter_id=chapter.chapter_id):
                    take_scope = take.get("namespace")
                    if (take.get("chunk_id") == item.chunk_id
                            and take.get("request_sha256") == chunk["request_sha256"]
                            and take.get("namespace") == _data(request.scope)
                            and (not isinstance(request.scope, dto.ProductionScope)
                                 or (take.get("provenance") == "native_generation"
                                     and take.get("media", {}).get("encoding") in {"signed_integer", "float"}))):
                        matching_takes.append(take)
            if matching_takes:
                chunk["reuse_status"] = "reusable"
                chunk["take_ids"] = [take["take_id"] for take in matching_takes]
                chunk["reusable_take_ids"] = [take["take_id"] for take in matching_takes]
            elif item.chunk_id in previous_by_id:
                chunk["reuse_status"] = "changed" if chunk["request_sha256"] is not None else "needs_check"
            elif chunk["request_sha256"] is None:
                chunk["reuse_status"] = "needs_check"
            chunks.append(chunk)
            plan_rows.append({"chunk_id": chunk["chunk_id"], "prompt_sha256": chunk["prompt_sha256"],
                              "request_sha256": chunk["request_sha256"]})
        if state is not None:
            accepted_head = state.chapter_head(chapter.chapter_id, scope_key)
            accepted_build = state.build(accepted_head["accepted_build_id"]) if accepted_head else None
            accepted_snapshot = state.snapshot(accepted_head["accepted_snapshot_id"]) if accepted_head else None
            if accepted_build is not None and accepted_snapshot is not None:
                accepted_chunk_ids = {
                    value.get("chunk_id") for value in accepted_snapshot["payload"].get("result", {}).get("chunks", [])
                }
                accepted_takes: dict[str, str] = {}
                for take_id in accepted_build.get("input_take_ids", []):
                    accepted_take = state.take(take_id)
                    if accepted_take is not None and accepted_take.get("chunk_id") in accepted_chunk_ids:
                        accepted_takes[accepted_take["chunk_id"]] = take_id
                for chunk in chunks:
                    take_id = accepted_takes.get(chunk["chunk_id"])
                    if take_id in chunk["reusable_take_ids"]:
                        chunk["accepted_take_id"] = take_id
        plan_sha = canonical_json_sha256({
            "chunks": plan_rows,
            "selection": {"speech_paragraph_ids": selected_ids,
                          "excluded_paragraphs": excluded_values,
                          "projection_version": projected.projection_version},
            "request_limit": _data(request.request_limit),
            "production_settings_sha256": (
                canonical_json_sha256(validate_production_settings(
                    _read_bytes(self.root, layout.shared_paths.production_settings_filepath)
                ).model_dump(mode="json", exclude_unset=True))
                if isinstance(request.scope, dto.ProductionScope) else None
            ),
            "production_target": prepared_production_target,
        })
        result = {
            "snapshot_id": snapshot_id,
            "manifest_revision": 1,
            "input_tagged_sha256": hashlib.sha256(tagged_bytes).hexdigest(),
            "snapshot_tagged_sha256": projected.tagged_sha256,
            "snapshot_prose_sha256": projected.prose_sha256,
            "prose_projection_sha256": projected.prose_projection_sha256,
            "spoken_projection_sha256": projected.spoken_projection_sha256,
            "request_plan_sha256": plan_sha,
            "snapshot_filepath": prose_path.relative_to(self.root).as_posix(),
            "working_tagged_filepath": working_tagged_filepath,
            "working_tagged_updated": request.publish_bookmarks_to_working_tagged_docx,
            "chunks": chunks, "retired_chunk_ids": retired_chunk_ids,
            "coverage": _data(coverage), "current_outputs_stale": True,
        }
        snapshot_payload = {
            "result": result, "prose_filepath": prose_path.relative_to(self.root).as_posix(),
            "tagged_filepath": tagged_path.relative_to(self.root).as_posix(),
            "input_tagged_filepath": input_tagged_path.relative_to(self.root).as_posix(),
            "input_tagged_sha256": result["input_tagged_sha256"],
            "speech_text": projected.speech_text,
            "spoken_projection": projected.spoken_projection,
            "tag_deletion_spans": tag_deletion_spans,
            "prose_projection": projected.prose_projection,
            "production_settings_sha256": production_settings_sha256,
            "production_settings_digest_sha256": (
                canonical_json_sha256(validate_production_settings(
                    _read_bytes(self.root, layout.shared_paths.production_settings_filepath)
                ).model_dump(mode="json", exclude_unset=True))
                if isinstance(request.scope, dto.ProductionScope) else None
            ),
            "production_target": prepared_production_target,
            "speech_paragraph_ids": selected_ids,
            "selected_source_ordinals": [
                paragraph.source_ordinal for paragraph in projected.paragraphs
                if paragraph.paragraph_id in set(selected_ids)
            ],
            "excluded_paragraphs": excluded_values,
            "explicit_tag_spans": explicit_values,
            "projection_version": projected.projection_version,
            "working_prose_filepath": working_prose_filepath,
            "working_tagged_filepath": working_tagged_filepath,
        }
        if isinstance(request.scope, dto.ProductionScope):
            approval = chapter_state.approval_provenance
            assert approval is not None
            snapshot_payload["approval_equivalence"] = {
                "approval_source_raw_sha256": approval.source_raw_sha256,
                "observed_prose_raw_sha256": projected.prose_sha256,
                "complete_prose_projection_sha256": projected.prose_projection_sha256,
                "projection_version": projected.projection_version,
                "equivalent": True,
            }
            snapshot_payload["production_settings_binding"] = {
                "observed_raw_sha256": production_settings_sha256,
                "canonical_relevant_sha256": snapshot_payload["production_settings_digest_sha256"],
            }
        journal_id: str | None = None
        if request.publish_bookmarks_to_working_tagged_docx:
            target = _path(self.root, working_tagged_filepath)
            old_hash = hashlib.sha256(tagged_bytes).hexdigest()
            new_hash = hashlib.sha256(tagged_snapshot_bytes).hexdigest()
            if _file_sha256(target) != old_hash:
                raise BookServiceError("stale_source", "The registered tagged DOCX changed before bookmark publication.")
            _require_unlocked(target)
            backup_path = snapshot_dir / f"{snapshot_id}-working-tagged-backup.docx"
            stage_path = target.with_name(f".{target.name}.{snapshot_id}.stage")
            restore_path = target.with_name(f".{target.name}.{snapshot_id}.restore")
            try:
                for path, content in ((backup_path, tagged_bytes), (stage_path, tagged_snapshot_bytes)):
                    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                    with os.fdopen(fd, "wb") as stream:
                        stream.write(content)
                        stream.flush()
                        os.fsync(stream.fileno())
            except OSError as exc:
                raise BookServiceError("publication_failed", "The guarded tagged-DOCX publication could not be staged.") from exc
            journal_id = str(uuid.uuid4())
            journal_payload = {
                "kind": "chapter_bookmark_prepare", "book_id": layout.book_id,
                "chapter_id": chapter.chapter_id, "scope_key": scope_key,
                "working_prose_filepath": working_prose_filepath,
                "target_filepath": working_tagged_filepath,
                "old_sha256": old_hash, "new_sha256": new_hash,
                "backup_filepath": backup_path.relative_to(self.root).as_posix(),
                "stage_filepath": stage_path.relative_to(self.root).as_posix(),
                "restore_stage_filepath": restore_path.relative_to(self.root).as_posix(),
                "snapshot_id": snapshot_id,
                "snapshot_prose_filepath": prose_path.relative_to(self.root).as_posix(),
                "snapshot_tagged_filepath": tagged_path.relative_to(self.root).as_posix(),
                "input_tagged_filepath": input_tagged_path.relative_to(self.root).as_posix(),
                "prose_sha256": hashlib.sha256(prose_bytes).hexdigest(),
                "owner_key": owner_key, "project": self.project_name,
                "tool": "audiobook_prepare_chapter", "operation_id": request.operation_id,
                "args_sha256": args_sha,
            }
            try:
                state.begin_publication(journal_id, "chapter_bookmark_prepare", journal_payload)
            except ProjectStateError as exc:
                leftovers = [
                    path.relative_to(self.root).as_posix()
                    for path in (backup_path, stage_path)
                    if path.exists()
                ]
                if leftovers:
                    log.warning(
                        "Bookmark publication journal creation failed; unregistered staged artifacts remain: %s",
                        leftovers,
                    )
                raise BookServiceError("publication_failed", "The bookmark publication journal could not be created.") from exc
            try:
                if _file_sha256(target) != old_hash:
                    raise BookServiceError("stale_source", "The registered tagged DOCX changed before bookmark publication.")
                _require_unlocked(target)
                os.replace(stage_path, target)
                state.advance_publication(journal_id, "published")
            except Exception:
                self.recover_bookmark_publications()
                raise
        try:
            status, revision, committed = state.commit_snapshot(
                snapshot_id=snapshot_id, chapter_id=request.chapter_id,
                scope_key=request.scope.model_dump_json(),
                expected_manifest_revision=request.expected_manifest_revision,
                payload=snapshot_payload, request_plan_sha256=plan_sha,
                receipt_owner_key=owner_key, project=self.project_name,
                tool="audiobook_prepare_chapter", operation_id=request.operation_id,
                args_sha256=args_sha, result=result,
                chunk_lineage=lineage_rows,
                journal_id=journal_id,
            )
        except ProjectStateError as exc:
            if journal_id is not None:
                self.recover_bookmark_publications()
            reason = "duplicate_or_recycled_chunk_id" if "duplicate_or_recycled_chunk_id" in str(exc) else (
                "stale_manifest" if "stale_manifest" in str(exc) else "state_unavailable")
            raise BookServiceError(reason,
                                   "Snapshot publication could not be committed.") from exc
        except Exception:
            if journal_id is not None:
                self.recover_bookmark_publications()
            raise
        if status == "replay":
            return committed, True
        if journal_id is not None:
            self.recover_bookmark_publications()
        self._pending_views.pop(request.document_view_id, None)
        result = committed
        result["manifest_revision"] = revision
        return result, False

    def _selected_chunk_takes(self, state: ProjectState, build: dict[str, Any] | None,
                              *, chapter_id: str, scope_key: str) -> dict[str, str]:
        """Read only the selected take association of one frozen chapter build."""
        if build is None:
            return {}
        stored = state.snapshot(build.get("snapshot_id"))
        if (build.get("scope") != "chapter" or build.get("chapter_id") != chapter_id
                or build.get("scope_key") != scope_key or stored is None
                or stored["chapter_id"] != chapter_id or stored["scope_key"] != scope_key):
            raise BookServiceError("state_unavailable", "The selected chapter build association is inconsistent.")
        chunk_ids = {chunk["chunk_id"] for chunk in stored["payload"]["result"]["chunks"]}
        namespace = json.loads(scope_key)
        selected: dict[str, str] = {}
        for take_id in build.get("input_take_ids", []):
            take = state.take(take_id)
            if (take is None or take.get("chapter_id") != chapter_id or take.get("namespace") != namespace
                    or take.get("chunk_id") not in chunk_ids or take["chunk_id"] in selected):
                raise BookServiceError("state_unavailable", "A selected take does not belong to this chapter build.")
            # Reuse can select a take originating in an older snapshot.
            selected[take["chunk_id"]] = take_id
        return selected

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
        if stored is not None:
            self._authorize_snapshot_read(stored, chapter.chapter_id)
        snap = stored["payload"] if stored else {}
        result_data = snap.get("result", {})
        chunks = [dict(item) for item in result_data.get("chunks", [])]
        head = state.chapter_head(chapter.chapter_id, scope_key)
        accepted_takes = self._selected_chunk_takes(
            state, state.build(head["accepted_build_id"]) if head else None,
            chapter_id=chapter.chapter_id, scope_key=scope_key,
        )
        selected_chunk_ids = {item.get("chunk_id") for item in chunks}
        takes = [item for item in state.takes(chapter_id=chapter.chapter_id)
                 if item.get("namespace") == _data(scope) and item.get("chunk_id") in selected_chunk_ids]
        take_ids_by_chunk: dict[str, list[str]] = {}
        for take in takes:
            take_ids_by_chunk.setdefault(take["chunk_id"], []).append(take["take_id"])
        for chunk in chunks:
            chunk["take_ids"] = take_ids_by_chunk.get(chunk["chunk_id"], [])
            chunk["accepted_take_id"] = accepted_takes.get(chunk["chunk_id"])
        if "chunk_ids" in request.model_fields_set:
            wanted = set(request.chunk_ids)
            chunks = [item for item in chunks if item["chunk_id"] in wanted]
            takes = [item for item in takes if item["chunk_id"] in wanted]
        candidates = state.builds(chapter_id=chapter.chapter_id, scope_key=scope_key)
        source_status = "not_prepared"
        if snapshot_id:
            try:
                if isinstance(scope, dto.ProductionScope):
                    eligible, reason = self._production_snapshot_eligible(state, layout, chapter, stored)
                    source_status = "eligible" if eligible else (
                        "changed" if reason == "source_changed" else "unapproved"
                    )
                else:
                    working_prose, working_tagged = self._snapshot_working_pair(snap, chapter)
                    current = _read_bytes(self.root, working_prose)
                    self._active_test_authorization(
                        layout, chapter, authorization_id=scope.authorization_id,
                        prose_filepath=working_prose, tagged_filepath=working_tagged, prose_bytes=current,
                        selected_ordinals=snap.get("selected_source_ordinals"),
                    )
                    source_status = "eligible" if hashlib.sha256(current).hexdigest() == result_data.get("snapshot_prose_sha256") else "changed"
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
            "head": head,
            "metadata": [(kind, item if isinstance(item, str) else item.get("chunk_id", item.get("take_id")))
                         for kind, item in metadata],
        })
        limit = request.limit if "limit" in request.model_fields_set else 100
        include_text = "include_text" in request.model_fields_set and request.include_text
        text_offset = 0
        try:
            if include_text:
                offset, text_offset = (parse_read_cursor(request.cursor, view)
                                       if "cursor" in request.model_fields_set else (0, 0))
            else:
                offset = _cursor_offset(request.cursor, view) if "cursor" in request.model_fields_set else 0
        except ReadCursorError as exc:
            raise BookServiceError("invalid_cursor", "The chapter cursor is invalid or stale.") from exc
        if offset > len(metadata):
            raise BookServiceError("invalid_cursor", "The chapter cursor is invalid or stale.")
        cap = request.max_characters if "max_characters" in request.model_fields_set else 12000
        if text_offset and (offset == len(metadata) or metadata[offset][0] != "chunk"):
            raise BookServiceError("invalid_cursor", "The chapter text cursor is invalid or stale.")
        page_metadata: list[tuple[str, Any]] = []
        returned_texts: list[dict[str, Any]] = []
        next_offset = offset
        next_text_offset = 0
        remaining = cap
        for kind, item in metadata[offset:offset + limit]:
            if include_text and stored is not None and kind == "chunk":
                speech_text = snap.get("speech_text", "")
                prompt = speech_text[item["start"]:item["end"]]
                try:
                    spoken = spoken_interval(speech_text, snap.get("spoken_projection", ""),
                                             item["start"], item["end"], snap.get("tag_deletion_spans"))
                except ValueError as exc:
                    raise BookServiceError("state_unavailable", "Frozen speech projection is malformed.") from exc
                total = len(prompt) + len(spoken)
                # Budget the prospective pair before publishing its metadata.
                # A later chunk that does not fit starts the next page; an
                # oversized first pair keeps this record cursor until complete.
                if remaining == 0 or (page_metadata and total > remaining):
                    break
                try:
                    prompt_page, spoken_page, pair_end = paired_text_page(prompt, spoken, text_offset, remaining)
                except ValueError as exc:
                    raise BookServiceError("invalid_cursor", "The chapter text cursor is invalid or stale.") from exc
                returned_texts.append({"chunk_id": item["chunk_id"], "prompt": prompt_page,
                                       "spoken_text": spoken_page})
                remaining -= len(prompt_page["text"]) + len(spoken_page["text"])
                page_metadata.append((kind, item))
                if pair_end < total:
                    next_text_offset = pair_end
                    break
                text_offset = 0
            else:
                page_metadata.append((kind, item))
            next_offset += 1
        page_chunks = [dict(item) for kind, item in page_metadata if kind == "chunk"]
        page_takes = [item for kind, item in page_metadata if kind == "take"]
        page_take_ids = {item["take_id"] for item in page_takes}
        for item in page_chunks:
            item["take_ids"] = [take_id for take_id in item.get("take_ids", []) if take_id in page_take_ids]
            item["reusable_take_ids"] = [take_id for take_id in item.get("reusable_take_ids", []) if take_id in page_take_ids]
        page_candidates = [item for kind, item in page_metadata if kind == "candidate"]
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
            "current_outputs_stale": bool(head and (
                not namespace or head["accepted_plan_sha256"] != namespace.get("current_plan_sha256")
                or not head["accepted_plan_matches_prepared"]
            )),
            "source_status": source_status,
            "chunks": page_chunks, "takes": page_takes, "returned_texts": returned_texts,
            "has_more": next_offset < len(metadata),
            "next_cursor": ((read_cursor(view, next_offset, next_text_offset) if include_text
                             else _cursor(view, next_offset)) if next_offset < len(metadata) else None),
        }

    def _working_chunk_bookmark_proof(
        self, stored: dict[str, Any], *, chapter_id: str, scope_key: str,
    ) -> tuple[dict[str, str], dict[str, Any]]:
        """Check current navigation against frozen ranges, never matching by prose."""
        if stored["chapter_id"] != chapter_id or stored["scope_key"] != scope_key:
            raise BookServiceError("state_unavailable", "The current navigation snapshot has an inconsistent namespace.")
        self._authorize_snapshot_read(stored, chapter_id)
        payload = stored["payload"]
        result = payload["result"]
        chunks = result["chunks"]
        identity: dict[str, Any] = {"snapshot_id": result["snapshot_id"], "scope_key": scope_key}
        try:
            frozen_tagged = _read_bytes(self.root, payload["tagged_filepath"])
            frozen_prose = _read_bytes(self.root, payload["prose_filepath"])
            if (hashlib.sha256(frozen_tagged).hexdigest() != result["snapshot_tagged_sha256"]
                    or hashlib.sha256(frozen_prose).hexdigest() != result["snapshot_prose_sha256"]):
                raise ValueError("frozen source identity changed")
            expected = parse_docx(frozen_tagged)
            pair = project_docx_pair(
                frozen_prose, frozen_tagged, explicit_tag_spans=payload.get("explicit_tag_spans", []),
            )
            expected_ordinals = {paragraph.paragraph_id: paragraph.source_ordinal for paragraph in pair.paragraphs}
            expected_marks = {mark.name: mark for mark in expected.bookmarks}
        except (BookServiceError, OSError, ValueError, KeyError, TypeError) as exc:
            raise BookServiceError("state_unavailable", "The frozen current bookmark source could not be verified.") from exc
        try:
            _, _, layout = self._enabled_layout()
            chapter = self._chapter(layout, chapter_id)
            working_pair = self._snapshot_working_pair(payload, chapter)
            if json.loads(scope_key) == {"kind": "production"} and working_pair != (
                    chapter.working_filepath, chapter.tagged_filepath):
                raise BookServiceError("stale_source", "The current navigation working pair is no longer registered.")
            identity["working_tagged_filepath"] = working_pair[1]
            working_raw = _read_bytes(self.root, working_pair[1])
            identity["working_tagged_sha256"] = hashlib.sha256(working_raw).hexdigest()
            working = parse_docx(working_raw)
        except (BookServiceError, OSError, ValueError) as exc:
            reason = exc.reason if isinstance(exc, BookServiceError) else type(exc).__name__
            identity["check_error"] = reason
            status = "missing" if reason == "file_not_found" else "not_checked"
            return {chunk["chunk_id"]: status for chunk in chunks}, identity
        working_marks = {mark.name: mark for mark in working.bookmarks}
        checked: dict[str, str] = {}
        for chunk in chunks:
            segments = chunk.get("source_segments", [])
            present = bool(segments)
            for segment in segments:
                mark = expected_marks.get(segment.get("bookmark"))
                ordinal = expected_ordinals.get(segment.get("paragraph_id"))
                if (mark is None or ordinal is None or mark.paragraph_ordinal != ordinal
                        or mark.end_paragraph_ordinal != ordinal
                        or (mark.offset, mark.end_offset) != (segment["start"], segment["end"])):
                    raise BookServiceError("state_unavailable", "A frozen chunk source segment lacks its exact bookmark range.")
                live = working_marks.get(mark.name)
                if live is None or (live.paragraph_ordinal, live.offset, live.end_paragraph_ordinal, live.end_offset) != (
                        mark.paragraph_ordinal, mark.offset, mark.end_paragraph_ordinal, mark.end_offset):
                    present = False
                    continue
                expected_text = expected.paragraphs[ordinal].text[mark.offset:mark.end_offset]
                live_text = working.paragraphs[ordinal].text[live.offset:live.end_offset]
                if hashlib.sha256(live_text.encode("utf-8")).digest() != hashlib.sha256(expected_text.encode("utf-8")).digest():
                    present = False
            checked[chunk["chunk_id"]] = "present" if present else "missing"
        return checked, identity

    @staticmethod
    def _timeline_frame(value: Any, *, field: str) -> int:
        if isinstance(value, bool) or not (
            isinstance(value, int) or (isinstance(value, str) and value.isdecimal())
        ):
            raise BookServiceError("state_unavailable", f"The immutable timeline has an invalid {field}.")
        return int(value)

    def _pinned_chapter_timeline(
        self, state: ProjectState, dependency: dict[str, Any], *, target: dto.ProductionTarget,
        global_start: int = 0, expected_frames: int | None = None,
    ) -> dict[str, Any]:
        """Return one validated immutable chapter timeline bound to a book dependency.

        New book timelines serialize this mapping below their top-level chapter
        entry.  The same reader is intentionally retained for older top-only
        book timelines, whose exact dependency is the only historical fallback.
        """
        chapter_id = dependency.get("chapter_id")
        chapter_build_id = dependency.get("chapter_build_id")
        snapshot_id = dependency.get("snapshot_id")
        if not all(isinstance(value, str) and value for value in (chapter_id, chapter_build_id, snapshot_id)):
            raise BookServiceError("state_unavailable", "A pinned book dependency is malformed.")
        build = state.build(chapter_build_id)
        if (build is None or build.get("scope") != "chapter" or build.get("chapter_id") != chapter_id
                or build.get("snapshot_id") != snapshot_id or not isinstance(build.get("scope_key"), str)):
            raise BookServiceError("state_unavailable", "A pinned chapter build is unavailable or inconsistent.")
        try:
            namespace = json.loads(build["scope_key"])
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise BookServiceError("state_unavailable", "A pinned chapter build namespace is malformed.") from exc
        if namespace != {"kind": "production"}:
            raise BookServiceError("state_unavailable", "A production book dependency has a non-production chapter namespace.")
        selected = self._selected_chunk_takes(
            state, build, chapter_id=chapter_id, scope_key=build["scope_key"],
        )
        output = next((item for item in build.get("result", {}).get("outputs", [])
                       if item.get("kind") == "pcm_master"), None)
        timeline_path = build.get("result", {}).get("timeline_filepath")
        if not isinstance(output, dict) or not isinstance(timeline_path, str):
            raise BookServiceError("state_unavailable", "A pinned chapter build lacks its PCM timeline facts.")
        try:
            timeline = json.loads(_path(self.root, timeline_path).read_text(encoding="utf-8"))
            rate = int(timeline["sample_rate_hz"])
            channels = int(timeline["channels"])
            encoding = timeline["encoding"]
            bits = int(timeline["storage_bits"])
            frame_count = self._timeline_frame(timeline["frame_count"], field="frame_count")
            entries = timeline["entries"]
            media = output["media"]
            media_frame_count = self._timeline_frame(media["frame_count"], field="media frame_count")
        except (BookServiceError, OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
            raise BookServiceError("state_unavailable", "A pinned chapter timeline could not be read.") from exc
        if (not isinstance(entries, list) or rate != target.sample_rate_hz or channels != target.channels
                or encoding != target.encoding or bits != target.storage_bits
                or media_frame_count != frame_count
                or any(media.get(key) != value for key, value in (
                    ("sample_rate_hz", rate), ("channels", channels),
                    ("encoding", encoding), ("storage_bits", bits),
                ))):
            raise BookServiceError("state_unavailable", "Pinned chapter media facts do not match its immutable timeline.")
        if expected_frames is not None and frame_count != expected_frames:
            raise BookServiceError("state_unavailable", "A pinned chapter timeline does not fill its book interval.")
        snapshot = state.snapshot(snapshot_id)
        chunks = (snapshot or {}).get("payload", {}).get("result", {}).get("chunks", [])
        chunk_ids = {item.get("chunk_id") for item in chunks}
        cursor = 0
        child_entries: list[dict[str, Any]] = []
        for entry in entries:
            if not isinstance(entry, dict):
                raise BookServiceError("state_unavailable", "A pinned chapter timeline entry is malformed.")
            source_id, kind = entry.get("source_id"), entry.get("kind")
            start = self._timeline_frame(entry.get("start_frame"), field="entry start")
            end = self._timeline_frame(entry.get("end_frame"), field="entry end")
            if (not isinstance(source_id, str) or not source_id or kind not in {"audio", "silence"}
                    or start != cursor or end <= start):
                raise BookServiceError("state_unavailable", "A pinned chapter timeline has non-contiguous entries.")
            cursor = end
            child = {"source_id": source_id, "kind": kind,
                     "start_frame": str(global_start + start), "end_frame": str(global_start + end)}
            if kind == "audio":
                take_id = selected.get(source_id)
                take = state.take(take_id) if take_id else None
                samples_sha256 = entry.get("source_samples_sha256", entry.get("canonical_sample_sha256"))
                if (source_id not in chunk_ids or take is None
                        or entry.get("source_bytes_sha256") != take.get("bytes_sha256")
                        or samples_sha256 != take.get("media", {}).get("canonical_sample_sha256")):
                    raise BookServiceError("state_unavailable", "A pinned chapter audio entry lacks its selected take facts.")
                child.update({"take_id": take_id,
                              "source_bytes_sha256": entry["source_bytes_sha256"],
                              "source_samples_sha256": samples_sha256})
            child_entries.append(child)
        if cursor != frame_count:
            raise BookServiceError("state_unavailable", "A pinned chapter timeline does not cover its retained PCM master.")
        return {"chapter_id": chapter_id, "chapter_build_id": chapter_build_id,
                "snapshot_id": snapshot_id, "namespace": namespace,
                "child_entries": child_entries}

    def find_chunk(self, request: dto.FindChunkRequest) -> dict[str, Any]:
        state = self.discover_state()
        matches: list[dict[str, Any]] = []
        searched_snapshots: list[str] = []
        current_heads: list[tuple[str, str, dict[str, Any] | None]] = []
        working_checks: list[dict[str, Any]] = []
        navigation_views: list[dict[str, Any]] = []
        navigation_contexts: dict[tuple[str, str], dict[str, Any]] = {}

        def current_mapping(seeds: list[str], chapter_id: str, scope_key: str, accepted_takes: dict[str, str]):
            key = (chapter_id, scope_key)
            if key not in navigation_contexts:
                namespace = state.namespace(chapter_id, scope_key)
                snapshot_id = namespace.get("current_snapshot_id") if namespace else None
                stored = state.snapshot(snapshot_id) if snapshot_id else None
                if stored is not None and (stored["chapter_id"] != chapter_id or stored["scope_key"] != scope_key):
                    raise BookServiceError("state_unavailable", "The prepared navigation namespace is inconsistent.")
                chunks = stored["payload"].get("result", {}).get("chunks", []) if stored else []
                records = state.chunk_lineage(chapter_id=chapter_id, scope_key=scope_key)
                navigation_contexts[key] = {"stored": stored, "chunks": chunks, "records": records}
                navigation_views.append({"chapter_id": chapter_id, "scope_key": scope_key,
                                         "namespace": namespace, "lineage": records})
            context = navigation_contexts[key]
            target_order = list(dict.fromkeys(chunk["chunk_id"] for chunk in context["chunks"]))
            mapped = _lineage_targets(seeds, target_order, context["records"])
            reached = {target for targets in mapped.values() for target in targets}
            current_ids = [target for target in target_order if target in reached]
            lineage = [{"old_chunk_id": seed, "current_chunk_ids": targets}
                       for seed, targets in mapped.items() if targets != [seed]]
            accepted = _lineage_targets(seeds, list(accepted_takes), context["records"], allow_ancestors=True)
            accepted_ids = {target for targets in accepted.values() for target in targets}
            take_ids = [take_id for chunk_id, take_id in accepted_takes.items() if chunk_id in accepted_ids]
            status = "present" if current_ids and all(len(targets) == 1 for targets in mapped.values()) else "missing"
            if any(len(targets) > 1 for targets in mapped.values()):
                status = "ambiguous"
            if status == "present":
                if "bookmarks" not in context:
                    context["bookmarks"], checked_source = self._working_chunk_bookmark_proof(
                        context["stored"], chapter_id=chapter_id, scope_key=scope_key,
                    )
                    working_checks.append(checked_source)
                statuses = [context["bookmarks"].get(chunk_id, "missing") for chunk_id in current_ids]
                if "missing" in statuses:
                    status = "missing"
                elif "not_checked" in statuses:
                    status = "not_checked"
            return current_ids, take_ids, lineage, status

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

            def chapter_mapping(entry: dict[str, Any], start: int, end: int) -> dict[str, Any]:
                """Read nested facts, or the exact retained dependency for old books."""
                chapter_id = entry.get("source_id")
                dependency = next((item for item in build.get("dependencies", [])
                                   if item.get("chapter_id") == chapter_id), None)
                if not isinstance(dependency, dict):
                    raise BookServiceError("state_unavailable", "A book chapter timeline entry has no pinned dependency.")
                child_entries = entry.get("child_entries")
                if child_entries is None:
                    dependency_build = state.build(dependency["chapter_build_id"])
                    pcm_output = next((item for item in (dependency_build or {}).get("result", {}).get("outputs", [])
                                       if item.get("kind") == "pcm_master"), None)
                    if not isinstance(pcm_output, dict):
                        raise BookServiceError("state_unavailable", "A retained book dependency lacks its PCM output facts.")
                    target = dto.ProductionTarget.model_validate({
                        "sample_rate_hz": timeline["sample_rate_hz"], "channels": timeline["channels"],
                        "encoding": timeline["encoding"], "storage_bits": timeline["storage_bits"],
                        "valid_bits": pcm_output["media"]["valid_bits"], "mp3_bitrate_kbps": 192,
                    }, strict=True)
                    return self._pinned_chapter_timeline(
                        state, dependency, target=target, global_start=start, expected_frames=end - start,
                    )
                mapping = {key: entry.get(key) for key in (
                    "chapter_id", "chapter_build_id", "snapshot_id", "namespace",
                )}
                if (mapping["chapter_id"] != dependency.get("chapter_id")
                        or mapping["chapter_build_id"] != dependency.get("chapter_build_id")
                        or mapping["snapshot_id"] != dependency.get("snapshot_id")
                        or mapping["namespace"] != {"kind": "production"}
                        or not isinstance(child_entries, list)):
                    raise BookServiceError("state_unavailable", "A nested book chapter timeline is inconsistent.")
                mapping["child_entries"] = child_entries
                return mapping

            def chapter_context(mapping: dict[str, Any], child: dict[str, Any], *, segment_kind: str) -> None:
                chapter_id = mapping["chapter_id"]
                chapter_build = state.build(mapping["chapter_build_id"])
                if (chapter_build is None or chapter_build.get("scope") != "chapter"
                        or chapter_build.get("chapter_id") != chapter_id
                        or chapter_build.get("snapshot_id") != mapping["snapshot_id"]
                        or json.loads(chapter_build.get("scope_key", "{}")) != mapping["namespace"]):
                    raise BookServiceError("state_unavailable", "A nested book chapter build is unavailable.")
                matched_takes = self._selected_chunk_takes(
                    state, chapter_build, chapter_id=chapter_id, scope_key=chapter_build["scope_key"],
                )
                source_id = child.get("source_id")
                if not isinstance(source_id, str) or child.get("kind") != "audio":
                    raise BookServiceError("state_unavailable", "A nested book timeline lacks a frozen speech entry.")
                if child.get("take_id") != matched_takes.get(source_id):
                    raise BookServiceError("state_unavailable", "A nested book timeline take does not match its pinned chapter build.")
                head = state.chapter_head(chapter_id, chapter_build["scope_key"])
                current_heads.append((chapter_id, chapter_build["scope_key"], head))
                current_takes = self._selected_chunk_takes(
                    state, state.build(head["accepted_build_id"]) if head else None,
                    chapter_id=chapter_id, scope_key=chapter_build["scope_key"],
                )
                if "chapter_id" in request.model_fields_set and request.chapter_id != chapter_id:
                    return
                current_ids, current_take_ids, lineage, mapping_status = current_mapping(
                    [source_id], chapter_id, chapter_build["scope_key"], current_takes,
                )
                matches.append({
                    "chapter_id": chapter_id, "snapshot_id": mapping["snapshot_id"], "chunk_ids": [source_id],
                    "occurrence_start": None, "occurrence_end": None, "coordinate_projection": "timeline",
                    "excerpt": f"{segment_kind}:{source_id}", "matched_build_id": request.query.build_id,
                    "matched_take_ids": [matched_takes[source_id]], "segment_kind": segment_kind,
                    "current_chunk_ids": current_ids, "current_take_ids": current_take_ids,
                    "lineage": lineage, "current_mapping_status": mapping_status, "match_mode": "timestamp",
                })

            def validate_children(mapping: dict[str, Any], start: int, end: int) -> list[dict[str, Any]]:
                cursor = start
                children = mapping["child_entries"]
                for child in children:
                    if not isinstance(child, dict):
                        raise BookServiceError("state_unavailable", "A nested book timeline entry is malformed.")
                    child_start = self._timeline_frame(child.get("start_frame"), field="nested entry start")
                    child_end = self._timeline_frame(child.get("end_frame"), field="nested entry end")
                    if child_start != cursor or child_end <= child_start or child_end > end:
                        raise BookServiceError("state_unavailable", "Nested book timeline entries do not fill their chapter interval.")
                    cursor = child_end
                if cursor != end:
                    raise BookServiceError("state_unavailable", "Nested book timeline entries do not cover their chapter interval.")
                return children

            for position, entry in enumerate(entries):
                start_frame, end_frame = int(entry["start_frame"]), int(entry["end_frame"])
                if start_frame <= frame < end_frame:
                    if build.get("scope") == "chapter":
                        selected = self._selected_chunk_takes(
                            state, build, chapter_id=build["chapter_id"], scope_key=build["scope_key"],
                        )
                        direct_mapping = {"chapter_id": build["chapter_id"], "chapter_build_id": build["build_id"],
                                          "snapshot_id": build["snapshot_id"],
                                          "namespace": json.loads(build["scope_key"])}

                        def direct_speech(value: dict[str, Any]) -> dict[str, Any]:
                            result = dict(value)
                            result["take_id"] = selected.get(result.get("source_id"))
                            return result

                        if entry.get("kind") == "audio":
                            chapter_context(direct_mapping, direct_speech(entry), segment_kind="speech")
                        else:
                            for candidates in (entries[:position][::-1], entries[position + 1:]):
                                neighbour = next((candidate for candidate in candidates
                                                  if candidate.get("kind") == "audio"), None)
                                if neighbour is not None:
                                    chapter_context(direct_mapping, direct_speech(neighbour), segment_kind="silence")
                        continue
                    if entry.get("kind") == "audio":
                        mapping = chapter_mapping(entry, start_frame, end_frame)
                    else:
                        mapping = None
                    if mapping is not None:
                        children = validate_children(mapping, start_frame, end_frame)
                        child = next((item for item in children
                                      if int(item["start_frame"]) <= frame < int(item["end_frame"])), None)
                        if child is None:
                            raise BookServiceError("state_unavailable", "A nested chapter interval could not be resolved.")
                        if child["kind"] == "audio":
                            chapter_context(mapping, child, segment_kind="speech")
                        else:
                            child_position = children.index(child)
                            for candidates in (children[:child_position][::-1], children[child_position + 1:]):
                                neighbour = next((candidate for candidate in candidates
                                                  if candidate.get("kind") == "audio"), None)
                                if neighbour is not None:
                                    chapter_context(mapping, neighbour, segment_kind="silence")
                    else:
                        neighbours: list[tuple[dict[str, Any], dict[str, Any]]] = []
                        for direction in (-1, 1):
                            index = position + direction
                            while 0 <= index < len(entries):
                                candidate = entries[index]
                                if candidate.get("kind") == "audio":
                                    candidate_start, candidate_end = int(candidate["start_frame"]), int(candidate["end_frame"])
                                    candidate_mapping = chapter_mapping(candidate, candidate_start, candidate_end)
                                    candidate_children = validate_children(candidate_mapping, candidate_start, candidate_end)
                                    speech = next((value for value in (
                                        candidate_children[::-1] if direction < 0 else candidate_children
                                    ) if value.get("kind") == "audio"), None)
                                    if speech is not None:
                                        neighbours.append((candidate_mapping, speech))
                                    break
                                index += direction
                        for candidate_mapping, speech in neighbours:
                            chapter_context(candidate_mapping, speech, segment_kind="silence")
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
                search_scope_key = stored["scope_key"]
                historical_builds = state.builds(chapter_id=chapter_id, scope_key=search_scope_key)
                matched = next((build for build in historical_builds
                                if build.get("snapshot_id") == snapshot_id and build.get("was_accepted")), None)
                matched_build_id = matched.get("build_id") if matched else None
                matched_takes = self._selected_chunk_takes(
                    state, matched, chapter_id=chapter_id, scope_key=search_scope_key,
                )
                head = state.chapter_head(chapter_id, search_scope_key)
                current_heads.append((chapter_id, search_scope_key, head))
                accepted_takes = self._selected_chunk_takes(
                    state, state.build(head["accepted_build_id"]) if head else None,
                    chapter_id=chapter_id, scope_key=search_scope_key,
                )
                searched_snapshots.append(snapshot_id)
                snapshot = stored["payload"]
                text = snapshot.get("spoken_projection", "")
                speech_text = snapshot.get("speech_text", "")
                start = 0
                while query and (at := text.find(query, start)) >= 0:
                    end = at + len(query)
                    if ((before is None or text[max(0, at-len(before)):at] == before)
                            and (after is None or text[end:end+len(after)] == after)):
                        chunks = snapshot.get("result", {}).get("chunks", [])
                        try:
                            overlapping = []
                            for chunk in chunks:
                                spoken_start, spoken_end = spoken_coordinates(
                                    speech_text, text, chunk["start"], chunk["end"],
                                    snapshot.get("tag_deletion_spans"),
                                )
                                if spoken_start < end and spoken_end > at:
                                    overlapping.append(chunk["chunk_id"])
                        except (KeyError, TypeError, ValueError) as exc:
                            raise BookServiceError(
                                "state_unavailable",
                                "The frozen spoken projection could not be mapped to its prepared chunks.",
                            ) from exc
                        matching_takes = [take_id for chunk_id, take_id in matched_takes.items() if chunk_id in overlapping]
                        current_ids, current_take_ids, lineage, mapping_status = current_mapping(
                            overlapping, chapter_id, search_scope_key, accepted_takes,
                        )
                        matches.append({
                            "chapter_id": chapter_id, "snapshot_id": snapshot_id,
                            "chunk_ids": overlapping, "occurrence_start": at,
                            "occurrence_end": end, "coordinate_projection": "spoken_text_codepoints",
                            "excerpt": text[max(0, at-80):min(len(text), end+80)],
                            "matched_build_id": matched_build_id, "matched_take_ids": matching_takes,
                            "segment_kind": "speech", "current_chunk_ids": current_ids,
                            "current_take_ids": current_take_ids, "lineage": lineage,
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
            "current_heads": current_heads,
            "working_checks": working_checks,
            "navigation_views": navigation_views,
            "mapping": [
                (match["snapshot_id"], match["chunk_ids"], match["current_chunk_ids"],
                 match["current_take_ids"], match["current_mapping_status"])
                for match in matches
            ],
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
        _validate_request_spec_durable_fields(change.request.spec)
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
                args_sha256=args_sha256, expected_manifest_revision=change.expected_manifest_revision,
                request_plan_sha256=snapshot["result"]["request_plan_sha256"],
            )
        except ProjectStateError as exc:
            if "operation_id_conflict" in str(exc):
                raise BookServiceError("operation_id_conflict", "The operation ID was used with different arguments.") from exc
            if str(exc) == "stale_manifest":
                raise BookServiceError("stale_manifest", "The chapter manifest changed before generation reservation.") from exc
            raise BookServiceError("state_unavailable", "Generation state could not be persisted.") from exc
        return result, disposition == "replay"

    def _update_generation(self, change, state, request, owner_key: str, args_sha256: str) -> tuple[dict[str, Any], bool]:
        if "provider_ids" in change.model_fields_set:
            _validate_durable_provider_facts(_data(change.provider_ids), key="provider_ids")
        if "provider_response_metadata" in change.model_fields_set:
            _validate_durable_provider_facts(
                _data(change.provider_response_metadata), key="provider_response_metadata",
            )
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
        staging_root = self.root / STATE_ROOT / "audiobook-staging" / job_id
        staging_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        staging = staging_root / "source.part"
        extensions = {"raw_pcm": "pcm", "headered_pcm": "wav", "mp3": "mp3"}
        try:
            extension = extensions[media_kind]
        except KeyError as exc:
            raise BookServiceError("unsupported_media", "The imported media kind is not supported.") from exc
        relative = f"{chapter.audio_root}/takes/{take_id}/native.{extension}"
        target = _path(self.root, relative, allow_missing=True)
        return staging, target, relative

    def _media_free_bytes(self, layout) -> int:
        """Return the minimum available space across media and staging devices."""
        locations = [self.root / STATE_ROOT / "audiobook-staging", *self._registered_audio_roots(layout)]
        free_by_device: dict[int, int] = {}
        for location in locations:
            probe = location
            while not probe.exists() and probe != probe.parent:
                probe = probe.parent
            try:
                usage = shutil.disk_usage(probe)
                free_by_device[os.stat(probe).st_dev] = usage.free
            except OSError as exc:
                raise BookServiceError("insufficient_storage", "Audiobook staging storage is unavailable.") from exc
        return min(free_by_device.values(), default=0)

    def _check_media_budget(self, layout, *, incoming_retained_bytes: int = 0,
                            additional_free_bytes: int = 0) -> None:
        """Check current union-root quota and device reserve without double-counting staged files."""
        retained_bytes = self._retained_audio_bytes(self._registered_audio_roots(layout))
        if retained_bytes + incoming_retained_bytes > layout.storage.quota_bytes:
            raise BookServiceError("insufficient_storage", "The operation would exceed the configured audiobook media quota.")
        if self._media_free_bytes(layout) - layout.storage.reserve_bytes < additional_free_bytes:
            raise BookServiceError("insufficient_storage", "The configured media reserve leaves insufficient free space.")

    @staticmethod
    def _bounded_mp3_bytes(*, frame_count: int, sample_rate_hz: int, bitrate_kbps: int) -> int:
        """Bound a CBR listening file plus container/metadata headroom before encoding."""
        audio = (frame_count * bitrate_kbps * 1000 + (sample_rate_hz * 8 - 1)) // (sample_rate_hz * 8)
        return audio + 1_048_576

    def _check_pcm_build_preflight(self, layout, sources: list[PcmSource], gaps: list[dto.SilenceGap],
                                   target: dto.ProductionTarget, *, emit_mp3: bool) -> None:
        timeline = plan_pcm_timeline(sources, gaps, target)
        frame_count = int(timeline.frame_count)
        pcm_bytes = frame_count * target.channels * (target.storage_bits // 8)
        mp3_bytes = (self._bounded_mp3_bytes(
            frame_count=frame_count, sample_rate_hz=target.sample_rate_hz,
            bitrate_kbps=target.mp3_bitrate_kbps,
        ) if emit_mp3 else 0)
        # ``os.replace`` renames the same-filesystem PCM stage into place, so
        # it does not allocate a second master. The peak is one master plus
        # the bounded listening output and immutable fact files.
        facts_bytes = 1_048_576
        self._check_media_budget(
            layout, incoming_retained_bytes=pcm_bytes + mp3_bytes + facts_bytes,
            additional_free_bytes=pcm_bytes + mp3_bytes + facts_bytes,
        )

    def _check_mp3_build_preflight(self, layout, sources: list[Mp3Source]) -> None:
        output_bytes = sum(source.inspection.size_bytes for source in sources)
        facts_bytes = 1_048_576
        self._check_media_budget(
            layout, incoming_retained_bytes=output_bytes + facts_bytes,
            additional_free_bytes=output_bytes + facts_bytes,
        )

    def _finish_build_success(self, state: ProjectState, job_id: str, build: dict,
                              *, before_finalize: Callable[[], None] | None,
                              after_finalize: Callable[[], None] | None,
                              cleanup: Callable[[], None] | None = None) -> None:
        acquired = False
        try:
            if before_finalize is not None:
                before_finalize()
                acquired = True
            _, _, current_layout = self._enabled_layout()
            # Build files already lie under registered roots, so inventory is
            # the exact final retained footprint and must not be added again.
            self._check_media_budget(current_layout)
            state.finish_build_success(job_id=job_id, build=build)
        except BaseException:
            if cleanup is not None:
                cleanup()
            raise
        finally:
            if acquired and after_finalize is not None:
                after_finalize()

    def _available_import_space(self, layout, chapter, source_size: int) -> None:
        """Admit source staging before detected media facts are available."""
        del chapter
        self._check_media_budget(
            layout, incoming_retained_bytes=source_size, additional_free_bytes=source_size * 3,
        )

    def _registered_audio_roots(self, layout) -> list[Path]:
        roots = [
            _path(self.root, layout.shared_paths.book_audio_root, allow_missing=True),
            *(_path(self.root, item.audio_root, allow_missing=True) for item in layout.chapters),
        ]
        canonical: list[Path] = []
        for root in sorted({item.resolve(strict=False) for item in roots}, key=lambda item: len(item.parts)):
            if not any(root == parent or parent in root.parents for parent in canonical):
                canonical.append(root)
        return canonical

    @staticmethod
    def _retained_audio_bytes(roots: list[Path]) -> int:
        retained = 0
        for audio_root in roots:
            if not audio_root.exists():
                continue
            for current, directories, filenames in os.walk(audio_root, followlinks=False):
                current_path = Path(current)
                directories[:] = [name for name in directories if not (current_path / name).is_symlink()]
                for name in filenames:
                    candidate = current_path / name
                    try:
                        facts = candidate.lstat()
                    except OSError as exc:
                        raise BookServiceError(
                            "source_unavailable", "Existing audiobook storage could not be inventoried."
                        ) from exc
                    if stat.S_ISREG(facts.st_mode) and not stat.S_ISLNK(facts.st_mode):
                        retained += facts.st_size
        return retained

    def source_stage_policy(self, job_id: str) -> tuple[Path, int, int]:
        """Create one exclusive UUID-owned stage and calculate its current budget."""
        job = self._state_required().import_job(job_id)
        if job is None or job["state"] != "running":
            raise BookServiceError("job_failed", "The import no longer owns a running job.")
        _, _, layout = self._enabled_layout()
        chapter = self._chapter(layout, job["payload"]["chapter_id"])
        self._available_import_space(layout, chapter, 1)
        retained = self._retained_audio_bytes(self._registered_audio_roots(layout))
        free_bytes = max(0, self._media_free_bytes(layout) - layout.storage.reserve_bytes)
        quota_bytes = max(0, layout.storage.quota_bytes - retained)
        from ..bridge import MAX_TRANSFER_BYTES
        # Source, canonical raw normalization and lossless wrapper can overlap;
        # final detected facts replace this upper bound before publication.
        max_bytes = min(MAX_TRANSFER_BYTES, free_bytes // 4, quota_bytes)
        if max_bytes < 1:
            raise BookServiceError("insufficient_storage", "No safe source-import storage budget is available.")
        base = (self.root / STATE_ROOT / "audiobook-staging").resolve(strict=False)
        base.mkdir(mode=0o700, parents=True, exist_ok=True)
        root = base / job_id
        try:
            root.mkdir(mode=0o700)
        except FileExistsError as exc:
            raise BookServiceError("job_failed", "The import staging directory is already owned.") from exc
        resolved = root.resolve(strict=True)
        if resolved.parent != base.resolve(strict=True) or root.is_symlink():
            raise BookServiceError("permission_denied", "The import staging directory is unsafe.")
        return resolved, max_bytes, layout.storage.reserve_bytes

    def import_https_hosts(self) -> tuple[str, ...]:
        _, _, layout = self._enabled_layout()
        return tuple(layout.storage.import_https_hosts)

    def claim_import_job(self, job_id: str) -> dict[str, Any] | None:
        state = self._state_required()
        claimed = state.claim_import_job(job_id)
        if claimed is not None:
            self._active_import_jobs.add(job_id)
            return claimed
        current = state.import_job(job_id)
        if current is not None and current["state"] == "cancel_requested":
            state.finish_import_failure(
                job_id=job_id, reason="cancelled",
                message="The import was cancelled before it started.", cancelled=True,
            )
        return None

    def finish_staging_failure(
        self, job_id: str, reason: str, message: str, *, cancelled: bool = False,
    ) -> None:
        self._state_required().finish_import_failure(
            job_id=job_id, reason=reason, message=message, cancelled=cancelled,
        )
        self._active_import_jobs.discard(job_id)

    def _cleanup_import_stage(self, job_id: str) -> list[str]:
        try:
            canonical_id = str(uuid.UUID(job_id))
        except (ValueError, TypeError, AttributeError):
            return [job_id]
        base = (self.root / STATE_ROOT / "audiobook-staging").resolve(strict=False)
        exact_root = base / canonical_id
        leftovers: list[str] = []
        try:
            if exact_root.resolve(strict=False).parent != base or exact_root.is_symlink():
                return [str(exact_root)]
            if exact_root.exists():
                allowed = {"source.part", "source.wav.part", "source.canonical.pcm"}
                for item in exact_root.iterdir():
                    if (item.name.startswith(".cognita-book-source-") or item.name in allowed):
                        try:
                            facts = item.lstat()
                            if stat.S_ISREG(facts.st_mode) and not stat.S_ISLNK(facts.st_mode):
                                item.unlink()
                            else:
                                leftovers.append(str(item))
                        except OSError:
                            leftovers.append(str(item))
                    else:
                        leftovers.append(str(item))
                if not leftovers:
                    exact_root.rmdir()
            # Existing project-file jobs used this exact job-derived filename.
            legacy_part = base / f"{canonical_id}.part"
            try:
                facts = legacy_part.lstat()
            except FileNotFoundError:
                pass
            else:
                if stat.S_ISREG(facts.st_mode) and not stat.S_ISLNK(facts.st_mode):
                    legacy_part.unlink()
                else:
                    leftovers.append(str(legacy_part))
        except OSError:
            leftovers.append(str(exact_root))
        return leftovers

    def recover_interrupted_import_jobs(self) -> None:
        """Terminally recover every queued/running/cancel-requested durable row."""
        state = self.discover_state()
        if state is None:
            return
        self.state = state
        for job in state.unfinished_import_jobs():
            job_id = job["job_id"]
            interrupted_cancel = job["state"] == "cancel_requested"
            source_kind = job["payload"].get("source_kind")
            reason = (
                "cancelled" if interrupted_cancel else
                "source_unavailable" if source_kind in {"workspace", "https_url"} else
                "job_failed"
            )
            leftovers = self._cleanup_import_stage(job_id)
            self._discard_unregistered_import_artifacts(job)
            state.finish_import_failure(
                job_id=job_id, reason=reason,
                message="The interrupted import was not resumed after restart.",
                cancelled=interrupted_cancel,
            )
            if leftovers:
                log.error(
                    "Interrupted audiobook import staging cleanup left project=%s job=%s paths=%s",
                    self.project_name, job_id, ",".join(leftovers),
                )

    def import_audio(self, request: dto.ImportAudioRequest, *, owner_key: str) -> tuple[dict[str, Any], bool]:
        """Reserve a durable guarded import; worker execution is separate.

        Transient URL material contributes to the private receipt digest only;
        the durable job retains source kind and guarded relative identity/hash.
        """
        state, _, layout = self._enabled_layout()
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
        # Local path/hash preflight is deliberately after receipt lookup. A
        # replay never touches the source filesystem. Network and Workspace
        # authority are checked only by their post-reservation staging seams.
        if isinstance(request.source, dto.ProjectAudioSource):
            try:
                source_path = _path(self.root, request.source.filepath)
                facts = source_path.stat(follow_symlinks=False)
                if not stat.S_ISREG(facts.st_mode) or stat.S_ISLNK(facts.st_mode):
                    raise BookServiceError("source_unavailable", "The project-file source is not a regular file.")
                digest = hashlib.sha256()
                with source_path.open("rb") as stream:
                    while block := stream.read(1024 * 1024):
                        digest.update(block)
                after = source_path.stat(follow_symlinks=False)
            except BookServiceError:
                raise
            except OSError as exc:
                raise BookServiceError("source_unavailable", "The project-file source is unavailable.") from exc
            if (facts.st_dev, facts.st_ino, facts.st_size, facts.st_mtime_ns) != (
                after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns
            ):
                raise BookServiceError("stale_file", "The project-file source changed during validation.")
            if digest.hexdigest() != request.source.expected_sha256:
                raise BookServiceError("media_mismatch", "The project-file source does not match its expected hash.")
        elif isinstance(request.source, dto.HttpsAudioSource):
            from urllib.parse import urlsplit
            parsed = urlsplit(request.source.url)
            host = (parsed.hostname or "").casefold().rstrip(".")
            if (parsed.scheme != "https" or not host or parsed.username or parsed.password
                    or host not in set(layout.storage.import_https_hosts)):
                raise BookServiceError("source_forbidden", "The source URL is not on the authorized HTTPS import allowlist.")
        stored_scope = generation["scope"]
        scope = json.loads(stored_scope) if isinstance(stored_scope, str) else stored_scope
        if scope.get("kind") == "production" and request.provenance != "native_generation":
            raise BookServiceError("native_pcm_required", "Production imports require verified native-generation PCM.")
        if request.provenance in {"test_mp3", "derived_audio"} and scope.get("kind") != "test":
            raise BookServiceError("permission_denied", "Lossy and derived media are available only in an authorized test namespace.")
        stored_snapshot = state.snapshot(generation["snapshot_id"])
        if stored_snapshot is None:
            raise BookServiceError("snapshot_not_found", "The generation snapshot is unavailable.")
        self._authorize_snapshot_read(stored_snapshot, generation["chapter_id"])
        if raw_format is not None and not self._metadata_contains(
            generation["provider_response_metadata"], raw_format.provider_format_evidence
        ):
            raise BookServiceError("media_mismatch", "RawFormat provider evidence is not present in saved generation evidence.")
        job_id, take_id = str(uuid.uuid4()), str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()
        # The exact HTTPS URL is represented by the operation digest only and
        # never enters this durable job payload.
        source_fields: dict[str, Any] = {"source_kind": request.source.kind}
        expected_source_hash = (
            request.source.expected_sha256
            if "expected_sha256" in request.source.model_fields_set else None
        )
        if isinstance(request.source, dto.ProjectAudioSource):
            source_fields["source_filepath"] = request.source.filepath
        elif isinstance(request.source, dto.WorkspaceAudioSource):
            source_fields["workspace_path"] = request.source.path
        pinned = {
            "generation_record_id": generation["generation_record_id"],
            "generation_revision": generation["generation_revision"],
            "chapter_id": generation["chapter_id"], "snapshot_id": generation["snapshot_id"],
            "chunk_id": generation["chunk_id"], "request_sha256": generation["request_sha256"],
            **source_fields, "expected_sha256": expected_source_hash,
            "provenance": request.provenance,
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

    def _adopt_staged_audio(
        self, staged: StagedAudioSource, staging: Path, staging_root: Path,
        *, expected_sha256: str | None, job_id: str,
    ) -> None:
        root = staging_root.resolve(strict=True)
        expected_root = (self.root / STATE_ROOT / "audiobook-staging" / job_id).resolve(strict=True)
        source = staged.staged_path
        try:
            before = source.lstat()
        except OSError as exc:
            raise BookServiceError("source_unavailable", "The verified audio stage is unavailable.") from exc
        if (root != expected_root or source.parent.resolve(strict=True) != root
                or source.is_symlink() or not stat.S_ISREG(before.st_mode)
                or before.st_size < 1 or before.st_size != staged.size_bytes):
            raise BookServiceError("source_unavailable", "The verified audio stage is unsafe.")
        if expected_sha256 is not None and staged.bytes_sha256 != expected_sha256:
            raise BookServiceError("media_mismatch", "The imported bytes do not match expected_sha256.")
        digest, copied = hashlib.sha256(), 0
        try:
            with source.open("rb") as incoming, staging.open("xb") as outgoing:
                while block := incoming.read(1024 * 1024):
                    current = self._state_required().import_job(job_id)
                    if current is None or current["state"] == "cancel_requested":
                        raise BookServiceError("cancelled", "The import was cancelled.")
                    digest.update(block)
                    copied += len(block)
                    outgoing.write(block)
                outgoing.flush()
                os.fsync(outgoing.fileno())
        except FileExistsError as exc:
            raise BookServiceError("job_failed", "The owned import staging file already exists.") from exc
        after = source.stat(follow_symlinks=False)
        actual = digest.hexdigest()
        if ((before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
                != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
                or copied != staged.size_bytes or actual != staged.bytes_sha256
                or (expected_sha256 is not None and actual != expected_sha256)):
            raise BookServiceError("media_mismatch", "The verified audio stage changed before adoption.")

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
        claimed_job: dict[str, Any] | None = None,
        staged_source: StagedAudioSource | None = None,
        source_staging_root: Path | None = None,
    ) -> None:
        """Run one owned import outside SQLite transactions and finalize atomically."""
        state = self._state_required()
        claimed = claimed_job if claimed_job is not None else state.claim_import_job(job_id)
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
            if staged_source is None:
                if pinned.get("source_kind") != "project_file":
                    raise BookServiceError("source_unavailable", "The transient audio source was not staged.")
                source = _path(self.root, pinned["source_filepath"])
                source_size = source.stat(follow_symlinks=False).st_size
            else:
                if (source_staging_root is None
                        or staged_source.source_kind != ("https" if pinned["source_kind"] == "https_url" else pinned["source_kind"])):
                    raise BookServiceError("source_unavailable", "The verified audio stage does not match its reservation.")
                source_size = staged_source.size_bytes
            self._available_import_space(layout, chapter, source_size)
            # The staging object is deliberately independent of its eventual
            # extension; detected facts below choose the immutable take name.
            staging, _, _ = self._import_paths(
                layout, chapter, job_id, pinned["take_id"], media_kind="raw_pcm"
            )
            if staged_source is None:
                self._stream_project_audio(
                    pinned["source_filepath"], staging, pinned["expected_sha256"],
                    source_size=source_size, job_id=job_id,
                )
            else:
                self._adopt_staged_audio(
                    staged_source, staging, source_staging_root,
                    expected_sha256=pinned.get("expected_sha256"), job_id=job_id,
                )
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
            # The finalization lock serializes media publication with other
            # project writes. Reload current storage policy only; the frozen
            # generation remains historically admissible after plan changes.
            _, _, current_layout = self._enabled_layout()
            current_generation = state.generation(pinned["generation_record_id"])
            if (current_generation is None
                    or current_generation.get("import_job_id") != job_id
                    or current_generation.get("snapshot_id") != pinned["snapshot_id"]
                    or current_generation.get("chunk_id") != pinned["chunk_id"]
                    or current_generation.get("request_sha256") != pinned["request_sha256"]
                    or current_generation.get("scope") != generation.get("scope")):
                raise ProjectStateError("import_reservation_lost")
            authorized_snapshot = state.snapshot(pinned["snapshot_id"])
            if authorized_snapshot is None:
                raise BookServiceError("snapshot_not_found", "The generation snapshot is unavailable.")
            self._authorize_snapshot_read(authorized_snapshot, pinned["chapter_id"])
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
            take_fact_value = {"schema_version": 1, "take": take}
            wrapper_bytes = 0 if wrapper_staging is None else wrapper_staging.stat().st_size
            self._check_media_budget(
                current_layout,
                incoming_retained_bytes=inspection.size_bytes + wrapper_bytes + len(_immutable_json_bytes(take_fact_value)),
                additional_free_bytes=len(_immutable_json_bytes(take_fact_value)),
            )
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            if target.exists() or (wrapper_target is not None and wrapper_target.exists()):
                raise BookServiceError("job_failed", "The immutable take path already exists.")
            os.replace(staging, target)
            staging = None
            published_paths.append(target)
            if wrapper_staging is not None and wrapper_target is not None:
                os.replace(wrapper_staging, wrapper_target)
                published_paths.append(wrapper_target)
            take_fact = target.parent / "take.json"
            _write_immutable_json(take_fact, take_fact_value)
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
        state, _, layout = self._enabled_layout()
        args_sha256 = canonical_json_sha256(_data(request))
        # The authenticated dispatch gate still runs on every call. Resolve
        # durable replay before any variant checks mutable heads/plans/media.
        prior = state.receipt(
            owner_key=owner_key, project=self.project_name,
            tool="audiobook_build", operation_id=request.operation_id,
        )
        if prior is not None:
            if prior[0] != args_sha256:
                raise BookServiceError("operation_id_conflict", "This operation ID was used with different arguments.")
            return prior[1], True
        if isinstance(request.input, dto.BookBuildInput):
            return self._reserve_book_build(request, owner_key=owner_key)
        if not isinstance(request.input, dto.ChapterBuildInput):
            raise BookServiceError("unsupported_build_mode", "The requested assembly input is unavailable.")
        if request.mode == "test_mp3_stream_copy":
            return self._reserve_test_mp3_build(request, owner_key=owner_key)
        if request.mode != "production_pcm" or not request.outputs.master:
            raise BookServiceError("unsupported_build_mode", "This checkpoint assembles retained chapter PCM masters only.")
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
        if scope.get("kind") == "production":
            eligible, reason = self._production_snapshot_eligible(state, layout, chapter, stored)
            if (not eligible or namespace.get("current_snapshot_id") != item.snapshot_id
                    or namespace.get("current_plan_sha256") != item.request_plan_sha256):
                raise BookServiceError("stale_dependency", f"The production chapter plan is no longer eligible ({reason}).")
        else:
            working_prose, working_tagged = self._snapshot_working_pair(snapshot, chapter)
            self._active_test_authorization(
                layout, chapter, authorization_id=scope.get("authorization_id"),
                prose_filepath=working_prose, tagged_filepath=working_tagged,
                prose_bytes=_read_bytes(self.root, working_prose),
                selected_ordinals=stored["payload"].get("selected_source_ordinals"),
            )
        if [value.chunk_id for value in item.takes] != [value.get("chunk_id") for value in chunks]:
            raise BookServiceError("coverage_incomplete", "Build takes must cover frozen chunks exactly once in frozen order.")
        source_take_ids: list[str] = []
        sources: list[PcmSource] = []
        target: dto.ProductionTarget | None = None
        for supplied, frozen in zip(item.takes, chunks, strict=True):
            take = state.take(supplied.take_id)
            if take is None or (
                take.get("chapter_id") != chapter.chapter_id or take.get("namespace") != scope
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

    def _reserve_test_mp3_build(self, request: dto.BuildRequest, *, owner_key: str) -> tuple[dict[str, Any], bool]:
        """Pin an explicitly test-only, packet-copy chapter candidate."""
        state, _, layout = self._enabled_layout()
        item = request.input
        assert isinstance(item, dto.ChapterBuildInput)
        if request.outputs.master or request.gaps:
            raise BookServiceError("unsupported_build_mode", "Test MP3 stream-copy has no PCM master, bitrate conversion, or silence gaps.")
        stored = state.snapshot(item.snapshot_id)
        if stored is None or stored["chapter_id"] != item.chapter_id or stored["manifest_revision"] != item.expected_manifest_revision:
            raise BookServiceError("stale_manifest", "The requested frozen chapter snapshot is unavailable.")
        scope = json.loads(stored["scope_key"])
        if scope.get("kind") != "test":
            raise BookServiceError("permission_denied", "MP3 stream-copy is available only in an authorized test namespace.")
        chapter = self._chapter(layout, item.chapter_id)
        working_prose, working_tagged = self._snapshot_working_pair(stored["payload"], chapter)
        self._active_test_authorization(
            layout, chapter, authorization_id=scope.get("authorization_id"),
            prose_filepath=working_prose, tagged_filepath=working_tagged,
            prose_bytes=_read_bytes(self.root, working_prose),
            selected_ordinals=stored["payload"].get("selected_source_ordinals"),
        )
        namespace = state.namespace(item.chapter_id, stored["scope_key"])
        if namespace is None or namespace.get("head_revision") != request.expected_head_revision:
            raise BookServiceError("stale_head", "The chapter head changed before build admission.")
        snapshot = stored["payload"]["result"]
        if snapshot.get("request_plan_sha256") != item.request_plan_sha256 or [v.chunk_id for v in item.takes] != [v["chunk_id"] for v in snapshot["chunks"]]:
            raise BookServiceError("coverage_incomplete", "Test MP3 takes must cover frozen chunks exactly once in order.")
        take_ids: list[str] = []
        source_bitrate: int | None = None
        for supplied, frozen in zip(item.takes, snapshot["chunks"], strict=True):
            take = state.take(supplied.take_id)
            media = None if take is None else take.get("media", {})
            if take is None or take.get("chapter_id") != item.chapter_id or take.get("namespace") != scope or take.get("chunk_id") != supplied.chunk_id or take.get("request_sha256") != supplied.request_sha256 or frozen.get("request_sha256") != supplied.request_sha256 or media.get("codec") != "mp3" or media.get("encoding") != "compressed":
                raise BookServiceError("stale_dependency", "A selected test take is not a matching verified MP3 chunk.")
            bitrate = media.get("bitrate_bps")
            if not isinstance(bitrate, int) or bitrate <= 0 or bitrate % 1000:
                raise BookServiceError("media_mismatch", "Test MP3 takes require an exact verified source bitrate.")
            if source_bitrate is None:
                source_bitrate = bitrate // 1000
            elif source_bitrate != bitrate // 1000:
                raise BookServiceError("media_mismatch", "Test MP3 takes must have one matching source bitrate.")
            take_ids.append(supplied.take_id)
        if "mp3_bitrate_kbps" in request.outputs.model_fields_set and request.outputs.mp3_bitrate_kbps != source_bitrate:
            raise BookServiceError("settings_mismatch", "Test MP3 stream-copy cannot change the verified source bitrate.")
        pinned = {"mode": "test_mp3_stream_copy", "chapter_id": item.chapter_id, "scope_key": stored["scope_key"],
                  "snapshot_id": item.snapshot_id, "manifest_revision": item.expected_manifest_revision,
                  "request_plan_sha256": item.request_plan_sha256, "take_ids": take_ids,
                  "chunk_ids": [v.chunk_id for v in item.takes], "source_bitrate_kbps": source_bitrate, "metadata": _data(request.metadata)}
        try:
            disposition, result = state.reserve_build_job(job={"job_id": str(uuid.uuid4()), "payload": pinned, "created_at": datetime.now(timezone.utc).isoformat(), "pinned_inputs_sha256": canonical_json_sha256(pinned)}, owner_key=owner_key, project=self.project_name, operation_id=request.operation_id, args_sha256=canonical_json_sha256(_data(request)))
        except ProjectStateError as exc:
            raise BookServiceError("operation_id_conflict" if str(exc) == "operation_id_conflict" else "state_unavailable", "The build reservation could not be persisted.") from exc
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
            chapter = self._chapter(layout, dependency.chapter_id)
            eligible, reason = self._accepted_chapter_build_eligible(
                state, layout, chapter, build,
                allow_historical_plan=not bool(chapter_head["accepted_plan_matches_prepared"]),
            )
            if not eligible:
                raise BookServiceError("stale_dependency", f"An accepted chapter plan is no longer eligible ({reason}).")
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

    def _run_test_mp3_build(self, job_id: str, pinned: dict[str, Any], *,
                            before_finalize: Callable[[], None] | None = None,
                            after_finalize: Callable[[], None] | None = None) -> None:
        """Copy verified MP3 packets in frozen chunk order; never decode to a master."""
        state = self._state_required()
        _, _, layout = self._enabled_layout()
        chapter = self._chapter(layout, pinned["chapter_id"])
        ffmpeg, ffprobe = self._registered_media_executables()
        takes = [state.take(value) for value in pinned["take_ids"]]
        if any(value is None for value in takes):
            raise BookServiceError("stale_dependency", "A pinned MP3 take is unavailable.")
        sources: list[Mp3Source] = []
        packet_inputs = []
        for chunk_id, take in zip(pinned["chunk_ids"], takes, strict=True):
            assert take is not None
            source = _path(self.root, take["filepath"])
            probe = asyncio.run(ffprobe_json(ffprobe, source, timeout_seconds=60.0))
            inspected = inspect_media_file(source, ffprobe=probe)
            if inspected.bytes_sha256 != take["bytes_sha256"] or inspected.media.codec != "mp3" or inspected.media.encoding != "compressed":
                raise BookServiceError("stale_media", "A pinned MP3 take no longer matches verified media facts.")
            sources.append(Mp3Source(chunk_id, source, inspected))
            packet_inputs.append(asyncio.run(ffprobe_packet_facts(ffprobe, source, timeout_seconds=60.0)))
        self._check_mp3_build_preflight(layout, sources)
        build_id = str(uuid.uuid4())
        relative = f"{chapter.audio_root}/builds/{build_id}"
        _mkdir_safe(self.root, relative)
        directory = _path(self.root, relative)
        directory_identity = _owned_directory_identity(directory)
        manifest, output = directory / "inputs.ffconcat", directory / "listening.mp3"
        write_ffconcat_manifest([source.filepath for source in sources], manifest)
        stream_copy_argv = build_test_mp3_stream_copy_argv(
            ffmpeg, sources, [], scope="chapter", manifest=manifest, destination=output,
            metadata=dto.BuildMetadata.model_validate(pinned["metadata"], strict=True),
        )
        tool_versions = {
            "ffmpeg": {"executable": str(ffmpeg), "version": asyncio.run(_media_tool_version(ffmpeg))},
            "ffprobe": {"executable": str(ffprobe), "version": asyncio.run(_media_tool_version(ffprobe))},
        }
        process = asyncio.run(run_process(stream_copy_argv, timeout_seconds=1800.0, cwd=directory))
        if process.cancelled: raise BookServiceError("cancelled", "MP3 stream-copy was cancelled.")
        if process.timed_out: raise BookServiceError("tool_timeout", "MP3 stream-copy exceeded its bounded runtime.")
        if process.returncode != 0: raise BookServiceError("tool_failed", "MP3 stream-copy failed.")
        output_probe = asyncio.run(ffprobe_json(ffprobe, output, timeout_seconds=60.0))
        inspected = inspect_media_file(output, ffprobe=output_probe)
        proof = verify_mp3_packet_copy(packet_inputs, asyncio.run(ffprobe_packet_facts(ffprobe, output, timeout_seconds=60.0)))
        if inspected.media.codec != "mp3" or inspected.media.duration_seconds <= 0:
            raise BookServiceError("media_mismatch", "Stream-copy output lacks decodable MP3 duration facts.")
        decoder = asyncio.run(verify_chapter_mp3_decoder(
            ffmpeg, ffprobe, [source.filepath for source in sources], output,
            timeout_seconds=300.0,
        ))
        if decoder.status != "checked" or not (
            decoder.packet_order_checked and decoder.decoder_checked and decoder.boundaries_checked
        ):
            raise BookServiceError("validation_failed", "MP3 decoder and join-boundary verification did not complete.")
        decoder_facts = asdict(decoder)
        for fact, source in zip(decoder_facts["sources"], sources, strict=True):
            fact["filepath"] = str(source.filepath.relative_to(self.root))
        decoder_facts["output"]["filepath"] = str(output.relative_to(self.root))
        recipe = {
            "recipe_version": 1, "mode": "test_mp3_stream_copy", "scope": "chapter",
            "inputs": [
                {"chunk_id": source.source_id, "filepath": str(source.filepath.relative_to(self.root)),
                 "take_id": take["take_id"], "bytes_sha256": take["bytes_sha256"],
                 "media": source.inspection.media.model_dump(mode="json")}
                for source, take in zip(sources, takes, strict=True)
            ],
            "output": {"filepath": str(output.relative_to(self.root)), "codec": "mp3",
                       "encoding": "compressed", "source_bitrate_kbps": pinned["source_bitrate_kbps"]},
            "settings": {"metadata": pinned["metadata"], "stream_copy": True,
                         "resample": False, "bitrate_change": False, "master_output": False},
            "tools": tool_versions,
            "processing_argv": stream_copy_argv,
            "packet_copy": asdict(proof),
            "decoder_verification": decoder_facts,
        }
        join_offsets = [boundary.output_decoded_frame_offset for boundary in decoder.join_boundaries]
        offsets = [0, *join_offsets, decoder.output.decoded_frames]
        if len(offsets) != len(pinned["chunk_ids"]) + 1 or any(
            start >= end for start, end in zip(offsets[:-1], offsets[1:], strict=True)
        ):
            raise BookServiceError("media_mismatch", "Decoded MP3 join offsets do not map every chunk to a nonempty timeline interval.")
        timeline_entries = [
            {"kind": "audio", "source_id": chunk_id, "start_frame": start, "end_frame": end}
            for chunk_id, (start, end) in zip(
                pinned["chunk_ids"], zip(offsets[:-1], offsets[1:], strict=True), strict=True
            )
        ]
        timeline = directory / "timeline.json"
        _write_immutable_json(timeline, {
            "mode": "test_mp3_stream_copy", "duration_seconds": decoder.output.decoded_duration_seconds,
            "sample_rate_hz": decoder.output.sample_rate, "channels": decoder.output.channels,
            "frame_count": decoder.output.decoded_frames, "entries": timeline_entries,
            "packet_count": proof.packet_count, "ordered_packets_sha256": proof.ordered_packets_sha256,
            "delay_padding_verified": decoder.boundaries_checked,
            "seam_quality_assessed": False, "decoder_verification": decoder_facts,
        })
        result = dto.BuildResult.model_validate({"kind": "build", "build_id": build_id, "scope": "chapter", "namespace": json.loads(pinned["scope_key"]), "source_snapshot_ids": [pinned["snapshot_id"]], "input_take_ids": pinned["take_ids"], "chapter_dependencies": [], "request_plan_sha256": pinned["request_plan_sha256"], "outputs": [{"kind": "mp3_download", "filepath": f"{relative}/listening.mp3", "bytes_sha256": inspected.bytes_sha256, "size_bytes": inspected.size_bytes, "media": inspected.media.model_dump(mode="json")}], "timeline_filepath": f"{relative}/timeline.json", "recipe_sha256": canonical_json_sha256(recipe), "validation": {"complete": True, "media_integrity": True, "coverage": True, "sample_or_packet_verification": True, "errors": []}, "needs_listening_review": True}).model_dump(mode="json")
        _write_immutable_json(directory / "build.json", {"schema_version": 1, "build": result, "recipe": recipe})
        self._finish_build_success(state, job_id, {"scope": "chapter", "build_id": build_id,
            "chapter_id": chapter.chapter_id, "scope_key": pinned["scope_key"], "snapshot_id": pinned["snapshot_id"],
            "request_plan_sha256": pinned["request_plan_sha256"], "input_take_ids": pinned["take_ids"],
            "created_at": datetime.now(timezone.utc).isoformat(), "was_accepted": False, "result": result},
            before_finalize=before_finalize, after_finalize=after_finalize,
            cleanup=lambda: _cleanup_owned_build_files(
                self.root, directory, directory_identity,
                ("inputs.ffconcat", "listening.mp3", "timeline.json", "build.json"),
            ))

    def run_build_job(self, job_id: str, *, before_finalize: Callable[[], None] | None = None,
                      after_finalize: Callable[[], None] | None = None) -> None:
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
                self._run_book_build(job_id, pinned, before_finalize=before_finalize, after_finalize=after_finalize)
                return
            if pinned.get("mode") == "test_mp3_stream_copy":
                self._run_test_mp3_build(job_id, pinned, before_finalize=before_finalize, after_finalize=after_finalize)
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
            gaps = [dto.SilenceGap.model_validate(g, strict=True) for g in pinned["gaps"]]
            self._check_pcm_build_preflight(layout, sources, gaps, target, emit_mp3=bool(pinned.get("emit_mp3")))
            build_relative = f"{chapter.audio_root}/builds/{build_id}"
            _mkdir_safe(self.root, build_relative)
            build_dir = _path(self.root, build_relative)
            build_directory_identity = _owned_directory_identity(build_dir)
            pcm = build_dir / "master.pcm"
            pcm_stage = build_dir / "master.pcm.part"
            timeline = build_dir / "timeline.json"
            timeline_stage = build_dir / "timeline.json.part"
            staged.extend([pcm_stage, timeline_stage])
            with pcm_stage.open("xb") as output:
                assembled = assemble_pcm_stream(sources, gaps, target, output)
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
            encoder_argv = None
            tool_versions: dict[str, Any] = {}
            if pinned.get("emit_mp3"):
                ffmpeg, ffprobe = self._registered_media_executables()
                tool_versions = {
                    "ffmpeg": {"executable": str(ffmpeg), "version": asyncio.run(_media_tool_version(ffmpeg))},
                    "ffprobe": {"executable": str(ffprobe), "version": asyncio.run(_media_tool_version(ffprobe))},
                }
                mp3 = build_dir / "listening.mp3"
                encoder_argv = production_mp3_argv(
                    ffmpeg, pcm, mp3, target,
                    dto.BuildMetadata.model_validate(pinned["metadata"], strict=True),
                )
                process = asyncio.run(run_process(encoder_argv, timeout_seconds=1800.0, cwd=build_dir))
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
            recipe = {
                "recipe_version": 1, "scope": "chapter", "mode": "production_pcm_mp3" if encoder_argv else "production_pcm",
                "inputs": [
                    {"chunk_id": source.source_id, "take_id": take["take_id"],
                     "filepath": str(source.filepath.relative_to(self.root)),
                     "bytes_sha256": source.inspection.bytes_sha256,
                     "media": source.inspection.media.model_dump(mode="json")}
                    for source, take in zip(sources, takes, strict=True)
                ],
                "settings": {"target": _data(target), "gaps": pinned["gaps"],
                             "metadata": pinned["metadata"], "emit_mp3": bool(pinned.get("emit_mp3"))},
                "assembler": "cognita.books.assembly.assemble_pcm_stream",
                "tools": tool_versions, "processing_argv": encoder_argv,
                "timeline": timeline_value,
                "outputs": [
                    {"kind": item["kind"], "filepath": item["filepath"],
                     "bytes_sha256": item["bytes_sha256"], "media": item["media"]}
                    for item in outputs
                ],
            }
            result = dto.BuildResult.model_validate({
                "kind": "build", "build_id": build_id, "scope": "chapter",
                "namespace": json.loads(pinned["scope_key"]), "source_snapshot_ids": [pinned["snapshot_id"]],
                "input_take_ids": pinned["take_ids"], "chapter_dependencies": [],
                "request_plan_sha256": pinned["request_plan_sha256"], "outputs": outputs,
                "timeline_filepath": f"{build_relative}/timeline.json", "recipe_sha256": canonical_json_sha256(recipe),
                "validation": {"complete": True, "media_integrity": True, "coverage": True,
                               "sample_or_packet_verification": True, "errors": []},
                "needs_listening_review": True,
            }, strict=True).model_dump(mode="json", exclude_unset=True)
            _write_immutable_json(build_dir / "build.json", {"schema_version": 1, "build": result, "recipe": recipe})
            self._finish_build_success(state, job_id, {
                "scope": "chapter", "build_id": build_id, "chapter_id": chapter.chapter_id, "scope_key": pinned["scope_key"],
                "snapshot_id": pinned["snapshot_id"], "request_plan_sha256": pinned["request_plan_sha256"],
                "input_take_ids": pinned["take_ids"], "created_at": datetime.now(timezone.utc).isoformat(),
                "was_accepted": False, "result": result,
            }, before_finalize=before_finalize, after_finalize=after_finalize,
            cleanup=lambda: _cleanup_owned_build_files(
                self.root, build_dir, build_directory_identity,
                ("master.pcm.part", "timeline.json.part", "master.pcm", "timeline.json", "listening.mp3", "build.json"),
            ))
        except (BookServiceError, MediaValidationError, AssemblyError, ProcessRunnerError, OSError, ProjectStateError) as exc:
            reason = exc.reason if isinstance(exc, BookServiceError) else (
                exc.code if isinstance(exc, (MediaValidationError, AssemblyError, ProcessRunnerError)) else (
                    "cancelled" if isinstance(exc, ProjectStateError) and str(exc) == "cancel_requested" else "job_failed"
                )
            )
            state.finish_build_failure(job_id=job_id, reason=reason,
                                       message=str(exc) if isinstance(exc, (BookServiceError, MediaValidationError, AssemblyError, ProcessRunnerError, ProjectStateError)) else "The local build could not complete safely.",
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

    def _run_book_build(self, job_id: str, pinned: dict[str, Any], *,
                        before_finalize: Callable[[], None] | None = None,
                        after_finalize: Callable[[], None] | None = None) -> None:
        """Assemble one continuous book candidate from pinned chapter heads."""
        state, _, layout = self._enabled_layout()
        target = dto.ProductionTarget.model_validate(pinned["target"], strict=True)
        build_id = str(uuid.uuid4())
        root_relative = f"{layout.shared_paths.book_audio_root}/builds/{build_id}"
        sources: list[PcmSource] = []
        source_snapshots: list[str] = []
        input_take_ids: list[str] = []
        child_timelines: dict[str, dict[str, Any]] = {}
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
            if chapter_build is None:
                raise BookServiceError("stale_dependency", "A pinned chapter build is unavailable for book assembly.")
            input_take_ids.extend(chapter_build.get("input_take_ids", []))
            child_timelines[dependency["chapter_id"]] = self._pinned_chapter_timeline(
                state, dependency, target=target,
                expected_frames=int(inspected.media.frame_count),
            )
        gaps = [dto.SilenceGap.model_validate(item, strict=True) for item in pinned["gaps"]]
        self._check_pcm_build_preflight(layout, sources, gaps, target, emit_mp3=True)
        _mkdir_safe(self.root, root_relative)
        build_dir = _path(self.root, root_relative)
        build_directory_identity = _owned_directory_identity(build_dir)
        pcm, pcm_stage = build_dir / "master.pcm", build_dir / "master.pcm.part"
        timeline, timeline_stage = build_dir / "timeline.json", build_dir / "timeline.json.part"
        with pcm_stage.open("xb") as output:
            assembled = assemble_pcm_stream(
                sources, gaps, target, output,
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
        timeline_entries: list[dict[str, Any]] = []
        for entry in assembled.timeline.entries:
            value = _data(entry)
            if entry.kind == "audio":
                child = child_timelines.get(entry.source_id)
                if child is None:
                    raise BookServiceError("state_unavailable", "A book chapter entry lacks its pinned timeline facts.")
                child_entries = []
                for nested in child["child_entries"]:
                    nested = dict(nested)
                    nested["start_frame"] = str(int(entry.start_frame) + int(nested["start_frame"]))
                    nested["end_frame"] = str(int(entry.start_frame) + int(nested["end_frame"]))
                    child_entries.append(nested)
                value.update({**child, "child_entries": child_entries})
            timeline_entries.append(value)
        timeline_value = {"sample_rate_hz": assembled.timeline.sample_rate_hz,
                          "channels": assembled.timeline.channels, "encoding": assembled.timeline.encoding,
                          "storage_bits": assembled.timeline.storage_bits, "frame_count": assembled.timeline.frame_count,
                          "entries": timeline_entries}
        with timeline_stage.open("x", encoding="utf-8") as output:
            json.dump(timeline_value, output, ensure_ascii=False, separators=(",", ":"))
            output.flush()
            os.fsync(output.fileno())
        os.replace(pcm_stage, pcm)
        os.replace(timeline_stage, timeline)
        outputs = [{"kind": "pcm_master", "filepath": f"{root_relative}/master.pcm",
                    "bytes_sha256": assembled.bytes_sha256, "size_bytes": assembled.sample_bytes, "media": media}]
        ffmpeg, ffprobe = self._registered_media_executables()
        tool_versions = {
            "ffmpeg": {"executable": str(ffmpeg), "version": asyncio.run(_media_tool_version(ffmpeg))},
            "ffprobe": {"executable": str(ffprobe), "version": asyncio.run(_media_tool_version(ffprobe))},
        }
        mp3 = build_dir / "listening.mp3"
        encoder_argv = production_mp3_argv(
            ffmpeg, pcm, mp3, target, dto.BuildMetadata.model_validate(pinned["metadata"], strict=True),
        )
        process = asyncio.run(run_process(
            encoder_argv,
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
        recipe = {
            "recipe_version": 1, "scope": "book", "mode": "production_pcm_mp3",
            "chapter_inputs": [
                {"dependency": entry["dependency"], "filepath": entry["output"]["filepath"],
                 "bytes_sha256": entry["output"]["bytes_sha256"],
                 "media": entry["output"]["media"]}
                for entry in pinned["chapters"]
            ],
            "settings": {"target": _data(target), "gaps": pinned["gaps"], "metadata": pinned["metadata"]},
            "assembler": "cognita.books.assembly.assemble_pcm_stream",
            "tools": tool_versions, "processing_argv": encoder_argv,
            "timeline": timeline_value,
            "outputs": [
                {"kind": item["kind"], "filepath": item["filepath"],
                 "bytes_sha256": item["bytes_sha256"], "media": item["media"]}
                for item in outputs
            ],
        }
        result = dto.BuildResult.model_validate({
            "kind": "build", "build_id": build_id, "scope": "book", "namespace": {"kind": "production"},
            "source_snapshot_ids": source_snapshots, "input_take_ids": input_take_ids,
            "chapter_dependencies": dependencies, "request_plan_sha256": None, "outputs": outputs,
            "timeline_filepath": f"{root_relative}/timeline.json", "recipe_sha256": canonical_json_sha256(recipe),
            "validation": {"complete": True, "media_integrity": True, "coverage": True,
                           "sample_or_packet_verification": True, "errors": []},
            "needs_listening_review": True,
        }, strict=True).model_dump(mode="json", exclude_unset=True)
        _write_immutable_json(build_dir / "build.json", {"schema_version": 1, "build": result, "recipe": recipe})
        self._finish_build_success(state, job_id, {
            "scope": "book", "build_id": build_id, "book_id": layout.book_id,
            "created_at": datetime.now(timezone.utc).isoformat(), "was_accepted": False,
            "dependencies": dependencies, "result": result,
        }, before_finalize=before_finalize, after_finalize=after_finalize,
        cleanup=lambda: _cleanup_owned_build_files(
            self.root, build_dir, build_directory_identity,
            ("master.pcm.part", "timeline.json.part", "master.pcm", "timeline.json", "listening.mp3", "build.json"),
        ))

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
        plan_matches = current_plan == build["request_plan_sha256"]
        if request.intent == "accept_candidate" and not plan_matches:
            raise BookServiceError("stale_dependency", "The candidate no longer matches the current prepared chapter plan.")
        # A rollback selects prior acceptance only when its frozen prose still
        # equals current registered prose; it never rewrites working documents.
        scope = json.loads(build["scope_key"])
        if scope.get("kind") == "test":
            working_prose, working_tagged = self._snapshot_working_pair(stored["payload"], chapter)
            prose = _read_bytes(self.root, working_prose)
            tagged = _read_bytes(self.root, working_tagged)
            projected = project_docx_pair(prose, tagged)
            if projected.prose_projection_sha256 != snap_result.get("prose_projection_sha256"):
                raise BookServiceError("stale_source", "The current test prose differs from this frozen build.")
            if (request.intent == "rollback"
                    and projected.spoken_projection_sha256 != snap_result.get("spoken_projection_sha256")):
                raise BookServiceError("stale_source", "Test rollback requires the same currently authorized spoken projection.")
            self._active_test_authorization(
                layout, chapter, authorization_id=scope.get("authorization_id"),
                prose_filepath=working_prose, tagged_filepath=working_tagged, prose_bytes=prose,
                selected_ordinals=stored["payload"].get("selected_source_ordinals"),
            )
        elif scope.get("kind") == "production":
            eligible, reason = self._production_snapshot_eligible(
                state, layout, chapter, stored,
                allow_historical_plan=request.intent == "rollback",
            )
            if not eligible:
                raise BookServiceError("stale_dependency", f"Current production source or settings are ineligible ({reason}).")
            if request.intent == "accept_candidate":
                eligible, reason = self._accepted_chapter_build_eligible(
                    state, layout, chapter, build, allow_historical_plan=False,
                )
                if not eligible:
                    raise BookServiceError("stale_dependency", f"The candidate dependencies are ineligible ({reason}).")
            elif not build.get("was_accepted"):
                raise BookServiceError("stale_dependency", "Rollback requires an archived previously accepted chapter build.")
        exports = [{key: output[key] for key in ("kind", "filepath", "bytes_sha256")} for output in result["outputs"]]
        try:
            disposition, committed = state.commit_chapter_build(
                build_id=request.build_id, chapter_id=chapter.chapter_id, scope_key=build["scope_key"],
                expected_head_revision=request.expected_head_revision, intent=request.intent,
                plan_matches_prepared=plan_matches, owner_key=owner_key, project=self.project_name,
                operation_id=request.operation_id, args_sha256=args_sha256,
                acceptance=_data(request.acceptance),
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
        dependencies = build.get("dependencies", [])
        if [item.get("chapter_id") for item in dependencies] != layout.chapter_order:
            raise BookServiceError("stale_dependency", "The book candidate does not match current registered chapter order.")
        plan_matches = True
        for dependency in dependencies:
            chapter = self._chapter(layout, dependency["chapter_id"])
            current = state.chapter_head(dependency["chapter_id"], production_key)
            chosen = state.build(dependency["chapter_build_id"])
            if chosen is None:
                raise BookServiceError("stale_dependency", "A historical chapter build is unavailable.")
            exact_head = (current is not None
                          and current["accepted_build_id"] == dependency["chapter_build_id"]
                          and int(current["head_revision"]) == dependency["chapter_head_revision"]
                          and current["accepted_snapshot_id"] == dependency["snapshot_id"]
                          and current["accepted_plan_sha256"] == dependency["request_plan_sha256"])
            plan_matches = plan_matches and exact_head and bool(current["accepted_plan_matches_prepared"])
            if request.intent == "accept_candidate":
                if not exact_head:
                    raise BookServiceError("stale_dependency", "A chapter head changed after this book candidate was assembled.")
                eligible, reason = self._accepted_chapter_build_eligible(
                    state, layout, chapter, chosen,
                    allow_historical_plan=not bool(current["accepted_plan_matches_prepared"]),
                )
            else:
                if not chosen.get("was_accepted"):
                    raise BookServiceError("stale_dependency", "Book rollback requires previously accepted chapter builds.")
                eligible, reason = self._accepted_chapter_build_eligible(
                    state, layout, chapter, chosen, allow_historical_plan=True,
                )
            if not eligible:
                raise BookServiceError("stale_dependency", f"A book chapter dependency is no longer eligible ({reason}).")
        exports = [{key: output[key] for key in ("kind", "filepath", "bytes_sha256")}
                   for output in build["result"]["outputs"]]
        try:
            disposition, committed = state.commit_book_build(
                build_id=request.build_id, book_id=layout.book_id,
                expected_head_revision=request.expected_head_revision,
                owner_key=owner_key, project=self.project_name, operation_id=request.operation_id,
                args_sha256=args_sha256,
                intent=request.intent, acceptance=_data(request.acceptance),
                result={"accepted_build_id": request.build_id, "head_revision": 1, "previous_build_id": None,
                        "accepted_plan_matches_prepared": plan_matches, "exports": exports,
                        "dependent_book_ids_marked_stale": [], "rollback_available": True},
                plan_matches_prepared=plan_matches,
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
            if accepted_chapter is None:
                not_ready.append({"chapter_id": chapter_id, "reason": "ineligible_accepted_production_head"})
                continue
            registered_chapter = self._chapter(layout, chapter_id)
            eligible, reason = self._accepted_chapter_build_eligible(
                state, layout, registered_chapter, accepted_chapter,
                allow_historical_plan=not bool(head["accepted_plan_matches_prepared"]),
            )
            outputs = accepted_chapter.get("result", {}).get("outputs", [])
            settings_current = None
            try:
                settings_current = validate_production_settings(
                    _read_bytes(self.root, layout.shared_paths.production_settings_filepath)
                )
            except (BookServiceError, ValueError):
                pass
            pcm = next((item for item in outputs if item.get("kind") == "pcm_master"), None)
            target = settings_current.production_target if settings_current else None
            media = pcm.get("media", {}) if pcm else {}
            if (not eligible or pcm is None or target is None
                    or media.get("sample_rate_hz") != target.sample_rate_hz
                    or media.get("channels") != target.channels
                    or media.get("storage_bits") != target.storage_bits
                    or media.get("encoding") != target.encoding):
                not_ready.append({"chapter_id": chapter_id,
                                  "reason": reason if not eligible else "ineligible_accepted_production_head"})
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
                interrupted_cancel = job["state"] == "cancel_requested"
                source_kind = job["payload"].get("source_kind")
                self._cleanup_import_stage(request.job_id)
                self._discard_unregistered_import_artifacts(job)
                state.finish_import_failure(
                    job_id=request.job_id,
                    reason=("cancelled" if interrupted_cancel else
                            "source_unavailable" if source_kind in {"workspace", "https_url"} else "job_failed"),
                    message="The unfinished import was interrupted by restart.",
                    cancelled=interrupted_cancel,
                )
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


def _owned_directory_identity(directory: Path) -> tuple[Path, int, int]:
    """Capture the exact directory that this job created for later cleanup."""
    facts = directory.lstat()
    if stat.S_ISLNK(facts.st_mode) or not stat.S_ISDIR(facts.st_mode):
        raise BookServiceError("permission_denied", "The owned build directory is unsafe.")
    return directory.resolve(strict=True), facts.st_dev, facts.st_ino


def _cleanup_owned_build_files(project_root: Path, directory: Path, identity: tuple[Path, int, int],
                               filenames: tuple[str, ...]) -> None:
    """Remove only known artifacts after unregistered build publication fails."""
    try:
        root = project_root.resolve(strict=True)
        facts = directory.lstat()
        resolved = directory.resolve(strict=True)
        expected_path, expected_device, expected_inode = identity
        if (stat.S_ISLNK(facts.st_mode) or not stat.S_ISDIR(facts.st_mode)
                or resolved != expected_path
                or (facts.st_dev, facts.st_ino) != (expected_device, expected_inode)):
            raise OSError("owned build directory identity changed")
        resolved.relative_to(root)
    except (OSError, ValueError) as exc:
        log.error("Owned build cleanup refused path=%s error=%s", directory, exc)
        return
    remaining: list[str] = []
    for filename in filenames:
        candidate = directory / filename
        try:
            candidate.relative_to(directory)
            facts = candidate.lstat()
            if stat.S_ISLNK(facts.st_mode) or not stat.S_ISREG(facts.st_mode):
                raise OSError("owned artifact is not a regular file")
            candidate.unlink()
        except FileNotFoundError:
            continue
        except OSError as exc:
            remaining.append(f"{candidate}:{exc}")
    try:
        directory.rmdir()
    except OSError as exc:
        remaining.append(f"{directory}:{exc}")
        try:
            remaining.extend(str(item) for item in directory.iterdir())
        except OSError as listing_error:
            remaining.append(f"{directory}:{listing_error}")
    if remaining:
        log.error("Owned build cleanup incomplete paths=%s", ";".join(remaining))


async def _media_tool_version(executable: Path) -> str:
    """Capture the registered media tool's actual version banner for build provenance."""
    result = await run_process(
        [str(executable), "-version"], timeout_seconds=10.0, max_output_bytes=16_384,
    )
    if result.cancelled or result.timed_out or result.returncode != 0 or result.stdout_truncated:
        raise ProcessRunnerError("tool_version_unavailable", "A registered media tool version could not be verified.")
    banner = (result.stdout or result.stderr).decode("utf-8", errors="replace").splitlines()
    if not banner or not banner[0].strip():
        raise ProcessRunnerError("tool_version_unavailable", "A registered media tool returned no version banner.")
    return banner[0].strip()


def _immutable_json_bytes(value: dict[str, Any]) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _write_immutable_json(path: Path, value: dict[str, Any]) -> None:
    """Create one durable fact file without making it a second authority."""
    try:
        with path.open("xb") as stream:
            stream.write(_immutable_json_bytes(value))
            stream.flush()
            os.fsync(stream.fileno())
    except FileExistsError as exc:
        raise BookServiceError("publication_conflict", "The immutable media fact path already exists.") from exc
    except OSError as exc:
        raise BookServiceError("publication_failed", "The immutable media fact could not be published.") from exc
