"""The 4.0 in-process engine — the 13 knowledge tools served by the retrieval
core over Postgres, speaking the same minimal MCP the workers spoke (4.0-M3,
DESIGN-4.0-vector-engine.md D4.3/D4.4/D4.6).

Architecture trick: rather than teaching the gateway a second dispatch path,
this host exposes an ASGI app at /engine/{project}/mcp and the gateway talks
to it through httpx's ASGITransport — in-process function calls wearing the
same HTTP clothes as a 3.x worker. proxy.py (read-only policy, backups, edit
tools, write locks, tool stamping) therefore runs BYTE-IDENTICAL logic in both
engine modes; only the URL differs. The supervisor, port pool, and startup
probes have nothing left to supervise.

Tool names, argument schemas, and result shapes are copied from the retired
engine (mcp_server/server.py) so every claude.ai connector keeps working with
zero re-registration (D4.4) — including quirks like "bm25_rank" (now FTS rank)
and cache_hit_rate (no query cache in 4.0; always 0.0).

Protocol notes: stateless streamable-http. POST initialize/tools/list/
tools/call are answered as plain JSON (spec-allowed; 3.x gateway already
answered many calls this way in production); notifications get 202; the GET
SSE channel and DELETE session teardown answer 405 (no sessions to manage).
"""

from __future__ import annotations

# Explicit re-exports preserve imports from engine_local during this move-only
# release. Operation methods resolve globals in their owning modules.
import asyncio
import base64 as base64
import copy
import hashlib as hashlib
import inspect as inspect
import ipaddress as ipaddress
import json
import logging as logging
import os as os
import re as re
import shutil as shutil
import socket as socket
import time as time
import uuid as uuid
from dataclasses import dataclass as dataclass
from datetime import datetime as datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse as urlparse

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

from . import __version__
from .assets.models import AssetError
from .assets.ocr_service import OCRService, SchedulerOCRCapacityGate
from .assets.repository import AssetRepository
from .assets.service import AssetService
from .assets.wire import ASSET_MUTATING_TOOLS, ASSET_TOOL_DEFS as ASSET_TOOL_DEFS, ASSET_TOOL_NAMES
from .result_contracts import attach_output_schema as attach_output_schema, build_tool_result
from .backups import (
    BACKUPS_DIRNAME as BACKUPS_DIRNAME,
    BackupError as BackupError,
    backup_id_of as backup_id_of,
    backup_if_exists as backup_if_exists,
    resolve_target as resolve_target,
)
from .byte_facts import (
    MAX_BASE64_ATOMIC_SET_BYTES as MAX_BASE64_ATOMIC_SET_BYTES,
    byte_facts as byte_facts,
    check_expected_bytes_sha256 as check_expected_bytes_sha256,
    check_expected_bytes_sha256_digest as check_expected_bytes_sha256_digest,
    classify_text_bytes as classify_text_bytes,
    decode_base64 as decode_base64,
    validate_expected_bytes_sha256 as validate_expected_bytes_sha256,
)
from .config import CognitaConfig
from .connectors import ConnectorPolicyError, ConnectorStore
from .deindexed import FILENAME as DEINDEXED_FILENAME
from .deindexed import DeindexedPaths
from .document_roots import display_for as display_for
from .editing import (
    EXPECTED_SHA_PROPERTY as EXPECTED_SHA_PROPERTY,
    MAX_FILE_BYTES as MAX_FILE_BYTES,
    SHA_PREFIX_MIN as SHA_PREFIX_MIN,
    sha_matches as sha_matches,
)
from .literals import (
    FIND_LITERAL_TOOL_DEF as FIND_LITERAL_TOOL_DEF,
    MAX_CONTEXT_LINES as MAX_CONTEXT_LINES,
    MAX_MATCHES_CEILING as MAX_MATCHES_CEILING,
    MAX_MATCHES_DEFAULT as MAX_MATCHES_DEFAULT,
    SKIPPED_LISTED as SKIPPED_LISTED,
    BadPattern as BadPattern,
    build_matcher as build_matcher,
    glob_matches as glob_matches,
    scan_text as scan_text,
)
from .manifest import (
    TEXT_HASH_MAX_BYTES as TEXT_HASH_MAX_BYTES,
    file_facts as file_facts,
    stat_drift as stat_drift,
    text_sha256 as text_sha256,
)
from .parsing import (
    SYNC_CONFLICT_PATTERNS as SYNC_CONFLICT_PATTERNS,
    TIER_REGISTERED as TIER_REGISTERED,
    ExtensionPolicy,
    detect_category as detect_category,
    is_sync_conflict as is_sync_conflict,
    parse_file as parse_file,
)
from .readonly import MUTATING_TOOLS
from .books import models as book_dto
from .books.configuration import load_book_config
from .books.policy import BookMutationPolicy, EffectiveIndexPolicy
from .books.schemas import (
    ALL_ADDITIVE_MUTATING_TOOLS, ALL_ADDITIVE_TOOL_NAMES,
    BOOK_MUTATING_TOOLS, PROJECT_STORAGE_MUTATING_TOOLS,
    error_envelope as book_error_envelope,
    success_envelope as book_success_envelope,
)
from .books.service import BookService, BookServiceError
from .books.state import ProjectState, ProjectStateError
from .books.storage import ProjectFileError
from .source_mount_guard import SourceMountGuard
from .registry import Project, Registry
from .retrieval import RetrievalCore
from .store import SchemaVersionMismatch
from .toolargs import reject_unknown_arguments, reject_wrong_types, wire_error as wire_error

