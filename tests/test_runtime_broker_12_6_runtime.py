from __future__ import annotations

import logging
import sqlite3
import subprocess
import sys
import time
import types
from uuid import uuid4

import httpx
import pytest

from cognita.runtime_broker.app import create_app
from cognita.runtime_broker.network import NETWORK_SCHEMES, NetworkPolicy
from cognita.runtime_broker.protocol import BrokerOperation, RpcFailure, RpcRequest
from cognita.runtime_broker.sdk_adapter_v2 import (
    MicrosandboxSdkAdapter,
    SdkOperationError,
    _SdkNetworkTypes,
    _BOUNDED_LIST_HELPER,
    operation_context,
)
from cognita.runtime_broker.service import BrokerService
from cognita.runtime_broker.state import RuntimeStateIncompatible, RuntimeStateStore
from cognita.runtime_broker.validation import ResourcePolicy, normalize_rpc_arguments


class _ChunkedReadFs:
    def __init__(self, chunks: list[bytes]):
        self.chunks = chunks
        self.read_called = False

    async def read(self, _path):
        self.read_called = True
        raise AssertionError("bounded operations must not call full-file read")

    def read_stream(self, _path):
        async def stream():
            for chunk in self.chunks:
                yield chunk
        return stream()

    async def list(self, *_args, **_kwargs):
        raise AssertionError("bounded enumeration must not request a full SDK listing")


class _BoundedListHandle:
    def __init__(self, output: bytes):
        self.output = output
        self.argv: list[str] | None = None
        self.fs = _ChunkedReadFs([])

    async def exec(self, _executable, argv, **_kwargs):
        self.argv = argv
        return types.SimpleNamespace(stdout=self.output, stderr=b"", exit_code=0)


def test_ensure_wire_defaults_carry_the_12_6_resource_contract():
    result = normalize_rpc_arguments(BrokerOperation.ENSURE, {}, ResourcePolicy())
    assert result["vcpus"] == 4
    assert result["memory_bytes"] == 8 * 1024**3
    assert result["quota_bytes"] == 4 * 1024**3
    assert result["root_quota_bytes"] == 4 * 1024**3
    assert result["require_writable_root_quota"] is True


@pytest.mark.asyncio
async def test_fs_read_streams_only_requested_range_without_full_materialization():
    adapter = object.__new__(MicrosandboxSdkAdapter)
    adapter.timeout_seconds = 5
    fs = _ChunkedReadFs([b"0123", b"4567", b"89abcdef"])
    raw = await adapter._read_range(fs, "/workspace/large.bin", 3, 5, adapter._deadline(5))
    assert raw == b"34567"
    assert fs.read_called is False


@pytest.mark.asyncio
async def test_fs_search_text_streams_across_chunk_boundaries():
    adapter = object.__new__(MicrosandboxSdkAdapter)
    adapter.timeout_seconds = 5
    fs = _ChunkedReadFs([b"prefix ma", b"tched suffix"])
    assert await adapter._contains_streamed(
        fs, "/workspace/file.txt", b"matched", adapter._deadline(5),
    )
    assert fs.read_called is False


@pytest.mark.asyncio
async def test_one_byte_stream_search_retains_no_chunk_tail(monkeypatch):
    adapter = object.__new__(MicrosandboxSdkAdapter)
    adapter.timeout_seconds = 5
    fs = _ChunkedReadFs([b"a" * 4096, b"b" * 4096])
    tail_lengths: list[int] = []
    original = adapter._stream_tail

    def track_tail(data, needle):
        tail = original(data, needle)
        tail_lengths.append(len(tail))
        return tail

    monkeypatch.setattr(adapter, "_stream_tail", track_tail)
    assert not await adapter._contains_streamed(
        fs, "/workspace/file.txt", b"z", adapter._deadline(5),
    )
    assert tail_lengths == [0, 0]


