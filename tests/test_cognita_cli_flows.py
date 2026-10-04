"""Flow tests for scripts/cognita_cli.py: install, the proof, GPU fallback, remote access, add-folder,
update, rollback, uninstall, adopt.  All fakes live in tests/test_cognita_cli.py.

Nothing here waits: the Funnel retry loop gets a recording sleeper, the model-size ticker is driven by
the fake runner, and no command, Docker, systemd, network or Admin server is real.
"""
from __future__ import annotations

import datetime
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_cognita_cli import FakeAdmin, Rig, cli, release  # noqa: F401  (FakeAdmin re-exported for readers)

PASSWORD = "correct horse"


@pytest.fixture
def rig(tmp_path, monkeypatch):
    rig = Rig(tmp_path, monkeypatch)
    rig.ocr_calls = []
    rig.ocr_error = None

    def fetch(dest, log, progress=None):
        rig.ocr_calls.append(str(dest))
        if rig.ocr_error:
            raise rig.ocr_error

    monkeypatch.setitem(sys.modules, "ocr_weights", SimpleNamespace(fetch=fetch))
    return rig


def password_file(rig) -> str:
    path = rig.tmp / "pw.txt"
    path.write_text(PASSWORD + "\n", encoding="utf-8")
    return str(path)


def run_install(rig, *extra, workspace="on") -> int:
    args = rig.install_args("--non-interactive", "--yes", "--workspace", workspace, "--remote-access", "no",
                            "--admin-password-file", password_file(rig), *extra)
    rig.ui.non_interactive = True          # main() builds the UI from --non-interactive; the tests do it here
    return cli.cmd_install(rig.ctx, args)


def unattended(rig, command, *extra):
    rig.ui.non_interactive = True
    return rig.args(command, "--non-interactive", "--admin-password-file", password_file(rig), *extra)


def installed(rig, **kw) -> dict:
    assert run_install(rig, **kw) == cli.EXIT_OK
    return rig.env()


def target(rig):
    return rig.tool.resolve_target("local")


def positions(rig, *needles) -> list[int]:
    """Where each needle first appears in the recorded call order (runner commands, release calls, Admin)."""
    flat = [" ".join(str(x) for x in item) for item in rig.order]
    found = []
    for needle in needles:
        found.append(next(i for i, text in enumerate(flat) if needle in text))
    return found


# --------------------------------------------------------------------------
# install: the happy path
# --------------------------------------------------------------------------


def test_install_runs_the_twelve_steps_in_order_and_ends_on_the_finish_screen(rig):
    rig.sh.ticks = 3
    rig.host.sizes[f"{rig.data}/models"] = 1_100_000_000
    assert run_install(rig) == cli.EXIT_OK

    stage, creds, prefetch, linger, apply_, enable, verify, login, qa = positions(
        rig, "release stage_published", "sac.py", "cognita.prefetch_models", "enable-linger",
        "release apply_release", "release enable_unit", "release verify_release", "admin LOGIN",
        "release qa_release")
    assert stage < creds < prefetch < linger < apply_ < enable < verify < login < qa

    env = rig.env()
    assert env["COGNITA_RELEASE_TARGET"] == "local" and env["COGNITA_VERSION"] == "14.1.0"
    assert env["COGNITA_WORKSPACE"] == "on" and env["COGNITA_ACCELERATION"] == "cpu"
    assert env["COGNITA_LINGER_SET_BY_INSTALLER"] == "1"
    assert env["COGNITA_PROJECTS_ROOT"] == rig.docs

    log = Path(env["COGNITA_RELEASES_ROOT"]) / "local" / "logs"
    installs = list(log.glob("install-*.log"))
    assert len(installs) == 1
    text = installs[0].read_text(encoding="utf-8")
    assert "published: version=14.1.0" in text and "check:" in text        # steps 1-4 are in the file too
    assert "layout: generated postgres.password (never printed)" in text
    secret = (Path(env["COGNITA_SECRETS_ROOT"]) / "postgres.password").read_text().strip()
    assert secret not in text and PASSWORD not in text

    screen = "\n".join(installs[0].read_text(encoding="utf-8").splitlines())
    assert "Cognita 14.1.0 is installed and working." in screen
    assert "Downloading Cognita images: about" in screen and "minutes on a 100 Mbit/s line." in screen
    assert "Search models: 1.1 GB of about 3.6 GB" in screen
    assert "Proof passed" in screen
    assert rig.admin_state.projects == [] and rig.admin_state.connectors == []     # the proof cleaned up
    assert rig.ocr_calls == [str(Path(rig.data) / "models" / "easyocr")]


def test_the_admin_password_travels_only_on_stdin_as_json_and_never_in_argv_or_the_log(rig):
    run_install(rig)
    creds = [(argv, stdin) for kind, argv, stdin in rig.sh.calls if "sac.py" in " ".join(argv) or
             (stdin and "password" in stdin)]
    assert len(creds) == 1
    argv, stdin = creds[0]
    assert json.loads(stdin) == {"username": "admin", "password": PASSWORD}
    assert PASSWORD not in " ".join(argv)
    assert "--stdin-json" in argv and "--entrypoint" in argv and "--no-deps" in argv and "-T" in argv
    assert any(part.endswith("scripts/set-admin-credentials.py:/tmp/sac.py:ro") or
               part.endswith("set-admin-credentials.py:/tmp/sac.py:ro") for part in argv)
    for kind, call_argv, call_stdin in rig.sh.calls:
        assert PASSWORD not in " ".join(call_argv)
        if call_stdin and call_argv is not argv:
            assert PASSWORD not in call_stdin
    everything = "\n".join(p.read_text(encoding="utf-8") for p in Path(rig.data).rglob("*.log"))
    assert PASSWORD not in everything
    assert PASSWORD not in json.dumps(rig.admin_state.calls, default=str)   # Admin got it in the login body only


def test_nothing_is_created_when_a_check_fails(rig):
    rig.host.busy_ports = {8675}
    with pytest.raises(cli.CliError) as info:
        run_install(rig)
    assert "check(s) failed" in str(info.value)
    assert not Path(rig.data).exists() and not rig.env_path.exists()
    assert rig.tool.calls == []                       # release.py was not touched either


def test_declining_the_plan_changes_nothing(rig):
    rig.input.answers = ["n"]
    args = rig.install_args("--workspace", "on", "--remote-access", "no",
                            "--admin-password-file", password_file(rig))
    with pytest.raises(cli.CliError) as info:
        cli.cmd_install(rig.ctx, args)
    assert "Nothing was changed" in str(info.value)
    assert not Path(rig.data).exists() and not rig.env_path.exists()


def test_missing_flags_are_named_in_a_non_interactive_run(rig):
    rig.ui.non_interactive = True
    with pytest.raises(cli.CliError) as info:
        cli.cmd_install(rig.ctx, rig.args("install", "--non-interactive"))
    assert "--documents" in (info.value.hint or "")


def test_a_documents_folder_that_contains_the_data_dir_is_refused_before_any_check(rig):
    args = rig.args("install", "--documents", rig.root, "--data-dir", rig.data, "--admin-user", "admin",
                    "--non-interactive")
    with pytest.raises(cli.CliError) as info:
        cli.cmd_install(rig.ctx, args)
    assert "overlap" in str(info.value)


def test_a_path_the_rules_reject_is_refused_with_the_reason(rig):
    rig.ctx.check_paths = cli.path_problems
    args = rig.args("install", "--documents", "/home/u/My $Docs", "--data-dir", "/home/u/data",
                    "--admin-user", "admin", "--non-interactive")
    with pytest.raises(cli.CliError) as info:
        cli.cmd_install(rig.ctx, args)
    assert "--documents" in str(info.value) and "dollar sign" in str(info.value)


def test_workspace_off_stages_core_and_proves_core(rig):
    env = installed(rig, workspace="off")
    assert env["COGNITA_WORKSPACE"] == "off" and "COGNITA_KVM_GID" not in env
    assert ("stage_published", "cpu", "off") in rig.tool.calls
    made = [body for method, path, body in rig.admin_state.calls if (method, path) == ("POST", "/api/connectors")]
    assert made[0]["workspace_enabled"] is False and made[0]["default_workspace_transfer"] == "deny"


def test_no_kvm_prints_the_c7_message_and_installs_core(rig):
    rig.host.chars.clear()
    assert run_install(rig, workspace="on") == cli.EXIT_OK
    assert rig.env()["COGNITA_WORKSPACE"] == "off"
    text = "\n".join(p.read_text(encoding="utf-8") for p in Path(rig.data).rglob("install-*.log"))
    assert cli.KVM_MESSAGE in text


def test_a_failed_model_prefetch_is_a_warning_above_the_finish_screen_not_a_stop(rig):
    # Only the prefetch container run fails; `docker compose version` and the rest keep answering.
    rig.sh.script.insert(0, (("docker", "compose"), lambda argv: cli.Result(
        1 if "cognita.prefetch_models" in argv else 0, "2.29.0\n" if argv[2:3] == ["version"] else "", "")))
    assert run_install(rig) == cli.EXIT_OK
    log = next(Path(rig.data).rglob("install-*.log")).read_text(encoding="utf-8")
    assert "Search will use plain ranking" in log and log.index("Search will use plain ranking") < log.index(
        "is installed and working")


def test_missing_ocr_weights_module_or_download_failure_warns_and_continues(rig):
    rig.ocr_error = OSError("no network")
    assert run_install(rig) == cli.EXIT_OK
    log = next(Path(rig.data).rglob("install-*.log")).read_text(encoding="utf-8")
    assert "OCR model files could not be downloaded" in log


def test_a_proof_failure_leaves_the_install_running_and_names_the_rerun_command(rig):
    rig.tool.qa_error = release.ReleaseError("verify-failed", "live self-test failed")
    assert run_install(rig) == cli.EXIT_FAILED
    log = next(Path(rig.data).rglob("install-*.log")).read_text(encoding="utf-8")
    assert "the proof failed" in log and "./cognita install" in log and "Log:" in log
    assert "enable_unit" in rig.tool.names() and "select_release" not in rig.tool.names()   # no rollback
    assert rig.admin_state.projects == [] and rig.admin_state.connectors == []            # cleaned up anyway
    assert "is installed and working" not in log
    assert cli.status_data(rig.ctx)["proof"] is None


def test_a_failed_repair_proof_clears_the_previous_passed_result(rig):
    installed(rig)
    rig.tool.qa_error = release.ReleaseError("verify-failed", "repair self-test failed")
    assert run_install(rig) == cli.EXIT_FAILED
    env = rig.env()
    assert cli.PROOF_KEY not in env
    assert cli.status_data(rig.ctx)["proof"] is None
    rig.log.said.clear()
    assert cli.cmd_status(rig.ctx, rig.args("status")) == cli.EXIT_OK
    assert "Self-tests: unverified (run ./cognita install to repair and verify)" in rig.log.said


def test_the_remote_access_offer_at_the_end_publishes_the_address_on_the_finish_screen(rig):
    rig.host.commands.add("tailscale")
    rig.sh.when("tailscale", "status", "--json",
                out=json.dumps({"BackendState": "Running", "Self": {"DNSName": "cognita-box.tail1234.ts.net."}}))
    rig.http_script = [(200, '{"service": "cognita"}'), (401, "")]
    args = rig.install_args("--non-interactive", "--yes", "--workspace", "on", "--remote-access", "yes",
                            "--admin-password-file", password_file(rig))
    assert cli.cmd_install(rig.ctx, args) == cli.EXIT_OK
    log = next(Path(rig.data).rglob("install-*.log")).read_text(encoding="utf-8")
    assert "Public       https://cognita-box.tail1234.ts.net" in log
    assert rig.admin_state.public_url == "https://cognita-box.tail1234.ts.net"


# --------------------------------------------------------------------------
# Step 2: Docker, and the group change (proven: PROOF-LINUX-INSTALL.md, P3)
# --------------------------------------------------------------------------


def _docker_missing(rig):
    rig.host.commands.discard("docker")


def test_docker_missing_installs_it_with_consent_then_stops_with_the_logout_message(rig):
    _docker_missing(rig)
    seen_before_run = []

    def note(kind, argv, stdin):
        if kind == "interactive":
            seen_before_run.append("\n".join(rig.log._pending))

    rig.sh.on_call = note
    code = run_install(rig, "--install-docker", "yes")
    assert code == cli.EXIT_RELOGIN
    interactive = rig.sh.joined("interactive")
    assert interactive[0] == "sudo apt-get update"
    assert any("download.docker.com/linux/ubuntu/gpg" in c for c in interactive)
    assert any(c.endswith("docker.list") and "install -m 0644" in c for c in interactive)
    assert ("sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin "
            "docker-compose-plugin") in interactive
    assert interactive[-1] == "sudo usermod -aG docker tester"
    for index, command in enumerate(interactive):     # each sudo command was announced with its reason first
        assert f"sudo {command[5:]}" in seen_before_run[index] and "why:" in seen_before_run[index]
    listing = next(iter(rig.host.temp.values()))
    assert listing == ("deb [arch=amd64 signed-by=/etc/apt/keyrings/docker.asc] "
                       "https://download.docker.com/linux/ubuntu noble stable\n")
    assert rig.host.removed                                                # the temp list file is cleaned up
    text = rig.screen()
    assert ("Docker is installed. Your account was added to the docker group, which takes effect at your "
            "next login.") in text and "Log out fully and back in (or reboot)" in text
    assert not Path(rig.data).exists() and not rig.env_path.exists()      # nothing beyond Docker was changed


def test_docker_missing_and_linger_already_on_says_reboot(rig):
    _docker_missing(rig)
    rig.sh.script.insert(0, (("loginctl", "show-user"), cli.Result(0, "Linger=yes\n")))
    assert run_install(rig, "--install-docker", "yes") == cli.EXIT_RELOGIN
    text = rig.screen()
    assert "Reboot, then run ./cognita install again." in text and "linger is already on" in text


def test_declining_docker_installs_nothing(rig):
    _docker_missing(rig)
    with pytest.raises(cli.CliError) as info:
        run_install(rig, "--install-docker", "no")
    assert "Docker Engine is required" in str(info.value)
    assert rig.sh.argvs("interactive") == []


def test_a_docker_install_needs_an_answer_when_non_interactive(rig):
    _docker_missing(rig)
    with pytest.raises(cli.CliError) as info:
        run_install(rig)
    assert "--install-docker" in (info.value.hint or "")
    assert rig.sh.argvs("interactive") == []


def test_a_user_who_is_in_the_group_but_has_an_old_session_is_told_to_log_in_again(rig):
    rig.sh.script.insert(0, (("docker", "version"), cli.Result(1, "", "permission denied")))
    assert run_install(rig) == cli.EXIT_RELOGIN
    assert "next login" in rig.screen() and rig.sh.argvs("interactive") == []


def test_a_user_not_in_the_group_is_offered_the_group_change(rig):
    rig.sh.script.insert(0, (("docker", "version"), cli.Result(1, "", "permission denied")))
    rig.sh.script.insert(0, (("getent", "group", "docker"), cli.Result(0, "docker:x:999:someoneelse\n")))
    assert run_install(rig, "--install-docker", "yes") == cli.EXIT_RELOGIN
    assert rig.sh.joined("interactive") == ["sudo usermod -aG docker tester"]


def test_a_shell_that_reaches_docker_while_the_systemd_manager_cannot_stops_the_run(rig):
    rig.sh.script.insert(0, (("systemd-run",), cli.Result(1, "", "permission denied")))
    assert run_install(rig) == cli.EXIT_RELOGIN
    text = rig.screen()
    assert "not for your systemd user manager" in text and "Log out fully and back in" in text
    assert not Path(rig.data).exists()
    rig.sh.script.insert(0, (("loginctl", "show-user"), cli.Result(0, "Linger=yes\n")))
    rig.log._pending.clear()
    assert run_install(rig) == cli.EXIT_RELOGIN
    assert "Reboot" in rig.screen()


# --------------------------------------------------------------------------
# Rerun (C13)
# --------------------------------------------------------------------------


def _give_admin_a_hash(env):
    path = Path(env["COGNITA_CONFIG_ROOT"]) / "cognita.yaml"
    text = path.read_text(encoding="utf-8").replace('admin_password_hash: ""', "admin_password_hash: $argon2id$v=19$abc")
    path.write_text(text, encoding="utf-8")


def test_a_rerun_keeps_every_secret_and_answer_asks_nothing_and_sets_no_password(rig):
    env = installed(rig)
    _give_admin_a_hash(env)
    secrets_dir = Path(env["COGNITA_SECRETS_ROOT"])
    before = {p.name: p.read_bytes() for p in secrets_dir.iterdir()}
    creds_before = sum("sac.py" in " ".join(a) for _k, a, _s in rig.sh.calls)
    rig.sh.script.insert(0, (("docker", "ps"), cli.Result(0, "abc\n")))     # our own containers are up
    rig.host.busy_ports = {8675, 8676}
    args = rig.args("install", "--non-interactive", "--yes", "--remote-access", "no",
                    "--admin-password-file", password_file(rig))
    assert cli.cmd_install(rig.ctx, args) == cli.EXIT_OK                   # no --documents, no --data-dir, no --admin-user
    assert {p.name: p.read_bytes() for p in secrets_dir.iterdir()} == before
    assert sum("sac.py" in " ".join(a) for _k, a, _s in rig.sh.calls) == creds_before   # hash present: no password set
    assert rig.env()["COGNITA_PROJECTS_ROOT"] == rig.docs
    assert rig.tool.names().count("stage_published") == 2 and rig.tool.names().count("apply_release") == 2


