"""Bounded, idempotent provisioning of the persistent ``Self-Test`` fixtures.

Only files named by the package manifest are ever opened.  In particular, the
provisioner does not walk the target directory and never copies or logs project
content.  Registry and connector policy checks happen before any registry
mutation or fixture replacement.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..connectors import (
    ConnectorPolicyError,
    ConnectorStore,
    PolicyUnavailable,
    resolve_project_access,
)
from ..registry import Project, Registry

PROJECT_NAME = "Self-Test"
FIXTURE_MANIFEST_NAME = "manifest.json"
_PACKAGE_DATA = Path(__file__).resolve().parent / "data"
_MAX_MANIFEST_BYTES = 64 * 1024
_MAX_FIXTURE_BYTES = 16 * 1024 * 1024
_ALLOWED_PATHS = (
    "cognita-selftest/ocr/canonical-clear.png",
    "cognita-selftest/ocr/blank.png",
    "cognita-selftest/ocr/malformed.png",
    "cognita-selftest/ocr/animated.png",
    "cognita-selftest/ocr/not-a-png.txt",
    "cognita-selftest/ocr/over-limit.png",
)
_PINNED_HASHES = {
    "cognita-selftest/ocr/canonical-clear.png":
        "f371c0951f5ad07b2c039b4481ad23e746f2db18bee014e5d54a6de734bb8a63",
    "cognita-selftest/ocr/blank.png":
        "cf3adf0667963af8ed7a70f1902dcee28cb809f08f98853c59782e6b7021cebd",
}


class FixtureProvisionError(RuntimeError):
    """A fail-closed fixture or policy provisioning error."""

    reason = "fixture_provisioning"


@dataclass(frozen=True, slots=True)
class FixtureSpec:
    """One manifest-pinned source and project-relative destination."""

    path: str
    sha256: str
    purpose: str


@dataclass(frozen=True, slots=True)
class ProvisionResult:
    """Bounded operator-facing result; no fixture contents are included."""

    project_created: bool
    copied: tuple[str, ...]
    verified: tuple[str, ...]


def _fail(message: str) -> FixtureProvisionError:
    return FixtureProvisionError(f"blocked: fixture_provisioning ({message})")


def _sha256(path: Path, *, maximum: int = _MAX_FIXTURE_BYTES) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    try:
        with path.open("rb") as stream:
            while True:
                chunk = stream.read(min(1024 * 1024, maximum + 1 - size))
                if not chunk:
                    break
                size += len(chunk)
                if size > maximum:
                    raise _fail("fixture exceeds bounded size")
                digest.update(chunk)
    except FixtureProvisionError:
        raise
    except OSError as exc:
        raise _fail("fixture source is unavailable") from exc
    return size, digest.hexdigest()


def _safe_relative(value: Any) -> str:
    if not isinstance(value, str) or not value or "\\" in value:
        raise _fail("manifest contains an invalid relative path")
    path = Path(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise _fail("manifest contains an unsafe relative path")
    return value


def load_manifest() -> tuple[FixtureSpec, ...]:
    """Load and verify the package manifest and every allowlisted source file."""
    manifest_path = _PACKAGE_DATA / FIXTURE_MANIFEST_NAME
    try:
        if manifest_path.stat().st_size > _MAX_MANIFEST_BYTES:
            raise _fail("fixture manifest exceeds bounded size")
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    except FixtureProvisionError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise _fail("fixture manifest is unavailable or invalid") from exc
    if not isinstance(raw, dict) or raw.get("version") != 1 or raw.get("project") != PROJECT_NAME:
        raise _fail("unsupported fixture manifest")
    rows = raw.get("fixtures")
    if not isinstance(rows, list) or len(rows) != len(_ALLOWED_PATHS):
        raise _fail("fixture manifest allowlist is invalid")
    specs: list[FixtureSpec] = []
    for row in rows:
        if not isinstance(row, dict):
            raise _fail("fixture manifest entry is invalid")
        path = _safe_relative(row.get("path"))
        if path not in _ALLOWED_PATHS or any(spec.path == path for spec in specs):
            raise _fail("fixture manifest contains an unallowlisted or duplicate path")
        sha256 = row.get("sha256")
        purpose = row.get("purpose")
        if (not isinstance(sha256, str) or len(sha256) != 64
                or sha256.casefold() != sha256 or any(c not in "0123456789abcdef" for c in sha256)
                or not isinstance(purpose, str) or not purpose):
            raise _fail("fixture manifest entry has invalid hash or purpose")
        if path in _PINNED_HASHES and sha256 != _PINNED_HASHES[path]:
            raise _fail("canonical fixture hash is not pinned")
        source = _PACKAGE_DATA / path
        if not source.is_file() or source.is_symlink():
            raise _fail("fixture source is missing")
        _, actual = _sha256(source)
        if actual != sha256:
            raise _fail("fixture source hash does not match manifest")
        specs.append(FixtureSpec(path, sha256, purpose))
    return tuple(specs)


def _resolved(path: Path) -> Path:
    try:
        return path.expanduser().resolve(strict=False)
    except OSError as exc:
        raise _fail("target path cannot be resolved") from exc


def _check_project(registry: Registry, documents_dir: Path, data_dir: Path) -> bool:
    existing = registry.get(PROJECT_NAME)
    if existing is None:
        return True
    if (_resolved(Path(existing.documents_dir)) != documents_dir
            or _resolved(Path(existing.data_dir)) != data_dir
            or not existing.writable or not existing.enabled):
        raise _fail("Self-Test registry entry conflicts with requested paths or policy")
    return False


def _create_project(registry: Registry, documents_dir: Path, data_dir: Path) -> None:
    if registry.get(PROJECT_NAME) is None:
        registry.add(Project(name=PROJECT_NAME, documents_dir=documents_dir, data_dir=data_dir, writable=True))


def _verify_connector_policy(connectors_path: Path, registry: Registry) -> None:
    try:
        # Provisioning only reads connector policy; legacy normalization must
        # not turn this deployment check into an unrelated policy write.
        config = ConnectorStore(connectors_path, persist_migrations=False).snapshot()
    except (ConnectorPolicyError, PolicyUnavailable) as exc:
        raise _fail("connector policy is unavailable") from exc
    production = [item for item in config.connectors if item.name.casefold() == "cognita"]
    if len(production) != 1:
        raise _fail("production Cognita connector policy is missing or ambiguous")
    connector = production[0]
    project = next(
        (item for item in registry.projects if item.name == PROJECT_NAME),
        Project(name=PROJECT_NAME, documents_dir=Path("."), data_dir=Path(".")),
    )
    if (not connector.enabled or connector.project_mode != "all"
            or connector.default_access != "write"
            or resolve_project_access(connector, project) != "write"):
        raise _fail("production Cognita connector must be enabled with project_mode=all/default_access=write")
    # Validate the complete policy against existing projects, without allowing
    # this read-only check to rewrite connector configuration.
    try:
        connector.validate_projects([p.name for p in (*registry.projects, Project(name=PROJECT_NAME, documents_dir=Path("."), data_dir=Path(".")))])
    except ConnectorPolicyError as exc:
        raise _fail("connector policy references an unknown project") from exc


def _atomic_replace(source: Path, target: Path, expected_hash: str) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
    temp = Path(name)
    try:
        with os.fdopen(fd, "wb") as stream, source.open("rb") as source_stream:
            remaining = _MAX_FIXTURE_BYTES
            while True:
                chunk = source_stream.read(min(1024 * 1024, remaining + 1))
                if not chunk:
                    break
                remaining -= len(chunk)
                if remaining < 0:
                    raise _fail("fixture exceeds bounded size")
                stream.write(chunk)
            stream.flush()
            os.fsync(stream.fileno())
        _, staged_hash = _sha256(temp)
        if staged_hash != expected_hash:
            raise _fail("staged fixture hash mismatch")
        os.replace(temp, target)
    except FixtureProvisionError:
        raise
    except OSError as exc:
        raise _fail("atomic fixture replacement failed") from exc
    finally:
        try:
            temp.unlink(missing_ok=True)
        except OSError:
            pass


def _assert_target_chain(root: Path, relative: str) -> None:
    """Reject a symlink in the allowlisted destination's parent chain."""
    current = root
    for part in Path(relative).parts[:-1]:
        current /= part
        if current.is_symlink():
            raise _fail("fixture destination may not traverse a symlink")


