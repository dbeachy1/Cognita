"""Every self-test section a client can ask for comes back with its real instructions.

15.0.1, two bugs found by a claude.ai run against the Windows connector (14.2.4):

* Steps 1-9 are right-aligned in the plan (" 1. CREATE:"), and the section extractor
  only matched headings at column 0, so asking for step 1 answered "available only to
  the connector/server operator" while the index listed it as available.
* A section of a group came back without the group's intro. For the asset checks that
  intro holds the per-run paths and the exact 68-byte fixture image, so the client made
  up its own image and put_asset refused it on the pinned hash.

15.0.1, second pass (a fresh review found section serving still wrong in structure):

* Headings the catalog did not list (7b, 13b, 33b, O3, O4, O5) could never be fetched, and
  because a section ends at the next heading, step 7 and O2 silently lost their tails.
* Every group or stage intro is free text between the last heading of one group and the
  first heading of the next. It rode on the section BEFORE it and was missing from the
  sections it governs (read-only step 13 ended with "report this subsection SKIPPED";
  E1 ended with "SKIP THESE" for the raw-transport steps; step 26 swallowed the
  literal-search rules for 27+).
* Read-only steps 1-13 were labelled scope "writable".
"""

from __future__ import annotations

import re

import pytest

from cognita import selftest

OPERATOR_ONLY = "available only to the connector/server operator"
VERSION = "15.0.1"

# The same heading shape the extractor uses, written out here so the tests do not lean on
# the code under test to find the headings: an id at the start of a line, one leading
# space allowed (" 1." to " 9."), followed by ". ".
HEADING = re.compile(
    r"(?m)^ ?((?:[A-Z]+(?:-[A-Za-z]+)?\d+[A-Za-z]?|[A-Z]|\d+[A-Za-z]?))\. ")

BLANK_PNG_HASH = "cf3adf0667963af8ed7a70f1902dcee28cb809f08f98853c59782e6b7021cebd"


def _available(readonly: bool) -> list[dict]:
    return [row for row in selftest.self_test_section_catalog(readonly=readonly) if row["available"]]


def _plan(readonly: bool) -> str:
    return selftest.build_self_test_plan(VERSION, readonly)


def _instructions(readonly: bool, section_id: str) -> str:
    result = selftest.select_self_test_plan(VERSION, readonly, section_id)
    assert result["status"] == "success", (section_id, result)
    return result["instructions"]


def _headings(plan: str) -> list[re.Match[str]]:
    return list(HEADING.finditer(plan))


def _catalog_ids(readonly: bool) -> list[str]:
    return [str(row["id"]) for row in selftest.self_test_section_catalog(readonly=readonly)]


@pytest.mark.parametrize("readonly", [False, True])
def test_every_available_client_section_returns_its_instructions(readonly):
    """Only the server-shell sections (S, G) are operator-only by design."""
    wrong = []
    for row in _available(readonly):
        section_id = str(row["id"])
        if row["scope"] == "server":
            continue
        result = selftest.select_self_test_plan(VERSION, readonly, section_id)
        assert result["status"] == "success", (section_id, result)
        if OPERATOR_ONLY in result["instructions"]:
            wrong.append(section_id)
    assert not wrong, f"sections answered as operator-only although the index lists them: {wrong}"


@pytest.mark.parametrize("readonly", [False, True])
def test_a_single_digit_step_returns_that_step_and_not_the_next(readonly):
    first = str(_available(readonly)[0]["id"])
    assert first == "1"
    text = selftest.select_self_test_plan(VERSION, readonly, "1")["instructions"]
    assert "1. " in text
    assert "\n 2. " not in text and not text.rstrip().endswith(" 2.")


@pytest.mark.parametrize("section_id", ["A", "A1", "A2", "A7", "A12"])
def test_every_asset_section_carries_the_fixture_image_and_the_run_paths(section_id):
    text = selftest.select_self_test_plan(VERSION, False, section_id)["instructions"]
    assert selftest.ASSET_TEST_DATA_URL in text
    assert selftest.ASSET_TEST_SHA256 in text
    assert "A = '" in text and "B = '" in text


@pytest.mark.parametrize("readonly", [False, True])
@pytest.mark.parametrize("section_id", ["O", "O1", "O2", "O3", "O4", "O5"])
def test_every_ocr_section_carries_the_fixture_hash_list(section_id, readonly):
    """The blank.png hash appears ONLY in the OCR intro's fixture list (O4 names the path,
    never the hash), so finding it on any O section proves the intro travelled with it."""
    text = _instructions(readonly, section_id)
    assert "cognita-selftest/ocr/canonical-clear.png" in text
    assert BLANK_PNG_HASH in text


