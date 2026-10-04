"""Disk-side stat + hash for the manifest (5.0 §4).

The manifest's whole reason to exist is that its hashes come from the FILE, not
from index state — a hash served out of the index cannot detect the one failure
it is there to detect, which is the index and the disk disagreeing. These tests
pin that the two hash definitions mean what the docs say they mean, because a
client will feed content_sha256 straight back as expected_sha256 and a
mismatched definition would reject every guarded write.
"""

import hashlib
import os
import pathlib
from datetime import datetime, timedelta, timezone

from cognita.editing import content_sha256
from cognita.manifest import bytes_sha256, file_facts, stat_drift, text_sha256


def test_bytes_sha256_matches_sha256sum(tmp_path):
    target = tmp_path / "f.md"
    target.write_bytes(b"a\r\nb\r\n")
    assert bytes_sha256(target) == hashlib.sha256(b"a\r\nb\r\n").hexdigest()


def test_text_sha256_matches_the_hash_the_write_guard_uses(tmp_path):
    """content_sha256 is what read_document stamps and expected_sha256 accepts.

    If the manifest computed anything else, every write guarded with a hash from
    a manifest would be rejected as stale on a file nobody had touched.
    """
    assert text_sha256(b"alpha\nbeta\n") == content_sha256("alpha\nbeta\n")


def test_text_sha256_folds_line_endings_but_bytes_sha256_does_not(tmp_path):
    lf, crlf = tmp_path / "lf.md", tmp_path / "crlf.md"
    lf.write_bytes(b"a\nb\n")
    crlf.write_bytes(b"a\r\nb\r\n")
    # Same text, different bytes: exactly the case where a naive byte comparison
    # reports a byte-exact document as a mismatch.
    assert text_sha256(lf.read_bytes()) == text_sha256(crlf.read_bytes())
    assert bytes_sha256(lf) != bytes_sha256(crlf)


def test_text_sha256_ignores_a_bom_the_same_way_the_read_path_does(tmp_path):
    assert text_sha256(b"\xef\xbb\xbfalpha\n") == text_sha256(b"alpha\n")


def test_text_sha256_is_none_for_non_text():
    assert text_sha256(b"\xff\xfe\x00\x01binary") is None


def test_the_two_hashes_agree_on_a_plain_lf_file(tmp_path):
    """The common case: no BOM, LF only. Documented as identical; pinned here so
    the documentation cannot quietly become false."""
    target = tmp_path / "f.md"
    target.write_bytes(b"alpha\nbeta\n")
    facts = file_facts(target)
    assert facts["content_sha256"] == facts["bytes_sha256"]


def test_file_facts_reports_stat_and_both_hashes(tmp_path):
    target = tmp_path / "f.md"
    target.write_bytes(b"alpha\n")
    facts = file_facts(target)
    assert facts["on_disk"] is True
    assert facts["size_bytes"] == 6
    assert facts["bytes_sha256"] == hashlib.sha256(b"alpha\n").hexdigest()
    assert facts["content_sha256"] == content_sha256("alpha\n")
    assert isinstance(facts["mtime_epoch"], float)


def test_file_facts_never_raises_on_a_missing_file(tmp_path):
    """A manifest that 500s because one of 319 files was mid-sync would be
    useless exactly when it is most needed."""
    facts = file_facts(tmp_path / "gone.md")
    assert facts["on_disk"] is False and "error" in facts


def test_stat_drift_is_false_when_the_index_matches_disk(tmp_path):
    target = tmp_path / "f.md"
    target.write_bytes(b"alpha\n")
    facts = file_facts(target)
    known_mtime = datetime.fromtimestamp(facts["mtime_epoch"], tz=timezone.utc)
    assert stat_drift(facts, facts["size_bytes"], known_mtime) is False


def test_stat_drift_is_true_when_size_or_mtime_moved(tmp_path):
    target = tmp_path / "f.md"
    target.write_bytes(b"alpha\n")
    facts = file_facts(target)
    known_mtime = datetime.fromtimestamp(facts["mtime_epoch"], tz=timezone.utc)
    assert stat_drift(facts, facts["size_bytes"] + 1, known_mtime) is True
    assert stat_drift(facts, facts["size_bytes"], known_mtime + timedelta(seconds=5)) is True


def test_a_missing_file_counts_as_drift(tmp_path):
    """The index is serving a document that is not there — that is drift, and
    reporting it as "in sync" would be the manifest lying."""
    facts = file_facts(tmp_path / "gone.md")
    assert stat_drift(facts, 10, datetime.now(tz=timezone.utc)) is True


def test_nothing_stored_is_not_evidence_of_drift(tmp_path):
    target = tmp_path / "f.md"
    target.write_bytes(b"alpha\n")
    assert stat_drift(file_facts(target), None, None) is False


# --------------------------------------------------------------- 6.1.1
# Drift is decided on CONTENT. The stat is a pre-filter, because a stat moves
# for reasons that have nothing to do with the bytes.


def test_a_sync_client_rewriting_the_mtime_is_not_drift(tmp_path):
    """The live false positive: `onedrive --monitor` stamps the local mtime to
    whole-second precision after uploading, so a file Cognita indexed at
    ...19.933396 stats at ...19.000000 with every byte identical. Reported
    index_drift:true beside three matching hashes on kei, twice."""
    target = tmp_path / "f.txt"
    target.write_bytes(b"alpha\n")
    facts = file_facts(target)
    indexed_mtime = datetime.fromtimestamp(facts["mtime_epoch"], tz=timezone.utc)
    indexed_hash = facts["content_sha256"]

    os.utime(target, (facts["mtime_epoch"], float(int(facts["mtime_epoch"]))))
    stamped = file_facts(target)

    # The stat alone still calls it drift — which is exactly the bug, so assert
    # it, or this test would pass on a build that never had the problem.
    assert stat_drift(stamped, facts["size_bytes"], indexed_mtime) is True
    assert stat_drift(stamped, facts["size_bytes"], indexed_mtime,
                      indexed_hash=indexed_hash) is False


