from __future__ import annotations

import json
from uuid import uuid4

import pytest

from cognita.runtime_broker.jobs import OutputRing, process_identity_is_current
from cognita.runtime_broker.network import BraveSearchService, NetworkPolicy
from cognita.runtime_broker.protocol import BrokerOperation
from cognita.runtime_broker.state import RuntimeStateStore
from cognita.runtime_broker.sdk_adapter import (
    PINNED_RUNTIME_ROOT,
    PINNED_SDK_VERSION,
    PINNED_WHEEL_FILENAME,
    PINNED_WHEEL_SHA256,
)
from cognita.runtime_broker.validation import (
    ArgumentError,
    MAX_JOB_ARG_BYTES,
    MAX_JOB_ARGV,
    ResourcePolicy,
    normalize_guest_path,
    normalize_rpc_arguments,
)


def test_guest_paths_are_canonical_and_never_escape():
    assert normalize_guest_path("src/main.py") == "/workspace/src/main.py"
    assert normalize_guest_path("/workspace/src/main.py") == "/workspace/src/main.py"
    for path in ("../secret", "/etc/passwd", "/workspace/../secret", "a//b", "a\\b"):
        with pytest.raises(ArgumentError):
            normalize_guest_path(path)


def test_job_and_filesystem_limits_are_strict():
    with pytest.raises(ArgumentError):
        normalize_rpc_arguments(BrokerOperation.JOB_START, {"argv": ["python"], "shell_script": "echo"})
    with pytest.raises(ArgumentError):
        normalize_rpc_arguments(BrokerOperation.JOB_START, {"argv": ["python"], "async": "yes"})
    args = normalize_rpc_arguments(
        BrokerOperation.FS_WRITE,
        {"path": "x.txt", "text": "hello", "create_parents": True},
    )
    assert args["path"] == "/workspace/x.txt"
    assert args["content_encoding"] == "text"
    with pytest.raises(ArgumentError):
        normalize_rpc_arguments(BrokerOperation.FS_READ, {"path": "/tmp/nope"})
    assert normalize_rpc_arguments(
        BrokerOperation.FS_LIST, {"path": "/workspace"},
    )["path"] == "/workspace"
    assert normalize_rpc_arguments(
        BrokerOperation.FS_SEARCH, {"roots": ["/workspace"], "pattern": "*.txt"},
    )["roots"] == ["/workspace"]
    assert ResourcePolicy().max_processes == 256


def test_job_argv_preserves_argument_data_and_rejects_only_posix_nul():
    source = "print('line1')\n\tprint('line2')\r\x01"
    normalized = normalize_rpc_arguments(
        BrokerOperation.JOB_START,
        {"argv": ["python3", "-c", source], "cwd": "/workspace"},
    )
    assert normalized["argv"] == ["python3", "-c", source]

    for invalid in (["python3", "\x00"], ["python3", 3], ["python3", None]):
        with pytest.raises(ArgumentError, match="argv contains an invalid argument"):
            normalize_rpc_arguments(
                BrokerOperation.JOB_START,
                {"argv": invalid, "cwd": "/workspace"},
            )


def test_job_argv_count_and_utf8_byte_limits_remain_enforced():
    with pytest.raises(ArgumentError, match="between 1 and 256"):
        normalize_rpc_arguments(
            BrokerOperation.JOB_START,
            {"argv": ["x"] * (MAX_JOB_ARGV + 1), "cwd": "/workspace"},
        )
    with pytest.raises(ArgumentError, match="argv is too large"):
        normalize_rpc_arguments(
            BrokerOperation.JOB_START,
            {"argv": ["x" * (MAX_JOB_ARG_BYTES + 1)], "cwd": "/workspace"},
        )


def test_guest_path_control_character_guard_is_unchanged():
    for path in ("notes\n.txt", "notes\t.txt", "notes\x01.txt"):
        with pytest.raises(ArgumentError, match="control characters"):
            normalize_guest_path(path)


def test_state_leases_reclaim_and_jobs_become_lost_on_restart():
    store = RuntimeStateStore()
    workspace = uuid4()
    store.ensure_workspace(workspace, quota_bytes=4 * 1024**3)
    store.acquire_lease(workspace, ttl_seconds=1)
    assert store.reclaim_expired_leases(now=10**12) == 1
    job = store.create_job(workspace, deadline=10, metadata={"argv_count": 1})
    assert store.active_job(workspace) == job
    assert store.recover_jobs()[0].state == "lost"
    store.close()


def test_output_ring_reports_tail_truncation_and_identity_guard():
    output = OutputRing(limit=4)
    output.append(b"abcdef")
    page = output.page(0)
    assert page.data == b"cdef"
    assert page.first_available_offset == 2
    assert page.truncated is True
    assert process_identity_is_current(expected_pid=3, expected_start_token="x",
                                      observed_pid=3, observed_start_token="x")
    assert not process_identity_is_current(expected_pid=3, expected_start_token="x",
                                           observed_pid=3, observed_start_token="y")


def test_network_and_brave_are_fail_closed_and_redacted():
    policy = NetworkPolicy.from_mapping({"mode": "allowlist", "rules": [
        {"domain": "example.com", "suffix": True, "ports": [443], "protocols": ["https"]}
    ]})
    assert policy.allows("cdn.example.com", 443, "https", ["93.184.216.34"])
    assert not policy.allows("example.com", 443, "https", ["192.168.1.5"])
    assert not policy.allows("example.com", 443, "https", [])
    assert not policy.allows("example.com", 443, "https", ["not-an-ip"])
    with pytest.raises(ValueError):
        NetworkPolicy.from_mapping({"mode": "allowlist", "rules": [
            {"domain": "example.com", "suffix": "false"}
        ]})
    calls = []
    brave = BraveSearchService(lambda **kwargs: calls.append(kwargs) or {
        "results": [{"title": "T", "url": "https://example.com", "snippet": "S", "extra": "drop"}]
    })
    brave.configure("k" * 32, enabled=True)
    result = brave.search("query")
    assert result["results"] == [{"title": "T", "url": "https://example.com", "snippet": "S"}]
    assert calls[0]["api_key"] == "k" * 32
    assert "k" * 32 not in json.dumps(result)


def test_microsandbox_sdk_pin_and_immutable_names():
    assert PINNED_SDK_VERSION == "0.7.0"
    assert PINNED_WHEEL_FILENAME.endswith("manylinux_2_28_x86_64.whl")
    assert len(PINNED_WHEEL_SHA256) == 64
    assert PINNED_RUNTIME_ROOT == "/opt/microsandbox/0.7.0"


def test_multiline_content_and_scripts_remain_valid_bounded_payloads():
    write = normalize_rpc_arguments(
        BrokerOperation.FS_WRITE,
        {"path": "notes.txt", "text": "one\ntwo\n", "create_parents": True},
    )
    assert write["text"] == "one\ntwo\n"
    job = normalize_rpc_arguments(
        BrokerOperation.JOB_START,
        {"shell_script": "set -e\nprintf 'ok\\n'", "env": {"MULTILINE": "a\nb"}},
    )
    assert job["shell_script"].startswith("set -e")
