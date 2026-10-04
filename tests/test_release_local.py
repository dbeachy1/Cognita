"""Tests for the Linux installer's release.py surface (DESIGN-LINUX-INSTALLER.md
section 5): the `local` target, mode read from the release, the folders fragment,
staging from published images, `publish`, unit rendering, and the lock-free
functions the `cognita` CLI calls.

Nothing here touches the network, Docker, systemd or a real clone: Docker and git
go through the module's own seams (`run`, `image_exists`, `image_identity`,
`git_show_file`, `_capture`) and are faked.  There is no sleep and no wall-clock
wait anywhere.
"""
from __future__ import annotations

import ast
import contextlib
import dataclasses
import importlib.util
import json
import os
import string
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO = Path(__file__).resolve().parents[1]


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, REPO / "scripts" / filename)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module  # @dataclass resolves annotations through sys.modules
    spec.loader.exec_module(module)
    return module


release = _load("cognita_release_local_tests", "release.py")

COMMIT = "a" * 40
VERSION = "14.1.0"
TOOLBOX_VERSION = "12.6.0"
ROOTS = ["/home/u/Documents"]


def _digest(name: str, char: str) -> str:
    return f"ghcr.io/o/{name}@sha256:{char * 64}"


PUBLISHED = {
    "version": VERSION,
    "commit": COMMIT,
    "published_at": "2026-09-28T10:00:00-07:00",
    "image_ref_cognita_cpu": _digest("cognita-app", "1"),
    "image_ref_cognita_amd": _digest("cognita-app", "2"),
    "image_ref_workspace_runtime": _digest("cognita-workspace-runtime", "3"),
    "image_ref_toolbox": _digest("cognita-workspace-toolbox", "4"),
    "toolbox_version": TOOLBOX_VERSION,
    "size_cognita_cpu": "100",
    "size_cognita_amd": "200",
    "size_workspace_runtime": "300",
    "size_toolbox": "400",
}


@pytest.fixture
def log(tmp_path):
    return release.Log(tmp_path / "test.log")


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    """No test reads the developer's real env file or leaks COGNITA_VERSION."""
    monkeypatch.setenv("COGNITA_LOCAL_ENV_FILE", str(tmp_path / "absent" / "cognita.env"))
    monkeypatch.setenv("COGNITA_VERSION", "unset")
    monkeypatch.setattr(release, "RELEASES_ROOT", tmp_path / "table-releases")


def _write_env(path: Path, **overrides: str) -> None:
    values = {
        "COGNITA_ACCELERATION": "cpu",
        "COGNITA_WORKSPACE": "on",
        "COGNITA_MCP_HOST_PORT": "8675",
        "COGNITA_ADMIN_HOST_PORT": "8676",
        "COGNITA_PROJECTS_ROOT": ROOTS[0],
        "COGNITA_RELEASE_TARGET": "local",
    }
    values.update(overrides)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(f"{key}={value}\n" for key, value in values.items() if value is not None),
                    encoding="utf-8")


@pytest.fixture
def local(tmp_path, monkeypatch):
    """A `local` target whose env file and releases root live under tmp_path."""
    env_file = tmp_path / "config" / "cognita.env"
    releases = tmp_path / "local-releases"
    _write_env(env_file, COGNITA_RELEASES_ROOT=str(releases))
    monkeypatch.setenv("COGNITA_LOCAL_ENV_FILE", str(env_file))
    return release.resolve_target("local")


def _adopted_main(tmp_path, monkeypatch):
    """A table target `main` whose config folder the local install's env file also names (kei, P10b)."""
    main_env = tmp_path / "main.env"
    _write_env(main_env, COGNITA_CONFIG_ROOT=str(tmp_path / "data" / "config"))
    target = dataclasses.replace(release.TARGETS["main"], env_file=main_env)
    monkeypatch.setitem(release.TARGETS, "main", target)
    local_env = tmp_path / "install.env"
    _write_env(local_env, COGNITA_CONFIG_ROOT=str(tmp_path / "data" / "config"))
    monkeypatch.setenv("COGNITA_LOCAL_ENV_FILE", str(local_env))
    return target, local_env


def test_release_py_refuses_to_start_a_table_target_the_installer_adopted(tmp_path, monkeypatch, capsys):
    target, _ = _adopted_main(tmp_path, monkeypatch)
    assert release.adopted_by_local(target) is True
    for command in (["deploy", "--target", "main"], ["qa", "--target", "main"], ["install-unit", "--target", "main"]):
        assert release.main(command) == release.EXIT_CODES["usage"]
        assert "moved onto ./cognita" in capsys.readouterr().err
    assert not (tmp_path / "table-releases").exists()          # refused before any log or lock was made


def test_a_table_target_is_not_adopted_when_the_local_install_names_other_folders(tmp_path, monkeypatch):
    target, local_env = _adopted_main(tmp_path, monkeypatch)
    _write_env(local_env, COGNITA_CONFIG_ROOT=str(tmp_path / "elsewhere"))
    assert release.adopted_by_local(target) is False
    local_env.unlink()
    assert release.adopted_by_local(target) is False


def _set_env(target, **overrides):
    values = release.read_env_file(target.env_file)
    values.update(overrides)
    _write_env(target.env_file, **values)


# --------------------------------------------------------------------------
# 5.1 the local target
# --------------------------------------------------------------------------


def test_table_targets_resolve_to_the_table_entries():
    for name, entry in release.TARGETS.items():
        assert release.resolve_target(name) is entry
    assert release.TARGET_CHOICES == ("beta", "main", "test", "local")


def test_local_target_is_read_from_the_env_file(local, tmp_path):
    assert (local.name, local.profile, local.unit, local.project) == ("local", "cpu", "cognita.service", "cognita")
    # The proof's own connector, not `self-test` (final review, finding 2): qa_release routes by this slug.
    assert (local.mcp_port, local.admin_port, local.connector) == (8675, 8676, "install-proof")
    assert local.env_file == tmp_path / "config" / "cognita.env"
    assert local.releases_root == tmp_path / "local-releases"


def test_local_profile_follows_the_acceleration_key(local):
    _set_env(local, COGNITA_ACCELERATION="amd")
    assert release.resolve_target("local").profile == "amd"
    _set_env(local, COGNITA_ACCELERATION="nvidia")
    assert release.resolve_target("local").profile == "nvidia"
    _set_env(local, COGNITA_ACCELERATION=None)
    assert release.resolve_target("local").profile == "cpu"


def test_local_target_names_the_valid_profiles_when_the_acceleration_is_unknown(local):
    _set_env(local, COGNITA_ACCELERATION="intel")
    with pytest.raises(release.ReleaseError, match="expected cpu, amd or nvidia"):
        release.resolve_target("local")


@pytest.mark.parametrize("key,value", [
    ("COGNITA_ACCELERATION", "gpu"),
    ("COGNITA_MCP_HOST_PORT", "eighty"),
    ("COGNITA_ADMIN_HOST_PORT", "70000"),
    ("COGNITA_RELEASES_ROOT", "relative/releases"),
    ("COGNITA_RELEASES_ROOT", None),
])
def test_local_target_refuses_a_bad_env_file(local, key, value):
    _set_env(local, **{key: value})
    with pytest.raises(release.ReleaseError) as failure:
        release.resolve_target("local")
    assert failure.value.state == "usage"


def test_the_install_state_file_is_install_env_not_a_12x_cognita_env(monkeypatch, tmp_path):
    # kei has a 12.x-era ~/.config/cognita/cognita.env; the installer's own file must not share the
    # name, or it reads that file as an install (seen on the 14.1.0 deploy's prune, 2026-09-29).
    monkeypatch.delenv("COGNITA_LOCAL_ENV_FILE", raising=False)
    monkeypatch.setattr(release.Path, "home", staticmethod(lambda: tmp_path))
    (tmp_path / ".config" / "cognita").mkdir(parents=True)
    (tmp_path / ".config" / "cognita" / "cognita.env").write_text("COGNITA_VERSION=12.17.0\n", encoding="utf-8")
    assert release.local_env_file() == tmp_path / ".config" / "cognita" / "install.env"
    with pytest.raises(release.ReleaseError):
        release.resolve_target("local")          # the old file is not an install


