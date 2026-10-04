"""13.2.5: copy_to_workspace imports into subdirectories, and the log says why not.

DESIGN-13.2-CONNECTOR-DIAGNOSTICS §4. On 2026-09-23 every copy_to_workspace on
prod failed after the first flat file worked: the SDK's ``copy_from_host`` does
not create the guest parent directory and nothing in the broker did either.
Every fake at every layer (bridge, broker, adapter) accepted any guest path, so
a nested path passed all of them. This file pins the adapter, which is the
layer that actually talks to the SDK, and the two log rules the same incident
produced: the reason is in the line, and an expected miss is not a warning.
"""

from __future__ import annotations

import logging
import types
from uuid import uuid4

import pytest

from cognita.__main__ import _ColorFormatter, _RedactingFormatter
from cognita.runtime_broker.sdk_adapter_v2 import MicrosandboxSdkAdapter, SdkOperationError
from cognita.workspace import BrokerRuntimeClient, WorkspaceError


class _NotFoundError(Exception):
    pass


class _Fs:
    """SDK 0.7.0 shape: non-recursive mkdir, exists/stat probes, copy_from_host."""

    def __init__(self, existing=("/workspace",)):
        self.existing = set(existing)
        self.directories: list[str] = []
        self.copies: list[tuple[str, str]] = []

    async def exists(self, path):
        return path in self.existing

    async def stat(self, path):
        if path in self.existing:
            return types.SimpleNamespace(kind="directory")
        raise _NotFoundError(path)

    async def mkdir(self, path):
        self.directories.append(path)
        self.existing.add(path)

    async def copy_from_host(self, host_path, guest_path):
        parent = guest_path.rsplit("/", 1)[0]
        if parent not in self.existing:
            raise RuntimeError("FilesystemError: parent missing")
        self.copies.append((host_path, guest_path))


def _adapter(monkeypatch, fs):
    owner_calls = []

    async def guest_exec(executable, argv, **kwargs):
        owner_calls.append(argv[-1])
        return types.SimpleNamespace(exit_code=0)

    handle = types.SimpleNamespace(fs=fs, exec=guest_exec)
    adapter = object.__new__(MicrosandboxSdkAdapter)
    adapter.timeout_seconds = 5

    async def connected(*_args, **_kwargs):
        return handle

    monkeypatch.setattr(adapter, "_connected", connected)
    return adapter, owner_calls


@pytest.mark.asyncio
async def test_copy_from_host_creates_missing_guest_parents_in_order(monkeypatch, tmp_path):
    fs = _Fs()
    adapter, owner_calls = _adapter(monkeypatch, fs)
    staged = tmp_path / "staged"
    staged.write_bytes(b"x")

    await adapter.copy_from_host(uuid4(), str(staged), "/workspace/docs/guides/intro.md")

    assert fs.directories == ["/workspace/docs", "/workspace/docs/guides"]
    assert fs.copies == [(str(staged), "/workspace/docs/guides/intro.md")]
    # Each created directory and the file itself are handed to the toolbox user.
    assert owner_calls == ["/workspace/docs", "/workspace/docs/guides", "/workspace/docs/guides/intro.md"]


@pytest.mark.asyncio
async def test_copy_from_host_flat_file_creates_nothing(monkeypatch, tmp_path):
    fs = _Fs()
    adapter, owner_calls = _adapter(monkeypatch, fs)
    staged = tmp_path / "staged"
    staged.write_bytes(b"x")

    await adapter.copy_from_host(uuid4(), str(staged), "/workspace/not-a-png.txt")

    assert fs.directories == []
    assert fs.copies == [(str(staged), "/workspace/not-a-png.txt")]
    assert owner_calls == ["/workspace/not-a-png.txt"]


@pytest.mark.asyncio
async def test_copy_from_host_reuses_existing_parents(monkeypatch, tmp_path):
    fs = _Fs(existing=("/workspace", "/workspace/docs"))
    adapter, _ = _adapter(monkeypatch, fs)
    staged = tmp_path / "staged"
    staged.write_bytes(b"x")

    await adapter.copy_from_host(uuid4(), str(staged), "/workspace/docs/guides/intro.md")

    assert fs.directories == ["/workspace/docs/guides"]


