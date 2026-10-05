"""Normative, strict DTOs for the Cognita book and audiobook API.

The public contract lives here independently of the engine, proxy, and result
contract registry so those layers can consume schemas without import cycles.
"""

from __future__ import annotations

import base64
import binascii
from typing import Annotated, Literal, TypeAlias

from pydantic import (
    BaseModel, ConfigDict, Field, JsonValue, StringConstraints, field_validator,
    model_validator,
)
from pydantic.experimental.missing_sentinel import MISSING

SAFE_INTEGER_MAX = 9_007_199_254_740_991
SafeInt = Annotated[int, Field(ge=0, le=SAFE_INTEGER_MAX)]
PositiveSafeInt = Annotated[int, Field(ge=1, le=SAFE_INTEGER_MAX)]
PageLimit500 = Annotated[int, Field(ge=1, le=500)]
PageLimit200 = Annotated[int, Field(ge=1, le=200)]
PageLimit100 = Annotated[int, Field(ge=1, le=100)]
MaxTextCharacters = Annotated[int, Field(ge=1, le=40_000)]
MaxReadBytes = Annotated[int, Field(ge=1, le=1_048_576)]
Base64Content = Annotated[str, StringConstraints(max_length=1_398_104)]
Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
DecimalCount = Annotated[str, StringConstraints(pattern=r"^(0|[1-9][0-9]*)$")]

JsonObject: TypeAlias = dict[str, JsonValue]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class ProductionScope(StrictModel):
    kind: Literal["production"]


class TestScope(StrictModel):
    kind: Literal["test"]
    authorization_id: str


Scope: TypeAlias = Annotated[ProductionScope | TestScope, Field(discriminator="kind")]


class RequestSpec(StrictModel):
    provider: str
    route: str
    model_id: str
    voice_id: str
    parameters: JsonObject
    context_fields: JsonObject


class TextPage(StrictModel):
    text: str
    returned_start: SafeInt
    returned_end: SafeInt
    total_codepoints: SafeInt
    text_sha256: Sha256

    @model_validator(mode="after")
    def _valid_page_bounds(self) -> TextPage:
        if self.returned_start > self.returned_end or self.returned_end > self.total_codepoints:
            raise ValueError("TextPage bounds must be ordered within total_codepoints")
        if len(self.text) != self.returned_end - self.returned_start:
            raise ValueError("TextPage text length must equal its code-point bounds")
        try:
            self.text.encode("utf-8", errors="strict")
        except UnicodeEncodeError as exc:
            raise ValueError("TextPage text must not contain lone surrogates") from exc
        return self


class SourceSegment(StrictModel):
    paragraph_id: str
    start: SafeInt
    end: SafeInt
    bookmark: str | None

    @model_validator(mode="after")
    def _nonempty_segment(self) -> SourceSegment:
        if self.end <= self.start:
            raise ValueError("source segment must be a nonempty half-open range")
        return self


class ChunkRecord(StrictModel):
    chunk_id: str
    snapshot_id: str
    order: SafeInt
    start: SafeInt
    end: SafeInt
    bookmark: str
    source_segments: list[SourceSegment]
    codepoint_count: SafeInt
    limit_count: SafeInt
    prompt_sha256: Sha256
    spoken_text_sha256: Sha256
    request_sha256: Sha256 | None
    request_spec: RequestSpec | None
    replaces_chunk_ids: list[str]
    replaced_by_chunk_ids: list[str]
    opening_phrase: str
    closing_phrase: str
    take_ids: list[str]
    accepted_take_id: str | None
    reuse_status: Literal["reusable", "changed", "new", "needs_check"]
    reusable_take_ids: list[str]

    @model_validator(mode="after")
    def _valid_range(self) -> ChunkRecord:
        if self.end <= self.start:
            raise ValueError("chunk range must be nonempty")
        return self


