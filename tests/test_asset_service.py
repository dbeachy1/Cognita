import asyncio
import base64
import hashlib
import struct
import zlib
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from cognita.assets.limits import MAX_PNG_BYTES
from cognita.assets.models import AssetError
from cognita.assets.service import AssetService

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


def _png_chunk(kind: bytes, payload: bytes) -> bytes:
    return (
        struct.pack(">I", len(payload))
        + kind
        + payload
        + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
    )


def _synthetic_png(size: int, *, width: int = 1320, height: int = 2868) -> bytes:
    """Build a structurally valid non-personal PNG of an exact compressed size."""
    base = (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
        + _png_chunk(b"IDAT", zlib.compress(b"\x00\x00\x00\x00\x00"))
        + _png_chunk(b"IEND", b"")
    )
    padding = size - len(base) - 12
    assert padding >= 0
    return base[:-12] + _png_chunk(b"tEXt", b"x" * padding) + base[-12:]


def test_service_round_trip(tmp_path: Path):
    class Project:
        name = "test"
        documents_dir = tmp_path
        data_dir = tmp_path / "data"

    async def run():
        service = AssetService(Project())
        result = await service.put_asset(
            {
                "filepath": "art/a.png",
                "image": {
                    "image_url": "data:image/png;base64," + base64.b64encode(PNG).decode(),
                    "output_hint": "ignored",
                },
                "metadata": {"title": "A"},
                "operation_id": "put-1",
            }
        )
        assert result["received_sha256"]
        info = await service.get_asset_info({"filepath": "art/a.png"})
        assert result["asset_id"] == info["asset_id"] == info["metadata"]["asset_id"]
        assert "score" not in info and "search_method" not in info
        assert info["width"] == 1
        image = await service.get_asset({"filepath": "art/a.png"})
        assert "score" not in image["structuredContent"]
        assert "search_method" not in image["structuredContent"]
        assert (
            image["content"][1]["data"]
            == base64.b64encode((tmp_path / "art/a.png").read_bytes()).decode()
        )
        empty = await service.search_assets({"query": "absent marker", "hybrid_alpha": 0})
        assert empty == {
            "status": "success", "project": "test", "results": [],
            "reason": "no_matches",
        }

    asyncio.run(run())


class _Embedder:
    def embed(self, texts):
        return [[1.0] for _ in texts]


class _FailingRepository:
    def __init__(self, replay=None):
        self.replay = replay
        self.failed = None

    async def get(self, filepath):
        return None

    async def claim_operation(self, operation_id, tool, fingerprint):
        if self.replay is not None:
            return "replay", self.replay
        return "claimed", None

    async def commit_asset_operation(self, *args):
        raise RuntimeError("database unavailable")

    async def finish_operation(self, operation_id, result, *, failed=False):
        self.failed = (operation_id, result, failed)


class _RecordLike:
    """Small asyncpg.Record-shaped row without Mapping registration."""

    def __init__(self, **values):
        self._values = values

    def __getitem__(self, key):
        return self._values[key]


def _args(operation_id="put-1", filepath="art/a.png"):
    return {
        "filepath": filepath,
        "image": {"image_url": "data:image/png;base64," + base64.b64encode(PNG).decode()},
        "metadata": {"title": "A"},
        "operation_id": operation_id,
    }


def test_catalog_failure_rolls_back_new_publication(tmp_path):
    class Project:
        name = "test"
        documents_dir = tmp_path
        data_dir = tmp_path / "data"

    async def run():
        repository = _FailingRepository()
        service = AssetService(Project(), repository, embedder=_Embedder())
        try:
            await service.put_asset(_args())
        except AssetError as exc:
            assert exc.reason == "internal_error"
        else:
            raise AssertionError("catalog failure should fail the tool")
        assert not (tmp_path / "art/a.png").exists()
        assert repository.failed == (
            "put-1",
            {"status": "error", "reason": "internal_error", "message": "asset operation failed"},
            True,
        )
        assert not list(service.publisher.journal_dir.glob("*.json"))

    asyncio.run(run())


def test_committed_replay_happens_before_destination_checks(tmp_path):
    class Project:
        name = "test"
        documents_dir = tmp_path
        data_dir = tmp_path / "data"

    async def run():
        stored = '{"status":"success","filepath":"art/a.png"}'
        service = AssetService(Project(), _FailingRepository(stored), embedder=_Embedder())
        result = await service.put_asset(_args())
        assert result == {
            "status": "success", "filepath": "art/a.png", "idempotent_replay": True,
        }
        assert not (tmp_path / "art/a.png").exists()

    asyncio.run(run())