def test_a_flag_that_differs_from_the_env_file_is_a_change_applied_and_logged(rig):
    env = installed(rig, workspace="on")
    _give_admin_a_hash(env)
    args = rig.args("install", "--non-interactive", "--yes", "--workspace", "off", "--remote-access", "no",
                    "--admin-password-file", password_file(rig))
    assert cli.cmd_install(rig.ctx, args) == cli.EXIT_OK
    assert rig.env()["COGNITA_WORKSPACE"] == "off" and "COGNITA_KVM_GID" not in rig.env()
    stages = [c for c in rig.tool.calls if c[0] == "stage_published"]
    assert [s[2] for s in stages] == ["on", "off"]      # the current version was re-staged in the new mode


def test_changing_data_dir_on_a_rerun_is_refused_because_data_cannot_be_moved(rig):
    installed(rig)
    args = rig.args("install", "--non-interactive", "--data-dir", f"{rig.root}/elsewhere")
    with pytest.raises(cli.CliError) as info:
        cli.cmd_install(rig.ctx, args)
    assert "keeps its data in" in str(info.value)


# --------------------------------------------------------------------------
# Acceleration (section 8, C6)
# --------------------------------------------------------------------------


def _amd_machine(rig):
    rig.host.existing |= {"/dev/kfd", "/sys/module/amdgpu"}
    rig.host.globs["/dev/dri/renderD*"] = ["/dev/dri/renderD128"]
    rig.host.globs["/dev/dri/card*"] = ["/dev/dri/card1"]
    rig.host.gids.update({"/dev/dri/card1": 44, "/dev/dri/renderD128": 992})


def test_amd_is_turned_on_through_admin_restarted_verified_and_proven_on_the_gpu(rig):
    _amd_machine(rig)
    assert run_install(rig, "--acceleration", "amd") == cli.EXIT_OK
    env = rig.env()
    assert env["COGNITA_ACCELERATION"] == "amd" and env["COGNITA_VIDEO_GID"] == "44" and env["COGNITA_RENDER_GID"] == "992"
    patches = [b for m, p, b in rig.admin_state.calls if (m, p) == ("PATCH", "/api/settings/gpu-acceleration")]
    assert len(patches) == 1
    assert patches[0]["knowledge"] == {"gpu_enabled": True, "gpu_device_ids": []}
    assert patches[0]["ocr"] == {"device": "gpu", "gpu_device_ids": []}
    assert patches[0]["expected_revision"] == 0 and len(patches[0]["idempotency_token"]) == 36
    # The response said restart.required, so the app container was recreated before the verify.
    restart = next(i for i, c in enumerate(rig.sh.joined("stream")) if "--force-recreate cognita" in c)
    assert restart >= 0
    calls = [f"{m} {p}" for m, p, _b in rig.admin_state.calls]
    assert calls.index("POST /api/settings/gpu-acceleration/verify") > calls.index("PATCH /api/settings/gpu-acceleration")
    assert ("qa_release", "14.1.0", "amd") in rig.tool.calls
    assert rig.tool.names().count("stage_published") == 1               # no fallback


def test_an_unverified_gpu_falls_back_to_cpu_says_why_and_the_install_still_succeeds(rig):
    _amd_machine(rig)
    rig.admin_state.verify_result = {"state": "failed", "cards": [], "cleanup": "passed",
                                     "runtimes": {"embedding": {"reason": "canary_failed"}, "ocr": {}}}
    assert run_install(rig, "--acceleration", "amd") == cli.EXIT_OK
    log = next(Path(rig.data).rglob("install-*.log")).read_text(encoding="utf-8")
    assert "AMD acceleration did not verify: embedding: canary_failed. Switching to CPU." in log
    env = rig.env()
    assert env["COGNITA_ACCELERATION"] == "cpu"
    patches = [b for m, p, b in rig.admin_state.calls if (m, p) == ("PATCH", "/api/settings/gpu-acceleration")]
    assert patches[-1]["knowledge"]["gpu_enabled"] is False and patches[-1]["ocr"]["device"] == "cpu"
    assert [c[1] for c in rig.tool.calls if c[0] == "stage_published"] == ["amd", "cpu"]
    assert [c[3] for c in rig.tool.calls if c[0] == "apply_release"] == ["amd", "cpu"]
    assert [c[2] for c in rig.tool.calls if c[0] == "qa_release"] == ["cpu"]      # the proof ran once, on the CPU
    assert "Cognita 14.1.0 is installed and working." in log and "AMD acceleration did not verify" in log
    # The same version was re-staged for the CPU, so its release.txt no longer names the AMD app image:
    # it is removed now or nothing ever would (P8, 2026-09-29).  What the CPU release still uses stays.
    removed = [a[-1] for a in rig.sh.argvs("capture") if a[:3] == ["docker", "image", "rm"]]
    assert removed == ["cognita/app:14.1.0-local-amd-abc123"]


def test_a_proof_that_fails_on_the_gpu_also_falls_back_to_cpu_and_proves_again(rig):
    _amd_machine(rig)
    rig.tool.qa_fail_profiles = {"amd"}
    assert run_install(rig, "--acceleration", "amd") == cli.EXIT_OK
    assert rig.env()["COGNITA_ACCELERATION"] == "cpu"
    assert [c[2] for c in rig.tool.calls if c[0] == "qa_release"] == ["amd", "cpu"]
    assert rig.admin_state.projects == [] and rig.admin_state.connectors == []


def test_an_admin_error_while_enabling_the_gpu_never_fails_the_install(rig):
    _amd_machine(rig)
    rig.admin_state.fail[("PATCH", "/api/settings/gpu-acceleration")] = cli.AdminError(503, "configuration_invalid")
    assert run_install(rig, "--acceleration", "amd") == cli.EXIT_OK
    assert rig.env()["COGNITA_ACCELERATION"] == "cpu"


def test_an_amd_card_with_no_published_amd_image_installs_for_the_cpu_and_says_so(rig):
    _amd_machine(rig)
    published = rig.repo / "containers" / "published-release.txt"
    published.write_text("\n".join(line for line in published.read_text().splitlines()
                                   if "image_ref_cognita_amd" not in line) + "\n", encoding="utf-8")
    assert run_install(rig) == cli.EXIT_OK
    assert rig.env()["COGNITA_ACCELERATION"] == "cpu"
    assert "published release has no AMD image" in rig.screen()
    with pytest.raises(cli.CliError) as info:
        run_install(rig, "--acceleration", "amd")
    assert "no AMD image" in str(info.value)


def test_a_failure_names_the_step_it_happened_in(rig, capsys):
    rig.tool.stage_error = release.ReleaseError("build-failed", "pull failed: no route to host")
    rig.ui.non_interactive = True
    argv = ["install", "--documents", rig.docs, "--data-dir", rig.data, "--admin-user", "admin",
            "--non-interactive", "--yes", "--workspace", "on", "--remote-access", "no",
            "--admin-password-file", password_file(rig)]
    assert cli.main(argv, ctx=rig.ctx) == release.EXIT_CODES["build-failed"]
    err = capsys.readouterr().err
    assert "Error (build-failed) during [6/12] Downloading Cognita: pull failed: no route to host" in err
    assert "Log:" in err and "run the same command again" in err


def test_asking_for_amd_on_a_machine_without_one_is_refused_plainly(rig):
    with pytest.raises(cli.CliError) as info:
        run_install(rig, "--acceleration", "amd")
    assert "no AMD GPU" in str(info.value)


def test_the_acceleration_question_is_asked_only_when_an_amd_card_qualifies(rig):
    _amd_machine(rig)
    rig.ui.non_interactive = False
    rig.input.answers = ["cpu"]
    args = rig.install_args("--yes", "--workspace", "on", "--remote-access", "no",
                            "--admin-password-file", password_file(rig))
    assert cli.cmd_install(rig.ctx, args) == cli.EXIT_OK
    assert any("Acceleration" in prompt for prompt in rig.input.prompts)
    assert rig.env()["COGNITA_ACCELERATION"] == "cpu"


# --------------------------------------------------------------------------
# The proof (section 7.5)
# --------------------------------------------------------------------------


def _proof(rig, env, password=PASSWORD):
    cli.run_proof(rig.ctx, env, target(rig), "admin", password)


def _delete_calls(rig):
    return [(m, p) for m, p, _b in rig.admin_state.calls if m == "DELETE"]


def test_the_proof_creates_then_deletes_only_what_it_made_connector_first_then_the_project_with_its_data(rig):
    env = installed(rig)
    rig.admin_state.calls.clear()
    _proof(rig, env)
    posted = [(m, p) for m, p, _b in rig.admin_state.calls if m in ("POST", "DELETE")]
    assert posted[0] == ("POST", "/api/projects") and posted[1] == ("POST", "/api/connectors")
    deletes = _delete_calls(rig)
    assert deletes[0][1].startswith("/api/connectors/") and "expected_revision=" in deletes[0][1]
    assert deletes[1] == ("DELETE", "/api/projects/Self-Test?deleteData=true")
    project = next(b for m, p, b in rig.admin_state.calls if (m, p) == ("POST", "/api/projects"))
    assert project["name"] == "Self-Test"
    assert Path(project["documents_dir"]) == cli.selftest_root(env) / "Self-Test"   # outside the user's documents
    connector = next(b for m, p, b in rig.admin_state.calls if (m, p) == ("POST", "/api/connectors"))
    assert connector["project_mode"] == "selected" and connector["project_access"] == {"Self-Test": "write"}
    assert connector["workspace_enabled"] is True and connector["default_workspace_transfer"] == "allow"
    provision = [a for k, a, s in rig.sh.calls if "provision_selftest.py" in " ".join(a)][-1]
    assert "--defer-connector-check" in provision and "/app/config/data/Self-Test" in provision
    assert Path(provision[provision.index("--documents-dir") + 1]) == cli.selftest_root(env) / "Self-Test"
    assert provision[provision.index("--registry") + 1] == "/app/config/registry.yaml"
    assert not any(cli.selftest_root(env).iterdir())        # the Self-Test root is emptied at the end


def test_the_proof_removes_its_own_connectors_workspace_before_the_connector_and_nobody_elses(rig):
    # Deleting a connector leaves its Workspace running; on the proof VM the third proof
    # run found two leaked Self-Test Workspaces filling the running limit (capacity_busy).
    env = installed(rig)
    mine = f"c{rig.admin_state.revision + 1}"          # the id the proof's connector will get
    rig.admin_state.workspaces = [
        {"workspace_id": "w-mine", "connector_id": mine, "state": "running", "revision": 3},
        {"workspace_id": "w-user", "connector_id": "someone-else", "state": "running", "revision": 1},
    ]
    rig.admin_state.calls.clear()
    _proof(rig, env)
    deletes = [p for _m, p in _delete_calls(rig)]
    assert deletes[0] == "/api/workspaces/w-mine" and deletes[1].startswith(f"/api/connectors/{mine}")
    assert "/api/workspaces/w-user" not in deletes
    body = next(b for m, p, b in rig.admin_state.calls if (m, p) == ("DELETE", "/api/workspaces/w-mine"))
    assert body == {"expected_revision": 3, "confirm": True}


def test_a_core_proof_does_not_ask_the_workspace_service_to_clean_up(rig):
    # Core mode has no Workspace service: asking it for a list is a 503 that
    # failed the cleanup on the installer VM.
    env = installed(rig, workspace="off")
    rig.admin_state.calls.clear()
    _proof(rig, env)
    assert not [p for _m, p, _b in rig.admin_state.calls if p.startswith("/api/workspaces")]


def test_a_wrong_admin_password_stops_the_proof_before_creating_anything(rig):
    env = installed(rig)
    rig.admin_state.calls.clear()
    with pytest.raises(cli.ProofFailed) as info:
        _proof(rig, env, password="wrong")
    assert "Admin login failed" in str(info.value) and "./cognita password" in (info.value.hint or "")
    assert [m for m, _p, _b in rig.admin_state.calls] == ["LOGIN"] and _delete_calls(rig) == []


def test_leftovers_of_an_interrupted_run_are_reused_and_deleted(rig):
    env = installed(rig)
    project_dir = f"{cli.selftest_root(env).as_posix()}/Self-Test"
    rig.admin_state.projects = [{"name": "Self-Test", "documents_dir": project_dir}]
    rig.admin_state.connectors = [{"id": "c9", "slug": "install-proof", "name": "Install proof", "enabled": True,
                                   "project_mode": "selected", "project_access": {"Self-Test": "write"},
                                   "workspace_enabled": True, "default_workspace_transfer": "allow"}]
    rig.admin_state.calls.clear()
    _proof(rig, env)
    assert not any(m == "POST" and p in ("/api/projects", "/api/connectors") for m, p, _b in rig.admin_state.calls)
    assert rig.admin_state.projects == [] and rig.admin_state.connectors == []


def test_a_self_test_project_elsewhere_is_reused_and_never_deleted_and_its_files_stay(rig):
    """An adopted install such as kei main has its own Self-Test project (section 7.5 step 2)."""
    env = installed(rig)
    rig.admin_state.projects = [{"name": "Self-Test", "documents_dir": "/srv/cognita/sources/self-test"}]
    keep = cli.selftest_root(env) / "keep.txt"
    keep.write_text("x", encoding="utf-8")
    rig.admin_state.calls.clear()
    _proof(rig, env)
    assert rig.admin_state.projects == [{"name": "Self-Test", "documents_dir": "/srv/cognita/sources/self-test"}]
    assert not any(p.startswith("/api/projects") for _m, p in _delete_calls(rig))
    assert keep.exists()
    provision = [a for k, a, s in rig.sh.calls if "provision_selftest.py" in " ".join(a)][-1]     # this proof's
    assert provision[provision.index("--documents-dir") + 1] == "/srv/cognita/sources/self-test"
    assert rig.admin_state.connectors == []                # the connector it created is gone


def test_a_foreign_connector_with_the_proofs_slug_is_left_alone_and_stops_the_proof(rig):
    env = installed(rig)
    foreign = {"id": "c7", "slug": "install-proof", "name": "Install proof", "enabled": True, "project_mode": "all",
               "project_access": {}, "workspace_enabled": False}
    rig.admin_state.connectors = [dict(foreign)]
    rig.admin_state.calls.clear()
    with pytest.raises(cli.ProofFailed) as info:
        _proof(rig, env)
    assert "not the one this proof creates" in str(info.value)
    assert rig.admin_state.connectors == [foreign]          # untouched
    assert rig.admin_state.projects == []                   # the project the proof created is cleaned up
    assert not any(p.startswith("/api/connectors") for _m, p in _delete_calls(rig))


def test_an_existing_self_test_connector_is_never_matched_reused_or_deleted(rig):
    """kei test's own connector is called `self-test`.  The proof's is `Install proof` (slug install-proof), so
    on an adopted machine the two never meet (final review, finding 2)."""
    env = installed(rig)
    # Exactly the shape the OLD proof created and reused, name and all: it must still not be picked up.
    kei = {"id": "c7", "slug": "self-test", "name": "Self-Test", "enabled": True, "project_mode": "selected",
           "project_access": {"Self-Test": "write"}, "workspace_enabled": True,
           "default_workspace_transfer": "allow"}
    rig.admin_state.connectors = [dict(kei)]
    rig.admin_state.calls.clear()
    _proof(rig, env)
    made = [b for m, p, b in rig.admin_state.calls if (m, p) == ("POST", "/api/connectors")]
    assert len(made) == 1 and made[0]["name"] == "Install proof"
    assert rig.admin_state.connectors == [kei]                   # untouched: not reused, not deleted
    assert not any("c7" in p for _m, p in _delete_calls(rig))
    assert cli.PROOF_CONNECTOR_SLUG == "install-proof" and rig.tool.resolve_target("local").connector == "install-proof"


def test_a_connector_with_the_proofs_slug_but_another_name_does_not_match():
    proof = {"name": "Install proof", "enabled": True, "project_mode": "selected",
             "project_access": {"Self-Test": "write"}, "workspace_enabled": True,
             "default_workspace_transfer": "allow"}
    assert cli._connector_matches(proof, full=True)
    assert not cli._connector_matches({**proof, "name": "Self-Test"}, full=True)


def test_a_failing_self_test_still_cleans_up_and_reports_the_failure(rig):
    env = installed(rig)
    rig.tool.qa_error = release.ReleaseError("verify-failed", "live self-test failed at step 12")
    with pytest.raises(cli.ProofFailed) as info:
        _proof(rig, env)
    assert "live self-test failed at step 12" in str(info.value)
    assert rig.admin_state.projects == [] and rig.admin_state.connectors == []


def test_a_cleanup_failure_fails_the_proof_even_when_the_self_test_passed(rig):
    env = installed(rig)
    rig.admin_state.fail[("DELETE", "/api/projects/")] = cli.AdminError(500, "boom")
    with pytest.raises(cli.ProofFailed) as info:
        _proof(rig, env)
    assert "cleanup failed" in str(info.value) and "Self-Test project" in str(info.value)
    assert rig.admin_state.connectors == []                 # the connector was still deleted first


def test_a_core_install_proves_without_workspace(rig):
    env = installed(rig, workspace="off")
    rig.admin_state.calls.clear()
    _proof(rig, env)
    connector = next(b for m, p, b in rig.admin_state.calls if (m, p) == ("POST", "/api/connectors"))
    assert connector["workspace_enabled"] is False and connector["default_workspace_transfer"] == "deny"


def test_admin_over_tls_is_reached_at_loopback_with_verification_off_and_never_another_host():
    client = cli.AdminClient(8676, https=True)
    assert client.base == "https://127.0.0.1:8676"
    with pytest.raises(ValueError):
        cli.AdminClient(8676, host="example.com")


