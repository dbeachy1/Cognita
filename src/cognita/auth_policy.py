"""Parent-owned OAuth/static-key authentication policy.

This module deliberately has no FastAPI, worker, or OAuth-child dependency.  It
owns the small durable policy document and exposes immutable, non-secret views
for the gateway and administration surfaces.  Raw static keys are generated
and returned by a mutation exactly once; only their SHA-256 digest is written.
"""

from __future__ import annotations

import base64
import copy
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import subprocess
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any, Callable, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .registry import NAME_RE

log = logging.getLogger("cognita.auth_policy")

POLICY_VERSION = 1
STATIC_KEY_PREFIX = "cog_sk_v1_"
STATIC_KEY_LENGTH = len(STATIC_KEY_PREFIX) + 43
MAX_POLICY_BYTES = 1 * 1024 * 1024
_DIGEST_RE = re.compile(r"\A[0-9a-f]{64}\Z")
_KEY_RE = re.compile(r"\A" + re.escape(STATIC_KEY_PREFIX) + r"[A-Za-z0-9_-]{43}\Z")

# Credential-v2 is intentionally additive.  The 11.x policy models below remain
# readable for rollback and legacy project-key authentication; new named keys use
# this separate record format and never inherit project/global key semantics.
V2_POLICY_VERSION = 2
V2_STATIC_KEY_PREFIX = "cog_sk_v2_"
V2_SECRET_BYTES = 32
V2_MAX_ACTIVE_PER_SURFACE = 64
V2_AAD_VERSION = 1
_V2_TOKEN_RE = re.compile(r"\A" + re.escape(V2_STATIC_KEY_PREFIX) + r"([A-Za-z0-9_-]{22})\.([A-Za-z0-9_-]{43})\Z")
_UUID_RE = re.compile(r"\A[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\Z")


class AuthenticationPolicyError(RuntimeError):
    """Base class for policy load and mutation failures."""


class AuthenticationPolicyUnavailable(AuthenticationPolicyError):
    """The policy is malformed, unreadable, or cannot be safely persisted."""


class AuthenticationRevisionConflict(AuthenticationPolicyError):
    """A mutation was based on a stale policy revision."""

    def __init__(self, current: dict):
        super().__init__("revision_conflict")
        self.current = current


class AuthenticationLockoutConfirmationRequired(AuthenticationPolicyError):
    """A mutation would leave an enabled project without client auth."""

    def __init__(self, projects: list[str]):
        super().__init__("lockout_confirmation_required")
        self.projects = tuple(projects)


class StaticKeyRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    algorithm: Literal["sha256"] = "sha256"
    digest: str
    key_id: str
    created_at: str

    @field_validator("digest")
    @classmethod
    def _digest(cls, value: str) -> str:
        if not isinstance(value, str) or _DIGEST_RE.fullmatch(value) is None:
            raise ValueError("digest must be 64 lowercase hexadecimal characters")
        return value

    @field_validator("key_id")
    @classmethod
    def _key_id(cls, value: str) -> str:
        if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{12}", value) is None:
            raise ValueError("key_id must be 12 lowercase hexadecimal characters")
        return value

    @model_validator(mode="after")
    def _consistent_id(self) -> StaticKeyRecord:
        if self.key_id != self.digest[:12]:
            raise ValueError("key_id must equal the first 12 digest characters")
        return self


class ProjectAuthenticationPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    oauth_mode: Literal["inherit", "enabled", "disabled"] = "inherit"
    static_key: StaticKeyRecord | None = None


class GlobalAuthenticationPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    oauth_enabled: bool = True
    static_key: StaticKeyRecord | None = None


class AuthenticationPolicy(BaseModel):
    version: Literal[1] = POLICY_VERSION
    revision: Annotated[int, Field(ge=0)] = 0
    global_: GlobalAuthenticationPolicy = Field(alias="global")
    projects: dict[str, ProjectAuthenticationPolicy] = Field(default_factory=dict)

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    @field_validator("projects")
    @classmethod
    def _valid_project_names(cls, value: dict[str, ProjectAuthenticationPolicy]):
        for name in value:
            if NAME_RE.fullmatch(name) is None:
                raise ValueError("projects contains an invalid project name")
        return value

    @model_validator(mode="after")
    def _canonical_rows(self) -> AuthenticationPolicy:
        # Rows with no override carry no authority and are not persisted.
        self.projects = {
            name: row for name, row in self.projects.items()
            if row.oauth_mode != "inherit" or row.static_key is not None
        }
        return self


class AuthPrincipal(BaseModel):
    """Immutable credential proof passed from authentication into authorization."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal[
        "oauth", "oauth_grant", "static_global", "static_project",
        "static_credential", "legacy_static",
        # 13.0 §7.3: the built-in self-test principal.  It is never stored in
        # any credential file; it exists only while the process runs in test
        # mode and is always scoped to the SELF_TEST_PROJECT_NAME project.
        "self_test",
    ]
    project_name: str | None = None
    key_id: str | None = None
    oauth_resource: str | None = None
    principal_id: str | None = None
    surface_kind: str | None = None
    surface_id: str | None = None
    credential_label: str | None = None


# --- 13.0 §7.3: test mode and the built-in test key -------------------------
#
# The key below is PUBLIC test material, deliberately checked into source.  It
# is not a secret and must never be written to `credentials-v2.json`, shown in
# Admin, issued, or revoked (AGENTS.md, "Self-Test project and the built-in
# test key").  It authenticates only while the process was started in test
# mode, only on an enabled connector's combined MCP route, and only for the
# `Self-Test` project.  Outside test mode it is a 401 like any other unknown
# bearer, whatever is stored in the credential files.
SELF_TEST_API_KEY = "cognita-self-test-only-v1"
SELF_TEST_PROJECT_NAME = "Self-Test"
SELF_TEST_PRINCIPAL_KIND = "self_test"
SELF_TEST_PRINCIPAL_LABEL = "built-in test key"
# One fixed window, no renewal and no deadline argument (§3, §7.3).
SELF_TEST_WINDOW_SECONDS = 30 * 60


def self_test_principal_id(connector_id: str) -> str:
    """Return the deterministic principal ID for a connector's test principal.

    Deterministic on purpose: the Workspace manager and the bridge both require
    a UUID, and a Workspace left behind by a killed run must be found and
    reused by the next run rather than orphaned.  This is a local choice, not a
    subsystem — there is no reservation registry and no collision ledger.
    """
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"cognita:self-test:{connector_id}"))


def is_self_test_principal(principal: Any) -> bool:
    """True when this principal is the built-in test principal."""
    kind = (
        principal.get("kind") if isinstance(principal, dict)
        else getattr(principal, "kind", None)
    )
    return kind == SELF_TEST_PRINCIPAL_KIND


def self_test_principal_matches(principal: Any, connector_id: str | None) -> bool:
    """True when the test principal's ID is the one this connector derives.

    A test principal carries the connector it was bound to at authentication
    inside its ID.  Re-deriving it at each authorization boundary means a
    handle borrowed from, or forged for, another connector fails closed.
    """
    if not connector_id:
        return False
    value = (
        principal.get("principal_id") if isinstance(principal, dict)
        else getattr(principal, "principal_id", None)
    )
    return bool(value) and str(value) == self_test_principal_id(str(connector_id))


class SelfTestModeGate:
    """Whether the built-in test key is currently accepted, and for how long.

    Test mode is decided once, at startup, from the process environment; it is
    never read from configuration and never saved.  Expiry is one
    ``time.monotonic()`` comparison against the startup reading.
    """

    def __init__(
        self,
        enabled: bool,
        *,
        window_seconds: float = SELF_TEST_WINDOW_SECONDS,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.clock = clock or time.monotonic
        self.enabled = bool(enabled)
        self.window_seconds = float(window_seconds)
        self.started_at = self.clock()
        self._expiry_logged = False
        if self.enabled:
            log.info(
                "test mode ACTIVE: the built-in test key is accepted on combined "
                "MCP routes for project %s until %s (%.0fs from startup)",
                SELF_TEST_PROJECT_NAME,
                (datetime.now(UTC) + timedelta(seconds=self.window_seconds))
                .isoformat(timespec="seconds"),
                self.window_seconds,
            )

    def remaining_seconds(self) -> float:
        if not self.enabled:
            return 0.0
        return max(0.0, self.window_seconds - (self.clock() - self.started_at))

    def active(self) -> bool:
        """True while the key is accepted.  Logs the transition to expired once."""
        if not self.enabled:
            return False
        if self.clock() - self.started_at < self.window_seconds:
            return True
        if not self._expiry_logged:
            self._expiry_logged = True
            log.info(
                "test mode EXPIRED after %.0fs; the built-in test key is now "
                "rejected until the next test-mode start",
                self.window_seconds,
            )
        return False


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _digest_key(raw_key: str) -> str:
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()


def generate_static_key() -> tuple[str, StaticKeyRecord]:
    """Generate a 256-bit static key and its persistable non-secret record."""
    raw = STATIC_KEY_PREFIX + secrets.token_urlsafe(32)
    digest = _digest_key(raw)
    return raw, StaticKeyRecord(
        digest=digest, key_id=digest[:12], created_at=_now()
    )


def is_static_key_candidate(value: object) -> bool:
    return isinstance(value, str) and value.startswith(STATIC_KEY_PREFIX)


def _policy_dump(policy: AuthenticationPolicy) -> str:
    data = policy.model_dump(mode="json", by_alias=True, exclude_none=True)
    return yaml.safe_dump(data, sort_keys=False, allow_unicode=True)


def _restrict_private_file(path: Path) -> None:
    """Apply owner/service-account-only permissions before secret-bearing writes."""
    if os.name != "nt":
        os.chmod(path, 0o600)
        return

    # Windows' chmod does not restrict ACLs.  Remove inherited grants and give
    # only the account running Cognita full control.  The ACL travels with the
    # same-directory temporary file across the atomic replace.
    system32 = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32"
    icacls = system32 / "icacls.exe"
    try:
        identity = _windows_process_identity(system32)
        completed = subprocess.run(
            [str(icacls), str(path), "/inheritance:r", "/grant:r", f"{identity}:(F)"],
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise AuthenticationPolicyUnavailable(
            "cannot restrict authentication policy permissions"
        ) from exc
    if completed.returncode != 0:
        raise AuthenticationPolicyUnavailable(
            "cannot restrict authentication policy permissions"
        )


def _windows_process_identity(system32: Path | None = None) -> str:
    root = system32 or (
        Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32"
    )
    try:
        result = subprocess.run(
            [str(root / "whoami.exe")],
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise AuthenticationPolicyUnavailable(
            "cannot identify authentication policy owner"
        ) from exc
    identity = result.stdout.strip()
    if result.returncode != 0 or not identity:
        raise AuthenticationPolicyUnavailable(
            "cannot identify authentication policy owner"
        )
    return identity


def _windows_permissions_are_private(path: Path) -> bool:
    system32 = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32"
    try:
        identity = _windows_process_identity(system32)
        result = subprocess.run(
            [str(system32 / "icacls.exe"), str(path)],
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError, AuthenticationPolicyUnavailable):
        return False
    ace_lines = [line.strip() for line in result.stdout.splitlines() if ":(" in line]
    return (
        result.returncode == 0
        and len(ace_lines) == 1
        and f"{identity}:(F)".casefold() in ace_lines[0].casefold()
    )


def redact_authentication_text(text: str) -> str:
    """Redact bearer values and generated_key fields from arbitrary log text."""
    if not isinstance(text, str):
        return text
    text = re.sub(
        r"(?i)(authorization\s*[:=]\s*bearer\s+|bearer\s+)[^\s,}\"']+",
        r"\1<redacted>", text,
    )
    text = re.sub(
        r"(?i)([\"']?generated_key[\"']?\s*[:=]\s*[\"']?)[^\s,}\"']+",
        r"\1<redacted>", text,
    )
    return text


class AuthenticationRedactionFilter(logging.Filter):
    """Formatter-independent last-mile redaction for parent-bound logs."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            rendered = record.getMessage()
            safe = redact_authentication_text(rendered)
            if safe != rendered:
                record.msg = safe
                record.args = ()
        except (AttributeError, TypeError, ValueError):
            # Logging must never make an authentication request fail.
            pass
        return True


