"""Pure slice/section logic for read_document (2.3) + insert_in_document (2.5)."""

import pytest

from cognita.editing import EditReject
from cognita.reading import READ_MAX_LINES, apply_insert, read_slice

DOC = """# Title

## Section One
alpha line
beta line

### Sub One-A
sub content

## Section Two
gamma line

```bash
# not a heading — fenced code
echo hi
```

## Section Three
delta line"""


def reject(*args, **kw) -> EditReject:
    with pytest.raises(EditReject) as ei:
        read_slice(*args, **kw)
    return ei.value


def test_whole_file():
    p = read_slice(DOC)
    assert p["start_line"] == 1 and p["end_line"] == p["total_lines"]
    assert p["truncated"] is False
    assert p["text"] == DOC  # verbatim


def test_line_range_1indexed_inclusive():
    p = read_slice(DOC, start_line=4, end_line=5)
    assert p["text"] == "alpha line\nbeta line"
    assert p["start_line"] == 4 and p["end_line"] == 5


def test_range_clamped_and_out_of_range():
    p = read_slice(DOC, start_line=17, end_line=999)
    assert p["end_line"] == p["total_lines"]
    exc = reject(DOC, start_line=999)
    assert exc.payload["reason"] == "out_of_range"
    assert reject(DOC, start_line=5, end_line=2).payload["reason"] == "invalid"


def test_section_includes_subsections_stops_at_peer():
    p = read_slice(DOC, section="Section One")
    assert p["section"] == "## Section One"
    assert "alpha line" in p["text"]
    assert "### Sub One-A" in p["text"] and "sub content" in p["text"]  # subsection kept
    assert "gamma line" not in p["text"]  # next ## closes it
    assert p["text"].startswith("## Section One")


def test_section_end_line_matches_delivered_text():
    """2.10.6 (run-6 finding): section reads included the trailing blank
    separator as an invisible last 'line', so end_line pointed one past what
    the text visibly contained (and disagreed with insert end_of_section's
    notion of where a section's content ends)."""
    p = read_slice(DOC, section="Section One")
    body = p["text"].split("\n")
    assert body[-1].strip() != ""                              # ends at content
    assert p["end_line"] - p["start_line"] + 1 == len(body)    # count agrees
    # the last delivered line really is at end_line in the source
    assert DOC.split("\n")[p["end_line"] - 1] == body[-1]


def test_section_heading_forms_tolerated():
    for form in ("## Section Two", "section two", "  SECTION TWO  "):
        p = read_slice(DOC, section=form)
        assert "gamma line" in p["text"]


def test_backtick_fence_inside_tilde_fence():
    """2.10.4: a ``` line inside a ~~~ block is literal content (CommonMark);
    it must not close the tilde fence and resurrect '# lines' as headings."""
    doc = ("## Real One\ntext\n\n~~~\n```\n# not a heading\n```\n~~~\n\n"
           "## Real Two\nmore")
    p = read_slice(doc, section="Real One")
    assert "# not a heading" in p["text"]      # fence content stays inside
    assert "## Real Two" not in p["text"]      # next real heading closes it
    p2 = read_slice(doc, section="Real Two")
    assert p2["text"] == "## Real Two\nmore"
    with pytest.raises(EditReject):
        read_slice(doc, section="not a heading")


def test_fenced_hash_line_is_not_a_heading():
    p = read_slice(DOC, section="Section Two")
    # the fenced '# not a heading' line must NOT terminate or match sections
    assert "# not a heading" in p["text"]
    assert "## Section Three" not in p["text"]
    exc = reject(DOC, section="not a heading — fenced code")
    assert exc.payload["reason"] == "not_found"


def test_unknown_section_hints_available_headers():
    exc = reject(DOC, section="Ghost Section")
    assert exc.payload["reason"] == "not_found"
    hint = exc.payload["hint"]
    assert "## Section One" in hint and "line 3" in hint


def test_duplicate_section_ambiguous():
    doc = "## Dup\na\n## Dup\nb"
    exc = reject(doc, section="Dup")
    assert exc.payload["reason"] == "ambiguous"
    assert [m["line"] for m in exc.payload["matches"]] == [1, 3]


def test_section_and_range_mutually_exclusive():
    assert reject(DOC, start_line=1, section="Section One").payload["reason"] == "invalid"


def test_truncation_reports_resume_point():
    big = "\n".join(f"line {i}" for i in range(1, 1001))
    p = read_slice(big)
    assert p["truncated"] is True
    assert p["end_line"] == READ_MAX_LINES
    assert "start_line" in p["message"]
    assert p["total_lines"] == 1000


