"""Exhaustive literal / regex search across a corpus — the `find_literal` tool.

Why this exists: every other retrieval path on this server is RANKED. Hybrid
search, keyword search, similarity — they all answer "the best N documents for
this query" and have no notion of completeness. That is the right design for
"documents about X" and the wrong one for "every place I wrote X", because a
ranked engine cannot tell you it found everything and will happily return three
of five hits.

The 2026-08-01 case that motivated it: a stale, hyphenated filename appeared
twice in one document. `search_knowledge` at hybrid_alpha=0.0 with
max_results=20 returned one result, and neither real occurrence — BM25 splits
such filenames into tokens that are near-universal in a folder with similar
names, so IDF collapses and the query carries no
signal. No analyzer tuning fixes that; ranked retrieval is simply a different
operation. What was missing was grep.

The composition this completes is  find_literal -> edit_document_batch:
locate every occurrence, then fix them atomically.

Two deliberate design decisions, both load-bearing:

  * The corpus is the INDEX's document list, but the content is read from
    DISK. Walking the index keeps find_literal and list_documents agreeing on
    what "the corpus" is (and gets backups/ exclusion for free — the backup
    tree is never indexed, so a rename can't drown in hits from old
    snapshots). Reading from disk means a match cannot be hidden by chunk
    boundaries, stale embeddings, or a document being registered-tier with no
    chunks at all. An index entry whose file has vanished is counted as
    missing, never silently treated as "no match".

  * Registered-tier files (.py, .sh — no embeddings, invisible to semantic
    search) are INCLUDED by default. They are exactly the files most likely to
    hold a hardcoded stale path, and literal search is the only retrieval
    mechanism that reaches them properly.

Pure logic only — no I/O, no store access. engine_local.py does the walk.
"""

from __future__ import annotations

import re
from bisect import bisect_right
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Iterator

FIND_LITERAL_TOOL_NAME = "find_literal"

MAX_MATCHES_DEFAULT = 200
MAX_MATCHES_CEILING = 1000
MAX_CONTEXT_LINES = 3
BINARY_SNIFF_BYTES = 8192
SKIPPED_LISTED = 25  # cap on the per-file skip detail list; the COUNT is always exact


FIND_LITERAL_TOOL_DEF: dict = {
    "name": FIND_LITERAL_TOOL_NAME,
    "description": (
        "Find every literal occurrence of a string across the corpus — exhaustive, "
        "exact, unranked. Read-only. Unlike search_knowledge, which returns the best "
        "N matches by relevance, this returns ALL matches with filepath and line "
        "number, and a zero-match result is a trustworthy 'it appears nowhere'. Use "
        "for renames, stale cross-references, hardcoded paths, and any 'where else "
        "did I write this?' question. Reads live file content from disk, so chunk "
        "boundaries and index staleness cannot hide a match, and it covers "
        "registered-tier files (.py, .sh) that have no embeddings and cannot be "
        "reached semantically at all. Line numbers are 1-indexed and line up with "
        "read_document's start_line/end_line, so results feed straight back in; "
        "compose with edit_document_batch to fix what it finds. Use search_knowledge "
        "instead for topical or conceptual lookup — this tool has no notion of "
        "meaning, and does no stemming, fuzzy matching or synonyms."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "pattern": {
                "type": "string",
                "description": (
                    "The exact substring to find. Treated literally (no escaping "
                    "needed) unless regex=true."
                ),
            },
            "regex": {
                "type": "boolean",
                "default": False,
                "description": (
                    "Treat pattern as a Python regular expression. Anchors ^ and $ "
                    "apply per line. A bad pattern returns reason='bad_pattern', "
                    "never a crash."
                ),
            },
            "case_sensitive": {
                "type": "boolean",
                "default": True,
                "description": (
                    "Default true — literal search is for exact strings. Set false "
                    "to fold case."
                ),
            },
            "filepath_glob": {
                "type": "string",
                "description": (
                    "Restrict the walk. A pattern with no '/' matches the FILENAME "
                    "anywhere in the tree ('*.py' finds every Python file at any "
                    "depth); a pattern with '/' matches the whole path relative to "
                    "the documents folder ('_shared/*.md', 'Equestria/**/*.json'). "
                    "'*' NEVER crosses a '/', exactly as in a shell: "
                    "'Wildcards/*.txt' means files DIRECTLY in Wildcards and finds "
                    "nothing if they all live one level deeper — use "
                    "'Wildcards/**/*.txt' for the subtree. If a glob selects no "
                    "documents at all the result says so explicitly "
                    "(reason='no_documents_selected'), so a zero here is never "
                    "ambiguous between 'filtered everything out' and 'string not "
                    "present'."
                ),
            },
            "category": {
                "type": "string",
                "description": "Optional category filter; see list_categories().",
            },
            "max_matches": {
                "type": "integer",
                "default": MAX_MATCHES_DEFAULT,
                "description": (
                    f"Cap on matches RETURNED (max {MAX_MATCHES_CEILING}); every "
                    "file is still scanned. On overflow the response sets "
                    "truncated=true and total_matches still reports the true count "
                    "— results are never silently dropped."
                ),
            },
            "context_lines": {
                "type": "integer",
                "default": 0,
                "description": (
                    f"Surrounding lines to include per match, 0-{MAX_CONTEXT_LINES}. "
                    "Default 0 keeps responses tight."
                ),
            },
            "include_registered": {
                "type": "boolean",
                "default": True,
                "description": (
                    "Include registered-tier files (.py, .sh — no embeddings). True "
                    "by default and usually what you want: those files are "
                    "unreachable by semantic search, so this is the only tool that "
                    "can see inside them."
                ),
            },
            "compact": {
                "type": "boolean",
                "default": False,
                "description": (
                    "Opt in to the compact collection response. The legacy "
                    "results alias is omitted; matches, result_key, counts, "
                    "truncation and diagnostics are retained."
                ),
            },
        },
        "required": ["pattern"],
    },
}