from .engine_contract import (
    log,
    ENGINE_TOOL_DEFS,
    ENGINE_TOOL_DEFS_BY_NAME,
    LOCAL_ENGINE_BASE,
    MAX_BATCH_DOCUMENTS as MAX_BATCH_DOCUMENTS,
    MAX_CONTENT_BYTES as MAX_CONTENT_BYTES,
    MAX_COPY_FILES as MAX_COPY_FILES,
    MAX_PLURAL_PATHS as MAX_PLURAL_PATHS,
    MAX_RESULTS as MAX_RESULTS,
    PLURAL_BODY_MAX_BYTES as PLURAL_BODY_MAX_BYTES,
    PLURAL_BODY_TOTAL_MAX_BYTES as PLURAL_BODY_TOTAL_MAX_BYTES,
    WRITE_LOCK_WAIT_S,
    LITERAL_WALK_BUDGET_S as LITERAL_WALK_BUDGET_S,
    _MAX_REDIRECT_HOPS as _MAX_REDIRECT_HOPS,
    _ReindexContext as _ReindexContext,
    _UrlRefused as _UrlRefused,
    _collection as _collection,
    _empty_selection_message as _empty_selection_message,
    _verbatim_text as _verbatim_text,
    make_snippet as make_snippet,
    normalize_prefix as normalize_prefix,
)
from .engine_reads import EngineReadOperations
from .engine_documents import EngineDocumentOperations
from .engine_transfers import EngineTransferOperations
from .engine_reindex import EngineReindexOperations


def _with_reason(tool: str, payload: dict) -> dict:
    """Backstop: no error leaves this engine without a machine-readable reason.

    5.0.2. The self-test plan tells clients to branch on `reason` and never on
    message text, because message text is prose and gets reworded. That
    instruction is only honest if the field is always there — and on 2026-08-29
    four error paths (get_document/remove_document not-found, move_document
    same-path and destination-exists) still returned reason=None, leaving a
    runner nothing to assert on but the prose it had just been told to ignore.

    The specific reasons are set at each return site; this catches a new one
    that forgets. "error" is deliberately useless as a discriminator — the log
    line is the actionable part, and tests/test_error_reasons.py fails the build
    before it can ship.
    """
    if isinstance(payload, dict) and payload.get("status") == "error" and not payload.get("reason"):
        log.warning("%s returned an error with no reason field: %r", tool,
                    payload.get("message"))
        payload["reason"] = "error"
    return payload


