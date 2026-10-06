"""Bounded structural catalog and exact ranged project-file reads."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import stat
import time
from pathlib import Path, PurePosixPath
from typing import Any, Callable

MAX_LIST_LIMIT = 500
DEFAULT_LIST_LIMIT = 100
MAX_INVENTORY_ENTRIES = 100_000
INVENTORY_BUDGET_SECONDS = 5.0
DEFAULT_READ_BYTES = 262_144
MAX_READ_BYTES = 1_048_576


class ProjectFileError(ValueError):
    def __init__(self, reason: str, message: str, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.reason = reason
        self.details = details or {}


def normalize_project_path(value: str, *, allow_root: bool = False) -> str:
    if not isinstance(value, str) or "\x00" in value or "\\" in value:
        raise ProjectFileError("validation_failed", "path must use normalized project-relative syntax")
    if value == "" and allow_root:
        return ""
    pure = PurePosixPath(value)
    if pure.is_absolute() or any(part in {"", ".", ".."} for part in pure.parts):
        raise ProjectFileError("validation_failed", "path must stay within the project root")
    normalized = pure.as_posix()
    if normalized != value:
        raise ProjectFileError("validation_failed", "path must be normalized")
    return normalized


def _rooted_path(root: Path, relative: str, *, allow_root: bool = False) -> tuple[Path, str]:
    normalized = normalize_project_path(relative, allow_root=allow_root)
    root = Path(root).resolve(strict=True)
    target = root if not normalized else root.joinpath(*PurePosixPath(normalized).parts)
    try:
        relative_to_root = target.resolve(strict=False).relative_to(root)
    except ValueError as exc:
        raise ProjectFileError("permission_denied", "path resolves outside the project root") from exc
    # Refuse links in every component, including a link which resolves back
    # inside the project. Listing exposes the link itself without traversing it.
    current = root
    for part in PurePosixPath(normalized).parts if normalized else ():
        current = current / part
        try:
            if current.is_symlink():
                raise ProjectFileError("permission_denied", "symbolic links are not followed")
        except OSError as exc:
            raise ProjectFileError("permission_denied", "path cannot be inspected safely") from exc
    return target, normalized


def _is_protected_storage_path(path: str) -> bool:
    return path == ".cognita-storage" or path.startswith(".cognita-storage/")


def _folder_decision(path: str, rules: tuple[tuple[str, bool], ...]) -> tuple[bool, str | None]:
    applicable = [
        (rule_path, indexed)
        for rule_path, indexed in rules
        if rule_path == "" or path == rule_path or path.startswith(rule_path + "/")
    ]
    exclusions = [(rule_path, indexed) for rule_path, indexed in applicable if not indexed]
    if exclusions:
        rule_path = max(exclusions, key=lambda item: len(item[0]))[0]
        return False, f"folder_rule:{rule_path}"
    return True, None


def _cursor_encode(payload: dict[str, Any]) -> str:
    body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(body).rstrip(b"=").decode("ascii")


def _cursor_decode(cursor: str) -> dict[str, Any]:
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        value = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProjectFileError("invalid_cursor", "cursor is invalid; restart the listing") from exc
    if not isinstance(value, dict) or value.get("version") != 1:
        raise ProjectFileError("invalid_cursor", "cursor is invalid; restart the listing")
    return value


def _walk_inventory(root: Path, start: Path, prefix: str, recursive: bool) -> list[dict[str, Any]]:
    deadline = time.monotonic() + INVENTORY_BUDGET_SECONDS
    entries: list[dict[str, Any]] = []
    pending: list[tuple[Path, str]] = [(start, prefix)]
    while pending:
        directory, rel_dir = pending.pop()
        if time.monotonic() > deadline:
            raise ProjectFileError("inventory_limit", "project inventory exceeded its time budget")
        try:
            with os.scandir(directory) as iterator:
                children = sorted(iterator, key=lambda item: item.name.casefold())
        except PermissionError:
            raise ProjectFileError(
                "permission_denied", "a directory in the requested inventory cannot be read",
                {"path": rel_dir},
            )
        except OSError as exc:
            raise ProjectFileError("inventory_unavailable", "project inventory could not be completed",
                                   {"path": rel_dir}) from exc
        recurse: list[tuple[Path, str]] = []
        for child in children:
            rel = f"{rel_dir}/{child.name}" if rel_dir else child.name
            try:
                facts = child.stat(follow_symlinks=False)
                if stat.S_ISLNK(facts.st_mode):
                    kind = "symlink"
                    size = None
                elif stat.S_ISDIR(facts.st_mode):
                    kind = "directory"
                    size = None
                    if recursive:
                        recurse.append((Path(child.path), rel))
                elif stat.S_ISREG(facts.st_mode):
                    kind = "file"
                    size = int(facts.st_size)
                else:
                    # Sockets, devices, and other special entries are not
                    # readable project files and are not followed.
                    kind = "symlink"
                    size = None
            except PermissionError:
                kind, size = "file", None
            except OSError:
                kind, size = "file", None
            entries.append({"path": rel, "type": kind, "size_bytes": size})
            if len(entries) > MAX_INVENTORY_ENTRIES:
                raise ProjectFileError("inventory_limit", "project inventory exceeded its entry limit")
        pending.extend(reversed(recurse))
    entries.sort(key=lambda item: item["path"].casefold())
    return entries


def list_project_files(
    project_root: Path,
    path: str,
    *,
    recursive: bool = False,
    cursor: str | None = None,
    limit: int = DEFAULT_LIST_LIMIT,
    policy_revision: int = 0,
    folder_rules: tuple[tuple[str, bool], ...] = (),
    effective_index: Callable[[str], tuple[bool, str | None]] | None = None,
    effective_read_only: Callable[[str], bool] | None = None,
    index_state: Callable[[str], tuple[str, dict[str, str] | None]] | None = None,
) -> dict[str, Any]:
    """List a pinned, deterministically ordered source inventory page."""
    normalized = normalize_project_path(path, allow_root=True)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_LIST_LIMIT:
        raise ProjectFileError("validation_failed", f"limit must be between 1 and {MAX_LIST_LIMIT}")
    if not isinstance(recursive, bool):
        raise ProjectFileError("validation_failed", "recursive must be boolean")
    root = Path(project_root).resolve(strict=True)
    start, _ = _rooted_path(root, normalized, allow_root=True)
    try:
        facts = start.stat(follow_symlinks=False)
    except FileNotFoundError as exc:
        raise ProjectFileError("file_not_found", "requested directory does not exist") from exc
    except PermissionError as exc:
        raise ProjectFileError("permission_denied", "requested directory cannot be read") from exc
    if stat.S_ISLNK(facts.st_mode) or not stat.S_ISDIR(facts.st_mode):
        raise ProjectFileError("validation_failed", "path must name a directory")
    inventory = _walk_inventory(root, start, normalized, recursive)
    inventory_digest = hashlib.sha256(
        json.dumps(inventory, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    query = {"path": normalized, "recursive": recursive, "limit": limit}
    offset = 0
    if cursor is not None:
        decoded = _cursor_decode(cursor)
        if (decoded.get("query") != query or decoded.get("policy_revision") != policy_revision
                or decoded.get("inventory_sha256") != inventory_digest
                or isinstance(decoded.get("offset"), bool)
                or not isinstance(decoded.get("offset"), int)
                or decoded["offset"] < 0 or decoded["offset"] > len(inventory)):
            raise ProjectFileError("invalid_cursor", "project listing changed; restart the listing")
        offset = decoded["offset"]
    page = inventory[offset: offset + limit]
    enriched: list[dict[str, Any]] = []
    for entry in page:
        item = dict(entry)
        rel = entry["path"]
        protected = _is_protected_storage_path(rel)
        if effective_index is not None:
            indexed, reason = effective_index(rel)
        else:
            indexed, reason = _folder_decision(rel, folder_rules)
        if protected:
            indexed, reason = False, "managed_state"
        if entry["type"] == "symlink":
            indexed, reason = False, reason or "symbolic_link"
        read_only = bool(protected or entry["type"] == "symlink")
        if effective_read_only is not None:
            read_only = read_only or bool(effective_read_only(rel))
        state, error = ("excluded", None) if not indexed else ("not_indexed", None)
        if index_state is not None:
            state, error = index_state(rel)
            if not indexed:
                state = "excluded"
        item.update({
            "effective_read_only": read_only,
            "effective_indexed": indexed,
            "exclusion_reason": reason,
            "index_state": state,
            "error": error,
        })
        enriched.append(item)
    next_offset = offset + len(page)
    has_more = next_offset < len(inventory)
    next_cursor = _cursor_encode({
        "version": 1, "query": query, "policy_revision": policy_revision,
        "inventory_sha256": inventory_digest, "offset": next_offset,
    }) if has_more else None
    return {
        "policy_revision": policy_revision,
        "entries": enriched,
        "has_more": has_more,
        "next_cursor": next_cursor,
    }


def read_project_file(
    project_root: Path,
    path: str,
    *,
    offset: int = 0,
    max_bytes: int = DEFAULT_READ_BYTES,
    expected_bytes_sha256: str | None = None,
    lock_check: Callable[[Path], None] | None = None,
) -> dict[str, Any]:
    """Read an exact byte range while hashing a stable source identity."""
    target, normalized = _rooted_path(Path(project_root), path)
    if _is_protected_storage_path(normalized):
        raise ProjectFileError("permission_denied", "managed project state is not readable through this tool")
    for value, name in ((offset, "offset"), (max_bytes, "max_bytes")):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ProjectFileError("validation_failed", f"{name} must be a nonnegative integer")
    if not 1 <= max_bytes <= MAX_READ_BYTES:
        raise ProjectFileError("validation_failed", f"max_bytes must be between 1 and {MAX_READ_BYTES}")
    if offset and (not isinstance(expected_bytes_sha256, str)
                   or len(expected_bytes_sha256) != 64
                   or any(c not in "0123456789abcdef" for c in expected_bytes_sha256)):
        raise ProjectFileError("validation_failed", "nonzero offsets require expected_bytes_sha256")
    if expected_bytes_sha256 is not None and (
        len(expected_bytes_sha256) != 64
        or any(c not in "0123456789abcdef" for c in expected_bytes_sha256)
    ):
        raise ProjectFileError("validation_failed", "expected_bytes_sha256 must be lowercase SHA-256 hex")
    # Admit the exact target before opening our own read handle. On Windows,
    # require_unlocked uses an exclusive open; running it after os.open makes
    # our descriptor look like an external sharing violation.
    if lock_check is not None:
        lock_check(target)
    if target.suffix.lower() == ".docx":
        try:
            from .docx import FileLockedError, require_unlocked
            require_unlocked(target)
        except ImportError:
            pass
        except FileLockedError as exc:
            raise ProjectFileError("file_locked", "project file is locked") from exc
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(target, flags)
    except FileNotFoundError as exc:
        raise ProjectFileError("file_not_found", "project file does not exist") from exc
    except PermissionError as exc:
        raise ProjectFileError("permission_denied", "project file cannot be read") from exc
    except OSError as exc:
        if getattr(exc, "winerror", None) in {32, 33}:
            raise ProjectFileError("file_locked", "project file is locked") from exc
        raise ProjectFileError("permission_denied", "project file cannot be opened safely") from exc
    try:
        with os.fdopen(descriptor, "rb", closefd=True) as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode):
                raise ProjectFileError("validation_failed", "path must name a regular file")
            full_hash = hashlib.sha256()
            stream.seek(0)
            while block := stream.read(1024 * 1024):
                full_hash.update(block)
            digest = full_hash.hexdigest()
            total = int(before.st_size)
            if offset > total:
                raise ProjectFileError("validation_failed", "offset exceeds file size")
            if expected_bytes_sha256 is not None and digest != expected_bytes_sha256:
                raise ProjectFileError("stale_file", "project file does not match expected_bytes_sha256",
                                       {"current_bytes_sha256": digest})
            stream.seek(offset)
            content = stream.read(max_bytes)
            after_range = hashlib.sha256()
            stream.seek(0)
            while block := stream.read(1024 * 1024):
                after_range.update(block)
            after = os.fstat(stream.fileno())
            if (after_range.hexdigest() != digest or before.st_size != after.st_size
                    or before.st_mtime_ns != after.st_mtime_ns or before.st_ino != after.st_ino):
                raise ProjectFileError("stale_file", "project file changed while the range was read")
    except ProjectFileError:
        raise
    except PermissionError as exc:
        raise ProjectFileError("permission_denied", "project file cannot be read") from exc
    has_more = offset + len(content) < total
    return {
        "path": normalized,
        "offset": offset,
        "next_offset": offset + len(content) if has_more else None,
        "total_size_bytes": total,
        "bytes_sha256": digest,
        "content_base64": base64.b64encode(content).decode("ascii"),
        "has_more": has_more,
    }
