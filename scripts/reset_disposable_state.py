#!/usr/bin/env python3
"""Discard and regenerate Cognita's disposable state for one target.

DESIGN-13.0-DOCKER-REWRITE.md section 6.5.  This script is never called by
anything else: not by the release tool, not by the service, not by a unit.
Doug runs it by hand when the index or the Workspace VMs need to be thrown
away and rebuilt.

    python3 scripts/reset_disposable_state.py --target main --scope index
    python3 scripts/reset_disposable_state.py --target main --scope all --apply

Without ``--apply`` it prints the plan and changes nothing.  With ``--apply``
it prints the same plan and then requires the operator to type
``RESET <target> <scope>`` at a prompt.  There is no ``--yes`` and no
environment override, on purpose: this is the one command in the repo that
destroys data that cannot be recovered from a backup, only rebuilt from
source.

What it touches, and nothing else (section 5):

  --scope index       the PostgreSQL data root
  --scope workspaces  the Workspace root, the transfer staging root, and the
                      disposable tables of config/data/workspace-metadata
                      .sqlite3
  --scope all         both

Original documents, published assets, ``backups/``, ``config/`` (registry,
connectors, authentication, credentials, OAuth, TLS, the de-index list), the
``workspace_settings`` row, the models cache, the Toolbox image cache and the
release directories are protected: every deletion root is checked against them
before anything is removed.

There is no journal and no rollback.  A partial failure leaves the unit
stopped, prints what was and was not done, and is finished by running the same
command again.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shutil
import sqlite3
import stat as stat_module
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import release  # the sys.path line above has to run before this import

# The release tool owns the target table, the flock, the unit helpers, the
# Toolbox load and the /healthz check.  Importing them is deliberate: a second
# copy of any of those would be a second thing to keep in step (section 6.5).
Log = release.Log
ReleaseError = release.ReleaseError

SCOPES = ("index", "workspaces", "all")

EXIT_CODES = {
    "reset-complete": 0,
    "usage": 1,
    "refused": 2,
    "cancelled": 3,
    "stop-failed": 4,
    "reset-failed": 5,
    "verify-failed": 6,
}

# Mirrors cognita.workspace.DISPOSABLE_TABLES.  The reset runs on kei's host
# interpreter, which cannot import the package (no venv, no dependencies), so
# the list is repeated here; tests/test_reset_disposable_state.py imports both
# and fails if they ever disagree.  `workspace_settings` is the installation's
# Workspace policy and is NEVER dropped.
DISPOSABLE_TABLES = (
    "workspaces",
    "workspace_leases",
    "workspace_jobs",
    "workspace_idempotency",
    "workspace_admin_idempotency",
    "workspace_growth_reservations",
    "workspace_delete_previews",
    "workspace_delete_apply_items",
    "workspace_schema",  # 13.2.0: the layout stamp; dropped with what it describes
)
PRESERVED_TABLES = ("workspace_settings",)
METADATA_DB_NAME = "workspace-metadata.sqlite3"

# cognita.assets.publication.JOURNAL_DIRNAME.  A journal entry here is an asset
# publication that got as far as the file system but whose catalog row may not
# have committed; recovery reads it at startup and reconciles against
# PostgreSQL.  Wiping the index would erase the other half of that decision, so
# an index reset refuses while one is pending (section 5).
ASSET_JOURNAL_DIRNAME = ".cognita-asset-journal"

CATALOG_WARNING = (
    "catalog-only asset metadata (metadata_storage=\"catalog\") and OCR results live "
    "ONLY in PostgreSQL. A reindex re-reads documents and asset bytes from disk; it "
    "cannot bring those back. They are lost for good."
)


class ResetError(ReleaseError):
    """A refusal or a failure, carrying one of this script's exit states."""


# --------------------------------------------------------------------------
# Layout: what this target's roots actually are
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Layout:
    """Every path the reset cares about, resolved from ONE env file.

    The env file is the installation's descriptor (section 6.1), so a root
    this script deletes must be named there.  A missing key is a refusal, not
    a guess: the alternative is inventing a path under someone's data.
    """

    target: str
    unit: str
    project: str
    env_file: Path
    config_root: Path | None
    projects_roots: tuple[Path, ...]
    secrets_root: Path | None
    models_root: Path | None
    toolbox_cache_root: Path | None
    postgres_root: Path | None
    workspaces_root: Path | None
    transfers_root: Path | None
    releases_root: Path
    service_uid: int | None
    service_gid: int | None
    # The installer-owned Self-Test root (`local` only; DESIGN-LINUX-INSTALLER
    # 7.4/9).  It lives under releases_root, which is protected already; it is
    # named on its own so a reset can never treat it as something to delete.
    selftest_root: Path | None = None

    @property
    def metadata_db(self) -> Path | None:
        if self.config_root is None:
            return None
        return self.config_root / "data" / METADATA_DB_NAME

    def protected(self) -> tuple[Path, ...]:
        """Paths that must survive, and that no deletion root may contain.

        The Toolbox image cache is deliberately NOT here even though it is
        preserved (section 5).  release.py's own fallback puts it INSIDE the
        Workspace root, and the generated test installation uses that fallback,
        so treating it as a containment refusal would make a workspaces reset
        impossible on an ordinary installation.  It is stepped over during the
        clear instead -- see `clear_root`'s ``keep`` -- and the plan lists it.
        """
        candidates = [
            self.config_root, self.secrets_root, self.models_root,
            self.releases_root, *self.projects_roots, self.selftest_root,
        ]
        return tuple(path for path in candidates if path is not None)

    def roots_for(self, scope: str) -> tuple[tuple[str, Path | None], ...]:
        """The (env key, root) pairs this scope deletes the contents of."""
        index = (("COGNITA_POSTGRES_DATA_ROOT", self.postgres_root),)
        workspaces = (
            ("COGNITA_WORKSPACE_DATA_ROOT", self.workspaces_root),
            ("COGNITA_TRANSFER_STAGING_ROOT", self.transfers_root),
        )
        if scope == "index":
            return index
        if scope == "workspaces":
            return workspaces
        return index + workspaces


