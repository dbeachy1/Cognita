"""16.1.5: CIMD from any host is bounded by the client cap and a first-sight budget."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def test_cimd_first_sight_budget_cap_and_fetch_backoff() -> None:
    script = r'''
from pathlib import Path
from tempfile import TemporaryDirectory

import yaml
from argon2 import PasswordHasher

from cognita.oauth_service.asgi import create_application
from cognita.oauth_service.bootstrap import bootstrap_service

with TemporaryDirectory(prefix="cognita-cimd-first-sight-") as owned:
    root = Path(owned)
    data = root / "data"
    data.mkdir()
    registry = root / "registry.yaml"
    registry.write_text(yaml.safe_dump({"version": 1, "projects": []}), encoding="utf-8")
    connectors = root / "connectors.yaml"
    connectors.write_text(yaml.safe_dump({"version": 1, "revision": 1, "connectors": []}), encoding="utf-8")
    config = root / "cognita.yaml"
    config.write_text(yaml.safe_dump({
        "data_root": str(data),
        "registry_path": str(registry),
        "connectors_path": str(connectors),
        "public_base_url": "https://beta.example.test",
        "oauth_service_store_path": str(data / "oauth.sqlite3"),
        "admin_username": "admin",
        "admin_password_hash": PasswordHasher().hash("pw"),
    }), encoding="utf-8")
    context = bootstrap_service(config)
    create_application(context)

    from django import db
    from oauth2_provider import cimd as dot_cimd
    from oauth2_provider.models import get_application_model
    from oauth2_provider.settings import oauth2_settings

    from cognita.oauth_service import cimd_auth
    from cognita.oauth_service.views import REGISTERED_CLIENT_CAPACITY

    def permitted(url):
        return dot_cimd._registration_permitted(None, url)

    now = [1000.0]
    cimd_auth._first_sight_clock = lambda: now[0]
    cimd_auth.clear_caches_for_tests()

    # Any host is admitted, up to the hourly budget of new URLs.
    budget = cimd_auth.FIRST_SIGHT_CIMD_PER_HOUR
    urls = [f"https://client{i}.example/meta.json" for i in range(budget + 1)]
    for url in urls[:budget]:
        assert permitted(url), url
    # Both resolvers ask about one request; a just-admitted URL costs nothing more.
    assert permitted(urls[0])
    assert not permitted(urls[budget])

    # A client that already has a row always refreshes, budget or not.
    application_model = get_application_model()
    application_model.objects.create(
        client_id="https://claude.ai/oauth/mcp-oauth-client-metadata",
        name="Claude",
        client_type=application_model.CLIENT_PUBLIC,
        authorization_grant_type=application_model.GRANT_AUTHORIZATION_CODE,
        redirect_uris="https://claude.ai/api/mcp/auth_callback",
        client_secret="",
    )
    assert permitted("https://claude.ai/oauth/mcp-oauth-client-metadata")

    # The budget refills after the window.
    now[0] += cimd_auth.FIRST_SIGHT_WINDOW_SECONDS + 1
    assert permitted(urls[budget])

    # A full client cap refuses first-sight URLs, which keeps DCR room safe.
    existing = application_model.objects.exclude(client_id=context.internal_client_id).count()
    for index in range(REGISTERED_CLIENT_CAPACITY - existing):
        application_model.objects.create(
            client_id=f"filler-{index}",
            name="Filler",
            client_type=application_model.CLIENT_PUBLIC,
            authorization_grant_type=application_model.GRANT_AUTHORIZATION_CODE,
            redirect_uris="https://filler.example/cb",
            client_secret="",
        )
    now[0] += cimd_auth.FIRST_SIGHT_WINDOW_SECONDS + 1
    assert not permitted("https://www.typingmind.com/client.json")
    application_model.objects.filter(client_id__startswith="filler-").delete()

    # A failed fetch backs off instead of refetching on every request.
    calls = []

    class FailingFetcher:
        def fetch(self, client_id):
            calls.append(client_id)
            raise ValueError("unreachable")

    oauth2_settings.user_settings["CIMD_METADATA_FETCHER"] = FailingFetcher
    failing = "https://broken.example/meta.json"
    for _ in range(3):
        try:
            cimd_auth._fetch_metadata(failing)
        except ValueError:
            pass
        else:
            raise AssertionError("fetch should fail")
    assert calls == [failing], calls

    db.connections.close_all()
'''
    env = dict(os.environ)
    env["PYTHONPATH"] = str(Path(__file__).parents[1] / "src")
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).parents[1],
        env=env,
        capture_output=True,
        text=True,
        timeout=90,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
