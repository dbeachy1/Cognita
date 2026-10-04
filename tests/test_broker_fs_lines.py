"""Tests for release 12.18.0 Release A broker additions: ``fs_lines``,
``fs_usage``, and ``job_get``'s ``tail_lines`` (DESIGN-12.18-WORKSPACE-NEXT-FEATURES
SS3.3, SS3.4, SS10.1).

Covers four layers, per the coding brief's acceptance criteria:

* ``validation.py`` bounds -- each edge in SS10.1's bounds table.
* ``sdk_adapter_v2.MicrosandboxSdkAdapter._filesystem``/``_job`` -- adapter
  behavior against a fake ``_exec``/guest filesystem, no real sandbox needed.
* ``sdk_adapter_v2._FS_LINES_PROGRAM`` itself -- it is plain Python 3, so it
  is run directly with the venv interpreter against a real temp file rather
  than faked; this is the only thing in this module that touches the
  filesystem for real.
* ``BrokerService._execute`` / ``protocol.MAX_RESPONSE_BYTES`` -- the
  response-size ceiling a review found still sized for a 256 KiB pre-fs_lines
  world even though the declared read/output bound is 1 MiB base64-encoded.
"""

from __future__ import annotations

import base64
import json
import subprocess
import sys
import types
from uuid import uuid4

import pytest

from cognita.runtime_broker import sdk_adapter_v2 as sdk
from cognita.runtime_broker.protocol import (
    MAX_RESPONSE_BYTES,
    BrokerOperation,
    RpcRequest,
    RpcSuccess,
)
from cognita.runtime_broker.service import BrokerService
from cognita.runtime_broker.validation import (
    ArgumentError,
    normalize_rpc_arguments,
    validate_filesystem_arguments,
)

# ---------------------------------------------------------------------------
# validation.py bounds (SS10.1)
# ---------------------------------------------------------------------------


def test_fs_lines_requires_exactly_one_of_range_or_tail():
    with pytest.raises(ArgumentError):
        validate_filesystem_arguments(BrokerOperation.FS_LINES, {"path": "notes.txt"})
    with pytest.raises(ArgumentError):
        validate_filesystem_arguments(
            BrokerOperation.FS_LINES,
            {"path": "notes.txt", "start_line": 1, "tail_lines": 5},
        )
    # A range with only end_line, or only start_line, is still "a range" and
    # is accepted on its own.
    assert validate_filesystem_arguments(
        BrokerOperation.FS_LINES, {"path": "notes.txt", "start_line": 3},
    )["start_line"] == 3
    assert validate_filesystem_arguments(
        BrokerOperation.FS_LINES, {"path": "notes.txt", "end_line": 9},
    )["end_line"] == 9
    assert validate_filesystem_arguments(
        BrokerOperation.FS_LINES, {"path": "notes.txt", "tail_lines": 5},
    )["tail_lines"] == 5


def test_fs_lines_rejects_offset_style_keys():
    with pytest.raises(ArgumentError):
        validate_filesystem_arguments(
            BrokerOperation.FS_LINES, {"path": "notes.txt", "offset": 0, "tail_lines": 5},
        )


def test_fs_lines_line_bounds_are_strict():
    # lines >= 1
    with pytest.raises(ArgumentError):
        validate_filesystem_arguments(
            BrokerOperation.FS_LINES, {"path": "notes.txt", "start_line": 0},
        )
    assert validate_filesystem_arguments(
        BrokerOperation.FS_LINES, {"path": "notes.txt", "start_line": 1},
    )["start_line"] == 1
    with pytest.raises(ArgumentError):
        validate_filesystem_arguments(
            BrokerOperation.FS_LINES, {"path": "notes.txt", "end_line": 0},
        )
    # end_line >= start_line when both given
    with pytest.raises(ArgumentError):
        validate_filesystem_arguments(
            BrokerOperation.FS_LINES,
            {"path": "notes.txt", "start_line": 5, "end_line": 4},
        )
    assert validate_filesystem_arguments(
        BrokerOperation.FS_LINES,
        {"path": "notes.txt", "start_line": 5, "end_line": 5},
    )["end_line"] == 5


def test_fs_lines_tail_lines_bounds_are_1_to_10000():
    for invalid in (0, 10_001):
        with pytest.raises(ArgumentError):
            validate_filesystem_arguments(
                BrokerOperation.FS_LINES, {"path": "notes.txt", "tail_lines": invalid},
            )
    for valid in (1, 10_000):
        assert validate_filesystem_arguments(
            BrokerOperation.FS_LINES, {"path": "notes.txt", "tail_lines": valid},
        )["tail_lines"] == valid