def test_the_admin_client_sends_the_csrf_cookie_back_as_a_header():
    class Opener:
        def __init__(self, jar):
            self.jar, self.seen = jar, []

        def open(self, request, timeout=None):
            self.seen.append((request.get_method(), request.full_url, dict(request.header_items())))
            if request.full_url.endswith("/api/login"):
                import http.cookiejar
                self.jar.set_cookie(http.cookiejar.Cookie(
                    0, "cognita_csrf", "tok123", None, False, "127.0.0.1", True, False, "/", True, False, None,
                    False, None, None, {}))

            class Response:
                def __enter__(self):
                    return self

                def __exit__(self, *a):
                    return False

                def read(self):
                    return b'{"ok": true}'
            return Response()

    client = cli.AdminClient(8676)
    client.opener = Opener(client.jar)
    client.login("admin", "pw")
    client.request("PATCH", "/api/settings/public-base-url", {"public_base_url": "https://x"})
    last = client.opener.seen[-1]
    assert last[2].get("X-csrf-token") == "tok123" and last[0] == "PATCH"
    assert "X-csrf-token" not in client.opener.seen[0][2]


# --------------------------------------------------------------------------
# add-folder (section 7.1)
# --------------------------------------------------------------------------


def test_add_folder_takes_the_next_slot_rewrites_the_fragment_and_re_applies(rig):
    installed(rig)
    second = f"{rig.root}/docs two"
    rig.host.add_dir(second)
    rig.tool.calls.clear()
    assert cli.cmd_add_folder(rig.ctx, rig.args("add-folder", second)) == cli.EXIT_OK
    assert rig.env()["COGNITA_PROJECTS_ROOT_2"] == second
    assert [c[0] for c in rig.tool.calls if c[0] in ("write_folders_fragment", "apply_release", "verify_release")] == [
        "write_folders_fragment", "apply_release", "verify_release"]
    third = f"{rig.root}/docs3"
    rig.host.add_dir(third)
    cli.cmd_add_folder(rig.ctx, rig.args("add-folder", third))
    assert rig.env()["COGNITA_PROJECTS_ROOT_3"] == third


def test_add_folder_of_an_existing_root_is_a_no_op_that_says_so(rig):
    installed(rig)
    rig.tool.calls.clear()
    assert cli.cmd_add_folder(rig.ctx, rig.args("add-folder", rig.docs)) == cli.EXIT_OK
    assert rig.tool.calls == [] and "already one of" in rig.screen()


def test_a_tenth_documents_folder_is_refused_with_the_reason(rig):
    env = installed(rig)
    for n in range(2, 10):
        env[f"COGNITA_PROJECTS_ROOT_{n}"] = f"{rig.root}/extra{n}"
    cli.write_env(rig.ctx, env)
    tenth = f"{rig.root}/tenth"
    rig.host.add_dir(tenth)
    with pytest.raises(cli.CliError) as info:
        cli.cmd_add_folder(rig.ctx, rig.args("add-folder", tenth))
    assert "at most 9" in str(info.value)
    assert "COGNITA_PROJECTS_ROOT_10" not in rig.env()


def test_add_folder_refuses_nesting_missing_folders_and_bad_paths(rig):
    installed(rig)
    inside = f"{rig.docs}/sub"
    rig.host.add_dir(inside)
    with pytest.raises(cli.CliError) as info:
        cli.cmd_add_folder(rig.ctx, rig.args("add-folder", inside))
    assert "overlap" in str(info.value)
    with pytest.raises(cli.CliError) as info:
        cli.cmd_add_folder(rig.ctx, rig.args("add-folder", f"{rig.root}/nope"))
    assert "not usable" in str(info.value) and "does not exist" in str(info.value)
    rig.ctx.check_paths = cli.path_problems
    with pytest.raises(cli.CliError) as info:
        cli.cmd_add_folder(rig.ctx, rig.args("add-folder", "/home/u/a$b"))
    assert "dollar sign" in str(info.value)
    assert "COGNITA_PROJECTS_ROOT_2" not in rig.env()


def test_add_folder_on_a_removable_drive_warns_about_mount_points(rig):
    installed(rig)
    rig.host.add_dir("/media/tester/usb")
    cli.cmd_add_folder(rig.ctx, rig.args("add-folder", "/media/tester/usb"))
    assert "nofail" in rig.screen()
    assert rig.env()["COGNITA_PROJECTS_ROOT_2"] == "/media/tester/usb"


# --------------------------------------------------------------------------
# remote access (section 10)
# --------------------------------------------------------------------------

ADDRESS = "https://cognita-box.tail1234.ts.net"


def _tailscale_ready(rig, states=("Running",)):
    rig.host.commands.add("tailscale")
    queue = list(states)

    def status(argv):
        state = queue.pop(0) if len(queue) > 1 else queue[0]
        return cli.Result(0, json.dumps({"BackendState": state, "Self": {"DNSName": "cognita-box.tail1234.ts.net."}}))

    rig.sh.script.insert(0, (("tailscale", "status", "--json"), status))


def test_remote_access_retries_healthz_on_an_injected_sleeper_and_saves_the_address(rig):
    env = installed(rig)
    _tailscale_ready(rig)
    rig.http_script = [(None, ""), (502, ""), (200, '{"service": "cognita"}'), (401, "")]
    rig.sleep.calls.clear()
    assert cli.cmd_remote_access(rig.ctx, unattended(rig, "remote-access")) == cli.EXIT_OK
    assert rig.sleep.calls == [10, 10]                              # two retries, ten seconds apart
    assert rig.http_calls[0] == ("GET", f"{ADDRESS}/healthz")
    assert rig.http_calls[-1] == ("POST", f"{ADDRESS}/mcp/connectors/self-test/mcp")
    assert rig.admin_state.public_url == ADDRESS
    assert ["sudo", "tailscale", "funnel", "--bg", env["COGNITA_MCP_HOST_PORT"]] in rig.sh.argvs()
    text = rig.screen()
    assert "attempt 1/18" in text and "attempt 3/18" in text and "401 without a key: that is correct" in text


def test_remote_access_gives_up_after_three_minutes_and_explains_502_and_401(rig):
    installed(rig)
    _tailscale_ready(rig)
    rig.http_script = [(502, "")] * 18
    rig.sleep.calls.clear()
    with pytest.raises(cli.CliError) as info:
        cli.cmd_remote_access(rig.ctx, unattended(rig, "remote-access"))
    assert len(rig.http_calls) == 18 and len(rig.sleep.calls) == 17
    assert "502" in str(info.value) and "3 minutes" in str(info.value)
    assert "./cognita remote-access" in (info.value.hint or "")


def test_an_mcp_route_that_does_not_answer_401_is_reported(rig):
    installed(rig)
    _tailscale_ready(rig)
    rig.http_script = [(200, '{"service": "cognita"}'), (502, "")]
    with pytest.raises(cli.CliError) as info:
        cli.cmd_remote_access(rig.ctx, unattended(rig, "remote-access"))
    assert "expected 401" in str(info.value)


def test_a_wrong_service_on_healthz_is_not_accepted(rig):
    installed(rig)
    _tailscale_ready(rig)
    rig.http_script = [(200, '{"service": "something else"}')] * 18
    with pytest.raises(cli.CliError):
        cli.cmd_remote_access(rig.ctx, unattended(rig, "remote-access"))


def test_tailscale_is_installed_only_with_consent_step_by_step_then_signed_in_then_published(rig):
    installed(rig)
    _tailscale_ready(rig, states=("NeedsLogin", "Running"))
    rig.host.commands.discard("tailscale")
    rig.http_script = [(200, '{"service": "cognita"}'), (401, "")]
    args = unattended(rig, "remote-access", "--remote-access", "yes")
    assert cli.cmd_remote_access(rig.ctx, args) == cli.EXIT_OK
    steps = [c for c in rig.sh.joined("interactive") if "tailscale" in c or "apt-get" in c]
    assert steps[:4] == [
        "sudo curl -fsSL https://pkgs.tailscale.com/stable/ubuntu/noble.noarmor.gpg -o "
        "/usr/share/keyrings/tailscale-archive-keyring.gpg",
        "sudo curl -fsSL https://pkgs.tailscale.com/stable/ubuntu/noble.tailscale-keyring.list -o "
        "/etc/apt/sources.list.d/tailscale.list",
        "sudo apt-get update", "sudo apt-get install -y tailscale"]
    assert steps[4] == "sudo tailscale up --hostname=cognita-box-1"
    text = rig.screen()
    assert text.count("why:") >= 5


def test_declining_the_tailscale_install_installs_nothing(rig):
    installed(rig)
    rig.sh.calls.clear()
    rig.host.commands.discard("tailscale")
    with pytest.raises(cli.CliError):
        cli.cmd_remote_access(rig.ctx, unattended(rig, "remote-access"))     # non-interactive, no --remote-access yes
    assert rig.sh.argvs("interactive") == []


def test_funnel_that_needs_tailnet_approval_shows_the_link_waits_and_retries_once(rig):
    installed(rig)
    _tailscale_ready(rig)
    attempts = []

    def funnel(argv):
        attempts.append(argv)
        return cli.Result(1, "", "Funnel is not enabled. Visit https://login.tailscale.com/f/funnel?node=x") \
            if len(attempts) == 1 else cli.Result(0)

    rig.sh.script.insert(0, (("sudo", "tailscale", "funnel"), funnel))
    rig.ui.non_interactive = False
    rig.input.answers = [""]
    rig.http_script = [(200, '{"service": "cognita"}'), (401, "")]
    args = rig.args("remote-access", "--admin-password-file", password_file(rig))
    assert cli.cmd_remote_access(rig.ctx, args) == cli.EXIT_OK
    assert len(attempts) == 2 and "https://login.tailscale.com/f/funnel" in rig.screen()


def test_status_shows_the_funnel_address_when_tailscale_reports_one(rig):
    installed(rig)
    rig.host.commands.add("tailscale")
    rig.sh.when("tailscale", "funnel", "status", "--json", out=json.dumps({"AllowFunnel": {"cognita-box.t.ts.net:443": True}}))
    cli.cmd_status(rig.ctx, rig.args("status"))
    text = rig.screen()
    assert "Public: https://cognita-box.t.ts.net" in text and "Admin:  http://127.0.0.1:8676" in text
    assert "status: current -> fake" in text
    rig.log._pending.clear()
    rig.host.commands.discard("tailscale")
    cli.cmd_status(rig.ctx, rig.args("status"))
    assert "Public: not set up" in rig.screen()


# --------------------------------------------------------------------------
# update and rollback (C14, section 9)
# --------------------------------------------------------------------------


def _update(rig, *extra):
    return cli.cmd_update(rig.ctx, unattended(rig, "update", *extra))


def test_update_refuses_a_dirty_checkout(rig):
    installed(rig)
    rig.sh.when("git", "-C", str(rig.repo), "status", "--porcelain", out=" M scripts/release.py\n")
    with pytest.raises(cli.CliError) as info:
        _update(rig)
    assert "uncommitted changes" in str(info.value)
    assert not any(a[:1] == ["git"] and "pull" in a for a in rig.sh.argvs())


def test_update_pulls_then_restarts_itself_when_head_moved_and_does_nothing_else_first(rig):
    installed(rig)
    heads = iter(["a" * 40, "b" * 40])
    rig.sh.script.insert(0, (("git", "-C", str(rig.repo), "rev-parse", "HEAD"), lambda argv: cli.Result(0, next(heads) + "\n")))
    staged_before = rig.tool.names().count("stage_published")
    assert _update(rig) == cli.EXIT_OK
    assert ["git", "-C", str(rig.repo), "pull", "--ff-only"] in rig.sh.argvs("stream")
    assert rig.reexec_calls == [["update", "--after-pull", "--admin-password-file", password_file(rig),
                                 "--non-interactive"]]
    assert rig.tool.names().count("stage_published") == staged_before       # the NEW code stages, not this process


def test_update_after_the_pull_says_already_up_to_date_when_the_version_matches(rig):
    installed(rig)
    assert _update(rig, "--after-pull") == cli.EXIT_OK
    assert "Already up to date (14.1.0)." in rig.screen()
    assert rig.tool.names().count("stage_published") == 1


def test_same_version_update_keeps_noop_for_unknown_proof_and_reports_repair_guidance(rig):
    installed(rig)
    env = rig.env()
    env.pop(cli.PROOF_KEY)
    cli.write_env(rig.ctx, env)
    qa_count = rig.tool.names().count("qa_release")
    rig.log.said.clear()
    assert _update(rig, "--after-pull") == cli.EXIT_OK
    assert rig.tool.names().count("qa_release") == qa_count
    assert any("Already up to date (14.1.0)." in line for line in rig.log.said)
    assert any("Self-tests are unverified; run ./cognita install to repair and verify." in line
               for line in rig.log.said)


def test_update_rerun_after_a_failed_start_does_not_claim_up_to_date(rig):
    """15.0.1: the failed update had already made the new release current, and the rerun said
    "Already up to date" over a service that was down."""
    installed(rig)
    rig.sh.when("systemctl", "--user", "is-active", rc=3, out="failed\n")
    with pytest.raises(cli.CliError) as info:
        _update(rig, "--after-pull")
    assert "failed to start" in str(info.value) and "./cognita rollback" in (info.value.hint or "")
    assert "Already up to date" not in rig.screen()


def test_update_to_a_newer_published_version_stages_applies_and_proves_it(rig):
    installed(rig)
    rig.write_published("14.2.0")
    rig.tool.calls.clear()
    assert _update(rig, "--after-pull") == cli.EXIT_OK
    assert rig.env()["COGNITA_VERSION"] == "14.2.0"
    assert [c[0] for c in rig.tool.calls if c[0] in ("stage_published", "apply_release", "verify_release", "qa_release")] \
        == ["stage_published", "apply_release", "verify_release", "qa_release"]
    assert ("qa_release", "14.2.0", "cpu") in rig.tool.calls
    assert "Updated to 14.2.0." in rig.screen()


def test_a_failed_update_says_where_it_failed_and_the_command_to_go_back(rig):
    installed(rig)
    rig.write_published("14.2.0")
    rig.tool.apply_error = release.ReleaseError("apply-failed", "systemctl start failed")
    with pytest.raises(cli.CliError) as info:
        _update(rig, "--after-pull")
    # apply_release failed before it repointed `current`: 14.1.0 is still the selected release, so
    # there is nothing to roll back and the message must not send the user to ./cognita rollback
    # (final review, finding 4); and the env file still names what runs.
    assert "./cognita rollback" not in str(info.value)
    assert str(info.value) == ("Update to 14.2.0 failed at apply before anything was switched. 14.1.0 is still "
                               "the installed version, so there is nothing to roll back. If it is not running, "
                               "start it with ./cognita start.")
    assert rig.env()["COGNITA_VERSION"] == "14.1.0"


def test_a_failed_proof_during_update_names_the_proof_step(rig):
    installed(rig)
    rig.write_published("14.2.0")
    rig.tool.qa_error = release.ReleaseError("verify-failed", "self-test failed")
    with pytest.raises(cli.CliError) as info:
        _update(rig, "--after-pull")
    assert "failed at proof" in str(info.value) and "./cognita rollback" in str(info.value)
    # `current` was repointed to 14.2.0 before the proof, so the env file keeps describing what runs.
    assert rig.env()["COGNITA_VERSION"] == "14.2.0"
    assert cli.PROOF_KEY not in rig.env()
    assert cli.status_data(rig.ctx)["proof"] is None


def test_a_post_selection_verify_failure_invalidates_the_previous_proof_before_run_proof(rig, monkeypatch):
    installed(rig)
    rig.write_published("14.2.0")

    def fail_verify(_repo, _target, _version, _log):
        raise release.ReleaseError("verify-failed", "selected release did not verify")

    monkeypatch.setattr(release, "verify_release", fail_verify)
    with pytest.raises(cli.CliError) as info:
        _update(rig, "--after-pull")
    assert "failed at apply" in str(info.value)
    assert cli.release_facts(cli.current_dir(rig.env()))["version"] == "14.2.0"
    assert rig.env()["COGNITA_VERSION"] == "14.2.0"
    assert cli.PROOF_KEY not in rig.env()
    assert not any(call[0] == "qa_release" and call[1] == "14.2.0" for call in rig.tool.calls)


def test_a_failed_download_leaves_the_old_version_running_and_recorded(rig):
    installed(rig)
    rig.write_published("14.2.0")
    rig.tool.stage_error = release.ReleaseError("build-failed", "pull failed: no route to host")
    with pytest.raises(cli.CliError) as info:
        _update(rig, "--after-pull")
    assert "14.1.0 is still running" in str(info.value) and "rollback" not in str(info.value)
    assert rig.env()["COGNITA_VERSION"] == "14.1.0"
    assert rig.tool.names().count("apply_release") == 1      # only the original install ever applied


def _two_releases(rig):
    installed(rig)
    rig.write_published("14.2.0")
    _update(rig, "--after-pull")


def test_rollback_selects_the_newest_other_release_then_proves_it_without_touching_git(rig):
    _two_releases(rig)
    rig.tool.calls.clear()
    rig.sh.calls.clear()
    assert cli.cmd_rollback(rig.ctx, unattended(rig, "rollback")) == cli.EXIT_OK
    assert ("select_release", "14.1.0") in rig.tool.calls and ("qa_release", "14.1.0", "cpu") in rig.tool.calls
    assert rig.env()["COGNITA_VERSION"] == "14.1.0"
    assert not any(a[0] == "git" for a in rig.sh.argvs())
    assert "Rolling back from 14.2.0 to 14.1.0" in rig.screen()


def test_rollback_takes_the_mode_and_profile_of_the_release_it_selects(rig):
    _two_releases(rig)
    old = Path(rig.env()["COGNITA_RELEASES_ROOT"]) / "local" / "14.1.0" / "release.txt"
    old.write_text(old.read_text(encoding="utf-8").replace("mode: full", "mode: core"), encoding="utf-8")
    cli.cmd_rollback(rig.ctx, unattended(rig, "rollback"))
    assert rig.env()["COGNITA_WORKSPACE"] == "off"


