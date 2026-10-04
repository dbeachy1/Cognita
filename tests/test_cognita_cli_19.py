"""scripts/cognita_cli.py, design 19 (docs/DESIGN-LINUX-INSTALLER.md 19.1, 19.2, 19.4, 19.5, 19.6): what
Windows setup needs from the installer.  The fakes are the ones tests/test_cognita_cli.py defines.

Nothing here waits: the progress clock is injected, the model-size ticker is driven by the fake runner, and
no command, Docker, systemd, network call or Admin server is real.
"""
from __future__ import annotations

import datetime as dt
import json
import sys
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from test_cognita_cli import Rig, cli, release
from test_cognita_cli_flows import _amd_machine, _update, password_file, run_install, unattended
from test_release_local import release as real_release

FIXED_NOW = dt.datetime(2026, 9, 29, 12, 30, 0)
WINDOWS_DISPLAY = "D:\\Documents"


def installed(rig, *extra, **kw) -> dict:
    """A finished install, with any extra install flags; the env file as it was written."""
    assert run_install(rig, *extra, **kw) == cli.EXIT_OK
    return rig.env()


@pytest.fixture
def rig(tmp_path, monkeypatch):
    rig = Rig(tmp_path, monkeypatch)
    rig.ocr_calls = []
    rig.ocr_error = None

    def fetch(dest, log, progress=None):
        rig.ocr_calls.append(str(dest))
        if progress is not None:
            progress(10 * 1024 * 1024, 0)
            progress(30 * 1024 * 1024, 0)
        if rig.ocr_error:
            raise rig.ocr_error

    monkeypatch.setitem(sys.modules, "ocr_weights", SimpleNamespace(fetch=fetch))
    return rig


def with_progress(rig, path: Path) -> Path:
    """Switch the progress file on the way main() does, with a fixed clock."""
    rig.ui.progress = cli.Progress(path, log=rig.log, render=rig.ui.render, clock=lambda: FIXED_NOW)
    rig.ui.progress.start_command()
    return path


