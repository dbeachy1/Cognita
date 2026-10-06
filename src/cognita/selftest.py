"""The get_self_test_plan gateway tool — a server-versioned test protocol.

Why: the connector's capabilities grow release by release, and the test
    instructions were previously copied manually and could lag a release.
    This tool returns the current plan, so a client can request the Cognita
    self-test and execute the
returned steps. New features ship with their test steps in the same commit.

Every project-scoped operation in the plan uses the provisioned ``Self-Test``
project, including document, asset, OCR, read-only, and cleanup checks. The plan
uses only reserved self-test paths, so it never mutates real knowledge-base
documents. Document fixtures are removed at the end. Writable asset checks
create disposable PNGs and remove them with remove_asset; the historical a.png
and b.png paths are retained as untouched canaries.
"""

from __future__ import annotations

import base64
import functools
import hashlib
import logging
import re
from typing import NamedTuple

from . import __version__
from .release_identity import COMBINED_CONTRACT_VERSION

log = logging.getLogger("cognita.selftest")

SELFTEST_TOOL_NAME = "get_self_test_plan"

SELFTEST_TOOL_DEF: dict = {
    "name": SELFTEST_TOOL_NAME,
    "description": (
        "Return this server's current self-test plan — a versioned, ordered "
        "checklist exercising this connector's tool surface. Call this when the "
        "user asks to run the Cognita self-test, then EXECUTE the returned steps "
        "exactly, in order, and report the scorecard it asks for. The plan is "
        "self-contained for documents (it creates and deletes its own test files). "
        "Every project-scoped operation MUST use the exact provisioned Self-Test "
        "project, including ordinary document, byte-fidelity, search, manifest, "
        "copy, de-index, error-envelope, asset, OCR, read-only, and cleanup checks. "
        "Never select or test another project. First call list_projects and require "
        "the exact project name Self-Test, then include project=Self-Test on every "
        "project-scoped call in this plan. Asset checks preserve the historical PNG "
        "canaries without modifying them. It is updated server-side whenever "
        "functionality changes, so it is always current. It ends "
        "with two OPTIONAL sections a connector client cannot run — raw HTTPS transport "
        "checks and server-shell checks — which are reported SKIPPED, not failed. "
        "Two tools are deliberately NOT covered and their absence is not a gap: "
        "add_from_url (needs a stable external URL) and get_self_test_plan itself "
        "(calling it IS this step). reindex_documents and get_reindex_status appear "
        "ONLY in the optional server-shell section, because a full reindex of a live "
        "knowledge base is too expensive to run unconditionally. Read-only; the "
        "plan's steps do the testing. Optional section selects the stable index, "
        "a group, or one exact test while retaining shared safety/setup/cleanup "
        "instructions; omit it for the full plan."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "section": {
                "type": "string",
                "maxLength": 64,
                "description": (
                    "Optional stable section ID. Omit for the full plan; use "
                    "index for the catalog, a group ID (for example B), or an "
                    "exact test ID (for example B1 or R3)."
                ),
            },
        },
        "required": [],
    },
}

TEST_FILE = "cognita-selftest.md"
TEST_FILE_MOVED = "cognita-selftest-moved.md"  # step 13b move/rename target
DEINDEX_FILE = "cognita-selftest-deindex.md"  # D-steps: the durable de-index (5.7)
# 4.4 registered tier. A .py path so the tier is chosen by EXTENSION, which is
# the routing rule under test; the steps below never touch the .md file above,
# so the hash chain is unaffected.
TEST_SCRIPT = "cognita-selftest-script.py"
TEST_SCRIPT_AS_MD = "cognita-selftest-script.md"  # tier-crossing move target
# 4.5 literal search. Its OWN pair of files, deliberately: step 26 deletes
# TEST_SCRIPT, so the registered-tier reach check below cannot borrow it, and
# the .md file's markers must not perturb the hash chain above.
GREP_FILE = "cognita-selftest-grep.md"
GREP_SCRIPT = "cognita-selftest-grep.py"

ASSET_TEST_DIR = "cognita-selftest-assets"
# Historical paths retained from the 7.1 self-test. 10.1 names them only as
# protected canaries; no writable self-test step may read, alter, reindex, or
# remove them.
ASSET_TEST_A = f"{ASSET_TEST_DIR}/a.png"
ASSET_TEST_B = f"{ASSET_TEST_DIR}/b.png"
ASSET_RUN_DIR = f"{ASSET_TEST_DIR}/selftest-{{RUN}}"
ASSET_RUN_A = f"{ASSET_RUN_DIR}/a.png"
ASSET_RUN_B = f"{ASSET_RUN_DIR}/b.png"
ASSET_REMOVE_DISPOSABLE = f"{ASSET_TEST_DIR}/disposable-{{RUN}}.png"
ASSET_TEST_DATA_URL = (
    "data:image/png;base64,"
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8A"
    "AQUBAScY42YAAAAASUVORK5CYII="
)
ASSET_TEST_SIZE = 68
ASSET_TEST_SHA256 = "431ced6916a2a21a156e38701afe55bbd7f88969fbbfc56d7fe099d47f265460"

# The expected hash chain (10.0): the plan's steps are deterministic, so the
# file passes through five exact, known states. Pinning them turns the
# self-test into a byte-level canary for the whole write path — any drift in
# chunking, normalization, EOL handling or strip behavior shows up as a hash
# mismatch at a named step. These values are derived from the canonical 10.0
# fixture below and the exact edit/insert sequence in the plan. The regression
# test recomputes this chain from the same pure functions, so changing the test
# content or steps forces a conscious update.
EXPECTED_HASHES: dict[str, str] = {
    "H0": "fbd51a29598453da166e89511d6a6686a931e66edfdd98a1ed511c24e7bf6c0a",
    "H1": "ac3741e266b4f1c0bd7330f3d90d60ea5fdf6f7a9b8fdf439ca9547f1db22352",
    "H2": "7f5e720281cd7957651b9903809c912aac84ab3558ccc2422edea112a1b82db8",
    "H3": "3c31fec838c23250ba065c5fc3172658a6e13dda6ca082410a45d62536364f4a",
    "H4": "9ea421b873b0bb5b54514bab6fc2ced597a791164d62c0ab4e15517e234f59ab",
}


def _h(name: str) -> str:  # 16-char prefix for the plan text
    return EXPECTED_HASHES[name][:16]

_TEST_CONTENT = """# Selftest

intro line

## Alpha
alpha one
alpha two

### Alpha-child
child line

## Beta
beta one
"""

# A distinctive literal token: the registered tier is found by exact-token
# match, so the search steps need a string that cannot occur anywhere else in
# the corpus (and would be meaningless as a vector, which is the point).
SCRIPT_MARKER = "cognita_selftest_marker"

_TEST_SCRIPT_CONTENT = f"""#!/usr/bin/env python3
def {SCRIPT_MARKER}(sections_dir):
    return sorted(sections_dir.glob('*.md'))"""

# Literal-search fixtures (4.5). The counts in steps 27-30 are exact, so this
# text is load-bearing: line 3 holds one occurrence, line 4 holds two (proving
# multiple matches per line are not collapsed), line 5 differs only by case.
GREP_MARKER = "zzmarker_alpha"
_GREP_CONTENT = f"""# Grep Selftest

{GREP_MARKER} appears here
and {GREP_MARKER} appears twice on this line: {GREP_MARKER}
{GREP_MARKER.upper()} in caps
nothing on this line"""

# A hardcoded path inside a registered (.py) file — the real-world case the
# tool exists for, and the one thing no semantic search can ever reach.
GREP_REGISTERED_MARKER = "zzmarker_registered"

# A NON-DEFAULT category, deliberately. 4.4.2 fixed writes silently resetting a
# document's category to "general"; a canary that files everything under
# "general" could never have caught it, and cannot catch a recurrence.
GREP_CATEGORY = "selftest"

# 5.0 directory-tool fixtures. Their OWN directories, deliberately: copy and
# remove operate on whole prefixes, so anything sharing a directory with them
# would be swept up. The names are distinctive enough to be recognizable as test
# litter if a run aborts before its cleanup step.
COPY_SRC_DIR = "cognita-selftest-pack"
COPY_DST_DIR = "cognita-selftest-pack-copy"
COPY_FILES = ("one.md", "two.md", "three.txt")
COPY_MARKER = "zzmarker_pack"
# A cloud-sync conflict copy's name. add_document must REFUSE it: 5.0 stopped
# indexing these, and 4.6.0's rule is that Cognita never writes a file it cannot
# index — so accepting the write would create exactly the orphan both rules
# exist to prevent.
CONFLICT_NAME = "cognita-selftest-probe-PC-conflict.txt"

# Byte-fidelity fixtures. Steps B1-B3 are FIRST in the plan on purpose: a store
# that does not persist what it is handed is the highest-impact failure on this
# list, and a run that aborts halfway must not be the reason nobody found out.
# The extensions span both tiers (.md/.txt embedded, .py/.yml registered) because
# the strip was reported as possibly extension-conditional; it was not, and this
# is what keeps that answer true.
BYTES_FILE = "cognita-selftest-bytes"
SELF_TEST_CRLF_BYTES = b"a\r\nb\r\n"
SELF_TEST_CRLF_BASE64 = base64.b64encode(SELF_TEST_CRLF_BYTES).decode("ascii")
SELF_TEST_CRLF_SHA256 = hashlib.sha256(SELF_TEST_CRLF_BYTES).hexdigest()
BYTES_EXTENSIONS = (".md", ".txt", ".py", ".yml")

# Whole-file replacement fixture for update_document. Two occurrences on one
# line so the post-replace count is unambiguous.
GREP_MARKER_V2 = "zzmarker_gamma"
_GREP_REPLACEMENT = f"""# Grep Selftest Replaced

{GREP_MARKER_V2} and {GREP_MARKER_V2} on one line"""
_GREP_SCRIPT_CONTENT = f"""#!/usr/bin/env python3
STALE_PATH = "{GREP_REGISTERED_MARKER}/config.yaml\""""