class MediaProperties(StrictModel):
    codec: str
    container: str
    sample_rate_hz: PositiveSafeInt
    channels: PositiveSafeInt
    encoding: Literal["signed_integer", "float", "compressed"]
    storage_bits: SafeInt | None
    valid_bits: SafeInt | None
    endianness: Literal["little", "big", "not_applicable"]
    bitrate_bps: SafeInt | None
    frame_count: DecimalCount | None
    duration_seconds: Annotated[float, Field(ge=0, allow_inf_nan=False)]
    canonical_sample_sha256: Sha256 | None


class MediaOutput(StrictModel):
    kind: Literal["pcm_master", "mp3_download"]
    filepath: str
    bytes_sha256: Sha256
    size_bytes: SafeInt
    media: MediaProperties


class AssemblyDerivative(StrictModel):
    filepath: str
    bytes_sha256: Sha256
    media: MediaProperties


class RawFormat(StrictModel):
    container: Literal["raw_pcm"]
    encoding: Literal["signed_integer", "float"]
    sample_rate_hz: PositiveSafeInt
    channels: PositiveSafeInt
    storage_bits: PositiveSafeInt
    valid_bits: PositiveSafeInt
    endianness: Literal["little", "big"]
    interleaving: Literal["interleaved"]
    provider_format_evidence: str

    @model_validator(mode="after")
    def _supported_sample_format(self) -> RawFormat:
        if self.valid_bits != self.storage_bits:
            raise ValueError("v1 raw PCM requires valid_bits equal storage_bits")
        supported = {"signed_integer": {16, 24, 32}, "float": {32, 64}}
        if self.storage_bits not in supported[self.encoding]:
            raise ValueError("unsupported v1 raw PCM sample width")
        return self


class ProductionTarget(StrictModel):
    sample_rate_hz: PositiveSafeInt
    channels: PositiveSafeInt
    encoding: Literal["signed_integer", "float"]
    storage_bits: PositiveSafeInt
    valid_bits: PositiveSafeInt
    mp3_bitrate_kbps: PositiveSafeInt

    @model_validator(mode="after")
    def _supported_sample_format(self) -> ProductionTarget:
        if self.valid_bits != self.storage_bits:
            raise ValueError("v1 production requires valid_bits equal storage_bits")
        supported = {"signed_integer": {16, 24, 32}, "float": {32, 64}}
        if self.storage_bits not in supported[self.encoding]:
            raise ValueError("unsupported v1 production sample width")
        return self


class GenerationProviderIds(StrictModel):
    flow_id: str | MISSING = MISSING
    node_id: str | MISSING = MISSING
    session_ids: list[str] | MISSING = MISSING
    generation_ids: list[str] | MISSING = MISSING


class Failure(StrictModel):
    code: str
    message: str


class GenerationCost(StrictModel):
    credits: Annotated[float, Field(ge=0, allow_inf_nan=False)] | MISSING = MISSING
    amount: Annotated[float, Field(ge=0, allow_inf_nan=False)] | MISSING = MISSING
    currency: str | MISSING = MISSING
    kind: Literal["estimated", "actual"]


class GenerationRequest(StrictModel):
    prompt_sha256: Sha256
    spec: RequestSpec


class GenerationRecord(StrictModel):
    generation_record_id: str
    scope: Scope
    chapter_id: str
    snapshot_id: str
    chunk_id: str
    generation_revision: SafeInt
    state: Literal["reserved", "submitted", "running", "completed", "failed", "outcome_unknown"]
    request_sha256: Sha256
    request: GenerationRequest
    provider_ids: GenerationProviderIds
    provider_response_metadata: JsonObject
    cost: GenerationCost | None
    failure: Failure | None
    media_registered: bool
    take_id: str | None
    import_job_id: str | None
    created_at: str
    updated_at: str


class TakeRecord(StrictModel):
    take_id: str
    namespace: Scope
    chapter_id: str
    snapshot_id: str
    chunk_id: str
    generation_record_id: str
    request_sha256: Sha256
    filepath: str
    bytes_sha256: Sha256
    size_bytes: SafeInt
    media: MediaProperties
    provenance: Literal["native_generation", "test_mp3", "derived_audio"]
    assembly_derivative: AssemblyDerivative | None