def test_fs_lines_max_bytes_bounds_are_1_to_1048576():
    for invalid in (0, 1_048_577):
        with pytest.raises(ArgumentError):
            validate_filesystem_arguments(
                BrokerOperation.FS_LINES,
                {"path": "notes.txt", "tail_lines": 5, "max_bytes": invalid},
            )
    for valid in (1, 1_048_576):
        assert validate_filesystem_arguments(
            BrokerOperation.FS_LINES,
            {"path": "notes.txt", "tail_lines": 5, "max_bytes": valid},
        )["max_bytes"] == valid
    # Omitted max_bytes defaults the same way fs_read's does.
    assert validate_filesystem_arguments(
        BrokerOperation.FS_LINES, {"path": "notes.txt", "tail_lines": 5},
    )["max_bytes"] == 1_048_576


def test_fs_lines_routes_through_normalize_rpc_arguments():
    result = normalize_rpc_arguments(
        BrokerOperation.FS_LINES, {"path": "notes.txt", "tail_lines": 5},
    )
    assert result["path"] == "/workspace/notes.txt"
    assert result["tail_lines"] == 5


def test_fs_usage_requires_the_workspace_root():
    assert validate_filesystem_arguments(
        BrokerOperation.FS_USAGE, {"path": "/workspace"},
    ) == {"path": "/workspace"}
    for other in ("/workspace/sub", "sub"):
        with pytest.raises(ArgumentError):
            validate_filesystem_arguments(BrokerOperation.FS_USAGE, {"path": other})


def test_fs_usage_rejects_unknown_keys():
    with pytest.raises(ArgumentError):
        validate_filesystem_arguments(
            BrokerOperation.FS_USAGE, {"path": "/workspace", "recursive": True},
        )


def test_job_get_tail_lines_bounds_are_1_to_10000():
    for invalid in (0, 10_001, True):
        with pytest.raises(ArgumentError):
            normalize_rpc_arguments(
                BrokerOperation.JOB_GET, {"job_id": "abc", "tail_lines": invalid},
            )
    for valid in (1, 10_000):
        result = normalize_rpc_arguments(
            BrokerOperation.JOB_GET, {"job_id": "abc", "tail_lines": valid},
        )
        assert result["tail_lines"] == valid


def test_job_get_without_tail_lines_matches_todays_normalized_arguments():
    result = normalize_rpc_arguments(BrokerOperation.JOB_GET, {"job_id": "abc"})
    assert "tail_lines" not in result
    assert result == {
        "job_id": "abc", "stdout_offset": 0, "stderr_offset": 0, "max_bytes": 1024 * 1024,
    }


# ---------------------------------------------------------------------------
# sdk_adapter_v2.MicrosandboxSdkAdapter._filesystem: fs_lines
# ---------------------------------------------------------------------------


def _bind_pinned_adapter(monkeypatch):
    monkeypatch.setattr(
        sdk, "pinned_sdk_info",
        lambda: sdk.SdkRuntimeInfo("0.7.0", sdk.PINNED_RUNTIME_ROOT, "test"),
    )
    return sdk.MicrosandboxSdkAdapter(timeout_seconds=10)


@pytest.mark.asyncio
async def test_fs_lines_adapter_decodes_one_guest_exec_json_line(monkeypatch):
    adapter = _bind_pinned_adapter(monkeypatch)

    async def connected(*_args, **_kwargs):
        return types.SimpleNamespace(fs=types.SimpleNamespace())

    payload = {
        "content_b64": base64.b64encode(b"line3\nline4\nline5\n").decode("ascii"),
        "bytes": 18, "start_line": 3, "end_line": 5,
        "total_lines": 10_000, "total_bytes": 123_456, "has_more": False,
    }
    calls = []

    async def exec_call(workspace_id, executable, argv, *, cwd, env, deadline):
        calls.append((executable, argv, cwd, env))
        return sdk.ExecResult(json.dumps(payload).encode(), b"", 0)

    monkeypatch.setattr(adapter, "_connected", connected)
    monkeypatch.setattr(adapter, "_exec", exec_call)
    result = await adapter._filesystem(uuid4(), "fs_lines", {
        "path": "/workspace/notes.txt", "start_line": 3, "end_line": 5,
        "max_bytes": 1_048_576,
    })
    assert calls[0][0] == "python3"
    assert calls[0][1][0] == "-c"
    assert calls[0][1][1] == sdk._FS_LINES_PROGRAM
    assert calls[0][1][2:] == ["/workspace/notes.txt", "3", "5", "0", "1048576"]
    assert result["path"] == "/workspace/notes.txt"
    assert result["start_line"] == 3
    assert result["end_line"] == 5
    assert result["total_lines"] == 10_000
    assert result["has_more"] is False
    assert base64.b64decode(result["content_b64"]) == b"line3\nline4\nline5\n"


