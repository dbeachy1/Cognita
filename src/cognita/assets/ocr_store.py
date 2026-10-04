"""Durable, project-isolated persistence for derived PNG OCR.

OCR is derived data rather than a replacement for the asset catalog.  The
tables in this module are additive, so an older binary can continue to use
``assets`` and ``asset_chunks`` while ignoring OCR rows.  Pixel/file validation
remains owned by :mod:`assets.service`; this module only accepts already
validated source snapshots and OCR facts.

SUPERSEDED (13.0 §4.1): these tables used to be *independently versioned*, in a
per-project ``asset_ocr_schema_meta`` row.  The complete Cognita PostgreSQL
schema now carries ONE version, ``release_identity.DATABASE_SCHEMA_VERSION``,
stamped in ``public.cognita_metadata`` and checked by ``store.Store.connect``
before any DDL runs.
"""

from __future__ import annotations

import json
import math
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from ..registry import NAME_RE

_MAX_DIGEST = 64
_MAX_LANGUAGES_KEY = 256
_MAX_TEXT_BYTES = 1_048_576
_MAX_WARNING_BYTES = 8_192
_MAX_REGIONS = 10_000
_MAX_WARNINGS = 128


def _schema_for(project: str) -> str:
    if not isinstance(project, str) or not NAME_RE.match(project):
        raise ValueError("invalid project name")
    return "proj_" + project


def _quoted_schema(project: str) -> str:
    return '"' + _schema_for(project).replace('"', '""') + '"'


def _digest(value: str, name: str) -> str:
    if not isinstance(value, str) or len(value) != _MAX_DIGEST:
        raise ValueError(f"{name} must be a SHA-256 hex digest")
    try:
        int(value, 16)
    except ValueError as exc:
        raise ValueError(f"{name} must be a SHA-256 hex digest") from exc
    return value.lower()


def normalize_languages_key(languages: Sequence[str] | str) -> str:
    """Return a stable identity for an already validated language selection.

    Language order is not semantically meaningful to the OCR pipeline, so the
    key is sorted after NFC/case normalization.  The runtime still owns the
    installed-language allowlist and maximum-three check; this helper merely
    ensures cache keys cannot vary because of list ordering or Unicode form.
    """
    if isinstance(languages, str):
        values = [part for part in languages.split(",") if part]
    else:
        values = list(languages)
    if not 1 <= len(values) <= 3 or any(
        not isinstance(value, str) or not value or len(value) > 32 for value in values
    ):
        raise ValueError("languages must contain one to three bounded language codes")
    normalized = sorted({unicodedata.normalize("NFC", value).casefold() for value in values})
    if len(normalized) != len(values):
        raise ValueError("languages must not contain duplicates")
    result = ",".join(normalized)
    if len(result.encode("utf-8")) > _MAX_LANGUAGES_KEY:
        raise ValueError("languages key is too large")
    return result


def _normalize_text(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("OCR text must be a string")  # noqa: TRY004 - persistence validation contract
    value = unicodedata.normalize("NFC", value.replace("\r\n", "\n").replace("\r", "\n"))
    if len(value.encode("utf-8")) > _MAX_TEXT_BYTES:
        raise ValueError("OCR text exceeds the configured result bound")
    return value


def _json_value(value: Any, name: str, maximum: int) -> Any:
    try:
        encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be JSON-compatible") from exc
    if len(encoded.encode("utf-8")) > maximum:
        raise ValueError(f"{name} exceeds its configured bound")
    return json.loads(encoded)


@dataclass(frozen=True, slots=True)
class OcrSourceSnapshot:
    """Immutable facts for the exact source bytes given to the OCR worker."""

    filepath: str
    source_sha256: str
    file_size: int
    width: int
    height: int
    snapshot_token: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.filepath, str) or not self.filepath:
            raise ValueError("filepath is required")
        object.__setattr__(self, "source_sha256", _digest(self.source_sha256, "source_sha256"))
        for value, name in ((self.file_size, "file_size"), (self.width, "width"), (self.height, "height")):
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if not isinstance(self.snapshot_token, str) or len(self.snapshot_token.encode("utf-8")) > 512:
            raise ValueError("snapshot_token is invalid")