def _assert_root_safe(path: Path) -> None:
    """Reject symlinked explicit roots and their existing parent components."""
    current = Path(path.anchor) if path.anchor else Path()
    for part in path.parts[1:] if path.anchor else path.parts:
        current /= part
        if current.is_symlink():
            raise _fail("target root may not be a symlink")


def provision_self_test(
    *,
    documents_dir: Path,
    data_dir: Path,
    registry_path: Path,
    connectors_path: Path | None = None,
) -> ProvisionResult:
    """Provision the exact synthetic allowlist and verify Self-Test policy.

    ``connectors_path`` is optional for isolated package tests.  Deployment
    passes it explicitly, which enables the production all-project/write
    policy check required before declaring the connector self-test ready.
    """
    specs = load_manifest()
    requested_docs = Path(documents_dir).expanduser()
    requested_data = Path(data_dir).expanduser()
    _assert_root_safe(requested_docs)
    _assert_root_safe(requested_data)
    docs = _resolved(requested_docs)
    data = _resolved(requested_data)
    if docs == data:
        raise _fail("documents and data roots must be distinct")
    registry = Registry(Path(registry_path))
    if connectors_path is not None:
        _verify_connector_policy(Path(connectors_path), registry)
    # Check existing policy before creating either target root.  A conflict is
    # therefore entirely read-only and cannot leave a misleading partial setup.
    created = _check_project(registry, docs, data)
    try:
        docs.mkdir(parents=True, exist_ok=True)
        data.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise _fail("Self-Test target roots are unavailable") from exc
    copied: list[str] = []
    verified: list[str] = []
    for spec in specs:
        source = _PACKAGE_DATA / spec.path
        target = docs / spec.path
        _assert_target_chain(docs, spec.path)
        if target.exists() and target.is_dir():
            raise _fail("fixture destination conflicts with a directory")
        if target.is_symlink():
            raise _fail("fixture destination may not be a symlink")
        if target.is_file():
            try:
                _, current = _sha256(target)
            except FixtureProvisionError:
                # A changed destination may itself exceed the source bound;
                # replace it from the trusted, bounded package source rather
                # than allowing an oversized destination to block repair.
                current = None
            if current != spec.sha256:
                _atomic_replace(source, target, spec.sha256)
                copied.append(spec.path)
        else:
            _atomic_replace(source, target, spec.sha256)
            copied.append(spec.path)
        _, actual = _sha256(target)
        if actual != spec.sha256:
            raise _fail("destination fixture hash mismatch")
        verified.append(spec.path)
    _create_project(registry, docs, data)
    return ProvisionResult(created, tuple(copied), tuple(verified))