def test_local_target_without_an_env_file_names_it(monkeypatch, tmp_path):
    missing = tmp_path / "nowhere" / "cognita.env"
    monkeypatch.setenv("COGNITA_LOCAL_ENV_FILE", str(missing))
    with pytest.raises(release.ReleaseError) as failure:
        release.resolve_target("local")
    assert str(missing) in str(failure.value)
    with pytest.raises(release.ReleaseError, match="unknown target"):
        release.resolve_target("nonesuch")


def test_path_helpers_follow_the_targets_releases_root(local, tmp_path):
    root = tmp_path / "local-releases" / "local"
    assert release.target_root(local) == root
    assert release.release_dir(local, VERSION) == root / VERSION
    assert release.current_link(local) == root / "current"
    assert release.toolbox_dir(local) == root / "toolbox"
    assert release.logs_dir(local) == root / "logs"
    assert release.selftest_root(local) == root / "self-test"
    # A table target keeps following the module value, looked up at call time.
    assert release.target_root(release.TARGETS["main"]) == tmp_path / "table-releases" / "main"


def test_other_release_uses_tag_includes_local(local, tmp_path, log):
    tag = "cognita/app:14.1.0-local-cpu-x"
    assert release.other_release_uses_tag(tag, None, log) is False
    staged = release.release_dir(local, VERSION)
    staged.mkdir(parents=True)
    (staged / "release.txt").write_text(
        f"version: {VERSION}\ncommit: {COMMIT}\nmode: core\nimage_ref_cognita: {tag}\n", encoding="utf-8")
    assert release.other_release_uses_tag(tag, None, log) is True
    assert release.other_release_uses_tag(tag, staged, log) is False


def test_other_release_uses_tag_retains_when_local_cannot_be_resolved(local, log):
    _set_env(local, COGNITA_MCP_HOST_PORT="bad")
    assert release.other_release_uses_tag("cognita/app:x", None, log) is True


def test_parser_accepts_local_and_publish():
    parser = release.build_parser()
    assert parser.parse_args(["status", "--target", "local"]).target == "local"
    args = parser.parse_args(["publish", "--target", "test", "--registry", "ghcr.io/o", "--amd"])
    assert (args.registry, args.amd) == ("ghcr.io/o", True)
    with pytest.raises(SystemExit):
        parser.parse_args(["status", "--target", "elsewhere"])


def test_live_qa_can_use_an_installed_connector(local):
    parser = release.build_parser()
    assert parser.parse_args(["qa", "--target", "local", "--connector", "cognita-st"]).connector == "cognita-st"
    assert parser.parse_args(["deploy", "--target", "local", "--test", "--connector", "cognita-st"]).connector == "cognita-st"
    assert release.with_qa_connector(local, "cognita-st").connector == "cognita-st"
    assert local.connector == "install-proof"
    with pytest.raises(release.ReleaseError, match="local live QA requires --connector"):
        release.with_qa_connector(local, None)
    with pytest.raises(release.ReleaseError, match="invalid QA connector slug"):
        release.with_qa_connector(local, "../other")


# --------------------------------------------------------------------------
# 5.2 the mode comes from the release
# --------------------------------------------------------------------------


def test_staging_mode_reads_the_workspace_switch(local, log):
    assert release.staging_mode(local, log) == "full"
    _set_env(local, COGNITA_WORKSPACE="off")
    assert release.staging_mode(local, log) == "core"
    _set_env(local, COGNITA_WORKSPACE=None)
    assert release.staging_mode(local, log) == "core"
    _set_env(local, COGNITA_WORKSPACE="maybe")
    with pytest.raises(release.ReleaseError, match="expected on or off"):
        release.staging_mode(local, log)
    assert release.staging_mode(release.TARGETS["main"], log) == "full"


def _stage_dir(root: Path, mode: str | None, name: str = "14.1.0") -> Path:
    directory = root / name
    directory.mkdir(parents=True)
    if mode is not None:
        (directory / "release.txt").write_text(f"version: {name}\nmode: {mode}\n", encoding="utf-8")
    return directory


def test_release_mode_defaults_to_full_and_files_follow_it(tmp_path):
    assert release.release_mode(_stage_dir(tmp_path, None, "old")) == "full"
    assert release.release_mode(_stage_dir(tmp_path, "garbage", "odd")) == "full"
    core = _stage_dir(tmp_path, "core", "core")
    full = _stage_dir(tmp_path, "full", "full")
    (full / "compose.workspace.yaml").write_text("services: {}\n", encoding="utf-8")
    assert [p.name for p in release.staged_compose_files(core, "cpu")] == [
        "compose.yaml", "compose.cpu.yaml", "compose.images.yaml"]
    assert [p.name for p in release.staged_compose_files(full, "cpu")] == [
        "compose.yaml", "compose.cpu.yaml", "compose.workspace.yaml", "compose.images.yaml"]


def test_a_core_release_records_no_workspace_runtime():
    target = release.TARGETS["test"]
    text = release.release_text(
        target=target, version=VERSION, commit=COMMIT, ids={"cognita": "sha256:1"},
        tags={"cognita": "cognita/app:x"}, toolbox_version=TOOLBOX_VERSION, tested=False, mode="core")
    values = dict(line.split(": ", 1) for line in text.splitlines())
    assert values["mode"] == "core" and values["image_ref_cognita"] == "cognita/app:x"
    assert "image_workspace_runtime" not in values and "image_ref_workspace_runtime" not in values
    assert "published_refs" not in values
    values["version"] = VERSION
    assert release.recorded_release_tags(values) == {"cognita": "cognita/app:x"}
    # An unrecorded core release derives only the app tag.
    legacy = release.recorded_release_tags({"mode": "core", "version": VERSION, "commit": COMMIT})
    assert list(legacy) == ["cognita"]
    # A full release with one reference missing is still refused.
    with pytest.raises(release.ReleaseError, match="incomplete"):
        release.recorded_release_tags({"version": VERSION, "commit": COMMIT, "image_ref_cognita": "x"})


def _fake_tagging(monkeypatch):
    commands: list[list[str]] = []

    def fake_run(command, **_kwargs):
        commands.append(command)
        return 0, ""

    monkeypatch.setattr(release, "run", fake_run)
    return commands


def test_core_staging_tags_one_image_and_writes_no_workspace_files(local, tmp_path, monkeypatch, log):
    commands = _fake_tagging(monkeypatch)
    source = tmp_path / "source"
    source.mkdir()
    for name in ("compose.yaml", "compose.cpu.yaml"):
        (source / name).write_text("services: {}\n", encoding="utf-8")
    directory = release.stage_release(source, local, VERSION, COMMIT, {"cognita": "sha256:1"},
                                      TOOLBOX_VERSION, False, log, mode="core")
    assert [c[:2] for c in commands] == [["docker", "tag"]]
    values = release.read_release_text(directory)
    assert values["mode"] == "core" and "image_ref_workspace_runtime" not in values
    assert sorted(p.name for p in directory.iterdir()) == [
        "compose.cpu.yaml", "compose.folders.yaml", "compose.images.yaml", "compose.yaml", "release.txt"]
    assert "workspace" not in (directory / "compose.images.yaml").read_text(encoding="utf-8")


def _apply_harness(monkeypatch, tmp_path, target, directory):
    events: list[str] = []
    monkeypatch.setattr(release, "SYSTEMD_USER_DIR", tmp_path / "systemd")
    monkeypatch.setattr(release, "current_link", lambda _target: directory)
    monkeypatch.setattr(release, "point_current", lambda *_args: None)
    monkeypatch.setattr(release, "warn_on_live_traffic", lambda *_args: None)
    monkeypatch.setattr(release, "toolbox_cache_root", lambda _env: tmp_path / "cache")
    monkeypatch.setattr(release, "load_toolbox", lambda **_kwargs: events.append("toolbox"))

    def fake_run(command, **_kwargs):
        if command[:3] == ["systemctl", "--user", "is-active"]:
            return (3, "inactive")
        if command[:2] == ["systemctl", "--user"]:
            events.append(command[2])
        return (0, "")

    monkeypatch.setattr(release, "run", fake_run)
    return events