_WRITABLE_PLAN = """COGNITA SELF-TEST PLAN — server version {version}

ROUTING (REQUIRED BEFORE STEP 1). Call list_projects with no project argument.
Require the exact provisioned project name Self-Test and set PROJECT="Self-Test".
Do not select or test any other project. EVERY project-scoped operation in this
entire plan MUST include project=PROJECT at the top level, including ordinary
document, byte-fidelity, search, manifest, copy, de-index, error-envelope, asset,
OCR, read-only, cleanup, and any request to retrieve this plan again. Do not infer
a project from the connector URL, the tool description, or a previous call; do
not put a second project inside a compound argument. If list_projects is
unavailable, returns an error, or does not expose Self-Test, FAIL the self-test
before attempting project operations. If any required tool is not present in
tools/list, report that tool as FAIL (never SKIPPED).

DISCOVERY AND FAILURE CLASSIFICATION (10.1). Capture the complete emitted tool
definitions from the installed public catalog, authenticated wire tools/list,
direct MCP discovery, and the client-visible app/ChatGPT schema when accessible.
Normalize only wrapper prefixes and object-key order. Keep missing tools,
arguments, required fields, enums, defaults, and material descriptions visible.
Record UTC timestamp, server_version, plan_version, transport/surface, tool
count (40 on the writable public catalog), and a reproducible digest of each normalized catalog; never record a
credential, token, authorization header, or credential-bearing URL. Compare
against the authoritative 10.1 definitions (including remove_asset and
ocr_asset), not a historical tool count. Every tool definition must include a
valid outputSchema.

Use exactly one diagnostic class on each discovery or execution outcome:
  * client_contract_mismatch — the client-visible catalog is stale or differs
    from the authenticated/direct server contract (including a missing exposed
    tool or argument); the affected calls are BLOCKED/NOT EXECUTED.
  * plan_contract_mismatch — the emitted self-test instruction is invalid for
    the authoritative schema (for example content_encoding on read_document);
    this is a plan defect, not a server execution failure.
  * server_execution_failure — the prescribed call was actually sent through a
    conforming schema and the server returned an error or an invalid result.
An unavailable client/schema inspection is UNVERIFIED, never PASS. Do not turn
an absent client-visible tool into a fabricated server failure.

SUPPORTED DISCOVERY REFRESH. Client-visible connector schemas are snapshots.
For the 10.1 schema, recreate the client connector with the canonical V3 URL
shown by the Cognita Admin UI, complete its OAuth authorization, then repeat
the full catalog capture and digest comparison. There is no server-side
in-place refresh operation. A Cognita restart or correct server-side tools/list
is not evidence that the client refreshed; Cognita cannot invalidate a
client-owned cache. Do not disable or alter another server connector as a
substitute. If the coordinator cannot perform the client recreation, report
that exact action as PENDING and include the observed pre/post evidence rather
than claiming reconnect fixed it.

Execute every step IN ORDER using this connector's tools and project=PROJECT,
where PROJECT is exactly Self-Test. Work ONLY on the dedicated test files and
directories this plan names — never edit other documents or use another project.
For EVERY search_knowledge call below, collect the returned result scores and
assert `scores == sorted(scores, reverse=True)` (an empty result list passes
this invariant), including calls described as negative or no-hit checks.
When done, report a scorecard: one line per step with PASS/FAIL and brief
evidence. If a step fails, continue with the remaining steps and flag it.

HOW TO REPORT A FAILURE. Never report an aggregate ("3 failures", "the copy
section failed"). Every FAIL line carries, at minimum:

  * the STEP number and the TOOL that was called,
  * the ARGUMENTS you passed (abbreviate long content to its first line),
  * the ASSERTION that failed, as expected-vs-received: "expected
    total_matches 3, received 2", not "wrong count".

A suite that reports "3 failures" is a suite nobody debugs, and the run is worth
having only because the next person can act on it without re-running it.

READING A RESULT. On 10.1 every tool result also carries structuredContent.
Parse the first text block and require it to be deeply equal to
structuredContent; validate the object against that tool's advertised
outputSchema when the client exposes it. Errors from this server arrive TWO ways and both must be
handled: a tool-level error is HTTP 200 carrying a normal result whose payload
has status:"error" (and, from server 5.0.0, isError:true on the result); a
protocol-level error is a JSON-RPC error object with no result at all. When a
step says "expect failure", it means the PAYLOAD says status:"error" — the call
returning at all is not success. Never report an error envelope as a success.

9.2 CONNECTOR MEASUREMENT. For one fixed representative sequence, record the
number of connector tools/call requests and the UTF-8 serialized response bytes
before and after using the compact/plural paths. Report both totals and the
sequence name. This is evidence, not a target: never claim a fixed call count
or response size, and never weaken an assertion to reach a hoped-for reduction.

9.2 PLURAL AND CONNECTOR-BATCH CHECKS. These are additive checks for the three
new connector-facing tools. `get_documents` must receive 1-100 unique safe
paths, return one `documents` collection in input order, preserve per-path
errors, and support `include_content=false` facts-only reads. `remove_documents`
must receive explicit paths only, reuse the single-file backup/de-index receipt,
and return ordered `documents` entries with `result_key`, counts, and
success/error/skipped outcomes; it never recurses or deletes a directory. Use
both `on_error='stop'` and `on_error='continue'`, and confirm a backup failure
does not delete its path. `batch` is sequential and non-atomic: each child has
its own project, earlier successes remain after a later error, and stop/continue
controls only later scheduling. Reject nested batch and image-result children.

⚠️ IF YOUR CLIENT RAISES ON status:"error", IT HAS THROWN AWAY THE PAYLOAD THIS
PLAN ASKS YOU TO INSPECT. Every negative step below asserts on fields INSIDE the
error payload — reason, expected_sha256, actual_sha256, rejected_arguments,
conflicts. A helper that converts an error result into an exception leaves you
holding a message string and nothing else, and the step then fails for a reason
that has nothing to do with the server. Before you start, make sure you can
capture the RAW result for a failing call.

⚠️ ASSERT ON `reason`, NEVER ON MESSAGE TEXT. Message strings are written for a
human and are changed freely between releases; `reason` is the stable machine
field and is what every step below means. "must return not_found" means
reason == "not_found" — the message will read "No such document: '<path>'" and
will not contain the literal token you are matching on.

⚠️ CLEANUP ASSERTIONS CAN FAIL THROUGH NO FAULT OF THE SERVER — CHECK THE MTIME
BEFORE YOU FILE A DELETE BUG. Cognita's documents folder is bidirectionally
synced by OneDrive, which is a second writer that respects nothing in this tool
surface. A file this plan deleted can reappear minutes later because sync
restored it, and from the tool surface that looks identical to a delete that
silently failed. The discriminator is the mtime, and remove_document hands it to
you: every delete with delete_file=true returns deleted_mtime (plus
deleted_bytes_sha256 and a ghost_check line). KEEP THEM.

  At any cleanup assertion that finds a fixture still present:
    * mtime OLDER than or equal to deleted_mtime, content unchanged
        -> GHOST DETECTED (cloud-sync resurrection). Report it as an
           INFORMATIONAL finding, not a FAIL, re-issue the remove_document, and
           carry on. This happened on the 2026-08-29 run at step 51.
    * mtime NEWER than deleted_mtime
        -> something wrote the file again. That IS a finding worth chasing.
    * mtime, size_bytes and bytes_sha256 all NULL, index_drift true
        -> NOT a ghost. Cloud sync can put a FILE back; it cannot put a ROW back
           in Cognita's index. Nulls mean there is no file, only a leftover index
           record — Cognita's own state, and as of 5.0.2 a real FAIL: every
           removal holds the project write lock across its whole operation, so a
           row it reports as removed is removed before the call returns. Do NOT
           settle and retry; a row that clears a moment later is the defect, not
           the fix. Report the tool, its full response, and the listing.
    * remove_document reported gateway_warning, or status error with
      reason delete_failed
        -> the delete itself failed; that is a real FAIL. Since 5.7 the engine
           catches this itself and refuses: `file_deleted` reports the OUTCOME
           rather than echoing the argument, and on a refusal the index entry is
           deliberately LEFT so the document stays searchable instead of being
           stranded. gateway_warning now means the file came back in the window
           between the engine's check and the response — a sync writer, not a
           failed unlink.

BYTE-FIDELITY STEPS. RUN THESE FIRST. A store must persist what it is handed;
until 5.0.0 this one did not, and the corruption was silent in both directions.
Every assertion here is BYTE equality — not "looks the same", not "equal after
stripping".

🔴 EVERY VALID-UTF-8 READ PATH IS BYTE-VERBATIM AS OF 5.6.0. Accepted malformed
UTF-8 uses a visibly lossy readable view; compare its base64 form for exact bytes.
For valid UTF-8, compare anything against anything; they must all agree:

  * read_document's `text` and get_document's `content` — the FILE, decoded as
    UTF-8 and otherwise untouched: its own line endings, its own BOM, its own
    leading and trailing whitespace. sha256 of a whole-file read equals
    bytes_sha256.
  * bytes_sha256 / size_bytes — the file byte for byte, from read_document,
    get_document and list_documents(include_hashes=true).
  * content_sha256 — the WRITE-GUARD stamp, and the one value here that is
    deliberately not a description of the bytes: BOM dropped, CRLF/CR folded to
    LF, so an EOL-only rewrite by OneDrive does not read as a stale file. It is
    what expected_sha256 compares against. Never byte-compare with it; use
    bytes_sha256.

Until 5.6.0 two of the three read paths silently altered the document on the way
out, and this preamble told you that was correct. It was not. get_document
returned the INDEXER's extraction — .json re-serialized through
json.dumps(indent=2), .md with its YAML frontmatter deleted, .csv rewritten as a
" | " table, CRLF folded — and read_document folded CRLF and dropped the BOM.
A store does not edit what it is handed. If a read here disagrees with what you
sent, that is a BUG IN THE SERVER; do not explain it away as normalization, and
do not "fix" your comparison to match.
B1. TRAILING AND LEADING WHITESPACE: for each of these six payloads in turn,
    add_document filepath='{bytes_file}.md' with exactly that content, then
    read_document it back and compare byte for byte:
      (a) 'a\nb'              — a plain two-line file
      (b) 'a\nb\n'             — ONE trailing newline; it must survive
      (c) 'a\nb\n\n'            — TWO trailing newlines; BOTH must survive
      (d) '  a\nb'            — LEADING indent; it must survive
      (e) 'a\r\nb\r\n'          — CRLF; it must NOT become LF
      (f) 'a\tb\n'             — an interior tab, plus a trailing newline
    Every one must round-trip unchanged. Historical failures, all of which this
    catches returning: (b) and (c) lost every trailing newline, (d) lost the
    indent — silently corrupting any file whose first line is indented, which is
    most Python fragments and every YAML continuation — and (e) was converted to
    LF. Report each payload on its own scorecard line, naming the payload.
    ENCODED BYTE CHECK: repeat payload (e) through the connector's base64 path:
    add_document filepath='{bytes_file}.md' content='{crlf_base64}'
    content_encoding='base64', then get_document filepath='{bytes_file}.md'
    content_encoding='base64'. Decode the returned content and require the six
    original bytes exactly. This connector check is additive; independently
    verify the on-disk bytes (or a raw read) rather than trusting the receipt.
    The read_document call remains unencoded; it has no encoded-read mode.
B2. EVERY EXTENSION BEHAVES THE SAME: repeat payload (b) ('a\nb\n') for
    '{bytes_file}.txt', '{bytes_file}.py' and '{bytes_file}.yml'. All must
    round-trip unchanged. .md and .txt are embedded-tier and .py and .yml are
    registered-tier, so this covers both storage paths. There is no
    extension-conditional handling and there must never be one; a report that
    says otherwise is describing a regression.
B3. UPDATE IS VERBATIM TOO: update_document '{bytes_file}.md' with content
    '  indented\nsecond\n\n', then read_document — expect exactly those bytes,
    leading spaces and both trailing newlines intact. add_document was the tool
    originally measured; update_document shares the write path and must not be
    assumed to have been fixed with it.
B4. EVERY READ PATH AGREES, BYTE FOR BYTE: add_document '{bytes_file}.md'
    with content 'a\r\nb\r\n', then read it three ways and compare:
      * read_document — expect line_endings='crlf', size_bytes=6, bytes_sha256
        EQUAL to the sha256 of the six bytes you sent, and its `text` EXACTLY
        'a\r\nb\r\n'. NOT 'a\nb\n'. Its content_sha256 is the LF-folded write-guard
        stamp and is SUPPOSED to differ from bytes_sha256 here — that one value
        is a version stamp, not a description of the bytes.
      * get_document — its `content` must be the SAME six bytes, and
        content_is_extracted must be false.
      * list_documents prefix='{bytes_file}.md' include_hashes=true — its
        bytes_sha256 and content_sha256 must EQUAL read_document's. Three tools,
        one file, no disagreement anywhere.
    Then repeat with content 'a\nb\n' (LF only) and expect line_endings='lf' and
    bytes_sha256 EQUAL to content_sha256. THIS STEP IS THE GUARD AGAINST THE NEXT
    SILENT NORMALIZATION landing in a path described as verbatim. Before 5.6.0
    two paths had one and this step was written to EXPECT it, which is how it
    survived five releases: a read that alters the document is a defect no matter
    how reasonable the reason sounds.
    ENCODED BYTE CHECK: repeat the CRLF fixture using
    content='{crlf_base64}', content_encoding='base64', then get_document it with
    content_encoding='base64'. Require content_is_extracted=false and the
    decoded bytes to equal {crlf_text_repr}. Independently hash the raw file and
    compare it with bytes_sha256; a connector receipt alone is insufficient.
    The read_document call remains unencoded; it has no encoded-read mode.
B4b. STRUCTURED FORMATS ARE NOT REFORMATTED: add_document '{bytes_file}.json'
    with content '{{\n  "k": ["a", "b"],\n  "n": 1\n}}\n' — a compact inline array
    and a trailing newline — then get_document it. The content must come back
    with '"k": ["a", "b"]' on ONE line and the trailing newline intact. Until
    5.6.0 it came back through json.dumps(indent=2): the array exploded over four
    lines and the trailing newline was gone. Then add_document
    '{bytes_file}.front.md' with content '---\ntitle: T\n---\n\n# Body\n' and
    get_document it — the YAML frontmatter must still be there. It used to be
    DELETED from the read, which is the quietest form this bug took: valid
    content, no error, silently missing. Remove both files afterwards with
    remove_document delete_file=true.
B5. CLEANUP: remove_document delete_file=true for '{bytes_file}.md',
    '{bytes_file}.txt', '{bytes_file}.py' and '{bytes_file}.yml'. KEEP the
    deleted_mtime each call returns — step 51 needs them for the ghost check.

 1. CREATE: add_document filepath='{test_file}' category='general' with exactly
    this content (between the markers, exclusive):
    ----8<----
{content}
    ---->8----
    If the result reports overwrote_existing, a leftover copy was on disk and
    was safely backed up before overwriting — not a failure, but do the
    forensics the result hands you: if previous_content_sha256 starts with
    {h0}, the leftover was a prior run's post-restore state resurrected by
    cloud sync after its delete — report 'GHOST DETECTED (prior run, id
    <previous_backup_id>)' as an informational finding and continue.
 2. SEARCH: search_knowledge for "alpha two" (keyword-biased, hybrid_alpha 0)
    — expect a hit in {test_file}: new content is immediately indexed. Assert
    returned scores are descending.
 3. READ: read_document section='Alpha' — expect it to span through
    '### Alpha-child' (subsections included) and include content_sha256 and
    mtime. KEEP the hash (call it H0). HASH CHECK: H0 must start with
    {h0} — a mismatch means the write/index pipeline changed behavior.
 4. DRY RUN: edit_document old_str='alpha one' new_str='alpha ONE'
    dry_run=true — expect applied:false, replacements:1, a context_diff, and
    current_content_sha256 (should equal H0).
 5. GUARDED EDIT: repeat step 4 without dry_run, passing expected_sha256=H0 —
    expect success, the same diff as the preview, and new_content_sha256 (H1).
    HASH CHECK: H1 must start with {h1}.
 6. NEGATIVE stale: edit_document old_str='alpha two' new_str='x' with
    expected_sha256=H0 (now outdated) — expect reason:stale_file with the
    actual hash echoed. Nothing may be written.
 7. BATCH: edit_document_batch expected_sha256=H1 with two edits:
    'alpha two'->'alpha TWO' and 'beta one'->'beta ONE' — expect
    edits_applied:2, per-edit results, one unified diff, and a new hash (H2).
    HASH CHECK: H2 must start with {h2}.
7b. SEARCH FRESH EDIT: search_knowledge for "alpha TWO" (keyword-biased,
    hybrid_alpha 0, max_results 3) — expect a hit in {test_file} whose content
    contains 'alpha TWO'. This is the search-AFTER-edit lifecycle phase the
    earlier plan never exercised (all other searches run pre-edit or
    post-delete): it guards post-edit index freshness as a permanent tripwire.
    Assert returned scores are descending.
 8. NEGATIVE ambiguous: edit_document old_str='line' new_str='x' — expect
    reason:ambiguous with match line numbers listed.
 9. INSERT intro: insert_in_document position='end_of_intro' section='Selftest'
    text='intro two' expected_sha256=H2 — expect placement after 'intro line'
    and BEFORE '## Alpha' (the H1's own content, not the whole document).
    Keep the new hash (H3). HASH CHECK: H3 must start with {h3}.
10. INSERT section: insert_in_document position='end_of_section'
    section='Alpha' text='alpha three' expected_sha256=H3 — expect placement
    after 'child line' (subsections included) and before '## Beta'. (H4)
    HASH CHECK: H4 must start with {h4}.
11. HISTORY: list_backups filepath='{test_file}' — expect the NEWEST 4 entries
    to correspond to this run's writes (steps 5, 7, 9, 10), newest first, and
    the two rejected writes (6, 8) to have left none. Entries from PREVIOUS
    self-test runs may also be present — that is normal and correct, not a
    finding: deleting a file never purges its backups (they are the
    post-deletion recovery path).
12. DIFF: diff_backup against the oldest backup_id OF THIS RUN (the step-5
    one) — expect a unified diff telling the story of steps 5-10 in one
    read-only call.
13. ROUND-TRIP: restore_backup to this run's oldest backup_id — expect success
    and new_content_sha256 equal to H0 ({h0}…): a cryptographic proof the file
    round-tripped bit-for-bit. (The restore snapshots first, so it's undoable.)

These hash checks are a byte-level canary for the entire write path: the five
values are constants of this plan and must reproduce on every run of this
server version. Any mismatch is a REAL regression — report it prominently.
13b. MOVE/RENAME: move_document filepath='{test_file}' new_filepath='{test_file_moved}'
    — expect status success and chunks_moved >= 1. A rename is metadata-only: the
    index follows the file and nothing is re-embedded (content is byte-identical).
    Verify the index moved WITH the file: read_document '{test_file}' must return
    not_found (old path gone), and search_knowledge for "alpha two" (keyword-biased,
    hybrid_alpha 0, max_results 3) must return a hit whose source ends in
    '{test_file_moved}' and whose returned scores are descending. NEGATIVE:
    move_document filepath='{test_file_moved}'
    new_filepath='{test_file_moved}' (same path) must be refused with status error and
    nothing moved. Then move it BACK: move_document filepath='{test_file_moved}'
    new_filepath='{test_file}' — expect success, leaving the file at '{test_file}' so
    the next step can clean it up.
14. CLEANUP: remove_document filepath (absolute path from search/list results)
    with delete_file=true; check the result for any gateway_warning. Then
    verify BOTH layers: search_knowledge for "alpha two" must return no hit in
    {test_file} (index gone), with returned scores descending, and read_document on '{test_file}' must return
    not_found (disk gone — catches a silently failed delete or a file about to
    be resurrected by cloud sync). The file's backups intentionally SURVIVE
    the delete (they are the recovery path); leftover selftest backups are
    expected and must not be flagged.

Retention note (no action): backups are capped server-side at the newest N per
file (default 20) and pruned automatically with each deletion logged.

REGISTERED-TIER STEPS (4.4). These use their OWN file, '{script_file}', and
never touch '{test_file}'. A registered document is a first-class citizen
everywhere except semantic retrieval: listed, readable, writable, backed up and
findable by literal string — the one capability it lacks is an embedding.
15. REGISTER: add_document filepath='{script_file}' with exactly this content
    (between the markers, exclusive):
    ----8<----
{script_content}
    ---->8----
    Expect status success, tier='registered', semantic_searchable=false, and
    chunks_added EXACTLY 0. A non-zero chunk count here means code is being
    embedded again — the regression this tier exists to prevent.
16. LIST: list_documents — expect an entry for '{script_file}' with
    tier='registered', semantic_searchable=false and chunks=0, and the response
    to carry registered_count >= 1. This step is load-bearing: a path that never
    appears in a listing cannot be known, and get_document on a known path is
    the entire point of the tier.
17. READ WHOLE: get_document filepath='{script_file}' — expect the FULL content
    above, tier='registered' and chunk_count 0.
18. READ RANGE: read_document filepath='{script_file}' start_line=1 end_line=2 —
    expect those two lines verbatim plus content_sha256. Registered documents
    use the ordinary read path; there is no tier-specific size limit.
19. KEYWORD SEARCH: search_knowledge for "{script_marker}" at the DEFAULT
    hybrid_alpha — expect a hit whose source ends in '{script_file}', carrying
    tier='registered', semantic_searchable=false and search_method='keyword'.
    Assert returned scores are descending.
20. SEMANTIC SEARCH (NEGATIVE): search_knowledge for "{script_marker}" with
    hybrid_alpha=1.0 — expect NO hit from '{script_file}'. Semantic-only means
    embedded-only: the keyword leg is switched off, so this tier cannot appear.
    A hit here means an orphaned vector survived and is the single most
    important failure on this list — report it prominently. Assert returned
    scores are descending.
21. SIMILARITY (NEGATIVE): search_similar filepath='{script_file}' — expect
    status error with reason='registered_document' and a message explaining it
    has no embedding. It must NOT be a generic "not found": step 16 just showed
    the file exists.
22. EDIT: edit_document filepath='{script_file}' old_str='{script_marker}'
    new_str='{script_marker}_v2' — expect success. Then list_backups
    filepath='{script_file}' — expect a backup was made (writes to this tier are
    backed up exactly like any other). Confirm via get_index_stats that the
    registered tier still reports 0 chunks and 0 vectors.
23. TIER CROSSING OUT: move_document filepath='{script_file}'
    new_filepath='{script_md}' — moving to a .md path re-evaluates the tier.
    Expect success, then get_document '{script_md}' to report tier='embedded'
    with chunk_count >= 1: crossing INTO the embedded tier creates chunks and
    vectors.
24. TIER CROSSING BACK: move_document filepath='{script_md}'
    new_filepath='{script_file}' — expect success and tier='registered' with
    chunk_count back to 0. The chunks and vectors from step 23 must be GONE;
    orphaned vectors after a crossing are a real defect.
25. STATS: get_index_stats — expect a 'tiers' block reporting the two tiers
    SEPARATELY: tiers.embedded with non-zero documents/chunks/vectors, and
    tiers.registered with documents >= 1 and chunks and vectors both EXACTLY 0.
    Expect '.py' among tiers.registered.extensions and '.md' among
    tiers.embedded.extensions.
26. CLEANUP: remove_document filepath='{script_file}' delete_file=true, then
    verify read_document '{script_file}' returns not_found. As in step 14, its
    backups intentionally survive and must not be flagged.

LITERAL-SEARCH STEPS (4.5, find_literal). These use their OWN files,
'{grep_file}' and '{grep_script}', and never touch the files above. find_literal
is the only EXHAUSTIVE retrieval path on this server: search_knowledge returns
the best N by relevance and cannot tell you it found everything, so the counts
below are exact and an off-by-one is a real failure, not a ranking difference.

⚠️ FIRST CALL NEEDS APPROVAL, AND THE PROMPT EXPIRES. find_literal is newer
than the other tools, so on a connector that has never called it claude.ai
shows a tool-approval request in the chat (Allow once / Allow always). If
nobody answers it, it TIMES OUT and the call fails with
"No approval received" — a client-side prompt that expired, NOT a server
failure and NOT a missing tool. It reads like an auth error; it is not. Report it
BLOCKED (not FAIL), tell the user to re-run the call and choose "Allow always"
(otherwise every later find_literal step prompts again), and resume at step 27.
If NO prompt appears, have them check the "Search and tools" menu — an
individual tool can be switched off there, which blocks it regardless of
approval.

⚠️ IF YOU STOP EARLY, CLEAN UP. Steps 27-33 leave '{grep_file}' and
'{grep_script}' on disk; step 34 is what removes them. Aborting in between
strands test fixtures in a real knowledge base — if you cannot continue, run
step 34's two remove_document calls before you stop.
27. SETUP + EXACT: add_document filepath='{grep_file}' category='{grep_category}'
    (a NON-default category on purpose — step 31 checks it survives a write)
    with exactly this content (between the markers, exclusive):
    ----8<----
{grep_content}
    ---->8----
    Then find_literal pattern='{grep_marker}' — expect total_matches EXACTLY 3
    (line 3 once, line 4 twice), files_with_matches 1, truncated false, and the
    two line-4 matches distinguished by DIFFERENT column values. A count of 2
    means multiple matches on one line are being collapsed. Line and column are
    both 1-indexed; line_number must agree with read_document's numbering.
28. CASE + NEGATIVE: find_literal pattern='{grep_marker}' case_sensitive=false
    — expect total_matches 4 (the caps line on line 5 now included). Then
    find_literal pattern='zzmarker_omega' — expect status success,
    total_matches 0, an empty matches array, and NOT an error. A zero result
    must be a trustworthy answer, not a failure; that is what makes a sweep
    worth believing.
29. REGEX + REGISTERED REACH: find_literal pattern='zzmarker_(alpha|beta)'
    regex=true — expect the SAME 3 matches as step 27. Then find_literal
    pattern='[' regex=true — expect status error, reason='bad_pattern', the
    compile error echoed in the message, and no traceback. Then add_document
    filepath='{grep_script}' category='{grep_category}' with exactly this
    content (between the markers, exclusive):
    ----8<----
{grep_script_content}
    ---->8----
    (expect tier='registered', chunks_added 0), and find_literal
    pattern='{grep_registered_marker}' — expect exactly 1 match carrying
    tier='registered'. THIS IS THE CAPABILITY WITH NO OTHER PATH: a registered
    file has no embedding and cannot be reached semantically at all, and a
    hardcoded stale path inside a script is precisely what this tool exists to
    find. Failure here is the most important item on this list.
30. FRESHNESS: edit_document filepath='{grep_file}'
    old_str='{grep_marker} appears here' new_str='zzmarker_beta appears here',
    then find_literal pattern='zzmarker_beta' — expect total_matches EXACTLY 1
    at line 3 — and find_literal pattern='{grep_marker}' — expect EXACTLY 2
    (both now on line 4). A stale count here means the walk is reading cached
    text instead of the live file. (The anchor must be the full 'appears here':
    '{grep_marker} appears' also occurs on line 4 and would be ambiguous.)

CATEGORY + FULL-REPLACE STEPS. These close the last gaps in tool coverage:
update_document, list_categories and evaluate_retrieval are exercised nowhere
above, and update_document is the exact path whose category-reset bug shipped
in 4.4.1 and was fixed in 4.4.2.
31. CATEGORIES: list_categories — expect '{grep_category}' present with count
    EXACTLY 2 (the .md and the .py from steps 27 and 29). Then find_literal
    pattern='zzmarker_beta' category='{grep_category}' — expect the same 1
    match as step 30, proving the category filter selects rather than silently
    dropping. Then find_literal pattern='zzmarker_beta' category='nonexistent'
    — expect total_matches 0.
32. FULL REPLACE: update_document filepath='{grep_file}' with exactly this
    content (between the markers, exclusive):
    ----8<----
{grep_replacement}
    ---->8----
    Then find_literal pattern='{grep_marker_v2}' — expect total_matches EXACTLY
    2, both on line 3 with different columns — and find_literal
    pattern='zzmarker_beta' — expect EXACTLY 0, proving update_document is a
    FULL replacement, not a merge, and that the literal walk sees the
    replacement immediately. Then get_document filepath='{grep_file}' — its
    category MUST still be '{grep_category}'. A category of 'general' here is
    the 4.4.1 regression returning: a write silently reclassified the document.
    Report it prominently.
33. RETRIEVAL EVAL: evaluate_retrieval with test_cases
    '[{{"query": "{grep_marker_v2}", "expected_filepath": "{grep_file}"}}]' —
    expect status success with total_queries 1, mrr_at_5, recall_at_5 and a
    per_query breakdown. found_at_rank should be 1: the query is a unique
    literal token in an embedded document. A null rank means the keyword leg is
    not reaching a document that demonstrably contains the string — investigate
    rather than passing it.
33b. ATOMIC BATCH (6.1.0): write_documents with TWO documents in one call —
    filepath='{grep_file}' with its current content UNCHANGED, and
    filepath='zz_batch_probe.md' with content '# Batch probe\\n\\nzzmarker_batch\\n'.
    Expect status success, documents_written EXACTLY 2, and chunks_indexed > 0.
    Then find_literal pattern='zzmarker_batch' — expect total_matches 1,
    proving the batch's writes are indexed and not merely on disk.
    Now the ALL-OR-NOTHING check, which is the point of the tool: call
    write_documents again with filepath='zz_batch_probe.md' content
    '# Replaced\\n\\nzzmarker_batch2\\n' AND a second entry whose filepath is
    'zz_batch_probe.exe' (an unindexable extension). Expect status error and
    documents_written 0 — then get_document filepath='zz_batch_probe.md' and
    confirm it STILL contains zzmarker_batch and NOT zzmarker_batch2. A file
    that changed here means the batch is not atomic and a half-applied set can
    reach disk, which is the 2026-09-02 worldbook failure exactly. Report it
    prominently. Finally remove_document filepath='zz_batch_probe.md'
    delete_file=true.
34. CLEANUP: remove_document filepath='{grep_file}' delete_file=true and
    remove_document filepath='{grep_script}' delete_file=true. Then
    find_literal pattern='zzmarker_' — expect status success and total_matches
    0. That zero is doing double duty: the backups of both files still hold
    these markers on disk, so a non-zero count means the backup tree is being
    walked — which would make every rename sweep drown in hits from old
    snapshots. Backups surviving is correct and must not be flagged. Finally
    list_categories — '{grep_category}' must be GONE (removing the last
    document in a category removes the category).

MANIFEST + WRITE-GUARD STEPS (5.0). These cover the sync workflow: answering
"which of these files differ from my local copy?" in one call, and making a
whole-file overwrite refuse to clobber a concurrent change. Every step here uses
its OWN files under '{copy_src}/' and never touches the files above.
35. SETUP A PACK: add_document three files, each with exactly the content shown
    (between the markers, exclusive) — no category argument on any of them,
    which step 42 checks:
      '{copy_src}/one.md'    ->  {marker}_one
      '{copy_src}/two.md'    ->  {marker}_two
      '{copy_src}/three.txt' ->  {marker}_three
    Expect status success on all three. The parent directory '{copy_src}/' does
    NOT exist beforehand: add_document creates missing parents, and that is
    load-bearing — there is no mkdir tool, so this is the only way a new pack
    directory comes into existence. If any of these fails with not_found, stop
    and report it: nothing below can run.
36. MANIFEST: list_documents prefix='{copy_src}/' include_hashes=true — expect
    count EXACTLY 3, and every entry to carry filepath, content_sha256,
    bytes_sha256, size_bytes, mtime and index_drift:false. Keep the three
    content_sha256 values. Then get_document '{copy_src}/one.md' — its
    content_sha256 must EQUAL the manifest's for that file. Two hashes of the
    same bytes from two different tools disagreeing is a real defect; they are
    computed from the file on disk in both cases, which is the point.
    (Catches index/disk drift going undetectable — the reason the manifest
    exists. A hash served out of index state cannot detect the index and the
    disk disagreeing.)
37. PREFIX IS NOT SILENTLY DROPPED: list_documents prefix='{copy_src}/' — expect
    3, NOT the whole corpus. Then list_documents path_prefix='{copy_src}/'
    (deliberately the WRONG argument name) — expect status error,
    reason='unknown_argument', 'path_prefix' listed in rejected_arguments, and
    'prefix' among accepted_arguments. THIS IS THE MOST IMPORTANT STEP IN THIS
    SECTION: before 5.0 that call returned all 319 documents and reported
    success, so the client believed it had filtered and had not. A success here
    is a silent wrong answer, which is worse than an error.
38. ROUND TRIP IS IDENTITY: read_document '{copy_src}/three.txt' and compare its
    bytes_sha256 and size_bytes against what you sent in step 35 — they must
    match EXACTLY, with no stripping and no allowance of any kind. Writes have
    been byte-verbatim since 5.0.0: what you send is what is stored, trailing
    newline included.
    Since 5.6.0 get_document's content is byte-verbatim too, so either tool
    answers this; bytes_sha256 remains the value to compare against, because
    content_sha256 is the LF-folded write-guard stamp rather than a description
    of the bytes. See the byte-fidelity preamble above.
39. WRITE GUARD — update_document: take H5 = the content_sha256 of
    '{copy_src}/one.md' from step 36. First the NEGATIVE: edit_document
    '{copy_src}/one.md' old_str='{marker}_one' new_str='{marker}_one_v2' (no
    expected_sha256) to change the file behind your own back — RECORD its
    previous_backup_id as B5, the snapshot this edit just took of the file as
    of H5; step 41 restores that exact id. (5.5: every write that takes a
    backup names it, so the undo point never has to be inferred from a later
    listing. Every backed-up write from here on carries the field — the
    update_document below returns one too.) Then
    update_document '{copy_src}/one.md' content='clobbered'
    expected_sha256=H5 — expect status error, reason='stale_file', with
    expected_sha256 and actual_sha256 both echoed, and get_document to show the
    file STILL contains '{marker}_one_v2'. Nothing may be written. THEN the
    positive: re-read its current content_sha256 (H6) and repeat the
    update_document with expected_sha256=H6 — expect success. (Catches the
    2026-08-26 regression specifically: update_document rewrites a WHOLE file
    and until 5.0 had no concurrency check at all, so every push could clobber a
    concurrent change on the server with only a backup to show for it.)
40. WRITE GUARD — add_document overwrite: add_document filepath=
    '{copy_src}/two.md' content='clobbered by add' expected_sha256=<the step-36
    hash for two.md, now stale only if something changed it — so first change it
    with edit_document old_str='{marker}_two' new_str='{marker}_two_v2'> —
    expect status error, reason='stale_file', and get_document to show
    '{marker}_two_v2' still there, unchanged. The push path overwrites through
    add_document, which is why the guard has to be on this tool too and not only
    on update_document.
    ASSERT ON THAT get_document PAYLOAD, not just on its content:
    content_sha256, bytes_sha256 and indexed_sha256 must all be EQUAL, and
    index_drift must be FALSE. 6.1.1: this exact call returned index_drift:true
    beside three identical hashes — a write the server had just performed and
    indexed itself, reported as the index disagreeing with the disk. It was
    found twice (5.6.3 on 2026-08-31, 6.1.0 on 2026-09-02) by a reader who
    happened to look, because no step asserted on it, and it self-cleared within
    minutes, which is exactly what makes it easy to dismiss. The cause was a
    second writer that changes no content: on a cloud-synced documents tree the
    sync client rewrites the local mtime to whole-second precision after upload,
    and drift was decided on the stat alone. Three identical hashes cannot be
    drift — if you see that combination again, it is a real defect, and the
    numbers to report are all four fields plus the mtime's sub-second part.
41. BACKUPS ARE THE UNDO PATH: list_backups filepath='{copy_src}/one.md' —
    expect B5 from step 39 among the entries, newest first. Then restore_backup
    filepath='{copy_src}/one.md' backup_id=B5 — expect status success and
    new_content_sha256 EQUAL to H5 ({h5_note}), a cryptographic proof the file
    round-tripped bit for bit. Its previous_backup_id names the snapshot of
    what the restore just overwrote, so the undo is itself undoable by id.
    Restore B5 BY ID. Do NOT restore 'the oldest entry in the listing': backups
    survive deletes and accumulate across runs on this reused fixture path (a
    2026-08-29 run found 20 on this one file, the oldest from an earlier
    session), so the oldest entry holds content this run never wrote — and when
    the file already matches it, the answer is reason='no_change', which reads
    as a failed step but is only fixture drift.
    Then repeat that SAME restore_backup call — expect status error,
    reason='no_change' naming B5, and the file untouched. A restore that would
    change nothing says so instead of reporting a write that did not happen.
    This is the ONLY undo path in the system; a failure here means a bad write
    is unrecoverable.

COPY + DIRECTORY STEPS (5.0). Duplicating a pack before editing it is the rule
in this project with the worst track record — it has been violated once and
needed a restore from backups. These steps exercise the tools that make it one
operation instead of two calls per file.
42. CATEGORY INFERENCE: list_documents prefix='{copy_src}/' — every entry's
    category must be the SAME value, derived from the path (it will be 'general'
    unless this project configures category_mappings for that path). Step 35
    passed no category on any file; before 5.0 an omitted category was forced to
    the literal string 'general' regardless of configuration, which made a
    project's category mappings a no-op for every document written through the
    connector. Record the value you see — step 44 asserts the copies inherit it.
43. COPY ONE: copy_document src_filepath='{copy_src}/three.txt'
    dst_filepath='{copy_dst}/three.txt' — expect status success and a
    content_sha256 EQUAL to the step-36 hash for three.txt. A copy is
    byte-for-byte: it never round-trips through JSON and never strips, so unlike
    add_document its hash must match the source exactly. Then copy_document with
    the SAME arguments again — expect status error,
    reason='destination_exists', the conflicting path named, and nothing
    written. Then repeat with overwrite=true — expect success and
    previous_backup_id present.
44. COPY THE PACK: copy_directory src_prefix='{copy_src}' dst_prefix='{copy_dst}'
    overwrite=true — expect status success, files_copied EXACTLY 3,
    destination_paths listing all three, and every entry's content_sha256 equal
    to its source's. Then list_documents prefix='{copy_dst}/' — expect 3
    entries, each with the SAME category you recorded in step 42 (copies inherit
    the source's category; a pack that lands in 'general' is no longer
    enumerable as itself). (Catches a half-copied directory that looks
    complete — the failure the tool exists to prevent.)
45. COPY REFUSES A NON-EMPTY DESTINATION: copy_directory src_prefix='{copy_src}'
    dst_prefix='{copy_dst}' with overwrite omitted (false) — expect status
    error, reason='destination_exists', a conflicts array naming ALL THREE
    destination paths (not just the first), and file_count 3. Then verify NO
    PARTIAL WRITE occurred: list_documents prefix='{copy_dst}/' include_hashes=
    true must still show exactly the 3 files from step 44 with UNCHANGED
    content_sha256 values. A refusal that half-wrote is worse than no refusal.
    NOTE ON ATOMICITY, no action required: this server rolls back every file it
    already wrote if one fails mid-copy, so a copy is atomic against ERRORS. It
    is NOT atomic against the process being killed mid-call — that can leave a
    partial destination, recoverable with remove_directory. Knowing which
    guarantee you have is worth as much as having the stronger one.
46. COPIES ARE INDEXED ON ARRIVAL: find_literal pattern='{marker}_three' —
    expect matches in BOTH '{copy_src}/three.txt' and '{copy_dst}/three.txt'
    with no reindex_documents call in between. Then find_literal
    pattern='{marker}_one' filepath_glob='{copy_dst}/*' — expect exactly 1
    match, in the copy. A copy that is on disk but not in the index is invisible
    to every search, which is the same as not having been copied.
47. FRESHNESS FOR BOTH TIERS: add_document '{copy_dst}/fresh.md' with content
    '{marker}_fresh' and add_document '{copy_dst}/fresh.py' with content
    '{marker}_fresh' — then, with NO reindex_documents call, find_literal
    pattern='{marker}_fresh' — expect EXACTLY 2 matches, one in each file. The
    .md goes to the embedded tier (tier='embedded', chunks_added 1) and the .py
    to the registered tier (tier='registered', chunks_added 0); both are indexed
    synchronously before the write returns, and this step is the evidence for
    that rather than a code reading of it. 6.1.1: the second fixture was a .txt,
    which this plan's own step B2 and get_index_stats both correctly place in
    the EMBEDDED tier — so the step tested one tier twice while its heading
    claimed two, and told every run to expect a 'registered' it was never going
    to get.
48. REMOVE_DIRECTORY REFUSES BY DEFAULT: remove_directory prefix='{copy_dst}'
    (delete_files omitted, so false) — expect status error, reason='not_empty',
    the file count reported, and NOTHING removed: list_documents
    prefix='{copy_dst}/' must still show all 5 files. De-indexing files while
    leaving them on disk would produce documents no search can reach, which is
    why it refuses rather than doing half the job.
49. REMOVE_DIRECTORY + BULK ROLLBACK SET: first list_backups prefix='{copy_dst}/'
    and keep its set of (filepath, backup_id) pairs as BEFORE. Then remove_directory prefix='{copy_dst}'
    delete_files=true — expect status success, documents_removed 5,
    files_deleted 5, a backups array with 5 entries, and pruned_directories
    naming '{copy_dst}'. Keep the 5 DISTINCT (filepath, backup_id) receipts in
    its backups array as RECEIPTS. Then list_backups prefix='{copy_dst}/' and
    keep its (filepath, backup_id) set as AFTER — expect AFTER minus BEFORE to
    equal EXACTLY RECEIPTS, with all 5 receipt paths present. A shared backup_id
    across different paths is correct; compare the PAIRS, not ids alone. The
    whole listing may also contain backups from previous runs and earlier
    copy overwrites in this run. Preserve them; never delete historical backups
    to satisfy an exact-count assertion. Missing or extra NEW receipts are a
    FAIL. since/until may narrow discovery, but same-second backups can share
    that time window, so time bounds alone do not identify the operation.
    That is what makes a bulk
    operation undoable as a SET: a backup system drivable only one file at a
    time is not a usable undo path for a bulk transform. Finally read_document
    '{copy_dst}/three.txt' — expect not_found (disk gone). Write that assertion
    so it FAILS if the not_found arrives inside a success envelope: on
    2026-08-29 exactly that shape was read as "file still present" and had to be
    retracted. Check the payload's status field, not merely that a result came
    back.
50. SYNC-CONFLICT NAMES ARE REFUSED: add_document filepath='{conflict_name}'
    content='should never be stored' — expect status error,
    reason='sync_conflict_name', and NOTHING written: list_documents
    prefix='cognita-selftest-probe' must return 0 documents, and read_document
    on that path must report not_found. A cloud-sync conflict copy indexed
    beside the real file is the failure most likely to produce a wrong answer
    nobody notices — a builder glob picks up both and the output changes with
    nobody having edited anything. Then get_index_stats — expect a
    'sync_conflicts' block with a count, a files list and the patterns in force.
    (If that block reports files you did not create, they are real conflict
    copies sitting in the knowledge base right now: report them, they are a
    finding, not a failure.)
51. CLEANUP: remove_directory prefix='{copy_src}' delete_files=true — expect
    success and documents_removed 3 (one.md, two.md, three.txt; '{copy_dst}'
    already went in step 49 and step 50 wrote nothing). The prefix is the
    directory NAME: through 13.0.1 this step said 'cognita-selftest-', which
    is not a directory, so the server correctly refused with not_found and every
    literal run stranded the pack — which then made step 34's zero count on the
    NEXT run come back 3. Its backups array carries a deleted_mtime
    per file, and backup_ids lists the DISTINCT ids: a bulk call normally
    produces ONE shared id, because backup_ids are per-second timestamps and
    that is exactly what makes the operation enumerable as a set. Three files
    sharing one id is correct and is NOT a collision — feed every id in
    backup_ids to list_backups to see the whole set.
    Then list_documents prefix='{copy_src}' — expect 0 documents, proving every
    fixture from steps 35-50 is gone (that string prefix also covers
    '{copy_dst}'). Do NOT widen it to 'cognita-selftest': the OCR fixtures under
    'cognita-selftest/ocr/' are provisioned by the server, live there
    permanently, and one of them (not-a-png.txt) is a document — so that wider
    listing can never be 0 and is not a failure. Backups of all of them
    intentionally SURVIVE and must not be flagged.
    Issue that listing IMMEDIATELY, with no settle and no retry: the whole
    removal runs inside the project write lock, so the counts it returned are
    true at the moment it returns them. A row still listed here — especially one
    with null mtime/size_bytes/bytes_sha256 and index_drift — is a FAIL, and it
    is the 5.0.1 defect this step exists to catch (on that run the row cleared on
    its own a call later, which is precisely what made the delete look broken).
    ⚠️ IF A FIXTURE IS STILL LISTED HERE, RUN THE GHOST CHECK from the top of
    this plan before recording a FAIL: compare its mtime against the
    deleted_mtime the earlier remove_document returned. An unchanged mtime and
    unchanged content mean cloud sync restored it after Cognita deleted it —
    INFORMATIONAL, not a failure. Re-issue the delete and confirm it stays gone
    across two checks a few seconds apart. This is the single most likely
    non-defect failure in this plan, and it occurred on the 2026-08-29 run.

SEARCH SHAPE + GLOB STEPS (5.0). These cover the four defects that all have
one root cause: a caller cannot predict what a response looks like, so it reads
the wrong key, gets an empty list or a null, and reports a working tool as
broken. That is not hypothetical — it happened on 2026-08-29 in both directions.
GL1. GLOB SEMANTICS.
    ⚠️ PRECONDITION: '{copy_src}/' must contain only 'globtest/deep.md' at this
    point — step 51 removed the rest, so running the GL section IN ORDER is
    correct. Do NOT re-create '{copy_src}/one.md' or 'two.md' before GL2: with a
    .md file sitting directly in '{copy_src}/', case (c) below correctly returns
    no_matches instead of no_documents_selected (documents WERE selected, the
    string simply was not in them) and the step proves nothing. If you need
    those fixtures for GL3-GL7, create them AFTER GL2.
    add_document '{copy_src}/globtest/deep.md' with content
    '{marker}_deep'. Then run find_literal pattern='{marker}_deep' three times:
      (a) filepath_glob='*.md'                      — expect 1 match. A bare
          pattern matches the FILENAME at any depth.
      (b) filepath_glob='{copy_src}/globtest/*.md'  — expect 1 match.
      (c) filepath_glob='{copy_src}/*.md'           — expect 0 matches, with
          reason='no_documents_selected'.
    (c) is NOT a bug and must not be reported as one: '*' never crosses a '/',
    exactly as in a shell, so that pattern means "directly in {copy_src}" and
    the file is one level deeper. On 2026-08-29 this exact behavior was
    reported as a broken glob, because a zero from a filter and a zero from an
    absent string looked identical.
GL2. A ZERO IS DIAGNOSABLE: the (c) result above must carry
    reason='no_documents_selected', corpus_size, and a message saying the search
    NEVER RAN. Then find_literal pattern='zzmarker_definitely_absent_xyz' with
    NO glob — expect status success, total_matches 0 and reason='no_matches',
    with a message saying files were scanned and the string is genuinely absent.
    THE TWO REASONS MUST DIFFER. That distinction is the whole fix: an
    exhaustive tool whose zero is ambiguous produces confident wrong answers.
GL3. SCORES AND KEYS: search_similar filepath='{copy_src}/one.md' — expect every
    entry to carry a NON-NULL 'score' as well as 'similarity', and the entries
    to be in DESCENDING score order. A null score makes ranking and thresholding
    impossible on a tool whose entire output is a ranking. Then check the
    response carries result_key='similar_documents' and a 'results' array equal
    to 'similar_documents'. For search_knowledge (result_key 'results'), also
    assert its returned scores are descending. Do the same for list_documents
    ('documents' — named but NOT duplicated, because
    it is unbounded), list_backups ('backups') and find_literal ('matches').
    Reading 'results' off a search_similar response used to yield nothing and
    got a working tool reported as broken.
GL4. PATHS FEED BACK IN: take the FIRST hit from a search_knowledge for
    '{marker}_one' and pass its 'filepath' value STRAIGHT into get_document with
    no editing — expect success. Then confirm the same entry's 'source' is the
    absolute host path. Every result now carries both; before 5.0 only the
    absolute one was returned and it could not be fed into any tool without
    string surgery. Check the same pair on search_similar and on
    evaluate_retrieval's per_query.top_result_filepath.
GL5. COUNTS RECONCILE: list_documents (no filters), list_categories and
    get_index_stats — the document total must be IDENTICAL in all three, and
    every per-category count in list_categories must equal the number of
    list_documents entries with that category and the matching entry in
    get_index_stats.categories. Three views of one index that disagree mean one
    of them is reading stale state, and every "how many documents are there?"
    answer after that is a guess.
GL6. BACKUP IDS ARE DISTINCT WITHIN ONE SECOND: run five update_document calls on
    '{copy_src}/one.md' back to back, as fast as you can, then list_backups
    filepath='{copy_src}/one.md' — expect FIVE distinct backup_ids from this
    burst. Several writes inside one second are disambiguated with -1/-2/-3
    suffixes; without that the second write's backup would overwrite the first's
    and a recovery point would vanish silently.
GL7. INSERT REJECTS AN INVALID POSITION: insert_in_document
    filepath='{copy_src}/one.md' position='middle' text='x' — expect status
    error and a message NAMING the valid position values. An enum rejection that
    does not say what is valid costs a round trip every time.
GL8. CLEANUP: remove_document '{copy_src}/globtest/deep.md' delete_file=true —
    expect success and pruned_directories naming '{copy_src}/globtest'. Removing
    the last file in a directory prunes the now-empty parent; before 5.0 the
    directory was left behind and cleanup was an undocumented manual step.

DE-INDEX STEPS (5.7). remove_document has two meanings and until 5.7 only one of
them worked. Without delete_file it dropped the index row, left the file, and
returned status:"success" with a reindex_warning saying the watcher would undo it
on the next filesystem event — so `success` described a state that reverted, and
a client branching on `status` rather than reading the prose (the obvious thing
to do) believed a document was gone while search still returned it. Deleting an
already-de-indexed file then failed with not_found, leaving it STRANDED on disk:
invisible to search and unreachable by every tool. These steps prove both halves.
D1. A DE-INDEX STICKS: add_document '{deindex_file}' with content
    '{marker}_deindex'. Confirm list_documents names it. Then remove_document
    filepath='{deindex_file}' with NO delete_file. Expect status success,
    file_deleted false, indexing_suppressed TRUE, already_deindexed false, and
    was_indexed true. read_document '{deindex_file}' must still return the
    content — THE FILE IS KEPT, which is the whole point of the argument — while
    list_documents must NOT name it.
D2. AND IT SURVIVES A REINDEX: reindex_documents force=true, poll
    get_reindex_status until it finishes, then list_documents again. '{deindex_file}'
    must STILL be absent, and read_document must STILL return its content. A
    reappearance here is the 5.7 defect returning and is a FAIL.
    Then get_index_stats: .deindexed.count must be >= 1 and .deindexed.files must
    name '{deindex_file}'. An exclusion nobody can see is the failure mode this
    block exists to prevent — if the file is missing from the corpus, the stats
    have to say why.
D3. THE STRANDED FILE CAN BE DELETED: remove_document filepath='{deindex_file}'
    delete_file=true — the file has no index entry now, and before 5.7 this
    returned not_found and left it on disk forever. Expect status success,
    file_deleted TRUE, was_indexed FALSE and chunks_removed 0. Then read_document
    '{deindex_file}' must return not_found (disk gone) and get_index_stats
    .deindexed must no longer name it.
D4. WRITING TO A DE-INDEXED PATH RE-ADMITS IT: add_document '{deindex_file}'
    again, remove_document with NO delete_file, then add_document '{deindex_file}'
    with content '{marker}_readmit'. reindex_documents force=true, poll until it
    finishes, and list_documents MUST now name it — a write that landed and then
    lost its row to the next sweep would be the re-admission failing silently.
    CLEANUP: remove_document filepath='{deindex_file}' delete_file=true.

ERROR ENVELOPE STEPS (5.0.2). Every failure this surface can return is supposed
to name a machine-readable `reason`. Four of them did not until 5.0.2, and the
plan above tells you to branch on `reason` and never on message text — an
instruction that is only honest if the field is always there.
E1. REASON ON EVERY ERROR PATH. Create 'cognita-selftest-reasons/here.md' with
    add_document, then run each call below and assert BOTH that status is
    'error' AND that `reason` equals the value named. A null or missing reason
    is a FAIL even when the message text reads correctly — a client that has to
    parse prose to tell a missing file from a permission problem has no stable
    contract at all.
      get_document filepath='cognita-selftest-reasons/nope.md'    -> not_found
      remove_document filepath='cognita-selftest-reasons/nope.md' -> not_found
      move_document filepath=…/here.md new_filepath=…/here.md     -> same_path
      move_document …/here.md -> an EXISTING path          -> destination_exists
      Record the backup count for 'cognita-selftest-reasons/here.md' before
      these two refused moves. After EACH refusal, list_backups again and
      assert that the count is unchanged: validation happens before a move
      can create its source backup.
      update_document filepath='cognita-selftest-reasons/gone.md' -> not_found
      add_document with content='   ' (whitespace only)           -> invalid
      add_document filepath='cognita-selftest-reasons/x.zip'
                                                    -> unindexable_extension
      remove_document filepath='cognita-selftest-reasons/x.zip' delete_file=true
        (create the file on disk first if you have a shell; skip otherwise)
                                                    -> unindexable_extension
      search_knowledge query='   '                                -> invalid
      list_documents path_prefix='x'                     -> unknown_argument
      remove_directory prefix='cognita-selftest-reasons' (no delete_files)
                                                                  -> not_empty
    Then CLEANUP: remove_directory prefix='cognita-selftest-reasons'
    delete_files=true.
    Report any error payload anywhere else in this run that carried a null
    reason, with the tool and arguments that produced it.

RAW-TRANSPORT STEPS (10.1). SKIP THESE unless you can issue raw HTTPS POSTs to
this server's MCP endpoint yourself (a shell with curl, a sandbox with network
access). A connector client cannot perform them — report them SKIPPED, which is
not a failure. They exist because the raw endpoint is a SUPPORTED access path,
not an implementation detail, and a protocol change that broke it would
otherwise be invisible until a client silently fell back to something more
expensive.
R1. PLAIN POST: POST {{"jsonrpc":"2.0","id":1,"method":"initialize","params":
    {{"protocolVersion":"2025-03-26"}}}} to the canonical URL
    https://<public-origin>/mcp/connectors/<connector-slug>/mcp/v{combined_version} with
    Content-Type: application/json. Expect HTTP 200, Content-Type
    application/json (NOT text/event-stream), and a result carrying
    serverInfo.version. Assert specifically that no SSE framing was required and
    that NO session header was returned or is needed on the calls below.
R2. TOOLS OVER THE SAME POST: tools/call list_categories over a second plain
    POST with no session header — expect a parseable result with
    structuredContent deeply equal to the parsed first text block. Then
    tools/list — expect exactly 40 tool definitions, each with a valid object
    outputSchema. Record the normalized catalog digest. (R1+R2 catch a protocol change that would break
    every curl-based client and force a fallback to context-expensive
    connector-only paths.)
R3. BATCH: POST a JSON ARRAY of three requests with distinct ids — a valid
    list_categories, a valid get_index_stats, and one deliberately broken call
    (tools/call with name='no_such_tool'). Expect an ARRAY of three responses,
    ids matching one-for-one, the two valid ones carrying results, and the
    broken one carrying its own error WITHOUT failing the other two. Then POST
    an empty array [] — expect a JSON-RPC error, code -32600. A batch is a
    transport optimization, NOT a transaction: nothing rolls back.
R4. ERROR ENVELOPE: tools/call get_document with a filepath that does not exist.
    Expect HTTP 200, a well-formed result, isError TRUE on the result, and
    status:"error" inside the parsed text block. Both signals must agree. On a
    server older than 5.0.0, isError is hardcoded false — check
    serverInfo.version from R1 before treating isError alone as authoritative.
R5. SIZE CEILING: tools/call add_document filepath=
    'cognita-selftest-oversize.md' with content of about 6,000,000 characters.
    Expect status error, reason='too_large', with both size_bytes and
    limit_bytes reported, and read_document on that path to return not_found —
    NOTHING may be written and nothing may be truncated. A silently half-written
    builder still parses, which is what makes truncation the dangerous failure
    rather than the safe one.

OPTIONAL SHELL STEPS. SKIP unless you have a shell on the server itself; report
SKIPPED. These are the only way to test the second writer — cloud sync — because
every path in the tool surface goes through Cognita and therefore cannot
simulate a writer that does not.
S1. EXTERNAL MODIFICATION IS DETECTED: add_document
    'cognita-selftest-external.md' with content '{marker}_ext', note its
    content_sha256, then modify the file ON DISK outside Cognita (echo
    something into it). Without any reindex: get_document must return the NEW
    content (reads always come from disk), and list_documents
    prefix='cognita-selftest-external' include_hashes=true must report
    index_drift:true with a drift_hint. Then reindex_documents force=true and
    poll get_reindex_status until it finishes — index_drift must return to
    false. Clean up with remove_document delete_file=true.
S2. CONFLICT COPIES ARE NOT INDEXED: create '{conflict_name}' directly on disk
    in the documents folder with any text content, then reindex_documents
    force=true. Expect list_documents to NOT contain it, and get_index_stats
    .sync_conflicts to name it with count >= 1. Delete the file from disk
    afterwards. (This is the disk-side half of step 50, which only proves the
    write path refuses the name.)

GPU STEPS (6.0). SKIP every one of these and report SKIPPED unless GET /healthz
reports .embed.gpu == "ready" — on a machine with no GPU worker environment the
correct answer is "disabled" and there is nothing to test. The GPU is an
ACCELERATOR: every step below must hold identically on a CPU-only box, so a
failure here means the accelerator changed an ANSWER, never merely a duration.
G1. WHAT IS DOING THE WORK: GET /healthz and read .embed. Expect
    cpu_provider always present; when .gpu is "ready", expect .gpu_provider and
    a .devices array whose entries carry sysfs, pci and vram_free_gb. A device
    that does NOT qualify must appear in .devices_skipped WITH ITS REASON —
    "gpu: false" with no reason is the answer that sends someone hunting.
G2. THE DECISION IS LOGGED AND TRUE: add_document a file large enough to clear
    gpu_min_chunks (20 chunks ~= 16,000 characters of prose), then ask the
    operator for the 'embed.plan' and 'embed.done' lines for that walk. On
    embed.done, decision=gpu REQUIRES at least one device row with chunks > 0.
    decision=gpu with every device row at zero means the walk fell back to the
    CPU and said otherwise, which is the one thing these lines exist to prevent.
G3. THE VECTORS ARE USABLE, NOT MERELY PRODUCED: search_knowledge with
    hybrid_alpha=1.0 (semantic only) for a distinctive phrase from that
    document. It MUST come back with search_method 'semantic'. The query is
    embedded on the CPU and the document on the GPU, so this is the end-to-end
    proof that the two agree; a GPU that returns plausible-looking but
    incompatible vectors indexes cleanly and is unsearchable, which no count or
    status field can show.
G4. THE CANARY RAN: in the same log, expect one 'embed.gpu.canary' line PER
    QUALIFYING DEVICE with delta well inside tolerance (1.4e-07 against 1e-04 is
    typical). A missing canary line for a device that then embedded is a
    correctness hole, not a logging gap.
G5. THE MEMORY CAME BACK: on the per-device 'embed.gpu.device' lines expect
    released=OK, and vram_free_after within ~64MB of vram_free_before. This is
    the §8.8 proof and it is why the numbers are logged rather than asserted in
    prose. (These rows are emitted at pool TEARDOWN, which since 6.2 can be up
    to gpu_idle_linger_s after the walk finished — see G7.)
G6. NO GPU IS STILL A PASS: confirm the walk's summary reports errors=0 and
    outcome=ok. Every GPU failure mode is defined to end with the index correct
    and only the duration changed, so a red walk is never explained by "the GPU
    was busy".
G7. THE CARDS STAY WARM, AND THEN GO AWAY (6.2, as the index scheduler does
    it since 10.0): WITHIN 30 SECONDS of G2's large document, add_document a file
    of about 3,000 characters of GREEK or CJK prose. Chunks are at most 1,000
    characters, and the CPU lane may take any chunk up to 1 KB even while a card
    is ready; in a script of two or more bytes per character every full-size
    chunk is over 1 KB, so the card must take them (a short final chunk may
    still go to the CPU). English prose would split into chunks under 1 KB, and
    (a) would then be a race. Three things must hold together:
    (a) its embed.done says decision=gpu with a GPU device row of chunks > 0 — a
        later write reaching the warm card is the whole point of the linger;
    (b) there is NO new 'embed.gpu.init' or 'embed.gpu.canary' line between the
        two jobs. A second one means the card was started again and the linger
        bought nothing, which is the failure that would otherwise look like a
        success;
    (c) get_index_stats reports .scheduler.gpus with each card's state "ready"
        while nothing is being written.
    Then leave it alone for 40 seconds. (d) Expect the G5 'embed.gpu.device'
    rows, then one 'index scheduler GPU idle reap completed' line, and get_index_stats
    .scheduler.gpus now "cold" with reason "idle linger elapsed". A card that is
    never reaped holds VRAM for the life of the process and the CPU fallback
    hides it, so (d) is not optional: confirm the reap actually happened.
    (Before 15.0.2 this step asked for pool=warm and /healthz .embed.gpu_warm,
    which only the pre-scheduler pool path ever set, so it failed on every GPU
    machine while the cards were working.)
G8. A ONE-LINE WRITE IS CHEAP (6.3): repeat G7's timing with a ONE-LINE
    document. The scheduler may embed a chunk that small on either the CPU lane
    or the card, so decision=cpu is correct here. Whichever device row carries
    the chunk, its elapsed must be well under 0.2s. At gpu_batch_size 64 a
    one-chunk write on the card cost 1.83s, because `embed_batch` pads a slice
    out to the full batch, so the batch size was also the floor on what one
    chunk cost; at 4 it is ~0.08s. If a GPU row here ever reports ~1.8s again,
    the batch size has been raised back to 64 and the padding is back with it.
G9. THROUGHPUT IS OBSERVATIONAL (6.3): record the per-card and aggregate rate
    reported by the walk in G2 on embed.done. Rates are informational evidence
    that depends on workload, warm state, and hardware; do not pass or fail this
    self-test on a minimum throughput floor. Batch-shape correctness remains
    covered by the G8 elapsed-time check."""

