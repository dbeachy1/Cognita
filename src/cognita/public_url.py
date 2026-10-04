"""Persisted administrator override for Cognita's canonical public URL.

The deployment configuration remains the initial value.  Once an Admin saves a
URL, this small Cognita-owned state file becomes the runtime source of truth,
which lets a tunnel or DNS change take effect without editing a deployment
file.  The file contains no credentials and is replaced atomically.
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import threading
from pathlib import Path
from typing import Any, ClassVar
from urllib.parse import urlsplit

log = logging.getLogger("cognita.public_url")

STATE_VERSION = 1
MAX_URL_LENGTH = 2048
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost", "test"})


class PublicURLValidationError(ValueError):
    """The proposed public URL is not safe to use as an external identity."""


def validate_public_base_url(value: Any) -> str:
    """Validate and canonicalize a public HTTP(S) base URL.

    Credentials, query strings, fragments, and control/whitespace characters
    are rejected because this value is copied into OAuth issuer and audience
    metadata.  HTTPS is mandatory except for explicitly local development
    hosts, matching the OAuth readiness rule.
    """
    if not isinstance(value, str):
        raise PublicURLValidationError("public_base_url must be a URL string")
    if not value or len(value) > MAX_URL_LENGTH or value != value.strip():
        raise PublicURLValidationError("public_base_url must be 1 to 2048 characters")
    if _CONTROL_RE.search(value) or any(character.isspace() for character in value):
        raise PublicURLValidationError("public_base_url must not contain whitespace or control characters")
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
        # Accessing port also validates malformed/out-of-range ports.
        _ = parsed.port
    except ValueError as exc:
        raise PublicURLValidationError("public_base_url is not a valid URL") from exc
    if parsed.scheme.lower() not in {"http", "https"} or not hostname:
        raise PublicURLValidationError("public_base_url must use HTTP or HTTPS and include a host")
    if parsed.username is not None or parsed.password is not None:
        raise PublicURLValidationError("public_base_url must not include username or password")
    if parsed.query or parsed.fragment:
        raise PublicURLValidationError("public_base_url must not include a query or fragment")
    host = hostname.casefold().rstrip(".")
    if parsed.scheme.lower() != "https" and host not in _LOOPBACK_HOSTS:
        raise PublicURLValidationError("public_base_url must use HTTPS except for local development")
    # URL identity is case-insensitive for scheme/host, while preserving a
    # user-selected path prefix.  Strip only trailing separators so generated
    # connector paths have exactly one slash at the join.
    normalized = value.rstrip("/")
    if not normalized:
        raise PublicURLValidationError("public_base_url must include a host")
    return normalized


def public_base_url_state_path(config: Any) -> Path:
    """Return the installation-local path for the persisted URL override."""
    return Path(config.data_root) / "public-base-url.json"


def _read_override(path: Path) -> str | None:
    try:
        if not path.is_file():
            return None
        if path.stat().st_size > 16 * 1024:
            raise PublicURLValidationError("state file exceeds the bounded read limit")
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or payload.get("version") != STATE_VERSION:
            raise PublicURLValidationError("unsupported state version")
        return validate_public_base_url(payload.get("public_base_url"))
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, PublicURLValidationError) as exc:
        log.error("public URL state unavailable path=%s reason=%s", path, type(exc).__name__)
        raise PublicURLValidationError("persisted public base URL state is invalid") from exc


class PublicBaseURLStore:
    """Thread-safe persisted override shared by Admin, gateway, and OAuth."""

    _locks: ClassVar[dict[str, threading.RLock]] = {}
    _locks_guard = threading.Lock()

    def __init__(self, config: Any):
        self.config = config
        self.path = public_base_url_state_path(config)
        # ``load_config`` and app construction project the effective value back
        # onto config for legacy consumers. Capture the deployment seed once so
        # later store instances can still distinguish it from an Admin override.
        if getattr(config, "_deployment_public_base_url", None) is None:
            config._deployment_public_base_url = str(config.public_base_url or "").rstrip("/")
        key = str(self.path.resolve())
        with self._locks_guard:
            self._lock = self._locks.setdefault(key, threading.RLock())

    def effective(self) -> str:
        """Return the saved URL, falling back to deployment configuration."""
        with self._lock:
            override = _read_override(self.path)
            if override is not None:
                return override
            # Keep legacy startup behavior for malformed deployment defaults;
            # OAuth readiness already reports those as a configuration problem.
            # Admin mutations and persisted overrides are always validated.
            return str(self.config._deployment_public_base_url or "").rstrip("/")

    def has_override(self) -> bool:
        """Whether a valid administrator override is currently persisted."""
        with self._lock:
            return _read_override(self.path) is not None

    def save(self, value: str) -> str:
        """Validate and atomically persist a new URL override."""
        normalized = validate_public_base_url(value)
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, temp_name = tempfile.mkstemp(
                prefix=f".{self.path.name}.", suffix=".tmp", dir=self.path.parent
            )
            temp_path = Path(temp_name)
            try:
                os.chmod(temp_path, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
                    fd = -1  # the stream now owns and closes the descriptor
                    json.dump({"version": STATE_VERSION, "public_base_url": normalized}, stream)
                    stream.write("\n")
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temp_path, self.path)
                try:
                    os.chmod(self.path, 0o600)
                except OSError:
                    pass
            except Exception:
                if fd >= 0:
                    try:
                        os.close(fd)
                    except OSError:
                        pass
                try:
                    temp_path.unlink(missing_ok=True)
                except OSError:
                    log.error("public URL temporary cleanup failed path=%s", temp_path)
                raise
            return normalized

    def clear(self) -> None:
        """Remove the override so deployment configuration is effective again."""
        with self._lock:
            self.path.unlink(missing_ok=True)


def effective_public_base_url(config: Any) -> str:
    """Read the current effective URL for a config-like object."""
    return PublicBaseURLStore(config).effective()
