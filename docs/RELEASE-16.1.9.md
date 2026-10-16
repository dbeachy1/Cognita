# Cognita 16.1.9 release notes

A write refused before anything was written can now be retried with the same `operation_id`.

**Latest verified platform installers (16.1.0; these do not include this patch):**
[Windows installer 16.1.0](https://github.com/dbeachy1/Cognita/releases/download/v16.1.0/Cognita-Setup-16.1.0-r1.exe) |
[Linux source installer 16.1.0](https://github.com/dbeachy1/Cognita/releases/download/v16.1.0/cognita-src-16.1.0.tar.gz).

This is a source patch for existing installations. No new Windows executable or installer is supplied by this source patch.

**What was wrong.** A document write can pass an `operation_id` so that a retry after a timeout replays the first result instead of writing twice. The gateway remembered every result under its `operation_id` for an hour, errors included. That is right for most errors, but three refusals mean nothing was written and the cause passes on its own:

- `busy`: another write, usually a reindex, held the project for more than 20 seconds. Its message tells the caller to retry, passing an `operation_id`.
- `backup_failed`: the backup Cognita takes before every write failed, for example on a full disk. Its message says no changes were made.
- `source_unavailable`: the project folder was unmounted or being reconciled.

Because these were remembered, every retry with the same id got the same refusal back for an hour and never ran, even after the cause had cleared. This was seen on Kei when a `reindex_documents(full_rebuild=true)` was refused as `busy` behind a watcher walk: the retry, following the message's advice, replayed the refusal.

**What changes.** Those three refusals are no longer remembered. A retry with the same `operation_id` runs normally, and whatever it returns is what later retries replay. Every other failure is still remembered, because a retry must never repeat a write that may already have happened. Each of the three reasons is returned only before anything is written, which is what makes this safe. Asset, book and project-storage tools keep their own receipts and were not affected.

MCP arguments, results, and connector generations are unchanged (combined v6, Workspace v3).
