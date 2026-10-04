from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def test_oauth_host_bootstrap_dcr_and_control_surface(tmp_path: Path) -> None:
    """Exercise the child host in a fresh interpreter and owned temporary database."""
    script = r'''
from pathlib import Path
from tempfile import TemporaryDirectory
import base64
import os
import html
import json
import re
from urllib.parse import parse_qs, urlsplit
import yaml
from argon2 import PasswordHasher
from django import db
from django.test import Client
from cognita.oauth_service.asgi import create_application
from cognita.oauth_service.bootstrap import bootstrap_service

with TemporaryDirectory(prefix="cognita-oauth8-host-test-") as root:
    base = Path(root)
    data = base / "data"
    registry = base / "registry.yaml"
    data.mkdir()
    registry.write_text(yaml.safe_dump({
        "version": 1,
        "projects": [{
            "name": "demo",
            "documents_dir": str(base / "docs"),
            "data_dir": str(base / "project"),
            "enabled": True,
        }],
    }), encoding="utf-8")
    connectors = base / "connectors.yaml"
    connectors.write_text(yaml.safe_dump({
        "version": 1,
        "revision": 1,
        "connectors": [{
            "id": "2c520a44-2037-4bb5-a565-d88ec2bb02d1",
            "name": "Cognita",
            "enabled": True,
            "project_mode": "all",
            "default_access": "write",
            "project_access": {},
        }],
    }), encoding="utf-8")
    config = base / "cognita.yaml"
    config.write_text(yaml.safe_dump({
        "data_root": str(data),
        "registry_path": str(registry),
        "public_base_url": "https://kei.example",
        "connectors_path": str(connectors),
        "oauth_service_store_path": str(data / "oauth.sqlite3"),
        "admin_password_hash": PasswordHasher().hash("pw"),
        "admin_username": "admin",
    }), encoding="utf-8")

    context = bootstrap_service(config)
    assert not (data / "oauth-backups").exists()
    if os.name == "posix":
        assert context.store_path.stat().st_mode & 0o777 == 0o600
    create_application(context)
    client = Client()
    internal = "Basic " + base64.b64encode(
        (context.internal_client_id + ":" + context.key.hex()).encode()
    ).decode()

    assert client.get("/_cognita/ready").status_code == 401
    ready = client.get("/_cognita/ready", HTTP_AUTHORIZATION=internal)
    assert ready.status_code == 200
    assert ready.json() == {"ready": True}

    metadata = client.get("/.well-known/oauth-authorization-server")
    assert metadata.status_code == 200
    assert metadata.json()["issuer"] == "https://kei.example"
    assert metadata.json()["scopes_supported"] == ["cognita:access"]
    assert metadata.json()["client_id_metadata_document_supported"] is True

    # An Admin-saved identity must update the long-lived child before Django's
    # Host validation runs; the child is not restarted for this mutation.
    public_url_state = data / "public-base-url.json"
    public_url_state.write_text(json.dumps({
        "version": 1,
        "public_base_url": "https://new-tunnel.example",
    }), encoding="utf-8")
    changed_metadata = client.get(
        "/.well-known/oauth-authorization-server",
        HTTP_HOST="new-tunnel.example",
    )
    assert changed_metadata.status_code == 200
    assert changed_metadata.json()["issuer"] == "https://new-tunnel.example"
    public_url_state.unlink()
    restored_metadata = client.get(
        "/.well-known/oauth-authorization-server",
        HTTP_HOST="kei.example",
    )
    assert restored_metadata.status_code == 200
    assert restored_metadata.json()["issuer"] == "https://kei.example"

    valid = {
        "client_name": "<Client & Test>",
        "redirect_uris": ["https://chatgpt.com/callback"],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
        "scope": "cognita:access",
    }
    created = client.post(
        "/oauth/register", data=json.dumps(valid), content_type="application/json"
    )
    assert created.status_code == 201
    assert created.json()["token_endpoint_auth_method"] == "none"

    authorization = {
        "client_id": created.json()["client_id"],
        "redirect_uri": "https://chatgpt.com/callback",
        "response_type": "code",
        "code_challenge": "a" * 43,
        "code_challenge_method": "S256",
        "resource": "https://kei.example/mcp/connectors/cognita/mcp/v5",
        "scope": "cognita:access",
    }
    rejected_authorization = client.get(
        "/oauth/authorize", {**authorization, "client_id": "missing-client"}
    )
    assert rejected_authorization.status_code == 400
    assert "/oauth/login" not in rejected_authorization.get("Location", "")
    assert "Authorization could not continue" in rejected_authorization.text, rejected_authorization.text
    assert "oauth2_provider/css/oauth2_provider.css" not in rejected_authorization.text
    assert "<Client & Test>" not in rejected_authorization.text

    invalid_login_request = client.get(
        "/oauth/login", HTTP_ACCEPT_LANGUAGE="fr-FR, en-US;q=0.8"
    )
    assert invalid_login_request.status_code == 400
    assert 'lang="fr-FR"' in invalid_login_request.text
    assert "La demande d’autorisation n’a pas pu être validée." in invalid_login_request.text
    assert '"error_description"' not in invalid_login_request.text

    login_redirect = client.get("/oauth/authorize", authorization)
    assert login_redirect.status_code == 302
    login_page = client.get(login_redirect["Location"])
    assert login_page.status_code == 200
    assert login_page["Cache-Control"] == "no-store"
    assert login_page["Content-Security-Policy"].startswith("default-src 'none'")
    assert login_page["X-Frame-Options"] == "DENY"
    assert "Path=/oauth" in login_page.cookies["csrftoken"].output()
    assert "oauth2_provider/css/oauth2_provider.css" not in login_page.text
    assert not re.search(r"<link\b[^>]*stylesheet", login_page.text, re.I)
    assert "prefers-color-scheme" in login_page.text
    assert "prefers-reduced-motion" in login_page.text
    assert 'autocomplete="current-password"' in login_page.text
    next_url = html.unescape(
        re.search(r'name=["\']?next["\']? value="([^"]+)"', login_page.content.decode()).group(1)
    )
    for _ in range(5):
        failed = client.post(
            "/oauth/login",
            {"username": "admin", "password": "wrong", "next": next_url},
            HTTP_X_REAL_IP="203.0.113.41",
        )
        assert failed.status_code == 401
    limited = client.post(
        "/oauth/login",
        {"username": "admin", "password": "wrong", "next": next_url},
        HTTP_X_REAL_IP="203.0.113.41",
    )
    assert limited.status_code == 429

    successful = client.post(
        "/oauth/login",
        {"username": "admin", "password": "pw", "next": next_url},
        HTTP_X_REAL_IP="203.0.113.42",
    )
    assert successful.status_code == 302
    consent = client.get(successful["Location"])
    assert consent.status_code == 200
    assert "oauth2_provider/css/oauth2_provider.css" not in consent.text
    assert not re.search(r"<link\b[^>]*stylesheet", consent.text, re.I)
    assert "name=\"allow\"" in consent.text
    assert "name=\"deny\"" in consent.text
    assert "&lt;Client &amp; Test&gt;" in consent.text
    assert "<Client & Test>" not in consent.text
    assert "future enabled projects are included unless excluded" in consent.text
    assert "includes all enabled projects" not in consent.text

    malformed_consent = client.post("/oauth/authorize", {"allow": "1"})
    assert malformed_consent.status_code == 200
    assert "The authorization request could not be validated." in malformed_consent.text
    assert "This field is required." not in malformed_consent.text

    # Language selection is a separate CSRF-protected POST. It changes only the
    # host-only UI cookie and revalidates the same authorization request.
    language_client = Client(enforce_csrf_checks=True)
    language_authorization = {**authorization, "state": "language-state"}
    login_redirect = language_client.get(
        "/oauth/authorize", language_authorization,
        HTTP_ACCEPT_LANGUAGE="fr-FR, en-US;q=0.8",
    )
    language_login = language_client.get(
        login_redirect["Location"], HTTP_ACCEPT_LANGUAGE="fr-FR, en-US;q=0.8"
    )
    assert 'lang="fr-FR"' in language_login.text
    language_csrf = re.search(
        r'name=["\']?csrfmiddlewaretoken["\']? value="([^"\']+)"', language_login.text
    ).group(1)
    language_next = html.unescape(
        re.search(r'name="next" value="([^"]+)"', language_login.text).group(1)
    )
    csrf_rejected = language_client.post(
        "/oauth/login",
        {"language": "pt-BR", "change_language": "1", "next": language_next},
    )
    assert csrf_rejected.status_code == 403
    switched = language_client.post(
        "/oauth/login",
        {"csrfmiddlewaretoken": language_csrf, "language": "pt-BR",
         "change_language": "1", "next": language_next},
        follow=False,
    )
    assert switched.status_code == 302
    assert switched["Location"].startswith("/oauth/login?")
    language_cookie = switched.cookies["cognita_lang"].output()
    assert "Path=/oauth" in language_cookie and "Secure" in language_cookie
    assert "HttpOnly" in language_cookie and "SameSite=Lax" in language_cookie
    language_login = language_client.get(switched["Location"], HTTP_ACCEPT_LANGUAGE="es-ES")
    assert 'lang="pt-BR"' in language_login.text
    assert "Entrar" in language_login.text
    language_csrf = re.search(
        r'name=["\']?csrfmiddlewaretoken["\']? value="([^"\']+)"', language_login.text
    ).group(1)
    logged_in = language_client.post(
        "/oauth/login",
        {"csrfmiddlewaretoken": language_csrf, "username": "admin", "password": "pw",
         "next": language_next},
        follow=False,
    )
    assert logged_in.status_code == 302
    language_consent = language_client.get(logged_in["Location"])
    assert language_consent.status_code == 200 and 'lang="pt-BR"' in language_consent.text
    assert "Acesso ao conector" in language_consent.text
    language_csrf = re.search(
        r'name=["\']?csrfmiddlewaretoken["\']? value="([^"\']+)"', language_consent.text
    ).group(1)
    language_next = html.unescape(
        re.search(r'name="next" value="([^"]+)"', language_consent.text).group(1)
    )
    consent_switch = language_client.post(
        "/oauth/authorize",
        {"csrfmiddlewaretoken": language_csrf, "language": "de-DE",
         "change_language": "1", "next": language_next},
        follow=False,
    )
    assert consent_switch.status_code == 302
    assert parse_qs(urlsplit(consent_switch["Location"]).query)["state"] == ["language-state"]
    language_consent = language_client.get(consent_switch["Location"])
    assert language_consent.status_code == 200 and 'lang="de-DE"' in language_consent.text
    assert "Zugriff" in language_consent.text

    language_client.cookies.pop("cognita_lang", None)
    invalid_switch = language_client.post(
        "/oauth/authorize",
        {"csrfmiddlewaretoken": language_csrf, "language": "es-ES",
         "change_language": "1", "next": "/oauth/authorize?client_id=missing-client"},
    )
    assert invalid_switch.status_code == 400
    assert "cognita_lang" not in invalid_switch.cookies

    for _ in range(99):
        capacity_entry = client.post(
            "/oauth/register", data=json.dumps(valid), content_type="application/json"
        )
        assert capacity_entry.status_code == 201
    capacity_rejected = client.post(
        "/oauth/register", data=json.dumps(valid), content_type="application/json"
    )
    assert capacity_rejected.status_code == 503

    invalid = dict(valid)
    invalid["redirect_uris"] = ["https://evil.example/callback"]
    rejected = client.post(
        "/oauth/register", data=json.dumps(invalid), content_type="application/json"
    )
    assert rejected.status_code == 400
    assert client.get("/_cognita/connections", HTTP_AUTHORIZATION=internal).json() == []

    db.connections.close_all()
assert not Path(root).exists()
'''
    env = dict(os.environ)
    env["PYTHONPATH"] = str(Path(__file__).parents[1] / "src")
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).parents[1],
        env=env,
        capture_output=True,
        text=True,
        # This performs 100 persisted DCR writes plus Django bootstrap in the
        # child. Slower Windows hosts can legitimately exceed 45 seconds.
        timeout=90,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
