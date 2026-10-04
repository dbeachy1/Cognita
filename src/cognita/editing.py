"""Anchored surgical edits — pure logic for the `edit_document` gateway tool.

Design: DESIGN-2.0-edit-document.md. No I/O here: the proxy reads the file,
calls apply_edit(), and forwards the resulting full content to the worker's
update_document (which owns the disk write and reindex).

All matching and splicing happens in LF-space — file text and both arguments are
newline-normalized first, because CRLF files + LF-flavored anchors is the #1
real-world mismatch. That is a MATCHING convenience and nothing more: since 5.6
the proxy passes the result through restore_line_endings() before the write, so
the file keeps its own newlines and its BOM. Editing one line of a CRLF document
used to re-flavor every other line in it — an unrequested rewrite of content
this server exists to store unaltered.

old_str is always a LITERAL — never a regex or glob, no case folding, no
whitespace collapsing. Fuzzy matching is used only to build not-found hints,
never to apply an edit.
"""

from __future__ import annotations

import difflib
import hashlib
import re
from bisect import bisect_right
from collections import Counter
from dataclasses import dataclass

EDIT_TOOL_NAME = "edit_document"
BATCH_TOOL_NAME = "edit_document_batch"

# Guard rails (see DESIGN-2.0-edit-document.md §3).
MAX_FILE_BYTES = 5_000_000
BATCH_MAX_EDITS = 50
DIFF_CONTEXT_LINES = 3
DIFF_MAX_LINES = 40
DIFF_MAX_CHARS = 2000
HINT_MAX_LINES = 12
HINT_MAX_CHARS = 1000
HINT_CUTOFF = 0.5
HINT_MAX_FILE_LINES = 5000  # skip hint search on pathological files
AMBIGUOUS_MATCHES_LISTED = 5

# Staleness guard (2.7): read_document stamps every response with a hash of the
# normalized text; the write tools accept expected_sha256 and refuse to write
# if the file changed since that read. Normalized hashing means an EOL-only
# rewrite (OneDrive/platform churn) does not false-positive as stale.
SHA_PREFIX_MIN = 12

EXPECTED_SHA_PROPERTY: dict = {
    "type": "string",
    "description": (
        "Optional staleness guard: the content_sha256 from a prior read_document. "
        f"Full hash or a prefix of at least {SHA_PREFIX_MIN} characters. If the "
        "file's current content no longer matches, the write is rejected with "
        "reason=stale_file — re-read and rebuild the edit. Recommended on every "
        "write when the read and the write may be separated in time."
    ),
}

# MCP tool definition injected into writable projects' tools/list responses.
EDIT_TOOL_DEF: dict = {
    "name": EDIT_TOOL_NAME,
    "description": (
        "Surgically edit an existing document by replacing an exact substring — use this "
        "instead of update_document for localized changes; you do not need to send (or even "
        "hold) the whole file. Mutating: the file is backed up automatically, overwritten on "
        "disk, and re-indexed immediately. old_str must match the current file verbatim and "
        "occur exactly once (or set replace_all=true to change every occurrence); zero "
        "matches or unwanted multiples reject the call and nothing changes — extend old_str "
        "with surrounding lines to disambiguate. old_str may span many lines: to rewrite a "
        "section, anchor on the whole section. Pass an empty new_str to delete. Making "
        "MORE THAN ONE change to the same file? Prefer edit_document_batch — one backup "
        "and one re-index for the whole set instead of one per change. Set dry_run:true "
        "to validate the match and preview the diff WITHOUT writing — useful before "
        "editing a file you haven't read recently. The result's previous_backup_id "
        "names the backup this call took — pass it to restore_backup to undo exactly "
        "this write. Use update_document for full rewrites, add_document to create, "
        "remove_document to delete files. Changes are immediately searchable."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "filepath": {
                "type": "string",
                "description": (
                    "Path to an already-indexed document (absolute, as returned by "
                    "search/list results, or relative to the project's documents folder)."
                ),
            },
            "old_str": {
                "type": "string",
                "description": (
                    "Exact text to replace, verbatim including whitespace. May span many "
                    "lines (a whole section). Must occur exactly once unless "
                    "replace_all=true."
                ),
            },
            "new_str": {
                "type": "string",
                "description": "Replacement text. Empty string deletes old_str.",
            },
            "replace_all": {
                "type": "boolean",
                "default": False,
                "description": (
                    "Replace every occurrence of old_str instead of requiring exactly one "
                    "match."
                ),
            },
            "dry_run": {
                "type": "boolean",
                "default": False,
                "description": (
                    "Validate the match and return the would-be diff WITHOUT writing "
                    "anything. Use to test an anchor before mutating."
                ),
            },
            "expected_sha256": EXPECTED_SHA_PROPERTY,
            "expected_bytes_sha256": {
                "type": "string",
                "description": "Optional exact SHA-256 of current persisted bytes (64 hexadecimal characters).",
            },
        },
        "required": ["filepath", "old_str", "new_str"],
    },
}

