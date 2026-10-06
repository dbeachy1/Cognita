"""Reads operations inherited by LocalEngineHost.

These methods use the host's existing project, config, core, store, connector, and
operation state; this class adds no fields or lifecycle behavior.
"""
from __future__ import annotations

import asyncio
import base64
import json
import time
from pathlib import Path
from typing import Any
from .backups import (
    BACKUPS_DIRNAME,
    resolve_target,
)
from .byte_facts import (
    classify_text_bytes,
)
from .literals import (
    MAX_CONTEXT_LINES,
    MAX_MATCHES_CEILING,
    MAX_MATCHES_DEFAULT,
    SKIPPED_LISTED,
    BadPattern,
    build_matcher,
    glob_matches,
    scan_text,
)
from .manifest import file_facts, stat_drift
from .parsing import (
    SYNC_CONFLICT_PATTERNS,
    TIER_REGISTERED,
    detect_category,
    parse_file,
)
from .registry import Project
from .engine_contract import LITERAL_WALK_BUDGET_S, MAX_PLURAL_PATHS, MAX_RESULTS, PLURAL_BODY_MAX_BYTES, PLURAL_BODY_TOTAL_MAX_BYTES, _collection, _empty_selection_message, make_snippet, normalize_prefix

RETRIEVAL_PROFILES = frozenset({"editing", "canon", "instructions", "workflow"})


