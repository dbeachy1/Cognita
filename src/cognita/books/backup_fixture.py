"""Project-scoped, offline proof helpers for audiobook backup restoration.

These helpers are deliberately not a public MCP backup API.  A caller must
hold the project mutation lock and quiesce book work before snapshotting.
They copy one project tree and the exact external ``deindexed.json`` policy
file; they never mirror arbitrary contents of ``project.data_dir``.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import stat
from dataclasses import dataclass
from pathlib import Path

from .state import DATABASE_FILENAME, STATE_DIRECTORY, ProjectState

_MANIFEST = "book-backup-manifest.json"
_EXTERNAL_POLICY = "data/deindexed.json"


class BookBackupFixtureError(RuntimeError):
    """The offline proof cannot make or validate a safe scoped snapshot."""


@dataclass(frozen=True, slots=True)
class ScopedBookBackup:
    root: Path
    manifest_path: Path
    entries: tuple[dict[str, object], ...]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def _checked_regular(path: Path) -> os.stat_result:
    try:
        facts = path.lstat()
    except OSError as exc:
        raise BookBackupFixtureError("A scoped backup path could not be inspected.") from exc
    if not stat.S_ISREG(facts.st_mode) or stat.S_ISLNK(facts.st_mode):
        raise BookBackupFixtureError("Scoped backup accepts only ordinary regular files.")
    return facts


def _copy_regular(source: Path, target: Path) -> dict[str, object]:
    facts = _checked_regular(source)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)
    return {"sha256": _sha256(target), "size_bytes": facts.st_size}


def _project_files(project_root: Path) -> tuple[Path, ...]:
    files: list[Path] = []
    for directory, dirs, names in os.walk(project_root, topdown=True, followlinks=False):
        current = Path(directory)
        for name in tuple(dirs):
            candidate = current / name
            if candidate.is_symlink():
                raise BookBackupFixtureError("Scoped backup refuses symbolic-link directories.")
        for name in sorted(names):
            candidate = current / name
            relative = candidate.relative_to(project_root).as_posix()
            if relative == f"{STATE_DIRECTORY}/{DATABASE_FILENAME}":
                continue
            if relative in {
                f"{STATE_DIRECTORY}/{DATABASE_FILENAME}-journal",
                f"{STATE_DIRECTORY}/{DATABASE_FILENAME}-wal",
                f"{STATE_DIRECTORY}/{DATABASE_FILENAME}-shm",
            }:
                raise BookBackupFixtureError("Book state is not quiesced for backup.")
            _checked_regular(candidate)
            files.append(candidate)
    return tuple(files)


def _safe_manifest_path(value: object) -> str:
    if not isinstance(value, str) or not value or value.startswith("/") or "\\" in value:
        raise BookBackupFixtureError("Backup manifest contains an unsafe path.")
    parts = value.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise BookBackupFixtureError("Backup manifest contains an unsafe path.")
    return value


def _write_sqlite_backup(source: Path, target: Path) -> None:
    _checked_regular(source)
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        with sqlite3.connect(f"file:{source.as_posix()}?mode=ro", uri=True) as incoming, sqlite3.connect(target) as outgoing:
            incoming.backup(outgoing)
    except sqlite3.Error as exc:
        raise BookBackupFixtureError("The book SQLite state could not be backed up.") from exc


def snapshot_quiesced_book_project(
    project_root: Path,
    data_dir: Path,
    backup_root: Path,
) -> ScopedBookBackup:
    """Create a verified offline fixture from a quiesced book project.

    ``backup_root`` must be outside the source project.  The caller owns the
    project lock and confirms no source/media/state mutations are in flight.
    """
    project = Path(project_root).resolve(strict=True)
    data = Path(data_dir).resolve(strict=True)
    destination = Path(backup_root).resolve(strict=False)
    if destination == project or project in destination.parents:
        raise BookBackupFixtureError("Backup destination must be outside the project tree.")
    if destination.exists():
        raise BookBackupFixtureError("Backup destination must not already exist.")
    state_database = project / STATE_DIRECTORY / DATABASE_FILENAME
    _checked_regular(state_database)

    entries: list[dict[str, object]] = []
    try:
        for source in _project_files(project):
            relative = source.relative_to(project).as_posix()
            destination_file = destination / "project" / relative
            facts = _copy_regular(source, destination_file)
            entries.append({"path": f"project/{relative}", **facts})
        state_target = destination / "project" / STATE_DIRECTORY / DATABASE_FILENAME
        _write_sqlite_backup(state_database, state_target)
        entries.append({"path": f"project/{STATE_DIRECTORY}/{DATABASE_FILENAME}",
                        "sha256": _sha256(state_target), "size_bytes": state_target.stat().st_size})
        policy = data / "deindexed.json"
        if policy.exists():
            facts = _copy_regular(policy, destination / _EXTERNAL_POLICY)
            entries.append({"path": _EXTERNAL_POLICY, **facts})
        manifest = {"schema_version": 1, "entries": sorted(entries, key=lambda item: str(item["path"]))}
        manifest_path = destination / _MANIFEST
        manifest_path.write_text(json.dumps(manifest, sort_keys=True, separators=(",", ":")), encoding="utf-8")
        return ScopedBookBackup(destination, manifest_path, tuple(manifest["entries"]))
    except Exception:
        if destination.exists():
            shutil.rmtree(destination)
        raise


def restore_quiesced_book_project(
    backup_root: Path,
    project_root: Path,
    data_dir: Path,
) -> ScopedBookBackup:
    """Verify a scoped fixture then restore it only into new destination roots."""
    backup = Path(backup_root).resolve(strict=True)
    destination = Path(project_root).resolve(strict=False)
    data = Path(data_dir).resolve(strict=False)
    if destination.exists() or data.exists():
        raise BookBackupFixtureError("Restore destinations must not already exist.")
    try:
        raw_manifest = json.loads((backup / _MANIFEST).read_text(encoding="utf-8"))
        entries = raw_manifest["entries"]
        if raw_manifest.get("schema_version") != 1 or not isinstance(entries, list):
            raise ValueError
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        raise BookBackupFixtureError("Scoped backup manifest is invalid.") from exc
    normalized: list[dict[str, object]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise BookBackupFixtureError("Scoped backup manifest is invalid.")
        relative = _safe_manifest_path(entry.get("path"))
        expected_hash, expected_size = entry.get("sha256"), entry.get("size_bytes")
        if not isinstance(expected_hash, str) or len(expected_hash) != 64 or not isinstance(expected_size, int):
            raise BookBackupFixtureError("Scoped backup manifest is invalid.")
        source = backup / relative
        if not source.is_file() or source.is_symlink() or source.stat().st_size != expected_size or _sha256(source) != expected_hash:
            raise BookBackupFixtureError("Scoped backup file does not match its manifest.")
        normalized.append({"path": relative, "sha256": expected_hash, "size_bytes": expected_size})
    try:
        for entry in normalized:
            relative = str(entry["path"])
            if relative.startswith("project/"):
                target = destination / relative.removeprefix("project/")
            elif relative == _EXTERNAL_POLICY:
                target = data / "deindexed.json"
            else:
                raise BookBackupFixtureError("Scoped backup manifest contains an unknown entry.")
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(backup / relative, target)
        ProjectState(destination)
    except Exception:
        if destination.exists():
            shutil.rmtree(destination)
        if data.exists():
            shutil.rmtree(data)
        raise
    return ScopedBookBackup(backup, backup / _MANIFEST, tuple(normalized))


__all__ = [
    "BookBackupFixtureError", "ScopedBookBackup", "restore_quiesced_book_project",
    "snapshot_quiesced_book_project",
]
