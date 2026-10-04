"""Defense-in-depth validation for the private runtime broker.

The public Workspace tools have their own schemas.  The broker repeats the
important limits because it is a separate security boundary and must not trust
the gateway to have performed validation correctly.
"""

from __future__ import annotations

import base64
import ipaddress
import posixpath
import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Mapping

from .protocol import BrokerOperation

MAX_PATH_BYTES = 4096
MAX_COMPONENT_BYTES = 255
MAX_DIRECT_FILE_BYTES = 1024 * 1024
MAX_LIST_ENTRIES = 2_000
MAX_COPY_PATHS = 1_000
MAX_SEARCH_ROOTS = 16
MAX_SEARCH_PATTERN_BYTES = 4096
MAX_SEARCH_PATHS = 2_000
MAX_SEARCH_MATCHES = 10_000
MAX_SEARCH_TIMEOUT_SECONDS = 30
MAX_JOB_ARGV = 256
MAX_JOB_ARG_BYTES = 256 * 1024
MAX_JOB_ENV_KEYS = 128
MAX_JOB_ENV_BYTES = 256 * 1024
MAX_JOB_TIMEOUT_SECONDS = 3_600
MAX_PROCESS_LIMIT = 256
MAX_FD_LIMIT = 4_096
MAX_STDOUT_BYTES = 8 * 1024 * 1024
DEFAULT_QUOTA_BYTES = 4 * 1024**3
DEFAULT_ROOT_QUOTA_BYTES = 4 * 1024**3
DEFAULT_VCPUS = 4
DEFAULT_MEMORY_BYTES = 8 * 1024**3

_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_ENV_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_SHA256 = re.compile(r"^[0-9a-fA-F]{64}$")


class ArgumentError(ValueError):
    """An RPC argument cannot be admitted by the broker."""


def _string(value: Any, name: str, *, max_bytes: int | None = None) -> str:
    if not isinstance(value, str):
        raise ArgumentError(f"{name} must be a string")
    if _CONTROL.search(value):
        raise ArgumentError(f"{name} contains control characters")
    if max_bytes is not None and len(value.encode("utf-8")) > max_bytes:
        raise ArgumentError(f"{name} is too long")
    if unicodedata.normalize("NFC", value) != value:
        raise ArgumentError(f"{name} must use NFC Unicode normalization")
    return value


def normalize_guest_path(value: Any, *, allow_root: bool = True) -> str:
    """Return a canonical guest path, never a host path.

    Relative paths are interpreted beneath ``/workspace``.  We deliberately
    reject rather than normalize ``..`` or empty components so a caller cannot
    hide a traversal or ambiguous Unicode spelling in an apparently valid path.
    """

    path = _string(value, "path", max_bytes=MAX_PATH_BYTES)
    if "\\" in path or path.startswith("//"):
        raise ArgumentError("path must be a POSIX Workspace path")
    if path == "/workspace":
        if not allow_root:
            raise ArgumentError("Workspace root is not valid for this operation")
        return path
    if path.startswith("/"):
        if not path.startswith("/workspace/"):
            raise ArgumentError("path escapes the Workspace")
        relative = path[len("/workspace/") :]
    else:
        relative = path
    parts = relative.split("/")
    if not parts or any(
        not part or part in {".", ".."} or len(part.encode("utf-8")) > MAX_COMPONENT_BYTES
        for part in parts
    ):
        raise ArgumentError("path contains an empty or traversal component")
    normalized = posixpath.join("/workspace", *parts)
    if normalized == "/workspace" and not allow_root:
        raise ArgumentError("Workspace root is not valid for this operation")
    if not normalized.startswith("/workspace/") and normalized != "/workspace":
        raise ArgumentError("path escapes the Workspace")
    return normalized


def _strict_keys(arguments: Mapping[str, Any], allowed: set[str]) -> None:
    unknown = set(arguments) - allowed
    if unknown:
        raise ArgumentError("unknown operation argument")


