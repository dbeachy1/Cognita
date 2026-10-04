"""list_backups / restore_backup plumbing (2.4): parsing, sorting, lookup."""

import pytest

from cognita.backups import BackupError, backup_if_exists, find_backup, list_backup_entries


@pytest.fixture
def docs(tmp_path):
    d = tmp_path / "docs"
    (d / "sub").mkdir(parents=True)
    (d / "note.md").write_text("current note\n", encoding="utf-8")
    (d / "sub" / "deep.md").write_text("current deep\n", encoding="utf-8")
    # manufactured backups (older + newer), a sibling doc's backup, and junk
    b = d / "backups"
    (b / "sub").mkdir(parents=True)
    (b / "note.20260101-120000.md").write_text("old note v1\n", encoding="utf-8")
    (b / "note.20260615-093000.md").write_text("old note v2\n", encoding="utf-8")
    (b / "sub" / "deep.20260501-000000.md").write_text("old deep\n", encoding="utf-8")
    (b / "README.txt").write_text("junk, not a backup\n", encoding="utf-8")
    (b / "note.badstamp.md").write_text("junk, malformed stamp\n", encoding="utf-8")
    return d


def test_list_for_one_file_newest_first(docs):
    entries = list_backup_entries(docs, "note.md")
    assert [e["backup_id"] for e in entries] == ["20260615-093000", "20260101-120000"]
    e = entries[0]
    assert e["filepath"] == "note.md"
    assert e["created"] == "2026-06-15 09:30:00"
    assert e["size_bytes"] and e["size_bytes"] >= len("old note v2")  # EOL flavor varies


def test_list_subdir_file(docs):
    entries = list_backup_entries(docs, "sub/deep.md")
    assert len(entries) == 1
    assert entries[0]["filepath"] == "sub/deep.md"
    assert entries[0]["backup_id"] == "20260501-000000"


def test_list_all_ignores_junk(docs):
    entries = list_backup_entries(docs)
    assert len(entries) == 3  # junk files skipped
    assert {e["filepath"] for e in entries} == {"note.md", "sub/deep.md"}
    ids = [e["backup_id"] for e in entries]
    assert ids == sorted(ids, reverse=True)


def test_list_empty_when_no_backups_dir(tmp_path):
    d = tmp_path / "docs"
    d.mkdir()
    assert list_backup_entries(d) == []


def test_list_path_escape_rejected(docs):
    with pytest.raises(BackupError):
        list_backup_entries(docs, "../outside.md")


def test_find_backup(docs):
    p = find_backup(docs, "note.md", "20260101-120000")
    assert p is not None and p.read_text(encoding="utf-8") == "old note v1\n"
    assert find_backup(docs, "note.md", "20990101-000000") is None  # absent
    assert find_backup(docs, "note.md", "not-a-stamp") is None      # malformed id
    assert find_backup(docs, "../evil.md", "20260101-120000") is None  # escape


def test_glob_metacharacters_in_filename(docs):
    """'note [draft].md': the old glob-based listing turned [draft] into a
    character class and made the file's backups invisible (2.10.4 fix)."""
    (docs / "note [draft].md").write_text("bracketed\n", encoding="utf-8")
    made = backup_if_exists(docs, "note [draft].md")
    entries = list_backup_entries(docs, "note [draft].md")
    assert len(entries) == 1
    assert entries[0]["filepath"] == "note [draft].md"
    assert find_backup(docs, "note [draft].md", entries[0]["backup_id"]) == made
    # pruning path uses the same listing — must see them too
    backup_if_exists(docs, "note [draft].md", keep=1)
    assert len(list_backup_entries(docs, "note [draft].md")) == 1


def test_extensionless_document_backups_listable(docs):
    """'README' (no suffix): the timestamp becomes the backup's path-suffix,
    so stem-based parsing missed it entirely (2.10.4 fix)."""
    (docs / "README").write_text("no extension\n", encoding="utf-8")
    made = backup_if_exists(docs, "README")
    entries = list_backup_entries(docs, "README")
    assert len(entries) == 1
    assert entries[0]["filepath"] == "README"
    assert find_backup(docs, "README", entries[0]["backup_id"]) == made


def test_real_backup_roundtrips_into_listing(docs):
    """A backup created by backup_if_exists is discoverable by list/find."""
    made = backup_if_exists(docs, "note.md")
    entries = list_backup_entries(docs, "note.md")
    assert len(entries) == 3
    newest = entries[0]
    assert find_backup(docs, "note.md", newest["backup_id"]) == made
    assert made.read_text(encoding="utf-8") == "current note\n"


def test_retention_prunes_oldest_beyond_keep(docs, caplog):
    """keep=N: after a new backup, only the newest N remain; deletions logged."""
    import logging

    with caplog.at_level(logging.INFO, logger="cognita.backups"):
        backup_if_exists(docs, "note.md", keep=2)
    entries = list_backup_entries(docs, "note.md")
    assert len(entries) == 2  # fixture's two + new one, pruned back to 2
    ids = [e["backup_id"] for e in entries]
    assert "20260101-120000" not in ids  # the oldest fell off
    assert ids[0] > ids[1]  # newest first
    assert any("Pruned backup" in r.message for r in caplog.records)  # said so in the log


def test_retention_zero_means_unlimited(docs):
    backup_if_exists(docs, "note.md", keep=0)
    assert len(list_backup_entries(docs, "note.md")) == 3
    backup_if_exists(docs, "note.md")  # default None: unlimited too
    assert len(list_backup_entries(docs, "note.md")) == 4


def test_retention_scoped_per_file(docs):
    """Pruning note.md must never touch sub/deep.md's backups."""
    backup_if_exists(docs, "note.md", keep=1)
    assert len(list_backup_entries(docs, "note.md")) == 1
    assert len(list_backup_entries(docs, "sub/deep.md")) == 1  # untouched


def test_same_second_backups_never_overwrite(docs):
    """Two writes within one second must yield TWO backups (-N suffix), not a
    silent overwrite — the lost-generation bug caught by the 2.5 live smoke."""
    first = backup_if_exists(docs, "note.md")
    (docs / "note.md").write_text("changed between writes\n", encoding="utf-8")
    second = backup_if_exists(docs, "note.md")
    assert first != second
    assert first.read_text(encoding="utf-8") == "current note\n"
    assert second.read_text(encoding="utf-8") == "changed between writes\n"
    # both discoverable, suffixed id sorts newer, find resolves it
    entries = list_backup_entries(docs, "note.md")
    ids = [e["backup_id"] for e in entries]
    assert len(entries) == 4 and ids == sorted(ids, reverse=True)
    assert find_backup(docs, "note.md", ids[0]) == second