def test_apply_reads_the_mode_from_the_release(local, tmp_path, monkeypatch, log):
    core = _stage_dir(tmp_path / "rel", "core")
    events = _apply_harness(monkeypatch, tmp_path, local, core)
    release.apply_release(tmp_path, local, core, TOOLBOX_VERSION, log)
    # Core: no Toolbox archive is needed or loaded, and the unit has no overlay.
    # A first install has no unit file yet, so there is nothing to stop (a
    # `systemctl stop` of a never-loaded unit exits 5; seen on a fresh VM).
    assert events == ["daemon-reload", "start"]
    unit = (tmp_path / "systemd" / local.unit).read_text(encoding="utf-8")
    assert "compose.workspace.yaml" not in unit

    full = _stage_dir(tmp_path / "rel", "full", "14.2.0")
    (full / "compose.workspace.yaml").write_text("services: {}\n", encoding="utf-8")
    (release.toolbox_dir(local)).mkdir(parents=True)
    (release.toolbox_dir(local) / f"toolbox-{TOOLBOX_VERSION}.tar").write_bytes(b"")
    events = _apply_harness(monkeypatch, tmp_path, local, full)
    release.apply_release(tmp_path, local, full, TOOLBOX_VERSION, log)
    assert events == ["stop", "daemon-reload", "toolbox", "start"]
    assert "compose.workspace.yaml" in (tmp_path / "systemd" / local.unit).read_text(encoding="utf-8")


@pytest.mark.parametrize("mode", ["core", "full"])
def test_qa_runs_the_selftest_in_the_releases_mode(local, tmp_path, monkeypatch, log, mode):
    directory = _stage_dir(tmp_path / "rel", mode)
    if mode == "full":
        (directory / "compose.workspace.yaml").write_text("services: {}\n", encoding="utf-8")
    monkeypatch.setattr(release, "current_link", lambda _target: directory)
    monkeypatch.setattr(release, "check_healthz", lambda *_a, **_k: {})
    monkeypatch.setattr(release, "self_test_key", lambda _repo: "key")
    monkeypatch.setattr(release, "expect_unauthorized", lambda *_a: None)
    monkeypatch.setattr(release, "run", lambda *_a, **_k: (0, ""))
    seen = {}
    monkeypatch.setattr(release, "run_live_selftest", lambda **kwargs: seen.update(kwargs))
    release.qa_release(tmp_path, local, VERSION, tmp_path, log)
    assert seen["mode"] == mode
    names = [p.name for p in seen["compose_files"]]
    assert ("compose.workspace.yaml" in names) == (mode == "full")


# --------------------------------------------------------------------------
# 5.3 the folders fragment
# --------------------------------------------------------------------------


def _fragment(target, tmp_path, log, name="rel"):
    directory = tmp_path / name
    directory.mkdir(exist_ok=True)
    path = release.write_folders_fragment(target, directory, log)
    return path, path.read_text(encoding="utf-8")


def _selftest_line(target) -> str:
    quoted = json.dumps(str(release.selftest_root(target)), ensure_ascii=False)
    return f"      - {{type: bind, source: {quoted}, target: {quoted}}}"


# 19.6: both services are told the command name and the target name, so their reset hints name what the
# user types.  The runtime service is named only when the release has one (a directory with no release.txt
# reads as full, which is what these tests stage).
PASSED_THROUGH = ('      COGNITA_COMMAND: "./cognita"\n'
                  '      COGNITA_RELEASE_TARGET: "local"\n')
RUNTIME_BLOCK = "  workspace-runtime:\n    environment:\n" + PASSED_THROUGH


def test_fragment_with_only_the_first_root(local, tmp_path, log):
    _path, text = _fragment(local, tmp_path, log)
    assert text == (
        "# Generated by release.py from the env file; rewritten by `cognita add-folder`.\n"
        "services:\n  cognita:\n    environment:\n"
        "      COGNITA_DOCUMENT_ROOTS: '[\"/home/u/Documents\"]'\n" + PASSED_THROUGH +
        "    volumes:\n" + _selftest_line(local) + "\n" + RUNTIME_BLOCK)
    # The Self-Test root is created as the user, never left for Docker to create as root.
    assert release.selftest_root(local).is_dir()


def test_fragment_with_a_root_whose_name_has_spaces(local, tmp_path, log):
    _set_env(local, COGNITA_PROJECTS_ROOT_2="/home/u/OneDrive - Personal")
    _path, text = _fragment(local, tmp_path, log)
    assert text == (
        "# Generated by release.py from the env file; rewritten by `cognita add-folder`.\n"
        "services:\n  cognita:\n    environment:\n"
        "      COGNITA_DOCUMENT_ROOTS: "
        "'[\"/home/u/Documents\", \"/home/u/OneDrive - Personal\"]'\n" + PASSED_THROUGH +
        "    volumes:\n"
        "      - {type: bind, source: \"/home/u/OneDrive - Personal\", "
        "target: \"/home/u/OneDrive - Personal\", bind: {propagation: rslave}}\n"
        + _selftest_line(local) + "\n" + RUNTIME_BLOCK)


def test_fragment_with_three_extra_roots_binds_each_but_not_the_first(local, tmp_path, log):
    _set_env(local, COGNITA_PROJECTS_ROOT_2="/a", COGNITA_PROJECTS_ROOT_4="/c", COGNITA_PROJECTS_ROOT_9="/z")
    _path, text = _fragment(local, tmp_path, log)
    assert "COGNITA_DOCUMENT_ROOTS: '[\"/home/u/Documents\", \"/a\", \"/c\", \"/z\"]'" in text
    binds = [line for line in text.splitlines() if line.startswith("      - {type: bind")]
    assert len(binds) == 4  # three extras + the Self-Test root
    assert all("/home/u/Documents" not in line for line in binds)
    # The Self-Test root is bound but is not a user root.
    assert str(release.selftest_root(local)).replace("\\", "\\\\") in binds[-1]
    documents_line = next(line for line in text.splitlines() if "COGNITA_DOCUMENT_ROOTS" in line)
    assert "self-test" not in documents_line


@pytest.mark.parametrize("bad", ["relative/dir", "/has$dollar", '/has"quote', "/has'quote", "/has%pct", "/has\\back"])
def test_fragment_refuses_a_root_it_cannot_quote_safely(local, tmp_path, log, bad):
    _set_env(local, COGNITA_PROJECTS_ROOT_2=bad)
    with pytest.raises(release.ReleaseError, match="not a usable documents root"):
        release.write_folders_fragment(local, tmp_path, log)


def test_table_targets_get_no_fragment(tmp_path, log):
    assert release.write_folders_fragment(release.TARGETS["main"], tmp_path, log) is None
    assert not (tmp_path / "compose.folders.yaml").exists()


def test_staged_files_include_the_fragment_last_and_only_when_present(tmp_path):
    directory = _stage_dir(tmp_path, "core")
    assert release.staged_compose_files(directory, "cpu")[-1].name == "compose.images.yaml"
    (directory / "compose.folders.yaml").write_text("services: {}\n", encoding="utf-8")
    assert release.staged_compose_files(directory, "cpu")[-1].name == "compose.folders.yaml"


def test_local_staging_writes_the_fragment_and_a_table_target_does_not(local, tmp_path, monkeypatch, log):
    _fake_tagging(monkeypatch)
    source = tmp_path / "source"
    source.mkdir()
    for name in ("compose.yaml", "compose.cpu.yaml", "compose.amd.yaml", "compose.workspace.yaml"):
        (source / name).write_text("services: {}\n", encoding="utf-8")
    ids = {"cognita": "sha256:1", "workspace-runtime": "sha256:2"}
    directory = release.stage_release(source, local, VERSION, COMMIT, ids, TOOLBOX_VERSION, False, log)
    assert (directory / "compose.folders.yaml").is_file()
    other = release.stage_release(source, release.TARGETS["test"], VERSION, COMMIT, ids,
                                  TOOLBOX_VERSION, False, log)
    assert not (other / "compose.folders.yaml").exists()


