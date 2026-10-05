"""Generated public input/output schemas for all requested book tools."""

from __future__ import annotations

from copy import deepcopy
from importlib.resources import files
import json
from typing import Any

from jsonschema import Draft202012Validator

from . import models as dto
from . import config as config_dto

BOOK_TOOL_NAMES: tuple[str, ...] = (
    "audiobook_inspect_chapter",
    "audiobook_prepare_chapter",
    "audiobook_get_chapter",
    "audiobook_find_chunk",
    "audiobook_record_generation",
    "audiobook_import_audio",
    "audiobook_build",
    "audiobook_commit_build",
    "audiobook_get_job",
    "audiobook_cancel_job",
    "book_get_index_status",
    "audiobook_get_generations",
    "audiobook_get_book",
)

BOOK_MUTATING_TOOLS = frozenset({
    "audiobook_prepare_chapter",
    "audiobook_record_generation",
    "audiobook_import_audio",
    "audiobook_build",
    "audiobook_commit_build",
    "audiobook_cancel_job",
})

PROJECT_STORAGE_TOOL_NAMES: tuple[str, ...] = (
    "set_folder_indexing", "list_project_files", "read_project_file",
)
PROJECT_STORAGE_MUTATING_TOOLS = frozenset({"set_folder_indexing"})
ALL_ADDITIVE_TOOL_NAMES = (*BOOK_TOOL_NAMES, *PROJECT_STORAGE_TOOL_NAMES)
ALL_ADDITIVE_MUTATING_TOOLS = BOOK_MUTATING_TOOLS | PROJECT_STORAGE_MUTATING_TOOLS

_MODEL_PAIRS: dict[str, tuple[type[dto.StrictModel], type[dto.StrictModel]]] = {
    "audiobook_inspect_chapter": (dto.InspectRequest, dto.InspectResult),
    "audiobook_prepare_chapter": (dto.PrepareRequest, dto.PrepareResult),
    "audiobook_get_chapter": (dto.GetChapterRequest, dto.GetChapterResult),
    "audiobook_find_chunk": (dto.FindChunkRequest, dto.FindChunkResult),
    "audiobook_record_generation": (dto.RecordGenerationRequest, dto.RecordGenerationResult),
    "audiobook_import_audio": (dto.ImportAudioRequest, dto.JobRef),
    "audiobook_build": (dto.BuildRequest, dto.JobRef),
    "audiobook_commit_build": (dto.CommitBuildRequest, dto.CommitBuildResult),
    "audiobook_get_job": (dto.GetJobRequest, dto.GetJobResult),
    "audiobook_cancel_job": (dto.CancelJobRequest, dto.CancelJobResult),
    "book_get_index_status": (dto.IndexStatusRequest, dto.IndexStatusResult),
    "audiobook_get_generations": (dto.GetGenerationsRequest, dto.GetGenerationsResult),
    "audiobook_get_book": (dto.GetBookRequest, dto.GetBookResult),
}

_PROJECT_STORAGE_MODEL_PAIRS: dict[
    str, tuple[type[dto.StrictModel], type[dto.StrictModel]]
] = {
    "set_folder_indexing": (dto.SetFolderIndexingRequest, dto.SetFolderIndexingResult),
    "list_project_files": (dto.ListProjectFilesRequest, dto.ListProjectFilesResult),
    "read_project_file": (dto.ReadProjectFileRequest, dto.ReadProjectFileResult),
}

