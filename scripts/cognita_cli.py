#!/usr/bin/env python3
"""The user-facing Cognita command for Linux: ./cognita install | status | update | ...

docs/DESIGN-LINUX-INSTALLER.md is the design this file implements; section numbers in the
comments below refer to it.  Proof that every step works by hand:
docs/PROOF-LINUX-INSTALL.md and docs/proof-linux-install/.

This is the FRONT of scripts/release.py, not a second release system (L10).  It asks the
few questions only the user can answer, checks the machine, writes the one persistent state
file (the env file, section 3), and then CALLS release.py's lock-free functions to stage,
apply and prove the release.  It never reimplements them.  Every call into release.py is in
the "release.py adapter" section so the two files can be aligned in one place.

Standard library only, Python 3.11+, runs on the Linux host (release.py is imported the same
way scripts/reset_disposable_state.py imports it).  Everything that touches the machine goes
through an injected seam (Runner, Host, UI, AdminClient, http, sleep) so the tests never run
Docker, systemd, a network call, a subprocess or a wait.

Hard rules (design section 0, C19, C21):
  * A password never appears in argv, an environment variable, a log line or a file, except the
    file the user names with --admin-password-file, which is read and left alone, or the one line of
    this program's own standard input that --admin-password-stdin reads (design 19.9 item 9).  The only
    place it travels onward is stdin (JSON to set-admin-credentials.py) and Admin's login request body.
  * Generated secrets are written 0600 and never printed.
  * Every sudo command is printed with its one-line reason BEFORE it runs, and only after the
    user said yes (or passed the matching flag).
  * Every step, decision, skip and swallowed exception is logged with its values (never a secret).
"""
from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import getpass
import http.cookiejar
import json
import os
import posixpath
import re
import secrets
import shlex
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, MutableMapping

sys.path.insert(0, str(Path(__file__).resolve().parent))

import release  # noqa: E402  (the sys.path line above has to run before this import)

REPO_ROOT = Path(__file__).resolve().parents[1]

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_RELOGIN = 10  # "stop here, log out and back in, rerun": not a failure, but not done either

UNIT = "cognita.service"
PROJECT = "cognita"
TARGET_NAME = "local"
# The env file (section 3).  Tests and the release tool override the location the same way.
ENV_OVERRIDE = "COGNITA_LOCAL_ENV_FILE"
DEFAULT_DATA_DIR = "~/.local/share/cognita"
MAX_DOCUMENT_ROOTS = 9

# Compressed download sizes that are not in containers/published-release.txt.  The PostgreSQL
# image is pinned by digest in compose.yaml and is not ours to publish (section 6.1); 0.16 GB is
# the number measured in the hand proof (PROOF-LINUX-INSTALL.md, "Numbers for L11 / C5").
POSTGRES_IMAGE_BYTES = 160_000_000
# The search models the app downloads on first use.  This mirrors the sum of
# cognita.prefetch_models.EXPECTED_BYTES, which lives in the app image and cannot be imported
# on the host; keep the two in step (section 6.3).  14.0 made the default reranker
# BAAI/bge-reranker-v2-m3 (2.27 GB by its pinned spec), so it is about 3.6 GB now, not 2.3.
EXPECTED_MODEL_BYTES = 3_610_000_000
OCR_WEIGHTS_BYTES = 100_000_000
# Section 6.3: compressed images x4 on disk (measured 3-4x), the Workspace import into
# Microsandbox (1.4 GB) and the exported Toolbox archive (0.5 GB), and a margin.
DISK_IMAGE_FACTOR = 4
WORKSPACE_IMPORT_BYTES = 1_400_000_000
TOOLBOX_ARCHIVE_BYTES = 500_000_000
DISK_MARGIN_BYTES = 2_000_000_000
# Section 4 step 1: filesystems the data directory must not live on (L4).
NETWORK_FILESYSTEMS = {"nfs", "nfs4", "cifs", "smb2", "fuseblk", "fuse", "9p", "virtiofs"}
FUNNEL_ATTEMPTS = 18       # section 10 step 7: every 10 s for up to 3 minutes
FUNNEL_INTERVAL_S = 10
PREFETCH_TICK_S = 10       # section 6.4: the model cache size is printed this often
SELFTEST_PROJECT = "Self-Test"
# Section 7.5 step 3 (final review, finding 2): the proof's connector has a name only the proof
# writes, so the `self-test` connector of an adopted table target (kei test's) is never matched,
# reused or deleted.  The slug is what Admin derives from the name and what release.py's `local`
# target names as its connector (resolve_target), which qa_release uses as the route slug.
PROOF_CONNECTOR_NAME = "Install proof"
PROOF_CONNECTOR_SLUG = "install-proof"
# Only the Funnel check's unauthenticated MCP probe (section 10 step 7) still uses this slug: the
# proof deletes its connector, so no connector of either name has to exist for a 401.
SELFTEST_CONNECTOR_SLUG = "self-test"
KEEP_STAGED_RELEASES = 2     # section 9 update: the current release plus one to roll back to
PROVISION_SCRIPT_IN_CONTAINER = "/tmp/provision_selftest.py"
CREDENTIALS_SCRIPT_IN_CONTAINER = "/tmp/sac.py"
CONFIRM_DELETE_PHRASE = "DELETE COGNITA DATA"
# Design 19.6: the command users type.  A Linux clone runs ./cognita; Windows setup passes
# `--command-name cognita`, which install records as COGNITA_COMMAND in the env file.
DEFAULT_COMMAND = "./cognita"
COMMAND_KEY = "COGNITA_COMMAND"
DIAGNOSTIC_LOG_LINES = 2000       # design 19.5: the tail kept from each log
# Design 21.2: what install.env remembers about the self-tests: `passed` after a proof that passed,
# `skipped` after one the person stopped ("Skip self-tests" in Windows Setup).  `status` reads it.
PROOF_KEY = "COGNITA_PROOF"
PROOF_PASSED = "passed"
PROOF_SKIPPED = "skipped"
# The progress line a skipped proof writes (design 21.2): stage `proof`, state `done`, this title and message.
PROOF_SKIPPED_TITLE = "Self-tests skipped"
PROOF_SKIPPED_TEXT = "The self-tests were stopped at your request. Run Setup again later to run them."


class CliError(Exception):
    """A failure the user can act on: what was seen, and the exact next command."""

    def __init__(self, message: str, *, hint: str | None = None, code: int = EXIT_FAILED):
        super().__init__(message)
        self.hint = hint
        self.code = code


# --------------------------------------------------------------------------
# release.py adapter (the ONLY place this file calls release.py)
# --------------------------------------------------------------------------
# Names and argument lists below follow section 5.7 of the design.  Coder A owns the bodies of
# the new ones (resolve_target, stage_published, write_folders_fragment, enable_unit,
# status_lines, select_release); the rest exist in release.py today.  Each is looked up at call
# time so the tests replace them by name.


def rel_resolve_target(name: str):
    return release.resolve_target(name)


def rel_stage_published(target, log, on_image=None) -> Path:
    """``on_image(ref, size_bytes)`` is release.stage_published's per-image hook (design 19.1).  It is only
    passed when there is one, so a caller with no progress file calls exactly what it always did."""
    if on_image is None:
        return Path(release.stage_published(target, log))
    return Path(release.stage_published(target, log, on_image=on_image))


def rel_write_folders_fragment(target, release_dir: Path, log=None) -> None:
    release.write_folders_fragment(target, release_dir, log)


def rel_apply(repo: Path, target, release_dir: Path, toolbox_version: str, log) -> None:
    release.apply_release(repo, target, release_dir, toolbox_version, log)


def rel_enable_unit(target, log=None) -> None:
    release.enable_unit(target, log)


def rel_verify(repo: Path, target, version: str, log) -> None:
    release.verify_release(repo, target, version, log)


def rel_qa(repo: Path, target, version: str, tmp: Path, log, stop_check: Callable[[], bool] | None = None) -> None:
    """``stop_check`` is release.qa_release's design 21.2 hook (the "Skip self-tests" button).  Like
    ``on_image`` above it is only passed when there is one, so a run without a progress file calls exactly
    what it always did."""
    if stop_check is None:
        release.qa_release(repo, target, version, tmp, log)
    else:
        release.qa_release(repo, target, version, tmp, log, stop_check=stop_check)


def rel_select(target, version: str, log) -> None:
    release.select_release(target, version, log)


def rel_status_lines(target) -> list[str]:
    return list(release.status_lines(target))


def rel_export_version(version: str, log) -> None:
    release.export_version(version, log)


def rel_staged_compose_files(directory: Path, profile: str) -> list[Path]:
    return release.staged_compose_files(directory, profile)


def rel_compose_command(*, project: str, env_file: Path, files: list[Path]) -> list[str]:
    return release.compose_command(project=project, env_file=env_file, files=files)


def rel_read_env_file(path: Path) -> dict[str, str]:
    return release.read_env_file(path)


def rel_read_release_text(directory: Path) -> dict[str, str]:
    return release.read_release_text(directory)


def rel_recorded_release_tags(values: dict[str, str]) -> dict[str, str]:
    return release.recorded_release_tags(values)


def rel_target_lock(path: Path, log):
    return release.target_lock(path, log)


def rel_run(command: list[str], *, log, state: str = "usage", stdin_text: str | None = None,
            check: bool = True, quiet: bool = False) -> tuple[int, str]:
    return release.run(command, log=log, state=state, stdin_text=stdin_text, check=check,
                       quiet=quiet)


def rel_known_targets() -> dict:
    return dict(release.TARGETS)


def rel_target_root(target) -> Path:
    return release.target_root(target)


def rel_prune(target, keep: int, log) -> None:
    """release.py's own prune: it takes no lock, so it is safe under the CLI's (section 5.7)."""
    release.prune(target, keep, log)


def rel_releases_root_default() -> str:
    return str(release.RELEASES_ROOT)


# --------------------------------------------------------------------------
# Log, UI, process runner, host facts, HTTP: the seams
# --------------------------------------------------------------------------


class InstallLog(release.Log):
    """release.Log that can start life without a file.

    "The first run creates nothing until step 5" (section 4), yet steps 1-4 must still be in the
    install log.  Lines are held until attach() gives the log a file, then written first.
    """

    def __init__(self, path: Path | None = None):
        super().__init__(None)
        self._pending: list[str] = []
        if path is not None:
            self.attach(path)

    def attach(self, path: Path) -> None:
        if self._stream is not None:      # a later command in the same process starts its own file
            self._stream.close()
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._stream = path.open("a", encoding="utf-8")
        for text in self._pending:
            self._stream.write(text + "\n")
        self._stream.flush()
        self._pending.clear()

    def _record(self, rendered: str) -> None:
        if self._stream is not None:
            self._stream.write(rendered + "\n")
            self._stream.flush()
        else:
            self._pending.append(rendered)

    def line(self, text: str) -> None:
        """A decision, skip or step, with its values: to the LOG ONLY, stamped in local time (C21).
        The screen gets the user-facing text from UI.say and the child output from raw()."""
        self._record(f"{dt.datetime.now().strftime('%H:%M:%S')} {text}")

    def raw(self, text: str) -> None:
        """Text the user should see (and child process output): screen and log."""
        print(text, flush=True)
        self._record(text)


# Design 19.1: the stage ids a progress file may carry, with the plain-words title each shows.  One per
# install step; update, rollback, add_folder and password_change are an outer start/done pair around the
# install ids they actually run.
STAGE_TITLES = {
    "checks": "Checking this machine",
    "prerequisites": "Checking prerequisites",
    "plan": "Making the plan",
    "layout": "Creating folders, secrets and settings",
    "images": "Downloading Cognita",
    "password": "Setting the Admin password",
    "models": "Downloading the search models",
    "ocr_weights": "Downloading the OCR model files",
    "linger": "Setting Cognita to start at boot",
    "start": "Starting Cognita",
    # 15.0.0 (DESIGN-NVIDIA-ACCELERATION 11): this is the AMD wording and the default; Progress.emit swaps
    # the vendor name for the profile being checked (Progress.accel_label), so an NVIDIA install says
    # "Checking the NVIDIA card".
    "acceleration": "Checking the AMD card",
    "proof": "Running self-tests to verify the installation",
    "remote_access": "Setting up remote access",
    "finish": "Finishing",
    "update": "Updating Cognita",
    "rollback": "Going back to the previous release",
    "add_folder": "Adding a documents folder",
    "password_change": "Changing the Admin password",
}
PROGRESS_SCHEMA = 1


class Progress:
    """The machine-readable progress file (design 19.1): one JSON object per line, appended and flushed,
    never rewritten.  Windows setup tails it to draw its own progress screen.

        {"schema": 1, "time": "<local ISO>", "stage": "<id>", "title": "<plain words>",
         "state": "start|progress|done|failed|warning", "bytes_done": N, "bytes_total": N,
         "message": "...", "fix": "...", "presentation_id": "...",
         "presentation_values": {"name": "nonsensitive value"}}

    ``bytes_*`` appear only on download stages; ``message`` and ``fix`` only on ``failed`` and ``warning``.
    Presentation fields are optional and Setup-only; the English detail and fix remain present for support.
    Without a path every method is a no-op, so a caller never has to ask whether progress is on. The file
    carries no secrets. A write failure is logged ONCE and never fails the command. ``render`` (the UI's
    command-name rewrite) is applied to message and fix so the file says ``cognita``, not ``./cognita``,
    where the terminal does.
    """

    def __init__(self, path: str | os.PathLike | None = None, *, log=None,
                 clock: Callable[[], dt.datetime] | None = None,
                 render: Callable[[str], str] | None = None, append: bool = False):
        self.path = Path(path) if path else None
        self.log = log
        self.clock = clock or (lambda: dt.datetime.now().astimezone())
        self.render = render or (lambda text: text)
        self.append = append        # True on the re-exec after `git pull`: the file is NOT truncated again
        self.broken = False
        self.outer: str | None = None
        self.stage: str | None = None
        self._bytes: dict[str, tuple[int, int]] = {}
        self.accel_label = "AMD"    # 15.0.0: the vendor named in the `acceleration` stage's title

    @property
    def enabled(self) -> bool:
        return self.path is not None and not self.broken

    def _log(self, text: str) -> None:
        if self.log is not None:
            self.log.line(text)

    def start_command(self) -> None:
        """Truncate the file, once, when the command starts (not on the re-exec after `git pull`)."""
        if self.path is None:
            return
        if self.append:
            self._log(f"progress: continuing {self.path} (re-exec after the pull; not truncated)")
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text("", encoding="utf-8")
            self._log(f"progress: started {self.path}")
        except OSError as exc:
            self._break(exc)

    def _break(self, exc: OSError) -> None:
        if not self.broken:
            self.broken = True
            self._log(f"progress: cannot write {self.path} ({type(exc).__name__}: {exc}); "
                      "progress reporting is off for this run, the command goes on")

    def emit(self, stage: str, state: str, *, bytes_done: int | None = None, bytes_total: int | None = None,
             message: str | None = None, fix: str | None = None, title: str | None = None,
             presentation_id: str | None = None,
             presentation_values: dict[str, str] | None = None) -> None:
        """``title`` replaces the stage's usual title for this one line (design 21.2: a skipped proof)."""
        if not self.enabled:
            return
        if title is None:
            # The stage's usual title; the acceleration stage names the vendor being checked (15.0).
            title = STAGE_TITLES.get(stage, stage)
            if stage == "acceleration":
                title = title.replace("AMD", self.accel_label)
        record: dict = {"schema": PROGRESS_SCHEMA, "time": self.clock().isoformat(timespec="seconds"),
                        "stage": stage, "title": title, "state": state}
        if bytes_done is not None and bytes_total is not None:
            record["bytes_done"], record["bytes_total"] = int(bytes_done), int(bytes_total)
            self._bytes[stage] = (int(bytes_done), int(bytes_total))
        if message is not None:
            record["message"] = self.render(message)
        if fix is not None:
            record["fix"] = self.render(fix)
        if state in {"warning", "failed"} and presentation_id is not None:
            record["presentation_id"] = presentation_id
            record["presentation_values"] = dict(presentation_values or {})
        try:
            with open(self.path, "a", encoding="utf-8", newline="\n") as stream:
                stream.write(json.dumps(record, ensure_ascii=False) + "\n")
                stream.flush()
        except OSError as exc:
            self._break(exc)

    # -- the outer pair (update, rollback, add_folder, password_change) --------------------------------

    def outer_begin(self, name: str) -> None:
        self.outer = name
        self.emit(name, "start")

    def outer_resume(self, name: str) -> None:
        """Carry on an outer pair started before a re-exec: no second `start` line."""
        self.outer = name

    def outer_done(self) -> None:
        if self.outer is not None:
            self.end_stage()
            self.emit(self.outer, "done")
            self.outer = None

    # -- an install stage --------------------------------------------------------------------------------

    def begin(self, stage: str) -> None:
        """Start ``stage``; the stage that was open is finished first (steps run one after another).
        Beginning the stage that is already open changes nothing."""
        if self.stage == stage:
            return
        self.end_stage()
        self.stage = stage
        self.emit(stage, "start")

    def end_stage(self) -> None:
        if self.stage is not None:
            done, total = self._bytes.get(self.stage, (None, None))
            self.emit(self.stage, "done", bytes_done=done, bytes_total=total)
            self.stage = None

    def finish_stage_as(self, stage: str, title: str, message: str) -> None:
        """Finish ``stage`` with its own title and message (design 21.2: the proof, skipped).  When it is the
        open stage it is closed here, so the next ``begin`` does not write a second `done` line for it."""
        self.emit(stage, "done", message=message, title=title)
        if self.stage == stage:
            self.stage = None

    def update(self, stage: str, done: int, total: int) -> None:
        self.emit(stage, "progress", bytes_done=min(done, total) if total else done, bytes_total=total)

    def warning(self, stage: str, message: str, *, presentation_id: str | None = None,
                presentation_values: dict[str, str] | None = None) -> None:
        self.emit(stage, "warning", message=message, presentation_id=presentation_id,
                  presentation_values=presentation_values)

    def fail(self, message: str, fix: str | None = None, *, default_stage: str = "checks",
             presentation_id: str | None = None,
             presentation_values: dict[str, str] | None = None) -> None:
        """A `failed` line for the stage that was open (else the outer one, else ``default_stage``)."""
        self.emit(self.stage or self.outer or default_stage, "failed", message=message, fix=fix or "",
                  presentation_id=presentation_id, presentation_values=presentation_values)


class UI:
    """Everything the user sees and types.  Text goes to the screen and the log together."""

    def __init__(self, log, *, non_interactive: bool = False, assume_yes: bool = False,
                 input_fn: Callable[[str], str] = input,
                 getpass_fn: Callable[[str], str] = getpass.getpass):
        self.log = log
        self.non_interactive = non_interactive
        self.assume_yes = assume_yes
        self._input = input_fn
        self._getpass = getpass_fn
        self.current_step = ""
        self.command = DEFAULT_COMMAND          # design 19.6: what the user types; set by main and install
        self.progress = Progress(render=self.render)

    # Every message below is written with the launcher's default spelling and rendered here, so the ONE
    # setting (COGNITA_COMMAND) decides what every user-facing line says (design 19.6).
    _COMMAND_TOKEN = re.compile(r"(?<![\w/.~-])\./cognita(?![\w-])")

    def render(self, text: str) -> str:
        if self.command == DEFAULT_COMMAND:
            return text
        return self._COMMAND_TOKEN.sub(lambda _match: self.command, text)

    def say(self, text: str = "") -> None:
        self.log.raw(self.render(text))

    def step(self, title: str, stage: str | None = None) -> None:
        """A numbered install step.  Remembered so a failure can name the step it happened in.  ``stage``
        is the progress-file id of the step (design 19.1); steps with none leave the open stage alone."""
        self.current_step = title
        self.log.line(f"step: {title}")
        self.log.raw(f"\n{self.render(title)}")
        if stage:
            self.progress.begin(stage)

    def ask(self, prompt: str, *, default: str | None, flag: str) -> str:
        if self.non_interactive:
            # C2: every question has a default or a flag, so an unattended run
            # takes the shown default; only a question with no default stops.
            if default:
                self.log.line(f"ask: {prompt} -> {default!r} (default, --non-interactive)")
                return default
            raise CliError(f"{prompt} A value is required and --non-interactive was given.",
                           hint=f"Pass --{flag}.")
        suffix = f" [{default}]" if default else ""
        answer = self._input(f"{prompt}{suffix}: ").strip()
        value = answer or (default or "")
        self.log.line(f"ask: {prompt} -> {value!r}")
        return value

    def confirm(self, prompt: str, *, default: bool, preset: bool | None = None,
                flag: str | None = None, preference: bool = False) -> bool:
        """Yes/no.  ``preset`` is the answer a flag already gave.  Without one, a non-interactive
        run takes the shown default for a PREFERENCE (Workspace on, the Funnel offer; C2: every
        question has a default or a flag), and fails naming the flag for a CONSENT question
        (installing software, sudo, plain-HTTP exposure, uninstall), which is never guessed (C4)."""
        if preset is not None:
            self.log.line(f"confirm: {prompt} -> {preset} (from --{flag})")
            return preset
        if self.non_interactive and preference:
            self.log.line(f"confirm: {prompt} -> {default} (default, --non-interactive)")
            return default
        if self.non_interactive:
            raise CliError(f"{prompt} An answer is required and --non-interactive was given.",
                           hint=f"Pass --{flag}." if flag else None)
        hint = "Y/n" if default else "y/N"
        answer = self._input(f"{prompt} [{hint}] ").strip().lower()
        result = default if not answer else answer in ("y", "yes")
        self.log.line(f"confirm: {prompt} -> {result}")
        return result

    def secret(self, prompt: str) -> str:
        return self._getpass(prompt)

    def pause(self, prompt: str) -> None:
        """Wait for the user to do something outside this program (a browser approval)."""
        if self.non_interactive:
            raise CliError(prompt.strip(), hint="Do that, then run the same command again without --non-interactive.")
        self._input(prompt)


@dataclass
class Result:
    rc: int
    out: str = ""
    err: str = ""

    @property
    def ok(self) -> bool:
        return self.rc == 0


class Runner:
    """Runs commands.  capture = a probe whose output we read; stream = a command whose output the
    user should see (logged line by line by release.run); interactive = inherits the terminal
    (sudo asks for a password, tailscale prints a link); ticking = stream + a periodic callback."""

    def __init__(self, log):
        self.log = log

    def capture(self, argv: list[str], *, stdin_text: str | None = None, timeout: int = 120) -> Result:
        try:
            done = subprocess.run(argv, input=stdin_text, capture_output=True, text=True,
                                  timeout=timeout, check=False)
            result = Result(done.returncode, done.stdout, done.stderr)
        except FileNotFoundError:
            result = Result(127, "", f"{argv[0]}: command not found")
        except subprocess.TimeoutExpired:
            result = Result(124, "", f"{argv[0]}: timed out after {timeout}s")
        self.log.line(f"probe: {' '.join(argv)} -> exit {result.rc}")
        if result.rc in release._INTERRUPTED_EXITS:   # a Ctrl+C that only the child saw (see release.run)
            raise KeyboardInterrupt
        return result

    def stream(self, argv: list[str], *, stdin_text: str | None = None, check: bool = True,
               quiet: bool = False, state: str = "usage") -> Result:
        rc, tail = rel_run(argv, log=self.log, state=state, stdin_text=stdin_text, check=check,
                           quiet=quiet)
        return Result(rc, tail, "")

    def interactive(self, argv: list[str]) -> int:
        self.log.line(f"run (interactive): {' '.join(argv)}")
        try:
            rc = subprocess.call(argv)
        except FileNotFoundError:
            self.log.line(f"exit 127: {argv[0]} not found")
            return 127
        self.log.line(f"exit {rc}: {argv[0]}")
        if rc in release._INTERRUPTED_EXITS:   # a Ctrl+C that only the child saw (see release.run)
            raise KeyboardInterrupt
        return rc

    def ticking(self, argv: list[str], tick: Callable[[], None], interval_s: float,
                *, stdin_text: str | None = None) -> int:
        stop = threading.Event()

        def loop() -> None:
            while not stop.wait(interval_s):
                try:
                    tick()
                except Exception as exc:  # noqa: BLE001 - a progress line must never kill the run
                    self.log.line(f"tick: progress callback failed: {type(exc).__name__}: {exc}")

        thread = threading.Thread(target=loop, daemon=True)
        thread.start()
        try:
            return self.stream(argv, stdin_text=stdin_text, check=False, quiet=True).rc
        finally:
            stop.set()
            thread.join()


