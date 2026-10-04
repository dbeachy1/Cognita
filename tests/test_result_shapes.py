"""Result-shape consistency (5.0 §5 and §6).

Four reported defects, one root cause: a caller cannot predict what a response
looks like, so it reads the wrong key, gets an empty list or a null, and reports
a working tool as broken. That happened for real on 2026-08-29 — `results` was
read off a `search_similar` response and the tool was briefly reported broken
when it was returning correct neighbors under `similar_documents`.

These tests need no Postgres: they exercise the shape contracts directly.
"""

import pytest

from cognita.engine_local import ENGINE_TOOL_DEFS_BY_NAME, _empty_selection_message
from cognita.literals import glob_matches
from cognita.proxy import GATEWAY_TOOL_DEFS


# --------------------------------------------------------------------- 2.3
# The glob was reported broken. It is not — these are the exact cases from the
# report, and every one behaves the way a shell does. Pinned so the correct
# behavior cannot be "fixed" into incorrect behavior by a later reading of
# that report.


@pytest.mark.parametrize("path,pattern,expected", [
    # Reported as broken; correct. In the observed case, 114 .txt files lived
    # one level below the selected folder, and '*' does not cross a '/'.
    ("notes/research/topic/a.txt", "notes/research/*.txt", False),
    ("notes/research/a.txt", "notes/research/*.txt", True),
    ("notes/research/topic/a.txt", "notes/research/**/*.txt", True),
    # Reported as broken; correct. A bare pattern matches the basename at any
    # depth, which is what ripgrep --glob does.
    ("notes/research/build_index.py", "*.py", True),
    ("notes/research/topic/build_index.py", "**/build_index*.py", True),
    ("notes/research/build_index.py", "**/build_index*.py", True),
    ("notes/readme.md", "*.py", False),
])
def test_glob_matches_like_a_shell(path, pattern, expected):
    assert glob_matches(path, pattern) is expected


def test_a_malformed_glob_is_false_not_an_exception():
    assert glob_matches("a/b.md", "[") is False


def test_empty_selection_message_names_the_filter_that_emptied_it():
    """A zero must be diagnosable. The 2026-08-29 misdiagnosis happened because
    "no files selected" and "string not present" produced identical payloads."""
    msg = _empty_selection_message("selftest", "Wildcards/*.txt", True, 338)
    assert "NO DOCUMENTS WERE SELECTED" in msg
    assert "Wildcards/*.txt" in msg and "selftest" in msg
    assert "338" in msg
    assert "NOT the same as the pattern being absent" in msg


def test_empty_selection_message_explains_the_single_star_trap():
    """The specific misreading, answered inline with the pattern that works."""
    msg = _empty_selection_message(None, "Wildcards/*.txt", True, 338)
    assert "never crosses a '/'" in msg
    assert "Wildcards/**/*.txt" in msg


def test_empty_selection_message_does_not_invent_a_glob_hint():
    """A bare-basename glob has no '/' trap to explain, so do not claim one."""
    msg = _empty_selection_message(None, "*.py", True, 338)
    assert "never crosses" not in msg


def test_empty_corpus_is_reported_as_an_empty_corpus():
    msg = _empty_selection_message(None, None, True, 0)
    assert "index may be empty" in msg


# --------------------------------------------------------------------- 2.3/2.7
# Documented in the tool descriptions, which is what the model on the other end
# of the connector actually reads.


def test_filepath_glob_description_warns_about_the_separator():
    desc = ENGINE_TOOL_DEFS_BY_NAME["find_literal"]["inputSchema"]["properties"]
    text = desc["filepath_glob"]["description"]
    assert "NEVER crosses a '/'" in text
    assert "no_documents_selected" in text


# --------------------------------------------------------------------- 2.9
# The four collection key names still exist for wire compatibility, so the
# contract is that every collection response NAMES its own key.


def test_every_tool_that_returns_a_collection_is_accounted_for():
    """A registry of collection keys, asserted against the live tool list.

    If a new tool starts returning a collection it must be added here — which is
    the point: the mapping table in DESIGN-5.0-raw-surface.md is only true while
    something forces it to be.
    """
    collections = {
        "search_knowledge": "results",
        "search_similar": "similar_documents",
        "list_documents": "documents",
        "find_literal": "matches",
        "evaluate_retrieval": "per_query",
        "copy_directory": "documents",
        "remove_directory": "backups",
        "remove_documents": "documents",
        "list_backups": "backups",
    }
    known = set(ENGINE_TOOL_DEFS_BY_NAME) | set(GATEWAY_TOOL_DEFS)
    assert set(collections) <= known, set(collections) - known


def test_collection_helper_names_the_key_and_can_alias():
    from cognita.engine_local import _collection

    named = _collection({"similar_documents": [1, 2]}, "similar_documents", alias=True)
    assert named["result_key"] == "similar_documents"
    assert named["results"] == [1, 2]

    # Unbounded collections are named but NOT duplicated: doubling a 338-document
    # manifest would double a payload whose whole purpose is to travel cheaply.
    big = _collection({"documents": [1, 2]}, "documents", alias=False)
    assert big["result_key"] == "documents"
    assert "results" not in big


def test_collection_helper_does_not_alias_results_onto_itself():
    from cognita.engine_local import _collection

    payload = _collection({"results": [1]}, "results", alias=True)
    assert payload["result_key"] == "results" and payload["results"] == [1]