_DESCRIPTIONS: dict[str, str] = {
    "audiobook_inspect_chapter": "Inspect registered prose and tagged DOCX bytes and return a pinned speech projection.",
    "audiobook_prepare_chapter": "Validate assistant-selected speech ranges and persist an immutable prepared snapshot.",
    "audiobook_get_chapter": "Read current or historical chapter snapshots, chunks, takes, and accepted build metadata.",
    "audiobook_find_chunk": "Find literal spoken quotes or positions in a specific immutable build timeline.",
    "audiobook_record_generation": "Reserve or record evidence for an externally submitted generation request; never invokes TTS.",
    "audiobook_import_audio": "Import an authorized audio source as a durable asynchronous job.",
    "audiobook_build": "Assemble a candidate chapter or book audio build from pinned immutable inputs.",
    "audiobook_commit_build": "Accept or roll back a reviewed candidate build with current dependency checks.",
    "audiobook_get_job": "Read durable import or build job status and result.",
    "audiobook_cancel_job": "Request cancellation of a durable import or build job.",
    "book_get_index_status": "Read structural catalog and effective knowledge-index status for registered project files.",
    "audiobook_get_generations": "Recover generation request prompts and provider evidence without resubmitting work.",
    "audiobook_get_book": "Read book order, accepted build, chapter dependencies, and export references.",
}


def _schema(model: type[dto.StrictModel]) -> dict[str, Any]:
    return model.model_json_schema(mode="validation", ref_template="#/$defs/{model}")


def _envelope_schema(
    tool_name: str,
    result_schema: dict[str, Any],
    *,
    mutating: bool,
) -> dict[str, Any]:
    """Compose a strict tool envelope around one normative result DTO."""
    data_name = f"{tool_name}Data"
    definitions = dict(result_schema.pop("$defs", {}))
    definitions[data_name] = result_schema
    success_properties: dict[str, Any] = {
        "status": {"const": "success", "type": "string"},
        "replayed": {"type": "boolean"},
        "data": {"$ref": f"#/$defs/{data_name}"},
    }
    success_required = ["status", "replayed", "data"]
    if mutating:
        success_properties["operation_id"] = {"type": "string"}
        success_required.insert(1, "operation_id")
    success = {
        "type": "object", "additionalProperties": False,
        "properties": success_properties, "required": success_required,
    }
    error = {
        "type": "object", "additionalProperties": False,
        "properties": {
            "status": {"const": "error", "type": "string"},
            "reason": {"type": "string"},
            "message": {"type": "string"},
            "operation_outcome": {
                "enum": ["not_applied", "committed", "outcome_unknown"],
                "type": "string",
            },
            "correlation_id": {"type": "string"},
            "details": {"type": "object", "additionalProperties": True},
        },
        "required": ["status", "reason", "message", "operation_outcome", "correlation_id"],
    }
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$defs": definitions,
        "oneOf": [success, error],
    }


def build_book_schemas() -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    """Build detached schemas while keeping request and result contracts paired."""
    if tuple(_MODEL_PAIRS) != BOOK_TOOL_NAMES:
        raise RuntimeError("book DTO registry does not exactly match public tool order")
    requests: dict[str, dict[str, Any]] = {}
    results: dict[str, dict[str, Any]] = {}
    for name, (request_model, result_model) in _MODEL_PAIRS.items():
        requests[name] = _schema(request_model)
        results[name] = _schema(result_model)
    return requests, results


BOOK_REQUEST_SCHEMAS, BOOK_RESULT_SCHEMAS = build_book_schemas()


def build_project_storage_schemas() -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    if tuple(_PROJECT_STORAGE_MODEL_PAIRS) != PROJECT_STORAGE_TOOL_NAMES:
        raise RuntimeError("storage DTO registry does not exactly match public tool order")
    requests = {name: _schema(pair[0]) for name, pair in _PROJECT_STORAGE_MODEL_PAIRS.items()}
    results = {name: _schema(pair[1]) for name, pair in _PROJECT_STORAGE_MODEL_PAIRS.items()}
    return requests, results


PROJECT_STORAGE_REQUEST_SCHEMAS, PROJECT_STORAGE_RESULT_SCHEMAS = (
    build_project_storage_schemas()
)
CONFIG_SCHEMAS: dict[str, dict[str, Any]] = {
    name: _schema(model) for name, model in config_dto.CONFIG_MODELS.items()
}
BOOK_OUTPUT_SCHEMAS: dict[str, dict[str, Any]] = {
    name: _envelope_schema(name, _schema(pair[1]), mutating=name in BOOK_MUTATING_TOOLS)
    for name, pair in _MODEL_PAIRS.items()
}
PROJECT_STORAGE_OUTPUT_SCHEMAS: dict[str, dict[str, Any]] = {
    name: _envelope_schema(
        name, _schema(pair[1]), mutating=name in PROJECT_STORAGE_MUTATING_TOOLS
    )
    for name, pair in _PROJECT_STORAGE_MODEL_PAIRS.items()
}
DIRECTORY_MOVE_RESULT_SCHEMA = _schema(dto.DirectoryMoveResult)


