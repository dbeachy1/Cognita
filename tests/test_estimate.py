"""The §4 work estimator — DESIGN-6.0's up-front GPU decision.

Every test here is about a way the estimate can be wrong SILENTLY. That is the
whole risk profile of this module: it returns a number and a verdict, both of
which look perfectly reasonable when they are wrong, and the only symptom is
"the GPU feature doesn't seem to do much". Nothing raises, nothing logs, and no
existing test would fail.

The three exclusions in §4.3 each have that shape, and the `force` one inverts
the feature outright — an estimator that applies the mtime skip under a forced
rebuild returns near-zero for the single largest job Cognita ever performs and
routes it to the CPU.

§4.4's job scoping gets its own group at the end, because it is the defect the
design review caught by looking at CALLERS rather than at the rule: a per-file
decision is correct on every individual file and wrong in aggregate.
"""

from __future__ import annotations

import datetime
import os

import pytest

from cognita.estimate import (
    BINARY_FORMATS,
    advance_for,
    estimate_file_chunks,
    estimate_job,
)
from cognita.parsing import DEFAULT_POLICY


class Known:
    """A stored row, as `list_sources` returns them."""

    def __init__(self, mtime, size, tier="embedded"):
        self.file_mtime = mtime
        self.file_size = size
        self.tier = tier
        self.doc_id = "x"
        self.category = "general"


def write(tmp_path, name: str, size: int):
    p = tmp_path / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"x" * size)
    return p


def known_for(path, tmp_path):
    st = path.stat()
    return Known(
        datetime.datetime.fromtimestamp(st.st_mtime, datetime.timezone.utc),
        st.st_size,
    )


def run(files, tmp_path, **kw):
    kw.setdefault("chunk_size", 1000)
    kw.setdefault("chunk_overlap", 200)
    return estimate_job(files, tmp_path, **kw)


# --------------------------------------------------------------------------
# The arithmetic
# --------------------------------------------------------------------------


def test_advance_is_derived_from_config_not_hardcoded():
    """§4.1 says derive it. A project that retunes chunking must not silently
    keep estimating against 800."""
    assert advance_for(1000, 200) == 800
    assert advance_for(2000, 100) == 1900
    assert advance_for(500, 500) == 1, "advance must never be zero or negative"


def test_a_file_with_content_is_never_zero_chunks():
    """Integer division would call every file under `advance` bytes free, so a
    thousand small notes would estimate as no work at all."""
    assert estimate_file_chunks(0, 800) == 0
    assert estimate_file_chunks(1, 800) == 1
    assert estimate_file_chunks(799, 800) == 1
    assert estimate_file_chunks(8000, 800) == 10


def test_the_estimate_tracks_size(tmp_path):
    files = [write(tmp_path, "a.md", 8000), write(tmp_path, "b.md", 16000)]
    est = run(files, tmp_path)
    assert est.est_chunks == 10 + 20
    assert est.files == 2


# --------------------------------------------------------------------------
# 🔴 The three exclusions, each silent when wrong
# --------------------------------------------------------------------------


def test_unchanged_files_are_excluded(tmp_path):
    """The ordinary case: a reindex where nothing changed embeds zero chunks.

    Estimating before the skip decision would spin up workers for a walk that
    does no work at all — and most walks are this walk.
    """
    a = write(tmp_path, "a.md", 80000)
    b = write(tmp_path, "b.md", 80000)
    known = {"a.md": known_for(a, tmp_path), "b.md": known_for(b, tmp_path)}
    est = run([a, b], tmp_path, known=known)

    assert est.est_chunks == 0
    assert est.skipped_unchanged == 2
    assert est.decision == "cpu"


def test_a_changed_size_is_not_skipped(tmp_path):
    a = write(tmp_path, "a.md", 80000)
    stale = Known(known_for(a, tmp_path).file_mtime, 5)  # stored size disagrees
    est = run([a], tmp_path, known={"a.md": stale})

    assert est.skipped_unchanged == 0
    assert est.est_chunks == 100