def test_malformed_stored_replay_fails_safely_without_logging_payload(tmp_path):
    class Project:
        name = "test"
        documents_dir = tmp_path
        data_dir = tmp_path / "data"

    class Logger:
        def __init__(self):
            self.events = []

        def info(self, event, *, extra):
            self.events.append((event, extra))

    async def run():
        logger = Logger()
        stored = '{"status":"success","secret":"not-terminated"'
        service = AssetService(
            Project(), _FailingRepository(stored), embedder=_Embedder(), logger=logger,
        )
        try:
            await service.update_asset_metadata(
                {
                    "filepath": "art/a.png",
                    "expected_sha256": "a" * 64,
                    "metadata": {"title": "ignored"},
                    "operation_id": "replay-1",
                }
            )
        except AssetError as exc:
            assert exc.reason == "internal_error"
        else:
            raise AssertionError("malformed replay JSON should fail closed")

        assert logger.events == [
            (
                "asset.operation.replay",
                {
                    "asset": {
                        "project": "test",
                        "connector_id": None,
                        "tool": "update_asset_metadata",
                        "operation_id": "replay-1",
                    }
                },
            ),
            (
                "asset.catalog.malformed_json",
                {
                    "asset": {
                        "field": "update_asset_metadata.replay",
                        "value_type": "string",
                    }
                },
            ),
        ]
        assert stored not in repr(logger.events)

    asyncio.run(run())


def test_asyncpg_record_rows_preserve_catalog_fields_and_replay_json(tmp_path):
    class Project:
        name = "test"
        documents_dir = tmp_path
        data_dir = tmp_path / "data"

    class Repository:
        def __init__(self):
            self.row = _RecordLike(
                asset_id="asset-1", source="art/a.png", metadata='{"title":"Catalog title",'
                '"description":"keyword description","tags":["needle"]}',
                width=1, height=1, file_sha256="a" * 64, metadata_storage="catalog",
                provenance_state="none", metadata_revision=7, received_size=len(PNG),
                received_sha256="b" * 64, final_size=len(PNG), final_sha256="a" * 64,
            )

        async def get(self, filepath):
            return self.row if filepath == "art/a.png" else None

        async def list_assets(self, prefix, limit, after):
            return [self.row]

        async def search_hybrid(self, *args):
            return [self.row]

        async def claim_operation(self, operation_id, tool, fingerprint):
            return "replay", '{"status":"success","asset_id":"asset-1"}'

    async def run():
        (tmp_path / "art").mkdir()
        (tmp_path / "art/a.png").write_bytes(PNG)
        repository = Repository()
        service = AssetService(Project(), repository, embedder=_Embedder())

        listed = await service.list_assets({})
        assert listed["assets"][0]["asset_id"] == "asset-1"
        assert listed["assets"][0]["title"] == "Catalog title"
        info = await service.get_asset_info({"filepath": "art/a.png"})
        assert info["asset_id"] == "asset-1"
        assert info["metadata"]["tags"] == ["needle"]
        # A non-default value proves keyed access reads the asyncpg-shaped row
        # instead of silently returning get_asset_info's fallback revision.
        assert info["metadata_revision"] == 7
        searched = await service.search_assets({"query": "keyword", "hybrid_alpha": 0})
        assert searched["results"][0]["description"] == "keyword description"

        replay = await service.update_asset_metadata(
            {"filepath": "art/a.png", "expected_sha256": "a" * 64,
             "metadata": {"title": "ignored"}, "operation_id": "replay-1"}
        )
        assert replay == {
            "status": "success", "asset_id": "asset-1", "idempotent_replay": True,
        }
        reindexed = await service.reindex_assets({"operation_id": "replay-2"})
        assert reindexed["idempotent_replay"] is True

    asyncio.run(run())


def test_asyncpg_record_metadata_update_increments_revision(tmp_path):
    class Project:
        name = "test"
        documents_dir = tmp_path
        data_dir = tmp_path / "data"

    asset_id = str(uuid4())

    class Repository:
        def __init__(self):
            self.row = _RecordLike(
                asset_id=asset_id, source="art/a.png",
                metadata='{"title":"old","tags":["old"]}',
                width=1, height=1, file_sha256=hashlib.sha256(PNG).hexdigest(),
                metadata_storage="catalog", provenance_state="none", metadata_revision=1,
                received_size=len(PNG), received_sha256="b" * 64,
                final_size=len(PNG), final_sha256=hashlib.sha256(PNG).hexdigest(),
            )
            self.committed = None

        async def get(self, filepath):
            return self.row

        async def claim_operation(self, operation_id, tool, fingerprint):
            return "claimed", None

        async def commit_asset_operation(self, record, *args, **kwargs):
            self.committed = record

    async def run():
        (tmp_path / "art").mkdir()
        (tmp_path / "art/a.png").write_bytes(PNG)
        repository = Repository()
        service = AssetService(Project(), repository, embedder=_Embedder())
        result = await service.update_asset_metadata(
            {"filepath": "art/a.png", "expected_sha256": hashlib.sha256(PNG).hexdigest(),
             "metadata": {"title": "new"}, "metadata_action": "merge",
             "operation_id": "update-1"}
        )
        assert result["asset_id"] == asset_id
        assert result["metadata_revision"] == 2
        assert repository.committed.metadata["title"] == "new"
        assert repository.committed.metadata_revision == 2

    asyncio.run(run())