@pytest.mark.asyncio
async def test_fs_lines_adapter_maps_exit_2_to_not_found(monkeypatch):
    adapter = _bind_pinned_adapter(monkeypatch)

    async def connected(*_args, **_kwargs):
        return types.SimpleNamespace(fs=types.SimpleNamespace())

    async def exec_call(*_args, **_kwargs):
        return sdk.ExecResult(b"", b"", 2)

    monkeypatch.setattr(adapter, "_connected", connected)
    monkeypatch.setattr(adapter, "_exec", exec_call)
    with pytest.raises(sdk.SdkOperationError) as failure:
        await adapter._filesystem(uuid4(), "fs_lines", {
            "path": "/workspace/missing.txt", "tail_lines": 5, "max_bytes": 1024,
        })
    assert failure.value.category == "not_found"
    assert failure.value.stage == "fs_lines"


@pytest.mark.asyncio
async def test_fs_lines_adapter_maps_other_nonzero_exit_to_runtime_failure(monkeypatch):
    adapter = _bind_pinned_adapter(monkeypatch)

    async def connected(*_args, **_kwargs):
        return types.SimpleNamespace(fs=types.SimpleNamespace())

    async def exec_call(*_args, **_kwargs):
        return sdk.ExecResult(b"", b"", 1)

    monkeypatch.setattr(adapter, "_connected", connected)
    monkeypatch.setattr(adapter, "_exec", exec_call)
    with pytest.raises(sdk.SdkOperationError) as failure:
        await adapter._filesystem(uuid4(), "fs_lines", {
            "path": "/workspace/notes.txt", "tail_lines": 5, "max_bytes": 1024,
        })
    assert failure.value.category == "runtime_failure"
    assert failure.value.stage == "fs_lines"


# ---------------------------------------------------------------------------
# sdk_adapter_v2.MicrosandboxSdkAdapter._filesystem: fs_usage
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fs_usage_parses_du_output_and_sorts_descending(monkeypatch):
    adapter = _bind_pinned_adapter(monkeypatch)

    async def connected(*_args, **_kwargs):
        return types.SimpleNamespace(fs=types.SimpleNamespace())

    entries = [{"path": f"/workspace/dir{i}", "kind": "directory", "size": 0} for i in range(3)]

    async def bounded_listing(handle, path, recursive, limit, deadline):
        assert path == "/workspace"
        assert recursive is False
        return entries, False

    du_output = "".join(f"{(i + 1) * 1000}\t/workspace/dir{i}\n" for i in range(3))
    exec_calls = []

    async def exec_call(workspace_id, executable, argv, *, cwd, env, deadline):
        exec_calls.append((executable, argv))
        return sdk.ExecResult(du_output.encode(), b"", 0)

    monkeypatch.setattr(adapter, "_connected", connected)
    monkeypatch.setattr(adapter, "_bounded_guest_listing", bounded_listing)
    monkeypatch.setattr(adapter, "_exec", exec_call)
    result = await adapter._filesystem(uuid4(), "fs_usage", {"path": "/workspace"})
    assert exec_calls[0][0] == "du"
    assert exec_calls[0][1][:3] == ["-s", "--block-size=1", "--"]
    assert exec_calls[0][1][3:] == [entry["path"] for entry in entries]
    assert result["entries"] == [
        {"path": "/workspace/dir2", "bytes": 3000},
        {"path": "/workspace/dir1", "bytes": 2000},
        {"path": "/workspace/dir0", "bytes": 1000},
    ]
    assert result["truncated"] is False
    # 12.18.2 (bug 1): total_bytes is the sum over every parsed entry -- the
    # value workspace.py's refresh_usage()/info() trust in place of the SDK
    # volume's unreliable used_bytes.
    assert result["total_bytes"] == 1000 + 2000 + 3000


