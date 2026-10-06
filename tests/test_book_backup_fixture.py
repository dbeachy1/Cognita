from __future__ import annotations

import hashlib

import pytest

from cognita.books.backup_fixture import (
    BookBackupFixtureError,
    restore_quiesced_book_project,
    snapshot_quiesced_book_project,
)
from cognita.books.state import ProjectState


def _book_project(root):
    root.mkdir()
    files = {
        "Project Files/Book_Layout.json": b'{"layout":"fixture"}',
        "Project Files/production-settings.json": b'{"target":"native"}',
        ".cognita-book-binding.json": b'{"state_root":".cognita-storage"}',
        "Chapters/01/chapter.docx": b"working docx bytes",
        "Chapters/01/Originals/source.docx": b"original docx bytes",
        "Audiobook/Chapters/01/native/take.pcm": b"\x00\x01\x02\x03",
        "Audiobook/Builds/book.mp3": b"accepted build bytes",
        "backups/Chapters/01/chapter.before.docx": b"history bytes",
    }
    for relative, value in files.items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(value)
    state = ProjectState.initialize(root)
    snapshot = root / ".cognita-storage/snapshots/one/prose.docx"
    snapshot.parent.mkdir(parents=True, exist_ok=True)
    snapshot.write_bytes(b"immutable snapshot")
    files[".cognita-storage/snapshots/one/prose.docx"] = b"immutable snapshot"
    status, receipt, revision = state.set_folder_rule(
        "Chapters/01", False, 0, owner_key="fixture-owner", project="fixture",
        tool="set_folder_indexing", operation_id="fixture-op", args_sha256="a" * 64,
        result={"status": "success"},
    )
    assert (status, revision) == ("committed", 1)
    assert receipt["policy_revision"] == 1
    data = root.parent / "data"
    data.mkdir()
    policy = b'{"schema_version":1,"paths":{"Chapters/01":false}}\n'
    (data / "deindexed.json").write_bytes(policy)
    return files, data, policy


def test_quiesced_scoped_backup_restores_all_book_facts_and_receipts(tmp_path):
    project = tmp_path / "project"
    files, data, policy = _book_project(project)
    expected_hashes = {path: hashlib.sha256(value).hexdigest() for path, value in files.items()}

    snapshot = snapshot_quiesced_book_project(project, data, tmp_path / "backup")
    assert {entry["path"] for entry in snapshot.entries} >= {
        "project/Project Files/Book_Layout.json",
        "project/Chapters/01/Originals/source.docx",
        "project/.cognita-storage/state.sqlite",
        "project/Audiobook/Chapters/01/native/take.pcm",
        "project/Audiobook/Builds/book.mp3",
        "project/backups/Chapters/01/chapter.before.docx",
        "data/deindexed.json",
    }

    restored = tmp_path / "restored"
    restored_data = tmp_path / "restored-data"
    restore_quiesced_book_project(snapshot.root, restored, restored_data)
    for relative, expected_hash in expected_hashes.items():
        assert hashlib.sha256((restored / relative).read_bytes()).hexdigest() == expected_hash
    assert (restored_data / "deindexed.json").read_bytes() == policy
    reopened = ProjectState(restored)
    assert reopened.receipt(
        owner_key="fixture-owner", project="fixture", tool="set_folder_indexing", operation_id="fixture-op",
    ) == ("a" * 64, {"policy_revision": 1, "status": "success"})
    assert reopened.folder_policy().rules == (("Chapters/01", False),)


def test_restore_refuses_manifest_tampering_before_activation(tmp_path):
    project = tmp_path / "project"
    _files, data, _policy = _book_project(project)
    snapshot = snapshot_quiesced_book_project(project, data, tmp_path / "backup")
    (snapshot.root / "project/Audiobook/Builds/book.mp3").write_bytes(b"tampered")

    restored = tmp_path / "restored"
    restored_data = tmp_path / "restored-data"
    with pytest.raises(BookBackupFixtureError, match="does not match"):
        restore_quiesced_book_project(snapshot.root, restored, restored_data)
    assert not restored.exists() and not restored_data.exists()
