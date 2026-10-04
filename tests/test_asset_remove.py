import asyncio
import base64
import hashlib
from pathlib import Path

import pytest

from cognita.assets.models import AssetError
from cognita.assets.service import AssetService


PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


class Project:
    name = "remove-test"

    def __init__(self, root: Path):
        self.documents_dir = root
        self.data_dir = root / "data"


class Catalog:
    def __init__(self, *, row=None, ocr=None, fail=False):
        self.row = row
        self.ocr = ocr
        self.fail = fail
        self.deleted = False
        self.finished = []

    async def get(self, filepath):
        return self.row if not self.deleted else None

    async def get_ocr_source(self, filepath):
        return self.ocr if not self.deleted else None

    async def claim_operation(self, operation_id, tool, fingerprint, **kwargs):
        return "claimed", None

    async def commit_asset_removal(self, filepath, operation_id, result, **kwargs):
        if self.fail:
            raise RuntimeError("derived store unavailable")
        self.deleted = True

    async def finish_operation(self, operation_id, result, *, failed=False, **kwargs):
        self.finished.append((operation_id, result, failed))


class StatefulOperationRepository:
    """A minimal in-memory analogue of the Postgres ``asset_operations``
    claim/finish state machine (see ``assets/repository.py``'s
    ``claim_operation``/``finish_operation``): a fingerprint mismatch is
    ``conflict``, a matching fingerprint against a completed row replays
    ``committed`` or ``failed`` state.  Deliberately has no
    ``commit_asset_removal``/``delete_source``/``detach_source`` so
    ``remove_asset`` falls back to the plain ``_finish()`` path, keeping
    this fake small while still exercising the real claim/finish contract
    (12.18.5: a retry with the same operation_id and arguments must replay
    the ORIGINAL failure, not a generic operation_conflict).
    """

    def __init__(self):
        self._ops: dict[tuple[str, str], dict] = {}

    async def get(self, filepath):
        return None

    async def get_ocr_source(self, filepath):
        return None

    async def claim_operation(self, operation_id, tool, fingerprint, **kwargs):
        key = (tool, operation_id)
        existing = self._ops.get(key)
        if existing is None:
            self._ops[key] = {"fingerprint": fingerprint, "state": "running", "result": None}
            return "claimed", None
        if existing["fingerprint"] != fingerprint:
            return "conflict", None
        if existing["state"] == "committed":
            return "replay", existing["result"]
        if existing["state"] == "failed":
            return "failed", existing["result"]
        return "busy", None

    async def finish_operation(self, operation_id, result, *, failed=False, tool=None, **kwargs):
        key = (tool, operation_id)
        if key not in self._ops:
            key = next(k for k in self._ops if k[1] == operation_id)
        self._ops[key]["state"] = "failed" if failed else "committed"
        self._ops[key]["result"] = dict(result)


def _service(tmp_path, repository=None):
    return AssetService(Project(tmp_path), repository)


def test_remove_asset_success_keeps_exact_backup_and_canaries(tmp_path):
    async def run():
        for name in ("a.png", "b.png", "disposable.png"):
            (tmp_path / name).write_bytes(PNG)
        service = _service(tmp_path)
        await service.reconcile_all()
        result = await service.remove_asset({
            "filepath": "disposable.png", "expected_sha256": hashlib.sha256(PNG).hexdigest(),
            "operation_id": "remove-1",
        })
        assert result["file_deleted"] is True
        assert result["catalog_removed"] is True
        assert result["ocr_removed"] is False
        assert result["deleted_size"] == len(PNG)
        assert result["deleted_sha256"] == hashlib.sha256(PNG).hexdigest()
        assert result["backup_id"]
        backup = next((tmp_path / "backups").rglob("disposable.*.png"))
        assert backup.read_bytes() == PNG
        assert not (tmp_path / "disposable.png").exists()
        assert (tmp_path / "a.png").read_bytes() == PNG
        assert (tmp_path / "b.png").read_bytes() == PNG

    asyncio.run(run())


def test_remove_asset_stale_and_missing(tmp_path):
    async def run():
        target = tmp_path / "stale.png"
        target.write_bytes(PNG)
        service = _service(tmp_path)
        try:
            await service.remove_asset({
                "filepath": "stale.png", "expected_sha256": "0" * 64, "operation_id": "stale-1",
            })
        except AssetError as exc:
            assert exc.reason == "stale_file"
        else:
            pytest.fail("stale hash was accepted")
        assert target.exists() and not (tmp_path / "backups").exists()
        try:
            await service.remove_asset({"filepath": "missing.png", "operation_id": "missing-1"})
        except AssetError as exc:
            assert exc.reason == "not_found"
        else:
            pytest.fail("missing asset was reported as success")

    asyncio.run(run())


