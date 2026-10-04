"""Pure-logic tests for literals.py — the find_literal matcher, globs and scan.

The engine-level walk (index list + disk read + tiers + caps) is covered in
tests/test_engine_local.py, which needs a live store.
"""

import pytest

from cognita.literals import (
    FIND_LITERAL_TOOL_DEF,
    MAX_CONTEXT_LINES,
    BadPattern,
    build_matcher,
    glob_matches,
    looks_binary,
    normalize,
    scan_text,
)

SAMPLE = """# Grep Selftest

zzmarker_alpha appears here
and zzmarker_alpha appears twice on this line: zzmarker_alpha
ZZMARKER_ALPHA in caps
nothing on this line"""


def find(text, pattern, **kw):
    ctx = kw.pop("context_lines", 0)
    return list(scan_text(text, build_matcher(pattern, **kw), ctx))


# ---------------------------------------------------------------- matching


def test_literal_counts_every_occurrence_including_repeats_on_one_line():
    hits = find(SAMPLE, "zzmarker_alpha")
    assert len(hits) == 3  # line 3 once, line 4 twice
    assert [h["line_number"] for h in hits] == [3, 4, 4]


def test_multiple_matches_on_a_line_are_distinguished_by_column():
    line4 = [h for h in find(SAMPLE, "zzmarker_alpha") if h["line_number"] == 4]
    assert len({h["column"] for h in line4}) == 2


def test_line_and_column_are_both_one_indexed():
    (hit,) = find("xxNEEDLExx", "NEEDLE")
    assert hit["line_number"] == 1
    assert hit["column"] == 3  # 0-based offset 2


def test_case_insensitive_picks_up_the_caps_line():
    assert len(find(SAMPLE, "zzmarker_alpha", case_sensitive=False)) == 4


def test_case_sensitive_is_the_default():
    assert len(find(SAMPLE, "ZZMARKER_ALPHA")) == 1


def test_absent_pattern_yields_nothing_rather_than_raising():
    assert find(SAMPLE, "zzmarker_omega") == []


def test_literal_pattern_is_escaped_not_interpreted():
    # a regex metacharacter salad must match only itself in literal mode
    assert find("a.c and abc", "a.c") == [] or [h["match"] for h in find("a.c and abc", "a.c")] == ["a.c"]
    assert [h["column"] for h in find("a.c and abc", "a.c")] == [1]


def test_regex_mode_matches_alternation():
    hits = find(SAMPLE, "zzmarker_(alpha|beta)", regex=True)
    assert len(hits) == 3


def test_regex_anchors_apply_per_line():
    hits = find(SAMPLE, r"^nothing", regex=True)
    assert [h["line_number"] for h in hits] == [6]


def test_bad_regex_raises_bad_pattern_with_the_compile_message():
    with pytest.raises(BadPattern) as exc:
        build_matcher("[", regex=True)
    assert "Invalid regular expression" in str(exc.value)


def test_empty_pattern_is_refused():
    with pytest.raises(BadPattern):
        build_matcher("")


def test_zero_width_regex_matches_are_dropped():
    # 'a*' matches the empty string at every position; recording those would
    # emit one record per character in the corpus
    assert find("bbbb", "a*", regex=True) == []


def test_zero_width_guard_keeps_real_matches_of_the_same_pattern():
    assert [h["match"] for h in find("baab", "a*", regex=True)] == ["aa"]


# ---------------------------------------------------------------- lines/context


def test_line_numbering_matches_normalized_text_like_read_document():
    crlf = "one\r\nneedle\r\nthree"
    (hit,) = find(crlf, "needle")
    assert hit["line_number"] == 2
    assert hit["line"] == "needle"  # no stray \r
    assert normalize(crlf).split("\n")[1] == "needle"


def test_lone_cr_is_normalized_too():
    (hit,) = find("one\rneedle\rthree", "needle")
    assert hit["line_number"] == 2


def test_context_lines_are_empty_by_default():
    (hit,) = find(SAMPLE, "in caps")
    assert hit["context_before"] == [] and hit["context_after"] == []


def test_context_lines_bracket_the_match():
    (hit,) = find(SAMPLE, "in caps", context_lines=2)
    assert hit["context_before"] == [
        "zzmarker_alpha appears here",
        "and zzmarker_alpha appears twice on this line: zzmarker_alpha",
    ]
    assert hit["context_after"] == ["nothing on this line"]


def test_context_is_clamped_and_never_runs_off_either_end():
    (hit,) = find("solo needle", "needle", context_lines=99)
    assert hit["context_before"] == [] and hit["context_after"] == []
    (hit2,) = find(SAMPLE, "Selftest", context_lines=MAX_CONTEXT_LINES + 5)
    assert len(hit2["context_after"]) == MAX_CONTEXT_LINES


