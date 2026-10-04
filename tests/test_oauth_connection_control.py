from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def test_package_connection_groups_use_live_native_tokens_and_group_revoke(tmp_path: Path) -> None:
    script = r'''
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
import base64
import json
import uuid
import yaml
from argon2 import PasswordHasher
from django import db
from django.test import RequestFactory
from django.utils import timezone
from cognita.oauth_service.asgi import create_application
from cognita.oauth_service.bootstrap import bootstrap_service
from cognita.oauth_service.principal import OAuthPrincipalStore
from cognita.workspace import WorkspaceMetadataStore

with TemporaryDirectory(prefix="cognita-oauth8-connections-") as root:
    base = Path(root)
    data = base / "data"
    data.mkdir()
    registry = base / "registry.yaml"
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
    try:
        context = bootstrap_service(config)
        create_application(context)
        from django.contrib.auth import get_user_model
        from oauth2_provider.models import (
            get_access_token_model,
            get_application_model,
            get_refresh_token_model,
        )
        from cognita.oauth_service.views import (
            ConnectionsView,
            IntrospectionView,
            _connection_groups,
            _connection_id,
            _record,
            _revoke_groups,
        )
        import cognita.oauth_service.views as views

        user = get_user_model().objects.get(username=context.subject_username)
        app_model = get_application_model()
        access_model = get_access_token_model()
        refresh_model = get_refresh_token_model()
        resource = "https://kei.example/mcp/connectors/cognita/mcp/v5"

        def app(name):
            return app_model.objects.create(
                client_id=name,
                user=user,
                name=name,
                client_type=app_model.CLIENT_PUBLIC,
                authorization_grant_type=app_model.GRANT_AUTHORIZATION_CODE,
            )

        def pair(application, label, expires, family=None):
            access = access_model.objects.create(
                user=user,
                application=application,
                token=f"access-{label}",
                expires=expires,
                scope="cognita:access",
                resource=[resource],
            )
            refresh = refresh_model.objects.create(
                user=user,
                application=application,
                token=f"refresh-{label}",
                access_token=access,
                token_family=family or uuid.uuid4(),
                resource=[resource],
            )
            return access, refresh

        now = timezone.now()
        rotating_app = app("rotating")
        family = uuid.uuid4()
        first_access, first_refresh = pair(
            rotating_app, "first", now + timedelta(hours=1), family
        )
        second_access, second_refresh = pair(
            rotating_app, "second", now + timedelta(hours=1), family
        )
        expired_app = app("expired")
        expired_access = access_model.objects.create(
            user=user,
            application=expired_app,
            token="access-expired",
            expires=now - timedelta(seconds=1),
            scope="cognita:access",
            resource=[resource],
        )
        access_only_app = app("access-only")
        access_only = access_model.objects.create(
            user=user,
            application=access_only_app,
            token="access-only-live",
            expires=now + timedelta(hours=1),
            scope="cognita:access",
            resource=[resource],
        )
        retired_access = access_model.objects.create(
            user=user,
            application=access_only_app,
            token="access-retired-v1",
            expires=now + timedelta(hours=1),
            scope="cognita:access",
            resource=["https://kei.example/mcp/connectors/cognita/mcp/v1"],
        )
        retired_introspection = IntrospectionView.get_token_response(retired_access.token)
        assert json.loads(retired_introspection.content) == {"active": False}
        retired_access.delete()

        groups = _connection_groups()
        rotating_key = (user.pk, rotating_app.pk, resource)
        expired_key = (user.pk, expired_app.pk, resource)
        access_only_key = (user.pk, access_only_app.pk, resource)
        assert rotating_key in groups
        assert expired_key not in groups
        assert access_only_key in groups
        first_id = _connection_id(rotating_key)
        assert first_id == _connection_id(rotating_key)
        record = _record(rotating_key, groups[rotating_key])
        assert record["id"] == first_id
        assert record["connector"]["id"] == "2c520a44-2037-4bb5-a565-d88ec2bb02d1"
        assert record["connector"]["name"] == "Cognita"
        assert record["connector"]["projects"] == [{"name": "demo", "access": "write"}]
        assert len(groups[rotating_key]["refresh"]) == 2
        assert len(groups[rotating_key]["access"]) == 2

        principal_store = OAuthPrincipalStore(context.store_path)
        principal = principal_store.create_principal(
            str(user.pk), str(rotating_app.pk), resource
        )
        metadata = WorkspaceMetadataStore(data / "workspace-metadata.sqlite3")
        workspace = metadata.create(
            principal.principal_id, "connector", "OAuth Workspace",
            now=now.isoformat(timespec="seconds"), quota_bytes=1024, retention_days=90,
        )
        workspace = metadata.update(
            workspace.workspace_id, pinned=1, deletion_intent="keep"
        )
        metadata.close()

        # A failure after principal revoke but before DOT revoke must leave the
        # live group as the retry handle.  The retry must reconcile the same
        # principal/Workspace and then revoke the native token rows.
        original_workspace_reconcile = views._reconcile_revoked_workspaces
        failed = {"value": False}

        def fail_once(principal_ids):
            if not failed["value"]:
                failed["value"] = True
                raise RuntimeError("synthetic Workspace reconciliation failure")
            return original_workspace_reconcile(principal_ids)

        views._reconcile_revoked_workspaces = fail_once
        try:
            try:
                _revoke_groups({rotating_key: groups[rotating_key]})
            except RuntimeError as exc:
                assert "synthetic" in str(exc)
            else:
                raise AssertionError("partial reconciliation unexpectedly succeeded")
        finally:
            views._reconcile_revoked_workspaces = original_workspace_reconcile

        assert access_model.objects.filter(pk__in=[first_access.pk, second_access.pk]).exists()
        assert refresh_model.objects.filter(
            pk__in=[first_refresh.pk, second_refresh.pk], revoked__isnull=True
        ).count() == 2
        assert OAuthPrincipalStore(context.store_path).get(principal.principal_id).revoked_at is not None

        assert _revoke_groups({rotating_key: groups[rotating_key]}) == 1
        assert not access_model.objects.filter(pk__in=[first_access.pk, second_access.pk]).exists()
        assert refresh_model.objects.filter(pk__in=[first_refresh.pk, second_refresh.pk], revoked__isnull=False).count() == 2
        assert rotating_key not in _connection_groups()
        metadata = WorkspaceMetadataStore(data / "workspace-metadata.sqlite3")
        retained = metadata.get_by_principal(principal.principal_id)
        assert retained is not None
        assert retained.owner_status == "revoked"
        assert retained.pinned == 1
        assert retained.deletion_intent == "keep"
        metadata.close()

        internal = "Basic " + base64.b64encode(
            (context.internal_client_id + ":" + context.key.hex()).encode()
        ).decode()
        request = RequestFactory().delete(f"/_cognita/connections/{first_id}")
        request.META["HTTP_AUTHORIZATION"] = internal
        assert ConnectionsView.as_view()(request, connection_id=first_id).status_code == 404

        all_app_a = app("all-a")
        all_app_b = app("all-b")
        pair(all_app_a, "all-a", now + timedelta(hours=1))
        pair(all_app_b, "all-b", now + timedelta(hours=1))
        request = RequestFactory().delete("/_cognita/connections")
        request.META["HTTP_AUTHORIZATION"] = internal
        response = ConnectionsView.as_view()(request)
        assert response.status_code == 200
        assert json.loads(response.content) == {"revoked_count": 3}
        assert _connection_groups() == {}

        db.connections.close_all()
    finally:
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
        timeout=45,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
