"""Connector definitions and effective project policy.

This module deliberately has no FastAPI, Django, engine, or OAuth imports.  It
is the small shared policy boundary used by the parent, Admin API, and OAuth
child.  The configuration file is treated as a security policy: a malformed
or unreadable existing file is never replaced with an empty policy.
"""

from __future__ import annotations

import logging
import os
import re
import tempfile
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Literal, Mapping
from urllib.parse import urlsplit

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from .release_identity import COMBINED_CONTRACT_VERSION
from .release_identity import WORKSPACE_CONTRACT_VERSION as _WORKSPACE_CONTRACT_VERSION

log = logging.getLogger("cognita.connectors")

CONNECTOR_CONFIG_VERSION = 1
# Public MCP schemas are code, so their URL generation is code-owned too. A
# release that intentionally changes the client-visible contract increments
# this constant; administrators do not publish schema generations by hand.
# 13.0 §4: the number itself now lives in `release_identity`, the single
# authority, and these two names are re-exports so every existing caller and
# import site is unchanged.
PUBLIC_CONTRACT_VERSION = COMBINED_CONTRACT_VERSION
PREVIOUS_CONTRACT_VERSION = PUBLIC_CONTRACT_VERSION - 1
# Workspace-only v3 publishes the current Workspace response schemas,
# including job streaming offsets, replay receipts, destination hashes, and
# measured usage fields.  Earlier generations are intentionally retired at
# this cutover; Workspace has no compatibility window.
WORKSPACE_CONTRACT_VERSION = _WORKSPACE_CONTRACT_VERSION
WORKSPACE_PREVIOUS_CONTRACT_VERSION: int | None = None
MAX_CONFIG_BYTES = 1_048_576
MAX_CONNECTOR_NAME = 120
MAX_CONNECTOR_SLUG = 63
Access = Literal["read", "write"]
ProjectMode = Literal["all", "selected"]
_NAME_RE = re.compile(r"\A[^\x00-\x1f\x7f]{1,120}\Z")
_SLUG_RE = re.compile(r"\A[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")
_CONNECTOR_PATH_RE = re.compile(
    r"\A/mcp/connectors/"
    r"([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)"
    r"/mcp/v([1-9][0-9]*)\Z"
)
_COMBINED_ROUTE_RE = re.compile(
    r"\A/mcp/connectors/"
    r"([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)/mcp"
    r"(?:/v([1-9][0-9]*))?\Z"
)
_WORKSPACE_ROUTE_RE = re.compile(
    r"\A/mcp/workspace/"
    r"([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)/mcp"
    r"(?:/v([1-9][0-9]*))?\Z"
)


def connector_slug_from_name(name: str) -> str:
    """Create the stable URL slug assigned when a connector is first loaded."""
    slug = re.sub(r"[^a-z0-9]+", "-", name.casefold()).strip("-")
    slug = slug[:MAX_CONNECTOR_SLUG].rstrip("-")
    return slug or "connector"


class ConnectorPolicyError(ValueError):
    """The current configuration is invalid or a mutation is not valid."""


class PolicyUnavailable(ConnectorPolicyError):
    """The current policy cannot be safely read; callers must fail closed."""


class RevisionConflict(ConnectorPolicyError):
    """A mutation was based on a stale configuration revision."""


class ConnectorNotFound(ConnectorPolicyError):
    """A requested connector ID does not exist in the current snapshot."""