BATCH_TOOL_DEF: dict = {
    "name": BATCH_TOOL_NAME,
    "description": (
        "Apply several surgical edits to ONE document in a single atomic call. PREFER "
        "this over repeated edit_document calls whenever you have more than one change "
        "to the same file — it is much more efficient: ONE automatic backup and ONE "
        "re-index for the whole set, instead of one per change. Edits are applied in "
        "order, each old_str matched against the file as already modified by the "
        "preceding edits; every old_str must match exactly once (or set "
        "replace_all:true on that item). If ANY edit fails to match, the ENTIRE batch "
        "is rejected and nothing is written — the error names the failing edit index "
        "with a hint (edits_applied is 0; the file is untouched). The result reports "
        "per-edit replacement counts and a unified diff of the full change. Set "
        "dry_run:true to validate every anchor and preview the combined diff WITHOUT "
        "writing — recommended before large batches. The result's previous_backup_id "
        "names the ONE backup the batch took — pass it to restore_backup to undo the "
        "whole set. Changes are immediately searchable."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "filepath": {
                "type": "string",
                "description": (
                    "Path to an already-indexed document (absolute, as returned by "
                    "search/list results, or relative to the project's documents folder)."
                ),
            },
            "edits": {
                "type": "array",
                "minItems": 1,
                "maxItems": BATCH_MAX_EDITS,
                "description": (
                    "Applied in order; each old_str is matched against the file as "
                    "already modified by the preceding edits in this batch."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "old_str": {
                            "type": "string",
                            "description": (
                                "Exact text to replace, verbatim including whitespace. "
                                "Must occur exactly once unless replace_all=true."
                            ),
                        },
                        "new_str": {
                            "type": "string",
                            "description": "Replacement text. Empty string deletes old_str.",
                        },
                        "replace_all": {
                            "type": "boolean",
                            "default": False,
                            "description": "Replace every occurrence of this old_str.",
                        },
                    },
                    "required": ["old_str", "new_str"],
                },
            },
            "dry_run": {
                "type": "boolean",
                "default": False,
                "description": (
                    "Validate every edit and return the would-be combined diff WITHOUT "
                    "writing anything."
                ),
            },
            "expected_sha256": EXPECTED_SHA_PROPERTY,
            "expected_bytes_sha256": {
                "type": "string",
                "description": "Optional exact SHA-256 of current persisted bytes (64 hexadecimal characters).",
            },
        },
        "required": ["filepath", "edits"],
    },
}


class EditReject(Exception):
    """Edit refused — nothing was (or will be) written.

    Carries the tool-result payload returned to the caller (status:error JSON in
    a successful tool result, matching the engine's own error convention).
    """

    def __init__(self, reason: str, message: str, **fields):
        super().__init__(message)
        payload = {"status": "error", "reason": reason, "message": message}
        payload.update({k: v for k, v in fields.items() if v not in ("", None)})
        self.payload = payload


