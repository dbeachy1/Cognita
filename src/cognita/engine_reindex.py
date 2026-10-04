"""Reindex operations inherited by LocalEngineHost.

These methods use the host's existing project, config, core, store, connector, and
operation state; this class adds no fields or lifecycle behavior.
"""
from __future__ import annotations

import asyncio
import inspect
from datetime import datetime
from pathlib import Path
from typing import Any
from .connectors import ConnectorPolicyError
from .document_roots import display_for
from .registry import Project
from .engine_contract import _ReindexContext, log


class EngineReindexOperations:
    def _reindex_block(self, name: str) -> dict:
        progress = self._reindex_progress.get(name, {})
        if not progress.get("active"):
            return {"active": False}
        total = max(1, progress.get("total_files", 1))
        processed = progress.get("processed", 0)
        return {
            "active": True,
            "operation": progress.get("operation"),
            "progress": f"{processed}/{progress.get('total_files', 0)}",
            "percent": round(processed / total * 100),
            "indexed": progress.get("indexed", 0),
            "skipped": progress.get("skipped", 0),
            "errors": progress.get("errors", 0),
            "started_at": progress.get("started_at"),
        }

    def _reindex_admission(
        self,
        project: Project,
        trusted_connector_id: str | None,
        trusted_project_key_project: str | None = None,
    ) -> tuple[_ReindexContext | None, dict | None]:
        """Snapshot trusted routing and reject an unsafe queued operation."""
        project_name = project.name
        project_copy = project.model_copy(deep=True)
        documents_dir = Path(project_copy.documents_dir)
        if not project_copy.enabled:
            log.info(
                "Background reindex denied project=%s outcome=project_disabled",
                project_name,
            )
            return None, {
                "status": "error",
                "reason": "project_unavailable",
                "message": "The project is unavailable for background reindex.",
            }

        if trusted_connector_id is None:
            # Local maintenance and boot-time indexing do not have a remote
            # connector identity. Their administrator boundary is separate from
            # remote connector policy, but the project snapshot is still fixed.
            return _ReindexContext(project_name, documents_dir, None, None, None), None

        try:
            access = self.connector_store.effective_access(
                trusted_connector_id,
                project_name,
                self.registry.projects,
                project_key_grant=trusted_project_key_project,
            )
        except ConnectorPolicyError as exc:
            log.error(
                "Background reindex policy unavailable connector_id=%s project=%s "
                "phase=admission error=%s",
                trusted_connector_id,
                project_name,
                type(exc).__name__,
            )
            return None, {
                "status": "error",
                "reason": "policy_unavailable",
                "message": "Connector policy is unavailable for background reindex.",
            }
        if access is None:
            log.info(
                "Background reindex denied connector_id=%s project=%s "
                "outcome=project_unavailable phase=admission",
                trusted_connector_id,
                project_name,
            )
            return None, {
                "status": "error",
                "reason": "project_unavailable",
                "message": "The connector cannot access this project.",
            }
        if access.access != "write":
            log.info(
                "Background reindex denied connector_id=%s project=%s access=%s "
                "outcome=read_only phase=admission",
                trusted_connector_id,
                project_name,
                access.access,
            )
            return None, {
                "status": "error",
                "reason": "read_only",
                "message": "The connector has read-only access to this project.",
            }
        return _ReindexContext(
            project_name,
            documents_dir,
            trusted_connector_id,
            trusted_project_key_project,
            access.revision,
        ), None

    def _reindex_execution_allowed(self, context: _ReindexContext) -> bool:
        """Recheck project and connector policy immediately before indexing."""
        current_project = self.registry.get(context.project_name)
        if current_project is None or not current_project.enabled:
            log.info(
                "Background reindex denied connector_id=%s project=%s "
                "outcome=project_unavailable phase=execution",
                context.connector_id,
                context.project_name,
            )
            return False
        if Path(current_project.documents_dir) != context.documents_dir:
            log.info(
                "Background reindex denied connector_id=%s project=%s "
                "outcome=project_identity_changed phase=execution",
                context.connector_id,
                context.project_name,
            )
            return False
        if context.connector_id is None:
            return True
        try:
            access = self.connector_store.effective_access(
                context.connector_id,
                context.project_name,
                self.registry.projects,
                project_key_grant=context.project_key_grant,
            )
        except ConnectorPolicyError as exc:
            log.error(
                "Background reindex policy unavailable connector_id=%s project=%s "
                "phase=execution error=%s",
                context.connector_id,
                context.project_name,
                type(exc).__name__,
            )
            return False
        if access is None or access.access != "write":
            outcome = "project_unavailable" if access is None else "read_only"
            log.info(
                "Background reindex denied connector_id=%s project=%s access=%s "
                "outcome=%s phase=execution",
                context.connector_id,
                context.project_name,
                access.access if access is not None else "none",
                outcome,
            )
            return False
        return True

    def start_background_asset_reconcile(self, project: Project) -> bool:
        """Attach existing PNGs to the catalog without delaying host readiness."""
        task = self._asset_reconcile_tasks.get(project.name)
        if task and not task.done():
            return False
        service = self._asset_service_for(project, None)

        async def run() -> None:
            try:
                async with self.core.write_lock(project.name):
                    source_guard = self.source_guard
                    if source_guard.enabled:
                        source_status = source_guard.check(
                            project.documents_dir, validate_names=True,
                        )
                        if source_status.state != "available":
                            log.warning(
                                "Asset backfill skipped project=%s reason=%s",
                                project.name, source_status.reason or source_status.state,
                            )
                            return
                        reconcile = service.reconcile_all
                        try:
                            parameters = inspect.signature(reconcile).parameters.values()
                        except (TypeError, ValueError):
                            parameters = ()
                        if any(parameter.name == "source_is_safe" or
                               parameter.kind == inspect.Parameter.VAR_KEYWORD
                               for parameter in parameters):
                            result = await reconcile(source_is_safe=lambda: source_guard.check(
                                project.documents_dir,
                            ).state == "available")
                        else:
                            result = await reconcile()
                    else:
                        result = await service.reconcile_all()
                log.info(
                    "Asset backfill %s: indexed=%d removed=%d errors=%d",
                    project.name,
                    result["indexed"],
                    result["removed"],
                    len(result["errors"]),
                )
                if result["errors"]:
                    if self.source_guard.enabled:
                        log.warning(
                            "Asset backfill project=%s completed with %d bounded error(s)",
                            project.name, len(result["errors"]),
                        )
                    else:
                        log.warning(
                            "Asset backfill %s completed with %d bounded error(s): %s",
                            project.name,
                            len(result["errors"]),
                            result["errors"],
                        )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # Catalog/bootstrap failure must be visible without taking the
                # MCP host or unrelated projects down.
                if self.source_guard.enabled:
                    log.error("Asset backfill failed project=%s reason=%s",
                              project.name, type(exc).__name__)
                else:
                    log.exception("Asset backfill %s failed", project.name)

        self._asset_reconcile_tasks[project.name] = asyncio.create_task(
            run(), name=f"asset-backfill:{project.name}",
        )
        return True

    def start_background_reindex(
        self,
        project: Project,
        operation: str,
        *,
        trusted_connector_id: str | None = None,
        context: _ReindexContext | None = None,
    ) -> bool:
        """Start a background (re)index unless one is already running. Returns
        False when one is active."""
        task = self._reindex_tasks.get(project.name)
        if task and not task.done():
            return False
        if context is None:
            context, admission_error = self._reindex_admission(
                project, trusted_connector_id
            )
            if admission_error is not None:
                log.info(
                    "Background reindex not started project=%s reason=%s",
                    project.name,
                    admission_error["reason"],
                )
                return False
        self._reindex_progress[project.name] = {
            "active": True,
            "operation": operation,
            "total_files": 0,
            "processed": 0,
            "indexed": 0,
            "skipped": 0,
            "errors": 0,
            "started_at": datetime.now().isoformat(),
        }

        def on_progress(state: dict) -> None:
            self._reindex_progress[context.project_name].update(
                total_files=state.get("total_files", 0),
                processed=state.get("processed", 0),
                indexed=state.get("indexed", 0),
                skipped=state.get("skipped", 0),
                errors=len(state.get("errors", [])),
            )

        async def run() -> None:
            progress = self._reindex_progress[context.project_name]
            try:
                if not self._reindex_execution_allowed(context):
                    progress["error"] = (
                        "Background reindex denied by the current project or "
                        "connector policy."
                    )
                    return
                source_status = self.source_guard.check(
                    context.documents_dir, validate_names=True,
                )
                if source_status.state == "unavailable":
                    if source_status.collision_paths:
                        first, second = source_status.collision_paths
                        progress["error"] = (
                            "Windows case-fold collision between relative paths "
                            f"'{first}' and '{second}'."
                        )
                    else:
                        progress["error"] = "The project source is unavailable."
                    return
                index_kwargs: dict[str, Any] = {}
                if self.source_guard.enabled:
                    try:
                        parameters = inspect.signature(self.core.index_project).parameters.values()
                    except (TypeError, ValueError):
                        parameters = ()
                    if any(parameter.name == "before_removal" or
                           parameter.kind == inspect.Parameter.VAR_KEYWORD
                           for parameter in parameters):
                        index_kwargs["before_removal"] = lambda: self.source_guard.check(
                            context.documents_dir,
                        ).state in {"available", "reconnected"}
                summary = await self.core.index_project(
                    context.project_name,
                    context.documents_dir,
                    force=(operation == "nuclear_rebuild"),
                    progress=on_progress,
                    **index_kwargs,
                )
                reconciled_source = self.source_guard.mark_reconciled(context.documents_dir)
                if reconciled_source.state == "unavailable":
                    progress["error"] = reconciled_source.reason or "source_unavailable"
                    return
                progress["result"] = {
                    "indexed": summary["indexed"],
                    "skipped": summary["skipped"],
                    "removed": summary["removed"],
                    "errors": len(summary["errors"]),
                    "total_files": summary["total_files"],
                    # 4.4: how much of this run was a tier migration, and how
                    # many vectors it destroyed. Non-zero on the first reindex
                    # after upgrade; zero thereafter.
                    "tier_changed": summary.get("tier_changed", 0),
                    "chunks_purged": summary.get("chunks_purged", 0),
                }
                # Installer design 7.3: the empty-walk refusal was safe but invisible.
                # Surface its plain sentence where a failed reindex's error lands, so
                # Admin's project view (project_status -> reindex_error) shows it.
                if summary.get("empty_root_message"):
                    progress["error"] = summary["empty_root_message"]
                    log.warning(
                        "Reindex %s: documents folder empty or not mounted; progress error set "
                        "(nothing removed) path=%s",
                        context.project_name, context.documents_dir,
                    )
                log.info(
                    "Reindex %s (%s): %s",
                    context.project_name,
                    operation,
                    progress["result"],
                )
                if summary.get("chunks_purged"):
                    log.warning(
                        "Reindex %s: %d document(s) changed tier; purged %d chunk(s) "
                        "and their vectors (documents moved to the registered tier "
                        "are no longer semantically searchable)",
                        context.project_name,
                        summary["tier_changed"],
                        summary["chunks_purged"],
                    )
            except Exception as exc:
                progress["error"] = (
                    type(exc).__name__ if self.source_guard.enabled
                    else f"{type(exc).__name__}: {exc}"
                )
                if isinstance(exc, NotADirectoryError) and not self.source_guard.enabled:
                    # C10: a documents folder missing at startup (an unplugged drive, a
                    # cloud folder not synced yet) is shown in Admin in plain words, not
                    # as an exception name.  Proven on the installer VM, 2026-09-28.
                    progress["error"] = (
                        f"The documents folder {display_for(str(context.documents_dir))} is missing or not "
                        "readable. Nothing was removed. When it is back, restart Cognita "
                        "or reindex the project."
                    )
                if self.source_guard.enabled:
                    log.error("Reindex failed project=%s reason=%s",
                              context.project_name, type(exc).__name__)
                else:
                    log.exception("Reindex %s failed", context.project_name)
            finally:
                progress["active"] = False

        self._reindex_tasks[project.name] = asyncio.create_task(run())
        return True

    async def _reindex_documents(
        self,
        project: Project,
        args: dict,
        *,
        trusted_connector_id: str | None = None,
        trusted_project_key_project: str | None = None,
    ) -> dict:
        force = bool(args.get("force", False))
        full_rebuild = bool(args.get("full_rebuild", False))
        if full_rebuild:
            operation = "nuclear_rebuild"  # -> force re-embed of everything
        elif force:
            operation = "smart_reindex"
        else:
            operation = "incremental"
        # (smart_reindex and incremental are the same pass in 4.0: change
        # detection is always on, so both index new/changed and drop vanished.)
        context, admission_error = self._reindex_admission(
            project, trusted_connector_id, trusted_project_key_project
        )
        if admission_error is not None:
            return admission_error
        if not self.start_background_reindex(
            project,
            operation,
            trusted_connector_id=trusted_connector_id,
            context=context,
        ):
            progress = self._reindex_progress.get(project.name, {})
            return {
                "status": "already_running",
                "progress": f"{progress.get('processed', 0)}/{progress.get('total_files', 0)}",
                "operation": progress.get("operation"),
                "hint": "Use get_reindex_status() to check progress",
            }
        return {"status": "started", "operation": operation,
                "message": "Reindex running in background. Use get_reindex_status() "
                           "to monitor progress."}