@pytest.mark.asyncio
async def test_fs_usage_caps_at_20_entries_and_reports_truncated(monkeypatch):
    adapter = _bind_pinned_adapter(monkeypatch)

    async def connected(*_args, **_kwargs):
        return types.SimpleNamespace(fs=types.SimpleNamespace())

    entries = [{"path": f"/workspace/d{i}", "kind": "directory", "size": 0} for i in range(25)]

    async def bounded_listing(*_args, **_kwargs):
        return entries, False

    du_output = "".join(f"{i}\t/workspace/d{i}\n" for i in range(25))

    async def exec_call(*_args, **_kwargs):
        return sdk.ExecResult(du_output.encode(), b"", 0)

    monkeypatch.setattr(adapter, "_connected", connected)
    monkeypatch.setattr(adapter, "_bounded_guest_listing", bounded_listing)
    monkeypatch.setattr(adapter, "_exec", exec_call)
    result = await adapter._filesystem(uuid4(), "fs_usage", {"path": "/workspace"})
    assert len(result["entries"]) == 20
    assert result["truncated"] is True
    assert result["entries"][0]["bytes"] == 24
    # 12.18.2 (bug 1): total_bytes sums ALL 25 parsed entries, not just the
    # 20 the response displays -- the whole point is a truthful total for a
    # Workspace with more than 20 top-level entries.
    assert result["total_bytes"] == sum(range(25))


@pytest.mark.asyncio
async def test_fs_usage_partial_du_failure_still_returns_parsed_rows(monkeypatch):
    adapter = _bind_pinned_adapter(monkeypatch)

    async def connected(*_args, **_kwargs):
        return types.SimpleNamespace(fs=types.SimpleNamespace())

    entries = [
        {"path": "/workspace/ok", "kind": "directory", "size": 0},
        {"path": "/workspace/unreadable", "kind": "directory", "size": 0},
    ]

    async def bounded_listing(*_args, **_kwargs):
        return entries, False

    # du exits 1 on an unreadable subdirectory but still prints the totals it
    # could measure.
    async def exec_call(*_args, **_kwargs):
        return sdk.ExecResult(b"500\t/workspace/ok\n", b"du: cannot read directory", 1)

    monkeypatch.setattr(adapter, "_connected", connected)
    monkeypatch.setattr(adapter, "_bounded_guest_listing", bounded_listing)
    monkeypatch.setattr(adapter, "_exec", exec_call)
    result = await adapter._filesystem(uuid4(), "fs_usage", {"path": "/workspace"})
    assert result["entries"] == [{"path": "/workspace/ok", "bytes": 500}]
    assert result["total_bytes"] is None


@pytest.mark.asyncio
async def test_fs_usage_no_output_at_all_has_no_total(monkeypatch):
    adapter = _bind_pinned_adapter(monkeypatch)

    async def connected(*_args, **_kwargs):
        return types.SimpleNamespace(fs=types.SimpleNamespace())

    entries = [{"path": "/workspace/dir0", "kind": "directory", "size": 0}]

    async def bounded_listing(*_args, **_kwargs):
        return entries, False

    async def exec_call(*_args, **_kwargs):
        return sdk.ExecResult(b"", b"du: fatal error", 1)

    monkeypatch.setattr(adapter, "_connected", connected)
    monkeypatch.setattr(adapter, "_bounded_guest_listing", bounded_listing)
    monkeypatch.setattr(adapter, "_exec", exec_call)
    result = await adapter._filesystem(uuid4(), "fs_usage", {"path": "/workspace"})
    assert result == {"entries": [], "truncated": False, "total_bytes": None}


@pytest.mark.asyncio
async def test_fs_usage_with_no_top_level_entries_skips_du_entirely(monkeypatch):
    adapter = _bind_pinned_adapter(monkeypatch)

    async def connected(*_args, **_kwargs):
        return types.SimpleNamespace(fs=types.SimpleNamespace())

    async def bounded_listing(*_args, **_kwargs):
        return [], False

    async def exec_call(*_args, **_kwargs):
        raise AssertionError("du must not run when there are no top-level entries")

    monkeypatch.setattr(adapter, "_connected", connected)
    monkeypatch.setattr(adapter, "_bounded_guest_listing", bounded_listing)
    monkeypatch.setattr(adapter, "_exec", exec_call)
    result = await adapter._filesystem(uuid4(), "fs_usage", {"path": "/workspace"})
    # 12.18.2 (bug 1): an empty Workspace's total_bytes is 0, not absent.
    assert result == {"entries": [], "truncated": False, "total_bytes": 0}


