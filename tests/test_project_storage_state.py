from __future__ import annotations

import json

import pytest

from cognita.books.state import (
    BOOTSTRAP_FILENAME,
    DATABASE_FILENAME,
    INITIALIZED_FILENAME,
    IndexedRoleProvenance,
    STATE_DIRECTORY,
    ProjectState,
    ProjectStateError,
)


def test_discover_does_not_initialize_a_pristine_project(tmp_path):
    assert ProjectState.discover(tmp_path) is None
    assert not (tmp_path / STATE_DIRECTORY).exists()


def test_initialized_policy_and_receipt_survive_reopen(tmp_path):
    state = ProjectState.initialize(tmp_path)
    status, result, revision = state.set_folder_rule(
        "source", False, 0,
        owner_key="principal:owner:connector-a",
        project="fixture",
        tool="set_folder_indexing",
        operation_id="disable-source-1",
        args_sha256="a" * 64,
        result={"path": "source", "indexed": False, "job_id": None},
    )

    assert status == "committed"
    assert revision == 1
    assert result["policy_revision"] == 1
    reopened = ProjectState.discover(tmp_path)
    assert reopened is not None
    assert reopened.folder_policy().policy_revision == 1
    assert reopened.folder_policy().rules == (("source", False),)
    assert reopened.receipt(
        owner_key="principal:owner:connector-a",
        project="fixture",
        tool="set_folder_indexing",
        operation_id="disable-source-1",
    ) == ("a" * 64, result)


def test_receipt_replay_precedes_policy_revision_check_and_conflicts_on_digest(tmp_path):
    state = ProjectState.initialize(tmp_path)
    kwargs = dict(
        owner_key="principal:owner:connector-a",
        project="fixture",
        tool="set_folder_indexing",
        operation_id="op-1",
        result={"path": "source", "indexed": False, "job_id": None},
    )
    first = state.set_folder_rule("source", False, 0, args_sha256="a" * 64, **kwargs)
    replay = state.set_folder_rule("source", False, 0, args_sha256="a" * 64, **kwargs)
    conflict = state.set_folder_rule("elsewhere", False, 99, args_sha256="b" * 64, **kwargs)

    assert first[0] == "committed"
    assert replay == ("replay", first[1], -1)
    assert conflict == ("conflict", {}, -1)
    assert state.folder_policy().policy_revision == 1


def test_stale_policy_revision_does_not_change_rule_or_consume_receipt(tmp_path):
    state = ProjectState.initialize(tmp_path)
    status, _, current = state.set_folder_rule(
        "source", False, 2,
        owner_key="owner", project="fixture", tool="set_folder_indexing",
        operation_id="stale", args_sha256="a" * 64,
        result={"path": "source", "indexed": False, "job_id": None},
    )
    assert (status, current) == ("stale", 0)
    assert state.folder_policy().rules == ()
    assert state.receipt(
        owner_key="owner", project="fixture", tool="set_folder_indexing", operation_id="stale",
    ) is None


def test_existing_state_evidence_with_missing_database_fails_closed(tmp_path):
    root = tmp_path / STATE_DIRECTORY
    root.mkdir()
    (root / INITIALIZED_FILENAME).write_text(
        json.dumps({"schema_version": 1, "database": DATABASE_FILENAME}), encoding="utf-8"
    )

    with pytest.raises(ProjectStateError):
        ProjectState.discover(tmp_path)


def test_interrupted_first_bootstrap_is_the_only_markerless_recovery_case(tmp_path):
    root = tmp_path / STATE_DIRECTORY
    root.mkdir()
    (root / BOOTSTRAP_FILENAME).write_text(
        json.dumps({"schema_version": 1, "database": DATABASE_FILENAME}), encoding="utf-8"
    )

    state = ProjectState.discover(tmp_path)
    assert state is not None
    assert (root / DATABASE_FILENAME).is_file()
    assert (root / INITIALIZED_FILENAME).is_file()
    assert not (root / BOOTSTRAP_FILENAME).exists()