def _record_view(record: StaticKeyRecord | None) -> dict | None:
    if record is None:
        return None
    return {"configured": True, "key_id": record.key_id, "created_at": record.created_at}


def _action(action: str | None) -> Literal["unchanged", "generate", "clear"]:
    if action is None:
        return "unchanged"
    if action not in {"unchanged", "generate", "clear"}:
        raise ValueError("static_key_action must be unchanged, generate, or clear")
    return action  # type: ignore[return-value]


class AuthenticationPolicyStore:
    """Thread-safe policy store with revision-checked atomic persistence."""

    def __init__(
        self,
        path: Path,
        *,
        project_names: list[str] | tuple[str, ...] | None = None,
        legacy_oauth_enabled: bool | None = None,
        ignored_legacy_credentials: int = 0,
        create: bool = True,
    ):
        self.path = Path(path)
        self._lock = threading.RLock()
        self._project_names = tuple(project_names or ())
        self._policy: AuthenticationPolicy
        if self.path.exists():
            self._policy = self._read()
        elif create:
            self._policy = AuthenticationPolicy(
                revision=0,
                **{"global": {"oauth_enabled": True if legacy_oauth_enabled is None else bool(legacy_oauth_enabled)}},
                projects={},
            )
            self._write(self._policy, backup=False)
            if ignored_legacy_credentials:
                log.warning("Ignored %d legacy test credential(s) during authentication migration", ignored_legacy_credentials)
        else:
            raise AuthenticationPolicyUnavailable("authentication policy file is missing")

    @classmethod
    def load_or_migrate(cls, path: Path, **kwargs) -> AuthenticationPolicyStore:
        return cls(path, **kwargs)

    def _read(self) -> AuthenticationPolicy:
        try:
            metadata = self.path.stat()
            if metadata.st_size > MAX_POLICY_BYTES:
                raise AuthenticationPolicyUnavailable("authentication policy is oversized")
            # Persisted credentials fail closed unless the file is accessible
            # only to the current service identity (Windows) or owner (POSIX).
            if os.name == "nt":
                if not _windows_permissions_are_private(self.path):
                    raise AuthenticationPolicyUnavailable(
                        "authentication policy permissions are too broad"
                    )
            elif metadata.st_mode & 0o077:
                raise AuthenticationPolicyUnavailable("authentication policy permissions are too broad")
            raw = self.path.read_bytes()
            data = yaml.safe_load(raw) or {}
            if not isinstance(data, dict):
                raise TypeError("policy root must be a mapping")
            return AuthenticationPolicy.model_validate(data)
        except AuthenticationPolicyUnavailable:
            raise
        except (OSError, ValueError, TypeError, yaml.YAMLError) as exc:
            raise AuthenticationPolicyUnavailable(
                f"authentication policy cannot be loaded ({type(exc).__name__})"
            ) from exc

    def _write(self, policy: AuthenticationPolicy, *, backup: bool = True) -> None:
        parent = self.path.parent
        parent.mkdir(parents=True, exist_ok=True)
        payload = _policy_dump(policy).encode("utf-8")
        if backup and self.path.exists():
            backup_path = self.path.with_name(self.path.name + ".bak")
            backup_fd = -1
            backup_temporary: str | None = None
            try:
                backup_fd, backup_temporary = tempfile.mkstemp(
                    prefix=f".{backup_path.name}.", suffix=".tmp", dir=parent
                )
                _restrict_private_file(Path(backup_temporary))
                with os.fdopen(backup_fd, "wb") as stream:
                    backup_fd = -1
                    stream.write(self.path.read_bytes())
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(backup_temporary, backup_path)
                backup_temporary = None
            except (OSError, AuthenticationPolicyUnavailable) as exc:
                raise AuthenticationPolicyUnavailable("authentication policy backup failed") from exc
            finally:
                if backup_fd >= 0:
                    os.close(backup_fd)
                if backup_temporary:
                    try:
                        os.unlink(backup_temporary)
                    except OSError:
                        pass
        fd = -1
        temporary: str | None = None
        try:
            fd, temporary = tempfile.mkstemp(prefix=f".{self.path.name}.", suffix=".tmp", dir=parent)
            _restrict_private_file(Path(temporary))
            with os.fdopen(fd, "wb") as stream:
                fd = -1
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            temporary = None
            _restrict_private_file(self.path)
            try:
                directory_fd = os.open(parent, os.O_RDONLY)
            except OSError:
                directory_fd = -1
            if directory_fd >= 0:
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
        except OSError as exc:
            raise AuthenticationPolicyUnavailable("authentication policy write failed") from exc
        finally:
            if fd >= 0:
                os.close(fd)
            if temporary:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass

    def snapshot(self) -> AuthenticationPolicy:
        with self._lock:
            return AuthenticationPolicy.model_validate(
                copy.deepcopy(self._policy.model_dump(mode="python", by_alias=True))
            )

    @property
    def revision(self) -> int:
        with self._lock:
            return self._policy.revision

    def set_project_names(self, project_names) -> None:
        """Publish the registry membership used to keep orphan keys inert."""
        with self._lock:
            self._project_names = tuple(project_names)

    def orphaned_project_entries(self, project_names=None) -> list[str]:
        current = set(self._project_names if project_names is None else project_names)
        with self._lock:
            return sorted(set(self._policy.projects) - current)

    def effective_oauth(self, project_name: str) -> bool:
        with self._lock:
            row = self._policy.projects.get(project_name)
            if row is None or row.oauth_mode == "inherit":
                return self._policy.global_.oauth_enabled
            return row.oauth_mode == "enabled"

    def effective_static_key(self, project_name: str) -> StaticKeyRecord | None:
        with self._lock:
            row = self._policy.projects.get(project_name)
            if row is not None and row.static_key is not None:
                return row.static_key
            return self._policy.global_.static_key

    def oauth_runtime_required(self, project_names=None) -> bool:
        """Whether the parent must keep the OAuth child available."""
        names = self._project_names if project_names is None else tuple(project_names)
        return any(self.effective_oauth(name) for name in names)

    def view(self, project_names=None) -> dict:
        names = sorted(set(self._project_names if project_names is None else project_names))
        with self._lock:
            policy = self._policy
            projects = []
            for name in names:
                row = policy.projects.get(name, ProjectAuthenticationPolicy())
                effective_key = row.static_key or policy.global_.static_key
                source = "project" if row.static_key is not None else ("global" if effective_key else "none")
                projects.append({
                    "name": name,
                    "oauth_mode": row.oauth_mode,
                    "effective_oauth_enabled": self.effective_oauth(name),
                    "static_key_override": _record_view(row.static_key),
                    "effective_static_key_source": source,
                    "effective_static_key_id": effective_key.key_id if effective_key else None,
                    "locked_out": not self.effective_oauth(name) and effective_key is None,
                })
            return {
                "version": policy.version,
                "revision": policy.revision,
                "global": {
                    "oauth_enabled": policy.global_.oauth_enabled,
                    "static_key": _record_view(policy.global_.static_key),
                },
                "projects": projects,
                "orphaned_project_entries": self.orphaned_project_entries(names),
            }

    def verify_static_key(self, candidate: str, project_names=None) -> AuthPrincipal | None:
        """Scan every configured digest before returning a matching principal."""
        names = set(self._project_names if project_names is None else project_names)
        digest = _digest_key(candidate) if isinstance(candidate, str) else "0" * 64
        match: AuthPrincipal | None = None
        with self._lock:
            records: list[tuple[str, StaticKeyRecord]] = []
            if self._policy.global_.static_key is not None:
                records.append(("", self._policy.global_.static_key))
            for name in sorted(self._policy.projects):
                row = self._policy.projects[name]
                if name in names and row.static_key is not None:
                    records.append((name, row.static_key))
            for name, record in records:
                equal = hmac.compare_digest(digest, record.digest)
                if equal:
                    match = AuthPrincipal(
                        kind="static_project" if name else "static_global",
                        project_name=name or None,
                        key_id=record.key_id,
                    )
        return match

    def _candidate(self, expected_revision: int) -> AuthenticationPolicy:
        if expected_revision != self._policy.revision:
            raise AuthenticationRevisionConflict(self.view())
        return self.snapshot()

    def _apply_key(
        self, record: StaticKeyRecord | None, action: str
    ) -> tuple[StaticKeyRecord | None, str | None]:
        if action == "unchanged":
            return record, None
        if action == "clear":
            return None, None
        configured = {
            item.digest
            for item in (
                [self._policy.global_.static_key]
                + [row.static_key for row in self._policy.projects.values()]
            )
            if item is not None
        }
        for _ in range(4):
            raw, generated = generate_static_key()
            if generated.digest not in configured:
                return generated, raw
        raise AuthenticationPolicyUnavailable("could not generate a unique static key")

    def _commit(self, candidate: AuthenticationPolicy, *, generated_key: str | None = None) -> dict:
        if candidate.model_dump(mode="json", by_alias=True) == self._policy.model_dump(mode="json", by_alias=True):
            output = self.view()
            if generated_key:
                output["generated_key"] = generated_key
            return output
        candidate.revision = self._policy.revision + 1
        self._write(candidate)
        self._policy = candidate
        output = self.view()
        if generated_key:
            output["generated_key"] = generated_key
        return output

    def _ensure_no_lockout(self, candidate: AuthenticationPolicy, names, confirm_lockout: bool) -> None:
        locked = []
        for name in sorted(set(names)):
            row = candidate.projects.get(name, ProjectAuthenticationPolicy())
            if not (candidate.global_.oauth_enabled if row.oauth_mode == "inherit" else row.oauth_mode == "enabled") and not (row.static_key or candidate.global_.static_key):
                locked.append(name)
        if locked and not confirm_lockout:
            raise AuthenticationLockoutConfirmationRequired(locked)

    def mutate_global(
        self, *, expected_revision: int, oauth_enabled: bool | None = None,
        static_key_action: str = "unchanged", confirm_lockout: bool = False,
        project_names=None,
    ) -> dict:
        action = _action(static_key_action)
        with self._lock:
            self._policy = self._read()
            candidate = self._candidate(expected_revision)
            generated = None
            candidate.global_.oauth_enabled = candidate.global_.oauth_enabled if oauth_enabled is None else bool(oauth_enabled)
            candidate.global_.static_key, generated = self._apply_key(candidate.global_.static_key, action)
            names = self._project_names if project_names is None else tuple(project_names)
            self._ensure_no_lockout(candidate, names, confirm_lockout)
            return self._commit(candidate, generated_key=generated)

    def mutate_project(
        self, name: str, *, expected_revision: int, oauth_mode: str | None = None,
        static_key_action: str = "unchanged", confirm_lockout: bool = False,
        project_names=None,
    ) -> dict:
        if NAME_RE.fullmatch(name) is None:
            raise KeyError("unknown project")
        if self._project_names and name not in self._project_names:
            raise KeyError("unknown project")
        action = _action(static_key_action)
        with self._lock:
            self._policy = self._read()
            candidate = self._candidate(expected_revision)
            row = candidate.projects.get(name, ProjectAuthenticationPolicy())
            if oauth_mode is not None and oauth_mode not in {"inherit", "enabled", "disabled"}:
                raise ValueError("oauth_mode must be inherit, enabled, or disabled")
            row.oauth_mode = row.oauth_mode if oauth_mode is None else oauth_mode
            row.static_key, generated = self._apply_key(row.static_key, action)
            if row.oauth_mode == "inherit" and row.static_key is None:
                candidate.projects.pop(name, None)
            else:
                candidate.projects[name] = row
            names = self._project_names if project_names is None else tuple(project_names)
            self._ensure_no_lockout(candidate, names, confirm_lockout)
            return self._commit(candidate, generated_key=generated)

    def generate_global_static_key(
        self, *, expected_revision: int, project_names=None,
    ) -> dict:
        """Replace the global static key and return its plaintext exactly once."""
        with self._lock:
            self._policy = self._read()
            candidate = self._candidate(expected_revision)
            candidate.global_.static_key, generated = self._apply_key(
                candidate.global_.static_key, "generate"
            )
            return self._commit(candidate, generated_key=generated)

    def generate_project_static_key(
        self, name: str, *, expected_revision: int, project_names=None,
    ) -> dict:
        """Replace one project's static-key override, never creating an orphan row."""
        if NAME_RE.fullmatch(name) is None:
            raise KeyError("unknown project")
        names = tuple(self._project_names if project_names is None else project_names)
        if names and name not in names:
            raise KeyError("unknown project")
        with self._lock:
            self._policy = self._read()
            candidate = self._candidate(expected_revision)
            row = candidate.projects.get(name, ProjectAuthenticationPolicy())
            row.static_key, generated = self._apply_key(row.static_key, "generate")
            candidate.projects[name] = row
            return self._commit(candidate, generated_key=generated)

    def revoke_global_static_key(
        self, *, expected_revision: int, confirm_lockout: bool = False,
        project_names=None, lockout_project_names=None,
    ) -> dict:
        """Remove only the global key, enforcing the normal lockout guard."""
        with self._lock:
            self._policy = self._read()
            candidate = self._candidate(expected_revision)
            if candidate.global_.static_key is None:
                return self.view()
            candidate.global_.static_key = None
            names = self._project_names if lockout_project_names is None else tuple(lockout_project_names)
            self._ensure_no_lockout(candidate, names, confirm_lockout)
            return self._commit(candidate)

    def revoke_project_static_key(
        self, name: str, *, expected_revision: int, confirm_lockout: bool = False,
        project_names=None, lockout_project_names=None,
    ) -> dict:
        """Remove only a project's override; an inherited global key is untouched."""
        if NAME_RE.fullmatch(name) is None:
            raise KeyError("unknown project")
        names = tuple(self._project_names if project_names is None else project_names)
        if names and name not in names:
            raise KeyError("unknown project")
        with self._lock:
            self._policy = self._read()
            candidate = self._candidate(expected_revision)
            row = candidate.projects.get(name)
            if row is None or row.static_key is None:
                return self.view()
            row.static_key = None
            if row.oauth_mode == "inherit":
                candidate.projects.pop(name, None)
            else:
                candidate.projects[name] = row
            names = self._project_names if lockout_project_names is None else tuple(lockout_project_names)
            self._ensure_no_lockout(candidate, names, confirm_lockout)
            return self._commit(candidate)

    def remove_orphan(self, name: str, *, expected_revision: int | None = None) -> dict:
        with self._lock:
            self._policy = self._read()
            if name not in self._policy.projects:
                return self.view()
            expected = self._policy.revision if expected_revision is None else expected_revision
            candidate = self._candidate(expected)
            candidate.projects.pop(name, None)
            return self._commit(candidate)

    def remove_project(self, name: str, *, expected_revision: int | None = None) -> dict:
        return self.remove_orphan(name, expected_revision=expected_revision)