def test_a_moved_stat_with_changed_bytes_is_still_drift(tmp_path):
    """Clearing a false positive must not clear a true one."""
    target = tmp_path / "f.txt"
    target.write_bytes(b"alpha\n")
    facts = file_facts(target)
    indexed_mtime = datetime.fromtimestamp(facts["mtime_epoch"], tz=timezone.utc)
    indexed_hash = facts["content_sha256"]

    target.write_bytes(b"alpha\nbeta\n")
    assert stat_drift(file_facts(target), facts["size_bytes"], indexed_mtime,
                      indexed_hash=indexed_hash) is True


def test_an_extraction_hash_settles_drift_in_both_directions(tmp_path):
    """What get_document has: the file re-parsed right now. It answers the
    question outright, so a stat that agrees cannot mask a real change and a
    stat that disagrees cannot invent one."""
    target = tmp_path / "f.pdf"
    target.write_bytes(b"%PDF-1.4 whatever\n")
    facts = file_facts(target)
    matching_mtime = datetime.fromtimestamp(facts["mtime_epoch"], tz=timezone.utc)

    assert stat_drift(facts, facts["size_bytes"], matching_mtime,
                      indexed_hash="abc", extracted_hash="abc") is False
    assert stat_drift(facts, facts["size_bytes"], matching_mtime,
                      indexed_hash="abc", extracted_hash="def") is True
    # A stat that disagrees loses to the extraction, not the other way round.
    assert stat_drift(facts, facts["size_bytes"] + 99, matching_mtime,
                      indexed_hash="abc", extracted_hash="abc") is False


def test_an_unmatchable_indexed_hash_falls_back_to_the_stat(tmp_path):
    """`content_hash` is the hash of the EXTRACTION, so for a PDF, a .docx or a
    .md with frontmatter it legitimately equals neither file hash. Disagreement
    therefore proves nothing and must not be read as drift on its own — the
    manifest keeps the stat's answer instead of inventing a better one."""
    target = tmp_path / "f.md"
    target.write_bytes(b"---\ntitle: x\n---\nbody\n")
    facts = file_facts(target)
    indexed_mtime = datetime.fromtimestamp(facts["mtime_epoch"], tz=timezone.utc)
    extraction_hash = content_sha256("body\n")  # what parse_file would store
    assert extraction_hash not in (facts["content_sha256"], facts["bytes_sha256"])

    assert stat_drift(facts, facts["size_bytes"], indexed_mtime,
                      indexed_hash=extraction_hash) is False
    assert stat_drift(facts, facts["size_bytes"], indexed_mtime + timedelta(seconds=5),
                      indexed_hash=extraction_hash) is True


def test_a_missing_file_is_drift_even_with_hashes_in_hand(tmp_path):
    facts = file_facts(tmp_path / "gone.md")
    assert stat_drift(facts, 10, datetime.now(tz=timezone.utc),
                      indexed_hash="abc", extracted_hash="abc") is True


# --------------------------------------------------------------- 5.0.1
# read_document normalizes line endings on purpose (its text has to be
# anchor-safe for edit_document). The defect was that it did not SAY so, and a
# client byte-comparing its text against a CRLF payload concluded a perfectly
# stored file was corrupt.


def test_line_ending_style_names_what_the_file_uses():
    from cognita.manifest import line_ending_style

    assert line_ending_style(b"a\nb\n") == "lf"
    assert line_ending_style(b"a\r\nb\r\n") == "crlf"
    assert line_ending_style(b"a\rb\r") == "cr"
    assert line_ending_style(b"a\r\nb\nc") == "mixed"
    assert line_ending_style(b"single line") == "none"


def test_crlf_is_not_miscounted_as_separate_cr_and_lf():
    """The naive count() pair reports 'mixed' for every CRLF file, which would
    make the flag useless exactly where it matters."""
    from cognita.manifest import line_ending_style

    assert line_ending_style(b"a\r\nb\r\nc\r\n") == "crlf"


def test_file_facts_can_skip_the_text_hash(tmp_path):
    """The delete paths want mtime + byte hash for ghost forensics and have no
    use for a hash whose only consumer is expected_sha256."""
    target = tmp_path / "f.md"
    target.write_bytes(b"alpha\n")
    facts = file_facts(target, hash_text=False)
    assert facts["bytes_sha256"] == hashlib.sha256(b"alpha\n").hexdigest()
    assert facts["content_sha256"] is None
    assert facts["size_bytes"] == 6 and facts["mtime_epoch"]


def test_file_facts_reads_the_file_once_when_it_wants_both_hashes(tmp_path, monkeypatch):
    """This runs per document on the manifest path. Hashing 338 files twice over
    would double the I/O of the one call whose entire purpose is to be cheap
    enough to make a sync diff a single request."""
    target = tmp_path / "f.md"
    target.write_bytes(b"alpha\n")

    opens = []
    real_open = pathlib.Path.open

    def counting_open(self, *a, **kw):
        if self == target:
            opens.append(1)
        return real_open(self, *a, **kw)

    monkeypatch.setattr(pathlib.Path, "open", counting_open)
    monkeypatch.setattr(pathlib.Path, "read_bytes",
                        lambda self: counting_open(self, "rb").read())
    facts = file_facts(target)
    assert facts["content_sha256"] and facts["bytes_sha256"]
    assert len(opens) == 1, f"read the file {len(opens)} times"
