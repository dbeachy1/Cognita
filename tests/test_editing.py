"""Pure match/splice logic for edit_document (DESIGN-2.0-edit-document.md §7 #1-12)
and the atomic batch tool (2.1)."""

import pytest

from cognita.editing import BATCH_MAX_EDITS, EditReject, apply_batch, apply_edit

DOC = """# Title

## Section One
alpha line
beta line

## Section Two
gamma line
delta line
"""


def reject(text, old, new, **kw) -> EditReject:
    with pytest.raises(EditReject) as ei:
        apply_edit(text, old, new, **kw)
    return ei.value


# 1. exact single match
def test_exact_single_match_replaced():
    out = apply_edit(DOC, "beta line", "BETA LINE")
    assert out.replacements == 1
    assert out.match_mode == "exact"
    assert "BETA LINE" in out.new_content
    assert "beta line" not in out.new_content
    assert "alpha line" in out.new_content  # untouched text intact


# 2. zero matches -> not_found with a useful hint
def test_not_found_with_near_miss_hint():
    exc = reject(DOC, "beta lin3", "x")
    assert exc.payload["reason"] == "not_found"
    assert exc.payload["replacements"] == 0
    assert "beta line" in exc.payload.get("hint", "")  # actual file bytes for retry


# 3. multiple matches -> ambiguous with line numbers
def test_ambiguous_lists_match_lines():
    exc = reject(DOC, "line", "row")
    assert exc.payload["reason"] == "ambiguous"
    assert exc.payload["match_count"] == 4
    lines = [m["line"] for m in exc.payload["matches"]]
    assert lines == sorted(lines) and len(lines) == 4


# 4. replace_all over N occurrences
def test_replace_all():
    out = apply_edit(DOC, "line", "row", replace_all=True)
    assert out.replacements == 4
    assert "line" not in out.new_content
    assert out.new_content.count("row") == 4


# 5. CRLF file + \n anchor -> newline_normalized; output stays \n-flavored
def test_crlf_file_with_lf_anchor():
    crlf_doc = DOC.replace("\n", "\r\n")
    out = apply_edit(crlf_doc, "alpha line\nbeta line", "single line")
    assert out.match_mode == "newline_normalized"
    assert out.replacements == 1
    # spliced content is \n-normalized (engine text-mode write owns platform EOL)
    assert "\r" not in out.new_content
    assert "single line" in out.new_content


def test_crlf_anchor_against_lf_file():
    out = apply_edit(DOC, "alpha line\r\nbeta line", "single line")
    assert out.match_mode == "newline_normalized"
    assert "single line" in out.new_content


# 6. multi-line (whole-section) anchor — the section-level reliability bar
def test_whole_section_anchor():
    old = "## Section One\nalpha line\nbeta line"
    new = "## Section One (rewritten)\nnew content here"
    out = apply_edit(DOC, old, new)
    assert out.replacements == 1
    assert "## Section One (rewritten)" in out.new_content
    assert "## Section Two" in out.new_content  # neighbors untouched


# 7. empty new_str = deletion
def test_empty_new_str_deletes():
    out = apply_edit(DOC, "alpha line\n", "")
    assert "alpha line" not in out.new_content
    assert "beta line" in out.new_content


# 8. regex/markdown metacharacters are literal
def test_metacharacters_literal():
    doc = "value is [a-z]+ and *bold* and (paren)\n"
    out = apply_edit(doc, "[a-z]+ and *bold*", "PLAIN")
    assert out.new_content == "value is PLAIN and (paren)\n"


# 9. unicode / em-dash anchors match raw code points
def test_unicode_em_dash_anchor():
    doc = "KEI — the Debian AI box — uses ROCm\n"
    out = apply_edit(doc, "— the Debian AI box —", "— rebuilt —")
    assert out.new_content == "KEI — rebuilt — uses ROCm\n"


# 10. edit that would empty the file
def test_would_empty_file_rejected():
    exc = reject("only content\n", "only content\n", "")
    assert exc.payload["reason"] == "would_empty_file"