class EngineReadOperations:
    async def _book_admitted_doc_pairs(
        self, project: Project, retrieval_profile: str | None = None,
    ) -> set[tuple[str, str]] | None:
        policy = self.core.effective_index_policy_for(project.name)
        if policy is None or getattr(policy, "layout", None) is None:
            return None
        sources, doc_ids, _metadata = await self.core._effective_indexed_sources(
            project.name, retrieval_profile,
        )
        return set(zip(sources or (), doc_ids or ()))

    @staticmethod
    def _retrieval_profile_error(value: Any) -> dict | None:
        if value is None or (isinstance(value, str) and value in RETRIEVAL_PROFILES):
            return None
        return {"status": "error", "reason": "invalid",
                "message": "retrieval_profile must be editing, canon, instructions, or workflow."}

    def _indexed_source_predicate(self, project: Project):
        policy = self.core.effective_index_policy_for(project.name)
        if policy is None:
            return lambda _source: True
        extension_policy = self.core.policy_for(project.name)

        def is_indexed(source: str) -> bool:
            return policy.decision(
                source,
                globally_eligible=extension_policy.tier_for(Path(source).suffix) is not None,
            ).indexed

        return is_indexed

    async def _search_knowledge(self, project: Project, args: dict) -> dict:
        profile_error = self._retrieval_profile_error(args.get("retrieval_profile"))
        if profile_error is not None:
            return profile_error
        query = (args.get("query") or "").strip()
        if not query:
            return {"status": "error", "reason": "invalid", "message": "Query cannot be empty"}
        max_results = max(1, min(int(args.get("max_results") or 5), MAX_RESULTS))
        raw_alpha = args.get("hybrid_alpha")
        hybrid_alpha = max(0.0, min(float(0.3 if raw_alpha is None else raw_alpha), 1.0))
        raw_min = args.get("min_score")
        min_score = max(0.0, min(float(0.0 if raw_min is None else raw_min), 1.0))
        snippet_mode = bool(args.get("snippet_mode", True))
        category = args.get("category") or None

        # No category validation by design. A category is just document metadata that
        # add_document accepts freely, so the only authoritative set is the one in the
        # store — the same source list_categories/get_index_stats read. An unknown
        # category matches nothing and falls through to no_results below, which is the
        # forgiving contract for a search endpoint and keeps this path from drifting
        # away from the other read tools again.
        results = await self.core.search(
            project.name, query, max_results=max_results,
            category=category, hybrid_alpha=hybrid_alpha,
            retrieval_profile=args.get("retrieval_profile"),
        )
        for r in results:
            # 5.0 §5.1: keep BOTH. `source` stays the absolute host path 3.x
            # emitted (wire compat); `filepath` is the relative path every tool
            # ACCEPTS, so a hit can be handed straight to get_document,
            # read_document or edit_document without stripping the host's
            # absolute documents-root prefix from the source path.
            r["filepath"] = r["source"]
            r["source"] = self._abs(project, r["source"])  # 3.x emitted absolute paths
        if not results:
            message = "No relevant documents found."
            if category:
                known = await self.store.category_counts(project.name)
                if category not in known:
                    known_list = ", ".join(sorted(known)) or "none"
                    message = (f"No relevant documents found. Category '{category}' is not "
                               f"present in the index. Categories in the index: {known_list}")
            return _collection(
                {"status": "no_results", "query": query, "message": message,
                 "results": []},
                "results", alias=False,
            )
        total_before = len(results)
        if min_score > 0.0:
            results = [r for r in results if r.get("score", 0) >= min_score]
        if snippet_mode:
            for r in results:
                full_len = len(r.get("content", ""))
                r["content"] = make_snippet(r["content"])
                r["content_length"] = full_len
        return _collection({
            "status": "success",
            "query": query,
            "hybrid_alpha": hybrid_alpha,
            "result_count": len(results),
            "filtered_by_score": total_before - len(results),
            "cache_hit_rate": self.core.query_cache(project.name).stats()["hit_rate"],
            "results": results,
        }, "results", alias=False)

    async def _get_document(self, project: Project, args: dict) -> dict:
        filepath = args.get("filepath") or ""
        target = resolve_target(project.documents_dir, filepath) if filepath else None
        if target is None or not target.is_file():
            return {"status": "error", "reason": "not_found", "message": f"Document not found: {filepath}"}
        docs_dir = Path(project.documents_dir)
        policy = self.core.policy_for(project.name)
        try:
            doc = await asyncio.to_thread(parse_file, target, docs_dir, policy=policy)
        except ValueError as exc:  # unsupported format
            return {"status": "error", "reason": "unsupported_format",
                    "message": str(exc)}
        if doc is None:
            # 5.6.2: the file IS there. parse_file returns None when a document
            # extracts to no indexable text — a .md that is only YAML
            # frontmatter, or a whitespace-only file. Answering "not found" about
            # a file that exists on disk is the same class of untruth as the
            # reformatted read this release fixed: the caller is told the wrong
            # thing about the bytes. It is also self-defeating, because
            # read_document serves this file perfectly, so a caller that believes
            # not_found stops one call short of the content it asked for.
            #
            # add_document refuses to CREATE such a file (5.1, parse_failed +
            # rollback), so the way one appears is the second writer this project
            # already knows about: cloud sync landing a file Cognita never
            # indexed. That is exactly when a truthful answer matters.
            facts = await asyncio.to_thread(file_facts, target)
            return {
                "status": "error", "reason": "no_indexable_content",
                "message": (
                    f"'{filepath}' exists on disk ({facts.get('size_bytes')} bytes) but "
                    "holds no indexable text — it is empty, whitespace-only, or (for a "
                    ".md) nothing but YAML frontmatter. It is NOT in the index, which is "
                    "why the knowledge-base tools cannot see it."
                ),
                "filepath": filepath,
                "size_bytes": facts.get("size_bytes"),
                "bytes_sha256": facts.get("bytes_sha256"),
                "mtime": facts.get("mtime"),
                "hint": (
                    "read_document returns this file's bytes verbatim — use it to see the "
                    "content. To get it indexed, give it body text below the frontmatter."
                ),
            }
        stored = await self.store.get_document(project.name, doc.source)
        chunk_count = await self.store.chunk_count(project.name, doc.source)
        tier = stored.tier if stored else doc.tier
        registered = tier == TIER_REGISTERED
        # 5.0 §4: the natural place to verify a single write. Hashed from the
        # bytes just read off disk, so it is directly comparable to the manifest's
        # and is what expected_sha256 wants on the write that follows.
        facts = await asyncio.to_thread(file_facts, target)
        # 5.6: the FILE, not the indexed extraction. `doc` is still parsed above
        # for its metadata (tier, category, keywords, chunk count) — that is
        # index state and is exactly what those fields mean. `content` is the
        # document, and the document is what is on disk.
        raw_for_facts = await asyncio.to_thread(target.read_bytes)
        readable_facts = classify_text_bytes(raw_for_facts)
        encoding = args.get("content_encoding", "utf-8")
        if encoding not in ("utf-8", "base64"):
            return {"status": "error", "reason": "invalid",
                    "message": "content_encoding must be 'utf-8' or 'base64'."}
        verbatim = (base64.b64encode(raw_for_facts).decode("ascii")
                    if encoding == "base64" else
                    (readable_facts.text if readable_facts.accepted else None))
        extracted = encoding == "utf-8" and verbatim is None
        return {"status": "success", "document": {
            "content": doc.content if extracted else verbatim,
            # Only ever true for a binary format that has no text on disk to
            # return. Never silently: a caller byte-comparing what it pushed is
            # told outright when it is looking at extracted text instead.
            "content_is_extracted": extracted,
            "content_encoding": encoding,
            "content_note": (
                f"'{doc.format}' is a binary format with no text on disk, so `content` is "
                "the extracted text used for indexing, NOT the stored bytes. "
                "Compare bytes_sha256 against the file, never this."
                if extracted else
                "`content` is standard base64 for the exact original file bytes; decode it "
                "before comparing with bytes_sha256. No extraction or lossy text decoding "
                "was applied."
                if encoding == "base64" else
                "`content` is a readable UTF-8 view containing U+FFFD replacements for "
                "malformed source bytes, so it does NOT hash to bytes_sha256. Use "
                "content_encoding=base64 for the exact original bytes."
                if readable_facts.content_is_lossy else
                "`content` is the file's bytes, decoded as UTF-8 and otherwise untouched — "
                "line endings, BOM, leading and trailing whitespace all as stored. "
                "sha256 of it equals bytes_sha256. `content_sha256` is the "
                "newline-normalized write-guard hash that expected_sha256 wants."
            ),
            "source": str(target),
            "filepath": doc.source,
            "filename": target.name,
            "category": stored.category if stored else doc.category,
            "format": doc.format,
            "content_sha256": facts.get("content_sha256"),
            "bytes_sha256": facts.get("bytes_sha256"),
            "size_bytes": facts.get("size_bytes"),
            "utf8_valid": None if not readable_facts.accepted else readable_facts.utf8_valid,
            "decode_error_bytes": None if not readable_facts.accepted else readable_facts.decode_error_bytes,
            "content_is_lossy": (
                False if encoding == "base64" or not readable_facts.accepted
                else readable_facts.content_is_lossy
            ),
            "index_text_sanitized": (
                False if not readable_facts.accepted else readable_facts.index_text_sanitized
            ),
            "line_endings": None if not readable_facts.accepted else readable_facts.line_endings,
            "mtime": facts.get("mtime"),
            "indexed_sha256": stored.content_hash if stored else None,
            # 6.1.1: `doc` is this file re-parsed a few lines up, so its
            # content_hash is the extraction of the CURRENT bytes and settles
            # drift outright — no stat comparison, and no false positive when a
            # sync client rewrites the mtime of a file it did not change.
            "index_drift": (
                stat_drift(facts, stored.file_size, stored.file_mtime,
                           indexed_hash=stored.content_hash,
                           extracted_hash=doc.content_hash)
                if stored is not None else None
            ),
            "metadata": {
                "type": doc.format.lstrip("."),
                "file_size": doc.file_size,
                "modified": doc.file_mtime.isoformat() if doc.file_mtime else None,
            },
            "keywords": stored.keywords if stored else doc.keywords,
            # Registered documents are stored whole, so 0 is the truth, not a
            # gap — never report the would-be chunk count for them.
            "chunk_count": 0 if registered else (
                chunk_count or len(doc.chunks(self.core.chunk_size,
                                              self.core.chunk_overlap))
            ),
            "tier": tier,
            "semantic_searchable": not registered,
        }}

    async def _get_documents(self, project: Project, args: dict) -> dict:
        """Read an ordered list without changing the single-file read contract.

        Validation is deliberately complete before the first disk read. Once the
        list is valid, each path is independent: an error is an entry, not a
        reason to suppress later paths. Facts-only reads avoid parsing/indexing;
        body reads delegate to ``_get_document`` so the shared TextView and
        extracted-format behavior remain authoritative.
        """
        filepaths = args.get("filepaths")
        if not isinstance(filepaths, list) or not filepaths:
            return {"status": "error", "reason": "invalid",
                    "message": "filepaths must be a non-empty list"}
        if len(filepaths) > MAX_PLURAL_PATHS:
            return {"status": "error", "reason": "invalid",
                    "message": f"filepaths exceeds the {MAX_PLURAL_PATHS}-path limit"}
        include_content = args.get("include_content", True)
        encoding = args.get("content_encoding", "utf-8")
        if encoding not in ("utf-8", "base64"):
            return {"status": "error", "reason": "invalid",
                    "message": "content_encoding must be 'utf-8' or 'base64'."}

        normalized: list[tuple[str, Path]] = []
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
            canonical = target.resolve()
            if canonical in seen:
                return {"status": "error", "reason": "duplicate_path",
                        "message": f"{raw!r} appears more than once in filepaths"}
            seen.add(canonical)
            normalized.append((raw, target))

        entries: list[dict] = []
        succeeded = failed = 0
        body_bytes = 0
        for index, (filepath, target) in enumerate(normalized):
            try:
                before = target.stat()
                before_signature = (before.st_size, before.st_mtime_ns, before.st_ctime_ns)
            except OSError:
                before_signature = None
            if not include_content:
                if not target.is_file():
                    item = {"index": index, "filepath": filepath, "status": "error",
                            "reason": "not_found",
                            "error": {"status": "error", "reason": "not_found",
                                      "message": f"Document not found: {filepath}",
                                      "on_disk": False}}
                    entries.append(item)
                    failed += 1
                    continue
                facts = await asyncio.to_thread(file_facts, target)
                try:
                    after = target.stat()
                    after_signature = (after.st_size, after.st_mtime_ns, after.st_ctime_ns)
                except OSError:
                    after_signature = None
                if before_signature is None or after_signature != before_signature:
                    entries.append({
                        "index": index, "filepath": filepath, "status": "error",
                        "reason": "file_changed_during_read",
                        "error": {"status": "error", "reason": "file_changed_during_read",
                                  "message": "The file changed while its facts were read; retry."},
                    })
                    failed += 1
                    continue
                if facts.get("error"):
                    entries.append({
                        "index": index, "filepath": filepath, "status": "error",
                        "reason": "unreadable",
                        "error": {"status": "error", "reason": "unreadable",
                                  "message": "The document could not be read.",
                                  "on_disk": facts.get("on_disk", False)},
                    })
                    failed += 1
                    continue
                try:
                    rel = self._rel(project, target.resolve())
                except ValueError:
                    rel = filepath
                stored = await self.store.get_document(project.name, rel)
                # Facts-only reads deliberately avoid parsing the body. The
                # index owns authoritative metadata when a file is indexed;
                # for an unindexed supported path derive the stable tier and
                # category labels and report zero indexed chunks.
                policy = self.core.policy_for(project.name)
                tier = stored.tier if stored is not None else policy.tier_for(target.suffix)
                category = (
                    stored.category if stored is not None else
                    detect_category(rel, self.core.category_mappings)
                )
                chunk_count = (
                    await self.store.chunk_count(project.name, rel)
                    if stored is not None else 0
                )
                document = {"filepath": rel, "source": str(target),
                            "indexed": stored is not None, "include_content": False,
                            "category": category, "chunk_count": chunk_count,
                            "tier": tier, **facts}
                if stored is not None:
                    document["indexed_sha256"] = stored.content_hash
                    document["index_drift"] = stat_drift(
                        facts, stored.file_size, stored.file_mtime,
                        indexed_hash=stored.content_hash,
                    )
                entries.append({"index": index, "filepath": filepath,
                                "status": "success", "document": document})
                succeeded += 1
                continue

            if before_signature is not None and before_signature[0] > PLURAL_BODY_MAX_BYTES:
                facts = await asyncio.to_thread(file_facts, target)
                try:
                    after = target.stat()
                    after_signature = (after.st_size, after.st_mtime_ns, after.st_ctime_ns)
                except OSError:
                    after_signature = None
                if after_signature != before_signature:
                    entries.append({
                        "index": index, "filepath": filepath, "status": "error",
                        "reason": "file_changed_during_read",
                        "error": {"status": "error", "reason": "file_changed_during_read",
                                  "message": "The file changed while its facts were read; retry."},
                    })
                    failed += 1
                    continue
                error = {"status": "error", "reason": "content_too_large",
                         "message": (
                             "This plural read omits bodies over 1 MiB per file; use "
                             "get_document or read_document for the full content.")}
                entries.append({"index": index, "filepath": filepath, "status": "error",
                                "reason": "content_too_large", "document": facts,
                                "error": error})
                failed += 1
                continue
            single = await self._get_document(
                project, {"filepath": filepath, "content_encoding": encoding}
            )
            try:
                after = target.stat()
                after_signature = (after.st_size, after.st_mtime_ns, after.st_ctime_ns)
            except OSError:
                after_signature = None
            if after_signature != before_signature:
                entries.append({
                    "index": index, "filepath": filepath, "status": "error",
                    "reason": "file_changed_during_read",
                    "error": {"status": "error", "reason": "file_changed_during_read",
                              "message": "The file changed while its content was read; retry."},
                })
                failed += 1
                continue
            if single.get("status") != "success":
                if single.get("reason") == "not_found":
                    single = {**single, "on_disk": False}
                entries.append({"index": index, "filepath": filepath, "status": "error",
                                "reason": single.get("reason", "error"),
                                "error": single})
                failed += 1
                continue
            document = single.get("document") or {}
            raw_size = document.get("size_bytes")
            encoded = document.get("content")
            encoded_size = len(encoded.encode("utf-8")) if isinstance(encoded, str) else 0
            if (isinstance(raw_size, int) and raw_size > PLURAL_BODY_MAX_BYTES
                    or body_bytes + encoded_size > PLURAL_BODY_TOTAL_MAX_BYTES):
                facts = {key: value for key, value in document.items() if key != "content"}
                error = {"status": "error", "reason": "content_too_large",
                         "message": (
                             "This plural read omits bodies over 1 MiB per file or "
                             "8 MiB total; use get_document or read_document for the "
                             "full content.")}
                entries.append({"index": index, "filepath": filepath, "status": "error",
                                "reason": "content_too_large",
                                "document": facts, "error": error})
                failed += 1
                continue
            body_bytes += encoded_size
            entries.append({"index": index, "filepath": filepath, "status": "success",
                            "document": document})
            succeeded += 1

        status = "success" if failed == 0 else "partial_failure"
        return _collection({"status": status, "result_key": "documents",
                            "documents": entries, "succeeded": succeeded,
                            "failed": failed, "skipped": 0}, "documents", alias=False)

    async def _search_similar(self, project: Project, args: dict) -> dict:
        profile_error = self._retrieval_profile_error(args.get("retrieval_profile"))
        if profile_error is not None:
            return profile_error
        retrieval_profile = args.get("retrieval_profile")
        filepath = args.get("filepath") or ""
        if not filepath:
            return {"status": "error", "reason": "invalid", "message": "Filepath required"}
        max_results = max(1, min(int(args.get("max_results") or 5), MAX_RESULTS))
        target = resolve_target(project.documents_dir, filepath)
        no_results = _collection(
            {"status": "no_results",
             "message": "No similar documents found or document not indexed",
             "similar_documents": []},
            "similar_documents", alias=True,
        )
        if target is None:
            return no_results
        try:
            rel = self._rel(project, target.resolve())
        except ValueError:
            return no_results
        is_indexed = self._indexed_source_predicate(project)
        if not is_indexed(rel):
            return no_results
        # A registered document has no embedding, so it can be neither a result
        # nor a reference. Say that specifically — "not found" would be actively
        # misleading when list_documents just showed the file (D4.4-6).
        stored = await self.store.get_document(project.name, rel)
        admitted_pairs = await self._book_admitted_doc_pairs(project, retrieval_profile)
        if (admitted_pairs is not None and
                (rel, stored.doc_id if stored is not None else "") not in admitted_pairs):
            return no_results
        if stored is not None and stored.is_registered:
            return {
                "status": "error",
                "reason": "registered_document",
                "message": (
                    f"'{filepath}' is a registered document ({stored.format}): it is "
                    "indexed and readable, but stored without an embedding, so "
                    "similarity comparison is not possible. It IS keyword-searchable "
                    "— use search_knowledge with a literal string from its content, "
                    "or get_document to read it in full."
                ),
                "filepath": filepath,
                "tier": stored.tier,
            }
        embedding = await self.store.first_chunk_embedding(project.name, rel)
        if embedding is None:
            return no_results
        # Exclude the reference and collapse to one hit per document IN SQL, so
        # `max_results` counts documents. It used to fetch max_results+20 CHUNKS
        # and filter afterwards: the reference's own chunks sit at distance 0, so
        # a document with 25+ chunks filled the entire budget with rows that were
        # all discarded and the tool reported "No similar documents found".
        admitted_sources, admitted_doc_ids, admission_metadata = await self.core._effective_indexed_sources(
            project.name, retrieval_profile,
        )
        admission_kwargs = (
            {"include_sources": admitted_sources, "include_doc_ids": admitted_doc_ids}
            if self.core.effective_index_policy_for(project.name) is not None else {}
        )
        hits = await self.store.dense_search(
            project.name, embedding, max_results,
            exclude_source=rel, one_per_source=True,
            **admission_kwargs,
        )
        seen: set[str] = set()
        similar = []
        is_indexed = self._indexed_source_predicate(project)
        for hit in hits:
            if hit.source == rel or hit.source in seen:
                continue
            if not is_indexed(hit.source):
                continue
            seen.add(hit.source)
            similarity = round(max(0.0, 1.0 - hit.score), 4)  # hit.score = cosine distance
            similar.append({
                "source": self._abs(project, hit.source),
                # 5.0 §5.1: the RELATIVE path, which is the form every tool
                # accepts as input. Before this a search result could not be fed
                # into get_document without string surgery against a host path.
                "filepath": hit.source,
                "filename": hit.source.rsplit("/", 1)[-1],
                "category": hit.category,
                "similarity": similarity,
                # 5.0 §5.2: the same number under the name search_knowledge
                # uses. Every entry read as `score` used to come back null, so
                # there was no way to tell a strong neighbor from a weak one or
                # to threshold at all — on a tool whose entire output is a
                # ranking.
                "score": similarity,
                "preview": hit.content[:200],
                "_doc_id": hit.doc_id,
            })
            admission = admission_metadata.get(hit.doc_id)
            if admission is not None:
                similar[-1].update({
                    "book_role": admission["role"],
                    "chapter_id": admission["chapter_id"],
                    "editorial_status": admission["editorial_status"],
                    "summary_freshness": admission["summary_freshness"],
                })
            if len(similar) >= max_results:
                break
        current_pairs = await self._book_admitted_doc_pairs(project, retrieval_profile)
        _sources, _doc_ids, current_metadata = await self.core._effective_indexed_sources(
            project.name, retrieval_profile,
        )
        if current_pairs is not None:
            similar = [
                item for item in similar
                if (item["filepath"], item.get("_doc_id", "")) in current_pairs
            ]
        for item in similar:
            admission = current_metadata.get(item.get("_doc_id", ""))
            if admission is not None:
                item.update({
                    "book_role": admission["role"],
                    "chapter_id": admission["chapter_id"],
                    "editorial_status": admission["editorial_status"],
                    "summary_freshness": admission["summary_freshness"],
                })
            item.pop("_doc_id", None)
        if not similar:
            return no_results
        return _collection(
            {"status": "success", "reference": filepath,
             "count": len(similar), "similar_documents": similar},
            "similar_documents", alias=True,
        )

    async def _list_documents(self, project: Project, args: dict) -> dict:
        category = args.get("category") or None
        prefix = normalize_prefix(args.get("prefix"))
        include_hashes = bool(args.get("include_hashes", False))
        docs = await self.store.list_documents(project.name)
        counts = await self.store.chunk_counts(project.name)
        is_indexed = self._indexed_source_predicate(project)
        admitted_pairs = await self._book_admitted_doc_pairs(project)
        selected = [
            d for d in docs
            if (not category or d.category == category)
            and (prefix is None or d.source.startswith(prefix))
            and is_indexed(d.source)
            and (admitted_pairs is None or (d.source, d.doc_id) in admitted_pairs)
        ]
        entries = [
            {
                "id": d.doc_id,
                "source": self._abs(project, d.source),
                # 5.0: the RELATIVE path alongside the absolute one. Every write
                # tool takes a relative path and every listing returned only an
                # absolute one, so a client syncing a directory had to reverse the
                # join itself — and get it right on Windows.
                "filepath": d.source,
                "category": d.category,
                "format": d.format or "",
                "chunks": counts.get(d.doc_id, 0),
                "keywords": d.keywords[:5],
                # Registered documents MUST appear here (D4.4-4): the whole
                # value of the tier is get_document on a known path, and a path
                # cannot be known if it never shows up in a listing. The markers
                # say which files it is pointless to search semantically.
                "tier": d.tier,
                "semantic_searchable": not d.is_registered,
            }
            for d in selected
        ]
        payload = {"status": "success", "filter": category or "all",
                   "prefix": prefix or "", "count": len(entries), "documents": entries}
        # 5.1: say WHICH kind of zero this is. A typo'd prefix and a genuinely
        # empty directory returned the identical `count: 0, documents: []`, which
        # is the same ambiguity find_literal builds a whole no_documents_selected
        # branch to avoid — and which 5.0 §6 records as having "turned correct
        # glob behavior into a confidently reported bug". A filtered zero over a
        # non-empty corpus is nearly always a wrong filter, and the caller is the
        # only one who can tell.
        if not entries and (category or prefix):
            filters = []
            if prefix:
                filters.append(f"prefix={prefix!r}")
            if category:
                filters.append(f"category={category!r}")
            payload["corpus_size"] = len(docs)
            payload["message"] = (
                f"NO DOCUMENTS MATCHED: {' and '.join(filters)} selected 0 of this "
                f"project's {len(docs)} indexed documents. The corpus is not empty — "
                "this is a filter that matched nothing, which is usually a typo or a "
                "prefix with the wrong case or a missing/extra trailing slash. "
                "Re-run without the filter to see the paths that exist."
            )
            if category:
                payload["available_categories"] = sorted({d.category for d in docs})
        if include_hashes:
            # 5.0 §4: the manifest. Read from the FILES, in one thread hop, so
            # the answer describes the disk rather than the index — a hash served
            # out of index state cannot detect the one failure it exists to
            # detect, which is the two disagreeing.
            docs_dir = Path(project.documents_dir)
            facts = await asyncio.to_thread(
                lambda: [file_facts(docs_dir / d.source) for d in selected]
            )
            drift = 0
            missing = 0
            for entry, doc, fact in zip(entries, selected, facts):
                entry.update(fact)
                entry["indexed_sha256"] = doc.content_hash
                # 6.1.1: the manifest hashes every file anyway, so hand the
                # indexed hash in and let agreement with the disk clear a stat
                # that moved without the content moving with it.
                entry["index_drift"] = stat_drift(fact, doc.file_size, doc.file_mtime,
                                                  indexed_hash=doc.content_hash)
                drift += entry["index_drift"]
                missing += not fact.get("on_disk")
            payload["hashes"] = "on_disk"
            payload["drift_count"] = drift
            payload["missing_on_disk_count"] = missing
            if drift:
                # Say what to DO about it in the payload itself: the model reading
                # this cannot see the code, and "index_drift: true" with no next
                # step reads as noise and gets ignored.
                payload["drift_hint"] = (
                    f"{drift} document(s) no longer match the stat the index stored — "
                    "something wrote to them outside Cognita (OneDrive sync is the "
                    "usual culprit). get_document and read_document always serve the "
                    "file on disk, so reads are correct; search results may be stale "
                    "until reindex_documents(force=true) runs."
                )
        current_pairs = await self._book_admitted_doc_pairs(project)
        if current_pairs is not None:
            keep = {(d.source, d.doc_id) for d in selected if (d.source, d.doc_id) in current_pairs}
            entries = [e for e in entries if any(
                e["filepath"] == source and e["id"] == doc_id for source, doc_id in keep
            )]
            payload["documents"] = entries
            payload["count"] = len(entries)
            payload["embedded_count"] = sum(1 for e in entries if e["tier"] != TIER_REGISTERED)
            payload["registered_count"] = len(entries) - payload["embedded_count"]
        registered = sum(1 for e in entries if e["tier"] == TIER_REGISTERED)
        payload["embedded_count"] = len(entries) - registered
        payload["registered_count"] = registered
        return _collection(payload, "documents", alias=False)

    async def _list_categories(self, project: Project, args: dict) -> dict:
        categories = await self.store.category_counts(project.name)
        return {"status": "success", "categories": categories,
                "total_documents": sum(categories.values())}

    async def _get_index_stats(self, project: Project, args: dict) -> dict:
        stats = await self.store.stats(project.name)
        conflicts = self.core.sync_conflicts.get(project.name, [])
        deindexed = self.deindexed(project)
        categories = await self.store.category_counts(project.name)
        policy = self.core.policy_for(project.name)
        payload = {
            "total_documents": stats.documents,
            "total_chunks": stats.chunks,
            # Per-tier split (D4.4-7). total_documents/total_chunks keep their
            # 3.x meaning for wire compat; chunks and vectors belong to the
            # embedded tier alone, so a merged count would hide whether the
            # tiers are behaving on a reindex.
            "tiers": {
                "embedded": {
                    "documents": stats.embedded_documents,
                    "chunks": stats.chunks,
                    "vectors": stats.chunks,
                    "extensions": sorted(policy.embedded),
                },
                "registered": {
                    "documents": stats.registered_documents,
                    "chunks": 0,
                    "vectors": 0,
                    "extensions": sorted(policy.registered),
                },
            },
            "categories": categories,
            "supported_formats": sorted(policy.all_extensions),
            "embedding_model": self.config.embedding_model,
            "embedding_dim": self.config.embedding_dimensions,
            "reranker_model": (self.config.reranker_model
                               if self.core.reranker else "disabled"),
            "chunk_size": self.core.chunk_size,
            "chunk_overlap": self.core.chunk_overlap,
            "query_cache": self.core.query_cache(project.name).stats(),
            "reindex": self._reindex_block(project.name),
            # 10.0: additive per-project scheduler accounting.  Snapshot is
            # nonblocking and contains no chunk text or filesystem paths.
            "scheduler": (self.scheduler.snapshot(project.name)
                           if self.scheduler is not None else None),
            # 5.0 §5.1: cloud-sync conflict copies found by the last walk and
            # deliberately NOT indexed. Reported rather than merely logged,
            # because the cost of the filter is a false positive — a real
            # document silently missing from the corpus — and a skip nobody can
            # see is exactly the failure the filter exists to prevent.
            "sync_conflicts": {
                "count": len(conflicts),
                "files": conflicts[:25],
                "patterns": (
                    self.core.sync_conflict_patterns
                    if self.core.sync_conflict_patterns is not None
                    else SYNC_CONFLICT_PATTERNS
                ),
                "note": (
                    "Not indexed. If one of these is a genuine document, rename it "
                    "or set sync_conflict_patterns in config/cognita.yaml."
                ),
            },
            # 5.7: paths a caller de-indexed on purpose with remove_document
            # (delete_file=false). Same reasoning as sync_conflicts above and as
            # 5.0 §10: the cost of the mechanism is a document missing from the
            # corpus for a reason nobody can see, so the exclusion is REPORTED,
            # not merely logged. This is also the only route back — the list
            # names what to write to if a suppression was a mistake.
            "deindexed": {
                "count": len(deindexed.paths()),
                "files": deindexed.sorted()[:25],
                "list_file": str(deindexed.path),
                "note": (
                    "On disk but deliberately not indexed, and it STAYS that way "
                    "across restarts and full rebuilds. Write to one of these paths "
                    "(add_document / update_document) or move it to index it again."
                ),
                **({"load_error": deindexed.load_error} if deindexed.load_error else {}),
            },
        }
        return {"status": "success", "stats": payload}

    async def _get_reindex_status(self, project: Project, args: dict) -> dict:
        progress = self._reindex_progress.get(project.name, {})
        conflicts = self.core.sync_conflicts.get(project.name, [])
        if progress.get("active"):
            return {"status": "success", "reindex": self._reindex_block(project.name)}
        out: dict[str, Any] = {"active": False}
        if "result" in progress:
            out["last_result"] = progress["result"]
        if "error" in progress:
            out["last_error"] = progress["error"]
        if conflicts:
            out["sync_conflicts_skipped"] = len(conflicts)
        return {"status": "success", "reindex": out}

    async def _evaluate_retrieval(self, project: Project, args: dict) -> dict:
        raw = args.get("test_cases")
        try:
            cases = json.loads(raw) if isinstance(raw, str) else raw
        except json.JSONDecodeError:
            return {"status": "error", "reason": "invalid", "message": "Invalid JSON for test_cases"}
        if not isinstance(cases, list) or not cases:
            return {"status": "error", "reason": "invalid", "message": "test_cases must be a non-empty JSON array"}
        k = 5
        per_query = []
        mrr_sum = recall_sum = 0.0
        for tc in cases:
            tc = tc if isinstance(tc, dict) else {}
            query = tc.get("query", "")
            expected = tc.get("expected_filepath", "")
            # Registered documents are excluded from retrieval by design, so
            # scoring retrieval quality against them is meaningless (D4.4-8).
            results = await self.core.search(
                project.name, query, max_results=k, include_registered=False
            )
            for r in results:
                r["filepath"] = r["source"]  # 5.0 §5.1: relative, as the tools accept
                r["source"] = self._abs(project, r["source"])
            found_rank = next(
                (i + 1 for i, r in enumerate(results) if expected in r.get("source", "")),
                None,
            )
            rr = 1.0 / found_rank if found_rank else 0.0
            mrr_sum += rr
            recall_sum += 1.0 if found_rank else 0.0
            per_query.append({
                "query": query,
                "expected": expected,
                "found_at_rank": found_rank,
                "reciprocal_rank": round(rr, 4),
                "top_result": results[0]["source"] if results else "none",
                # The absolute host path is unusable as an input; this one can be
                # handed straight back to get_document.
                "top_result_filepath": results[0]["filepath"] if results else None,
            })
        n = len(cases)
        return _collection(
            {"status": "success", "total_queries": n,
             "mrr_at_5": round(mrr_sum / n, 4), "recall_at_5": round(recall_sum / n, 4),
             "per_query": per_query},
            "per_query", alias=True,
        )

    async def _find_literal(self, project: Project, args: dict) -> dict:
        """Exhaustive literal/regex search (4.5, literals.py).

        Corpus = the index's document list; content = live bytes off disk. See
        literals.py's module docstring for why that split is the right one.
        """
        try:
            matcher = build_matcher(
                args.get("pattern") or "",
                regex=bool(args.get("regex", False)),
                case_sensitive=bool(args.get("case_sensitive", True)),
            )
        except BadPattern as exc:
            return {"status": "error", "reason": "bad_pattern",
                    "pattern": args.get("pattern") or "",
                    "regex": bool(args.get("regex", False)),
                    "message": str(exc)}

        raw_max = args.get("max_matches")
        max_matches = max(1, min(int(MAX_MATCHES_DEFAULT if raw_max is None else raw_max),
                                 MAX_MATCHES_CEILING))
        context_lines = max(0, min(int(args.get("context_lines") or 0), MAX_CONTEXT_LINES))
        category = args.get("category") or None
        glob = args.get("filepath_glob") or None
        include_registered = bool(args.get("include_registered", True))
        compact = bool(args.get("compact", False))

        docs = await self.store.list_documents(project.name)  # ORDER BY source
        is_indexed = self._indexed_source_predicate(project)
        admitted_pairs = await self._book_admitted_doc_pairs(project)
        selected = [
            d for d in docs
            if (not category or d.category == category)
            and (include_registered or not d.is_registered)
            and (not glob or glob_matches(d.source, glob))
            # Belt and braces: the backup tree is never indexed, so this should
            # never fire — but a rename sweep drowning in hits from old
            # snapshots is exactly the failure that makes an exhaustive tool
            # useless, so it is not left to depend on indexing config.
            and not d.source.startswith(f"{BACKUPS_DIRNAME}/")
            and is_indexed(d.source)
            and (admitted_pairs is None or (d.source, d.doc_id) in admitted_pairs)
        ]
        # Distinguish an empty filter selection from a scan that found no match;
        # both return zero matches, but only the latter proves absence.
        if not selected:
            payload = {
                "status": "success", "pattern": matcher.pattern,
                "regex": matcher.regex, "case_sensitive": matcher.case_sensitive,
                "files_scanned": 0, "files_with_matches": 0, "total_matches": 0,
                "truncated": False, "matches": [],
                "reason": "no_documents_selected",
                "corpus_size": len(docs),
                "message": _empty_selection_message(category, glob, include_registered,
                                                    len(docs)),
            }
            if category:
                payload["category"] = category
            if glob:
                payload["filepath_glob"] = glob
            return _collection(payload, "matches", alias=not compact)

        docs_dir = Path(project.documents_dir)
        found = await asyncio.to_thread(
            self._walk_literal, docs_dir, selected, matcher, context_lines, max_matches
        )
        current_pairs = await self._book_admitted_doc_pairs(project)
        if current_pairs is not None:
            current_selected = [
                d for d in selected if (d.source, d.doc_id) in current_pairs
            ]
            if len(current_selected) != len(selected):
                found = await asyncio.to_thread(
                    self._walk_literal, docs_dir, current_selected, matcher,
                    context_lines, max_matches,
                )
                # A second external save during the rescan still cannot publish
                # a stale role result; this last filter may shorten the page.
                final_pairs = await self._book_admitted_doc_pairs(project)
                if final_pairs is not None:
                    found["matches"] = [
                        item for item in found["matches"]
                        if any(item["filepath"] == source for source, _doc in final_pairs)
                    ]

        payload = {
            "status": "success",
            "pattern": matcher.pattern,
            "regex": matcher.regex,
            "case_sensitive": matcher.case_sensitive,
            "files_scanned": found["scanned"],
            "files_with_matches": found["files_with_matches"],
            "total_matches": found["total"],
            "truncated": found["total"] > len(found["matches"]),
            "matches": found["matches"],
        }
        if category:
            payload["category"] = category
        if glob:
            payload["filepath_glob"] = glob
        if found["skipped"]:
            payload["files_skipped"] = len(found["skipped"])
            payload["skipped"] = found["skipped"][:SKIPPED_LISTED]
        if found["timed_out"]:
            # The sweep was cut short, so it is NOT exhaustive — and the whole
            # value of this tool is that its zero can be trusted. Saying so is
            # mandatory: a partial sweep reported as complete is exactly the
            # silent miss find_literal exists to eliminate.
            payload["timed_out"] = True
            payload["exhaustive"] = False
            payload["message"] = (
                f"SWEEP INCOMPLETE: it exceeded the {LITERAL_WALK_BUDGET_S:.0f}s budget "
                f"after scanning {found['scanned']} of {len(selected)} selected file(s), so "
                "these results are PARTIAL and a zero here does NOT mean 'not present'. "
                "Narrow with filepath_glob or category, or simplify the pattern — a regex "
                "that backtracks catastrophically (nested quantifiers like (a+)+) on one "
                "very long line is the usual cause."
            )
        elif payload["truncated"]:
            payload["message"] = (
                f"{found['total']} matches found; returning the first {len(found['matches'])} "
                "in filepath/line order. Raise max_matches or narrow with filepath_glob — "
                "total_matches is the true count."
            )
        elif payload["total_matches"] == 0:
            # Scanned real files and found nothing: a TRUSTWORTHY zero, and
            # saying so is the point of an exhaustive tool. Distinguished from
            # the no_documents_selected case above by files_scanned.
            payload["reason"] = "no_matches"
            payload["message"] = (
                f"Scanned {found['scanned']} file(s) and found no occurrence of this "
                "pattern. This is an exhaustive search over the selected documents, so "
                "the answer is 'not present', not 'not found yet'."
            )
        return _collection(payload, "matches", alias=not compact)

    @staticmethod
    def _walk_literal(docs_dir: Path, docs, matcher, context_lines: int,
                      max_matches: int) -> dict:
        """Blocking disk walk (runs in a thread). Scans EVERY selected file even
        after the cap is reached: the cap limits what is returned, and
        total_matches has to stay the honest count or a truncated sweep would
        read as a complete one."""
        matches: list[dict] = []
        skipped: list[dict] = []
        total = scanned = files_with_matches = 0
        # A wall-clock ceiling on the whole sweep. `re` has no timeout, so a
        # catastrophic-backtracking pattern from a caller — (a+)+$ against one
        # very long line, and a minified .js or a base64 blob in a registered
        # file IS one long line — runs forever in a thread that cannot be
        # canceled. find_literal is in READONLY_TOOLS, so a READ-ONLY token
        # could do it repeatedly and starve the default executor, taking
        # embedding, parsing and file_facts down with it. This cannot interrupt a
        # single pathological match, but it stops the sweep between files and
        # returns an honest partial answer instead of never returning.
        deadline = time.monotonic() + LITERAL_WALK_BUDGET_S
        timed_out = False
        for record in docs:
            if time.monotonic() > deadline:
                timed_out = True
                break
            target = docs_dir / record.source
            try:
                raw = target.read_bytes()
            except FileNotFoundError:
                # Indexed but gone from disk. Reported, never silently counted
                # as "no match" — that would be an invisible hole in a sweep.
                skipped.append({"filepath": record.source, "reason": "missing_on_disk"})
                continue
            except OSError as exc:
                skipped.append({"filepath": record.source, "reason": "unreadable",
                                "detail": exc.strerror or str(exc)})
                continue
            view = classify_text_bytes(raw)
            if not view.accepted:
                skipped.append({"filepath": record.source,
                                "reason": view.reason or "binary_content"})
                continue
            # Search the same tolerant readable view returned by read_document.
            # Source bytes remain untouched; exact raw-byte search is explicitly
            # not this tool's contract.
            text = view.text
            scanned += 1
            hit_here = False
            for hit in scan_text(text, matcher, context_lines):
                total += 1
                hit_here = True
                if len(matches) < max_matches:
                    item = {"filepath": record.source, "source": str(target),
                            "tier": record.tier, **hit}
                    if view.content_is_lossy or view.index_text_sanitized:
                        item.update({
                            "utf8_valid": view.utf8_valid,
                            "decode_error_bytes": view.decode_error_bytes,
                            "content_is_lossy": view.content_is_lossy,
                            "index_text_sanitized": view.index_text_sanitized,
                        })
                    matches.append(item)
            if hit_here:
                files_with_matches += 1
        return {"matches": matches, "skipped": skipped, "total": total,
                "scanned": scanned, "files_with_matches": files_with_matches,
                "timed_out": timed_out}