def test_a_failed_rollback_restores_the_env_file(rig):
    _two_releases(rig)
    rig.tool.select_error = release.ReleaseError("apply-failed", "start failed")
    with pytest.raises(release.ReleaseError):
        cli.cmd_rollback(rig.ctx, unattended(rig, "rollback"))
    assert rig.env()["COGNITA_VERSION"] == "14.2.0"


def test_rollback_with_one_release_says_there_is_nothing_to_go_back_to(rig):
    installed(rig)
    with pytest.raises(cli.CliError) as info:
        cli.cmd_rollback(rig.ctx, unattended(rig, "rollback"))
    assert "nothing to roll back to" in str(info.value)


# --------------------------------------------------------------------------
# uninstall (C16, section 9)
# --------------------------------------------------------------------------


def _unit_file(rig) -> Path:
    path = Path(rig.host.home()) / ".config" / "systemd" / "user" / "cognita.service"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("[Unit]\n", encoding="utf-8")
    return path


def test_uninstall_keeps_every_piece_of_data_and_the_documents_and_lists_them(rig):
    env = installed(rig)
    unit = _unit_file(rig)
    other_target = Path(env["COGNITA_RELEASES_ROOT"]) / "main"
    other_target.mkdir()
    (Path(rig.docs) / "note.md").write_text("mine", encoding="utf-8")
    rig.ui.non_interactive = False
    rig.input.answers = ["y"]
    assert cli.cmd_uninstall(rig.ctx, rig.args("uninstall")) == cli.EXIT_OK
    assert not unit.exists()
    assert not (Path(env["COGNITA_RELEASES_ROOT"]) / "local").exists()
    assert other_target.exists()                    # another release.py target sharing <releases> is not touched
    for key in ("COGNITA_CONFIG_ROOT", "COGNITA_SECRETS_ROOT", "COGNITA_POSTGRES_DATA_ROOT",
                "COGNITA_MODEL_CACHE_ROOT", "COGNITA_WORKSPACE_DATA_ROOT"):
        assert Path(env[key]).is_dir(), key
    assert rig.env_path.exists() and (Path(rig.docs) / "note.md").read_text() == "mine"
    joined = rig.sh.joined()
    assert any(c.endswith(" down") and "-p cognita" in c for c in joined)
    assert not any(" -v" in c and c.endswith("down") for c in joined)         # containers and networks only
    assert not any("prune" in c for c in joined)
    text = rig.screen()
    assert "Kept:" in text and rig.docs in text and "Reinstalling with ./cognita install" in text
    assert "systemctl --user stop cognita.service" in " ".join(" ".join(a) for a in rig.sh.argvs())


def test_uninstall_removes_only_the_images_this_install_recorded(rig):
    installed(rig)
    rig.ui.non_interactive = False
    rig.input.answers = ["y"]
    cli.cmd_uninstall(rig.ctx, rig.args("uninstall"))
    removed = [a[3] for a in rig.sh.argvs("capture") if a[:3] == ["docker", "image", "rm"]]
    assert "cognita/app:14.1.0-local-cpu-abc123" in removed
    assert "cognita/workspace-runtime:14.1.0-local-abc123" in removed
    assert "ghcr.io/x/cognita-app@sha256:aaa" in removed and "ghcr.io/x/toolbox@sha256:bbb" in removed
    assert "cognita-workspace-toolbox:12.6.0" in removed
    assert len(removed) == len(set(removed))


def test_uninstall_turns_linger_off_only_when_this_installer_turned_it_on(rig):
    env = installed(rig)
    assert env["COGNITA_LINGER_SET_BY_INSTALLER"] == "1"
    rig.ui.non_interactive = False
    rig.input.answers = ["y"]
    cli.cmd_uninstall(rig.ctx, rig.args("uninstall"))
    assert ["sudo", "loginctl", "disable-linger", "tester"] in rig.sh.argvs("interactive")
    assert "COGNITA_LINGER_SET_BY_INSTALLER" not in rig.env()


def test_uninstall_leaves_linger_alone_when_it_was_already_on(rig):
    rig.sh.script.insert(0, (("loginctl", "show-user"), cli.Result(0, "Linger=yes\n")))
    installed(rig)
    assert "COGNITA_LINGER_SET_BY_INSTALLER" not in rig.env()
    rig.ui.non_interactive = False
    rig.input.answers = ["y"]
    cli.cmd_uninstall(rig.ctx, rig.args("uninstall"))
    assert not any(a[:3] == ["sudo", "loginctl", "disable-linger"] for a in rig.sh.argvs("interactive"))


def test_declining_the_uninstall_confirmation_changes_nothing(rig):
    installed(rig)
    unit = _unit_file(rig)
    rig.ui.non_interactive = False
    rig.input.answers = ["n"]
    with pytest.raises(cli.CliError):
        cli.cmd_uninstall(rig.ctx, rig.args("uninstall"))
    assert unit.exists() and not any("stop" in a for a in rig.sh.argvs("capture"))


def test_delete_data_needs_the_typed_phrase_and_lists_the_exact_directories(rig):
    env = installed(rig)
    unit = _unit_file(rig)
    rig.ui.non_interactive = False
    rig.input.answers = ["yes please"]
    with pytest.raises(cli.CliError) as info:
        cli.cmd_uninstall(rig.ctx, rig.args("uninstall", "--delete-data"))
    assert "phrase was not typed" in str(info.value)
    assert unit.exists() and Path(env["COGNITA_CONFIG_ROOT"]).is_dir()
    listed = rig.screen()
    lines = listed.splitlines()
    for key in ("COGNITA_CONFIG_ROOT", "COGNITA_SECRETS_ROOT", "COGNITA_POSTGRES_DATA_ROOT",
                "COGNITA_MODEL_CACHE_ROOT", "COGNITA_WORKSPACE_DATA_ROOT", "COGNITA_TRANSFER_STAGING_ROOT",
                "COGNITA_TOOLBOX_IMAGE_CACHE_ROOT"):
        assert f"  {env[key]}" in lines, key                          # each is on its own line, exactly
    assert f"  {rig.docs}" not in lines                               # a documents root is never in the list
    assert "Your documents folders are never deleted." in listed
    rig.input.answers = [cli.CONFIRM_DELETE_PHRASE]
    assert cli.cmd_uninstall(rig.ctx, rig.args("uninstall", "--delete-data", "--yes")) == cli.EXIT_OK
    for key in ("COGNITA_CONFIG_ROOT", "COGNITA_SECRETS_ROOT", "COGNITA_POSTGRES_DATA_ROOT",
                "COGNITA_MODEL_CACHE_ROOT", "COGNITA_WORKSPACE_DATA_ROOT"):
        assert not Path(env[key]).exists(), key
    assert not rig.env_path.exists() and Path(rig.docs).is_dir()


def test_delete_data_cannot_be_confirmed_non_interactively(rig):
    installed(rig)
    rig.ui.non_interactive = True
    with pytest.raises(cli.CliError) as info:
        cli.cmd_uninstall(rig.ctx, rig.args("uninstall", "--delete-data", "--non-interactive", "--yes"))
    assert "cannot" in str(info.value)


def test_a_data_root_that_overlaps_a_documents_root_is_never_deleted_whatever_the_env_says(rig):
    """C16: uninstall NEVER deletes a documents root.  Even an edited env file that points a data root at,
    above or inside one is refused per path."""
    env = installed(rig)
    inside = f"{rig.docs}/models"
    above = rig.root
    Path(inside).mkdir()
    (Path(inside) / "keep.txt").write_text("k", encoding="utf-8")
    env["COGNITA_MODEL_CACHE_ROOT"] = inside
    env["COGNITA_TRANSFER_STAGING_ROOT"] = above
    cli.write_env(rig.ctx, env)
    deletable, refused = cli.safe_delete_targets(rig.ctx, env)
    assert inside in refused and above in refused
    assert inside not in deletable and above not in deletable and rig.docs not in deletable
    rig.ui.non_interactive = False
    rig.input.answers = [cli.CONFIRM_DELETE_PHRASE]
    cli.cmd_uninstall(rig.ctx, rig.args("uninstall", "--delete-data"))
    assert (Path(inside) / "keep.txt").exists() and Path(rig.docs).is_dir()
    assert "will NOT delete" in rig.screen()


def test_uninstall_tells_a_tailscale_user_how_to_turn_the_funnel_off(rig):
    installed(rig)
    rig.host.commands.add("tailscale")
    rig.ui.non_interactive = False
    rig.input.answers = ["y"]
    cli.cmd_uninstall(rig.ctx, rig.args("uninstall"))
    assert "sudo tailscale funnel --https=443 off" in rig.screen()


# --------------------------------------------------------------------------
# adopt (section 13, D4, C23)
# --------------------------------------------------------------------------


def _old_install(rig, monkeypatch):
    base = Path(rig.root) / "old"
    for name in ("config", "secrets", "postgres", "models", "workspaces", "transfers", "toolbox-cache"):
        (base / name).mkdir(parents=True)
    (base / "config" / "cognita.yaml").write_text(
        'admin_username: doug\nadmin_password_hash: "$argon2id$v=19$abc"\npublic_base_url: https://x\n', encoding="utf-8")
    old_docs = f"{rig.root}/onedrive"
    rig.host.add_dir(old_docs)
    Path(old_docs).mkdir()
    old_env = Path(rig.root) / "cognita-main-amd.env"
    old_env.write_text("\n".join([
        f"COGNITA_CONFIG_ROOT={base.as_posix()}/config", f"COGNITA_SECRETS_ROOT={base.as_posix()}/secrets",
        f"COGNITA_POSTGRES_DATA_ROOT={base.as_posix()}/postgres", f"COGNITA_MODEL_CACHE_ROOT={base.as_posix()}/models",
        f"COGNITA_WORKSPACE_DATA_ROOT={base.as_posix()}/workspaces",
        f"COGNITA_TRANSFER_STAGING_ROOT={base.as_posix()}/transfers",
        f"COGNITA_TOOLBOX_IMAGE_CACHE_ROOT={base.as_posix()}/toolbox-cache",
        f"COGNITA_PROJECTS_ROOT={old_docs}", "COGNITA_SERVICE_UID=1000", "COGNITA_SERVICE_GID=1000",
        "COGNITA_KVM_GID=108", "COGNITA_VIDEO_GID=44", "COGNITA_RENDER_GID=992", "COGNITA_MCP_HOST_PORT=8675",
        "COGNITA_MCP_BIND_ADDRESS=127.0.0.1", "COGNITA_ADMIN_HOST_PORT=8676",
        "COGNITA_ADMIN_BIND_ADDRESS=0.0.0.0", "COGNITA_VERSION=13.7.0", "COGNITA_RELEASE_TARGET=main", ""]),
        encoding="utf-8")
    releases = Path(rig.root) / "oldreleases"
    (releases / "main" / "current").mkdir(parents=True)
    monkeypatch.setattr(release, "RELEASES_ROOT", releases)
    rig.tool.foreign_targets["main"] = SimpleNamespace(
        name="main", profile="amd", env_file=old_env, unit="cognita-compose-main.service", project="cognita-main")
    rig.admin_state.projects = [{"name": "Self-Test", "documents_dir": "/srv/sources/self-test"}]
    return old_env, base, old_docs, releases


def test_adopt_accepts_a_legacy_sha256_admin_password(rig, monkeypatch):
    # kei's test target predates Argon2; the app still logs in with the sha256 hash (P10a, 2026-09-29).
    old_env, base, _docs, _releases = _old_install(rig, monkeypatch)
    (base / "config" / "cognita.yaml").write_text(
        "admin_username: selftest-admin\nadmin_password_hash: ''\nadmin_password_sha256: " + "ab" * 32 + "\n",
        encoding="utf-8")
    args = rig.args("install", "--adopt", str(old_env), "--force", "--yes", "--non-interactive",
                    "--admin-password-file", password_file(rig))
    assert cli.cmd_install(rig.ctx, args) == cli.EXIT_OK
    assert not any("sac.py" in " ".join(a) for a in rig.sh.argvs())         # the old password is kept


def test_config_admin_counts_only_a_real_hash(tmp_path):
    (tmp_path / "cognita.yaml").write_text("admin_username: a\nadmin_password_hash: ''\nadmin_password_sha256: ''\n",
                                           encoding="utf-8")
    assert cli.config_admin({"COGNITA_CONFIG_ROOT": str(tmp_path)}) == (False, "a")


def test_adopt_points_the_new_env_at_the_old_directories_and_stops_the_old_unit(rig, monkeypatch):
    old_env, base, old_docs, releases = _old_install(rig, monkeypatch)
    args = rig.args("install", "--adopt", str(old_env), "--force", "--yes", "--non-interactive",
                    "--admin-password-file", password_file(rig))
    assert cli.cmd_install(rig.ctx, args) == cli.EXIT_OK
    env = rig.env()
    assert env["COGNITA_CONFIG_ROOT"] == f"{base.as_posix()}/config" and env["COGNITA_PROJECTS_ROOT"] == old_docs
    assert env["COGNITA_POSTGRES_DATA_ROOT"] == f"{base.as_posix()}/postgres"
    assert env["COGNITA_RELEASES_ROOT"] == str(releases) and env["COGNITA_RELEASE_TARGET"] == "local"
    assert env["COGNITA_ACCELERATION"] == "amd" and env["COGNITA_WORKSPACE"] == "on"
    assert env["COGNITA_ADMIN_BIND_ADDRESS"] == "0.0.0.0" and env["COGNITA_VERSION"] == "14.1.0"
    assert ["systemctl", "--user", "disable", "--now", "cognita-compose-main.service"] in rig.sh.argvs("interactive")
    down = next(a for a in rig.sh.argvs("stream") if a[-1] == "down")
    assert "cognita-main" in down and str(old_env) in down                  # the OLD project, with its own env file
    assert (base / "config" / "cognita.yaml").read_text(encoding="utf-8").count("doug") == 1     # nothing re-seeded
    assert not (base / "secrets" / "postgres.dsn").exists() and not (base / "secrets" / "broker.secret").exists()
    assert not any("sac.py" in " ".join(a) for a in rig.sh.argvs())         # the hash exists: no password is set
    # The proof reused the existing Self-Test project and did not delete it.
    assert rig.admin_state.projects == [{"name": "Self-Test", "documents_dir": "/srv/sources/self-test"}]
    assert rig.admin_state.connectors == []
    order = positions(rig, "release stage_published", "cognita.prefetch_models", "enable-linger",
                      "systemctl --user disable", "release apply_release", "release qa_release")
    # Everything that can fail (the long pull, the models, linger) runs BEFORE the old service is stopped
    # (design 13 step 2, final review finding 1); the stop comes right before the new one is applied.
    assert order == sorted(order)


def _adopt_args(rig, old_env, *extra):
    return rig.args("install", "--adopt", str(old_env), "--force", "--non-interactive",
                    "--admin-password-file", password_file(rig), *extra)


def _stopped_the_old_unit(rig) -> bool:
    return any(a[:4] == ["systemctl", "--user", "disable", "--now"] and a[4] == "cognita-compose-main.service"
               for a in rig.sh.argvs()) or any(a[-1] == "down" for a in rig.sh.argvs("stream"))


def test_a_wrong_password_stops_an_adoption_before_anything_changes(rig, monkeypatch):
    # The old service is still running with the same config, so its Admin checks the password first;
    # before this, a typo only showed at the proof, after production had been stopped.
    old_env, *_ = _old_install(rig, monkeypatch)
    rig.admin_state.password = "not what the file says"
    with pytest.raises(cli.CliError, match="password is wrong"):
        cli.cmd_install(rig.ctx, _adopt_args(rig, old_env))
    assert not _stopped_the_old_unit(rig) and "stage_published" not in rig.tool.names()
    assert not rig.env_path.exists()


def test_an_old_admin_that_does_not_answer_leaves_the_password_to_the_proof(rig, monkeypatch):
    old_env, *_ = _old_install(rig, monkeypatch)
    real_login = FakeAdmin.login
    calls = {"n": 0}

    def first_one_unreachable(self, username, password):
        calls["n"] += 1
        if calls["n"] == 1:                                  # the pre-check: nothing on the old port
            raise cli.AdminError(0, "could not reach Admin")
        return real_login(self, username, password)

    monkeypatch.setattr(FakeAdmin, "login", first_one_unreachable)
    assert cli.cmd_install(rig.ctx, _adopt_args(rig, old_env)) == cli.EXIT_OK
    assert calls["n"] >= 2                                   # skipped, not refused; the proof logged in


def test_a_failed_download_leaves_the_old_service_untouched_and_the_adoption_retryable(rig, monkeypatch):
    old_env, *_ = _old_install(rig, monkeypatch)
    rig.tool.stage_error = release.ReleaseError("build-failed", "pull failed: no route to host")
    with pytest.raises(release.ReleaseError):
        cli.cmd_install(rig.ctx, _adopt_args(rig, old_env))
    assert not _stopped_the_old_unit(rig)                     # no stop, no compose down
    assert not [a for a in rig.sh.argvs() if a[:3] == ["systemctl", "--user", "disable"]]
    assert "apply_release" not in rig.tool.names()
    assert not rig.env_path.exists()                          # nothing changed, so the same command can be rerun
    log = next((Path(rig.root) / "oldreleases" / "local" / "logs").glob("install-*.log")).read_text(encoding="utf-8")
    assert "failed BEFORE the old service was touched" in log
    rig.tool.stage_error = None
    assert cli.cmd_install(rig.ctx, _adopt_args(rig, old_env)) == cli.EXIT_OK      # and it is retryable


