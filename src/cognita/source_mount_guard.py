"""Fail-closed checks for the optional Windows installation source projection.

KEI has no projection and deliberately keeps the existing source behavior.
The projection is ephemeral and contains only alias-to-runtime identity facts;
this module never opens document contents or logs host paths/file names.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

log = logging.getLogger("cognita.sources")


@dataclass(frozen=True)
class SourceStatus:
    state: str
    reason: str | None = None
    mount_instance: tuple[int, int, int] | None = None
    collision_paths: tuple[str, str] | None = None


class SourceMountGuard:
    """Validate mapped source roots and remember reconnects for reconciliation."""

    def __init__(self, projection_path: str | Path | None = None):
        value = projection_path or os.environ.get("COGNITA_SOURCE_IDENTITIES_FILE", "")
        if not value and Path("/run/cognita/source-identities.json").is_file():
            value = "/run/cognita/source-identities.json"
        self.projection_path = Path(value) if value else None
        self._entries: dict[str, dict[str, Any]] = {}
        self._baseline: dict[str, tuple[int, int, int]] = {}
        self._unavailable: set[str] = set()
        self._startup_unavailable: set[str] = set()
        self._reported: dict[str, str] = {}
        self._enabled = self.projection_path is not None
        if self._enabled:
            self._load()

    @property
    def enabled(self) -> bool:
        return self._enabled

    def _load(self) -> None:
        try:
            payload = json.loads(
                self.projection_path.read_text(encoding="utf-8"),
                object_pairs_hook=self._object_without_duplicate_keys,
            )
        except (OSError, UnicodeError, ValueError) as exc:
            raise RuntimeError("Windows source identity projection is unavailable") from exc

        if (not isinstance(payload, dict)
                or set(payload) != {"schema", "sources"}
                or type(payload["schema"]) is not int
                or payload["schema"] != 1
                or not isinstance(payload["sources"], list)):
            raise RuntimeError("Windows source identity projection has an invalid shape")

        for row in payload["sources"]:
            if (not isinstance(row, dict)
                    or set(row) != {"alias", "source_kind", "observation", "identity"}):
                raise RuntimeError("Windows source identity projection has an invalid source row")
            alias = row["alias"]
            source_kind = row["source_kind"]
            observation = row["observation"]
            identity = row["identity"]
            if (type(alias) is not str or not alias or alias in {".", ".."}
                    or "/" in alias or "\\" in alias):
                raise RuntimeError("Windows source identity projection has an invalid alias")
            if (type(source_kind) is not str
                    or source_kind not in {"ntfs", "smb", "installation_ext4"}):
                raise RuntimeError("Windows source identity projection has an invalid source kind")
            if (type(observation) is not str
                    or observation not in {"available", "unavailable"}):
                raise RuntimeError("Windows source identity projection has an invalid observation")
            if (not isinstance(identity, dict)
                    or set(identity) != {"device", "inode"}
                    or type(identity["device"]) is not int or identity["device"] < 0
                    or type(identity["inode"]) is not int or identity["inode"] < 0):
                raise RuntimeError("Windows source identity projection has invalid identity facts")
            if any(alias.casefold() == prior.casefold() for prior in self._entries):
                raise RuntimeError("Windows source identity projection contains case-folding aliases")
            self._entries[alias] = row
            if observation == "unavailable":
                self._unavailable.add(alias)
                self._startup_unavailable.add(alias)

    @staticmethod
    def _object_without_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        """Reject duplicate JSON member names instead of accepting the last value."""
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON member")
            result[key] = value
        return result

    def _report_failure(self, alias: str, reason: str, *, level: int = logging.WARNING) -> None:
        """Log source state transitions once without exposing host paths."""
        if self._reported.get(alias) == reason:
            return
        self._reported[alias] = reason
        source_kind = self._entries.get(alias, {}).get("source_kind", "unknown")
        log.log(
            level, "Source mount unavailable alias=%s source_kind=%s reason=%s",
            alias, source_kind, reason,
        )

    @staticmethod
    def _mount_id(path: Path) -> int | None:
        """Return the mount id for the deepest mount containing *path*."""
        try:
            resolved = str(path.resolve())
            candidates: list[tuple[int, int]] = []
            for line in Path("/proc/self/mountinfo").read_text(encoding="utf-8").splitlines():
                left, _separator, _right = line.partition(" - ")
                fields = left.split()
                if len(fields) < 5:
                    continue
                mountpoint = fields[4].replace("\\040", " ").replace("\\011", "\t")
                if resolved == mountpoint or resolved.startswith(mountpoint.rstrip("/") + "/"):
                    candidates.append((len(mountpoint), int(fields[0])))
            return max(candidates)[1] if candidates else None
        except (OSError, ValueError):
            return None

    @staticmethod
    def _project_alias(project_root: Path, source_root: Path) -> str | None:
        try:
            relative = project_root.resolve(strict=False).relative_to(source_root.resolve(strict=False))
        except (OSError, ValueError):
            return None
        return relative.parts[0] if relative.parts else None

    @staticmethod
    def casefold_collision(root: Path) -> tuple[str, str] | None:
        """Find the first pair of relative names that collide on Windows."""
        if not root.is_dir():
            return None
        def fail_walk(error: OSError) -> None:
            raise error

        for current, dirs, files in os.walk(root, onerror=fail_walk):
            names = dirs + files
            seen: dict[str, str] = {}
            for name in names:
                previous = seen.get(name.casefold())
                rel = (Path(current) / name).relative_to(root).as_posix()
                if previous is not None and previous != rel:
                    return previous, rel
                seen[name.casefold()] = rel
        return None

    def check(self, project_root: str | Path, *, validate_names: bool = False) -> SourceStatus:
        """Check source identity; return a reconnect state until reconciled."""
        if not self._enabled:
            return SourceStatus("available")
        root = Path(project_root)
        source_root = Path(os.environ.get("COGNITA_PROJECTS_ROOT", "/srv/cognita/sources"))
        # The root is a fixed Compose path on the Windows projection. Resolve
        # from the projection's recorded aliases rather than logging any path.
        alias = self._project_alias(root, source_root)
        if alias is None or alias not in self._entries:
            self._report_failure("unknown", "alias_not_configured", level=logging.ERROR)
            return SourceStatus("unavailable", "source_unavailable")
        row = self._entries[alias]
        if alias in self._startup_unavailable:
            # A fresh process must honor the helper's unavailable observation
            # once, even when a path with coincidentally matching stat facts
            # already exists. A later live check can request full reconciliation.
            self._startup_unavailable.discard(alias)
            self._report_failure(alias, "startup_observed_unavailable")
            return SourceStatus("unavailable", "source_unavailable")
        expected = row["identity"]
        alias_root = source_root / alias
        if not root.is_dir():
            self._unavailable.add(alias)
            self._report_failure(alias, "project_root_missing")
            return SourceStatus("unavailable", "source_unavailable")
        try:
            stat = alias_root.stat()
        except OSError:
            self._unavailable.add(alias)
            self._report_failure(alias, "stat_failed")
            return SourceStatus("unavailable", "source_unavailable")
        if expected["device"] != stat.st_dev:
            self._unavailable.add(alias)
            self._report_failure(alias, "device_mismatch", level=logging.ERROR)
            return SourceStatus("unavailable", "source_unavailable")
        if expected["inode"] != stat.st_ino:
            self._unavailable.add(alias)
            self._report_failure(alias, "inode_mismatch", level=logging.ERROR)
            return SourceStatus("unavailable", "source_unavailable")
        mount_id = self._mount_id(alias_root)
        if mount_id is None:
            self._unavailable.add(alias)
            self._report_failure(alias, "mount_missing", level=logging.ERROR)
            return SourceStatus("unavailable", "source_unavailable")
        identity = (int(stat.st_dev), int(stat.st_ino), mount_id)
        if validate_names:
            try:
                collision = self.casefold_collision(root)
            except OSError:
                self._unavailable.add(alias)
                self._report_failure(alias, "directory_unreadable", level=logging.ERROR)
                return SourceStatus("unavailable", "source_unavailable")
            if collision:
                self._unavailable.add(alias)
                self._report_failure(alias, "casefold_collision", level=logging.ERROR)
                return SourceStatus(
                    "unavailable", "case_fold_collision", identity, collision,
                )
        if alias in self._unavailable:
            # A remount may reuse the same mount ID. The mandatory full walk is
            # still the safe response after an observed outage.
            return SourceStatus("reconnected", mount_instance=identity)
        previous = self._baseline.setdefault(alias, identity)
        if previous != identity:
            self._unavailable.add(alias)
            self._report_failure(alias, "mount_instance_changed")
            return SourceStatus("reconnected", mount_instance=identity)
        self._reported.pop(alias, None)
        return SourceStatus("available", mount_instance=identity)

    def mark_reconciled(self, project_root: str | Path) -> SourceStatus:
        if not self._enabled:
            return SourceStatus("available")
        source_root = Path(os.environ.get("COGNITA_PROJECTS_ROOT", "/srv/cognita/sources"))
        alias = self._project_alias(Path(project_root), source_root)
        if alias is None:
            return SourceStatus("available")
        result = self.check(project_root, validate_names=True)
        if result.state in {"available", "reconnected"} and result.mount_instance:
            self._baseline[alias] = result.mount_instance
            self._unavailable.discard(alias)
            self._reported.pop(alias, None)
            return SourceStatus("available", mount_instance=result.mount_instance)
        return result