# --------------------------------------------------------------------------
# published-release.txt and 5.4 stage_published
# --------------------------------------------------------------------------


def _write_published(path: Path, values: dict[str, str | None]) -> Path:
    path.write_text("".join(f"{key}: {value}\n" for key, value in values.items() if value is not None),
                    encoding="utf-8")
    return path


def test_published_release_round_trips(tmp_path):
    path = _write_published(tmp_path / "published-release.txt", PUBLISHED)
    assert release.read_published_release(path) == PUBLISHED
    rows = list(PUBLISHED.items())
    path.write_text(release.published_release_text(rows), encoding="utf-8")
    assert release.read_published_release(path) == PUBLISHED


@pytest.mark.parametrize("change,message", [
    ({"commit": "abc"}, "full SHA-1"),
    ({"version": None}, "missing version"),
    ({"image_ref_toolbox": "ghcr.io/o/x:latest"}, "sha256"),
    ({"toolbox_version": None}, "missing toolbox_version"),
])
def test_published_release_refuses_a_bad_file(tmp_path, change, message):
    path = _write_published(tmp_path / "p.txt", {**PUBLISHED, **change})
    with pytest.raises(release.ReleaseError, match=message):
        release.read_published_release(path)
    with pytest.raises(release.ReleaseError, match="names no published"):
        release.read_published_release(tmp_path / "absent.txt")


POSTGRES_REF = "pgvector/pgvector:0.8.6-pg18-trixie@sha256:" + "7" * 64


class FakeDocker:
    """Docker and git as `stage_published` sees them."""

    def __init__(self, monkeypatch, tmp_path, present=()):
        self.present = set(present)
        self.commands: list[list[str]] = []
        self.toolbox_exports: list[str] = []
        self.git_reads: list[tuple[str, str]] = []
        self.labels: dict[str, tuple[str, str]] = {}
        self.published = _write_published(tmp_path / "published-release.txt", PUBLISHED)
        monkeypatch.setattr(release, "PUBLISHED_RELEASE", self.published)
        monkeypatch.setattr(release, "image_exists", lambda ref: ref in self.present)
        monkeypatch.setattr(release, "run", self._run)
        monkeypatch.setattr(release, "image_identity", self._identity)
        monkeypatch.setattr(release, "image_id", lambda ref: "sha256:id-" + ref[-6:])
        monkeypatch.setattr(release, "_label_version", lambda ref: self.labels.get(ref, (TOOLBOX_VERSION, ""))[0])
        monkeypatch.setattr(release, "build_toolbox",
                            lambda repo, target, version, log: self.toolbox_exports.append(version))
        monkeypatch.setattr(release, "git_show_file", self._git_show)
        # A checkout has a .git; the tree-only case (19.4) has its own tests below.
        monkeypatch.setattr(release, "repo_has_git", lambda repo: True)

    def _run(self, command, **_kwargs):
        self.commands.append(command)
        if command[:2] == ["docker", "pull"]:
            self.present.add(command[2])
        return 0, ""

    def _identity(self, ref):
        version, commit = self.labels.get(ref, (VERSION, COMMIT))
        return f"sha256:id-{ref[-6:]}", version, commit

    def _git_show(self, repo, commit, name, log):
        self.git_reads.append((commit, name))
        if name == "compose.yaml":
            return (f"# {name} at {commit}\nservices:\n  postgres:\n    image: {POSTGRES_REF}\n").encode()
        return f"# {name} at {commit}\nservices: {{}}\n".encode()

    def of(self, verb):
        return [c for c in self.commands if c[:2] == ["docker", verb]]


def test_stage_published_pulls_checks_tags_and_stages_from_the_published_commit(local, tmp_path, monkeypatch, log):
    docker = FakeDocker(monkeypatch, tmp_path)
    directory = release.stage_published(local, log)
    assert directory == release.release_dir(local, VERSION)
    pulls = [c[2] for c in docker.of("pull")]
    # PostgreSQL comes from the published commit's compose.yaml pin: the unit
    # starts with --pull never, so staging must fetch it too.
    assert pulls == [PUBLISHED["image_ref_cognita_cpu"], PUBLISHED["image_ref_workspace_runtime"],
                     PUBLISHED["image_ref_toolbox"], POSTGRES_REF]
    pulls = pulls[:-1]   # ...but it is a shared public image, never one uninstall removes
    # The Toolbox gets the one tag the broker loader accepts, then the existing export.
    assert ["docker", "tag", PUBLISHED["image_ref_toolbox"], f"cognita-workspace-toolbox:{TOOLBOX_VERSION}"] \
        in docker.commands
    assert docker.toolbox_exports == [TOOLBOX_VERSION]
    # Per-release tags by image ID, exactly as a source build does.
    tags = release.release_tags(VERSION, COMMIT, local)
    assert ["docker", "tag", "sha256:id-111111", tags["cognita"]] in docker.commands
    assert ["docker", "tag", "sha256:id-333333", tags["workspace-runtime"]] in docker.commands
    # Compose files come from the PUBLISHED commit, not HEAD.
    assert docker.git_reads == [(COMMIT, "compose.yaml"), (COMMIT, "compose.cpu.yaml"),
                                (COMMIT, "compose.workspace.yaml")]
    assert (directory / "compose.yaml").read_text(encoding="utf-8").startswith(f"# compose.yaml at {COMMIT}")
    values = release.read_release_text(directory)
    assert (values["version"], values["commit"], values["mode"], values["profile"]) == (
        VERSION, COMMIT, "full", "cpu")
    assert values["published_refs"].split() == pulls
    assert values["image_ref_cognita"] == tags["cognita"]
    assert (directory / "compose.folders.yaml").is_file()
    assert os.environ["COGNITA_VERSION"] == VERSION
    # It stages and stops: nothing was applied, verified or tested.
    assert not [c for c in docker.commands if c[0] == "systemctl"]


def test_stage_published_skips_pulls_of_images_already_present(local, tmp_path, monkeypatch, log):
    docker = FakeDocker(monkeypatch, tmp_path, present=[
        PUBLISHED["image_ref_cognita_cpu"], PUBLISHED["image_ref_workspace_runtime"],
        PUBLISHED["image_ref_toolbox"], POSTGRES_REF])
    release.stage_published(local, log)
    assert docker.of("pull") == []


def test_stage_published_core_mode_pulls_only_the_app_image(local, tmp_path, monkeypatch, log):
    _set_env(local, COGNITA_WORKSPACE="off")
    docker = FakeDocker(monkeypatch, tmp_path)
    directory = release.stage_published(local, log)
    assert [c[2] for c in docker.of("pull")] == [PUBLISHED["image_ref_cognita_cpu"], POSTGRES_REF]
    assert docker.toolbox_exports == []
    assert docker.git_reads == [(COMMIT, "compose.yaml"), (COMMIT, "compose.cpu.yaml")]
    values = release.read_release_text(directory)
    assert values["mode"] == "core" and "image_ref_workspace_runtime" not in values
    assert values["published_refs"] == PUBLISHED["image_ref_cognita_cpu"]


def test_stage_published_uses_the_amd_image_for_an_amd_target(local, tmp_path, monkeypatch, log):
    _set_env(local, COGNITA_ACCELERATION="amd")
    docker = FakeDocker(monkeypatch, tmp_path)
    release.stage_published(release.resolve_target("local"), log)
    assert docker.of("pull")[0][2] == PUBLISHED["image_ref_cognita_amd"]
    assert docker.git_reads[1] == (COMMIT, "compose.amd.yaml")


@pytest.mark.parametrize("ref_key,labels", [
    ("image_ref_cognita_cpu", ("14.0.0", COMMIT)),
    ("image_ref_cognita_cpu", (VERSION, "b" * 40)),
    ("image_ref_workspace_runtime", (VERSION, "b" * 40)),
])
def test_stage_published_refuses_a_label_mismatch_and_stages_nothing(local, tmp_path, monkeypatch, log, ref_key, labels):
    docker = FakeDocker(monkeypatch, tmp_path)
    docker.labels[PUBLISHED[ref_key]] = labels
    with pytest.raises(release.ReleaseError, match="identity mismatch"):
        release.stage_published(local, log)
    assert not release.release_dir(local, VERSION).exists()
    assert docker.git_reads == []


