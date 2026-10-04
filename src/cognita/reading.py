"""Ranged / section-addressed document reads — the `read_document` gateway tool.

Building an edit_document anchor used to require get_document on the whole
file, even for a small write. This tool reads straight from disk in the gateway and
returns the file VERBATIM — its own line endings and BOM, byte for byte. Anchors
built from it still cannot mismatch, because the matcher normalizes the anchor
as well as the file (editing.apply_edit); it never needed the read to be folded.

Workflow it enables on big files:
    search_knowledge (locate) -> read_document (verbatim anchor bytes)
        -> edit_document / edit_document_batch (surgical write)
The full file never enters the model's context.

Pure logic only — the proxy does the disk I/O and hands text in.
"""

from __future__ import annotations

import re

from dataclasses import dataclass

from .editing import (
    EXPECTED_SHA_PROPERTY,
    EditReject,
    _context_diff,
    _normalize,
    content_sha256,
)

# 5.6: reads hand back the file, not a re-flavored copy of it.
#
# read_slice does all of its ADDRESSING on newline-normalized text — line
# numbers, section boundaries and the truncation budget have to mean the same
# thing whatever line endings a file happens to use, and the edit matcher
# normalizes anchors too, so nothing about matching depends on the text it
# returns being folded. The folding was only ever in the JOIN, and a client
# byte-comparing a CRLF file it had pushed saw a mismatch on a file the server
# had stored perfectly.
_EOL = re.compile("\r\n|\r|\n")
_BOM = "\ufeff"


def _without_bom(text: str) -> str:
    return text[1:] if text.startswith(_BOM) else text


def _rejoin(pieces: list[str], separators: list[str]) -> str:
    """pieces[0] + separators[0] + pieces[1] + ... — the file's own newlines."""
    out = [pieces[0]] if pieces else []
    for piece, sep in zip(pieces[1:], separators):
        out.append(sep)
        out.append(piece)
    return "".join(out)


READ_TOOL_NAME = "read_document"
INSERT_TOOL_NAME = "insert_in_document"

READ_MAX_LINES = 400
READ_MAX_CHARS = 24_000
HEADERS_LISTED = 30  # cap for the "available sections" hint

READ_TOOL_DEF: dict = {
    "name": READ_TOOL_NAME,
    "description": (
        "Read part of a document straight from disk — the exact text the edit tools "
        "match against. Prefer this over get_document whenever you only need a section "
        "or a line range (building an edit_document anchor, checking one part of a big "
        "file): search_knowledge to locate, read_document for the text, edit_document "
        "to change. Pass `section` (a markdown heading, with or without leading #'s, "
        "case-insensitive) to get that whole section including its subsections; or "
        "start_line/end_line (1-indexed, inclusive); or neither for the whole file "
        "(large files are truncated — the response says so and reports total_lines for "
        "a follow-up ranged read). "
        "For valid UTF-8, returned `text` is BYTE-VERBATIM: it preserves the file's own line endings, BOM "
        "and whitespace exactly, so a whole-file read hashes to bytes_sha256. Accepted "
        "malformed UTF-8 is returned as a visibly lossy U+FFFD view; use get_document "
        "with content_encoding=base64 for exact bytes. Anchors copied from valid text "
        "still match edit_document, which "
        "normalizes both the file and old_str before comparing. `content_sha256` is "
        "the separate write-guard stamp (BOM dropped, CRLF/CR folded to LF) — a "
        "version stamp rather than a description of these bytes; pass it as "
        "expected_sha256 on a later write to guard against the file changing "
        "underneath you, and use bytes_sha256 for byte comparisons. Read-only, no "
        "side effects."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "filepath": {
                "type": "string",
                "description": (
                    "Path to a document (absolute, as returned by search/list results, "
                    "or relative to the project's documents folder)."
                ),
            },
            "section": {
                "type": "string",
                "description": (
                    "A markdown heading (e.g. 'Section Two' or '## Section Two'). "
                    "Returns that heading through the end of its section, subsections "
                    "included. Mutually exclusive with start_line/end_line."
                ),
            },
            "start_line": {
                "type": "integer",
                "description": "First line to return (1-indexed, inclusive).",
            },
            "end_line": {
                "type": "integer",
                "description": "Last line to return (1-indexed, inclusive).",
            },
        },
        "required": ["filepath"],
    },
}