class Host:
    """Facts about this machine that are not a command.  Tests replace it with a dict-backed fake."""

    def read_text(self, path: str) -> str | None:
        try:
            return Path(path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None

    def os_release(self) -> dict[str, str]:
        values: dict[str, str] = {}
        for line in (self.read_text("/etc/os-release") or "").splitlines():
            key, sep, value = line.partition("=")
            if sep:
                values[key.strip()] = value.strip().strip('"')
        return values

    def machine(self) -> str:
        return os.uname().machine

    def exists(self, path: str) -> bool:
        return os.path.exists(path)

    def is_dir(self, path: str) -> bool:
        return os.path.isdir(path)

    def is_char_device(self, path: str) -> bool:
        import stat as stat_module
        try:
            return stat_module.S_ISCHR(os.stat(path).st_mode)
        except OSError:
            return False

    def access(self, path: str, mode: int) -> bool:
        return os.access(path, mode)

    def glob(self, pattern: str) -> list[str]:
        import glob
        return sorted(glob.glob(pattern))

    def gid_of(self, path: str) -> int | None:
        try:
            return os.stat(path).st_gid
        except OSError:
            return None

    def uid(self) -> int:
        return os.getuid()

    def gid(self) -> int:
        return os.getgid()

    def user(self) -> str:
        return getpass.getuser()

    def hostname(self) -> str:
        return socket.gethostname()

    def home(self) -> str:
        return str(Path.home())

    def which(self, name: str) -> str | None:
        return shutil.which(name)

    def realpath(self, path: str) -> str:
        return os.path.realpath(path)

    def free_bytes(self, path: str) -> int:
        return shutil.disk_usage(path).free

    def device_of(self, path: str) -> int:
        return os.stat(path).st_dev

    def port_free(self, port: int) -> bool:
        with contextlib.closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as probe:
            # SO_REUSEADDR as every real server (docker-proxy included) sets it:
            # without it, connections still in TIME_WAIT (for example the proof's
            # own requests, just before an uninstall) make a free port look taken.
            # Seen 2026-09-28 reinstalling on the proof VM.  A LISTENING socket
            # still makes this bind fail, which is what the check is for.
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                probe.bind(("127.0.0.1", port))
            except OSError:
                return False
            return True

    def dir_size(self, path: str) -> int:
        total = 0
        for root, _dirs, files in os.walk(path):
            for name in files:
                with contextlib.suppress(OSError):
                    total += os.lstat(os.path.join(root, name)).st_size
        return total

    def write_temp(self, text: str) -> str:
        handle, name = tempfile.mkstemp(prefix="cognita-install-", suffix=".tmp")
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(text)
        return name

    def remove(self, path: str) -> None:
        with contextlib.suppress(OSError):
            os.unlink(path)


class AdminError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(f"HTTP {status}: {detail}")
        self.status = status
        self.detail = detail


class AdminClient:
    """One Admin session (section 7.5 step 1): login, then the CSRF cookie echoed as a header.

    Admin is always reached at 127.0.0.1.  With Admin TLS on it is reached over HTTPS with
    certificate verification OFF, for loopback only: the user's certificate names their host,
    not 127.0.0.1.  That is the one place this file turns verification off, and the constructor
    refuses any other host.
    """

    def __init__(self, port: int, *, https: bool = False, opener=None, host: str = "127.0.0.1", log=None):
        if host != "127.0.0.1":
            raise ValueError("AdminClient only talks to 127.0.0.1")
        self.log = log
        self.base = f"{'https' if https else 'http'}://{host}:{port}"
        self.jar = http.cookiejar.CookieJar()
        if opener is None:
            handlers: list = [urllib.request.HTTPCookieProcessor(self.jar)]
            if https:
                unverified = ssl.create_default_context()
                unverified.check_hostname = False
                unverified.verify_mode = ssl.CERT_NONE
                handlers.append(urllib.request.HTTPSHandler(context=unverified))
            opener = urllib.request.build_opener(*handlers)
        self.opener = opener
        self.csrf: str | None = None

    def request(self, method: str, path: str, body=None, *, timeout: int = 60):
        data = None if body is None else json.dumps(body).encode("utf-8")
        req = urllib.request.Request(self.base + path, method=method, data=data)
        if body is not None:
            req.add_header("Content-Type", "application/json")
        if self.csrf:
            req.add_header("X-CSRF-Token", self.csrf)
        try:
            with self.opener.open(req, timeout=timeout) as response:
                raw = response.read()
                status = getattr(response, "status", 200)
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                payload = json.loads(exc.read() or b"{}")
                detail = str(payload.get("detail") or payload.get("message") or payload)
            except (ValueError, AttributeError, OSError) as parse_error:   # a non-JSON error body is still an error
                if self.log:
                    self.log.line(f"admin: {method} {path} -> HTTP {exc.code}; body was not JSON ({parse_error})")
            if self.log:
                self.log.line(f"admin: {method} {path} -> HTTP {exc.code}")
            raise AdminError(exc.code, detail) from exc
        except (urllib.error.URLError, OSError) as exc:
            if self.log:
                self.log.line(f"admin: {method} {path} -> no answer ({exc})")
            raise AdminError(0, f"could not reach Admin at {self.base}: {exc}") from exc
        if self.log:
            self.log.line(f"admin: {method} {path} -> HTTP {status}")   # never the body: it may hold a password
        return json.loads(raw) if raw else {}

    def login(self, username: str, password: str) -> None:
        self.request("GET", "/api/session")
        self.request("POST", "/api/login", {"username": username, "password": password})
        cookie = next((c.value for c in self.jar if c.name == "cognita_csrf"), None)
        if not cookie:
            raise AdminError(0, "Admin did not issue a CSRF cookie after login")
        self.csrf = cookie


def real_http(method: str, url: str, *, data: bytes | None = None,
              headers: dict[str, str] | None = None, timeout: int = 20) -> tuple[int | None, str]:
    """A plain HTTP call with normal certificate verification.  None = nothing answered."""
    req = urllib.request.Request(url, method=method, data=data, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return response.status, response.read(65536).decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(65536).decode("utf-8", "replace")
    except (urllib.error.URLError, OSError, ValueError):
        return None, ""


def real_reexec(argv: list[str]) -> None:
    os.execv(sys.executable, [sys.executable, str(Path(__file__).resolve()), *argv])


def real_stdin_line() -> bytes:
    """One line of standard input as raw bytes (design 19.9 item 9).  Bytes, so the UTF-8 decode is ours
    and does not depend on the locale Python happened to start under.  A terminal is refused: reading it
    would wait for a password typed with echo on (the flag is for a program piping it in)."""
    if sys.stdin is None or sys.stdin.isatty():
        raise CliError("--admin-password-stdin needs the password piped in, not typed at a terminal.",
                       hint="Pipe it in, use --admin-password-file, or drop the flag and type it when asked.")
    return sys.stdin.buffer.readline()


@dataclass
class Ctx:
    """Everything a command needs, injected so a test can fake all of it."""

    log: InstallLog
    ui: UI
    sh: Runner
    host: Host
    repo: Path = REPO_ROOT
    env_path: Path = field(default_factory=lambda: default_env_path())
    admin_factory: Callable[..., AdminClient] = AdminClient
    http: Callable[..., tuple[int | None, str]] = real_http
    sleep: Callable[[float], None] = time.sleep
    reexec: Callable[[list[str]], None] = real_reexec
    check_paths: Callable[[str], list[str]] = lambda path: path_problems(path)
    # Design 19.9 items 9 and 12: the process environment (session variables are set in it at startup, and
    # every child process inherits it) and the one line of standard input a password may arrive on.
    environ: MutableMapping[str, str] = field(default_factory=lambda: os.environ)
    stdin_line: Callable[[], bytes] = real_stdin_line
    stdin_password: str | None = None      # the password once read from stdin: stdin can be read only once
    # True from the moment this run stopped the unit to re-stage the running version until an apply
    # starts it again, so a failure in between can say Cognita is stopped (final review 2, finding 3).
    stopped_for_restage: bool = False


def default_env_path() -> Path:
    override = os.environ.get(ENV_OVERRIDE)
    # install.env: see release.local_env_file (kei has an unrelated 12.x cognita.env there).
    return Path(override) if override else Path(os.path.expanduser("~/.config/cognita/install.env"))


# --------------------------------------------------------------------------
# Paths and the env file (section 3)
# --------------------------------------------------------------------------


def path_problems(path: str) -> list[str]:
    """Why a path cannot go in the env file, unit or Compose files.  Empty = fine (section 3).

    Compose interpolates `$`; systemd expands `%`; quotes and backslashes break the quoting the
    env file, unit and generated YAML rely on; a newline ends the env line.  Spaces are allowed
    (cloud folders often have them) and are handled by quoting (section 5.6).  " #" would start a
    comment in an unquoted env value, and leading or trailing blanks are trimmed by the parser,
    so both are refused too.
    """
    problems: list[str] = []
    if not posixpath.isabs(path):
        problems.append("it must be an absolute path (start with /)")
    for char, name in (("\n", "a newline"), ("\r", "a carriage return"), ("\0", "a NUL byte"),
                       ("$", "a dollar sign"), ('"', "a double quote"), ("'", "a single quote"),
                       ("\\", "a backslash"), ("%", "a percent sign"), (" #", "a space then #")):
        if char in path:
            problems.append(f"it contains {name}")
    if path != path.strip():
        problems.append("it starts or ends with a space")
    return problems


def overlaps(a: str, b: str) -> bool:
    """True when one path is the other or lies inside it (Linux path semantics)."""
    a = posixpath.normpath(a)
    b = posixpath.normpath(b)
    if a == b:
        return True
    return a.startswith(b.rstrip("/") + "/") or b.startswith(a.rstrip("/") + "/")


def containment_problems(host: Host, data_dir: str, roots: list[str]) -> list[str]:
    """Section 3: refused both ways.  The data dir may not be inside or contain a documents root,
    and no two documents roots may nest.  Compared on the given spelling AND on the resolved one,
    so a symlink cannot hide an overlap."""
    problems: list[str] = []

    def forms(path: str) -> set[str]:
        return {posixpath.normpath(path), posixpath.normpath(host.realpath(path))}

    for root in roots:
        if any(overlaps(a, b) for a in forms(data_dir) for b in forms(root)):
            problems.append(
                f"The data directory {data_dir} and the documents folder {root} overlap. Cognita's "
                "secrets and index must stay outside anything served over MCP, and documents must "
                "stay outside anything uninstall or reset deletes. Choose a different --data-dir.")
    for index, first in enumerate(roots):
        for second in roots[index + 1:]:
            if any(overlaps(a, b) for a in forms(first) for b in forms(second)):
                problems.append(f"The documents folders {first} and {second} overlap; "
                                "no documents folder may be inside another one.")
    return problems


def root_key(number: int) -> str:
    """The env key of documents root ``number`` (1 is COGNITA_PROJECTS_ROOT, as compose.yaml binds it)."""
    return "COGNITA_PROJECTS_ROOT" if number == 1 else f"COGNITA_PROJECTS_ROOT_{number}"


def display_key(number: int) -> str:
    """The env key of the display text of root ``number`` (design 19.2)."""
    return "COGNITA_PROJECTS_ROOT_DISPLAY" if number == 1 else f"COGNITA_PROJECTS_ROOT_{number}_DISPLAY"


# Order matters only for readability: this is the order the file is written in.
ENV_ORDER = [
    "COGNITA_RELEASE_TARGET", "COGNITA_VERSION", "COGNITA_ACCELERATION", "COGNITA_WORKSPACE",
    "COGNITA_RELEASES_ROOT", COMMAND_KEY, "COGNITA_CONFIG_ROOT",
    *[key for number in range(1, MAX_DOCUMENT_ROOTS + 1)
      for key in (root_key(number), display_key(number))],
    "COGNITA_POSTGRES_DATA_ROOT", "COGNITA_WORKSPACE_DATA_ROOT", "COGNITA_TRANSFER_STAGING_ROOT",
    "COGNITA_SECRETS_ROOT", "COGNITA_MODEL_CACHE_ROOT", "COGNITA_TOOLBOX_IMAGE_CACHE_ROOT",
    "COGNITA_SERVICE_UID", "COGNITA_SERVICE_GID", "COGNITA_KVM_GID", "COGNITA_VIDEO_GID",
    "COGNITA_RENDER_GID", "COGNITA_MCP_HOST_PORT", "COGNITA_MCP_BIND_ADDRESS",
    "COGNITA_ADMIN_HOST_PORT", "COGNITA_ADMIN_BIND_ADDRESS", "COGNITA_LINGER_SET_BY_INSTALLER",
    PROOF_KEY,
]


def render_env(values: dict[str, str]) -> str:
    command = values.get(COMMAND_KEY) or DEFAULT_COMMAND
    lines = [f"# Written by {command} install. This is Cognita's only per-machine state; do not edit it.",
             f"# Change settings with {command} commands so this file and the release stay in step."]
    seen = set()
    for key in ENV_ORDER:
        if key in values and values[key] != "":
            lines.append(f"{key}={values[key]}")
            seen.add(key)
    for key in sorted(set(values) - seen):  # keys a newer version wrote: kept, never dropped
        if values[key] != "":
            lines.append(f"{key}={values[key]}")
    return "\n".join(lines) + "\n"


def atomic_write(path: Path, data: str | bytes, mode: int = 0o600) -> None:
    """Temp file in the same directory + os.replace, so a crash never leaves half a file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = data.encode("utf-8") if isinstance(data, str) else data
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex[:8]}.tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        with contextlib.suppress(OSError):
            temporary.unlink()
        raise


def write_env(ctx: Ctx, values: dict[str, str]) -> None:
    atomic_write(ctx.env_path, render_env(values), 0o600)
    ctx.log.line(f"env: wrote {ctx.env_path} ({len(values)} keys)")


def read_env(ctx: Ctx) -> dict[str, str]:
    return rel_read_env_file(ctx.env_path)


def document_roots(env: dict[str, str]) -> list[str]:
    roots = [env["COGNITA_PROJECTS_ROOT"]] if env.get("COGNITA_PROJECTS_ROOT") else []
    for number in range(2, MAX_DOCUMENT_ROOTS + 1):
        value = env.get(f"COGNITA_PROJECTS_ROOT_{number}")
        if value:
            roots.append(value)
    return roots


def root_slots(env: dict[str, str]) -> list[tuple[int, str]]:
    """(slot number, path) for every documents root, in slot order.  Slots can have gaps, so a root's
    position in ``document_roots`` is not its slot number; the display keys are addressed by slot."""
    return [(number, env[root_key(number)]) for number in range(1, MAX_DOCUMENT_ROOTS + 1)
            if env.get(root_key(number))]


def root_displays(env: dict[str, str]) -> list[str]:
    """The display text of each root, parallel to ``document_roots(env)``; "" where a root has none."""
    return [env.get(display_key(number), "") for number, _path in root_slots(env)]


DISPLAY_MAX_CHARS = 400
HIDDEN_SHARE_HINT = ("Windows hidden shares such as \\\\nas\\docs$ cannot be used here, because Compose reads "
                     "the settings file and would take the $ for a variable. Use a folder that is not a "
                     "hidden share.")


def display_problems(text: str) -> list[str]:
    """Why a documents display cannot go in the env file.  Empty = fine (design 19.2).

    Separate from ``path_problems`` because Compose reads the env file too: the display is the Windows
    path a person knows the folder by, so backslashes and colons are allowed; but 1-400 printable
    characters, a start that a parser cannot take for a quote (a letter, a backslash or a slash), no
    leading or trailing blank (both parsers trim it), no newline, no ``$`` (interpolation), no ``"``,
    and no ``#`` after whitespace (a comment)."""
    problems: list[str] = []
    if not 1 <= len(text) <= DISPLAY_MAX_CHARS:
        problems.append(f"it must be 1 to {DISPLAY_MAX_CHARS} characters (it is {len(text)})")
    if "\n" in text or "\r" in text:
        problems.append("it contains a newline")
    elif not text.isprintable():
        problems.append("it contains a character that cannot be shown (a control character)")
    if text and not (text[0].isalpha() or text[0] in "\\/"):
        problems.append("it must start with a letter, a backslash or a slash (for example D:\\Documents "
                        "or \\\\nas\\docs)")
    if text != text.strip():
        problems.append("it starts or ends with a space")
    if "$" in text:
        problems.append("it contains a dollar sign. " + HIDDEN_SHARE_HINT)
    if '"' in text:
        problems.append("it contains a double quote")
    if re.search(r"\s#", text):
        problems.append("it contains a space then # (that would start a comment in the settings file)")
    return problems


COMMAND_NAME_RE = re.compile(r"[A-Za-z0-9._/-]{1,100}")


def command_problems(text: str) -> list[str]:
    if COMMAND_NAME_RE.fullmatch(text):
        return []
    return ["use 1 to 100 letters, digits, dots, slashes, underscores or hyphens (for example cognita)"]


def command_of(env: dict[str, str]) -> str:
    """Design 19.6: the command the user types, from the env file; ./cognita when it records none."""
    return env.get(COMMAND_KEY) or DEFAULT_COMMAND


def derived_roots(data_dir: str) -> dict[str, str]:
    """The proof's layout (step1-layout.sh) under one data directory."""
    return {
        "COGNITA_CONFIG_ROOT": f"{data_dir}/config",
        "COGNITA_POSTGRES_DATA_ROOT": f"{data_dir}/postgres",
        "COGNITA_WORKSPACE_DATA_ROOT": f"{data_dir}/workspaces",
        "COGNITA_TRANSFER_STAGING_ROOT": f"{data_dir}/transfers",
        "COGNITA_SECRETS_ROOT": f"{data_dir}/secrets",
        "COGNITA_MODEL_CACHE_ROOT": f"{data_dir}/models",
        "COGNITA_TOOLBOX_IMAGE_CACHE_ROOT": f"{data_dir}/toolbox-cache",
        "COGNITA_RELEASES_ROOT": f"{data_dir}/releases",
    }


def data_dir_of(env: dict[str, str]) -> str:
    config = env.get("COGNITA_CONFIG_ROOT", "")
    return posixpath.dirname(config.rstrip("/")) if config else ""


def local_root(env: dict[str, str]) -> Path:
    """<releases>/local : this install's release directories, logs, Toolbox archive and Self-Test root."""
    return Path(env["COGNITA_RELEASES_ROOT"]) / TARGET_NAME


def selftest_root(env: dict[str, str]) -> Path:
    return local_root(env) / "self-test"


# --------------------------------------------------------------------------
# containers/published-release.txt (section 5.5) and sizes (section 6.3)
# --------------------------------------------------------------------------


def parse_published(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in text.splitlines():
        key, sep, value = line.partition(":")
        if sep and value.strip():
            values[key.strip()] = value.strip()
    return values


def read_published(ctx: Ctx) -> dict[str, str]:
    path = ctx.repo / "containers" / "published-release.txt"
    if not path.is_file():
        raise CliError(
            f"This checkout has no published release ({path} is missing), so there is nothing to install.",
            hint="Update the checkout with: git pull --ff-only")
    values = parse_published(path.read_text(encoding="utf-8"))
    if not values.get("version"):
        raise CliError(f"{path} records no version.")
    ctx.log.line(f"published: version={values['version']} commit={values.get('commit', '?')[:12]}")
    return values


def image_download_bytes(published: dict[str, str], profile: str, workspace: bool) -> int:
    """The compressed images alone: what the published file records for this profile and mode, plus
    PostgreSQL (which it does not record).  The `images` progress stage's bytes_total (design 19.1)."""
    total = _size(published, f"size_cognita_{profile}") + POSTGRES_IMAGE_BYTES
    if workspace:
        total += _size(published, "size_workspace_runtime") + _size(published, "size_toolbox")
    return total


def download_bytes(published: dict[str, str], profile: str, workspace: bool) -> int:
    """Section 6.3: the sizes the published file records for this profile and mode, plus models."""
    return image_download_bytes(published, profile, workspace) + EXPECTED_MODEL_BYTES + OCR_WEIGHTS_BYTES


def disk_bytes(published: dict[str, str], profile: str, workspace: bool) -> int:
    images = _size(published, f"size_cognita_{profile}") + POSTGRES_IMAGE_BYTES
    total = EXPECTED_MODEL_BYTES + OCR_WEIGHTS_BYTES + DISK_MARGIN_BYTES
    if workspace:
        images += _size(published, "size_workspace_runtime") + _size(published, "size_toolbox")
        total += WORKSPACE_IMPORT_BYTES + TOOLBOX_ARCHIVE_BYTES
    return total + images * DISK_IMAGE_FACTOR


def _size(published: dict[str, str], key: str) -> int:
    try:
        return int(published.get(key, "0"))
    except ValueError:
        return 0


def gb(nbytes: float) -> str:
    return f"{nbytes / 1e9:.1f} GB"


# --------------------------------------------------------------------------
# Step 1: checks (C3, L1-L4), read-only
# --------------------------------------------------------------------------


@dataclass
class Facts:
    os_id: str = ""
    os_version: str = ""
    os_codename: str = ""
    # ("ubuntu"|"debian", codename) when Docker's and Tailscale's apt repositories serve this system, else None.
    # Only the install-it-for-you helpers use it; Cognita itself runs on any systemd Linux (15.0.1).
    apt_family: tuple[str, str] | None = None
    machine: str = ""
    systemd_pid1: bool = True
    user_systemd: bool = True
    docker_state: str = "ok"          # ok | missing | denied | down
    docker_group_member: bool = False  # in the docker group per the group database (not the session)
    docker_version: str = ""
    compose_ok: bool = True
    docker_context: str = "default"
    manager_docker_ok: bool = True
    docker_root: str = "/var/lib/docker"
    # Docker runs containers under SELinux (Fedora's own moby-engine does by default). Cognita's folder mounts
    # carry no SELinux labels, so the containers may be refused access to them; untested, hence a warning.
    docker_selinux: bool = False
    data_fs: str = ""
    data_writable: bool = True
    data_free: int = 0
    docker_free: int | None = None
    same_fs: bool = False
    home: str = "/root"
    docs: dict[str, str] = field(default_factory=dict)   # root -> ok|missing|notdir|noread|nowrite
    # root -> (propagation, mount point) as Docker's daemon sees it (systemd's /proc/1/mountinfo); absent when
    # that table could not be read.  compose.yaml binds every documents root with `rslave`, which Docker refuses
    # on a private mount.  WSL makes every mount private, even `/` (DESIGN-WINDOWS-INSTALLER P1).
    docs_mount: dict[str, tuple[str, str]] = field(default_factory=dict)
    wsl: bool = False
    ports_busy: list[int] = field(default_factory=list)
    collision: str = ""
    kvm: bool = False
    kfd: bool = False
    render_nodes: list[str] = field(default_factory=list)
    amdgpu_module: bool = False
    lspci_amd: bool | None = None      # None = lspci not installed
    # 15.0.0 (DESIGN-NVIDIA-ACCELERATION 11).  nvidia_driver: the kernel driver is loaded, or (WSL) the Windows
    # driver's /dev/dxg and libnvidia-ml are visible; nvidia_wsl: that second condition held; nvidia_runtime:
    # Docker's runtime list names `nvidia` (None = Docker was not asked or did not answer, so it is UNKNOWN, never
    # "absent": a stopped daemon must not read as a vanished toolkit); nvidia_driver_version: "580.65" etc., None
    # when it could not be read; lspci_nvidia: None = lspci not installed (and under WSL lspci lists nothing,
    # which is False, so the WSL case never consults it).
    nvidia_driver: bool = False
    nvidia_wsl: bool = False
    nvidia_runtime: bool | None = None
    nvidia_driver_version: str | None = None
    lspci_nvidia: bool | None = None
    video_gid: int | None = None
    render_gid: int | None = None
    linger: bool | None = None
    uid: int = 1000
    gid: int = 1000
    user: str = "user"
    mem_total: int | None = None       # bytes, from /proc/meminfo; None = unreadable


# Measured on the proof VMs, 2026-09-28: the running service holds 2.1-2.9 GiB, and
# CPU OCR admits a job only with ocr_worker_memory_mb + ocr_cpu_reserve_ram_mb
# (2048 + 1024 MiB) AVAILABLE (assets/ocr_service._cpu_headroom_available).  A 6 GiB VM
# installed fine and then timed out on every OCR call; 10 GiB passed.  "8 GB" machines
# report about 7.6-7.8 GiB total, hence the 7.5 GiB floor.
MIN_MEMORY_BYTES = int(7.5 * 1024**3)


def meminfo_total(text: str | None) -> int | None:
    for line in (text or "").splitlines():
        if line.startswith("MemTotal:"):
            parts = line.split()
            if len(parts) >= 2 and parts[1].isdigit():
                return int(parts[1]) * 1024
    return None


@dataclass
class Finding:
    name: str
    saw: str
    fix: str


@dataclass
class Report:
    problems: list[Finding] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)   # printed again above the finish screen
    notes: list[str] = field(default_factory=list)      # printed once, at step 1
    needs_docker: bool = False
    docker_denied: bool = False
    manager_blocked: bool = False
    workspace_available: bool = False
    amd_ok: bool = False
    nvidia_ok: bool = False
    # An NVIDIA card and driver are there but Docker did not answer about its runtime, so whether NVIDIA still
    # works is UNKNOWN (15.0 review: unknown is never "gone"; a rerun must not downgrade a saved nvidia on it).
    nvidia_runtime_unknown: bool = False
    # The GPU facts `evaluate` judged, kept so `choose_hardware` can say WHY a requested GPU was dropped under
    # --acceleration-fallback cpu (design 22.12 item 2).  None for a Report built by hand.
    facts: Facts | None = None


# The two user-manager probes of step 1 (once shared with a one-machine adoption that was withdrawn,
# design 19.10): the user's systemd must be up, and a service started by that manager must reach Docker
# (proof P3, because cognita.service runs docker compose under it).
USER_SYSTEMD_PROBE = ["systemctl", "--user", "is-system-running"]
MANAGER_DOCKER_PROBE = ["systemd-run", "--user", "--wait", "--pipe", "--quiet",
                        "docker", "info", "--format", "{{.ServerVersion}}"]


def user_systemd_ok(answer: str) -> bool:
    return answer.strip() in {"running", "degraded", "starting", "initializing", "maintenance", "stopping"}


def unit_file_path(host: Host) -> str:
    return posixpath.join(host.home(), ".config", "systemd", "user", UNIT)


def nearest_existing(host: Host, path: str) -> str:
    current = posixpath.normpath(path)
    while not host.exists(current) and current not in ("/", ""):
        current = posixpath.dirname(current)
    return current or "/"


def mount_propagation(mountinfo: str, path: str) -> tuple[str, str] | None:
    """(shared|slave|private, mount point) of the mount holding ``path``, from a mountinfo table.

    The mount point is field 5, octal-escaped (a space is ``\\040``); the optional fields between field 7 and the
    lone ``-`` carry ``shared:N`` / ``master:N``, and a mount with neither is private.  The longest mount point
    that contains ``path`` wins, and when one point is mounted twice the later line (the top one) does."""
    best: tuple[str, str] | None = None
    for line in mountinfo.splitlines():
        fields = line.split()
        if len(fields) < 7 or "-" not in fields[6:]:
            continue
        point = re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), fields[4])
        if not (path == point or point == "/" or path.startswith(point.rstrip("/") + "/")):
            continue
        if best is not None and len(point) < len(best[1]):
            continue
        optional = fields[6:fields.index("-", 6)]
        kind = ("shared" if any(o.startswith("shared:") for o in optional)
                else "slave" if any(o.startswith("master:") for o in optional) else "private")
        best = (kind, point)
    return best


def apt_family(osr: dict[str, str]) -> tuple[str, str] | None:
    """Which of Docker's / Tailscale's apt repositories fits this system, with its codename, or None.

    Ubuntu and its derivatives (Mint, Pop!_OS, ...: ID_LIKE names ubuntu, UBUNTU_CODENAME gives the base) use
    the `ubuntu` repository; Debian and its other derivatives use `debian`.  Anything else (Fedora, Arch, a
    homebrew distro) gets None, and the helpers point at the vendor's own instructions instead of guessing a
    package manager.  None also when no codename can be read, because the repository line needs one."""
    ids = {osr.get("ID", "").lower(), *osr.get("ID_LIKE", "").lower().split()}
    if "ubuntu" in ids:
        codename = osr.get("UBUNTU_CODENAME") or osr.get("VERSION_CODENAME", "")
        return ("ubuntu", codename) if codename else None
    if "debian" in ids:
        # A Debian derivative carries its OWN codename in VERSION_CODENAME (LMDE 6: faye, Kali: kali-rolling),
        # which Docker's and Tailscale's debian repositories do not serve; DEBIAN_CODENAME names the base.
        # Only Debian itself may use VERSION_CODENAME.  (15.0.1 review: `debian faye` broke apt for good.)
        codename = osr.get("DEBIAN_CODENAME") or (osr.get("VERSION_CODENAME", "")
                                                   if osr.get("ID", "").lower() == "debian" else "")
        return ("debian", codename) if codename else None
    return None


def collect_facts(ctx: Ctx, data_dir: str, docs: list[str], ports: list[int], *, rerun: bool) -> Facts:
    """Gather what step 1 needs.  Nothing here writes to the machine."""
    host, sh = ctx.host, ctx.sh
    facts = Facts()
    osr = host.os_release()
    facts.os_id, facts.os_version = osr.get("ID", ""), osr.get("VERSION_ID", "")
    facts.os_codename = osr.get("VERSION_CODENAME", "")
    facts.apt_family = apt_family(osr)
    facts.machine = host.machine()
    facts.uid, facts.gid, facts.user = host.uid(), host.gid(), host.user()
    facts.systemd_pid1 = (host.read_text("/proc/1/comm") or "").strip() == "systemd"
    state = sh.capture(USER_SYSTEMD_PROBE)
    facts.user_systemd = user_systemd_ok(state.out)

    # Docker: the shell, the group database, the Compose plugin, the context, the daemon's root,
    # and, because the unit runs docker compose under the user's systemd MANAGER (proof P3),
    # whether the manager can reach Docker too.
    if not host.which("docker"):
        facts.docker_state = "missing"
    else:
        server = sh.capture(["docker", "version", "--format", "{{.Server.Version}}"])
        if server.ok:
            facts.docker_state = "ok"
            facts.docker_version = server.out.strip()
        else:
            text = (server.err + server.out).lower()
            facts.docker_state = "denied" if "permission denied" in text else "down"
        group = sh.capture(["getent", "group", "docker"])
        members = group.out.strip().rsplit(":", 1)[-1].split(",") if group.ok else []
        facts.docker_group_member = facts.user in members
    if facts.docker_state == "ok":
        facts.compose_ok = sh.capture(["docker", "compose", "version", "--short"]).ok
        facts.docker_context = sh.capture(["docker", "context", "show"]).out.strip()
        root = sh.capture(["docker", "info", "--format", "{{.DockerRootDir}}"])
        if root.ok and root.out.strip():
            facts.docker_root = root.out.strip()
        security = sh.capture(["docker", "info", "--format", "{{json .SecurityOptions}}"])
        facts.docker_selinux = security.ok and "name=selinux" in security.out
        facts.manager_docker_ok = sh.capture(MANAGER_DOCKER_PROBE).ok

    # Data directory (L4): it or its nearest existing parent, on a local filesystem, writable.
    base = nearest_existing(host, data_dir)
    facts.data_fs = sh.capture(["stat", "-f", "-c", "%T", base]).out.strip()
    facts.data_writable = host.access(base, os.W_OK)
    facts.data_free = host.free_bytes(base)
    docker_base = nearest_existing(host, facts.docker_root)
    try:
        facts.docker_free = host.free_bytes(docker_base)
        facts.same_fs = host.device_of(docker_base) == host.device_of(base)
    except OSError as exc:
        ctx.log.line(f"facts: could not measure the Docker root {docker_base}: {exc}")

    for root in docs:
        if not host.exists(root):
            facts.docs[root] = "missing"
        elif not host.is_dir(root):
            facts.docs[root] = "notdir"
        elif not host.access(root, os.R_OK):
            facts.docs[root] = "noread"
        elif not host.access(root, os.W_OK):
            facts.docs[root] = "nowrite"
        else:
            facts.docs[root] = "ok"
    facts.wsl = "microsoft" in (host.read_text("/proc/sys/kernel/osrelease") or "").lower()
    # Docker's view, not this shell's: under WSL every session has its own mount namespace (design P1).
    mountinfo = host.read_text("/proc/1/mountinfo")
    for root in docs:
        found = mount_propagation(mountinfo, host.realpath(root)) if mountinfo and facts.docs.get(root) == "ok" else None
        if found:
            facts.docs_mount[root] = found
            ctx.log.line(f"facts: documents folder {root} mount={found[1]} propagation={found[0]} wsl={facts.wsl}")

    own_running = False
    if facts.docker_state == "ok":
        found = sh.capture(["docker", "ps", "-a", "-q", "--filter",
                            f"label=com.docker.compose.project={PROJECT}"])
        own_running = bool(found.out.strip())
        if not rerun and own_running:
            facts.collision = f"a Docker Compose project named {PROJECT} already exists"
    facts.home = host.home()
    if not rerun:
        unit_path = unit_file_path(host)
        if host.exists(unit_path):
            facts.collision = (facts.collision + "; " if facts.collision else "") + f"{unit_path} exists"
    for port in ports:
        if not host.port_free(port) and not (rerun and own_running):
            facts.ports_busy.append(port)

    facts.mem_total = meminfo_total(host.read_text("/proc/meminfo"))
    facts.kvm = host.is_char_device("/dev/kvm")
    collect_gpu_facts(ctx, facts, docker_ok=facts.docker_state == "ok")
    cards = host.glob("/dev/dri/card*")
    facts.video_gid = host.gid_of(cards[0]) if cards else None
    facts.render_gid = host.gid_of(facts.render_nodes[0]) if facts.render_nodes else None
    linger = sh.capture(["loginctl", "show-user", facts.user, "-p", "Linger"])
    facts.linger = linger.out.strip() == "Linger=yes" if linger.ok else None
    ctx.log.line(f"facts: os={facts.os_id} {facts.os_version} machine={facts.machine} docker={facts.docker_state} "
                 f"{facts.docker_version} manager_ok={facts.manager_docker_ok} data_fs={facts.data_fs} "
                 f"free={facts.data_free} kvm={facts.kvm} kfd={facts.kfd} render={len(facts.render_nodes)} "
                 f"amdgpu={facts.amdgpu_module} lspci_amd={facts.lspci_amd} "
                 f"nvidia_driver={facts.nvidia_driver} nvidia_wsl={facts.nvidia_wsl} "
                 f"nvidia_runtime={facts.nvidia_runtime} lspci_nvidia={facts.lspci_nvidia} linger={facts.linger} "
                 f"ports_busy={facts.ports_busy} mem_total={facts.mem_total}")
    return facts


NVIDIA_WSL_LIBRARY = "/usr/lib/wsl/lib/libnvidia-ml.so.1"
NVIDIA_WSL_SMI = "/usr/lib/wsl/lib/nvidia-smi"      # WSL puts it here, off the default PATH


def collect_gpu_facts(ctx: Ctx, facts: Facts, *, docker_ok: bool = True) -> None:
    """The GPU facts of both vendors, into ``facts``.  Shared by step 1 and by `update` / `status`, which
    re-ask the machine whether the GPU profile this install was set up for can still be honored (design 9).
    Nothing here writes to the machine.  ``docker_ok`` False means Docker could not be asked: the NVIDIA
    runtime is then unknown (None), and so is it when `docker info` fails or answers something unreadable.
    Unknown never qualifies a card and never counts as "gone" (15.0 review: a daemon down after a host or WSL
    restart was being diagnosed as a missing toolkit, with a repair that drops acceleration for good)."""
    host, sh = ctx.host, ctx.sh
    facts.kfd = host.exists("/dev/kfd")
    facts.render_nodes = host.glob("/dev/dri/renderD*")
    facts.amdgpu_module = host.exists("/sys/module/amdgpu")
    facts.lspci_amd = facts.lspci_nvidia = None
    if host.which("lspci"):
        listing = sh.capture(["lspci", "-nn"]).out.splitlines()
        facts.lspci_amd = any("[1002:" in row and re.search(r"VGA|Display|3D", row) for row in listing)
        facts.lspci_nvidia = any("[10de:" in row and re.search(r"VGA|Display|3D", row) for row in listing)
    # NVIDIA: the loaded kernel driver (plain Linux), or the Windows driver's /dev/dxg plus its libnvidia-ml
    # that WSL2 mounts (the WSL case, which has no /proc/driver/nvidia and an lspci that lists nothing).
    facts.nvidia_wsl = host.exists("/dev/dxg") and host.exists(NVIDIA_WSL_LIBRARY)
    facts.nvidia_driver = host.exists("/proc/driver/nvidia/version") or facts.nvidia_wsl
    facts.nvidia_driver_version = nvidia_driver_version(ctx, facts) if facts.nvidia_driver else None
    facts.nvidia_runtime = None
    if docker_ok:
        runtimes = sh.capture(["docker", "info", "--format", "{{json .Runtimes}}"])
        if not runtimes.ok or not runtimes.out.strip():
            ctx.log.line("facts: docker info did not answer with a runtime list; the NVIDIA runtime is unknown")
        else:
            try:
                listed = json.loads(runtimes.out)
                facts.nvidia_runtime = isinstance(listed, dict) and "nvidia" in listed
            except ValueError as exc:
                ctx.log.line(f"facts: could not read Docker's runtime list ({type(exc).__name__}: {exc}); "
                             "the NVIDIA runtime is unknown")
    ctx.log.line(f"facts: gpu: kfd={facts.kfd} render={len(facts.render_nodes)} amdgpu={facts.amdgpu_module} "
                 f"lspci_amd={facts.lspci_amd} nvidia_driver={facts.nvidia_driver} "
                 f"nvidia_driver_version={facts.nvidia_driver_version} nvidia_wsl={facts.nvidia_wsl} "
                 f"nvidia_runtime={facts.nvidia_runtime} lspci_nvidia={facts.lspci_nvidia} docker_asked={docker_ok}")


# CUDA 13 (what the NVIDIA image's onnxruntime-gpu and torch are built against) needs driver R580 or newer, on
# Linux and for the Windows driver WSL uses (DESIGN-NVIDIA-ACCELERATION 1.5).
NVIDIA_DRIVER_FLOOR = 580


