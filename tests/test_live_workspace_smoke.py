"""Safety-unit coverage for the connector-free live Workspace harness.

These tests do not stand in for the real broker/SDK run.  The harness itself
is intentionally a real HTTP client; this file only proves that its refusal
guards and cleanup sequencing cannot accidentally broaden the target.
"""

from __future__ import annotations

import asyncio
import json
import uuid

import pytest

from scripts.live_workspace_smoke import (
    HarnessError,
    RunState,
    _cleanup,
    _run_root,
    _workspace_route,
)
from scripts.live_broker_workspace_smoke import (
    BrokerSmokeError,
    EXPECTED_IMAGE,
    MEMORY_MIB,
    PERSISTENT_FIXTURE_UUID,
    ROOT_QUOTA_BYTES,
    _read_persisted_metadata,
    _validate_persisted_metadata,
    _require_first_create,
    parse_workspace_id,
    resource_names,
    run_root,
)
from cognita.runtime_broker.protocol import BrokerOperation
from cognita.runtime_broker.validation import normalize_rpc_arguments


@pytest.mark.parametrize(
    "url, expected",
    [
        ("http://127.0.0.1/mcp/workspace/self-test/mcp", True),
        ("https://beta.example/mcp/workspace/self-test/mcp/v3", True),
        ("https://beta.example/mcp/connectors/combined/mcp/v5", False),
        ("https://beta.example/mcp/workspace/self-test/mcp/v1", False),
        ("https://beta.example/mcp/workspace/self-test/mcp?project=Self-Test", False),
        ("/mcp/workspace/self-test/mcp", False),
    ],
)
def test_workspace_route_is_direct_and_current(url: str, expected: bool):
    assert _workspace_route(url) is expected


def test_direct_broker_fixture_identity_is_stable_and_uuid_bound():
    workspace_id = parse_workspace_id("12345678-1234-4234-8234-123456789abc")
    assert resource_names(workspace_id) == (
        "cognita-ws-12345678-1234-4234-8234-123456789abc",
        "cognita-ws-data-12345678-1234-4234-8234-123456789abc",
    )
    assert run_root(workspace_id).endswith("/12345678-1234-4234-8234-123456789abc")
    with pytest.raises(BrokerSmokeError):
        parse_workspace_id("12345678-1234-1234-8234-123456789abc")
    assert str(parse_workspace_id(PERSISTENT_FIXTURE_UUID)) == PERSISTENT_FIXTURE_UUID


def _persisted_sdk_config(workspace_id: str, sandbox: str, volume: str) -> dict:
    return {
        "name": sandbox,
        "labels": {
            "cognita.owner": "workspace-broker",
            "cognita.workspace": workspace_id,
            "cognita.image": EXPECTED_IMAGE,
            "cognita.volume": volume,
            "cognita.network": "off",
            "cognita.root_quota_bytes": str(ROOT_QUOTA_BYTES),
        },
        "resources": {"cpus": 4, "memory_mib": MEMORY_MIB},
        "image": {"Oci": {"reference": EXPECTED_IMAGE, "root_disk": {}}},
        "mounts": [{"guest": "/workspace", "name": volume, "type": "named", "options": {}}],
        "network": {"mode": "off", "rules": []},
    }


def test_direct_broker_fixture_reuses_only_matching_persisted_sdk_metadata():
    workspace_id = parse_workspace_id(PERSISTENT_FIXTURE_UUID)
    sandbox, volume = resource_names(workspace_id)
    _validate_persisted_metadata(
        _persisted_sdk_config(str(workspace_id), sandbox, volume),
        {"cognita.owner": "workspace-broker", "cognita.workspace": str(workspace_id)},
        4096,
        workspace_id,
        sandbox,
        volume,
    )
    assert _require_first_create(
        {"state": "running", "sandbox_name": sandbox, "volume_name": volume},
        sandbox,
        volume,
    )
    assert _require_first_create(
        {"state": "failed", "sandbox_name": sandbox, "volume_name": volume},
        sandbox,
        volume,
    )
    assert _require_first_create(
        {"state": "partial", "partial_state": "volume_only", "sandbox_name": sandbox, "volume_name": volume},
        sandbox,
        volume,
    )
    assert not _require_first_create(
        {"state": "absent", "sandbox_name": sandbox, "volume_name": volume},
        sandbox,
        volume,
    )


def test_direct_broker_fixture_rejects_label_less_or_mismatched_persisted_metadata():
    workspace_id = parse_workspace_id(PERSISTENT_FIXTURE_UUID)
    sandbox, volume = resource_names(workspace_id)
    config = _persisted_sdk_config(str(workspace_id), sandbox, volume)
    config["labels"].pop("cognita.owner")
    with pytest.raises(BrokerSmokeError, match="owner/config labels do not match"):
        _validate_persisted_metadata(config, {}, 4096, workspace_id, sandbox, volume)

    config = _persisted_sdk_config(str(workspace_id), sandbox, volume)
    config["labels"] = None
    with pytest.raises(BrokerSmokeError, match="omitted persisted owner labels"):
        _validate_persisted_metadata(config, {}, 4096, workspace_id, sandbox, volume)

    config = _persisted_sdk_config(str(workspace_id), sandbox, volume)
    config["mounts"][0]["name"] = "other-task-volume"
    with pytest.raises(BrokerSmokeError, match="mount or name"):
        _validate_persisted_metadata(
            config,
            {"cognita.owner": "workspace-broker", "cognita.workspace": str(workspace_id)},
            4096,
            workspace_id,
            sandbox,
            volume,
        )