@pytest.mark.asyncio
async def test_fs_usage_bounded_listing_does_not_claim_a_complete_total(monkeypatch):
    adapter = _bind_pinned_adapter(monkeypatch)
    entries = [{"path": f"/workspace/f{i}", "kind": "file", "size": 1}
               for i in range(sdk.FS_USAGE_LISTING_LIMIT)]

    async def connected(*_args, **_kwargs):
        return types.SimpleNamespace(fs=types.SimpleNamespace())

    async def bounded_listing(_handle, _path, _recursive, limit, _deadline):
        assert limit == sdk.FS_USAGE_LISTING_LIMIT
        return entries, True  # one or more entries beyond the bound

    async def exec_call(*_args, **_kwargs):
        return sdk.ExecResult(
            b"".join(f"1\t{entry['path']}\n".encode() for entry in entries), b"", 0,
        )

    monkeypatch.setattr(adapter, "_connected", connected)
    monkeypatch.setattr(adapter, "_bounded_guest_listing", bounded_listing)
    monkeypatch.setattr(adapter, "_exec", exec_call)
    result = await adapter._filesystem(uuid4(), "fs_usage", {"path": "/workspace"})
    assert len(result["entries"]) == sdk.FS_USAGE_MAX_ENTRIES
    assert result["truncated"] is True
    assert result["total_bytes"] is None


@pytest.mark.parametrize("du_output", [
    b"1\t/workspace/a\n",  # missing target
    b"1\t/workspace/a\n2\t/workspace/a\n",  # duplicate target
    b"-1\t/workspace/a\n2\t/workspace/b\n",  # negative measurement
    b"1\t/workspace/a\n2\t/workspace/b",  # clipped final row
    b"1\t/workspace/a\n2\t/workspace/other\n",  # unexpected target
])
@pytest.mark.asyncio
async def test_fs_usage_ambiguous_du_output_has_no_total(monkeypatch, du_output):
    adapter = _bind_pinned_adapter(monkeypatch)
    entries = [{"path": f"/workspace/{name}", "kind": "file", "size": 1}
               for name in ("a", "b")]

    async def connected(*_args, **_kwargs):
        return types.SimpleNamespace(fs=types.SimpleNamespace())

    async def bounded_listing(*_args, **_kwargs):
        return entries, False

    async def exec_call(*_args, **_kwargs):
        return sdk.ExecResult(du_output, b"", 0)

    monkeypatch.setattr(adapter, "_connected", connected)
    monkeypatch.setattr(adapter, "_bounded_guest_listing", bounded_listing)
    monkeypatch.setattr(adapter, "_exec", exec_call)
    result = await adapter._filesystem(uuid4(), "fs_usage", {"path": "/workspace"})
    assert result["total_bytes"] is None


@pytest.mark.asyncio
async def test_fs_usage_incomplete_empty_listing_is_not_zero(monkeypatch):
    adapter = _bind_pinned_adapter(monkeypatch)

    async def connected(*_args, **_kwargs):
        return types.SimpleNamespace(fs=types.SimpleNamespace())

    async def bounded_listing(*_args, **_kwargs):
        return [], True

    async def exec_call(*_args, **_kwargs):
        raise AssertionError("du must not run without known targets")

    monkeypatch.setattr(adapter, "_connected", connected)
    monkeypatch.setattr(adapter, "_bounded_guest_listing", bounded_listing)
    monkeypatch.setattr(adapter, "_exec", exec_call)
    assert await adapter._filesystem(uuid4(), "fs_usage", {"path": "/workspace"}) == {
        "entries": [], "truncated": True, "total_bytes": None,
    }


@pytest.mark.asyncio
async def test_fs_usage_output_at_capture_limit_has_no_total(monkeypatch):
    adapter = _bind_pinned_adapter(monkeypatch)
    output = b"1\t/workspace/a\n"
    monkeypatch.setattr(sdk, "MAX_ENUM_OUTPUT_BYTES", len(output))

    async def connected(*_args, **_kwargs):
        return types.SimpleNamespace(fs=types.SimpleNamespace())

    async def bounded_listing(*_args, **_kwargs):
        return [{"path": "/workspace/a", "kind": "file", "size": 1}], False

    async def exec_call(*_args, **_kwargs):
        return sdk.ExecResult(output, b"", 0)

    monkeypatch.setattr(adapter, "_connected", connected)
    monkeypatch.setattr(adapter, "_bounded_guest_listing", bounded_listing)
    monkeypatch.setattr(adapter, "_exec", exec_call)
    result = await adapter._filesystem(uuid4(), "fs_usage", {"path": "/workspace"})
    assert result["entries"] == [{"path": "/workspace/a", "bytes": 1}]
    assert result["total_bytes"] is None


