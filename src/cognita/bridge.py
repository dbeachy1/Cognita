"""Knowledge ↔ Workspace transfer bridge.

The bridge is deliberately a narrow service boundary.  It owns authorization,
manifests, staging and conflict handling, while the Knowledge engine and the
Workspace broker remain responsible for their own persistence and locks.  File
bytes never become part of an MCP result: only a bounded receipt is returned.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import logging
import os
import shutil
import stat
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterable, Mapping, Protocol

from .auth_policy import (
    SELF_TEST_PROJECT_NAME,
    is_self_test_principal,
    self_test_principal_matches,
)
from .backups import BackupError, backup_if_exists, resolve_target
from .connectors import PUBLIC_CONTRACT_VERSION, ConnectorDefinition, resolve_project_access
from .workspace import WorkspaceError

log = logging.getLogger("cognita.bridge")

MAX_TRANSFER_FILES = 10_000
MAX_TRANSFER_BYTES = 4 * 1024**3
MAX_FRAME_BYTES = 8 * 1024**2
MAX_PATH_BYTES = 4096
_MARKER_NAME = ".cognita-transfer.json"
_MARKER_KIND = "cognita-bridge-transfer-v1"
_SHA256 = frozenset("0123456789abcdef")


class BridgeError(RuntimeError):
    """A bounded, client-safe bridge failure."""

    def __init__(self, reason: str, message: str = "Bridge operation failed", **fields: Any):
        super().__init__(message)
        self.reason = reason
        self.fields = fields


class TransferClient(Protocol):
    """Private broker transfer methods; implementations may be sync or async."""

    def admit_transfer(self, manifest: Mapping[str, Any]) -> Any: ...
    def put_transfer_frame(self, transfer_id: str, path: str, offset: int, content: bytes, digest: str) -> Any: ...
    def commit_transfer(self, transfer_id: str) -> Any: ...
    def abort_transfer(self, transfer_id: str) -> Any: ...
    def get_transfer_content(self, transfer_id: str, path: str, offset: int, length: int) -> Any: ...


class BrokerTransferClient:
    """Async adapter for the broker's private framed-transfer HTTP surface."""

    def __init__(self, base_url: str, bearer: str, *, timeout: float = 60.0, client: Any = None):
        self.base_url = base_url.rstrip("/")
        self.bearer = bearer
        self.timeout = timeout
        self._client = client

    def _url(self, suffix: str) -> str:
        prefix = self.base_url if self.base_url.endswith("/v1") else f"{self.base_url}/v1"
        return f"{prefix}{suffix}"

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.bearer}"}

    async def _request(self, method: str, suffix: str, **kwargs: Any) -> Any:
        import httpx
        client = self._client or httpx.AsyncClient(timeout=self.timeout)
        close = self._client is None
        try:
            request_headers = {**self._headers(), **dict(kwargs.pop("headers", {}) or {})}
            response = await client.request(method, self._url(suffix), headers=request_headers, **kwargs)
            response.raise_for_status()
            if response.headers.get("content-type", "").startswith("application/json"):
                payload = response.json()
                if isinstance(payload, dict) and payload.get("code"):
                    raise BridgeError("runtime_unavailable", "Workspace transfer runtime rejected the operation")
                return payload
            return response.content
        except BridgeError:
            raise
        except Exception as exc:
            raise BridgeError("runtime_unavailable", "Workspace transfer runtime is unavailable") from exc
        finally:
            if close:
                await client.aclose()

    async def admit_transfer(self, manifest: Mapping[str, Any]) -> Any:
        return await self._request("POST", "/transfers", json=dict(manifest))

    async def put_transfer_frame(self, transfer_id: str, path: str, offset: int, content: bytes, digest: str) -> Any:
        return await self._request("PUT", f"/transfers/{transfer_id}/content", content=content, headers={**self._headers(), "X-Transfer-Path": path, "X-Frame-Offset": str(offset), "X-Frame-SHA256": digest})

    async def commit_transfer(self, transfer_id: str) -> Any:
        return await self._request("POST", f"/transfers/{transfer_id}/commit")

    async def abort_transfer(self, transfer_id: str) -> Any:
        return await self._request("POST", f"/transfers/{transfer_id}/abort")

    async def get_transfer_content(self, transfer_id: str, path: str, offset: int, length: int) -> Any:
        return await self._request("GET", f"/transfers/{transfer_id}/content", params={"path": path, "offset": offset, "length": length})


@dataclass(frozen=True, slots=True)
class ManifestEntry:
    path: str
    size: int
    sha256: str
    source: Path | None = None
    mode: int = 0o644
    destination: str | None = None

    def wire(self) -> dict[str, Any]:
        return {"path": self.path, "size": self.size, "sha256": self.sha256}


@dataclass(frozen=True, slots=True)
class TransferManifest:
    transfer_id: str
    workspace_id: str
    direction: str
    files: tuple[ManifestEntry, ...]
    directories: tuple[str, ...] = ()

    @property
    def total_bytes(self) -> int:
        return sum(item.size for item in self.files)

    def wire(self) -> dict[str, Any]:
        return {
            "transfer_id": self.transfer_id,
            "workspace_id": self.workspace_id,
            "direction": self.direction,
            "files": [item.wire() for item in self.files],
            "total_bytes": self.total_bytes,
        }


@dataclass(slots=True)
class _Stage:
    transfer_id: str
    root: Path
    manifest: TransferManifest


def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return value
    return value


