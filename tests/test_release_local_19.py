"""Design 19 (docs/DESIGN-LINUX-INSTALLER.md 19.1, 19.2, 19.4, 19.6) as release.py sees it: what Windows
setup needs from the release tool.  The helpers and fakes are the ones tests/test_release_local.py uses.

Nothing here touches the network, Docker, systemd or a real clone, and nothing waits.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from test_release_local import (
    COMMIT,
    POSTGRES_REF,
    PUBLISHED,
    ROOTS,
    TOOLBOX_VERSION,
    VERSION,
    FakeDocker,
    _fragment,
    _set_env,
    _stage_dir,
    _write_env,
    release,
    reset,
)


@pytest.fixture
def log(tmp_path):
    return release.Log(tmp_path / "test.log")


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("COGNITA_LOCAL_ENV_FILE", str(tmp_path / "absent" / "cognita.env"))
    monkeypatch.setenv("COGNITA_VERSION", "unset")
    monkeypatch.setattr(release, "RELEASES_ROOT", tmp_path / "table-releases")


@pytest.fixture
def local(tmp_path, monkeypatch):
    env_file = tmp_path / "config" / "cognita.env"
    _write_env(env_file, COGNITA_RELEASES_ROOT=str(tmp_path / "local-releases"))
    monkeypatch.setenv("COGNITA_LOCAL_ENV_FILE", str(env_file))
    return release.resolve_target("local")


def _environment(text: str, service: str) -> dict:
    """The fragment parsed as YAML (PyYAML is an app dependency): the quoting proof, not string matching."""
    return yaml.safe_load(text)["services"][service]["environment"]


# --------------------------------------------------------------------------
# 19.2 the folders fragment carries displays
# --------------------------------------------------------------------------


def test_fragment_carries_a_display_only_for_the_roots_that_have_one(local, tmp_path, log):
    _set_env(local, COGNITA_PROJECTS_ROOT_DISPLAY="D:\\Documents", COGNITA_PROJECTS_ROOT_2="/mnt/nas",
             COGNITA_PROJECTS_ROOT_2_DISPLAY="\\\\nas\\docs", COGNITA_PROJECTS_ROOT_3="/mnt/plain")
    _path, text = _fragment(local, tmp_path, log)
    environment = _environment(text, "cognita")
    # The list of paths is untouched (an older image after a rollback reads it), and the displays are a
    # second variable that an image which does not know it ignores.
    assert json.loads(environment["COGNITA_DOCUMENT_ROOTS"]) == [ROOTS[0], "/mnt/nas", "/mnt/plain"]
    assert json.loads(environment["COGNITA_DOCUMENT_ROOT_DISPLAYS"]) == {
        ROOTS[0]: "D:\\Documents", "/mnt/nas": "\\\\nas\\docs"}


def test_fragment_without_any_display_has_no_displays_variable(local, tmp_path, log):
    _path, text = _fragment(local, tmp_path, log)
    assert "COGNITA_DOCUMENT_ROOT_DISPLAYS" not in text


@pytest.mark.parametrize("bad", ["D:\\has$dollar", 'D:\\has"quote'])
def test_a_display_that_would_break_compose_or_the_yaml_is_refused(local, tmp_path, log, bad):
    _set_env(local, COGNITA_PROJECTS_ROOT_DISPLAY=bad)
    with pytest.raises(release.ReleaseError, match="display text"):
        release.write_folders_fragment(local, tmp_path, log)


def test_a_display_with_a_single_quote_survives_the_yaml_quoting(local, tmp_path, log):
    _set_env(local, COGNITA_PROJECTS_ROOT_DISPLAY="D:\\User's docs")
    _path, text = _fragment(local, tmp_path, log)
    assert json.loads(_environment(text, "cognita")["COGNITA_DOCUMENT_ROOT_DISPLAYS"]) == {
        ROOTS[0]: "D:\\User's docs"}


# --------------------------------------------------------------------------
# 19.6 the command name and the target name reach the app AND the Workspace runtime
# --------------------------------------------------------------------------


def test_fragment_passes_the_command_name_and_target_to_the_app_and_the_workspace_runtime(local, tmp_path, log):
    _set_env(local, COGNITA_COMMAND="cognita")
    _path, text = _fragment(local, tmp_path, log)
    services = yaml.safe_load(text)["services"]
    for service in ("cognita", "workspace-runtime"):
        environment = services[service]["environment"]
        assert environment["COGNITA_COMMAND"] == "cognita" and environment["COGNITA_RELEASE_TARGET"] == "local"
    assert set(services["workspace-runtime"]) == {"environment"}      # nothing but the two variables


def test_fragment_defaults_the_command_name_to_the_launcher(local, tmp_path, log):
    _path, text = _fragment(local, tmp_path, log)
    assert _environment(text, "cognita")["COGNITA_COMMAND"] == "./cognita"


def test_fragment_names_the_runtime_service_only_in_a_full_release(local, tmp_path, log):
    core = release.write_folders_fragment(local, _stage_dir(tmp_path, "core", "core-release"), log)
    assert list(yaml.safe_load(core.read_text(encoding="utf-8"))["services"]) == ["cognita"]
    full = release.write_folders_fragment(local, _stage_dir(tmp_path, "full", "full-release"), log)
    assert list(yaml.safe_load(full.read_text(encoding="utf-8"))["services"]) == ["cognita", "workspace-runtime"]


def test_fragment_refuses_a_command_name_it_cannot_quote_safely(local, tmp_path, log):
    _set_env(local, COGNITA_COMMAND="cognita $HOME")
    with pytest.raises(release.ReleaseError, match="not a usable command name"):
        release.write_folders_fragment(local, tmp_path, log)


def test_the_local_hints_use_the_command_name(local, monkeypatch, tmp_path):
    assert release.command_name(release.read_env_file(local.env_file)) == "./cognita"
    _set_env(local, COGNITA_COMMAND="cognita")
    assert release._reset_all_command(local) == "cognita reset all"
    # The retry hint after a refusal names the launcher too, never the developer script.
    assert release._retry_command(local, "14.1.0") == "cognita restart"
    assert release._retry_command(release.TARGETS["test"], "14.1.0") == (
        "python3 scripts/release.py select --target test --version 14.1.0")
    # A missing env file has no name to read, so the message says the launcher's default.
    monkeypatch.setenv("COGNITA_LOCAL_ENV_FILE", str(tmp_path / "absent" / "cognita.env"))
    with pytest.raises(release.ReleaseError, match=r"Run \./cognita install to create it"):
        release.resolve_target("local")
    # kei's targets keep the script command.
    assert release._reset_all_command(release.TARGETS["test"]) == (
        "python3 scripts/reset_disposable_state.py --target test --scope all --apply")


def test_the_unit_description_names_the_command(local):
    release.current_link(local).mkdir(parents=True, exist_ok=True)
    assert "installed by ./cognita)" in release.render_unit(local, "note")
    _set_env(local, COGNITA_COMMAND="cognita")
    assert "installed by cognita)" in release.render_unit(local, "note")


def test_the_reset_script_hint_uses_the_command_name(local, capsys):
    reset_log = reset.Log(None)
    reset.report("index", local, [], reset_log)
    assert "Watch it with: ./cognita status" in capsys.readouterr().out
    _set_env(local, COGNITA_COMMAND="cognita")
    reset.report("index", local, [], reset_log)
    assert "Watch it with: cognita status" in capsys.readouterr().out
    reset.report("workspaces", release.TARGETS["test"], [], reset_log)
    assert "Status: python3 scripts/release.py status --target test" in capsys.readouterr().out


# --------------------------------------------------------------------------
# 19.1 on_image: the download progress hook
# --------------------------------------------------------------------------


def test_on_image_is_called_after_each_image_is_present_with_its_published_size(local, tmp_path, monkeypatch, log):
    docker = FakeDocker(monkeypatch, tmp_path, present=[PUBLISHED["image_ref_workspace_runtime"]])
    seen: list[tuple[str, int | None, bool]] = []

    def on_image(reference, size):
        seen.append((reference, size, reference in docker.present))       # present at the moment it is reported

    release.stage_published(local, log, on_image=on_image)
    assert seen == [
        (PUBLISHED["image_ref_cognita_cpu"], 100, True),
        (PUBLISHED["image_ref_workspace_runtime"], 300, True),            # already there: still reported
        (PUBLISHED["image_ref_toolbox"], 400, True),
        (POSTGRES_REF, None, True),                                       # not ours to publish: no size
    ]


def test_on_image_in_core_mode_skips_the_workspace_images(local, tmp_path, monkeypatch, log):
    _set_env(local, COGNITA_WORKSPACE="off")
    FakeDocker(monkeypatch, tmp_path)
    seen = []
    release.stage_published(local, log, on_image=lambda ref, size: seen.append((ref, size)))
    assert seen == [(PUBLISHED["image_ref_cognita_cpu"], 100), (POSTGRES_REF, None)]


def test_a_failing_on_image_callback_never_fails_the_staging(local, tmp_path, monkeypatch, log):
    FakeDocker(monkeypatch, tmp_path)

    def broken(reference, size):
        raise RuntimeError("progress file is on fire")

    directory = release.stage_published(local, log, on_image=broken)
    assert (directory / "release.txt").is_file()
    assert "the on_image callback failed" in (tmp_path / "test.log").read_text(encoding="utf-8")


# --------------------------------------------------------------------------
# 19.4 staging without a .git
# --------------------------------------------------------------------------


def _tree_without_git(tmp_path: Path, monkeypatch, tree_version: str) -> Path:
    tree = tmp_path / "wsl-tree"
    (tree / "src" / "cognita").mkdir(parents=True)
    (tree / "src" / "cognita" / "release_identity.py").write_text(
        f'APPLICATION_VERSION = "{tree_version}"\nTOOLBOX_VERSION = "{TOOLBOX_VERSION}"\n', encoding="utf-8")
    (tree / "compose.yaml").write_text(
        f"# tree compose.yaml\nservices:\n  postgres:\n    image: {POSTGRES_REF}\n", encoding="utf-8")
    for name in ("compose.cpu.yaml", "compose.workspace.yaml"):
        (tree / name).write_text(f"# tree {name}\nservices: {{}}\n", encoding="utf-8")
    monkeypatch.setattr(release, "REPO_ROOT", tree)
    monkeypatch.setattr(release, "repo_has_git", lambda repo: False)
    return tree


def test_staging_without_a_dot_git_reads_the_compose_files_from_the_tree(local, tmp_path, monkeypatch, log):
    docker = FakeDocker(monkeypatch, tmp_path)
    _tree_without_git(tmp_path, monkeypatch, VERSION)
    directory = release.stage_published(local, log)
    assert docker.git_reads == []                                          # git was never asked
    assert (directory / "compose.yaml").read_text(encoding="utf-8").startswith("# tree compose.yaml")
    assert (directory / "compose.workspace.yaml").read_text(encoding="utf-8") == (
        "# tree compose.workspace.yaml\nservices: {}\n")
    assert release.read_release_text(directory)["version"] == VERSION


def test_staging_without_a_dot_git_stops_when_the_tree_is_another_version(local, tmp_path, monkeypatch, log):
    docker = FakeDocker(monkeypatch, tmp_path)
    _tree_without_git(tmp_path, monkeypatch, "14.2.0")
    with pytest.raises(release.ReleaseError) as caught:
        release.stage_published(local, log)
    assert str(caught.value) == ("This Cognita tree is 14.2.0 but its published images are 14.1.0; the tree "
                                 "and the file must come from the same release.")
    assert not release.release_dir(local, VERSION).exists()               # nothing was staged
    assert docker.git_reads == []


def test_a_checkout_with_a_dot_git_still_reads_the_published_commit(local, tmp_path, monkeypatch, log):
    docker = FakeDocker(monkeypatch, tmp_path)                             # its repo_has_git says True
    release.stage_published(local, log)
    assert docker.git_reads[0] == (COMMIT, "compose.yaml")


# --------------------------------------------------------------------------
# 19.9 item 8: the .cognita-tree stamp
# --------------------------------------------------------------------------


def _stamp(tree: Path, text: str) -> None:
    (tree / ".cognita-tree").write_text(text, encoding="utf-8")


def test_a_stamp_whose_commit_equals_the_published_one_reads_the_tree_and_replaces_the_version_check(
        local, tmp_path, monkeypatch, log):
    docker = FakeDocker(monkeypatch, tmp_path)
    tree = _tree_without_git(tmp_path, monkeypatch, "14.2.0")              # another VERSION than the published 14.1.0
    _stamp(tree, f"commit: {COMMIT}\nbuilt: 2026-09-29\n")
    directory = release.stage_published(local, log)
    # The version disagrees and staging still succeeds: the stamp's commit is the stronger check and replaces it.
    assert release.read_release_text(directory)["version"] == VERSION
    assert docker.git_reads == []
    assert (directory / "compose.yaml").read_text(encoding="utf-8").startswith("# tree compose.yaml")
    text = (tmp_path / "test.log").read_text(encoding="utf-8")
    assert f".cognita-tree says this tree is commit {COMMIT}" in text
    assert "matches the published one" in text


def test_a_stamped_tree_is_read_even_when_it_has_a_dot_git(local, tmp_path, monkeypatch, log):
    docker = FakeDocker(monkeypatch, tmp_path)                             # its repo_has_git says True
    tree = _tree_without_git(tmp_path, monkeypatch, VERSION)
    monkeypatch.setattr(release, "repo_has_git", lambda repo: True)
    _stamp(tree, f"commit: {COMMIT}\n")
    release.stage_published(local, log)
    assert docker.git_reads == []                                          # the stamp decides, git is never asked


def test_a_stamp_for_another_commit_stops_the_staging_and_names_both_commits(local, tmp_path, monkeypatch, log):
    docker = FakeDocker(monkeypatch, tmp_path)
    other = "b" * 40
    tree = _tree_without_git(tmp_path, monkeypatch, VERSION)               # the version matches: still refused
    _stamp(tree, f"commit: {other}\n")
    with pytest.raises(release.ReleaseError) as caught:
        release.stage_published(local, log)
    assert str(caught.value) == (f"This Cognita tree is commit {other} but its published images are commit "
                                 f"{COMMIT}; the tree and the file must come from the same release.")
    assert not release.release_dir(local, VERSION).exists()               # nothing was staged
    assert docker.git_reads == []


@pytest.mark.parametrize("text", ["", "built: today\n", "commit: abc123\n", f"commit: {'A' * 40}\n",
                                  f"commit: {'a' * 39}\n", "commit\n"])
def test_a_stamp_without_a_full_commit_is_refused_not_ignored(local, tmp_path, monkeypatch, log, text):
    FakeDocker(monkeypatch, tmp_path)
    tree = _tree_without_git(tmp_path, monkeypatch, VERSION)               # the version check would have passed
    _stamp(tree, text)
    with pytest.raises(release.ReleaseError, match="no usable `commit:` line"):
        release.stage_published(local, log)
    assert not release.release_dir(local, VERSION).exists()


def test_no_stamp_and_no_dot_git_still_uses_the_version_check(local, tmp_path, monkeypatch, log):
    docker = FakeDocker(monkeypatch, tmp_path)
    tree = _tree_without_git(tmp_path, monkeypatch, VERSION)
    assert release.read_tree_stamp(tree, log) is None
    release.stage_published(local, log)                                    # version matches: staged from the tree
    assert docker.git_reads == []
    _tree_without_git(tmp_path / "other", monkeypatch, "14.2.0")           # a second tree: another version, no stamp
    with pytest.raises(release.ReleaseError, match="This Cognita tree is 14.2.0 but its published images are 14.1.0"):
        release.stage_published(local, log)


def test_the_stamp_reader_takes_extra_keys_and_the_first_of_a_repeated_key(tmp_path, log):
    _stamp(tmp_path, f"built:  2026-09-29T10:00:00  \ncommit:{COMMIT}\ncommit: {'b' * 40}\nno separator here\n")
    assert release.read_tree_stamp(tmp_path, log) == {"built": "2026-09-29T10:00:00", "commit": COMMIT}


def test_repo_has_git_sees_a_directory_and_a_worktree_file(tmp_path):
    assert release.repo_has_git(tmp_path) is False
    (tmp_path / ".git").mkdir()
    assert release.repo_has_git(tmp_path) is True
    other = tmp_path / "worktree"
    other.mkdir()
    (other / ".git").write_text("gitdir: /elsewhere\n", encoding="utf-8")
    assert release.repo_has_git(other) is True


# --------------------------------------------------------------------------
# 19.3: the Windows setup chat's WSL image recipe, run by publish
# --------------------------------------------------------------------------


def _published_file(tmp_path):
    path = tmp_path / "published-release.txt"
    path.write_text(f"version: {VERSION}\ncommit: {COMMIT}\n", encoding="utf-8")
    return path


def test_publish_skips_the_wsl_step_when_there_is_no_recipe(tmp_path, log, monkeypatch):
    monkeypatch.setattr(release, "_capture", lambda *a, **k: pytest.fail("no recipe, nothing may run"))
    published = _published_file(tmp_path)
    assert release.build_wsl_artifacts(tmp_path, release.TARGETS["test"], VERSION, COMMIT, published, log) == []
    assert "wsl" not in published.read_text(encoding="utf-8")
    assert "no WSL image recipe" in log.path.read_text(encoding="utf-8")


def test_publish_runs_the_recipe_after_the_file_exists_and_appends_its_four_keys(tmp_path, log, monkeypatch):
    repo = tmp_path / "repo"
    (repo / "containers" / "wsl").mkdir(parents=True)
    (repo / "containers" / "wsl" / "build-wsl-image.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    published = _published_file(tmp_path)
    seen = []

    def capture(command, **_k):
        seen.append(command)
        assert published.is_file()          # the image carries this file, so it must exist first
        return (f"file=cognita-wsl-{VERSION}.tar.gz sha256={'a' * 64} bytes=585000000\n"
                f"file=cognita-src-{VERSION}.tar.gz sha256={'b' * 64} bytes=95000000\n")

    monkeypatch.setattr(release, "_capture", capture)
    files = release.build_wsl_artifacts(repo, release.TARGETS["test"], VERSION, COMMIT, published, log)
    command = seen[0]
    assert command[1].endswith("build-wsl-image.sh")
    assert command[command.index("--commit") + 1] == COMMIT
    assert command[command.index("--published-release") + 1] == str(published)
    values = dict(line.split(": ", 1) for line in published.read_text(encoding="utf-8").splitlines())
    assert values["wsl_image_sha256"] == "a" * 64 and values["size_wsl_image"] == "585000000"
    assert values["src_tarball_sha256"] == "b" * 64 and values["size_src_tarball"] == "95000000"
    assert [p.name for p in files] == [f"cognita-wsl-{VERSION}.tar.gz", f"cognita-src-{VERSION}.tar.gz"]
    assert "upload it to the GitHub release" in log.path.read_text(encoding="utf-8")


def test_a_recipe_that_reports_only_one_file_is_refused_and_writes_no_keys(tmp_path, log, monkeypatch):
    repo = tmp_path / "repo"
    (repo / "containers" / "wsl").mkdir(parents=True)
    (repo / "containers" / "wsl" / "build-wsl-image.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    published = _published_file(tmp_path)
    monkeypatch.setattr(release, "_capture",
                        lambda *a, **k: f"file=cognita-wsl-{VERSION}.tar.gz sha256={'a' * 64} bytes=1\n")
    with pytest.raises(release.ReleaseError, match="did not report both files"):
        release.build_wsl_artifacts(repo, release.TARGETS["test"], VERSION, COMMIT, published, log)
    assert "wsl_image_sha256" not in published.read_text(encoding="utf-8")
