"""Backup-before-write (DESIGN.md §6.1).

Every destructive edit (update/remove, or an add that would overwrite) copies the
current file to `<documents_dir>/backups/<subpath>/<name>.<timestamp><ext>` BEFORE
the worker touches it. No overwrite or delete proceeds without a successful backup.
The backups/ tree is excluded from indexing (worker config).
"""

from __future__ import annotations

import logging
import re
import shutil
from datetime import datetime
from pathlib import Path

log = logging.getLogger("cognita.backups")

BACKUPS_DIRNAME = "backups"

# Backup filenames: <original-stem>.<YYYYMMDD-HHMMSS[-N]><original-ext>,
# mirrored under backups/<original-subpath>/. The stamp doubles as the
# backup_id; -N disambiguates several writes within the same second (without
# it the second write's backup would silently OVERWRITE the first's — a real
# lost-generation bug caught by the 2.5 live smoke).
_BACKUP_ID_RE = r"\d{8}-\d{6}(?:-\d+)?"
_BACKUP_STEM_RE = re.compile(rf"^(?P<orig>.+)\.(?P<ts>{_BACKUP_ID_RE})$")

LIST_BACKUPS_TOOL_NAME = "list_backups"
RESTORE_BACKUP_TOOL_NAME = "restore_backup"

LIST_BACKUPS_TOOL_DEF: dict = {
    "name": LIST_BACKUPS_TOOL_NAME,
    "description": (
        "List the automatic backups Cognita creates before every destructive write "
        "(update/edit/remove/overwrite). Backups live under backups/ inside the "
        "project's documents folder (excluded from indexing), mirror the original "
        "subpath, and are named <name>.<YYYYMMDD-HHMMSS><ext> — that timestamp is the "
        "backup_id. With filepath: that document's backups, newest first. Without: "
        "every backup in the project, narrowable by prefix/since/until — that is how a "
        "BULK operation is rolled back as a set (a copy_directory or remove_directory "
        "makes one backup per file, all under the same prefix and within the same "
        "second or two). Feed a backup_id to restore_backup to roll a document back. "
        "Read-only, no side effects."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "filepath": {
                "type": "string",
                "description": (
                    "Optional: a document path (absolute or relative to the documents "
                    "folder) to list only that document's backups."
                ),
            },
            "prefix": {
                "type": "string",
                "description": (
                    "Optional: list only backups whose ORIGINAL document path starts with "
                    "this (e.g. 'notes/research/'). Combine with "
                    "since/until to recover exactly one bulk operation."
                ),
            },
            "since": {
                "type": "string",
                "description": (
                    "Optional lower bound on backup_id, inclusive. Accepts a full "
                    "'YYYYMMDD-HHMMSS' id, a 'YYYYMMDD' date (start of day) or an ISO "
                    "timestamp."
                ),
            },
            "until": {
                "type": "string",
                "description": (
                    "Optional upper bound on backup_id, inclusive. A bare 'YYYYMMDD' "
                    "means the END of that day."
                ),
            },
        },
        "required": [],
    },
}

DIFF_BACKUP_TOOL_NAME = "diff_backup"

DIFF_BACKUP_TOOL_DEF: dict = {
    "name": DIFF_BACKUP_TOOL_NAME,
    "description": (
        "Show what changed in a document SINCE a backup was taken — a unified diff "
        "from that backup to the current file, without restoring anything. Turns the "
        "backup trail into a history view: answer 'what did we change this week?' from "
        "diffs instead of restore-and-peek. Read-only, no side effects. Use "
        "list_backups to find backup_ids; identical content returns identical:true "
        "with no diff."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "filepath": {
                "type": "string",
                "description": (
                    "The document (absolute or relative to the documents folder) — "
                    "the ORIGINAL path, not the backup file's path."
                ),
            },
            "backup_id": {
                "type": "string",
                "description": "Timestamp id from list_backups, e.g. '20260702-015013'.",
            },
        },
        "required": ["filepath", "backup_id"],
    },
}

RESTORE_BACKUP_TOOL_DEF: dict = {
    "name": RESTORE_BACKUP_TOOL_NAME,
    "description": (
        "Restore a document to a previous automatic backup — the undo button. The "
        "document's CURRENT content is backed up first, so a restore is itself "
        "undoable — the result's previous_backup_id names that snapshot. Provide the document's filepath and a backup_id (the "
        "YYYYMMDD-HHMMSS timestamp from list_backups). The restored content is "
        "written and re-indexed immediately; the result includes a unified diff of "
        "current -> restored. To revert the most recent edit: list_backups(filepath), "
        "then restore_backup with the newest backup_id."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "filepath": {
                "type": "string",
                "description": (
                    "The document to restore (absolute or relative to the documents "
                    "folder) — the ORIGINAL path, not the backup file's path."
                ),
            },
            "backup_id": {
                "type": "string",
                "description": "Timestamp id from list_backups, e.g. '20260702-015013'.",
            },
            "expected_sha256": {
                "type": "string",
                "description": (
                    "Optional staleness guard: the content_sha256 of the CURRENT file "
                    "from a prior read_document; the restore is rejected if the file "
                    "changed since."
                ),
            },
            "expected_bytes_sha256": {
                "type": "string",
                "description": "Optional exact SHA-256 of CURRENT persisted bytes (64 hexadecimal characters).",
            },
        },
        "required": ["filepath", "backup_id"],
    },
}


