"""Targeted PNG reconciliation contracts for Cognita 11.4."""

import asyncio
import base64
import logging
import shutil
import struct
import zlib
from pathlib import Path

import pytest

from cognita.assets.models import AssetError
from cognita.assets.service import AssetService


PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


class _Project:
    name = "targeted-assets"

    def __init__(self, documents_dir: Path):
        self.documents_dir = documents_dir
        self.data_dir = documents_dir / "data"


def test_directory_reconcile_adds_updates_and_removes_only_its_prefix(tmp_path):
    async def run():
        affected = tmp_path / "affected"
        sibling = tmp_path / "sibling"
        affected.mkdir()
        sibling.mkdir()
        first = affected / "first.png"
        second = affected / "second.png"
        untouched = sibling / "untouched.png"
        first.write_bytes(PNG)
        second.write_bytes(PNG)
        untouched.write_bytes(PNG)
        service = AssetService(_Project(tmp_path))

        initial = await service.reconcile_paths((), ("affected", "sibling"))
        assert initial["indexed"] == 3
        payload = b"variant\0value"
        chunk = (
            struct.pack(">I", len(payload)) + b"tEXt" + payload
            + struct.pack(">I", zlib.crc32(b"tEXt" + payload) & 0xFFFFFFFF)
        )
        first.write_bytes(PNG[:-12] + chunk + PNG[-12:])
        second.unlink()
        added = affected / "added.png"
        added.write_bytes(PNG)

        result = await service.reconcile_paths((), ("affected",))
        assert result["indexed"] == 2
        assert result["removed"] == 1
        listed = await service.list_assets({"max_results": 20})
        assert {item["filepath"] for item in listed["assets"]} == {
            "affected/added.png", "affected/first.png", "sibling/untouched.png",
        }

    asyncio.run(run())


def test_targeted_reconcile_root_safety_fails_closed(tmp_path):
    async def run():
        path = tmp_path / "safe.png"
        path.write_bytes(PNG)
        service = AssetService(_Project(tmp_path))
        await service.reconcile_paths(("safe.png",))
        service._documents_root_identity = (0, 0)
        with pytest.raises(AssetError) as caught:
            await service.reconcile_paths((), ("",))
        assert caught.value.reason == "root_changed"
        assert (await service.list_assets({}))["assets"]

    asyncio.run(run())


def test_missing_root_never_removes_catalog_rows(tmp_path):
    async def run():
        path = tmp_path / "safe.png"
        path.write_bytes(PNG)
        service = AssetService(_Project(tmp_path))
        await service.reconcile_paths(("safe.png",))
        path.unlink()
        # AssetService owns its journal/staging directories under the project
        # data root; remove that test-owned sidecar before simulating root
        # disappearance on Windows, where rmdir requires an empty directory.
        shutil.rmtree(tmp_path / "data")
        tmp_path.rmdir()
        with pytest.raises(AssetError) as caught:
            await service.reconcile_paths((), ("",))
        assert caught.value.reason == "root_unavailable"

    asyncio.run(run())


class _OutsideDataProject:
    """A project whose data lives outside documents_dir, as in production."""

    name = "empty-root-assets"

    def __init__(self, documents_dir: Path, data_dir: Path):
        self.documents_dir = documents_dir
        self.data_dir = data_dir


class _Catalog:
    """Just enough repository for reconcile_all's removal sweep."""

    def __init__(self, sources):
        self.sources = list(sources)
        self.deleted = []

    async def list_sources(self, prefix):
        return [s for s in self.sources if s.startswith(prefix)]

    async def delete_source(self, relative):
        self.deleted.append(relative)
        self.sources.remove(relative)


def _empty_root(tmp_path):
    # Installer design 22.14: an unmounted projects folder reads as an empty
    # directory, so the root exists and is readable but holds nothing.
    root = tmp_path / "mnt-root"
    root.mkdir()
    return root, tmp_path / "data"


