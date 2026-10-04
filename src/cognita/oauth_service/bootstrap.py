"""Safe child database initialization and DOT application provisioning."""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from django.core.management import call_command

from cognita.admin_auth import has_argon2_credentials
from cognita.config import (
    CognitaConfig,
    ensure_oauth_service_key,
    load_config,
    oauth_service_store_path,
)
from cognita.registry import Registry

from .settings import ServiceContext, configure_django
from .storage import ensure_private_sqlite
from .principal import OAuthPrincipalStore

log = logging.getLogger("cognita.oauth_service.bootstrap")


def _backup_sqlite(path: Path, data_root: Path) -> None:
    if not path.is_file():
        return
    backup_dir = data_root / "oauth-backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target = backup_dir / f"oauth-service.{stamp}.sqlite3"
    temporary = target.with_suffix(".tmp")
    ensure_private_sqlite(temporary, create=True)
    try:
        source = sqlite3.connect(path)
        destination = sqlite3.connect(temporary)
        try:
            source.backup(destination)
            destination.commit()
            row = destination.execute("PRAGMA integrity_check").fetchone()
            if not row or row[0] != "ok":
                raise RuntimeError("OAuth service database backup failed integrity check")
        finally:
            destination.close()
            source.close()
        temporary.replace(target)
    finally:
        if temporary.exists():
            temporary.unlink()


def _provision_subject_and_internal_app(ctx: ServiceContext) -> None:
    from django.contrib.auth import get_user_model
    from django.contrib.auth.hashers import check_password
    from oauth2_provider.models import get_application_model

    user_model = get_user_model()
    subject, _ = user_model.objects.get_or_create(username=ctx.subject_username)
    changed = False
    if subject.has_usable_password():
        subject.set_unusable_password()
        changed = True
    if subject.is_staff or subject.is_superuser:
        subject.is_staff = False
        subject.is_superuser = False
        changed = True
    if changed:
        subject.save(update_fields=["password", "is_staff", "is_superuser"])
    app_model = get_application_model()
    app = app_model.objects.filter(client_id=ctx.internal_client_id).first()
    if app is None:
        app = app_model(
            client_id=ctx.internal_client_id,
            user=subject,
            name="Cognita internal control",
            client_type=app_model.CLIENT_CONFIDENTIAL,
            authorization_grant_type=app_model.GRANT_CLIENT_CREDENTIALS,
            client_secret=ctx.key.hex(),
            hash_client_secret=True,
        )
        app.save()
    elif (
        app.user_id != subject.pk
        or app.client_type != app_model.CLIENT_CONFIDENTIAL
        or app.authorization_grant_type != app_model.GRANT_CLIENT_CREDENTIALS
        or not check_password(ctx.key.hex(), app.client_secret)
    ):
        raise RuntimeError("Cognita internal OAuth application has unexpected configuration")


def bootstrap_service(config_path: Path | None = None) -> ServiceContext:
    config: CognitaConfig = load_config(config_path)
    key = ensure_oauth_service_key(config)
    store_path = oauth_service_store_path(config)
    store_path.parent.mkdir(parents=True, exist_ok=True)
    store_existed = store_path.is_file()
    ensure_private_sqlite(store_path, create=True)
    if store_existed:
        _backup_sqlite(store_path, Path(config.data_root))
    configure_django(config, key, store_path)
    call_command("migrate", verbosity=0, interactive=False)
    # Durable Cognita principal bindings share the OAuth database but remain a
    # small project-owned schema.  Creating them after DOT migrations keeps
    # startup idempotent and lets the child bind code/access/refresh rows in one
    # SQLite transaction without introducing a second OAuth implementation.
    OAuthPrincipalStore(store_path)
    registry = Registry(Path(config.registry_path))
    ctx = ServiceContext(config=config, key=key, store_path=store_path, registry=registry)
    _provision_subject_and_internal_app(ctx)
    if not has_argon2_credentials(config):
        log.warning("OAuth child readiness disabled: Argon2 admin credentials are not configured")
    from .policy import ResourcePolicy, set_policy
    set_policy(ResourcePolicy(config, registry))
    return ctx
