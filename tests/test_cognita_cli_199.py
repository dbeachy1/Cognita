"""scripts/cognita_cli.py, design 19.9 (docs/DESIGN-LINUX-INSTALLER.md, the Windows chat's second request:
items 7, 8, 9, 12, 13 and 14; item 11 is the launcher, item 10 is not built).  The fakes are the ones
tests/test_cognita_cli.py defines.

Nothing here waits: the Funnel retry loop gets the recording sleeper, standard input is a list of byte lines
the Rig hands out, the process environment is a dict, and no command, Docker, systemd, network call or Admin
server is real.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_cognita_cli import Rig, _evaluate, _facts, cli
from test_cognita_cli_19 import progress_lines, with_progress
from test_cognita_cli_flows import _update, password_file, run_install, unattended

PASSWORD = "correct horse"
ADDRESS = "https://cognita.example.com"


@pytest.fixture
def rig(tmp_path, monkeypatch):
    rig = Rig(tmp_path, monkeypatch)
    rig.ocr_calls = []
    monkeypatch.setitem(sys.modules, "ocr_weights",
                        SimpleNamespace(fetch=lambda dest, log, progress=None: rig.ocr_calls.append(str(dest))))
    return rig


def installed(rig, *extra, **kw) -> dict:
    assert run_install(rig, *extra, **kw) == cli.EXIT_OK
    return rig.env()


def quiet(rig) -> None:
    """Forget everything the install did, so a later assertion sees only the command under test."""
    rig.sh.calls.clear()
    rig.admin_state.calls.clear()
    rig.http_calls.clear()
    rig.sleep.calls.clear()
    rig.log.said.clear()


def stdin_args(rig, command: str, *extra):
    return rig.args(command, "--admin-password-stdin", "--non-interactive", *extra)


def all_logs(rig) -> str:
    """Every log line the run wrote, in the files and in the pending buffer, plus what was said on screen."""
    files = "\n".join(p.read_text(encoding="utf-8") for p in Path(rig.data).rglob("*.log"))
    return files + "\n" + "\n".join(rig.log._pending) + "\n" + rig.screen()


# --------------------------------------------------------------------------
# Item 7: remote-access --external-url
# --------------------------------------------------------------------------


@pytest.mark.parametrize("bad", [
    "http://cognita.example.com", "cognita.example.com", "ftp://cognita.example.com", "https://",
    "https:///path", ADDRESS + "/mcp", ADDRESS + "/a/b", ADDRESS + "?x=1", ADDRESS + "/?x=1", ADDRESS + "#top",
    "https://user:hunter2@cognita.example.com", "https://user@cognita.example.com",
    "https://cognita.example.com:99999", "https://cognita.example.com:abc", " " + ADDRESS, ADDRESS + " ",
    "https://cognita .example.com", "https://cognita.example.com\n", "", "https://" + "a" * 2050,
])
def test_a_bad_external_url_is_refused_before_anything_else_happens(rig, bad):
    installed(rig)
    quiet(rig)
    args = unattended(rig, "remote-access", "--external-url", bad)
    with pytest.raises(cli.CliError) as info:
        cli.cmd_remote_access(rig.ctx, args)
    assert "--external-url cannot be used" in str(info.value) and "https://" in (info.value.hint or "")
    assert "hunter2" not in str(info.value) and "hunter2" not in all_logs(rig)   # a mistyped secret is not echoed
    assert rig.sh.calls == [] and rig.admin_state.calls == [] and rig.http_calls == []   # nothing ran, nothing was asked


@pytest.mark.parametrize("given,saved", [(ADDRESS, ADDRESS), (ADDRESS + "/", ADDRESS),
                                         ("https://cognita.example.com:8443", "https://cognita.example.com:8443"),
                                         ("HTTPS://Cognita.Example.com", "HTTPS://Cognita.Example.com")])
def test_a_good_external_url_is_saved_in_admin_and_checked(rig, given, saved):
    installed(rig)
    quiet(rig)
    rig.http_script = [(200, '{"service": "cognita"}'), (401, "")]
    assert cli.cmd_remote_access(rig.ctx, unattended(rig, "remote-access", "--external-url", given)) == cli.EXIT_OK
    patches = [body for method, path, body in rig.admin_state.calls
               if (method, path) == ("PATCH", "/api/settings/public-base-url")]
    assert patches == [{"public_base_url": saved}]
    assert rig.admin_state.public_url == saved
    # Section 10 step 7, unchanged: /healthz first, then the unauthenticated MCP POST.
    assert rig.http_calls == [("GET", f"{saved}/healthz"), ("POST", f"{saved}/mcp/connectors/self-test/mcp")]
    assert "answers at its public address from this machine" in rig.screen()
    assert "401 without a key: that is correct" in rig.screen() and "take a few minutes" in rig.screen()


def test_an_external_url_runs_no_tailscale_and_no_sudo_command_at_all(rig):
    installed(rig)
    quiet(rig)
    rig.host.commands.discard("tailscale")                                   # not installed: the Funnel path would install it
    rig.http_script = [(200, '{"service": "cognita"}'), (401, "")]
    assert cli.cmd_remote_access(rig.ctx, unattended(rig, "remote-access", "--external-url", ADDRESS)) == cli.EXIT_OK
    assert rig.sh.calls == []                                                # not one command of any kind
    assert "Tailscale is not installed" not in rig.screen() and "Funnel" not in rig.screen()
    assert "no Tailscale command is run" in all_logs(rig)


def test_an_external_url_keeps_the_healthz_retries_and_their_messages(rig):
    installed(rig)
    quiet(rig)
    rig.http_script = [(None, ""), (502, ""), (200, '{"service": "cognita"}'), (401, "")]
    assert cli.cmd_remote_access(rig.ctx, unattended(rig, "remote-access", "--external-url", ADDRESS)) == cli.EXIT_OK
    assert rig.sleep.calls == [10, 10]
    assert "attempt 1/18" in rig.screen() and "attempt 3/18" in rig.screen()

    quiet(rig)
    rig.http_script = [(502, "")] * 18
    with pytest.raises(cli.CliError) as info:
        cli.cmd_remote_access(rig.ctx, unattended(rig, "remote-access", "--external-url", ADDRESS))
    assert len(rig.http_calls) == 18 and len(rig.sleep.calls) == 17
    assert "502" in str(info.value) and "3 minutes" in str(info.value)

    quiet(rig)
    rig.http_script = [(200, '{"service": "cognita"}'), (502, "")]
    with pytest.raises(cli.CliError) as info:
        cli.cmd_remote_access(rig.ctx, unattended(rig, "remote-access", "--external-url", ADDRESS))
    assert "expected 401" in str(info.value)


def test_admin_refusing_the_external_url_is_reported_and_nothing_is_checked(rig):
    installed(rig)
    quiet(rig)
    rig.admin_state.fail[("PATCH", "/api/settings/public-base-url")] = cli.AdminError(400, "not acceptable")
    with pytest.raises(cli.CliError) as info:
        cli.cmd_remote_access(rig.ctx, unattended(rig, "remote-access", "--external-url", ADDRESS))
    assert f"Admin refused the public address {ADDRESS}" in str(info.value)
    assert rig.http_calls == [] and rig.admin_state.public_url is None


def test_a_tailscale_flag_beside_an_external_url_is_refused_not_dropped(rig):
    installed(rig)
    quiet(rig)
    for flags in (["--tailscale-name", "box"], ["--remote-access", "yes"]):
        with pytest.raises(cli.CliError) as info:
            cli.cmd_remote_access(rig.ctx, unattended(rig, "remote-access", "--external-url", ADDRESS, *flags))
        assert flags[0] in str(info.value) and "--external-url" in str(info.value)
    assert rig.sh.calls == [] and rig.admin_state.calls == []


def test_remote_access_writes_its_own_progress_stage(rig, tmp_path):
    installed(rig)
    rig.http_script = [(200, '{"service": "cognita"}'), (401, "")]
    path = with_progress(rig, tmp_path / "remote.jsonl")
    assert cli.cmd_remote_access(rig.ctx, unattended(rig, "remote-access", "--external-url", ADDRESS)) == cli.EXIT_OK
    assert [(line["stage"], line["state"]) for line in progress_lines(path)] == [
        ("remote_access", "start"), ("remote_access", "done")]


# --------------------------------------------------------------------------
# Item 8: update without git
# --------------------------------------------------------------------------


def test_plain_update_in_a_tree_with_no_git_says_so_and_runs_no_git_command(rig):
    installed(rig)
    quiet(rig)
    rig.host.existing.discard(str(rig.repo / ".git"))
    with pytest.raises(cli.CliError) as info:
        _update(rig)
    assert str(info.value) == "This Cognita has no git history; replace the tree and run update --no-pull."
    assert not any(argv[:1] == ["git"] for _kind, argv, _stdin in rig.sh.calls)
    assert "there is no git history to pull" in all_logs(rig)


def test_the_no_git_message_reaches_the_terminal_and_the_progress_file(rig, tmp_path, capsys):
    installed(rig)
    rig.host.existing.discard(str(rig.repo / ".git"))
    path = tmp_path / "update.jsonl"
    rig.ui.non_interactive = True
    argv = ["update", "--non-interactive", "--admin-password-file", password_file(rig), "--progress-file", str(path)]
    assert cli.main(argv, ctx=rig.ctx) == cli.EXIT_FAILED
    assert "This Cognita has no git history; replace the tree and run update --no-pull." in capsys.readouterr().err
    last = progress_lines(path)[-1]
    assert (last["stage"], last["state"]) == ("update", "failed")
    assert last["message"] == "This Cognita has no git history; replace the tree and run update --no-pull."


def test_update_no_pull_in_a_tree_with_no_git_moves_to_the_release_the_tree_names(rig):
    installed(rig)
    rig.host.existing.discard(str(rig.repo / ".git"))
    rig.write_published("14.2.0")
    quiet(rig)
    assert _update(rig, "--no-pull") == cli.EXIT_OK
    assert rig.env()["COGNITA_VERSION"] == "14.2.0"
    assert not any(argv[:1] == ["git"] for _kind, argv, _stdin in rig.sh.calls)


def test_a_clone_with_git_still_updates_the_old_way(rig):
    installed(rig)                                    # the Rig's repo has a .git
    heads = iter(["a" * 40, "a" * 40])
    rig.sh.script.insert(0, (("git", "-C", str(rig.repo), "rev-parse", "HEAD"),
                             lambda argv: cli.Result(0, next(heads) + "\n")))
    assert _update(rig) == cli.EXIT_OK
    assert ["git", "-C", str(rig.repo), "pull", "--ff-only"] in rig.sh.argvs("stream")


# --------------------------------------------------------------------------
# Item 9: --admin-password-stdin on every command Setup runs
# --------------------------------------------------------------------------


@pytest.mark.parametrize("command", [["install"], ["update"], ["rollback"], ["remote-access"], ["password"]])
def test_the_commands_setup_runs_take_the_password_flags_and_progress(command):
    parsed = cli.build_parser().parse_args([
        *command, "--admin-password-stdin", "--non-interactive", "--progress-file", "p"])
    assert parsed.admin_password_stdin is True and parsed.non_interactive is True and parsed.progress_file == "p"
    parsed = cli.build_parser().parse_args([*command, "--admin-password-file", "f", "--non-interactive"])
    assert parsed.admin_password_file == "f" and parsed.admin_password_stdin is False


def test_the_password_line_is_read_as_utf_8_and_only_the_trailing_newline_is_removed(rig):
    for raw, expected in ((b"correct horse\n", "correct horse"), (b"correct horse\r\n", "correct horse"),
                          (b"correct horse", "correct horse"), (b"  spaced  \n", "  spaced  "),
                          ("p\u00e4ssw\u00f6rd \u2713\n".encode("utf-8"), "p\u00e4ssw\u00f6rd \u2713"),
                          (b"two\n\n", "two\n")):
        rig.ctx.stdin_password = None
        rig.stdin_lines = [raw]
        assert cli.read_password_stdin(rig.ctx) == expected
        assert rig.stdin_lines == []                                          # exactly one line was taken
    rig.ctx.stdin_password = None
    rig.stdin_lines = [b"first\n", b"second\n"]
    assert cli.read_password_stdin(rig.ctx) == "first" and rig.stdin_lines == [b"second\n"]
    assert cli.read_password_stdin(rig.ctx) == "first"                        # stdin is read once, then remembered


@pytest.mark.parametrize("raw", [b"", b"\n", b"\r\n"])
def test_an_empty_password_on_stdin_is_refused(rig, raw):
    rig.stdin_lines = [raw]
    with pytest.raises(cli.CliError) as info:
        cli.get_password(rig.ctx, stdin_args(rig, "install"), prompt="x", twice=False)
    assert "empty" in str(info.value)


def test_a_password_that_is_not_utf_8_is_refused_without_echoing_it(rig):
    rig.stdin_lines = [b"bad\xff\xfe\n"]
    with pytest.raises(cli.CliError) as info:
        cli.get_password(rig.ctx, stdin_args(rig, "install"), prompt="x", twice=False)
    assert "not valid UTF-8" in str(info.value) and "bad" not in str(info.value)


def test_stdin_and_the_password_file_together_are_refused_before_anything_runs(rig, capsys):
    installed(rig)
    quiet(rig)
    rig.stdin_lines = [b"first line stays unread\n"]
    for command in (["install", "--documents", rig.docs], ["update"], ["rollback"], ["remote-access"], ["password"]):
        argv = [*command, "--admin-password-stdin", "--admin-password-file", password_file(rig), "--non-interactive"]
        assert cli.main(argv, ctx=rig.ctx) == cli.EXIT_FAILED
        err = capsys.readouterr().err
        assert "--admin-password-stdin and --admin-password-file cannot be used together" in err
    assert rig.stdin_lines == [b"first line stays unread\n"]                   # nothing was read
    assert rig.sh.calls == [] and rig.admin_state.calls == []
    with pytest.raises(cli.CliError):
        cli.get_password(rig.ctx, rig.args("install", "--admin-password-stdin", "--admin-password-file", "f",
                                           "--non-interactive"), prompt="x", twice=False)


def test_stdin_without_non_interactive_is_refused_because_a_question_would_read_the_password(rig, capsys):
    rig.stdin_lines = [b"secret-line\n"]
    assert cli.main(["install", "--documents", rig.docs, "--admin-password-stdin"], ctx=rig.ctx) == cli.EXIT_FAILED
    assert "--admin-password-stdin uses standard input for the password" in capsys.readouterr().err
    assert rig.stdin_lines == [b"secret-line\n"]


def test_install_reads_the_password_from_stdin_and_it_is_never_in_argv_or_a_log(rig, tmp_path):
    path = with_progress(rig, tmp_path / "install.jsonl")
    rig.stdin_lines = [(PASSWORD + "\n").encode()]
    rig.ui.non_interactive = True
    args = rig.install_args("--admin-password-stdin", "--non-interactive", "--yes", "--workspace", "on",
                            "--remote-access", "no")
    assert cli.cmd_install(rig.ctx, args) == cli.EXIT_OK
    assert rig.stdin_lines == []
    assert ("LOGIN", "admin", None) in rig.admin_state.calls                     # the proof logged in with it
    creds = [stdin for _kind, argv, stdin in rig.sh.calls if stdin and "password" in stdin]
    assert [json.loads(stdin)["password"] for stdin in creds] == [PASSWORD]      # set-admin-credentials got it on stdin
    for _kind, argv, _stdin in rig.sh.calls:
        assert PASSWORD not in " ".join(argv)
    assert PASSWORD not in all_logs(rig) and PASSWORD not in path.read_text(encoding="utf-8")
    assert "read from --admin-password-stdin (13 characters, not logged)" in all_logs(rig)


def test_main_reads_the_stdin_password_before_any_check_runs(rig):
    order = []
    rig.sh.on_call = lambda kind, argv, stdin: order.append(("command", argv[0]))
    rig.stdin_lines = [(PASSWORD + "\n").encode()]
    original = rig.ctx.stdin_line

    def spy():
        order.append(("stdin", ""))
        return original()

    rig.ctx.stdin_line = spy
    rig.ui.non_interactive = True
    argv = ["install", "--documents", rig.docs, "--data-dir", rig.data, "--admin-user", "admin",
            "--non-interactive", "--yes", "--workspace", "on", "--remote-access", "no", "--admin-password-stdin"]
    assert cli.main(argv, ctx=rig.ctx) == cli.EXIT_OK
    assert order[0] == ("stdin", "") and order.count(("stdin", "")) == 1          # first, and only once


def test_update_reads_the_stdin_password_and_proves_with_it(rig):
    installed(rig)
    rig.write_published("14.2.0")
    rig.stdin_lines = [(PASSWORD + "\n").encode()]
    assert cli.cmd_update(rig.ctx, stdin_args(rig, "update", "--after-pull")) == cli.EXIT_OK
    assert rig.stdin_lines == [] and rig.env()["COGNITA_VERSION"] == "14.2.0"
    assert PASSWORD not in all_logs(rig)


def test_a_pulling_update_leaves_stdin_unread_and_passes_the_flag_to_the_new_process(rig):
    installed(rig)
    heads = iter(["a" * 40, "b" * 40])
    rig.sh.script.insert(0, (("git", "-C", str(rig.repo), "rev-parse", "HEAD"),
                             lambda argv: cli.Result(0, next(heads) + "\n")))
    rig.stdin_lines = [(PASSWORD + "\n").encode()]
    rig.ui.non_interactive = True
    assert cli.main(["update", "--admin-password-stdin", "--non-interactive"], ctx=rig.ctx) == cli.EXIT_OK
    assert rig.reexec_calls == [["update", "--after-pull", "--admin-password-stdin", "--non-interactive"]]
    assert rig.stdin_lines == [(PASSWORD + "\n").encode()]                       # the new process will read it
    # The new process: same stdin, now read first thing.
    rig.write_published("14.2.0")
    assert cli.main(rig.reexec_calls[0], ctx=rig.ctx) == cli.EXIT_OK
    assert rig.stdin_lines == [] and rig.env()["COGNITA_VERSION"] == "14.2.0"


def test_rollback_reads_the_stdin_password(rig):
    installed(rig)
    rig.write_published("14.2.0")
    _update(rig, "--after-pull")
    rig.stdin_lines = [(PASSWORD + "\n").encode()]
    assert cli.cmd_rollback(rig.ctx, stdin_args(rig, "rollback")) == cli.EXIT_OK
    assert rig.stdin_lines == [] and rig.env()["COGNITA_VERSION"] == "14.1.0"


def test_remote_access_reads_the_stdin_password(rig):
    installed(rig)
    quiet(rig)
    rig.http_script = [(200, '{"service": "cognita"}'), (401, "")]
    rig.stdin_lines = [(PASSWORD + "\n").encode()]
    assert cli.cmd_remote_access(rig.ctx, stdin_args(rig, "remote-access", "--external-url", ADDRESS)) == cli.EXIT_OK
    assert rig.stdin_lines == [] and rig.admin_state.public_url == ADDRESS
    assert PASSWORD not in all_logs(rig)


def test_a_wrong_stdin_password_fails_at_the_admin_login_without_echoing_it(rig, capsys):
    installed(rig)
    quiet(rig)
    rig.stdin_lines = [b"not the password\n"]
    rig.ui.non_interactive = True
    assert cli.main(["remote-access", "--admin-password-stdin", "--non-interactive", "--external-url", ADDRESS],
                    ctx=rig.ctx) == cli.EXIT_FAILED
    assert "not the password" not in capsys.readouterr().err + all_logs(rig)


def test_password_command_reads_the_stdin_password_for_the_new_admin_password(rig):
    installed(rig)
    quiet(rig)
    rig.stdin_lines = [b"a brand new password\n"]
    assert cli.cmd_password(rig.ctx, stdin_args(rig, "password")) == cli.EXIT_OK
    sent = [json.loads(stdin) for _kind, argv, stdin in rig.sh.calls if stdin and "password" in stdin]
    assert [item["password"] for item in sent] == ["a brand new password"]
    assert "a brand new password" not in all_logs(rig)


# --------------------------------------------------------------------------
# Item 12: session variables
# --------------------------------------------------------------------------


def _log_text(rig) -> str:
    return "\n".join(rig.log._pending)


def test_both_session_variables_are_set_when_unset_and_their_paths_exist(rig):
    rig.host.add_dir("/run/user/1000")
    rig.host.existing.add("/run/user/1000/bus")
    cli.ensure_session_env(rig.ctx)
    assert rig.environ == {"XDG_RUNTIME_DIR": "/run/user/1000",
                           "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/1000/bus"}
    text = _log_text(rig)
    assert "session: XDG_RUNTIME_DIR was unset; set to /run/user/1000" in text
    assert "session: DBUS_SESSION_BUS_ADDRESS was unset; set to unix:path=/run/user/1000/bus" in text


def test_nothing_is_set_when_the_directory_and_the_socket_do_not_exist(rig):
    cli.ensure_session_env(rig.ctx)
    assert rig.environ == {}
    text = _log_text(rig)
    assert "XDG_RUNTIME_DIR is unset and /run/user/1000 does not exist; left unset" in text
    assert "DBUS_SESSION_BUS_ADDRESS is unset and there is no runtime directory to look in; left unset" in text


def test_the_directory_alone_sets_only_the_runtime_dir(rig):
    rig.host.add_dir("/run/user/1000")
    cli.ensure_session_env(rig.ctx)
    assert rig.environ == {"XDG_RUNTIME_DIR": "/run/user/1000"}
    assert "DBUS_SESSION_BUS_ADDRESS is unset and /run/user/1000/bus does not exist; left unset" in _log_text(rig)


def test_a_variable_that_is_already_set_is_never_overwritten(rig):
    rig.host.add_dir("/run/user/1000")
    rig.host.existing.add("/run/user/1000/bus")
    rig.environ.update({"XDG_RUNTIME_DIR": "/somewhere/else", "DBUS_SESSION_BUS_ADDRESS": "unix:path=/other/bus"})
    cli.ensure_session_env(rig.ctx)
    assert rig.environ == {"XDG_RUNTIME_DIR": "/somewhere/else", "DBUS_SESSION_BUS_ADDRESS": "unix:path=/other/bus"}
    text = _log_text(rig)
    assert "XDG_RUNTIME_DIR is already set (/somewhere/else); left as it is" in text
    assert "DBUS_SESSION_BUS_ADDRESS is already set (unix:path=/other/bus); left as it is" in text


def test_the_bus_is_looked_for_in_the_runtime_dir_that_is_already_set(rig):
    rig.environ["XDG_RUNTIME_DIR"] = "/custom/run"
    rig.host.existing.add("/custom/run/bus")
    cli.ensure_session_env(rig.ctx)
    assert rig.environ == {"XDG_RUNTIME_DIR": "/custom/run", "DBUS_SESSION_BUS_ADDRESS": "unix:path=/custom/run/bus"}


def test_main_sets_the_variables_at_startup_before_the_command_runs(rig):
    installed(rig)
    rig.host.add_dir("/run/user/1000")
    rig.host.existing.add("/run/user/1000/bus")
    rig.environ.clear()
    seen = []
    rig.sh.on_call = lambda kind, argv, stdin: seen.append((argv[0], dict(rig.environ)))
    assert cli.main(["restart"], ctx=rig.ctx) == cli.EXIT_OK
    assert seen and seen[0][0] == "systemctl"                          # the command did run, and ran after the setup
    assert all(env == {"XDG_RUNTIME_DIR": "/run/user/1000", "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/1000/bus"}
               for _name, env in seen)


# --------------------------------------------------------------------------
# Item 13: no sudo when nothing needs it
# --------------------------------------------------------------------------


def test_an_install_with_docker_the_manager_check_and_linger_all_fine_runs_no_sudo_and_asks_for_no_logout(
        rig, tmp_path):
    rig.sh.linger = True                                              # linger is already on
    path = with_progress(rig, tmp_path / "progress.jsonl")
    assert run_install(rig) == cli.EXIT_OK                            # Docker present, systemd-run docker info passes
    assert any(argv[:1] == ["systemd-run"] for _kind, argv, _stdin in rig.sh.calls)     # the manager check ran
    assert not any(argv[0] == "sudo" or "sudo" in argv for _kind, argv, _stdin in rig.sh.calls)
    assert rig.sh.argvs("interactive") == []
    text = rig.screen().lower()
    for words in ("log out", "logout", "reboot", "log in again", "next login", "sudo"):
        assert words not in text, words
    assert "COGNITA_LINGER_SET_BY_INSTALLER" not in rig.env()        # the installer did not turn linger on
    assert not any(line["state"] == "warning" for line in progress_lines(path))
    assert "linger: already on; nothing to do" in all_logs(rig)


# --------------------------------------------------------------------------
# Item 14: a documents root is never chown'd or chmod'd, and a failure names it by its display
# --------------------------------------------------------------------------


@pytest.fixture
def ownership_calls(monkeypatch):
    """Every chmod / chown of any path, whichever way it is spelled.  The real chmod still runs (the layout step
    sets 0700 on the install's own folders); a chown does not exist on every host, so it is recorded only."""
    calls: list[tuple[str, str]] = []
    real_chmod = os.chmod

    def chmod(path, *args, **kwargs):
        calls.append(("chmod", os.fspath(path).replace("\\", "/")))
        return real_chmod(path, *args, **kwargs)

    def recorder(name):
        def record(path, *args, **kwargs):
            calls.append((name, os.fspath(path).replace("\\", "/")))
        return record

    monkeypatch.setattr(os, "chmod", chmod)
    for name in ("chown", "lchown"):
        monkeypatch.setattr(os, name, recorder(name), raising=False)
    monkeypatch.setattr(shutil, "chown", recorder("shutil.chown"))
    return calls


def test_no_chown_or_chmod_is_ever_run_on_a_documents_root_across_install_add_folder_and_uninstall(
        rig, ownership_calls):
    second = f"{rig.root}/docs two"
    rig.host.add_dir(second)
    Path(second).mkdir()
    (Path(rig.docs) / "note.md").write_text("mine", encoding="utf-8")
    env = installed(rig)
    assert cli.cmd_add_folder(rig.ctx, rig.args("add-folder", second)) == cli.EXIT_OK
    assert len(cli.document_roots(rig.env())) == 2
    unit = Path(rig.host.home()) / ".config" / "systemd" / "user" / "cognita.service"
    unit.parent.mkdir(parents=True, exist_ok=True)
    unit.write_text("[Unit]\n", encoding="utf-8")
    rig.ui.non_interactive = False
    rig.input.answers = ["y"]
    assert cli.cmd_uninstall(rig.ctx, rig.args("uninstall")) == cli.EXIT_OK

    roots = [rig.docs, second]
    # The control: the hook does see the install's own chmods (0700 on its data folders), so a silent hook would fail here.
    own = [path for name, path in ownership_calls if name == "chmod"]
    assert own and all(path.startswith(env["COGNITA_RELEASES_ROOT"].replace("\\", "/")) or
                       path.startswith(cli.data_dir_of(env).replace("\\", "/")) for path in own)
    touched = [(name, path) for name, path in ownership_calls if any(cli.overlaps(path, root) for root in roots)]
    assert touched == []
    # And through the Runner: no chown or chmod command of any kind, with or without sudo.
    for _kind, argv, _stdin in rig.sh.calls:
        assert not {"chown", "chmod", "chgrp", "setfacl"} & set(argv), argv
    assert (Path(rig.docs) / "note.md").read_text(encoding="utf-8") == "mine"


def test_the_documents_check_names_a_root_by_its_display_when_it_has_one(rig):
    shown = "D:\\Documents"
    for state, phrase in (("missing", "does not exist"), ("notdir", "is not a directory"),
                          ("noread", "is not readable by you"), ("nowrite", "is not writable by you")):
        report = _evaluate(rig, _facts(rig, docs={rig.docs: state}), displays=[shown])
        [problem] = report.problems
        assert problem.name == "Documents folder"
        assert problem.saw == f"{shown} {phrase}"
        assert rig.docs not in problem.saw and rig.docs not in problem.fix
        assert "mkdir" not in problem.fix and "chmod" not in problem.fix
    # Without a display the root's own path is named, as before.
    [problem] = _evaluate(rig, _facts(rig, docs={rig.docs: "missing"})).problems
    assert problem.saw == f"{rig.docs} does not exist" and "mkdir -p" in problem.fix
    # A display belongs to its own root: the second root, with none, is still named by its path.
    other = f"{rig.root}/other"
    report = _evaluate(rig, _facts(rig, docs={rig.docs: "missing", other: "missing"}), docs=[rig.docs, other],
                       displays=[shown, ""])
    assert [p.saw for p in report.problems] == [f"{shown} does not exist", f"{other} does not exist"]


def test_an_install_whose_documents_folder_is_missing_says_the_display_on_screen(rig):
    rig.host.existing.discard(rig.docs)
    rig.host.dirs.discard(rig.docs)
    with pytest.raises(cli.CliError) as info:
        run_install(rig, "--documents-display", "D:\\Documents")
    assert "check(s) failed" in str(info.value)
    screen = rig.screen()
    assert "D:\\Documents does not exist" in screen and rig.docs not in screen


def test_add_folder_names_the_new_folder_by_its_display_in_a_refusal(rig):
    installed(rig)
    missing = f"{rig.root}/not there"
    with pytest.raises(cli.CliError) as info:
        cli.cmd_add_folder(rig.ctx, rig.args("add-folder", missing, "--display", "E:\\Work"))
    assert str(info.value) == "E:\\Work is not usable: it does not exist."
    assert missing not in str(info.value)
    with pytest.raises(cli.CliError) as plain:
        cli.cmd_add_folder(rig.ctx, rig.args("add-folder", missing))
    assert str(plain.value) == f"{missing} is not usable: it does not exist."
