from __future__ import annotations

import logging
from pathlib import Path

import pytest

from cognita.auth_policy import (
    STATIC_KEY_PREFIX,
    AuthenticationLockoutConfirmationRequired,
    AuthenticationPolicyStore,
    AuthenticationPolicyUnavailable,
    AuthenticationRedactionFilter,
    AuthenticationRevisionConflict,
    generate_static_key,
    migrate_authentication_policy,
    redact_authentication_text,
)


def test_new_policy_defaults_oauth_on_and_never_persists_raw_key(tmp_path: Path):
    store = migrate_authentication_policy(tmp_path / "authentication.yaml", project_names=("KEI",))
    assert store.effective_oauth("KEI") is True
    result = store.mutate_global(expected_revision=0, static_key_action="generate")
    raw = result["generated_key"]
    assert raw.startswith(STATIC_KEY_PREFIX)
    assert raw not in (tmp_path / "authentication.yaml").read_text()
    assert store.verify_static_key(raw).key_id == result["global"]["static_key"]["key_id"]


def test_project_override_shadows_global_and_clear_inherits(tmp_path: Path):
    store = AuthenticationPolicyStore(tmp_path / "authentication.yaml", project_names=("A", "B"))
    global_result = store.mutate_global(expected_revision=0, static_key_action="generate")
    global_key = global_result["generated_key"]
    project_result = store.mutate_project("A", expected_revision=1, static_key_action="generate")
    project_key = project_result["generated_key"]
    assert store.verify_static_key(project_key).kind == "static_project"
    assert store.verify_static_key(global_key).project_name is None
    cleared = store.mutate_project("A", expected_revision=2, static_key_action="clear")
    assert cleared["projects"][0]["effective_static_key_source"] == "global"
    assert store.verify_static_key(global_key).kind == "static_global"


def test_dedicated_key_methods_replace_and_revoke_exact_scope(tmp_path: Path):
    store = AuthenticationPolicyStore(tmp_path / "authentication.yaml", project_names=("A",))
    global_result = store.generate_global_static_key(expected_revision=0)
    global_key = global_result["generated_key"]
    project_result = store.generate_project_static_key("A", expected_revision=1)
    project_key = project_result["generated_key"]

    revoked = store.revoke_project_static_key("A", expected_revision=2)
    assert revoked["projects"][0]["effective_static_key_source"] == "global"
    assert store.verify_static_key(project_key) is None
    assert store.verify_static_key(global_key) is not None

    unchanged = store.revoke_project_static_key("A", expected_revision=3)
    assert unchanged["revision"] == 3
    assert store.verify_static_key(global_key) is not None


def test_dedicated_global_revoke_requires_confirmation_and_rolls_back_on_write_failure(tmp_path: Path, monkeypatch):
    path = tmp_path / "authentication.yaml"
    store = AuthenticationPolicyStore(path, project_names=("A",), legacy_oauth_enabled=False)
    generated = store.generate_global_static_key(expected_revision=0)
    raw = generated["generated_key"]
    before = path.read_bytes()

    with pytest.raises(AuthenticationLockoutConfirmationRequired):
        store.revoke_global_static_key(expected_revision=1, project_names=("A",))
    assert path.read_bytes() == before
    assert store.verify_static_key(raw) is not None

    monkeypatch.setattr(store, "_write", lambda *_args, **_kwargs: (_ for _ in ()).throw(
        AuthenticationPolicyUnavailable("simulated persistence failure")
    ))
    with pytest.raises(AuthenticationPolicyUnavailable):
        store.revoke_global_static_key(
            expected_revision=1, confirm_lockout=True, project_names=("A",)
        )
    assert path.read_bytes() == before
    assert store.verify_static_key(raw) is not None


