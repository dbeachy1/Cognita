"""Every error payload carries a machine-readable `reason` (5.0.2).

The self-test plan tells clients to branch on `reason` and never on message
text, because message text is prose and gets reworded between versions. That
instruction is only honest if the field is always there — and on 2026-08-29 a
connector run found four paths still returning reason=None (get_document and
remove_document not-found, move_document same-path and destination-exists),
leaving the runner nothing to assert on but the prose it had just been told to
ignore.

This is the source-level guard: a new error payload without a reason fails here,
at the commit that adds it, rather than in a connector run months later. The
runtime backstops (engine_local._with_reason, proxy._tool_result) are the net
under it, and tests/test_engine_local.py exercises the live paths.
"""

import ast
import pathlib

import pytest

SRC = pathlib.Path(__file__).resolve().parents[1] / "src" / "cognita"

# Vocabulary. Adding a value here is a deliberate act — a client branching on
# `reason` is reading this list, so a synonym for an existing reason is a
# defect, not a new case.
KNOWN_REASONS = {
    "ambiguous", "backup_failed", "bad_pattern", "batch_aborted", "busy", "copy_failed",
    "destination_exists",
    "destination_not_a_file", "empty", "error", "fetch_failed", "has_subdirectories",
    "delete_failed",
    "internal_error", "invalid", "invalid_path", "no_change", "no_documents_selected",
    "no_indexable_content",
    "no_matches", "not_empty", "not_found", "not_text", "parse_failed",
    "read_only", "registered_document", "same_path", "stale_file", "sync_conflict_name",
    "too_large", "too_many_files", "unindexable_extension", "unknown_argument",
    "unreadable", "unsupported_format", "out_of_range", "would_empty_file",
    # Connector, policy, and replay reason codes.
    "operation_conflict", "operation_id_conflict", "policy_conflict",
    "stale_policy_revision", "state_unavailable", "policy_unavailable",
    "project_unavailable",
    "content_too_large", "duplicate_path", "lossy_edit_unsupported", "unknown_section",
    "batch_too_large", "nested_batch_not_allowed", "tool_not_batchable", "invalid_batch",
    "child_no_response", "child_invalid_response", "child_error", "result_too_large",
    "previous_error", "file_changed_during_read", "unknown_tool",
    # Structured-result validation containment reason.
    "output_contract_violation",
    # 10.2 rolling-generation compatibility: the operation belongs to a newer
    # public contract and the client must reconnect using the current URL.
    "upgrade_required", "runtime_unavailable",
    # 13.0 §4.1: the PostgreSQL schema version in the database is not the one
    # this image understands, so the store was left untouched and the whole
    # index tool surface is refused. Deliberately NOT project_unavailable: that
    # one is a bounded authorization answer about one project, and it must stay
    # bounded, whereas this message names both versions and the reset command.
    "index_unavailable",
    # Approved Windows CPU runtime source/workspace availability contracts.
    "source_unavailable", "workspace_unavailable",
}

M1_REASONS = {"operation_conflict", "policy_unavailable", "project_unavailable"}
STRUCTURED_OUTPUT_REASONS = {"output_contract_violation"}
M2_REASONS = {"runtime_unavailable"}
# Database-schema compatibility reason.
SCHEMA_VERSION_REASONS = {"index_unavailable"}


def _error_dicts(tree: ast.AST):
    """Every dict literal that declares status:"error"."""
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        keys = {k.value: v for k, v in zip(node.keys, node.values)
                if isinstance(k, ast.Constant) and isinstance(k.value, str)}
        status = keys.get("status")
        if isinstance(status, ast.Constant) and status.value == "error":
            yield node, keys


# Error payloads built by a CONSTRUCTOR rather than a dict literal. EditReject's
# first positional argument IS the wire `reason` (it builds
# {"status": "error", "reason": ..., ...} in __init__), and _tool_error's second
# is. The dict-literal walk above could not see either, so 20 raise sites in
# editing.py and reading.py were invisible to this guard — and three of their
# values (would_empty_file, batch_aborted, out_of_range) reached clients while
# living outside KNOWN_REASONS. A
# vocabulary guard with a hole in it is worse than none: it reports green.
_REASON_CTORS = {"EditReject": 0, "_tool_error": 1}