# ---------------------------------------------------------------------------
# sdk_adapter_v2.MicrosandboxSdkAdapter._job: job_get tail_lines
# ---------------------------------------------------------------------------


class _TwoStreamFs:
    """Fake guest fs backing one job's stdout/stderr files."""

    def __init__(self, files: dict[str, bytes]) -> None:
        self._files = files

    async def exists(self, path: str) -> bool:
        return path in self._files

    async def read(self, path: str) -> bytes:
        return self._files[path]


@pytest.mark.asyncio
async def test_job_get_tail_lines_returns_last_n_lines_of_each_stream(monkeypatch):
    adapter = _bind_pinned_adapter(monkeypatch)
    job_id = str(uuid4())
    directory = f"/workspace/.cognita/jobs/{job_id}"
    files = {
        f"{directory}/stdout": b"a\nb\nc\nd\n",
        f"{directory}/stderr": b"x\ny\nz\n",
    }
    handle = types.SimpleNamespace(fs=_TwoStreamFs(files))

    async def connected(*_args, **_kwargs):
        return handle

    async def exec_call(*_args, **_kwargs):
        return types.SimpleNamespace(exit_code=0, stdout=b'{"state":"succeeded"}')

    monkeypatch.setattr(adapter, "_connected", connected)
    monkeypatch.setattr(adapter, "_exec", exec_call)
    result = await adapter._job(uuid4(), "job_get", {"job_id": job_id, "tail_lines": 2})
    assert base64.b64decode(result["stdout"]) == b"c\nd\n"
    assert base64.b64decode(result["stderr"]) == b"y\nz\n"
    assert result["stdout_next_offset"] == len(files[f"{directory}/stdout"])
    assert result["stderr_next_offset"] == len(files[f"{directory}/stderr"])
    assert result["stdout_truncated"] is False
    assert result["stdout_has_more"] is False
    assert result["stderr_has_more"] is False
    # Terminal state: stdout_lines/stderr_lines count b"\n" in the whole
    # retained file, not just the tail slice that was returned.
    assert result["stdout_lines"] == 4
    assert result["stderr_lines"] == 3


@pytest.mark.asyncio
async def test_job_get_tail_lines_omits_line_counts_when_not_terminal(monkeypatch):
    adapter = _bind_pinned_adapter(monkeypatch)
    job_id = str(uuid4())
    directory = f"/workspace/.cognita/jobs/{job_id}"
    files = {f"{directory}/stdout": b"a\nb\n", f"{directory}/stderr": b""}
    handle = types.SimpleNamespace(fs=_TwoStreamFs(files))

    async def connected(*_args, **_kwargs):
        return handle

    async def exec_call(*_args, **_kwargs):
        return types.SimpleNamespace(exit_code=0, stdout=b'{"state":"running"}')

    monkeypatch.setattr(adapter, "_connected", connected)
    monkeypatch.setattr(adapter, "_exec", exec_call)
    result = await adapter._job(uuid4(), "job_get", {"job_id": job_id, "tail_lines": 2})
    assert "stdout_lines" not in result
    assert "stderr_lines" not in result


@pytest.mark.asyncio
async def test_job_get_without_tail_lines_matches_todays_response_shape(monkeypatch):
    """Snapshot the exact key set/values today's (no tail_lines) job_get
    produces, so a regression that leaks a new field into the default path
    is caught even though tail_lines is absent -- required by the brief's
    "keep every existing field and value exactly as today" rule."""

    adapter = _bind_pinned_adapter(monkeypatch)
    job_id = str(uuid4())
    directory = f"/workspace/.cognita/jobs/{job_id}"
    files = {f"{directory}/stdout": b"begin\n", f"{directory}/stderr": b""}
    handle = types.SimpleNamespace(fs=_TwoStreamFs(files))

    async def connected(*_args, **_kwargs):
        return handle

    async def exec_call(*_args, **_kwargs):
        return types.SimpleNamespace(exit_code=0, stdout=b'{"state":"succeeded"}')

    monkeypatch.setattr(adapter, "_connected", connected)
    monkeypatch.setattr(adapter, "_exec", exec_call)
    result = await adapter._job(uuid4(), "job_get", {
        "job_id": job_id, "stdout_offset": 0, "stderr_offset": 0, "max_bytes": 16,
    })
    assert result == {
        "job_id": job_id, "state": "succeeded",
        "stdout": "YmVnaW4K", "stdout_first_available_offset": 0,
        "stdout_next_offset": 6, "stdout_truncated": False, "stdout_has_more": False,
        "stderr": "", "stderr_first_available_offset": 0,
        "stderr_next_offset": 0, "stderr_truncated": False, "stderr_has_more": False,
        "has_more": False,
    }


