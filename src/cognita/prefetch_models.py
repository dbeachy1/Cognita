"""Download the search models now, not during the first search (installer design 6.4, C5).

    python -m cognita.prefetch_models

The installer runs this once, inside the app image through ``compose run --rm --no-deps -T``,
so the 2.3 GB of model files arrive with visible progress instead of silently inside the
first search (which sits inside the live self-test's time budget).

It does NOT reimplement model loading. It loads the effective config the way ``__main__``
does, constructs the ``Embedder`` and ``Reranker`` exactly as ``_build_engine_host`` does
(same cache dir, same threads, same degrade-in-steps constructor), and forces the download
by embedding one short string and reranking one pair.

Exit code: 0 when both models loaded and ran, 1 when either failed. A failure is not fatal
to the install (the CLI treats it as a warning: search works without the reranker, in RRF
order, and the embedder loads at first index).

Log lines, one per model:

    prefetch.start model=<name> cache=<dir>
    prefetch.done model=<name> elapsed_s=<s> bytes_on_disk=<n>
    prefetch.failed model=<name> reason=<type: message>
"""
from __future__ import annotations

import logging
import os
import sys
import time
from pathlib import Path

log = logging.getLogger("cognita.prefetch")

# Expected download size per model, in bytes, so the CLI can show "1.1 of about 2.3 GB".
# 🔴 These are ESTIMATES, not measurements. The only figure observed is 2.3 GB for the two
# default models together (the Linux install proof); the split below apportions it by the
# models' published sizes and was not measured per model. A model missing from this dict is
# still downloaded; the caller just has no denominator for it. 14.0 made the default reranker
# BAAI/bge-reranker-v2-m3; its figure is the size_in_gb=2.27 of its pinned spec in
# embeddings.PINNED_RERANKERS, not a measurement either. The Jina entry stays for a config
# that still names it.
EXPECTED_BYTES: dict[str, int] = {
    "BAAI/bge-large-en-v1.5": 1_340_000_000,
    "BAAI/bge-reranker-v2-m3": 2_270_000_000,
    "jinaai/jina-reranker-v2-base-multilingual": 960_000_000,
}


def bytes_on_disk(cache_dir: Path, model_name: str) -> int:
    """Bytes of regular files in ``cache_dir`` that belong to ``model_name``.

    fastembed keeps a model under a directory whose name contains the model name with
    ``/`` turned into ``--`` (Hugging Face cache layout). If no directory matches (a
    different layout), the whole cache directory is counted, which is the honest upper
    bound. Symlinks are skipped so the blob behind a snapshot link is counted once.
    """
    cache_dir = Path(cache_dir)
    if not cache_dir.is_dir():
        return 0
    marker = model_name.replace("/", "--")
    roots = [entry for entry in cache_dir.iterdir() if entry.is_dir() and marker in entry.name]
    if not roots:
        roots = [cache_dir]
    total = 0
    for root in roots:
        for base, _dirs, files in os.walk(root, followlinks=False):
            for name in files:
                path = os.path.join(base, name)
                if not os.path.islink(path):
                    try:
                        total += os.path.getsize(path)
                    except OSError as exc:
                        log.warning("prefetch.size_skip path=%s reason=%s: %s", path, type(exc).__name__, exc)
    return total


def _run_one(model_name: str, cache_dir: Path, work) -> bool:
    """Run ``work()`` under the start/done/failed log lines. True when it succeeded."""
    log.info("prefetch.start model=%s cache=%s", model_name, cache_dir)
    started = time.monotonic()
    try:
        work()
    except Exception as exc:  # noqa: BLE001 - every failure is reported with its reason, never dropped
        log.error("prefetch.failed model=%s reason=%s: %s", model_name, type(exc).__name__, exc)
        return False
    log.info(
        "prefetch.done model=%s elapsed_s=%.1f bytes_on_disk=%d",
        model_name, time.monotonic() - started, bytes_on_disk(cache_dir, model_name),
    )
    return True


def prefetch(config) -> bool:
    """Download both models named by ``config``. True only when both worked."""
    # Imported here, as __main__._build_engine_host does, so this module imports cheaply.
    from .embeddings import Embedder, Reranker

    cache_dir = Path(config.models_cache_dir)
    embedder = Embedder(
        config.embedding_model,
        config.embedding_dimensions,
        cache_dir,
        threads=config.embedding_threads,
        batch_size=config.embed_batch_size,
    )
    reranker = Reranker(
        config.reranker_model,
        cache_dir,
        threads=config.embedding_threads,
    )

    def embed_one() -> None:
        vectors = embedder.embed(["Cognita model prefetch."])
        if len(vectors) != 1:
            raise RuntimeError(f"expected 1 vector, got {len(vectors)}")

    def rerank_one() -> None:
        # Reranker swallows a load or run failure and answers None (search then falls back
        # to RRF order). For a prefetch that is a failure, and it must be reported as one.
        scores = reranker.rerank("model prefetch", ["Cognita downloads its search models before first use."])
        if scores is None:
            raise RuntimeError("reranker could not be loaded or run; see the cognita.embeddings warning above")
        if len(scores) != 1:
            raise RuntimeError(f"expected 1 score, got {len(scores)}")

    embedded = _run_one(config.embedding_model, cache_dir, embed_one)
    reranked = _run_one(config.reranker_model, cache_dir, rerank_one)
    return embedded and reranked


def main(argv: list[str] | None = None) -> int:
    from .config import load_config

    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stdout,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    started = time.monotonic()
    try:
        config = load_config()
    except Exception as exc:  # noqa: BLE001 - reported with its reason, exit 1
        log.error("prefetch.failed model=<config> reason=%s: %s", type(exc).__name__, exc)
        return 1
    ok = prefetch(config)
    log.info("prefetch.finished ok=%s elapsed_s=%.1f", ok, time.monotonic() - started)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