def test_remove_asset_reconciles_catalog_only_and_disk_only(tmp_path):
    async def run():
        catalog = Catalog(row={"asset_id": "catalog-only"})
        service = _service(tmp_path, catalog)
        result = await service.remove_asset({"filepath": "catalog.png", "operation_id": "cat-1"})
        assert result["file_deleted"] is False and result["catalog_removed"] is True
        assert result["deleted_sha256"] is None and result["backup_id"] is None

        disk = tmp_path / "disk.png"
        disk.write_bytes(PNG)
        catalog = Catalog()
        service = _service(tmp_path, catalog)
        result = await service.remove_asset({"filepath": "disk.png", "operation_id": "disk-1"})
        assert result["file_deleted"] is True and result["catalog_removed"] is False
        assert not disk.exists()

    asyncio.run(run())


def test_remove_asset_backup_failure_does_not_mutate(tmp_path, monkeypatch):
    async def run():
        target = tmp_path / "asset.png"
        target.write_bytes(PNG)
        service = _service(tmp_path)

        def fail(_relative):
            raise AssetError("backup_failed", "backup unavailable")

        monkeypatch.setattr(service.publisher, "backup_for_delete", fail)
        with pytest.raises(AssetError, match="backup unavailable") as raised:
            await service.remove_asset({"filepath": "asset.png", "operation_id": "backup-1"})
        assert raised.value.reason == "backup_failed"
        assert target.read_bytes() == PNG

    asyncio.run(run())


def test_remove_asset_ocr_detachment_and_rollback(tmp_path):
    async def run():
        detached = tmp_path / "detached.png"
        detached.write_bytes(PNG)
        catalog_ok = Catalog(row={"asset_id": "detached"}, ocr={"filepath": "detached.png"})
        service_ok = _service(tmp_path, catalog_ok)
        result = await service_ok.remove_asset({"filepath": "detached.png", "operation_id": "ocr-ok"})
        assert result["ocr_removed"] is True and result["catalog_removed"] is True

        target = tmp_path / "ocr.png"
        target.write_bytes(PNG)
        catalog = Catalog(row={"asset_id": "ocr"}, ocr={"filepath": "ocr.png"}, fail=True)
        service = _service(tmp_path, catalog)
        with pytest.raises(AssetError) as raised:
            await service.remove_asset({"filepath": "ocr.png", "operation_id": "ocr-1"})
        assert raised.value.reason == "internal_error"
        assert raised.value.details == {
            "operation_outcome": "rolled_back", "backup_id": raised.value.details["backup_id"],
        }
        assert target.read_bytes() == PNG

    asyncio.run(run())


def test_remove_asset_unknown_compensation_includes_backup(tmp_path, monkeypatch):
    async def run():
        target = tmp_path / "unknown.png"
        target.write_bytes(PNG)
        catalog = Catalog(row={"asset_id": "unknown"}, fail=True)
        service = _service(tmp_path, catalog)

        def fail_restore(*_args):
            raise AssetError("recovery_required", "restore unavailable")

        monkeypatch.setattr(service.publisher, "rollback_delete", fail_restore)
        with pytest.raises(AssetError) as raised:
            await service.remove_asset({"filepath": "unknown.png", "operation_id": "unknown-1"})
        assert raised.value.reason == "recovery_required"
        assert raised.value.details["operation_outcome"] == "unknown"
        assert raised.value.details["backup_id"]

    asyncio.run(run())


def test_remove_asset_replay_is_stable_and_connector_scoped(tmp_path):
    async def run():
        target = tmp_path / "replay.png"
        target.write_bytes(PNG)
        stored = {"status": "success", "project": "remove-test", "filepath": "replay.png",
                  "file_deleted": True, "catalog_removed": True, "ocr_removed": False,
                  "deleted_size": len(PNG), "deleted_sha256": hashlib.sha256(PNG).hexdigest(),
                  "backup_id": "20260916-000000", "idempotent_replay": False}

        class Replay(Catalog):
            async def claim_operation(self, *args, **kwargs):
                return "replay", stored

        service = _service(tmp_path, Replay())
        result = await service.remove_asset({"filepath": "replay.png", "operation_id": "replay-1"})
        assert result["idempotent_replay"] is True
        assert target.exists()

    asyncio.run(run())