def test_a_failed_model_step_before_the_stop_also_leaves_the_old_service_running(rig, monkeypatch):
    old_env, *_ = _old_install(rig, monkeypatch)

    # linger is the last step before the stop; its sudo command failing must not cost the user the old service
    rig.sh.script.insert(0, (("sudo", "loginctl", "enable-linger"), cli.Result(1)))
    with pytest.raises(cli.CliError):
        cli.cmd_install(rig.ctx, _adopt_args(rig, old_env))
    assert not _stopped_the_old_unit(rig) and not rig.env_path.exists()


# `;`, not `&&`: when the failure came before cognita.service existed, its disable fails, and `&&`
# would then skip re-enabling the old service (final review 2, finding 1).
ROLLBACK = "systemctl --user disable --now cognita.service; systemctl --user enable --now cognita-compose-main.service"


def test_any_failure_after_the_old_service_stopped_prints_the_exact_rollback_command(rig, monkeypatch):
    old_env, *_ = _old_install(rig, monkeypatch)
    rig.tool.apply_error = release.ReleaseError("apply-failed", "systemctl start failed")   # not a ProofFailed
    with pytest.raises(release.ReleaseError):
        cli.cmd_install(rig.ctx, _adopt_args(rig, old_env))
    assert _stopped_the_old_unit(rig)
    assert ROLLBACK in rig.screen()


def test_a_proof_failure_after_the_stop_prints_the_same_rollback_command(rig, monkeypatch):
    old_env, *_ = _old_install(rig, monkeypatch)
    rig.tool.qa_error = release.ReleaseError("verify-failed", "live self-test failed")
    assert cli.cmd_install(rig.ctx, _adopt_args(rig, old_env)) == cli.EXIT_FAILED
    assert f"Rollback: {ROLLBACK}" in rig.screen()


def test_a_disable_that_fails_stops_the_adoption_and_prints_the_rollback_command(rig, monkeypatch):
    old_env, *_ = _old_install(rig, monkeypatch)
    rig.sh.script.insert(0, (("systemctl", "--user", "disable"), cli.Result(1, "", "Failed to disable unit")))
    with pytest.raises(cli.CliError) as info:
        cli.cmd_install(rig.ctx, _adopt_args(rig, old_env))
    assert "Could not stop and disable cognita-compose-main.service (exit 1)" in str(info.value)
    assert "apply_release" not in rig.tool.names()             # the new stack was never started on the old one's ports
    assert ROLLBACK in rig.screen()
    assert not rig.env_path.exists()                           # nothing new started: the adoption can be rerun


def test_a_same_version_rerun_that_fails_after_stopping_says_cognita_is_stopped(rig, capsys):
    # A repair rerun stops the unit to re-stage the running version; a failure before the apply used
    # to leave Cognita down with only "run the same command again" (final review 2, finding 3).
    installed(rig)
    capsys.readouterr()
    rig.ui.non_interactive = True
    rig.tool.apply_error = release.ReleaseError("apply-failed", "systemctl start failed")
    code = cli.main(["install", "--non-interactive", "--yes", "--remote-access", "no",
                     "--admin-password-file", password_file(rig)], ctx=rig.ctx)
    err = capsys.readouterr().err
    assert code != 0
    assert "Cognita is stopped" in err and "cognita start" in err


def test_a_failure_with_no_restage_stop_says_nothing_about_being_stopped(rig, capsys):
    rig.tool.stage_error = release.ReleaseError("build-failed", "pull failed")
    rig.ui.non_interactive = True
    argv = ["install", "--documents", rig.docs, "--data-dir", rig.data, "--admin-user", "admin",
            "--non-interactive", "--yes", "--workspace", "on", "--remote-access", "no",
            "--admin-password-file", password_file(rig)]
    assert cli.main(argv, ctx=rig.ctx) != 0
    assert "Cognita is stopped" not in capsys.readouterr().err


def test_the_real_runner_treats_a_child_stopped_by_ctrl_c_as_an_interruption(tmp_path):
    # The runner's own probes and interactive commands, not only release.run (see its test).
    runner = cli.Runner(cli.InstallLog(None))
    with pytest.raises(KeyboardInterrupt):
        runner.capture([sys.executable, "-c", "import sys; sys.exit(130)"])
    with pytest.raises(KeyboardInterrupt):
        runner.interactive([sys.executable, "-c", "import sys; sys.exit(130)"])
    assert runner.capture([sys.executable, "-c", "import sys; sys.exit(3)"]).rc == 3


def test_an_interruption_during_a_step_says_interrupted_and_how_to_finish(rig, capsys):
    rig.tool.stage_error = KeyboardInterrupt()
    rig.ui.non_interactive = True
    argv = ["install", "--documents", rig.docs, "--data-dir", rig.data, "--admin-user", "admin",
            "--non-interactive", "--yes", "--workspace", "on", "--remote-access", "no",
            "--admin-password-file", password_file(rig)]
    assert cli.main(argv, ctx=rig.ctx) == 130
    err = capsys.readouterr().err
    assert "Interrupted. Run the same command again" in err and "build-failed" not in err


def test_a_terminal_on_stdin_is_refused_for_the_stdin_password(monkeypatch):
    class Tty:
        def isatty(self):
            return True
    monkeypatch.setattr(cli.sys, "stdin", Tty())
    with pytest.raises(cli.CliError, match="piped in"):
        cli.real_stdin_line()


def test_a_failed_weight_download_in_an_adoption_warns_and_continues(rig, monkeypatch):
    old_env, *_ = _old_install(rig, monkeypatch)
    rig.ocr_error = OSError("no network")
    assert cli.cmd_install(rig.ctx, _adopt_args(rig, old_env)) == cli.EXIT_OK
    assert "OCR model files could not be downloaded" in rig.screen() and "apply_release" in rig.tool.names()


def test_adopt_needs_no_yes_and_yes_is_not_what_stops_the_old_service(rig, monkeypatch):
    """--adopt is the explicit request (final review, finding 9): no confirmation is asked, so a run without
    --yes goes through and a run with it behaves the same."""
    old_env, *_ = _old_install(rig, monkeypatch)
    assert _adopt_args(rig, old_env).yes is False
    assert cli.cmd_install(rig.ctx, _adopt_args(rig, old_env)) == cli.EXIT_OK
    assert _stopped_the_old_unit(rig) and "Stop the old service" not in " ".join(rig.input.prompts)
    assert "the old service is stopped only after that succeeds" in rig.screen()


def test_adopt_refuses_when_a_cognita_install_already_exists(rig, monkeypatch):
    old_env, *_ = _old_install(rig, monkeypatch)
    installed(rig)
    args = rig.args("install", "--adopt", str(old_env), "--yes", "--non-interactive")
    with pytest.raises(cli.CliError) as info:
        cli.cmd_install(rig.ctx, args)
    assert "already has a ./cognita install" in str(info.value)


def test_adopt_of_an_install_with_no_admin_password_is_refused(rig, monkeypatch):
    old_env, base, *_ = _old_install(rig, monkeypatch)
    (base / "config" / "cognita.yaml").write_text('admin_password_hash: ""\n', encoding="utf-8")
    args = rig.args("install", "--adopt", str(old_env), "--force", "--yes", "--non-interactive",
                    "--admin-password-file", password_file(rig))
    with pytest.raises(cli.CliError) as info:
        cli.cmd_install(rig.ctx, args)
    assert "no Admin password" in str(info.value)
    assert not rig.env_path.exists()


# --------------------------------------------------------------------------
# password, reset, logs, start/stop, main()
# --------------------------------------------------------------------------


def test_password_sets_the_hash_through_the_image_with_stdin_json_then_recreates_the_app(rig):
    installed(rig)
    rig.sh.calls.clear()
    assert cli.cmd_password(rig.ctx, unattended(rig, "password")) == cli.EXIT_OK
    stream = rig.sh.argvs("stream")
    assert "sac.py" in " ".join(stream[0]) and stream[0][-1] == "--stdin-json"
    assert json.loads(next(s for k, a, s in rig.sh.calls if s)) == {"username": "admin", "password": PASSWORD}
    assert stream[1][-5:] == ["up", "-d", "--no-deps", "--force-recreate", "cognita"]
    assert PASSWORD not in " ".join(" ".join(a) for a in rig.sh.argvs())


def test_reset_runs_the_reset_script_for_the_local_target_and_takes_no_lock(rig):
    installed(rig)
    rig.sh.calls.clear()
    assert cli.cmd_reset(rig.ctx, rig.args("reset", "index")) == cli.EXIT_OK
    argv = rig.sh.argvs("interactive")[0]
    assert argv[1].endswith("reset_disposable_state.py")
    assert argv[2:] == ["--target", "local", "--scope", "index", "--apply"]


@pytest.mark.parametrize("which, needle", [("app", "cognita.log"), ("workspace", "workspace-runtime"),
                                           ("install", "install-")])
def test_logs_show_the_right_file_and_follow_only_when_asked(rig, which, needle):
    installed(rig)
    rig.sh.calls.clear()
    cli.cmd_logs(rig.ctx, rig.args("logs", which, "-f"))
    argv = rig.sh.argvs("interactive")[0]
    assert needle in " ".join(argv) and argv[-2] == "-f"        # (workspace's Compose files also use -f)
    rig.sh.calls.clear()
    cli.cmd_logs(rig.ctx, rig.args("logs", which))
    assert rig.sh.argvs("interactive")[0][-2] != "-f"


def test_following_a_log_ends_quietly_on_ctrl_c_but_a_plain_read_does_not_swallow_it(rig):
    installed(rig)

    def interrupt(kind, argv, stdin):
        raise KeyboardInterrupt

    rig.sh.on_call = interrupt
    assert cli.cmd_logs(rig.ctx, rig.args("logs", "app", "-f")) == cli.EXIT_OK
    with pytest.raises(KeyboardInterrupt):
        cli.cmd_logs(rig.ctx, rig.args("logs", "app"))


@pytest.mark.parametrize("verb", ["start", "stop", "restart"])
def test_start_stop_restart_go_through_the_user_unit(rig, verb):
    installed(rig)
    rig.sh.calls.clear()
    assert cli.main([verb], ctx=rig.ctx) == cli.EXIT_OK
    assert rig.sh.argvs("interactive") == [["systemctl", "--user", verb, "cognita.service"]]


def test_commands_before_an_install_say_to_run_install(rig, capsys):
    assert cli.main(["status"], ctx=rig.ctx) == cli.EXIT_FAILED
    err = capsys.readouterr().err
    assert "not installed" in err and "Next: Run: ./cognita install" in err


def test_a_release_tool_failure_prints_its_state_and_uses_its_exit_code(rig, capsys, monkeypatch):
    installed(rig)

    def fail(target):
        raise release.ReleaseError("verify-failed", "boom")

    monkeypatch.setattr(release, "status_lines", fail, raising=False)
    assert cli.main(["status"], ctx=rig.ctx) == release.EXIT_CODES["verify-failed"]
    err = capsys.readouterr().err
    assert "Error (verify-failed): boom" in err and "run the same command again" in err


def test_an_unexpected_exception_is_logged_with_its_traceback_and_named_to_the_user(rig, capsys, monkeypatch):
    installed(rig)

    def crash(target):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(release, "status_lines", crash, raising=False)
    log_file = rig.log.path
    assert cli.main(["status"], ctx=rig.ctx) == cli.EXIT_FAILED
    assert "Unexpected error: RuntimeError: kaboom" in capsys.readouterr().err
    assert "Traceback" in Path(log_file).read_text(encoding="utf-8")


def test_ctrl_c_says_to_rerun_and_exits_130(rig, capsys, monkeypatch):
    installed(rig)

    def interrupt(target):
        raise KeyboardInterrupt

    monkeypatch.setattr(release, "status_lines", interrupt, raising=False)
    assert cli.main(["status"], ctx=rig.ctx) == 130
    assert "Run the same command again" in capsys.readouterr().err


# --------------------------------------------------------------------------
# Final review fixes (13.7.1): each test names the finding it pins
# --------------------------------------------------------------------------


def _wrong_password_file(rig) -> str:
    path = rig.tmp / "wrong.txt"
    path.write_text("not the password\n", encoding="utf-8")
    return str(path)


def _flat(rig) -> list[str]:
    return [" ".join(str(x) for x in item) for item in rig.order]


def _install_log(rig, prefix: str = "install") -> str:
    logs = Path(rig.env()["COGNITA_RELEASES_ROOT"], "local", "logs")
    return "\n".join(p.read_text(encoding="utf-8") for p in sorted(logs.glob(f"{prefix}-*.log")))


def test_finding3_a_wrong_password_on_an_amd_rerun_is_the_proofs_password_failure_not_a_gpu_fallback(rig):
    _amd_machine(rig)
    assert run_install(rig, "--acceleration", "amd") == cli.EXIT_OK
    assert rig.env()["COGNITA_ACCELERATION"] == "amd"
    rig.admin_state.calls.clear()
    rig.log.said.clear()
    args = rig.args("install", "--non-interactive", "--yes", "--remote-access", "no",
                    "--admin-password-file", _wrong_password_file(rig))
    assert cli.cmd_install(rig.ctx, args) == cli.EXIT_FAILED
    screen = rig.screen()
    assert "Admin login failed" in screen and "The Admin password is wrong." in screen
    assert "Switching to CPU" not in screen and "did not verify" not in screen
    assert rig.env()["COGNITA_ACCELERATION"] == "amd"                                  # still amd
    assert [c[1] for c in rig.tool.calls if c[0] == "stage_published"] == ["amd", "amd"]   # no CPU restage
    assert [m for m, _p, _b in rig.admin_state.calls] == ["LOGIN"]                     # the GPU was never touched
    assert len([c for c in rig.tool.calls if c[0] == "qa_release"]) == 1               # only the first install's proof


def test_finding3_a_gpu_that_does_not_verify_still_switches_to_the_cpu(rig):
    _amd_machine(rig)
    rig.admin_state.verify_result = {"state": "failed", "cards": [], "cleanup": "passed", "runtimes": {}}
    assert run_install(rig, "--acceleration", "amd") == cli.EXIT_OK
    assert rig.env()["COGNITA_ACCELERATION"] == "cpu"


def test_finding5_restaging_the_running_release_stops_the_unit_before_staging(rig):
    env = installed(rig, workspace="on")
    _give_admin_a_hash(env)
    before = len(rig.order)
    args = rig.args("install", "--non-interactive", "--yes", "--workspace", "off", "--remote-access", "no",
                    "--admin-password-file", password_file(rig))
    assert cli.cmd_install(rig.ctx, args) == cli.EXIT_OK
    rerun = _flat(rig)[before:]
    stop = next(i for i, t in enumerate(rerun) if "systemctl --user stop cognita.service" in t)
    stage = next(i for i, t in enumerate(rerun) if t == "release stage_published")
    apply_ = next(i for i, t in enumerate(rerun) if t == "release apply_release")
    assert stop < stage < apply_
    assert "restage: 14.1.0 is the running release" in _install_log(rig)


def test_finding5_a_first_install_and_a_new_version_stop_nothing_before_staging(rig):
    assert run_install(rig) == cli.EXIT_OK
    flat = _flat(rig)
    assert not any("systemctl --user stop" in t for t in flat[:flat.index("release stage_published")])   # nothing runs yet
    _give_admin_a_hash(rig.env())
    rig.write_published("14.2.0")
    rig.order.clear()
    assert cli.cmd_update(rig.ctx, unattended(rig, "update", "--after-pull")) == cli.EXIT_OK
    flat = _flat(rig)
    assert not any("systemctl --user stop" in t for t in flat[:flat.index("release stage_published")])   # a separate directory


def test_finding5_the_cpu_fallback_stops_the_amd_unit_before_restaging_the_same_version(rig):
    _amd_machine(rig)
    rig.admin_state.verify_result = {"state": "failed", "cards": [], "cleanup": "passed", "runtimes": {}}
    assert run_install(rig, "--acceleration", "amd") == cli.EXIT_OK
    flat = _flat(rig)
    stages = [i for i, t in enumerate(flat) if t == "release stage_published"]
    assert len(stages) == 2
    assert any("systemctl --user stop cognita.service" in t for t in flat[stages[0]:stages[1]])


def test_finding4_a_rollback_whose_proof_fails_keeps_the_env_describing_the_release_that_now_runs(rig):
    _two_releases(rig)
    rig.tool.qa_error = release.ReleaseError("verify-failed", "self-test failed")
    with pytest.raises(cli.CliError):
        cli.cmd_rollback(rig.ctx, unattended(rig, "rollback"))
    assert ("select_release", "14.1.0") in rig.tool.calls            # `current` moved to 14.1.0 ...
    assert rig.env()["COGNITA_VERSION"] == "14.1.0"                  # ... so the env file says so
    assert "14.1.0 is now the selected release, but the proof did not pass" in rig.screen()
    assert cli.PROOF_KEY not in rig.env()
    assert cli.status_data(rig.ctx)["proof"] is None


def test_finding4_a_failure_before_current_moves_puts_the_env_file_back(rig):
    installed(rig)
    rig.write_published("14.2.0")
    rig.tool.apply_error = release.ReleaseError("apply-failed", "refused drop-in")
    with pytest.raises(cli.CliError):
        _update(rig, "--after-pull")
    assert rig.env()["COGNITA_VERSION"] == "14.1.0"
    assert "current still runs 14.1.0; the env file is back as it was" in _install_log(rig, "update")


def _kei_toolbox(rig, toolbox_version: str) -> Path:
    kei = Path(rig.root) / "kei-releases"
    (kei / "main" / "13.7.0").mkdir(parents=True)
    (kei / "main" / "13.7.0" / "release.txt").write_text(
        f"version: 13.7.0\ntoolbox_version: {toolbox_version}\n", encoding="utf-8")
    rig.tool.foreign_targets["main"] = SimpleNamespace(name="main", releases_root=kei)
    return kei