def test_the_ocr_hash_is_only_in_the_ocr_intro():
    """Guards the assertion above: if the hash ever moved into a step body, it would stop
    proving anything about the intro."""
    for readonly in (False, True):
        plan = _plan(readonly)
        assert plan.count(BLANK_PNG_HASH) == 1, readonly


def test_a_group_intro_is_included_once():
    text = selftest.select_self_test_plan(VERSION, False, "A")["instructions"]
    assert text.count("Fixture image_url (copy exactly):") == 1


# --------------------------------------------------------------------------------------
# 15.0.1 second pass
# --------------------------------------------------------------------------------------

def _intro_positions(plan: str, readonly: bool) -> list[tuple[int, selftest._Intro]]:
    return sorted(
        (match.start(), intro)
        for intro in selftest._intro_blocks_for_mode(readonly)
        for match in re.finditer("(?m)^" + re.escape(intro.header), plan)
    )


@pytest.mark.parametrize("readonly", [False, True])
def test_the_intro_table_matches_the_rendered_plan(readonly):
    """Each intro header is in the plan exactly once, nowhere else, and the headings right
    after it (up to the next intro) are exactly the ids the table says it governs."""
    plan = _plan(readonly)
    intros = selftest._intro_blocks_for_mode(readonly)
    for intro in intros:
        assert plan.count(intro.header) == 1, intro.header
        assert len(re.findall("(?m)^" + re.escape(intro.header), plan)) == 1, intro.header
    other_mode = selftest._intro_blocks_for_mode(not readonly)
    for intro in other_mode:
        if intro not in intros:
            assert intro.header not in plan, (intro.header, "belongs to the other plan only")
    ordered = _intro_positions(plan, readonly)
    assert [intro for _, intro in ordered] == list(intros), "table is not in plan order"
    headings = _headings(plan)
    catalog_group = {str(row["id"]): row["group"] for row in selftest.self_test_section_catalog(readonly=readonly)}
    for index, (start, intro) in enumerate(ordered):
        end = ordered[index + 1][0] if index + 1 < len(ordered) else len(plan)
        between = [m.group(1) for m in headings if start < m.start() < end]
        governed = [step_id for step_id in between if step_id in intro.governs]
        assert governed == list(intro.governs), (intro.header, between)
        # A group's intro governs the group only. Headings after the group that belong to
        # no group (the core steps 1-14 follow B5 with no intro of their own) are not
        # governed by it; they must be plain core steps, never another group's rows.
        leftovers = [step_id for step_id in between if step_id not in intro.governs]
        assert not leftovers or (intro.group == "B" and all(re.fullmatch(r"\d+[a-z]?", s) for s in leftovers)), \
            (intro.header, leftovers)
        for step_id in governed:
            expected_group = catalog_group.get(step_id, intro.group)
            assert expected_group == intro.group, (intro.header, step_id, expected_group)


@pytest.mark.parametrize("readonly", [False, True])
def test_every_heading_belongs_to_exactly_one_available_catalog_section(readonly):
    plan = _plan(readonly)
    ids = [m.group(1) for m in _headings(plan)]
    assert len(ids) == len(set(ids)), "a heading id appears twice in one plan"
    rows = selftest.self_test_section_catalog(readonly=readonly)
    by_id = {str(row["id"]): row for row in rows}
    assert len(by_id) == len(rows), "duplicate catalog id"
    orphans = []
    for step_id in ids:
        row = by_id.get(step_id)
        if row is None:
            # The server-shell steps (S1.., G1..) are children of the operator-only rows.
            parent = by_id.get(step_id[0])
            if parent is not None and parent["scope"] == "server" and re.fullmatch(r"[SG]\d+", step_id):
                assert parent["available"] == (not readonly), step_id
                continue
            orphans.append(step_id)
        else:
            assert row["available"], (step_id, "heading is in the plan but the row says unavailable")
    assert not orphans, f"headings in the {'read-only' if readonly else 'writable'} plan with no catalog row: {orphans}"
    # And the reverse: an available leaf row (not a group, not server-shell) has a heading.
    groups = {str(row["group"]) for row in rows if row["group"]}
    phantom = [str(row["id"]) for row in rows
               if row["available"] and row["scope"] != "server" and str(row["id"]) not in groups
               and str(row["id"]) not in ids]
    assert not phantom, f"available catalog rows with no heading in the plan: {phantom}"