class ChapterDependency(StrictModel):
    chapter_id: str
    chapter_build_id: str
    chapter_head_revision: PositiveSafeInt
    snapshot_id: str
    request_plan_sha256: Sha256


class BuildValidation(StrictModel):
    complete: bool
    media_integrity: bool
    coverage: bool
    sample_or_packet_verification: bool
    errors: list[str]


class BuildResult(StrictModel):
    kind: Literal["build"]
    build_id: str
    scope: Literal["chapter", "book"]
    namespace: Scope
    source_snapshot_ids: list[str]
    input_take_ids: list[str]
    chapter_dependencies: list[ChapterDependency]
    request_plan_sha256: Sha256 | None
    outputs: list[MediaOutput]
    timeline_filepath: str
    recipe_sha256: Sha256
    validation: BuildValidation
    needs_listening_review: bool


class ImportResult(StrictModel):
    kind: Literal["import"]
    generation_record_id: str
    generation_revision: PositiveSafeInt
    media_revision: PositiveSafeInt
    take: TakeRecord


JobResultData: TypeAlias = Annotated[ImportResult | BuildResult, Field(discriminator="kind")]


class JobRef(StrictModel):
    job_id: str
    job_revision: PositiveSafeInt
    state: Literal["queued", "running"]
    poll_after_seconds: SafeInt
    pinned_inputs_sha256: Sha256


class ExcludedParagraph(StrictModel):
    paragraph_id: str
    reason: str


class ExplicitTagSpan(StrictModel):
    paragraph_id: str
    start: SafeInt
    end: SafeInt
    expected_text_sha256: Sha256

    @model_validator(mode="after")
    def _nonempty_span(self) -> ExplicitTagSpan:
        if self.end <= self.start:
            raise ValueError("tag span must be a nonempty half-open range")
        return self


class ParagraphProjection(StrictModel):
    paragraph_id: str
    source_ordinal: SafeInt
    text: str
    paragraph_returned_start: SafeInt
    paragraph_returned_end: SafeInt
    paragraph_total_codepoints: SafeInt
    style: str
    speech_start: SafeInt | None
    speech_end: SafeInt | None
    tags: list["TagSpan"]
    bookmarks: list[str]

    @model_validator(mode="after")
    def _valid_paragraph_bounds(self) -> ParagraphProjection:
        if self.paragraph_returned_start > self.paragraph_returned_end:
            raise ValueError("paragraph page bounds must be ordered")
        if self.paragraph_returned_end > self.paragraph_total_codepoints:
            raise ValueError("paragraph page bounds exceed the complete paragraph")
        if len(self.text) != self.paragraph_returned_end - self.paragraph_returned_start:
            raise ValueError("paragraph text length must match its page bounds")
        if (self.speech_start is None) != (self.speech_end is None):
            raise ValueError("speech_start and speech_end must both be present or both null")
        if self.speech_start is not None and self.speech_end <= self.speech_start:
            raise ValueError("speech range must be a nonempty half-open range")
        return self


class TagSpan(StrictModel):
    start: SafeInt
    end: SafeInt

    @model_validator(mode="after")
    def _nonempty_span(self) -> TagSpan:
        if self.end <= self.start:
            raise ValueError("tag span must be a nonempty half-open range")
        return self


class ProjectionWarning(StrictModel):
    code: str
    paragraph_id: str | MISSING = MISSING
    message: str


class InspectRequest(StrictModel):
    project: str
    chapter_id: str
    prose_filepath: str
    tagged_filepath: str
    base_document_view_id: str | MISSING = MISSING
    speech_paragraph_ids: list[str] | MISSING = MISSING
    excluded_paragraphs: list[ExcludedParagraph] | MISSING = MISSING
    explicit_tag_spans: list[ExplicitTagSpan] | MISSING = MISSING
    cursor: str | MISSING = MISSING
    max_characters: MaxTextCharacters | MISSING = MISSING


