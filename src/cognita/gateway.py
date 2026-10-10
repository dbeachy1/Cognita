"""Public MCP gateway app (DESIGN-9.0-UNIFIED-CONNECTORS.md §4-§5).

Bearer-token auth -> exact connector routing -> explicit project policy ->
streaming reverse proxy to the project's worker.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import hmac
import json
import logging
import re
import time
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from fastapi import FastAPI, Request, Response
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse

from . import __version__
from .books.caller_context import BookCallerContext, CURRENT_BOOK_CALLER
from .auth_policy import (
    SELF_TEST_API_KEY,
    SELF_TEST_PRINCIPAL_KIND,
    SELF_TEST_PRINCIPAL_LABEL,
    SELF_TEST_PROJECT_NAME,
    SelfTestModeGate,
    is_self_test_principal,
    self_test_principal_id,
    self_test_principal_matches,
)
from .bridge import bridge_tool_result
from .config import OAUTH_SCOPE, CognitaConfig
from .connectors import (
    PUBLIC_CONTRACT_VERSION,
    ConnectorConfig,
    ConnectorDefinition,
    ConnectorPolicyError,
    ConnectorStore,
    RouteResource,
    build_connector_url,
    is_supported_route,
    parse_connector_path,
    parse_route_path,
    resolve_project_access,
)
from .mcp_protocol import (
    INVALID,
    NO_ID,
    NOTIFICATION,
    REQUEST,
    BodyParseError,
    MessageKind,
    classify_message,
    error_body,
    log_safe,
    negotiate_protocol_version,
    parse_body,
    unencodable_text_reply,
)
from .oauth import (
    RejectionLogCoalescer,
    _client_ip,
    _rate_limited,
    oauth_request_diagnostics,
)
from .oauth_service_client import OAuthServiceClient, OAuthServiceUnavailable
from .proxy import (
    BRIDGE_TOOL_NAMES,
    CONNECTOR_BATCH_TOOL_NAME,
    LIST_PROJECTS_TOOL_NAME,
    PUBLIC_TOOL_NAMES,
    WORKSPACE_TOOL_NAMES,
    _payload_from_response,
    make_client,
    proxy_mcp,
    public_tool_catalog,
    public_tool_names_for_contract,
    public_tools_response,
    workspace_tool_catalog,
    workspace_tool_names,
)
from .public_url import PublicBaseURLStore
from .readonly import is_tool_allowed_remote
from .registry import Registry
from .result_contracts import (
    build_tool_result, is_known_tool, normalize_legacy_error_payload, refusal_payload,
)
from .selftest import SELFTEST_TOOL_NAME, select_self_test_plan
from .workspace import workspace_tool_result
from .workspace_selftest import WORKSPACE_SELFTEST_TOOL_NAME, workspace_selftest_plan

log = logging.getLogger("cognita.gateway")

# Redact long token-like path segments before logging (never log secrets).
_SECRET_SEG = re.compile(r"[A-Za-z0-9_-]{16,}")
_DISCOVERY_LEAVES = frozenset({"openid-configuration", "oauth-authorization-server"})
_DISCOVERY_PREFIX = "/.well-known/"
_PROTECTED_RESOURCE_METADATA_PREFIX = "/.well-known/oauth-protected-resource"
_STATIC_AUTH_ERROR_AT = 0.0
_CONNECTOR_ICON_ROUTE = "/assets/cognita-icon-512.png"
_CONNECTOR_ICON_FILE = Path(__file__).with_name("web") / "cognita-icon-512.png"
_AUTHORIZATION_MAX_BYTES = 8 * 1024
_STATIC_KEY_PREFIX = "cog_sk_v1_"
# A1 (DESIGN-12.18 §3.1): wait_ms > 0 calls run WorkspaceManager.execute (a
# blocking, potentially multi-second call) off the event loop here so a wait
# never freezes the whole server. Ten threads, fixed -- an eleventh
# concurrent wait queues in the executor; nothing else is built around this.
_WORKSPACE_WAIT_EXECUTOR = ThreadPoolExecutor(max_workers=10, thread_name_prefix="workspace-wait")
_V2_STATIC_KEY_PREFIX = "cog_sk_v2_"


try:  # The policy worker lands independently; keep this branchable meanwhile.
    from .auth_policy import AuthPrincipal
except ImportError:  # pragma: no cover - replaced when policy store is present
    @dataclass(frozen=True, slots=True)
    class AuthPrincipal:
        """Immutable request-local proof; raw credentials never enter state."""

        kind: str
        project_name: str | None = None
        key_id: str | None = None
        oauth_resource: str | None = None


def _with_invalid_token(challenge: str) -> str:
    """16.1.3 (RFC 6750 section 3.1): a 401 for a credential that WAS presented
    and is invalid or expired names the error. Appended after the existing
    parameters, so `resource_metadata` and `scope` (and the realm) stay exactly
    as they were; a request with no credential keeps the plain challenge."""
    return f'{challenge}, error="invalid_token"'


def _valid_mcp_resource_path(path: str) -> bool:
    """Return whether *path* is a canonical connector resource path."""
    return parse_connector_path(path) is not None


def _suppress_wire_bodies(raw: bytes | None) -> bool:
    """Conservatively suppress captured bodies for source-bearing tools."""
    if raw is None:
        return True
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeDecodeError, RecursionError):
        # 16.1.3: a body nested past the parser's recursion limit is a parse
        # error like any other unparseable body (bodies suppressed).
        return True
    from .books.schemas import ALL_ADDITIVE_TOOL_NAMES

    def contains(candidate: Any) -> bool:
        if isinstance(candidate, list):
            return any(contains(item) for item in candidate)
        if not isinstance(candidate, dict):
            return False
        if candidate.get("method") == "batch":
            return True
        if candidate.get("method") == "tools/call":
            params = candidate.get("params")
            if not isinstance(params, dict) or not isinstance(params.get("name"), str):
                return True
            return params["name"] in ALL_ADDITIVE_TOOL_NAMES
        return any(contains(item) for item in candidate.values() if isinstance(item, (dict, list)))

    return contains(value)


def _valid_issuer_suffix(path: str) -> bool:
    """Return whether *path* is a non-empty RFC-style issuer path suffix."""
    if not path.startswith("/"):
        return False
    segments = path[1:].split("/")
    return bool(segments and all(segment not in {"", ".", ".."} for segment in segments))


def _is_expected_discovery_probe(path: str) -> bool:
    """Recognize only known OAuth/OIDC metadata fallback URL shapes.

    RFC 8414/OIDC clients may append an issuer path after the well-known leaf;
    Cognita clients also retry from an MCP protected-resource path and append
    the well-known leaf there. Unknown leaves and malformed MCP paths remain
    warning-level diagnostics.
    """
    for leaf in _DISCOVERY_LEAVES:
        marker = f"{_DISCOVERY_PREFIX}{leaf}"
        if path == marker:
            return True
        if path.startswith(marker + "/"):
            suffix = path[len(marker):]
            if suffix.startswith("/mcp"):
                return _valid_mcp_resource_path(suffix)
            return _valid_issuer_suffix(suffix)

        suffix_marker = f"/.well-known/{leaf}"
        if not path.endswith(suffix_marker):
            continue
        resource = path[:-len(suffix_marker)]
        if _valid_mcp_resource_path(resource):
            return True
        if resource.startswith(_PROTECTED_RESOURCE_METADATA_PREFIX):
            return _valid_mcp_resource_path(
                resource[len(_PROTECTED_RESOURCE_METADATA_PREFIX):]
            )
    return False


def _log_static_auth_disabled() -> None:
    """Report release-mode static auth without allowing request spam to fill logs."""
    global _STATIC_AUTH_ERROR_AT
    now = time.monotonic()
    if now - _STATIC_AUTH_ERROR_AT >= 60:
        log.error(
            "Static API-key authentication was attempted in RELEASE mode and ignored; "
            "use OAuth, or restart explicitly with `cognita serve --test` for testing"
        )
        _STATIC_AUTH_ERROR_AT = now


def _embed_health(config: CognitaConfig, engine) -> dict:
    """What is doing the embedding, and can it (§14.7).

    Deliberately says WHY the GPU is off rather than just that it is: "gpu:
    disabled" and "gpu: enabled but no worker environment" and "gpu: enabled,
    no device qualified" want completely different responses, and a single
    false is the answer that sends someone hunting.

    Never raises: /healthz answering is worth more than /healthz being complete,
    and a probe fault must not take liveness down with it.
    """
    out: dict = {"cpu_provider": "CPUExecutionProvider"}
    try:
        if not getattr(config, "gpu_enabled", False):
            out["gpu"] = "disabled"
            return out
        if not getattr(config, "gpu_venv_python", ""):
            out["gpu"] = "enabled but gpu_venv_python is unset"
            return out
        from .acceleration_profiles import (
            current_profile,
            effective_max_busy_percent,
            gpu_settings_profile,
        )
        from .gpu_host import LEASE
        from .gpu_probe import (
            batch_ceiling_gb,
            default_probe,
            gate_devices,
            qualifying,
            resolve_cards,
            skipped_summary,
        )

        # 15.0: the probe and the ceiling follow the deployment's acceleration
        # profile (NVML for `nvidia`, today's sysfs logic otherwise).
        profile = current_profile()
        devices = default_probe(profile).devices()
        selection = resolve_cards(
            devices,
            getattr(config, "gpu_cards", "all"),
            config.gpu_device_ids,
        )
        results = gate_devices(
            devices,
            # DERIVED, like the walk's — this was a literal 3.62 here and a
            # separate literal 3.62 in retrieval.py, so /healthz could report
            # `gpu: ready` for a batch size whose real ceiling no device met.
            # Two copies of a measured constant with nothing binding them is how
            # a health endpoint comes to disagree with the thing it reports on.
            batch_ceiling_gb=batch_ceiling_gb(
                getattr(config, "gpu_batch_size", 64) or 64, profile
            ),
            reserve_vram_gb=config.gpu_reserve_vram_gb,
            max_busy_percent=effective_max_busy_percent(
                config.gpu_max_busy_percent, profile
            ),
            pinned=selection.pinned,
            pinned_by=selection.source,
        )
        out["gpu"] = "ready" if qualifying(results) else "no device qualifies"
        # 15.0: `gpu_provider` defaults to "" (the profile's), so report the
        # RESOLVED short name — /healthz must say what the worker will use.
        out["gpu_provider"] = (
            config.gpu_provider or gpu_settings_profile(profile).provider_key
        )
        out["gpu_cards"] = selection.source
        # 6.4: the `card` index is what a user types into `gpu_cards`, so it is
        # reported beside every device — and `devices_skipped` carries it too,
        # because the card someone wants to exclude is usually the one already
        # failing the gate.
        index_of = {d.pci_address: i for i, d in enumerate(devices)}
        out["devices"] = [
            {"card": index_of.get(d.pci_address), "sysfs": d.sysfs_name,
             "pci": d.pci_address,
             "vram_free_gb": round(d.vram_free_gb, 2)}
            for d in qualifying(results)
        ]
        if skipped_summary(results):
            out["devices_skipped"] = skipped_summary(results)
        if selection.warnings:
            # A misconfigured index degrades to the CPU silently everywhere
            # else. This is the one place it is visible without reading logs.
            out["gpu_cards_warnings"] = selection.warnings
        if LEASE.holder:
            out["gpu_lease"] = "held"
        # 6.2: a held lease no longer means "a job is running" — a pool parked
        # warm holds it while doing nothing at all. Reporting only the lease
        # would send someone hunting for a walk that finished 20 seconds ago, so
        # say which of the two it is. `status()` never blocks (see gpu_warm).
        from .gpu_warm import WARM

        warm = WARM.status()
        if warm.get("warm"):
            out["gpu_warm"] = warm
    except Exception as exc:  # pragma: no cover - liveness must survive this
        out["gpu"] = f"probe error: {type(exc).__name__}"
    return out


def create_gateway_app(
    config: CognitaConfig, registry: Registry, engine=None,
    oauth_client: OAuthServiceClient | None = None,
    connector_store: ConnectorStore | None = None,
    auth_policy: Any = None,
    authentication_policy: Any = None,
    authentication_store: Any = None,
    workspace_connector_store: Any = None,
    credential_store: Any = None,
    workspace_service: Any = None,
    workspace_manager: Any = None,
    bridge_service: Any = None,
    public_url_store: PublicBaseURLStore | None = None,
    credential_admission: Callable[[str], bool | None] | None = None,
) -> FastAPI:
    """engine (a LocalEngineHost) routes tool traffic in-process over ASGI to the
    retrieval core. With no engine there is nothing to route to and every project
    call answers project_unavailable. (14.0.0: the 3.x worker mode, selected by a
    `supervisor` argument, was removed.)"""
    if auth_policy is None:
        auth_policy = authentication_store or authentication_policy
    # ``workspace_manager`` is retained as a descriptive alias for callers
    # constructing the domain service directly.  The gateway never talks to
    # the private broker itself.
    workspace_service = workspace_service or workspace_manager
    # Host capability is fixed by the paired runtime URL and bearer-file
    # settings consumed by __main__. Broker health never changes this value.
    workspace_configured = workspace_service is not None
    public_url_store = public_url_store or PublicBaseURLStore(config)
    config.public_base_url = public_url_store.effective()
    app = FastAPI(title="Cognita MCP Gateway", version=__version__, docs_url=None, redoc_url=None)
    app.state.workspace_configured = workspace_configured
    client = engine.make_client() if engine is not None else make_client()
    connector_store = connector_store or ConnectorStore(config.connectors_path)
    mcp_rejection_logs = RejectionLogCoalescer()
    register_attempts: dict[str, deque[float]] = defaultdict(deque)
    # 13.0 §7.3. The window starts when the app is built, which is process
    # startup: there is no renewal and no deadline argument. The gate is on
    # app.state so /healthz, tests and a future diagnostic can read it without
    # reaching into this closure.
    test_mode_gate = SelfTestModeGate(bool(getattr(config, "self_test_mode", False)))
    app.state.cognita_test_mode_gate = test_mode_gate

    def _public_base_url() -> str:
        """Read the Admin-selected URL for each metadata/connector response."""
        value = public_url_store.effective()
        config.public_base_url = value
        return value

    def _authentication_snapshot() -> Any:
        """Read the parent-owned immutable auth policy snapshot.

        The policy store is intentionally injected rather than constructed by
        the gateway.  A tiny adapter here accepts both the production store
        and focused test fakes while keeping persistence and policy mutation
        outside this process boundary.
        """
        if auth_policy is None:
            return None
        reader = getattr(auth_policy, "snapshot", None)
        if reader is None:
            reader = getattr(auth_policy, "read_snapshot", None)
        if reader is None:
            return auth_policy
        try:
            return reader(registry.projects)
        except TypeError:
            return reader()

    def _snapshot_projects(snapshot: Any) -> list[Any]:
        if snapshot is None:
            return list(registry.projects)
        value = getattr(snapshot, "projects", None)
        if value is None:
            value = getattr(snapshot, "project_policies", None)
        if value is None:
            value = getattr(snapshot, "policies", None)
        if value is None and isinstance(snapshot, dict):
            value = snapshot.get("projects")
        if isinstance(value, dict):
            return list(value.values())
        return list(value) if isinstance(value, (list, tuple, set)) else list(registry.projects)

    def _policy_project(snapshot: Any, name: str) -> Any:
        mapping = None
        if snapshot is not None:
            mapping = getattr(snapshot, "projects", None)
            if mapping is None:
                mapping = getattr(snapshot, "project_policies", None)
            if mapping is None:
                mapping = getattr(snapshot, "policies", None)
        if isinstance(mapping, dict) and name in mapping:
            return mapping[name]
        if isinstance(snapshot, dict) and isinstance(snapshot.get("projects"), dict):
            return snapshot["projects"].get(name)
        finder = getattr(snapshot, "project", None) if snapshot is not None else None
        if callable(finder):
            try:
                found = finder(name)
                if found is not None:
                    return found
            except (KeyError, TypeError, ValueError):
                pass
        for item in _snapshot_projects(snapshot):
            item_name = item.get("name") if isinstance(item, dict) else getattr(item, "name", None)
            if item_name == name:
                return item
        return None

    def _oauth_enabled_for(snapshot: Any, name: str) -> bool:
        if auth_policy is None:
            return bool(config.oauth_enabled)
        for owner in (snapshot, auth_policy):
            if owner is None:
                continue
            for method_name in (
                "effective_oauth_enabled", "oauth_enabled_for", "is_oauth_enabled",
                "effective_oauth",
            ):
                method = getattr(owner, method_name, None)
                if callable(method):
                    try:
                        return bool(method(name))
                    except TypeError:
                        try:
                            return bool(method(name, snapshot))
                        except (TypeError, KeyError, ValueError):
                            try:
                                return bool(method(_policy_project(snapshot, name)))
                            except (TypeError, KeyError, ValueError):
                                pass
            item = _policy_project(snapshot, name)
            if item is not None:
                if isinstance(item, dict):
                    if "effective_oauth_enabled" in item:
                        return bool(item["effective_oauth_enabled"])
                    mode = item.get("oauth_mode", "inherit")
                else:
                    if hasattr(item, "effective_oauth_enabled"):
                        return bool(item.effective_oauth_enabled)
                    mode = getattr(item, "oauth_mode", "inherit")
                global_value = getattr(owner, "oauth_enabled", None)
                if global_value is None:
                    global_value = getattr(owner, "global_oauth_enabled", None)
                if global_value is None:
                    global_policy = getattr(owner, "global", None)
                    global_value = getattr(global_policy, "oauth_enabled", None) if global_policy else None
                if global_value is None:
                    global_policy = getattr(owner, "global_", None)
                    global_value = getattr(global_policy, "oauth_enabled", None) if global_policy else None
                if global_value is None and isinstance(owner, dict):
                    global_value = owner.get("global", {}).get("oauth_enabled")
                if global_value is None:
                    global_value = bool(config.oauth_enabled)
                return bool(global_value) if mode == "inherit" else mode == "enabled"
        return bool(config.oauth_enabled)

    def _oauth_potentially_enabled(snapshot: Any = None) -> bool:
        if auth_policy is None:
            return bool(config.oauth_enabled)
        try:
            names = []
            for project in registry.projects:
                if not getattr(project, "enabled", True):
                    continue
                name = getattr(project, "name", None)
                if name is None and isinstance(project, dict):
                    name = project.get("name")
                if name is not None:
                    names.append(name)
            return any(
                _oauth_enabled_for(snapshot, name) for name in names
            )
        except (AttributeError, KeyError, TypeError, ValueError):
            return False

    def _credential_entries(snapshot: Any) -> list[Any]:
        """Return all active static-key slots; never short-circuit matching."""
        for owner in (snapshot, auth_policy):
            if owner is None:
                continue
            entries = getattr(owner, "static_credentials", None)
            if entries is None:
                entries = getattr(owner, "static_keys", None)
            if entries is None:
                entries = getattr(owner, "static_slots", None)
            if entries is None:
                entries = getattr(owner, "credentials", None)
            if entries is None and isinstance(owner, dict):
                entries = owner.get("static_credentials") or owner.get("static_keys")
                if entries is None:
                    glob = owner.get("global", {}).get("static_key")
                    if glob:
                        entries = [("static_global", None, glob)]
                    else:
                        entries = []
                    for name, value in (owner.get("projects") or {}).items():
                        key = value.get("static_key") if isinstance(value, dict) else None
                        if key:
                            entries.append(("static_project", name, key))
            if entries is None:
                global_policy = getattr(owner, "global_", None)
                projects = getattr(owner, "projects", None)
                if global_policy is not None and isinstance(projects, dict):
                    entries = []
                    global_key = getattr(global_policy, "static_key", None)
                    if global_key is not None:
                        entries.append(("static_global", None, global_key))
                    for name, value in projects.items():
                        key = getattr(value, "static_key", None)
                        if key is not None:
                            entries.append(("static_project", name, key))
            if entries is not None:
                if isinstance(entries, dict):
                    entries = list(entries.values())
                return list(entries)
        return []

    @staticmethod
    def _entry_values(entry: Any) -> tuple[tuple[str | None, str | None, str | None], str | None]:
        if isinstance(entry, (tuple, list)) and len(entry) >= 3:
            kind, project, record = entry[0], entry[1], entry[2]
        else:
            kind = getattr(entry, "kind", None) or getattr(entry, "scope", None)
            project = getattr(entry, "project_name", None)
            record = entry
        if isinstance(entry, dict):
            kind = entry.get("kind", entry.get("scope", kind))
            project = entry.get("project_name", entry.get("project", project))
            record = entry
        if isinstance(record, dict):
            digest = record.get("digest") or record.get("key_digest") or record.get("sha256")
            key_id = record.get("key_id")
        else:
            digest = (getattr(record, "digest", None)
                      or getattr(record, "key_digest", None)
                      or getattr(record, "sha256", None))
            key_id = getattr(record, "key_id", None)
        return (str(kind) if kind is not None else None,
                str(project) if project is not None else None,
                str(digest) if digest is not None else None), key_id

    def registration_rate_limited(request: Request) -> bool:
        limited = _rate_limited(register_attempts, _client_ip(request), 10, 3600)
        while len(register_attempts) > 1024:
            register_attempts.pop(next(iter(register_attempts)))
        return limited

    def log_mcp_rejection(connector_id: str, category: str, request: Request) -> None:
        key = ("mcp", category, connector_id)
        suppressed = mcp_rejection_logs.note(key)
        if suppressed is None:
            return
        suffix = f" suppressed={suppressed}" if suppressed else ""
        level = logging.INFO if category == "missing_authorization" else logging.WARNING
        log.log(
            level,
            "MCP connector rejected connector_id=%s method=%s category=%s%s %s",
            connector_id, request.method, category, suffix, oauth_request_diagnostics(request),
        )

    async def _oauth_state() -> str:
        try:
            policy_snapshot = _authentication_snapshot()
        except (OSError, TypeError, ValueError):
            return "unavailable"
        if not _oauth_potentially_enabled(policy_snapshot):
            return "disabled"
        if oauth_client is None:
            return "unavailable"
        checker = getattr(oauth_client, "check_readiness", None)
        if checker is not None:
            try:
                return "ready" if await checker() else "unavailable"
            except Exception:
                return "unavailable"
        linked = getattr(oauth_client, "supervisor", None)
        if linked is None:
            return "ready"
        snapshot = linked.snapshot
        state = getattr(snapshot, "state", snapshot)
        return "ready" if str(getattr(state, "value", state)).lower() == "ready" else "unavailable"

    def _extract_bearer(request: Request) -> tuple[str | None, str]:
        """Parse exactly one strict Authorization Bearer credential."""
        values = request.headers.getlist("authorization")
        if not values:
            return None, "missing_authorization"
        if len(values) != 1:
            return None, "duplicate_authorization"
        value = values[0]
        try:
            if len(value.encode("utf-8", "strict")) > _AUTHORIZATION_MAX_BYTES:
                return None, "authorization_too_large"
        except UnicodeError:
            return None, "malformed_authorization"
        if any(ord(char) < 0x20 or ord(char) == 0x7F for char in value):
            return None, "malformed_authorization"
        if "," in value or len(value) < 7 or value[:6].casefold() != "bearer" or value[6] != " ":
            return None, "malformed_authorization"
        if len(value) <= 7:
            return None, "malformed_authorization"
        token = value[7:]
        # The token grammar intentionally has no whitespace around or within
        # the credential.  In particular, do not normalize a comma-joined or
        # proxy-folded header into a credential.
        if token != token.strip() or any(char.isspace() for char in token):
            return None, "malformed_authorization"
        return token, ""

    def _static_verify(snapshot: Any, token: str) -> tuple[str | None, str | None, str | None]:
        """Verify a static candidate with a complete constant-time scan."""
        immutable_policy = (
            snapshot is not None
            and getattr(snapshot, "global_", None) is not None
            and isinstance(getattr(snapshot, "projects", None), dict)
        )
        verifier = (
            None if immutable_policy
            else getattr(snapshot, "verify_static_key", None) if snapshot is not None else None
        )
        if not immutable_policy and not callable(verifier) and auth_policy is not None:
            verifier = getattr(auth_policy, "verify_static_key", None)
            if verifier is None:
                verifier = getattr(auth_policy, "verify_static_candidate", None)
            if verifier is None:
                verifier = getattr(auth_policy, "verify_static", None)
            if verifier is None:
                verifier = getattr(auth_policy, "match_static_key", None)
        if callable(verifier):
            try:
                result = verifier(token, snapshot=snapshot)
            except TypeError:
                try:
                    result = verifier(token)
                except (TypeError, ValueError):
                    result = None
            except ValueError:
                result = None
            if result is None or result is False:
                return None, None, None
            if isinstance(result, AuthPrincipal):
                return ("static_project" if result.kind == "project" else
                        "static_global" if result.kind == "global" else result.kind,
                        result.project_name, result.key_id)
            if isinstance(result, dict):
                kind = result.get("kind")
                kind = "static_project" if kind == "project" else "static_global" if kind == "global" else kind
                return kind, result.get("project_name"), result.get("key_id")
            kind = getattr(result, "kind", None)
            kind = "static_project" if kind == "project" else "static_global" if kind == "global" else kind
            return (kind, getattr(result, "project_name", None),
                    getattr(result, "key_id", None))

        presented = hashlib.sha256(token.encode("utf-8", "strict")).hexdigest()
        match: tuple[str | None, str | None, str | None] | None = None
        for entry in _credential_entries(snapshot):
            values = _entry_values(entry)
            (kind, project, digest), key_id = values
            if digest is None:
                # Keep the comparison count stable even for malformed policy
                # slots; malformed records can never authenticate.
                digest = "0" * 64
            equal = hmac.compare_digest(presented, digest)
            if equal and match is None:
                normalized_kind = kind or ("static_project" if project else "static_global")
                if normalized_kind == "project":
                    normalized_kind = "static_project"
                elif normalized_kind == "global":
                    normalized_kind = "static_global"
                match = (normalized_kind, project, str(key_id) if key_id else digest[:12])
        return match or (None, None, None)

    def _principal_allows(snapshot: Any, principal: AuthPrincipal, project_name: str) -> bool:
        if not isinstance(principal, AuthPrincipal):
            principal = AuthPrincipal(
                kind=getattr(principal, "kind", None) or (principal.get("kind") if isinstance(principal, dict) else ""),
                project_name=(getattr(principal, "project_name", None) if not isinstance(principal, dict) else principal.get("project_name")),
                key_id=(getattr(principal, "key_id", None) if not isinstance(principal, dict) else principal.get("key_id")),
                oauth_resource=(getattr(principal, "oauth_resource", None) if not isinstance(principal, dict) else principal.get("oauth_resource")),
            )
        if principal.kind == SELF_TEST_PRINCIPAL_KIND:
            # 13.0 §7.3: the built-in test principal sees exactly one project,
            # by exact (case-sensitive) name. No credential policy is consulted
            # for it, because it has no credential record anywhere.
            allowed = project_name == SELF_TEST_PROJECT_NAME
            if not allowed:
                log.debug(
                    "built-in test principal denied project=%s allowed=%s",
                    project_name, SELF_TEST_PROJECT_NAME,
                )
            return allowed
        if principal.kind in {"oauth", "oauth_grant"}:
            return _oauth_enabled_for(snapshot, project_name)
        if principal.kind == "static_credential":
            # Named credentials inherit the owning connector's current
            # Knowledge policy.  Their immutable surface binding was already
            # proven during authentication; 11.x global/project key policy is
            # deliberately not consulted for v2 credentials.
            return True
        if principal.kind == "legacy_static":
            return principal.project_name in {None, project_name}
        if principal.kind == "static_project":
            if principal.project_name != project_name:
                return False
        elif principal.kind != "static_global":
            return False
        if (
            snapshot is not None
            and getattr(snapshot, "global_", None) is not None
            and isinstance(getattr(snapshot, "projects", None), dict)
        ):
            row = snapshot.projects.get(project_name)
            override = getattr(row, "static_key", None) if row is not None else None
            effective = override or snapshot.global_.static_key
            expected_kind = "static_project" if override is not None else "static_global"
            return (
                effective is not None
                and principal.kind == expected_kind
                and principal.key_id == effective.key_id
            )
        item = _policy_project(snapshot, project_name)
        # Prefer the policy store's authoritative key-id/scope method when it
        # exists; this makes queued-write rechecks reject replacement/clear.
        for owner in (snapshot, auth_policy):
            method = getattr(owner, "static_key_authorized", None) if owner is not None else None
            if callable(method):
                try:
                    return bool(method(project_name, principal.kind, principal.key_id))
                except TypeError:
                    try:
                        return bool(method(principal, project_name))
                    except (TypeError, ValueError, KeyError):
                        pass
        if item is not None:
            source = item.get("effective_static_key_source") if isinstance(item, dict) else getattr(item, "effective_static_key_source", None)
            effective_id = item.get("effective_static_key_id") if isinstance(item, dict) else getattr(item, "effective_static_key_id", None)
            if source is not None or effective_id is not None:
                expected_source = "project" if principal.kind == "static_project" else "global"
                return source == expected_source and effective_id == principal.key_id
            override = item.get("static_key_override") if isinstance(item, dict) else getattr(item, "static_key_override", None)
            if principal.kind == "static_global" and override:
                return False
            if principal.kind == "static_project" and override:
                override_id = override.get("key_id") if isinstance(override, dict) else getattr(override, "key_id", None)
                return override_id == principal.key_id
        for owner in (snapshot, auth_policy):
            effective = getattr(owner, "effective_static_key", None) if owner is not None else None
            if callable(effective):
                try:
                    record = effective(project_name)
                except (TypeError, KeyError, ValueError):
                    continue
                if record is None:
                    return False
                source = record.get("source") if isinstance(record, dict) else getattr(record, "source", None)
                key_id = record.get("key_id") if isinstance(record, dict) else getattr(record, "key_id", None)
                if source is None:
                    row = _policy_project(snapshot, project_name)
                    override = row.get("static_key") if isinstance(row, dict) else getattr(row, "static_key", None)
                    source = "project" if override is not None else "global"
                expected_source = "project" if principal.kind == "static_project" else "global"
                return source == expected_source and key_id == principal.key_id
        entries = _credential_entries(snapshot)
        allowed = False
        for entry in entries:
            kind, project, digest = _entry_values(entry)[0]
            key_id = _entry_values(entry)[1]
            if kind in ({principal.kind, "project"} if principal.kind == "static_project" else {principal.kind, "global"}) and project == (project_name if principal.kind == "static_project" else None):
                candidate_id = str(key_id) if key_id else (digest[:12] if digest else None)
                allowed = allowed or candidate_id == principal.key_id
        if entries:
            return allowed
        return False

    def _unavailable_oauth_response(request: Request) -> Response:
        if request.method == "GET" and request.url.path.endswith("/authorize"):
            return Response(
                status_code=503,
                content="<html><body>OAuth service temporarily unavailable.</body></html>",
                media_type="text/html",
                headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
            )
        return Response(
            status_code=503,
            content=b'{"error":"temporarily_unavailable"}',
            media_type="application/json",
            headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
        )

    def _protocol_response(upstream) -> Response:
        response = Response(content=upstream.content, status_code=upstream.status_code)
        response.raw_headers = [
            (name.encode("latin-1"), value.encode("latin-1"))
            for name, value in upstream.headers.multi_items()
        ]
        return response

    def _policy_snapshot() -> ConnectorConfig | None:
        """Read one immutable connector/project policy snapshot for a request."""
        try:
            return connector_store.snapshot(registry.projects)
        except (ConnectorPolicyError, OSError, TypeError, ValueError) as exc:
            log.error("connector policy unavailable during gateway request reason=%s",
                      type(exc).__name__)
            return None

    def _connector(snapshot: ConnectorConfig, connector_id: str) -> ConnectorDefinition | None:
        return next((item for item in snapshot.connectors if item.id == connector_id), None)

    def _connector_by_slug(
        snapshot: ConnectorConfig, connector_slug: str
    ) -> ConnectorDefinition | None:
        return next((item for item in snapshot.connectors if item.slug == connector_slug), None)

    def _workspace_snapshot() -> Any:
        if workspace_connector_store is None:
            return None
        reader = getattr(workspace_connector_store, "snapshot", None)
        return reader() if callable(reader) else workspace_connector_store

    def _workspace_by_slug(slug: str) -> Any:
        snapshot = _workspace_snapshot()
        if snapshot is None:
            return None
        rows = getattr(snapshot, "connectors", None)
        if rows is None:
            rows = getattr(snapshot, "workspace_connectors", None)
        if rows is None and isinstance(snapshot, dict):
            rows = snapshot.get("connectors") or snapshot.get("workspace_connectors")
        for row in rows or ():
            value = row.get("slug") if isinstance(row, dict) else getattr(row, "slug", None)
            if value == slug:
                return row
        return None

    def _resource(connector_slug: str, contract_version: int) -> str:
        return build_connector_url(_public_base_url(), connector_slug, contract_version)

    def _route_resource(route: RouteResource) -> str:
        """Return the exact requested route for audience and diagnostics."""
        origin = _public_base_url().rstrip("/")
        return f"{origin}{route.path}" if origin else route.path

    def _metadata_resource(connector_slug: str, contract_version: int) -> str:
        return (
            f"{_public_base_url().rstrip('/')}/.well-known/oauth-protected-resource"
            f"/mcp/connectors/{connector_slug}/mcp/v{contract_version}"
        )

    def _metadata_for_route(route: RouteResource) -> str:
        return f"{_public_base_url().rstrip('/')}/.well-known/oauth-protected-resource{route.path}"

    def _connector_server_info() -> dict:
        """Return MCP server metadata shared by singular and batch initialization."""
        return {
            "name": "Cognita",
            "title": "Cognita",
            "version": __version__,
            "description": "Private knowledge, securely connected through Cognita.",
            "icons": [{
                # The asset is cacheable and 9.3.1 intentionally replaced the
                # 9.3.0 bitmap at this route. Version the advertised URL so a
                # client does not retain the superseded mark until max-age.
                "src": (
                    f"{_public_base_url().rstrip('/')}{_CONNECTOR_ICON_ROUTE}"
                    f"?v={__version__}"
                ),
                "mimeType": "image/png",
                "sizes": ["512x512"],
            }],
        }

    def _jsonrpc_error(msg_id, message: str, code: int = -32602) -> JSONResponse:
        # 16.1.3: NO_ID (parse error, an invalid request with no usable id)
        # omits the `id` member; the MCP schema never allows "id": null. A
        # request's own id, null included, is echoed exactly as before.
        return JSONResponse(error_body(msg_id, code, message))

    def _tool_result(msg_id, payload: dict, tool_name: str | None = None) -> JSONResponse:
        if tool_name is None:
            tool_name = "batch" if "results" in payload and "result_key" in payload else None
        if tool_name is not None and not is_known_tool(tool_name):
            # 16.1.3: a name with no advertised output schema (a client-supplied
            # batch child that does not exist) has nothing to validate against
            # and build_tool_result would raise KeyError for it.
            log.info("tool result for a name with no output schema; not validated status=%s",
                     payload.get("status"))
            tool_name = None
        if tool_name is not None:
            result = build_tool_result(
                tool_name, payload,
                is_error=payload.get("status") in {"error", "partial_failure"},
                # Earlier mutating children may have committed before the outer
                # batch receipt is built, so contract failure is unknown-outcome.
                # 16.1.3: for every other tool the contract module decides from
                # its own mutating sets. This used to pass mutating=False for
                # them, so a contract violation on a Workspace or bridge WRITE
                # was reported as internal_error, and a client could retry a
                # write that had committed.
                mutating=True if tool_name == "batch" else None,
            )
        else:
            # Policy/project errors are shared by all output schemas.  Keep the
            # synthesized response structured even before a tool is resolved.
            normalized = normalize_legacy_error_payload(payload)
            result = {"content": [{"type": "text", "text": json.dumps(normalized, indent=2)}],
                      "structuredContent": normalized,
                      "isError": payload.get("status") in {"error", "partial_failure"}}
        return JSONResponse({"jsonrpc": "2.0", "id": msg_id, "result": result})

    def _refusal(msg_id, tool_name: str | None, reason: str, message: str,
                 **fields: Any) -> JSONResponse:
        """A tool-error result for a named tool, in that tool's own advertised shape.

        16.1.3: the single gateway-side way to refuse a call. The payload comes
        from result_contracts.refusal_payload (strict envelope for the 16
        strict-envelope tools, the legacy shape for the rest) and the result is
        validated against the tool's outputSchema by _tool_result.
        """
        return _tool_result(msg_id, refusal_payload(tool_name, reason, message, **fields), tool_name)

    def _unknown_argument_refusal(msg_id, tool_name: str, arguments: dict,
                                  accepted: tuple[str, ...]) -> JSONResponse | None:
        """16.1.3: the refusal for an argument a self-test tool does not take, or
        None when every argument is accepted.

        These two tools used to answer an unknown argument with "section must
        be a string when supplied", which names the wrong problem. This uses
        the reason every other tool uses for it (`unknown_argument`, see
        docs/ERROR-REASONS.md). The message echoes client text into a result,
        so each name goes through log_safe (one line, printable, 40
        characters) and at most five are named.
        """
        extra = sorted(str(key) for key in arguments if key not in accepted)
        if not extra:
            return None
        named = ", ".join(repr(log_safe(key, 40)) for key in extra[:5])
        more = f" (and {len(extra) - 5} more)" if len(extra) > 5 else ""
        log.info("Refused %s: unknown argument(s) count=%d reason=unknown_argument", tool_name, len(extra))
        return _refusal(
            msg_id, tool_name, "unknown_argument",
            f"{tool_name} does not accept {named}{more}. NOTHING was executed. "
            f"Accepted arguments: {', '.join(accepted)}.",
        )

    def _combined_capabilities(connector: ConnectorDefinition, contract_version: int) -> frozenset[str]:
        names = set(_public_tool_names(contract_version))
        if contract_version != PUBLIC_CONTRACT_VERSION:
            return frozenset(names)
        if not connector.workspace_enabled:
            names.difference_update(WORKSPACE_TOOL_NAMES)
            names.difference_update(BRIDGE_TOOL_NAMES)
            return frozenset(names)
        if not workspace_configured:
            names.difference_update(WORKSPACE_TOOL_NAMES)
            names.difference_update(BRIDGE_TOOL_NAMES)
            return frozenset(names)
        bridge_allowed = any(
            project.enabled
            and resolve_project_access(connector, project) is not None
            and connector.transfer_allowed(project.name)
            for project in registry.projects
        )
        if not bridge_allowed:
            names.difference_update(BRIDGE_TOOL_NAMES)
        return frozenset(names)

    def _public_tools_response(
        msg_id, contract_version: int,
        connector: ConnectorDefinition | None = None,
    ) -> JSONResponse:
        """Build the catalog for the exact authenticated generation."""
        if connector is None:
            return public_tools_response(msg_id, contract_version=contract_version)
        allowed = _combined_capabilities(connector, contract_version)
        tools = [
            item for item in public_tool_catalog(contract_version)
            if item.get("name") in allowed
        ]
        # 13.2.9 (DESIGN-13.2 §11): a client's tool list is what it validates
        # results against, so WHEN it fetched one and WHICH one matters. The
        # catalog digest changes whenever any name, description or schema does.
        catalog_sha = hashlib.sha256(
            json.dumps(tools, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()[:12]
        log.info("tools list connector_id=%s id=%s contract=v%d tools=%d catalog_sha=%s",
                 connector.slug, _describe_id(msg_id), contract_version, len(tools), catalog_sha)
        return JSONResponse({"jsonrpc": "2.0", "id": msg_id, "result": {"tools": tools}})

    def _workspace_tools_response(msg_id, contract_version: int) -> JSONResponse:
        tools = workspace_tool_catalog(contract_version)
        return JSONResponse({"jsonrpc": "2.0", "id": msg_id, "result": {"tools": tools}})

    def _public_tool_names(contract_version: int) -> frozenset[str]:
        """Read the generation-specific names from the proxy catalog seam."""
        try:
            return frozenset(public_tool_names_for_contract(contract_version))
        except ValueError as exc:
            log.error("generation catalog unavailable contract_version=%d reason=%s",
                      contract_version, type(exc).__name__)
            return frozenset()

    def _upgrade_required(msg_id, snapshot: ConnectorConfig, connector_id: str,
                          tool: str) -> JSONResponse:
        connector = _connector(snapshot, connector_id)
        if connector is None:
            current_url = build_connector_url(_public_base_url(), "connector")
        else:
            current_url = _resource(connector.slug, PUBLIC_CONTRACT_VERSION)
        log.info(
            "connector compatibility rejected connector_id=%s tool=%s reason=upgrade_required",
            connector_id, tool,
        )
        return _refusal(
            msg_id, tool, "upgrade_required",
            f"Tool '{tool}' is not available under this connector generation; "
            f"upgrade the connector to {current_url}.",
        )

    # 16.1.3: the next two take the tool name so the refusal matches THAT tool's
    # advertised outputSchema (see _refusal).
    def _project_unavailable(msg_id, tool_name: str | None) -> JSONResponse:
        return _refusal(
            msg_id, tool_name, "project_unavailable",
            "The requested project is unavailable through this connector.",
        )

    def _no_projects_configured(
        msg_id, principal: AuthPrincipal | None, tool_name: str | None
    ) -> JSONResponse:
        subject = (
            "key"
            if principal is not None and principal.kind.startswith("static_")
            else "credential"
        )
        return _refusal(
            msg_id, tool_name, "project_unavailable",
            f"No projects are configured for this {subject}.",
        )

    def _write_admission(
        connector_id: str, project_name: str, principal: AuthPrincipal | None = None
    ):
        """Recheck mutable policy after a queued write acquires its lock."""
        snapshot = _policy_snapshot()
        if snapshot is None:
            return "policy_unavailable", "Connector policy is unavailable; nothing was written."
        resolved = _resolve_project(snapshot, connector_id, project_name, principal)
        if resolved is None:
            return (
                "project_unavailable",
                "The requested project is unavailable through this connector.",
            )
        _project, access = resolved
        if principal is not None:
            try:
                auth_snapshot = _authentication_snapshot()
            except (OSError, TypeError, ValueError):
                return "policy_unavailable", "Authentication policy is unavailable; nothing was written."
            if not _principal_allows(auth_snapshot, principal, project_name):
                return (
                    "project_unavailable",
                    "The requested project is unavailable through this connector.",
                )
        if config.remote_readonly or access != "write":
            return "read_only", "This connector has read-only access; nothing was written."
        return None

    def _book_caller_admission(
        principal: AuthPrincipal, connector_id: str, project: Any,
    ) -> tuple[str, str] | None:
        denial = _write_admission(connector_id, project.name, principal)
        if denial is not None:
            return denial
        if principal.kind in {"static_credential", "legacy_static"}:
            credential_id = (
                principal.key_id if principal.kind == "static_credential"
                else principal.principal_id
            )
            if not isinstance(credential_id, str) or not credential_id:
                return "project_unavailable", "The authenticated credential is no longer available."
            try:
                import uuid
                credential_id = str(uuid.UUID(credential_id))
                active = credential_admission(credential_id) if credential_admission else None
            except Exception:
                active = None
            if active is not True:
                return "project_unavailable", "The authenticated credential is no longer available."
        snapshot = _policy_snapshot()
        if snapshot is None:
            return "policy_unavailable", "Connector policy is unavailable; nothing was written."
        resolved = _resolve_project(snapshot, connector_id, project.name, principal)
        if resolved is None:
            return "project_unavailable", "The requested project is unavailable through this connector."
        current_project, _access = resolved
        try:
            original_root = Path(project.documents_dir).resolve(strict=True)
            current_root = Path(current_project.documents_dir).resolve(strict=True)
        except (OSError, RuntimeError, TypeError, ValueError):
            return "project_unavailable", "The requested project source is unavailable."
        if current_project.name != project.name or current_root != original_root:
            return "project_unavailable", "The requested project source has changed."
        return None


    @app.get(
        "/.well-known/oauth-protected-resource/mcp/connectors/{connector_slug}/mcp/{version_segment}"
    )
    async def protected_resource_metadata(
        request: Request, connector_slug: str, version_segment: str
    ) -> Response:
        try:
            auth_snapshot = _authentication_snapshot()
        except (OSError, TypeError, ValueError):
            return Response(status_code=503, content="Authentication policy unavailable")
        if not _oauth_potentially_enabled(auth_snapshot):
            return Response(status_code=404, content="OAuth is not enabled")
        if request.url.query:
            return Response(status_code=404, content="Connector unavailable")
        route = parse_route_path(
            f"/mcp/connectors/{connector_slug}/mcp"
            if version_segment == "stable"
            else f"/mcp/connectors/{connector_slug}/mcp/{version_segment}"
        )
        if (
            route is None
            or route.family != "combined"
            # Router parameters are decoded before reaching this handler. Match
            # the wire path too, so /stable and percent-encoded aliases cannot
            # borrow the canonical stable or immutable resource identity.
            or request.scope.get("raw_path") != (
                f"/.well-known/oauth-protected-resource{route.path}".encode("ascii")
            )
        ):
            return Response(status_code=404, content="Connector unavailable")
        snapshot = _policy_snapshot()
        if snapshot is None:
            return Response(status_code=503, content="Connector policy unavailable")
        connector = _connector_by_slug(snapshot, route.slug)
        if (
            connector is None
            or not connector.enabled
            or not is_supported_route(route)
        ):
            return Response(status_code=404, content="Connector unavailable")
        if not any(
            item.enabled
            and resolve_project_access(connector, item) is not None
            and _oauth_enabled_for(auth_snapshot, item.name)
            for item in registry.projects
        ):
            return Response(status_code=404, content="Connector unavailable")
        return {
            "resource": _route_resource(route),
            "authorization_servers": [_public_base_url().rstrip("/")],
            "scopes_supported": [OAUTH_SCOPE],
            "bearer_methods_supported": ["header"],
        }

    @app.get(
        "/.well-known/oauth-protected-resource/mcp/connectors/{connector_slug}/mcp/{version_segment}/"
    )
    async def protected_resource_metadata_trailing_slash(
        connector_slug: str, version_segment: str
    ) -> Response:
        # A trailing slash is not a canonical protected-resource URL. Keep an
        # explicit route so Starlette cannot turn this malformed request into
        # a redirect to a valid resource.
        return Response(status_code=404, content="Connector unavailable")

    @app.get(
        "/.well-known/oauth-protected-resource/mcp/connectors/{connector_slug}/mcp"
    )
    async def protected_resource_metadata_stable(
        request: Request, connector_slug: str
    ) -> Response:
        return await protected_resource_metadata(request, connector_slug, "stable")

    @app.get(
        "/.well-known/oauth-protected-resource/mcp/connectors/{connector_slug}/mcp/"
    )
    async def protected_resource_metadata_stable_trailing_slash(
        connector_slug: str,
    ) -> Response:
        return Response(status_code=404, content="Connector unavailable")

    @app.api_route("/.well-known/oauth-authorization-server", methods=["GET"])
    @app.api_route("/oauth/{path:path}", methods=["GET", "POST", "PUT", "DELETE"])
    async def oauth_forward(request: Request, path: str = "") -> Response:
        try:
            auth_snapshot = _authentication_snapshot()
        except (OSError, TypeError, ValueError):
            return Response(status_code=503, content="Authentication policy unavailable")
        if not _oauth_potentially_enabled(auth_snapshot):
            return Response(status_code=404, content="OAuth is not enabled")
        if oauth_client is None or await _oauth_state() != "ready":
            return _unavailable_oauth_response(request)
        # The Admin URL is persisted independently of this long-lived gateway
        # object. Synchronize the proxy's forwarded Host/scheme on every
        # public OAuth request so discovery and authorization never remain
        # bound to the origin that was present at process startup.
        current_origin = _public_base_url().rstrip("/")
        if isinstance(getattr(oauth_client, "public_base_url", None), str):
            oauth_client.public_base_url = current_origin
        if (
            request.method == "POST"
            and request.url.path.rstrip("/") == "/oauth/register"
            and registration_rate_limited(request)
        ):
                return Response(
                    status_code=429,
                    content=b'{"error":"temporarily_unavailable","error_description":"Too many registrations"}',
                    media_type="application/json",
                    headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
                )
        try:
            return _protocol_response(await oauth_client.forward(request))
        except OAuthServiceUnavailable:
            return _unavailable_oauth_response(request)

    @app.on_event("shutdown")
    async def _close_client() -> None:
        await client.aclose()

    @app.get("/healthz")
    async def healthz() -> dict:
        # Liveness only — no project data (DESIGN.md §7.1)
        oauth_status = await _oauth_state()
        try:
            auth_snapshot = _authentication_snapshot()
            static_configured = bool(_credential_entries(auth_snapshot))
            auth_policy_status = "ready" if auth_policy is None or auth_snapshot is not None else "unavailable"
        except (OSError, TypeError, ValueError):
            static_configured = False
            auth_policy_status = "unavailable"
        out = {
            "status": "degraded" if oauth_status == "unavailable" else "ok",
            "service": "cognita", "version": __version__,
            "oauth": {"status": oauth_status},
            "authentication": {
                "policy": auth_policy_status,
                "oauth": oauth_status,
                "static_keys_configured": static_configured,
            },
            "workspace": {
                "mode": "full" if workspace_configured else "core",
                "status": "configured" if workspace_configured else "unconfigured",
            },
        }
        # DESIGN-6.0 §14.7: report the execution provider and, when a GPU is in
        # use, the devices with their free VRAM. `__version__` exists because
        # the cheapest way to know what is running on a box is to ask it; the
        # same argument applies to which device is doing the work — and after
        # the §12.3 spike, to whether a device is doing any work at all.
        out["embed"] = _embed_health(config, engine)
        # 14.0 §2.5: additive. The reranker's state is the only way to see a
        # 2.3 GB first download in progress without SSH. Never raises, like
        # _embed_health: engine, core or reranker may each be missing (no engine,
        # a test FakeEngineHost) and that reads as "disabled".
        try:
            reranker = getattr(getattr(engine, "core", None), "reranker", None)
            state_fn = getattr(reranker, "state", None)
            out["reranker"] = {
                "model": (getattr(config, "reranker_model", "") or "disabled")
                if reranker is not None else "disabled",
                "state": (state_fn() if state_fn is not None else "unknown")
                if reranker is not None else "disabled",
            }
        except Exception:
            log.warning("healthz reranker block failed", exc_info=True)
            out["reranker"] = {"model": "disabled", "state": "disabled"}
        # 13.0 §7.3: `deploy` recreates the app container in normal mode and
        # then asserts this is false, so it has to be the live gate's answer —
        # not the flag the process was started with. An expired window reports
        # false, because the key is rejected from that moment on.
        out["test_mode"] = test_mode_gate.active()
        # 13.0 §6.3/§4.1: an image that cannot read the database schema still
        # serves, with the index unavailable and /healthz saying so. The
        # property is added by the store work; read it defensively so this
        # endpoint is correct whether or not that landed.
        out["index"] = getattr(engine, "index_status", {"status": "ok"})
        return out

    @app.get(_CONNECTOR_ICON_ROUTE, include_in_schema=False)
    async def connector_icon() -> FileResponse:
        # MCP clients fetch icon URLs without connector credentials. Only this
        # fixed, packaged asset is public; project and connector data stay gated.
        return FileResponse(
            _CONNECTOR_ICON_FILE,
            media_type="image/png",
            headers={"Cache-Control": "public, max-age=86400"},
        )

    async def _authenticate_connector(
        request: Request, connector_slug: str, contract_version: int,
        *, route: RouteResource | None = None,
    ) -> AuthPrincipal | bool | Response:
        header_token, category = _extract_bearer(request)
        route = route or RouteResource("combined", connector_slug, contract_version)
        resource = _route_resource(route)
        policy_snapshot = None
        try:
            policy_snapshot = _authentication_snapshot()
        except (OSError, TypeError, ValueError):
            return Response(status_code=503, content="Authentication policy unavailable")
        oauth_possible = _oauth_potentially_enabled(policy_snapshot)
        challenge = (
            f'Bearer resource_metadata="{_metadata_for_route(route)}", '
            f'scope="{OAUTH_SCOPE}"'
        )
        generic_challenge = challenge if oauth_possible else 'Bearer realm="Cognita"'
        if route.family == "workspace":
            # Workspace-only is intentionally static-key-only in 12.0. It
            # never advertises an OAuth metadata URL or accepts OAuth tokens.
            oauth_possible = False
            generic_challenge = 'Bearer realm="Cognita-Workspace"'

        def generic_rejection(token_presented: bool = True) -> Response:
            # 16.1.3 (B4, RFC 6750): a bearer token that was presented and is
            # invalid or expired gets error="invalid_token" on the challenge.
            # A request with no usable credential (missing, or an Authorization
            # header that is not a single well-formed Bearer token) keeps
            # today's challenge unchanged. Status and body never change.
            return Response(
                status_code=401,
                content="Invalid or expired credential",
                headers={"WWW-Authenticate": (
                    _with_invalid_token(generic_challenge) if token_presented
                    else generic_challenge
                )},
            )

        if category:
            log_mcp_rejection(connector_slug, category, request)
            if (
                auth_policy is None
                and category == "missing_authorization"
                and config.oauth_enabled
                and (oauth_client is None or await _oauth_state() != "ready")
            ):
                return Response(status_code=503, content="OAuth service unavailable")
            if auth_policy is not None:
                return generic_rejection(token_presented=False)
            return Response(status_code=401, content="Authorization required",
                            headers={"WWW-Authenticate": generic_challenge})
        assert header_token is not None
        if header_token.isascii() and hmac.compare_digest(header_token, SELF_TEST_API_KEY):
            # 13.0 §7.3: the built-in test key. It is intercepted BEFORE any
            # credential store is consulted, so it can never be turned into a
            # real credential by anything written to the credential files, and
            # it can never fall through to OAuth introspection. Every path out
            # of here is either the self-test principal or the same generic 401
            # an unknown bearer receives.
            if not test_mode_gate.active():
                log.debug(
                    "built-in test key rejected connector_id=%s reason=%s",
                    connector_slug,
                    "test_mode_off" if not test_mode_gate.enabled else "test_mode_expired",
                )
                log_mcp_rejection(connector_slug, "self_test_key_not_admitted", request)
                return generic_rejection()
            if route.family != "combined":
                # Workspace-only routes are static-credential-only and are not
                # where the Self-Test project lives. Retired and future
                # generations never reach authentication at all (404 first).
                log.debug(
                    "built-in test key rejected connector_id=%s reason=route_family family=%s",
                    connector_slug, route.family,
                )
                log_mcp_rejection(connector_slug, "self_test_key_wrong_route", request)
                return generic_rejection()
            connector_snapshot = _policy_snapshot()
            surface = (
                None if connector_snapshot is None
                else _connector_by_slug(connector_snapshot, route.slug)
            )
            if surface is None or not getattr(surface, "enabled", False):
                log.debug(
                    "built-in test key rejected connector_id=%s reason=connector_unavailable",
                    connector_slug,
                )
                log_mcp_rejection(connector_slug, "self_test_key_unknown_connector", request)
                return generic_rejection()
            principal = AuthPrincipal(
                kind=SELF_TEST_PRINCIPAL_KIND,
                principal_id=self_test_principal_id(surface.id),
                surface_kind="combined",
                surface_id=surface.id,
                credential_label=SELF_TEST_PRINCIPAL_LABEL,
            )
            request.state.cognita_principal = principal
            log.info(
                "connector authorization accepted connector_id=%s method=%s "
                "kind=self_test credential=%s project=%s test_mode_remaining_s=%.0f",
                connector_slug, request.method, SELF_TEST_PRINCIPAL_LABEL,
                SELF_TEST_PROJECT_NAME, test_mode_gate.remaining_seconds(),
            )
            return principal
        if header_token.startswith(_V2_STATIC_KEY_PREFIX):
            if credential_store is None:
                return generic_rejection()
            try:
                if route.family == "combined":
                    connector_snapshot = _policy_snapshot()
                    surface = (
                        None if connector_snapshot is None
                        else _connector_by_slug(connector_snapshot, route.slug)
                    )
                else:
                    surface = _workspace_by_slug(route.slug)
                surface_id = (
                    surface.get("id") if isinstance(surface, dict)
                    else getattr(surface, "id", None)
                ) if surface is not None else None
                principal = (
                    credential_store.verify_for_surface(
                        header_token,
                        surface_kind=route.family,
                        surface_id=surface_id,
                        surface_slug=route.slug,
                    )
                    if surface_id is not None else None
                )
            except Exception as exc:
                log.error(
                    "named credential verification failed family=%s reason=%s",
                    route.family, type(exc).__name__,
                )
                return generic_rejection()
            if principal is None:
                return generic_rejection()
            auth_principal = principal.as_auth_principal()
            request.state.cognita_principal = auth_principal
            log.info(
                "connector authorization accepted connector_id=%s method=%s "
                "kind=static_credential key_id=%s",
                connector_slug, request.method, auth_principal.key_id,
            )
            return auth_principal
        if header_token.startswith(_STATIC_KEY_PREFIX) and credential_store is not None:
            # Once the v2 policy is installed, legacy digests are represented
            # as exact-surface migration rows.  Falling back to the old global
            # scan here would recreate the cross-surface bypass that migration
            # is intended to close.
            if route.family != "combined":
                return generic_rejection()
            try:
                connector_snapshot = _policy_snapshot()
                surface = (
                    None if connector_snapshot is None
                    else _connector_by_slug(connector_snapshot, route.slug)
                )
                principal = (
                    credential_store.verify_for_surface(
                        header_token,
                        surface_kind="combined",
                        surface_id=surface.id,
                        surface_slug=route.slug,
                    )
                    if surface is not None else None
                )
            except Exception as exc:
                log.error("legacy credential verification failed reason=%s", type(exc).__name__)
                return generic_rejection()
            if principal is None:
                return generic_rejection()
            auth_principal = principal.as_auth_principal()
            request.state.cognita_principal = auth_principal
            return auth_principal
        # Until the parent wires AuthenticationPolicyStore, retain the
        # existing test-mode token compatibility and OAuth contract.  The
        # production branch below is selected solely by the injected store.
        if auth_policy is None:
            if config.oauth_enabled and route.family == "combined":
                if oauth_client is None or await _oauth_state() != "ready":
                    return Response(status_code=503, content="OAuth service unavailable")
                try:
                    introspection = await oauth_client.introspect(header_token)
                except OAuthServiceUnavailable:
                    return Response(status_code=503, content="OAuth service unavailable")
                if (not introspection.active or OAUTH_SCOPE not in introspection.scopes
                        or introspection.audiences != (resource,)):
                    log_mcp_rejection(connector_slug, "invalid_token", request)
                    # 16.1.3: this pre-policy branch already named the error
                    # (first); the other 401s now do too, via _with_invalid_token.
                    return Response(status_code=401, content="Invalid or expired token",
                                    headers={"WWW-Authenticate": challenge.replace(
                                        "Bearer ", 'Bearer error="invalid_token", ', 1)})
                if (
                    route.effective_version == PUBLIC_CONTRACT_VERSION
                    and introspection.cognita_principal_id is None
                ):
                    log_mcp_rejection(connector_slug, "unbound_oauth_principal", request)
                    return generic_rejection()
                principal = AuthPrincipal(
                    kind=("oauth_grant" if introspection.cognita_principal_id else "oauth"),
                    oauth_resource=resource,
                    principal_id=introspection.cognita_principal_id,
                    surface_kind="combined",
                )
                request.state.cognita_principal = principal
                return principal
            # 13.0 §7.3: the retired `config.test_mode` branch that used to
            # live here — a `registry.find_by_token` fallback admitting raw
            # project tokens — is DELETED, not reused. Test mode is the
            # separate, runtime-only `config.self_test_mode` gate handled at
            # the top of this function; mapping it back onto `config.test_mode`
            # would re-arm this fallback.
            if route.family == "workspace":
                return generic_rejection()
            _log_static_auth_disabled()
            return Response(status_code=401, content="Release mode requires OAuth")
        is_static_namespace = header_token.startswith(_STATIC_KEY_PREFIX)
        if is_static_namespace:
            kind, project_name, key_id = _static_verify(policy_snapshot, header_token)
            if kind in {"static_global", "static_project"} and key_id:
                principal = AuthPrincipal(
                    kind=kind, project_name=project_name, key_id=key_id
                )
                request.state.cognita_principal = principal
                log.info("connector authorization accepted connector_id=%s method=%s kind=%s key_id=%s",
                         connector_slug, request.method, kind, key_id)
                return principal
            # Static credentials never fall through to OAuth.  This remains a
            # generic 401 even if OAuth is unavailable or the policy is empty.
            return generic_rejection()
        if oauth_possible and route.family == "combined":
            if oauth_client is None or await _oauth_state() != "ready":
                return Response(status_code=503, content="OAuth service unavailable")
            try:
                introspection = await oauth_client.introspect(header_token)
            except OAuthServiceUnavailable:
                return Response(status_code=503, content="OAuth service unavailable")
            if (not introspection.active or OAUTH_SCOPE not in introspection.scopes
                    or introspection.audiences != (resource,)):
                log_mcp_rejection(connector_slug, "invalid_token", request)
                return generic_rejection()
            if (
                route.effective_version == PUBLIC_CONTRACT_VERSION
                and introspection.cognita_principal_id is None
            ):
                log_mcp_rejection(connector_slug, "unbound_oauth_principal", request)
                return generic_rejection()
            principal = AuthPrincipal(
                kind=("oauth_grant" if introspection.cognita_principal_id else "oauth"),
                oauth_resource=resource,
                principal_id=introspection.cognita_principal_id,
                surface_kind="combined",
            )
            request.state.cognita_principal = principal
            log.info("connector authorization accepted connector_id=%s method=%s kind=oauth %s",
                     connector_slug, request.method, oauth_request_diagnostics(request))
            return principal
        # No project currently permits OAuth.  An OAuth-shaped token is not
        # sent to an unavailable/disabled child and receives generic 401.
        return generic_rejection()

    def _access_for_principal(
        connector: ConnectorDefinition | None, project: Any,
        principal: AuthPrincipal | None,
    ) -> str | None:
        """Resolve access, treating a matching project key as an explicit grant.

        The project exclusion blocks inherited connector access. A project key
        is already an explicit project-scoped grant, so an all-project connector
        may apply its configured default without exposing the project to OAuth,
        a global key, or a key belonging to another project.
        """
        if connector is None:
            return None
        if is_self_test_principal(principal):
            # 13.0 §7.3. Two conditions, both required: the project is
            # Self-Test, and this connector was the one the key authenticated
            # against. Everything else resolves to no access at all, which is
            # what makes `list_projects`, omitted-project inference and every
            # per-call lookup return the ordinary "unavailable" answer without
            # ever touching the forbidden project.
            name = getattr(project, "name", project)
            if name != SELF_TEST_PROJECT_NAME or not self_test_principal_matches(
                principal, getattr(connector, "id", None)
            ):
                log.debug(
                    "built-in test principal has no access project=%s connector_id=%s",
                    name, getattr(connector, "id", None),
                )
                return None
            # The connector must still grant the project: a `selected`
            # connector that does not list Self-Test gives the test principal
            # nothing. The grant is passed the way a project-scoped key's is,
            # for the same 11.1 reason — the built-in key IS an explicit
            # single-project grant, so `exclude_from_default_permissions` (set
            # to keep a project away from OAuth and global keys) does not also
            # hide Self-Test from the release check that exists to use it.
            return resolve_project_access(
                connector, project, project_key_grant=SELF_TEST_PROJECT_NAME,
            )
        project_key_grant = (
            principal.project_name
            if isinstance(principal, AuthPrincipal)
            and principal.kind == "static_project"
            else None
        )
        return resolve_project_access(
            connector, project, project_key_grant=project_key_grant
        )

    def _resolve_project(
        snapshot: ConnectorConfig, connector_id: str, raw: object,
        principal: AuthPrincipal | None = None,
    ):
        if not isinstance(raw, str) or not raw or raw != raw.strip():
            return None
        project = next((item for item in registry.projects if item.name == raw), None)
        connector = _connector(snapshot, connector_id)
        if connector is None or project is None or not project.enabled:
            return None
        access = _access_for_principal(connector, project, principal)
        if access is None:
            return None
        return project, access

    def _nested_project(value: object) -> bool:
        if isinstance(value, dict):
            return any(key == "project" or _nested_project(item) for key, item in value.items())
        if isinstance(value, list):
            return any(_nested_project(item) for item in value)
        return False

    def _call_parts(message: dict, inferred_project: str | None = None):
        params = message.get("params")
        if not isinstance(params, dict) or not isinstance(params.get("name"), str):
            return None, None, _jsonrpc_error(message.get("id"), "tools/call params must name a tool")
        # 16.1.3: `arguments` is optional in the tools/call schema, so an
        # omitted one is {}. One that is present and not an object stays a
        # JSON-RPC -32602 (a malformed request, not a mistake the model can fix).
        args = params.get("arguments", {})
        if not isinstance(args, dict):
            return None, None, _jsonrpc_error(message.get("id"), "tools/call arguments must be an object")
        # 16.1.3 (MCP 2025-11-25, Tools, Error Handling): input validation errors
        # SHOULD be tool execution errors, not protocol errors, so the model can
        # read the message and correct the call. Same text and the engine's
        # invalid-argument reason; built for THE NAMED TOOL so it validates.
        project_name = args.get("project")
        if "project" not in args and inferred_project is not None:
            project_name = inferred_project
        if not isinstance(project_name, str) or not project_name or project_name != project_name.strip():
            log.info("tools/call refused: project missing or malformed tool=%s reason=invalid",
                     log_safe(params["name"]))
            return None, None, _refusal(
                message.get("id"), params["name"], "invalid", "project must be a nonempty exact string")
        if _nested_project({key: value for key, value in args.items() if key != "project"}):
            log.info("tools/call refused: nested project routing tool=%s reason=invalid",
                     log_safe(params["name"]))
            return None, None, _refusal(
                message.get("id"), params["name"], "invalid", "nested project routing is not allowed")
        return params["name"], project_name, None

    _BATCH_REQUEST_MAX = 8 * 1024 * 1024
    _BATCH_RESULT_MAX = 1 * 1024 * 1024
    _BATCH_RECEIPT_MAX = 64 * 1024
    _BATCH_RESPONSE_MAX = 8 * 1024 * 1024

    def _batch_receipt(decoded: object) -> dict:
        """Project a large child payload to bounded mutation/read facts."""
        if not isinstance(decoded, dict):
            return {}
        allowed = (
            "status", "reason", "filepath", "filepaths", "bytes_sha256",
            "size_bytes", "line_endings", "previous_backup_id",
            "previous_backup_ids", "backup_id", "backups", "documents_written",
            "succeeded", "failed", "skipped", "message",
        )
        receipt = {key: decoded[key] for key in allowed if key in decoded}
        if len(json.dumps(receipt, ensure_ascii=False).encode("utf-8")) <= _BATCH_RECEIPT_MAX:
            return receipt
        return {
            key: receipt[key]
            for key in ("status", "reason", "filepath", "bytes_sha256", "size_bytes")
            if key in receipt
        } | {"receipt_truncated": True}

    def _batch_child_item(index: int, tool: str, payload: dict | None) -> tuple[dict, bool, bool]:
        """Retain one child result, applying the bounded-result contract.

        ``failed`` is execution outcome (and therefore drives the success and
        failure counters).  ``omitted`` is deliberately separate: a successful
        child can be too large to return, but it still ran and must not be
        reported as an error or re-run by a caller.
        """
        item = {"index": index, "tool": tool}
        if payload is None:
            item.update({"status": "error", "reason": "child_no_response", "error": {
                "reason": "child_no_response", "message": "The child returned no response."
            }})
            return item, True, False
        if isinstance(payload.get("error"), dict):
            error = dict(payload["error"])
            if (error.get("code") == -32602
                    and str(error.get("message", "")).startswith("Unknown tool:")):
                error.setdefault("reason", "unknown_tool")
            else:
                error.setdefault("reason", "child_error")
            item.update({"status": "error", "reason": error["reason"], "error": error})
            return item, True, False
        result = payload.get("result")
        if not isinstance(result, dict):
            item.update({"status": "error", "reason": "child_invalid_response", "error": {
                "reason": "child_invalid_response", "message": "The child returned no MCP result."
            }})
            return item, True, False
        # 10.1 prefers the typed child payload.  Text parsing remains solely a
        # compatibility path for legacy workers that predate structuredContent.
        decoded = result.get("structuredContent")
        blocks = result.get("content")
        if not isinstance(decoded, dict) and isinstance(blocks, list) and blocks and isinstance(blocks[0], dict):
            try:
                decoded = json.loads(blocks[0].get("text") or "")
            except (TypeError, ValueError):
                pass
        failed = bool(result.get("isError")) or (
            isinstance(decoded, dict)
            and decoded.get("status") in {"error", "partial_failure"}
        )
        item["result"] = result
        item["status"] = "error" if failed else "success"
        if failed:
            item["error"] = {
                "reason": decoded.get("reason", "child_error") if isinstance(decoded, dict)
                else "child_error",
                **({"message": decoded["message"]}
                   if isinstance(decoded, dict) and decoded.get("message") else {}),
            }
        try:
            result_bytes = len(json.dumps(result, ensure_ascii=False).encode("utf-8"))
        except (TypeError, ValueError):
            result_bytes = _BATCH_RESULT_MAX + 1
        if result_bytes <= _BATCH_RESULT_MAX:
            return item, failed, False

        # A bounded receipt is useful for mutation outcomes and facts-only
        # reads, but the original MCP result is intentionally not truncated:
        # truncation would look like complete content to a caller.
        item.pop("result", None)
        item["result_omitted"] = True
        item["reason"] = "result_too_large"
        receipt = _batch_receipt(decoded)
        if receipt:
            item["receipt"] = receipt
        return item, failed, True

    def _validate_connector_batch(
        args: object, request_bytes: int | None = None,
    ) -> tuple[list[dict] | None, str, dict | None]:
        """Validate only the batch envelope; child arguments remain child-owned."""
        if not isinstance(args, dict):
            return None, "stop", {"reason": "invalid_batch", "message": "batch arguments must be an object"}
        unknown = sorted(set(args) - {"calls", "on_error"})
        calls = args.get("calls")
        on_error = args.get("on_error", "stop")
        if unknown or "calls" not in args:
            message = f"batch has unknown arguments: {unknown}" if unknown else "batch requires calls"
            return None, on_error, {"reason": "invalid_batch", "message": message}
        if not isinstance(calls, list) or not 1 <= len(calls) <= 50:
            return None, on_error, {"reason": "invalid_batch", "message": "calls must contain 1-50 objects"}
        if on_error not in ("stop", "continue"):
            return None, on_error, {"reason": "invalid_batch", "message": "on_error must be 'stop' or 'continue'"}
        try:
            encoded_size = len(json.dumps(args, ensure_ascii=False).encode("utf-8"))
        except (TypeError, ValueError):
            encoded_size = _BATCH_REQUEST_MAX + 1
        if (request_bytes if request_bytes is not None else encoded_size) > _BATCH_REQUEST_MAX:
            return None, on_error, {"reason": "batch_too_large",
                                    "message": "batch JSON request exceeds the 8 MiB UTF-8 limit"}
        for index, call in enumerate(calls):
            if not isinstance(call, dict) or set(call) != {"tool", "arguments"}:
                return None, on_error, {"reason": "invalid_batch",
                                        "message": f"calls[{index}] must be exactly {{tool, arguments}}"}
            if not isinstance(call["tool"], str) or not call["tool"]:
                return None, on_error, {"reason": "invalid_batch",
                                        "message": f"calls[{index}].tool must be a non-empty string"}
            if not isinstance(call["arguments"], dict):
                return None, on_error, {"reason": "invalid_batch",
                                        "message": f"calls[{index}].arguments must be an object"}
            if call["tool"] == CONNECTOR_BATCH_TOOL_NAME:
                return None, on_error, {"reason": "nested_batch_not_allowed",
                                        "message": "nested batch calls are not allowed"}
            if call["tool"] == "get_asset":
                return None, on_error, {"reason": "tool_not_batchable",
                                        "message": "get_asset image results are not supported inside batch"}
        return calls, on_error, None

    async def _dispatch_connector_batch(
        request: Request, connector_id: str, message: dict, contract_version: int,
    ) -> Response:
        """Execute the connector ``batch`` envelope one child at a time.

        This deliberately calls the same gateway dispatch function used by a
        single tools/call.  There is no alternate worker path here: each child
        gets a fresh policy snapshot, project resolution, read-only decision,
        lock, and replay check.
        """
        args = (message.get("params") or {}).get("arguments")
        calls, on_error, invalid = _validate_connector_batch(
            args, getattr(request.state, "cognita_body_bytes", None)
        )
        msg_id = message.get("id")
        if invalid is not None:
            # 16.1.3: built for the named tool, so it is validated against
            # batch's own outputSchema like every other batch result.
            return _refusal(msg_id, CONNECTOR_BATCH_TOOL_NAME,
                            invalid.get("reason", "invalid_batch"), invalid["message"],
                            executed=0)

        results: list[dict] = []
        succeeded = failed = skipped = omitted = 0
        stopped = False
        for index, call in enumerate(calls or []):
            tool = call["tool"]
            if stopped:
                results.append({"index": index, "tool": tool, "status": "skipped",
                                "reason": "previous_error"})
                skipped += 1
                continue
            started = time.monotonic()
            try:
                current = _policy_snapshot()
                if current is None:
                    # 16.1.3: in the CHILD's own shape (a strict-envelope child
                    # needs operation_outcome and correlation_id). A name that
                    # does not exist keeps the unvalidated path.
                    child_response = _refusal(
                        index, tool, "policy_unavailable",
                        "Connector policy is unavailable; try again later.",
                    )
                else:
                    child_message = {
                        "jsonrpc": "2.0", "id": index, "method": "tools/call",
                        "params": {"name": tool,
                                   "arguments": copy.deepcopy(call["arguments"])},
                    }
                    child_response = await _dispatch_call(
                        request, current, connector_id, child_message, contract_version
                    )
                child_payload = _payload_from_response(child_response, index)
                item, child_failed, child_omitted = _batch_child_item(
                    index, tool, child_payload
                )
            except asyncio.CancelledError:
                log.info("connector batch cancelled connector_id=%s index=%d tool=%s",
                         connector_id, index, log_safe(tool))
                raise
            except Exception:
                # A malformed/failed worker should be an element error, not an
                # outer 500 that discards earlier committed children.
                log.exception("connector batch child failed connector_id=%s index=%d tool=%s",
                              connector_id, index, log_safe(tool))
                item, child_failed, child_omitted = _batch_child_item(
                    index, tool, None
                )
            elapsed_ms = int((time.monotonic() - started) * 1000)
            try:
                projected_size = len(json.dumps(
                    [*results, item], ensure_ascii=False
                ).encode("utf-8"))
            except (TypeError, ValueError):
                projected_size = _BATCH_RESPONSE_MAX + 1
            if projected_size > _BATCH_RESPONSE_MAX and "result" in item:
                decoded_result = item.pop("result")
                decoded_payload = (
                    decoded_result.get("structuredContent")
                    if isinstance(decoded_result, dict) else None
                )
                item["result_omitted"] = True
                item["reason"] = "result_too_large"
                receipt = _batch_receipt(decoded_payload)
                if receipt:
                    item["receipt"] = receipt
                child_omitted = True
            reason = (item.get("error") or {}).get("reason") or item.get("reason")
            log.info(
                "connector batch element connector_id=%s index=%d tool=%s outcome=%s "
                "reason=%s elapsed_ms=%d",
                connector_id, index, log_safe(tool), item.get("status"), reason or "", elapsed_ms,
            )
            results.append(item)
            if child_failed:
                failed += 1
            else:
                succeeded += 1
            if child_omitted:
                omitted += 1
            if child_failed or child_omitted:
                stopped = on_error == "stop"

        status = "success" if failed == 0 and skipped == 0 and omitted == 0 else "partial_failure"
        return _tool_result(msg_id, {
            "status": status, "result_key": "results", "on_error": on_error,
            "results": results, "succeeded": succeeded, "failed": failed,
            "skipped": skipped, "omitted": omitted,
        }, "batch")

    async def _logged_call(request: Request, snapshot: ConnectorConfig, connector_id: str,
                           message: dict, contract_version: int) -> Response:
        """One log line per tools/call, whatever answered it (13.2.3).

        Before 2026-09-22, only project-scoped operations logged anything, so a client that
        showed "[No content]" for get_self_test_plan left no trace of what the
        server had actually sent. This records the connector, the tool, the
        payload status and error flag, how many content blocks and bytes went
        back, and how long it took. Never the arguments or the content.

        13.2.4 adds the JSON-RPC id, the argument NAMES (never values), the
        size of the text block and of structuredContent separately — the two
        carry the same payload twice, and a client that drops a large result
        needs both numbers visible — and the block types.
        """
        params = message.get("params") if isinstance(message.get("params"), dict) else {}
        tool = params.get("name") if isinstance(params, dict) else None
        arguments = params.get("arguments") if isinstance(params, dict) else None
        argument_names = _describe_arguments(arguments)
        started = time.monotonic()
        response = await _dispatch_call(request, snapshot, connector_id, message, contract_version)
        elapsed_ms = int((time.monotonic() - started) * 1000)
        body = getattr(response, "body", None)
        size = len(body) if isinstance(body, (bytes, bytearray)) else -1
        status = blocks = is_error = text_chars = structured_bytes = block_types = "-"
        reason = structured_keys = "-"
        fingerprint = "text_sha=- non_ascii=- control_chars=- lines=-"
        if isinstance(body, (bytes, bytearray)):
            try:
                decoded = json.loads(body)
                result = decoded.get("result")
                if isinstance(result, dict):
                    content = result.get("content") or []
                    blocks = len(content)
                    block_types = ",".join(
                        str(block.get("type", "?")) if isinstance(block, dict) else "?"
                        for block in content
                    ) or "-"
                    first = content[0] if content and isinstance(content[0], dict) else {}
                    if isinstance(first.get("text"), str):
                        text_chars = len(first["text"])
                    is_error = bool(result.get("isError"))
                    structured = result.get("structuredContent")
                    if isinstance(structured, dict):
                        status = structured.get("status", "-")
                        structured_bytes = len(json.dumps(structured))
                        # 16.1.3: log_safe on every value that can carry client
                        # text (a message such as "Unknown tool: <name>" does).
                        reason = log_safe(structured.get("reason", "-"), 64)
                        structured_keys = log_safe(",".join(sorted(str(k) for k in structured)), 300)
                    if isinstance(first.get("text"), str):
                        fingerprint = _describe_text(first["text"])
                elif "error" in decoded:
                    status = "jsonrpc_error"
                    reason = log_safe((decoded.get("error") or {}).get("message", "-"), 120)
            except (ValueError, AttributeError):
                status = "unparsed"
        log.info("tool call connector_id=%s tool=%s id=%s arguments=%s status=%s reason=%s is_error=%s "
                 "content_blocks=%s block_types=%s text_chars=%s structured_bytes=%s structured_keys=%s "
                 "%s bytes=%s content_type=%s elapsed_ms=%d",
                 connector_id, log_safe(tool, 80), _describe_id(message.get("id")), argument_names, status, reason,
                 is_error, blocks, block_types, text_chars, structured_bytes, structured_keys,
                 fingerprint, size, response.headers.get("content-type", "-"), elapsed_ms)
        return response

    # 13.2.9 (DESIGN-13.2 §11): argument VALUES that are identifiers or
    # switches, never content. A section id or a project name is what tells two
    # calls apart in the log; text, content, argv, env, edits, base64,
    # patterns and hashes stay out.
    _LOGGED_ARGUMENT_VALUES = frozenset({
        "project", "section", "filepath", "path", "destination", "conflict_policy",
        "job_id", "prefix", "max_results", "include_hashes", "recursive", "wait_ms",
        "dry_run", "delete_file", "category", "source", "on_error", "operation_id",
        "start_line", "end_line", "tail_lines", "max_bytes", "offset", "encoding",
        "output_encoding", "include_content", "compact", "case_sensitive",
        "filepath_glob", "max_matches", "hybrid_alpha", "mode", "timeout", "cwd",
        "create_policy", "parents", "result_count",
    })

    def _describe_arguments(arguments) -> str:
        """`name=value` for allowlisted scalars, `name=<n items>` for lists,
        bare `name` for everything else; sorted, bounded, single-line."""
        if not isinstance(arguments, dict) or not arguments:
            return "-"
        parts = []
        for key in sorted(str(k) for k in arguments):
            value = arguments.get(key)
            # 16.1.3: the key is client text too (log_safe), not only the value.
            shown_key = log_safe(key, 60)
            if isinstance(value, (list, tuple)):
                parts.append(f"{shown_key}=<{len(value)} items>")
            elif key in _LOGGED_ARGUMENT_VALUES and isinstance(value, (str, int, float, bool)):
                parts.append(f"{shown_key}={log_safe(value, 80)}")
            else:
                parts.append(shown_key)
        return ",".join(parts)

    def _describe_text(text: str) -> str:
        """Wire fingerprint of a text block: length, sha256 prefix, and counts of
        the characters a strict parser or renderer might choke on. No content."""
        non_ascii = sum(1 for ch in text if ord(ch) > 127)
        control = sum(1 for ch in text if ord(ch) < 32 and ch not in "\n\t")
        digest = hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()[:12]
        return f"text_sha={digest} non_ascii={non_ascii} control_chars={control} lines={text.count(chr(10)) + 1}"

    def _describe_id(msg_id) -> str:
        """JSON-RPC id with its type, bounded — a client chooses the id."""
        if msg_id is None:
            return "null"
        # 16.1.3: ids are lenient (an array or object is echoed), so the value
        # goes through log_safe: no newline forges a line, and a container
        # shows its type and never its content.
        return f"{type(msg_id).__name__}:{log_safe(msg_id, 40)}"

    def _describe_mcp_body(raw: bytes) -> tuple[str, str, str]:
        """(methods, ids, batch) of a JSON-RPC request body. Never the params."""
        # 16.1.3: this runs AFTER the reply is built, so an exception here is a
        # 500 for a request the handler already answered. An invalid-UTF-8 body
        # and a deeply nested one are parse errors (-32700), described as
        # "unparsed" like any other unparseable body; rendering a deeply nested
        # method or id with str() can also exhaust the recursion limit.
        try:
            body = parse_body(raw) if raw else None
            if body is None:
                return "-", "-", "-"
            messages = body if isinstance(body, list) else [body]
            methods: list[str] = []
            ids: list[str] = []
            for item in messages:
                if not isinstance(item, dict):
                    methods.append("invalid")
                    ids.append("-")
                    continue
                # 16.1.3: a method is client text. A string is logged through
                # log_safe (one line, printable, bounded); a message with no
                # `method` member keeps its old "None"; anything else (null,
                # a number, an object) is the word "invalid", never its
                # content.
                method = item.get("method")
                if isinstance(method, str):
                    methods.append(log_safe(method))
                elif "method" not in item:
                    methods.append("None")
                else:
                    methods.append("invalid")
                ids.append(_describe_id(item.get("id")))
        except (BodyParseError, RecursionError) as exc:
            log.debug("mcp body not described cause=%s bytes=%d",
                      getattr(exc, "cause", type(exc).__name__), len(raw))
            return "unparsed", "-", "-"
        batch = str(len(messages)) if isinstance(body, list) else "no"
        return ",".join(methods) or "-", ",".join(ids) or "-", batch

    def _note_unexecuted(connector_slug: str, kind: MessageKind, member: int | None = None) -> None:
        """16.1.3: say why a message was not executed. Never params or content.

        A `notifications/*` message is the routine case and is not logged (the
        exchange line already shows it). An invalid request (including a
        non-finite id) and a client response are not executed either; those
        are the decisions worth a line (kind, method, reason, member).
        """
        if kind.kind == NOTIFICATION and (kind.method or "").startswith("notifications/"):
            return
        # 16.1.3: the method is client text, so it goes through log_safe (one
        # line, printable, bounded) like every client value in these lines.
        log.info("mcp message not executed connector=%s kind=%s method=%s reason=%s member=%s",
                 log_safe(connector_slug, 80), kind.kind, log_safe(kind.method or "-"),
                 kind.reason or "-", "-" if member is None else member)

    def _header(request: Request, name: str, limit: int = 120) -> str:
        value = request.headers.get(name)
        if value is None:
            return "-"
        # 16.1.3: a header value is client text in a log line; log_safe keeps
        # it on one line, printable and bounded (it also strips).
        return log_safe(value, limit) or "-"

    async def _dispatch_call(request: Request, snapshot: ConnectorConfig,
                             connector_id: str, message: dict,
                             contract_version: int) -> Response:
        params = message.get("params")
        if isinstance(params, dict) and "arguments" not in params:
            # 16.1.3: `arguments` is optional in the tools/call schema, so an
            # omitted one means {} for every tool (a tool that takes none, such
            # as list_projects, was answered -32602). Worked on as a copy; the
            # client's message is not changed. An `arguments` that is present
            # and not an object stays a -32602 in each path below.
            log.info("tools/call without arguments; treated as {} connector_id=%s", connector_id)
            params = {**params, "arguments": {}}
            message = {**message, "params": params}
        if isinstance(params, dict):
            tool = params.get("name")
            connector = _connector(snapshot, connector_id)
            # A client can keep a catalog from before the host was switched to
            # core mode. Preserve the public tool result envelope for these
            # known names, but never contact a missing broker or bridge.
            if (contract_version == PUBLIC_CONTRACT_VERSION and not workspace_configured
                    and isinstance(tool, str) and (
                        tool in WORKSPACE_TOOL_NAMES or tool in BRIDGE_TOOL_NAMES
                    )):
                return _tool_result(message.get("id"), {
                    "status": "error",
                    "reason": "runtime_unavailable",
                    "message": "Workspace runtime is not configured.",
                }, tool)
            available = (
                _combined_capabilities(connector, contract_version)
                if connector is not None else frozenset()
            )
            # 16.1.3: a name that is not a string (a list or object is unhashable)
            # is an unknown tool, not a TypeError out of the set lookup.
            if not isinstance(tool, str) or tool not in available or tool not in PUBLIC_TOOL_NAMES:
                if (
                    isinstance(tool, str)
                    and contract_version != PUBLIC_CONTRACT_VERSION
                    and (tool in PUBLIC_TOOL_NAMES or tool in available)
                ):
                    return _upgrade_required(message.get("id"), snapshot, connector_id, tool)
                return _jsonrpc_error(message.get("id"), f"Unknown tool: {tool}", code=-32602)
            if tool in WORKSPACE_TOOL_NAMES or tool in BRIDGE_TOOL_NAMES:
                # Workspace operations are principal-scoped and deliberately
                # bypass Knowledge project resolution. Bridge calls resolve
                # their project here, then pass the trusted project object into
                # the bridge policy boundary; raw project paths never enter the
                # public adapter.
                if tool in WORKSPACE_TOOL_NAMES:
                    arguments = params.get("arguments", {})
                    if not isinstance(arguments, dict):
                        return _jsonrpc_error(message.get("id"), "arguments must be an object")
                    return await _workspace_call(request, message.get("id"), tool, arguments, connector_id)
                arguments = params.get("arguments", {})
                if not isinstance(arguments, dict):
                    return _jsonrpc_error(message.get("id"), "arguments must be an object")
                project_name = arguments.get("project")
                resolved = _resolve_project(snapshot, connector_id, project_name, getattr(request.state, "cognita_principal", None))
                if resolved is None:
                    return _refusal(message.get("id"), tool, "project_unavailable", "Project is unavailable.")
                project, _access = resolved
                # copy_from_workspace publishes files into the Knowledge
                # source tree, outside LocalEngineHost's tool dispatcher.
                # Recheck the same mount authority before that write.
                source_guard = getattr(engine, "source_guard", None)
                if tool == "copy_from_workspace" and source_guard is not None:
                    source_state = source_guard.check(project.documents_dir)
                    if source_state.state != "available":
                        if source_state.state == "reconnected":
                            engine.start_background_reindex(project, "incremental")
                        return _tool_result(message.get("id"), {
                            "status": "error",
                            "reason": "source_unavailable",
                            "message": "The project source is unavailable or is being reconciled.",
                        }, tool)
                payload = await bridge_tool_result(
                    bridge_service,
                    getattr(request.state, "cognita_principal", None),
                    connector,
                    project,
                    tool,
                    arguments,
                    connector_id=connector_id,
                    contract_version=contract_version,
                )
                return _tool_result(message.get("id"), payload, tool)
        if isinstance(params, dict) and params.get("name") == CONNECTOR_BATCH_TOOL_NAME:
            return await _dispatch_connector_batch(
                request, connector_id, message, contract_version
            )
        if isinstance(params, dict) and params.get("name") == LIST_PROJECTS_TOOL_NAME:
            args = params.get("arguments", {})
            if not isinstance(args, dict):
                return _jsonrpc_error(message.get("id"), "arguments must be an object")
            if args:
                # 16.1.3: a mistake the model can read and fix, so a tool
                # result (isError) rather than a JSON-RPC -32602.
                log.info("list_projects refused: arguments supplied count=%d reason=invalid", len(args))
                return _refusal(message.get("id"), LIST_PROJECTS_TOOL_NAME, "invalid",
                                "list_projects accepts no arguments")
            connector = _connector(snapshot, connector_id)
            principal = getattr(request.state, "cognita_principal", None)
            auth_snapshot = _authentication_snapshot()
            projects = [
                {"name": item.name, "access": access}
                for item in sorted(registry.projects, key=lambda item: item.name)
                if item.enabled
                and (access := _access_for_principal(connector, item, principal)) is not None
                and (principal is None or _principal_allows(auth_snapshot, principal, item.name))
            ]
            return _tool_result(message.get("id"), {
                "status": "success",
                "connector": {"id": connector.id, "name": connector.name},
                "revision": snapshot.revision,
                "projects": projects,
            }, "list_projects")
        connector = _connector(snapshot, connector_id)
        principal = getattr(request.state, "cognita_principal", None)
        auth_snapshot = _authentication_snapshot()
        accessible_projects = [
            item
            for item in registry.projects
            if item.enabled
            and _access_for_principal(connector, item, principal) is not None
            and (principal is None or _principal_allows(auth_snapshot, principal, item.name))
        ]
        arguments = params.get("arguments") if isinstance(params, dict) else None
        inferred_project = (
            accessible_projects[0].name
            if isinstance(arguments, dict)
            and "project" not in arguments
            and len(accessible_projects) == 1
            else None
        )
        if not accessible_projects:
            if not isinstance(params, dict):
                # 16.1.3: the refusal below is built for a named tool; params
                # that name none is the one malformed shape that stays -32602.
                return _call_parts(message)[2]
            return _no_projects_configured(message.get("id"), principal, params.get("name"))
        tool, project_name, failure = _call_parts(message, inferred_project)
        if failure is not None:
            return failure
        resolved = _resolve_project(snapshot, connector_id, project_name, principal)
        if resolved is None:
            project = next(
                (item for item in registry.projects if item.name == project_name), None
            )
            outcome = (
                "excluded_from_default_permissions"
                if connector is not None and project is not None
                and connector.project_mode == "all"
                and project_name not in connector.project_access
                and project.exclude_from_default_permissions
                else "unavailable"
            )
            log.info(
                "project authorization denied connector_id=%s project=%s outcome=%s",
                connector_id, log_safe(project_name, 80), outcome,
            )
            return _project_unavailable(message.get("id"), tool)
        project, access = resolved
        if principal is not None and not _principal_allows(
            auth_snapshot, principal, project.name
        ):
            log.info("project authorization denied connector_id=%s project=%s outcome=principal_scope",
                     connector_id, project.name)
            return _project_unavailable(message.get("id"), tool)
        readonly = config.remote_readonly or access == "read"
        if tool == SELFTEST_TOOL_NAME:
            call_arguments = params.get("arguments", {})
            # 16.1.3: an unknown argument is named as such; the section
            # message below is only for a section that is not a string.
            unknown = _unknown_argument_refusal(
                message.get("id"), SELFTEST_TOOL_NAME, call_arguments, ("project", "section"))
            if unknown is not None:
                return unknown
            if "section" in call_arguments and not isinstance(call_arguments["section"], str):
                return _refusal(message.get("id"), SELFTEST_TOOL_NAME, "invalid",
                                "section must be a string when supplied")
            section = call_arguments.get("section")
            workspace_enabled = bool(connector and connector.workspace_enabled)
            bridge_enabled = bool(
                workspace_enabled and access == "write" and not config.remote_readonly
                and connector.transfer_allowed(project.name)
                and set(BRIDGE_TOOL_NAMES).issubset(available)
            )
            if isinstance(section, str) and section.startswith("W"):
                # 16.1.3: all four Workspace answers are validated against
                # get_self_test_plan's own outputSchema, which now describes
                # them as sent (the blocks stay isError:false: the self-test
                # text treats BLOCKED as distinct from FAIL).
                if not workspace_enabled:
                    log.info("self-test Workspace section blocked reason=workspace_unavailable "
                             "connector_id=%s", connector_id)
                    return _tool_result(message.get("id"), {
                        "status": "blocked", "reason": "workspace_unavailable",
                        "section": section,
                    }, SELFTEST_TOOL_NAME)
                if section == "W11" and not bridge_enabled:
                    log.info("self-test Workspace section blocked section=W11 "
                             "reason=bridge_unavailable connector_id=%s", connector_id)
                    return _tool_result(message.get("id"), {
                        "status": "blocked", "reason": "bridge_unavailable",
                        "section": section,
                    }, SELFTEST_TOOL_NAME)
                return _tool_result(message.get("id"), workspace_selftest_plan(
                    server_version=__version__, section=section,
                    bridge=bridge_enabled, catalog=available,
                    plan_tool_name=SELFTEST_TOOL_NAME, project=project.name,
                    workspace_only=False,
                ), SELFTEST_TOOL_NAME)
            knowledge = select_self_test_plan(__version__, readonly, section)
            if workspace_enabled and knowledge.get("status") == "success":
                workspace = workspace_selftest_plan(
                    server_version=__version__, section=section,
                    bridge=bridge_enabled, catalog=available,
                    plan_tool_name=SELFTEST_TOOL_NAME, project=project.name,
                    workspace_only=False,
                )
                if workspace.get("status") == "success":
                    knowledge["workspace_plan_version"] = workspace["plan_version"]
                    if section == "index":
                        knowledge["sections"] = [*knowledge["sections"], *workspace["sections"]]
                    elif section in {None, "full"}:
                        knowledge["plan"] = f"{knowledge['plan']}\n\n{workspace['plan']}"
            # 13.2.7 (DESIGN-13.2 §7): validated against the tool's own
            # advertised outputSchema like every other tool result. This was
            # the one result built without the tool name, so the server never
            # checked it, and `workspace_plan_version` above broke the contract
            # for five releases without a single log line — a validating
            # client (SillyTavern's node MCP client) showed "[No content]".
            return _tool_result(message.get("id"), knowledge, "get_self_test_plan")
        if readonly and not is_tool_allowed_remote(tool):
            log.info("project authorization denied connector_id=%s project=%s access=%s tool=%s outcome=read_only",
                     connector_id, project.name, access, tool)
            return _refusal(
                message.get("id"), tool, "read_only",
                f"Tool '{tool}' is not available: this knowledge base is read-only.",
            )
        forwarded = copy.deepcopy(message)
        forwarded["params"]["arguments"].pop("project", None)
        if engine is None:
            log.info("project operation unavailable connector_id=%s project=%s outcome=no_engine",
                     connector_id, project.name)
            return _project_unavailable(message.get("id"), tool)
        worker_url = engine.url_for(project.name)
        log.info("project operation accepted connector_id=%s project=%s access=%s tool=%s revision=%d",
                 connector_id, project.name, access, tool, snapshot.revision)
        caller_token = None
        if isinstance(principal, AuthPrincipal):
            from .books.models import WorkspaceAudioSource
            from .books.sources import SourceStageError

            def check_write_admission() -> tuple[str, str] | None:
                return _book_caller_admission(principal, connector_id, project)

            async def stage_workspace(
                source: WorkspaceAudioSource,
                *, staging_root: Path,
                max_bytes: int,
                reserve_bytes: int,
            ):
                denial = check_write_admission()
                if denial is not None:
                    raise SourceStageError(*denial)
                current = _policy_snapshot()
                connector_now = _connector(current, connector_id) if current else None
                resolved_now = (
                    _resolve_project(current, connector_id, project.name, principal)
                    if current is not None else None
                )
                if connector_now is None or resolved_now is None or bridge_service is None:
                    raise SourceStageError(
                        "source_unavailable", "The authorized Workspace source is unavailable."
                    )
                current_project, _current_access = resolved_now
                try:
                    if (current_project.name != project.name or
                            Path(current_project.documents_dir).resolve(strict=True) !=
                            Path(project.documents_dir).resolve(strict=True)):
                        raise SourceStageError(
                            "project_unavailable", "The requested project source has changed."
                        )
                except (OSError, RuntimeError, TypeError, ValueError) as exc:
                    raise SourceStageError(
                        "project_unavailable", "The requested project source is unavailable."
                    ) from exc
                try:
                    return await bridge_service.stage_book_workspace_source(
                        principal, connector_now, current_project, source.path,
                        source.expected_sha256, staging_root=staging_root,
                        connector_id=connector_id, max_bytes=max_bytes,
                        reserve_bytes=reserve_bytes,
                    )
                except Exception as exc:
                    from .bridge import BridgeError
                    if not isinstance(exc, BridgeError):
                        raise SourceStageError(
                            "source_unavailable", "Workspace source staging is unavailable."
                        ) from exc
                    raise SourceStageError(
                        exc.reason, str(exc)
                    ) from exc

            caller = BookCallerContext(
                principal=principal,
                project=project,
                connector_id=connector_id,
                check_write_admission=check_write_admission,
                stage_workspace=stage_workspace,
            )
            caller_token = CURRENT_BOOK_CALLER.set(caller)
        try:
            return await proxy_mcp(
                client, request, worker_url, readonly=readonly, documents_dir=project.documents_dir,
                backup_keep=config.backup_keep_per_file, project_name=project.name,
                connector_id=connector_id, body_override=json.dumps(forwarded).encode(),
                write_admission=lambda: _write_admission(
                    connector_id, project.name, principal
                ),
            )
        finally:
            if caller_token is not None:
                CURRENT_BOOK_CALLER.reset(caller_token)

    async def _dispatch_batch(request: Request, snapshot: ConnectorConfig,
                              connector_id: str, messages: list,
                              contract_version: int) -> Response:
        if not messages:
            return _jsonrpc_error(NO_ID, "Invalid request: empty batch", code=-32600)
        replies = []
        for index, message in enumerate(messages):
            # 16.1.3: each member is classified by shape (mcp_protocol), the
            # same routine the single-message path and the Workspace route use.
            # notifications/* and client responses add no reply; an invalid
            # request is answered -32600 with no id member unless it has a
            # valid id to echo. Every other string-method member is a request,
            # whatever its id (see classify_message).
            kind = classify_message(message)
            if kind.kind != REQUEST:
                _note_unexecuted(connector_id, kind, index)
                if kind.kind == INVALID:
                    replies.append(error_body(kind.reply_id, -32600, "Invalid request"))
                continue
            method = kind.method
            if method == "tools/call":
                response = await _logged_call(
                    request, snapshot, connector_id, message, contract_version
                )
            elif method == "initialize":
                response = _initialize_response(message.get("id"), message.get("params"))
            elif method == "ping":
                response = JSONResponse({"jsonrpc": "2.0", "id": message.get("id"), "result": {}})
            elif method == "tools/list":
                response = _public_tools_response(
                    message.get("id"), contract_version,
                    _connector(snapshot, connector_id),
                )
            else:
                response = _jsonrpc_error(message.get("id"), f"Method not found: {method}", code=-32601)
            payload = _payload_from_response(response, message.get("id"))
            if payload is not None and message.get("id") is not None:
                replies.append(payload)
        if not replies:
            return Response(status_code=202)
        return JSONResponse(replies)

    def _initialize_response(msg_id, params) -> JSONResponse:
        params = params if isinstance(params, dict) else {}
        # 13.2.4: say which client this is. The name/version a client declares
        # is the only way to tell a SillyTavern session from a claude.ai one in
        # the log. 16.1.3: the protocol version it asks for is no longer simply
        # echoed back; the answer is negotiated (mcp_protocol), and the logged
        # `protocol=` is what the client asked for. Every one of these values
        # is client text, so each goes through log_safe.
        client = params.get("clientInfo") if isinstance(params.get("clientInfo"), dict) else {}
        capabilities = params.get("capabilities")
        log.info("mcp initialize id=%s client=%s client_version=%s protocol=%s capabilities=%s",
                 _describe_id(msg_id), log_safe(client.get("name", "-"), 60),
                 log_safe(client.get("version", "-"), 40),
                 log_safe(params.get("protocolVersion", "-"), 40),
                 log_safe(",".join(sorted(str(k) for k in capabilities)), 200) or "-"
                 if isinstance(capabilities, dict) else "-")
        # 16.1.3: answer with the requested version if this server speaks it
        # (or is one of the two legacy revisions it still echoes), else the
        # newest it does (spec, Version Negotiation); before, any string was
        # echoed back. An absent version still gets 2025-03-26.
        granted = negotiate_protocol_version(params)
        if "protocolVersion" in params and params["protocolVersion"] != granted:
            log.info("mcp version negotiated requested=%s granted=%s",
                     log_safe(params["protocolVersion"], 40), granted)
        return JSONResponse({"jsonrpc": "2.0", "id": msg_id, "result": {
            "protocolVersion": granted,
            "capabilities": {"tools": {}},
            "serverInfo": _connector_server_info(),
        }})

    async def _handle_connector_mcp(
        request: Request, connector_slug: str, version_segment: str,
        workspace_path: str | None = None,
    ) -> Response:
        """One log line per HTTP exchange on the connector MCP route (13.2.4).

        The 13.2.3 per-call line proved the
        server answered SillyTavern's get_self_test_plan with 218 KB; it could
        not say what the client had asked for or how the reply was framed.
        This records what came in (methods, ids, batch shape, body size, the
        Accept / Content-Type / MCP-Protocol-Version / Mcp-Session-Id headers,
        the user agent) and what went out (HTTP status, media type, bytes,
        time). Never the token, the arguments or the content. The tools/call
        line from `_logged_call` sits between the two halves of this one.

        16.1.3 (B5): the Workspace-only route is served through this same
        function (`workspace_path` set; `connector=` then carries the Workspace
        slug), so both routes log the same fields with the same redaction. It
        had no exchange line before. The wire capture below stays
        connector-only: its body suppression only knows the combined tools, and
        a Workspace body carries file content.
        """
        started = time.monotonic()
        # 16.1.3: the slug and the version segment come from the percent-decoded
        # URL path and are logged BEFORE authentication, so `%0A` in either
        # would write a forged second log line. Both go through log_safe; the
        # unmodified values still drive routing and the wire capture.
        slug_for_log = log_safe(connector_slug, 80)
        route_for_log = log_safe(version_segment, 40)
        try:
            if workspace_path is not None:
                response = await _serve_workspace_mcp(request, connector_slug, workspace_path)
            else:
                response = await _serve_connector_mcp(request, connector_slug, version_segment)
        except UnicodeEncodeError:
            # 16.1.3: the one place both MCP routes share. A reply that cannot
            # be encoded as UTF-8 (the request carried a lone surrogate in an
            # id, method, tool name or section) used to be an HTTP 500. It is
            # a JSON-RPC error instead; for a batch the whole batch gets this
            # one reply. Acceptance review: the reply is an internal error
            # (-32603) that echoes a single request's own id (the SDK cannot
            # match an id-less error to its pending call and hung for its
            # whole timeout) and says to verify a write's outcome; the
            # failure may be in the server's own data, or after a write ran.
            # Logged at WARNING with the traceback, which carries the code
            # point and position, never the offending text; no request text
            # or argument is added.
            log.warning("mcp reply not encodable kind=unicode_encode_error connector=%s route=%s",
                        slug_for_log, route_for_log, exc_info=True)
            response = JSONResponse(unencodable_text_reply(
                getattr(request.state, "cognita_mcp_body", None)))
        except Exception:
            log.exception("mcp exchange raised connector=%s route=%s elapsed_ms=%d",
                          slug_for_log, route_for_log,
                          int((time.monotonic() - started) * 1000))
            raise
        elapsed_ms = int((time.monotonic() - started) * 1000)
        # The authorized MCP path owns body acquisition. A route/auth rejection
        # may have answered before reading a single byte, so diagnostics must
        # not drain that stream after the fact. None means no observed body;
        # an authorized empty body is still observed as b"".
        raw = getattr(request.state, "cognita_mcp_body", None)
        methods, ids, batch = _describe_mcp_body(raw) if raw is not None else ("-", "-", "-")
        body = getattr(response, "body", None)
        size = len(body) if isinstance(body, (bytes, bytearray)) else -1
        log.info("mcp exchange connector=%s route=%s http_method=%s methods=%s ids=%s batch=%s "
                 "request_bytes=%s accept=%s content_type=%s protocol_version=%s session_id=%s "
                 "user_agent=%s -> http=%d media_type=%s response_bytes=%d elapsed_ms=%d",
                 slug_for_log, route_for_log, request.method, methods, ids, batch,
                 len(raw) if raw is not None else "unknown",
                 _header(request, "accept"), _header(request, "content-type"),
                 # 16.1.3: read for this log line ONLY. The MCP-Protocol-Version
                 # header is never validated or rejected: clients that probe
                 # with `server/discover` and `MCP-Protocol-Version: 2026-07-28`,
                 # take the -32601 answer and then `initialize`, work today and
                 # must keep working (Streamable HTTP says a server MUST answer
                 # an unsupported version 400; that is deliberately not done).
                 _header(request, "mcp-protocol-version"),
                 "present" if request.headers.get("mcp-session-id") else "absent",
                 _header(request, "user-agent", 80), response.status_code,
                 response.headers.get("content-type", "-"), size, elapsed_ms)
        if config.mcp_wire_capture and workspace_path is None:
            _capture_wire(request, connector_slug, version_segment, raw, ids, response, body, elapsed_ms)
        return response

    _SENSITIVE_WIRE_HEADERS = frozenset({
        "authorization", "proxy-authorization", "cookie", "set-cookie",
        "x-api-key", "api-key",
    })

    def _redacted_wire_headers(headers) -> dict[str, str]:
        """Preserve ordinary headers while hiding common credential headers."""
        return {
            key: "<redacted>" if key.casefold() in _SENSITIVE_WIRE_HEADERS else value
            for key, value in headers.items()
        }

    def _capture_wire(request: Request, connector_slug: str, version_segment: str, raw: bytes | None,
                      ids: str, response: Response, body, elapsed_ms: int) -> None:
        """13.2.10 (DESIGN-13.2 §12): write the WHOLE exchange to a file.

        Earlier diagnostics described the data (sizes, hashes, key names)
        and never showed it. One JSON file per exchange under
        <log_dir>/mcp-wire/: credential headers redacted on both sides,
        observed request body and response body verbatim. An unconsumed request
        body is recorded as unavailable rather than read for this capture.
        Only when `mcp_wire_capture: true`; never fails the response.
        """
        try:
            # A malformed exchange may still contain partial source or URL
            # credentials; classification failure suppresses the bodies.
            suppress_bodies = _suppress_wire_bodies(raw)
            capture_dir = Path(config.log_dir) / "mcp-wire"
            capture_dir.mkdir(parents=True, exist_ok=True)
            stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime()) + f"{time.time() % 1:.3f}"[1:]
            safe_ids = re.sub(r"[^A-Za-z0-9_.-]+", "_", ids)[:40]
            # 16.1.3 (acceptance review): the slug is the percent-decoded URL
            # segment and this runs for unauthenticated requests too, so on a
            # Windows host a slug holding backslashes wrote the file outside
            # mcp-wire. Sanitized for the FILE NAME only, as the ids are; the
            # record's own `connector` field below keeps the real value.
            safe_slug = re.sub(r"[^A-Za-z0-9_.-]+", "_", connector_slug)[:80]
            path = capture_dir / f"{stamp}-{safe_slug}-{safe_ids}.json"
            record = {
                "captured_at_utc": stamp, "connector": connector_slug, "route": version_segment,
                "elapsed_ms": elapsed_ms,
                "request": {"method": request.method, "path": request.url.path,
                            "headers": _redacted_wire_headers(request.headers),
                            "body": (None if suppress_bodies else raw.decode("utf-8", "replace"))
                                    if raw is not None else None,
                            "body_suppressed": suppress_bodies,
                            "body_bytes": len(raw) if raw is not None else None},
                "response": {"status": response.status_code,
                             "headers": _redacted_wire_headers(response.headers),
                             "body": (body.decode("utf-8", "replace")
                                      if not suppress_bodies and isinstance(body, (bytes, bytearray)) else None),
                             "body_suppressed": suppress_bodies,
                             "body_bytes": len(body) if isinstance(body, (bytes, bytearray)) else None},
            }
            path.write_text(json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8")
            log.info("mcp wire captured connector=%s ids=%s file=%s request_bytes=%s response_bytes=%d",
                     connector_slug, ids, path.name, len(raw) if raw is not None else "unknown",
                     len(body) if isinstance(body, (bytes, bytearray)) else -1)
            # Bounded: keep the newest `mcp_wire_capture_keep` files, drop the rest.
            keep = max(1, int(config.mcp_wire_capture_keep))
            captured = sorted(capture_dir.glob("*.json"), key=lambda p: p.name)
            for stale in captured[:-keep]:
                stale.unlink(missing_ok=True)
            if len(captured) > keep:
                log.info("mcp wire capture pruned removed=%d kept=%d", len(captured) - keep, keep)
        except Exception as exc:  # noqa: BLE001 - capture must never fail a response
            log.warning("mcp wire capture failed connector=%s ids=%s error=%s",
                        connector_slug, ids, type(exc).__name__)

    async def _serve_connector_mcp(
        request: Request, connector_slug: str, version_segment: str
    ) -> Response:
        if request.url.query:
            # The query string is not part of the canonical OAuth resource.
            # Reject it rather than letting a noncanonical request alias the
            # audience-bound versioned endpoint.
            return PlainTextResponse("Not Found", status_code=404)
        route = parse_route_path(
            f"/mcp/connectors/{connector_slug}/mcp"
            if version_segment == "stable"
            else f"/mcp/connectors/{connector_slug}/mcp/{version_segment}"
        )
        if (
            route is None
            or route.family != "combined"
            # FastAPI decodes the route parameter before this point. Checking
            # raw_path rejects /stable and encoded aliases of valid resources.
            or request.scope.get("raw_path") != route.path.encode("ascii")
            or not is_supported_route(route)
        ):
            return PlainTextResponse("Not Found", status_code=404)
        contract_version = route.effective_version

        # Authenticate against the exact stable or versioned resource before loading
        # mutable connector policy.  This prevents unknown, disabled, deleted,
        # and future generations from being distinguishable without a valid
        # audience-bound credential.
        authenticated = await _authenticate_connector(
            request, route.slug, contract_version, route=route
        )
        if isinstance(authenticated, Response):
            return authenticated
        # Preserve the exact authenticated audience generation for downstream
        # proxy seams. The route predicate above maps stable to the current
        # code-owned generation; retired versions never reach authentication.
        request.state.cognita_contract_version = contract_version
        snapshot = _policy_snapshot()
        if snapshot is None:
            return Response(status_code=503, content="Connector policy unavailable")
        connector = _connector_by_slug(snapshot, route.slug)
        principal = authenticated if isinstance(authenticated, AuthPrincipal) else None
        if connector is None or not connector.enabled:
            if principal is not None:
                return Response(
                    status_code=401,
                    content="Invalid or expired credential",
                    # 16.1.3 (B4): a credential was presented and the answer
                    # says it is invalid or expired, so the challenge names it.
                    headers={"WWW-Authenticate": _with_invalid_token('Bearer realm="Cognita"')},
                )
            return PlainTextResponse("Connector unavailable", status_code=404)
        # Trusted connector identity for downstream asset/replay seams. This
        # state is set only after exact resource authentication and is copied
        # into the internal worker request by proxy.py; callers cannot supply it.
        canonical = connector.id
        request.state.cognita_connector_id = canonical
        request.state.cognita_connector_resource = _route_resource(route)
        request.state.cognita_project_key_project = (
            principal.project_name
            if principal is not None and principal.kind == "static_project"
            else None
        )
        if principal is not None:
            auth_snapshot = _authentication_snapshot()
            accessible = any(
                item.enabled
                and _access_for_principal(connector, item, principal) is not None
                and _principal_allows(auth_snapshot, principal, item.name)
                for item in registry.projects
            )
            if not accessible:
                principal_id = principal.key_id or principal.kind
                suppressed = mcp_rejection_logs.note((
                    "mcp", "authenticated_without_projects", connector.slug,
                    principal.kind, principal_id,
                ))
                if suppressed is not None:
                    suffix = f" suppressed={suppressed}" if suppressed else ""
                    log.warning(
                        "connector authenticated without projects connector_id=%s "
                        "kind=%s principal_id=%s%s",
                        connector.slug, principal.kind, principal_id, suffix,
                    )
        if request.method != "POST":
            return Response(status_code=405, headers={"Allow": "POST"})
        raw_body = await request.body()
        request.state.cognita_mcp_body = raw_body
        request.state.cognita_body_bytes = len(raw_body)
        try:
            body = parse_body(raw_body)
        except BodyParseError as exc:
            # 16.1.3: invalid UTF-8 and over-deep nesting are parse errors too
            # (they used to escape as HTTP 500). The reply carries no id member.
            log.info("mcp parse error connector=%s cause=%s bytes=%d",
                     log_safe(connector_slug, 80), exc.cause, len(raw_body))
            return _jsonrpc_error(NO_ID, "Parse error", code=-32700)
        if isinstance(body, list):
            return await _dispatch_batch(
                request, snapshot, canonical, body, contract_version
            )
        # 16.1.3: the kind is decided by shape (mcp_protocol.classify_message),
        # the same routine as batch members and the Workspace route. A
        # notifications/* message is answered 202 with no body whatever its id;
        # a client's own response (result / error, no method) is accepted the
        # same way. Any other string method is a request, as before.
        kind = classify_message(body)
        if kind.kind != REQUEST:
            _note_unexecuted(connector_slug, kind)
            if kind.kind == INVALID:
                return _jsonrpc_error(kind.reply_id, "Invalid request", code=-32600)
            return Response(status_code=202)
        # (classify_message makes every notifications/* message a notification,
        # so a REQUEST never needs a notifications/ check of its own.)
        method = kind.method
        if method == "initialize":
            return _initialize_response(body.get("id"), body.get("params"))
        if method == "ping":
            return JSONResponse({"jsonrpc": "2.0", "id": body.get("id"), "result": {}})
        if method == "tools/list":
            return _public_tools_response(
                body.get("id"), contract_version, connector,
            )
        if method == "tools/call":
            return await _logged_call(
                request, snapshot, canonical, body, contract_version
            )
        return _jsonrpc_error(body.get("id"), f"Method not found: {method}", code=-32601)

    def _workspace_unavailable(msg_id, tool_name: str | None = None) -> JSONResponse:
        """Catalog adapters are callable transport surfaces before runtime lands."""
        payload = {
            "status": "error",
            "reason": "workspace_unavailable",
            "message": "Workspace is not configured on this host.",
        }
        if tool_name:
            payload["tool"] = tool_name
        # Workspace adapter errors remain bounded by the adapter result
        # contract when the runtime is unavailable.
        return _tool_result(msg_id, payload, tool_name)

    async def _workspace_call(request: Request, msg_id: Any, tool: str, arguments: dict[str, Any], connector_id: str | None = None) -> JSONResponse:
        if not workspace_configured:
            return _tool_result(msg_id, {
                "status": "error",
                "reason": "workspace_unavailable",
                "message": "Workspace is not configured on this host.",
            }, tool)
        if tool == WORKSPACE_SELFTEST_TOOL_NAME:
            # 16.1.3: an unknown argument is named as such; the section
            # message is only for a section that is not a string.
            unknown = _unknown_argument_refusal(msg_id, tool, arguments, ("section",))
            if unknown is not None:
                return unknown
            if "section" in arguments and not isinstance(arguments["section"], str):
                return _refusal(msg_id, tool, "invalid", "section must be a string when supplied")
            # 16.1.3: this result was never validated (no tool name was passed),
            # although workspace_generate_self_test advertises an outputSchema.
            return _tool_result(msg_id, workspace_selftest_plan(
                server_version=__version__, section=arguments.get("section"), bridge=False,
                catalog=(*workspace_tool_names(),),
            ), tool)
        principal = getattr(request.state, "cognita_principal", None)
        if is_self_test_principal(principal):
            # 13.0 §7.3: the Workspace path goes through the same scope check
            # as every other call. A test principal whose connector does not
            # actually grant Self-Test gets no Workspace either — the Workspace
            # is principal-scoped, so without this it would otherwise be the
            # one thing the key could reach on a connector that grants nothing.
            snapshot = _policy_snapshot()
            connector = _connector(snapshot, connector_id) if snapshot is not None else None
            project = next(
                (item for item in registry.projects
                 if item.name == SELF_TEST_PROJECT_NAME and item.enabled),
                None,
            )
            if (
                connector is None
                or project is None
                or _access_for_principal(connector, project, principal) is None
            ):
                log.info(
                    "workspace call denied connector_id=%s tool=%s outcome=self_test_scope",
                    connector_id, tool,
                )
                return _tool_result(msg_id, {
                    "status": "error", "reason": "project_unavailable",
                    "message": "The requested project is unavailable through this connector.",
                }, tool)
        # A1 (DESIGN-12.18 §3.1): a call that asks to wait can block for
        # seconds inside WorkspaceManager.execute. Run only THAT call off the
        # event loop, in the fixed 10-thread pool, so it cannot freeze every
        # other request this server is handling. A call without wait_ms (or
        # an invalid one, which execute() will reject immediately) runs
        # exactly as it did before -- synchronously, on the loop.
        raw_wait = arguments.get("wait_ms") if isinstance(arguments, dict) else None
        waits = tool in {"workspace_start_job", "workspace_get_job"} and isinstance(raw_wait, int) and not isinstance(raw_wait, bool) and raw_wait > 0
        if waits:
            loop = asyncio.get_running_loop()
            payload = await loop.run_in_executor(
                _WORKSPACE_WAIT_EXECUTOR,
                lambda: workspace_tool_result(
                    workspace_service, principal, tool, arguments, connector_id=connector_id,
                ),
            )
        else:
            payload = workspace_tool_result(
                workspace_service, principal, tool, arguments,
                connector_id=connector_id,
            )
        return _tool_result(msg_id, payload, tool)

    async def _handle_workspace_mcp(request: Request, workspace_slug: str, path: str) -> Response:
        # 16.1.3 (B5): the Workspace-only route logs one line per exchange, the
        # same line the connector route logs, through the same function.
        # `route=` is the version segment of the path, or "stable".
        segment = path.rsplit("/", 1)[-1]
        return await _handle_connector_mcp(
            request, workspace_slug, segment if segment != "mcp" else "stable",
            workspace_path=path,
        )

    async def _serve_workspace_mcp(request: Request, workspace_slug: str, path: str) -> Response:
        if request.url.query:
            return PlainTextResponse("Not Found", status_code=404)
        route = parse_route_path(path)
        if (
            route is None
            or route.family != "workspace"
            or not is_supported_route(route)
        ):
            return PlainTextResponse("Not Found", status_code=404)
        # Authenticate the exact stable/versioned route before looking up the
        # independent Workspace-only record. Unknown/future/retired versions
        # therefore cannot disclose whether a slug exists. The route gate must
        # remain before authentication so retired and future generations fail
        # closed without invoking credential verification.
        authenticated = await _authenticate_connector(
            request, workspace_slug, route.effective_version, route=route
        )
        if isinstance(authenticated, Response):
            return authenticated
        request.state.cognita_contract_version = route.effective_version
        workspace = _workspace_by_slug(route.slug)
        if workspace is None or not bool(
            workspace.get("enabled", True) if isinstance(workspace, dict)
            else getattr(workspace, "enabled", True)
        ):
            return PlainTextResponse("Not Found", status_code=404)
        request.state.cognita_workspace_connector_slug = route.slug
        if request.method != "POST":
            return Response(status_code=405, headers={"Allow": "POST"})
        raw_body = await request.body()
        # 16.1.3 (B5): recorded for the per-exchange log line, as on the
        # connector route.
        request.state.cognita_mcp_body = raw_body
        try:
            body = parse_body(raw_body)
        except BodyParseError as exc:
            # 16.1.3: invalid UTF-8 and over-deep nesting are parse errors too
            # (they used to escape as HTTP 500). The reply carries no id member.
            log.info("mcp parse error connector=%s cause=%s bytes=%d",
                     log_safe(workspace_slug, 80), exc.cause, len(raw_body))
            return _jsonrpc_error(NO_ID, "Parse error", code=-32700)
        if isinstance(body, list):
            if not body:
                # 16.1.3: an empty batch is an invalid request here as on the
                # connector route (JSON-RPC 2.0); it used to fall through to 202.
                return _jsonrpc_error(NO_ID, "Invalid request: empty batch", code=-32600)
            replies = []
            for index, message in enumerate(body):
                # 16.1.3: same shape-based classification as the connector
                # route (mcp_protocol.classify_message). A request member
                # without a usable id is still executed and its reply dropped,
                # as before.
                kind = classify_message(message)
                if kind.kind != REQUEST:
                    _note_unexecuted(workspace_slug, kind, index)
                    if kind.kind == INVALID:
                        replies.append(error_body(kind.reply_id, -32600, "Invalid request"))
                    continue
                method = kind.method
                if method == "initialize":
                    response = _initialize_response(message.get("id"), message.get("params"))
                elif method == "ping":
                    response = JSONResponse({"jsonrpc": "2.0", "id": message.get("id"), "result": {}})
                elif method == "tools/list":
                    response = _workspace_tools_response(message.get("id"), route.effective_version)
                elif method == "tools/call":
                    params = message.get("params") if isinstance(message.get("params"), dict) else {}
                    name = params.get("name")
                    arguments = params.get("arguments", {})
                    if name not in workspace_tool_names(route.effective_version):
                        response = _jsonrpc_error(message.get("id"), f"Unknown tool: {name}", code=-32602)
                    elif not isinstance(arguments, dict):
                        response = _jsonrpc_error(message.get("id"), "arguments must be an object")
                    else:
                        response = await _workspace_call(request, message.get("id"), name, arguments, str(getattr(authenticated, "surface_id", None) or workspace_slug))
                else:
                    response = _jsonrpc_error(message.get("id"), f"Method not found: {method}", code=-32601)
                payload = _payload_from_response(response, message.get("id"))
                if payload is not None and message.get("id") is not None:
                    replies.append(payload)
            return Response(status_code=202) if not replies else JSONResponse(replies)
        # 16.1.3: single messages are classified by the same routine as batch
        # members and as the connector route (see _serve_connector_mcp).
        kind = classify_message(body)
        if kind.kind != REQUEST:
            _note_unexecuted(workspace_slug, kind)
            if kind.kind == INVALID:
                return _jsonrpc_error(kind.reply_id, "Invalid request", code=-32600)
            return Response(status_code=202)
        method = kind.method
        if method == "initialize":
            return _initialize_response(body.get("id"), body.get("params"))
        if method == "ping":
            return JSONResponse({"jsonrpc": "2.0", "id": body.get("id"), "result": {}})
        if method == "tools/list":
            return _workspace_tools_response(body.get("id"), route.effective_version)
        if method == "tools/call":
            params = body.get("params") if isinstance(body.get("params"), dict) else {}
            name = params.get("name")
            if name not in workspace_tool_names(route.effective_version):
                return _jsonrpc_error(body.get("id"), f"Unknown tool: {name}", code=-32602)
            arguments = params.get("arguments", {})
            if not isinstance(arguments, dict):
                return _jsonrpc_error(body.get("id"), "arguments must be an object")
            return await _workspace_call(request, body.get("id"), name, arguments, str(getattr(authenticated, "surface_id", None) or workspace_slug))
        return _jsonrpc_error(body.get("id"), f"Method not found: {method}", code=-32601)

    @app.api_route(
        "/mcp/connectors/{connector_slug}/mcp/{version_segment}",
        methods=["GET", "POST", "DELETE"],
    )
    async def mcp_connector(
        request: Request, connector_slug: str, version_segment: str
    ) -> Response:
        return await _handle_connector_mcp(request, connector_slug, version_segment)

    @app.api_route(
        "/mcp/connectors/{connector_slug}/mcp/{version_segment}/",
        methods=["GET", "POST", "DELETE"],
    )
    async def mcp_connector_trailing_slash(
        connector_slug: str, version_segment: str
    ) -> Response:
        # Do not redirect malformed resources: clients must receive the
        # canonical complete versioned URL from discovery or a challenge.
        return PlainTextResponse("Not Found", status_code=404)

    @app.api_route(
        "/mcp/connectors/{connector_slug}/mcp",
        methods=["GET", "POST", "DELETE"],
    )
    async def mcp_connector_stable(
        request: Request, connector_slug: str
    ) -> Response:
        return await _handle_connector_mcp(request, connector_slug, "stable")

    @app.api_route(
        "/mcp/connectors/{connector_slug}/mcp/",
        methods=["GET", "POST", "DELETE"],
    )
    async def mcp_connector_stable_trailing_slash(
        connector_slug: str,
    ) -> Response:
        return PlainTextResponse("Not Found", status_code=404)

    @app.api_route(
        "/mcp/workspace/{workspace_slug}/mcp",
        methods=["GET", "POST", "DELETE"],
    )
    async def mcp_workspace_stable(
        request: Request, workspace_slug: str
    ) -> Response:
        return await _handle_workspace_mcp(
            request, workspace_slug, f"/mcp/workspace/{workspace_slug}/mcp"
        )

    @app.api_route(
        "/mcp/workspace/{workspace_slug}/mcp/{version_segment}",
        methods=["GET", "POST", "DELETE"],
    )
    async def mcp_workspace_versioned(
        request: Request, workspace_slug: str, version_segment: str
    ) -> Response:
        return await _handle_workspace_mcp(
            request, workspace_slug,
            f"/mcp/workspace/{workspace_slug}/mcp/{version_segment}",
        )

    @app.api_route(
        "/mcp/workspace/{workspace_slug}/mcp/{version_segment}/",
        methods=["GET", "POST", "DELETE"],
    )
    async def mcp_workspace_trailing_slash(
        workspace_slug: str, version_segment: str
    ) -> Response:
        return PlainTextResponse("Not Found", status_code=404)

    @app.api_route("/mcp/{legacy_project}", methods=["GET", "POST", "DELETE"])
    async def legacy_mcp(legacy_project: str) -> Response:
        # The cutover is deliberate: never reinterpret a project name or old
        # token path as a connector resource.
        return PlainTextResponse(
            "Not Found. Use /mcp/connectors/<connector-slug>/mcp/v<generation>.",
            status_code=404,
        )

    @app.exception_handler(404)
    async def _log_unmatched(request: Request, exc) -> PlainTextResponse:
        # Fires only for genuinely unmatched routes (Starlette raises 404),
        # not for 404 Responses returned by the proxy. Helps catch the classic
        # "forgot /mcp/ in the connector URL" mistake without a silent failure.
        safe = _SECRET_SEG.sub("<token>", request.url.path)
        # Some OAuth clients probe standard metadata names before falling back
        # to Cognita's RFC 9728/8414 metadata. Only known, well-formed shapes
        # are expected misses; arbitrary .well-known and MCP paths stay loud.
        level = (
            logging.DEBUG
            if _is_expected_discovery_probe(request.url.path)
            else logging.WARNING
        )
        log.log(
            level,
            "404 %s %s — no matching route. OAuth connectors use "
            "/mcp/connectors/<connector-slug>/mcp/v<generation>; legacy project paths are retired.",
            request.method, safe,
        )
        return PlainTextResponse(
            "Not Found. Cognita's MCP endpoint is "
            "/mcp/connectors/<connector-slug>/mcp/v<generation>.",
            status_code=404,
        )

    return app
