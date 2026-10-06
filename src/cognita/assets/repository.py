"""PostgreSQL catalog for assets; file publication is intentionally elsewhere."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

from ..registry import NAME_RE
from .models import LEGACY_CONNECTOR_ID, AssetRecord, normalize_connector_id
from .ocr_store import (
    OcrResultFacts,
    OcrSearchChunk,
    OcrSourceSnapshot,
    OcrStore,
)

# SUPERSEDED (13.0 §4.1): ASSET_SCHEMA_VERSION and the per-project
# `asset_schema_meta` table lived here from 7.1 to 12.x. The whole Cognita
# PostgreSQL schema — documents, asset catalog and OCR — now has ONE version,
# `release_identity.DATABASE_SCHEMA_VERSION`, stamped once in
# `public.cognita_metadata` and checked by `store.Store.connect` before any DDL
# runs. The per-project tables were not merely redundant: an older image could
# still find them, run its own DO migration and stamp a version that the newer
# image would then refuse.


def schema_for(project: str) -> str:
    if not isinstance(project, str) or not NAME_RE.match(project):
        raise ValueError("invalid project name")
    return "proj_" + project


def asset_ddl(project: str, dimensions: int = 1024) -> str:
    schema = '"' + schema_for(project).replace('"', '""') + '"'
    return f"""
