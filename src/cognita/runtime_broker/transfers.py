"""Bounded framed-transfer state machine for bridge-owned byte movement."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import os
import tempfile
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from threading import RLock
from typing import BinaryIO
from uuid import UUID

from .protocol import (
    MAX_TRANSFER_FRAME_BYTES,
    TransferManifest,
    TransferState,
)

MAX_ACTIVE_TRANSFERS = 64
MAX_RETAINED_TRANSFERS = 256
TRANSFER_MEMORY_SPOOL_BYTES = MAX_TRANSFER_FRAME_BYTES


@dataclass
class _Transfer:
    manifest: TransferManifest
    offsets: dict[str, int] = field(default_factory=dict)
    state: str = "admitted"
    stream: BinaryIO = field(
        default_factory=lambda: tempfile.SpooledTemporaryFile(  # noqa: SIM115 - owned by store.close
            max_size=TRANSFER_MEMORY_SPOOL_BYTES, mode="w+b"
        )
    )


class TransferStore:
    """Admit manifests and enforce ordered, non-overlapping frames.

    Bytes are held only in the broker's private process/storage boundary.  The
    public Cognita gateway owns the project-side staging and authorization.
    """

    def __init__(
        self,
        *,
        copy_from_host: Callable[[UUID, str, str], Awaitable[None] | None] | None = None,
        copy_to_host: Callable[[UUID, str, str], Awaitable[None] | None] | None = None,
    ) -> None:
        self._items: dict[UUID, _Transfer] = {}
        self._lock = RLock()
        self._operation_lock = asyncio.Lock()
        self._copy_from_host = copy_from_host
        self._copy_to_host = copy_to_host

    @staticmethod
    async def _await_copy(value: Awaitable[None] | None) -> None:
        # Synchronous callbacks remain useful for narrow unit-test fakes, but
        # the production SDK boundary is async and must never be discarded as
        # an un-awaited coroutine.
        if inspect.isawaitable(value):
            await value

    @staticmethod
    def _copy_stream(source: BinaryIO, target: BinaryIO, size: int) -> str:
        digest = hashlib.sha256()
        remaining = size
        while remaining:
            chunk = source.read(min(1024 * 1024, remaining))
            if not chunk:
                raise ValueError("transfer content is truncated")
            target.write(chunk)
            digest.update(chunk)
            remaining -= len(chunk)
        return digest.hexdigest()

    async def _stage_from_workspace(self, item: _Transfer) -> None:
        if self._copy_to_host is None:
            return
        for entry in item.manifest.files:
            fd, raw_name = tempfile.mkstemp(prefix="cognita-transfer-export-")
            os.close(fd)
            host_path = Path(raw_name)
            try:
                await self._await_copy(
                    self._copy_to_host(item.manifest.workspace_id, entry.path, str(host_path))
                )
                digest = await asyncio.to_thread(
                    self._append_staged_file, host_path, item.stream, entry.size
                )
                if digest != entry.sha256.lower():
                    raise ValueError("workspace transfer hash changed")
                item.offsets[entry.path] = entry.size
            finally:
                host_path.unlink(missing_ok=True)
        item.stream.flush()
        item.state = "ready"

    @classmethod
    def _append_staged_file(
        cls, host_path: Path, stream: BinaryIO, expected_size: int
    ) -> str:
        if host_path.stat().st_size != expected_size:
            raise ValueError("workspace transfer size changed")
        with host_path.open("rb") as source:
            return cls._copy_stream(source, stream, expected_size)

    async def _commit_to_workspace(self, item: _Transfer) -> None:
        if self._copy_from_host is None:
            return
        position = 0
        for entry in item.manifest.files:
            fd, raw_name = tempfile.mkstemp(prefix="cognita-transfer-import-")
            os.close(fd)
            host_path = Path(raw_name)
            try:
                digest = await asyncio.to_thread(
                    self._write_staged_file,
                    item.stream,
                    position,
                    host_path,
                    entry.size,
                )
                if digest != entry.sha256.lower():
                    raise ValueError("manifest hash mismatch")
                await self._await_copy(
                    self._copy_from_host(
                        item.manifest.workspace_id, entry.path, str(host_path)
                    )
                )
            finally:
                host_path.unlink(missing_ok=True)
            position += entry.size

    @classmethod
    def _write_staged_file(
        cls,
        stream: BinaryIO,
        position: int,
        host_path: Path,
        expected_size: int,
    ) -> str:
        stream.seek(position)
        with host_path.open("wb") as target:
            digest = cls._copy_stream(stream, target, expected_size)
            target.flush()
            os.fsync(target.fileno())
        return digest

    async def admit(self, manifest: TransferManifest) -> TransferState:
        async with self._operation_lock:
            with self._lock:
                prior = self._items.get(manifest.transfer_id)
                if prior is not None:
                    if prior.manifest != manifest:
                        raise ValueError("transfer_id was reused with different manifest")
                    return self.state(manifest.transfer_id)
                active = sum(
                    item.state not in {"committed", "aborted"}
                    for item in self._items.values()
                )
                if active >= MAX_ACTIVE_TRANSFERS:
                    raise ValueError("too many active transfers")
                while len(self._items) >= MAX_RETAINED_TRANSFERS:
                    finalized_id = next(
                        (
                            transfer_id
                            for transfer_id, item in self._items.items()
                            if item.state in {"committed", "aborted"}
                        ),
                        None,
                    )
                    if finalized_id is None:
                        raise ValueError("too many retained transfers")
                    finalized = self._items.pop(finalized_id)
                    finalized.stream.close()
                item = _Transfer(manifest)
                item.offsets.update(
                    {entry.path: 0 for entry in manifest.files if entry.size == 0}
                )
                if all(entry.size == 0 for entry in manifest.files):
                    item.state = "ready"
                self._items[manifest.transfer_id] = item
            try:
                if manifest.direction == "from_workspace":
                    await self._stage_from_workspace(item)
            except Exception:
                with self._lock:
                    self._items.pop(manifest.transfer_id, None)
                    item.stream.close()
                raise
            return self.state(manifest.transfer_id)

    def state(self, transfer_id: UUID) -> TransferState:
        with self._lock:
            item = self._items.get(transfer_id)
            if item is None:
                raise KeyError(transfer_id)
            received = sum(item.offsets.values())
            next_path = None
            for entry in item.manifest.files:
                if item.offsets.get(entry.path, 0) < entry.size:
                    next_path = entry.path
                    break
            return TransferState(
                transfer_id=transfer_id,
                workspace_id=item.manifest.workspace_id,
                state=item.state,
                file_count=len(item.manifest.files),
                received_bytes=received,
                total_bytes=item.manifest.total_bytes,
                next_path=next_path,
            )

    def put_frame(
        self, transfer_id: UUID, path: str, offset: int, content: bytes, digest: str
    ) -> TransferState:
        if len(content) > MAX_TRANSFER_FRAME_BYTES:
            raise ValueError("transfer frame is too large")
        if hashlib.sha256(content).hexdigest() != digest.lower():
            raise ValueError("transfer frame hash mismatch")
        with self._lock:
            item = self._items.get(transfer_id)
            if item is None:
                raise KeyError(transfer_id)
            if item.state in {"committed", "aborted"}:
                raise ValueError("transfer is already finalized")
            entry = next((entry for entry in item.manifest.files if entry.path == path), None)
            if entry is None:
                raise ValueError("path was not declared by manifest")
            next_path = next(
                (
                    candidate.path
                    for candidate in item.manifest.files
                    if item.offsets.get(candidate.path, 0) < candidate.size
                ),
                None,
            )
            if next_path != path:
                raise ValueError("transfer files must be received in manifest order")
            prior = item.offsets.get(path, 0)
            if offset != prior:
                raise ValueError("transfer frames must be contiguous and ordered")
            if offset + len(content) > entry.size:
                raise ValueError("transfer exceeds declared file size")
            item.stream.seek(0, 2)
            item.stream.write(content)
            item.stream.flush()
            item.offsets[path] = offset + len(content)
            item.state = (
                "ready"
                if all(item.offsets.get(e.path, 0) == e.size for e in item.manifest.files)
                else "receiving"
            )
            return self.state(transfer_id)

    def content(
        self, transfer_id: UUID, path: str, offset: int = 0, length: int | None = None
    ) -> bytes:
        with self._lock:
            item = self._items.get(transfer_id)
            if item is None:
                raise KeyError(transfer_id)
            if path not in item.offsets:
                raise ValueError("path has no received content")
            available = item.offsets[path]
            if offset < 0 or offset > available:
                raise ValueError("invalid content offset")
            entry_index = next(
                index for index, entry in enumerate(item.manifest.files) if entry.path == path
            )
            base = sum(entry.size for entry in item.manifest.files[:entry_index])
            item.stream.seek(base + offset)
            remaining = available - offset
            return item.stream.read(remaining if length is None else min(length, remaining))

    async def commit(self, transfer_id: UUID) -> TransferState:
        async with self._operation_lock:
            with self._lock:
                item = self._items.get(transfer_id)
                if item is None:
                    raise KeyError(transfer_id)
                if item.state == "committed":
                    return self.state(transfer_id)
                if item.state != "ready":
                    raise ValueError("transfer is incomplete")
                position = 0
                for entry in item.manifest.files:
                    item.stream.seek(position)
                    remaining = entry.size
                    digest = hashlib.sha256()
                    while remaining:
                        chunk = item.stream.read(min(1024 * 1024, remaining))
                        if not chunk:
                            raise ValueError("transfer content is truncated")
                        digest.update(chunk)
                        remaining -= len(chunk)
                    if digest.hexdigest() != entry.sha256.lower():
                        raise ValueError("manifest hash mismatch")
                    position += entry.size
            if item.manifest.direction == "to_workspace":
                await self._commit_to_workspace(item)
            with self._lock:
                item.state = "committed"
                return self.state(transfer_id)

    def abort(self, transfer_id: UUID) -> TransferState:
        with self._lock:
            item = self._items.get(transfer_id)
            if item is None:
                raise KeyError(transfer_id)
            if item.state == "committed":
                raise ValueError("committed transfer cannot be aborted")
            item.state = "aborted"
            item.stream.close()
            item.offsets.clear()
            return self.state(transfer_id)

    def close(self) -> None:
        with self._lock:
            for item in self._items.values():
                item.stream.close()
            self._items.clear()