def test_bounded_guest_helper_reserves_an_unambiguous_truncation_marker(tmp_path):
    for index in range(8):
        (tmp_path / f"entry-{index}-with-padding").write_bytes(b"x")
    completed = subprocess.run(
        [
            sys.executable, "-c", _BOUNDED_LIST_HELPER, str(tmp_path), "0", "20", "200",
        ],
        capture_output=True,
        timeout=5,
        check=True,
    )
    assert b'{"_truncated":true}' in completed.stdout


@pytest.mark.asyncio
async def test_fs_list_uses_bounded_guest_metadata_enumerator():
    output = b"".join(
        f'{{"path":"/workspace/{name}","kind":"file","size":1}}\n'.encode()
        for name in ("a", "b", "c")
    )
    handle = _BoundedListHandle(output)
    adapter = object.__new__(MicrosandboxSdkAdapter)
    adapter.timeout_seconds = 5
    entries, truncated = await adapter._bounded_guest_listing(
        handle, "/workspace", False, 2, adapter._deadline(5),
    )
    assert [entry["path"] for entry in entries] == ["/workspace/a", "/workspace/b"]
    assert truncated is True
    assert handle.argv is not None
    assert handle.argv[-2] == "3"


@pytest.mark.asyncio
async def test_fs_search_does_not_materialize_sdk_listing_over_path_cap(monkeypatch):
    output = b"".join(
        f'{{"path":"/workspace/{name}","kind":"file","size":1}}\n'.encode()
        for name in ("a", "b", "c")
    )
    handle = _BoundedListHandle(output)
    adapter = object.__new__(MicrosandboxSdkAdapter)
    adapter.timeout_seconds = 5
    monkeypatch.setattr(adapter, "_connected", lambda *_args, **_kwargs: _ready(handle))
    result = await adapter._filesystem(
        uuid4(), "fs_search", {
            "roots": ["/workspace"], "mode": "glob", "pattern": "*.txt",
            "max_paths": 2, "max_matches": 10, "timeout_seconds": 5,
        },
    )
    assert result == {"matches": [], "truncated": True}


@pytest.mark.asyncio
async def test_fs_write_uses_sdk_070_componentwise_mkdir_without_parents_keyword(monkeypatch):
    class StrictMkdirFs:
        def __init__(self):
            self.directories = []
            self.files = {}
            self.existing = set()
            self.exists_calls = []

        async def exists(self, path):
            self.exists_calls.append(path)
            return path in self.existing or path in self.files

        async def stat(self, path):
            if path in self.files:
                return types.SimpleNamespace(kind="file")
            if path in self.existing:
                return types.SimpleNamespace(kind="directory")
            raise NotFoundError(path)

        async def mkdir(self, path):
            self.directories.append(path)
            self.existing.add(path)

        async def write(self, path, data):
            self.files[path] = data

    fs = StrictMkdirFs()
    owner_calls = []

    async def guest_exec(executable, argv, **kwargs):
        owner_calls.append((executable, argv, kwargs))
        return types.SimpleNamespace(exit_code=0)

    handle = types.SimpleNamespace(fs=fs, exec=guest_exec)
    adapter = object.__new__(MicrosandboxSdkAdapter)
    adapter.timeout_seconds = 5

    async def connected(*_args, **_kwargs):
        return handle

    monkeypatch.setattr(adapter, "_connected", connected)
    path = "/workspace/.cognita-live/12345678-1234-4234-8234-123456789abc/probe.txt"
    result = await adapter._filesystem(
        uuid4(), "fs_write", {
            "path": path,
            "text": "cognita isolated smoke\n",
            "content_encoding": "text",
            "create_parents": True,
        },
    )

    assert fs.directories == [
        "/workspace",
        "/workspace/.cognita-live",
        "/workspace/.cognita-live/12345678-1234-4234-8234-123456789abc",
    ]
    assert fs.exists_calls == fs.directories
    assert fs.files[path] == b"cognita isolated smoke\n"
    assert result["bytes"] == len(fs.files[path])
    assert [call[1][-1] for call in owner_calls] == [*fs.directories, path]
    assert all(call[2]["user"] == "root" for call in owner_calls)


