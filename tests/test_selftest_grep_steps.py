"""The self-test plan's pinned find_literal counts (4.5, steps 27-30).

Same job as test_selftest_hashes.py does for the write path. Steps 27-30 assert
EXACT match counts — 3, 4, 0, 3, 1, 1, 2 — and an exhaustiveness tool whose
self-test promises the wrong number is worse than one with no self-test at all:
the run reports PASS/FAIL against a fiction, and a real off-by-one hides inside
it.

Nothing else checks those numbers. The plan text and the matcher could drift
apart silently, so this recomputes every count from the plan's OWN content
constants using the same pure functions the server runs, and asserts the
numbers still appear in the rendered prose.

If it fails: either you changed the fixture content or the step wording (update
both sides consciously, in the same commit), or you changed matcher behavior —
in which case the live self-test would have failed too, and it is a regression
until proven otherwise.
"""

import pytest

from cognita.editing import apply_edit
from cognita.literals import BadPattern, build_matcher, scan_text
from cognita.selftest import (
    _GREP_CONTENT,
    _GREP_REPLACEMENT,
    _GREP_SCRIPT_CONTENT,
    GREP_CATEGORY,
    GREP_FILE,
    GREP_MARKER,
    GREP_MARKER_V2,
    GREP_REGISTERED_MARKER,
    GREP_SCRIPT,
    build_self_test_plan,
)


def find(text: str, pattern: str, **kw) -> list[dict]:
    return list(scan_text(text, build_matcher(pattern, **kw)))


# The engine strips content on add_document, so the plan's line numbers are
# numbered off the stripped text — exactly what step 27 asserts.
DOC = _GREP_CONTENT.strip()
SCRIPT = _GREP_SCRIPT_CONTENT.strip()
REPLACEMENT = _GREP_REPLACEMENT.strip()


def test_step27_exact_count_and_lines():
    hits = find(DOC, GREP_MARKER)
    assert len(hits) == 3, "plan promises EXACTLY 3"
    assert [h["line_number"] for h in hits] == [3, 4, 4]


def test_step27_line_4_matches_have_distinct_columns():
    """The plan calls a count of 2 here a collapse bug; this is what makes the
    two line-4 hits separately addressable."""
    cols = [h["column"] for h in find(DOC, GREP_MARKER) if h["line_number"] == 4]
    assert len(cols) == 2 and cols[0] != cols[1]


def test_step27_line_numbers_address_the_real_lines():
    lines = DOC.split("\n")
    for h in find(DOC, GREP_MARKER):
        assert lines[h["line_number"] - 1] == h["line"]
        assert h["line"][h["column"] - 1:].startswith(GREP_MARKER)


def test_step28_case_insensitive_count():
    assert len(find(DOC, GREP_MARKER, case_sensitive=False)) == 4  # + the caps line


def test_step28_absent_marker_finds_nothing():
    assert find(DOC, "zzmarker_omega") == []


def test_step29_regex_alternation_matches_the_same_three():
    assert len(find(DOC, "zzmarker_(alpha|beta)", regex=True)) == 3


def test_step29_bad_pattern_is_rejected_at_compile():
    with pytest.raises(BadPattern):
        build_matcher("[", regex=True)


def test_step29_registered_fixture_holds_exactly_one_marker():
    assert len(find(SCRIPT, GREP_REGISTERED_MARKER)) == 1


def test_step29_registered_fixture_routes_to_the_registered_tier():
    """The step is meaningless unless the fixture is actually a registered
    file — a .py path is what selects the tier."""
    from pathlib import PurePosixPath

    from cognita.parsing import TIER_REGISTERED, ExtensionPolicy

    policy = ExtensionPolicy.build([".md"], [".py"])
    assert GREP_SCRIPT.endswith(".py")
    assert policy.tier_for(PurePosixPath(GREP_SCRIPT).suffix) == TIER_REGISTERED
    # and the .md fixture must NOT be registered, or step 27 has no chunks
    assert policy.tier_for(PurePosixPath(GREP_FILE).suffix) != TIER_REGISTERED


def test_step30_edit_anchor_is_unambiguous():
    """'zzmarker_alpha appears' also occurs on line 4; the plan's anchor has to
    be the longer 'appears here' or the live edit is rejected as ambiguous."""
    edited = apply_edit(DOC, f"{GREP_MARKER} appears here",
                        "zzmarker_beta appears here").new_content
    assert edited.split("\n")[2] == "zzmarker_beta appears here"