class InspectResult(StrictModel):
    document_view_id: str
    prose_sha256: Sha256
    tagged_sha256: Sha256
    projection_version: str
    prose_projection_sha256: Sha256
    spoken_projection_sha256: Sha256
    speech_text_sha256: Sha256
    speech_text_total_codepoints: SafeInt
    speech_text: str
    returned_start: SafeInt
    returned_end: SafeInt
    paragraphs: list[ParagraphProjection]
    excluded_paragraphs: list[ExcludedParagraph]
    source_text_matches_without_tags: bool
    warnings: list[ProjectionWarning]
    has_more: bool
    next_cursor: str | None


class RequestLimit(StrictModel):
    value: PositiveSafeInt
    unit: Literal["unicode_codepoints", "utf16_units"]


class ChunkInput(StrictModel):
    chunk_id: str
    start: SafeInt
    end: SafeInt
    replaces_chunk_ids: list[str] | MISSING = MISSING
    request_spec: RequestSpec | None

    @model_validator(mode="after")
    def _nonempty_range(self) -> ChunkInput:
        if self.end <= self.start:
            raise ValueError("chunk request range must be nonempty")
        return self


class PrepareRequest(StrictModel):
    project: str
    operation_id: str
    chapter_id: str
    document_view_id: str
    expected_prose_sha256: Sha256
    expected_tagged_sha256: Sha256
    expected_manifest_revision: SafeInt | None
    scope: Scope
    speech_selection_confirmed: Literal[True]
    request_limit: RequestLimit
    expected_settings_sha256: Sha256 | None
    production_target: ProductionTarget | None
    chunks: list[ChunkInput]
    publish_bookmarks_to_working_tagged_docx: bool


class Coverage(StrictModel):
    speech_codepoints: SafeInt
    covered_codepoints: SafeInt
    gaps: SafeInt
    overlaps: SafeInt


class PrepareResult(StrictModel):
    snapshot_id: str
    manifest_revision: PositiveSafeInt
    input_tagged_sha256: Sha256
    snapshot_tagged_sha256: Sha256
    snapshot_prose_sha256: Sha256
    prose_projection_sha256: Sha256
    spoken_projection_sha256: Sha256
    request_plan_sha256: Sha256
    snapshot_filepath: str
    working_tagged_filepath: str
    working_tagged_updated: bool
    chunks: list[ChunkRecord]
    retired_chunk_ids: list[str]
    coverage: Coverage
    current_outputs_stale: bool


class GetChapterRequest(StrictModel):
    project: str
    chapter_id: str
    scope: Scope | MISSING = MISSING
    snapshot_id: str | MISSING = MISSING
    chunk_ids: list[str] | MISSING = MISSING
    include_text: bool | MISSING = MISSING
    cursor: str | MISSING = MISSING
    limit: PageLimit500 | MISSING = MISSING
    max_characters: MaxTextCharacters | MISSING = MISSING


class ReturnedChunkText(StrictModel):
    chunk_id: str
    prompt: TextPage
    spoken_text: TextPage


class GetChapterResult(StrictModel):
    chapter_id: str
    namespace: Scope
    manifest_revision: SafeInt | None
    media_revision: SafeInt
    head_revision: SafeInt | None
    snapshot_id: str | None
    prose_sha256: Sha256 | None
    tagged_sha256: Sha256 | None
    spoken_projection_sha256: Sha256 | None
    request_plan_sha256: Sha256 | None
    accepted_build_id: str | None
    accepted_snapshot_id: str | None
    accepted_request_plan_sha256: Sha256 | None
    production_settings_sha256: Sha256 | None
    candidate_build_ids: list[str]
    accepted_plan_matches_prepared: bool | None
    current_outputs_stale: bool
    source_status: Literal["not_prepared", "eligible", "changed", "unapproved", "blocked"]
    chunks: list[ChunkRecord]
    takes: list[TakeRecord]
    returned_texts: list[ReturnedChunkText]
    has_more: bool
    next_cursor: str | None


class QuoteQuery(StrictModel):
    kind: Literal["quote"]
    text: str
    before_text: str | MISSING = MISSING
    after_text: str | MISSING = MISSING
    snapshot_id: str | MISSING = MISSING