# ---------------------------------------------------------------- globs


@pytest.mark.parametrize(
    "rel,pattern,expected",
    [
        ("_shared/notes.md", "_shared/*.md", True),
        ("_shared/deep/notes.md", "_shared/*.md", False),
        ("Equestria/a/b/w.json", "Equestria/**/*.json", True),
        ("Equestria/w.json", "Equestria/**/*.json", True),  # ** spans zero segments
        ("Other/w.json", "Equestria/**/*.json", False),
        # No separator => basename match, so the obvious '*.py' is not a
        # silent top-level-only search.
        ("deep/nested/build.py", "*.py", True),
        ("build.py", "*.py", True),
        ("build.pyc", "*.py", False),
        ("_shared/build_worldbook.py", "build_*.py", True),
    ],
)
def test_glob_matching(rel, pattern, expected):
    assert glob_matches(rel, pattern) is expected


def test_malformed_glob_does_not_raise():
    assert glob_matches("a/b.md", "a/**b/*.md") is False


# ---------------------------------------------------------------- binary sniff


def test_binary_sniff_flags_null_bytes():
    assert looks_binary(b"\x89PNG\r\n\x1a\n\x00\x00\x00")


def test_binary_sniff_passes_utf8_text():
    assert not looks_binary("héllo — wörld\n".encode("utf-8"))


def test_binary_sniff_only_reads_the_head():
    from cognita.literals import BINARY_SNIFF_BYTES

    assert not looks_binary(b"a" * BINARY_SNIFF_BYTES + b"\x00")


# ---------------------------------------------------------------- tool def


def test_tool_def_declares_only_pattern_as_required():
    assert FIND_LITERAL_TOOL_DEF["inputSchema"]["required"] == ["pattern"]


def test_tool_def_advertises_every_documented_parameter():
    props = FIND_LITERAL_TOOL_DEF["inputSchema"]["properties"]
    assert set(props) == {
        "pattern", "regex", "case_sensitive", "filepath_glob",
        "category", "max_matches", "context_lines", "include_registered", "compact",
    }


def test_tool_def_defaults_to_including_registered_documents():
    # the tier with no other retrieval path — the default must not hide it
    assert FIND_LITERAL_TOOL_DEF["inputSchema"]["properties"]["include_registered"]["default"] is True


# ------------------------------------------- 5.1: a pattern spanning a newline


def test_multiline_literal_pattern_matches():
    """The silent miss: scan_text ran the matcher per LINE, so a pattern
    containing a newline could never match anything — and _find_literal turned
    that into reason "no_matches" with "this is an exhaustive search, so the
    answer is 'not present', not 'not found yet'". A confident zero for a string
    that is right there, in the one tool whose whole contract is that its zeroes
    can be trusted. It also defeated the motivating use case: sweeping for a
    stale multi-line block before a rename.
    """
    text = "alpha\nbeta\ngamma\n"
    hits = list(scan_text(text, build_matcher("alpha\nbeta")))
    assert len(hits) == 1
    assert hits[0]["line_number"] == 1
    assert hits[0]["column"] == 1
    assert hits[0]["match"] == "alpha\nbeta"


def test_multiline_regex_pattern_matches():
    text = "one\ntwo\nthree\n"
    hits = list(scan_text(text, build_matcher(r"two\nthree", regex=True)))
    assert [h["line_number"] for h in hits] == [2]


def test_anchors_still_apply_per_line():
    """Per-line scanning gave ^ and $ their per-line meaning for free, and the
    tool description promises it. Scanning the whole document has to ask for it
    (re.MULTILINE) or ^ silently becomes "start of file"."""
    text = "alpha\nbeta\ngamma\n"
    assert [h["line_number"] for h in scan_text(text, build_matcher("^beta", regex=True))] == [2]
    assert [h["line_number"] for h in scan_text(text, build_matcher("a$", regex=True))] == [1, 2, 3]


def test_context_after_follows_the_end_of_a_multiline_match():
    """Context must not repeat lines the match itself already spans."""
    text = "one\ntwo\nthree\nfour\n"
    hits = list(scan_text(text, build_matcher("one\ntwo"), context_lines=1))
    assert hits[0]["context_after"] == ["three"]
    assert hits[0]["context_before"] == []


def test_line_and_column_are_still_right_after_the_offset_rewrite():
    """The 1-indexed line/column contract find_literal shares with
    read_document, re-derived from match offsets instead of per-line state."""
    text = "aa\nbbXbb\ncc\n"
    hits = list(scan_text(text, build_matcher("X")))
    assert (hits[0]["line_number"], hits[0]["column"]) == (2, 3)
    assert hits[0]["line"] == "bbXbb"
