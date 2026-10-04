"""Canonical Windows deployment boundaries against synthetic local inputs.

The Git remote is an owned local bare fixture. The PowerShell script records
dispatch arguments and never touches WSL/Docker or the real installation.
"""
from __future__ import annotations

import argparse
import contextlib
from dataclasses import dataclass
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace

import pytest

from scripts import release, release_windows as windows


IDENTIFIER = "11111111-2222-4333-8444-555555555555"


@pytest.fixture
def owned_root():
    # Register teardown immediately, including exceptions during fixture setup.
    temporary = tempfile.TemporaryDirectory(prefix="cognita-release-windows-")
    root = Path(temporary.name)
    try:
        yield root
    finally:
        temporary.cleanup()
        assert not root.exists(), f"Owned test root remains: {root}"


def git(repo, *arguments):
    return windows.owned_process(["git", "-C", str(repo), *arguments], cwd=repo, timeout=30)


def git_ok(repo, *arguments):
    result = git(repo, *arguments)
    assert result.returncode == 0, arguments
    return result.stdout.strip()


@dataclass
class Candidate:
    repo: Path
    origin: Path
    state: Path
    bundle: Path
    commit: str
    values: dict

    @property
    def args(self):
        return argparse.Namespace(bundle=self.bundle, expected_installation_id=IDENTIFIER)

    def publish_manifest(self):
        (self.bundle / "release.txt").write_text(
            "".join(f"{key}: {value}\n" for key, value in self.values.items()), encoding="utf-8")
        self.close_bundle()

    def close_bundle(self):
        paths = sorted(item for item in self.bundle.rglob("*") if item.is_file() and item.name != "SHA256SUMS")
        release._write_sha256sums(paths, self.bundle / "SHA256SUMS", relative_to=self.bundle)

    def push(self):
        git_ok(self.repo, "push", "--quiet", "origin", "HEAD:refs/heads/main")


