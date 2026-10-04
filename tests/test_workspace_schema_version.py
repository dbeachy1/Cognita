"""13.2.0: the Workspace metadata file carries a schema version (Doug, 2026-09-22).

The rule: a file at this build's version is left alone; a newer file is refused;
an older file -- older stamp, or no stamp with columns missing -- is RESET by
default and upgraded in place only when the build explicitly says its change is
additive (WORKSPACE_SCHEMA_RESET_REQUIRED = False). A file with no stamp whose
tables already match (what 13.1.0 wrote) is simply stamped.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from cognita import workspace as ws
from cognita import workspace_store as storage
from cognita.workspace import WorkspaceMetadataStore, WorkspaceStateIncompatible


def columns(path: Path, table: str) -> set[str]:
    db = sqlite3.connect(path)
    try:
        return {str(row[1]) for row in db.execute(f"PRAGMA table_info({table})")}
    finally:
        db.close()


def stamp_of(path: Path) -> int | None:
    db = sqlite3.connect(path)
    try:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='workspace_schema'").fetchone():
            return None
        row = db.execute("SELECT version FROM workspace_schema WHERE singleton=1").fetchone()
        return int(row[0]) if row else None
    finally:
        db.close()


def write_stamp(path: Path, version: int) -> None:
    db = sqlite3.connect(path)
    try:
        db.execute("INSERT OR REPLACE INTO workspace_schema(singleton, version, stamped_at) VALUES(1, ?, 'x')", (version,))
        db.commit()
    finally:
        db.close()


def drop_column(path: Path, table: str, column: str) -> None:
    db = sqlite3.connect(path)
    try:
        db.execute(f"ALTER TABLE {table} DROP COLUMN {column}")
        db.commit()
    finally:
        db.close()


def lease_count(path: Path) -> int:
    db = sqlite3.connect(path)
    try:
        return int(db.execute("SELECT count(*) FROM workspace_leases").fetchone()[0])
    finally:
        db.close()


def a_file_written_by_this_build(path: Path) -> Path:
    store = WorkspaceMetadataStore(path)
    try:
        with store.transaction() as db:
            db.execute(
                "INSERT INTO workspace_leases(workspace_id, lease_id, kind, expires_at) "
                "VALUES('w1', 'l1', 'read', '2099-01-01T00:00:00')"
            )
    finally:
        store.close()
    return path


def test_a_fresh_file_is_stamped_with_this_builds_version(tmp_path):
    path = a_file_written_by_this_build(tmp_path / "w.sqlite3")
    assert stamp_of(path) == ws.WORKSPACE_SCHEMA_VERSION
    assert "workspace_schema" in ws.DISPOSABLE_TABLES


def test_a_file_at_this_version_is_left_alone(tmp_path, caplog):
    path = a_file_written_by_this_build(tmp_path / "w.sqlite3")
    with caplog.at_level("DEBUG", logger="cognita.workspace"):
        WorkspaceMetadataStore(path).close()
    assert stamp_of(path) == ws.WORKSPACE_SCHEMA_VERSION
    assert any(getattr(record, "event", "") == "workspace_store_layout_ok" for record in caplog.records)
    assert lease_count(path) == 1


def test_a_file_from_before_the_stamp_with_matching_tables_is_stamped(tmp_path, caplog):
    """13.1.0 wrote exactly this layout minus the stamp table: no reset."""
    path = a_file_written_by_this_build(tmp_path / "w.sqlite3")
    db = sqlite3.connect(path)
    db.execute("DROP TABLE workspace_schema")
    db.commit()
    db.close()
    assert stamp_of(path) is None
    with caplog.at_level("INFO", logger="cognita.workspace"):
        WorkspaceMetadataStore(path).close()
    assert stamp_of(path) == ws.WORKSPACE_SCHEMA_VERSION
    assert any(getattr(record, "event", "") == "workspace_store_layout_stamped" for record in caplog.records)
    assert lease_count(path) == 1


def test_an_older_file_is_refused_by_default(tmp_path, monkeypatch):
    """The default is a reset: 'if it's explicitly set to no reset, then we
    don't reset; otherwise, we do.'"""
    path = a_file_written_by_this_build(tmp_path / "w.sqlite3")
    drop_column(path, "workspaces", "last_auto_action_at")
    write_stamp(path, ws.WORKSPACE_SCHEMA_VERSION - 1)
    monkeypatch.setattr(storage, "WORKSPACE_SCHEMA_RESET_REQUIRED", True)
    with pytest.raises(WorkspaceStateIncompatible) as refused:
        WorkspaceMetadataStore(path)
    text = str(refused.value)
    assert "requires a reset" in text
    assert "workspaces: missing column(s) last_auto_action_at" in text
    assert "Discard and regenerate it with:" in text
    assert "--scope workspaces --apply" in text
    # Nothing was touched: the column is still missing and the stamp is the old one.
    assert "last_auto_action_at" not in columns(path, "workspaces")
    assert stamp_of(path) == ws.WORKSPACE_SCHEMA_VERSION - 1


def test_an_unstamped_older_file_is_refused_by_default_too(tmp_path, monkeypatch):
    """What a 13.0.x file looks like to 13.1.0+: no stamp, two columns short."""
    path = a_file_written_by_this_build(tmp_path / "w.sqlite3")
    db = sqlite3.connect(path)
    db.execute("DROP TABLE workspace_schema")
    db.execute("ALTER TABLE workspaces DROP COLUMN last_auto_action")
    db.execute("ALTER TABLE workspaces DROP COLUMN last_auto_action_at")
    db.commit()
    db.close()
    monkeypatch.setattr(storage, "WORKSPACE_SCHEMA_RESET_REQUIRED", True)
    with pytest.raises(WorkspaceStateIncompatible) as refused:
        WorkspaceMetadataStore(path)
    assert "no stamp, this build is" in str(refused.value)
    assert "last_auto_action, last_auto_action_at" in str(refused.value)


def test_an_older_file_is_upgraded_in_place_only_when_the_build_says_so(tmp_path, monkeypatch, caplog):
    path = a_file_written_by_this_build(tmp_path / "w.sqlite3")
    drop_column(path, "workspaces", "last_auto_action_at")
    drop_column(path, "workspace_leases", "generation")   # NOT NULL DEFAULT 0: addable
    write_stamp(path, ws.WORKSPACE_SCHEMA_VERSION - 1)
    monkeypatch.setattr(storage, "WORKSPACE_SCHEMA_RESET_REQUIRED", False)
    with caplog.at_level("INFO", logger="cognita.workspace"):
        WorkspaceMetadataStore(path).close()
    assert "last_auto_action_at" in columns(path, "workspaces")
    assert "generation" in columns(path, "workspace_leases")
    assert stamp_of(path) == ws.WORKSPACE_SCHEMA_VERSION
    upgraded = [record for record in caplog.records if getattr(record, "event", "") == "workspace_store_layout_upgraded"]
    assert {(record.table, record.column) for record in upgraded} == {
        ("workspaces", "last_auto_action_at"), ("workspace_leases", "generation"),
    }
    # The row survived and the added NOT NULL column took its default.
    db = sqlite3.connect(path)
    assert db.execute("SELECT generation FROM workspace_leases WHERE lease_id='l1'").fetchone()[0] == 0
    db.close()
    # And the next open is the quiet same-version path.
    WorkspaceMetadataStore(path).close()


def test_a_no_reset_claim_for_a_column_sqlite_cannot_add_is_refused(tmp_path, monkeypatch):
    """A NOT NULL column without a default cannot be added to a table with
    rows; a build that says 'no reset' for that change is wrong, and the
    message says so instead of half-upgrading."""
    path = a_file_written_by_this_build(tmp_path / "w.sqlite3")
    drop_column(path, "workspace_leases", "expires_at")   # TEXT NOT NULL, no default
    write_stamp(path, ws.WORKSPACE_SCHEMA_VERSION - 1)
    monkeypatch.setattr(storage, "WORKSPACE_SCHEMA_RESET_REQUIRED", False)
    with pytest.raises(WorkspaceStateIncompatible) as refused:
        WorkspaceMetadataStore(path)
    assert "WORKSPACE_SCHEMA_RESET_REQUIRED is wrong for this change" in str(refused.value)
    assert "workspace_leases.expires_at" in str(refused.value)
    assert "expires_at" not in columns(path, "workspace_leases")


def test_a_file_from_a_newer_build_is_refused(tmp_path, monkeypatch):
    path = a_file_written_by_this_build(tmp_path / "w.sqlite3")
    write_stamp(path, ws.WORKSPACE_SCHEMA_VERSION + 5)
    monkeypatch.setattr(storage, "WORKSPACE_SCHEMA_RESET_REQUIRED", False)
    with pytest.raises(WorkspaceStateIncompatible) as refused:
        WorkspaceMetadataStore(path)
    assert f"schema version {ws.WORKSPACE_SCHEMA_VERSION + 5} is newer than this build's" in str(refused.value)
    assert stamp_of(path) == ws.WORKSPACE_SCHEMA_VERSION + 5


def test_a_file_that_claims_this_version_but_lacks_columns_is_refused(tmp_path, monkeypatch):
    path = a_file_written_by_this_build(tmp_path / "w.sqlite3")
    drop_column(path, "workspaces", "last_auto_action")
    monkeypatch.setattr(storage, "WORKSPACE_SCHEMA_RESET_REQUIRED", False)
    with pytest.raises(WorkspaceStateIncompatible) as refused:
        WorkspaceMetadataStore(path)
    assert f"schema version {ws.WORKSPACE_SCHEMA_VERSION} but workspaces: missing column(s) last_auto_action" in str(refused.value)


def test_a_missing_table_is_created_not_refused(tmp_path):
    path = a_file_written_by_this_build(tmp_path / "w.sqlite3")
    db = sqlite3.connect(path)
    db.execute("DROP TABLE workspace_delete_previews")
    db.commit()
    db.close()
    WorkspaceMetadataStore(path).close()
    assert columns(path, "workspace_delete_previews")


def test_read_only_open_never_stamps_or_upgrades(tmp_path, monkeypatch):
    path = a_file_written_by_this_build(tmp_path / "w.sqlite3")
    drop_column(path, "workspaces", "last_auto_action_at")
    write_stamp(path, ws.WORKSPACE_SCHEMA_VERSION - 1)
    monkeypatch.setattr(storage, "WORKSPACE_SCHEMA_RESET_REQUIRED", False)
    WorkspaceMetadataStore(path, read_only=True).close()
    assert "last_auto_action_at" not in columns(path, "workspaces")
    assert stamp_of(path) == ws.WORKSPACE_SCHEMA_VERSION - 1