# 11. no-op guards
def test_identical_old_new_rejected():
    assert reject(DOC, "beta line", "beta line").payload["reason"] == "no_change"


def test_eol_only_difference_rejected():
    assert reject(DOC, "alpha line\nbeta line", "alpha line\r\nbeta line").payload[
        "reason"
    ] == "no_change"


def test_empty_old_str_rejected():
    assert reject(DOC, "", "x").payload["reason"] == "invalid"


# 12. context_diff shape + caps
def test_context_diff_marks_changed_lines():
    out = apply_edit(DOC, "beta line", "BETA LINE")
    assert "@@ line" in out.context_diff
    assert "> BETA LINE" in out.context_diff
    assert "  alpha line" in out.context_diff  # context, unmarked


def test_context_diff_capped_on_replace_all():
    big = "word here\n" * 300
    out = apply_edit(big, "word", "term", replace_all=True)
    assert out.replacements == 300
    assert len(out.context_diff) < 3000
    assert "truncated" in out.context_diff


# ---------------------------------------------------------------- batch (2.1)


def test_batch_ordered_application():
    """Edit 2's anchor only exists AFTER edit 1 is applied (spec semantics)."""
    out = apply_batch(DOC, [
        {"old_str": "## Section One", "new_str": "## Section Uno"},
        {"old_str": "## Section Uno\nalpha line", "new_str": "## Section Uno\nALPHA"},
    ])
    assert [e["replacements"] for e in out.edits] == [1, 1]
    assert [e["index"] for e in out.edits] == [0, 1]
    assert "## Section Uno\nALPHA" in out.new_content
    assert "## Section Two" in out.new_content  # untouched


def test_batch_abort_names_failing_index_and_writes_nothing():
    with pytest.raises(EditReject) as ei:
        apply_batch(DOC, [
            {"old_str": "alpha line", "new_str": "ALPHA"},   # would succeed
            {"old_str": "does not exist", "new_str": "x"},   # aborts everything
        ])
    p = ei.value.payload
    assert p["reason"] == "batch_aborted"
    assert p["failed_edit"] == 1
    assert p["failed_reason"] == "not_found"
    assert p["edits_applied"] == 0  # explicit: file untouched
    assert "NOTHING was written" in p["message"]


def test_batch_abort_carries_inner_hint_fields():
    with pytest.raises(EditReject) as ei:
        apply_batch(DOC, [{"old_str": "line", "new_str": "row"}])  # ambiguous (4×)
    p = ei.value.payload
    assert p["failed_reason"] == "ambiguous"
    assert p["match_count"] == 4
    assert "matches" in p and "hint" in p


def test_batch_per_item_replace_all():
    out = apply_batch(DOC, [
        {"old_str": "line", "new_str": "row", "replace_all": True},
        {"old_str": "# Title", "new_str": "# Batch Title"},
    ])
    assert [e["replacements"] for e in out.edits] == [4, 1]
    assert "line" not in out.new_content
    assert "# Batch Title" in out.new_content


def test_batch_unified_diff_shape():
    out = apply_batch(DOC, [
        {"old_str": "alpha line", "new_str": "ALPHA"},
        {"old_str": "delta line", "new_str": "DELTA"},
    ])
    d = out.context_diff
    assert d.startswith("--- before")
    assert "+++ after" in d and "@@" in d
    assert "-alpha line" in d and "+ALPHA" in d
    assert "-delta line" in d and "+DELTA" in d


def test_batch_validation():
    for bad in ([], "not a list", None):
        with pytest.raises(EditReject) as ei:
            apply_batch(DOC, bad)
        assert ei.value.payload["reason"] == "invalid"
    with pytest.raises(EditReject) as ei:
        apply_batch(DOC, [{"old_str": "x"}])  # missing new_str
    assert ei.value.payload["failed_edit"] == 0
    with pytest.raises(EditReject) as ei:
        apply_batch(DOC, [{"old_str": "a", "new_str": "b"}] * (BATCH_MAX_EDITS + 1))
    assert "Too many" in ei.value.payload["message"]