def migrate_authentication_policy(
    path: Path, *, legacy_oauth_enabled: bool | None = None,
    ignored_legacy_credentials: int = 0, project_names=(),
) -> AuthenticationPolicyStore:
    """Create the additive 11.0 policy once, preserving explicit legacy OAuth."""
    return AuthenticationPolicyStore(
        path, project_names=tuple(project_names), legacy_oauth_enabled=legacy_oauth_enabled,
        ignored_legacy_credentials=ignored_legacy_credentials,
    )


# ---------------------------------------------------------------------------
# Cognita 12 credential-v2 policy


class CredentialPolicyError(AuthenticationPolicyError):
    """Base class for named credential and master-key failures."""


class CredentialNotFound(CredentialPolicyError):
    pass


class CredentialRevoked(CredentialPolicyError):
    pass


class CredentialCapacityExceeded(CredentialPolicyError):
    pass


class CredentialRevealDenied(CredentialPolicyError):
    pass


class CredentialMasterKeyUnavailable(CredentialPolicyError):
    pass


class CredentialSurfaceConflict(CredentialPolicyError):
    pass


class CredentialWorkspaceConflict(CredentialPolicyError):
    """The Workspace target changed after the Admin confirmation read."""
    pass


def _urlsafe(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _decode_urlsafe(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _uuid_text(value: str | uuid.UUID) -> str:
    try:
        result = uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError) as exc:
        raise ValueError("value must be a UUID") from exc
    return str(result)


def _credential_aad(credential_id: str, surface_kind: str, surface_id: str) -> bytes:
    # Delimiters and all identifiers are canonicalized before encryption.  This
    # prevents ciphertext swaps between surfaces, records, or policy formats.
    return (
        f"cognita-credential|policy={V2_POLICY_VERSION}|format={V2_AAD_VERSION}|"
        f"kind={surface_kind}|surface={surface_id}|credential={credential_id}"
    ).encode("ascii")


def _write_json_atomic(path: Path, payload: object, *, private: bool = True) -> None:
    parent = path.parent
    parent.mkdir(parents=True, exist_ok=True)
    fd = -1
    temporary: str | None = None
    try:
        fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=parent)
        tmp = Path(temporary)
        if private:
            _restrict_private_file(tmp)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            fd = -1
            json.dump(payload, stream, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
        if private:
            _restrict_private_file(path)
        try:
            directory_fd = os.open(parent, os.O_RDONLY)
        except OSError:
            directory_fd = -1
        if directory_fd >= 0:
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        temporary = None
    except OSError as exc:
        raise CredentialPolicyError("credential policy write failed") from exc
    finally:
        if fd >= 0:
            os.close(fd)
        if temporary:
            try:
                os.unlink(temporary)
            except OSError:
                pass


class MasterKeyRing:
    """Owner-only generation directory with an atomic active manifest."""

    def __init__(self, directory: Path, *, create: bool = True):
        self.directory = Path(directory)
        self.manifest_path = self.directory / "manifest.json"
        self._lock = threading.RLock()
        if self.manifest_path.exists():
            self._load_manifest()
        elif create:
            self.directory.mkdir(parents=True, exist_ok=True)
            generation = self._new_generation()
            self._write_manifest({"schema": 1, "active_generation": generation, "generations": [generation]})
        else:
            raise CredentialMasterKeyUnavailable("master-key manifest is missing")

    def _new_generation(self) -> str:
        generation = f"gen-{uuid.uuid4().hex}"
        key_path = self.directory / f"{generation}.key"
        fd = -1
        try:
            fd = os.open(
                key_path,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0),
                0o600,
            )
            os.write(fd, secrets.token_bytes(32))
            os.fsync(fd)
        except OSError as exc:
            raise CredentialMasterKeyUnavailable("master-key generation could not be created") from exc
        finally:
            if fd >= 0:
                os.close(fd)
        try:
            _restrict_private_file(key_path)
        except AuthenticationPolicyUnavailable as exc:
            raise CredentialMasterKeyUnavailable("master-key permissions could not be restricted") from exc
        return generation

    def _load_manifest(self) -> dict:
        try:
            data = json.loads(self.manifest_path.read_text(encoding="utf-8"))
            active = data.get("active_generation")
            generations = data.get("generations") or ([active] if active else [])
            if data.get("schema") != 1 or not isinstance(active, str) or active not in generations:
                raise ValueError
            for generation in generations:
                if not isinstance(generation, str) or not re.fullmatch(r"gen-[0-9a-f]{32}", generation):
                    raise ValueError
                key = self.directory / f"{generation}.key"
                if not key.is_file() or key.stat().st_size != 32:
                    raise ValueError
            self._manifest = {"schema": 1, "active_generation": active, "generations": list(generations)}
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            raise CredentialMasterKeyUnavailable("master-key manifest is invalid") from exc

    def _write_manifest(self, data: dict) -> None:
        _write_json_atomic(self.manifest_path, data, private=False)
        self._manifest = data

    @property
    def active_generation(self) -> str:
        with self._lock:
            return self._manifest["active_generation"]

    def key(self, generation: str | None = None) -> bytes:
        with self._lock:
            selected = generation or self._manifest["active_generation"]
            if selected not in self._manifest["generations"]:
                raise CredentialMasterKeyUnavailable("master-key generation is retired")
            try:
                value = (self.directory / f"{selected}.key").read_bytes()
            except OSError as exc:
                raise CredentialMasterKeyUnavailable("master-key generation is unavailable") from exc
            if len(value) != 32:
                raise CredentialMasterKeyUnavailable("master-key generation has invalid size")
            return value

    def create_generation(self, *, activate: bool = True) -> str:
        with self._lock:
            generation = self._new_generation()
            generations = [*self._manifest["generations"], generation]
            self._write_manifest({
                "schema": 1,
                "active_generation": generation if activate else self._manifest["active_generation"],
                "generations": generations,
            })
            return generation

    def activate(self, generation: str) -> None:
        with self._lock:
            if generation not in self._manifest["generations"]:
                raise CredentialMasterKeyUnavailable("unknown master-key generation")
            self._write_manifest({**self._manifest, "active_generation": generation})

    def retire(self, generation: str) -> None:
        with self._lock:
            if generation == self._manifest["active_generation"]:
                raise CredentialMasterKeyUnavailable("cannot retire active master-key generation")
            generations = [item for item in self._manifest["generations"] if item != generation]
            if len(generations) == len(self._manifest["generations"]):
                return
            self._write_manifest({**self._manifest, "generations": generations})
            try:
                (self.directory / f"{generation}.key").unlink()
            except OSError as exc:
                raise CredentialMasterKeyUnavailable("master-key generation removal failed") from exc


