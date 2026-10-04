"""HTTP boundary for the Cognita 8.0 OAuth child service.

The parent deliberately treats the child as an authenticated protocol peer.  This
module contains transport and response-shape handling only; token lifecycle and
connection state remain owned by Django OAuth Toolkit in the child.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx

log = logging.getLogger("cognita.oauth_service_client")

REQUEST_HEADERS = frozenset(
    {"accept", "accept-language", "authorization", "content-type", "cookie", "origin", "referer", "user-agent"}
)
RESPONSE_HEADERS = frozenset(
    {"cache-control", "content-language", "content-type", "expires", "location", "pragma", "retry-after", "set-cookie", "vary", "www-authenticate"}
)
HOP_BY_HOP_HEADERS = frozenset(
    {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailer", "transfer-encoding", "upgrade"}
)
UNAVAILABLE_MESSAGE = (
    "OAuth service unavailable; protected access is closed. Check Cognita health and logs. "
    "Restart Cognita if the OAuth child failed to launch or exited."
)

_NON_REPLAYABLE_CONNECT_MARKERS = (
    "dns", "getaddrinfo", "nodename", "name or service", "tls", "ssl",
    "certificate", "configuration", "proxy", "pool",
)


def _safe_connect_failure(exc: BaseException) -> bool:
    """Recognize a pre-send refusal/timeout without retrying DNS/TLS/config faults."""
    if not isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout)):
        return False
    if getattr(exc, "request_bytes_sent", False) is not False:
        return False
    detail = str(exc).lower()
    return not any(marker in detail for marker in _NON_REPLAYABLE_CONNECT_MARKERS)


class OAuthServiceUnavailable(RuntimeError):
    """The child could not provide a trustworthy protocol response."""

    def __init__(self, message: str = UNAVAILABLE_MESSAGE, *, category: str = "unavailable", retryable: bool = False) -> None:
        super().__init__(message)
        self.category = category
        self.retryable = retryable


@dataclass(frozen=True, slots=True)
class IntrospectionResult:
    """The small, non-secret subset of RFC 7662 used by the parent."""

    active: bool
    scopes: tuple[str, ...] = ()
    audiences: tuple[str, ...] = ()
    client_id: str | None = None
    subject: str | None = None
    # Required for current (v5) routes.  Kept optional at this transport layer
    # Historical token records may still be present, but current-only route
    # admission prevents them from authorizing a retired generation.
    cognita_principal_id: str | None = None

    @property
    def scope(self) -> str:
        """Compatibility view for callers that expect RFC 7662's string scope."""
        return " ".join(self.scopes)

    @property
    def aud(self) -> tuple[str, ...]:
        return self.audiences


def _safe_headers(headers: Mapping[str, str] | httpx.Headers) -> list[tuple[str, str]]:
    """Select the protocol allowlist while retaining repeated Set-Cookie values."""
    selected: list[tuple[str, str]] = []
    items = headers.multi_items() if isinstance(headers, httpx.Headers) else headers.items()
    for name, value in items:
        lowered = name.lower()
        if lowered in RESPONSE_HEADERS and lowered not in HOP_BY_HOP_HEADERS:
            selected.append((name, value))
    return selected


def _json_response(status_code: int, payload: Any, *, request: httpx.Request | None = None) -> httpx.Response:
    return httpx.Response(
        status_code,
        headers=[("content-type", "application/json"), ("cache-control", "no-store")],
        content=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
        request=request,
    )


