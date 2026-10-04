"""Big-document behavior: the small self-test document never
exercises truncation/pagination or deep-in-file editing at realistic sizes.

The generated document is deterministic (seeded), several times larger than
both read caps, and every content line carries a unique [sXXpYlZ] marker so
anchors are provably unambiguous and reassembly errors cannot cancel out.
"""

import random

from cognita.editing import apply_batch, apply_edit, content_sha256
from cognita.reading import READ_MAX_CHARS, READ_MAX_LINES, read_slice

_WORDS = (
    "lorem ipsum dolor sit amet consectetur adipiscing elit sed do eiusmod "
    "tempor incididunt ut labore et dolore magna aliqua enim ad minim veniam "
    "quis nostrud exercitation ullamco laboris nisi ut aliquip ex ea commodo"
).split()


def make_big_doc(sections: int = 40, paras: int = 4, lines_per: int = 6,
                 seed: int = 42) -> str:
    rng = random.Random(seed)
    lines = ["# Big Document", ""]
    for s in range(1, sections + 1):
        lines.append(f"## Section {s:02d}")
        for p in range(paras):
            for line_no in range(lines_per):
                n = rng.randint(8, 14)
                words = " ".join(rng.choice(_WORDS) for _ in range(n))
                lines.append(f"{words} [s{s:02d}p{p}l{line_no}]")
            lines.append("")
    return "\n".join(lines).strip()


DOC = make_big_doc()
TOTAL_LINES = DOC.count("\n") + 1


def test_doc_is_actually_big():
    """Guard the premise: the doc must exceed BOTH caps by a wide margin."""
    assert len(DOC) > 3 * READ_MAX_CHARS
    assert TOTAL_LINES > 2 * READ_MAX_LINES


def test_pagination_reassembles_byte_identical():
    """The star: follow the resume hints from start to finish and the pieces
    must recompose the EXACT document — no lost, duplicated or mangled lines
    anywhere (finding #1's bug class, proven dead at scale)."""
    pieces, start, rounds = [], 1, 0
    while True:
        p = read_slice(DOC, start_line=start)
        assert p["total_lines"] == TOTAL_LINES  # stable across every page
        body = p["text"].split("\n")
        assert p["end_line"] - p["start_line"] + 1 == len(body)  # count-exact
        assert len(body) <= READ_MAX_LINES
        assert len(p["text"]) <= READ_MAX_CHARS
        pieces.append(p["text"])
        if not p["truncated"]:
            assert p["end_line"] == TOTAL_LINES
            break
        start = p["end_line"] + 1
        rounds += 1
        assert rounds < 100, "pagination is not terminating"
    assert rounds >= 3  # multiple pages actually exercised
    assert "\n".join(pieces) == DOC  # byte-identical reassembly


def test_every_page_reports_same_content_sha():
    """The whole-file version stamp must be identical on every page — it's the
    staleness credential, and pagination must not perturb it."""
    h = content_sha256(DOC)
    p1 = read_slice(DOC, start_line=1)
    p2 = read_slice(DOC, start_line=p1["end_line"] + 1)
    assert p1["content_sha256"] == p2["content_sha256"] == h


def test_section_read_deep_in_file():
    p = read_slice(DOC, section="Section 37")
    assert p["section"] == "## Section 37"
    assert p["text"].startswith("## Section 37")
    assert "[s37p0l0]" in p["text"] and "[s37p3l5]" in p["text"]
    assert "[s38p0l0]" not in p["text"]  # next section excluded
    body = p["text"].split("\n")
    assert body[-1].strip() != ""  # ends at content (2.10.6 boundary rule)
    assert DOC.split("\n")[p["end_line"] - 1] == body[-1]


def test_anchor_from_deep_page_edits_exactly_once():
    """The workflow at scale: ranged read deep in the file -> multi-line anchor
    from the returned text -> exactly-once edit, nothing else disturbed."""
    deep = TOTAL_LINES - 60  # near the end, wherever the generator put it
    page = read_slice(DOC, start_line=deep, end_line=deep + 10)
    anchor = page["text"]
    out = apply_edit(DOC, anchor, "REPLACED-DEEP-BLOCK")
    assert out.replacements == 1
    assert "REPLACED-DEEP-BLOCK" in out.new_content
    # everything outside the anchor is untouched: same doc except the splice
    assert out.new_content == DOC.replace(anchor, "REPLACED-DEEP-BLOCK")


def test_batch_edits_at_opposite_ends():
    first_marker = "[s01p0l0]"
    last_marker = "[s40p3l5]"
    out = apply_batch(DOC, [
        {"old_str": first_marker, "new_str": "[EDITED-NEAR-TOP]"},
        {"old_str": last_marker, "new_str": "[EDITED-NEAR-BOTTOM]"},
    ])
    assert [e["replacements"] for e in out.edits] == [1, 1]
    assert "[EDITED-NEAR-TOP]" in out.new_content
    assert "[EDITED-NEAR-BOTTOM]" in out.new_content
    assert "truncated" in out.context_diff or out.context_diff.count("@@") <= 4


def test_unique_markers_make_ambiguity_detectable():
    """Sanity check on the generator itself: a repeated phrase IS ambiguous
    (the guard still works at scale), while markers are unique."""
    import pytest

    from cognita.editing import EditReject

    with pytest.raises(EditReject) as ei:
        apply_edit(DOC, "## Section", "x")
    assert ei.value.payload["reason"] == "ambiguous"
    assert ei.value.payload["match_count"] == 40
