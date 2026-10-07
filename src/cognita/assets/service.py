"""The single use-case orchestrator for Cognita's PNG asset tier."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import inspect
import json
import os
import stat as stat_module
import time
import unicodedata
from collections.abc import Mapping
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, NoReturn
from uuid import uuid4

from ..backups import BACKUPS_DIRNAME, backup_id_of, resolve_target
from ..chunking import chunk_text
from .limits import (
    MAX_INLINE_PNG_BYTES,
    MAX_METADATA_BYTES,
    MAX_PNG_BYTES,
    MAX_PNG_CHUNK_BYTES,
    MAX_PROMPT_BYTES,
    bounded_string,
    canonical_json,
    integer,
    operation_id,
    sha256_value,
    utf8_len,
    validate_data_url,
)
from .metadata import prepare_metadata, search_projection
from .models import AssetError, AssetRecord, normalize_connector_id
from .ocr_service import (
    OCRService,
    _ocr_timeout_seconds,
    configured_pipeline_identity,
    effective_dimension_limit,
    result_facts_from_result,
    source_snapshot_from_result,
)
from .ocr_store import OcrResultFacts, OcrSearchChunk, OcrSourceSnapshot, normalize_languages_key
from .png import embed_metadata, scan_png
from .publication import AssetPublisher, DetachedFile, PublishedFile
from .wire import image_result

_SERVICE_CONNECTOR = object()


class AssetService:
    """Coordinate validation, publication, catalog replacement and safe reads."""

    def __init__(self, project: Any, repository: Any | None = None, *, logger: Any | None = None, backup_keep: int = 20, embedder: Any | None = None, reranker: Any | None = None, limits: Any | None = None, connector_id: str | None = None, ocr_service: OCRService | None = None, scheduler: Any | None = None, book_mutation_policy: Callable[[], Any] | None = None, effective_index_policy: Callable[[], Any] | None = None):
        self.project = project
        self.project_name = getattr(project, "name", "project")
        # The gateway supplies this trusted identity after connector/project
        # authorization. None remains source-compatible for local maintenance
        # callers and is stored in the reserved legacy namespace by the repo.
        if connector_id is not None:
            normalize_connector_id(connector_id)
        self.connector_id = connector_id
        self.documents_dir = Path(getattr(project, "documents_dir", project))
        self.data_dir = Path(getattr(project, "data_dir", self.documents_dir.parent / ".cognita-data"))
        self.repository = repository
        self.log = logger
        self.publisher = AssetPublisher(self.documents_dir, self.data_dir)
        self._memory: dict[str, AssetRecord] = {}
        self.backup_keep = backup_keep
        self.embedder = embedder
        self.reranker = reranker
        self.scheduler = scheduler
        self.book_mutation_policy_provider = book_mutation_policy
        self.effective_index_policy_provider = effective_index_policy
        self.recovery_blocked = False
        self.max_png_bytes = min(int(getattr(limits, "asset_max_png_bytes", MAX_PNG_BYTES)), MAX_PNG_BYTES)
        self.max_metadata_bytes = min(int(getattr(limits, "asset_max_metadata_bytes", MAX_METADATA_BYTES)), MAX_METADATA_BYTES)
        self.max_prompt_bytes = min(int(getattr(limits, "asset_max_prompt_bytes", MAX_PROMPT_BYTES)), MAX_PROMPT_BYTES)
        self.max_dimension = int(getattr(limits, "asset_max_dimension", 4_096))
        self.max_pixels = int(getattr(limits, "asset_max_pixels", 16_777_216))
        self.max_chunks = int(getattr(limits, "asset_max_chunks", 2_048))
        self.max_chunk_bytes = int(
            getattr(limits, "asset_max_chunk_bytes", MAX_PNG_CHUNK_BYTES)
        )
        self.max_list_results = min(int(getattr(limits, "asset_max_list_results", 200)), 200)
        self.max_search_results = min(int(getattr(limits, "asset_max_search_results", 20)), 20)
        self.ocr_service = ocr_service or OCRService(limits, logger=logger)
        self.chunk_size = int(getattr(limits, "chunk_size", 1_000))
        self.chunk_overlap = int(getattr(limits, "chunk_overlap", 200))
        # Watcher attachment records this identity and passes it back to the
        # targeted reconciler.  Keeping the fallback here also protects
        # startup/manual asset walks that run before a watcher is attached.
        self._documents_root_identity = self._root_identity(allow_missing=True)

    @staticmethod
    def _supports_keyword(method: Any, name: str) -> bool:
        try:
            parameters = inspect.signature(method).parameters.values()
        except (TypeError, ValueError):
            return False
        return any(
            parameter.name == name or parameter.kind == inspect.Parameter.VAR_KEYWORD
            for parameter in parameters
        )

    async def _repository_call(self, method: Any, *args: Any, **kwargs: Any) -> Any:
        supported = {
            key: value for key, value in kwargs.items()
            if self._supports_keyword(method, key)
        }
        return await method(*args, **supported)

    def _log(self, event: str, **fields: Any) -> None:
        if self.log is not None:
            try:
                quiet_reconcile = False
                if event == "asset.reconcile" and fields.get("outcome") == "complete":
                    changed = any(
                        int(fields.get(key, 0) or 0) > 0
                        for key in ("indexed", "metadata_refreshed", "removed")
                    )
                    quiet_reconcile = (
                        not changed and not int(fields.get("error_count", 0) or 0)
                    )
                writer = self.log.debug if quiet_reconcile else self.log.info
                writer(event, extra={"asset": fields})
            except (AttributeError, TypeError):
                pass

    def _target(self, filepath: Any) -> tuple[str, Path]:
        if not isinstance(filepath, str) or not filepath or "\0" in filepath:
            raise AssetError("invalid_path", "filepath is invalid")
        relative = filepath.replace("\\", "/")
        if not relative.lower().endswith(".png"):
            raise AssetError("unsupported_media_type", "only .png asset paths are supported")
        parts = relative.split("/")
        if any(part in {"", ".", ".."} for part in parts):
            raise AssetError("invalid_path", "filepath is not a safe project-relative path")
        if parts[0].casefold() == BACKUPS_DIRNAME.casefold():
            raise AssetError("invalid_path", "the backup tree is not an asset destination")
        target = resolve_target(self.documents_dir, relative)
        if target is None:
            raise AssetError("invalid_path", "filepath is outside the project")
        lexical = self.documents_dir
        for part in parts[:-1]:
            lexical = lexical / part
            if lexical.is_symlink():
                raise AssetError("invalid_path", "asset paths may not traverse links")
        parent = self.documents_dir
        for part in parts:
            if parent.is_dir():
                wanted = unicodedata.normalize("NFC", part).casefold()
                for child in parent.iterdir():
                    if child.name == part:
                        break
                    if unicodedata.normalize("NFC", child.name).casefold() == wanted:
                        raise AssetError("invalid_path", "asset path collides with an existing name")
            parent /= part
        return relative, target

    def _mutation_allowed(self, relative: str, operation: str) -> None:
        provider = self.book_mutation_policy_provider
        policy = provider() if provider is not None else None
        if policy is None:
            return
        decision = policy.decide(
            relative, operation=operation,
            source_exists=(self.documents_dir / Path(relative)).is_file(),
        )
        if not decision.allowed or decision.preserve_original:
            raise AssetError(
                decision.reason if not decision.allowed else "configuration_conflict",
                "this managed book path is protected or requires its guarded document workflow",
            )

    def _effective_indexed(self, relative: str) -> bool:
        provider = self.effective_index_policy_provider
        policy = provider() if provider is not None else None
        return policy is None or policy.decision(relative).indexed

    async def _searchable_asset_sources(self) -> list[str] | None:
        provider = self.effective_index_policy_provider
        policy = provider() if provider is not None else None
        if policy is None:
            return None
        if self.repository is not None and hasattr(self.repository, "searchable_source_paths"):
            sources = await self.repository.searchable_source_paths()
        else:
            sources = list(self._memory)
        return [source for source in sources if policy.decision(source).indexed]

    async def _claim(self, tool: str, op: str, fingerprint: str) -> tuple[str, Any | None]:
        if self.repository is not None and hasattr(self.repository, "claim_operation"):
            return await self._repository_call(
                self.repository.claim_operation, op, tool, fingerprint,
                connector_id=self.connector_id,
            )
        return "claimed", None

    async def _finish(
        self, op: str, result: dict[str, Any], *, failed: bool = False, tool: str | None = None,
        connector_id: str | None | object = _SERVICE_CONNECTOR,
    ) -> None:
        if self.repository is not None and hasattr(self.repository, "finish_operation"):
            resolved_connector = (
                self.connector_id if connector_id is _SERVICE_CONNECTOR else connector_id
            )
            await self._repository_call(
                self.repository.finish_operation, op, result, failed=failed,
                connector_id=resolved_connector, tool=tool,
            )

    def _scan(self, data: bytes):
        return scan_png(
            data,
            metadata_limit=self.max_metadata_bytes,
            byte_limit=self.max_png_bytes,
            dimension_limit=self.max_dimension,
            pixel_limit=self.max_pixels,
            chunk_limit=self.max_chunks,
            chunk_byte_limit=self.max_chunk_bytes,
        )

    def _prepare_metadata(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        metadata = prepare_metadata(*args, **kwargs)
        if len(canonical_json(metadata)) > self.max_metadata_bytes:
            raise AssetError("metadata_limit", "metadata exceeds its configured limit")
        prompts = metadata.get("prompts", {})
        if isinstance(prompts, Mapping):
            for value in prompts.values():
                if isinstance(value, str) and utf8_len(value) > self.max_prompt_bytes:
                    raise AssetError("metadata_limit", "prompt exceeds its configured limit")
        return metadata

    def _read_png(self, target: Path) -> tuple[bytes, str]:
        try:
            with target.open("rb") as handle:
                before = target.stat()
                data = handle.read(self.max_png_bytes + 1)
                after = target.stat()
        except OSError as exc:
            raise AssetError("not_found", "asset could not be read") from exc
        if len(data) > self.max_png_bytes:
            raise AssetError("byte_limit", "PNG exceeds the configured byte limit")
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
            after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns
        ) or len(data) != before.st_size:
            raise AssetError("stale_file", "asset changed while it was being read")
        return data, hashlib.sha256(data).hexdigest()

    async def ocr_asset(self, args: Mapping[str, Any]) -> dict[str, Any]:
        """Read and OCR one authorized project-relative static PNG.

        The service resolves the path before any OCR/cache activity.  The OCR
        adapter receives only the immutable bytes, then revalidates the source
        before returning its derived result.
        """
        started = time.monotonic()
        selected_timeout = _ocr_timeout_seconds(args.get("timeout_seconds"))
        outer_deadline = started + selected_timeout
        requested_project = args.get("project")
        if requested_project is not None and requested_project != self.project_name:
            raise AssetError("project_unavailable", "project is unavailable")
        try:
            relative, target = self._target(args.get("filepath"))
        except AssetError as exc:
            if exc.reason == "unsupported_media_type":
                raise AssetError("wrong_media_type", "OCR accepts PNG files only") from exc
            raise
        if target.is_symlink():
            raise AssetError("invalid_path", "asset paths may not be links")
        if not target.is_file():
            raise AssetError("not_found", "asset was not found")
        languages = self.ocr_service._languages(args.get("languages"))

        # Hash/validate before cache activity.  This uses the same bounded,
        # O_NOATIME descriptor path as inference and therefore cannot expose a
        # cache oracle for an unauthorized or stale source.
        snapshot_dict, _facts, _image = self.ocr_service._snapshot(target, relative)
        del _image
        identity = configured_pipeline_identity(self.ocr_service.config, languages)
        if self.repository is not None and identity is not None and hasattr(self.repository, "get_ocr_result"):
            _model_fingerprint, pipeline_fingerprint = identity
            languages_key = normalize_languages_key(languages)
            cached = await self._await_ocr_phase(
                self.repository.get_ocr_result(
                    snapshot_dict["source_sha256"], pipeline_fingerprint, languages_key,
                ), outer_deadline, selected_timeout, "cache",
            )
            if cached is not None:
                fresh = await self._await_ocr_phase(
                    self.repository.validate_ocr_freshness(
                        relative, snapshot_dict["source_sha256"],
                    ), outer_deadline, selected_timeout, "cache",
                )
                if fresh:
                    chunks = await self._await_ocr_phase(
                        self.repository.get_ocr_chunks(
                            snapshot_dict["source_sha256"], pipeline_fingerprint, languages_key,
                        ), outer_deadline, selected_timeout, "cache",
                    )
                    snapshot = self._ocr_snapshot(snapshot_dict)
                    # Same-byte copies/moves get their source mapping only after
                    # current catalog facts are checked in the transaction.
                    self.ocr_service._freshness_check(target, snapshot_dict)
                    if self._effective_indexed(relative):
                        await self._await_ocr_phase(
                            self.repository.publish_ocr_result(snapshot, cached, chunks),
                            outer_deadline, selected_timeout, "publication",
                        )
                    return self._cached_ocr_result(cached, relative, started)

        coalesce_key = (
            self.project_name, snapshot_dict["source_sha256"],
            identity[1] if identity is not None else "unqualified",
            normalize_languages_key(languages),
        )

        async def compute() -> tuple[dict[str, Any], Any, list[OcrSearchChunk]]:
            # The shared computation uses the maximum caller contract. Each
            # waiter below retains its own shorter deadline, and the last timed
            # out/canceled waiter cancels and owns cleanup of this task.
            compute_started = time.monotonic()
            compute_deadline = compute_started + 600
            result = await self.ocr_service.extract(
                self.project, relative, languages=languages, target=target,
                timeout_seconds=600, outer_deadline=compute_deadline,
                started_at=compute_started,
            )
            if result["sha256"] != snapshot_dict["source_sha256"]:
                raise AssetError("source_changed", "asset changed during OCR")
            result_facts = result_facts_from_result(result)
            if self.repository is None or not hasattr(self.repository, "publish_ocr_result"):
                return result, result_facts, []
            text_chunks = chunk_text(result_facts.text, self.chunk_size, self.chunk_overlap)
            vectors = await self._ocr_vectors(
                [chunk.content for chunk in text_chunks], compute_deadline,
            )
            chunks = [
                OcrSearchChunk(index, chunk.content, vector)
                for index, (chunk, vector) in enumerate(zip(text_chunks, vectors, strict=True))
            ]
            return result, result_facts, chunks

        try:
            result, result_facts, chunks = await self.ocr_service.coalesce(
                coalesce_key, compute,
                timeout_seconds=outer_deadline - time.monotonic(),
            )
        except TimeoutError as exc:
            raise AssetError(
                "timeout",
                f"OCR deadline exceeded phase=inference limit_seconds={selected_timeout}",
                details={"phase": "inference", "limit_seconds": selected_timeout},
            ) from exc
        # A coalesced leader may have used another same-byte authorized path;
        # each caller gets its own source identity and publication mapping.
        result = dict(result)
        result["filepath"] = relative
        result["source_snapshot"] = dict(snapshot_dict)
        if (self.repository is not None and hasattr(self.repository, "publish_ocr_result")
                and self._effective_indexed(relative)):
            try:
                source_snapshot = source_snapshot_from_result(result)
                self.ocr_service._freshness_check(target, result["source_snapshot"])
                remaining = outer_deadline - time.monotonic()
                if remaining <= 0:
                    raise AssetError(
                        "timeout",
                        f"OCR deadline exceeded phase=publication limit_seconds={selected_timeout}",
                        details={"phase": "publication", "limit_seconds": selected_timeout},
                    )
                await asyncio.wait_for(
                    self.repository.publish_ocr_result(source_snapshot, result_facts, chunks),
                    timeout=remaining,
                )
            except AssetError:
                raise
            except TimeoutError as exc:
                raise AssetError(
                    "timeout",
                    f"OCR deadline exceeded phase=publication limit_seconds={selected_timeout}",
                    details={"phase": "publication", "limit_seconds": selected_timeout},
                ) from exc
            except Exception as exc:
                raise AssetError("ocr_failed", "OCR derived publication failed") from exc
        result["duration_ms"] = max(0, round((time.monotonic() - started) * 1000))
        return self._public_ocr_result(result)

    @staticmethod
    async def _await_ocr_phase(
        awaitable: Any, deadline: float, limit_seconds: int, phase: str,
    ) -> Any:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            if inspect.iscoroutine(awaitable):
                awaitable.close()
            raise AssetError(
                "timeout", f"OCR deadline exceeded phase={phase} limit_seconds={limit_seconds}",
                details={"phase": phase, "limit_seconds": limit_seconds},
            )
        try:
            return await asyncio.wait_for(awaitable, timeout=remaining)
        except TimeoutError as exc:
            raise AssetError(
                "timeout", f"OCR deadline exceeded phase={phase} limit_seconds={limit_seconds}",
                details={"phase": phase, "limit_seconds": limit_seconds},
            ) from exc

    async def _ocr_vectors(self, texts: list[str], deadline: float) -> list[list[float]]:
        if not texts:
            return []
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError
        if self.scheduler is not None:
            job = self.scheduler.open_job(
                self.project_name, "ocr", estimated_chunks=len(texts),
            )
            try:
                return await asyncio.wait_for(
                    self.scheduler.embed(job, texts), timeout=remaining,
                )
            finally:
                outcome = "canceled" if job.state == "canceled" else "completed"
                await self.scheduler.close_job(job, outcome=outcome)
        if self.embedder is None:
            raise RuntimeError("OCR text embedder is unavailable")
        return await asyncio.wait_for(
            asyncio.to_thread(self.embedder.embed, texts), timeout=remaining,
        )

    @staticmethod
    def _ocr_snapshot(snapshot: Mapping[str, Any]) -> OcrSourceSnapshot:
        return OcrSourceSnapshot(
            filepath=str(snapshot["filepath"]),
            source_sha256=str(snapshot["source_sha256"]),
            file_size=int(snapshot["file_size"]),
            width=int(snapshot["width"]), height=int(snapshot["height"]),
            snapshot_token=str(snapshot["snapshot_token"]),
        )

    def _cached_ocr_result(
        self, cached: OcrResultFacts, filepath: str, started: float,
    ) -> dict[str, Any]:
        return {
            "status": "success", "outcome": cached.outcome,
            "project": self.project_name, "filepath": filepath,
            "sha256": cached.source_sha256,
            "width": cached.width, "height": cached.height,
            "text": cached.text, "regions": list(cached.regions),
            "engine": {
                "name": cached.engine_name, "version": cached.engine_version,
                "model_fingerprint": cached.model_fingerprint,
                "pipeline_version": cached.pipeline_version,
                "device": cached.device, "backend": cached.backend,
            },
            "languages": cached.languages_key.split(","),
            "cache_hit": True, "searchable": cached.searchable,
            "warnings": list(cached.warnings),
            "duration_ms": max(0, round((time.monotonic() - started) * 1000)),
            "limits": {
                "max_png_bytes": min(int(getattr(
                    self.ocr_service.config, "ocr_max_png_bytes", 16 * 1024 * 1024,
                )), MAX_PNG_BYTES),
                "max_pixels": int(getattr(
                    self.ocr_service.config, "ocr_max_pixels", 16_777_216,
                )),
                "max_dimension": effective_dimension_limit(self.ocr_service.config),
                "max_regions": int(getattr(
                    self.ocr_service.config, "ocr_max_regions", 10_000,
                )),
                "max_result_bytes": int(getattr(
                    self.ocr_service.config, "ocr_max_result_bytes", 1_048_576,
                )),
            },
        }

    @staticmethod
    def _public_ocr_result(result: Mapping[str, Any]) -> dict[str, Any]:
        public = dict(result)
        public.pop("source_snapshot", None)
        public.pop("pipeline_fingerprint", None)
        return public

    async def _vectors(self, projection: str) -> list[list[float]]:
        if self.embedder is None:
            if self.repository is not None:
                raise AssetError("index_failed", "asset embedder is unavailable")
            return []
        vectors = await asyncio.to_thread(self.embedder.embed, [projection])
        if not vectors:
            raise AssetError("index_failed", "asset metadata could not be embedded")
        return vectors

    async def _commit_record(
        self, record: AssetRecord, operation: str | None = None, result: dict | None = None,
        *, tool: str | None = None,
    ) -> None:
        projection = search_projection(record.metadata, record.filepath)
        target = self.documents_dir / Path(record.filepath)
        if record.file_mtime is None:
            record.file_mtime = datetime.fromtimestamp(target.stat().st_mtime, UTC)
        searchable = self._effective_indexed(record.filepath)
        vectors = await self._vectors(projection) if searchable else []
        record.indexed = searchable
        if result is not None:
            result["indexed"] = searchable
        if self.repository is not None:
            if operation and result is not None and hasattr(self.repository, "commit_asset_operation"):
                await self._repository_call(
                    self.repository.commit_asset_operation, record, projection, vectors,
                    operation, result, connector_id=self.connector_id, tool=tool,
                    searchable=searchable,
                )
            else:
                await self.repository.replace_asset(
                    record, projection, vectors, searchable=searchable,
                )
                if operation and result is not None:
                    await self._finish(operation, result, tool=tool)
        self._memory[record.filepath] = record

    async def _fail_claim(
        self, op: str, reason: str, message: str = "asset operation failed", *,
        tool: str | None = None,
        connector_id: str | None | object = _SERVICE_CONNECTOR,
    ) -> None:
        # Identical retries must replay the bounded reason and message; changed
        # arguments remain an operation conflict.
        resolved_connector = (
            self.connector_id if connector_id is _SERVICE_CONNECTOR else connector_id
        )
        try:
            await self._finish(
                op, {"status": "error", "reason": reason, "message": message[:512]}, failed=True,
                tool=tool, connector_id=connector_id,
            )
        except Exception:  # noqa: BLE001 - failure recording is best-effort
            self._log("asset.operation.failure_record_failed", project=self.project_name,
                      connector_id=resolved_connector, tool=tool, operation_id=op, reason=reason)

    def _operation_outcome(self, tool: str, operation_id: str, state: str) -> None:
        if state == "replay":
            self._log("asset.operation.replay", project=self.project_name,
                      connector_id=self.connector_id, tool=tool, operation_id=operation_id)
        elif state in {"conflict", "failed"}:
            self._log("asset.operation.conflict", project=self.project_name,
                      connector_id=self.connector_id, tool=tool, operation_id=operation_id,
                      state=state)

    async def put_asset(self, args: Mapping[str, Any]) -> dict[str, Any]:
        if self.recovery_blocked:
            raise AssetError("recovery_required", "asset recovery must complete before mutation")
        relative, target = self._target(args.get("filepath"))
        self._mutation_allowed(relative, "write")
        image = args.get("image")
        if not isinstance(image, Mapping) or set(image) - {"image_url", "output_hint"}:
            raise AssetError("invalid_arguments", "image must contain image_url and optional output_hint")
        if "output_hint" in image:
            bounded_string(image["output_hint"], "output_hint", 4_096)
        op = operation_id(args.get("operation_id"))
        encoded = validate_data_url(image.get("image_url"))
        if len(encoded) > ((self.max_png_bytes + 2) // 3) * 4:
            raise AssetError("encoded_limit", "image_url exceeds the configured PNG limit")
        staged, received_size, received_hash = self.publisher.stage_base64(
            op, encoded, connector_id=self.connector_id, tool="put_asset"
        )
        if received_size > self.max_png_bytes:
            self.publisher.cleanup(staged)
            raise AssetError("byte_limit", "PNG exceeds the configured byte limit")
        claimed = False
        published: PublishedFile | None = None
        try:
            expected_size = args.get("expected_received_size")
            if expected_size is not None and integer(expected_size, "expected_received_size", 1, MAX_PNG_BYTES) != received_size:
                raise AssetError("size_mismatch", "received PNG size differs from expected size")
            if args.get("expected_received_sha256") is not None and sha256_value(args["expected_received_sha256"], "expected_received_sha256") != received_hash:
                raise AssetError("hash_mismatch", "received PNG hash differs from expected hash")
            raw = staged.read_bytes()
            facts, _ = self._scan(raw)
            fingerprint = hashlib.sha256(
                json.dumps({k: v for k, v in args.items() if k != "image"}, sort_keys=True, separators=(",", ":")).encode()
                + received_hash.encode()
            ).hexdigest()
            state, replay = await self._claim("put_asset", op, fingerprint)
            self._operation_outcome("put_asset", op, state)
            if state == "replay":
                return self._replay_result(replay, "put_asset")
            if state == "failed":
                self._replay_failure(replay, "put_asset")
            if state == "conflict":
                raise AssetError("operation_conflict", "operation_id was already used")
            if state == "busy":
                raise AssetError("busy", "matching operation is still running")
            claimed = True
            existing = await self._get_record(relative)
            if target.exists():
                if args.get("overwrite") is not True:
                    raise AssetError("destination_exists", "destination already exists")
                _, current_hash = self._read_png(target)
                expected = args.get("expected_current_sha256")
                if expected is None or sha256_value(expected, "expected_current_sha256") != current_hash:
                    raise AssetError("stale_file", "current file hash is required for overwrite")
            elif args.get("expected_current_sha256") is not None:
                raise AssetError("stale_file", "expected_current_sha256 cannot target a new file")
            storage = args.get("metadata_storage", "auto")
            if storage not in {"auto", "embedded", "catalog"}:
                raise AssetError("invalid_arguments", "metadata_storage is invalid")
            if storage == "auto":
                storage = "catalog" if facts.cabx_chunk_count else "embedded"
            if storage == "embedded" and facts.cabx_chunk_count:
                raise AssetError("provenance_requires_preserve", "caBX PNGs require catalog storage")
            existing_asset_id = self._field(existing, "asset_id") if existing is not None else None
            asset_id = str(existing_asset_id or uuid4())
            metadata = self._prepare_metadata(
                args.get("metadata"), action=args.get("metadata_action", "merge"),
                asset_id=asset_id, received_sha256=received_hash,
                existing=self._json_mapping(self._field(existing, "metadata"),
                                            "put_asset.metadata"),
                native=facts.embedded_metadata,
            )
            final = raw if storage == "catalog" else embed_metadata(raw, metadata)
            if len(final) > self.max_png_bytes:
                raise AssetError("byte_limit", "final PNG exceeds the configured byte limit")
            received_backup = None if storage == "catalog" else self.publisher.received_backup(staged, relative)
            published = self.publisher.publish(
                staged, relative, operation_id=op, connector_id=self.connector_id,
                tool="put_asset", overwrite=target.exists(), final_data=final,
            )
            record = AssetRecord(
                asset_id,
                relative, metadata, received_size, received_hash, len(final),
                hashlib.sha256(final).hexdigest(), facts.width, facts.height, storage,
                int(self._field(existing, "metadata_revision", 0)) + 1, False,
                facts.provenance_state, facts.cabx_chunk_count,
                facts.cognita_chunks > 0 or storage == "embedded",
                backup_id_of(published.backup_path) if published.backup_path else None,
                received_backup,
            )
            result = self._result(record)
            result["indexed"] = True
            await self._commit_record(record, op, result, tool="put_asset")
            self.publisher.commit(published)
            self._log("asset.publish.committed", project=self.project_name, filepath=relative,
                      operation_id=op, final_sha256=record.final_sha256,
                      received_size=received_size, final_size=len(final),
                      width=facts.width, height=facts.height)
            return result
        except AssetError as exc:
            if published is not None:
                try:
                    self.publisher.rollback(published)
                except AssetError as rollback_error:
                    self.recovery_blocked = True
                    await self._fail_claim(op, "recovery_required", rollback_error.message, tool="put_asset")
                    raise rollback_error from exc
            if claimed:
                await self._fail_claim(op, exc.reason, exc.message, tool="put_asset")
            raise
        except Exception as exc:
            if published is not None:
                try:
                    self.publisher.rollback(published)
                except AssetError:
                    self.recovery_blocked = True
            if claimed:
                await self._fail_claim(op, "internal_error", "asset operation failed", tool="put_asset")
            raise AssetError("internal_error", "asset operation failed") from exc
        finally:
            if published is None:
                self.publisher.cleanup(staged)

    async def update_asset_metadata(self, args: Mapping[str, Any]) -> dict[str, Any]:
        if self.recovery_blocked:
            raise AssetError("recovery_required", "asset recovery must complete before mutation")
        relative, target = self._target(args.get("filepath"))
        self._mutation_allowed(relative, "write")
        op = operation_id(args.get("operation_id"))
        fingerprint = hashlib.sha256(
            json.dumps(dict(args), sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        state, replay = await self._claim("update_asset_metadata", op, fingerprint)
        self._operation_outcome("update_asset_metadata", op, state)
        if state == "replay":
            return self._replay_result(replay, "update_asset_metadata")
        if state == "failed":
            self._replay_failure(replay, "update_asset_metadata")
        if state == "conflict":
            raise AssetError("operation_conflict", "operation_id was already used")
        if state == "busy":
            raise AssetError("busy", "matching operation is still running")
        published: PublishedFile | None = None
        staged: Path | None = None
        try:
            if not target.is_file():
                raise AssetError("not_found", "asset was not found")
            raw, current_hash = self._read_png(target)
            size = len(raw)
            if sha256_value(args.get("expected_sha256"), "expected_sha256") != current_hash:
                raise AssetError("stale_file", "current file hash differs from expected hash")
            facts, _ = self._scan(raw)
            current = await self._get_record(relative)
            metadata = self._prepare_metadata(
                args.get("metadata"), action=args.get("metadata_action"),
                asset_id=self._field(current, "asset_id", str(uuid4())),
                received_sha256=self._field(current, "received_sha256", current_hash),
                existing=self._json_mapping(self._field(current, "metadata"),
                                            "update_asset_metadata.metadata")
                        or facts.embedded_metadata or {},
            )
            storage = args.get("metadata_storage", "preserve_current")
            if storage == "preserve_current":
                storage = self._field(current, "metadata_storage",
                                      "catalog" if facts.cabx_chunk_count else "embedded")
            elif storage == "auto":
                storage = "catalog" if facts.cabx_chunk_count else "embedded"
            if storage not in {"embedded", "catalog"}:
                raise AssetError("invalid_arguments", "metadata_storage is invalid")
            if storage == "embedded" and facts.cabx_chunk_count:
                raise AssetError("provenance_requires_preserve", "caBX PNGs require catalog storage")
            final = raw if storage == "catalog" else embed_metadata(raw, metadata)
            if len(final) > self.max_png_bytes:
                raise AssetError("byte_limit", "final PNG exceeds the configured byte limit")
            if final != raw:
                staged = self.publisher.stage(
                    op, final, connector_id=self.connector_id, tool="update_asset_metadata"
                )
                published = self.publisher.publish(
                    staged, relative, operation_id=op, connector_id=self.connector_id,
                    tool="update_asset_metadata", overwrite=True, final_data=final,
                )
            record = AssetRecord(
                str(self._field(current, "asset_id", str(uuid4()))), relative, metadata,
                int(self._field(current, "received_size", size)),
                str(self._field(current, "received_sha256", current_hash)), len(final),
                hashlib.sha256(final).hexdigest(), facts.width, facts.height, storage,
                int(self._field(current, "metadata_revision", 0)) + 1, False,
                facts.provenance_state, facts.cabx_chunk_count,
                facts.cognita_chunks > 0 or storage == "embedded",
                backup_id_of(published.backup_path) if published and published.backup_path else None,
                None,
            )
            result = self._result(record)
            result["indexed"] = True
            await self._commit_record(record, op, result, tool="update_asset_metadata")
            if published is not None:
                self.publisher.commit(published)
            self._log("asset.metadata.updated", filepath=relative, operation_id=op)
            return result
        except AssetError as exc:
            if published is not None:
                try:
                    self.publisher.rollback(published)
                except AssetError as rollback_error:
                    self.recovery_blocked = True
                    await self._fail_claim(op, "recovery_required", rollback_error.message, tool="update_asset_metadata")
                    raise rollback_error from exc
            if staged is not None:
                self.publisher.cleanup(staged)
            await self._fail_claim(op, exc.reason, exc.message, tool="update_asset_metadata")
            raise
        except Exception as exc:
            if published is not None:
                try:
                    self.publisher.rollback(published)
                except AssetError:
                    self.recovery_blocked = True
            if staged is not None:
                self.publisher.cleanup(staged)
            await self._fail_claim(op, "internal_error", "asset metadata update failed", tool="update_asset_metadata")
            raise AssetError("internal_error", "asset metadata update failed") from exc

    async def remove_asset(self, args: Mapping[str, Any]) -> dict[str, Any]:
        """Delete one PNG and detach its project-local derived state safely.

        The source file and catalog are deliberately treated as independently
        observable: reconciliation must be able to remove a stale catalog row or
        an unindexed on-disk PNG.  A verified project backup is made before the
        first unlink, and is retained as the operator recovery handle.
        """
        if self.recovery_blocked:
            raise AssetError("recovery_required", "asset recovery must complete before mutation")
        relative, target = self._target(args.get("filepath"))
        self._mutation_allowed(relative, "remove")
        if target.is_symlink():
            raise AssetError("invalid_path", "asset paths may not be links")
        op = operation_id(args.get("operation_id"))
        expected = args.get("expected_sha256")
        if expected is not None:
            expected = sha256_value(expected, "expected_sha256")
        fingerprint = hashlib.sha256(
            json.dumps(
                {"filepath": relative, "expected_sha256": expected},
                sort_keys=True, separators=(",", ":"),
            ).encode()
        ).hexdigest()
        state, replay = await self._claim("remove_asset", op, fingerprint)
        self._operation_outcome("remove_asset", op, state)
        if state == "replay":
            return self._replay_result(replay, "remove_asset")
        if state == "failed":
            self._replay_failure(replay, "remove_asset")
        if state == "conflict":
            raise AssetError("operation_conflict", "operation_id was already used")
        if state == "busy":
            raise AssetError("busy", "matching operation is still running")

        claimed = True
        backup: Path | None = None
        detached: DetachedFile | None = None
        backup_id: str | None = None
        file_data: bytes | None = None
        file_digest: str | None = None
        catalog_present = False
        ocr_present = False
        file_present = False
        mutation_started = False
        try:
            catalog_row = await self._get_record(relative)
            ocr_row = None
            if self.repository is not None and hasattr(self.repository, "get_ocr_source"):
                ocr_row = await self._repository_call(self.repository.get_ocr_source, relative)
            catalog_present = catalog_row is not None
            ocr_present = ocr_row is not None
            file_present = target.is_file()
            if not file_present and not catalog_present and not ocr_present:
                raise AssetError("not_found", "asset was not found")
            if file_present:
                file_data, file_digest = self._read_png(target)
                # Deletion uses the same authorized static-PNG boundary as the
                # rest of the asset surface; an arbitrary/corrupt .png file is
                # not silently treated as a managed asset.
                self._scan(file_data)
                if expected is not None and file_digest != expected:
                    raise AssetError("stale_file", "current file hash differs from expected hash")
                # This call performs the shared backup copy and verifies it is
                # byte-identical before deletion is allowed to proceed.
                backup = self.publisher.backup_for_delete(relative)
                if backup is None:
                    raise AssetError("backup_failed", "could not back up existing asset")
                backup_id = backup_id_of(backup)
                if backup_id is None:
                    raise AssetError("backup_failed", "asset backup identifier is unavailable")
                # Detect a concurrent replacement between the guarded read and
                # backup.  The project write lock normally prevents this, while
                # this check also protects direct/local service callers.
                _, after_digest = self._read_png(target)
                if after_digest != file_digest:
                    raise AssetError("stale_file", "asset changed while it was being backed up")
                detached = self.publisher.detach_for_delete(
                    relative, backup, operation_id=op, expected_sha256=file_digest,
                    connector_id=self.connector_id, tool="remove_asset",
                )
                mutation_started = True

            result = {
                "status": "success", "project": self.project_name, "filepath": relative,
                "file_deleted": file_present, "catalog_removed": catalog_present,
                "ocr_removed": ocr_present, "deleted_size": len(file_data) if file_data is not None else None,
                "deleted_sha256": file_digest, "backup_id": backup_id,
                "idempotent_replay": False,
            }

            if self.repository is not None and hasattr(self.repository, "commit_asset_removal"):
                # The SQL adapter deletes catalog + OCR rows and records the
                # receipt in one transaction.  A failure leaves derived state
                # untouched, allowing the file backup to compensate the unlink.
                await self._repository_call(
                    self.repository.commit_asset_removal, relative, op, result,
                    connector_id=self.connector_id, tool="remove_asset",
                )
            elif self.repository is not None and hasattr(self.repository, "delete_source"):
                await self._repository_call(self.repository.delete_source, relative)
                await self._finish(op, result, tool="remove_asset")
            elif self.repository is not None and hasattr(self.repository, "detach_source"):
                await self._repository_call(self.repository.detach_source, relative)
                await self._finish(op, result, tool="remove_asset")
            else:
                self._memory.pop(relative, None)
                await self._finish(op, result, tool="remove_asset")
            self._memory.pop(relative, None)
            if detached is not None:
                try:
                    self.publisher.commit_delete(detached)
                except AssetError:
                    # The public path and derived state are already committed.
                    # Keep the journal for startup recovery rather than claiming
                    # the user-visible deletion failed or restoring stale state.
                    self.recovery_blocked = True
                    self._log(
                        "asset.remove.cleanup_pending", project=self.project_name,
                        filepath=relative, operation_id=op, backup_id=backup_id,
                    )
            if file_present:
                try:
                    target.parent.rmdir()
                except OSError:
                    pass
            self._log(
                "asset.remove.committed", project=self.project_name, filepath=relative,
                operation_id=op, file_deleted=file_present,
                catalog_removed=catalog_present, ocr_removed=ocr_present,
                backup_id=backup_id,
            )
            return result
        except AssetError as exc:
            if mutation_started and backup is not None:
                try:
                    if detached is not None:
                        self.publisher.rollback_delete(detached)
                    else:
                        self.publisher.restore_backup_file(backup, target)
                    if not await self._asset_state_matches(relative, catalog_present, ocr_present):
                        raise AssetError("recovery_required", "asset deletion compensation could not prove prior state")
                except Exception as compensation_error:
                    details = {"operation_outcome": "unknown", "backup_id": backup_id}
                    await self._fail_claim(
                        op, "recovery_required",
                        "asset deletion outcome is unknown; use the backup to recover",
                        tool="remove_asset",
                    )
                    raise AssetError(
                        "recovery_required", "asset deletion outcome is unknown; use the backup to recover",
                        details=details,
                    ) from compensation_error
                await self._fail_claim(
                    op, "internal_error",
                    "asset deletion was rolled back after derived-state failure",
                    tool="remove_asset",
                )
                raise AssetError(
                    "internal_error", "asset deletion was rolled back after derived-state failure",
                    details={"operation_outcome": "rolled_back", "backup_id": backup_id},
                ) from exc
            if claimed:
                await self._fail_claim(op, exc.reason, exc.message, tool="remove_asset")
            raise
        except Exception as exc:
            if mutation_started and backup is not None:
                try:
                    if detached is not None:
                        self.publisher.rollback_delete(detached)
                    else:
                        self.publisher.restore_backup_file(backup, target)
                    if not await self._asset_state_matches(relative, catalog_present, ocr_present):
                        raise RuntimeError("asset state could not be verified after compensation")
                except Exception as compensation_error:
                    await self._fail_claim(
                        op, "recovery_required",
                        "asset deletion outcome is unknown; use the backup to recover",
                        tool="remove_asset",
                    )
                    raise AssetError(
                        "recovery_required", "asset deletion outcome is unknown; use the backup to recover",
                        details={"operation_outcome": "unknown", "backup_id": backup_id},
                    ) from compensation_error
                await self._fail_claim(
                    op, "internal_error",
                    "asset deletion was rolled back after derived-state failure",
                    tool="remove_asset",
                )
                raise AssetError(
                    "internal_error", "asset deletion was rolled back after derived-state failure",
                    details={"operation_outcome": "rolled_back", "backup_id": backup_id},
                ) from exc
            await self._fail_claim(op, "internal_error", "asset deletion failed", tool="remove_asset")
            raise AssetError("internal_error", "asset deletion failed") from exc

    async def _asset_state_matches(self, relative: str, catalog_present: bool, ocr_present: bool) -> bool:
        """Verify the derived stores still have their pre-call presence state."""
        if self.repository is None:
            return relative in self._memory if catalog_present else relative not in self._memory
        checked = False
        if hasattr(self.repository, "get"):
            checked = True
            current = await self._repository_call(self.repository.get, relative)
            if (current is not None) != catalog_present:
                return False
        if hasattr(self.repository, "get_ocr_source"):
            checked = True
            current_ocr = await self._repository_call(self.repository.get_ocr_source, relative)
            if (current_ocr is not None) != ocr_present:
                return False
        return checked

    async def recover(self) -> None:
        self._log("asset.recovery.begin", project=self.project_name)
        try:
            for payload in self.publisher.pending():
                committed = False
                op = payload.get("operation_id")
                journal_connector = payload.get("connector_id")
                journal_tool = payload.get("tool")
                if journal_connector is not None and not isinstance(journal_tool, str):
                    raise AssetError("recovery_required", "asset recovery journal is ambiguous")
                operation_row = None
                if isinstance(op, str) and self.repository is not None and hasattr(self.repository, "get_operation"):
                    operation_row = await self._repository_call(
                        self.repository.get_operation, op, connector_id=journal_connector,
                        tool=journal_tool,
                    )
                    committed = self._field(operation_row, "state") == "committed"
                self.publisher.recover_entry(payload, committed=committed)
                if isinstance(op, str) and not committed:
                    await self._fail_claim(
                        op, "publication_failed",
                        "asset publication did not complete before the server restarted",
                        tool=journal_tool or self._field(operation_row, "tool"),
                        connector_id=journal_connector,
                    )
            if self.repository is not None and hasattr(self.repository, "prune_operations"):
                await self.repository.prune_operations()
            self.recovery_blocked = False
            self._log("asset.recovery.completed", project=self.project_name)
        except Exception as exc:
            self.recovery_blocked = True
            self._log("asset.recovery.blocked", project=self.project_name)
            raise AssetError("recovery_required", "asset recovery could not complete") from exc

    async def _replace(self, record: AssetRecord) -> None:
        await self._commit_record(record)

    async def _get_record(self, relative: str) -> Any:
        if self.repository is not None and hasattr(self.repository, "get"):
            found = await self.repository.get(relative)
            if found is not None:
                return found
        return self._memory.get(relative)

    async def list_assets(self, args: Mapping[str, Any]) -> dict[str, Any]:
        detail = args.get("detail", "full")
        if not isinstance(detail, str) or detail not in {"full", "summary"}:
            raise AssetError("invalid_arguments", "detail must be 'full' or 'summary'")
        prefix = args.get("prefix")
        if prefix is not None:
            bounded_string(prefix, "prefix", 1_024)
        limit = integer(args.get("max_results", 100), "max_results", 1, self.max_list_results)
        after = self._decode_cursor(args["cursor"]) if args.get("cursor") else None
        if self.repository is not None and hasattr(self.repository, "list_assets"):
            rows = await self.repository.list_assets(prefix, limit + 1, after)
        else:
            rows = sorted(self._memory.values(), key=lambda row: (row.filepath.casefold(), row.filepath))
            if prefix:
                rows = [row for row in rows if row.filepath.startswith(prefix)]
            if after:
                rows = [row for row in rows if (row.filepath.casefold(), row.filepath) > after]
            rows = rows[:limit + 1]
        more = len(rows) > limit
        visible = rows[:limit]
        cursor = None
        if more and visible:
            filepath = self._summary(visible[-1])["filepath"]
            cursor = self._encode_cursor((filepath.casefold(), filepath))
        return {"status": "success", "project": self.project_name,
                "assets": [self._project(row, detail) for row in visible],
                "next_cursor": cursor}

    @staticmethod
    def _encode_cursor(value: tuple[str, str]) -> str:
        raw = json.dumps(list(value), ensure_ascii=False, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    @staticmethod
    def _decode_cursor(value: Any) -> tuple[str, str]:
        if not isinstance(value, str) or len(value) > 4_096:
            raise AssetError("invalid_arguments", "cursor is invalid")
        try:
            raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
            parsed = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AssetError("invalid_arguments", "cursor is invalid") from exc
        if (not isinstance(parsed, list) or len(parsed) != 2 or
                not all(isinstance(item, str) and len(item.encode()) <= 1_024 for item in parsed)):
            raise AssetError("invalid_arguments", "cursor is invalid")
        return parsed[0], parsed[1]

    async def search_assets(self, args: Mapping[str, Any]) -> dict[str, Any]:
        detail = args.get("detail", "full")
        if not isinstance(detail, str) or detail not in {"full", "summary"}:
            raise AssetError("invalid_arguments", "detail must be 'full' or 'summary'")
        query = bounded_string(args.get("query"), "query", 4_096, empty=False)
        limit = integer(args.get("max_results", 5), "max_results", 1, self.max_search_results)
        alpha = args.get("hybrid_alpha", 0.3)
        minimum = args.get("min_score", 0)
        for value, name in ((alpha, "hybrid_alpha"), (minimum, "min_score")):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 1:
                raise AssetError("invalid_arguments", f"{name} must be between 0 and 1")
        prefix = args.get("path_prefix")
        if prefix is not None:
            bounded_string(prefix, "path_prefix", 1_024)
        tags = args.get("tags")
        if tags is not None:
            if not isinstance(tags, list) or not 1 <= len(tags) <= 64:
                raise AssetError("invalid_arguments", "tags must be a nonempty array")
            tags = [bounded_string(tag, "tag", 256, empty=False) for tag in tags]
        freshness_warning = False
        if self.repository is not None and hasattr(self.repository, "search_hybrid"):
            allowed_sources = await self._searchable_asset_sources()
            vector = None
            if alpha > 0:
                if self.embedder is None:
                    raise AssetError("index_failed", "asset embedder is unavailable")
                vector = (await asyncio.to_thread(self.embedder.embed, [query]))[0]
            pool_limit = min(limit * 3, 60) if self.reranker is not None else limit
            hits = await self._repository_call(
                self.repository.search_hybrid,
                query, vector, pool_limit, prefix, tags, float(alpha),
                include_sources=allowed_sources,
            )
            if allowed_sources is not None:
                allowed = set(allowed_sources)
                hits = [
                    hit for hit in hits
                    if str(self._field(hit, "source", self._field(hit, "filepath", ""))) in allowed
                ]
            if self.reranker is not None and hits:
                scores = await asyncio.to_thread(
                    self.reranker.rerank, query, [str(hit.get("content", "")) for hit in hits]
                )
                if scores is not None:
                    numeric = [float(score) for score in scores]
                    low, high = min(numeric), max(numeric)
                    span = high - low
                    for hit, score in zip(hits, numeric, strict=False):
                        hit["display_score"] = (score - low) / span if span else 1.0
                    hits.sort(key=lambda hit: hit.get("display_score", 0.0), reverse=True)
            # OCR candidates are derived from an external file snapshot.  The
            # catalog join prevents ordinary stale mappings, while this final
            # bounded read catches an out-of-band replacement between catalog
            # publication and search.  Do not refill indefinitely: callers get
            # fewer results plus a truthful freshness warning.
            fresh_hits: list[Any] = []
            for hit in hits:
                if self._field(hit, "provenance") != "ocr":
                    fresh_hits.append(hit)
                    continue
                source_hash = self._field(hit, "ocr_source_sha256")
                filepath = self._field(hit, "source", self._field(hit, "filepath", ""))
                try:
                    _, target = self._target(filepath)
                    # Search freshness is part of the OCR read contract: use
                    # its authorized, timestamp-preserving descriptor boundary
                    # instead of the ordinary asset read that can advance atime.
                    snapshot, _facts, _image = self.ocr_service._snapshot(target, filepath)
                    current_hash = snapshot["source_sha256"]
                except (AssetError, OSError):
                    freshness_warning = True
                    continue
                if current_hash != source_hash:
                    freshness_warning = True
                    continue
                fresh_hits.append(hit)
            hits = fresh_hits
            # Folder policy can change while OCR validation or reranking runs.
            # Apply a final current-policy check before result limiting/publication.
            allowed_sources = await self._searchable_asset_sources()
            if allowed_sources is not None:
                allowed = set(allowed_sources)
                hits = [
                    hit for hit in hits
                    if str(self._field(hit, "source", self._field(hit, "filepath", ""))) in allowed
                ]
            results = [self._project(hit, detail, include_score=True) for hit in hits[:limit]]
        else:
            q = query.casefold()
            allowed_sources = await self._searchable_asset_sources()
            results = [self._project(row, detail, include_score=True) for row in self._memory.values()
                       if q in search_projection(row.metadata, row.filepath).casefold()
                       and (allowed_sources is None or row.filepath in allowed_sources)][:limit]
        filtered_results = [row for row in results if row.get("score", 1.0) >= minimum]
        response = {"status": "success", "project": self.project_name,
                    "results": filtered_results}
        if not filtered_results:
            # Keep an empty search successful, but make the trustworthy zero
            # explicit for clients that need to distinguish it from a failed
            # or unexecuted request.
            response["reason"] = "no_matches"
        if freshness_warning:
            response["warnings"] = [{"code": "ocr_freshness", "message": "some OCR candidates changed or became unavailable"}]
        return response

    async def get_asset_info(self, args: Mapping[str, Any]) -> dict[str, Any]:
        relative, target = self._target(args.get("filepath"))
        if not target.is_file():
            raise AssetError("not_found", "asset was not found")
        data, digest = self._read_png(target)
        size = len(data)
        return await self._info(relative, size, digest, data)

    async def _info(self, relative: str, size: int, digest: str, data: bytes) -> dict[str, Any]:
        facts, _ = self._scan(data)
        row = await self._get_record(relative)
        result = self._summary(row) if row is not None else {"filepath": relative}
        result.update({
            "status": "success", "project": self.project_name, "size": size,
            "final_sha256": digest, "width": facts.width, "height": facts.height,
            "embedded_metadata_present": facts.cognita_chunks > 0,
            "provenance_state": facts.provenance_state,
            "cabx_chunk_count": facts.cabx_chunk_count,
            "catalog_drift": row is None or self._field(row, "final_sha256", digest) != digest,
            "metadata": self._json_mapping(
                self._field(row, "metadata"), "get_asset_info.metadata",
                facts.embedded_metadata or {},
            ),
        })
        # Search-only ranking fields belong to list/search projections. Reads
        # of one asset expose canonical facts and metadata only.
        result.pop("score", None)
        result.pop("search_method", None)
        # ``_summary`` intentionally keeps the compact list/search schema.  The
        # canonical info response also exposes the catalog's current revision,
        # which is needed to reason about metadata updates and replay safety.
        if row is not None:
            result["metadata_revision"] = max(
                1, int(self._field(row, "metadata_revision", 1))
            )
        return result

    async def get_asset(self, args: Mapping[str, Any]) -> dict[str, Any]:
        relative, target = self._target(args.get("filepath"))
        if not target.is_file():
            raise AssetError("not_found", "asset was not found")
        try:
            if target.stat().st_size > MAX_INLINE_PNG_BYTES:
                raise AssetError(
                    "byte_limit",
                    "PNG exceeds the inline response byte limit; use get_asset_info or OCR locally",
                )
        except OSError as exc:
            raise AssetError("not_found", "asset could not be read") from exc
        data, digest = self._read_png(target)
        size = len(data)
        expected = args.get("expected_sha256")
        if expected is not None and sha256_value(expected, "expected_sha256") != digest:
            raise AssetError("stale_file", "current file hash differs from expected hash")
        info = await self._info(relative, size, digest, data)
        return image_result({key: value for key, value in info.items() if key != "metadata"}, data)

    async def reindex_assets(self, args: Mapping[str, Any]) -> dict[str, Any]:
        if self.recovery_blocked:
            raise AssetError("recovery_required", "asset recovery must complete before mutation")
        op = operation_id(args.get("operation_id"))
        prefix = args.get("prefix", "") or ""
        bounded_string(prefix, "prefix", 1_024)
        fingerprint = hashlib.sha256(json.dumps({"prefix": prefix}, separators=(",", ":")).encode()).hexdigest()
        state, replay = await self._claim("reindex_assets", op, fingerprint)
        self._operation_outcome("reindex_assets", op, state)
        if state == "replay":
            return self._replay_result(replay, "reindex_assets")
        if state == "failed":
            self._replay_failure(replay, "reindex_assets")
        if state == "conflict":
            raise AssetError("operation_conflict", "operation_id was already used")
        if state == "busy":
            raise AssetError("busy", "matching operation is still running")
        try:
            self._verify_documents_root()
            paths = await asyncio.to_thread(self._discover_png_paths)
            result = await self._reconcile(paths, prefix=prefix, remove_missing=True)
            result["idempotent_replay"] = False
            await self._finish(op, result, tool="reindex_assets")
            return result
        except AssetError as exc:
            await self._fail_claim(op, exc.reason, exc.message, tool="reindex_assets")
            raise
        except Exception as exc:
            await self._fail_claim(op, "internal_error", "asset reindex failed", tool="reindex_assets")
            raise AssetError("internal_error", "asset reindex failed") from exc

    def attach_documents_root(self) -> tuple[int, int]:
        """Record and return the root identity captured at watch attachment."""
        identity = self._root_identity()
        self._documents_root_identity = identity
        return identity

    async def reconcile_paths(
        self,
        paths: list[str] | tuple[str, ...] = (),
        dirty_prefixes: list[str] | tuple[str, ...] = (),
        *,
        root_identity: tuple[int, int] | None = None,
        source_is_safe: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        """Reconcile dirty PNG files and directory prefixes from current disk state.

        ``paths`` contains file-level event paths.  ``dirty_prefixes`` contains
        normalized directory paths (without a trailing slash); each prefix is
        expanded to the PNG descendants currently on disk, and catalog rows
        absent from that expansion are removed.  A verified root is required
        before any work so a missing or replaced mount can never empty a catalog.
        The caller owns the project's existing write lock.
        """
        return await self._reconcile_targeted(
            paths, dirty_prefixes, root_identity=root_identity,
            source_is_safe=source_is_safe,
        )

    async def deindex_searchable_paths(self, paths: list[str]) -> int:
        """Drop asset/OCR search projections without deleting catalog/file facts."""
        normalized = sorted({path.replace("\\", "/") for path in paths})
        if not normalized:
            return 0
        for path in normalized:
            if resolve_target(self.documents_dir, path) is None:
                raise AssetError("invalid_path", "index exclusion path is outside the project")
            row = self._memory.get(path)
            if row is not None:
                row.indexed = False
        if self.repository is not None and hasattr(self.repository, "deindex_searchable_paths"):
            return int(await self.repository.deindex_searchable_paths(normalized))
        return 0

    async def deindex_searchable_prefix(self, prefix: str) -> int:
        """Retire only derived projections beneath one policy prefix.

        Asset rows, IDs, metadata, receipts, and the exact OCR result remain
        catalog authority.  This is deliberately narrower than deleting an
        asset source, which would make a storage exclusion erase the user's
        exact-path catalog access.
        """
        normalized = prefix.strip("/")
        if self.repository is not None and hasattr(self.repository, "searchable_source_paths"):
            sources = await self.repository.searchable_source_paths()
        else:
            sources = list(self._memory)
        selected = [
            source for source in sources
            if not normalized or source == normalized or source.startswith(normalized + "/")
        ]
        return await self.deindex_searchable_paths(selected)

    def apply_directory_source_rebase(self, old_prefix: str, new_prefix: str) -> None:
        """Keep this service's non-authoritative cache aligned with a SQL rebase."""
        old_prefix = old_prefix.strip("/")
        new_prefix = new_prefix.strip("/")
        marker = old_prefix + "/"
        moved = {
            source: new_prefix + source[len(old_prefix):]
            for source in self._memory
            if source == old_prefix or source.startswith(marker)
        }
        for source, target in moved.items():
            record = self._memory.pop(source)
            record.filepath = target
            self._memory[target] = record

    async def rebase_directory_sources(self, old_prefix: str, new_prefix: str) -> int:
        """Repoint durable catalog paths after an already-authorized directory move."""
        if self.repository is None or not hasattr(self.repository, "rebase_directory_sources"):
            self.apply_directory_source_rebase(old_prefix, new_prefix)
            return 0
        moved = await self.repository.rebase_directory_sources(old_prefix, new_prefix)
        self.apply_directory_source_rebase(old_prefix, new_prefix)
        return int(moved)

    async def reconcile_all(
        self, *, source_is_safe: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        """Rebuild catalog visibility from existing PNGs without touching bytes.

        Discovery runs off the event loop so attaching a project with a large
        tree cannot delay unrelated connector readiness.  Callers provide the
        project write lock, matching watcher and explicit reindex serialization.
        """
        self._verify_documents_root()
        if not self._source_is_safe(source_is_safe):
            return {
                "status": "error", "project": self.project_name,
                "indexed": 0, "removed": 0, "errors": [{"reason": "source_unavailable"}],
            }
        paths = await asyncio.to_thread(self._discover_png_paths)
        return await self._reconcile(
            paths, remove_missing=True, source_is_safe=source_is_safe,
        )

    @staticmethod
    def _source_is_safe(source_is_safe: Callable[[], bool] | None) -> bool:
        if source_is_safe is None:
            return True
        try:
            return bool(source_is_safe())
        except Exception:
            return False

    def _discover_png_paths(self) -> list[str]:
        return self._discover_png_paths_under("")

    def _discover_png_paths_under(self, prefix: str) -> list[str]:
        """Walk one verified lexical subtree without following directory links."""
        root = self.documents_dir
        start = root if not prefix else root.joinpath(*prefix.split("/"))
        try:
            start_facts = start.stat()
        except FileNotFoundError:
            return []
        # Other access failures must propagate. Treating an unreadable prefix
        # as empty would make the catalog sweep destructive.
        if not stat_module.S_ISDIR(start_facts.st_mode):
            return []
        found: list[str] = []

        def onerror(error: OSError) -> None:
            raise error

        for directory, _dirs, files in os.walk(start, followlinks=False, onerror=onerror):
            base = Path(directory)
            for name in files:
                if not name.lower().endswith(".png"):
                    continue
                path = base / name
                try:
                    facts = path.stat()
                except FileNotFoundError:
                    continue
                # Any other access failure must abort this discovery pass. An
                # incomplete prefix snapshot is not proof that catalog rows
                # beneath the inaccessible path should be retired.
                if stat_module.S_ISREG(facts.st_mode) and not self._excluded(path):
                    found.append(path.relative_to(root).as_posix())
        return found

    @staticmethod
    def _read_root_identity(path: Path | None = None, *, allow_missing: bool = False) -> tuple[int, int] | None:
        root = path
        if root is None:
            # This method is only called with an instance path through the
            # wrapper below; retaining the optional argument keeps the facts
            # operation easy to test without touching file contents.
            raise TypeError("root path is required")
        try:
            facts = root.stat()
        except OSError as exc:
            if allow_missing:
                return None
            raise AssetError("root_unavailable", "documents root could not be read") from exc
        if not root.is_dir() or not os.access(root, os.R_OK):
            if allow_missing:
                return None
            raise AssetError("root_unavailable", "documents root is not a readable directory")
        return int(facts.st_dev), int(facts.st_ino)

    def _root_identity(self, *, allow_missing: bool = False) -> tuple[int, int] | None:
        return self._read_root_identity(self.documents_dir, allow_missing=allow_missing)

    def _verify_documents_root(self, expected: tuple[int, int] | None = None) -> tuple[int, int]:
        current = self._root_identity()
        baseline = expected if expected is not None else self._documents_root_identity
        if baseline is None:
            self._documents_root_identity = current
            return current
        if tuple(baseline) != current:
            raise AssetError("root_changed", "documents root identity changed; reconciliation is unsafe")
        return current

    def _refuse_empty_root_sweep(self, scope: str, catalog_rows: int) -> None:
        """Refuse a removal sweep that would empty the catalog because the root is empty.

        Same trade as retrieval's empty-walk guard: a stale row costs one wrong
        listing, a wrong sweep costs the user's catalog metadata (titles, tags
        and descriptions of ``catalog`` rows live only in the database).  An
        unmounted projects folder reads as an EMPTY directory, not a missing one
        (installer design 22.14: the Windows mount point inside the shared base
        bind is a plain root-owned 0755 directory), so the root-identity check
        passes and discovery finds nothing.  Refused only when the whole
        documents root has no entries at all; a real folder whose PNGs were
        deleted still has its other files and is swept as before.
        """
        if catalog_rows <= 0:
            return
        try:
            with os.scandir(self.documents_dir) as entries:
                empty = next(entries, None) is None
        except OSError as exc:
            raise AssetError("root_unavailable", "documents root could not be read") from exc
        if not empty:
            return
        self._log("asset.reconcile", outcome="refused_empty_root", scope=scope or "/",
                  catalog_rows=catalog_rows, removed=0)
        raise AssetError(
            "root_empty",
            f"the documents folder is empty or not mounted; {catalog_rows} catalog "
            "row(s) were kept. When the files are back, restart Cognita or reindex the assets.",
        )

    @staticmethod
    def _source_in_prefix(source: str, prefix: str) -> bool:
        return not prefix or source == prefix or source.startswith(prefix + "/")

    def _normalize_reconcile_path(self, value: Any, *, prefix: bool = False) -> str:
        if not isinstance(value, str) or "\0" in value:
            raise AssetError("invalid_path", "reconciliation path is invalid")
        relative = value.replace("\\", "/")
        if relative in {"", "."}:
            return ""
        parts = relative.split("/")
        if any(part in {"", ".", ".."} for part in parts):
            raise AssetError("invalid_path", "reconciliation path is not project-relative")
        if parts[0].casefold() == BACKUPS_DIRNAME.casefold():
            raise AssetError("invalid_path", "the backup tree is not reconciled")
        if any(part.startswith(".cognita-asset-") for part in parts):
            raise AssetError("invalid_path", "asset staging paths are not reconciled")
        if not prefix and not relative.lower().endswith(".png"):
            raise AssetError("unsupported_media_type", "only .png paths are reconciled")
        if prefix:
            lexical = self.documents_dir
            for part in parts:
                if lexical.is_symlink():
                    raise AssetError("invalid_path", "reconciliation prefixes may not traverse links")
                lexical /= part
            if lexical.is_symlink():
                raise AssetError("invalid_path", "reconciliation prefixes may not be links")
        return "/".join(parts)

    async def _list_catalog_sources(self, prefix: str) -> list[str]:
        if self.repository is not None and hasattr(self.repository, "list_sources"):
            rows = await self.repository.list_sources(prefix + "/" if prefix else "")
            return [str(row) for row in rows if self._source_in_prefix(str(row), prefix)]
        return [
            row.filepath for row in self._memory.values()
            if self._source_in_prefix(row.filepath, prefix)
        ]

    async def _refresh_asset_stat(self, existing: Any, relative: str, stat: os.stat_result) -> None:
        mtime = datetime.fromtimestamp(stat.st_mtime, UTC)
        if self.repository is not None and hasattr(self.repository, "update_asset_file_facts"):
            await self.repository.update_asset_file_facts(
                relative, int(stat.st_size), mtime,
            )
        row = self._memory.get(relative)
        if row is not None:
            row.final_size = int(stat.st_size)
            row.file_mtime = mtime
        # Repository-backed services may not have loaded this row in memory.
        # No replacement is needed: update_asset_file_facts is a catalog-only
        # metadata refresh and deliberately leaves vectors/OCR state intact.

    @staticmethod
    def _asset_stat_matches(existing: Any, stat: os.stat_result) -> bool:
        if existing is None:
            return False
        stored_size = AssetService._field(
            existing, "final_size",
            AssetService._field(existing, "file_size"),
        )
        stored_mtime = AssetService._field(existing, "file_mtime")
        if stored_size is None or stored_mtime is None:
            return False
        try:
            stored_ns = int(float(stored_mtime.timestamp()) * 1_000_000_000)
        except (AttributeError, TypeError, ValueError, OverflowError):
            return False
        return int(stored_size) == int(stat.st_size) and abs(stored_ns - stat.st_mtime_ns) <= 1_000

    async def _reconcile_targeted(
        self,
        paths: list[str] | tuple[str, ...],
        dirty_prefixes: list[str] | tuple[str, ...],
        *,
        root_identity: tuple[int, int] | None,
        source_is_safe: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        self._verify_documents_root(root_identity)
        if not self._source_is_safe(source_is_safe):
            return {
                "status": "error", "project": self.project_name,
                "indexed": 0, "metadata_refreshed": 0, "skipped": 0,
                "removed": 0, "failed": 1,
                "errors": [{"reason": "source_unavailable"}],
                "retryable_failures": [],
            }
        normalized_files = {
            self._normalize_reconcile_path(path)
            for path in paths
        }
        normalized_prefixes = {
            self._normalize_reconcile_path(prefix, prefix=True)
            for prefix in dirty_prefixes
        }
        # A directory event subsumes file events below it.  Prefixes are
        # lexical and boundary-aware, so ``art`` does not subsume ``artist``.
        collapsed_prefixes: list[str] = []
        for prefix in sorted(normalized_prefixes, key=lambda item: (item.count("/"), item)):
            if not any(self._source_in_prefix(prefix, ancestor) for ancestor in collapsed_prefixes):
                collapsed_prefixes.append(prefix)

        candidates: set[str] = set()
        for prefix in collapsed_prefixes:
            discovered = await asyncio.to_thread(self._discover_png_paths_under, prefix)
            candidates.update(discovered)
        for relative in normalized_files:
            if not any(self._source_in_prefix(relative, prefix) for prefix in collapsed_prefixes):
                candidates.add(relative)

        indexed = skipped = metadata_refreshed = removed = 0
        errors: list[dict[str, str]] = []
        for relative in sorted(candidates):
            if not self._source_is_safe(source_is_safe):
                errors.append({"filepath": relative, "reason": "source_unavailable"})
                break
            try:
                _, target = self._target(relative)
                try:
                    file_stat = target.stat()
                except FileNotFoundError:
                    file_stat = None
                except OSError:
                    errors.append({"filepath": relative, "reason": "read_failed"})
                    continue
                if file_stat is None or not stat_module.S_ISREG(file_stat.st_mode):
                    self._verify_documents_root(root_identity)
                    if not self._source_is_safe(source_is_safe):
                        errors.append({"filepath": relative, "reason": "source_unavailable"})
                        break
                    await self._delete(relative)
                    removed += 1
                    continue
                existing = await self._get_record(relative)
                if self._asset_stat_matches(existing, file_stat):
                    skipped += 1
                    continue
                raw, digest = self._read_png(target)
                after_read = target.stat()
                if (
                    int(after_read.st_dev), int(after_read.st_ino),
                    int(after_read.st_size), int(after_read.st_mtime_ns),
                ) != (
                    int(file_stat.st_dev), int(file_stat.st_ino),
                    int(file_stat.st_size), int(file_stat.st_mtime_ns),
                ):
                    raise AssetError(
                        "stale_file", "asset changed before reconciliation could publish it"
                    )
                if existing is not None and self._field(existing, "final_sha256") == digest:
                    if not self._source_is_safe(source_is_safe):
                        errors.append({"filepath": relative, "reason": "source_unavailable"})
                        break
                    await self._refresh_asset_stat(existing, relative, after_read)
                    metadata_refreshed += 1
                    continue
                facts, _ = self._scan(raw)
                metadata, storage = self._metadata_for_reconcile(existing, facts, digest)
                record = AssetRecord(
                    str(self._field(existing, "asset_id", metadata["asset_id"])), relative, metadata,
                    int(self._field(existing, "received_size", len(raw))),
                    str(self._field(existing, "received_sha256", digest)), len(raw), digest,
                    facts.width, facts.height, storage,
                    max(1, int(self._field(existing, "metadata_revision", 1))), False,
                    facts.provenance_state, facts.cabx_chunk_count, facts.cognita_chunks > 0,
                )
                record.file_mtime = datetime.fromtimestamp(after_read.st_mtime, UTC)
                if not self._source_is_safe(source_is_safe):
                    errors.append({"filepath": relative, "reason": "source_unavailable"})
                    break
                await self._commit_record(record)
                indexed += 1
            except AssetError as exc:
                # Keep the established reconcile policy: a present but invalid
                # PNG is removed from the catalog. A changing or temporarily
                # unreadable source and an embedding failure preserve the prior
                # committed row and remain pending for a later stable pass.
                retire = exc.reason in {
                    "invalid_png", "animated_png", "byte_limit", "dimension_limit",
                }
                retry_reason = exc.reason
                if exc.reason == "not_found":
                    try:
                        target.stat()
                    except FileNotFoundError:
                        retire = True
                    except OSError:
                        retry_reason = "read_failed"
                    else:
                        retry_reason = "read_failed"
                if retire:
                    try:
                        self._verify_documents_root(root_identity)
                        if not self._source_is_safe(source_is_safe):
                            errors.append({"filepath": relative, "reason": "source_unavailable"})
                            break
                        await self._delete(relative)
                        removed += 1
                    except AssetError:
                        raise
                errors.append({"filepath": relative, "reason": retry_reason})
            except OSError:
                errors.append({"filepath": relative, "reason": "read_failed"})

        for prefix in collapsed_prefixes:
            if not self._source_is_safe(source_is_safe):
                errors.append({"filepath": prefix, "reason": "source_unavailable"})
                break
            self._verify_documents_root(root_identity)
            current = {
                relative for relative in await asyncio.to_thread(
                    self._discover_png_paths_under, prefix
                )
            }
            catalog = await self._list_catalog_sources(prefix)
            if not current:
                self._refuse_empty_root_sweep(prefix, len(catalog))
            for relative in catalog:
                if relative not in current:
                    self._verify_documents_root(root_identity)
                    if not self._source_is_safe(source_is_safe):
                        errors.append({"filepath": relative, "reason": "source_unavailable"})
                        break
                    await self._delete(relative)
                    removed += 1

        result = {
            "status": "success", "project": self.project_name,
            "indexed": indexed, "metadata_refreshed": metadata_refreshed,
            "skipped": skipped, "removed": removed, "errors": errors[:20],
            # WatcherManager retries only transient read failures.  Invalid
            # PNGs and policy-rejected files have already been retired above
            # and therefore are intentionally not retryable.
            "retryable_failures": [
                item["filepath"] for item in errors
                if item.get("reason") in {"read_failed", "stale_file", "index_failed"}
            ],
            "expanded_prefixes": len(collapsed_prefixes),
        }
        result["failed"] = len(result["retryable_failures"])
        self._log(
            "asset.reconcile", project=self.project_name, indexed=indexed,
            metadata_refreshed=metadata_refreshed, skipped=skipped,
            removed=removed, expanded_prefixes=len(collapsed_prefixes),
            error_count=len(errors),
            error_reasons=sorted({item["reason"] for item in errors}),
            outcome="complete",
        )
        return result

    def _metadata_for_reconcile(
        self, existing: Any, facts: Any, digest: str,
    ) -> tuple[dict[str, Any], str]:
        if facts.embedded_metadata is not None:
            metadata = self._prepare_metadata(
                None, asset_id=self._field(existing, "asset_id", str(uuid4())),
                received_sha256=self._field(existing, "received_sha256", digest),
                native=facts.embedded_metadata,
            )
            return metadata, "embedded"
        if existing is not None and self._field(existing, "final_sha256") == digest:
            metadata = self._json_mapping(
                self._field(existing, "metadata"), "reconcile_paths.metadata"
            )
            if metadata is None:
                metadata = self._prepare_metadata(
                    None, asset_id=self._field(existing, "asset_id", str(uuid4())),
                    received_sha256=self._field(existing, "received_sha256", digest),
                )
            return metadata, self._field(existing, "metadata_storage", "catalog")
        metadata = self._prepare_metadata(
            None, asset_id=self._field(existing, "asset_id", str(uuid4())),
            received_sha256=digest,
        )
        return metadata, ("catalog" if facts.cabx_chunk_count else "embedded")

    async def _reconcile(
        self,
        paths: list[str],
        *,
        prefix: str = "",
        remove_missing: bool = False,
        source_is_safe: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        if remove_missing:
            self._verify_documents_root()
        indexed = removed = 0
        errors: list[dict[str, str]] = []
        seen: set[str] = set()
        for relative in paths:
            if not self._source_is_safe(source_is_safe):
                errors.append({"filepath": relative, "reason": "source_unavailable"})
                break
            if prefix and not relative.startswith(prefix):
                continue
            seen.add(relative)
            target: Path | None = None
            try:
                _, target = self._target(relative)
                if not target.is_file():
                    if remove_missing:
                        self._verify_documents_root()
                    if not self._source_is_safe(source_is_safe):
                        errors.append({"filepath": relative, "reason": "source_unavailable"})
                        break
                    await self._delete(relative)
                    removed += 1
                    continue
                raw, digest = self._read_png(target)
                size = len(raw)
                facts, _ = self._scan(raw)
                existing = await self._get_record(relative)
                if facts.embedded_metadata is not None:
                    metadata = self._prepare_metadata(
                        None, asset_id=self._field(existing, "asset_id", str(uuid4())),
                        received_sha256=self._field(existing, "received_sha256", digest),
                        native=facts.embedded_metadata)
                    storage = "embedded"
                elif existing is not None and self._field(existing, "final_sha256") == digest:
                    metadata = self._json_mapping(
                        self._field(existing, "metadata"), "reindex_assets.metadata"
                    )
                    if metadata is None:
                        metadata = self._prepare_metadata(
                            None,
                            asset_id=self._field(existing, "asset_id", str(uuid4())),
                            received_sha256=self._field(existing, "received_sha256", digest),
                        )
                    storage = self._field(existing, "metadata_storage", "catalog")
                else:
                    metadata = self._prepare_metadata(
                        None, asset_id=self._field(existing, "asset_id", str(uuid4())),
                        received_sha256=digest)
                    storage = "catalog" if facts.cabx_chunk_count else "embedded"
                record = AssetRecord(
                    str(self._field(existing, "asset_id", metadata["asset_id"])), relative, metadata,
                    int(self._field(existing, "received_size", size)),
                    str(self._field(existing, "received_sha256", digest)), size, digest,
                    facts.width, facts.height, storage,
                    max(1, int(self._field(existing, "metadata_revision", 1))), False,
                    facts.provenance_state, facts.cabx_chunk_count, facts.cognita_chunks > 0)
                if not self._source_is_safe(source_is_safe):
                    errors.append({"filepath": relative, "reason": "source_unavailable"})
                    break
                await self._commit_record(record)
                indexed += 1
            except AssetError as exc:
                # The same retire rule as _reconcile_targeted (installer design 22.14 item 6): only a
                # file that is present but not an acceptable PNG, or one a fresh stat confirms is
                # gone, loses its row. A read that failed (_read_png maps every OSError to
                # not_found), a file that changed mid-read (stale_file) or an embedder failure keeps
                # the committed row: a catalog row's title, tags and description live only in the
                # database, and the next pass retries the file.
                retire = exc.reason in {
                    "invalid_png", "animated_png", "byte_limit", "dimension_limit",
                }
                reason = exc.reason
                if exc.reason == "not_found" and target is not None:
                    try:
                        target.stat()
                    except FileNotFoundError:
                        retire = True
                    except OSError:
                        reason = "read_failed"
                    else:
                        reason = "read_failed"
                if retire:
                    if remove_missing:
                        self._verify_documents_root()
                    if not self._source_is_safe(source_is_safe):
                        errors.append({"filepath": relative, "reason": "source_unavailable"})
                        break
                    await self._delete(relative)
                    removed += 1
                else:
                    self._log("asset.reconcile", outcome="kept_after_error", filepath=relative,
                              reason=reason)
                errors.append({"filepath": relative, "reason": reason})
        if remove_missing and self.repository is not None and hasattr(self.repository, "list_sources"):
            self._verify_documents_root()
            catalog = list(await self.repository.list_sources(prefix))
            if not seen:
                self._refuse_empty_root_sweep(prefix, len(catalog))
            for relative in catalog:
                if relative not in seen:
                    self._verify_documents_root()
                    if not self._source_is_safe(source_is_safe):
                        errors.append({"filepath": relative, "reason": "source_unavailable"})
                        break
                    await self._delete(relative)
                    removed += 1
        self._log(
            "asset.reconcile", indexed=indexed, removed=removed,
            error_count=len(errors),
            error_reasons=sorted({item["reason"] for item in errors}),
            outcome="complete",
        )
        return {"status": "success", "project": self.project_name, "indexed": indexed,
                "removed": removed, "errors": errors[:20]}

    async def _delete(self, relative: str) -> None:
        self._memory.pop(relative, None)
        if self.repository is not None and hasattr(self.repository, "delete_source"):
            await self.repository.delete_source(relative)

    def _excluded(self, path: Path) -> bool:
        rel = path.relative_to(self.documents_dir)
        return (any(part.casefold() == BACKUPS_DIRNAME.casefold() for part in rel.parts)
                or path.name.startswith(".cognita-asset-"))

    @staticmethod
    def _field(row: Any, key: str, default: Any = None) -> Any:
        if row is None:
            return default
        if isinstance(row, Mapping):
            return row.get(key, default)
        # asyncpg.Record exposes mapping-style keyed access without registering
        # as collections.abc.Mapping. Try that boundary before attribute access
        # so catalog rows retain their actual identity and metadata fields.
        try:
            return row[key]
        except (KeyError, IndexError, TypeError):
            return getattr(row, key, default)

    def _json_mapping(
        self, value: Any, field: str, default: Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any] | None:
        """Decode catalog JSON objects without exposing stored values in logs."""
        if value is None:
            return default
        if isinstance(value, Mapping):
            return value
        if isinstance(value, str):
            try:
                decoded = json.loads(value)
            except (TypeError, ValueError, json.JSONDecodeError):
                self._log("asset.catalog.malformed_json", field=field, value_type="string")
                return default
            if isinstance(decoded, Mapping):
                return decoded
            self._log(
                "asset.catalog.malformed_json", field=field,
                value_type=type(decoded).__name__,
            )
            return default
        self._log(
            "asset.catalog.malformed_json", field=field,
            value_type=type(value).__name__,
        )
        return default

    def _replay_result(self, value: Any, tool: str) -> dict[str, Any]:
        replay = self._json_mapping(value, f"{tool}.replay")
        if replay is None:
            raise AssetError("internal_error", "stored asset operation result is invalid")
        return {**replay, "idempotent_replay": True}

    def _replay_failure(self, value: Any, tool: str) -> NoReturn:
        """Replay the stored failure for an identical operation and fingerprint."""
        stored = self._json_mapping(value, f"{tool}.replay")
        if stored is None or not isinstance(stored.get("reason"), str):
            raise AssetError("internal_error", "stored asset operation failure is invalid")
        message = stored.get("message")
        raise AssetError(
            stored["reason"],
            message if isinstance(message, str) and message else "asset operation failed",
            details={"idempotent_replay": True},
        )

    def _summary(self, row: Any) -> dict[str, Any]:
        if isinstance(row, AssetRecord):
            return {"filepath": row.filepath, "asset_id": row.asset_id, "title": row.metadata.get("title", ""), "description": row.metadata.get("description", ""), "alt_text": row.metadata.get("alt_text", ""), "tags": row.metadata.get("tags", []), "width": row.width, "height": row.height, "final_sha256": row.final_sha256, "metadata_storage": row.metadata_storage, "provenance_state": row.provenance_state, "score": 0.0, "search_method": "lexical"}
        if hasattr(row, "__dataclass_fields__"):
            row = asdict(row)
        meta = self._json_mapping(self._field(row, "metadata"), "summary.metadata", {}) or {}
        result = {"filepath": self._field(row, "source", self._field(row, "filepath", "")), "asset_id": str(self._field(row, "asset_id", "")), "title": meta.get("title", ""), "description": meta.get("description", ""), "alt_text": meta.get("alt_text", ""), "tags": meta.get("tags", []), "width": self._field(row, "width", 0), "height": self._field(row, "height", 0), "final_sha256": self._field(row, "final_sha256", ""), "metadata_storage": self._field(row, "metadata_storage", ""), "provenance_state": self._field(row, "provenance_state", "none"), "score": float(self._field(row, "display_score", self._field(row, "score", 1.0))), "search_method": self._field(row, "search_method", "keyword")}
        # OCR provenance is additive and only appears for an OCR-backed hit;
        # metadata-only results retain the established response shape.
        provenance = self._field(row, "provenance")
        if provenance == "ocr":
            result["provenance"] = "ocr"
            result["source_sha256"] = self._field(row, "ocr_source_sha256", "")
        return result

    def _project(self, row: Any, detail: str, *, include_score: bool = False) -> dict[str, Any]:
        """Return the requested collection projection without changing order/cursors."""
        full = self._summary(row)
        if detail == "full":
            if not include_score:
                # 13.0.2: a listing is not a search. ``_summary`` is shared
                # with search_assets and always carries a placeholder score
                # and search_method; list_assets used to leak both (score 1.0,
                # "keyword") on every entry, which the plan's rule for
                # non-search tools forbids.
                full.pop("score", None)
                full.pop("search_method", None)
            return full
        compact = {
            "filepath": full.get("filepath", ""),
            "asset_id": full.get("asset_id", ""),
            "title": full.get("title", ""),
            "width": full.get("width", 0),
            "height": full.get("height", 0),
            "mime_type": "image/png",
            "final_size": self._field(row, "final_size", self._field(row, "size_bytes", 0)),
            "final_sha256": full.get("final_sha256", ""),
        }
        if include_score:
            compact["score"] = full.get("score", 0.0)
            compact["search_method"] = full.get("search_method", "keyword")
            if full.get("provenance") == "ocr":
                compact["provenance"] = "ocr"
                compact["source_sha256"] = full.get("source_sha256", "")
        return compact

    def _result(self, record: AssetRecord) -> dict[str, Any]:
        return {"status": "success", "project": self.project_name, "filepath": record.filepath, "asset_id": record.asset_id, "received_size": record.received_size, "received_sha256": record.received_sha256, "final_size": record.final_size, "final_sha256": record.final_sha256, "width": record.width, "height": record.height, "metadata_storage": record.metadata_storage, "metadata_revision": record.metadata_revision, "schema_version": 1, "provenance_state": record.provenance_state, "cabx_chunk_count": record.cabx_chunk_count, "embedded_metadata_present": record.embedded_metadata_present, "previous_backup_id": record.previous_backup_id, "received_backup_id": record.received_backup_id, "indexed": record.indexed, "idempotent_replay": False}