def nvidia_driver_version(ctx: Ctx, facts: Facts) -> str | None:
    """The NVIDIA driver version as "<major>.<minor>", or None when it cannot be read (then it is not held against
    the card: verification still catches a driver that is too old, as `driver_too_old`).  Plain Linux: the loaded
    kernel module's version line in /proc/driver/nvidia/version.  WSL has no such file; the nvidia-smi WSL mounts
    reports the Windows driver's version in the same form."""
    text = ctx.host.read_text("/proc/driver/nvidia/version") or ""
    match = re.search(r"Kernel Module(?:\s+for\s+\S+)?\s+(\d+)\.(\d+)", text)
    if not match and facts.nvidia_wsl and ctx.host.exists(NVIDIA_WSL_SMI):
        answer = ctx.sh.capture([NVIDIA_WSL_SMI, "--query-gpu=driver_version", "--format=csv,noheader"])
        if answer.ok:
            match = re.match(r"\s*(\d+)\.(\d+)", answer.out)
    if not match:
        ctx.log.line("facts: NVIDIA driver version could not be read; it is not checked against R580 here")
        return None
    return f"{match.group(1)}.{match.group(2)}"


def nvidia_driver_too_old(facts: Facts) -> bool:
    """True only for a version that was read and is below the floor; unknown is not too old."""
    version = facts.nvidia_driver_version
    return version is not None and int(version.split(".", 1)[0]) < NVIDIA_DRIVER_FLOOR


AMD_DRIVER_MESSAGE = ("The amdgpu kernel driver is not loaded. Cognita does not install drivers. "
                      "See https://rocm.docs.amd.com/projects/install-on-linux/ .")
# 15.0 review: every NVIDIA note that sends the user off to fix a prerequisite ends with the command that turns
# acceleration on afterwards.  A plain rerun keeps the `cpu` this install saved (it asks nothing it already
# knows, C13), so "run the install again" alone would leave a user who did everything right on the CPU.
NVIDIA_ENABLE_AFTER = "Then run ./cognita install --acceleration nvidia."
NVIDIA_DRIVER_MESSAGE = ("The NVIDIA driver is not loaded. Cognita does not install drivers. "
                         "See https://www.nvidia.com/drivers . " + NVIDIA_ENABLE_AFTER)
NVIDIA_DRIVER_OLD_MESSAGE = ("The NVIDIA driver is older than R580, and CUDA 13 needs R580 or newer, so NVIDIA "
                             "acceleration is not offered. Cognita does not install drivers. See "
                             "https://www.nvidia.com/drivers . " + NVIDIA_ENABLE_AFTER)
NVIDIA_RUNTIME_MESSAGE = ("The NVIDIA Container Toolkit is not installed, or Docker does not know its `nvidia` "
                          "runtime. Cognita does not install it. See "
                          "https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html . "
                          + NVIDIA_ENABLE_AFTER)
NVIDIA_DOCKER_MESSAGE = ("An NVIDIA GPU was found, but Docker cannot be asked about its NVIDIA runtime yet, so "
                         "NVIDIA acceleration is not offered. Once Docker works for you, run ./cognita install "
                         "--acceleration nvidia to turn it on.")
KVM_MESSAGE = ("Workspace needs hardware virtualization (/dev/kvm), which this machine does not expose. "
               "Everything else will be installed. On a VM, turn on nested virtualization; on a PC, "
               "turn on VT-x/AMD-V in the firmware. Then run ./cognita install --workspace on.")
MEDIA_WARNING = ("Drives that are plugged in and out get a new mount point each time. Give the drive a "
                 "fixed mount point with an /etc/fstab entry using the nofail option, and add that "
                 "folder instead.")


def amd_qualifies(facts: Facts) -> bool:
    """Section 8: /dev/kfd, at least one render node, the amdgpu module, and (when lspci exists)
    an AMD display controller."""
    return (facts.kfd and bool(facts.render_nodes) and facts.amdgpu_module
            and facts.lspci_amd is not False)


def nvidia_qualifies(facts: Facts) -> bool:
    """DESIGN-NVIDIA-ACCELERATION 11: the driver is there and not known to be older than R580, Docker answered
    that it knows the `nvidia` runtime, and either this is WSL (where lspci lists nothing, so it is never
    consulted) or lspci does not say there is no NVIDIA display controller (True, or not installed).  The
    version floor was added in the 15.0 review: without it a plain-Linux R550 box was offered NVIDIA, pulled the
    ~5 GB image, and only then failed verification."""
    return (facts.nvidia_driver and facts.nvidia_runtime is True and not nvidia_driver_too_old(facts)
            and (facts.nvidia_wsl or facts.lspci_nvidia is not False))


# The GPU profiles an install can be set up for, with the name each is shown by.
GPU_LABELS = {"amd": "AMD", "nvidia": "NVIDIA"}
GPU_QUALIFIERS = {"amd": amd_qualifies, "nvidia": nvidia_qualifies}
ACCELERATION_PROFILES = ("cpu", *GPU_LABELS)    # what COGNITA_ACCELERATION and a release's `profile` may say


def gpu_still_honored(vendor: str, facts: Facts) -> bool:
    """`update` and an `install` rerun re-ask this before staging a saved GPU profile (design 9 item 2)."""
    return GPU_QUALIFIERS[vendor](facts)


def gpu_no_longer_honored_note(vendor: str, facts: Facts | None = None) -> str:
    """Why a saved GPU profile is dropped.  When the facts show the card is still there and only the NVIDIA driver
    fell below the floor, say THAT (15.0 review: `update` never runs `evaluate`, so the old-driver note was never
    shown and the user was told no GPU was found)."""
    if vendor == "nvidia" and facts is not None and facts.nvidia_driver and nvidia_driver_too_old(facts):
        return ("this install used NVIDIA acceleration, but the NVIDIA driver is now older than R580 (CUDA 13 "
                "needs R580 or newer), so it will run on the CPU. Update the driver "
                "(https://www.nvidia.com/drivers ), then run ./cognita install --acceleration nvidia.")
    return (f"this install used {GPU_LABELS[vendor]} acceleration, but no {GPU_LABELS[vendor]} GPU that Cognita "
            "can use was found now, so it will run on the CPU.")


def evaluate(ctx: Ctx, facts: Facts, *, published: dict[str, str], data_dir: str, docs: list[str],
             profile: str, workspace: bool, mcp_port: int = 0, admin_port: int = 0,
             displays: list[str] | None = None) -> Report:
    """Turn the facts into a list of failures (all of them, together) and the states step 2 acts on.

    ``displays`` is parallel to ``docs`` ("" = none): a documents folder that has a display is named by it
    in a failure, because that is the path the person knows it by (design 19.2, 19.9 item 14)."""
    report = Report()
    report.facts = facts
    problems, warnings, notes = report.problems, report.warnings, report.notes

    # 15.0.1 (Doug): no distribution gate.  Everything Cognita runs is inside its containers; the host supplies
    # Docker, systemd and an x86_64 CPU, and each of those is checked for what it is.  Until 15.0.0 anything but
    # Ubuntu 24.04 was refused unless --force, which blocked Debian, Fedora and kei itself (Ubuntu 26.04) for
    # no technical reason: the requirement it came from was a statement of what had been TESTED.
    ctx.log.line(f"check: os {facts.os_id or 'unknown'} {facts.os_version or '?'} on {facts.machine or '?'} "
                 f"(tested on Ubuntu 24.04; any systemd Linux with Docker is accepted) apt_family={facts.apt_family}")
    if facts.machine and facts.machine != "x86_64":
        problems.append(Finding("Processor", facts.machine,
                                "Cognita's container images are built for 64-bit Intel/AMD (x86_64) processors "
                                "only, so they cannot run here."))
    if not facts.systemd_pid1:
        problems.append(Finding("systemd", "PID 1 is not systemd",
                                "Cognita starts at boot through systemd; run it on a systemd system."))
    elif not facts.user_systemd:
        problems.append(Finding("systemd user manager", "`systemctl --user` did not answer",
                                "Log in through a normal session (SSH or console) and run "
                                "./cognita install again."))

    if facts.docker_state == "missing":
        report.needs_docker = True
    elif facts.docker_state == "denied":
        report.docker_denied = True
    elif facts.docker_state == "down":
        problems.append(Finding("Docker", "the Docker daemon is installed but not answering",
                                "Start it with: sudo systemctl enable --now docker"))
    else:
        major = re.match(r"(\d+)", facts.docker_version)
        if not major or int(major.group(1)) < 25:
            problems.append(Finding("Docker version", f"Docker {facts.docker_version or 'unknown'}",
                                    "Cognita needs Docker Engine 25 or newer. Upgrade it: "
                                    + docker_install_url(facts)))
        if not facts.compose_ok:
            # One hint for everyone: `docker-compose-plugin` exists only in Docker's own apt repository
            # (Ubuntu's own package is docker-compose-v2), so naming it sent some users to "Unable to locate".
            compose_fix = ("Install Docker's Compose plugin: https://docs.docker.com/compose/install/linux/ "
                           "(from Docker's apt repository the package is docker-compose-plugin; Ubuntu's own "
                           "package is docker-compose-v2)")
            problems.append(Finding("Docker Compose", "`docker compose version` failed", compose_fix))
        if facts.docker_selinux:
            # 15.0.1 review: a warning, not a stop. Nobody has run Cognita under SELinux-enforcing Docker, and
            # the likely failure (containers refused access to the documents and data folders) would otherwise
            # surface later as a bare permission error.
            warnings.append("Docker is running containers with SELinux enforcement. Cognita has not been tested "
                            "that way, and its containers may be refused access to your documents and data "
                            "folders. If the install fails with a permission error, use Docker Engine from "
                            "https://docs.docker.com/engine/install/ (it runs without SELinux enforcement by "
                            "default), or report it.")
            ctx.log.line("check: docker SecurityOptions include selinux; warned")
        if facts.docker_context == "desktop-linux":
            problems.append(Finding(
                "Docker Desktop", "the active Docker context is desktop-linux",
                "Cognita uses Docker Engine, not Docker Desktop. Run `docker context use default` "
                "or remove Docker Desktop, then install Docker Engine."))
        if not facts.manager_docker_ok:
            report.manager_blocked = True

    if facts.data_fs in NETWORK_FILESYSTEMS:
        problems.append(Finding("Data directory", f"{data_dir} is on a {facts.data_fs} filesystem",
                                "The index and database need a local filesystem. Choose one with "
                                "--data-dir PATH."))
    if not facts.data_writable:
        problems.append(Finding("Data directory", f"{data_dir} cannot be written by you",
                                "Choose a folder you own with --data-dir PATH."))

    for index, root in enumerate(docs):
        shown = displays[index] if displays and index < len(displays) else ""
        label = shown or root
        state = facts.docs.get(root, "ok")
        if shown:
            ctx.log.line(f"check: documents folder {root} is named {shown!r} in messages (state={state})")
        if state == "missing":
            # A folder with a display is a Windows path seen through a mount: a Linux `mkdir` or `chmod`
            # command would mean nothing to the person reading it, so the fix says what to check instead.
            problems.append(Finding("Documents folder", f"{label} does not exist",
                                    "Check that this folder exists and is reachable, or choose another."
                                    if shown else
                                    f"Create it (mkdir -p '{root}') or choose another with --documents PATH."))
        elif state == "notdir":
            problems.append(Finding("Documents folder", f"{label} is not a directory",
                                    "Choose a folder with --documents PATH."))
        elif state in ("noread", "nowrite"):
            problems.append(Finding("Documents folder",
                                    f"{label} is not {'readable' if state == 'noread' else 'writable'} by you",
                                    "Check that you can read and write this folder, or choose another."
                                    if shown else
                                    f"Fix its permissions (for example chmod u+rwx '{root}') or choose another."))
        mount = facts.docs_mount.get(root)
        if mount and mount[0] == "private":
            # 15.0.1: found running this installer by hand in a WSL distro (Maia, 2026-09-30); Compose stopped
            # with "not a shared or slave mount" at the first container start.  Windows Setup mounts its
            # folders `shared` from fstab, so only a hand-run WSL install reaches this.
            fix = (f"Docker can only share a folder into Cognita from a shared mount. Run "
                   f"sudo nsenter -t 1 -m -- mount --make-rshared {shlex.quote(mount[1])} "
                   f"and then ./cognita install again.")
            if facts.wsl:
                fix += (" WSL makes every mount private again when it restarts, so this must be repeated "
                        "after each restart; on Windows, Cognita Setup sets this up for you.")
            problems.append(Finding("Documents folder", f"{label} is on a private mount ({mount[1]})", fix))
        if root.startswith(("/media/", "/run/media/")):
            warnings.append(f"{root} looks like a removable drive. {MEDIA_WARNING}")

    for port in facts.ports_busy:
        flag = "admin-port" if port == admin_port else "mcp-port"
        problems.append(Finding("Port", f"127.0.0.1:{port} is already in use",
                                f"Free it, or pick another with --{flag} N "
                                f"(see what holds it: ss -ltnp 'sport = :{port}')."))

    if facts.mem_total is None:
        ctx.log.line("check: memory: /proc/meminfo unreadable; memory not checked")
    elif facts.mem_total < MIN_MEMORY_BYTES:
        problems.append(Finding(
            "Memory", f"{facts.mem_total / 1024**3:.1f} GB of memory",
            "Cognita needs at least 8 GB. The running service uses about 3 GB, and reading text "
            "from images (OCR) waits until another 3 GB is free, so with less it never runs. "
            "Give the machine (or VM) more memory."))

    need = disk_bytes(published, profile, workspace)
    if facts.same_fs and facts.docker_free is not None:
        combined = min(facts.data_free, facts.docker_free)
        if combined < need:
            problems.append(Finding("Disk space", f"{gb(combined)} free where the data directory and Docker "
                                    f"both live; about {gb(need)} is needed",
                                    "Free space, or pass --data-dir on a bigger disk. "
                                    "Smaller choices: --acceleration cpu --workspace off."))
    else:
        if facts.data_free < need:
            problems.append(Finding("Disk space", f"{gb(facts.data_free)} free at {data_dir}; about "
                                    f"{gb(need)} is needed", "Choose a bigger disk with --data-dir PATH."))
        if facts.docker_free is not None and facts.docker_free < need:
            problems.append(Finding("Disk space", f"{gb(facts.docker_free)} free where Docker stores images "
                                    f"({facts.docker_root}); about {gb(need)} is needed",
                                    "Free space there or move Docker's data-root (see Docker's docs)."))

    if facts.collision:
        problems.append(Finding("Existing Cognita", facts.collision,
                                "This install did not create it, so it will not touch it. Stop and remove "
                                "it yourself, or run ./cognita install --adopt ENV_FILE to take it over."))

    report.workspace_available = facts.kvm
    if not facts.kvm:
        notes.append(KVM_MESSAGE)
    report.amd_ok = amd_qualifies(facts)
    if not report.amd_ok and facts.lspci_amd and not facts.amdgpu_module:
        notes.append(AMD_DRIVER_MESSAGE)
    report.nvidia_ok = nvidia_qualifies(facts)
    if not report.nvidia_ok:
        card_seen = facts.nvidia_wsl or facts.lspci_nvidia is not False
        if facts.lspci_nvidia and not facts.nvidia_driver:
            notes.append(NVIDIA_DRIVER_MESSAGE)
        elif facts.nvidia_driver and card_seen and nvidia_driver_too_old(facts):
            notes.append(NVIDIA_DRIVER_OLD_MESSAGE)
        elif facts.nvidia_driver and card_seen and facts.nvidia_runtime is not True:
            # Only the runtime is missing.  Say so when Docker answered; when Docker could not be asked, or did
            # not answer (not installed, not usable by this user, daemon down), the runtime is unknown, and
            # saying "not installed" would be a guess.
            notes.append(NVIDIA_RUNTIME_MESSAGE if facts.nvidia_runtime is False else NVIDIA_DOCKER_MESSAGE)
            report.nvidia_runtime_unknown = facts.nvidia_runtime is None
    ctx.log.line(f"check: nvidia_ok={report.nvidia_ok} (driver={facts.nvidia_driver} "
                 f"version={facts.nvidia_driver_version} wsl={facts.nvidia_wsl} "
                 f"runtime={facts.nvidia_runtime} lspci={facts.lspci_nvidia}); amd_ok={report.amd_ok}")
    ctx.log.line(f"check: {len(problems)} problem(s), {len(warnings)} warning(s), {len(notes)} note(s), "
                 f"needs_docker={report.needs_docker} "
                 f"denied={report.docker_denied} manager_blocked={report.manager_blocked} "
                 f"workspace_available={report.workspace_available} amd_ok={report.amd_ok} "
                 f"nvidia_ok={report.nvidia_ok}")
    return report


def print_problems(ctx: Ctx, report: Report) -> None:
    ctx.ui.say("Cognita cannot be installed yet. Nothing was changed. These need fixing:")
    for number, item in enumerate(report.problems, 1):
        ctx.ui.say(f"  {number}. {item.name}: {item.saw}")
        ctx.ui.say(f"     Fix: {item.fix}")


# --------------------------------------------------------------------------
# Step 2: prerequisites (C4, L2).  Only Docker.  Proven: PROOF-LINUX-INSTALL.md, P3.
# --------------------------------------------------------------------------


def relogin_message(facts: Facts, *, added: bool) -> str:
    lead = ("Docker is installed. Your account was added to the docker group, which takes effect at "
            "your next login." if added else
            "Docker works in this shell but not for your systemd user manager, which is what starts "
            "Cognita. That is the state right after a group change.")
    if facts.linger:
        return (f"{lead} Because linger is already on for your account, your user manager keeps running "
                "when you log out, so logging out is not enough. Reboot, then run ./cognita install again.")
    return f"{lead} Log out fully and back in (or reboot), then run ./cognita install again."


def say_relogin(ctx: Ctx, facts: Facts, *, added: bool) -> None:
    """Tell the user to log out and back in.  The run then stops with EXIT_RELOGIN, which is neither a
    success nor a failure, so a progress file gets a `warning` line carrying the same text (design 19.1)."""
    message = relogin_message(facts, added=added)
    ctx.ui.say(message)
    ctx.ui.progress.warning("prerequisites", message)


def sudo_step(ctx: Ctx, reason: str, argv: list[str]) -> None:
    """Print the command and its one-line reason, THEN run it.  Callers ask for consent first."""
    printable = " ".join(shlex.quote(part) for part in argv)
    ctx.ui.say(f"  sudo {printable}")
    ctx.ui.say(f"      why: {reason}")
    ctx.log.line(f"sudo: {printable} (reason: {reason})")
    rc = ctx.sh.interactive(["sudo", *argv])
    if rc != 0:
        raise CliError(f"`sudo {argv[0]}` failed (exit {rc}) while: {reason}.",
                       hint="Fix that, then run ./cognita install again; finished steps are skipped.")


def apt_arch(machine: str) -> str:
    return {"x86_64": "amd64", "aarch64": "arm64"}.get(machine, "amd64")


def require_sudo(ctx: Ctx, facts: Facts, doing: str, manual_url: str) -> None:
    """Stop with the fix when there is no sudo (Debian installed with a root password has none), instead of a
    bare "`sudo apt-get` failed (exit 127)" from the first step (15.0.1 review)."""
    if ctx.host.which("sudo"):
        return
    ctx.log.line(f"sudo: not installed; {doing} cannot run its steps")
    raise CliError(f"{doing} needs sudo, and this system has no sudo command.",
                   hint=f"As root, install sudo and add your account to the sudo group (on Debian: apt-get install "
                        f"sudo; usermod -aG sudo {facts.user or 'YOUR-USER'}), log out and back in, then run the "
                        f"command again. Or install it yourself: {manual_url}")


def docker_install_url(facts: Facts) -> str:
    """Docker's own install page for this system: the distro's page on a Debian-family system, else the index."""
    family = facts.apt_family[0] if facts.apt_family else ""
    return f"https://docs.docker.com/engine/install/{family}/" if family else "https://docs.docker.com/engine/install/"


def install_docker(ctx: Ctx, a, facts: Facts) -> None:
    """Docker Engine from Docker's official apt repository (the proof's steps), then the group.

    Only on a Debian-family system (15.0.1): elsewhere there is no one package manager to drive, so the run
    stops with Docker's own instructions for that distribution and resumes once Docker is there."""
    if facts.apt_family is None:
        saw = f"{facts.os_id or 'this system'} {facts.os_version}".strip()
        ctx.log.line(f"docker: not installed and {saw} is not Debian-family; pointing at Docker's instructions")
        raise CliError(f"Docker Engine is not installed. Cognita runs in Docker containers, and on {saw} it "
                       "needs installing with your distribution's own steps.",
                       hint="Install Docker Engine with the Compose plugin: https://docs.docker.com/engine/install/ "
                            ", then run ./cognita install again.")
    family, codename = facts.apt_family
    # 15.0.1 review: both checks run BEFORE any sudo step, because a failure after the repository line is
    # written leaves /etc/apt/sources.list.d/docker.list behind and breaks every later `apt-get update`.
    require_sudo(ctx, facts, "Installing Docker", docker_install_url(facts))
    release_url = f"https://download.docker.com/linux/{family}/dists/{codename}/Release"
    status, _body = ctx.http("GET", release_url, timeout=20)
    ctx.log.line(f"docker: repository check {release_url} -> {status}")
    if status != 200:
        raise CliError(f"Docker's {family} repository has no packages for '{codename}'"
                       + (" (it did not answer)." if status is None else f" (HTTP {status})."),
                       hint=f"Install Docker Engine with the Compose plugin: {docker_install_url(facts)} , then run "
                            "./cognita install again.")
    ctx.ui.say("Docker Engine is not installed. Cognita runs in Docker containers.")
    ctx.ui.say("I will install it from Docker's own apt repository (download.docker.com):")
    ctx.ui.say("  docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin")
    ctx.ui.say("and add your account to the docker group. Each step below needs sudo and says why.")
    if not ctx.ui.confirm("Install Docker Engine now?", default=True, preset=_yes_no(a.install_docker),
                          flag="install-docker"):
        raise CliError("Docker Engine is required and was not installed.",
                       hint=f"Install it from {docker_install_url(facts)} then run ./cognita install again.")
    ctx.log.line(f"docker: installing from Docker's {family} apt repository, codename {codename}")
    arch = apt_arch(facts.machine)
    listing = ctx.host.write_temp(
        f"deb [arch={arch} signed-by=/etc/apt/keyrings/docker.asc] "
        f"https://download.docker.com/linux/{family} {codename} stable\n")
    sudo_step(ctx, "refresh the package lists", ["apt-get", "update"])
    sudo_step(ctx, "make sure curl and CA certificates exist to fetch Docker's signing key",
              ["apt-get", "install", "-y", "ca-certificates", "curl"])
    sudo_step(ctx, "create the keyring directory", ["install", "-m", "0755", "-d", "/etc/apt/keyrings"])
    sudo_step(ctx, "download Docker's package signing key",
              ["curl", "-fsSL", f"https://download.docker.com/linux/{family}/gpg", "-o", "/etc/apt/keyrings/docker.asc"])
    sudo_step(ctx, "let apt read the signing key", ["chmod", "a+r", "/etc/apt/keyrings/docker.asc"])
    sudo_step(ctx, "add Docker's apt repository",
              ["install", "-m", "0644", listing, "/etc/apt/sources.list.d/docker.list"])
    ctx.host.remove(listing)
    ctx.log.line(f"docker: installed the apt source from {listing} and removed that temporary file")
    sudo_step(ctx, "refresh the package lists with Docker's repository", ["apt-get", "update"])
    sudo_step(ctx, "install Docker Engine and the Compose plugin",
              ["apt-get", "install", "-y", "docker-ce", "docker-ce-cli", "containerd.io",
               "docker-buildx-plugin", "docker-compose-plugin"])


def add_to_docker_group(ctx: Ctx, facts: Facts) -> None:
    sudo_step(ctx, "let your account run Docker without sudo", ["usermod", "-aG", "docker", facts.user])


def prerequisites(ctx: Ctx, a, facts: Facts, report: Report) -> int | None:
    """Return None to carry on, or an exit code when the run must stop here."""
    if report.needs_docker:
        install_docker(ctx, a, facts)
        add_to_docker_group(ctx, facts)
        say_relogin(ctx, facts, added=True)
        return EXIT_RELOGIN
    if report.docker_denied:
        if facts.docker_group_member:
            ctx.log.line("docker: user is in the docker group but this session predates it")
            say_relogin(ctx, facts, added=True)
            return EXIT_RELOGIN
        ctx.ui.say("Your account is not in the docker group, so it cannot use Docker.")
        if not ctx.ui.confirm("Add your account to the docker group now?", default=True,
                              preset=_yes_no(a.install_docker), flag="install-docker"):
            raise CliError("Your account cannot use Docker.",
                           hint=f"Run: sudo usermod -aG docker {facts.user} , log out and back in, "
                                "then ./cognita install")
        add_to_docker_group(ctx, facts)
        say_relogin(ctx, facts, added=True)
        return EXIT_RELOGIN
    if report.manager_blocked:
        say_relogin(ctx, facts, added=False)
        return EXIT_RELOGIN
    return None


# --------------------------------------------------------------------------
# Answers: the questions only the user can answer (section 0), each with a flag (section 2.2)
# --------------------------------------------------------------------------


@dataclass
class Plan:
    documents: list[str]
    data_dir: str
    admin_user: str
    acceleration: str = "cpu"
    workspace: bool = False
    mcp_port: int = 8675
    admin_port: int = 8676
    admin_lan: bool = False
    tls_cert: str | None = None
    tls_key: str | None = None
    tailscale_name: str | None = None
    force: bool = False
    adopt: str | None = None
    displays: list[str] = field(default_factory=list)   # parallel to `documents`; "" = no display (design 19.2)
    command: str | None = None                          # COGNITA_COMMAND to record (design 19.6); None = as is


def clean_path(text: str) -> str:
    return posixpath.normpath(os.path.expanduser(text.strip()))


def config_admin(env: dict[str, str]) -> tuple[bool, str]:
    """(has an Admin password, current admin username) from config/cognita.yaml, when it exists.

    A password is an Argon2 `admin_password_hash` or a legacy `admin_password_sha256`: the app still
    logs in with either (admin_auth.py).  Counting only Argon2 made `--adopt` refuse kei's test target,
    whose config predates Argon2, with "no Admin password set" (P10a, 2026-09-29)."""
    root = env.get("COGNITA_CONFIG_ROOT")
    if not root:
        return False, ""
    text = Path(root, "cognita.yaml")
    if not text.is_file():
        return False, ""
    body = text.read_text(encoding="utf-8", errors="replace")
    has_hash = bool(re.search(r"^admin_password_hash:\s*[\"']?\$argon2", body, re.M)
                    or re.search(r"^admin_password_sha256:\s*[\"']?[0-9a-fA-F]{64}", body, re.M))
    match = re.search(r"^admin_username:\s*[\"']?([^\"'\r\n]+)", body, re.M)
    return has_hash, (match.group(1).strip() if match else "")


def resolve_plan(ctx: Ctx, a, env: dict[str, str]) -> Plan:
    """Flag > the env file (a rerun keeps every earlier answer, C13) > a question > the default.

    A flag that differs from the file is a change the user asked for: applied and logged."""
    log = ctx.log
    rerun = bool(env)
    roots = document_roots(env)
    displays = root_displays(env)
    if a.documents:
        wanted = clean_path(a.documents)
        if roots and roots[0] != wanted:
            log.line(f"plan: --documents changes the first documents folder {roots[0]} -> {wanted}")
            if displays[0] and a.documents_display is None:
                # Design 19.2: the old display names the OLD folder; keeping it would label the new one wrongly.
                log.line(f"plan: dropping the old display {displays[0]!r} of the first documents folder "
                         "because --documents changed it and no --documents-display was given")
                displays[0] = ""
        roots = [wanted, *roots[1:]]
        displays = displays[:len(roots)] + [""] * (len(roots) - len(displays))
    elif not roots:
        # The documents folder is the one answer only the user has (C2), so an
        # unattended run never guesses it: no default when --non-interactive.
        default_docs = None if ctx.ui.non_interactive else os.path.expanduser("~/Documents")
        roots = [clean_path(ctx.ui.ask("Where are your documents?", default=default_docs,
                                       flag="documents"))]
        displays = [""]
    else:
        log.line(f"plan: keeping documents folders from the env file: {roots}")
    if a.documents_display is not None:
        bad = display_problems(a.documents_display)
        if bad:
            raise CliError(f"--documents-display {a.documents_display!r} cannot be used: " + "; ".join(bad) + ".")
        if displays[0] != a.documents_display:
            log.line(f"plan: the display of the first documents folder is now {a.documents_display!r}")
        displays[0] = a.documents_display

    data_dir = data_dir_of(env)
    if a.data_dir:
        wanted_dir = clean_path(a.data_dir)
        if data_dir and wanted_dir != data_dir:
            raise CliError(
                f"This install keeps its data in {data_dir}; --data-dir {wanted_dir} would need it moved.",
                hint="Reinstalling cannot move data. Run ./cognita uninstall --delete-data first, or "
                     "move the folder yourself and edit nothing else.")
        data_dir = wanted_dir
    elif not data_dir:
        data_dir = clean_path(DEFAULT_DATA_DIR)

    has_hash, cfg_user = config_admin(env)
    if a.admin_user:
        admin_user = a.admin_user.strip()
        if has_hash and cfg_user and admin_user != cfg_user:
            log.line(f"plan: --admin-user changes the Admin username {cfg_user} -> {admin_user}")
    elif has_hash and cfg_user:
        admin_user = cfg_user
    else:
        admin_user = ctx.ui.ask("Admin username", default="admin", flag="admin-user") or "admin"

    def pick(flag_value, key: str, default: int) -> int:
        if flag_value is not None:
            return int(flag_value)
        return int(env[key]) if env.get(key, "").isdigit() else default

    lan = a.admin_lan if a.admin_lan is not None else env.get("COGNITA_ADMIN_BIND_ADDRESS") == "0.0.0.0"
    plan = Plan(
        documents=roots, data_dir=data_dir, admin_user=admin_user,
        mcp_port=pick(a.mcp_port, "COGNITA_MCP_HOST_PORT", 8675),
        admin_port=pick(a.admin_port, "COGNITA_ADMIN_HOST_PORT", 8676),
        admin_lan=lan, tls_cert=a.admin_tls_cert, tls_key=a.admin_tls_key,
        tailscale_name=a.tailscale_name, force=a.force, adopt=a.adopt, displays=displays,
        command=a.command_name)
    if a.command_name is not None:
        bad = command_problems(a.command_name)
        if bad:
            raise CliError(f"--command-name {a.command_name!r} cannot be used: " + "; ".join(bad) + ".")
    if bool(plan.tls_cert) != bool(plan.tls_key):
        raise CliError("--admin-tls-cert and --admin-tls-key go together.")
    if plan.mcp_port == plan.admin_port:
        raise CliError("The MCP port and the Admin port must differ.")
    for label, value in [("--data-dir", plan.data_dir), *[("--documents", r) for r in plan.documents]]:
        bad = ctx.check_paths(value)
        if bad:
            raise CliError(f"{label} {value!r} cannot be used: " + "; ".join(bad) + ".")
    problems = containment_problems(ctx.host, plan.data_dir, plan.documents)
    if problems:
        raise CliError(problems[0], hint="; ".join(problems[1:]) or None)
    if len(plan.documents) > MAX_DOCUMENT_ROOTS:
        raise CliError(f"At most {MAX_DOCUMENT_ROOTS} documents folders are supported.")
    log.line(f"plan: rerun={rerun} data_dir={plan.data_dir} documents={plan.documents} admin_user={plan.admin_user} "
             f"ports={plan.mcp_port}/{plan.admin_port} lan={plan.admin_lan} tls={bool(plan.tls_cert)} "
             f"displays={[d for d in plan.displays if d] or 'none'} command={plan.command or command_of(env)}")
    return plan


