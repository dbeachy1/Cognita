"""Non-mutating inventory for a legacy Prologue/ChapterNN editorial layout."""
from __future__ import annotations

import hashlib
import re
import stat
from pathlib import Path
from typing import Any

_LEGACY_CHAPTER = re.compile(r"^(?P<stem>Prologue|Chapter\d+)\.docx$", re.IGNORECASE)


class EditorialMappingError(ValueError):
    """A dry-run mapping cannot safely inspect its proposed source files."""


def _pointer(parts: tuple[str, ...]) -> str:
    return "/" + "/".join(part.replace("~", "~0").replace("/", "~1") for part in parts)


def _registered_references(value: Any, old: str, parts: tuple[str, ...] = ()) -> list[str]:
    if isinstance(value, dict):
        return [pointer for key, child in value.items()
                for pointer in _registered_references(child, old, (*parts, str(key)))]
    if isinstance(value, list):
        return [pointer for index, child in enumerate(value)
                for pointer in _registered_references(child, old, (*parts, str(index)))]
    return [_pointer(parts)] if value == old else []


def dry_run_editorial_layout(project_root: Path, layout: dict[str, Any]) -> dict[str, object]:
    """Map root-level legacy chapter DOCX files into proposed chapter folders.

    The returned JSON-ready plan is evidence only: it creates no folder, moves
    no bytes, and does not edit the supplied layout.  Every exact layout value
    equal to a proposed source is listed so an approved migration can update
    registrations deliberately rather than guessing which references matter.
    """
    root = Path(project_root).resolve(strict=True)
    if not isinstance(layout, dict):
        raise EditorialMappingError("The editorial layout must be an object.")
    candidates: list[tuple[str, Path, str]] = []
    for source in sorted(root.iterdir(), key=lambda item: item.name.casefold()):
        match = _LEGACY_CHAPTER.fullmatch(source.name)
        if match is None:
            continue
        try:
            facts = source.lstat()
        except OSError as exc:
            raise EditorialMappingError("A legacy chapter source could not be inspected.") from exc
        if not stat.S_ISREG(facts.st_mode) or stat.S_ISLNK(facts.st_mode):
            raise EditorialMappingError("A legacy chapter source must be an ordinary file.")
        source_rel = source.relative_to(root).as_posix()
        stem = match.group("stem")
        destination_rel = f"Chapters/{stem}/{source.name}"
        candidates.append((source_rel, source, destination_rel))

    destinations: dict[str, list[str]] = {}
    for source_rel, _source, destination_rel in candidates:
        destinations.setdefault(destination_rel.casefold(), []).append(source_rel)
    moves: list[dict[str, object]] = []
    collisions: list[dict[str, object]] = []
    for source_rel, source, destination_rel in candidates:
        conflicts: list[str] = []
        target = root / destination_rel
        if target.exists():
            conflicts.append("destination_exists")
        if len(destinations[destination_rel.casefold()]) > 1:
            conflicts.append("duplicate_destination")
        updates = [
            {"json_pointer": pointer, "from": source_rel, "to": destination_rel}
            for pointer in _registered_references(layout, source_rel)
        ]
        move = {
            "source": source_rel,
            "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
            "destination": destination_rel,
            "registered_reference_updates": updates,
            "blocked": bool(conflicts),
        }
        moves.append(move)
        for reason in conflicts:
            collisions.append({"source": source_rel, "destination": destination_rel, "reason": reason})
    return {"schema_version": 1, "moves": moves, "collisions": collisions}


__all__ = ["EditorialMappingError", "dry_run_editorial_layout"]