@pytest.mark.parametrize("readonly", [False, True])
def test_bodies_and_intros_partition_the_plan_in_order(readonly):
    """Every non-heading, non-intro line is in exactly one section body or one intro, and
    concatenating them in plan order gives back the plan (whitespace-only joins)."""
    plan = _plan(readonly)
    pieces: list[tuple[int, int, str]] = []
    for match in _headings(plan):
        body = selftest._rendered_section(plan, match.group(1))
        assert body, match.group(1)
        assert plan.count(body) == 1, (match.group(1), "body is not unique in the plan")
        start = plan.index(body)
        pieces.append((start, start + len(body), f"section {match.group(1)}"))
    for intro in selftest._intro_blocks_for_mode(readonly):
        text = selftest._intro_text(plan, intro)
        assert text, intro.header
        assert plan.count(text) == 1, intro.header
        start = plan.index(text)
        pieces.append((start, start + len(text), f"intro {intro.header}"))
    pieces.sort()
    # The text before the first piece is the plan-wide preamble: it holds no heading and no intro.
    first_start = pieces[0][0]
    assert not HEADING.search(plan[:first_start]), "a heading sits before the first piece"
    assert selftest._boundary_pattern().search(plan[:first_start]) is None
    rebuilt = []
    for index, (start, end, label) in enumerate(pieces):
        rebuilt.append(plan[start:end])
        if index + 1 < len(pieces):
            next_start = pieces[index + 1][0]
            assert end <= next_start, (label, "overlaps", pieces[index + 1][2])
            assert plan[end:next_start].strip() == "", (label, "text between pieces belongs to neither", plan[end:next_start][:80])
    assert plan[pieces[-1][1]:].strip() == "", "text after the last piece belongs to neither"
    squashed = lambda text: re.sub(r"\s+", "", text)  # noqa: E731
    assert squashed("".join(rebuilt)) == squashed(plan[first_start:])


@pytest.mark.parametrize("readonly", [False, True])
def test_no_section_body_contains_an_intro_header(readonly):
    plan = _plan(readonly)
    headers = [intro.header for intro in selftest._INTRO_BLOCKS]
    for match in _headings(plan):
        body = selftest._rendered_section(plan, match.group(1))
        for header in headers:
            assert not re.search("(?m)^" + re.escape(header), body), (match.group(1), header)


def test_a_governed_step_carries_its_intro():
    step27 = _instructions(False, "27")
    assert "No approval received" in step27
    assert "LITERAL-SEARCH STEPS" in step27
    step15 = _instructions(False, "15")
    assert "REGISTERED-TIER STEPS (4.4)." in step15
    step30 = _instructions(False, "30")
    assert "LITERAL-SEARCH STEPS" in step30
    assert "REGISTERED-TIER" not in step30
    assert "MANIFEST + WRITE-GUARD STEPS" in _instructions(False, "35")
    assert "COPY + DIRECTORY STEPS" in _instructions(False, "42")
    assert "CATEGORY + FULL-REPLACE STEPS" in _instructions(False, "33b")
    assert "SEARCH SHAPE + GLOB STEPS" in _instructions(False, "GL4")
    assert "DE-INDEX STEPS (5.7)" in _instructions(False, "D3")
    assert "ERROR ENVELOPE STEPS (5.0.2)" in _instructions(False, "E1")
    assert "BYTE-FIDELITY STEPS." in _instructions(False, "B3")


def test_an_intro_does_not_ride_on_the_section_before_it():
    """The core of the bug: the intro was the tail of the previous section."""
    assert "REGISTERED-TIER" not in _instructions(False, "14")
    assert "LITERAL-SEARCH" not in _instructions(False, "26")
    assert "MANIFEST + WRITE-GUARD STEPS" not in _instructions(False, "34")
    assert "SEARCH SHAPE + GLOB STEPS" not in _instructions(False, "51")
    assert "DE-INDEX STEPS" not in _instructions(False, "GL8")
    assert "ERROR ENVELOPE STEPS" not in _instructions(False, "D4")
    assert "OCR CHECKS" not in _instructions(False, "A12")
    assert BLANK_PNG_HASH not in _instructions(False, "A12")


def test_read_only_step_13_does_not_carry_the_asset_skip_rule():
    step13 = _instructions(True, "13")
    assert "If list_assets returns no assets" not in step13
    assert "ASSET READ CHECKS" not in step13
    for section_id in ("R-A", "R-A1", "R-A5"):
        assert "If list_assets returns no assets" in _instructions(True, section_id)


def test_raw_transport_skip_rule_goes_with_r_and_not_with_e1():
    e1 = _instructions(False, "E1")
    assert "RAW-TRANSPORT STEPS" not in e1 and "SKIP THESE" not in e1
    for section_id in ("R", "R1", "R5"):
        text = _instructions(False, section_id)
        assert "RAW-TRANSPORT STEPS (10.1). SKIP THESE" in text, section_id
        assert text.count("RAW-TRANSPORT STEPS") == 1, section_id


