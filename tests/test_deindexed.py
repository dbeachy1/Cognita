"""The durable de-index list (5.7, deindexed.py).

These run everywhere — no PostgreSQL — because the list is the half of the fix
that has to survive a restart, and a suite that only proves that on kei proves it
too late. The engine-level behavior (remove_document's payloads, the walk
skipping suppressed paths) is in test_engine_local.py behind the `pg` gate.
"""

import json

import pytest

from cognita.deindexed import FILENAME, DeindexedPaths


def make(tmp_path) -> DeindexedPaths:
    return DeindexedPaths(tmp_path / "data" / FILENAME)


def test_a_missing_file_is_an_empty_list_not_an_error(tmp_path):
    listing = make(tmp_path)
    assert listing.paths() == set()
    assert listing.sorted() == []
    assert listing.load_error is None
    # Reading must not create the file: a project that never de-indexed anything
    # should leave no trace in its data dir.
    assert not (tmp_path / "data" / FILENAME).exists()


def test_add_persists_and_a_fresh_instance_sees_it(tmp_path):
    """The whole point. A list that lived only in memory would revert on the next
    restart, which is the self-reverting bug this module replaced."""
    first = make(tmp_path)
    assert first.add("notes/rocm.md") is True
    assert "notes/rocm.md" in first

    reopened = make(tmp_path)
    assert reopened.paths() == {"notes/rocm.md"}
    assert "notes/rocm.md" in reopened


def test_add_is_idempotent_and_reports_which_it_was(tmp_path):
    listing = make(tmp_path)
    assert listing.add("a.md") is True
    assert listing.add("a.md") is False  # already listed
    assert listing.sorted() == ["a.md"]


def test_discard_persists_and_reports_whether_it_removed_anything(tmp_path):
    listing = make(tmp_path)
    listing.add("a.md")
    listing.add("b.md")
    assert listing.discard("a.md") is True
    assert listing.discard("a.md") is False
    assert make(tmp_path).sorted() == ["b.md"]


def test_the_data_dir_is_created_on_demand(tmp_path):
    """A project can be registered before anything has written to its data_dir."""
    listing = DeindexedPaths(tmp_path / "never" / "made" / FILENAME)
    listing.add("x.md")
    assert (tmp_path / "never" / "made" / FILENAME).is_file()


def test_the_file_is_readable_json_with_a_version(tmp_path):
    """Recovery is a text editor. A format nobody can read by eye would make the
    one piece of non-derivable state here the hardest thing in the system to fix
    by hand."""
    listing = make(tmp_path)
    listing.add("b.md")
    listing.add("a.md")
    raw = json.loads((tmp_path / "data" / FILENAME).read_text(encoding="utf-8"))
    assert raw["version"] == 1
    assert raw["paths"] == ["a.md", "b.md"]  # sorted, so diffs stay small


@pytest.mark.parametrize("corrupt", ["{not json", '{"paths": "a.md"}', "[1, 2"])
def test_an_unreadable_list_reads_as_empty_and_says_so(tmp_path, corrupt):
    """Losing the list is not data loss — files simply get indexed again — but it
    silently reverses an explicit decision, which is exactly the failure this
    module exists to remove. So it is reported, and get_index_stats carries the
    string through to the caller."""
    path = tmp_path / "data" / FILENAME
    path.parent.mkdir(parents=True)
    path.write_text(corrupt, encoding="utf-8")

    listing = DeindexedPaths(path)
    assert listing.paths() == set()
    assert listing.load_error is not None and str(path) in listing.load_error


def test_an_unreadable_list_is_not_rewritten_on_read(tmp_path):
    """Overwriting it would destroy the only copy of the decisions it holds, and
    a transient read failure would make that permanent."""
    path = tmp_path / "data" / FILENAME
    path.parent.mkdir(parents=True)
    path.write_text("{not json", encoding="utf-8")

    DeindexedPaths(path).paths()
    assert path.read_text(encoding="utf-8") == "{not json"


def test_a_bare_list_is_accepted(tmp_path):
    """Forward tolerance in the direction that costs nothing: a hand-edited file
    holding just the array still loads, rather than reading as corrupt and
    silently un-suppressing everything in it."""
    path = tmp_path / "data" / FILENAME
    path.parent.mkdir(parents=True)
    path.write_text('["a.md", "b.md"]', encoding="utf-8")
    assert DeindexedPaths(path).paths() == {"a.md", "b.md"}


def test_no_temp_file_is_left_behind(tmp_path):
    """The write is temp-then-replace, so a half-written list can never be read —
    _load treats unparseable as empty, i.e. every suppression undone."""
    listing = make(tmp_path)
    listing.add("a.md")
    listing.discard("a.md")
    listing.add("b.md")
    assert sorted(p.name for p in (tmp_path / "data").iterdir()) == [FILENAME]