@pytest.fixture
def candidate(owned_root, monkeypatch):
    repo, origin, state, bundle = (owned_root / name for name in ("repo", "origin", "state", "bundle"))
    for path in (repo, origin, state, bundle):
        path.mkdir()
    git_ok(origin, "init", "--bare", "--quiet")
    git_ok(repo, "init", "--quiet", "--initial-branch=main")
    git_ok(repo, "config", "core.autocrlf", "false")
    git_ok(repo, "config", "user.email", "fixture@example.invalid")
    git_ok(repo, "config", "user.name", "Synthetic Windows boundary test")
    git_ok(repo, "remote", "add", "origin", str(origin))
    sources = {
        "src/cognita/release_identity.py": 'APPLICATION_VERSION = "13.6.0"\nTOOLBOX_VERSION = "12.6.0"\n',
        "compose.yaml": "services:\n  postgres:\n    image: pgvector/pgvector:pg18@sha256:" + "a" * 64 + "\n",
        "compose.cpu.yaml": "services: {}\n",
        "compose.workspace.yaml": "services: {}\n",
        "scripts/windows/Update-Release.py": '# Synthetic lifecycle input; never invoked.\n',
        "scripts/windows/Install-CognitaWindows.ps1": (
            "param([string]$Action,[string]$BundlePath,[string]$ExpectedInstallationId)\n"
            f"$script:Root = '{state}'\n"
            "$script:RecordPath = Join-Path $script:Root 'install.json'\n"
            "if ($Action -cne 'UpdateRelease') { exit 9 }\n"
            "@{action=$Action;bundle=$BundlePath;installation_id=$ExpectedInstallationId;"
            "cwd=(Get-Location).Path;script=$PSCommandPath} | ConvertTo-Json | "
            "Set-Content -LiteralPath (Join-Path $script:Root 'dispatch.json')\n"
            "Write-Output 'synthetic-secret-must-not-be-logged'\n"
            "[Console]::Error.WriteLine('COGNITA_UPDATE_PHASE apply-toolbox failed')\n"
            "[Console]::Error.WriteLine('COGNITA_UPDATE_PHASE preflight-source-state failed')\n"
            "[Console]::Error.WriteLine('synthetic-secret-must-not-be-logged')\n"
            "if (Test-Path (Join-Path $script:Root 'refuse')) { exit 1 }\n"
            "exit 0\n"
        ),
    }
    for relative, contents in sources.items():
        source = repo / relative
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(contents.encode("utf-8"))
        if not relative.startswith("src/"):
            target = bundle / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
    git_ok(repo, "add", ".")
    git_ok(repo, "commit", "--quiet", "-m", "Synthetic source candidate")
    commit = git_ok(repo, "rev-parse", "HEAD")
    (state / "install.json").write_text(json.dumps({"installation_id": IDENTIFIER}), encoding="utf-8")
    values = {
        "version": "13.6.0", "commit": commit, "bundle_mode": "full",
        "image_ref_cognita_cpu": release.candidate_app_reference("cpu", "13.6.0", commit),
        "image_cognita_cpu": "sha256:" + "b" * 64,
        "image_ref_workspace_runtime": release.candidate_workspace_reference("13.6.0", commit),
        "image_workspace_runtime": "sha256:" + "c" * 64,
        "image_ref_postgres": release.postgres_service_reference(repo), "image_postgres": "sha256:" + "d" * 64,
        "test_runner_ref": release.candidate_test_reference("13.6.0", commit), "test_runner_id": "sha256:" + "e" * 64,
        "toolbox_version": "12.6.0",
    }
    for name in ("cognita-cpu.tar", "workspace-runtime.tar", "toolbox.tar"):
        (bundle / name).write_bytes(b"synthetic closed archive transport")
    values["toolbox_sha256"] = release.sha256_file(bundle / "toolbox.tar")
    receipt = {key: values[key] for key in ("version", "commit", "image_cognita_cpu", "image_workspace_runtime",
                                          "test_runner_id", "toolbox_version", "toolbox_sha256")}
    receipt.update(schema=1, mode="full", result="passed", mandatory_ocr="passed", missing_file_parity="passed",
                   cleanup="verified", canonical_log="synthetic-canonical.log", http_log="synthetic-http.log", run_id="synthetic")
    values["qualification_cpu_full"] = json.dumps(receipt)
    (bundle / "compose.cpu.full.images.yaml").write_text(release.candidate_fragment_text({
        "cognita": values["image_ref_cognita_cpu"], "workspace-runtime": values["image_ref_workspace_runtime"]}), encoding="utf-8")
    fixture = Candidate(repo, origin, state, bundle, commit, values)
    fixture.publish_manifest()
    fixture.push()
    # Pure preflight also runs in the Linux packaged test runner; a real
    # PowerShell dispatch test below is conditional on that interpreter.
    monkeypatch.setattr(windows, "_is_windows", lambda: True)
    return fixture


def invoke(candidate):
    return windows.main(candidate.args, repo=candidate.repo, api=release)


def log_text(candidate):
    return "\n".join(path.read_text(encoding="utf-8") for path in (candidate.state / "logs").glob("*.log"))


def forbid_dispatch(monkeypatch):
    original = windows.owned_process
    def execute(arguments, **kwargs):
        assert arguments[0] == "git", "Rejected preflight must never dispatch a lifecycle mutation"
        return original(arguments, **kwargs)
    monkeypatch.setattr(windows, "owned_process", execute)


def test_parser_requires_only_bundle_and_expected_id():
    parser = argparse.ArgumentParser()
    windows.add_parser(parser.add_subparsers(dest="command", required=True))
    result = parser.parse_args(["deploy-windows", "--bundle", "somewhere", "--expected-installation-id", IDENTIFIER])
    assert vars(result) == {"command": "deploy-windows", "bundle": Path("somewhere"), "expected_installation_id": IDENTIFIER}
    with pytest.raises(SystemExit):
        parser.parse_args(["deploy-windows", "--bundle", "somewhere"])


@pytest.mark.parametrize("failure", ["missing", "core", "checksum", "unlisted", "missing-runtime", "static-change",
                                     "postgres", "fragment", "same-c", "bad-image", "toolbox", "foreign-checksum"])