INSERT_TOOL_DEF: dict = {
    "name": INSERT_TOOL_NAME,
    "description": (
        "Insert new text into a document WITHOUT rewriting any existing bytes — for "
        "ADDING content, prefer this over edit_document (no anchor to quote, nothing "
        "existing gets regenerated). position: 'start' (top of file), 'end' (bottom), "
        "'end_of_section' with section (a markdown heading, with or without #'s, "
        "case-insensitive) to append inside that section right after its last content "
        "line — subsections INCLUDED, so on a heading with children this lands after "
        "the whole subtree — or 'end_of_intro' with section to append after the "
        "heading's OWN content only, before its first subheading (use this for a "
        "section's preamble; on an H1 it targets the document intro). Text is inserted "
        "verbatim on its own line(s); INTERIOR blank lines are preserved, but blank "
        "lines at the very start/end of text are trimmed (placement is always flush "
        "against the neighboring content line). Mutating: backed up automatically and "
        "re-indexed immediately, like every write. The result's previous_backup_id "
        "names that backup — pass it to restore_backup to undo exactly this write. "
        "Supports dry_run to preview placement."
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
            "text": {
                "type": "string",
                "description": "The new content, inserted verbatim on its own line(s).",
            },
            "position": {
                "type": "string",
                "enum": ["start", "end", "end_of_section", "end_of_intro"],
                "description": (
                    "Where to insert. end_of_section = after the section's last "
                    "content line, subsections included. end_of_intro = after the "
                    "heading's own content, BEFORE its first subheading. Both "
                    "require `section`."
                ),
            },
            "section": {
                "type": "string",
                "description": (
                    "For end_of_section / end_of_intro: the markdown heading of the "
                    "target section."
                ),
            },
            "dry_run": {
                "type": "boolean",
                "default": False,
                "description": "Preview the placement and diff WITHOUT writing.",
            },
            "expected_sha256": EXPECTED_SHA_PROPERTY,
            "expected_bytes_sha256": {
                "type": "string",
                "description": "Optional exact SHA-256 of current persisted bytes (64 hexadecimal characters).",
            },
        },
        "required": ["filepath", "text", "position"],
    },
}


@dataclass
class InsertOutcome:
    """A successful insertion, ready to hand to update_document."""

    new_content: str  # \n-normalized full text
    inserted_at_line: int  # 1-indexed first line of the inserted block
    context_diff: str