def _path(values: dict[str, str], key: str) -> Path | None:
    value = values.get(key, "").strip()
    return Path(value) if value else None


def _int(values: dict[str, str], key: str) -> int | None:
    value = values.get(key, "").strip()
    try:
        return int(value)
    except ValueError:
        return None


def registry_data_roots(config_root: Path | None, log: Log) -> tuple[Path, ...]:
    """Every registered project's data directory, as HOST paths.

    registry.yaml records the data directory as the container sees it
    (``/app/config/data/<project>``), because that is what the app reads.  The
    config root is bind-mounted at ``/app/config``, so the host path is that
    prefix swapped back.  Anything else in the file is taken as written.  The
    parse is deliberately a two-line scanner rather than a YAML dependency:
    the host interpreter has no PyYAML, and this only needs one key.
    """
    if config_root is None:
        return ()
    roots: list[Path] = []
    data_root = config_root / "data"
    if data_root.is_dir():
        roots.append(data_root)
    registry = config_root / "registry.yaml"
    if not registry.is_file():
        log.line(f"journal: no registry at {registry}; checking {data_root} only")
        return tuple(roots)
    for line in registry.read_text(encoding="utf-8").splitlines():
        text = line.strip()
        if not text.startswith("data_dir:"):
            continue
        value = text.partition(":")[2].strip().strip("'\"")
        if not value:
            continue
        if value.startswith("/app/config"):
            mapped = config_root / Path(value[len("/app/config"):].lstrip("/"))
        else:
            mapped = Path(value)
        if mapped not in roots:
            roots.append(mapped)
    log.line(f"journal: {len(roots)} project data root(s) to check for pending publications")
    return tuple(roots)


def layout_for(target: release.Target, log: Log) -> Layout:
    values = release.read_env_file(target.env_file)
    if not values:
        raise ResetError(
            "refused",
            f"the env file for target {target.name} is missing or empty: {target.env_file}. "
            "Every root this script may delete is named there; it refuses to guess one.",
        )
    workspaces_root = _path(values, "COGNITA_WORKSPACE_DATA_ROOT")
    # release.py owns the rule for where the archive lives (its own
    # `toolbox_cache_root`: the env key, else `toolbox-cache` beside the
    # Workspace root), and the generated throwaway installation uses that
    # fallback, which puts the cache INSIDE the root a `workspaces` reset
    # empties.  It is a release artifact and is never deleted (section 5), so
    # it has to be known here or the reset would take it with the rest.  Ask
    # release.py rather than repeating the rule; an env file that names
    # neither key leaves it unknown, which only a Workspace reset cares about.
    try:
        toolbox_root: Path | None = release.toolbox_cache_root(target.env_file)
    except ReleaseError as exc:
        toolbox_root = None
        log.line(f"layout: the Toolbox image cache root is unknown: {exc}")
    # EVERY documents root is protected: COGNITA_PROJECTS_ROOT and, on `local`,
    # COGNITA_PROJECTS_ROOT_2 .. _9 (design section 9), plus the Self-Test root.
    projects_roots = tuple(Path(path) for _key, path in release.document_roots(values))
    selftest = release.selftest_root(target) if target.name == release.LOCAL_TARGET else None
    layout = Layout(
        target=target.name,
        unit=target.unit,
        project=target.project,
        env_file=target.env_file,
        config_root=_path(values, "COGNITA_CONFIG_ROOT"),
        projects_roots=projects_roots,
        secrets_root=_path(values, "COGNITA_SECRETS_ROOT"),
        models_root=_path(values, "COGNITA_MODEL_CACHE_ROOT"),
        toolbox_cache_root=toolbox_root,
        postgres_root=_path(values, "COGNITA_POSTGRES_DATA_ROOT"),
        workspaces_root=workspaces_root,
        transfers_root=_path(values, "COGNITA_TRANSFER_STAGING_ROOT"),
        releases_root=release.target_root(target),
        service_uid=_int(values, "COGNITA_SERVICE_UID"),
        service_gid=_int(values, "COGNITA_SERVICE_GID"),
        selftest_root=selftest,
    )
    log.line(f"layout: target={layout.target} unit={layout.unit} project={layout.project}")
    log.line(f"layout: {len(projects_roots)} documents root(s) protected: "
             f"{', '.join(str(path) for path in projects_roots) or '(none)'}; "
             f"self-test root {selftest or '(none)'}")
    log.line(f"layout: postgres={layout.postgres_root} workspaces={layout.workspaces_root} "
             f"transfers={layout.transfers_root}")
    log.line(f"layout: metadata={layout.metadata_db} toolbox-cache={layout.toolbox_cache_root} "
             f"(preserved)")
    log.line(f"layout: service uid={layout.service_uid} gid={layout.service_gid}")
    return layout