def test_view_and_snapshot_records_are_durable_and_snapshot_receipt_is_atomic(tmp_path):
    state = ProjectState.initialize(tmp_path)
    state.save_view(
        view_id="view-1", chapter_id="chapter-1", scope_json='{"kind":"production"}',
        payload={"prose_sha256": "a" * 64, "text": "fixture"},
        expires_at="2099-01-01T00:00:00+00:00",
    )
    assert ProjectState.discover(tmp_path).load_view("view-1")["payload"]["text"] == "fixture"

    kwargs = dict(
        snapshot_id="snapshot-1", chapter_id="chapter-1", scope_key="production",
        expected_manifest_revision=None,
        payload={"snapshot_filepath": "Chapters/One/Audiobook/Snapshots/snapshot-1"},
        request_plan_sha256="b" * 64,
        receipt_owner_key="principal:owner:connector-a", project="fixture",
        tool="audiobook_prepare_chapter", operation_id="prepare-1",
        args_sha256="c" * 64, result={"snapshot_id": "snapshot-1"},
    )
    status, revision, result = state.commit_snapshot(**kwargs)
    replay = state.commit_snapshot(**kwargs | {"expected_manifest_revision": 0})

    assert status == "committed" and revision == 1
    assert result == {"snapshot_id": "snapshot-1", "manifest_revision": 1}
    assert replay == ("replay", -1, result)
    reopened = ProjectState.discover(tmp_path)
    assert reopened.namespace("chapter-1", "production")["current_snapshot_id"] == "snapshot-1"
    assert reopened.snapshot("snapshot-1")["payload"]["snapshot_filepath"].endswith("snapshot-1")


def test_snapshot_receipt_conflict_and_manifest_cas_are_atomic(tmp_path):
    state = ProjectState.initialize(tmp_path)
    base = dict(
        chapter_id="chapter-1", scope_key="production", request_plan_sha256="b" * 64,
        receipt_owner_key="owner", project="fixture", tool="audiobook_prepare_chapter",
        operation_id="prepare-1", args_sha256="c" * 64,
    )
    first = state.commit_snapshot(
        snapshot_id="snapshot-1", expected_manifest_revision=None,
        payload={"value": 1}, result={"snapshot_id": "snapshot-1"}, **base,
    )
    with pytest.raises(ProjectStateError, match="operation_id_conflict"):
        state.commit_snapshot(
            snapshot_id="snapshot-2", expected_manifest_revision=1,
            payload={"value": 2}, result={"snapshot_id": "snapshot-2"},
            **(base | {"args_sha256": "d" * 64}),
        )
    with pytest.raises(ProjectStateError, match="stale_manifest"):
        state.commit_snapshot(
            snapshot_id="snapshot-3", expected_manifest_revision=7,
            payload={"value": 3}, result={"snapshot_id": "snapshot-3"},
            **(base | {"operation_id": "prepare-2"}),
        )

    assert first[0:2] == ("committed", 1)
    assert state.namespace("chapter-1", "production")["current_snapshot_id"] == "snapshot-1"
    assert state.snapshot("snapshot-2") is None


def test_indexed_role_provenance_is_strict_and_survives_reopen(tmp_path):
    state = ProjectState.initialize(tmp_path)
    record = IndexedRoleProvenance(
        source_path="Book/Chapter.docx", doc_id="doc-current",
        extracted_sha256="a" * 64, raw_sha256="b" * 64,
        extraction_version="cognita-docx-v1", role="chapter_working",
        chapter_id="ch01", layout_sha256="c" * 64,
        chapter_state_sha256="d" * 64, annotations_sha256=None,
        approval_source_raw_sha256="b" * 64,
        approval_prose_projection_sha256="e" * 64,
        approval_projection_version="cognita-docx-v1",
        summary_raw_sha256=None, summary_source_raw_sha256=None,
        summary_source_prose_projection_sha256=None,
    )

    state.put_indexed_role_provenance(record)
    reopened = ProjectState.discover(tmp_path)

    assert reopened is not None
    assert reopened.indexed_role_provenance(record.source_path) == record
    assert reopened.delete_indexed_role_provenance(record.source_path)
    assert not reopened.delete_indexed_role_provenance(record.source_path)
    assert reopened.indexed_role_provenance(record.source_path) is None


def test_indexed_role_provenance_rejects_invalid_paths_or_hashes():
    with pytest.raises(ValueError):
        IndexedRoleProvenance(
            source_path="../private.docx", doc_id="id", extracted_sha256="a" * 64,
            raw_sha256="b" * 64, extraction_version="v1", role="chapter_working",
            chapter_id="ch1", layout_sha256="c" * 64,
            chapter_state_sha256=None, annotations_sha256=None,
            approval_source_raw_sha256=None, approval_prose_projection_sha256=None,
            approval_projection_version=None, summary_raw_sha256=None,
            summary_source_raw_sha256=None, summary_source_prose_projection_sha256=None,
        )
    with pytest.raises(ValueError):
        IndexedRoleProvenance(
            source_path="chapter.docx", doc_id="id", extracted_sha256="A" * 64,
            raw_sha256="b" * 64, extraction_version="v1", role="chapter_working",
            chapter_id="ch1", layout_sha256="c" * 64,
            chapter_state_sha256=None, annotations_sha256=None,
            approval_source_raw_sha256=None, approval_prose_projection_sha256=None,
            approval_projection_version=None, summary_raw_sha256=None,
            summary_source_raw_sha256=None, summary_source_prose_projection_sha256=None,
         )
