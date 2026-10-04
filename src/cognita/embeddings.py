"""Shared embedding + reranker models for the 4.0 retrieval core (D4.6, D4.8).

One lazy, thread-safe instance of each model serves ALL projects — in 3.x every
worker process loaded its own ~1.3 GB copy. Same models, same cache, no API
dependency: fastembed BAAI/bge-large-en-v1.5 (1024-dim) for embeddings and
BAAI/bge-reranker-v2-m3 for cross-encoder reranking (14.0.0; a pinned ONNX
export Cognita downloads itself — see PINNED_RERANKERS. It replaced
jinaai/jina-reranker-v2-base-multilingual, whose weights are CC-BY-NC).

IMPORTANT parity detail: 3.x embedded documents AND queries through the same
plain ``embed()`` path (the ChromaDB embedding_function interface), never
fastembed's bge-specific ``query_embed()`` (which prepends an instruction
prefix). We do the same, so 4.0's vectors and similarities are bit-identical
to 3.x for the same text (D4.8: "the vector math itself is identical").

Failures are sticky and LOUD (a 3.8.1 lesson: silently returning zero vectors
corrupts an index in ways count-based checks can't see).
"""

from __future__ import annotations

import ctypes
import hashlib
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from .embed_telemetry import CPU_DEVICE, record_batch, record_cpu_init

log = logging.getLogger("cognita.embeddings")

_LIBC = None


def release_to_os() -> None:
    """Hand freed heap pages back to the OS.

    Python's allocator and ONNX Runtime's CPU arena both free into glibc's heap
    without returning pages, so RSS stays at the high-water mark of the largest
    bulk embed the process ever ran (12.8 GiB observed on kei, against a ~2 GB
    working set). malloc_trim(0) walks the arenas and MADV_DONTNEEDs what is
    genuinely free. Safe to call at any point — it only releases memory nothing
    holds a pointer into. No-op on non-glibc (Windows dev boxes included).
    """
    global _LIBC
    try:
        if _LIBC is None:
            _LIBC = ctypes.CDLL("libc.so.6")
        _LIBC.malloc_trim(0)
    except Exception:
        pass  # not glibc, or trim unavailable — never fatal


def resolve_threads(threads: int) -> int:
    """Clamp a configured ORT intra-op thread count to what the box actually has.

    A number larger than the core count is not a bigger pool, it is
    oversubscription: ORT still spawns the threads and they contend. The config
    default is tuned for kei (32 logical cores) and must not become a pessimism
    on a 4-core VM, so it is clamped rather than trusted. 0 is passed through
    untouched — it is the documented "let ORT decide" escape hatch, not a count.
    """
    if threads <= 0:
        return 0
    return min(threads, os.cpu_count() or threads)


# ONNX Runtime pools its CPU allocations in an "arena" and, by default, NEVER
# returns those blocks — it keeps them on its own free list for the life of the
# process. That is what held 12.8 GiB on kei: 81% of the resident set was ONE
# 10.8 GiB anonymous mapping, an arena block. glibc still counts it live, which
# is why malloc_trim(0) reclaims almost nothing (~20 MB of a 5.25 GB process).
#
# ORT's documented switch for this is the per-Run option
# `memory.enable_memory_arena_shrinkage`, which scans and shrinks the arena at
# the end of every Run(). We cannot use it: fastembed owns the session.run()
# call and exposes no way to pass RunOptions, and there is an open upstream
# issue (microsoft/onnxruntime#23339) reporting it has no effect in Python.
#
# What fastembed DOES expose — its EXPOSED_SESSION_OPTIONS allowlist is exactly
# ('enable_cpu_mem_arena',) — is turning the arena off. ORT then allocates and
# frees per inference, so memory goes back to the OS when the work is done.
#
# 🔴 This is NOT a cap. It does not limit memory, threads, or batch size. An
# index may take whatever it needs; it simply stops HOARDING afterwards.
_NO_ARENA = {"enable_cpu_mem_arena": False}


