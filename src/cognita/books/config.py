"""Strict, versioned project configuration records for book and folder policy."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import PurePosixPath
from collections.abc import Mapping
from typing import Any, Literal, TypeVar

from pydantic import StringConstraints, field_validator, model_validator
from typing import Annotated

from .models import (
    PositiveSafeInt,
    ProductionTarget,
    RequestSpec,
    SafeInt,
    Sha256,
    StrictModel,
)

NormalizedProjectPath = Annotated[str, StringConstraints(min_length=1)]
RootedProjectPath = Annotated[str, StringConstraints(min_length=1)]


def normalize_project_path(value: str, *, allow_root: bool = False) -> str:
    """Validate the wire spelling of a project-relative path without resolving it."""
    if not isinstance(value, str):
        raise TypeError("project path must be a string")
    if value == "" and allow_root:
        return ""
    if not value or "\\" in value or ":" in value or value.startswith("/"):
        raise ValueError("path must be a normalized project-relative POSIX path")
    parts = value.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise ValueError("path contains an empty, dot, or traversal component")
    if PurePosixPath(value).is_absolute():
        raise ValueError("path must be project-relative")
    return value


def _path_validator(value: str) -> str:
    return normalize_project_path(value)


def _utc_time(value: str) -> str:
    if not isinstance(value, str):
        raise TypeError("timestamp must be a string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("timestamp must be ISO-8601 with a UTC offset") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise ValueError("timestamp must use UTC")
    return value


def _optional_utc_time(value: str | None) -> str | None:
    return None if value is None else _utc_time(value)


class ChapterRegistration(StrictModel):
    chapter_id: str
    title: str
    chapter_state_filepath: NormalizedProjectPath
    working_filepath: NormalizedProjectPath
    tagged_filepath: NormalizedProjectPath
    summary_filepath: NormalizedProjectPath | None
    originals_root: NormalizedProjectPath
    audio_root: NormalizedProjectPath

    _validate_paths = field_validator(
        "chapter_state_filepath", "working_filepath", "tagged_filepath", "summary_filepath",
        "originals_root", "audio_root", mode="before",
    )(lambda value: None if value is None else _path_validator(value))


class IndexedRegistration(StrictModel):
    filepath: NormalizedProjectPath
    role: str

    _validate_filepath = field_validator("filepath", mode="before")(_path_validator)


class BookIndexPolicy(StrictModel):
    default_unknown: Literal["exclude"]
    tagged_copies: Literal["exclude"]
    archives: Literal["exclude"]
    media: Literal["exclude"]
    duplicate_prose: Literal["single_active_source"]


class BookSharedPaths(StrictModel):
    production_settings_filepath: NormalizedProjectPath
    book_audio_root: NormalizedProjectPath

    _validate_paths = field_validator(
        "production_settings_filepath", "book_audio_root", mode="before"
    )(_path_validator)


class BookStorage(StrictModel):
    quota_bytes: PositiveSafeInt
    reserve_bytes: SafeInt
    import_https_hosts: list[str]


class ProductionAuthorization(StrictModel):
    authorization_id: str
    actor: str
    authorized_at: str
    completed_book: Literal[True]
    revoked: bool

    _validate_time = field_validator("authorized_at")(_utc_time)


class TestAuthorization(StrictModel):
    authorization_id: str
    chapter_id: str
    prose_filepath: NormalizedProjectPath
    tagged_filepath: NormalizedProjectPath
    allowed_paragraph_ordinals: list[SafeInt]
    source_raw_sha256: Sha256
    actor: str
    authorized_at: str
    expires_at: str
    revoked: bool

    _validate_paths = field_validator("prose_filepath", "tagged_filepath", mode="before")(
        _path_validator
    )
    _validate_times = field_validator("authorized_at", "expires_at")(_utc_time)

    @model_validator(mode="after")
    def _ordinals_are_unique(self) -> TestAuthorization:
        if not self.allowed_paragraph_ordinals:
            raise ValueError("test authorization must allow at least one paragraph")
        if len(set(self.allowed_paragraph_ordinals)) != len(self.allowed_paragraph_ordinals):
            raise ValueError("allowed paragraph ordinals must be unique")
        if self.prose_filepath == self.tagged_filepath:
            raise ValueError("test prose and tagged paths must be distinct")
        return self


class BookLayout(StrictModel):
    schema_version: Literal[1]
    layout_revision: PositiveSafeInt
    book_id: str
    title: str
    chapter_order: list[str]
    chapters: list[ChapterRegistration]
    indexed_references: list[IndexedRegistration]
    indexed_instructions: list[IndexedRegistration]
    indexed_workflow_documents: list[IndexedRegistration]
    index_policy: BookIndexPolicy
    source_master_filepath: NormalizedProjectPath | None
    shared_paths: BookSharedPaths
    storage: BookStorage
    production_authorization: ProductionAuthorization | None
    test_authorizations: list[TestAuthorization]

    _validate_source_master = field_validator("source_master_filepath", mode="before")(
        lambda value: None if value is None else _path_validator(value)
    )

    @model_validator(mode="after")
    def _validate_registrations(self) -> BookLayout:
        ids = [chapter.chapter_id for chapter in self.chapters]
        if len(ids) != len(set(ids)) or set(ids) != set(self.chapter_order):
            raise ValueError("chapter_order and chapter registrations must contain the same unique IDs")
        if len(self.chapter_order) != len(set(self.chapter_order)):
            raise ValueError("chapter_order must not contain duplicate IDs")

        active_prose = [chapter.working_filepath for chapter in self.chapters]
        if len(active_prose) != len(set(active_prose)):
            raise ValueError("active working chapter paths must be unique")
        if any(c.working_filepath == c.tagged_filepath for c in self.chapters):
            raise ValueError("working prose and tagged-copy paths must be distinct")

        all_paths = [
            self.shared_paths.production_settings_filepath,
            self.shared_paths.book_audio_root,
            *(p for p in (
                self.source_master_filepath,
            ) if p is not None),
            *(path for chapter in self.chapters for path in (
                chapter.chapter_state_filepath, chapter.working_filepath,
                chapter.tagged_filepath, chapter.summary_filepath,
                chapter.originals_root, chapter.audio_root,
            ) if path is not None),
            *(record.filepath for record in (
                *self.indexed_references,
                *self.indexed_instructions,
                *self.indexed_workflow_documents,
            )),
        ]
        if len(all_paths) != len(set(all_paths)):
            raise ValueError("registered paths must not be duplicated")

        for auth in self.test_authorizations:
            if auth.chapter_id not in set(ids):
                raise ValueError("test authorization must target a registered chapter")

        for chapter in self.chapters:
            if not _is_within(chapter.audio_root, self.shared_paths.book_audio_root):
                raise ValueError("chapter audio_root must be inside the registered book_audio_root")
            if self.source_master_filepath is not None and (
                _is_within(self.source_master_filepath, chapter.audio_root)
                or _is_within(self.source_master_filepath, chapter.originals_root)
            ):
                raise ValueError("source master cannot overlap managed originals or audio roots")
            for source_path in (chapter.working_filepath, chapter.tagged_filepath):
                if _is_within(source_path, chapter.audio_root) or _is_within(
                    source_path, chapter.originals_root
                ):
                    raise ValueError("working documents cannot be inside managed immutable roots")

        return self


class ChapterApprovalProvenance(StrictModel):
    actor: str
    approved_at: str
    source_raw_sha256: Sha256
    prose_projection_sha256: Sha256
    projection_version: str

    _validate_time = field_validator("approved_at")(_utc_time)


class ChapterSummaryState(StrictModel):
    filepath: NormalizedProjectPath
    source_raw_sha256: Sha256
    source_prose_projection_sha256: Sha256
    summary_raw_sha256: Sha256
    approved: bool
    actor: str | None
    approved_at: str | None

    _validate_filepath = field_validator("filepath", mode="before")(_path_validator)
    _validate_time = field_validator("approved_at")(_optional_utc_time)


class IndexAnnotationSpan(StrictModel):
    paragraph_id: str
    start: SafeInt
    end: SafeInt
    expected_text_sha256: Sha256
    reason: str

    @model_validator(mode="after")
    def _nonempty_span(self) -> IndexAnnotationSpan:
        if self.end <= self.start:
            raise ValueError("annotation span must be a nonempty half-open range")
        return self


class IndexAnnotations(StrictModel):
    source_raw_sha256: Sha256
    extraction_version: str
    spans: list[IndexAnnotationSpan]


class ChapterState(StrictModel):
    schema_version: Literal[1]
    chapter_id: str
    layout_revision: PositiveSafeInt
    state_revision: PositiveSafeInt
    editorial_status: Literal["draft", "approved"]
    approval_binding: Literal["prose_projection"]
    approved_source_raw_sha256: Sha256 | None
    approved_prose_projection_sha256: Sha256 | None
    approval_projection_version: str | None
    approval_provenance: ChapterApprovalProvenance | None
    summary: ChapterSummaryState | None
    index_annotations: IndexAnnotations | None


class RequestLimitEvidence(StrictModel):
    value: PositiveSafeInt
    unit: Literal["unicode_codepoints", "utf16_units"]
    evidence: str


class ProductionSettings(StrictModel):
    schema_version: Literal[1]
    target_codepoints: PositiveSafeInt
    request_limit: RequestLimitEvidence
    request_spec: RequestSpec | None
    production_target: ProductionTarget | None
    native_format_evidence: str | None


class BookBinding(StrictModel):
    schema_version: Literal[1]
    book_id: str
    layout_filepath: Literal["Project Files/Book_Layout.json"]
    state_root: Literal[".cognita-storage"]


class FolderRule(StrictModel):
    path: str
    indexed: bool

    _validate_path = field_validator("path", mode="before")(
        lambda value: normalize_project_path(value, allow_root=True)
    )


class FolderPolicyState(StrictModel):
    """Typed state payload used to validate the durable project's folder rules."""

    policy_revision: SafeInt
    rules: list[FolderRule]

    @model_validator(mode="after")
    def _unique_rules(self) -> FolderPolicyState:
        paths = [rule.path.casefold() for rule in self.rules]
        if len(paths) != len(set(paths)):
            raise ValueError("folder policy paths must be unique")
        return self


