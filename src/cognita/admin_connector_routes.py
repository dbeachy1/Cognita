"""Combined connector policy routes for the Admin application.

The app factory retains authentication, CSRF, and the shared high-trust guard.
This router receives those checks and the existing policy stores explicitly.
"""

from __future__ import annotations

import logging
from typing import Callable

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from .config import CognitaConfig
from .connectors import (
    PUBLIC_CONTRACT_VERSION,
    ConnectorDefinition,
    ConnectorNotFound,
    ConnectorPolicyError,
    ConnectorStore,
    PolicyUnavailable,
    RevisionConflict,
    build_route_url,
)
from .public_url import PublicBaseURLStore
from .registry import Registry

log = logging.getLogger("cognita.admin")

_CONNECTOR_FIELDS = frozenset(
    {
        "expected_revision", "name", "enabled", "project_mode", "default_access",
        "project_access", "workspace_enabled", "default_workspace_transfer",
        "project_transfer", "confirm_high_trust",
    }
)

_WORKSPACE_TRANSFER_POLICIES = frozenset({"allow", "deny"})
_PROJECT_TRANSFER_POLICIES = frozenset({"inherit", "allow", "deny"})


def _connector_input(body: dict, *, partial: bool = False) -> tuple[int, dict]:
    if not isinstance(body, dict):
        raise ConnectorPolicyError("request body must be a JSON object")
    unknown = sorted(set(body) - _CONNECTOR_FIELDS)
    if unknown:
        raise ConnectorPolicyError(f"unknown connector field(s): {', '.join(unknown)}")
    revision = body.get("expected_revision")
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
        raise ConnectorPolicyError("expected_revision must be a nonnegative integer")
    values = {
        key: body[key] for key in _CONNECTOR_FIELDS - {"expected_revision", "confirm_high_trust"}
        if key in body
    }
    workspace_enabled = values.get("workspace_enabled")
    transfer = values.get("default_workspace_transfer")
    project_transfer = values.get("project_transfer")
    if workspace_enabled is not None and not isinstance(workspace_enabled, bool):
        raise ConnectorPolicyError("workspace_enabled must be a boolean")
    if transfer is not None and transfer not in _WORKSPACE_TRANSFER_POLICIES:
        raise ConnectorPolicyError("default_workspace_transfer must be allow or deny")
    if project_transfer is not None:
        if not isinstance(project_transfer, dict):
            raise ConnectorPolicyError("project_transfer must be an object")
        if any(value not in _PROJECT_TRANSFER_POLICIES for value in project_transfer.values()):
            raise ConnectorPolicyError("project_transfer values must be inherit, allow, or deny")
    # Since 13.0.1 (2026-09-22), Workspace access and transfer no longer
    # require confirm_high_trust. Accept its boolean value for older clients;
    # enabling Workspace through Admin still requires a configured password.
    if "confirm_high_trust" in body and not isinstance(body["confirm_high_trust"], bool):
        raise ConnectorPolicyError("confirm_high_trust must be a boolean")
    if not partial:
        if "name" not in values:
            raise ConnectorPolicyError("missing connector field(s): name")
        values.setdefault("enabled", True)
        values.setdefault("project_mode", "all")
        values.setdefault("project_access", {})
        # Selected mode has no default; all mode defaults to writable, matching
        # the primary connector migration contract.
        values.setdefault("default_access", None if values["project_mode"] == "selected" else "write")
    return revision, values


def _connector_view(
    config: CognitaConfig, connector: ConnectorDefinition, *, workspace_configured: bool = True
) -> dict:
    out = connector.model_dump(mode="json")
    out["workspace_policy_supported"] = "workspace_enabled" in getattr(type(connector), "model_fields", {})
    out["contract_version"] = PUBLIC_CONTRACT_VERSION
    # Stable and immutable-current URLs are distinct OAuth resources serving
    # the same current catalog. Retired generations are never advertised.
    out["path"] = build_route_url("", "combined", connector.slug)
    out["stable_url"] = build_route_url(config.public_base_url, "combined", connector.slug)
    out["current_url"] = build_route_url(
        config.public_base_url, "combined", connector.slug, PUBLIC_CONTRACT_VERSION
    )
    out["url"] = out["stable_url"]
    # Keep the Admin response explicit for older records that lack the
    # Workspace policy fields; migration defaults deny Workspace access.
    out.setdefault("workspace_enabled", False)
    out.setdefault("default_workspace_transfer", "deny")
    out.setdefault("project_transfer", {})
    out["workspace_requested"] = bool(out["workspace_enabled"])
    out["workspace_effective"] = bool(out["workspace_enabled"] and workspace_configured)
    out["workspace_reason"] = (
        None if out["workspace_effective"] or not out["workspace_requested"]
        else "host_workspace_disabled"
    )
    return out


def _connector_workspace_fields(body: dict) -> bool:
    return any(key in body for key in ("workspace_enabled", "default_workspace_transfer", "project_transfer"))