def test_finding6_uninstall_keeps_a_toolbox_tag_a_table_target_still_records(rig):
    installed(rig)
    _kei_toolbox(rig, "12.6.0")
    rig.ui.non_interactive = False
    rig.input.answers = ["y"]
    cli.cmd_uninstall(rig.ctx, rig.args("uninstall"))
    removed = [a[3] for a in rig.sh.argvs("capture") if a[:3] == ["docker", "image", "rm"]]
    assert "cognita-workspace-toolbox:12.6.0" not in removed                     # shared with kei main
    assert "cognita/app:14.1.0-local-cpu-abc123" in removed                      # this install's own tags still go
    assert "ghcr.io/x/cognita-app@sha256:aaa" in removed


def test_finding6_a_table_target_on_another_toolbox_version_does_not_keep_the_tag(rig):
    installed(rig)
    _kei_toolbox(rig, "12.5.0")
    assert cli.toolbox_tag_used_by_table_target("12.6.0", rig.log) == ""
    assert cli.toolbox_tag_used_by_table_target("12.5.0", rig.log) == "main/13.7.0"
    rig.ui.non_interactive = False
    rig.input.answers = ["y"]
    cli.cmd_uninstall(rig.ctx, rig.args("uninstall"))
    removed = [a[3] for a in rig.sh.argvs("capture") if a[:3] == ["docker", "image", "rm"]]
    assert "cognita-workspace-toolbox:12.6.0" in removed


def test_finding7_a_rollback_writes_the_folders_fragment_into_the_release_it_selects_first(rig):
    _two_releases(rig)
    second = f"{rig.root}/docs two"
    rig.host.add_dir(second)
    cli.cmd_add_folder(rig.ctx, rig.args("add-folder", second))
    rig.tool.calls.clear()
    assert cli.cmd_rollback(rig.ctx, unattended(rig, "rollback")) == cli.EXIT_OK
    fragments = [c for c in rig.tool.calls if c[0] == "write_folders_fragment"]
    assert len(fragments) == 1 and fragments[0][1].replace("\\", "/").endswith("/local/14.1.0")
    names = rig.tool.names()
    assert names.index("write_folders_fragment") < names.index("select_release")
    assert rig.env()["COGNITA_PROJECTS_ROOT_2"] == second               # and the env kept the added folder


def test_finding8_an_update_with_a_failing_weight_download_still_proceeds_and_warns(rig):
    installed(rig)
    rig.write_published("14.2.0")
    rig.ocr_error = OSError("no network")
    assert _update(rig, "--after-pull") == cli.EXIT_OK
    assert rig.env()["COGNITA_VERSION"] == "14.2.0"
    assert ("qa_release", "14.2.0", "cpu") in rig.tool.calls
    assert "OCR model files could not be downloaded" in rig.screen()


def test_finding9_yes_does_not_accept_plain_http_admin_on_the_lan(rig):
    with pytest.raises(cli.CliError) as info:
        run_install(rig, "--admin-lan")                                 # run_install passes --yes
    assert "--accept-plain-http-admin" in (info.value.hint or "")
    assert not Path(rig.data).exists() and not rig.env_path.exists()


def test_finding9_an_interactive_no_to_plain_http_is_respected_even_with_yes(rig):
    rig.ui.non_interactive = False
    rig.input.answers = ["n"]
    args = rig.install_args("--admin-lan", "--yes", "--workspace", "on", "--remote-access", "no",
                            "--admin-password-file", password_file(rig))
    with pytest.raises(cli.CliError) as info:
        cli.cmd_install(rig.ctx, args)
    assert "declined" in str(info.value)
    assert any("plain HTTP" in prompt for prompt in rig.input.prompts)


def test_finding9_the_explicit_flag_accepts_plain_http_admin_unattended(rig):
    assert run_install(rig, "--admin-lan", "--accept-plain-http-admin") == cli.EXIT_OK
    assert rig.env()["COGNITA_ADMIN_BIND_ADDRESS"] == "0.0.0.0"


def test_finding10_release_py_gets_the_install_log_for_its_unit_enable_and_folders_fragment(rig):
    installed(rig)
    assert "unit: fake enable of cognita.service" in _install_log(rig)      # rel_enable_unit passes the log
    second = f"{rig.root}/docs two"
    rig.host.add_dir(second)
    cli.cmd_add_folder(rig.ctx, rig.args("add-folder", second))
    assert "folders: fake fragment for" in _install_log(rig, "add-folder")   # so does the fragment


def test_finding11_a_successful_update_prunes_to_the_two_newest_releases_after_the_proof(rig):
    installed(rig)
    rig.write_published("14.2.0")
    rig.tool.calls.clear()
    assert _update(rig, "--after-pull") == cli.EXIT_OK
    assert cli.KEEP_STAGED_RELEASES == 2 and ("prune", 2) in rig.tool.calls
    names = rig.tool.names()
    assert names.index("qa_release") < names.index("prune")
    assert "prune: fake prune keep=2" in _install_log(rig, "update")


def test_finding11_a_failed_update_and_a_rollback_prune_nothing(rig):
    installed(rig)
    rig.write_published("14.2.0")
    rig.tool.qa_error = release.ReleaseError("verify-failed", "self-test failed")
    with pytest.raises(cli.CliError):
        _update(rig, "--after-pull")
    assert "prune" not in rig.tool.names()
    rig.tool.qa_error = None
    rig.tool.calls.clear()
    cli.cmd_rollback(rig.ctx, unattended(rig, "rollback"))
    assert "prune" not in rig.tool.names()


def test_finding11_a_prune_problem_never_fails_an_update_that_worked(rig):
    installed(rig)
    rig.write_published("14.2.0")
    rig.tool.prune_error = release.ReleaseError("usage", "docker busy")
    assert _update(rig, "--after-pull") == cli.EXIT_OK
    assert "Updated to 14.2.0." in rig.screen() and "older releases could not be cleaned up" in rig.screen()


# --------------------------------------------------------------------------
# 15.0.0 (DESIGN-NVIDIA-ACCELERATION 9 and 11): the nvidia profile in the installer
# --------------------------------------------------------------------------

NVIDIA_LSPCI = "01:00.0 VGA compatible controller [0300]: NVIDIA Corporation AD102 [10de:2684] (rev a1)\n"
RUNTIMES_WITH_NVIDIA = '{"nvidia":{"path":"nvidia-container-runtime"},"runc":{"path":"runc"}}\n'
RUNTIMES_WITHOUT_NVIDIA = '{"runc":{"path":"runc"}}\n'
RUNTIMES_ARGV = ("docker", "info", "--format", "{{json .Runtimes}}")


def _nvidia_machine(rig, *, runtime=True, lspci=True):
    """The kernel driver is loaded, Docker knows the `nvidia` runtime, lspci (when asked for) lists the card,
    and the published release lists an NVIDIA image."""
    rig.host.existing.add("/proc/driver/nvidia/version")
    rig.sh.script.insert(0, (RUNTIMES_ARGV, cli.Result(0, RUNTIMES_WITH_NVIDIA if runtime else RUNTIMES_WITHOUT_NVIDIA)))
    if lspci:
        rig.host.commands.add("lspci")
        rig.sh.when("lspci", "-nn", out=NVIDIA_LSPCI)
    rig.write_published(nvidia=True)


def _install_with(rig, *extra) -> dict:
    """A finished unattended install with extra install flags; the env file as it was written."""
    assert run_install(rig, *extra) == cli.EXIT_OK
    return rig.env()


def _set_runtime(rig, present: bool):
    rig.sh.script.insert(0, (RUNTIMES_ARGV, cli.Result(0, RUNTIMES_WITH_NVIDIA if present else RUNTIMES_WITHOUT_NVIDIA)))


def _stage_profiles(rig):
    return [c[1] for c in rig.tool.calls if c[0] == "stage_published"]


def _questions(rig):
    return [prompt for prompt in rig.input.prompts if "Acceleration" in prompt]


def test_nvidia_is_turned_on_through_admin_verified_and_proven_on_the_gpu(rig):
    _nvidia_machine(rig)
    assert run_install(rig, "--acceleration", "nvidia") == cli.EXIT_OK
    assert rig.env()["COGNITA_ACCELERATION"] == "nvidia"
    patches = [b for m, p, b in rig.admin_state.calls if (m, p) == ("PATCH", "/api/settings/gpu-acceleration")]
    assert len(patches) == 1 and patches[0]["knowledge"] == {"gpu_enabled": True, "gpu_device_ids": []}
    calls = [f"{m} {p}" for m, p, _b in rig.admin_state.calls]
    assert calls.index("POST /api/settings/gpu-acceleration/verify") > calls.index("PATCH /api/settings/gpu-acceleration")
    assert ("qa_release", "14.1.0", "nvidia") in rig.tool.calls
    assert _stage_profiles(rig) == ["nvidia"]                                    # no fallback
    screen = rig.screen()
    assert "Checking the NVIDIA card with real inference (up to two minutes)..." in screen
    assert "Acceleration    NVIDIA GPU" in screen                                 # the plan
    assert "Acceleration NVIDIA GPU" in screen                                    # the finish screen
    log = _install_log(rig)
    assert "facts: gpu:" in log and "nvidia_driver=True nvidia_wsl=False nvidia_runtime=True lspci_nvidia=True" in log
    assert "check: nvidia_ok=True" in log and "choice: offered GPU profiles=['nvidia']" in log


def test_the_nvidia_size_comes_from_the_published_file_in_the_plan_and_the_download_line(rig):
    _nvidia_machine(rig)
    assert run_install(rig, "--acceleration", "nvidia") == cli.EXIT_OK
    expected = cli.gb(cli.download_bytes(cli.parse_published((rig.repo / "containers" / "published-release.txt")
                                                             .read_text(encoding="utf-8")), "nvidia", True))
    assert f"Download        about {expected}" in rig.screen()
    assert cli.image_download_bytes({"size_cognita_nvidia": "5100000000"}, "nvidia", False) == (
        5_100_000_000 + cli.POSTGRES_IMAGE_BYTES)                                 # the existing size_cognita_<profile> pattern


def test_an_unverified_nvidia_card_falls_back_to_cpu_says_why_and_removes_the_nvidia_image_reference(rig):
    _nvidia_machine(rig)
    rig.admin_state.verify_result = {"state": "failed", "cards": [], "cleanup": "passed",
                                     "runtimes": {"embedding": {"reason": "canary_failed"}, "ocr": {}}}
    assert run_install(rig, "--acceleration", "nvidia") == cli.EXIT_OK
    log = _install_log(rig)
    assert "NVIDIA acceleration did not verify: embedding: canary_failed. Switching to CPU." in log
    assert "AMD acceleration did not verify" not in log
    assert rig.env()["COGNITA_ACCELERATION"] == "cpu"
    assert _stage_profiles(rig) == ["nvidia", "cpu"]
    assert [c[2] for c in rig.tool.calls if c[0] == "qa_release"] == ["cpu"]      # the proof ran once, on the CPU
    removed = [a[-1] for a in rig.sh.argvs("capture") if a[:3] == ["docker", "image", "rm"]]
    assert removed == ["cognita/app:14.1.0-local-nvidia-abc123"]
    assert "gpu: removed the unused NVIDIA image reference cognita/app:14.1.0-local-nvidia-abc123" in log
    assert "Cognita 14.1.0 is installed and working." in log


def test_a_driver_that_is_too_old_is_named_in_the_fallback_sentence(rig):
    _nvidia_machine(rig)
    rig.admin_state.verify_result = {"state": "failed", "cards": [], "cleanup": "passed",
                                     "runtimes": {"embedding": {"reason": "driver_too_old"}, "ocr": {}}}
    assert run_install(rig, "--acceleration", "nvidia") == cli.EXIT_OK
    assert ("NVIDIA acceleration did not verify: the NVIDIA driver is older than R580 "
            "(CUDA 13 needs R580 or newer). Switching to CPU.") in _install_log(rig)
    assert cli.gpu_reason({"runtimes": {"embedding": {}, "ocr": {"reason": "driver_too_old"}}}) == cli.DRIVER_TOO_OLD_TEXT
    assert cli.gpu_reason({"cards": [{"component": "embedding", "reason": "driver_too_old"}]}) == cli.DRIVER_TOO_OLD_TEXT
    assert cli.gpu_reason({"runtimes": {"embedding": {"reason": "canary_failed"}}}) == "embedding: canary_failed"


def test_a_proof_that_fails_on_the_nvidia_gpu_also_falls_back_to_cpu(rig):
    _nvidia_machine(rig)
    rig.tool.qa_fail_profiles = {"nvidia"}
    assert run_install(rig, "--acceleration", "nvidia") == cli.EXIT_OK
    assert rig.env()["COGNITA_ACCELERATION"] == "cpu"
    assert [c[2] for c in rig.tool.calls if c[0] == "qa_release"] == ["nvidia", "cpu"]
    assert "NVIDIA acceleration did not verify" in _install_log(rig)


def test_asking_for_nvidia_without_a_card_is_refused_and_says_the_card_is_what_is_missing(rig):
    with pytest.raises(cli.CliError) as info:
        run_install(rig, "--acceleration", "nvidia")
    assert "--acceleration nvidia was given but no NVIDIA GPU that Cognita can use was found." in str(info.value)
    assert "Drop the flag to install for the CPU." in (info.value.hint or "")
    assert not Path(rig.data).exists()                                            # refused before anything was created


def test_the_refusal_carries_the_driver_or_toolkit_note_that_explains_it(rig):
    rig.host.commands.add("lspci")
    rig.sh.when("lspci", "-nn", out=NVIDIA_LSPCI)                                 # a card, no driver
    with pytest.raises(cli.CliError) as info:
        run_install(rig, "--acceleration", "nvidia")
    assert cli.NVIDIA_DRIVER_MESSAGE in (info.value.hint or "")
    rig.host.existing.add("/proc/driver/nvidia/version")                          # a driver, but Docker has no runtime
    _set_runtime(rig, False)
    rig.write_published(nvidia=True)
    with pytest.raises(cli.CliError) as info:
        run_install(rig, "--acceleration", "nvidia")
    assert cli.NVIDIA_RUNTIME_MESSAGE in (info.value.hint or "")


def test_asking_for_nvidia_when_the_release_publishes_no_nvidia_image_is_refused_and_says_the_image_is_missing(rig):
    _nvidia_machine(rig)
    rig.write_published()                                                         # no NVIDIA image listed
    with pytest.raises(cli.CliError) as info:
        run_install(rig, "--acceleration", "nvidia")
    assert "--acceleration nvidia was given but the published release has no NVIDIA image." in str(info.value)
    assert "no NVIDIA GPU" not in str(info.value)                                 # the card is fine; the image is what is missing


def test_an_nvidia_card_with_no_published_image_installs_for_the_cpu_and_says_so(rig):
    _nvidia_machine(rig)
    rig.write_published()
    assert run_install(rig) == cli.EXIT_OK
    assert rig.env()["COGNITA_ACCELERATION"] == "cpu"
    assert "an NVIDIA GPU was found, but the published release has no NVIDIA image" in rig.screen()
    assert "choice: acceleration cpu because published-release.txt has no image_ref_cognita_nvidia" in _install_log(rig)
    assert _questions(rig) == []


def test_no_qualifying_vendor_means_no_question_and_the_cpu_silently(rig):
    rig.ui.non_interactive = False
    args = rig.install_args("--yes", "--workspace", "on", "--remote-access", "no",
                            "--admin-password-file", password_file(rig))
    assert cli.cmd_install(rig.ctx, args) == cli.EXIT_OK
    assert _questions(rig) == [] and rig.env()["COGNITA_ACCELERATION"] == "cpu"
    assert not [line for line in rig.log.said if "GPU" in line and "found" in line]      # nothing offered, nothing said


def test_one_qualifying_nvidia_card_asks_the_familiar_question_naming_nvidia_and_its_size(rig):
    _nvidia_machine(rig)
    rig.ui.non_interactive = False
    rig.input.answers = ["nvidia"]
    args = rig.install_args("--yes", "--workspace", "on", "--remote-access", "no",
                            "--admin-password-file", password_file(rig))
    assert cli.cmd_install(rig.ctx, args) == cli.EXIT_OK
    assert _questions(rig) == ["Acceleration: cpu or nvidia [nvidia]: "]           # default is the vendor
    assert "An NVIDIA GPU that Cognita can use was found. The NVIDIA image is about 5.1 GB to download." in rig.screen()
    assert rig.env()["COGNITA_ACCELERATION"] == "nvidia"


def test_declining_the_single_nvidia_offer_installs_for_the_cpu(rig):
    _nvidia_machine(rig)
    rig.ui.non_interactive = False
    rig.input.answers = ["cpu"]
    args = rig.install_args("--yes", "--workspace", "on", "--remote-access", "no",
                            "--admin-password-file", password_file(rig))
    assert cli.cmd_install(rig.ctx, args) == cli.EXIT_OK
    assert rig.env()["COGNITA_ACCELERATION"] == "cpu" and _stage_profiles(rig) == ["cpu"]


def test_an_unattended_install_with_one_nvidia_card_takes_the_default_which_is_nvidia(rig):
    _nvidia_machine(rig)
    assert run_install(rig) == cli.EXIT_OK
    assert rig.env()["COGNITA_ACCELERATION"] == "nvidia"


def _both_vendors_machine(rig):
    _amd_machine(rig)
    _nvidia_machine(rig, lspci=False)                     # no lspci: neither card is contradicted