@pytest.mark.asyncio
async def test_fs_write_reuses_existing_sdk_070_parent_directories(monkeypatch):
    class ExistingDirectoryFs:
        def __init__(self):
            self.directories = {
                "/workspace",
                "/workspace/.cognita-live",
                "/workspace/.cognita-live/12345678-1234-4234-8234-123456789abc",
            }
            self.files = {}
            self.mkdir_calls = []
            self.exists_calls = []

        async def exists(self, path):
            self.exists_calls.append(path)
            return path in self.directories or path in self.files

        async def stat(self, path):
            if path in self.files:
                return types.SimpleNamespace(kind="file")
            if path in self.directories:
                return types.SimpleNamespace(kind="directory")
            raise NotFoundError(path)

        async def mkdir(self, path):
            self.mkdir_calls.append(path)

        async def write(self, path, data):
            self.files[path] = data

    fs = ExistingDirectoryFs()
    adapter = object.__new__(MicrosandboxSdkAdapter)
    adapter.timeout_seconds = 5
    owner_calls = []

    async def guest_exec(executable, argv, **kwargs):
        owner_calls.append((executable, argv, kwargs))
        return types.SimpleNamespace(exit_code=0)

    async def connected(*_args, **_kwargs):
        return types.SimpleNamespace(fs=fs, exec=guest_exec)

    monkeypatch.setattr(adapter, "_connected", connected)
    path = "/workspace/.cognita-live/12345678-1234-4234-8234-123456789abc/probe.txt"
    await adapter._filesystem(
        uuid4(), "fs_write", {
            "path": path,
            "text": "cognita isolated smoke\n",
            "content_encoding": "text",
            "create_parents": True,
        },
    )

    assert fs.mkdir_calls == []
    assert fs.exists_calls == [
        "/workspace",
        "/workspace/.cognita-live",
        "/workspace/.cognita-live/12345678-1234-4234-8234-123456789abc",
    ]
    assert fs.files[path] == b"cognita isolated smoke\n"
    assert [call[1][-1] for call in owner_calls] == [path]


class NotFoundError(Exception):
    pass


@pytest.mark.asyncio
async def test_sdk_failure_log_includes_class_without_exception_text(caplog):
    adapter = object.__new__(MicrosandboxSdkAdapter)
    adapter.timeout_seconds = 5

    async def fail():
        raise NotFoundError("/private/path must not appear")

    with caplog.at_level(logging.WARNING, logger="cognita.runtime_broker.sdk"):
        with pytest.raises(SdkOperationError):
            await adapter._call("fs_exists", fail(), adapter._deadline(5))

    message = caplog.records[-1].getMessage()
    assert "NotFoundError" in message
    assert "/private/path" not in message


@pytest.mark.asyncio
async def test_fs_write_refuses_existing_file_in_parent_chain(monkeypatch):
    class FileCollisionFs:
        def __init__(self):
            self.exists_calls = []

        async def exists(self, path):
            self.exists_calls.append(path)
            return path in {"/workspace", "/workspace/.cognita-live"}

        async def stat(self, path):
            kind = "file" if path == "/workspace/.cognita-live" else "directory"
            return types.SimpleNamespace(kind=kind)

        async def mkdir(self, _path):
            raise AssertionError("mkdir must not replace a file")

        async def write(self, _path, _data):
            raise AssertionError("write must not run after a parent collision")

    adapter = object.__new__(MicrosandboxSdkAdapter)
    adapter.timeout_seconds = 5

    fs = FileCollisionFs()

    async def connected(*_args, **_kwargs):
        return types.SimpleNamespace(fs=fs)

    monkeypatch.setattr(adapter, "_connected", connected)
    with pytest.raises(SdkOperationError) as error:
        await adapter._filesystem(
            uuid4(), "fs_write", {
                "path": "/workspace/.cognita-live/probe.txt",
                "text": "collision",
                "content_encoding": "text",
                "create_parents": True,
            },
        )
    assert error.value.stage == "fs_mkdir"
    assert fs.exists_calls == ["/workspace", "/workspace/.cognita-live"]