_READONLY_PLAN = """COGNITA SELF-TEST PLAN — server version {version} (READ-ONLY project)

ROUTING (REQUIRED BEFORE STEP 1). Call list_projects with no project argument,
require the exact provisioned project name Self-Test, and include
project="Self-Test" on every project operation below. A missing list_projects
or required client-visible tool is a client_contract_mismatch and is BLOCKED / NOT
EXECUTED. Do not select or test any other project. A missing Self-Test project,
fixture path, manifest entry, or pinned hash is blocked: fixture_provisioning
(not an OCR or execution failure), never SKIPPED. A conforming OCR call that
returns an unexpected result is a server execution failure.
This project is read-only, so only the read surface can be exercised. Execute
in order and report a PASS/FAIL scorecard with brief evidence.

DISCOVERY DIAGNOSTICS (10.1). Compare complete normalized definitions from the
public catalog, authenticated/direct tools/list, and the client-visible schema
when accessible, retaining missing tools/arguments, required fields, enums,
defaults, and material descriptions. Classify outcomes as
client_contract_mismatch (client drift; blocked/not executed),
plan_contract_mismatch (invalid emitted instruction), or
server_execution_failure (a conforming call actually failed). An unavailable
inspection is UNVERIFIED, never PASS. For the 10.1 schema, recreate the client
connector with the canonical V3 URL shown by the Cognita Admin UI, complete
OAuth authorization, and repeat the comparison. A Cognita restart cannot
invalidate a client-owned cache, and there is no server-side in-place refresh.
Do not alter another server connector as a substitute. Report
coordinator-unavailable client actions as PENDING.

 1. list_documents — expect the project's documents.
 2. get_documents with one existing path and include_content=false — expect a
    one-entry documents collection in input order with facts but no content.
 3. `batch` with one or more read-only children, each carrying project=PROJECT,
    must preserve child order and return a named results collection.
 4. Pick one document; search_knowledge for a phrase you can see in its
    listing/preview — expect a scored hit.
 5. read_document on that document with a small start_line/end_line range —
    expect verbatim text plus content_sha256 and mtime.
 6. read_document with section=<one of its markdown headings> — expect that
    section, subsections included; unknown headings must return the available
    header tree as a hint.
 7. list_backups (no filepath) — expect any existing backups, newest first.
 8. If any backup exists: diff_backup on it — expect a unified diff or
    identical:true.
 9. NEGATIVE: attempt edit_document on any file — expect a read-only
    rejection. Nothing may be written.
10. TIERS: get_index_stats — expect a 'tiers' block reporting embedded and
    registered separately, with tiers.registered chunks and vectors both 0.
    If list_documents showed any entry with tier='registered', also check:
    get_document on it returns its full content with chunk_count 0, and
    search_similar on it returns reason='registered_document' (NOT a generic
    "not found" — the file demonstrably exists).
11. LITERAL SEARCH: pick a distinctive exact string you can SEE in the text
    read at step 3, then find_literal on it — expect at least one match whose
    filepath is that document and whose line_number points at the line you read
    it from (both tools number lines 1-indexed off the same text). Then
    find_literal pattern='zzmarker_definitely_absent_xyz' — expect status
    success with total_matches 0 and an empty matches array, NOT an error: an
    exhaustive search that finds nothing is a trustworthy answer, and that is
    the whole point of the tool. Finally find_literal pattern='[' regex=true —
    expect status error with reason='bad_pattern' and no traceback.
12. CATEGORIES: list_categories — expect the categories present in the index
    with counts, and the total to agree with list_documents from step 1. Pick
    one and re-run find_literal from step 9 with that category — expect the
    match to survive if the document is in that category, and 0 if it is not.
13. RETRIEVAL EVAL: evaluate_retrieval with test_cases
    '[{{"query": "<the phrase from step 2>", "expected_filepath": "<that
    document>"}}]' — expect status success with total_queries 1, mrr_at_5,
     recall_at_5 and a per_query breakdown. Read-only; it runs queries and
     scores them, writing nothing."""