def test_a1_carries_the_asset_intro_and_a12_does_not_carry_the_ocr_intro():
    a1 = _instructions(False, "A1")
    assert "Fixture image_url (copy exactly):" in a1
    assert "ASSET CHECKS (10.1" in a1
    a12 = _instructions(False, "A12")
    assert "OCR CHECKS (10.1)" not in a12
    assert "canonical-clear.png" not in a12


def test_server_shell_sections_stay_operator_only():
    for section_id in ("S", "G"):
        result = selftest.select_self_test_plan(VERSION, False, section_id)
        assert result["status"] == "success"
        assert OPERATOR_ONLY in result["instructions"]
        assert "OPTIONAL SHELL STEPS" not in result["instructions"]
        assert "GPU STEPS (6.0)" not in result["instructions"]


@pytest.mark.parametrize("readonly", [False, True])
def test_the_ocr_group_carries_all_five_steps(readonly):
    text = _instructions(readonly, "O")
    for step_id in ("O1.", "O2.", "O3.", "O4.", "O5."):
        assert step_id in text, step_id
    assert text.count("OCR CHECKS (10.1)") == 1
    assert "O3" in _catalog_ids(readonly)


@pytest.mark.parametrize("section_id,readonly", [
    ("7b", False), ("13b", False), ("33b", False),
    ("O3", False), ("O4", False), ("O5", False),
    ("O3", True), ("O4", True), ("O5", True),
])
def test_the_headings_the_catalog_used_to_miss_are_fetchable(section_id, readonly):
    text = _instructions(readonly, section_id)
    assert OPERATOR_ONLY not in text
    assert f"{section_id}. " in text


def test_a_lettered_step_and_its_neighbor_do_not_share_text():
    step7 = _instructions(False, "7")
    step7b = _instructions(False, "7b")
    assert "7b. SEARCH FRESH EDIT" not in step7
    assert "SEARCH FRESH EDIT" in step7b
    assert "NEGATIVE ambiguous: edit_document old_str='line'" not in step7b
    assert "NEGATIVE ambiguous: edit_document old_str='line'" in _instructions(False, "8")
    step13 = _instructions(False, "13")
    assert "13b. MOVE/RENAME" not in step13
    assert "restore_backup to this run's oldest backup_id" in step13
    assert "MOVE/RENAME" in _instructions(False, "13b")
    step33 = _instructions(False, "33")
    assert "33b. " not in step33


def test_o2_stops_before_o3_and_o3_is_its_own_section():
    o2 = _instructions(False, "O2")
    assert "O3. Search the canonical fixture's extracted token" not in o2
    o3 = _instructions(False, "O3")
    assert "O3. Search the canonical fixture's extracted token" in o3
    assert "O4. " not in o3


def test_read_only_core_steps_report_a_scope_that_includes_read_only():
    rows = {str(row["id"]): row for row in selftest.self_test_section_catalog(readonly=True)}
    for step in range(1, 14):
        assert rows[str(step)]["scope"] in {"both", "readonly"}, step
        assert rows[str(step)]["available"], step
        # The row a client fetches by that id serves the same scope.
        assert selftest.select_self_test_plan(VERSION, True, str(step))["scope"] in {"both", "readonly"}


def test_core_step_scope_matches_the_two_rendered_plans():
    writable_ids = {m.group(1) for m in _headings(_plan(False))}
    readonly_ids = {m.group(1) for m in _headings(_plan(True))}
    for row in selftest.self_test_section_catalog(readonly=False):
        step_id = str(row["id"])
        if not re.fullmatch(r"\d+[a-z]?", step_id):
            continue
        in_writable, in_readonly = step_id in writable_ids, step_id in readonly_ids
        expected = "both" if (in_writable and in_readonly) else "writable" if in_writable else "readonly"
        assert row["scope"] == expected, (step_id, row["scope"], expected)


def test_the_catalog_lists_the_new_rows_in_a_sensible_order():
    ids = _catalog_ids(False)
    assert ids.index("7") + 1 == ids.index("7b") == ids.index("8") - 1
    assert ids.index("13") + 1 == ids.index("13b") == ids.index("14") - 1
    assert ids.index("33") + 1 == ids.index("33b") == ids.index("34") - 1
    o_rows = [row for row in selftest.self_test_section_catalog(readonly=False) if row["group"] == "O"]
    assert [row["id"] for row in o_rows] == ["O1", "O2", "O3", "O4", "O5"]
    assert {row["scope"] for row in o_rows} == {"both"}