class ConnectorDefinition(BaseModel):
    """One immutable-ID connector access configuration."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    id: str
    name: str
    # Public URL identity. The UUID remains the immutable internal policy and
    # audit identity, but there is no reason to expose it in a connector URL.
    # Existing pre-9.3.3 records derive this once from their display name and
    # persist it on the next policy write.
    slug: str = ""
    enabled: bool = True
    project_mode: ProjectMode = "all"
    default_access: Access | None = "write"
    project_access: dict[str, Access] = Field(default_factory=dict)
    # 13.0.1 (Doug, 2026-09-22): Workspace tools and Knowledge<->Workspace
    # transfer are ON for a new connector. They used to default off ("high
    # trust"), which meant every new installation shipped without the feature
    # until someone found the checkbox; the first 13.0 live check on main
    # failed for exactly that reason. A connector record that says false keeps
    # false; only the default for an unset field changed.
    workspace_enabled: bool = True
    default_workspace_transfer: Literal["allow", "deny"] = "allow"
    project_transfer: dict[str, Literal["inherit", "allow", "deny"]] = Field(default_factory=dict)
    @field_validator("id")
    @classmethod
    def valid_id(cls, value: str) -> str:
        try:
            return str(uuid.UUID(value))
        except (ValueError, AttributeError, TypeError) as exc:
            raise ValueError("id must be a UUID") from exc

    @field_validator("name")
    @classmethod
    def valid_name(cls, value: str) -> str:
        if not isinstance(value, str) or value != value.strip() or not _NAME_RE.fullmatch(value):
            raise ValueError("name must be a nonempty display string without control characters")
        return value

    @model_validator(mode="before")
    @classmethod
    def default_slug(cls, value: Any) -> Any:
        if isinstance(value, Mapping) and not value.get("slug"):
            value = dict(value)
            name = value.get("name")
            if isinstance(name, str):
                value["slug"] = connector_slug_from_name(name)
        return value

    @field_validator("slug")
    @classmethod
    def valid_slug(cls, value: str) -> str:
        if not isinstance(value, str) or not _SLUG_RE.fullmatch(value):
            raise ValueError("slug must be a lowercase URL label of 1 to 63 characters")
        return value

    @field_validator("project_access")
    @classmethod
    def valid_access_keys(cls, value: dict[str, Access]) -> dict[str, Access]:
        for project in value:
            if not isinstance(project, str) or not project or project != project.strip():
                raise ValueError("project_access keys must be exact nonempty project names")
        return value

    @field_validator("project_transfer")
    @classmethod
    def valid_transfer_keys(cls, value: dict[str, Literal["inherit", "allow", "deny"]]):
        for project in value:
            if not isinstance(project, str) or not project or project != project.strip():
                raise ValueError("project_transfer keys must be exact nonempty project names")
        return value

    @model_validator(mode="after")
    def valid_mode(self) -> "ConnectorDefinition":
        if self.project_mode == "selected" and self.default_access is not None:
            raise ValueError("selected connectors must not define default_access")
        if self.project_mode == "all" and self.default_access is None:
            raise ValueError("all connectors require default_access")
        return self

    def validate_projects(self, project_names: Iterable[str]) -> None:
        known = set(project_names)
        unknown = sorted(set(self.project_access) - known)
        if unknown:
            raise ConnectorPolicyError(
                f"connector {self.id} references unknown project(s): {', '.join(unknown)}"
            )
        unknown_transfer = sorted(set(self.project_transfer) - known)
        if unknown_transfer:
            raise ConnectorPolicyError(
                f"connector {self.id} references unknown transfer project(s): {', '.join(unknown_transfer)}"
            )

    def _configured_access(self, project_name: str) -> Access | None:
        """Return configured access before project-default filtering."""
        if self.project_mode == "selected":
            return self.project_access.get(project_name)
        return self.project_access.get(project_name, self.default_access)

    def transfer_allowed(self, project_name: str) -> bool:
        """Resolve transfer permission independently of project access."""
        if not self.workspace_enabled:
            return False
        setting = self.project_transfer.get(project_name, "inherit")
        return (
            self.default_workspace_transfer == "allow"
            if setting == "inherit"
            else setting == "allow"
        )


class ConnectorConfig(BaseModel):
    """Versioned on-disk connector document."""

    model_config = ConfigDict(extra="forbid")

    version: int = CONNECTOR_CONFIG_VERSION
    revision: int = Field(default=0, ge=0)
    connectors: list[ConnectorDefinition] = Field(default_factory=list)

    @field_validator("version")
    @classmethod
    def supported_version(cls, value: int) -> int:
        if value != CONNECTOR_CONFIG_VERSION:
            raise ValueError("unsupported connector configuration version")
        return value

    @model_validator(mode="after")
    def unique_connectors(self) -> "ConnectorConfig":
        ids = [c.id for c in self.connectors]
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate connector IDs are not allowed")
        names = [c.name.casefold() for c in self.connectors]
        if len(set(names)) != len(names):
            raise ValueError("connector names must be unique")
        slugs = [c.slug for c in self.connectors]
        if len(set(slugs)) != len(slugs):
            raise ValueError("connector slugs must be unique")
        return self

    def validate_projects(self, project_names: Iterable[str]) -> None:
        for connector in self.connectors:
            connector.validate_projects(project_names)


class WorkspaceConnectorDefinition(BaseModel):
    """Independent Workspace-only surface policy."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    slug: str
    display_name: str
    enabled: bool = False
    revision: int = Field(default=1, ge=1)

    @field_validator("id")
    @classmethod
    def valid_id(cls, value: str) -> str:
        try:
            return str(uuid.UUID(value))
        except (ValueError, AttributeError, TypeError) as exc:
            raise ValueError("id must be a UUID") from exc

    @field_validator("slug")
    @classmethod
    def valid_slug(cls, value: str) -> str:
        if not isinstance(value, str) or _SLUG_RE.fullmatch(value) is None:
            raise ValueError("slug must be a lowercase URL label of 1 to 63 characters")
        return value

    @field_validator("display_name")
    @classmethod
    def valid_name(cls, value: str) -> str:
        if not isinstance(value, str) or value != value.strip() or _NAME_RE.fullmatch(value) is None:
            raise ValueError("display_name must be a nonempty display string without control characters")
        return value


class WorkspaceConnectorConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: Literal[1] = 1
    revision: int = Field(default=0, ge=0)
    workspace_connectors: list[WorkspaceConnectorDefinition] = Field(default_factory=list)

    @model_validator(mode="after")
    def unique_records(self) -> "WorkspaceConnectorConfig":
        ids = [item.id for item in self.workspace_connectors]
        slugs = [item.slug for item in self.workspace_connectors]
        names = [item.display_name.casefold() for item in self.workspace_connectors]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate Workspace connector IDs are not allowed")
        if len(slugs) != len(set(slugs)):
            raise ValueError("Workspace connector slugs must be unique")
        if len(names) != len(set(names)):
            raise ValueError("Workspace connector names must be unique")
        return self


@dataclass(frozen=True)
class EffectiveAccess:
    connector_id: str
    project: str
    access: Access
    revision: int


@dataclass(frozen=True)
class MigrationResult:
    changed: bool
    dry_run: bool
    revision: int
    connector_id: str | None
    plan: tuple[str, ...]


@dataclass(frozen=True)
class ConnectorResource:
    """The identity encoded by a canonical versioned connector resource."""

    connector_slug: str
    contract_version: int


SurfaceFamily = Literal["combined", "workspace"]


@dataclass(frozen=True)
class RouteResource:
    """Canonical public identity for a combined or Workspace-only route."""

    family: SurfaceFamily
    slug: str
    contract_version: int | None = None

    @property
    def is_stable(self) -> bool:
        return self.contract_version is None

    @property
    def effective_version(self) -> int:
        return self.contract_version or (
            PUBLIC_CONTRACT_VERSION if self.family == "combined" else WORKSPACE_CONTRACT_VERSION
        )

    @property
    def path(self) -> str:
        prefix = "/mcp/connectors" if self.family == "combined" else "/mcp/workspace"
        suffix = "" if self.contract_version is None else f"/v{self.contract_version}"
        return f"{prefix}/{self.slug}/mcp{suffix}"


def _project_name(project: Any) -> str:
    return project if isinstance(project, str) else str(getattr(project, "name"))


def _project_enabled(project: Any) -> bool:
    return bool(getattr(project, "enabled", True))


def _project_writable(project: Any) -> bool:
    return bool(getattr(project, "writable", True))


def resolve_project_access(
    connector: ConnectorDefinition, project: Any, *,
    project_key_grant: str | None = None,
) -> Access | None:
    """Resolve one connector/project join using the current project object.

    The project object is required so default-scope exclusions cannot be
    bypassed by callers that only know a project name. Explicit map entries
    always win, including when they equal the connector's default.
    """
    if isinstance(project, str) or project is None:
        raise ConnectorPolicyError("project-aware access resolution requires a project object")
    project_name = getattr(project, "name", None)
    if not isinstance(project_name, str) or not project_name:
        raise ConnectorPolicyError("project object must have a nonempty name")
    if project_name in connector.project_access:
        return connector.project_access[project_name]
    if connector.project_mode == "selected":
        return None
    if project_key_grant == project_name:
        return connector.default_access
    if bool(getattr(project, "exclude_from_default_permissions", False)):
        return None
    return connector.default_access