def _construct_without_arena(cls, kwargs: dict, what: str = "model") -> tuple:
    """Build a fastembed model with ORT's CPU arena disabled.

    Returns ``(model, step)`` where ``step`` names the rung that succeeded, so
    the caller can say in its own log line what it is actually running.

    Degrades in steps, because a model that fails to load is far worse than a
    model that keeps its arena: without a reranker, search silently drops to RRF
    order, and without an embedder there is no index at all. In order:

      1. the arena setting plus everything asked for  (what we want)
      2. drop `extra_session_options`  — an older fastembed has no such
         parameter (TypeError), or does not list `enable_cpu_mem_arena` in its
         EXPOSED_SESSION_OPTIONS allowlist (AssertionError)
      3. drop `threads` as well — some builds' TextCrossEncoder does not take it

    Each step is strictly less capable than the one before, and the last is the
    plain construction that has always worked.

    🔴 Every degradation is logged at WARNING with the exception that caused it
    (DESIGN-6.0 §14.5). It used to degrade in silence, which meant a process
    quietly running one rung down — keeping the very arena 5.10 exists to
    disable — was indistinguishable from one running as designed. That is the
    exact shape of defect this repo has shipped and then documented as intended
    behavior three times; a ladder nobody can see the position of is not a
    safety net, it is a hiding place.
    """
    attempts = [
        ("arena-off", dict(kwargs, extra_session_options=dict(_NO_ARENA))),
        ("default", dict(kwargs)),
        ("no-threads", {k: v for k, v in kwargs.items() if k != "threads"}),
    ]
    last: Exception | None = None
    for step, attempt in attempts:
        try:
            model = cls(**attempt)
        except (TypeError, AssertionError) as exc:
            log.warning(
                "%s construction step %r failed (%s: %s); trying the next rung "
                "down. The ORT CPU arena stays ON below the first rung, which is "
                "the 5.10 memory behavior this build is meant to avoid.",
                what, step, type(exc).__name__, exc,
            )
            last = exc
            continue
        if step != "arena-off":
            log.warning(
                "%s loaded at degraded step %r — ORT's CPU memory arena is ACTIVE "
                "for this model and will not return its blocks to the OS.",
                what, step,
            )
        return model, step
    raise last  # type: ignore[misc]


def _session_facts(model) -> dict:
    """What a loaded fastembed model is ACTUALLY running, read back from it.

    Best effort by design: every field is optional and any failure here must
    never break a model that loaded fine. fastembed's internals are not a public
    API, so this reads defensively and reports what it could see.

    ``provider_active`` comes off the live ``InferenceSession``, never from what
    was requested — a runtime that loads and then serves a different provider is
    invisible in every other signal (§14.2). ``model_file`` is the ONNX artifact
    fastembed selected: it ships quantized variants for some models and picks by
    name, so which file is on disk is a fact worth logging rather than an
    assumption to re-derive during a later investigation (§9.1).
    """
    facts: dict = {}
    try:
        inner = getattr(model, "model", None)
        session = getattr(inner, "model", None)
        if session is not None and hasattr(session, "get_providers"):
            facts["provider_active"] = ",".join(session.get_providers())
        description = getattr(inner, "model_description", None)
        if description is not None:
            facts["model_file"] = getattr(description, "model_file", None)
            source = getattr(description, "sources", None)
            facts["model_source"] = getattr(source, "hf", None) if source else None
    except Exception:  # pragma: no cover - diagnostics must never be fatal
        log.debug("Could not read session facts back from the model", exc_info=True)
    return {k: v for k, v in facts.items() if v}


class EmbeddingUnavailable(RuntimeError):
    """The embedding model could not be loaded or run."""