class TimestampQuery(StrictModel):
    kind: Literal["timestamp"]
    build_id: str
    seconds: Annotated[float, Field(ge=0, allow_inf_nan=False)]


FindQuery: TypeAlias = Annotated[QuoteQuery | TimestampQuery, Field(discriminator="kind")]


class FindChunkRequest(StrictModel):
    project: str
    chapter_id: str | MISSING = MISSING
    cursor: str | MISSING = MISSING
    limit: PageLimit200 | MISSING = MISSING
    query: FindQuery


class ChunkLineage(StrictModel):
    old_chunk_id: str
    current_chunk_ids: list[str]


class ChunkMatch(StrictModel):
    chapter_id: str
    snapshot_id: str
    chunk_ids: list[str]
    occurrence_start: SafeInt | None
    occurrence_end: SafeInt | None
    coordinate_projection: Literal["spoken_text_codepoints", "timeline"]
    excerpt: str
    matched_build_id: str | None
    matched_take_ids: list[str]
    segment_kind: Literal["speech", "silence"]
    current_chunk_ids: list[str]
    current_take_ids: list[str]
    lineage: list[ChunkLineage]
    current_mapping_status: Literal["present", "missing", "ambiguous", "not_checked"]
    match_mode: Literal["literal", "case_whitespace_insensitive", "timestamp"]


class FindChunkResult(StrictModel):
    matches: list[ChunkMatch]
    ambiguous: bool
    searched_version: str
    has_more: bool
    next_cursor: str | None


class ProviderIds(StrictModel):
    flow_id: str | MISSING = MISSING
    node_id: str | MISSING = MISSING
    session_ids: list[str] | MISSING = MISSING
    generation_ids: list[str] | MISSING = MISSING


class ReserveGenerationChange(StrictModel):
    kind: Literal["reserve"]
    chapter_id: str
    snapshot_id: str
    chunk_id: str
    expected_manifest_revision: PositiveSafeInt
    request: GenerationRequest


class UpdateGenerationChange(StrictModel):
    kind: Literal["update"]
    generation_record_id: str
    expected_generation_revision: PositiveSafeInt
    state: Literal["reserved", "submitted", "running", "completed", "failed", "outcome_unknown"]
    provider_ids: ProviderIds | MISSING = MISSING
    provider_response_metadata: JsonObject | MISSING = MISSING
    cost: GenerationCost | MISSING = MISSING
    failure: Failure | MISSING = MISSING


GenerationChange: TypeAlias = Annotated[
    ReserveGenerationChange | UpdateGenerationChange, Field(discriminator="kind")
]


class RecordGenerationRequest(StrictModel):
    project: str
    operation_id: str
    change: GenerationChange


class RecordGenerationResult(StrictModel):
    generation: GenerationRecord


class WorkspaceAudioSource(StrictModel):
    kind: Literal["workspace"]
    path: str
    expected_sha256: Sha256


class ProjectAudioSource(StrictModel):
    kind: Literal["project_file"]
    filepath: str
    expected_sha256: Sha256


class HttpsAudioSource(StrictModel):
    kind: Literal["https_url"]
    url: str
    expected_sha256: Sha256 | MISSING = MISSING


AudioSource: TypeAlias = Annotated[
    WorkspaceAudioSource | ProjectAudioSource | HttpsAudioSource,
    Field(discriminator="kind"),
]


class ImportAudioRequest(StrictModel):
    project: str
    operation_id: str
    generation_record_id: str
    expected_generation_revision: PositiveSafeInt
    source: AudioSource
    provenance: Literal["native_generation", "test_mp3", "derived_audio"]
    source_format: RawFormat | MISSING = MISSING


class ChunkTakeInput(StrictModel):
    chunk_id: str
    take_id: str
    request_sha256: Sha256


class ChapterBuildInput(StrictModel):
    kind: Literal["chapter"]
    chapter_id: str
    snapshot_id: str
    expected_manifest_revision: PositiveSafeInt
    request_plan_sha256: Sha256
    takes: list[ChunkTakeInput]