_BOOK_READ_CHECKS = """
AUDIOBOOK AND PROJECT STORAGE CHECKS (16.0). Use only project=Self-Test. These
checks never read chapter prose or create audiobook state. A missing or disabled
book configuration is a bounded SKIPPED outcome, not a reason to create a book.

AB1. PROJECT FILES: call list_project_files with path="", recursive=false,
    limit=50. Record the bounded file count and policy_revision. If the exact
    'Project Files/Book_Layout.json' entry is present, read only that file with
    read_project_file and its returned expected_bytes_sha256; inspect only
    book_id, chapter_order, and registered chapter IDs/paths. Do not quote or
    read any working prose, tagged prose, production settings, or unrelated
    project file.

    Call book_get_index_status with project="Self-Test". If it returns
    reason='configuration_conflict' because no enabled book layout is
    registered, report SKIPPED: book_fixture_not_configured and stop this
    subsection without creating state or editing configuration. Otherwise
    require a successful bounded page whose chapter/file identities match the
    registered layout. This reports indexing/readiness metadata only.

    With an enabled layout, call audiobook_get_book for its book_id and
    audiobook_get_chapter for one registered chapter_id with include_text=false.
    Do not include user prose in the report. Call audiobook_get_generations for
    that chapter with limit=1 and omit include_prompt. Call audiobook_get_job
    only when a job_id is already supplied by an explicitly provisioned
    synthetic Self-Test fixture; otherwise report that lookup SKIPPED because
    no synthetic job ID is provisioned. These calls are read-only.

    audiobook_inspect_chapter and audiobook_find_chunk can expose or search
    spoken text. Call them only when the operator's existing Self-Test fixture
    record explicitly identifies that registered chapter as synthetic; use
    the bounded inspect response and a quote copied only from that synthetic
    fixture. Otherwise report SKIPPED: book_fixture_not_configured. Do not
    inspect, search, copy, or quote any other chapter's content.
"""