class Embedder:
    """Lazy fastembed TextEmbedding wrapper (CPU, as in production 3.x)."""

    def __init__(
        self,
        model_name: str,
        dimensions: int,
        cache_dir: Path,
        threads: int = 0,
        batch_size: int = 256,
    ):
        self.model_name = model_name
        self.dimensions = dimensions
        self._cache_dir = str(cache_dir)
        self._threads = resolve_threads(threads)
        self._batch_size = batch_size
        self._model = None
        self._lock = threading.Lock()
        self._load_failed: Exception | None = None

    def _ensure_model(self):
        if self._model is not None:
            return self._model
        if self._load_failed is not None:
            raise EmbeddingUnavailable(
                f"Embedding model previously failed to load: {self._load_failed}"
            ) from self._load_failed
        with self._lock:
            if self._model is not None:
                return self._model
            try:
                from fastembed import TextEmbedding

                kwargs = {
                    "model_name": self.model_name,
                    "cache_dir": self._cache_dir,
                    "providers": ["CPUExecutionProvider"],
                }
                # threads=0 means "let ORT decide" — the pre-5.8.0 behavior,
                # i.e. an intra-op pool sized to the box's core count and an
                # arena that grows with it.
                if self._threads:
                    kwargs["threads"] = self._threads
                started = time.monotonic()
                self._model, step = _construct_without_arena(
                    TextEmbedding, kwargs, what="Embedder"
                )
                record_cpu_init(
                    model=self.model_name,
                    dims=self.dimensions,
                    threads=self._threads,
                    batch=self._batch_size,
                    provider_requested="CPUExecutionProvider",
                    **_session_facts(self._model),
                    arena="off" if step == "arena-off" else "ON",
                    step=step,
                    session_build=time.monotonic() - started,
                )
            except Exception as exc:
                self._load_failed = exc
                raise EmbeddingUnavailable(f"Failed to load {self.model_name}: {exc}") from exc
            return self._model

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Embed texts (documents and queries alike — see module docstring)."""
        if not texts:
            return []
        model = self._ensure_model()
        started = time.monotonic()
        # Bound BEFORE the try so the `finally` can always report how many
        # vectors actually exist, including when the very first call raises.
        vectors: list[list[float]] = []
        try:
            # Stream the generator instead of materializing every vector first.
            # tolist() is applied per-vector and the numpy array is dropped
            # immediately, so peak transient memory is one batch, not the corpus.
            for v in model.embed(texts, batch_size=self._batch_size):
                vectors.append(v.tolist())
        except Exception as exc:
            raise EmbeddingUnavailable(f"Embedding generation failed: {exc}") from exc
        finally:
            # Timed and recorded BEFORE release_to_os(), so `elapsed` is the
            # embed itself and not the embed plus a malloc_trim. The record is
            # in the `finally` so a failed embed still shows up as TIME SPENT —
            # a walk that got slower because every call was raising must not
            # look like a walk that did no work.
            #
            # 🔴 But `chunks` is a WORK-COMPLETED count, not time, and it was
            # reported as `len(texts)` on the failure path too. §14.4 uses it as
            # the actual side of `est_error`, so a walk that aborted mid-window
            # logged `chunks=512 ... indexed=0` and anything summing `embed.done
            # chunks=` counted 512 vectors that are not in the index. Report what
            # was produced; the elapsed time — which is the thing the `finally`
            # exists for — is unaffected.
            record_batch(
                CPU_DEVICE,
                chunks=len(vectors),
                chars=sum(len(t) for t in texts[:len(vectors)]),
                elapsed=time.monotonic() - started,
            )
            # A bulk embed is exactly when the arena ratchets up. Give the pages
            # back now rather than holding them for the life of the process.
            # len(texts) > 1 keeps the trim off the single-query search path
            # (retrieval.py), which runs per user query and must stay hot.
            if len(texts) > 1:
                release_to_os()
        if len(vectors) != len(texts):
            raise EmbeddingUnavailable(
                f"Embedding count mismatch: expected {len(texts)}, got {len(vectors)}"
            )
        if vectors and len(vectors[0]) != self.dimensions:
            raise EmbeddingUnavailable(
                f"Embedding dim mismatch: expected {self.dimensions}, got {len(vectors[0])}"
            )
        return vectors


# 14.0 §2.1: fastembed 0.8.0 cannot pin a model revision — its Hugging Face
# download asks the repo for its CURRENT sha. So for a model in this table
# Cognita downloads the pinned files itself (`_fetch_pinned`) and hands fastembed
# the local snapshot directory through `specific_model_path`, which makes
# fastembed skip its own download. Every file is verified against the size and
# SHA-256 below on every load.
@dataclass(frozen=True)
class PinnedModel:
    repo: str
    revision: str
    model_file: str
    additional_files: tuple[str, ...]
    license: str
    size_in_gb: float
    # relative path -> (size in bytes, sha256 hex)
    files: dict[str, tuple[int, str]] = field(default_factory=dict)


# BAAI/bge-reranker-v2-m3 through the onnx-community ONNX export. Values from the
# Hugging Face tree API at this revision on 2026-09-28: LFS object ids are SHA-256
# values, and the small files were hashed after download. The weights are
# Apache-2.0 (upstream BAAI repo, commit 953dc6f6f85a1b2dbfca4c34a2796e7dde08d41e).
# The export repo's model card names BAAI/bge-reranker-v2-m3 as its base and
# carries no license line of its own; this repo is a format conversion and adds no
# licensed content, and exporting the weights ourselves would add a build step.
# Both commits are recorded in THIRD_PARTY_NOTICES.md.
PINNED_RERANKERS: dict[str, PinnedModel] = {
    "BAAI/bge-reranker-v2-m3": PinnedModel(
        repo="onnx-community/bge-reranker-v2-m3-ONNX",
        revision="6f5ff65298512715a1e669753bc754d2bc8f367b",
        model_file="onnx/model.onnx",
        additional_files=("onnx/model.onnx_data",),
        license="apache-2.0",
        size_in_gb=2.27,
        files={
            "config.json": (
                848,
                "122e922dcfed6503c8721e6fe1daf090340c3d95ca7f3aa3a72730b321a51cfd",
            ),
            "tokenizer.json": (
                17_082_900,
                "8bf8afbfd11306bd872018c53bfdf2e160a56f8edbcf49933324404791c148d3",
            ),
            "tokenizer_config.json": (
                1_203,
                "b87c8703482b0300d3da30e201519aa641f6a450f5eb5bf1e624afbf70c74d80",
            ),
            "special_tokens_map.json": (
                964,
                "8c785abebea9ae3257b61681b4e6fd8365ceafde980c21970d001e834cf10835",
            ),
            "onnx/model.onnx": (
                656_891,
                "faae32b124a9d54afb7e89b5e9896e03c18a9552d56d1d6b273a709a83012486",
            ),
            "onnx/model.onnx_data": (
                2_271_088_656,
                "f009aa6c6cf21986fd7e0021fa66b20ccce27abc6900a57c7109c8496811bcbe",
            ),
        },
    ),
}

_HASH_BLOCK = 1024 * 1024
# Guards `_register_pinned`: the check against fastembed's registry and the
# registration itself must be one step, or two Rerankers built at once could
# both see "absent" and the second add_custom_model would raise on a duplicate.
_REGISTER_LOCK = threading.Lock()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(_HASH_BLOCK), b""):
            digest.update(block)
    return digest.hexdigest()


def pinned_model_dir(spec: PinnedModel, cache_dir: str) -> Path:
    """Where a pinned model's files live: REAL files, one directory, owned by us.

    Not the Hugging Face cache's snapshot directory. There, on Linux, every file is
    a symlink into a content-addressed blob store, and ONNX Runtime resolves the
    model file through its link and then refuses the external weights file
    (`model.onnx_data`) because ITS link resolves to a different blob directory:
    "External data path escapes model directory". Seen on kei's test target
    2026-09-28 with onnxruntime 1.30; Windows passed only because its cache holds
    copies. `hf_hub_download(local_dir=...)` writes plain files instead.
    """
    return Path(cache_dir) / "pinned" / spec.repo.replace("/", "--") / spec.revision


def _fetch_pinned(name: str, spec: PinnedModel, cache_dir: str) -> Path:
    """Download (if not present) and verify a pinned model; return its directory.

    The returned directory (`pinned_model_dir`) is what goes to fastembed as
    ``specific_model_path``.
    Every file is size- and SHA-256-checked on EVERY load: that runs off the
    request path (background load), needs no marker file and no state, and cost
    1.4 s on Maia's NVMe with the 2.3 GB file probably in the OS cache (a cold
    read on kei is not measured and may take several seconds — the elapsed time
    is in the `reranker.ready` line).

    A mismatch logs ERROR with the file's path and raises RuntimeError; NOTHING is
    deleted. huggingface_hub moves a file into place only when it is complete, so
    a mismatch means something altered the directory, and the ERROR names the path
    to remove. Any other exception (network, disk full, HF_HUB_OFFLINE with an
    empty directory) propagates to `Reranker._ensure_model`, which turns it into
    one WARNING and RRF order for the life of the process.
    """
    # Imported here, not at module level: importing `embeddings` must not import
    # huggingface_hub, and tests replace this function.
    from huggingface_hub import hf_hub_download

    local_dir = pinned_model_dir(spec, cache_dir)
    big = max(spec.files, key=lambda rel: spec.files[rel][0])
    first_fetch = not (local_dir / big).is_file()
    started = time.monotonic()
    if first_fetch:
        log.info(
            "reranker.download start model=%s repo=%s revision=%s bytes=%d",
            name, spec.repo, spec.revision, sum(size for size, _ in spec.files.values()),
        )

    downloaded: dict[str, Path] = {}
    for rel in spec.files:
        # A file already in local_dir at this revision is reused without a download.
        downloaded[rel] = Path(
            hf_hub_download(spec.repo, rel, revision=spec.revision, local_dir=local_dir)
        )
    if first_fetch:
        log.info(
            "reranker.download done model=%s files=%d elapsed_s=%.1f",
            name, len(downloaded), time.monotonic() - started,
        )

    verify_started = time.monotonic()
    for rel, (want_size, want_hash) in spec.files.items():
        path = downloaded[rel]
        have_size = path.stat().st_size
        if have_size != want_size:
            log.error(
                "reranker.verify mismatch model=%s path=%s expected_size=%d "
                "actual_size=%d; nothing was deleted — remove that file to fetch it "
                "again",
                name, path, want_size, have_size,
            )
            raise RuntimeError(
                f"{name}: {path} has size {have_size}, expected {want_size}"
            )
        have_hash = _file_sha256(path)
        if have_hash != want_hash:
            log.error(
                "reranker.verify mismatch model=%s path=%s expected_sha256=%s "
                "actual_sha256=%s; nothing was deleted — remove that file to fetch it "
                "again",
                name, path, want_hash, have_hash,
            )
            raise RuntimeError(
                f"{name}: {path} has sha256 {have_hash}, expected {want_hash}"
            )
    log.info(
        "reranker.verify ok model=%s files=%d elapsed_s=%.1f first_fetch=%s",
        name, len(spec.files), time.monotonic() - verify_started, first_fetch,
    )
    return local_dir


def _register_pinned(name: str, spec: PinnedModel) -> None:
    """Tell fastembed about a pinned model so TextCrossEncoder accepts its name.

    `list_supported_models()` returns DICTS, so the check is against each entry's
    "model" value, case-insensitively as fastembed's own constructor compares. A
    bare `name in list` is always false. `add_custom_model` raises ValueError on
    a duplicate, so this check under the lock is what makes a second Reranker
    (or a name fastembed ships built in) safe. Never called at import time.
    """
    from fastembed.common.model_description import ModelSource
    from fastembed.rerank.cross_encoder import TextCrossEncoder

    with _REGISTER_LOCK:
        known = {
            str(entry.get("model", "")).lower()
            for entry in TextCrossEncoder.list_supported_models()
        }
        if name.lower() in known:
            log.info(
                "reranker.register skipped model=%s: fastembed already knows the name",
                name,
            )
            return
        TextCrossEncoder.add_custom_model(
            name,
            sources=ModelSource(hf=spec.repo),
            model_file=spec.model_file,
            additional_files=list(spec.additional_files),
            license=spec.license,
            size_in_gb=spec.size_in_gb,
            description=f"{name} ONNX export pinned by Cognita at {spec.revision}",
        )
        log.info(
            "reranker.register model=%s repo=%s revision=%s",
            name, spec.repo, spec.revision,
        )


class Reranker:
    """Lazy fastembed TextCrossEncoder wrapper. Degrades gracefully: if the
    model can't load, search falls back to RRF order (as in 3.x).

    14.0 §2.4: `LocalEngineHost.startup()` calls `start_background_load()`, so a
    2.3 GB first download happens off the request path. While that load is in
    progress `rerank()` returns None at once (RRF order) instead of blocking a
    search on it. Without a background load (CLI, tests) `rerank()` still loads
    synchronously on first use."""

    def __init__(self, model_name: str, cache_dir: Path, threads: int = 0):
        self.model_name = model_name
        self._cache_dir = str(cache_dir)
        self._threads = resolve_threads(threads)
        self._model = None
        self._lock = threading.Lock()
        self._load_failed = False
        # `_ensure_model` holds `_lock` for the whole download, and
        # start_background_load() is called from async startup(), so it must not
        # wait on `_lock`. It takes this small lock instead, held only for the
        # few lines that create the thread.
        self._start_lock = threading.Lock()
        self._load_thread: threading.Thread | None = None
        self._loading_logged = False

    def start_background_load(self) -> None:
        """Begin loading the model on a daemon thread; never blocks, idempotent."""
        with self._start_lock:
            if self._model is not None or self._load_failed or self._load_thread is not None:
                log.debug(
                    "reranker.background_load skipped model=%s state=%s",
                    self.model_name, self.state(),
                )
                return
            thread = threading.Thread(
                target=self._ensure_model, name="reranker-load", daemon=True
            )
            thread.start()
            # Stored only after start() succeeded: a failed start() must not
            # leave a never-started thread that makes every rerank() think a
            # load is in progress. _ensure_model already catches and logs.
            self._load_thread = thread
            log.info("reranker.background_load started model=%s", self.model_name)

    def state(self) -> str:
        """"ready", "failed", "loading" or "not_loaded". Reads flags only — never
        takes a lock and never blocks, because /healthz calls it."""
        if self._model is not None:
            return "ready"
        if self._load_failed:
            return "failed"
        if self._load_thread is not None:
            return "loading"
        return "not_loaded"

    def _ensure_model(self):
        if self._load_failed:
            return None
        if self._model is None:
            with self._lock:
                if self._model is None and not self._load_failed:
                    try:
                        from fastembed.rerank.cross_encoder import TextCrossEncoder

                        started = time.monotonic()
                        kwargs = {
                            "model_name": self.model_name,
                            "cache_dir": self._cache_dir,
                        }
                        spec = PINNED_RERANKERS.get(self.model_name)
                        if spec is not None:
                            # 14.0 §2.1/§2.3: fetch + verify the pinned files,
                            # register the name, and point fastembed at the local
                            # snapshot. The kwarg sits in the base kwargs, so every
                            # rung of the ladder keeps it.
                            root = _fetch_pinned(self.model_name, spec, self._cache_dir)
                            _register_pinned(self.model_name, spec)
                            kwargs["specific_model_path"] = str(root)
                        fetched = time.monotonic()
                        if self._threads:
                            kwargs["threads"] = self._threads
                        self._model, step = _construct_without_arena(
                            TextCrossEncoder, kwargs, what="Reranker"
                        )
                        log.info(
                            "reranker.ready model=%s step=%s pinned=%s "
                            "fetch_verify_s=%.1f elapsed_s=%.1f",
                            self.model_name, step, spec is not None,
                            fetched - started, time.monotonic() - started,
                        )
                    except Exception:
                        # Unchanged: the reranker is allowed to be absent, and
                        # search falls back to RRF order. But it must not be
                        # absent SILENTLY — an unranked search looks like a
                        # working one, so the reason goes in the log exactly
                        # once, at WARNING, on the load that failed.
                        log.warning(
                            "Reranker %s could not be loaded; search will fall back "
                            "to RRF order for the life of this process.",
                            self.model_name, exc_info=True,
                        )
                        self._load_failed = True
                        return None
        return self._model

    def rerank(self, query: str, texts: list[str]) -> list[float] | None:
        """Cross-encoder scores for (query, text) pairs; None if unavailable."""
        if not texts:
            return []
        if (
            self._load_thread is not None
            and self._model is None
            and not self._load_failed
        ):
            # A background load is still running. Waiting on `_lock` here would
            # park this search behind a possible multi-minute download, so answer
            # None (RRF order) now. Logged once per load, then quiet.
            with self._start_lock:
                first = not self._loading_logged
                self._loading_logged = True
            if first:
                log.info(
                    "reranker.loading — search is using RRF order until the "
                    "model is ready (model=%s)", self.model_name,
                )
            return None
        model = self._ensure_model()
        if model is None:
            return None
        try:
            return [float(s) for s in model.rerank(query, texts)]
        except Exception:
            # Still returns None (RRF order), but a scoring fault is no longer
            # invisible: an unranked search looks like a working one.
            log.warning(
                "Reranker %s failed while scoring %d passages; this search used "
                "RRF order.", self.model_name, len(texts), exc_info=True,
            )
            return None