async def _ready(handle):
    return handle


def test_network_rules_round_trip_as_structured_objects():
    wire = {
        "mode": "allowlist",
        "rules": [{
            "domain": "Example.com",
            "ports": [8443, 443],
            "protocols": ["https", "http"],
            "suffix": True,
        }],
    }
    policy = NetworkPolicy.from_mapping(wire)
    assert policy.to_mapping() == {
        "mode": "allowlist",
        "rules": [{
            "domain": "example.com",
            "ports": [443, 8443],
            "protocols": ["http", "https"],
            "suffix": True,
        }],
        "explicit_confirmation": False,
    }


def test_network_rule_defaults_to_domain_level_http_and_https():
    policy = NetworkPolicy.from_mapping({
        "mode": "allowlist",
        "rules": [{"domain": "example.com"}],
    })
    assert policy.rules[0].protocols == NETWORK_SCHEMES
    assert policy.rules[0].ports == (80, 443)
    assert policy.rules[0].availability == "available"


def test_legacy_single_scheme_rule_round_trips_unavailable():
    policy = NetworkPolicy.from_mapping({
        "mode": "allowlist",
        "rules": [{"domain": "legacy.example", "protocols": ["https"]}],
    })
    assert policy.rules[0].protocols == ("https",)
    assert policy.rules[0].availability == "unavailable"
    assert policy.rules[0].availability_reason
    assert policy.to_mapping()["rules"][0]["protocols"] == ["https"]


def test_network_modes_fail_closed_without_pinned_sdk_controls():
    class Network:
        @staticmethod
        def none():
            return object()

    with pytest.raises(SdkOperationError) as error:
        MicrosandboxSdkAdapter._network_object(
            Network, {"mode": "allowlist", "rules": [{"domain": "example.com"}]}
        )
    assert error.value.category == "unsupported"
    assert error.value.stage == "network"
    assert error.value.evidence["reason"] == "pinned_sdk_allowlist_control_unavailable"


def _fake_network_controls():
    class Action:
        DENY = "deny"
        ALLOW = "allow"

    class Direction:
        EGRESS = "egress"

    class Protocol:
        TCP = "tcp"
        UDP = "udp"

    class NetworkProfile:
        PUBLIC = "public"

    class DnsConfig:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    class Destination:
        @staticmethod
        def domain(domain):
            return ("domain", domain)

        @staticmethod
        def domain_suffix(domain):
            return ("domain_suffix", domain)

    class Rule:
        @staticmethod
        def allow_dns():
            return (("dns", "udp", 53), ("dns", "tcp", 53))

        @staticmethod
        def allow(**kwargs):
            return ("allow", kwargs)

    class NetworkPolicy:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    class DestGroup:
        HOST = "host"
        PUBLIC = "public"

    return _SdkNetworkTypes(
        action=Action,
        dns_config=DnsConfig,
        destination=Destination,
        dest_group=DestGroup,
        direction=Direction,
        network_policy=NetworkPolicy,
        network_profile=NetworkProfile,
        protocol=Protocol,
        rule=Rule,
    )


def test_network_allowlist_uses_strict_typed_default_deny_policy():
    controls = _fake_network_controls()

    class Network:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    result = MicrosandboxSdkAdapter._network_object(
        Network,
        {
            "mode": "allowlist",
            "rules": [
                {
                    "domain": "example.com",
                    "ports": [443, 8443],
                    "protocols": ["https", "http"],
                    "suffix": False,
                },
                {
                    "domain": "packages.example",
                    "ports": [80],
                    "protocols": ["http", "https"],
                    "suffix": True,
                },
            ],
        },
        controls,
    )

    assert result.strict is True
    assert result.dns.rebind_protection is True
    assert result.policy.default_egress == "deny"
    assert result.policy.default_ingress == "deny"
    assert result.policy.rules[:2] == (("dns", "udp", 53), ("dns", "tcp", 53))
    translated = result.policy.rules[2:]
    assert [item[1]["destination"] for item in translated[:2]] == [
        ("domain", "example.com"),
        ("domain", "example.com"),
    ]
    assert translated[-1][1]["destination"] == ("domain_suffix", "packages.example")
    assert {item[1]["port"] for item in translated} == {80, 443, 8443}
    assert all(item[1]["protocol"] == "tcp" for item in translated)


