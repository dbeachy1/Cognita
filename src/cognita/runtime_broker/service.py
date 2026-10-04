"""Broker application service: validation, auth-independent dispatch, and replay."""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
from threading import RLock
from typing import Any
from uuid import UUID

from pydantic import ValidationError

from .journal import RequestJournal, request_digest
from .protocol import (
    MAX_RESPONSE_BYTES,
    BrokerOperation,
    ErrorCode,
    RpcFailure,
    RpcRequest,
    RpcSuccess,
)
from .state import RuntimeStateStore
from .sdk_adapter import RuntimeAdapter
from .sdk_adapter import SdkOperationError, operation_context
from .validation import ArgumentError, ResourcePolicy, normalize_rpc_arguments

log = logging.getLogger(__name__)

MUTATING_OPERATIONS = {
    BrokerOperation.ENSURE,
    BrokerOperation.START,
    BrokerOperation.STOP,
    BrokerOperation.REMOVE,
    BrokerOperation.FS_WRITE,
    BrokerOperation.FS_EDIT,
    BrokerOperation.FS_MKDIR,
    BrokerOperation.FS_COPY,
    BrokerOperation.FS_MOVE,
    BrokerOperation.FS_REMOVE,
    BrokerOperation.JOB_START,
    BrokerOperation.JOB_CANCEL,
}