@dataclass(frozen=True, slots=True)
class CredentialPrincipal:
    principal_id: str
    kind: Literal["oauth_grant", "static_credential", "legacy_static"]
    surface_kind: str
    surface_id: str
    credential_label: str | None = None
    credential_id: str | None = None
    workspace_enabled: bool = False
    surface_slug: str | None = None
    legacy_scope: str | None = None

    def as_auth_principal(self) -> AuthPrincipal:
        """Adapt to the immutable legacy gateway principal shape when needed."""
        kind = "oauth" if self.kind == "oauth_grant" else self.kind
        return AuthPrincipal(
            kind=kind, key_id=self.credential_id, oauth_resource=self.surface_id,
            principal_id=self.principal_id, surface_kind=self.surface_kind,
            surface_id=self.surface_id, credential_label=self.credential_label,
            project_name=(
                self.legacy_scope.removeprefix("project:")
                if self.kind == "legacy_static"
                and isinstance(self.legacy_scope, str)
                and self.legacy_scope.startswith("project:")
                else None
            ),
        )


@dataclass(frozen=True, slots=True)
class CredentialRecord:
    credential_id: str
    principal_id: str
    surface_kind: str
    surface_id: str
    label: str
    key_id: str
    digest: str
    created_at: str
    status: Literal["active", "revoked", "tombstone", "legacy"]
    rotated_at: str | None = None
    encrypted_secret: dict | None = None
    revoked_at: str | None = None
    deleted_at: str | None = None
    workspace_retention: Literal["keep", "delete_now", "normal"] = "normal"
    legacy_scope: str | None = None
    surface_slug: str | None = None


