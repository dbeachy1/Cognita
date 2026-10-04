from __future__ import annotations

import asyncio
import hashlib
import logging
from pathlib import Path
from uuid import uuid4

import httpx
import pytest

from cognita.runtime_broker.app import create_app
from cognita.runtime_broker.journal import RequestJournal, request_digest
from cognita.runtime_broker.protocol import (
    ErrorCode,
    RpcRequest,
    RpcSuccess,
    TransferFile,
    TransferManifest,
)
from cognita.runtime_broker.sdk_adapter import PINNED_SDK_VERSION
from cognita.runtime_broker.service import BrokerService
from cognita.runtime_broker.sdk_adapter import SdkOperationError
from cognita.runtime_broker.transfers import MAX_RETAINED_TRANSFERS, TransferStore

SECRET = "s" * 32


class FakeAdapter:
    def __init__(self):
        self.calls = []

    async def readiness_probe(self):
        return {"status": "ok", "stage": "complete"}

    async def ensure(self, workspace_id, arguments):
        return await self.execute(workspace_id, "ensure", arguments)

    async def inspect(self, workspace_id):
        return {"state": "running"}

    async def start(self, workspace_id):
        return {"state": "running"}

    async def stop(self, workspace_id, *, force=False):
        return {"state": "stopped"}

    async def remove(self, workspace_id):
        return {"state": "absent"}

    async def execute(self, workspace_id, operation, arguments):
        runtime_name = f"cognita-ws-{workspace_id}"
        self.calls.append((runtime_name, operation, arguments))
        return {"accepted": True}


class FileAdapter(FakeAdapter):
    def __init__(self):
        super().__init__()
        self.files: dict[tuple[str, str], bytes] = {}

    async def copy_from_host(self, workspace_id, host_path, guest_path):
        self.files[(f"cognita-ws-{workspace_id}", guest_path)] = Path(host_path).read_bytes()

    async def copy_to_host(self, workspace_id, guest_path, host_path):
        Path(host_path).write_bytes(
            self.files[(f"cognita-ws-{workspace_id}", guest_path)]
        )


async def call(app, method, url, **kwargs):
    if not app.state.broker_service.runtime_ready:
        await app.state.broker_service.startup()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://broker") as client:
        return await client.request(method, url, **kwargs)


def test_pinned_sdk_version_is_the_only_component_contract():
    assert PINNED_SDK_VERSION == "0.7.0"


@pytest.mark.asyncio
async def test_internal_bearer_is_required_and_never_echoed():
    app = create_app(secret=SECRET, adapter=FakeAdapter(), startup_probe=True)
    response = await call(app, "POST", "/v1/rpc", content=b"{}")
    assert response.status_code == 401
    assert SECRET not in response.text


@pytest.mark.asyncio
async def test_rpc_is_strict_and_mutating_retry_is_replayed():
    adapter = FakeAdapter()
    app = create_app(secret=SECRET, adapter=adapter, startup_probe=True)
    headers = {"Authorization": f"Bearer {SECRET}"}
    request_id = str(uuid4())
    body = {
        "request_id": request_id,
        "operation": "ensure",
        "workspace_id": str(uuid4()),
        "expected_runtime_generation": 0,
        "arguments": {},
    }
    first = await call(app, "POST", "/v1/rpc", headers=headers, json=body)
    second = await call(app, "POST", "/v1/rpc", headers=headers, json=body)
    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()
    assert len(adapter.calls) == 1
    assert adapter.calls[0][0].startswith("cognita-ws-")

    invalid = dict(body, extra="not allowed")
    response = await call(app, "POST", "/v1/rpc", headers=headers, json=invalid)
    assert response.json()["code"] == ErrorCode.INVALID_REQUEST


@pytest.mark.asyncio
async def test_generation_conflict_is_bounded_and_retryable():
    app = create_app(secret=SECRET, adapter=FakeAdapter(), startup_probe=True)
    body = {
        "request_id": str(uuid4()),
        "operation": "inspect",
        "workspace_id": str(uuid4()),
        "expected_runtime_generation": 99,
        "arguments": {},
    }
    response = await call(
        app,
        "POST",
        "/v1/rpc",
        headers={"Authorization": f"Bearer {SECRET}"},
        json=body,
    )
    assert response.json() == {
        "request_id": body["request_id"],
        "code": "generation_conflict",
        "retryable": True,
        "message": "runtime generation does not match",
    }
    assert len(response.text) < 400