@dataclass(frozen=True, slots=True)
class OcrResultFacts:
    """Validated OCR output independent of the selected CPU/GPU device."""

    source_sha256: str
    pipeline_fingerprint: str
    languages_key: str
    engine_name: str
    engine_version: str
    model_fingerprint: str
    pipeline_version: int
    width: int
    height: int
    outcome: str
    text: str
    regions: Sequence[Mapping[str, Any]] = ()
    warnings: Sequence[Mapping[str, Any]] = ()
    device: str = "cpu"
    backend: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_sha256", _digest(self.source_sha256, "source_sha256"))
        object.__setattr__(self, "pipeline_fingerprint", _digest(self.pipeline_fingerprint, "pipeline_fingerprint"))
        object.__setattr__(self, "model_fingerprint", _digest(self.model_fingerprint, "model_fingerprint"))
        object.__setattr__(self, "languages_key", normalize_languages_key(self.languages_key))
        for value, name in ((self.engine_name, "engine_name"), (self.engine_version, "engine_version"), (self.device, "device"), (self.backend, "backend")):
            if not isinstance(value, str) or len(value.encode("utf-8")) > 256:
                raise ValueError(f"{name} is invalid")
        if isinstance(self.pipeline_version, bool) or not isinstance(self.pipeline_version, int) or self.pipeline_version < 1:
            raise ValueError("pipeline_version must be positive")
        for value, name in ((self.width, "width"), (self.height, "height")):
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be positive")
        if self.outcome not in {"text", "no_text"}:
            raise ValueError("outcome must be 'text' or 'no_text'")
        text = _normalize_text(self.text)
        if self.outcome == "no_text" and text:
            raise ValueError("no_text OCR results must have empty text")
        if self.outcome == "text" and not text:
            raise ValueError("text OCR results must contain text")
        regions = tuple(_json_value(list(self.regions), "regions", _MAX_TEXT_BYTES))
        warnings = tuple(_json_value(list(self.warnings), "warnings", _MAX_WARNING_BYTES))
        if len(regions) > _MAX_REGIONS or len(warnings) > _MAX_WARNINGS:
            raise ValueError("OCR regions or warnings exceed their configured bound")
        if not all(isinstance(item, Mapping) for item in regions + warnings):
            raise ValueError("OCR regions and warnings must be objects")
        object.__setattr__(self, "text", text)
        object.__setattr__(self, "regions", regions)
        object.__setattr__(self, "warnings", warnings)

    @property
    def searchable(self) -> bool:
        """Whether this result is eligible for OCR search publication."""
        return self.outcome == "text" and bool(self.text)

    @property
    def cache_key(self) -> tuple[str, str, str]:
        """The complete project-local derived-result identity."""
        return self.source_sha256, self.pipeline_fingerprint, self.languages_key


@dataclass(frozen=True, slots=True)
class OcrSearchChunk:
    ordinal: int
    text: str
    embedding: Sequence[float]

    def __post_init__(self) -> None:
        if isinstance(self.ordinal, bool) or not isinstance(self.ordinal, int) or self.ordinal < 0:
            raise ValueError("OCR chunk ordinal must be non-negative")
        object.__setattr__(self, "text", _normalize_text(self.text))
        if not self.text:
            raise ValueError("OCR chunk text must not be empty")
        vector = tuple(float(value) for value in self.embedding)
        if not vector or not all(math.isfinite(value) for value in vector):
            raise ValueError("OCR chunk embedding must be finite and non-empty")
        object.__setattr__(self, "embedding", vector)


class OcrStoreError(RuntimeError):
    """Safe persistence failure; callers translate it to the OCR error envelope."""


class OcrSourceChangedError(OcrStoreError):
    """The catalog source facts no longer match the worker's snapshot."""