def _is_within(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip("/") + "/")


def classify_book_config(
    *,
    binding_present: bool,
    layout_present: bool,
    binding_valid: bool,
    layout_valid: bool,
    binding_book_id: str | None = None,
    layout_book_id: str | None = None,
    binding_layout_filepath: str | None = None,
) -> Literal["never_enabled", "bootstrap_pending", "enabled", "configuration_conflict"]:
    """Classify stable binding state without treating damaged state as legacy absence."""
    if not binding_present and not layout_present:
        return "never_enabled"
    if binding_present and not binding_valid:
        return "configuration_conflict"
    if layout_present and not layout_valid:
        return "configuration_conflict"
    if binding_present and not layout_present:
        return "configuration_conflict"
    if not binding_present and layout_present:
        return "bootstrap_pending"
    if binding_book_id is None or layout_book_id is None:
        return "configuration_conflict"
    if binding_book_id != layout_book_id:
        return "configuration_conflict"
    if binding_layout_filepath != "Project Files/Book_Layout.json":
        return "configuration_conflict"
    return "enabled"


CONFIG_MODELS: dict[str, type[StrictModel]] = {
    "book-layout": BookLayout,
    "chapter-state": ChapterState,
    "production-settings": ProductionSettings,
    "book-binding": BookBinding,
    "folder-policy": FolderPolicyState,
}

