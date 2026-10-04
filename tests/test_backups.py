import time
from pathlib import Path

import pytest

from cognita.backups import BackupError, backup_if_exists, resolve_target


def _docs(tmp_path: Path) -> Path:
    d = tmp_path / "Project Files"
    d.mkdir()
    return d


def test_new_file_no_backup(tmp_path):
    docs = _docs(tmp_path)
    assert backup_if_exists(docs, "brand-new.md") is None
    assert not (docs / "backups").exists()


def test_existing_file_is_backed_up_with_content(tmp_path):
    docs = _docs(tmp_path)
    (docs / "KEI-Todos.md").write_text("original content", encoding="utf-8")
    backup = backup_if_exists(docs, "KEI-Todos.md")
    assert backup is not None
    assert backup.parent == docs / "backups"
    assert backup.read_text(encoding="utf-8") == "original content"
    assert backup.name.startswith("KEI-Todos.")
    assert backup.suffix == ".md"


def test_subdirectory_structure_preserved(tmp_path):
    docs = _docs(tmp_path)
    (docs / "sub").mkdir()
    (docs / "sub" / "note.md").write_text("x", encoding="utf-8")
    backup = backup_if_exists(docs, "sub/note.md")
    assert backup.parent == docs / "backups" / "sub"


def test_path_escape_refused(tmp_path):
    docs = _docs(tmp_path)
    with pytest.raises(BackupError):
        backup_if_exists(docs, "../outside.md")


def test_backups_dir_itself_not_re_backed_up(tmp_path):
    docs = _docs(tmp_path)
    (docs / "backups").mkdir()
    (docs / "backups" / "old.md").write_text("x", encoding="utf-8")
    assert backup_if_exists(docs, "backups/old.md") is None


def test_two_edits_make_two_distinct_backups(tmp_path):
    docs = _docs(tmp_path)
    f = docs / "doc.md"
    f.write_text("v1", encoding="utf-8")
    b1 = backup_if_exists(docs, "doc.md")
    time.sleep(1.05)  # timestamps are per-second
    f.write_text("v2", encoding="utf-8")
    b2 = backup_if_exists(docs, "doc.md")
    assert b1 != b2
    assert b1.read_text() == "v1"
    assert b2.read_text() == "v2"


# ----------------------------------------- 5.1: shapes that are not ordinary files


@pytest.mark.parametrize("bad", [
    "notes.md:hidden",   # NTFS alternate data stream
    "sub/a.md:stream",
    "CON.md", "nul.txt", "con", "LPT1.md", "COM9.md",  # Windows devices
])
def test_resolve_target_refuses_streams_and_devices(tmp_path, bad):
    """Both shapes pass the containment check — they resolve INSIDE the tree —
    but neither is an ordinary file. An ADS holds content the indexer,
    list_documents and every backup cannot see; a reserved name opens a DEVICE
    instead of a file, and add_from_url can build one straight from a page
    <title>. Windows-only in effect, but this is the one path chokepoint every
    write tool shares."""
    assert resolve_target(tmp_path, bad) is None


@pytest.mark.parametrize("ok", ["contract.md", "console.md", "nullable.md", "comic.md"])
def test_resolve_target_does_not_over_refuse(tmp_path, ok):
    """The device check matches the stem exactly — a file whose name merely
    STARTS with a reserved word is an ordinary file."""
    assert resolve_target(tmp_path, ok) is not None
