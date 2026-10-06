"""Book services, projection primitives, strict wire contracts, and helpers."""

from . import models as _models  # noqa: F401
from .models import *  # noqa: F403
from .models import __all__ as _model_exports
from .models import (
    ExcludedParagraph as ContractExcludedParagraph,
    ExplicitTagSpan as ContractExplicitTagSpan,
    ParagraphProjection as ContractParagraphProjection,
    SourceSegment as ContractSourceSegment,
)
from .fingerprint import (
    FINGERPRINT_VERSION,
    canonical_json_sha256,
    grapheme_boundaries,
    grapheme_spans,
    is_grapheme_boundary,
    request_fingerprint,
    sha256_text,
    validate_grapheme_boundary,
)
from .config import (
    BookBinding,
    BookLayout,
    ChapterState,
    FolderPolicyState,
    FolderRule,
    ProductionSettings,
    classify_book_config,
    parse_book_layout,
    validate_book_binding,
    validate_book_layout,
    validate_chapter_state,
    validate_production_settings,
)
from .policy import BookMutationPolicy, EffectiveIndexPolicy, IndexDecision, MutationDecision
from .media import MediaInspection, MediaValidationError, inspect_media, inspect_media_file
from .jobs import PacketFact, ProcessResult, ProcessRunnerError, ffprobe_json, ffprobe_packet_facts, run_process
from .assembly import (
    AssemblyError,
    Mp3Source,
    PacketCopyValidation,
    PcmAssemblyResult,
    PcmSource,
    PcmTimeline,
    TimelineEntry,
    WaveWrapperResult,
    assemble_pcm_stream,
    build_test_mp3_stream_copy_argv,
    plan_pcm_timeline,
    production_mp3_argv,
    verify_mp3_packet_copy,
    wrap_pcm_as_wave,
    write_ffconcat_manifest,
)
from .schemas import (
    ALL_ADDITIVE_MUTATING_TOOLS,
    ALL_ADDITIVE_TOOL_NAMES,
    BOOK_MUTATING_TOOLS,
    BOOK_OUTPUT_SCHEMAS,
    BOOK_TOOL_DEFS,
    BOOK_TOOL_NAMES,
    PROJECT_STORAGE_OUTPUT_SCHEMAS,
    PROJECT_STORAGE_TOOL_DEFS,
    PROJECT_STORAGE_MUTATING_TOOLS,
    PROJECT_STORAGE_TOOL_NAMES,
    book_tool_definitions,
    error_envelope,
    project_storage_tool_definitions,
    success_envelope,
    with_directory_move_result,
)

# Keep the pre-contract package-level projection names stable; contract DTOs
# with colliding names are available with a ``Contract`` prefix above and from
# ``cognita.books.models``.
from .docx import (
    Bookmark,
    BookmarkLocation,
    BookmarkPlacement,
    DocxProjection,
    DocxProjectionError,
    FileLockedError,
    MalformedBookmarks,
    Paragraph,
    UnsupportedDocxStructure,
    UnsupportedLocation,
    add_bookmarks,
    is_file_locked,
    parse_docx,
    require_unlocked,
)
from .projection import (
    ChunkRange,
    Coverage,
    ExcludedParagraph,
    ExplicitTagSpan,
    MappedSegment,
    ParagraphProjection,
    ProjectedDocument,
    ProjectionError,
    SeparatorMapping,
    SourceSegment,
    ValidatedChunkRange,
    project_docx_pair,
    validate_chunk_ranges,
)

__all__ = [
    *[name for name in _model_exports if name not in {
        "ExcludedParagraph", "ExplicitTagSpan", "ParagraphProjection", "SourceSegment"
    }],
    "ContractExcludedParagraph", "ContractExplicitTagSpan",
    "ContractParagraphProjection", "ContractSourceSegment",
    "BOOK_MUTATING_TOOLS", "BOOK_TOOL_NAMES", "PROJECT_STORAGE_TOOL_NAMES",
    "PROJECT_STORAGE_MUTATING_TOOLS", "ALL_ADDITIVE_TOOL_NAMES", "ALL_ADDITIVE_MUTATING_TOOLS",
    "BOOK_OUTPUT_SCHEMAS", "PROJECT_STORAGE_OUTPUT_SCHEMAS", "BOOK_TOOL_DEFS",
    "PROJECT_STORAGE_TOOL_DEFS", "success_envelope", "error_envelope",
    "with_directory_move_result",
    "BookBinding", "BookLayout", "ChapterState", "FolderPolicyState", "FolderRule",
    "ProductionSettings", "classify_book_config", "validate_book_binding",
    "validate_book_layout", "validate_chapter_state", "validate_production_settings",
    "parse_book_layout",
    "BookMutationPolicy", "EffectiveIndexPolicy", "IndexDecision", "MutationDecision",
    "book_tool_definitions", "project_storage_tool_definitions", "FINGERPRINT_VERSION",
    "MediaInspection", "MediaValidationError", "PacketFact", "ProcessResult", "ProcessRunnerError",
    "inspect_media", "inspect_media_file", "ffprobe_json", "ffprobe_packet_facts", "run_process",
    "AssemblyError", "Mp3Source", "PacketCopyValidation", "PcmAssemblyResult", "PcmSource", "PcmTimeline",
    "TimelineEntry", "WaveWrapperResult", "assemble_pcm_stream", "plan_pcm_timeline",
    "build_test_mp3_stream_copy_argv", "production_mp3_argv", "verify_mp3_packet_copy", "wrap_pcm_as_wave",
    "write_ffconcat_manifest",
    "canonical_json_sha256", "grapheme_boundaries", "grapheme_spans",
    "is_grapheme_boundary", "request_fingerprint", "sha256_text",
    "validate_grapheme_boundary",
    "Bookmark", "BookmarkLocation", "BookmarkPlacement", "ChunkRange", "Coverage",
    "DocxProjection", "DocxProjectionError", "ExcludedParagraph", "ExplicitTagSpan",
    "FileLockedError", "MalformedBookmarks", "MappedSegment", "Paragraph",
    "ParagraphProjection", "ProjectedDocument", "ProjectionError", "SeparatorMapping",
    "SourceSegment", "UnsupportedDocxStructure", "UnsupportedLocation",
    "ValidatedChunkRange", "add_bookmarks", "is_file_locked", "parse_docx",
    "project_docx_pair", "require_unlocked", "validate_chunk_ranges",
]
