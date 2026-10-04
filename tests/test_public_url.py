"""Public URL override validation, persistence, and Admin API contracts."""

from __future__ import annotations

import json

import pytest
from httpx import ASGITransport, AsyncClient

from cognita.admin_api import create_admin_app
from cognita.config import CognitaConfig
from cognita.public_url import (
    PublicBaseURLStore,
    PublicURLValidationError,
    validate_public_base_url,
)
from cognita.registry import Registry
from cognita.tokens import hash_token


@pytest.mark.parametrize(
    "value",
    [
        "http://public.example",
        "https://user:password@public.example",
        "https://public.example/path?token=secret",
        "https://public.example/#fragment",
        "ftp://public.example",
        "https://public.example/with space",
    ],
)
def test_public_url_validation_rejects_unsafe_identity(value):
    with pytest.raises(PublicURLValidationError):
        validate_public_base_url(value)


def test_public_url_store_round_trip_and_deployment_fallback(tmp_path):
    config = CognitaConfig(data_root=tmp_path / "data", public_base_url="https://deploy.example")
    store = PublicBaseURLStore(config)
    assert store.effective() == "https://deploy.example"
    assert store.has_override() is False
    assert store.save("https://admin.example/") == "https://admin.example"
    assert PublicBaseURLStore(config).effective() == "https://admin.example"
    store.clear()
    assert store.effective() == "https://deploy.example"


def test_public_url_store_preserves_deployment_seed_after_runtime_projection(tmp_path):
    config = CognitaConfig(data_root=tmp_path / "data", public_base_url="https://deploy.example")
    store = PublicBaseURLStore(config)
    config.public_base_url = store.save("https://admin.example")

    store.clear()

    assert PublicBaseURLStore(config).effective() == "https://deploy.example"


def test_public_url_store_fails_closed_for_corrupt_persisted_state(tmp_path):
    config = CognitaConfig(data_root=tmp_path / "data", public_base_url="https://deploy.example")
    store = PublicBaseURLStore(config)
    store.path.parent.mkdir(parents=True)
    store.path.write_text(
        json.dumps({"version": 1, "public_base_url": "https://user:secret@example.test"}),
        encoding="utf-8",
    )

    with pytest.raises(PublicURLValidationError, match="persisted public base URL state is invalid"):
        store.effective()
    with pytest.raises(PublicURLValidationError, match="persisted public base URL state is invalid"):
        store.has_override()


async def test_admin_public_url_is_authenticated_persisted_and_runtime_effective(tmp_path):
    registry = Registry(tmp_path / "registry.yaml")
    config = CognitaConfig(
        registry_path=registry.path,
        data_root=tmp_path / "data",
        public_base_url="https://deploy.example",
        admin_allowed_hosts=["*"],
        admin_password_sha256=hash_token("password"),
    )
    app = create_admin_app(config, registry)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.get("/api/settings/public-base-url")).status_code == 401
        assert (await client.post("/api/login", json={"username": "admin", "password": "password"})).status_code == 200
        client.headers["X-CSRF-Token"] = client.cookies.get("cognita_csrf")
        response = await client.patch(
            "/api/settings/public-base-url", json={"public_base_url": "https://tunnel.example/"}
        )
        assert response.status_code == 200
        assert response.json() == {"public_base_url": "https://tunnel.example", "source": "admin"}
        status = await client.get("/api/public-base-url")
        assert status.json()["public_base_url"] == "https://tunnel.example"
        assert (config.data_root / "public-base-url.json").is_file()
