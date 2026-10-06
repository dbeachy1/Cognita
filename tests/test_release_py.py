"""Unit tests for scripts/release.py (DESIGN-13.0 sections 6 and 7).

They cover the decisions the release tool makes on its own: the target table,
the lock, staging atomicity, the symlink switch, the generated Compose and unit
text, and every refusal with its exit code.  Docker, systemd and kei are not
involved; what needs a real daemon is proven on kei instead, and the acceptance
run for this slice is recorded in the coder's report.
"""
from __future__ import annotations

import dataclasses
import contextlib
import builtins
import importlib.util
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO = Path(__file__).resolve().parents[1]


def _load_release_module():
    """Load scripts/release.py by path: it is a script, not a package module."""
    spec = importlib.util.spec_from_file_location("cognita_release_tool", REPO / "scripts" / "release.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    # @dataclass resolves annotations through sys.modules, so the module has to
    # be registered before it is executed.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


release = _load_release_module()


@pytest.fixture
def releases_root(tmp_path, monkeypatch):
    root = tmp_path / "releases"
    monkeypatch.setattr(release, "RELEASES_ROOT", root)
    return root


@pytest.fixture(autouse=True)
def no_local_env_file(monkeypatch, tmp_path):
    """`other_release_uses_tag` scans the `local` target when its env file exists
    (DESIGN-LINUX-INSTALLER 5.1); no table-target test may see a real one."""
    monkeypatch.setenv("COGNITA_LOCAL_ENV_FILE", str(tmp_path / "no-local" / "cognita.env"))


@pytest.fixture
def log(tmp_path):
    return release.Log(tmp_path / "test.log")


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True, timeout=60)


@pytest.fixture
def clean_repo(tmp_path):
    """A throwaway Git checkout with the two identity files release.py reads."""
    repo = tmp_path / "checkout"
    (repo / "src" / "cognita").mkdir(parents=True)
    (repo / "src" / "cognita" / "__init__.py").write_text('__version__ = "13.0.0"\n', encoding="utf-8")
    (repo / "src" / "cognita" / "release_identity.py").write_text(
        'APPLICATION_VERSION = "13.0.0"\nTOOLBOX_VERSION = "12.6.0"\n', encoding="utf-8")
    (repo / "compose.yaml").write_text("services: {}\n", encoding="utf-8")
    (repo / "compose.cpu.yaml").write_text("services: {}\n", encoding="utf-8")
    (repo / "compose.amd.yaml").write_text("services: {}\n", encoding="utf-8")
    (repo / "compose.workspace.yaml").write_text("services: {}\n", encoding="utf-8")
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "Release Test")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "fixture")
    return repo


# --------------------------------------------------------------------------
# Target table (section 6.1)
# --------------------------------------------------------------------------


def test_target_table_rows_match_the_design():
    main = release.TARGETS["main"]
    assert (main.profile, main.unit, main.project, main.mcp_port, main.admin_port) == (
        "amd", "cognita-compose-main.service", "cognita-main", 8675, 8676)
    assert main.env_file.name == "cognita-main-amd.env"
    beta = release.TARGETS["beta"]
    assert (beta.profile, beta.unit, beta.project, beta.mcp_port, beta.admin_port) == (
        "amd", "cognita-compose-amd.service", "cognita-12", 9675, 9676)
    assert beta.env_file.name == "cognita-amd.env"
    acceptance = release.TARGETS["test"]
    assert (acceptance.profile, acceptance.unit, acceptance.project,
            acceptance.mcp_port, acceptance.admin_port) == (
        "amd", "cognita-compose-test.service", "cognita-13test", 8775, 8776)
    # The live self-test hits the real connector; both 12.x instances serve it
    # as `cognita`, and the throwaway/test target generates `self-test`.
    assert (main.connector, beta.connector, acceptance.connector) == ("cognita", "cognita", "self-test")
    assert release.TEST_SLUG == acceptance.connector
    # No two targets may share a port, a project or a unit: that is how a run
    # against one instance would restart another.
    assert len({t.project for t in release.TARGETS.values()}) == len(release.TARGETS)
    assert len({t.unit for t in release.TARGETS.values()}) == len(release.TARGETS)
    ports = [p for t in release.TARGETS.values() for p in (t.mcp_port, t.admin_port)]
    assert len(set(ports)) == len(ports)


def test_compose_file_sets_follow_the_profile(tmp_path):
    amd = release.checkout_compose_files(tmp_path, "amd", "full")
    assert [p.name for p in amd] == ["compose.yaml", "compose.amd.yaml", "compose.workspace.yaml"]
    assert [p.name for p in release.checkout_compose_files(tmp_path, "cpu", "core")] == [
        "compose.yaml", "compose.cpu.yaml"]
    (tmp_path / "compose.workspace.yaml").write_text("services: {}\n", encoding="utf-8")
    staged = release.staged_compose_files(tmp_path, "amd")
    assert [p.name for p in staged] == ["compose.yaml", "compose.amd.yaml", "compose.workspace.yaml",
                                        "compose.images.yaml"]
    # compose.images.yaml is last so its pinned IDs win over the base files.
    assert staged[-1].name == "compose.images.yaml"


def test_a_release_staged_before_the_workspace_overlay_is_started_without_it(tmp_path):
    """Before 13.5.0 the Workspace runtime lived in compose.yaml; `select` back
    to such a release must not name an overlay file it does not have."""
    old = tmp_path / "13.4.0"
    old.mkdir()
    assert [p.name for p in release.staged_compose_files(old, "amd")] == [
        "compose.yaml", "compose.amd.yaml", "compose.images.yaml"]
    # A path that does not exist yet (the unit rendered before any release is
    # applied) still names the full set.
    assert "compose.workspace.yaml" in [
        p.name for p in release.staged_compose_files(tmp_path / "current", "amd")]


def test_apply_rewrites_a_unit_whose_compose_files_are_stale(tmp_path, monkeypatch, releases_root, log):
    """2026-09-28, kei main 13.4.0 -> 13.7.0: the unit installed at 13.0 did
    not name compose.workspace.yaml, so the new stack started with the app in
    core mode and the Workspace runtime dead.  apply must bring the unit in
    line with the release it starts, after the stop and before the start."""
    target = release.TARGETS["main"]
    systemd = tmp_path / "systemd"
    monkeypatch.setattr(release, "SYSTEMD_USER_DIR", systemd)
    directory = releases_root / "main" / "13.7.0"
    directory.mkdir(parents=True)
    (directory / "compose.workspace.yaml").write_text("services: {}\n", encoding="utf-8")
    (releases_root / "main" / "toolbox").mkdir()
    (releases_root / "main" / "toolbox" / "toolbox-12.6.0.tar").write_bytes(b"")
    monkeypatch.setattr(release, "current_link", lambda _target: directory)
    monkeypatch.setattr(release, "point_current", lambda *_args: None)
    monkeypatch.setattr(release, "warn_on_live_traffic", lambda *_args: None)
    monkeypatch.setattr(release, "toolbox_cache_root", lambda _env: tmp_path / "cache")
    events: list[str] = []
    monkeypatch.setattr(release, "load_toolbox", lambda **_kwargs: events.append("toolbox"))

    stale = release.render_unit(target, version_note="release.py-managed").replace(
        f" -f {directory / 'compose.workspace.yaml'}", "")
    assert stale != release.render_unit(target, version_note="release.py-managed")
    systemd.mkdir()
    (systemd / target.unit).write_text(stale, encoding="utf-8")

    def fake_run(command, **_kwargs):
        if command[:3] == ["systemctl", "--user", "is-active"]:
            return (3, "inactive")
        if command[:2] == ["systemctl", "--user"] and command[2] in {"stop", "start"}:
            unit_text = (systemd / target.unit).read_text(encoding="utf-8")
            events.append(f"{command[2]}:{'workspace' if 'compose.workspace.yaml' in unit_text else 'core'}")
        return (0, "")

    monkeypatch.setattr(release, "run", fake_run)

    release.apply_release(tmp_path, target, directory, "12.6.0", log)

    # Stopped with the unit that started the old stack; started with the new one.
    assert events == ["stop:core", "toolbox", "start:workspace"]
    saved = list((releases_root / "main" / "units").glob(f"{target.unit}.*"))
    assert len(saved) == 1 and saved[0].read_text(encoding="utf-8") == stale


def test_compose_command_carries_project_env_and_every_file(tmp_path):
    command = release.compose_command(
        project="cognita-13test", env_file=tmp_path / "e.env",
        files=[tmp_path / "compose.yaml"], extra_files=[tmp_path / "over.yaml"])
    assert command[:4] == ["docker", "compose", "-p", "cognita-13test"]
    assert "--env-file" in command
    assert command.count("-f") == 2


# --------------------------------------------------------------------------
# The lock (section 3)
# --------------------------------------------------------------------------


def test_target_lock_excludes_a_second_holder(releases_root, log):
    path = releases_root / "test" / ".lock"
    with release.target_lock(path, log):
        assert path.is_file()
        handle = path.open("a+", encoding="utf-8")
        try:
            with pytest.raises(OSError):
                release._lock_take(handle, blocking=False)
        finally:
            handle.close()
    # Released: the same non-blocking take now succeeds.
    handle = path.open("a+", encoding="utf-8")
    try:
        release._lock_take(handle, blocking=False)
        release._lock_release(handle)
    finally:
        handle.close()


def test_target_lock_creates_the_target_directory(releases_root, log):
    path = releases_root / "test" / ".lock"
    assert not path.parent.exists()
    with release.target_lock(path, log):
        pass
    assert path.parent.is_dir()


# --------------------------------------------------------------------------
# Generated files
# --------------------------------------------------------------------------


