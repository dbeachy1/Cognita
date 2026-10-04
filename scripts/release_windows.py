"""Windows-local dispatch for the canonical release.py deployment command.

The caller supplies release.py's authoritative parsers. This module does not
import that script, open a Docker engine, or implement installation lifecycle.
The reviewed PowerShell UpdateRelease action owns those runtime boundaries.
"""
from __future__ import annotations

import argparse
import ctypes
import datetime as dt
import json
import os
from pathlib import Path
import re
import shutil
import signal
import stat
import subprocess
import time
import uuid


GIT_TIMEOUT = 120
# Includes the lifecycle's bounded apply and restoration budgets. Its own
# narrower deadlines normally finish first, allowing restoration to complete.
UPDATE_TIMEOUT = 8 * 60 * 60


def add_parser(subparsers) -> None:
    command = subparsers.add_parser("deploy-windows", help="apply a qualified Full bundle to the owned Windows installation")
    command.add_argument("--bundle", type=Path, required=True)
    command.add_argument("--expected-installation-id", required=True)


class _WindowsJob:
    """Assign a suspended child before it can create unowned descendants."""

    def __init__(self):
        from ctypes import wintypes as w

        class BasicLimits(ctypes.Structure):
            _fields_ = [("process_time", ctypes.c_int64), ("job_time", ctypes.c_int64),
                        ("flags", w.DWORD), ("minimum", ctypes.c_size_t),
                        ("maximum", ctypes.c_size_t), ("active", w.DWORD),
                        ("affinity", ctypes.c_size_t), ("priority", w.DWORD), ("scheduling", w.DWORD)]

        class ExtendedLimits(ctypes.Structure):
            _fields_ = [("basic", BasicLimits), ("io", ctypes.c_uint64 * 6),
                        ("process_memory", ctypes.c_size_t), ("job_memory", ctypes.c_size_t),
                        ("peak_process", ctypes.c_size_t), ("peak_job", ctypes.c_size_t)]

        class Accounting(ctypes.Structure):
            _fields_ = [("times", ctypes.c_int64 * 4), ("faults", w.DWORD),
                        ("total", w.DWORD), ("active", w.DWORD), ("terminated", w.DWORD)]

        class ThreadEntry(ctypes.Structure):
            _fields_ = [("size", w.DWORD), ("usage", w.DWORD), ("thread", w.DWORD),
                        ("owner", w.DWORD), ("priority", w.LONG), ("delta", w.LONG), ("flags", w.DWORD)]

        self.accounting_type, self.thread_type = Accounting, ThreadEntry
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        signatures = {
            "CreateJobObjectW": ([ctypes.c_void_p, w.LPCWSTR], w.HANDLE),
            "SetInformationJobObject": ([w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD], w.BOOL),
            "AssignProcessToJobObject": ([w.HANDLE, w.HANDLE], w.BOOL),
            "QueryInformationJobObject": ([w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD, ctypes.c_void_p], w.BOOL),
            "TerminateJobObject": ([w.HANDLE, w.UINT], w.BOOL),
            "CreateToolhelp32Snapshot": ([w.DWORD, w.DWORD], w.HANDLE),
            "Thread32First": ([w.HANDLE, ctypes.POINTER(ThreadEntry)], w.BOOL),
            "Thread32Next": ([w.HANDLE, ctypes.POINTER(ThreadEntry)], w.BOOL),
            "OpenThread": ([w.DWORD, w.BOOL, w.DWORD], w.HANDLE),
            "ResumeThread": ([w.HANDLE], w.DWORD),
            "CloseHandle": ([w.HANDLE], w.BOOL),
        }
        for name, (arguments, result) in signatures.items():
            function = getattr(self.kernel, name)
            function.argtypes, function.restype = arguments, result
        self.handle = self.kernel.CreateJobObjectW(None, None)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        limits = ExtendedLimits()
        limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE.
        if not self.kernel.SetInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            self.close()
            raise OSError("Could not establish owned process cleanup")

    def start(self, process) -> None:
        if not self.kernel.AssignProcessToJobObject(self.handle, int(process._handle)):
            raise OSError("Could not assign suspended child to its cleanup job")
        # Popen closes the primary thread handle. The suspended process has
        # exactly one thread, so obtain that owned thread with the documented
        # Toolhelp API and resume it only after assignment to the job.
        snapshot = self.kernel.CreateToolhelp32Snapshot(4, 0)
        if snapshot in (None, ctypes.c_void_p(-1).value):
            raise OSError("Could not inspect suspended child thread")
        try:
            entry = self.thread_type()
            entry.size = ctypes.sizeof(entry)
            available = self.kernel.Thread32First(snapshot, ctypes.byref(entry))
            while available:
                if entry.owner == process.pid:
                    thread = self.kernel.OpenThread(2, False, entry.thread)
                    if not thread:
                        raise OSError("Could not open suspended child thread")
                    try:
                        if self.kernel.ResumeThread(thread) != 1:
                            raise OSError("Could not resume owned child")
                        return
                    finally:
                        self.kernel.CloseHandle(thread)
                available = self.kernel.Thread32Next(snapshot, ctypes.byref(entry))
            raise OSError("Suspended child thread disappeared")
        finally:
            self.kernel.CloseHandle(snapshot)

    def reap(self) -> None:
        if not self.kernel.TerminateJobObject(self.handle, 1):
            raise OSError("Could not terminate owned process job")
        deadline = time.monotonic() + 10
        while True:
            accounting = self.accounting_type()
            if not self.kernel.QueryInformationJobObject(self.handle, 1, ctypes.byref(accounting), ctypes.sizeof(accounting), None):
                raise OSError("Could not verify owned descendant cleanup")
            if accounting.active == 0:
                return
            if time.monotonic() >= deadline:
                raise OSError("Owned process descendants did not exit")
            time.sleep(0.02)

    def close(self) -> None:
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


