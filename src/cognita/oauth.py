"""OAuth 2.1 authorization server and MCP protected-resource helpers.

Cognita has one resource owner (the configured admin) and one resource per
project. This module deliberately implements only the authorization-code +
PKCE and refresh-token profile MCP clients need; implicit and password grants,
client secrets, and bearer tokens in URLs are not supported.
"""

from __future__ import annotations

import base64
import hashlib
import html
import ipaddress
import json
import logging
import re
import secrets
import socket
import time
from collections import OrderedDict, defaultdict, deque
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from .admin_auth import (
    credential_fingerprint,
    durable_install_secret,
    has_argon2_credentials,
    verify_login,
)
from .config import CognitaConfig
from .oauth_store import SCOPE, ClientRegistration, OAuthStore, OAuthStoreError
from .registry import Registry

log = logging.getLogger("cognita.oauth")

_PKCE_RE = re.compile(r"^[A-Za-z0-9._~-]{43,128}$")
_CHALLENGE_RE = re.compile(r"^[A-Za-z0-9_-]{43}$")


def _diagnostic_id(value: str) -> str:
    """Return a non-reversible correlation ID without exposing OAuth material."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12] if value else "none"


class RejectionLogCoalescer:
    """Bound repeated application rejection logs without changing responses."""

    def __init__(self, window_seconds: float = 60.0, max_keys: int = 1024):
        self.window_seconds = max(1.0, float(window_seconds))
        self.max_keys = max(1, int(max_keys))
        self._events: OrderedDict[tuple[str, ...], tuple[float, int]] = OrderedDict()

    def note(self, key: tuple[str, ...], now: float | None = None) -> int | None:
        """Return suppressed count when an event should be emitted, else ``None``."""
        current = time.monotonic() if now is None else float(now)
        entry = self._events.get(key)
        if entry is None:
            if len(self._events) >= self.max_keys:
                self._events.popitem(last=False)
            self._events[key] = (current, 0)
            return 0
        started, suppressed = entry
        self._events.move_to_end(key)
        if current - started < self.window_seconds:
            self._events[key] = (started, suppressed + 1)
            return None
        self._events[key] = (current, 0)
        return suppressed


def _safe_log_value(value: str, limit: int = 80) -> str:
    """Keep attacker-controlled diagnostic fields single-line and bounded."""
    return re.sub(r"[^A-Za-z0-9._:/-]", "_", value)[:limit] or "none"


def oauth_request_diagnostics(request: Request) -> str:
    """Describe an edge request using identifiers safe for persistent logs."""
    ray = _safe_log_value(request.headers.get("cf-ray", "direct"))
    user_agent = request.headers.get("user-agent", "").lower()
    if "anthropic" in user_agent or "claude" in user_agent:
        agent = "claude"
    elif "openai" in user_agent or "chatgpt" in user_agent or "codex" in user_agent:
        agent = "openai"
    elif "httpx" in user_agent:
        agent = "httpx"
    elif user_agent:
        agent = "browser_or_other"
    else:
        agent = "none"
    return f"ray={ray} remote={_client_ip(request)} agent={agent}"


def _client_diagnostics(client: ClientRegistration | None, client_id: str = "") -> str:
    """Identify a registration without logging an opaque DCR client ID."""
    if client is not None:
        return f"client={_safe_log_value(client.client_name, 40)} source={client.source}"
    if client_id.startswith("https://"):
        host = urlparse(client_id).hostname or "unknown"
        return f"client_host={_safe_log_value(host)} source=cimd"
    return "client=unknown source=opaque"


def _callback_diagnostics(uri: str) -> str:
    """Log a callback route without its query, which can contain OAuth material."""
    try:
        parsed = urlparse(uri)
        port = f":{parsed.port}" if parsed.port is not None else ""
        route = f"{parsed.scheme}://{parsed.hostname or 'unknown'}{port}{parsed.path}"
        return _safe_log_value(route, 160)
    except ValueError:
        return "invalid"


def oauth_store_path(config: CognitaConfig) -> Path:
    return Path(config.oauth_store_path or (Path(config.data_root) / "oauth.sqlite3"))


def make_oauth_store(config: CognitaConfig) -> OAuthStore:
    secret = durable_install_secret(config)
    if secret is None:
        raise RuntimeError("OAuth requires a durable per-install secret under data_root")
    store = OAuthStore(oauth_store_path(config), config.oauth_access_token_ttl_seconds, secret)
    if config.admin_password_hash or config.admin_password_sha256:
        revoked = store.revoke_stale_credentials(credential_fingerprint(config))
        if revoked:
            log.info("Revoked %d OAuth grants after an admin credential change", revoked)
    return store


def issuer(config: CognitaConfig) -> str:
    return config.public_base_url.rstrip("/")


def resource_url(config: CognitaConfig, project: str) -> str:
    return f"{issuer(config)}/mcp/{project}"


def resource_metadata_url(config: CognitaConfig, project: str) -> str:
    return f"{issuer(config)}/.well-known/oauth-protected-resource/mcp/{project}"


def oauth_readiness(config: CognitaConfig) -> list[str]:
    problems: list[str] = []
    base = issuer(config)
    parsed = urlparse(base)
    if not base:
        problems.append("public_base_url is not configured")
    elif parsed.scheme != "https" and parsed.hostname not in {"127.0.0.1", "localhost", "test"}:
        problems.append("public_base_url must use HTTPS")
    if not has_argon2_credentials(config):
        if config.admin_password_sha256:
            problems.append("admin credentials still use legacy SHA-256; rerun set-admin-credentials")
        else:
            problems.append("an Argon2id admin password is not configured")
    if config.oauth_access_token_ttl_seconds < 60:
        problems.append("oauth_access_token_ttl_seconds must be at least 60")
    if not config.oauth_allowed_client_hosts:
        problems.append("oauth_allowed_client_hosts is empty")
    if durable_install_secret(config) is None:
        problems.append("a durable per-install secret could not be created under data_root")
    return problems


def validate_oauth_config(config: CognitaConfig) -> None:
    if not config.oauth_enabled:
        return
    problems = oauth_readiness(config)
    if problems:
        raise SystemExit("Refusing to enable OAuth: " + "; ".join(problems))
    # Creating the database here proves the configured directory is durable and
    # writable before the public server begins accepting authorization requests.
    make_oauth_store(config)


def bearer_challenge(config: CognitaConfig, project: str, *, invalid: bool = False) -> str:
    parts = [f'resource_metadata="{resource_metadata_url(config, project)}"', f'scope="{SCOPE}"']
    if invalid:
        parts.insert(0, 'error="invalid_token"')
    return "Bearer " + ", ".join(parts)


def _token_error(code: str, description: str, status: int = 400) -> JSONResponse:
    return JSONResponse(
        {"error": code, "error_description": description}, status_code=status,
        headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
    )


def _host_allowed(host: str | None, allowed: list[str]) -> bool:
    host = (host or "").lower().rstrip(".")
    for item in allowed:
        item = item.lower().strip().rstrip(".")
        if host == item or (item not in {"localhost", "127.0.0.1", "::1"} and host.endswith("." + item)):
            return True
    return False


def _valid_redirect(uri: str, allowed: list[str]) -> bool:
    try:
        parsed = urlparse(uri)
        if parsed.fragment or parsed.username or parsed.password or not parsed.hostname:
            return False
        loopback = parsed.hostname in {"127.0.0.1", "localhost", "::1"}
        if parsed.scheme != "https" and not (parsed.scheme == "http" and loopback):
            return False
        return _host_allowed(parsed.hostname, allowed)
    except ValueError:
        return False


def _redirect_matches(registered: str, presented: str) -> bool:
    """Exact redirect matching with RFC 8252's variable loopback-port rule."""
    try:
        expected, actual = urlparse(registered), urlparse(presented)
        if expected.hostname in {"127.0.0.1", "localhost", "::1"} and expected.port is None:
            return (
                expected.scheme == actual.scheme and expected.hostname == actual.hostname
                and expected.path == actual.path and expected.params == actual.params
                and expected.query == actual.query and not actual.fragment
            )
        return registered == presented
    except ValueError:
        return False