class BookBuildInput(StrictModel):
    kind: Literal["book"]
    book_id: str
    expected_layout_revision: PositiveSafeInt
    chapters: list[ChapterDependency]


BuildInput: TypeAlias = Annotated[
    ChapterBuildInput | BookBuildInput, Field(discriminator="kind")
]


class BuildOutputs(StrictModel):
    master: bool
    mp3_bitrate_kbps: PositiveSafeInt | MISSING = MISSING


class BuildMetadata(StrictModel):
    title: str
    author: str
    edition: str
    chapter_number: PositiveSafeInt | MISSING = MISSING


class SilenceGap(StrictModel):
    before_id: str
    sample_frames: DecimalCount


class BuildRequest(StrictModel):
    project: str
    operation_id: str
    expected_head_revision: SafeInt | None
    input: BuildInput
    mode: Literal["production_pcm", "test_mp3_stream_copy"]
    outputs: BuildOutputs
    gaps: list[SilenceGap]
    metadata: BuildMetadata
    qa_notes: list[str] | MISSING = MISSING


class CommitAcceptance(StrictModel):
    actor: str
    accepted_at: str
    listening_review: Literal["passed", "explicitly_waived"]
    notes: list[str]


class CommitBuildRequest(StrictModel):
    project: str
    operation_id: str
    build_id: str
    expected_head_revision: SafeInt | None
    intent: Literal["accept_candidate", "rollback"]
    acceptance: CommitAcceptance


class ExportReference(StrictModel):
    kind: Literal["pcm_master", "mp3_download"]
    filepath: str
    bytes_sha256: Sha256


class CommitBuildResult(StrictModel):
    accepted_build_id: str
    head_revision: PositiveSafeInt
    previous_build_id: str | None
    accepted_plan_matches_prepared: bool
    exports: list[ExportReference]
    dependent_book_ids_marked_stale: list[str]
    rollback_available: bool


class GetJobRequest(StrictModel):
    project: str
    job_id: str


class JobProgress(StrictModel):
    completed_units: SafeInt
    total_units: SafeInt | None


class JobError(StrictModel):
    reason: str
    message: str
    operation_outcome: Literal["not_applied", "committed", "outcome_unknown"]
    correlation_id: str | None


class GetJobResult(StrictModel):
    job_id: str
    operation_id: str
    job_revision: PositiveSafeInt
    state: Literal["queued", "running", "succeeded", "failed", "cancelled"]
    phase: str
    progress: JobProgress
    poll_after_seconds: SafeInt | None
    result: JobResultData | None
    error: JobError | None


class CancelJobRequest(StrictModel):
    project: str
    operation_id: str
    job_id: str
    expected_job_revision: PositiveSafeInt


class CancelJobResult(StrictModel):
    job_id: str
    job_revision: PositiveSafeInt
    state: Literal["cancel_requested", "cancelled", "succeeded", "failed"]


class IndexStatusRequest(StrictModel):
    project: str
    chapter_id: str | MISSING = MISSING
    filepath: str | MISSING = MISSING
    cursor: str | MISSING = MISSING
    limit: PageLimit500 | MISSING = MISSING

    @model_validator(mode="after")
    def _one_path_selector(self) -> IndexStatusRequest:
        if {"chapter_id", "filepath"}.issubset(self.model_fields_set):
            raise ValueError("chapter_id and filepath are mutually exclusive")
        return self


class IndexStatusEntry(StrictModel):
    filepath: str
    chapter_id: str | None
    role: str
    index_state: Literal["pending", "indexed", "stale", "excluded", "blocked", "failed"]
    effective_rule: str
    source_raw_sha256: Sha256 | None
    indexed_source_raw_sha256: Sha256 | None
    extracted_text_sha256: Sha256 | None
    source_revision: str | None
    indexed_revision: str | None
    extraction_version: str | None
    editorial_status: str | None
    summary_freshness: Literal["fresh", "stale", "unapproved", "not_applicable"]
    last_indexed_at: str | None
    error: Failure | None


