"""Skip the self-tests, the CLI half (DESIGN-WINDOWS-INSTALLER section 21.2).

A `<progress file>.skip` file that appears during the proof stops it: the proof's cleanup still runs, the
install finishes with exit 0, and install.env remembers `COGNITA_PROOF=skipped` until a proof passes.
The fake release tool's qa_release (tests/test_cognita_cli.py) stands in for release.run's stop_check:
``rig.tool.qa_stop`` is called with the stop_check the CLI handed over.  No test waits on the clock.
"""
from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_cognita_cli import Rig, cli, release
from test_cognita_cli_flows import _proof, _update, installed, run_install, target

FIXED_NOW = dt.datetime(2026, 9, 29, 12, 30, 0)
SKIPPED_TEXT = "The self-tests were stopped at your request. Run Setup again later to run them."


@pytest.fixture
def rig(tmp_path, monkeypatch):
    """The flows rig, with the OCR weights fetch taking the progress callback a progress-file run passes."""
    rig = Rig(tmp_path, monkeypatch)
    monkeypatch.setitem(sys.modules, "ocr_weights", SimpleNamespace(fetch=lambda dest, log, progress=None: None))
    return rig


def with_progress(rig, path: Path) -> Path:
    rig.ui.progress = cli.Progress(path, log=rig.log, render=rig.ui.render, clock=lambda: FIXED_NOW)
    rig.ui.progress.start_command()
    return path