class OAuthServiceClient:
    """Authenticated HTTP client shared by gateway and admin integrations.

    ``transport`` and ``http_client`` are intentionally injectable so lifecycle
    tests can use a fake child without creating a real OAuth installation.
    """

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:8778",
        *,
        internal_client_id: str = "cognita-internal",
        internal_client_secret: str | bytes = "",
        timeout: float = 10.0,
        public_base_url: str = "",
        accepted_redirect_uris: tuple[str, ...] | list[str] = (),
        allowed_redirect_hosts: tuple[str, ...] | list[str] = (),
        max_body_bytes: int = 2 * 1024 * 1024,
        http_client: httpx.AsyncClient | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        supervisor: Any = None,
    ) -> None:
        parsed = urlsplit(base_url)
        if parsed.scheme not in {"http", "https"} or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("OAuth service base_url must be loopback HTTP(S)")
        self.base_url = base_url.rstrip("/")
        self.internal_client_id = internal_client_id
        self.internal_client_secret = internal_client_secret.decode("utf-8", "replace") if isinstance(internal_client_secret, bytes) else internal_client_secret
        self.timeout = timeout
        self.public_base_url = public_base_url.rstrip("/")
        self.accepted_redirect_uris = frozenset(accepted_redirect_uris)
        self.allowed_redirect_hosts = frozenset(
            host.lower().lstrip(".") for host in allowed_redirect_hosts
        )
        self.max_body_bytes = max_body_bytes
        self._http_client = http_client
        self._transport = transport
        self.supervisor = supervisor
        self._introspection_waiters: set[asyncio.Future[None]] = set()

    def _auth(self) -> httpx.BasicAuth:
        return httpx.BasicAuth(self.internal_client_id, self.internal_client_secret)

    def _url(self, path: str) -> str:
        if not path.startswith("/"):
            path = "/" + path
        return self.base_url + path

    def _available(self) -> bool:
        if self.supervisor is None:
            return True
        snap = self.supervisor.snapshot
        state = getattr(snap, "state", snap)
        return str(getattr(state, "value", state)).lower() == "ready"

    async def check_readiness(self) -> bool:
        """Check the shared child only when a parent path needs OAuth."""
        if self.supervisor is None:
            return True
        checker = getattr(self.supervisor, "check_readiness", None)
        if checker is None:
            return self._available()
        return bool(await checker())

    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        skip_availability = bool(kwargs.pop("_skip_availability", False))
        request_timeout = kwargs.pop("_request_timeout", None)
        if not skip_availability and not self._available():
            raise OAuthServiceUnavailable(UNAVAILABLE_MESSAGE)
        own_client = self._http_client is None
        client = self._http_client or httpx.AsyncClient(
            timeout=httpx.Timeout(self.timeout), follow_redirects=False, transport=self._transport
        )
        try:
            kwargs.setdefault("follow_redirects", False)
            if request_timeout is not None:
                kwargs["timeout"] = max(0.0, float(request_timeout))
            response = await client.request(method, self._url(path), **kwargs)
            return response
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            # HTTPX raises these only before it has sent request bytes. Keep
            # this primitive to one attempt: the supervisor owns the bounded
            # one-second-slot recovery episode and can therefore coalesce
            # readiness callers without replaying an ambiguous request.
            if not _safe_connect_failure(exc):
                log.error("OAuth service connection setup failed phase=request category=%s", type(exc).__name__)
                raise OAuthServiceUnavailable(
                    UNAVAILABLE_MESSAGE, category=type(exc).__name__, retryable=False
                ) from exc
            log.debug("OAuth service connection attempt failed phase=request category=%s", type(exc).__name__)
            raise OAuthServiceUnavailable(
                UNAVAILABLE_MESSAGE, category=type(exc).__name__, retryable=True
            ) from exc
        except (httpx.HTTPError, TimeoutError) as exc:
            # Read/write/protocol failures have unknown delivery state. They
            # are deliberately not replayable, even for an idempotent method.
            log.error("OAuth service transport failure phase=request: %s", type(exc).__name__)
            raise OAuthServiceUnavailable(
                UNAVAILABLE_MESSAGE, category=type(exc).__name__, retryable=False
            ) from exc
        finally:
            if own_client:
                await client.aclose()

    async def probe(self) -> bool:
        """Perform one authenticated readiness probe without launching anything."""
        try:
            response = await self._request("GET", "/_cognita/ready", auth=self._auth(), headers={"accept": "application/json"}, _skip_availability=True)
        except OAuthServiceUnavailable:
            return False
        return response.status_code == 200

    async def probe_once(self, *, timeout: float | None = None) -> httpx.Response:
        """Perform exactly one readiness request for the supervisor recovery loop."""
        request_timeout = {} if timeout is None else {"_request_timeout": timeout}
        return await self._request(
            "GET", "/_cognita/ready", auth=self._auth(),
            headers={"accept": "application/json"}, _skip_availability=True,
            **request_timeout,
        )

    async def introspect(self, raw_token: str) -> IntrospectionResult:
        if not isinstance(raw_token, str) or not raw_token:
            raise ValueError("raw_token must be a non-empty string")
        waiter = asyncio.get_running_loop().create_future()
        self._introspection_waiters.add(waiter)
        try:
            response = await self._request(
                "POST",
                "/_cognita/introspect",
                data={"token": raw_token},
                auth=self._auth(),
                headers={"accept": "application/json", "content-type": "application/x-www-form-urlencoded"},
            )
            if response.status_code != 200:
                log.error("OAuth introspection failed phase=internal-control status=%s", response.status_code)
                raise OAuthServiceUnavailable(UNAVAILABLE_MESSAGE)
            payload = response.json()
            if not isinstance(payload, dict) or type(payload.get("active")) is not bool:
                raise ValueError("active must be boolean")
            scopes = self._claims(payload.get("scope"), "scope", allow_string=True)
            audiences = self._claims(payload.get("aud"), "aud", allow_string=True)
            client_id = payload.get("client_id")
            subject = payload.get("sub")
            principal_id = payload.get("cognita_principal_id")
            if client_id is not None and not isinstance(client_id, str):
                raise ValueError("client_id must be string")
            if subject is not None and not isinstance(subject, str):
                raise ValueError("sub must be string")
            if principal_id is not None:
                if not isinstance(principal_id, str):
                    raise ValueError("cognita_principal_id must be string")
                try:
                    import uuid
                    principal_id = str(uuid.UUID(principal_id))
                except (ValueError, AttributeError, TypeError):
                    raise ValueError("cognita_principal_id must be UUID")
            return IntrospectionResult(payload["active"], scopes, audiences, client_id, subject, principal_id)
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            log.error("OAuth introspection returned malformed response phase=internal-control")
            raise OAuthServiceUnavailable(UNAVAILABLE_MESSAGE) from exc
        finally:
            if not waiter.done():
                waiter.set_result(None)
            self._introspection_waiters.discard(waiter)

    async def drain_introspections(self, timeout: float) -> bool:
        """Wait for already admitted introspections before stopping the child."""
        pending = tuple(
            waiter for waiter in self._introspection_waiters if not waiter.done()
        )
        if not pending:
            return True
        _done, still_pending = await asyncio.wait(pending, timeout=max(0.0, timeout))
        return not still_pending

    @staticmethod
    def _claims(value: Any, name: str, *, allow_string: bool) -> tuple[str, ...]:
        if value is None:
            return ()
        if allow_string and isinstance(value, str):
            return tuple(value.split()) if name == "scope" else (value,)
        if isinstance(value, (list, tuple)) and all(isinstance(item, str) and item for item in value):
            return tuple(value)
        raise ValueError(f"{name} has invalid type")

    async def list_connections(self) -> list[dict[str, Any]]:
        response = await self._request("GET", "/_cognita/connections", auth=self._auth(), headers={"accept": "application/json"})
        payload = self._control_json(response)
        if not isinstance(payload, list):
            raise OAuthServiceUnavailable(UNAVAILABLE_MESSAGE)
        return [self._connection_record(item) for item in payload]

    async def _csrf_control_delete(self, path: str) -> httpx.Response:
        """Obtain Django's standard CSRF cookie and use it for one control DELETE."""
        if not self._available():
            raise OAuthServiceUnavailable(UNAVAILABLE_MESSAGE)
        own_client = self._http_client is None
        client = self._http_client or httpx.AsyncClient(
            timeout=httpx.Timeout(self.timeout), follow_redirects=False, transport=self._transport
        )
        try:
            get_response = await client.request(
                "GET",
                self._url("/_cognita/connections"),
                auth=self._auth(),
                headers={"accept": "application/json"},
                follow_redirects=False,
            )
            if get_response.status_code != 200:
                log.error(
                    "OAuth internal CSRF bootstrap failed phase=internal-control status=%s",
                    get_response.status_code,
                )
                raise OAuthServiceUnavailable(UNAVAILABLE_MESSAGE)
            csrf_token = get_response.cookies.get("csrftoken") or client.cookies.get("csrftoken")
            if not isinstance(csrf_token, str) or not csrf_token:
                log.error("OAuth internal CSRF bootstrap returned no cookie phase=internal-control")
                raise OAuthServiceUnavailable(UNAVAILABLE_MESSAGE)
            return await client.request(
                "DELETE",
                self._url(path),
                auth=self._auth(),
                headers={
                    "accept": "application/json",
                    "Cookie": f"csrftoken={csrf_token}",
                    "X-CSRFToken": csrf_token,
                },
                follow_redirects=False,
            )
        except OAuthServiceUnavailable:
            raise
        except (httpx.HTTPError, TimeoutError) as exc:
            log.error("OAuth service transport failure phase=internal-control: %s", type(exc).__name__)
            raise OAuthServiceUnavailable(UNAVAILABLE_MESSAGE) from exc
        finally:
            if own_client:
                await client.aclose()

    async def revoke_connection(self, connection_id: str) -> int:
        if not connection_id or "/" in connection_id:
            raise ValueError("invalid connection id")
        response = await self._csrf_control_delete(f"/_cognita/connections/{connection_id}")
        if response.status_code == 404:
            return 0
        payload = self._control_json(response)
        if not isinstance(payload, dict) or not isinstance(payload.get("revoked_count"), int):
            raise OAuthServiceUnavailable(UNAVAILABLE_MESSAGE)
        return payload["revoked_count"]

    async def revoke_all_connections(self) -> int:
        response = await self._csrf_control_delete("/_cognita/connections")
        payload = self._control_json(response)
        if not isinstance(payload, dict) or not isinstance(payload.get("revoked_count"), int):
            raise OAuthServiceUnavailable(UNAVAILABLE_MESSAGE)
        return payload["revoked_count"]

    @staticmethod
    def _connection_record(item: Any) -> dict[str, Any]:
        if not isinstance(item, dict):
            raise OAuthServiceUnavailable(UNAVAILABLE_MESSAGE)
        allowed = {
            "id", "client_id", "client_name", "project", "connector",
            "resource", "created_at", "last_used_at",
        }
        if not isinstance(item.get("id"), str) or not set(item) <= allowed:
            raise OAuthServiceUnavailable(UNAVAILABLE_MESSAGE)
        record = dict(item)
        if "connector" in item:
            # Retired project-bound grants remain visible during the explicit
            # 9.0 cutover so an administrator can revoke them. The child cannot
            # attach a current connector summary to those resources, but every
            # non-null summary still crosses the strict shape validator below.
            record["connector"] = (
                None
                if item["connector"] is None
                else OAuthServiceClient._connector_summary(item["connector"])
            )
        return record

    @staticmethod
    def _connector_summary(value: Any) -> dict[str, Any]:
        """Validate the non-secret connector summary used by Admin clients."""
        if not isinstance(value, dict):
            raise OAuthServiceUnavailable(UNAVAILABLE_MESSAGE)
        allowed = {"id", "name", "enabled", "revision", "projects"}
        if set(value) - allowed:
            raise OAuthServiceUnavailable(UNAVAILABLE_MESSAGE)
        connector_id = value.get("id")
        if not isinstance(connector_id, str):
            raise OAuthServiceUnavailable(UNAVAILABLE_MESSAGE)
        try:
            normalized_id = str(uuid.UUID(connector_id))
        except (ValueError, AttributeError, TypeError):
            raise OAuthServiceUnavailable(UNAVAILABLE_MESSAGE) from None
        name = value.get("name")
        if name is not None and (not isinstance(name, str) or not name.strip()):
            raise OAuthServiceUnavailable(UNAVAILABLE_MESSAGE)
        if type(value.get("enabled")) is not bool:
            raise OAuthServiceUnavailable(UNAVAILABLE_MESSAGE)
        revision = value.get("revision")
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
            raise OAuthServiceUnavailable(UNAVAILABLE_MESSAGE)
        projects = value.get("projects")
        if not isinstance(projects, list):
            raise OAuthServiceUnavailable(UNAVAILABLE_MESSAGE)
        normalized_projects: list[dict[str, str]] = []
        for project in projects:
            if not isinstance(project, dict) or set(project) != {"name", "access"}:
                raise OAuthServiceUnavailable(UNAVAILABLE_MESSAGE)
            project_name = project.get("name")
            access = project.get("access")
            if not isinstance(project_name, str) or not project_name or access not in {"read", "write"}:
                raise OAuthServiceUnavailable(UNAVAILABLE_MESSAGE)
            normalized_projects.append({"name": project_name, "access": access})
        return {
            "id": normalized_id,
            "name": name,
            "enabled": value["enabled"],
            "revision": revision,
            "projects": normalized_projects,
        }

    @staticmethod
    def _control_json(response: httpx.Response) -> Any:
        if response.status_code < 200 or response.status_code >= 300:
            log.error("OAuth internal control failed phase=internal-control status=%s", response.status_code)
            raise OAuthServiceUnavailable(UNAVAILABLE_MESSAGE)
        try:
            return response.json()
        except (ValueError, json.JSONDecodeError) as exc:
            raise OAuthServiceUnavailable(UNAVAILABLE_MESSAGE) from exc

    async def forward(
        self,
        request: Any = None,
        *,
        method: str | None = None,
        path: str | None = None,
        query: str | bytes = "",
        headers: Mapping[str, str] | None = None,
        content: bytes | str | None = None,
    ) -> httpx.Response:
        """Forward a public OAuth request and return a sanitized protocol response."""
        if request is not None:
            method = getattr(request, "method", method)
            url = getattr(request, "url", None)
            path = path or getattr(url, "path", "/")
            query = query or getattr(url, "query", b"")
            if hasattr(request, "headers"):
                headers = headers or request.headers
            if content is None and hasattr(request, "body"):
                content = await request.body()
            client_host = getattr(getattr(request, "client", None), "host", None)
            if client_host in {"127.0.0.1", "::1"}:
                forwarded = request.headers.get("cf-connecting-ip", "").strip()
                if forwarded:
                    try:
                        client_host = str(ipaddress.ip_address(forwarded))
                    except ValueError:
                        pass
        else:
            client_host = None
        method = (method or "GET").upper()
        path = path or "/"
        if content is not None:
            if isinstance(content, str):
                content = content.encode("utf-8")
            if len(content) > self.max_body_bytes:
                return _json_response(413, {"error": "request_entity_too_large"})
        outgoing: list[tuple[str, str]] = []
        for name, value in (headers or {}).items():
            lowered = name.lower()
            if lowered in REQUEST_HEADERS and lowered not in HOP_BY_HOP_HEADERS:
                outgoing.append((name, value))
        issuer = urlsplit(self.public_base_url) if self.public_base_url else None
        if issuer and issuer.netloc:
            outgoing.append(("host", issuer.netloc))
            outgoing.append(("x-forwarded-proto", issuer.scheme))
        if client_host:
            outgoing.append(("x-real-ip", client_host))
        if content is not None:
            outgoing.append(("content-length", str(len(content))) )
        target = self._url(path)
        if query:
            target += "?" + (query.decode("ascii", "replace") if isinstance(query, bytes) else query)
        try:
            response = await self._request(method, path + (("?" + (query.decode("ascii", "replace") if isinstance(query, bytes) else query)) if query else ""), headers=outgoing, content=content)
        except OAuthServiceUnavailable:
            return _json_response(503, {"error": "temporarily_unavailable"})
        if len(response.content) > self.max_body_bytes:
            return _json_response(502, {"error": "upstream_response_too_large"}, request=response.request)
        response_headers = _safe_headers(response.headers)
        location = next((value for name, value in response_headers if name.lower() == "location"), None)
        if location and not self._valid_location(location):
            log.error("OAuth proxy rejected unsafe redirect phase=forward")
            return _json_response(502, {"error": "bad_gateway"}, request=response.request)
        return httpx.Response(response.status_code, headers=response_headers, content=response.content, request=response.request)

    def _valid_location(self, location: str) -> bool:
        parsed = urlsplit(location)
        if not parsed.scheme and not parsed.netloc:
            return location.startswith("/")
        if parsed.username or parsed.password or parsed.fragment or not parsed.hostname:
            return False
        if self.public_base_url:
            issuer = urlsplit(self.public_base_url)
            if parsed.scheme == issuer.scheme and parsed.netloc == issuer.netloc:
                return True
        if any(self._matches_registered_redirect(parsed, uri) for uri in self.accepted_redirect_uris):
            return True
        child = urlsplit(self.base_url)
        if parsed.scheme == child.scheme and parsed.netloc == child.netloc:
            return False
        host = parsed.hostname.lower()
        if parsed.scheme == "http":
            return host in {"127.0.0.1", "localhost", "::1"}
        if parsed.scheme != "https":
            return False
        return any(host == item or host.endswith("." + item) for item in self.allowed_redirect_hosts)

    @staticmethod
    def _matches_registered_redirect(location, registered: str) -> bool:
        expected = urlsplit(registered)
        return (
            not expected.fragment
            and not expected.username
            and not expected.password
            and expected.scheme in {"http", "https"}
            and bool(expected.hostname)
            and location.scheme == expected.scheme
            and location.netloc == expected.netloc
            and location.path == expected.path
            and (
                not expected.query
                or location.query == expected.query
                or location.query.startswith(expected.query + "&")
            )
        )


__all__ = ["IntrospectionResult", "OAuthServiceClient", "OAuthServiceUnavailable"]