@pytest.mark.asyncio
async def test_transfer_manifest_frames_commit_and_hash_validation():
    adapter = FileAdapter()
    app = create_app(secret=SECRET, adapter=adapter)
    headers = {"Authorization": f"Bearer {SECRET}"}
    transfer_id, workspace_id = uuid4(), uuid4()
    content = b"hello runtime"
    manifest = {
        "transfer_id": str(transfer_id),
        "workspace_id": str(workspace_id),
        "direction": "to_workspace",
        "files": [
            {
                "path": "src/greeting.txt",
                "size": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
            }
        ],
        "total_bytes": len(content),
    }
    admitted = await call(app, "POST", "/v1/transfers", headers=headers, json=manifest)
    assert admitted.status_code == 200
    frame_headers = dict(
        headers,
        **{
            "X-Transfer-Path": "src/greeting.txt",
            "X-Frame-Offset": "0",
            "X-Frame-Sha256": hashlib.sha256(content).hexdigest(),
        },
    )
    framed = await call(
        app,
        "PUT",
        f"/v1/transfers/{transfer_id}/content",
        headers=frame_headers,
        content=content,
    )
    assert framed.json()["state"] == "ready"
    committed = await call(app, "POST", f"/v1/transfers/{transfer_id}/commit", headers=headers)
    assert committed.json()["state"] == "committed"
    assert adapter.files[(f"cognita-ws-{workspace_id}", "/workspace/src/greeting.txt")] == content
    received = await call(
        app,
        "GET",
        f"/v1/transfers/{transfer_id}/content",
        headers=headers,
        params={"path": "src/greeting.txt"},
    )
    assert received.content == content


@pytest.mark.asyncio
async def test_from_workspace_transfer_is_staged_and_hash_verified():
    adapter = FileAdapter()
    workspace_id, transfer_id = uuid4(), uuid4()
    runtime_name = f"cognita-ws-{workspace_id}"
    content = b"workspace output\x00"
    adapter.files[(runtime_name, "/workspace/out/result.bin")] = content
    app = create_app(secret=SECRET, adapter=adapter)
    headers = {"Authorization": f"Bearer {SECRET}"}
    manifest = {
        "transfer_id": str(transfer_id),
        "workspace_id": str(workspace_id),
        "direction": "from_workspace",
        "files": [{
            "path": "out/result.bin",
            "size": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
        }],
        "total_bytes": len(content),
    }

    admitted = await call(app, "POST", "/v1/transfers", headers=headers, json=manifest)
    assert admitted.json()["state"] == "ready"
    received = await call(
        app,
        "GET",
        f"/v1/transfers/{transfer_id}/content",
        headers=headers,
        params={"path": "out/result.bin"},
    )
    assert received.content == content
    committed = await call(
        app, "POST", f"/v1/transfers/{transfer_id}/commit", headers=headers
    )
    assert committed.json()["state"] == "committed"


def test_journal_digest_distinguishes_arguments():
    common = {"operation": "ensure", "workspace_id": str(uuid4()), "arguments": {}}
    assert request_digest(common) != request_digest({**common, "arguments": {"network": "off"}})


@pytest.mark.asyncio
async def test_concurrent_duplicate_mutation_executes_once():
    class SlowAdapter(FakeAdapter):
        async def execute(self, workspace_id, operation, arguments):
            await asyncio.sleep(0)
            return await super().execute(workspace_id, operation, arguments)

    adapter = SlowAdapter()
    service = BrokerService(adapter)
    await service.startup()
    request = RpcRequest.model_validate(
        {
            "request_id": str(uuid4()),
            "operation": "ensure",
            "workspace_id": str(uuid4()),
            "expected_runtime_generation": 0,
            "arguments": {},
        }
    )
    first, second = await asyncio.gather(service.handle(request), service.handle(request))
    assert isinstance(first, RpcSuccess)
    assert second.code == ErrorCode.RUNTIME_FAILURE
    assert second.retryable is True
    assert len(adapter.calls) == 1