def test_direct_broker_fixture_reads_official_sdk_config_json_shape(monkeypatch):
    workspace_id = parse_workspace_id(PERSISTENT_FIXTURE_UUID)
    sandbox, volume = resource_names(workspace_id)
    config = _persisted_sdk_config(str(workspace_id), sandbox, volume)

    class FakeSandbox:
        config_json = json.dumps(config)

        @classmethod
        async def get(cls, name):
            assert name == sandbox
            return cls()

    class FakeVolume:
        labels = {"cognita.owner": "workspace-broker", "cognita.workspace": str(workspace_id)}
        quota_mib = 4096

        @classmethod
        async def get(cls, name):
            assert name == volume
            return cls()

    monkeypatch.setattr(
        "cognita.runtime_broker.sdk_adapter_v2._sdk_types",
        lambda: (FakeSandbox, FakeVolume, object),
    )
    parsed, labels, quota_mib = asyncio.run(_read_persisted_metadata(sandbox, volume))
    _validate_persisted_metadata(parsed, labels, quota_mib, workspace_id, sandbox, volume)


def test_direct_broker_smoke_payloads_match_strict_rpc_contract():
    """Keep every live-harness request on the broker's public wire shape."""

    workspace_id = parse_workspace_id(PERSISTENT_FIXTURE_UUID)
    root = run_root(workspace_id)
    content = "cognita direct broker smoke\n"
    payloads = [
        (
            BrokerOperation.ENSURE,
            {
                "quota_bytes": 4 * 1024**3,
                "root_quota_bytes": 4 * 1024**3,
                "vcpus": 4,
                "memory_bytes": 8 * 1024**3,
                "max_processes": 256,
                "max_file_descriptors": 4096,
                "require_writable_root_quota": True,
                "network": {"mode": "off", "rules": [], "explicit_confirmation": False},
            },
        ),
        (
            BrokerOperation.FS_WRITE,
            {"path": f"{root}/probe.txt", "text": content, "create_parents": True},
        ),
        (
            BrokerOperation.FS_READ,
            {"path": f"{root}/probe.txt", "offset": 0, "max_bytes": 1024, "binary": False},
        ),
        (
            BrokerOperation.JOB_START,
            {
                "argv": ["python3", "-c", "print('cognita-direct-broker-ok')"],
                "cwd": root,
                "timeout_seconds": 60,
                "env": {},
                "async": True,
            },
        ),
        (
            BrokerOperation.JOB_GET,
            {"job_id": str(uuid.uuid4()), "stdout_offset": 0, "stderr_offset": 0, "max_bytes": 1024},
        ),
    ]
    normalized = [normalize_rpc_arguments(operation, arguments) for operation, arguments in payloads]
    assert normalized[1]["content_encoding"] == "text"
    assert "content_encoding" not in payloads[1][1]
    assert normalized[2]["binary"] is False
    assert normalized[3]["async"] is True


def test_run_root_is_bounded_and_under_workspace_selftest():
    assert _run_root("live-abc123") == "/workspace/.cognita-self-test/live-abc123"
    for unsafe in ("../personal", "Personal", "live/test", "live_underscore"):
        with pytest.raises(HarnessError):
            _run_root(unsafe)


def test_cleanup_does_not_admit_a_workspace_before_first_mutation():
    class UnexpectedMcpCall:
        def call(self, *_args, **_kwargs):
            raise AssertionError("cleanup must not call MCP without an owned Workspace")

    class UnexpectedAdminCall:
        def diagnostics(self, *_args, **_kwargs):
            raise AssertionError("cleanup must not inspect Admin without an owned Workspace")

    state = RunState(root="/workspace/.cognita-self-test/live-abc123")
    _cleanup(UnexpectedMcpCall(), UnexpectedAdminCall(), state)
    assert state.cleanup_error is None


def test_cleanup_requires_exact_admin_absence_proof():
    calls: list[tuple[str, object]] = []

    class M:
        def call(self, name, args):
            calls.append((name, args))
            if name == "workspace_get_job":
                return {"status": "success", "job": {"state": "succeeded"}}
            return {"status": "success"}

    class A:
        def diagnostics(self, workspace_id):
            calls.append(("diagnostics", workspace_id))
            if len([item for item in calls if item[0] == "diagnostics"]) == 1:
                return 200, {"workspace": {"revision": 7}}
            return 200, {"workspace": {"revision": 8}}

        def remove(self, workspace_id, revision, token):
            calls.append(("remove", (workspace_id, revision, token)))
            return {"status": "success"}

    state = RunState(root="/workspace/.cognita-self-test/live-abc123", workspace_id="ws-owned")
    _cleanup(M(), A(), state)
    assert state.cleanup_error == "HarnessError"
    assert any(name == "remove" for name, _ in calls)


def test_cleanup_proves_exact_workspace_absence_before_success():
    calls: list[tuple[str, object]] = []

    class M:
        def call(self, name, args):
            calls.append((name, args))
            return {"status": "success", "job": {"state": "succeeded"}}

    class A:
        def diagnostics(self, workspace_id):
            calls.append(("diagnostics", workspace_id))
            if len([item for item in calls if item[0] == "diagnostics"]) == 1:
                return 200, {"workspace": {"revision": 7}}
            return 404, {}

        def remove(self, workspace_id, revision, token):
            calls.append(("remove", (workspace_id, revision, token)))
            return {"status": "success"}

    state = RunState(root="/workspace/.cognita-self-test/live-abc123", workspace_id="ws-owned")
    _cleanup(M(), A(), state)
    assert state.cleanup_error is None
    assert any(name == "remove" for name, _ in calls)