class LocalEngineHost(
    EngineReadOperations, EngineDocumentOperations,
    EngineTransferOperations, EngineReindexOperations,
):
    """Owns the store + retrieval core + per-project reindex state, and serves
    the engine tool surface as an in-process ASGI app the gateway proxies to."""

    def __init__(
        self,
        config: CognitaConfig,
        registry: Registry,
        core: RetrievalCore,
        connector_store: ConnectorStore | None = None,
    ):
        self.config = config
        self.registry = registry
        self.source_guard = SourceMountGuard()
        self.core = core
        # The retrieval core carries the one scheduler owned by this host.
        # Shutdown it before stores and GPU lifecycle so no native indexing
        # call can outlive the host's database connections.
        self.scheduler = getattr(core, "scheduler", None)
        self.store = core.store
        self.connector_store = (
            connector_store if connector_store is not None
            else ConnectorStore(config.connectors_path)
        )
        self._reindex_progress: dict[str, dict[str, Any]] = {}
        self._reindex_tasks: dict[str, asyncio.Task] = {}
        self._asset_reconcile_tasks: dict[str, asyncio.Task] = {}
        self.watcher = None  # WatcherManager, set by startup() when enabled (M4)
        self._asset_services: dict[tuple[str, str | None], AssetService] = {}
        self._book_services: dict[str, BookService] = {}
        if hasattr(self.core, "set_effective_index_policy_provider"):
            self.core.set_effective_index_policy_provider(
                lambda name: self.effective_index_policy_for(name)
            )
        if hasattr(self.core, "set_book_index_admission_provider"):
            self.core.set_book_index_admission_provider(
                lambda project, sources, retrieval_profile=None:
                    self.book_index_admission_for(project, sources, retrieval_profile)
            )
        if hasattr(self.core, "set_book_index_provenance_recorder"):
            self.core.set_book_index_provenance_recorder(
                lambda project, source_path, doc_id, extracted_sha256,
                       raw_sha256, extraction_version:
                    self.record_book_index_provenance_for(
                        project, source_path, doc_id, extracted_sha256,
                        raw_sha256, extraction_version,
                    )
            )
        # One admission queue per host, shared by every project and connector.
        # AssetService remains project-scoped for authorization and path rules.
        self.ocr_capacity_gate = SchedulerOCRCapacityGate(
            config, self.scheduler, getattr(core, "gpu_probe", None),
        ) if self.scheduler is not None else None
        self.ocr_service = OCRService(
            config, logger=log, capacity_gate=self.ocr_capacity_gate,
        )
        self._probe_task: asyncio.Task | None = None
        self.probe_ok: bool = True  # last probe verdict (exposed for admin/ops)
        self.app = self._build_app()

    # ---------------- lifecycle ----------------

    async def startup(self) -> None:
        """Connect the store and make sure every enabled project has its schema,
        attach the on-disk watcher, then kick a background smart reindex per
        project (boot-time drift catch — the 3.x workers did the same on spawn;
        unchanged files skip on a stat).

        13.0 §4.1: a database whose schema version is not this image's does NOT
        kill the process. The store stays unavailable, /healthz says so, every
        index/search tool and Admin's add-project answer with the reason — and
        the Workspace half of the product, which does not touch PostgreSQL at
        all, keeps working. Exiting instead would take Workspaces down over an
        index the user can reset in one command.
        """
        # 14.0 §2.4: warm the reranker on a background thread FIRST — it does
        # not depend on the database, so a schema mismatch below must not stop
        # it. getattr, because test fakes have no start_background_load.
        start_load = getattr(self.core.reranker, "start_background_load", None)
        if start_load is not None:
            start_load()
        try:
            await self.store.connect()
        except SchemaVersionMismatch as exc:
            log.error(
                "Index unavailable — PostgreSQL schema check refused this database: %s. "
                "Workspace tools continue; index and search tools will return this "
                "reason until the database is reset.",
                exc,
            )
            return
        attached_projects: list[Project] = []
        for project in self.registry.projects:
            if project.enabled:
                self.apply_extension_policy(project)
                # Before the boot-time reindex below, or that walk would run
                # against an unloaded list and re-index every de-indexed file.
                self.deindexed(project)
                await self.store.ensure_project(project.name)
                service = self._asset_service_for(project, None)
                await service.recover()
                attached_projects.append(project)
        if self.config.watch_enabled:
            from .watcher import WatcherManager

            watcher_services = {
                project_name: service
                for (project_name, connector_id), service in self._asset_services.items()
                if connector_id is None
            }
            self.watcher = WatcherManager(
                self.core,
                debounce_s=self.config.watch_debounce_s,
                poll_interval_s=self.config.watch_poll_interval_s,
                max_pending_paths=self.config.watch_max_pending_paths,
                retry_initial_s=self.config.watch_retry_initial_s,
                retry_max_s=self.config.watch_retry_max_s,
                asset_services=watcher_services,
                source_guard=self.source_guard,
            )
            await self.watcher.start(self.registry.projects)
        # Start both startup reconciliations only after watcher attachment so
        # changes racing either initial scan are observed. Reconciliation uses
        # the same per-project write lock as watcher events and explicit writes.
        for project in attached_projects:
            self.start_background_reindex(project, "incremental")
            self.start_background_asset_reconcile(project)
        if self.config.pg_probe_interval_s > 0:
            self._probe_task = asyncio.create_task(
                self._probe_loop(self.config.pg_probe_interval_s)
            )

    @property
    def index_status(self) -> dict[str, Any]:
        """What /healthz reports for the index half of the product (13.0 §4.1).

        `{"status": "ok"}`, or `{"status": "unavailable", "reason": <message>}`
        carrying the schema-version message verbatim — both numbers and the
        exact reset command — so the answer is actionable wherever it is read.
        The store owns the verdict; this is a view of it, not a second copy of
        the state. getattr for the same reason as in _dispatch: a store double
        in a test has no verdict, and the real store always has one.
        """
        error = getattr(self.store, "schema_error", None)
        if error is None:
            if self.source_guard.enabled:
                degraded = []
                for project in self.registry.projects:
                    if not project.enabled:
                        continue
                    state = self.source_guard.check(project.documents_dir)
                    if state.state != "available":
                        degraded.append({
                            "project": project.name,
                            "status": "unavailable",
                            "reason": state.reason or "source_unavailable",
                        })
                if degraded:
                    return {"status": "degraded", "sources": degraded}
            return {"status": "ok"}
        return {"status": "unavailable", "reason": str(error)}

    async def _probe_loop(self, interval_s: float) -> None:
        """The 4.0 deep probe (DESIGN-4.0 §3): a periodic SELECT 1. It exists
        to make a Postgres outage LOUD in the log the minute it starts, not to
        cure anything — there is no bounce ladder because there is nothing to
        bounce; the pool reconnects by itself when the server returns."""
        while True:
            await asyncio.sleep(interval_s)
            try:
                ok = await self.store.ping()
            except Exception as exc:
                ok = False
                if self.probe_ok:
                    log.error("Deep probe: PostgreSQL unreachable — searches will "
                              "fail cleanly until it returns (%s)", exc)
            if ok and not self.probe_ok:
                log.info("Deep probe: PostgreSQL recovered")
            self.probe_ok = ok

    async def shutdown(self) -> None:
        if self._probe_task is not None:
            self._probe_task.cancel()
            await asyncio.gather(self._probe_task, return_exceptions=True)
            self._probe_task = None
        if self.watcher is not None:
            await self.watcher.stop()
        background = [*self._reindex_tasks.values(), *self._asset_reconcile_tasks.values()]
        for task in background:
            task.cancel()
        if background:
            await asyncio.gather(*background, return_exceptions=True)
        # Reconciliation and startup backfills may use the shared scheduler;
        # stop their owned tasks before tearing the scheduler down.
        if self.scheduler is not None:
            await self.scheduler.shutdown()
        # 6.2: a warm pool is worker subprocesses holding VRAM on a timer.
        # Tear it down while the event loop is alive, after every indexing task
        # that could still claim or park it has been canceled and awaited.
        from .gpu_warm import WARM

        await asyncio.to_thread(WARM.shutdown_now, "service_shutdown")
        await self.store.close()

    # ---------------- gateway plumbing ----------------

    def url_for(self, project_name: str) -> str:
        return f"{LOCAL_ENGINE_BASE}/engine/{project_name}/mcp"

    def make_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app), base_url=LOCAL_ENGINE_BASE
        )

    async def call_tool(
        self,
        project: Project,
        tool: str,
        arguments: dict | None = None,
        *,
        trusted_connector_id: str | None = None,
        trusted_principal_id: str | None = None,
    ):
        """Direct tool dispatch for the admin API (it replaced the 3.x worker
        client, which 14.0.0 removed with the worker engine)."""
        return await self._dispatch(
            project, tool, arguments or {}, trusted_connector_id=trusted_connector_id,
            trusted_principal_id=trusted_principal_id,
        )

    # ---------------- ASGI app (the wire protocol) ----------------

    def _build_app(self) -> FastAPI:
        app = FastAPI(docs_url=None, redoc_url=None)

        @app.post("/engine/{name}/mcp")
        async def mcp(name: str, request: Request) -> Response:
            project = self.registry.get(name)
            if project is None or not project.enabled:
                return Response(status_code=404, content=f"Unknown project {name!r}")
            try:
                message = json.loads(await request.body())
            except json.JSONDecodeError:
                return JSONResponse(
                    {"jsonrpc": "2.0", "id": None,
                     "error": {"code": -32700, "message": "Parse error"}}
                )
            if not isinstance(message, dict):
                return JSONResponse(
                    {"jsonrpc": "2.0", "id": None,
                     "error": {"code": -32600, "message": "Invalid request"}}
                )
            method = message.get("method")
            msg_id = message.get("id")
            if isinstance(method, str) and method.startswith("notifications/"):
                return Response(status_code=202)
            if method == "initialize":
                params = message.get("params") or {}
                return JSONResponse({"jsonrpc": "2.0", "id": msg_id, "result": {
                    "protocolVersion": params.get("protocolVersion") or "2025-03-26",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "cognita-engine", "version": __version__},
                }})
            if method == "ping":
                return JSONResponse({"jsonrpc": "2.0", "id": msg_id, "result": {}})
            if method == "tools/list":
                return JSONResponse({"jsonrpc": "2.0", "id": msg_id,
                                     "result": {"tools": [copy.deepcopy(t) for t in ENGINE_TOOL_DEFS]}})
            if method == "tools/call":
                params = message.get("params")
                params = params if isinstance(params, dict) else {}
                tool = params.get("name", "")
                args = params.get("arguments")
                args = args if isinstance(args, dict) else {}
                trusted_connector_id = request.headers.get("x-cognita-connector-id") or None
                trusted_project_key_project = (
                    request.headers.get("x-cognita-project-key-project") or None
                )
                trusted_principal_id = request.headers.get("x-cognita-principal-id") or None
                try:
                    payload = await self._dispatch(
                        project, tool, args,
                        trusted_connector_id=trusted_connector_id,
                        trusted_project_key_project=trusted_project_key_project,
                        trusted_principal_id=trusted_principal_id,
                    )
                except KeyError:
                    return JSONResponse({"jsonrpc": "2.0", "id": msg_id,
                                         "error": {"code": -32602, "message": f"Unknown tool: {tool}"}})
                except Exception as exc:  # tool crash -> engine-style error payload
                    log.exception("engine tool %s failed", tool)
                    payload = {"status": "error", "reason": "internal_error",
                       "message": f"{type(exc).__name__}: {exc}"}
                extras = []
                candidate = payload
                is_error = isinstance(payload, dict) and payload.get("status") == "error"
                if isinstance(payload, dict) and "content" in payload:
                    content = payload.get("content")
                    if isinstance(content, list):
                        extras = content[1:]
                    candidate = payload.get("structuredContent")
                    if not isinstance(candidate, dict) and isinstance(content, list) and content:
                        try:
                            candidate = json.loads(content[0].get("text", ""))
                        except (TypeError, ValueError, AttributeError):
                            candidate = None
                    is_error = bool(payload.get("isError", is_error))
                result = build_tool_result(
                    tool, candidate if isinstance(candidate, dict) else {},
                    is_error=is_error, extra_content=extras,
                    mutating=tool in MUTATING_TOOLS or tool in ALL_ADDITIVE_MUTATING_TOOLS,
                )
                # MCP marks a failing tool on the result itself. On 2026-08-29,
                # an error status buried only in the text of an HTTP 200 response
                # made a client report that a deleted file remained. This keeps
                # the existing status payload and sets isError for clients.
                return JSONResponse({"jsonrpc": "2.0", "id": msg_id, "result": result})
            return JSONResponse({"jsonrpc": "2.0", "id": msg_id,
                                 "error": {"code": -32601, "message": f"Method not found: {method}"}})

        @app.api_route("/engine/{name}/mcp", methods=["GET", "DELETE"])
        async def mcp_no_session(name: str) -> Response:
            # Stateless server: no SSE push channel, no sessions to delete.
            return Response(status_code=405, headers={"Allow": "POST"})

        return app

    # ---------------- dispatch ----------------

    def _asset_service_for(
        self, project: Project, trusted_connector_id: str | None
    ) -> AssetService:
        """Get the project/connector asset namespace without trusting tool args."""
        key = (project.name, trusted_connector_id)
        service = self._asset_services.get(key)
        if service is None:
            repository = AssetRepository(
                self.store.pool,
                project.name,
                dimensions=self.config.embedding_dimensions,
            )
            policy_callbacks = {}
            asset_parameters = inspect.signature(AssetService).parameters
            if "book_mutation_policy" in asset_parameters:
                policy_callbacks["book_mutation_policy"] = lambda: self.book_mutation_policy_for(project)
            if "effective_index_policy" in asset_parameters:
                policy_callbacks["effective_index_policy"] = lambda: self.effective_index_policy_for(project)
            service = AssetService(
                project,
                repository,
                logger=log,
                backup_keep=self.config.backup_keep_per_file,
                embedder=self.core.embedder,
                reranker=self.core.reranker,
                limits=self.config,
                connector_id=trusted_connector_id,
                ocr_service=self.ocr_service,
                scheduler=self.scheduler,
                **policy_callbacks,
            )
            self._asset_services[key] = service
        return service

    def project_state_for(self, project: Project) -> ProjectState | None:
        """Discover existing source-side authority without initializing it."""
        return ProjectState.discover(project.documents_dir)

    def book_config_snapshot_for(self, project: Project):
        state = self.project_state_for(project)
        return load_book_config(project.documents_dir, state)

    def read_registered_file(self, project: Project, relative_path: str) -> tuple[bytes, str]:
        from .books.service import _read_bytes
        raw = _read_bytes(project.documents_dir, relative_path)
        return raw, hashlib.sha256(raw).hexdigest()

    def book_chapter_state_for(self, project: Project, chapter_id: str):
        from .books.config import validate_chapter_state
        config = self.book_config_snapshot_for(project)
        if config.config_state != "enabled" or config.layout is None:
            raise ProjectStateError("book configuration is not enabled")
        chapter = next((item for item in config.layout.chapters if item.chapter_id == chapter_id), None)
        if chapter is None:
            raise ProjectStateError("chapter is not registered")
        raw = self.read_registered_file(project, chapter.chapter_state_filepath)[0]
        try:
            return validate_chapter_state(raw)
        except Exception as exc:
            raise ProjectStateError("chapter state is damaged") from exc

    def production_settings_for(self, project: Project):
        from .books.config import validate_production_settings
        config = self.book_config_snapshot_for(project)
        if config.config_state != "enabled" or config.layout is None:
            raise ProjectStateError("book configuration is not enabled")
        relative = config.layout.shared_paths.production_settings_filepath
        raw = self.read_registered_file(project, relative)[0]
        try:
            return validate_production_settings(raw)
        except Exception as exc:
            raise ProjectStateError("production settings are damaged") from exc

    def book_service_for(self, project: Project) -> BookService:
        service = self._book_services.get(project.name)
        if service is None:
            service = BookService(
                project.documents_dir, project.name,
                state=self.project_state_for(project),
            )
            self._book_services[project.name] = service
        return service

    def effective_index_policy_for(self, project_or_name: Project | str) -> EffectiveIndexPolicy:
        project = (self.registry.get(project_or_name)
                   if isinstance(project_or_name, str) else project_or_name)
        if project is None:
            raise ProjectStateError("project is not registered")
        state = self.project_state_for(project)
        folder = state.folder_policy() if state is not None else None
        legacy = self.deindexed(project)
        if legacy.load_error:
            raise ProjectStateError("legacy per-file exclusion state is damaged")
        config = load_book_config(project.documents_dir, state)
        if config.config_state == "configuration_conflict":
            raise ProjectStateError("book configuration is damaged")
        layout = config.layout if config.config_state == "enabled" else None
        from .books.config import FolderRule
        return EffectiveIndexPolicy(
            [FolderRule(path=path, indexed=indexed)
             for path, indexed in (folder.rules if folder else ())],
            hard_exclusion_roots=(".cognita-storage",),
            deindexed_paths=legacy.sorted(), book_layout=layout,
        )

    def book_mutation_policy_for(self, project: Project) -> BookMutationPolicy:
        state = self.project_state_for(project)
        config = load_book_config(project.documents_dir, state)
        return BookMutationPolicy(
            config.layout, config_state=config.config_state,
            binding=config.binding if config.config_state == "enabled" else None,
        )

    def book_index_provenance_for(
        self, project: Project | str, source_path: str, doc_id: str,
        extracted_sha256: str, raw_sha256: str, extraction_version: str,
    ):
        if isinstance(project, str):
            project = self.registry.get(project)
        if project is None:
            return None
        return self.book_service_for(project).index_provenance_for(
            source_path, doc_id, extracted_sha256, raw_sha256, extraction_version,
        )

    def book_index_provenance_is_current(self, project: Project | str, record) -> bool:
        if isinstance(project, str):
            project = self.registry.get(project)
        if project is None:
            return False
        return self.book_service_for(project).index_provenance_is_current(record)

    def record_book_index_provenance_for(
        self, project: Project | str, source_path: str, doc_id: str,
        extracted_sha256: str, raw_sha256: str, extraction_version: str,
    ):
        if isinstance(project, str):
            project = self.registry.get(project)
        if project is None:
            return None
        return self.book_service_for(project).record_index_provenance(
            source_path, doc_id, extracted_sha256, raw_sha256, extraction_version,
        )

    def book_index_admission_for(
        self, project_or_name, sources, retrieval_profile: str | None = None,
    ) -> dict[str, dict[str, Any]]:
        project = (self.registry.get(project_or_name)
                   if isinstance(project_or_name, str) else project_or_name)
        if project is None:
            return {}
        return self.book_service_for(project).index_admitted_doc_ids(sources, retrieval_profile)

    async def _dispatch_book_tool(
        self, project: Project, tool: str, args: dict, *,
        trusted_connector_id: str | None,
        trusted_project_key_project: str | None,
        trusted_principal_id: str | None,
    ) -> dict:
        from .books.schemas import success_envelope, error_envelope
        request_models = {
            "audiobook_inspect_chapter": book_dto.InspectRequest,
            "audiobook_prepare_chapter": book_dto.PrepareRequest,
            "audiobook_get_chapter": book_dto.GetChapterRequest,
            "audiobook_find_chunk": book_dto.FindChunkRequest,
            "audiobook_record_generation": book_dto.RecordGenerationRequest,
            "audiobook_get_generations": book_dto.GetGenerationsRequest,
            "set_folder_indexing": book_dto.SetFolderIndexingRequest,
            "list_project_files": book_dto.ListProjectFilesRequest,
            "read_project_file": book_dto.ReadProjectFileRequest,
        }
        mutating = tool in ALL_ADDITIVE_MUTATING_TOOLS
        correlation_id = uuid.uuid4().hex
        try:
            request_model = request_models[tool].model_validate(args, strict=True)
            if request_model.project != project.name:
                raise BookServiceError("project_mismatch", "The request project does not match the authenticated route.")
            owner_key = (
                f"principal:{trusted_principal_id}" if trusted_principal_id else
                f"connector:{trusted_connector_id}" if trusted_connector_id else
                "principal:local-admin"
            )
            service = self.book_service_for(project)

            async def perform() -> dict[str, Any]:
                if tool == "audiobook_inspect_chapter":
                    data = service.inspect(request_model)
                elif tool == "audiobook_prepare_chapter":
                    data, replayed = service.prepare(request_model, owner_key=owner_key)
                    return success_envelope(tool, data, operation_id=request_model.operation_id, replayed=replayed)
                elif tool == "audiobook_get_chapter":
                    data = service.get_chapter(request_model)
                elif tool == "audiobook_find_chunk":
                    data = service.find_chunk(request_model)
                elif tool == "audiobook_record_generation":
                    data, replayed = service.record_generation(request_model, owner_key=owner_key)
                    return success_envelope(tool, data, operation_id=request_model.operation_id, replayed=replayed)
                elif tool == "audiobook_get_generations":
                    data = service.get_generations(request_model)
                elif tool == "list_project_files":
                    data = service.list_files(
                        request_model.path,
                        recursive=request_model.recursive if "recursive" in request_model.model_fields_set else False,
                        cursor=request_model.cursor if "cursor" in request_model.model_fields_set else None,
                        limit=request_model.limit if "limit" in request_model.model_fields_set else 100,
                        effective_index=self.effective_index_policy_for(project),
                    )
                elif tool == "read_project_file":
                    data = service.read_file(
                        request_model.path,
                        offset=request_model.offset if "offset" in request_model.model_fields_set else 0,
                        max_bytes=request_model.max_bytes if "max_bytes" in request_model.model_fields_set else 262144,
                        expected_bytes_sha256=(request_model.expected_bytes_sha256
                                               if "expected_bytes_sha256" in request_model.model_fields_set else None),
                    )
                else:
                    data, replayed = service.set_folder_indexing(
                        path=request_model.path, indexed=request_model.indexed,
                        operation_id=request_model.operation_id,
                        expected_policy_revision=request_model.expected_policy_revision,
                        owner_key=owner_key,
                    )
                    if not replayed:
                        # Policy authority commits before PostgreSQL work.  A
                        # disabled path is therefore invisible to search as soon
                        # as this call returns even if physical cleanup cannot
                        # run.  When the index is healthy, use the existing
                        # targeted reconciler immediately; it deletes derived
                        # rows under the path and re-admits only currently
                        # eligible descendants on an enable.
                        job_id = data["job_id"]
                        try:
                            service.update_folder_policy_job(job_id, "running", {})
                            dirty_path = request_model.path or "."
                            summary = await self.core.reconcile_paths(
                                project.name, project.documents_dir, [dirty_path],
                            )
                            if summary.get("failed", 0):
                                raise RuntimeError("derived policy reconciliation failed")
                            terminal = "indexed" if request_model.indexed else "excluded"
                            service.update_folder_policy_job(
                                job_id, terminal,
                                {"indexed": summary.get("indexed", 0),
                                 "removed": summary.get("removed", 0)},
                            )
                        except Exception as exc:
                            # Do not turn a committed source-side exclusion into
                            # a false success about derived cleanup.  The durable
                            # job remains pending for startup/reindex recovery.
                            try:
                                service.update_folder_policy_job(
                                    job_id, "pending", {"error": type(exc).__name__},
                                )
                            except BookServiceError:
                                pass
                            return error_envelope(
                                tool, reason="index_cleanup_pending",
                                message=("Folder policy was committed, but derived index cleanup is "
                                         "pending until PostgreSQL is available."),
                                operation_outcome="committed", correlation_id=correlation_id,
                                details={"job_id": job_id},
                            )
                    return success_envelope(tool, data, operation_id=request_model.operation_id, replayed=replayed)
                return success_envelope(tool, data)

            if not mutating:
                return await perform()
            lock = self.core.write_lock(project.name)
            if not await lock.acquire_within(WRITE_LOCK_WAIT_S):
                raise BookServiceError("busy", "Another project write is in progress.")
            try:
                source_status = self.source_guard.check(project.documents_dir)
                if source_status.state != "available":
                    raise BookServiceError("source_unavailable", "The project source is unavailable or reconciling.")
                if denied := self._connector_write_denial(
                    project, trusted_connector_id, trusted_project_key_project,
                ):
                    raise BookServiceError(denied.get("reason", "permission_denied"), denied.get("message", "Write access is required."))
                return await perform()
            finally:
                await lock.release()
        except Exception as exc:
            if isinstance(exc, (BookServiceError, ProjectFileError)):
                reason = exc.reason
                message = str(exc)
                outcome = getattr(exc, "outcome", "not_applied")
            elif isinstance(exc, (ProjectStateError, OSError)):
                reason, message, outcome = "state_unavailable", "Durable project state is unavailable.", "outcome_unknown" if mutating else "not_applied"
            else:
                # DTO failures and projection exceptions are intentionally
                # reduced to a bounded, non-content diagnostic.
                reason = "validation_failed" if isinstance(exc, (ValueError, TypeError)) else "internal_error"
                message = "The request or source could not be processed safely."
                outcome = "not_applied"
            return error_envelope(
                tool, reason=reason, message=message,
                operation_outcome=outcome, correlation_id=correlation_id,
            )

    async def _dispatch(
        self,
        project: Project,
        tool: str,
        args: dict,
        *,
        trusted_connector_id: str | None = None,
        trusted_project_key_project: str | None = None,
        trusted_principal_id: str | None = None,
    ) -> dict:
        if tool in ALL_ADDITIVE_TOOL_NAMES:
            return await self._dispatch_book_tool(
                project, tool, args, trusted_connector_id=trusted_connector_id,
                trusted_project_key_project=trusted_project_key_project,
                trusted_principal_id=trusted_principal_id,
            )
        # 13.0 §4.1: one gate for the whole index tool surface. Every tool
        # reachable here — search, read, write, asset, OCR — needs the store, so
        # a mismatched database is refused once, here, with the reason and the
        # reset command. Workspace tools do not come through this dispatcher and
        # are deliberately unaffected. No state machine: the store's own
        # schema_error is the only flag, and nothing clears it in-process.
        #
        # getattr, because tests substitute narrow store doubles here and a
        # double that never connected has no verdict to report. The real store
        # (cognita.store.Store) always defines the attribute, so this default
        # can only be reached by a stand-in.
        schema_error = getattr(self.store, "schema_error", None)
        if schema_error is not None:
            log.error(
                "Refused %s on %s: index unavailable (%s)",
                tool, project.name, schema_error,
            )
            return {
                "status": "error",
                "reason": "index_unavailable",
                "message": str(schema_error),
            }
        if tool in MUTATING_TOOLS or tool in ASSET_MUTATING_TOOLS:
            source_status = self.source_guard.check(project.documents_dir)
            if source_status.state != "available":
                if source_status.state == "reconnected":
                    self.start_background_reindex(project, "incremental")
                return {
                    "status": "error",
                    "reason": "source_unavailable",
                    "message": "The project source is unavailable or is being reconciled.",
                }
        if tool in ASSET_TOOL_NAMES:
            service = self._asset_service_for(project, trusted_connector_id)
            rejected = reject_unknown_arguments(ENGINE_TOOL_DEFS_BY_NAME[tool], args)
            if rejected is not None:
                return rejected
            mistyped = reject_wrong_types(ENGINE_TOOL_DEFS_BY_NAME[tool], args)
            if mistyped is not None:
                return mistyped

            async def call_asset() -> dict:
                try:
                    return await getattr(service, tool)(args)
                except AssetError as exc:
                    response = {"status": "error", "reason": exc.reason, "message": exc.message}
                    if exc.details:
                        response.update(exc.details)
                    return response

            if tool not in ASSET_MUTATING_TOOLS:
                return await call_asset()
            lock = self.core.write_lock(project.name)
            if not await lock.acquire_within(WRITE_LOCK_WAIT_S):
                return {"status": "error", "reason": "busy",
                        "message": "Another project write is still in progress.",
                        "retry_after_seconds": WRITE_LOCK_WAIT_S}
            try:
                source_status = self.source_guard.check(project.documents_dir)
                if source_status.state != "available":
                    return {
                        "status": "error", "reason": "source_unavailable",
                        "message": "The project source is unavailable or is being reconciled.",
                    }
                if denied := self._connector_write_denial(
                    project, trusted_connector_id, trusted_project_key_project
                ):
                    return denied
                return await call_asset()
            finally:
                await lock.release()
        handler = {
            "search_knowledge": self._search_knowledge,
            "get_document": self._get_document,
            "get_documents": self._get_documents,
            "search_similar": self._search_similar,
            "list_documents": self._list_documents,
            "list_categories": self._list_categories,
            "get_index_stats": self._get_index_stats,
            "get_reindex_status": self._get_reindex_status,
            "evaluate_retrieval": self._evaluate_retrieval,
            "find_literal": self._find_literal,
            "add_document": self._add_document,
            "update_document": self._update_document,
            "write_documents": self._write_documents,
            "remove_document": self._remove_document,
            "remove_documents": self._remove_documents,
            "move_document": self._move_document,
            "add_from_url": self._add_from_url,
            "reindex_documents": self._reindex_documents,
            "copy_document": self._copy_document,
            "copy_directory": self._copy_directory,
            "remove_directory": self._remove_directory,
        }[tool]
        # 5.0 §2: an argument the schema does not declare is a REFUSAL, not a
        # silently dropped key. list_documents(path_prefix=...) used to return all
        # 319 documents and report success; the damage was never the wasted call
        # but a client that believed it had filtered.
        rejected = reject_unknown_arguments(ENGINE_TOOL_DEFS_BY_NAME[tool], args)
        if rejected is not None:
            log.info("Refused %s: unknown argument(s) %s", tool, rejected["rejected_arguments"])
            return rejected
        # The name is declared; is the VALUE the declared type? Handlers do
        # int(args.get("max_results") or 5), so a string there raised ValueError
        # and surfaced as internal_error with raw exception text.
        mistyped = reject_wrong_types(ENGINE_TOOL_DEFS_BY_NAME[tool], args)
        if mistyped is not None:
            log.info("Refused %s: wrong type for %s", tool, mistyped["invalid_arguments"])
            return mistyped
        if tool == "move_document":
            # Directory-move receipts are durable project state, so their owner
            # must be trusted route identity rather than a client-supplied field.
            args = dict(args)
            args["_owner_key"] = (
                f"principal:{trusted_principal_id}" if trusted_principal_id else
                f"connector:{trusted_connector_id}" if trusted_connector_id else
                "principal:local-admin"
            )
        # 5.1: the single-writer gate belongs to the ENGINE, not to one caller of
        # it. proxy._intercept holds _worker_write_lock for every mutating call,
        # but LocalEngineHost.call_tool dispatches straight past that — the admin
        # API and stdio mode go that way. Nothing is broken today (the admin API
        # only reaches get_index_stats and reindex_documents), but the guarantee
        # was one new admin endpoint away from a lost update: an admin write
        # landing between a gateway edit's read and its write is silently
        # reverted, with no error and no backup on the admin side.
        # The lock is re-entrant per task, so the handlers that already take it
        # (remove/move/remove_directory/copy_directory, and index_file within the
        # others) are unaffected.
        if tool in MUTATING_TOOLS:
            lock = self.core.write_lock(project.name)
            if not await lock.acquire_within(WRITE_LOCK_WAIT_S):
                # A full rebuild holds this for the whole corpus walk, and the
                # gateway's read timeout is None — so a mutating call arriving
                # during one used to hang until the CLIENT gave up, then get
                # retried into a stale_file / not_found for a write that had by
                # then succeeded. Measured: a remove_file waited 1.42s behind a
                # 150-document rebuild, i.e. the rebuild's entire remainder.
                # Answering quickly is strictly better, and the retry is safe now.
                status = self._reindex_block(project.name)
                log.info("Refused %s on %s: write lock busy", tool, project.name)
                return {
                    "status": "error", "reason": "busy",
                    "message": (
                        f"Another write is in progress on {project.name!r} and did not "
                        f"finish within {WRITE_LOCK_WAIT_S:.0f}s, so NOTHING was written. "
                        "This is almost always a reindex holding the project write lock "
                        "for its whole walk. Check get_reindex_status, then retry — "
                        "passing an operation_id makes the retry safe."
                    ),
                    "retry_after_seconds": WRITE_LOCK_WAIT_S,
                    "reindex": status,
                }
            try:
                source_status = self.source_guard.check(project.documents_dir)
                if source_status.state != "available":
                    if source_status.state == "reconnected":
                        self.start_background_reindex(project, "incremental")
                    return {
                        "status": "error", "reason": "source_unavailable",
                        "message": "The project source is unavailable or is being reconciled.",
                    }
                if tool != "reindex_documents" and (
                    denied := self._connector_write_denial(
                        project, trusted_connector_id, trusted_project_key_project
                    )):
                    return denied
                if tool == "reindex_documents":
                    payload = await handler(
                        project,
                        args,
                        trusted_connector_id=trusted_connector_id,
                        trusted_project_key_project=trusted_project_key_project,
                    )
                else:
                    payload = await handler(project, args)
                return _with_reason(tool, payload)
            finally:
                await lock.release()
        if tool == "reindex_documents":
            return _with_reason(
                tool,
                await handler(
                    project,
                    args,
                    trusted_connector_id=trusted_connector_id,
                    trusted_project_key_project=trusted_project_key_project,
                ),
            )
        return _with_reason(tool, await handler(project, args))

    def _connector_write_denial(
        self, project: Project, trusted_connector_id: str | None,
        trusted_project_key_project: str | None = None,
    ) -> dict | None:
        """Recheck remote write authority at engine execution admission."""
        if trusted_connector_id is None:
            return None
        try:
            access = self.connector_store.effective_access(
                trusted_connector_id, project.name, self.registry.projects,
                project_key_grant=trusted_project_key_project,
            )
        except ConnectorPolicyError as exc:
            log.error(
                "Engine write policy unavailable connector_id=%s project=%s error=%s",
                trusted_connector_id, project.name, type(exc).__name__,
            )
            return {
                "status": "error", "reason": "policy_unavailable",
                "message": "Connector policy is unavailable; nothing was written.",
            }
        if access is None:
            return {
                "status": "error", "reason": "project_unavailable",
                "message": "The connector cannot access this project.",
            }
        if access.access != "write" or self.config.remote_readonly:
            return {
                "status": "error", "reason": "read_only",
                "message": "The connector has read-only access to this project.",
            }
        return None


    # ---------------- shared helpers ----------------

    def _abs(self, project: Project, source: str) -> str:
        return str(Path(project.documents_dir) / source)

    def _rel(self, project: Project, target: Path) -> str:
        return target.relative_to(Path(project.documents_dir).resolve()).as_posix()

    def deindexed(self, project: Project) -> DeindexedPaths:
        """This project's de-index list, registered with the core on first use.

        The core is the layer that consults the list (every walk goes through
        index_project) and the engine is the only layer that knows where each
        project's data_dir is, so the registration has to happen here. Lazy as
        well as eager — startup() calls apply_extension_policy for every enabled
        project, but the admin API and the tests build a host without it, and a
        list that silently defaulted to empty in those paths would un-suppress
        every de-indexed file the moment one of them ran a reindex.
        """
        registered = self.core.deindexed_for(project.name)
        if registered is None:
            registered = DeindexedPaths(Path(project.data_dir) / DEINDEXED_FILENAME)
            self.core.set_deindexed(project.name, registered)
        return registered

    def apply_extension_policy(self, project: Project) -> ExtensionPolicy:
        """Resolve this project's tier map (per-connector override, else the
        global config default) and register it with the core.

        An extension named in both lists resolves to REGISTERED, because the
        extensions this tier exists for were already in the embedded allowlist:
        "embedded wins" would make naming one in registered_extensions a no-op.
        """
        policy = ExtensionPolicy.build(
            project.indexed_extensions
            if project.indexed_extensions is not None
            else self.config.indexed_extensions,
            project.registered_extensions
            if project.registered_extensions is not None
            else self.config.registered_extensions,
        )
        if policy.conflicts:
            log.warning(
                "Project %s: %s listed in BOTH indexed_extensions and "
                "registered_extensions — resolving to the registered tier "
                "(never embedded)",
                project.name, ", ".join(policy.conflicts),
            )
        self.core.set_policy(project.name, policy)
        return policy



    async def detach_project(self, project_name: str) -> None:
        """Stop project-owned background work and discard path-bound services."""
        if self.watcher is not None:
            watcher_task = self.watcher.unwatch(project_name)
            if watcher_task is not None:
                await asyncio.gather(watcher_task, return_exceptions=True)
        tasks = [
            mapping.pop(project_name)
            for mapping in (self._reindex_tasks, self._asset_reconcile_tasks)
            if project_name in mapping
        ]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        for key in [key for key in self._asset_services if key[0] == project_name]:
            self._asset_services.pop(key, None)
