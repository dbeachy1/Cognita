"""Verified transient staging for audiobook import sources.

These helpers own only local staging.  The import service adopts a staged file
by rename after its durable publication succeeds; failed callers discard it.
"""
from __future__ import annotations

import hashlib
import os
import stat
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Literal


class SourceStageError(RuntimeError):
    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class StagedAudioSource:
    staged_path: Path
    bytes_sha256: str
    size_bytes: int
    source_kind: Literal["workspace", "https"]


_STAGE_PREFIX = ".cognita-book-source-"
_CHUNK = 1024 * 1024


def stage_verified_file(
    source: Path,
    staging_root: Path,
    *,
    expected_sha256: str,
    source_kind: Literal["workspace", "https"],
    max_bytes: int,
    reserve_bytes: int = 0,
) -> StagedAudioSource:
    """Copy one regular source file into an owned, hash-verified stage."""
    if len(expected_sha256) != 64 or any(c not in "0123456789abcdef" for c in expected_sha256):
        raise SourceStageError("validation_failed", "An exact lowercase SHA-256 is required.")
    root = Path(staging_root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    if (not isinstance(max_bytes, int) or isinstance(max_bytes, bool) or max_bytes < 1
            or not isinstance(reserve_bytes, int) or isinstance(reserve_bytes, bool)
            or reserve_bytes < 0):
        raise SourceStageError("validation_failed", "The source byte budget is invalid.")
    try:
        source_info = source.stat(follow_symlinks=False)
    except OSError as exc:
        raise SourceStageError("source_unavailable", "The staged source is unavailable.") from exc
    if not stat.S_ISREG(source_info.st_mode) or stat.S_ISLNK(source_info.st_mode):
        raise SourceStageError("source_unavailable", "The staged source is not a regular file.")
    import shutil
    if source_info.st_size > max_bytes:
        raise SourceStageError("quota_exceeded", "The audio source exceeds the configured byte quota.")
    if shutil.disk_usage(root).free - reserve_bytes < source_info.st_size:
        raise SourceStageError("storage_unavailable", "Insufficient free storage for the audio source.")
    target = root / f"{_STAGE_PREFIX}{uuid.uuid4().hex}"
    digest = hashlib.sha256()
    total = 0
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
        fd = os.open(target, flags, 0o600)
        with source.open("rb") as incoming, os.fdopen(fd, "wb") as outgoing:
            while chunk := incoming.read(_CHUNK):
                total += len(chunk)
                digest.update(chunk)
                outgoing.write(chunk)
        if digest.hexdigest() != expected_sha256 or total != source_info.st_size:
            raise SourceStageError("source_changed", "The staged source no longer matches its pinned hash.")
        return StagedAudioSource(target, digest.hexdigest(), total, source_kind)
    except Exception:
        try:
            target.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def discard_staged_audio(source: StagedAudioSource, staging_root: Path) -> None:
    """Remove only a helper-owned, not-yet-adopted staging file."""
    root = Path(staging_root).resolve()
    path = source.staged_path.resolve(strict=False)
    if path.parent != root or not path.name.startswith(_STAGE_PREFIX):
        raise SourceStageError("invalid_stage", "Refusing to remove a non-owned source stage.")
    try:
        info = path.stat(follow_symlinks=False)
    except FileNotFoundError:
        return
    except OSError as exc:
        raise SourceStageError("stage_unavailable", "The source stage cannot be inspected.") from exc
    if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
        raise SourceStageError("invalid_stage", "Refusing to remove a non-regular source stage.")
    try:
        path.unlink()
    except OSError as exc:
        raise SourceStageError("stage_unavailable", "The source stage cannot be removed.") from exc

_MAX_REDIRECTS = 5


def _public_addresses(host: str, port: int, resolver=None) -> tuple[str, ...]:
    import ipaddress
    import socket

    try:
        rows = (resolver or socket.getaddrinfo)(host, port, 0, socket.SOCK_STREAM)
    except OSError as exc:
        raise SourceStageError("source_unavailable", "The authorized source host could not be resolved.") from exc
    addresses = tuple(dict.fromkeys(str(row[4][0]) for row in rows))
    if not addresses:
        raise SourceStageError("source_unavailable", "The authorized source host could not be resolved.")
    for address in addresses:
        try:
            parsed = ipaddress.ip_address(address)
        except ValueError as exc:
            raise SourceStageError("source_unavailable", "The authorized source host has an invalid address.") from exc
        if not parsed.is_global or parsed.is_multicast:
            raise SourceStageError("source_forbidden", "The source host resolves to a private or internal address.")
    return addresses


def _validate_https_url(url: str, allowed_hosts: set[str], resolver=None) -> tuple[str, tuple[str, ...]]:
    from urllib.parse import urlsplit

    parsed = urlsplit(url)
    host = (parsed.hostname or "").casefold().rstrip(".")
    if parsed.scheme != "https" or not host or parsed.username or parsed.password:
        raise SourceStageError("source_forbidden", "The source must be an authorized HTTPS URL.")
    if host not in allowed_hosts:
        raise SourceStageError("source_forbidden", "The source host is not authorized for audio import.")
    return host, _public_addresses(host, parsed.port or 443, resolver)


class _PinnedPublicBackend:
    """Resolve each TCP connection to the vetted address it must use."""

    def __init__(self, *, resolver=None, backend=None):
        import httpcore

        self._resolver = resolver
        self._backend = backend or httpcore.AnyIOBackend()

    async def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        import asyncio

        name = host.decode() if isinstance(host, bytes) else str(host)
        addresses = await asyncio.to_thread(_public_addresses, name, int(port), self._resolver)
        # This numeric target is the connection authority. httpcore retains the
        # request hostname for TLS SNI and certificate verification.
        return await self._backend.connect_tcp(addresses[0], port, timeout, local_address, socket_options)

    async def connect_unix_socket(self, path, timeout=None, socket_options=None):
        raise RuntimeError("Unix sockets are not available for HTTPS import")

    async def sleep(self, seconds):
        await self._backend.sleep(seconds)


async def stage_https_audio_source(
    url: str,
    staging_root: Path,
    *,
    allowed_hosts: tuple[str, ...] | list[str],
    max_bytes: int,
    reserve_bytes: int = 0,
    expected_sha256: str | None = None,
    timeout_seconds: float = 60.0,
    resolver=None,
    _pool_factory=None,
    _clock=time.monotonic,
) -> StagedAudioSource:
    """Download one allowlisted HTTPS source through a public-address pin.

    Redirects are followed manually.  Each request is connected to one of the
    exact public addresses resolved for that hop, avoiding a DNS rebind between
    validation and connection.  The URL never enters durable state or errors.
    """
    import asyncio
    import httpcore
    import shutil
    from urllib.parse import urljoin

    if not isinstance(max_bytes, int) or isinstance(max_bytes, bool) or max_bytes < 1:
        raise SourceStageError("validation_failed", "A positive source byte quota is required.")
    if not isinstance(reserve_bytes, int) or isinstance(reserve_bytes, bool) or reserve_bytes < 0:
        raise SourceStageError("validation_failed", "A nonnegative storage reserve is required.")
    if not isinstance(timeout_seconds, (int, float)) or timeout_seconds <= 0:
        raise SourceStageError("validation_failed", "A positive source timeout is required.")
    expected = None if expected_sha256 is None else expected_sha256.lower()
    if expected is not None and (len(expected) != 64 or any(c not in "0123456789abcdef" for c in expected)):
        raise SourceStageError("validation_failed", "The optional source SHA-256 is invalid.")
    hosts = {str(host).casefold().rstrip(".") for host in allowed_hosts}
    if not hosts or "" in hosts:
        raise SourceStageError("source_forbidden", "No authorized HTTPS import host is configured.")
    root = Path(staging_root).resolve()
    root.mkdir(parents=True, exist_ok=True)

    current = url
    deadline = _clock() + float(timeout_seconds)
    stage_path = root / f"{_STAGE_PREFIX}{uuid.uuid4().hex}"
    try:
        for _hop in range(_MAX_REDIRECTS + 1):
            if _clock() > deadline:
                raise SourceStageError("source_timeout", "The audio source exceeded its total download deadline.")
            _validate_https_url(current, hosts, resolver)
            backend = _PinnedPublicBackend(resolver=resolver)
            pool = (_pool_factory(backend, timeout_seconds) if _pool_factory is not None
                    else httpcore.AsyncConnectionPool(network_backend=backend, max_connections=1))
            try:
                async with pool.stream(
                    "GET", current,
                    headers=[(b"user-agent", b"Cognita audiobook importer")],
                    extensions={"timeout": {"connect": timeout_seconds, "read": timeout_seconds,
                                             "write": timeout_seconds, "pool": timeout_seconds}},
                ) as response:
                    headers = {key.decode("latin-1").lower(): value.decode("latin-1") for key, value in response.headers}
                    if response.status in {301, 302, 303, 307, 308}:
                        location = headers.get("location")
                        if not location:
                            raise SourceStageError("source_unavailable", "The audio source redirect is invalid.")
                        current = urljoin(current, location)
                        continue
                    if response.status < 200 or response.status >= 300:
                        raise SourceStageError("source_unavailable", "The audio source could not be retrieved.")
                    content_length = headers.get("content-length")
                    if content_length is not None:
                        if not content_length.isdecimal() or int(content_length) > max_bytes:
                            raise SourceStageError("quota_exceeded", "The audio source exceeds the configured byte quota.")
                        if shutil.disk_usage(root).free - reserve_bytes < int(content_length):
                            raise SourceStageError("storage_unavailable", "Insufficient free storage for the audio source.")
                    digest, total = hashlib.sha256(), 0
                    fd = os.open(stage_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
                    with os.fdopen(fd, "wb") as outgoing:
                        stream = response.aiter_stream().__aiter__()
                        while True:
                            if _clock() > deadline:
                                raise SourceStageError("source_timeout", "The audio source exceeded its total download deadline.")
                            remaining = deadline - _clock()
                            if remaining <= 0:
                                raise SourceStageError("source_timeout", "The audio source exceeded its total download deadline.")
                            try:
                                chunk = await asyncio.wait_for(stream.__anext__(), timeout=remaining)
                            except StopAsyncIteration:
                                break
                            except asyncio.TimeoutError as exc:
                                raise SourceStageError("source_timeout", "The audio source exceeded its total download deadline.") from exc
                            if shutil.disk_usage(root).free - reserve_bytes < len(chunk):
                                raise SourceStageError("storage_unavailable", "Insufficient free storage for the audio source.")
                            total += len(chunk)
                            if total > max_bytes:
                                raise SourceStageError("quota_exceeded", "The audio source exceeds the configured byte quota.")
                            digest.update(chunk)
                            outgoing.write(chunk)
                    if content_length is not None and total != int(content_length):
                        raise SourceStageError("source_changed", "The audio source ended before its declared length.")
                    actual = digest.hexdigest()
                    if expected is not None and actual != expected:
                        raise SourceStageError("source_changed", "The audio source did not match its expected hash.")
                    return StagedAudioSource(stage_path, actual, total, "https")
            finally:
                await pool.aclose()
        raise SourceStageError("source_unavailable", "The audio source redirected too many times.")
    except SourceStageError:
        try:
            stage_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    except BaseException as exc:
        try:
            stage_path.unlink(missing_ok=True)
        except OSError:
            pass
        if isinstance(exc, asyncio.CancelledError):
            raise
        raise SourceStageError("source_unavailable", "The authorized audio source could not be retrieved.") from exc