def _bool(arguments: Mapping[str, Any], key: str, default: bool = False) -> bool:
    value = arguments.get(key, default)
    if not isinstance(value, bool):
        raise ArgumentError(f"{key} must be a boolean")
    return value


def _bounded_int(arguments: Mapping[str, Any], key: str, default: int, low: int, high: int) -> int:
    value = arguments.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ArgumentError(f"{key} is outside the supported range")
    return value


def _sha(value: Any, key: str) -> str:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise ArgumentError(f"{key} must be a SHA-256 digest")
    return value.lower()


def _content(arguments: Mapping[str, Any]) -> tuple[str, str]:
    has_text = "text" in arguments
    has_base64 = "base64" in arguments
    if has_text == has_base64:
        raise ArgumentError("exactly one content representation is required")
    if has_text:
        text = arguments["text"]
        if not isinstance(text, str) or "\x00" in text or len(text.encode("utf-8")) > MAX_DIRECT_FILE_BYTES:
            raise ArgumentError("text content is invalid or too large")
        return "text", text
    encoded = arguments["base64"]
    if not isinstance(encoded, str):
        raise ArgumentError("base64 content must be a string")
    try:
        decoded = base64.b64decode(encoded, validate=True)
    except (ValueError, base64.binascii.Error) as exc:
        raise ArgumentError("base64 content is invalid") from exc
    if len(decoded) > MAX_DIRECT_FILE_BYTES:
        raise ArgumentError("direct file content exceeds the broker limit")
    return "base64", encoded