def test_pending_mutation_is_not_replayed_after_uncertain_restart():
    adapter = FakeAdapter()
    journal = RequestJournal()
    service = BrokerService(adapter, journal)
    asyncio.run(service.startup())
    request = RpcRequest.model_validate(
        {
            "request_id": str(uuid4()),
            "operation": "ensure",
            "workspace_id": str(uuid4()),
            "expected_runtime_generation": 0,
            "arguments": {},
        }
    )
    wire = request.model_dump(mode="json")
    journal.reserve(request.request_id, request_digest(wire))
    result = asyncio.run(service.handle(request))
    assert result.code == ErrorCode.RUNTIME_FAILURE
    assert result.retryable is True
    assert adapter.calls == []


@pytest.mark.asyncio
async def test_cancel_failure_logs_safe_broker_context_without_guest_output(caplog):
    job_id = uuid4()

    class CancelFailureAdapter(FakeAdapter):
        async def execute(self, workspace_id, operation, arguments):
            if operation == "job_start":
                return {"job_id": str(job_id), "state": "running"}
            if operation == "job_cancel":
                raise SdkOperationError(
                    "runtime_failure", stage="job_cancel",
                    evidence={"exit_status": 1, "guest_state": "canceled", "stdout": "secret guest output"},
                )
            return await super().execute(workspace_id, operation, arguments)

    adapter = CancelFailureAdapter()
    service = BrokerService(adapter)
    await service.startup()
    workspace_id = uuid4()
    start = RpcRequest.model_validate({
        "request_id": str(uuid4()), "operation": "job_start", "workspace_id": str(workspace_id),
        "expected_runtime_generation": service.runtime_generation,
            "arguments": {"argv": ["python3"], "cwd": "/workspace", "timeout_seconds": 30, "env": {}, "async": True},
    })
    started = await service.handle(start)
    assert isinstance(started, RpcSuccess)
    with caplog.at_level(logging.WARNING, logger="cognita.runtime_broker.service"):
        cancel = RpcRequest.model_validate({
            "request_id": str(uuid4()), "operation": "job_cancel", "workspace_id": str(workspace_id),
            "expected_runtime_generation": service.runtime_generation,
            "arguments": {"job_id": str(job_id)},
        })
        result = await service.handle(cancel)
    assert result.code == ErrorCode.RUNTIME_FAILURE
    record = next(item for item in caplog.records if item.event == "workspace_job_cancel_failure")
    assert record.workspace_id == str(workspace_id)
    assert record.job_id == str(job_id)
    assert record.stage == "job_cancel"
    assert record.category == "runtime_failure"
    assert record.exit_status == 1
    assert record.guest_state == "canceled"
    assert "secret guest output" not in caplog.text


def test_transfer_paths_reject_controls_and_oversized_components():
    digest = "0" * 64
    with pytest.raises(ValueError):
        TransferFile(path="bad\nname", size=0, sha256=digest)
    with pytest.raises(ValueError):
        TransferFile(path="a" * 256, size=0, sha256=digest)


@pytest.mark.asyncio
async def test_zero_byte_transfer_content_and_finalized_retention_are_bounded():
    store = TransferStore()
    workspace_id = uuid4()
    digest = hashlib.sha256(b"").hexdigest()

    def manifest():
        return TransferManifest(
            transfer_id=uuid4(),
            workspace_id=workspace_id,
            direction="to_workspace",
            files=[TransferFile(path="empty.txt", size=0, sha256=digest)],
            total_bytes=0,
        )

    first = manifest()
    await store.admit(first)
    assert store.content(first.transfer_id, "empty.txt") == b""
    assert (await store.commit(first.transfer_id)).state == "committed"

    for _ in range(MAX_RETAINED_TRANSFERS):
        item = manifest()
        await store.admit(item)
        await store.commit(item.transfer_id)

    with pytest.raises(KeyError):
        store.state(first.transfer_id)
    store.close()


def test_mounted_bearer_secret_is_loaded(monkeypatch, tmp_path):
    secret_path = tmp_path / "broker.secret"
    secret_path.write_text(SECRET + "\n", encoding="ascii")
    monkeypatch.setenv("COGNITA_INTERNAL_BEARER_FILE", str(secret_path))
    app = create_app(adapter=FakeAdapter())
    assert app is not None