def test_force_skips_nothing(tmp_path):
    """🔴 The inversion. force=True re-embeds EVERYTHING, so an estimator that
    applies the mtime skip returns near-zero for the largest job there is and
    sends a full rebuild to the CPU. Silent, and exactly backwards."""
    files = [write(tmp_path, f"d{i}.md", 80000) for i in range(10)]
    known = {f"d{i}.md": known_for(f, tmp_path) for i, f in enumerate(files)}

    unforced = run(files, tmp_path, known=known)
    forced = run(files, tmp_path, known=known, force=True)

    assert unforced.est_chunks == 0 and unforced.decision == "cpu"
    assert forced.est_chunks == 1000, "a forced rebuild must count every file"
    assert forced.skipped_unchanged == 0
    assert forced.decision == "gpu"


def test_deindexed_paths_are_excluded(tmp_path):
    """5.7: deindexed.json suppresses these from every walk, so counting them
    describes a different set of files than the one that will be embedded."""
    a = write(tmp_path, "keep.md", 80000)
    b = write(tmp_path, "dropped.md", 80000)
    est = run([a, b], tmp_path, suppressed={"dropped.md"})

    assert est.est_chunks == 100
    assert est.skipped_deindexed == 1
    assert est.files == 1


def test_registered_tier_files_contribute_nothing(tmp_path):
    """4.4: registered documents are stored whole and NEVER embedded, however
    large. A scripts-heavy project would otherwise estimate high and embed
    nothing — the same class of error as the other two, and not in §4.3's list."""
    md = write(tmp_path, "notes.md", 80000)
    py = write(tmp_path, "big_script.py", 800000)
    est = run([md, py], tmp_path, policy=DEFAULT_POLICY)

    assert est.skipped_registered == 1
    assert est.est_chunks == 100, "the .py file's 1000 chunks must not be counted"


# --------------------------------------------------------------------------
# §4.2 binary formats
# --------------------------------------------------------------------------


@pytest.mark.parametrize("suffix", sorted(BINARY_FORMATS))
def test_any_binary_format_is_sufficient_for_gpu(tmp_path, suffix):
    """§4.2: no yield factor is attempted, because there is no relationship
    between size and text volume. The costs are ~20:1 asymmetric, so the
    uncertain case resolves to GPU."""
    tiny = write(tmp_path, f"scan{suffix}", 2000)
    est = run([tiny], tmp_path)

    assert est.binary_formats == 1
    assert est.decision == "gpu", "a binary format alone must be enough"
    assert est.est_chunks == 0, "its size must not be used as a chunk estimate"


def test_binary_detection_is_case_insensitive(tmp_path):
    est = run([write(tmp_path, "REPORT.PDF", 2000)], tmp_path)
    assert est.binary_formats == 1


def test_a_deindexed_binary_file_does_not_force_the_gpu(tmp_path):
    """The exclusions run BEFORE the binary check, or a suppressed PDF would
    spin up a worker for a file the walk never touches."""
    est = run([write(tmp_path, "gone.pdf", 2000)], tmp_path, suppressed={"gone.pdf"})
    assert est.binary_formats == 0
    assert est.decision == "cpu"


def test_an_unchanged_binary_file_does_not_force_the_gpu(tmp_path):
    pdf = write(tmp_path, "manual.pdf", 200000)
    est = run([pdf], tmp_path, known={"manual.pdf": known_for(pdf, tmp_path)})
    assert est.binary_formats == 0
    assert est.decision == "cpu"


# --------------------------------------------------------------------------
# The threshold
# --------------------------------------------------------------------------


def test_small_work_stays_on_the_cpu(tmp_path):
    """A 3 KB note is ~4 chunks. It must never spin anything up — that was the
    only thing the rejected single-document special case was protecting."""
    est = run([write(tmp_path, "note.md", 3000)], tmp_path, threshold=50)
    assert est.est_chunks == 3
    assert est.decision == "cpu"