def read_password_file(ctx: Ctx, path: str) -> str:
    """The user-supplied file is read and left alone (C19).  Only the trailing newline is dropped."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise CliError(f"Cannot read --admin-password-file {path}: {exc.strerror or exc}") from exc
    text = text.rstrip("\r\n")
    if not text:
        raise CliError(f"--admin-password-file {path} is empty.")
    ctx.log.line(f"password: read from --admin-password-file {path} ({len(text)} characters, not logged)")
    return text


def check_password_flags(a) -> None:
    """Design 19.9 item 9: the two non-interactive password sources exclude each other, and standard
    input cannot also answer questions.  Called first in main (before anything runs) and again in
    get_password, so a command that reaches it another way is checked too."""
    stdin = getattr(a, "admin_password_stdin", False)
    if stdin and getattr(a, "admin_password_file", None):
        raise CliError("--admin-password-stdin and --admin-password-file cannot be used together.",
                       hint="Give the Admin password one way: on standard input or in a file.")
    if stdin and not getattr(a, "non_interactive", False):
        # The password is the first line of standard input; a question asked of the terminal would read
        # it instead.  A run that reads its password from stdin has nothing left to ask with.
        raise CliError("--admin-password-stdin uses standard input for the password, so nothing can be asked.",
                       hint="Add --non-interactive and give every answer as a flag.")


def read_password_stdin(ctx: Ctx) -> str:
    """Design 19.9 item 9: ONE line of standard input, decoded as UTF-8.  Only the trailing newline is
    removed (\\n, or \\r\\n as a Windows program writes it); nothing else is trimmed.  Empty is refused.
    Never echoed, never logged (the log gets the length), never put in argv or the environment.  Read
    once: a second call in the same process returns the first answer, because stdin cannot be read twice."""
    if ctx.stdin_password is not None:
        return ctx.stdin_password
    raw = ctx.stdin_line()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CliError("The password on standard input is not valid UTF-8.",
                       hint="Write it to --admin-password-stdin as one line of UTF-8 text.") from exc
    if text.endswith("\r\n"):
        text = text[:-2]
    elif text.endswith("\n"):
        text = text[:-1]
    if not text:
        raise CliError("--admin-password-stdin was given but standard input had no password line "
                       "(it was empty).",
                       hint="Write the Admin password as the first line on standard input.")
    ctx.stdin_password = text
    ctx.log.line(f"password: read from --admin-password-stdin ({len(text)} characters, not logged)")
    return text


def get_password(ctx: Ctx, a, *, prompt: str, twice: bool) -> str:
    """The Admin password.  From the file flag or the stdin flag, else typed (never echoed).  Held in
    memory only."""
    check_password_flags(a)
    if getattr(a, "admin_password_file", None):
        return read_password_file(ctx, a.admin_password_file)
    if getattr(a, "admin_password_stdin", False):
        return read_password_stdin(ctx)
    if ctx.ui.non_interactive:
        raise CliError("The Admin password is required and --non-interactive was given.",
                       hint="Pass --admin-password-file PATH, or --admin-password-stdin and the password on "
                            "standard input.")
    for _attempt in range(3):
        first = ctx.ui.secret(f"{prompt}: ")
        if not first:
            ctx.ui.say("The password cannot be empty.")
            continue
        if not twice or ctx.ui.secret("Type it again: ") == first:
            ctx.log.line("password: entered interactively (not logged)")
            return first
        ctx.ui.say("The two passwords did not match. Try again.")
    raise CliError("The passwords did not match three times.")


# --------------------------------------------------------------------------
# Step 5: layout, secrets, seed config (proof step 1)
# --------------------------------------------------------------------------

SEED_REGISTRY = "version: 1\nprojects: []\n"
SEED_CONNECTORS = "version: 1\nrevision: 0\nconnectors: []\n"
SEED_ACCELERATION = ("schema: 1\nrevision: 0\nknowledge:\n  gpu_enabled: false\n  gpu_device_ids: []\n"
                     "ocr:\n  device: cpu\n  gpu_device_ids: []\n")
OBSOLETE_AUTH_SEED = b"version: 1\nrevision: 0\nprojects: {}\n"


def seed_cognita_yaml(mcp_port: int) -> str:
    return ("mcp_host: 0.0.0.0\nmcp_port: 8675\nadmin_host: 0.0.0.0\nadmin_port: 8676\n"
            "admin_username: admin\nadmin_password_hash: \"\"\n"
            f"public_base_url: http://127.0.0.1:{mcp_port}\n"
            "registry_path: /app/config/registry.yaml\nconnectors_path: /app/config/connectors.yaml\n"
            "authentication_path: /app/config/authentication.yaml\n"
            "acceleration_path: /app/config/acceleration.yaml\ndata_root: /app/config/data\n"
            "watch_enabled: true\nlog_level: INFO\nlog_dir: /app/config/logs\n")


def _existing_regular(path: Path) -> bool:
    """True for a non-empty regular file; refuses a symlink or a directory in its place."""
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise CliError(f"{path} must be a regular file (it is a symlink or a directory).",
                       hint="Move it aside yourself; the installer never overwrites it.")
    return path.is_file() and path.stat().st_size > 0


def _validate_seed(path: Path) -> None:
    """Section 4 step 5: an existing non-empty seed is kept if it is plausible YAML text; an invalid
    one stops the install naming its path and is never overwritten.  The host has no YAML parser;
    the app validates the deep structure when it loads the file (a bad file fails loudly there)."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise CliError(f"{path} is not readable text ({type(exc).__name__}); it was preserved.",
                       hint="Fix or move it, then run ./cognita install again.") from exc
    if "\0" in text or not re.search(r"^[A-Za-z_][\w-]*:", text, re.M):
        raise CliError(f"{path} does not look like a Cognita configuration file; it was preserved.",
                       hint="Fix or move it, then run ./cognita install again.")


def ensure_layout(ctx: Ctx, env: dict[str, str], plan: Plan) -> None:
    """Directories 0700; secrets generated ONCE and never rotated (C13, C19); three seeds plus
    cognita.yaml; existing non-empty files validated and kept.  authentication.yaml is never seeded."""
    log = ctx.log
    for key in ("COGNITA_CONFIG_ROOT", "COGNITA_POSTGRES_DATA_ROOT", "COGNITA_WORKSPACE_DATA_ROOT",
                "COGNITA_TRANSFER_STAGING_ROOT", "COGNITA_SECRETS_ROOT", "COGNITA_MODEL_CACHE_ROOT",
                "COGNITA_TOOLBOX_IMAGE_CACHE_ROOT", "COGNITA_RELEASES_ROOT"):
        Path(env[key]).mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(env[key], 0o700)
    for extra in ("data", "logs"):
        Path(env["COGNITA_CONFIG_ROOT"], extra).mkdir(parents=True, exist_ok=True, mode=0o700)
    ensure_selftest_root(ctx, env)
    secrets_dir = Path(env["COGNITA_SECRETS_ROOT"])
    config_dir = Path(env["COGNITA_CONFIG_ROOT"])

    password_file, dsn_file = secrets_dir / "postgres.password", secrets_dir / "postgres.dsn"
    if not _existing_regular(password_file):
        if _existing_regular(dsn_file):
            match = re.match(r"postgresql://cognita:([^@]+)@postgres:5432/cognita", dsn_file.read_text().strip())
            if not match:
                raise CliError(f"{dsn_file} exists but {password_file} is missing and the DSN is not in the "
                               "expected shape; both were preserved.")
            atomic_write(password_file, match.group(1))
            log.line("layout: recovered postgres.password from the existing DSN (nothing rotated)")
        else:
            atomic_write(password_file, secrets.token_hex(32))
            log.line("layout: generated postgres.password (never printed)")
    password = password_file.read_text().strip()
    if not password or len(password) > 4096 or any(c.isspace() or c in ":/@?#%" for c in password):
        raise CliError(f"{password_file} holds an invalid PostgreSQL password; it was preserved.")
    expected_dsn = f"postgresql://cognita:{password}@postgres:5432/cognita"
    if not _existing_regular(dsn_file):
        atomic_write(dsn_file, expected_dsn)
        log.line("layout: wrote postgres.dsn to match the password")
    elif dsn_file.read_text().strip() != expected_dsn:
        raise CliError(f"{dsn_file} differs from {password_file}; both were preserved.",
                       hint="Fix one of them yourself; the installer never rotates credentials.")
    broker = secrets_dir / "broker.secret"
    if not _existing_regular(broker):
        atomic_write(broker, secrets.token_hex(48))
        log.line("layout: generated broker.secret (never printed)")
    value = broker.read_text().strip()
    if not value or len(value) > 4096 or any(c.isspace() for c in value):
        raise CliError(f"{broker} holds an invalid broker secret; it was preserved.")

    for name, source in (("admin_tls_certfile", plan.tls_cert), ("admin_tls_keyfile", plan.tls_key)):
        target = secrets_dir / name
        if source:
            try:
                atomic_write(target, Path(source).read_bytes(), 0o600)
            except OSError as exc:
                raise CliError(f"Cannot read the Admin TLS file {source}: {exc.strerror or exc}") from exc
            log.line(f"layout: copied {source} to {target} (0600)")
        elif not target.exists():
            atomic_write(target, "", 0o600)

    if env.get("COGNITA_WORKSPACE") == "on":
        marker = Path(env["COGNITA_WORKSPACE_DATA_ROOT"]) / ".cognita-12-workspaces.json"
        if not _existing_regular(marker):
            atomic_write(marker, json.dumps({"schema": 1, "root_id": str(uuid.uuid4()), "role": "workspaces"},
                                            separators=(",", ":")) + "\n")
            log.line("layout: wrote the Workspace capacity marker")
        else:
            try:
                parsed = json.loads(marker.read_text())
                assert parsed["schema"] == 1 and parsed["role"] == "workspaces"
                uuid.UUID(parsed["root_id"])
            except (ValueError, KeyError, AssertionError, TypeError) as exc:
                raise CliError(f"The Workspace capacity marker {marker} is invalid; it was preserved.") from exc

    for name, content in (("registry.yaml", SEED_REGISTRY), ("connectors.yaml", SEED_CONNECTORS),
                          ("acceleration.yaml", SEED_ACCELERATION),
                          ("cognita.yaml", seed_cognita_yaml(plan.mcp_port))):
        path = config_dir / name
        if _existing_regular(path):
            _validate_seed(path)
            log.line(f"layout: kept the existing {name}")
        else:
            atomic_write(path, content)
            log.line(f"layout: seeded {name}")
    auth = config_dir / "authentication.yaml"
    if auth.is_file() and auth.read_bytes() == OBSOLETE_AUTH_SEED:
        auth.unlink()
        log.line("layout: removed the obsolete empty authentication.yaml seed; the app initializes its own")
    if plan.tls_cert:
        set_yaml_keys(config_dir / "cognita.yaml", {
            "admin_tls_certfile": "/run/secrets/admin_tls_certfile",
            "admin_tls_keyfile": "/run/secrets/admin_tls_keyfile"})
        log.line("layout: cognita.yaml now names the Admin TLS files")


def ensure_selftest_root(ctx: Ctx, env: dict[str, str]) -> None:
    """The proof's Self-Test root is bind-mounted by the folders fragment, so it must exist, owned by
    the user, BEFORE the app starts: a missing bind source is created by Compose as an empty
    root-owned directory (PROOF-LINUX-INSTALL.md), which the proof could then not write into."""
    root = selftest_root(env)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    ctx.log.line(f"layout: Self-Test root {root} is in place")


def set_yaml_keys(path: Path, values: dict[str, str]) -> None:
    """Set top-level `key: value` lines in a flat YAML file, keeping every other line as it was."""
    lines = path.read_text(encoding="utf-8").splitlines()
    for key, value in values.items():
        row = f"{key}: {value}"
        for index, line in enumerate(lines):
            if line.startswith(f"{key}:"):
                lines[index] = row
                break
        else:
            lines.append(row)
    atomic_write(path, "\n".join(lines) + "\n")


# --------------------------------------------------------------------------
# Compose helpers, the Admin password (step 7), models (step 8), linger (step 9)
# --------------------------------------------------------------------------


def target_of(env: dict[str, str]):
    return rel_resolve_target(TARGET_NAME)


def compose_for(ctx: Ctx, target, directory: Path) -> list[str]:
    files = rel_staged_compose_files(directory, target.profile)
    return rel_compose_command(project=target.project, env_file=target.env_file, files=files)


def current_dir(env: dict[str, str]) -> Path:
    return local_root(env) / "current"


def release_facts(directory: Path) -> dict[str, str]:
    return rel_read_release_text(directory)


def set_admin_credentials(ctx: Ctx, target, directory: Path, username: str, password: str) -> None:
    """Proof step 3: set-admin-credentials.py --stdin-json inside the app image, JSON on stdin only."""
    compose = compose_for(ctx, target, directory)
    script = ctx.repo / "scripts" / "set-admin-credentials.py"
    command = [*compose, "run", "--pull", "never", "--rm", "--no-deps", "-T",
               "-v", f"{script}:{CREDENTIALS_SCRIPT_IN_CONTAINER}:ro",
               "--entrypoint", "python", "cognita", CREDENTIALS_SCRIPT_IN_CONTAINER,
               "--config", "/app/config/cognita.yaml", "--stdin-json"]
    payload = json.dumps({"username": username, "password": password})
    ctx.log.line(f"admin: setting the Admin credentials for user {username!r} (password via stdin, not logged)")
    ctx.sh.stream(command, stdin_text=payload, quiet=True, state="apply-failed")


def stage_with_progress(ctx: Ctx, target, published: dict[str, str], profile: str, workspace: bool) -> Path:
    """rel_stage_published, reporting the `images` stage (design 19.1).

    ``bytes_done`` is the compressed size of the images present so far, counting ones that were already
    there; ``bytes_total`` is what those images add up to (PostgreSQL included).  Docker's own layer
    progress is not parsed.  Without a progress file this is exactly rel_stage_published."""
    progress = ctx.ui.progress
    if not progress.enabled:
        return rel_stage_published(target, ctx.log)
    total = image_download_bytes(published, profile, workspace)
    done = 0
    progress.begin("images")
    progress.update("images", 0, total)

    def on_image(reference: str, size: int | None) -> None:
        nonlocal done
        # None = an image whose size the published file does not record: PostgreSQL, which is pinned by
        # digest in compose.yaml and is not ours to publish (section 6.1).
        done += POSTGRES_IMAGE_BYTES if size is None else size
        ctx.log.line(f"progress: image present {reference[:80]} +{size if size is not None else 'postgres'} "
                     f"-> {done}/{total} bytes")
        progress.update("images", done, total)

    directory = rel_stage_published(target, ctx.log, on_image)
    progress.update("images", done, total)
    return directory


def fetch_ocr_weights(ctx: Ctx, env: dict[str, str]) -> None:
    import ocr_weights  # Coder C's module; imported lazily so this file loads without it
    dest = Path(env["COGNITA_MODEL_CACHE_ROOT"]) / "easyocr"
    ctx.log.line(f"models: fetching the OCR weights into {dest}")
    progress = ctx.ui.progress
    if not progress.enabled:
        ocr_weights.fetch(dest, ctx.log)
        return
    progress.begin("ocr_weights")

    def on_bytes(done: int, _announced_total: int) -> None:
        # The archives' announced lengths grow as each download starts (0 when a server sends none), so
        # the total reported is the one estimate that does not jump: OCR_WEIGHTS_BYTES.
        progress.update("ocr_weights", done, OCR_WEIGHTS_BYTES)

    ocr_weights.fetch(dest, ctx.log, progress=on_bytes)


def fetch_ocr_weights_guarded(ctx: Ctx, env: dict[str, str], warnings: list[str]) -> None:
    """install, update and adopt all fetch the OCR weights the same way: a failure is a warning and OCR
    stays unavailable until a rerun, it never stops the run (final review, finding 8).  Since 14.2.0 the
    images no longer carry the weights, so this download is the only source of them; OCR reports
    "missing from the model cache" until a rerun succeeds.  (Superseded: in 14.1 the images still
    carried them, which is why a failure could be only a warning; it stays a warning because search
    and everything else work without OCR.)  ocr_weights.OcrWeightsError is a RuntimeError."""
    try:
        fetch_ocr_weights(ctx, env)
    except (ImportError, OSError, RuntimeError, ValueError) as exc:
        warnings.append(f"The OCR model files could not be downloaded ({exc}). OCR stays unavailable "
                        "until ./cognita install is rerun with a working connection.")
        ctx.ui.progress.warning("ocr_weights", warnings[-1])
        ctx.log.line(f"models: OCR weights failed: {type(exc).__name__}: {exc}")


def stop_unit_if_restaging(ctx: Ctx, env: dict[str, str], version: str) -> None:
    """Stop the unit BEFORE the release directory that `current` points to is replaced in place.

    Staging the same version again (install --workspace off, the CPU fallback) swaps the directory the
    running unit's ExecStop reads its Compose file set from.  If the unit were stopped only afterwards
    (apply_release does that), ExecStop would see the NEW file set and could miss containers the old one
    defined (final review, finding 5).  A different version is staged beside the running one and needs
    nothing here."""
    running = release_facts(current_dir(env)).get("version", "")
    if running != version:
        ctx.log.line(f"restage: current is {running or '(none)'}, staging {version}: a separate directory, "
                     "the unit keeps running")
        return
    ctx.log.line(f"restage: {version} is the running release and is about to be staged in place; "
                 f"stopping {UNIT} first so its ExecStop still sees the old Compose files")
    ctx.ui.say(f"Stopping Cognita before re-staging {version}...")
    result = ctx.sh.capture(["systemctl", "--user", "stop", UNIT], timeout=180)
    ctx.stopped_for_restage = True
    if not result.ok:
        ctx.log.line(f"restage: systemctl stop {UNIT} exited {result.rc}: {result.err.strip()}; continuing")


def stopped_note(ctx: Ctx) -> str | None:
    """The line a failure adds when this run stopped Cognita to re-stage and did not start it again."""
    if not ctx.stopped_for_restage:
        return None
    return "Cognita is stopped (this run stopped it to re-stage the same version). Start it with: ./cognita start"


def prefetch_models(ctx: Ctx, env: dict[str, str], target, directory: Path, warnings: list[str]) -> None:
    """Section 6.4.  The models are about 3.6 GB; show the cache growing so a slow step is not a hung one.
    A failed prefetch is a warning, not a stop: search works without the reranker (RRF order) and
    the embedder loads at first index."""
    cache = env["COGNITA_MODEL_CACHE_ROOT"]
    compose = compose_for(ctx, target, directory)
    command = [*compose, "run", "--pull", "never", "--rm", "--no-deps", "-T",
               "--entrypoint", "python", "cognita", "-m", "cognita.prefetch_models"]
    ctx.ui.say(f"Downloading the search models (about {gb(EXPECTED_MODEL_BYTES)}, once)...")
    ctx.ui.progress.begin("models")

    def tick() -> None:
        size = ctx.host.dir_size(cache)
        ctx.ui.say(f"  Search models: {gb(size)} of about {gb(EXPECTED_MODEL_BYTES)}")
        ctx.ui.progress.update("models", size, EXPECTED_MODEL_BYTES)    # design 19.1: every 10 s, from the cache size

    rc = ctx.sh.ticking(command, tick, PREFETCH_TICK_S)
    if rc != 0:
        warnings.append("The search models could not be downloaded now. Search will use plain ranking until "
                        "they are, and the embedder loads at the first index. Rerun ./cognita install "
                        "with a working connection to retry.")
        ctx.ui.progress.warning("models", warnings[-1])
        ctx.log.line(f"models: prefetch_models exited {rc}; continuing with a warning")
    else:
        ctx.log.line(f"models: prefetch_models finished; cache is {ctx.host.dir_size(cache)} bytes")


def ensure_linger(ctx: Ctx, values: dict[str, str], facts_linger: bool | None, user: str) -> None:
    """L7: so Cognita starts at boot before anyone logs in."""
    current = ctx.sh.capture(["loginctl", "show-user", user, "-p", "Linger"])
    if current.out.strip() == "Linger=yes":
        ctx.log.line("linger: already on; nothing to do")
        return
    ctx.ui.say("Turning on linger so Cognita starts at boot, before anyone logs in.")
    sudo_step(ctx, "start your user services at boot without a login", ["loginctl", "enable-linger", user])
    values["COGNITA_LINGER_SET_BY_INSTALLER"] = "1"
    write_env(ctx, values)


# --------------------------------------------------------------------------
# Acceleration (section 8)
# --------------------------------------------------------------------------


DRIVER_TOO_OLD_TEXT = "the NVIDIA driver is older than R580 (CUDA 13 needs R580 or newer)"


def gpu_reason(result: dict) -> str:
    runtimes = result.get("runtimes") or {}
    for key in ("embedding", "ocr"):
        reason = (runtimes.get(key) or {}).get("reason")
        if reason == "driver_too_old":
            # DESIGN-NVIDIA-ACCELERATION 7: a bounded category with a sentence of its own, not the token.
            return DRIVER_TOO_OLD_TEXT
        if reason:
            return f"{key}: {reason}"
    for card in result.get("cards") or []:
        if card.get("reason") == "driver_too_old":
            return DRIVER_TOO_OLD_TEXT
        if card.get("reason"):
            return f"{card.get('component', 'card')}: {card['reason']}"
    return f"verification state {result.get('state', 'unknown')}"


def set_gpu_policy(ctx: Ctx, admin: AdminClient, *, enabled: bool) -> dict:
    """PATCH /api/settings/gpu-acceleration.  Returns the status the endpoint answers with."""
    status = admin.request("GET", "/api/settings/gpu-acceleration")
    configured = status.get("configured") or {}
    knowledge = configured.get("knowledge") or {}
    ocr = configured.get("ocr") or {}
    if (bool(knowledge.get("gpu_enabled")) == enabled and (ocr.get("device") == "gpu") == enabled
            and not (status.get("restart") or {}).get("required")):
        ctx.log.line(f"gpu: policy already {'on' if enabled else 'off'}; not changing it")
        return status
    body = {"expected_revision": int(configured.get("revision", 0)), "idempotency_token": str(uuid.uuid4()),
            "knowledge": {"gpu_enabled": enabled, "gpu_device_ids": []},
            "ocr": {"device": "gpu" if enabled else "cpu", "gpu_device_ids": []}}
    result = admin.request("PATCH", "/api/settings/gpu-acceleration", body)
    ctx.log.line(f"gpu: policy set enabled={enabled} restart_required={(result.get('restart') or {}).get('required')}")
    return result


def restart_app(ctx: Ctx, target, directory: Path) -> None:
    compose = compose_for(ctx, target, directory)
    ctx.sh.stream([*compose, "up", "-d", "--no-deps", "--no-build", "--pull", "never", "--wait",
                   "--force-recreate", "cognita"], state="apply-failed")


def enable_and_verify_gpu(ctx: Ctx, env: dict[str, str], target, admin_user: str, password: str) -> tuple[bool, str]:
    """Turn the GPU on through Admin, restart if asked, and verify with real inference.
    Returns (verified, reason).  Never raises for a GPU problem: the GPU is an accelerator (C6)."""
    directory = current_dir(env)
    try:
        admin = ctx.admin_factory(int(env["COGNITA_ADMIN_HOST_PORT"]), https=admin_https(env))
        admin.login(admin_user, password)
        status = set_gpu_policy(ctx, admin, enabled=True)
        if (status.get("restart") or {}).get("required"):
            ctx.ui.say("Restarting Cognita so the acceleration setting takes effect...")
            restart_app(ctx, target, directory)
            rel_verify(ctx.repo, target, env["COGNITA_VERSION"], ctx.log)
            admin = ctx.admin_factory(int(env["COGNITA_ADMIN_HOST_PORT"]), https=admin_https(env))
            admin.login(admin_user, password)
        label = GPU_LABELS.get(env.get("COGNITA_ACCELERATION", ""), "AMD")
        ctx.ui.say(f"Checking the {label} card with real inference (up to two minutes)...")
        result = admin.request("POST", "/api/settings/gpu-acceleration/verify", {}, timeout=200)
    except (AdminError, release.ReleaseError, CliError) as exc:
        ctx.log.line(f"gpu: enabling or verifying failed: {type(exc).__name__}: {exc}")
        return False, str(exc).splitlines()[0]
    state = result.get("state")
    ctx.log.line(f"gpu: verification state={state} cards={len(result.get('cards') or [])} "
                 f"cleanup={result.get('cleanup')}")
    if state == "passed":
        return True, ""
    return False, gpu_reason(result)


def switch_to_cpu(ctx: Ctx, values: dict[str, str], admin_user: str, password: str, why: str) -> None:
    """The GPU did not work: say so, turn it off, and put the CPU image in its place (C6)."""
    vendor = values.get("COGNITA_ACCELERATION", "")
    label = GPU_LABELS.get(vendor, "AMD")
    ctx.ui.say(f"{label} acceleration did not verify: {why}. Switching to CPU.")
    ctx.log.line(f"gpu: falling back to CPU because: {why} (profile was {vendor or 'unrecorded'})")
    try:
        admin = ctx.admin_factory(int(values["COGNITA_ADMIN_HOST_PORT"]), https=admin_https(values))
        admin.login(admin_user, password)
        set_gpu_policy(ctx, admin, enabled=False)
    except (AdminError, CliError) as exc:
        ctx.log.line(f"gpu: could not turn the GPU policy off through Admin ({exc}); the CPU image ignores it")
    values["COGNITA_ACCELERATION"] = "cpu"
    write_env(ctx, values)
    target = target_of(values)
    # Re-staging the SAME version overwrites its release.txt, so the AMD images it recorded would be
    # named nowhere and uninstall could never remove them (P8 on kei, 2026-09-29: the 12.6 GB AMD app
    # image stayed behind).  Remember them now; drop the ones the CPU release no longer records below.
    # (15.0.0: the same holds for the NVIDIA app image; `gpu_refs` is the GPU release's refs, whichever vendor.)
    gpu_refs = release_image_refs(current_dir(values))
    stop_unit_if_restaging(ctx, values, values["COGNITA_VERSION"])   # the GPU release is what is running
    directory = rel_stage_published(target, ctx.log)
    rel_export_version(values["COGNITA_VERSION"], ctx.log)
    rel_apply(ctx.repo, target, directory, release_facts(directory).get("toolbox_version", ""), ctx.log)
    ctx.stopped_for_restage = False
    rel_verify(ctx.repo, target, values["COGNITA_VERSION"], ctx.log)
    kept = set(release_image_refs(current_dir(values)))
    for ref in gpu_refs:
        if ref not in kept:
            result = ctx.sh.capture(["docker", "image", "rm", ref])
            ctx.log.line(f"gpu: removed the unused {label} image reference {ref} -> exit {result.rc}")


def release_image_refs(directory: Path) -> list[str]:
    """The per-release tags and published references one release directory's release.txt records
    (not the Toolbox tag, which is shared machine-wide; see uninstall_image_refs)."""
    if not (directory / "release.txt").is_file():
        return []
    info = rel_read_release_text(directory)
    try:
        refs = list(rel_recorded_release_tags(info).values())
    except release.ReleaseError:
        refs = []
    return [ref for ref in dict.fromkeys(refs + info.get("published_refs", "").split()) if ref]


def admin_https(env: dict[str, str]) -> bool:
    """Whether the Admin serves HTTPS: the app turns TLS on only when config/cognita.yaml names BOTH
    `admin_tls_certfile` and `admin_tls_keyfile` (__main__.py).  The secret files alone prove nothing:
    compose.yaml mounts them on every install, and kei's test target has non-empty ones with TLS off,
    so reading the secrets made adoption's password pre-check speak TLS to plain HTTP, fail, and be
    skipped as "Admin did not answer" (P10a, 2026-09-29)."""
    root = env.get("COGNITA_CONFIG_ROOT")
    if not root:
        return False
    try:
        body = Path(root, "cognita.yaml").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    return all(re.search(rf"^{key}:\s*[\"']?[^\s\"'#]", body, re.M)
               for key in ("admin_tls_certfile", "admin_tls_keyfile"))


# --------------------------------------------------------------------------
# The proof (C11, section 7.5): a real operation, against a throwaway project, cleaned up
# --------------------------------------------------------------------------


class ProofFailed(CliError):
    pass


def admin_login(ctx: Ctx, admin: AdminClient, admin_user: str, password: str) -> None:
    """One Admin login.  Any failure to log in is the proof's failure: a 401 is the wrong password, and
    nothing else about Admin can be asked without a session.  Used by run_proof and, for a GPU (AMD or NVIDIA)
    install, once BEFORE any GPU work (final review, finding 3), so a wrong password on a rerun is
    reported as the password and never mistaken for a GPU that failed to verify."""
    try:
        admin.login(admin_user, password)
    except AdminError as exc:
        hint = " The Admin password is wrong." if exc.status == 401 else ""
        ctx.log.line(f"proof: Admin login failed: HTTP {exc.status}")
        raise ProofFailed(f"Admin login failed ({exc}).{hint}",
                          hint="Run ./cognita password to set it, then ./cognita install.") from exc


def _connector_matches(connector: dict, *, full: bool) -> bool:
    """Exactly what run_proof creates: the name only the proof writes, enabled, selected mode,
    Self-Test write, Workspace iff full."""
    return (connector.get("name") == PROOF_CONNECTOR_NAME
            and connector.get("enabled") is True and connector.get("project_mode") == "selected"
            and connector.get("project_access") == {SELFTEST_PROJECT: "write"}
            and bool(connector.get("workspace_enabled")) == full
            and connector.get("default_workspace_transfer", "deny") == ("allow" if full else "deny"))


def proof_skip_file(ctx: Ctx) -> Path | None:
    """Design 21.2: the file whose appearance means "stop the self-tests".  It is the progress file's
    name plus `.skip`, so no new flag is needed (Windows Setup's helper knows the progress path and
    creates it).  None when this run has no progress file: then there is nobody to press the button."""
    path = ctx.ui.progress.path
    return Path(str(path) + ".skip") if path is not None else None


def record_proof(ctx: Ctx, env: dict[str, str], outcome: str) -> None:
    """Design 21.2: remember in install.env whether the self-tests passed or were skipped.  The value goes
    into the dict every later write_env of this run uses too, or the next write would drop it."""
    env[PROOF_KEY] = outcome
    write_env(ctx, env)
    ctx.log.line(f"proof: {PROOF_KEY}={outcome} recorded in {ctx.env_path}")


def invalidate_proof(ctx: Ctx, env: dict[str, str]) -> None:
    """Forget a prior result before the selected release gets a new proof attempt."""
    if env.pop(PROOF_KEY, None) is not None:
        write_env(ctx, env)
        ctx.log.line(f"proof: prior {PROOF_KEY} cleared; the new proof is unverified until it passes or is skipped")


