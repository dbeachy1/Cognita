"""Offline bootstrap and verification for the pinned Workspace toolbox image.

Microsandbox maintains an image cache separate from Docker Engine.  The host
release step exports the exact local Docker image, then this module imports it
through the pinned SDK's local ``Image.load`` API while the runtime's normal
``MSB_DATA_DIR`` bind mount is active.  No registry client or Docker socket is
available in the workspace-runtime container.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import tarfile
import tempfile
from pathlib import Path
from typing import Any, NamedTuple

from microsandbox import Image

TOOLBOX_TAG = "cognita-workspace-toolbox:12.6.0"
TOOLBOX_TITLE = "Cognita Workspace toolbox"
TOOLBOX_DESCRIPTION = "Credential-free CPU toolbox for isolated Workspaces"
TOOLBOX_VERSION = "12.6.0"
ARCHIVE_ROOT = Path("/var/lib/cognita/toolbox-cache")
ARCHIVE_NAME = "toolbox-12.6.0.tar"
BINDING_NAME = "toolbox-12.6.0.binding.json"
MAX_ARCHIVE_MANIFEST_BYTES = 1024 * 1024
MAX_CONFIG_BYTES = 4 * 1024 * 1024


class ArchiveBinding(NamedTuple):
    """Identity emitted by Docker's deterministic ``save`` archive.

    Docker's image ``Id`` is a local Engine chain identity.  It is not the
    digest stored in a ``docker save`` archive, and Microsandbox 0.7 reports
    the latter as ``ImageConfigDetail.digest``.  Binding both the complete
    archive digest and its config-blob digest lets the runtime verify the
    exact release artifact without a Docker socket or registry access.
    """

    tag: str
    archive_digest: str
    config_digest: str


def _validated_archive_path(archive: Path) -> Path:
    if archive.parent != ARCHIVE_ROOT or archive.is_symlink():
        raise SystemExit("toolbox cache: archive path is outside the configured release cache")
    resolved = archive.resolve()
    if resolved.parent != ARCHIVE_ROOT or resolved.name != ARCHIVE_NAME:
        raise SystemExit("toolbox cache: archive path is outside the configured release cache")
    if not resolved.is_file() or resolved.is_symlink():
        raise SystemExit("toolbox cache: deterministic local archive is missing")
    return resolved


def _archive_binding(archive: Path, tag: str) -> ArchiveBinding:
    """Validate and derive the release binding from a Docker-save archive."""
    resolved = _validated_archive_path(archive)
    archive_hash = hashlib.sha256()
    with resolved.open("rb") as stream:
        for block in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            archive_hash.update(block)

    try:
        with tarfile.open(resolved, mode="r:") as saved:
            # tarfile.getmember selects the last duplicate, while an image
            # loader may select another. Reject ambiguous archive paths.
            members = saved.getmembers()
            if len({member.name for member in members}) != len(members):
                raise ValueError("archive contains duplicate paths")
            manifests_in_tar = [member for member in members if member.name == "manifest.json"]
            if len(manifests_in_tar) != 1:
                raise ValueError("archive must contain one manifest entry")
            manifest_info = manifests_in_tar[0]
            if not manifest_info.isfile() or manifest_info.size > MAX_ARCHIVE_MANIFEST_BYTES:
                raise ValueError("invalid manifest entry")
            manifests = json.load(saved.extractfile(manifest_info))
            if not isinstance(manifests, list) or len(manifests) != 1:
                raise ValueError("manifest must contain one image")
            manifest = manifests[0]
            if (
                not isinstance(manifest, dict)
                or not isinstance(manifest.get("RepoTags"), list)
                or tag not in manifest["RepoTags"]
            ):
                raise ValueError("manifest tag does not match requested image")
            config_name = manifest.get("Config")
            match = (
                re.fullmatch(r"blobs/sha256/([0-9a-f]{64})", config_name)
                if isinstance(config_name, str) else None
            )
            if match is None:
                raise ValueError("manifest config is not a sha256 blob")
            configs_in_tar = [member for member in members if member.name == config_name]
            if len(configs_in_tar) != 1:
                raise ValueError("archive must contain one config entry")
            config_info = configs_in_tar[0]
            if not config_info.isfile() or config_info.size > MAX_CONFIG_BYTES:
                raise ValueError("invalid config entry")
            config_bytes = saved.extractfile(config_info).read()
            config_digest = "sha256:" + hashlib.sha256(config_bytes).hexdigest()
            if config_digest != "sha256:" + match.group(1):
                raise ValueError("manifest config digest does not match config bytes")
    except (KeyError, OSError, tarfile.TarError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise SystemExit(f"toolbox cache: release archive binding is invalid: {exc}") from exc
    return ArchiveBinding(
        tag=tag,
        archive_digest="sha256:" + archive_hash.hexdigest(),
        config_digest=config_digest,
    )


def _binding_path(archive: Path) -> Path:
    _validated_archive_path(archive)
    return ARCHIVE_ROOT / BINDING_NAME


def _write_binding(archive: Path, tag: str = TOOLBOX_TAG) -> ArchiveBinding:
    """Record the archive binding after the host's Docker save completes."""
    binding = _archive_binding(archive, tag)
    output = _binding_path(archive)
    temporary: Path | None = None
    try:
        # A unique owner-only file prevents an existing path from redirecting
        # the write. Replace publishes a complete binding atomically.
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=ARCHIVE_ROOT,
            prefix=f".{output.name}.",
            suffix=".tmp",
            delete=False,
        ) as stream:
            temporary = Path(stream.name)
            stream.write(
                json.dumps(
                    {
                        "schema": 1,
                        "tag": binding.tag,
                        "archive": ARCHIVE_NAME,
                        "archive_sha256": binding.archive_digest,
                        "config_digest": binding.config_digest,
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ) + "\n"
            )
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(output)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()
    return binding


def _verified_binding(archive: Path, tag: str) -> ArchiveBinding:
    binding = _archive_binding(archive, tag)
    path = _binding_path(archive)
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 16 * 1024:
        raise SystemExit("toolbox cache: release archive binding is missing or unsafe")
    try:
        recorded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SystemExit(f"toolbox cache: release archive binding is invalid: {exc}") from exc
    if not isinstance(recorded, dict) or recorded != {
        "schema": 1,
        "tag": tag,
        "archive": ARCHIVE_NAME,
        "archive_sha256": binding.archive_digest,
        "config_digest": binding.config_digest,
    }:
        raise SystemExit("toolbox cache: release archive binding does not match archive")
    return binding


def _detail_is_valid(detail: Any, tag: str, config_digest: str) -> bool:
    handle = getattr(detail, "handle", None)
    config = getattr(detail, "config", None)
    if handle is None or config is None:
        return False
    if getattr(handle, "reference", None) != tag:
        return False
    if getattr(config, "digest", None) != config_digest:
        return False
    labels = getattr(config, "labels", {}) or {}
    return (
        labels.get("org.opencontainers.image.title") == TOOLBOX_TITLE
        and labels.get("org.opencontainers.image.description") == TOOLBOX_DESCRIPTION
        and labels.get("org.opencontainers.image.version") == TOOLBOX_VERSION
        and getattr(config, "user", None) == "workspace"
        and getattr(config, "working_dir", None) == "/workspace"
        and getattr(config, "cmd", None) == ["bash"]
    )


async def _inspect(tag: str, config_digest: str) -> bool:
    try:
        detail = await Image.inspect(tag)
    except Exception:  # Microsandbox has version-specific not-found errors.
        return False
    return _detail_is_valid(detail, tag, config_digest)


async def verify(
    archive: Path = ARCHIVE_ROOT / ARCHIVE_NAME,
    config_digest: str | None = None,
    tag: str = TOOLBOX_TAG,
) -> None:
    binding = _verified_binding(archive, tag)
    if config_digest is not None and config_digest != binding.config_digest:
        raise SystemExit("toolbox cache: supplied image identity does not match release archive")
    if not await _inspect(tag, binding.config_digest):
        raise SystemExit(f"toolbox cache: required local image is missing or invalid: {tag}")


async def load(archive: Path, config_digest: str | None = None, tag: str = TOOLBOX_TAG) -> None:
    binding = _verified_binding(archive, tag)
    if config_digest is not None and config_digest != binding.config_digest:
        raise SystemExit("toolbox cache: supplied image identity does not match release archive")
    if await _inspect(tag, binding.config_digest):
        return
    existing = None
    try:
        existing = await Image.get(tag)
    except Exception:
        pass
    if existing is not None:
        # A running Workspace may still depend on the old cache entry.  Let
        # Microsandbox refuse removal in that case so deployment fails closed.
        await existing.remove()
    await Image.load(str(_validated_archive_path(archive)), tag=tag)
    if not await _inspect(tag, binding.config_digest):
        raise SystemExit("toolbox cache: imported image failed identity verification")


def main() -> None:
    parser = argparse.ArgumentParser(description="Verify or load the offline Workspace toolbox cache")
    parser.add_argument("action", choices=("verify", "load", "materialize-binding"))
    parser.add_argument("--archive", type=Path, default=ARCHIVE_ROOT / ARCHIVE_NAME)
    parser.add_argument("--tag", default=TOOLBOX_TAG)
    parser.add_argument("--config-digest")
    args = parser.parse_args()
    if args.tag != TOOLBOX_TAG:
        raise SystemExit("toolbox cache: mutable or unsupported image tag")
    if args.config_digest is not None and not re.fullmatch(r"sha256:[0-9a-f]{64}", args.config_digest):
        raise SystemExit("toolbox cache: Docker config digest is invalid")
    if args.action == "materialize-binding":
        _write_binding(args.archive, args.tag)
    elif args.action == "verify":
        asyncio.run(verify(args.archive, args.config_digest, args.tag))
    else:
        asyncio.run(load(args.archive, args.config_digest, args.tag))
    print(f"toolbox cache: {args.action} passed for {args.tag}")


if __name__ == "__main__":
    main()