def test_one_large_document_still_uses_the_gpu(tmp_path):
    """🔴 §4.3: there is NO special case for a single-document write. An edit
    re-embeds the WHOLE file, so one line changed in a 5 MB manual is ~6,000
    chunks — the expensive case, which a "one document means small" rule would
    have sent to the slow path."""
    est = run([write(tmp_path, "manual.md", 5_000_000)], tmp_path, threshold=50)
    assert est.est_chunks == 6250
    assert est.decision == "gpu"


@pytest.mark.parametrize("size,expect", [(40 * 800, "cpu"), (51 * 800, "gpu")])
def test_the_threshold_is_the_boundary(tmp_path, size, expect):
    est = run([write(tmp_path, "f.md", size)], tmp_path, threshold=50)
    assert est.decision == expect


def test_the_threshold_is_configurable(tmp_path):
    f = write(tmp_path, "f.md", 80000)  # 100 chunks
    assert run([f], tmp_path, threshold=50).decision == "gpu"
    assert run([f], tmp_path, threshold=500).decision == "cpu"


# --------------------------------------------------------------------------
# 🔴 §4.4 — the estimate is over a JOB, not a file
# --------------------------------------------------------------------------


def test_many_small_files_aggregate_over_the_threshold(tmp_path):
    """The copy_directory defect, in one assertion.

    500 documents of a handful of chunks each: every per-file decision is
    correctly "cpu", and the aggregate is a large job. A per-file loop would
    make 500 correct decisions and never use the GPU once — on precisely the
    workload the feature exists for. Nothing is wrong with any decision; the
    shape of the loop is what loses.
    """
    files = [write(tmp_path, f"d{i:03d}.md", 4000) for i in range(500)]

    per_file = [run([f], tmp_path, threshold=50).decision for f in files]
    assert set(per_file) == {"cpu"}, "each file alone is genuinely small"

    as_one_job = run(files, tmp_path, threshold=50)
    assert as_one_job.est_chunks == 500 * 5
    assert as_one_job.decision == "gpu"


def test_a_job_of_one_is_just_a_job(tmp_path):
    """No branch for single documents: the same estimate applies to a set of
    one, which is what gets both ends right."""
    small = run([write(tmp_path, "note.md", 3000)], tmp_path, threshold=50)
    large = run([write(tmp_path, "book.md", 900000)], tmp_path, threshold=50)
    assert small.decision == "cpu"
    assert large.decision == "gpu"


def test_an_empty_job_is_cpu(tmp_path):
    est = run([], tmp_path)
    assert est.decision == "cpu"
    assert est.est_chunks == 0 and est.files == 0


# --------------------------------------------------------------------------
# Robustness — an estimate must never be the thing that breaks a walk
# --------------------------------------------------------------------------


def test_a_vanished_file_does_not_raise(tmp_path):
    """The walk stats files that may be gone by the time we look."""
    f = write(tmp_path, "gone.md", 8000)
    keep = write(tmp_path, "here.md", 8000)
    os.unlink(f)
    est = run([f, keep], tmp_path)
    assert est.est_chunks == 10


def test_a_file_outside_documents_dir_is_counted_not_fatal(tmp_path):
    outside = tmp_path.parent / "stray.md"
    outside.write_bytes(b"x" * 8000)
    try:
        est = run([outside], tmp_path)
        assert est.est_chunks == 10
    finally:
        outside.unlink()


def test_a_row_missing_stat_metadata_is_not_skipped(tmp_path):
    """An old row with no mtime/size cannot prove the file is unchanged, so the
    safe answer is to count it. Skipping on absent evidence would under-estimate."""
    f = write(tmp_path, "a.md", 80000)
    est = run([f], tmp_path, known={"a.md": Known(None, None)})
    assert est.skipped_unchanged == 0
    assert est.est_chunks == 100


def test_log_fields_carry_what_embed_plan_promises(tmp_path):
    """§14.1's line is assembled from these; a missing key is a silent gap in
    the only record of what the estimator decided."""
    est = run([write(tmp_path, "a.md", 80000)], tmp_path, threshold=50)
    fields = est.as_log_fields()
    assert set(fields) == {
        "files", "est_chunks", "threshold", "binary_formats", "skipped_unchanged",
    }
    assert fields["est_chunks"] == 100 and fields["threshold"] == 50