def test_network_allowlist_rejects_unrepresentable_single_scheme_rules():
    controls = _fake_network_controls()

    class Network:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    with pytest.raises(SdkOperationError) as error:
        MicrosandboxSdkAdapter._network_object(
            Network,
            {
                "mode": "allowlist",
                "rules": [{
                    "domain": "secure.example",
                    "ports": [443],
                    "protocols": ["https"],
                    "suffix": False,
                }],
            },
            controls,
        )

    assert error.value.evidence == {
        "mode": "allowlist",
        "reason": "pinned_sdk_protocol_scheme_control_unavailable",
        "protocols": ["https"],
    }


def test_network_public_uses_only_the_sdk_public_profile():
    controls = _fake_network_controls()
    calls = []

    class Kind:
        value = "group"

    class Destination:
        def __init__(self, value):
            self.kind = Kind()
            self.value = value

    class Rule:
        def __init__(self, destination, protocol=None, port=None):
            self.action = "allow"
            self.direction = "egress"
            self.destination = Destination(destination)
            self.protocol = protocol
            self.port = port

    class PublicPolicy:
        default_egress = "deny"
        default_ingress = "allow"
        rules = (
            Rule("host", "udp", 53),
            Rule("host", "tcp", 53),
            Rule("public"),
        )

    class PublicNetwork:
        policy = PublicPolicy()
        strict = False
        dns = None

        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    class Network:
        @staticmethod
        def from_profiles(*profiles):
            calls.append(profiles)
            return PublicNetwork()

        def __new__(cls, **kwargs):
            return PublicNetwork(**kwargs)

    result = MicrosandboxSdkAdapter._network_object(
        Network,
        {"mode": "unrestricted_public", "rules": [], "explicit_confirmation": True},
        controls,
    )

    assert result.policy.default_egress == "deny"
    assert result.policy.default_ingress == "deny"
    assert result.strict is True
    assert result.dns.rebind_protection is True
    assert {rule.destination.value for rule in result.policy.rules} == {"host", "public"}
    assert all(rule.destination.value not in {"private", "link-local", "metadata"} for rule in result.policy.rules)
    assert calls == [("public",)]


def test_network_public_rejects_non_public_profile_rules():
    controls = _fake_network_controls()

    class Kind:
        value = "group"

    class Destination:
        kind = Kind()
        value = "private"

    class Rule:
        action = "allow"
        direction = "egress"
        destination = Destination()

    class Policy:
        rules = (Rule(),)

    class Network:
        policy = Policy()

        @staticmethod
        def from_profiles(*_profiles):
            return Network()

    with pytest.raises(SdkOperationError) as error:
        MicrosandboxSdkAdapter._network_object(
            Network,
            {"mode": "unrestricted_public", "rules": [], "explicit_confirmation": True},
            controls,
        )

    assert error.value.evidence["reason"] == "pinned_sdk_public_profile_contract_unavailable"
    assert "public_only_destination_rules" in error.value.evidence["missing_controls"]


def test_sdk_error_context_is_bounded_and_correlated():
    request_id = str(uuid4())
    workspace_id = str(uuid4())
    with operation_context(request_id, workspace_id):
        error = SdkOperationError(
            "runtime_failure", retryable=True, stage="sandbox_create",
            evidence={"reason": "owned_failure"}, ownership_verified=True,
        )
    assert error.category == "runtime_failure"
    assert error.stage == "sandbox_create"
    assert error.correlation_id == request_id
    assert error.workspace_id == workspace_id
    assert error.ownership_verified is True
    assert error.evidence == {"reason": "owned_failure"}