# --------------------------------------------------------------------------
# Refusals.  All of them run before anything is deleted.
# --------------------------------------------------------------------------


def _contains(parent: Path, child: Path) -> bool:
    """True when `parent` IS `child` or holds it, comparing lexically.

    Lexical on purpose: a resolve() on a root that is a symlink would answer
    about the target instead of the link, and the symlink case is its own
    refusal below.
    """
    try:
        child.relative_to(parent)
    except ValueError:
        return False
    return True


def root_refusals(key: str, root: Path | None, layout: Layout, log: Log) -> list[str]:
    """Every reason this one root may not have its contents deleted."""
    problems: list[str] = []
    if root is None:
        log.line(f"check: {key} is not in {layout.env_file.name}")
        return [(f"{layout.env_file.name} names no {key}; this script deletes only roots the "
                 "env file names.")]
    if not root.is_absolute():
        problems.append(f"{key}={root} is not an absolute path.")
    if str(root) in {"/", root.anchor} or root == Path(root.anchor):
        problems.append(f"{key}={root} is a filesystem root.")
    if root.is_symlink():
        problems.append(f"{key}={root} is a symlink; a reset follows no links.")
    for protected in layout.protected():
        if _contains(root, protected):
            problems.append(f"{key}={root} is, or contains, the protected path {protected}.")
    if root.exists() and not root.is_dir():
        problems.append(f"{key}={root} exists and is not a directory.")
    for line in problems:
        log.line(f"check: REFUSED {line}")
    if not problems:
        log.line(f"check: {key}={root} is a dedicated root named in the env file"
                 f"{'' if root.exists() else ' (does not exist yet; it will be created)'}")
    return problems


def pending_publication_journals(roots: tuple[Path, ...], log: Log) -> list[Path]:
    """Asset publications that got as far as the file system (section 5)."""
    pending: list[Path] = []
    for root in roots:
        if not root.is_dir():
            continue
        for journal_dir in [root / ASSET_JOURNAL_DIRNAME, *root.glob(f"*/{ASSET_JOURNAL_DIRNAME}")]:
            if not journal_dir.is_dir():
                continue
            entries = sorted(journal_dir.glob("*.json"))
            if entries:
                log.line(f"journal: {len(entries)} pending entr(ies) in {journal_dir}")
            pending.extend(entries)
    if not pending:
        log.line("journal: no pending asset publication in any registered project data root")
    return pending