def run_proof(ctx: Ctx, env: dict[str, str], target, admin_user: str, password: str) -> str:
    """Section 7.5.  Deletes ONLY what it created; a leftover of an interrupted run is reused and
    counted as created; anything else that is in the way is left alone and stops the proof.

    Design 21.2: with a progress file, the skip file beside it (see ``proof_skip_file``) stops the
    self-tests.  A stop is not a failure: the cleanup below still runs, the install goes on with exit 0,
    and install.env records COGNITA_PROOF=skipped (a proof that passes records `passed`).  Returns the
    outcome, ``passed`` or ``skipped``; every failure raises ProofFailed, as before."""
    log = ctx.log
    invalidate_proof(ctx, env)
    ctx.ui.progress.begin("proof")
    skip_file = proof_skip_file(ctx)
    skip_seen = False

    def skip_requested() -> bool:
        nonlocal skip_seen
        if skip_file is None or not skip_file.exists():
            return False
        if not skip_seen:
            skip_seen = True
            log.line(f"proof: skip requested: {skip_file} exists; stopping the self-tests")
        return True

    stop_check = skip_requested if skip_file is not None else None
    log.line(f"proof: skip file {skip_file}" if skip_file is not None
             else "proof: no progress file, so the self-tests cannot be skipped")
    skipped = False
    directory = current_dir(env)
    facts = release_facts(directory)
    version = facts.get("version") or env.get("COGNITA_VERSION", "")
    full = facts.get("mode", "full") == "full"
    root = selftest_root(env)
    project_dir = root / SELFTEST_PROJECT
    log.line(f"proof: start version={version} mode={'full' if full else 'core'} selftest_root={root}")
    ctx.ui.say("Running self-tests to verify the installation: Admin login, a document indexed and found, OCR"
               + (", and a Workspace file write and job" if full else "") + "...")
    admin = ctx.admin_factory(int(env["COGNITA_ADMIN_HOST_PORT"]), https=admin_https(env))
    created_project = created_connector = False
    connector_id: str | None = None
    failure: Exception | None = None
    cleanup_failures: list[str] = []
    try:
        admin_login(ctx, admin, admin_user, password)
        log.line("proof: Admin login ok")

        projects = admin.request("GET", "/api/projects").get("projects", [])
        existing = next((p for p in projects if p.get("name") == SELFTEST_PROJECT), None)
        if existing is None:
            project_dir.mkdir(parents=True, exist_ok=True)
            admin.request("POST", "/api/projects", {"name": SELFTEST_PROJECT, "documents_dir": str(project_dir)})
            created_project = True
            log.line(f"proof: created project {SELFTEST_PROJECT} at {project_dir}")
        elif Path(str(existing.get("documents_dir", ""))) == project_dir:
            created_project = True
            log.line("proof: reusing a Self-Test project left by an interrupted run (will delete it)")
        else:
            log.line(f"proof: reusing the existing Self-Test project at {existing.get('documents_dir')} "
                     "(not created here, will not be deleted)")
        documents_dir = str(existing.get("documents_dir")) if existing else str(project_dir)

        listing = admin.request("GET", "/api/connectors")
        found = next((c for c in listing.get("connectors", []) if c.get("slug") == PROOF_CONNECTOR_SLUG), None)
        if found is None:
            made = admin.request("POST", "/api/connectors", {
                "expected_revision": int(listing.get("revision", 0)), "name": PROOF_CONNECTOR_NAME, "enabled": True,
                "project_mode": "selected", "default_access": None,
                "project_access": {SELFTEST_PROJECT: "write"}, "workspace_enabled": full,
                "default_workspace_transfer": "allow" if full else "deny"})
            created_connector = True
            connector_id = (made.get("connector") or {}).get("id")
            log.line(f"proof: created connector {PROOF_CONNECTOR_SLUG} id={connector_id}")
        elif _connector_matches(found, full=full):
            created_connector = True
            connector_id = found.get("id")
            log.line(f"proof: reusing an {PROOF_CONNECTOR_NAME!r} connector left by an interrupted run (will delete it)")
        else:
            raise ProofFailed(
                f"A connector with the slug {PROOF_CONNECTOR_SLUG!r} already exists and is not the one "
                "this proof creates, so it was left alone.",
                hint="Rename or delete it in Admin -> Connectors, then run ./cognita install again.")

        if skip_requested():         # pressed before the throwaway project's provisioning: do not start it
            raise release.Stopped("skip requested before provisioning")
        compose = compose_for(ctx, target, directory)
        script = ctx.repo / "scripts" / "provision_selftest.py"
        ctx.sh.stream([*compose, "run", "--pull", "never", "--rm", "--no-deps", "-T",
                       "-v", f"{script}:{PROVISION_SCRIPT_IN_CONTAINER}:ro", "--entrypoint", "python", "cognita",
                       PROVISION_SCRIPT_IN_CONTAINER, "--documents-dir", documents_dir,
                       "--data-dir", f"/app/config/data/{SELFTEST_PROJECT}",
                       "--registry", "/app/config/registry.yaml", "--defer-connector-check"],
                      state="verify-failed")
        with tempfile.TemporaryDirectory(prefix="cognita-proof-") as tmp:
            rel_qa(ctx.repo, target, version, Path(tmp), ctx.log, stop_check=stop_check)
        log.line("proof: the live self-test passed and the built-in key is a 401 again")
    except release.Stopped as exc:
        skipped = True
        log.line(f"proof: SKIPPED ({exc}); the throwaway project, connector and Workspace are cleaned up below")
    except (release.ReleaseError, AdminError) as exc:
        failure = ProofFailed(str(exc))
        log.line(f"proof: FAILED {type(exc).__name__}: {str(exc).splitlines()[0] if str(exc) else ''}")
    except ProofFailed as exc:
        failure = exc
        log.line(f"proof: FAILED {exc}")
    finally:
        cleanup_failures = _proof_cleanup(ctx, admin, root, created_connector, connector_id, created_project,
                                          workspace_on=full)
    if failure is not None:
        note = ("\nCleanup also failed: " + "; ".join(cleanup_failures)) if cleanup_failures else ""
        raise ProofFailed(f"{failure}{note}", hint=getattr(failure, "hint", None)) from failure
    if cleanup_failures:
        raise ProofFailed(f"The proof {'was skipped' if skipped else 'passed'} but its cleanup failed: "
                          + "; ".join(cleanup_failures),
                          hint="Delete the Self-Test project and the 'Install proof' connector in Admin, then rerun.")
    if skipped:
        ctx.ui.progress.finish_stage_as("proof", PROOF_SKIPPED_TITLE, PROOF_SKIPPED_TEXT)
        ctx.ui.say(PROOF_SKIPPED_TEXT)
        log.line("proof: skipped at the person's request; cleanup done; the install goes on")
        record_proof(ctx, env, PROOF_SKIPPED)
        return PROOF_SKIPPED
    ctx.ui.say("Proof passed; the throwaway Self-Test project and connector were removed.")
    record_proof(ctx, env, PROOF_PASSED)
    return PROOF_PASSED


def _delete_connector_workspaces(ctx: Ctx, admin: AdminClient, connector_id: str) -> list[str]:
    """Remove every Workspace owned by the proof's own connector.  Reports, never raises."""
    failures: list[str] = []
    try:
        listing = admin.request("GET", "/api/workspaces?limit=500")
    except (AdminError, OSError) as exc:
        ctx.log.line(f"proof: cleanup: listing Workspaces failed: {exc}")
        return [f"could not list the Install proof connector's Workspaces ({exc})"]
    for row in listing.get("workspaces", []):
        if str(row.get("connector_id")) != connector_id or row.get("state") in {"deleting", "deleted"}:
            continue
        workspace_id = str(row.get("workspace_id") or row.get("id"))
        try:
            admin.request("DELETE", f"/api/workspaces/{urllib.parse.quote(workspace_id)}",
                          {"expected_revision": int(row.get("revision", 0)), "confirm": True})
            ctx.log.line(f"proof: removed Workspace {workspace_id} of connector {connector_id}")
        except (AdminError, OSError) as exc:
            failures.append(f"could not remove the Install proof Workspace {workspace_id} ({exc})")
            ctx.log.line(f"proof: cleanup: Workspace {workspace_id} remove failed: {exc}")
    return failures


def _proof_cleanup(ctx: Ctx, admin: AdminClient, root: Path, created_connector: bool,
                   connector_id: str | None, created_project: bool, *, workspace_on: bool = True) -> list[str]:
    """Delete what the proof created: the connector's Workspace (full mode only; a core install has
    no Workspace service to ask), the connector, then the project WITH its data (otherwise its
    schema is left behind), then empty the Self-Test root.  Never raises: it reports."""
    failures: list[str] = []
    if created_connector:
        try:
            listing = admin.request("GET", "/api/connectors")
            target_id = connector_id or next((c.get("id") for c in listing.get("connectors", [])
                                              if c.get("slug") == PROOF_CONNECTOR_SLUG), None)
            if target_id:
                # The connector's Workspace goes first.  Deleting a connector leaves its
                # Workspace running, and every proof run creates a new connector, so on
                # the proof VM (2026-09-28) the third proof found two leaked Self-Test
                # Workspaces filling the running limit and failed with capacity_busy.
                if workspace_on:
                    failures.extend(_delete_connector_workspaces(ctx, admin, str(target_id)))
                admin.request("DELETE", f"/api/connectors/{urllib.parse.quote(str(target_id))}"
                                        f"?expected_revision={int(listing.get('revision', 0))}")
                ctx.log.line(f"proof: deleted connector {target_id}")
        except (AdminError, OSError) as exc:
            failures.append(f"could not delete the Install proof connector ({exc})")
            ctx.log.line(f"proof: cleanup: connector delete failed: {exc}")
    if created_project:
        try:
            admin.request("DELETE", f"/api/projects/{urllib.parse.quote(SELFTEST_PROJECT)}?deleteData=true")
            ctx.log.line(f"proof: deleted project {SELFTEST_PROJECT} with its data")
        except (AdminError, OSError) as exc:
            failures.append(f"could not delete the Self-Test project ({exc})")
            ctx.log.line(f"proof: cleanup: project delete failed: {exc}")
        try:
            if root.is_dir():
                for child in root.iterdir():
                    shutil.rmtree(child) if child.is_dir() and not child.is_symlink() else child.unlink()
                ctx.log.line(f"proof: emptied {root}")
        except OSError as exc:
            failures.append(f"could not empty {root} ({exc})")
            ctx.log.line(f"proof: cleanup: emptying {root} failed: {exc}")
    return failures


# --------------------------------------------------------------------------
# Remote access: Tailscale Funnel (section 10, C18, D3).  UNPROVEN until P5.
# --------------------------------------------------------------------------


def tailscale_install(ctx: Ctx, family: tuple[str, str] | None) -> None:
    """Tailscale from its official apt repository on a Debian-family system; elsewhere, its own instructions
    (15.0.1: this used to assume Ubuntu)."""
    if family is None:
        ctx.log.line("funnel: tailscale missing on a non-Debian-family system; pointing at Tailscale's instructions")
        raise CliError("Tailscale is not installed, and on this system it needs installing with your "
                       "distribution's own steps.",
                       hint="Install Tailscale: https://tailscale.com/download/linux , then run "
                            "./cognita remote-access.")
    repo, codename = family
    if not ctx.host.which("sudo"):
        ctx.log.line("funnel: sudo not installed; cannot install tailscale")
        raise CliError("Installing Tailscale needs sudo, and this system has no sudo command.",
                       hint="As root, install sudo and add your account to the sudo group, or install Tailscale "
                            "yourself: https://tailscale.com/download/linux , then run ./cognita remote-access.")
    ctx.log.line(f"funnel: installing tailscale from its {repo} apt repository, codename {codename}")
    base = f"https://pkgs.tailscale.com/stable/{repo}/{codename}"
    sudo_step(ctx, "download Tailscale's package signing key",
              ["curl", "-fsSL", f"{base}.noarmor.gpg", "-o", "/usr/share/keyrings/tailscale-archive-keyring.gpg"])
    sudo_step(ctx, "add Tailscale's apt repository",
              ["curl", "-fsSL", f"{base}.tailscale-keyring.list", "-o", "/etc/apt/sources.list.d/tailscale.list"])
    sudo_step(ctx, "refresh the package lists", ["apt-get", "update"])
    sudo_step(ctx, "install Tailscale", ["apt-get", "install", "-y", "tailscale"])


def tailscale_status(ctx: Ctx) -> dict:
    result = ctx.sh.capture(["tailscale", "status", "--json"])
    if not result.ok:
        return {}
    try:
        return json.loads(result.out)
    except ValueError:
        ctx.log.line("tailscale: status --json was not JSON")
        return {}


def default_tailscale_name(ctx: Ctx) -> str:
    cleaned = re.sub(r"[^a-z0-9-]+", "-", ctx.host.hostname().lower()).strip("-") or "box"
    return f"cognita-{cleaned}"


def remote_access(ctx: Ctx, a, env: dict[str, str], target, admin_user: str, password: str) -> str:
    """Returns the verified public address ("https://..."), or raises CliError.

    Installing Tailscale needs its own consent even after the user said yes to remote access: the
    offer says "set it up", the install says exactly what goes on the machine (C4)."""
    ui, log = ctx.ui, ctx.log
    external = getattr(a, "external_url", None)
    if external is not None:          # an empty --external-url is refused by the validator, never read as "not given"
        # Design 19.9 item 7: the address is given, so section 10 steps 2-5 (everything Tailscale) are skipped;
        # steps 6 and 7 (save it in Admin, then the /healthz and 401 checks) run exactly as they do for a Funnel.
        address = validate_external_url(external)
        log.line(f"remote-access: --external-url {address}; no Tailscale command is run")
        ui.say(f"Using {address} as Cognita's public address. Tailscale is not touched here.")
        return save_and_check_public_address(ctx, env, admin_user, password, address)
    ui.say("Web AI clients (claude.ai, ChatGPT) need a fixed public HTTPS address for Cognita's MCP port.")
    ui.say("Tailscale Funnel gives one for free. Only the MCP port is published, never Admin.")
    mcp_port = int(env["COGNITA_MCP_HOST_PORT"])
    if not ctx.host.which("tailscale"):
        family = apt_family(ctx.host.os_release())
        if family is None:
            tailscale_install(ctx, None)             # stops with Tailscale's own instructions
        ui.say("Tailscale is not installed. I will add its official apt repository "
               "(pkgs.tailscale.com) and install the `tailscale` package.")
        wanted = True if getattr(a, "remote_access", None) == "yes" else None
        if not ui.confirm("Install Tailscale now?", default=True, preset=wanted, flag="remote-access"):
            raise CliError("Tailscale is required for remote access and was not installed.")
        tailscale_install(ctx, family)
    status = tailscale_status(ctx)
    if status.get("BackendState") != "Running":
        name = getattr(a, "tailscale_name", None) or default_tailscale_name(ctx)
        ui.say("Signing this machine in to Tailscale. A sign-in link will appear; open it in a browser.")
        sudo_step(ctx, "join your tailnet (prints a sign-in link, returns when you have signed in)",
                  ["tailscale", "up", f"--hostname={name}"])
        status = tailscale_status(ctx)
    funnel = ["sudo", "tailscale", "funnel", "--bg", str(mcp_port)]
    ui.say(f"  sudo tailscale funnel --bg {mcp_port}")
    ui.say("      why: publish the MCP port at your Tailscale address")
    result = ctx.sh.capture(funnel, timeout=300)
    if not result.ok:
        ui.say((result.out + result.err).strip())
        ui.say("Tailscale needs you to enable HTTPS / Funnel for your tailnet. Open the link above, "
               "approve it, then come back.")
        ui.pause("Press Enter after you approved it. ")
        result = ctx.sh.capture(funnel, timeout=300)
        if not result.ok:
            raise CliError("`tailscale funnel` still failed after approval:\n" + (result.out + result.err).strip(),
                           hint="Fix that, then run ./cognita remote-access.")
    status = tailscale_status(ctx) or status
    dns = str((status.get("Self") or {}).get("DNSName", "")).rstrip(".")
    if not dns:
        raise CliError("Tailscale did not report this machine's address (Self.DNSName).",
                       hint="Check `tailscale status`, then run ./cognita remote-access.")
    address = f"https://{dns}"
    log.line(f"funnel: address {address}")
    return save_and_check_public_address(ctx, env, admin_user, password, address)


EXTERNAL_URL_MAX_CHARS = 2048         # the same bound Admin applies (public_url.MAX_URL_LENGTH)


def external_url_problems(text: str) -> list[str]:
    """Why an --external-url cannot be used; empty = fine (design 19.9 item 7).  The same rule Admin applies
    to a public base URL, plus the one the design adds: an address with no path.  HTTPS only, a host, no
    user name or password, no query or fragment, no whitespace or control character."""
    if not text or len(text) > EXTERNAL_URL_MAX_CHARS or text != text.strip():
        return [f"it must be 1 to {EXTERNAL_URL_MAX_CHARS} characters with no space at either end"]
    if any(ch.isspace() or ord(ch) < 0x20 or ord(ch) == 0x7F for ch in text):
        return ["it must not contain whitespace or control characters"]
    try:
        parsed = urllib.parse.urlsplit(text)
        hostname = parsed.hostname
        _ = parsed.port                       # a malformed or out-of-range port raises here
    except ValueError:
        return ["it is not a valid URL"]
    problems: list[str] = []
    if parsed.scheme.lower() != "https" or not hostname:
        problems.append("it must start with https:// and include a host (for example "
                        "https://cognita.example.com)")
    if parsed.username is not None or parsed.password is not None:
        problems.append("it must not include a user name or password")
    if "?" in text or "#" in text:
        problems.append("it must not include a query or a fragment")
    if parsed.path not in ("", "/"):
        problems.append("it must not include a path (only the address itself, with nothing after the host "
                        "and optional port)")
    return problems


def validate_external_url(text: str) -> str:
    """The URL Admin should save: as given, minus one trailing slash.  The value itself is never echoed in
    the refusal, because a mistyped one may carry a password (https://user:pass@host)."""
    problems = external_url_problems(text)
    if problems:
        raise CliError("--external-url cannot be used: " + "; ".join(problems) + ".",
                       hint="Give the https:// address people use to reach Cognita, for example "
                            "--external-url https://cognita.example.com")
    return text.rstrip("/")


def save_and_check_public_address(ctx: Ctx, env: dict[str, str], admin_user: str, password: str,
                                  address: str) -> str:
    """Section 10 steps 6 and 7: log in to Admin, save ``address`` as the public URL, then check that it
    answers as Cognita (/healthz 200, and 401 without a key on the MCP route)."""
    admin = ctx.admin_factory(int(env["COGNITA_ADMIN_HOST_PORT"]), https=admin_https(env))
    try:
        admin.login(admin_user, password)
        admin.request("PATCH", "/api/settings/public-base-url", {"public_base_url": address})
    except AdminError as exc:
        raise CliError(f"Admin refused the public address {address} ({exc}).",
                       hint="Check the Admin password and run ./cognita remote-access.") from exc
    ctx.ui.say(f"Saved {address} as Cognita's public address.")
    return _check_funnel(ctx, address)


def _yes_no(value: str | None) -> bool | None:
    return None if value is None else value == "yes"


def _check_funnel(ctx: Ctx, address: str) -> str:
    """Section 10 step 7: /healthz must answer, retried while the certificate and DNS provision."""
    url = f"{address}/healthz"
    for attempt in range(1, FUNNEL_ATTEMPTS + 1):
        status, body = ctx.http("GET", url)
        ok = False
        if status == 200:
            try:
                ok = json.loads(body).get("service") == "cognita"
            except (ValueError, AttributeError) as exc:
                ctx.log.line(f"funnel: /healthz answered 200 but not with Cognita's JSON ({type(exc).__name__})")
        ctx.ui.say(f"  Checking {url} (attempt {attempt}/{FUNNEL_ATTEMPTS}): "
                   f"{'HTTP ' + str(status) if status else 'no answer yet'}")
        ctx.log.line(f"funnel: healthz attempt={attempt} status={status} service_ok={ok}")
        if ok:
            break
        if attempt < FUNNEL_ATTEMPTS:
            ctx.sleep(FUNNEL_INTERVAL_S)
    else:
        raise CliError(
            f"{url} did not answer as Cognita after {FUNNEL_ATTEMPTS * FUNNEL_INTERVAL_S // 60} minutes. "
            "A 502 means Cognita is not running or Funnel points at the wrong port; a 404 or no answer "
            "usually means the certificate or DNS is still being set up.",
            hint="Wait a few minutes, then run ./cognita remote-access.")
    mcp = f"{address}/mcp/connectors/{SELFTEST_CONNECTOR_SLUG}/mcp"
    status, _body = ctx.http("POST", mcp, data=b'{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}',
                             headers={"Content-Type": "application/json",
                                      "Accept": "application/json, text/event-stream"})
    ctx.log.line(f"funnel: unauthenticated MCP POST -> {status}")
    if status == 401:
        ctx.ui.say("  MCP answers 401 without a key: that is correct. Your AI client signs in itself.")
    else:
        raise CliError(f"An unauthenticated MCP request to {mcp} returned {status}, expected 401. "
                       "A 502 means Cognita is not running or Funnel points at the wrong port.",
                       hint="Run ./cognita status, then ./cognita remote-access.")
    return address


def funnel_address(ctx: Ctx) -> str | None:
    """The address `tailscale funnel status --json` reports, when tailscale exists (section 9 status)."""
    if not ctx.host.which("tailscale"):
        return None
    result = ctx.sh.capture(["tailscale", "funnel", "status", "--json"])
    if not result.ok:
        return None
    try:
        data = json.loads(result.out)
    except ValueError:
        ctx.log.line("funnel: status --json was not JSON")
        return None
    for hostport, allowed in (data.get("AllowFunnel") or {}).items():
        if allowed:
            host = hostport.rsplit(":", 1)[0]
            return f"https://{host}"
    return None


# --------------------------------------------------------------------------
# Finish screen (section 12, C12)
# --------------------------------------------------------------------------


def acceleration_label(profile: str | None) -> str:
    """The words the finish screen and the plan use for a profile: "AMD GPU", "NVIDIA GPU" or "CPU"."""
    return f"{GPU_LABELS[profile]} GPU" if profile in GPU_LABELS else "CPU"


def finish_text(ctx: Ctx, env: dict[str, str], *, version: str, admin_user: str, public: str | None,
                warnings: list[str]) -> str:
    scheme = "https" if admin_https(env) else "http"
    admin_port, mcp_port = env["COGNITA_ADMIN_HOST_PORT"], env["COGNITA_MCP_HOST_PORT"]
    accel = acceleration_label(env.get("COGNITA_ACCELERATION"))
    workspace = "on" if env.get("COGNITA_WORKSPACE") == "on" else "off"
    # Design 19.2: a documents folder is shown the way a person knows it (the Windows path on a WSL install).
    roots = [shown or path for path, shown in zip(document_roots(env), root_displays(env))]
    cmd = command_of(env)          # design 19.6: the one name the user types, everywhere on this screen
    lines = [*warnings, *([""] if warnings else []),
             f"Cognita {version} is installed and working.", "",
             f"  Admin        {scheme}://127.0.0.1:{admin_port}   user: {admin_user}"]
    if env.get("COGNITA_ADMIN_BIND_ADDRESS") == "0.0.0.0":
        lines.append(f"  Admin (LAN)  {scheme}://{ctx.host.hostname()}:{admin_port}")
    lines += [f"  MCP (local)  http://127.0.0.1:{mcp_port}",
              f"  Public       {public or f'not set up — {cmd} remote-access'}",
              f"  Acceleration {accel}        Workspace: {workspace}",
              f"  Data         {data_dir_of(env)}",
              f"  Documents    {roots[0] if roots else ''}",
              *[f"               {root}" for root in roots[1:]],
              "",
              "To connect claude.ai or ChatGPT: open Admin → Connectors, create a connector,",
              "and use its Stable MCP URL.", "",
              f"  {cmd} status            {cmd} logs [app|workspace] [-f]",
              f"  {cmd} start|stop|restart",
              f"  {cmd} password          {cmd} add-folder PATH",
              f"  {cmd} update            {cmd} rollback",
              f"  {cmd} reset index       {cmd} uninstall",
              f"  {cmd} remote-access"]
    return "\n".join(lines)


# --------------------------------------------------------------------------
# install (section 4)
# --------------------------------------------------------------------------


def acceleration_and_proof(ctx: Ctx, values: dict[str, str], target, admin_user: str, password: str,
                           warnings: list[str]):
    """Step 11 (sections 7.5 and 8).  Returns (the target to use from now on, the proof failure or None).

    Only a GPU verification result may switch a GPU (AMD or NVIDIA) install to the CPU (C6).  The Admin login
    is made once, first: a 401 is the proof's password failure and leaves the install on its GPU profile
    untouched."""
    log = ctx.log
    profile = values["COGNITA_ACCELERATION"]
    if profile not in GPU_LABELS:
        try:
            run_proof(ctx, values, target, admin_user, password)
        except ProofFailed as exc:
            return target, exc
        return target, None
    label = GPU_LABELS[profile]
    ctx.ui.progress.accel_label = label
    ctx.ui.progress.begin("acceleration")
    try:
        admin_login(ctx, ctx.admin_factory(int(values["COGNITA_ADMIN_HOST_PORT"]),
                                           https=admin_https(values)), admin_user, password)
    except ProofFailed as exc:
        log.line("gpu: Admin login failed before any GPU work; the GPU was not touched and the "
                 f"install stays on {profile}")
        return target, exc
    log.line("gpu: Admin login ok; enabling and verifying the GPU")
    verified, why = enable_and_verify_gpu(ctx, values, target, admin_user, password)
    if verified:
        try:
            run_proof(ctx, values, target, admin_user, password)
        except ProofFailed as exc:
            verified, why = False, f"the proof failed on the GPU: {str(exc).splitlines()[0]}"
    if verified:
        return target, None
    warnings.append(f"{label} acceleration did not verify ({why}), so Cognita is using the CPU.")
    ctx.ui.progress.warning(
        "acceleration",
        warnings[-1],
        presentation_id="setup.acceleration.verification_failed",
        presentation_values={"vendor": label},
    )
    switch_to_cpu(ctx, values, admin_user, password, why)
    target = target_of(values)
    try:
        run_proof(ctx, values, target, admin_user, password)
    except ProofFailed as exc:
        return target, exc
    return target, None


def plan_text(plan: Plan, published: dict[str, str], *, profile: str, workspace: bool, linger_needed: bool,
              ) -> list[str]:
    download = download_bytes(published, profile, workspace)
    minutes = max(1, round(download * 8 / 100e6 / 60))
    lines = ["Here is what will happen:",
             f"  Data folder     {plan.data_dir}",
             "  Documents       " + ", ".join(
                 (plan.displays[index] if index < len(plan.displays) else "") or path
                 for index, path in enumerate(plan.documents)),
             f"  Ports           MCP {plan.mcp_port}, Admin {plan.admin_port} "
             f"({'all network interfaces' if plan.admin_lan else 'localhost only'})",
             f"  Acceleration    {acceleration_label(profile)}",
             f"  Workspace       {'on' if workspace else 'off'}",
             f"  Version         {published.get('version', '?')}",
             f"  Download        about {gb(download)} (roughly {minutes} min on a 100 Mbit/s line)",
             f"  Disk needed     about {gb(disk_bytes(published, profile, workspace))}"]
    if linger_needed:
        lines.append("  Needs sudo      turning on linger so Cognita starts at boot (one command, explained when it runs)")
    return lines


def build_env_values(ctx: Ctx, plan: Plan, facts: Facts, existing: dict[str, str],
                     published: dict[str, str]) -> dict[str, str]:
    """The env file (section 3): user choices from the plan, everything else derived from the machine."""
    values: dict[str, str] = dict(existing)
    # Roots already in the file win: an adopted install (section 13) keeps its own directories, and a
    # rerun must never repoint a data root.  Only a fresh install derives them from --data-dir.
    for key, value in derived_roots(plan.data_dir).items():
        values.setdefault(key, value)
    values.update({
        "COGNITA_RELEASE_TARGET": TARGET_NAME,
        "COGNITA_VERSION": published["version"],
        "COGNITA_ACCELERATION": plan.acceleration,
        "COGNITA_WORKSPACE": "on" if plan.workspace else "off",
        "COGNITA_SERVICE_UID": str(facts.uid), "COGNITA_SERVICE_GID": str(facts.gid),
        "COGNITA_MCP_HOST_PORT": str(plan.mcp_port), "COGNITA_MCP_BIND_ADDRESS": "127.0.0.1",
        "COGNITA_ADMIN_HOST_PORT": str(plan.admin_port),
        "COGNITA_ADMIN_BIND_ADDRESS": "0.0.0.0" if plan.admin_lan else "127.0.0.1",
    })
    for number in range(1, MAX_DOCUMENT_ROOTS + 1):
        key = root_key(number)
        if number <= len(plan.documents):
            values[key] = plan.documents[number - 1]
        else:
            values.pop(key, None)
        # Design 19.2: the display travels with the root it names; no display -> no key.
        shown = plan.displays[number - 1] if number <= len(plan.displays) else ""
        if number <= len(plan.documents) and shown:
            values[display_key(number)] = shown
        else:
            values.pop(display_key(number), None)
    if plan.command:
        values[COMMAND_KEY] = plan.command
    if plan.workspace and ctx.host.gid_of("/dev/kvm") is not None:
        values["COGNITA_KVM_GID"] = str(ctx.host.gid_of("/dev/kvm"))
    elif not plan.workspace:
        values.pop("COGNITA_KVM_GID", None)
    if facts.video_gid is not None:
        values["COGNITA_VIDEO_GID"] = str(facts.video_gid)
    if facts.render_gid is not None:
        values["COGNITA_RENDER_GID"] = str(facts.render_gid)
    return values


def gpu_notes_for(vendor: str, report: Report) -> str:
    """The step-1 driver / runtime notes that explain why ``vendor`` did not qualify, each with a leading space,
    for a refusal's hint (the AMD driver note is the one the 12.x refusal always carried)."""
    candidates = {"amd": (AMD_DRIVER_MESSAGE,),
                  "nvidia": (NVIDIA_DRIVER_MESSAGE, NVIDIA_DRIVER_OLD_MESSAGE, NVIDIA_RUNTIME_MESSAGE,
                             NVIDIA_DOCKER_MESSAGE)}[vendor]
    return "".join(f" {message}" for message in candidates if message in report.notes)


def gpu_fallback_reason_details(vendor: str, report: Report, *, no_image: bool) -> tuple[str, str]:
    """The plain reason a requested GPU vendor cannot be used here, built from the facts, for the one
    `acceleration` warning of --acceleration-fallback cpu (design 22.12 item 2).  In this order: the published
    release has no image for it; Linux cannot see an NVIDIA driver (under WSL that means WSL does not pass the
    card through); the NVIDIA driver is older than 580; Docker does not know the nvidia runtime; else it did
    not qualify.  Facts the Report does not carry (a hand-built one) can only reach the last."""
    label = GPU_LABELS[vendor]
    facts = report.facts
    if no_image:
        return f"the published release has no {label} image", "no_image"
    if vendor == "nvidia" and facts is not None:
        if not facts.nvidia_driver:
            return "Linux cannot see an NVIDIA driver", "driver_unavailable"
        if nvidia_driver_too_old(facts):
            return f"the NVIDIA driver is older than {NVIDIA_DRIVER_FLOOR}", "driver_too_old"
        if facts.nvidia_runtime is False:
            return "Docker does not know the nvidia runtime", "runtime_unavailable"
    return "it did not qualify", "not_qualified"


def gpu_fallback_reason(vendor: str, report: Report, *, no_image: bool) -> str:
    """Return the existing English detail used by console output and logs."""
    return gpu_fallback_reason_details(vendor, report, no_image=no_image)[0]


