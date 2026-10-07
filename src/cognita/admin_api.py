"""Admin API + UI app — localhost only (DESIGN.md §7.2, §8).

Full project lifecycle from the browser: list, add, OAuth connection management,
optional show-once API keys, reindex, status, and remove.

Defaults to a 127.0.0.1 bind. To reach it from another machine, bind it to a
non-loopback address and set an admin password — a form login + signed session
cookie is then enforced on every route (see admin_auth.py). Binding non-loopback
without a password is refused at startup (DESIGN.md §6/§8).
"""

from __future__ import annotations

import asyncio
import inspect
import dataclasses
import logging
import os
import re
import secrets
import shutil
import time
from enum import StrEnum
from pathlib import Path
from typing import Any
from uuid import UUID

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, StrictBool, ValidationError
from starlette.middleware.trustedhost import TrustedHostMiddleware

from . import __version__
from .acceleration import (
    AccelerationConfigurationError,
    AccelerationConflict,
    AccelerationStore,
    acceleration_path,
)
from .acceleration_profiles import current_profile
from .admin_auth import (
    SESSION_COOKIE,
    admin_auth_configured,
    allowed_admin_hosts,
    is_loopback,
    issue_session_token,
    read_session_user,
    revoke_all_sessions,
    session_cookie_kwargs,
    verify_login,
)
from .auth_policy import (
    AuthenticationLockoutConfirmationRequired,
    AuthenticationPolicyStore,
    AuthenticationPolicyUnavailable,
    AuthenticationRevisionConflict,
)
from .admin_connector_routes import create_connector_router
from .config import CognitaConfig
# 14.1.0 (installer design 19.2): the documents roots and their display text live in one module.
# `configured_document_roots` moved there from this file.
from .document_roots import configured_document_roots, display_for, roots_with_displays
from .localization import SUPPORTED_LOCALES, resolve_locale
from .connectors import (
    WORKSPACE_CONTRACT_VERSION,
    ConnectorPolicyError,
    ConnectorStore,
    PolicyUnavailable,
)
from .oauth_service_client import OAuthServiceClient, OAuthServiceUnavailable
from .public_url import PublicBaseURLStore, PublicURLValidationError
from .registry import Project, Registry
from .workspace_admin import admin_payload, preview_view, settings_view, status_view, workspace_view

log = logging.getLogger("cognita.admin")


class WorkerStatus(StrEnum):
    """Project serving status as the Admin JSON reports it (`worker_status`).

    14.0.0: this enum used to live in workers.py beside the 3.x per-project
    worker processes. Those are gone; the values stay because they are the wire
    values the Admin UI reads. In core mode a project is RUNNING when it is
    enabled and STOPPED otherwise; STARTING and ERROR are kept verbatim so the
    JSON vocabulary does not shrink under a client that still compares against it.
    """

    STOPPED = "stopped"
    STARTING = "starting"
    RUNNING = "running"
    ERROR = "error"

# Login throttling. The stored verifier is unsalted single-round SHA-256 (the
# documented design), so an online guessing loop is cheap for the attacker — and
# /api/login had no counter, delay or lockout of any kind. In-memory and per
# source address: it does not survive a restart and a distributed attacker can
# spread across addresses, but it turns an unbounded dictionary run against
# admin/<word> into a slow one, which is the whole of what a lockout buys.
LOGIN_MAX_FAILURES = 5
LOGIN_LOCKOUT_S = 60
_login_failures: dict[str, tuple[int, float]] = {}  # host -> (count, first_failure_at)


def _login_lockout_remaining(host: str) -> int:
    """Seconds the caller must wait, or 0 if it may attempt a login now."""
    count, first = _login_failures.get(host, (0, 0.0))
    if count < LOGIN_MAX_FAILURES:
        return 0
    remaining = LOGIN_LOCKOUT_S - (time.time() - first)
    if remaining <= 0:
        _login_failures.pop(host, None)  # window elapsed; start clean
        return 0
    return int(remaining) + 1


def _record_login_failure(host: str) -> None:
    count, first = _login_failures.get(host, (0, 0.0))
    if count >= LOGIN_MAX_FAILURES:
        return  # already locked; the window is fixed, not sliding
    _login_failures[host] = (count + 1, first or time.time())
    # Bound the map so a spray across forged addresses cannot grow it forever.
    if len(_login_failures) > 1024:
        cutoff = time.time() - LOGIN_LOCKOUT_S
        for key, (_c, started) in list(_login_failures.items()):
            if started < cutoff:
                _login_failures.pop(key, None)


def _clear_login_failures(host: str) -> None:
    _login_failures.pop(host, None)

WEB_DIR = Path(__file__).resolve().parent / "web"

# Persisted color-theme preference. The browser writes this cookie when the
# admin picks a theme; the server reads it and stamps data-theme on the served
# HTML so the right palette paints on the first byte (no flash before app.js
# runs). A cookie — not localStorage — because only cookies ride the request,
# which is what lets the server apply the choice server-side. Non-sensitive, so
# it stays readable by JS (not HttpOnly).
#
# No cookie means "follow the system": the page goes out with no data-theme at
# all, and Pico then paints from the browser's prefers-color-scheme. Before
# 14.2.5 no cookie meant light, so the first sign-in page on a dark desktop came
# up light.
THEME_COOKIE = "cognita_theme"
CSRF_COOKIE = "cognita_csrf"
_VALID_THEMES = frozenset({"light", "dark"})


def _theme_from(request: Request) -> str | None:
    """The persisted theme, or None (follow the system) when unset or unrecognized."""
    theme = request.cookies.get(THEME_COOKIE)
    return theme if theme in _VALID_THEMES else None


def _admin_locale(request: Request) -> str:
    """Resolve the host's saved Admin language, then its supported browser preference."""
    cookie = request.cookies.get("cognita_lang", "")
    if cookie in SUPPORTED_LOCALES:
        return cookie
    choices: list[tuple[float, int, str]] = []
    for index, part in enumerate(request.headers.get("Accept-Language", "").split(",")[:32]):
        fields = [field.strip() for field in part.split(";")]
        tag = fields[0].replace("_", "-").lower()
        quality = 1.0
        for field in fields[1:]:
            if field.startswith("q="):
                try:
                    quality = float(field[2:])
                except ValueError:
                    quality = 0.0
                break
        if not 0.0 < quality <= 1.0 or tag == "*":
            continue
        locale = next((item for item in SUPPORTED_LOCALES if item.lower() == tag), None)
        if locale is None and tag in {"es", "fr", "de", "it", "en"}:
            locale = resolve_locale(tag)
        if locale is not None:
            choices.append((quality, -index, locale))
    return max(choices)[2] if choices else "en-US"


def _render_page(filename: str, theme: str | None, locale: str = "en-US") -> HTMLResponse:
    """Serve a web/ page with the persisted theme and resolved language on <html>.

    The pages ship with data-theme="light"; we rewrite it to the chosen theme,
    or drop it when unset so the system theme applies. The language is resolved
    per request from the cookie and Accept-Language header. Rendered per-request
    (not FileResponse) because the output depends on request preferences.
    """
    html = (WEB_DIR / filename).read_text(encoding="utf-8")
    # The running package is the sole version authority.  Replacing tokens at
    # response time keeps the visible shell, login page, and asset cache keys
    # aligned after deployment without a browser-bundled version constant.
    html = html.replace("__COGNITA_VERSION__", __version__)
    html = re.sub(
        r'(<html\b[^>]*\blang=)["\'][^"\']*["\']',
        lambda match: f'{match.group(1)}"{locale}"',
        html,
        count=1,
    )
    if theme is None:
        html = html.replace(' data-theme="light"', "", 1)
    elif theme != "light":
        html = html.replace('data-theme="light"', f'data-theme="{theme}"', 1)
    # Admin and login HTML carries no reusable user data; prevent an
    # intermediary or browser from replaying an old shell after deployment.
    return HTMLResponse(
        html,
        headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
    )


class NewProject(BaseModel):
    name: str
    documents_dir: str
    writable: bool = True
    exclude_from_default_permissions: StrictBool = False


class ProjectSettingsUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    exclude_from_default_permissions: StrictBool


class DocumentsPathProbe(BaseModel):
    """Two forms: today's absolute ``documents_dir``, or (installer design 7.2) a ``root``
    from ``GET /api/document-roots`` plus a relative ``folder`` inside it."""

    documents_dir: str | None = None
    root: str | None = None
    folder: str = ""


class Login(BaseModel):
    username: str
    password: str


class DebugTokensModeUpdate(BaseModel):
    enabled: bool


_SURFACE_KINDS = frozenset({"combined", "workspace"})
_WORKSPACE_SLUG_RE = re.compile(r"\A[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")


def _as_mapping(value: Any) -> dict:
    """Return a non-secret, JSON-shaped view from a worker-owned record.

    The route layer deliberately accepts Pydantic models, dataclasses, and the
    small mapping views used by the policy workers. It never introspects or
    serializes a credential object directly; credential normalization below
    selects an allowlist of metadata fields.
    """
    if isinstance(value, dict):
        return dict(value)
    dumper = getattr(value, "model_dump", None)
    if callable(dumper):
        return dict(dumper(mode="json"))
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        # Slots dataclasses have no __dict__, so read declared fields directly.
        # _credential_metadata still controls which values leave this process.
        return {field.name: getattr(value, field.name) for field in dataclasses.fields(value)}
    return dict(getattr(value, "__dict__", {}))


def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return value
    return value