def validate_filesystem_arguments(operation: BrokerOperation, raw: Mapping[str, Any]) -> dict[str, Any]:
    """Validate and canonicalize one filesystem operation's arguments."""

    arguments = dict(raw)
    if operation is BrokerOperation.FS_LIST:
        _strict_keys(arguments, {"path", "recursive", "max_entries"})
        return {"path": normalize_guest_path(arguments.get("path", "/workspace")),
                "recursive": _bool(arguments, "recursive"),
                "max_entries": _bounded_int(arguments, "max_entries", 200, 1, MAX_LIST_ENTRIES)}
    if operation is BrokerOperation.FS_STAT:
        _strict_keys(arguments, {"path", "include_hash"})
        return {"path": normalize_guest_path(arguments.get("path")),
                "include_hash": _bool(arguments, "include_hash")}
    if operation is BrokerOperation.FS_READ:
        _strict_keys(arguments, {"path", "offset", "max_bytes", "binary"})
        return {"path": normalize_guest_path(arguments.get("path")),
                "offset": _bounded_int(arguments, "offset", 0, 0, 2**63 - 1),
                "max_bytes": _bounded_int(arguments, "max_bytes", MAX_DIRECT_FILE_BYTES,
                                           1, MAX_DIRECT_FILE_BYTES),
                "binary": _bool(arguments, "binary")}
    if operation is BrokerOperation.FS_WRITE:
        _strict_keys(arguments, {"path", "text", "base64", "create_parents", "expected_sha256"})
        result = {"path": normalize_guest_path(arguments.get("path"), allow_root=False),
                  "create_parents": _bool(arguments, "create_parents")}
        result["content_encoding"], value = _content(arguments)
        result["text" if result["content_encoding"] == "text" else "base64"] = value
        if "expected_sha256" in arguments:
            result["expected_sha256"] = _sha(arguments["expected_sha256"], "expected_sha256")
        return result
    if operation is BrokerOperation.FS_EDIT:
        _strict_keys(arguments, {"path", "edits", "expected_sha256"})
        edits = arguments.get("edits")
        if not isinstance(edits, list) or not edits or len(edits) > 256:
            raise ArgumentError("edits must contain between 1 and 256 items")
        normalized: list[dict[str, str]] = []
        for edit in edits:
            if not isinstance(edit, dict) or set(edit) != {"match", "replacement"}:
                raise ArgumentError("each edit must contain match and replacement")
            match, replacement = edit["match"], edit["replacement"]
            if (
                not isinstance(match, str) or not isinstance(replacement, str)
                or "\x00" in match or "\x00" in replacement
                or len(match.encode("utf-8")) > MAX_DIRECT_FILE_BYTES
                or len(replacement.encode("utf-8")) > MAX_DIRECT_FILE_BYTES
            ):
                raise ArgumentError("edit content is invalid or too large")
            normalized.append({"match": match, "replacement": replacement})
        result = {"path": normalize_guest_path(arguments.get("path"), allow_root=False),
                  "edits": normalized}
        if "expected_sha256" in arguments:
            result["expected_sha256"] = _sha(arguments["expected_sha256"], "expected_sha256")
        return result
    if operation is BrokerOperation.FS_MKDIR:
        _strict_keys(arguments, {"path", "parents"})
        return {"path": normalize_guest_path(arguments.get("path"), allow_root=False),
                "parents": _bool(arguments, "parents")}
    if operation in {BrokerOperation.FS_COPY, BrokerOperation.FS_MOVE}:
        _strict_keys(arguments, {"sources", "destination", "conflict_policy"})
        sources = arguments.get("sources")
        if not isinstance(sources, list) or not 1 <= len(sources) <= MAX_COPY_PATHS:
            raise ArgumentError("sources must contain between 1 and 1000 paths")
        paths = [normalize_guest_path(item, allow_root=False) for item in sources]
        if len(set(paths)) != len(paths):
            raise ArgumentError("sources must be unique")
        destination = normalize_guest_path(arguments.get("destination"))
        policy = arguments.get("conflict_policy", "fail")
        if policy not in {"fail", "skip", "replace", "rename"}:
            raise ArgumentError("invalid conflict policy")
        return {"sources": paths, "destination": destination, "conflict_policy": policy}
    if operation is BrokerOperation.FS_REMOVE:
        _strict_keys(arguments, {"paths", "recursive", "expected_hashes"})
        paths = arguments.get("paths")
        if not isinstance(paths, list) or not 1 <= len(paths) <= MAX_COPY_PATHS:
            raise ArgumentError("paths must contain between 1 and 1000 paths")
        normalized_paths = [normalize_guest_path(item, allow_root=False) for item in paths]
        expected = arguments.get("expected_hashes", {})
        if not isinstance(expected, dict) or len(expected) > MAX_COPY_PATHS:
            raise ArgumentError("expected_hashes is invalid")
        hashes = {normalize_guest_path(key, allow_root=False): _sha(value, "expected hash")
                  for key, value in expected.items()}
        return {"paths": normalized_paths, "recursive": _bool(arguments, "recursive"),
                "expected_hashes": hashes}
    if operation is BrokerOperation.FS_SEARCH:
        _strict_keys(arguments, {"roots", "mode", "pattern", "max_paths", "max_matches", "timeout_seconds"})
        roots = arguments.get("roots", ["/workspace"])
        if not isinstance(roots, list) or not 1 <= len(roots) <= MAX_SEARCH_ROOTS:
            raise ArgumentError("roots must contain between 1 and 16 paths")
        mode = arguments.get("mode", "glob")
        if mode not in {"glob", "text", "regex"}:
            raise ArgumentError("invalid search mode")
        pattern = _string(arguments.get("pattern", ""), "pattern", max_bytes=MAX_SEARCH_PATTERN_BYTES)
        if not pattern:
            raise ArgumentError("search pattern is required")
        if mode == "regex":
            try:
                re.compile(pattern)
            except re.error as exc:
                raise ArgumentError("search pattern is invalid") from exc
        return {"roots": [normalize_guest_path(item) for item in roots], "mode": mode,
                "pattern": pattern,
                "max_paths": _bounded_int(arguments, "max_paths", 2_000, 1, MAX_SEARCH_PATHS),
                "max_matches": _bounded_int(arguments, "max_matches", 10_000, 1, MAX_SEARCH_MATCHES),
                "timeout_seconds": _bounded_int(arguments, "timeout_seconds", 30, 1,
                                                MAX_SEARCH_TIMEOUT_SECONDS)}
    if operation is BrokerOperation.FS_LINES:
        _strict_keys(arguments, {"path", "start_line", "end_line", "tail_lines", "max_bytes"})
        has_start = "start_line" in arguments
        has_end = "end_line" in arguments
        has_tail = "tail_lines" in arguments
        has_range = has_start or has_end
        # Exactly one selection mode: a line range (start_line and/or
        # end_line) XOR tail_lines. Neither given, or both given together,
        # is rejected the same way (has_range == has_tail is True in both
        # of those cases and False only when exactly one mode was chosen).
        if has_range == has_tail:
            raise ArgumentError("fs_lines requires exactly one of a line range or tail_lines")
        result: dict[str, Any] = {"path": normalize_guest_path(arguments.get("path"))}
        if has_tail:
            result["tail_lines"] = _bounded_int(arguments, "tail_lines", 1, 1, 10_000)
        else:
            if has_start:
                result["start_line"] = _bounded_int(arguments, "start_line", 1, 1, 2**63 - 1)
            if has_end:
                result["end_line"] = _bounded_int(arguments, "end_line", 1, 1, 2**63 - 1)
            if has_start and has_end and result["end_line"] < result["start_line"]:
                raise ArgumentError("end_line must not be before start_line")
        result["max_bytes"] = _bounded_int(arguments, "max_bytes", MAX_DIRECT_FILE_BYTES,
                                           1, MAX_DIRECT_FILE_BYTES)
        return result
    if operation is BrokerOperation.FS_USAGE:
        _strict_keys(arguments, {"path"})
        path = normalize_guest_path(arguments.get("path"))
        if path != "/workspace":
            raise ArgumentError("fs_usage path must be the Workspace root")
        return {"path": path}
    raise ArgumentError("unsupported filesystem operation")


