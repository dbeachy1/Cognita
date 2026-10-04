"""Executable Django OAuth Toolkit 3.4.1 proof spike for Cognita 8.0.

The script creates only synthetic users, applications, and tokens in a task-owned
temporary SQLite database. It prints redacted statuses and model metadata, never
token values or credentials.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import django
from django.conf import settings
from django.core.management import call_command
from django.db import close_old_connections, connection
from django.test import Client
from django.urls import include, path

CODE_VERIFIER = "v" * 64
CODE_CHALLENGE = base64.urlsafe_b64encode(
    hashlib.sha256(CODE_VERIFIER.encode()).digest()
).rstrip(b"=").decode()
RESOURCE = "https://cognita.example/mcp/KEI"
REDIRECT = "https://client.example/callback"
INTROSPECTION_SECRET = "synthetic-introspection-secret"
APPLICATION_COUNTER = 0
SYNTHETIC_PASSWORD = "synthetic-password-never-production"


def checksum(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def configure(db_path: Path, grace_seconds: int = 0, hash_only: bool = True) -> None:
    settings.configure(
        DEBUG=False,
        SECRET_KEY="synthetic-django-secret",
        ALLOWED_HOSTS=["testserver", "localhost"],
        ROOT_URLCONF=__name__,
        MIDDLEWARE=[
            "django.contrib.sessions.middleware.SessionMiddleware",
            "django.contrib.auth.middleware.AuthenticationMiddleware",
            "django.contrib.messages.middleware.MessageMiddleware",
        ],
        INSTALLED_APPS=[
            "django.contrib.auth",
            "django.contrib.contenttypes",
            "django.contrib.sessions",
            "oauth2_provider",
        ],
        DATABASES={
            "default": {
                "ENGINE": "django.db.backends.sqlite3",
                "NAME": str(db_path),
                # Django 5.1+ IMMEDIATE transactions acquire the SQLite write
                # lock before DOT reads and mutates a refresh family.
                "OPTIONS": {"timeout": 20, "transaction_mode": "IMMEDIATE"},
            }
        },
        TEMPLATES=[
            {
                "BACKEND": "django.template.backends.django.DjangoTemplates",
                "APP_DIRS": True,
                "OPTIONS": {"context_processors": [
                    "django.template.context_processors.request",
                    "django.contrib.auth.context_processors.auth",
                    "django.contrib.messages.context_processors.messages",
                ]},
            }
        ],
        USE_TZ=True,
        LOGIN_URL="/login/",
        OAUTH2_PROVIDER={
            "SCOPES": {"cognita:access": "Cognita project access", "introspection": "Synthetic introspection"},
            "DEFAULT_SCOPES": ["cognita:access"],
            "PKCE_REQUIRED": True,
            "ROTATE_REFRESH_TOKEN": True,
            "REFRESH_TOKEN_GRACE_PERIOD_SECONDS": grace_seconds,
            "REFRESH_TOKEN_REUSE_PROTECTION": True,
            "ACCESS_TOKEN_EXPIRE_SECONDS": 3600,
            "COMPLIANT_BCP_RFC9700_TOKEN_STORAGE": hash_only,
        },
    )
    django.setup()
    global urlpatterns
    urlpatterns = [path("o/", include("oauth2_provider.urls", namespace="oauth2_provider"))]
    call_command("migrate", verbosity=0, interactive=False)


def new_client() -> Client:
    client = Client()
    client.defaults["HTTP_HOST"] = "testserver"
    return client


def create_application():
    global APPLICATION_COUNTER
    APPLICATION_COUNTER += 1
    from django.contrib.auth import get_user_model
    from oauth2_provider.models import get_application_model

    user = get_user_model().objects.create_user(
        username=f"synthetic-admin-{APPLICATION_COUNTER}", password=SYNTHETIC_PASSWORD
    )
    app_model = get_application_model()
    app = app_model.objects.create(
        name="Cognita proof client",
        user=user,
        client_type=app_model.CLIENT_PUBLIC,
        authorization_grant_type=app_model.GRANT_AUTHORIZATION_CODE,
        redirect_uris=REDIRECT,
        skip_authorization=True,
    )
    return user, app


def authorize_and_exchange(client: Client, app) -> dict:
    query = {
        "response_type": "code",
        "client_id": app.client_id,
        "redirect_uri": REDIRECT,
        "code_challenge": CODE_CHALLENGE,
        "code_challenge_method": "S256",
        "scope": "cognita:access",
        "resource": RESOURCE,
    }
    response = client.get("/o/authorize/", query, follow=False)
    if response.status_code not in {302, 303}:
        raise AssertionError(f"authorization did not redirect: {response.status_code}")
    location = response["Location"]
    query_values = parse_qs(urlparse(location).query)
    code = query_values["code"][0]
    token_response = client.post(
        "/o/token/",
        data={
            "grant_type": "authorization_code",
            "client_id": app.client_id,
            "redirect_uri": REDIRECT,
            "code": code,
            "code_verifier": CODE_VERIFIER,
            "resource": RESOURCE,
        },
    )
    if token_response.status_code != 200:
        raise AssertionError(f"token exchange failed: {token_response.status_code} {token_response.content!r}")
    return token_response.json()


def token_request(client: Client, app, token: str):
    return client.post(
        "/o/token/",
        data={
            "grant_type": "refresh_token",
            "client_id": app.client_id,
            "refresh_token": token,
            "resource": RESOURCE,
        },
    )


def introspect(client: Client, token: str, introspection_token: str):
    return client.post(
        "/o/introspect/",
        data={"token": token},
        HTTP_AUTHORIZATION=f"Bearer {introspection_token}",
    )


def create_confidential_introspection_client():
    global APPLICATION_COUNTER
    APPLICATION_COUNTER += 1
    from django.contrib.auth import get_user_model
    from oauth2_provider.models import get_application_model

    user = get_user_model().objects.create_user(
        username=f"synthetic-introspection-client-{APPLICATION_COUNTER}",
        password=SYNTHETIC_PASSWORD,
    )
    app_model = get_application_model()
    app = app_model.objects.create(
        name="Cognita proof introspection client",
        user=user,
        client_type=app_model.CLIENT_CONFIDENTIAL,
        authorization_grant_type=app_model.GRANT_CLIENT_CREDENTIALS,
        client_secret="synthetic-introspection-client-secret",
        hash_client_secret=False,
    )
    return app


def introspect_basic(client: Client, token: str, app):
    credentials = base64.b64encode(
        f"{app.client_id}:{app.client_secret}".encode()
    ).decode()
    return client.post(
        "/o/introspect/",
        data={"token": token},
        HTTP_AUTHORIZATION=f"Basic {credentials}",
    )


def create_introspection_token(user, app) -> str:
    from datetime import timedelta
    from django.utils import timezone
    from oauth2_provider.models import get_access_token_model, set_token_value

    raw = "synthetic-introspection-bearer"
    token = get_access_token_model()(
        user=user,
        application=app,
        scope="introspection",
        expires=timezone.now() + timedelta(hours=1),
        resource=[],
    )
    set_token_value(token, raw)
    token.save()
    return raw


def legacy_hash_import_probe(client: Client, introspection_token: str) -> dict:
    """Prove native DOT can consume synthetic legacy checksum rows without plaintext."""
    import uuid
    from datetime import timedelta
    from django.utils import timezone
    from oauth2_provider.models import (
        get_access_token_model,
        get_refresh_token_model,
    )

    user, app = create_application()
    access_raw = "synthetic-legacy-access"
    refresh_raw = "synthetic-legacy-refresh"
    access_model = get_access_token_model()
    refresh_model = get_refresh_token_model()
    access = access_model.objects.create(
        user=user,
        application=app,
        token="",
        token_checksum=checksum(access_raw),
        expires=timezone.now() + timedelta(hours=1),
        scope="cognita:access",
        resource=[RESOURCE],
    )
    refresh = refresh_model.objects.create(
        user=user,
        application=app,
        access_token=access,
        token="",
        token_checksum=checksum(refresh_raw),
        token_family=uuid.uuid4(),
        resource=[RESOURCE],
    )
    imported_introspection = introspect(client, access_raw, introspection_token)
    refresh_response = token_request(client, app, refresh_raw)
    successor = refresh_response.json() if refresh_response.status_code == 200 else {}
    successor_introspection = (
        introspect(client, successor["access_token"], introspection_token)
        if refresh_response.status_code == 200
        else None
    )

    revoked_user, revoked_app = create_application()
    revoked_access = access_model.objects.create(
        user=revoked_user,
        application=revoked_app,
        token="",
        token_checksum=checksum("synthetic-revoked-access"),
        expires=timezone.now() + timedelta(hours=1),
        scope="cognita:access",
        resource=[RESOURCE],
    )
    refresh_model.objects.create(
        user=revoked_user,
        application=revoked_app,
        access_token=revoked_access,
        token="",
        token_checksum=checksum("synthetic-revoked-refresh"),
        token_family=uuid.uuid4(),
        resource=[RESOURCE],
        revoked=timezone.now(),
    )
    revoked_response = token_request(
        client, revoked_app, "synthetic-revoked-refresh"
    )
    return {
        "imported_access_introspection_status": imported_introspection.status_code,
        "imported_access_active": imported_introspection.json().get("active") is True,
        "imported_refresh_status": refresh_response.status_code,
        "imported_successor_introspection_status": (
            successor_introspection.status_code if successor_introspection else None
        ),
        "imported_successor_active": (
            successor_introspection.json().get("active") is True
            if successor_introspection
            else False
        ),
        "imported_rows_have_no_plaintext": not bool(access.token or refresh.token),
        "revoked_refresh_status": revoked_response.status_code,
        "revoked_refresh_error": revoked_response.json().get("error"),
    }


def row_metadata() -> dict:
    from oauth2_provider.models import get_access_token_model, get_refresh_token_model

    access_model = get_access_token_model()
    refresh_model = get_refresh_token_model()
    access_fields = {field.name for field in access_model._meta.fields}
    refresh_fields = {field.name for field in refresh_model._meta.fields}
    with connection.cursor() as cursor:
        table_names = {
            row[0] for row in cursor.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'oauth2_provider_%'"
            )
        }
    return {
        "access_fields": sorted(access_fields),
        "refresh_fields": sorted(refresh_fields),
        "hash_only_access_column": "token_checksum" in access_fields and "token" in access_fields,
        "hash_only_refresh_column": "token_checksum" in refresh_fields and "token" in refresh_fields,
        "oauth_tables": sorted(table_names),
    }


def run_proof(
    db_path: Path,
    grace_seconds: int = 0,
    hash_only: bool = True,
    include_migration: bool = True,
) -> dict:
    configure(db_path, grace_seconds, hash_only)
    from oauth2_provider.models import get_access_token_model, get_refresh_token_model

    client = new_client()
    user, app = create_application()
    client.force_login(user)
    introspection_token = create_introspection_token(user, app)
    first = authorize_and_exchange(client, app)
    public_keys = sorted(first)
    introspected = introspect(client, first["access_token"], introspection_token)
    if introspected.status_code != 200:
        raise AssertionError(f"introspection failed: {introspected.status_code}")
    introspection_json = introspected.json()
    basic_client = create_confidential_introspection_client()
    basic_introspected = introspect_basic(client, first["access_token"], basic_client)
    basic_introspection_json = (
        basic_introspected.json()
        if basic_introspected.headers.get("Content-Type", "").startswith("application/json")
        else {}
    )

    first_refresh = token_request(client, app, first["refresh_token"])
    if first_refresh.status_code != 200:
        raise AssertionError(f"first refresh failed: {first_refresh.status_code}")
    second = first_refresh.json()
    successor_before_restart = introspect(client, second["access_token"], introspection_token)
    successor_before_restart_json = successor_before_restart.json()

    # Verify a second independent client/database connection sees the live successor
    # before deliberately replaying its predecessor.
    connection.close()
    reopened = new_client()
    persistence = introspect(reopened, second["access_token"], introspection_token)
    persistence_active = persistence.status_code == 200 and persistence.json().get("active") is True
    retry = token_request(reopened, app, first["refresh_token"])

    # Two independent synchronous clients/connections contend for a fresh grant.
    race_user, race_app = create_application()
    client.force_login(race_user)
    race_first = authorize_and_exchange(client, race_app)
    race_source = race_first["refresh_token"]
    barrier = threading.Barrier(2)

    def concurrent_refresh():
        close_old_connections()
        candidate = new_client()
        try:
            barrier.wait(timeout=10)
        except threading.BrokenBarrierError:
            return {
                "status": 598,
                "error": "barrier_timeout",
                "_access_token": None,
                "_refresh_token": None,
            }
        try:
            response = token_request(candidate, race_app, race_source)
            payload = (
                response.json() if response.status_code == 200 else {}
            )
            return {
                "status": response.status_code,
                "error": payload.get("error"),
                "_access_token": payload.get("access_token"),
                "_refresh_token": payload.get("refresh_token"),
            }
        except Exception as exc:
            return {
                "status": 599,
                "error": exc.__class__.__name__,
                "_access_token": None,
                "_refresh_token": None,
            }
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as executor:
        race_results = list(executor.map(lambda _: concurrent_refresh(), range(2)))
    race_successes = [
        result for result in race_results if result["status"] == 200
    ]
    race_safe_results = [
        {
            "status": result["status"],
            "error": result["error"],
            "has_access_token": bool(result["_access_token"]),
            "has_refresh_token": bool(result["_refresh_token"]),
        }
        for result in race_results
    ]
    concurrent_same_successor = (
        len(race_successes) == 2
        and len({result["_access_token"] for result in race_successes}) == 1
        and len({result["_refresh_token"] for result in race_successes}) == 1
    )
    concurrent_successor_active = False
    if race_successes:
        observed = race_successes[0]["_access_token"]
        observed_response = introspect(client, observed, introspection_token)
        concurrent_successor_active = (
            observed_response.status_code == 200
            and observed_response.json().get("active") is True
        )

    # Explicit RFC 7009 revocation of a fresh live token, followed by introspection.
    revoke_user, revoke_app = create_application()
    client.force_login(revoke_user)
    revocation_tokens = authorize_and_exchange(client, revoke_app)
    revoke_response = client.post(
        "/o/revoke_token/",
        data={
            "token": revocation_tokens["refresh_token"],
            "token_type_hint": "refresh_token",
            "client_id": revoke_app.client_id,
        },
    )
    revoked_introspection = introspect(client, revocation_tokens["access_token"], introspection_token)

    access_model = get_access_token_model()
    refresh_model = get_refresh_token_model()
    access_row = access_model.objects.order_by("id").first()
    refresh_row = refresh_model.objects.order_by("id").first()
    migration = {"skipped": True}
    if include_migration:
        from django.db import connections

        connections.close_all()
        try:
            migration = legacy_hash_import_probe(client, introspection_token)
        except Exception as exc:
            migration = {"error_type": exc.__class__.__name__}
    persistence_fields = {
        "access_token_has_raw_value": bool(getattr(access_row, "token", None)),
        "refresh_token_has_raw_value": bool(getattr(refresh_row, "token", None)),
        "access_checksum_present": bool(getattr(access_row, "token_checksum", None)),
        "refresh_checksum_present": bool(getattr(refresh_row, "token_checksum", None)),
    }

    return {
        "django": django.get_version(),
        "dot": __import__("importlib.metadata").metadata.version("django-oauth-toolkit"),
        "oauthlib": __import__("importlib.metadata").metadata.version("oauthlib"),
        "grace_seconds": grace_seconds,
        "storage_mode": "hash_only" if hash_only else "plaintext",
        "authorization_code_s256_status": 302,
        "token_exchange_public_keys_only": public_keys == [
            "access_token", "expires_in", "refresh_token", "scope", "token_type"
        ],
        "initial_introspection_active": introspection_json.get("active") is True,
        "initial_introspection_exact_audience": introspection_json.get("aud") == [RESOURCE],
        "basic_introspection_status": basic_introspected.status_code,
        "basic_introspection_content_type": basic_introspected.headers.get("Content-Type", ""),
        "basic_introspection_active": basic_introspection_json.get("active") is True,
        "basic_introspection_exact_audience": basic_introspection_json.get("aud") == [RESOURCE],
        "first_refresh_status": first_refresh.status_code,
        "retry_status": retry.status_code,
        "retry_error": retry.json().get("error"),
        "retry_returns_successor": retry.status_code == 200 and retry.json().get("refresh_token") == second["refresh_token"],
        "successor_before_restart_status": successor_before_restart.status_code,
        "successor_before_restart_active": successor_before_restart_json.get("active"),
        "persistence_after_connection_restart": persistence_active,
        "persistence_status": persistence.status_code,
        "persistence_response_keys": sorted(persistence.json()) if persistence.status_code == 200 else sorted(persistence.json()),
        "concurrent_results": race_safe_results,
        "concurrent_same_successor": concurrent_same_successor,
        "concurrent_successor_introspection_active": concurrent_successor_active,
        "revocation_http_status": revoke_response.status_code,
        "revoked_introspection_active": revoked_introspection.json().get("active") is True,
        "row_metadata": row_metadata(),
        "persistence_fields": persistence_fields,
        "legacy_hash_import": migration,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path)
    parser.add_argument("--grace", type=int, default=0)
    parser.add_argument(
        "--plaintext",
        action="store_true",
        help="Use DOT's plaintext token columns (required for documented grace mode).",
    )
    parser.add_argument("--child", action="store_true")
    parser.add_argument("--migration-only", action="store_true")
    parser.add_argument("--skip-migration", action="store_true")
    args = parser.parse_args()
    if args.migration_only:
        owned_root = None
        with tempfile.TemporaryDirectory(
            prefix="cognita-oauth8-migration-", dir=tempfile.gettempdir()
        ) as root:
            owned_root = Path(root).resolve()
            try:
                db_path = owned_root / "oauth.sqlite3"
                configure(db_path, 0, True)
                client = new_client()
                user, app = create_application()
                client.force_login(user)
                introspection_token = create_introspection_token(user, app)
                print(
                    json.dumps(
                        legacy_hash_import_probe(client, introspection_token),
                        sort_keys=True,
                    )
                )
            finally:
                from django.db import connections

                connections.close_all()
        if owned_root.exists():
            raise AssertionError(f"owned migration root remains: {owned_root}")
        return
    if args.child:
        if not args.db:
            raise SystemExit("--child requires --db")
        result = run_proof(args.db, args.grace, not args.plaintext, not args.skip_migration)
        print(json.dumps(result, sort_keys=True))
        return
    owned_root = None
    try:
        with tempfile.TemporaryDirectory(
            prefix="cognita-oauth8-proof-", dir=tempfile.gettempdir()
        ) as root:
            owned_root = Path(root).resolve()
            db_path = owned_root / "oauth.sqlite3"
            try:
                results = run_proof(
                    db_path, args.grace, not args.plaintext, not args.skip_migration
                )
            finally:
                from django.db import connections

                connections.close_all()
            print(json.dumps(results, sort_keys=True))
            if not db_path.is_relative_to(owned_root):
                raise AssertionError("proof database escaped owned temp root")
    finally:
        if owned_root is not None and owned_root.exists():
            raise AssertionError(f"owned proof root remains: {owned_root}")


if __name__ == "__main__":
    main()
