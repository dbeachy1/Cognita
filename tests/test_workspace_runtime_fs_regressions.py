"""Regressions for the pinned Microsandbox 0.7.0 filesystem API."""

from __future__ import annotations

import types
import hashlib
from uuid import uuid4

import pytest

from cognita.runtime_broker import sdk_adapter_v2 as sdk


class FilesystemError(Exception):
    """The pinned SDK uses this generic class for an absent stat target."""


class FakeFs:
    def __init__(self) -> None:
        self.directories = {"/", "/workspace"}
        self.files: dict[str, bytes] = {}
        self.owners: set[str] = set()

    async def exists(self, path: str) -> bool:
        return path in self.directories or path in self.files

    async def stat(self, path: str):
        if path in self.directories:
            return types.SimpleNamespace(kind="directory", size=0)
        if path in self.files:
            return types.SimpleNamespace(kind="file", size=len(self.files[path]))
        raise FilesystemError("missing target")

    async def mkdir(self, path: str) -> None:
        if await self.exists(path):
            raise FilesystemError("already exists")
        self.directories.add(path)

    async def write(self, path: str, data: bytes) -> None:
        self.files[path] = bytes(data)

    async def read(self, path: str) -> bytes:
        return self.files[path]

    def read_stream(self, path: str):
        """The pinned SDK's bounded read, including its absent-file behavior.

        It raises the same generic class as `stat` does, which is the whole
        reason the adapter cannot tell a missing file from a broken runtime by
        exception type alone.
        """
        files = self.files

        class _Stream:
            def __aiter__(self):
                if path not in files:
                    raise FilesystemError("missing read target")

                async def chunks():
                    yield files[path]

                return chunks()

        async def open_stream():
            return _Stream()

        return open_stream()

    async def copy(self, source: str, target: str) -> None:
        self.files[target] = self.files[source]

    async def rename(self, source: str, target: str) -> None:
        self.files[target] = self.files.pop(source)

    async def remove(self, path: str) -> None:
        self.files.pop(path)

    async def remove_dir(self, path: str) -> None:
        self.files = {name: value for name, value in self.files.items()
                      if not name.startswith(path + "/")}
        self.directories = {name for name in self.directories
                            if name != path and not name.startswith(path + "/")}


class FakeHandle:
    def __init__(self, fs: FakeFs) -> None:
        self.fs = fs
        self.chown_calls: list[tuple[str, str]] = []

    async def exec(self, executable: str, argv: list[str], **kwargs):
        assert executable == "/usr/bin/chown"
        assert argv[:2] == ["--no-dereference", "workspace:workspace"]
        assert kwargs["user"] == "root"
        self.fs.owners.add(argv[2])
        self.chown_calls.append((executable, argv[2]))
        return types.SimpleNamespace(exit_code=0)


@pytest.fixture
def adapter_and_fs(monkeypatch):
    monkeypatch.setattr(sdk, "pinned_sdk_info", lambda: sdk.SdkRuntimeInfo("0.7.0", sdk.PINNED_RUNTIME_ROOT, "test"))
    adapter = sdk.MicrosandboxSdkAdapter(timeout_seconds=10)
    fs = FakeFs()
    handle = FakeHandle(fs)

    async def connected(*_args, **_kwargs):
        return handle

    monkeypatch.setattr(adapter, "_connected", connected)
    return adapter, fs, handle


@pytest.mark.asyncio
async def test_copy_move_and_remove_use_pinned_sdk_signatures(adapter_and_fs):
    adapter, fs, _handle = adapter_and_fs
    workspace_id = uuid4()
    root = "/workspace/.cognita-self-test/run"
    alpha, beta, moved = (root + suffix for suffix in ("/alpha.txt", "/beta.txt", "/moved.txt"))

    await adapter._filesystem(workspace_id, "fs_mkdir", {"path": root, "parents": True})
    await adapter._filesystem(workspace_id, "fs_write", {
        "path": alpha, "text": "fixture", "content_encoding": "text",
    })
    assert (await adapter._filesystem(workspace_id, "fs_stat", {"path": alpha}))["size"] == 7
    copied = await adapter._filesystem(workspace_id, "fs_copy", {
        "sources": [alpha], "destination": beta, "conflict_policy": "fail",
    })
    assert copied["destination_sha256"] == hashlib.sha256(b"fixture").hexdigest()
    moved_receipt = await adapter._filesystem(workspace_id, "fs_move", {
        "sources": [beta], "destination": moved, "conflict_policy": "fail",
    })
    assert moved_receipt["destination_sha256"] == hashlib.sha256(b"fixture").hexdigest()
    assert beta not in fs.files and fs.files[moved] == b"fixture"
    await adapter._filesystem(workspace_id, "fs_remove", {"paths": [moved], "recursive": False})
    await adapter._filesystem(workspace_id, "fs_remove", {"paths": [root], "recursive": True})
    assert root not in fs.directories and alpha not in fs.files