@dataclass(frozen=True)
class ResourcePolicy:
    quota_bytes: int = DEFAULT_QUOTA_BYTES
    vcpus: int = DEFAULT_VCPUS
    memory_bytes: int = DEFAULT_MEMORY_BYTES
    max_processes: int = MAX_PROCESS_LIMIT
    max_file_descriptors: int = MAX_FD_LIMIT
    sync_timeout_seconds: int = 600
    async_timeout_seconds: int = 3_600
    stdout_limit: int = MAX_STDOUT_BYTES
    stderr_limit: int = MAX_STDOUT_BYTES

    def __post_init__(self) -> None:
        if self.quota_bytes <= 0 or self.vcpus <= 0 or self.memory_bytes <= 0:
            raise ValueError("resource values must be positive")
        if not 1 <= self.max_processes <= MAX_PROCESS_LIMIT:
            raise ValueError("process limit exceeds broker maximum")
        if not 1 <= self.max_file_descriptors <= MAX_FD_LIMIT:
            raise ValueError("file descriptor limit exceeds broker maximum")
        if not 1 <= self.sync_timeout_seconds <= self.async_timeout_seconds <= MAX_JOB_TIMEOUT_SECONDS:
            raise ValueError("job timeout bounds are invalid")
        if self.stdout_limit < 1 or self.stderr_limit < 1:
            raise ValueError("output limits must be positive")