def test_release_tags_name_the_version_and_the_commit():
    tags = release.release_tags("13.0.0", "abcdef0123456789")
    assert tags == {
        "cognita": "cognita/app:13.0.0-abcdef012345",
        "workspace-runtime": "cognita/workspace-runtime:13.0.0-abcdef012345",
    }
    # A different commit at the same version is a different tag, so two
    # releases can never share one.
    assert release.release_tags("13.0.0", "ffffffffffff")["cognita"] != tags["cognita"]


def test_staged_tags_separate_targets_and_profiles_with_legacy_fallback():
    main = release.TARGETS["main"]
    beta = release.TARGETS["beta"]
    cpu = dataclasses.replace(main, profile="cpu")
    tags = [release.release_tags("13.3.0", "a" * 40, target)["cognita"]
            for target in (main, beta, cpu)]
    assert len(set(tags)) == 3
    legacy = release.release_tags("13.2.11", "a" * 40)
    assert release.recorded_release_tags({"version": "13.2.11", "commit": "a" * 40}) == legacy
    assert release.recorded_release_tags({
        "version": "13.3.0", "commit": "a" * 40,
        "image_ref_cognita": tags[0],
        "image_ref_workspace_runtime": release.release_tags("13.3.0", "a" * 40, main)["workspace-runtime"],
    })["cognita"] == tags[0]
    with pytest.raises(release.ReleaseError, match="incomplete image references"):
        release.recorded_release_tags({"version": "13.3.0", "commit": "a" * 40,
                                       "image_ref_cognita": tags[0]})


def test_compose_images_references_the_release_tag_not_the_id():
    """A tagged image cannot be orphaned; an ID can.

    Incident, 2026-09-22: a rebuild at the same version gave the image a new
    ID, the old ID survived only while containers referenced it, and a reset
    that stopped those containers left the staged release pointing at
    `sha256:64e92e57…` that the daemon had dropped.
    """
    tags = release.release_tags("13.0.0", "abcdef0123456789")
    text = release.compose_images_text("13.0.0", "abcdef0123456789", tags)
    assert "image: cognita/app:13.0.0-abcdef012345" in text
    assert "image: cognita/workspace-runtime:13.0.0-abcdef012345" in text
    assert "sha256:" not in text
    assert text.count("build: !reset null") == 2
    # Nothing else: no postgres row (it is digest-pinned in compose.yaml), no
    # environment, no devices.
    assert "postgres" not in text
    assert "devices" not in text
    assert "13.0.0" in text and "abcdef0123456789" in text


def test_build_override_only_names_the_release_images():
    images = release.build_references(release.TARGETS["test"], "13.0.0", "abc123", "run1")
    text = release.build_override_text(images)
    assert f"image: {images['cognita']}" in text
    assert f"image: {images['workspace-runtime']}" in text
    # The stage belongs in compose.yaml, where a hand-run `docker compose
    # build` sees it too.
    assert "target:" not in text


def test_build_references_separate_targets_profiles_commits_and_invocations():
    main = release.TARGETS["main"]
    beta = release.TARGETS["beta"]
    cpu = dataclasses.replace(main, profile="cpu")
    refs = [release.build_references(target, "13.3.0", commit, run)
            for target, commit, run in ((main, "a" * 40, "run1"),
                                        (beta, "a" * 40, "run1"),
                                        (cpu, "a" * 40, "run1"),
                                        (main, "b" * 40, "run1"),
                                        (main, "a" * 40, "run2"))]
    for service in ("cognita", "workspace-runtime"):
        assert len({mapping[service] for mapping in refs}) == len(refs)