def test_asset_paths_refuse_backup_tree_and_reserved_names(tmp_path):
    class Project:
        name = "test"
        documents_dir = tmp_path
        data_dir = tmp_path / "data"

    service = AssetService(Project())
    for filepath in ("backups/private.png", "NUL.png", "../escape.png"):
        try:
            service._target(filepath)
        except AssetError as exc:
            assert exc.reason in {"invalid_path", "unsupported_media_type"}
        else:
            raise AssertionError(f"unsafe path accepted: {filepath}")


def test_asset_paths_accept_safe_dot_prefixed_directories(tmp_path):
    class Project:
        name = "test"
        documents_dir = tmp_path
        data_dir = tmp_path / "data"

    target = tmp_path / ".obsidian" / "plugins" / "theme" / "normal.png"
    target.parent.mkdir(parents=True)
    target.write_bytes(PNG)

    relative, resolved = AssetService(Project())._target(
        ".obsidian/plugins/theme/normal.png"
    )

    assert relative == ".obsidian/plugins/theme/normal.png"
    assert resolved == target.resolve()

    async def reconcile():
        service = AssetService(Project())
        result = await service.reconcile_all()
        listed = await service.list_assets({"max_results": 10})
        assert result == {
            "status": "success",
            "project": "test",
            "indexed": 1,
            "removed": 0,
            "errors": [],
        }
        assert [asset["filepath"] for asset in listed["assets"]] == [
            ".obsidian/plugins/theme/normal.png"
        ]

    asyncio.run(reconcile())


def test_list_assets_cursor_is_opaque_and_stable(tmp_path):
    class Project:
        name = "test"
        documents_dir = tmp_path
        data_dir = tmp_path / "data"

    async def run():
        service = AssetService(Project())
        await service.put_asset(_args("one", "b.png"))
        await service.put_asset(_args("two", "a.png"))
        first = await service.list_assets({"max_results": 1})
        assert [item["filepath"] for item in first["assets"]] == ["a.png"]
        second = await service.list_assets({"max_results": 1, "cursor": first["next_cursor"]})
        assert [item["filepath"] for item in second["assets"]] == ["b.png"]

    asyncio.run(run())


def test_configured_lower_png_limit_is_enforced_before_staging(tmp_path):
    class Project:
        name = "test"
        documents_dir = tmp_path
        data_dir = tmp_path / "data"

    async def run():
        service = AssetService(Project(), limits=SimpleNamespace(asset_max_png_bytes=8))
        try:
            await service.put_asset(_args())
        except AssetError as exc:
            assert exc.reason == "encoded_limit"
        else:
            raise AssertionError("configured lower PNG limit was ignored")
        assert not list(service.publisher.staging_dir.glob("*"))

    asyncio.run(run())


def test_large_png_put_update_and_local_info_keep_inline_output_bounded(tmp_path):
    class Project:
        name = "TheLargerVoice"
        documents_dir = tmp_path
        data_dir = tmp_path / "data"

    async def run():
        raw = _synthetic_png(2 * 1_048_576)
        service = AssetService(Project())
        result = await service.put_asset({
            "filepath": "Other Files/20250411_193315000_iOS.png",
            "image": {"image_url": "data:image/png;base64," + base64.b64encode(raw).decode()},
            "metadata": {"source": {"type": "imported"}},
            "metadata_storage": "catalog",
            "operation_id": "large-put",
        })
        assert result["final_size"] == len(raw)
        info = await service.get_asset_info({"filepath": result["filepath"]})
        assert (info["width"], info["height"]) == (1320, 2868)
        updated = await service.update_asset_metadata({
            "filepath": result["filepath"],
            "metadata": {"title": "Synthetic letter scan"},
            "metadata_action": "merge",
            "metadata_storage": "catalog",
            "expected_sha256": result["final_sha256"],
            "operation_id": "large-update",
        })
        assert updated["metadata_revision"] == 2
        try:
            await service.get_asset({"filepath": result["filepath"]})
        except AssetError as exc:
            assert exc.reason == "byte_limit"
            assert "inline response" in exc.message
        else:
            raise AssertionError("large catalog asset escaped the inline response limit")

    asyncio.run(run())


