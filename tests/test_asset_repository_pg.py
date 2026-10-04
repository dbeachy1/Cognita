"""Real PostgreSQL transaction coverage for the 7.1 asset catalog."""

import hashlib
import os
import uuid
from datetime import UTC, datetime

import asyncpg
import pytest

from cognita.assets.models import AssetRecord
from cognita.assets.ocr_store import OcrResultFacts, OcrSearchChunk, OcrSourceSnapshot
from cognita.assets.repository import AssetRepository
from cognita.assets.service import AssetService
from cognita.store import Store

DSN = os.environ.get("COGNITA_TEST_PG_DSN", "")
pytestmark = pytest.mark.skipif(not DSN, reason="COGNITA_TEST_PG_DSN not set")


def record(source: str, revision: int = 1) -> AssetRecord:
    digest = f"{revision:064x}"
    return AssetRecord(
        asset_id=str(uuid.uuid4()),
        filepath=source,
        metadata={"title": f"revision {revision}", "tags": ["asset"]},
        received_size=64,
        received_sha256=digest,
        final_size=64,
        final_sha256=digest,
        width=8,
        height=8,
        metadata_storage="catalog",
        metadata_revision=revision,
        file_mtime=datetime.now(UTC),
    )


@pytest.fixture
async def repository():
    store = Store(DSN, embedding_dimensions=8)
    await store.connect()
    project = f"AssetT{uuid.uuid4().hex[:8]}"
    await store.ensure_project(project)
    repo = AssetRepository(store.pool, project, dimensions=8)
    try:
        yield repo
    finally:
        await store.drop_project(project)
        await store.close()


@pytest.mark.asyncio
async def test_replace_and_chunks_rollback_as_one_transaction(repository):
    original = record("images/a.png")
    await repository.replace_asset(original, "original asset", [[0.1] * 8])

    replacement = record("images/a.png", revision=2)
    replacement.asset_id = original.asset_id
    with pytest.raises(asyncpg.DataError):
        await repository.replace_asset(replacement, "broken vector", [[0.2] * 7])

    row = await repository.get("images/a.png")
    assert row["metadata_revision"] == 1
    chunks = await repository.pool.fetch(
        f"SELECT content FROM {repository.schema}.asset_chunks WHERE asset_id=$1",
        original.asset_id,
    )
    assert [chunk["content"] for chunk in chunks] == ["original asset"]


@pytest.mark.asyncio
async def test_asset_and_operation_result_commit_atomically(repository):
    item = record("images/atomic.png")
    operation_id = "asset-atomic-1"
    fingerprint = "a" * 64
    assert await repository.claim_operation(operation_id, "put_asset", fingerprint) == (
        "claimed",
        None,
    )
    await repository.commit_asset_operation(
        item,
        "atomic asset",
        [[0.1] * 8],
        operation_id,
        {"status": "success", "filepath": item.filepath},
    )
    operation = await repository.get_operation(operation_id)
    assert operation["state"] == "committed"
    assert (await repository.get(item.filepath))["asset_id"] == item.asset_id


@pytest.mark.asyncio
async def test_catalog_rows_and_keyword_search_preserve_identity_and_metadata(
    repository, tmp_path,
):
    item = record("images/searchable.png")
    await repository.replace_asset(item, "Title: searchable keyword", [[0.1] * 8])

    class Project:
        name = repository.project
        documents_dir = tmp_path
        data_dir = tmp_path / "data"

    service = AssetService(Project(), repository, connector_id="connector-a")
    listed = await service.list_assets({})
    assert listed["assets"][0]["asset_id"] == item.asset_id
    assert listed["assets"][0]["title"] == "revision 1"
    searched = await service.search_assets({"query": "searchable", "hybrid_alpha": 0})
    assert searched["results"][0]["asset_id"] == item.asset_id
    assert searched["results"][0]["tags"] == ["asset"]

    operation_id = "catalog-replay-1"
    fingerprint = hashlib.sha256(b'{"prefix":""}').hexdigest()
    result = {"status": "success", "asset_id": item.asset_id, "metadata_revision": 1}
    assert await repository.claim_operation(
        operation_id, "reindex_assets", fingerprint, connector_id="connector-a",
    ) == ("claimed", None)
    await repository.finish_operation(
        operation_id, result, tool="reindex_assets", connector_id="connector-a",
    )
    replay = await service.reindex_assets({"operation_id": operation_id})
    assert replay == {**result, "idempotent_replay": True}


@pytest.mark.asyncio
async def test_operation_identity_is_connector_and_tool_bound(repository):
    fingerprint = "b" * 64
    assert await repository.claim_operation(
        "shared-op", "put_asset", fingerprint, connector_id="connector-a"
    ) == ("claimed", None)
    assert await repository.claim_operation(
        "shared-op", "put_asset", fingerprint, connector_id="connector-b"
    ) == ("claimed", None)
    assert await repository.claim_operation(
        "shared-op", "update_asset_metadata", fingerprint, connector_id="connector-a"
    ) == ("claimed", None)
    assert await repository.claim_operation(
        "shared-op", "put_asset", fingerprint, connector_id="connector-a"
    ) == ("busy", None)


@pytest.mark.asyncio
async def test_legacy_operation_cannot_be_replayed_by_connector(repository):
    fingerprint = "c" * 64
    assert await repository.claim_operation("old-op", "put_asset", fingerprint) == (
        "claimed", None
    )
    assert await repository.claim_operation(
        "old-op", "put_asset", fingerprint, connector_id="connector-a"
    ) == ("conflict", None)
    assert await repository.claim_operation("old-op", "put_asset", fingerprint) == (
        "conflict", None
    )