class BackupError(Exception):
    """Raised when a required backup cannot be made — the write must be aborted."""


def _timestamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


# Windows reserved device names. Opening one of these writes to a DEVICE rather
# than a file, and every variant with an extension resolves to the same device.
_WIN_RESERVED = {
    "con", "prn", "aux", "nul",
    *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
}


def resolve_target(documents_dir: Path, filepath: str) -> Path | None:
    """Resolve a tool's `filepath` against documents_dir, refusing path escapes.

    Also refuses two shapes that survive the containment check but are not
    ordinary files. Windows-only in effect, so prod (Linux) is unaffected — but
    the dev box is Windows and this is the only path-hygiene chokepoint every
    write tool shares, so it belongs here rather than at four call sites:

      * an NTFS alternate data stream — ``notes.md:hidden`` resolves INSIDE the
        tree and writes content the indexer, `list_documents` and every backup
        cannot see;
      * a reserved device name — ``CON.md``, ``NUL.md`` resolve inside the tree
        and open a device instead of a file. `add_from_url` can generate such a
        name straight from a page's <title>.
    """
    try:
        docs = Path(documents_dir).resolve()
        target = (docs / filepath).resolve()
        rel = target.relative_to(docs)  # raises if outside the documents dir
    except (ValueError, OSError):
        return None
    for part in rel.parts:
        # A drive-relative spelling like "C:name" is caught by the containment
        # check above; what reaches here is "file.md:stream".
        if ":" in part:
            return None
        if part.split(".")[0].lower() in _WIN_RESERVED:
            return None
    return target


def backup_if_exists(documents_dir: Path, filepath: str, keep: int | None = None) -> Path | None:
    """Copy documents_dir/filepath into backups/ with a timestamp, if it exists.

    Returns the backup path, or None if there was nothing to back up (new file).
    Raises BackupError if the file exists but the backup could not be written —
    the caller MUST NOT proceed with the write in that case.

    keep (config backup_keep_per_file): after a successful backup, prune this
    file's backups down to the newest `keep`; every deletion is logged.
    None/0 = unlimited.
    """
    docs = Path(documents_dir)
    target = resolve_target(docs, filepath)
    if target is None:
        raise BackupError(f"refusing write to path outside project: {filepath!r}")
    if not target.is_file():
        return None  # brand-new file; nothing to preserve

    rel = target.relative_to(docs.resolve())
    # never back up the backups themselves
    if rel.parts and rel.parts[0] == BACKUPS_DIRNAME:
        return None

    ts = _timestamp()
    backup = docs / BACKUPS_DIRNAME / rel.parent / f"{rel.stem}.{ts}{rel.suffix}"
    n = 0
    while backup.exists():  # same-second collision: suffix, never overwrite
        n += 1
        backup = backup.with_name(f"{rel.stem}.{ts}-{n}{rel.suffix}")
    try:
        backup.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(target, backup)
    except OSError as exc:
        raise BackupError(f"could not back up {rel} before write: {exc}") from exc
    log.info("Backed up %s -> %s", rel, backup.relative_to(docs))
    if keep and keep > 0:
        _prune_old_backups(docs, filepath, keep)
    return backup


def _prune_old_backups(docs: Path, filepath: str, keep: int) -> None:
    """Delete this file's backups beyond the newest `keep`, logging each one.

    Best-effort: a prune failure never blocks the write it followed."""
    try:
        entries = list_backup_entries(docs, filepath)
        for entry in entries[keep:]:
            victim = find_backup(docs, filepath, entry["backup_id"])
            if victim is None:
                continue
            victim.unlink()
            log.info(
                "Pruned backup %s (keeping newest %d per file)",
                victim.relative_to(docs), keep,
            )
    except (BackupError, OSError) as exc:
        log.warning("Backup pruning skipped for %s: %s", filepath, exc)


# --------------------------------------------------------------- introspection