def ocr_ddl(project: str, dimensions: int = 1024) -> str:
    """Return additive OCR tables for one project schema."""
    if isinstance(dimensions, bool) or not isinstance(dimensions, int) or dimensions < 1:
        raise ValueError("embedding dimensions must be positive")
    s = _quoted_schema(project)
    return f"""
CREATE TABLE IF NOT EXISTS {s}.asset_ocr_results (
 source_sha256 text NOT NULL CHECK (length(source_sha256)=64),
 pipeline_fingerprint text NOT NULL CHECK (length(pipeline_fingerprint)=64),
 languages_key text NOT NULL CHECK (length(languages_key) BETWEEN 1 AND 256),
 engine_name text NOT NULL, engine_version text NOT NULL,
 model_fingerprint text NOT NULL CHECK (length(model_fingerprint)=64),
 pipeline_version integer NOT NULL CHECK (pipeline_version > 0),
 width integer NOT NULL CHECK (width > 0), height integer NOT NULL CHECK (height > 0),
 outcome text NOT NULL CHECK (outcome IN ('text','no_text')),
 text text NOT NULL, regions jsonb NOT NULL, warnings jsonb NOT NULL,
 device text NOT NULL, backend text NOT NULL,
 created_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY (source_sha256, pipeline_fingerprint, languages_key)
);
CREATE TABLE IF NOT EXISTS {s}.asset_ocr_sources (
 filepath text PRIMARY KEY,
 source_sha256 text NOT NULL,
 pipeline_fingerprint text NOT NULL,
 languages_key text NOT NULL,
 file_size bigint NOT NULL CHECK (file_size > 0),
 width integer NOT NULL CHECK (width > 0), height integer NOT NULL CHECK (height > 0),
 snapshot_token text NOT NULL DEFAULT '',
 updated_at timestamptz NOT NULL DEFAULT now(),
 FOREIGN KEY (source_sha256, pipeline_fingerprint, languages_key)
   REFERENCES {s}.asset_ocr_results(source_sha256, pipeline_fingerprint, languages_key)
   ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS asset_ocr_sources_hash ON {s}.asset_ocr_sources (source_sha256);
CREATE TABLE IF NOT EXISTS {s}.asset_ocr_chunks (
 source_sha256 text NOT NULL, pipeline_fingerprint text NOT NULL, languages_key text NOT NULL,
 chunk_ordinal integer NOT NULL CHECK (chunk_ordinal >= 0),
 text text NOT NULL, embedding vector({dimensions}) NOT NULL,
 tsv tsvector GENERATED ALWAYS AS (to_tsvector('english', text)) STORED,
 PRIMARY KEY (source_sha256, pipeline_fingerprint, languages_key, chunk_ordinal),
 FOREIGN KEY (source_sha256, pipeline_fingerprint, languages_key)
   REFERENCES {s}.asset_ocr_results(source_sha256, pipeline_fingerprint, languages_key)
   ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS asset_ocr_chunks_embedding_hnsw ON {s}.asset_ocr_chunks USING hnsw (embedding vector_cosine_ops);
CREATE INDEX IF NOT EXISTS asset_ocr_chunks_tsv_gin ON {s}.asset_ocr_chunks USING gin (tsv);
"""


