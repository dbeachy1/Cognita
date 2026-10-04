"""Transfers operations inherited by LocalEngineHost.

These methods use the host's existing project, config, core, store, connector, and
operation state; this class adds no fields or lifecycle behavior.
"""
from __future__ import annotations

import asyncio
import ipaddress
import re
import shutil
import socket
from pathlib import Path
from urllib.parse import urlparse
import httpx
from .backups import (
    BACKUPS_DIRNAME,
    BackupError,
    backup_id_of,
    backup_if_exists,
    resolve_target,
)
from .manifest import file_facts
from .parsing import (
    is_sync_conflict,
)
from .registry import Project
from .toolargs import wire_error
from .engine_contract import MAX_CONTENT_BYTES, MAX_COPY_FILES, _MAX_REDIRECT_HOPS, _UrlRefused, log, normalize_prefix


class EngineTransferOperations:
    async def _add_from_url(self, project: Project, args: dict) -> dict:
        url = (args.get("url") or "").strip()
        category = args.get("category") or "general"
        title = args.get("title") or None
        if not url:
            return {"status": "error", "reason": "invalid", "message": "URL cannot be empty"}
        if not url.startswith(("http://", "https://")):
            return {"status": "error", "reason": "invalid", "message": "Only http:// and https:// URLs are supported"}
        try:
            body, url = await self._fetch_public_url(url)
        except _UrlRefused as exc:
            return {"status": "error", "reason": exc.reason, "message": str(exc)}
        except Exception as exc:
            return {"status": "error", "reason": "fetch_failed",
                    "message": f"Failed to fetch URL: {wire_error(exc)}"}

        from bs4 import BeautifulSoup

        soup = BeautifulSoup(body, "html.parser")
        for tag in soup(["script", "style", "nav", "footer", "header"]):
            tag.decompose()
        if not title:
            title_tag = soup.find("title")
            title = title_tag.get_text(strip=True) if title_tag else url.split("/")[-1]
        text = soup.get_text(separator="\n", strip=True)
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        clean_text = f"# {title}\n\nSource: {url}\n\n" + "\n\n".join(lines)
        safe_title = re.sub(r"[^\w\s-]", "", title).strip().replace(" ", "-").lower()[:60]
        return await self._add_document(project, {
            "content": clean_text,
            "filepath": f"{category}/{safe_title or 'page'}.md",
            "category": category,
        })

    async def _fetch_public_url(self, url: str) -> tuple[str, str]:
        """GET `url`, refusing anything that resolves to a non-public address.

        `add_from_url` is a server-side fetch whose RESULT is stored and readable
        back out through get_document/read_document/find_literal — so an
        unrestricted one is not a blind SSRF, it is a read primitive into
        whatever the Cognita host can reach. On kei that is the whole LAN plus
        loopback: the admin API on 8676, the router, a cloud metadata endpoint.
        The only validation used to be the scheme, with follow_redirects=True, so
        an attacker-controlled page could 302 to http://127.0.0.1:8675/ and have
        the answer indexed into the corpus.

        Redirects are followed by hand so EVERY hop is re-validated; the
        automatic follower would check only the first. A hostname that resolves
        to a mix of public and private addresses is refused outright.

        Residual, and worth naming: a DNS entry that changes between this check
        and the connection (rebinding) is not closed by validation alone — that
        needs pinning the connection to the vetted IP. The window is small and
        the exposure is one fetch; the fix belongs with a custom transport.
        """
        seen: list[str] = []
        for _ in range(_MAX_REDIRECT_HOPS):
            await self._assert_public_host(url)
            seen.append(url)
            async with httpx.AsyncClient(timeout=30.0, follow_redirects=False) as client:
                async with client.stream(
                    "GET", url, headers={"User-Agent": "Mozilla/5.0 (cognita-ingester)"}
                ) as response:
                    if response.is_redirect:
                        location = response.headers.get("location", "")
                        if not location:
                            raise _UrlRefused("fetch_failed", "Redirect with no Location header.")
                        url = str(response.url.join(location))
                        if url in seen:
                            raise _UrlRefused("fetch_failed", "Redirect loop.")
                        continue
                    response.raise_for_status()
                    # Streamed with a ceiling: response.text buffers whatever
                    # arrives, so a multi-GB target was a memory DoS reachable by
                    # anyone who could call the tool.
                    chunks, total = [], 0
                    async for chunk in response.aiter_bytes():
                        total += len(chunk)
                        if total > MAX_CONTENT_BYTES:
                            raise _UrlRefused(
                                "too_large",
                                f"The response exceeds the {MAX_CONTENT_BYTES} byte limit. "
                                "Nothing was fetched or written.",
                            )
                        chunks.append(chunk)
                    encoding = response.encoding or "utf-8"
                    return b"".join(chunks).decode(encoding, errors="replace"), url
        raise _UrlRefused("fetch_failed", f"More than {_MAX_REDIRECT_HOPS} redirects.")

    @staticmethod
    async def _assert_public_host(url: str) -> None:
        """Refuse a URL whose host resolves anywhere but the public internet."""
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            raise _UrlRefused("invalid", "Only http:// and https:// URLs are supported")
        host = parsed.hostname
        if not host:
            raise _UrlRefused("invalid", "URL has no host.")
        try:
            infos = await asyncio.to_thread(
                socket.getaddrinfo, host, parsed.port or (443 if parsed.scheme == "https" else 80),
                0, socket.SOCK_STREAM,
            )
        except socket.gaierror as exc:
            raise _UrlRefused("fetch_failed", f"Could not resolve {host!r}: {exc.strerror}") from exc
        for info in infos:
            ip = ipaddress.ip_address(info[4][0])
            if not ip.is_global or ip.is_multicast:
                raise _UrlRefused(
                    "invalid",
                    f"{host!r} resolves to {ip}, which is not a public address. "
                    "add_from_url fetches from THIS SERVER, so a private, loopback or "
                    "link-local target would read something only the server can reach "
                    "and store it in the corpus. Nothing was fetched.",
                )

    def _copy_backup_keep(self) -> int:
        return int(getattr(self.config, "backup_keep_per_file", 0) or 0)

    def _resolve_dir(self, project: Project, prefix: str | None, field: str,
                     *, must_exist: bool) -> tuple[Path | None, dict | None]:
        """Resolve a directory argument, refusing escapes and the backups tree."""
        if not prefix:
            return None, {"status": "error", "reason": "invalid",
                          "message": f"{field} is required and cannot be empty."}
        target = resolve_target(project.documents_dir, prefix)
        if target is None:
            return None, {"status": "error", "reason": "invalid_path",
                          "message": f"{field} resolves outside this project: {prefix!r}"}
        docs = Path(project.documents_dir).resolve()
        rel = target.relative_to(docs)
        if rel.parts and rel.parts[0] == BACKUPS_DIRNAME:
            return None, {"status": "error", "reason": "invalid_path",
                          "message": (f"{field} points into {BACKUPS_DIRNAME}/, which is the "
                                      "recovery tree and is never indexed or written through "
                                      "the tool surface.")}
        if must_exist and not target.is_dir():
            return None, {"status": "error", "reason": "not_found",
                          "message": f"{field} is not a directory: {prefix!r}"}
        return target, None

    def _collect_dir_files(self, directory: Path, recursive: bool) -> list[Path]:
        """Every file directly in `directory` (or its whole subtree), sorted."""
        it = directory.rglob("*") if recursive else directory.iterdir()
        return sorted(path for path in it if path.is_file())

    def _classify_for_copy(self, project: Project, files: list[Path]) -> tuple[list[Path], list[dict]]:
        """Split a directory's files into (copyable, skipped-with-a-reason).

        Non-indexable extensions and sync-conflict copies are SKIPPED rather than
        failing the call — a stray .DS_Store must not block a pack port — but
        every skip is named in the result, because a copy that silently dropped
        a file would be the "looks complete and is not" failure with extra steps.
        """
        policy = self.core.policy_for(project.name)
        copyable: list[Path] = []
        skipped: list[dict] = []
        for path in files:
            if policy.tier_for(path.suffix) is None:
                skipped.append({"filepath": path.name, "reason": "not_indexable",
                                "detail": f"{path.suffix or '(no extension)'} is not an indexable extension"})
            elif is_sync_conflict(path.name, self.core.sync_conflict_patterns):
                skipped.append({"filepath": path.name, "reason": "sync_conflict",
                                "detail": "cloud-sync conflict copy; never indexed"})
            else:
                copyable.append(path)
        return copyable, skipped

    async def _category_for_copy(self, project: Project, src_rel: str, override: str | None) -> str | None:
        """Explicit argument, else the SOURCE document's stored category.

        None means "let indexing decide" (path mappings, then 'general'). A copy
        that landed in 'general' because nobody said otherwise would make the
        duplicated pack unenumerable by category, which is the one filter the raw
        surface has always had.
        """
        if override:
            return override
        stored = await self.store.get_document(project.name, src_rel)
        return stored.category if stored is not None else None

    async def _copy_one(self, project: Project, src: Path, dst: Path,
                        category: str | None) -> tuple[dict, Path | None]:
        """Copy one file, back up anything it replaces, index it. Raises on failure.

        Returns (per-file result, backup path or None) — the backup is what a
        rollback needs to put the destination back the way it was.
        """
        docs = Path(project.documents_dir)
        backup = None
        if dst.is_file():
            backup = backup_if_exists(docs, self._rel(project, dst.resolve()),
                                      keep=self._copy_backup_keep())
        dst.parent.mkdir(parents=True, exist_ok=True)
        # copy2, not read+write: the bytes never enter this process as text, so
        # the destination is byte-identical by construction — no encoding guess,
        # no newline normalization, no trailing-newline question.
        await asyncio.to_thread(shutil.copy2, src, dst)
        outcome = await self.core.index_file(
            project.name, docs, dst, category_override=category
        )
        chunks = outcome[1] if outcome else 0
        tier = self.core.policy_for(project.name).tier_for(dst.suffix)
        entry = {
            "filepath": self._rel(project, dst.resolve()),
            "source_filepath": self._rel(project, src.resolve()),
            "chunks_added": chunks,
            "tier": tier,
            "indexed": outcome is not None,
            # Keep the complete disk fact set.  COPY_ENTRY requires on_disk so
            # callers can verify the byte-identical destination without another
            # stat, and file_facts already reports that observed postcondition.
            **file_facts(dst),
        }
        if backup is not None:
            entry["overwrote_existing"] = True
            entry["previous_backup_id"] = backup_id_of(backup)
        return entry, backup

    async def _rollback_copies(self, project: Project, written: list[tuple[Path, Path | None]]) -> list[str]:
        """Undo the destinations this call already wrote, newest first.

        This is what makes copy_directory atomic against ERRORS: a failure on
        file 9 of 17 leaves the tree exactly as file 1 found it. It is NOT
        atomic against the process dying mid-call — nothing short of a staging
        directory plus a transactional index would be, and that guarantee is
        documented rather than pretended at.
        """
        docs = Path(project.documents_dir)
        undone: list[str] = []
        for dst, backup in reversed(written):
            rel = self._rel(project, dst.resolve()) if dst.exists() else None
            try:
                if backup is None:
                    if rel is not None:
                        await self.core.remove_file(project.name, rel)
                    dst.unlink(missing_ok=True)
                    self._prune_empty_parents(project, dst)
                else:
                    await asyncio.to_thread(shutil.copy2, backup, dst)
                    await self.core.index_file(project.name, docs, dst)
                undone.append(rel or dst.name)
            except Exception:  # a rollback must never mask the original failure
                log.exception("Rollback failed for %s", dst)
        return undone

    async def _copy_document(self, project: Project, args: dict) -> dict:
        src_fp = (args.get("src_filepath") or "").strip()
        dst_fp = (args.get("dst_filepath") or "").strip()
        overwrite = bool(args.get("overwrite", False))
        override = (args.get("category") or "").strip() or None
        if not src_fp or not dst_fp:
            return {"status": "error", "reason": "invalid",
                    "message": "src_filepath and dst_filepath are both required."}
        src = resolve_target(project.documents_dir, src_fp)
        dst = resolve_target(project.documents_dir, dst_fp)
        if src is None or dst is None:
            bad = src_fp if src is None else dst_fp
            return {"status": "error", "reason": "invalid_path",
                    "message": f"path resolves outside this project: {bad!r}"}
        if not src.is_file():
            return {"status": "error", "reason": "not_found",
                    "message": f"Source document not found: {src_fp}"}
        if src.resolve() == dst.resolve():
            return {"status": "error", "reason": "invalid",
                    "message": "src_filepath and dst_filepath are the same file."}
        for rejected in (self._reject_backups_write(project, dst),
                         self._reject_unindexable(project, dst),
                         self._reject_sync_conflict(dst)):
            if rejected is not None:
                return rejected
        if dst.exists() and not dst.is_file():
            return {"status": "error", "reason": "destination_not_a_file",
                    "message": (f"{dst_fp!r} exists and is not a regular file (a directory, "
                                "most likely). Replacing it with a document is never what a "
                                "copy means, so it is refused even with overwrite=true. "
                                "Nothing was copied."),
                    "conflicts": [self._rel(project, dst.resolve())]}
        if dst.exists() and not overwrite:
            return {
                "status": "error", "reason": "destination_exists",
                "message": (f"{dst_fp!r} already exists and overwrite is false. NOTHING was "
                            "copied. Pass overwrite=true to replace it (the current content "
                            "is backed up first), or choose another destination."),
                "conflicts": [self._rel(project, dst.resolve())],
            }
        try:
            size = src.stat().st_size
        except OSError as exc:
            return {"status": "error", "reason": "unreadable",
                    "message": f"Could not stat the source: {exc}"}
        if size > MAX_CONTENT_BYTES:
            return {"status": "error", "reason": "too_large",
                    "message": (f"Source is {size} bytes; the limit is {MAX_CONTENT_BYTES}. "
                                "Nothing was copied."),
                    "size_bytes": size, "limit_bytes": MAX_CONTENT_BYTES}
        category = await self._category_for_copy(
            project, self._rel(project, src.resolve()), override
        )
        # One critical section, and it rolls back — the same guarantee
        # copy_directory gives. _copy_one does shutil.copy2 and THEN index_file,
        # so an exception from the index step (Postgres briefly unavailable) left
        # the destination already overwritten while the caller was told the copy
        # failed and would reasonably assume dst was untouched.
        async with self.core.write_lock(project.name):
            existed = dst.exists()
            try:
                entry, backup = await self._copy_one(project, src, dst, category)
            except BackupError as exc:
                return {"status": "error", "reason": "backup_failed",
                        "message": f"Copy aborted: {wire_error(exc)}. No changes were made."}
            except Exception as exc:
                log.exception("copy_document failed at %s", dst)
                if not existed:
                    # Nothing was there before, so whatever landed is ours to
                    # remove — and removing it makes "the copy failed" true.
                    undone = await self._rollback_copies(project, [(dst, None)])
                    detail = ("The destination did not exist before this call and has been "
                              "removed again, so nothing was left behind.")
                else:
                    # It DID exist and _copy_one may have replaced it before
                    # failing. We cannot know from here whether its backup was
                    # taken, so say exactly that and point at the recovery path
                    # rather than claiming an atomicity this branch does not have.
                    undone = []
                    detail = ("The destination already existed and MAY have been replaced "
                              "before the failure. Check it, and use list_backups + "
                              "restore_backup on that path if it was.")
                return {
                    "status": "error", "reason": "copy_failed",
                    "message": f"Copy failed: {wire_error(exc)}. {detail}",
                    "filepath": self._rel(project, dst),
                    "rolled_back": undone,
                }
        return {"status": "success", "category": category or "(derived)", **entry}

    async def _copy_directory(self, project: Project, args: dict) -> dict:
        src_prefix = normalize_prefix(args.get("src_prefix"))
        dst_prefix = normalize_prefix(args.get("dst_prefix"))
        overwrite = bool(args.get("overwrite", False))
        recursive = bool(args.get("recursive", False))
        override = (args.get("category") or "").strip() or None

        src_dir, err = self._resolve_dir(project, src_prefix, "src_prefix", must_exist=True)
        if err is not None:
            return err
        dst_dir, err = self._resolve_dir(project, dst_prefix, "dst_prefix", must_exist=False)
        if err is not None:
            return err
        src_r, dst_r = src_dir.resolve(), dst_dir.resolve()
        if src_r == dst_r:
            return {"status": "error", "reason": "invalid",
                    "message": "src_prefix and dst_prefix are the same directory."}
        if recursive and (dst_r.is_relative_to(src_r) or src_r.is_relative_to(dst_r)):
            return {"status": "error", "reason": "invalid",
                    "message": ("A recursive copy cannot nest one directory inside the "
                                "other — it would copy its own output.")}

        files = self._collect_dir_files(src_dir, recursive)
        copyable, skipped = self._classify_for_copy(project, files)
        if not copyable:
            return {"status": "error", "reason": "empty",
                    "message": (f"No indexable documents in {src_prefix!r}"
                                + (" (non-recursive: pass recursive=true to include "
                                   "subdirectories)" if not recursive else "") + "."),
                    "skipped": skipped}
        if len(copyable) > MAX_COPY_FILES:
            return {"status": "error", "reason": "too_many_files",
                    "message": (f"{len(copyable)} files matched {src_prefix!r}; the per-call "
                                f"ceiling is {MAX_COPY_FILES}. Nothing was copied — narrow "
                                "the prefix."),
                    "file_count": len(copyable), "limit": MAX_COPY_FILES}

        pairs = [(f, dst_dir / f.relative_to(src_dir)) for f in copyable]
        # Pre-flight EVERY destination before writing ANY of them. Reporting the
        # first conflict and stopping would make the caller discover the rest one
        # round trip at a time; reporting them after a partial write would be the
        # half-copied directory this tool exists to prevent.
        # A destination that exists and is NOT a regular file (a directory of the
        # same name) is refused whatever `overwrite` says: shutil.copy2 would
        # copy INTO it, the parse would then fail on a directory, and the call
        # would take the rollback path for something that was never a legitimate
        # overwrite in the first place.
        not_files = [self._rel(project, dst.resolve())
                     for _, dst in pairs if dst.exists() and not dst.is_file()]
        if not_files:
            return {"status": "error", "reason": "destination_not_a_file",
                    "message": (f"{len(not_files)} destination path(s) exist and are not "
                                "regular files (directories, most likely). Nothing was "
                                "copied — the destination is untouched."),
                    "conflicts": not_files}
        conflicts = [self._rel(project, dst.resolve()) if dst.exists() else None
                     for _, dst in pairs]
        conflicts = [c for c in conflicts if c]
        if conflicts and not overwrite:
            return {
                "status": "error", "reason": "destination_exists",
                "message": (f"{len(conflicts)} destination file(s) already exist and overwrite "
                            "is false. NOTHING was copied — the destination is untouched. "
                            "Pass overwrite=true to replace them (each is backed up first), "
                            "or choose an empty destination."),
                "conflicts": conflicts,
                "file_count": len(pairs),
            }

        written: list[tuple[Path, Path | None]] = []
        copied: list[dict] = []
        # Keep writes and rollback under one project lock so the watcher cannot
        # reindex a destination between de-indexing and unlinking it. One bulk GPU
        # job covers all source files; per-file jobs can skip useful small work or
        # pay repeated startup cost (about 3 seconds plus a canary).
        async with self.core.write_lock(project.name), self.core.bulk_gpu_job(
            project.name, "copy_directory",
            [src for src, _ in pairs], Path(project.documents_dir),
        ):
            for src_file, dst_file in pairs:
                category = await self._category_for_copy(
                    project, self._rel(project, src_file.resolve()), override
                )
                try:
                    entry, backup = await self._copy_one(project, src_file, dst_file, category)
                except Exception as exc:
                    undone = await self._rollback_copies(project, written)
                    log.exception("copy_directory failed at %s; rolled back %d file(s)",
                                  dst_file, len(undone))
                    return {
                        "status": "error", "reason": "copy_failed",
                        "message": (f"Failed copying {src_file.name}: {type(exc).__name__}: "
                                    f"{exc}. The {len(undone)} file(s) already written were "
                                    "rolled back, so the destination is as it was before "
                                    "the call."),
                        "failed_at": self._rel(project, dst_file),
                        "rolled_back": undone,
                    }
                written.append((dst_file, backup))
                copied.append(entry)

        return {
            "status": "success",
            "src_prefix": src_prefix,
            "dst_prefix": dst_prefix,
            "recursive": recursive,
            "files_copied": len(copied),
            "chunks_added": sum(e["chunks_added"] for e in copied),
            "overwrote": sum(1 for e in copied if e.get("overwrote_existing")),
            # The full destination list WITH hashes: the caller can verify the
            # port without a second enumeration, which is the whole point of
            # doing this as one call.
            "destination_paths": [e["filepath"] for e in copied],
            "documents": copied,
            "result_key": "documents",
            "skipped": skipped,
        }

    async def _remove_directory(self, project: Project, args: dict) -> dict:
        prefix = normalize_prefix(args.get("prefix"))
        delete_files = bool(args.get("delete_files", False))
        recursive = bool(args.get("recursive", False))
        directory, err = self._resolve_dir(project, prefix, "prefix", must_exist=True)
        if err is not None:
            return err
        docs = Path(project.documents_dir).resolve()
        if directory.resolve() == docs:
            return {"status": "error", "reason": "invalid",
                    "message": ("prefix is the documents root. Removing the entire knowledge "
                                "base is not something this tool will do.")}

        on_disk = self._collect_dir_files(directory, recursive)
        if not recursive:
            nested = sorted({
                str(path.parent.relative_to(directory).parts[0])
                for path in directory.rglob("*")
                if path.is_file() and path.parent != directory
            })
            if nested:
                return {"status": "error", "reason": "has_subdirectories",
                        "message": (f"{prefix!r} has subdirectories holding files and "
                                    "recursive is false. Nothing was removed. Pass "
                                    "recursive=true to include them."),
                        "subdirectories": nested}
        if on_disk and not delete_files:
            return {
                "status": "error", "reason": "not_empty",
                "message": (f"{prefix!r} still holds {len(on_disk)} file(s) on disk and "
                            "delete_files is false. NOTHING was removed — de-indexing them "
                            "would leave files on disk that no search can reach. Pass "
                            "delete_files=true to back each one up and delete it."),
                "files": [self._rel(project, f.resolve()) for f in on_disk][:50],
                "file_count": len(on_disk),
            }

        # Resolve the directory before deriving its index prefix. Matching the
        # caller's raw path can delete files while leaving rows for paths spelled
        # with unresolved `.` segments.
        dir_prefix = self._rel(project, directory.resolve()) + "/"
        removed, chunks_removed, backups, failures = 0, 0, [], []
        # Keep deletion and de-indexing under one lock so the watcher cannot
        # reindex files between those steps and contradict the returned counts.
        async with self.core.write_lock(project.name):
            # Delete first, then de-index only files that were actually removed;
            # a backup failure must leave the live file searchable.
            deleted_rels: set[str] = set()
            for path in on_disk:
                try:
                    rel = self._rel(project, path.resolve())
                except ValueError as exc:
                    # A symlink inside the directory pointing outside the project:
                    # _collect_dir_files filters on is_file(), which follows links.
                    # This used to escape the loop entirely and surface as
                    # internal_error, so the caller learned neither which files had
                    # been deleted nor where their backups were.
                    failures.append({"filepath": path.name, "error": wire_error(exc)})
                    continue
                facts = await asyncio.to_thread(file_facts, path, hash_text=False)
                try:
                    made = await asyncio.to_thread(
                        backup_if_exists, docs, rel, keep=self._copy_backup_keep()
                    )
                    if made is None or backup_id_of(made) is None:
                        raise BackupError("No backup receipt was produced for the existing source")
                except BackupError as exc:
                    # Same posture as every other destructive write: no backup, no
                    # delete. A partially-deleted directory with no recovery path is
                    # the one outcome worse than refusing outright.
                    failures.append({"filepath": rel, "error": wire_error(exc)})
                    continue
                try:
                    await asyncio.to_thread(path.unlink)
                except OSError as exc:
                    failures.append({"filepath": rel, "error": wire_error(exc)})
                    continue
                deleted_rels.add(rel)
                if made is not None:
                    backups.append({"filepath": rel, "backup_id": backup_id_of(made),
                                    "deleted_mtime": facts.get("mtime"),
                                    "deleted_bytes_sha256": facts.get("bytes_sha256")})
            indexed = [d for d in await self.store.list_documents(project.name)
                       if d.source.startswith(dir_prefix)
                       and (recursive or "/" not in d.source[len(dir_prefix):])]
            for doc in indexed:
                # Keep the row for any file still on disk — a backup failure above
                # means that document was NOT removed, and saying otherwise would
                # make the count a lie in the direction that loses data.
                if doc.source not in deleted_rels and (docs / doc.source).exists():
                    continue
                chunks_removed += await self.store.chunk_count(project.name, doc.source)
                if await self.core.remove_file(project.name, doc.source):
                    removed += 1
            pruned = self._prune_empty_parents(project, directory / "_")
        payload = {
            "status": "success" if not failures else "partial",
            "prefix": prefix,
            "recursive": recursive,
            "documents_removed": removed,
            "chunks_removed": chunks_removed,
            "files_deleted": len(on_disk) - len(failures),
            "backups": backups,
            "result_key": "backups",
            # backup_ids are per-SECOND timestamps, so the files of one bulk call
            # normally share a single id — that is what makes the operation
            # enumerable as a set, and it is NOT a collision. A call that crosses
            # a second boundary produces two; feed every id here to list_backups.
            "backup_ids": sorted({b["backup_id"] for b in backups if b["backup_id"]}),
            "pruned_directories": pruned,
        }
        if failures:
            payload["failures"] = failures
            payload["message"] = (
                f"{len(failures)} file(s) could not be backed up or deleted and were LEFT "
                "IN PLACE; everything else was removed. Retry, or delete them manually."
            )
        elif backups:
            payload["restore_hint"] = (
                "Every deleted file was backed up first. The backups array names this "
                "operation's (filepath, backup_id) receipts; list_backups with this prefix "
                "also includes earlier operations. Compare the before and after "
                "(filepath, backup_id) sets to identify this operation's new receipts, "
                "and restore_backup puts any of them back. Preserve historical backups."
            )
        return payload