async def _await_if_needed(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


def _invoke(service: Any, method_names: tuple[str, ...], *args, **kwargs) -> Any:
    """Call the first named worker method; missing seams fail closed."""
    for name in method_names:
        method = getattr(service, name, None)
        if callable(method):
            return method(*args, **kwargs)
    raise RuntimeError(f"service is missing required method: {'/'.join(method_names)}")


def _surface_id(value: Any) -> str:
    row = _as_mapping(value)
    return str(row.get("id") or row.get("surface_id") or "")


def _credential_metadata(value: Any) -> dict:
    """Allowlist credential metadata so a list can never leak secret material."""
    row = _as_mapping(value)
    aliases = {
        "credential_id": row.get("credential_id", row.get("id")),
        "key_id": row.get("key_id"),
        "label": row.get("label", row.get("name", "")),
        "name": row.get("name", row.get("label", "")),
        "status": row.get("status", "active"),
        "enabled": row.get("enabled", row.get("status", "active") == "active"),
        "revoked": row.get("revoked", row.get("status") == "revoked"),
        "deleted": row.get("deleted", False),
        "created_at": row.get("created_at"),
        "rotated_at": row.get("rotated_at"),
        "revoked_at": row.get("revoked_at"),
        "last_used_at": row.get("last_used_at"),
        "workspace_id": row.get("workspace_id"),
        "revision": row.get("revision"),
    }
    return {key: value for key, value in aliases.items() if value is not None}


def _connector_error(status: int, reason: str, message: str) -> JSONResponse:
    """Stable machine-readable envelope for all connector Admin API failures."""
    return JSONResponse(
        status_code=status,
        content={"status": "error", "reason": reason, "message": message},
    )


def _documents_directory_stats(documents_dir: str) -> tuple[Path, int, int]:
    """Validate a documents root and count regular files without reading contents."""
    candidate = Path(documents_dir).expanduser()
    if not candidate.is_absolute():
        raise ValueError("Documents folder must be an absolute path")
    try:
        root = candidate.resolve(strict=True)
    except OSError as exc:
        # OS errors can include a child or component name. Keep the admin
        # response useful without turning this aggregate-only probe into a
        # filename disclosure surface.
        raise ValueError("Documents folder cannot be resolved") from exc
    if not root.is_dir():
        raise ValueError(f"Documents folder does not exist or is not a directory: {display_for(str(candidate))}")

    file_count = 0
    total_bytes = 0
    pending = [root]
    try:
        while pending:
            current = pending.pop()
            with os.scandir(current) as entries:
                for entry in entries:
                    if entry.is_dir(follow_symlinks=False):
                        pending.append(Path(entry.path))
                    elif entry.is_file(follow_symlinks=False):
                        file_count += 1
                        total_bytes += entry.stat(follow_symlinks=False).st_size
    except OSError as exc:
        raise ValueError("Documents folder cannot be read completely") from exc
    return root, file_count, total_bytes


def _probe_root_folder(root: str, folder: str) -> dict:
    """Probe ``folder`` inside a configured documents ``root`` (installer design 7.2).

    A request that is malformed or tries to leave the root raises ``ValueError`` (the route
    answers 400). A folder that is simply missing, a file, unreadable or read-only is a
    normal answer: HTTP 200 with ``readable``/``writable`` and a plain ``message`` the New
    Project form shows as it is.

    The write test creates ``.cognita-write-test-<16 hex>`` with O_CREAT|O_EXCL and removes
    it again. Both steps are logged with the path only, never any content.
    """
    roots = configured_document_roots()
    if root not in roots:
        raise ValueError("That documents root is not one of the folders Cognita was given.")
    if "\x00" in folder:
        raise ValueError("The folder name is not valid.")
    relative = Path(folder.strip().replace("\\", "/"))
    if relative.is_absolute() or folder.strip().startswith(("/", "\\")):
        raise ValueError("The folder must be written relative to the root, not as a full path.")
    if ".." in relative.parts:
        raise ValueError("The folder may not contain '..'.")
    root_path = Path(root).resolve()
    joined = (root_path / relative).resolve()
    try:
        joined.relative_to(root_path)
    except ValueError as exc:
        # A symlink inside the root pointing outside it lands here.
        raise ValueError("The folder is outside the documents root.") from exc

    result: dict = {
        "status": "ok", "path": str(joined), "file_count": 0, "total_bytes": 0,
        "exists": joined.exists(), "readable": False, "writable": False, "message": "",
        # 19.2: the folder as a person knows it (a Windows path on a WSL install); the path itself
        # when no root has a display. `path` stays the container path.
        "display": display_for(str(joined)),
    }
    if not joined.exists():
        result["message"] = "This folder does not exist. Create it first."
        result["presentation_id"] = "admin.folder.missing"
    elif not joined.is_dir():
        result["message"] = "This is a file, not a folder. Pick a folder."
        result["presentation_id"] = "admin.folder.file"
    else:
        try:
            os.listdir(joined)
            result["readable"] = True
        except OSError as exc:
            log.info("Documents folder probe: not readable path=%s reason=%s", joined, type(exc).__name__)
        if result["readable"]:
            try:
                _, result["file_count"], result["total_bytes"] = _documents_directory_stats(str(joined))
            except ValueError as exc:
                log.info("Documents folder probe: scan failed path=%s reason=%s", joined, exc)
                result["readable"] = False
        if result["readable"]:
            probe = joined / f".cognita-write-test-{secrets.token_hex(8)}"
            try:
                descriptor = os.open(probe, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                os.close(descriptor)
                log.info("Documents folder probe: created write-test file path=%s", probe)
                result["writable"] = True
            except OSError as exc:
                log.info("Documents folder probe: not writable path=%s reason=%s", joined, type(exc).__name__)
            if result["writable"]:
                try:
                    os.unlink(probe)
                    log.info("Documents folder probe: removed write-test file path=%s", probe)
                except OSError as exc:
                    log.warning("Documents folder probe: could not remove write-test file path=%s reason=%s: %s",
                                probe, type(exc).__name__, exc)
        if not result["readable"]:
            result["message"] = "Cognita cannot read this folder. Check who owns it and its permissions."
            result["presentation_id"] = "admin.folder.unreadable"
        elif result["writable"]:
            result["message"] = "Cognita can read and write this folder."
            result["presentation_id"] = "admin.folder.readwrite"
        else:
            result["message"] = ("Cognita can read this folder but cannot write to it. "
                                 "Editing documents through Cognita will fail.")
            result["presentation_id"] = "admin.folder.readonly"
    result["presentation_values"] = {"folder": result["display"]}
    log.info("Documents folder probe path=%s display=%s exists=%s readable=%s writable=%s files=%d",
             joined, result["display"], result["exists"], result["readable"], result["writable"],
             result["file_count"])
    return result


def _csrf_error(config: CognitaConfig, request: Request) -> JSONResponse | None:
    """Double-submit protection for authenticated Admin API mutations."""
    if not admin_auth_configured(config):
        return None
    cookie = request.cookies.get(CSRF_COOKIE)
    supplied = request.headers.get("X-CSRF-Token") or request.headers.get("X-CSRFToken")
    if not cookie or not supplied or not secrets.compare_digest(cookie, supplied):
        return _connector_error(403, "csrf_failed", "CSRF token is missing or invalid")
    return None


def create_admin_app(
    config: CognitaConfig, registry: Registry, engine=None,
    oauth_client: OAuthServiceClient | None = None,
    connector_store: ConnectorStore | None = None,
    authentication_store: AuthenticationPolicyStore | None = None,
    oauth_supervisor=None,
    workspace_connector_store: Any | None = None,
    credential_store: Any | None = None,
    workspace_service: Any | None = None,
    workspace_admin_service: Any | None = None,
    public_url_store: PublicBaseURLStore | None = None,
    acceleration_store: AccelerationStore | None = None,
) -> FastAPI:

    # ``workspace_admin_service`` is the descriptive alias used by deployment
    # wiring; retain the shorter name for callers that already construct the
    # Admin app directly.
    workspace_service = workspace_service or workspace_admin_service
    public_url_store = public_url_store or PublicBaseURLStore(config)
    # A directly constructed test/app config may not have gone through
    # load_config; make the effective URL consistent at app construction too.
    config.public_base_url = public_url_store.effective()
    connector_store = connector_store or ConnectorStore(config.connectors_path)
    legacy_config = Path(
        os.environ.get("COGNITA_CONFIG_PATH")
        or (acceleration_path(config).parent / "cognita.yaml")
    )
    acceleration_store = acceleration_store or AccelerationStore(
        acceleration_path(config), legacy_config_path=legacy_config
    )
    oauth_supervisor = oauth_supervisor or getattr(oauth_client, "supervisor", None)
    if authentication_store is not None:
        # Admin-created projects must use the same parent-owned store as the
        # gateway. Registry lifecycle hooks remove stale rows before add and
        # after delete, preventing a same-named project from inheriting access.
        registry.attach_authentication_store(authentication_store)

    def _project_names() -> list[str]:
        return [project.name for project in registry.projects]

    def _enabled_project_names() -> list[str]:
        return [project.name for project in registry.projects if project.enabled]

    def _auth_headers(response: JSONResponse) -> JSONResponse:
        response.headers["Cache-Control"] = "no-store"
        response.headers["Pragma"] = "no-cache"
        return response

    def _auth_json(payload: dict, status_code: int = 200) -> JSONResponse:
        return _auth_headers(JSONResponse(payload, status_code=status_code))

    def _auth_error(status: int, reason: str, message: str, **extra) -> JSONResponse:
        payload = {"status": "error", "reason": reason, "message": message}
        payload.update(extra)
        return _auth_json(payload, status)

    def _auth_mutation(body: object, *, project: bool = False) -> tuple[int, dict]:
        if not isinstance(body, dict):
            raise TypeError("request body must be a JSON object")
        allowed = {"expected_revision", "static_key_action", "confirm_lockout"}
        if project:
            allowed.add("oauth_mode")
        else:
            allowed.add("oauth_enabled")
        unknown = sorted(set(body) - allowed)
        if unknown:
            raise ValueError(f"unknown authentication field(s): {', '.join(unknown)}")
        expected = body.get("expected_revision")
        if isinstance(expected, bool) or not isinstance(expected, int) or expected < 0:
            raise ValueError("expected_revision must be a nonnegative integer")
        action = body.get("static_key_action", "unchanged")
        if action not in {"unchanged", "generate", "clear"}:
            raise ValueError("static_key_action must be unchanged, generate, or clear")
        if "confirm_lockout" in body and not isinstance(body["confirm_lockout"], bool):
            raise ValueError("confirm_lockout must be a boolean")
        values = {"expected_revision": expected, "static_key_action": action,
                  "confirm_lockout": bool(body.get("confirm_lockout", False))}
        if project:
            if "oauth_mode" in body and body["oauth_mode"] not in {"inherit", "enabled", "disabled"}:
                raise ValueError("oauth_mode must be inherit, enabled, or disabled")
            values["oauth_mode"] = body.get("oauth_mode")
        elif "oauth_enabled" in body:
            if not isinstance(body["oauth_enabled"], bool):
                raise ValueError("oauth_enabled must be a boolean")
            values["oauth_enabled"] = body["oauth_enabled"]
        return expected, values

    def _static_key_request(body: object, *, revoke: bool = False) -> int | tuple[int, bool]:
        """Parse the deliberately narrow dedicated static-key request models."""
        if not isinstance(body, dict):
            raise TypeError("request body must be a JSON object")
        expected_fields = {"expected_revision", "confirm_lockout"} if revoke else {"expected_revision"}
        if set(body) != expected_fields:
            missing = sorted(expected_fields - set(body))
            unknown = sorted(set(body) - expected_fields)
            detail = []
            if missing:
                detail.append(f"missing authentication field(s): {', '.join(missing)}")
            if unknown:
                detail.append(f"unknown authentication field(s): {', '.join(unknown)}")
            raise ValueError("; ".join(detail) or "invalid authentication request")
        expected = body["expected_revision"]
        if isinstance(expected, bool) or not isinstance(expected, int) or expected < 0:
            raise ValueError("expected_revision must be a nonnegative integer")
        if not revoke:
            return expected
        confirm = body["confirm_lockout"]
        if not isinstance(confirm, bool):
            raise ValueError("confirm_lockout must be a boolean")
        return expected, confirm

    def _policy_warnings(candidate, names: list[str]) -> dict:
        locked: list[str] = []
        for name in sorted(set(names)):
            row = candidate.projects.get(name)
            oauth = candidate.global_.oauth_enabled if row is None or row.oauth_mode == "inherit" else row.oauth_mode == "enabled"
            static = bool((row.static_key if row is not None else None) or candidate.global_.static_key)
            if not oauth and not static:
                locked.append(name)
        locked_set = set(locked)
        connectors: list[str] = []
        try:
            current = connector_store.snapshot(registry.projects)
            for connector in current.connectors:
                if not connector.enabled:
                    continue
                accessible = [item.project for item in connector_store.accessible_projects(connector.id, registry.projects)]
                if accessible and set(accessible).issubset(locked_set):
                    connectors.append(connector.id)
        except (ConnectorPolicyError, PolicyUnavailable, OSError, RuntimeError, TypeError, ValueError):
            # A connector-policy failure must not make an authentication preview
            # claim that there are no warnings. The mutation itself remains
            # governed by the authentication policy and connector gateway.
            log.warning("Authentication preview could not inspect connector access")
        return {"locked_out_projects": locked, "locked_out_connectors": sorted(connectors)}

    def _preview(body: dict, *, project_name: str | None = None) -> dict:
        if authentication_store is None:
            raise AuthenticationPolicyUnavailable("authentication policy is unavailable")
        is_project = project_name is not None
        expected, values = _auth_mutation(body, project=is_project)
        current = authentication_store.snapshot()
        if expected != current.revision:
            raise AuthenticationRevisionConflict(authentication_store.view(_project_names()))
        if is_project:
            if project_name not in _project_names():
                raise KeyError("unknown project")
            row = current.projects.get(project_name)
            from .auth_policy import ProjectAuthenticationPolicy
            row = row or ProjectAuthenticationPolicy()
            if values.get("oauth_mode") is not None:
                row.oauth_mode = values["oauth_mode"]
            if values["static_key_action"] == "clear":
                row.static_key = None
            elif values["static_key_action"] == "generate" and row.static_key is None:
                # A non-secret marker is sufficient for lockout analysis.
                row.static_key = object()  # type: ignore[assignment]
            if row.oauth_mode == "inherit" and row.static_key is None:
                current.projects.pop(project_name, None)
            else:
                current.projects[project_name] = row
        else:
            if "oauth_enabled" in values:
                current.global_.oauth_enabled = values["oauth_enabled"]
            if values["static_key_action"] == "clear":
                current.global_.static_key = None
            elif values["static_key_action"] == "generate" and current.global_.static_key is None:
                current.global_.static_key = object()  # type: ignore[assignment]
        warnings = _policy_warnings(current, _enabled_project_names())
        warnings["count"] = len(warnings["locked_out_projects"])
        return {"revision": expected, "warnings": warnings,
                "would_lock_out": bool(warnings["locked_out_projects"]),
                "affected_projects": warnings["locked_out_projects"],
                "affected_connectors": warnings["locked_out_connectors"]}

    def _static_revoke_warnings(project_name: str | None = None) -> dict:
        """Calculate the post-revocation lockout warning without persisting it."""
        if authentication_store is None:
            raise AuthenticationPolicyUnavailable("authentication policy is unavailable")
        candidate = authentication_store.snapshot()
        if project_name is None:
            candidate.global_.static_key = None
        else:
            row = candidate.projects.get(project_name)
            if row is not None and row.static_key is not None:
                row.static_key = None
                if row.oauth_mode == "inherit":
                    candidate.projects.pop(project_name, None)
                else:
                    candidate.projects[project_name] = row
        warnings = _policy_warnings(candidate, _enabled_project_names())
        warnings["count"] = len(warnings["locked_out_projects"])
        return warnings

    def _complete_authentication_view(payload: dict) -> dict:
        """Add the same bounded warning view returned by GET /api/authentication."""
        payload["warnings"] = _policy_warnings(
            authentication_store.snapshot(), _enabled_project_names()
        )
        return payload

    # App-level dependency gates EVERY route except the login surface itself
    # (the login page at "/", plus /api/login, /api/logout, /api/session) and the
    # mounted /static assets. A no-op when no password is set — but then the
    # startup guard has already ensured the bind is loopback-only. Static assets
    # (vendored CSS/JS + client app.js) carry no project data and only drive the
    # already-protected API, so they stay open (the login page needs the CSS).
    open_paths = {"/", "/api/login", "/api/logout", "/api/session", "/api/bootstrap"}

    trusted_hosts = allowed_admin_hosts(config)

    async def _gate(request: Request) -> None:
        if not admin_auth_configured(config):
            return  # open admin (loopback-only, per the startup guard)
        if request.url.path in open_paths:
            return
        if read_session_user(config, request.cookies.get(SESSION_COOKIE)):
            return
        raise HTTPException(status_code=401, detail="Not authenticated")

    app = FastAPI(
        title="Cognita Admin",
        version=__version__,
        docs_url=None,
        redoc_url=None,
        dependencies=[Depends(_gate)],
    )

    @app.middleware("http")
    async def no_store_admin_json(request: Request, call_next):
        """Prevent browser/proxy caches from retaining mutable Admin responses."""
        response = await call_next(request)
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
            response.headers["Pragma"] = "no-cache"
        # Sessions issued before CSRF protection was introduced remain valid.
        # Bootstrap the readable double-submit cookie on their next response so
        # a normal Admin reload can submit mutations without weakening the
        # equality check in _csrf_error.
        if (
            admin_auth_configured(config)
            and read_session_user(config, request.cookies.get(SESSION_COOKIE))
            and not request.cookies.get(CSRF_COOKIE)
        ):
            response.set_cookie(
                CSRF_COOKIE,
                secrets.token_urlsafe(32),
                httponly=False,
                samesite="strict",
                secure=bool(config.admin_tls_certfile and config.admin_tls_keyfile),
                path="/",
            )
        return response
    # Host validation (5.4). Nothing checked the Host header, so a page the admin
    # visited could rebind attacker.tld to this machine and the browser would
    # treat http://attacker.tld:8676/ as SAME-ORIGIN — reading responses from an
    # authenticated admin session. SameSite=Lax does not help: after rebinding the
    # request IS same-site.
    #
    # "*" is the documented escape hatch and means no check at all. Otherwise the
    # list is derived (see allowed_admin_hosts) from things that already know the
    # answer — chiefly the TLS certificate's own SANs — so it cannot lock the
    # operator out of a name the browser already had to trust.
    if "*" not in trusted_hosts:
        app.add_middleware(TrustedHostMiddleware, allowed_hosts=trusted_hosts)
        log.info("Admin Host header restricted to: %s", ", ".join(trusted_hosts))
    else:
        log.warning("Admin Host validation DISABLED (admin_allowed_hosts contains '*')")
    if admin_auth_configured(config):
        log.info(
            "Admin auth: form login + session cookie ENABLED (user %r)", config.admin_username
        )

    def _status_of(name: str) -> str:
        if engine is not None:
            # 4.0 core mode: no worker processes; an enabled project is served
            # in-process and is "running" the moment the gateway is up.
            project = registry.get(name)
            return WorkerStatus.RUNNING if project and project.enabled else WorkerStatus.STOPPED
        # No engine (an app built without one, as some tests do) serves nothing.
        return WorkerStatus.STOPPED

    async def _oauth_state() -> str:
        oauth_required = (
            authentication_store.oauth_runtime_required(_enabled_project_names())
            if authentication_store is not None else config.oauth_enabled
        )
        if not oauth_required:
            return "disabled"
        if oauth_client is None:
            return "unavailable"
        checker = getattr(oauth_client, "check_readiness", None)
        if checker is not None:
            try:
                return "ready" if await checker() else "unavailable"
            except (OSError, RuntimeError, TypeError, ValueError):
                return "unavailable"
        linked = getattr(oauth_client, "supervisor", None)
        if linked is None:
            return "ready"
        snapshot = linked.snapshot
        state = getattr(snapshot, "state", snapshot)
        return "ready" if str(getattr(state, "value", state)).lower() == "ready" else "unavailable"

    def _oauth_unavailable() -> HTTPException:
        return HTTPException(status_code=503, detail="OAuth service unavailable")

    async def _engine_tool(name: str, tool: str, arguments: dict | None = None):
        """Call one MCP tool against the in-process engine (no worker process since 14.0.0)."""
        project = _require(name)
        return await engine.call_tool(project, tool, arguments or {})

    def _require(name: str) -> Project:
        project = registry.get(name)
        if project is None:
            raise HTTPException(status_code=404, detail=f"No project named {name!r}")
        return project

    # ------------------------------------------------------------------ auth

    @app.post("/api/login")
    async def login(request: Request, body: Login) -> JSONResponse:
        if not admin_auth_configured(config):
            return JSONResponse({"ok": True, "auth_required": False})
        client = request.client.host if request.client else "?"
        # Unsalted single-round SHA-256 is the documented verifier, so an online
        # guessing loop is cheap for the attacker and there was no counter, delay
        # or lockout on this route at all. A fixed penalty window after
        # LOGIN_MAX_FAILURES turns an unbounded dictionary run into a slow one.
        retry_after = _login_lockout_remaining(client)
        if retry_after:
            log.warning("Admin login locked out for %s (%ds remaining)", client, retry_after)
            return JSONResponse(
                status_code=429,
                content={
                    "detail": f"Too many failed attempts. Try again in {retry_after}s.",
                    "presentation_id": "admin.login.too_many_attempts",
                    "presentation_values": {"retry_after": retry_after},
                },
                headers={"Retry-After": str(retry_after)},
            )
        if not verify_login(config, body.username, body.password):
            _record_login_failure(client)
            log.warning("Admin login FAILED from %s", client)
            return JSONResponse(
                status_code=401,
                content={
                    "detail": "Invalid username or password",
                    "presentation_id": "admin.login.invalid_credentials",
                },
            )
        _clear_login_failures(client)
        resp = JSONResponse({"ok": True, "username": config.admin_username})
        resp.set_cookie(SESSION_COOKIE, issue_session_token(config), **session_cookie_kwargs(config))
        resp.set_cookie(CSRF_COOKIE, secrets.token_urlsafe(32), httponly=False, samesite="strict", secure=bool(config.admin_tls_certfile and config.admin_tls_keyfile), path="/")
        log.info("Admin login: %r", config.admin_username)
        return resp

    @app.post("/api/logout")
    async def logout() -> JSONResponse:
        # Clearing the cookie is not revocation: a copy captured earlier stays
        # valid until exp (30 days by default), because there is no session store
        # and no issued-at floor. Rotating the install signing key is what makes
        # "log me out" true — for every session of this single admin account.
        revoke_all_sessions(config)
        resp = JSONResponse({"ok": True})
        resp.delete_cookie(SESSION_COOKIE, path="/")
        log.info("Admin logout: all sessions revoked")
        return resp

    @app.get("/api/session")
    async def session_info(request: Request) -> dict:
        if not admin_auth_configured(config):
            return {"auth_required": False, "authenticated": True, "username": None}
        user = read_session_user(config, request.cookies.get(SESSION_COOKIE))
        return {"auth_required": True, "authenticated": bool(user), "username": user}

    @app.get("/api/bootstrap")
    async def admin_bootstrap() -> JSONResponse:
        """Return nonsecret process identity used by both Admin shells."""
        return _auth_json({"version": __version__})

    @app.get("/api/settings/public-base-url")
    @app.get("/api/public-base-url")
    async def public_base_url_status() -> JSONResponse:
        """Return the canonical URL currently used by generated identities."""
        try:
            effective = public_url_store.effective()
        except PublicURLValidationError:
            return _connector_error(
                503, "invalid_public_base_url",
                "The configured public base URL is invalid; fix deployment configuration",
            )
        config.public_base_url = effective
        return _auth_json({
            "public_base_url": effective,
            "source": "admin" if public_url_store.has_override() else "deployment",
        })

    @app.patch("/api/settings/public-base-url")
    @app.patch("/api/public-base-url")
    async def update_public_base_url(request: Request) -> JSONResponse:
        """Persist a validated URL and apply it to this parent immediately."""
        if failure := _csrf_error(config, request):
            return failure
        try:
            body = await request.json()
            if not isinstance(body, dict) or set(body) != {"public_base_url"}:
                raise PublicURLValidationError("public_base_url is the only accepted field")
            effective = public_url_store.save(body["public_base_url"])
        except PublicURLValidationError as exc:
            return _connector_error(400, "invalid_public_base_url", str(exc))
        except (OSError, TypeError, ValueError) as exc:
            log.error("public URL persistence failed reason=%s", type(exc).__name__)
            return _connector_error(503, "public_url_unavailable", "Public base URL could not be saved")
        config.public_base_url = effective
        # Credential URL projections are held by a parent-owned service object;
        # update that projection without logging or retaining any secret value.
        if credential_store is not None:
            service_url = getattr(credential_store, "public_base_url", None)
            if isinstance(service_url, str):
                credential_store.public_base_url = effective.rstrip("/")
        if oauth_client is not None and isinstance(getattr(oauth_client, "public_base_url", None), str):
            oauth_client.public_base_url = effective.rstrip("/")
        log.info("public base URL updated source=admin url=%s", effective)
        return _auth_json({"public_base_url": effective, "source": "admin"})

    # ------------------------------------------------ authentication policy

    async def _reconcile_oauth_before_enable(was_required: bool, will_require: bool) -> bool:
        """Start and await the child before making OAuth policy effective."""
        if not will_require or was_required:
            return True
        if oauth_supervisor is None:
            return False
        starter = getattr(oauth_supervisor, "start", None)
        if starter is None:
            return False
        snapshot = await starter()
        state = getattr(snapshot, "state", snapshot)
        ready = str(getattr(state, "value", state)).lower() == "ready"
        if not ready:
            stopper = getattr(oauth_supervisor, "stop", None)
            if stopper is not None:
                await stopper()
        return ready

    async def _reconcile_oauth_after_disable(was_required: bool, will_require: bool) -> None:
        if not was_required or will_require or oauth_supervisor is None:
            return
        drain = getattr(oauth_client, "drain_introspections", None)
        if callable(drain):
            drained = await drain(config.oauth_service_request_timeout_s)
            if not drained:
                log.warning("OAuth introspection drain timed out before child shutdown")
        stopper = getattr(oauth_supervisor, "stop", None)
        if stopper is not None:
            await stopper()

    @app.get("/api/authentication")
    async def authentication_status() -> JSONResponse:
        if authentication_store is None:
            return _auth_error(503, "policy_unavailable", "Authentication policy is unavailable")
        return _auth_json(_complete_authentication_view(
            authentication_store.view(_project_names())
        ))

    @app.post("/api/authentication/orphans/{name}/repair")
    async def repair_authentication_orphan(name: str, request: Request) -> JSONResponse:
        """Revision-checked cleanup for a row left after interrupted deletion."""
        if failure := _csrf_error(config, request):
            return failure
        if authentication_store is None:
            return _auth_error(503, "policy_unavailable", "Authentication policy is unavailable")
        try:
            body = await request.json()
            if not isinstance(body, dict) or set(body) != {"expected_revision"}:
                raise ValueError("expected_revision is the only accepted field")
            expected = body["expected_revision"]
            if isinstance(expected, bool) or not isinstance(expected, int) or expected < 0:
                raise ValueError("expected_revision must be a nonnegative integer")
            if name not in authentication_store.orphaned_project_entries(_project_names()):
                return _auth_error(404, "orphan_not_found", "Authentication orphan was not found")
            updated = authentication_store.remove_orphan(
                name, expected_revision=expected
            )
        except AuthenticationRevisionConflict:
            return _auth_error(
                409, "revision_conflict", "Authentication policy changed",
                current=authentication_store.view(_project_names()),
            )
        except AuthenticationPolicyUnavailable:
            return _auth_error(503, "policy_unavailable", "Authentication policy is unavailable")
        except (ValueError, TypeError):
            return _auth_error(400, "invalid_authentication", "Invalid repair request")
        log.info("Authentication orphan repaired project=%s revision=%s", name, updated["revision"])
        return _auth_json(updated)

    @app.post("/api/authentication/preview")
    async def authentication_preview(request: Request) -> JSONResponse:
        if failure := _csrf_error(config, request):
            return failure
        try:
            body = await request.json()
            if not isinstance(body, dict):
                raise TypeError("request body must be a JSON object")
            scope = body.pop("scope", "global")
            project_name = body.pop("project", body.pop("project_name", None))
            if scope not in {"global", "project"}:
                raise ValueError("scope must be global or project")
            if scope == "project" and not isinstance(project_name, str):
                raise ValueError("project is required for project preview")
            if scope == "global" and project_name is not None:
                raise ValueError("project is only valid for project preview")
            result = _preview(body, project_name=project_name if scope == "project" else None)
        except AuthenticationRevisionConflict:
            return _auth_error(409, "revision_conflict", "Authentication policy changed", current=authentication_store.view(_project_names()))
        except (AuthenticationPolicyUnavailable, ValueError, TypeError) as exc:
            return _auth_error(400, "invalid_authentication", str(exc))
        except KeyError:
            return _auth_error(404, "project_not_found", "Project was not found")
        return _auth_json(result)

    @app.post("/api/authentication/global/static-key/generate")
    async def generate_global_static_key(request: Request) -> JSONResponse:
        if failure := _csrf_error(config, request):
            return failure
        if authentication_store is None:
            return _auth_error(503, "policy_unavailable", "Authentication policy is unavailable")
        try:
            body = await request.json()
            expected = _static_key_request(body)
            prior = authentication_store.view(_project_names())
            updated = authentication_store.generate_global_static_key(
                expected_revision=expected, project_names=_project_names()
            )
        except AuthenticationRevisionConflict:
            return _auth_error(
                409, "revision_conflict", "Authentication policy changed",
                current=authentication_store.view(_project_names()),
            )
        except AuthenticationPolicyUnavailable:
            return _auth_error(503, "policy_unavailable", "Authentication policy is unavailable")
        except (ValueError, TypeError):
            return _auth_error(400, "invalid_authentication", "Invalid static-key generation request")
        generated = updated.get("generated_key")
        log.info(
            "Authentication policy mutation scope=global action=generate prior_revision=%s "
            "new_revision=%s prior_key_id=%s new_key_id=%s",
            prior["revision"], updated["revision"],
            (prior.get("global", {}).get("static_key") or {}).get("key_id"),
            (updated.get("global", {}).get("static_key") or {}).get("key_id"),
        )
        generated = updated.pop("generated_key", None)
        return _auth_json({
            "revision": updated["revision"],
            "scope": "global",
            "generated_key": generated,
            "key": updated["global"]["static_key"],
            "authentication": _complete_authentication_view(updated),
        })

    @app.post("/api/authentication/projects/{name}/static-key/generate")
    async def generate_project_static_key(name: str, request: Request) -> JSONResponse:
        if failure := _csrf_error(config, request):
            return failure
        if authentication_store is None:
            return _auth_error(503, "policy_unavailable", "Authentication policy is unavailable")
        if name not in _project_names():
            return _auth_error(404, "project_not_found", "Project was not found")
        try:
            body = await request.json()
            expected = _static_key_request(body)
            prior = authentication_store.view(_project_names())
            updated = authentication_store.generate_project_static_key(
                name, expected_revision=expected, project_names=_project_names()
            )
        except AuthenticationRevisionConflict:
            return _auth_error(
                409, "revision_conflict", "Authentication policy changed",
                current=authentication_store.view(_project_names()),
            )
        except KeyError:
            return _auth_error(404, "project_not_found", "Project was not found")
        except AuthenticationPolicyUnavailable:
            return _auth_error(503, "policy_unavailable", "Authentication policy is unavailable")
        except (ValueError, TypeError):
            return _auth_error(400, "invalid_authentication", "Invalid static-key generation request")
        prior_project = next(item for item in prior["projects"] if item["name"] == name)
        new_project = next(item for item in updated["projects"] if item["name"] == name)
        log.info(
            "Authentication policy mutation scope=project project=%s action=generate "
            "prior_revision=%s new_revision=%s prior_key_id=%s new_key_id=%s",
            name, prior["revision"], updated["revision"],
            (prior_project.get("static_key_override") or {}).get("key_id"),
            (new_project.get("static_key_override") or {}).get("key_id"),
        )
        generated = updated.pop("generated_key", None)
        return _auth_json({
            "revision": updated["revision"],
            "scope": "project",
            "project": name,
            "generated_key": generated,
            "key": new_project["static_key_override"],
            "authentication": _complete_authentication_view(updated),
        })

    @app.post("/api/authentication/global/static-key/revoke")
    async def revoke_global_static_key(request: Request) -> JSONResponse:
        if failure := _csrf_error(config, request):
            return failure
        if authentication_store is None:
            return _auth_error(503, "policy_unavailable", "Authentication policy is unavailable")
        warnings: dict = {}
        try:
            body = await request.json()
            expected, confirm = _static_key_request(body, revoke=True)
            warnings = _static_revoke_warnings()
            prior = authentication_store.view(_project_names())
            updated = authentication_store.revoke_global_static_key(
                expected_revision=expected, confirm_lockout=confirm,
                project_names=_project_names(), lockout_project_names=_enabled_project_names(),
            )
        except AuthenticationRevisionConflict:
            return _auth_error(
                409, "revision_conflict", "Authentication policy changed",
                current=authentication_store.view(_project_names()),
            )
        except AuthenticationLockoutConfirmationRequired as exc:
            return _auth_error(
                409, "lockout_confirmation_required",
                "Explicit confirmation is required before locking out clients",
                locked_out_projects=list(exc.projects), warnings=warnings,
                affected_projects=warnings.get("locked_out_projects", list(exc.projects)),
                affected_connectors=warnings.get("locked_out_connectors", []),
            )
        except AuthenticationPolicyUnavailable:
            return _auth_error(503, "policy_unavailable", "Authentication policy is unavailable")
        except (ValueError, TypeError):
            return _auth_error(400, "invalid_authentication", "Invalid static-key revocation request")
        log.info(
            "Authentication policy mutation scope=global action=revoke prior_revision=%s "
            "new_revision=%s prior_key_id=%s new_key_id=%s",
            prior["revision"], updated["revision"],
            (prior.get("global", {}).get("static_key") or {}).get("key_id"), None,
        )
        return _auth_json(_complete_authentication_view(updated))

    @app.post("/api/authentication/projects/{name}/static-key/revoke")
    async def revoke_project_static_key(name: str, request: Request) -> JSONResponse:
        if failure := _csrf_error(config, request):
            return failure
        if authentication_store is None:
            return _auth_error(503, "policy_unavailable", "Authentication policy is unavailable")
        if name not in _project_names():
            return _auth_error(404, "project_not_found", "Project was not found")
        warnings: dict = {}
        try:
            body = await request.json()
            expected, confirm = _static_key_request(body, revoke=True)
            warnings = _static_revoke_warnings(name)
            prior = authentication_store.view(_project_names())
            updated = authentication_store.revoke_project_static_key(
                name, expected_revision=expected, confirm_lockout=confirm,
                project_names=_project_names(), lockout_project_names=_enabled_project_names(),
            )
        except AuthenticationRevisionConflict:
            return _auth_error(
                409, "revision_conflict", "Authentication policy changed",
                current=authentication_store.view(_project_names()),
            )
        except AuthenticationLockoutConfirmationRequired as exc:
            return _auth_error(
                409, "lockout_confirmation_required",
                "Explicit confirmation is required before locking out clients",
                locked_out_projects=list(exc.projects), warnings=warnings,
                affected_projects=warnings.get("locked_out_projects", list(exc.projects)),
                affected_connectors=warnings.get("locked_out_connectors", []),
            )
        except KeyError:
            return _auth_error(404, "project_not_found", "Project was not found")
        except AuthenticationPolicyUnavailable:
            return _auth_error(503, "policy_unavailable", "Authentication policy is unavailable")
        except (ValueError, TypeError):
            return _auth_error(400, "invalid_authentication", "Invalid static-key revocation request")
        prior_project = next(item for item in prior["projects"] if item["name"] == name)
        log.info(
            "Authentication policy mutation scope=project project=%s action=revoke "
            "prior_revision=%s new_revision=%s prior_key_id=%s new_key_id=%s",
            name, prior["revision"], updated["revision"],
            (prior_project.get("static_key_override") or {}).get("key_id"), None,
        )
        return _auth_json(_complete_authentication_view(updated))

    @app.patch("/api/authentication/global")
    async def update_authentication_global(request: Request) -> JSONResponse:
        if failure := _csrf_error(config, request):
            return failure
        if authentication_store is None:
            return _auth_error(503, "policy_unavailable", "Authentication policy is unavailable")
        started_for_enable = False
        try:
            body = await request.json()
            expected, values = _auth_mutation(body)
            prior_view = authentication_store.view(_project_names())
            names = _enabled_project_names()
            was_required = authentication_store.oauth_runtime_required(names)
            # A preview is deliberately not required for API clients; the store
            # enforces the second explicit confirmation for lockout mutations.
            candidate_preview = _preview(body)
            will_require = any(authentication_store.effective_oauth(name) for name in names)
            if "oauth_enabled" in values and values["oauth_enabled"] is not None:
                snapshot = authentication_store.snapshot()
                will_require = any(
                    values["oauth_enabled"] if snapshot.projects.get(name) is None or snapshot.projects[name].oauth_mode == "inherit"
                    else authentication_store.effective_oauth(name)
                    for name in names
                )
            if not await _reconcile_oauth_before_enable(was_required, will_require):
                return _auth_error(503, "oauth_unavailable", "OAuth service is not ready")
            started_for_enable = will_require and not was_required
            updated = authentication_store.mutate_global(
                expected_revision=expected,
                oauth_enabled=values.get("oauth_enabled"),
                static_key_action=values["static_key_action"],
                confirm_lockout=values["confirm_lockout"],
                project_names=names,
            )
            await _reconcile_oauth_after_disable(was_required, authentication_store.oauth_runtime_required(names))
        except AuthenticationRevisionConflict:
            await _reconcile_oauth_after_disable(started_for_enable, False)
            return _auth_error(409, "revision_conflict", "Authentication policy changed", current=authentication_store.view(_project_names()))
        except AuthenticationLockoutConfirmationRequired as exc:
            await _reconcile_oauth_after_disable(started_for_enable, False)
            return _auth_error(409, "lockout_confirmation_required", "Explicit confirmation is required before locking out clients", locked_out_projects=list(exc.projects), warnings=candidate_preview.get("warnings", {}))
        except AuthenticationPolicyUnavailable:
            await _reconcile_oauth_after_disable(started_for_enable, False)
            return _auth_error(503, "policy_unavailable", "Authentication policy is unavailable")
        except (ValueError, TypeError) as exc:
            await _reconcile_oauth_after_disable(started_for_enable, False)
            return _auth_error(400, "invalid_authentication", str(exc))
        prior_key = prior_view.get("global", {}).get("static_key")
        new_key = updated.get("global", {}).get("static_key")
        log.info(
            "Authentication policy mutation scope=global action=%s prior_revision=%s "
            "new_revision=%s prior_key_id=%s new_key_id=%s warning_count=%d",
            values["static_key_action"], prior_view["revision"], updated["revision"],
            prior_key.get("key_id") if isinstance(prior_key, dict) else None,
            new_key.get("key_id") if isinstance(new_key, dict) else None,
            candidate_preview.get("warnings", {}).get("count", 0),
        )
        return _auth_json(updated)

    @app.patch("/api/authentication/projects/{name}")
    async def update_authentication_project(name: str, request: Request) -> JSONResponse:
        if failure := _csrf_error(config, request):
            return failure
        if authentication_store is None:
            return _auth_error(503, "policy_unavailable", "Authentication policy is unavailable")
        started_for_enable = False
        try:
            body = await request.json()
            expected, values = _auth_mutation(body, project=True)
            prior_view = authentication_store.view(_project_names())
            all_names = _project_names()
            if name not in all_names:
                return _auth_error(404, "project_not_found", "Project was not found")
            names = _enabled_project_names()
            was_required = authentication_store.oauth_runtime_required(names)
            candidate_preview = _preview(body, project_name=name)
            will_require = any(authentication_store.effective_oauth(item) for item in names)
            if values.get("oauth_mode") is not None and name in names:
                global_oauth = authentication_store.snapshot().global_.oauth_enabled
                selected_mode = values["oauth_mode"]
                will_require = any(
                    (
                        global_oauth if selected_mode == "inherit"
                        else selected_mode == "enabled"
                    ) if item == name else authentication_store.effective_oauth(item)
                    for item in names
                )
            if not await _reconcile_oauth_before_enable(was_required, will_require):
                return _auth_error(503, "oauth_unavailable", "OAuth service is not ready")
            started_for_enable = will_require and not was_required
            updated = authentication_store.mutate_project(
                name, expected_revision=expected, oauth_mode=values.get("oauth_mode"),
                static_key_action=values["static_key_action"],
                confirm_lockout=values["confirm_lockout"], project_names=names,
            )
            await _reconcile_oauth_after_disable(was_required, authentication_store.oauth_runtime_required(names))
        except AuthenticationRevisionConflict:
            await _reconcile_oauth_after_disable(started_for_enable, False)
            return _auth_error(409, "revision_conflict", "Authentication policy changed", current=authentication_store.view(_project_names()))
        except AuthenticationLockoutConfirmationRequired as exc:
            await _reconcile_oauth_after_disable(started_for_enable, False)
            return _auth_error(409, "lockout_confirmation_required", "Explicit confirmation is required before locking out clients", locked_out_projects=list(exc.projects), warnings=candidate_preview.get("warnings", {}))
        except AuthenticationPolicyUnavailable:
            await _reconcile_oauth_after_disable(started_for_enable, False)
            return _auth_error(503, "policy_unavailable", "Authentication policy is unavailable")
        except (ValueError, TypeError) as exc:
            await _reconcile_oauth_after_disable(started_for_enable, False)
            return _auth_error(400, "invalid_authentication", str(exc))
        prior_project = next(
            (item for item in prior_view["projects"] if item["name"] == name), {}
        )
        new_project = next(
            (item for item in updated["projects"] if item["name"] == name), {}
        )
        prior_key = prior_project.get("static_key_override")
        new_key = new_project.get("static_key_override")
        log.info(
            "Authentication policy mutation scope=project project=%s action=%s "
            "prior_revision=%s new_revision=%s prior_key_id=%s new_key_id=%s warning_count=%d",
            name, values["static_key_action"], prior_view["revision"], updated["revision"],
            prior_key.get("key_id") if isinstance(prior_key, dict) else None,
            new_key.get("key_id") if isinstance(new_key, dict) else None,
            candidate_preview.get("warnings", {}).get("count", 0),
        )
        return _auth_json(updated)

    # --------------------------------------------------- 12.0 surfaces/keys

    def _high_trust_available() -> bool:
        """Whether this installation can safely admit a high-trust mutation."""
        return admin_auth_configured(config)

    def _high_trust_error() -> JSONResponse | None:
        if not _high_trust_available():
            return _connector_error(
                503, "admin_password_required",
                "Configure an administrator password before enabling Workspace or credentials",
            )
        return None

    app.include_router(create_connector_router(
        config, registry, connector_store, public_url_store,
        _csrf_error, _high_trust_error, _connector_error,
        workspace_configured=workspace_admin_service is not None,
    ))

    def _workspace_snapshot() -> tuple[int, list[Any]]:
        if workspace_connector_store is None:
            raise RuntimeError("Workspace connector policy is unavailable")
        result = _invoke(workspace_connector_store, ("snapshot", "view", "list"))
        row = _as_mapping(result)
        if isinstance(result, (list, tuple)):
            return 0, list(result)
        records = row.get("connectors", row.get("workspace_connectors", row.get("records", [])))
        return int(row.get("revision", getattr(result, "revision", 0))), list(records or [])

    def _workspace_view(surface: Any, revision: int = 0) -> dict:
        row = _as_mapping(surface)
        surface_id = row.get("id", row.get("surface_id"))
        slug = row.get("slug", "")
        out = {
            "id": surface_id,
            "surface_id": surface_id,
            "slug": slug,
            "name": row.get("name", row.get("display_name", "")),
            "display_name": row.get("display_name", row.get("name", "")),
            "enabled": bool(row.get("enabled", False)),
            "workspace_requested": bool(row.get("enabled", False)),
            "workspace_effective": bool(row.get("enabled", False) and workspace_admin_service is not None),
            "workspace_reason": (
                "host_workspace_disabled"
                if row.get("enabled", False) and workspace_admin_service is None else None
            ),
            "revision": int(row.get("revision", revision)),
            # Workspace generations are code-owned; an older persisted surface
            # record must not keep advertising the retired v1 catalog.
            "contract_version": WORKSPACE_CONTRACT_VERSION,
            "url": f"{config.public_base_url.rstrip('/')}/mcp/workspace/{slug}/mcp",
            "path": f"/mcp/workspace/{slug}/mcp",
        }
        previous = row.get("previous_contract_version", row.get("previous_version"))
        if previous is not None:
            out["previous_contract_version"] = int(previous)
            out["previous_url"] = f"{config.public_base_url.rstrip('/')}/mcp/workspace/{slug}/mcp/v{int(previous)}"
        out["current_url"] = f"{out['url']}/v{out['contract_version']}"
        return out

    def _workspace_error(exc: Exception, *, default_reason: str = "workspace_policy") -> JSONResponse:
        name = type(exc).__name__.casefold()
        if "revision" in name or "conflict" in name:
            return _connector_error(409, "revision_conflict", "Workspace policy changed")
        if "notfound" in name or "not_found" in name:
            return _connector_error(404, "workspace_connector_not_found", "Workspace connector was not found")
        if "unavailable" in name:
            return _connector_error(503, "policy_unavailable", "Workspace policy is unavailable")
        return _connector_error(400, default_reason, str(exc) or "Invalid Workspace policy request")

    async def _credential_call(method_names: tuple[str, ...], *args, **kwargs) -> Any:
        if credential_store is None:
            raise RuntimeError("Named credential policy is unavailable")
        return await _await_if_needed(_invoke(credential_store, method_names, *args, **kwargs))

    def _credential_list_payload(result: Any) -> dict:
        row = _as_mapping(result)
        if isinstance(result, (list, tuple)):
            return {"revision": 0, "credentials": [_credential_metadata(item) for item in result]}
        records = row.get("credentials", row.get("records", []))
        return {
            "revision": int(row.get("revision", 0)),
            "credentials": [_credential_metadata(item) for item in (records or [])],
        }

    def _credential_result(result: Any, *, include_secret: bool = False) -> dict:
        row = _as_mapping(result)
        out = {key: value for key, value in row.items() if key not in {
            "secret", "generated_secret", "token", "plaintext", "raw_secret",
        }}
        if include_secret:
            secret = row.get("secret", row.get("generated_secret", row.get("token")))
            if secret is not None:
                out["secret"] = secret
        if "credential" in row:
            out["credential"] = _credential_metadata(row["credential"])
        if "credentials" in row:
            out["credentials"] = [_credential_metadata(item) for item in row["credentials"]]
        return out

    async def _credential_action_error(exc: Exception) -> JSONResponse:
        name = type(exc).__name__.casefold()
        # 13.2.0: the mapping below folds several distinct failures into one
        # client message ("Credential policy changed" covers a revision
        # mismatch, a duplicate label and a missing surface), so the server
        # log has to say which one it was. No secrets ride in these messages.
        log.warning("credential request failed error=%s detail=%s", type(exc).__name__, str(exc)[:200])
        if "revision" in name or "conflict" in name:
            return _auth_error(409, "revision_conflict", "Credential policy changed")
        if "notfound" in name or "not_found" in name:
            return _auth_error(404, "credential_not_found", "Credential was not found")
        if "limit" in name or "capacity" in name:
            return _auth_error(409, "credential_limit", "The surface already has 64 active credentials")
        if "unavailable" in name:
            return _auth_error(503, "policy_unavailable", "Credential policy is unavailable")
        return _auth_error(400, "invalid_credential", str(exc) or "Invalid credential request")

    def _credential_request(body: Any, *, label_required: bool = False) -> dict:
        if not isinstance(body, dict):
            raise ValueError("request body must be a JSON object")
        allowed = {"expected_revision", "label", "name", "current_password", "password", "retention", "confirm", "route_strategy", "provider", "workspace_id", "workspace_revision"}
        unknown = sorted(set(body) - allowed)
        if unknown:
            raise ValueError(f"unknown credential field(s): {', '.join(unknown)}")
        expected = body.get("expected_revision")
        if isinstance(expected, bool) or not isinstance(expected, int) or expected < 0:
            raise ValueError("expected_revision must be a nonnegative integer")
        label = body.get("label", body.get("name"))
        if label_required and (not isinstance(label, str) or not label.strip() or len(label.strip()) > 120):
            raise ValueError("label must be 1 to 120 characters")
        out = {"expected_revision": expected}
        if label is not None:
            out["label"] = label
        for key in ("current_password", "password", "retention", "route_strategy", "provider"):
            if key in body:
                if not isinstance(body[key], str):
                    raise ValueError(f"{key} must be a string")
                out[key] = body[key]
        if "confirm" in body:
            if not isinstance(body["confirm"], bool):
                raise ValueError("confirm must be a boolean")
            out["confirm"] = body["confirm"]
        if "workspace_id" in body:
            workspace_id = body["workspace_id"]
            if workspace_id is not None and (not isinstance(workspace_id, str) or not workspace_id.strip() or len(workspace_id) > 200):
                raise ValueError("workspace_id must be null or a bounded string")
            out["workspace_id"] = workspace_id
        if "workspace_revision" in body:
            workspace_revision = body["workspace_revision"]
            if workspace_revision is not None and (
                isinstance(workspace_revision, bool) or not isinstance(workspace_revision, int)
                or workspace_revision < 0
            ):
                raise ValueError("workspace_revision must be null or a nonnegative integer")
            out["workspace_revision"] = workspace_revision
        return out

    @app.get("/api/workspace-connectors")
    async def list_workspace_connectors() -> JSONResponse:
        try:
            config.public_base_url = public_url_store.effective()
            revision, records = _workspace_snapshot()
            return _auth_json({
                "revision": revision,
                "workspace_connectors": [_workspace_view(item, revision) for item in records],
            })
        except Exception as exc:
            return _workspace_error(exc, default_reason="policy_unavailable")

    @app.post("/api/workspace-connectors", status_code=201)
    async def add_workspace_connector(request: Request) -> JSONResponse:
        if failure := _csrf_error(config, request):
            return failure
        if failure := _high_trust_error():
            return failure
        try:
            body = await request.json()
            if not isinstance(body, dict):
                raise ValueError("request body must be a JSON object")
            allowed = {"expected_revision", "name", "display_name", "slug", "enabled", "confirm_high_trust"}
            unknown = sorted(set(body) - allowed)
            if unknown:
                raise ValueError(f"unknown Workspace connector field(s): {', '.join(unknown)}")
            expected = body.get("expected_revision")
            if isinstance(expected, bool) or not isinstance(expected, int) or expected < 0:
                raise ValueError("expected_revision must be a nonnegative integer")
            name = body.get("display_name", body.get("name"))
            if not isinstance(name, str) or not name.strip() or len(name.strip()) > 120:
                raise ValueError("display_name must be 1 to 120 characters")
            if body.get("confirm_high_trust") is not True:
                raise ValueError("explicit high-trust confirmation is required")
            enabled = body.get("enabled", False)
            if not isinstance(enabled, bool):
                raise ValueError("enabled must be a boolean")
            if enabled and workspace_admin_service is None:
                return _connector_error(
                    409, "host_workspace_disabled",
                    "Workspace cannot be enabled while this host is in core mode",
                )
            values = {"display_name": name.strip(), "enabled": enabled}
            if "slug" in body:
                if not isinstance(body["slug"], str) or not _WORKSPACE_SLUG_RE.fullmatch(body["slug"]):
                    raise ValueError("slug must be a string")
                values["slug"] = body["slug"]
            result = await _await_if_needed(_invoke(
                workspace_connector_store, ("create", "add"),
                expected_revision=expected, **values,
            ))
            revision, records = _workspace_snapshot()
            created = next((item for item in records if _surface_id(item) == _surface_id(result)), result)
            return _auth_json({"revision": revision, "workspace_connector": _workspace_view(created, revision)}, 201)
        except Exception as exc:
            return _workspace_error(exc)

    @app.patch("/api/workspace-connectors/{surface_id}")
    async def edit_workspace_connector(surface_id: str, request: Request) -> JSONResponse:
        if failure := _csrf_error(config, request):
            return failure
        try:
            body = await request.json()
            if not isinstance(body, dict):
                raise ValueError("request body must be a JSON object")
            allowed = {"expected_revision", "name", "display_name", "enabled", "confirm_high_trust"}
            unknown = sorted(set(body) - allowed)
            if unknown:
                raise ValueError(f"unknown Workspace connector field(s): {', '.join(unknown)}")
            expected = body.get("expected_revision")
            if isinstance(expected, bool) or not isinstance(expected, int) or expected < 0:
                raise ValueError("expected_revision must be a nonnegative integer")
            if body.get("enabled") is True and body.get("confirm_high_trust") is not True:
                raise ValueError("explicit high-trust confirmation is required")
            values = {}
            if "display_name" in body or "name" in body:
                values["display_name"] = body.get("display_name", body.get("name"))
            if "enabled" in body:
                if not isinstance(body["enabled"], bool):
                    raise ValueError("enabled must be a boolean")
                if body["enabled"] and workspace_admin_service is None:
                    _revision, current_records = _workspace_snapshot()
                    current_surface = next(
                        (item for item in current_records
                         if _surface_id(item).casefold() == surface_id.casefold()),
                        None,
                    )
                    current_enabled = (
                        bool(_as_mapping(current_surface).get("enabled", False))
                        if current_surface is not None else None
                    )
                    if current_enabled is False:
                        return _connector_error(
                            409, "host_workspace_disabled",
                            "Workspace cannot be enabled while this host is in core mode",
                        )
                values["enabled"] = body["enabled"]
            result = await _await_if_needed(_invoke(
                workspace_connector_store, ("update", "edit"), surface_id,
                expected_revision=expected, **values,
            ))
            revision, records = _workspace_snapshot()
            updated = next((item for item in records if _surface_id(item).casefold() == surface_id.casefold()), result)
            return _auth_json({"revision": revision, "workspace_connector": _workspace_view(updated, revision)})
        except Exception as exc:
            return _workspace_error(exc)

    @app.delete("/api/workspace-connectors/{surface_id}")
    async def remove_workspace_connector(surface_id: str, request: Request) -> JSONResponse:
        if failure := _csrf_error(config, request):
            return failure
        try:
            body = await request.json()
        except (ValueError, TypeError):
            body = {}
        try:
            if not isinstance(body, dict):
                raise ValueError("request body must be a JSON object")
            expected = body.get("expected_revision")
            if isinstance(expected, bool) or not isinstance(expected, int) or expected < 0:
                raise ValueError("expected_revision must be a nonnegative integer")
            if body.get("confirm") is not True and body.get("confirm_high_trust") is not True:
                raise ValueError("explicit confirmation is required")
            result = await _await_if_needed(_invoke(
                workspace_connector_store, ("delete", "remove"), surface_id,
                expected_revision=expected, confirm=True,
            ))
            row = _as_mapping(result)
            return _auth_json({"deleted": surface_id, "revision": int(row.get("revision", expected + 1))})
        except Exception as exc:
            return _workspace_error(exc)

    async def _list_credentials(surface_kind: str, surface_id: str) -> JSONResponse:
        try:
            result = await _credential_call(
                ("list_credentials", "list"),
                surface_kind=surface_kind, surface_id=surface_id,
            )
            return _auth_json(_credential_list_payload(result))
        except Exception as exc:
            return await _credential_action_error(exc)

    async def _create_credential(surface_kind: str, surface_id: str, request: Request) -> JSONResponse:
        if failure := _csrf_error(config, request):
            return failure
        if failure := _high_trust_error():
            return failure
        try:
            body = _credential_request(await request.json(), label_required=True)
            result = await _credential_call(
                ("create_credential", "create"),
                surface_kind=surface_kind, surface_id=surface_id, **body,
            )
            return _auth_json(_credential_result(result, include_secret=True), 201)
        except Exception as exc:
            return await _credential_action_error(exc)

    async def _credential_mutation(surface_kind: str, surface_id: str, credential_id: str,
                                   action: str, request: Request) -> JSONResponse:
        if failure := _csrf_error(config, request):
            return failure
        if failure := _high_trust_error():
            return failure
        try:
            body = _credential_request(await request.json())
            names = {
                "rotate": ("rotate_credential", "rotate"),
                "reveal": ("reveal_credential", "reveal"),
                "revoke": ("revoke_credential", "revoke"),
                "delete": ("delete_credential", "delete", "remove"),
            }[action]
            if action == "reveal":
                proof = body.get("current_password", body.get("password"))
                if not proof:
                    raise ValueError("current_password is required")
            if action == "delete" and body.get("confirm") is not True:
                raise ValueError("explicit confirmation is required")
            if action == "delete" and (
                "workspace_id" not in body or "workspace_revision" not in body
                or ((body["workspace_id"] is None) != (body["workspace_revision"] is None))
            ):
                raise ValueError("Workspace binding is required for credential deletion")
            result = await _credential_call(
                names, surface_kind=surface_kind, surface_id=surface_id,
                credential_id=credential_id, **body,
            )
            return _auth_json(_credential_result(result, include_secret=action in {"rotate", "reveal"}))
        except Exception as exc:
            return await _credential_action_error(exc)

    async def _credential_setup(surface_kind: str, surface_id: str, credential_id: str,
                                request: Request) -> JSONResponse:
        if failure := _csrf_error(config, request):
            return failure
        if failure := _high_trust_error():
            return failure
        try:
            body = await request.json()
            if not isinstance(body, dict):
                raise ValueError("request body must be a JSON object")
            allowed = {"route_strategy", "provider", "current_password"}
            if set(body) - allowed:
                raise ValueError("unknown setup field")
            # Both products expose a stable alias for their current catalog;
            # setup material may also pin the immutable current generation.
            strategy = body.get("route_strategy", "stable")
            if surface_kind == "workspace":
                if strategy not in {"stable", "current"}:
                    raise ValueError("Workspace route_strategy must be stable or current")
            elif strategy not in {"stable", "current"}:
                raise ValueError("route_strategy must be stable or current")
            proof = body.get("current_password")
            if not proof:
                raise ValueError("current_password is required")
            result = await _credential_call(
                ("setup_material", "build_setup_material"),
                surface_kind=surface_kind, surface_id=surface_id,
                credential_id=credential_id, route_strategy=strategy,
                provider=body.get("provider", "provider-neutral"),
                current_password=proof,
            )
            return _auth_json(_credential_result(result, include_secret=True))
        except Exception as exc:
            return await _credential_action_error(exc)

    # --------------------------------------------------- 12.0 Workspace lifecycle

    _WORKSPACE_SORTS = frozenset({
        "credential", "connector", "state", "created", "last_accessed",
        "deletion_due", "actual_allocation", "apparent_size", "quota_percent",
    })
    _WORKSPACE_ACTIONS = frozenset({
        "start", "stop", "pin", "unpin", "remove", "diagnostics", "retry",
    })
    _WORKSPACE_SETTING_FIELDS = frozenset({
        "retention_days", "quota_bytes", "idle_stop_seconds", "host_reserve_bytes",
        "network_mode", "network_rules", "brave_enabled", "brave_api_key",
        "warning_threshold_percent", "max_running_workspaces",
    })
    _SECRET_SETTING_FIELDS = frozenset({"brave_api_key", "api_key", "key", "secret", "token"})

    def _workspace_admin_error(exc: Exception, *, not_found: str = "Workspace was not found") -> JSONResponse:
        """Map domain failures without echoing paths, commands, or secret values."""
        name = type(exc).__name__.casefold()
        if "revision" in name or "conflict" in name:
            return _auth_error(409, "revision_conflict", "Workspace state changed; refresh and retry")
        if "notfound" in name or "not_found" in name:
            return _auth_error(404, "workspace_not_found", not_found)
        if "unavailable" in name or "runtime" in name:
            return _auth_error(503, "workspace_unavailable", "Workspace service is unavailable")
        if "quota" in name or "capacity" in name or "reserve" in name:
            return _auth_error(409, "workspace_capacity", "Workspace capacity or quota does not permit this operation")
        if "confirm" in name or "trust" in name:
            return _auth_error(400, "high_trust_confirmation_required", "Explicit high-trust confirmation is required")
        # Do not include exception text: domain exceptions may contain exact
        # host paths, guest commands, or administrator-supplied secrets.
        return _auth_error(400, "invalid_workspace_request", "Workspace request was rejected")

    def _workspace_service_required() -> Any:
        if workspace_service is None:
            raise RuntimeError("workspace admin service is unavailable")
        return workspace_service

    async def _workspace_admin_call(method_names: tuple[str, ...], *args, **kwargs) -> Any:
        service = _workspace_service_required()
        return await _await_if_needed(_invoke(service, method_names, *args, **kwargs))

    def _workspace_mutation_body(body: Any, *, require_confirmation: bool = False) -> dict[str, Any]:
        if not isinstance(body, dict):
            raise ValueError("request body must be an object")
        allowed = {"expected_revision", "confirm", "confirm_high_trust", "idempotency_token"}
        unknown = sorted(set(body) - allowed)
        if unknown:
            raise ValueError("unknown workspace mutation field")
        expected = body.get("expected_revision")
        if isinstance(expected, bool) or not isinstance(expected, int) or expected < 0:
            raise ValueError("expected_revision must be a nonnegative integer")
        token = body.get("idempotency_token")
        if token is not None and (not isinstance(token, str) or not token or len(token) > 128):
            raise ValueError("idempotency_token must be a bounded string")
        if require_confirmation and body.get("confirm") is not True and body.get("confirm_high_trust") is not True:
            raise ValueError("explicit confirmation is required")
        return {
            "expected_revision": expected,
            "idempotency_token": token,
        }

    def _workspace_action_result(result: Any) -> dict[str, Any]:
        row = _as_mapping(result)
        output: dict[str, Any] = {
            key: row[key] for key in ("revision", "idempotency_replayed", "accepted", "status")
            if key in row
        }
        if "workspace" in row:
            output["workspace"] = workspace_view(row["workspace"])
        elif any(key in row for key in ("workspace_id", "state", "actual_bytes", "apparent_bytes")):
            output["workspace"] = workspace_view(row)
        if "runtime" in row:
            output["runtime"] = status_view(row["runtime"])
        return output

    @app.get("/api/workspaces")
    async def list_admin_workspaces(
        sort: str = Query("credential"),
        direction: str = Query("asc"),
        search: str = Query(""),
        state: str | None = Query(None),
        pinned: bool | None = Query(None),
        expired: bool | None = Query(None),
        over_warning: bool | None = Query(None),
        owner_status: str | None = Query(None),
        cursor: str | None = Query(None),
        limit: int = Query(100),
    ) -> JSONResponse:
        if sort not in _WORKSPACE_SORTS or direction not in {"asc", "desc"}:
            return _auth_error(400, "invalid_workspace_query", "Workspace sort or direction is invalid")
        if len(search) > 200:
            return _auth_error(400, "invalid_workspace_query", "Workspace search is too long")
        states = tuple(item for item in (state or "").split(",") if item)
        if len(states) > 8 or any(len(item) > 32 for item in states):
            return _auth_error(400, "invalid_workspace_query", "Workspace state filter is invalid")
        if (cursor is not None and len(cursor) > 256) or not 1 <= limit <= 500:
            return _auth_error(400, "invalid_workspace_query", "Workspace page is invalid")
        if owner_status is not None and owner_status not in {"active", "revoked", "tombstoned", "orphaned"}:
            return _auth_error(400, "invalid_workspace_query", "Workspace owner filter is invalid")
        try:
            filters = {
                "sort": sort, "direction": direction, "search": search,
                "states": states, "pinned": pinned, "expired": expired,
                "over_warning": over_warning, "cursor": cursor, "limit": limit,
            }
            if owner_status is not None:
                filters["owner_status"] = owner_status
            result = await _workspace_admin_call(
                ("list_admin_workspaces", "list_workspaces"),
                **filters,
            )
            payload = admin_payload(result)
            # Runtime/storage cards are server-owned.  Never derive any value
            # from rows in this transport layer or in the browser.
            return _auth_json(payload)
        except Exception as exc:
            return _workspace_admin_error(exc, not_found="Workspace list is unavailable")

    @app.get("/api/workspaces/health")
    @app.get("/api/workspace-runtime")
    async def workspace_runtime_health() -> JSONResponse:
        try:
            result = await _workspace_admin_call(("runtime_health", "health"))
            row = _as_mapping(result)
            nested = row.get("runtime")
            runtime = nested if isinstance(nested, dict) else result
            payload = {"runtime": status_view(runtime)}
            if isinstance(row.get("storage"), dict):
                payload["storage"] = status_view(row["storage"])
            return _auth_json(payload)
        except Exception as exc:
            return _workspace_admin_error(exc, not_found="Workspace runtime is unavailable")

    async def _workspace_bulk_request(request: Request, *, preview: bool = False) -> JSONResponse:
        """Validate a bounded bulk selection and delegate lifecycle ownership.

        Preview/apply are separate, fail-closed interfaces. A destructive apply
        must always be bound to a server-issued preview token; falling back to
        the pre-preview bulk method would allow a missing token to delete
        immediately.
        """
        if failure := _csrf_error(config, request):
            return failure
        if failure := _high_trust_error():
            return failure
        try:
            body = await request.json()
            if not isinstance(body, dict):
                raise ValueError("request body must be an object")
            allowed = {
                "action", "workspace_ids", "expected_revisions", "confirm",
                "confirm_high_trust", "idempotency_token", "preview_token",
            }
            if set(body) - allowed:
                raise ValueError("unknown bulk workspace field")
            action = body.get("action")
            if action != "remove":
                raise ValueError("bulk action must be remove")
            ids = body.get("workspace_ids")
            revisions = body.get("expected_revisions")
            if (
                not isinstance(ids, list) or not ids or len(ids) > 1000
                or any(not isinstance(item, str) or not item for item in ids)
            ):
                raise ValueError("workspace_ids must be a nonempty list")
            if len(set(ids)) != len(ids) or not isinstance(revisions, dict):
                raise ValueError("expected_revisions must match selected workspaces")
            expected: dict[str, int] = {}
            for item in ids:
                value = revisions.get(item)
                if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                    raise ValueError("expected_revisions must contain nonnegative integers")
                expected[item] = value
            if preview:
                result = await _workspace_admin_call(
                    ("preview_bulk_workspace_action", "preview_bulk_delete"),
                    action, ids, expected_revisions=expected,
                )
                row = _as_mapping(result)
                return _auth_json({
                    "action": action,
                    "preview_token": row.get("preview_token", row.get("token")),
                    "expires_at": row.get("expires_at"),
                    "targets": [workspace_view(item) for item in row.get("targets", row.get("workspaces", []))],
                    "reclaim_estimate_bytes": row.get("reclaim_estimate_bytes"),
                    "reclaim_estimate_status": row.get("reclaim_estimate_status", "unknown"),
                })
            if body.get("confirm") is not True and body.get("confirm_high_trust") is not True:
                raise ValueError("explicit confirmation is required")
            token = body.get("idempotency_token")
            if token is not None and (not isinstance(token, str) or not token or len(token) > 128):
                raise ValueError("idempotency_token must be a bounded string")
            preview_token = body.get("preview_token")
            service = _workspace_service_required()
            if (
                not isinstance(preview_token, str)
                or not preview_token
                or len(preview_token) > 256
            ):
                raise ValueError("preview_token is required and must be a bounded string")
            if not callable(getattr(service, "apply_bulk_workspace_action", None)):
                raise ValueError("bulk workspace apply is unavailable")
            result = await _workspace_admin_call(
                ("apply_bulk_workspace_action",), action, ids,
                expected_revisions=expected, preview_token=preview_token,
                idempotency_token=token,
                confirm_high_trust=(
                    body.get("confirm_high_trust") is True
                    or body.get("confirm") is True
                ),
            )
            row = _as_mapping(result)
            return _auth_json({
                "action": action,
                "revision": row.get("revision"),
                "removed": row.get("removed", []),
                "workspaces": [workspace_view(item) for item in row.get("workspaces", [])],
                "results": row.get("results", []),
            })
        except Exception as exc:
            return _workspace_admin_error(exc)

    @app.post("/api/workspaces/bulk-delete/preview")
    @app.post("/api/workspaces/bulk/preview")
    async def preview_bulk_workspace_action(request: Request) -> JSONResponse:
        return await _workspace_bulk_request(request, preview=True)

    @app.post("/api/workspaces/bulk")
    @app.post("/api/workspaces/bulk-delete")
    async def bulk_workspace_action(request: Request) -> JSONResponse:
        return await _workspace_bulk_request(request)

    @app.post("/api/workspaces/{workspace_id}/{action}")
    async def workspace_action(workspace_id: str, action: str, request: Request) -> JSONResponse:
        if action not in _WORKSPACE_ACTIONS:
            raise HTTPException(status_code=404, detail="Workspace action was not found")
        if failure := _csrf_error(config, request):
            return failure
        if failure := _high_trust_error():
            return failure
        try:
            body = await request.json()
            parsed = _workspace_mutation_body(body, require_confirmation=action == "remove")
            result = await _workspace_admin_call(
                ("workspace_action", "mutate_workspace"), action, workspace_id, **parsed,
            )
            return _auth_json(_workspace_action_result(result))
        except Exception as exc:
            return _workspace_admin_error(exc)

    @app.patch("/api/workspaces/{workspace_id}/retention")
    async def workspace_retention(workspace_id: str, request: Request) -> JSONResponse:
        if failure := _csrf_error(config, request):
            return failure
        if failure := _high_trust_error():
            return failure
        try:
            body = await request.json()
            if not isinstance(body, dict) or set(body) - {
                "expected_revision", "pinned", "retention_days", "idempotency_token",
            }:
                raise ValueError("invalid retention request")
            expected = body.get("expected_revision")
            pinned = body.get("pinned")
            days = body.get("retention_days")
            token = body.get("idempotency_token")
            if isinstance(expected, bool) or not isinstance(expected, int) or expected < 0:
                raise ValueError("expected_revision is invalid")
            if not isinstance(pinned, bool):
                raise ValueError("pinned is invalid")
            if token is not None and (not isinstance(token, str) or not token or len(token) > 128):
                raise ValueError("idempotency_token is invalid")
            result = await _workspace_admin_call(
                ("set_workspace_retention", "update_retention"), workspace_id,
                expected_revision=expected, pinned=pinned, retention_days=days,
                idempotency_token=token,
            )
            return _auth_json(_workspace_action_result(result))
        except Exception as exc:
            return _workspace_admin_error(exc)

    @app.delete("/api/workspaces/{workspace_id}")
    async def delete_workspace(workspace_id: str, request: Request) -> JSONResponse:
        if failure := _csrf_error(config, request):
            return failure
        if failure := _high_trust_error():
            return failure
        try:
            parsed = _workspace_mutation_body(await request.json(), require_confirmation=True)
            result = await _workspace_admin_call(
                ("workspace_action", "mutate_workspace"), "remove", workspace_id, **parsed,
            )
            return _auth_json(_workspace_action_result(result))
        except Exception as exc:
            return _workspace_admin_error(exc)

    @app.get("/api/workspaces/{workspace_id}/diagnostics")
    async def workspace_diagnostics(workspace_id: str) -> JSONResponse:
        try:
            result = await _workspace_admin_call(
                ("workspace_diagnostics", "diagnostics"), workspace_id,
            )
            return _auth_json(_workspace_action_result(result))
        except Exception as exc:
            return _workspace_admin_error(exc)

    def _workspace_settings_body(body: Any, *, require_confirmation: bool = False) -> tuple[int, dict[str, Any], bool]:
        if not isinstance(body, dict):
            raise ValueError("request body must be an object")
        allowed = _WORKSPACE_SETTING_FIELDS | {"expected_revision", "confirm_high_trust", "idempotency_token"}
        if set(body) - allowed:
            raise ValueError("unknown Workspace setting field")
        expected = body.get("expected_revision")
        if isinstance(expected, bool) or not isinstance(expected, int) or expected < 0:
            raise ValueError("expected_revision must be a nonnegative integer")
        values = {key: body[key] for key in _WORKSPACE_SETTING_FIELDS if key in body}
        if "brave_api_key" in values and (
            not isinstance(values["brave_api_key"], str)
            or len(values["brave_api_key"]) > 4096
        ):
            raise ValueError("brave API key is invalid")
        if "network_mode" in values and values["network_mode"] not in {"off", "allowlist", "unrestricted_public"}:
            raise ValueError("network_mode is invalid")
        token = body.get("idempotency_token")
        if token is not None and (not isinstance(token, str) or not token or len(token) > 128):
            raise ValueError("idempotency_token must be a bounded string")
        # Every policy change can affect resource use or data egress.  Require
        # an explicit typed confirmation even for retention/quota changes;
        # administrator authentication alone is not an adequate acknowledgement
        # for a high-trust Workspace policy mutation.
        sensitive = bool(values)
        if (require_confirmation or sensitive) and body.get("confirm_high_trust") is not True:
            raise ValueError("explicit high-trust confirmation is required")
        values["_idempotency_token"] = token
        return expected, values, sensitive

    @app.get("/api/workspace-settings")
    async def get_workspace_settings() -> JSONResponse:
        try:
            result = await _workspace_admin_call(("get_workspace_settings", "workspace_settings", "get_settings"))
            row = _as_mapping(result)
            return _auth_json({"settings": settings_view(row.get("settings", result))})
        except Exception as exc:
            return _workspace_admin_error(exc, not_found="Workspace settings are unavailable")

    @app.post("/api/workspace-settings/preview")
    @app.post("/api/workspace-settings/preview-network")
    async def preview_workspace_settings(request: Request) -> JSONResponse:
        if failure := _csrf_error(config, request):
            return failure
        if failure := _high_trust_error():
            return failure
        try:
            _expected, values, _sensitive = _workspace_settings_body(await request.json(), require_confirmation=True)
            values.pop("_idempotency_token", None)
            result = await _workspace_admin_call(("preview_workspace_settings", "preview_settings"), values)
            row = _as_mapping(result)
            return _auth_json({"preview": preview_view(row.get("preview", result))})
        except Exception as exc:
            return _workspace_admin_error(exc)

    @app.patch("/api/workspace-settings")
    @app.post("/api/workspace-settings")
    async def update_workspace_settings(request: Request) -> JSONResponse:
        if failure := _csrf_error(config, request):
            return failure
        if failure := _high_trust_error():
            return failure
        try:
            expected, values, _sensitive = _workspace_settings_body(await request.json())
            token = values.pop("_idempotency_token", None)
            result = await _workspace_admin_call(
                ("update_workspace_settings", "update_settings"), values,
                expected_revision=expected,
                confirm_high_trust=True,
                idempotency_token=token,
            )
            row = _as_mapping(result)
            return _auth_json({"settings": settings_view(row.get("settings", result))})
        except Exception as exc:
            return _workspace_admin_error(exc)

    @app.post("/api/workspace-settings/test-brave")
    async def test_workspace_brave(request: Request) -> JSONResponse:
        if failure := _csrf_error(config, request):
            return failure
        if failure := _high_trust_error():
            return failure
        try:
            body = await request.json()
            if body not in ({}, None):
                raise ValueError("test-brave does not accept settings")
            result = _as_mapping(await _workspace_admin_call(("test_brave_search", "test_brave")))
            # The service returns a bounded category only; no key, URL, or
            # response body is accepted into the Admin response.
            return _auth_json({
                "ok": bool(result.get("ok", result.get("success", False))),
                "category": result.get("category", result.get("status", "unknown")),
            })
        except Exception as exc:
            return _workspace_admin_error(exc)

    @app.get("/api/connectors/{connector_id}/credentials")
    async def list_combined_credentials(connector_id: str) -> JSONResponse:
        return await _list_credentials("combined", connector_id)

    @app.post("/api/connectors/{connector_id}/credentials", status_code=201)
    async def create_combined_credential(connector_id: str, request: Request) -> JSONResponse:
        return await _create_credential("combined", connector_id, request)

    @app.get("/api/workspace-connectors/{surface_id}/credentials")
    async def list_workspace_credentials(surface_id: str) -> JSONResponse:
        return await _list_credentials("workspace", surface_id)

    @app.post("/api/workspace-connectors/{surface_id}/credentials", status_code=201)
    async def create_workspace_credential(surface_id: str, request: Request) -> JSONResponse:
        return await _create_credential("workspace", surface_id, request)

    @app.post("/api/connectors/{connector_id}/credentials/{credential_id}/{action}")
    async def combined_credential_action(connector_id: str, credential_id: str, action: str, request: Request) -> JSONResponse:
        if action == "setup":
            return await _credential_setup("combined", connector_id, credential_id, request)
        if action not in {"rotate", "reveal", "revoke", "delete"}:
            raise HTTPException(status_code=404, detail="Credential action was not found")
        if action == "delete":
            # DELETE is the normative method; POST is accepted for clients that
            # cannot send a body with DELETE and remains confirmation-gated.
            return await _credential_mutation("combined", connector_id, credential_id, action, request)
        return await _credential_mutation("combined", connector_id, credential_id, action, request)

    @app.delete("/api/connectors/{connector_id}/credentials/{credential_id}")
    async def delete_combined_credential(connector_id: str, credential_id: str, request: Request) -> JSONResponse:
        return await _credential_mutation("combined", connector_id, credential_id, "delete", request)

    @app.post("/api/workspace-connectors/{surface_id}/credentials/{credential_id}/{action}")
    async def workspace_credential_action(surface_id: str, credential_id: str, action: str, request: Request) -> JSONResponse:
        if action == "setup":
            return await _credential_setup("workspace", surface_id, credential_id, request)
        if action not in {"rotate", "reveal", "revoke", "delete"}:
            raise HTTPException(status_code=404, detail="Credential action was not found")
        return await _credential_mutation("workspace", surface_id, credential_id, action, request)

    @app.delete("/api/workspace-connectors/{surface_id}/credentials/{credential_id}")
    async def delete_workspace_credential(surface_id: str, credential_id: str, request: Request) -> JSONResponse:
        return await _credential_mutation("workspace", surface_id, credential_id, "delete", request)

    # ---------------------------------------------------------------- list

    @app.get("/api/projects")
    async def list_projects() -> dict:
        oauth_status = await _oauth_state()
        connections: list[dict] | None = []
        if oauth_status == "ready" and oauth_client is not None:
            try:
                connections = await oauth_client.list_connections()
            except OAuthServiceUnavailable:
                oauth_status = "unavailable"
                connections = None
        elif oauth_status == "unavailable":
            connections = None
        grant_counts: dict[str, int] = {}
        for connection in connections or []:
            project = connection.get("project")
            if isinstance(project, str):
                grant_counts[project] = grant_counts.get(project, 0) + 1
                continue
            connector = connection.get("connector")
            if isinstance(connector, dict):
                for access in connector.get("projects") or []:
                    name = access.get("name") if isinstance(access, dict) else None
                    if isinstance(name, str):
                        grant_counts[name] = grant_counts.get(name, 0) + 1
        # 13.0 §7.3: `test_mode` is gone from this payload with the
        # `config.test_mode` flag it reported. The 13.0 test window is a
        # gateway-side gate reported by /healthz, not Admin state; no Admin UI
        # read it here (checked before removal).
        return {
            "oauth_status": oauth_status,
            "projects": [
                {
                    "name": p.name,
                    "documents_dir": str(p.documents_dir),
                    # 19.2: what the Projects table shows; equals documents_dir when no root has a display.
                    "documents_display": display_for(str(p.documents_dir)),
                    "data_dir": str(p.data_dir),
                    "enabled": p.enabled,
                    "writable": p.writable,
                    "exclude_from_default_permissions": p.exclude_from_default_permissions,
                    "connected_clients": (
                        None if connections is None else grant_counts.get(p.name, 0)
                    ),
                    "worker_port": p.worker_port,
                    "worker_status": _status_of(p.name),
                }
                for p in registry.projects
            ]
        }

    # ------------------------------------------------------ OAuth connections

    @app.get("/api/oauth/status")
    async def oauth_status() -> dict:
        state = await _oauth_state()
        return {
            "enabled": state != "disabled",
            "status": state,
            "ready": state == "ready",
            "problems": [] if state in {"ready", "disabled"} else ["OAuth service unavailable"],
            "store_path": str(config.oauth_service_store_path or (config.data_root / "oauth-service.sqlite3")),
        }

    @app.get("/api/oauth/grants")
    async def oauth_grants() -> dict:
        state = await _oauth_state()
        if state == "disabled":
            return {"grants": []}
        if state != "ready" or oauth_client is None:
            raise _oauth_unavailable()
        try:
            connections = await oauth_client.list_connections()
        except OAuthServiceUnavailable:
            raise _oauth_unavailable()
        return {"grants": connections}

    @app.delete("/api/oauth/grants/{grant_id}")
    async def revoke_oauth_grant(grant_id: str) -> dict:
        if await _oauth_state() != "ready" or oauth_client is None:
            raise _oauth_unavailable()
        try:
            revoked = await oauth_client.revoke_connection(grant_id)
        except OAuthServiceUnavailable:
            raise _oauth_unavailable()
        if not revoked:
            raise HTTPException(status_code=404, detail="OAuth grant not found")
        log.info("OAuth grant revoked id=%s", grant_id)
        return {"revoked": grant_id}

    @app.delete("/api/oauth/grants")
    async def revoke_all_oauth_grants() -> dict:
        if await _oauth_state() != "ready" or oauth_client is None:
            raise _oauth_unavailable()
        try:
            count = await oauth_client.revoke_all_connections()
        except OAuthServiceUnavailable:
            raise _oauth_unavailable()
        log.warning("All OAuth grants revoked count=%d", count)
        return {"revoked": count}

    # Legacy static-token and debug-token controls were deliberately retired in
    # 11.0. Keep a stable migration response even for ``--test`` callers so an
    # old UI cannot accidentally recreate a production credential path.
    @app.api_route("/api/debug-tokens-mode", methods=["GET", "PUT", "POST", "PATCH", "DELETE"])
    async def retired_debug_tokens_mode() -> JSONResponse:
        return _auth_error(410, "retired", "Debug Tokens Mode was retired in Cognita 11.0; use Authentication static keys")

    # ------------------------------------------------------------- add

    @app.get("/api/document-roots")
    async def document_roots() -> JSONResponse:
        """Documents roots the installer bound in (empty when none, such as in development)."""
        roots = roots_with_displays()
        log.info("Document roots requested count=%d with_display=%d", len(roots),
                 sum(1 for root in roots if root["display"] != root["path"]))
        return JSONResponse({"roots": roots})

    @app.post("/api/projects/path-info")
    async def documents_path_info(body: DocumentsPathProbe, request: Request) -> JSONResponse:
        if failure := _csrf_error(config, request):
            return failure
        if body.root is not None:
            if body.documents_dir is not None:
                return JSONResponse({
                    "detail": "Send either documents_dir or root and folder, not both",
                    "presentation_id": "admin.folder.invalid",
                    "presentation_values": {"folder": body.folder},
                }, status_code=400)
            try:
                probed = await asyncio.to_thread(_probe_root_folder, body.root, body.folder)
            except ValueError as exc:
                log.info("Documents folder probe refused root=%s reason=%s", body.root, exc)
                return JSONResponse({
                    "detail": str(exc),
                    "presentation_id": "admin.folder.invalid",
                    "presentation_values": {"folder": body.folder},
                }, status_code=400)
            return JSONResponse(probed)
        if body.documents_dir is None:
            return JSONResponse({
                "detail": "documents_dir (or root and folder) is required",
                "presentation_id": "admin.folder.path_required",
            }, status_code=400)
        try:
            root, file_count, total_bytes = await asyncio.to_thread(
                _documents_directory_stats, body.documents_dir
            )
        except ValueError as exc:
            candidate = Path(body.documents_dir).expanduser()
            presentation_id = "admin.folder.invalid"
            if candidate.is_absolute():
                if not candidate.exists():
                    presentation_id = "admin.folder.missing"
                elif not candidate.is_dir():
                    presentation_id = "admin.folder.file"
                else:
                    presentation_id = "admin.folder.unreadable"
            return JSONResponse({
                "detail": str(exc),
                "presentation_id": presentation_id,
                "presentation_values": {"folder": display_for(body.documents_dir)},
            }, status_code=400)
        log.info(
            "Documents folder probe succeeded files=%d total_bytes=%d",
            file_count,
            total_bytes,
        )
        return JSONResponse({
            "status": "ok",
            "path": str(root),
            "file_count": file_count,
            "total_bytes": total_bytes,
            "presentation_id": "admin.project.path_checked",
            "presentation_values": {"folder": display_for(str(root))},
        })

    @app.post("/api/projects", status_code=201)
    async def add_project(body: NewProject, request: Request) -> dict:
        if failure := _csrf_error(config, request):
            return failure
        docs = Path(body.documents_dir).expanduser()
        if not docs.is_dir():
            display = display_for(str(docs))
            detail = f"Documents folder does not exist or is not a directory: {display}"
            presentation_id = "admin.folder.missing" if not docs.exists() else "admin.folder.file"
            return JSONResponse(
                {
                    "detail": detail,
                    "presentation_id": presentation_id,
                    "presentation_values": {"folder": display},
                },
                status_code=400,
            )
        data_dir = Path(config.data_root) / body.name
        try:
            project = Project(
                name=body.name,
                documents_dir=docs,
                data_dir=data_dir,
                token_sha256="",
                writable=body.writable,
                exclude_from_default_permissions=body.exclude_from_default_permissions,
            )
        except ValidationError as exc:
            raise HTTPException(status_code=400, detail=_first_error(exc)) from exc

        oauth_was_required = (
            authentication_store.oauth_runtime_required(_enabled_project_names())
            if authentication_store is not None else False
        )
        try:
            registry.add(project)  # raises ValueError on duplicate name
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except AuthenticationPolicyUnavailable as exc:
            raise HTTPException(
                status_code=503, detail="Authentication policy is unavailable"
            ) from exc

        oauth_is_required = (
            authentication_store.oauth_runtime_required(_enabled_project_names())
            if authentication_store is not None else False
        )
        if not await _reconcile_oauth_before_enable(
            oauth_was_required, oauth_is_required
        ):
            registry.remove(project.name)
            raise HTTPException(
                status_code=503,
                detail="OAuth service is not ready; project was not added",
            )

        if engine is not None:
            try:
                await engine.store.ensure_project(project.name)
                engine.apply_extension_policy(project)
                # Load the de-index list BEFORE the first walk. A project re-added
                # under a data_dir that already holds one would otherwise reindex
                # every path it suppresses, silently undoing them.
                engine.deindexed(project)
                asset_service = engine._asset_service_for(project, None)
                await asset_service.recover()
                if engine.watcher is not None:
                    engine.watcher.watch(project, asset_service=asset_service)
                # Attach discovery before the initial scans so direct changes
                # racing project creation cannot precede the observer snapshot.
                engine.start_background_reindex(project, "incremental")  # initial index
                engine.start_background_asset_reconcile(project)
            except Exception as exc:  # rollback the registry entry so state stays consistent
                log.error("Project %s failed to initialize on add: %s", project.name, exc)
                registry.remove(project.name)
                await _reconcile_oauth_after_disable(
                    oauth_is_required,
                    authentication_store.oauth_runtime_required(_enabled_project_names())
                    if authentication_store is not None else False,
                )
                raise HTTPException(
                    status_code=500, detail=f"Failed to initialize project: {exc}"
                ) from exc

        log.info("Project %s added; connector access is configured separately", project.name)
        return {
            "name": project.name,
            "exclude_from_default_permissions": project.exclude_from_default_permissions,
            "worker_status": _status_of(project.name),
        }

    # Legacy project token controls are gone. A 410 is returned rather than a
    # mode-dependent 403 so test mode cannot accidentally preserve the old
    # credential namespace.
    @app.api_route("/api/projects/{name}/api-key", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
    async def retired_project_api_key(name: str) -> JSONResponse:
        return _auth_error(410, "retired", "Project API keys were retired in Cognita 11.0; use Authentication static keys")

    @app.api_route("/api/projects/{name}/token", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
    async def retired_project_token(name: str) -> JSONResponse:
        return _auth_error(410, "retired", "Project tokens were retired in Cognita 11.0; use Authentication static keys")

    # --------------------------------------------------------------- status

    @app.patch("/api/projects/{name}")
    async def update_project_settings(
        name: str, body: ProjectSettingsUpdate, request: Request,
    ) -> dict:
        if failure := _csrf_error(config, request):
            return failure
        try:
            project = registry.update_settings(
                name,
                exclude_from_default_permissions=body.exclude_from_default_permissions,
            )
        except KeyError:
            raise HTTPException(status_code=404, detail="Project not found")
        except (OSError, TypeError, ValueError) as exc:
            log.error(
                "Project settings persistence failed project=%s error=%s",
                name, type(exc).__name__,
            )
            raise HTTPException(
                status_code=500, detail="Project settings could not be persisted"
            ) from exc
        return {
            "name": project.name,
            "exclude_from_default_permissions": project.exclude_from_default_permissions,
        }

    @app.get("/api/projects/{name}/status")
    async def project_status(name: str) -> dict:
        project = _require(name)
        status = _status_of(name)
        out = {
            "name": name,
            "worker_status": status,
            # Core mode is in-process: there has been no port since 4.0. The key
            # stays because the Admin UI reads it.
            "worker_port": None,
            "enabled": project.enabled,
            "writable": project.writable,
            "exclude_from_default_permissions": project.exclude_from_default_permissions,
            "doc_count": None,
            "chunk_count": None,
            # Installer design 7.3: the last background reindex's error, e.g. "The
            # documents folder ... is empty or not mounted. Nothing was removed."
            "reindex_error": None,
            # Additive local diagnostics; never part of the public MCP schema.
            "watcher": None,
        }
        watcher = getattr(engine, "watcher", None) if engine is not None else None
        if watcher is not None:
            try:
                health = getattr(watcher, "health_snapshot", None) or getattr(watcher, "health", None)
                if callable(health):
                    watcher_status = health(name)
                    source_guard = getattr(engine, "source_guard", None)
                    if source_guard is not None and source_guard.enabled and isinstance(watcher_status, dict):
                        watcher_status = dict(watcher_status)
                        watcher_status.pop("last_event", None)
                        summary = watcher_status.get("last_summary")
                        if isinstance(summary, dict):
                            watcher_status["last_summary"] = {
                                key: summary.get(key)
                                for key in ("indexed", "metadata_refreshed", "skipped", "removed", "failed")
                                if key in summary
                            }
                    out["watcher"] = watcher_status
            except (OSError, RuntimeError, TypeError, ValueError) as exc:
                log.debug("status: watcher health unavailable for %s: %s", name, type(exc).__name__)
        if engine is not None and status == WorkerStatus.RUNNING:
            try:
                payload = await _engine_tool(name, "get_index_stats")
                stats = (payload or {}).get("stats", {}) if isinstance(payload, dict) else {}
                out["doc_count"] = stats.get("total_documents")
                out["chunk_count"] = stats.get("total_chunks")
            except (OSError, RuntimeError, TypeError, ValueError) as exc:
                log.debug("status: stats unavailable for %s: %s", name, exc)
            try:
                reindex = await _engine_tool(name, "get_reindex_status")
                block = (reindex or {}).get("reindex", {}) if isinstance(reindex, dict) else {}
                error = block.get("last_error") if isinstance(block, dict) else None
                if isinstance(error, str) and error:
                    out["reindex_error"] = error
                    # DEBUG, not INFO: Admin polls this endpoint, and the reindex itself
                    # already logged the error once when it happened.
                    log.debug("status: reindex error for %s reported to Admin: %s", name, error)
            except (OSError, RuntimeError, TypeError, ValueError) as exc:
                log.warning("status: reindex status unavailable for %s: %s: %s", name, type(exc).__name__, exc)
        return out

    # --------------------------------------------------------------- reindex

    @app.post("/api/projects/{name}/watcher/clear-queue")
    async def clear_watcher_queue(name: str, request: Request) -> dict:
        if failure := _csrf_error(config, request):
            return failure
        _require(name)
        watcher = getattr(engine, "watcher", None) if engine is not None else None
        clear_queue = getattr(watcher, "clear_queue", None)
        if not callable(clear_queue):
            raise HTTPException(status_code=503, detail="Watcher is unavailable")
        result = await clear_queue(name)
        return result

    @app.post("/api/projects/{name}/reindex")
    async def reindex(name: str) -> dict:
        _require(name)
        status = _status_of(name)
        if status != WorkerStatus.RUNNING:
            log.info("reindex refused project=%s status=%s", name, status)
            raise HTTPException(
                status_code=503, detail=f"Project {name!r} is disabled; cannot reindex."
            )
        result = await _engine_tool(name, "reindex_documents", {})
        return {"name": name, "result": result}

    # --------------------------------------------------------------- remove

    @app.delete("/api/projects/{name}")
    async def remove_project(
        name: str, deleteData: bool = Query(False)
    ) -> dict:
        project = _require(name)
        oauth_was_required = (
            authentication_store.oauth_runtime_required(_enabled_project_names())
            if authentication_store is not None else False
        )
        if engine is not None:
            await engine.detach_project(name)
        # Remove explicit connector memberships first.  If the process is
        # interrupted after this write but before registry removal, access is
        # denied; a later same-named project cannot inherit selected access.
        try:
            connector_store.remove_project_references(
                name, after_persist=lambda: registry.remove(name)
            )
        except PolicyUnavailable:
            log.error("Project deletion blocked: connector policy unavailable project=%s", name)
            raise HTTPException(status_code=503, detail="Connector policy is unavailable")

        await _reconcile_oauth_after_disable(
            oauth_was_required,
            authentication_store.oauth_runtime_required(_enabled_project_names())
            if authentication_store is not None else False,
        )

        data_deleted = False
        if deleteData:
            data_deleted = _safe_delete_index(config, project)
            if engine is not None:
                # 4.0: the index lives in Postgres, not under data_dir
                try:
                    await engine.store.drop_project(name)
                    data_deleted = True
                except (OSError, RuntimeError, TypeError, ValueError) as exc:
                    log.warning("drop_project(%s) failed: %s", name, exc)
        log.info("Project %s removed (deleteData=%s, deleted=%s)", name, deleteData, data_deleted)
        return {"removed": name, "dataDeleted": data_deleted}

    # ------------------------------------------------------------ folder picker

    @app.get("/api/browse")
    async def browse(request: Request, path: str = Query("")) -> dict:
        """List subdirectories under `path` to help pick a documents_dir.

        Since 3.0, Admin can bind to a non-loopback address. This route can
        enumerate directories and drive roots readable by the service user,
        while the Add-project form no longer calls it. Restrict it to loopback
        so remote Admin clients cannot browse the server filesystem.
        """
        client = request.client.host if request.client else ""
        if not is_loopback(client):
            raise HTTPException(
                status_code=403,
                detail=("/api/browse is available to loopback callers only — it lists the "
                        "server's own filesystem. Type the documents_dir path directly."),
            )
        if not path:
            return {"path": "", "parent": None, "dirs": _drive_roots()}
        base = Path(path).expanduser()
        if not base.is_dir():
            raise HTTPException(status_code=400, detail=f"Not a directory: {base}")
        try:
            dirs = sorted(
                (str(p) for p in base.iterdir() if p.is_dir()),
                key=str.lower,
            )
        except OSError as exc:
            raise HTTPException(status_code=400, detail=f"Cannot read {base}: {exc}") from exc
        parent = str(base.parent) if base.parent != base else None
        return {"path": str(base), "parent": parent, "dirs": dirs}

    # ------------------------------------------------------- GPU acceleration

    def _acceleration_profile() -> str:
        # This is deployment metadata, not an Admin-owned policy override.  A
        # malformed value fails closed to the CPU profile rather than claiming
        # that a device is available. 15.0: the parsing (and the one warning line
        # for an unknown value) lives in `acceleration_profiles.current_profile`,
        # shared with `__main__`; the store still takes the profile NAME.
        return current_profile().name

    @app.get("/api/settings/gpu-acceleration")
    async def get_gpu_acceleration() -> JSONResponse:
        try:
            return _auth_json(acceleration_store.status(profile=_acceleration_profile()))
        except AccelerationConfigurationError:
            return _auth_error(
                503, "configuration_invalid",
                "acceleration configuration is invalid; restore the validated acceleration.yaml.previous while the service is stopped",
            )

    @app.patch("/api/settings/gpu-acceleration")
    async def patch_gpu_acceleration(request: Request) -> JSONResponse:
        if failure := _csrf_error(config, request):
            return failure
        try:
            body = await request.json()
            if not isinstance(body, dict):
                raise ValueError("request body must be a JSON object")
            allowed = {"expected_revision", "idempotency_token", "knowledge", "ocr"}
            unknown = sorted(set(body) - allowed)
            if unknown:
                raise ValueError("unknown acceleration field(s): " + ", ".join(unknown))
            expected = body.get("expected_revision")
            token = body.get("idempotency_token")
            if isinstance(expected, bool) or not isinstance(expected, int) or expected < 0:
                raise ValueError("expected_revision must be a nonnegative integer")
            if not isinstance(token, str) or not token or len(token) > 128:
                raise ValueError("idempotency_token must be a canonical UUID")
            try:
                if str(UUID(token)) != token:
                    raise ValueError
            except (ValueError, AttributeError) as exc:
                raise ValueError("idempotency_token must be a canonical UUID") from exc
            candidate = {"knowledge": body.get("knowledge"), "ocr": body.get("ocr")}
            saved = acceleration_store.update(expected, candidate, token)
            status = acceleration_store.status(profile=_acceleration_profile())
            log.info(
                "acceleration policy saved old_revision=%d new_revision=%d profile=%s restart_required=%s",
                expected, saved.revision, _acceleration_profile(),
                status["restart"]["required"],
            )
            return _auth_json(status)
        except AccelerationConflict as exc:
            return _auth_error(409, "revision_conflict", str(exc), configured=exc.current.public())
        except (ValueError, TypeError) as exc:
            return _auth_error(400, "invalid_acceleration_policy", str(exc))
        except AccelerationConfigurationError:
            return _auth_error(503, "configuration_invalid", "acceleration configuration is invalid")

    @app.post("/api/settings/gpu-acceleration/verify")
    async def verify_gpu_acceleration(request: Request) -> JSONResponse:
        if failure := _csrf_error(config, request):
            return failure
        try:
            result = await asyncio.to_thread(
                acceleration_store.verify, profile=_acceleration_profile()
            )
            log.info(
                "acceleration verification completed profile=%s state=%s cards=%d",
                _acceleration_profile(), result.get("state"), len(result.get("cards", [])),
            )
            return _auth_json(result)
        except RuntimeError as exc:
            if str(exc) == "verification_busy":
                return _auth_error(409, "verification_busy", "an acceleration verification is already running")
            raise
        except AccelerationConfigurationError:
            return _auth_error(503, "configuration_invalid", "acceleration configuration is invalid")

    # ------------------------------------------------------------------- UI

    @app.get("/")
    async def index(request: Request) -> HTMLResponse:
        theme = _theme_from(request)
        # Unauthenticated (with auth on) -> the login page; otherwise the app.
        # Both honor the persisted theme so login and admin don't flip palettes.
        if admin_auth_configured(config) and not read_session_user(
            config, request.cookies.get(SESSION_COOKIE)
        ):
            return _render_page("login.html", theme, _admin_locale(request))
        return _render_page("index.html", theme, _admin_locale(request))

    app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")
    return app


# --------------------------------------------------------------------- helpers


def _first_error(exc: ValidationError) -> str:
    errs = exc.errors()
    if errs:
        return errs[0].get("msg", "invalid input")
    return "invalid input"


def _safe_delete_index(config: CognitaConfig, project: Project) -> bool:
    """Delete a project's managed index dir, guarded to stay under data_root.

    NEVER touches documents_dir (source files). Returns True if something was
    deleted. A path that resolves outside data_root is refused (defensive).
    """
    data_root = Path(config.data_root).resolve()
    target = Path(project.data_dir).resolve()
    try:
        target.relative_to(data_root)
    except ValueError:
        log.error(
            "Refusing to delete %s: not under data_root %s (documents are never touched)",
            target, data_root,
        )
        return False
    if target == data_root:
        log.error("Refusing to delete data_root itself (%s)", data_root)
        return False
    if target.is_dir():
        shutil.rmtree(target, ignore_errors=True)
        return True
    return False


def _drive_roots() -> list[str]:
    """Best-effort list of filesystem roots for the browse helper."""
    import string
    import sys

    if sys.platform == "win32":
        return [f"{d}:\\" for d in string.ascii_uppercase if Path(f"{d}:\\").exists()]
    return ["/"]