def _constructed_reasons(tree: ast.AST):
    """(lineno, reason_or_None) for every EditReject/_tool_error call."""
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
        idx = _REASON_CTORS.get(name)
        if idx is None or len(node.args) <= idx:
            continue
        arg = node.args[idx]
        yield node.lineno, (arg.value if isinstance(arg, ast.Constant) else None)


MODULES = sorted(SRC.glob("*.py"))


@pytest.mark.parametrize("path", MODULES, ids=lambda p: p.name)
def test_every_error_literal_declares_a_reason(path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    missing = [node.lineno for node, keys in _error_dicts(tree) if "reason" not in keys]
    assert not missing, (
        f"{path.name}: status:\"error\" with no reason at line(s) "
        f"{missing}. Pick a value from KNOWN_REASONS in this file, or add a new "
        "one here deliberately — a client branching on reason reads that list."
    )


@pytest.mark.parametrize("path", MODULES, ids=lambda p: p.name)
def test_reason_values_stay_in_the_vocabulary(path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    unknown = set()
    for _node, keys in _error_dicts(tree):
        reason = keys.get("reason")
        if isinstance(reason, ast.Constant) and reason.value not in KNOWN_REASONS:
            unknown.add(reason.value)
    for _lineno, reason in _constructed_reasons(tree):
        if reason is not None and reason not in KNOWN_REASONS:
            unknown.add(reason)
    assert not unknown, (
        f"{path.name}: reason value(s) {sorted(unknown)} are not in KNOWN_REASONS. "
        "Add intentional public reason codes to the vocabulary; use an existing code "
        "when the meaning is already covered."
    )


def test_the_engine_backstop_stamps_a_reason_it_was_not_given():
    from cognita.engine_local import _with_reason

    out = _with_reason("some_tool", {"status": "error", "message": "no reason here"})
    assert out["reason"] == "error"
    # A reason already set is never overwritten.
    kept = _with_reason("t", {"status": "error", "reason": "not_found", "message": "x"})
    assert kept["reason"] == "not_found"
    # Success payloads are untouched.
    ok = _with_reason("t", {"status": "success", "count": 1})
    assert "reason" not in ok


def test_the_guard_can_actually_see_constructor_reasons():
    """A meta-test, because the failure this guard had was reporting GREEN.

    _error_dicts only walks dict literals, and EditReject builds its payload in
    __init__ — so 20 raise sites across editing.py and reading.py were invisible,
    and three of their values reached clients while appearing in neither
    KNOWN_REASONS nor DESIGN-5.0 §11.1's "closed" vocabulary. Asserting that the
    walk finds them is the only thing that stops the hole reopening silently.
    """
    seen = set()
    for path in MODULES:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        seen.update(r for _ln, r in _constructed_reasons(tree) if r)
    assert {"would_empty_file", "batch_aborted", "out_of_range"} <= seen
    assert seen <= KNOWN_REASONS


def test_the_design_doc_vocabulary_matches_the_code():
    """§11.1 calls the vocabulary closed; nothing made that true.

    KNOWN_REASONS and the table in DESIGN-5.0-raw-surface.md had already drifted
    (bad_pattern was in one and not the other) with no test comparing them. A
    client writes its `reason` switch from the DOC, so the doc is the half that
    has to be right.
    """
    doc = (pathlib.Path(__file__).resolve().parents[1]
           / "docs" / "ERROR-REASONS.md").read_text(encoding="utf-8")
    missing = sorted(
        r for r in (KNOWN_REASONS - M1_REASONS - STRUCTURED_OUTPUT_REASONS
                    - M2_REASONS - SCHEMA_VERSION_REASONS)
        if f"`{r}`" not in doc
    )
    assert not missing, (
        f"reason value(s) {missing} exist in the code but are not documented in "
        "docs/ERROR-REASONS.md."
    )
    structured_doc = doc
    missing_structured = sorted(r for r in STRUCTURED_OUTPUT_REASONS if f"`{r}`" not in structured_doc)
    assert not missing_structured, (
        f"10.1 structured-output reason(s) {missing_structured} are missing from "
        "the design that introduces their result-validation behavior."
    )
    milestone_doc = doc
    missing_m2 = sorted(r for r in M2_REASONS if f"`{r}`" not in milestone_doc)
    assert not missing_m2
