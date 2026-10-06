from __future__ import annotations

import hashlib
import uuid
from pathlib import Path

import pytest
from argon2 import PasswordHasher

import cognita.auth_policy as auth_policy
from cognita.auth_policy import (
    V2_STATIC_KEY_PREFIX,
    CredentialAdminService,
    CredentialCapacityExceeded,
    CredentialNotFound,
    CredentialPolicyStore,
    CredentialRevealDenied,
    parse_v2_static_key,
)


def _store(tmp_path: Path) -> CredentialPolicyStore:
    return CredentialPolicyStore(
        tmp_path / "credentials-v2.json",
        master_key_dir=tmp_path / "master-keys",
        admin_password_hash=PasswordHasher().hash("correct horse"),
    )


def test_v2_add_verify_reveal_and_aad_binding(tmp_path: Path):
    store = _store(tmp_path)
    surface = str(uuid.uuid4())
    record, secret = store.add_credential("combined", surface, "Primary", surface_slug="primary", password="correct horse")
    assert secret.startswith(V2_STATIC_KEY_PREFIX)
    assert store.verify(secret, surface_kind="combined", surface_id=surface).principal_id == record.credential_id
    assert store.verify_for_surface(secret, surface_kind="combined", surface_id=surface, surface_slug="wrong") is None
    assert store.verify_for_surface(secret, surface_kind="combined", surface_id=surface, surface_slug="primary").principal_id == record.credential_id
    assert store.verify(secret, surface_kind="workspace", surface_id=surface) is None
    proof = store.begin_reveal(record.credential_id, "correct horse")
    assert store.reveal(record.credential_id, proof) == secret
    with pytest.raises(CredentialRevealDenied):
        store.reveal(record.credential_id, proof)
    assert secret not in (tmp_path / "credentials-v2.json").read_text()


def test_rotate_preserves_identity_and_revoke_denies(tmp_path: Path):
    store = _store(tmp_path)
    surface = str(uuid.uuid4())
    record, first = store.add_credential("workspace", surface, "Runner", password="correct horse")
    rotated, second = store.rotate_secret(record.credential_id, password="correct horse")
    assert rotated.credential_id == record.credential_id
    assert store.verify(first, surface_kind="workspace", surface_id=surface) is None
    assert store.verify(second, surface_kind="workspace", surface_id=surface).principal_id == record.credential_id
    store.revoke(record.credential_id)
    assert store.verify(second, surface_kind="workspace", surface_id=surface) is None


def test_tombstone_replay_passes_original_deleted_at(tmp_path: Path, monkeypatch):
    store = _store(tmp_path)
    surface = str(uuid.uuid4())
    row, _secret = store.add_credential("workspace", surface, "Runner", password="correct horse")
    monkeypatch.setattr(auth_policy, "_now", lambda: "2026-09-19T00:00:00+00:00")
    first = store.delete(row.credential_id, workspace_retention="normal")
    monkeypatch.setattr(auth_policy, "_now", lambda: "2026-11-19T00:00:00+00:00")
    repeated = store.delete(row.credential_id, workspace_retention="normal")
    assert repeated.deleted_at == first.deleted_at
    seen = []

    def reconcile(**kwargs):
        seen.append(kwargs)
        return {"complete": False}

    assert store.reconcile_tombstones(reconcile)[0]["status"] == "pending"
    assert seen == [{
        "credential_id": row.credential_id, "owner_status": "tombstoned",
        "retention": "normal", "deleted_at": "2026-09-19T00:00:00+00:00",
    }]


def test_limit_names_tombstones_and_legacy_are_bounded(tmp_path: Path):
    store = _store(tmp_path)
    surface = str(uuid.uuid4())
    for index in range(64):
        store.add_credential("combined", surface, f"key-{index}", password="correct horse")
    with pytest.raises(CredentialCapacityExceeded):
        store.add_credential("combined", surface, "overflow", password="correct horse")
    row = store.list_surface("combined", surface)[0]
    store.delete(row.credential_id, workspace_retention="delete_now")
    assert len(store.list_surface("combined", surface)) == 63
    assert len(store.list_surface("combined", surface, include_tombstones=True)) == 64
    legacy_secret = "cog_sk_v1_" + "x" * 43
    principal = store.migrate_legacy_digest(hashlib.sha256(legacy_secret.encode()).hexdigest(), surface_kind="combined", surface_id=surface, scope="global")
    assert principal.kind == "legacy_static" and principal.workspace_enabled is False
    assert store.verify(legacy_secret, surface_kind="combined", surface_id=surface).kind == "legacy_static"