def test_step30_counts_after_the_edit():
    edited = apply_edit(DOC, f"{GREP_MARKER} appears here",
                        "zzmarker_beta appears here").new_content.strip()
    beta = find(edited, "zzmarker_beta")
    assert len(beta) == 1 and beta[0]["line_number"] == 3
    alpha = find(edited, GREP_MARKER)
    assert len(alpha) == 2
    assert {h["line_number"] for h in alpha} == {4}


def test_plan_prose_still_states_these_numbers():
    """Pins the wording to the arithmetic above, so a hand-edit of either one
    without the other fails here instead of during a live run."""
    plan = build_self_test_plan("x.y.z", readonly=False)
    for fragment in (
        "total_matches EXACTLY 3",
        "total_matches 4",
        "total_matches 0",
        "zzmarker_(alpha|beta)",
        "reason='bad_pattern'",
        "total_matches EXACTLY 1",
        "EXACTLY 2",
        GREP_FILE,
        GREP_SCRIPT,
        GREP_REGISTERED_MARKER,
        GREP_CATEGORY,
        GREP_MARKER_V2,
    ):
        assert fragment in plan, f"plan text no longer states {fragment!r}"


def test_plan_warns_that_a_blocked_approval_is_not_a_failure():
    """A live 4.5.0 run reported step 27 as BLOCKED on 'No approval received'
    — claude.ai's tool-approval prompt for a first-time tool, which TIMED OUT
    unanswered. The string reads like an auth error and is a known source of
    misdiagnosis, so the plan has to name it and say what to do."""
    plan = build_self_test_plan("x.y.z", readonly=False)
    assert "No approval received" in plan
    assert "BLOCKED (not FAIL)" in plan
    assert "Allow always" in plan  # or every later step prompts again
    assert "Search and tools" in plan  # the per-tool off switch, if no prompt shows


def test_plan_tells_an_aborted_run_to_clean_up_after_itself():
    """That same run stranded the step-27 fixture in a real knowledge base."""
    plan = build_self_test_plan("x.y.z", readonly=False)
    assert "IF YOU STOP EARLY, CLEAN UP" in plan
    assert "remove_document" in plan
    assert "remove_asset" in plan


def test_step31_category_is_not_the_default():
    """A canary that files everything under 'general' could not have caught the
    4.4.1 category-reset bug, and cannot catch a recurrence."""
    assert GREP_CATEGORY != "general"


def test_step32_replacement_holds_exactly_two_markers_on_one_line():
    hits = find(REPLACEMENT, GREP_MARKER_V2)
    assert len(hits) == 2, "plan promises EXACTLY 2"
    assert {h["line_number"] for h in hits} == {3}
    assert hits[0]["column"] != hits[1]["column"]


def test_step32_replacement_erases_the_earlier_markers():
    """update_document is a FULL replacement; the plan asserts 0 for the old
    markers, which is only true if the fixture genuinely drops them."""
    for gone in (GREP_MARKER, "zzmarker_beta"):
        assert find(REPLACEMENT, gone) == []


def test_step33_eval_query_token_is_present_in_the_replacement():
    """evaluate_retrieval expects found_at_rank 1; that is only a fair
    expectation if the queried token is actually in the document."""
    assert GREP_MARKER_V2 in REPLACEMENT


def test_grep_steps_do_not_touch_the_hash_chain_files():
    """Steps 27-30 must stay isolated: the hash-chain file is byte-pinned, and
    a stray marker in it would break the canary for the whole write path."""
    from cognita.selftest import _TEST_CONTENT, _TEST_SCRIPT_CONTENT, TEST_FILE, TEST_SCRIPT

    assert GREP_FILE != TEST_FILE and GREP_SCRIPT != TEST_SCRIPT
    for content in (_TEST_CONTENT, _TEST_SCRIPT_CONTENT):
        assert "zzmarker" not in content


def test_readonly_plan_exercises_find_literal_without_writing():
    plan = build_self_test_plan("x.y.z", readonly=True)
    assert "find_literal" in plan
    assert "total_matches 0" in plan and "bad_pattern" in plan
    for mutating in ("add_document", "edit_document", "remove_document", "remove_asset"):
        assert f"{mutating} filepath" not in plan