_BOOK_WRITE_GATE = """

SYNTHETIC AUDIOBOOK MUTATION GATE (16.0).
AB2. MUTATION SAFETY GATE (writable plan only). The ordinary Self-Test does not
    create or alter Book_Layout, chapter JSON, production settings, approval,
    DOCX, generation, take, import, build, export, or folder-indexing state.
    Therefore audiobook_prepare_chapter, audiobook_record_generation,
    audiobook_import_audio, audiobook_build, audiobook_commit_build,
    audiobook_cancel_job, and set_folder_indexing are NOT CALLED by this plan.
    Report these as SKIPPED: book_fixture_not_configured unless the operator has
    separately provisioned and explicitly identified a disposable synthetic
    book plus its expected source/take hashes and acceptance procedure. In
    that case, use only that external fixture-specific acceptance procedure;
    this plan still does not create fixtures or change its registration.
    Never invoke a paid TTS provider or use real/user audio. Absence of this
    separately provisioned fixture is not a Self-Test failure.
"""

_OCR_SELF_TEST = """

OCR CHECKS (10.1). Use only the persistent, synthetic Self-Test project and the
tracked provisioner manifest at src/cognita/selftest_fixtures/data/manifest.json.
Every fixture operation MUST use project="Self-Test" and the exact relative
path below. The provisioner verifies each file against this SHA-256 manifest;
missing project/path/manifest entry or any hash mismatch is blocked:
fixture_provisioning, never an OCR failure. Never select a personal image or
copy image bytes, OCR text, or fixture contents into ordinary logs.

  * cognita-selftest/ocr/canonical-clear.png —
    f371c0951f5ad07b2c039b4481ad23e746f2db18bee014e5d54a6de734bb8a63
  * cognita-selftest/ocr/blank.png —
    cf3adf0667963af8ed7a70f1902dcee28cb809f08f98853c59782e6b7021cebd
  * cognita-selftest/ocr/malformed.png —
    ef6f8789db64e06d89a40f688167580849a5fde7a62a921036637d1d297a5fff
  * cognita-selftest/ocr/animated.png —
    75bc52600e4bb7c790569621dc1f22b8814755fa3bef94786108c28e3eafda14
  * cognita-selftest/ocr/not-a-png.txt —
    ab5942ac996b7b5fa6d1f619cdc01ce9ef4158749633c72d48e6485aaf58a86a
  * cognita-selftest/ocr/over-limit.png —
    e8320adf84b0bc0bb82f75a616ab9586458ec5854a66be67750303d139b3d945

O1. Verify the canonical-clear fixture is provisioned with the pinned hash, then
    call ocr_asset project="Self-Test", filepath="cognita-selftest/ocr/canonical-clear.png",
    languages=["en"]. Require outcome text, normalized LF/NFC text, bounded
    regions, and an engine manifest with name/version/model_fingerprint/device/backend.
O2. Repeat the identical call. Require cache_hit=true and the same source hash,
    dimensions, text, region order, and pipeline fingerprint. A first call after
    deployment or service restart may correctly report cache_hit=false because
    the durable OCR cache is cold; the repeated call is the cache assertion.
O3. Search the canonical fixture's extracted token with search_assets and require
    an OCR provenance hit tied to the pinned source hash.
O4. ocr_asset project="Self-Test", filepath="cognita-selftest/ocr/blank.png",
    languages=["en"] must return cached no_text. Call OCR on the exact malformed,
    animated, non-PNG, and over-limit paths above; require each bounded refusal
    reason from its fixture class, with no image bytes or engine stderr. The
    over-limit refusal must use reason='too_large' and identify the applicable
    byte, dimension, or pixel limit in its message; it must not call a valid
    over-limit IHDR merely invalid.
O5. Verify the provisioned fixture hashes before and after extraction, cache hits,
    and refusals using the deployment provisioner's manifest verification evidence.
    The connector can corroborate hashes returned by successful OCR calls, but it
    cannot independently hash a fixture that is intentionally rejected before a
    result exists; report that distinction explicitly instead of claiming a
    client-side hash check. Do not replace or mutate a persistent fixture to test
    freshness; source-replacement races and engine-unavailable injection are
    server-side acceptance checks. In a read-only project, do no fixture
    creation/removal and classify missing provisioning as blocked:
    fixture_provisioning.
"""