def test_existing_larger_voice_pngs_backfill_idempotently_without_byte_changes(tmp_path):
    class Project:
        name = "TheLargerVoice"
        documents_dir = tmp_path
        data_dir = tmp_path / "data"

    fixtures = {
        "20250411_193228000_iOS.png": 499_529,
        "20250411_193238000_iOS.png": 1_240_500,
        "20250411_193300000_iOS.png": 6_434_505,
        "20250411_193308000_iOS.png": 7_090_101,
        "20250411_193315000_iOS.png": 1_580_412,
        "20250411_195954000_iOS.png": 1_216_463,
        "4_Sophia_age_10_letter_27_Jan_2003.png": 279_573,
    }

    async def run():
        folder = tmp_path / "Other Files"
        folder.mkdir()
        before = {}
        for name, size in fixtures.items():
            path = folder / name
            path.write_bytes(_synthetic_png(size))
            before[name] = hashlib.sha256(path.read_bytes()).hexdigest()

        service = AssetService(Project())
        first = await service.reconcile_all()
        listed = await service.list_assets({"max_results": 100})
        first_ids = {item["filepath"]: item["asset_id"] for item in listed["assets"]}
        second = await service.reconcile_all()
        repeated = await service.list_assets({"max_results": 100})

        assert first == {"status": "success", "project": "TheLargerVoice",
                         "indexed": 7, "removed": 0, "errors": []}
        assert second["indexed"] == 7 and second["errors"] == []
        assert len(first_ids) == 7
        assert {item["filepath"]: item["asset_id"] for item in repeated["assets"]} == first_ids
        assert all(item["width"] == 1320 and item["height"] == 2868
                   for item in repeated["assets"])
        assert {
            path.name: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in folder.glob("*.png")
        } == before

    asyncio.run(run())


def test_reconcile_rejects_png_over_sixteen_mib(tmp_path):
    class Project:
        name = "test"
        documents_dir = tmp_path
        data_dir = tmp_path / "data"

    async def run():
        path = tmp_path / "too-large.png"
        path.write_bytes(_synthetic_png(MAX_PNG_BYTES + 1))
        service = AssetService(Project())
        result = await service.reconcile_all()
        assert result["indexed"] == 0
        assert result["errors"] == [{"filepath": "too-large.png", "reason": "byte_limit"}]
        assert (await service.list_assets({}))["assets"] == []

    asyncio.run(run())

def test_search_uses_reranker_and_normalizes_display_scores(tmp_path):
    class Project:
        name = "test"
        documents_dir = tmp_path
        data_dir = tmp_path / "data"

    class Repository:
        async def search_hybrid(self, *args):
            return [
                {"asset_id": "a", "source": "a.png", "metadata": {}, "content": "low"},
                {"asset_id": "b", "source": "b.png", "metadata": {}, "content": "high"},
            ]

    class Reranker:
        def rerank(self, query, texts):
            assert texts == ["low", "high"]
            return [-2.0, 2.0]

    async def run():
        service = AssetService(Project(), Repository(), reranker=Reranker())
        result = await service.search_assets({"query": "image", "hybrid_alpha": 0})
        assert [hit["asset_id"] for hit in result["results"]] == ["b", "a"]
        assert [hit["score"] for hit in result["results"]] == [1.0, 0.0]

    asyncio.run(run())


def test_legacy_project_writable_flag_does_not_veto_connector_policy(tmp_path):
    class Project:
        name = "test"
        documents_dir = tmp_path
        data_dir = tmp_path / "data"
        writable = False

    async def run():
        service = AssetService(Project())
        result = await service.put_asset(_args())
        assert result["status"] == "success"
        assert (tmp_path / "art/a.png").is_file()

    asyncio.run(run())


def test_recovery_fails_the_original_connector_operation_claim(tmp_path):
    class Project:
        name = "test"
        documents_dir = tmp_path
        data_dir = tmp_path / "data"

    class Repository:
        def __init__(self):
            self.finished = None

        async def get_operation(self, operation_id, *, connector_id=None, tool=None):
            assert (operation_id, connector_id, tool) == (
                "recover-op", "connector-a", "put_asset"
            )
            return {"state": "running", "tool": tool}

        async def finish_operation(
            self, operation_id, result, *, failed=False, connector_id=None, tool=None,
        ):
            self.finished = (operation_id, result, failed, connector_id, tool)

        async def prune_operations(self):
            return None

    async def run():
        repository = Repository()
        service = AssetService(Project(), repository)
        staged = service.publisher.stage(
            "recover-op", PNG, connector_id="connector-a", tool="put_asset"
        )
        service.publisher.publish(
            staged, "recover.png", operation_id="recover-op",
            connector_id="connector-a", tool="put_asset",
        )
        await service.recover()
        assert not (tmp_path / "recover.png").exists()
        assert repository.finished == (
            "recover-op",
            {
                "status": "error", "reason": "publication_failed",
                "message": "asset publication did not complete before the server restarted",
            },
            True, "connector-a", "put_asset",
        )

    asyncio.run(run())