def test_remove_asset_retry_with_same_operation_id_replays_the_original_failure(tmp_path):
    # 12.18.5 (LIVE BUG): a retry that reuses operation_id with byte-
    # identical arguments used to answer `operation_conflict` regardless of
    # what the first attempt actually did -- exactly the case the
    # idempotency contract (idempotency.py: "Errors are remembered too")
    # exists to make safe. Runs against a stateful fake that mirrors the
    # real claim/finish state machine, not a canned stub.
    async def run():
        repository = StatefulOperationRepository()
        service = _service(tmp_path, repository)

        # 1. Wrong expected_sha256 on an existing file -> stale_file, a
        # fresh (non-replayed) failure.
        target = tmp_path / "stale.png"
        target.write_bytes(PNG)
        stale_args = {"filepath": "stale.png", "expected_sha256": "0" * 64, "operation_id": "retry-stale"}
        with pytest.raises(AssetError) as first:
            await service.remove_asset(dict(stale_args))
        assert first.value.reason == "stale_file"
        assert "idempotent_replay" not in first.value.details

        # 2. The exact same call again -> the SAME failure, replayed --
        # never re-reads the file or re-attempts the backup.
        with pytest.raises(AssetError) as second:
            await service.remove_asset(dict(stale_args))
        assert second.value.reason == "stale_file"
        assert second.value.message == first.value.message
        assert second.value.details.get("idempotent_replay") is True
        assert target.exists()

        # 3. Same operation_id, a DIFFERENT filepath -> the fingerprint no
        # longer matches -> operation_conflict, not a replayed failure.
        (tmp_path / "other.png").write_bytes(PNG)
        with pytest.raises(AssetError) as conflict:
            await service.remove_asset({"filepath": "other.png", "operation_id": "retry-stale"})
        assert conflict.value.reason == "operation_conflict"

        # 4. A nonexistent path -> not_found, replayed as not_found on retry.
        missing_args = {"filepath": "missing.png", "operation_id": "retry-missing"}
        with pytest.raises(AssetError) as missing_first:
            await service.remove_asset(dict(missing_args))
        assert missing_first.value.reason == "not_found"
        with pytest.raises(AssetError) as missing_second:
            await service.remove_asset(dict(missing_args))
        assert missing_second.value.reason == "not_found"
        assert missing_second.value.details.get("idempotent_replay") is True

        # 5. Success then retry -> the stored SUCCESS result, replayed
        # unchanged (the existing idempotent-replay contract, unaffected by
        # this fix -- proven here end to end through the same stateful
        # repository rather than a canned "replay" stub).
        ok_target = tmp_path / "ok.png"
        ok_target.write_bytes(PNG)
        ok_args = {"filepath": "ok.png", "operation_id": "retry-ok"}
        first_ok = await service.remove_asset(dict(ok_args))
        assert first_ok["idempotent_replay"] is False
        second_ok = await service.remove_asset(dict(ok_args))
        assert second_ok["idempotent_replay"] is True
        assert {k: v for k, v in second_ok.items() if k != "idempotent_replay"} == {
            k: v for k, v in first_ok.items() if k != "idempotent_replay"
        }

    asyncio.run(run())


def test_remove_asset_rejects_non_static_png_before_backup(tmp_path):
    async def run():
        target = tmp_path / "not-static.png"
        target.write_bytes(b"not a png")
        service = _service(tmp_path)
        with pytest.raises(AssetError) as raised:
            await service.remove_asset({"filepath": "not-static.png", "operation_id": "bad-1"})
        assert raised.value.reason == "invalid_png"
        assert target.read_bytes() == b"not a png"
        assert not (tmp_path / "backups").exists()

    asyncio.run(run())


def test_delete_journal_restores_uncommitted_atomic_detach(tmp_path):
    target = tmp_path / "journal.png"
    target.write_bytes(PNG)
    service = _service(tmp_path)
    backup = service.publisher.backup_for_delete("journal.png")
    digest = hashlib.sha256(PNG).hexdigest()
    detached = service.publisher.detach_for_delete(
        "journal.png", backup, operation_id="journal-1", expected_sha256=digest,
        tool="remove_asset",
    )
    assert not target.exists() and detached.tombstone.is_file()
    payload = service.publisher.pending()[0]
    service.publisher.recover_entry(payload, committed=False)
    assert target.read_bytes() == PNG
    assert not detached.tombstone.exists()
    assert service.publisher.pending() == []
