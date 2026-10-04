"""Churn child for the kill-mid-reindex gauntlet (tests/test_store_pg.py).

Loops transactional replace_document over every doc forever, bumping a version
counter each pass; the parent test SIGKILLs this process at a random moment and
then verifies every document is complete and single-version — the M1 done-when
from DESIGN-4.0-vector-engine.md §9.

Not a pytest file (no test_ prefix). Invoked as:
    python tests/reindex_churn_child.py <dsn> <project> <n_docs> <n_chunks> <dims>
"""

import asyncio
import sys

from cognita.store import ChunkRecord, DocumentRecord, Store


def make_doc(d: int, version: int, n_chunks: int, dims: int):
    """Version-stamped doc + chunks. The parent checks atomicity through these
    stamps: content_hash carries the version, file_size the expected chunk
    count, and every chunk's content repeats the version — so a half-applied
    replace is detectable from the rows alone."""
    source = f"doc{d}.md"
    doc = DocumentRecord(
        doc_id=f"doc{d}-v{version}",  # new id per version, as a content hash would be
        source=source,
        content_hash=f"v{version}",
        file_size=n_chunks,
    )
    chunks = [
        ChunkRecord(
            chunk_id=f"doc{d}-v{version}-c{i}",
            chunk_index=i,
            content=f"{source} v{version} chunk {i}",
            embedding=[float(version)] * dims,
        )
        for i in range(n_chunks)
    ]
    return doc, chunks


async def main() -> None:
    dsn, project = sys.argv[1], sys.argv[2]
    n_docs, n_chunks, dims = int(sys.argv[3]), int(sys.argv[4]), int(sys.argv[5])
    store = Store(dsn, embedding_dimensions=dims)
    await store.connect()
    print("READY", flush=True)  # parent waits for this before starting the kill timer
    version = 1
    while True:
        for d in range(n_docs):
            doc, chunks = make_doc(d, version, n_chunks, dims)
            await store.replace_document(project, doc, chunks)
        version += 1


if __name__ == "__main__":
    asyncio.run(main())
