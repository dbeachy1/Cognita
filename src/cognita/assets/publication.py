"""Atomic PNG publication, byte-exact backups, and crash recovery journals."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import shutil
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from ..backups import BACKUPS_DIRNAME, BackupError, backup_id_of, backup_if_exists, resolve_target
from .limits import MAX_PNG_BYTES
from .models import AssetError

JOURNAL_DIRNAME = ".cognita-asset-journal"
TEMP_PREFIX = ".cognita-asset-"


def sha256_file(path: Path, *, maximum: int | None = None) -> tuple[int, str]:
    """Hash a file without loading it, optionally enforcing a byte ceiling."""
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(64 * 1024), b""):
            size += len(block)
            if maximum is not None and size > maximum:
                raise AssetError("byte_limit", "PNG exceeds the raw byte limit")
            digest.update(block)
    return size, digest.hexdigest()


def _fsync_dir(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except (OSError, AttributeError):
        pass


def _fsync_file(path: Path) -> None:
    with path.open("ab") as handle:
        handle.flush()
        os.fsync(handle.fileno())


def _private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    try:
        path.chmod(0o700)
    except OSError:
        pass


def _safe_token(
    operation_id: str, connector_id: str | None = None, tool: str | None = None,
) -> str:
    if connector_id is None and tool is None:
        # Preserve names for pre-9.0 recovery journals and staging files.
        value = operation_id
    else:
        value = f"{connector_id or ''}\0{tool or ''}\0{operation_id}"
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


@dataclass(slots=True)
class PublishedFile:
    operation_id: str
    destination: Path
    journal: Path
    staged: Path
    temp: Path
    old_exists: bool
    backup_path: Path | None
    connector_id: str | None = None
    tool: str | None = None


@dataclass(slots=True)
class DetachedFile:
    """An atomically detached deletion target retained until DB commit."""

    operation_id: str
    destination: Path
    journal: Path
    tombstone: Path
    backup_path: Path
    expected_sha256: str
    connector_id: str | None = None
    tool: str | None = None


class AssetPublisher:
    """Prepare and publish complete files while retaining recovery evidence."""

    def __init__(self, documents_dir: Path, data_dir: Path):
        self.documents_dir = Path(documents_dir).resolve()
        self.data_dir = Path(data_dir)
        self.staging_dir = self.data_dir / "assets" / "staging"
        self.journal_dir = self.data_dir / JOURNAL_DIRNAME
        _private_dir(self.staging_dir)
        _private_dir(self.journal_dir)

    def stage(
        self, operation_id: str, data: bytes, *, connector_id: str | None = None,
        tool: str | None = None,
    ) -> Path:
        if len(data) > MAX_PNG_BYTES:
            raise AssetError("byte_limit", "PNG exceeds the raw byte limit")
        path = self.staging_dir / f"{TEMP_PREFIX}{_safe_token(operation_id, connector_id, tool)}.received"
        try:
            with path.open("xb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
        except FileExistsError as exc:
            raise AssetError("busy", "operation staging already exists") from exc
        except OSError as exc:
            self.cleanup(path)
            raise AssetError("publication_failed", "could not stage PNG") from exc
        return path

    def stage_base64(
        self, operation_id: str, encoded: str, *, connector_id: str | None = None,
        tool: str | None = None,
    ) -> tuple[Path, int, str]:
        """Strictly decode standard base64 into a private file in bounded blocks."""
        path = self.staging_dir / f"{TEMP_PREFIX}{_safe_token(operation_id, connector_id, tool)}.received"
        digest = hashlib.sha256()
        size = 0
        try:
            with path.open("xb") as handle:
                for offset in range(0, len(encoded), 64 * 1024):
                    block = encoded[offset:offset + 64 * 1024]
                    try:
                        decoded = base64.b64decode(block.encode("ascii"), validate=True)
                    except (UnicodeEncodeError, binascii.Error, ValueError) as exc:
                        raise AssetError("invalid_base64", "image_url is not valid standard base64") from exc
                    size += len(decoded)
                    if size > MAX_PNG_BYTES:
                        raise AssetError("byte_limit", "PNG exceeds the raw byte limit")
                    handle.write(decoded)
                    digest.update(decoded)
                handle.flush()
                os.fsync(handle.fileno())
        except FileExistsError as exc:
            raise AssetError("busy", "operation staging already exists") from exc
        except AssetError:
            self.cleanup(path)
            raise
        except OSError as exc:
            self.cleanup(path)
            raise AssetError("publication_failed", "could not stage PNG") from exc
        return path, size, digest.hexdigest()

    def received_backup(self, staged: Path, relative_path: str) -> str:
        """Retain exact received bytes in the normal project backup tree."""
        relative = Path(relative_path)
        root = self.documents_dir / BACKUPS_DIRNAME / relative.parent
        root.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
        candidate = root / f"{relative.stem}.{stamp}{relative.suffix}"
        n = 0
        while candidate.exists():
            n += 1
            candidate = root / f"{relative.stem}.{stamp}-{n}{relative.suffix}"
        try:
            shutil.copy2(staged, candidate)
            if sha256_file(candidate) != sha256_file(staged):
                raise OSError("backup verification failed")
            return backup_id_of(candidate) or stamp
        except OSError as exc:
            self.cleanup(candidate)
            raise AssetError("backup_failed", "could not retain received asset") from exc

    def backup_for_delete(self, relative_path: str) -> Path | None:
        """Create and verify the exact-byte recovery snapshot for a deletion.

        ``backup_if_exists`` is the shared project backup implementation used by
        document writes.  Deletion needs the same naming and retention rules, but
        additionally verifies the copied bytes before the caller unlinks the
        source.  A failed copy is therefore always a hard stop before mutation.
        """
        try:
            backup = backup_if_exists(self.documents_dir, relative_path)
            if backup is None:
                return None
            target = resolve_target(self.documents_dir, relative_path)
            if target is None or sha256_file(backup, maximum=MAX_PNG_BYTES) != sha256_file(
                target, maximum=MAX_PNG_BYTES
            ):
                raise OSError("backup verification failed")
            return backup
        except AssetError:
            raise
        except (BackupError, OSError, TypeError, ValueError) as exc:
            raise AssetError("backup_failed", "could not back up existing asset") from exc

    def restore_backup_file(self, backup: Path, destination: Path) -> None:
        """Restore a deletion backup and verify its exact bytes."""
        if not backup.is_file():
            raise AssetError("recovery_required", "asset deletion backup is unavailable")
        try:
            expected = sha256_file(backup, maximum=MAX_PNG_BYTES)
            self._restore_backup(backup, destination)
            if sha256_file(destination, maximum=MAX_PNG_BYTES) != expected:
                raise OSError("restored backup verification failed")
        except AssetError:
            raise
        except (OSError, TypeError) as exc:
            raise AssetError("recovery_required", "asset deletion backup could not be restored") from exc

    def detach_for_delete(
        self, relative_path: str, backup: Path, *, operation_id: str,
        expected_sha256: str, connector_id: str | None = None,
        tool: str | None = "remove_asset",
    ) -> DetachedFile:
        """Atomically detach exactly the bytes that were backed up.

        Hashing the same-directory tombstone after the rename closes the final
        hash-to-unlink race, while the journal makes a process death before the
        database commit recoverable on the next start.
        """
        destination = resolve_target(self.documents_dir, relative_path)
        if destination is None or not destination.is_file():
            raise AssetError("not_found", "asset was not found")
        token = _safe_token(operation_id, connector_id, tool)
        tombstone = destination.parent / f"{TEMP_PREFIX}{token}.delete"
        journal = self.journal_dir / f"{token}.json"
        if tombstone.exists() or journal.exists():
            raise AssetError("busy", "asset deletion recovery state already exists")
        payload = {
            "operation_id": operation_id, "operation_kind": "delete",
            "filepath": destination.relative_to(self.documents_dir).as_posix(),
            "phase": "prepared", "tombstone": tombstone.name,
            "backup": backup.relative_to(self.documents_dir).as_posix(),
            "expected_sha256": expected_sha256,
        }
        if connector_id is not None:
            payload["connector_id"] = connector_id
        if tool is not None:
            payload["tool"] = tool
        self._write_journal(journal, payload)
        moved = False
        try:
            os.replace(destination, tombstone)
            moved = True
            _fsync_dir(destination.parent)
            if sha256_file(tombstone, maximum=MAX_PNG_BYTES)[1] != expected_sha256:
                raise AssetError("stale_file", "asset changed while it was being removed")
            payload["phase"] = "file_detached"
            self._write_journal(journal, payload)
            return DetachedFile(
                operation_id, destination, journal, tombstone, backup,
                expected_sha256, connector_id, tool,
            )
        except Exception as exc:
            if moved:
                try:
                    if destination.exists():
                        raise AssetError("recovery_required", "asset path was replaced during deletion")
                    os.replace(tombstone, destination)
                    _fsync_dir(destination.parent)
                    if sha256_file(destination, maximum=MAX_PNG_BYTES)[1] != expected_sha256:
                        raise AssetError("recovery_required", "asset deletion rollback verification failed")
                except Exception as rollback_error:
                    raise AssetError("recovery_required", "asset deletion outcome requires recovery") from rollback_error
            self.cleanup(journal)
            if isinstance(exc, AssetError):
                raise
            raise AssetError("publication_failed", "asset could not be detached") from exc

    def commit_delete(self, detached: DetachedFile) -> None:
        """Discard committed detached bytes, then retire their journal."""
        self.cleanup(detached.tombstone)
        if detached.tombstone.exists():
            raise AssetError("recovery_required", "committed asset tombstone could not be removed")
        self.cleanup(detached.journal)
        _fsync_dir(detached.destination.parent)
        _fsync_dir(self.journal_dir)

    def rollback_delete(self, detached: DetachedFile) -> None:
        """Restore an uncommitted atomic deletion and verify exact bytes."""
        if detached.destination.exists():
            raise AssetError("recovery_required", "asset path was replaced during deletion rollback")
        if detached.tombstone.is_file():
            os.replace(detached.tombstone, detached.destination)
            _fsync_dir(detached.destination.parent)
        else:
            self.restore_backup_file(detached.backup_path, detached.destination)
        if sha256_file(detached.destination, maximum=MAX_PNG_BYTES)[1] != detached.expected_sha256:
            raise AssetError("recovery_required", "asset deletion rollback verification failed")
        self.cleanup(detached.journal)
        _fsync_dir(self.journal_dir)

    def publish(
        self,
        staged: Path,
        relative_path: str,
        *,
        operation_id: str,
        connector_id: str | None = None,
        tool: str | None = None,
        overwrite: bool = False,
        final_data: bytes | None = None,
    ) -> PublishedFile:
        """Replace the destination but keep the journal until catalog commit."""
        destination = resolve_target(self.documents_dir, relative_path)
        if destination is None:
            raise AssetError("invalid_path", "filepath is outside the project")
        rel = destination.relative_to(self.documents_dir)
        if rel.parts and rel.parts[0].casefold() == BACKUPS_DIRNAME.casefold():
            raise AssetError("invalid_path", "the backup tree is not an asset destination")
        destination.parent.mkdir(parents=True, exist_ok=True)
        old_exists = destination.is_file()
        if old_exists and not overwrite:
            raise AssetError("destination_exists", "destination already exists")
        backup_path: Path | None = None
        if old_exists:
            try:
                backup_path = backup_if_exists(self.documents_dir, relative_path)
                if backup_path is None or sha256_file(backup_path) != sha256_file(destination):
                    raise OSError("backup verification failed")
            except Exception as exc:
                raise AssetError("backup_failed", "could not back up existing asset") from exc
        fd, name = tempfile.mkstemp(prefix=TEMP_PREFIX, suffix=".png", dir=destination.parent)
        temp = Path(name)
        try:
            with os.fdopen(fd, "wb") as handle:
                if final_data is None:
                    with staged.open("rb") as source:
                        shutil.copyfileobj(source, handle, 64 * 1024)
                else:
                    handle.write(final_data)
                handle.flush()
                os.fsync(handle.fileno())
            token = _safe_token(operation_id, connector_id, tool)
            journal = self.journal_dir / f"{token}.json"
            payload = {
                "operation_id": operation_id,
                "filepath": rel.as_posix(),
                "phase": "prepared",
                "temp": temp.name,
                "old_exists": old_exists,
                "backup": backup_path.relative_to(self.documents_dir).as_posix()
                if backup_path else None,
                "new_sha256": sha256_file(temp)[1],
            }
            if connector_id is not None:
                payload["connector_id"] = connector_id
            if tool is not None:
                payload["tool"] = tool
            self._write_journal(journal, payload)
            checked = resolve_target(self.documents_dir, rel.as_posix())
            if checked != destination or checked.parent.resolve() != destination.parent.resolve():
                raise AssetError("invalid_path", "asset destination changed during publication")
            os.replace(temp, destination)
            _fsync_dir(destination.parent)
            payload["phase"] = "file_published"
            self._write_journal(journal, payload)
            return PublishedFile(
                operation_id, destination, journal, staged, temp, old_exists, backup_path,
                connector_id, tool,
            )
        except Exception:
            self.cleanup(temp)
            raise

    def commit(self, published: PublishedFile) -> None:
        self.cleanup(published.journal)
        self.cleanup(published.staged)
        self.cleanup(published.temp)
        _fsync_dir(self.journal_dir)

    def rollback(self, published: PublishedFile) -> None:
        """Restore the verified prior file, or remove a newly created destination."""
        if published.old_exists:
            if published.backup_path is None or not published.backup_path.is_file():
                raise AssetError("recovery_required", "asset rollback backup is unavailable")
            fd, name = tempfile.mkstemp(
                prefix=TEMP_PREFIX, suffix=".rollback", dir=published.destination.parent
            )
            temp = Path(name)
            try:
                with os.fdopen(fd, "wb") as handle, published.backup_path.open("rb") as source:
                    shutil.copyfileobj(source, handle, 64 * 1024)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temp, published.destination)
                _fsync_dir(published.destination.parent)
            finally:
                self.cleanup(temp)
        else:
            self.cleanup(published.destination)
            _fsync_dir(published.destination.parent)
        self.commit(published)

    def pending(self) -> list[dict[str, Any]]:
        entries: list[dict[str, Any]] = []
        for journal in self.journal_dir.glob("*.json"):
            try:
                payload = json.loads(journal.read_text(encoding="utf-8"))
                payload["_journal"] = journal
                entries.append(payload)
            except (OSError, ValueError, TypeError):
                entries.append({"phase": "blocked", "operation_id": journal.stem, "_journal": journal})
        return entries

    def recover_entry(self, payload: dict[str, Any], *, committed: bool) -> None:
        journal = Path(payload["_journal"])
        phase = payload.get("phase")
        relative = payload.get("filepath")
        if not isinstance(relative, str):
            if phase == "prepared":
                self.cleanup(journal)
                return
            raise AssetError("recovery_required", "asset recovery journal is invalid")
        destination = resolve_target(self.documents_dir, relative)
        if destination is None:
            raise AssetError("recovery_required", "asset recovery path is invalid")
        if payload.get("operation_kind") == "delete":
            tombstone_name = payload.get("tombstone")
            expected = payload.get("expected_sha256")
            backup_rel = payload.get("backup")
            if (not isinstance(tombstone_name, str) or Path(tombstone_name).name != tombstone_name
                    or not isinstance(expected, str) or len(expected) != 64):
                raise AssetError("recovery_required", "asset deletion journal is invalid")
            tombstone = destination.parent / tombstone_name
            if committed:
                self.cleanup(tombstone)
                if tombstone.exists():
                    raise AssetError("recovery_required", "committed asset tombstone remains")
                self.cleanup(journal)
                return
            if destination.exists():
                if sha256_file(destination, maximum=MAX_PNG_BYTES)[1] != expected:
                    raise AssetError("recovery_required", "asset deletion destination was replaced")
                self.cleanup(tombstone)
            elif tombstone.is_file():
                os.replace(tombstone, destination)
                _fsync_dir(destination.parent)
            else:
                backup = resolve_target(self.documents_dir, backup_rel) if isinstance(backup_rel, str) else None
                if backup is None:
                    raise AssetError("recovery_required", "asset deletion backup is unavailable")
                self.restore_backup_file(backup, destination)
            if sha256_file(destination, maximum=MAX_PNG_BYTES)[1] != expected:
                raise AssetError("recovery_required", "asset deletion recovery verification failed")
            self.cleanup(journal)
            return
        if phase == "prepared":
            temp_name = payload.get("temp")
            if isinstance(temp_name, str):
                self.cleanup(destination.parent / temp_name)
            published = False
            if destination.is_file() and isinstance(payload.get("new_sha256"), str):
                try:
                    published = sha256_file(destination, maximum=MAX_PNG_BYTES)[1] == payload["new_sha256"]
                except AssetError:
                    published = False
            if not published:
                self.cleanup(journal)
                return
            phase = "file_published"
        if phase != "file_published":
            raise AssetError("recovery_required", "asset recovery journal is invalid")
        if not committed:
            backup_rel = payload.get("backup")
            if payload.get("old_exists"):
                backup = resolve_target(self.documents_dir, backup_rel) if isinstance(backup_rel, str) else None
                if backup is None or not backup.is_file():
                    raise AssetError("recovery_required", "asset recovery backup is unavailable")
                self._restore_backup(backup, destination)
            else:
                self.cleanup(destination)
                _fsync_dir(destination.parent)
        self.cleanup(journal)

    def _restore_backup(self, backup: Path, destination: Path) -> None:
        fd, name = tempfile.mkstemp(prefix=TEMP_PREFIX, suffix=".rollback", dir=destination.parent)
        temp = Path(name)
        try:
            with os.fdopen(fd, "wb") as handle, backup.open("rb") as source:
                shutil.copyfileobj(source, handle, 64 * 1024)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, destination)
            _fsync_dir(destination.parent)
        finally:
            self.cleanup(temp)

    def recover(self) -> list[dict[str, Any]]:
        """Compatibility helper: clean prepared entries and return published work."""
        remaining: list[dict[str, Any]] = []
        for payload in self.pending():
            if payload.get("phase") == "prepared" and not payload.get("filepath"):
                self.cleanup(Path(payload["_journal"]))
            else:
                remaining.append(payload)
        return remaining

    def _write_journal(self, path: Path, payload: dict[str, Any]) -> None:
        temp = path.with_suffix(".tmp")
        temp.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        _fsync_file(temp)
        os.replace(temp, path)
        _fsync_dir(self.journal_dir)

    @staticmethod
    def cleanup(path: Path) -> None:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
