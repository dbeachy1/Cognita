"""PostgreSQL + pgvector store — the 4.0 vector engine (DESIGN-4.0-vector-engine.md §4–5).

One shared Postgres server; one schema per project (``"proj_<Name>"``, quoted, so
project-name case is preserved — ``proj_KEI``, exactly as in the design diagram).
Every document (re)index is a single transaction — DELETE the old row (chunks
cascade) + INSERT the document + INSERT its chunks — so a crash at any moment
leaves the previous committed version fully intact. The 3.x "poisoned residue"
class is impossible by construction, not detected-and-bounced.

M1 scope: connection/pool management, per-project schema DDL, the transactional
document replace/delete, metadata queries, and a consistency check (the 4.0
descendant of the 3.0.2 deep probe). Hybrid search lands in M2.

Embeddings ride as pgvector text literals with a ``::vector`` cast — no client-side
extension codec, no extra dependency. Note pgvector stores float32: values written
as float64 come back at float32 precision.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import datetime
from typing import Sequence

import asyncpg

from .parsing import TIER_EMBEDDED, TIER_REGISTERED
from .registry import NAME_RE
from .release_identity import DATABASE_SCHEMA_VERSION
from .assets.repository import AssetRepository

log = logging.getLogger("cognita.store")

# ---------------------------------------------------------------------------
# Schema version (13.0 §4.1)
# ---------------------------------------------------------------------------

# One singleton row in the database's own `public` schema is the ONLY schema
# version there is. The per-project `asset_schema_meta` / `asset_ocr_schema_meta`
# tables that 9.0 and 10.x used were deleted in 13.0: leaving them behind lets an
# older image run its own DO migration and stamp a version that 13.0 would then
# refuse, which is a worse failure than the one they were meant to catch.
METADATA_SCHEMA = "public"
METADATA_TABLE = "cognita_metadata"
METADATA_RELATION = f"{METADATA_SCHEMA}.{METADATA_TABLE}"

# Advisory lock guarding the one-time create-and-stamp of an empty database, so
# two containers starting together cannot both decide the database is fresh.
# The value is arbitrary but must be stable across images: it is the ASCII bytes
# of 'Cognita1' read as a bigint.
_SCHEMA_LOCK_KEY = 0x436F676E69746131

DEFAULT_RESET_TARGET = "<your target>"


def reset_command_for(target: str | None) -> str:
    """The exact command the user runs to discard and rebuild the index.

    The store does not know which deployment target it belongs to (the same
    image serves main and beta), so a caller that does know passes the rendered
    command to `Store`; otherwise the message says `<your target>` rather than
    naming the wrong one.

    Installer design 9: an install made by `./cognita install` runs as release
    target `local` (COGNITA_RELEASE_TARGET=local in its env file). Its user has
    the `cognita` launcher and no `scripts/reset_disposable_state.py` path to
    remember, so the message names the launcher command instead.

    19.6: the command name is COGNITA_COMMAND (the folders fragment passes it in), because a
    Windows install types `cognita`, not `./cognita`.  Unset means `./cognita`, the Linux clone's
    launcher.
    """
    if target == "local":
        return f"{os.environ.get('COGNITA_COMMAND') or './cognita'} reset index"
    return (
        "python3 scripts/reset_disposable_state.py --target "
        f"{target or DEFAULT_RESET_TARGET} --scope index --apply"
    )


DEFAULT_RESET_COMMAND = reset_command_for(None)


class SchemaVersionMismatch(RuntimeError):
    """The database's schema version is not the one this image understands.

    Raised by `Store.connect` BEFORE any DDL runs, and never accompanied by a
    write of any kind: 13.0 has no forward migration and no automatic reset.
    `LocalEngineHost.startup` catches it and serves with the index marked
    unavailable, so Workspace tools keep working while every index/search tool
    and Admin's add-project answer with this message.
    """


# ---------------------------------------------------------------------------
# Identifier + value helpers (pure functions — unit-tested without a server)
# ---------------------------------------------------------------------------


def schema_for(project_name: str) -> str:
    """Map a registry project name to its Postgres schema name.

    The result is only ever used as a double-quoted identifier; NAME_RE
    (letters/digits/hyphens, leading alphanumeric — the same rule the registry
    enforces) guarantees it cannot escape the quotes.
    """
    if not NAME_RE.match(project_name):
        raise ValueError(
            f"Invalid project name {project_name!r}: use letters/digits/hyphens, "
            "starting alphanumeric"
        )
    return f"proj_{project_name}"


def _quoted_schema(project_name: str) -> str:
    return f'"{schema_for(project_name)}"'


def vector_literal(embedding: Sequence[float], dimensions: int) -> str:
    """Encode an embedding as a pgvector text literal ('[1.0,2.0,...]')."""
    if len(embedding) != dimensions:
        raise ValueError(f"Embedding has {len(embedding)} dimensions, expected {dimensions}")
    return "[" + ",".join(map(str, embedding)) + "]"


def parse_vector(literal: str) -> list[float]:
    """Decode a pgvector text literal back to floats (inverse of vector_literal)."""
    return [float(x) for x in literal.strip("[]").split(",")]


def metadata_ddl() -> str:
    """DDL for the one singleton schema-version row (13.0 §4.1).

    It lives in `public`, beside the project schemas rather than inside any one
    of them, because it describes the whole database: documents, asset catalog
    and OCR share one version.
    """
    return f"""