@pytest.mark.asyncio
async def test_ocr_publication_cache_search_and_detach_are_project_atomic(repository):
    item = record("images/ocr.png")
    await repository.replace_asset(item, "asset metadata", [[0.1] * 8])
    snapshot = OcrSourceSnapshot(item.filepath, item.final_sha256, item.final_size, item.width, item.height, "synthetic-stat")
    result = OcrResultFacts(
        item.final_sha256, "b" * 64, ["en"], "easyocr", "1.7.2", "c" * 64, 1,
        item.width, item.height, "text", "OCR_ONLY_UNIQUE_TOKEN",
    )
    await repository.publish_ocr_result(
        snapshot, result, [OcrSearchChunk(0, result.text, [0.2] * 8)]
    )
    cached = await repository.get_ocr_result(item.final_sha256, "b" * 64, "en")
    assert cached is not None and cached.text == result.text
    assert await repository.validate_ocr_freshness(item.filepath, item.final_sha256)
    hit = await repository.search_hybrid("OCR_ONLY_UNIQUE_TOKEN", None, 5, None, None, 0)
    assert hit and hit[0]["provenance"] == "ocr"
    assert hit[0]["ocr_source_sha256"] == item.final_sha256

    replacement = record(item.filepath, revision=2)
    replacement.asset_id = item.asset_id
    await repository.replace_asset(replacement, "replacement metadata", [[0.3] * 8])
    assert await repository.get_ocr_source(item.filepath) is None
    assert not await repository.validate_ocr_freshness(item.filepath, item.final_sha256)


@pytest.mark.asyncio
async def test_ocr_unreferenced_maintenance_is_bounded(repository):
    item = record("images/old-ocr.png")
    await repository.replace_asset(item, "asset metadata", [[0.1] * 8])
    snapshot = OcrSourceSnapshot(item.filepath, item.final_sha256, item.final_size, item.width, item.height)
    result = OcrResultFacts(
        item.final_sha256, "d" * 64, "en", "easyocr", "1.7.2", "e" * 64, 1,
        item.width, item.height, "no_text", "",
    )
    await repository.publish_ocr_result(snapshot, result, [])
    await repository.delete_source(item.filepath)
    await repository.pool.execute(
        f"UPDATE {repository.schema}.asset_ocr_results SET created_at=now() - interval '31 days'"
    )
    assert await repository.prune_ocr_cache(limit=1) == 1
    assert await repository.get_ocr_result(item.final_sha256, "d" * 64, "en") is None


@pytest.mark.asyncio
async def test_uncataloged_ocr_search_uses_source_identity_and_preserves_hash_and_tag_filters(repository, tmp_path):
    from types import SimpleNamespace
    from cognita.result_contracts import build_tool_result, validate_structured_payload
    service = AssetService(SimpleNamespace(name=repository.project, documents_dir=tmp_path,
                                          data_dir=tmp_path / "data"), repository)
    digest = "a" * 64
    facts = OcrResultFacts(digest, "b" * 64, "en", "easyocr", "1.7.2", "c" * 64, 1,
                           20, 10, "text", "Uncataloged distinctive123 token")
    for path in ("screens/a.png", "screens/b.png"):
        await repository.publish_ocr_result(
            OcrSourceSnapshot(path, digest, 100, 20, 10, "synthetic-stat"), facts,
            [OcrSearchChunk(0, facts.text, [0.2] * 8)])
    for alpha in (0, 0.3, 1):
        vector = [0.2] * 8 if alpha else None
        hits = await repository.search_hybrid("distinctive123", vector, 10, "screens/", None, alpha)
        assert {row["source"] for row in hits} == {"screens/a.png", "screens/b.png"}
        assert all(row["provenance"] == "ocr" and row["ocr_source_sha256"] == digest for row in hits)
        assert all(row["asset_id"] == "" and row["final_sha256"] == digest for row in hits)
        assert all(row["metadata_storage"] == "catalog" for row in hits)
        for detail in ("full", "summary"):
            payload = {"status": "success", "project": repository.project,
                       "results": [service._project(row, detail, include_score=True) for row in hits]}
            validate_structured_payload("search_assets", payload)
            encoded = build_tool_result("search_assets", payload)
            assert encoded["structuredContent"] == payload and not encoded["isError"]
        assert len(await repository.search_hybrid("distinctive123", vector, 10, "screens/a", None, alpha)) == 1
        assert await repository.search_hybrid("distinctive123", vector, 10, None, ["missing-tag"], alpha) == []
    # A present catalog row with a different hash must fail closed, not become
    # indistinguishable from an absent catalog row after an optional join.
    item = record("screens/a.png")
    await repository.replace_asset(item, "catalog metadata", [[0.1] * 8])
    # Model a stale mapping left by an external catalog update: normal asset
    # replacement already detaches it atomically, covered above.
    await repository.pool.execute(
        f"INSERT INTO {repository.schema}.asset_ocr_sources "
        "(filepath,source_sha256,pipeline_fingerprint,languages_key,file_size,width,height,snapshot_token) "
        "VALUES($1,$2,$3,'en',100,20,10,'synthetic-stat')", item.filepath, digest, "b" * 64)
    for alpha in (0, 0.3, 1):
        hits = await repository.search_hybrid("distinctive123", [0.2] * 8 if alpha else None,
                                             10, None, None, alpha)
        ocr_hits = [row for row in hits if row.get("provenance") == "ocr"]
        assert [row["source"] for row in ocr_hits] == ["screens/b.png"]
