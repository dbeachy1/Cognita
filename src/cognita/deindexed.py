"""The durable de-index list (5.7, DESIGN-5.0 §11.3).

`remove_document(delete_file=false)` used to be self-reverting. It dropped the
index row, left the file on disk, and returned `status: "success"` with a
`reindex_warning` explaining that the watcher would put the row back on the next
filesystem event in the project. So the postcondition a caller reads out of
`success` held for an unbounded short interval and then undid itself, and a
caller that branched on `status` — the obvious thing to do — believed the
document was gone while search still returned it.

Prose in one field cannot fix that: the operation had no durable meaning. This
module gives it one. Each project keeps a list of paths that are deliberately NOT
indexed, and every indexing path consults it:

    remove_document(delete_file=false)  ->  the path joins the list
    the walk in index_project           ->  listed paths are skipped, and their
                                            rows are swept away like any file
                                            that vanished
    add/update/copy/move onto the path  ->  the path LEAVES the list

so `delete_file: false` means "keep the file, stop indexing it", which is a
coherent operation and the one callers actually wanted.

**Why a file and not a table.** The index is derived data — DESIGN-4.0's recovery
story is `pg_dump` for backup and reindex-from-source for repair, and dropping a
schema to rebuild it from disk is a supported move. A de-index is the one piece of
state here that CANNOT be re-derived from the corpus: it is a decision. Put it in
the schema and the documented recovery path silently reverts every one of them —
the same self-reverting bug this module exists to remove, with a longer fuse. It
lives beside the project instead, in `data_dir`, where a rebuild cannot reach it.

The list is by PATH, not by content: a new file later written to a listed path is
still not indexed until something re-admits it. That is deliberate — the caller
suppressed a location — and `get_index_stats` reports the whole list so the
exclusion is never silent, exactly as 5.0 §10 requires of sync-conflict skips.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

log = logging.getLogger("cognita.deindexed")

FILENAME = "deindexed.json"
FORMAT_VERSION = 1


class DeindexedPaths:
    """A project's de-indexed paths, persisted as JSON beside its data dir.

    Project-relative POSIX paths, exactly as `source` is spelled in the store, so
    a membership test needs no conversion at the call sites that matter (the walk
    in `index_project`, one string compare per file).

    Loaded once and then held in memory: one `cognita serve` owns a project (see
    CLAUDE.md), so the in-memory set is authoritative and every mutation rewrites
    the file atomically. Reads never touch the disk.
    """

    def __init__(self, path: Path):
        self.path = Path(path)
        self._paths: set[str] | None = None
        # A file we could not parse is reported, not swallowed. Losing the list
        # means files start being indexed again — not data loss, but a silent
        # reversal of an explicit decision, which is the failure mode this whole
        # module is about. get_index_stats surfaces this string.
        self.load_error: str | None = None

    # ---------------- reading ----------------

    def paths(self) -> set[str]:
        """The de-indexed set (loaded on first use). Never None."""
        if self._paths is None:
            self._paths = self._load()
        return self._paths

    def __contains__(self, source: str) -> bool:
        return source in self.paths()

    def sorted(self) -> list[str]:
        return sorted(self.paths())

    def _load(self) -> set[str]:
        if not self.path.is_file():
            return set()
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            listed = raw["paths"] if isinstance(raw, dict) else raw
            if not isinstance(listed, list):
                raise ValueError(f"'paths' is {type(listed).__name__}, expected a list")
            loaded = {str(p) for p in listed}
        except (OSError, ValueError, KeyError) as exc:
            # Empty, NOT a rewrite: overwriting an unreadable list would destroy
            # the only copy of the decisions it holds, and a transient read error
            # would make that permanent. The file stays exactly as it is until
            # someone fixes it or a mutation deliberately replaces it.
            self.load_error = f"{self.path}: {exc}"
            log.error(
                "De-index list unreadable (%s) — treating it as EMPTY, so every path "
                "it listed will be indexed again. The file was NOT rewritten; fix or "
                "delete it and re-issue the remove_document calls.", self.load_error,
            )
            return set()
        self.load_error = None
        return loaded

    # ---------------- writing ----------------

    def add(self, source: str) -> bool:
        """Suppress `source`. True if it was not already suppressed."""
        current = self.paths()
        if source in current:
            return False
        current.add(source)
        self._save()
        return True

    def discard(self, source: str) -> bool:
        """Re-admit `source`. True if it had been suppressed."""
        current = self.paths()
        if source not in current:
            return False
        current.discard(source)
        self._save()
        return True

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Temp-then-replace: a half-written list read after a crash would be
        # unparseable, and _load treats unparseable as empty — i.e. every
        # suppression silently undone. os.replace is atomic on both platforms.
        tmp = self.path.with_name(self.path.name + ".tmp")
        payload = {"version": FORMAT_VERSION, "paths": sorted(self._paths or ())}
        tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, self.path)