def test_runtime_path_evidence_never_fabricates_from_volume_name(tmp_path):
    adapter = object.__new__(MicrosandboxSdkAdapter)
    adapter.workspace_data_root = str(tmp_path)
    volume = type("Volume", (), {"name": "cognita-ws-data-opaque"})()
    evidence = adapter._verified_volume_evidence(volume, uuid4())
    assert evidence["host_path"] is None
    assert evidence["path_status"] == "not_reported"


def _usage_adapter(tmp_path):
    adapter = object.__new__(MicrosandboxSdkAdapter)
    adapter.workspace_data_root = None
    adapter.volume_data_dir = str(tmp_path)
    return adapter


def _backing_volume(tmp_path, name, *, lock=True):
    volumes = tmp_path / "volumes"
    (volumes / ".locks").mkdir(parents=True, exist_ok=True)
    root = volumes / name
    (root / ".cognita" / "deep").mkdir(parents=True)
    (root / "alpha.txt").write_bytes(b"x" * 42)
    (root / ".cognita" / "deep" / "batch.txt").write_bytes(b"y" * 27)
    if lock:
        (volumes / ".locks" / f"{name}.lock").write_bytes(b"")
    return root


@pytest.mark.asyncio
async def test_volume_usage_measures_backing_dir_when_sdk_reports_zero(tmp_path):
    # 13.0.2: the pinned SDK's used_bytes is 0 for a directory volume that
    # holds files (measured on kei). A zero is not a measurement; the backing
    # directory under the SDK data dir is, once verified.
    name = f"cognita-ws-data-{uuid4()}"
    root = _backing_volume(tmp_path, name)
    adapter = _usage_adapter(tmp_path)
    volume = type("Volume", (), {"name": name, "used_bytes": 0})()
    evidence = {"measured_allocated_bytes": None, "usage_status": "unknown"}
    usage = await adapter._volume_usage(volume, name, evidence)
    expected = sum(p.lstat().st_size for p in root.rglob("*"))
    assert usage["measured_apparent_bytes"] == expected
    assert usage["usage_status"] == "measured"
    # A symlink inside the volume is counted by its own size, never followed.
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"z" * 100_000)
    try:
        (root / "escape").symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")
    usage = await adapter._volume_usage(volume, name, evidence)
    assert usage["measured_apparent_bytes"] < expected + 100_000


@pytest.mark.asyncio
async def test_volume_usage_is_unknown_without_a_verified_backing_dir(tmp_path):
    adapter = _usage_adapter(tmp_path)
    evidence = {"measured_allocated_bytes": None, "usage_status": "unknown"}
    # No backing directory at all: never a fabricated 0.
    name = f"cognita-ws-data-{uuid4()}"
    volume = type("Volume", (), {"name": name, "used_bytes": 0})()
    usage = await adapter._volume_usage(volume, name, evidence)
    assert usage == {"measured_allocated_bytes": None, "measured_apparent_bytes": None, "usage_status": "unknown"}
    # A directory without the SDK's lock file is not an SDK-registered volume.
    unlocked = f"cognita-ws-data-{uuid4()}"
    _backing_volume(tmp_path, unlocked, lock=False)
    assert adapter._volume_backing_dir(unlocked) is None
    # A name the adapter does not generate is never looked up.
    assert adapter._volume_backing_dir("../etc") is None
    assert adapter._volume_backing_dir("cognita-ws-data-opaque") is None
    # A positive SDK figure is still trusted as-is.
    volume = type("Volume", (), {"name": name, "used_bytes": 777})()
    usage = await adapter._volume_usage(volume, name, {"measured_allocated_bytes": 1024, "usage_status": "measured"})
    assert usage == {"measured_allocated_bytes": 1024, "measured_apparent_bytes": 777, "usage_status": "measured"}