def test_interleaved_target_builds_stage_their_own_images(
    clean_repo, releases_root, tmp_path, log, monkeypatch,
):
    commits = ((release.TARGETS["main"], "a" * 40),
               (release.TARGETS["beta"], "b" * 40),
               (dataclasses.replace(release.TARGETS["test"], profile="cpu"), "a" * 40))
    image_ids = {}
    monkeypatch.setattr(release, "validate_cpu_ocr_inputs", lambda *_args: {})
    monkeypatch.setattr(release, "target_models_root", lambda env_file: tmp_path / "models")
    monkeypatch.setattr(release, "fetch_ocr_weights", lambda models_root, log: models_root / "easyocr")
    monkeypatch.setattr(release, "cpu_ocr_smoke", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(release, "stage_microsandbox_wheel", lambda *a: None)
    monkeypatch.setattr(release, "image_id", lambda reference: image_ids[reference])

    def fake_run(command, **kwargs):
        override = next((Path(command[i + 1]) for i, part in enumerate(command[:-1])
                         if part == "-f" and command[i + 1].endswith("compose.build.yaml")), None)
        assert override is not None
        text = override.read_text(encoding="utf-8")
        for target, commit in commits:
            refs = release.build_references(target, "13.3.0", commit, target.name)
            if refs["cognita"] in text:
                image_ids.update({service_ref: f"sha256:{target.name}-{service}"
                                  for service, service_ref in refs.items()})
                return 0, ""
        raise AssertionError("build used an unowned image reference")

    monkeypatch.setattr(release, "run", fake_run)
    built = []
    for target, commit in commits:
        root = tmp_path / target.name
        root.mkdir()
        refs, ids = release.build_images(clean_repo, target, "13.3.0", commit, root, log,
                                         run_id=target.name)
        built.append((target, commit, refs, ids))
    assert built[0][3]["cognita"] != built[1][3]["cognita"]
    for target, commit, refs, ids in built:
        directory = release.stage_release(clean_repo, target, "13.3.0", commit, ids,
                                          "12.6.0", False, log)
        values = release.read_release_text(directory)
        assert values["image_cognita"] == ids["cognita"]
        assert refs["cognita"] in image_ids
        assert values["image_ref_cognita"] in (directory / "compose.images.yaml").read_text()

    # Drive the actual isolated-stack path with synthetic file and command
    # operations: each test Compose file must consume its own build mapping.
    archive = tmp_path / "toolbox" / "toolbox-12.6.0.tar"
    archive.parent.mkdir()
    archive.write_text("synthetic archive\n", encoding="utf-8")
    monkeypatch.setattr(release, "toolbox_dir", lambda target: archive.parent)
    monkeypatch.setattr(release, "read_toolbox_version", lambda *a: "12.6.0")
    monkeypatch.setattr(release, "target_models_root", lambda env: tmp_path / "models")
    monkeypatch.setattr(release, "toolbox_cache_root", lambda env: tmp_path / "cache")
    monkeypatch.setattr(release, "free_port", lambda: 8765)
    monkeypatch.setattr(release, "load_toolbox", lambda **kwargs: None)
    monkeypatch.setattr(release, "image_exists", lambda _reference: False)
    monkeypatch.setattr(release, "self_test_key", lambda repo: "synthetic")
    monkeypatch.setattr(release, "run_live_selftest", lambda **kwargs:
                        {"schema": 1, "mode": kwargs["mode"], "result": "passed", "mandatory_ocr": "passed",
                         "missing_file_parity": "passed", "canonical_log": "synthetic-http.log"})
    monkeypatch.setattr(release, "_report_residue", lambda *a: True)
    monkeypatch.setattr(release.tempfile, "gettempdir", lambda: str(tmp_path))
    run_ids = iter(("1" * 32, "2" * 32, "3" * 32))
    monkeypatch.setattr(release.uuid, "uuid4", lambda: SimpleNamespace(hex=next(run_ids)))
    monkeypatch.setattr(release, "write_installation", lambda root, **kwargs:
                        SimpleNamespace(env_path=str(root / "test.env"), postgres_password="synthetic"))
    consumed = []
    commands = []

    def test_run(command, **kwargs):
        commands.append(command)
        if "up" in command:
            image_file = next(Path(command[i + 1]) for i, part in enumerate(command[:-1])
                              if part == "-f" and command[i + 1].endswith("compose.images.yaml"))
            consumed.append(image_file.read_text(encoding="utf-8"))
        if "--entrypoint" in command:
            return 0, "1 passed"
        return 0, ""

    monkeypatch.setattr(release, "run", test_run)
    for target, commit, refs, _ids in built:
        receipt = release.run_test_stack(clean_repo, target, "13.3.0", commit, refs, log)
        assert receipt["canonical_log"] == str(log.path.resolve())
        assert receipt["http_log"] == "synthetic-http.log"
        assert receipt["cleanup"] == "verified"
    assert len(consumed) == len(built)
    one_off_runs = [command for command in commands if "run" in command]
    stack_starts = [command for command in commands if "up" in command]
    assert len(one_off_runs) == len(built)
    assert len(stack_starts) == len(built)
    for command in one_off_runs:
        assert "--no-build" not in command
        assert command[command.index("run") + 1:command.index("run") + 7] == [
            "--pull", "never", "--rm", "--no-deps", "--entrypoint", "sh"
        ]
    for command in stack_starts:
        assert "--no-build" in command
        up_index = command.index("up")
        assert command[up_index + 1:up_index + 6] == [
            "-d", "--no-build", "--pull", "never", "--wait"
        ]
    for text, (_target, _commit, refs, ids) in zip(consumed, built, strict=True):
        assert refs["cognita"] in text and refs["workspace-runtime"] in text
        assert ids["cognita"] == image_ids[refs["cognita"]]
        for other_target, other_commit, other_refs, _other_ids in built:
            if other_refs is not refs:
                assert other_refs["cognita"] not in text


def test_load_toolbox_one_off_runs_omit_no_build_and_keep_pull_policy(tmp_path, log, monkeypatch):
    archive = tmp_path / "toolbox-12.6.0.tar"
    archive.write_text("synthetic archive\n", encoding="utf-8")
    commands = []

    def fake_run(command, **kwargs):
        commands.append(command)
        if command[-3] == "verify":
            return 1, "not imported"
        return 0, ""

    monkeypatch.setattr(release, "run", fake_run)
    release.load_toolbox(
        compose=["docker", "compose", "-p", "test-project"], repo=tmp_path,
        cache_root=tmp_path / "cache", archive=archive, toolbox_version="12.6.0", log=log,
    )

    assert [command[command.index("run")] for command in commands] == ["run"] * 3
    for command in commands:
        run_index = command.index("run")
        assert command[run_index + 1:run_index + 5] == ["--pull", "never", "--rm", "--no-deps"]
        assert "--no-build" not in command


def test_invocation_tags_retire_only_after_test_and_release_retention_proof(
    clean_repo, releases_root, tmp_path, log, monkeypatch,
):
    target, directory = _stage(clean_repo, releases_root, log, commit="a" * 40)
    refs = release.build_references(target, "13.0.0", "a" * 40, "run123")
    retained = release.release_tags("13.0.0", "a" * 40, target)
    ids = {"cognita": "sha256:1", "workspace-runtime": "sha256:2"}
    lookup = {**dict(zip(refs.values(), ids.values(), strict=True)),
              **dict(zip(retained.values(), ids.values(), strict=True))}
    monkeypatch.setattr(release, "image_id", lambda ref: lookup[ref])
    monkeypatch.setattr(release, "_report_residue", lambda *a: True)
    monkeypatch.setattr(release.tempfile, "gettempdir", lambda: str(tmp_path))
    removed = []
    monkeypatch.setattr(release, "run", lambda command, **kwargs:
                        (removed.append(command[-1]) or 0, ""))
    release.retire_build_references(
        refs, target=target, version="13.0.0", commit="a" * 40, built_ids=ids,
        staged_directory=directory, stage_attempted=True, test_started=True,
        run_id="run123", log=log)
    assert set(removed) == set(refs.values())
    assert not set(removed) & set(retained.values())


@pytest.mark.parametrize("failed_proof", ["test-residue", "test-root", "staged-id", "stage-uncertain"])
def test_invocation_tags_are_retained_when_cleanup_proof_fails(
    clean_repo, releases_root, tmp_path, log, monkeypatch, failed_proof,
):
    target, directory = _stage(clean_repo, releases_root, log, commit="a" * 40)
    refs = release.build_references(target, "13.0.0", "a" * 40, "run123")
    ids = {"cognita": "sha256:1", "workspace-runtime": "sha256:2"}
    retained = release.release_tags("13.0.0", "a" * 40, target)
    lookup = {**dict(zip(refs.values(), ids.values(), strict=True)),
              **dict(zip(retained.values(), ids.values(), strict=True))}
    if failed_proof == "staged-id":
        lookup[retained["cognita"]] = "sha256:wrong"
    monkeypatch.setattr(release, "image_id", lambda ref: lookup[ref])
    monkeypatch.setattr(release, "_report_residue", lambda *a: failed_proof != "test-residue")
    monkeypatch.setattr(release.tempfile, "gettempdir", lambda: str(tmp_path))
    if failed_proof == "test-root":
        (tmp_path / "cognita-test-run123").mkdir()
    removed = []
    monkeypatch.setattr(release, "run", lambda command, **kwargs:
                        (removed.append(command[-1]) or 0, ""))
    release.retire_build_references(
        refs, target=target, version="13.0.0", commit="a" * 40, built_ids=ids,
        staged_directory=None if failed_proof == "stage-uncertain" else directory,
        stage_attempted=True, test_started=failed_proof in {"test-residue", "test-root"},
        run_id="run123", log=log)
    assert removed == []
    for ref in refs.values():
        assert ref in log.path.read_text(encoding="utf-8")


def test_partial_build_failure_retires_only_inspectable_invocation_tag(
    releases_root, log, monkeypatch,
):
    target = release.TARGETS["test"]
    commit = "a" * 40
    refs = release.build_references(target, "13.0.0", commit, "run123")

    def inspect(ref):
        if ref == refs["workspace-runtime"]:
            raise release.ReleaseError("build-failed", "not built")
        return "sha256:1"

    monkeypatch.setattr(release, "image_id", inspect)
    removed = []
    monkeypatch.setattr(release, "run", lambda command, **kwargs:
                        (removed.append(command[-1]) or 0, ""))
    release.retire_build_references(
        refs, target=target, version="13.0.0", commit=commit, built_ids=None,
        staged_directory=None, stage_attempted=False, test_started=False,
        run_id="run123", log=log)
    assert removed == [refs["cognita"]]
    assert refs["workspace-runtime"] in log.path.read_text(encoding="utf-8")


def test_failed_tag_removal_retains_and_reports_exact_reference(releases_root, log, monkeypatch):
    target = release.TARGETS["test"]
    commit = "a" * 40
    refs = release.build_references(target, "13.0.0", commit, "run123")
    ids = {"cognita": "sha256:1", "workspace-runtime": "sha256:2"}
    monkeypatch.setattr(release, "image_id", lambda ref: ids[next(
        service for service, known_ref in refs.items() if known_ref == ref)])
    monkeypatch.setattr(release, "run", lambda command, **kwargs: (1, "image in use"))
    release.retire_build_references(
        refs, target=target, version="13.0.0", commit=commit, built_ids=ids,
        staged_directory=None, stage_attempted=False, test_started=False,
        run_id="run123", log=log)
    text = log.path.read_text(encoding="utf-8")
    for ref in refs.values():
        assert f"retaining invocation tag {ref}: docker image rm exited 1" in text


def test_standalone_build_leaves_a_named_usable_result(releases_root, log, monkeypatch):
    target = release.TARGETS["test"]
    commit = "a" * 40
    refs = release.build_references(target, "13.0.0", commit)
    monkeypatch.setattr(release, "require_clean_checkout", lambda *a: commit)
    monkeypatch.setattr(release, "read_version", lambda *a: "13.0.0")
    monkeypatch.setattr(release, "read_toolbox_version", lambda *a: "12.6.0")
    monkeypatch.setattr(release, "doctor", lambda *a: None)
    monkeypatch.setattr(release, "target_lock", lambda *a: contextlib.nullcontext())
    monkeypatch.setattr(release, "build_images", lambda *a, **k:
                        (refs, {"cognita": "sha256:1", "workspace-runtime": "sha256:2"}))
    monkeypatch.setattr(release, "build_toolbox", lambda *a: None)
    monkeypatch.setattr(release, "run", lambda *a, **k:
                        pytest.fail("standalone build must not remove its usable image tags"))
    release.cmd_build(SimpleNamespace(), target, log)
    text = log.path.read_text(encoding="utf-8")
    assert "until explicitly removed" in text
    assert all(ref in text for ref in refs.values())


def test_deploy_preserves_original_partial_build_failure_during_cleanup(
    releases_root, log, monkeypatch, tmp_path,
):
    target = release.TARGETS["test"]
    commit = "a" * 40
    run_id = "b" * 12
    refs = release.build_references(target, "13.0.0", commit, run_id)
    monkeypatch.setattr(release, "require_clean_checkout", lambda *a: commit)
    monkeypatch.setattr(release, "read_version", lambda *a: "13.0.0")
    monkeypatch.setattr(release, "read_toolbox_version", lambda *a: "12.6.0")
    monkeypatch.setattr(release, "doctor", lambda *a: None)
    monkeypatch.setattr(release, "check_version_free", lambda *a: None)
    monkeypatch.setattr(release, "target_lock", lambda *a: contextlib.nullcontext())
    monkeypatch.setattr(release, "target_models_root", lambda env_file: tmp_path / "models")
    monkeypatch.setattr(release, "fetch_ocr_weights", lambda models_root, log: models_root / "easyocr")
    monkeypatch.setattr(release.uuid, "uuid4", lambda: SimpleNamespace(hex=run_id))
    monkeypatch.setattr(release, "build_images", lambda *a, **k:
                        (_ for _ in ()).throw(release.ReleaseError("build-failed", "original build error")))
    monkeypatch.setattr(release, "image_id", lambda ref: "sha256:1")
    removed = []
    monkeypatch.setattr(release, "run", lambda command, **kwargs:
                        (removed.append(command[-1]) or 0, ""))
    with pytest.raises(release.ReleaseError, match="original build error") as failure:
        release.cmd_deploy(SimpleNamespace(test=False, no_build=False, connector=None), target, log)
    assert failure.value.state == "build-failed"
    assert set(removed) == set(refs.values())


def _deploy_rig(monkeypatch, tmp_path, target, events, *, fetch_error=None):
    """The seams cmd_deploy crosses, recording their order; the real fetch_ocr_weights is replaced by a fake."""
    commit = "a" * 40
    monkeypatch.setattr(release, "require_clean_checkout", lambda *a: commit)
    monkeypatch.setattr(release, "read_version", lambda *a: "14.2.0")
    monkeypatch.setattr(release, "read_toolbox_version", lambda *a: "12.6.0")
    monkeypatch.setattr(release, "export_version", lambda *a: None)
    monkeypatch.setattr(release, "doctor", lambda *a: events.append("doctor"))
    monkeypatch.setattr(release, "check_version_free", lambda *a: None)
    monkeypatch.setattr(release, "target_lock", lambda *a: contextlib.nullcontext())
    monkeypatch.setattr(release, "target_models_root", lambda env_file: tmp_path / "models")

    def fetch(models_root, log):
        events.append(("fetch", models_root))
        if fetch_error is not None:
            raise fetch_error
        return models_root / "easyocr"

    monkeypatch.setattr(release, "fetch_ocr_weights", fetch)
    monkeypatch.setattr(release, "build_images", lambda *a, **k: (
        events.append("build") or {"cognita": "c", "workspace-runtime": "w"}, {"cognita": "sha256:1", "workspace-runtime": "sha256:2"}))
    monkeypatch.setattr(release, "build_toolbox", lambda *a: events.append("toolbox"))
    monkeypatch.setattr(release, "stage_release", lambda *a, **k: events.append("stage") or tmp_path)
    monkeypatch.setattr(release, "apply_release", lambda *a: events.append("apply"))
    monkeypatch.setattr(release, "verify_release", lambda *a: events.append("verify"))
    monkeypatch.setattr(release, "retire_build_references", lambda *a, **k: events.append("retire"))
    return commit


def test_deploy_fetches_the_ocr_weights_into_the_targets_model_cache_before_it_applies(
    releases_root, log, monkeypatch, tmp_path,
):
    """14.2.0 (design 5.8): kei keeps OCR through the change because a deploy puts the weights in the cache first."""
    events: list = []
    target = release.TARGETS["main"]
    _deploy_rig(monkeypatch, tmp_path, target, events)
    release.cmd_deploy(SimpleNamespace(test=False, no_build=False, connector=None), target, log)
    assert ("fetch", tmp_path / "models") in events
    assert events.index(("fetch", tmp_path / "models")) < events.index("apply")
    assert events.index(("fetch", tmp_path / "models")) < events.index("build")
    assert events.count(("fetch", tmp_path / "models")) == 1


def test_the_no_build_beta_deploy_also_fetches_the_weights_before_it_applies(
    releases_root, log, monkeypatch, tmp_path,
):
    events: list = []
    target = release.TARGETS["beta"]
    commit = _deploy_rig(monkeypatch, tmp_path, target, events)
    monkeypatch.setattr(release, "candidate_manifest", lambda path: {"commit": commit})
    monkeypatch.setattr(release.subprocess, "run",
                        lambda *a, **k: SimpleNamespace(returncode=0, stdout="tree\n", stderr=""))
    monkeypatch.setattr(release, "validate_candidate", lambda *a, **k: (
        {"cognita": "c", "workspace-runtime": "w"}, "runner", {"toolbox_version": "12.6.0"}))
    monkeypatch.setattr(release, "image_id", lambda ref: "sha256:" + ref)
    monkeypatch.setattr(release, "stage_candidate_toolbox", lambda *a: events.append("toolbox"))
    monkeypatch.setattr(release, "run_test_stack", lambda *a, **k: events.append("test-stack"))
    release.cmd_deploy(SimpleNamespace(test=False, no_build=True, connector=None, images=tmp_path / "images",
                                       profile="amd", mode="full"), target, log)
    fetch = ("fetch", tmp_path / "models")
    assert events.count(fetch) == 1
    assert events.index(fetch) < events.index("test-stack") < events.index("apply")


def test_a_refused_deploy_downloads_nothing(releases_root, log, monkeypatch, tmp_path):
    # Version reuse (and a --no-build usage error) refuse before the 98 MB fetch, not after it.
    events: list = []
    target = release.TARGETS["main"]
    _deploy_rig(monkeypatch, tmp_path, target, events)

    def refuse(*a):
        raise release.ReleaseError("version-exists", "14.2.0 exists with another commit")

    monkeypatch.setattr(release, "check_version_free", refuse)
    with pytest.raises(release.ReleaseError, match="exists"):
        release.cmd_deploy(SimpleNamespace(test=False, no_build=False, connector=None), target, log)
    with pytest.raises(release.ReleaseError, match="supported only for Beta"):
        release.cmd_deploy(SimpleNamespace(test=False, no_build=True, connector=None, images=None), target, log)
    assert not [e for e in events if isinstance(e, tuple) and e[0] == "fetch"]


def test_a_failed_weights_fetch_stops_the_deploy_before_anything_is_built_or_applied(
    releases_root, log, monkeypatch, tmp_path,
):
    events: list = []
    target = release.TARGETS["main"]
    _deploy_rig(monkeypatch, tmp_path, target, events,
                fetch_error=release.ReleaseError("build-failed", "could not fetch the EasyOCR weights"))
    with pytest.raises(release.ReleaseError, match="could not fetch the EasyOCR weights"):
        release.cmd_deploy(SimpleNamespace(test=False, no_build=False, connector=None), target, log)
    assert "build" not in events and "apply" not in events and "stage" not in events


def test_fetch_ocr_weights_asks_the_downloader_for_the_easyocr_directory_of_the_cache_root(tmp_path, monkeypatch, log):
    seen = []
    monkeypatch.setattr(release.ocr_weights, "fetch", lambda dest, logger, **kw: seen.append((dest, logger)))
    assert release.fetch_ocr_weights(tmp_path / "cache", log) == tmp_path / "cache" / "easyocr"
    assert seen == [(tmp_path / "cache" / "easyocr", log)]
    text = log.path.read_text(encoding="utf-8")
    assert f"ocr-weights: ensuring the EasyOCR weights in {tmp_path / 'cache' / 'easyocr'}" in text
    assert "ocr-weights: ready in" in text


@pytest.mark.parametrize("failure", [release.ocr_weights.OcrWeightsError("english_g2.pth: SHA-256 mismatch"),
                                     OSError("no space left on device")])
def test_fetch_ocr_weights_turns_a_download_failure_into_a_named_release_error(tmp_path, monkeypatch, log, failure):
    def fetch(dest, logger, **kw):
        raise failure

    monkeypatch.setattr(release.ocr_weights, "fetch", fetch)
    with pytest.raises(release.ReleaseError, match="could not fetch the EasyOCR weights") as raised:
        release.fetch_ocr_weights(tmp_path, log)
    assert raised.value.state == "build-failed" and type(failure).__name__ in str(raised.value)


def test_run_test_stack_confirms_the_ocr_weights_in_the_targets_model_cache_before_the_stack_starts(
    tmp_path, monkeypatch, log,
):
    """The stack mounts the target's model cache read-write; the weights must be in it BEFORE anything is
    generated or started (14.2.0)."""
    events: list = []
    target = release.TARGETS["main"]
    monkeypatch.setattr(release, "read_toolbox_version", lambda *a: "12.6.0")
    monkeypatch.setattr(release, "free_port", lambda: 1)
    monkeypatch.setattr(release, "target_models_root", lambda env_file: tmp_path / "models")
    monkeypatch.setattr(release, "fetch_ocr_weights", lambda models_root, logger: events.append(("fetch", models_root)))

    class Stop(Exception):
        pass

    def write_installation(*a, **k):
        events.append("write_installation")
        raise Stop

    monkeypatch.setattr(release, "write_installation", write_installation)
    monkeypatch.setattr(release, "image_exists", lambda ref: False)
    monkeypatch.setattr(release, "run", lambda *a, **k: (0, ""))
    monkeypatch.setattr(release.tempfile, "gettempdir", lambda: str(tmp_path))
    with pytest.raises(Stop):
        release.run_test_stack(tmp_path, target, "14.2.0", "a" * 40, {}, log, run_id="r1",
                               test_runner_ref="runner:1", no_build=True)
    assert events == [("fetch", tmp_path / "models"), "write_installation"]


def test_compose_selects_the_app_stage_and_both_dockerfiles_provide_it():
    """`target: app` in compose.yaml is only valid while both name that stage.

    containers/cognita/Dockerfile's last stage is `test`, so without the target
    a plain build would ship the test image; the amd Dockerfile has to answer
    to the same name because compose.amd.yaml only replaces the dockerfile.
    """
    compose = (REPO / "compose.yaml").read_text(encoding="utf-8")
    assert "target: app" in compose
    for dockerfile in ("containers/cognita/Dockerfile", "containers/cognita-amd/Dockerfile"):
        assert " AS app\n" in (REPO / dockerfile).read_text(encoding="utf-8"), dockerfile


def test_test_mode_override_sets_the_env_var_and_disables_restart():
    text = release.test_mode_override_text()
    assert 'COGNITA_TEST_MODE: "1"' in text
    assert 'restart: "no"' in text


def test_qa_restores_normal_mode_after_test_start_health_failure(monkeypatch, tmp_path, log):
    target = release.TARGETS["test"]
    events = []
    monkeypatch.setattr(release, "self_test_key", lambda repo: "synthetic-key")
    monkeypatch.setattr(release, "check_healthz", lambda *args, **kwargs:
                        events.append(("health", kwargs.get("expect_test_mode"))) or {})
    monkeypatch.setattr(release, "expect_unauthorized", lambda *args:
                        events.append(("key-rejected", None)))

    def fake_run(command, **kwargs):
        mode = "test" if any("test-mode.override.yaml" in str(part) for part in command) else "normal"
        events.append(("compose", mode))
        if mode == "test":
            raise release.ReleaseError("verify-failed", "test-mode health wait failed")
        return 0, ""

    monkeypatch.setattr(release, "run", fake_run)
    with pytest.raises(release.ReleaseError, match="test-mode health wait failed") as failure:
        release.qa_release(REPO, target, "13.3.0", tmp_path, log)
    assert failure.value.state == "verify-failed"
    assert events == [("health", None), ("compose", "test"), ("compose", "normal"),
                      ("health", False), ("key-rejected", None)]
    assert not (tmp_path / "test-mode.override.yaml").exists()


@pytest.mark.parametrize("restoration_raises", [False, True])
def test_qa_reports_restoration_failure_distinctly(monkeypatch, tmp_path, log,
                                                   restoration_raises):
    target = release.TARGETS["test"]
    monkeypatch.setattr(release, "self_test_key", lambda repo: "synthetic-key")
    monkeypatch.setattr(release, "check_healthz", lambda *args, **kwargs: {})
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        if len(calls) == 1:
            raise release.ReleaseError("verify-failed", "test startup failed")
        if restoration_raises:
            raise OSError("compose could not start")
        return 1, "normal startup failed"

    monkeypatch.setattr(release, "run", fake_run)
    with pytest.raises(release.ReleaseError) as failure:
        release.qa_release(REPO, target, "13.3.0", tmp_path, log)
    assert failure.value.state == "test-mode-stuck"
    assert "systemctl --user restart" in str(failure.value)
    assert len(calls) == 2


def test_test_stack_override_has_a_run_scoped_volume_and_a_runner():
    text = release.test_override_text(
        version="13.0.0", run_id="deadbeef", dsn="postgresql://cognita:pw@postgres:5432/cognita",
        models_root="/data/models")
    assert "name: cognita-test-deadbeef-pg" in text
    assert "image: cognita/app-test:13.0.0-deadbeef\n" in text
    assert "COGNITA_TEST_PG_DSN: postgresql://cognita:pw@postgres:5432/cognita" in text
    assert "source: /data/models" in text
    # The runner must not come up with `up`; it is started by `compose run`.
    assert 'profiles: ["test"]' in text
    # The throwaway app runs in test mode from the start so the live self-test
    # can use the built-in key (7.3); found missing on kei, where `live`
    # got a 401 body and died decoding it.
    assert 'COGNITA_TEST_MODE: "1"' in text
    assert 'restart: "no"' in text


def test_release_text_records_what_status_reports():
    target = release.TARGETS["test"]
    tags = release.release_tags("13.0.0", "abc123", target)
    text = release.release_text(target=target, version="13.0.0", commit="abc123",
                                ids={"cognita": "sha256:1", "workspace-runtime": "sha256:2"},
                                tags=tags, toolbox_version="12.6.0", tested=True)
    values = dict(line.split(": ", 1) for line in text.splitlines())
    assert values["version"] == "13.0.0"
    assert values["commit"] == "abc123"
    assert values["image_cognita"] == "sha256:1"
    assert values["image_ref_cognita"] == tags["cognita"]
    assert values["toolbox_version"] == "12.6.0"
    assert values["full_suite"] == "yes"


def test_unit_template_renders_every_placeholder():
    target = release.TARGETS["test"]
    text = release.render_unit(target, version_note="release.py-managed")
    assert "$" not in text, "an unsubstituted placeholder would be written into the unit"
    assert "-p cognita-13test" in text
    assert "up -d --no-build --pull never --wait" in text
    assert "ExecStop=" in text
    assert str(release.current_link(target)) in text
    assert "compose.images.yaml" in text


# --------------------------------------------------------------------------
# Identity
# --------------------------------------------------------------------------


def test_version_comes_only_from_release_identity(clean_repo, log):
    """One authority, no fallback (13.0 §4).

    A fallback to `cognita.__version__` would let a release be built and
    tagged from a version the running service would not report, so a missing
    literal is a refusal with the file named in it.
    """
    assert release.read_version(clean_repo, log) == "13.0.0"
    (clean_repo / "src" / "cognita" / "release_identity.py").write_text(
        'TOOLBOX_VERSION = "12.6.0"\n', encoding="utf-8")
    (clean_repo / "src" / "cognita" / "__init__.py").write_text('__version__ = "12.17.0"\n', encoding="utf-8")
    with pytest.raises(release.ReleaseError) as excinfo:
        release.read_version(clean_repo, log)
    assert "APPLICATION_VERSION" in str(excinfo.value)
    assert "release_identity.py" in str(excinfo.value)
    assert "12.17.0" not in str(excinfo.value)


def test_release_installation_and_host_runner_need_no_installed_cognita(tmp_path, monkeypatch):
    """The host release path reads the repo identity without importing Cognita."""
    checkout = tmp_path / "checkout"
    scripts = checkout / "scripts"
    identity = checkout / "src" / "cognita" / "release_identity.py"
    scripts.mkdir(parents=True)
    identity.parent.mkdir(parents=True)
    (scripts / "kei_http_selftest.py").write_bytes(
        (REPO / "scripts" / "kei_http_selftest.py").read_bytes())
    (scripts / "run-selftest.py").write_text("# synthetic copied runner\n", encoding="utf-8")
    identity.write_text("COMBINED_CONTRACT_VERSION = 77\n", encoding="utf-8")
    monkeypatch.setattr(release, "REPO_ROOT", checkout)
    monkeypatch.setattr(release.os, "getuid", lambda: 1000, raising=False)
    monkeypatch.setattr(release.os, "getgid", lambda: 1000, raising=False)

    original_import = builtins.__import__

    def forbid_cognita(name, *args, **kwargs):
        if name == "cognita" or name.startswith("cognita."):
            raise AssertionError(f"host release runner imported {name}")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", forbid_cognita)
    monkeypatch.setattr(release.shutil, "which", lambda command: None)
    install_root = tmp_path / "installation"
    install_root.mkdir()
    installation = release.write_installation(
        install_root, mcp_port=8765, admin_port=8766, version="16.0.0",
        release_target="test", gpu=False, models_root=tmp_path / "models",
        log=release.Log(None), mode="core",
    )
    assert installation.env_path.is_file()

    host = release._selftest_module()
    commands = []

    def run_command(args, *unused, **kwargs):
        commands.append(args)
        return 0

    class Process:
        returncode = 1

        def __init__(self, args, **kwargs):
            commands.append(args)

        def communicate(self, value, timeout):
            assert value == b"synthetic-key\n"
            return None, None

    monkeypatch.setattr(host, "run_command", run_command)
    monkeypatch.setattr(host.subprocess, "Popen", Process)
    assert host.run_selftest(
        checkout, ["docker", "compose"], "synthetic-key", tmp_path,
        mcp_port=8765, mode="core",
    ) == 1
    assert commands[0][-1] == "cognita:/tmp/cognita-run-selftest.py"
    assert commands[1][-1].endswith(
        "/mcp/connectors/self-test/mcp/v77"
    )


def test_the_repo_names_its_own_version_in_one_place():
    """The real checkout: release.py reads what the package reports."""
    from cognita import __version__

    assert release.read_version(REPO, release.Log(None)) == __version__


def test_toolbox_version_disagreement_is_refused(clean_repo, log):
    broker = clean_repo / "src" / "cognita" / "runtime_broker"
    broker.mkdir()
    (broker / "image_cache.py").write_text('TOOLBOX_VERSION = "12.7.0"\n', encoding="utf-8")
    with pytest.raises(release.ReleaseError) as excinfo:
        release.read_toolbox_version(clean_repo, log)
    assert "disagrees" in str(excinfo.value)


def test_the_repo_agrees_with_itself_about_the_toolbox_version():
    """The real checkout: the release literal and the loader literal are one."""
    assert release.read_toolbox_version(REPO, release.Log(None)) == "12.6.0"


# --------------------------------------------------------------------------
# Staging and selection
# --------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def tagged_images(monkeypatch):
    """Record the `docker tag` calls staging would make, and make none.

    Tagging is the one part of staging that needs a daemon; everything else
    these tests cover is file and symlink work, and the kei acceptance run
    covers the real tagging.
    """
    calls: list[tuple[dict[str, str], str, str, str]] = []

    def fake_tag(ids, target, version, commit, log):
        calls.append((dict(ids), target.name, version, commit))
        return release.release_tags(version, commit, target)

    monkeypatch.setattr(release, "tag_release_images", fake_tag)
    return calls


def _stage(clean_repo, releases_root, log, version="13.0.0", commit="abc123"):
    target = release.TARGETS["test"]
    return target, release.stage_release(
        clean_repo, target, version, commit,
        {"cognita": "sha256:1", "workspace-runtime": "sha256:2"}, "12.6.0", False, log)


def test_stage_release_writes_the_directory_atomically(clean_repo, releases_root, log, tagged_images):
    target, directory = _stage(clean_repo, releases_root, log)
    assert directory == releases_root / "test" / "13.0.0"
    assert sorted(p.name for p in directory.iterdir()) == [
        "compose.amd.yaml", "compose.images.yaml", "compose.workspace.yaml", "compose.yaml", "release.txt"]
    assert not directory.with_name("13.0.0.staging").exists()
    values = release.read_release_text(directory)
    assert values["commit"] == "abc123"
    # The release owns its images by tag, and records the IDs it built from.
    assert tagged_images == [({"cognita": "sha256:1", "workspace-runtime": "sha256:2"},
                              "test", "13.0.0", "abc123")]
    assert values["image_cognita"] == "sha256:1"
    images_file = (directory / "compose.images.yaml").read_text(encoding="utf-8")
    assert release.release_tags("13.0.0", "abc123", target)["cognita"] in images_file
    assert "sha256:" not in images_file


def test_select_uses_recorded_image_refs(clean_repo, releases_root, log, monkeypatch):
    target, directory = _stage(clean_repo, releases_root, log)
    expected = set(release.recorded_release_tags(release.read_release_text(directory)).values())
    seen = []
    monkeypatch.setattr(release, "image_exists", lambda ref: seen.append(ref) or True)
    monkeypatch.setattr(release, "apply_release", lambda *a: None)
    monkeypatch.setattr(release, "verify_release", lambda *a: None)
    release.cmd_select(SimpleNamespace(version="13.0.0"), target, log)
    assert set(seen) == expected
    legacy = release.release_tags("13.0.0", "abc123")
    (directory / "release.txt").write_text(
        "".join(line for line in (directory / "release.txt").read_text(encoding="utf-8").splitlines(True)
                if not line.startswith("image_ref_")), encoding="utf-8")
    (directory / "compose.images.yaml").write_text(
        release.compose_images_text("13.0.0", "abc123", legacy), encoding="utf-8")
    seen.clear()
    release.cmd_select(SimpleNamespace(version="13.0.0"), target, log)
    assert set(seen) == set(legacy.values())


def test_stage_release_removes_a_leftover_staging_directory(clean_repo, releases_root, log):
    leftover = releases_root / "test" / "13.0.0.staging"
    leftover.mkdir(parents=True)
    (leftover / "junk-from-a-crashed-run.txt").write_text("junk\n", encoding="utf-8")
    _target, directory = _stage(clean_repo, releases_root, log)
    assert not leftover.exists()
    assert not (directory / "junk-from-a-crashed-run.txt").exists()


def test_restaging_the_same_commit_replaces_the_directory(clean_repo, releases_root, log):
    _target, directory = _stage(clean_repo, releases_root, log)
    (directory / "stale.txt").write_text("stale\n", encoding="utf-8")
    _target, again = _stage(clean_repo, releases_root, log)
    assert again == directory
    assert not (again / "stale.txt").exists()
    assert not list(releases_root.joinpath("test").glob("*.replaced-*"))


def test_version_exists_with_another_commit_is_refused(clean_repo, releases_root, log):
    target, _directory = _stage(clean_repo, releases_root, log, commit="aaaaaaa")
    with pytest.raises(release.ReleaseError) as excinfo:
        release.check_version_free(target, "13.0.0", "bbbbbbb", log)
    assert excinfo.value.state == "version-exists"
    assert "Bump the version" in str(excinfo.value)
    # The same commit is a rerun, not a refusal.
    release.check_version_free(target, "13.0.0", "aaaaaaa", log)


@pytest.mark.skipif(os.name == "nt" and not os.environ.get("COGNITA_TEST_SYMLINKS"),
                    reason="creating a symlink on Windows needs a privilege this user does not hold; "
                           "the switch is exercised on kei")
def test_point_current_switches_atomically(clean_repo, releases_root, log):
    target = release.TARGETS["test"]
    first = releases_root / "test" / "13.0.0"
    second = releases_root / "test" / "13.0.1"
    first.mkdir(parents=True)
    second.mkdir(parents=True)
    release.point_current(target, first, log)
    assert release.current_link(target).resolve() == first.resolve()
    release.point_current(target, second, log)
    assert release.current_link(target).resolve() == second.resolve()
    assert not (releases_root / "test" / "current.switching").exists()


def test_prune_untags_only_the_releases_it_removes(clean_repo, releases_root, log, monkeypatch):
    target = release.TARGETS["test"]
    for index, version in enumerate(("13.0.0", "13.0.1", "13.0.2")):
        _target, directory = _stage(clean_repo, releases_root, log, version=version,
                                    commit=f"commit{index}0000000")
        os.utime(directory, (1_700_000_000 + index, 1_700_000_000 + index))
    removed: list[str] = []

    def fake_run(command, **kwargs):
        # Only `docker image rm <tag>` may be issued, and only for a release
        # being removed: pruning must never touch another release's images.
        assert command[:3] == ["docker", "image", "rm"], command
        removed.append(command[3])
        return 0, ""

    monkeypatch.setattr(release, "run", fake_run)
    release.prune(target, 1, log)
    assert sorted(removed) == sorted(
        [release.release_tags("13.0.0", "commit00000000", target)["cognita"],
         release.release_tags("13.0.0", "commit00000000", target)["workspace-runtime"],
         release.release_tags("13.0.1", "commit10000000", target)["cognita"],
         release.release_tags("13.0.1", "commit10000000", target)["workspace-runtime"]])
    assert [p.name for p in release.target_root(target).iterdir() if p.is_dir()] == ["13.0.2"]


def test_prune_also_removes_a_pruned_releases_own_published_images(clean_repo, releases_root, log, monkeypatch):
    # Pulled digest references keep an image alive after its per-release tag is gone, so a pruned
    # published release freed nothing (installer final review).  A ref a kept release shares stays.
    target = release.TARGETS["test"]
    refs = {"13.0.0": "reg/app@sha256:" + "a" * 64 + " reg/shared@sha256:" + "c" * 64,
            "13.0.1": "reg/app@sha256:" + "b" * 64 + " reg/shared@sha256:" + "c" * 64}
    for index, version in enumerate(("13.0.0", "13.0.1")):
        _target, directory = _stage(clean_repo, releases_root, log, version=version,
                                    commit=f"commit{index}0000000")
        with (directory / "release.txt").open("a", encoding="utf-8") as handle:
            handle.write(f"published_refs: {refs[version]}\n")
        os.utime(directory, (1_700_000_000 + index, 1_700_000_000 + index))
    removed: list[str] = []
    monkeypatch.setattr(release, "run", lambda command, **_k: (removed.append(command[3]), (0, ""))[1])
    release.prune(target, 1, log)
    assert "reg/app@sha256:" + "a" * 64 in removed
    assert "reg/app@sha256:" + "b" * 64 not in removed          # the kept release's own
    assert "reg/shared@sha256:" + "c" * 64 not in removed       # still recorded by the kept release


def test_prune_retains_a_legacy_tag_until_all_staged_users_are_removed(
    releases_root, log, monkeypatch,
):
    main, beta = release.TARGETS["main"], release.TARGETS["beta"]
    legacy = release.release_tags("13.2.11", "a" * 40)
    for target in (main, beta):
        directory = release.release_dir(target, "13.2.11")
        directory.mkdir(parents=True)
        (directory / "release.txt").write_text(
            f"version: 13.2.11\ncommit: {'a' * 40}\n", encoding="utf-8")
        (directory / "compose.images.yaml").write_text(
            release.compose_images_text("13.2.11", "a" * 40, legacy), encoding="utf-8")
    removed = []
    monkeypatch.setattr(release, "run", lambda command, **kwargs:
                        (removed.append(command[-1]) or 0, ""))
    release.prune(main, 0, log)
    assert removed == []
    assert not release.release_dir(main, "13.2.11").exists()
    release.prune(beta, 0, log)
    assert set(removed) == set(legacy.values())


def test_prune_keeps_metadata_when_untag_fails(clean_repo, releases_root, log, monkeypatch):
    target, directory = _stage(clean_repo, releases_root, log)
    monkeypatch.setattr(release, "run", lambda command, **kwargs: (1, "image in use"))
    release.prune(target, 0, log)
    assert directory.is_dir()
    assert "could not be removed" in log.path.read_text(encoding="utf-8")


@pytest.mark.skipif(os.name == "nt" and not os.environ.get("COGNITA_TEST_SYMLINKS"),
                    reason="creating a symlink on Windows needs a privilege this user does not hold; "
                           "the same case is exercised on kei")
def test_prune_never_touches_the_running_release_or_the_symlink(clean_repo, releases_root, log,
                                                                monkeypatch):
    """`current` may point at an OLDER release, and prune must see that.

    `current` is a symlink inside the same directory, so `is_dir()` is true for
    it and comparing the link path against a resolved one never matches. Both
    halves were wrong: prune treated the link as a release of its own, and the
    release it pointed at was not protected -- so pruning after a `select` of
    an older version untagged the images of the stack that was running and then
    tried to rmtree a symlink.
    """
    target = release.TARGETS["test"]
    for index, version in enumerate(("13.0.0", "13.0.1", "13.0.2", "13.0.3")):
        _target, directory = _stage(clean_repo, releases_root, log, version=version,
                                    commit=f"commit{index}0000000")
        os.utime(directory, (1_700_000_000 + index, 1_700_000_000 + index))
    running = release.release_dir(target, "13.0.0")  # the OLDEST, as after a select
    release.point_current(target, running, log)
    untagged: list[str] = []

    def fake_run(command, **kwargs):
        assert command[:3] == ["docker", "image", "rm"], command
        untagged.append(command[3])
        return 0, ""

    monkeypatch.setattr(release, "run", fake_run)
    release.prune(target, 2, log)

    # `--keep 2` keeps two releases in total and the current one is one of
    # them, so the newest survivor plus 13.0.0 remain.
    remaining = sorted(p.name for p in release.target_root(target).iterdir()
                       if p.is_dir() and not p.is_symlink())
    assert remaining == ["13.0.0", "13.0.3"]
    assert release.current_link(target).is_symlink()
    assert release.current_link(target).resolve() == running.resolve()
    # The running release keeps its images; only the pruned ones lose their tags.
    running_tags = set(release.release_tags("13.0.0", "commit00000000", target).values())
    assert not running_tags & set(untagged)
    assert set(untagged) == set(release.release_tags("13.0.1", "commit10000000", target).values()) | set(
        release.release_tags("13.0.2", "commit20000000", target).values())


def test_prune_keeps_the_newest_and_never_the_current(clean_repo, releases_root, log, monkeypatch):
    target = release.TARGETS["test"]
    made = []
    for index, version in enumerate(("13.0.0", "13.0.1", "13.0.2", "13.0.3")):
        _target, directory = _stage(clean_repo, releases_root, log, version=version)
        os.utime(directory, (1_700_000_000 + index, 1_700_000_000 + index))
        made.append(directory)
    # prune untags the removed releases' images; this test is about which
    # directories survive, and it must also run inside the test image, which
    # has no docker binary. The untagging itself is covered by the test above.
    monkeypatch.setattr(release, "run", lambda command, **kwargs: (0, ""))
    release.prune(target, 2, log)
    remaining = sorted(p.name for p in release.target_root(target).iterdir() if p.is_dir())
    assert remaining == ["13.0.2", "13.0.3"]


# --------------------------------------------------------------------------
# Refusals and exit codes (section 6.2)
# --------------------------------------------------------------------------


def test_exit_code_table_matches_the_design():
    assert release.EXIT_CODES == {
        "verified": 0, "usage": 1, "dirty-checkout": 2, "version-exists": 3,
        "doctor-failed": 4, "build-failed": 5, "test-failed": 6, "apply-failed": 7,
        "verify-failed": 8, "test-mode-stuck": 9,
    }


def test_dirty_checkout_is_refused(clean_repo, log):
    (clean_repo / "src" / "cognita" / "__init__.py").write_text('__version__ = "13.0.1"\n', encoding="utf-8")
    with pytest.raises(release.ReleaseError) as excinfo:
        release.require_clean_checkout(clean_repo, log)
    assert excinfo.value.state == "dirty-checkout"


def test_the_staged_build_artifact_is_ignored_by_git_not_by_the_rule(clean_repo, log):
    """The wheel is fetched into the checkout; `.gitignore` carries it, so the
    clean-checkout rule needs no exception and keeps refusing everything else."""
    (clean_repo / ".gitignore").write_text("build-artifacts/\n", encoding="utf-8")
    _git(clean_repo, "add", "-A")
    _git(clean_repo, "commit", "-q", "-m", "ignore build artifacts")
    (clean_repo / "build-artifacts").mkdir()
    (clean_repo / "build-artifacts" / "microsandbox-0.7.0-cp310-abi3-manylinux_2_28_x86_64.whl").write_bytes(b"x")
    assert release.require_clean_checkout(clean_repo, log)
    # Anything else untracked still refuses.
    (clean_repo / "stray.py").write_text("x = 1\n", encoding="utf-8")
    with pytest.raises(release.ReleaseError) as excinfo:
        release.require_clean_checkout(clean_repo, log)
    assert excinfo.value.state == "dirty-checkout"
    assert "stray.py" in str(excinfo.value)


def test_the_repo_ignores_the_build_artifacts_directory():
    """The rule above depends on it, so the real .gitignore is checked here."""
    assert "build-artifacts/" in (REPO / ".gitignore").read_text(encoding="utf-8").splitlines()


def test_build_on_a_dirty_checkout_exits_two(clean_repo, releases_root, monkeypatch, capsys):
    (clean_repo / "compose.yaml").write_text("services: {changed: true}\n", encoding="utf-8")
    monkeypatch.setattr(release, "REPO_ROOT", clean_repo)
    assert release.main(["build", "--target", "test"]) == 2
    printed = capsys.readouterr().out
    assert "[dirty-checkout]" in printed
    assert "log is" in printed


def test_unknown_target_is_a_usage_error(capsys):
    with pytest.raises(SystemExit) as excinfo:
        release.main(["build", "--target", "prod"])
    assert excinfo.value.code == 2  # argparse's own usage exit


def test_run_raises_with_the_tail_of_a_failing_command(log):
    with pytest.raises(release.ReleaseError) as excinfo:
        release.run([sys.executable, "-c", "import sys; print('boom'); sys.exit(3)"],
                    log=log, state="build-failed")
    assert excinfo.value.state == "build-failed"
    assert "boom" in str(excinfo.value)


@pytest.mark.parametrize("check", [True, False])
def test_a_child_stopped_by_ctrl_c_is_an_interruption_not_a_failure(log, check):
    # Exit 130 is how a child reports SIGINT.  When only the child saw the Ctrl+C (we were started in
    # the background with SIGINT ignored), the run must still stop, and say "interrupted", not
    # "command failed (130)" (interrupt proof on the installer VM, 2026-09-29).
    with pytest.raises(KeyboardInterrupt):
        release.run([sys.executable, "-c", "import sys; sys.exit(130)"],
                    log=log, state="build-failed", check=check)
    assert "interrupted:" in log.path.read_text(encoding="utf-8")


def test_run_can_report_a_failure_without_raising(log):
    code, tail = release.run([sys.executable, "-c", "import sys; sys.exit(7)"],
                             log=log, state="build-failed", check=False)
    assert code == 7
    assert tail == ""


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------


def test_release_target_key_is_checked_not_enforced(tmp_path, log):
    """A missing COGNITA_RELEASE_TARGET degrades one message; it warns."""
    env = tmp_path / "cognita-test-amd.env"
    target = release.TARGETS["test"]
    env.write_text("COGNITA_RELEASE_TARGET=test\n", encoding="utf-8")
    patched = dataclasses.replace(target, env_file=env)
    assert release.check_release_target_key(patched, log) is True
    env.write_text("COGNITA_CONFIG_ROOT=/data/x\n", encoding="utf-8")
    assert release.check_release_target_key(patched, log) is False
    env.write_text("COGNITA_RELEASE_TARGET=main\n", encoding="utf-8")
    assert release.check_release_target_key(patched, log) is False


def test_export_version_wins_over_a_stale_env_file(monkeypatch, log):
    """compose.yaml has no version literal, so the authority is exported.

    Compose prefers the process environment over --env-file, which is what
    keeps a target whose env file still names an older release from building
    or tagging an image with that version.
    """
    monkeypatch.delenv("COGNITA_VERSION", raising=False)
    release.export_version("13.0.0", log)
    assert os.environ["COGNITA_VERSION"] == "13.0.0"


_LAYOUT_REFUSAL = ("cognita.workspace.WorkspaceStateIncompatible: The Workspace metadata at "
                   "/app/config/data/workspace-metadata.sqlite3 was written by a different build "
                   "(workspaces: missing column(s) last_auto_action). Workspace state is disposable and "
                   "this build does not migrate it. Discard and regenerate it with: "
                   "python3 scripts/reset_disposable_state.py --target test --scope workspaces --apply")


def test_a_workspace_layout_refusal_requires_explicit_manual_reset(monkeypatch, log):
    """A failed start reports recovery commands without spawning a reset."""
    seen: list[list[str]] = []

    def fake_run(command, *, log, state, check=True, quiet=False, **kwargs):
        seen.append(command)
        if command[:2] == ["docker", "compose"]:
            # Compose prefixes the container name; the app's colour reset survives --no-color.
            return 0, "cognita-1  | traceback line one\ncognita-1  | " + _LAYOUT_REFUSAL + "\x1b[0m\n"
        raise AssertionError("release must not launch a reset subprocess")

    monkeypatch.setattr(release, "run", fake_run)
    target = release.TARGETS["test"]
    compose = ["docker", "compose", "-p", target.project]

    with pytest.raises(release.ReleaseError) as failure:
        release.handle_start_failure(target, compose, 1, version="13.1.0", log=log)

    assert failure.value.state == "apply-failed"
    assert "No reset was run" in str(failure.value)
    assert "python3 scripts/reset_disposable_state.py --target test --scope workspaces --apply" in str(failure.value)
    assert "python3 scripts/release.py select --target test --version 13.1.0" in str(failure.value)
    assert seen == [compose + ["logs", "--no-color", "--tail", "40", "cognita"]]
    assert "\x1b" not in str(failure.value)


def test_a_refusal_for_another_target_or_scope_is_never_acted_on(monkeypatch, log):
    """A refusal for another target is reported without recommending this target's reset."""
    other = _LAYOUT_REFUSAL.replace("--target test --scope workspaces", "--target main --scope workspaces")
    seen: list[list[str]] = []

    def fake_run(command, *, log, state, check=True, quiet=False, **kwargs):
        seen.append(command)
        return 0, "cognita-1  | " + other + "\n"

    monkeypatch.setattr(release, "run", fake_run)
    target = release.TARGETS["test"]
    with pytest.raises(release.ReleaseError) as failure:
        release.handle_start_failure(target, ["docker", "compose"], 1, version="13.1.0", log=log)
    assert failure.value.state == "apply-failed"
    assert other in str(failure.value)
    assert "select --target test --version 13.1.0" in str(failure.value)
    assert "Review the refusal's target and scope" in str(failure.value)
    assert "python3 scripts/reset_disposable_state.py --target test" not in str(failure.value)
    assert len(seen) == 1


def test_a_start_failure_without_a_refusal_is_reported_plainly(monkeypatch, log):
    monkeypatch.setattr(release, "run", lambda command, **kwargs: (0, "ordinary startup noise\n"))
    target = release.TARGETS["test"]
    with pytest.raises(release.ReleaseError) as failure:
        release.handle_start_failure(target, ["docker", "compose"], 3, version="13.1.0", log=log)
    assert str(failure.value) == f"systemctl --user start {target.unit} failed (3)"


def test_install_unit_refuses_while_the_unit_is_active(tmp_path, monkeypatch, log):
    """Replacing a unit under a running stack strands it.

    systemd stops a unit with the ExecStop of the file on disk, so after a
    daemon-reload the old containers have no command left that can bring them
    down.  One stop first, and the whole class of problem is gone.
    """
    target = release.TARGETS["test"]
    monkeypatch.setattr(release, "SYSTEMD_USER_DIR", tmp_path / "systemd")
    monkeypatch.setattr(release, "run", lambda command, **kwargs: (0, "active"))
    with pytest.raises(release.ReleaseError) as excinfo:
        release.install_unit(target, log)
    assert f"systemctl --user stop {target.unit}" in str(excinfo.value)
    assert not (tmp_path / "systemd").exists(), "nothing may be written while the unit is active"

    # Inactive: `systemctl is-active` exits non-zero and the unit is written.
    monkeypatch.setattr(release, "run", lambda command, **kwargs: (3, "inactive"))
    release.install_unit(target, log)
    assert (tmp_path / "systemd" / target.unit).is_file()


def test_the_container_mcp_port_is_the_one_inside_the_container():
    # Host ports differ per target; the live self-test runs in the container.
    assert release.CONTAINER_MCP_PORT == 8675


def test_toolbox_cache_root_comes_from_the_env_file(tmp_path):
    """The container mounts exactly this path, so it is read, never assumed."""
    env = tmp_path / "t.env"
    env.write_text("COGNITA_TOOLBOX_IMAGE_CACHE_ROOT=/data/x/toolbox-cache\n", encoding="utf-8")
    assert release.toolbox_cache_root(env) == Path("/data/x/toolbox-cache")
    # The historical default, for an env file that predates the explicit key.
    env.write_text("COGNITA_WORKSPACE_DATA_ROOT=/data/x/workspaces\n", encoding="utf-8")
    assert release.toolbox_cache_root(env) == Path("/data/x/workspaces/toolbox-cache")
    env.write_text("COGNITA_VERSION=13.0.0\n", encoding="utf-8")
    with pytest.raises(release.ReleaseError):
        release.toolbox_cache_root(env)


def test_read_env_file_ignores_comments_and_blanks(tmp_path):
    path = tmp_path / "t.env"
    path.write_text("# comment\n\nCOGNITA_CONFIG_ROOT=/data/x\nBAD LINE\nA=1\n", encoding="utf-8")
    assert release.read_env_file(path) == {"COGNITA_CONFIG_ROOT": "/data/x", "A": "1"}
    assert release.read_env_file(tmp_path / "missing.env") == {}


def test_parse_pytest_summary_finds_the_counts():
    assert release.parse_pytest_summary("...\n5 passed, 2 skipped in 3.1s\n") == "5 passed, 2 skipped in 3.1s"
    assert "no pytest summary" in release.parse_pytest_summary("nothing here\n")


def test_check_healthz_never_fails_a_deploy_on_the_gpu(monkeypatch, log):
    """The GPU is an accelerator, never a dependency (13.2.3). Busy cards
    (beta rebuilding its index) or a scan that has not landed yet are a
    WARNING with the server's own reasons, and verification carries on."""
    busy = {"version": "13.2.3", "status": "ok", "index": {"status": "ok"},
            "embed": {"gpu": "no device qualifies",
                      "devices_skipped": ["card1[0]:busy=100%>20%", "card2[1]:busy=95%>20%"]}}
    reads: list[int] = []
    monkeypatch.setattr(release, "healthz", lambda port, *, log, attempts=30: (reads.append(port), dict(busy))[1])
    monkeypatch.setattr(release, "_sleep", lambda _seconds: (_ for _ in ()).throw(AssertionError("no waiting on the GPU")))
    target = release.TARGETS["main"]

    payload = release.check_healthz(target, "13.2.3", log)

    assert payload["embed"]["gpu"] == "no device qualifies"
    assert reads == [target.mcp_port]
    logged = Path(log.path).read_text(encoding="utf-8")
    assert "WARNING no GPU qualifies right now" in logged
    assert "busy=100%>20%" in logged
    assert "Not a deploy failure" in logged


def test_check_healthz_still_fails_on_the_wrong_version(monkeypatch, log):
    monkeypatch.setattr(release, "healthz", lambda port, *, log, attempts=30: {"version": "13.2.2", "status": "ok"})
    with pytest.raises(release.ReleaseError) as failure:
        release.check_healthz(release.TARGETS["main"], "13.2.3", log)
    assert failure.value.state == "verify-failed"


def test_install_unit_retires_dropins_that_would_override_the_unit(tmp_path, monkeypatch, log):
    """13.2.2, kei: beta's unit had `<unit>.d/90-cognita-release-lifecycle.conf`
    from the 12.x tooling, whose Exec lines override the unit file; the 13.x
    unit was written and logged, and systemd started the old controller."""
    target = release.TARGETS["beta"]
    systemd = tmp_path / "systemd"
    monkeypatch.setattr(release, "SYSTEMD_USER_DIR", systemd)
    monkeypatch.setattr(release, "RELEASES_ROOT", tmp_path / "releases")
    dropin = systemd / f"{target.unit}.d" / "90-cognita-release-lifecycle.conf"
    dropin.parent.mkdir(parents=True)
    dropin.write_text("""[Service]
ExecStart=
ExecStart=/usr/bin/python3 old-controller.py
""", encoding="utf-8")
    commands: list[list[str]] = []
    monkeypatch.setattr(release, "run", lambda command, **kwargs: (commands.append(command), (3, "inactive"))[1])

    release.install_unit(target, log)

    assert not dropin.exists()
    assert not dropin.parent.exists()
    saved = list((tmp_path / "releases" / "beta" / "units").glob(f"{target.unit}.d.90-cognita-release-lifecycle.conf.*"))
    assert len(saved) == 1 and "old-controller.py" in saved[0].read_text(encoding="utf-8")
    assert (systemd / target.unit).is_file()
    assert ["systemctl", "--user", "daemon-reload"] in commands
    assert "retired drop-in" in Path(log.path).read_text(encoding="utf-8")


def test_install_unit_reloads_when_only_a_dropin_changed(tmp_path, monkeypatch, log):
    target = release.TARGETS["beta"]
    systemd = tmp_path / "systemd"
    monkeypatch.setattr(release, "SYSTEMD_USER_DIR", systemd)
    monkeypatch.setattr(release, "RELEASES_ROOT", tmp_path / "releases")
    systemd.mkdir(parents=True)
    (systemd / target.unit).write_text(release.render_unit(target, version_note="release.py-managed"), encoding="utf-8")
    dropin = systemd / f"{target.unit}.d" / "90-x.conf"
    dropin.parent.mkdir(parents=True)
    dropin.write_text("""[Service]
ExecStop=
""", encoding="utf-8")
    commands: list[list[str]] = []
    monkeypatch.setattr(release, "run", lambda command, **kwargs: (commands.append(command), (3, "inactive"))[1])

    release.install_unit(target, log)

    assert not dropin.exists()
    assert ["systemctl", "--user", "daemon-reload"] in commands


def test_apply_refuses_a_unit_that_still_has_a_dropin(tmp_path, monkeypatch):
    target = release.TARGETS["beta"]
    systemd = tmp_path / "systemd"
    monkeypatch.setattr(release, "SYSTEMD_USER_DIR", systemd)
    release.refuse_dropins(target)   # none: fine
    dropin = systemd / f"{target.unit}.d" / "90-x.conf"
    dropin.parent.mkdir(parents=True)
    dropin.write_text("""[Service]
ExecStart=
""", encoding="utf-8")
    with pytest.raises(release.ReleaseError) as failure:
        release.refuse_dropins(target)
    assert failure.value.state == "usage"
    assert str(dropin) in str(failure.value)
    assert "install-unit --target beta" in str(failure.value)


# --------------------------------------------------------------------------
# 15.0.0 (DESIGN-NVIDIA-ACCELERATION 10): the nvidia profile
# --------------------------------------------------------------------------


def test_the_profile_sets_name_nvidia_beside_cpu_and_amd():
    assert release.PROFILES == ("cpu", "amd", "nvidia")
    assert release.GPU_PROFILES == ("amd", "nvidia")


def test_compose_file_sets_include_the_nvidia_overlay(tmp_path):
    nvidia = release.checkout_compose_files(tmp_path, "nvidia", "full")
    assert [p.name for p in nvidia] == ["compose.yaml", "compose.nvidia.yaml", "compose.workspace.yaml"]
    assert [p.name for p in release.checkout_compose_files(tmp_path, "nvidia", "core")] == [
        "compose.yaml", "compose.nvidia.yaml"]
    (tmp_path / "compose.workspace.yaml").write_text("services: {}\n", encoding="utf-8")
    staged = release.staged_compose_files(tmp_path, "nvidia")
    assert [p.name for p in staged] == ["compose.yaml", "compose.nvidia.yaml", "compose.workspace.yaml",
                                        "compose.images.yaml"]
    with pytest.raises(release.ReleaseError, match="unknown profile"):
        release.checkout_compose_files(tmp_path, "intel", "core")


def test_docker_build_app_maps_each_profile_to_its_own_dockerfile(tmp_path, monkeypatch, log):
    seen: list[list[str]] = []
    monkeypatch.setattr(release, "run", lambda command, **kwargs: seen.append(command))
    for profile, directory in (("cpu", "cognita"), ("amd", "cognita-amd"), ("nvidia", "cognita-nvidia")):
        release.docker_build_app(tmp_path, profile, "15.0.0", "c" * 40, f"cognita-app:x-{profile}", log)
        argv = seen[-1]
        assert argv[argv.index("-f") + 1] == str(tmp_path / "containers" / directory / "Dockerfile"), profile
        assert argv[argv.index("--target") + 1] == "app"
        assert argv[argv.index("-t") + 1] == f"cognita-app:x-{profile}"


def test_the_gpu_health_check_treats_an_nvidia_target_like_an_amd_one(monkeypatch, log):
    busy = {"version": "15.0.0", "status": "ok", "index": {"status": "ok"},
            "embed": {"gpu": "no device qualifies", "devices_skipped": ["card0:vram_free=1.0<6.9"]}}
    monkeypatch.setattr(release, "healthz", lambda port, *, log, attempts=30: dict(busy))
    target = dataclasses.replace(release.TARGETS["test"], profile="nvidia")
    release.check_healthz(target, "15.0.0", log)
    logged = Path(log.path).read_text(encoding="utf-8")
    assert "verify: embed.gpu=no device qualifies" in logged and "Not a deploy failure" in logged


def test_candidate_profiles_keep_cpu_first_and_refuse_a_profile_that_does_not_exist(tmp_path, monkeypatch):
    """`--profiles nvidia,cpu` builds the CPU image first (its smoke gates everything else), and the smoke's
    failure here holds the NVIDIA build back exactly as it holds AMD's."""
    events: list = []
    monkeypatch.setattr(release, "validate_cpu_ocr_inputs", lambda repo: {})
    monkeypatch.setattr(release, "target_models_root", lambda env_file: tmp_path / "models")
    monkeypatch.setattr(release, "fetch_ocr_weights", lambda models_root, log: models_root / "easyocr")
    monkeypatch.setattr(release, "verify_candidate_image", lambda *args: "sha256:" + "a" * 64)
    monkeypatch.setattr(release, "stage_microsandbox_wheel", lambda *args: events.append("wheel"))
    monkeypatch.setattr(release, "run", lambda command, **kwargs: events.append(command))

    def smoke(*args, **kwargs):
        events.append("smoke")
        raise release.ReleaseError("build-failed", "inference failed")

    monkeypatch.setattr(release, "cpu_ocr_smoke", smoke)
    images = tmp_path / "images"
    with pytest.raises(release.ReleaseError, match="inference failed"):
        release.build_candidate_images(tmp_path, release.TARGETS["test"], "test", "b" * 40, ["nvidia", "cpu"],
                                       images, tmp_path / "cpu.tar", tmp_path / "SHA256SUMS", release.Log(None))
    builds = [event for event in events if isinstance(event, list)]
    assert len(builds) == 1 and str(tmp_path / "containers/cognita/Dockerfile") in builds[0]
    assert "wheel" not in events                      # the GPU profile's build context is staged only after the CPU proof
    for bad in (["cpu", "intel"], ["nvidia"], ["cpu", "nvidia", "nvidia"]):
        with pytest.raises(release.ReleaseError, match="--profiles must contain cpu"):
            release.build_candidate_images(tmp_path, release.TARGETS["test"], "test", "b" * 40, bad,
                                           images, tmp_path / "cpu.tar", tmp_path / "SHA256SUMS", release.Log(None))