def create_connector_router(
    config: CognitaConfig,
    registry: Registry,
    connector_store: ConnectorStore,
    public_url_store: PublicBaseURLStore,
    csrf_error: Callable[[CognitaConfig, Request], JSONResponse | None],
    high_trust_error: Callable[[], JSONResponse | None],
    connector_error: Callable[[int, str, str], JSONResponse],
    *, workspace_configured: bool = True,
) -> APIRouter:
    """Bind connector CRUD to the app's existing policy and security checks."""
    router = APIRouter()

    @router.get("/api/connectors")
    async def list_connectors() -> JSONResponse:
        try:
            config.public_base_url = public_url_store.effective()
            current = connector_store.snapshot(registry.projects)
        except PolicyUnavailable as exc:
            log.error("Connector list unavailable: %s", type(exc).__name__)
            return connector_error(503, "policy_unavailable", "Connector policy is unavailable")
        return JSONResponse(
            {"revision": current.revision, "connectors": [
                _connector_view(config, c, workspace_configured=workspace_configured)
                for c in current.connectors
            ]}
        )

    @router.post("/api/connectors", status_code=201)
    async def add_connector(body: dict, request: Request) -> JSONResponse:
        if failure := csrf_error(config, request):
            return failure
        try:
            expected, values = _connector_input(body)
            if _connector_workspace_fields(body) and any(
                values.get(key) in (True, "allow") for key in ("workspace_enabled", "default_workspace_transfer")
            ):
                if failure := high_trust_error():
                    return failure
            current = connector_store.create(
                expected_revision=expected,
                project_names=(p.name for p in registry.projects),
                **values,
            )
            created = current.connectors[-1]
        except RevisionConflict as exc:
            return connector_error(409, "revision_conflict", str(exc))
        except PolicyUnavailable:
            return connector_error(503, "policy_unavailable", "Connector policy is unavailable")
        except (ConnectorPolicyError, ValidationError, ValueError) as exc:
            return connector_error(400, "invalid_connector", str(exc))
        log.info("connector created connector_id=%s revision=%d", created.id, current.revision)
        return JSONResponse({"revision": current.revision, "connector": _connector_view(
            config, created, workspace_configured=workspace_configured,
        )}, status_code=201)

    @router.patch("/api/connectors/{connector_id}")
    async def edit_connector(connector_id: str, body: dict, request: Request) -> JSONResponse:
        if failure := csrf_error(config, request):
            return failure
        try:
            expected, values = _connector_input(body, partial=True)
            if _connector_workspace_fields(body) and any(
                values.get(key) in (True, "allow") for key in ("workspace_enabled", "default_workspace_transfer")
            ):
                if failure := high_trust_error():
                    return failure
            current = connector_store.update(
                connector_id,
                expected_revision=expected,
                project_names=(p.name for p in registry.projects),
                **values,
            )
            updated = next(c for c in current.connectors if c.id.casefold() == connector_id.casefold())
        except RevisionConflict as exc:
            return connector_error(409, "revision_conflict", str(exc))
        except ConnectorNotFound:
            return connector_error(404, "connector_not_found", "Connector was not found")
        except PolicyUnavailable:
            return connector_error(503, "policy_unavailable", "Connector policy is unavailable")
        except (ConnectorPolicyError, ValidationError, ValueError) as exc:
            return connector_error(400, "invalid_connector", str(exc))
        log.info("connector updated connector_id=%s revision=%d", updated.id, current.revision)
        return JSONResponse({"revision": current.revision, "connector": _connector_view(
            config, updated, workspace_configured=workspace_configured,
        )})

    @router.delete("/api/connectors/{connector_id}")
    async def remove_connector(connector_id: str, request: Request, expected_revision: str | None = Query(None)) -> JSONResponse:
        if failure := csrf_error(config, request):
            return failure
        # DELETE clients commonly use a query parameter; accept a JSON body too
        # for parity with POST/PATCH without making GET or DELETE side-effectful.
        if expected_revision is None:
            try:
                payload = await request.json()
            except (ValueError, TypeError):
                payload = {}
            expected_revision = payload.get("expected_revision") if isinstance(payload, dict) else None
        try:
            parsed_revision = int(expected_revision)
        except (TypeError, ValueError):
            parsed_revision = -1
        if isinstance(expected_revision, bool) or parsed_revision < 0 or str(parsed_revision) != str(expected_revision):
            return connector_error(400, "invalid_connector", "expected_revision must be a nonnegative integer")
        try:
            current = connector_store.delete(connector_id, expected_revision=parsed_revision)
        except RevisionConflict as exc:
            return connector_error(409, "revision_conflict", str(exc))
        except ConnectorNotFound:
            return connector_error(404, "connector_not_found", "Connector was not found")
        except PolicyUnavailable:
            return connector_error(503, "policy_unavailable", "Connector policy is unavailable")
        except ConnectorPolicyError as exc:
            return connector_error(400, "invalid_connector", str(exc))
        log.info("connector deleted connector_id=%s revision=%d", connector_id, current.revision)
        return JSONResponse({"deleted": connector_id, "revision": current.revision})

    return router