@dataclass
class EditOutcome:
    """A successful splice, ready to hand to update_document."""

    new_content: str  # \n-normalized full text (see module docstring for why)
    replacements: int
    match_mode: str  # "exact" | "newline_normalized"
    context_diff: str


@dataclass
class BatchOutcome:
    """A successful ordered batch of splices — one write, one reindex."""

    new_content: str  # \n-normalized final text
    edits: list  # per-edit: {index, replacements, match_mode}
    context_diff: str  # unified diff, original -> final, capped


def _normalize(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


_EOL = re.compile("\r\n|\r|\n")
BOM = "\ufeff"


def strip_bom(text: str) -> str:
    return text[1:] if text.startswith(BOM) else text


def restore_line_endings(original: str, edited_norm: str) -> str:
    """Re-dress edited text in the file's OWN newlines and BOM (5.6).

    The edit functions work on newline-normalized text deliberately: an anchor
    written with LF has to match a CRLF file, sections and line numbers have to
    mean one thing regardless of flavor, and every one of them is a pure
    function over normalized text. What was wrong was WRITING that normalized
    text back. A single edit to one line of a CRLF file rewrote all 4000 of its
    line endings and deleted its BOM — a diff the caller never asked for, on
    content Cognita is only supposed to store.

    Untouched lines get back the exact separator they had, matched positionally
    through a line-level diff, so this is byte-exact even on a mixed-ending file
    away from the edit. Lines the edit introduced get the file's dominant
    separator, which is the only defensible guess and is what an editor would do.
    """
    seps = _EOL.findall(original)
    had_bom = original.startswith(BOM)
    if not seps and not had_bom:
        return edited_norm  # single-line, LF-only: nothing to restore
    # Exactly one BOM if the file had one, none if it did not, whether or not
    # the caller already stripped it before editing. The proxy does strip it;
    # a helper that doubles the mark when handed unstripped text is a trap.
    if had_bom:
        edited_norm = strip_bom(edited_norm)
    old_lines = strip_bom(_normalize(original)).split("\n")
    new_lines = edited_norm.split("\n")
    dominant = Counter(seps).most_common(1)[0][0] if seps else "\n"

    # new-line index -> old-line index, for lines the edit left alone
    same: dict[int, int] = {}
    matcher = difflib.SequenceMatcher(None, old_lines, new_lines, autojunk=False)
    for tag, i1, _i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            for offset in range(j2 - j1):
                same[j1 + offset] = i1 + offset

    parts: list[str] = [BOM] if had_bom else []
    for index, line in enumerate(new_lines):
        parts.append(line)
        if index == len(new_lines) - 1:
            break
        source = same.get(index)
        parts.append(seps[source] if source is not None and source < len(seps) else dominant)
    return "".join(parts)


def content_sha256(text: str) -> str:
    """Version stamp of a document's content as the matcher sees it (2.7)."""
    return hashlib.sha256(_normalize(text).encode("utf-8")).hexdigest()


def sha_matches(expected: str, actual: str) -> bool:
    """Full or prefix (>= SHA_PREFIX_MIN chars) comparison."""
    return len(expected) >= SHA_PREFIX_MIN and actual.startswith(expected.lower())


def _find_all(haystack: str, needle: str) -> list[int]:
    positions, start = [], 0
    while True:
        i = haystack.find(needle, start)
        if i < 0:
            return positions
        positions.append(i)
        start = i + len(needle)  # non-overlapping, like str.replace


def _line_starts(text: str) -> list[int]:
    starts = [0]
    for i, ch in enumerate(text):
        if ch == "\n":
            starts.append(i + 1)
    return starts


def _line_of(starts: list[int], offset: int) -> int:
    """0-indexed line containing `offset`."""
    return bisect_right(starts, offset) - 1


def apply_edit(
    text: str, old_str: str, new_str: str, replace_all: bool = False
) -> EditOutcome:
    """Match old_str in text (exact, then newline-normalized) and splice new_str.

    Raises EditReject for every refusal case; returns EditOutcome on success.
    Pure function — no disk, no network.
    """
    if not old_str:
        raise EditReject("invalid", "old_str must not be empty.")
    if old_str == new_str:
        raise EditReject("no_change", "old_str and new_str are identical; nothing to do.")

    norm = _normalize(text)
    # A BOM can now ride along in an anchor: read_document returns the file's
    # first character verbatim since 5.6, and the text matched against here is
    # BOM-stripped. Dropping it makes the copied anchor match instead of
    # failing on a character the caller cannot see.
    n_old = strip_bom(_normalize(old_str))
    n_new = strip_bom(_normalize(new_str))
    if n_old == n_new:
        raise EditReject(
            "no_change",
            "old_str and new_str differ only in line-ending flavor; nothing to change.",
        )

    positions = _find_all(norm, n_old)
    if not positions:
        raise EditReject(
            "not_found",
            "old_str was not found in the file. It must match the current content "
            "verbatim (whitespace included).",
            replacements=0,
            hint=_near_miss_hint(norm, n_old),
        )
    if len(positions) > 1 and not replace_all:
        starts = _line_starts(norm)
        raise EditReject(
            "ambiguous",
            f"old_str matched {len(positions)} times; nothing was changed.",
            match_count=len(positions),
            matches=[
                {"line": _line_of(starts, p) + 1}
                for p in positions[:AMBIGUOUS_MATCHES_LISTED]
            ],
            hint=(
                "Extend old_str with surrounding context so it matches exactly once, "
                "or set replace_all=true if you meant all of them."
            ),
        )

    # Splice all (one, unless replace_all) occurrences; track spans in NEW text.
    delta = len(n_new) - len(n_old)
    new_norm = norm.replace(n_old, n_new)
    new_spans = [(p + i * delta, p + i * delta + len(n_new)) for i, p in enumerate(positions)]

    if not new_norm.strip():
        raise EditReject(
            "would_empty_file",
            "This edit would leave the file empty. Use remove_document to delete files.",
        )

    return EditOutcome(
        new_content=new_norm,
        replacements=len(positions),
        match_mode="exact" if old_str in text else "newline_normalized",
        context_diff=_context_diff(new_norm, new_spans),
    )


def _context_diff(new_norm: str, spans: list[tuple[int, int]]) -> str:
    """Post-edit context: ±N lines around each change, changed lines marked '>'.

    Lets the caller verify the edit landed without a get_document round-trip.
    Capped so replace_all over a big file cannot blow up the response.
    """
    lines = new_norm.split("\n")
    starts = _line_starts(new_norm)
    chunks: list[str] = []
    for s, e in spans:
        ls = _line_of(starts, s)
        le = _line_of(starts, max(s, e - 1)) if e > s else ls
        lo, hi = max(0, ls - DIFF_CONTEXT_LINES), min(len(lines) - 1, le + DIFF_CONTEXT_LINES)
        part = [f"@@ line {ls + 1} @@"]
        for i in range(lo, hi + 1):
            changed = e > s and ls <= i <= le
            part.append(("> " if changed else "  ") + lines[i])
        chunks.append("\n".join(part))
    diff = "\n".join(chunks)

    out_lines = diff.split("\n")
    if len(out_lines) > DIFF_MAX_LINES or len(diff) > DIFF_MAX_CHARS:
        diff = "\n".join(out_lines[:DIFF_MAX_LINES])[:DIFF_MAX_CHARS] + "\n… (diff truncated)"
    return diff


def apply_batch(text: str, edits: list) -> BatchOutcome:
    """Apply an ordered list of anchored edits atomically (all-or-nothing).

    Each edit's old_str is matched against the text as modified by the
    preceding edits in the batch (spec semantics: a later edit can reference
    what an earlier one produced). Any failure aborts the WHOLE batch with a
    batch_aborted payload naming the failing index — the caller writes nothing.
    Pure function, like apply_edit.
    """
    if not isinstance(edits, list) or not edits:
        raise EditReject("invalid", "edits must be a non-empty array of {old_str, new_str}.")
    if len(edits) > BATCH_MAX_EDITS:
        raise EditReject(
            "invalid", f"Too many edits in one batch ({len(edits)}; max {BATCH_MAX_EDITS})."
        )

    original = _normalize(text)
    current = text
    meta: list[dict] = []
    for i, e in enumerate(edits):
        if (
            not isinstance(e, dict)
            or not isinstance(e.get("old_str"), str)
            or not isinstance(e.get("new_str"), str)
        ):
            raise EditReject(
                "invalid",
                f"edit[{i}] must be an object with old_str and new_str strings.",
                failed_edit=i,
                edits_applied=0,
            )
        try:
            out = apply_edit(current, e["old_str"], e["new_str"], bool(e.get("replace_all", False)))
        except EditReject as exc:
            inner = dict(exc.payload)
            inner.pop("status", None)
            reason = inner.pop("reason", "error")
            msg = inner.pop("message", str(exc))
            raise EditReject(
                "batch_aborted",
                f"edit[{i}] failed ({reason}): {msg} "
                "The batch was rejected; NOTHING was written.",
                failed_edit=i,
                failed_reason=reason,
                edits_applied=0,
                **inner,
            ) from exc
        meta.append({"index": i, "replacements": out.replacements, "match_mode": out.match_mode})
        current = out.new_content

    return BatchOutcome(
        new_content=current,
        edits=meta,
        context_diff=_unified_context_diff(original, current),
    )


def _unified_context_diff(
    before: str,
    after: str,
    fromfile: str = "before",
    tofile: str = "after",
    max_lines: int = DIFF_MAX_LINES,
    max_chars: int = DIFF_MAX_CHARS,
) -> str:
    """Unified diff (before -> after), capped.

    Used for batch results (changes spread across a file merge into standard
    hunks instead of overlapping per-edit windows), restore previews, and
    diff_backup history views (which pass larger caps + real labels).
    """
    lines = list(
        difflib.unified_diff(
            before.split("\n"), after.split("\n"),
            fromfile=fromfile, tofile=tofile,
            n=DIFF_CONTEXT_LINES, lineterm="",
        )
    )
    diff = "\n".join(lines)
    if len(lines) > max_lines or len(diff) > max_chars:
        diff = "\n".join(lines[:max_lines])[:max_chars] + "\n… (diff truncated)"
    return diff


def _near_miss_hint(norm: str, n_old: str) -> str:
    """Closest region of the file to the failed anchor, verbatim, capped.

    Returned so the caller can retry with a corrected old_str in ONE shot
    instead of re-reading the whole document. Hint-only: never used to apply.
    """
    lines = norm.split("\n")
    if len(lines) > HINT_MAX_FILE_LINES:
        return ""
    width = max(1, len(n_old.split("\n")))
    if width > 60:
        return ""  # windows would cost O(lines*width) memory; anchor is huge anyway
    windows = ["\n".join(lines[i : i + width]) for i in range(max(1, len(lines) - width + 1))]
    close = difflib.get_close_matches(n_old, windows, n=1, cutoff=HINT_CUTOFF)
    if not close:
        return ""
    at = windows.index(close[0])
    lo, hi = max(0, at - 1), min(len(lines), at + width + 1)
    hint = "\n".join(lines[lo:hi][:HINT_MAX_LINES])[:HINT_MAX_CHARS]
    return f"Closest region in the file (line {lo + 1}):\n{hint}"
