"""Durable book/audiobook services and deterministic projection primitives."""

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
    "Bookmark", "BookmarkLocation", "BookmarkPlacement", "ChunkRange", "Coverage",
    "DocxProjection", "DocxProjectionError", "ExcludedParagraph", "ExplicitTagSpan",
    "FileLockedError", "MalformedBookmarks", "MappedSegment", "Paragraph",
    "ParagraphProjection", "ProjectedDocument", "ProjectionError", "SeparatorMapping",
    "SourceSegment", "UnsupportedDocxStructure", "UnsupportedLocation",
    "ValidatedChunkRange", "add_bookmarks", "is_file_locked", "parse_docx",
    "project_docx_pair", "require_unlocked", "validate_chunk_ranges",
]
