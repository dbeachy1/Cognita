from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from cognita.oauth_service.principal import (
    OAuthPrincipalStore,
    PrincipalBindingConflict,
    PrincipalRevoked,
    PrincipalValidationError,
)


def test_authorization_exchange_and_refresh_preserve_principal(tmp_path: Path):
    store = OAuthPrincipalStore(tmp_path / "oauth.sqlite3")
    principal = store.create_principal("user", "application", "https://example.test/mcp/x", code_key="code-1")
    assert store.bind_exchange("code-1", "access-1", "refresh-1").principal_id == principal.principal_id
    assert store.bind_refresh_rotation("refresh-1", "refresh-2", "access-2").principal_id == principal.principal_id
    assert store.principal_for_token("access", "access-2").principal_id == principal.principal_id


def test_separate_consents_and_revoke_are_isolated(tmp_path: Path):
    store = OAuthPrincipalStore(tmp_path / "oauth.sqlite3")
    first = store.create_principal("u", "a", "https://example.test/mcp/x", code_key="code-a")
    second = store.create_principal("u", "a", "https://example.test/mcp/x", code_key="code-b")
    assert first.principal_id != second.principal_id
    store.bind_exchange("code-a", "access-a")
    store.bind_exchange("code-b", "access-b")
    store.revoke(first.principal_id)
    assert store.principal_for_token("access", "access-a") is None
    assert store.principal_for_token("access", "access-b").principal_id == second.principal_id
    with pytest.raises(PrincipalRevoked):
        store.bind_token(first.principal_id, "access", "later")


def test_migration_is_idempotent_and_cross_principal_binding_fails(tmp_path: Path):
    store = OAuthPrincipalStore(tmp_path / "oauth.sqlite3")
    first = store.migrate_connection("u", "a", "https://example.test/mcp/x", [("access", "old-a"), ("refresh", "old-r")])
    again = store.migrate_connection("u", "a", "https://example.test/mcp/x", [("access", "old-a"), ("refresh", "old-r")])
    assert first.principal_id == again.principal_id
    other = store.create_principal("u", "a", "https://example.test/mcp/x")
    with pytest.raises(PrincipalBindingConflict):
        store.bind_token(other.principal_id, "access", "old-a")


def test_malformed_and_unbound_fail_closed(tmp_path: Path):
    store = OAuthPrincipalStore(tmp_path / "oauth.sqlite3")
    assert store.principal_for_token("access", "unknown") is None
    with pytest.raises(PrincipalValidationError):
        store.get(str(uuid.uuid4()) + "-bad")
    with pytest.raises(PrincipalValidationError):
        store.bind_exchange("unbound", "access")
