from __future__ import annotations

import base64
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from cognita.runtime_broker.app import create_app
from cognita.runtime_broker.protocol import MAX_ARGUMENT_BYTES, MAX_RPC_BODY_BYTES
from cognita.workspace import BrokerRuntimeClient, MAX_FILE_BYTES, WorkspaceError

SECRET = "test-broker-secret-that-is-long-enough-123"
WORKSPACE_ID = "11111111-1111-4111-8111-111111111111"


class RecordingAdapter:
    def __init__(self):
        self.calls = []

    async def readiness_probe(self):
        return {"status": "ok"}

    async def inspect(self, workspace_id: UUID):
        return {"state": "running"}

    async def execute(self, workspace_id: UUID, operation: str, arguments: dict):
        self.calls.append((workspace_id, operation, arguments))
        return {"ok": True}


def _client(adapter: RecordingAdapter):
    app = create_app(secret=SECRET, adapter=adapter, startup_probe=True)
    return TestClient(app)


def _write(client: TestClient, *, text: str | None = None, encoded: str | None = None):
    arguments = {"path": "payload.bin"}
    if text is not None:
        arguments["text"] = text
    if encoded is not None:
        arguments["base64"] = encoded
    return BrokerRuntimeClient("http://runtime", SECRET, client=client).call(
        WORKSPACE_ID, "fs_write", arguments,
    )


def test_rpc_caps_cover_file_writes_escaped_text_and_bounded_edits():
    adapter = RecordingAdapter()
    with _client(adapter) as client:
        assert _write(client, text="x" * 40_000)["ok"] is True
        reported = "A" * 635_064
        assert _write(client, encoded=reported)["ok"] is True

        maximum_binary = base64.b64encode(b"x" * MAX_FILE_BYTES).decode("ascii")
        assert len(base64.b64decode(maximum_binary)) == MAX_FILE_BYTES
        assert _write(client, encoded=maximum_binary)["ok"] is True

        # Each control character expands to six JSON bytes. NUL is excluded by
        # the public API, while U+0001 remains supported.
        escaped_text = "\x01" * MAX_FILE_BYTES
        assert _write(client, text=escaped_text)["ok"] is True

        # The public edit validator bounds the serialized edit array at 1 MiB.
        edit_arguments = {
            "path": "payload.txt",
            "edits": [{"match": "m" * 500_000, "replacement": "r" * 500_000}],
        }
        result = BrokerRuntimeClient("http://runtime", SECRET, client=client).call(
            WORKSPACE_ID, "fs_edit", edit_arguments,
        )
        assert result["ok"] is True

    assert [call[1] for call in adapter.calls] == ["fs_write"] * 4 + ["fs_edit"]


def test_rpc_caps_cover_maximum_escaped_path_list():
    adapter = RecordingAdapter()
    # 1,000 distinct paths close to the public 4 KiB UTF-8 path bound. The
    # HTTP JSON encoder escapes each supplementary character as 12 ASCII
    # bytes, so this exercises the body cap as well as the argument cap.
    prefix = ["😀" * 63] * 15
    paths = ["/".join([*prefix, "😀" * 50 + str(index)]) for index in range(1_000)]
    arguments = {"paths": paths, "recursive": False, "expected_hashes": {}}

    with _client(adapter) as client:
        result = BrokerRuntimeClient("http://runtime", SECRET, client=client).call(
            WORKSPACE_ID, "fs_remove", arguments,
        )
    assert result["ok"] is True
    assert adapter.calls[0][1] == "fs_remove"
    assert len(adapter.calls[0][2]["paths"]) == 1_000
    assert MAX_ARGUMENT_BYTES == 16 * 1024**2
    assert MAX_RPC_BODY_BYTES > MAX_ARGUMENT_BYTES


@pytest.mark.parametrize(
    "arguments",
    [
        {"path": "payload.bin", "base64": base64.b64encode(b"x" * (MAX_FILE_BYTES + 1)).decode("ascii")},
        {"path": "payload.bin", "text": "x" * (MAX_ARGUMENT_BYTES + 1)},
    ],
    ids=["over-public-file-limit", "oversized-argument-schema"],
)
def test_typed_invalid_request_is_mapped_to_invalid_arguments(arguments):
    adapter = RecordingAdapter()
    with _client(adapter) as client:
        runtime = BrokerRuntimeClient("http://runtime", SECRET, client=client)
        with pytest.raises(WorkspaceError) as error:
            runtime.call(WORKSPACE_ID, "fs_write", arguments)
    assert error.value.reason == "invalid_arguments"
    assert adapter.calls == []


def test_oversized_http_body_is_typed_invalid_arguments():
    adapter = RecordingAdapter()
    arguments = {"path": "payload.bin", "text": "x" * (MAX_RPC_BODY_BYTES + 1)}
    with _client(adapter) as client:
        runtime = BrokerRuntimeClient("http://runtime", SECRET, client=client)
        with pytest.raises(WorkspaceError) as error:
            runtime.call(WORKSPACE_ID, "fs_write", arguments)
    assert error.value.reason == "invalid_arguments"
    assert adapter.calls == []


@pytest.mark.parametrize(
    "status,payload",
    [
        (400, {"code": "unknown_code", "retryable": False, "message": "bad"}),
        (400, {"code": "invalid_request"}),
        (401, {"data": {"ok": True}}),
        (500, {"code": "conflict", "retryable": False, "message": "bad"}),
    ],
    ids=["unknown-code", "malformed-envelope", "unauthorized-json", "server-error-json"],
)
def test_untrusted_http_error_payloads_remain_runtime_unavailable(status, payload):
    class Response:
        status_code = status

        def json(self):
            return payload

        def raise_for_status(self):
            raise RuntimeError("HTTP request failed")

    class Client:
        def post(self, *_args, **_kwargs):
            return Response()

    runtime = BrokerRuntimeClient("http://runtime", SECRET, client=Client())
    with pytest.raises(WorkspaceError) as error:
        runtime.call(WORKSPACE_ID, "fs_write", {"path": "payload.bin", "text": "x"})
    assert error.value.reason == "runtime_unavailable"


def test_transport_failure_remains_runtime_unavailable():
    class Client:
        def post(self, *_args, **_kwargs):
            raise OSError("connection failed")

    runtime = BrokerRuntimeClient("http://runtime", SECRET, client=Client())
    with pytest.raises(WorkspaceError) as error:
        runtime.call(WORKSPACE_ID, "fs_write", {"path": "payload.bin", "text": "x"})
    assert error.value.reason == "runtime_unavailable"