def parse_v2_static_key(value: str) -> tuple[str, str] | None:
    """Return the embedded credential UUID and random component for a strict token."""
    if not isinstance(value, str):
        return None
    match = _V2_TOKEN_RE.fullmatch(value)
    if match is None:
        return None
    try:
        parsed_id = uuid.UUID(bytes=_decode_urlsafe(match.group(1)))
        if parsed_id.version != 4 or parsed_id.variant != uuid.RFC_4122:
            return None
        credential_id = str(parsed_id)
    except (ValueError, UnicodeError):
        return None
    if len(_urlsafe(uuid.UUID(credential_id).bytes)) != 22:
        return None
    return credential_id, match.group(2)


def is_v2_static_key_candidate(value: object) -> bool:
    return isinstance(value, str) and value.startswith(V2_STATIC_KEY_PREFIX)


class AdminPasswordGate:
    """Password proof service for create/reveal and other high-trust controls."""

    def __init__(
        self,
        password_hash: str | None,
        *,
        ttl_seconds: int = 300,
        verifier: Callable[[str], bool] | None = None,
    ):
        self.password_hash = password_hash
        self._verifier = verifier
        self.ttl_seconds = ttl_seconds
        self._proofs: dict[str, tuple[str, float, str]] = {}
        self._lock = threading.RLock()

    def _check(self, password: str) -> bool:
        if self._verifier is not None:
            try:
                return bool(self._verifier(password))
            except Exception:
                return False
        if not self.password_hash or not isinstance(password, str):
            return False
        try:
            from argon2 import PasswordHasher
            return bool(PasswordHasher().verify(self.password_hash, password))
        except Exception:
            return False

    def verify_password(self, password: str) -> None:
        if not self._check(password):
            raise CredentialRevealDenied("current administrator password is required")

    def issue(self, password: str, *, operation: str) -> str:
        if not self._check(password):
            raise CredentialRevealDenied("current administrator password is required")
        token = secrets.token_urlsafe(32)
        with self._lock:
            self._proofs[token] = (operation, __import__("time").monotonic() + self.ttl_seconds, token)
        return token

    def consume(self, proof: str, *, operation: str) -> None:
        with self._lock:
            row = self._proofs.pop(proof, None)
        if row is None or row[0] != operation or row[1] < __import__("time").monotonic():
            raise CredentialRevealDenied("reauthentication proof is invalid or expired")


