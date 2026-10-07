from __future__ import annotations

from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient

from cognita.admin_api import CSRF_COOKIE, create_admin_app
from cognita.config import CognitaConfig
from cognita.registry import Project, Registry
from cognita.tokens import hash_token

PASSWORD = "queue-test-password"


class FakeWatcher:
    def __init__(self):
        self.calls = []

    async def clear_queue(self, name):
        self.calls.append(name)
        return {"project": name, "cleared_paths": 3, "active_cancelled": True}


def _make_app(tmp_path, *, watcher, authenticated=False):
    docs = tmp_path / "docs"
    docs.mkdir()
    registry = Registry(tmp_path / "registry.yaml")
    registry.add(Project(name="KEI", documents_dir=docs, data_dir=tmp_path / "data" / "KEI"))
    config = CognitaConfig(
        registry_path=registry.path,
        data_root=tmp_path / "data",
        admin_allowed_hosts=["*"],
        admin_password_sha256=hash_token(PASSWORD) if authenticated else "",
    )
    engine = SimpleNamespace(watcher=watcher)
    return create_admin_app(config, registry, engine=engine), watcher


async def _client(app):
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.mark.asyncio
async def test_clear_watcher_queue_requires_session_and_csrf(tmp_path):
    app, watcher = _make_app(tmp_path, watcher=FakeWatcher(), authenticated=True)
    async with await _client(app) as client:
        endpoint = "/api/projects/KEI/watcher/clear-queue"
        assert (await client.post(endpoint)).status_code == 401
        assert (await client.post("/api/login", json={"username": "admin", "password": PASSWORD})).status_code == 200
        assert (await client.post(endpoint)).status_code == 403
        csrf = client.cookies.get(CSRF_COOKIE)
        result = await client.post(endpoint, headers={"X-CSRF-Token": csrf})
    assert result.status_code == 200
    assert result.json() == {"project": "KEI", "cleared_paths": 3, "active_cancelled": True}
    assert watcher.calls == ["KEI"]


@pytest.mark.asyncio
async def test_clear_watcher_queue_requires_project_and_available_watcher(tmp_path):
    app, _ = _make_app(tmp_path, watcher=None)
    async with await _client(app) as client:
        missing_project = await client.post("/api/projects/missing/watcher/clear-queue")
        unavailable = await client.post("/api/projects/KEI/watcher/clear-queue")
    assert missing_project.status_code == 404
    assert unavailable.status_code == 503
