"""Strict, bounded wire types for the private ``/v1`` broker protocol."""

from __future__ import annotations

import unicodedata
from enum import StrEnum
from itertools import pairwise
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, field_validator

BROKER_PROTOCOL_VERSION = "v1"
MAX_ARGUMENT_BYTES = 64 * 1024
# The declared read/output bound is 1 MiB (workspace.MAX_FILE_BYTES, and this
# module's own fs_lines/job_get max_bytes ceilings), but file and job-stream
# content crosses the broker as base64 -- 4/3 expansion -- plus the JSON
# envelope around it (path, offsets, hashes, ...). 256 KiB silently failed
# every fs_read/fs_lines/job_get reply whose content exceeded ~190 KB with
# "runtime response exceeded broker limit" (service._execute, ~line 433),
# because it was sized as if content crossed the wire raw. 2 MiB covers the
# 1 MiB bound's ~1.37 MiB base64 form plus headroom for the envelope.
MAX_RESPONSE_BYTES = 2 * 1024**2
MAX_TRANSFER_FILES = 10_000
MAX_TRANSFER_BYTES = 4 * 1024**3
MAX_TRANSFER_FRAME_BYTES = 8 * 1024**2


class BrokerOperation(StrEnum):
    ENSURE = "ensure"
    INSPECT = "inspect"
    START = "start"
    STOP = "stop"
    REMOVE = "remove"
    FS_LIST = "fs_list"
    FS_STAT = "fs_stat"
    FS_READ = "fs_read"
    FS_WRITE = "fs_write"
    FS_EDIT = "fs_edit"
    FS_MKDIR = "fs_mkdir"
    FS_COPY = "fs_copy"
    FS_MOVE = "fs_move"
    FS_REMOVE = "fs_remove"
    FS_SEARCH = "fs_search"
    FS_LINES = "fs_lines"
    FS_USAGE = "fs_usage"
    JOB_START = "job_start"
    JOB_GET = "job_get"
    JOB_CANCEL = "job_cancel"


class ErrorCode(StrEnum):
    INVALID_REQUEST = "invalid_request"
    GENERATION_CONFLICT = "generation_conflict"
    NOT_FOUND = "not_found"
    BUSY = "busy"
    CONFLICT = "conflict"
    QUOTA = "quota"
    TIMEOUT = "timeout"
    UNSUPPORTED = "unsupported"
    RUNTIME_FAILURE = "runtime_failure"


class StrictModel(BaseModel):
    # UUIDs and enums necessarily arrive as JSON strings. Individual scalar
    # fields use StrictInt/StrictStr where JSON coercion would be unsafe.
    model_config = ConfigDict(extra="forbid")


class RpcRequest(StrictModel):
    request_id: UUID
    operation: BrokerOperation
    workspace_id: UUID
    expected_runtime_generation: StrictInt | None = Field(default=None, ge=0)
    arguments: dict[str, Any] = Field(default_factory=dict)

    @field_validator("arguments")
    @classmethod
    def validate_arguments(cls, value: dict[str, Any]) -> dict[str, Any]:
        # JSON serialization gives one deterministic bound independent of the
        # ASGI server's parser and prevents an argument body becoming an
        # unbounded broker response/request journal entry.
        import json

        try:
            encoded = json.dumps(value, separators=(",", ":"), ensure_ascii=False)
        except (TypeError, ValueError) as exc:
            raise ValueError("arguments must contain JSON values") from exc
        if len(encoded.encode("utf-8")) > MAX_ARGUMENT_BYTES:
            raise ValueError("arguments exceed the broker limit")
        forbidden = {"host_path", "hostPath", "sandbox_name", "sandboxName", "runtime_name"}
        if any(key in forbidden for key in value):
            raise ValueError("host paths and runtime names are broker-owned")
        return value


class RpcSuccess(StrictModel):
    request_id: UUID
    runtime_generation: StrictInt = Field(ge=0)
    data: dict[str, Any] = Field(default_factory=dict)


class RpcFailure(StrictModel):
    request_id: UUID | None = None
    code: ErrorCode
    retryable: bool
    message: StrictStr = Field(min_length=1, max_length=160)
    # These fields are intentionally bounded and optional.  They preserve the
    # broker's safe operation context without leaking SDK exception text,
    # guest output, commands, or host paths across the private boundary.
    stage: StrictStr | None = Field(default=None, max_length=64)
    category: StrictStr | None = Field(default=None, max_length=64)
    correlation_id: UUID | None = None
    diagnostics: dict[str, Any] | None = None


class TransferFile(StrictModel):
    path: StrictStr = Field(min_length=1, max_length=4096)
    size: StrictInt = Field(ge=0, le=MAX_TRANSFER_BYTES)
    sha256: StrictStr = Field(pattern=r"^[0-9a-fA-F]{64}$")

    @field_validator("path")
    @classmethod
    def guest_relative_path(cls, value: str) -> str:
        if unicodedata.normalize("NFC", value) != value:
            raise ValueError("transfer paths must use NFC Unicode normalization")
        encoded = value.encode("utf-8")
        if len(encoded) > 4096:
            raise ValueError("transfer path exceeds 4096 encoded bytes")
        if "\\" in value or value.startswith("/"):
            raise ValueError("transfer paths must be normalized guest-relative paths")
        parts = value.split("/")
        if any(
            part in {"", ".", ".."}
            or len(part.encode("utf-8")) > 255
            or any(unicodedata.category(character).startswith("C") for character in part)
            for part in parts
        ):
            raise ValueError("transfer path is not normalized")
        return value


class TransferManifest(StrictModel):
    transfer_id: UUID
    workspace_id: UUID
    direction: Literal["to_workspace", "from_workspace"]
    files: list[TransferFile] = Field(min_length=1, max_length=MAX_TRANSFER_FILES)
    total_bytes: StrictInt = Field(ge=0, le=MAX_TRANSFER_BYTES)

    @field_validator("files")
    @classmethod
    def unique_paths(cls, value: list[TransferFile]) -> list[TransferFile]:
        paths = [entry.path for entry in value]
        if len(paths) != len(set(paths)):
            raise ValueError("transfer paths must be unique")
        ordered = sorted(paths)
        if any(current.startswith(previous + "/") for previous, current in pairwise(ordered)):
            raise ValueError("a transfer file cannot also be another file's parent")
        return value

    def model_post_init(self, __context: Any, /) -> None:
        actual = sum(entry.size for entry in self.files)
        if actual != self.total_bytes:
            raise ValueError("total_bytes must equal the manifest file sizes")


class TransferState(StrictModel):
    transfer_id: UUID
    workspace_id: UUID
    state: Literal["admitted", "receiving", "ready", "committed", "aborted"]
    file_count: StrictInt = Field(ge=0)
    received_bytes: StrictInt = Field(ge=0)
    total_bytes: StrictInt = Field(ge=0)
    next_path: StrictStr | None = None


class HealthResponse(StrictModel):
    status: Literal["ok", "degraded"]
    protocol: Literal["v1"]
    runtime: Literal["ready", "unavailable"]
    sdk_version: StrictStr | None = None
    readiness: dict[str, Any] = Field(default_factory=dict)