# ---------------------------------------------------------------------------
# Plan-vs-surface coverage. The whole promise of get_self_test_plan is that it
# is "always current" — which silently stops being true the moment a tool ships
# without a step. This is the tripwire for that.
# ---------------------------------------------------------------------------

# Deliberately unexercised, with the reason. Adding a tool means either giving
# it a step or adding it here CONSCIOUSLY — never quietly.
UNCOVERED = {
    "add_from_url",        # needs a stable external URL to fetch
    "get_self_test_plan",  # calling it IS the first step
}
# 5.0: reindex_documents and get_reindex_status left this set. They are named in
# the OPTIONAL server-shell section (step S1, the external-modification check),
# which is the only place a reindex can be justified — a full reindex of a live
# knowledge base is still too expensive to run unconditionally, which is why S1
# is opt-in rather than a numbered step.


def _surface() -> set[str]:
    from cognita.engine_local import ENGINE_TOOL_DEFS
    from cognita.readonly import MUTATING_TOOLS, READONLY_TOOLS

    return {t["name"] for t in ENGINE_TOOL_DEFS} | READONLY_TOOLS | MUTATING_TOOLS


def test_writable_plan_names_every_tool_except_the_documented_exclusions():
    plan = build_self_test_plan("x.y.z", readonly=False)
    missing = {t for t in _surface() if t not in plan}
    assert missing == UNCOVERED, (
        f"self-test drift — unexercised: {sorted(missing - UNCOVERED)}; "
        f"newly covered (drop from UNCOVERED): {sorted(UNCOVERED - missing)}"
    )


def test_asset_tools_are_in_required_numbered_steps_not_optional_prose():
    plan = build_self_test_plan("x.y.z", readonly=False)
    asset_plan = plan[plan.index("ASSET CHECKS (10.1;"):]
    assert "These are REQUIRED" in asset_plan
    assert "optional unless" not in asset_plan
    for number in range(1, 13):
        assert f"A{number}." in asset_plan
    assert "remove_asset" in asset_plan
    assert "cognita-selftest-assets/a.png" in asset_plan
    assert "cognita-selftest-assets/b.png" in asset_plan
    assert "must never modify, reindex, or remove" in asset_plan


def test_readonly_plan_names_every_read_tool_it_can_reach():
    from cognita.readonly import READONLY_TOOLS

    plan = build_self_test_plan("x.y.z", readonly=True)
    missing = {t for t in READONLY_TOOLS if t not in plan}
    assert missing == {"get_reindex_status", "get_self_test_plan"}


def test_book_and_storage_plan_coverage_is_read_only_and_fixture_gated():
    from cognita.readonly import READONLY_TOOLS

    readonly = build_self_test_plan("x.y.z", readonly=True)
    writable = build_self_test_plan("x.y.z", readonly=False)
    book_reads = {
        "audiobook_inspect_chapter", "audiobook_get_chapter", "audiobook_find_chunk",
        "audiobook_get_job", "audiobook_get_generations", "audiobook_get_book",
        "book_get_index_status", "list_project_files", "read_project_file",
    }
    book_writes = {
        "audiobook_prepare_chapter", "audiobook_record_generation", "audiobook_import_audio",
        "audiobook_build", "audiobook_commit_build", "audiobook_cancel_job",
        "set_folder_indexing",
    }
    assert book_reads <= READONLY_TOOLS
    assert all(tool in readonly and tool in writable for tool in book_reads)
    assert all(tool in writable for tool in book_writes)
    assert all(tool not in readonly for tool in book_writes)
    for plan in (readonly, writable):
        assert "book_fixture_not_configured" in plan
        assert "configuration_conflict" in plan
    assert "does not" in writable and "create or alter Book_Layout" in writable
    assert "Never invoke a paid TTS provider" in writable
    assert "include_text=false" in readonly
    assert "omit include_prompt" in readonly
    assert "chapter prose" in readonly


def test_tool_description_admits_what_it_does_not_cover():
    """The description used to claim it exercised EVERY tool while missing
    seven. An overclaiming canary is worse than none — it converts a real gap
    into a clean bill of health."""
    from cognita.selftest import SELFTEST_TOOL_DEF

    desc = SELFTEST_TOOL_DEF["description"]
    assert "every tool" not in desc
    for tool in UNCOVERED:
        assert tool in desc, f"{tool} is uncovered but not declared in the description"