def test_bundle_refusals_never_dispatch(candidate, monkeypatch, failure):
    forbid_dispatch(monkeypatch)
    if failure == "missing":
        args = candidate.args
        args.bundle = candidate.bundle / "absent"
        assert windows.main(args, repo=candidate.repo, api=release) == release.EXIT_CODES["usage"]
        return
    if failure == "core":
        candidate.values["bundle_mode"] = "core"
    elif failure == "checksum":
        (candidate.bundle / "cognita-cpu.tar").write_bytes(b"changed without new sums")
    elif failure == "unlisted":
        (candidate.bundle / "surprise.txt").write_text("not in checksum closure")
    elif failure == "missing-runtime":
        (candidate.bundle / "workspace-runtime.tar").unlink()
    elif failure == "static-change":
        (candidate.bundle / "scripts/windows/Update-Release.py").write_text("# unreviewed local input\n")
    elif failure == "postgres":
        candidate.values["image_ref_postgres"] = "different/pg:pg18@sha256:" + "a" * 64
    elif failure == "fragment":
        (candidate.bundle / "compose.cpu.full.images.yaml").write_text("services: {}\n")
    elif failure == "same-c":
        candidate.values["image_ref_workspace_runtime"] = release.candidate_workspace_reference("13.6.0", "f" * 40)
    elif failure == "bad-image":
        candidate.values["test_runner_id"] = "not-an-image"
        receipt = json.loads(candidate.values["qualification_cpu_full"])
        receipt["test_runner_id"] = candidate.values["test_runner_id"]
        candidate.values["qualification_cpu_full"] = json.dumps(receipt)
    elif failure == "toolbox":
        (candidate.bundle / "toolbox.tar").write_bytes(b"different qualified archive")
    elif failure == "foreign-checksum":
        with (candidate.bundle / "SHA256SUMS").open("a") as stream:
            stream.write("0" * 64 + "  C:foreign.txt\n")
        original_hash = release.sha256_file
        def only_owned_reads(path):
            assert path.is_relative_to(candidate.repo) or path.is_relative_to(candidate.bundle)
            return original_hash(path)
        monkeypatch.setattr(release, "sha256_file", only_owned_reads)
    if failure not in {"checksum", "unlisted", "foreign-checksum"}:
        candidate.publish_manifest()
    assert invoke(candidate) == release.EXIT_CODES["usage"]
    assert not (candidate.state / "dispatch.json").exists()


@pytest.mark.parametrize("modified", [False, True])
def test_bundle_static_inputs_use_committed_blobs_with_crlf_checkout(candidate, monkeypatch, modified):
    git_ok(candidate.repo, "config", "core.autocrlf", "true")
    relative = "scripts/windows/Update-Release.py"
    # Override machine-global attributes within this owned fixture so the
    # checkout conversion is deterministic on both Windows and Linux runners.
    (candidate.repo / ".git/info/attributes").write_text(relative + " text eol=crlf\n")
    source = candidate.repo / relative
    original = source.read_bytes()
    source.unlink()  # Force materialization rather than Git's clean-stat shortcut.
    git_ok(candidate.repo, "checkout-index", "--force", "--index", "--", relative)
    assert source.read_bytes() != (candidate.bundle / relative).read_bytes()
    assert git_ok(candidate.repo, "status", "--porcelain") == ""
    log = SimpleNamespace(line=lambda message: None)
    windows.verify_source(candidate.repo, candidate.commit, release, log)
    assert windows.validate_bundle(candidate.bundle, candidate.repo, release, log)["commit"] == candidate.commit
    if modified:
        # Recomputing transport checksums must not bless changed source, even
        # if the same change is made in both checkout and bundle.
        source.write_bytes(original + b"# unreviewed edit\r\n")
        (candidate.bundle / relative).write_bytes(original + b"# unreviewed edit\n")
        candidate.close_bundle()
        with pytest.raises(release.ReleaseError, match="uncommitted"):
            windows.verify_source(candidate.repo, candidate.commit, release, log)
        with pytest.raises(release.ReleaseError, match="reviewed Git tree"):
            windows.validate_bundle(candidate.bundle, candidate.repo, release, log)


@pytest.mark.parametrize("field", [None, "schema", "mode", "commit", "image_cognita_cpu", "image_workspace_runtime", "test_runner_id",
                                    "toolbox_sha256", "mandatory_ocr", "missing_file_parity", "result", "cleanup", "canonical_log"])