def test_stage_published_refuses_a_toolbox_version_label_mismatch(local, tmp_path, monkeypatch, log):
    docker = FakeDocker(monkeypatch, tmp_path)
    docker.labels[PUBLISHED["image_ref_toolbox"]] = ("11.0.0", "")
    with pytest.raises(release.ReleaseError, match="identity mismatch"):
        release.stage_published(local, log)
    assert not release.release_dir(local, VERSION).exists()


def test_stage_published_refuses_a_different_commit_at_the_same_version(local, tmp_path, monkeypatch, log):
    docker = FakeDocker(monkeypatch, tmp_path)
    existing = release.release_dir(local, VERSION)
    existing.mkdir(parents=True)
    (existing / "release.txt").write_text(f"version: {VERSION}\ncommit: {'c' * 40}\n", encoding="utf-8")
    with pytest.raises(release.ReleaseError) as failure:
        release.stage_published(local, log)
    assert failure.value.state == "version-exists"
    assert docker.of("pull") == []


def test_restaging_the_same_published_release_replaces_it(local, tmp_path, monkeypatch, log):
    FakeDocker(monkeypatch, tmp_path)
    first = release.stage_published(local, log)
    (first / "stray.txt").write_text("x\n", encoding="utf-8")
    second = release.stage_published(local, log)
    assert second == first and not (second / "stray.txt").exists()


def test_git_show_file_reports_a_shallow_clone(monkeypatch, log):
    seen = []

    def fake_git(command, **kwargs):
        seen.append(command)
        return SimpleNamespace(returncode=128, stdout=b"", stderr=b"fatal: invalid object name 'aaaa'.\n")

    monkeypatch.setattr(release.subprocess, "run", fake_git)
    with pytest.raises(release.ReleaseError) as failure:
        release.git_show_file(Path("/repo"), COMMIT, "compose.yaml", log)
    assert "Run git fetch --unshallow, then retry." in str(failure.value)
    assert seen[0][-1] == f"{COMMIT}:compose.yaml"

    monkeypatch.setattr(release.subprocess, "run",
                        lambda *_a, **_k: SimpleNamespace(returncode=0, stdout=b"services: {}\n", stderr=b""))
    assert release.git_show_file(Path("/repo"), COMMIT, "compose.yaml", log) == b"services: {}\n"


# --------------------------------------------------------------------------
# 5.5 publish
# --------------------------------------------------------------------------


REGISTRY = "ghcr.io/o"


class FakePublish:
    def __init__(self, monkeypatch, tmp_path, *, suite_fails=False, smoke_fails=False, toolbox_tag_present=True,
                 load_creates_tag=True):
        self.events: list[str] = []
        self.toolbox_tag_present = toolbox_tag_present
        self.archive = tmp_path / "toolbox" / f"toolbox-{TOOLBOX_VERSION}.tar"
        self.commands: list[list[str]] = []
        self.output = tmp_path / "containers" / "published-release.txt"
        self.captured: list[list[str]] = []
        push_order: list[str] = []
        self.push_order = push_order

        def event(name):
            return lambda *a, **k: self.events.append(name)

        monkeypatch.setattr(release, "validate_cpu_ocr_inputs", event("validate"))
        # 14.2.0 (design 5.5 step 2): the OCR weights are fetched into the TEST target's model cache
        # root; the fake records the root it was asked for and hands back a fake weights directory.
        self.models_root = tmp_path / "test-models"
        self.weights_dir = self.models_root / "easyocr"
        self.fetched: list[Path] = []
        monkeypatch.setattr(release, "target_models_root", lambda env_file: self.models_root)

        def fetch(models_root, log):
            self.events.append("ocr-weights")
            self.fetched.append(models_root)
            return models_root / "easyocr"

        monkeypatch.setattr(release, "fetch_ocr_weights", fetch)
        monkeypatch.setattr(release, "stage_microsandbox_wheel", event("wheel"))
        monkeypatch.setattr(release, "docker_build_app",
                            lambda repo, profile, version, commit, ref, log: self.events.append(f"build-app-{profile}"))
        monkeypatch.setattr(release, "docker_build_workspace_runtime", lambda *a: self.events.append("build-runtime"))
        monkeypatch.setattr(release, "verify_candidate_image", lambda ref, v, c, i=None: "sha256:built-" + ref[-4:])

        def build_toolbox(*a):
            self.events.append("build-toolbox")
            return self.archive

        monkeypatch.setattr(release, "build_toolbox", build_toolbox)
        # The Toolbox tag is checked with image_exists; the fake says whether the tag is there and
        # `docker load` of the archive is what brings it back.
        monkeypatch.setattr(release, "image_exists",
                            lambda ref: self.toolbox_tag_present if ref.startswith("cognita-workspace-toolbox:")
                            else True)

        def smoke(*a, **k):
            self.events.append("smoke")
            self.smoke_weights = k.get("weights_dir")
            if smoke_fails:
                raise release.ReleaseError("build-failed", "ocr smoke failed")

        def suite(*a, **k):
            self.events.append("suite")
            assert k["profile"] == "cpu" and k["mode"] == "full"
            if suite_fails:
                raise release.ReleaseError("test-failed", "suite failed")

        monkeypatch.setattr(release, "cpu_ocr_smoke", smoke)
        monkeypatch.setattr(release, "run_test_stack", suite)

        def run(command, **_kwargs):
            self.commands.append(command)
            self.events.append(" ".join(command[:2]))
            if command[:2] == ["docker", "load"] and load_creates_tag:
                self.toolbox_tag_present = True
            if command[:2] == ["docker", "push"]:
                push_order.append(command[2])
            return 0, ""

        monkeypatch.setattr(release, "run", run)

        def capture(command, **_kwargs):
            self.captured.append(command)
            reference = command[-1]
            if command[:3] == ["docker", "image", "inspect"]:
                repository = reference.rsplit(":", 1)[0]
                return json.dumps([f"{repository}@sha256:" + "9" * 64, "other/repo@sha256:" + "8" * 64])
            if command[:3] == ["docker", "manifest", "inspect"]:
                return json.dumps({"SchemaV2Manifest": {"layers": [{"size": 10}, {"size": 32}]}})
            raise AssertionError(command)

        monkeypatch.setattr(release, "_capture", capture)

        # The WSL image step has its own tests (test_release_local_19.py). Here it is only a recorded
        # step, because the real recipe is in the repo once the Windows installer has landed and would
        # otherwise run for real inside the publish flow.
        self.wsl_published = None

        def wsl(repo, target, version, commit, published, log):
            self.events.append("wsl")
            self.wsl_published = published
            return []

        monkeypatch.setattr(release, "build_wsl_artifacts", wsl)


def _publish(tmp_path, log, fake, *, amd=False, nvidia=False, registry=REGISTRY):
    release.publish_release(REPO, release.TARGETS["test"], VERSION, COMMIT, TOOLBOX_VERSION, registry,
                            amd=amd, nvidia=nvidia, log=log, output=fake.output)