CREATE SCHEMA IF NOT EXISTS {schema};
CREATE TABLE IF NOT EXISTS {schema}.assets (
 asset_id text PRIMARY KEY, source text NOT NULL UNIQUE,
 media_type text NOT NULL CHECK (media_type = 'image/png'),
 width integer NOT NULL CHECK (width > 0), height integer NOT NULL CHECK (height > 0),
 file_size bigint NOT NULL CHECK (file_size > 0), file_sha256 text NOT NULL CHECK (length(file_sha256)=64),
 received_sha256 text NOT NULL CHECK (length(received_sha256)=64), file_mtime timestamptz,
 metadata jsonb NOT NULL, metadata_storage text NOT NULL CHECK (metadata_storage IN ('embedded','catalog')),
 metadata_revision bigint NOT NULL CHECK (metadata_revision > 0),
 provenance_state text NOT NULL CHECK (provenance_state IN ('none','cabx_present_unverified')),
 cabx_chunk_count integer NOT NULL DEFAULT 0 CHECK (cabx_chunk_count >= 0),
 embedded_metadata boolean NOT NULL, indexed_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS assets_source_order ON {schema}.assets ((lower(source)), source);
CREATE INDEX IF NOT EXISTS assets_metadata_tags_gin ON {schema}.assets USING gin ((metadata -> 'tags'));
CREATE TABLE IF NOT EXISTS {schema}.asset_chunks (
 chunk_id text PRIMARY KEY,
 asset_id text NOT NULL REFERENCES {schema}.assets(asset_id) ON DELETE CASCADE,
 chunk_index integer NOT NULL, content text NOT NULL, embedding vector({dimensions}) NOT NULL,
 tsv tsvector GENERATED ALWAYS AS (to_tsvector('english', content)) STORED,
 UNIQUE (asset_id, chunk_index)
);
CREATE INDEX IF NOT EXISTS asset_chunks_embedding_hnsw ON {schema}.asset_chunks USING hnsw (embedding vector_cosine_ops);
CREATE INDEX IF NOT EXISTS asset_chunks_tsv_gin ON {schema}.asset_chunks USING gin (tsv);
-- 9.0 expanded the durable identity to connector + operation type + operation
-- ID; 7.1/8.x rows had no connector binding and were moved into a reserved
-- legacy namespace ({LEGACY_CONNECTOR_ID}) by a DO migration keyed on
-- asset_schema_meta. 13.0 deleted that migration with the meta table: a
-- database from before 9.0 is refused by the schema-version check and reset,
-- never rewritten in place. The connector column is simply part of the shape.
CREATE TABLE IF NOT EXISTS {schema}.asset_operations (
 connector_id text NOT NULL, operation_id text NOT NULL, tool text NOT NULL,
 request_sha256 text NOT NULL CHECK (length(request_sha256)=64),
 state text NOT NULL CHECK (state IN ('running','committed','failed')), result jsonb,
 created_at timestamptz NOT NULL DEFAULT now(), completed_at timestamptz,
 PRIMARY KEY (connector_id, tool, operation_id)
);
CREATE INDEX IF NOT EXISTS asset_operations_created ON {schema}.asset_operations (created_at);
"""


class AssetRepository:
    """SQL adapter with all asset/catalog changes kept in explicit transactions."""

    def __init__(self, pool: Any, project: str, *, dimensions: int = 1024):
        self.pool = pool
        self.project = project
        self.schema = '"' + schema_for(project).replace('"', '""') + '"'
        self.dimensions = dimensions
        self.ocr = OcrStore(pool, project, dimensions=dimensions)

    @staticmethod
    def _columns(alias: str = "") -> str:
        prefix = f"{alias}." if alias else ""
        names = (
            "asset_id", "source", "media_type", "width", "height", "file_size",
            "file_sha256", "received_sha256", "file_mtime", "metadata",
            "metadata_storage", "metadata_revision", "provenance_state",
            "cabx_chunk_count", "embedded_metadata", "indexed_at",
        )
        columns = ",".join(prefix + name for name in names)
        return (
            f"{columns},{prefix}file_size AS received_size,"
            f"{prefix}file_size AS final_size,{prefix}file_sha256 AS final_sha256,"
            f"{prefix}embedded_metadata AS embedded_metadata_present,true AS indexed"
        )

    async def ensure_schema(self) -> None:
        """Create the catalog and OCR tables if absent. Idempotent.

        13.0: no per-project version read afterwards. The one schema version is
        checked once, in `store.Store.connect`, before any of this DDL can run.
        """
        async with self.pool.acquire() as conn, conn.transaction():
            await conn.execute(asset_ddl(self.project, self.dimensions))
            await self.ocr._ensure_schema_on_connection(conn)

    async def get(self, filepath: str) -> dict[str, Any] | None:
        return await self.pool.fetchrow(
            f"SELECT {self._columns()} FROM {self.schema}.assets WHERE source=$1", filepath
        )

    async def replace_asset(
        self, record: AssetRecord, projection: str, vectors: Iterable[Iterable[float]],
        *, connection: Any | None = None, searchable: bool = True,
    ) -> None:
        vector_list = list(vectors)
        if searchable and not vector_list:
            raise ValueError("an indexed asset requires an embedding")
        if connection is not None:
            await self._replace(connection, record, projection, vector_list if searchable else [])
            return
        async with self.pool.acquire() as conn, conn.transaction():
            await self._replace(conn, record, projection, vector_list if searchable else [])

    async def _replace(
        self, conn: Any, record: AssetRecord, projection: str, vectors: list[Iterable[float]]
    ) -> None:
        await conn.execute(
            f"""DELETE FROM {self.schema}.asset_ocr_sources
                WHERE filepath=$1 OR filepath IN (
                    SELECT source FROM {self.schema}.assets WHERE asset_id=$2
                )""",
            record.filepath, record.asset_id,
        )
        await conn.execute(
            f"DELETE FROM {self.schema}.assets WHERE source=$1 OR asset_id=$2",
            record.filepath, record.asset_id,
        )
        await conn.execute(
            f"""INSERT INTO {self.schema}.assets
                (asset_id,source,media_type,width,height,file_size,file_sha256,
                 received_sha256,file_mtime,metadata,metadata_storage,metadata_revision,
                 provenance_state,cabx_chunk_count,embedded_metadata,indexed_at)
                VALUES ($1,$2,'image/png',$3,$4,$5,$6,$7,$8,$9::jsonb,$10,$11,$12,$13,$14,now())""",
            record.asset_id, record.filepath, record.width, record.height,
            record.final_size, record.final_sha256, record.received_sha256,
            record.file_mtime, json.dumps(record.metadata, ensure_ascii=False),
            record.metadata_storage, record.metadata_revision, record.provenance_state,
            record.cabx_chunk_count, record.embedded_metadata_present,
        )
        for index, vector in enumerate(vectors):
            literal = "[" + ",".join(str(float(value)) for value in vector) + "]"
            await conn.execute(
                f"""INSERT INTO {self.schema}.asset_chunks
                    (chunk_id,asset_id,chunk_index,content,embedding)
                    VALUES($1,$2,$3,$4,$5::vector)""",
                f"{record.asset_id}_{index}", record.asset_id, index, projection, literal,
            )

    async def commit_asset_operation(
        self, record: AssetRecord, projection: str, vectors: Iterable[Iterable[float]],
        operation_id: str, result: Mapping[str, Any], *, connector_id: str | None = None,
        tool: str = "put_asset", searchable: bool = True,
    ) -> None:
        connector_key = normalize_connector_id(connector_id)
        async with self.pool.acquire() as conn, conn.transaction():
            await self.replace_asset(
                record, projection, vectors, connection=conn, searchable=searchable,
            )
            status = await conn.execute(
                f"""UPDATE {self.schema}.asset_operations
                        SET state='committed',result=$2::jsonb,completed_at=now()
                        WHERE connector_id=$1 AND tool=$3 AND operation_id=$4""",
                connector_key, json.dumps(dict(result), ensure_ascii=False), tool, operation_id,
            )
            if status != "UPDATE 1":
                raise RuntimeError("asset operation claim was not found")

    async def list_sources(self, prefix: str = "") -> list[str]:
        if prefix:
            rows = await self.pool.fetch(
                f"SELECT source FROM {self.schema}.assets WHERE source LIKE $1 || '%'", prefix
            )
        else:
            rows = await self.pool.fetch(f"SELECT source FROM {self.schema}.assets")
        return [row["source"] for row in rows]

    @staticmethod
    def _literal_descendant_pattern(prefix: str) -> str:
        """Return a ``LIKE`` pattern whose prefix remains a literal path.

        Asset filenames may contain percent or underscore.  PostgreSQL treats
        both as wildcards in ``LIKE``, so directory moves must escape them
        rather than accidentally rebasing a similarly spelled sibling.
        """
        return prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "/%"

    @staticmethod
    def _rebased_source(source: str, old_prefix: str, new_prefix: str) -> str:
        if source == old_prefix:
            return new_prefix
        return new_prefix + source[len(old_prefix):]

    async def rebase_directory_sources(self, old_prefix: str, new_prefix: str) -> int:
        """Move catalog/OCR path bindings without changing asset identity.

        The caller has already validated an ordinary directory rename.  This
        transaction updates only source keys; asset IDs, catalog metadata,
        revisions, chunks, provenance, and operation receipts stay untouched.
        """
        old_prefix = old_prefix.strip("/")
        new_prefix = new_prefix.strip("/")
        if not old_prefix or not new_prefix or old_prefix == new_prefix:
            raise ValueError("directory prefixes must be distinct and nonempty")
        old_pattern = self._literal_descendant_pattern(old_prefix)
        async with self.pool.acquire() as conn, conn.transaction():
            assets = await conn.fetch(
                f"""SELECT source FROM {self.schema}.assets
                    WHERE source=$1 OR source LIKE $2 ESCAPE '\\' FOR UPDATE""",
                old_prefix, old_pattern,
            )
            ocr_sources = await conn.fetch(
                f"""SELECT filepath FROM {self.schema}.asset_ocr_sources
                    WHERE filepath=$1 OR filepath LIKE $2 ESCAPE '\\' FOR UPDATE""",
                old_prefix, old_pattern,
            )
            asset_targets = [
                self._rebased_source(str(row["source"]), old_prefix, new_prefix)
                for row in assets
            ]
            ocr_targets = [
                self._rebased_source(str(row["filepath"]), old_prefix, new_prefix)
                for row in ocr_sources
            ]
            if len(asset_targets) != len(set(asset_targets)) or len(ocr_targets) != len(set(ocr_targets)):
                raise RuntimeError("asset catalog rebase would collide with itself")
            if asset_targets:
                conflicts = await conn.fetch(
                    f"SELECT source FROM {self.schema}.assets WHERE source=ANY($1::text[]) FOR UPDATE",
                    asset_targets,
                )
                if conflicts:
                    raise RuntimeError("asset catalog destination already has a source")
            if ocr_targets:
                conflicts = await conn.fetch(
                    f"SELECT filepath FROM {self.schema}.asset_ocr_sources WHERE filepath=ANY($1::text[]) FOR UPDATE",
                    ocr_targets,
                )
                if conflicts:
                    raise RuntimeError("asset OCR destination already has a source")
            if assets:
                await conn.execute(
                    f"""UPDATE {self.schema}.assets
                        SET source=$2 || substring(source from char_length($1)+1)
                        WHERE source=$1 OR source LIKE $3 ESCAPE '\\'""",
                    old_prefix, new_prefix, old_pattern,
                )
            if ocr_sources:
                await conn.execute(
                    f"""UPDATE {self.schema}.asset_ocr_sources
                        SET filepath=$2 || substring(filepath from char_length($1)+1)
                        WHERE filepath=$1 OR filepath LIKE $3 ESCAPE '\\'""",
                    old_prefix, new_prefix, old_pattern,
                )
        return len(assets)

    async def delete_source(self, filepath: str) -> None:
        async with self.pool.acquire() as conn, conn.transaction():
            await conn.execute(f"DELETE FROM {self.schema}.asset_ocr_sources WHERE filepath=$1", filepath)
            await conn.execute(f"DELETE FROM {self.schema}.assets WHERE source=$1", filepath)

    async def update_asset_file_facts(self, filepath: str, file_size: int, file_mtime: Any) -> None:
        """Refresh filesystem facts without rebuilding catalog vectors or OCR state."""
        await self.pool.execute(
            f"UPDATE {self.schema}.assets SET file_size=$2,file_mtime=$3,indexed_at=now() WHERE source=$1",
            filepath, file_size, file_mtime,
        )

    async def commit_asset_removal(
        self, filepath: str, operation_id: str, result: Mapping[str, Any], *,
        connector_id: str | None = None, tool: str = "remove_asset",
    ) -> None:
        """Detach catalog and path-bound OCR state with the operation receipt.

        The file is owned by :class:`AssetPublisher`; this transaction owns the
        derived stores and the durable idempotency transition.  PostgreSQL rolls
        both deletes back together if either derived-store operation fails.
        """
        connector_key = normalize_connector_id(connector_id)
        async with self.pool.acquire() as conn, conn.transaction():
            await conn.execute(
                f"DELETE FROM {self.schema}.asset_ocr_sources WHERE filepath=$1", filepath
            )
            await conn.execute(
                f"DELETE FROM {self.schema}.assets WHERE source=$1", filepath
            )
            status = await conn.execute(
                f"""UPDATE {self.schema}.asset_operations
                       SET state='committed',result=$2::jsonb,completed_at=now()
                     WHERE connector_id=$1 AND tool=$3 AND operation_id=$4""",
                connector_key, json.dumps(dict(result), ensure_ascii=False), tool, operation_id,
            )
            if status != "UPDATE 1":
                raise RuntimeError("asset operation claim was not found")

    # ---------- derived PNG OCR persistence ----------

    async def get_ocr_result(
        self, source_sha256: str, pipeline_fingerprint: str, languages_key: str,
    ) -> OcrResultFacts | None:
        return await self.ocr.get_cached_result(source_sha256, pipeline_fingerprint, languages_key)

    async def get_ocr_chunks(
        self, source_sha256: str, pipeline_fingerprint: str, languages_key: str,
    ) -> list[OcrSearchChunk]:
        return await self.ocr.get_result_chunks(source_sha256, pipeline_fingerprint, languages_key)

    async def get_ocr_source(self, filepath: str) -> Any | None:
        return await self.ocr.get_source_mapping(filepath)

    async def validate_ocr_freshness(self, filepath: str, source_sha256: str) -> bool:
        return await self.ocr.validate_freshness(filepath, source_sha256)

    async def publish_ocr_result(
        self, snapshot: OcrSourceSnapshot, result: OcrResultFacts,
        chunks: Iterable[OcrSearchChunk], *, connection: Any | None = None,
    ) -> None:
        await self.ocr.publish_result(snapshot, result, list(chunks), connection=connection)

    async def prune_ocr_cache(self, *, limit: int = 100, older_than: timedelta = timedelta(days=30)) -> int:
        return await self.ocr.prune_unreferenced(older_than=older_than, limit=limit)

    async def list_assets(self, prefix: str | None = None, limit: int = 100, after: tuple[str, str] | None = None) -> list[Any]:
        clauses = ["1=1"]
        args: list[Any] = []
        if prefix:
            args.append(prefix)
            clauses.append(f"source LIKE ${len(args)} || '%'")
        if after:
            args.extend([after[0], after[1]])
            clauses.append(f"(lower(source),source) > (${len(args)-1},${len(args)})")
        args.append(limit)
        return await self.pool.fetch(f"SELECT {self._columns()} FROM {self.schema}.assets WHERE {' AND '.join(clauses)} ORDER BY lower(source),source LIMIT ${len(args)}", *args)

    @staticmethod
    def _ocr_search_columns() -> str:
        """Project OCR facts even before a watcher catalogs the source PNG.

        OCR is a source read with derived publication; it must not require a
        metadata reindex first. Keep absent catalog identity/metadata empty,
        rather than inventing an asset ID or writing source metadata. The OCR
        projection is stored only in PostgreSQL, so uncataloged PNGs report
        catalog storage, never embedded metadata or an invalid empty enum.
        """
        return """COALESCE(a.asset_id,'') AS asset_id, os.filepath AS source,
                  os.width, os.height, os.file_size AS final_size,
                  os.source_sha256 AS final_sha256,
                  COALESCE(a.metadata,'{}'::jsonb) AS metadata,
                  COALESCE(a.metadata_storage,'catalog') AS metadata_storage,
                  COALESCE(a.provenance_state,'none') AS provenance_state"""

    async def search_hybrid(
        self, query: str, vector: list[float] | None, limit: int,
        prefix: str | None, tags: list[str] | None, alpha: float,
        include_sources: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        if include_sources is not None and not include_sources:
            return []
        candidates = min(limit * 3, 60)
        common = " AND ($3::text IS NULL OR a.source LIKE $3 || '%') AND ($4::text[] IS NULL OR a.metadata->'tags' ?| $4::text[]) AND ($5::text[] IS NULL OR a.source=ANY($5::text[]))"
        # A present catalog row remains an additional freshness constraint.
        # A missing row is valid: OCR can publish directly from an authorized
        # local PNG, including files provisioned after startup with watch off.
        ocr_common = """ AND (a.source IS NULL OR a.file_sha256=os.source_sha256)
            AND ($3::text IS NULL OR os.filepath LIKE $3 || '%')
            AND ($4::text[] IS NULL OR a.metadata->'tags' ?| $4::text[])
            AND ($5::text[] IS NULL OR os.filepath=ANY($5::text[]))"""
        common_args = (prefix, tags, include_sources)
        lexical: list[Any] = []
        dense: list[Any] = []
        if alpha < 1:
            lexical = list(await self.pool.fetch(
                f"""SELECT {self._columns("a")}, c.chunk_id, c.content, ts_rank_cd(c.tsv,q)::float8 AS rank_score
                    FROM {self.schema}.asset_chunks c JOIN {self.schema}.assets a USING(asset_id),
                         websearch_to_tsquery('english',$1) q
                    WHERE c.tsv @@ q {common}
                    ORDER BY rank_score DESC,c.chunk_id LIMIT $2""",
                query, candidates, *common_args))
            # OCR chunks use the current source mapping, with optional catalog
            # facts checked when present. The service still verifies the actual
            # file hash through its bounded no-atime reader before exposing it.
            lexical.extend(await self.pool.fetch(
                f"""SELECT {self._ocr_search_columns()}, oc.source_sha256 AS ocr_source_sha256,
                           oc.chunk_ordinal AS chunk_index,
                           'ocr'::text AS provenance, oc.source_sha256 || ':' || oc.chunk_ordinal AS chunk_id,
                           oc.text AS content, ts_rank_cd(oc.tsv,q)::float8 AS rank_score
                    FROM {self.schema}.asset_ocr_chunks oc
                    JOIN {self.schema}.asset_ocr_sources os
                      ON os.source_sha256=oc.source_sha256
                     AND os.pipeline_fingerprint=oc.pipeline_fingerprint
                     AND os.languages_key=oc.languages_key
                    LEFT JOIN {self.schema}.assets a ON a.source=os.filepath,
                         websearch_to_tsquery('english',$1) q
                    WHERE oc.tsv @@ q {ocr_common}
                    ORDER BY rank_score DESC,oc.source_sha256,oc.chunk_ordinal LIMIT $2""",
                query, candidates, *common_args))
        if alpha > 0 and vector is not None:
            literal = "[" + ",".join(str(float(value)) for value in vector) + "]"
            dense = list(await self.pool.fetch(
                f"""SELECT {self._columns("a")}, c.chunk_id, c.content, c.embedding <=> $1::vector AS rank_score
                    FROM {self.schema}.asset_chunks c JOIN {self.schema}.assets a USING(asset_id)
                    WHERE true {common}
                    ORDER BY c.embedding <=> $1::vector,c.chunk_id LIMIT $2""",
                literal, candidates, *common_args))
            dense.extend(await self.pool.fetch(
                f"""SELECT {self._ocr_search_columns()}, oc.source_sha256 AS ocr_source_sha256,
                           oc.chunk_ordinal AS chunk_index,
                           'ocr'::text AS provenance, oc.source_sha256 || ':' || oc.chunk_ordinal AS chunk_id,
                           oc.text AS content, oc.embedding <=> $1::vector AS rank_score
                    FROM {self.schema}.asset_ocr_chunks oc
                    JOIN {self.schema}.asset_ocr_sources os
                      ON os.source_sha256=oc.source_sha256
                     AND os.pipeline_fingerprint=oc.pipeline_fingerprint
                     AND os.languages_key=oc.languages_key
                    LEFT JOIN {self.schema}.assets a ON a.source=os.filepath
                    WHERE true {ocr_common}
                    ORDER BY oc.embedding <=> $1::vector,oc.source_sha256,oc.chunk_ordinal LIMIT $2""",
                literal, candidates, *common_args))
        fused: dict[str, dict[str, Any]] = {}
        for rank, row in enumerate(dense, 1):
            key = str(row["source"])
            fused[key] = {"row": row, "semantic_rank": rank, "keyword_rank": None}
        for rank, row in enumerate(lexical, 1):
            key = str(row["source"])
            item = fused.setdefault(key, {"row": row, "semantic_rank": None, "keyword_rank": None})
            item["keyword_rank"] = rank
        for item in fused.values():
            semantic = item["semantic_rank"] or 10000
            keyword = item["keyword_rank"] or 10000
            item["raw"] = alpha / (60 + semantic) + (1 - alpha) / (60 + keyword)
        ordered = sorted(fused.values(), key=lambda item: item["raw"], reverse=True)[:limit]
        if not ordered:
            return []
        low = min(item["raw"] for item in ordered)
        high = max(item["raw"] for item in ordered)
        results: list[dict[str, Any]] = []
        for item in ordered:
            row = dict(item["row"])
            row["display_score"] = ((item["raw"] - low) / (high - low)) if high > low else 1.0
            row["search_method"] = ("hybrid" if item["semantic_rank"] and item["keyword_rank"]
                                    else "semantic" if item["semantic_rank"] else "keyword")
            results.append(row)
        return results

    async def searchable_source_paths(self) -> list[str]:
        """Current cataloged and OCR-mapped paths, including uncataloged OCR."""
        rows = await self.pool.fetch(
            f"""SELECT source FROM {self.schema}.assets
                UNION SELECT filepath AS source FROM {self.schema}.asset_ocr_sources
                ORDER BY source"""
        )
        return [str(row["source"]) for row in rows]

    async def deindex_searchable_paths(self, paths: list[str]) -> int:
        """Remove searchable asset/OCR projections while retaining asset facts."""
        if not paths:
            return 0
        async with self.pool.acquire() as conn, conn.transaction():
            status = await conn.execute(
                f"""DELETE FROM {self.schema}.asset_chunks c
                    USING {self.schema}.assets a
                    WHERE c.asset_id=a.asset_id AND a.source=ANY($1::text[])""",
                paths,
            )
            await conn.execute(
                f"DELETE FROM {self.schema}.asset_ocr_sources WHERE filepath=ANY($1::text[])",
                paths,
            )
        try:
            return int(status.rsplit(" ", 1)[-1])
        except (AttributeError, ValueError):
            return 0

    async def claim_operation(
        self, operation_id: str, tool: str, request_sha256: str, *,
        connector_id: str | None = None,
    ) -> tuple[str, Any | None]:
        connector_key = normalize_connector_id(connector_id)
        async with self.pool.acquire() as conn, conn.transaction():
            legacy = await conn.fetchrow(
                f"""SELECT 1 FROM {self.schema}.asset_operations
                    WHERE connector_id=$1 AND tool=$2 AND operation_id=$3""",
                LEGACY_CONNECTOR_ID, tool, operation_id,
            )
            if legacy:
                # The old row predates connector identity. Returning its
                # result would disclose/replay another connector's work. A
                # connector-less compatibility caller is equally ambiguous.
                return "conflict", None
            row = await conn.fetchrow(
                f"""SELECT * FROM {self.schema}.asset_operations
                    WHERE connector_id=$1 AND tool=$2 AND operation_id=$3 FOR UPDATE""",
                connector_key, tool, operation_id,
            )
            if row:
                if row["request_sha256"] != request_sha256:
                    return "conflict", None
                if row["state"] == "committed":
                    return "replay", row["result"]
                if row["state"] == "failed":
                    return "failed", row["result"]
                return "busy", None
            await conn.execute(
                f"""INSERT INTO {self.schema}.asset_operations
                    (connector_id,operation_id,tool,request_sha256,state)
                    VALUES($1,$2,$3,$4,'running')""",
                connector_key, operation_id, tool, request_sha256,
            )
            return "claimed", None

    async def get_operation(
        self, operation_id: str, *, connector_id: str | None = None, tool: str | None = None,
    ) -> Any | None:
        connector_key = normalize_connector_id(connector_id)
        clauses = ["connector_id=$1", "operation_id=$2"]
        args: list[Any] = [connector_key, operation_id]
        if tool is not None:
            clauses.append("tool=$3")
            args.append(tool)
        return await self.pool.fetchrow(
            f"SELECT * FROM {self.schema}.asset_operations WHERE {' AND '.join(clauses)}",
            *args,
        )

    async def finish_operation(
        self, operation_id: str, result: Mapping[str, Any], *, failed: bool = False,
        connector_id: str | None = None, tool: str | None = None,
    ) -> None:
        connector_key = normalize_connector_id(connector_id)
        clauses = ["connector_id=$1", "operation_id=$2"]
        args: list[Any] = [connector_key, operation_id]
        if tool is not None:
            clauses.append("tool=$3")
            args.append(tool)
        state_index = len(args) + 1
        result_index = state_index + 1
        args.extend(["failed" if failed else "committed", json.dumps(dict(result), ensure_ascii=False)])
        await self.pool.execute(
            f"""UPDATE {self.schema}.asset_operations
                SET state=${state_index},result=${result_index}::jsonb,completed_at=now()
                WHERE {' AND '.join(clauses)}""",
            *args,
        )

    async def prune_operations(self, days: int = 7) -> None:
        cutoff = datetime.now(UTC) - timedelta(days=days)
        await self.pool.execute(f"DELETE FROM {self.schema}.asset_operations WHERE state IN ('committed','failed') AND created_at < $1", cutoff)