def emit_acceleration_warning(ctx: Ctx, vendor: str, message: str, *, why: str,
                              presentation_reason: str) -> None:
    """One `acceleration`-stage progress warning (design 22.12 items 2 and 9).  The stage title names the vendor,
    so the label is set before emitting; Setup adds its own fix line to a warning of this stage.  ``why`` is
    only for the log line, which carries what was emitted."""
    ctx.ui.progress.accel_label = GPU_LABELS[vendor]
    ctx.ui.progress.warning(
        "acceleration",
        message,
        presentation_id="setup.acceleration.fallback_unavailable",
        presentation_values={"vendor": GPU_LABELS[vendor], "reason": presentation_reason},
    )
    ctx.log.line(f"acceleration warning: vendor={vendor} why={why} message=[{message}]")


def _sentence(note: str) -> str:
    """A note that starts mid-sentence ("this install used ...") as the sentence a warning line shows."""
    return note[:1].upper() + note[1:]


def _or_list(items: tuple[str, ...] | list[str]) -> str:
    """("cpu", "amd", "nvidia") -> "cpu, amd or nvidia"; ("cpu", "amd") -> "cpu or amd"."""
    return f"{', '.join(items[:-1])} or {items[-1]}" if len(items) > 1 else "".join(items)


def choose_hardware(ctx: Ctx, a, report: Report, published: dict[str, str],
                    existing: dict[str, str]) -> tuple[str, bool]:
    """Acceleration (only when an AMD or NVIDIA card qualifies) and Workspace (only when /dev/kvm exists).

    A flag wins; else the answer already in the env file (a rerun asks nothing it already knows,
    C13); else the question.  An earlier choice the machine can no longer honor (the card or
    /dev/kvm is gone) degrades to CPU / off with a note instead of failing the rerun.

    15.0.0 (DESIGN-NVIDIA-ACCELERATION 11): the OFFERED set is every vendor that qualifies AND whose image the
    published release lists.  None offered: CPU, silently.  One: the question as it always was, naming that
    vendor and its size.  Two (a machine with both): one question listing cpu, amd and nvidia, default amd.

    15.1.0 (design 22.6 and 22.12 items 2, 8, 9): `--acceleration-fallback cpu` turns the two refusals of an
    explicit `--acceleration amd|nvidia` that cannot be honored into a CPU install with ONE `acceleration`
    warning (Setup's installer cannot know what Linux inside WSL will see).  Without it both raise as before."""
    ui, log = ctx.ui, ctx.log
    before_accel = existing.get("COGNITA_ACCELERATION")
    requested = a.acceleration
    fallback = getattr(a, "acceleration_fallback", None) == "cpu" and requested in GPU_LABELS
    if getattr(a, "acceleration_fallback", None) and not fallback:
        log.line(f"choice: --acceleration-fallback {a.acceleration_fallback} ignored: --acceleration is "
                 f"{requested or 'not given'}, and the fallback only applies to amd or nvidia")
    no_image: set[str] = set()
    fallback_warned = False
    qualified = {"amd": report.amd_ok, "nvidia": report.nvidia_ok}
    for vendor, label in GPU_LABELS.items():
        if qualified[vendor] and not published.get(f"image_ref_cognita_{vendor}"):
            # published-release.txt only lists a GPU image when `release.py publish --amd/--nvidia` pushed one.
            no_image.add(vendor)
            if requested == vendor and not fallback:
                raise CliError(f"--acceleration {vendor} was given but the published release has no {label} image.",
                               hint="Drop the flag to install for the CPU.")
            ui.say(f"Note: an {label} GPU was found, but the published release has no {label} image, so "
                   "Cognita will run on the CPU.")
            log.line(f"choice: acceleration cpu because published-release.txt has no image_ref_cognita_{vendor}")
            qualified[vendor] = False
    report.amd_ok, report.nvidia_ok = qualified["amd"], qualified["nvidia"]
    offered = [vendor for vendor in GPU_LABELS if qualified[vendor]]
    log.line(f"choice: offered GPU profiles={offered or 'none'} (amd qualifies+image={qualified['amd']}, "
             f"nvidia qualifies+image={qualified['nvidia']})")
    if (before_accel == "nvidia" and "nvidia" not in offered and report.nvidia_runtime_unknown
            and a.acceleration in (None, "", "nvidia")):
        # Only a run that would otherwise have to guess about NVIDIA stops: no flag (keep the saved choice?) or
        # an explicit nvidia.  An explicit cpu or amd does not depend on the unknown fact.
        # 15.0 review: unknown is never "gone".  The saved choice is nvidia and the card and driver are still
        # there, but Docker did not answer about its runtime.  Downgrading here would save `cpu` for good on a
        # guess; stop instead, as a daemon that does not answer at all already stops a rerun.
        log.line("choice: saved acceleration=nvidia, Docker did not answer about its runtime; stopping, not "
                 "downgrading")
        raise CliError("Docker did not answer when asked about its NVIDIA runtime, so this install cannot tell "
                       "whether its NVIDIA setup still works.",
                       hint="Check that Docker is running (docker info), then run the install again. To move this "
                            "install to the CPU instead, run ./cognita install --acceleration cpu.")
    if requested in GPU_LABELS and requested not in offered:
        wanted = requested
        if not fallback:
            raise CliError(f"--acceleration {wanted} was given but no {GPU_LABELS[wanted]} GPU that Cognita can use "
                           "was found.",
                           hint="Drop the flag to install for the CPU." + gpu_notes_for(wanted, report))
        # The ONE warning of this run for the fallback.  The progress message carries the plain reason built
        # from the facts; the screen also gets the step-1 driver / runtime notes, which stay out of the progress
        # line (they say "run ./cognita install ...", which is not what a Setup user does).
        label = GPU_LABELS[wanted]
        why, reason_code = gpu_fallback_reason_details(wanted, report, no_image=wanted in no_image)
        message = (f"{label} acceleration was chosen, but Cognita cannot use the {label} GPU here ({why}), "
                   "so it will use the CPU.")
        ui.say(message + gpu_notes_for(wanted, report))
        emit_acceleration_warning(ctx, wanted, message, why=why, presentation_reason=reason_code)
        log.line(f"choice: --acceleration {wanted} not usable here; --acceleration-fallback cpu -> cpu")
        requested = "cpu"
        fallback_warned = True
    allowed = ("cpu", *offered)
    if offered:
        if requested:
            accel = requested
        elif before_accel in allowed:
            accel = before_accel
            log.line(f"choice: keeping acceleration={accel} from the env file")
        elif len(offered) == 1:
            label = GPU_LABELS[offered[0]]
            ui.say(f"An {label} GPU that Cognita can use was found. The {label} image is about "
                   f"{gb(_size(published, f'size_cognita_{offered[0]}'))} to download.")
            accel = ui.ask(f"Acceleration: {_or_list(allowed)}", default=offered[0], flag="acceleration").lower()
        else:
            ui.say("GPUs that Cognita can use were found: " + " and ".join(GPU_LABELS[v] for v in offered)
                   + ". " + " ".join(f"The {GPU_LABELS[v]} image is about "
                                     f"{gb(_size(published, f'size_cognita_{v}'))} to download."
                                     for v in offered))
            accel = ui.ask(f"Acceleration: {_or_list(allowed)}", default="amd", flag="acceleration").lower()
        if accel not in allowed:
            raise CliError(f"Acceleration must be {_or_list(allowed)}, not {accel!r}.")
    else:
        accel = "cpu"
    if before_accel in GPU_LABELS and before_accel not in offered:
        # 15.1.0 review: with the facts, so a driver that fell below R580 is named instead of "no GPU found".
        note = gpu_no_longer_honored_note(before_accel, report.facts)
        ui.say(f"Note: {note}")
        log.line(f"choice: acceleration {before_accel} -> cpu because no qualifying "
                 f"{GPU_LABELS[before_accel]} card is present")
        if fallback_warned:
            # The fallback already said this run, once (design 22.12 item 8: exactly one warning per run).
            log.line("choice: saved-profile note not emitted as a second acceleration warning; the "
                     "--acceleration-fallback warning already covers it")
        elif a.acceleration == "cpu":
            # 15.1.0 review: CPU was asked for explicitly (Setup passes it when the user picked CPU, or when its
            # page already told them why NVIDIA is not offered). A warning telling them the GPU was dropped, with
            # Setup's "run Setup again and choose NVIDIA" under it, would describe a choice they made as a loss.
            log.line("choice: saved-profile note not emitted as an acceleration warning; --acceleration cpu "
                     "was given")
        else:
            emit_acceleration_warning(
                ctx, before_accel, _sentence(note), why="saved profile no longer honored",
                presentation_reason="saved_profile",
            )
    before_workspace = existing.get("COGNITA_WORKSPACE")
    if report.workspace_available:
        if a.workspace:
            workspace = a.workspace == "on"
        elif before_workspace in ("on", "off"):
            workspace = before_workspace == "on"
            log.line(f"choice: keeping workspace={before_workspace} from the env file")
        else:
            workspace = ui.confirm("Turn on Workspace (a sandboxed Linux scratchpad for the AI)?", default=True,
                                   flag="workspace", preference=True)
    else:
        if a.workspace == "on":
            ui.say("--workspace on was given, but " + KVM_MESSAGE)
        workspace = False
        if before_workspace == "on":
            log.line("choice: workspace on -> off because /dev/kvm is gone")
    log.line(f"choice: acceleration={accel} workspace={workspace}")
    return accel, workspace


def sizing_profile_for(ctx: Ctx, a, existing: dict[str, str], facts: Facts, published: dict[str, str]) -> str:
    """The profile the step-1 disk check sizes for.  A choice already made (the flag, else the env file) that the
    machine can still honor wins; `cpu` chosen means cpu.  Otherwise the choice is still open, and the check
    sizes the LARGEST image the user could still pick: among the vendors that qualify and whose image the
    published release lists, the one with the bigger recorded size (DESIGN-NVIDIA-ACCELERATION 11)."""
    chosen = a.acceleration or existing.get("COGNITA_ACCELERATION") or ""
    open_choices = [vendor for vendor in GPU_LABELS
                    if GPU_QUALIFIERS[vendor](facts) and published.get(f"image_ref_cognita_{vendor}")]
    fallback = getattr(a, "acceleration_fallback", None) == "cpu" and a.acceleration in GPU_LABELS
    if chosen == "cpu":
        result = "cpu"
    elif chosen in open_choices:
        result = chosen
    elif fallback:
        # 15.1.0 (design 22.6): the requested vendor is not an open choice and --acceleration-fallback cpu means
        # the install will be the CPU one, whatever the OTHER vendor could have offered.
        result = "cpu"
    elif open_choices:
        result = max(open_choices, key=lambda vendor: _size(published, f"size_cognita_{vendor}"))
    else:
        result = "cpu"
    ctx.log.line(f"sizing: chosen={chosen or 'none'} open_choices={open_choices or 'none'} "
                 f"fallback={'cpu' if fallback else 'none'} -> profile {result}")
    return result


def cmd_install(ctx: Ctx, a) -> int:
    log, ui = ctx.log, ctx.ui
    existing = read_env(ctx)
    if a.adopt:
        return cmd_adopt_install(ctx, a, existing)
    rerun = bool(existing)
    log.line(f"install: start rerun={rerun}")
    ui.command = a.command_name or command_of(existing)   # design 19.6: every message below names it
    ui.progress.begin("checks")                            # design 19.1: the plan questions belong to step 1
    published = read_published(ctx)
    plan = resolve_plan(ctx, a, existing)
    if plan.command:
        ui.command = plan.command

    # Step 1: read-only checks, all of them, listed together.
    ui.step("[1/12] Checking this machine (nothing is changed yet)", "checks")
    facts = collect_facts(ctx, plan.data_dir, plan.documents, [plan.mcp_port, plan.admin_port], rerun=rerun)
    # The disk check runs before the acceleration/Workspace questions, so it sizes the LARGEST choice
    # still open to the user; --acceleration cpu / --workspace off shrink it.
    sizing_profile = sizing_profile_for(ctx, a, existing, facts, published)
    sizing_workspace = {"on": True, "off": False}.get(a.workspace or existing.get("COGNITA_WORKSPACE") or "", True)
    report = evaluate(ctx, facts, published=published, data_dir=plan.data_dir, docs=plan.documents,
                      profile=sizing_profile,
                      workspace=facts.kvm and sizing_workspace,
                      mcp_port=plan.mcp_port, admin_port=plan.admin_port, displays=plan.displays)
    if report.problems:
        print_problems(ctx, report)
        raise CliError(f"{len(report.problems)} check(s) failed.", hint="Fix them and run ./cognita install again.")
    for note in report.notes:
        ui.say(f"Note: {note}")
    for warning in report.warnings:
        ui.progress.warning("checks", warning)

    # Step 2: prerequisites (only Docker), or stop for the group change.
    ui.step("[2/12] Prerequisites", "prerequisites")
    stop = prerequisites(ctx, a, facts, report)
    if stop is not None:
        return stop

    plan.acceleration, plan.workspace = choose_hardware(ctx, a, report, published, existing)
    if plan.admin_lan and not plan.tls_cert and not admin_https(existing):
        # --yes skips ONLY the final plan confirmation (section 2.2).  Sending the Admin password over
        # plain HTTP on the LAN is a security decision, so an unattended run has to say so with its
        # own flag (final review, finding 9); a non-interactive run without it fails naming the flag.
        if not ui.confirm("Admin will be on plain HTTP on your network. Anyone on it can read your Admin "
                          "password when you log in. Continue?", default=False,
                          preset=True if a.accept_plain_http_admin else None,
                          flag="accept-plain-http-admin"):
            raise CliError("Admin on the LAN without TLS was declined.",
                           hint="Pass --admin-tls-cert and --admin-tls-key, or drop --admin-lan.")

    has_hash, cfg_user = config_admin(existing)
    need_credentials = (not has_hash) or bool(cfg_user and plan.admin_user != cfg_user)
    password = get_password(
        ctx, a, twice=need_credentials,
        prompt=("Choose an Admin password" if need_credentials else "Admin password (to prove the install works)"))

    # Step 3: plan and confirm.
    ui.step("[3/12] Plan", "plan")
    linger_needed = facts.linger is not True
    for row in plan_text(plan, published, profile=plan.acceleration, workspace=plan.workspace,
                         linger_needed=linger_needed):
        ui.say(row)
    if not ui.confirm("Continue?", default=True, preset=True if a.yes else None, flag="yes"):
        raise CliError("Cancelled. Nothing was changed.")

    warnings = list(report.warnings)   # notes were already shown at step 1; these go above the finish screen
    values = build_env_values(ctx, plan, facts, existing, published)
    target = None
    # Step 4: the lock, for the rest of the run.
    ui.step("[4/12] Taking the install lock")     # no progress stage of its own: it is part of `layout`
    lock_path = Path(values["COGNITA_RELEASES_ROOT"]) / TARGET_NAME / ".lock"
    with rel_target_lock(lock_path, log):
        log.attach(local_root(values) / "logs" / f"install-{dt.datetime.now():%Y%m%d-%H%M%S}.log")
        ui.step("[5/12] Creating folders, secrets and settings", "layout")
        ensure_layout(ctx, values, plan)
        write_env(ctx, values)
        target = target_of(values)

        ui.step("[6/12] Downloading Cognita", "images")
        ui.say(f"Downloading Cognita images: about {gb(download_bytes(published, plan.acceleration, plan.workspace))}, "
               f"usually {max(1, round(download_bytes(published, plan.acceleration, plan.workspace) * 8 / 100e6 / 60))} "
               "minutes on a 100 Mbit/s line.")
        stop_unit_if_restaging(ctx, values, published["version"])
        directory = stage_with_progress(ctx, target, published, plan.acceleration, plan.workspace)
        release_info = release_facts(directory)
        toolbox_version = release_info.get("toolbox_version", "")

        ui.step("[7/12] Admin password", "password")
        if need_credentials:
            set_admin_credentials(ctx, target, directory, plan.admin_user, password)
        else:
            log.line("admin: an Admin password (Argon2 or legacy sha256) is already set; not setting it")

        ui.step("[8/12] Search and OCR models")     # two progress stages: ocr_weights, then models
        fetch_ocr_weights_guarded(ctx, values, warnings)
        prefetch_models(ctx, values, target, directory, warnings)

        ui.step("[9/12] Start at boot", "linger")
        ensure_linger(ctx, values, facts.linger, facts.user)

        ui.step("[10/12] Starting Cognita", "start")
        rel_export_version(values["COGNITA_VERSION"], log)
        rel_apply(ctx.repo, target, directory, toolbox_version, log)
        ctx.stopped_for_restage = False
        rel_enable_unit(target, log)
        rel_verify(ctx.repo, target, values["COGNITA_VERSION"], log)

        ui.step("[11/12] Acceleration and proof")   # progress stages: acceleration (a GPU profile only), then proof
        target, proof_error = acceleration_and_proof(ctx, values, target, plan.admin_user, password, warnings)
        if proof_error is not None:
            ui.say(f"The install is running, but the proof failed: {proof_error}")
            if proof_error.hint:
                ui.say(proof_error.hint)
            ui.say(f"Log: {log.path}")
            ui.say("Repair and prove again with: ./cognita install")
            ui.progress.fail(f"The install is running, but the proof failed: {proof_error}",
                             "Repair and prove again with: ./cognita install", default_stage="proof")
            return EXIT_FAILED

    # Step 12: remote access offer, then the finish screen.
    ui.step("[12/12] Remote access", "remote_access")
    public: str | None = None
    if ui.confirm("Set up remote access with Tailscale Funnel now?", default=False,
                  preset=_yes_no(a.remote_access), flag="remote-access", preference=True):
        try:
            public = remote_access(ctx, a, values, target, plan.admin_user, password)
        except CliError as exc:
            warnings.append(f"Remote access is not working yet: {exc} {exc.hint or ''}".strip())
            ui.progress.warning("remote_access", warnings[-1])
            log.line(f"funnel: setup did not complete: {exc}")
    ui.progress.begin("finish")
    ui.say("")
    ui.say(finish_text(ctx, values, version=values["COGNITA_VERSION"], admin_user=plan.admin_user,
                       public=public, warnings=warnings))
    ui.progress.end_stage()
    log.line(f"install: done version={values['COGNITA_VERSION']}")
    return EXIT_OK


# --------------------------------------------------------------------------
# --adopt (section 13, D4, C23)
# --------------------------------------------------------------------------


def cmd_adopt_install(ctx: Ctx, a, existing: dict[str, str]) -> int:
    """Move an existing release.py target (kei main, test) onto ./cognita, keeping its data.

    Reads the old env, writes install.env pointing at THE SAME directories, stages the published release,
    fetches the OCR weights and models and sets linger, and only THEN stops the old unit and project (its
    unit file and release directories stay, for rollback) and applies (design section 13 step 2; the
    old order stopped first, so a failed download left the machine with neither install running).  The
    layout step is skipped: an adopted install already has every file, and its DSN and secrets are the
    old install's, not ours to regenerate or validate."""
    log, ui = ctx.log, ctx.ui
    old_path = Path(os.path.expanduser(a.adopt))
    old = rel_read_env_file(old_path)
    if not old:
        raise CliError(f"--adopt {old_path} is missing or empty.")
    if existing:
        raise CliError(f"{ctx.env_path} already exists, so this machine already has a ./cognita install.",
                       hint="Adoption is for a machine that does not. Uninstall first, or drop --adopt.")
    known = next((t for t in rel_known_targets().values()
                  if os.path.realpath(str(t.env_file)) == os.path.realpath(str(old_path))), None)
    published = read_published(ctx)
    profile = a.acceleration or (known.profile if known else "cpu")
    values = {k: v for k, v in old.items() if k.startswith("COGNITA_")
              and k not in ("COGNITA_RELEASE_TARGET", "COGNITA_VERSION")}
    values.update({
        "COGNITA_RELEASE_TARGET": TARGET_NAME, "COGNITA_VERSION": published["version"],
        "COGNITA_ACCELERATION": profile,
        "COGNITA_WORKSPACE": "on" if old.get("COGNITA_KVM_GID") else "off",
        "COGNITA_RELEASES_ROOT": rel_releases_root_default(),
    })
    if a.command_name is not None:
        bad = command_problems(a.command_name)
        if bad:
            raise CliError(f"--command-name {a.command_name!r} cannot be used: " + "; ".join(bad) + ".")
        values[COMMAND_KEY] = a.command_name
    ui.command = command_of(values)          # design 19.6
    for key in ("COGNITA_CONFIG_ROOT", "COGNITA_SECRETS_ROOT", "COGNITA_POSTGRES_DATA_ROOT",
                "COGNITA_MODEL_CACHE_ROOT", "COGNITA_PROJECTS_ROOT"):
        if not values.get(key):
            raise CliError(f"{old_path} does not name {key}; it is not an installation env file I can adopt.")
    for label, value in [(k, v) for k, v in values.items() if k.endswith(("_ROOT",)) or "_ROOT_" in k]:
        bad = ctx.check_paths(value)
        if bad:
            raise CliError(f"{label}={value!r} in {old_path} cannot be used by ./cognita: " + "; ".join(bad) + ".")
    roots = document_roots(values)
    problems = containment_problems(ctx.host, data_dir_of(values) or values["COGNITA_CONFIG_ROOT"], roots)
    if problems and not a.force:
        raise CliError(problems[0], hint="Adoption keeps the old directories; pass --force to accept the overlap.")
    facts = collect_facts(ctx, data_dir_of(values), roots, [], rerun=True)
    report = evaluate(ctx, facts, published=published, data_dir=data_dir_of(values), docs=roots,
                      profile=profile, workspace=values["COGNITA_WORKSPACE"] == "on",
                      displays=root_displays(values))
    report.problems = [p for p in report.problems if p.name not in ("Port", "Existing Cognita")]
    if report.problems:
        print_problems(ctx, report)
        # --force covers only the folder overlap above (15.0.1: it no longer bypasses any check here).
        raise CliError(f"{len(report.problems)} check(s) failed.", hint="Fix them and rerun.")
    stop = prerequisites(ctx, a, facts, report)
    if stop is not None:
        return stop
    ui.say(f"Adopting {old_path}: same data folders, documents and ports; Cognita {published['version']} "
           f"({profile}, Workspace {values['COGNITA_WORKSPACE']}).")
    # --adopt is the explicit request to replace the old service, so there is no second question and
    # --yes is not involved (final review, finding 9): --yes only skips the final plan confirmation.
    ui.say("Everything is downloaded and checked first; the old service is stopped only after that succeeds.")
    log.line(f"adopt: stopping the old service after staging is implied by --adopt (old unit "
             f"{known.unit if known else 'not recognized'})")
    has_hash, admin_user = config_admin(values)
    if not has_hash:
        raise CliError("The old installation has no Admin password set, so there is no login to prove with.",
                       hint="Set one on the old install first, or run a fresh ./cognita install.")
    password = get_password(ctx, a, twice=False, prompt="Admin password (to prove the install works)")
    # The adopted install keeps the old config, so the old service's Admin (same port, same hash)
    # checks the password NOW, while nothing has changed.  Otherwise a typo would only show at the
    # proof, after production was already stopped.  An old Admin that does not answer is not a
    # refusal: the proof checks the password later, with the rollback command in hand.
    try:
        ctx.admin_factory(int(values["COGNITA_ADMIN_HOST_PORT"]), https=admin_https(values)).login(
            admin_user, password)
        log.line("adopt: Admin password checked against the running old service")
    except AdminError as exc:
        if exc.status == 0:        # nothing answered on the old Admin port: checked at the proof instead
            log.line(f"adopt: old Admin did not answer ({exc}); the proof checks the password")
        else:
            log.line(f"adopt: old Admin refused the login: HTTP {exc.status}")
            raise CliError(f"The old install's Admin refused the login (HTTP {exc.status})"
                           f"{': the password is wrong' if exc.status == 401 else ''}. Nothing was changed.",
                           hint="Rerun with the old install's Admin password.") from exc
    warnings: list[str] = []
    rollback = adopt_rollback_command(known)
    lock_path = local_root(values) / ".lock"
    with rel_target_lock(lock_path, log):
        log.attach(local_root(values) / "logs" / f"install-{dt.datetime.now():%Y%m%d-%H%M%S}.log")
        write_env(ctx, values)
        # Everything that can fail runs BEFORE the old stack stops (design section 13 step 2, final
        # review finding 1): the target, the long image pull, the OCR weights, the model download and
        # linger.  A failure here has changed nothing that matters, so the env file this run wrote is
        # removed again and the same --adopt command can simply be rerun.
        try:
            ensure_selftest_root(ctx, values)
            target = target_of(values)
            directory = stage_with_progress(ctx, target, published, profile, values["COGNITA_WORKSPACE"] == "on")
            toolbox_version = release_facts(directory).get("toolbox_version", "")
            fetch_ocr_weights_guarded(ctx, values, warnings)
            prefetch_models(ctx, values, target, directory, warnings)
            ui.progress.begin("linger")
            ensure_linger(ctx, values, facts.linger, facts.user)
        except BaseException as exc:
            log.line(f"adopt: failed BEFORE the old service was touched ({type(exc).__name__}: {exc}); "
                     f"removing {ctx.env_path} again so the adoption can be retried")
            try:
                ctx.env_path.unlink(missing_ok=True)
            except OSError as unlink_error:
                log.line(f"adopt: could not remove {ctx.env_path}: {unlink_error}")
            raise
        # From the stop on ANY failure leaves the machine between the two installs, so every one
        # prints the exact way back, not the generic "run it again".
        try:
            if known is not None:
                try:
                    stop_old_service(ctx, known)
                except BaseException:
                    # Nothing new was started, so the env file this run wrote goes again and the same
                    # --adopt command can be rerun once the old service is back (the rollback line
                    # below re-enables it, which the stop may have half undone).
                    ctx.env_path.unlink(missing_ok=True)
                    log.line(f"adopt: stopping the old service failed; removed {ctx.env_path} so the adoption can be retried")
                    raise
            else:
                log.line("adopt: the old env file is not a known release.py target; stop its unit yourself if it is running")
                warnings.append("The old service was not recognized, so it was not stopped. Stop it yourself if the "
                                "ports collide.")
            ui.progress.begin("start")
            rel_export_version(values["COGNITA_VERSION"], log)
            rel_apply(ctx.repo, target, directory, toolbox_version, log)
            rel_enable_unit(target, log)
            rel_verify(ctx.repo, target, values["COGNITA_VERSION"], log)
            run_proof(ctx, values, target, admin_user, password)
        except ProofFailed as exc:
            ui.say(f"The adopted install is running, but the proof failed: {exc}")
            ui.say(f"Rollback: {rollback}")
            ui.progress.fail(f"The adopted install is running, but the proof failed: {exc}", f"Rollback: {rollback}",
                             default_stage="proof")
            log.line(f"adopt: proof failed; rollback command printed: {rollback}")
            return EXIT_FAILED
        except BaseException as exc:
            ui.say(f"The adoption stopped part way ({type(exc).__name__}). To go back to the old service, run: "
                   f"{rollback}")
            # main() writes the `failed` line for the exception itself; this warning carries the way back.
            ui.progress.warning(ui.progress.stage or "start",
                                f"The adoption stopped part way. To go back to the old service, run: {rollback}")
            log.line(f"adopt: failed after the old service was stopped ({type(exc).__name__}: {exc}); "
                     f"rollback command printed: {rollback}")
            raise
    ui.progress.begin("finish")
    ui.say("")
    ui.say(finish_text(ctx, values, version=values["COGNITA_VERSION"], admin_user=admin_user, public=None,
                       warnings=warnings))
    ui.progress.end_stage()
    return EXIT_OK


def adopt_rollback_command(known) -> str:
    """Design section 13: the exact way back to the old service.  `;`, not `&&`: this is printed for
    ANY failure after the stop, including ones before cognita.service was ever installed, and then
    the first half fails ("unit does not exist") and `&&` would skip re-enabling the old service
    (final review of the installer's second half, 2026-09-29)."""
    return ("systemctl --user disable --now cognita.service; systemctl --user enable --now "
            + (known.unit if known else "<old unit>"))


def stop_old_service(ctx: Ctx, known) -> None:
    """Stop and disable the adopted target's unit, then take its Compose project down.  Its unit file and
    release directories stay, for rollback.  A failing disable is an error: continuing would start the
    new stack on the old one's ports."""
    old_dir = Path(rel_releases_root_default()) / known.name / "current"
    rc = ctx.sh.interactive(["systemctl", "--user", "disable", "--now", known.unit])
    if rc != 0:
        raise CliError(f"Could not stop and disable {known.unit} (exit {rc}), so the new install was not started.",
                       hint=f"Check it with: systemctl --user status {known.unit}")
    if old_dir.exists():
        compose = rel_compose_command(project=known.project, env_file=known.env_file,
                                      files=rel_staged_compose_files(old_dir, known.profile))
        ctx.sh.stream([*compose, "down"], check=False, state="apply-failed")
    ctx.log.line(f"adopt: stopped and disabled {known.unit}; its unit file and releases stay for rollback")


# --------------------------------------------------------------------------
# Everyday commands (sections 9, 7.1, 10)
# --------------------------------------------------------------------------


def require_install(ctx: Ctx) -> dict[str, str]:
    env = read_env(ctx)
    if not env.get("COGNITA_RELEASES_ROOT"):
        raise CliError("Cognita is not installed on this machine.", hint="Run: ./cognita install")
    return env


def open_log(ctx: Ctx, env: dict[str, str], command: str) -> None:
    ctx.log.attach(local_root(env) / "logs" / f"{command}-{dt.datetime.now():%Y%m%d-%H%M%S}.log")


LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost", "test"}    # the app's own list (public_url.py)


def _is_loopback_url(url: str) -> bool:
    try:
        host = (urllib.parse.urlsplit(url).hostname or "").casefold().rstrip(".")
    except ValueError:
        return True          # not a URL anyone could reach: counts as not set
    return host in LOOPBACK_HOSTS or not host


def public_url_of(ctx: Ctx, env: dict[str, str]) -> str | None:
    """The public address Admin saved (design 19.6), read from disk with no login.

    First `<config>/data/public-base-url.json` key `public_base_url` (what Admin's public-address
    setting writes), then `public_base_url` in `<config>/cognita.yaml` (the only key read from it).  A
    loopback URL counts as not set, so the seed's http://127.0.0.1:<port> gives None.  Anything
    unreadable is logged and skipped: status never fails on this."""
    config = env.get("COGNITA_CONFIG_ROOT")
    if not config:
        return None
    candidates: list[tuple[str, str]] = []
    saved = Path(config, "data", "public-base-url.json")
    try:
        if saved.is_file():
            value = json.loads(saved.read_text(encoding="utf-8")).get("public_base_url")
            if isinstance(value, str) and value:
                candidates.append((str(saved), value.strip()))
    except (OSError, ValueError, AttributeError) as exc:
        ctx.log.line(f"status: could not read {saved}: {type(exc).__name__}: {exc}")
    yaml_file = Path(config, "cognita.yaml")
    try:
        if yaml_file.is_file():
            match = re.search(r"^public_base_url:\s*[\"']?([^\"'\s#]+)", yaml_file.read_text(encoding="utf-8"), re.M)
            if match:
                candidates.append((str(yaml_file), match.group(1)))
    except OSError as exc:
        ctx.log.line(f"status: could not read {yaml_file}: {exc}")
    for source, value in candidates:
        if _is_loopback_url(value):
            ctx.log.line(f"status: public url from {source} is a loopback address; counts as not set")
            continue
        ctx.log.line(f"status: public url {value} from {source}")
        return value
    return None


