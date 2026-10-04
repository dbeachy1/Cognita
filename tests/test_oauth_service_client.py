from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from cognita.oauth_service_client import (
    IntrospectionResult,
    OAuthServiceClient,
    OAuthServiceUnavailable,
)


@pytest.mark.asyncio
async def test_introspection_uses_basic_auth_and_normalizes_claims() -> None:
    seen: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["authorization"] = request.headers["authorization"]
        seen["body"] = request.content
        return httpx.Response(
            200,
            json={"active": True, "scope": "cognita:access", "aud": ["https://c/mcp/p"], "client_id": "client", "sub": "subject"},
        )

    client = OAuthServiceClient(
        internal_client_id="internal",
        internal_client_secret="secret",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    try:
        result = await client.introspect("opaque-token")
    finally:
        await client._http_client.aclose()
    assert result == IntrospectionResult(True, ("cognita:access",), ("https://c/mcp/p",), "client", "subject")
    assert str(seen["authorization"]).startswith("Basic ")
    assert seen["body"] == b"token=opaque-token"


@pytest.mark.asyncio
async def test_introspection_drain_tracks_only_active_introspection() -> None:
    entered = asyncio.Event()
    release = asyncio.Event()

    async def handler(_: httpx.Request) -> httpx.Response:
        entered.set()
        await release.wait()
        return httpx.Response(200, json={"active": False})

    client = OAuthServiceClient(
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    task = asyncio.create_task(client.introspect("opaque-token"))
    try:
        await entered.wait()
        assert await client.drain_introspections(0) is False
        release.set()
        await task
        assert await client.drain_introspections(0) is True
    finally:
        release.set()
        await client._http_client.aclose()


@pytest.mark.asyncio
async def test_inactive_token_is_normal_but_malformed_response_is_unavailable() -> None:
    async def inactive(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"active": False})

    transport = httpx.MockTransport(inactive)
    client = OAuthServiceClient(http_client=httpx.AsyncClient(transport=transport))
    try:
        result = await client.introspect("token")
    finally:
        await client._http_client.aclose()
    assert result.active is False

    async def malformed(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"{not-json")

    client = OAuthServiceClient(http_client=httpx.AsyncClient(transport=httpx.MockTransport(malformed)))
    try:
        with pytest.raises(OAuthServiceUnavailable):
            await client.introspect("token")
    finally:
        await client._http_client.aclose()


@pytest.mark.asyncio
async def test_list_connections_accepts_connector_access_summary_and_rejects_extra_fields() -> None:
    connector_id = "2c520a44-2037-4bb5-a565-d88ec2bb02d1"
    record = {
        "id": "connection-1",
        "client_id": "client",
        "client_name": "ChatGPT",
        "project": None,
        "resource": "https://cognita.example/mcp/connectors/cognita/mcp/v5",
        "created_at": "2026-09-15T00:00:00Z",
        "last_used_at": None,
        "connector": {
            "id": connector_id,
            "name": "Cognita",
            "enabled": True,
            "revision": 7,
            "projects": [{"name": "KEI", "access": "write"}],
        },
    }

    async def valid(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[record])

    client = OAuthServiceClient(
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(valid))
    )
    try:
        connections = await client.list_connections()
    finally:
        await client._http_client.aclose()
    assert connections[0]["connector"] == record["connector"]

    legacy_record = {**record, "connector": None}

    async def legacy(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[legacy_record])

    client = OAuthServiceClient(
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(legacy))
    )
    try:
        connections = await client.list_connections()
    finally:
        await client._http_client.aclose()
    assert connections[0]["connector"] is None

    bad_record = {**record, "connector": {**record["connector"], "secret": "must reject"}}

    async def invalid(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[bad_record])

    client = OAuthServiceClient(
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(invalid))
    )
    try:
        with pytest.raises(OAuthServiceUnavailable):
            await client.list_connections()
    finally:
        await client._http_client.aclose()


@pytest.mark.asyncio
async def test_forward_preserves_repeated_cookies_and_rejects_loopback_location() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert "x-forwarded-for" not in request.headers
        assert request.headers["host"] == "public.example"
        return httpx.Response(
            302,
            headers=[("set-cookie", "a=1"), ("set-cookie", "b=2"), ("location", "https://public.example/ok")],
            content=b"redirect",
        )

    transport = httpx.MockTransport(handler)
    client = OAuthServiceClient(
        public_base_url="https://public.example",
        http_client=httpx.AsyncClient(transport=transport),
    )
    try:
        response = await client.forward(method="POST", path="/oauth/token", headers={"Host": "evil", "X-Forwarded-For": "evil", "Content-Type": "application/json"}, content=b"{}")
    finally:
        await client._http_client.aclose()
    assert response.status_code == 302
    assert response.headers.get_list("set-cookie") == ["a=1", "b=2"]

    async def leaked(_: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "http://127.0.0.1:8778/oauth/login"})

    client = OAuthServiceClient(http_client=httpx.AsyncClient(transport=httpx.MockTransport(leaked)))
    try:
        response = await client.forward(method="GET", path="/oauth/authorize")
    finally:
        await client._http_client.aclose()
    assert response.status_code == 502
    assert json.loads(response.content) == {"error": "bad_gateway"}


@pytest.mark.asyncio
async def test_forward_accepts_policy_allowed_callbacks_but_not_untrusted_hosts() -> None:
    locations = iter([
        "https://chatgpt.com/oauth/callback?code=one&state=two",
        "http://127.0.0.1:49152/callback?code=one",
        "https://evil.example/callback?code=one",
    ])

    async def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": next(locations)})

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = OAuthServiceClient(
        public_base_url="https://public.example",
        allowed_redirect_hosts=["chatgpt.com"],
        http_client=http_client,
    )
    try:
        first = await client.forward(method="GET", path="/oauth/authorize")
        second = await client.forward(method="GET", path="/oauth/authorize")
        rejected = await client.forward(method="GET", path="/oauth/authorize")
    finally:
        await http_client.aclose()
    assert first.status_code == 302
    assert second.status_code == 302
    assert rejected.status_code == 502

@pytest.mark.asyncio
async def test_revoke_controls_bootstrap_django_csrf_and_preserve_basic_auth() -> None:
    seen: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.method == "GET":
            return httpx.Response(
                200,
                json=[],
                headers={"set-cookie": "csrftoken=csrf-value; Path=/"},
                request=request,
            )
        assert request.method == "DELETE"
        assert request.headers["authorization"].startswith("Basic ")
        assert request.headers["cookie"] == "csrftoken=csrf-value"
        assert request.headers["x-csrftoken"] == "csrf-value"
        return httpx.Response(200, json={"revoked_count": 1}, request=request)

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = OAuthServiceClient(
        internal_client_id="internal",
        internal_client_secret="secret",
        http_client=http_client,
    )
    try:
        assert await client.revoke_connection("connection-id") == 1
        assert await client.revoke_all_connections() == 1
    finally:
        await http_client.aclose()
    assert [request.method for request in seen] == ["GET", "DELETE", "GET", "DELETE"]