@pytest.mark.asyncio
async def test_broker_preserves_stage_category_correlation_and_safe_blocker_evidence():
    workspace_id = uuid4()

    class FailingAdapter:
        async def readiness_probe(self):
            return {"status": "ok"}

        async def ensure(self, _workspace_id, _config):
            raise SdkOperationError(
                "unsupported", stage="quota_probe",
                evidence={
                    "requirement": "4GiB writable root ceiling",
                    "reason": "named_volume_quota_covers_workspace_only",
                    "writable_paths": ["/", "/workspace"],
                },
            )

        async def inspect(self, _workspace_id):
            return {"state": "failed"}

        async def start(self, _workspace_id):
            return {"state": "running"}

        async def stop(self, _workspace_id, *, force=False):
            return {"state": "stopped"}

        async def remove(self, _workspace_id):
            return {"state": "absent"}

        async def execute(self, *_args, **_kwargs):
            return {}

    service = BrokerService(FailingAdapter())
    await service.startup()
    request = RpcRequest(
        request_id=uuid4(), operation=BrokerOperation.ENSURE,
        workspace_id=workspace_id, arguments={},
    )
    result = await service.handle(request)
    assert isinstance(result, RpcFailure)
    assert result.category == "unsupported"
    assert result.stage == "quota_probe"
    assert result.correlation_id == request.request_id
    assert result.diagnostics == {
        "requirement": "4GiB writable root ceiling",
        "reason": "named_volume_quota_covers_workspace_only",
        "writable_paths": ["/", "/workspace"],
    }
    record = service.state_store.workspace(workspace_id)
    assert record.state == "failed"
    assert record.last_error_code == "unsupported"


@pytest.mark.asyncio
async def test_broker_startup_leaves_failed_runtime_for_admitted_repair():
    workspace_id = uuid4()

    class RecoveringAdapter:
        def __init__(self):
            self.calls = []

        async def readiness_probe(self):
            return {"status": "ok"}

        async def inspect(self, _workspace_id):
            self.calls.append("inspect")
            return {"state": "failed"}

        async def execute(self, *_args, **_kwargs):
            self.calls.append("execute")
            raise AssertionError("startup reconciliation must not replay jobs")

    adapter = RecoveringAdapter()
    service = BrokerService(adapter)
    service.state_store.ensure_workspace(workspace_id, quota_bytes=4 * 1024**3)
    service.state_store.update_workspace(workspace_id, state="failed", desired_state="running")

    await service.startup()
    await service.wait_for_reconciliation()

    record = service.state_store.workspace(workspace_id)
    assert record.state == "failed"
    assert record.desired_state == "running"
    assert adapter.calls == ["inspect"]


@pytest.mark.asyncio
async def test_12_13_failed_workspace_does_not_block_12_14_broker_health(tmp_path):
    """A persisted 12.13 failed/running row recovers behind 12.14 health."""

    workspace_id = uuid4()
    state_store = RuntimeStateStore(tmp_path / "broker-state.sqlite3")
    # This is the durable state left by 12.12 after a runtime crash: the
    # caller's intent remains running, while the last observed runtime failed.
    state_store.ensure_workspace(workspace_id, quota_bytes=4 * 1024**3)
    state_store.update_workspace(workspace_id, state="failed", desired_state="running")

    class StalledRecoveryAdapter:
        async def readiness_probe(self):
            return {"status": "ok"}

        async def inspect(self, _workspace_id):
            return {"state": "failed"}

        async def execute(self, *_args, **_kwargs):
            raise AssertionError("startup recovery must not replay jobs")

    adapter = StalledRecoveryAdapter()
    app = create_app(
        secret="s" * 32,
        adapter=adapter,
        state_store=state_store,
        startup_probe=False,
    )
    service = app.state.broker_service

    started = time.monotonic()
    await service.startup()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://broker") as client:
        response = await client.get("/healthz")
    elapsed = time.monotonic() - started

    assert elapsed < 1.0
    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert response.json()["runtime"] == "ready"
    assert response.json()["readiness"]["stage"] == "reconcile"
    record = state_store.workspace(workspace_id)
    assert record.state == "failed"
    assert record.desired_state == "running"

    await service.wait_for_reconciliation()
    record = state_store.workspace(workspace_id)
    assert record.state == "failed"
    assert record.desired_state == "running"
    await service.shutdown()
    state_store.close()