def progress_lines(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def press_skip_during_the_selftest(rig, path: Path) -> None:
    """The person presses the button while the live self-test runs: the helper creates the skip file, and
    release.run's next stop_check answers true, so it terminates the child and raises Stopped."""
    skip = Path(str(path) + ".skip")

    def stop(stop_check):
        skip.write_text("", encoding="utf-8")
        if stop_check is not None and stop_check():
            raise release.Stopped("selftest was stopped on request")

    rig.tool.qa_stop = stop


def test_a_skip_during_the_selftest_cleans_up_finishes_with_exit_0_and_records_skipped(rig, tmp_path):
    path = with_progress(rig, tmp_path / "progress.jsonl")
    press_skip_during_the_selftest(rig, path)
    assert run_install(rig) == cli.EXIT_OK
    assert rig.env()["COGNITA_PROOF"] == "skipped"
    # The cleanup ran: no Self-Test project, no proof connector, an empty Self-Test root.
    assert rig.admin_state.projects == [] and rig.admin_state.connectors == []
    assert not any(cli.selftest_root(rig.env()).iterdir())
    # The one proof line the skip writes, then the install goes on to its finish.
    lines = progress_lines(path)
    proof = [line for line in lines if line["stage"] == "proof"]
    assert [line["state"] for line in proof] == ["start", "done"]
    assert proof[-1]["title"] == "Self-tests skipped" and proof[-1]["message"] == SKIPPED_TEXT
    assert [line["stage"] for line in lines][-2:] == ["finish", "finish"]
    assert lines[-1]["state"] == "done"
    assert SKIPPED_TEXT in rig.screen() and "Proof passed" not in rig.screen()
    # Every decision is in the install log with its value.
    logs = cli.local_root(rig.env()) / "logs"
    text = "\n".join(p.read_text(encoding="utf-8") for p in logs.glob("install-*.log"))
    for fragment in (f"proof: skip file {path}.skip", "proof: skip requested:", "proof: SKIPPED",
                     "proof: skipped at the person's request; cleanup done", "COGNITA_PROOF=skipped recorded"):
        assert fragment in text, fragment


def test_a_skip_pressed_before_the_throwaway_project_is_provisioned_starts_no_self_test(rig, tmp_path):
    path = with_progress(rig, tmp_path / "progress.jsonl")
    Path(str(path) + ".skip").write_text("", encoding="utf-8")
    assert run_install(rig) == cli.EXIT_OK
    assert rig.env()["COGNITA_PROOF"] == "skipped"
    assert "qa_release" not in rig.tool.names()                # the live self-test never started
    assert rig.admin_state.projects == [] and rig.admin_state.connectors == []      # what was created is gone
    assert not [c for c in rig.sh.calls if any("provision_selftest.py" in part for part in c[1])]


def test_a_passing_proof_records_passed_and_a_run_with_a_progress_file_hands_qa_a_stop_check(rig, tmp_path):
    with_progress(rig, tmp_path / "progress.jsonl")
    assert run_install(rig) == cli.EXIT_OK
    assert rig.env()["COGNITA_PROOF"] == "passed"
    assert len(rig.tool.qa_stop_checks) == 1 and callable(rig.tool.qa_stop_checks[0])
    assert rig.tool.qa_stop_checks[0]() is False               # no skip file, so the check says "go on"


def test_a_run_without_a_progress_file_cannot_be_skipped_and_passes_no_stop_check(rig):
    assert run_install(rig) == cli.EXIT_OK
    assert rig.tool.qa_stop_checks == [None]
    assert rig.env()["COGNITA_PROOF"] == "passed"


def test_the_skip_file_is_the_progress_file_name_plus_dot_skip(rig, tmp_path):
    assert cli.proof_skip_file(rig.ctx) is None
    path = with_progress(rig, tmp_path / "sub" / "p.jsonl")
    assert cli.proof_skip_file(rig.ctx) == Path(str(path) + ".skip")


def test_run_proof_returns_the_outcome_and_the_skipped_proof_leaves_the_env_in_step(rig, tmp_path):
    env = installed(rig)
    path = with_progress(rig, tmp_path / "progress.jsonl")
    press_skip_during_the_selftest(rig, path)
    assert cli.run_proof(rig.ctx, env, target(rig), "admin", "correct horse") == "skipped"
    assert env["COGNITA_PROOF"] == "skipped" and rig.env()["COGNITA_PROOF"] == "skipped"


def test_a_skipped_proof_whose_cleanup_fails_is_a_failure_and_leaves_result_unverified(rig, tmp_path):
    env = installed(rig)
    before = rig.env()["COGNITA_PROOF"]
    path = with_progress(rig, tmp_path / "progress.jsonl")
    press_skip_during_the_selftest(rig, path)
    rig.admin_state.fail[("DELETE", "/api/projects/")] = cli.AdminError(500, "boom")
    with pytest.raises(cli.ProofFailed) as info:
        _proof(rig, env)
    assert "was skipped but its cleanup failed" in str(info.value)
    assert before == "passed" and cli.PROOF_KEY not in rig.env()  # the old proof no longer describes this attempt
    assert cli.status_data(rig.ctx)["proof"] is None


def test_an_update_whose_proof_is_skipped_still_finishes_and_records_skipped(rig, tmp_path):
    installed(rig)
    rig.write_published("14.2.0")
    path = with_progress(rig, tmp_path / "update.jsonl")
    press_skip_during_the_selftest(rig, path)
    assert _update(rig, "--after-pull") == cli.EXIT_OK
    env = rig.env()
    assert env["COGNITA_PROOF"] == "skipped" and env["COGNITA_VERSION"] == "14.2.0"
    assert rig.admin_state.projects == [] and rig.admin_state.connectors == []


def test_a_later_passing_proof_replaces_skipped_and_the_status_line_goes_away(rig, tmp_path, capsys):
    path = with_progress(rig, tmp_path / "progress.jsonl")
    press_skip_during_the_selftest(rig, path)
    assert run_install(rig) == cli.EXIT_OK
    rig.tool.qa_stop = None
    Path(str(path) + ".skip").unlink()
    assert run_install(rig) == cli.EXIT_OK                     # a repair run: the proof passes this time
    assert rig.env()["COGNITA_PROOF"] == "passed"
    rig.log.said.clear()
    assert cli.cmd_status(rig.ctx, rig.args("status")) == cli.EXIT_OK
    assert not [line for line in rig.log.said if "Self-tests:" in line]


def test_status_says_skipped_only_when_the_self_tests_were_skipped(rig, tmp_path):
    path = with_progress(rig, tmp_path / "progress.jsonl")
    press_skip_during_the_selftest(rig, path)
    assert run_install(rig) == cli.EXIT_OK
    rig.log.said.clear()
    assert cli.cmd_status(rig.ctx, rig.args("status")) == cli.EXIT_OK
    assert "Self-tests: skipped at install (run Setup again to run them)" in rig.log.said
    rig.log.said.clear()
    assert cli.cmd_status(rig.ctx, rig.args("status", "--json")) == cli.EXIT_OK
    assert json.loads(rig.log.said[0])["proof"] == "skipped"


def test_status_json_proof_is_null_when_nothing_was_recorded_or_the_value_is_unknown(rig):
    installed(rig)
    env = rig.env()
    del env["COGNITA_PROOF"]
    cli.write_env(rig.ctx, env)
    assert cli.status_data(rig.ctx)["proof"] is None
    env["COGNITA_PROOF"] = "garbage"
    cli.write_env(rig.ctx, env)
    assert cli.status_data(rig.ctx)["proof"] is None
    env["COGNITA_PROOF"] = "passed"
    cli.write_env(rig.ctx, env)
    assert cli.status_data(rig.ctx)["proof"] == "passed"