# ---------------------------------------------------------------------------
# sdk_adapter_v2._FS_LINES_PROGRAM run directly with the venv interpreter
# ---------------------------------------------------------------------------


def _run_fs_lines_program(path, start, end, tail, max_bytes):
    """Run the guest helper as plain Python 3 -- no guest/sandbox needed."""

    completed = subprocess.run(
        [sys.executable, "-c", sdk._FS_LINES_PROGRAM,
         str(path), str(start), str(end), str(tail), str(max_bytes)],
        capture_output=True, timeout=30, check=False,
    )
    return completed


def test_fs_lines_program_tail_returns_exact_last_n_lines(tmp_path):
    fixture = tmp_path / "big.txt"
    fixture.write_bytes("".join(f"line{i}\n" for i in range(1, 10_001)).encode("utf-8"))
    completed = _run_fs_lines_program(fixture, 0, 0, 5, 1_048_576)
    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    content = base64.b64decode(payload["content_b64"]).decode("utf-8")
    assert content == "".join(f"line{i}\n" for i in range(9_996, 10_001))
    assert payload["has_more"] is False
    assert payload["total_lines"] == 10_000
    assert payload["start_line"] == 9_996
    assert payload["end_line"] == 10_000


def test_fs_lines_program_range_returns_requested_lines(tmp_path):
    fixture = tmp_path / "big.txt"
    fixture.write_bytes("".join(f"line{i}\n" for i in range(1, 10_001)).encode("utf-8"))
    completed = _run_fs_lines_program(fixture, 3, 5, 0, 1_048_576)
    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    content = base64.b64decode(payload["content_b64"]).decode("utf-8")
    assert content == "line3\nline4\nline5\n"
    assert payload["start_line"] == 3
    assert payload["end_line"] == 5
    assert payload["has_more"] is False
    assert payload["total_lines"] == 10_000


def test_fs_lines_program_max_bytes_truncation_keeps_whole_lines(tmp_path):
    # Truncation contract, matching the docstring above _FS_LINES_PROGRAM:
    # the program returns only whole lines, so `bytes` is always a multiple
    # of one line's byte length here, never a mid-line cut.
    fixture = tmp_path / "small.txt"
    fixture.write_bytes(b"aaaaa\nbbbbb\nccccc\n")  # three 6-byte lines
    completed = _run_fs_lines_program(fixture, 1, 3, 0, 10)
    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    assert payload["has_more"] is True
    assert payload["bytes"] <= 10
    assert payload["bytes"] % 6 == 0
    content = base64.b64decode(payload["content_b64"])
    assert content == b"aaaaa\n"


def test_fs_lines_program_tail_truncation_keeps_the_most_recent_lines(tmp_path):
    fixture = tmp_path / "small.txt"
    fixture.write_bytes(b"aaaaa\nbbbbb\nccccc\n")  # three 6-byte lines
    # tail_lines=3 (the whole file) but max_bytes only fits one line: the
    # kept line must be the LAST one, not the first.
    completed = _run_fs_lines_program(fixture, 0, 0, 3, 10)
    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    assert payload["has_more"] is True
    content = base64.b64decode(payload["content_b64"])
    assert content == b"ccccc\n"


def test_fs_lines_program_missing_file_exits_2(tmp_path):
    completed = _run_fs_lines_program(tmp_path / "missing.txt", 1, 1, 0, 1_048_576)
    assert completed.returncode == 2


# ---------------------------------------------------------------------------
# protocol.MAX_RESPONSE_BYTES: a 1 MiB read/output bound crosses the broker
# as base64 (4/3 expansion), so the response-size ceiling must hold more
# than the raw 1 MiB -- 256 KiB silently failed every fs_read/fs_lines/
# job_get reply over ~190 KB of content with "runtime response exceeded
# broker limit" (review finding on this release's broker work).
# ---------------------------------------------------------------------------