def test_master_key_rotation_keeps_reveal_working(tmp_path: Path):
    store = _store(tmp_path)
    surface = str(uuid.uuid4())
    row, secret = store.add_credential("combined", surface, "Rotate", password="correct horse")
    old_generation = store.master_keys.active_generation
    new_generation = store.rotate_master_key()
    assert new_generation != old_generation
    proof = store.begin_reveal(row.credential_id, "correct horse")
    assert store.reveal(row.credential_id, proof) == secret


def test_master_key_rotation_includes_trusted_service_secrets(tmp_path: Path):
    store = _store(tmp_path)
    store.store_trusted_secret("brave-search-api-key", "brave-secret-value")
    policy_text = (tmp_path / "credentials-v2.json").read_text()
    assert "brave-secret-value" not in policy_text
    old_generation = store.master_keys.active_generation
    new_generation = store.rotate_master_key()
    assert new_generation != old_generation
    assert store.trusted_secret("brave-search-api-key") == "brave-secret-value"


def test_master_key_generation_can_be_staged_without_early_activation(tmp_path: Path):
    store = _store(tmp_path)
    active = store.master_keys.active_generation
    staged = store.master_keys.create_generation(activate=False)
    assert staged != active
    assert store.master_keys.active_generation == active
    assert store.master_keys.key(staged) != store.master_keys.key(active)


def test_admin_adapter_rejects_cross_surface_credential_actions(tmp_path: Path):
    store = _store(tmp_path)
    first = str(uuid.uuid4())
    second = str(uuid.uuid4())
    surfaces = {
        ("combined", first): {"id": first, "slug": "first"},
        ("combined", second): {"id": second, "slug": "second"},
    }
    service = CredentialAdminService(
        store,
        surface_resolver=lambda kind, identifier: surfaces.get((kind, identifier)),
        public_base_url="https://example.test",
    )
    created = service.create_credential(
        surface_kind="combined", surface_id=first, expected_revision=0,
        label="Primary", current_password="correct horse",
    )
    credential_id = created["credential"].credential_id
    with pytest.raises(CredentialNotFound, match="credential not found"):
        service.reveal_credential(
            surface_kind="combined", surface_id=second,
            credential_id=credential_id, expected_revision=1,
            current_password="correct horse",
        )


def test_workspace_stable_setup_uses_current_v3_alias(tmp_path: Path):
    store = _store(tmp_path)
    surface_id = str(uuid.uuid4())
    service = CredentialAdminService(
        store,
        surface_resolver=lambda kind, identifier: (
            {"id": surface_id, "slug": "workspace"}
            if (kind, identifier) == ("workspace", surface_id)
            else None
        ),
        public_base_url="https://example.test",
    )
    created = service.create_credential(
        surface_kind="workspace", surface_id=surface_id,
        expected_revision=0, label="Runner", current_password="correct horse",
    )

    stable = service.setup_material(
        surface_kind="workspace", surface_id=surface_id,
        credential_id=created["credential"].credential_id,
        route_strategy="stable", current_password="correct horse",
    )
    current = service.setup_material(
        surface_kind="workspace", surface_id=surface_id,
        credential_id=created["credential"].credential_id,
        route_strategy="current", current_password="correct horse",
    )
    assert stable["url"] == "https://example.test/mcp/workspace/workspace/mcp"
    assert current["url"] == "https://example.test/mcp/workspace/workspace/mcp/v3"


def test_combined_credential_setup_offers_stable_and_current_v5(tmp_path: Path):
    store = _store(tmp_path)
    surface_id = str(uuid.uuid4())
    service = CredentialAdminService(
        store,
        surface_resolver=lambda kind, identifier: (
            {"id": surface_id, "slug": "primary"}
            if (kind, identifier) == ("combined", surface_id)
            else None
        ),
        public_base_url="https://example.test",
    )
    created = service.create_credential(
        surface_kind="combined", surface_id=surface_id,
        expected_revision=0, label="Client", current_password="correct horse",
    )
    credential_id = created["credential"].credential_id

    stable = service.setup_material(
        surface_kind="combined", surface_id=surface_id,
        credential_id=credential_id, current_password="correct horse",
    )
    current = service.setup_material(
        surface_kind="combined", surface_id=surface_id,
        credential_id=credential_id, route_strategy="current",
        current_password="correct horse",
    )
    assert stable["url"] == "https://example.test/mcp/connectors/primary/mcp"
    assert current["url"] == f"https://example.test/mcp/connectors/primary/mcp/v{PUBLIC_CONTRACT_VERSION}"
    assert stable["secret"] == current["secret"]


def test_token_parser_rejects_swapped_or_malformed_values():
    assert parse_v2_static_key("cog_sk_v2_bad") is None
    assert parse_v2_static_key("cog_sk_v2_aaaaaaaaaaaaaaaaaaaaaa." + "a" * 43) is None