def metadata_table_refusals(database: Path | None, log: Log) -> list[str]:
    """Refuse a SQLite file whose table layout this script does not know.

    Dropping named tables out of a file written by something else is how a
    reset turns into data loss, so an unrecognized layout is reported as
    unsupported BEFORE any deletion anywhere (a missing file is fine: the
    service creates it empty on the next start).
    """
    if database is None:
        return [("the env file names no COGNITA_CONFIG_ROOT, so the Workspace metadata "
                 "database cannot be located.")]
    if not database.exists():
        log.line(f"sqlite: {database} does not exist; the service will create it empty")
        return []
    try:
        connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        log.line(f"sqlite: REFUSED cannot open {database} read-only: {exc}")
        return [f"{database} cannot be opened as a SQLite database ({exc})."]
    try:
        found = {str(row[0]) for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
    except sqlite3.Error as exc:
        log.line(f"sqlite: REFUSED cannot read the table list of {database}: {exc}")
        return [f"{database} cannot be read as a SQLite database ({exc})."]
    finally:
        connection.close()
    known = set(DISPOSABLE_TABLES) | set(PRESERVED_TABLES)
    unknown = sorted(found - known)
    problems: list[str] = []
    if unknown:
        problems.append(
            f"{database} holds table(s) this build does not know: {', '.join(unknown)}. "
            "Refusing rather than dropping tables out of a file written by something else.")
    missing_preserved = sorted(set(PRESERVED_TABLES) - found)
    if missing_preserved:
        problems.append(
            f"{database} is missing {', '.join(missing_preserved)}, so it is not a Cognita "
            "Workspace metadata database.")
    for line in problems:
        log.line(f"sqlite: REFUSED {line}")
    if not problems:
        log.line(f"sqlite: {database} holds {len(found)} known table(s); "
                 f"{len(found & set(DISPOSABLE_TABLES))} of them are disposable")
    return problems


def deployment_refusals(target: release.Target, scope: str, log: Log) -> list[str]:
    """Refuse a target this script could not bring back up.

    A reset ends by starting the unit again, and the unit can only start what
    `deploy` staged: the Compose files under `<releases>/<target>/current` and,
    for a Workspace reset, the Toolbox archive whose image the broker's
    readiness probe needs.  Before the first 13.0 deploy neither exists -- main
    is still on the 12.17 layout -- so checking this UP FRONT is the difference
    between "nothing was changed, run deploy first" and an emptied database
    directory with no way to start the service that would rebuild it.
    """
    problems: list[str] = []
    current = release.current_link(target)
    if not current.exists():
        log.line(f"check: REFUSED there is no current release at {current}")
        problems.append(
            f"there is no selected release at {current}, so this script could not start "
            f"{target.unit} again afterwards. Run `python3 scripts/release.py deploy --target "
            f"{target.name}` first, then reset.")
        return problems
    log.line(f"check: current release is {current} -> {os.readlink(current) if current.is_symlink() else current}")
    if scope not in {"workspaces", "all"}:
        return problems
    mode = release.release_mode(current)
    if mode == "core":
        # A core release (DESIGN-LINUX-INSTALLER 5.2) has no Workspace runtime,
        # so there is no Toolbox archive for a reset to re-import.
        log.line("check: the selected release is core; no Toolbox archive is needed")
        return problems
    try:
        toolbox_version = selected_toolbox_version(target, log)
    except ResetError as exc:
        problems.append(str(exc))
        return problems
    archive = release.toolbox_dir(target) / f"toolbox-{toolbox_version}.tar"
    if archive.is_file():
        log.line(f"check: the staged Toolbox archive is present: {archive}")
    else:
        log.line(f"check: REFUSED the staged Toolbox archive is missing: {archive}")
        problems.append(
            f"the staged Toolbox archive is missing: {archive}. A Workspace reset empties the "
            "Microsandbox image cache, and without that archive the broker cannot pass its "
            f"readiness probe. Run `python3 scripts/release.py deploy --target {target.name}` "
            "first, then reset.")
    return problems


def selected_toolbox_version(target: release.Target, log: Log) -> str:
    """The selected release, not this checkout, owns the reload archive."""
    current = release.current_link(target)
    value = release.read_release_text(current).get("toolbox_version", "")
    if not re.fullmatch(r"[0-9]+(?:\.[0-9]+){2}", value):
        raise ResetError(
            "refused",
            f"{current}/release.txt has no valid toolbox_version; a Workspace reset cannot "
            "identify the selected release's Toolbox archive. Deploy that release again first.",
        )
    log.line(f"check: selected release Toolbox version is {value}")
    return value


def refusals(layout: Layout, scope: str, log: Log) -> list[str]:
    """Every refusal for this target and scope, gathered in one pass."""
    problems: list[str] = []
    for key, root in layout.roots_for(scope):
        problems.extend(root_refusals(key, root, layout, log))
    if scope in {"index", "all"}:
        pending = pending_publication_journals(
            registry_data_roots(layout.config_root, log), log)
        if pending:
            names = ", ".join(str(path) for path in pending[:5])
            problems.append(
                f"{len(pending)} asset publication journal entr(ies) are pending ({names}). "
                "They refer to catalog rows an index reset would erase. Start the service, let "
                "recovery finish, and run this again.")
    if scope in {"workspaces", "all"}:
        problems.extend(metadata_table_refusals(layout.metadata_db, log))
    return problems


# --------------------------------------------------------------------------
# The plan
# --------------------------------------------------------------------------


def current_version(layout: Layout, target: release.Target, log: Log) -> str:
    values = release.read_release_text(release.current_link(target))
    version = values.get("version", "")
    log.line(f"plan: current release {version or '(unknown: no release.txt under current/)'}")
    return version or "unknown"


def render_plan(layout: Layout, scope: str, version: str, target: release.Target) -> list[str]:
    lines = [
        "",
        f"RESET PLAN  target={layout.target}  scope={scope}",
        f"  version now running : {version}",
        f"  env file            : {layout.env_file}",
        f"  unit / project      : {layout.unit} / {layout.project}",
        "",
        "  Directories whose CONTENTS would be deleted (the roots themselves stay):",
    ]
    for key, root in layout.roots_for(scope):
        lines.append(f"    {key} = {root if root else '(not named in the env file)'}")
    if scope in {"workspaces", "all"}:
        lines += [
            "",
            f"  SQLite tables that would be DROPPED from {layout.metadata_db}:",
            *[f"    {name}" for name in DISPOSABLE_TABLES],
            f"  Never touched in that file: {', '.join(PRESERVED_TABLES)}",
        ]
    lines += [
        "",
        "  Protected paths checked, and left alone:",
        *[f"    {path}" for path in layout.protected()],
        f"    {layout.toolbox_cache_root}  (Toolbox image cache: a release artifact)",
        "",
    ]
    if scope in {"index", "all"}:
        lines += [
            f"  WARNING  {CATALOG_WARNING}",
            "  Documents and asset bytes on disk are untouched and are reindexed from source.",
            "",
        ]
    lines += [
        "  After the reset: "
        + ("the Toolbox archive is re-imported (before the start: the broker's readiness "
           "probe needs that image), " if scope in {"workspaces", "all"} else "")
        + f"{layout.unit} is started again, and /healthz is checked.",
        f"  Status afterwards: python3 scripts/release.py status --target {target.name}",
        "",
    ]
    return lines


def confirmation_phrase(target: str, scope: str) -> str:
    return f"RESET {target} {scope}"


def confirmed(target: str, scope: str, log: Log, *, stream=None) -> bool:
    """Read the confirmation from stdin.  Anything but an exact match cancels."""
    phrase = confirmation_phrase(target, scope)
    print(f"Type exactly  {phrase}  to proceed, or anything else to cancel: ", end="", flush=True)
    line = (stream or sys.stdin).readline()
    typed = line.rstrip("\r\n")
    if typed == phrase:
        log.line("confirm: the exact phrase was typed; proceeding")
        return True
    log.line(f"confirm: cancelled ({len(typed)} character(s) typed, not the phrase)")
    return False


# --------------------------------------------------------------------------
# The destructive part.  Plain functions over explicit paths, so the tests
# drive them against a synthetic layout with no prompt and no systemd.
# --------------------------------------------------------------------------


def _own(path: Path, uid: int | None, gid: int | None, log: Log) -> None:
    if uid is None or gid is None or not hasattr(os, "chown"):
        log.line(f"own: leaving {path} as it is (no service uid/gid, or not a POSIX host)")
        return
    try:
        os.chown(path, uid, gid)
        log.line(f"own: {path} -> {uid}:{gid}")
    except OSError as exc:
        # Not fatal by itself: say so loudly rather than swallow it, because
        # the service runs as that user and will fail to write here.
        log.line(f"own: WARNING could not chown {path} to {uid}:{gid}: {exc}")


def clear_root(root: Path, *, uid: int | None, gid: int | None, keep: tuple[Path, ...] = (),
               log: Log) -> list[str]:
    """Delete everything inside one root, then restore the root itself.

    The root's own inode is kept (a bind-mount source that disappears is a
    container that will not start), and its mode and ownership are put back
    exactly as they were found.  Entries in ``keep`` -- the Toolbox image
    cache, when the installation put it inside the Workspace root -- are
    stepped over and logged.
    """
    done: list[str] = []
    if not root.exists():
        root.mkdir(parents=True, exist_ok=True)
        root.chmod(0o700)
        _own(root, uid, gid, log)
        log.line(f"clear: {root} did not exist; created it empty")
        return [f"created {root} (it did not exist)"]
    mode = stat_module.S_IMODE(root.stat().st_mode)
    removed_dirs = removed_files = 0
    for entry in sorted(root.iterdir()):
        if any(entry == kept or _contains(entry, kept) for kept in keep if kept is not None):
            log.line(f"clear: KEEPING {entry} (protected: it is or holds the Toolbox image cache)")
            continue
        if entry.is_dir() and not entry.is_symlink():
            shutil.rmtree(entry)
            removed_dirs += 1
        else:
            entry.unlink()
            removed_files += 1
    root.chmod(mode)
    _own(root, uid, gid, log)
    log.line(f"clear: {root} emptied ({removed_dirs} director(ies), {removed_files} file(s)); "
             f"mode {oct(mode)} restored")
    done.append(f"emptied {root} ({removed_dirs} directories, {removed_files} files)")
    return done


def drop_disposable_tables(database: Path, *, log: Log) -> list[str]:
    """Drop exactly DISPOSABLE_TABLES from an OFFLINE metadata database.

    The WAL is checkpointed through SQLite (``wal_checkpoint(TRUNCATE)``) and
    the ``-wal``/``-shm`` files are never unlinked: unlinking them behind a
    SQLite that still holds the database is how a file that was merely stale
    becomes a file that is corrupt.
    """
    if not database.exists():
        log.line(f"sqlite: {database} does not exist; nothing to drop")
        return [f"{database} did not exist; the service will create it empty"]
    connection = sqlite3.connect(database)
    dropped: list[str] = []
    try:
        connection.execute("PRAGMA busy_timeout=30000")
        present = {str(row[0]) for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        for name in DISPOSABLE_TABLES:
            if name not in present:
                log.line(f"sqlite: {name} is not in {database.name}; nothing to drop")
                continue
            connection.execute(f"DROP TABLE {name}")
            dropped.append(name)
            log.line(f"sqlite: dropped {name}")
        connection.commit()
        mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
        if str(mode).lower() == "wal":
            result = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            log.line(f"sqlite: wal_checkpoint(TRUNCATE) -> {tuple(result)}")
        else:
            log.line(f"sqlite: journal_mode is {mode}; no WAL to checkpoint")
    finally:
        connection.close()
    kept = ", ".join(PRESERVED_TABLES)
    log.line(f"sqlite: {len(dropped)} table(s) dropped from {database}; {kept} untouched")
    return [(f"dropped {len(dropped)} table(s) from {database}: {', '.join(dropped) or 'none'} "
             f"({kept} untouched)")]


def write_capacity_markers(workspaces_root: Path, transfers_root: Path | None, *,
                           uid: int | None, gid: int | None, log: Log) -> list[str]:
    """Recreate the marker files the installation writer creates.

    ``compose.yaml`` bind-mounts ``<workspaces>/.cognita-12-workspaces.json``
    into the app as its capacity marker with ``create_host_path: false``, so
    without this file the service does not start at all.  The content is the
    same shape kei_http_selftest.write_config writes; the two roots share one
    ``root_id`` exactly as they do there.
    """
    root_id = str(uuid.uuid4())
    done: list[str] = []
    marker = workspaces_root / ".cognita-12-workspaces.json"
    marker.write_text(
        json.dumps({"schema": 1, "root_id": root_id, "role": "workspaces"}) + "\n",
        encoding="utf-8")
    if os.name == "posix":
        marker.chmod(0o600)
    _own(marker, uid, gid, log)
    log.line(f"marker: wrote the capacity marker {marker}")
    done.append(f"recreated {marker}")
    if transfers_root is not None:
        transfer_marker = transfers_root / ".cognita-12-transfer-root.json"
        transfer_marker.write_text(
            json.dumps({"schema": 1, "root_id": root_id, "marker_owned": True}) + "\n",
            encoding="utf-8")
        if os.name == "posix":
            transfer_marker.chmod(0o600)
        _own(transfer_marker, uid, gid, log)
        log.line(f"marker: wrote the transfer root marker {transfer_marker}")
        done.append(f"recreated {transfer_marker}")
    return done


def apply_reset(layout: Layout, scope: str, log: Log) -> list[str]:
    """Clear the roots and drop the tables.  No prompt, no systemd, no network.

    Everything it touches arrives as an explicit path on ``layout``, so a test
    runs exactly this code against a synthetic tree.  It assumes the caller has
    already taken the lock, run the refusals and stopped the unit -- the
    service must not be running while its roots are emptied.
    """
    done: list[str] = []
    keep = (layout.toolbox_cache_root,) if layout.toolbox_cache_root else ()
    # refusals() has already rejected every one of these, but this function is
    # the destructive one and is callable on its own, so it re-states the
    # requirement rather than assuming it (an `assert` would vanish under -O).
    if any(root is None for _key, root in layout.roots_for(scope)):
        raise ResetError("reset-failed",
                         f"scope {scope} has a root the env file does not name; refusing here too")
    if scope in {"workspaces", "all"} and (layout.metadata_db is None or layout.workspaces_root is None):
        raise ResetError("reset-failed",
                         "the Workspace metadata database or the Workspace root is unknown")
    for key, root in layout.roots_for(scope):
        log.line(f"apply: clearing {key}={root}")
        done.extend(clear_root(root, uid=layout.service_uid, gid=layout.service_gid,
                               keep=keep, log=log))
    if scope in {"workspaces", "all"}:
        done.extend(drop_disposable_tables(layout.metadata_db, log=log))
        done.extend(write_capacity_markers(
            layout.workspaces_root, layout.transfers_root,
            uid=layout.service_uid, gid=layout.service_gid, log=log))
    return done


# --------------------------------------------------------------------------
# Stopping and starting the unit
# --------------------------------------------------------------------------


def remaining_containers(project: str, log: Log) -> tuple[int, list[str]]:
    """Ask DOCKER what is left of a Compose project, by name and status.

    The question goes to the daemon's own project label -- the shape
    release.py's `_report_residue` uses -- and deliberately NOT to
    `docker compose ps`, which needs the Compose files of the release that is
    currently selected.  Those files are the wrong authority here: before the
    first 13.0 deploy `<releases>/<target>/current` does not exist at all,
    while the 12.17 stack is still up under the same project name.  Asking
    Compose would have answered "nothing to wait for" and the reset would have
    emptied the postgres and Workspace roots underneath running containers.

    Returns (docker's exit code, the lines it printed).  A non-zero code means
    the question could not be answered, which is never treated as "empty".
    """
    code, tail = release.run(
        ["docker", "ps", "-a", "--filter", f"label=com.docker.compose.project={project}",
         "--format", "{{.Names}} ({{.Status}})"],
        log=log, state="stop-failed", check=False, quiet=True,
    )
    return code, [line.strip() for line in tail.splitlines() if line.strip()]


def compose_down(target: release.Target, log: Log) -> None:
    """Ask Compose to take down a stack that outlived its unit's stop.

    13.1.0, measured on kei 2026-09-22: when a release's unit FAILS to start
    (the app container refuses its Workspace metadata layout, so `up --wait`
    never sees it healthy) systemd never runs the unit's ExecStop, and the
    Compose containers stay -- the app restarting, postgres and the broker
    healthy.  `systemctl stop` on that unit is then a no-op, so the wait in
    `stop_target` ran its whole budget and refused, which sent the operator
    off to run by hand the one `docker compose down` this reset -- the
    documented recovery for exactly that failed start -- exists to make
    unnecessary.  Ask Compose once, with the selected release's files; with
    nothing selected there is nothing to ask and the caller refuses as before.
    """
    current = release.current_link(target)
    files = [path for path in release.staged_compose_files(current, target.profile) if path.is_file()]
    if not files:
        log.line(f"stop: no staged Compose files under {current}; cannot ask Compose to bring "
                 f"project {target.project} down")
        return
    log.line(f"stop: containers survived the unit stop (a unit whose start failed never ran its "
             f"ExecStop); asking Compose to bring project {target.project} down")
    command = release.compose_command(project=target.project, env_file=target.env_file, files=files)
    release.run(command + ["down"], log=log, state="stop-failed", check=False)


def stop_target(target: release.Target, log: Log, *, attempts: int = 30) -> None:
    """Stop the unit and wait until Docker has no container for this project.

    `systemctl stop` returns when its ExecStop returns, but that says nothing
    about what is actually still running: a slow teardown can outlive it, and
    an ExecStop naming Compose files that are not there (the pre-13.0 state of
    a target) exits without stopping anything at all.  Emptying the Workspace
    or postgres root out from under live containers is the one thing this
    script must not do, so the DAEMON's container list is the condition, and
    anything left is a refusal rather than a warning -- after ONE
    `compose down` for the selected release (`compose_down`, 13.1.0), because
    a unit whose start failed leaves its containers up with nothing else
    ever going to remove them.
    """
    release.warn_on_live_traffic(target, log)
    release.systemctl(["stop", target.unit], log=log, state="stop-failed")
    remaining: list[str] = []
    asked_compose = False
    for attempt in range(1, attempts + 1):
        code, remaining = remaining_containers(target.project, log)
        if code == 0 and not remaining:
            log.line(f"stop: docker has no container for project {target.project} "
                     f"(attempt {attempt})")
            return
        if code != 0:
            log.line(f"stop: docker could not be asked about project {target.project} "
                     f"(exit {code}); treating that as 'still running' and waiting")
        else:
            log.line(f"stop: {len(remaining)} container(s) still in project "
                     f"{target.project}: {', '.join(remaining)}; waiting")
            if not asked_compose:
                asked_compose = True
                compose_down(target, log)
                continue
        # release.py's one wait helper, so every wait this tooling performs is
        # visible in one place.
        release._sleep(2)
    listed = "; ".join(remaining) if remaining else "docker could not be asked"
    raise ResetError(
        "stop-failed",
        f"docker still has container(s) for project {target.project} after stopping "
        f"{target.unit}: {listed}. NOTHING has been deleted. Stop them by hand "
        f"(`docker ps -a --filter label=com.docker.compose.project={target.project}`) and run "
        "this again.",
    )


def start_and_verify(target: release.Target, layout: Layout, scope: str, version: str,
                     log: Log) -> None:
    """Load the Toolbox, THEN start the unit, then check /healthz.

    The order is not arbitrary and it is the reverse of what this function did
    first.  The broker's readiness probe creates a sandbox from the Toolbox
    image, and the Compose healthcheck requires the broker to report
    ``runtime: ready``; a `workspaces` or `all` reset empties the Microsandbox
    image cache along with the Workspace root, so a broker started before the
    archive is back can never become healthy and the unit's ``up --wait``
    hangs until it fails.  Measured on kei on September 22, on the target the
    lead had reset.  ``load_toolbox`` runs in a one-off ``compose run --rm
    --no-deps`` container and needs no running stack, so loading first costs
    nothing and removes the dependency inversion.  ``release.apply_release``
    was corrected the same way, and DESIGN-13.0 section 6.5 (which said to
    load after the start) is being corrected to match.
    """
    mode = release.release_mode(release.current_link(target))
    log.line(f"start: the selected release is {mode} mode (read from its release.txt)")
    if scope in {"workspaces", "all"} and mode == "full":
        # The Microsandbox image cache lived in the root that was just wiped
        # (section 6.5), so the Toolbox archive has to be imported again before
        # the broker can pass its own readiness probe.
        toolbox_version = selected_toolbox_version(target, log)
        archive = release.toolbox_dir(target) / f"toolbox-{toolbox_version}.tar"
        if not archive.is_file():
            raise ResetError(
                "verify-failed",
                f"the staged Toolbox archive is missing: {archive}. The unit is still STOPPED, "
                "because a broker with an empty image cache cannot become healthy. Rerun "
                f"`python3 scripts/release.py deploy --target {target.name}` to stage it.")
        compose = release.compose_command(
            project=target.project, env_file=target.env_file,
            files=release.staged_compose_files(release.current_link(target), target.profile),
        )
        if layout.toolbox_cache_root is None:
            raise ResetError(
                "verify-failed",
                "the Toolbox image cache root is unknown, so the archive cannot be re-imported. "
                "The unit is still STOPPED; a broker without that image cannot become healthy.")
        release.load_toolbox(compose=compose, repo=release.REPO_ROOT,
                             cache_root=layout.toolbox_cache_root, archive=archive,
                             toolbox_version=toolbox_version, log=log)
    release.systemctl(["start", target.unit], log=log, state="verify-failed")
    if version and version != "unknown":
        release.check_healthz(target, version, log)
    else:
        payload = release.healthz(target.mcp_port, log=log)
        log.line(f"verify: /healthz reports version={payload.get('version')} "
                 f"status={payload.get('status')}")


# --------------------------------------------------------------------------
# Command
# --------------------------------------------------------------------------


def report(scope: str, target: release.Target, done: list[str], log: Log) -> None:
    log.line("")
    log.line("Done:")
    for line in done:
        log.line(f"  - {line}")
    # A user install is driven by ./cognita (C17: no script paths to type); the
    # developer targets keep the release.py command.
    # 19.6: the launcher is named by COGNITA_COMMAND in the install's env file (`cognita` on Windows).
    status = (f"{release.command_name(release.read_env_file(target.env_file))} status"
              if target.name == "local"
              else f"python3 scripts/release.py status --target {target.name}")
    if scope == "index":
        log.line(f"reset-complete: the index is EMPTY and is usable; indexing in progress. "
                 f"Watch it with: {status}")
    elif scope == "all":
        log.line(f"reset-complete: Workspace state is gone and recreated; the index is EMPTY and "
                 f"is usable; indexing in progress. Watch it with: {status}")
    else:
        log.line(f"reset-complete: Workspace state is gone and recreated. Status: {status}")


def reset(args, target: release.Target, log: Log) -> None:
    scope = args.scope
    layout = layout_for(target, log)
    version = current_version(layout, target, log)
    problems = deployment_refusals(target, scope, log) + refusals(layout, scope, log)
    for line in render_plan(layout, scope, version, target):
        log.line(line)
    if problems:
        log.line("REFUSED. Nothing has been changed:")
        for line in problems:
            log.line(f"  - {line}")
        raise ResetError("refused", f"{len(problems)} refusal(s); nothing was changed")
    if not args.apply:
        log.line("This is the plan only. Nothing has been changed. Add --apply to do it.")
        return
    if not confirmed(target.name, scope, log):
        raise ResetError(
            "cancelled",
            f"the phrase '{confirmation_phrase(target.name, scope)}' was not typed; "
            "nothing was changed")
    done: list[str] = []
    try:
        stop_target(target, log)
        done.append(f"stopped {target.unit} and waited for its containers to go")
        done.extend(apply_reset(layout, scope, log))
    except Exception as exc:
        # Partial failure: the unit stays stopped on purpose (section 6.5).  A
        # half-cleared root behind a running service is worse than an outage
        # Doug can see, and rerunning this command finishes the job.
        log.line(f"reset: FAILED after partial work: {exc}")
        log.line(f"reset: {target.unit} is left STOPPED. Done so far:")
        for line in done or ["(nothing)"]:
            log.line(f"  - {line}")
        log.line("reset: not done: everything after the line above. Rerun this command to finish.")
        if isinstance(exc, ReleaseError):
            raise
        raise ResetError("reset-failed", str(exc)) from exc
    start_and_verify(target, layout, scope, version, log)
    done.append(f"started {target.unit} and verified /healthz")
    report(scope, target, done, log)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Discard and regenerate Cognita's disposable state for one target.")
    parser.add_argument("--target", required=True, choices=release.TARGET_CHOICES)
    parser.add_argument("--scope", required=True, choices=SCOPES)
    parser.add_argument(
        "--apply", action="store_true",
        help="do it, after typing the confirmation phrase; without this the plan is printed only")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        target = release.resolve_target(args.target)
    except ReleaseError as exc:
        print(f"reset: [{exc.state}] {exc}", file=sys.stderr, flush=True)
        return EXIT_CODES.get(exc.state, 1)
    # The same guard as release.py main() (final review, 2026-09-29): once ./cognita adopted a table
    # target (kei main), this target's env names the LIVE install's data folders, and a reset here would
    # delete state under the running stack and then start the old, disabled unit on the same database.
    if release.adopted_by_local(target):
        print(f"reset: [usage] {target.name} was moved onto ./cognita, so this script no longer resets it. "
              f"Use ./cognita reset (or --target {release.LOCAL_TARGET}).", file=sys.stderr, flush=True)
        return EXIT_CODES.get("usage", 1)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    log = Log(release.logs_dir(target) / f"reset-{stamp}.log")
    log.line(f"reset_disposable_state.py target={args.target} scope={args.scope} "
             f"apply={args.apply} repo={release.REPO_ROOT} "
             f"releases={release.releases_root_for(target)}")
    try:
        # Every reset is a separate, explicit operation and owns its target
        # lock. The former deploy-only lock bypass was removed with automatic
        # Workspace reset; no release command may supply confirmation for it.
        with release.target_lock(release.target_root(target) / ".lock", log):
            reset(args, target, log)
    except ReleaseError as exc:
        log.line(f"reset: [{exc.state}] {exc}")
        log.line(f"reset: log is {log.path}")
        log.close()
        return EXIT_CODES.get(exc.state, 1)
    except KeyboardInterrupt:  # pragma: no cover - interactive only
        log.line("reset: interrupted; nothing further was changed")
        log.close()
        return EXIT_CODES["cancelled"]
    log.line(f"reset: log is {log.path}")
    log.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
