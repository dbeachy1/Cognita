"""Dev harness for the 4.0 retrieval core: index a directory, search, stats.

NOT the MCP surface (that's M3) — a direct line to the core for development,
the M2 smoke runs, and the M5 burn-in gauntlet. Examples (on kei):

    export COGNITA_PG_DSN=postgresql:///cognita
    export COGNITA_MODELS_CACHE_DIR="$HOME/Cognita/models_cache"
    .venv/bin/python scripts/retrieval-cli.py index --project Example \
        --docs "$HOME/Documents/research-notes"
    .venv/bin/python scripts/retrieval-cli.py search --project Example --query "build notes"
    .venv/bin/python scripts/retrieval-cli.py stats --project Example
"""

import argparse
import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from cognita.config import load_config  # noqa: E402
from cognita.embeddings import Embedder, Reranker  # noqa: E402
from cognita.retrieval import RetrievalCore  # noqa: E402
from cognita.store import Store  # noqa: E402


def build_core(config, store: Store, with_reranker: bool = True) -> RetrievalCore:
    embedder = Embedder(
        config.embedding_model, config.embedding_dimensions, config.models_cache_dir
    )
    reranker = Reranker(config.reranker_model, config.models_cache_dir) if with_reranker else None
    return RetrievalCore(
        store,
        embedder,
        reranker,
        chunk_size=config.chunk_size,
        chunk_overlap=config.chunk_overlap,
        exclude_patterns=config.index_exclude_patterns,
        category_mappings=config.category_mappings,
        keyword_routes=config.keyword_routes,
    )


async def cmd_index(args, config, store):
    core = build_core(config, store, with_reranker=False)  # indexing never reranks
    t0 = time.perf_counter()

    def progress(state):
        print(
            f"\r  {state['processed']}/{state['total_files']} files "
            f"(indexed {state['indexed']}, skipped {state['skipped']})",
            end="",
            flush=True,
        )

    summary = await core.index_project(
        args.project, Path(args.docs), force=args.force, progress=progress
    )
    elapsed = time.perf_counter() - t0
    print(f"\n{args.project}: {summary['indexed']} indexed, {summary['skipped']} skipped, "
          f"{summary['removed']} removed in {elapsed:.1f}s")
    for err in summary["errors"]:
        print(f"  ERROR {err}")
    stats = await store.stats(args.project)
    print(f"index now holds {stats.documents} documents / {stats.chunks} chunks")
    violations = await store.check_consistency(args.project)
    print(f"consistency: {'OK' if not violations else violations}")


async def cmd_search(args, config, store):
    core = build_core(config, store)
    t0 = time.perf_counter()
    results = await core.search(
        args.project,
        args.query,
        max_results=args.n,
        category=args.category,
        hybrid_alpha=args.alpha,
    )
    elapsed = time.perf_counter() - t0
    print(f"{len(results)} results for {args.query!r} (alpha={args.alpha}) in {elapsed:.2f}s\n")
    for i, r in enumerate(results, 1):
        snippet = " ".join(r["content"].split())[:180]
        print(f"{i}. {r['source']}  [chunk {r['chunk_index']}]  "
              f"score={r['score']} method={r['search_method']} "
              f"rerank={r['reranker_score']}")
        print(f"   {snippet}\n")


async def cmd_stats(args, config, store):
    stats = await store.stats(args.project)
    print(f"{args.project}: {stats.documents} documents, {stats.chunks} chunks")
    for doc in await store.list_documents(args.project):
        print(f"  {doc.source}  ({doc.format}, {doc.category}, indexed {doc.indexed_at:%Y-%m-%d %H:%M})")


async def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_index = sub.add_parser("index", help="(re)index a documents directory")
    p_index.add_argument("--project", required=True)
    p_index.add_argument("--docs", required=True)
    p_index.add_argument("--force", action="store_true")

    p_search = sub.add_parser("search", help="hybrid search")
    p_search.add_argument("--project", required=True)
    p_search.add_argument("--query", required=True)
    p_search.add_argument("--alpha", type=float, default=0.3)
    p_search.add_argument("-n", type=int, default=5)
    p_search.add_argument("--category", default=None)

    p_stats = sub.add_parser("stats", help="index stats + document list")
    p_stats.add_argument("--project", required=True)

    args = parser.parse_args()
    config = load_config()
    store = Store(config.pg_dsn, embedding_dimensions=config.embedding_dimensions)
    await store.connect()
    try:
        await {"index": cmd_index, "search": cmd_search, "stats": cmd_stats}[args.cmd](
            args, config, store
        )
    finally:
        await store.close()


if __name__ == "__main__":
    asyncio.run(main())