def test_missing_failed_or_mismatched_qualification_is_refused(candidate, monkeypatch, field):
    forbid_dispatch(monkeypatch)
    if field is None:
        candidate.values.pop("qualification_cpu_full")
    else:
        receipt = json.loads(candidate.values["qualification_cpu_full"])
        receipt[field] = "" if field == "canonical_log" else "failed-or-mismatched"
        candidate.values["qualification_cpu_full"] = json.dumps(receipt)
    candidate.publish_manifest()
    assert invoke(candidate) == release.EXIT_CODES["usage"]
    assert not (candidate.state / "dispatch.json").exists()


@pytest.mark.parametrize("failure", ["untracked", "tracked", "unpushed", "different-tree", "unrelated-c", "remote-unavailable"])
def test_source_refusal_against_real_local_git_remote(candidate, monkeypatch, failure):
    forbid_dispatch(monkeypatch)
    if failure == "untracked":
        (candidate.repo / "untracked.txt").write_text("uncommitted")
    elif failure == "tracked":
        (candidate.repo / "compose.cpu.yaml").write_text("# dirty\nservices: {}\n")
    elif failure == "unpushed":
        git_ok(candidate.repo, "commit", "--quiet", "--allow-empty", "-m", "not pushed")
    elif failure == "different-tree":
        (candidate.repo / "new-tree.txt").write_text("changed source tree")
        git_ok(candidate.repo, "add", ".")
        git_ok(candidate.repo, "commit", "--quiet", "-m", "changed tree")
        candidate.push()
    elif failure == "unrelated-c":
        candidate.values["commit"] = "f" * 40
        candidate.values["image_ref_cognita_cpu"] = release.candidate_app_reference("cpu", "13.6.0", "f" * 40)
        candidate.values["image_ref_workspace_runtime"] = release.candidate_workspace_reference("13.6.0", "f" * 40)
        candidate.values["test_runner_ref"] = release.candidate_test_reference("13.6.0", "f" * 40)
        receipt = json.loads(candidate.values["qualification_cpu_full"])
        receipt["commit"] = "f" * 40
        candidate.values["qualification_cpu_full"] = json.dumps(receipt)
        (candidate.bundle / "compose.cpu.full.images.yaml").write_text(release.candidate_fragment_text({
            "cognita": candidate.values["image_ref_cognita_cpu"], "workspace-runtime": candidate.values["image_ref_workspace_runtime"]}))
        candidate.publish_manifest()
    else:
        git_ok(candidate.repo, "remote", "set-url", "origin", str(candidate.origin / "missing"))
    assert invoke(candidate) == release.EXIT_CODES["dirty-checkout"]
    assert not (candidate.state / "dispatch.json").exists()


@pytest.mark.parametrize("case", ["wrong-id", "invalid-id", "missing-record", "ambiguous-root", "missing-shell", "wrong-host"])
def test_local_preconditions_fail_without_mutation(candidate, monkeypatch, case):
    forbid_dispatch(monkeypatch)
    args = candidate.args
    if case == "wrong-id":
        args.expected_installation_id = "22222222-2222-4333-8444-555555555555"
    elif case == "invalid-id":
        args.expected_installation_id = "not a uuid; pretend-secret"
    elif case == "missing-record":
        (candidate.state / "install.json").unlink()
    elif case == "ambiguous-root":
        with (candidate.repo / "scripts/windows/Install-CognitaWindows.ps1").open("a") as stream:
            stream.write(f"$script:Root = '{candidate.state}'\n")
    elif case == "missing-shell":
        monkeypatch.setattr(windows.shutil, "which", lambda _name: None)
    else:
        monkeypatch.setattr(windows, "_is_windows", lambda: False)
    assert windows.main(args, repo=candidate.repo, api=release) == release.EXIT_CODES["usage"]
    assert not (candidate.state / "dispatch.json").exists()


