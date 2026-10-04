"""Project registry — the single source of truth for configured projects.

Backed by config/registry.yaml (DESIGN.md §5.1). Written ONLY by the admin
backend / CLI. Stores token hashes, never plaintext tokens.
"""

from __future__ import annotations

import logging
import re
import threading
from datetime import UTC, datetime
from pathlib import Path

import yaml
from pydantic import BaseModel, StrictBool, field_validator

# \A...\Z, not ^...$: Python's $ also matches just BEFORE a terminal newline, so
# a project name of "Example\n" validated, and store.schema_for then built the
# identifier proj_Example\n — legal, correctly quoted, and different from proj_Example.
# Not injection (no quote survives the character class), but two registry
# entries that read as the same project would own two separate schemas.
NAME_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9-]*\Z")

REGISTRY_VERSION = 1
log = logging.getLogger("cognita.registry")


class Project(BaseModel):
    """One isolated knowledge base."""

    name: str
    documents_dir: Path
    data_dir: Path
    token_sha256: str = ""
    enabled: bool = True
    writable: bool = True  # allow edit/delete tools; every write is backed up first
    exclude_from_default_permissions: StrictBool = False
    worker_port: int | None = None  # runtime-assigned; persisted for stability
    created_at: str = ""
    # 4.4 per-connector document tiers. None = inherit the global default from
    # config/cognita.yaml. Set either list here to scope tiers to THIS project
    # (DESIGN-4.4-registered-tier.md §2) — e.g. register .js/.css for one
    # project without other projects picking up scripts.
    indexed_extensions: list[str] | None = None
    registered_extensions: list[str] | None = None

    @field_validator("name")
    @classmethod
    def _valid_name(cls, v: str) -> str:
        if not NAME_RE.match(v):
            raise ValueError(
                f"Invalid project name {v!r}: use letters/digits/hyphens, starting alphanumeric"
            )
        return v


class Registry:
    """Load/save the registry YAML and provide project CRUD + token lookup."""

    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.RLock()
        self.projects: list[Project] = []
        self.authentication_store = None
        self.load()

    def attach_authentication_store(self, store) -> None:
        """Attach the parent-owned policy store for project lifecycle joins."""
        with self._lock:
            self.authentication_store = store
            setter = getattr(store, "set_project_names", None)
            if callable(setter):
                setter(project.name for project in self.projects)

    # ---------- persistence ----------

    def load(self) -> None:
        with self._lock:
            if not self.path.is_file():
                self.projects = []
                return
            with open(self.path, encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            self.projects = [Project(**p) for p in data.get("projects", [])]

    def save(self) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            data = {
                "version": REGISTRY_VERSION,
                "projects": [
                    # mode="json" serializes Paths as strings for clean YAML
                    p.model_dump(mode="json")
                    for p in self.projects
                ],
            }
            # BOM-free UTF-8, atomic replacement under the registry lock.
            tmp = self.path.with_suffix(".yaml.tmp")
            with open(tmp, "w", encoding="utf-8", newline="\n") as f:
                yaml.safe_dump(data, f, sort_keys=False, allow_unicode=True)
            tmp.replace(self.path)

    # ---------- lookups ----------

    def get(self, name: str) -> Project | None:
        return next((p for p in self.projects if p.name == name), None)

    def find_by_token(self, presented_token: str) -> Project | None:
        """Constant-time token match across enabled projects (DESIGN.md §6)."""
        from .tokens import token_matches

        match: Project | None = None
        # Check ALL projects (no early exit) to keep timing uniform.
        for p in self.projects:
            if p.enabled and p.token_sha256 and token_matches(presented_token, p.token_sha256):
                match = p
        return match

    # ---------- mutation ----------

    def add(self, project: Project, authentication_store=None) -> None:
        with self._lock:
            if self.get(project.name):
                raise ValueError(f"Project {project.name!r} already exists")
            store = authentication_store or self.authentication_store
            # A deleted project may leave an inert policy row if cleanup was
            # interrupted. Remove it before re-adding the name so a new
            # project cannot inherit a stale credential.
            if store is not None:
                store.remove_orphan(project.name)
            prior_created_at = project.created_at
            if not project.created_at:
                project.created_at = datetime.now(UTC).isoformat(timespec="seconds")
            self.projects.append(project)
            try:
                self.save()
            except Exception:
                self.projects.pop()
                project.created_at = prior_created_at
                raise
            if store is not None:
                setter = getattr(store, "set_project_names", None)
                if callable(setter):
                    setter(item.name for item in self.projects)

    def remove(self, name: str, authentication_store=None) -> Project:
        with self._lock:
            p = self.get(name)
            if p is None:
                raise KeyError(f"No project named {name!r}")
            store = authentication_store or self.authentication_store
            index = self.projects.index(p)
            self.projects.remove(p)
            try:
                self.save()
            except Exception:
                self.projects.insert(index, p)
                raise
            if store is not None:
                setter = getattr(store, "set_project_names", None)
                if callable(setter):
                    setter(item.name for item in self.projects)
                try:
                    store.remove_project(name)
                except Exception:
                    # Registry removal is intentionally first: even if policy
                    # cleanup is unavailable, the key cannot authorize a
                    # missing project. Admin repair can retry this operation.
                    log.exception("Authentication policy cleanup failed project=%s", name)
            return p

    def update(self) -> None:
        """Persist in-place mutations made to Project objects."""
        with self._lock:
            self.save()

    def update_settings(
        self, name: str, *, exclude_from_default_permissions: bool,
    ) -> Project:
        """Atomically set the project-owned connector default exclusion."""
        if not isinstance(exclude_from_default_permissions, bool):
            raise TypeError("exclude_from_default_permissions must be a boolean")
        with self._lock:
            project = self.get(name)
            if project is None:
                raise KeyError(f"No project named {name!r}")
            prior = project.exclude_from_default_permissions
            project.exclude_from_default_permissions = exclude_from_default_permissions
            try:
                self.save()
            except Exception:
                project.exclude_from_default_permissions = prior
                raise
            log.info(
                "Project default connector permissions updated project=%s excluded=%s",
                name, exclude_from_default_permissions,
            )
            return project.model_copy(deep=True)