class _FixedPayloadAdapter:
    """Minimal RuntimeAdapter fake that hands back one fixed dict verbatim.

    Only ``readiness_probe`` (required by ``BrokerService.startup``) and
    ``execute`` (the seam ``service._execute`` calls for fs_*/job_* operations)
    are needed to exercise the MAX_RESPONSE_BYTES check in isolation from any
    real adapter/guest behavior.
    """

    def __init__(self, payload: dict) -> None:
        self.payload = payload

    async def readiness_probe(self):
        return {"status": "ok", "stage": "complete"}

    async def execute(self, workspace_id, operation, arguments):
        return self.payload


def test_max_response_bytes_covers_the_declared_1mib_bound_base64_encoded():
    # The bound itself: base64 expands 1 MiB to ceil(2**20 / 3) * 4 bytes
    # (~1.33 MiB); MAX_RESPONSE_BYTES must clear that plus JSON envelope
    # overhead, and must clear the old 256 KiB ceiling by a wide margin so a
    # future accidental revert is caught immediately by this assertion.
    base64_of_one_mib = -(-(1024 * 1024) // 3) * 4
    assert MAX_RESPONSE_BYTES > base64_of_one_mib
    assert MAX_RESPONSE_BYTES == 2 * 1024**2


@pytest.mark.asyncio
async def test_service_accepts_fs_read_response_at_the_1mib_bound():
    raw = b"\x00\x01" * (512 * 1024)  # 1 MiB, deliberately not text-compressible
    payload = {
        "path": "/workspace/big.bin",
        "content": base64.b64encode(raw).decode("ascii"),
        "encoding": "base64",
        "bytes": len(raw),
        "offset": 0,
        "next_offset": len(raw),
    }
    service = BrokerService(_FixedPayloadAdapter(payload))
    await service.startup()
    request = RpcRequest.model_validate({
        "request_id": str(uuid4()),
        "operation": "fs_read",
        "workspace_id": str(uuid4()),
        "arguments": {"path": "/workspace/big.bin", "max_bytes": 1024 * 1024, "binary": True},
    })
    result = await service.handle(request)
    assert isinstance(result, RpcSuccess), getattr(result, "message", result)
    assert result.data["bytes"] == len(raw)


@pytest.mark.asyncio
async def test_service_accepts_fs_lines_response_at_the_1mib_max_bytes_bound():
    raw = b"\x00\x01" * (512 * 1024)  # 1 MiB, deliberately not text-compressible
    payload = {
        "content_b64": base64.b64encode(raw).decode("ascii"),
        "bytes": len(raw), "start_line": 1, "end_line": 1,
        "total_lines": 1, "total_bytes": len(raw), "has_more": False,
    }
    service = BrokerService(_FixedPayloadAdapter(payload))
    await service.startup()
    request = RpcRequest.model_validate({
        "request_id": str(uuid4()),
        "operation": "fs_lines",
        "workspace_id": str(uuid4()),
        "arguments": {"path": "/workspace/big.bin", "tail_lines": 5, "max_bytes": 1024 * 1024},
    })
    result = await service.handle(request)
    assert isinstance(result, RpcSuccess), getattr(result, "message", result)
    assert result.data["bytes"] == len(raw)


@pytest.mark.asyncio
async def test_service_accepts_job_get_response_at_the_1mib_max_bytes_bound():
    raw_stream = base64.b64encode(b"\x00\x01" * (512 * 1024)).decode("ascii")
    job_id = uuid4()
    payload = {
        "job_id": str(job_id), "state": "succeeded",
        "stdout": raw_stream, "stdout_first_available_offset": 0,
        "stdout_next_offset": 1024 * 1024, "stdout_truncated": False, "stdout_has_more": False,
        "stderr": "", "stderr_first_available_offset": 0,
        "stderr_next_offset": 0, "stderr_truncated": False, "stderr_has_more": False,
        "has_more": False,
    }
    service = BrokerService(_FixedPayloadAdapter(payload))
    await service.startup()
    workspace_id = uuid4()
    # job_get's own state-lookup guard (service._execute) requires the job
    # to already exist and belong to this workspace before it will even ask
    # the adapter, independent of the MAX_RESPONSE_BYTES check under test.
    service.state_store.ensure_workspace(workspace_id, quota_bytes=4 * 1024**3)
    service.state_store.create_job(workspace_id, deadline=60, metadata={}, job_id=job_id)
    request = RpcRequest.model_validate({
        "request_id": str(uuid4()),
        "operation": "job_get",
        "workspace_id": str(workspace_id),
        "arguments": {"job_id": str(job_id), "max_bytes": 1024 * 1024},
    })
    result = await service.handle(request)
    assert isinstance(result, RpcSuccess), getattr(result, "message", result)
    assert result.data["stdout"] == raw_stream