def test_publish_builds_proves_pushes_and_writes_the_file(tmp_path, monkeypatch, log):
    fake = FakePublish(monkeypatch, tmp_path)
    _publish(tmp_path, log, fake)
    events = fake.events
    # 14.2.0 (design 5.5 step 2): the weights are fetched into the test target's model cache root
    # BEFORE any build, and that same directory is what the OCR smoke mounts.
    assert fake.fetched == [fake.models_root]
    assert events.index("ocr-weights") < events.index("build-app-cpu") < events.index("smoke")
    assert fake.smoke_weights == fake.weights_dir
    # Everything is proven before anything is pushed.
    assert events.index("suite") < events.index("docker tag")
    assert events.index("smoke") < events.index("suite")
    assert "build-app-amd" not in events
    assert fake.push_order == [f"{REGISTRY}/cognita-app:{VERSION}-cpu",
                               f"{REGISTRY}/cognita-workspace-runtime:{VERSION}",
                               f"{REGISTRY}/cognita-workspace-toolbox:{TOOLBOX_VERSION}"]
    text = fake.output.read_text(encoding="utf-8")
    d = "sha256:" + "9" * 64
    assert text.splitlines() == [
        f"version: {VERSION}",
        f"commit: {COMMIT}",
        next(line for line in text.splitlines() if line.startswith("published_at: ")),
        f"image_ref_cognita_cpu: {REGISTRY}/cognita-app@{d}",
        f"image_ref_workspace_runtime: {REGISTRY}/cognita-workspace-runtime@{d}",
        f"image_ref_toolbox: {REGISTRY}/cognita-workspace-toolbox@{d}",
        f"toolbox_version: {TOOLBOX_VERSION}",
        "size_cognita_cpu: 42",
        "size_workspace_runtime: 42",
        "size_toolbox: 42",
    ]
    # And what it wrote is what stage_published reads back.
    assert release.read_published_release(fake.output)["image_ref_cognita_cpu"] == f"{REGISTRY}/cognita-app@{d}"
    # Design 19.3: the WSL image is built AFTER the images are pushed and the file is written, from
    # that same file (the image carries it).
    last_push = max(i for i, e in enumerate(events) if e == "docker push")
    assert events.index("wsl") > last_push
    assert fake.wsl_published == fake.output


def test_publish_recreates_a_missing_toolbox_tag_from_the_archive_before_pushing(tmp_path, monkeypatch, log):
    """build_toolbox returns as soon as the archive exists, so a tag removed elsewhere (shared on kei) used to
    make the later `docker tag` of the Toolbox fail after the whole suite had run (final review, finding 6)."""
    fake = FakePublish(monkeypatch, tmp_path, toolbox_tag_present=False)
    _publish(tmp_path, log, fake)
    load = [c for c in fake.commands if c[:2] == ["docker", "load"]]
    assert load == [["docker", "load", "-i", str(fake.archive)]]
    assert fake.events.index("docker load") < fake.events.index("smoke") < fake.events.index("docker tag")
    assert f"{REGISTRY}/cognita-workspace-toolbox:{TOOLBOX_VERSION}" in fake.push_order


def test_publish_reloads_the_archive_even_when_the_toolbox_tag_is_present(tmp_path, monkeypatch, log):
    """P10a, 2026-09-29: a present tag can hold a different build of the same Toolbox version than the
    archive every kei target runs; 14.2.1 pushed that other build.  The archive is always reloaded first."""
    fake = FakePublish(monkeypatch, tmp_path)
    _publish(tmp_path, log, fake)
    assert [c for c in fake.commands if c[:2] == ["docker", "load"]] == [["docker", "load", "-i", str(fake.archive)]]
    assert fake.events.index("docker load") < fake.events.index("docker tag")


def test_publish_stops_before_any_push_when_loading_the_archive_does_not_bring_the_tag_back(tmp_path, monkeypatch, log):
    fake = FakePublish(monkeypatch, tmp_path, toolbox_tag_present=False, load_creates_tag=False)
    with pytest.raises(release.ReleaseError, match="did not create"):
        _publish(tmp_path, log, fake)
    assert fake.push_order == [] and "suite" not in fake.events


def test_publish_with_amd_adds_the_amd_image_after_the_suite(tmp_path, monkeypatch, log):
    fake = FakePublish(monkeypatch, tmp_path)
    _publish(tmp_path, log, fake, amd=True)
    assert fake.events.index("suite") < fake.events.index("build-app-amd") < fake.events.index("docker tag")
    values = release.read_published_release(fake.output)
    assert values["image_ref_cognita_amd"] == f"{REGISTRY}/cognita-app@sha256:" + "9" * 64
    assert values["size_cognita_amd"] == "42"
    lines = fake.output.read_text(encoding="utf-8").splitlines()
    keys = [line.partition(":")[0] for line in lines]
    assert keys == ["version", "commit", "published_at", "image_ref_cognita_cpu", "image_ref_cognita_amd",
                    "image_ref_workspace_runtime", "image_ref_toolbox", "toolbox_version",
                    "size_cognita_cpu", "size_cognita_amd", "size_workspace_runtime", "size_toolbox"]
    assert fake.push_order[1] == f"{REGISTRY}/cognita-app:{VERSION}-amd"


def test_publish_with_nvidia_adds_the_nvidia_image_after_the_suite_and_records_its_keys(tmp_path, monkeypatch, log):
    """15.0.0 (DESIGN-NVIDIA-ACCELERATION 10): `publish --nvidia` builds cognita-app:<version>-nvidia, verifies
    its labels, pushes it, and writes image_ref_cognita_nvidia and size_cognita_nvidia (both optional keys,
    like AMD's).  Without the flag nothing about NVIDIA is built or written."""
    fake = FakePublish(monkeypatch, tmp_path)
    verified: list[str] = []
    monkeypatch.setattr(release, "verify_candidate_image",
                        lambda ref, v, c, i=None: verified.append(ref) or "sha256:built-" + ref[-4:])
    _publish(tmp_path, log, fake, nvidia=True)
    assert release.candidate_app_reference("nvidia", VERSION, COMMIT) in verified      # its labels were checked
    assert fake.events.index("suite") < fake.events.index("build-app-nvidia") < fake.events.index("docker tag")
    assert "build-app-amd" not in fake.events
    values = release.read_published_release(fake.output)
    assert values["image_ref_cognita_nvidia"] == f"{REGISTRY}/cognita-app@sha256:" + "9" * 64
    assert values["size_cognita_nvidia"] == "42"
    assert "image_ref_cognita_amd" not in values
    keys = [line.partition(":")[0] for line in fake.output.read_text(encoding="utf-8").splitlines()]
    assert keys == ["version", "commit", "published_at", "image_ref_cognita_cpu", "image_ref_cognita_nvidia",
                    "image_ref_workspace_runtime", "image_ref_toolbox", "toolbox_version",
                    "size_cognita_cpu", "size_cognita_nvidia", "size_workspace_runtime", "size_toolbox"]
    assert fake.push_order[1] == f"{REGISTRY}/cognita-app:{VERSION}-nvidia"
    assert "app images to push=['cognita_cpu', 'cognita_nvidia']" in Path(log.path).read_text(encoding="utf-8")


def test_publish_with_both_gpu_flags_pushes_amd_then_nvidia(tmp_path, monkeypatch, log):
    fake = FakePublish(monkeypatch, tmp_path)
    _publish(tmp_path, log, fake, amd=True, nvidia=True)
    assert fake.events.index("build-app-amd") < fake.events.index("build-app-nvidia")
    assert fake.push_order[1:3] == [f"{REGISTRY}/cognita-app:{VERSION}-amd", f"{REGISTRY}/cognita-app:{VERSION}-nvidia"]
    keys = [line.partition(":")[0] for line in fake.output.read_text(encoding="utf-8").splitlines()]
    assert keys.index("image_ref_cognita_amd") < keys.index("image_ref_cognita_nvidia")
    assert keys.index("size_cognita_amd") < keys.index("size_cognita_nvidia")


def test_publish_parser_takes_nvidia_beside_amd():
    args = release.build_parser().parse_args(["publish", "--target", "test", "--registry", "ghcr.io/o", "--nvidia"])
    assert (args.amd, args.nvidia) == (False, True)
    args = release.build_parser().parse_args(["publish", "--target", "test", "--registry", "ghcr.io/o", "--amd"])
    assert (args.amd, args.nvidia) == (True, False)
    args = release.build_parser().parse_args(["build", "--target", "test", "--profiles", "cpu,nvidia"])
    assert args.profiles == "cpu,nvidia"
    assert release.build_parser().parse_args(["test", "--target", "test", "--profile", "nvidia"]).profile == "nvidia"
    assert release.build_parser().parse_args(["deploy", "--target", "beta", "--profile", "nvidia"]).profile == "nvidia"