def test_lockout_requires_explicit_confirmation(tmp_path: Path):
    store = AuthenticationPolicyStore(tmp_path / "authentication.yaml", project_names=("A",))
    with pytest.raises(AuthenticationLockoutConfirmationRequired):
        store.mutate_global(expected_revision=0, oauth_enabled=False)
    result = store.mutate_global(expected_revision=0, oauth_enabled=False, confirm_lockout=True)
    assert result["projects"][0]["locked_out"] is True


def test_stale_revision_does_not_generate_or_write(tmp_path: Path):
    store = AuthenticationPolicyStore(tmp_path / "authentication.yaml")
    first = store.mutate_global(expected_revision=0, static_key_action="generate")
    before = (tmp_path / "authentication.yaml").read_bytes()
    with pytest.raises(AuthenticationRevisionConflict):
        store.mutate_global(expected_revision=0, static_key_action="generate")
    assert (tmp_path / "authentication.yaml").read_bytes() == before
    assert first["revision"] == 1


def test_malformed_policy_fails_closed_without_replacement(tmp_path: Path):
    path = tmp_path / "authentication.yaml"
    path.write_text("version: 999\n", encoding="utf-8")
    with pytest.raises(AuthenticationPolicyUnavailable):
        AuthenticationPolicyStore(path)
    assert path.read_text(encoding="utf-8") == "version: 999\n"


def test_generator_shape_and_constant_scan(tmp_path: Path):
    raw, record = generate_static_key()
    assert len(raw) == len(STATIC_KEY_PREFIX) + 43
    assert record.key_id == record.digest[:12]
    store = AuthenticationPolicyStore(tmp_path / "authentication.yaml", project_names=("A",))
    store.mutate_global(expected_revision=0, static_key_action="generate")
    assert store.verify_static_key("cog_sk_v1_bad") is None


def test_orphan_rows_are_inert_and_readd_cleanup(tmp_path: Path):
    store = AuthenticationPolicyStore(tmp_path / "authentication.yaml", project_names=("A",))
    result = store.mutate_project("A", expected_revision=0, static_key_action="generate")
    key = result["generated_key"]
    assert store.verify_static_key(key).project_name == "A"
    assert store.orphaned_project_entries([]) == ["A"]
    store.remove_orphan("A")
    assert store.verify_static_key(key) is None


def test_registry_membership_updates_allow_keys_for_projects_added_after_start(tmp_path: Path):
    from cognita.registry import Project, Registry

    registry = Registry(tmp_path / "registry.yaml")
    store = AuthenticationPolicyStore(tmp_path / "authentication.yaml")
    registry.attach_authentication_store(store)
    registry.add(Project(name="Later", documents_dir=tmp_path, data_dir=tmp_path))

    generated = store.mutate_project(
        "Later", expected_revision=0, static_key_action="generate"
    )

    assert store.verify_static_key(generated["generated_key"]).project_name == "Later"


def test_mutation_rereads_disk_before_revision_check(tmp_path: Path):
    path = tmp_path / "authentication.yaml"
    first = AuthenticationPolicyStore(path, project_names=("A",))
    second = AuthenticationPolicyStore(path, project_names=("A",))
    first.mutate_global(expected_revision=0, static_key_action="generate")

    with pytest.raises(AuthenticationRevisionConflict):
        second.mutate_global(expected_revision=0, oauth_enabled=False)


def test_redaction_filter_covers_bearer_and_generated_key(caplog):
    text = redact_authentication_text("Authorization: Bearer cog_sk_v1_secret generated_key=cog_sk_v1_other")
    assert "cog_sk_v1_secret" not in text and "cog_sk_v1_other" not in text
    logger = logging.getLogger("auth-policy-test")
    handler = logging.Handler()
    handler.addFilter(AuthenticationRedactionFilter())
    records = []
    handler.emit = records.append
    logger.addHandler(handler)
    try:
        logger.warning("generated_key=%s", "cog_sk_v1_secret")
    finally:
        logger.removeHandler(handler)
    assert "cog_sk_v1_secret" not in records[0].getMessage()