class BrokerService:
    def __init__(
        self,
        adapter: RuntimeAdapter,
        journal: RequestJournal | None = None,
        runtime_generation: int = 0,
        state_store: RuntimeStateStore | None = None,
        resource_policy: ResourcePolicy | None = None,
    ) -> None:
        self.adapter = adapter
        self.journal = journal or RequestJournal()
        self.runtime_generation = runtime_generation
        self.state_store = state_store or RuntimeStateStore()
        self.resource_policy = resource_policy or ResourcePolicy()
        self._runtime_ready = False
        self._readiness: dict[str, Any] = {"status": "degraded", "stage": "not_started"}
        self._last_failure: dict[str, Any] | None = None
        self._journal_lock = RLock()
        self._reconcile_task: asyncio.Task[None] | None = None

    async def _await(self, value: Any) -> Any:
        """Await SDK calls while permitting narrow synchronous fakes in unit tests."""
        return await value if inspect.isawaitable(value) else value

    @staticmethod
    def _safe_evidence(evidence: dict[str, Any]) -> dict[str, Any]:
        """Keep release-blocker evidence small and non-sensitive."""
        safe: dict[str, Any] = {}
        for key in {"mode", "reason", "requirement", "writable_paths", "expected_sha256", "actual_sha256", "exit_status", "guest_state"}:
            value = evidence.get(key)
            if isinstance(value, str):
                if key.endswith("_sha256") and len(value) != 64:
                    continue
                safe[key] = value[:160]
            elif isinstance(value, bool) or isinstance(value, int):
                safe[key] = value
            elif isinstance(value, list):
                safe[key] = [str(item)[:96] for item in value[:8] if isinstance(item, (str, int))]
        return safe

    @staticmethod
    def _safe_job_id(value: Any) -> str | None:
        try:
            parsed = UUID(str(value))
        except (ValueError, TypeError, AttributeError):
            return None
        return str(parsed)

    async def startup(self) -> None:
        self._readiness = {"status": "degraded", "stage": "probe"}
        try:
            probe = await self._await(self.adapter.readiness_probe())
        except Exception:
            observed = getattr(self.adapter, "last_readiness", None)
            if isinstance(observed, dict):
                self._readiness = {key: value for key, value in observed.items() if key in {"status", "stage", "reason", "cleanup"}}
            raise
        if not isinstance(probe, dict) or probe.get("status") != "ok":
            raise RuntimeError("workspace runtime readiness probe failed")
        self.runtime_generation = self.state_store.advance_runtime_generation()
        self._runtime_ready = True
        self._readiness = {"status": "ok", "stage": "reconcile"}
        # Expired leases are safe to reclaim. Jobs are never replayed: the
        # guest supervisor must prove each admitted job still exists.
        self.state_store.reclaim_expired_leases()
        # Do not hold the ASGI lifespan open while an existing failed
        # Workspace is being recovered.  A Microsandbox start can take longer
        # than the broker/container health deadline, and the durable row must
        # remain failed until the SDK proves that the owned runtime is running.
        self._schedule_reconciliation()

    def _schedule_reconciliation(self) -> None:
        task = self._reconcile_task
        if task is not None and not task.done():
            return
        self._reconcile_task = asyncio.create_task(
            self._run_reconciliation(), name="cognita-workspace-reconciliation"
        )

    async def _run_reconciliation(self) -> None:
        try:
            await self.reconcile()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - readiness must not die with diagnostics
            self._last_failure = {
                "stage": "reconcile",
                "category": "runtime_failure",
            }
            log.warning(
                "workspace runtime reconciliation failed",
                extra={"event": "workspace_reconcile_failure"},
            )
        else:
            self._readiness = {"status": "ok", "stage": "complete"}

    async def wait_for_reconciliation(self) -> None:
        """Wait for the current startup recovery task in tests and shutdown."""

        task = self._reconcile_task
        if task is not None:
            await task

    async def shutdown(self) -> None:
        """Cancel startup recovery before its durable stores are closed."""

        task = self._reconcile_task
        if task is None or task.done():
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    def restart(self) -> int:
        """Record a broker/runtime restart and require a fresh SDK probe."""
        self.runtime_generation = self.state_store.advance_runtime_generation()
        self._runtime_ready = False
        self._readiness = {"status": "degraded", "stage": "restart_required"}
        return self.runtime_generation

    async def reconcile(self) -> list[dict[str, Any]]:
        """Compare durable identities with labeled runtime observations.

        Unknown Microsandbox resources are intentionally never adopted or
        removed.  Startup records observations and leaves repair to the next
        admitted operation, which has the application's current validated
        configuration.  Uncertain jobs are reconciled separately and are
        never replayed.
        """
        if not self._runtime_ready:
            return []
        outcomes: list[dict[str, Any]] = []
        for record in self.state_store.workspaces():
            lease_owner: str | None = None
            try:
                # Serialize startup recovery with public lifecycle mutations.
                # A busy workspace is left untouched so its durable desired
                # state and in-flight operation remain authoritative.
                try:
                    lease_owner = self.state_store.acquire_lease(
                        record.workspace_id,
                        owner=f"startup-reconcile:{self.runtime_generation}",
                        ttl_seconds=900,
                    )
                except RuntimeError:
                    outcomes.append({"workspace_id": str(record.workspace_id), "state": "busy"})
                    continue
                observed = await self._await(self.adapter.inspect(record.workspace_id))
                state = observed.get("state") if isinstance(observed, dict) else None
                if state not in {"running", "stopped", "absent", "failed"}:
                    state = "failed"
                # Startup observes durable state only.  A failed VM stays
                # visible as failed until the user removes it or runs the reset
                # script (13.0 section 8); nothing replaces or recreates it.
                # (Superseded: through 12.x this comment said replacement
                # happened on the next admitted ``ensure`` call -- ``ensure``
                # no longer removes or recreates anything by itself.)
                # In particular, never start an old failed VM here: that SDK
                # call can stall and must not hold either health or admission.
                self.state_store.update_workspace(record.workspace_id, state=state,
                                                  runtime_generation=self.runtime_generation,
                                                  last_error_code=None if state != "failed" else "runtime_failure")
                outcomes.append({"workspace_id": str(record.workspace_id), "state": state})
            except Exception:  # noqa: BLE001 - diagnostics remain bounded
                self.state_store.update_workspace(record.workspace_id, state="failed",
                                                  last_error_code="runtime_failure")
                outcomes.append({"workspace_id": str(record.workspace_id), "state": "failed"})
            finally:
                if lease_owner is not None:
                    self.state_store.release_lease(record.workspace_id, lease_owner)
        await self._reconcile_jobs()
        return outcomes

    async def _reconcile_jobs(self) -> None:
        for job in self.state_store.active_jobs():
            try:
                observed = await self._await(self.adapter.execute(
                    job.workspace_id, BrokerOperation.JOB_GET.value,
                    {"job_id": str(job.job_id), "stdout_offset": 0,
                     "stderr_offset": 0, "max_bytes": 1},
                ))
                state = observed.get("state") if isinstance(observed, dict) else None
                if state not in {"queued", "running", "succeeded", "failed", "canceled", "timed_out", "lost"}:
                    state = "lost"
            except Exception:  # noqa: BLE001 - never replay an unprovable job
                state = "lost"
            self.state_store.update_job(job.job_id, state=state)
            log.info("workspace job reconciled", extra={"event": "job_reconcile", "state": state})

    @property
    def runtime_ready(self) -> bool:
        return self._runtime_ready

    def health(self) -> dict[str, Any]:
        info = getattr(self.adapter, "runtime_info", None)
        return {
            "status": "ok" if self._runtime_ready else "degraded",
            "protocol": "v1",
            "runtime": "ready" if self._runtime_ready else "unavailable",
            "sdk_version": getattr(info, "sdk_version", None),
            "readiness": dict(self._readiness),
        }

    def diagnostics(self, workspace_id: UUID) -> dict[str, Any]:
        """Return bounded broker-owned diagnostics without requiring SDK uptime."""
        try:
            record = self.state_store.workspace(workspace_id)
        except KeyError:
            return {"workspace_id": str(workspace_id), "status": "not_found"}
        result: dict[str, Any] = {
            "workspace_id": str(workspace_id),
            "state": record.state,
            "desired_state": record.desired_state,
            "runtime_generation": record.runtime_generation,
            "last_error_code": record.last_error_code,
            "runtime_available": self._runtime_ready,
        }
        if self._last_failure and self._last_failure.get("workspace_id") == str(workspace_id):
            result["last_failure"] = dict(self._last_failure)
        return result

    async def handle(self, request: RpcRequest) -> RpcSuccess | RpcFailure:
        wire = request.model_dump(mode="json")
        digest = request_digest(wire)
        if request.operation in MUTATING_OPERATIONS:
            with self._journal_lock:
                # Serialize the journal lookup, side effect, and durable result.
                # Without one critical section, two simultaneous deliveries of
                # the same request_id can both execute before either inserts.
                prior = self.journal.get(request.request_id)
                if prior is not None:
                    if prior.digest != digest:
                        return RpcFailure(
                            request_id=request.request_id,
                            code=ErrorCode.INVALID_REQUEST,
                            retryable=False,
                            message="request_id was reused with different content",
                        )
                    if prior.response == {"_pending": True}:
                        return RpcFailure(
                            request_id=request.request_id,
                            code=ErrorCode.RUNTIME_FAILURE,
                            retryable=True,
                            message="request outcome requires runtime reconciliation",
                        )
                    try:
                        if "code" in prior.response:
                            return RpcFailure.model_validate(prior.response)
                        return RpcSuccess.model_validate(prior.response)
                    except ValidationError:
                        return RpcFailure(
                            request_id=request.request_id,
                            code=ErrorCode.RUNTIME_FAILURE,
                            retryable=False,
                            message="stored request result is invalid",
                        )
                # The pending reservation is committed before handing the
                # mutation to the runtime. A crash may require reconciliation,
                # but an uncertain retry can never blindly repeat the effect.
                self.journal.reserve(request.request_id, digest)
                result = await self._execute(request)
                self.journal.complete(request.request_id, result.model_dump(mode="json", exclude_none=True))
                return result

        return await self._execute(request)

    async def _execute(self, request: RpcRequest) -> RpcSuccess | RpcFailure:
        """Execute one validated request and normalize every upstream failure."""

        if request.expected_runtime_generation is not None and (
            request.expected_runtime_generation != self.runtime_generation
        ):
            return RpcFailure(
                request_id=request.request_id,
                code=ErrorCode.GENERATION_CONFLICT,
                retryable=True,
                message="runtime generation does not match",
            )
        if not self._runtime_ready:
            return RpcFailure(
                request_id=request.request_id,
                code=ErrorCode.RUNTIME_FAILURE,
                retryable=True,
                message="runtime is unavailable",
            )

        try:
            arguments = normalize_rpc_arguments(request.operation, request.arguments, self.resource_policy)
        except ArgumentError:
            return RpcFailure(
                request_id=request.request_id,
                code=ErrorCode.INVALID_REQUEST,
                retryable=False,
                message="operation arguments are invalid",
            )

        workspace_id = request.workspace_id
        # Keep only broker-owned metadata.  The upstream adapter still owns
        # the guest filesystem and persistent runtime state.
        try:
            self.state_store.ensure_workspace(workspace_id, quota_bytes=self.resource_policy.quota_bytes)
            lease_owner = self.state_store.acquire_lease(workspace_id, ttl_seconds=60)
        except RuntimeError:
            return RpcFailure(
                request_id=request.request_id,
                code=ErrorCode.BUSY,
                retryable=True,
                message="workspace operation is busy",
            )

        result: RpcSuccess | RpcFailure | None = None
        try:
            active = self.state_store.active_job(workspace_id)
            if request.operation is BrokerOperation.JOB_START:
                if active is not None:
                    return RpcFailure(
                        request_id=request.request_id,
                        code=ErrorCode.BUSY,
                        retryable=True,
                        message="workspace already has a running job",
                    )
            elif request.operation in {BrokerOperation.STOP, BrokerOperation.REMOVE} and active is not None:
                # Detached guest jobs must settle before lifecycle teardown;
                # otherwise a stopped/removed sandbox can leave an uncertain
                # process writing durable output.
                try:
                    settled = await self._await(self.adapter.execute(
                        workspace_id, BrokerOperation.JOB_CANCEL.value,
                        {"job_id": str(active.job_id)},
                    ))
                    terminal = settled.get("state") if isinstance(settled, dict) else None
                    if terminal not in {"succeeded", "failed", "canceled", "timed_out", "lost"}:
                        raise RuntimeError("job did not settle")
                    self.state_store.update_job(active.job_id, state=terminal)
                    log.info("workspace job settled for lifecycle", extra={
                        "event": "job_settle", "operation": request.operation.value,
                        "state": terminal,
                    })
                except Exception as exc:
                    log.warning("workspace job settlement failed", extra={
                        "event": "job_settle_failure", "operation": request.operation.value,
                        "category": type(exc).__name__.casefold(),
                    })
                    return RpcFailure(
                        request_id=request.request_id, code=ErrorCode.BUSY,
                        retryable=True, message="workspace job is still running",
                    )
            elif request.operation in {
                BrokerOperation.FS_WRITE,
                BrokerOperation.FS_EDIT,
                BrokerOperation.FS_MKDIR,
                BrokerOperation.FS_COPY,
                BrokerOperation.FS_MOVE,
                BrokerOperation.FS_REMOVE,
            } and active is not None:
                return RpcFailure(
                    request_id=request.request_id,
                    code=ErrorCode.BUSY,
                    retryable=True,
                    message="workspace has a running job",
                )
            if request.operation in {BrokerOperation.JOB_GET, BrokerOperation.JOB_CANCEL}:
                try:
                    job = self.state_store.job(UUID(str(arguments["job_id"])))
                except (KeyError, TypeError, ValueError) as exc:
                    raise FileNotFoundError from exc
                if job.workspace_id != workspace_id:
                    raise FileNotFoundError
            if request.operation is BrokerOperation.ENSURE:
                # Both layers must agree this is first creation.  A lost
                # broker database must not let an existing app Workspace
                # receive an empty replacement volume.
                ensure_arguments = dict(arguments)
                ensure_arguments["_allow_volume_create"] = bool(
                    arguments["create_volume_if_absent"]
                    and self.state_store.claim_initial_volume_creation(workspace_id)
                )
                with operation_context(str(request.request_id), str(workspace_id)):
                    data = await self._await(self.adapter.ensure(workspace_id, ensure_arguments))
            elif request.operation is BrokerOperation.INSPECT:
                with operation_context(str(request.request_id), str(workspace_id)):
                    data = await self._await(self.adapter.inspect(workspace_id))
            elif request.operation is BrokerOperation.START:
                with operation_context(str(request.request_id), str(workspace_id)):
                    data = await self._await(self.adapter.start(workspace_id))
            elif request.operation is BrokerOperation.STOP:
                with operation_context(str(request.request_id), str(workspace_id)):
                    data = await self._await(self.adapter.stop(workspace_id, force=bool(arguments.get("force", False))))
            elif request.operation is BrokerOperation.REMOVE:
                with operation_context(str(request.request_id), str(workspace_id)):
                    data = await self._await(self.adapter.remove(workspace_id))
            else:
                with operation_context(str(request.request_id), str(workspace_id)):
                    data = await self._await(self.adapter.execute(workspace_id, request.operation.value, arguments))
            if not isinstance(data, dict):
                raise TypeError("adapter result is not an object")
            encoded = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            if len(encoded) > MAX_RESPONSE_BYTES:
                return RpcFailure(
                    request_id=request.request_id,
                    code=ErrorCode.RUNTIME_FAILURE,
                    retryable=False,
                    message="runtime response exceeded broker limit",
                )
            result: RpcSuccess | RpcFailure = RpcSuccess(
                request_id=request.request_id,
                runtime_generation=self.runtime_generation,
                data=data,
            )
            self._record_lifecycle(request, data)
            if request.operation is BrokerOperation.JOB_START:
                self._record_job_start(workspace_id, arguments, data)
            elif request.operation in {BrokerOperation.JOB_GET, BrokerOperation.JOB_CANCEL}:
                self._record_job_result(request, data)
            if request.operation in {BrokerOperation.JOB_START, BrokerOperation.JOB_CANCEL}:
                log.info("workspace job transition", extra={"event": request.operation.value, "state": data.get("state", "unknown")})
        except TimeoutError:
            self._last_failure = {
                "workspace_id": str(workspace_id), "stage": request.operation.value,
                "category": "timeout", "correlation_id": str(request.request_id),
            }
            result = RpcFailure(
                request_id=request.request_id,
                code=ErrorCode.TIMEOUT,
                retryable=True,
                message="runtime operation timed out",
                stage=request.operation.value,
                category="timeout",
                correlation_id=request.request_id,
            )
        except FileNotFoundError:
            result = RpcFailure(
                request_id=request.request_id,
                code=ErrorCode.NOT_FOUND,
                retryable=False,
                message="runtime resource was not found",
                stage=request.operation.value,
                category="not_found",
                correlation_id=request.request_id,
            )
        except NotImplementedError:
            result = RpcFailure(
                request_id=request.request_id,
                code=ErrorCode.UNSUPPORTED,
                retryable=False,
                message="operation is not supported by the pinned runtime",
                stage=request.operation.value,
                category="unsupported",
                correlation_id=request.request_id,
            )
        except SdkOperationError as exc:
            mapping = {
                "not_found": ErrorCode.NOT_FOUND,
                "busy": ErrorCode.BUSY,
                "conflict": ErrorCode.CONFLICT,
                "quota": ErrorCode.QUOTA,
                "timeout": ErrorCode.TIMEOUT,
                "unsupported": ErrorCode.UNSUPPORTED,
                "runtime_failure": ErrorCode.RUNTIME_FAILURE,
            }
            code = mapping.get(exc.category, ErrorCode.RUNTIME_FAILURE)
            self._last_failure = {
                "workspace_id": str(workspace_id),
                "stage": exc.stage,
                "category": exc.category,
                "correlation_id": str(request.request_id),
            }
            if exc.evidence:
                self._last_failure["diagnostics"] = self._safe_evidence(exc.evidence)
            if request.operation is BrokerOperation.JOB_CANCEL:
                safe_job_id = self._safe_job_id(arguments.get("job_id"))
                log.warning(
                    "workspace job cancel failed",
                    extra={
                        "event": "workspace_job_cancel_failure",
                        "operation": request.operation.value,
                        "stage": exc.stage[:64], "category": exc.category,
                        "correlation_id": str(request.request_id),
                        "workspace_id": str(workspace_id),
                        **({"job_id": safe_job_id} if safe_job_id is not None else {}),
                        **self._safe_evidence(exc.evidence),
                    },
                )
            result = RpcFailure(
                request_id=request.request_id, code=code,
                retryable=bool(exc.retryable),
                message={
                    ErrorCode.NOT_FOUND: "runtime resource was not found",
                    ErrorCode.BUSY: "workspace operation is busy",
                    ErrorCode.CONFLICT: "Workspace path or hash changed",
                    ErrorCode.QUOTA: "Workspace quota was exceeded",
                    ErrorCode.TIMEOUT: "runtime operation timed out",
                    ErrorCode.UNSUPPORTED: "operation is not supported by the pinned runtime",
                }.get(code, "runtime operation failed"),
                stage=exc.stage,
                category=exc.category,
                correlation_id=request.request_id,
                diagnostics=(
                    {
                        **({"ownership_verified": True} if exc.ownership_verified else {}),
                        **self._safe_evidence(exc.evidence),
                    } or None
                ),
            )
        except Exception as exc:  # noqa: BLE001 - SDK error text never crosses the boundary
            # Deliberately do not log exception text.  Upstream diagnostics may
            # contain guest paths, commands, or file content.
            name = type(exc).__name__.casefold()
            if "timeout" in name:
                code, retryable, message = ErrorCode.TIMEOUT, True, "runtime operation timed out"
            elif "notfound" in name or "not_found" in name:
                code, retryable, message = ErrorCode.NOT_FOUND, False, "runtime resource was not found"
            elif "busy" in name or "alreadyrunning" in name:
                code, retryable, message = ErrorCode.BUSY, True, "workspace operation is busy"
            elif "quota" in name or "space" in name:
                code, retryable, message = ErrorCode.QUOTA, False, "Workspace quota was exceeded"
            elif "unsupported" in name or "notimplemented" in name:
                code, retryable, message = ErrorCode.UNSUPPORTED, False, "operation is not supported by the pinned runtime"
            else:
                code, retryable, message = ErrorCode.RUNTIME_FAILURE, True, "runtime operation failed"
            log.warning(
                "workspace runtime operation failed",
                extra={
                    "event": "runtime_failure", "operation": request.operation.value,
                    "error_category": code.value, "stage": request.operation.value,
                    "category": code.value, "correlation_id": str(request.request_id),
                    "workspace_id": str(workspace_id),
                    **({"job_id": self._safe_job_id(arguments.get("job_id"))} if request.operation in {BrokerOperation.JOB_GET, BrokerOperation.JOB_CANCEL} and self._safe_job_id(arguments.get("job_id")) is not None else {}),
                },
            )
            self._last_failure = {
                "workspace_id": str(workspace_id), "stage": request.operation.value,
                "category": code.value, "correlation_id": str(request.request_id),
            }
            result = RpcFailure(
                request_id=request.request_id,
                code=code,
                retryable=retryable,
                message=message,
                stage=request.operation.value,
                category=code.value,
                correlation_id=request.request_id,
            )

        finally:
            if isinstance(result, RpcFailure) and result.code in {
                ErrorCode.RUNTIME_FAILURE, ErrorCode.TIMEOUT, ErrorCode.QUOTA,
                ErrorCode.UNSUPPORTED,
            }:
                # Preserve the failed row and its bounded category. The broker
                # never replaces or deletes an existing runtime object on error.
                try:
                    self.state_store.update_workspace(
                        workspace_id, state="failed",
                        last_error_code=(result.category or result.code.value),
                    )
                except (KeyError, RuntimeError):
                    pass
            try:
                self.state_store.release_lease(workspace_id, lease_owner)
            except (KeyError, RuntimeError):
                log.warning("workspace lease release failed", extra={"operation": request.operation.value})

        return result

    def _record_lifecycle(self, request: RpcRequest, data: dict[str, Any]) -> None:
        """Project only normalized lifecycle intent into the durable store."""
        state = {
            BrokerOperation.ENSURE: ("running", "running"),
            BrokerOperation.START: ("running", "running"),
            BrokerOperation.STOP: ("stopped", "stopped"),
            BrokerOperation.REMOVE: ("absent", "absent"),
        }.get(request.operation)
        if state is None:
            return
        observed_generation = data.get("runtime_generation", self.runtime_generation)
        if not isinstance(observed_generation, int) or observed_generation < 0:
            observed_generation = self.runtime_generation
        self.state_store.update_workspace(
            request.workspace_id, state=state[0], desired_state=state[1],
            runtime_generation=observed_generation,
        )

    def _record_job_start(self, workspace_id: UUID, arguments: dict[str, Any], data: dict[str, Any]) -> None:
        raw_job_id = data.get("job_id")
        try:
            job_id = UUID(str(raw_job_id))
        except (ValueError, TypeError):
            # A pinned runtime must return a durable job UUID.  Keep the
            # adapter result visible for diagnostics but do not invent one.
            return
        # Replace the broker-generated placeholder only when the upstream
        # supervisor provided an actual UUID.  This compact metadata is safe
        # to retain and deliberately excludes argv and environment values.
        existing = self.state_store.active_job(workspace_id)
        if existing is None:
            record = self.state_store.create_job(
                workspace_id, job_id=job_id,
                deadline=__import__("time").time() + arguments["timeout_seconds"],
                metadata={"job_id": str(job_id), "argv_count": len(arguments.get("argv", [])),
                          "request_digest": request_digest(arguments)},
            )
            state = data.get("state", "running")
            if state not in {"queued", "running"}:
                state = "running"
            identity: dict[str, Any] = {"state": state}
            if isinstance(data.get("pid"), int) and not isinstance(data.get("pid"), bool) and data["pid"] > 0:
                identity["pid"] = data["pid"]
            token = data.get("process_start_token")
            if isinstance(token, str) and 0 < len(token) <= 128:
                identity["process_start_token"] = token
            group = data.get("process_group_id")
            if isinstance(group, int) and not isinstance(group, bool) and group > 0:
                identity["process_group_id"] = group
            self.state_store.update_job(record.job_id, **identity)

    def _record_job_result(self, request: RpcRequest, data: dict[str, Any]) -> None:
        raw_job_id = request.arguments.get("job_id")
        try:
            job_id = UUID(str(raw_job_id))
            state = data.get("state")
        except (ValueError, TypeError):
            return
        if state in {"succeeded", "failed", "canceled", "timed_out", "lost"}:
            try:
                self.state_store.update_job(job_id, state=state)
            except KeyError:
                pass