class CredentialPolicyStore:
    """Durable v2 named-credential policy and reveal/encryption service.

    The store is intentionally transport-neutral.  Gateway and Admin callers get
    immutable ``CredentialPrincipal`` values and never need to understand storage,
    ciphertext, or master-key generations.
    """

    def __init__(
        self,
        path: Path,
        *,
        master_key_dir: Path | None = None,
        admin_password_hash: str | None = None,
        admin_password_verifier: Callable[[str], bool] | None = None,
        create: bool = True,
    ):
        self.path = Path(path)
        self.master_keys = MasterKeyRing(master_key_dir or self.path.parent / "master-keys", create=create)
        self.password_gate = AdminPasswordGate(
            admin_password_hash,
            verifier=admin_password_verifier,
        )
        self._lock = threading.RLock()
        if self.path.exists():
            self._data = self._read()
        elif create:
            self._data = {"version": V2_POLICY_VERSION, "revision": 0, "credentials": [], "legacy": [], "trusted_secrets": []}
            self._write(self._data)
        else:
            raise CredentialPolicyError("credential policy file is missing")
        # Older tombstones predate the explicit Workspace retention choice.
        # Backfill conservatively to normal retention; never infer delete-now.
        changed = False
        for row in self._data.get("credentials", []):
            if row.get("status") == "tombstone" and row.get("workspace_retention") not in {"keep", "delete_now", "normal"}:
                row["workspace_retention"] = "normal"
                changed = True
        if changed:
            self._save(self._data)

    def _read(self) -> dict:
        try:
            metadata = self.path.stat()
            if metadata.st_size > MAX_POLICY_BYTES:
                raise CredentialPolicyError("credential policy exceeds the bounded read limit")
            if os.name == "nt" and not _windows_permissions_are_private(self.path):
                raise CredentialPolicyError("credential policy permissions are too broad")
            if os.name != "nt" and metadata.st_mode & 0o077:
                raise CredentialPolicyError("credential policy permissions are too broad")
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if data.get("version") != V2_POLICY_VERSION or not isinstance(data.get("credentials"), list):
                raise ValueError
            if not isinstance(data.get("legacy", []), list):
                raise ValueError
            if not isinstance(data.get("trusted_secrets", []), list):
                raise ValueError
            return data
        except CredentialPolicyError:
            raise
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            raise CredentialPolicyError("credential policy is invalid") from exc

    def _write(self, data: dict) -> None:
        _write_json_atomic(self.path, data, private=True)

    @staticmethod
    def _record(row: dict) -> CredentialRecord:
        return CredentialRecord(**{**row, "surface_slug": row.get("surface_slug")})

    def snapshot(self) -> tuple[CredentialRecord, ...]:
        with self._lock:
            return tuple(self._record(row) for row in self._data["credentials"])

    def list_surface(self, surface_kind: str, surface_id: str, *, include_tombstones: bool = False) -> tuple[CredentialRecord, ...]:
        surface_id = _uuid_text(surface_id)
        rows = [r for r in self.snapshot() if r.surface_kind == surface_kind and r.surface_id == surface_id]
        if not include_tombstones:
            rows = [r for r in rows if r.status != "tombstone"]
        return tuple(rows)

    def admission_status(self, credential_id: str) -> bool | None:
        """Return static status, or ``None`` when another identity owns the ID.

        OAuth grant principals live in the child-owned OAuth store.  Returning
        ``None`` for an ID absent from this static store lets the already
        authenticated gateway principal proceed without making the parent read
        the child database; known static tombstones remain a hard denial.
        """
        credential_id = _uuid_text(credential_id)
        with self._lock:
            self._data = self._read()
            for row in self._data["credentials"]:
                if row.get("credential_id") == credential_id:
                    return row.get("status") == "active"
            for row in self._data.get("legacy", []):
                if row.get("principal_id") == credential_id:
                    return not bool(row.get("retired", False))
            return None

    def is_active(self, credential_id: str) -> bool:
        """Compatibility boolean for callers that require known static IDs."""
        return self.admission_status(credential_id) is True

    def _save(self, data: dict) -> None:
        data = copy.deepcopy(data)
        data["revision"] = int(self._data.get("revision", 0)) + 1
        self._write(data)
        self._data = data

    def _check_revision(self, expected_revision: int | None) -> None:
        if expected_revision is None:
            return
        current = int(self._data.get("revision", 0))
        if isinstance(expected_revision, bool) or expected_revision != current:
            raise AuthenticationRevisionConflict({"revision": current})

    @property
    def revision(self) -> int:
        with self._lock:
            self._data = self._read()
            return int(self._data.get("revision", 0))

    def _find(self, credential_id: str) -> tuple[int, dict]:
        credential_id = _uuid_text(credential_id)
        for index, row in enumerate(self._data["credentials"]):
            if row.get("credential_id") == credential_id:
                return index, row
        raise CredentialNotFound("credential not found")

    def _require_password(self, password: str | None, *, operation: str) -> None:
        if (
            self.password_gate.password_hash is None
            and self.password_gate._verifier is None
        ):
            raise CredentialRevealDenied("administrator password must be configured")
        self.password_gate.verify_password(password or "")

    def add_credential(self, surface_kind: str, surface_id: str, label: str, *, surface_slug: str | None = None, password: str | None = None, expected_revision: int | None = None) -> tuple[CredentialRecord, str]:
        surface_id = _uuid_text(surface_id)
        if not isinstance(surface_kind, str) or not surface_kind or len(surface_kind) > 40:
            raise ValueError("surface_kind is invalid")
        if surface_slug is not None and (not isinstance(surface_slug, str) or not 1 <= len(surface_slug) <= 160 or "/" in surface_slug or "\\" in surface_slug):
            raise ValueError("surface_slug is invalid")
        if not isinstance(label, str) or not 1 <= len(label) <= 120 or not label.strip():
            raise ValueError("credential label must contain 1-120 characters")
        self._require_password(password, operation="credential:add")
        with self._lock:
            self._data = self._read()
            self._check_revision(expected_revision)
            active = [r for r in self._data["credentials"] if r["surface_kind"] == surface_kind and r["surface_id"] == surface_id and r["status"] == "active"]
            if len(active) >= V2_MAX_ACTIVE_PER_SURFACE:
                raise CredentialCapacityExceeded("surface has reached its active credential limit")
            if any(r["label"].casefold() == label.casefold() for r in self._data["credentials"] if r["surface_kind"] == surface_kind and r["surface_id"] == surface_id):
                raise CredentialSurfaceConflict("credential label already exists on this surface")
            credential_id = str(uuid.uuid4())
            raw = f"{V2_STATIC_KEY_PREFIX}{_urlsafe(uuid.UUID(credential_id).bytes)}.{_urlsafe(secrets.token_bytes(V2_SECRET_BYTES))}"
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM
            generation = self.master_keys.active_generation
            nonce = secrets.token_bytes(12)
            ciphertext = AESGCM(self.master_keys.key(generation)).encrypt(nonce, raw.encode(), _credential_aad(credential_id, surface_kind, surface_id))
            now = _now()
            row = {
                "credential_id": credential_id, "principal_id": credential_id,
                "surface_kind": surface_kind, "surface_id": surface_id, "surface_slug": surface_slug, "label": label,
                "key_id": credential_id, "digest": _digest_key(raw), "created_at": now,
                "status": "active", "encrypted_secret": {"algorithm": "AES-256-GCM", "aad_version": V2_AAD_VERSION, "generation": generation, "nonce": _urlsafe(nonce), "ciphertext": _urlsafe(ciphertext)},
                "revoked_at": None, "deleted_at": None, "workspace_retention": "normal", "legacy_scope": None,
            }
            candidate = copy.deepcopy(self._data)
            candidate["credentials"].append(row)
            self._save(candidate)
            return self._record(row), raw

    def verify(self, presented: str, *, surface_kind: str, surface_id: str, surface_slug: str | None = None) -> CredentialPrincipal | None:
        parsed = parse_v2_static_key(presented)
        if parsed is None:
            return self._verify_legacy(presented, surface_kind=surface_kind, surface_id=surface_id, surface_slug=surface_slug)
        credential_id, _ = parsed
        surface_id = _uuid_text(surface_id)
        digest = _digest_key(presented)
        with self._lock:
            self._data = self._read()
            row = next((item for item in self._data["credentials"] if item.get("credential_id") == credential_id), None)
            if row is None or row.get("surface_kind") != surface_kind or row.get("surface_id") != surface_id or (surface_slug is not None and row.get("surface_slug") != surface_slug):
                return None
            if row.get("status") != "active" or not hmac.compare_digest(str(row.get("digest", "")), digest):
                return None
            return CredentialPrincipal(credential_id, "static_credential", surface_kind, surface_id, row["label"], credential_id, True, row.get("surface_slug"))

    def _verify_legacy(self, presented: str, *, surface_kind: str, surface_id: str, surface_slug: str | None = None) -> CredentialPrincipal | None:
        digest = _digest_key(presented) if isinstance(presented, str) else "0" * 64
        surface_id = _uuid_text(surface_id)
        with self._lock:
            for row in self._data.get("legacy", []):
                if row.get("surface_kind") == surface_kind and row.get("surface_id") == surface_id and (surface_slug is None or row.get("surface_slug") == surface_slug) and hmac.compare_digest(row.get("digest", ""), digest) and not row.get("retired", False):
                    return CredentialPrincipal(row["principal_id"], "legacy_static", surface_kind, surface_id, row.get("label"), None, False, row.get("surface_slug"), row.get("scope"))
        return None

    def verify_static_key(self, presented: str, *, surface_kind: str, surface_id: str, surface_slug: str | None = None) -> CredentialPrincipal | None:
        """Transport-neutral alias used by gateway authentication adapters."""
        return self.verify(presented, surface_kind=surface_kind, surface_id=surface_id, surface_slug=surface_slug)

    def verify_for_surface(self, presented: str, *, surface_kind: str, surface_id: str, surface_slug: str) -> CredentialPrincipal | None:
        """Verify family, immutable surface UUID, and route slug together."""
        if not isinstance(surface_slug, str) or not 1 <= len(surface_slug) <= 160 or "/" in surface_slug:
            return None
        return self.verify(presented, surface_kind=surface_kind, surface_id=surface_id, surface_slug=surface_slug)

    def begin_reveal(
        self, credential_id: str, password: str, *, expected_revision: int | None = None,
    ) -> str:
        with self._lock:
            self._data = self._read()
            self._check_revision(expected_revision)
            self._find(credential_id)
        return self.password_gate.issue(password, operation=f"credential:reveal:{_uuid_text(credential_id)}")

    def reveal(self, credential_id: str, proof: str) -> str:
        credential_id = _uuid_text(credential_id)
        self.password_gate.consume(proof, operation=f"credential:reveal:{credential_id}")
        with self._lock:
            _, row = self._find(credential_id)
            if row["status"] != "active" or not row.get("encrypted_secret"):
                raise CredentialRevealDenied("credential is not revealable")
            encrypted = row["encrypted_secret"]
            try:
                from cryptography.hazmat.primitives.ciphers.aead import AESGCM
                raw = AESGCM(self.master_keys.key(encrypted["generation"])).decrypt(_decode_urlsafe(encrypted["nonce"]), _decode_urlsafe(encrypted["ciphertext"]), _credential_aad(credential_id, row["surface_kind"], row["surface_id"]))
                value = raw.decode("utf-8")
            except Exception as exc:
                raise CredentialRevealDenied("credential recovery material is unavailable") from exc
            if not hmac.compare_digest(_digest_key(value), row["digest"]):
                raise CredentialRevealDenied("credential recovery material failed verification")
            return value

    def rotate_secret(self, credential_id: str, *, password: str | None = None, expected_revision: int | None = None) -> tuple[CredentialRecord, str]:
        credential_id = _uuid_text(credential_id)
        self._require_password(password, operation="credential:rotate")
        with self._lock:
            self._data = self._read()
            self._check_revision(expected_revision)
            index, row = self._find(credential_id)
            if row["status"] != "active":
                raise CredentialRevoked("credential is not active")
            raw = f"{V2_STATIC_KEY_PREFIX}{_urlsafe(uuid.UUID(credential_id).bytes)}.{_urlsafe(secrets.token_bytes(V2_SECRET_BYTES))}"
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM
            generation = self.master_keys.active_generation
            nonce = secrets.token_bytes(12)
            row = copy.deepcopy(row)
            row["digest"] = _digest_key(raw)
            row["encrypted_secret"] = {"algorithm": "AES-256-GCM", "aad_version": V2_AAD_VERSION, "generation": generation, "nonce": _urlsafe(nonce), "ciphertext": _urlsafe(AESGCM(self.master_keys.key(generation)).encrypt(nonce, raw.encode(), _credential_aad(credential_id, row["surface_kind"], row["surface_id"]))) }
            row["rotated_at"] = _now()
            candidate = copy.deepcopy(self._data)
            candidate["credentials"][index] = row
            self._save(candidate)
            return self._record(row), raw

    def revoke(self, credential_id: str, *, expected_revision: int | None = None) -> CredentialRecord:
        return self._transition(credential_id, "revoked", expected_revision=expected_revision)

    def delete(self, credential_id: str, *, workspace_retention: Literal["keep", "delete_now", "normal"] = "normal", expected_revision: int | None = None) -> CredentialRecord:
        if workspace_retention not in {"keep", "delete_now", "normal"}:
            raise ValueError("invalid workspace retention")
        return self._transition(credential_id, "tombstone", workspace_retention=workspace_retention, expected_revision=expected_revision)

    def _transition(self, credential_id: str, status: str, *, workspace_retention: str = "normal", expected_revision: int | None = None) -> CredentialRecord:
        with self._lock:
            self._data = self._read()
            self._check_revision(expected_revision)
            index, row = self._find(credential_id)
            if status == "revoked" and row["status"] == "tombstone":
                return self._record(row)
            row = copy.deepcopy(row)
            row["status"] = status
            row["revoked_at"] = row.get("revoked_at") or _now()
            if status == "tombstone":
                row["deleted_at"] = row.get("deleted_at") or _now()
                row["workspace_retention"] = workspace_retention
            candidate = copy.deepcopy(self._data)
            candidate["credentials"][index] = row
            self._save(candidate)
            return self._record(row)

    def purge_tombstone(self, credential_id: str, *, workspace_deleted: bool = False, retention_complete: bool = False) -> bool:
        """Remove a tombstone only after its workspace-retention decision is complete."""
        credential_id = _uuid_text(credential_id)
        with self._lock:
            self._data = self._read()
            index, row = self._find(credential_id)
            if row.get("status") != "tombstone":
                return False
            retention = row.get("workspace_retention", "normal")
            allowed = retention == "delete_now" and workspace_deleted or retention == "normal" and retention_complete or retention == "keep" and retention_complete
            if not allowed:
                return False
            candidate = copy.deepcopy(self._data)
            candidate["credentials"].pop(index)
            self._save(candidate)
            return True

    def reconcile_tombstones(
        self, workspace_lifecycle: Any, *, limit: int | None = None,
        after_credential_id: str = "",
    ) -> list[dict[str, Any]]:
        """Reconcile credential owner state without exposing secret material.

        ``workspace_lifecycle`` may implement ``reconcile_credential`` or be a
        callable.  A failed item remains durable and is safe to retry after a
        broker or Workspace service restart.
        """
        results: list[dict[str, Any]] = []
        if limit is not None and (limit < 1 or limit > 256):
            raise ValueError("credential reconciliation limit is invalid")
        rows = sorted(
            (row for row in self.snapshot()
             if row.status in {"revoked", "tombstone"} and row.credential_id > after_credential_id),
            key=lambda row: row.credential_id,
        )
        if limit is not None:
            rows = rows[:limit]
        for row in rows:
            retention = row.workspace_retention or "normal"
            payload = {"credential_id": row.credential_id, "owner_status": "tombstoned" if row.status == "tombstone" else "revoked", "retention": retention}
            if row.status == "tombstone" and row.deleted_at is not None:
                payload["deleted_at"] = row.deleted_at
            try:
                method = getattr(workspace_lifecycle, "reconcile_credential", None)
                outcome = method(**payload) if callable(method) else workspace_lifecycle(**payload)
                outcome = outcome if isinstance(outcome, dict) else {"complete": bool(outcome)}
                complete = bool(outcome.get("complete", False))
                purged = False
                if row.status == "tombstone" and complete:
                    purged = self.purge_tombstone(
                        row.credential_id,
                        workspace_deleted=bool(outcome.get("workspace_deleted", False)),
                        retention_complete=True,
                    )
                results.append({"credential_id": row.credential_id, "status": "complete" if complete else "pending", "purged": purged})
            except Exception as exc:  # reconciliation is retryable, never destructive on error
                log.debug("credential Workspace reconciliation deferred credential=%s reason=%s", row.credential_id, type(exc).__name__)
                results.append({"credential_id": row.credential_id, "status": "pending", "reason": "reconciliation_unavailable"})
        return results

    def migrate_legacy_digest(self, digest: str, *, surface_kind: str, surface_id: str, scope: str, surface_slug: str | None = None, label: str | None = None, principal_id: str | None = None) -> CredentialPrincipal:
        if _DIGEST_RE.fullmatch(str(digest).lower()) is None:
            raise ValueError("legacy digest is invalid")
        surface_id = _uuid_text(surface_id)
        principal_id = _uuid_text(principal_id or uuid.uuid4())
        row = {"principal_id": principal_id, "surface_kind": surface_kind, "surface_id": surface_id, "surface_slug": surface_slug, "scope": scope, "label": label or "Legacy — migrate to a named credential for reveal and Workspace", "digest": str(digest).lower(), "retired": False, "created_at": _now()}
        with self._lock:
            self._data = self._read()
            existing = next((item for item in self._data.get("legacy", []) if item.get("surface_kind") == surface_kind and item.get("surface_id") == surface_id and item.get("surface_slug") == surface_slug and item.get("scope") == scope and item.get("digest") == str(digest).lower()), None)
            if existing is not None:
                return CredentialPrincipal(existing["principal_id"], "legacy_static", surface_kind, surface_id, existing.get("label"), None, False, existing.get("surface_slug"), existing.get("scope"))
            if not any(item.get("principal_id") == principal_id for item in self._data.get("legacy", [])):
                candidate = copy.deepcopy(self._data)
                candidate.setdefault("legacy", []).append(row)
                self._save(candidate)
        return CredentialPrincipal(principal_id, "legacy_static", surface_kind, surface_id, row["label"], None, False, surface_slug, scope)

    def migrate_legacy_policy(self, records, *, surface_kind: str, surface_id: str) -> tuple[CredentialPrincipal, ...]:
        """Import 11.x digest rows without inventing revealable v2 material."""
        imported = []
        for record in records:
            if not isinstance(record, dict):
                continue
            digest = record.get("digest")
            scope = record.get("scope") or ("project:" + str(record.get("project")) if record.get("project") else "global")
            if isinstance(digest, str) and _DIGEST_RE.fullmatch(digest.lower()):
                imported.append(self.migrate_legacy_digest(digest, surface_kind=surface_kind, surface_id=surface_id, surface_slug=record.get("surface_slug"), scope=str(scope), label=record.get("label")))
        return tuple(imported)

    def retire_legacy(self, principal_id: str) -> None:
        principal_id = _uuid_text(principal_id)
        with self._lock:
            self._data = self._read()
            changed = False
            candidate = copy.deepcopy(self._data)
            for row in candidate.get("legacy", []):
                if row.get("principal_id") == principal_id:
                    row["retired"] = True
                    row["retired_at"] = _now()
                    changed = True
            if changed:
                self._save(candidate)

    @staticmethod
    def _trusted_secret_aad(name: str) -> bytes:
        return f"cognita:trusted-secret:v1:{name}".encode("utf-8")

    def store_trusted_secret(self, name: str, plaintext: str) -> None:
        """Encrypt one installation-scoped trusted-service secret.

        These records share the master-key lifecycle with credentials but use
        distinct AAD, so ciphertext cannot be swapped between record types.
        """
        if not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", name):
            raise CredentialPolicyError("trusted secret name is invalid")
        if not isinstance(plaintext, str) or not plaintext or len(plaintext.encode("utf-8")) > 4096:
            raise CredentialPolicyError("trusted secret value is invalid")
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        with self._lock:
            self._data = self._read()
            candidate = copy.deepcopy(self._data)
            generation = self.master_keys.active_generation
            nonce = secrets.token_bytes(12)
            encrypted = {
                "name": name,
                "algorithm": "AES-256-GCM",
                "aad_version": 1,
                "generation": generation,
                "nonce": _urlsafe(nonce),
                "ciphertext": _urlsafe(AESGCM(self.master_keys.key(generation)).encrypt(
                    nonce, plaintext.encode("utf-8"), self._trusted_secret_aad(name)
                )),
            }
            rows = [row for row in candidate.get("trusted_secrets", []) if row.get("name") != name]
            rows.append(encrypted)
            candidate["trusted_secrets"] = rows
            self._save(candidate)

    def trusted_secret(self, name: str) -> str | None:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        with self._lock:
            self._data = self._read()
            row = next((item for item in self._data.get("trusted_secrets", []) if item.get("name") == name), None)
            if row is None:
                return None
            try:
                plaintext = AESGCM(self.master_keys.key(row["generation"])).decrypt(
                    _decode_urlsafe(row["nonce"]), _decode_urlsafe(row["ciphertext"]),
                    self._trusted_secret_aad(name),
                )
                return plaintext.decode("utf-8")
            except Exception as exc:
                raise CredentialMasterKeyUnavailable("trusted secret cannot be decrypted") from exc

    def rotate_master_key(self) -> str:
        """Re-encrypt every active/tombstoned secret before switching manifest."""
        with self._lock:
            self._data = self._read()
            old_generation = self.master_keys.active_generation
            original = copy.deepcopy(self._data)
            # Stage the generation without changing the active manifest.  A
            # crash before the complete ciphertext replacement therefore
            # leaves both new writes and existing records on the old key.
            new_generation = self.master_keys.create_generation(activate=False)
            try:
                from cryptography.hazmat.primitives.ciphers.aead import AESGCM
                old_key = self.master_keys.key(old_generation)
                new_key = self.master_keys.key(new_generation)
                candidate = copy.deepcopy(self._data)
                for row in candidate["credentials"]:
                    encrypted = row.get("encrypted_secret")
                    if not encrypted:
                        continue
                    plaintext = AESGCM(old_key).decrypt(_decode_urlsafe(encrypted["nonce"]), _decode_urlsafe(encrypted["ciphertext"]), _credential_aad(row["credential_id"], row["surface_kind"], row["surface_id"]))
                    nonce = secrets.token_bytes(12)
                    row["encrypted_secret"] = {"algorithm": "AES-256-GCM", "aad_version": V2_AAD_VERSION, "generation": new_generation, "nonce": _urlsafe(nonce), "ciphertext": _urlsafe(AESGCM(new_key).encrypt(nonce, plaintext, _credential_aad(row["credential_id"], row["surface_kind"], row["surface_id"]))) }
                for row in candidate.get("trusted_secrets", []):
                    name = row["name"]
                    plaintext = AESGCM(self.master_keys.key(row["generation"])).decrypt(
                        _decode_urlsafe(row["nonce"]), _decode_urlsafe(row["ciphertext"]),
                        self._trusted_secret_aad(name),
                    )
                    nonce = secrets.token_bytes(12)
                    row.update({
                        "generation": new_generation,
                        "nonce": _urlsafe(nonce),
                        "ciphertext": _urlsafe(AESGCM(new_key).encrypt(
                            nonce, plaintext, self._trusted_secret_aad(name)
                        )),
                    })
                self._write({**candidate, "revision": int(self._data.get("revision", 0)) + 1})
                self._data = {**candidate, "revision": int(self._data.get("revision", 0)) + 1}
                # Manifest is switched only after the complete replacement verifies.
                self.master_keys.activate(new_generation)
                return new_generation
            except Exception as exc:
                try:
                    self.master_keys.activate(old_generation)
                except Exception:
                    pass
                # If policy replacement succeeded but manifest activation failed,
                # restore the old ciphertext set before reporting failure.  The
                # old key remains retained for an operator-selected rollback.
                try:
                    if self._data != original:
                        self._write(original)
                        self._data = original
                except Exception:
                    log.error("credential master-key rollback failed")
                raise CredentialMasterKeyUnavailable("master-key rotation failed; old generation remains active") from exc