def progress_lines(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def stages_in_order(lines: list[dict]) -> list[str]:
    ordered: list[str] = []
    for line in lines:
        if line["stage"] not in ordered:
            ordered.append(line["stage"])
    return ordered


# --------------------------------------------------------------------------
# 19.1 --progress-file
# --------------------------------------------------------------------------


def test_an_install_writes_every_stage_in_order_with_start_and_done(rig, tmp_path):
    path = with_progress(rig, tmp_path / "progress.jsonl")
    assert run_install(rig) == cli.EXIT_OK
    lines = progress_lines(path)
    assert stages_in_order(lines) == [
        "checks", "prerequisites", "plan", "layout", "images", "password", "ocr_weights", "models", "linger",
        "start", "proof", "remote_access", "finish"]
    assert all(line["schema"] == 1 and line["time"] == "2026-09-29T12:30:00" for line in lines)
    assert all(line["title"] == cli.STAGE_TITLES[line["stage"]] for line in lines)
    for stage in stages_in_order(lines):
        states = [line["state"] for line in lines if line["stage"] == stage]
        assert states[0] == "start" and states[-1] == "done", stage
    assert (lines[0]["stage"], lines[0]["state"]) == ("checks", "start")
    assert (lines[-1]["stage"], lines[-1]["state"]) == ("finish", "done")
    assert not any(line["state"] in ("failed", "warning") for line in lines)


def test_an_amd_install_adds_the_acceleration_stage_before_the_proof(rig, tmp_path):
    _amd_machine(rig)
    path = with_progress(rig, tmp_path / "progress.jsonl")
    assert run_install(rig, "--acceleration", "amd") == cli.EXIT_OK
    order = stages_in_order(progress_lines(path))
    assert order.index("start") < order.index("acceleration") < order.index("proof")


def test_the_acceleration_stage_is_titled_for_the_vendor_being_checked(rig, tmp_path):
    """15.0.0: an NVIDIA install says "Checking the NVIDIA card"; the AMD title is unchanged."""
    from test_cognita_cli_flows import _nvidia_machine
    _nvidia_machine(rig)
    path = with_progress(rig, tmp_path / "nvidia-progress.jsonl")
    assert run_install(rig, "--acceleration", "nvidia") == cli.EXIT_OK
    lines = [line for line in progress_lines(path) if line["stage"] == "acceleration"]
    assert lines and {line["title"] for line in lines} == {"Checking the NVIDIA card"}
    assert cli.STAGE_TITLES["acceleration"] == "Checking the AMD card"


def test_a_gpu_that_does_not_verify_is_a_warning_at_the_acceleration_stage(rig, tmp_path):
    _amd_machine(rig)
    rig.admin_state.verify_result = {"state": "failed", "cards": [], "cleanup": "passed",
                                     "runtimes": {"embedding": {"reason": "canary_failed"}, "ocr": {}}}
    path = with_progress(rig, tmp_path / "progress.jsonl")
    assert run_install(rig, "--acceleration", "amd") == cli.EXIT_OK
    warning = [line for line in progress_lines(path) if line["state"] == "warning"][0]
    assert warning["stage"] == "acceleration" and "canary_failed" in warning["message"]


def test_only_download_stages_carry_bytes_and_only_failed_and_warning_carry_a_message(rig, tmp_path):
    rig.sh.ticks = 2
    rig.host.sizes[f"{rig.data}/models"] = 1_100_000_000
    path = with_progress(rig, tmp_path / "progress.jsonl")
    run_install(rig)
    downloads = {"images", "models", "ocr_weights"}
    lines = progress_lines(path)
    for line in lines:
        if "bytes_done" in line:
            assert line["stage"] in downloads and line["state"] in ("progress", "done")
            assert "bytes_total" in line
        assert "message" not in line and "fix" not in line               # no failure and no warning in this run
        assert set(line) <= {"schema", "time", "stage", "title", "state", "bytes_done", "bytes_total"}
    assert {line["stage"] for line in lines if "bytes_done" in line} == downloads


def test_images_reports_the_compressed_sizes_of_the_images_present_so_far(rig, tmp_path):
    path = with_progress(rig, tmp_path / "progress.jsonl")
    run_install(rig, workspace="on")
    published = cli.parse_published((rig.repo / "containers" / "published-release.txt").read_text())
    total = cli.image_download_bytes(published, "cpu", True)
    assert total == 720_000_000 + 210_000_000 + 430_000_000 + cli.POSTGRES_IMAGE_BYTES      # PostgreSQL included
    images = [line for line in progress_lines(path) if line["stage"] == "images"]
    assert [line["state"] for line in images] == ["start", "progress", "progress", "progress", "progress",
                                                  "progress", "progress", "done"]
    assert [line.get("bytes_done") for line in images if line["state"] == "progress"] == [
        0, 720_000_000, 930_000_000, 1_360_000_000, 1_520_000_000, 1_520_000_000]
    assert {line["bytes_total"] for line in images if "bytes_total" in line} == {total}
    assert images[-1]["bytes_done"] == images[-1]["bytes_total"] == total


def test_core_images_leave_out_the_workspace_images(rig, tmp_path):
    path = with_progress(rig, tmp_path / "progress.jsonl")
    run_install(rig, workspace="off")
    done = [line for line in progress_lines(path) if line["stage"] == "images" and line["state"] == "done"][0]
    assert done["bytes_done"] == done["bytes_total"] == 720_000_000 + cli.POSTGRES_IMAGE_BYTES


def test_models_reports_the_cache_size_each_tick_and_ocr_weights_each_callback(rig, tmp_path):
    rig.sh.ticks = 2
    rig.host.sizes[f"{rig.data}/models"] = 1_100_000_000
    path = with_progress(rig, tmp_path / "progress.jsonl")
    run_install(rig)
    lines = progress_lines(path)
    models = [line for line in lines if line["stage"] == "models" and line["state"] == "progress"]
    assert [(line["bytes_done"], line["bytes_total"]) for line in models] == [
        (1_100_000_000, cli.EXPECTED_MODEL_BYTES)] * 2
    ocr = [line for line in lines if line["stage"] == "ocr_weights" and line["state"] == "progress"]
    assert [(line["bytes_done"], line["bytes_total"]) for line in ocr] == [
        (10 * 1024 * 1024, cli.OCR_WEIGHTS_BYTES), (30 * 1024 * 1024, cli.OCR_WEIGHTS_BYTES)]


def test_without_a_progress_file_nothing_is_written_and_nothing_changes(rig, tmp_path):
    assert run_install(rig) == cli.EXIT_OK
    assert not rig.ui.progress.enabled
    assert not list(tmp_path.glob("*.jsonl"))
    assert rig.ocr_calls == [str(Path(rig.data) / "models" / "easyocr")]      # fetch was called as it always was


def test_the_file_is_truncated_once_when_the_command_starts(rig, tmp_path):
    path = tmp_path / "progress.jsonl"
    path.write_text('{"stale": true}\n', encoding="utf-8")
    args = rig.install_args("--non-interactive", "--yes", "--remote-access", "no", "--workspace", "on",
                            "--admin-password-file", password_file(rig), "--progress-file", str(path))
    rig.ui.non_interactive = True
    cli.setup_progress(rig.ctx, args)
    assert path.read_text(encoding="utf-8") == ""
    assert cli.cmd_install(rig.ctx, args) == cli.EXIT_OK
    lines = progress_lines(path)
    assert lines and "stale" not in lines[0]


def test_the_reexec_after_the_pull_keeps_the_file_and_does_not_truncate_it_again(rig, tmp_path):
    installed(rig)
    path = tmp_path / "progress.jsonl"
    heads = iter(["a" * 40, "b" * 40])
    rig.sh.script.insert(0, (("git", "-C", str(rig.repo), "rev-parse", "HEAD"),
                             lambda argv: cli.Result(0, next(heads) + "\n")))
    args = unattended(rig, "update", "--progress-file", str(path))
    cli.setup_progress(rig.ctx, args)
    assert cli.cmd_update(rig.ctx, args) == cli.EXIT_OK
    # The re-exec passes the flag on (through the flag carry-over) plus the marker that says "continue".
    assert rig.reexec_calls == [["update", "--after-pull", "--admin-password-file", password_file(rig),
                                 "--non-interactive", "--progress-file", str(path), "--progress-continue"]]
    assert [(line["stage"], line["state"]) for line in progress_lines(path)] == [("update", "start")]
    # The second process: same file, no truncation, no second `update: start`.
    rig.write_published("14.2.0")
    second = rig.args(*rig.reexec_calls[0])
    cli.setup_progress(rig.ctx, second)
    assert cli.cmd_update(rig.ctx, second) == cli.EXIT_OK
    lines = progress_lines(path)
    assert [(line["stage"], line["state"]) for line in lines if line["stage"] == "update"] == [
        ("update", "start"), ("update", "done")]
    assert stages_in_order(lines) == ["update", "images", "ocr_weights", "models", "start", "proof"]


def test_no_pull_is_a_fresh_command_and_does_truncate(rig, tmp_path):
    installed(rig)
    path = tmp_path / "progress.jsonl"
    path.write_text('{"stale": true}\n', encoding="utf-8")
    args = unattended(rig, "update", "--no-pull", "--progress-file", str(path))
    cli.setup_progress(rig.ctx, args)
    assert cli.cmd_update(rig.ctx, args) == cli.EXIT_OK
    assert [(line["stage"], line["state"]) for line in progress_lines(path)] == [
        ("update", "start"), ("update", "done")]                                # already up to date: 14.1.0


def test_update_rollback_add_folder_and_password_are_an_outer_pair_around_the_stages_they_run(rig, tmp_path):
    installed(rig)
    rig.write_published("14.2.0")
    path = with_progress(rig, tmp_path / "update.jsonl")
    _update(rig, "--after-pull")
    lines = progress_lines(path)
    assert (lines[0]["stage"], lines[0]["state"]) == ("update", "start")
    assert (lines[-1]["stage"], lines[-1]["state"]) == ("update", "done")
    assert stages_in_order(lines) == ["update", "images", "ocr_weights", "models", "start", "proof"]

    path = with_progress(rig, tmp_path / "rollback.jsonl")
    cli.cmd_rollback(rig.ctx, unattended(rig, "rollback"))
    lines = progress_lines(path)
    assert stages_in_order(lines) == ["rollback", "start", "proof"]
    assert (lines[-1]["stage"], lines[-1]["state"]) == ("rollback", "done")

    second = f"{rig.root}/docs two"
    rig.host.add_dir(second)
    path = with_progress(rig, tmp_path / "add.jsonl")
    cli.cmd_add_folder(rig.ctx, rig.args("add-folder", second))
    lines = progress_lines(path)
    assert stages_in_order(lines) == ["add_folder", "start"]
    assert (lines[-1]["stage"], lines[-1]["state"]) == ("add_folder", "done")

    path = with_progress(rig, tmp_path / "password.jsonl")
    cli.cmd_password(rig.ctx, unattended(rig, "password"))
    lines = progress_lines(path)
    assert stages_in_order(lines) == ["password_change", "password", "start"]
    assert (lines[-1]["stage"], lines[-1]["state"]) == ("password_change", "done")


def _main_install(rig, path: Path, *extra) -> int:
    argv = ["install", "--documents", rig.docs, "--data-dir", rig.data, "--admin-user", "admin",
            "--non-interactive", "--yes", "--workspace", "on", "--remote-access", "no",
            "--admin-password-file", password_file(rig), "--progress-file", str(path), *extra]
    rig.ui.non_interactive = True
    return cli.main(argv, ctx=rig.ctx)


def test_a_release_error_mid_install_writes_failed_with_the_terminals_message_and_next_step(rig, tmp_path, capsys):
    rig.tool.stage_error = release.ReleaseError("build-failed", "pull failed: no route to host")
    path = tmp_path / "progress.jsonl"
    code = _main_install(rig, path)
    assert code == release.EXIT_CODES["build-failed"]
    last = progress_lines(path)[-1]
    assert (last["stage"], last["state"]) == ("images", "failed")
    assert last["message"] == "pull failed: no route to host"
    assert last["fix"] == "Fix the cause above, then run the same command again; finished steps are skipped."
    err = capsys.readouterr().err
    assert "pull failed: no route to host" in err and "Next: fix the cause above" in err     # the terminal says it too


def test_a_cli_error_writes_failed_with_the_hint_as_the_fix(rig, tmp_path, capsys):
    rig.host.busy_ports = {8675}
    path = tmp_path / "progress.jsonl"
    assert _main_install(rig, path) == cli.EXIT_FAILED
    last = progress_lines(path)[-1]
    assert (last["stage"], last["state"]) == ("checks", "failed")
    assert last["message"] == "1 check(s) failed." and last["fix"] == "Fix them and run ./cognita install again."
    assert "Fix them and run ./cognita install again." in capsys.readouterr().err


def test_an_interrupt_and_an_unexpected_error_each_write_failed(rig, tmp_path, capsys):
    rig.tool.stage_error = KeyboardInterrupt()
    path = tmp_path / "interrupt.jsonl"
    assert _main_install(rig, path) == 130
    last = progress_lines(path)[-1]
    assert (last["stage"], last["state"], last["message"]) == ("images", "failed", "Interrupted.")
    assert "Run the same command again" in last["fix"]

    rig.tool.stage_error = RuntimeError("kaboom")
    path = tmp_path / "unexpected.jsonl"
    assert _main_install(rig, path) == cli.EXIT_FAILED
    last = progress_lines(path)[-1]
    assert (last["stage"], last["state"]) == ("images", "failed")
    assert last["message"] == "Unexpected error: RuntimeError: kaboom"
    capsys.readouterr()


def test_a_failed_proof_writes_failed_at_proof(rig, tmp_path):
    rig.tool.qa_error = release.ReleaseError("verify-failed", "live self-test failed")
    path = with_progress(rig, tmp_path / "progress.jsonl")
    assert run_install(rig) == cli.EXIT_FAILED
    last = progress_lines(path)[-1]
    assert (last["stage"], last["state"]) == ("proof", "failed")
    assert "The install is running, but the proof failed" in last["message"]
    assert last["fix"] == "Repair and prove again with: ./cognita install"


def test_a_stop_for_a_logout_writes_a_warning_and_keeps_exit_code_10(rig, tmp_path):
    rig.host.commands.discard("docker")
    path = with_progress(rig, tmp_path / "progress.jsonl")
    assert run_install(rig, "--install-docker", "yes") == cli.EXIT_RELOGIN
    warning = [line for line in progress_lines(path) if line["state"] == "warning"][0]
    assert warning["stage"] == "prerequisites" and "Log out fully and back in" in warning["message"]


def test_warnings_are_written_at_the_stage_that_raised_them(rig, tmp_path):
    rig.ocr_error = OSError("no network")
    rig.sh.script.insert(0, (("docker", "compose"), lambda argv: cli.Result(
        1 if "cognita.prefetch_models" in argv else 0, "2.29.0\n" if argv[2:3] == ["version"] else "", "")))
    path = with_progress(rig, tmp_path / "progress.jsonl")
    assert run_install(rig) == cli.EXIT_OK
    warnings = {line["stage"]: line["message"] for line in progress_lines(path) if line["state"] == "warning"}
    assert "OCR model files could not be downloaded" in warnings["ocr_weights"]
    assert "search models could not be downloaded" in warnings["models"]


def test_the_progress_file_holds_no_secret(rig, tmp_path):
    path = with_progress(rig, tmp_path / "progress.jsonl")
    rig.tool.qa_error = release.ReleaseError("verify-failed", "live self-test failed")
    run_install(rig)
    text = path.read_text(encoding="utf-8")
    secrets_dir = Path(rig.env()["COGNITA_SECRETS_ROOT"])
    for secret in [(secrets_dir / name).read_text().strip() for name in ("postgres.password", "postgres.dsn",
                                                                          "broker.secret")]:
        assert secret and secret not in text
    assert "correct horse" not in text


def test_a_write_failure_is_logged_once_and_never_fails_the_command(rig, tmp_path):
    blocker = tmp_path / "a-file"
    blocker.write_text("not a directory", encoding="utf-8")
    rig.ui.progress = cli.Progress(blocker / "progress.jsonl", log=rig.log, clock=lambda: FIXED_NOW)
    rig.ui.progress.start_command()
    assert run_install(rig) == cli.EXIT_OK
    log = "\n".join(p.read_text(encoding="utf-8") for p in Path(rig.data).rglob("install-*.log"))
    assert log.count("progress: cannot write") == 1 and "the command goes on" in log
    assert not rig.ui.progress.enabled


def test_the_progress_flag_is_on_every_command_that_runs_install_steps():
    parser = cli.build_parser()
    for command in (["install"], ["update"], ["rollback"], ["password"], ["add-folder", "/x"], ["remote-access"]):
        assert parser.parse_args([*command, "--progress-file", "p"]).progress_file == "p"
    for command in (["status"], ["logs"], ["uninstall"], ["reset", "index"]):
        with pytest.raises(SystemExit):
            parser.parse_args([*command, "--progress-file", "p"])


# --------------------------------------------------------------------------
# 19.2 documents displays
# --------------------------------------------------------------------------


@pytest.mark.parametrize("text", [WINDOWS_DISPLAY, "\\\\nas\\docs", "/mnt/docs and more", "Documents (Work)",
                                  "D:\\Aerzte und Bücher", "d:\\Mixed Case\\ok-1_2.3", "X" * 400])
def test_displays_the_rule_accepts(text):
    assert cli.display_problems(text) == []


@pytest.mark.parametrize("text, fragment", [
    ("", "1 to 400"), ("X" * 401, "1 to 400"),
    ('"D:\\quoted"', "must start with a letter"), ("'D:\\q'", "must start with a letter"),
    ("1:\\digits", "must start with a letter"), (" D:\\lead", "starts or ends with a space"),
    ("D:\\trail ", "starts or ends with a space"), ("D:\\a\nb", "newline"), ("D:\\tab\there", "cannot be shown"),
    ("D:\\has$dollar", "dollar sign"), ('D:\\has"quote', "double quote"), ("D:\\a #comment", "space then #"),
    ("\\\\nas\\docs$", "dollar sign"),
])
def test_displays_the_rule_refuses_with_the_reason(text, fragment):
    problems = cli.display_problems(text)
    assert problems and any(fragment in problem for problem in problems), problems


def test_a_hidden_share_is_refused_and_the_refusal_says_why(rig):
    args = rig.install_args("--non-interactive", "--documents-display", "\\\\nas\\docs$")
    with pytest.raises(cli.CliError) as info:
        cli.cmd_install(rig.ctx, args)
    text = str(info.value)
    assert "--documents-display" in text and "hidden shares" in text and "\\\\nas\\docs$" in text
    assert not Path(rig.data).exists() and not rig.env_path.exists()      # refused before anything changed


def test_backslashes_and_colons_are_allowed_in_a_display_but_not_in_the_path_rule():
    assert cli.display_problems("D:\\Documents") == []
    assert any("backslash" in problem for problem in cli.path_problems("/a\\b"))


def test_install_stores_the_display_next_to_the_root_in_the_env_file(rig):
    env = installed(rig, "--documents-display", WINDOWS_DISPLAY)
    assert env["COGNITA_PROJECTS_ROOT"] == rig.docs and env["COGNITA_PROJECTS_ROOT_DISPLAY"] == WINDOWS_DISPLAY
    assert f"COGNITA_PROJECTS_ROOT_DISPLAY={WINDOWS_DISPLAY}\n" in rig.env_path.read_text(encoding="utf-8")
    assert "Documents       " + WINDOWS_DISPLAY in rig.screen()          # the plan shows what a person knows it by


def test_no_display_means_no_key(rig):
    env = installed(rig)
    assert "COGNITA_PROJECTS_ROOT_DISPLAY" not in env and not any(key.endswith("_DISPLAY") for key in env)


def test_a_rerun_keeps_the_display_and_a_new_flag_updates_it(rig):
    installed(rig, "--documents-display", WINDOWS_DISPLAY)
    args = rig.args("install", "--non-interactive", "--yes", "--remote-access", "no",
                    "--admin-password-file", password_file(rig))
    assert cli.cmd_install(rig.ctx, args) == cli.EXIT_OK
    assert rig.env()["COGNITA_PROJECTS_ROOT_DISPLAY"] == WINDOWS_DISPLAY
    args = rig.args("install", "--non-interactive", "--yes", "--remote-access", "no",
                    "--admin-password-file", password_file(rig), "--documents-display", "E:\\Work")
    assert cli.cmd_install(rig.ctx, args) == cli.EXIT_OK
    assert rig.env()["COGNITA_PROJECTS_ROOT_DISPLAY"] == "E:\\Work"


def test_a_rerun_that_changes_documents_without_a_display_drops_the_old_display(rig):
    installed(rig, "--documents-display", WINDOWS_DISPLAY)
    other = f"{rig.root}/other docs"
    rig.host.add_dir(other)
    Path(other).mkdir()
    args = rig.args("install", "--non-interactive", "--yes", "--remote-access", "no", "--documents", other,
                    "--admin-password-file", password_file(rig))
    assert cli.cmd_install(rig.ctx, args) == cli.EXIT_OK
    env = rig.env()
    assert env["COGNITA_PROJECTS_ROOT"] == other and "COGNITA_PROJECTS_ROOT_DISPLAY" not in env
    log = "\n".join(p.read_text(encoding="utf-8") for p in Path(rig.data).rglob("install-*.log"))
    assert "dropping the old display" in log


def test_a_rerun_that_changes_documents_with_a_display_takes_the_new_one(rig):
    installed(rig, "--documents-display", WINDOWS_DISPLAY)
    other = f"{rig.root}/other docs"
    rig.host.add_dir(other)
    Path(other).mkdir()
    args = rig.args("install", "--non-interactive", "--yes", "--remote-access", "no", "--documents", other,
                    "--documents-display", "E:\\Other", "--admin-password-file", password_file(rig))
    assert cli.cmd_install(rig.ctx, args) == cli.EXIT_OK
    assert rig.env()["COGNITA_PROJECTS_ROOT_DISPLAY"] == "E:\\Other"


def test_add_folder_with_a_display_stores_it_under_the_slot_it_takes(rig):
    installed(rig, "--documents-display", WINDOWS_DISPLAY)
    second = f"{rig.root}/docs two"
    rig.host.add_dir(second)
    rig.tool.calls.clear()
    assert cli.cmd_add_folder(rig.ctx, rig.args("add-folder", second, "--display", "\\\\nas\\docs")) == cli.EXIT_OK
    env = rig.env()
    assert env["COGNITA_PROJECTS_ROOT_2"] == second and env["COGNITA_PROJECTS_ROOT_2_DISPLAY"] == "\\\\nas\\docs"
    assert env["COGNITA_PROJECTS_ROOT_DISPLAY"] == WINDOWS_DISPLAY
    assert "choose \\\\nas\\docs as its folder" in rig.screen()
    assert [c[0] for c in rig.tool.calls if c[0] in ("write_folders_fragment", "apply_release", "verify_release")] == [
        "write_folders_fragment", "apply_release", "verify_release"]


def test_add_folder_of_an_existing_root_with_a_new_display_updates_the_display(rig):
    installed(rig)
    rig.tool.calls.clear()
    assert cli.cmd_add_folder(rig.ctx, rig.args("add-folder", rig.docs, "--display", WINDOWS_DISPLAY)) == cli.EXIT_OK
    assert rig.env()["COGNITA_PROJECTS_ROOT_DISPLAY"] == WINDOWS_DISPLAY
    assert [c[0] for c in rig.tool.calls if c[0] in ("write_folders_fragment", "apply_release", "verify_release")] == [
        "write_folders_fragment", "apply_release", "verify_release"]        # it reaches the app: fragment + restart
    rig.tool.calls.clear()
    assert cli.cmd_add_folder(rig.ctx, rig.args("add-folder", rig.docs, "--display", WINDOWS_DISPLAY)) == cli.EXIT_OK
    assert rig.tool.calls == [] and "already one of" in rig.screen()         # the same display again: nothing to do


def test_add_folder_refuses_a_bad_display_before_changing_anything(rig):
    installed(rig)
    second = f"{rig.root}/docs two"
    rig.host.add_dir(second)
    before = rig.env_path.read_text(encoding="utf-8")
    with pytest.raises(cli.CliError) as info:
        cli.cmd_add_folder(rig.ctx, rig.args("add-folder", second, "--display", "D:\\a$b"))
    assert "--display" in str(info.value) and "dollar sign" in str(info.value)
    assert rig.env_path.read_text(encoding="utf-8") == before


def test_add_folder_addresses_the_display_by_slot_when_the_slots_have_a_gap(rig):
    env = installed(rig)
    env["COGNITA_PROJECTS_ROOT_3"] = f"{rig.root}/three"        # slot 2 is empty
    cli.write_env(rig.ctx, env)
    rig.host.add_dir(f"{rig.root}/three")
    assert cli.cmd_add_folder(rig.ctx, rig.args("add-folder", f"{rig.root}/three", "--display", "F:\\Three")) == cli.EXIT_OK
    assert rig.env()["COGNITA_PROJECTS_ROOT_3_DISPLAY"] == "F:\\Three"
    assert "COGNITA_PROJECTS_ROOT_2_DISPLAY" not in rig.env()


def test_the_finish_screen_shows_each_folder_the_way_a_person_knows_it(rig):
    installed(rig, "--documents-display", WINDOWS_DISPLAY)
    assert f"Documents    {WINDOWS_DISPLAY}" in rig.screen()


def test_a_display_round_trips_from_the_env_file_through_the_fragment_to_admin(rig, monkeypatch, tmp_path):
    """The whole chain 19.2 describes, with the real release.write_folders_fragment and the real Admin app."""
    import asyncio

    from httpx import ASGITransport, AsyncClient

    from cognita.admin_api import create_admin_app
    from cognita.config import CognitaConfig
    from cognita.registry import Registry

    env = installed(rig, "--documents-display", WINDOWS_DISPLAY)
    second = f"{rig.root}/docs two"
    rig.host.add_dir(second)
    cli.cmd_add_folder(rig.ctx, rig.args("add-folder", second, "--display", "\\\\nas\\docs"))

    # The real release tool wants POSIX roots (documents roots are Linux paths whatever host runs the tests),
    # so the roots are swapped for POSIX ones; the display keys are exactly what the CLI wrote.
    written = cli.read_env(rig.ctx)
    assert written["COGNITA_PROJECTS_ROOT_DISPLAY"] == WINDOWS_DISPLAY
    assert written["COGNITA_PROJECTS_ROOT_2_DISPLAY"] == "\\\\nas\\docs"
    written.update({"COGNITA_PROJECTS_ROOT": "/mnt/d/Documents", "COGNITA_PROJECTS_ROOT_2": "/mnt/nas"})
    posix_env = tmp_path / "roundtrip.env"
    cli.atomic_write(posix_env, cli.render_env(written))
    monkeypatch.setenv("COGNITA_LOCAL_ENV_FILE", str(posix_env))
    real_target = real_release.resolve_target("local")
    fragment_dir = tmp_path / "fragment"
    fragment_dir.mkdir()
    fragment = real_release.write_folders_fragment(real_target, fragment_dir)
    environment = yaml.safe_load(fragment.read_text(encoding="utf-8"))["services"]["cognita"]["environment"]
    assert json.loads(environment["COGNITA_DOCUMENT_ROOTS"]) == ["/mnt/d/Documents", "/mnt/nas"]   # still a list
    assert json.loads(environment["COGNITA_DOCUMENT_ROOT_DISPLAYS"]) == {
        "/mnt/d/Documents": WINDOWS_DISPLAY, "/mnt/nas": "\\\\nas\\docs"}
    monkeypatch.setenv("COGNITA_DOCUMENT_ROOTS", environment["COGNITA_DOCUMENT_ROOTS"])
    monkeypatch.setenv("COGNITA_DOCUMENT_ROOT_DISPLAYS", environment["COGNITA_DOCUMENT_ROOT_DISPLAYS"])

    async def ask() -> dict:
        config = CognitaConfig(registry_path=tmp_path / "r.yaml", data_root=tmp_path / "d",
                               public_base_url="https://cognita.example.com", admin_allowed_hosts=["*"],
                               admin_password_sha256="")
        app = create_admin_app(config, Registry(tmp_path / "r.yaml"))
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            return (await client.get("/api/document-roots")).json()

    assert asyncio.run(ask()) == {"roots": [{"path": "/mnt/d/Documents", "display": WINDOWS_DISPLAY},
                                            {"path": "/mnt/nas", "display": "\\\\nas\\docs"}]}
    assert env["COGNITA_PROJECTS_ROOT_DISPLAY"] == WINDOWS_DISPLAY


# --------------------------------------------------------------------------
# 19.4 --no-pull
# --------------------------------------------------------------------------


def test_no_pull_is_a_public_alias_of_the_hidden_after_pull():
    parser = cli.build_parser()
    assert parser.parse_args(["update", "--no-pull"]).after_pull is True
    assert parser.parse_args(["update", "--after-pull"]).after_pull is True
    assert parser.parse_args(["update"]).after_pull is False
    help_text = parser._subparsers._group_actions[0].choices["update"].format_help()
    assert "--no-pull" in help_text and "--after-pull" not in help_text


def test_update_no_pull_skips_the_dirty_check_the_pull_and_the_reexec(rig):
    installed(rig)
    rig.write_published("14.2.0")
    rig.sh.calls.clear()
    assert _update(rig, "--no-pull") == cli.EXIT_OK
    assert not any(argv[0] == "git" for argv in rig.sh.argvs())
    assert rig.reexec_calls == [] and rig.env()["COGNITA_VERSION"] == "14.2.0"


# --------------------------------------------------------------------------
# 19.6 the one command name
# --------------------------------------------------------------------------


def test_the_command_name_rewrites_only_the_launcher_token():
    ui = cli.UI(cli.InstallLog(None))
    ui.command = "cognita"
    assert ui.render("Run ./cognita install, then ./cognita status.") == "Run cognita install, then cognita status."
    assert ui.render("in /home/u/./cognita and ./cognita-x and x./cognita") == (
        "in /home/u/./cognita and ./cognita-x and x./cognita")
    ui.command = cli.DEFAULT_COMMAND
    assert ui.render("Run ./cognita install") == "Run ./cognita install"


@pytest.mark.parametrize("name, ok", [("cognita", True), ("./cognita", True), ("/usr/local/bin/cognita", True),
                                      ("cognita $HOME", False), ("cog nita", False), ('a"b', False), ("", False)])
def test_command_names_the_rule_accepts_and_refuses(name, ok):
    assert (cli.command_problems(name) == []) == ok


def test_install_records_the_command_name_and_the_finish_screen_uses_it(rig):
    env = installed(rig, "--command-name", "cognita")
    assert env["COGNITA_COMMAND"] == "cognita"
    screen = rig.screen()
    finish = screen[screen.index("is installed and working"):]
    for line in ("  cognita status            cognita logs [app|workspace] [-f]", "  cognita start|stop|restart",
                 "  cognita password          cognita add-folder PATH", "  cognita update            cognita rollback",
                 "  cognita reset index       cognita uninstall", "  cognita remote-access"):
        assert line in finish
    assert "./cognita" not in finish
    assert "not set up — cognita remote-access" in finish


def test_no_command_name_means_the_launcher_everywhere_as_before(rig):
    env = installed(rig)
    assert "COGNITA_COMMAND" not in env
    assert "  ./cognita status            ./cognita logs [app|workspace] [-f]" in rig.screen()


def test_every_message_the_user_reads_names_the_recorded_command(rig, capsys):
    env = installed(rig, "--command-name", "cognita")
    rig.sh.when("git", "-C", str(rig.repo), "status", "--porcelain", out=" M x\n")
    rig.ui.command = cli.DEFAULT_COMMAND                # main() sets it from the env file, as the next line does
    assert cli.main(["update", "--non-interactive", "--admin-password-file", password_file(rig)],
                    ctx=rig.ctx) == cli.EXIT_FAILED
    err = capsys.readouterr().err
    assert "then run cognita update." in err and "./cognita" not in err
    assert env["COGNITA_COMMAND"] == "cognita"


def test_the_env_file_comment_uses_the_command_name():
    text = cli.render_env({"COGNITA_COMMAND": "cognita", "COGNITA_VERSION": "1"})
    assert text.startswith("# Written by cognita install.") and "Change settings with cognita commands" in text
    assert cli.render_env({"COGNITA_VERSION": "1"}).startswith("# Written by ./cognita install.")


def test_a_bad_command_name_is_refused(rig):
    with pytest.raises(cli.CliError) as info:
        cli.cmd_install(rig.ctx, rig.install_args("--non-interactive", "--command-name", "cog nita"))
    assert "--command-name" in str(info.value)


# --------------------------------------------------------------------------
# 19.6 status --json
# --------------------------------------------------------------------------

JSON_KEYS = ["installed", "running", "version", "admin_url", "mcp_url", "public_url", "workspace", "acceleration",
             "proof"]


def status_json(rig) -> dict:
    rig.log.said.clear()
    assert cli.cmd_status(rig.ctx, rig.args("status", "--json")) == cli.EXIT_OK
    assert len(rig.log.said) == 1                       # one object and nothing else
    parsed = json.loads(rig.log.said[0])
    assert list(parsed) == JSON_KEYS
    return parsed


def test_status_json_on_a_machine_with_no_install_answers_and_does_not_fail(rig):
    assert status_json(rig) == {"installed": False, "running": False, "version": None, "admin_url": None,
                                "mcp_url": None, "public_url": None, "workspace": None, "acceleration": None,
                                "proof": None}
    assert rig.sh.calls == []                           # nothing was asked of the machine


def test_status_json_on_a_running_install(rig):
    installed(rig)
    rig.sh.when("systemctl", "--user", "is-active", out="active\n")
    rig.http_script = [(200, '{"status": "ok"}')]
    assert status_json(rig) == {
        "installed": True, "running": True, "version": "14.1.0", "admin_url": "http://127.0.0.1:8676",
        "mcp_url": "http://127.0.0.1:8675", "public_url": None, "workspace": "on", "acceleration": "cpu",
        "proof": "passed"}
    assert rig.http_calls[-1] == ("GET", "http://127.0.0.1:8675/healthz")


def test_status_json_on_a_stopped_install_is_not_running_and_asks_no_healthz(rig):
    installed(rig)
    rig.http_calls.clear()
    rig.sh.when("systemctl", "--user", "is-active", rc=3, out="inactive\n")
    result = status_json(rig)
    assert result["installed"] is True and result["running"] is False and result["version"] == "14.1.0"
    assert rig.http_calls == []


def test_status_json_running_needs_healthz_to_answer_too(rig):
    installed(rig)
    rig.sh.when("systemctl", "--user", "is-active", out="active\n")
    rig.http_script = [(None, "")]                      # the unit is up but nothing answers yet
    assert status_json(rig)["running"] is False


def test_status_json_reads_workspace_acceleration_and_version_from_the_current_release_after_a_rollback(rig):
    installed(rig)
    rig.write_published("14.2.0")
    _update(rig, "--after-pull")
    cli.cmd_rollback(rig.ctx, unattended(rig, "rollback"))
    # The env file describes what the NEXT staging uses; make it disagree with what runs.
    env = rig.env()
    env["COGNITA_WORKSPACE"], env["COGNITA_ACCELERATION"], env["COGNITA_VERSION"] = "off", "amd", "14.9.9"
    cli.write_env(rig.ctx, env)
    result = status_json(rig)
    assert (result["version"], result["workspace"], result["acceleration"]) == ("14.1.0", "on", "cpu")


def test_status_json_workspace_follows_a_core_release(rig):
    installed(rig, workspace="off")
    assert status_json(rig)["workspace"] == "off"


def test_status_json_with_an_env_file_but_no_release_is_not_installed(rig):
    env = installed(rig)
    (Path(env["COGNITA_RELEASES_ROOT"]) / "local" / "current" / "release.txt").unlink()
    result = status_json(rig)
    assert result["installed"] is False and result["version"] is None
    assert result["workspace"] is None and result["acceleration"] is None       # unknown is null, never an error
    assert result["admin_url"] == "http://127.0.0.1:8676"


def test_status_json_admin_url_is_https_only_when_cognita_yaml_turns_tls_on(rig):
    env = installed(rig)
    for name in ("admin_tls_certfile", "admin_tls_keyfile"):
        (Path(env["COGNITA_SECRETS_ROOT"]) / name).write_text("pem", encoding="utf-8")
    # Certificate files alone do not turn TLS on (kei's test target, P10a): the app reads cognita.yaml.
    assert status_json(rig)["admin_url"] == "http://127.0.0.1:8676"
    yaml_file = Path(env["COGNITA_CONFIG_ROOT"]) / "cognita.yaml"
    yaml_file.write_text(yaml_file.read_text(encoding="utf-8") + "admin_tls_certfile: /run/secrets/admin_tls_certfile\n"
                         "admin_tls_keyfile: /run/secrets/admin_tls_keyfile\n", encoding="utf-8")
    assert status_json(rig)["admin_url"] == "https://127.0.0.1:8676"


def _config_dir(env) -> Path:
    return Path(env["COGNITA_CONFIG_ROOT"])


def test_the_public_url_is_the_one_admin_saved_first_then_cognita_yaml_and_a_loopback_url_is_not_set(rig):
    env = installed(rig)
    assert status_json(rig)["public_url"] is None                       # the seed says http://127.0.0.1:8675
    yaml_file = _config_dir(env) / "cognita.yaml"
    yaml_file.write_text(yaml_file.read_text(encoding="utf-8").replace(
        "public_base_url: http://127.0.0.1:8675", 'public_base_url: "https://from-yaml.example.com"'), encoding="utf-8")
    assert status_json(rig)["public_url"] == "https://from-yaml.example.com"
    saved = _config_dir(env) / "data" / "public-base-url.json"
    saved.write_text(json.dumps({"version": 1, "public_base_url": "https://admin-saved.example.com"}), encoding="utf-8")
    assert status_json(rig)["public_url"] == "https://admin-saved.example.com"
    saved.write_text(json.dumps({"version": 1, "public_base_url": "http://localhost:9000"}), encoding="utf-8")
    assert status_json(rig)["public_url"] == "https://from-yaml.example.com"      # a loopback override is not set
    saved.write_text("not json", encoding="utf-8")                                 # unreadable: skipped, no error
    assert status_json(rig)["public_url"] == "https://from-yaml.example.com"


def test_the_text_status_uses_the_same_public_url_source(rig):
    env = installed(rig)
    saved = _config_dir(env) / "data" / "public-base-url.json"
    saved.write_text(json.dumps({"version": 1, "public_base_url": "https://admin-saved.example.com"}), encoding="utf-8")
    rig.log.said.clear()
    cli.cmd_status(rig.ctx, rig.args("status"))
    assert "Public: https://admin-saved.example.com" in rig.screen()


def test_main_dispatches_status_json_without_an_install_and_without_a_log_file(rig, capsys):
    assert cli.main(["status", "--json"], ctx=rig.ctx) == cli.EXIT_OK
    assert json.loads(rig.log.said[-1])["installed"] is False
    capsys.readouterr()


# --------------------------------------------------------------------------
# 19.5 diagnostics
# --------------------------------------------------------------------------


def _plant(rig, env) -> dict[str, str]:
    """Secrets that must never reach the zip, planted where a real run would leak them."""
    secrets_dir = Path(env["COGNITA_SECRETS_ROOT"])
    planted = {"postgres_password": (secrets_dir / "postgres.password").read_text().strip(),
               "dsn": (secrets_dir / "postgres.dsn").read_text().strip(),
               "broker": (secrets_dir / "broker.secret").read_text().strip(),
               "bearer": "b3arer-t0ken-abc123", "mcp": "MCPTOKENSEGMENT9", "generated": "gk-live-77",
               "static": "cognita_v2_AbCdEf123456", "api": "sk-api-0001"}
    (secrets_dir / "empty.secret").write_text("", encoding="utf-8")            # empty files are skipped
    log_dir = _config_dir(env) / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    app_lines = [f"line {n} ordinary" for n in range(2500)]
    app_lines[-3] = f"POST /mcp/{planted['mcp']}/messages 200"
    app_lines[-2] = f'Authorization: Bearer {planted["bearer"]}  {{"generated_key": "{planted["generated"]}", "api_key": "{planted["api"]}"}}'
    app_lines[-1] = f"static key {planted['static']} and the dsn {planted['dsn']}"
    (log_dir / "cognita.log").write_text("\n".join(app_lines) + "\n", encoding="utf-8")
    install_logs = Path(env["COGNITA_RELEASES_ROOT"]) / "local" / "logs"
    (install_logs / "install-planted.log").write_text(f"stderr: password={planted['postgres_password']} x\n",
                                                      encoding="utf-8")

    def compose_logs(argv):
        service = argv[-1]
        return cli.Result(0, f"{service} | traceback ... {planted['postgres_password']} ... broker {planted['broker']}\n"
                             f"{service} | POST /mcp/{planted['mcp']}/x\n", "")

    rig.sh.script.insert(0, (("docker", "compose"), lambda argv: (
        compose_logs(argv) if "logs" in argv else cli.Result(0, "cognita\npostgres\nworkspace-runtime\n"
                                                             if "--services" in argv else "2.29.0\n", ""))))
    return planted


def _zip_names(path: Path) -> list[str]:
    with zipfile.ZipFile(path) as bundle:
        return sorted(bundle.namelist())


def _zip_texts(path: Path) -> dict[str, str]:
    with zipfile.ZipFile(path) as bundle:
        return {name: bundle.read(name).decode("utf-8") for name in bundle.namelist()}


def test_the_zip_holds_exactly_the_documented_files(rig, tmp_path):
    env = installed(rig)
    _plant(rig, env)
    out = tmp_path / "out" / "diag.zip"
    assert cli.cmd_diagnostics(rig.ctx, rig.args("diagnostics", "--out", str(out))) == cli.EXIT_OK
    assert rig.log.said[-1] == str(out)                 # it prints its path
    names = _zip_names(out)
    assert names == sorted([
        "app-log-tail.txt", "disk.txt", "docker-ps.txt", "env.txt", "logs-cognita.txt", "logs-postgres.txt",
        "logs-workspace-runtime.txt", "release.txt", "status.json", "status.txt", "versions.txt",
        *[f"install-logs/{p.name}" for p in (Path(env["COGNITA_RELEASES_ROOT"]) / "local" / "logs").glob("*.log")]])
    assert "install-logs/install-planted.log" in names
    assert not [name for name in names if name.endswith(".error.txt")]


def test_files_that_must_never_be_included_are_not(rig, tmp_path):
    env = installed(rig)
    _plant(rig, env)
    out = tmp_path / "diag.zip"
    cli.cmd_diagnostics(rig.ctx, rig.args("diagnostics", "--out", str(out)))
    joined = "\n".join(_zip_names(out))
    for forbidden in ("secrets", "cognita.yaml", "authentication.yaml", "connectors.yaml", "postgres.password",
                      "broker.secret", "registry.yaml"):
        assert forbidden not in joined, forbidden


def test_no_planted_secret_appears_anywhere_in_the_zip(rig, tmp_path):
    env = installed(rig)
    planted = _plant(rig, env)
    out = tmp_path / "diag.zip"
    cli.cmd_diagnostics(rig.ctx, rig.args("diagnostics", "--out", str(out)))
    texts = _zip_texts(out)
    blob = "\n".join(texts.values())
    for label, secret in planted.items():
        assert secret not in blob, label
    assert "correct horse" not in blob                                  # the Admin password never was in reach
    assert cli.REDACTED in texts["logs-cognita.txt"] and cli.REDACTED in texts["app-log-tail.txt"]
    assert cli.REDACTED in texts["install-logs/install-planted.log"]
    # What is useful stays readable around the redactions.
    assert "POST /mcp/<redacted>/messages 200" in texts["app-log-tail.txt"]
    assert "Bearer <redacted>" in texts["app-log-tail.txt"]
    assert '"generated_key": "<redacted>"' in texts["app-log-tail.txt"] and '"api_key": "<redacted>"' in texts["app-log-tail.txt"]


def test_the_app_log_tail_is_the_last_two_thousand_lines(rig, tmp_path):
    env = installed(rig)
    _plant(rig, env)
    out = tmp_path / "diag.zip"
    cli.cmd_diagnostics(rig.ctx, rig.args("diagnostics", "--out", str(out)))
    lines = _zip_texts(out)["app-log-tail.txt"].splitlines()
    assert len(lines) == cli.DIAGNOSTIC_LOG_LINES == 2000
    assert lines[0] == "line 500 ordinary"


def test_env_txt_redacts_secret_valued_keys_and_keeps_the_display_paths_and_roots(rig, tmp_path):
    installed(rig, "--documents-display", WINDOWS_DISPLAY)
    with rig.env_path.open("a", encoding="utf-8") as stream:
        stream.write("COGNITA_SOMETHING_PASSWORD=hunter2\nCOGNITA_API_KEY=k-1\nCOGNITA_DB_DSN=postgresql://u:p@h/d\n"
                     "COGNITA_X_TOKEN=t\nCOGNITA_A_SECRET=s\n")
    out = tmp_path / "diag.zip"
    cli.cmd_diagnostics(rig.ctx, rig.args("diagnostics", "--out", str(out)))
    text = _zip_texts(out)["env.txt"]
    for key in ("COGNITA_SOMETHING_PASSWORD", "COGNITA_API_KEY", "COGNITA_DB_DSN", "COGNITA_X_TOKEN",
                "COGNITA_A_SECRET"):
        assert f"{key}={cli.REDACTED}" in text
    for secret in ("hunter2", "k-1", "postgresql://u:p@h/d"):
        assert secret not in text
    assert f"COGNITA_PROJECTS_ROOT={rig.docs}" in text and f"COGNITA_PROJECTS_ROOT_DISPLAY={WINDOWS_DISPLAY}" in text


def test_a_collector_that_fails_writes_its_error_file_and_the_others_still_run(rig, tmp_path, monkeypatch):
    installed(rig)

    def broken(_target):
        raise RuntimeError("status is on fire")

    monkeypatch.setattr(release, "status_lines", broken)
    out = tmp_path / "diag.zip"
    assert cli.cmd_diagnostics(rig.ctx, rig.args("diagnostics", "--out", str(out))) == cli.EXIT_OK
    texts = _zip_texts(out)
    assert "status is on fire" in texts["status.error.txt"] and "status.txt" not in texts
    assert "status.json" in texts and "env.txt" in texts and "release.txt" in texts     # the others ran
    assert "app-log-tail.error.txt" in texts                                         # no cognita.log yet: says why
    assert "No such file" in texts["app-log-tail.error.txt"] or "cognita.log" in texts["app-log-tail.error.txt"]


def test_diagnostics_works_on_a_machine_with_nothing_installed(rig, tmp_path):
    out = tmp_path / "diag.zip"
    assert cli.cmd_diagnostics(rig.ctx, rig.args("diagnostics", "--out", str(out))) == cli.EXIT_OK
    names = _zip_names(out)
    assert "install-logs.error.txt" in names and "env.error.txt" in names and "status.json" in names
    assert json.loads(_zip_texts(out)["status.json"])["installed"] is False


def test_diagnostics_takes_no_lock_and_needs_no_admin_login(rig, tmp_path, monkeypatch):
    installed(rig)
    rig.admin_state.calls.clear()

    def no_lock(*_a, **_k):
        raise AssertionError("diagnostics must not take the install lock")

    monkeypatch.setattr(cli, "rel_target_lock", no_lock)
    cli.cmd_diagnostics(rig.ctx, rig.args("diagnostics", "--out", str(tmp_path / "d.zip")))
    assert rig.admin_state.calls == []


def test_a_zip_that_cannot_be_written_fails_the_command_and_leaves_no_partial_file(rig, tmp_path):
    blocker = tmp_path / "a-file"
    blocker.write_text("x", encoding="utf-8")
    with pytest.raises(cli.CliError) as info:
        cli.cmd_diagnostics(rig.ctx, rig.args("diagnostics", "--out", str(blocker / "d.zip")))
    assert "Could not write the diagnostics file" in str(info.value) and "--out FILE" in (info.value.hint or "")
    assert not list(tmp_path.glob("*.partial"))


def test_the_disk_and_version_collectors_ask_for_each_directory_and_tool(rig, tmp_path):
    env = installed(rig)
    out = tmp_path / "diag.zip"
    cli.cmd_diagnostics(rig.ctx, rig.args("diagnostics", "--out", str(out)))
    asked = rig.sh.joined("capture")
    assert f"df -h {cli.data_dir_of(env)}" in asked and f"df -h {rig.docs}" in asked
    assert "df -h /var/lib/docker" in asked
    for key in ("COGNITA_CONFIG_ROOT", "COGNITA_POSTGRES_DATA_ROOT", "COGNITA_MODEL_CACHE_ROOT"):
        assert f"du -sh {env[key]}" in asked
    assert "docker version" in asked and "docker compose version" in asked and "uname -r" in asked
    assert "docker ps -a" in asked


# --------------------------------------------------------------------------
# The scrubber itself
# --------------------------------------------------------------------------


def test_the_scrubber_replaces_each_pattern_and_the_literals_longest_first():
    scrub = cli.Scrubber(["topsecret", "postgresql://cognita:topsecret@postgres:5432/cognita", ""]).scrub
    assert scrub("dsn postgresql://cognita:topsecret@postgres:5432/cognita end") == "dsn <redacted> end"
    assert scrub("the topsecret word") == "the <redacted> word"
    assert scrub("GET /mcp/abcDEF123/tools?x=1") == "GET /mcp/<redacted>/tools?x=1"
    assert scrub("Authorization: bearer abc.DEF-123~x+y/z=") == "Authorization: bearer <redacted>"
    assert scrub('{"password": "p\\"w", "user": "u"}') == '{"password": "<redacted>", "user": "u"}'
    assert scrub('{"generated_key": "k", "API_KEY": "k2"}') == '{"generated_key": "<redacted>", "API_KEY": "<redacted>"}'
    assert scrub("key cognita_v2_AbC-123_x here") == "key <redacted> here"
    assert scrub("nothing to hide") == "nothing to hide"


def test_secret_literals_skip_empty_files_and_include_the_stripped_form(rig, tmp_path):
    secrets_dir = tmp_path / "secrets"
    secrets_dir.mkdir()
    (secrets_dir / "a").write_bytes(b"value-a\n")         # bytes: no newline translation on any host
    (secrets_dir / "empty").write_bytes(b"")
    (secrets_dir / "blank").write_bytes(b"  \n")
    (secrets_dir / "binary").write_bytes(b"\xff\xfe\x00")
    found = cli.secret_literals(rig.ctx, {"COGNITA_SECRETS_ROOT": str(secrets_dir)})
    assert sorted(found) == ["value-a", "value-a\n"]


def test_redact_env_text_leaves_comments_and_other_keys_alone():
    text = "# a KEY comment\nCOGNITA_VERSION=14.1.0\nCOGNITA_X_KEY=abc\n"
    assert cli.redact_env_text(text) == f"# a KEY comment\nCOGNITA_VERSION=14.1.0\nCOGNITA_X_KEY={cli.REDACTED}\n"