class BadPattern(ValueError):
    """The caller's pattern could not be compiled (or was empty)."""


@dataclass(slots=True)
class Matcher:
    """A compiled needle. Literal and regex modes share one code path — a
    literal is just its own re.escape() — so line handling, overlap rules and
    the zero-width guard cannot drift between the two."""

    pattern: str
    regex: bool
    case_sensitive: bool
    compiled: re.Pattern

    def finditer(self, line: str) -> Iterator[re.Match]:
        return self.compiled.finditer(line)


def build_matcher(pattern: str, regex: bool = False, case_sensitive: bool = True) -> Matcher:
    """Compile a search pattern. Raises BadPattern with the compile error's own
    message — the caller turns that into reason='bad_pattern', never a traceback."""
    if not pattern:
        raise BadPattern("pattern must not be empty.")
    # MULTILINE is load-bearing now that scan_text matches against the whole
    # document instead of line by line: the tool description promises "anchors ^
    # and $ apply per line", and per-line scanning used to deliver that for free.
    # Without this flag ^ would silently mean "start of file" — a wrong answer
    # from a tool whose entire contract is that its zeroes are trustworthy.
    flags = re.MULTILINE
    if not case_sensitive:
        flags |= re.IGNORECASE
    try:
        compiled = re.compile(pattern if regex else re.escape(pattern), flags)
    except re.error as exc:
        raise BadPattern(f"Invalid regular expression: {exc}") from exc
    return Matcher(pattern=pattern, regex=regex, case_sensitive=case_sensitive,
                   compiled=compiled)


def glob_matches(rel_path: str, pattern: str) -> bool:
    """Does a documents-dir-relative path satisfy `filepath_glob`?

    A pattern without a separator matches the BASENAME, the way ripgrep's
    --glob does. Strict whole-path matching would make the obvious '*.py'
    silently return only top-level files — a silent miss, which is the single
    thing an exhaustiveness tool must never produce.
    """
    path = PurePosixPath(rel_path)
    target = PurePosixPath(path.name) if "/" not in pattern else path
    try:
        return target.full_match(pattern)
    except ValueError:  # malformed pattern (e.g. bare '**' fragments)
        return False


def looks_binary(head: bytes) -> bool:
    """Null-byte sniff of a file's first bytes. Cheap, and the same heuristic
    grep uses — a PNG must be skipped, not decoded and not raised over."""
    return b"\x00" in head[:BINARY_SNIFF_BYTES]


def normalize(text: str) -> str:
    """CRLF/CR -> LF, identical to editing._normalize.

    Line numbers here have to mean the same thing as read_document's
    start_line/end_line or the two tools cannot compose, and read_document
    numbers lines of the NORMALIZED text.
    """
    return text.replace("\r\n", "\n").replace("\r", "\n")


def scan_text(text: str, matcher: Matcher, context_lines: int = 0) -> Iterator[dict]:
    """Yield one record per occurrence, in line then column order.

    line_number and column are both 1-INDEXED. (The spec's worked example
    happened to show 0-indexed columns; mixing the two bases inside one record
    is a defect generator, and every editor and compiler reports line:col
    1-indexed, so both are 1-indexed here and the tool schema says so.)

    Zero-width regex matches are dropped: a match that spans no characters is
    not an occurrence of anything, and 'a*' would otherwise emit one record per
    character position in the corpus.
    """
    # Scan the WHOLE text and derive line/column from the match offset, rather
    # than running the matcher per line. Per-line scanning meant a pattern
    # containing a newline could never match ANYTHING — and _find_literal
    # reported that as reason: "no_matches" with "this is an exhaustive search,
    # so the answer is 'not present', not 'not found yet'". A silent miss dressed
    # as a trustworthy zero is the one answer DESIGN-4.5 says this tool must
    # never give, and it defeated the tool's own motivating use case: sweeping
    # for a stale multi-line block before a rename.
    body = normalize(text)
    lines = body.split("\n")
    # Start offset of each line, for O(log n) offset -> line lookups.
    starts: list[int] = []
    pos = 0
    for line in lines:
        starts.append(pos)
        pos += len(line) + 1  # +1 for the "\n" split consumed
    ctx = max(0, min(int(context_lines), MAX_CONTEXT_LINES))
    for m in matcher.finditer(body):
        if m.end() == m.start():
            continue
        idx = bisect_right(starts, m.start()) - 1
        line = lines[idx]
        record = {
            "line_number": idx + 1,
            "column": m.start() - starts[idx] + 1,
            "line": line,
            "match": m.group(0),
        }
        if ctx:
            record["context_before"] = lines[max(0, idx - ctx):idx]
            # A multi-line match ends on a later line; context follows the END,
            # or it would repeat lines the match itself already covers.
            end_idx = bisect_right(starts, max(m.start(), m.end() - 1)) - 1
            record["context_after"] = lines[end_idx + 1:end_idx + 1 + ctx]
        else:
            record["context_before"] = []
            record["context_after"] = []
        yield record