def book_tool_definitions() -> list[dict[str, Any]]:
    """Return independent catalog definitions with generated strict schemas.

    This function intentionally does not import result_contracts or any tool
    catalog module. Integrators can pass these definitions through their usual
    boundary without creating a dependency cycle.
    """
    return [
        {
            "name": name,
            "description": _DESCRIPTIONS[name],
            "inputSchema": deepcopy(BOOK_REQUEST_SCHEMAS[name]),
            "outputSchema": deepcopy(BOOK_OUTPUT_SCHEMAS[name]),
        }
        for name in BOOK_TOOL_NAMES
    ]


def success_envelope(
    tool_name: str,
    data: dict[str, Any],
    *,
    operation_id: str | None = None,
    replayed: bool = False,
) -> dict[str, Any]:
    """Validate a book/storage data result and wrap it in the public success form."""
    if tool_name not in BOOK_OUTPUT_SCHEMAS and tool_name not in PROJECT_STORAGE_OUTPUT_SCHEMAS:
        raise KeyError(f"unknown additive tool: {tool_name}")
    mutating = tool_name in ALL_ADDITIVE_MUTATING_TOOLS
    result: dict[str, Any] = {"status": "success", "replayed": replayed, "data": data}
    if mutating:
        if operation_id is None:
            raise ValueError("mutating success requires operation_id")
        result["operation_id"] = operation_id
    elif operation_id is not None:
        raise ValueError("read success must omit operation_id")
    output_schema = (
        BOOK_OUTPUT_SCHEMAS.get(tool_name) or PROJECT_STORAGE_OUTPUT_SCHEMAS[tool_name]
    )
    Draft202012Validator(output_schema).validate(result)
    return result


def error_envelope(
    tool_name: str,
    *,
    reason: str,
    message: str,
    operation_outcome: str,
    correlation_id: str,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a strict error form; details is the contract's explicit JsonObject field."""
    if tool_name not in BOOK_OUTPUT_SCHEMAS and tool_name not in PROJECT_STORAGE_OUTPUT_SCHEMAS:
        raise KeyError(f"unknown additive tool: {tool_name}")
    result: dict[str, Any] = {
        "status": "error", "reason": reason, "message": message,
        "operation_outcome": operation_outcome, "correlation_id": correlation_id,
    }
    if details is not None:
        result["details"] = details
    output_schema = (
        BOOK_OUTPUT_SCHEMAS.get(tool_name) or PROJECT_STORAGE_OUTPUT_SCHEMAS[tool_name]
    )
    Draft202012Validator(output_schema).validate(result)
    return result


def with_directory_move_result(existing_schema: dict[str, Any]) -> dict[str, Any]:
    """Add the new directory variant while preserving the full legacy result schema."""
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "oneOf": [deepcopy(existing_schema), deepcopy(DIRECTORY_MOVE_RESULT_SCHEMA)],
    }


def project_storage_tool_definitions() -> list[dict[str, Any]]:
    """Return additive storage definitions independently of legacy catalogs."""
    return [
        {
            "name": name,
            "description": {
                "set_folder_indexing": "Persist an explicit folder indexing rule and report its policy revision.",
                "list_project_files": "List authorized project files and folders with effective access and indexing state.",
                "read_project_file": "Read a hash-verified byte range from an authorized project file.",
            }[name],
            "inputSchema": deepcopy(PROJECT_STORAGE_REQUEST_SCHEMAS[name]),
            "outputSchema": deepcopy(PROJECT_STORAGE_OUTPUT_SCHEMAS[name]),
        }
        for name in PROJECT_STORAGE_TOOL_NAMES
    ]