# The plan version is a release identity check, not an independently versioned
# protocol. Derive it from the package version so the server and plan cannot
# silently drift apart during a release bump.
SELF_TEST_PLAN_VERSION = __version__
_SHARED_SECTION_INSTRUCTIONS = """SHARED SAFETY INSTRUCTIONS
Use only the named Cognita self-test fixtures. Every project-scoped operation
MUST use the exact provisioned project Self-Test, including ordinary document,
asset, OCR, read-only, and cleanup operations.
Do not select or test any other project. Include project="Self-Test" at the top
level of every project-scoped call. Do not execute prerequisites implicitly;
retrieve and run each requested section explicitly and in order.

SHARED SETUP INSTRUCTIONS
Call list_projects first and require the exact project name Self-Test. If it is
absent, fail before attempting project operations. Capture the raw result of every
call. Keep independent read-back/hash verification for byte fixtures.

SHARED CLEANUP INSTRUCTIONS
Run the cleanup IDs listed in this section even when an assertion fails, while
preserving backups required by the plan. Report cleanup failures separately.
"""


def _section(id_: str, title: str, *, group: str | None = None,
             prerequisites: tuple[str, ...] = (), cleanup: tuple[str, ...] = (),
             scope: str = "writable") -> dict[str, object]:
    return {"id": id_, "title": title, "group": group,
            "prerequisite_ids": list(prerequisites), "cleanup_ids": list(cleanup),
            "scope": scope}


