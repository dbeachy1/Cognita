"""Verified transient staging for audiobook import sources.

These helpers own only local staging.  The import service adopts a staged file
by rename after its durable publication succeeds; failed callers discard it.
"""
from __future__ import annotations

import hashlib
import os
import stat
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Literal


class SourceStageError(RuntimeError):
    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class StagedAudioSource:
    staged_path: Path
    bytes_sha256: str
    size_bytes: int
    source_kind: Literal["workspace", "https"]


_STAGE_PREFIX = ".cognita-book-source-"
_CHUNK = 1024 * 1024


def stage_verified_file(
    source: Path,
    staging_root: Path,
    *,
    expected_sha256: str,
    source_kind: Literal["workspace", "https"],
) -> StagedAudioSource:
    """Copy one regular source file into an owned, hash-verified stage."""
    if len(expected_sha256) != 64 or any(c not in "0123456789abcdef" for c in expected_sha256):
        raise SourceStageError("validation_failed", "An exact lowercase SHA-256 is required.")
    root = Path(staging_root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    try:
        source_info = source.stat(follow_symlinks=False)
    except OSError as exc:
        raise SourceStageError("source_unavailable", "The staged source is unavailable.") from exc
    if not stat.S_ISREG(source_info.st_mode) or stat.S_ISLNK(source_info.st_mode):
        raise SourceStageError("source_unavailable", "The staged source is not a regular file.")
    target = root / f"{_STAGE_PREFIX}{uuid.uuid4().hex}"
    digest = hashlib.sha256()
    total = 0
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
        fd = os.open(target, flags, 0o600)
        with source.open("rb") as incoming, os.fdopen(fd, "wb") as outgoing:
            while chunk := incoming.read(_CHUNK):
                total += len(chunk)
                digest.update(chunk)
                outgoing.write(chunk)
        if digest.hexdigest() != expected_sha256 or total != source_info.st_size:
            raise SourceStageError("source_changed", "The staged source no longer matches its pinned hash.")
        return StagedAudioSource(target, digest.hexdigest(), total, source_kind)
    except Exception:
        try:
            target.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def discard_staged_audio(source: StagedAudioSource, staging_root: Path) -> None:
    """Remove only a helper-owned, not-yet-adopted staging file."""
    root = Path(staging_root).resolve()
    path = source.staged_path.resolve(strict=False)
    if path.parent != root or not path.name.startswith(_STAGE_PREFIX):
        raise SourceStageError("invalid_stage", "Refusing to remove a non-owned source stage.")
    try:
        info = path.stat(follow_symlinks=False)
    except FileNotFoundError:
        return
    except OSError as exc:
        raise SourceStageError("stage_unavailable", "The source stage cannot be inspected.") from exc
    if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
        raise SourceStageError("invalid_stage", "Refusing to remove a non-regular source stage.")
    try:
        path.unlink()
    except OSError as exc:
        raise SourceStageError("stage_unavailable", "The source stage cannot be removed.") from exc