class OcrStore:
    """Project-scoped OCR cache and search-chunk persistence."""

    def __init__(self, pool: Any, project: str, *, dimensions: int = 1024):
        self.pool = pool
        self.project = project
        self.schema = _quoted_schema(project)
        self.dimensions = dimensions

    async def _ensure_schema_on_connection(self, conn: Any) -> None:
        # 13.0: no per-project version read afterwards — the one schema version
        # is checked in store.Store.connect, before any of this DDL can run.
        await conn.execute(ocr_ddl(self.project, self.dimensions))

    async def ensure_schema(self) -> None:
        async with self.pool.acquire() as conn, conn.transaction():
            await self._ensure_schema_on_connection(conn)

    async def get_cached_result(
        self, source_sha256: str, pipeline_fingerprint: str, languages_key: str | Sequence[str]
    ) -> OcrResultFacts | None:
        source_sha256 = _digest(source_sha256, "source_sha256")
        pipeline_fingerprint = _digest(pipeline_fingerprint, "pipeline_fingerprint")
        languages_key = normalize_languages_key(languages_key)
        row = await self.pool.fetchrow(
            f"""SELECT source_sha256,pipeline_fingerprint,languages_key,engine_name,
                       engine_version,model_fingerprint,pipeline_version,width,height,
                       outcome,text,regions,warnings,device,backend
                FROM {self.schema}.asset_ocr_results
                WHERE source_sha256=$1 AND pipeline_fingerprint=$2 AND languages_key=$3""",
            source_sha256, pipeline_fingerprint, languages_key,
        )
        return self._result_from_row(row) if row else None

    async def get_result_chunks(
        self, source_sha256: str, pipeline_fingerprint: str, languages_key: str | Sequence[str]
    ) -> list[OcrSearchChunk]:
        key = (_digest(source_sha256, "source_sha256"), _digest(pipeline_fingerprint, "pipeline_fingerprint"), normalize_languages_key(languages_key))
        rows = await self.pool.fetch(
            f"""SELECT chunk_ordinal,text,embedding::text AS embedding
                FROM {self.schema}.asset_ocr_chunks
                WHERE source_sha256=$1 AND pipeline_fingerprint=$2 AND languages_key=$3
                ORDER BY chunk_ordinal""", *key,
        )
        return [OcrSearchChunk(r["chunk_ordinal"], r["text"], _parse_vector(r["embedding"])) for r in rows]

    async def get_source_mapping(self, filepath: str) -> Any | None:
        return await self.pool.fetchrow(
            f"SELECT filepath,source_sha256,pipeline_fingerprint,languages_key,file_size,width,height,snapshot_token,updated_at FROM {self.schema}.asset_ocr_sources WHERE filepath=$1",
            filepath,
        )

    async def validate_freshness(
        self, filepath: str, source_sha256: str, *, connection: Any | None = None
    ) -> bool:
        """Validate catalog/source-mapping hash facts only.

        This deliberately performs no filesystem access.  The service must
        supply a freshly verified file hash before invoking this method and
        again before exposing a search hit.
        """
        source_sha256 = _digest(source_sha256, "source_sha256")

        async def check(conn: Any) -> bool:
            row = await conn.fetchrow(
                f"SELECT file_sha256 FROM {self.schema}.assets WHERE source=$1", filepath
            )
            if row is not None:
                return row["file_sha256"] == source_sha256
            mapping = await conn.fetchrow(
                f"SELECT source_sha256 FROM {self.schema}.asset_ocr_sources WHERE filepath=$1", filepath
            )
            return mapping is not None and mapping["source_sha256"] == source_sha256

        if connection is not None:
            return await check(connection)
        async with self.pool.acquire() as conn:
            return await check(conn)

    async def publish_result(
        self, snapshot: OcrSourceSnapshot, result: OcrResultFacts,
        chunks: Sequence[OcrSearchChunk], *, connection: Any | None = None,
    ) -> None:
        """Atomically publish result facts, source mapping and search chunks."""
        self._validate_publication(snapshot, result, chunks)
        if connection is not None:
            await self._publish_on_connection(connection, snapshot, result, chunks)
            return
        async with self.pool.acquire() as conn, conn.transaction():
            await self._publish_on_connection(conn, snapshot, result, chunks)

    def _validate_publication(
        self, snapshot: OcrSourceSnapshot, result: OcrResultFacts, chunks: Sequence[OcrSearchChunk]
    ) -> None:
        if result.source_sha256 != snapshot.source_sha256:
            raise ValueError("OCR result does not match source snapshot")
        if result.width != snapshot.width or result.height != snapshot.height:
            raise ValueError("OCR dimensions do not match source snapshot")
        if result.outcome == "no_text" and chunks:
            raise ValueError("no_text OCR results cannot publish search chunks")
        if result.outcome == "text" and result.text and not chunks:
            raise ValueError("searchable OCR text requires embedded chunks")
        ordinals = [chunk.ordinal for chunk in chunks]
        if ordinals != list(range(len(ordinals))):
            raise ValueError("OCR chunk ordinals must be contiguous from zero")
        if any(len(chunk.embedding) != self.dimensions for chunk in chunks):
            raise ValueError("OCR chunk embedding dimensions do not match the project")

    async def _publish_on_connection(
        self, conn: Any, snapshot: OcrSourceSnapshot, result: OcrResultFacts,
        chunks: Sequence[OcrSearchChunk],
    ) -> None:
        current = await conn.fetchrow(
            f"SELECT file_sha256,file_size,width,height FROM {self.schema}.assets WHERE source=$1 FOR UPDATE",
            snapshot.filepath,
        )
        if current is not None and (
            current["file_sha256"] != snapshot.source_sha256
            or current["file_size"] != snapshot.file_size
            or current["width"] != snapshot.width
            or current["height"] != snapshot.height
        ):
            raise OcrSourceChangedError("catalog source facts changed during OCR")
        key = (result.source_sha256, result.pipeline_fingerprint, result.languages_key)
        await conn.execute(
            f"""INSERT INTO {self.schema}.asset_ocr_results
                (source_sha256,pipeline_fingerprint,languages_key,engine_name,engine_version,
                 model_fingerprint,pipeline_version,width,height,outcome,text,regions,warnings,device,backend)
                VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12::jsonb,$13::jsonb,$14,$15)
                ON CONFLICT (source_sha256,pipeline_fingerprint,languages_key) DO UPDATE SET
                 engine_name=EXCLUDED.engine_name,engine_version=EXCLUDED.engine_version,
                 model_fingerprint=EXCLUDED.model_fingerprint,pipeline_version=EXCLUDED.pipeline_version,
                 width=EXCLUDED.width,height=EXCLUDED.height,outcome=EXCLUDED.outcome,text=EXCLUDED.text,
                 regions=EXCLUDED.regions,warnings=EXCLUDED.warnings,device=EXCLUDED.device,
                 backend=EXCLUDED.backend,created_at=now()""",
            *key, result.engine_name, result.engine_version, result.model_fingerprint,
            result.pipeline_version, result.width, result.height, result.outcome, result.text,
            json.dumps(list(result.regions), ensure_ascii=False, separators=(",", ":")),
            json.dumps(list(result.warnings), ensure_ascii=False, separators=(",", ":")),
            result.device, result.backend,
        )
        await conn.execute(
            f"DELETE FROM {self.schema}.asset_ocr_chunks WHERE source_sha256=$1 AND pipeline_fingerprint=$2 AND languages_key=$3",
            *key,
        )
        for chunk in chunks:
            await conn.execute(
                f"""INSERT INTO {self.schema}.asset_ocr_chunks
                    (source_sha256,pipeline_fingerprint,languages_key,chunk_ordinal,text,embedding)
                    VALUES($1,$2,$3,$4,$5,$6::vector)""",
                *key, chunk.ordinal, chunk.text, _vector_literal(chunk.embedding),
            )
        await conn.execute(
            f"""INSERT INTO {self.schema}.asset_ocr_sources
                (filepath,source_sha256,pipeline_fingerprint,languages_key,file_size,width,height,snapshot_token)
                VALUES($1,$2,$3,$4,$5,$6,$7,$8)
                ON CONFLICT (filepath) DO UPDATE SET source_sha256=EXCLUDED.source_sha256,
                 pipeline_fingerprint=EXCLUDED.pipeline_fingerprint,languages_key=EXCLUDED.languages_key,
                 file_size=EXCLUDED.file_size,width=EXCLUDED.width,height=EXCLUDED.height,
                 snapshot_token=EXCLUDED.snapshot_token,updated_at=now()""",
            snapshot.filepath, *key, snapshot.file_size, snapshot.width, snapshot.height,
            snapshot.snapshot_token,
        )

    async def detach_source(self, filepath: str, *, connection: Any | None = None) -> None:
        if connection is not None:
            await self.detach_source_on_connection(connection, filepath)
            return
        async with self.pool.acquire() as conn, conn.transaction():
            await self.detach_source_on_connection(conn, filepath)

    async def detach_sources(self, filepaths: Sequence[str], *, connection: Any | None = None) -> None:
        paths = list(dict.fromkeys(filepaths))
        if not paths:
            return
        if connection is not None:
            await connection.execute(f"DELETE FROM {self.schema}.asset_ocr_sources WHERE filepath = ANY($1::text[])", paths)
            return
        async with self.pool.acquire() as conn, conn.transaction():
            await conn.execute(f"DELETE FROM {self.schema}.asset_ocr_sources WHERE filepath = ANY($1::text[])", paths)

    async def detach_source_on_connection(self, conn: Any, filepath: str, *, asset_id: str | None = None) -> None:
        if asset_id is None:
            await conn.execute(f"DELETE FROM {self.schema}.asset_ocr_sources WHERE filepath=$1", filepath)
            return
        await conn.execute(
            f"""DELETE FROM {self.schema}.asset_ocr_sources s USING {self.schema}.assets a
                WHERE (s.filepath=$1 OR (a.asset_id=$2 AND s.filepath=a.source))""",
            filepath, asset_id,
        )

    async def prune_unreferenced(self, *, older_than: timedelta = timedelta(days=30), limit: int = 100) -> int:
        if older_than < timedelta(0) or isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 10_000:
            raise ValueError("invalid OCR maintenance bound")
        cutoff = datetime.now(UTC) - older_than
        async with self.pool.acquire() as conn, conn.transaction():
            status = await conn.execute(
                f"""WITH doomed AS (
                    SELECT r.source_sha256,r.pipeline_fingerprint,r.languages_key
                    FROM {self.schema}.asset_ocr_results r
                    LEFT JOIN {self.schema}.asset_ocr_sources s
                      ON s.source_sha256=r.source_sha256
                     AND s.pipeline_fingerprint=r.pipeline_fingerprint
                     AND s.languages_key=r.languages_key
                    WHERE s.filepath IS NULL AND r.created_at < $1
                    ORDER BY r.created_at,r.source_sha256,r.pipeline_fingerprint,r.languages_key
                    LIMIT $2
                )
                DELETE FROM {self.schema}.asset_ocr_results r USING doomed d
                WHERE r.source_sha256=d.source_sha256
                  AND r.pipeline_fingerprint=d.pipeline_fingerprint
                  AND r.languages_key=d.languages_key""",
                cutoff, limit,
            )
        return int(status.removeprefix("DELETE "))

    @staticmethod
    def _result_from_row(row: Any) -> OcrResultFacts:
        def obj(value: Any) -> Any:
            if isinstance(value, str):
                return json.loads(value)
            return value
        return OcrResultFacts(
            row["source_sha256"], row["pipeline_fingerprint"], row["languages_key"],
            row["engine_name"], row["engine_version"], row["model_fingerprint"],
            row["pipeline_version"], row["width"], row["height"], row["outcome"], row["text"],
            obj(row["regions"]), obj(row["warnings"]), row["device"], row["backend"],
        )


def _vector_literal(values: Sequence[float]) -> str:
    return "[" + ",".join(str(float(value)) for value in values) + "]"


def _parse_vector(literal: str) -> list[float]:
    return [float(value) for value in literal.strip("[]").split(",") if value]


__all__ = [
    "OcrResultFacts", "OcrSearchChunk", "OcrSourceChangedError",
    "OcrSourceSnapshot", "OcrStore", "OcrStoreError", "normalize_languages_key", "ocr_ddl",
]