def validate_job_arguments(raw: Mapping[str, Any], policy: ResourcePolicy | None = None) -> dict[str, Any]:
    policy = policy or ResourcePolicy()
    arguments = dict(raw)
    _strict_keys(arguments, {"argv", "shell_script", "cwd", "env", "timeout_seconds", "async",
                             "process_limit", "file_descriptor_limit"})
    has_argv, has_script = "argv" in arguments, "shell_script" in arguments
    if has_argv == has_script:
        raise ArgumentError("exactly one of argv or shell_script is required")
    if has_argv:
        argv = arguments["argv"]
        if not isinstance(argv, list) or not 1 <= len(argv) <= MAX_JOB_ARGV:
            raise ArgumentError("argv must contain between 1 and 256 arguments")
        # argv elements are data passed directly to exec, not paths, env keys,
        # or shell source. POSIX only forbids NUL inside an argument; newlines,
        # tabs, and other bytes must remain intact for ordinary program input.
        if any(not isinstance(item, str) or "\x00" in item for item in argv):
            raise ArgumentError("argv contains an invalid argument")
        if sum(len(item.encode("utf-8")) for item in argv) > MAX_JOB_ARG_BYTES:
            raise ArgumentError("argv is too large")
        command = {"argv": argv}
    else:
        script = arguments["shell_script"]
        if not isinstance(script, str) or "\x00" in script or len(script.encode("utf-8")) > MAX_JOB_ARG_BYTES:
            raise ArgumentError("shell_script is invalid or too large")
        command = {"shell_script": script}
    env = arguments.get("env", {})
    if not isinstance(env, dict) or len(env) > MAX_JOB_ENV_KEYS:
        raise ArgumentError("env contains too many keys")
    env_bytes = 0
    normalized_env: dict[str, str] = {}
    for key, value in env.items():
        if not isinstance(key, str) or not _ENV_KEY.fullmatch(key) or not isinstance(value, str):
            raise ArgumentError("env has an invalid key or value")
        if "\x00" in value:
            raise ArgumentError("env contains a NUL character")
        env_bytes += len(key.encode()) + len(value.encode())
        normalized_env[key] = value
    if env_bytes > MAX_JOB_ENV_BYTES:
        raise ArgumentError("env is too large")
    asynchronous = arguments.get("async", True)
    if not isinstance(asynchronous, bool):
        raise ArgumentError("async must be a boolean")
    timeout = _bounded_int(arguments, "timeout_seconds", policy.async_timeout_seconds if asynchronous
                           else policy.sync_timeout_seconds, 1, MAX_JOB_TIMEOUT_SECONDS)
    if timeout > (policy.async_timeout_seconds if asynchronous else policy.sync_timeout_seconds):
        raise ArgumentError("timeout exceeds the configured job limit")
    cwd = normalize_guest_path(arguments.get("cwd", "/workspace"))
    process_limit = _bounded_int(arguments, "process_limit", policy.max_processes, 1, policy.max_processes)
    fd_limit = _bounded_int(arguments, "file_descriptor_limit", policy.max_file_descriptors, 1,
                            policy.max_file_descriptors)
    result = {**command, "cwd": cwd, "env": normalized_env, "timeout_seconds": timeout,
              "async": asynchronous, "process_limit": process_limit,
              "file_descriptor_limit": fd_limit}
    return result


