from __future__ import annotations

import hashlib

import pytest

from cognita.books import docx as docx_module
from cognita.books import storage as storage_module
from cognita.books.docx import FileLockedError
from cognita.books.storage import ProjectFileError, list_project_files, read_project_file


def test_listing_paginates_deterministically_and_keeps_excluded_files_visible(tmp_path):
    (tmp_path / "source").mkdir()
    (tmp_path / "source" / "master.docx").write_bytes(b"master")
    (tmp_path / "other.txt").write_text("other", encoding="utf-8")

    first = list_project_files(
        tmp_path, "", limit=1, policy_revision=4,
        folder_rules=(("source", False),),
    )
    second = list_project_files(
        tmp_path, "", limit=1, cursor=first["next_cursor"], policy_revision=4,
        folder_rules=(("source", False),),
    )

    assert first["has_more"] and second["has_more"] is False
    assert first["entries"][0]["path"] == "other.txt"
    page = list_project_files(
        tmp_path, "source", folder_rules=(("source", False),),
    )
    entry = page["entries"][0]
    assert entry["path"] == "source/master.docx"
    assert entry["effective_indexed"] is False
    assert entry["exclusion_reason"] == "folder_rule:source"


def test_listing_cursor_rejects_changed_inventory_or_policy_revision(tmp_path):
    (tmp_path / "a.txt").write_text("a", encoding="utf-8")
    (tmp_path / "b.txt").write_text("b", encoding="utf-8")
    first = list_project_files(tmp_path, "", limit=1, policy_revision=0)
    (tmp_path / "c.txt").write_text("c", encoding="utf-8")

    with pytest.raises(ProjectFileError, match="changed"):
        list_project_files(tmp_path, "", limit=1, cursor=first["next_cursor"], policy_revision=0)
    with pytest.raises(ProjectFileError, match="changed"):
        list_project_files(tmp_path, "", limit=1, cursor=first["next_cursor"], policy_revision=1)


def test_read_project_file_returns_exact_bytes_and_checks_expected_hash(tmp_path):
    body = bytes(range(256)) * 3
    (tmp_path / "media.bin").write_bytes(body)
    digest = hashlib.sha256(body).hexdigest()
    result = read_project_file(
        tmp_path, "media.bin", offset=257, max_bytes=31, expected_bytes_sha256=digest,
    )

    import base64

    assert base64.b64decode(result["content_base64"]) == body[257:288]
    assert result["bytes_sha256"] == digest
    assert result["offset"] == 257
    assert result["next_offset"] == 288
    assert result["has_more"] is True

    with pytest.raises(ProjectFileError) as error:
        read_project_file(
            tmp_path, "media.bin", offset=1, expected_bytes_sha256="0" * 64,
        )
    assert error.value.reason == "stale_file"


def test_docx_lock_admission_precedes_reader_open_and_translates_external_lock(tmp_path, monkeypatch):
    path = tmp_path / "source.docx"
    path.write_bytes(b"stable DOCX fixture")
    opened = False
    original_open = storage_module.os.open

    def tracked_open(*args, **kwargs):
        nonlocal opened
        opened = True
        return original_open(*args, **kwargs)

    def admission(_path):
        assert opened is False

    monkeypatch.setattr(storage_module.os, "open", tracked_open)
    monkeypatch.setattr(docx_module, "require_unlocked", admission)
    result = read_project_file(tmp_path, "source.docx")
    assert result["bytes_sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()

    def externally_locked(locked_path):
        raise FileLockedError(locked_path)

    monkeypatch.setattr(docx_module, "require_unlocked", externally_locked)
    with pytest.raises(ProjectFileError) as failure:
        read_project_file(tmp_path, "source.docx")
    assert failure.value.reason == "file_locked"
    assert str(failure.value) == "project file is locked"


def test_nonzero_offset_requires_hash_and_path_cannot_escape_project(tmp_path):
    (tmp_path / "file.bin").write_bytes(b"abc")
    with pytest.raises(ProjectFileError, match="require"):
        read_project_file(tmp_path, "file.bin", offset=1)
    with pytest.raises(ProjectFileError):
        read_project_file(tmp_path, "../file.bin")


def test_listing_reports_symlink_without_following_and_exact_read_refuses_it(tmp_path):
    outside = tmp_path.parent / f"{tmp_path.name}-outside.bin"
    outside.write_bytes(b"private")
    try:
        (tmp_path / "link.bin").symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation is unavailable on this host")

    result = list_project_files(tmp_path, "")
    entry = result["entries"][0]
    assert entry["type"] == "symlink"
    assert entry["size_bytes"] is None
    assert entry["effective_read_only"] is True
    with pytest.raises(ProjectFileError) as error:
        read_project_file(tmp_path, "link.bin")
    assert error.value.reason == "permission_denied"