def validate_book_schema_size(*, maximum_bytes: int = 128_000) -> None:
    """Reject accidental unbounded schema expansion before package publication."""
    import json

    for registry_name, registry in (
        ("request", BOOK_REQUEST_SCHEMAS), ("result", BOOK_RESULT_SCHEMAS),
        ("storage request", PROJECT_STORAGE_REQUEST_SCHEMAS),
        ("storage result", PROJECT_STORAGE_RESULT_SCHEMAS),
        ("book output", BOOK_OUTPUT_SCHEMAS),
        ("storage output", PROJECT_STORAGE_OUTPUT_SCHEMAS),
        ("configuration", CONFIG_SCHEMAS),
    ):
        for tool_name, schema in registry.items():
            encoded_size = len(json.dumps(schema, separators=(",", ":")).encode("utf-8"))
            if encoded_size > maximum_bytes:
                raise RuntimeError(
                    f"{registry_name} schema for {tool_name} exceeds {maximum_bytes} bytes"
                )


def published_schema_document() -> dict[str, Any]:
    """Return the stable package-data payload generated from the DTO registry."""
    return {
        "schema_version": 1,
        "tools": {
            name: {
                "input": deepcopy(BOOK_REQUEST_SCHEMAS[name]),
                "output": deepcopy(BOOK_OUTPUT_SCHEMAS[name]),
            }
            for name in BOOK_TOOL_NAMES
        } | {
            name: {
                "input": deepcopy(PROJECT_STORAGE_REQUEST_SCHEMAS[name]),
                "output": deepcopy(PROJECT_STORAGE_OUTPUT_SCHEMAS[name]),
            }
            for name in PROJECT_STORAGE_TOOL_NAMES
        },
        "configuration": deepcopy(CONFIG_SCHEMAS),
        "directory_move_result": deepcopy(DIRECTORY_MOVE_RESULT_SCHEMA),
    }


def validate_published_schema_document() -> None:
    """Ensure the shipped schema artifact matches the generated DTO contracts."""
    path = files("cognita.books").joinpath("schemas/book-tool-schemas.json")
    try:
        published = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("published book tool schemas are missing or invalid") from exc
    if published != published_schema_document():
        raise RuntimeError("published book tool schemas are stale relative to DTO definitions")


validate_book_schema_size()
BOOK_OUTPUT_SCHEMAS = {name: Draft202012Validator.check_schema(schema) or schema for name, schema in BOOK_OUTPUT_SCHEMAS.items()}
PROJECT_STORAGE_OUTPUT_SCHEMAS = {
    name: Draft202012Validator.check_schema(schema) or schema
    for name, schema in PROJECT_STORAGE_OUTPUT_SCHEMAS.items()
}
BOOK_TOOL_DEFS = book_tool_definitions()
PROJECT_STORAGE_TOOL_DEFS = project_storage_tool_definitions()

__all__ = [
    "BOOK_TOOL_NAMES", "BOOK_MUTATING_TOOLS", "BOOK_REQUEST_SCHEMAS",
    "BOOK_RESULT_SCHEMAS", "book_tool_definitions", "validate_book_schema_size",
    "published_schema_document", "validate_published_schema_document",
    "PROJECT_STORAGE_TOOL_NAMES", "PROJECT_STORAGE_MUTATING_TOOLS",
    "ALL_ADDITIVE_TOOL_NAMES", "ALL_ADDITIVE_MUTATING_TOOLS",
    "PROJECT_STORAGE_REQUEST_SCHEMAS", "PROJECT_STORAGE_RESULT_SCHEMAS",
    "BOOK_OUTPUT_SCHEMAS", "PROJECT_STORAGE_OUTPUT_SCHEMAS",
    "CONFIG_SCHEMAS", "project_storage_tool_definitions",
    "BOOK_TOOL_DEFS", "PROJECT_STORAGE_TOOL_DEFS", "success_envelope",
    "error_envelope", "with_directory_move_result", "DIRECTORY_MOVE_RESULT_SCHEMA",
]