# 15.0.1: the plan has steps 7b, 13b and 33b beside 1-51. The catalog listed 1-51 only, so
# those three could never be fetched, and (because a section ends at the next heading)
# step 7 ended at "7b." and step 13 at "13b.", silently dropping half of each.
_CORE_STEP_IDS: tuple[str, ...] = tuple(
    step_id
    for step in range(1, 52)
    for step_id in ((str(step), f"{step}b") if step in (7, 13, 33) else (str(step),))
)
# The read-only plan carries core steps 1-13 (no "b" steps) and the writable plan carries
# all of them, so a row's scope says "both" for those and "writable" for the rest.
# tests/test_self_test_plan_sections.py checks this against the two rendered plans.
_READONLY_CORE_STEP_IDS = frozenset(str(step) for step in range(1, 14))
_CORE_STEP_CATALOG = tuple(
    _section(step_id, f"Core self-test step {step_id}",
             prerequisites=("list_projects",),
             scope="both" if step_id in _READONLY_CORE_STEP_IDS else "writable")
    for step_id in _CORE_STEP_IDS
)
_NAMED_PLAN_CATALOG = (
    _section("D", "De-index checks", prerequisites=("list_projects",), scope="writable"),
    _section("D1", "De-index and retain", group="D", prerequisites=("D",), scope="writable"),
    _section("D2", "De-index survives reindex", group="D", prerequisites=("D1",), scope="writable"),
    _section("D3", "Delete stranded file", group="D", prerequisites=("D2",), scope="writable"),
    _section("D4", "Re-admit de-indexed path", group="D", prerequisites=("D3",), scope="writable"),
    _section("E", "Error envelope checks", prerequisites=("list_projects",), scope="writable"),
    _section("E1", "Stable error reasons", group="E", prerequisites=("E",), scope="writable"),
    _section("AB", "Audiobook and project storage checks", prerequisites=("list_projects",), scope="both"),
    _section("AB1", "Read-only audiobook and storage inventory", group="AB", prerequisites=("AB",), scope="both"),
    _section("AB2", "Explicit synthetic audiobook mutation gate", group="AB", prerequisites=("AB1",), scope="writable"),
    _section("GL", "Glob semantics checks", prerequisites=("list_projects",), scope="writable"),
    *tuple(
        _section(f"GL{step}", f"Glob semantics step {step}", group="GL",
                 prerequisites=(f"GL{step - 1}",) if step > 1 else ("GL",),
                 scope="writable")
        for step in range(1, 9)
    ),
)


_SECTION_CATALOG: tuple[dict[str, object], ...] = (
    *_CORE_STEP_CATALOG,
    *_NAMED_PLAN_CATALOG,
    _section("B", "Byte fidelity fixtures", prerequisites=("list_projects",), cleanup=("B5",), scope="writable"),
    _section("B1", "Whitespace and encoded-byte round trips", group="B", prerequisites=("list_projects",), cleanup=("B5",), scope="writable"),
    _section("B2", "Cross-extension byte round trips", group="B", prerequisites=("B1",), cleanup=("B5",), scope="writable"),
    _section("B3", "Verbatim update", group="B", prerequisites=("B2",), cleanup=("B5",)),
    _section("B4", "Independent read-path and encoded-byte agreement", group="B", prerequisites=("B3",), cleanup=("B5",), scope="writable"),
    _section("B4b", "Structured-format byte preservation", group="B", prerequisites=("B4",), cleanup=("B5",)),
    _section("B5", "Byte-fixture cleanup", group="B", prerequisites=("B4",)),
    _section("A", "Disposable asset checks", prerequisites=("list_projects",), cleanup=("A12",)),
    _section("A1", "Self-Test asset project and path preflight", group="A", prerequisites=("A",), cleanup=("A12",)),
    _section("A2", "Asset publication", group="A", prerequisites=("A1",), cleanup=("A12",)),
    _section("A3", "Asset operation replay", group="A", prerequisites=("A2",), cleanup=("A12",)),
    _section("A4", "Asset metadata read", group="A", prerequisites=("A2",), cleanup=("A12",)),
    _section("A5", "Asset byte read", group="A", prerequisites=("A4",), cleanup=("A12",)),
    _section("A6", "Asset search and listing", group="A", prerequisites=("A4",), cleanup=("A12",)),
    _section("A7", "Asset metadata update", group="A", prerequisites=("A4",), cleanup=("A12",)),
    _section("A8", "Asset stale guards", group="A", prerequisites=("A7",), cleanup=("A12",)),
    _section("A9", "Asset reindex", group="A", prerequisites=("A8",), cleanup=("A12",)),
    _section("A10", "Asset publication validation", group="A", prerequisites=("A9",), cleanup=("A12",)),
    _section("A11", "Guarded asset removal and replay", group="A", prerequisites=("A10",), cleanup=("A12",)),
    _section("A12", "Disposable asset cleanup", group="A", prerequisites=("A11",)),
    _section("O", "OCR checks", prerequisites=("list_projects",), cleanup=(), scope="both"),
    _section("O1", "OCR extraction and cache", group="O", prerequisites=("O",), cleanup=(), scope="both"),
    _section("O2", "OCR freshness and failures", group="O", prerequisites=("O1",), cleanup=(), scope="both"),
    _section("O3", "OCR provenance search", group="O", prerequisites=("O2",), cleanup=(), scope="both"),
    _section("O4", "OCR no-text and bounded refusals", group="O", prerequisites=("O3",), cleanup=(), scope="both"),
    _section("O5", "OCR provisioning evidence", group="O", prerequisites=("O4",), cleanup=(), scope="both"),
    _section("R-A", "Read-only asset checks", prerequisites=("list_projects",), cleanup=(), scope="readonly"),
    _section("R-A1", "Read-only asset listing", group="R-A", prerequisites=("R-A",), cleanup=(), scope="readonly"),
    _section("R-A2", "Read-only asset info", group="R-A", prerequisites=("R-A1",), cleanup=(), scope="readonly"),
    _section("R-A3", "Read-only asset lexical search", group="R-A", prerequisites=("R-A2",), cleanup=(), scope="readonly"),
    _section("R-A4", "Read-only asset semantic search", group="R-A", prerequisites=("R-A3",), cleanup=(), scope="readonly"),
    _section("R-A5", "Read-only asset bytes", group="R-A", prerequisites=("R-A4",), cleanup=(), scope="readonly"),
    _section("R", "Raw transport checks", prerequisites=("list_projects",), cleanup=(), scope="connector"),
    _section("R1", "Raw transport initialization", group="R", prerequisites=("R",), cleanup=(), scope="connector"),
    _section("R2", "Raw transport tool calls", group="R", prerequisites=("R1",), cleanup=(), scope="connector"),
    _section("R3", "Raw transport JSON-RPC batch", group="R", prerequisites=("R2",), cleanup=(), scope="connector"),
    _section("R4", "Raw transport error envelope", group="R", prerequisites=("R2",), cleanup=(), scope="connector"),
    _section("R5", "Raw transport size ceiling", group="R", prerequisites=("R2",), cleanup=("remove_document",), scope="connector"),
    _section("S", "Optional server-shell checks", prerequisites=("list_projects",), cleanup=(), scope="server"),
    _section("G", "GPU checks", prerequisites=("S",), cleanup=(), scope="server"),
)


def self_test_section_catalog(*, readonly: bool) -> list[dict[str, object]]:
    """Return stable section metadata; `available` says whether THIS plan can serve it.

    15.0.1: availability was read off each entry's hand-written `scope` and had drifted
    from the plans themselves. The writable index listed the read-only asset checks
    (which only the read-only plan contains); the read-only index listed the byte and
    raw-transport checks (which only the writable plan contains) and hid its own steps
    1-13. A client that asked for an "available" section got "operator-only" back. Now a
    section is available exactly when its text (or a child's) is in this mode's plan.
    The server-shell sections are the one deliberate exception: operator-only by design,
    listed for a writable connector as before.
    """
    return [dict(entry) for entry in _catalog_for_mode(readonly)]


@functools.lru_cache(maxsize=2)
def _catalog_for_mode(readonly: bool) -> tuple[dict[str, object], ...]:
    """Computed once per mode: the plan text is fixed for the life of the process."""
    plan = build_self_test_plan("0", readonly)   # headings do not depend on the version
    children: dict[str, list[str]] = {}
    for item in _SECTION_CATALOG:
        if item["group"]:
            children.setdefault(str(item["group"]), []).append(str(item["id"]))
    result = []
    for item in _SECTION_CATALOG:
        entry = dict(item)
        section_id = str(entry["id"])
        if entry["scope"] == "server":
            entry["available"] = not readonly
        else:
            entry["available"] = any(_rendered_section(plan, candidate)
                                     for candidate in (section_id, *children.get(section_id, ())))
        result.append(entry)
    return tuple(result)


# The intro paragraph each group's steps depend on: the rules they are judged by, the
# fixture paths, and (for assets) the exact image bytes. A client that asks for one
# section never sees the full plan, so this travels with every section of the group.
# 15.0.1: it did not, and a client running the asset checks by section never received
# the 68-byte fixture image, made up its own, and was refused on the pinned hash.
# SUPERSEDED (15.0.1, second pass): the four-entry `_GROUP_PREAMBLES` table that used to
# follow the comment above (B, A, R-A, O) fixed only those four groups. Every OTHER intro in the plan
# (registered tier, literal search, category, manifest, copy, glob, de-index, error
# envelope, raw transport, shell, GPU) is free text between the last heading of one group
# and the first heading of the next, and because a section ended only at the next HEADING
# each such intro was swallowed by the section BEFORE it and missing from the sections it
# governs: read-only step 13 ended with "If list_assets returns no assets, report this
# subsection SKIPPED" (so a client could skip step 13), E1 ended with the raw-transport
# "SKIP THESE" intro, step 26 swallowed the literal-search rules that govern 27+, and A12
# carried the OCR intro. `_INTRO_BLOCKS` below is the one ordered table that replaces it.


class _Intro(NamedTuple):
    """One intro block: free text that opens with `header` and governs the headings after it."""
    header: str                     # exact text the intro's first line starts with
    modes: frozenset[str]           # "writable" and/or "readonly": the plans it appears in
    governs: tuple[str, ...]        # heading ids from the one right after it, to the next intro
    group: str | None = None        # the catalog group those headings belong to, if any


_WRITABLE = frozenset({"writable"})
_READONLY = frozenset({"readonly"})
_BOTH_MODES = frozenset({"writable", "readonly"})

# ONE ordered table of every intro in the two rendered plans, in plan order (an intro that
# is nested inside another would be listed AFTER its outer one, and serving prepends the
# governing intros in this order, outer first; the plan has none nested today). An intro
# governs the headings from the one right after its text up to (not including) the next
# intro header or the end of the plan. The text before the first intro or heading is the
# plan-wide preamble (routing, failure reporting, cloud-sync ghost rules); it governs the
# whole run, is carried by _SHARED_SECTION_INSTRUCTIONS in spirit, and is not an intro.
# tests/test_self_test_plan_sections.py proves every entry against the rendered plans:
# each header occurs exactly once in its modes' plans and nowhere else, and the headings
# after it are exactly its `governs` list.
_INTRO_BLOCKS: tuple[_Intro, ...] = (
    _Intro("BYTE-FIDELITY STEPS.", _WRITABLE,
           ("B1", "B2", "B3", "B4", "B4b", "B5"), "B"),
    _Intro("REGISTERED-TIER STEPS (4.4).", _WRITABLE,
           tuple(str(step) for step in range(15, 27))),
    _Intro("LITERAL-SEARCH STEPS (4.5, find_literal).", _WRITABLE,
           ("27", "28", "29", "30")),
    _Intro("CATEGORY + FULL-REPLACE STEPS.", _WRITABLE,
           ("31", "32", "33", "33b", "34")),
    _Intro("MANIFEST + WRITE-GUARD STEPS (5.0).", _WRITABLE,
           tuple(str(step) for step in range(35, 42))),
    _Intro("COPY + DIRECTORY STEPS (5.0).", _WRITABLE,
           tuple(str(step) for step in range(42, 52))),
    _Intro("SEARCH SHAPE + GLOB STEPS (5.0).", _WRITABLE,
           tuple(f"GL{step}" for step in range(1, 9)), "GL"),
    _Intro("DE-INDEX STEPS (5.7).", _WRITABLE,
           ("D1", "D2", "D3", "D4"), "D"),
    _Intro("ERROR ENVELOPE STEPS (5.0.2).", _WRITABLE,
           ("E1",), "E"),
    _Intro("RAW-TRANSPORT STEPS (10.1).", _WRITABLE,
           ("R1", "R2", "R3", "R4", "R5"), "R"),
    # The next two are the server-shell groups: their steps are operator-only and are not
    # catalog rows, but their intros must still END the section before them (R5, S2).
    _Intro("OPTIONAL SHELL STEPS.", _WRITABLE,
           ("S1", "S2"), "S"),
    _Intro("GPU STEPS (6.0).", _WRITABLE,
           tuple(f"G{step}" for step in range(1, 10)), "G"),
    _Intro("ASSET CHECKS (10.1", _WRITABLE,
           tuple(f"A{step}" for step in range(1, 13)), "A"),
    _Intro("ASSET READ CHECKS (7.1)", _READONLY,
           tuple(f"R-A{step}" for step in range(1, 6)), "R-A"),
    _Intro("AUDIOBOOK AND PROJECT STORAGE CHECKS (16.0).", _BOTH_MODES,
           ("AB1",), "AB"),
    _Intro("SYNTHETIC AUDIOBOOK MUTATION GATE (16.0).", _WRITABLE,
           ("AB2",), "AB"),
    _Intro("OCR CHECKS (10.1)", _BOTH_MODES,
           tuple(f"O{step}" for step in range(1, 6)), "O"),
)


def _intro_blocks_for_mode(readonly: bool) -> tuple[_Intro, ...]:
    mode = "readonly" if readonly else "writable"
    return tuple(intro for intro in _INTRO_BLOCKS if mode in intro.modes)


# A heading is a step or section id at the start of a line: " 1." to " 9." are
# right-aligned in the plan (one leading space), "10." and up and every lettered id start
# in column 0. 15.0.1: the pattern used to demand column 0 and steps 1-9 were not found.
_HEADING_LINE = r"^ ?(?:[A-Z]+(?:-[A-Za-z]+)?\d+[A-Za-z]?|[A-Z]|\d+[A-Za-z]?)\. "


@functools.lru_cache(maxsize=1)
def _boundary_pattern() -> re.Pattern[str]:
    """Where a section or an intro ends: the next heading OR the next intro header."""
    headers = "|".join(re.escape(intro.header) for intro in _INTRO_BLOCKS)
    return re.compile(f"(?m)(?:{_HEADING_LINE}|^(?:{headers}))")


def _intro_text(plan: str, intro: _Intro) -> str:
    """One intro's own text, verbatim (ends at the next heading or intro header), or ""."""
    match = re.search("(?m)^" + re.escape(intro.header), plan)
    if not match:
        return ""
    end = _boundary_pattern().search(plan, match.end())
    return plan[match.start():end.start() if end else len(plan)].strip()


def _governing_intros(plan: str, readonly: bool, section_ids: list[str]) -> list[str]:
    """The intros that govern any of `section_ids`, once each, in plan order (outer first)."""
    texts = []
    for intro in _intro_blocks_for_mode(readonly):
        if intro.group in section_ids or any(step in intro.governs for step in section_ids):
            text = _intro_text(plan, intro)
            if text:
                texts.append(text)
    return texts