def _contract_version(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ConnectorPolicyError("contract version must be a positive integer")
    return value


def build_connector_path(
    connector_slug: str, contract_version: int = PUBLIC_CONTRACT_VERSION,
) -> str:
    """Construct the canonical versioned endpoint path for a connector."""
    if not isinstance(connector_slug, str) or not _SLUG_RE.fullmatch(connector_slug):
        raise ConnectorPolicyError("connector slug must be a canonical lowercase URL label")
    version = _contract_version(contract_version)
    return f"/mcp/connectors/{connector_slug}/mcp/v{version}"


def build_connector_url(
    public_origin: str,
    connector_slug: str,
    contract_version: int = PUBLIC_CONTRACT_VERSION,
) -> str:
    """Construct the canonical versioned endpoint from the configured origin."""
    path = build_connector_path(connector_slug, contract_version)
    origin = (public_origin or "").rstrip("/")
    if origin:
        parsed = urlsplit(origin)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.netloc
            or parsed.query
            or parsed.fragment
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise ConnectorPolicyError("public origin must be an absolute HTTP(S) origin")
    return f"{origin}{path}" if origin else path


def build_route_url(
    public_origin: str, family: SurfaceFamily, slug: str,
    contract_version: int | None = None,
) -> str:
    """Construct a canonical stable/versioned route URL."""
    path = build_route_path(family, slug, contract_version)
    origin = (public_origin or "").rstrip("/")
    if origin:
        parsed = urlsplit(origin)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.netloc
            or parsed.query
            or parsed.fragment
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise ConnectorPolicyError("public origin must be an absolute HTTP(S) origin")
    return f"{origin}{path}" if origin else path


def parse_connector_path(path: str) -> ConnectorResource | None:
    """Parse only a canonical versioned connector path.

    ``None`` is returned for all malformed, legacy, noncanonical, padded, or
    trailing-slash paths so callers can fail closed without exposing policy
    details.  Query strings are rejected by this path-only API as well.
    """
    if not isinstance(path, str):
        return None
    match = _CONNECTOR_PATH_RE.fullmatch(path)
    if match is None:
        return None
    connector_slug, version_text = match.groups()
    try:
        version = int(version_text)
    except ValueError:
        return None
    return ConnectorResource(connector_slug, version)


def parse_route_path(path: str) -> RouteResource | None:
    """Parse a strict stable or versioned public MCP path.

    Query strings are deliberately rejected by this path-only parser (and by
    the gateway before authentication). Stable and versioned paths retain
    distinct resource identities even when they serve the same catalog.
    """
    if not isinstance(path, str) or "?" in path or "#" in path:
        return None
    for family, pattern in (("combined", _COMBINED_ROUTE_RE), ("workspace", _WORKSPACE_ROUTE_RE)):
        match = pattern.fullmatch(path)
        if match is None:
            continue
        slug, version_text = match.groups()
        version = int(version_text) if version_text is not None else None
        return RouteResource(family, slug, version)  # type: ignore[arg-type]
    return None


def build_route_path(
    family: SurfaceFamily, slug: str, contract_version: int | None = None,
) -> str:
    """Build a canonical stable or versioned path for one surface family."""
    if family not in {"combined", "workspace"} or not _SLUG_RE.fullmatch(slug):
        raise ConnectorPolicyError("invalid MCP route family or slug")
    if contract_version is not None:
        _contract_version(contract_version)
    return RouteResource(family, slug, contract_version).path


def supported_route_versions(family: SurfaceFamily) -> tuple[int, ...]:
    """Return exactly the callable generation for a product family."""
    if family == "combined":
        return (PUBLIC_CONTRACT_VERSION,)
    if family == "workspace":
        return (WORKSPACE_CONTRACT_VERSION,)
    return ()


def is_supported_route(resource: RouteResource) -> bool:
    """Whether a parsed route is stable or its family's current generation."""
    if resource.family in {"combined", "workspace"} and resource.contract_version is None:
        return True
    return (
        resource.contract_version is not None
        and resource.contract_version in supported_route_versions(resource.family)
    )


def parse_connector_resource(resource: str, public_origin: str | None = None) -> ConnectorResource | None:
    """Parse a canonical connector resource URL or path.

    When ``public_origin`` is supplied, the resource must be an absolute URL
    using that exact scheme/authority.  Without an origin, either an absolute
    HTTP(S) URL or a relative path is accepted.  Gateway request targets should
    use :func:`parse_connector_path` directly.
    """
    if not isinstance(resource, str):
        return None
    parsed = urlsplit(resource)
    if parsed.query or parsed.fragment:
        return None
    if parsed.scheme or parsed.netloc:
        if not parsed.scheme or not parsed.netloc:
            return None
        if public_origin:
            origin = urlsplit(public_origin.rstrip("/"))
            if parsed.scheme != origin.scheme or parsed.netloc != origin.netloc:
                return None
            origin_path = origin.path.rstrip("/")
            if origin_path:
                if not parsed.path.startswith(origin_path + "/"):
                    return None
                return parse_connector_path(parsed.path[len(origin_path):])
        elif parsed.scheme not in {"http", "https"}:
            return None
        return parse_connector_path(parsed.path)
    if public_origin:
        return None
    return parse_connector_path(resource)


def parse_route_resource(resource: str, public_origin: str | None = None) -> RouteResource | None:
    """Parse an absolute or relative stable/versioned product resource URL."""
    if not isinstance(resource, str):
        return None
    parsed = urlsplit(resource)
    if parsed.query or parsed.fragment:
        return None
    if parsed.scheme or parsed.netloc:
        if not parsed.scheme or not parsed.netloc:
            return None
        if public_origin:
            origin = urlsplit(public_origin.rstrip("/"))
            if parsed.scheme != origin.scheme or parsed.netloc != origin.netloc:
                return None
            origin_path = origin.path.rstrip("/")
            if origin_path:
                if not parsed.path.startswith(origin_path + "/"):
                    return None
                return parse_route_path(parsed.path[len(origin_path):])
        elif parsed.scheme not in {"http", "https"}:
            return None
        return parse_route_path(parsed.path)
    if public_origin:
        return None
    return parse_route_path(resource)


def supported_contract_versions(current_version: int | None = None) -> tuple[int, ...]:
    """Return only the code-owned current public generation.

    Contract publication is a hard cutover.  Retired and future generations
    are not callable, advertised, OAuth-valid, or accepted as audiences.
    """
    current = PUBLIC_CONTRACT_VERSION if current_version is None else current_version
    if not isinstance(current, int) or isinstance(current, bool) or current < 1:
        return ()
    return (current,)


def is_supported_contract_version(
    requested_version: int, current_version: int | None = None,
) -> bool:
    """Return whether *requested_version* is in the bounded public window."""
    return (
        not isinstance(requested_version, bool)
        and isinstance(requested_version, int)
        and requested_version in supported_contract_versions(current_version)
    )


def is_published_contract_version(requested_version: int) -> bool:
    """Return whether a generation is the sole current publication."""
    return is_supported_contract_version(requested_version)


# Explicit alias for callers that name the input as a resource path.
parse_connector_resource_path = parse_connector_path


class ConnectorStore:
    """Atomic, revisioned connector configuration store."""

    _locks: dict[str, threading.RLock] = {}
    _locks_guard = threading.Lock()

    def __init__(self, path: Path, *, persist_migrations: bool = True):
        self.path = Path(path)
        # Only the parent service owns connector-policy writes. The OAuth
        # child may derive legacy fields for current reads, but allowing it to
        # replace the policy file would create a cross-process lost-update
        # race with an Admin mutation during first-start migration.
        self._persist_migrations = persist_migrations
        key = str(self.path.resolve())
        with self._locks_guard:
            self._lock = self._locks.setdefault(key, threading.RLock())

    def _read(self) -> ConnectorConfig:
        if not self.path.exists():
            return ConnectorConfig()
        try:
            size = self.path.stat().st_size
            if size > MAX_CONFIG_BYTES:
                raise PolicyUnavailable("connector configuration exceeds the bounded read limit")
            with self.path.open("r", encoding="utf-8") as stream:
                raw = yaml.safe_load(stream)
            if raw is None:
                raw = {}
            if not isinstance(raw, dict):
                raise ValueError("top-level YAML value must be a mapping")
            if raw.get("version", CONNECTOR_CONFIG_VERSION) != CONNECTOR_CONFIG_VERSION:
                raise ValueError("unsupported connector configuration version")
            raw, migrated = self._migrate_legacy_config(raw)
            config = ConnectorConfig(**raw)
            if migrated and self._persist_migrations:
                # Persist the complete, validated schema repair without
                # changing the policy revision. Slugs are public identities
                # that must survive restarts and display-name changes; obsolete
                # contract generations must not remain as apparent policy.
                self._write(config)
            return config
        except PolicyUnavailable:
            raise
        except (OSError, UnicodeError, yaml.YAMLError, ValidationError, ValueError, TypeError) as exc:
            log.error("connector policy load failed path=%s error=%s", self.path, type(exc).__name__)
            raise PolicyUnavailable("connector policy is unavailable") from exc

    @staticmethod
    def _migrate_legacy_config(raw: dict) -> tuple[dict, bool]:
        """Normalize legacy connector records to the code-owned schema.

        Obsolete per-connector contract generations are discarded rather than
        admitted into the runtime model: their values cannot override or even
        appear to compete with ``PUBLIC_CONTRACT_VERSION``. Explicit slugs are
        reserved before deriving missing legacy values so a newly derived slug
        can never steal a public identity already persisted by another
        connector. Connector order is the stable collision tie-breaker.
        """
        entries = raw.get("connectors")
        if not isinstance(entries, list):
            return raw, False
        rows = [dict(entry) if isinstance(entry, Mapping) else entry for entry in entries]
        reserved = {
            row.get("slug")
            for row in rows
            if isinstance(row, dict) and row.get("slug")
        }
        used = set(reserved)
        migrated = False
        for row in rows:
            if not isinstance(row, dict):
                continue
            if "contract_version" in row:
                row.pop("contract_version")
                migrated = True
            if row.get("slug"):
                continue
            name = row.get("name")
            if not isinstance(name, str):
                continue
            base = connector_slug_from_name(name)
            slug = base
            suffix = 2
            while slug in used:
                tail = f"-{suffix}"
                slug = f"{base[:MAX_CONNECTOR_SLUG - len(tail)].rstrip('-')}{tail}"
                suffix += 1
            row["slug"] = slug
            used.add(slug)
            migrated = True
        if not migrated:
            return raw, False
        normalized = dict(raw)
        normalized["connectors"] = rows
        return normalized, True

    def snapshot(self, projects: Iterable[Any] | None = None) -> ConnectorConfig:
        with self._lock:
            config = self._read()
            if projects is not None:
                # Materialize once: callers often provide a generator, and
                # validation must not consume the iterable used by a later
                # access resolution pass.
                names = [_project_name(p) for p in projects]
                config.validate_projects(names)
            return config.model_copy(deep=True)

    def _write(self, config: ConnectorConfig) -> None:
        payload = config.model_dump(mode="json")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix=f".{self.path.name}.", suffix=".tmp", dir=self.path.parent)
        temp_path = Path(temp_name)
        try:
            os.chmod(temp_path, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
                yaml.safe_dump(payload, stream, sort_keys=False, allow_unicode=True)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp_path, self.path)
            try:
                os.chmod(self.path, 0o600)
            except OSError:
                pass
        except Exception:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                log.error("connector policy temporary cleanup failed path=%s", temp_path)
            raise

    def save(self, config: ConnectorConfig, *, expected_revision: int) -> ConnectorConfig:
        with self._lock:
            current = self._read()
            if current.revision != expected_revision:
                raise RevisionConflict(f"expected revision {expected_revision}, current is {current.revision}")
            if config.revision != expected_revision + 1:
                raise ConnectorPolicyError("persisted connector revision must increment by one")
            self._write(config)
            return config.model_copy(deep=True)

    def mutate(self, expected_revision: int, mutation: Callable[[ConnectorConfig], None]) -> ConnectorConfig:
        with self._lock:
            current = self._read()
            if current.revision != expected_revision:
                raise RevisionConflict(f"expected revision {expected_revision}, current is {current.revision}")
            candidate = current.model_copy(deep=True)
            mutation(candidate)
            candidate.revision = current.revision + 1
            self._write(candidate)
            return candidate.model_copy(deep=True)

    def effective_access(
        self, connector_id: str, project_name: str, projects: Iterable[Any], *,
        project_key_grant: str | None = None,
    ) -> EffectiveAccess | None:
        try:
            connector_id = str(uuid.UUID(connector_id))
        except (ValueError, AttributeError, TypeError):
            return None
        project_list = list(projects)
        config = self.snapshot(project_list)
        connector = next((c for c in config.connectors if c.id == connector_id), None)
        if connector is None or not connector.enabled:
            return None
        project = next((p for p in project_list if _project_name(p) == project_name), None)
        if project is None or not _project_enabled(project):
            return None
        access = resolve_project_access(
            connector, project, project_key_grant=project_key_grant
        )
        if access is None:
            return None
        return EffectiveAccess(connector.id, project_name, access, config.revision)

    def accessible_projects(self, connector_id: str, projects: Iterable[Any]) -> list[EffectiveAccess]:
        project_list = list(projects)
        config = self.snapshot(project_list)
        connector = next((c for c in config.connectors if c.id == connector_id), None)
        if connector is None or not connector.enabled:
            return []
        result = []
        for project in sorted(project_list, key=_project_name):
            if not _project_enabled(project):
                continue
            access = resolve_project_access(connector, project)
            if access is not None:
                result.append(EffectiveAccess(connector.id, _project_name(project), access, config.revision))
        return result

    def create(self, *, expected_revision: int, name: str, enabled: bool = True, project_mode: ProjectMode = "all", default_access: Access | None = "write", project_access: Mapping[str, Access] | None = None, project_names: Iterable[str] = (), workspace_enabled: bool = True, default_workspace_transfer: Literal["allow", "deny"] = "allow", project_transfer: Mapping[str, Literal["inherit", "allow", "deny"]] | None = None) -> ConnectorConfig:
        def add(config: ConnectorConfig) -> None:
            base_slug = connector_slug_from_name(name)
            slug = base_slug
            suffix = 2
            used = {item.slug for item in config.connectors}
            while slug in used:
                tail = f"-{suffix}"
                slug = f"{base_slug[:MAX_CONNECTOR_SLUG - len(tail)].rstrip('-')}{tail}"
                suffix += 1
            connector = ConnectorDefinition(
                id=str(uuid.uuid4()), name=name, slug=slug, enabled=enabled,
                project_mode=project_mode, default_access=default_access,
                project_access=dict(project_access or {}),
                workspace_enabled=workspace_enabled,
                default_workspace_transfer=default_workspace_transfer,
                project_transfer=dict(project_transfer or {}),
            )
            connector.validate_projects(project_names)
            config.connectors.append(connector)
            ConnectorConfig(**config.model_dump()).unique_connectors()
        return self.mutate(expected_revision, add)

    def update(self, connector_id: str, *, expected_revision: int, project_names: Iterable[str] = (), **changes: Any) -> ConnectorConfig:
        try:
            connector_id = str(uuid.UUID(connector_id))
        except (ValueError, AttributeError, TypeError) as exc:
            raise ConnectorPolicyError("connector ID must be a UUID") from exc
        if "contract_version" in changes:
            raise ConnectorPolicyError("contract_version is owned by the deployed MCP schema")
        if "slug" in changes:
            raise ConnectorPolicyError("slug is managed by Cognita")
        def apply(config: ConnectorConfig) -> None:
            current = next((c for c in config.connectors if c.id == connector_id), None)
            if current is None:
                raise ConnectorNotFound("connector not found")
            data = current.model_dump()
            data.update({key: value for key, value in changes.items() if value is not None or key == "default_access"})
            if changes.get("project_mode") == "selected" and "default_access" not in changes:
                data["default_access"] = None
            data["id"] = connector_id
            replacement = ConnectorDefinition(**data)
            replacement.validate_projects(project_names)
            config.connectors[config.connectors.index(current)] = replacement
            ConnectorConfig(**config.model_dump()).unique_connectors()
        return self.mutate(expected_revision, apply)

    def delete(self, connector_id: str, *, expected_revision: int) -> ConnectorConfig:
        try:
            connector_id = str(uuid.UUID(connector_id))
        except (ValueError, AttributeError, TypeError) as exc:
            raise ConnectorPolicyError("connector ID must be a UUID") from exc
        def remove(config: ConnectorConfig) -> None:
            before = len(config.connectors)
            config.connectors[:] = [c for c in config.connectors if c.id != connector_id]
            if len(config.connectors) == before:
                raise ConnectorNotFound("connector not found")
        return self.mutate(expected_revision, remove)

    def remove_project_references(
        self, project_name: str, *, after_persist: Callable[[], None] | None = None,
    ) -> ConnectorConfig:
        """Remove memberships, then run the project deletion under the same lock.

        Holding the parent mutation lock through after_persist prevents a
        concurrent connector edit from restoring a selected membership between
        the policy write and registry removal. Failure after the policy write
        remains fail-closed.
        """
        with self._lock:
            current = self._read()
            candidate = current.model_copy(deep=True)
            changed = False
            for connector in candidate.connectors:
                if project_name in connector.project_access:
                    del connector.project_access[project_name]
                    changed = True
            if changed:
                candidate.revision += 1
                self._write(candidate)
            if after_persist is not None:
                after_persist()
            return candidate if changed else current