def test_a_machine_with_both_asks_one_question_listing_all_three_and_defaults_to_amd(rig):
    _both_vendors_machine(rig)
    rig.ui.non_interactive = False
    rig.input.answers = [""]                              # Enter: the default
    args = rig.install_args("--yes", "--workspace", "on", "--remote-access", "no",
                            "--admin-password-file", password_file(rig))
    assert cli.cmd_install(rig.ctx, args) == cli.EXIT_OK
    assert _questions(rig) == ["Acceleration: cpu, amd or nvidia [amd]: "]
    screen = rig.screen()
    assert "GPUs that Cognita can use were found: AMD and NVIDIA." in screen
    assert "The AMD image is about 20.0 GB to download." in screen and "The NVIDIA image is about 5.1 GB to download." in screen
    assert rig.env()["COGNITA_ACCELERATION"] == "amd"


def test_a_machine_with_both_lets_the_user_pick_nvidia_and_rejects_a_word_that_is_not_a_choice(rig):
    _both_vendors_machine(rig)
    rig.ui.non_interactive = False
    rig.input.answers = ["nvidia"]
    args = rig.install_args("--yes", "--workspace", "on", "--remote-access", "no",
                            "--admin-password-file", password_file(rig))
    assert cli.cmd_install(rig.ctx, args) == cli.EXIT_OK
    assert rig.env()["COGNITA_ACCELERATION"] == "nvidia"
    # An answer that is none of the three is refused with the three named (a fresh machine: the rig is reused).
    rig.input.answers = ["intel"]
    rig.env_path.unlink()
    with pytest.raises(cli.CliError, match="Acceleration must be cpu, amd or nvidia, not 'intel'"):
        cli.cmd_install(rig.ctx, rig.install_args("--yes", "--workspace", "on", "--remote-access", "no",
                                                  "--admin-password-file", password_file(rig)))


def test_a_machine_with_both_can_be_told_which_with_the_flag_and_unattended_defaults_to_amd(rig):
    _both_vendors_machine(rig)
    assert run_install(rig, "--acceleration", "nvidia") == cli.EXIT_OK
    assert rig.env()["COGNITA_ACCELERATION"] == "nvidia"


def test_a_machine_with_both_unattended_and_no_flag_takes_amd(rig):
    _both_vendors_machine(rig)
    assert run_install(rig) == cli.EXIT_OK
    assert rig.env()["COGNITA_ACCELERATION"] == "amd"


def test_an_nvidia_card_beside_an_amd_card_with_no_amd_image_is_offered_alone(rig):
    _both_vendors_machine(rig)
    published = rig.repo / "containers" / "published-release.txt"
    published.write_text("\n".join(line for line in published.read_text().splitlines()
                                   if "cognita_amd" not in line) + "\n", encoding="utf-8")
    rig.ui.non_interactive = False
    rig.input.answers = ["nvidia"]
    args = rig.install_args("--yes", "--workspace", "on", "--remote-access", "no",
                            "--admin-password-file", password_file(rig))
    assert cli.cmd_install(rig.ctx, args) == cli.EXIT_OK
    assert _questions(rig) == ["Acceleration: cpu or nvidia [nvidia]: "]
    assert "the published release has no AMD image" in rig.screen()


def test_sizing_uses_the_larger_image_when_two_vendors_qualify_and_only_listed_images_count(rig):
    facts = cli.Facts(kfd=True, render_nodes=["/dev/dri/renderD128"], amdgpu_module=True, nvidia_driver=True,
                      nvidia_runtime=True)
    published = {"image_ref_cognita_amd": "a", "size_cognita_amd": "20000000000",
                 "image_ref_cognita_nvidia": "n", "size_cognita_nvidia": "5100000000"}
    args = SimpleNamespace(acceleration=None)

    def sized(published=published, chosen=None, saved=None, facts=facts):
        args.acceleration = chosen
        return cli.sizing_profile_for(rig.ctx, args, {"COGNITA_ACCELERATION": saved} if saved else {}, facts, published)

    assert sized() == "amd"                                                       # 20 GB beats 5.1 GB
    bigger_nvidia = {**published, "size_cognita_nvidia": "25000000000"}
    assert sized(bigger_nvidia) == "nvidia"                                       # the LARGER one, whichever vendor
    assert sized(chosen="cpu") == "cpu" and sized(saved="cpu") == "cpu"
    assert sized(chosen="nvidia") == "nvidia" and sized(saved="nvidia") == "nvidia"
    no_amd_image = {k: v for k, v in published.items() if "amd" not in k}
    assert sized(no_amd_image) == "nvidia"                                        # an unlisted image cannot be chosen
    assert sized({}) == "cpu"
    no_nvidia_card = cli.Facts(kfd=True, render_nodes=["/dev/dri/renderD128"], amdgpu_module=True)
    assert sized(saved="nvidia", facts=no_nvidia_card) == "amd"                   # the saved vendor is gone: the open choice
    assert sized(facts=cli.Facts()) == "cpu"


def test_a_saved_nvidia_choice_is_kept_on_a_rerun_with_no_question(rig):
    _nvidia_machine(rig)
    env = _install_with(rig, "--acceleration", "nvidia")
    _give_admin_a_hash(env)
    rig.input.prompts.clear()
    args = rig.args("install", "--non-interactive", "--yes", "--remote-access", "no",
                    "--admin-password-file", password_file(rig))
    assert cli.cmd_install(rig.ctx, args) == cli.EXIT_OK
    assert rig.env()["COGNITA_ACCELERATION"] == "nvidia" and _questions(rig) == []
    assert "choice: keeping acceleration=nvidia from the env file" in _install_log(rig)
    assert "Acceleration must" not in rig.screen()


def test_a_saved_nvidia_choice_the_machine_can_no_longer_honor_degrades_to_cpu_with_the_note(rig):
    _nvidia_machine(rig)
    env = _install_with(rig, "--acceleration", "nvidia")
    _give_admin_a_hash(env)
    _set_runtime(rig, False)                              # the toolkit was removed under the working install
    rig.log.said.clear()
    args = rig.args("install", "--non-interactive", "--yes", "--remote-access", "no",
                    "--admin-password-file", password_file(rig))
    assert cli.cmd_install(rig.ctx, args) == cli.EXIT_OK
    assert rig.env()["COGNITA_ACCELERATION"] == "cpu"
    assert ("Note: this install used NVIDIA acceleration, but no NVIDIA GPU that Cognita can use was found now, "
            "so it will run on the CPU.") in rig.screen()
    assert "choice: acceleration nvidia -> cpu because no qualifying NVIDIA card is present" in _install_log(rig)
    assert _stage_profiles(rig)[-1] == "cpu"


def test_update_stages_nvidia_again_when_the_gpu_can_still_be_honored(rig):
    _nvidia_machine(rig)
    _give_admin_a_hash(_install_with(rig, "--acceleration", "nvidia"))
    rig.write_published("14.2.0", nvidia=True)
    assert _update(rig, "--after-pull") == cli.EXIT_OK
    assert _stage_profiles(rig) == ["nvidia", "nvidia"] and rig.env()["COGNITA_ACCELERATION"] == "nvidia"
    assert "update: re-checked saved acceleration=nvidia: honored=True" in _install_log(rig, "update")
    assert "no NVIDIA GPU that Cognita can use" not in rig.screen()


def test_update_re_qualifies_a_saved_nvidia_profile_before_staging_and_degrades_to_cpu_with_the_note(rig):
    _nvidia_machine(rig)
    _give_admin_a_hash(_install_with(rig, "--acceleration", "nvidia"))
    _set_runtime(rig, False)                              # the runtime is gone by the time of the update
    rig.write_published("14.2.0", nvidia=True)
    rig.log.said.clear()
    assert _update(rig, "--after-pull") == cli.EXIT_OK
    assert _stage_profiles(rig) == ["nvidia", "cpu"]      # the second staging, the update's, is the CPU image
    assert rig.env()["COGNITA_ACCELERATION"] == "cpu"
    assert ("Note: this install used NVIDIA acceleration, but no NVIDIA GPU that Cognita can use was found now, "
            "so it will run on the CPU.") in rig.screen()
    assert [c[2] for c in rig.tool.calls if c[0] == "qa_release"][-1] == "cpu"
    log = _install_log(rig, "update")
    assert "update: re-checked saved acceleration=nvidia: honored=False" in log
    assert "update: acceleration nvidia -> cpu because no qualifying NVIDIA card is present" in log


def test_update_re_qualifies_a_saved_amd_profile_too_and_a_cpu_install_is_not_asked_about_a_gpu(rig):
    _amd_machine(rig)
    _give_admin_a_hash(_install_with(rig, "--acceleration", "amd"))
    rig.host.existing.discard("/dev/kfd")                 # /dev/kfd is gone
    rig.write_published("14.2.0")
    assert _update(rig, "--after-pull") == cli.EXIT_OK
    assert _stage_profiles(rig) == ["amd", "cpu"] and rig.env()["COGNITA_ACCELERATION"] == "cpu"
    assert "this install used AMD acceleration, but no AMD GPU that Cognita can use was found now" in rig.screen()


def test_update_of_a_cpu_install_never_asks_the_machine_about_a_gpu(rig):
    _give_admin_a_hash(installed(rig))
    rig.write_published("14.2.0", nvidia=True)
    rig.sh.calls.clear()
    assert _update(rig, "--after-pull") == cli.EXIT_OK
    assert not [argv for argv in rig.sh.argvs() if argv[:2] == ["docker", "info"] and "{{json .Runtimes}}" in argv]
    assert _stage_profiles(rig)[-1] == "cpu"
    assert "no GPU profile to re-check" in _install_log(rig, "update")


def test_the_arguments_accept_nvidia_and_reject_anything_else():
    parser = cli.build_parser()
    assert parser.parse_args(["install", "--acceleration", "nvidia"]).acceleration == "nvidia"
    with pytest.raises(SystemExit):
        parser.parse_args(["install", "--acceleration", "intel"])


# -- status: the one hint, both vendors -----------------------------------------------------------------------

STATUS_HINT = "the GPU runtime this install was set up for is missing; run `./cognita install --acceleration cpu`"


def _status_lines(rig) -> str:
    rig.log.said.clear()
    assert cli.cmd_status(rig.ctx, rig.args("status")) == cli.EXIT_OK
    return rig.screen()


def test_status_of_a_failed_nvidia_install_whose_runtime_is_gone_prints_the_repair_hint_once(rig):
    _nvidia_machine(rig)
    _install_with(rig, "--acceleration", "nvidia")
    rig.sh.when("systemctl", "--user", "is-active", rc=3, out="failed\n")
    _set_runtime(rig, False)
    text = _status_lines(rig)
    assert text.count(STATUS_HINT) == 1


def test_status_of_a_failed_amd_install_whose_kfd_is_gone_prints_the_same_hint(rig):
    _amd_machine(rig)
    _install_with(rig, "--acceleration", "amd")
    rig.sh.when("systemctl", "--user", "is-active", rc=3, out="failed\n")
    rig.host.existing.discard("/dev/kfd")
    assert _status_lines(rig).count(STATUS_HINT) == 1


def test_status_prints_no_hint_when_the_unit_is_not_failed_or_the_runtime_is_still_there_or_it_is_a_cpu_install(rig):
    _nvidia_machine(rig)
    _install_with(rig, "--acceleration", "nvidia")
    rig.sh.script.insert(0, (("systemctl", "--user", "is-active"), cli.Result(0, "active\n")))
    _set_runtime(rig, False)
    assert STATUS_HINT not in _status_lines(rig)                                  # running: not a failure to diagnose
    rig.sh.script[0] = (("systemctl", "--user", "is-active"), cli.Result(3, "inactive\n"))
    assert STATUS_HINT not in _status_lines(rig)                                  # stopped on purpose
    rig.sh.script[0] = (("systemctl", "--user", "is-active"), cli.Result(3, "failed\n"))
    _set_runtime(rig, True)
    assert STATUS_HINT not in _status_lines(rig)                                  # failed for some other reason


def test_status_gives_no_cpu_repair_when_docker_itself_does_not_answer(rig):
    """15.0 review: a daemon down after a host or WSL restart read as "no nvidia runtime", and the hint told the
    user to reinstall on the CPU, which drops acceleration for good and leaves the real fault."""
    _nvidia_machine(rig)
    _install_with(rig, "--acceleration", "nvidia")
    rig.sh.when("systemctl", "--user", "is-active", rc=3, out="failed\n")
    rig.sh.script.insert(0, (RUNTIMES_ARGV, cli.Result(1, "", "Cannot connect to the Docker daemon")))
    assert STATUS_HINT not in _status_lines(rig)
    _set_runtime(rig, False)              # control: the same failed unit with Docker answering "no runtime"
    assert _status_lines(rig).count(STATUS_HINT) == 1


def test_status_hints_when_the_nvidia_driver_is_gone_even_though_the_runtime_is_still_registered(rig):
    """Under WSL the Windows driver can go while the distro's `nvidia` runtime stays registered; the hint uses the
    same test `update` does, so the vanished driver counts."""
    _nvidia_machine(rig)
    _install_with(rig, "--acceleration", "nvidia")
    rig.sh.when("systemctl", "--user", "is-active", rc=3, out="failed\n")
    rig.host.existing.discard("/proc/driver/nvidia/version")
    assert _status_lines(rig).count(STATUS_HINT) == 1


def _rerun_install(rig, *extra):
    rig.log.said.clear()
    return cli.cmd_install(rig.ctx, rig.args("install", "--non-interactive", "--yes", "--remote-access", "no",
                                             "--admin-password-file", password_file(rig), *extra))


def test_a_rerun_stops_rather_than_downgrading_a_saved_nvidia_when_docker_does_not_answer_about_it(rig):
    """15.0 second review: unknown is never "gone". `docker version` answers but the runtime list does not, so
    whether NVIDIA still works is unknown; saving `cpu` on that guess would drop acceleration for good."""
    _nvidia_machine(rig)
    _give_admin_a_hash(_install_with(rig, "--acceleration", "nvidia"))
    rig.sh.script.insert(0, (RUNTIMES_ARGV, cli.Result(1, "", "context deadline exceeded")))
    with pytest.raises(cli.CliError) as info:
        _rerun_install(rig)
    assert "Docker did not answer when asked about its NVIDIA runtime" in str(info.value)
    assert "./cognita install --acceleration cpu" in (info.value.hint or "")
    assert rig.env()["COGNITA_ACCELERATION"] == "nvidia"                 # nothing was saved on a guess
    assert "no NVIDIA GPU that Cognita can use" not in rig.screen()
    # Asking for the CPU explicitly is not a guess, so it goes through.
    assert _rerun_install(rig, "--acceleration", "cpu") == cli.EXIT_OK
    assert rig.env()["COGNITA_ACCELERATION"] == "cpu"


def test_an_explicit_amd_on_a_two_vendor_machine_is_not_stopped_by_the_unknown_nvidia_runtime(rig):
    """Third review: the stop is for a run that would have to guess about NVIDIA. AMD does not depend on it."""
    _both_vendors_machine(rig)
    _give_admin_a_hash(_install_with(rig, "--acceleration", "nvidia"))
    rig.sh.script.insert(0, (RUNTIMES_ARGV, cli.Result(1, "", "context deadline exceeded")))
    assert _rerun_install(rig, "--acceleration", "amd") == cli.EXIT_OK
    assert rig.env()["COGNITA_ACCELERATION"] == "amd"


OLD_DRIVER_PROC ="NVRM version: NVIDIA UNIX x86_64 Kernel Module  550.127.05  Tue Oct  8 03:00:00 UTC 2024\n"


def test_update_names_an_nvidia_driver_that_fell_below_r580(rig):
    """15.0 second review: `update` never runs the step-1 notes, so it said "no NVIDIA GPU ... was found" when the
    card was there and only the driver was too old."""
    _nvidia_machine(rig)
    _give_admin_a_hash(_install_with(rig, "--acceleration", "nvidia"))
    rig.host.files["/proc/driver/nvidia/version"] = OLD_DRIVER_PROC
    rig.write_published("14.2.0", nvidia=True)
    rig.log.said.clear()
    assert _update(rig, "--after-pull") == cli.EXIT_OK
    assert rig.env()["COGNITA_ACCELERATION"] == "cpu"
    screen = rig.screen()
    assert "the NVIDIA driver is now older than R580" in screen
    assert "./cognita install --acceleration nvidia" in screen
    assert "no NVIDIA GPU that Cognita can use" not in screen


def test_status_names_an_nvidia_driver_that_fell_below_r580(rig):
    _nvidia_machine(rig)
    _install_with(rig, "--acceleration", "nvidia")
    rig.sh.when("systemctl", "--user", "is-active", rc=3, out="failed\n")
    rig.host.files["/proc/driver/nvidia/version"] = OLD_DRIVER_PROC
    text = _status_lines(rig)
    assert "the NVIDIA driver is older than R580" in text and STATUS_HINT not in text


def test_update_keeps_a_saved_nvidia_profile_when_docker_does_not_answer(rig):
    """Unknown is not gone: the saved choice stays, and staging (which needs Docker) decides on its own."""
    _nvidia_machine(rig)
    _give_admin_a_hash(_install_with(rig, "--acceleration", "nvidia"))
    rig.sh.script.insert(0, (RUNTIMES_ARGV, cli.Result(1, "", "Cannot connect to the Docker daemon")))
    rig.write_published("14.2.0", nvidia=True)
    rig.log.said.clear()
    assert _update(rig, "--after-pull") == cli.EXIT_OK
    assert rig.env()["COGNITA_ACCELERATION"] == "nvidia"
    assert "no NVIDIA GPU that Cognita can use" not in rig.screen()
    assert "update: saved acceleration=nvidia kept: Docker did not answer" in _install_log(rig, "update")


def test_status_of_a_failed_cpu_install_never_hints_at_a_gpu_runtime(rig):
    installed(rig)
    rig.sh.when("systemctl", "--user", "is-active", rc=3, out="failed\n")
    rig.sh.calls.clear()
    assert STATUS_HINT not in _status_lines(rig)
    assert not [argv for argv in rig.sh.argvs() if argv[:2] == ["docker", "info"]]