def test_empty_root_refuses_the_whole_catalog_sweep(tmp_path):
    async def run():
        root, data = _empty_root(tmp_path)
        catalog = _Catalog(["a.png", "art/b.png"])
        service = AssetService(_OutsideDataProject(root, data), catalog)
        with pytest.raises(AssetError) as caught:
            await service.reconcile_all()
        assert caught.value.reason == "root_empty"
        assert catalog.deleted == []
        assert catalog.sources == ["a.png", "art/b.png"]

    asyncio.run(run())


def test_empty_root_refuses_the_targeted_root_sweep(tmp_path):
    async def run():
        root, data = _empty_root(tmp_path)
        (root / "safe.png").write_bytes(PNG)
        service = AssetService(_OutsideDataProject(root, data))
        await service.reconcile_paths(("safe.png",))
        (root / "safe.png").unlink()
        with pytest.raises(AssetError) as caught:
            await service.reconcile_paths((), ("",))
        assert caught.value.reason == "root_empty"
        assert {item["filepath"] for item in (await service.list_assets({}))["assets"]} == {"safe.png"}

    asyncio.run(run())


def test_a_root_with_other_files_still_retires_deleted_pngs(tmp_path):
    async def run():
        root, data = _empty_root(tmp_path)
        (root / "notes.md").write_text("still here")
        catalog = _Catalog(["gone.png"])
        service = AssetService(_OutsideDataProject(root, data), catalog)
        result = await service.reconcile_all()
        assert result["removed"] == 1
        assert catalog.deleted == ["gone.png"]

    asyncio.run(run())


def test_an_empty_root_with_an_empty_catalog_is_a_quiet_success(tmp_path):
    async def run():
        root, data = _empty_root(tmp_path)
        result = await AssetService(_OutsideDataProject(root, data), _Catalog([])).reconcile_all()
        assert result["status"] == "success"
        assert result["removed"] == 0

    asyncio.run(run())


def test_full_reconcile_keeps_the_row_of_a_present_file_that_fails_to_read(tmp_path, monkeypatch):
    async def run():
        root, data = _empty_root(tmp_path)
        (root / "busy.png").write_bytes(PNG)
        catalog = _Catalog(["busy.png"])
        service = AssetService(_OutsideDataProject(root, data), catalog)

        def failed_read(_target):
            # _read_png maps every OSError (EIO, EACCES, a OneDrive rewrite) to not_found.
            raise AssetError("not_found", "asset could not be read")

        monkeypatch.setattr(service, "_read_png", failed_read)
        result = await service.reconcile_all()
        assert catalog.deleted == []
        assert result["removed"] == 0
        assert result["errors"] == [{"filepath": "busy.png", "reason": "read_failed"}]

    asyncio.run(run())