def status_data(ctx: Ctx) -> dict:
    """The `status --json` object (design 19.6).  No Admin login, no lock; every field that cannot be
    determined is None, and nothing here raises: a stopped, half-installed or missing install answers."""
    env = read_env(ctx)
    result: dict = {"installed": False, "running": False, "version": None, "admin_url": None, "mcp_url": None,
                    "public_url": None, "workspace": None, "acceleration": None, "proof": None}
    if not env.get("COGNITA_RELEASES_ROOT"):
        ctx.log.line("status: json: no install (the env file is missing or has no COGNITA_RELEASES_ROOT)")
        return result
    current = current_dir(env)
    info: dict[str, str] = {}
    try:
        if (current / "release.txt").is_file():
            info = release_facts(current)
    except (OSError, release.ReleaseError) as exc:
        ctx.log.line(f"status: json: cannot read the current release ({type(exc).__name__}: {exc})")
    result["installed"] = bool(info)
    # version, workspace and acceleration come from the CURRENT release's release.txt, not the env file:
    # after a rollback the env file describes what the NEXT staging uses, the release what runs (5.2).
    result["version"] = info.get("version") or None
    if info.get("mode") in ("full", "core"):
        result["workspace"] = "on" if info["mode"] == "full" else "off"
    if info.get("profile") in ACCELERATION_PROFILES:
        result["acceleration"] = info["profile"]
    admin_port, mcp_port = env.get("COGNITA_ADMIN_HOST_PORT", ""), env.get("COGNITA_MCP_HOST_PORT", "")
    if admin_port.isdigit():
        result["admin_url"] = f"{'https' if admin_https(env) else 'http'}://127.0.0.1:{admin_port}"
    if mcp_port.isdigit():
        result["mcp_url"] = f"http://127.0.0.1:{mcp_port}"
    result["public_url"] = public_url_of(ctx, env)
    # Design 21.2: what the last self-tests did (`passed`, `skipped`); None = never recorded.
    result["proof"] = env.get(PROOF_KEY) if env.get(PROOF_KEY) in (PROOF_PASSED, PROOF_SKIPPED) else None
    active = ctx.sh.capture(["systemctl", "--user", "is-active", UNIT]).out.strip() == "active"
    healthy = False
    if active and mcp_port.isdigit():
        code, _body = ctx.http("GET", f"http://127.0.0.1:{mcp_port}/healthz", timeout=5)
        healthy = code == 200
    result["running"] = active and healthy
    ctx.log.line(f"status: json: installed={result['installed']} unit_active={active} healthz_ok={healthy}")
    return result


def cmd_status(ctx: Ctx, a) -> int:
    if getattr(a, "json", False):
        ctx.ui.say(json.dumps(status_data(ctx), ensure_ascii=False))
        return EXIT_OK
    env = require_install(ctx)
    for row in status_text_lines(ctx, env):
        ctx.ui.say(row)
    # Design 21.2: said only when the self-tests were skipped; a later passing proof replaces the value.
    if env.get(PROOF_KEY) == PROOF_SKIPPED:
        ctx.ui.say("Self-tests: skipped at install (run Setup again to run them)")
        ctx.log.line(f"status: {PROOF_KEY}={PROOF_SKIPPED}, so the skipped line is shown")
    elif env.get(PROOF_KEY) != PROOF_PASSED:
        ctx.ui.say(f"Self-tests: unverified (run {command_of(env)} install to repair and verify)")
    return EXIT_OK


# --------------------------------------------------------------------------
# diagnostics (design 19.5): one zip a user can attach to a support request
# --------------------------------------------------------------------------

REDACTED = "<redacted>"
# env.txt: a value whose KEY matches this is replaced (display paths and roots are kept).
SECRET_ENV_KEY = re.compile(r"PASSWORD|SECRET|TOKEN|KEY|DSN")
# The patterns the host-side scrubber removes from EVERY collected file.  Only the app's own log records are
# redacted at the source; child-process stderr, startup tracebacks, PostgreSQL and the broker are not.
MCP_TOKEN_SEGMENT = re.compile(r"(/mcp/)[^/\s\"'?#&]+")
BEARER_TOKEN = re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._~+/=-]+")
SECRET_JSON_VALUE = re.compile(
    r'("[^"\\]*(?:generated_key|api_key|password)[^"\\]*"\s*:\s*)"(?:[^"\\]|\\.)*"', re.I)
STATIC_KEY = re.compile(r"cognita_v\d+_[A-Za-z0-9_-]+")


class Scrubber:
    """Replaces secrets with <redacted> in a text (design 19.5).

    The literal contents of every non-empty file in ``secrets/`` come first, longest first (the Postgres
    password, the DSN that embeds it, the broker secret, a TLS key), then the patterns above.  It is
    applied to every file before it is zipped, so a value that leaked into a container's stderr never
    leaves the machine."""

    def __init__(self, literals: list[str] | None = None):
        self.literals = sorted({item for item in (literals or []) if item}, key=len, reverse=True)

    def scrub(self, text: str) -> str:
        for literal in self.literals:
            text = text.replace(literal, REDACTED)
        text = MCP_TOKEN_SEGMENT.sub(lambda m: m.group(1) + REDACTED, text)
        text = BEARER_TOKEN.sub(lambda m: f"{m.group(1)} {REDACTED}", text)
        text = SECRET_JSON_VALUE.sub(lambda m: f'{m.group(1)}"{REDACTED}"', text)
        return STATIC_KEY.sub(REDACTED, text)