@pytest.mark.parametrize("fails", ["suite_fails", "smoke_fails"])
def test_publish_pushes_nothing_when_a_proof_fails(tmp_path, monkeypatch, log, fails):
    fake = FakePublish(monkeypatch, tmp_path, **{fails: True})
    with pytest.raises(release.ReleaseError):
        _publish(tmp_path, log, fake, amd=True)
    assert fake.push_order == []
    assert not [c for c in fake.commands if c[:2] in (["docker", "tag"], ["docker", "push"])]
    assert not fake.output.exists()


def test_publish_refuses_a_malformed_registry(tmp_path, monkeypatch, log):
    fake = FakePublish(monkeypatch, tmp_path)
    with pytest.raises(release.ReleaseError, match="--registry"):
        _publish(tmp_path, log, fake, registry="just-a-name")
    assert fake.events == []


def test_cmd_publish_is_restricted_to_the_test_target(log):
    with pytest.raises(release.ReleaseError, match="restricted to --target test"):
        release.cmd_publish(SimpleNamespace(registry=REGISTRY, amd=False), release.TARGETS["main"], log)


def test_manifest_size_sums_layers_for_single_and_list_manifests(monkeypatch):
    def single(command, **_k):
        return json.dumps({"SchemaV2Manifest": {"layers": [{"size": 5}, {"size": 7}]}})

    monkeypatch.setattr(release, "_capture", single)
    assert release.manifest_compressed_size("r@sha256:x") == 12

    def listing(command, **_k):
        return json.dumps([
            {"Descriptor": {"platform": {"architecture": "arm64"}}, "OCIManifest": {"layers": [{"size": 1}]}},
            {"Descriptor": {"platform": {"architecture": "amd64"}}, "OCIManifest": {"layers": [{"size": 3}, {"size": 4}]}},
        ])

    monkeypatch.setattr(release, "_capture", listing)
    assert release.manifest_compressed_size("r@sha256:x") == 7
    monkeypatch.setattr(release, "_capture", lambda *_a, **_k: json.dumps({"SchemaV2Manifest": {"layers": []}}))
    with pytest.raises(release.ReleaseError, match="no layers"):
        release.manifest_compressed_size("r@sha256:x")


def test_manifest_size_asks_insecure_only_for_a_local_registry(monkeypatch):
    seen = []

    def capture(command, **_k):
        seen.append(command)
        return json.dumps({"SchemaV2Manifest": {"layers": [{"size": 1}]}})

    monkeypatch.setattr(release, "_capture", capture)
    release.manifest_compressed_size("localhost:5000/cognita/cognita-app@sha256:x")
    release.manifest_compressed_size("ghcr.io/owner/cognita-app@sha256:x")
    assert "--insecure" in seen[0]
    assert "--insecure" not in seen[1]


def test_pushed_digest_matches_only_the_pushed_repository(monkeypatch):
    monkeypatch.setattr(release, "_capture", lambda *_a, **_k: json.dumps(
        ["ghcr.io/o/a@sha256:" + "1" * 64, "ghcr.io/o/b@sha256:" + "2" * 64]))
    assert release.pushed_digest("ghcr.io/o/b", "ghcr.io/o/b:1") == "ghcr.io/o/b@sha256:" + "2" * 64
    with pytest.raises(release.ReleaseError, match="exactly one"):
        release.pushed_digest("ghcr.io/o/c", "ghcr.io/o/c:1")


# --------------------------------------------------------------------------
# 5.6 unit rendering
# --------------------------------------------------------------------------

# The template exactly as it was at 13.7.0, before ${EXEC_START_PRE} and
# ${DESCRIPTION} existed.  kei's installed units were rendered from it, and the
# new template must render the SAME BYTES for every table target, or
# install_unit rewrites them.
OLD_TEMPLATE = """[Unit]
Description=Cognita ${TARGET} container stack (${PROFILE}, ${VERSION_NOTE})
Documentation=https://github.com/dbeachy1/Cognita
Wants=network-online.target
# Docker runs under the system manager while this is a user unit, so a hard
# dependency on docker.service would resolve in the wrong manager and fail the
# unit.  The Compose CLI is the readiness gate instead: `up --wait` blocks
# until every service healthcheck passes.
After=network-online.target

[Service]
Type=oneshot
RemainAfterExit=yes
# No WorkingDirectory: every Compose file is named by absolute path, and
# `current` is a symlink that only exists once a release has been applied.
# Startup never builds, pulls, tests, migrates, restores or resets
# (DESIGN-13.0 section 6.4).
ExecStart=/usr/bin/docker compose -p ${PROJECT} --env-file ${ENV_FILE} ${COMPOSE_FILES} up -d --no-build --pull never --wait
ExecStop=/usr/bin/docker compose -p ${PROJECT} --env-file ${ENV_FILE} ${COMPOSE_FILES} down
TimeoutStartSec=15min
TimeoutStopSec=2min

[Install]
WantedBy=default.target
"""


def _old_render(target, note: str) -> str:
    current = release.current_link(target)
    files = release.staged_compose_files(current, target.profile)
    return string.Template(OLD_TEMPLATE).substitute(
        TARGET=target.name, PROFILE=target.profile, PROJECT=target.project,
        ENV_FILE=str(target.env_file), CURRENT=str(current),
        COMPOSE_FILES=" ".join(f"-f {path}" for path in files), VERSION_NOTE=note)


@pytest.mark.parametrize("name", ["main", "beta", "test"])
def test_table_target_units_render_byte_identical_to_the_old_template(name):
    target = release.TARGETS[name]
    new = release.render_unit(target, version_note="release.py-managed")
    assert new == _old_render(target, "release.py-managed")
    assert new.startswith(f"[Unit]\nDescription=Cognita {name} container stack (amd, release.py-managed)\n")
    assert "\nExecStart=/usr/bin/docker compose -p " in new and "ExecStartPre" not in new


def test_local_unit_quotes_every_path_and_makes_the_roots(local, tmp_path):
    _set_env(local, COGNITA_PROJECTS_ROOT="/home/u/Documents",
             COGNITA_PROJECTS_ROOT_2="/home/u/OneDrive - Personal")
    current = release.current_link(local)
    current.mkdir(parents=True)
    (current / "release.txt").write_text("mode: core\n", encoding="utf-8")
    (current / "compose.folders.yaml").write_text("services: {}\n", encoding="utf-8")
    text = release.render_unit(local, version_note="release.py-managed")
    assert "$" not in text
    lines = text.splitlines()
    assert "Description=Cognita (cpu, installed by ./cognita)" in lines
    pre = [line for line in lines if line.startswith("ExecStartPre=")]
    assert pre == ['ExecStartPre=-/usr/bin/mkdir -p -- "/home/u/Documents"',
                   'ExecStartPre=-/usr/bin/mkdir -p -- "/home/u/OneDrive - Personal"']
    start = lines.index(next(line for line in lines if line.startswith("ExecStart=")))
    assert lines[start - 2:start] == pre  # the mkdir lines come immediately before ExecStart
    files = release.staged_compose_files(current, "cpu")
    expected_files = " ".join(f'-f "{path}"' for path in files)
    expected = (f'-p cognita --env-file "{local.env_file}" {expected_files}')
    assert f"ExecStart=/usr/bin/docker compose {expected} up -d --no-build --pull never --wait" in lines
    assert f"ExecStop=/usr/bin/docker compose {expected} down" in lines
    assert "compose.workspace.yaml" not in text  # the release is core
    assert text.count('"') % 2 == 0


def test_local_unit_with_no_roots_has_no_execstartpre(local):
    _set_env(local, COGNITA_PROJECTS_ROOT=None)
    assert "ExecStartPre" not in release.render_unit(local, version_note="x")


def test_enable_unit_enables_the_targets_unit(local, monkeypatch, log):
    commands = _fake_tagging(monkeypatch)
    release.enable_unit(local, log)
    assert commands == [["systemctl", "--user", "enable", "cognita.service"]]


# --------------------------------------------------------------------------
# 5.7 lock-free functions
# --------------------------------------------------------------------------

LOCK_FREE = ("resolve_target", "stage_published", "write_folders_fragment", "install_unit", "apply_release",
             "enable_unit", "verify_release", "qa_release", "status_lines", "select_release",
             "prune")     # cognita update prunes under the CLI's own lock (design 9, finding 11)