class WorkspaceConnectorStore:
    """Atomic, revisioned persistence for independent Workspace-only surfaces."""

    _locks: dict[str, threading.RLock] = {}
    _locks_guard = threading.Lock()

    def __init__(self, path: Path):
        self.path = Path(path)
        key = str(self.path.resolve())
        with self._locks_guard:
            self._lock = self._locks.setdefault(key, threading.RLock())

    def _read(self) -> WorkspaceConnectorConfig:
        if not self.path.exists():
            return WorkspaceConnectorConfig()
        try:
            if self.path.stat().st_size > MAX_CONFIG_BYTES:
                raise PolicyUnavailable("Workspace connector policy exceeds the bounded read limit")
            raw = yaml.safe_load(self.path.read_text(encoding="utf-8")) or {}
            if not isinstance(raw, dict):
                raise ValueError("top-level YAML value must be a mapping")
            return WorkspaceConnectorConfig.model_validate(raw)
        except PolicyUnavailable:
            raise
        except (OSError, UnicodeError, yaml.YAMLError, ValidationError, ValueError, TypeError) as exc:
            log.error(
                "Workspace connector policy load failed path=%s error=%s",
                self.path,
                type(exc).__name__,
            )
            raise PolicyUnavailable("Workspace connector policy is unavailable") from exc

    def _write(self, config: WorkspaceConnectorConfig) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(
            prefix=f".{self.path.name}.", suffix=".tmp", dir=self.path.parent
        )
        temp_path = Path(temp_name)
        try:
            os.chmod(temp_path, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
                yaml.safe_dump(
                    config.model_dump(mode="json"), stream,
                    sort_keys=False, allow_unicode=True,
                )
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp_path, self.path)
            try:
                os.chmod(self.path, 0o600)
            except OSError:
                pass
        except Exception:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                log.error("Workspace connector temporary cleanup failed path=%s", temp_path)
            raise

    def snapshot(self) -> WorkspaceConnectorConfig:
        with self._lock:
            return self._read().model_copy(deep=True)

    def _mutate(
        self, expected_revision: int,
        mutation: Callable[[WorkspaceConnectorConfig], WorkspaceConnectorDefinition | None],
    ) -> tuple[WorkspaceConnectorConfig, WorkspaceConnectorDefinition | None]:
        with self._lock:
            current = self._read()
            if expected_revision != current.revision:
                raise RevisionConflict(
                    f"expected revision {expected_revision}, current is {current.revision}"
                )
            candidate = current.model_copy(deep=True)
            result = mutation(candidate)
            candidate.revision = current.revision + 1
            candidate = WorkspaceConnectorConfig.model_validate(candidate.model_dump())
            self._write(candidate)
            return candidate, result

    def create(
        self, *, expected_revision: int, display_name: str,
        enabled: bool = False, slug: str | None = None,
    ) -> WorkspaceConnectorDefinition:
        def add(config: WorkspaceConnectorConfig) -> WorkspaceConnectorDefinition:
            base = slug or connector_slug_from_name(display_name)
            selected = base
            suffix = 2
            used = {item.slug for item in config.workspace_connectors}
            while selected in used:
                tail = f"-{suffix}"
                selected = f"{base[:MAX_CONNECTOR_SLUG - len(tail)].rstrip('-')}{tail}"
                suffix += 1
            row = WorkspaceConnectorDefinition(
                id=str(uuid.uuid4()), slug=selected,
                display_name=display_name, enabled=enabled,
            )
            config.workspace_connectors.append(row)
            return row
        _config, row = self._mutate(expected_revision, add)
        assert row is not None
        return row

    add = create

    def update(
        self, surface_id: str, *, expected_revision: int,
        display_name: str | None = None, enabled: bool | None = None,
    ) -> WorkspaceConnectorDefinition:
        try:
            surface_id = str(uuid.UUID(surface_id))
        except (ValueError, AttributeError, TypeError) as exc:
            raise ConnectorNotFound("Workspace connector not found") from exc

        def edit(config: WorkspaceConnectorConfig) -> WorkspaceConnectorDefinition:
            current = next(
                (item for item in config.workspace_connectors if item.id == surface_id), None
            )
            if current is None:
                raise ConnectorNotFound("Workspace connector not found")
            replacement = current.model_copy(update={
                "display_name": current.display_name if display_name is None else display_name,
                "enabled": current.enabled if enabled is None else enabled,
                "revision": current.revision + 1,
            })
            replacement = WorkspaceConnectorDefinition.model_validate(replacement.model_dump())
            config.workspace_connectors[config.workspace_connectors.index(current)] = replacement
            return replacement
        _config, row = self._mutate(expected_revision, edit)
        assert row is not None
        return row

    edit = update

    def delete(
        self, surface_id: str, *, expected_revision: int, confirm: bool = False,
    ) -> dict[str, int | str]:
        if not confirm:
            raise ConnectorPolicyError("explicit confirmation is required")
        try:
            surface_id = str(uuid.UUID(surface_id))
        except (ValueError, AttributeError, TypeError) as exc:
            raise ConnectorNotFound("Workspace connector not found") from exc

        def disable(config: WorkspaceConnectorConfig) -> WorkspaceConnectorDefinition:
            current = next(
                (item for item in config.workspace_connectors if item.id == surface_id), None
            )
            if current is None:
                raise ConnectorNotFound("Workspace connector not found")
            replacement = current.model_copy(update={
                "enabled": False,
                "revision": current.revision + 1,
            })
            config.workspace_connectors[config.workspace_connectors.index(current)] = replacement
            return replacement
        config, _row = self._mutate(expected_revision, disable)
        # Connector deletion is intentionally a reversible disable. Credentials and
        # the surface identity remain durable; deleting a Workspace is a separate API.
        return {"deleted": surface_id, "revision": config.revision}

    remove = delete