def apply_insert(
    text: str, insert_text: str, position: str, section: str | None = None
) -> InsertOutcome:
    """Insert a block at start / end / end-of-section / end-of-intro. Pure.

    end_of_section places the block after the section's last CONTENT line,
    subsections included (trailing blank lines stay below the new block, so
    spacing survives). end_of_intro stops at the section's FIRST subheading —
    the heading's own content only. An H1's section spans the whole document,
    so its preamble needs this mode."""
    _SECTION_POSITIONS = ("end_of_section", "end_of_intro")
    block = _normalize(insert_text).strip("\n")
    if not block.strip():
        raise EditReject("invalid", "text must not be empty.")
    if position not in ("start", "end", *_SECTION_POSITIONS):
        raise EditReject(
            "invalid",
            f"position must be start, end, end_of_section or end_of_intro (got {position!r}).",
        )
    if position in _SECTION_POSITIONS and not section:
        raise EditReject("invalid", f"position={position} requires `section`.")
    if section and position not in _SECTION_POSITIONS:
        raise EditReject(
            "invalid", "`section` only applies to end_of_section / end_of_intro."
        )

    norm = _normalize(text)
    lines = norm.split("\n")

    if position == "start":
        at = 0
    elif position == "end":
        at = len(lines)
        # a newline-terminated file splits to a phantom "" last element;
        # appending after it would gain a stray blank line (and end_of_section
        # already backs past trailing blanks — the two append modes must agree)
        while at > 0 and not lines[at - 1].strip():
            at -= 1
    else:
        idx, _level, _txt, end = locate_section(lines, section)
        if position == "end_of_intro":
            # own content only: the first heading of ANY level inside the
            # section closes the intro (all inner headings are deeper by
            # construction — a same-or-higher one would have closed the section)
            for i, _lvl, _t in _headers(lines):
                if idx < i < end:
                    end = i
                    break
        at = end
        while at - 1 > idx and not lines[at - 1].strip():
            at -= 1  # back up past trailing blank lines

    block_lines = block.split("\n")
    new_lines = lines[:at] + block_lines + lines[at:]
    new_norm = "\n".join(new_lines)

    start_off = sum(len(ln) + 1 for ln in new_lines[:at])  # +1 per newline
    span = (start_off, start_off + len(block))
    return InsertOutcome(
        new_content=new_norm,
        inserted_at_line=at + 1,
        context_diff=_context_diff(new_norm, [span]),
    )


_HEADER_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
_FENCE_RE = re.compile(r"^\s*(```|~~~)")


def _headers(lines: list[str]) -> list[tuple[int, int, str]]:
    """(line_index_0based, level, text) for every markdown heading.

    Fence-aware: '# comment' lines inside ``` / ~~~ code blocks are not
    headings and must not match (KB docs are full of shell snippets).
    A fence closes only on ITS OWN marker — per CommonMark a ``` line inside
    a ~~~ block is literal content (that's how you show a backtick fence),
    so treating the two as one interchangeable toggle corrupted heading
    detection after such blocks.
    """
    out: list[tuple[int, int, str]] = []
    fence: str | None = None  # the marker that opened the current block
    for i, line in enumerate(lines):
        m_fence = _FENCE_RE.match(line)
        if m_fence:
            marker = m_fence.group(1)
            if fence is None:
                fence = marker
            elif marker == fence:
                fence = None
            continue
        if fence is not None:
            continue
        m = _HEADER_RE.match(line)
        if m:
            out.append((i, len(m.group(1)), m.group(2)))
    return out


def _canon(heading: str) -> str:
    return heading.strip().lstrip("#").strip().casefold()


def locate_section(lines: list[str], section: str) -> tuple[int, int, str, int]:
    """Resolve a heading reference to (header_index, level, text, end_index).

    Indices are 0-based; end_index is exclusive — the next same-or-higher
    heading (or EOF) closes the section. Shared by read_document (section
    reads) and insert_in_document (end-of-section inserts) so both address
    sections identically. Raises EditReject with helpful hints."""
    headers = _headers(lines)
    want = _canon(section)
    matches = [(i, lvl, txt) for i, lvl, txt in headers if _canon(txt) == want]
    if not matches:
        available = ", ".join(
            f"{'#' * lvl} {txt} (line {i + 1})" for i, lvl, txt in headers[:HEADERS_LISTED]
        )
        raise EditReject(
            "not_found",
            f"No section heading matches {section!r}.",
            hint=f"Available sections: {available}" if available
            else "This document has no markdown headings.",
        )
    if len(matches) > 1:
        raise EditReject(
            "ambiguous",
            f"{len(matches)} headings match {section!r}.",
            matches=[{"line": i + 1, "heading": f"{'#' * lvl} {txt}"}
                     for i, lvl, txt in matches],
            hint="Use a more specific heading, or line-based addressing.",
        )
    idx, level, txt = matches[0]
    end = len(lines)
    for i, lvl, _t in headers:  # next same-or-higher heading closes the section
        if i > idx and lvl <= level:
            end = i
            break
    return idx, level, txt, end