def _entry(docs: Path, backup: Path) -> dict | None:
    """Parse one backup file into a listing entry; None if not a backup name."""
    m = _BACKUP_STEM_RE.match(backup.stem)
    suffix = backup.suffix
    if not m:
        # Extensionless original (e.g. 'README' -> backup 'README.20260702-120000'):
        # the timestamp IS the path suffix, so match against the full name.
        m = _BACKUP_STEM_RE.match(backup.name)
        suffix = ""
        if not m:
            return None
    ts = m.group("ts")
    try:  # -N collision suffix is not part of the wall-clock stamp
        created = datetime.strptime(ts[:15], "%Y%m%d-%H%M%S").isoformat(sep=" ")
    except ValueError:
        return None
    rel_backup = backup.relative_to(docs / BACKUPS_DIRNAME)
    original = rel_backup.parent / f"{m.group('orig')}{suffix}"
    try:
        size = backup.stat().st_size
    except OSError:
        size = None
    return {
        "filepath": original.as_posix(),
        "backup_id": ts,
        "created": created,
        "size_bytes": size,
    }


def normalize_stamp(value: str | None, *, end: bool = False) -> str | None:
    """A since/until bound as a comparable 'YYYYMMDD-HHMMSS' string (5.0 §7.3).

    backup_ids sort lexicographically because the format is fixed-width, so a
    range filter is a pair of string comparisons once the bound is in the same
    shape. A bare date means the START of that day for `since` and the END of it
    for `until`, which is what someone typing "everything from the 29th" means.
    Anything unparseable returns None (no bound) rather than raising: a filter
    that silently matched nothing would be the 2.4 failure in a new place.
    """
    if not value:
        return None
    raw = str(value).strip()
    if re.fullmatch(rf"{_BACKUP_ID_RE}", raw):
        return raw[:15]
    if re.fullmatch(r"\d{8}", raw):
        return f"{raw}-235959" if end else f"{raw}-000000"
    try:
        parsed = datetime.fromisoformat(raw.replace("/", "-"))
    except ValueError:
        log.warning("Ignoring unparseable backup time bound: %r", raw)
        return None
    return parsed.strftime("%Y%m%d-%H%M%S")


def _normalize_prefix(prefix: str | None) -> str | None:
    if not prefix:
        return None
    cleaned = str(prefix).strip().replace("\\", "/")
    while cleaned.startswith("./"):
        cleaned = cleaned[2:]
    return cleaned.lstrip("/") or None


def list_backup_entries(
    documents_dir: Path,
    filepath: str | None = None,
    *,
    prefix: str | None = None,
    since: str | None = None,
    until: str | None = None,
) -> list[dict]:
    """Backups for one document (newest first), or every backup in the project,
    optionally narrowed to a path prefix and/or a backup_id time range.

    Raises BackupError for a filepath outside the project."""
    docs = Path(documents_dir).resolve()
    root = docs / BACKUPS_DIRNAME
    if not root.is_dir():
        return []
    if filepath:
        target = resolve_target(docs, filepath)
        if target is None:
            raise BackupError(f"filepath resolves outside this project: {filepath!r}")
        rel = target.relative_to(docs)
        # iterdir + exact-match filter, NOT a glob built from the filename:
        # names like 'note [draft].md' would turn '[draft]' into a character
        # class and make the file's backups invisible to list/prune/hints.
        parent = root / rel.parent
        candidates = parent.iterdir() if parent.is_dir() else iter(())
    else:
        candidates = root.rglob("*")
    entries = [e for p in candidates if p.is_file() and (e := _entry(docs, p))]
    if filepath:
        rel_posix = rel.as_posix()
        entries = [e for e in entries if e["filepath"] == rel_posix]
    if (path_prefix := _normalize_prefix(prefix)) is not None:
        entries = [e for e in entries if e["filepath"].startswith(path_prefix)]
    if (lo := normalize_stamp(since)) is not None:
        entries = [e for e in entries if e["backup_id"][:15] >= lo]
    if (hi := normalize_stamp(until, end=True)) is not None:
        entries = [e for e in entries if e["backup_id"][:15] <= hi]
    entries.sort(key=lambda e: e["backup_id"], reverse=True)
    return entries


def backup_id_of(backup: Path) -> str | None:
    """The timestamp id encoded in a backup filename (extensionless-aware)."""
    m = _BACKUP_STEM_RE.match(backup.stem) or _BACKUP_STEM_RE.match(backup.name)
    return m.group("ts") if m else None


def find_backup(documents_dir: Path, filepath: str, backup_id: str) -> Path | None:
    """Locate the backup file for (document, backup_id); None if absent."""
    docs = Path(documents_dir).resolve()
    target = resolve_target(docs, filepath)
    if target is None or not re.fullmatch(_BACKUP_ID_RE, backup_id):
        return None
    rel = target.relative_to(docs)
    backup = docs / BACKUPS_DIRNAME / rel.parent / f"{rel.stem}.{backup_id}{rel.suffix}"
    return backup if backup.is_file() else None
