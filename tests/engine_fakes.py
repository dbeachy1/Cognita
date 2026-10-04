"""A minimal stand-in for ``LocalEngineHost`` for the gateway and admin tests.

14.0.0: these tests used to hand ``create_gateway_app`` a ``FakeSupervisor``
(``status_of`` -> RUNNING, ``port_of`` -> 8677) and monkeypatch
``gateway_mod.make_client`` so the proxy talked to an in-process fake worker app.
The supervisor is gone with the 3.x worker engine; the gateway now routes to an
engine host. ``FakeEngineHost`` is the smallest engine that satisfies what the
gateway and admin apps read from it:

- ``url_for(name)``  -> ``http://127.0.0.1:8677/mcp``, the URL the fake worker
  apps already serve (an ASGI transport ignores the host, but the path matters).
- ``make_client()``  -> an ``httpx.AsyncClient`` over ``ASGITransport(app)`` so
  proxied traffic lands in the fake worker app and nowhere else.
- ``start_background_reindex(project, mode)`` -> a recorded no-op.

Anything else the apps ask of an engine is read with ``getattr(engine, ..., None)``
(watcher, source_guard, index_status, core), so a missing attribute reports
"not configured" rather than raising.
"""

from __future__ import annotations

from httpx import ASGITransport, AsyncClient

FAKE_WORKER_URL = "http://127.0.0.1:8677/mcp"


class FakeEngineHost:
    """Route the gateway's proxy traffic to one in-process ASGI app."""

    def __init__(self, app) -> None:
        self.app = app
        self.reindex_requests: list[tuple[str, str]] = []

    def url_for(self, name: str) -> str:
        return FAKE_WORKER_URL

    def make_client(self) -> AsyncClient:
        return AsyncClient(transport=ASGITransport(app=self.app))

    def start_background_reindex(self, project, mode: str = "incremental") -> None:
        self.reindex_requests.append((getattr(project, "name", str(project)), mode))