async def _invoke(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


def _principal_id(principal: Any) -> str:
    value = principal.get("principal_id") if isinstance(principal, dict) else getattr(principal, "principal_id", None)
    if not value:
        raise BridgeError("unauthorized", "Bridge requires a durable authenticated principal")
    return str(value)


def _project_name(project: Any) -> str:
    value = project if isinstance(project, str) else getattr(project, "name", None)
    if not isinstance(value, str) or not value or value != value.strip():
        raise BridgeError("invalid_arguments", "project must be an exact project name")
    return value


def _safe_rel(value: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or len(value.encode()) > MAX_PATH_BYTES or "\\" in value or "\x00" in value:
        raise BridgeError("invalid_path", "path is not a normalized relative path")
    if value in {".", "/"} and allow_empty:
        return ""
    if value.startswith("/"):
        raise BridgeError("invalid_path", "path is not a normalized relative path")
    if not value and allow_empty:
        return ""
    if not value or value.startswith("/"):
        raise BridgeError("invalid_path", "path is not a normalized relative path")
    parts = value.split("/")
    if any(not part or part in {".", ".."} for part in parts):
        raise BridgeError("invalid_path", "path contains an invalid component")
    return "/".join(parts)


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def _hash_file(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                size += len(chunk)
                digest.update(chunk)
    except OSError as exc:
        raise BridgeError("source_unavailable", "Source file could not be read") from exc
    return size, digest.hexdigest()


def _reject_entry(path: Path, *, root: Path) -> None:
    try:
        path.relative_to(root)
        info = path.lstat()
    except (OSError, ValueError) as exc:
        raise BridgeError("invalid_path", "Path is not inside the project root") from exc
    mode = info.st_mode
    if stat.S_ISLNK(mode) or not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
        raise BridgeError("invalid_path", "Symlinks and special files are not transferable")
    if stat.S_ISREG(mode) and info.st_nlink != 1:
        raise BridgeError("invalid_path", "Hard-linked files are not transferable")


def _unique_project_name(root: Path, path: str, used: set[str]) -> str:
    """Choose a nonexisting contained project path without overwriting."""
    candidate = path
    posix = PurePosixPath(path)
    n = 0
    while True:
        target = resolve_target(root, candidate)
        if target is None:
            raise BridgeError("invalid_path", "Destination escapes the project root")
        if candidate not in used and not target.exists() and not target.is_symlink():
            return candidate
        n += 1
        candidate = str(posix.with_name(f"{posix.stem} ({n}){posix.suffix}"))


class BridgeService:
    """Authorized, retryable bridge coordinator.

    ``workspace`` is the existing Workspace manager.  ``transfer_client`` is
    the private framed broker adapter; it is injected so tests and deployment
    wiring can use either an HTTP client or an in-process broker fake.
    """

    def __init__(
        self,
        workspace: Any,
        transfer_client: TransferClient | Any | None = None,
        *,
        staging_root: str | Path | None = None,
        watcher: Any | None = None,
        knowledge_core: Any | None = None,
        reconcile: Callable[..., Any] | None = None,
        backup_keep: int = 0,
        growth_renewal_interval: float = 60.0,
    ):
        self.workspace = workspace
        self.transfer_client = transfer_client or getattr(workspace, "transfer_client", None)
        if self.transfer_client is None:
            runtime = getattr(workspace, "runtime", None)
            base_url, bearer = getattr(runtime, "base_url", None), getattr(runtime, "bearer", None)
            if base_url and bearer:
                self.transfer_client = BrokerTransferClient(base_url, bearer, timeout=600.0)
        self.staging_root = Path(staging_root or os.environ.get("COGNITA_TRANSFER_STAGING_ROOT", tempfile.gettempdir()))
        self.staging_root.mkdir(parents=True, exist_ok=True)
        self.watcher = watcher
        self.knowledge_core = knowledge_core
        self.reconcile = reconcile
        self.backup_keep = backup_keep
        if growth_renewal_interval <= 0:
            raise ValueError("growth_renewal_interval must be positive")
        self.growth_renewal_interval = float(growth_renewal_interval)
        self._idempotency: dict[tuple[str, str], tuple[str, dict[str, Any]]] = {}
        self._idempotency_lock = threading.RLock()

    def _check_policy(self, principal: Any, connector: ConnectorDefinition, project: Any, direction: str) -> None:
        if connector is None or not connector.workspace_enabled:
            raise BridgeError("workspace_disabled", "Workspace transfer is disabled for this connector")
        name = _project_name(project)
        access = resolve_project_access(connector, project)
        if access is None:
            raise BridgeError("project_forbidden", "The connector cannot access this project")
        if not connector.transfer_allowed(name):
            raise BridgeError("transfer_forbidden", "Knowledge–Workspace transfer is disabled for this project")
        if direction == "to_workspace" and access not in {"read", "write"}:
            raise BridgeError("project_forbidden", "Read access is required for copy_to_workspace")
        if direction == "from_workspace" and (access != "write" or not bool(getattr(project, "writable", True))):
            raise BridgeError("read_only", "Write access is required for copy_from_workspace")
        if is_self_test_principal(principal):
            # 13.0 §7.3: both bridge directions go through the same scope check
            # as a direct call. The gateway already resolved the project, so
            # this is the second, independent gate: the project must be
            # Self-Test, and the principal handle must be the one THIS
            # connector derives, so a handle forged for another connector — or
            # borrowed from one — cannot move bytes here.
            if name != SELF_TEST_PROJECT_NAME or not self_test_principal_matches(
                principal, getattr(connector, "id", None)
            ):
                log.info(
                    "bridge denied direction=%s project=%s connector_id=%s outcome=self_test_scope",
                    direction, name, getattr(connector, "id", None),
                )
                raise BridgeError("project_forbidden", "The connector cannot access this project")
        _principal_id(principal)

    def _stage(self, transfer_id: str, manifest: TransferManifest) -> _Stage:
        root = (self.staging_root / transfer_id).resolve()
        parent = self.staging_root.resolve()
        try:
            root.relative_to(parent)
        except ValueError as exc:
            raise BridgeError("internal_error", "Transfer staging root is invalid") from exc
        root.mkdir(parents=True, exist_ok=False)
        marker = {"kind": _MARKER_KIND, "transfer_id": transfer_id, "pid": os.getpid(), "files": [item.wire() for item in manifest.files]}
        (root / _MARKER_NAME).write_text(json.dumps(marker, separators=(",", ":")), encoding="utf-8")
        return _Stage(transfer_id, root, manifest)

    def _cleanup(self, stage: _Stage | None) -> None:
        if stage is None:
            return
        root = stage.root.resolve()
        parent = self.staging_root.resolve()
        marker = root / _MARKER_NAME
        try:
            if root.parent != parent or not marker.is_file():
                return
            data = json.loads(marker.read_text(encoding="utf-8"))
            if data.get("kind") != _MARKER_KIND or data.get("transfer_id") != stage.transfer_id:
                return
            last: OSError | None = None
            for attempt in range(3):
                try:
                    shutil.rmtree(root)
                    last = None
                    break
                except OSError as exc:
                    last = exc
                    if attempt < 2:
                        time.sleep(0.05 * (attempt + 1))
            if last is not None:
                raise last
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            log.warning("bridge staging cleanup failed transfer_id=%s reason=%s", stage.transfer_id, type(exc).__name__)

    def scavenge_staging(self, active_transfer_ids: Iterable[str] = ()) -> list[str]:
        """Remove only stale roots carrying our exact marker."""
        active = set(map(str, active_transfer_ids))
        removed: list[str] = []
        parent = self.staging_root.resolve()
        for root in self.staging_root.iterdir():
            if not root.is_dir() or root.resolve().parent != parent:
                continue
            marker = root / _MARKER_NAME
            try:
                data = json.loads(marker.read_text(encoding="utf-8"))
                transfer_id = str(data["transfer_id"])
                if data.get("kind") != _MARKER_KIND or transfer_id in active or not uuid.UUID(transfer_id):
                    continue
                for attempt in range(3):
                    try:
                        shutil.rmtree(root)
                        break
                    except OSError:
                        if attempt == 2:
                            raise
                        time.sleep(0.05 * (attempt + 1))
                removed.append(transfer_id)
            except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
                continue
        return removed

    async def _broker(self, method: str, *args: Any, **kwargs: Any) -> Any:
        client = self.transfer_client
        if client is None:
            raise BridgeError("runtime_unavailable", "Workspace transfer runtime is unavailable")
        names = {
            "admit": ("admit_transfer", "admit"),
            "put": ("put_transfer_frame", "put_frame"),
            "commit": ("commit_transfer", "commit"),
            "abort": ("abort_transfer", "abort"),
            "content": ("get_transfer_content", "content", "read_transfer_frame"),
        }[method]
        func = next((getattr(client, name, None) for name in names if callable(getattr(client, name, None))), None)
        if func is None:
            raise BridgeError("runtime_unavailable", "Workspace transfer runtime is unavailable")
        try:
            return await _invoke(func(*args, **kwargs))
        except BridgeError:
            raise
        except Exception as exc:
            raise BridgeError("runtime_unavailable", "Workspace transfer runtime rejected the operation") from exc

    async def _stream_to_workspace(
        self,
        stage: _Stage,
        renewal_errors: list[BridgeError] | None = None,
    ) -> BridgeError | None:
        committed = False
        try:
            self._raise_growth_renewal_error(renewal_errors)
            await self._broker("admit", stage.manifest.wire())
            self._raise_growth_renewal_error(renewal_errors)
            for entry in stage.manifest.files:
                source = stage.root / entry.path
                offset = 0
                with source.open("rb") as handle:
                    while True:
                        self._raise_growth_renewal_error(renewal_errors)
                        chunk = handle.read(MAX_FRAME_BYTES)
                        if not chunk:
                            break
                        await self._broker("put", stage.transfer_id, entry.path, offset, chunk, hashlib.sha256(chunk).hexdigest())
                        self._raise_growth_renewal_error(renewal_errors)
                        offset += len(chunk)
            self._raise_growth_renewal_error(renewal_errors)
            await self._broker("commit", stage.transfer_id)
            committed = True
            try:
                self._raise_growth_renewal_error(renewal_errors)
            except BridgeError as exc:
                # The broker has acknowledged commit, so do not abort.  The
                # caller persists the normal receipt before surfacing this
                # post-commit capacity error to make retries idempotent.
                return exc
        except Exception:
            if not committed:
                try:
                    await self._broker("abort", stage.transfer_id)
                except Exception:
                    log.warning("bridge transfer abort failed transfer_id=%s", stage.transfer_id)
            raise
        return None

    async def _stage_from_workspace(self, stage: _Stage) -> None:
        # Download APIs are private and intentionally return framed bytes only
        # to this trusted service, never to the MCP adapter.
        for entry in stage.manifest.files:
            target = stage.root / entry.path
            target.parent.mkdir(parents=True, exist_ok=True)
            digest = hashlib.sha256()
            received = 0
            with target.open("wb") as handle:
                while received < entry.size:
                    chunk = await self._broker("content", stage.transfer_id, entry.path, received, min(MAX_FRAME_BYTES, entry.size - received))
                    if not isinstance(chunk, (bytes, bytearray)) or not chunk:
                        raise BridgeError("transfer_changed", "Workspace transfer ended before the declared file size")
                    chunk = bytes(chunk)
                    if received + len(chunk) > entry.size:
                        raise BridgeError("transfer_changed", "Workspace transfer exceeded its declared file size")
                    handle.write(chunk)
                    digest.update(chunk)
                    received += len(chunk)
            if digest.hexdigest() != entry.sha256:
                raise BridgeError("transfer_changed", "Workspace source changed during transfer")

    def _knowledge_manifest(self, project: Any, paths: list[str], destination: str) -> tuple[tuple[ManifestEntry, ...], tuple[str, ...]]:
        root = Path(project.documents_dir).resolve()
        files: list[ManifestEntry] = []
        dirs: set[str] = set()
        for raw in paths:
            rel = _safe_rel(raw, allow_empty=True)
            if not rel:
                rel = "."
            lexical = root.joinpath(*rel.split("/"))
            if lexical.exists() or lexical.is_symlink():
                _reject_entry(lexical, root=root)
            source = resolve_target(root, rel)
            if source is None or not source.exists():
                raise BridgeError("path_unavailable", "Requested project path is unavailable")
            _reject_entry(source, root=root)
            base = PurePosixPath(rel).name if source.is_file() else PurePosixPath(rel).name
            target_root = "/".join(part for part in (destination, base) if part)
            if source.is_file():
                size, digest = _hash_file(source)
                files.append(ManifestEntry(target_root, size, digest, source, stat.S_IMODE(source.stat().st_mode) & 0o777))
            else:
                dirs.add(target_root)
                for item in sorted(source.rglob("*")):
                    _reject_entry(item, root=root)
                    item_rel = item.relative_to(source).as_posix()
                    target = f"{target_root}/{item_rel}" if item_rel else target_root
                    if item.is_dir():
                        dirs.add(target)
                    elif item.is_file():
                        size, digest = _hash_file(item)
                        files.append(ManifestEntry(target, size, digest, item, stat.S_IMODE(item.stat().st_mode) & 0o777))
        files.sort(key=lambda item: item.path)
        if not files:
            raise BridgeError("empty_transfer", "No regular files were selected")
        if len(files) > MAX_TRANSFER_FILES or sum(item.size for item in files) > MAX_TRANSFER_BYTES:
            raise BridgeError("quota_exceeded", "Transfer exceeds its file or byte limit")
        return tuple(files), tuple(sorted(dirs))

    async def _workspace_inventory(self, principal: Any, paths: Iterable[str], connector_id: str | None) -> dict[str, dict[str, Any]]:
        inventory = getattr(self.workspace, "inventory", None)
        if callable(inventory):
            result = await _invoke(inventory(principal, list(paths), connector_id=connector_id))
            return result if isinstance(result, dict) else {}
        result: dict[str, dict[str, Any]] = {}
        for path in paths:
            try:
                row = self.workspace.execute(principal, "workspace_stat", {"path": path, "include_hash": True}, connector_id=connector_id)
                row = await _invoke(row)
                if not isinstance(row, dict) or row.get("status") != "success":
                    reason = row.get("reason") if isinstance(row, dict) else None
                    if reason == "path_unavailable":
                        continue
                    raise BridgeError("runtime_unavailable", "Workspace inventory is unavailable")
                data = row.get("data", row) if isinstance(row, dict) else {}
                if isinstance(data, dict) and data.get("exists", True) is not False:
                    result[path] = data
            except BridgeError:
                raise
            except Exception as exc:
                if getattr(exc, "reason", None) == "path_unavailable":
                    continue
                raise BridgeError("runtime_unavailable", "Workspace inventory is unavailable") from exc
        return result

    async def _workspace_list(
        self, principal: Any, path: str, connector_id: str | None
    ) -> list[dict[str, Any]]:
        try:
            result = self.workspace.execute(
                principal,
                "workspace_list_files",
                {"path": path, "recursive": True, "max_entries": 2000},
                connector_id=connector_id,
            )
            result = await _invoke(result)
        except Exception as exc:
            raise BridgeError("runtime_unavailable", "Workspace directory inventory is unavailable") from exc
        if not isinstance(result, dict) or result.get("status") != "success":
            raise BridgeError("runtime_unavailable", "Workspace directory inventory is unavailable")
        data = result.get("data")
        rows = data.get("entries", data.get("files")) if isinstance(data, dict) else None
        if not isinstance(rows, list):
            raise BridgeError("runtime_unavailable", "Workspace directory inventory is invalid")
        if len(rows) >= 2000 and not bool(data.get("eof", True)):
            raise BridgeError("quota_exceeded", "Workspace directory exceeds the bridge listing limit")
        return [row for row in rows if isinstance(row, dict)]

    async def _workspace_manifest(
        self,
        principal: Any,
        selected: list[str],
        destination: str,
        connector_id: str | None,
    ) -> tuple[ManifestEntry, ...]:
        roots = await self._workspace_inventory(principal, selected, connector_id)
        source_to_destination: dict[str, str] = {}
        for selected_path in selected:
            row = roots.get(selected_path)
            if not row:
                raise BridgeError("path_unavailable", "Workspace source path is unavailable")
            kind = str(row.get("type") or row.get("kind") or "file")
            if kind in {"directory", "dir"}:
                root_name = PurePosixPath(selected_path).name
                for entry in await self._workspace_list(principal, selected_path, connector_id):
                    entry_kind = str(entry.get("type") or entry.get("kind") or "file")
                    if entry_kind in {"directory", "dir"}:
                        continue
                    raw_path = entry.get("path")
                    if not isinstance(raw_path, str):
                        raise BridgeError("runtime_unavailable", "Workspace directory inventory is invalid")
                    if raw_path.startswith("/workspace/"):
                        raw_path = raw_path[len("/workspace/"):]
                    source_path = _safe_rel(raw_path)
                    prefix = selected_path.rstrip("/") + "/"
                    if not source_path.startswith(prefix):
                        raise BridgeError("invalid_path", "Workspace directory inventory escaped its source root")
                    relative = source_path[len(prefix):]
                    source_to_destination[source_path] = _safe_rel(
                        "/".join(part for part in (destination, root_name, relative) if part)
                    )
            else:
                source_to_destination[selected_path] = _safe_rel(
                    "/".join(
                        part
                        for part in (destination, PurePosixPath(selected_path).name)
                        if part
                    )
                )
        if not source_to_destination:
            raise BridgeError("empty_transfer", "No regular files were selected")
        if len(source_to_destination) > MAX_TRANSFER_FILES:
            raise BridgeError("quota_exceeded", "Transfer exceeds its file limit")
        inventory = await self._workspace_inventory(
            principal, source_to_destination, connector_id
        )
        realized: list[ManifestEntry] = []
        total = 0
        for source_path, target_path in sorted(source_to_destination.items()):
            row = inventory.get(source_path)
            if not row:
                raise BridgeError("path_unavailable", "Workspace source path is unavailable")
            size = int(row.get("size", row.get("size_bytes", 0)))
            digest = str(row.get("sha256") or row.get("bytes_sha256") or "").lower()
            if size < 0 or len(digest) != 64 or set(digest) - _SHA256:
                raise BridgeError("runtime_unavailable", "Workspace did not provide a valid source manifest")
            total += size
            if total > MAX_TRANSFER_BYTES:
                raise BridgeError("quota_exceeded", "Transfer exceeds its byte limit")
            realized.append(
                ManifestEntry(source_path, size, digest, destination=target_path)
            )
        return tuple(realized)

    async def _workspace_id(self, principal: Any, connector_id: str | None) -> str:
        admit = getattr(self.workspace, "_admit", None)
        if not callable(admit):
            info = await _invoke(self.workspace.info(principal, connector_id=connector_id))
            record = info.get("workspace") if isinstance(info, dict) else None
            if not record or not record.get("workspace_id"):
                raise BridgeError("runtime_unavailable", "Workspace runtime is unavailable")
            return str(record["workspace_id"])
        try:
            record = await _invoke(admit(principal, connector_id))
        except WorkspaceError as exc:
            # 13.2.6 (DESIGN-13.2 §6): admission's own reason reaches the
            # caller. This used to fall through to bridge_tool_result's
            # catch-all and answer `internal_error: Bridge operation failed`
            # with a traceback at ERROR — for a generation_conflict that the
            # next call would have cleared. Same mapping _check_workspace_
            # transfer_ready already uses.
            raise BridgeError(exc.reason, str(exc), **exc.fields) from exc
        value = getattr(record, "workspace_id", None)
        if value is None and isinstance(record, Mapping):
            value = record.get("workspace_id")
        try:
            return str(uuid.UUID(str(value)))
        except (TypeError, ValueError, AttributeError) as exc:
            raise BridgeError("runtime_unavailable", "Workspace runtime returned an invalid identity") from exc

    async def _workspace_lock(self, workspace_id: str):
        queue = getattr(self.workspace, "_queue", None)
        if not callable(queue):
            return _AsyncNullContext()
        return _SyncLockContext(queue(workspace_id))

    async def _unique_workspace_name(
        self,
        principal: Any,
        path: str,
        used: set[str],
        connector_id: str | None,
    ) -> str:
        """Choose a nonexisting Workspace path without relying on a stale list."""
        candidate = path
        posix = PurePosixPath(path)
        n = 0
        while True:
            if candidate not in used:
                inventory = await self._workspace_inventory(
                    principal, [candidate], connector_id
                )
                if candidate not in inventory:
                    return candidate
            n += 1
            candidate = str(posix.with_name(f"{posix.stem} ({n}){posix.suffix}"))

    def _check_workspace_transfer_ready(
        self, workspace_id: str, *, growth_bytes: int = 0
    ) -> None:
        metadata = getattr(self.workspace, "metadata", None)
        reconcile = getattr(self.workspace, "active_job_after_reconcile", None)
        try:
            if callable(reconcile):
                active = reconcile(workspace_id)
            else:
                active_job = getattr(metadata, "active_job", None)
                active = active_job(workspace_id) if callable(active_job) else None
        except WorkspaceError as exc:
            raise BridgeError(exc.reason, str(exc), **exc.fields) from exc
        if active is not None:
            raise BridgeError(
                "job_running", "Workspace has a running job",
                job_id=str(active["job_id"]), state=str(active["state"]),
                started_at=str(active["created_at"]), retryable=True,
            )
        if growth_bytes:
            get_record = getattr(metadata, "get", None)
            if callable(get_record):
                record = get_record(workspace_id)
                if record is None:
                    raise BridgeError("runtime_unavailable", "Workspace metadata is unavailable")
                remaining = max(
                    0,
                    int(getattr(record, "quota_bytes", 0))
                    - int(getattr(record, "measured_apparent_bytes", None) or 0),
                )
                if growth_bytes > remaining:
                    raise BridgeError("quota_exceeded", "Transfer exceeds Workspace quota")

    def _reserve_workspace_growth(self, workspace_id: str, growth_bytes: int, transfer_id: str) -> str | None:
        """Durably reserve Workspace growth before a bridge upload mutates it.

        ``host_admission`` is only a snapshot check and cannot protect two
        concurrent transfers from both observing the same free capacity.  The
        Workspace manager's reservation hook persists a held row under its
        metadata transaction, so competing transfers are serialized by the
        database.  Lightweight test/integration managers may not expose that
        hook yet; retain their boolean admission behavior as a compatibility
        fallback, while production managers fail closed through the durable
        hook.
        """
        if growth_bytes <= 0:
            return None
        reserve_admission = getattr(self.workspace, "_reserve_bridge_growth", None)
        if not callable(reserve_admission):
            reserve_admission = getattr(self.workspace, "_reserve_admission", None)
        if callable(reserve_admission):
            try:
                return reserve_admission(
                    growth_bytes,
                    request_key=f"bridge:{transfer_id}",
                )
            except Exception as exc:
                reason = getattr(exc, "reason", None)
                if reason in {"capacity_busy", "capacity_unavailable"}:
                    raise BridgeError(reason, "Workspace host reserve cannot admit transfer") from exc
                raise BridgeError("capacity_unavailable", "Workspace host capacity is unavailable") from exc

        host_admission = getattr(self.workspace, "host_admission", None)
        if callable(host_admission):
            try:
                admitted = host_admission(additional_growth_bytes=growth_bytes)
            except Exception as exc:
                reason = getattr(exc, "reason", None)
                if reason in {"capacity_busy", "capacity_unavailable"}:
                    raise BridgeError(reason, "Workspace host reserve cannot admit transfer") from exc
                raise BridgeError("capacity_unavailable", "Workspace host capacity is unavailable") from exc
            if not admitted:
                raise BridgeError("capacity_busy", "Workspace host reserve cannot admit transfer")
        return None

    def _release_workspace_growth(self, reservation_id: str | None, transfer_id: str) -> None:
        if reservation_id is None:
            return
        metadata = getattr(self.workspace, "metadata", None)
        release_growth = getattr(metadata, "release_growth", None)
        if not callable(release_growth):
            release_growth = getattr(self.workspace, "release_growth", None)
        if not callable(release_growth):
            log.warning(
                "bridge growth reservation has no release hook transfer_id=%s",
                transfer_id,
            )
            return
        try:
            release_growth(reservation_id)
        except Exception:
            log.warning(
                "bridge growth reservation release failed transfer_id=%s",
                transfer_id,
            )

    def _renew_workspace_growth(self, reservation_id: str) -> None:
        metadata = getattr(self.workspace, "metadata", None)
        renew_growth = getattr(metadata, "renew_growth", None)
        if not callable(renew_growth):
            renew_growth = getattr(self.workspace, "renew_growth", None)
        if not callable(renew_growth):
            raise BridgeError("capacity_unavailable", "Workspace capacity reservation cannot be renewed")
        try:
            renew_growth(reservation_id)
        except Exception as exc:
            reason = getattr(exc, "reason", None)
            if reason in {"capacity_busy", "capacity_unavailable"}:
                raise BridgeError(reason, "Workspace host reserve cannot be renewed") from exc
            raise BridgeError("capacity_unavailable", "Workspace capacity reservation cannot be renewed") from exc

    async def _growth_reservation_heartbeat(
        self,
        reservation_id: str,
        renewal_errors: list[BridgeError],
    ) -> None:
        try:
            while True:
                await asyncio.sleep(self.growth_renewal_interval)
                try:
                    self._renew_workspace_growth(reservation_id)
                except BridgeError as exc:
                    renewal_errors.append(exc)
                    return
        except asyncio.CancelledError:
            raise

    @staticmethod
    def _raise_growth_renewal_error(renewal_errors: list[BridgeError] | None) -> None:
        if renewal_errors:
            raise renewal_errors[0]

    async def execute(self, principal: Any, connector: ConnectorDefinition, project: Any, tool: str, arguments: Mapping[str, Any], *, connector_id: str | None = None, contract_version: int = PUBLIC_CONTRACT_VERSION) -> dict[str, Any]:
        if contract_version != PUBLIC_CONTRACT_VERSION or tool not in {"copy_to_workspace", "copy_from_workspace"}:
            return {"status": "error", "reason": "upgrade_required", "message": f"Bridge tools are available only on connector contract v{PUBLIC_CONTRACT_VERSION}."}
        if not isinstance(arguments, Mapping):
            raise BridgeError("invalid_arguments", "arguments must be an object")
        allowed = {"project", "paths", "destination", "conflict_policy", "expected_destination_hashes", "idempotency_key"}
        if set(arguments) - allowed:
            raise BridgeError("invalid_arguments", "unknown bridge argument")
        direction = "to_workspace" if tool == "copy_to_workspace" else "from_workspace"
        self._check_policy(principal, connector, project, direction)
        name = _project_name(project)
        selected = arguments.get("paths")
        if not isinstance(selected, list) or not selected or len(selected) > MAX_TRANSFER_FILES or any(not isinstance(item, str) for item in selected):
            raise BridgeError("invalid_arguments", "paths must contain 1-10000 strings")
        normalized_selected = [_safe_rel(str(item)) for item in selected]
        if len(normalized_selected) != len(set(normalized_selected)):
            raise BridgeError("invalid_arguments", "paths must be unique")
        destination = _safe_rel(arguments.get("destination", "."), allow_empty=True)
        policy = arguments.get("conflict_policy", "fail")
        if policy not in {"fail", "skip", "replace", "rename"}:
            raise BridgeError("invalid_arguments", "invalid conflict_policy")
        expected = arguments.get("expected_destination_hashes", {})
        if not isinstance(expected, Mapping):
            raise BridgeError("invalid_arguments", "expected_destination_hashes must be an object")
        expected = {str(k): str(v).lower() for k, v in expected.items()}
        if any(len(value) != 64 or set(value) - _SHA256 for value in expected.values()):
            raise BridgeError("invalid_arguments", "destination hashes must be SHA-256 digests")
        clean = {key: value for key, value in arguments.items() if key != "idempotency_key"}
        key = arguments.get("idempotency_key")
        principal_id = _principal_id(principal)
        digest = _digest({"tool": tool, "project": name, "arguments": clean})
        if key is not None:
            if not isinstance(key, str) or not key or len(key.encode()) > 128:
                raise BridgeError("invalid_arguments", "idempotency_key must be a nonempty string of at most 128 bytes")
            with self._idempotency_lock:
                prior = self._idempotency.get((principal_id, key))
                if prior:
                    if prior[0] != digest:
                        raise BridgeError("path_conflict", "idempotency key was reused with different arguments")
                    return dict(prior[1])

        workspace_id = await self._workspace_id(principal, connector_id)
        if key is not None:
            metadata = getattr(self.workspace, "metadata", None)
            lookup = getattr(metadata, "idempotent", None)
            if callable(lookup):
                try:
                    replay = lookup(workspace_id, f"bridge:{key}", digest)
                except Exception as exc:
                    raise BridgeError("path_conflict", "idempotency key was reused with different arguments") from exc
                if replay is not None:
                    return dict(replay)
        transfer_id = str(uuid.uuid4())
        stage: _Stage | None = None
        transfer_lease: str | None = None
        growth_reservation: str | None = None
        growth_heartbeat: asyncio.Task[None] | None = None
        growth_renewal_errors: list[BridgeError] = []
        post_commit_error: BridgeError | None = None
        committed: list[str] = []
        skipped: list[str] = []
        try:
            metadata = getattr(self.workspace, "metadata", None)
            acquire_lease = getattr(metadata, "lease", None)
            if callable(acquire_lease):
                try:
                    # Cover the largest supported synchronous transfer window.
                    # The lease is nonmutex: mutation queues below still provide
                    # serialization, while retention/idle cleanup sees activity.
                    transfer_lease = acquire_lease(
                        workspace_id, "transfer", 3600, owner=transfer_id
                    )
                except Exception as exc:
                    raise BridgeError("capacity_busy", "Workspace has an active transfer") from exc
            if direction == "to_workspace":
                files, dirs = self._knowledge_manifest(project, normalized_selected, destination)
                inventory = await self._workspace_inventory(principal, [item.path for item in files], connector_id)
                final_files: list[ManifestEntry] = []
                used = set(inventory)
                for item in files:
                    prior = inventory.get(item.path)
                    duplicate = item.path in {candidate.path for candidate in final_files}
                    if duplicate and policy == "fail":
                        raise BridgeError("path_conflict", "Transfer contains duplicate destination paths", conflicts=[item.path])
                    if duplicate and policy == "skip":
                        skipped.append(item.path)
                        continue
                    if duplicate and policy == "rename":
                        renamed = await self._unique_workspace_name(
                            principal, item.path, used, connector_id
                        )
                        used.add(renamed)
                        item = ManifestEntry(renamed, item.size, item.sha256, item.source, item.mode)
                        prior = inventory.get(item.path)
                    if prior:
                        actual = str(prior.get("sha256") or prior.get("bytes_sha256") or "").lower()
                        if policy == "skip":
                            skipped.append(item.path)
                            continue
                        if policy == "fail":
                            raise BridgeError("path_conflict", "Destination already exists", conflicts=[item.path])
                        if policy == "replace":
                            if expected.get(item.path) != actual:
                                raise BridgeError("stale_file", "replace requires the exact destination hash", path=item.path, actual_destination_hash=actual)
                        if policy == "rename":
                            renamed = await self._unique_workspace_name(
                                principal, item.path, used, connector_id
                            )
                            used.add(renamed)
                            item = ManifestEntry(renamed, item.size, item.sha256, item.source, item.mode)
                    final_files.append(item)
                    used.add(item.path)
                if not final_files:
                    return self._receipt(transfer_id, direction, name, [], skipped, files=0, bytes_count=0, manifest=())
                manifest = TransferManifest(transfer_id, workspace_id, direction, tuple(final_files), dirs)
                destination_snapshot = await self._workspace_inventory(
                    principal, [item.path for item in final_files], connector_id
                )
                self._check_workspace_transfer_ready(
                    workspace_id, growth_bytes=manifest.total_bytes
                )
                stage = self._stage(transfer_id, manifest)
                for item in final_files:
                    assert item.source is not None
                    target = stage.root / item.path
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(item.source, target)
                    if (
                        _hash_file(target) != (item.size, item.sha256)
                        or _hash_file(item.source) != (item.size, item.sha256)
                    ):
                        raise BridgeError("transfer_changed", "Project source changed during transfer", retryable=True)
                growth_reservation = self._reserve_workspace_growth(
                    workspace_id, manifest.total_bytes, transfer_id
                )
                if growth_reservation is not None:
                    # Validate that this deployment can renew before entering
                    # the potentially long broker transfer window.
                    self._renew_workspace_growth(growth_reservation)
                    growth_heartbeat = asyncio.create_task(
                        self._growth_reservation_heartbeat(
                            growth_reservation, growth_renewal_errors
                        )
                    )
                # The source snapshot is complete before destination queue
                # admission.  Streaming staged bytes is the destination
                # mutation and is therefore serialized with Workspace writes.
                async with await self._workspace_lock(workspace_id):
                    self._check_policy(principal, connector, project, direction)
                    self._raise_growth_renewal_error(growth_renewal_errors)
                    self._check_workspace_transfer_ready(workspace_id)
                    current_inventory = await self._workspace_inventory(
                        principal, [item.path for item in final_files], connector_id
                    )
                    if current_inventory != destination_snapshot:
                        raise BridgeError(
                            "path_conflict",
                            "Workspace destinations changed during transfer staging",
                            retryable=True,
                        )
                    post_commit_error = await self._stream_to_workspace(
                        stage, growth_renewal_errors
                    )
                committed = [item.path for item in final_files]
                # ``manifest`` was rebuilt from final_files above, so skipped
                # entries are already out of this total.
                bytes_count = sum(item.size for item in manifest.files)
            else:
                # Queue is held only for immutable snapshot production, then
                # released before the Knowledge project lock is acquired.
                async with await self._workspace_lock(workspace_id):
                    self._check_policy(principal, connector, project, direction)
                    self._check_workspace_transfer_ready(workspace_id)
                    realized = await self._workspace_manifest(
                        principal, normalized_selected, destination, connector_id
                    )
                    manifest = TransferManifest(
                        transfer_id, workspace_id, direction, realized
                    )
                    stage = self._stage(transfer_id, manifest)
                    try:
                        await self._broker("admit", manifest.wire())
                        await self._stage_from_workspace(stage)
                        await self._broker("commit", transfer_id)
                    except Exception:
                        try:
                            await self._broker("abort", transfer_id)
                        except Exception:
                            log.warning(
                                "bridge transfer abort failed transfer_id=%s",
                                transfer_id,
                            )
                        raise
                self._check_policy(principal, connector, project, direction)
                # ``bytes`` counts what was WRITTEN. A conflict_policy=skip
                # transfer used to report the staged manifest's total beside
                # file_count 0 (13.0.2).
                bytes_count = await self._commit_to_knowledge(project, manifest, stage.root, destination, policy, expected, committed, skipped)
            receipt = self._receipt(transfer_id, direction, name, committed, skipped, files=len(committed), bytes_count=bytes_count, manifest=manifest.files)
            receipt_persisted = False
            receipt_persist_error: Exception | None = None
            if key is not None:
                with self._idempotency_lock:
                    self._idempotency[(principal_id, key)] = (digest, dict(receipt))
                metadata = getattr(self.workspace, "metadata", None)
                save = getattr(metadata, "save_idempotent", None)
                if callable(save):
                    try:
                        save(workspace_id, f"bridge:{key}", digest, receipt)
                        receipt_persisted = True
                    except Exception as exc:
                        receipt_persist_error = exc
            if receipt_persist_error is not None:
                raise BridgeError(
                    "runtime_unavailable",
                    "Transfer committed but idempotency receipt could not be durably recorded",
                    committed=True,
                    receipt=receipt,
                    durable_replay=False,
                    prior_error=post_commit_error.reason if post_commit_error else None,
                    retryable=False,
                    reconciliation_required=True,
                ) from receipt_persist_error
            if post_commit_error is not None:
                raise BridgeError(
                    post_commit_error.reason,
                    str(post_commit_error),
                    committed=True,
                    receipt=receipt,
                    durable_replay=receipt_persisted,
                    retryable=receipt_persisted,
                    reconciliation_required=not receipt_persisted,
                ) from post_commit_error
            return receipt
        except BridgeError:
            raise
        finally:
            if growth_heartbeat is not None:
                growth_heartbeat.cancel()
                try:
                    await growth_heartbeat
                except asyncio.CancelledError:
                    pass
            self._release_workspace_growth(growth_reservation, transfer_id)
            self._cleanup(stage)
            if transfer_lease is not None:
                try:
                    self.workspace.metadata.release_lease(transfer_lease)
                except Exception:
                    log.warning(
                        "bridge transfer lease release failed transfer_id=%s",
                        transfer_id,
                    )

    async def copy_to_workspace(self, principal: Any, connector: ConnectorDefinition, project: Any, arguments: Mapping[str, Any], *, connector_id: str | None = None) -> dict[str, Any]:
        return await self.execute(principal, connector, project, "copy_to_workspace", arguments, connector_id=connector_id)

    async def copy_from_workspace(self, principal: Any, connector: ConnectorDefinition, project: Any, arguments: Mapping[str, Any], *, connector_id: str | None = None) -> dict[str, Any]:
        return await self.execute(principal, connector, project, "copy_from_workspace", arguments, connector_id=connector_id)

    async def _commit_to_knowledge(self, project: Any, manifest: TransferManifest, root: Path, destination: str, policy: str, expected: Mapping[str, str], committed: list[str], skipped: list[str]) -> int:
        """Write the staged files into the project; return the bytes committed."""
        docs = Path(project.documents_dir).resolve()
        committed_bytes = 0
        # Validate every destination before the first write.  A stale hash on
        # file N must not leave files 1..N-1 partially committed.
        planned: list[tuple[ManifestEntry, str]] = []
        used: set[str] = set()
        for item in manifest.files:
            target_rel = item.destination or _safe_rel(
                "/".join(
                    part
                    for part in (destination, PurePosixPath(item.path).name)
                    if part
                )
            )
            if target_rel in used:
                if policy == "skip":
                    skipped.append(target_rel)
                    continue
                if policy == "rename":
                    target_rel = _unique_project_name(docs, target_rel, used)
                else:
                    raise BridgeError(
                        "path_conflict",
                        "Transfer contains duplicate destination paths",
                        conflicts=[target_rel],
                    )
            lexical = docs.joinpath(*target_rel.split("/"))
            if lexical.is_symlink():
                raise BridgeError("path_conflict", "Destination is a symlink", path=target_rel)
            target = resolve_target(docs, target_rel)
            if target is None:
                raise BridgeError("invalid_path", "Destination escapes the project root")
            if target.exists():
                try:
                    target_info = target.lstat()
                except OSError as exc:
                    raise BridgeError("path_unavailable", "Destination could not be inspected") from exc
                if stat.S_ISLNK(target_info.st_mode) or not target.is_file():
                    raise BridgeError("path_conflict", "Destination is not a regular file", path=target_rel)
                if policy == "fail":
                    raise BridgeError("path_conflict", "Destination already exists", conflicts=[target_rel])
                if policy == "replace":
                    _, actual = _hash_file(target)
                    if expected.get(target_rel) != actual:
                        raise BridgeError("stale_file", "replace requires the exact destination hash", path=target_rel, actual_destination_hash=actual)
                if policy == "rename":
                    target_rel = _unique_project_name(docs, target_rel, used)
            elif policy == "replace" and target_rel in expected:
                raise BridgeError("stale_file", "replace destination no longer exists", path=target_rel)
            used.add(target_rel)
            planned.append((item, target_rel))
        # Destination checks and writes happen under Knowledge's lock only; the
        # Workspace queue was released above, preventing lock overlap/deadlock.
        lock_owner = self.knowledge_core or getattr(getattr(self, "watcher", None), "core", None)
        lock = getattr(lock_owner, "write_lock", None)
        context = lock(_project_name(project)) if callable(lock) else _AsyncNullContext()
        commit_used: set[str] = set()
        async with context:
            for item, target_rel in planned:
                lexical = docs.joinpath(*target_rel.split("/"))
                if lexical.is_symlink():
                    raise BridgeError("path_conflict", "Destination is a symlink", path=target_rel)
                target = resolve_target(docs, target_rel)
                if target is None:
                    raise BridgeError("invalid_path", "Destination escapes the project root")
                prior_hash = None
                if target.exists():
                    try:
                        target_info = target.lstat()
                    except OSError as exc:
                        raise BridgeError("path_unavailable", "Destination could not be inspected") from exc
                    if stat.S_ISLNK(target_info.st_mode) or not target.is_file():
                        raise BridgeError("path_conflict", "Destination is not a regular file", path=target_rel)
                    _, prior_hash = _hash_file(target)
                    if policy == "skip":
                        skipped.append(target_rel)
                        continue
                    if policy == "fail":
                        raise BridgeError("path_conflict", "Destination already exists", conflicts=[target_rel])
                    if policy == "replace" and expected.get(target_rel) != prior_hash:
                        raise BridgeError("stale_file", "replace requires the exact destination hash", path=target_rel, actual_destination_hash=prior_hash)
                    if policy == "rename":
                        target_rel = _unique_project_name(docs, target_rel, commit_used)
                        target = resolve_target(docs, target_rel)
                target.parent.mkdir(parents=True, exist_ok=True)
                if prior_hash and policy == "replace":
                    try:
                        backup_if_exists(docs, target_rel, keep=self.backup_keep)
                    except BackupError as exc:
                        raise BridgeError("backup_failed", "Destination backup failed; nothing was written") from exc
                source = root / item.path
                check = _hash_file(source)
                if check != (item.size, item.sha256):
                    raise BridgeError("transfer_changed", "Staged source no longer matches its manifest", retryable=True)
                tmp = target.with_name(f".{target.name}.cognita-transfer-{uuid.uuid4().hex}.tmp")
                try:
                    shutil.copyfile(source, tmp)
                    os.chmod(tmp, item.mode & 0o777)
                    os.replace(tmp, target)
                finally:
                    try:
                        tmp.unlink(missing_ok=True)
                    except OSError:
                        pass
                committed.append(target_rel)
                committed_bytes += item.size
                commit_used.add(target_rel)
                if self.watcher is not None:
                    marker = getattr(self.watcher, "_mark", None) or getattr(self.watcher, "mark_dirty", None)
                    if callable(marker):
                        try:
                            marker(_project_name(project), target_rel, "created")
                        except TypeError:
                            marker(_project_name(project), target_rel)
        # Reconciliation owns the same Knowledge write lock. Invoke it once,
        # only after the bridge commit lock has been released, or an in-process
        # core would deadlock trying to reacquire its non-reentrant async lock.
        if self.reconcile is not None and committed:
            await _invoke(self.reconcile(_project_name(project), list(committed)))
        return committed_bytes

    @staticmethod
    def _receipt(transfer_id: str, direction: str, project: str, committed: list[str], skipped: list[str], *, files: int | None = None, bytes_count: int = 0, manifest: Iterable[ManifestEntry] = ()) -> dict[str, Any]:
        return {"status": "success", "transfer_id": transfer_id, "direction": direction, "project": project, "file_count": files if files is not None else len(committed), "bytes": bytes_count, "manifest": [item.wire() for item in manifest], "committed": list(committed), "skipped": list(skipped)}


class _AsyncNullContext:
    async def __aenter__(self): return self
    async def __aexit__(self, *_): return False


class _SyncLockContext:
    def __init__(self, lock: Any): self.lock = lock
    async def __aenter__(self):
        result = self.lock.acquire()
        if inspect.isawaitable(result):
            await result
        return self
    async def __aexit__(self, *_):
        result = self.lock.release()
        if inspect.isawaitable(result):
            await result
        return False


async def bridge_tool_result(service: BridgeService | None, principal: Any, connector: ConnectorDefinition | None, project: Any, tool: str, arguments: Mapping[str, Any], *, connector_id: str | None = None, contract_version: int = PUBLIC_CONTRACT_VERSION) -> dict[str, Any]:
    try:
        if service is None:
            raise BridgeError("runtime_unavailable", "Knowledge–Workspace bridge is not configured")
        return await service.execute(principal, connector, project, tool, arguments, connector_id=connector_id, contract_version=contract_version)
    except BridgeError as exc:
        return {"status": "error", "reason": exc.reason, "message": str(exc), **exc.fields}
    except Exception:
        log.exception("unexpected bridge failure tool=%s", tool)
        return {"status": "error", "reason": "internal_error", "message": "Bridge operation failed"}