def test_the_documented_tool_counts_are_true():
    """DESIGN-10.1-MCP-STRUCTURED-OUTPUTS.md §15 and the CHANGELOG state these numbers,
    and a client sizes its expectations on them. A count in prose that nothing
    checks is a count that quietly becomes wrong the next time a tool lands.
    """
    from cognita.engine_local import ENGINE_TOOL_DEFS
    from cognita.readonly import READONLY_TOOLS

    engine = {t["name"] for t in ENGINE_TOOL_DEFS}
    gateway = set(GATEWAY_TOOL_DEFS)
    assert len(engine) == 30 and len(gateway) == 9
    assert len(engine | gateway) == 39, "engine and gateway provide 39 tools; list_projects brings the public catalog to 40"
    # Read-only projects: the engine list is filtered to the allow-list and only
    # the read-only gateway tools are injected.
    assert len((engine | gateway) & READONLY_TOOLS) == 20
    # Every allow-listed read tool must actually be served by one of the layers,
    # or tools/list would advertise a tool that does not exist.
    assert READONLY_TOOLS <= (engine | gateway)


def test_read_tool_descriptions_match_the_verbatim_behavior():
    """The tool description is where the claim has to be true.

    This test used to assert the OPPOSITE — that the description must say
    NEWLINE-NORMALIZED and must not claim byte fidelity — because in 5.0.1 the
    read really did fold CRLF, and a self-test run that believed the older
    "exact bytes" wording reported a perfectly stored file as corrupt.

    ⚠️ Fixing that by pinning the description to the DEVIATION is what made the
    deviation permanent. The guard passed for five releases while the read it
    guarded was quietly reformatting .json and deleting .md frontmatter — the
    test was measuring agreement between two wrong things rather than either of
    them against the truth. 5.6.0 made every read verbatim; this now asserts the
    behavior, which is what the neighboring write-side test already did.
    """
    from cognita.engine_local import ENGINE_TOOL_DEFS_BY_NAME
    from cognita.reading import READ_TOOL_DEF

    desc = READ_TOOL_DEF["description"]
    assert "BYTE-VERBATIM" in desc
    assert "NEWLINE-NORMALIZED" not in desc
    # ...and it must still name the write-guard stamp and the byte hash, or a
    # caller has no way to tell which value answers which question.
    assert "bytes_sha256" in desc and "content_sha256" in desc

    get_desc = ENGINE_TOOL_DEFS_BY_NAME["get_document"]["description"]
    assert "BYTE-VERBATIM" in get_desc
    # the single exception has to be named where it is read, not only in DESIGN
    assert "content_is_extracted" in get_desc


def test_write_tool_descriptions_do_not_claim_to_strip_a_trailing_newline():
    """Same defect as the one above, found in the same place a release later.

    add_document and update_document still advertised "A trailing newline is
    stripped on write" — false since 5.0.0 made writes byte-verbatim. A client
    reading that adds a newline defensively, or treats a byte comparison as
    expected-to-differ, and either way the description is what it believes.
    """
    from cognita.engine_local import ENGINE_TOOL_DEFS_BY_NAME

    for name in ("add_document", "update_document"):
        desc = ENGINE_TOOL_DEFS_BY_NAME[name]["description"]
        assert "trailing newline is stripped" not in desc, name
        assert "BYTE-VERBATIM" in desc, name


def test_remove_document_description_matches_what_the_tool_now_does():
    """Until 5.7 this asserted the description said "the watcher re-indexes it",
    which was honest prose about a dishonest operation: de-indexing without
    deleting reverted itself. 5.7 made the removal durable, so the same sentence
    became the false one — 5.6.1's lesson (a description outliving the behavior
    it describes) applied the other way round.

    The client on the far end acts on this text, so it has to say the removal
    sticks, has to name the route back, and must not still promise a re-index.
    """
    from cognita.engine_local import ENGINE_TOOL_DEFS_BY_NAME

    desc = ENGINE_TOOL_DEFS_BY_NAME["remove_document"]["description"]
    assert "watcher re-indexes it" not in desc
    assert "DE-INDEX LIST" in desc
    assert "survives restarts" in desc
    assert "add_document/update_document" in desc  # how to undo it
    # file_deleted is the field the caller has to read, and it reports the
    # OUTCOME now rather than echoing the argument.
    assert "file_deleted" in desc and "OUTCOME" in desc


def test_self_test_plan_does_not_claim_writes_strip():  # noqa: D401
    """The plan told the connector-side model that writes strip.

    False since 5.0.0 made writes byte-verbatim, and it contradicted the
    BYTE-FIDELITY section of the same document, which says every assertion is
    byte equality "not equal after stripping". The plan text is what the model
    on the other end actually follows, so a false claim there produces a run
    that reports a perfectly stored file as correct-after-stripping — or worse,
    a sync script built on the belief. Same defect class as 5.0.1's
    read_document claim and 5.0.2's tool descriptions, in the one place neither
    of those fixes looked.
    """
    from cognita.selftest import build_self_test_plan

    plan = build_self_test_plan("5.1.0", readonly=False)
    assert "content.strip()" not in plan
    assert "Writes strip" not in plan
    assert "comes back without one" not in plan
