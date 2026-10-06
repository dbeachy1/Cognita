"""Private MCP contract definitions and pure engine helpers.

Operation methods live in their responsibility modules and inherit onto the host.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from .assets.wire import ASSET_TOOL_DEFS
from .byte_facts import classify_text_bytes
from .editing import EXPECTED_SHA_PROPERTY, MAX_FILE_BYTES
from .literals import FIND_LITERAL_TOOL_DEF
from .result_contracts import attach_output_schema

log = logging.getLogger("cognita.engine")

MAX_RESULTS = 20
LOCAL_ENGINE_BASE = "http://cognita-local"  # never resolved; routes via ASGITransport

# 5.0 §11.5: the documented ceiling on content accepted by a write tool. Shared
# with the gateway's read/edit path (editing.MAX_FILE_BYTES) on purpose — a file
# Cognita accepts on a write but refuses to read back is a trap, and builders are
# now expected to live in Cognita. Over the limit is a clean refusal naming both
# numbers; nothing is ever truncated.
MAX_CONTENT_BYTES = MAX_FILE_BYTES

# How long a mutating call waits for the project write lock before answering
# `busy`. Long enough that ordinary contention between two writes is invisible,
# short enough that a caller queued behind a full corpus rebuild learns why
# rather than hanging until its own client times out.
WRITE_LOCK_WAIT_S = 20.0

# Wall-clock ceiling for one find_literal sweep. `re` offers no timeout, so a
# caller-supplied catastrophic-backtracking pattern would otherwise run forever
# in an uncancellable thread — and find_literal is READ-ONLY, so a read token
# could starve the executor every other to_thread call shares.
LITERAL_WALK_BUDGET_S = 20.0

# add_from_url follows redirects by hand so every hop is re-validated.
_MAX_REDIRECT_HOPS = 5


class _UrlRefused(Exception):
    """add_from_url refused a URL. Carries the wire `reason` to report."""

    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason


@dataclass(frozen=True)
class _ReindexContext:
    """Immutable admission context for one background reindex task."""

    project_name: str
    documents_dir: Path
    connector_id: str | None
    project_key_grant: str | None
    admission_revision: int | None

# copy_directory's per-call file ceiling. A pack is ~17 files; this exists so a
# prefix typo (an empty prefix meaning the whole corpus) is refused rather than
# silently duplicating 319 documents.
MAX_COPY_FILES = 500

# 6.0.13: write_documents' per-call ceiling. A worldbook push is ~14 files; this
# exists so a runaway caller cannot stage an unbounded number of temp files
# before the commit point, where every one of them is holding disk.
MAX_BATCH_DOCUMENTS = 100

# 9.2 plural reads/removals are bounded request-shaped operations. The limits
# apply only to the plural wire surface; individual get_document/read_document
# behavior remains compatible with its existing limits.
MAX_PLURAL_PATHS = 100
PLURAL_BODY_MAX_BYTES = 1 * 1024 * 1024
PLURAL_BODY_TOTAL_MAX_BYTES = 8 * 1024 * 1024


# ---------------------------------------------------------------------------
# Tool definitions — names/args/descriptions carried over from the 3.x engine
# ---------------------------------------------------------------------------


def _tool(name: str, description: str, properties: dict, required: list[str]) -> dict:
    return {
        "name": name,
        "description": description,
        "inputSchema": {"type": "object", "properties": properties, "required": required},
    }


ENGINE_TOOL_DEFS: list[dict] = [
    _tool(
        "search_knowledge",
        "Hybrid search combining semantic search + keyword search with cross-encoder "
        "reranking. Read-only. No side effects. Returns JSON with results including "
        "content chunks, source filepath, relevance score, and search method used — "
        "chunks, not full document content. Primary search tool — use for any topic or "
        "keyword lookup. Prefer search_similar() when you already have a reference "
        "document and want more like it; get_document() when you know the exact filepath. "
        "hybrid_alpha: 0.0 = keyword-only (exact technical terms), 0.3 = balanced default, "
        "1.0 = semantic-only (conceptual queries). min_score (0.0-1.0) discards weak "
        "results relative to this query's normalized scores. snippet_mode=true (default) "
        "truncates content to ~500 chars and adds "
        "content_length; use get_document() for full content.",
        {
            "query": {"type": "string", "description": "Search query text (1-3 keywords recommended)"},
            "max_results": {"type": "integer", "default": 5, "description": "Maximum results (max 20)"},
            "category": {"type": "string", "description": "Optional category filter; see list_categories()"},
            "hybrid_alpha": {"type": "number", "default": 0.3, "description": "0.0 keyword-only … 1.0 semantic-only"},
            "min_score": {"type": "number", "default": 0.0,
                          "description": "Minimum score to include, relative to this query's normalized scores"},
            "snippet_mode": {"type": "boolean", "default": True, "description": "Truncate content to ~500 chars"},
            "retrieval_profile": {
                "type": "string", "enum": ["editing", "canon", "instructions", "workflow"],
                "description": "Book search scope: editing includes labeled drafts and current references; canon includes approved current prose, fresh approved summaries, and canonical references; instructions and workflow search their registered documents. Omit for legacy non-book search behavior; enabled books default to canon.",
            },
        },
        ["query"],
    ),
    _tool(
        "get_document",
        "Get the full content of a specific document by filepath. Read-only. Use when you "
        "need the complete text of a known file — search_knowledge() returns chunks, not "
        "full docs. Use list_documents() to browse available paths. `content` is "
        "BYTE-VERBATIM for valid UTF-8: the file's bytes decoded without changing line "
        "endings, BOM or trailing whitespace, and sha256 of it equals bytes_sha256. "
        "Accepted malformed UTF-8 returns a visibly lossy U+FFFD view with "
        "content_is_lossy=true; request base64 for exact bytes. A binary format (.pdf/.docx/"
        ".xlsx/.pptx), which has no text on disk: there `content` is the extracted "
        "text and `content_is_extracted` is true. content_sha256 is the separate "
        "write-guard stamp (BOM dropped, CRLF/CR folded) that expected_sha256 wants.",
        {
            "filepath": {"type": "string", "description": "Relative path within the documents directory"},
            "content_encoding": {
                "type": "string", "enum": ["utf-8", "base64"], "default": "utf-8",
                "description": "Return exact original bytes as strict base64, or readable UTF-8 text.",
            },
        },
        ["filepath"],
    ),
    _tool(
        "search_similar",
        "Find documents semantically similar to a given reference document. Read-only. "
        "Uses the document's embedding for similarity comparison. The reference document "
        "must be indexed — call list_documents() to confirm it exists.",
        {
            "filepath": {"type": "string", "description": "Path to the indexed reference document"},
            "max_results": {"type": "integer", "default": 5, "description": "Similar documents to return (max 20)"},
            "retrieval_profile": {
                "type": "string", "enum": ["editing", "canon", "instructions", "workflow"],
                "description": "Book search scope: editing includes labeled drafts and current references; canon includes approved current prose, fresh approved summaries, and canonical references; instructions and workflow search their registered documents. Omit for legacy non-book behavior; enabled books default to canon.",
            },
        },
        ["filepath"],
    ),
    _tool(
        "get_documents",
        "Read up to 100 unique project-relative document paths in the exact order supplied. "
        "Read-only and non-snapshotting: a missing or unreadable path is reported in its "
        "own entry while later paths are still attempted. include_content=false returns "
        "facts only (including for files not currently indexed). UTF-8 bodies are capped "
        "at 1 MiB per file and 8 MiB total; use get_document or read_document for larger "
        "content. content_encoding=base64 returns exact original bytes.",
        {
            "filepaths": {
                "type": "array", "items": {"type": "string"},
                "description": "1-100 unique project-relative paths, returned in input order",
            },
            "include_content": {
                "type": "boolean", "default": True,
                "description": "Include bodies; false returns facts and index status only",
            },
            "content_encoding": {
                "type": "string", "enum": ["utf-8", "base64"], "default": "utf-8",
                "description": "Readable UTF-8 text or exact original bytes as strict base64",
            },
        },
        ["filepaths"],
    ),
    _tool(
        "list_documents",
        "List all indexed documents, optionally filtered by category and/or path "
        "prefix. Read-only. Use to browse the index or verify a file is indexed; use "
        "search_knowledge() to find documents by topic instead. "
        "include_hashes=true turns this into a MANIFEST: each entry also carries "
        "content_sha256, bytes_sha256, size_bytes and mtime read from the file ON DISK "
        "at request time (never from index state), plus index_drift. That makes a sync "
        "diff ONE call - compare hashes, pull only what changed - and it is the only "
        "way to see the index and the disk disagreeing. content_sha256 is the same hash "
        "read_document reports and the write tools' expected_sha256 accepts; "
        "bytes_sha256 is the raw-file hash sha256sum prints. Hashing reads every listed "
        "file, so narrow with prefix/category on a large corpus.",
        {
            "category": {"type": "string", "description": "Optional category filter; see list_categories()"},
            "prefix": {
                "type": "string",
                "description": (
                    "Optional path prefix, relative to the documents folder, matched as a "
                    "plain string against each document's relative path (e.g. "
                    "'notes/research'). Append a trailing '/' to scope "
                    "strictly to that directory rather than also matching siblings whose "
                    "names start with the same text."
                ),
            },
            "include_hashes": {
                "type": "boolean",
                "default": False,
                "description": (
                    "Add on-disk content_sha256/bytes_sha256/size_bytes/mtime/index_drift "
                    "to every entry"
                ),
            },
        },
        [],
    ),
    _tool(
        "list_categories",
        "List all document categories with their document counts. Read-only. Reflects the "
        "live index state. Use before filtering search_knowledge() or list_documents() by "
        "category.",
        {},
        [],
    ),
    _tool(
        "get_index_stats",
        "Get statistics and health metrics for the knowledge base index: total documents, "
        "total chunks, embedding model, per-category counts, and reindex progress. "
        "Read-only.",
        {},
        [],
    ),
    _tool(
        "get_reindex_status",
        "Get the current status of a background reindex operation. Lightweight — poll "
        "after reindex_documents() until reindex.active becomes false.",
        {},
        [],
    ),
    _tool(
        "evaluate_retrieval",
        'Evaluate search quality: whether search_knowledge() retrieves expected documents. '
        'Read-only. test_cases is a JSON string array like '
        '[{"query": "...", "expected_filepath": "..."}]. Returns MRR@5, Recall@5, and a '
        "per-query breakdown. MRR@5 above 0.7 indicates good retrieval quality.",
        {"test_cases": {"type": "string", "description": "JSON array of {query, expected_filepath}"}},
        ["test_cases"],
    ),
    _tool(
        "add_document",
        "Add a new document to the knowledge base from raw text content. Mutating — "
        "writes a file to disk and indexes it immediately; the document is searchable "
        "right away. Missing parent directories are CREATED, so this is also how a new "
        "pack directory comes into existence. Use update_document() to replace an "
        "existing file's content. If the path already exists the current file is backed "
        "up and then OVERWRITTEN — pass expected_sha256 to make that overwrite safe, "
        "and the result's previous_backup_id names the backup of what was buried. "
        "category is optional: omitted, it is derived from the path via the project's "
        "category mappings, falling back to the document's existing category on an "
        "overwrite and then to 'general'. Content is written BYTE-VERBATIM: nothing is "
        "stripped, added or newline-translated, so what you send is what the file holds.",
        {
            "content": {"type": "string", "description": "Full text content (markdown supported)"},
            "filepath": {"type": "string", "description": "Relative path within the documents directory"},
            "category": {
                "type": "string",
                "description": (
                    "Document category; omit to derive it from the path (see "
                    "list_categories())"
                ),
            },
            "expected_sha256": dict(
                EXPECTED_SHA_PROPERTY,
                description=(
                    "Optional staleness guard, applied when filepath ALREADY EXISTS: the "
                    "content_sha256 of the current file from a prior read_document, "
                    "get_document or list_documents(include_hashes=true). The overwrite is "
                    "refused with reason=stale_file if the file changed since — and also if "
                    "there is no file there at all, since you asked to replace a specific "
                    "version. Full hash or a prefix of at least 12 characters."
                ),
            ),
            "expected_bytes_sha256": {
                "type": "string",
                "description": "Optional exact SHA-256 of the current persisted bytes (64 hex characters).",
            },
            "content_encoding": {
                "type": "string", "enum": ["utf-8", "base64"], "default": "utf-8",
                "description": "Encoding of content; base64 is strict RFC 4648 and preserves arbitrary bytes.",
            },
        },
        ["content", "filepath"],
    ),
    _tool(
        "update_document",
        "Update the content of an existing document. Mutating — overwrites the file on "
        "disk and re-indexes immediately (full content replacement, not a patch). Old "
        "chunks are removed and replaced; the previous content is backed up first, and "
        "the result's previous_backup_id names that backup — pass it to restore_backup "
        "to undo exactly this write. Pass "
        "expected_sha256 whenever the read and the write are separated in time: this is "
        "the one tool that rewrites a WHOLE file, so without the guard it will happily "
        "clobber a change someone else made in between. Content is written BYTE-VERBATIM: "
        "nothing is stripped, added or newline-translated, so what you send is what the "
        "file holds.",
        {
            "filepath": {"type": "string", "description": "Path to an already-indexed document"},
            "content": {"type": "string", "description": "New full-text content"},
            "expected_sha256": EXPECTED_SHA_PROPERTY,
            "expected_bytes_sha256": {
                "type": "string",
                "description": "Optional exact SHA-256 of the current persisted bytes (64 hex characters).",
            },
            "content_encoding": {
                "type": "string", "enum": ["utf-8", "base64"], "default": "utf-8",
            },
        },
        ["filepath", "content"],
    ),
    _tool(
        "write_documents",
        "Write SEVERAL documents as ONE atomic unit — all of them land, or none do. "
        "Mutating. Use this instead of a loop of update_document/add_document calls "
        "whenever the documents form a SET that must agree with each other: sections of "
        "one work, a file plus an index or manifest compiled from it, anything where "
        "half-applied is worse than not-applied. "
        "Every document is validated first, so a rejection anywhere means nothing at all "
        "was written and documents_written is 0. The files are then staged and published "
        "together, which makes the window in which the set could be observed half-written "
        "microseconds long instead of the many seconds a per-document loop is exposed for. "
        "If indexing then fails for any reason, EVERY file is restored to its previous "
        "content and the call reports an error — you are never told a partial write "
        "succeeded. "
        "Each entry takes filepath and content, plus optional category and "
        "expected_sha256 (same meaning as on update_document — pass it per document when "
        "the read and the write are separated in time). Content is written BYTE-VERBATIM. "
        "A filepath may appear only once per batch.",
        {
            "documents": {
                "type": "array",
                "description": (
                    "The documents to write together, as objects with keys "
                    "filepath, content, and optionally category and "
                    "expected_sha256. 1-100 entries."
                ),
                "items": {"type": "object"},
            },
        },
        ["documents"],
    ),
    _tool(
        "remove_document",
        "Remove a document from the knowledge base index. Mutating. delete_file=true also "
        "permanently deletes the file from disk (irreversible) — it is backed up first, "
        "and the result's previous_backup_id names the backup holding it, so "
        "restore_backup can bring the file back. WITHOUT delete_file the file stays on "
        "disk and the path joins this project's DE-INDEX LIST: the watcher and every "
        "reindex skip it from then on, so the removal survives restarts and full "
        "rebuilds (get_index_stats lists every de-indexed path; writing to the path "
        "with add_document/update_document, or moving it, indexes it again). "
        "Check file_deleted for what happened to the file — it reports the OUTCOME, so "
        "it is false if the file was already gone — and was_indexed for whether there "
        "was an index entry to remove. delete_file=true works on a file that is not "
        "indexed, which is how an already-de-indexed file is deleted; that returns "
        "chunks_removed 0 with was_indexed false.",
        {
            "filepath": {"type": "string", "description": "Path to the indexed document"},
            "delete_file": {"type": "boolean", "default": False, "description": "Also delete the file from disk"},
        },
        ["filepath"],
    ),
    _tool(
        "remove_documents",
        "Remove 1-100 explicit project-relative documents in input order. Sequential "
        "and non-atomic: successful earlier calls remain applied after a failure; "
        "nothing rolls back, and other callers may interleave between elements. "
        "Each path reuses remove_document's backup and durable de-index behavior, so "
        "successful earlier removals remain applied after a later failure. delete_file "
        "defaults to false and never removes directories or recurses. on_error=stop "
        "skips remaining paths after the first error; on_error=continue attempts all "
        "independent paths. operation_id makes the completed plural result replayable, "
        "including per-path outcomes and backup receipts.",
        {
            "filepaths": {
                "type": "array", "minItems": 1, "maxItems": MAX_PLURAL_PATHS,
                "items": {"type": "string"},
                "description": "1-100 unique safe project-relative file paths, in execution order",
            },
            "delete_file": {
                "type": "boolean", "default": False,
                "description": "Also delete each file from disk after backing it up",
            },
            "on_error": {
                "type": "string", "enum": ["stop", "continue"], "default": "stop",
                "description": "Stop and skip later paths, or continue with independent paths",
            },
        },
        ["filepaths"],
    ),
    _tool(
        "move_document",
        "Rename or move an indexed document to a new path. Mutating — relocates the file "
        "on disk and updates the index in place (content is unchanged, so it is not "
        "re-embedded). The source is backed up first and the result's "
        "previous_backup_id names that backup. Refuses if new_filepath already "
        "exists. A rename is a move whose new name is in the same folder. An authorized "
        "directory move additionally requires operation_id and expected_policy_revision; it "
        "preserves explicit folder and per-file exclusion decisions.",
        {
            "filepath": {"type": "string", "description": "Current path of the indexed document"},
            "new_filepath": {"type": "string", "description": "Destination path (relative; parent dirs are created)"},
            "operation_id": {"type": "string", "description": "Required idempotency key for directory moves"},
            "expected_policy_revision": {"type": "integer", "minimum": 0,
                                         "description": "Required current policy revision for directory moves"},
        },
        ["filepath", "new_filepath"],
    ),
    _tool(
        "add_from_url",
        "Fetch content from a URL, convert to markdown, and add to the knowledge base. "
        "Mutating — makes an outbound HTTP request, strips HTML, saves to disk, and "
        "indexes immediately.",
        {
            "url": {"type": "string", "description": "Full http(s):// URL to fetch"},
            "category": {"type": "string", "default": "general", "description": "Document category"},
            "title": {"type": "string", "description": "Optional title (auto-detected if omitted)"},
        },
        ["url"],
    ),
    _tool(
        "reindex_documents",
        "Index or reindex all documents in the knowledge base. Runs in background — "
        "returns immediately; poll get_reindex_status(). force=true: smart reindex after "
        "manual on-disk edits. full_rebuild=true: re-embed everything from scratch. "
        "Normal add/update/remove auto-index — this tool is for out-of-band changes.",
        {
            "force": {"type": "boolean", "default": False, "description": "Smart reindex (changed files)"},
            "full_rebuild": {"type": "boolean", "default": False, "description": "Re-embed everything"},
        },
        [],
    ),
    # 4.5: exhaustive literal/regex search. Defined in literals.py rather than
    # inline because, unlike the 3.x-inherited fourteen above, it is new — its
    # wire shape is ours to own, so it lives with the logic that implements it.
    FIND_LITERAL_TOOL_DEF,
    # 5.0: whole-directory operations. Duplicating a source pack before editing
    # it is the project rule with the worst track record — done client-side it is
    # two calls per file that can partially fail, leaving a directory that LOOKS
    # complete and is not. As one call it either happens or it does not, and the
    # bytes never make a JSON round trip, so the copy is byte-exact by
    # construction and the trailing-newline question cannot arise.
    _tool(
        "copy_document",
        "Copy one document to a new path, byte for byte. Mutating — the copy is written "
        "to disk and indexed on arrival, exactly as add_document would, but the content "
        "never round-trips through JSON so the destination is a byte-exact duplicate. "
        "Missing parent directories are created. REFUSES by default if the destination "
        "exists; overwrite=true is opt-in and still backs up whatever it replaces. The "
        "destination inherits the source's category unless you pass one. Use this "
        "instead of get_document + add_document whenever you are duplicating rather "
        "than authoring.",
        {
            "src_filepath": {"type": "string", "description": "Existing document to copy from"},
            "dst_filepath": {"type": "string", "description": "Destination path (relative; parents are created)"},
            "overwrite": {
                "type": "boolean",
                "default": False,
                "description": "Allow replacing an existing destination (backed up first)",
            },
            "category": {
                "type": "string",
                "description": "Category for the copy; omit to inherit the source's",
            },
        },
        ["src_filepath", "dst_filepath"],
    ),
    _tool(
        "copy_directory",
        "Copy every document in a directory to another directory, byte for byte, in ONE "
        "call. Mutating. This is how a pack is ported: 17 files duplicated as a single "
        "operation instead of 34 client-side calls that can half-fail. NON-RECURSIVE by "
        "default (pack directories are flat) — pass recursive=true for the whole "
        "subtree. REFUSES before writing anything if ANY destination already exists, "
        "naming every conflict; overwrite=true is opt-in and backs up each file it "
        "replaces. Every copy is indexed on arrival and inherits its source's category "
        "unless you pass one. If a file fails mid-copy the whole call is rolled back, so "
        "the only way to get a partial destination is the process dying mid-call. "
        "Returns the file count and the full list of destination paths with their "
        "hashes, so no second enumeration is needed to verify.",
        {
            "src_prefix": {"type": "string", "description": "Source directory, relative to the documents folder"},
            "dst_prefix": {"type": "string", "description": "Destination directory (created if absent)"},
            "overwrite": {
                "type": "boolean",
                "default": False,
                "description": "Allow replacing existing destination files (each backed up first)",
            },
            "recursive": {
                "type": "boolean",
                "default": False,
                "description": "Include subdirectories (default: only files directly in src_prefix)",
            },
            "category": {
                "type": "string",
                "description": "Category for every copy; omit to inherit each source's",
            },
        },
        ["src_prefix", "dst_prefix"],
    ),
    _tool(
        "remove_directory",
        "Remove every document under a directory prefix from the index, in one call — "
        "the undo for copy_directory. Mutating. NON-RECURSIVE by default. REFUSES by "
        "default if the directory still holds files on disk, because de-indexing them "
        "would leave files nothing can find; pass delete_files=true to back each one up "
        "and delete it, which is the usual intent. Emptied parent directories are "
        "pruned. Every deleted file is backed up first, so the whole operation is "
        "recoverable as a set: list_backups with the same prefix returns exactly the "
        "backups this call created.",
        {
            "prefix": {"type": "string", "description": "Directory, relative to the documents folder"},
            "delete_files": {
                "type": "boolean",
                "default": False,
                "description": "Also delete the files from disk (each is backed up first)",
            },
            "recursive": {
                "type": "boolean",
                "default": False,
                "description": "Include subdirectories (default: only files directly in prefix)",
            },
        },
        ["prefix"],
    ),
]

# Additive 7.1 core-engine surface. The legacy workers engine is filtered by the
# gateway and never receives these definitions.
ENGINE_TOOL_DEFS.extend(ASSET_TOOL_DEFS)
# The implemented reservation/recovery and local raw-PCM import job surface is
# advertised together. Assembly/commit/index-status/book-read remain withheld
# until their durable implementations exist.
from .books.schemas import book_tool_definitions, project_storage_tool_definitions

_IMPLEMENTED_BOOK_TOOLS = {
    "audiobook_inspect_chapter", "audiobook_prepare_chapter",
    "audiobook_get_chapter", "audiobook_find_chunk",
    "audiobook_record_generation", "audiobook_import_audio",
    "audiobook_get_job", "audiobook_cancel_job", "audiobook_get_generations",
}
ENGINE_TOOL_DEFS.extend(
    item for item in book_tool_definitions() if item["name"] in _IMPLEMENTED_BOOK_TOOLS
)
ENGINE_TOOL_DEFS.extend(project_storage_tool_definitions())
ENGINE_TOOL_DEFS[:] = [attach_output_schema(tool) for tool in ENGINE_TOOL_DEFS]
ENGINE_TOOL_DEFS_BY_NAME: dict[str, dict] = {t["name"]: t for t in ENGINE_TOOL_DEFS}


def normalize_prefix(raw) -> str | None:
    """A caller's `prefix`/`src_prefix` as a relative posix string, or None.

    Leading './' and '/' are stripped so an absolute-looking path still matches
    the relative sources the index stores. A trailing slash is PRESERVED — it is
    the caller's way of saying "this directory only, not siblings that merely
    start with the same text", and quietly removing it would change the answer.
    """
    if not isinstance(raw, str):
        return None
    prefix = raw.strip().replace("\\", "/")
    while prefix.startswith("./"):
        prefix = prefix[2:]
    prefix = prefix.lstrip("/")
    return prefix or None


# Collection tools retain their established keys: `results`,
# `similar_documents`, `documents`, `backups`, and `matches`. Each response
# also names its key in `result_key`, allowing generic clients to read the
# correct collection without a lookup table. Bounded collections include a
# `results` alias for compatibility; unbounded `list_documents` does not, to
# avoid duplicating large responses (338 documents in the measured corpus).
def _collection(payload: dict, key: str, *, alias: bool) -> dict:
    payload["result_key"] = key
    if alias and key != "results":
        payload["results"] = payload[key]
    return payload


def _empty_selection_message(category, glob, include_registered, corpus_size) -> str:
    """Why did the filters select nothing? Name the filter, not just the zero."""
    filters = []
    if glob:
        filters.append(f"filepath_glob={glob!r}")
    if category:
        filters.append(f"category={category!r}")
    if not include_registered:
        filters.append("include_registered=false")
    if not filters:
        return (f"This knowledge base has {corpus_size} indexed document(s) and none "
                "were selected — the index may be empty or still building.")
    hint = ""
    if glob and "/" in glob and "**" not in glob:
        # The exact misreading that produced the 2026-08-29 report: '*' does not
        # cross a '/' in any glob dialect, so 'dir/*.txt' means "directly in dir"
        # and finds nothing when every file lives one level deeper.
        head, _, tail = glob.rpartition("/")
        hint = (" Note that '*' never crosses a '/': "
                f"{glob!r} matches files DIRECTLY in that directory only — use "
                f"{f'{head}/**/{tail}'!r} to include subdirectories.")
    return (
        f"NO DOCUMENTS WERE SELECTED, so nothing was scanned: {' and '.join(filters)} "
        f"matched 0 of this project's {corpus_size} indexed documents. This is NOT the "
        "same as the pattern being absent — the search never ran. Re-run without the "
        f"filter, or use list_documents to see the paths that exist.{hint}"
    )


def make_snippet(content: str, max_chars: int = 500) -> str:
    """Truncate content at a natural break point (3.x _make_snippet port)."""
    if len(content) <= max_chars:
        return content
    truncated = content[:max_chars]
    min_pos = int(max_chars * 0.6)
    last_nl = truncated.rfind("\n", min_pos)
    if last_nl > min_pos:
        return truncated[:last_nl].rstrip() + "\n..."
    for sep in (". ", "? ", "! ", "; "):
        last_sep = truncated.rfind(sep, min_pos)
        if last_sep > min_pos:
            return truncated[: last_sep + len(sep) - 1] + " ..."
    last_space = truncated.rfind(" ", min_pos)
    if last_space > min_pos:
        return truncated[:last_space] + " ..."
    return truncated + "..."


# ---------------------------------------------------------------------------
# Reads return the original stored bytes, not the indexer's extracted text.
# Extraction remains the input for chunking, embedding, and search; using it for
# reads would reformat JSON, remove Markdown frontmatter, join CSV cells, and
# normalize CRLF line endings.


def _verbatim_text(target: Path) -> str | None:
    """The file's own characters, or None when it is not UTF-8 text.

    No BOM strip and no newline folding: whatever `_write_verbatim` put on disk
    is what comes back. None means a genuinely binary format (.pdf/.docx/
    .xlsx/.pptx), which has no text on disk to return — those are the only
    documents a read may answer with extracted text, and it says so when it does.
    """
    try:
        view = classify_text_bytes(target.read_bytes())
        return view.text if view.accepted else None
    except OSError:
        return None
