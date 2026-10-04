"""Documents operations inherited by LocalEngineHost.

These methods use the host's existing project, config, core, store, connector, and
operation state; this class adds no fields or lifecycle behavior.
"""
from __future__ import annotations

import asyncio
import hashlib
import os
from pathlib import Path
from .backups import (
    BACKUPS_DIRNAME,
    BackupError,
    backup_id_of,
    backup_if_exists,
    resolve_target,
)
from .byte_facts import (
    MAX_BASE64_ATOMIC_SET_BYTES,
    byte_facts,
    check_expected_bytes_sha256,
    check_expected_bytes_sha256_digest,
    classify_text_bytes,
    decode_base64,
    validate_expected_bytes_sha256,
)
from .editing import SHA_PREFIX_MIN, sha_matches
from .manifest import TEXT_HASH_MAX_BYTES, file_facts, text_sha256
from .parsing import (
    TIER_REGISTERED,
    is_sync_conflict,
)
from .registry import Project
from .toolargs import wire_error
from .engine_contract import MAX_BATCH_DOCUMENTS, MAX_CONTENT_BYTES, MAX_PLURAL_PATHS, _collection, log


class EngineDocumentOperations:
    def _reject_backups_write(self, project: Project, target: Path) -> dict | None:
        """None unless `target` is inside `backups/` — the recovery tree.

        DESIGN-5.0 §7 states `backups/` "is never indexed or written through the
        tool surface", and until 5.1 that was true only for the two DIRECTORY
        tools, which check it in `_resolve_dir`. The four single-file writers did
        not, and `backup_if_exists` deliberately returns None for a path already
        under `backups/` — so the gateway's mandatory-backup hook took NO
        snapshot and raised no objection. `update_document("backups/DESIGN.2026
        0829-141203.md", ...)` therefore overwrote a recovery point silently and
        irreversibly. `move_document` was worse: it relocated a live document
        into a tree the reindex walk excludes, so the next reindex dropped its
        row while the file sat in the recovery tree.
        """
        try:
            rel = target.resolve().relative_to(Path(project.documents_dir).resolve())
        except ValueError:
            return None  # outside the project entirely — resolve_target's problem
        if rel.parts and rel.parts[0] == BACKUPS_DIRNAME:
            return {"status": "error", "reason": "invalid_path",
                    "message": (f"filepath points into {BACKUPS_DIRNAME}/, which is the "
                                "recovery tree: it is never indexed, and writing there — or "
                                "deleting from it — would destroy a restore point with no "
                                "backup of its own. Nothing was changed. Use restore_backup "
                                "to recover a version.")}
        return None

    def _reject_unindexable(self, project: Project, target: Path) -> dict | None:
        """None if `target`'s extension is indexable for this project, else the
        error payload to return — BEFORE anything touches the disk (4.6.0).

        Every write path used to write first and discover the extension during
        the parse that follows, which failed badly in both directions: an
        add_document to notes.zip left an orphan file on disk that nothing would
        ever index or list, and an update_document to an existing archive.zip
        OVERWROTE the archive with text and only then raised "Unsupported
        format". Cognita must never write a file it cannot index, so the check
        belongs in front of the write, not behind it.

        The message is addressed to the model on the other end of the connector,
        which cannot see this code: it states that nothing was written (so it
        neither retries nor tries to clean up), lists what it could have used
        (so a wrong-suffix mistake is fixable without another round trip), and
        names the alternative — hand the file to the user directly — because
        without an action to take the model tends to just apologize and stop.
        """
        policy = self.core.policy_for(project.name)
        if policy.tier_for(target.suffix) is not None:
            return None
        allowed = " ".join(sorted(policy.all_extensions))
        suffix = target.suffix or "(no extension)"
        return {
            "status": "error",
            "reason": "unindexable_extension",
            "message": (
                f"Cannot store {target.name!r} — Cognita only accepts files it can "
                f"index, and {suffix} is not one of them. NOTHING was written to disk.\n"
                f"Indexable extensions: {allowed}\n"
                "If this content is text, save it under an indexable extension "
                "(.md or .txt). Otherwise do not store it here — give the file to "
                "the user directly as a download or attachment instead. Cognita is "
                "a searchable document index, not general file storage."
            ),
            "allowed_extensions": sorted(policy.all_extensions),
            "filepath": str(target),
        }

    def _reject_sync_conflict(self, target: Path) -> dict | None:
        """None unless `target` is named like a cloud-sync conflict copy.

        4.6.0's rule is that Cognita never writes a file it cannot index, and
        5.0 stopped indexing conflict copies (2.7) — so writing to one of those
        names would create exactly the orphan that rule exists to prevent.
        """
        if not is_sync_conflict(target.name, self.core.sync_conflict_patterns):
            return None
        return {
            "status": "error",
            "reason": "sync_conflict_name",
            "message": (
                f"Refusing to write {target.name!r}: that name matches the cloud-sync "
                "conflict-copy patterns, and Cognita does not index those — the file "
                "would exist on disk and be invisible to every search. NOTHING was "
                "written. Choose a name without the conflict marker."
            ),
            "filepath": str(target),
        }

    @staticmethod
    def _reject_oversize(content: str | bytes) -> dict | None:
        """None unless `content` exceeds the documented ceiling (2.10).

        Checked BEFORE the write, so an over-limit document is refused whole
        rather than truncated — a half-written builder is worse than no builder,
        because it still parses.
        """
        size = len(content.encode("utf-8") if isinstance(content, str) else content)
        if size <= MAX_CONTENT_BYTES:
            return None
        return {
            "status": "error",
            "reason": "too_large",
            "message": (
                f"Content is {size} bytes; the limit is {MAX_CONTENT_BYTES}. NOTHING was "
                "written and nothing was truncated. Split the document, or store the "
                "oversized part outside the knowledge base."
            ),
            "size_bytes": size,
            "limit_bytes": MAX_CONTENT_BYTES,
        }

    @staticmethod
    def _decode_content(args: dict) -> tuple[bytes | None, dict | None]:
        """Decode one write payload exactly once, before any mutation."""
        content = args.get("content")
        encoding = args.get("content_encoding", "utf-8")
        if encoding not in ("utf-8", "base64"):
            return None, {"status": "error", "reason": "invalid",
                          "message": "content_encoding must be 'utf-8' or 'base64'."}
        if not isinstance(content, str):
            return None, {"status": "error", "reason": "invalid",
                          "message": "content must be a string."}
        try:
            data = decode_base64(content) if encoding == "base64" else content.encode("utf-8")
        except ValueError as exc:
            return None, {"status": "error", "reason": "invalid", "message": str(exc)}
        return data, None

    @staticmethod
    def _validate_input_bytes(target: Path, data: bytes) -> dict | None:
        """Apply text classification to text extensions before publication."""
        if target.suffix.lower() in {".pdf", ".docx", ".xlsx", ".pptx"}:
            return None
        view = classify_text_bytes(data)
        if not view.accepted:
            return {"status": "error", "reason": view.reason,
                    "message": view.message, **view.facts()}
        return None

    @staticmethod
    def _reject_stale(target: Path, expected) -> dict | None:
        """None unless `expected` (an expected_sha256) no longer matches disk.

        The engine-side half of the 2.3 write guard. The gateway checks this too
        before it takes its backup; this copy is what protects the admin API and
        stdio mode, which never pass through the gateway at all.

        A MISSING file is a rejection, not a pass: the caller named a specific
        version to replace, and "it is not there any more" is precisely the
        concurrent change the guard exists to catch.
        """
        if expected in (None, ""):
            return None
        expected = str(expected).strip().lower()
        if len(expected) < SHA_PREFIX_MIN:
            return {"status": "error", "reason": "invalid",
                    "message": f"expected_sha256 must be at least {SHA_PREFIX_MIN} hex characters."}
        if not target.is_file():
            return {
                "status": "error", "reason": "stale_file",
                "message": ("expected_sha256 was given, but no file exists at that path — "
                            "it was deleted or moved since you read it. Nothing was written."),
                "expected_sha256": expected, "actual_sha256": None,
            }
        # Ceiling before the read. Unlike manifest.file_facts (TEXT_HASH_MAX_BYTES)
        # and proxy._load_document_bytes (MAX_FILE_BYTES), this had none — so an
        # expected_sha256 aimed at a path that happens to hold a 300 MB PDF pulled
        # the whole file into memory, inside the event loop's thread.
        if target.stat().st_size > TEXT_HASH_MAX_BYTES:
            return {
                "status": "error", "reason": "too_large",
                "message": (f"The file on disk is larger than the {TEXT_HASH_MAX_BYTES} byte "
                            "hashing limit, so expected_sha256 cannot be checked against it. "
                            "Nothing was written."),
                "expected_sha256": expected, "actual_sha256": None,
            }
        actual = text_sha256(target.read_bytes())
        if actual is None:
            return {"status": "error", "reason": "not_text",
                    "message": "The file on disk is not valid UTF-8 text, so it cannot be hashed."}
        if not sha_matches(expected, actual):
            return {
                "status": "error", "reason": "stale_file",
                "message": "The file changed since you read it; nothing was written.",
                "expected_sha256": expected, "actual_sha256": actual,
                "hint": ("Re-read with read_document (or list_documents with "
                         "include_hashes=true) and rebuild the write against the "
                         "current content."),
            }
        return None

    def _prune_empty_parents(self, project: Project, target: Path) -> list[str]:
        """Delete now-empty parent directories of `target`, up to the documents
        root (never the root itself, never anything outside it).

        5.0 §11.2: remove_document with delete_file left the directory behind,
        so a probe into a fresh directory could not be fully undone and cleanup
        was an undocumented manual step. Best-effort by design — a prune failure
        must never turn a successful delete into a reported error.
        """
        docs = Path(project.documents_dir).resolve()
        pruned: list[str] = []
        try:
            parent = target.parent.resolve()
        except OSError:
            return pruned
        while parent != docs and parent.is_relative_to(docs):
            try:
                if any(parent.iterdir()):
                    break
                parent.rmdir()
            except OSError:
                break
            pruned.append(parent.relative_to(docs).as_posix())
            parent = parent.parent
        if pruned:
            log.info("Pruned empty director%s: %s",
                     "y" if len(pruned) == 1 else "ies", ", ".join(pruned))
        return pruned

    @staticmethod
    def _write_verbatim(target: Path, content: str | bytes) -> None:
        """Persist the exact UTF-8 or byte payload without newline translation.

        Text-mode writes changed LF to CRLF on Windows, while in-place writes
        exposed truncated files after interruption. Stage beside the target,
        fsync, and replace atomically so readers see the old or complete new
        bytes. Index normalization remains separate from stored content.
        """
        data = content.encode("utf-8") if isinstance(content, str) else bytes(content)
        tmp = target.with_name(target.name + ".cognita-tmp")
        try:
            with open(tmp, "wb") as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, target)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise

    @staticmethod
    def _undo_write(target: Path, previous: bytes | None) -> None:
        """Put `target` back after a write whose indexing failed.

        `previous` is the file's bytes before the write, or None if it did not
        exist — in which case undoing means removing the file we created. Best
        effort: a failure here is logged, never raised over the original error.

        Catch `BaseException` at the caller so shutdown cancellation also rolls
        back bytes already written before indexing. Restore through a sibling
        temp file and `os.replace`, so interruption cannot expose a partial
        rollback. The original failure remains an error to the caller.
        """
        try:
            if previous is None:
                target.unlink(missing_ok=True)
                return
            tmp = target.with_name(target.name + ".cognita-undo")
            try:
                with open(tmp, "wb") as fh:
                    fh.write(previous)
                    fh.flush()
                    os.fsync(fh.fileno())
                os.replace(tmp, target)
            except BaseException:
                tmp.unlink(missing_ok=True)
                raise
        except OSError:
            log.exception("Could not roll back the write to %s", target)

    @staticmethod
    def _stage_verbatim(target: Path, content: str | bytes) -> Path:
        """Write `content` to a staging file beside `target`. Do NOT publish it.

        Stage every batch file before publishing any target; the publish phase
        is a sequence of sibling-file renames.
        """
        tmp = target.with_name(target.name + ".cognita-batch")
        with open(tmp, "wb") as fh:
            fh.write(content.encode("utf-8") if isinstance(content, str) else bytes(content))
            fh.flush()
            os.fsync(fh.fileno())
        return tmp

    @staticmethod
    def _publish_staged(staged: list[tuple[Path, Path]]) -> None:
        """`os.replace` every staged file into place. The commit point.

        Each rename is atomic on its own, and this loop is the entire window in
        which a batch can be observed half-applied — microseconds, versus the
        ~6s per document that `index_file` costs. It contains no I/O that can
        block on the network, the model, or Postgres, and nothing here can raise
        for a reason that was not already true before the first rename.
        """
        for tmp, target in staged:
            os.replace(tmp, target)

    async def _write_documents(self, project: Project, args: dict) -> dict:
        """Validate, stage, publish, then index a batch of document writes.

        Validation and staging complete before targets change. Publication uses
        sibling `os.replace` calls; if indexing fails or is canceled, previous
        bytes are restored. The rename sequence is not journaled, so a hard
        process kill between renames can still leave a partially published set.
        """
        documents = args.get("documents")
        if not isinstance(documents, list) or not documents:
            return {"status": "error", "reason": "invalid",
                    "message": "documents must be a non-empty list of "
                               "{filepath, content} objects"}
        if len(documents) > MAX_BATCH_DOCUMENTS:
            return {"status": "error", "reason": "invalid",
                    "message": (f"{len(documents)} documents exceeds the "
                                f"{MAX_BATCH_DOCUMENTS}-document batch ceiling")}

        # ---- phase 1: validate EVERYTHING before touching anything ----------
        plan: list[tuple[Path, bytes, str, str | None]] = []
        seen: set[Path] = set()
        base64_bytes = 0
        for index, entry in enumerate(documents):
            if not isinstance(entry, dict):
                return {"status": "error", "reason": "invalid",
                        "message": f"documents[{index}] is not an object"}
            unknown = set(entry) - {"filepath", "content", "category",
                                    "expected_sha256", "expected_bytes_sha256",
                                    "content_encoding"}
            if unknown:
                return {"status": "error", "reason": "unknown_argument",
                        "message": (f"documents[{index}] has unknown key(s): "
                                    f"{sorted(unknown)}")}
            filepath = (entry.get("filepath") or "").strip()
            content, decode_error = self._decode_content(entry)
            if decode_error is not None:
                return {**decode_error, "filepath": entry.get("filepath")}
            assert content is not None
            if entry.get("content_encoding", "utf-8") == "base64":
                base64_bytes += len(content)
                if base64_bytes > MAX_BASE64_ATOMIC_SET_BYTES:
                    return {
                        "status": "error", "reason": "too_large",
                        "message": (
                            "Decoded base64 content exceeds the 32 MiB atomic-set limit. "
                            "NOTHING was written — the batch is all-or-nothing."
                        ),
                        "limit_bytes": MAX_BASE64_ATOMIC_SET_BYTES,
                        "documents_written": 0,
                    }
            category = (entry.get("category") or "").strip() or None
            if not filepath:
                return {"status": "error", "reason": "invalid",
                        "message": f"documents[{index}]: filepath cannot be empty"}
            if entry.get("content_encoding", "utf-8") == "utf-8" and not content.strip():
                return {"status": "error", "reason": "invalid",
                        "message": f"documents[{index}]: content cannot be empty",
                        "filepath": filepath}
            target = resolve_target(project.documents_dir, filepath)
            if target is None:
                return {"status": "error", "reason": "invalid_path",
                        "message": f"filepath resolves outside this project: {filepath!r}",
                        "filepath": filepath}
            # 🔴 The same duplicate path twice in one batch is a caller bug that
            # would make the result order-dependent — the second write wins and
            # the first is silently lost. Refuse it rather than pick.
            if target in seen:
                return {"status": "error", "reason": "invalid",
                        "message": f"{filepath!r} appears more than once in the batch",
                        "filepath": filepath}
            seen.add(target)
            for rejected in (self._reject_backups_write(project, target),
                             self._reject_unindexable(project, target),
                             self._reject_sync_conflict(target),
                             self._validate_input_bytes(target, content),
                             self._reject_oversize(content),
                             self._reject_stale(target, entry.get("expected_sha256"))):
                if rejected is not None:
                    # Nothing has been written. Say so explicitly — the whole
                    # point of the tool is that the caller can trust that.
                    return {**rejected, "filepath": filepath,
                            "documents_written": 0,
                            "message": (f"{rejected.get('message', 'rejected')} "
                                        f"NOTHING was written — the batch is "
                                        f"all-or-nothing and no file changed.")}
            expected_bytes = entry.get("expected_bytes_sha256")
            try:
                expected_bytes = validate_expected_bytes_sha256(expected_bytes)
            except ValueError as exc:
                byte_guard = {"status": "error", "reason": "invalid",
                              "message": str(exc), "guard": "expected_bytes_sha256"}
            else:
                actual_bytes = None
                if expected_bytes is not None and target.is_file():
                    with target.open("rb") as handle:
                        actual_bytes = hashlib.file_digest(handle, "sha256").hexdigest()
                byte_guard = check_expected_bytes_sha256_digest(actual_bytes, expected_bytes)
            if byte_guard is not None:
                return {**byte_guard, "filepath": filepath, "documents_written": 0,
                        "message": f"{byte_guard['message']} NOTHING was written — the batch is all-or-nothing."}
            plan.append((target, content, filepath, category))

        # ---- phase 2: stage every file, publishing none ---------------------
        staged: list[tuple[Path, Path]] = []
        previous: list[tuple[Path, bytes | None]] = []
        try:
            for target, content, _filepath, _category in plan:
                target.parent.mkdir(parents=True, exist_ok=True)
                previous.append(
                    (target, target.read_bytes() if target.exists() else None)
                )
                staged.append(
                    (await asyncio.to_thread(self._stage_verbatim, target, content),
                     target)
                )
        except BaseException:
            # Staging failed. No target has changed; drop the temps and report.
            for tmp, _target in staged:
                tmp.unlink(missing_ok=True)
            raise

        # ---- phase 3: publish. The commit point. ----------------------------
        await asyncio.to_thread(self._publish_staged, staged)

        # ---- phase 4: index, rolling the WHOLE batch back on any failure ----
        try:
            indexed = 0
            chunks_total = 0
            receipts: list[dict] = []
            for target, _content, _filepath, category in plan:
                outcome = await self.core.index_file(
                    project.name, Path(project.documents_dir), target,
                    category_override=category,
                )
                if outcome is None:
                    self._undo_batch(previous)
                    return {"status": "error", "reason": "parse_failed",
                            "message": ("A document produced no indexable text. The "
                                        "ENTIRE batch was rolled back; every file is "
                                        "as it was before the call."),
                            "filepath": self._rel(project, target.resolve()),
                            "documents_written": 0, "rolled_back": True}
                indexed += 1
                chunks_total += outcome[1]
                receipts.append({"filepath": _filepath, **byte_facts(target.read_bytes())})
        except BaseException:
            # 6.0.12's rule, applied to the set: synchronous undo (you cannot
            # await out of a cancellation) and the error still propagates.
            self._undo_batch(previous)
            raise

        return {"status": "success",
                "documents_written": indexed,
                "chunks_indexed": chunks_total,
                "receipts": receipts,
                "filepaths": [fp for _t, _c, fp, _cat in plan]}

    def _undo_batch(self, previous: list[tuple[Path, bytes | None]]) -> None:
        """Restore every file in a batch. Best effort, and never raises.

        Order does not matter — each `_undo_write` is independently atomic, and
        a batch that fails partway through its restore is still strictly better
        off than one that was never restored at all.
        """
        for target, prior in previous:
            self._undo_write(target, prior)

    async def _add_document(self, project: Project, args: dict) -> dict:
        content, decode_error = self._decode_content(args)
        if decode_error is not None:
            return decode_error
        assert content is not None
        filepath = (args.get("filepath") or "").strip()
        # 5.0 §5.3: an OMITTED category is no longer the literal string
        # "general". It falls through to index_file's override > stored >
        # path-mapping > "general" chain, which makes the assignment dependable
        # and stops an overwrite silently reclassifying a document that had a
        # deliberate category. An explicitly passed category still wins outright.
        category = (args.get("category") or "").strip() or None
        if args.get("content_encoding", "utf-8") == "utf-8" and not content.strip():
            return {"status": "error", "reason": "invalid", "message": "Content cannot be empty"}
        if not filepath:
            return {"status": "error", "reason": "invalid", "message": "Filepath cannot be empty"}
        target = resolve_target(project.documents_dir, filepath)
        if target is None:
            return {"status": "error", "reason": "invalid_path",
                    "message": f"filepath resolves outside this project: {filepath!r}"}
        for rejected in (self._reject_backups_write(project, target),
                         self._reject_unindexable(project, target),
                         self._reject_sync_conflict(target),
                         self._validate_input_bytes(target, content),
                         self._reject_oversize(content),
                         self._reject_stale(target, args.get("expected_sha256"))):
            if rejected is not None:
                # Relative, like every other filepath a tool emits (2.7): an
                # error the caller wants to retry against needs a path it can
                # actually pass back.
                return {**rejected, "filepath": filepath}
        byte_guard = check_expected_bytes_sha256(
            target.read_bytes() if target.is_file() else None,
            args.get("expected_bytes_sha256"),
        )
        if byte_guard is not None:
            return {**byte_guard, "filepath": filepath}
        target.parent.mkdir(parents=True, exist_ok=True)
        existed_before = target.exists()
        previous = target.read_bytes() if existed_before else None
        await asyncio.to_thread(self._write_verbatim, target, content)
        try:
            outcome = await self.core.index_file(
                project.name, Path(project.documents_dir), target, category_override=category
            )
        except BaseException:
            # 🔴 6.0.12: BaseException, and the undo runs SYNCHRONOUSLY. See
            # `_undo_write` — `except Exception` let the one failure mode that
            # actually happens in production walk straight past the rollback.
            self._undo_write(target, previous)
            raise
        if outcome is None:
            # The bytes are already on disk at this point, and index_file returns
            # None whenever the parse extracts NO text — which is not the same
            # condition as the content.strip() emptiness check above. A markdown
            # file that is nothing but YAML frontmatter passes validation, gets
            # written, and parses to "". Reporting parse_failed while leaving the
            # file there produced exactly the orphan 4.6.0 exists to prevent
            # (a file on disk that nothing will ever index), reached through
            # CONTENT instead of through the extension. Put the disk back, so
            # "this call failed" and "nothing changed" agree.
            await asyncio.to_thread(self._undo_write, target, previous)
            return {"status": "error", "reason": "parse_failed",
                    "message": ("The content produced no indexable text (an empty document, "
                                "or one that is only markdown frontmatter). NOTHING was "
                                "written — the file on disk is unchanged."),
                    "rolled_back": True}
        _, chunks_added = outcome
        # Route by extension at write time: a write to a registered-extension
        # path registers without embedding. chunks_added=0 is then the correct
        # outcome, not a failure — the markers say so explicitly.
        tier = self.core.policy_for(project.name).tier_for(target.suffix)
        # Report the category that was actually STORED, not the argument: with
        # 2.9's inference an omitted category is resolved during indexing, and a
        # response echoing "general" when the path mapping chose something else
        # would be the same silent lie the inference was meant to remove.
        stored = await self.store.get_document(project.name, self._rel(project, target.resolve()))
        return {"status": "success", "chunks_added": chunks_added, "dedup_skipped": 0,
                "category": stored.category if stored else (category or "general"),
                # 5.0 §5.1: filepath is RELATIVE — the form the tools accept —
                # and the absolute host path moves to `source`, which is what it
                # is called everywhere else. Before this, add_document echoed an
                # absolute path under the same key list_documents used for a
                # relative one.
                "filepath": self._rel(project, target.resolve()),
                "source": str(target),
                "content_sha256": text_sha256(target.read_bytes()),
                **byte_facts(target.read_bytes()),
                "tier": tier, "semantic_searchable": tier != TIER_REGISTERED}

    async def _update_document(self, project: Project, args: dict) -> dict:
        filepath = args.get("filepath") or ""
        content, decode_error = self._decode_content(args)
        if decode_error is not None:
            return decode_error
        assert content is not None
        if not filepath:
            return {"status": "error", "reason": "invalid", "message": "Filepath required"}
        if args.get("content_encoding", "utf-8") == "utf-8" and not content.strip():
            return {"status": "error", "reason": "invalid", "message": "Content cannot be empty"}
        target = resolve_target(project.documents_dir, filepath)
        if target is None:
            return {"status": "error", "reason": "invalid_path",
                    "message": f"filepath resolves outside this project: {filepath!r}"}
        # Ahead of the exists() check on purpose: the destructive case is an
        # update to a file that DOES exist (archive.zip), and write_text would
        # replace its bytes with text before the parse ever objected.
        for rejected in (self._reject_backups_write(project, target),
                         self._reject_unindexable(project, target),
                         # add_document has always refused a conflict-shaped name;
                         # update_document did not, so writing to an EXISTING
                         # notes-PC-conflict.md succeeded and indexed a row that the
                         # next reindex walk (which excludes conflict copies) then
                         # dropped. The write stuck and the document was unreachable.
                         self._reject_sync_conflict(target),
                         self._validate_input_bytes(target, content),
                         self._reject_oversize(content),
                         self._reject_stale(target, args.get("expected_sha256"))):
            if rejected is not None:
                return {**rejected, "filepath": filepath}
        if not target.exists():
            # Relative path: `target` is absolute, and every other not-found in
            # this file reports the caller's own filepath. An absolute host path
            # in a client-visible message leaks the server's directory layout.
            return {"status": "error", "reason": "not_found",
                    "message": f"File not found: {filepath}"}
        byte_guard = check_expected_bytes_sha256(target.read_bytes(), args.get("expected_bytes_sha256"))
        if byte_guard is not None:
            return {**byte_guard, "filepath": filepath}
        rel = self._rel(project, target.resolve())
        old_chunks = await self.store.chunk_count(project.name, rel)
        previous = target.read_bytes()
        await asyncio.to_thread(self._write_verbatim, target, content)
        try:
            outcome = await self.core.index_file(project.name, Path(project.documents_dir), target)
        except BaseException:
            # 🔴 6.0.12: see the twin in `_add_document`. BaseException, and the
            # undo is synchronous because you cannot await your way out of a
            # cancellation. The error still propagates — the caller is told the
            # write failed, which is now TRUE of the disk as well.
            self._undo_write(target, previous)
            raise
        if outcome is None:
            # See _add_document: the bytes have already landed. Leaving them
            # there meant the disk held the new content, the index still held the
            # OLD document's chunks (so search_knowledge and get_document served
            # text no longer in the file), and the caller had been told the call
            # failed. Restore, and say so.
            await asyncio.to_thread(self._undo_write, target, previous)
            return {"status": "error", "reason": "parse_failed",
                    "message": ("The content produced no indexable text (an empty document, "
                                "or one that is only markdown frontmatter). The document was "
                                "restored to its previous contents; the index is unchanged."),
                    "old_chunks_removed": 0, "rolled_back": True}
        _, new_chunks = outcome
        tier = self.core.policy_for(project.name).tier_for(target.suffix)
        return {"status": "success", "old_chunks_removed": old_chunks,
                "new_chunks_added": new_chunks, "dedup_skipped": 0,
                "filepath": rel, "source": str(target),
                "content_sha256": text_sha256(target.read_bytes()),
                **byte_facts(target.read_bytes()),
                "tier": tier, "semantic_searchable": tier != TIER_REGISTERED}

    async def _remove_documents(self, project: Project, args: dict) -> dict:
        """Remove explicit paths in order, preserving each single-file outcome.

        Validation is complete before the first backup or removal. The operation
        is intentionally non-atomic: ``remove_document`` remains the authority
        for locking, de-indexing, delete verification, and result facts, while
        this adapter adds bounded orchestration and per-path error policy.
        """
        filepaths = args.get("filepaths")
        if not isinstance(filepaths, list) or not filepaths:
            return {"status": "error", "reason": "invalid",
                    "message": "filepaths must be a non-empty list"}
        if len(filepaths) > MAX_PLURAL_PATHS:
            return {"status": "error", "reason": "invalid",
                    "message": f"filepaths exceeds the {MAX_PLURAL_PATHS}-path limit"}
        on_error = args.get("on_error", "stop")
        if on_error not in ("stop", "continue"):
            return {"status": "error", "reason": "invalid",
                    "message": "on_error must be 'stop' or 'continue'"}
        delete_file = args.get("delete_file", False)
        if not isinstance(delete_file, bool):
            return {"status": "error", "reason": "invalid",
                    "message": "delete_file must be a boolean"}

        validated: list[tuple[str, Path]] = []
        seen: set[Path] = set()
        for index, raw in enumerate(filepaths):
            if not isinstance(raw, str) or not raw or raw != raw.strip():
                return {"status": "error", "reason": "invalid",
                        "message": f"filepaths[{index}] must be a non-empty project-relative path"}
            if Path(raw).is_absolute():
                return {"status": "error", "reason": "invalid_path",
                        "message": f"filepaths[{index}] must be project-relative"}
            target = resolve_target(project.documents_dir, raw)
            if target is None:
                return {"status": "error", "reason": "invalid_path",
                        "message": f"filepath resolves outside this project: {raw!r}"}
            if (rejected := self._reject_backups_write(project, target)) is not None:
                return {**rejected, "filepath": raw}
            canonical = target.resolve()
            if canonical in seen:
                return {"status": "error", "reason": "duplicate_path",
                        "message": f"{raw!r} appears more than once in filepaths"}
            if target.exists() and target.is_dir():
                return {"status": "error", "reason": "invalid_path",
                        "message": f"{raw!r} is a directory; recursive removal is not supported"}
            seen.add(canonical)
            validated.append((raw, target))

        entries: list[dict] = []
        backups: list[dict] = []
        succeeded = failed = skipped = 0
        stopped = False
        for index, (filepath, _target) in enumerate(validated):
            if stopped:
                entries.append({"index": index, "filepath": filepath,
                                "status": "skipped", "reason": "previous_error"})
                skipped += 1
                continue

            result = await self._remove_document(
                project, {"filepath": filepath, "delete_file": delete_file}
            )
            if result.get("status") == "success":
                # The unlink owner takes the snapshot under its project lock.
                # Collect that exact receipt rather than taking a second backup.
                if (backup_id := result.get("previous_backup_id")) is not None:
                    receipt = {"filepath": result["filepath"], "backup_id": backup_id,
                               **{key: result[key] for key in
                                  ("deleted_mtime", "deleted_bytes_sha256") if key in result}}
                    backups.append(receipt)
                entries.append({"index": index, "filepath": filepath,
                                "status": "success", "result": result})
                succeeded += 1
            else:
                entries.append({"index": index, "filepath": filepath,
                                "status": "error", "reason": result.get("reason", "error"),
                                "error": result})
                failed += 1
                if on_error == "stop":
                    stopped = True

        status = "success" if failed == 0 and skipped == 0 else "partial_failure"
        return _collection({"status": status, "result_key": "documents",
                            "on_error": on_error, "documents": entries,
                            "succeeded": succeeded, "failed": failed,
                            "skipped": skipped, "backups": backups},
                           "documents", alias=False)

    async def _remove_document(self, project: Project, args: dict) -> dict:
        """De-index a document, optionally deleting the file (5.7 rewrite).

        Two defects are fixed here, and they are opposite halves of one mistake:
        treating the index row as the thing that exists.

        **delete_file=false was self-reverting.** It dropped the row, left the
        file, and returned `status: "success"` with a `reindex_warning` saying the
        watcher would put the row straight back. `success` is a postcondition
        claim, and that one held for an unbounded short interval and then undid
        itself — so a caller branching on `status`, the obvious thing to do, was
        told the wrong answer. The path now joins the project's de-index list
        (deindexed.py), which every walk consults, so the removal is DURABLE and
        `success` means what it says. Writing to the path again (add / update /
        copy / move) re-admits it.

        **delete_file=true could not reach an unindexed file.** The old order was
        de-index, then unlink, with a `not_found` return in between — so once a
        file had been de-indexed the delete failed and the file was STRANDED: on
        disk, invisible to search, unreachable by any tool. Recovery meant
        re-adding it and removing it again, which nothing documents. The target is
        resolved by PATH now, and the delete does not require a row.

        The delete happens FIRST and the de-index only if it succeeded — the order
        _remove_directory already uses, for its reason: the reverse leaves a live
        file on disk that no search can reach.

        Snapshot creation belongs beside the unlink, under the same lock. The
        2026-09-28 Windows bridge self-test deleted an existing unindexed source
        without a backup. An earlier gateway file check cannot protect a source
        appearing between those layers; the unlink must still get an undo receipt.
        """
        filepath = args.get("filepath") or ""
        delete_file = bool(args.get("delete_file", False))
        if not filepath:
            return {"status": "error", "reason": "invalid", "message": "Filepath required"}
        target = resolve_target(project.documents_dir, filepath)
        if target is None:
            return {"status": "error", "reason": "invalid_path",
                    "message": f"filepath resolves outside this project: {filepath!r}"}
        # Resolving by path rather than by row hands this tool a delete it never
        # had, and backups/ is where that new reach matters: the recovery tree is
        # never indexed, so until now every file in it was unreachable here BY
        # ACCIDENT. Make it unreachable on purpose — a restore point is the one
        # file in the tree with no backup of its own.
        refused = self._reject_backups_write(project, target)
        if refused is not None:
            return {**refused, "filepath": filepath}
        try:
            rel = self._rel(project, target.resolve())
        except ValueError:
            return {"status": "error", "reason": "not_found",
                    "message": f"Document not found in index: {filepath}"}
        deindexed = self.deindexed(project)
        # 5.0.2: de-index and unlink are ONE critical section, held against the
        # watcher — see _remove_directory, where the same race was observed live.
        async with self.core.write_lock(project.name):
            on_disk = await asyncio.to_thread(target.is_file)
            chunks_removed = await self.store.chunk_count(project.name, rel)
            was_indexed = await self.store.get_document(project.name, rel) is not None
            if not was_indexed and not on_disk:
                # Nothing here under either meaning of "here". Shape unchanged:
                # the self-test plan and idempotency.py both pin this reason.
                return {"status": "error", "reason": "not_found",
                        "message": f"Document not found in index: {filepath}"}
            if not was_indexed:
                # A file with no row — reachable now that a row is not required.
                # Bound that new reach to files this project could index: the
                # documents tree also holds images, archives and other things
                # Cognita deliberately does not manage, and a general delete
                # primitive over them is not what un-stranding a .md asks for.
                policy = self.core.policy_for(project.name)
                if policy.tier_for(target.suffix) is None:
                    return {
                        "status": "error", "reason": "unindexable_extension",
                        "message": (
                            f"{rel!r} has no index entry and "
                            f"{(target.suffix or target.name)!r} is not an extension "
                            "this project indexes, so remove_document does not manage "
                            "it and will NOT delete it. Nothing was changed. Indexable "
                            f"here: {' '.join(sorted(policy.all_extensions))}."),
                        "filepath": rel, "source": str(target),
                        "file_deleted": False, "delete_file_requested": delete_file,
                    }

            base = {"filepath": rel, "source": str(target),
                    "chunks_removed": chunks_removed, "was_indexed": was_indexed,
                    "delete_file_requested": delete_file}

            if not delete_file:
                # Durable de-index: the row goes, and the path joins the list the
                # walk consults, so nothing puts it back until something writes to
                # that path on purpose.
                await self.core.remove_file(project.name, rel)
                newly_listed = deindexed.add(rel)
                log.info("De-indexed %s/%s — file kept on disk, indexing suppressed",
                         project.name, rel)
                return {
                    **base, "status": "success", "file_deleted": False,
                    "pruned_directories": [],
                    # The field this fix is about. A caller reading only `status`
                    # is now right anyway; one reading this knows WHY it is right.
                    "indexing_suppressed": True,
                    "already_deindexed": not newly_listed,
                    "note": (
                        "The file is still on disk and this path is now on the "
                        "project's de-index list, so the watcher and every reindex "
                        "will skip it. The removal is DURABLE — it survives restarts "
                        "and full rebuilds. To index it again, write to the path "
                        "(add_document / update_document) or move it; get_index_stats "
                        "lists every de-indexed path."
                    ),
                }

            # 5.0.1 ghost forensics. A deleted fixture reappearing minutes later
            # has two completely different causes — the delete failed, or cloud
            # sync put the file back — and from the tool surface they look
            # identical. The discriminator is the mtime: a resurrected file
            # carries its ORIGINAL mtime, while a failed delete leaves a file that
            # was never removed. Capturing it here, before the unlink, is the only
            # moment the information exists; without it a later runner can only
            # guess, and the 2026-08-29 self-test run had to.
            deleted_facts: dict = {}
            previous_backup_id = None
            if on_disk:
                facts = await asyncio.to_thread(file_facts, target, hash_text=False)
                if facts.get("on_disk"):
                    deleted_facts = {
                        "deleted_mtime": facts.get("mtime"),
                        "deleted_size_bytes": facts.get("size_bytes"),
                        "deleted_bytes_sha256": facts.get("bytes_sha256"),
                    }
                try:
                    made = await asyncio.to_thread(
                        backup_if_exists, Path(project.documents_dir), rel,
                        keep=self._copy_backup_keep(),
                    )
                    previous_backup_id = backup_id_of(made) if made is not None else None
                    if previous_backup_id is None:
                        raise BackupError("No backup receipt was produced for the existing source")
                except BackupError as exc:
                    log.warning("Source deletion backup failed project=%s filepath=%s indexed=%s",
                                project.name, rel, was_indexed)
                    return {**base, "status": "error", "reason": "backup_failed",
                            "message": f"Removal aborted: {wire_error(exc)}. No changes were made.",
                            "chunks_removed": 0, "file_deleted": False}
                log.info("Deleting source project=%s filepath=%s indexed=%s backup_id=%s",
                         project.name, rel, was_indexed, previous_backup_id)
                try:
                    await asyncio.to_thread(target.unlink)
                except OSError as exc:
                    log.warning("Failed to delete file %s: %s", target, exc)
                    return {**base, **self._delete_failed(rel, target, wire_error(exc))}
                # Observed, not assumed. `file_deleted` used to be a straight echo
                # of the ARGUMENT, so it read true whatever happened — the same
                # class of lie as the reindex_warning above, and the reason
                # proxy.py has to re-check the disk behind this tool. unlink() can
                # also return without raising and leave the name in place (a sync
                # daemon recreating it inside the same tick). The row is still
                # intact here, so a failure leaves a consistent state.
                if await asyncio.to_thread(target.exists):
                    log.warning("Deleted %s but the path still exists", target)
                    return {**base, **self._delete_failed(
                        rel, target,
                        "the path still exists immediately after unlink() returned")}

            await self.core.remove_file(project.name, rel)
            # The file is gone by request, so a suppression on that path is moot —
            # and leaving it would silently suppress an unrelated file written
            # there later.
            deindexed.discard(rel)
            pruned = self._prune_empty_parents(project, target) if on_disk else []

        payload = {**base, "status": "success",
                   # True only when a file was there and is now gone. A row
                   # pointing at a path with no file (index drift, or a retry of a
                   # delete that already landed) deletes nothing, and calling that
                   # "deleted" would be the echo bug again.
                   "file_deleted": on_disk, "file_was_on_disk": on_disk,
                   "pruned_directories": pruned, "indexing_suppressed": False,
                   **deleted_facts}
        if previous_backup_id is not None:
            payload["previous_backup_id"] = previous_backup_id
        if deleted_facts:
            payload["ghost_check"] = (
                "If this path is present again later, compare its mtime to "
                f"deleted_mtime ({deleted_facts['deleted_mtime']}). An OLDER-or-equal "
                "mtime means cloud sync restored the file Cognita deleted — a "
                "GHOST, not a failed delete. A newer mtime means something wrote "
                "it again."
            )
        return payload

    @staticmethod
    def _delete_failed(rel: str, target: Path, detail: str) -> dict:
        """The delete_file=true refusal.

        The index row is deliberately still there. A caller told the delete failed
        can retry; a caller told that with the row ALREADY gone would be looking
        at a file no search can reach and no tool admits to owning — which is the
        stranding this rewrite exists to remove, arrived at from the other side.
        """
        return {
            "status": "error", "reason": "delete_failed",
            "message": (
                f"The file for {rel!r} was NOT deleted and its index entry was NOT "
                f"removed: {detail}. The document is still indexed and still "
                "searchable, so nothing is stranded. Usual causes are a file lock or "
                "cloud-sync interference; retry, or delete the file outside Cognita "
                "and let the watcher drop the row."),
            "filepath": rel, "source": str(target),
            "file_deleted": False, "chunks_removed": 0,
        }

    async def _move_document(self, project: Project, args: dict) -> dict:
        old_fp = args.get("filepath") or ""
        new_fp = args.get("new_filepath") or ""
        if not old_fp or not new_fp:
            return {"status": "error", "reason": "invalid", "message": "filepath and new_filepath are required"}
        old_t = resolve_target(project.documents_dir, old_fp)
        new_t = resolve_target(project.documents_dir, new_fp)
        if old_t is None:
            return {"status": "error", "reason": "invalid_path",
                    "message": f"filepath resolves outside this project: {old_fp!r}"}
        if new_t is None:
            return {"status": "error", "reason": "invalid_path",
                    "message": f"new_filepath resolves outside this project: {new_fp!r}"}
        # The DESTINATION extension decides indexability. A rename to an
        # unindexable suffix used to move the file on disk, delete the old index
        # row, and only then fail the parse — the document vanished from search
        # and the caller got a stack-trace string.
        for rejected in (self._reject_backups_write(project, new_t),
                         self._reject_unindexable(project, new_t),
                         self._reject_sync_conflict(new_t)):
            if rejected is not None:
                return rejected
        if old_t == new_t:
            return {"status": "error", "reason": "same_path", "message": "filepath and new_filepath are the same"}
        if not old_t.is_file():
            return {"status": "error", "reason": "not_found", "message": f"Document not found: {old_fp}"}
        if new_t.exists():
            return {"status": "error", "reason": "destination_exists", "message": f"destination already exists: {new_fp}"}
        docs = Path(project.documents_dir)
        old_rel = self._rel(project, old_t)
        new_rel = self._rel(project, new_t)
        # Move on disk first (atomic within the dir), then re-point the index. If the
        # index step fails the watcher reconciles the moved file within the debounce
        # window; the gateway already snapshotted the source before forwarding here.
        # 5.0.2: same critical section as the removals. A watcher flush between
        # the rename and the re-point sees a file at the NEW path with no row and
        # indexes it fresh, and move_file then re-points the old row onto a
        # source that already has one.
        async with self.core.write_lock(project.name):
            new_t.parent.mkdir(parents=True, exist_ok=True)
            await asyncio.to_thread(os.replace, str(old_t), str(new_t))
            doc_id, chunks_moved = await self.core.move_file(
                project.name, docs, old_rel, new_rel)
        return {"status": "success",
                "old_filepath": old_rel, "new_filepath": new_rel, "filepath": new_rel,
                "old_source": str(old_t), "new_source": str(new_t), "source": str(new_t),
                "doc_id": doc_id, "chunks_moved": chunks_moved}