ConfigModelT = TypeVar("ConfigModelT", bound=StrictModel)


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON property: {key}")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> Any:
    raise ValueError(f"invalid JSON constant: {value}")


def parse_config(model: type[ConfigModelT], document: bytes | str | Mapping[str, Any]) -> ConfigModelT:
    """Parse and strictly validate one configuration document.

    JSON text rejects duplicate object keys and non-standard NaN/Infinity values
    before Pydantic applies exact DTO, nullability, and cross-field validation.
    """
    if isinstance(document, bytes):
        text = document.decode("utf-8", errors="strict")
        value = json.loads(
            text, object_pairs_hook=_unique_object, parse_constant=_reject_json_constant
        )
    elif isinstance(document, str):
        value = json.loads(
            document, object_pairs_hook=_unique_object, parse_constant=_reject_json_constant
        )
    elif isinstance(document, Mapping):
        value = dict(document)
    else:
        raise TypeError("configuration must be UTF-8 JSON bytes, JSON text, or an object mapping")
    if not isinstance(value, dict):
        raise ValueError("configuration document must have an object at its root")
    return model.model_validate(value, strict=True)


def validate_book_layout(document: bytes | str | Mapping[str, Any]) -> BookLayout:
    return parse_config(BookLayout, document)


parse_book_layout = validate_book_layout


def validate_chapter_state(document: bytes | str | Mapping[str, Any]) -> ChapterState:
    return parse_config(ChapterState, document)


def validate_production_settings(document: bytes | str | Mapping[str, Any]) -> ProductionSettings:
    return parse_config(ProductionSettings, document)


def validate_book_binding(document: bytes | str | Mapping[str, Any]) -> BookBinding:
    return parse_config(BookBinding, document)


def __getattr__(name: str) -> object:
    # Keep the module's public list explicit while supporting simple wildcard imports.
    if name in CONFIG_MODELS:
        return CONFIG_MODELS[name]
    raise AttributeError(name)


__all__ = [
    "NormalizedProjectPath", "RootedProjectPath", "normalize_project_path",
    "ChapterRegistration", "IndexedRegistration", "BookIndexPolicy", "BookSharedPaths",
    "BookStorage", "ProductionAuthorization", "TestAuthorization", "BookLayout",
    "ChapterApprovalProvenance", "ChapterSummaryState", "IndexAnnotationSpan",
    "IndexAnnotations", "ChapterState", "RequestLimitEvidence", "ProductionSettings",
    "BookBinding", "FolderRule", "FolderPolicyState", "classify_book_config", "CONFIG_MODELS",
    "parse_config", "validate_book_layout", "validate_chapter_state",
    "validate_production_settings", "validate_book_binding",
    "parse_book_layout",
]