class CredentialAdminService:
    """Surface-aware Admin adapter over :class:`CredentialPolicyStore`.

    Credential IDs are public routing hints.  Every mutation therefore joins
    the ID back to the exact URL surface before touching secret state, so a
    credential copied into another surface's Admin URL cannot be revealed,
    rotated, revoked, or deleted.
    """

    def __init__(
        self,
        store: CredentialPolicyStore,
        *,
        surface_resolver: Callable[[str, str], Any | None],
        public_base_url: str,
        workspace_lifecycle: Any = None,
    ):
        self.store = store
        self.surface_resolver = surface_resolver
        self.public_base_url = public_base_url.rstrip("/")
        self.workspace_lifecycle = workspace_lifecycle

    def _reconcile_workspace(self, row: CredentialRecord) -> dict[str, Any] | None:
        if self.workspace_lifecycle is None:
            return None
        try:
            method = getattr(self.workspace_lifecycle, "reconcile_credential", None)
            if not callable(method):
                return None
            payload = {
                "credential_id": row.credential_id,
                "owner_status": "tombstoned" if row.status == "tombstone" else "revoked",
                "retention": row.workspace_retention or "normal",
            }
            if row.status == "tombstone" and row.deleted_at is not None:
                payload["deleted_at"] = row.deleted_at
            outcome = method(**payload)
            return outcome if isinstance(outcome, dict) else {"complete": bool(outcome)}
        except Exception:
            log.warning("credential Workspace reconciliation deferred credential=%s", row.credential_id)
            return {"complete": False, "status": "pending"}

    def _surface(self, surface_kind: str, surface_id: str) -> tuple[str, str]:
        if surface_kind not in {"combined", "workspace"}:
            raise CredentialSurfaceConflict("unsupported credential surface")
        surface_id = _uuid_text(surface_id)
        surface = self.surface_resolver(surface_kind, surface_id)
        if surface is None:
            raise CredentialSurfaceConflict("credential surface was not found")
        if isinstance(surface, dict):
            slug = surface.get("slug")
        else:
            slug = getattr(surface, "slug", None)
        if not isinstance(slug, str) or not slug:
            raise CredentialSurfaceConflict("credential surface has no canonical slug")
        return surface_id, slug

    def _owned(
        self, surface_kind: str, surface_id: str, credential_id: str,
    ) -> CredentialRecord:
        surface_id, _slug = self._surface(surface_kind, surface_id)
        credential_id = _uuid_text(credential_id)
        row = next(
            (
                item for item in self.store.list_surface(
                    surface_kind, surface_id, include_tombstones=True
                )
                if item.credential_id == credential_id
            ),
            None,
        )
        if row is None:
            raise CredentialNotFound("credential not found")
        return row

    def list_credentials(self, *, surface_kind: str, surface_id: str) -> dict:
        surface_id, _slug = self._surface(surface_kind, surface_id)
        return {
            "revision": self.store.revision,
            "credentials": self.store.list_surface(surface_kind, surface_id),
        }

    list = list_credentials

    def create_credential(
        self, *, surface_kind: str, surface_id: str, expected_revision: int,
        label: str, current_password: str | None = None,
        password: str | None = None,
    ) -> dict:
        surface_id, slug = self._surface(surface_kind, surface_id)
        row, secret = self.store.add_credential(
            surface_kind, surface_id, label.strip(), surface_slug=slug,
            password=current_password or password,
            expected_revision=expected_revision,
        )
        return {"revision": self.store.revision, "credential": row, "secret": secret}

    create = create_credential

    def rotate_credential(
        self, *, surface_kind: str, surface_id: str, credential_id: str,
        expected_revision: int, current_password: str | None = None,
        password: str | None = None,
    ) -> dict:
        self._owned(surface_kind, surface_id, credential_id)
        row, secret = self.store.rotate_secret(
            credential_id, password=current_password or password,
            expected_revision=expected_revision,
        )
        return {"revision": self.store.revision, "credential": row, "secret": secret}

    rotate = rotate_credential

    def reveal_credential(
        self, *, surface_kind: str, surface_id: str, credential_id: str,
        expected_revision: int, current_password: str | None = None,
        password: str | None = None,
    ) -> dict:
        self._owned(surface_kind, surface_id, credential_id)
        proof = self.store.begin_reveal(
            credential_id, current_password or password or "",
            expected_revision=expected_revision,
        )
        return {"revision": self.store.revision, "secret": self.store.reveal(credential_id, proof)}

    def revoke_credential(
        self, *, surface_kind: str, surface_id: str, credential_id: str,
        expected_revision: int, **_unused: Any,
    ) -> dict:
        self._owned(surface_kind, surface_id, credential_id)
        row = self.store.revoke(credential_id, expected_revision=expected_revision)
        return {"revision": self.store.revision, "credential": row, "workspace_reconciliation": self._reconcile_workspace(row)}

    revoke = revoke_credential

    def delete_credential(
        self, *, surface_kind: str, surface_id: str, credential_id: str,
        expected_revision: int, retention: str = "normal", confirm: bool = False,
        workspace_id: str | None = None, workspace_revision: int | None = None,
        **_unused: Any,
    ) -> dict:
        if not confirm:
            raise CredentialPolicyError("explicit confirmation is required")
        self._owned(surface_kind, surface_id, credential_id)
        validator = getattr(self.workspace_lifecycle, "validate_credential_workspace", None)
        gate = getattr(self.workspace_lifecycle, "credential_deletion_gate", None)
        if not callable(validator) or not callable(gate):
            raise CredentialWorkspaceConflict("Workspace binding is unavailable")
        with gate(credential_id):
            try:
                validator(
                    credential_id=credential_id,
                    workspace_id=workspace_id,
                    workspace_revision=workspace_revision,
                )
            except CredentialWorkspaceConflict:
                raise
            except Exception as exc:
                raise CredentialWorkspaceConflict("Workspace target changed") from exc
            row = self.store.delete(
                credential_id,
                workspace_retention=retention,  # type: ignore[arg-type]
                expected_revision=expected_revision,
            )
            workspace_reconciliation = self._reconcile_workspace(row)
        return {"revision": self.store.revision, "credential": row, "workspace_reconciliation": workspace_reconciliation}

    delete = delete_credential
    remove = delete_credential

    def setup_material(
        self, *, surface_kind: str, surface_id: str, credential_id: str,
        route_strategy: str = "stable", provider: str = "provider-neutral",
        current_password: str,
    ) -> dict:
        row = self._owned(surface_kind, surface_id, credential_id)
        proof = self.store.begin_reveal(row.credential_id, current_password)
        secret = self.store.reveal(row.credential_id, proof)
        _surface_id, slug = self._surface(surface_kind, surface_id)
        from .connectors import (
            PUBLIC_CONTRACT_VERSION,
            WORKSPACE_CONTRACT_VERSION,
            build_route_url,
        )
        if route_strategy == "stable":
            # Each surface's stable alias serves its current catalog.
            version = None
        elif route_strategy == "current":
            version = (
                PUBLIC_CONTRACT_VERSION
                if surface_kind == "combined"
                else WORKSPACE_CONTRACT_VERSION
            )
        else:
            raise CredentialPolicyError(
                "credentials must use the stable or current contract route"
            )
        url = build_route_url(self.public_base_url, surface_kind, slug, version)
        return {
            "url": url,
            "provider": provider,
            "secret": secret,
            "authorization_header": f"Bearer {secret}",
            "sillytavern": {
                "type": "streamable-http",
                "url": url,
                "headers": {"Authorization": f"Bearer {secret}"},
            },
        }

    build_setup_material = setup_material


__all__ = [
    "AdminPasswordGate",
    "CredentialCapacityExceeded",
    "CredentialAdminService",
    "CredentialMasterKeyUnavailable",
    "CredentialNotFound",
    "CredentialPolicyError",
    "CredentialPolicyStore",
    "CredentialPrincipal",
    "CredentialRecord",
    "CredentialRevealDenied",
    "CredentialRevoked",
    "CredentialSurfaceConflict",
    "CredentialWorkspaceConflict",
    "MasterKeyRing",
    "STATIC_KEY_PREFIX",
    "V2_STATIC_KEY_PREFIX",
    "AuthPrincipal",
    "AuthenticationLockoutConfirmationRequired",
    "AuthenticationPolicy",
    "AuthenticationPolicyError",
    "AuthenticationPolicyStore",
    "AuthenticationPolicyUnavailable",
    "AuthenticationRedactionFilter",
    "AuthenticationRevisionConflict",
    "GlobalAuthenticationPolicy",
    "ProjectAuthenticationPolicy",
    "StaticKeyRecord",
    "generate_static_key",
    "is_v2_static_key_candidate",
    "is_static_key_candidate",
    "migrate_authentication_policy",
    "redact_authentication_text",
    "parse_v2_static_key",
]