def test_no_lock_free_function_can_reach_the_lock():
    """Static: `target_lock` is not reachable from any function the CLI calls under its own lock."""
    tree = ast.parse((REPO / "scripts" / "release.py").read_text(encoding="utf-8"))
    functions = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
    assert set(LOCK_FREE) <= set(functions)
    seen: set[str] = set()
    stack = list(LOCK_FREE)
    while stack:
        name = stack.pop()
        if name in seen or name not in functions:
            continue
        seen.add(name)
        stack.extend(node.id for node in ast.walk(functions[name])
                     if isinstance(node, ast.Name) and node.id in functions)
    assert "target_lock" not in seen, sorted(seen)
    assert not {"cmd_select", "cmd_deploy", "cmd_qa", "cmd_publish"} & seen


def test_select_release_takes_no_lock_and_cmd_select_takes_exactly_one(local, tmp_path, monkeypatch, log):
    directory = release.release_dir(local, VERSION)
    directory.mkdir(parents=True)
    (directory / "release.txt").write_text(
        f"version: {VERSION}\ncommit: {COMMIT}\nmode: core\nimage_ref_cognita: cognita/app:x\n"
        f"toolbox_version: {TOOLBOX_VERSION}\n", encoding="utf-8")
    calls: list[tuple] = []
    monkeypatch.setattr(release, "image_exists", lambda ref: True)
    monkeypatch.setattr(release, "apply_release", lambda *a: calls.append(("apply", a[2].name, a[3])))
    monkeypatch.setattr(release, "verify_release", lambda *a: calls.append(("verify", a[2])))
    locks: list[Path] = []

    @contextlib.contextmanager
    def recording_lock(path, _log):
        locks.append(path)
        yield

    monkeypatch.setattr(release, "target_lock", recording_lock)
    release.select_release(local, VERSION, log)
    assert locks == [] and calls == [("apply", VERSION, TOOLBOX_VERSION), ("verify", VERSION)]
    release.cmd_select(SimpleNamespace(version=VERSION), local, log)
    assert locks == [release.target_root(local) / ".lock"]
    with pytest.raises(release.ReleaseError, match="no such release"):
        release.select_release(local, "9.9.9", log)


def test_status_lines_returns_the_lines_and_prints_nothing(local, tmp_path, monkeypatch, log, capsys):
    directory = release.release_dir(local, VERSION)
    directory.mkdir(parents=True)
    (directory / "release.txt").write_text(
        f"version: {VERSION}\ncommit: {COMMIT}\nprofile: cpu\nmode: core\nimage_ref_cognita: cognita/app:x\n"
        "image_cognita: sha256:1\n", encoding="utf-8")
    # `current` is a symlink in production; a directory answers the same questions.
    monkeypatch.setattr(release, "current_link", lambda _target: directory)
    monkeypatch.setattr(release, "image_exists", lambda ref: True)
    monkeypatch.setattr(release, "run", lambda command, **_k: (0, "active"))
    monkeypatch.setattr(release, "healthz", lambda port, **_k: {"version": VERSION, "status": "ok"})
    services: list[tuple[str, ...]] = []
    monkeypatch.setattr(release, "report_running_labels", lambda _target, _log, names: services.append(names))
    capsys.readouterr()
    lines = release.status_lines(local)
    assert capsys.readouterr().out == ""
    assert f"status: current -> {directory.resolve()}" in lines
    assert "status: mode: core" in lines
    assert "status: image cognita: cognita/app:x [present]" in lines
    assert not any("workspace-runtime" in line for line in lines)  # a core release has none
    assert services == [("cognita",)]
    assert f"status: /healthz version={VERSION} status=ok embed.gpu=None" in lines
    assert lines[-1] == f"status: staged releases: {VERSION}"
    release.cmd_status(SimpleNamespace(), local, log)
    assert "status: mode: core" in Path(log.path).read_text(encoding="utf-8")


def test_status_lines_for_an_unstaged_local_target(local, monkeypatch):
    monkeypatch.setattr(release, "run", lambda command, **_k: (3, "inactive"))
    monkeypatch.setattr(release, "healthz", lambda *_a, **_k: (_ for _ in ()).throw(
        release.ReleaseError("verify-failed", "no answer")))
    monkeypatch.setattr(release, "report_running_labels", lambda *_a: None)
    lines = release.status_lines(local)
    assert any("has no current release" in line for line in lines)
    assert lines[-1] == "status: staged releases: (none)"


# --------------------------------------------------------------------------
# 9 the reset script
# --------------------------------------------------------------------------


reset = _load("cognita_reset_local_tests", "reset_disposable_state.py")


def test_reset_protects_every_documents_root_and_the_selftest_root(tmp_path, monkeypatch):
    env_file = tmp_path / "cognita.env"
    releases = tmp_path / "releases"
    _write_env(env_file, COGNITA_RELEASES_ROOT=str(releases),
               COGNITA_CONFIG_ROOT=str(tmp_path / "config"),
               COGNITA_PROJECTS_ROOT=str(tmp_path / "docs"),
               COGNITA_PROJECTS_ROOT_2=str(tmp_path / "docs 2"),
               COGNITA_PROJECTS_ROOT_5=str(tmp_path / "docs5"),
               COGNITA_POSTGRES_DATA_ROOT=str(tmp_path / "pg"))
    monkeypatch.setenv("COGNITA_LOCAL_ENV_FILE", str(env_file))
    target = reset.release.resolve_target("local")
    log = reset.Log(tmp_path / "reset.log")
    layout = reset.layout_for(target, log)
    assert layout.projects_roots == (tmp_path / "docs", tmp_path / "docs 2", tmp_path / "docs5")
    assert layout.selftest_root == releases / "local" / "self-test"
    protected = layout.protected()
    for root in (*layout.projects_roots, layout.selftest_root):
        assert root in protected
    # A deletion root that IS or CONTAINS any documents root, or the Self-Test
    # root, is refused -- each one on its own, not only the first documents root.
    for protected_root in (*layout.projects_roots, layout.selftest_root, tmp_path):
        problems = reset.root_refusals("COGNITA_POSTGRES_DATA_ROOT", protected_root, layout, log)
        assert problems and "protected path" in problems[0], protected_root
    assert reset.root_refusals("COGNITA_POSTGRES_DATA_ROOT", tmp_path / "pg", layout, log) == []


def test_reset_table_target_layout_is_unchanged(tmp_path, monkeypatch):
    monkeypatch.setattr(reset.release, "RELEASES_ROOT", tmp_path / "releases")
    env = tmp_path / "main.env"
    env.write_text(f"COGNITA_PROJECTS_ROOT={tmp_path / 'docs'}\nCOGNITA_CONFIG_ROOT={tmp_path / 'c'}\n",
                   encoding="utf-8")
    target = dataclasses.replace(reset.release.TARGETS["test"], env_file=env)
    layout = reset.layout_for(target, reset.Log(None))
    assert layout.projects_roots == (tmp_path / "docs",)
    assert layout.selftest_root is None


def test_reset_of_a_core_release_needs_no_toolbox_archive(tmp_path, monkeypatch):
    monkeypatch.setattr(reset.release, "RELEASES_ROOT", tmp_path / "releases")
    target = reset.release.TARGETS["test"]
    current = reset.release.current_link(target)
    current.mkdir(parents=True)
    log = reset.Log(None)
    (current / "release.txt").write_text("version: 14.1.0\nmode: core\ntoolbox_version: 12.6.0\n", encoding="utf-8")
    assert reset.deployment_refusals(target, "workspaces", log) == []
    (current / "release.txt").write_text("version: 14.1.0\nmode: full\ntoolbox_version: 12.6.0\n", encoding="utf-8")
    problems = reset.deployment_refusals(target, "workspaces", log)
    assert len(problems) == 1 and "Toolbox archive is missing" in problems[0]


def test_reset_accepts_the_local_target(monkeypatch):
    args = reset.build_parser().parse_args(["--target", "local", "--scope", "index"])
    assert args.target == "local"
