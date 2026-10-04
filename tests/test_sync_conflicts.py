"""Cloud-sync conflict copies are never indexed (5.0 §10).

Cognita can watch a bidirectionally synced documents folder, so
a second writer exists that nothing in the tool surface acknowledged. When that
writer loses a race it does not fail — it writes a SECOND file beside the first.
Indexed, that copy is a silent wrong answer: a builder's `*.txt` glob picks up
both and the pack output changes with nobody having edited anything.

No filename rule is free of false positives (`git-merge-conflict.md` matches the
first glob), so the mitigation is not cleverness — it is that every skip is
logged and counted into get_index_stats. These tests pin both halves: the
exclusion, and the fact that it is never silent.
"""

import pytest

from cognita.parsing import (
    SYNC_CONFLICT_PATTERNS,
    is_sync_conflict,
    partition_sync_conflicts,
)


@pytest.mark.parametrize("name", [
    "probe-PC-conflict.txt",
    "notes-DESKTOP-conflict.md",
    "notes-LAPTOP-conflicted.md",
    "note (Editor's conflicted copy 2026-08-29).md",
    "note.sync-conflict-20260829-120000-ABCDEFG.md",
    "PROBE-PC-CONFLICT.TXT",  # case-insensitive
    # abraunegg onedrive, as found on kei 2026-09-28 (14.0.1)
    "two-kei-safeBackup-0001.md",
    "cognita-selftest-kei-safeBackup-0001.md",
    "cognita-selftest-kei-safeBackup-0001.20260802-001629.md",
])
def test_conflict_names_are_detected(name):
    assert is_sync_conflict(name) is True


@pytest.mark.parametrize("name", [
    "probe.txt",
    "build_pack.py",
    "conflict.md",          # one segment: not the <name>-<device>-conflict shape
    "resolution-notes.md",
    "krea2-fantasy.txt",
    "safeBackup-0001.md",           # no <name>-<device> in front
    "my-safeBackup-notes.md",       # no counter after it
])
def test_ordinary_names_are_not_flagged(name):
    assert is_sync_conflict(name) is False


def test_the_filter_can_be_switched_off_entirely():
    """The escape hatch for a corpus whose real filenames trip the globs.

    An empty list means "index everything"; None means "use the defaults". The
    distinction matters because a config that set [] must not silently fall back
    to the built-ins.
    """
    assert is_sync_conflict("probe-PC-conflict.txt", []) is False
    assert is_sync_conflict("probe-PC-conflict.txt", None) is True
    assert is_sync_conflict("probe-PC-conflict.txt", SYNC_CONFLICT_PATTERNS) is True


def test_partition_splits_a_walk_and_keeps_both_halves(tmp_path):
    files = [tmp_path / n for n in
             ("a.md", "a-PC-conflict.md", "b.txt", "b.sync-conflict-1-x.txt")]
    kept, conflicts = partition_sync_conflicts(files)
    assert [f.name for f in kept] == ["a.md", "b.txt"]
    # The conflicts are RETURNED, not dropped on the floor — the caller reports
    # them, which is what stops a false positive being invisible.
    assert [f.name for f in conflicts] == ["a-PC-conflict.md", "b.sync-conflict-1-x.txt"]


def test_partition_with_the_filter_off_keeps_everything(tmp_path):
    files = [tmp_path / "a-PC-conflict.md"]
    kept, conflicts = partition_sync_conflicts(files, [])
    assert kept == files and conflicts == []


def test_watcher_ignores_a_conflict_copy_landing_in_a_watched_tree(tmp_path):
    """The watcher is the FASTEST path from a sync collision to a corrupted
    corpus — it fires within the debounce window, long before anyone looks."""
    from cognita.parsing import DEFAULT_POLICY
    from cognita.watcher import _ProjectEventHandler

    class FakeManager:
        exclude_patterns = ["backups"]
        sync_conflict_patterns = None

        def __init__(self):
            self.marked = []

        def _mark(self, project, rel_path, event_type):
            self.marked.append(rel_path)

    class FakeEvent:
        is_directory = False
        event_type = "modified"

        def __init__(self, path):
            self.src_path = str(path)
            self.dest_path = None

    docs = tmp_path / "docs"
    docs.mkdir()
    manager = FakeManager()
    handler = _ProjectEventHandler(manager, "P", docs.resolve(), DEFAULT_POLICY)

    good = docs / "note.md"
    good.write_text("x", encoding="utf-8")
    handler.on_any_event(FakeEvent(good))
    assert manager.marked == ["note.md"]

    conflict = docs / "note-PC-conflict.md"
    conflict.write_text("x", encoding="utf-8")
    handler.on_any_event(FakeEvent(conflict))
    assert manager.marked == ["note.md"]  # unchanged: the conflict never queued