class IndexStatusResult(StrictModel):
    policy_revision: PositiveSafeInt
    catalog_revision: PositiveSafeInt
    entries: list[IndexStatusEntry]
    has_more: bool
    next_cursor: str | None


class RecordGenerationQuery(StrictModel):
    kind: Literal["record"]
    generation_record_id: str


class ChapterGenerationsQuery(StrictModel):
    kind: Literal["chapter"]
    chapter_id: str
    states: list[Literal["reserved", "submitted", "running", "completed", "failed", "outcome_unknown"]] | MISSING = MISSING


GenerationQuery: TypeAlias = Annotated[
    RecordGenerationQuery | ChapterGenerationsQuery, Field(discriminator="kind")
]


class GetGenerationsRequest(StrictModel):
    project: str
    query: GenerationQuery
    include_prompt: bool | MISSING = MISSING
    cursor: str | MISSING = MISSING
    limit: PageLimit100 | MISSING = MISSING


class GenerationPrompt(StrictModel):
    generation_record_id: str
    prompt: TextPage


class GetGenerationsResult(StrictModel):
    generations: list[GenerationRecord]
    prompts: list[GenerationPrompt]
    has_more: bool
    next_cursor: str | None


class GetBookRequest(StrictModel):
    project: str
    book_id: str
    cursor: str | MISSING = MISSING
    limit: PageLimit500 | MISSING = MISSING


class BookChapterNotReady(StrictModel):
    chapter_id: str
    reason: str


class GetBookResult(StrictModel):
    book_id: str
    layout_revision: PositiveSafeInt
    head_revision: SafeInt | None
    accepted_build_id: str | None
    candidate_build_ids: list[str]
    current_outputs_stale: bool
    accepted_plan_matches_prepared: bool | None
    chapter_order: list[str]
    current_chapter_dependencies: list[ChapterDependency]
    accepted_book_dependencies: list[ChapterDependency]
    chapters_not_ready: list[BookChapterNotReady]
    exports: list[MediaOutput]
    has_more: bool
    next_cursor: str | None


class SetFolderIndexingRequest(StrictModel):
    project: str
    path: str
    indexed: bool
    operation_id: str
    expected_policy_revision: SafeInt

    @field_validator("path", mode="before")
    @classmethod
    def _folder_path(cls, value: str) -> str:
        return _validate_wire_path(value, allow_root=True)


class SetFolderIndexingResult(StrictModel):
    path: str
    indexed: bool
    policy_revision: PositiveSafeInt
    job_id: str | None


class ProjectFileEntry(StrictModel):
    path: str
    type: Literal["file", "directory", "symlink"]
    size_bytes: SafeInt | None
    effective_read_only: bool
    effective_indexed: bool
    exclusion_reason: str | None
    index_state: Literal[
        "pending", "indexed", "stale", "excluded", "blocked", "failed", "not_indexed"
    ]
    error: Failure | None


class ListProjectFilesRequest(StrictModel):
    project: str
    path: str
    recursive: bool | MISSING = MISSING
    cursor: str | MISSING = MISSING
    limit: PageLimit500 | MISSING = MISSING

    @field_validator("path", mode="before")
    @classmethod
    def _list_path(cls, value: str) -> str:
        return _validate_wire_path(value, allow_root=True)


class ListProjectFilesResult(StrictModel):
    policy_revision: SafeInt
    entries: list[ProjectFileEntry]
    has_more: bool
    next_cursor: str | None


class ReadProjectFileRequest(StrictModel):
    project: str
    path: str
    offset: SafeInt | MISSING = MISSING
    max_bytes: MaxReadBytes | MISSING = MISSING
    expected_bytes_sha256: Sha256 | MISSING = MISSING

    @field_validator("path", mode="before")
    @classmethod
    def _file_path(cls, value: str) -> str:
        return _validate_wire_path(value, allow_root=False)

    @model_validator(mode="after")
    def _guarded_offset(self) -> ReadProjectFileRequest:
        offset = self.offset if "offset" in self.model_fields_set else 0
        if offset and "expected_bytes_sha256" not in self.model_fields_set:
            raise ValueError("nonzero offset requires expected_bytes_sha256")
        return self