def test_status_json_reports_nvidia_as_the_acceleration(rig):
    _nvidia_machine(rig)
    _install_with(rig, "--acceleration", "nvidia")
    rig.log.said.clear()
    assert cli.cmd_status(rig.ctx, rig.args("status", "--json")) == cli.EXIT_OK
    assert json.loads(rig.log.said[0])["acceleration"] == "nvidia"


# -- diagnostics: gpu.txt ---------------------------------------------------------------------------------------


def _diagnostic_texts(rig, tmp_path):
    import zipfile
    out = tmp_path / "diag" / "diag.zip"
    assert cli.cmd_diagnostics(rig.ctx, rig.args("diagnostics", "--out", str(out))) == cli.EXIT_OK
    with zipfile.ZipFile(out) as bundle:
        return {name: bundle.read(name).decode("utf-8") for name in bundle.namelist()}


def test_diagnostics_of_an_nvidia_install_lists_the_cards_with_nvidia_smi(rig, tmp_path):
    _nvidia_machine(rig)
    _install_with(rig, "--acceleration", "nvidia")
    rig.host.commands.add("nvidia-smi")
    rig.sh.when("/usr/bin/nvidia-smi", "-L", out="GPU 0: NVIDIA GeForce RTX 4090 (UUID: GPU-4cd28834-e5a4-6b4e-85aa-3e54bcbf0630)\n")
    texts = _diagnostic_texts(rig, tmp_path)
    assert "$ /usr/bin/nvidia-smi -L" in texts["gpu.txt"] and "RTX 4090" in texts["gpu.txt"]
    assert "gpu.error.txt" not in texts


def test_diagnostics_under_wsl_finds_nvidia_smi_off_the_path(rig, tmp_path):
    _nvidia_machine(rig)
    _install_with(rig, "--acceleration", "nvidia")
    rig.host.existing.add(cli.NVIDIA_WSL_SMI)
    rig.sh.when(cli.NVIDIA_WSL_SMI, "-L", out="GPU 0: NVIDIA GeForce RTX 4090 (UUID: GPU-x)\n")
    assert f"$ {cli.NVIDIA_WSL_SMI} -L" in _diagnostic_texts(rig, tmp_path)["gpu.txt"]


def test_diagnostics_notes_a_missing_nvidia_smi_and_is_never_fatal(rig, tmp_path):
    _nvidia_machine(rig)
    _install_with(rig, "--acceleration", "nvidia")
    texts = _diagnostic_texts(rig, tmp_path)
    assert "nvidia-smi is not installed on this machine" in texts["gpu.txt"]
    assert "status.txt" in texts and "gpu.error.txt" not in texts


def test_diagnostics_of_an_amd_install_lists_dev_dri(rig, tmp_path):
    _amd_machine(rig)
    _install_with(rig, "--acceleration", "amd")
    rig.host.existing.add("/dev/dri")
    rig.sh.when("ls", "-l", "/dev/dri", out="crw-rw---- 1 root video 226, 1 Sep 29 card1\n")
    text = _diagnostic_texts(rig, tmp_path)["gpu.txt"]
    assert "$ ls -l /dev/dri" in text and "card1" in text


def test_diagnostics_of_a_cpu_install_has_no_gpu_file(rig, tmp_path):
    installed(rig)
    assert "gpu.txt" not in _diagnostic_texts(rig, tmp_path)


# --------------------------------------------------------------------------
# 15.1.0 (design 22.6 and 22.12 items 2, 8, 9): --acceleration-fallback cpu, and the `acceleration`-stage
# warnings Setup shows when the Linux side drops a GPU
# --------------------------------------------------------------------------

FALLBACK_TIME = datetime.datetime(2026, 9, 30, 9, 0, 0)


def _progress_to(rig, tmp_path) -> Path:
    """Switch the progress file on the way main() does, with a fixed clock (nothing here waits)."""
    path = tmp_path / "progress.jsonl"
    rig.ui.progress = cli.Progress(path, log=rig.log, render=rig.ui.render, clock=lambda: FALLBACK_TIME)
    rig.ui.progress.start_command()
    return path


def _accel_warnings(path: Path) -> list[dict]:
    lines = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    return [line for line in lines if line["stage"] == "acceleration" and line["state"] == "warning"]


def _fallback_install(rig, *extra):
    return run_install(rig, "--acceleration", "nvidia", "--acceleration-fallback", "cpu", *extra)


def test_the_fallback_installs_for_the_cpu_when_no_card_qualifies_with_one_warning_and_the_notes_on_screen(rig, tmp_path):
    rig.host.commands.add("lspci")
    rig.sh.when("lspci", "-nn", out=NVIDIA_LSPCI)                                 # a card, no driver
    path = _progress_to(rig, tmp_path)
    assert _fallback_install(rig) == cli.EXIT_OK
    assert rig.env()["COGNITA_ACCELERATION"] == "cpu" and _stage_profiles(rig) == ["cpu"]
    warnings = _accel_warnings(path)
    assert len(warnings) == 1
    assert warnings[0]["title"] == "Checking the NVIDIA card"                      # accel_label was set first
    assert warnings[0]["message"] == ("NVIDIA acceleration was chosen, but Cognita cannot use the NVIDIA GPU here "
                                      "(Linux cannot see an NVIDIA driver), so it will use the CPU.")
    assert "./cognita install" not in warnings[0]["message"]                      # the notes are screen-only
    screen = rig.screen()
    assert warnings[0]["message"] in screen and cli.NVIDIA_DRIVER_MESSAGE in screen
    log = _install_log(rig)
    assert "choice: --acceleration nvidia not usable here; --acceleration-fallback cpu -> cpu" in log
    assert "choice: acceleration=cpu workspace=True" in log


def test_the_fallback_works_on_a_machine_with_no_nvidia_signs_at_all_and_never_raises(rig, tmp_path):
    path = _progress_to(rig, tmp_path)
    assert _fallback_install(rig) == cli.EXIT_OK
    assert rig.env()["COGNITA_ACCELERATION"] == "cpu"
    assert [w["message"].split("(")[1].split(")")[0] for w in _accel_warnings(path)] == [
        "Linux cannot see an NVIDIA driver"]


def test_the_fallback_reason_names_an_nvidia_driver_older_than_580(rig, tmp_path):
    _nvidia_machine(rig)
    rig.host.files["/proc/driver/nvidia/version"] = OLD_DRIVER_PROC
    path = _progress_to(rig, tmp_path)
    assert _fallback_install(rig) == cli.EXIT_OK
    [warning] = _accel_warnings(path)
    assert "(the NVIDIA driver is older than 580)" in warning["message"]
    assert cli.NVIDIA_DRIVER_OLD_MESSAGE in rig.screen()


def test_the_fallback_reason_names_a_docker_without_the_nvidia_runtime(rig, tmp_path):
    _nvidia_machine(rig, runtime=False)
    path = _progress_to(rig, tmp_path)
    assert _fallback_install(rig) == cli.EXIT_OK
    [warning] = _accel_warnings(path)
    assert "(Docker does not know the nvidia runtime)" in warning["message"]
    assert cli.NVIDIA_RUNTIME_MESSAGE in rig.screen()


def test_the_fallback_reason_names_a_release_without_an_nvidia_image(rig, tmp_path):
    _nvidia_machine(rig)
    rig.write_published()                                                         # no NVIDIA image listed
    path = _progress_to(rig, tmp_path)
    assert _fallback_install(rig) == cli.EXIT_OK
    assert rig.env()["COGNITA_ACCELERATION"] == "cpu" and _stage_profiles(rig) == ["cpu"]
    [warning] = _accel_warnings(path)                                             # the loop's note is screen-only
    assert "(the published release has no NVIDIA image)" in warning["message"]
    assert "an NVIDIA GPU was found, but the published release has no NVIDIA image" in rig.screen()


def test_the_fallback_reason_for_a_card_lspci_denies_is_that_it_did_not_qualify(rig, tmp_path):
    _nvidia_machine(rig, lspci=False)
    rig.host.commands.add("lspci")
    rig.sh.when("lspci", "-nn", out="00:02.0 VGA compatible controller [0300]: Intel Corporation [8086:9a49]\n")
    path = _progress_to(rig, tmp_path)
    assert _fallback_install(rig) == cli.EXIT_OK
    [warning] = _accel_warnings(path)
    assert "(it did not qualify)" in warning["message"]


def test_the_reason_of_a_report_built_without_facts_is_that_it_did_not_qualify():
    assert cli.gpu_fallback_reason("nvidia", cli.Report(), no_image=False) == "it did not qualify"
    assert cli.gpu_fallback_reason("amd", cli.Report(), no_image=False) == "it did not qualify"
    assert cli.gpu_fallback_reason("amd", cli.Report(), no_image=True) == "the published release has no AMD image"


def test_the_fallback_still_installs_nvidia_when_the_card_and_the_image_qualify_with_no_warning(rig, tmp_path):
    _nvidia_machine(rig)
    path = _progress_to(rig, tmp_path)
    assert _fallback_install(rig) == cli.EXIT_OK
    assert rig.env()["COGNITA_ACCELERATION"] == "nvidia" and _stage_profiles(rig) == ["nvidia"]
    assert _accel_warnings(path) == []
    assert "not usable here" not in _install_log(rig)


def test_the_fallback_beside_a_qualifying_amd_card_still_picks_cpu_not_amd(rig, tmp_path):
    """Item 8: `requested` replaces a.acceleration for the rest of the function, so a non-empty `offered` (AMD here)
    cannot turn the dropped NVIDIA request into a pick."""
    _amd_machine(rig)                                                             # AMD qualifies, no NVIDIA at all
    path = _progress_to(rig, tmp_path)
    assert _fallback_install(rig) == cli.EXIT_OK
    assert rig.env()["COGNITA_ACCELERATION"] == "cpu" and _stage_profiles(rig) == ["cpu"]
    assert len(_accel_warnings(path)) == 1


def test_without_the_fallback_a_requested_gpu_that_cannot_be_honored_still_raises_and_warns_nothing(rig, tmp_path):
    path = _progress_to(rig, tmp_path)
    with pytest.raises(cli.CliError) as info:
        run_install(rig, "--acceleration", "nvidia")
    assert "--acceleration nvidia was given but no NVIDIA GPU that Cognita can use was found." in str(info.value)
    assert _accel_warnings(path) == []
    _nvidia_machine(rig)
    rig.write_published()
    with pytest.raises(cli.CliError) as info:
        run_install(rig, "--acceleration", "nvidia")
    assert "the published release has no NVIDIA image." in str(info.value)
    assert _accel_warnings(path) == []


def test_the_fallback_is_ignored_and_logged_without_a_gpu_acceleration_flag(rig, tmp_path):
    path = _progress_to(rig, tmp_path)
    assert run_install(rig, "--acceleration-fallback", "cpu") == cli.EXIT_OK
    assert _accel_warnings(path) == []
    assert ("choice: --acceleration-fallback cpu ignored: --acceleration is not given, and the fallback only "
            "applies to amd or nvidia") in _install_log(rig)
    assert run_install(rig, "--acceleration", "cpu", "--acceleration-fallback", "cpu") == cli.EXIT_OK
    assert "--acceleration is cpu, and the fallback only applies" in _install_log(rig)


def test_sizing_is_cpu_under_the_fallback_when_no_qualifying_card_is_open(rig):
    published = {"image_ref_cognita_nvidia": "n", "size_cognita_nvidia": "5100000000"}
    args = SimpleNamespace(acceleration="nvidia", acceleration_fallback="cpu")
    assert cli.sizing_profile_for(rig.ctx, args, {}, cli.Facts(), published) == "cpu"
    # The requested vendor is not open but ANOTHER one is: the install will still be the CPU one.
    amd_only = cli.Facts(kfd=True, render_nodes=["/dev/dri/renderD128"], amdgpu_module=True)
    both_published = {**published, "image_ref_cognita_amd": "a", "size_cognita_amd": "20000000000"}
    assert cli.sizing_profile_for(rig.ctx, args, {}, amd_only, both_published) == "cpu"
    args.acceleration_fallback = None                                             # unchanged without the option
    assert cli.sizing_profile_for(rig.ctx, args, {}, amd_only, both_published) == "amd"
    # A qualifying card with its image is still sized for it, fallback or not.
    nvidia_ok = cli.Facts(nvidia_driver=True, nvidia_runtime=True)
    args.acceleration_fallback = "cpu"
    assert cli.sizing_profile_for(rig.ctx, args, {}, nvidia_ok, published) == "nvidia"


def test_an_install_rerun_that_drops_a_saved_gpu_also_warns_at_the_acceleration_stage(rig, tmp_path):
    _nvidia_machine(rig)
    env = _install_with(rig, "--acceleration", "nvidia")
    _give_admin_a_hash(env)
    _set_runtime(rig, False)
    path = _progress_to(rig, tmp_path)
    args = rig.args("install", "--non-interactive", "--yes", "--remote-access", "no",
                    "--admin-password-file", password_file(rig))
    assert cli.cmd_install(rig.ctx, args) == cli.EXIT_OK
    [warning] = _accel_warnings(path)
    assert warning["title"] == "Checking the NVIDIA card"
    assert warning["message"] == ("This install used NVIDIA acceleration, but no NVIDIA GPU that Cognita can use "
                                  "was found now, so it will run on the CPU.")
    assert "Note: this install used NVIDIA acceleration" in rig.screen()


def test_a_rerun_with_the_fallback_that_drops_a_saved_gpu_warns_exactly_once(rig, tmp_path):
    _nvidia_machine(rig)
    env = _install_with(rig, "--acceleration", "nvidia")
    _give_admin_a_hash(env)
    _set_runtime(rig, False)
    path = _progress_to(rig, tmp_path)
    args = rig.args("install", "--non-interactive", "--yes", "--remote-access", "no",
                    "--admin-password-file", password_file(rig), "--acceleration", "nvidia",
                    "--acceleration-fallback", "cpu")
    assert cli.cmd_install(rig.ctx, args) == cli.EXIT_OK
    [warning] = _accel_warnings(path)
    assert "(Docker does not know the nvidia runtime)" in warning["message"]
    assert rig.env()["COGNITA_ACCELERATION"] == "cpu"
    assert "saved-profile note not emitted as a second acceleration warning" in _install_log(rig)


def test_an_install_rerun_that_drops_a_saved_gpu_names_an_old_driver(rig, tmp_path):
    """15.1.0 review: the rerun note had no facts, so a driver that fell below R580 read as "no GPU found"."""
    _nvidia_machine(rig)
    _give_admin_a_hash(_install_with(rig, "--acceleration", "nvidia"))
    rig.host.files["/proc/driver/nvidia/version"] = OLD_DRIVER_PROC
    path = _progress_to(rig, tmp_path)
    args = rig.args("install", "--non-interactive", "--yes", "--remote-access", "no",
                    "--admin-password-file", password_file(rig))
    assert cli.cmd_install(rig.ctx, args) == cli.EXIT_OK
    [warning] = _accel_warnings(path)
    assert warning["message"].startswith("This install used NVIDIA acceleration, but the NVIDIA driver is now "
                                         "older than R580")


def test_an_explicit_cpu_rerun_that_drops_a_saved_gpu_says_so_but_does_not_warn(rig, tmp_path):
    """15.1.0 review: Setup passes --acceleration cpu when the user picked CPU or its page said why NVIDIA is not
    offered; that is a choice, not a loss, so no acceleration warning (and no Setup "choose NVIDIA" line)."""
    _nvidia_machine(rig)
    _give_admin_a_hash(_install_with(rig, "--acceleration", "nvidia"))
    rig.host.files["/proc/driver/nvidia/version"] = OLD_DRIVER_PROC
    path = _progress_to(rig, tmp_path)
    args = rig.args("install", "--non-interactive", "--yes", "--remote-access", "no",
                    "--admin-password-file", password_file(rig), "--acceleration", "cpu")
    assert cli.cmd_install(rig.ctx, args) == cli.EXIT_OK
    assert _accel_warnings(path) == []
    assert rig.env()["COGNITA_ACCELERATION"] == "cpu"
    assert "Note: this install used NVIDIA acceleration, but the NVIDIA driver is now older" in rig.screen()
    assert "saved-profile note not emitted as an acceleration warning; --acceleration cpu was given" in _install_log(
        rig)


def test_update_that_drops_a_saved_gpu_also_warns_at_the_acceleration_stage_with_the_driver_reason(rig, tmp_path):
    _nvidia_machine(rig)
    _give_admin_a_hash(_install_with(rig, "--acceleration", "nvidia"))
    rig.host.files["/proc/driver/nvidia/version"] = OLD_DRIVER_PROC
    rig.write_published("14.2.0", nvidia=True)
    path = _progress_to(rig, tmp_path)
    assert _update(rig, "--after-pull") == cli.EXIT_OK
    [warning] = _accel_warnings(path)
    assert warning["title"] == "Checking the NVIDIA card"
    assert warning["message"].startswith("This install used NVIDIA acceleration, but the NVIDIA driver is now "
                                         "older than R580")
    assert "acceleration warning: vendor=nvidia why=saved profile no longer honored on update" in _install_log(
        rig, "update")


def test_update_that_keeps_its_gpu_emits_no_acceleration_warning(rig, tmp_path):
    _nvidia_machine(rig)
    _give_admin_a_hash(_install_with(rig, "--acceleration", "nvidia"))
    rig.write_published("14.2.0", nvidia=True)
    path = _progress_to(rig, tmp_path)
    assert _update(rig, "--after-pull") == cli.EXIT_OK
    assert _accel_warnings(path) == []