def owned_process(argv, *, cwd: Path, timeout: float, capture: bool = True, diagnostics: bool = False,
                  capture_stderr: bool = False):
    """Run without a shell or prompts; reap the complete owned process tree.

    Git output is returned to validators, never printed. Lifecycle output is
    captured only for filtering code-owned phase records; arbitrary child output
    and credential-provider errors never enter release logs. Jobs catch children
    outlive a successful parent; ending a command is not proof of cleanup.
    ``capture_stderr`` returns stderr separately (never merged into stdout); only
    `_git` asks for it, for local commands, so a failure can say why.
    """
    environment = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "Never"}
    windows = os.name == "nt"
    job = _WindowsJob() if windows else None
    process = None
    try:
        process = subprocess.Popen(
            argv, cwd=cwd, env=environment, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
            stderr=(subprocess.STDOUT if diagnostics else subprocess.PIPE if capture_stderr else subprocess.DEVNULL),
            text=True, encoding="utf-8", errors="replace",
            creationflags=(0x4 | subprocess.CREATE_NO_WINDOW) if windows else 0,
            start_new_session=not windows,
        )
        if job:
            job.start(process)
        output, errors = process.communicate(timeout=timeout)
        return subprocess.CompletedProcess(argv, process.returncode, output or "", errors or "")
    finally:
        try:
            if process is not None:
                try:
                    if job:
                        job.reap()
                    else:
                        try:
                            os.killpg(process.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                finally:
                    # Also covers assignment/inspection failure while the
                    # process is still suspended outside a successfully owned job.
                    if process.poll() is None:
                        process.kill()
                    try:
                        process.wait(timeout=10)
                    finally:
                        if process.stdout is not None:
                            process.stdout.close()
                        if process.stderr is not None:
                            process.stderr.close()
                        if windows:
                            process._handle.Close()
        finally:
            if job:
                job.close()


def _is_windows() -> bool:
    return os.name == "nt"


def _regular(path: Path) -> bool:
    info = path.lstat()
    return stat.S_ISREG(info.st_mode) and not getattr(info, "st_file_attributes", 0) & 0x400


def installation_paths(installer: Path, api) -> tuple[Path, Path]:
    """Consume the installer's existing path declaration, without executing it.

    This narrowly supports its current literal/Join-Path declarations. A future
    change to discovery must provide an explicit interface rather than guess a
    second installation root or bootstrap missing state.
    """
    source = installer.read_text(encoding="utf-8-sig")
    roots = re.findall(r"(?m)^\$script:Root\s*=\s*'([^'\r\n]+)'\s*$", source)
    names = re.findall(r"(?m)^\$script:RecordPath\s*=\s*Join-Path\s+\$script:Root\s+'([^'\r\n]+)'\s*$", source)
    if len(roots) != 1 or len(names) != 1 or Path(names[0]).name != names[0]:
        raise api.ReleaseError("usage", "Installer installation-path authority is missing or ambiguous")
    root = Path(roots[0])
    if not root.is_absolute() or not root.is_dir() or root.is_symlink() or getattr(root.lstat(), "st_file_attributes", 0) & 0x400:
        raise api.ReleaseError("usage", "Existing Windows installation state root is missing or unsafe")
    return root, root / names[0]


# Git subcommands that may reach the remote and its credential helper: their stderr can carry
# credential-provider text, which never enters a release log (owned_process). Only their exit code is logged.
_GIT_REMOTE_COMMANDS = frozenset({"ls-remote", "fetch", "pull", "push"})


def _git(repo: Path, api, *arguments: str, check=True, log=None):
    # 2026-09-30: test_bundle_refusals_never_dispatch[fragment] once failed here with the generic
    # "refused or could not verify" and nothing said which git command failed or why. The command, its
    # exit code and (for local commands) a stderr tail now go to the log, and the refusal names the command.
    command = " ".join(arguments)
    local = not arguments or arguments[0] not in _GIT_REMOTE_COMMANDS
    try:
        result = owned_process(["git", "-C", str(repo), *arguments], cwd=repo, timeout=GIT_TIMEOUT,
                               capture_stderr=local)
    except (OSError, subprocess.TimeoutExpired) as exc:
        if log is not None:
            log.line(f"source: git {command} did not finish ({type(exc).__name__}); child cleanup was attempted")
        raise api.ReleaseError("dirty-checkout", "Bounded Git source verification failed; child cleanup was attempted") from exc
    if result.returncode and log is not None:
        tail = " ".join(result.stderr.split())[-300:] if local else "(not logged: remote command)"
        log.line(f"source: git {command} exited {result.returncode}; stderr: {tail or '(empty)'}")
    if check and result.returncode:
        raise api.ReleaseError("dirty-checkout", "Git source verification refused or could not verify the configured "
                               f"origin (git {arguments[0] if arguments else ''} exited {result.returncode})")
    return result


def verify_source(repo: Path, candidate: str, api, log) -> str:
    if not re.fullmatch(r"[0-9a-f]{40}", candidate):
        raise api.ReleaseError("usage", "Candidate source commit is invalid")
    if _git(repo, api, "status", "--porcelain", log=log).stdout.strip():
        raise api.ReleaseError("dirty-checkout", "Deployment checkout has uncommitted changes")
    promotion = _git(repo, api, "rev-parse", "HEAD", log=log).stdout.strip()
    if not re.fullmatch(r"[0-9a-f]{40}", promotion):
        raise api.ReleaseError("dirty-checkout", "Deployment HEAD is invalid")
    if len(_git(repo, api, "config", "--get-all", "remote.origin.url", log=log).stdout.splitlines()) != 1:
        raise api.ReleaseError("dirty-checkout", "Configured origin is missing or ambiguous")
    remote = _git(repo, api, "ls-remote", "--exit-code", "origin", "refs/heads/main", log=log).stdout.splitlines()
    if remote != [f"{promotion}\trefs/heads/main"]:
        raise api.ReleaseError("dirty-checkout", "Deployment HEAD is not the verified pushed origin/main commit")
    if _git(repo, api, "merge-base", "--is-ancestor", candidate, promotion, check=False, log=log).returncode:
        raise api.ReleaseError("dirty-checkout", "Candidate source is not reachable from pushed main")
    trees = _git(repo, api, "rev-parse", f"{candidate}^{{tree}}", f"{promotion}^{{tree}}", log=log).stdout.splitlines()
    if len(trees) != 2 or trees[0] != trees[1] or not re.fullmatch(r"[0-9a-f]{40}", trees[0]):
        raise api.ReleaseError("dirty-checkout", "Candidate and pushed main source trees differ")
    log.line(f"source: candidate C={candidate}; pushed main M={promotion}; equal trees")
    return promotion


def validate_bundle(bundle: Path, repo: Path, api, log) -> dict:
    # Reject junctions and links before the authoritative checksum walk can
    # encounter another tree. This visits only the supplied closed bundle.
    pending, files = [bundle], []
    while pending:
        directory = pending.pop()
        if directory.is_symlink() or getattr(directory.lstat(), "st_file_attributes", 0) & 0x400:
            raise api.ReleaseError("usage", "Bundle contains a link or reparse point")
        for item in directory.iterdir():
            if item.is_symlink() or getattr(item.lstat(), "st_file_attributes", 0) & 0x400:
                raise api.ReleaseError("usage", "Bundle contains a link or reparse point")
            if item.is_dir():
                pending.append(item)
            elif _regular(item):
                files.append(item)
            else:
                raise api.ReleaseError("usage", "Bundle contains unsupported filesystem content")
    # Constrain the validator's reads to files already observed inside this
    # bundle. In particular, a Windows drive-relative checksum name must not
    # cause a read from another drive before checksum closure rejects it.
    owned_names = {item.relative_to(bundle).as_posix() for item in files if item.name != "SHA256SUMS"}
    for line in (bundle / "SHA256SUMS").read_text(encoding="utf-8").splitlines():
        if line.partition("  ")[2] not in owned_names:
            raise api.ReleaseError("usage", "Bundle checksum names a file outside its closed inputs")
    api.verify_bundle_checksums(bundle)
    values = api.candidate_manifest(bundle)
    if values.get("bundle_mode") != "full":
        raise api.ReleaseError("usage", "Windows maintenance deployment requires a Full bundle")
    api.cpu_full_qualification(values)
    version, commit = values["version"], values["commit"]
    if version != api.read_version(repo, log) or values.get("toolbox_version") != api.read_toolbox_version(repo, log):
        raise api.ReleaseError("usage", "Bundle versions differ from the reviewed source authorities")
    expected = {
        "image_ref_cognita_cpu": api.candidate_app_reference("cpu", version, commit),
        "image_ref_workspace_runtime": api.candidate_workspace_reference(version, commit),
        "test_runner_ref": api.candidate_test_reference(version, commit),
        "image_ref_postgres": api.postgres_service_reference(repo),
    }
    if any(values.get(key) != value for key, value in expected.items()):
        raise api.ReleaseError("usage", "Bundle image references do not select this same-C candidate")
    if any(not re.fullmatch(r"sha256:[0-9a-f]{64}", values.get(key, "")) for key in
           ("image_cognita_cpu", "image_workspace_runtime", "test_runner_id")):
        raise api.ReleaseError("usage", "Bundle has an invalid qualified image identity")
    refs = {"cognita": expected["image_ref_cognita_cpu"], "workspace-runtime": expected["image_ref_workspace_runtime"]}
    fragment = bundle / "compose.cpu.full.images.yaml"
    if fragment.read_text(encoding="utf-8") != api.candidate_fragment_text(refs):
        raise api.ReleaseError("usage", "Bundle Compose fragment differs from the qualified image pair")
    required = ("cognita-cpu.tar", "workspace-runtime.tar", "toolbox.tar",
                "compose.yaml", "compose.cpu.yaml", "compose.workspace.yaml",
                "scripts/windows/Install-CognitaWindows.ps1", "scripts/windows/Update-Release.py")
    if any(not (bundle / relative).is_file() for relative in required):
        raise api.ReleaseError("usage", "Closed Full bundle is missing a required deployment input")
    if api.sha256_file(bundle / "toolbox.tar") != values.get("toolbox_sha256"):
        raise api.ReleaseError("usage", "Toolbox archive differs from canonical qualification")
    # Static bundle scripts and definitions must come from the reviewed equal
    # tree; checksum closure alone can authenticate a locally modified script.
    for item in files:
        relative = item.relative_to(bundle)
        if relative.parts[0] in {"scripts", "docs"} or relative.as_posix() in {
            "compose.yaml", "compose.cpu.yaml", "compose.workspace.yaml"
        }:
            source = repo / relative
            if not source.is_file() or not _regular(source):
                raise api.ReleaseError("usage", "Bundle static input is missing from the reviewed checkout")
            # The source authority is the committed tree, not platform checkout
            # bytes: Git can materialize CRLF on Windows from an LF blob built
            # on Linux. Hash the unchanged bundle bytes as a Git blob, without
            # filters, and compare to C. Checksum closure still verifies their
            # exact transport bytes; verify_source separately rejects edits.
            expected_blob = _git(repo, api, "rev-parse", f"{commit}:{relative.as_posix()}", log=log).stdout.strip()
            actual_blob = _git(repo, api, "hash-object", "--no-filters", "--", str(item), log=log).stdout.strip()
            if actual_blob != expected_blob:
                raise api.ReleaseError("usage", "Bundle static input differs from the reviewed Git tree")
    log.line("bundle: checksum closure and canonical CPU/full qualification, mandatory OCR and cleanup verified")
    return values


def main(args: argparse.Namespace, *, repo: Path, api) -> int:
    """Own Windows logging/verdict; called before release.py's Linux setup."""
    log = None
    try:
        if not _is_windows():
            raise api.ReleaseError("usage", "deploy-windows requires Windows Python 3.11+ on the installation host")
        try:
            identifier = str(uuid.UUID(args.expected_installation_id))
        except (ValueError, AttributeError) as exc:
            raise api.ReleaseError("usage", "Expected installation ID must be a canonical UUID") from exc
        if identifier != args.expected_installation_id:
            raise api.ReleaseError("usage", "Expected installation ID must be a canonical UUID")
        installer = repo / "scripts/windows/Install-CognitaWindows.ps1"
        root, record_path = installation_paths(installer, api)
        if not _regular(record_path):
            raise api.ReleaseError("usage", "Existing installation owner record is unsafe")
        record = json.loads(record_path.read_text(encoding="utf-8-sig"))
        if not isinstance(record, dict) or record.get("installation_id") != identifier:
            raise api.ReleaseError("usage", "Expected installation ID differs from the existing installation")
        logs = root / "logs"
        if logs.exists() and (not logs.is_dir() or logs.is_symlink() or getattr(logs.lstat(), "st_file_attributes", 0) & 0x400):
            raise api.ReleaseError("usage", "Existing Windows log directory is unsafe")
        stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        log = api.Log(logs / f"deploy-windows-{stamp}-{uuid.uuid4().hex[:8]}.log")
        log.line("release.py deploy-windows: validating the existing Windows installation and candidate")
        bundle = args.bundle.absolute()
        # Establish checkout authority before comparing static bundle inputs
        # against it. The candidate SHA is strictly validated before Git use;
        # no bundle content is executed before its checksum/qualification gate.
        metadata = api.candidate_manifest(bundle)
        verify_source(repo, metadata["commit"], api, log)
        values = validate_bundle(bundle, repo, api, log)
        executable = shutil.which("pwsh.exe") or shutil.which("pwsh")
        if not executable:
            raise api.ReleaseError("usage", "PowerShell 7 (pwsh.exe) is required for Windows deployment")
        # Full ownership/runtime/recognizable-partial-update validation happens
        # under the existing installer mutex, before lifecycle mutation.
        log.line("dispatch: same reviewed checkout PowerShell UpdateRelease; runtime verification follows")
        try:
            result = owned_process([executable, "-NoProfile", "-NonInteractive", "-File", str(installer),
                                    "-Action", "UpdateRelease", "-BundlePath", str(bundle),
                                    "-ExpectedInstallationId", identifier],
                                   cwd=repo, timeout=UPDATE_TIMEOUT, capture=True, diagnostics=True)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise api.ReleaseError("apply-failed", "Bounded Windows update failed; inspect installation Status before retrying") from exc
        # PowerShell may add formatting to errors. Only complete, fixed phase
        # records are publishable; raw stdout/stderr remains private.
        for match in re.finditer(r"(?m)^COGNITA_UPDATE_PHASE ((?:(?:apply|rollback|repair|current)-(?:load-images|toolbox|start-pair|verify)|preflight-(?:owner|prior-bundle|candidate-bundle|source-state|launcher|paths|begin))) (started|passed|failed)\r?$", result.stdout):
            log.line("update: " + match.group(1) + " " + match.group(2))
        if result.returncode:
            raise api.ReleaseError("apply-failed", f"PowerShell UpdateRelease refused or failed (exit {result.returncode}); inspect installation Status")
        log.line(f"deployed: version={values['version']} source C={values['commit']}; packaged runtime verification passed; plugin acceptance remains separate")
        log.line(f"deploy-windows: ok; log is {log.path}")
        return 0
    except api.ReleaseError as exc:
        message = f"deploy-windows: FAILED [{exc.state}] {exc}"
        if log:
            log.line(message)
            log.line(f"deploy-windows: log is {log.path}")
        else:
            print(message, flush=True)
        return api.EXIT_CODES.get(exc.state, 1)
    except (OSError, ValueError, TypeError) as exc:
        # Never render raw JSON, credential-provider errors, or child output.
        message = f"deploy-windows: FAILED [usage] Required local input is missing or invalid ({type(exc).__name__})"
        log.line(message) if log else print(message, flush=True)
        return api.EXIT_CODES["usage"]
    except KeyboardInterrupt:
        message = "deploy-windows: interrupted; owned child cleanup completed; inspect installation Status"
        log.line(message) if log else print(message, flush=True)
        return api.EXIT_CODES["apply-failed"]
    finally:
        if log:
            log.close()