def test_noop_asset_reconciliation_is_debug_but_changes_and_errors_are_info(
    tmp_path, monkeypatch, caplog,
):
    async def run():
        root, data = _empty_root(tmp_path)
        path = root / "same.png"
        path.write_bytes(PNG)
        logger = logging.getLogger("cognita.assets.reconcile_test")
        service = AssetService(_OutsideDataProject(root, data), logger=logger)
        await service.reconcile_paths(("same.png",))

        caplog.clear()
        with caplog.at_level(logging.DEBUG, logger="cognita.assets"):
            no_op = await service.reconcile_paths(("same.png",))
        assert no_op["skipped"] == 1
        target_log = next(record for record in caplog.records
                           if record.name == logger.name
                           and record.getMessage() == "asset.reconcile")
        assert target_log.levelno == logging.DEBUG

        caplog.clear()
        with caplog.at_level(logging.DEBUG, logger="cognita.assets"):
            empty_base = tmp_path / "empty"
            empty_base.mkdir()
            empty_root, empty_data = _empty_root(empty_base)
            empty_service = AssetService(
                _OutsideDataProject(empty_root, empty_data), _Catalog([]), logger=logger,
            )
            full_no_op = await empty_service.reconcile_all()
            assert full_no_op["indexed"] == full_no_op["removed"] == 0
            full_log = next(record for record in caplog.records
                            if record.name == logger.name
                            and record.getMessage() == "asset.reconcile")
            assert full_log.levelno == logging.DEBUG

        payload = b"key\0value"
        chunk = (struct.pack(">I", len(payload)) + b"tEXt" + payload
                 + struct.pack(">I", zlib.crc32(b"tEXt" + payload) & 0xFFFFFFFF))
        path.write_bytes(PNG[:-12] + chunk + PNG[-12:])
        caplog.clear()
        with caplog.at_level(logging.INFO, logger="cognita.assets"):
            changed = await service.reconcile_paths(("same.png",))
        assert changed["indexed"] == 1
        changed_log = next(record for record in caplog.records
                           if record.name == logger.name
                           and record.getMessage() == "asset.reconcile")
        assert changed_log.levelno == logging.INFO

        previous_digest = service._memory["same.png"].final_sha256
        path.write_bytes(PNG)

        def rejected_read(_target):
            raise AssetError("policy_rejected", "source was refused by policy")

        monkeypatch.setattr(service, "_read_png", rejected_read)
        caplog.clear()
        with caplog.at_level(logging.INFO, logger="cognita.assets"):
            rejected = await service.reconcile_paths(("same.png",))
        assert rejected["removed"] == rejected["failed"] == 0
        assert rejected["errors"] == [{"filepath": "same.png", "reason": "policy_rejected"}]
        assert service._memory["same.png"].final_sha256 == previous_digest
        error_log = next(record for record in caplog.records
                         if record.name == logger.name
                         and record.getMessage() == "asset.reconcile")
        assert error_log.levelno == logging.INFO
        assert error_log.asset["error_count"] == 1
        assert error_log.asset["error_reasons"] == ["policy_rejected"]

    asyncio.run(run())


def test_full_reconcile_retires_a_file_that_is_really_gone(tmp_path, monkeypatch):
    async def run():
        root, data = _empty_root(tmp_path)
        (root / "notes.md").write_text("keeps the root non-empty")
        path = root / "going.png"
        path.write_bytes(PNG)
        catalog = _Catalog(["going.png"])
        service = AssetService(_OutsideDataProject(root, data), catalog)

        def vanished(target):
            target.unlink()
            raise AssetError("not_found", "asset could not be read")

        monkeypatch.setattr(service, "_read_png", vanished)
        result = await service.reconcile_all()
        assert catalog.deleted == ["going.png"]
        assert result["removed"] == 1

    asyncio.run(run())


def test_stale_asset_read_preserves_catalog_and_requests_retry(tmp_path, monkeypatch):
    async def run():
        path = tmp_path / "changing.png"
        path.write_bytes(PNG)
        service = AssetService(_Project(tmp_path))
        await service.reconcile_paths(("changing.png",))

        def stale_read(_target):
            raise AssetError("stale_file", "asset changed while it was being read")

        path.write_bytes(PNG + b"changing")
        monkeypatch.setattr(service, "_read_png", stale_read)
        result = await service.reconcile_paths(("changing.png",))

        assert result["removed"] == 0
        assert result["failed"] == 1
        assert result["retryable_failures"] == ["changing.png"]
        assert {item["filepath"] for item in (await service.list_assets({}))["assets"]} == {
            "changing.png"
        }

    asyncio.run(run())


def test_asset_changed_between_stat_and_read_is_not_published(tmp_path, monkeypatch):
    async def run():
        path = tmp_path / "racing.png"
        path.write_bytes(PNG)
        service = AssetService(_Project(tmp_path))
        await service.reconcile_paths(("racing.png",))
        previous = service._memory["racing.png"].final_sha256
        path.write_bytes(PNG + b"first-change")
        original_read = service._read_png

        def racing_read(target):
            data, digest = original_read(target)
            target.write_bytes(PNG + b"second-change-is-longer")
            return data, digest

        monkeypatch.setattr(service, "_read_png", racing_read)
        result = await service.reconcile_paths(("racing.png",))

        assert result["failed"] == 1
        assert result["retryable_failures"] == ["racing.png"]
        assert service._memory["racing.png"].final_sha256 == previous

    asyncio.run(run())
