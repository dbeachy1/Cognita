"""Unit tests for scripts/reset_disposable_state.py (DESIGN-13.0 sections 5,
6.5 and the section 9 reset bullet).

Everything runs against a synthetic installation under `tmp_path` with a
sentinel in each protected place, so a test that deletes the wrong thing fails
loudly instead of passing against an empty tree.  Docker, systemd and kei are
not involved: the destructive part is a plain function over explicit paths,
which is exactly why it is one.
"""
from __future__ import annotations

import contextlib
import dataclasses
import importlib.util
import io
import json
import shutil
import sqlite3
import sys
from pathlib import Path

import pytest

from cognita.workspace import (
    DISPOSABLE_TABLES as STORE_DISPOSABLE_TABLES,
)
from cognita.workspace import (
    PRESERVED_TABLES as STORE_PRESERVED_TABLES,
)
from cognita.workspace import (
    WorkspaceMetadataStore,
)

REPO = Path(__file__).resolve().parents[1]


def _load_reset_module():
    """Load scripts/reset_disposable_state.py by path; it is a script."""
    spec = importlib.util.spec_from_file_location(
        "cognita_reset_tool", REPO / "scripts" / "reset_disposable_state.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    # @dataclass resolves annotations through sys.modules, so the module has to
    # be registered before it is executed.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


reset = _load_reset_module()
release = reset.release


@dataclasses.dataclass
class _Args:
    """What argparse hands `reset()`."""

    target: str
    scope: str
    apply: bool


# --------------------------------------------------------------------------
# A synthetic installation, with a sentinel in every protected place
# --------------------------------------------------------------------------

SENTINELS = {
    "config/cognita.yaml": "admin_username: operator\n",
    "config/authentication.yaml": "version: 1\n",
    "config/data/Alpha/deindexed.json": '{"paths": ["kept.md"]}\n',
    "projects/Alpha/original-document.md": "# An original the user wrote\n",
    "secrets/postgres.password": "not-a-real-password\n",
    "models/model.onnx": "weights\n",
}
JUNK = {
    "postgres/PG_VERSION": "18\n",
    "postgres/18/docker/base/1/2345": "pages\n",
    "workspaces/namespaces/default/sandbox-a/rootfs.img": "guest disk\n",
    "workspaces/broker-state.sqlite3": "broker scratch\n",
    "workspaces/toolbox-cache/toolbox-12.6.0.tar": "the Toolbox archive\n",
    "transfers/cognita-transfer-abc/payload.bin": "in-flight transfer\n",
}


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def snapshot(root: Path) -> dict[str, str]:
    """Every file under `root`, by relative path, with its content.

    SQLite's own ``-wal`` and ``-shm`` sidecars are left out: opening a WAL
    database read-only -- which the refusal check does, to read its table list
    -- creates the shared-memory file, and that is SQLite housekeeping rather
    than a change to anything this script owns.  The database itself, its
    tables and the settings row are all compared for real.
    """
    return {
        str(path.relative_to(root)).replace("\\", "/"):
            path.read_bytes().decode("utf-8", "replace")
        for path in sorted(root.rglob("*"))
        if path.is_file() and not path.name.endswith(("-wal", "-shm"))
    }


def settings_row(database: Path) -> tuple:
    connection = sqlite3.connect(database)
    try:
        return connection.execute(
            "SELECT retention_days, network_mode FROM workspace_settings").fetchone()
    finally:
        connection.close()


def tables_in(database: Path) -> set[str]:
    connection = sqlite3.connect(database)
    try:
        return {str(row[0]) for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
    finally:
        connection.close()


@pytest.fixture
def installation(tmp_path: Path) -> Path:
    """A complete synthetic target: sentinels, junk, env file, metadata DB."""
    root = tmp_path / "data"
    for name, text in SENTINELS.items():
        _write(root / name, text)
    for name, text in JUNK.items():
        _write(root / name, text)
    _write(root / "config" / "registry.yaml",
           "version: 1\nprojects:\n  - name: Alpha\n"
           f"    documents_dir: {root / 'projects' / 'Alpha'}\n"
           "    data_dir: /app/config/data/Alpha\n    enabled: true\n")
    # The real DDL, written by the real store.
    store = WorkspaceMetadataStore(root / "config" / "data" / "workspace-metadata.sqlite3")
    with store.transaction() as db:
        db.execute(
            "INSERT INTO workspaces(workspace_id,principal_id,display_label,state,desired_state,"
            "created_at,last_accessed_at,quota_bytes,runtime_name) "
            "VALUES('w-1','p-1','Scratch','stopped','stopped','now','now',1,'runtime-1')")
        db.execute("UPDATE workspace_settings SET retention_days=17, network_mode='allowlist'")
    store.close()
    _write(root / "cognita-test.env", "\n".join([
        f"COGNITA_CONFIG_ROOT={root / 'config'}",
        f"COGNITA_PROJECTS_ROOT={root / 'projects'}",
        f"COGNITA_SECRETS_ROOT={root / 'secrets'}",
        f"COGNITA_MODEL_CACHE_ROOT={root / 'models'}",
        f"COGNITA_POSTGRES_DATA_ROOT={root / 'postgres'}",
        f"COGNITA_WORKSPACE_DATA_ROOT={root / 'workspaces'}",
        f"COGNITA_TRANSFER_STAGING_ROOT={root / 'transfers'}",
        "COGNITA_SERVICE_UID=1000",
        "COGNITA_SERVICE_GID=1000",
        "COGNITA_RELEASE_TARGET=test",
    ]) + "\n")
    return root


@pytest.fixture
def target(installation: Path, tmp_path, monkeypatch):
    """A deployed target: env file, a selected release, and a staged Toolbox.

    Both of the last two are what `deploy` leaves behind, and a reset refuses
    without them -- it ends by starting the unit again and can only start what
    was staged -- so the ordinary fixture has them and the tests that prove
    those refusals take them away.
    """
    monkeypatch.setattr(release, "RELEASES_ROOT", tmp_path / "releases")
    target = dataclasses.replace(release.TARGETS["test"],
                                 env_file=installation / "cognita-test.env")
    current = release.current_link(target)
    current.mkdir(parents=True)
    (current / "compose.yaml").write_text("services: {}\n", encoding="utf-8")
    (current / "release.txt").write_text(
        "version: 13.0.0\ntarget: test\ntoolbox_version: 12.6.0\n", encoding="utf-8")
    archive = (release.toolbox_dir(target)
               / f"toolbox-{release.read_toolbox_version(release.REPO_ROOT, release.Log(None))}.tar")
    archive.parent.mkdir(parents=True, exist_ok=True)
    archive.write_text("not really a tar, and never opened here\n", encoding="utf-8")
    return target


@pytest.fixture
def log(tmp_path):
    return release.Log(tmp_path / "reset-test.log")


def layout_of(target, log):
    return reset.layout_for(target, log)


def assert_protected_intact(root: Path) -> None:
    for name, text in SENTINELS.items():
        assert (root / name).read_text(encoding="utf-8") == text, f"{name} was touched"
    assert settings_row(root / "config" / "data" / "workspace-metadata.sqlite3") == (17, "allowlist")


# --------------------------------------------------------------------------
# The table lists have to agree with the store they drop tables out of
# --------------------------------------------------------------------------


def test_a_target_the_installer_adopted_is_refused_before_anything_happens(
        installation, target, tmp_path, monkeypatch, capsys):
    """kei main after P10b: its env names the LIVE ./cognita install's folders.  A reset would delete state
    under the running stack and then start the old, disabled unit on the same database (final review)."""
    monkeypatch.setitem(release.TARGETS, "test", target)
    local_env = tmp_path / "install.env"
    local_env.write_text(f"COGNITA_CONFIG_ROOT={installation / 'config'}\n", encoding="utf-8")
    monkeypatch.setenv("COGNITA_LOCAL_ENV_FILE", str(local_env))
    assert reset.main(["--target", "test", "--scope", "index", "--apply"]) == reset.EXIT_CODES["usage"]
    assert "moved onto ./cognita" in capsys.readouterr().err
    assert not (release.logs_dir(target)).exists() or not any(release.logs_dir(target).glob("reset-*.log"))
    assert_protected_intact(installation)


def test_the_table_lists_match_the_workspace_metadata_store():
    """The script repeats the store's table names because kei's host Python
    cannot import the package.  This is the guard on that copy."""
    assert reset.DISPOSABLE_TABLES == STORE_DISPOSABLE_TABLES
    assert reset.PRESERVED_TABLES == STORE_PRESERVED_TABLES
    assert not set(reset.DISPOSABLE_TABLES) & set(reset.PRESERVED_TABLES)


def test_every_named_table_actually_exists_in_a_fresh_store(tmp_path):
    store = WorkspaceMetadataStore(tmp_path / "meta.sqlite3")
    store.close()
    assert (set(reset.DISPOSABLE_TABLES) | set(reset.PRESERVED_TABLES)
            == tables_in(tmp_path / "meta.sqlite3"))


# --------------------------------------------------------------------------
# The plan
# --------------------------------------------------------------------------


def test_plan_is_accurate_and_changes_nothing(installation, target, log, capsys):
    before = snapshot(installation)
    reset.reset(_Args(target="test", scope="all", apply=False), target, log)
    printed = capsys.readouterr().out

    for root in ("postgres", "workspaces", "transfers"):
        assert str(installation / root) in printed
    for name in reset.DISPOSABLE_TABLES:
        assert name in printed
    assert "workspace_settings" in printed
    # The protected paths it checked are named, and so is the loss warning.
    assert str(installation / "projects") in printed and str(installation / "secrets") in printed
    assert str(installation / "config") in printed
    assert "metadata_storage" in printed
    assert "Add --apply" in printed
    assert snapshot(installation) == before


# --------------------------------------------------------------------------
# What each scope actually clears
# --------------------------------------------------------------------------


def test_index_scope_clears_only_the_postgres_root(installation, target, log):
    reset.apply_reset(layout_of(target, log), "index", log)

    assert (installation / "postgres").is_dir()
    assert list((installation / "postgres").iterdir()) == []
    assert_protected_intact(installation)
    # Workspace state is not this scope's business.
    assert (installation / "workspaces" / "broker-state.sqlite3").is_file()
    assert (installation / "transfers" / "cognita-transfer-abc" / "payload.bin").is_file()
    assert "workspaces" in tables_in(installation / "config" / "data" / "workspace-metadata.sqlite3")


def test_workspaces_scope_clears_both_roots_and_drops_only_the_disposable_tables(
    installation, target, log,
):
    database = installation / "config" / "data" / "workspace-metadata.sqlite3"
    reset.apply_reset(layout_of(target, log), "workspaces", log)

    # The guests, the broker's own SQLite and the staged transfers are gone.
    assert not (installation / "workspaces" / "namespaces").exists()
    assert not (installation / "workspaces" / "broker-state.sqlite3").exists()
    assert not (installation / "transfers" / "cognita-transfer-abc").exists()
    # The capacity marker compose.yaml bind-mounts is back, and readable.
    marker = json.loads((installation / "workspaces" / ".cognita-12-workspaces.json").read_text())
    assert marker["role"] == "workspaces" and marker["root_id"]
    transfer_marker = json.loads(
        (installation / "transfers" / ".cognita-12-transfer-root.json").read_text())
    assert transfer_marker["root_id"] == marker["root_id"]
    # Exactly the disposable tables were dropped; the settings row survives.
    assert tables_in(database) == set(reset.PRESERVED_TABLES)
    assert settings_row(database) == (17, "allowlist")
    assert_protected_intact(installation)
    # The index is not this scope's business.
    assert (installation / "postgres" / "PG_VERSION").is_file()


def test_the_toolbox_image_cache_inside_the_workspace_root_survives(installation, target, log):
    """release.py's fallback puts the cache inside the Workspace root, and
    section 5 says the cache is a release artifact that a reset preserves."""
    layout = layout_of(target, log)
    assert layout.toolbox_cache_root == installation / "workspaces" / "toolbox-cache"
    reset.apply_reset(layout, "workspaces", log)
    assert (installation / "workspaces" / "toolbox-cache" / "toolbox-12.6.0.tar").read_text(
        encoding="utf-8") == "the Toolbox archive\n"


def test_all_scope_does_both(installation, target, log):
    database = installation / "config" / "data" / "workspace-metadata.sqlite3"
    reset.apply_reset(layout_of(target, log), "all", log)

    assert list((installation / "postgres").iterdir()) == []
    assert not (installation / "workspaces" / "namespaces").exists()
    assert tables_in(database) == set(reset.PRESERVED_TABLES)
    assert_protected_intact(installation)


def test_a_reset_store_reopens_clean_and_keeps_its_settings(installation, target, log):
    """The point of dropping tables rather than deleting the file: the service
    recreates what it needs and the installation's policy is still there."""
    database = installation / "config" / "data" / "workspace-metadata.sqlite3"
    reset.apply_reset(layout_of(target, log), "workspaces", log)

    store = WorkspaceMetadataStore(database)
    try:
        with store.transaction() as db:
            assert db.execute("SELECT COUNT(*) FROM workspaces").fetchone()[0] == 0
            assert db.execute(
                "SELECT retention_days FROM workspace_settings").fetchone()[0] == 17
    finally:
        store.close()
    assert tables_in(database) == set(reset.DISPOSABLE_TABLES) | set(reset.PRESERVED_TABLES)


# --------------------------------------------------------------------------
# Refusals.  Every one of them must leave BOTH stores exactly as they were.
# --------------------------------------------------------------------------


def _repoint(env_file: Path, key: str, value: str) -> None:
    lines = [line for line in env_file.read_text(encoding="utf-8").splitlines()
             if not line.startswith(f"{key}=")]
    env_file.write_text("\n".join([*lines, f"{key}={value}"]) + "\n", encoding="utf-8")


def _refuse(installation, target, log, monkeypatch, scope: str, *, readable_db: bool = True) -> str:
    """Drive the real command past confirmation and assert it refused.

    ``readable_db`` is False only for the case that deliberately corrupts the
    metadata file: there is no settings row to compare, and the point of the
    test is that the refusal happened before anything was deleted.
    """
    database = installation / "config" / "data" / "workspace-metadata.sqlite3"
    before = snapshot(installation)
    before_tables = tables_in(database) if readable_db else None
    monkeypatch.setattr(reset, "confirmed", lambda *a, **k: True)

    def must_not_stop(*_args, **_kwargs):
        raise AssertionError("the unit was stopped although the reset should have refused")

    monkeypatch.setattr(reset, "stop_target", must_not_stop)
    with pytest.raises(reset.ReleaseError) as failure:
        reset.reset(_Args(target="test", scope=scope, apply=True), target, log)
    assert failure.value.state == "refused"
    assert snapshot(installation) == before
    if readable_db:
        assert tables_in(database) == before_tables
        assert_protected_intact(installation)
    else:
        for name, text in SENTINELS.items():
            assert (installation / name).read_text(encoding="utf-8") == text, f"{name} was touched"
    return log.path.read_text(encoding="utf-8")


def test_refuses_a_filesystem_root(installation, target, log, monkeypatch):
    _repoint(target.env_file, "COGNITA_POSTGRES_DATA_ROOT", Path(installation.anchor).as_posix())
    assert "filesystem root" in _refuse(installation, target, log, monkeypatch, "index")


def test_refuses_a_symlinked_root(installation, target, log, monkeypatch):
    link = installation / "postgres-link"
    try:
        link.symlink_to(installation / "postgres", target_is_directory=True)
    except (OSError, NotImplementedError) as exc:  # pragma: no cover - host privilege
        pytest.skip(f"this host cannot create a directory symlink: {exc}")
    _repoint(target.env_file, "COGNITA_POSTGRES_DATA_ROOT", str(link))
    assert "symlink" in _refuse(installation, target, log, monkeypatch, "index")


def test_refuses_a_symlinked_root_without_needing_the_privilege_to_make_one(
    installation, target, log, monkeypatch,
):
    """The same refusal as above, provable on a host that cannot create a
    directory symlink (an unprivileged Windows account).  It patches the one
    question the check asks, so the check itself is what is under test."""
    postgres = installation / "postgres"
    monkeypatch.setattr(Path, "is_symlink", lambda self: self == postgres)
    assert "symlink" in _refuse(installation, target, log, monkeypatch, "index")


def test_refuses_a_root_that_holds_a_protected_path(installation, target, log, monkeypatch):
    # The whole data root holds config/, projects/, secrets/ and models/.
    _repoint(target.env_file, "COGNITA_POSTGRES_DATA_ROOT", str(installation))
    assert "protected path" in _refuse(installation, target, log, monkeypatch, "index")


def test_refuses_a_root_the_env_file_does_not_name(installation, target, log, monkeypatch):
    _repoint(target.env_file, "COGNITA_WORKSPACE_DATA_ROOT", "")
    assert "names no COGNITA_WORKSPACE_DATA_ROOT" in _refuse(
        installation, target, log, monkeypatch, "workspaces")


def test_refuses_an_index_reset_while_a_publication_journal_is_pending(
    installation, target, log, monkeypatch,
):
    _write(installation / "config" / "data" / "Alpha" / reset.ASSET_JOURNAL_DIRNAME / "op-7.json",
           '{"phase": "published", "filepath": "figure.png"}\n')
    printed = _refuse(installation, target, log, monkeypatch, "index")
    assert "publication journal" in printed and "op-7.json" in printed


def test_a_pending_journal_does_not_block_a_workspaces_reset(installation, target, log):
    """The journal refers to catalog rows, which `workspaces` never touches."""
    _write(installation / "config" / "data" / "Alpha" / reset.ASSET_JOURNAL_DIRNAME / "op-7.json",
           '{"phase": "published"}\n')
    assert reset.refusals(layout_of(target, log), "workspaces", log) == []


def test_refuses_an_unknown_sqlite_layout(installation, target, log, monkeypatch):
    database = installation / "config" / "data" / "workspace-metadata.sqlite3"
    connection = sqlite3.connect(database)
    try:
        connection.execute("CREATE TABLE somebody_elses_table (id TEXT)")
        connection.commit()
    finally:
        connection.close()
    assert "somebody_elses_table" in _refuse(installation, target, log, monkeypatch, "workspaces")


def test_refuses_a_metadata_file_that_is_not_a_database(installation, target, log, monkeypatch):
    database = installation / "config" / "data" / "workspace-metadata.sqlite3"
    for suffix in ("-wal", "-shm"):
        database.with_name(database.name + suffix).unlink(missing_ok=True)
    database.write_text("this is not a SQLite file\n", encoding="utf-8")
    assert "SQLite" in _refuse(installation, target, log, monkeypatch, "workspaces",
                               readable_db=False)


def test_refuses_when_no_release_is_selected(installation, target, log, monkeypatch):
    """Main's state before the first 13.0 deploy: no `current` directory.

    The reset ends by starting the unit again and can only start what deploy
    staged, so this has to be a refusal UP FRONT -- the alternative is an
    emptied database directory with no way to start the service that would
    rebuild it.
    """
    current = release.current_link(target)
    (current / "compose.yaml").unlink()
    (current / "release.txt").unlink()
    current.rmdir()
    printed = _refuse(installation, target, log, monkeypatch, "index")
    assert "no selected release" in printed
    assert "release.py deploy --target test" in printed


def test_refuses_a_workspace_reset_without_the_staged_toolbox_archive(
    installation, target, log, monkeypatch,
):
    staged_archive(target, log).unlink()
    printed = _refuse(installation, target, log, monkeypatch, "workspaces")
    assert "staged Toolbox archive is missing" in printed
    assert "release.py deploy --target test" in printed


@pytest.mark.parametrize("metadata", ["version: 13.0.0\n", "toolbox_version: ../outside\n"])
def test_workspace_reset_refuses_invalid_selected_toolbox_before_stop(
    installation, target, log, monkeypatch, metadata,
):
    (release.current_link(target) / "release.txt").write_text(metadata, encoding="utf-8")
    printed = _refuse(installation, target, log, monkeypatch, "workspaces")
    assert "no valid toolbox_version" in printed


def test_reset_uses_selected_toolbox_when_checkout_differs(
    installation, target, log, monkeypatch,
):
    selected = "12.5.9"
    (release.current_link(target) / "release.txt").write_text(
        f"version: 13.0.0\ntoolbox_version: {selected}\n", encoding="utf-8")
    archive = release.toolbox_dir(target) / f"toolbox-{selected}.tar"
    archive.write_text("synthetic archive\n", encoding="utf-8")
    monkeypatch.setattr(release, "read_toolbox_version", lambda *a, **k:
                        pytest.fail("reset consulted the checkout Toolbox version"))
    assert reset.deployment_refusals(target, "workspaces", log) == []
    loaded = []
    monkeypatch.setattr(release, "load_toolbox", lambda **kwargs: loaded.append(kwargs))
    monkeypatch.setattr(release, "systemctl", lambda *a, **k: None)
    monkeypatch.setattr(release, "healthz", lambda *a, **k: {})
    reset.start_and_verify(target, layout_of(target, log), "workspaces", "unknown", log)
    assert len(loaded) == 1
    assert loaded[0]["toolbox_version"] == selected
    assert loaded[0]["archive"] == archive


def test_an_index_reset_does_not_need_the_toolbox_archive(installation, target, log):
    """`index` never empties the Microsandbox cache, so it does not ask."""
    staged_archive(target, log).unlink()
    assert reset.deployment_refusals(target, "index", log) == []


def _docker_ps_stub(monkeypatch, lines: list[str], code: int = 0) -> list[list[str]]:
    """Answer every child process this stop path runs, recording the commands."""
    commands: list[list[str]] = []

    def fake_run(command, *, log, state, check=True, quiet=False, **kwargs):
        commands.append(command)
        return code, "\n".join(lines)

    monkeypatch.setattr(release, "run", fake_run)
    monkeypatch.setattr(release, "systemctl", lambda args, **kwargs: (0, ""))
    monkeypatch.setattr(release, "warn_on_live_traffic", lambda *a, **k: None)
    monkeypatch.setattr(release, "_sleep", lambda _seconds: None)
    return commands


def test_stop_asks_docker_for_the_project_not_the_compose_files(target, log, monkeypatch):
    """The container check must not depend on `current`, because the release
    that is still running may not be the one `current` describes -- or there
    may be no `current` at all while the old stack is up."""
    commands = _docker_ps_stub(monkeypatch, [])
    reset.stop_target(target, log, attempts=2)
    assert len(commands) == 1
    assert commands[0][:3] == ["docker", "ps", "-a"]
    assert f"label=com.docker.compose.project={target.project}" in commands[0]
    assert "compose" not in commands[0]


def test_a_container_that_outlives_the_stop_refuses_and_names_it(
    installation, target, log, monkeypatch,
):
    """Containers still listed after the stop: refuse, and delete nothing."""
    before = snapshot(installation)
    _docker_ps_stub(monkeypatch, ["cognita-13test-postgres-1 (Up 3 hours)",
                                  "cognita-13test-cognita-1 (Up 3 hours)"])
    monkeypatch.setattr(reset, "confirmed", lambda *a, **k: True)
    started: list[str] = []
    monkeypatch.setattr(reset, "start_and_verify", lambda *a, **k: started.append("started"))
    # The real stop_target, with a short wait: the lambda must hold the
    # ORIGINAL function, or replacing the name makes it call itself.
    real_stop = reset.stop_target
    monkeypatch.setattr(reset, "stop_target",
                        lambda tgt, lg, **kwargs: real_stop(tgt, lg, attempts=2))

    with pytest.raises(reset.ReleaseError) as failure:
        reset.reset(_Args(target="test", scope="all", apply=True), target, log)

    assert failure.value.state == "stop-failed"
    assert "cognita-13test-postgres-1" in str(failure.value)
    assert "NOTHING has been deleted" in str(failure.value)
    assert started == []
    assert snapshot(installation) == before
    assert_protected_intact(installation)
    # Both roots are untouched, contents and all.
    assert (installation / "postgres" / "PG_VERSION").is_file()
    assert (installation / "workspaces" / "broker-state.sqlite3").is_file()


def test_containers_left_by_a_failed_unit_start_are_brought_down_once(target, log, monkeypatch):
    """13.1.0: a unit whose start failed never ran its ExecStop, so its
    containers outlive `systemctl stop`; one `compose down` for the selected
    release clears them and the reset proceeds instead of refusing."""
    listed = ["cognita-13test-cognita-1 (Restarting (1) 26 seconds ago)",
              "cognita-13test-postgres-1 (Up About a minute (healthy))"]
    commands: list[list[str]] = []

    def fake_run(command, *, log, state, check=True, quiet=False, **kwargs):
        commands.append(command)
        if command[:2] == ["docker", "ps"]:
            return 0, "\n".join(listed)
        if command[:2] == ["docker", "compose"] and command[-1] == "down":
            listed.clear()
            return 0, ""
        raise AssertionError(f"unexpected command {command}")

    monkeypatch.setattr(release, "run", fake_run)
    monkeypatch.setattr(release, "systemctl", lambda args, **kwargs: (0, ""))
    monkeypatch.setattr(release, "warn_on_live_traffic", lambda *a, **k: None)
    monkeypatch.setattr(release, "_sleep", lambda _seconds: None)

    reset.stop_target(target, log, attempts=3)

    downs = [command for command in commands if command[:2] == ["docker", "compose"]]
    assert len(downs) == 1
    assert downs[0][-1] == "down"
    assert downs[0][downs[0].index("-p") + 1] == target.project
    assert str(release.current_link(target) / "compose.yaml") in downs[0]
    assert str(target.env_file) in downs[0]
    # ps, down, ps -- the second ps is what proves the stack is gone.
    assert [command[:2] for command in commands] == [["docker", "ps"], ["docker", "compose"], ["docker", "ps"]]


def test_deploy_lock_bypass_flag_is_rejected():
    """No caller can opt out of the manual reset's target lock."""
    with pytest.raises(SystemExit) as failure:
        reset.build_parser().parse_args([
            "--target", "test", "--scope", "workspaces", "--apply", "--called-by-deploy",
        ])
    assert failure.value.code == 2


def test_manual_reset_takes_the_target_lock(target, log, monkeypatch):
    taken: list[Path] = []

    @contextlib.contextmanager
    def fake_lock(path, log):
        taken.append(path)
        yield

    monkeypatch.setattr(release, "target_lock", fake_lock)
    monkeypatch.setattr(reset, "reset", lambda args, tgt, lg: None)
    assert reset.main(["--target", "test", "--scope", "workspaces", "--apply"]) == 0
    assert taken == [release.target_root(target) / ".lock"]


def test_without_a_selected_release_the_stop_still_refuses(target, log, monkeypatch):
    """No `current` means no Compose files to ask with: the old refusal stands
    and nothing but `docker ps` runs."""
    shutil.rmtree(release.current_link(target))
    commands = _docker_ps_stub(monkeypatch, ["cognita-13test-cognita-1 (Restarting (1) 5 seconds ago)"])
    with pytest.raises(reset.ReleaseError) as failure:
        reset.stop_target(target, log, attempts=2)
    assert failure.value.state == "stop-failed"
    assert all(command[:2] == ["docker", "ps"] for command in commands)


def test_docker_that_cannot_be_asked_is_never_read_as_empty(target, log, monkeypatch):
    _docker_ps_stub(monkeypatch, ["Cannot connect to the Docker daemon"], code=1)
    with pytest.raises(reset.ReleaseError) as failure:
        reset.stop_target(target, log, attempts=2)
    assert failure.value.state == "stop-failed"


def test_a_stop_failure_leaves_both_stores_intact(installation, target, log, monkeypatch):
    """The unit would not go down, so nothing may be deleted (section 6.5)."""
    before = snapshot(installation)
    monkeypatch.setattr(reset, "confirmed", lambda *a, **k: True)

    def refuses_to_stop(_target, _log, **_kwargs):
        raise reset.ResetError("stop-failed", "containers are still present")

    monkeypatch.setattr(reset, "stop_target", refuses_to_stop)
    started: list[str] = []
    monkeypatch.setattr(reset, "start_and_verify", lambda *a, **k: started.append("started"))
    with pytest.raises(reset.ReleaseError) as failure:
        reset.reset(_Args(target="test", scope="all", apply=True), target, log)

    assert failure.value.state == "stop-failed"
    assert started == [], "the unit must be left stopped after a partial failure"
    assert snapshot(installation) == before
    assert_protected_intact(installation)


# --------------------------------------------------------------------------
# Coming back up: the Toolbox archive is imported BEFORE the unit starts
# --------------------------------------------------------------------------


def staged_archive(target, log) -> Path:
    """The archive `release.py deploy` leaves under <releases>/<target>/toolbox."""
    version = reset.selected_toolbox_version(target, log)
    return release.toolbox_dir(target) / f"toolbox-{version}.tar"


def _record_startup(monkeypatch) -> list[str]:
    """Replace the three things start_and_verify does with a command log."""
    order: list[str] = []
    monkeypatch.setattr(release, "load_toolbox", lambda **kwargs: order.append("load"))
    monkeypatch.setattr(release, "systemctl",
                        lambda args, **kwargs: order.append(" ".join(args[:1]) or "systemctl"))
    monkeypatch.setattr(release, "healthz", lambda port, **kwargs: order.append("healthz") or {})
    monkeypatch.setattr(release, "check_healthz",
                        lambda *a, **k: order.append("healthz") or {})
    return order


def test_the_toolbox_is_loaded_before_the_unit_starts(
    installation, target, log, monkeypatch,
):
    """Measured on kei, September 22: the broker's readiness probe creates a
    sandbox from the Toolbox image and the Compose healthcheck now requires
    `runtime: ready`.  A reset empties the Microsandbox image cache, so a
    broker started before the archive is back can never become healthy and
    `up --wait` hangs until it fails.  The load runs in a one-off
    `compose run --rm --no-deps` container and needs no running stack."""
    order = _record_startup(monkeypatch)
    reset.start_and_verify(target, layout_of(target, log), "workspaces", "unknown", log)
    assert order == ["load", "start", "healthz"]


def test_an_index_reset_does_not_touch_the_toolbox(installation, target, log, monkeypatch):
    """`index` leaves the Workspace root alone, so the image cache is intact."""
    order = _record_startup(monkeypatch)
    reset.start_and_verify(target, layout_of(target, log), "index", "unknown", log)
    assert order == ["start", "healthz"]


def test_a_missing_toolbox_archive_leaves_the_unit_stopped(
    installation, target, log, monkeypatch,
):
    """The backstop for the up-front refusal below: even called directly,
    start_and_verify starts nothing it cannot make healthy."""
    staged_archive(target, log).unlink()
    order = _record_startup(monkeypatch)
    with pytest.raises(reset.ReleaseError) as failure:
        reset.start_and_verify(target, layout_of(target, log), "all", "unknown", log)
    assert failure.value.state == "verify-failed"
    assert "STOPPED" in str(failure.value)
    assert order == [], "nothing may start when the broker's image cannot be restored"


# --------------------------------------------------------------------------
# The confirmation phrase
# --------------------------------------------------------------------------


@pytest.mark.parametrize("typed", [
    "", "yes", "RESET test", "reset test index", "RESET main index", "RESET test all",
    " RESET test index", "RESET test index ", "RESET  test index",
])
def test_anything_but_the_exact_phrase_cancels(typed, log):
    assert not reset.confirmed("test", "index", log, stream=io.StringIO(typed + "\n"))


def test_the_exact_phrase_proceeds(log):
    assert reset.confirmed("test", "index", log, stream=io.StringIO("RESET test index\n"))
    assert reset.confirmation_phrase("main", "workspaces") == "RESET main workspaces"


def test_a_cancelled_confirmation_changes_nothing(installation, target, log, monkeypatch):
    before = snapshot(installation)
    monkeypatch.setattr(reset, "confirmed", lambda *a, **k: False)
    monkeypatch.setattr(reset, "stop_target",
                        lambda *a, **k: pytest.fail("the unit was stopped after a cancellation"))
    with pytest.raises(reset.ReleaseError) as failure:
        reset.reset(_Args(target="test", scope="all", apply=True), target, log)

    assert failure.value.state == "cancelled"
    assert snapshot(installation) == before


# --------------------------------------------------------------------------
# Odds and ends the plan depends on
# --------------------------------------------------------------------------


def test_registry_data_roots_maps_the_container_path_back_to_the_host(installation, log):
    roots = reset.registry_data_roots(installation / "config", log)
    assert installation / "config" / "data" / "Alpha" in roots


def test_exit_codes_cover_every_state_the_script_raises():
    for state in ("refused", "cancelled", "stop-failed", "reset-failed", "verify-failed"):
        assert state in reset.EXIT_CODES
    assert reset.EXIT_CODES["reset-complete"] == 0