def read_slice(
    text: str,
    start_line: int | None = None,
    end_line: int | None = None,
    section: str | None = None,
) -> dict:
    """Slice a document by line range or markdown section. Returns the payload
    dict for the tool result; raises EditReject on every refusal.

    Returned text is VERBATIM: the file's own line endings and BOM. Addressing —
    line numbers, sections, the truncation budget — and content_sha256 are all
    computed on a normalized view, so they mean the same thing whatever flavor
    a file is in, and anchors stay safe because the matcher normalizes them too.
    """
    norm = _normalize(text)
    lines = norm.split("\n")
    # The ORIGINAL separator that followed each line. _normalize rewrites every
    # CRLF and CR to LF and touches nothing else, so this split lands on exactly
    # the same boundaries as norm.split(LF): `lines` already holds verbatim line
    # CONTENT, and only the joins between them were being invented.
    seps = _EOL.findall(text)
    total = len(lines)

    has_range = start_line is not None or end_line is not None
    if section and has_range:
        raise EditReject(
            "invalid", "Pass either section OR start_line/end_line, not both."
        )

    payload: dict = {
        "status": "success",
        "total_lines": total,
        # whole-file version stamp, even for ranged reads — pass this as
        # expected_sha256 on a later write to guard against staleness
        # BOM-stripped on purpose: this is the write-guard currency that
        # expected_sha256 compares against and that list_documents reports, and
        # those three must never disagree. It is a HASH, not the document —
        # `text` below carries the BOM when the file has one.
        "content_sha256": content_sha256(_without_bom(norm)),
    }

    if section:
        idx, level, txt, end = locate_section(lines, section)
        # a section READ ends at its last CONTENT line — trailing blank
        # separator lines before the next heading are padding, not content
        # (and insert end_of_section already treats them that way; delivering
        # them made end_line point one past what the text visibly contains)
        while end - 1 > idx and not lines[end - 1].strip():
            end -= 1
        s, e = idx + 1, end
        payload["section"] = f"{'#' * level} {txt}"
    elif has_range:
        for name, v in (("start_line", start_line), ("end_line", end_line)):
            if v is not None and (isinstance(v, bool) or not isinstance(v, int)):
                raise EditReject("invalid", f"{name} must be an integer.")
        s = start_line if start_line is not None else 1
        e = end_line if end_line is not None else total
        # only flag start<=end when BOTH were given — a defaulted end is clamped
        if s < 1 or (end_line is not None and e < s):
            raise EditReject(
                "invalid", f"Bad line range {s}..{e} (1-indexed, start <= end)."
            )
        if s > total:
            raise EditReject(
                "out_of_range", f"start_line {s} is past the end of the file ({total} lines)."
            )
        e = min(e, total)
    else:
        s, e = 1, total

    body = lines[s - 1 : e]
    truncated = False
    if len(body) > READ_MAX_LINES:
        body = body[:READ_MAX_LINES]
        truncated = True
    joined = "\n".join(body)
    if len(joined) > READ_MAX_CHARS:
        cut = joined[:READ_MAX_CHARS]
        edge = cut.rfind("\n")
        if edge > 0:
            cut = cut[:edge]  # cut on a line edge, NO trailing newline — a
            # trailing "\n" made split() grow a phantom "" line, so end_line
            # overcounted and the resume message pointed past a line that was
            # never delivered (silently lost to the caller).
        body = cut.split("\n")
        truncated = True

    payload.update(
        start_line=s,
        end_line=s + len(body) - 1,
        truncated=truncated,
        # Rejoined with the file's OWN separators, not with LF. `body` is joined by
        # position rather than sliced out of `text` because the character budget
        # may have cut its last line mid-way, and a cut line has no separator of
        # its own to carry.
        text=_rejoin(body, seps[s - 1 : s - 2 + len(body)]),
    )
    if truncated:
        payload["message"] = (
            f"Truncated at line {s + len(body) - 1} of {total}; request the rest "
            "with start_line/end_line."
        )
    return payload