@pytest.mark.asyncio
async def test_copy_from_host_failure_logs_depth_and_host_presence_never_paths(monkeypatch, tmp_path, caplog):
    class BrokenCopyFs(_Fs):
        async def copy_from_host(self, host_path, guest_path):
            raise RuntimeError("FilesystemError: /private/guest/detail")

    fs = BrokenCopyFs()
    adapter, _ = _adapter(monkeypatch, fs)
    staged = tmp_path / "secret-name.bin"
    staged.write_bytes(b"x")

    with caplog.at_level(logging.WARNING, logger="cognita.runtime_broker.sdk_adapter_v2"):
        with pytest.raises(SdkOperationError) as failure:
            await adapter.copy_from_host(uuid4(), str(staged), "/workspace/a/b/c.txt")
    assert failure.value.category == "runtime_failure"
    line = next(r.getMessage() for r in caplog.records if r.getMessage().startswith("transfer import failed"))
    assert "stage=transfer_import category=runtime_failure guest_depth=4 host_file_present=True" in line
    assert "secret-name" not in caplog.text and "/private/guest" not in caplog.text
    assert "c.txt" not in caplog.text


def _runtime_client(code: str, stage: str):
    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"code": code, "stage": stage, "category": code, "correlation_id": "corr-9"}

    class Client:
        def post(self, *args, **kwargs):
            return Response()

    return BrokerRuntimeClient("http://runtime", "secret-token", client=Client())


def test_expected_stat_miss_is_debug_and_a_real_rejection_says_why(caplog):
    workspace_id = "33333333-3333-4333-8333-333333333333"
    with caplog.at_level(logging.DEBUG, logger="cognita.workspace"):
        with pytest.raises(WorkspaceError) as miss:
            _runtime_client("not_found", "fs_stat").call(workspace_id, "fs_stat", {"path": "docs/x.md"})
        with pytest.raises(WorkspaceError) as failure:
            _runtime_client("runtime_failure", "transfer_import").call(workspace_id, "transfer_commit", {})
    assert miss.value.reason == "path_unavailable"
    assert failure.value.reason == "runtime_unavailable"

    miss_record, failure_record = [r for r in caplog.records if r.name == "cognita.workspace"]
    assert miss_record.levelno == logging.DEBUG
    assert failure_record.levelno == logging.WARNING
    assert failure_record.getMessage() == (
        "Workspace broker rejected operation=transfer_commit category=runtime_failure "
        f"stage=transfer_import reason=runtime_unavailable workspace_id={workspace_id}"
    )
    assert "secret-token" not in caplog.text and "docs/x.md" not in caplog.text


def _record_with_extras(**extra) -> logging.LogRecord:
    record = logging.LogRecord("cognita.workspace", logging.WARNING, "x", 0,
                               "Workspace runtime operation failed", (), None)
    record.__dict__.update(extra)
    return record


def test_console_formatters_print_extra_fields_on_the_message_line():
    record = _record_with_extras(category="not_found", stage="fs_stat", correlation_id=None,
                                 event="workspace_broker_failure")
    plain = _RedactingFormatter("%(levelname)s %(name)s: %(message)s").format(record)
    assert plain.endswith("Workspace runtime operation failed category=not_found "
                          "event=workspace_broker_failure stage=fs_stat")
    color = _ColorFormatter().format(record)
    assert "category=not_found" in color and "stage=fs_stat" in color


def test_extra_fields_stay_ahead_of_tracebacks_and_are_redacted():
    try:
        raise ValueError("boom")
    except ValueError:
        import sys
        record = logging.LogRecord("cognita.workspace", logging.ERROR, "x", 0, "failed", (), sys.exc_info())
    record.path = "/mcp/OwyQvlUamcYy46oY3pXZeBGOmmSMAdzy89/cognita-st/mcp"
    out = _RedactingFormatter("%(message)s").format(record)
    first, _, rest = out.partition("\n")
    assert first == "failed path=/mcp/<token>/cognita-st/mcp"
    assert "Traceback" in rest and "OwyQvlUamc" not in out


def test_plain_records_render_exactly_as_before():
    record = logging.LogRecord("cognita.gateway", logging.INFO, "x", 0, "tool call tool=%s", ("x",), None)
    assert _RedactingFormatter("%(message)s").format(record) == "tool call tool=x"
