"""Offline, one-shot migration from Cognita 7.x OAuth rows into DOT.

This module is deliberately outside the request path.  It reads the legacy SQLite
file through SQLite's read-only URI mode, creates a fresh DOT database beside the
requested target, and installs that file only when the target does not exist.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import secrets
import sqlite3
import sys
import uuid
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse

from cognita.config import DEFAULT_CONFIG_PATH, CognitaConfig, load_config
from cognita.registry import Registry
from cognita.oauth_service.storage import ensure_private_sqlite

log = logging.getLogger("cognita.oauth_service.legacy_import")
SCOPE = "cognita:access"
SCHEMA_VERSION = 1
SUBJECT_USERNAME = "cognita-oauth-subject"
_FAMILY_NAMESPACE = uuid.UUID("c6e6bfb9-9b3d-4fb4-9d76-6b8df3a4b67a")
_REQUIRED_COLUMNS = {
    "clients": {"client_id", "client_name", "redirect_uris", "source", "created_at"},
    "grants": {
        "grant_id", "client_id", "client_name", "project", "resource", "subject",
        "credential_fingerprint", "created_at", "last_used_at", "revoked_at",
    },
    "access_tokens": {"token_hash", "grant_id", "resource", "created_at", "expires_at"},
    "refresh_tokens": {
        "token_hash", "grant_id", "family_id", "created_at", "used_at", "revoked_at",
    },
}


class LegacyImportError(RuntimeError):
    """The source or staging database cannot satisfy the migration contract."""


class TargetExistsError(LegacyImportError):
    """The requested target already exists and cannot be overwritten."""


@dataclass
class ImportReport:
    source_schema_version: int = SCHEMA_VERSION
    clients_seen: int = 0
    clients_imported: int = 0
    clients_skipped: int = 0
    grants_seen: int = 0
    grants_eligible: int = 0
    grants_skipped: int = 0
    access_tokens_seen: int = 0
    access_tokens_imported: int = 0
    access_tokens_skipped: int = 0
    refresh_tokens_seen: int = 0
    refresh_tokens_imported: int = 0
    refresh_tokens_skipped: int = 0
    installed: bool = False
    staging_cleaned: bool = False
    skip_reasons: dict[str, int] = field(default_factory=dict)

    def skip(self, reason: str) -> None:
        self.skip_reasons[reason] = self.skip_reasons.get(reason, 0) + 1

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _epoch(value: Any, field_name: str) -> datetime:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field_name} is not a timestamp")
    return datetime.fromtimestamp(float(value), timezone.utc)


def _hash(value: Any) -> str | None:
    if not isinstance(value, str) or len(value) != 64:
        return None
    try:
        int(value, 16)
    except ValueError:
        return None
    return value.lower()


def _valid_host(host: str, allowed: Iterable[str]) -> bool:
    host = host.lower().rstrip(".")
    for configured in allowed:
        item = str(configured).lower().strip().rstrip(".")
        if host == item or (item not in {"localhost", "127.0.0.1", "::1"} and host.endswith("." + item)):
            return True
    return False


def _valid_redirect(uri: Any, allowed: Iterable[str]) -> bool:
    if not isinstance(uri, str) or not uri or "\x00" in uri:
        return False
    try:
        parsed = urlparse(uri)
        if parsed.fragment or parsed.username or parsed.password or not parsed.hostname:
            return False
        loopback = parsed.hostname in {"127.0.0.1", "localhost", "::1"}
        if parsed.scheme != "https" and not (parsed.scheme == "http" and loopback):
            return False
        return _valid_host(parsed.hostname, allowed)
    except ValueError:
        return False


def _valid_cimd_id(value: str, allowed: Iterable[str]) -> bool:
    try:
        parsed = urlparse(value)
    except ValueError:
        return False
    return parsed.scheme == "https" and bool(parsed.hostname) and _valid_host(parsed.hostname, allowed)


def _redirects(value: Any) -> tuple[str, ...] | None:
    if not isinstance(value, str):
        return None
    try:
        decoded = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(decoded, list) or not 1 <= len(decoded) <= 10:
        return None
    if not all(isinstance(item, str) for item in decoded):
        return None
    return tuple(decoded)


def _resource(config: CognitaConfig, project: str) -> str:
    return f"{config.public_base_url.rstrip('/')}/mcp/{project}"


def _open_legacy(path: Path) -> sqlite3.Connection:
    resolved = path.resolve()
    if not resolved.is_file():
        raise LegacyImportError("legacy source database does not exist")
    uri = f"file:{resolved.as_posix()}?mode=ro"
    try:
        connection = sqlite3.connect(uri, uri=True)
    except sqlite3.Error as exc:
        raise LegacyImportError("legacy source database could not be opened read-only") from exc
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    return connection


def _check_schema(db: sqlite3.Connection) -> None:
    tables = {
        row[0] for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    missing_tables = sorted(set(_REQUIRED_COLUMNS) - tables)
    if missing_tables:
        raise LegacyImportError("legacy source schema is missing required tables")
    version = int(db.execute("PRAGMA user_version").fetchone()[0])
    if version not in {0, SCHEMA_VERSION}:
        raise LegacyImportError("legacy source schema version is unsupported")
    for table, required in _REQUIRED_COLUMNS.items():
        columns = {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
        if not required <= columns:
            raise LegacyImportError(f"legacy source schema is missing columns for {table}")


def _configure_dot(path: Path) -> None:
    """Configure a one-use Django process for native DOT ORM writes."""
    ensure_private_sqlite(path, create=True)
    from django.conf import settings

    if settings.configured:
        raise LegacyImportError("Django is already configured; importer must run once per process")
    settings.configure(
        DEBUG=False,
        SECRET_KEY=secrets.token_hex(32),
        ROOT_URLCONF="cognita.oauth_service.urls",
        INSTALLED_APPS=[
            "django.contrib.auth",
            "django.contrib.contenttypes",
            "django.contrib.sessions",
            "oauth2_provider",
        ],
        DATABASES={
            "default": {
                "ENGINE": "django.db.backends.sqlite3",
                "NAME": str(path),
                "OPTIONS": {"transaction_mode": "IMMEDIATE", "timeout": 20},
            }
        },
        USE_TZ=True,
        TIME_ZONE="UTC",
        OAUTH2_PROVIDER={
            "SCOPES": {SCOPE: "Cognita project access", "introspection": "Internal token introspection"},
            "DEFAULT_SCOPES": [SCOPE],
            "ROTATE_REFRESH_TOKEN": False,
            "REFRESH_TOKEN_REUSE_PROTECTION": False,
            "REFRESH_TOKEN_GRACE_PERIOD_SECONDS": 0,
            "COMPLIANT_BCP_RFC9700_TOKEN_STORAGE": True,
        },
    )
    import django
    from django.core.management import call_command

    django.setup()
    call_command("migrate", verbosity=0, interactive=False)


def _verify_staging_database() -> None:
    """Verify migrations are complete and SQLite can read the staged file."""
    from django.db import connection, connections
    from django.db.migrations.executor import MigrationExecutor

    executor = MigrationExecutor(connection)
    if executor.migration_plan(executor.loader.graph.leaf_nodes()):
        raise LegacyImportError("staging database still has unapplied migrations")
    staged_path = Path(connection.settings_dict["NAME"])
    connections.close_all()
    check = sqlite3.connect(staged_path)
    try:
        result = check.execute("PRAGMA integrity_check").fetchone()
    finally:
        check.close()
    if not result or result[0] != "ok":
        raise LegacyImportError("staging database failed SQLite integrity check")

def _staging_path(target: Path) -> Path:
    return target.with_name(f".{target.name}.cognita-staging-{uuid.uuid4().hex}.sqlite3")


def _cleanup_staging(path: Path) -> bool:
    """Delete only the exact staging file and its SQLite sidecars."""
    cleaned = True
    for candidate in (path, Path(str(path) + "-wal"), Path(str(path) + "-shm"), Path(str(path) + "-journal")):
        try:
            candidate.unlink()
        except FileNotFoundError:
            continue
        except OSError:
            cleaned = False
            log.error("OAuth migration staging cleanup failed type=%s", type(candidate).__name__)
    return cleaned


def _install_staging(staging: Path, target: Path) -> None:
    """Atomically create the target without replacing a concurrent target."""
    if target.exists():
        raise TargetExistsError("DOT target already exists; refusing overwrite")
    try:
        # The target is a hard link to this already-private inode, so there is no
        # post-install permission transition that can fail after target creation.
        ensure_private_sqlite(staging)
        os.link(staging, target)
    except FileExistsError as exc:
        raise TargetExistsError("DOT target appeared during migration; refusing overwrite") from exc
    except OSError as exc:
        raise LegacyImportError("staging database could not be atomically installed") from exc
    staging.unlink()


def _application_for(row: sqlite3.Row, subject: Any, config: CognitaConfig) -> tuple[Any | None, str | None]:
    from django.utils import timezone
    from oauth2_provider.models import get_application_model

    client_id = row["client_id"]
    name = row["client_name"]
    source = row["source"]
    redirects = _redirects(row["redirect_uris"])
    if not isinstance(client_id, str) or not client_id or not isinstance(name, str) or not name or len(name) > 255:
        return None, "invalid_client_metadata"
    if source not in {"cimd", "dcr", "manual"}:
        return None, "unsupported_registration_source"
    if source == "cimd" and not _valid_cimd_id(client_id, config.oauth_cimd_allowed_hosts):
        return None, "invalid_cimd_client_id"
    if redirects is None or any(not _valid_redirect(item, config.oauth_allowed_client_hosts) for item in redirects):
        return None, "invalid_redirect_uri"
    model = get_application_model()
    defaults = {
        "user": subject,
        "name": name[:255],
        "redirect_uris": "\n".join(redirects),
        "client_type": model.CLIENT_PUBLIC,
        "authorization_grant_type": model.GRANT_AUTHORIZATION_CODE,
        "skip_authorization": False,
        "registration_source": source,
    }
    if source == "cimd":
        defaults["cimd_expires_at"] = timezone.now() - timedelta(seconds=1)
    app = model.objects.create(client_id=client_id, **defaults)
    return app, None


def _family(value: Any) -> uuid.UUID | None:
    if not isinstance(value, str) or not value:
        return None
    return uuid.uuid5(_FAMILY_NAMESPACE, value)


def _rows(db: sqlite3.Connection, table: str) -> list[sqlite3.Row]:
    return list(db.execute(f"SELECT * FROM {table} ORDER BY rowid"))


def _import_rows(
    db: sqlite3.Connection,
    config: CognitaConfig,
    registry: Registry,
    subject: Any,
    report: ImportReport,
) -> None:
    from django.db import transaction
    from django.utils import timezone
    from oauth2_provider.models import get_access_token_model, get_refresh_token_model

    clients = _rows(db, "clients")
    grants = _rows(db, "grants")
    accesses = _rows(db, "access_tokens")
    refreshes = _rows(db, "refresh_tokens")
    report.clients_seen = len(clients)
    report.grants_seen = len(grants)
    report.access_tokens_seen = len(accesses)
    report.refresh_tokens_seen = len(refreshes)
    apps: dict[str, Any] = {}
    for row in clients:
        app, reason = _application_for(row, subject, config)
        if app is None:
            report.clients_skipped += 1
            report.skip(reason or "invalid_client_metadata")
        else:
            apps[row["client_id"]] = app
            report.clients_imported += 1

    access_by_grant: dict[str, list[sqlite3.Row]] = defaultdict(list)
    refresh_by_grant: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for row in accesses:
        access_by_grant[row["grant_id"]].append(row)
    for row in refreshes:
        refresh_by_grant[row["grant_id"]].append(row)
    access_model = get_access_token_model()
    refresh_model = get_refresh_token_model()
    now = timezone.now()
    imported_access: dict[tuple[str, str], Any] = {}
    with transaction.atomic():
        for grant in grants:
            client_id = grant["client_id"]
            project = grant["project"]
            resource = grant["resource"]
            configured = registry.get(project) if isinstance(project, str) else None
            eligible = (
                client_id in apps
                and configured is not None
                and configured.enabled
                and isinstance(resource, str)
                and resource == _resource(config, project)
                and isinstance(grant["grant_id"], str)
            )
            if not eligible:
                report.grants_skipped += 1
                report.skip("ineligible_grant")
                report.access_tokens_skipped += len(access_by_grant[grant["grant_id"]])
                report.refresh_tokens_skipped += len(refresh_by_grant[grant["grant_id"]])
                continue
            report.grants_eligible += 1
            grant_id = grant["grant_id"]
            grant_access = access_by_grant[grant_id]
            grant_refresh = refresh_by_grant[grant_id]
            # A revoked grant contributes no live access row; its refresh checksums
            # remain as native revoked rows even when no access relationship exists.
            grant_revoked_at = grant["revoked_at"]
            if grant_revoked_at is not None:
                for row in grant_access:
                    report.access_tokens_skipped += 1
                    report.skip("revoked_grant_access")
                for row in grant_refresh:
                    checksum = _hash(row["token_hash"])
                    family = _family(row["family_id"])
                    if checksum is None or family is None:
                        report.refresh_tokens_skipped += 1
                        report.skip("invalid_revoked_refresh")
                        continue
                    created = _epoch(row["created_at"], "refresh.created_at")
                    revoked = _epoch(row["revoked_at"] or grant_revoked_at, "refresh.revoked_at")
                    refresh_model.objects.create(
                        user=subject, application=apps[client_id], access_token=None,
                        token="", token_checksum=checksum, token_family=family,
                        resource=[resource], created=created, revoked=revoked,
                    )
                    report.refresh_tokens_imported += 1
                continue

            valid_access = []
            for row in grant_access:
                checksum = _hash(row["token_hash"])
                try:
                    created = _epoch(row["created_at"], "access.created_at")
                    expires = _epoch(row["expires_at"], "access.expires_at")
                except ValueError:
                    checksum = None
                    created = expires = None
                if checksum is None or created is None or expires is None:
                    report.access_tokens_skipped += 1
                    report.skip("invalid_access_token")
                    continue
                valid_access.append((row, checksum, created, expires))
            valid_refresh = []
            for row in grant_refresh:
                checksum = _hash(row["token_hash"])
                family = _family(row["family_id"])
                try:
                    created = _epoch(row["created_at"], "refresh.created_at")
                except ValueError:
                    created = None
                if checksum is None or family is None or created is None:
                    report.refresh_tokens_skipped += 1
                    report.skip("invalid_refresh_token")
                    continue
                valid_refresh.append((row, checksum, family, created))
            by_created: dict[datetime, list[tuple[Any, str, datetime, datetime]]] = defaultdict(list)
            for item in valid_access:
                by_created[item[2]].append(item)
            refresh_pairs: dict[int, tuple[Any, str, datetime, datetime]] = {}
            for refresh_row, _checksum, _family_id, created in valid_refresh:
                candidates = by_created.get(created, [])
                if len(candidates) != 1 or sum(1 for item in valid_refresh if item[3] == created) != 1:
                    report.skip("ambiguous_refresh_access_pair")
                    continue
                refresh_pairs[id(refresh_row)] = candidates[0]
            paired_ids = {id(item[0]) for item in refresh_pairs.values()}
            for row, checksum, created, expires in valid_access:
                if expires < now and id(row) not in paired_ids:
                    report.access_tokens_skipped += 1
                    report.skip("expired_unpaired_access")
                    continue
                try:
                    token = access_model.objects.create(
                        user=subject, application=apps[client_id], token="",
                        token_checksum=checksum, expires=expires, scope=SCOPE,
                        resource=[resource], created=created,
                    )
                except Exception as exc:
                    report.access_tokens_skipped += 1
                    report.skip("access_insert_failed")
                    log.error("OAuth migration access insert failed cause=%s", type(exc).__name__)
                    continue
                imported_access[(grant_id, checksum)] = token
                report.access_tokens_imported += 1
            for row, checksum, family, created in valid_refresh:
                pair = refresh_pairs.get(id(row))
                if pair is None:
                    report.refresh_tokens_skipped += 1
                    report.skip("unpaired_refresh")
                    continue
                access_row, access_checksum, _access_created, _expires = pair
                access = imported_access.get((grant_id, access_checksum))
                if access is None:
                    report.refresh_tokens_skipped += 1
                    report.skip("paired_access_not_imported")
                    continue
                revoked = None
                if row["used_at"] is not None or row["revoked_at"] is not None:
                    try:
                        revoked = _epoch(row["revoked_at"] or row["used_at"], "refresh.revoked_at")
                    except ValueError:
                        report.refresh_tokens_skipped += 1
                        report.skip("invalid_refresh_revocation_time")
                        continue
                try:
                    refresh_model.objects.create(
                        user=subject, application=apps[client_id], access_token=access,
                        token="", token_checksum=checksum, token_family=family,
                        resource=[resource], created=created, revoked=revoked,
                    )
                except Exception as exc:
                    report.refresh_tokens_skipped += 1
                    report.skip("refresh_insert_failed")
                    log.error("OAuth migration refresh insert failed cause=%s", type(exc).__name__)
                    continue
                report.refresh_tokens_imported += 1


def import_legacy(
    source: Path,
    target: Path,
    config: CognitaConfig,
    *,
    subject_username: str = SUBJECT_USERNAME,
) -> ImportReport:
    """Migrate one legacy database, installing a new target only on success."""
    source = Path(source).resolve()
    target = Path(target).resolve()
    if source == target:
        raise LegacyImportError("legacy source and DOT target must be different files")
    if target.exists():
        raise TargetExistsError("DOT target already exists; refusing overwrite")
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = _staging_path(target)
    report = ImportReport()
    legacy = _open_legacy(source)
    try:
        _check_schema(legacy)
        _configure_dot(staging)
        from django.contrib.auth import get_user_model

        subject_model = get_user_model()
        subject, _ = subject_model.objects.get_or_create(username=subject_username)
        subject.set_unusable_password()
        subject.is_staff = False
        subject.is_superuser = False
        subject.save(update_fields=["password", "is_staff", "is_superuser"])
        _import_rows(legacy, config, Registry(config.registry_path), subject, report)
        _verify_staging_database()
        _install_staging(staging, target)
        report.installed = True
        return report
    finally:
        legacy.close()
        report.staging_cleaned = _cleanup_staging(staging)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Import Cognita 7.x OAuth state into a fresh DOT database")
    parser.add_argument("--source", type=Path, required=True, help="legacy oauth.sqlite3 source (read-only)")
    parser.add_argument("--target", type=Path, required=True, help="new oauth-service.sqlite3 target (must not exist)")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    parser.add_argument("--subject-username", default=SUBJECT_USERNAME)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        report = import_legacy(args.source, args.target, load_config(args.config), subject_username=args.subject_username)
    except (LegacyImportError, OSError, sqlite3.Error, ValueError) as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 2
    print(json.dumps(report.as_dict(), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