@pytest.mark.parametrize("refused", [False, True])
def test_real_powershell_dispatch_uses_reviewed_script_and_reports_failure(candidate, monkeypatch, capsys, refused):
    executable = shutil.which("pwsh.exe") or shutil.which("pwsh")
    if not executable:
        pytest.skip("PowerShell is unavailable in this test runner; real dispatch runs on Windows")
    if refused:
        (candidate.state / "refuse").touch()
    promotion = git_ok(candidate.repo, "rev-parse", "HEAD")
    assert invoke(candidate) == (release.EXIT_CODES["apply-failed"] if refused else 0)
    observed = json.loads((candidate.state / "dispatch.json").read_text(encoding="utf-8-sig"))
    assert observed == {"action": "UpdateRelease", "bundle": str(candidate.bundle), "installation_id": IDENTIFIER,
                        "cwd": str(candidate.repo), "script": str(candidate.repo / "scripts/windows/Install-CognitaWindows.ps1")}
    text = log_text(candidate)
    assert candidate.commit in text and promotion in text
    assert "synthetic-secret-must-not-be-logged" not in text + capsys.readouterr().out
    assert "update: apply-toolbox failed" in text
    assert "update: preflight-source-state failed" in text
    assert "deployed: version=13.6.0" in text if not refused else "deployed:" not in text
    assert not (candidate.repo / "data/cognita/releases").exists()


def test_empty_pushed_review_checkpoint_preserves_candidate_source(candidate):
    git_ok(candidate.repo, "commit", "--quiet", "--allow-empty", "-m", "Code review: synthetic checkpoint")
    candidate.push()
    promotion = git_ok(candidate.repo, "rev-parse", "HEAD")
    assert promotion != candidate.commit
    log = release.Log(None)
    assert windows.verify_source(candidate.repo, candidate.commit, release, log) == promotion


def test_canonical_cli_dispatches_before_linux_logging(candidate, monkeypatch):
    executable = shutil.which("pwsh.exe") or shutil.which("pwsh")
    if not executable:
        pytest.skip("PowerShell is unavailable in this test runner; real dispatch runs on Windows")
    monkeypatch.setattr(release, "REPO_ROOT", candidate.repo)
    def linux_logging_is_forbidden(*_args):
        pytest.fail("Windows dispatch reached Linux release logging")
    monkeypatch.setattr(release, "logs_dir", linux_logging_is_forbidden)
    assert release.main(["deploy-windows", "--bundle", str(candidate.bundle), "--expected-installation-id", IDENTIFIER]) == 0
    assert (candidate.state / "dispatch.json").is_file()
    assert "deployed: version=13.6.0" in log_text(candidate)


def test_direct_script_execution_imports_and_refuses_invalid_id(owned_root):
    script = Path(release.__file__)
    result = windows.owned_process([sys.executable, str(script), "deploy-windows", "--bundle", str(owned_root),
                                   "--expected-installation-id", "invalid"], cwd=owned_root, timeout=15)
    assert result.returncode == release.EXIT_CODES["usage"]
    assert "deploy-windows: FAILED [usage]" in result.stdout
    assert not (owned_root / "logs").exists()


