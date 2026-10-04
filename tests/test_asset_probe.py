"""Focused protocol and cleanup tests for the disposable asset-handoff probe."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import struct
import zlib
from pathlib import Path

import httpx
import pytest
from starlette.requests import ClientDisconnect, Request

from cognita.asset_probe import (
    MAX_INLINE_BASE64_CHARS,
    MAX_PNG_BYTES,
    PROBE_PROTOCOL_VERSION,
    TOOL_ARTIFACT,
    TOOL_INLINE,
    TOOL_PREPARE,
    TOOL_RESULT,
    UPLOAD_TTL_SECONDS,
    ProbeInputError,
    ProbeState,
    create_probe_app,
    validate_static_png,
)


def _png_chunk(kind: bytes, payload: bytes) -> bytes:
    return (
        struct.pack(">I", len(payload))
        + kind
        + payload
        + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
    )


PNG = (
    b"\x89PNG\r\n\x1a\n"
    + _png_chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0))
    + _png_chunk(b"IDAT", zlib.compress(b"\x00\x00\x00\x00\x00"))
    + _png_chunk(b"IEND", b"")
)
PNG_B64 = base64.b64encode(PNG).decode("ascii")
PNG_SHA256 = hashlib.sha256(PNG).hexdigest()


@pytest.fixture
def probe(tmp_path: Path):
    state = ProbeState(root_capability="root-capability", temp_dir=tmp_path / "staging")
    app = create_probe_app(state)
    yield app, state, tmp_path
    state.close()


async def _client(app: object):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://probe.test",
    )


async def _rpc(
    client: httpx.AsyncClient, state: ProbeState, method: str, params: dict | None = None
):
    response = await client.post(
        "/mcp/KEI",
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}},
    )
    assert response.status_code == 200
    return response.json()


def _tool_payload(response: dict) -> dict:
    content = response["result"]["content"][0]["text"]
    return json.loads(content)


@pytest.mark.asyncio
async def test_initialize_and_list_expose_only_probe_tools(probe):
    app, state, _ = probe
    async with await _client(app) as client:
        initialize = await _rpc(client, state, "initialize")
        assert initialize["result"]["protocolVersion"] == PROBE_PROTOCOL_VERSION
        listed = await _rpc(client, state, "tools/list")

    assert {tool["name"] for tool in listed["result"]["tools"]} == {
        TOOL_INLINE,
        TOOL_ARTIFACT,
        TOOL_PREPARE,
        TOOL_RESULT,
    }
    assert all(
        tool["inputSchema"]["additionalProperties"] is False for tool in listed["result"]["tools"]
    )
    artifact_tool = next(
        tool for tool in listed["result"]["tools"] if tool["name"] == TOOL_ARTIFACT
    )
    assert "_meta" not in artifact_tool
    artifact_schema = artifact_tool["inputSchema"]["properties"]["artifact"]
    assert set(artifact_schema["properties"]) == {"image_url", "output_hint"}
    assert artifact_schema["required"] == ["image_url"]
    assert artifact_schema["additionalProperties"] is False


@pytest.mark.asyncio
async def test_inline_png_hashes_validates_and_deletes_staging_file(probe):
    app, state, tmp_path = probe
    prompt = "keep this prompt out of logs"
    async with await _client(app) as client:
        response = await _rpc(
            client,
            state,
            "tools/call",
            {"name": TOOL_INLINE, "arguments": {"data_base64": PNG_B64, "prompt": prompt}},
        )

    result = _tool_payload(response)
    assert result["status"] == "success"
    assert result["transport"] == "inline"
    assert result["received_byte_count"] == len(PNG)
    assert result["received_sha256"] == PNG_SHA256
    assert result["png_valid"] is True
    assert result["png_width"] == 1
    assert result["png_height"] == 1
    assert result["prompt_present"] is True
    assert result["prompt_length"] == len(prompt.encode())
    assert prompt not in json.dumps(result)
    assert list((tmp_path / "staging").glob("*")) == []


@pytest.mark.asyncio
async def test_inline_rejects_bad_encoding_and_apng_without_leaking_bytes(probe):
    app, state, tmp_path = probe
    apng = PNG[:33] + _png_chunk(b"acTL", struct.pack(">II", 1, 0)) + PNG[33:]
    async with await _client(app) as client:
        bad = await _rpc(
            client,
            state,
            "tools/call",
            {"name": TOOL_INLINE, "arguments": {"data_base64": "%%%not-base64%%%"}},
        )
        animated = await _rpc(
            client,
            state,
            "tools/call",
            {"name": TOOL_INLINE, "arguments": {"data_base64": base64.b64encode(apng).decode()}},
        )

    assert _tool_payload(bad)["failure_reason"] == "invalid_base64"
    assert _tool_payload(animated)["failure_reason"] == "animated_png"
    assert list((tmp_path / "staging").glob("*")) == []


@pytest.mark.asyncio
async def test_tool_arguments_are_closed_and_required(probe):
    app, state, _ = probe
    async with await _client(app) as client:
        unknown = await _rpc(
            client,
            state,
            "tools/call",
            {"name": TOOL_INLINE, "arguments": {"data_base64": PNG_B64, "extra": True}},
        )
        missing = await _rpc(
            client,
            state,
            "tools/call",
            {"name": TOOL_ARTIFACT, "arguments": {}},
        )

    assert _tool_payload(unknown)["reason"] == "unknown_argument"
    assert _tool_payload(missing)["reason"] == "missing_argument"


@pytest.mark.asyncio
async def test_direct_put_uses_digest_ticket_and_rejects_replay(probe):
    app, state, tmp_path = probe
    async with await _client(app) as client:
        prepared = _tool_payload(
            await _rpc(
                client,
                state,
                "tools/call",
                {
                    "name": TOOL_PREPARE,
                    "arguments": {
                        "expected_size": len(PNG),
                        "expected_sha256": PNG_SHA256,
                        "prompt": "direct transfer",
                    },
                },
            )
        )
        uploaded = await client.put(
            prepared["upload_url"],
            content=PNG,
            headers={"content-type": "image/png", "content-length": str(len(PNG))},
        )
        replay = await client.put(
            prepared["upload_url"],
            content=PNG,
            headers={"content-type": "image/png", "content-length": str(len(PNG))},
        )
        result = _tool_payload(
            await _rpc(
                client,
                state,
                "tools/call",
                {"name": TOOL_RESULT, "arguments": {"attempt_id": prepared["attempt_id"]}},
            )
        )

    assert uploaded.status_code == 200
    assert uploaded.json()["received_byte_count"] == len(PNG)
    assert replay.status_code == 409
    assert result["received_sha256"] == PNG_SHA256
    assert result["png_valid"] is True
    assert list(tmp_path.glob("staging/*")) == []


@pytest.mark.asyncio
async def test_direct_put_rejects_size_and_media_type_mismatch(probe):
    app, state, _ = probe
    async with await _client(app) as client:
        prepared = _tool_payload(
            await _rpc(
                client,
                state,
                "tools/call",
                {
                    "name": TOOL_PREPARE,
                    "arguments": {
                        "expected_size": len(PNG) + 1,
                        "expected_sha256": PNG_SHA256,
                    },
                },
            )
        )
        wrong_type = await client.put(
            prepared["upload_url"],
            content=PNG,
            headers={"content-type": "application/octet-stream"},
        )

    assert wrong_type.status_code == 400
    assert wrong_type.json()["reason"] == "media_type_mismatch"


@pytest.mark.asyncio
async def test_generated_image_artifact_transfers_png_and_redacts_data_url(probe, caplog):
    app, state, tmp_path = probe
    data_url = f"data:image/png;base64,{PNG_B64}"
    output_hint = "do not retain this generated-image hint"
    caplog.set_level(logging.INFO, logger="cognita.asset_probe")
    async with await _client(app) as client:
        response = await _rpc(
            client,
            state,
            "tools/call",
            {
                "name": TOOL_ARTIFACT,
                "arguments": {
                    "artifact": {"image_url": data_url, "output_hint": output_hint},
                    "filename": "image.png",
                },
            },
        )

    serialized = json.dumps(_tool_payload(response))
    result = _tool_payload(response)
    assert result["status"] == "success"
    assert result["transport"] == "generated_image_data_url"
    assert result["received_byte_count"] == len(PNG)
    assert result["received_sha256"] == PNG_SHA256
    assert result["png_valid"] is True
    assert result["artifact_shape"]["fields"]["image_url"]["length"] == len(data_url)
    assert data_url not in serialized
    assert output_hint not in serialized
    assert data_url not in caplog.text
    assert output_hint not in caplog.text
    assert list((tmp_path / "staging").glob("*")) == []


@pytest.mark.asyncio
async def test_connector_path_and_project_isolation(probe):
    app, state, tmp_path = probe
    project = tmp_path / "project"
    project.mkdir()
    sentinel = project / "do-not-touch.txt"
    sentinel.write_text("untouched", encoding="utf-8")
    async with await _client(app) as client:
        unauthorized = await client.post(
            f"/probe/{state.root_capability}/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize"},
        )
        result = _tool_payload(
            await _rpc(
                client,
                state,
                "tools/call",
                {"name": TOOL_INLINE, "arguments": {"data_base64": PNG_B64}},
            )
        )

    assert unauthorized.status_code == 404
    assert result["status"] == "success"
    assert sentinel.read_text(encoding="utf-8") == "untouched"
    assert list((tmp_path / "staging").glob("*")) == []


def test_png_validator_rejects_crc_trailing_data_and_accepts_only_static_png():
    assert validate_static_png(PNG)["valid"] is True
    corrupted = bytearray(PNG)
    corrupted[-1] ^= 1
    assert validate_static_png(bytes(corrupted))["reason"] == "crc_mismatch"
    assert validate_static_png(PNG + b"trailing")["reason"] == "trailing_data"


def test_inline_enforces_encoded_and_raw_limits(monkeypatch, probe):
    import cognita.asset_probe as module

    _app, state, tmp_path = probe
    monkeypatch.setattr(module, "MAX_INLINE_BASE64_CHARS", 4)
    encoded = state.receive_inline(
        data_base64="AAAAA",
        expected_size=None,
        expected_sha256=None,
        prompt={"prompt_present": False, "prompt_length": 0, "prompt_sha256": None},
    )
    assert encoded.failure_reason == "encoded_limit"

    monkeypatch.setattr(module, "MAX_INLINE_BASE64_CHARS", len(PNG_B64) + 1)
    monkeypatch.setattr(module, "MAX_PNG_BYTES", 2)
    raw = state.receive_inline(
        data_base64=PNG_B64,
        expected_size=None,
        expected_sha256=None,
        prompt={"prompt_present": False, "prompt_length": 0, "prompt_sha256": None},
    )
    assert raw.failure_reason == "byte_limit"
    assert list((tmp_path / "staging").glob("*")) == []


def test_limits_are_bounded_and_the_probe_does_not_advertise_a_large_inline_payload():
    assert MAX_PNG_BYTES <= 4 * 1024 * 1024
    assert MAX_INLINE_BASE64_CHARS < 6_000_000


@pytest.mark.asyncio
async def test_expired_upload_ticket_is_rejected_and_result_state_is_pruned(tmp_path: Path):
    now = [1000.0]
    state = ProbeState(
        root_capability="expiry-capability", clock=lambda: now[0], temp_dir=tmp_path / "staging"
    )
    app = create_probe_app(state)
    try:
        async with await _client(app) as client:
            prepared = _tool_payload(
                await _rpc(
                    client,
                    state,
                    "tools/call",
                    {
                        "name": TOOL_PREPARE,
                        "arguments": {
                            "expected_size": len(PNG),
                            "expected_sha256": PNG_SHA256,
                        },
                    },
                )
            )
            now[0] += UPLOAD_TTL_SECONDS + 1
            expired = await client.put(
                prepared["upload_url"], content=PNG, headers={"content-type": "image/png"}
            )
            fetched = _tool_payload(
                await _rpc(
                    client,
                    state,
                    "tools/call",
                    {"name": TOOL_RESULT, "arguments": {"attempt_id": prepared["attempt_id"]}},
                )
            )
        assert expired.status_code == 410
        assert fetched["reason"] == "not_found"
    finally:
        state.close()


@pytest.mark.asyncio
async def test_canceled_direct_put_removes_partial_file(tmp_path: Path):
    state = ProbeState(temp_dir=tmp_path / "staging")
    token, result, _ = state.create_upload(
        expected_size=len(PNG),
        expected_sha256=PNG_SHA256,
        media_type="image/png",
        prompt={"prompt_present": False, "prompt_length": 0, "prompt_sha256": None},
    )
    events = iter(({"type": "http.request", "body": PNG[:4], "more_body": True},))

    async def receive():
        try:
            return next(events)
        except StopIteration as exc:
            raise asyncio.CancelledError from exc

    scope = {
        "type": "http",
        "method": "PUT",
        "path": "/upload",
        "headers": [(b"content-type", b"image/png")],
        "query_string": b"",
        "server": ("probe", 80),
        "client": ("client", 1),
        "scheme": "http",
        "http_version": "1.1",
    }
    with pytest.raises(asyncio.CancelledError):
        await state.receive_direct(Request(scope, receive), token)
    assert list((tmp_path / "staging").glob("*")) == []
    assert state.get_result(result.attempt_id).failure_reason == "client_disconnected"
    state.close()


def test_inline_streaming_rejects_padding_before_the_final_block(probe):
    _app, state, tmp_path = probe
    malformed = "A" * (64 * 1024 - 4) + "YQ==" + "Yg=="

    result = state.receive_inline(
        data_base64=malformed,
        expected_size=None,
        expected_sha256=None,
        prompt={"prompt_present": False, "prompt_length": 0, "prompt_sha256": None},
    )

    assert result.failure_reason == "invalid_base64"
    assert list((tmp_path / "staging").glob("*")) == []


@pytest.mark.asyncio
async def test_inline_expected_size_and_hash_are_independently_optional(probe):
    app, state, _ = probe
    async with await _client(app) as client:
        size_only = _tool_payload(
            await _rpc(
                client,
                state,
                "tools/call",
                {
                    "name": TOOL_INLINE,
                    "arguments": {"data_base64": PNG_B64, "expected_size": len(PNG)},
                },
            )
        )
        hash_only = _tool_payload(
            await _rpc(
                client,
                state,
                "tools/call",
                {
                    "name": TOOL_INLINE,
                    "arguments": {"data_base64": PNG_B64, "expected_sha256": PNG_SHA256},
                },
            )
        )

    assert size_only["status"] == "success"
    assert hash_only["status"] == "success"


@pytest.mark.asyncio
async def test_chunked_mcp_body_limit_returns_json_rpc_error(monkeypatch, probe):
    import cognita.asset_probe as module

    app, state, _ = probe
    monkeypatch.setattr(module, "MAX_MCP_BODY_BYTES", 32)

    class ChunkedBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'{"jsonrpc":"2.0",'
            yield b'"id":1,"method":"tools/list"}'

    async with await _client(app) as client:
        response = await client.post(
            "/mcp/KEI",
            content=ChunkedBody(),
            headers={"content-type": "application/json"},
        )

    assert response.status_code == 200
    assert response.json()["error"]["code"] == -32600
    assert "limit" in response.json()["error"]["message"]


@pytest.mark.asyncio
async def test_direct_put_reports_validation_failures_and_consumes_ticket(probe):
    app, state, _ = probe
    async with await _client(app) as client:
        prepared = _tool_payload(
            await _rpc(
                client,
                state,
                "tools/call",
                {
                    "name": TOOL_PREPARE,
                    "arguments": {
                        "expected_size": len(PNG) + 1,
                        "expected_sha256": PNG_SHA256,
                    },
                },
            )
        )
        failed = await client.put(
            prepared["upload_url"], content=PNG, headers={"content-type": "image/png"}
        )
        replay = await client.put(
            prepared["upload_url"], content=PNG, headers={"content-type": "image/png"}
        )
        result = _tool_payload(
            await _rpc(
                client,
                state,
                "tools/call",
                {"name": TOOL_RESULT, "arguments": {"attempt_id": prepared["attempt_id"]}},
            )
        )

    assert failed.status_code == 400
    assert failed.json()["status"] == "error"
    assert failed.json()["reason"] == "size_mismatch"
    assert replay.status_code == 409
    assert result["failure_reason"] == "size_mismatch"


@pytest.mark.asyncio
async def test_direct_put_rejects_declared_oversize_before_staging(monkeypatch, tmp_path: Path):
    import cognita.asset_probe as module

    state = ProbeState(root_capability="limit-capability", temp_dir=tmp_path / "staging")
    token, result, _ = state.create_upload(
        expected_size=len(PNG),
        expected_sha256=PNG_SHA256,
        media_type="image/png",
        prompt={"prompt_present": False, "prompt_length": 0, "prompt_sha256": None},
    )
    monkeypatch.setattr(module, "MAX_PNG_BYTES", 2)
    app = create_probe_app(state)
    async with await _client(app) as client:
        response = await client.put(
            f"/probe/{state.root_capability}/upload/{token}",
            content=PNG,
            headers={"content-type": "image/png"},
        )

    assert response.status_code == 400
    assert response.json()["reason"] == "byte_limit"
    assert state.get_result(result.attempt_id).failure_reason == "byte_limit"
    assert list((tmp_path / "staging").glob("*")) == []
    state.close()


@pytest.mark.asyncio
async def test_http_disconnect_consumes_ticket_and_removes_partial_file(tmp_path: Path):
    state = ProbeState(temp_dir=tmp_path / "staging")
    token, result, _ = state.create_upload(
        expected_size=len(PNG),
        expected_sha256=PNG_SHA256,
        media_type="image/png",
        prompt={"prompt_present": False, "prompt_length": 0, "prompt_sha256": None},
    )
    events = iter(
        (
            {"type": "http.request", "body": PNG[:4], "more_body": True},
            {"type": "http.disconnect"},
        )
    )

    async def receive():
        return next(events)

    scope = {
        "type": "http",
        "method": "PUT",
        "path": "/upload",
        "headers": [(b"content-type", b"image/png")],
        "query_string": b"",
        "server": ("probe", 80),
        "client": ("client", 1),
        "scheme": "http",
        "http_version": "1.1",
    }
    with pytest.raises(ClientDisconnect):
        await state.receive_direct(Request(scope, receive), token)
    assert list((tmp_path / "staging").glob("*")) == []
    assert state.get_result(result.attempt_id).failure_reason == "client_disconnected"
    with pytest.raises(ProbeInputError) as replayed:
        await state.receive_direct(Request(scope, receive), token)
    assert replayed.value.reason == "ticket_replayed"
    state.close()


def test_result_eviction_also_removes_its_upload_ticket(monkeypatch, tmp_path: Path):
    import cognita.asset_probe as module

    monkeypatch.setattr(module, "MAX_RESULTS", 1)
    state = ProbeState(temp_dir=tmp_path / "staging")
    first_token, first_result, _ = state.create_upload(
        expected_size=len(PNG),
        expected_sha256=PNG_SHA256,
        media_type="image/png",
        prompt={"prompt_present": False, "prompt_length": 0, "prompt_sha256": None},
    )
    state.create_upload(
        expected_size=len(PNG),
        expected_sha256=PNG_SHA256,
        media_type="image/png",
        prompt={"prompt_present": False, "prompt_length": 0, "prompt_sha256": None},
    )

    assert first_result.attempt_id not in state._results
    assert hashlib.sha256(first_token.encode("ascii")).digest() not in state._tickets
    state.close()


def test_failed_unlink_remains_tracked_for_shutdown_retry(monkeypatch, tmp_path: Path):
    state = ProbeState(temp_dir=tmp_path / "staging")
    fd, path = state._stage_path()
    Path(path).write_bytes(b"payload")
    Path(path).chmod(0o600)
    import os

    os.close(fd)
    real_unlink = Path.unlink
    calls = 0

    def flaky_unlink(self, *args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise PermissionError("transient")
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", flaky_unlink)
    assert state._finish_path(path) is False
    assert path in state._active_paths
    state.close()
    assert not path.exists()


def test_public_origin_must_be_https_and_credential_free():
    with pytest.raises(ValueError, match="HTTPS"):
        create_probe_app(public_origin="http://probe.example")
    with pytest.raises(ValueError, match="credential-free"):
        create_probe_app(public_origin="https://user:secret@probe.example")
    with pytest.raises(ValueError, match="query"):
        create_probe_app(public_origin="https://probe.example/path?token=secret")


def test_probe_cli_rejects_non_loopback_bind_before_opening_socket():
    from argparse import Namespace

    from cognita.__main__ import cmd_probe_assets

    with pytest.raises(SystemExit, match="loopback"):
        cmd_probe_assets(Namespace(host="0.0.0.0", port=0, public_base_url=None))
