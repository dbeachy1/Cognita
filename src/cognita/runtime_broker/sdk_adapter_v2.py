"""Production async Microsandbox SDK implementation used by the broker.

This module is deliberately separate from the historical adapter source so a
deployment can audit the exact typed SDK boundary.  It never invokes the
``msb`` CLI and does not accept caller-selected runtime names or host paths.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import importlib.metadata
import inspect
import json
import logging
import multiprocessing
import os
import re
import stat
import time
import uuid
from collections.abc import Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Protocol

PINNED_SDK_VERSION = "0.7.0"
PINNED_WHEEL_FILENAME = "microsandbox-0.7.0-cp310-abi3-manylinux_2_28_x86_64.whl"
PINNED_WHEEL_SHA256 = "848eab6e23b3bc168934b3093baafe26b086348d2914ae710900884e7bb74470"
PINNED_RUNTIME_ROOT = "/opt/microsandbox/0.7.0"
JOB_EXECUTABLE = "cognita-workspace-job"
WORKSPACE_QUOTA_MIB = 4096
WRITABLE_ROOT_QUOTA_MIB = 4096
TMPFS_SIZE_MIB = 512
MAX_ENUM_OUTPUT_BYTES = 8 * 1024 * 1024
# fs_usage (DESIGN-12.18 SS3.4): top-level entries considered before ranking by
# `du` size, and the number of ranked rows actually returned to the caller.
FS_USAGE_LISTING_LIMIT = 2_000
FS_USAGE_MAX_ENTRIES = 20
_JOB_ID = re.compile(r"^[0-9a-fA-F-]{1,80}$")
# Exactly the volume name ``names()`` generates; anything else is not ours.
_VOLUME_NAME = re.compile(r"cognita-ws-data-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
# Bound on the stat walk in ``_walk_usage``: a Workspace is quota-capped at
# 4 GiB, and a venv-sized tree (about 20k entries) walks in well under 100 ms
# on the reference host (1,284 entries: 4 ms). Past this the walk reports unknown, never a
# partial figure.
MAX_USAGE_WALK_ENTRIES = 2_000_000
log = logging.getLogger("cognita.runtime_broker.sdk")


def _walk_usage(root: Path) -> tuple[int | None, int] | None:
    """Bounded (allocated, apparent) byte count of a directory tree, or ``None``.

    Metadata only: ``lstat`` every entry, never follow a symlink, never open a
    file.  ``apparent`` sums ``st_size``; ``allocated`` sums ``st_blocks``
    (512-byte units, the ``du`` figure) and is ``None`` where the platform does
    not report blocks.  Any walk error and the entry cap both yield ``None``:
    the caller must not mistake a partial count for a measurement.
    """
    entries = apparent = allocated = 0
    blocks_known = True
    started = time.monotonic()
    stack = [root]
    try:
        while stack:
            directory = stack.pop()
            with os.scandir(directory) as it:
                for entry in it:
                    entries += 1
                    if entries > MAX_USAGE_WALK_ENTRIES:
                        log.warning("volume usage walk exceeded %d entries root=%s", MAX_USAGE_WALK_ENTRIES, root.name)
                        return None
                    info = entry.stat(follow_symlinks=False)
                    apparent += info.st_size
                    blocks = getattr(info, "st_blocks", None)
                    if blocks is None:
                        blocks_known = False
                    else:
                        allocated += blocks * 512
                    if entry.is_dir(follow_symlinks=False):
                        stack.append(Path(entry.path))
    except OSError as exc:
        log.warning("volume usage walk failed root=%s error=%s", root.name, type(exc).__name__)
        return None
    log.debug(
        "volume usage walk root=%s entries=%d apparent=%d allocated=%s elapsed_ms=%d",
        root.name, entries, apparent, allocated if blocks_known else None,
        round((time.monotonic() - started) * 1000),
    )
    return (allocated if blocks_known else None, apparent)
_operation_context: ContextVar[tuple[str | None, str | None] | None] = ContextVar(
    "workspace_runtime_operation_context", default=None
)


# Microsandbox 0.7.0 exposes ``read_stream`` but its list operation returns a
# fully materialized collection.  Keep enumeration bounded at the guest
# boundary instead of asking the SDK for an unbounded listing.  The helper
# emits metadata only; it never reads file contents or invokes a caller-
# selected command.
_BOUNDED_LIST_HELPER = r'''
import json
import os
import sys

root, recursive, limit = sys.argv[1], sys.argv[2] == "1", int(sys.argv[3])
stack = [root]
emitted = 0
output_bytes = 0
max_output_bytes = int(sys.argv[4])
truncation_marker = b'{"_truncated":true}\n'

def mark_truncated():
    if output_bytes + len(truncation_marker) <= max_output_bytes:
        sys.stdout.buffer.write(truncation_marker)

def mark_error(code):
    payload = json.dumps({"_error": code}, separators=(",", ":")).encode("utf-8") + b"\n"
    if output_bytes + len(payload) <= max_output_bytes:
        sys.stdout.buffer.write(payload)
        sys.stdout.buffer.flush()

def emit(path, kind, size):
    global emitted, output_bytes
    payload = json.dumps(
        {"path": path, "kind": kind, "size": size},
        separators=(",", ":"),
    ).encode("utf-8") + b"\n"
    # Reserve room for the marker so a rejected record can never leave the
    # caller unable to distinguish truncation from a complete short listing.
    if output_bytes + len(payload) + len(truncation_marker) > max_output_bytes:
        return False
    sys.stdout.buffer.write(payload)
    output_bytes += len(payload)
    emitted += 1
    return True

while stack and emitted < limit:
    current = stack.pop()
    try:
        entries = os.scandir(current)
    except OSError:
        if current == root:
            mark_error("not_found")
            raise SystemExit(2)
        continue
    with entries:
        for entry in entries:
            if emitted >= limit:
                break
            path = entry.path
            try:
                is_dir = entry.is_dir(follow_symlinks=False)
                is_file = entry.is_file(follow_symlinks=False)
                size = entry.stat(follow_symlinks=False).st_size if is_file else 0
            except OSError:
                is_dir = is_file = False
                size = 0
            kind = "directory" if is_dir else ("file" if is_file else "other")
            if not emit(path, kind, size):
                mark_truncated()
                emitted = limit
                break
            if recursive and is_dir:
                stack.append(path)
sys.stdout.buffer.flush()
'''


# Broker-owned helper for BrokerOperation.FS_LINES (DESIGN-12.18 SS3.3). Runs
# as ``python3 -c _FS_LINES_PROGRAM <path> <start_line> <end_line> <tail_lines>
# <max_bytes>`` inside the guest with no shell. It reads the file in binary,
# optionally counts total lines, slices the requested line range or tail, and
# prints exactly one JSON line. Argv sentinels: start_line, end_line and
# tail_lines arrive as "0" when the caller did not supply them --
# validation.py already guarantees exactly one selection mode (a line range
# vs tail_lines) reaches here, and 0 can never be a real value for either
# (both are bounded to >= 1 by validation.py), so 0 unambiguously means
# "not given". Exit codes: 2 for a missing file, 1 for any other OSError
# (permission, not-a-file, ...); the adapter maps those to not_found and
# runtime_failure respectively.
#
# Truncation contract (module docstring requirement): when max_bytes cannot
# hold the full requested selection, the program returns only WHOLE lines --
# it stops before the line that would exceed max_bytes rather than emitting
# a partial one. So ``bytes`` in the response is always <= max_bytes and is
# the sum of whichever whole lines fit; it is never max_bytes-aligned to a
# partial line. In tail mode, when the cap bites, the KEPT lines are the
# most recent ones that fit (the walk restarts from the newest line
# backward) so a caller asking for "the tail" still gets the tail and not
# whichever lines happened to come first within the pre-truncation window.
_FS_LINES_PROGRAM = r'''
import base64
import json
import sys

path = sys.argv[1]
start_line = int(sys.argv[2]) or None
end_line = int(sys.argv[3]) or None
tail_lines = int(sys.argv[4]) or None
max_bytes = int(sys.argv[5])

MAX_COUNT_BYTES = 16 * 1024 * 1024

try:
    with open(path, "rb") as handle:
        data = handle.read()
except FileNotFoundError:
    sys.exit(2)
except OSError:
    sys.exit(1)

total_bytes = len(data)
# total_lines is a caller-facing convenience count, not needed to select the
# slice below. Skip it past 16 MiB so a huge file does not pay an extra
# full-file scan on every line-range read; the response reports null and a
# caller that needs the exact count already has total_bytes from fs_stat.
if total_bytes <= MAX_COUNT_BYTES:
    total_lines = data.count(b"\n") + (1 if total_bytes and not data.endswith(b"\n") else 0)
else:
    total_lines = None

# splitlines(keepends=True) is binary-safe and treats a trailing partial
# line (no terminating newline) as its own final entry -- exactly the "line"
# a byte-oriented caller expects back.
lines = data.splitlines(keepends=True)

if tail_lines is not None:
    first_index = max(0, len(lines) - tail_lines)
    window = lines[first_index:]
else:
    first_index = max(0, (start_line - 1) if start_line is not None else 0)
    last_index = (end_line - 1) if end_line is not None else len(lines) - 1
    last_index = min(last_index, len(lines) - 1)
    window = lines[first_index:last_index + 1] if first_index <= last_index else []

selected = []
used_bytes = 0
has_more = False
for line in window:
    line_bytes = len(line)
    if used_bytes + line_bytes > max_bytes:
        has_more = True
        break
    selected.append(line)
    used_bytes += line_bytes

if tail_lines is not None and has_more:
    # Tail mode wants the MOST RECENT lines. The forward walk above keeps
    # whichever lines come first in `window` (oldest-first), which is the
    # wrong end once max_bytes is the binding constraint -- re-walk from the
    # newest line backward so the response still is a tail.
    selected = []
    used_bytes = 0
    for line in reversed(window):
        line_bytes = len(line)
        if used_bytes + line_bytes > max_bytes:
            break
        selected.insert(0, line)
        used_bytes += line_bytes

if selected:
    dropped_from_front = (len(window) - len(selected)) if tail_lines is not None else 0
    reported_start = first_index + 1 + dropped_from_front
    reported_end = reported_start + len(selected) - 1
else:
    reported_start = 0
    reported_end = 0

content = b"".join(selected)
print(json.dumps({
    "content_b64": base64.b64encode(content).decode("ascii"),
    "bytes": len(content),
    "start_line": reported_start,
    "end_line": reported_end,
    "total_lines": total_lines,
    "total_bytes": total_bytes,
    "has_more": has_more,
}, separators=(",", ":")))
'''


@contextmanager
def operation_context(correlation_id: str | None, workspace_id: str | None):
    """Attach safe request/workspace correlation to SDK diagnostics and logs."""
    token = _operation_context.set((correlation_id, workspace_id))
    try:
        yield
    finally:
        _operation_context.reset(token)


def _regex_worker(send: Any, pattern: str, candidates: list[str], limit: int) -> None:
    """Evaluate caller regexes outside the broker process.

    Python's backtracking regex engine cannot be interrupted safely in-process.
    The parent therefore enforces the operation deadline by terminating this
    dedicated, task-owned child if evaluation does not finish in time.
    """
    try:
        compiled = re.compile(pattern)
        matches: list[int] = []
        for index, value in enumerate(candidates):
            if compiled.search(value):
                matches.append(index)
                if len(matches) >= limit:
                    break
        send.send(("ok", matches))
    except re.error:
        send.send(("invalid", []))
    finally:
        send.close()


async def _bounded_regex_indices(
    pattern: str, candidates: list[str], limit: int, deadline: float,
) -> list[int]:
    """Return matching candidate indices within an absolute deadline."""
    if not candidates or limit <= 0:
        return []
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError
    context = multiprocessing.get_context("spawn")
    receive, send = context.Pipe(duplex=False)
    process = context.Process(
        target=_regex_worker, args=(send, pattern, candidates, limit),
        name="cognita-workspace-regex", daemon=True,
    )
    started = False
    try:
        process.start()
        started = True
        send.close()
        # Drain the pipe before joining: a valid result can exceed the pipe
        # buffer, in which case joining first would deadlock until deadline.
        if not await asyncio.to_thread(receive.poll, remaining):
            raise TimeoutError
        try:
            status, indices = receive.recv()
        except EOFError as exc:
            raise SdkOperationError("runtime_failure", retryable=True) from exc
        await asyncio.to_thread(process.join, max(0.0, deadline - time.monotonic()))
        if process.is_alive():
            raise TimeoutError
        if process.exitcode != 0:
            raise SdkOperationError("runtime_failure", retryable=True)
        if status != "ok":
            raise SdkOperationError(status, retryable=status == "runtime_failure")
        return [int(index) for index in indices]
    finally:
        receive.close()
        if not started:
            send.close()
        if started and process.is_alive():
            process.terminate()
            await asyncio.to_thread(process.join, 1.0)
        if started and process.is_alive():
            process.kill()
            await asyncio.to_thread(process.join, 1.0)
        if started and not process.is_alive():
            process.close()


@dataclass(frozen=True, slots=True)
class SdkRuntimeInfo:
    sdk_version: str
    runtime_path: str
    package_location: str


@dataclass(frozen=True, slots=True)
class ExecResult:
    stdout: bytes
    stderr: bytes
    exit_code: int


class SdkOperationError(RuntimeError):
    """Bounded SDK failure context; exception text never crosses the broker."""

    ALLOWED_CATEGORIES = {
        "not_found", "busy", "conflict", "quota", "timeout", "unsupported", "runtime_failure",
    }

    def __init__(
        self,
        category: str,
        *,
        retryable: bool = False,
        stage: str = "unknown",
        evidence: dict[str, Any] | None = None,
        ownership_verified: bool = False,
    ) -> None:
        normalized = category if category in self.ALLOWED_CATEGORIES else "runtime_failure"
        bounded_stage = stage[:64] if isinstance(stage, str) and stage else "unknown"
        correlation_id, workspace_id = _operation_context.get() or (None, None)
        super().__init__(normalized)
        self.category = normalized
        self.retryable = bool(retryable)
        self.stage = bounded_stage
        self.correlation_id = correlation_id
        self.workspace_id = workspace_id
        self.ownership_verified = bool(ownership_verified)
        self.evidence = dict(evidence or {})


class RuntimeAdapter(Protocol):
    async def readiness_probe(self) -> dict[str, Any]: ...
    async def ensure(self, workspace_id: uuid.UUID, config: dict[str, Any]) -> dict[str, Any]: ...
    async def inspect(self, workspace_id: uuid.UUID) -> dict[str, Any]: ...
    async def start(self, workspace_id: uuid.UUID) -> dict[str, Any]: ...
    async def stop(self, workspace_id: uuid.UUID, *, force: bool = False) -> dict[str, Any]: ...
    async def remove(self, workspace_id: uuid.UUID) -> dict[str, Any]: ...
    async def execute(self, workspace_id: uuid.UUID, operation: str, arguments: dict[str, Any]) -> dict[str, Any]: ...
    async def copy_from_host(self, workspace_id: uuid.UUID, host_path: str, guest_path: str) -> None: ...
    async def copy_to_host(self, workspace_id: uuid.UUID, guest_path: str, host_path: str) -> None: ...


class UnavailableAdapter:
    async def readiness_probe(self) -> dict[str, Any]:
        raise SdkOperationError("runtime_failure", retryable=True)

    async def _unavailable(self, *_args: Any, **_kwargs: Any) -> Any:
        raise SdkOperationError("runtime_failure", retryable=True)

    ensure = inspect = start = stop = remove = execute = copy_from_host = copy_to_host = _unavailable


def verify_runtime_selection() -> str:
    version = importlib.metadata.version("microsandbox")
    if version != PINNED_SDK_VERSION:
        raise SdkOperationError("unsupported")
    if any(os.environ.get(key) for key in ("MSB_HOME", "MSB_PATH", "MSB_LIBKRUNFW_PATH")):
        raise SdkOperationError("unsupported")
    root = os.environ.get("COGNITA_MICROSANDBOX_RUNTIME_ROOT", PINNED_RUNTIME_ROOT)
    if root != PINNED_RUNTIME_ROOT:
        raise SdkOperationError("unsupported")
    return root


def pinned_sdk_info() -> SdkRuntimeInfo:
    root = verify_runtime_selection()
    distribution = importlib.metadata.distribution("microsandbox")
    return SdkRuntimeInfo(PINNED_SDK_VERSION, root, str(Path(distribution.locate_file(""))))


def _sdk_types() -> tuple[Any, Any, Any]:
    try:
        from microsandbox import Network, Sandbox, Volume  # type: ignore
    except ImportError as exc:
        raise SdkOperationError("runtime_failure", retryable=True) from exc
    return Sandbox, Volume, Network


@dataclass(frozen=True, slots=True)
class _SdkNetworkTypes:
    """Typed network controls exported by the pinned Microsandbox SDK.

    Keep this separate from ``_sdk_types`` so the no-network path remains
    usable by the small SDK fakes used by lifecycle tests.  Non-off modes must
    prove that every typed control used below is present before constructing a
    guest network object.
    """

    action: Any
    dns_config: Any
    destination: Any
    dest_group: Any
    direction: Any
    network_policy: Any
    network_profile: Any
    protocol: Any
    rule: Any


def _sdk_network_types() -> _SdkNetworkTypes:
    try:
        from microsandbox import (  # type: ignore
            Action,
            DestGroup,
            Destination,
            Direction,
            DnsConfig,
            NetworkPolicy,
            NetworkProfile,
            Protocol,
            Rule,
        )
    except ImportError as exc:
        raise SdkOperationError(
            "unsupported", stage="network",
            evidence={
                "reason": "pinned_sdk_network_policy_controls_unavailable",
                "required": [
                    "NetworkPolicy", "NetworkProfile", "Rule", "Destination",
                    "DestGroup", "Protocol", "Direction", "Action",
                ],
            },
        ) from exc
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


def _managed_root_image(image: str, size_mib: int) -> Any:
    """Build the pinned SDK's bounded OCI root image source.

    ``Image.oci(root_disk=RootDisk.managed(...))`` is the supported 0.7.0
    control for a sparse, persistent writable root.  Do not fall back to the
    deprecated integer/upper-size sugar or to an unbounded image: a missing
    factory is a release-blocking capability failure.
    """
    try:
        from microsandbox import Image, RootDisk  # type: ignore
        factory = getattr(RootDisk, "managed", None)
        oci = getattr(Image, "oci", None)
        if not callable(factory) or not callable(oci):
            raise AttributeError
        return oci(image, root_disk=factory(size_mib=size_mib))
    except Exception as exc:  # SDK factories are capability probes; fail closed on any mismatch.
        raise SdkOperationError(
            "unsupported", stage="quota_probe",
            evidence={
                "requirement": "4GiB writable root ceiling",
                "reason": "pinned_sdk_root_disk_control_unavailable",
                "writable_paths": ["/", "/root/.cache", "/var/log"],
            },
        ) from exc


def _bounded_tmpfs_mount(volume_type: Any, size_mib: int) -> Any:
    """Return a bounded ephemeral /tmp mount from the pinned SDK."""
    factory = getattr(volume_type, "tmpfs", None)
    if not callable(factory):
        raise SdkOperationError(
            "unsupported", stage="quota_probe",
            evidence={
                "requirement": "bounded /tmp scratch",
                "reason": "pinned_sdk_tmpfs_control_unavailable",
                "size_mib": size_mib,
            },
        )
    try:
        return factory(size_mib=size_mib)
    except Exception as exc:  # SDK factories are capability probes; fail closed on any mismatch.
        raise SdkOperationError(
            "unsupported", stage="quota_probe",
            evidence={
                "requirement": "bounded /tmp scratch",
                "reason": "pinned_sdk_tmpfs_signature_unavailable",
                "size_mib": size_mib,
            },
        ) from exc


def _local_pull_policy() -> Any:
    """Require the pre-pulled toolbox image and never contact a registry.

    Workspace creation is intentionally an offline operation.  The pinned
    Microsandbox SDK exposes this as ``PullPolicy.NEVER``; keeping the import
    behind the SDK boundary also lets the broker's fake adapters run without
    installing the native SDK in unit-test environments.
    """
    try:
        from microsandbox import PullPolicy  # type: ignore
    except ImportError:
        return "never"
    return PullPolicy.NEVER


async def _await(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


class MicrosandboxSdkAdapter:
    def __init__(
        self,
        *,
        image: str = "cognita-workspace-toolbox:12.6.0",
        timeout_seconds: float = 600.0,
        workspace_data_root: str | None = None,
        volume_data_dir: str | None = None,
    ) -> None:
        if not image or any(c.isspace() for c in image):
            raise ValueError("toolbox image is invalid")
        if not 0 < timeout_seconds <= 3600:
            raise ValueError("runtime timeout is outside the supported range")
        self.image, self.timeout_seconds = image, timeout_seconds
        # A host path is evidence only when supplied by the broker/runtime
        # configuration and then verified against the SDK-reported path.  The
        # adapter never derives a path from a Workspace UUID or volume name
        # AS EVIDENCE: ``host_path`` stays ``not_reported`` and nothing
        # destructive is ever addressed through a derived path.  13.0.2 does
        # derive one for a READ-ONLY byte measurement (``_volume_backing_dir``)
        # because the SDK's own figure is not a measurement; see there.
        self.workspace_data_root = workspace_data_root or os.environ.get("COGNITA_WORKSPACE_DATA_ROOT")
        # Where the pinned SDK keeps directory volumes: ``$MSB_DATA_DIR/volumes/
        # <name>`` (compose sets MSB_DATA_DIR; the SDK's own default is
        # ``~/.microsandbox``).  Tests point this at a temp dir.
        self.volume_data_dir = volume_data_dir or os.environ.get("MSB_DATA_DIR") or str(Path.home() / ".microsandbox")
        self.runtime_info = pinned_sdk_info()
        self._configs: dict[str, dict[str, Any]] = {}
        # Microsandbox agent connections belong to the connected Sandbox
        # object. Repeated connect_or_start calls can close that connection,
        # so retain one validated connection per Workspace.
        self._connections: dict[str, Any] = {}
        self.last_readiness: dict[str, Any] = {"status": "degraded", "stage": "not_started"}

    @staticmethod
    def names(workspace_id: uuid.UUID) -> tuple[str, str]:
        value = str(workspace_id)
        return f"cognita-ws-{value}", f"cognita-ws-data-{value}"

    @staticmethod
    def _labels(workspace_id: uuid.UUID) -> dict[str, str]:
        return {"cognita.owner": "workspace-broker", "cognita.workspace": str(workspace_id)}

    def _sandbox_labels(
        self, workspace_id: uuid.UUID, *, vcpus: int, memory_mib: int,
        volume_name: str, network_mode: str = "off", network_rules_digest: str = "",
        root_quota_bytes: int | None = None, tmpfs_mib: int | None = None,
    ) -> dict[str, str]:
        return {
            **self._labels(workspace_id),
            "cognita.image": self.image,
            "cognita.vcpus": str(vcpus),
            "cognita.memory_mib": str(memory_mib),
            "cognita.volume": volume_name,
            "cognita.network": network_mode,
            "cognita.network_rules": network_rules_digest,
            **({"cognita.root_quota_bytes": str(root_quota_bytes)} if root_quota_bytes is not None else {}),
            **({"cognita.tmpfs_mib": str(tmpfs_mib)} if tmpfs_mib is not None else {}),
        }

    def _volume_labels(self, workspace_id: uuid.UUID, *, quota_mib: int) -> dict[str, str]:
        return {**self._labels(workspace_id), "cognita.quota_mib": str(quota_mib)}

    def _verified_volume_evidence(self, volume: Any, workspace_id: uuid.UUID) -> dict[str, Any]:
        """Return only SDK-reported identity/path/allocation evidence.

        The pinned SDK does not currently guarantee physical allocation or a
        host backing path.  Missing values remain ``None``/``not_reported``;
        a deterministic child of the configured root is never fabricated.
        """
        identity = getattr(volume, "id", None) or getattr(volume, "volume_id", None)
        result: dict[str, Any] = {
            "volume_id": str(identity) if identity is not None else None,
            "measured_allocated_bytes": None,
            "host_path": None,
            "path_status": "not_reported",
            "usage_status": "unknown",
        }
        for attr in ("allocated_bytes", "actual_bytes", "disk_usage_bytes"):
            value = getattr(volume, attr, None)
            if value is not None:
                try:
                    value = int(value)
                except (TypeError, ValueError):
                    value = None
                if value is not None and value >= 0:
                    result["measured_allocated_bytes"] = value
                    result["usage_status"] = "measured"
                    break
        raw_path = None
        for attr in ("host_path", "backing_path", "mount_path"):
            candidate = getattr(volume, attr, None)
            if isinstance(candidate, str) and candidate:
                raw_path = candidate
                break
        if raw_path and self.workspace_data_root:
            try:
                root = Path(self.workspace_data_root).resolve(strict=False)
                path = Path(raw_path)
                if path.is_absolute():
                    resolved = path.resolve(strict=False)
                    if resolved == root or root in resolved.parents:
                        result["host_path"] = str(resolved)
                        result["path_status"] = "verified"
                    else:
                        result["path_status"] = "stale"
            except (OSError, RuntimeError, ValueError):
                result["path_status"] = "not_reported"
        return result

    @staticmethod
    def _network_object(
        network_type: Any,
        policy: dict[str, Any],
        sdk_network_types: _SdkNetworkTypes | None = None,
    ) -> Any:
        """Build an SDK network object or fail closed when unsupported.

        The 0.7.0 SDK has no ``Network.allowlist`` or ``Network.public``
        factories.  Allowlist mode is represented by its typed policy/rule
        objects, while public mode uses the SDK's explicit PUBLIC profile.  A
        missing control is an unavailable mode, never a reason to substitute
        a weaker filter (or silently use no networking).
        """
        mode = policy.get("mode", "off")
        if mode == "off":
            factory = getattr(network_type, "none", None)
            if not callable(factory):
                raise SdkOperationError("unsupported", stage="network")
            return factory()
        rules = policy.get("rules", [])
        if not isinstance(rules, list) or any(not isinstance(rule, dict) for rule in rules):
            raise SdkOperationError("unsupported", stage="network")
        if mode == "allowlist":
            if sdk_network_types is None:
                raise SdkOperationError(
                    "unsupported", stage="network",
                    evidence={"mode": mode, "reason": "pinned_sdk_allowlist_control_unavailable"},
                )
            controls = sdk_network_types
            for value in (
                controls.action,
                controls.dns_config,
                controls.destination,
                controls.dest_group,
                controls.direction,
                controls.network_policy,
                controls.protocol,
                controls.rule,
            ):
                if value is None:
                    raise SdkOperationError(
                        "unsupported", stage="network",
                        evidence={"mode": mode, "reason": "pinned_sdk_allowlist_control_unavailable"},
                    )
            try:
                allow = controls.rule.allow
                allow_dns = controls.rule.allow_dns
                destination_factory = controls.destination.domain
                suffix_factory = controls.destination.domain_suffix
                sdk_policy = controls.network_policy
                deny = controls.action.DENY
                egress = controls.direction.EGRESS
                tcp = controls.protocol.TCP
                dns_rules = tuple(allow_dns())
                if not dns_rules:
                    raise TypeError("SDK did not provide explicit DNS rules")
                translated = list(dns_rules)
                for raw_rule in rules:
                    domain = raw_rule.get("domain")
                    ports = raw_rule.get("ports")
                    protocols = raw_rule.get("protocols")
                    suffix = raw_rule.get("suffix", False)
                    if (
                        not isinstance(domain, str)
                        or not isinstance(ports, list)
                        or not ports
                        or not isinstance(protocols, list)
                        or not protocols
                        or not isinstance(suffix, bool)
                        or any(isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535 for port in ports)
                        or any(protocol not in {"http", "https"} for protocol in protocols)
                    ):
                        raise TypeError("normalized network rule is invalid")
                    if set(protocols) != {"http", "https"}:
                        raise SdkOperationError(
                            "unsupported", stage="network",
                            evidence={
                                "mode": mode,
                                "reason": "pinned_sdk_protocol_scheme_control_unavailable",
                                "protocols": sorted(set(protocols)),
                            },
                        )
                    destination = (suffix_factory if suffix else destination_factory)(domain)
                    # The pinned SDK exposes only L4 protocol values.  A TCP
                    # rule is lossless only when Cognita explicitly permits
                    # both application schemes; single-scheme rules fail
                    # closed rather than widening HTTPS-only access to HTTP.
                    for port in ports:
                        translated.append(
                            allow(
                                direction=egress,
                                protocol=tcp,
                                port=port,
                                destination=destination,
                            )
                        )
                custom_policy = sdk_policy(
                    default_egress=deny,
                    default_ingress=deny,
                    rules=tuple(translated),
                )
                dns = controls.dns_config(rebind_protection=True)
                return network_type(policy=custom_policy, dns=dns, strict=True)
            except (AttributeError, TypeError, ValueError) as exc:
                raise SdkOperationError(
                    "unsupported", stage="network",
                    evidence={"mode": mode, "reason": "pinned_sdk_allowlist_signature_unavailable"},
                ) from exc
        if mode == "unrestricted_public":
            if not policy.get("explicit_confirmation", False):
                raise SdkOperationError("unsupported", stage="network")
            if sdk_network_types is None:
                raise SdkOperationError(
                    "unsupported", stage="network",
                    evidence={"mode": mode, "reason": "pinned_sdk_public_control_unavailable"},
                )
            factory = getattr(network_type, "from_profiles", None)
            profile = getattr(sdk_network_types.network_profile, "PUBLIC", None)
            if not callable(factory) or profile is None:
                raise SdkOperationError(
                    "unsupported", stage="network",
                    evidence={"mode": mode, "reason": "pinned_sdk_public_control_unavailable"},
                )
            try:
                public_network = factory(profile)
                public_policy = getattr(public_network, "policy", None)
                deny = getattr(sdk_network_types.action, "DENY", None)
                allow = getattr(sdk_network_types.action, "ALLOW", None)
                egress = getattr(sdk_network_types.direction, "EGRESS", None)
                public_group = getattr(sdk_network_types.dest_group, "PUBLIC", None)
                host_group = getattr(sdk_network_types.dest_group, "HOST", None)
                udp = getattr(sdk_network_types.protocol, "UDP", None)
                tcp = getattr(sdk_network_types.protocol, "TCP", None)
                missing_controls = []
                if public_policy is None:
                    missing_controls.append("policy")
                if any(value is None for value in (deny, allow, egress, public_group, host_group, udp, tcp)):
                    missing_controls.append("public_profile_typed_controls")
                profile_rules = getattr(public_policy, "rules", ()) if public_policy is not None else ()
                if not isinstance(profile_rules, tuple) or not profile_rules:
                    missing_controls.append("public_profile_rules")
                else:
                    destinations = set()
                    public_rule_count = 0
                    host_rule_count = 0
                    for rule in profile_rules:
                        destination = getattr(rule, "destination", None)
                        kind = getattr(getattr(destination, "kind", None), "value", None)
                        value = getattr(destination, "value", None)
                        if (
                            getattr(rule, "action", None) != allow
                            or getattr(rule, "direction", None) != egress
                            or kind != "group"
                            or value not in {public_group, host_group}
                        ):
                            missing_controls.append("public_only_destination_rules")
                            break
                        destinations.add(value)
                        protocol = getattr(rule, "protocol", None)
                        port = getattr(rule, "port", None)
                        if value == public_group:
                            public_rule_count += 1
                            if protocol is not None or port is not None:
                                missing_controls.append("public_rule_scope")
                                break
                        else:
                            host_rule_count += 1
                            if protocol not in {udp, tcp} or port != 53:
                                missing_controls.append("host_dns_rule_scope")
                                break
                    if public_group not in destinations or host_group not in destinations:
                        missing_controls.append("public_profile_public_and_dns_rules")
                    if public_rule_count != 1 or host_rule_count != 2:
                        missing_controls.append("public_profile_rule_cardinality")
                if missing_controls:
                    raise SdkOperationError(
                        "unsupported", stage="network",
                        evidence={
                            "mode": mode,
                            "reason": "pinned_sdk_public_profile_contract_unavailable",
                            "missing_controls": missing_controls,
                            "required": [
                                "default-deny ingress and egress",
                                "strict HTTPS authority inspection",
                                "DNS rebinding protection",
                                "PUBLIC destination only plus HOST DNS",
                                "private/host/link-local/metadata denial",
                            ],
                        },
                    )
                custom_policy = sdk_network_types.network_policy(
                    default_egress=deny,
                    default_ingress=deny,
                    rules=profile_rules,
                )
                custom_dns = sdk_network_types.dns_config(rebind_protection=True)
                return network_type(policy=custom_policy, dns=custom_dns, strict=True)
            except SdkOperationError:
                raise
            except (TypeError, ValueError) as exc:
                raise SdkOperationError(
                    "unsupported", stage="network",
                    evidence={"mode": mode, "reason": "pinned_sdk_public_signature_unavailable"},
                ) from exc
        raise SdkOperationError("unsupported", stage="network")

    def _deadline(self, seconds: float | None = None) -> float:
        return time.monotonic() + min(self.timeout_seconds, seconds or self.timeout_seconds)

    def _drop_connection(self, workspace_id: str | None) -> None:
        connections = getattr(self, "_connections", None)
        if workspace_id and isinstance(connections, dict):
            connections.pop(workspace_id, None)

    @staticmethod
    def _category(exc: BaseException) -> str:
        name = type(exc).__name__.casefold()
        for needle, category in (("notfound", "not_found"), ("not_found", "not_found"), ("busy", "busy"), ("quota", "quota"), ("space", "quota"), ("timeout", "timeout"), ("unsupported", "unsupported"), ("notimplemented", "unsupported")):
            if needle in name:
                return category
        return "runtime_failure"

    async def _call(self, event: str, value: Any, deadline: float) -> Any:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError
        trace = os.environ.get("COGNITA_MSB_TRACE") == "1"
        if trace:
            log.warning("sdk stage operation=%s phase=begin", event)
        try:
            async with asyncio.timeout(remaining):
                result = await _await(value)
            if trace:
                log.warning("sdk stage operation=%s phase=complete", event)
            return result
        except asyncio.CancelledError:
            log.info("sdk operation canceled", extra={"event": "sdk_cancel", "operation": event})
            raise
        except TimeoutError:
            correlation_id, workspace_id = _operation_context.get() or (None, None)
            log.warning("sdk operation timed out", extra={
                "event": "sdk_timeout", "operation": event,
                "stage": event, "correlation_id": correlation_id,
                "workspace_id": workspace_id,
            })
            # A timeout leaves ownership/effect uncertain.  Do not advertise
            # an automatic retry until reconciliation proves the owned
            # sandbox and volume identities.
            raise SdkOperationError("timeout", retryable=False, stage=event)
        except SdkOperationError as exc:
            if exc.stage == "unknown":
                exc.stage = event
            if exc.category == "runtime_failure":
                _correlation_id, workspace_id = _operation_context.get() or (None, None)
                self._drop_connection(workspace_id)
            raise
        except Exception as exc:
            category = self._category(exc)
            correlation_id, workspace_id = _operation_context.get() or (None, None)
            self._drop_connection(workspace_id)
            # Keep the human-readable message useful in deployments whose
            # formatter drops structured ``extra`` fields.  The values here
            # are bounded broker context only; exception text, paths, and
            # guest data must never cross this diagnostic boundary.
            log.warning(
                "sdk operation failed: operation=%s stage=%s category=%s sdk_error_class=%s correlation_id=%s",
                event[:64], event[:64], category, type(exc).__name__[:64], correlation_id or "none",
                extra={
                    "event": "sdk_failure", "operation": event, "stage": event,
                    "category": category, "sdk_error_class": type(exc).__name__[:64],
                    "correlation_id": correlation_id,
                    "workspace_id": workspace_id,
                },
            )
            raise SdkOperationError(
                category, retryable=category == "busy", stage=event,
            ) from exc

    async def _get(self, kind: Any, name: str, deadline: float) -> Any | None:
        try:
            return await self._call("get", kind.get(name), deadline)
        except SdkOperationError as exc:
            if exc.category == "not_found":
                return None
            raise

    async def _remove(self, kind: Any, name: str, deadline: float) -> bool:
        obj = await self._get(kind, name, deadline)
        if obj is None:
            return False
        fn = getattr(obj, "remove", None) or getattr(obj, "destroy", None)
        if not callable(fn):
            raise SdkOperationError("unsupported")
        await self._call("remove", fn(), deadline)
        return True

    @staticmethod
    def _status(obj: Any) -> str:
        status = getattr(obj, "status", "")
        return str(getattr(status, "value", status)).casefold()

    def _validate_owner(self, obj: Any, workspace_id: uuid.UUID, *, labels: Any = None) -> None:
        labels = getattr(obj, "labels", None) if labels is None else labels
        if not isinstance(labels, dict) or any(labels.get(key) != value for key, value in self._labels(workspace_id).items()):
            raise SdkOperationError("runtime_failure")

    @staticmethod
    def _sandbox_config(obj: Any) -> dict[str, Any]:
        """Read the SDK's persisted sandbox specification, not handle attributes.

        SDK 0.7.0 get/create return SandboxHandle, whose labels, image, mounts,
        and resources live in config(). Older in-process doubles expose direct
        attributes; only fully populated doubles may take that test path.
        """
        try:
            getter = getattr(obj, "config", None)
            if callable(getter):
                config = getter()
            elif isinstance(getattr(obj, "config_json", None), str):
                config = json.loads(obj.config_json)
            else:
                labels = getattr(obj, "labels", None)
                image = getattr(obj, "image", None)
                volumes = getattr(obj, "volumes", None)
                cpus = getattr(obj, "cpus", None)
                memory = getattr(obj, "memory", None)
                if not isinstance(labels, dict) or image is None or not isinstance(volumes, dict) or cpus is None or memory is None:
                    raise SdkOperationError("runtime_failure", stage="reconcile")
                config = {"labels": labels, "image": image, "volumes": volumes,
                          "resources": {"cpus": cpus, "memory_mib": memory}}
            if not isinstance(config, dict):
                raise SdkOperationError("runtime_failure", stage="reconcile")
            return config
        except SdkOperationError:
            raise
        except Exception as exc:
            raise SdkOperationError("runtime_failure", stage="reconcile") from exc

    def _validate_config(self, obj: Any, workspace_id: uuid.UUID) -> dict[str, str]:
        config = self._sandbox_config(obj)
        labels = config.get("labels")
        self._validate_owner(obj, workspace_id, labels=labels)
        name, volume_name = self.names(workspace_id)
        if "name" in config and config["name"] != name:
            raise SdkOperationError("runtime_failure", stage="reconcile")
        required = {
            "cognita.image": self.image,
            "cognita.volume": volume_name,
        }
        if any(labels.get(key) != value for key, value in required.items()):
            raise SdkOperationError("runtime_failure", stage="reconcile", ownership_verified=True)
        try:
            label_vcpus = int(labels["cognita.vcpus"])
            label_memory = int(labels["cognita.memory_mib"])
        except (KeyError, TypeError, ValueError) as exc:
            raise SdkOperationError("runtime_failure") from exc
        if label_vcpus < 1 or label_memory < 128:
            raise SdkOperationError("runtime_failure")
        expected = self._configs.get(str(workspace_id), {})
        image = config.get("image")
        if isinstance(image, dict):
            oci = image.get("Oci")
            image_reference = oci.get("reference") if isinstance(oci, dict) else None
        elif isinstance(image, str):
            image_reference = image
        else:
            image_reference = getattr(image, "reference", None)
        if image_reference != self.image:
            raise SdkOperationError("runtime_failure", stage="reconcile", ownership_verified=True)
        volumes = config.get("volumes")
        if isinstance(volumes, dict):
            mounted = volumes.get("/workspace")
            mounted_name = getattr(mounted, "name", mounted)
            if mounted_name != volume_name:
                raise SdkOperationError("runtime_failure")
        elif isinstance(config.get("mounts"), list):
            workspace_mounts = [mount for mount in config["mounts"]
                                if isinstance(mount, dict) and mount.get("guest") == "/workspace"]
            if (len(workspace_mounts) != 1 or workspace_mounts[0].get("name") != volume_name
                    or str(workspace_mounts[0].get("type", "")).casefold() != "named"):
                raise SdkOperationError("runtime_failure")
        else:
            raise SdkOperationError("runtime_failure")
        resources = config.get("resources")
        if not isinstance(resources, dict):
            raise SdkOperationError("runtime_failure")
        for actual_key, key, labeled in (
            ("cpus", "vcpus", label_vcpus),
            ("memory_mib", "memory_mib", label_memory),
        ):
            actual = resources.get(actual_key)
            if not isinstance(actual, int) or actual != labeled:
                raise SdkOperationError("runtime_failure")
            if key in expected and labeled != int(expected[key]):
                raise SdkOperationError("runtime_failure")

        network_mode = labels.get("cognita.network")
        if network_mode is None:
            raise SdkOperationError("runtime_failure", stage="reconcile", ownership_verified=True)
        if network_mode not in {"off", "allowlist", "unrestricted_public"}:
            raise SdkOperationError("runtime_failure", stage="reconcile", ownership_verified=True)
        expected_network = expected.get("network_mode")
        if expected_network is not None and network_mode != expected_network:
            raise SdkOperationError("runtime_failure", stage="reconcile", ownership_verified=True)
        expected_rules = expected.get("network_rules_digest")
        if expected_rules is not None and labels.get("cognita.network_rules") != expected_rules:
            raise SdkOperationError("runtime_failure", stage="reconcile", ownership_verified=True)
        expected_root_quota = expected.get("root_quota_bytes")
        labeled_root_quota = labels.get("cognita.root_quota_bytes")
        if "mounts" in config:
            # The 12.6 broker always requests separate 4 GiB writable-root
            # and 512 MiB /tmp ceilings. A restart loses _configs, so the
            # persisted SDK specification must prove both independently.
            if (labeled_root_quota != str(WRITABLE_ROOT_QUOTA_MIB * 2**20)
                    or labels.get("cognita.tmpfs_mib") != str(TMPFS_SIZE_MIB)):
                raise SdkOperationError("runtime_failure", stage="quota_probe", ownership_verified=True)
        if labeled_root_quota is not None and isinstance(image, dict):
            root_disk = image["Oci"].get("root_disk")
            try:
                if (not isinstance(root_disk, dict) or str(root_disk.get("kind", "")).casefold() != "managed"
                        or int(root_disk["size_mib"]) * 2**20 != int(labeled_root_quota)):
                    raise SdkOperationError("runtime_failure", stage="quota_probe", ownership_verified=True)
            except (KeyError, TypeError, ValueError) as exc:
                raise SdkOperationError("runtime_failure", stage="quota_probe", ownership_verified=True) from exc
        if expected_root_quota is not None:
            try:
                if int(labels["cognita.root_quota_bytes"]) != int(expected_root_quota):
                    raise SdkOperationError("runtime_failure", stage="quota_probe", ownership_verified=True)
            except (KeyError, TypeError, ValueError) as exc:
                raise SdkOperationError("runtime_failure", stage="quota_probe", ownership_verified=True) from exc
        expected_tmpfs_mib = expected.get("tmpfs_mib")
        labeled_tmpfs_mib = labels.get("cognita.tmpfs_mib")
        if labeled_tmpfs_mib is not None and isinstance(config.get("mounts"), list):
            tmp_mounts = [mount for mount in config["mounts"]
                          if isinstance(mount, dict) and mount.get("guest") == "/tmp"]
            try:
                if (len(tmp_mounts) != 1 or str(tmp_mounts[0].get("type", "")).casefold() != "tmpfs"
                        or int(tmp_mounts[0].get("size_mib")) != int(labeled_tmpfs_mib)):
                    raise SdkOperationError("runtime_failure", stage="quota_probe", ownership_verified=True)
            except (TypeError, ValueError) as exc:
                raise SdkOperationError("runtime_failure", stage="quota_probe", ownership_verified=True) from exc
        if expected_tmpfs_mib is not None:
            try:
                if int(labels["cognita.tmpfs_mib"]) != int(expected_tmpfs_mib):
                    raise SdkOperationError("runtime_failure", stage="quota_probe", ownership_verified=True)
            except (KeyError, TypeError, ValueError) as exc:
                raise SdkOperationError("runtime_failure", stage="quota_probe", ownership_verified=True) from exc
        network_config = config.get("network")
        if "mounts" in config and not isinstance(network_config, dict):
            raise SdkOperationError("runtime_failure", stage="reconcile", ownership_verified=True)
        if isinstance(network_config, dict):
            enabled = network_config.get("enabled")
            if not isinstance(enabled, bool):
                raise SdkOperationError("runtime_failure", stage="reconcile", ownership_verified=True)
            if network_mode == "off":
                # Microsandbox 0.7.0 persists Network.none() as an enabled
                # network wrapper with a non-strict, default-deny policy.
                # ``enabled=False`` is therefore not evidence of no egress;
                # prove the effective policy instead and fail closed when the
                # persisted shape is incomplete or permissive.
                policy = network_config.get("policy")
                rules = policy.get("rules") if isinstance(policy, dict) else None
                if (
                    not isinstance(policy, dict)
                    or str(policy.get("default_egress", "")).casefold() != "deny"
                    or str(policy.get("default_ingress", "")).casefold() != "deny"
                    or not isinstance(rules, (list, tuple))
                    or rules
                ):
                    raise SdkOperationError("runtime_failure", stage="reconcile", ownership_verified=True)
            elif not enabled:
                # Retain the positive enabled check for public modes: an
                # opaque label must not make a disabled persisted network
                # appear usable.
                raise SdkOperationError("runtime_failure", stage="reconcile", ownership_verified=True)
        return labels

    def _validate_volume(
        self, obj: Any, workspace_id: uuid.UUID, quota_mib: int | None = None,
    ) -> None:
        self._validate_owner(obj, workspace_id)
        labels = getattr(obj, "labels", {})
        try:
            labeled = int(labels["cognita.quota_mib"])
        except (KeyError, TypeError, ValueError) as exc:
            raise SdkOperationError("runtime_failure") from exc
        expected_quota = quota_mib
        if expected_quota is None:
            expected_quota = self._configs.get(str(workspace_id), {}).get("quota_mib")
        if not 1 <= labeled <= WORKSPACE_QUOTA_MIB or (expected_quota is not None and labeled != expected_quota):
            raise SdkOperationError("runtime_failure", stage="quota_probe", ownership_verified=True)
        actual = getattr(obj, "quota_mib", None)
        try:
            if actual is None or int(actual) != labeled:
                raise SdkOperationError("runtime_failure", stage="quota_probe", ownership_verified=True)
        except (TypeError, ValueError) as exc:
            raise SdkOperationError("runtime_failure", stage="quota_probe", ownership_verified=True) from exc

    async def readiness_probe(self) -> dict[str, Any]:
        Sandbox, Volume, _ = _sdk_types()
        token, challenge = uuid.uuid4().hex, uuid.uuid4().hex
        sandbox_name, volume_name = f"cognita-probe-{token}", f"cognita-probe-data-{token}"
        deadline = self._deadline(90)
        handle = None
        stage = "create"
        cleanup_ok = True
        try:
            await self._call("probe_volume_create", Volume.create(volume_name, quota_mib=64, labels={"cognita.owner": "workspace-broker-probe", "cognita.probe": token}), deadline)
            await self._call("probe_sandbox_create", Sandbox.create(sandbox_name, image=self.image, pull_policy=_local_pull_policy(), cpus=1, memory=512, detached=True, replace=False, volumes={"/workspace": Volume.named(volume_name)}, labels={"cognita.owner": "workspace-broker-probe", "cognita.probe": token}), deadline)
            stage = "connect"
            raw = await self._get(Sandbox, sandbox_name, deadline)
            if raw is None:
                raise SdkOperationError("not_found")
            handle = await self._call("probe_connect", raw.connect(), deadline)
            stage = "exec"
            result = await self._call("probe_exec", handle.exec("python3", ["-c", "import sys; print(sys.version_info.major); print(sys.argv[1])", challenge], cwd="/workspace", env={}, timeout=max(1, deadline - time.monotonic())), deadline)
            checked = self._result(result)
            lines = checked.stdout.decode("utf-8", "strict").splitlines()
            if checked.exit_code != 0 or len(lines) < 2 or lines[0].strip() != "3" or lines[1].strip() != challenge:
                raise SdkOperationError("runtime_failure")
            self.last_readiness = {"status": "ok", "stage": "complete", "cleanup": "pending"}
        except BaseException:
            self.last_readiness = {"status": "degraded", "stage": stage, "reason": "probe_failed"}
            log.warning("readiness probe failed", extra={"event": "readiness_failure", "stage": stage})
            raise
        finally:
            if handle is not None:
                try:
                    await self._call("probe_stop", self._stop(handle), deadline)
                except BaseException:  # noqa: BLE001 - cancellation must not skip owned cleanup
                    cleanup_ok = False
                    log.warning("readiness stop cleanup failed", extra={"event": "cleanup_failure", "stage": "stop"})
            for kind, name, label in ((Sandbox, sandbox_name, "sandbox_remove"), (Volume, volume_name, "volume_remove")):
                try:
                    await self._remove(kind, name, deadline)
                except BaseException:  # noqa: BLE001 - attempt every owned cleanup step
                    cleanup_ok = False
                    log.warning("readiness cleanup failed", extra={"event": "cleanup_failure", "stage": label})
            if not cleanup_ok:
                self.last_readiness = {"status": "degraded", "stage": "cleanup", "reason": "cleanup_incomplete"}
                log.error("readiness cleanup incomplete", extra={"event": "cleanup_incomplete"})
                raise SdkOperationError("runtime_failure", retryable=True)
            if self.last_readiness.get("status") == "ok":
                self.last_readiness["cleanup"] = "complete"
        return dict(self.last_readiness)

    async def _stop(self, handle: Any) -> Any:
        fn = getattr(handle, "stop", None) or getattr(handle, "request_stop", None)
        if not callable(fn):
            raise SdkOperationError("unsupported")
        return await _await(fn())

    async def _volume(
        self, volume_type: Any, name: str, workspace_id: uuid.UUID, quota: int,
        deadline: float, *, allow_create: bool = True,
    ) -> Any:
        quota_mib = max(1, (quota + 2**20 - 1) // 2**20)
        volume = await self._get(volume_type, name, deadline)
        if volume is None:
            if not allow_create:
                # An existing Workspace may only be repaired around its
                # verified named volume.  Creating an empty replacement here
                # would hide data loss behind a successful admission.
                raise SdkOperationError(
                    "not_found", stage="volume_reconcile", ownership_verified=False,
                )
            await self._call("volume_create", volume_type.create(name, quota_mib=quota_mib, labels=self._volume_labels(workspace_id, quota_mib=quota_mib)), deadline)
            log.info("volume lifecycle transition", extra={"event": "volume_create"})
            # The pinned SDK may return a transient operation handle without
            # labels or quota metadata. Validate the registry object, not the
            # create receipt, just as we do for Sandbox.create.
            volume = await self._get(volume_type, name, deadline)
            if volume is None:
                raise SdkOperationError("runtime_failure", stage="volume_create")
        self._validate_volume(volume, workspace_id, quota_mib)
        return volume

    async def ensure(self, workspace_id: uuid.UUID, config: dict[str, Any]) -> dict[str, Any]:
        Sandbox, Volume, Network = _sdk_types()
        deadline = self._deadline()
        quota, vcpus = int(config.get("quota_bytes", WORKSPACE_QUOTA_MIB * 2**20)), int(config.get("vcpus", 4))
        if not 0 < quota <= WORKSPACE_QUOTA_MIB * 2**20:
            raise SdkOperationError("unsupported", stage="quota_probe", evidence={"reason": "workspace_volume_quota_out_of_range"})
        memory_mib = max(128, int(config.get("memory_bytes", 8 * 1024**3) // 2**20))
        network = config.get("network") or {"mode": "off"}
        network_mode = network.get("mode", "off")
        require_root_quota = bool(config.get("require_writable_root_quota", False))
        root_quota_bytes = int(config.get("root_quota_bytes", WRITABLE_ROOT_QUOTA_MIB * 2**20))
        if require_root_quota and root_quota_bytes != WRITABLE_ROOT_QUOTA_MIB * 2**20:
            raise SdkOperationError(
                "unsupported", stage="quota_probe",
                evidence={
                    "requirement": "4GiB writable root ceiling",
                    "reason": "root_quota_not_4GiB",
                    "requested_bytes": root_quota_bytes,
                },
            )
        root_quota_mib = max(1, (root_quota_bytes + 2**20 - 1) // 2**20)
        image_source: Any = self.image
        tmpfs_mount: Any | None = None
        if require_root_quota:
            image_source = _managed_root_image(self.image, root_quota_mib)
            # /tmp is intentionally a separate, bounded, ephemeral mount.  It
            # does not enlarge the persistent root disk or /workspace volume.
            tmpfs_mount = _bounded_tmpfs_mount(Volume, TMPFS_SIZE_MIB)
        sdk_network_types = (
            _sdk_network_types()
            if network_mode in {"allowlist", "unrestricted_public"}
            else None
        )
        network_object = self._network_object(Network, network, sdk_network_types)
        canonical_network = json.dumps(network, sort_keys=True, separators=(",", ":"))
        network_rules_digest = hashlib.sha256(canonical_network.encode("utf-8")).hexdigest()
        self._configs[str(workspace_id)] = {
            "quota_mib": max(1, (quota + 2**20 - 1) // 2**20),
            "vcpus": vcpus, "memory_mib": memory_mib,
            "network_mode": network_mode, "network_rules_digest": network_rules_digest,
            "root_quota_bytes": root_quota_bytes if require_root_quota else None,
            "tmpfs_mib": TMPFS_SIZE_MIB if require_root_quota else None,
        }
        name, volume_name = self.names(workspace_id)
        # The broker service marks this internal flag false for an existing
        # durable row.  That prevents a missing volume from becoming a fresh
        # empty Workspace during repair.  Direct adapter callers retain the
        # first-creation default.
        allow_volume_create = bool(config.get("_allow_volume_create", True))
        await self._volume(Volume, volume_name, workspace_id, quota, deadline,
                           allow_create=allow_volume_create)

        create_kwargs = {
            "image": image_source, "pull_policy": _local_pull_policy(),
            "cpus": vcpus, "memory": memory_mib, "detached": True,
            "replace": False, "volumes": {"/workspace": Volume.named(volume_name)},
            "network": network_object,
            "labels": self._sandbox_labels(
                workspace_id, vcpus=vcpus, memory_mib=memory_mib,
                volume_name=volume_name, network_mode=network_mode,
                network_rules_digest=network_rules_digest,
                root_quota_bytes=root_quota_bytes if require_root_quota else None,
                tmpfs_mib=TMPFS_SIZE_MIB if require_root_quota else None,
            ),
        }
        if tmpfs_mount is not None:
            create_kwargs["volumes"]["/tmp"] = tmpfs_mount
        sandbox = await self._get(Sandbox, name, deadline)
        if sandbox is None:
            await self._call("sandbox_create", Sandbox.create(name, **create_kwargs), deadline)
            log.info("sandbox lifecycle transition", extra={"event": "sandbox_create"})
            # Sandbox.create may return a transient operation result or a
            # handle whose persisted config is not populated yet.  Reconcile
            # only the object re-fetched from the SDK registry below.
        else:
            # 13.0 (DESIGN-13.0-DOCKER-REWRITE.md section 8): `ensure` no
            # longer removes and recreates an existing sandbox on its own.
            # Superseded history: up to 12.x a `failed` sandbox was proven
            # broker-owned (`_validate_recovery_identity`), removed while its
            # named volume was kept (`_remove_sandbox_only`) and rebuilt
            # ("sandbox_recreate", reason "terminal"); a STOPPED sandbox whose
            # policy-controlled configuration no longer matched was replaced
            # the same way (reason "policy"), while a running one failed
            # closed because no one can prove a guest process has stopped
            # writing the durable volume.  A Workspace VM is disposable and
            # the user has an explicit Admin removal operation, so a broken or
            # stale VM now stays visible with a reason instead of being
            # silently rebuilt underneath its owner.  The policy comparison
            # itself is unchanged and still fails closed.
            state = self._status(sandbox)
            if state == "failed":
                log.info(
                    "sandbox left in place for explicit removal",
                    extra={"event": "sandbox_failed_retained", "status": state},
                )
                raise SdkOperationError(
                    "conflict", stage="sandbox_failed", ownership_verified=False,
                    evidence={"reason": "sandbox_failed_requires_explicit_removal", "status": state},
                )
            # Any other existing sandbox is validated by the unconditional
            # `_validate_config(raw, ...)` below, against the object re-fetched
            # from the SDK registry rather than this handle.
        raw = await self._get(Sandbox, name, deadline)
        if raw is None:
            raise SdkOperationError("not_found")
        self._validate_config(raw, workspace_id)
        connection = await self._call("sandbox_connect", self._connect_or_start(raw), deadline)
        self._connections[str(workspace_id)] = connection
        return await self.inspect(workspace_id)

    async def _connect_or_start(self, raw: Any) -> Any:
        fn = getattr(raw, "connect_or_start", None)
        if callable(fn):
            return await _await(fn())
        if "running" not in self._status(raw):
            start = getattr(raw, "start", None)
            if not callable(start):
                raise SdkOperationError("unsupported")
            await _await(start())
        connect = getattr(raw, "connect", None)
        if not callable(connect):
            raise SdkOperationError("unsupported")
        return await _await(connect())

    async def inspect(self, workspace_id: uuid.UUID) -> dict[str, Any]:
        Sandbox, Volume, _ = _sdk_types()
        deadline = self._deadline(30)
        name, volume_name = self.names(workspace_id)
        sandbox = await self._get(Sandbox, name, deadline)
        if sandbox is None:
            # A sandbox can disappear independently of its named volume (for
            # example, after an interrupted remove).  Do not collapse that
            # volume-only residual into ``absent``: the manager must prove
            # ownership and call remove so the broker, rather than metadata
            # cleanup, removes the surviving user data.
            volume = await self._get(Volume, volume_name, deadline)
            if volume is not None:
                self._validate_volume(volume, workspace_id)
                evidence = self._verified_volume_evidence(volume, workspace_id)
                usage = await self._volume_usage(volume, volume_name, evidence)
                return {
                    "state": "partial", "partial_state": "volume_only",
                    "sandbox_name": name, "volume_name": volume_name,
                    "runtime_id": None, "volume_id": evidence["volume_id"],
                    "host_path": evidence["host_path"],
                    "path_status": evidence["path_status"],
                    **usage,
                }
            return {
                "state": "absent", "sandbox_name": name, "volume_name": volume_name,
                "runtime_id": None, "volume_id": None,
                "measured_allocated_bytes": None, "measured_apparent_bytes": None,
                "host_path": None, "path_status": "absent", "usage_status": "unknown",
            }
        labels = self._validate_config(sandbox, workspace_id)
        state_text = self._status(sandbox)
        state = "running" if "running" in state_text else "stopped" if "stop" in state_text else "failed"
        volume = await self._get(Volume, volume_name, deadline)
        if volume is None:
            raise SdkOperationError("runtime_failure")
        self._validate_volume(volume, workspace_id)
        evidence = self._verified_volume_evidence(volume, workspace_id)
        usage = await self._volume_usage(volume, volume_name, evidence)
        return {
            "state": state,
            "sandbox_name": name,
            "volume_name": volume_name,
            "runtime_id": str(getattr(sandbox, "id", name)),
            "volume_id": evidence["volume_id"],
            "host_path": evidence["host_path"],
            "path_status": evidence["path_status"],
            **usage,
            "network_mode": labels["cognita.network"],
        }

    async def _volume_usage(self, volume: Any, volume_name: str, evidence: Mapping[str, Any]) -> dict[str, Any]:
        """Return ``measured_allocated_bytes`` / ``measured_apparent_bytes`` / ``usage_status``.

        13.0.2: the pinned SDK's ``VolumeHandle.used_bytes`` is an ``int``
        property that returns 0 for a directory volume — observed on the reference host
        against a volume holding 31 files (6,424 bytes by ``du -sb``).  Until
        13.0.1 that 0 was taken as a measurement, so every Workspace reported
        ``measured_apparent_bytes: 0`` beside ``usage_status: "measured"`` and
        the transfer quota check had nothing to check against.  A positive SDK
        figure is still trusted (a disk-image volume may report one); a zero is
        NOT evidence of an empty volume and falls through to a bounded stat
        walk of the volume's backing directory.  When neither source can
        measure, the status is ``unknown`` and both byte fields are ``None`` —
        never a fabricated 0.
        """
        allocated = evidence.get("measured_allocated_bytes")
        status = str(evidence.get("usage_status") or "unknown")
        used = await _await(getattr(volume, "used_bytes", None))
        try:
            apparent = int(used) if used is not None and int(used) > 0 else None
        except (TypeError, ValueError):
            apparent = None
        if apparent is None:
            backing = self._volume_backing_dir(volume_name)
            if backing is not None:
                walked = await asyncio.to_thread(_walk_usage, backing)
                if walked is not None:
                    apparent = walked[1]
                    if allocated is None:
                        allocated = walked[0]
        if apparent is not None:
            status = "measured"
        return {
            "measured_allocated_bytes": allocated,
            "measured_apparent_bytes": apparent,
            "usage_status": status,
        }

    def _volume_backing_dir(self, volume_name: str) -> Path | None:
        """The SDK-owned directory behind a directory volume, or ``None``.

        The pinned SDK exposes no path on its handle, so the path is derived
        from the data dir it was configured with and the exact name the SDK
        itself reports — then verified rather than trusted: the name must be
        one this adapter generates (``names()``), the entry must be a real
        directory (not a symlink) directly under ``<data dir>/volumes``, and
        the SDK's own lock file for that name must sit beside it, which is
        how the SDK registers the volume.  The result is used ONLY to stat
        entries for a byte count.  It is never reported as ``host_path`` and
        never addressed by a destructive operation (DESIGN-12.6.0 R3).
        """
        if not isinstance(volume_name, str) or not _VOLUME_NAME.fullmatch(volume_name):
            return None
        volumes = Path(self.volume_data_dir) / "volumes"
        candidate = volumes / volume_name
        lock = volumes / ".locks" / f"{volume_name}.lock"
        try:
            if not stat.S_ISDIR(os.lstat(candidate).st_mode):
                return None
            if not stat.S_ISREG(os.lstat(lock).st_mode):
                return None
        except OSError:
            return None
        return candidate

    async def _connected(self, workspace_id: uuid.UUID, *, start: bool, deadline: float) -> Any:
        Sandbox, Volume, _ = _sdk_types()
        connection_key = str(workspace_id)
        name, volume_name = self.names(workspace_id)
        raw = await self._get(Sandbox, name, deadline)
        if raw is None:
            self._connections.pop(connection_key, None)
            raise SdkOperationError("not_found")
        self._validate_config(raw, workspace_id)
        volume = await self._get(Volume, volume_name, deadline)
        if volume is None:
            raise SdkOperationError("runtime_failure", stage="reconcile")
        self._validate_volume(volume, workspace_id)
        cached = self._connections.get(connection_key)
        if cached is not None:
            status = self._status(raw)
            if not start or not any(marker in status for marker in ("stop", "fail")):
                return cached
            self._connections.pop(connection_key, None)
        if start:
            handle = await self._call("sandbox_connect_or_start", self._connect_or_start(raw), deadline)
        else:
            connect = getattr(raw, "connect", None)
            if not callable(connect):
                raise SdkOperationError("unsupported")
            handle = await self._call("sandbox_connect", connect(), deadline)
        self._connections[connection_key] = handle
        return handle

    async def start(self, workspace_id: uuid.UUID) -> dict[str, Any]:
        deadline = self._deadline()
        await self._connected(workspace_id, start=True, deadline=deadline)
        return await self.inspect(workspace_id)

    async def stop(self, workspace_id: uuid.UUID, *, force: bool = False) -> dict[str, Any]:
        deadline = self._deadline()
        handle = await self._connected(workspace_id, start=False, deadline=deadline)
        try:
            if force and callable(getattr(handle, "kill", None)):
                await self._call("sandbox_kill", handle.kill(), deadline)
            else:
                await self._call("sandbox_stop", self._stop(handle), deadline)
        finally:
            self._connections.pop(str(workspace_id), None)
        log.info("sandbox lifecycle transition", extra={"event": "sandbox_stop"})
        return await self.inspect(workspace_id)

    async def remove(self, workspace_id: uuid.UUID) -> dict[str, Any]:
        Sandbox, Volume, _ = _sdk_types()
        deadline = self._deadline()
        name, volume_name = self.names(workspace_id)
        self._connections.pop(str(workspace_id), None)
        raw = await self._get(Sandbox, name, deadline)
        volume = await self._get(Volume, volume_name, deadline)
        # Verify both objects before stopping or deleting either one. Names
        # alone are not ownership evidence, even for cached handles.
        if raw is not None:
            self._validate_config(raw, workspace_id)
        if volume is not None:
            self._validate_volume(volume, workspace_id)
        if raw is not None and "running" in self._status(raw):
            stop = getattr(raw, "stop", None)
            if not callable(stop):
                raise SdkOperationError("unsupported")
            await self._call("sandbox_stop", stop(), deadline)
        for obj in (raw, volume):
            if obj is None:
                continue
            fn = getattr(obj, "remove", None) or getattr(obj, "destroy", None)
            if not callable(fn):
                raise SdkOperationError("unsupported")
            await self._call("remove", fn(), deadline)
        if await self._get(Sandbox, name, deadline) is not None or await self._get(Volume, volume_name, deadline) is not None:
            raise SdkOperationError("runtime_failure", retryable=True)
        log.info("sandbox and volume lifecycle transition", extra={"event": "sandbox_remove"})
        return {"state": "absent", "sandbox_name": name, "volume_name": volume_name}

    @staticmethod
    def _result(result: Any) -> ExecResult:
        stdout = getattr(result, "stdout", getattr(result, "stdout_text", b""))
        stderr = getattr(result, "stderr", getattr(result, "stderr_text", b""))
        return ExecResult(stdout.encode() if isinstance(stdout, str) else bytes(stdout or b""), stderr.encode() if isinstance(stderr, str) else bytes(stderr or b""), int(getattr(result, "exit_code", getattr(result, "returncode", 1))))

    async def _exec(self, workspace_id: uuid.UUID, executable: str, argv: list[str], *, cwd: str, env: dict[str, str], deadline: float) -> ExecResult:
        handle = await self._connected(workspace_id, start=True, deadline=deadline)
        result = await self._call("guest_exec", handle.exec(executable, argv, cwd=cwd, env=env, timeout=max(1.0, deadline - time.monotonic())), deadline)
        return self._result(result)

    async def execute(self, workspace_id: uuid.UUID, operation: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if operation.startswith("fs_"):
            return await self._filesystem(workspace_id, operation, arguments)
        if operation.startswith("job_"):
            return await self._job(workspace_id, operation, arguments)
        raise NotImplementedError(operation)

    async def _read_all(self, fs: Any, path: str, deadline: float) -> bytes:
        """Read a complete file only for operations that require its contents."""
        return bytes(await self._call("fs_read", fs.read(path), deadline))

    async def _read(self, fs: Any, path: str, deadline: float) -> bytes:
        """Compatibility alias for internal callers that require full contents."""
        return await self._read_all(fs, path, deadline)

    async def _guest_owner(self, handle: Any, path: str, deadline: float) -> None:
        """Give a newly created guest path to the toolbox's unprivileged user."""
        result = await self._call(
            "fs_owner",
            handle.exec(
                "/usr/bin/chown", ["--no-dereference", "workspace:workspace", path],
                cwd="/workspace", user="root", env={},
                timeout=max(1.0, deadline - time.monotonic()),
            ),
            deadline,
        )
        if self._result(result).exit_code != 0:
            raise SdkOperationError("runtime_failure", stage="fs_owner")

    async def _mkdir_parents(
        self, fs: Any, path: str, deadline: float, *, handle: Any = None,
    ) -> None:
        """Create a guest directory tree through the SDK's one-argument API.

        Microsandbox 0.7.0 exposes ``mkdir(path)`` only; it does not accept a
        ``parents=`` keyword.  Walk the normalized absolute path explicitly so
        the broker does not depend on whether a future SDK implementation
        makes one ``mkdir`` call recursive.  Probe existing components first:
        SDK 0.7.0 reports an existing directory as an error instead of making
        ``mkdir`` idempotent.  Probe with ``exists`` on the one connected SDK
        handle cached by ``_connected`` and then verify present paths with
        ``stat``.  Repeatedly reconnecting the same persisted sandbox breaks
        the SDK agent stream, but both probes are stable on the cached handle.
        """
        stat = getattr(fs, "stat", None)
        exists = getattr(fs, "exists", None)
        if not callable(exists) or not callable(stat):
            raise SdkOperationError("unsupported", stage="fs_mkdir")
        current = PurePosixPath("/")
        for part in PurePosixPath(path).parts[1:]:
            current /= part
            candidate = str(current)
            present = await self._call("fs_exists", exists(candidate), deadline)
            if not isinstance(present, bool):
                raise SdkOperationError("runtime_failure", stage="fs_mkdir")
            if not present:
                await self._call("fs_mkdir", fs.mkdir(candidate), deadline)
                if handle is not None:
                    await self._guest_owner(handle, candidate, deadline)
                continue
            metadata = await self._call("fs_stat", stat(candidate), deadline)
            kind = getattr(metadata, "kind", getattr(metadata, "type", ""))
            kind = getattr(kind, "value", kind)
            if str(kind).casefold() not in {"dir", "directory"}:
                raise SdkOperationError("runtime_failure", stage="fs_mkdir")

    async def _read_range(
        self, fs: Any, path: str, offset: int, limit: int, deadline: float,
    ) -> bytes:
        """Read one bounded range without materializing the guest file."""
        read_stream = getattr(fs, "read_stream", None)
        if not callable(read_stream):
            raise SdkOperationError("unsupported", stage="fs_read")
        try:
            stream = await _await(read_stream(path))
            iterator = stream.__aiter__()
            remaining_skip = offset
            output = bytearray()
            async with asyncio.timeout(max(0.0, deadline - time.monotonic())):
                async for chunk in iterator:
                    if not isinstance(chunk, (bytes, bytearray, memoryview)):
                        raise SdkOperationError("runtime_failure", stage="fs_read")
                    chunk = bytes(chunk)
                    if remaining_skip:
                        if len(chunk) <= remaining_skip:
                            remaining_skip -= len(chunk)
                            continue
                        chunk = chunk[remaining_skip:]
                        remaining_skip = 0
                    if chunk:
                        output.extend(chunk[:limit - len(output)])
                    if len(output) >= limit:
                        break
            return bytes(output)
        except SdkOperationError:
            raise
        except TimeoutError as exc:
            raise SdkOperationError("timeout", retryable=False, stage="fs_read") from exc
        except Exception as exc:  # SDK stream errors are normalized at the boundary.
            category = self._category(exc)
            if category == "runtime_failure":
                # `_category` can only read the exception's CLASS NAME, and the
                # pinned SDK raises one generic error class for an absent read
                # target, so a missing file arrives here indistinguishable from
                # a broken runtime -- and the app maps everything it does not
                # recognize to `runtime_unavailable`. That is one observation
                # standing for two very different states: "your path is gone"
                # and "the Workspace runtime is down". Ask the guest which it
                # is, on the failure path only, so an ordinary read costs
                # nothing. If the probe itself fails, the original
                # runtime_failure stands, because that genuinely is a runtime
                # problem. Incident, 2026-09-22: a live self-test read of a
                # path cleaned up by an earlier run reported
                # `runtime_unavailable` and pointed the diagnosis at the
                # broker instead of at the path.
                try:
                    present = await self._call("fs_exists", fs.exists(path), deadline)
                except Exception:  # noqa: BLE001 - the probe is best-effort by design
                    present = True
                if present is False:
                    raise SdkOperationError("not_found", stage="fs_read") from exc
            raise SdkOperationError(
                category, retryable=category == "busy", stage="fs_read",
            ) from exc

    @staticmethod
    def _stream_tail(data: bytes, needle: bytes) -> bytes:
        overlap = len(needle) - 1
        return data[-overlap:] if overlap else b""

    async def _contains_streamed(
        self, fs: Any, path: str, needle: bytes, deadline: float,
    ) -> bool:
        """Search a file incrementally, retaining only a pattern-sized tail."""
        read_stream = getattr(fs, "read_stream", None)
        if not callable(read_stream):
            raise SdkOperationError("unsupported", stage="fs_search")
        try:
            stream = await _await(read_stream(path))
            iterator = stream.__aiter__()
            tail = b""
            async with asyncio.timeout(max(0.0, deadline - time.monotonic())):
                async for chunk in iterator:
                    if not isinstance(chunk, (bytes, bytearray, memoryview)):
                        raise SdkOperationError("runtime_failure", stage="fs_search")
                    data = tail + bytes(chunk)
                    if needle in data:
                        return True
                    tail = self._stream_tail(data, needle)
            return False
        except SdkOperationError:
            raise
        except TimeoutError as exc:
            raise SdkOperationError("timeout", retryable=False, stage="fs_search") from exc
        except Exception as exc:  # SDK stream errors are normalized at the boundary.
            category = self._category(exc)
            raise SdkOperationError(
                category, retryable=category == "busy", stage="fs_search",
            ) from exc

    @staticmethod
    def _listing_entry(item: Any) -> dict[str, Any]:
        if isinstance(item, dict):
            path = item.get("path", "")
            kind = item.get("kind", "file")
            size = item.get("size", 0)
        else:
            path = getattr(item, "path", item)
            kind = getattr(item, "kind", "file")
            size = getattr(item, "size", 0)
        return {
            "path": str(path),
            "kind": str(kind),
            "size": int(size or 0),
        }

    async def _bounded_guest_listing(
        self, handle: Any, path: str, recursive: bool, limit: int, deadline: float,
    ) -> tuple[list[dict[str, Any]], bool]:
        """Enumerate only ``limit + 1`` guest metadata records.

        The SDK's 0.7.0 ``fs.list`` API returns a complete collection and has
        no page-size argument.  A fixed Python helper performs bounded
        ``scandir`` traversal inside the guest, with no shell and no content
        reads, so a large directory cannot be materialized in the broker.
        """
        result = await self._call(
            "fs_list",
            handle.exec(
                "python3",
                [
                    "-c", _BOUNDED_LIST_HELPER, path,
                    "1" if recursive else "0", str(limit + 1),
                    str(MAX_ENUM_OUTPUT_BYTES),
                ],
                cwd="/workspace", env={},
                timeout=max(1.0, deadline - time.monotonic()),
            ),
            deadline,
        )
        exec_result = self._result(result)
        output = exec_result.stdout
        if len(output) > MAX_ENUM_OUTPUT_BYTES:
            raise SdkOperationError("runtime_failure", stage="fs_list")
        entries: list[dict[str, Any]] = []
        helper_truncated = len(output) >= MAX_ENUM_OUTPUT_BYTES
        helper_error: str | None = None
        try:
            for line in output.splitlines():
                entry = json.loads(line)
                if not isinstance(entry, dict):
                    raise ValueError
                if entry.get("_truncated") is True:
                    helper_truncated = True
                    continue
                if isinstance(entry.get("_error"), str):
                    helper_error = entry["_error"]
                    continue
                entries.append(self._listing_entry(entry))
        except (TypeError, ValueError, UnicodeDecodeError) as exc:
            raise SdkOperationError("runtime_failure", stage="fs_list") from exc
        if exec_result.exit_code != 0:
            if helper_error == "not_found":
                raise SdkOperationError("not_found", stage="fs_list")
            raise SdkOperationError("runtime_failure", stage="fs_list")
        return entries[:limit], helper_truncated or len(entries) > limit

    async def _remove_guest_path(
        self, fs: Any, path: str, *, recursive: bool, deadline: float,
    ) -> None:
        """Select the pinned SDK's file or recursive-directory primitive."""
        if not await self._call("fs_exists", fs.exists(path), deadline):
            raise SdkOperationError("not_found", stage="fs_remove")
        metadata = await self._call("fs_stat", fs.stat(path), deadline)
        kind = getattr(metadata, "kind", getattr(metadata, "type", ""))
        kind = str(getattr(kind, "value", kind)).casefold()
        if "dir" in kind:
            if not recursive:
                raise SdkOperationError("conflict", stage="fs_remove")
            method = getattr(fs, "remove_dir", None)
        else:
            method = getattr(fs, "remove", None) or getattr(fs, "remove_file", None)
        if not callable(method):
            raise SdkOperationError("unsupported", stage="fs_remove")
        await self._call("fs_remove", method(path), deadline)

    async def _filesystem(self, workspace_id: uuid.UUID, operation: str, arguments: dict[str, Any]) -> dict[str, Any]:
        # The gateway has a 30-second RPC read timeout. Leave room for
        # terminating a pathological regex worker and returning its error.
        search_timeout = min(arguments.get("timeout_seconds", 30), 25) if operation == "fs_search" else None
        deadline = self._deadline(search_timeout)
        handle = await self._connected(workspace_id, start=True, deadline=deadline)
        fs, path = handle.fs, arguments.get("path")
        if operation == "fs_read":
            offset, limit = arguments.get("offset", 0), arguments.get("max_bytes", 1024 * 1024)
            raw = await self._read_range(fs, path, offset, limit, deadline)
            binary = bool(arguments.get("binary"))
            return {"path": path, "content": base64.b64encode(raw).decode() if binary else raw.decode("utf-8", "replace"), "encoding": "base64" if binary else "text", "bytes": len(raw), "offset": offset, "next_offset": offset + len(raw)}
        if operation == "fs_write":
            raw = arguments.get("text", "").encode() if arguments.get("content_encoding") == "text" else base64.b64decode(arguments.get("base64", ""), validate=True)
            if "expected_sha256" in arguments:
                actual_sha256 = hashlib.sha256(await self._read_all(fs, path, deadline)).hexdigest()
                if actual_sha256 != arguments["expected_sha256"]:
                    raise SdkOperationError(
                        "conflict", stage="fs_write",
                        evidence={
                            "expected_sha256": arguments["expected_sha256"],
                            "actual_sha256": actual_sha256,
                        },
                    )
            if arguments.get("create_parents"):
                await self._mkdir_parents(fs, str(PurePosixPath(path).parent), deadline, handle=handle)
            await self._call("fs_write", fs.write(path, raw), deadline)
            await self._guest_owner(handle, path, deadline)
            return {"path": path, "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
        if operation == "fs_mkdir":
            if arguments.get("parents", False):
                await self._mkdir_parents(fs, path, deadline, handle=handle)
            else:
                await self._call("fs_mkdir", fs.mkdir(path), deadline)
                await self._guest_owner(handle, path, deadline)
            return {"path": path}
        if operation == "fs_stat":
            if not await self._call("fs_exists", fs.exists(path), deadline):
                raise SdkOperationError("not_found", stage="fs_stat")
            stat = await self._call("fs_stat", fs.stat(path), deadline)
            kind = str(getattr(stat, "kind", getattr(stat, "type", "file")))
            result = {"path": path, "kind": kind, "size": int(getattr(stat, "size", 0) or 0)}
            if arguments.get("include_hash") and "file" in kind:
                result["sha256"] = hashlib.sha256(await self._read_all(fs, path, deadline)).hexdigest()
            return result
        if operation == "fs_list":
            max_entries = arguments.get("max_entries", 200)
            entries, truncated = await self._bounded_guest_listing(
                handle, path, bool(arguments.get("recursive", False)), max_entries, deadline,
            )
            return {"path": path, "entries": entries, "truncated": truncated}
        if operation == "fs_search":
            import fnmatch
            pattern, mode, found = arguments["pattern"], arguments.get("mode", "glob"), []
            max_paths = arguments.get("max_paths", 2000)
            max_matches = arguments.get("max_matches", 10000)
            candidates: list[tuple[str, str]] = []
            paths_examined = 0
            for root in arguments["roots"]:
                remaining = max_paths - paths_examined
                entries, listing_truncated = await self._bounded_guest_listing(
                    handle, root, True, remaining, deadline,
                )
                for item in entries:
                    candidate = item["path"]
                    name = candidate.rsplit("/", 1)[-1]
                    kind = item["kind"]
                    paths_examined += 1
                    if mode == "regex":
                        candidates.append((candidate, kind))
                    elif mode == "glob":
                        if fnmatch.fnmatch(candidate, pattern) or fnmatch.fnmatch(name, pattern):
                            found.append({"path": candidate, "kind": kind})
                    elif "file" in kind.casefold() and await self._contains_streamed(fs, candidate, pattern.encode(), deadline):
                        found.append({"path": candidate, "kind": kind})
                    if len(found) >= max_matches:
                        return {"matches": found, "truncated": True}
                    if paths_examined >= max_paths:
                        if mode == "regex":
                            indices = await _bounded_regex_indices(pattern, [value[0] for value in candidates], max_matches, deadline)
                            found = [{"path": candidates[index][0], "kind": candidates[index][1]} for index in indices]
                        return {"matches": found, "truncated": True}
                if listing_truncated:
                    if mode == "regex":
                        indices = await _bounded_regex_indices(
                            pattern, [value[0] for value in candidates], max_matches, deadline,
                        )
                        found = [{"path": candidates[index][0], "kind": candidates[index][1]} for index in indices]
                    return {"matches": found, "truncated": True}
            if mode == "regex":
                indices = await _bounded_regex_indices(pattern, [value[0] for value in candidates], max_matches, deadline)
                found = [{"path": candidates[index][0], "kind": candidates[index][1]} for index in indices]
            return {"matches": found, "truncated": len(found) >= max_matches}
        if operation == "fs_edit":
            raw = await self._read_all(fs, path, deadline)
            if "expected_sha256" in arguments:
                actual_sha256 = hashlib.sha256(raw).hexdigest()
                if actual_sha256 != arguments["expected_sha256"]:
                    raise SdkOperationError(
                        "conflict", stage="fs_edit",
                        evidence={
                            "expected_sha256": arguments["expected_sha256"],
                            "actual_sha256": actual_sha256,
                        },
                    )
            text = raw.decode("utf-8")
            for edit in arguments["edits"]:
                match = edit.get("match")
                replacement = edit.get("replacement", "")
                if not isinstance(match, str) or text.count(match) != 1:
                    raise SdkOperationError("conflict", stage="fs_edit")
                text = text.replace(match, replacement, 1)
            data = text.encode()
            await self._call("fs_edit_write", fs.write(path, data), deadline)
            await self._guest_owner(handle, path, deadline)
            return {"path": path, "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}
        if operation in {"fs_copy", "fs_move"}:
            method = getattr(fs, "copy" if operation == "fs_copy" else "rename", None)
            if not callable(method):
                raise SdkOperationError("unsupported")
            policy = arguments.get("conflict_policy", "fail")
            destinations: list[str] = []
            for source in arguments["sources"]:
                if not await self._call("fs_exists", fs.exists(source), deadline):
                    raise SdkOperationError("not_found", stage=operation)
                target = arguments["destination"]
                # The SDK primitive intentionally receives one resolved target
                # per source; conflict policy remains broker-owned.
                if await self._call("fs_exists", fs.exists(target), deadline):
                    stat = await self._call("fs_stat", fs.stat(target), deadline)
                    exists = True
                    if "dir" in str(getattr(stat, "kind", getattr(stat, "type", ""))).casefold():
                        target = str(PurePosixPath(target) / PurePosixPath(source).name)
                        exists = await self._call("fs_exists", fs.exists(target), deadline)
                else:
                    exists = False
                if exists:
                    if policy == "fail":
                        raise SdkOperationError("conflict", stage=operation)
                    if policy == "skip":
                        destinations.append(target)
                        continue
                    if policy == "rename":
                        base, suffix = target, 1
                        while True:
                            candidate = f"{base} ({suffix})"
                            if not await self._call("fs_exists", fs.exists(candidate), deadline):
                                target = candidate
                                break
                            suffix += 1
                    elif policy == "replace":
                        # Removing an identical destination would delete the
                        # source before the SDK can copy or rename it.
                        if source == target:
                            raise SdkOperationError("conflict", stage=operation)
                        await self._remove_guest_path(fs, target, recursive=True, deadline=deadline)
                await self._call(operation, method(source, target), deadline)
                await self._guest_owner(handle, target, deadline)
                destinations.append(target)
            destination_hashes: dict[str, str] = {}
            for target in destinations:
                destination_hashes[target] = hashlib.sha256(
                    await self._read_all(fs, target, deadline)
                ).hexdigest()
            receipt: dict[str, Any] = {
                "sources": arguments["sources"],
                "destination": arguments["destination"],
                "destination_sha256": (
                    next(iter(destination_hashes.values()))
                    if len(destination_hashes) == 1 else destination_hashes
                ),
            }
            return receipt
        if operation == "fs_remove":
            for target in arguments["paths"]:
                expected = arguments.get("expected_hashes", {}).get(target)
                if expected and hashlib.sha256(await self._read_all(fs, target, deadline)).hexdigest() != expected:
                    raise SdkOperationError("conflict", stage="fs_remove")
                await self._remove_guest_path(fs, target, recursive=bool(arguments.get("recursive", False)), deadline=deadline)
            return {"paths": arguments["paths"]}
        if operation == "fs_lines":
            start = arguments.get("start_line", 0) or 0
            end = arguments.get("end_line", 0) or 0
            tail = arguments.get("tail_lines", 0) or 0
            max_bytes = arguments.get("max_bytes", 1024 * 1024)
            started = time.monotonic()
            result = await self._exec(
                workspace_id, "python3",
                ["-c", _FS_LINES_PROGRAM, path, str(start), str(end), str(tail), str(max_bytes)],
                cwd="/workspace", env={}, deadline=deadline,
            )
            elapsed_ms = int((time.monotonic() - started) * 1000)
            if result.exit_code != 0:
                log.info("workspace fs_lines exec", extra={
                    "event": "fs_lines", "elapsed_ms": elapsed_ms,
                    "exit_code": result.exit_code, "truncated": False,
                })
                if result.exit_code == 2:
                    raise SdkOperationError("not_found", stage="fs_lines")
                raise SdkOperationError("runtime_failure", stage="fs_lines")
            try:
                payload = json.loads(result.stdout.decode("utf-8"))
            except (UnicodeDecodeError, ValueError) as exc:
                log.info("workspace fs_lines exec", extra={
                    "event": "fs_lines", "elapsed_ms": elapsed_ms,
                    "exit_code": result.exit_code, "truncated": False,
                })
                raise SdkOperationError("runtime_failure", stage="fs_lines") from exc
            if not isinstance(payload, dict):
                log.info("workspace fs_lines exec", extra={
                    "event": "fs_lines", "elapsed_ms": elapsed_ms,
                    "exit_code": result.exit_code, "truncated": False,
                })
                raise SdkOperationError("runtime_failure", stage="fs_lines")
            truncated = bool(payload.get("has_more"))
            log.info("workspace fs_lines exec", extra={
                "event": "fs_lines", "elapsed_ms": elapsed_ms, "exit_code": 0,
                "bytes": payload.get("bytes"), "truncated": truncated,
            })
            return {"path": path, **payload}
        if operation == "fs_usage":
            entries, listing_truncated = await self._bounded_guest_listing(
                handle, path, False, FS_USAGE_LISTING_LIMIT, deadline,
            )
            if not entries:
                log.info("workspace fs_usage exec", extra={
                    "event": "fs_usage", "elapsed_ms": 0, "entry_count": 0,
                    "truncated": listing_truncated,
                    "total_bytes": None if listing_truncated else 0,
                })
                return {"entries": [], "truncated": listing_truncated,
                        "total_bytes": None if listing_truncated else 0}
            targets = [entry["path"] for entry in entries]
            started = time.monotonic()
            result = await self._exec(
                workspace_id, "du", ["-s", "--block-size=1", "--", *targets],
                cwd="/workspace", env={}, deadline=deadline,
            )
            elapsed_ms = int((time.monotonic() - started) * 1000)
            parsed: list[dict[str, Any]] = []
            # A clipped SDK stdout buffer can look like a successful prefix of
            # `du` output. Keep valid prefix rows for the bounded display, but
            # accept a total only when every target has one complete row.
            output_complete = bool(result.stdout.endswith(b"\n")) and len(result.stdout) < MAX_ENUM_OUTPUT_BYTES
            try:
                output_text = result.stdout.decode("utf-8")
            except UnicodeDecodeError:
                output_text = result.stdout.decode("utf-8", "replace")
                output_complete = False
            malformed = False
            for line in output_text.splitlines():
                if "\t" not in line:
                    malformed = True
                    continue
                size_text, entry_path = line.split("\t", 1)
                try:
                    size = int(size_text.strip())
                except ValueError:
                    malformed = True
                    continue
                if size < 0:
                    malformed = True
                    continue
                parsed.append({"path": entry_path, "bytes": size})
            # SDK volume usage may be incomplete, so `du` supplies the guest-side
            # measurement. Sum complete rows before the 20-entry display cap.
            # A total is valid only when every listed path is unique and matches
            # exactly one successful `du` row.
            listed_paths = set(targets)
            measured_paths = [item["path"] for item in parsed]
            complete = (
                not listing_truncated and result.exit_code == 0 and output_complete
                and not malformed and len(listed_paths) == len(targets)
                and len(measured_paths) == len(targets)
                and set(measured_paths) == listed_paths
            )
            total_bytes = sum(item["bytes"] for item in parsed) if complete else None
            parsed.sort(key=lambda item: item["bytes"], reverse=True)
            truncated = listing_truncated or len(parsed) > FS_USAGE_MAX_ENTRIES
            capped = parsed[:FS_USAGE_MAX_ENTRIES]
            log.info("workspace fs_usage exec", extra={
                "event": "fs_usage", "elapsed_ms": elapsed_ms, "exit_code": result.exit_code,
                "entry_count": len(capped), "truncated": truncated, "total_bytes": total_bytes,
            })
            return {"entries": capped, "truncated": truncated, "total_bytes": total_bytes}
        raise NotImplementedError(operation)

    async def _job(self, workspace_id: uuid.UUID, operation: str, arguments: dict[str, Any]) -> dict[str, Any]:
        job_id = str(arguments.get("job_id") or uuid.uuid4())
        if not _JOB_ID.fullmatch(job_id):
            raise SdkOperationError("unsupported")
        deadline = self._deadline(arguments.get("timeout_seconds", 30) if operation == "job_start" else 30)
        handle = await self._connected(workspace_id, start=True, deadline=deadline)
        directory = f"/workspace/.cognita/jobs/{job_id}"
        if operation == "job_start":
            await self._mkdir_parents(handle.fs, directory, deadline)
            # SDK fs.mkdir runs as guest root even though the toolbox executes
            # jobs as `workspace`. Without this ownership handoff, the job
            # supervisor cannot create status.tmp and every job_start fails.
            # Chown the exact broker-generated job directory only; do not
            # recursively change the user's Workspace tree or follow a final
            # symlink placed by a Workspace process.
            owner = await self._call(
                "job_directory_owner",
                handle.exec(
                    "/usr/bin/chown", ["--no-dereference", "workspace:workspace", directory],
                    cwd="/workspace", user="root", env={},
                    timeout=max(1.0, deadline - time.monotonic()),
                ),
                deadline,
            )
            if self._result(owner).exit_code != 0:
                raise SdkOperationError("runtime_failure", stage="job_directory_owner")
            descriptor = json.dumps({k: v for k, v in arguments.items() if k != "job_id"}, separators=(",", ":"), ensure_ascii=False).encode()
            await self._call("job_descriptor_write", handle.fs.write(f"{directory}/descriptor", descriptor), deadline)
            result = await self._exec(workspace_id, JOB_EXECUTABLE, ["start", job_id], cwd="/workspace", env={}, deadline=deadline)
            if result.exit_code != 0:
                raise SdkOperationError("runtime_failure")
            return {"job_id": job_id, "state": "running"}
        result = await self._exec(workspace_id, JOB_EXECUTABLE, ["cancel" if operation == "job_cancel" else "status", job_id], cwd="/workspace", env={}, deadline=deadline)
        if result.exit_code != 0:
            evidence: dict[str, Any] = {"exit_status": int(result.exit_code)}
            if operation == "job_cancel":
                try:
                    candidate = json.loads(result.stdout.decode("utf-8")) if result.stdout else {}
                except (UnicodeDecodeError, ValueError):
                    candidate = {}
                if isinstance(candidate, dict) and isinstance(candidate.get("state"), str):
                    evidence["guest_state"] = candidate["state"][:32]
            raise SdkOperationError(
                "not_found" if operation == "job_get" else "runtime_failure",
                stage=operation, evidence=evidence,
            )
        try:
            status = json.loads(result.stdout.decode("utf-8")) if result.stdout else {}
        except (UnicodeDecodeError, ValueError):
            status = {}
        if not isinstance(status, dict):
            status = {}
        status["job_id"] = job_id
        if operation == "job_cancel" and status.get("state") in {None, "queued", "running"}:
            raise SdkOperationError("runtime_failure")
        # tail_lines (DESIGN-12.18 SS3.3) is only ever present on job_get
        # (validation.py rejects it elsewhere); job_start/job_cancel arguments
        # never carry it, so this branch is a no-op there and every existing
        # field/value stays byte-for-byte identical to today when absent.
        tail_lines = arguments.get("tail_lines")
        terminal = status.get("state") in {"succeeded", "failed", "canceled", "timed_out", "lost"}
        stream_has_more: dict[str, bool] = {}
        for stream, offset_key in (("stdout", "stdout_offset"), ("stderr", "stderr_offset")):
            stream_path = f"{directory}/{stream}"
            # The monitor creates output files asynchronously. A status poll
            # may beat either file, and a successful job may never write to
            # stderr at all; both cases mean an empty stream, not a failure.
            exists = await self._call("fs_exists", handle.fs.exists(stream_path), deadline)
            raw = await self._read_all(handle.fs, stream_path, deadline) if exists else b""
            if tail_lines is not None:
                # Tail mode ignores stdout_offset/stderr_offset entirely and
                # always reports the retained file's end as next_offset, so a
                # caller cannot mix tail reads with offset-based polling on
                # the same stream.
                lines = raw.splitlines(keepends=True)
                tail_chunk = b"".join(lines[-tail_lines:]) if tail_lines < len(lines) else raw
                limit = int(arguments.get("max_bytes", 1024 * 1024))
                capped = len(tail_chunk) > limit
                # A caller-selected max_bytes smaller than the requested tail
                # is a safety cap only; keep the most recent bytes (matches
                # "tail") and do not try to realign it to a line boundary,
                # the same rule the byte-offset branch below applies to its
                # own max_bytes cap.
                chunk = tail_chunk[-limit:] if capped else tail_chunk
                first = max(0, len(raw) - 8 * 1024 * 1024)
                status[stream] = base64.b64encode(chunk).decode()
                status[f"{stream}_first_available_offset"] = first
                status[f"{stream}_next_offset"] = len(raw)
                status[f"{stream}_truncated"] = False
                stream_has_more[stream] = capped
                status[f"{stream}_has_more"] = capped
                if terminal:
                    status[f"{stream}_lines"] = raw.count(b"\n")
            else:
                offset, limit = int(arguments.get(offset_key, 0)), int(arguments.get("max_bytes", 1024 * 1024))
                first = max(0, len(raw) - 8 * 1024 * 1024)
                start = max(offset, first)
                chunk = raw[start:start + limit]
                status[stream] = base64.b64encode(chunk).decode()
                status[f"{stream}_first_available_offset"] = first
                status[f"{stream}_next_offset"] = start + len(chunk)
                status[f"{stream}_truncated"] = offset < first
                stream_has_more[stream] = start + len(chunk) < len(raw)
                status[f"{stream}_has_more"] = stream_has_more[stream]
        status["has_more"] = any(stream_has_more.values())
        return status

    async def copy_from_host(self, workspace_id: uuid.UUID, host_path: str, guest_path: str) -> None:
        """Import one staged file into the guest at ``guest_path``.

        13.2.5 (DESIGN-13.2-CONNECTOR-DIAGNOSTICS §4.1): the guest parent
        directory is created first, through the same component-wise
        ``_mkdir_parents`` that ``fs_write(create_parents=True)`` uses. The SDK's
        ``copy_from_host`` does not create it, so a transfer into any
        subdirectory (``docs/guide.md``) failed with ``FilesystemError`` while a
        flat file worked — the bridge ships the directory list on the wire and
        nothing here read it. The probe-first walk keeps this idempotent for a
        parent that already exists.
        """
        deadline = self._deadline()
        handle = await self._connected(workspace_id, start=True, deadline=deadline)
        method = getattr(handle.fs, "copy_from_host", None)
        if not callable(method):
            raise SdkOperationError("unsupported")
        parent = str(PurePosixPath(guest_path).parent)
        try:
            await self._mkdir_parents(handle.fs, parent, deadline, handle=handle)
            await self._call("transfer_import", method(host_path, guest_path), deadline)
        except SdkOperationError as exc:
            # Bounded context only: how deep the guest path is and whether the
            # staged host file was there to copy. Never the paths themselves.
            log.warning(
                "transfer import failed stage=%s category=%s guest_depth=%d host_file_present=%s",
                exc.stage, exc.category, len(PurePosixPath(guest_path).parts) - 1,
                os.path.isfile(host_path),
            )
            raise
        await self._guest_owner(handle, guest_path, deadline)
        log.info("transfer lifecycle transition", extra={"event": "transfer_import"})

    async def copy_to_host(self, workspace_id: uuid.UUID, guest_path: str, host_path: str) -> None:
        deadline = self._deadline()
        handle = await self._connected(workspace_id, start=False, deadline=deadline)
        method = getattr(handle.fs, "copy_to_host", None)
        if not callable(method):
            raise SdkOperationError("unsupported")
        await self._call("transfer_export", method(guest_path, host_path), deadline)
        log.info("transfer lifecycle transition", extra={"event": "transfer_export"})