def normalize_rpc_arguments(operation: BrokerOperation, raw: Mapping[str, Any],
                            policy: ResourcePolicy | None = None) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise ArgumentError("arguments must be an object")
    if operation in {BrokerOperation.FS_LIST, BrokerOperation.FS_STAT, BrokerOperation.FS_READ,
                     BrokerOperation.FS_WRITE, BrokerOperation.FS_EDIT, BrokerOperation.FS_MKDIR,
                     BrokerOperation.FS_COPY, BrokerOperation.FS_MOVE, BrokerOperation.FS_REMOVE,
                     BrokerOperation.FS_SEARCH, BrokerOperation.FS_LINES, BrokerOperation.FS_USAGE}:
        return validate_filesystem_arguments(operation, raw)
    if operation is BrokerOperation.JOB_START:
        return validate_job_arguments(raw, policy)
    if operation is BrokerOperation.JOB_GET:
        _strict_keys(raw, {"job_id", "stdout_offset", "stderr_offset", "max_bytes", "tail_lines"})
        job_id = _string(raw.get("job_id"), "job_id", max_bytes=80)
        result = {"job_id": job_id,
                  "stdout_offset": _bounded_int(raw, "stdout_offset", 0, 0, 2**63 - 1),
                  "stderr_offset": _bounded_int(raw, "stderr_offset", 0, 0, 2**63 - 1),
                  "max_bytes": _bounded_int(raw, "max_bytes", 1024 * 1024, 1, 1024 * 1024)}
        if "tail_lines" in raw:
            result["tail_lines"] = _bounded_int(raw, "tail_lines", 1, 1, 10_000)
        return result
    if operation is BrokerOperation.JOB_CANCEL:
        _strict_keys(raw, {"job_id"})
        return {"job_id": _string(raw.get("job_id"), "job_id", max_bytes=80)}
    if operation in {BrokerOperation.ENSURE, BrokerOperation.INSPECT, BrokerOperation.START,
                     BrokerOperation.STOP, BrokerOperation.REMOVE}:
        _strict_keys(raw, {"network", "quota_bytes", "vcpus", "memory_bytes",
                           "max_processes", "max_file_descriptors", "force",
                           "require_writable_root_quota", "root_quota_bytes",
                           "create_volume_if_absent"})
        policy = policy or ResourcePolicy()
        result: dict[str, Any] = {}
        if "network" in raw:
            from .network import NetworkPolicy
            try:
                network = NetworkPolicy.from_mapping(raw["network"])
            except (TypeError, ValueError) as exc:
                raise ArgumentError("network policy is invalid") from exc
            result["network"] = network.to_mapping()
        if "quota_bytes" in raw:
            result["quota_bytes"] = _bounded_int(raw, "quota_bytes", policy.quota_bytes, 1, policy.quota_bytes)
        if "root_quota_bytes" in raw:
            result["root_quota_bytes"] = _bounded_int(
                raw, "root_quota_bytes", DEFAULT_ROOT_QUOTA_BYTES,
                DEFAULT_ROOT_QUOTA_BYTES, DEFAULT_ROOT_QUOTA_BYTES,
            )
        if "vcpus" in raw:
            result["vcpus"] = _bounded_int(raw, "vcpus", policy.vcpus, 1, policy.vcpus)
        if "memory_bytes" in raw:
            result["memory_bytes"] = _bounded_int(raw, "memory_bytes", policy.memory_bytes, 1, policy.memory_bytes)
        if "max_processes" in raw:
            result["max_processes"] = _bounded_int(raw, "max_processes", policy.max_processes, 1, policy.max_processes)
        if "max_file_descriptors" in raw:
            result["max_file_descriptors"] = _bounded_int(raw, "max_file_descriptors",
                                                           policy.max_file_descriptors, 1,
                                                           policy.max_file_descriptors)
        if "force" in raw:
            result["force"] = _bool(raw, "force")
        if operation is BrokerOperation.ENSURE:
            result["create_volume_if_absent"] = (
                _bool(raw, "create_volume_if_absent") if "create_volume_if_absent" in raw else False
            )
            # Keep the complete resource contract in the broker-to-SDK request;
            # the adapter must not silently fall back to tiny SDK defaults.
            result.setdefault("quota_bytes", policy.quota_bytes)
            result.setdefault("root_quota_bytes", DEFAULT_ROOT_QUOTA_BYTES)
            result.setdefault("vcpus", policy.vcpus)
            result.setdefault("memory_bytes", policy.memory_bytes)
            result.setdefault("max_processes", policy.max_processes)
            result.setdefault("max_file_descriptors", policy.max_file_descriptors)
            # A named-volume quota does not prove the writable guest root is
            # bounded. Production callers therefore receive explicit
            # release-blocker evidence until the pinned SDK proves it.
            result.setdefault("require_writable_root_quota", True)
        elif "require_writable_root_quota" in raw:
            result["require_writable_root_quota"] = _bool(raw, "require_writable_root_quota")
        return result
    return dict(raw)


def is_forbidden_ip(address: str) -> bool:
    try:
        value = ipaddress.ip_address(address)
    except ValueError:
        return True
    return value.is_private or value.is_loopback or value.is_link_local or value.is_multicast \
        or value.is_unspecified or value.is_reserved
