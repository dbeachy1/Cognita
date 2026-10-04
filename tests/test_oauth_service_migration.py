from __future__ import annotations

import hashlib
import os
import json
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest
import yaml


SCHEMA = """
CREATE TABLE clients (
    client_id TEXT PRIMARY KEY,
    client_name TEXT NOT NULL,
    redirect_uris TEXT NOT NULL,
    source TEXT NOT NULL,
    created_at INTEGER NOT NULL
);
CREATE TABLE grants (
    grant_id TEXT PRIMARY KEY,
    client_id TEXT NOT NULL,
    client_name TEXT NOT NULL,
    project TEXT NOT NULL,
    resource TEXT NOT NULL,
    subject TEXT NOT NULL,
    credential_fingerprint TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    last_used_at INTEGER,
    revoked_at INTEGER
);
CREATE TABLE access_tokens (
    token_hash TEXT PRIMARY KEY,
    grant_id TEXT NOT NULL REFERENCES grants(grant_id),
    resource TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL
);
CREATE TABLE refresh_tokens (
    token_hash TEXT PRIMARY KEY,
    grant_id TEXT NOT NULL REFERENCES grants(grant_id),
    family_id TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    used_at INTEGER,
    revoked_at INTEGER
);
"""


def digest(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


@pytest.fixture
def migration_root():
    root = Path(tempfile.mkdtemp(prefix="cognita-oauth8-migration-"))
    try:
        yield root
    finally:
        shutil.rmtree(root)
        assert not root.exists()


def make_config(root: Path) -> Path:
    registry = root / "registry.yaml"
    registry.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "projects": [
                    {
                        "name": "KEI",
                        "documents_dir": str(root / "docs"),
                        "data_dir": str(root / "project-data"),
                        "enabled": True,
                    }
                ],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    config = root / "cognita.yaml"
    config.write_text(
        yaml.safe_dump(
            {
                "data_root": str(root / "data"),
                "registry_path": str(registry),
                "public_base_url": "http://127.0.0.1:8675",
                "oauth_allowed_client_hosts": ["claude.ai"],
                "oauth_cimd_allowed_hosts": ["chatgpt.com", "claude.ai"],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return config


def make_source(root: Path) -> tuple[Path, bytes]:
    source = root / "oauth.sqlite3"
    conn = sqlite3.connect(source)
    try:
        conn.executescript(SCHEMA)
        conn.executemany(
            "INSERT INTO clients VALUES(?,?,?,?,?)",
            [
                ("client-dcr", "DCR client", json.dumps(["https://claude.ai/callback"]), "dcr", 100),
                ("client-bad", "Bad client", json.dumps(["https://evil.example/callback"]), "dcr", 100),
            ],
        )
        resource = "http://127.0.0.1:8675/mcp/KEI"
        grants = [
            ("grant-live", "client-dcr", "DCR client", "KEI", resource, "subject", "fp", 100, None, None),
            ("grant-expired", "client-dcr", "DCR client", "KEI", resource, "subject", "fp", 200, None, None),
            ("grant-used", "client-dcr", "DCR client", "KEI", resource, "subject", "fp", 300, None, None),
            ("grant-revoked", "client-dcr", "DCR client", "KEI", resource, "subject", "fp", 400, None, 500),
            ("grant-wrong-resource", "client-dcr", "DCR client", "KEI", "http://127.0.0.1:8675/mcp/OTHER", "subject", "fp", 600, None, None),
        ]
        conn.executemany("INSERT INTO grants VALUES(?,?,?,?,?,?,?,?,?,?)", grants)
        now = int(time.time())
        access = [
            (digest("live-access"), "grant-live", resource, 100, now + 3600),
            (digest("expired-access"), "grant-expired", resource, 200, now - 60),
            (digest("used-access"), "grant-used", resource, 300, now + 3600),
            (digest("revoked-access"), "grant-revoked", resource, 400, now + 3600),
            (digest("wrong-resource-access"), "grant-wrong-resource", "http://127.0.0.1:8675/mcp/OTHER", 600, now + 3600),
        ]
        conn.executemany("INSERT INTO access_tokens VALUES(?,?,?,?,?)", access)
        refresh = [
            (digest("live-refresh"), "grant-live", "family-live", 100, None, None),
            (digest("expired-refresh"), "grant-expired", "family-expired", 200, None, None),
            (digest("used-refresh"), "grant-used", "family-used", 300, 350, None),
            (digest("revoked-refresh"), "grant-revoked", "family-revoked", 400, None, 450),
            (digest("wrong-resource-refresh"), "grant-wrong-resource", "family-wrong", 600, None, None),
        ]
        conn.executemany("INSERT INTO refresh_tokens VALUES(?,?,?,?,?,?)", refresh)
        conn.commit()
        before = source.read_bytes()
    finally:
        conn.close()
    return source, before


def run_import(source: Path, target: Path, config: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "cognita.oauth_service.legacy_import",
            "--source",
            str(source),
            "--target",
            str(target),
            "--config",
            str(config),
        ],
        cwd=Path(__file__).parents[1],
        text=True,
        capture_output=True,
        check=False,
    )


def inspect_target(target: Path) -> dict:
    script = f"""
import json
from django.conf import settings
settings.configure(INSTALLED_APPS=['django.contrib.auth','django.contrib.contenttypes','django.contrib.sessions','oauth2_provider'], DATABASES={{'default':{{'ENGINE':'django.db.backends.sqlite3','NAME':r'{target}'}}}}, SECRET_KEY='test', USE_TZ=True, OAUTH2_PROVIDER={{'ROTATE_REFRESH_TOKEN':False,'REFRESH_TOKEN_REUSE_PROTECTION':False,'REFRESH_TOKEN_GRACE_PERIOD_SECONDS':0,'COMPLIANT_BCP_RFC9700_TOKEN_STORAGE':True}})
import django; django.setup()
from django.utils import timezone
from oauth2_provider.models import get_application_model,get_access_token_model,get_refresh_token_model
A=get_access_token_model(); R=get_refresh_token_model(); apps=get_application_model()
expired=A.objects.filter(token_checksum__isnull=False, expires__lt=timezone.now()).count()
linked_expired=R.objects.filter(access_token__expires__lt=timezone.now(), revoked__isnull=True).count()
print(json.dumps({{'apps':apps.objects.count(),'access':A.objects.count(),'refresh':R.objects.count(),'blank_tokens':A.objects.filter(token='').count()+R.objects.filter(token='').count(),'checksums':A.objects.exclude(token_checksum='').count()+R.objects.exclude(token_checksum='').count(),'expired_access':expired,'linked_expired_refresh':linked_expired,'revoked_refresh':R.objects.filter(revoked__isnull=False).count(),'nullable_revoked_access':R.objects.filter(revoked__isnull=False,access_token__isnull=True).count(),'scopes_ok':not A.objects.exclude(scope='cognita:access').exists()}}))
"""
    result = subprocess.run([sys.executable, "-c", script], text=True, capture_output=True, check=True)
    return json.loads(result.stdout)


def refresh_native(target: Path, refresh_token: str, *, repeat: int = 1) -> dict:
    """Exercise DOT's refresh endpoint in an isolated process without printing tokens."""
    target_literal = target.as_posix()
    script = f"""
import hashlib
import json
from django.conf import settings
settings.configure(
    ALLOWED_HOSTS=['testserver', '127.0.0.1'],
    DEFAULT_AUTO_FIELD='django.db.models.BigAutoField',
    ROOT_URLCONF='cognita.oauth_service.urls',
    INSTALLED_APPS=['django.contrib.auth','django.contrib.contenttypes','django.contrib.sessions','oauth2_provider'],
    DATABASES={{'default':{{'ENGINE':'django.db.backends.sqlite3','NAME':r'{target_literal}','OPTIONS':{{'timeout':20}}}}}},
    SECRET_KEY='migration-refresh-test',
    USE_TZ=True,
    OAUTH2_PROVIDER={{
        'ROTATE_REFRESH_TOKEN': False,
        'REFRESH_TOKEN_REUSE_PROTECTION': False,
        'REFRESH_TOKEN_GRACE_PERIOD_SECONDS': 0,
        'COMPLIANT_BCP_RFC9700_TOKEN_STORAGE': True,
    }},
)
import django
django.setup()
from django.test import Client
from oauth2_provider.models import get_refresh_token_model
client = Client()
raw_refresh = {refresh_token!r}
results = []
for _ in range({repeat}):
    response = client.post('/oauth/token', data={{
        'grant_type': 'refresh_token',
        'client_id': 'client-dcr',
        'refresh_token': raw_refresh,
    }})
    payload = response.json()
    results.append({{'status': response.status_code, 'same_refresh': payload.get('refresh_token') == raw_refresh}})
row = get_refresh_token_model().objects.get(token_checksum=hashlib.sha256(raw_refresh.encode()).hexdigest())
print(json.dumps({{'results': results, 'plaintext_stored': bool(row.token), 'checksum_only': bool(row.token_checksum)}}))
"""
    result = subprocess.run([sys.executable, '-c', script], text=True, capture_output=True, check=True)
    return json.loads(result.stdout)

def test_imports_native_rows_and_reports_classification(migration_root: Path) -> None:
    config = make_config(migration_root)
    source, before = make_source(migration_root)
    target = migration_root / "oauth-service.sqlite3"
    result = run_import(source, target, config)
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["installed"] is True
    assert report["staging_cleaned"] is True
    assert report["clients_seen"] == 2
    assert report["clients_imported"] == 1
    assert report["clients_skipped"] == 1
    assert report["grants_seen"] == 5
    assert report["grants_eligible"] == 4
    assert report["grants_skipped"] == 1
    assert report["access_tokens_imported"] == 3
    assert report["refresh_tokens_imported"] == 4
    assert report["skip_reasons"]["invalid_redirect_uri"] == 1
    assert report["skip_reasons"]["ineligible_grant"] == 1
    assert source.read_bytes() == before
    if os.name == "posix":
        assert target.stat().st_mode & 0o777 == 0o600
    assert not list(migration_root.glob(".oauth-service.sqlite3.cognita-staging-*.sqlite3"))
    inspected = inspect_target(target)
    assert inspected == {
        "apps": 1,  # only the valid legacy public application is imported
        "access": 3,
        "refresh": 4,
        "blank_tokens": 7,
        "checksums": 7,
        "expired_access": 1,
        "linked_expired_refresh": 1,
        "revoked_refresh": 2,
        "nullable_revoked_access": 1,
        "scopes_ok": True,
    }


def test_existing_target_is_never_overwritten(migration_root: Path) -> None:
    config = make_config(migration_root)
    source, _ = make_source(migration_root)
    target = migration_root / "oauth-service.sqlite3"
    target.write_bytes(b"existing-target")
    result = run_import(source, target, config)
    assert result.returncode == 2
    assert json.loads(result.stderr)["error"] == "DOT target already exists; refusing overwrite"
    assert target.read_bytes() == b"existing-target"
    assert not list(migration_root.glob(".oauth-service.sqlite3.cognita-staging-*.sqlite3"))


def test_unsupported_source_schema_does_not_create_target(migration_root: Path) -> None:
    config = make_config(migration_root)
    source = migration_root / "bad.sqlite3"
    conn = sqlite3.connect(source)
    conn.executescript(SCHEMA)
    conn.execute("PRAGMA user_version=99")
    conn.commit()
    conn.close()
    target = migration_root / "oauth-service.sqlite3"
    result = run_import(source, target, config)
    assert result.returncode == 2
    assert "schema version" in json.loads(result.stderr)["error"]
    assert not target.exists()
    assert not list(migration_root.glob(".oauth-service.sqlite3.cognita-staging-*.sqlite3"))


def test_imported_refresh_is_reusable_across_sequential_calls_and_restart(migration_root: Path) -> None:
    config = make_config(migration_root)
    source, _ = make_source(migration_root)
    target = migration_root / 'oauth-service.sqlite3'
    result = run_import(source, target, config)
    assert result.returncode == 0, result.stderr

    sequential = refresh_native(target, 'live-refresh', repeat=2)
    assert sequential == {
        'results': [
            {'status': 200, 'same_refresh': True},
            {'status': 200, 'same_refresh': True},
        ],
        'plaintext_stored': False,
        'checksum_only': True,
    }
    restarted = refresh_native(target, 'live-refresh')
    assert restarted == {
        'results': [{'status': 200, 'same_refresh': True}],
        'plaintext_stored': False,
        'checksum_only': True,
    }