CREATE TABLE IF NOT EXISTS {METADATA_RELATION} (
  singleton      boolean PRIMARY KEY DEFAULT true CHECK (singleton),
  schema_version integer NOT NULL CHECK (schema_version > 0),
  stamped_at     timestamptz NOT NULL DEFAULT now()
);
INSERT INTO {METADATA_RELATION} (singleton, schema_version)
  VALUES (true, {DATABASE_SCHEMA_VERSION}) ON CONFLICT (singleton) DO NOTHING;
"""


def render_ddl(project_name: str, dimensions: int) -> str:
    """Per-project schema DDL (DESIGN-4.0 §5). Idempotent — IF NOT EXISTS throughout.

    Invariants the schema itself enforces (no application code involved):
    - ON DELETE CASCADE: orphaned chunks cannot exist (era-1's 0-docs/236-chunks).
    - UNIQUE (doc_id, chunk_index): double-indexing is a constraint violation,
      not silent duplicates (the zombie incident).
    - tsv is a GENERATED column: the lexical index is maintained by the database
      on every row change, transactionally — no in-memory BM25 rebuilds.

    13.0: this is the FINAL shape, stated directly. The 4.4 `ALTER`s and the
    4.4.0 self-healing `DO` block that used to follow the CREATEs are gone —
    the one schema version in `cognita_metadata` now decides whether a database
    is usable at all, so there is nothing left for in-place migration to do.
    """
    if dimensions < 1:
        raise ValueError(f"embedding dimensions must be >= 1, got {dimensions}")
    # The bare name used to be needed separately, for the information_schema
    # lookup in the 4.4.0 tsv migration; 13.0 deleted that branch, so only the
    # quoted identifier is left.
    s = f'"{schema_for(project_name)}"'
    return f"""
CREATE SCHEMA IF NOT EXISTS {s};

-- 4.4 registered tier: `tier` and `content`. content is populated for
-- registered documents only (embedded documents keep their text in chunks).
-- documents.tsv is generated from it, so a registered document is
-- keyword-searchable the moment its row commits — the lexical half works
-- without any embedding ever being computed.
--
-- SUPERSEDED (13.0): these two columns and documents.tsv used to be declared as
-- ALTERs below the CREATE, so that a fresh install and a pre-4.4 install took
-- the same path. There is no pre-4.4 install to upgrade any more — a database
-- whose schema version is not this image's is refused, never migrated — so the
-- one definition is the CREATE itself.
CREATE TABLE IF NOT EXISTS {s}.documents (
  doc_id       text PRIMARY KEY,
  source       text NOT NULL UNIQUE,
  category     text NOT NULL DEFAULT 'general',
  format       text,
  keywords     text[],
  content_hash text NOT NULL,
  file_mtime   timestamptz,
  file_size    bigint,
  indexed_at   timestamptz NOT NULL DEFAULT now(),
  tier         text NOT NULL DEFAULT 'embedded',
  content      text,
  -- Path first (weight A), body second (weight B): for a registered document
  -- the filename is the primary lookup key, so a name match must outrank a
  -- passing mention in someone else's prose.
  --
  -- The path is indexed in THREE forms because Postgres's parser swallows paths
  -- whole, and each form answers a different way of searching for a file:
  --   raw            'worldbook/build_worldbook.py' -> one `file` token
  --                  ...matches a full-path query
  --   '/' flattened  'worldbook' + `host` 'build_worldbook.py'
  --                  ...matches a bare-filename query
  --   '/_-.' flat    'worldbook' 'build' 'worldbook' 'py'
  --                  ...matches plain words, which is how a human searches
  -- Without these, searching "build_worldbook" cannot find build_worldbook.py —
  -- the exact failure seen live on kei against 4.4.0, whose tsv indexed content
  -- ALONE. That release's self-healing DROP COLUMN migration was deleted in
  -- 13.0 along with every other migration branch; changing this expression now
  -- means bumping DATABASE_SCHEMA_VERSION and reindexing.
  tsv          tsvector GENERATED ALWAYS AS (
                 setweight(to_tsvector('english',
                   coalesce(source, '')
                   || ' ' || translate(coalesce(source, ''), '/', ' ')
                   || ' ' || translate(coalesce(source, ''), '/_-.', '    ')
                 ), 'A')
                 || setweight(to_tsvector('english', coalesce(content, '')), 'B')
               ) STORED
);

CREATE TABLE IF NOT EXISTS {s}.chunks (
  chunk_id    text PRIMARY KEY,
  doc_id      text NOT NULL REFERENCES {s}.documents(doc_id) ON DELETE CASCADE,
  chunk_index int  NOT NULL,
  content     text NOT NULL,
  section     text,
  embedding   vector({dimensions}) NOT NULL,
  tsv         tsvector GENERATED ALWAYS AS (to_tsvector('english', content)) STORED,
  UNIQUE (doc_id, chunk_index)
);

CREATE INDEX IF NOT EXISTS chunks_embedding_hnsw ON {s}.chunks
  USING hnsw (embedding vector_cosine_ops);
CREATE INDEX IF NOT EXISTS chunks_tsv_gin ON {s}.chunks USING gin (tsv);

CREATE INDEX IF NOT EXISTS documents_tsv_gin ON {s}.documents USING gin (tsv);
CREATE INDEX IF NOT EXISTS documents_tier ON {s}.documents (tier);
"""


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class DocumentRecord:
    """One row of <schema>.documents — the metadata that replaces index_metadata.json."""

    doc_id: str
    source: str  # path relative to documents_dir
    category: str = "general"
    format: str | None = None
    keywords: list[str] = field(default_factory=list)
    content_hash: str = ""
    file_mtime: datetime | None = None  # tz-aware (timestamptz)
    file_size: int | None = None
    indexed_at: datetime | None = None  # set by the database; ignored on insert
    tier: str = TIER_EMBEDDED
    # Whole-document text — registered tier ONLY (it has no chunks to hold it).
    # None for embedded documents, whose text lives in chunks.content.
    content: str | None = None

    @property
    def is_registered(self) -> bool:
        return self.tier == TIER_REGISTERED


@dataclass(slots=True)
class ChunkRecord:
    """One row of <schema>.chunks. doc_id comes from the owning DocumentRecord."""

    chunk_id: str
    chunk_index: int
    content: str
    embedding: list[float]
    section: str | None = None


@dataclass(slots=True)
class StoreStats:
    """Index totals. The per-tier splits exist because a merged count hides
    whether the tiers are behaving (D4.4-7): chunks/vectors belong to the
    embedded tier alone, so `documents` and `chunks` are not comparable."""

    documents: int
    chunks: int
    embedded_documents: int = 0
    registered_documents: int = 0


@dataclass(slots=True)
class ChunkHit:
    """One chunk returned by dense_search/lexical_search, document metadata joined in.

    score is engine-native: cosine distance for dense (lower = closer),
    ts_rank_cd for lexical (higher = better). The retrieval core fuses by RANK
    (RRF), so the two scales never need reconciling.
    """

    chunk_id: str
    doc_id: str
    chunk_index: int
    content: str
    section: str | None
    source: str
    category: str
    keywords: list[str]
    score: float


@dataclass(slots=True)
class SourceInfo:
    """Per-source index metadata for smart-reindex change detection."""

    doc_id: str
    content_hash: str
    file_mtime: datetime | None
    file_size: int | None
    # The tier the row was written at. Change detection compares it against the
    # tier the CURRENT policy assigns, so an extension moving between tiers (a
    # config edit, or the 4.3 → 4.4 upgrade demoting code to registered) forces
    # a reindex even though the file on disk never changed.
    tier: str = TIER_EMBEDDED
    # The stored category, carried so a rewrite can PRESERVE it. Re-deriving it
    # from the path on every write reset every explicitly-chosen category to
    # "general" (detect_category's fallback when no mapping matches).
    category: str = "general"


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


class Store:
    """asyncpg-backed store. connect() once; all methods are pool-based and safe
    to call concurrently (Postgres serializes writers — worst case a lock wait,
    never a corrupt file)."""

    def __init__(
        self,
        dsn: str,
        *,
        embedding_dimensions: int = 1024,
        reset_command: str | None = None,
    ):
        if embedding_dimensions < 1:
            raise ValueError(f"embedding dimensions must be >= 1, got {embedding_dimensions}")
        self._dsn = dsn
        self._dims = embedding_dimensions
        self._pool: asyncpg.Pool | None = None
        # Printed verbatim in a version-mismatch message. The store cannot know
        # the deployment target, so a caller that does supplies the rendered
        # command (see reset_command_for).
        self._reset_command = reset_command or DEFAULT_RESET_COMMAND
        # Set by connect() when the database's schema version is not ours, and
        # the single source of truth for "the index is unavailable". It is a
        # STICKY refusal: nothing in this process clears it, because nothing in
        # this process is allowed to change the database to make it true again.
        self.schema_error: SchemaVersionMismatch | None = None

    # ---------- lifecycle ----------

    @property
    def pool(self) -> asyncpg.Pool:
        if self.schema_error is not None:
            raise self.schema_error
        if self._pool is None:
            raise RuntimeError("Store is not connected — call connect() first")
        return self._pool

    async def connect(self) -> None:
        """Check the database's schema version, then create the pool.

        The version check runs BEFORE any DDL — including the vector extension
        below — because a database this image does not understand must be left
        exactly as it was found (13.0 §4.1). On a mismatch this raises
        SchemaVersionMismatch and no pool exists.

        Ubuntu's pgvector package is NOT a trusted extension, so actually
        creating it needs superuser — that happens once at bootstrap
        (DESIGN-4.0 §8). For the app role, IF NOT EXISTS then reduces to a
        harmless notice; it only truly creates on dev/test databases where the
        connecting role is a superuser (e.g. the pg18 docker image).
        """
        conn = await asyncpg.connect(self._dsn)
        try:
            await self._check_schema_version(conn)
            await conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
        finally:
            await conn.close()
        self._pool = await asyncpg.create_pool(self._dsn, min_size=1, max_size=5)

    # ---------- schema version (13.0 §4.1) ----------

    async def _check_schema_version(self, conn: asyncpg.Connection) -> None:
        """Compare the stored schema version with this image's, and act on it.

        Fresh (no marker AND no project schema) -> create the marker and stamp
        it, in one transaction under an advisory lock. Equal -> proceed. Newer,
        older, or unversioned-on-a-populated-database -> raise, having changed
        nothing. There is deliberately no fourth outcome: 13.0 has no forward
        migration, and a database that is not this shape is reset by the user,
        explicitly, with the command in the message.
        """
        async with conn.transaction():
            # Two containers can start at the same moment and both see an empty
            # database. The lock makes the read-then-stamp below one decision;
            # it is held to the end of this transaction and released with it.
            await conn.execute("SELECT pg_advisory_xact_lock($1)", _SCHEMA_LOCK_KEY)
            found = await self._read_schema_version(conn)
            if found == DATABASE_SCHEMA_VERSION:
                log.info(
                    "Database schema version %s matches this image (%s) — proceeding",
                    found, DATABASE_SCHEMA_VERSION,
                )
                return
            if found is not None:
                self._refuse(found, reason="stored version differs from this image")
            # No usable version. The database is only FRESH if it is also empty:
            # a missing or damaged marker on a populated database is the 12.x
            # upgrade case and must never be mistaken for an empty one, because
            # treating it as fresh would stamp this version onto data that is
            # not in this shape.
            populated = await self._project_schemas(conn)
            if populated:
                log.error(
                    "Database has %d project schema(s) (%s) but no usable %s row",
                    len(populated), ", ".join(populated[:5]), METADATA_RELATION,
                )
                self._refuse(None, reason="populated database with no version marker")
            await conn.execute(metadata_ddl())
            log.info(
                "Fresh database (no %s, no project schemas) — created it and "
                "stamped schema version %s",
                METADATA_RELATION, DATABASE_SCHEMA_VERSION,
            )

    @staticmethod
    async def _read_schema_version(conn: asyncpg.Connection) -> int | None:
        """The stamped version, or None if the table or its row is absent.

        A present-but-empty table reads as None on purpose: an interrupted
        stamp leaves exactly that, and it is not evidence of any version.
        """
        if await conn.fetchval("SELECT to_regclass($1)", METADATA_RELATION) is None:
            return None
        return await conn.fetchval(
            f"SELECT schema_version FROM {METADATA_RELATION} WHERE singleton"
        )

    @staticmethod
    async def _project_schemas(conn: asyncpg.Connection) -> list[str]:
        """Schema names that `schema_for` could have produced.

        `public` and the `pg_*` catalogs are not project schemas and do not make
        a database populated; neither does `information_schema`. Matching the
        `proj_` prefix rather than "anything unexpected" keeps an unrelated
        schema in the same database from wedging Cognita. The backslash escapes
        the underscore, which is a LIKE wildcard.
        """
        rows = await conn.fetch(
            r"SELECT nspname FROM pg_namespace WHERE nspname LIKE 'proj\_%' ORDER BY nspname"
        )
        return [r["nspname"] for r in rows]

    def _refuse(self, found: int | None, *, reason: str) -> None:
        """Record and raise the mismatch. Always raises; never runs DDL."""
        message = self._mismatch_message(found)
        self.schema_error = SchemaVersionMismatch(message)
        log.error("Refusing to use this database (%s): %s", reason, message)
        raise self.schema_error

    def _mismatch_message(self, found: int | None) -> str:
        """The exact wording of 13.0 §4.1, with the reset command spelled out.

        The unversioned case has no number to report, so it names the condition
        instead; every case ends with the command that fixes it, because a
        warning the user cannot act on is the same as no warning.
        """
        if found is None:
            return (
                "Database schema is unversioned on a populated database; this image "
                f"supports schema {DATABASE_SCHEMA_VERSION}. Nothing was changed. "
                f"To reset the index: {self._reset_command}"
            )
        relation = "newer" if found > DATABASE_SCHEMA_VERSION else "older"
        return (
            f"Database schema {found} is {relation} than this image supports "
            f"({DATABASE_SCHEMA_VERSION}). Nothing was changed. "
            f"To reset the index: {self._reset_command}"
        )

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    async def ping(self) -> bool:
        """The 4.0 deep probe, degenerated to what it should be: trivial."""
        return await self.pool.fetchval("SELECT 1") == 1

    # ---------- schema management ----------

    async def ensure_project(self, project: str) -> None:
        """Create the project's schema/tables/indexes if absent. Idempotent.

        Refuses outright while the schema version is wrong. This is the guard
        Admin's add-project hits (admin_api calls this directly rather than
        going through the engine's tool dispatch), and it is DDL — exactly what
        a mismatched database must never receive.
        """
        if self.schema_error is not None:
            log.error("Refusing ensure_project(%s): %s", project, self.schema_error)
            raise self.schema_error
        await self.pool.execute(render_ddl(project, self._dims))
        await AssetRepository(self.pool, project, dimensions=self._dims).ensure_schema()

    async def drop_project(self, project: str) -> None:
        """Drop the project's schema and everything in it."""
        await self.pool.execute(f"DROP SCHEMA IF EXISTS {_quoted_schema(project)} CASCADE")

    async def has_project(self, project: str) -> bool:
        return await self.pool.fetchval(
            "SELECT EXISTS(SELECT 1 FROM pg_namespace WHERE nspname = $1)",
            schema_for(project),
        )

    # ---------- the transactional reindex (the point of 4.0) ----------

    async def replace_document(
        self, project: str, doc: DocumentRecord, chunks: Sequence[ChunkRecord]
    ) -> None:
        """Atomically replace a document and all its chunks.

        BEGIN → DELETE old rows → INSERT document → INSERT chunks → COMMIT.
        A crash/kill at any point rolls back to the previous committed version;
        a half-applied state cannot exist. Deleting by source OR doc_id also
        retires a moved file's stale row (same content hash at a new path).

        Both tiers go through this ONE transaction (CLAUDE.md: never split a
        per-document index). A registered document simply commits with zero
        chunks and its text in documents.content — which is also what makes a
        tier CROSSING safe: the DELETE drops the old row, chunks cascade, and
        the new tier's shape lands in the same transaction. No orphaned vectors
        can survive a .md → .py rename.
        """
        s = _quoted_schema(project)
        if not chunks and not doc.is_registered:
            # An embedded document must have chunks — check_consistency treats a
            # chunkless embedded document as a violation. Empty-parse policy is
            # the pipeline's call (M2), not the store's.
            raise ValueError(f"Refusing to index {doc.source!r} with zero chunks")
        if chunks and doc.is_registered:
            raise ValueError(
                f"Refusing to index registered document {doc.source!r} with "
                f"{len(chunks)} chunks — the registered tier is never embedded"
            )
        chunk_rows = [
            (
                c.chunk_id,
                doc.doc_id,
                c.chunk_index,
                c.content,
                c.section,
                vector_literal(c.embedding, self._dims),
            )
            for c in chunks
        ]
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    f"DELETE FROM {s}.documents WHERE source = $1 OR doc_id = $2",
                    doc.source,
                    doc.doc_id,
                )
                await conn.execute(
                    f"""INSERT INTO {s}.documents
                        (doc_id, source, category, format, keywords,
                         content_hash, file_mtime, file_size, tier, content)
                        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)""",
                    doc.doc_id,
                    doc.source,
                    doc.category,
                    doc.format,
                    doc.keywords,
                    doc.content_hash,
                    doc.file_mtime,
                    doc.file_size,
                    doc.tier,
                    doc.content if doc.is_registered else None,
                )
                await conn.executemany(
                    f"""INSERT INTO {s}.chunks
                        (chunk_id, doc_id, chunk_index, content, section, embedding)
                        VALUES ($1, $2, $3, $4, $5, $6::vector)""",
                    chunk_rows,
                )

    async def delete_document(self, project: str, source: str) -> bool:
        """Delete a document (chunks cascade). Returns False if it wasn't there."""
        status = await self.pool.execute(
            f"DELETE FROM {_quoted_schema(project)}.documents WHERE source = $1", source
        )
        return status != "DELETE 0"

    async def move_document(
        self, project: str, old_source: str, new_source: str, new_doc_id: str
    ) -> int | None:
        """Relocate a document to new_source WITHOUT re-embedding (4.1 move_document).

        The content is unchanged on a rename/move, so this rewrites the document
        row's source + content-addressed doc_id and re-keys its chunks in one
        transaction — embeddings and the generated tsv are untouched. Returns the
        number of chunks moved, or None if old_source isn't indexed. Raises
        ValueError if new_source is already occupied by a different document.

        FK-safe ordering (chunks.doc_id REFERENCES documents.doc_id, immediate):
        INSERT the new doc row first, repoint the chunks at it, then DELETE the old
        row (now chunkless).
        """
        s = _quoted_schema(project)
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                old = await conn.fetchrow(
                    f"SELECT * FROM {s}.documents WHERE source = $1 FOR UPDATE", old_source
                )
                if old is None:
                    return None
                if await conn.fetchval(
                    f"SELECT 1 FROM {s}.documents WHERE source = $1", new_source
                ):
                    raise ValueError(f"destination already indexed: {new_source!r}")
                await conn.execute(
                    f"""INSERT INTO {s}.documents
                        (doc_id, source, category, format, keywords, content_hash,
                         file_mtime, file_size, indexed_at, tier, content)
                        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)""",
                    new_doc_id, new_source, old["category"], old["format"],
                    old["keywords"], old["content_hash"], old["file_mtime"],
                    old["file_size"], old["indexed_at"], old["tier"], old["content"],
                )
                moved = await conn.execute(
                    f"""UPDATE {s}.chunks
                        SET doc_id = $1, chunk_id = $1 || '_' || chunk_index
                        WHERE doc_id = $2""",
                    new_doc_id, old["doc_id"],
                )
                await conn.execute(
                    f"DELETE FROM {s}.documents WHERE doc_id = $1", old["doc_id"]
                )
                return int(moved.removeprefix("UPDATE "))

    # ---------- metadata queries ----------

    async def get_document(self, project: str, source: str) -> DocumentRecord | None:
        row = await self.pool.fetchrow(
            f"SELECT * FROM {_quoted_schema(project)}.documents WHERE source = $1", source
        )
        return self._doc_from_row(row) if row else None

    # Every column except `content` — a listing must not drag the full text of
    # every registered document out of the database.
    _DOC_COLUMNS = """doc_id, source, category, format, keywords, content_hash,
                      file_mtime, file_size, indexed_at, tier"""

    async def list_documents(self, project: str) -> list[DocumentRecord]:
        rows = await self.pool.fetch(
            f"""SELECT {self._DOC_COLUMNS} FROM {_quoted_schema(project)}.documents
                ORDER BY source"""
        )
        return [self._doc_from_row(r) for r in rows]

    async def get_chunks(self, project: str, source: str) -> list[ChunkRecord]:
        rows = await self.pool.fetch(
            f"""SELECT c.chunk_id, c.chunk_index, c.content, c.section,
                       c.embedding::text AS embedding
                FROM {_quoted_schema(project)}.chunks c
                JOIN {_quoted_schema(project)}.documents d USING (doc_id)
                WHERE d.source = $1
                ORDER BY c.chunk_index""",
            source,
        )
        return [
            ChunkRecord(
                chunk_id=r["chunk_id"],
                chunk_index=r["chunk_index"],
                content=r["content"],
                embedding=parse_vector(r["embedding"]),
                section=r["section"],
            )
            for r in rows
        ]

    async def category_counts(self, project: str) -> dict[str, int]:
        rows = await self.pool.fetch(
            f"""SELECT category, count(*) AS n
                FROM {_quoted_schema(project)}.documents GROUP BY category ORDER BY category"""
        )
        return {r["category"]: r["n"] for r in rows}

    async def chunk_counts(self, project: str) -> dict[str, int]:
        """chunk count per doc_id (for the 3.x list_documents entry shape)."""
        rows = await self.pool.fetch(
            f"SELECT doc_id, count(*) AS n FROM {_quoted_schema(project)}.chunks GROUP BY doc_id"
        )
        return {r["doc_id"]: r["n"] for r in rows}

    async def chunk_count(self, project: str, source: str) -> int:
        return await self.pool.fetchval(
            f"""SELECT count(*) FROM {_quoted_schema(project)}.chunks c
                JOIN {_quoted_schema(project)}.documents d USING (doc_id)
                WHERE d.source = $1""",
            source,
        )

    async def first_chunk_embedding(self, project: str, source: str) -> list[float] | None:
        """The document's chunk-0 embedding (search_similar's reference vector)."""
        lit = await self.pool.fetchval(
            f"""SELECT c.embedding::text FROM {_quoted_schema(project)}.chunks c
                JOIN {_quoted_schema(project)}.documents d USING (doc_id)
                WHERE d.source = $1 ORDER BY c.chunk_index LIMIT 1""",
            source,
        )
        return parse_vector(lit) if lit else None

    async def stats(self, project: str) -> StoreStats:
        s = _quoted_schema(project)
        row = await self.pool.fetchrow(
            f"""SELECT (SELECT count(*) FROM {s}.documents) AS documents,
                       (SELECT count(*) FROM {s}.chunks)    AS chunks,
                       (SELECT count(*) FROM {s}.documents
                         WHERE tier = $1)                   AS embedded,
                       (SELECT count(*) FROM {s}.documents
                         WHERE tier = $2)                   AS registered""",
            TIER_EMBEDDED,
            TIER_REGISTERED,
        )
        return StoreStats(
            documents=row["documents"],
            chunks=row["chunks"],
            embedded_documents=row["embedded"],
            registered_documents=row["registered"],
        )

    # ---------- search (M2: the two legs the retrieval core fuses) ----------

    _HIT_COLUMNS = """c.chunk_id, c.doc_id, c.chunk_index, c.content, c.section,
                      d.source, d.category, d.keywords"""

    async def dense_search(
        self, project: str, embedding: Sequence[float], limit: int, category: str | None = None,
        *, exclude_source: str | None = None, one_per_source: bool = False,
    ) -> list[ChunkHit]:
        """Nearest chunks by cosine distance (pgvector HNSW).

        `exclude_source` and `one_per_source` exist for search_similar, which
        wants the nearest DOCUMENTS rather than the nearest chunks. Doing that
        filtering in Python after the LIMIT meant the reference document's own
        chunks — at distance 0, so always first — consumed the budget: a
        20-chunk document returned every one of its own chunks, all of them were
        discarded, and the tool answered "No similar documents found" while real
        neighbors sat in the index. Pushing both rules into SQL makes the LIMIT
        count documents, which is what the caller asked for.

        search_knowledge does NOT pass either flag: it fuses chunk-level hits by
        rank (RRF), so collapsing to one chunk per document there would change
        ranking semantics.
        """
        s = _quoted_schema(project)
        vec = vector_literal(embedding, self._dims)
        conds, params = [], []
        if category:
            params.append(category)
            conds.append(f"d.category = ${len(params) + 2}")
        if exclude_source is not None:
            params.append(exclude_source)
            conds.append(f"d.source <> ${len(params) + 2}")
        where_sql = ("WHERE " + " AND ".join(conds)) if conds else ""
        if one_per_source:
            # DISTINCT ON keeps the best-scoring chunk per document; the outer
            # ORDER BY then ranks those documents and the LIMIT counts them.
            sql = f"""SELECT * FROM (
                          SELECT DISTINCT ON (d.source) {self._HIT_COLUMNS},
                                 c.embedding <=> $1::vector AS score
                          FROM {s}.chunks c JOIN {s}.documents d USING (doc_id)
                          {where_sql}
                          ORDER BY d.source, c.embedding <=> $1::vector
                      ) best
                      ORDER BY best.score
                      LIMIT $2"""
        else:
            sql = f"""SELECT {self._HIT_COLUMNS},
                             c.embedding <=> $1::vector AS score
                      FROM {s}.chunks c JOIN {s}.documents d USING (doc_id)
                      {where_sql}
                      ORDER BY c.embedding <=> $1::vector
                      LIMIT $2"""
        if not conds:
            rows = await self.pool.fetch(sql, vec, limit, *params)
            return [self._hit_from_row(r) for r in rows]
        # A FILTERED vector search needs iterative scan, or it silently returns
        # too few rows — often zero. An HNSW index scan surfaces `hnsw.ef_search`
        # candidates (default 40) and the WHERE is applied to the JOIN output
        # AFTERWARDS, so if none of the 40 nearest chunks happen to be in the
        # requested category, the answer is empty while the category is full.
        #
        # Measured on 2026-08-29: a filtered category search against an index
        # with 11871 chunks and 73 documents in that category planned
        # `Index Scan using chunks_embedding_hnsw ... rows=40` feeding a join that
        # produced rows=0 for LIMIT 5. Zero results, no error, 73 documents right
        # there. With these two settings the same query returns its 5 rows.
        #
        # strict_order, not relaxed_order: the dense leg's ordering feeds RRF, so
        # exact distance order is worth the small extra cost. Requires pgvector
        # 0.8+ (kei runs 0.8.1); SET LOCAL is scoped to the transaction, so it
        # cannot leak onto a pooled connection's later queries.
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute("SET LOCAL hnsw.iterative_scan = strict_order")
                # ef_search must be at least the limit to be meaningful at all.
                await conn.execute(
                    f"SET LOCAL hnsw.ef_search = {max(40, min(int(limit) * 8, 1000))}"
                )
                rows = await conn.fetch(sql, vec, limit, *params)
        return [self._hit_from_row(r) for r in rows]

    async def lexical_search(
        self, project: str, query: str, limit: int, category: str | None = None
    ) -> list[ChunkHit]:
        """Full-text hits ranked by ts_rank_cd. websearch_to_tsquery tolerates
        arbitrary user input (no tsquery syntax errors), replacing 3.x's
        in-memory BM25 (D4.7)."""
        s = _quoted_schema(project)
        filter_sql = "AND d.category = $3" if category else ""
        rows = await self.pool.fetch(
            f"""SELECT {self._HIT_COLUMNS},
                       ts_rank_cd(c.tsv, q)::float8 AS score
                FROM {s}.chunks c JOIN {s}.documents d USING (doc_id),
                     websearch_to_tsquery('english', $1) q
                WHERE c.tsv @@ q {filter_sql}
                ORDER BY score DESC, c.chunk_id
                LIMIT $2""",
            query,
            limit,
            *((category,) if category else ()),
        )
        return [self._hit_from_row(r) for r in rows]

    async def registered_lexical_search(
        self, project: str, query: str, limit: int, category: str | None = None
    ) -> list[ChunkHit]:
        """Full-text hits over REGISTERED documents (D4.4-5).

        Same ts_rank_cd / websearch_to_tsquery as lexical_search, so the two
        legs' scores are directly comparable and the retrieval core can merge
        them into one keyword ranking before RRF. There is deliberately no
        dense counterpart: this tier is keyword-only by design, at every value
        of hybrid_alpha.

        Registered documents have no chunks, so the synthesized hit carries the
        WHOLE document text at chunk_index 0; the core excerpts it around the
        match before it reaches a caller.
        """
        s = _quoted_schema(project)
        filter_sql = "AND d.category = $4" if category else ""
        rows = await self.pool.fetch(
            f"""SELECT d.doc_id || '_r0' AS chunk_id, d.doc_id, 0 AS chunk_index,
                       coalesce(d.content, '') AS content, NULL::text AS section,
                       d.source, d.category, d.keywords,
                       ts_rank_cd(d.tsv, q)::float8 AS score
                FROM {s}.documents d, websearch_to_tsquery('english', $1) q
                WHERE d.tier = $3 AND d.tsv @@ q {filter_sql}
                ORDER BY score DESC, d.source
                LIMIT $2""",
            query,
            limit,
            TIER_REGISTERED,
            *((category,) if category else ()),
        )
        return [self._hit_from_row(r) for r in rows]

    async def adjacent_chunks(
        self, project: str, wanted: Sequence[tuple[str, int]]
    ) -> dict[tuple[str, int], str]:
        """Batch-fetch chunk contents by (doc_id, chunk_index) — context expansion."""
        if not wanted:
            return {}
        s = _quoted_schema(project)
        rows = await self.pool.fetch(
            f"""SELECT c.doc_id, c.chunk_index, c.content
                FROM {s}.chunks c
                JOIN unnest($1::text[], $2::int[]) AS t(doc_id, chunk_index)
                  ON c.doc_id = t.doc_id AND c.chunk_index = t.chunk_index""",
            [w[0] for w in wanted],
            [w[1] for w in wanted],
        )
        return {(r["doc_id"], r["chunk_index"]): r["content"] for r in rows}

    # ---------- smart-reindex support ----------

    async def list_sources(self, project: str) -> dict[str, SourceInfo]:
        rows = await self.pool.fetch(
            f"""SELECT source, doc_id, content_hash, file_mtime, file_size, tier, category
                FROM {_quoted_schema(project)}.documents"""
        )
        return {
            r["source"]: SourceInfo(
                doc_id=r["doc_id"],
                content_hash=r["content_hash"],
                file_mtime=r["file_mtime"],
                file_size=r["file_size"],
                tier=r["tier"] or TIER_EMBEDDED,
                category=r["category"] or "general",
            )
            for r in rows
        }

    async def touch_document(
        self, project: str, source: str, file_mtime: datetime | None, file_size: int | None
    ) -> None:
        """Refresh mtime/size for an unchanged document (content identical, file
        merely touched) so the next smart reindex can skip it without parsing."""
        await self.pool.execute(
            f"""UPDATE {_quoted_schema(project)}.documents
                SET file_mtime = $2, file_size = $3 WHERE source = $1""",
            source,
            file_mtime,
            file_size,
        )

    async def delete_documents_not_in(self, project: str, live_sources: Sequence[str]) -> int:
        """Remove rows for files that vanished from disk. Returns count removed."""
        status = await self.pool.execute(
            f"DELETE FROM {_quoted_schema(project)}.documents WHERE source <> ALL($1::text[])",
            list(live_sources),
        )
        return int(status.removeprefix("DELETE "))

    async def delete_all_documents(self, project: str) -> int:
        """Remove every document row in the project. Returns count removed.

        Split out from delete_documents_not_in (5.7) rather than passing it an
        empty list: `source <> ALL('{}')` is vacuously TRUE, so the two are the
        same DELETE, but one of them says so. The caller that wants this — an
        index_project walk where every file found was deliberately excluded — has
        to be distinguishable from the one that must never reach it, a walk that
        found nothing because the documents_dir is unmounted.
        """
        status = await self.pool.execute(
            f"DELETE FROM {_quoted_schema(project)}.documents"
        )
        return int(status.removeprefix("DELETE "))

    # ---------- consistency ----------

    async def check_consistency(self, project: str) -> list[str]:
        """Return human-readable invariant violations (empty list = healthy).

        Orphaned chunks and duplicate chunk indexes are impossible by schema
        (FK CASCADE, UNIQUE) — what's left to check is chunkless documents and
        non-contiguous chunk numbering, either of which would mean a write
        escaped its transaction.

        Tier-aware since 4.4: a chunkless REGISTERED document is the normal,
        correct state, so the check inverts for that tier — there it is chunks
        (leftover vectors from a tier crossing) that signal a bad write.
        """
        s = _quoted_schema(project)
        violations: list[str] = []
        for r in await self.pool.fetch(
            f"""SELECT d.doc_id, d.source FROM {s}.documents d
                WHERE d.tier = $1
                  AND NOT EXISTS (SELECT 1 FROM {s}.chunks c WHERE c.doc_id = d.doc_id)""",
            TIER_EMBEDDED,
        ):
            violations.append(f"document {r['source']!r} ({r['doc_id']}) has no chunks")
        for r in await self.pool.fetch(
            f"""SELECT d.doc_id, d.source, count(c.chunk_id) AS n
                FROM {s}.documents d JOIN {s}.chunks c USING (doc_id)
                WHERE d.tier = $1 GROUP BY d.doc_id, d.source""",
            TIER_REGISTERED,
        ):
            violations.append(
                f"registered document {r['source']!r} ({r['doc_id']}) has "
                f"{r['n']} chunks — the registered tier is never embedded"
            )
        for r in await self.pool.fetch(
            f"""SELECT doc_id, count(*) AS n, min(chunk_index) AS lo, max(chunk_index) AS hi
                FROM {s}.chunks GROUP BY doc_id
                HAVING min(chunk_index) <> 0 OR max(chunk_index) <> count(*) - 1"""
        ):
            violations.append(
                f"doc_id {r['doc_id']}: {r['n']} chunks spanning indexes "
                f"{r['lo']}..{r['hi']} (expected 0..{r['n'] - 1})"
            )
        return violations

    # ---------- internals ----------

    @staticmethod
    def _hit_from_row(row: asyncpg.Record) -> ChunkHit:
        return ChunkHit(
            chunk_id=row["chunk_id"],
            doc_id=row["doc_id"],
            chunk_index=row["chunk_index"],
            content=row["content"],
            section=row["section"],
            source=row["source"],
            category=row["category"],
            keywords=list(row["keywords"] or []),
            score=float(row["score"]),
        )

    @staticmethod
    def _doc_from_row(row: asyncpg.Record) -> DocumentRecord:
        keys = row.keys()
        return DocumentRecord(
            doc_id=row["doc_id"],
            source=row["source"],
            category=row["category"],
            format=row["format"],
            keywords=list(row["keywords"] or []),
            content_hash=row["content_hash"],
            file_mtime=row["file_mtime"],
            file_size=row["file_size"],
            indexed_at=row["indexed_at"],
            tier=(row["tier"] if "tier" in keys else TIER_EMBEDDED) or TIER_EMBEDDED,
            content=row["content"] if "content" in keys else None,
        )