def test_volume_creation_allowance_is_one_shot_even_after_absent_observation(tmp_path):
    workspace_id = uuid4()
    store = RuntimeStateStore(tmp_path / "broker-state.sqlite3")
    try:
        store.ensure_workspace(workspace_id, quota_bytes=4 * 1024**3)
        assert store.claim_initial_volume_creation(workspace_id)
        store.update_workspace(workspace_id, state="running")
        store.update_workspace(workspace_id, state="absent")
        assert not store.claim_initial_volume_creation(workspace_id)
    finally:
        store.close()


def test_broker_ensure_requires_explicit_application_first_creation_intent():
    policy = ResourcePolicy()
    assert normalize_rpc_arguments(BrokerOperation.ENSURE, {}, policy)["create_volume_if_absent"] is False
    assert normalize_rpc_arguments(
        BrokerOperation.ENSURE, {"create_volume_if_absent": True}, policy,
    )["create_volume_if_absent"] is True


# Superseded (13.0, DESIGN-13.0 section 8):
# `test_migrated_absent_workspace_cannot_create_replacement_volume` used to
# stand here.  It built a pre-12.6 `workspaces` table without
# `volume_creation_attempted`, opened it, and asserted that the ALTER TABLE
# migration defaulted every migrated row to "already attempted" so recovery
# could not put an empty replacement volume under a row that already owned
# one.  Broker state is disposable in 13.0 and that migration is gone, so the
# case it covered no longer exists; the two tests below cover what replaced it
# -- the refusal, and the one-shot rule on rows this build writes.
def test_legacy_broker_state_layout_is_refused_with_the_reset_command(tmp_path):
    path = tmp_path / "legacy-state.sqlite3"
    workspace_id = uuid4()
    with sqlite3.connect(path) as db:
        db.execute(
            "CREATE TABLE workspaces (workspace_id TEXT PRIMARY KEY, state TEXT NOT NULL, "
            "desired_state TEXT NOT NULL, runtime_generation INTEGER NOT NULL DEFAULT 0, "
            "quota_bytes INTEGER NOT NULL, measured_allocated_bytes INTEGER NOT NULL DEFAULT 0, "
            "measured_apparent_bytes INTEGER NOT NULL DEFAULT 0, pinned INTEGER NOT NULL DEFAULT 0, "
            "lease_owner TEXT, lease_expires_at REAL, last_error_code TEXT)"
        )
        db.execute(
            "INSERT INTO workspaces(workspace_id,state,desired_state,quota_bytes) VALUES (?,?,?,?)",
            (str(workspace_id), "absent", "running", 4 * 1024**3),
        )
    with pytest.raises(RuntimeStateIncompatible) as failure:
        RuntimeStateStore(path)
    message = str(failure.value)
    assert "volume_creation_attempted" in message
    assert "reset_disposable_state.py" in message and "--scope workspaces" in message
    # Nothing was created, altered or dropped: the file is exactly as found.
    with sqlite3.connect(path) as db:
        assert [row[0] for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")] == ["workspaces"]
        assert db.execute("SELECT COUNT(*) FROM workspaces").fetchone()[0] == 1


def test_first_use_row_claims_volume_creation_exactly_once(tmp_path):
    """The one-shot creation rule, on a store this build created itself."""
    store = RuntimeStateStore(str(tmp_path / "state.sqlite3"))
    try:
        workspace_id = uuid4()
        store.ensure_workspace(workspace_id, quota_bytes=4 * 1024**3)
        with store._lock:
            assert store._db.execute(
                "SELECT volume_creation_attempted FROM workspaces WHERE workspace_id=?",
                (str(workspace_id),),
            ).fetchone()[0] == 0
        assert store.claim_initial_volume_creation(workspace_id)
        assert not store.claim_initial_volume_creation(workspace_id)
    finally:
        store.close()
