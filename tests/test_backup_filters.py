"""list_backups prefix/since/until filters (5.0 §7.3).

copy_directory creates 17 files in one call and remove_directory deletes them in
one call, but recovering either was 17 restore_backup calls against 17 backup
ids you had to find first. A backup system drivable only one file at a time is
not an undo path for a bulk transform.

These filters are what make a bulk operation's backups enumerable as a SET: one
prefix, one time window, exactly the files that call touched.
"""

import pytest

from cognita.backups import list_backup_entries, normalize_stamp


@pytest.fixture
def tree(tmp_path):
    """A backups/ tree spanning two directories and two days."""
    root = tmp_path / "backups"
    for rel, stamps in {
        "pack": ["20260828-100000", "20260829-140000", "20260829-140001"],
        "pack/nested": ["20260829-140002"],
        "other": ["20260829-090000"],
    }.items():
        (root / rel).mkdir(parents=True, exist_ok=True)
        for i, stamp in enumerate(stamps):
            (root / rel / f"f{i}.{stamp}.md").write_text("x", encoding="utf-8")
    return tmp_path


def ids(entries):
    return sorted(e["backup_id"] for e in entries)


def test_no_filter_lists_everything(tree):
    assert len(list_backup_entries(tree)) == 5


def test_prefix_selects_one_directory_and_its_subtree(tree):
    entries = list_backup_entries(tree, prefix="pack/")
    assert ids(entries) == ["20260828-100000", "20260829-140000",
                            "20260829-140001", "20260829-140002"]
    # ...and only that directory: 'other' shares no prefix.
    assert all(e["filepath"].startswith("pack/") for e in entries)


def test_prefix_tolerates_a_leading_slash_or_dot(tree):
    assert len(list_backup_entries(tree, prefix="/pack/")) == 4
    assert len(list_backup_entries(tree, prefix="./pack/")) == 4


def test_since_and_until_bound_a_single_bulk_operation(tree):
    """The real use: one copy_directory wrote three backups inside two seconds."""
    entries = list_backup_entries(tree, prefix="pack",
                                  since="20260829-140000", until="20260829-140001")
    assert ids(entries) == ["20260829-140000", "20260829-140001"]


def test_a_bare_date_means_the_whole_day(tree):
    assert len(list_backup_entries(tree, since="20260829")) == 4
    assert len(list_backup_entries(tree, until="20260828")) == 1


def test_bounds_are_inclusive(tree):
    assert ids(list_backup_entries(tree, since="20260829-140001",
                                   until="20260829-140001")) == ["20260829-140001"]


def test_filters_compose_with_filepath(tree):
    entries = list_backup_entries(tree, "pack/f1.md", since="20260829")
    assert ids(entries) == ["20260829-140000"]


def test_an_unparseable_bound_is_ignored_not_silently_empty(tree):
    """A filter that quietly matched nothing would be the 2.2 failure in a new
    place: the caller believes it bounded a range and actually got an empty set
    for a reason it cannot see."""
    assert len(list_backup_entries(tree, since="last tuesday")) == 5


def test_normalize_stamp_shapes():
    assert normalize_stamp("20260829") == "20260829-000000"
    assert normalize_stamp("20260829", end=True) == "20260829-235959"
    assert normalize_stamp("20260829-120000") == "20260829-120000"
    assert normalize_stamp("2026-08-29 12:30:00") == "20260829-123000"
    assert normalize_stamp(None) is None
    assert normalize_stamp("nonsense") is None


def test_collision_suffixes_do_not_break_range_comparison(tmp_path):
    """Several writes in one second get -1/-2/-3 suffixes; the range filter
    compares the wall-clock stamp only, so a suffixed id still lands inside its
    own second's window."""
    root = tmp_path / "backups"
    root.mkdir()
    for suffix in ("", "-1", "-2"):
        (root / f"f.20260829-140000{suffix}.md").write_text("x", encoding="utf-8")
    entries = list_backup_entries(tmp_path, since="20260829-140000",
                                  until="20260829-140000")
    assert len(entries) == 3