class ReadProjectFileResult(StrictModel):
    path: str
    offset: SafeInt
    next_offset: SafeInt
    total_size_bytes: SafeInt
    bytes_sha256: Sha256
    content_base64: Base64Content
    has_more: bool

    @field_validator("content_base64")
    @classmethod
    def _valid_base64(cls, value: str) -> str:
        try:
            base64.b64decode(value, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise ValueError("content_base64 must contain valid base64") from exc
        return value


class DirectoryMoveIndexing(StrictModel):
    state: Literal["pending", "indexed", "stale", "excluded", "blocked", "failed"]
    job_id: str | None


class DirectoryMoveResult(StrictModel):
    status: Literal["success"]
    filepath: str
    new_filepath: str
    kind: Literal["directory"]
    policy_revision: PositiveSafeInt
    indexing: DirectoryMoveIndexing


__all__ = [
    "SAFE_INTEGER_MAX", "SafeInt", "PositiveSafeInt", "PageLimit500", "PageLimit200",
    "PageLimit100", "MaxTextCharacters", "MaxReadBytes", "Base64Content", "Sha256", "DecimalCount",
    "JsonValue", "JsonObject", "StrictModel", "Scope", "ProductionScope", "TestScope",
    "RequestSpec", "TextPage", "SourceSegment", "ChunkRecord", "MediaProperties",
    "MediaOutput", "AssemblyDerivative", "RawFormat", "ProductionTarget", "GenerationProviderIds", "ProviderIds",
    "Failure", "GenerationCost", "GenerationRequest", "GenerationRecord", "TakeRecord",
    "ChapterDependency", "BuildValidation", "BuildResult", "ImportResult", "JobResultData", "JobRef",
    "ExcludedParagraph", "ExplicitTagSpan", "ParagraphProjection", "TagSpan",
    "ProjectionWarning", "InspectRequest", "InspectResult", "RequestLimit", "ChunkInput",
    "PrepareRequest", "Coverage", "PrepareResult", "GetChapterRequest", "ReturnedChunkText",
    "GetChapterResult", "QuoteQuery", "TimestampQuery", "FindQuery", "FindChunkRequest",
    "ChunkLineage", "ChunkMatch", "FindChunkResult", "ReserveGenerationChange",
    "UpdateGenerationChange", "GenerationChange", "RecordGenerationRequest",
    "RecordGenerationResult", "WorkspaceAudioSource", "ProjectAudioSource", "HttpsAudioSource",
    "AudioSource", "ImportAudioRequest", "ChunkTakeInput", "ChapterBuildInput", "BookBuildInput",
    "BuildInput", "BuildOutputs", "BuildMetadata", "SilenceGap", "BuildRequest",
    "CommitAcceptance", "CommitBuildRequest", "ExportReference", "CommitBuildResult",
    "GetJobRequest", "JobProgress", "JobError", "GetJobResult", "CancelJobRequest",
    "CancelJobResult", "IndexStatusRequest", "IndexStatusEntry", "IndexStatusResult",
    "RecordGenerationQuery", "ChapterGenerationsQuery", "GenerationQuery",
    "GetGenerationsRequest", "GenerationPrompt", "GetGenerationsResult", "GetBookRequest",
    "BookChapterNotReady", "GetBookResult", "SetFolderIndexingRequest",
    "SetFolderIndexingResult", "ProjectFileEntry", "ListProjectFilesRequest",
    "ListProjectFilesResult", "ReadProjectFileRequest", "ReadProjectFileResult",
    "DirectoryMoveIndexing", "DirectoryMoveResult",
]


def _validate_wire_path(value: str, *, allow_root: bool) -> str:
    if not isinstance(value, str):
        raise TypeError("path must be a string")
    if value == "" and allow_root:
        return value
    if not value or "\\" in value or ":" in value or value.startswith("/"):
        raise ValueError("path must be a normalized project-relative path")
    if any(part in ("", ".", "..") for part in value.split("/")):
        raise ValueError("path contains an empty, dot, or traversal component")
    return value
