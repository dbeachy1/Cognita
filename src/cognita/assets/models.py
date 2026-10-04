"""Small dependency-free types shared by the asset implementation."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any


LEGACY_CONNECTOR_ID = "__legacy_asset_connector__"


def normalize_connector_id(value: Any) -> str:
    """Return a bounded trusted connector key for durable asset operations.

    ``None`` represents pre-9.0 callers.  Those rows are deliberately kept in
    a separate namespace during migration so a connector can never replay an
    operation whose original connector identity was not persisted.
    """
    if value is None:
        return LEGACY_CONNECTOR_ID
    if not isinstance(value, str) or not value or len(value) > 128:
        raise ValueError("invalid connector identity")
    if value == LEGACY_CONNECTOR_ID:
        raise ValueError("reserved connector identity")
    return value


class AssetReason(StrEnum):
    INVALID_ARGUMENTS = "invalid_arguments"
    INVALID_PATH = "invalid_path"
    UNSUPPORTED_MEDIA_TYPE = "unsupported_media_type"
    INVALID_DATA_URL = "invalid_data_url"
    INVALID_BASE64 = "invalid_base64"
    ENCODED_LIMIT = "encoded_limit"
    BYTE_LIMIT = "byte_limit"
    TOO_LARGE = "too_large"
    SIZE_MISMATCH = "size_mismatch"
    HASH_MISMATCH = "hash_mismatch"
    INVALID_PNG = "invalid_png"
    ANIMATED_PNG = "animated_png"
    DIMENSION_LIMIT = "dimension_limit"
    METADATA_INVALID = "metadata_invalid"
    METADATA_LIMIT = "metadata_limit"
    AMBIGUOUS_METADATA = "ambiguous_metadata"
    UNSUPPORTED_METADATA_SCHEMA = "unsupported_metadata_schema"
    PROVENANCE_REQUIRES_PRESERVE = "provenance_requires_preserve"
    NOT_FOUND = "not_found"
    DESTINATION_EXISTS = "destination_exists"
    STALE_FILE = "stale_file"
    READ_ONLY = "read_only"
    BUSY = "busy"
    OPERATION_CONFLICT = "operation_conflict"
    BACKUP_FAILED = "backup_failed"
    PUBLICATION_FAILED = "publication_failed"
    INDEX_FAILED = "index_failed"
    RECOVERY_REQUIRED = "recovery_required"
    INTERNAL_ERROR = "internal_error"


class AssetError(ValueError):
    """Safe, bounded application error suitable for an MCP tool result."""

    def __init__(self, reason: str | AssetReason, message: str = "asset operation failed", *, details: Mapping[str, Any] | None = None):
        self.reason = str(reason)
        self.message = message[:512]
        self.details = dict(details or {})
        super().__init__(self.message)


class MetadataStorage(StrEnum):
    AUTO = "auto"
    EMBEDDED = "embedded"
    CATALOG = "catalog"
    PRESERVE_CURRENT = "preserve_current"


@dataclass(frozen=True, slots=True)
class PngFacts:
    width: int
    height: int
    chunk_count: int
    cabx_chunk_count: int = 0
    cognita_chunks: int = 0
    embedded_metadata: dict[str, Any] | None = None
    embedded_compressed: bool = False

    @property
    def provenance_state(self) -> str:
        return "cabx_present_unverified" if self.cabx_chunk_count else "none"


@dataclass(slots=True)
class AssetRecord:
    asset_id: str
    filepath: str
    metadata: dict[str, Any]
    received_size: int
    received_sha256: str
    final_size: int
    final_sha256: str
    width: int
    height: int
    metadata_storage: str
    metadata_revision: int = 1
    indexed: bool = False
    provenance_state: str = "none"
    cabx_chunk_count: int = 0
    embedded_metadata_present: bool = False
    previous_backup_id: str | None = None
    received_backup_id: str | None = None
    file_mtime: datetime | None = None


@dataclass(slots=True)
class AssetOperation:
    operation_id: str
    tool: str
    request_sha256: str
    state: str = "running"
    result: dict[str, Any] | None = None


@dataclass(slots=True)
class AssetSearchHit:
    filepath: str
    asset_id: str
    title: str
    description: str
    alt_text: str
    tags: list[str] = field(default_factory=list)
    width: int = 0
    height: int = 0
    final_sha256: str = ""
    metadata_storage: str = ""
    provenance_state: str = "none"
    score: float = 0.0
    search_method: str = "lexical"