def secret_literals(ctx: Ctx, env: dict[str, str]) -> list[str]:
    """What is in ``secrets/`` (empty files skipped).  A file that cannot be read is logged; the run
    goes on, because the pattern scrubbing still applies."""
    directory = env.get("COGNITA_SECRETS_ROOT")
    found: list[str] = []
    if not directory or not Path(directory).is_dir():
        ctx.log.line(f"diagnostics: no secrets directory to scrub against ({directory!r})")
        return found
    for path in sorted(Path(directory).iterdir()):
        try:
            if not path.is_file() or path.is_symlink():
                continue
            text = path.read_bytes().decode("utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            ctx.log.line(f"diagnostics: cannot read {path.name} for scrubbing ({type(exc).__name__}: {exc})")
            continue
        if text.strip():
            found += [text, text.strip()]
    ctx.log.line(f"diagnostics: scrubbing against {len(found) // 2} secret file(s) (contents not logged)")
    return found


def redact_env_text(text: str) -> str:
    """env.txt: every value whose key matches PASSWORD|SECRET|TOKEN|KEY|DSN is replaced."""
    rows = []
    for line in text.splitlines():
        key, sep, _value = line.partition("=")
        if sep and not line.lstrip().startswith("#") and SECRET_ENV_KEY.search(key.strip().upper()):
            rows.append(f"{key}={REDACTED}")
        else:
            rows.append(line)
    return "\n".join(rows) + ("\n" if text.endswith("\n") else "")


def tail_lines(path: Path, count: int, *, block: int = 1 << 16, limit: int = 64 << 20) -> str:
    """The last ``count`` lines of a file, reading backwards in blocks (a log can be large)."""
    with path.open("rb") as stream:
        stream.seek(0, os.SEEK_END)
        position = stream.tell()
        data = b""
        while position > 0 and data.count(b"\n") <= count and len(data) < limit:
            step = min(block, position)
            position -= step
            stream.seek(position)
            data = stream.read(step) + data
    lines = data.decode("utf-8", errors="replace").splitlines()
    return "\n".join(lines[-count:]) + "\n"


def status_text_lines(ctx: Ctx, env: dict[str, str]) -> list[str]:
    """What `status` prints, as lines (also status.txt in the diagnostics zip)."""
    lines = list(rel_status_lines(target_of(env)))
    scheme = "https" if admin_https(env) else "http"
    lines.append(f"Admin:  {scheme}://127.0.0.1:{env['COGNITA_ADMIN_HOST_PORT']}")
    lines.append(f"MCP:    http://127.0.0.1:{env['COGNITA_MCP_HOST_PORT']}")
    # Design 19.6: the same public-URL source as `status --json`; the Funnel address stays as the fallback
    # for a Funnel that was set up but whose address was never saved.
    public = public_url_of(ctx, env) or funnel_address(ctx)
    lines.append(f"Public: {public or f'not set up ({command_of(env)} remote-access)'}")
    hint = gpu_runtime_hint(ctx, env)
    if hint:
        lines.append(hint)
    return lines


def gpu_runtime_hint(ctx: Ctx, env: dict[str, str]) -> str | None:
    """DESIGN-NVIDIA-ACCELERATION 9 item 3, both vendors: when the unit has FAILED and the facts say the runtime
    the install was set up for is gone (NVIDIA: Docker no longer lists its `nvidia` runtime; AMD: /dev/kfd), say
    so once, with the one command that repairs it.  A running or stopped unit, a CPU install, or a runtime that
    is still there prints nothing: the hint is a diagnosis of a failure, not a standing notice."""
    profile = env.get("COGNITA_ACCELERATION", "")
    try:
        current = release_facts(current_dir(env)) if (current_dir(env) / "release.txt").is_file() else {}
    except (OSError, release.ReleaseError):
        current = {}
    profile = current.get("profile") or profile        # what runs is what the current release was staged for
    if profile not in GPU_LABELS:
        return None
    state = ctx.sh.capture(["systemctl", "--user", "is-active", UNIT]).out.strip()
    if state != "failed":
        ctx.log.line(f"status: gpu hint: profile={profile} unit state={state or 'unknown'}; not failed, no hint")
        return None
    facts = Facts()
    collect_gpu_facts(ctx, facts)
    if profile == "nvidia" and facts.nvidia_runtime is None:
        # Docker did not answer, so the unit most likely failed because the daemon is down (after a host or WSL
        # restart, say).  "Reinstall on the CPU" would be the wrong repair: it drops acceleration for good and
        # leaves the real fault.  Say nothing rather than guess.
        ctx.log.line(f"status: gpu hint: profile={profile} unit failed but Docker did not answer; no hint")
        return None
    # The same test `update` re-asks, so a vanished driver counts as well as a vanished runtime or /dev/kfd.
    gone = not gpu_still_honored(profile, facts)
    ctx.log.line(f"status: gpu hint: profile={profile} unit failed nvidia_runtime={facts.nvidia_runtime} "
                 f"nvidia_driver={facts.nvidia_driver} kfd={facts.kfd} runtime_gone={gone}")
    if not gone:
        return None
    if profile == "nvidia" and facts.nvidia_driver and nvidia_driver_too_old(facts):
        # The card is there; the driver fell below the floor.  Name the real cause and both repairs.
        return (f"Hint:   the NVIDIA driver is older than R580, which CUDA 13 needs; update it "
                f"(https://www.nvidia.com/drivers ), or run `{command_of(env)} install --acceleration cpu`")
    return (f"Hint:   the GPU runtime this install was set up for is missing; run `{command_of(env)} install "
            "--acceleration cpu`")


def _capture_text(ctx: Ctx, argv: list[str], *, timeout: int = 120) -> str:
    result = ctx.sh.capture(argv, timeout=timeout)
    body = result.out + (("\n" + result.err) if result.err.strip() else "")
    return f"$ {' '.join(argv)}\n(exit {result.rc})\n{body}"


def diagnostics_entries(ctx: Ctx, env: dict[str, str], scrubber: Scrubber) -> list[tuple[str, str]]:
    """Every file of the zip as (name inside the zip, scrubbed text).  A collector that fails yields
    `<name>.error.txt` with the reason and the others still run (design 19.5)."""
    entries: list[tuple[str, str]] = []

    def add(name: str, text: str) -> None:
        entries.append((name, scrubber.scrub(text)))

    def collect(name: str, produce: Callable[[], list[tuple[str, str]] | str]) -> None:
        try:
            produced = produce()
        except Exception as exc:  # noqa: BLE001 - one collector failing must not stop the others
            ctx.log.line(f"diagnostics: {name} failed: {type(exc).__name__}: {exc}")
            add(f"{Path(name).stem}.error.txt", f"{name} could not be collected: {type(exc).__name__}: {exc}\n")
            return
        if isinstance(produced, str):
            add(name, produced)
        else:
            for part_name, text in produced:
                add(part_name, text)
        ctx.log.line(f"diagnostics: collected {name}")

    releases = Path(env["COGNITA_RELEASES_ROOT"]) / TARGET_NAME if env.get("COGNITA_RELEASES_ROOT") else None

    def install_logs() -> list[tuple[str, str]]:
        if releases is None:
            raise FileNotFoundError("no COGNITA_RELEASES_ROOT in the env file")
        directory = releases / "logs"
        if not directory.is_dir():
            raise FileNotFoundError(f"{directory} does not exist")
        return [(f"install-logs/{path.name}", path.read_text(encoding="utf-8", errors="replace"))
                for path in sorted(directory.glob("*.log"))]

    def compose_logs() -> list[tuple[str, str]]:
        target = target_of(env)
        compose = compose_for(ctx, target, current_dir(env))
        listing = ctx.sh.capture([*compose, "config", "--services"])
        if not listing.ok:
            raise RuntimeError(f"docker compose config --services exited {listing.rc}: {listing.err.strip()}")
        parts: list[tuple[str, str]] = []
        for service in listing.out.split():
            try:
                result = ctx.sh.capture([*compose, "logs", "--no-color", "--tail", str(DIAGNOSTIC_LOG_LINES),
                                         service], timeout=300)
                parts.append((f"logs-{service}.txt", result.out + result.err))
            except Exception as exc:  # noqa: BLE001 - per-service: the other services still get collected
                ctx.log.line(f"diagnostics: logs of {service} failed: {type(exc).__name__}: {exc}")
                parts.append((f"logs-{service}.error.txt", f"{type(exc).__name__}: {exc}\n"))
        return parts

    def disk() -> str:
        rows = []
        docker_root = ctx.sh.capture(["docker", "info", "--format", "{{.DockerRootDir}}"])
        targets = [env.get("COGNITA_CONFIG_ROOT") and data_dir_of(env), docker_root.out.strip() or None,
                   *document_roots(env)]
        for path in dict.fromkeys(item for item in targets if item):
            rows.append(_capture_text(ctx, ["df", "-h", path]))
        for path in data_paths(env):
            rows.append(_capture_text(ctx, ["du", "-sh", path]))
        return "\n".join(rows)

    def versions() -> str:
        return "\n".join([
            _capture_text(ctx, ["docker", "version"]), _capture_text(ctx, ["docker", "compose", "version"]),
            _capture_text(ctx, ["uname", "-r"]),
            "/etc/os-release:\n" + (ctx.host.read_text("/etc/os-release") or "(unreadable)\n")])

    def gpu_listing() -> str:
        """NVIDIA: `nvidia-smi -L` (card names and UUIDs only, no process names); AMD: the /dev/dri listing.
        Bounded (a short timeout) and never fatal: a tool that is not there is said so in the file."""
        profile = env.get("COGNITA_ACCELERATION", "")
        if profile == "nvidia":
            tool = ctx.host.which("nvidia-smi") or (NVIDIA_WSL_SMI if ctx.host.exists(NVIDIA_WSL_SMI) else None)
            if tool is None:
                ctx.log.line("diagnostics: gpu.txt: nvidia-smi is not installed on this machine")
                return "nvidia-smi is not installed on this machine (nothing to list).\n"
            return _capture_text(ctx, [tool, "-L"], timeout=20)
        if not ctx.host.exists("/dev/dri"):
            ctx.log.line("diagnostics: gpu.txt: /dev/dri does not exist")
            return "/dev/dri does not exist on this machine (nothing to list).\n"
        return _capture_text(ctx, ["ls", "-l", "/dev/dri"], timeout=20)

    collect("install-logs", install_logs)
    collect("status.txt", lambda: "\n".join(status_text_lines(ctx, env)) + "\n")
    collect("status.json", lambda: json.dumps(status_data(ctx), ensure_ascii=False, indent=2) + "\n")
    collect("release.txt", lambda: (current_dir(env) / "release.txt").read_text(encoding="utf-8", errors="replace"))
    collect("env.txt", lambda: redact_env_text(ctx.env_path.read_text(encoding="utf-8", errors="replace")))
    collect("docker-ps.txt", lambda: _capture_text(ctx, ["docker", "ps", "-a"]))
    collect("logs", compose_logs)
    collect("app-log-tail.txt", lambda: tail_lines(Path(env["COGNITA_CONFIG_ROOT"]) / "logs" / "cognita.log",
                                                   DIAGNOSTIC_LOG_LINES))
    collect("disk.txt", disk)
    collect("versions.txt", versions)
    if env.get("COGNITA_ACCELERATION") in GPU_LABELS:
        collect("gpu.txt", gpu_listing)        # 15.0.0: only a GPU install has a card list worth attaching
    return entries


def cmd_diagnostics(ctx: Ctx, a) -> int:
    """`diagnostics --out FILE` (design 19.5).  No lock, no Admin login, and it works on a stopped or
    half-installed machine: whatever cannot be collected says so in a `<name>.error.txt`."""
    out = Path(os.path.expanduser(a.out))
    env = read_env(ctx)
    ctx.log.line(f"diagnostics: start out={out} install_found={bool(env.get('COGNITA_RELEASES_ROOT'))}")
    entries = diagnostics_entries(ctx, env, Scrubber(secret_literals(ctx, env)))
    partial = out.with_name(out.name + ".partial")
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(partial, "w", zipfile.ZIP_DEFLATED) as bundle:
            for name, text in entries:
                bundle.writestr(name, text.encode("utf-8"))
        os.replace(partial, out)
    except OSError as exc:
        with contextlib.suppress(OSError):
            partial.unlink()
        raise CliError(f"Could not write the diagnostics file {out}: {exc.strerror or exc}",
                       hint="Choose another place with --out FILE.") from exc
    ctx.log.line(f"diagnostics: wrote {out} with {len(entries)} file(s)")
    ctx.ui.say(str(out))
    return EXIT_OK


def cmd_systemctl(ctx: Ctx, verb: str) -> int:
    require_install(ctx)
    ctx.ui.say(f"systemctl --user {verb} {UNIT}")
    return ctx.sh.interactive(["systemctl", "--user", verb, UNIT])


def cmd_logs(ctx: Ctx, a) -> int:
    env = require_install(ctx)
    which = a.which or "app"
    follow = ["-f"] if a.follow else []
    if which == "app":
        path = Path(env["COGNITA_CONFIG_ROOT"]) / "logs" / "cognita.log"
        command = ["tail", "-n", "200", *follow, str(path)]
    elif which == "workspace":
        target = target_of(env)
        compose = compose_for(ctx, target, current_dir(env))
        command = [*compose, "logs", "--tail", "200", *follow, "workspace-runtime"]
    else:
        logs = sorted((local_root(env) / "logs").glob("install-*.log"))
        if not logs:
            raise CliError("There is no install log yet.", hint="Run: ./cognita install")
        command = ["tail", "-n", "300", *follow, str(logs[-1])]
    try:
        return ctx.sh.interactive(command)
    except KeyboardInterrupt:
        if not a.follow:
            raise
        ctx.log.line(f"logs: {which} follow ended by Ctrl+C")     # following "until Ctrl+C" is the normal way out
        return EXIT_OK


def cmd_password(ctx: Ctx, a) -> int:
    env = require_install(ctx)
    open_log(ctx, env, "password")
    target = target_of(env)
    _has, user = config_admin(env)
    ctx.ui.progress.outer_begin("password_change")
    password = get_password(ctx, a, twice=True, prompt="New Admin password")
    with rel_target_lock(local_root(env) / ".lock", ctx.log):
        ctx.ui.progress.begin("password")
        set_admin_credentials(ctx, target, current_dir(env), user or "admin", password)
        compose = compose_for(ctx, target, current_dir(env))
        ctx.ui.progress.begin("start")
        ctx.sh.stream([*compose, "up", "-d", "--no-deps", "--force-recreate", "cognita"], state="apply-failed")
    ctx.ui.say("The Admin password was changed and Cognita restarted so it takes effect.")
    ctx.ui.progress.outer_done()
    return EXIT_OK


def validate_new_root(ctx: Ctx, env: dict[str, str], path: str, display: str | None = None) -> str:
    root = clean_path(path)
    label = display or root       # design 19.9 item 14: a folder with a display is named by it
    bad = ctx.check_paths(root)
    if bad:
        raise CliError(f"{root!r} cannot be used: " + "; ".join(bad) + ".")
    state = ("does not exist" if not ctx.host.exists(root)
             else "is not a directory" if not ctx.host.is_dir(root)
             else "is not readable by you" if not ctx.host.access(root, os.R_OK)
             else "is not writable by you" if not ctx.host.access(root, os.W_OK) else "")
    if state:
        raise CliError(f"{label} is not usable: it {state}.",
                       hint=(f"Check that the folder exists and can be read and written, then run "
                             f"./cognita add-folder {shlex.quote(root)} --display {shlex.quote(display)} again."
                             if display else
                             f"Create it or fix its permissions, then run ./cognita add-folder '{root}'."))
    problems = containment_problems(ctx.host, data_dir_of(env), document_roots(env) + [root])
    if problems:
        raise CliError(problems[0])
    if root.startswith(("/media/", "/run/media/")):
        ctx.ui.say(f"Note: {MEDIA_WARNING}")
    return root


def cmd_add_folder(ctx: Ctx, a) -> int:
    env = require_install(ctx)
    open_log(ctx, env, "add-folder")
    display = a.display
    if display is not None:
        bad = display_problems(display)
        if bad:
            raise CliError(f"--display {display!r} cannot be used: " + "; ".join(bad) + ".")
    roots = document_roots(env)
    wanted = clean_path(a.path)
    if wanted in roots:
        # Design 19.2: a root that already exists with a NEW --display updates the display.
        slot = next(number for number, path in root_slots(env) if path == wanted)
        current = env.get(display_key(slot), "")
        if display is None or display == current:
            ctx.log.line(f"add-folder: {a.path} is already a documents folder (display {current!r}); nothing to do")
            ctx.ui.say(f"{wanted} is already one of Cognita's documents folders.")
            return EXIT_OK
        ctx.log.line(f"add-folder: {wanted} is root {slot}; its display {current!r} -> {display!r}")
        ctx.ui.progress.outer_begin("add_folder")
        with rel_target_lock(local_root(env) / ".lock", ctx.log):
            env[display_key(slot)] = display
            write_env(ctx, env)
            ctx.ui.say(f"Updating how {wanted} is shown and restarting Cognita...")
            reapply_after_folder_change(ctx, env)
        ctx.ui.say(f"Done. In Admin, {wanted} is now shown as {display}.")
        ctx.ui.progress.outer_done()
        return EXIT_OK
    root = validate_new_root(ctx, env, a.path, display)
    if len(roots) >= MAX_DOCUMENT_ROOTS:
        raise CliError(f"Cognita supports at most {MAX_DOCUMENT_ROOTS} documents folders and {len(roots)} are in use.",
                       hint="Put the new folder inside an existing one, or remove a folder by uninstalling.")
    slot = next(n for n in range(2, MAX_DOCUMENT_ROOTS + 1) if not env.get(f"COGNITA_PROJECTS_ROOT_{n}"))
    ctx.ui.progress.outer_begin("add_folder")
    with rel_target_lock(local_root(env) / ".lock", ctx.log):
        env[f"COGNITA_PROJECTS_ROOT_{slot}"] = root
        if display is not None:
            env[display_key(slot)] = display
        else:
            env.pop(display_key(slot), None)      # a display left in a freed slot must not label this folder
        write_env(ctx, env)
        ctx.log.line(f"add-folder: added {root} as COGNITA_PROJECTS_ROOT_{slot} (display {display!r})")
        ctx.ui.say(f"Adding {root} and restarting Cognita...")
        reapply_after_folder_change(ctx, env)
    ctx.ui.say(f"Done. In Admin, create a project and choose {display or root} as its folder.")
    ctx.ui.progress.outer_done()
    return EXIT_OK


def reapply_after_folder_change(ctx: Ctx, env: dict[str, str]) -> None:
    """Rewrite the current release's folders fragment from the env file, re-render the unit and restart.
    The caller holds the install lock and has written the env file."""
    target = target_of(env)
    directory = current_dir(env).resolve()
    ctx.ui.progress.begin("start")
    rel_write_folders_fragment(target, directory, ctx.log)
    ctx.log.line(f"add-folder: re-applying {directory.name}")
    rel_export_version(env["COGNITA_VERSION"], ctx.log)
    rel_apply(ctx.repo, target, directory, release_facts(directory).get("toolbox_version", ""), ctx.log)
    rel_verify(ctx.repo, target, release_facts(directory).get("version", env["COGNITA_VERSION"]), ctx.log)


def cmd_remote_access(ctx: Ctx, a) -> int:
    env = require_install(ctx)
    open_log(ctx, env, "remote-access")
    external = getattr(a, "external_url", None)
    if external is not None:
        # Design 19.9 item 7: refused before the password is asked for or Admin is touched.  A Tailscale
        # flag beside an address that skips Tailscale would be dropped silently, so it is refused too.
        validate_external_url(external)
        conflicting = [flag for flag, value in (("--tailscale-name", getattr(a, "tailscale_name", None)),
                                                ("--remote-access", getattr(a, "remote_access", None)))
                       if value is not None]
        if conflicting:
            raise CliError(f"{' and '.join(conflicting)} cannot be used with --external-url, which skips Tailscale.",
                           hint="Drop the Tailscale flag, or drop --external-url to set up a Funnel.")
    ctx.ui.progress.begin("remote_access")       # design 19.1: this command runs the `remote_access` stage
    _has, user = config_admin(env)
    password = get_password(ctx, a, twice=False, prompt="Admin password")
    address = remote_access(ctx, a, env, target_of(env), user or "admin", password)
    # Say only what the check proved.  It runs on this machine, and with Tailscale the address can
    # resolve to the tailnet here, so it passed about a minute before a brand-new Funnel address
    # worked from the internet (proof VM, 2026-09-29: the relay dropped TLS until its certificate
    # was ready).  Claiming "works" there would be a success that did not hold yet.
    ctx.ui.say(f"Cognita answers at its public address from this machine: {address}")
    ctx.ui.say("A brand-new address can take a few minutes before it works from the internet. "
               "Then open it in a browser with /healthz on the end to check.")
    ctx.ui.progress.end_stage()
    return EXIT_OK


def git_capture(ctx: Ctx, *args: str) -> Result:
    return ctx.sh.capture(["git", "-C", str(ctx.repo), *args])


NO_GIT_UPDATE_MESSAGE = "This Cognita has no git history; replace the tree and run update --no-pull."


def cmd_update(ctx: Ctx, a) -> int:
    env = require_install(ctx)
    open_log(ctx, env, "update")
    log, ui = ctx.log, ctx.ui
    if a.progress_continue:
        ui.progress.outer_resume("update")     # the process before the re-exec already wrote `update: start`
    else:
        ui.progress.outer_begin("update")
    if not a.after_pull:
        git_dir = str(ctx.repo / ".git")
        if not ctx.host.exists(git_dir):
            # Design 19.9 item 8: a tree with no .git (the Windows image's) cannot pull; say so, not a git error.
            log.line(f"update: {git_dir} does not exist, so there is no git history to pull; stopping with the "
                     "plain message (update --no-pull moves to the release the tree names)")
            raise CliError(NO_GIT_UPDATE_MESSAGE)
        dirty = git_capture(ctx, "status", "--porcelain")
        if not dirty.ok or dirty.out.strip():
            raise CliError("The Cognita checkout has uncommitted changes, so it will not be updated.",
                           hint="Commit or discard them (git status shows them), then run ./cognita update.")
        before = git_capture(ctx, "rev-parse", "HEAD").out.strip()
        ui.say("Fetching the latest Cognita (git pull --ff-only)...")
        ctx.sh.stream(["git", "-C", str(ctx.repo), "pull", "--ff-only"])
        after = git_capture(ctx, "rev-parse", "HEAD").out.strip()
        log.line(f"update: HEAD {before[:12]} -> {after[:12]}")
        if after != before:
            ui.say("The update changed this program; restarting it so the new code runs.")
            ctx.reexec(["update", "--after-pull", *_carry_flags(a)])
            return EXIT_OK
    published = read_published(ctx)
    current = release_facts(current_dir(env)).get("version", "")
    if published["version"] == current:
        # 15.0.1: an update that failed at start has already made the new release current, so a rerun used to
        # say "Already up to date" over a service that was down (Maia's NVIDIA proof, 2026-09-30).  A unit
        # someone stopped on purpose is `inactive` and still up to date; only `failed` is refused.
        state = ctx.sh.capture(["systemctl", "--user", "is-active", UNIT]).out.strip()
        log.line(f"update: {current} is already current; unit state={state or 'unknown'}")
        if state == "failed":
            raise CliError(f"Cognita {current} is the current release, but its service failed to start.",
                           hint="See why with ./cognita status and ./cognita logs. Once it is fixed, start it "
                                "with ./cognita start, or go back with ./cognita rollback.")
        ui.say(f"Already up to date ({current}).")
        if env.get(PROOF_KEY) not in (PROOF_PASSED, PROOF_SKIPPED):
            ui.say(f"Self-tests are unverified; run {command_of(env)} install to repair and verify.")
        ui.progress.outer_done()
        return EXIT_OK
    code = switch_release(ctx, a, env, kind="update", old_version=current, new_version=published["version"])
    ui.progress.outer_done()
    return code


def _carry_flags(a) -> list[str]:
    flags: list[str] = []
    if getattr(a, "admin_password_file", None):
        flags += ["--admin-password-file", a.admin_password_file]
    if getattr(a, "admin_password_stdin", False):
        # Design 19.9 item 9: the update reads its password only after the pull, so the line is still
        # unread on standard input, which the re-exec inherits.  Nothing before the pull touches stdin.
        flags.append("--admin-password-stdin")
    if getattr(a, "non_interactive", False):
        flags.append("--non-interactive")
    if getattr(a, "progress_file", None):
        # Design 19.1: the re-exec keeps writing the same file and must not truncate it again.
        flags += ["--progress-file", a.progress_file, "--progress-continue"]
    return flags


def requalify_saved_gpu_profile(ctx: Ctx, env: dict[str, str]) -> None:
    """DESIGN-NVIDIA-ACCELERATION 9 item 2: before `update` stages a release for a saved GPU profile, ask the
    machine again whether that GPU can still be honored (the driver, /dev/kfd or the Docker `nvidia` runtime
    can disappear under a working install, and a GPU Compose file that cannot start takes the SERVICE down).
    Not honored: the env file says `cpu` from here on, with the same note an install rerun prints.  Honored,
    or already CPU: nothing changes.  ``env`` is edited in place; the caller writes it."""
    saved = env.get("COGNITA_ACCELERATION", "")
    if saved not in GPU_LABELS:
        ctx.log.line(f"update: saved acceleration={saved or 'unrecorded'}; no GPU profile to re-check")
        return
    facts = Facts()
    collect_gpu_facts(ctx, facts)
    if saved == "nvidia" and facts.nvidia_runtime is None:
        # Docker could not be asked, so whether the runtime is still there is unknown.  Keep the saved profile:
        # staging needs Docker anyway and fails on its own, and a guess here would drop acceleration for good.
        ctx.log.line("update: saved acceleration=nvidia kept: Docker did not answer, so the runtime is unknown")
        return
    honored = gpu_still_honored(saved, facts)
    ctx.log.line(f"update: re-checked saved acceleration={saved}: honored={honored}")
    if honored:
        return
    env["COGNITA_ACCELERATION"] = "cpu"
    note = gpu_no_longer_honored_note(saved, facts)
    ctx.ui.say(f"Note: {note}")
    ctx.log.line(f"update: acceleration {saved} -> cpu because no qualifying {GPU_LABELS[saved]} card is present")
    # 15.1.0 (design 22.12 item 9): Setup's page shows the drop too, as an `acceleration` warning.
    emit_acceleration_warning(
        ctx, saved, _sentence(note), why="saved profile no longer honored on update",
        presentation_reason="saved_profile",
    )


def switch_release(ctx: Ctx, a, env: dict[str, str], *, kind: str, old_version: str, new_version: str,
                   rollback_to: str | None = None) -> int:
    """update and rollback share this: move to another release, then prove it (C14)."""
    log, ui = ctx.log, ctx.ui
    _has, user = config_admin(env)
    user = user or "admin"
    password = get_password(ctx, a, twice=False, prompt="Admin password (to prove the release works)")
    original = dict(env)
    # What `current` runs BEFORE anything is switched.  A failure puts the env file back only while
    # `current` still runs this: once apply_release or select_release has repointed it, the env file
    # has to keep describing what actually runs (final review, finding 4).
    original_running = release_facts(current_dir(env)).get("version", "")
    step = "stage"
    warnings: list[str] = []
    try:
        with rel_target_lock(local_root(env) / ".lock", log):
            if kind == "update":
                env["COGNITA_VERSION"] = new_version
                invalidate_proof(ctx, env)
                requalify_saved_gpu_profile(ctx, env)
                write_env(ctx, env)
                target = target_of(env)
                ui.say(f"Downloading Cognita {new_version}...")
                directory = stage_with_progress(ctx, target, read_published(ctx), target.profile,
                                                env.get("COGNITA_WORKSPACE") == "on")
                facts = release_facts(directory)
                fetch_ocr_weights_guarded(ctx, env, warnings)
                prefetch_models(ctx, env, target, directory, warnings)
                step = "apply"
                ui.progress.begin("start")
                rel_export_version(new_version, log)
                rel_apply(ctx.repo, target, directory, facts.get("toolbox_version", ""), log)
                rel_verify(ctx.repo, target, new_version, log)
            else:
                assert rollback_to is not None
                chosen = local_root(env) / rollback_to
                facts = release_facts(chosen)
                env["COGNITA_VERSION"] = rollback_to
                invalidate_proof(ctx, env)
                if facts.get("profile") in ACCELERATION_PROFILES:
                    env["COGNITA_ACCELERATION"] = facts["profile"]
                env["COGNITA_WORKSPACE"] = "on" if facts.get("mode", "full") == "full" else "off"
                write_env(ctx, env)
                target = target_of(env)
                step = "apply"
                # The chosen release's folders fragment was written when it was staged, and any
                # `add-folder` since then only rewrote the fragment of the release that was current.
                # Rewrite this one from the env file before selecting it, or the rolled-back release
                # would start without the folders added since (final review, finding 7).
                ui.progress.begin("start")
                rel_write_folders_fragment(target, chosen, log)
                rel_export_version(rollback_to, log)
                rel_select(target, rollback_to, log)
            step = "proof"
            run_proof(ctx, env, target, user, password)
            if kind == "update":
                prune_old_releases(ctx, env, target)
    except (CliError, release.ReleaseError) as exc:
        log.line(f"{kind}: failed at {step}: {exc}")
        now_running = release_facts(current_dir(env)).get("version", "")
        switched = now_running != original_running
        if switched:
            log.line(f"{kind}: current now runs {now_running or '(unknown)'} (it ran {original_running or '(unknown)'}); "
                     "leaving the env file as it is, because it has to describe what runs")
        else:
            write_env(ctx, original)
            log.line(f"{kind}: current still runs {original_running or '(unknown)'}; the env file is back as it was")
        if kind == "rollback":
            if switched:
                ui.say(f"{new_version} is now the selected release, but the proof did not pass: "
                       f"{str(exc).splitlines()[0] if str(exc) else exc}")
            raise
        if step == "stage":
            raise CliError(f"Update to {new_version} failed while downloading it. {old_version} is still running.",
                           hint=f"{str(exc).splitlines()[0]}  Rerun ./cognita update.") from exc
        if not switched:
            # apply_release fails before it repoints `current` (a refused drop-in, a stop that did not
            # work): the old release is still the selected one and there is nothing to roll back to.
            raise CliError(f"Update to {new_version} failed at {step} before anything was switched. "
                           f"{old_version} is still the installed version, so there is nothing to roll back. "
                           f"If it is not running, start it with ./cognita start.",
                           hint=str(exc).splitlines()[0]) from exc
        raise CliError(f"Update to {new_version} failed at {step}. {old_version} is still on disk. "
                       f"Go back with: ./cognita rollback",
                       hint=str(exc).splitlines()[0]) from exc
    for warning in warnings:
        ui.say(f"Note: {warning}")
    ui.say(f"{'Updated to' if kind == 'update' else 'Rolled back to'} {new_version}.")
    return EXIT_OK


def prune_old_releases(ctx: Ctx, env: dict[str, str], target) -> None:
    """Section 9: after a successful update keep the two newest staged releases (the one that runs plus one
    to roll back to) and prune the rest through release.py's own prune, which takes no lock (the CLI
    already holds it).  A prune problem never fails an update that worked: it is logged and warned."""
    try:
        rel_prune(target, KEEP_STAGED_RELEASES, ctx.log)
        ctx.log.line(f"prune: kept the {KEEP_STAGED_RELEASES} newest staged releases; "
                     f"staged now: {[row[0] for row in staged_versions(env)]}")
    except (release.ReleaseError, OSError) as exc:
        ctx.log.line(f"prune: failed, the update itself succeeded: {type(exc).__name__}: {exc}")
        ctx.ui.say(f"Note: older releases could not be cleaned up ({exc}). The update itself worked.")


def staged_versions(env: dict[str, str]) -> list[tuple[str, str]]:
    """(version, built_at) for every staged release directory of this install."""
    rows = []
    root = local_root(env)
    if root.is_dir():
        for child in root.iterdir():
            # `current` is the selection (a symlink), never a release of its own.
            if (child.name != "current" and child.is_dir() and not child.is_symlink()
                    and (child / "release.txt").is_file()):
                info = rel_read_release_text(child)
                rows.append((child.name, info.get("built_at", "")))
    return rows


def cmd_rollback(ctx: Ctx, a) -> int:
    env = require_install(ctx)
    open_log(ctx, env, "rollback")
    current = release_facts(current_dir(env)).get("version", env.get("COGNITA_VERSION", ""))
    others = sorted((row for row in staged_versions(env) if row[0] != current), key=lambda row: row[1], reverse=True)
    if not others:
        raise CliError("No other release is staged, so there is nothing to roll back to.")
    target_version = others[0][0]
    ctx.ui.say(f"Rolling back from {current} to {target_version}. No git operation: the release directory "
               "carries its own Compose files.")
    ctx.ui.progress.outer_begin("rollback")
    code = switch_release(ctx, a, env, kind="rollback", old_version=current, new_version=target_version,
                          rollback_to=target_version)
    ctx.ui.progress.outer_done()
    return code


def cmd_reset(ctx: Ctx, a) -> int:
    require_install(ctx)
    command = [sys.executable, str(ctx.repo / "scripts" / "reset_disposable_state.py"),
               "--target", TARGET_NAME, "--scope", a.scope, "--apply"]
    ctx.ui.say("Reset discards the search index and/or Workspace scratch data. Your documents, "
               "settings and credentials are never touched. It asks you to type a confirmation.")
    return ctx.sh.interactive(command)


def data_paths(env: dict[str, str]) -> list[str]:
    """The exact directories `uninstall --delete-data` may remove: the install's own data roots."""
    keys = ("COGNITA_CONFIG_ROOT", "COGNITA_SECRETS_ROOT", "COGNITA_POSTGRES_DATA_ROOT",
            "COGNITA_MODEL_CACHE_ROOT", "COGNITA_WORKSPACE_DATA_ROOT", "COGNITA_TRANSFER_STAGING_ROOT",
            "COGNITA_TOOLBOX_IMAGE_CACHE_ROOT")
    seen: list[str] = []
    for key in keys:
        value = env.get(key)
        if value and value not in seen:
            seen.append(value)
    return seen


def safe_delete_targets(ctx: Ctx, env: dict[str, str]) -> tuple[list[str], list[str]]:
    """Split data_paths into (deletable, refused).  A path that is, contains or lies inside a documents
    root is refused whatever the env file says: uninstall NEVER deletes a documents root (C16)."""
    roots = document_roots(env)
    deletable, refused = [], []
    for path in data_paths(env):
        forms = {posixpath.normpath(path), posixpath.normpath(ctx.host.realpath(path))}
        clash = [r for r in roots
                 if any(overlaps(a, b) for a in forms
                        for b in (posixpath.normpath(r), posixpath.normpath(ctx.host.realpath(r))))]
        (refused if clash else deletable).append(path)
    return deletable, refused


def cmd_uninstall(ctx: Ctx, a) -> int:
    env = require_install(ctx)
    open_log(ctx, env, "uninstall")
    log, ui = ctx.log, ctx.ui
    roots = document_roots(env)
    releases = local_root(env)   # <releases>/local only: another release.py target may share <releases>
    deletable, refused = safe_delete_targets(ctx, env)
    ui.say("Uninstall stops Cognita and removes what the installer created to run it: the service, its "
           "containers, and the release files and images."
           + (" It also turns linger back off, because the installer turned it on."
              if env.get("COGNITA_LINGER_SET_BY_INSTALLER") == "1" else ""))
    ui.say("It KEEPS your documents, settings, credentials, the index, models and Workspace data:")
    for label, path in (("Documents", roots), ("Settings and credentials", [env.get("COGNITA_CONFIG_ROOT", ""),
                                                                              env.get("COGNITA_SECRETS_ROOT", "")]),
                        ("Index (database)", [env.get("COGNITA_POSTGRES_DATA_ROOT", "")]),
                        ("Models", [env.get("COGNITA_MODEL_CACHE_ROOT", "")]),
                        ("Workspace data", [env.get("COGNITA_WORKSPACE_DATA_ROOT", "")]),
                        ("Env file", [str(ctx.env_path)])):
        ui.say(f"  {label + ':':26}{', '.join(p for p in path if p)}")
    if a.delete_data:
        ui.say("")
        ui.say("--delete-data ALSO permanently deletes exactly these directories:")
        for path in deletable:
            ui.say(f"  {path}")
        ui.say(f"  {ctx.env_path}")
        for path in refused:
            ui.say(f"  (will NOT delete {path}: it overlaps a documents folder)")
        ui.say("Your documents folders are never deleted.")
        if ui.non_interactive:
            raise CliError("--delete-data needs you to type the confirmation phrase, which "
                           "--non-interactive cannot do. Nothing was changed.")
        typed = ui.ask(f"Type {CONFIRM_DELETE_PHRASE} to confirm", default=None, flag="delete-data")
        if typed != CONFIRM_DELETE_PHRASE:
            raise CliError("The confirmation phrase was not typed. Nothing was changed.")
    elif not ui.confirm("Uninstall Cognita now?", default=False, preset=True if a.yes else None, flag="yes"):
        raise CliError("Cancelled. Nothing was changed.")

    unit_file = Path(unit_file_path(ctx.host))
    failures: list[str] = []
    with rel_target_lock(local_root(env) / ".lock", log):
        target = target_of(env)
        directory = current_dir(env)
        ctx.sh.capture(["systemctl", "--user", "stop", UNIT])
        ctx.sh.capture(["systemctl", "--user", "disable", UNIT])
        if unit_file.is_file():
            unit_file.unlink()
            log.line(f"uninstall: removed {unit_file}")
        ctx.sh.capture(["systemctl", "--user", "daemon-reload"])
        if directory.exists():
            compose = compose_for(ctx, target, directory)
            ctx.sh.stream([*compose, "down"], check=False, state="apply-failed")   # containers and networks only
        else:
            found = ctx.sh.capture(["docker", "ps", "-a", "-q", "--filter",
                                    f"label=com.docker.compose.project={PROJECT}"]).out.split()
            if found:
                ctx.sh.stream(["docker", "rm", "-f", *found], check=False)
        refs = uninstall_image_refs(releases, log)
        for ref in refs:
            result = ctx.sh.capture(["docker", "image", "rm", ref])
            log.line(f"uninstall: image rm {ref} -> exit {result.rc}")
        if env.get("COGNITA_LINGER_SET_BY_INSTALLER") == "1":
            ui.say("Turning linger back off, because the installer turned it on.")
            try:
                sudo_step(ctx, "undo the linger this installer enabled",
                          ["loginctl", "disable-linger", ctx.host.user()])
                env.pop("COGNITA_LINGER_SET_BY_INSTALLER", None)
                if not a.delete_data:
                    write_env(ctx, env)
            except CliError as exc:
                failures.append(str(exc))
                log.line(f"uninstall: linger was not turned off: {exc}")
    # Now the lock is released (its file lives inside <releases>/local, which goes next) and this
    # run's own log is the last thing to lose: it is closed first, because nothing more can usefully
    # be written to a file that is about to be deleted with the release files.
    if releases.is_dir():
        ui.say(f"This run's log ({log.path}) is removed together with the release files.")
        log.close()
        remove_tree(ctx, releases, failures)
    try_rmdir(ctx, Path(env["COGNITA_RELEASES_ROOT"]))   # only when nothing else (another release.py target) is in it
    if a.delete_data:
        for path in deletable:
            remove_tree(ctx, Path(path), failures)
        try:
            ctx.env_path.unlink()
            log.line(f"uninstall: deleted {ctx.env_path}")
        except FileNotFoundError:
            log.line(f"uninstall: {ctx.env_path} was already gone")
        for parent in (data_dir_of(env), str(ctx.env_path.parent)):
            if parent:
                try_rmdir(ctx, Path(parent))                # only when empty; anything else in there is not ours
    ui.say("")
    ui.say("Cognita was uninstalled." if not failures else "Cognita was uninstalled, with problems:")
    for row in failures:
        ui.say(f"  - {row}")
    if not a.delete_data:
        ui.say("Kept: " + "; ".join(p for p in [*roots, *deletable, str(ctx.env_path)] if p))
        ui.say("Reinstalling with ./cognita install finds and reuses them.")
    if ctx.host.which("tailscale"):
        ui.say("Docker and Tailscale were not removed. If you set up a Funnel, turn it off with: "
               "sudo tailscale funnel --https=443 off")
    else:
        ui.say("Docker was not removed.")
    return EXIT_FAILED if failures else EXIT_OK


def try_rmdir(ctx: Ctx, path: Path) -> None:
    """Remove a directory only if it is empty; a directory that still holds something is left, and logged."""
    try:
        path.rmdir()
        ctx.log.line(f"uninstall: removed the empty directory {path}")
    except OSError as exc:
        ctx.log.line(f"uninstall: kept {path}: {exc.strerror or exc}")


def remove_tree(ctx: Ctx, path: Path, failures: list[str]) -> None:
    """Delete one directory tree; a failure is reported to the user and logged, never swallowed."""
    try:
        shutil.rmtree(path)
        ctx.log.line(f"uninstall: deleted {path}")
    except FileNotFoundError:
        ctx.log.line(f"uninstall: {path} was already gone")
    except OSError as exc:
        failures.append(f"could not delete {path}: {exc}")
        ctx.log.line(f"uninstall: could not delete {path}: {exc}")


def uninstall_image_refs(releases: Path, log) -> list[str]:
    """Per-release tags, published digest references and the Toolbox tag named in THIS install's
    release.txt files.  Never a prune (L12): only what this install recorded."""
    refs: list[str] = []
    if not releases.is_dir():
        return refs
    for child in sorted(releases.iterdir()):
        if child.name == "current" or not (child.is_dir() and not child.is_symlink()
                                           and (child / "release.txt").is_file()):
            continue
        info = rel_read_release_text(child)
        try:
            refs += list(rel_recorded_release_tags(info).values())
        except release.ReleaseError as exc:
            log.line(f"uninstall: {child.name} records no complete image tags ({exc}); only its published refs are removed")
        refs += info.get("published_refs", "").split()
        version = info.get("toolbox_version")
        if version:
            # The `cognita-workspace-toolbox:<v>` tag is one name for the whole machine: on a box that also
            # runs release.py targets (kei) their releases load the same tag, so removing it here would
            # break them (final review, finding 6).  Kept when any table target's release records it.
            sharer = toolbox_tag_used_by_table_target(version, log)
            if sharer:
                log.line(f"uninstall: keeping cognita-workspace-toolbox:{version}; {sharer} also records it")
            else:
                refs.append(f"cognita-workspace-toolbox:{version}")
    return list(dict.fromkeys(ref for ref in refs if ref))


def toolbox_tag_used_by_table_target(toolbox_version: str, log) -> str:
    """The first release directory of a release.py table target (main, beta, test) whose release.txt records
    this Toolbox version, or "" when none does.  Those targets' staged releases are what would break."""
    for name, table_target in sorted(rel_known_targets().items()):
        try:
            root = rel_target_root(table_target)
            children = sorted(root.iterdir()) if root.is_dir() else []
        except OSError as exc:
            log.line(f"uninstall: could not read the {name} releases ({exc}); treating its Toolbox as shared")
            return f"{name} (unreadable)"
        for child in children:
            if child.name == "current" or child.is_symlink() or not (child / "release.txt").is_file():
                continue
            if rel_read_release_text(child).get("toolbox_version") == toolbox_version:
                return f"{name}/{child.name}"
    return ""


# --------------------------------------------------------------------------
# Command line
# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="./cognita", description="Install and run Cognita.")
    sub = parser.add_subparsers(dest="command", required=True)

    def common(p, *, password: bool = True):
        p.add_argument("--non-interactive", action="store_true",
                       help="never ask; fail naming the flag that is missing")
        if password:
            p.add_argument("--admin-password-file", metavar="PATH",
                           help="read the Admin password from this file (never on the command line)")
            p.add_argument("--admin-password-stdin", action="store_true",
                           help="read the Admin password as one line of UTF-8 on standard input (only the "
                                "trailing newline is removed; needs --non-interactive; not with "
                                "--admin-password-file)")
        return p

    def progress_file(p):
        """Design 19.1: on every command that runs install steps."""
        p.add_argument("--progress-file", metavar="PATH",
                       help="append one JSON line per stage to this file (truncated when the command "
                            "starts); for a program that shows its own progress")
        return p

    install = progress_file(common(sub.add_parser(
        "install", help="install, repair, or change Workspace/acceleration")))
    install.add_argument("--documents", metavar="PATH")
    install.add_argument("--documents-display", metavar="TEXT",
                         help="how the first documents folder is shown to people, for example the Windows "
                              "path it lives at (letters, digits, \\ / : . - and spaces; no $ or \")")
    install.add_argument("--command-name", metavar="NAME",
                         help="the command users type in messages (default ./cognita); recorded in the "
                              "env file as COGNITA_COMMAND")
    install.add_argument("--admin-user", metavar="NAME")
    install.add_argument("--acceleration", choices=ACCELERATION_PROFILES)
    install.add_argument("--acceleration-fallback", choices=("cpu",),
                         help="with --acceleration amd or nvidia: when that GPU cannot be used here, install "
                              "for the CPU and say why instead of refusing (Setup passes it)")
    install.add_argument("--workspace", choices=("on", "off"))
    install.add_argument("--data-dir", metavar="PATH")
    install.add_argument("--mcp-port", type=int, metavar="N")
    install.add_argument("--admin-port", type=int, metavar="N")
    install.add_argument("--admin-lan", action=argparse.BooleanOptionalAction, default=None)
    install.add_argument("--admin-tls-cert", metavar="PATH")
    install.add_argument("--admin-tls-key", metavar="PATH")
    install.add_argument("--remote-access", choices=("yes", "no"))
    install.add_argument("--install-docker", choices=("yes", "no"))
    install.add_argument("--tailscale-name", metavar="NAME")
    install.add_argument("--adopt", metavar="ENV_FILE",
                         help="move the release.py target described by this env file onto ./cognita, keeping "
                              "its data. This IS the request to stop that target's service (after everything "
                              "is downloaded and checked): there is no further question, and --yes is not needed")
    install.add_argument("--force", action="store_true",
                         help="accept overlapping folders when adopting an existing install (no longer needed "
                              "for other Linux distributions; accepted so older scripts keep working)")
    install.add_argument("--accept-plain-http-admin", action="store_true",
                         help="with --admin-lan and no --admin-tls-cert/--admin-tls-key: accept that Admin, "
                              "and the Admin password typed into it, travel over plain HTTP on the network "
                              "(--yes does not accept this)")
    install.add_argument("--yes", action="store_true", help="skip only the final plan confirmation")

    status = sub.add_parser("status", help="what is running")
    status.add_argument("--json", action="store_true",
                        help="print one JSON object (installed, running, version, admin_url, mcp_url, "
                             "public_url, workspace, acceleration) instead of the text")
    diagnostics = sub.add_parser("diagnostics", help="collect logs and settings into one zip for support")
    diagnostics.add_argument("--out", required=True, metavar="FILE", help="the zip file to write")
    logs = sub.add_parser("logs", help="show logs")
    logs.add_argument("which", nargs="?", choices=("app", "workspace", "install"), default="app")
    logs.add_argument("-f", "--follow", action="store_true")
    for verb in ("start", "stop", "restart"):
        sub.add_parser(verb, help=f"{verb} Cognita")
    progress_file(common(sub.add_parser("password", help="change the Admin password")))
    add = progress_file(sub.add_parser("add-folder", help="add another documents folder"))
    add.add_argument("path")
    add.add_argument("--display", metavar="TEXT",
                     help="how this folder is shown to people (for a folder already added, this updates it)")
    add.add_argument("--non-interactive", action="store_true")
    remote = progress_file(common(sub.add_parser("remote-access", help="set up Tailscale Funnel")))
    remote.add_argument("--external-url", metavar="URL",
                        help="an https:// address that already reaches this machine's MCP port (for example "
                             "one Windows Tailscale publishes): skip Tailscale here, save the address in Admin "
                             "and check it")
    remote.add_argument("--tailscale-name", metavar="NAME")
    remote.add_argument("--remote-access", choices=("yes", "no"), help="yes = install Tailscale without asking")
    update = progress_file(common(sub.add_parser("update", help="pull the latest Cognita and move to it")))
    # --no-pull is the public spelling of the hidden --after-pull (design 19.4): both skip the dirty check,
    # the git pull and the re-exec.  One destination, so there is one code path.
    update.add_argument("--no-pull", dest="after_pull", action="store_true",
                        help="do not run git pull; move to the release this tree already names (for a "
                             "tree that has no .git)")
    update.add_argument("--after-pull", dest="after_pull", action="store_true", help=argparse.SUPPRESS)
    # Passed only by the re-exec after `git pull`: keep writing the progress file, do not truncate it.
    update.add_argument("--progress-continue", action="store_true", help=argparse.SUPPRESS)
    progress_file(common(sub.add_parser("rollback", help="go back to the previous release")))
    reset = sub.add_parser("reset", help="discard the index and/or Workspace scratch state")
    reset.add_argument("scope", choices=("index", "workspaces", "all"))
    uninstall = sub.add_parser("uninstall", help="remove Cognita, keeping your data")
    uninstall.add_argument("--delete-data", action="store_true")
    uninstall.add_argument("--yes", action="store_true")
    uninstall.add_argument("--non-interactive", action="store_true")
    return parser


def setup_progress(ctx: Ctx, args) -> None:
    """Design 19.1: switch the progress file on when --progress-file was given, and truncate it once,
    as the command starts.  The re-exec after `git pull` passes --progress-continue and does not truncate."""
    path = getattr(args, "progress_file", None)
    if not path:
        return
    progress = Progress(os.path.expanduser(path), log=ctx.log, render=ctx.ui.render,
                        append=bool(getattr(args, "progress_continue", False)))
    ctx.ui.progress = progress
    progress.start_command()


def preread_password(ctx: Ctx, args) -> None:
    """Design 19.9 item 9: with --admin-password-stdin the line is read FIRST, before any command that
    could touch standard input (the checks run child processes that inherit it).  The one exception is a
    plain `update` that has not pulled yet: it re-executes itself after `git pull`, and the password has to
    still be unread for the new process to read, so the first process leaves stdin alone."""
    if not getattr(args, "admin_password_stdin", False):
        return
    if args.command == "update" and not args.after_pull:
        ctx.log.line("password: --admin-password-stdin will be read after the pull (the update re-executes "
                     "itself first, so standard input is left unread until then)")
        return
    read_password_stdin(ctx)


def ensure_session_env(ctx: Ctx) -> None:
    """Design 19.9 item 12: `systemctl --user` and the Docker-under-systemd checks need the user's
    session variables, and a shell that reached this machine some other way (`wsl -d`, a service, `su`)
    often has neither.  XDG_RUNTIME_DIR=/run/user/<uid> is set when it is unset and that directory exists;
    DBUS_SESSION_BUS_ADDRESS=unix:path=<runtime dir>/bus when it is unset and that socket exists.  A variable
    that is already set is NEVER overwritten, and every decision is logged with its values."""
    environ, host, log = ctx.environ, ctx.host, ctx.log
    uid = host.uid()
    runtime = environ.get("XDG_RUNTIME_DIR")
    if runtime:
        log.line(f"session: XDG_RUNTIME_DIR is already set ({runtime}); left as it is")
    else:
        candidate = f"/run/user/{uid}"
        if host.is_dir(candidate):
            environ["XDG_RUNTIME_DIR"] = runtime = candidate
            log.line(f"session: XDG_RUNTIME_DIR was unset; set to {candidate}")
        else:
            log.line(f"session: XDG_RUNTIME_DIR is unset and {candidate} does not exist; left unset")
    bus = environ.get("DBUS_SESSION_BUS_ADDRESS")
    if bus:
        log.line(f"session: DBUS_SESSION_BUS_ADDRESS is already set ({bus}); left as it is")
    elif not runtime:
        log.line("session: DBUS_SESSION_BUS_ADDRESS is unset and there is no runtime directory to look in; left unset")
    else:
        socket_path = f"{runtime.rstrip('/')}/bus"
        if host.exists(socket_path):
            environ["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path={socket_path}"
            log.line(f"session: DBUS_SESSION_BUS_ADDRESS was unset; set to unix:path={socket_path}")
        else:
            log.line(f"session: DBUS_SESSION_BUS_ADDRESS is unset and {socket_path} does not exist; left unset")


def _print_stopped_note(ctx: Ctx) -> None:
    note = stopped_note(ctx)
    if note:
        ctx.log.line("failure: " + note)
        print(ctx.ui.render(note), file=sys.stderr)
        ctx.ui.progress.warning(ctx.ui.progress.stage or "start", ctx.ui.render(note))


def main(argv: list[str] | None = None, ctx: Ctx | None = None) -> int:
    args = build_parser().parse_args(argv)
    if ctx is None:
        log = InstallLog(None)
        ui = UI(log, non_interactive=getattr(args, "non_interactive", False),
                assume_yes=getattr(args, "yes", False))
        ctx = Ctx(log=log, ui=ui, sh=Runner(log), host=Host(),
                  admin_factory=lambda port, https=False: AdminClient(port, https=https, log=log))
    log = ctx.log
    ctx.ui.current_step = ""
    log.line(f"cognita {args.command}: repo={ctx.repo} env={ctx.env_path}")
    ensure_session_env(ctx)
    # Design 19.6: every message names the command the user types: --command-name, else the env file's
    # COGNITA_COMMAND, else ./cognita.  Design 19.1: the progress file is set up (and truncated) once, here.
    try:
        ctx.ui.command = getattr(args, "command_name", None) or command_of(read_env(ctx))
    except OSError as exc:
        log.line(f"{args.command}: cannot read the env file for the command name ({exc}); using {DEFAULT_COMMAND}")
    setup_progress(ctx, args)
    handlers: dict[str, Callable[[], int]] = {
        "install": lambda: cmd_install(ctx, args), "status": lambda: cmd_status(ctx, args),
        "diagnostics": lambda: cmd_diagnostics(ctx, args),
        "logs": lambda: cmd_logs(ctx, args), "password": lambda: cmd_password(ctx, args),
        "add-folder": lambda: cmd_add_folder(ctx, args), "remote-access": lambda: cmd_remote_access(ctx, args),
        "update": lambda: cmd_update(ctx, args), "rollback": lambda: cmd_rollback(ctx, args),
        "reset": lambda: cmd_reset(ctx, args), "uninstall": lambda: cmd_uninstall(ctx, args),
        "start": lambda: cmd_systemctl(ctx, "start"), "stop": lambda: cmd_systemctl(ctx, "stop"),
        "restart": lambda: cmd_systemctl(ctx, "restart"),
    }
    try:
        check_password_flags(args)
        preread_password(ctx, args)
        return handlers[args.command]()
    except CliError as exc:
        log.line(f"{args.command}: FAILED during {ctx.ui.current_step or 'startup'}: {exc}")
        print(ctx.ui.render(f"\nError{f' during {ctx.ui.current_step}' if ctx.ui.current_step else ''}: {exc}"),
              file=sys.stderr)
        if exc.hint:
            print(ctx.ui.render(f"Next: {exc.hint}"), file=sys.stderr)
        _print_stopped_note(ctx)
        if log.path:
            print(f"Log: {log.path}", file=sys.stderr)
        ctx.ui.progress.fail(str(exc), exc.hint)          # design 19.1: the terminal's text and next command
        return exc.code
    except release.ReleaseError as exc:
        log.line(f"{args.command}: FAILED during {ctx.ui.current_step or 'startup'} [{exc.state}] {exc}")
        step = f" during {ctx.ui.current_step}" if ctx.ui.current_step else ""
        print(ctx.ui.render(f"\nError ({exc.state}){step}: {exc}"), file=sys.stderr)
        if log.path:
            print(f"Log: {log.path}", file=sys.stderr)
        print("Next: fix the cause above, then run the same command again; finished steps are skipped.",
              file=sys.stderr)
        _print_stopped_note(ctx)
        ctx.ui.progress.fail(str(exc), "Fix the cause above, then run the same command again; finished steps "
                                       "are skipped.")
        return release.EXIT_CODES.get(exc.state, EXIT_FAILED)
    except KeyboardInterrupt:
        log.line(f"{args.command}: interrupted")
        print("\nInterrupted. Run the same command again to finish; finished steps are skipped.", file=sys.stderr)
        _print_stopped_note(ctx)
        ctx.ui.progress.fail("Interrupted.", "Run the same command again to finish; finished steps are skipped.")
        return 130
    except Exception as exc:  # noqa: BLE001 - the last line of defense: log the traceback, name the log
        log.line(f"{args.command}: UNEXPECTED {type(exc).__name__}: {exc}\n{traceback.format_exc()}")
        print(f"\nUnexpected error: {type(exc).__name__}: {exc}", file=sys.stderr)
        if log.path:
            print(f"Log with the full traceback: {log.path}", file=sys.stderr)
        _print_stopped_note(ctx)
        ctx.ui.progress.fail(f"Unexpected error: {type(exc).__name__}: {exc}",
                             f"See the log with the full traceback: {log.path}" if log.path else None)
        return EXIT_FAILED
    finally:
        log.close()


if __name__ == "__main__":
    sys.exit(main())