def migrate_connectors(registry: Any, store: ConnectorStore, *, dry_run: bool = False) -> MigrationResult:
    """Create the one default all-project connector from legacy project policy.

    Existing valid connectors are intentionally left untouched, making this
    command safe to retry after an interrupted service restart.  The legacy
    ``writable`` values remain in the project document for compatibility; the
    new policy layer owns remote access after cutover.
    """
    projects = list(getattr(registry, "projects", ()))
    had_file = store.path.exists()
    current = store.snapshot()
    if current.connectors:
        return MigrationResult(False, dry_run, current.revision, current.connectors[0].id, ("already migrated; no changes",))
    if had_file and current.revision:
        raise ConnectorPolicyError("connectors.yaml has a nonzero revision but no connectors; repair it before migration")
    connector_id = str(uuid.uuid4())
    overrides = {_project_name(p): "read" for p in projects if not _project_writable(p)}
    plan = [f"create connector {connector_id} (Cognita, all projects, default write)"]
    if overrides:
        plan.append(f"preserve read-only overrides for {len(overrides)} project(s)")
    if dry_run:
        return MigrationResult(True, True, current.revision + 1, connector_id, tuple(plan))
    connector = ConnectorDefinition(id=connector_id, name="Cognita", enabled=True, project_mode="all", default_access="write", project_access=overrides)
    connector.validate_projects(_project_name(p) for p in projects)
    candidate = ConnectorConfig(version=1, revision=current.revision + 1, connectors=[connector])
    store.save(candidate, expected_revision=current.revision)
    log.info("connector migration complete revision=%d connector_id=%s overrides=%d", candidate.revision, connector_id, len(overrides))
    return MigrationResult(True, False, candidate.revision, connector_id, tuple(plan))