def test_char_cap_truncation_no_phantom_line():
    """2.10.4: the char-cap cut left a trailing '' line, so end_line
    overcounted and the resume message pointed past a line never delivered —
    a follow-up read from there silently lost a line."""
    big = "\n".join(f"line {i}: " + "x" * 80 for i in range(1, 500))
    p = read_slice(big)
    body = p["text"].split("\n")
    assert body[-1] != ""                      # no phantom line
    assert p["end_line"] == p["start_line"] + len(body) - 1
    # resume from end_line+1 must return real content, starting with the
    # first UNdelivered line
    nxt = read_slice(big, start_line=p["end_line"] + 1)
    assert nxt["text"].split("\n")[0] == f"line {p['end_line'] + 1}: " + "x" * 80


# ------------------------------------------------------------ insert (2.5)

INS_DOC = """# Doc

## Tests
Test 1 does a thing.
Test 2 does another.

## Notes
some notes"""


def ireject(*args, **kw) -> EditReject:
    with pytest.raises(EditReject) as ei:
        apply_insert(*args, **kw)
    return ei.value


def test_insert_start_and_end():
    out = apply_insert(INS_DOC, "PREAMBLE", "start")
    assert out.new_content.startswith("PREAMBLE\n# Doc")
    assert out.inserted_at_line == 1
    out = apply_insert(INS_DOC, "POSTSCRIPT", "end")
    assert out.new_content.endswith("some notes\nPOSTSCRIPT")


def test_insert_end_on_newline_terminated_file():
    """2.10.4: 'a\\nb\\n' splits to a phantom '' last element; appending after
    it gained a stray blank line. 'end' now backs past trailing blanks like
    end_of_section does."""
    out = apply_insert("alpha\nbeta\n", "gamma", "end")
    assert out.new_content == "alpha\nbeta\ngamma\n"  # flush after beta; EOL kept
    out2 = apply_insert("alpha\nbeta\n\n\n", "gamma", "end")
    assert "beta\ngamma" in out2.new_content  # no blank between content and block


def test_insert_end_of_section_lands_after_last_content_line():
    out = apply_insert(INS_DOC, "Test 3 does a third thing.", "end_of_section",
                       section="Tests")
    lines = out.new_content.split("\n")
    at = out.inserted_at_line - 1
    assert lines[at] == "Test 3 does a third thing."
    assert lines[at - 1] == "Test 2 does another."   # right after last content
    assert lines[at + 1] == ""                        # trailing blank preserved below
    assert lines[at + 2] == "## Notes"                # next section untouched


def test_insert_end_of_section_on_last_section():
    out = apply_insert(INS_DOC, "more notes", "end_of_section", section="## Notes")
    assert out.new_content.endswith("some notes\nmore notes")


def test_insert_multiline_block_verbatim():
    block = "### Test 3\nsteps here\n\nexpected result"
    out = apply_insert(INS_DOC, block, "end_of_section", section="Tests")
    assert block in out.new_content
    assert "> ### Test 3" in out.context_diff  # diff marks the inserted lines


def test_insert_validation():
    assert ireject(INS_DOC, "", "start").payload["reason"] == "invalid"
    assert ireject(INS_DOC, "x", "middle").payload["reason"] == "invalid"
    assert ireject(INS_DOC, "x", "end_of_section").payload["reason"] == "invalid"
    assert ireject(INS_DOC, "x", "end_of_intro").payload["reason"] == "invalid"
    assert ireject(INS_DOC, "x", "start", section="Tests").payload["reason"] == "invalid"
    assert ireject(INS_DOC, "x", "end_of_section", section="Ghost").payload["reason"] == "not_found"


# --- end_of_intro (2.6, H1 preamble coverage) ---

INTRO_DOC = """# Doc
Preamble line one.
Preamble line two.

## Tests
Test 1 exists.

### Sub-test details
deep content

## Notes
some notes"""


def test_end_of_intro_on_h1_targets_preamble():
    """The finding: end_of_section on the H1 = end of FILE. end_of_intro must
    land right after the preamble, before the first subheading."""
    out = apply_insert(INTRO_DOC, "Added to the preamble.", "end_of_intro", section="Doc")
    lines = out.new_content.split("\n")
    at = out.inserted_at_line - 1
    assert lines[at] == "Added to the preamble."
    assert lines[at - 1] == "Preamble line two."
    assert "## Tests" in lines[at + 1:][:2]  # first subheading right below
    # contrast: end_of_section on the H1 is the whole document
    out2 = apply_insert(INTRO_DOC, "X", "end_of_section", section="Doc")
    assert out2.new_content.endswith("some notes\nX")


def test_end_of_intro_stops_at_first_subheading():
    out = apply_insert(INTRO_DOC, "Test 2 exists.", "end_of_intro", section="Tests")
    lines = out.new_content.split("\n")
    at = out.inserted_at_line - 1
    assert lines[at - 1] == "Test 1 exists."
    assert "### Sub-test details" in lines[at + 1:][:2]
    assert "deep content" in out.new_content  # subtree untouched, below


def test_end_of_intro_equals_end_of_section_when_no_children():
    a = apply_insert(INTRO_DOC, "more", "end_of_intro", section="Notes")
    b = apply_insert(INTRO_DOC, "more", "end_of_section", section="Notes")
    assert a.new_content == b.new_content