def _rendered_section(plan: str, section_id: str) -> str:
    """Extract one numbered section from a rendered plan without rewording it.

    Step numbers are right-aligned in the plan (" 1." .. " 9.", then "10."), so a
    heading may start with ONE space. 15.0.1: the pattern demanded column 0, found
    nothing for steps 1-9, and those were answered as operator-only.
    """
    match = re.search(r"(?m)^ ?" + re.escape(section_id) + r"\. ", plan)
    if not match:
        return ""
    # 15.0.1 (second pass): a section ends at the next heading OR the next intro header.
    # Ending only at a heading let each group/stage intro ride on the section BEFORE it.
    # This also retires the hand-cut "B" and "R" slices: a group has no heading of its own
    # and is served as its children, which now stop where the intros begin.
    end = _boundary_pattern().search(plan, match.end())
    return plan[match.start():end.start() if end else len(plan)].strip()


def select_self_test_plan(version: str, readonly: bool, section: str | None = None) -> dict:
    """Build full, index, group, or exact self-test output from one catalog."""
    catalog = self_test_section_catalog(readonly=readonly)
    metadata = {str(item["id"]): item for item in catalog if item["available"]}
    common = {"status": "success", "server_version": version, "plan_version": SELF_TEST_PLAN_VERSION}
    if section in {None, "full"}:
        return {**common, "section": "full", "plan": build_self_test_plan(version, readonly)}
    if section == "index":
        return {**common, "section": "index", "sections": catalog}
    item = metadata.get(section)
    if item is None:
        log.info("selftest.section unknown section=%s mode=%s", section,
                 "readonly" if readonly else "writable")
        return {"status": "error", "reason": "unknown_section", "section": section,
                "message": f"Unknown self-test section: {section}",
                "valid_sections": [str(row["id"]) for row in catalog if row["available"]],
                "plan_version": SELF_TEST_PLAN_VERSION, "server_version": version}
    plan = build_self_test_plan(version, readonly)
    child_ids = [str(row["id"]) for row in catalog if row.get("group") == section and row["available"]]
    served_ids = child_ids
    instructions = "\n\n".join(filter(None, (_rendered_section(plan, child) for child in child_ids)))
    if not instructions:
        served_ids = [str(section)]
        instructions = _rendered_section(plan, section)
    intro_count = 0
    if not instructions:
        instructions = "This section is available only to the connector/server operator; report it SKIPPED when that access is unavailable."
    else:
        # Every intro that governs a served section goes in front of it, once each, in plan
        # order (outer first); a group request adds the group's intros once, then each child.
        intros = _governing_intros(plan, readonly, served_ids)
        intro_count = len(intros)
        instructions = "\n\n".join([*intros, instructions])
    log.info("selftest.section served=%s mode=%s ids=%s intros=%d bytes=%d",
             section, "readonly" if readonly else "writable", ",".join(served_ids),
             intro_count, len(instructions))
    selected = {**common, "section": section, "section_id": section, "title": item["title"],
                "prerequisite_ids": item["prerequisite_ids"], "cleanup_ids": item["cleanup_ids"],
                "scope": item["scope"], "instructions": _SHARED_SECTION_INSTRUCTIONS + "\n" + instructions}
    selected["plan"] = selected["instructions"]
    return selected


def build_self_test_plan(version: str, readonly: bool) -> str:
    if readonly:
        return _READONLY_PLAN.format(version=version) + """

ASSET READ CHECKS (7.1). If list_assets returns no assets, report this subsection
SKIPPED; a read-only project cannot create the fixture.
R-A1. list_assets max_results=1. Expect zero/one item and null or opaque
      next_cursor. Follow a returned cursor and require a distinct next item.
      No entry carries search-only score or search_method fields: a listing
      is not a search (through 13.0.1 every entry leaked score 1.0/keyword).
R-A2. Choose one item. get_asset_info on its filepath must return success,
      matching filepath, 64-hex final_sha256, positive size/width/height,
      metadata, provenance_state, and catalog_drift=false. It must not contain
      search-only score or search_method fields.
R-A3. search_assets for a distinctive title/description/alt-text/tag/filename
      term with path_prefix=<parent>, max_results=5, hybrid_alpha=0. Require the
      chosen filepath. If it has tags, repeat with one exact tag filter.
R-A4. Repeat search_assets with hybrid_alpha=1. Require success and at most five
      results, each with score and search_method.
R-A5. get_asset with the filepath and R-A2 expected_sha256. Require exactly one
      bounded text block then one image/png block. Text/structured content must
      contain no base64 or search-only score/search_method fields. Decode the
      image; size and SHA-256 must equal R-A2.
      Repeat with 64 zeroes as expected_sha256; require reason='stale_file' and
      no image block. No read-only step mutates or reindexes an asset.
""" + _BOOK_READ_CHECKS + _OCR_SELF_TEST
    def indent(text: str) -> str:
        return "\n".join("    " + line for line in text.split("\n"))

    plan = _WRITABLE_PLAN.format(
        # 13.0 §4: the raw-transport step quotes a canonical URL, so the
        # generation in it comes from the authority rather than being spelled
        # in prose that a contract bump would silently leave stale.
        version=version, combined_version=COMBINED_CONTRACT_VERSION,
        test_file=TEST_FILE, test_file_moved=TEST_FILE_MOVED,
        deindex_file=DEINDEX_FILE,
        content=indent(_TEST_CONTENT),
        script_file=TEST_SCRIPT, script_md=TEST_SCRIPT_AS_MD,
        script_content=indent(_TEST_SCRIPT_CONTENT), script_marker=SCRIPT_MARKER,
        grep_file=GREP_FILE, grep_script=GREP_SCRIPT,
        grep_content=indent(_GREP_CONTENT),
        grep_script_content=indent(_GREP_SCRIPT_CONTENT),
        grep_replacement=indent(_GREP_REPLACEMENT),
        grep_marker=GREP_MARKER, grep_registered_marker=GREP_REGISTERED_MARKER,
        grep_marker_v2=GREP_MARKER_V2, grep_category=GREP_CATEGORY,
        h0=_h("H0"), h1=_h("H1"), h2=_h("H2"), h3=_h("H3"), h4=_h("H4"),
        copy_src=COPY_SRC_DIR, copy_dst=COPY_DST_DIR, marker=COPY_MARKER,
        conflict_name=CONFLICT_NAME, bytes_file=BYTES_FILE,
        crlf_base64=SELF_TEST_CRLF_BASE64,
        crlf_text_repr=repr(SELF_TEST_CRLF_BYTES.decode("utf-8")),
        # H5 is whatever step 36 measured — unlike H0-H4 it is NOT a constant of
        # this plan, because the pack files are written by the run rather than
        # pinned. Saying so inline stops a reader hunting for a value to compare.
        h5_note="the value you recorded in step 36, not a pinned constant",
    )
    return plan + f"""

ASSET CHECKS (10.1; preserving 7.1 coverage). These are REQUIRED. First call list_projects and
require the exact provisioned project name Self-Test with access='write'. Every
asset call in A1-A12 MUST pass project="Self-Test" at the top level; these
synthetic asset paths are project-relative paths inside that exact project.
Generate a fresh unpredictable RUN token. Replace {{RUN}} in these paths with
that token:

  A = '{ASSET_RUN_A}'
  B = '{ASSET_RUN_B}'
  disposable removal probe = '{ASSET_REMOVE_DISPOSABLE}'

The historical retained canaries '{ASSET_TEST_A}' and '{ASSET_TEST_B}' are
preservation checks only. They are outside this run's fixtures: do not pass
either path to any asset tool, and must never modify, reindex, or remove them.
If any per-run A, B, or disposable-removal path already exists, choose a new RUN
token before starting; do not overwrite or clean up an asset from another run. A12 is the
cleanup step for this run's disposable files. If the run stops early, execute
A12 using the exact RUN paths and current SHA-256 values before reporting.

Fixture image_url (copy exactly):
{ASSET_TEST_DATA_URL}
Decoded size={ASSET_TEST_SIZE}; SHA-256={ASSET_TEST_SHA256}.

A1. Call get_asset_info with project="Self-Test" for A, B, and the disposable
    removal probe. All three must return reason='not_found'. If any exists,
    choose another RUN token and repeat A1; never inspect, overwrite, or clean
    up a path owned by another run, and never inspect or replace the historical
    canaries. Keep all three absent paths fixed for the rest of this run.
A2. put_asset A and B with project="Self-Test", overwrite=false, and distinct
    'selftest-put-a-'+RUN and
    'selftest-put-b-'+RUN operation IDs, metadata_action='replace',
    metadata_storage='catalog', expected_received_size={ASSET_TEST_SIZE}, and
    expected_received_sha256='{ASSET_TEST_SHA256}'. Use title/description/tags:
    A='Cognita self-test alpha'/'violet compass canary'/
    ['cognita-selftest','alpha']; B='Cognita self-test beta'/
    'amber sextant canary'/['cognita-selftest','beta']. Set alt_text to
    'transparent self-test pixel' and source.type='generated'. Require
    success, matching path, received_size=final_size={ASSET_TEST_SIZE}, both
    hashes='{ASSET_TEST_SHA256}', 1x1 dimensions, catalog storage, indexed=true,
    idempotent_replay=false, and a positive metadata_revision. Record both revisions.
A3. Repeat A's exact put_asset call with project="Self-Test" and its operation ID; require
    idempotent_replay=true and unchanged hash/revision. Change only its title
    with that operation ID; require reason='operation_conflict'.
A4. get_asset_info project="Self-Test", filepath=A; require A2 hash/size/dimensions/metadata,
    catalog_drift=false, and embedded_metadata_present=false. Assert the
    top-level asset_id equals metadata.asset_id, and reject stray score or
    search_method fields.
A5. get_asset project="Self-Test", filepath=A with expected_sha256=<A2 hash>; require one bounded text block
    then one image/png block, no base64 or search-only score/search_method fields
    in text/structured content, and decoded size/hash exactly
    {ASSET_TEST_SIZE}/'{ASSET_TEST_SHA256}'. Repeat with 64
    zeroes; require reason='stale_file' and no image block.
A6. search_assets project="Self-Test", query='violet compass canary', path_prefix=
    '{ASSET_RUN_DIR}/', tags=['cognita-selftest','alpha'], max_results=5,
    hybrid_alpha=0; require A with metadata and score in [0,1]. Repeat for
    'transparent self-test pixel' with hybrid_alpha=1 and no tags; require
    success, at most five results, each with score and search_method.
    If either search returns zero results, require reason='no_matches'.
A7. list_assets project="Self-Test", prefix='{ASSET_RUN_DIR}/' max_results=1; require only A plus a
    nonempty next_cursor. Follow it with max_results=1; require only B, never A.
    Neither entry carries score or search_method: those are search-only fields
    and a listing is not a search (they leaked on every entry through 13.0.1).
A8. update_asset_metadata project="Self-Test", filepath=A using expected_sha256=<A2 hash>, operation_id=
    'selftest-meta-a-'+RUN, metadata_action='merge', metadata_storage='catalog',
    and description='violet compass canary updated', tags=
    ['cognita-selftest','alpha','updated']. Require unchanged bytes/hash,
    revision+1, idempotent_replay=false, and get_asset_info showing the merge
    with catalog_drift=false. Replay exactly; require idempotent_replay=true and
    no revision change. Change the description with the same operation ID;
    require reason='operation_conflict'. With a fresh operation ID and 64 zeroes
    as expected_sha256, require reason='stale_file' and no state change.
A9. Record both hashes. reindex_assets project="Self-Test", prefix='{ASSET_RUN_DIR}/' operation_id=
    'selftest-reindex-'+RUN; require success, indexed>=2, removed=0, errors=[],
    idempotent_replay=false. Replay exactly; require idempotent_replay=true.
    get_asset_info must show both original hashes, 1x1, catalog_drift=false:
    reindexing must never alter PNG bytes.
A10. put_asset project="Self-Test", filepath='{ASSET_RUN_DIR}/bad-size.png' with the fixture, operation_id=
     'selftest-bad-size-'+RUN, expected_received_size={ASSET_TEST_SIZE + 1}, and
     the certified hash. Require reason='size_mismatch'; get_asset_info must then
     return reason='not_found', proving validation published nothing.

A11. REMOVE A DISPOSABLE ASSET. put_asset project="Self-Test",
     filepath='{ASSET_REMOVE_DISPOSABLE}', overwrite=false, with operation_id=
     'selftest-remove-put-'+RUN, the certified image, and metadata whose title
     contains RUN. Require success and record its final hash. Verify that
     get_asset_info and search_assets with project="Self-Test" and the exact
     cognita-selftest-assets/ prefix both show this path. Call ocr_asset with
     project="Self-Test", filepath='{ASSET_REMOVE_DISPOSABLE}', languages=["en"];
     require a successful OCR/no_text result so the path-bound source mapping
     exists before deletion. Call remove_asset project="Self-Test",
     filepath='{ASSET_REMOVE_DISPOSABLE}', expected_sha256=<recorded hash>,
     operation_id='selftest-remove-'+RUN. Require status='success',
     file_deleted=true, catalog_removed=true, ocr_removed=true, exact deleted
     size/hash, a nonempty backup_id, and idempotent_replay=false. Repeat the
     identical remove_asset arguments and require the same deletion receipt with
     idempotent_replay=true. Then get_asset_info and ocr_asset on that exact path
     must return reason='not_found', and search_assets must no longer list it.
     Never use remove_document for PNGs.
A12. CLEAN UP THIS RUN. For A, B, and '{ASSET_REMOVE_DISPOSABLE}' only, inspect
     each exact path with project="Self-Test". If it exists, record the current
     final_sha256 and call remove_asset with that expected_sha256 and a distinct
     'selftest-cleanup-'+RUN+'-'+path operation_id. Require a verified backup
     receipt. If already absent, accept reason='not_found'. Confirm get_asset_info
     reports not_found for all three paths and search_assets with path_prefix=
     '{ASSET_RUN_DIR}/' no longer returns A or B. Do not use a wildcard removal,
     reindex as cleanup, or any path outside these three per-run files.

Score A1-A12 separately. A skipped asset step fails on a writable 10.1 project.
The historical '{ASSET_TEST_A}' and '{ASSET_TEST_B}' paths must remain untouched.
""" + _BOOK_READ_CHECKS + _BOOK_WRITE_GATE + _OCR_SELF_TEST
