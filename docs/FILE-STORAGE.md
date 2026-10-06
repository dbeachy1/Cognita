# Project file storage

Cognita's project file tools answer two different questions: **what files exist in the authorized project tree**, and **which files are eligible for semantic indexing**. An indexing exclusion does not hide a file from the structural catalog, remove it from backup, or prevent an authorized exact-file read.

## List and read

`list_project_files` inventories a normalized project-relative path, optionally recursively, with a bounded page and cursor. Entries report path, type, size when known, `effective_read_only`, `effective_indexed`, `exclusion_reason`, and index state/error where available. The listing is structural: excluded folders and files such as source masters, originals, tagged copies, media, archives, and managed book artifacts can still appear. Use its effective indexing fields to understand policy; do not infer that `not_indexed` means missing.

`read_project_file` returns a base64-encoded byte range plus the full file size and SHA-256. Reads are exact bytes, not decoded document text. The default range is 256 KiB and the maximum is 1 MiB. For a continuation, pass the returned `next_offset` and full-file `bytes_sha256` as `expected_bytes_sha256`; nonzero offsets require this digest. Cognita hashes and checks the source while reading, so a changed file returns `stale_file` rather than mixing revisions. DOCX lock checks are reported as `file_locked`.

Paths are project-relative and may not traverse outside the project. Symlinks are inventoried as `type: "symlink"`, with `size_bytes: null` and `effective_read_only: true`; reads do not follow them and return `permission_denied`. Managed `.cognita-storage` state is protected from this generic reader. Audiobook tools access their own committed snapshots and state through their guarded APIs.

For example, a range loop starts with offset zero and no expected hash, then repeats with the previous response's `next_offset` and `bytes_sha256`. Concatenate decoded `content_base64` ranges in order; the same `bytes_sha256` describes the complete file, not an individual page.

## Index policy is separate

`set_folder_indexing` changes a folder rule using `expected_policy_revision` and an operation ID. A successful rule change advances the policy revision; replaying the same operation is receipt-backed. Protected managed state cannot be made indexable through a folder rule. Hard registered roots and protected locations win over a broad positive rule. Unknown locations default to excluded under book policy.

Book indexing is opt-in through the exact registered layout and role policy. Only active working chapters, eligible registered references/instructions/workflow documents, and approved fresh summaries qualify. Tagged copies, source masters, originals, snapshots, audio, backups, configuration, and operational state do not become semantic content merely because a folder rule includes their parent. A saved chapter and its Knowledge index status are separate: an index job may be pending, blocked, or failed while the saved bytes remain valid. Reindexing after restore includes only registered eligible sources; it does not embed every restored archive.

Use `book_get_index_status` to distinguish registered role, current raw hash, indexed raw hash, approval/freshness, and blocked/pending/failed state. A structural listing or exact byte read does not index, approve, or generate anything.

For semantic search, `search_knowledge` and `search_similar` accept an optional `retrieval_profile`: `editing`, `canon`, `instructions`, or `workflow`. On an enabled book, omitting it selects `canon`; on a non-book project, omission keeps the legacy behavior. Editing can include clearly labeled current drafts. Canon is limited to approved current prose, fresh approved summaries, and canonical references. Instructions and workflow material remain separate profiles. Book-profile hits may expose `book_role`, `chapter_id`, `editorial_status` (`draft`, `approved`, or `null`), and `summary_freshness` (`fresh`, `stale`, `unapproved`, or `not_applicable`); legacy hits omit those fields. The book filter is applied before ranking limits when possible and checked again before a result is returned, including a current raw-source check. A role change in search never makes a file writable or causes an excluded source master to disappear from the structural listing.

## Backups and restore

Existing `list_backups`, `diff_backup`, and `restore_backup` operate on Cognita's ordinary per-file document backups. A restore first preserves the current version so it remains undoable. These backups do not replace an audiobook media backup: PostgreSQL and the document index do not contain the SQLite book authority or native audio.

The scoped audiobook backup/restore procedure is a separate release/operations gate. Quiesce project mutations under the project lock, pin registered files, create a consistent SQLite copy with SQLite's backup API, and copy the registered layout, working DOCX, originals, immutable snapshots, native chunks, builds, and required backup archives. Verify the manifest hashes before activation and then rebuild only registered Knowledge content. Retain accepted history; there is no automatic pruning of old book media. The media restore procedure and an actual representative media-file restoration must be proven before production. Do not claim that a configured external backup destination covers book media until a restore from that destination has been observed.