def test_existing_build_test_branch_replaces_profile_and_cleans_temp(candidate, monkeypatch):
    """Exercise the real non-no-build branch that uses dataclasses.replace."""
    monkeypatch.setattr(release, "REPO_ROOT", candidate.repo)
    monkeypatch.setattr(release, "require_clean_checkout", lambda *_args: candidate.commit)
    monkeypatch.setattr(release, "export_version", lambda *_args: None)
    monkeypatch.setattr(release, "doctor", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(release, "target_lock", lambda *_args: contextlib.nullcontext())
    observed = {}
    def build(repo, selected_target, version, commit, temporary, log, **kwargs):
        assert selected_target.profile == "cpu" and kwargs["mode"] == "core"
        assert commit == candidate.commit
        observed["temporary"] = temporary
        return {"cognita": "synthetic:cpu"}, {"cognita": "sha256:" + "b" * 64}
    def test_stack(repo, selected_target, version, commit, images, log, **kwargs):
        assert images == {"cognita": "synthetic:cpu"}
        assert kwargs["profile"] == "cpu" and kwargs["mode"] == "core"
        observed["tested"] = True
    def retire(*args, **kwargs):
        assert kwargs["built_ids"] == {"cognita": "sha256:" + "b" * 64}
        assert kwargs["test_started"] is True
        observed["retired"] = True
    monkeypatch.setattr(release, "build_images", build)
    monkeypatch.setattr(release, "run_test_stack", test_stack)
    monkeypatch.setattr(release, "retire_build_references", retire)
    target = release.TARGETS["test"]
    args = SimpleNamespace(profile="cpu", mode="core", no_build=False, images=None)
    release.cmd_test(args, target, release.Log(None))
    assert target.profile == "amd"
    assert observed["tested"] and observed["retired"] and not observed["temporary"].exists()


def test_git_timeout_is_bounded_and_not_logged(candidate, monkeypatch, capsys):
    def stalled(*args, **kwargs):
        assert kwargs["timeout"] == windows.GIT_TIMEOUT
        raise subprocess.TimeoutExpired(["git", "secret-provider-argument"], kwargs["timeout"])
    monkeypatch.setattr(windows, "owned_process", stalled)
    assert invoke(candidate) == release.EXIT_CODES["dirty-checkout"]
    assert "secret-provider-argument" not in log_text(candidate) + capsys.readouterr().out


@pytest.mark.parametrize("failure", ["timeout", "launch", "interrupt"])
def test_lifecycle_failure_never_claims_success(candidate, monkeypatch, failure):
    original = windows.owned_process
    monkeypatch.setattr(windows.shutil, "which", lambda _name: "controlled-pwsh")
    def execute(arguments, **kwargs):
        if arguments[0] == "git":
            return original(arguments, **kwargs)
        assert kwargs["capture"] is True and kwargs["diagnostics"] is True and kwargs["timeout"] == windows.UPDATE_TIMEOUT
        if failure == "timeout":
            raise subprocess.TimeoutExpired(arguments, kwargs["timeout"])
        if failure == "launch":
            raise OSError("synthetic-private-error")
        raise KeyboardInterrupt()
    monkeypatch.setattr(windows, "owned_process", execute)
    assert invoke(candidate) == release.EXIT_CODES["apply-failed"]
    assert "deployed:" not in log_text(candidate)
    assert "synthetic-private-error" not in log_text(candidate)


def test_owned_child_timeout_reaps_process(owned_root):
    marker = owned_root / "pid.txt"
    command = [sys.executable, "-c", "import os, pathlib, time; pathlib.Path(__import__('sys').argv[1]).write_text(str(os.getpid())); time.sleep(60)", str(marker)]
    with pytest.raises(subprocess.TimeoutExpired):
        windows.owned_process(command, cwd=owned_root, timeout=1)
    pid = int(marker.read_text())
    if os.name == "nt":
        assert_windows_process_exited(pid)
    else:
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)


def assert_windows_process_exited(pid):
    # Process objects can remain queryable after exit (a traceback or another
    # OS observer may retain a handle). A signaled process handle establishes
    # exit; OpenProcess succeeding alone does not mean it is still running.
    kernel = windows.ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [windows.ctypes.c_ulong, windows.ctypes.c_int, windows.ctypes.c_ulong]
    kernel.OpenProcess.restype = windows.ctypes.c_void_p
    kernel.WaitForSingleObject.argtypes = [windows.ctypes.c_void_p, windows.ctypes.c_ulong]
    kernel.CloseHandle.argtypes = [windows.ctypes.c_void_p]
    handle = kernel.OpenProcess(0x100000, False, pid)
    if handle:
        try:
            # Wait on the process handle itself: termination is asynchronous, so
            # a 0 ms poll straight after the kill lost the race on a loaded box.
            # A killed process always signals; 5 s is only a hang guard.
            assert kernel.WaitForSingleObject(handle, 5000) == 0, f"Owned process {pid} is still running"
        finally:
            kernel.CloseHandle(handle)


@pytest.mark.skipif(os.name != "nt", reason="Windows job-object descendant cleanup proof")
@pytest.mark.parametrize("parent_exits", [False, True])
def test_windows_job_reaps_descendant_even_after_parent_success(owned_root, parent_exits):
    marker = owned_root / "descendant.txt"
    command = [sys.executable, "-c", (
        "import subprocess, sys, time, pathlib; "
        "child=subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL); "
        "pathlib.Path(sys.argv[1]).write_text(str(child.pid)); "
        + ("sys.exit(0)" if parent_exits else "time.sleep(60)")
    ), str(marker)]
    if parent_exits:
        assert windows.owned_process(command, cwd=owned_root, timeout=5).returncode == 0
    else:
        with pytest.raises(subprocess.TimeoutExpired):
            windows.owned_process(command, cwd=owned_root, timeout=1)
    pid = int(marker.read_text())
    assert_windows_process_exited(pid)