def _client_ip(request: Request) -> str:
    peer = request.client.host if request.client else "?"
    # Only trust Cloudflare's forwarded address when the direct peer is local;
    # otherwise an attacker could choose the rate-limit key themselves.
    if peer in {"127.0.0.1", "::1"}:
        forwarded = request.headers.get("cf-connecting-ip", "").strip()
        if forwarded:
            try:
                return str(ipaddress.ip_address(forwarded))
            except ValueError:
                pass
    return peer


def _rate_limited(bucket: dict[str, deque[float]], key: str, limit: int, window: int) -> bool:
    now = time.monotonic()
    q = bucket[key]
    while q and q[0] < now - window:
        q.popleft()
    if len(q) >= limit:
        return True
    q.append(now)
    return False


def _append_query(uri: str, **params: str) -> str:
    parsed = urlparse(uri)
    existing = parse_qs(parsed.query, keep_blank_values=True)
    for key, value in params.items():
        if value != "":
            existing[key] = [value]
    query = urlencode([(k, v) for k, values in existing.items() for v in values])
    return urlunparse(parsed._replace(query=query))


def _authorization_html(request_token: str, client: ClientRegistration, project: str, redirect: str,
                        error: str = "") -> str:
    client_name = html.escape(client.client_name)
    callback = html.escape(urlparse(redirect).hostname or "unknown")
    project_name = html.escape(project)
    error_html = f'<p class="error">{html.escape(error)}</p>' if error else ""
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>Authorize Cognita</title><style>
:root{{color-scheme:dark}}
body{{font:16px system-ui;max-width:34rem;margin:4rem auto;padding:0 1rem;background:#0f1117;color:#e8eaed}}
main{{background:#171a22;border:1px solid #303540;border-radius:12px;padding:1.5rem;box-shadow:0 8px 32px #0008}}
label{{display:block;margin:1rem 0}}input{{display:block;width:100%;box-sizing:border-box;padding:.65rem;background:#0f1117;color:#e8eaed;border:1px solid #596273;border-radius:6px}}
input:focus{{border-color:#8ab4f8;outline:2px solid #8ab4f855}}.meta{{background:#222631;padding:1rem;border-radius:8px}}
.error{{color:#ff8a80}}.actions{{display:flex;gap:.75rem}}button{{padding:.65rem 1rem;background:#8ab4f8;color:#101218;border:1px solid #8ab4f8;border-radius:6px;font-weight:600}}
.deny{{background:#171a22;color:#e8eaed;border-color:#697386}}
</style></head><body><main><h1>Authorize Cognita</h1>{error_html}
<div class="meta"><strong>{client_name}</strong> is requesting full access to
<strong>{project_name}</strong>.<br><small>Callback: {callback}</small></div>
<form method="post" action="/oauth/authorize" autocomplete="off">
<input type="hidden" name="request_token" value="{html.escape(request_token, quote=True)}">
<label>Username<input name="username" required autocomplete="username"></label>
<label>Password<input name="password" type="password" required autocomplete="current-password"></label>
<div class="actions"><button name="action" value="approve" type="submit">Authorize</button>
<button class="deny" name="action" value="deny" type="submit">Deny</button></div></form>
</main></body></html>"""


def _callback_csp_source(redirect_uri: str) -> str:
    """Return the callback origin as a CSP source after redirect validation."""
    try:
        parsed = urlparse(redirect_uri)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return ""
        host = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
        port = f":{parsed.port}" if parsed.port is not None else ""
        return f"{parsed.scheme}://{host}{port}"
    except ValueError:
        return ""


def _html_response(
    body: str, status: int = 200, *, callback_uri: str = ""
) -> HTMLResponse:
    # Chrome applies form-action to the complete form redirect chain. Permit
    # only the validated callback origin so the authorization POST's 303 can
    # reach the client without weakening the rest of the consent-page policy.
    form_action = "'self'"
    callback_source = _callback_csp_source(callback_uri)
    if callback_source:
        form_action += f" {callback_source}"
    return HTMLResponse(body, status_code=status, headers={
        "Cache-Control": "no-store", "Pragma": "no-cache",
        "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; "
        f"form-action {form_action}; frame-ancestors 'none'; base-uri 'none'",
        "Referrer-Policy": "no-referrer", "X-Content-Type-Options": "nosniff",
    })


async def _form(request: Request) -> dict[str, str]:
    body = await request.body()
    if len(body) > 16384:
        raise HTTPException(status_code=413, detail="Request body is too large")
    raw = body.decode("utf-8", errors="strict")
    return {key: values[-1] for key, values in parse_qs(raw, keep_blank_values=True).items()}


async def _json_body(request: Request) -> dict:
    body = await request.body()
    if len(body) > 65536:
        raise HTTPException(status_code=413, detail="Request body is too large")
    value = json.loads(body)
    if not isinstance(value, dict):
        raise ValueError("JSON body must be an object")
    return value


def _check_public_ip(host: str) -> None:
    infos = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    if not infos:
        raise HTTPException(status_code=400, detail="Client metadata host did not resolve")
    for info in infos:
        address = ipaddress.ip_address(info[4][0])
        if not address.is_global:
            raise HTTPException(status_code=400, detail="Client metadata host is not public")


async def _cimd_client(config: CognitaConfig, client_id: str) -> ClientRegistration:
    parsed = urlparse(client_id)
    if parsed.scheme != "https" or not parsed.path or not _host_allowed(
        parsed.hostname, config.oauth_allowed_client_hosts
    ):
        raise HTTPException(status_code=400, detail="Untrusted client metadata URL")
    _check_public_ip(parsed.hostname or "")
    try:
        async with httpx.AsyncClient(follow_redirects=False, timeout=5.0) as client:
            response = await client.get(client_id, headers={"Accept": "application/json"})
        if response.status_code != 200 or len(response.content) > 65536:
            raise HTTPException(status_code=400, detail="Client metadata could not be loaded")
        data = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="Client metadata could not be loaded") from exc
    redirects = data.get("redirect_uris")
    if data.get("client_id") != client_id or not isinstance(redirects, list) or not redirects:
        raise HTTPException(status_code=400, detail="Invalid client metadata document")
    if any(not isinstance(uri, str) or not _valid_redirect(uri, config.oauth_allowed_client_hosts)
           for uri in redirects):
        raise HTTPException(status_code=400, detail="Client metadata has an untrusted redirect URI")
    return ClientRegistration(
        client_id, str(data.get("client_name") or parsed.hostname)[:200], tuple(redirects), "cimd"
    )


def install_oauth_routes(
    app: FastAPI, config: CognitaConfig, registry: Registry, store: OAuthStore
) -> None:
    """Attach authorization-server and discovery routes to the public gateway."""
    login_failures: dict[str, deque[float]] = defaultdict(deque)
    register_attempts: dict[str, deque[float]] = defaultdict(deque)
    token_attempts: dict[str, deque[float]] = defaultdict(deque)
    revoke_attempts: dict[str, deque[float]] = defaultdict(deque)
    token_rejection_logs = RejectionLogCoalescer()

    def require_enabled() -> None:
        if not config.oauth_enabled:
            raise HTTPException(status_code=404, detail="OAuth is not enabled")

    @app.get("/.well-known/oauth-protected-resource/mcp/{project}")
    async def protected_resource_metadata(project: str) -> dict:
        require_enabled()
        registered = registry.get(project)
        if registered is None or not registered.enabled:
            raise HTTPException(status_code=404, detail="Unknown project")
        return {
            "resource": resource_url(config, project),
            "authorization_servers": [issuer(config)],
            "scopes_supported": [SCOPE],
            "bearer_methods_supported": ["header"],
        }

    @app.get("/.well-known/oauth-authorization-server")
    async def authorization_server_metadata() -> dict:
        require_enabled()
        base = issuer(config)
        return {
            "issuer": base,
            "authorization_endpoint": f"{base}/oauth/authorize",
            "token_endpoint": f"{base}/oauth/token",
            "registration_endpoint": f"{base}/oauth/register",
            "revocation_endpoint": f"{base}/oauth/revoke",
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "code_challenge_methods_supported": ["S256"],
            "token_endpoint_auth_methods_supported": ["none"],
            "scopes_supported": [SCOPE],
            "client_id_metadata_document_supported": True,
            "authorization_response_iss_parameter_supported": True,
        }

    @app.post("/oauth/register")
    async def register(request: Request) -> JSONResponse:
        require_enabled()
        remote = _client_ip(request)
        if _rate_limited(register_attempts, remote, 10, 3600):
            return _token_error("temporarily_unavailable", "Too many registrations", 429)
        store.cleanup()
        if store.client_count() >= 100:
            return _token_error("temporarily_unavailable", "Registration capacity reached", 503)
        try:
            data = await _json_body(request)
        except (ValueError, UnicodeDecodeError):
            return _token_error("invalid_client_metadata", "Request must be JSON")
        redirects = data.get("redirect_uris")
        if not isinstance(redirects, list) or not redirects or len(redirects) > 10:
            return _token_error("invalid_redirect_uri", "redirect_uris is required")
        if any(not isinstance(uri, str) or not _valid_redirect(uri, config.oauth_allowed_client_hosts)
               for uri in redirects):
            return _token_error("invalid_redirect_uri", "A redirect URI is not trusted")
        if data.get("token_endpoint_auth_method", "none") != "none":
            return _token_error("invalid_client_metadata", "Only public clients are supported")
        grant_types = data.get("grant_types", ["authorization_code", "refresh_token"])
        response_types = data.get("response_types", ["code"])
        if (
            not isinstance(grant_types, list)
            or set(grant_types) not in ({"authorization_code"}, {"authorization_code", "refresh_token"})
            or response_types != ["code"]
        ):
            return _token_error("invalid_client_metadata", "Only authorization-code clients are supported")
        client_id = "cog_client_" + secrets.token_urlsafe(24)
        registration = ClientRegistration(
            client_id, str(data.get("client_name") or "MCP client")[:200], tuple(redirects), "dcr"
        )
        store.put_client(registration)
        log.info("Registered OAuth client_ref=%s source=dcr redirect_host=%s", _diagnostic_id(client_id),
                 urlparse(redirects[0]).hostname)
        return JSONResponse({
            "client_id": client_id, "client_name": registration.client_name,
            "redirect_uris": list(registration.redirect_uris),
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"], "token_endpoint_auth_method": "none",
        }, status_code=201, headers={"Cache-Control": "no-store"})

    @app.get("/oauth/authorize")
    async def authorize_get(
        request: Request,
        client_id: str, redirect_uri: str, response_type: str = "",
        code_challenge: str = "", code_challenge_method: str = "",
        resource: str = "", scope: str = "", state: str = "",
    ) -> HTMLResponse:
        require_enabled()
        if response_type != "code" or code_challenge_method != "S256" or not _CHALLENGE_RE.fullmatch(code_challenge):
            raise HTTPException(status_code=400, detail="Authorization code with PKCE-S256 is required")
        if scope and set(scope.split()) != {SCOPE}:
            raise HTTPException(status_code=400, detail="Invalid scope")
        project = next((p.name for p in registry.projects if p.enabled and resource == resource_url(config, p.name)), None)
        if project is None:
            raise HTTPException(status_code=400, detail="Invalid resource")
        if client_id.startswith("https://"):
            client = await _cimd_client(config, client_id)
            store.put_client(client)
        else:
            client = store.get_client(client_id)
        if client is None:
            raise HTTPException(status_code=400, detail="Unknown OAuth client")
        if not any(_redirect_matches(uri, redirect_uri) for uri in client.redirect_uris):
            raise HTTPException(status_code=400, detail="Redirect URI does not match the client registration")
        request_token = store.create_auth_request({
            "client_id": client.client_id, "client_name": client.client_name,
            "redirect_uri": redirect_uri, "resource": resource, "project": project,
            "scope": SCOPE, "state": state, "code_challenge": code_challenge,
        })
        log.info(
            "OAuth authorize accepted flow=%s %s project=%s callback=%s iss_response=true %s",
            _diagnostic_id(request_token), _client_diagnostics(client), project,
            _callback_diagnostics(redirect_uri), oauth_request_diagnostics(request),
        )
        return _html_response(
            _authorization_html(request_token, client, project, redirect_uri),
            callback_uri=redirect_uri,
        )

    @app.post("/oauth/authorize")
    async def authorize_post(request: Request) -> Response:
        require_enabled()
        try:
            form = await _form(request)
        except UnicodeDecodeError:
            raise HTTPException(status_code=400, detail="Invalid form encoding")
        request_token = form.get("request_token", "")
        flow_id = _diagnostic_id(request_token)
        pending = store.get_auth_request(request_token)
        if pending is None:
            log.warning("OAuth authorize rejected flow=%s reason=request_expired %s",
                        flow_id, oauth_request_diagnostics(request))
            return _html_response("<h1>Authorization request expired</h1>", 400)
        if form.get("action") == "deny":
            denied = store.deny_auth_request(request_token)
            if denied is None:
                log.warning("OAuth authorize rejected flow=%s reason=request_expired %s",
                            flow_id, oauth_request_diagnostics(request))
                return _html_response("<h1>Authorization request expired</h1>", 400)
            log.info(
                "OAuth authorize denied flow=%s project=%s callback=%s iss_response=true %s",
                flow_id, denied["project"], _callback_diagnostics(denied["redirect_uri"]),
                oauth_request_diagnostics(request),
            )
            return RedirectResponse(_append_query(
                denied["redirect_uri"], error="access_denied", state=denied["state"], iss=issuer(config)
            ), status_code=303)
        remote = _client_ip(request)
        if _rate_limited(login_failures, remote, 5, 60):
            log.warning("OAuth authorize rejected flow=%s reason=login_rate_limit %s",
                        flow_id, oauth_request_diagnostics(request))
            return _html_response("<h1>Too many failed attempts</h1><p>Try again in one minute.</p>", 429)
        if not verify_login(config, form.get("username", ""), form.get("password", "")):
            client = store.get_client(pending["client_id"]) or ClientRegistration(
                pending["client_id"], pending["client_name"], (pending["redirect_uri"],), "pending"
            )
            log.warning("OAuth authorize rejected flow=%s reason=invalid_login %s",
                        flow_id, oauth_request_diagnostics(request))
            return _html_response(
                _authorization_html(request_token, client, pending["project"], pending["redirect_uri"],
                                    "Invalid username or password"), 401,
                callback_uri=pending["redirect_uri"],
            )
        login_failures.pop(remote, None)
        try:
            code, approved = store.approve_auth_request(
                request_token, config.admin_username, credential_fingerprint(config)
            )
        except OAuthStoreError as exc:
            log.warning("OAuth authorize rejected flow=%s reason=%s %s",
                        flow_id, _safe_log_value(exc.code), oauth_request_diagnostics(request))
            return _html_response(f"<h1>{html.escape(exc.description)}</h1>", 400)
        log.info(
            "OAuth authorize approved flow=%s client=%s project=%s callback=%s "
            "iss_response=true %s",
            flow_id, _safe_log_value(approved["client_name"], 40), approved["project"],
            _callback_diagnostics(approved["redirect_uri"]), oauth_request_diagnostics(request),
        )
        return RedirectResponse(_append_query(
            approved["redirect_uri"], code=code, state=approved["state"], iss=issuer(config)
        ), status_code=303)

    @app.post("/oauth/token")
    async def token(request: Request) -> JSONResponse:
        require_enabled()
        if _rate_limited(token_attempts, _client_ip(request), 120, 60):
            return _token_error("temporarily_unavailable", "Too many token requests", 429)
        try:
            form = await _form(request)
        except UnicodeDecodeError:
            log.warning("OAuth token rejected reason=invalid_form_encoding %s",
                        oauth_request_diagnostics(request))
            return _token_error("invalid_request", "Invalid form encoding")
        grant_type = form.get("grant_type", "")
        client_id = form.get("client_id", "")
        resource = form.get("resource", "")
        project = next((
            item.name for item in registry.projects
            if item.enabled and resource == resource_url(config, item.name)
        ), None)
        client = store.get_client(client_id)
        client_ref = _diagnostic_id(client.client_id) if client is not None else "none"
        log.debug(
            "OAuth token received grant_type=%s client_ref=%s project=%s callback=%s %s",
            _safe_log_value(grant_type), client_ref, project or "invalid",
            _callback_diagnostics(form.get("redirect_uri", "")), oauth_request_diagnostics(request),
        )

        def log_token_rejection(category: str, *, grant_ref: str = "none") -> None:
            safe_category = _safe_log_value(category, 48)
            key = ("oauth_token", _safe_log_value(grant_type), safe_category,
                   project or "invalid", client_ref, grant_ref)
            suppressed = token_rejection_logs.note(key)
            if suppressed is None:
                return
            suffix = f" suppressed={suppressed}" if suppressed else ""
            log.warning(
                "OAuth token rejected grant_type=%s project=%s category=%s "
                "client_ref=%s grant_ref=%s%s %s",
                _safe_log_value(grant_type), project or "invalid", safe_category,
                client_ref, grant_ref, suffix, oauth_request_diagnostics(request),
            )

        if project is None:
            log_token_rejection("invalid_target")
            return _token_error("invalid_target", "The requested resource is unavailable")
        try:
            if grant_type == "authorization_code":
                verifier = form.get("code_verifier", "")
                if not _PKCE_RE.fullmatch(verifier):
                    raise OAuthStoreError("invalid_grant", "Invalid PKCE verifier")
                challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
                result = store.exchange_code(
                    form.get("code", ""), client_id, form.get("redirect_uri", ""), resource,
                    challenge, credential_fingerprint(config),
                )
            elif grant_type == "refresh_token":
                result = store.refresh(
                    form.get("refresh_token", ""), client_id, resource,
                    credential_fingerprint(config),
                )
            else:
                log_token_rejection("unsupported_grant_type")
                return _token_error("unsupported_grant_type", "Unsupported grant type")
        except OAuthStoreError as exc:
            log_token_rejection(exc.category, grant_ref=exc.grant_ref)
            return _token_error(exc.code, exc.description)
        grant_ref = result.pop("_grant_ref", "none")
        log.info(
            "OAuth token issued grant_type=%s project=%s client_ref=%s grant_ref=%s %s",
            _safe_log_value(grant_type), project, client_ref, grant_ref,
            oauth_request_diagnostics(request),
        )
        return JSONResponse(result, headers={"Cache-Control": "no-store", "Pragma": "no-cache"})

    @app.post("/oauth/revoke")
    async def revoke(request: Request) -> Response:
        require_enabled()
        if _rate_limited(revoke_attempts, _client_ip(request), 120, 60):
            return Response(status_code=429, headers={"Retry-After": "60"})
        try:
            form = await _form(request)
        except UnicodeDecodeError:
            return Response(status_code=200)
        store.revoke_token(form.get("token", ""))
        return Response(status_code=200, headers={"Cache-Control": "no-store"})