@pytest.mark.asyncio
async def test_replace_does_not_delete_target_when_source_is_absent(adapter_and_fs):
    adapter, fs, _handle = adapter_and_fs
    target = "/workspace/keep.txt"
    fs.files[target] = b"keep"
    with pytest.raises(sdk.SdkOperationError) as failure:
        await adapter._filesystem(uuid4(), "fs_copy", {
            "sources": ["/workspace/missing.txt"], "destination": target,
            "conflict_policy": "replace",
        })
    assert failure.value.category == "not_found"
    assert fs.files[target] == b"keep"


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["fs_copy", "fs_move"])
async def test_replace_same_file_does_not_delete_source(adapter_and_fs, operation):
    adapter, fs, _handle = adapter_and_fs
    source = "/workspace/keep.txt"
    fs.files[source] = b"keep"
    with pytest.raises(sdk.SdkOperationError) as failure:
        await adapter._filesystem(uuid4(), operation, {
            "sources": [source], "destination": source,
            "conflict_policy": "replace",
        })
    assert failure.value.category == "conflict"
    assert fs.files[source] == b"keep"


@pytest.mark.asyncio
async def test_stale_hash_reports_expected_and_actual_hashes(adapter_and_fs):
    adapter, fs, _handle = adapter_and_fs
    source, target = "/workspace/source.txt", "/workspace/target.txt"
    fs.files[source], fs.files[target] = b"source", b"keep"
    with pytest.raises(sdk.SdkOperationError) as stale:
        await adapter._filesystem(uuid4(), "fs_write", {
            "path": source, "text": "replace", "content_encoding": "text",
            "expected_sha256": "0" * 64,
        })
    with pytest.raises(sdk.SdkOperationError) as conflict:
        await adapter._filesystem(uuid4(), "fs_copy", {
            "sources": [source], "destination": target, "conflict_policy": "fail",
        })
    assert stale.value.category == conflict.value.category == "conflict"
    assert stale.value.evidence == {
        "expected_sha256": "0" * 64,
        "actual_sha256": hashlib.sha256(b"source").hexdigest(),
    }
    with pytest.raises(sdk.SdkOperationError) as stale_edit:
        await adapter._filesystem(uuid4(), "fs_edit", {
            "path": source, "expected_sha256": "0" * 64,
            "edits": [{"match": "source", "replacement": "updated"}],
        })
    assert stale_edit.value.evidence == stale.value.evidence
    assert fs.files == {source: b"source", target: b"keep"}


@pytest.mark.asyncio
async def test_api_created_paths_are_writable_by_guest_jobs(adapter_and_fs):
    adapter, fs, _handle = adapter_and_fs
    root = "/workspace/.cognita-self-test/run"
    path = root + "/api.txt"
    await adapter._filesystem(uuid4(), "fs_write", {
        "path": path, "text": "fixture", "content_encoding": "text", "create_parents": True,
    })
    assert {"/workspace/.cognita-self-test", root, path} <= fs.owners
    assert root in fs.owners  # A guest process can create its venv/node output here.


@pytest.mark.asyncio
async def test_edit_object_updates_exact_content_and_hash(adapter_and_fs):
    adapter, fs, _handle = adapter_and_fs
    path = "/workspace/.cognita-self-test/run/alpha.txt"
    original = b"needle-one\n"
    fs.files[path] = original
    result = await adapter._filesystem(uuid4(), "fs_edit", {
        "path": path,
        "expected_sha256": hashlib.sha256(original).hexdigest(),
        "edits": [{"match": "needle-one", "replacement": "needle-two"}],
    })
    assert fs.files[path] == b"needle-two\n"
    assert result["sha256"] == hashlib.sha256(fs.files[path]).hexdigest()
    assert result["bytes"] == len(fs.files[path])


@pytest.mark.asyncio
async def test_read_returns_the_bounded_range(adapter_and_fs):
    adapter, fs, _handle = adapter_and_fs
    path = "/workspace/.cognita-self-test/run/alpha.txt"
    fs.files[path] = b"needle-one\n"
    result = await adapter._filesystem(uuid4(), "fs_read", {
        "path": path, "offset": 0, "max_bytes": 1024,
    })
    assert result["content"] == "needle-one\n"
    assert result["bytes"] == len(fs.files[path])


@pytest.mark.asyncio
async def test_reading_an_absent_path_is_not_found_not_a_runtime_failure(adapter_and_fs):
    """An absent read target must not be reported as a broken runtime.

    The pinned SDK raises one generic error class for both, and the app maps
    anything it does not recognize to `runtime_unavailable` -- so without this
    the caller cannot tell "your path is gone" from "the Workspace runtime is
    down". Incident, 2026-09-22: a live self-test read of a cleaned-up path
    reported `runtime_unavailable` and sent the diagnosis after the broker.
    """
    adapter, _fs, _handle = adapter_and_fs
    with pytest.raises(sdk.SdkOperationError) as failure:
        await adapter._filesystem(uuid4(), "fs_read", {
            "path": "/workspace/.cognita-self-test/run/missing.txt",
            "offset": 0, "max_bytes": 1024,
        })
    assert failure.value.category == "not_found"
    assert failure.value.stage == "fs_read"


@pytest.mark.asyncio
async def test_a_failing_existence_probe_leaves_the_runtime_failure_alone(adapter_and_fs, monkeypatch):
    """A runtime that cannot answer `exists` is a runtime failure, and says so."""
    adapter, fs, _handle = adapter_and_fs

    async def broken_exists(_path: str) -> bool:
        raise FilesystemError("runtime is down")

    monkeypatch.setattr(fs, "exists", broken_exists)
    with pytest.raises(sdk.SdkOperationError) as failure:
        await adapter._filesystem(uuid4(), "fs_read", {
            "path": "/workspace/.cognita-self-test/run/missing.txt",
            "offset": 0, "max_bytes": 1024,
        })
    assert failure.value.category == "runtime_failure"
    assert failure.value.stage == "fs_read"
