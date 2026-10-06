"""Deterministic coverage for Cognita's asymmetric CIMD extension."""

from __future__ import annotations

import base64
import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from django.conf import settings

if not settings.configured:
    settings.configure(
        SECRET_KEY="cimd-auth-test",
        USE_TZ=True,
        INSTALLED_APPS=["django.contrib.auth", "django.contrib.contenttypes", "oauth2_provider"],
        DATABASES={"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}},
        OAUTH2_PROVIDER={"CIMD_ALLOWED_HOSTS": ["chatgpt.com"]},
    )
    import django

    django.setup()

from jwcrypto import jwk, jws

from cognita.oauth_service import cimd_auth
from cognita.oauth_service.policy import CognitaOAuth2Validator

CLIENT_ID = "https://chatgpt.com/oauth/client.json"
TOKEN_ENDPOINT = "https://beta.example.test/oauth/token"


def _key(kid: str):
    key = jwk.JWK.generate(kty="RSA", size=2048)
    public = json.loads(key.export_public())
    public["kid"] = kid
    return key, public


def _assertion(
    key,
    *,
    kid="one",
    audience=TOKEN_ENDPOINT,
    issued=None,
    expires=None,
    jti="jti-1",
    issuer=CLIENT_ID,
    subject=CLIENT_ID,
):
    now = time.time()
    issued = now if issued is None else issued
    expires = now + 120 if expires is None else expires
    payload = json.dumps(
        {
            "iss": issuer,
            "sub": subject,
            "aud": audience,
            "iat": issued,
            "exp": expires,
            "jti": jti,
        }
    ).encode()
    value = jws.JWS(payload)
    value.add_signature(key, protected=json.dumps({"alg": "RS256", "kid": kid}))
    return value.serialize(compact=True)


def _compact(header: dict, claims: dict) -> str:
    def encode(value: dict) -> str:
        return base64.urlsafe_b64encode(
            json.dumps(value, separators=(",", ":")).encode()
        ).decode().rstrip("=")

    return f"{encode(header)}.{encode(claims)}.c2lnbmF0dXJl"


def _logged_category(caplog) -> str:
    records = [
        record.message
        for record in caplog.records
        if record.name == "cognita.oauth_service.cimd_auth"
    ]
    assert len(records) == 1, records
    marker = "category="
    return records[0].split(marker, 1)[1].split(" ", 1)[0]


@pytest.fixture(autouse=True)
def _isolated_cache(monkeypatch):
    cimd_auth.clear_caches_for_tests()
    yield
    cimd_auth.clear_caches_for_tests()


def test_rs256_assertion_checks_claims_and_replay(monkeypatch):
    private, public = _key("one")

    class Fetcher:
        calls = 0

        def fetch(self, _uri):
            self.calls += 1
            return {"keys": [public]}, 300

    monkeypatch.setitem(cimd_auth.oauth2_settings.user_settings, "CIMD_JWKS_FETCHER", Fetcher)
    record = cimd_auth.ClientAuthMetadata(CLIENT_ID, "https://chatgpt.com/oauth/jwks.json", frozenset({"RS256"}), 0)
    assertion = _assertion(private)
    assert cimd_auth.validate_client_assertion(record, assertion, TOKEN_ENDPOINT)
    assert not cimd_auth.validate_client_assertion(record, assertion, TOKEN_ENDPOINT)
    assert not cimd_auth.validate_client_assertion(
        record, _assertion(private, audience="https://other.example.test/oauth/token", jti="jti-2"), TOKEN_ENDPOINT
    )
    assert not cimd_auth.validate_client_assertion(
        record, _assertion(private, kid="unknown", jti="jti-3"), TOKEN_ENDPOINT
    )


def test_unknown_kid_gets_one_bounded_refresh(monkeypatch):
    private, public = _key("rotated")
    calls = []

    class Fetcher:
        def fetch(self, _uri):
            calls.append(True)
            return {"keys": [public]}, 300

    monkeypatch.setitem(cimd_auth.oauth2_settings.user_settings, "CIMD_JWKS_FETCHER", Fetcher)
    record = cimd_auth.ClientAuthMetadata(CLIENT_ID, "https://chatgpt.com/oauth/jwks.json", frozenset({"RS256"}), 0)
    assert cimd_auth.validate_client_assertion(
        record, _assertion(private, kid="rotated", jti="rotation"), TOKEN_ENDPOINT
    )
    assert len(calls) == 1


def test_jwks_transport_failure_fails_closed(monkeypatch):
    private, _public = _key("one")

    class Fetcher:
        def fetch(self, _uri):
            raise RuntimeError("transport detail must not escape")

    monkeypatch.setitem(cimd_auth.oauth2_settings.user_settings, "CIMD_JWKS_FETCHER", Fetcher)
    record = cimd_auth.ClientAuthMetadata(CLIENT_ID, "https://chatgpt.com/oauth/jwks.json", frozenset({"RS256"}), 0)
    assert not cimd_auth.validate_client_assertion(record, _assertion(private), TOKEN_ENDPOINT)


def test_private_metadata_requires_exact_allowed_jwks_host(monkeypatch):
    monkeypatch.setitem(cimd_auth.oauth2_settings.user_settings, "CIMD_ALLOWED_HOSTS", ["chatgpt.com"])
    metadata = {
        "token_endpoint_auth_method": "private_key_jwt",
        "jwks_uri": "https://sub.chatgpt.com/oauth/jwks.json",
        "token_endpoint_auth_signing_alg": "RS256",
    }
    with pytest.raises(ValueError):
        cimd_auth._private_metadata(CLIENT_ID, metadata)


def test_private_metadata_accepts_chatgpt_standard_signing_algorithm_field(monkeypatch):
    monkeypatch.setitem(cimd_auth.oauth2_settings.user_settings, "CIMD_ALLOWED_HOSTS", ["chatgpt.com"])
    metadata = {
        "token_endpoint_auth_method": "private_key_jwt",
        "jwks_uri": "https://chatgpt.com/oauth/jwks.json",
        "token_endpoint_auth_signing_alg": "RS256",
    }
    record = cimd_auth._private_metadata(CLIENT_ID, metadata)
    assert record is not None
    assert record.algorithms == frozenset({"RS256"})


def test_allowed_cimd_hosts_follow_runtime_setting_after_lazy_lookup(monkeypatch):
    monkeypatch.setitem(cimd_auth.oauth2_settings.user_settings, "CIMD_ALLOWED_HOSTS", ["chatgpt.com"])
    assert not cimd_auth._exact_allowed_https_uri("https://claude.ai/client.json")

    monkeypatch.setitem(cimd_auth.oauth2_settings.user_settings, "CIMD_ALLOWED_HOSTS", ["claude.ai"])
    assert cimd_auth._exact_allowed_https_uri("https://claude.ai/client.json")


def test_public_metadata_accepts_claude_auxiliary_jwt_bearer_grant():
    metadata = {
        "client_id": "https://claude.ai/oauth/mcp-oauth-client-metadata",
        "client_name": "Claude",
        "client_uri": "https://claude.ai",
        "redirect_uris": ["https://claude.ai/api/mcp/auth_callback"],
        "grant_types": [
            "authorization_code",
            "refresh_token",
            "urn:ietf:params:oauth:grant-type:jwt-bearer",
        ],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
    }

    kwargs = cimd_auth._public_application_kwargs(metadata)

    assert kwargs["name"] == "Claude"
    assert kwargs["redirect_uris"] == "https://claude.ai/api/mcp/auth_callback"
    assert kwargs["authorization_grant_type"] == "authorization-code"


@pytest.mark.parametrize(
    "grant_type",
    [
        "urn:ietf:params:oauth:grant-type:device_code",
        "client_credentials",
    ],
)
def test_public_metadata_rejects_unadvertised_auxiliary_grants(grant_type):
    metadata = {
        "redirect_uris": ["https://claude.ai/api/mcp/auth_callback"],
        "grant_types": ["authorization_code", grant_type],
        "token_endpoint_auth_method": "none",
    }

    with pytest.raises(ValueError, match="unsupported grant"):
        cimd_auth._public_application_kwargs(metadata)


def test_cimd_fetch_rejects_unknown_host_and_client_id_mismatch(monkeypatch):
    monkeypatch.setitem(
        cimd_auth.oauth2_settings.user_settings,
        "CIMD_ALLOWED_HOSTS",
        ["claude.ai"],
    )
    monkeypatch.setitem(
        cimd_auth.oauth2_settings.user_settings,
        "CIMD_REGISTRATION_PERMISSION_CLASSES",
        ("oauth2_provider.cimd.HostAllowlistCIMDPermission",),
    )

    class Fetcher:
        def fetch(self, client_id):
            return {"client_id": "https://claude.ai/another-document"}, 300

    monkeypatch.setitem(cimd_auth.oauth2_settings.user_settings, "CIMD_METADATA_FETCHER", Fetcher)
    with pytest.raises(ValueError, match="host is not permitted"):
        cimd_auth._fetch_metadata("https://evil.example/client.json")
    with pytest.raises(ValueError, match="client_id mismatch"):
        cimd_auth._fetch_metadata("https://claude.ai/oauth/mcp-oauth-client-metadata")


def test_public_cimd_redirect_uri_matching_remains_exact():
    from oauth2_provider.models import get_application_model

    Application = get_application_model()
    application = Application(
        client_id="https://claude.ai/oauth/mcp-oauth-client-metadata",
        client_type=Application.CLIENT_PUBLIC,
        authorization_grant_type=Application.GRANT_AUTHORIZATION_CODE,
        redirect_uris="https://claude.ai/api/mcp/auth_callback",
    )

    assert application.redirect_uri_allowed("https://claude.ai/api/mcp/auth_callback")
    assert not application.redirect_uri_allowed("https://claude.ai/api/mcp/other")


def test_replay_cache_fails_closed_instead_of_evicting_live_assertions(monkeypatch):
    monkeypatch.setattr(cimd_auth, "MAX_REPLAY_ENTRIES", 2)
    future = time.time() + 120
    assert cimd_auth._remember_replay(CLIENT_ID, "first", future) is None
    assert cimd_auth._remember_replay(CLIENT_ID, "second", future) is None
    assert cimd_auth._remember_replay(CLIENT_ID, "third", future) == "replay_capacity"
    assert cimd_auth._remember_replay(CLIENT_ID, "first", future) == "replay_detected"


def test_assertion_rejections_have_stable_secret_safe_categories(monkeypatch, caplog):
    private, public = _key("one")
    wrong_private, _ = _key("wrong")

    class Fetcher:
        def fetch(self, _uri):
            return {"keys": [public]}, 300

    monkeypatch.setitem(cimd_auth.oauth2_settings.user_settings, "CIMD_JWKS_FETCHER", Fetcher)
    record = cimd_auth.ClientAuthMetadata(
        CLIENT_ID,
        "https://chatgpt.com/oauth/jwks.json",
        frozenset({"RS256"}),
        0,
    )
    now = time.time()
    ordinary_claims = {
        "iss": CLIENT_ID,
        "sub": CLIENT_ID,
        "aud": TOKEN_ENDPOINT,
        "iat": now,
        "exp": now + 120,
        "jti": "SENSITIVE-JTI-MARKER",
    }
    cases = [
        ("malformed_jwt", "not-a-jwt"),
        ("unsupported_algorithm", _compact({"alg": "HS256", "kid": "one"}, ordinary_claims)),
        ("missing_kid", _compact({"alg": "RS256"}, ordinary_claims)),
        ("unknown_kid", _assertion(private, kid="unknown", jti="unknown-kid")),
        ("signature_invalid", _assertion(wrong_private, kid="one", jti="bad-signature")),
        ("issuer_mismatch", _assertion(private, issuer="https://other.example/client", jti="iss")),
        ("subject_mismatch", _assertion(private, subject="https://other.example/client", jti="sub")),
        ("audience_mismatch", _assertion(private, audience="https://other.example/token", jti="aud")),
        ("iat_invalid", _assertion(private, issued="not-a-number", jti="iat")),
        ("not_yet_valid", _assertion(private, issued=now + 400, expires=now + 500, jti="nbf")),
        ("expiration_invalid", _assertion(private, expires="not-a-number", jti="exp-invalid")),
        ("expired", _assertion(private, issued=now - 200, expires=now - 400, jti="expired")),
        ("lifetime_invalid", _assertion(private, issued=now, expires=now + 700, jti="lifetime")),
        ("jti_invalid", _assertion(private, jti="")),
    ]
    caplog.set_level(logging.INFO, logger="cognita.oauth_service.cimd_auth")
    for expected, assertion in cases:
        caplog.clear()
        assert not cimd_auth.validate_client_assertion(
            record,
            assertion,
            TOKEN_ENDPOINT,
            correlation_id="safe-correlation",
        )
        assert _logged_category(caplog) == expected
        assert "SENSITIVE-JTI-MARKER" not in caplog.text
        assert assertion not in caplog.text
        cimd_auth.clear_caches_for_tests()


def test_jwks_fetch_and_parse_failures_have_distinct_categories(monkeypatch, caplog):
    private, _public = _key("one")
    record = cimd_auth.ClientAuthMetadata(
        CLIENT_ID,
        "https://chatgpt.com/oauth/jwks.json",
        frozenset({"RS256"}),
        0,
    )
    assertion = _assertion(private, jti="SENSITIVE-JTI-MARKER")
    caplog.set_level(logging.INFO, logger="cognita.oauth_service.cimd_auth")

    class FailedFetcher:
        def fetch(self, _uri):
            raise RuntimeError("SENSITIVE-TRANSPORT-MARKER")

    monkeypatch.setitem(
        cimd_auth.oauth2_settings.user_settings, "CIMD_JWKS_FETCHER", FailedFetcher
    )
    assert not cimd_auth.validate_client_assertion(record, assertion, TOKEN_ENDPOINT)
    assert _logged_category(caplog) == "jwks_fetch_failed"
    assert "SENSITIVE" not in caplog.text

    class InvalidFetcher:
        def fetch(self, _uri):
            return {"keys": [{"kty": "oct", "kid": "SENSITIVE-KEY-MARKER"}]}, 300

    caplog.clear()
    monkeypatch.setitem(
        cimd_auth.oauth2_settings.user_settings, "CIMD_JWKS_FETCHER", InvalidFetcher
    )
    assert not cimd_auth.validate_client_assertion(record, assertion, TOKEN_ENDPOINT)
    assert _logged_category(caplog) == "jwks_invalid"
    assert "SENSITIVE" not in caplog.text


def test_replay_and_capacity_have_distinct_categories(monkeypatch, caplog):
    private, public = _key("one")

    class Fetcher:
        def fetch(self, _uri):
            return {"keys": [public]}, 300

    monkeypatch.setitem(cimd_auth.oauth2_settings.user_settings, "CIMD_JWKS_FETCHER", Fetcher)
    record = cimd_auth.ClientAuthMetadata(
        CLIENT_ID,
        "https://chatgpt.com/oauth/jwks.json",
        frozenset({"RS256"}),
        0,
    )
    caplog.set_level(logging.INFO, logger="cognita.oauth_service.cimd_auth")
    first = _assertion(private, jti="first")
    assert cimd_auth.validate_client_assertion(record, first, TOKEN_ENDPOINT)
    assert not cimd_auth.validate_client_assertion(record, first, TOKEN_ENDPOINT)
    assert _logged_category(caplog) == "replay_detected"

    caplog.clear()
    monkeypatch.setattr(cimd_auth, "MAX_REPLAY_ENTRIES", 1)
    second = _assertion(private, jti="second")
    assert not cimd_auth.validate_client_assertion(record, second, TOKEN_ENDPOINT)
    assert _logged_category(caplog) == "replay_capacity"


def test_private_client_id_is_derived_from_signed_assertion_issuer(monkeypatch):
    private, _public = _key("one")
    assertion = _assertion(private, jti="issuer-hint")
    application = SimpleNamespace(client_id=CLIENT_ID, client_type="confidential")

    class Query:
        @staticmethod
        def first():
            return None

    class Manager:
        @staticmethod
        def filter(**_kwargs):
            return Query()

    class Application:
        CLIENT_CONFIDENTIAL = "confidential"
        objects = Manager()

    import oauth2_provider.models

    monkeypatch.setattr(oauth2_provider.models, "get_application_model", lambda: Application)
    monkeypatch.setattr(
        cimd_auth,
        "resolve_private_application",
        lambda client_id: application if client_id == CLIENT_ID else None,
    )
    request = SimpleNamespace(
        client_id=None,
        client=None,
        client_assertion=assertion,
        decoded_body=[("client_assertion", assertion)],
        validator_log={},
    )
    assert CognitaOAuth2Validator._private_client(request) is application
    assert request.client_id == CLIENT_ID
    assert request.client is application


def test_private_client_id_uses_decoded_body_when_assertion_attribute_is_absent(monkeypatch):
    """Support adapters that retain form fields only in oauthlib decoded_body."""
    private, _public = _key("one")
    assertion = _assertion(private, jti="decoded-body-hint")
    application = SimpleNamespace(client_id=CLIENT_ID, client_type="confidential")

    class Query:
        @staticmethod
        def first():
            return None

    class Manager:
        @staticmethod
        def filter(**_kwargs):
            return Query()

    class Application:
        CLIENT_CONFIDENTIAL = "confidential"
        objects = Manager()

    import oauth2_provider.models

    monkeypatch.setattr(oauth2_provider.models, "get_application_model", lambda: Application)
    monkeypatch.setattr(
        cimd_auth,
        "resolve_private_application",
        lambda client_id: application if client_id == CLIENT_ID else None,
    )
    request = SimpleNamespace(
        client_id=None,
        client=None,
        decoded_body=[("client_assertion", assertion)],
        validator_log={},
    )
    assert CognitaOAuth2Validator._private_client(request) is application
    assert request.client_id == CLIENT_ID
    assert request.client is application


def test_policy_auth_method_and_assertion_type_logs_are_secret_safe(monkeypatch, caplog):
    application = SimpleNamespace(client_id=CLIENT_ID)
    validator = object.__new__(CognitaOAuth2Validator)
    caplog.set_level(logging.INFO, logger="cognita.oauth_service.cimd_auth")

    monkeypatch.setattr(
        CognitaOAuth2Validator,
        "_private_client",
        classmethod(lambda _cls, _request: application),
    )
    missing = SimpleNamespace(
        decoded_body=[("client_assertion", "SENSITIVE-ASSERTION-MARKER")],
        client_assertion="SENSITIVE-ASSERTION-MARKER",
        client_assertion_type=None,
        headers={},
        validator_log={},
    )
    assert not validator.authenticate_client(missing)
    assert _logged_category(caplog) == "assertion_type_missing"
    assert "SENSITIVE" not in caplog.text

    caplog.clear()
    invalid = SimpleNamespace(
        decoded_body=[
            ("client_assertion", "SENSITIVE-ASSERTION-MARKER"),
            ("client_assertion_type", "SENSITIVE-TYPE-MARKER"),
        ],
        client_assertion="SENSITIVE-ASSERTION-MARKER",
        client_assertion_type="SENSITIVE-TYPE-MARKER",
        headers={},
        validator_log={},
    )
    assert not validator.authenticate_client(invalid)
    assert _logged_category(caplog) == "assertion_type_invalid"
    assert "SENSITIVE" not in caplog.text

    caplog.clear()
    monkeypatch.setattr(
        CognitaOAuth2Validator,
        "_private_client",
        classmethod(lambda _cls, _request: None),
    )
    mismatch = SimpleNamespace(
        client_id="public-client",
        decoded_body=[("client_assertion", "SENSITIVE-ASSERTION-MARKER")],
        validator_log={},
    )
    assert not validator.authenticate_client(mismatch)
    assert _logged_category(caplog) == "auth_method_mismatch"
    assert "SENSITIVE" not in caplog.text


def test_metadata_resolution_log_excludes_client_query_and_exception(monkeypatch, caplog):
    class Query:
        @staticmethod
        def first():
            return None

    class Manager:
        @staticmethod
        def filter(**_kwargs):
            return Query()

    class Application:
        objects = Manager()

    import oauth2_provider.models

    client_id = CLIENT_ID + "?SENSITIVE-QUERY-MARKER"
    monkeypatch.setattr(oauth2_provider.models, "get_application_model", lambda: Application)

    def fail_resolution(_client_id):
        raise RuntimeError("SENSITIVE-EXCEPTION-MARKER")

    monkeypatch.setattr(cimd_auth, "resolve_private_application", fail_resolution)
    request = SimpleNamespace(
        client_id=client_id,
        client=None,
        decoded_body=[],
        validator_log={},
    )
    caplog.set_level(logging.INFO, logger="cognita.oauth_service.cimd_auth")
    assert CognitaOAuth2Validator._private_client(request) is None
    assert _logged_category(caplog) == "metadata_resolution"
    assert "client_host=chatgpt.com" in caplog.text
    assert "SENSITIVE" not in caplog.text


def test_real_token_endpoint_accepts_chatgpt_assertion_without_duplicate_client_id() -> None:
    """Exercise DOT's authorization-code handler with RFC 7523 request semantics."""
    script = r'''
import base64
import hashlib
import json
import logging
import os
import time
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

import yaml
from argon2 import PasswordHasher
from jwcrypto import jwk, jws

from cognita.oauth_service.asgi import create_application
from cognita.oauth_service.bootstrap import bootstrap_service
from cognita.oauth_service.principal import OAuthPrincipalStore

logging.basicConfig(level=logging.INFO)

CLIENT_ID = "https://chatgpt.com/oauth/client.json"
CALLBACK = "https://chatgpt.com/connector_platform_oauth_redirect"
BASE_URL = "https://beta.example.test"
RESOURCE = BASE_URL + f"/mcp/connectors/cognita/mcp/v{PUBLIC_CONTRACT_VERSION}"
TOKEN_ENDPOINT = BASE_URL + "/oauth/token"
VERIFIER = "v" * 64
CHALLENGE = base64.urlsafe_b64encode(
    hashlib.sha256(VERIFIER.encode()).digest()
).decode().rstrip("=")

with TemporaryDirectory(prefix="cognita-oauth-cimd-token-") as owned:
    root = Path(owned)
    data = root / "data"
    data.mkdir()
    registry = root / "registry.yaml"
    registry.write_text(yaml.safe_dump({
        "version": 1,
        "projects": [{
            "name": "Self-Test",
            "documents_dir": str(root / "documents"),
            "data_dir": str(root / "project-data"),
            "enabled": True,
        }],
    }), encoding="utf-8")
    connectors = root / "connectors.yaml"
    connectors.write_text(yaml.safe_dump({
        "version": 1,
        "revision": 1,
        "connectors": [{
            "id": "2c520a44-2037-4bb5-a565-d88ec2bb02d1",
            "slug": "cognita",
            "name": "Cognita Test",
            "enabled": True,
            "workspace_enabled": False,
            "project_mode": "all",
            "default_access": "write",
            "project_access": {},
        }],
    }), encoding="utf-8")
    config = root / "cognita.yaml"
    config.write_text(yaml.safe_dump({
        "data_root": str(data),
        "registry_path": str(registry),
        "connectors_path": str(connectors),
        "public_base_url": BASE_URL,
        "oauth_service_store_path": str(data / "oauth.sqlite3"),
        "oauth_cimd_allowed_hosts": ["chatgpt.com"],
        "admin_username": "admin",
        "admin_password_hash": PasswordHasher().hash("pw"),
    }), encoding="utf-8")

    context = bootstrap_service(config)
    create_application(context)
    from django.contrib.auth import get_user_model
    from django.test import Client
    from django.utils import timezone
    from oauth2_provider.models import get_grant_model
    from oauth2_provider.settings import oauth2_settings
    from cognita.oauth_service.cimd_auth import resolve_private_application

    private = jwk.JWK.generate(kty="RSA", size=2048)
    public = json.loads(private.export_public())
    public.update({"kid": "chatgpt-test", "use": "sig", "alg": "RS256"})

    class MetadataFetcher:
        def fetch(self, client_id):
            assert client_id == CLIENT_ID
            return ({
                "client_id": CLIENT_ID,
                "client_name": "ChatGPT",
                "redirect_uris": [CALLBACK],
                "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"],
                "token_endpoint_auth_method": "private_key_jwt",
                "token_endpoint_auth_signing_alg": "RS256",
                "jwks_uri": "https://chatgpt.com/oauth/jwks.json",
            }, 300)

    class JWKSFetcher:
        def fetch(self, uri):
            assert uri == "https://chatgpt.com/oauth/jwks.json"
            return {"keys": [public]}, 300

    oauth2_settings.user_settings["CIMD_METADATA_FETCHER"] = MetadataFetcher
    oauth2_settings.user_settings["CIMD_JWKS_FETCHER"] = JWKSFetcher
    application = resolve_private_application(CLIENT_ID)
    assert application is not None
    assert application.client_type == application.CLIENT_CONFIDENTIAL
    user = get_user_model().objects.get(username=context.subject_username)
    grant = get_grant_model().objects.create(
        user=user,
        code="authorization-code",
        application=application,
        expires=timezone.now() + timedelta(minutes=5),
        redirect_uri=CALLBACK,
        scope="cognita:access",
        code_challenge=CHALLENGE,
        code_challenge_method="S256",
        claims={},
        resource=[RESOURCE],
    )
    OAuthPrincipalStore(context.store_path).create_principal(
        str(user.pk), str(application.pk), RESOURCE, code_key=str(grant.pk)
    )

    def signed_assertion(jti):
        now = time.time()
        payload = json.dumps({
            "iss": CLIENT_ID,
            "sub": CLIENT_ID,
            "aud": TOKEN_ENDPOINT,
            "iat": now,
            "exp": now + 120,
            "jti": jti,
        }).encode()
        assertion = jws.JWS(payload)
        assertion.add_signature(
            private,
            protected=json.dumps({"alg": "RS256", "kid": "chatgpt-test"}),
        )
        return assertion.serialize(compact=True)

    client = Client()
    response = client.post("/oauth/token", data={
        "grant_type": "authorization_code",
        "code": "authorization-code",
        "redirect_uri": CALLBACK,
        "resource": RESOURCE,
        "code_verifier": VERIFIER,
        "client_assertion_type": "urn:ietf:params:oauth:client-assertion-type:jwt-bearer",
        "client_assertion": signed_assertion("endpoint-integration"),
        # ChatGPT/RFC 7523 semantics: no duplicate client_id parameter.
    })
    status = response.status_code
    body = response.content
    result = response.json()
    refresh_response = client.post("/oauth/token", data={
        "grant_type": "refresh_token",
        "refresh_token": result.get("refresh_token", ""),
        "resource": RESOURCE,
        "client_assertion_type": "urn:ietf:params:oauth:client-assertion-type:jwt-bearer",
        "client_assertion": signed_assertion("endpoint-refresh"),
    })
    refresh_status = refresh_response.status_code
    refresh_body = refresh_response.content
    refresh_result = refresh_response.json()
    from django import db
    db.connections.close_all()
    import gc
    gc.collect()
    assert not Path(owned).joinpath("unexpected").exists()

assert not Path(owned).exists()
assert status == 200, body
assert isinstance(result.get("access_token"), str)
assert isinstance(result.get("refresh_token"), str)
assert result.get("token_type") == "Bearer"
assert refresh_status == 200, refresh_body
assert isinstance(refresh_result.get("access_token"), str)
assert refresh_result.get("refresh_token") == result.get("refresh_token")
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
