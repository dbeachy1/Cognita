#!/usr/bin/env python3
"""Cognita 13.0 release tool: build, test, deploy, select, verify.

One script, run on kei from a clean checkout with the host interpreter
(Python 3.11+, standard library only -- there is no venv on the host).  It is a
thin wrapper over Docker Compose, user systemd and the existing HTTP self-test;
thinness is a requirement (DESIGN-13.0-DOCKER-REWRITE.md sections 0 and 6).

A release is a directory ``<releases>/<target>/<version>/`` holding the rendered
Compose files and a ``release.txt``.  ``current`` is a symlink to one of them,
and that symlink is the selection.  There is no journal, no intent file, no
rollback engine: getting back to an older build is ``select --version <old>``.

Candidate release workflow (the commit placeholder is its full Git SHA):
    python3 scripts/release.py build --target test --profiles cpu,amd \
        --write-images /data/cognita/releases/candidates/C/images \
        --export-cpu /data/cognita/releases/candidates/C/cognita-cpu.tar \
        --write-sums /data/cognita/releases/candidates/C/SHA256SUMS
    python3 scripts/release.py test --target test --profile cpu --mode core \
        --images /data/cognita/releases/candidates/C/images --no-build
    python3 scripts/release.py test --target test --profile amd --mode full \
        --images /data/cognita/releases/candidates/C/images --no-build
    python3 scripts/release.py bundle-windows --target test \
        --images /data/cognita/releases/candidates/C/images \
        --cpu-archive /data/cognita/releases/candidates/C/cognita-cpu.tar \
        --output /data/cognita/releases/candidates/C/windows --mode core
    python3 scripts/release.py deploy --target beta --profile amd --mode full \
        --images /data/cognita/releases/candidates/C/images --no-build --test

``publish`` (run by Doug on kei, --target test only) builds, proves and pushes the
images the Linux installer installs and writes containers/published-release.txt
(``--amd`` and ``--nvidia`` add the GPU app images and their image_ref_/size_ keys);
``--target local`` is the installer's own target, read from ~/.config/cognita/
install.env (DESIGN-LINUX-INSTALLER.md section 5).

Standard release operations remain build, test, deploy, qa, select, status, doctor,
install-unit, and prune. A release is a directory ``<releases>/<target>/<version>/``
holding the rendered Compose files and a ``release.txt``. ``current`` selects one
directory; there is no journal or rollback engine.
"""
from __future__ import annotations

import argparse
import ast
import contextlib
import dataclasses
import datetime as dt
import difflib
import hashlib
import importlib.util
import json
import os
import re
import shutil
import socket
import string
import queue
import subprocess
import sys
import tempfile
import threading
import urllib.error
import urllib.request
import uuid
import time
import signal
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

try:
    from scripts import release_windows
except ImportError:  # Direct execution puts scripts/, rather than its parent, on sys.path.
    import release_windows

try:
    from scripts import ocr_weights
except ImportError:  # Direct execution puts scripts/, rather than its parent, on sys.path.
    import ocr_weights

try:  # POSIX (kei) -- the real lock.
    import fcntl
except ImportError:  # pragma: no cover - Windows unit tests only
    fcntl = None
try:  # Windows, so the lock and its test are real on the development box too.
    import msvcrt
except ImportError:  # pragma: no cover - POSIX
    msvcrt = None

REPO_ROOT = Path(__file__).resolve().parents[1]
RELEASES_ROOT = Path(os.environ.get("COGNITA_RELEASES_ROOT", "/data/cognita/releases"))
SYSTEMD_USER_DIR = Path.home() / ".config" / "systemd" / "user"
UNIT_TEMPLATE = REPO_ROOT / "scripts" / "systemd" / "cognita-compose.service.template"

# The MCP port INSIDE the app container.  Host ports differ per target; the
# live self-test runs in the container and always sees this one.
CONTAINER_MCP_PORT = 8675

# Failure states from DESIGN-13.0 section 6.2.  The name is what the script
# prints; the number is what the shell sees.
EXIT_CODES = {
    "verified": 0,
    "usage": 1,
    "dirty-checkout": 2,
    "version-exists": 3,
    "doctor-failed": 4,
    "build-failed": 5,
    "test-failed": 6,
    "apply-failed": 7,
    "verify-failed": 8,
    "test-mode-stuck": 9,
}


# 15.0.0 (DESIGN-NVIDIA-ACCELERATION 10): the image profiles.  `cpu` is always built first; the GPU profiles
# each add their own app image and Compose overlay (compose.<profile>.yaml).
PROFILES = ("cpu", "amd", "nvidia")
GPU_PROFILES = ("amd", "nvidia")


@dataclass(frozen=True)
class Target:
    """One deployment target.  DESIGN-13.0 section 6.1 -- the whole descriptor.

    The env file is the target's real descriptor (bind-mount roots, UID/GID,
    device group IDs, published ports); this table only says which file, unit,
    project and ports belong together.
    """

    name: str
    profile: str          # "cpu", "amd" or "nvidia"; selects the Compose file set
    env_file: Path
    unit: str
    project: str
    mcp_port: int
    admin_port: int
    # Enabled combined connector used by live QA. The install proof supplies
    # its temporary connector directly; later local QA names an installed one.
    connector: str
    # DESIGN-LINUX-INSTALLER 5.1.  None means "the module's RELEASES_ROOT",
    # which is what every table target uses and is looked up at CALL time (the
    # tests, and COGNITA_RELEASES_ROOT, move that module value after this table
    # is built).  The `local` target names its own root from its env file.
    releases_root: Path | None = None


def _env(path: str) -> Path:
    return Path(os.path.expanduser(path))


TARGETS: dict[str, Target] = {
    "main": Target(
        name="main",
        profile="amd",
        env_file=_env("~/.config/cognita/cognita-main-amd.env"),
        unit="cognita-compose-main.service",
        project="cognita-main",
        mcp_port=8675,
        admin_port=8676,
        connector="cognita",
    ),
    "beta": Target(
        name="beta",
        profile="amd",
        env_file=_env("~/.config/cognita/cognita-amd.env"),
        unit="cognita-compose-amd.service",
        project="cognita-12",
        mcp_port=9675,
        admin_port=9676,
        # Read from beta's own connectors.yaml on kei, September 22: beta
        # serves the same slug as main.
        connector="cognita",
    ),
    # 13.0 acceptance target.  It exists so the release tool can be proven
    # end to end on kei without stopping, restarting or repointing main or
    # beta.  Its data roots are /data/cognita-13-test/*, named in its own env
    # file exactly like the other two.
    "test": Target(
        name="test",
        profile="amd",
        env_file=_env("~/.config/cognita/cognita-test-amd.env"),
        unit="cognita-compose-test.service",
        project="cognita-13test",
        mcp_port=8775,
        admin_port=8776,
        connector="self-test",
    ),
}


class ReleaseError(Exception):
    """A failure with one of the section 6.2 states attached."""

    def __init__(self, state: str, message: str):
        super().__init__(message)
        self.state = state


# --------------------------------------------------------------------------
# The `local` target (DESIGN-LINUX-INSTALLER section 5.1)
# --------------------------------------------------------------------------

LOCAL_TARGET = "local"
TARGET_CHOICES = (*sorted(TARGETS), LOCAL_TARGET)

# The first documents root is COGNITA_PROJECTS_ROOT (compose.yaml binds it);
# `cognita add-folder` adds COGNITA_PROJECTS_ROOT_2 .. _9 (design section 3).
DOCUMENT_ROOT_KEYS = ("COGNITA_PROJECTS_ROOT",
                      *(f"COGNITA_PROJECTS_ROOT_{n}" for n in range(2, 10)))


# Design 19.2: a documents root may carry a display text (the Windows path a person knows it by).
# COGNITA_PROJECTS_ROOT_DISPLAY belongs to the first root, COGNITA_PROJECTS_ROOT_<n>_DISPLAY to root n.
DOCUMENT_DISPLAY_KEYS = {
    DOCUMENT_ROOT_KEYS[0]: "COGNITA_PROJECTS_ROOT_DISPLAY",
    **{key: f"{key}_DISPLAY" for key in DOCUMENT_ROOT_KEYS[1:]},
}

# Design 19.6: the one name users type for the CLI.  `./cognita` in a Linux clone; Windows setup records
# `cognita` (COGNITA_COMMAND in the env file).  Every user-facing hint for the `local` target uses it.
COMMAND_KEY = "COGNITA_COMMAND"
DEFAULT_COMMAND = "./cognita"


def command_name(values: dict[str, str]) -> str:
    return values.get(COMMAND_KEY) or DEFAULT_COMMAND


def local_env_file() -> Path:
    """The install's one state file.  Tests point COGNITA_LOCAL_ENV_FILE elsewhere."""
    override = os.environ.get("COGNITA_LOCAL_ENV_FILE")
    if override:
        return Path(override)
    # install.env, not cognita.env: kei already has a 12.x-era ~/.config/cognita/cognita.env,
    # which a fixed name would have mistaken for an install (seen on the 14.1.0 deploy, 2026-09-29).
    return Path.home() / ".config" / "cognita" / "install.env"


def _env_port(values: dict[str, str], key: str, env_file: Path) -> int:
    raw = values.get(key, "")
    if not raw.isdigit() or not 0 < int(raw) < 65536:
        raise ReleaseError("usage", f"{env_file} has no valid {key} (found {raw!r})")
    return int(raw)


def resolve_target(name: str) -> Target:
    """A table target by name, or `local` from ~/.config/cognita/install.env.

    No lock, no log: this only reads.  The env file is the local install's
    whole descriptor, so a missing one is a usage error that names it.
    """
    if name in TARGETS:
        return TARGETS[name]
    if name != LOCAL_TARGET:
        raise ReleaseError("usage", f"unknown target {name!r}; known targets: {', '.join(TARGET_CHOICES)}")
    env_file = local_env_file()
    values = read_env_file(env_file)
    if not values:
        raise ReleaseError(
            "usage",
            f"the local target is described by {env_file}, which is missing or empty. "
            f"Run {DEFAULT_COMMAND} install to create it.")   # 19.6: no env file, so the default name
    profile = values.get("COGNITA_ACCELERATION") or "cpu"
    if profile not in PROFILES:
        raise ReleaseError("usage", f"{env_file} has COGNITA_ACCELERATION={profile!r}; expected cpu, amd or nvidia")
    releases = values.get("COGNITA_RELEASES_ROOT", "")
    if not releases or not Path(releases).is_absolute():
        raise ReleaseError("usage", f"{env_file} has no absolute COGNITA_RELEASES_ROOT (found {releases!r})")
    return Target(
        name=LOCAL_TARGET,
        profile=profile,
        env_file=env_file,
        unit="cognita.service",
        project="cognita",
        mcp_port=_env_port(values, "COGNITA_MCP_HOST_PORT", env_file),
        admin_port=_env_port(values, "COGNITA_ADMIN_HOST_PORT", env_file),
        # The installer proof supplies this temporary connector to qa_release.
        # The public qa/deploy CLI requires --connector on an installed local target
        # because the proof deletes it after the install check.
        connector="install-proof",
        releases_root=Path(releases),
    )


def with_qa_connector(target: Target, slug: str | None) -> Target:
    """Use an installed connector for live QA after install proof cleans up its own."""
    if slug is None:
        if target.name == LOCAL_TARGET:
            raise ReleaseError(
                "usage", "local live QA requires --connector with an enabled combined "
                "connector that can write to the Self-Test project",
            )
        return target
    if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", slug):
        raise ReleaseError("usage", f"invalid QA connector slug: {slug!r}")
    return dataclasses.replace(target, connector=slug)


def staging_mode(target: Target, log: Log) -> str:
    """The mode a NEW staging uses (design 5.2): full or core.

    `local` takes it from COGNITA_WORKSPACE in its env file; a table target is
    always full here, because its deploy passes --mode itself where it matters.
    Once staged, the mode lives in release.txt and everything else reads it
    from there (`release_mode`).
    """
    if target.name != LOCAL_TARGET:
        return "full"
    value = read_env_file(target.env_file).get("COGNITA_WORKSPACE", "")
    if value == "on":
        mode = "full"
    elif value in {"off", ""}:
        mode = "core"
    else:
        raise ReleaseError("usage", f"{target.env_file} has COGNITA_WORKSPACE={value!r}; expected on or off")
    log.line(f"stage: COGNITA_WORKSPACE={value or '(unset)'} -> mode {mode}")
    return mode


# --------------------------------------------------------------------------
# Logging and process execution
# --------------------------------------------------------------------------


class Log:
    """Plain readable lines, to the screen and to one log file per run.

    Every decision, skip and retry goes through here with its values, because
    the only thing anyone has after a failed release is this file.
    """

    def __init__(self, path: Path | None):
        self.path = path
        self._stream = None
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            self._stream = path.open("a", encoding="utf-8")

    def line(self, text: str) -> None:
        stamp = dt.datetime.now().strftime("%H:%M:%S")
        rendered = f"{stamp} {text}"
        print(rendered, flush=True)
        if self._stream is not None:
            self._stream.write(rendered + "\n")
            self._stream.flush()

    def raw(self, text: str) -> None:
        """Child process output: to the log file always, to the screen too."""
        print(text, flush=True)
        if self._stream is not None:
            self._stream.write(text + "\n")
            self._stream.flush()

    def close(self) -> None:
        if self._stream is not None:
            self._stream.close()
            self._stream = None


# 130 = a shell's report of a child killed by SIGINT; -2 = Popen's (killed by signal 2).
_INTERRUPTED_EXITS = frozenset({130, -signal.SIGINT})


class Stopped(Exception):
    """A child process was stopped because the caller's ``stop_check`` asked for it (design 21.2).

    Deliberately not a ReleaseError: it is a decision, not a failure, and no caller may report it
    with one of the section 6.2 failure states."""


# How long a terminated child gets to exit before it is killed (design 21.2).
_STOP_TERMINATE_WAIT_S = 10
_END_OF_OUTPUT = object()


def _read_until_stopped(process, command: list[str], *, log: Log, tail: deque[str], quiet: bool,
                        stop_check: Callable[[], bool], stop_poll_s: float) -> None:
    """The stop_check half of ``run``: read the child's lines through a queue so the wait for a line
    can time out and ask ``stop_check``.  Returns at end of output; raises ``Stopped`` after it has
    terminated (then, if needed, killed) the child."""
    lines: queue.Queue = queue.Queue()

    def pump() -> None:
        try:
            for raw_line in process.stdout:
                lines.put(raw_line)
        except (OSError, ValueError) as exc:     # the pipe was closed under the reader (a stop, or a crash)
            log.line(f"stop: output reader of {command[0]} ended on {type(exc).__name__}: {exc}")
        finally:
            lines.put(_END_OF_OUTPUT)

    # A daemon: a grandchild that inherited the pipe can keep it open after the child is gone, and
    # nothing here may wait for that.
    threading.Thread(target=pump, name=f"run-reader-{command[0]}", daemon=True).start()
    check_failed = False

    def stop_requested() -> bool:
        nonlocal check_failed
        try:
            return bool(stop_check())
        except Exception as exc:                 # a broken check must not orphan the child or end the run
            if not check_failed:
                log.line(f"stop: stop_check raised {type(exc).__name__}: {exc}; treated as not requested")
                check_failed = True
            return False

    while True:
        try:
            item = lines.get(timeout=stop_poll_s)
        except queue.Empty:
            item = None
        if item is _END_OF_OUTPUT:
            return
        if item is not None:
            text = item.rstrip("\n")
            tail.append(text)
            if not quiet:
                log.raw(text)
            elif log.path is not None:
                log._stream.write(text + "\n")  # noqa: SLF001 - same object, file only
        if not stop_requested():
            continue
        log.line(f"stopped on request: {command[0]}")
        process.terminate()
        log.line(f"stop: terminate sent to {command[0]}; waiting up to {_STOP_TERMINATE_WAIT_S} s")
        try:
            code = process.wait(timeout=_STOP_TERMINATE_WAIT_S)
            log.line(f"stop: {command[0]} exited {code} after terminate")
        except subprocess.TimeoutExpired:
            log.line(f"stop: {command[0]} still running after {_STOP_TERMINATE_WAIT_S} s; killing it")
            process.kill()
            code = process.wait()
            log.line(f"stop: {command[0]} exited {code} after kill")
        raise Stopped(f"{command[0]} was stopped on request")


def run(
    command: list[str],
    *,
    log: Log,
    state: str,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    stdin_text: str | None = None,
    check: bool = True,
    quiet: bool = False,
    stop_check: Callable[[], bool] | None = None,
    stop_poll_s: float = 0.5,
) -> tuple[int, str]:
    """Run one child process, streaming its output into the log.

    Returns (returncode, tail).  On failure with ``check`` set, the caller gets
    a ReleaseError carrying the last screen of output and the log path, which
    is the one thing section 6.2 requires of every failure.

    ``stop_check`` (design 21.2, the Windows installer's "Skip self-tests"): when given, the
    child's output is read by a thread into a queue and ``stop_check()`` is asked on every
    ``stop_poll_s`` timeout and every line.  True means: terminate the child, wait up to
    ``_STOP_TERMINATE_WAIT_S``, kill it, and raise ``Stopped``.  Without it this function is
    exactly what it was before.
    """
    log.line(f"run: {' '.join(command)}")
    child_env = {**os.environ, **(env or {})}
    tail: deque[str] = deque(maxlen=40)
    process = subprocess.Popen(
        command,
        cwd=str(cwd) if cwd else None,
        env=child_env,
        stdin=subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    if stdin_text is not None:
        # The self-test key rides on stdin so it never appears in argv or in a
        # process listing.  Close the pipe immediately; the child reads once.
        assert process.stdin is not None
        process.stdin.write(stdin_text)
        process.stdin.close()
    assert process.stdout is not None
    if stop_check is None:
        for raw_line in process.stdout:
            text = raw_line.rstrip("\n")
            tail.append(text)
            if not quiet:
                log.raw(text)
            elif log.path is not None:
                log._stream.write(text + "\n")  # noqa: SLF001 - same object, file only
    else:
        _read_until_stopped(process, command, log=log, tail=tail, quiet=quiet,
                            stop_check=stop_check, stop_poll_s=stop_poll_s)
    code = process.wait()
    log.line(f"exit {code}: {command[0]} {command[1] if len(command) > 1 else ''}")
    if code in _INTERRUPTED_EXITS:
        # The child was stopped by Ctrl+C.  Report it as the interruption it is, not as a failed
        # command ("command failed (130)"), and stop even when this process itself did not get the
        # signal: a parent that started us in the background (a script, a setup program's hidden
        # process) leaves SIGINT ignored here, and the installer then carried on past a Ctrl+C
        # (interrupt proof on the installer VM, 2026-09-29).
        log.line(f"interrupted: {command[0]} exited {code} (Ctrl+C)")
        raise KeyboardInterrupt
    if code and check:
        raise ReleaseError(
            state,
            f"command failed ({code}): {' '.join(command)}\n"
            + "\n".join(tail),
        )
    return code, "\n".join(tail)


# --------------------------------------------------------------------------
# Identity: version, toolbox version, commit
# --------------------------------------------------------------------------


def _module_literal(path: Path, name: str) -> str | None:
    """Read one string literal from a Python module without importing it.

    release.py runs on the host interpreter, which has none of Cognita's
    dependencies, so importing the package is not an option.
    """
    if not path.is_file():
        return None
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in tree.body:
        targets = []
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        for target in targets:
            if isinstance(target, ast.Name) and target.id == name:
                value = node.value
                if isinstance(value, ast.Constant) and isinstance(value.value, str):
                    return value.value
    return None


def read_version(repo: Path, log: Log) -> str:
    """The application version.  release_identity.py is the ONLY authority.

    ``cognita.__version__``, the wheel metadata, /healthz and the image tags
    all derive from that literal (section 4), so reading anything else here
    would let a release be built and tagged from a version the running service
    would not report.  Superseded: an earlier revision of this script fell back
    to ``cognita.__version__`` while the literal was being introduced.
    """
    identity = repo / "src" / "cognita" / "release_identity.py"
    value = _module_literal(identity, "APPLICATION_VERSION")
    if not value:
        raise ReleaseError(
            "usage",
            f"no APPLICATION_VERSION literal in {identity}. It is the single authority for the "
            "version; add it there rather than anywhere else.",
        )
    log.line(f"version: {value} (from src/cognita/release_identity.py)")
    return value


def read_toolbox_version(repo: Path, log: Log) -> str:
    """The Toolbox archive/tag version the broker's loader will accept.

    Two copies of this literal exist by design: the broker owns the loader
    contract (``runtime_broker/image_cache.py``) and release_identity.py owns
    the release-side literal.  If both exist they must agree, because a
    mismatch means the archive this script stages is one the loader refuses.
    """
    identity = repo / "src" / "cognita" / "release_identity.py"
    release_side = _module_literal(identity, "TOOLBOX_VERSION")
    broker_side = _module_literal(
        repo / "src" / "cognita" / "runtime_broker" / "image_cache.py", "TOOLBOX_VERSION"
    )
    if release_side and broker_side and release_side != broker_side:
        raise ReleaseError(
            "usage",
            f"TOOLBOX_VERSION disagrees: release_identity.py says {release_side}, "
            f"the broker loader says {broker_side}",
        )
    value = release_side or broker_side
    if not value:
        raise ReleaseError("usage", "no TOOLBOX_VERSION literal found in the checkout")
    log.line(f"toolbox version: {value}")
    return value


def git_commit(repo: Path, log: Log) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=False, timeout=60,
    )
    if result.returncode:
        raise ReleaseError("dirty-checkout", f"git rev-parse failed: {result.stderr.strip()}")
    return result.stdout.strip()


# Where the Microsandbox wheel the workspace-runtime image installs is staged.
# The Dockerfile COPYs it from the build context, and it is fetched rather than
# committed, so `.gitignore` carries it -- which is why there is no exception in
# the clean-checkout rule below.  Superseded: an earlier revision of this script
# tolerated the untracked path here instead.
BUILD_ARTIFACTS_DIR = "build-artifacts"


def require_clean_checkout(repo: Path, log: Log) -> str:
    """A release records the commit it built, so the tree must be that commit."""
    result = subprocess.run(
        ["git", "-C", str(repo), "status", "--porcelain"],
        capture_output=True, text=True, check=False, timeout=120,
    )
    if result.returncode:
        raise ReleaseError("dirty-checkout", f"git status failed: {result.stderr.strip()}")
    if result.stdout.strip():
        raise ReleaseError(
            "dirty-checkout",
            "the checkout has uncommitted changes; commit them first:\n" + result.stdout.rstrip(),
        )
    commit = git_commit(repo, log)
    log.line(f"checkout: clean at {commit}")
    return commit


# --------------------------------------------------------------------------
# The lock
# --------------------------------------------------------------------------


def _lock_take(handle, *, blocking: bool) -> None:
    if fcntl is not None:
        flags = fcntl.LOCK_EX if blocking else fcntl.LOCK_EX | fcntl.LOCK_NB
        fcntl.flock(handle.fileno(), flags)
        return
    if msvcrt is None:  # pragma: no cover - no known third platform
        raise ReleaseError("usage", "no file locking primitive on this platform")
    handle.seek(0)
    msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK, 1)


def _lock_release(handle) -> None:
    if fcntl is not None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return
    handle.seek(0)
    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


@contextlib.contextmanager
def target_lock(path: Path, log: Log):
    """One flock per target, around the whole command (section 3).

    One person, one box: this exists only so two invocations cannot interleave.
    A held lock is waited on, not refused, and the wait is logged.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+", encoding="utf-8")
    try:
        try:
            _lock_take(handle, blocking=False)
        except OSError:
            log.line(f"lock: {path} is held by another run; waiting for it")
            _lock_take(handle, blocking=True)
        log.line(f"lock: held {path}")
        yield
    finally:
        with contextlib.suppress(OSError):
            _lock_release(handle)
        handle.close()
        log.line(f"lock: released {path}")


# --------------------------------------------------------------------------
# Paths and Compose files
# --------------------------------------------------------------------------


def releases_root_for(target: Target | None) -> Path:
    """The releases root that applies: the target's own, else the module's."""
    if target is not None and target.releases_root is not None:
        return target.releases_root
    return RELEASES_ROOT


def target_root(target: Target) -> Path:
    return releases_root_for(target) / target.name


def release_dir(target: Target, version: str) -> Path:
    return target_root(target) / version


def current_link(target: Target) -> Path:
    return target_root(target) / "current"


def toolbox_dir(target: Target) -> Path:
    return target_root(target) / "toolbox"


def logs_dir(target: Target | None) -> Path:
    return (target_root(target) if target else RELEASES_ROOT) / "logs"


def selftest_root(target: Target) -> Path:
    """The installer-owned root the proof's Self-Test project lives in (design 7.4).

    Derived, never stored: `<releases_root>/local/self-test`.  It is bound into
    the container by the folders fragment and is NOT one of the user's
    documents roots.
    """
    return target_root(target) / "self-test"


def document_roots(values: dict[str, str]) -> list[tuple[str, str]]:
    """The user's documents roots as (env key, path), first root first."""
    return [(key, values[key]) for key in DOCUMENT_ROOT_KEYS if values.get(key)]


# Characters a root may not contain (design section 3): Compose interpolates
# `$`, systemd expands `%`, and the rest would break a quoted string.
_FORBIDDEN_ROOT_CHARS = ("\n", "\r", "$", '"', "'", "\\", "%")

# Design 19.2 / 19.6: what may not appear in a documents display or the command name, because Compose reads
# the env file and the fragment and the fragment is YAML.  Backslashes and colons ARE allowed in a display.
_FORBIDDEN_DISPLAY_CHARS = ("\n", "\r", "$", '"')
_COMMAND_NAME = re.compile(r"[A-Za-z0-9._/-]{1,100}")


def release_mode(directory: Path) -> str:
    """The mode a staged release was staged in (design 5.2); full when unrecorded.

    Releases staged before the mode was recorded were all full, which is also
    what a directory with no release.txt at all means.
    """
    recorded = read_release_text(directory).get("mode", "")
    return recorded if recorded in {"core", "full"} else "full"


def checkout_compose_files(repo: Path, profile: str, mode: str = "full") -> list[Path]:
    if profile not in PROFILES:
        raise ReleaseError("usage", f"unknown profile: {profile}")
    if mode not in {"core", "full"}:
        raise ReleaseError("usage", f"unknown mode: {mode}")
    files = [repo / "compose.yaml"]
    files.append(repo / f"compose.{profile}.yaml")
    if mode == "full":
        files.append(repo / "compose.workspace.yaml")
    return files


def staged_compose_files(directory: Path, profile: str, mode: str | None = None) -> list[Path]:
    """The Compose files a staged release starts.

    ``mode`` defaults to the mode recorded in the release itself (design 5.2),
    so a rollback to a release staged in the other mode runs in THAT mode.
    """
    if mode is None:
        mode = release_mode(directory)
    files = [directory / f.name for f in checkout_compose_files(directory, profile, mode)]
    # Releases staged before 13.5.0 carry the Workspace runtime inside
    # compose.yaml and have no overlay file.  Naming a file that is not there
    # makes every Compose call on that release fail, so `select` back to one
    # of them must leave it out.
    overlay = directory / "compose.workspace.yaml"
    if directory.is_dir() and overlay in files and not overlay.is_file():
        files.remove(overlay)
    files.append(directory / "compose.images.yaml")
    # Only `local` has one (design 5.3); a table target's release never does,
    # so kei's file list is unchanged.
    folders = directory / "compose.folders.yaml"
    if folders.is_file():
        files.append(folders)
    return files


def compose_command(
    *, project: str, env_file: Path, files: list[Path], extra_files: list[Path] | None = None
) -> list[str]:
    command = ["docker", "compose", "-p", project, "--env-file", str(env_file)]
    for path in [*files, *(extra_files or [])]:
        command += ["-f", str(path)]
    return command


def read_env_file(path: Path) -> dict[str, str]:
    """Parse a target env file.  Values are never logged: they are host paths
    and group IDs today, but this file is the installation's descriptor."""
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip()
    return values


# --------------------------------------------------------------------------
# doctor
# --------------------------------------------------------------------------


def export_version(version: str, log: Log) -> None:
    """Put the authority's version into the environment every child inherits.

    compose.yaml interpolates ``${COGNITA_VERSION}`` for the build args and the
    image names and carries no literal of its own (13.0 §4).  Compose gives the
    process environment precedence over ``--env-file``, so exporting it here
    means a release always builds and tags from the authority even when a
    target's env file still names an older version.
    """
    os.environ["COGNITA_VERSION"] = version
    log.line(f"env: exported COGNITA_VERSION={version} for Compose interpolation")


def check_release_target_key(target: Target, log: Log) -> bool:
    """COGNITA_RELEASE_TARGET tells the app which deployment it is, so the
    schema-mismatch message can name the exact reset command.  A missing or
    wrong value costs only that sentence, so this warns rather than refuses;
    the env file generated for a new target always has it."""
    value = read_env_file(target.env_file).get("COGNITA_RELEASE_TARGET", "")
    if value == target.name:
        log.line(f"env: COGNITA_RELEASE_TARGET={value}")
        return True
    if not value:
        log.line(f"env: WARNING {target.env_file.name} has no COGNITA_RELEASE_TARGET; add "
                 f"COGNITA_RELEASE_TARGET={target.name} so a schema-mismatch message can name "
                 "the reset command")
    else:
        log.line(f"env: WARNING {target.env_file.name} says COGNITA_RELEASE_TARGET={value}, "
                 f"but this is target {target.name}")
    return False


def doctor(target: Target | None, log: Log, *, profile: str | None = None,
           mode: str = "full") -> None:
    """Host prerequisites for the selected Compose profile and mode.

    This is all that survives of six preflight-*.sh scripts (section 8): the
    real device and daemon probes, nothing else.
    """
    problems: list[str] = []

    def probe(label: str, command: list[str]) -> str | None:
        result = subprocess.run(command, capture_output=True, text=True, check=False, timeout=60)
        if result.returncode:
            problems.append(f"{label}: {command[0]} failed ({result.returncode}) {result.stderr.strip()[:200]}")
            log.line(f"doctor: {label} FAILED")
            return None
        value = result.stdout.strip().splitlines()[0] if result.stdout.strip() else ""
        log.line(f"doctor: {label} ok: {value}")
        return value

    log.line(f"doctor: python {sys.version.split()[0]} at {sys.executable}")
    if sys.version_info < (3, 11):
        problems.append(f"python is {sys.version.split()[0]}; release.py needs 3.11+")
    probe("docker", ["docker", "--version"])
    probe("docker compose", ["docker", "compose", "version", "--short"])
    probe("docker daemon", ["docker", "info", "--format", "{{.ServerVersion}}"])
    probe("systemd --user", ["systemctl", "--user", "show", "--property=Version"])

    kvm_gid: int | None = None
    if mode == "full":
        path = Path("/dev/kvm")
        if path.exists() and os.access(path, os.R_OK | os.W_OK):
            kvm_gid = path.stat().st_gid
            log.line(f"doctor: /dev/kvm ok (gid {kvm_gid})")
        else:
            problems.append("/dev/kvm is missing or not read/write for this user")
            log.line("doctor: /dev/kvm FAILED")
    else:
        log.line("doctor: skipping KVM check for core mode")

    profile = profile or (target.profile if target else "amd")
    if profile == "amd":
        for device in ("/dev/kfd", "/dev/dri"):
            path = Path(device)
            if path.exists():
                log.line(f"doctor: {device} ok (gid {path.stat().st_gid})")
            elif target is None:
                # Without a target this is information, not a requirement: the
                # cpu profile does not need the cards.
                log.line(f"doctor: {device} absent (only the amd profile needs it)")
            else:
                problems.append(f"{device} is missing; target {target.name} uses the amd profile")
                log.line(f"doctor: {device} FAILED")

    root = target_root(target) if target else RELEASES_ROOT
    try:
        root.mkdir(parents=True, exist_ok=True)
        probe_file = root / ".doctor-write-probe"
        probe_file.write_text("ok\n", encoding="utf-8")
        probe_file.unlink()
        log.line(f"doctor: releases root writable: {root}")
    except OSError as exc:
        problems.append(f"releases root is not writable: {root}: {exc}")
        log.line(f"doctor: releases root FAILED: {root}")

    if target is not None:
        if target.env_file.is_file():
            log.line(f"doctor: env file present: {target.env_file}")
            check_release_target_key(target, log)
            # The unit starts Compose with only this file, and compose.yaml
            # interpolates COGNITA_VERSION for its image names.  A target whose
            # env file lacks it cannot start at all, so this is a failure and
            # not a warning.
            if not read_env_file(target.env_file).get("COGNITA_VERSION"):
                problems.append(
                    f"{target.env_file} has no COGNITA_VERSION; the unit's Compose invocation "
                    "cannot interpolate the image names without it")
                log.line("doctor: env COGNITA_VERSION FAILED")
            else:
                log.line("doctor: env COGNITA_VERSION present")
            # 13.0 section 8: the one probe worth keeping from the deleted
            # preflight-kvm.sh.  compose.yaml gives workspace-runtime exactly
            # this supplementary group and nothing else, so a stale
            # COGNITA_KVM_GID means the broker cannot open /dev/kvm and every
            # Workspace fails to start -- while Docker, Compose and the device
            # itself all look fine, which is why it needs saying here.
            configured_kvm = read_env_file(target.env_file).get("COGNITA_KVM_GID", "")
            if mode != "full":
                log.line("doctor: skipping COGNITA_KVM_GID check for core mode")
            elif kvm_gid is None:
                log.line("doctor: env COGNITA_KVM_GID not compared (/dev/kvm is unreadable)")
            elif not configured_kvm.isdigit():
                problems.append(
                    f"{target.env_file} has no numeric COGNITA_KVM_GID; /dev/kvm is group "
                    f"{kvm_gid} on this host")
                log.line(f"doctor: env COGNITA_KVM_GID FAILED (value {configured_kvm!r})")
            elif int(configured_kvm) != kvm_gid:
                problems.append(
                    f"{target.env_file} says COGNITA_KVM_GID={configured_kvm}, but /dev/kvm is "
                    f"group {kvm_gid}; the broker would be denied the device")
                log.line(f"doctor: env COGNITA_KVM_GID FAILED ({configured_kvm} != {kvm_gid})")
            else:
                log.line(f"doctor: env COGNITA_KVM_GID matches /dev/kvm ({kvm_gid})")
        else:
            problems.append(f"env file is missing: {target.env_file}")
            log.line(f"doctor: env file FAILED: {target.env_file}")

    if problems:
        raise ReleaseError("doctor-failed", "host prerequisites failed:\n  " + "\n  ".join(problems))
    log.line("doctor: all prerequisites satisfied")


# --------------------------------------------------------------------------
# build
# --------------------------------------------------------------------------

APP_IMAGE_PREFIX = "cognita/app"
BROKER_IMAGE_PREFIX = "cognita/workspace-runtime"
TEST_IMAGE_PREFIX = "cognita/app-test"


def build_override_text(images: dict[str, str]) -> str:
    """Build-time override: the release's own image names.

    compose.yaml carries a historical literal tag, so the release names its
    own images here and nothing else.  The build stage is not set here: both
    Dockerfiles name their service stage ``app`` and compose.yaml selects it,
    for `docker compose build` by hand as much as for this script.
    """
    rows = ["# Generated by scripts/release.py -- build-time only, never staged.", "services:"]
    for service, reference in images.items():
        rows += [f"  {service}:", f"    image: {reference}"]
    return "\n".join(rows) + "\n"


def image_id(reference: str) -> str:
    """The immutable image ID.  A tag can be moved; this cannot."""
    result = subprocess.run(
        ["docker", "image", "inspect", "--format", "{{.Id}}", reference],
        capture_output=True, text=True, check=False, timeout=120,
    )
    if result.returncode:
        raise ReleaseError("build-failed", f"image is missing: {reference}")
    return result.stdout.strip()


def image_identity(reference: str) -> tuple[str, str, str]:
    """Return image ID, OCI version, and source revision for qualification reuse."""
    result = subprocess.run(
        ["docker", "image", "inspect", "--format",
         '{{.Id}} {{index .Config.Labels "org.opencontainers.image.version"}} '
         '{{index .Config.Labels "org.opencontainers.image.revision"}}', reference],
        capture_output=True, text=True, check=False, timeout=120,
    )
    if result.returncode:
        raise ReleaseError("usage", f"required image is missing: {reference}")
    parts = result.stdout.strip().split(maxsplit=2)
    if len(parts) != 3:
        raise ReleaseError("usage", f"image has incomplete Cognita identity labels: {reference}")
    return tuple(parts)  # type: ignore[return-value]


def image_exists(reference: str) -> bool:
    return subprocess.run(
        ["docker", "image", "inspect", reference],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False, timeout=120,
    ).returncode == 0


def sha256_file(path: Path) -> str:
    """Hash release archives incrementally so bundle assembly stays bounded."""
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def stage_microsandbox_wheel(repo: Path, log: Log) -> None:
    """Fetch the pinned Microsandbox wheel if the checkout does not have it.

    The workspace-runtime Dockerfile installs those exact bytes with no index,
    so the build context must carry them.  scripts/stage_microsandbox_wheel.py
    is the project's own mechanism and checks the hash against its lock; this
    only decides whether to call it.
    """
    wheel = repo / BUILD_ARTIFACTS_DIR / "microsandbox-0.7.0-cp310-abi3-manylinux_2_28_x86_64.whl"
    if wheel.is_file():
        log.line(f"build: Microsandbox wheel already staged ({wheel.stat().st_size} bytes)")
        return
    log.line("build: staging the pinned Microsandbox wheel (it is not in the checkout)")
    run([sys.executable, str(repo / "scripts" / "stage_microsandbox_wheel.py"),
         "--destination", str(repo / BUILD_ARTIFACTS_DIR)],
        log=log, state="build-failed", cwd=repo)


def build_references(target: Target, version: str, commit: str, run_id: str = "build") -> dict[str, str]:
    """Name a build by its source and target, never a shared version alias."""
    suffix = f"{version}-{target.name}-{target.profile}-{commit}-{run_id}"
    return {"cognita": f"{APP_IMAGE_PREFIX}:{suffix}",
            "workspace-runtime": f"{BROKER_IMAGE_PREFIX}:{suffix}"}


def candidate_app_reference(profile: str, version: str, commit: str) -> str:
    """Name an app image independently of the optional Workspace overlay."""
    return f"{APP_IMAGE_PREFIX}:{version}-{profile}-{commit}"


def candidate_workspace_reference(version: str, commit: str) -> str:
    return f"{BROKER_IMAGE_PREFIX}:{version}-workspace-{commit}"


def candidate_test_reference(version: str, commit: str) -> str:
    return f"{TEST_IMAGE_PREFIX}:{version}-{commit}"


_POSTGRES_IMAGE_REFERENCE = re.compile(
    r"^(?P<repository>[A-Za-z0-9][A-Za-z0-9._:/-]*):"
    r"(?P<tag>[A-Za-z0-9_][A-Za-z0-9_.-]*)@sha256:(?P<digest>[0-9a-fA-F]{64})$"
)


def postgres_service_reference(repo: Path) -> str:
    """Read the sole PostgreSQL image authority from the base Compose service."""
    compose = repo / "compose.yaml"
    if not compose.is_file():
        raise ReleaseError("usage", f"base Compose file is missing: {compose}")
    lines = compose.read_text(encoding="utf-8").splitlines()
    in_services = False
    services_seen = False
    in_postgres = False
    postgres_seen = False
    references: list[str] = []
    for line in lines:
        if re.match(r"^services:\s*(?:#.*)?$", line):
            if services_seen:
                raise ReleaseError("usage", "base Compose file has duplicate services sections")
            in_services = True
            services_seen = True
            continue
        if in_services and re.match(r"^[^\s#].*:\s*(?:#.*)?$", line):
            in_services = False
            in_postgres = False
        if not in_services:
            continue
        if re.match(r"^  postgres:\s*(?:#.*)?$", line):
            if postgres_seen:
                raise ReleaseError("usage", "base Compose file has duplicate postgres services")
            in_postgres = True
            postgres_seen = True
            continue
        if in_postgres and re.match(r"^  [A-Za-z0-9_-]+:\s*(?:#.*)?$", line):
            in_postgres = False
        if in_postgres:
            match = re.match(r"^    image:\s*(\S+)\s*(?:#.*)?$", line)
            if match:
                references.append(match.group(1))
    if len(references) != 1:
        raise ReleaseError("usage", "base Compose postgres service must declare exactly one image")
    reference = references[0]
    if not _POSTGRES_IMAGE_REFERENCE.fullmatch(reference):
        raise ReleaseError(
            "usage",
            "base Compose postgres image must use exact repository:tag@sha256:<64 hex> form",
        )
    return reference


def postgres_transport_reference(reference: str) -> str:
    """Derive Docker save's repository-preserving tag from the Compose pin."""
    match = _POSTGRES_IMAGE_REFERENCE.fullmatch(reference)
    if not match:
        raise ReleaseError("usage", "PostgreSQL image reference is not an exact digest-pinned repository:tag")
    return f"{match['repository']}:cognita-transport-{match['digest'].lower()}"


def verify_candidate_postgres(repo: Path, log: Log) -> tuple[str, str, str]:
    """Pull the Compose-owned pin, verify its immutable ID, and derive its archive tag."""
    reference = postgres_service_reference(repo)
    run(["docker", "pull", reference], log=log, state="build-failed", cwd=repo)
    image = image_id(reference)
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", image):
        raise ReleaseError("build-failed", f"PostgreSQL image has an invalid immutable image ID: {reference}")
    transport = postgres_transport_reference(reference)
    run(["docker", "tag", image, transport], log=log, state="build-failed", cwd=repo)
    log.line(f"build: verified PostgreSQL image={reference} id={image} transport={transport}")
    return reference, image, transport


def candidate_manifest(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    manifest = path / "release.txt"
    if not manifest.is_file():
        raise ReleaseError("usage", f"candidate image directory has no release.txt: {path}")
    for line in manifest.read_text(encoding="utf-8").splitlines():
        key, separator, value = line.partition(":")
        if separator:
            key = key.strip()
            if key in values:
                raise ReleaseError("usage", f"candidate release.txt repeats metadata key {key}")
            values[key] = value.strip()
    for key in ("version", "commit", "test_runner_ref", "image_ref_cognita_cpu", "image_cognita_cpu",
                "image_ref_postgres", "image_postgres"):
        if not values.get(key):
            raise ReleaseError("usage", f"candidate release.txt is missing {key}")
    if not _POSTGRES_IMAGE_REFERENCE.fullmatch(values["image_ref_postgres"]):
        raise ReleaseError("usage", "candidate release.txt has an invalid image_ref_postgres")
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", values["image_postgres"]):
        raise ReleaseError("usage", "candidate release.txt has an invalid image_postgres")
    return values


def update_cpu_full_qualification(images_dir: Path, receipt: dict | None) -> None:
    """Atomically invalidate/publish one test result without changing build identity."""
    values = candidate_manifest(images_dir)
    values.pop("qualification_cpu_full", None)
    if receipt is not None:
        values["qualification_cpu_full"] = json.dumps(receipt, sort_keys=True, separators=(",", ":"))
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=images_dir,
                                         prefix=".cognita-qualification-", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write("".join(f"{key}: {value}\n" for key, value in values.items()))
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(images_dir / "release.txt")
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def cpu_full_qualification(values: dict[str, str]) -> dict:
    """Require the canonical result to bind exactly this coherent candidate."""
    try:
        receipt = json.loads(values.get("qualification_cpu_full", ""))
    except ValueError as exc:
        raise ReleaseError("usage", "candidate lacks canonical CPU/full qualification") from exc
    bindings = ("version", "commit", "image_cognita_cpu", "image_workspace_runtime", "test_runner_id",
                "toolbox_version", "toolbox_sha256")
    if (not isinstance(receipt, dict) or receipt.get("schema") != 1 or receipt.get("mode") != "full"
            or any(not values.get(key) or receipt.get(key) != values[key] for key in bindings)
            or any(receipt.get(key) != "passed" for key in ("result", "mandatory_ocr", "missing_file_parity"))
            or receipt.get("cleanup") != "verified"
            or not isinstance(receipt.get("canonical_log"), str) or not receipt["canonical_log"]):
        raise ReleaseError("usage", "CPU/full qualification disagrees with candidate identity or mandatory results")
    return receipt


def validate_cpu_ocr_smoke(images_dir: Path, values: dict[str, str], repo: Path) -> dict:
    """Require the offline package proof, bound to the frozen source inputs."""
    proof_path = images_dir / "cpu-ocr-smoke.json"
    try:
        if proof_path.is_symlink() or sha256_file(proof_path) != values.get("cpu_ocr_smoke_sha256"):
            raise ValueError("offline CPU OCR proof checksum mismatch")
        proof = json.loads(proof_path.read_text(encoding="utf-8"))
        for proof_key, manifest_key in (("image_id", "image_cognita_cpu"), ("version", "version"), ("commit", "commit")):
            if proof.get(proof_key) != values[manifest_key]:
                raise ValueError("offline CPU OCR proof candidate mismatch")
        for proof_key, manifest_key, relative in (
            ("requirements_lock_sha256", "cpu_ocr_lock_sha256", "containers/ocr-cpu-requirements.lock"),
            ("maintenance_evidence_sha256", "cpu_ocr_maintenance_sha256", "containers/ocr-cpu-refresh-evidence.json")):
            if proof.get(proof_key) != values.get(manifest_key) or proof.get(proof_key) != sha256_file(repo / relative):
                raise ValueError("offline CPU OCR proof input mismatch")
        worker = proof["worker"]
        canonical, blank = worker["fixtures"]["canonical-clear.png"], worker["fixtures"]["blank.png"]
        if (proof.get("exit_code") != 0 or worker.get("status") != "passed" or worker.get("worker_cleanup") != "reaped"
                or not isinstance(canonical.get("regions"), int) or canonical["regions"] <= 0 or blank.get("regions") != 0
                or any(row.get("device") != "cpu" or row.get("backend") != "pytorch-cpu" for row in (canonical, blank))
                or proof["cleanup"].get("container_absent") is not True or proof["cleanup"].get("tmpfs_released") is not True):
            raise ValueError("offline CPU OCR proof inference or cleanup failed")
    except (OSError, KeyError, TypeError, ValueError) as exc:
        raise ReleaseError("usage", f"CPU OCR package proof validation failed: {exc}") from exc
    return proof


def validate_candidate(images_dir: Path, *, profile: str, mode: str,
                       version: str, commit: str, repo: Path | None = None
                       ) -> tuple[dict[str, str], str, dict[str, str]]:
    """Validate the exact accepted image identities before any no-build use."""
    if profile not in PROFILES or mode not in {"core", "full"}:
        raise ReleaseError("usage", f"unsupported candidate selection: profile={profile} mode={mode}")
    values = candidate_manifest(images_dir)
    expected_postgres = postgres_service_reference(repo or REPO_ROOT)
    if values["image_ref_postgres"] != expected_postgres:
        raise ReleaseError("usage", "candidate PostgreSQL reference does not match current compose.yaml")
    observed_postgres_id = image_id(values["image_ref_postgres"])
    if observed_postgres_id != values["image_postgres"]:
        raise ReleaseError("usage", "candidate PostgreSQL image ID differs from release.txt")
    if values["version"] != version or values["commit"] != commit:
        raise ReleaseError("usage", "candidate version/commit does not match this checkout")
    if profile == "cpu":
        validate_cpu_ocr_inputs(repo or REPO_ROOT)
        validate_cpu_ocr_smoke(images_dir, values, repo or REPO_ROOT)
    app_ref = values.get(f"image_ref_cognita_{profile}")
    app_id = values.get(f"image_cognita_{profile}")
    if not app_ref or not app_id:
        raise ReleaseError("usage", f"candidate is missing the {profile} Cognita app image")
    verify_candidate_image(app_ref, version, commit, app_id)
    refs = {"cognita": app_ref}
    if mode == "full":
        workspace_ref = values.get("image_ref_workspace_runtime")
        workspace_id = values.get("image_workspace_runtime")
        if not workspace_ref or not workspace_id:
            raise ReleaseError("usage", "candidate is missing the Workspace runtime image")
        verify_candidate_image(workspace_ref, version, commit, workspace_id)
        refs["workspace-runtime"] = workspace_ref
    fragment = images_dir / f"compose.{profile}.{mode}.images.yaml"
    expected_fragment = candidate_fragment_text(refs)
    if not fragment.is_file() or fragment.read_text(encoding="utf-8") != expected_fragment:
        raise ReleaseError("usage", f"candidate Compose image fragment does not match accepted images: {fragment}")
    test_ref = values["test_runner_ref"]
    verify_candidate_image(test_ref, version, commit, values.get("test_runner_id"))
    return refs, test_ref, values


def stage_candidate_toolbox(values: dict[str, str], target: Target, log: Log) -> Path:
    """Copy the already-built Toolbox archive into this target's release cache."""
    source = Path(values.get("toolbox_archive", ""))
    expected = values.get("toolbox_sha256", "")
    if not source.is_file() or not expected or sha256_file(source) != expected:
        raise ReleaseError("usage", "candidate Toolbox archive is missing or has changed")
    destination = toolbox_dir(target) / source.name
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.is_file() and sha256_file(destination) == expected:
        log.line(f"toolbox: accepted archive already staged for {target.name}: {destination}")
        return destination
    staging = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.staging")
    try:
        shutil.copyfile(source, staging)
        if sha256_file(staging) != expected:
            raise ReleaseError("apply-failed", "copied candidate Toolbox archive failed SHA-256 verification")
        staging.replace(destination)
    finally:
        staging.unlink(missing_ok=True)
    log.line(f"toolbox: staged accepted candidate archive for {target.name}: {destination}")
    return destination


def verify_candidate_image(reference: str, expected_version: str, expected_commit: str,
                           expected_id: str | None = None) -> str:
    observed_id, version, commit = image_identity(reference)
    if (version, commit) != (expected_version, expected_commit):
        raise ReleaseError("usage", f"image identity mismatch for {reference}: "
                           f"version={version!r} commit={commit!r}")
    if expected_id and observed_id != expected_id:
        raise ReleaseError("usage", f"image ID mismatch for {reference}: "
                           f"expected {expected_id}, found {observed_id}")
    return observed_id


def _write_candidate_fragments(directory: Path, profile: str, mode: str,
                               images: dict[str, str]) -> Path:
    fragment = directory / f"compose.{profile}.{mode}.images.yaml"
    fragment.write_text(candidate_fragment_text(images), encoding="utf-8")
    return fragment


def candidate_fragment_text(images: dict[str, str]) -> str:
    rows = ["# Generated by scripts/release.py; candidate image references.", "services:"]
    for service, reference in images.items():
        rows += [f"  {service}:", f"    image: {reference}", "    build: !reset null"]
    return "\n".join(rows) + "\n"


def _write_sha256sums(files: list[Path], destination: Path, *, relative_to: Path) -> None:
    rows = []
    for path in sorted(files, key=lambda item: item.relative_to(relative_to).as_posix()):
        digest = sha256_file(path)
        rows.append(f"{digest}  {path.relative_to(relative_to).as_posix()}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.staging")
    try:
        staging.write_text("\n".join(rows) + "\n", encoding="utf-8")
        staging.replace(destination)
    finally:
        staging.unlink(missing_ok=True)


def validate_cpu_ocr_inputs(repo: Path) -> dict:
    """Bind the frozen CPU lock to its explicit maintenance source evidence."""
    try:
        from scripts.dependency_locks import validate_cpu_ocr_lock
    except ImportError:
        from dependency_locks import validate_cpu_ocr_lock
    try:
        validate_cpu_ocr_lock(repo / "containers/cognita/runtime-lock.json", repo / "containers/ocr-cpu-requirements.lock")
        evidence = json.loads((repo / "containers/ocr-cpu-refresh-evidence.json").read_text(encoding="utf-8"))
        source = evidence["roles"]["ocr-cpu"]
        if source["lock_sha256"] != sha256_file(repo / "containers/ocr-cpu-requirements.lock"):
            raise ValueError("CPU dependency lock differs from maintenance evidence")
        for absolute, digest in evidence["consumed_source_sha256"].items():
            relative = Path(absolute).relative_to(Path(evidence["source_root"]))
            target = repo / relative
            if target.is_symlink() or not target.is_file() or sha256_file(target) != digest:
                raise ValueError(f"CPU dependency input differs from maintenance evidence: {relative}")
        vendors = source["resolver"]["vendor_artifacts"]
        if set(vendors) != {"torch", "torchvision"}:
            raise ValueError("CPU maintenance evidence lacks verified vendor artifacts")
    except (OSError, KeyError, ValueError) as exc:
        raise ReleaseError("build-failed", f"CPU OCR frozen input validation failed: {exc}") from exc
    return evidence


# 14.2.0 (DESIGN-LINUX-INSTALLER 5.8, 6.5, D5): the images no longer carry the EasyOCR weights, so
# nothing stages them into the build context any more. (Superseded: `stage_cpu_ocr_models` downloaded
# the two files into containers/cognita-amd/models/ before every CPU build and the Dockerfiles COPYed
# them; that function and the directory are gone.) Every path that needs the weights -- the OCR smoke,
# the throwaway test stack, a deploy that will apply a release -- fetches them into the TARGET's model
# cache root instead, which is the directory the running service mounts at /var/lib/cognita/models.
OCR_WEIGHTS_MOUNT = "/var/lib/cognita/models/easyocr"


def fetch_ocr_weights(models_root: Path, log: Log) -> Path:
    """Make ``<models_root>/easyocr`` hold the two hash-verified weight files; return that directory.

    ``scripts/ocr_weights.py`` is the only downloader. It skips a file that is already present with
    the right hash, so calling this on every deploy costs a hash of ~100 MB and no network."""
    destination = models_root / "easyocr"
    started = time.monotonic()
    log.line(f"ocr-weights: ensuring the EasyOCR weights in {destination}")
    try:
        ocr_weights.fetch(destination, log)
    except (ocr_weights.OcrWeightsError, OSError) as exc:
        raise ReleaseError("build-failed", f"could not fetch the EasyOCR weights into {destination}: "
                                           f"{type(exc).__name__}: {exc}. Nothing was built or applied, so "
                                           "the running release is unchanged; rerun once GitHub is "
                                           "reachable.") from exc
    log.line(f"ocr-weights: ready in {destination} ({time.monotonic() - started:.1f}s)")
    return destination


def cpu_ocr_smoke(image: str, repo: Path, log: Log, proof_path: Path | None = None, *,
                  weights_dir: Path) -> dict:
    """Run the final app as its production UID, offline, before other builds.

    ``weights_dir`` is a directory ``fetch_ocr_weights`` has filled (the image holds no weights since
    14.2.0); it is mounted READ-ONLY at ``/var/lib/cognita/models/easyocr``, exactly where the running
    service finds them (DESIGN-LINUX-INSTALLER 5.8)."""
    identifier, version, commit = image_identity(image)
    weights_dir = Path(weights_dir).resolve()
    log.line(f"ocr-smoke: image={identifier} weights {weights_dir} -> {OCR_WEIGHTS_MOUNT} (read-only)")
    name = "cognita-cpu-ocr-" + uuid.uuid4().hex
    label = "cognita.cpu-ocr-smoke=" + name
    container_id = ""
    process = None
    proof = None
    started = time.monotonic()
    try:
        created = subprocess.run(["docker", "create", "--name", name, "--label", label, "--pull=never",
                              "--user", "1000:1000", "--cap-drop=ALL", "--network", "none", "--read-only",
                              "--tmpfs", "/tmp:rw,nosuid,nodev,size=512m,mode=0700,uid=1000,gid=1000",
                              "--volume", f"{weights_dir}:{OCR_WEIGHTS_MOUNT}:ro",
                              "--env", "HOME=/tmp", "--entrypoint", "python", identifier,
                              "/opt/cognita-runtimes/verify_ocr_runtime.py", "--smoke",
                              "--python", "/opt/cognita-runtimes/ocr/bin/python",
                              "--model-dir", OCR_WEIGHTS_MOUNT,
                              "--manifest", "/opt/cognita-models/easyocr-qualification.json"],
                             capture_output=True, text=True, timeout=60, check=False)
        if not created.returncode and re.fullmatch(r"[0-9a-f]{64}", created.stdout.strip()):
            container_id = created.stdout.strip()
        else:
            raise ReleaseError("build-failed", "CPU OCR smoke container acquisition failed")
        process = subprocess.Popen(["docker", "start", "--attach", container_id], stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, text=True, start_new_session=(os.name == "posix"))
        stdout, stderr = process.communicate(timeout=180)
        if process.returncode:
            raise ReleaseError("build-failed", f"CPU OCR packaged smoke failed exit={process.returncode}: {stderr[-1200:]}")
        rows = [row for row in stdout.splitlines() if row.startswith("{")]
        facts = json.loads(rows[-1])
        if facts.get("status") != "passed":
            raise ReleaseError("build-failed", "CPU OCR packaged smoke returned invalid evidence")
        proof = {"image_id": identifier, "version": version, "commit": commit,
                 "requirements_lock_sha256": sha256_file(repo / "containers/ocr-cpu-requirements.lock"),
                 "maintenance_evidence_sha256": sha256_file(repo / "containers/ocr-cpu-refresh-evidence.json"),
                 "duration_seconds": round(time.monotonic() - started, 3), "exit_code": process.returncode,
                 "worker": facts}
    except (subprocess.TimeoutExpired, ValueError, IndexError) as exc:
        raise ReleaseError("build-failed", f"CPU OCR packaged smoke failed: {type(exc).__name__}") from exc
    finally:
        if process is not None and process.poll() is None:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
            process.communicate(timeout=10)
        # The exact recorded container owns all workers and scratch tmpfs. Docker
        # stop waits for its process tree; removal releases that tmpfs as well.
        if not container_id:
            # Docker may have acquired the container before its CLI timed out.
            # Recover only our exact unique name plus preassigned owner label.
            acquired = subprocess.run(["docker", "container", "inspect", name], capture_output=True,
                                      text=True, timeout=30, check=False)
            if acquired.returncode == 0:
                try:
                    entry = json.loads(acquired.stdout)[0]
                    if entry["Config"]["Labels"].get("cognita.cpu-ocr-smoke") != name:
                        raise ValueError("owner label mismatch")
                    container_id = entry["Id"]
                    if not re.fullmatch(r"[0-9a-f]{64}", container_id):
                        raise ValueError("invalid owned container ID")
                except (KeyError, TypeError, ValueError, IndexError) as exc:
                    raise ReleaseError("build-failed", f"CPU OCR smoke acquisition cleanup ownership unverified: {name}") from exc
            elif "No such container" not in acquired.stderr and "No such object" not in acquired.stderr:
                raise ReleaseError("build-failed", f"CPU OCR smoke acquisition cleanup absence unverified: {name}")
        if container_id:
            subprocess.run(["docker", "stop", "--time", "10", container_id], capture_output=True, timeout=30, check=False)
            removed = subprocess.run(["docker", "rm", container_id], capture_output=True, timeout=30, check=False)
            remains = subprocess.run(["docker", "container", "inspect", container_id], capture_output=True, timeout=30, check=False)
            if removed.returncode or remains.returncode == 0:
                raise ReleaseError("build-failed", f"CPU OCR smoke cleanup failed: {container_id}")
    proof["cleanup"] = {"container_id": container_id, "container_absent": True, "tmpfs_released": True}
    if proof_path:
        proof_path.write_text(json.dumps(proof, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    log.line(f"ocr-smoke: PASSED image={identifier} uid=1000 network=none cleanup=verified")
    return proof


MICROSANDBOX_BASE_IMAGE = (
    "ghcr.io/superradcompany/microsandbox:0.7.0"
    "@sha256:fed39d2863121ac1fec25bdeaaeab9498f48ee381294faa8fe5805b0e9ac0fdc")


def docker_build_app(repo: Path, profile: str, version: str, commit: str, reference: str,
                     log: Log) -> None:
    """The plain app-image build.  Shared by the candidate build and `publish`."""
    # 15.0.0: `nvidia` builds containers/cognita-nvidia/Dockerfile; cpu is the plain containers/cognita one.
    directory = {"amd": "cognita-amd", "nvidia": "cognita-nvidia"}.get(profile, "cognita")
    dockerfile = repo / "containers" / directory / "Dockerfile"
    run(["docker", "build", "-f", str(dockerfile), "--target", "app",
         "--build-arg", f"COGNITA_VERSION={version}", "--build-arg", f"COGNITA_COMMIT={commit}",
         "-t", reference, str(repo)], log=log, state="build-failed", cwd=repo)


def docker_build_workspace_runtime(repo: Path, version: str, commit: str, reference: str,
                                   log: Log) -> None:
    """The plain Workspace-runtime build.  Shared by the candidate build and `publish`."""
    dockerfile = repo / "containers" / "workspace-runtime" / "Dockerfile"
    run(["docker", "build", "-f", str(dockerfile), "--build-arg",
         f"MICROSANDBOX_IMAGE={MICROSANDBOX_BASE_IMAGE}",
         "--build-arg", f"COGNITA_VERSION={version}", "--build-arg", f"COGNITA_COMMIT={commit}",
         "-t", reference, str(repo)], log=log, state="build-failed", cwd=repo)


def build_candidate_images(repo: Path, target: Target, version: str, commit: str,
                           profiles: list[str], images_dir: Path, cpu_archive: Path,
                           sums_path: Path, log: Log) -> None:
    """Build each accepted profile and the reusable test runner exactly once."""
    invalid = set(profiles) - set(PROFILES)
    if invalid or "cpu" not in profiles or len(set(profiles)) != len(profiles):
        raise ReleaseError("usage", "--profiles must contain cpu and may also contain amd and nvidia")
    validate_cpu_ocr_inputs(repo)
    # The smoke and the candidate test stack both read the weights from the target's model cache.
    weights_dir = fetch_ocr_weights(target_models_root(target.env_file), log)
    profiles = ["cpu"] + [profile for profile in GPU_PROFILES if profile in profiles]   # cpu stays first
    gpu_built = any(profile in GPU_PROFILES for profile in profiles)
    log.line(f"build: profiles={profiles} gpu_profiles_built={gpu_built}")
    images_dir.mkdir(parents=True, exist_ok=True)
    manifest: list[tuple[str, str]] = [
        ("version", version), ("commit", commit), ("built_at", dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")),
    ]
    for profile in profiles:
        if profile in GPU_PROFILES:
            stage_microsandbox_wheel(repo, log)
        app_ref = candidate_app_reference(profile, version, commit)
        log.line(f"build: profile={profile} app image={app_ref}")
        docker_build_app(repo, profile, version, commit, app_ref, log)
        app_id = verify_candidate_image(app_ref, version, commit)
        if profile == "cpu":
            cpu_ocr_smoke(app_id, repo, log, images_dir / "cpu-ocr-smoke.json", weights_dir=weights_dir)
            manifest.extend((("cpu_ocr_smoke_sha256", sha256_file(images_dir / "cpu-ocr-smoke.json")),
                             ("cpu_ocr_lock_sha256", sha256_file(repo / "containers/ocr-cpu-requirements.lock")),
                             ("cpu_ocr_maintenance_sha256", sha256_file(repo / "containers/ocr-cpu-refresh-evidence.json"))))
        manifest.extend(((f"image_ref_cognita_{profile}", app_ref),
                         (f"image_cognita_{profile}", app_id)))

    workspace_ref = None
    if gpu_built:
        workspace_ref = candidate_workspace_reference(version, commit)
        log.line(f"build: shared Workspace runtime image={workspace_ref}")
        docker_build_workspace_runtime(repo, version, commit, workspace_ref, log)
        workspace_id = verify_candidate_image(workspace_ref, version, commit)
        manifest.extend((("image_ref_workspace_runtime", workspace_ref),
                         ("image_workspace_runtime", workspace_id)))
        toolbox_version = read_toolbox_version(repo, log)
        toolbox_archive = build_toolbox(repo, target, toolbox_version, log)
        manifest.extend((("toolbox_version", toolbox_version),
                         ("toolbox_archive", str(toolbox_archive)),
                         ("toolbox_sha256", sha256_file(toolbox_archive))))

    for profile in profiles:
        for mode in (("core", "full") if workspace_ref else ("core",)):
            refs = {"cognita": candidate_app_reference(profile, version, commit)}
            if mode == "full":
                refs["workspace-runtime"] = workspace_ref
            fragment = _write_candidate_fragments(images_dir, profile, mode, refs)
            manifest.append((f"fragment_{profile}_{mode}", fragment.name))

    test_ref = candidate_test_reference(version, commit)
    log.line(f"build: reusable test runner image={test_ref}")
    run(["docker", "build", "-f", str(repo / "containers" / "cognita" / "Dockerfile"),
         "--target", "test", "--build-arg", f"COGNITA_VERSION={version}",
         "--build-arg", f"COGNITA_COMMIT={commit}", "-t", test_ref, str(repo)],
        log=log, state="build-failed", cwd=repo)
    test_id = verify_candidate_image(test_ref, version, commit)
    manifest.extend((("test_runner_ref", test_ref), ("test_runner_id", test_id)))

    postgres_ref, postgres_id, postgres_transport = verify_candidate_postgres(repo, log)
    manifest.extend((("image_ref_postgres", postgres_ref), ("image_postgres", postgres_id)))

    cpu_ref = candidate_app_reference("cpu", version, commit)
    cpu_archive.parent.mkdir(parents=True, exist_ok=True)
    temp_archive = cpu_archive.with_name(f".{cpu_archive.name}.{uuid.uuid4().hex}.staging")
    try:
        run(["docker", "save", "--output", str(temp_archive), cpu_ref, postgres_transport],
            log=log, state="build-failed", cwd=repo)
        temp_archive.replace(cpu_archive)
    finally:
        temp_archive.unlink(missing_ok=True)
    digest = sha256_file(cpu_archive)
    manifest.extend((("cpu_archive", str(cpu_archive)), ("cpu_archive_sha256", digest)))
    if gpu_built:
        workspace_archive = images_dir / "workspace-runtime.tar"
        workspace_staging = workspace_archive.with_name(f".{workspace_archive.name}.{uuid.uuid4().hex}.staging")
        try:
            run(["docker", "save", "--output", str(workspace_staging), workspace_ref],
                log=log, state="build-failed", cwd=repo)
            workspace_staging.replace(workspace_archive)
        finally:
            workspace_staging.unlink(missing_ok=True)
        manifest.extend((("workspace_archive", workspace_archive.name),
                         ("workspace_archive_sha256", sha256_file(workspace_archive))))
    (images_dir / "release.txt").write_text("".join(f"{k}: {v}\n" for k, v in manifest), encoding="utf-8")
    _write_sha256sums([cpu_archive], sums_path, relative_to=cpu_archive.parent)
    log.line(f"build: CPU archive={cpu_archive} sha256={digest}")


def build_images(repo: Path, target: Target, version: str, commit: str, tmp: Path,
                 log: Log, *, run_id: str = "build", mode: str = "full") -> tuple[dict[str, str], dict[str, str]]:
    """Build the app and broker images.  BuildKit is the only build cache."""
    weights_dir: Path | None = None
    if target.profile == "cpu":
        validate_cpu_ocr_inputs(repo)
        # The OCR smoke below needs the weights; the image does not carry them (14.2.0).
        weights_dir = fetch_ocr_weights(target_models_root(target.env_file), log)
    if mode == "full":
        stage_microsandbox_wheel(repo, log)
    override = tmp / "compose.build.yaml"
    images = build_references(target, version, commit, run_id)
    if mode == "core":
        images = {"cognita": images["cognita"]}
    override.write_text(build_override_text(images), encoding="utf-8")
    files = checkout_compose_files(repo, target.profile, mode)
    services = ["cognita"] + (["workspace-runtime"] if mode == "full" else [])
    command = compose_command(
        project=target.project, env_file=target.env_file, files=files, extra_files=[override]
    ) + [
        "build",
        "--build-arg", f"COGNITA_VERSION={version}",
        "--build-arg", f"COGNITA_COMMIT={commit}",
        *services,
    ]
    log.line(f"build: {', '.join(images.values())} (profile {target.profile}, mode {mode})")
    run(command, log=log, state="build-failed", cwd=repo)
    ids = {service: image_id(images[service]) for service in services}
    if target.profile == "cpu":
        assert weights_dir is not None  # fetched above for exactly this profile
        cpu_ocr_smoke(ids["cognita"], repo, log, weights_dir=weights_dir)
    log.line(f"build: cognita={ids['cognita']}")
    if "workspace-runtime" in ids:
        log.line(f"build: workspace-runtime={ids['workspace-runtime']}")
    return images, ids


def toolbox_tag(toolbox_version: str) -> str:
    """The one tag the broker's loader accepts for the Toolbox image."""
    return f"cognita-workspace-toolbox:{toolbox_version}"


def build_toolbox(repo: Path, target: Target, toolbox_version: str, log: Log) -> Path:
    """Build and export the Toolbox archive, only when it is not already here.

    The broker's loader accepts exactly tag ``cognita-workspace-toolbox:<v>``
    and archive ``toolbox-<v>.tar``; that contract is not ours to change
    (section 6.2 step 2).
    """
    archive = toolbox_dir(target) / f"toolbox-{toolbox_version}.tar"
    if archive.is_file():
        log.line(f"toolbox: archive already staged, not rebuilding: {archive}")
        return archive
    tag = toolbox_tag(toolbox_version)
    if image_exists(tag):
        log.line(f"toolbox: image {tag} already present; exporting it")
    else:
        log.line(f"toolbox: building {tag}")
        run(
            [
                "docker", "build", "-f", str(repo / "containers" / "workspace-toolbox" / "Dockerfile"),
                "--build-arg", f"COGNITA_VERSION={toolbox_version}", "-t", tag, str(repo),
            ],
            log=log, state="build-failed", cwd=repo,
        )
    archive.parent.mkdir(parents=True, exist_ok=True)
    staging = archive.with_suffix(".tar.staging")
    run(["docker", "save", tag, "-o", str(staging)], log=log, state="build-failed", cwd=repo)
    staging.replace(archive)
    log.line(f"toolbox: exported {tag} to {archive} ({archive.stat().st_size} bytes)")
    return archive


def target_models_root(env_file: Path) -> Path:
    """The model cache named in that env file.

    The throwaway stack mounts this read-write (§7.1 step 3), so it is read
    from the target's own descriptor rather than assumed: a target that keeps
    its models somewhere else must still share them, and a target that names
    none is a target whose service could not start either.
    """
    configured = read_env_file(env_file).get("COGNITA_MODEL_CACHE_ROOT")
    if not configured:
        raise ReleaseError("usage", f"{env_file} names no COGNITA_MODEL_CACHE_ROOT")
    return Path(configured)


def toolbox_cache_root(env_file: Path) -> Path:
    """Where the broker expects the Toolbox archive, per that env file.

    The container mounts exactly this path at /var/lib/cognita/toolbox-cache,
    so the archive must be copied HERE and nowhere else.  Reading it rather
    than assuming a layout is what keeps the generated throwaway installation
    and the real targets on the same footing -- assuming cost one failed test
    run on September 22, when the generator moved and this did not.
    """
    env = read_env_file(env_file)
    configured = env.get("COGNITA_TOOLBOX_IMAGE_CACHE_ROOT")
    if configured:
        return Path(configured)
    workspace_root = env.get("COGNITA_WORKSPACE_DATA_ROOT", "")
    if not workspace_root:
        raise ReleaseError("usage", f"{env_file} names neither a toolbox cache root nor a workspace root")
    return Path(workspace_root) / "toolbox-cache"


def load_toolbox(
    *, compose: list[str], repo: Path, cache_root: Path, archive: Path, toolbox_version: str, log: Log
) -> None:
    """Copy the archive into the target's toolbox-cache root and import it.

    Microsandbox keeps its own image store, so the archive has to be imported
    through the broker's loader inside workspace-runtime; the container never
    gets a Docker socket.  ``materialize-binding`` then ``verify`` then, only
    when verify says it is not there, ``load`` -- the same order
    scripts/bootstrap-toolbox-cache.sh used from 12.6.0 until 13.0 section 8
    deleted it as redundant with this function.
    """
    cache_root.mkdir(parents=True, exist_ok=True)
    target_archive = cache_root / archive.name
    if target_archive.is_file() and target_archive.stat().st_size == archive.stat().st_size:
        log.line(f"toolbox: archive already in the cache root: {target_archive}")
    else:
        staging = cache_root / (archive.name + ".staging")
        shutil.copyfile(archive, staging)
        staging.replace(target_archive)
        log.line(f"toolbox: copied archive into the cache root: {target_archive}")
    in_container = f"/var/lib/cognita/toolbox-cache/toolbox-{toolbox_version}.tar"
    base = compose + ["run", "--pull", "never", "--rm", "--no-deps", "workspace-runtime", "python3", "-m",
                      "cognita.runtime_broker.image_cache"]
    run(base + ["materialize-binding", "--archive", in_container], log=log, state="apply-failed", cwd=repo)
    code, _tail = run(base + ["verify", "--archive", in_container], log=log, state="apply-failed",
                      cwd=repo, check=False)
    if code == 0:
        log.line("toolbox: image is already imported in the Microsandbox cache; skipping load")
        return
    log.line(f"toolbox: verify said not imported ({code}); loading")
    run(base + ["load", "--archive", in_container], log=log, state="apply-failed", cwd=repo)


# --------------------------------------------------------------------------
# stage
# --------------------------------------------------------------------------


def release_tag(prefix: str, version: str, commit: str, target: Target | None = None) -> str:
    """The tag a staged release owns; no target means the legacy tag.

    New tags include target and profile; older releases retain their shared
    version/commit tags until every staged consumer is retired.
    """
    if target is None:
        return f"{prefix}:{version}-{commit[:12]}"
    return f"{prefix}:{version}-{target.name}-{target.profile}-{commit}"


def release_tags(version: str, commit: str, target: Target | None = None) -> dict[str, str]:
    return {
        "cognita": release_tag(APP_IMAGE_PREFIX, version, commit, target),
        "workspace-runtime": release_tag(BROKER_IMAGE_PREFIX, version, commit, target),
    }


def recorded_release_tags(values: dict[str, str]) -> dict[str, str]:
    """Read current refs verbatim, or derive tags for an older release.txt."""
    keys = {"cognita": "image_ref_cognita", "workspace-runtime": "image_ref_workspace_runtime"}
    if values.get("mode") == "core":
        # A core release (design 5.2) has no Workspace runtime image at all.
        keys = {"cognita": keys["cognita"]}
    present = [key for key in keys.values() if key in values]
    if present and (len(present) != len(keys) or not all(values[key] for key in present)):
        raise ReleaseError("usage", "release.txt has incomplete image references")
    if present:
        return {service: values[key] for service, key in keys.items()}
    legacy = release_tags(values.get("version", ""), values.get("commit", ""))
    return {service: legacy[service] for service in keys}


def compose_images_text(version: str, commit: str, images: dict[str, str]) -> str:
    """Reference the release's own image tags and clear the build sections.

    ``!reset null`` is Compose's own way to drop an inherited mapping;
    ``postgres`` is already digest-pinned in compose.yaml and is not repeated
    here.

    Superseded, 2026-09-22: this used to pin the immutable image ID, on the
    reasoning that a retag could then never change what a release starts.
    Measured on kei with Docker 29.6.1, that is worse than the problem it
    solves -- a FULLY CACHED ``compose build`` still re-exports the image with
    a NEW ID, the old ID survives only while a container references it, and the
    moment those containers stop the daemon drops it.  A reset of the `test`
    target hit exactly that: `No such image: sha256:64e92e57...` from a
    release that had started fine an hour earlier, because a later build at the
    same version had orphaned its pin.  A per-release TAG cannot be orphaned:
    Docker never deletes a tagged image, and nothing but this release's own
    `prune` ever removes that tag.  The ID is still recorded in release.txt,
    where `status` reports it and no startup depends on it.
    """
    rows = [f"# Generated by scripts/release.py for {version} ({commit}).",
            "# Per-release tags are owned by this release directory.", "services:"]
    for service, reference in images.items():
        rows += [f"  {service}:", f"    image: {reference}", "    build: !reset null"]
    return "\n".join(rows) + "\n"


def release_text(
    *, target: Target, version: str, commit: str, ids: dict[str, str],
    tags: dict[str, str], toolbox_version: str, tested: bool, mode: str = "full",
    published_refs: list[str] | None = None,
) -> str:
    """release.txt.  A core release (design 5.2) has no Workspace runtime, so its
    two workspace-runtime rows are absent -- until 13.7.1 they were unconditional
    and a core staging raised KeyError.  ``published_refs`` (design 5.4) is the
    registry digest references a published install pulled, so uninstall can
    remove them; it is absent for a release built from source."""
    built = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    rows: list[tuple[str, str]] = [
        ("version", version),
        ("commit", commit),
        ("target", target.name),
        ("profile", target.profile),
        ("mode", mode),
        ("image_cognita", ids["cognita"]),
    ]
    if "workspace-runtime" in ids:
        rows.append(("image_workspace_runtime", ids["workspace-runtime"]))
    rows.append(("image_ref_cognita", tags["cognita"]))
    if "workspace-runtime" in tags:
        rows.append(("image_ref_workspace_runtime", tags["workspace-runtime"]))
    rows += [
        ("toolbox_version", toolbox_version),
        ("built_at", built),
        ("full_suite", "yes" if tested else "no"),
    ]
    if published_refs:
        rows.append(("published_refs", " ".join(published_refs)))
    return "".join(f"{key}: {value}\n" for key, value in rows)


def read_release_text(directory: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    path = directory / "release.txt"
    if not path.is_file():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        key, _, value = line.partition(":")
        if value:
            values[key.strip()] = value.strip()
    return values


def check_version_free(target: Target, version: str, commit: str, log: Log) -> None:
    """Refuse a version directory built from a different commit (section 6.2).

    Same commit is a rerun and simply restages.  A different commit under the
    same version is the mistake this refusal exists for: bump the version.
    """
    directory = release_dir(target, version)
    if not directory.exists():
        return
    recorded = read_release_text(directory).get("commit", "")
    if recorded and recorded != commit:
        raise ReleaseError(
            "version-exists",
            f"{directory} already exists and was built from {recorded}, not {commit}. "
            "Bump the version in release_identity.py and rerun.",
        )
    log.line(f"stage: {directory} exists at the same commit; restaging it")


def tag_release_images(ids: dict[str, str], target: Target, version: str,
                       commit: str, log: Log) -> dict[str, str]:
    """Give this release's images their own tags, by immutable ID.

    Tagging by ID, not by the moving ``cognita/app:<version>`` tag, so a
    concurrent rebuild cannot make this release point at a different build.
    Re-staging the same version and commit re-tags to the newer build of that
    same commit, which is the same content.
    """
    tags = {service: tag for service, tag in release_tags(version, commit, target).items()
            if service in ids}
    for service, tag in tags.items():
        run(["docker", "tag", ids[service], tag], log=log, state="build-failed")
        log.line(f"stage: tagged {service} {ids[service]} as {tag}")
    return tags


def stage_release(
    repo: Path, target: Target, version: str, commit: str, ids: dict[str, str],
    toolbox_version: str, tested: bool, log: Log, *, mode: str = "full",
    published_refs: list[str] | None = None,
) -> Path:
    """Write <releases>/<target>/<version>/ atomically via a .staging directory.

    Tags first: the release directory must never name an image that was not
    tagged, and tagging is the cheap, repeatable half.
    """
    log.line(f"stage: {target.name} {version} ({commit}) mode={mode} "
             f"images={','.join(sorted(ids))} published_refs={len(published_refs or [])}")
    tags = tag_release_images(ids, target, version, commit, log)
    final = release_dir(target, version)
    staging = final.with_name(final.name + ".staging")
    if staging.exists():
        # A crashed run leaves this behind; it is not a state file and carries
        # nothing worth keeping.
        log.line(f"stage: removing a leftover staging directory from an earlier run: {staging}")
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    for source in checkout_compose_files(repo, target.profile, mode):
        shutil.copyfile(source, staging / source.name)
        log.line(f"stage: copied {source.name}")
    (staging / "compose.images.yaml").write_text(compose_images_text(version, commit, tags), encoding="utf-8")
    (staging / "release.txt").write_text(
        release_text(target=target, version=version, commit=commit, ids=ids, tags=tags,
                     toolbox_version=toolbox_version, tested=tested, mode=mode,
                     published_refs=published_refs),
        encoding="utf-8",
    )
    write_folders_fragment(target, staging, log)
    if final.exists():
        replaced = final.with_name(final.name + f".replaced-{dt.datetime.now():%Y%m%d%H%M%S}")
        final.rename(replaced)
        staging.rename(final)
        shutil.rmtree(replaced)
        log.line(f"stage: replaced the existing {final} (same commit)")
    else:
        staging.rename(final)
    log.line(f"stage: {final} ready")
    return final


def _yaml_double(text: str) -> str:
    """A YAML double-quoted scalar.  JSON string syntax is a subset of it."""
    return json.dumps(text, ensure_ascii=False)


def _yaml_single(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


def write_folders_fragment(target: Target, release_directory: Path, log: Log | None = None) -> Path | None:
    """Write ``<release>/compose.folders.yaml`` for `local` only (design 5.3).

    It binds COGNITA_PROJECTS_ROOT_2 .. _9 (the first root is bound by
    compose.yaml) and the installer-owned Self-Test root, and names every USER
    root in COGNITA_DOCUMENT_ROOTS -- never the Self-Test root.  Table targets
    get nothing, so kei's staged releases and rendered units do not change.
    `cognita add-folder` rewrites it in place; no lock is taken here.
    """
    log = log or Log(None)
    if target.name != LOCAL_TARGET:
        return None
    roots = document_roots(read_env_file(target.env_file))
    for key, path in roots:
        # Documents roots are Linux paths whatever host runs the unit tests.
        if not PurePosixPath(path).is_absolute() or any(ch in path for ch in _FORBIDDEN_ROOT_CHARS):
            raise ReleaseError(
                "usage",
                f"{key} is not a usable documents root ({path!r}): it must be absolute and contain "
                "no newline, $, quote, backslash or %")
    extras = [(key, path) for key, path in roots if key != DOCUMENT_ROOT_KEYS[0]]
    selftest = selftest_root(target)
    # A bind source that is missing is created by Docker as an empty ROOT-owned
    # directory, which the proof (running as the user) could then not write
    # into.  The user roots get an ExecStartPre mkdir (design 7.3); this one is
    # installer-owned, so it is created here, as the user, before any start.
    selftest.mkdir(parents=True, exist_ok=True)
    values = read_env_file(target.env_file)
    # Design 19.2: display text per root, only when one exists.  COGNITA_DOCUMENT_ROOTS stays a plain JSON
    # list of paths (an older image after a rollback still reads it); the displays are a second variable an
    # older image simply ignores.  A display is written with the root that owns it, so it can never be
    # attached to a path the env file no longer lists.
    displays = {path: values[DOCUMENT_DISPLAY_KEYS[key]] for key, path in roots
                if values.get(DOCUMENT_DISPLAY_KEYS[key])}
    for path, display in displays.items():
        # The CLI refuses these when it writes the env file; this guards a hand-edited one, because
        # Compose interpolates `$` in this file too and a quote or newline would end the YAML string.
        if any(ch in display for ch in _FORBIDDEN_DISPLAY_CHARS):
            raise ReleaseError(
                "usage", f"the display text for {path} is not usable ({display!r}): it must contain no "
                "newline, $ or double quote")
    command_text = command_name(values)
    if not _COMMAND_NAME.fullmatch(command_text):
        raise ReleaseError("usage", f"{COMMAND_KEY} is not a usable command name ({command_text!r}): "
                           "use letters, digits, dot, slash, underscore and hyphen only")
    # Design 19.6: the command name and the target name reach the app AND the Workspace runtime, so the
    # reset hints they print name what the user types.  The runtime service exists only in a full release;
    # naming it in a core release would define a service with no image, which Compose refuses.
    command = command_text
    passed_through = [f"      COGNITA_COMMAND: {_yaml_double(command)}",
                      f"      COGNITA_RELEASE_TARGET: {_yaml_double(target.name)}"]
    runtime_in_release = release_mode(release_directory) == "full"
    rows = [
        "# Generated by release.py from the env file; rewritten by `cognita add-folder`.",
        "services:",
        "  cognita:",
        "    environment:",
        "      COGNITA_DOCUMENT_ROOTS: "
        + _yaml_single(json.dumps([path for _key, path in roots], ensure_ascii=False)),
        *([("      COGNITA_DOCUMENT_ROOT_DISPLAYS: "
            + _yaml_single(json.dumps(displays, ensure_ascii=False)))] if displays else []),
        *passed_through,
        "    volumes:",
    ]
    for _key, path in extras:
        quoted = _yaml_double(path)
        rows.append(f"      - {{type: bind, source: {quoted}, target: {quoted}, "
                    "bind: {propagation: rslave}}")
    quoted = _yaml_double(str(selftest))
    rows.append(f"      - {{type: bind, source: {quoted}, target: {quoted}}}")
    if runtime_in_release:
        rows += ["  workspace-runtime:", "    environment:", *passed_through]
    fragment = release_directory / "compose.folders.yaml"
    fragment.write_text("\n".join(rows) + "\n", encoding="utf-8", newline="\n")
    log.line(f"folders: wrote {fragment}: {len(roots)} documents root(s) "
             f"({len(extras)} extra bind(s), {len(displays)} display(s)), self-test root {selftest}, "
             f"command={command} runtime_service={runtime_in_release}")
    return fragment


# --------------------------------------------------------------------------
# Stage from published images (DESIGN-LINUX-INSTALLER section 5.4)
# --------------------------------------------------------------------------

# containers/published-release.txt: written only by `release.py publish` (run by
# Doug on kei) and read by `stage_published`.  A module constant so a test can
# point at a temporary file.
PUBLISHED_RELEASE = REPO_ROOT / "containers" / "published-release.txt"

_DIGEST_REFERENCE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]*@sha256:[0-9a-f]{64}$")
_PUBLISHED_REQUIRED = ("version", "commit", "toolbox_version", "image_ref_cognita_cpu",
                       "image_ref_workspace_runtime", "image_ref_toolbox")


def read_published_release(path: Path) -> dict[str, str]:
    """Parse published-release.txt (`key: value` lines, like release.txt)."""
    if not path.is_file():
        raise ReleaseError(
            "usage", f"{path} is missing: this checkout names no published Cognita images to install.")
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        key, separator, value = line.partition(":")
        if not separator:
            continue
        key = key.strip()
        if key in values:
            raise ReleaseError("usage", f"{path} repeats the key {key}")
        values[key] = value.strip()
    for key in _PUBLISHED_REQUIRED:
        if not values.get(key):
            raise ReleaseError("usage", f"{path} is missing {key}")
    if not re.fullmatch(r"[0-9a-f]{40}", values["commit"]):
        raise ReleaseError("usage", f"{path} has a commit that is not a full SHA-1: {values['commit']!r}")
    for key, value in values.items():
        if key.startswith("image_ref_") and not _DIGEST_REFERENCE.fullmatch(value):
            raise ReleaseError("usage", f"{path}: {key} is not a repository@sha256:<64 hex> reference")
    return values


def published_release_text(values: list[tuple[str, str]]) -> str:
    return "".join(f"{key}: {value}\n" for key, value in values)


def git_show_file(repo: Path, commit: str, relative: str, log: Log) -> bytes:
    """One file exactly as it was at ``commit``.  Not HEAD: see stage_published."""
    result = subprocess.run(["git", "-C", str(repo), "show", f"{commit}:{relative}"],
                            capture_output=True, check=False, timeout=60)
    if result.returncode:
        detail = result.stderr.decode("utf-8", "replace").strip()
        raise ReleaseError(
            "usage",
            f"cannot read {relative} at the published commit {commit} from this clone ({detail}). "
            "Run git fetch --unshallow, then retry.")
    log.line(f"stage: read {relative} at {commit} ({len(result.stdout)} bytes)")
    return result.stdout


def _label_version(reference: str) -> str:
    result = subprocess.run(
        ["docker", "image", "inspect", "--format",
         '{{index .Config.Labels "org.opencontainers.image.version"}}', reference],
        capture_output=True, text=True, check=False, timeout=120)
    if result.returncode:
        raise ReleaseError("usage", f"required image is missing: {reference}")
    return result.stdout.strip()


def _ensure_image(reference: str, log: Log) -> None:
    """Pull only what is not already here, so an offline rerun works (design 4)."""
    if image_exists(reference):
        log.line(f"stage: image already present, not pulling: {reference}")
        return
    log.line(f"stage: pulling {reference}")
    run(["docker", "pull", reference], log=log, state="build-failed")


def repo_has_git(repo: Path) -> bool:
    """True when ``repo`` is a git checkout (``.git`` is a directory, or a file for a worktree).

    The Windows installer's WSL image carries the source tree at the published commit and no ``.git``
    (design 19.4); a seam so a test can say which it is."""
    return (repo / ".git").exists()


# Design 19.9 item 8: the Windows image's tree carries this file at its root, written when the image was
# built: `key: value` lines, `commit: <full sha>` being the commit the tree was copied at.
TREE_STAMP_NAME = ".cognita-tree"


def read_tree_stamp(repo: Path, log: Log) -> dict[str, str] | None:
    """The ``.cognita-tree`` stamp of a tree, or None when the tree has none.

    A stamp that exists but cannot be used (unreadable, no ``commit``, or a commit that is not a full
    SHA-1) STOPS the staging rather than being ignored: a stamp is the tree saying which commit it is,
    and falling back to the weaker version check would let a damaged one pass."""
    path = repo / TREE_STAMP_NAME
    if not path.is_file():
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ReleaseError("usage", f"{path} cannot be read ({type(exc).__name__}: {exc}); the tree's "
                                    "identity is unknown.") from exc
    values: dict[str, str] = {}
    for line in text.splitlines():
        key, separator, value = line.partition(":")
        if separator and key.strip() and key.strip() not in values:
            values[key.strip()] = value.strip()
    commit = values.get("commit", "")
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ReleaseError("usage", f"{path} has no usable `commit:` line (a full 40-character SHA-1); the "
                                    "tree's identity is unknown.")
    log.line(f"stage: {path.name} says this tree is commit {commit}")
    return values


def read_published_compose_file(repo: Path, commit: str, name: str, version: str, log: Log) -> bytes:
    """One Compose file of the published release.

    With a ``.cognita-tree`` stamp (design 19.9 item 8, the Windows image's tree): the stamp's commit must
    EQUAL the published commit, and the file is read from the tree.  That replaces the version check below
    for such a tree, because a commit is a stronger statement than a version string (main is ahead of the
    last publish, and every deploy bumps the version, but a commit names exactly one tree).

    Without a stamp, with a ``.git``: ``git show <commit>:<name>``, exactly as before (design 5.4).  With
    neither (design 19.4) the tree IS the published commit, so the file is read from it -- but only after
    checking that the tree's version is the published version, because a tree that is not the published
    release would silently stage the wrong Compose files.  Nothing changes for a checkout without a stamp."""
    stamp = read_tree_stamp(repo, log)
    if stamp is not None:
        if stamp["commit"] != commit:
            raise ReleaseError(
                "usage",
                f"This Cognita tree is commit {stamp['commit']} but its published images are commit {commit}; "
                "the tree and the file must come from the same release.")
        return _read_tree_file(repo, name, log,
                               f"the {TREE_STAMP_NAME} commit {commit} matches the published one")
    if repo_has_git(repo):
        return git_show_file(repo, commit, name, log)
    tree_version = read_version(repo, log)
    if tree_version != version:
        raise ReleaseError(
            "usage",
            f"This Cognita tree is {tree_version} but its published images are {version}; the tree and "
            "the file must come from the same release.")
    return _read_tree_file(repo, name, log, f"no .git in {repo}; version {tree_version} matches the published "
                                            f"{version}")


def _read_tree_file(repo: Path, name: str, log: Log, why: str) -> bytes:
    """One file of the tree at ``repo``, the tree having been checked to be the published release."""
    path = repo / name
    if not path.is_file():
        raise ReleaseError("usage", f"{path} is missing from this Cognita tree (it is read from the tree, "
                                    "not from git, at the published release).")
    data = path.read_bytes()
    log.line(f"stage: read {name} from the tree ({why}), {len(data)} bytes")
    return data


def stage_published(target: Target, log: Log,
                    on_image: Callable[[str, int | None], None] | None = None) -> Path:
    """Stage the release containers/published-release.txt names (design 5.4).

    The install is what that file names, not what HEAD is: `main` is normally
    ahead of the last publish because every kei deploy bumps the version, and
    the checkout is never moved.  So the Compose files come from the published
    COMMIT (`git show`), and the images by digest.  Stops at staging: it does
    not apply, verify or run QA (the CLI owns credentials, models, linger and
    the proof).  Takes no lock; the caller holds the target's.

    ``on_image(reference, size_bytes)`` (design 19.1) is called after each image is present, pulled
    or already there, with its compressed size as published-release.txt records it (None for the
    PostgreSQL image, which is not ours to publish).  The install screen turns that into download
    progress; without a callback nothing changes.
    """
    published = read_published_release(PUBLISHED_RELEASE)

    def announce(reference: str, size_key: str | None) -> None:
        if on_image is None:
            return
        raw = published.get(size_key, "") if size_key else ""
        size = int(raw) if raw.isdigit() else None
        try:
            on_image(reference, size)
        except Exception as exc:  # noqa: BLE001 - a progress report must never fail the staging
            log.line(f"stage: the on_image callback failed for {reference}: {type(exc).__name__}: {exc}")

    version, commit, toolbox_version = published["version"], published["commit"], published["toolbox_version"]
    mode = staging_mode(target, log)
    profile = target.profile
    log.line(f"stage: published release {version} ({commit}) toolbox {toolbox_version}; "
             f"target={target.name} profile={profile} mode={mode}")
    export_version(version, log)
    check_version_free(target, version, commit, log)

    app_key = f"image_ref_cognita_{profile}"
    if not published.get(app_key):
        raise ReleaseError("usage", f"{PUBLISHED_RELEASE} publishes no {profile} app image ({app_key})")
    refs = {"cognita": published[app_key]}
    if mode == "full":
        refs["workspace-runtime"] = published["image_ref_workspace_runtime"]
    sizes = {"cognita": f"size_cognita_{profile}", "workspace-runtime": "size_workspace_runtime"}
    for service, reference in refs.items():
        _ensure_image(reference, log)
        announce(reference, sizes[service])
        verify_candidate_image(reference, version, commit)
        log.line(f"stage: {service} labels match {version} ({commit}): {reference}")
    ids = {service: image_id(reference) for service, reference in refs.items()}
    pulled = list(refs.values())

    if mode == "full":
        toolbox_ref = published["image_ref_toolbox"]
        _ensure_image(toolbox_ref, log)
        announce(toolbox_ref, "size_toolbox")
        # The toolbox image carries a version label but no revision label.
        observed = _label_version(toolbox_ref)
        if observed != toolbox_version:
            raise ReleaseError(
                "usage", f"image identity mismatch for {toolbox_ref}: version={observed!r}, "
                f"expected {toolbox_version!r}")
        run(["docker", "tag", toolbox_ref, toolbox_tag(toolbox_version)], log=log, state="build-failed")
        log.line(f"stage: tagged the Toolbox {toolbox_ref} as {toolbox_tag(toolbox_version)}")
        build_toolbox(REPO_ROOT, target, toolbox_version, log)
        pulled.append(toolbox_ref)
    else:
        log.line("stage: core mode; the Workspace runtime and Toolbox are not pulled")

    names = [f.name for f in checkout_compose_files(REPO_ROOT, profile, mode)]
    with tempfile.TemporaryDirectory(prefix="cognita-published-") as tmp:
        source = Path(tmp)
        for name in names:
            (source / name).write_bytes(read_published_compose_file(REPO_ROOT, commit, name, version, log))
        # PostgreSQL is not published by us; compose.yaml pins it by digest on
        # Docker Hub.  The unit starts with --pull never, so it must be present
        # before the first start (a fresh VM failed with "No such image" on
        # 2026-09-28).  It stays out of published_refs: uninstall must not
        # remove a public image another program on the machine may share.
        postgres_ref = postgres_service_reference(source)
        _ensure_image(postgres_ref, log)
        announce(postgres_ref, None)
        log.line(f"stage: PostgreSQL image present: {postgres_ref}")
        directory = stage_release(source, target, version, commit, ids, toolbox_version, False, log,
                                  mode=mode, published_refs=pulled)
    log.line(f"stage: published release {version} staged at {directory}")
    return directory


# --------------------------------------------------------------------------
# apply
# --------------------------------------------------------------------------


def systemctl(args: list[str], *, log: Log, state: str, check: bool = True) -> tuple[int, str]:
    return run(["systemctl", "--user", *args], log=log, state=state, check=check)


def warn_on_live_traffic(target: Target, log: Log) -> None:
    """A restart can cancel an in-flight update_document (about 6s of embedding
    against a 3s graceful stop). Warn, do not refuse: the operator chooses when
    to restart, and the only honest external signal is a recent target log."""
    config_root = read_env_file(target.env_file).get("COGNITA_CONFIG_ROOT", "")
    if not config_root:
        log.line("preflight: no COGNITA_CONFIG_ROOT in the env file; skipping the live-traffic check")
        return
    log_dir = Path(config_root) / "logs"
    if not log_dir.is_dir():
        log.line(f"preflight: no log directory at {log_dir}; skipping the live-traffic check")
        return
    now = dt.datetime.now().timestamp()
    recent = [p for p in log_dir.glob("*.log") if now - p.stat().st_mtime < 5]
    if recent:
        names = ", ".join(sorted(p.name for p in recent))
        log.line(f"preflight: WARNING {target.name} wrote {names} within the last 5 seconds; "
                 "a write may be in flight and stopping the unit will cancel it")
    else:
        log.line("preflight: no log write in the last 5 seconds on this target")


def point_current(target: Target, directory: Path, log: Log) -> None:
    """Repoint ``current`` atomically.  The symlink is the selection."""
    link = current_link(target)
    temporary = link.with_name("current.switching")
    if temporary.exists() or temporary.is_symlink():
        temporary.unlink()
    os.symlink(directory, temporary, target_is_directory=True)
    os.replace(temporary, link)
    log.line(f"apply: current -> {directory}")


def apply_release(
    repo: Path, target: Target, directory: Path, toolbox_version: str, log: Log
) -> None:
    """Stop, repoint, load the Toolbox, start.

    The Toolbox load comes BEFORE the unit start, and that order is not
    arbitrary: the broker's readiness probe creates a sandbox from the Toolbox
    image, so a runtime whose Microsandbox cache does not have that image
    reports `runtime: unavailable` and its healthcheck fails -- which means
    `up --wait` can never return on a workspace root that was just reset.
    Measured on kei, 2026-09-22, on the target the lead had reset:
    `{"status":"degraded","runtime":"unavailable","readiness":{"stage":"create",
    "reason":"probe_failed"}}`.  The load runs in a one-off `compose run --rm
    --no-deps` container and needs no running stack, so doing it first costs
    nothing and removes the dependency inversion.  (Superseded: DESIGN-13.0
    §6.2 step 5 loads it after the start, which only worked while the
    healthcheck accepted a broker that could not create a VM.)
    """
    warn_on_live_traffic(target, log)
    refuse_dropins(target)
    # A first install has no unit file yet, and `systemctl --user stop` of a
    # unit that was never loaded exits 5 ("not loaded").  Seen 2026-09-28 on the
    # installer's first fresh-VM run; kei's targets always had a unit.  There is
    # nothing to stop then, so the stop is skipped and said so.
    if (SYSTEMD_USER_DIR / target.unit).is_file():
        systemctl(["stop", target.unit], log=log, state="apply-failed")
    else:
        log.line(f"apply: {target.unit} is not installed yet; nothing to stop")
    point_current(target, directory, log)
    # The unit names the release's Compose files, so the file list must follow
    # the release being started.  2026-09-28, kei main 13.4.0 -> 13.7.0: 13.5.0
    # moved the Workspace runtime into compose.workspace.yaml, the unit
    # installed at 13.0 did not name it, and the new stack came up with the
    # app in core mode and the runtime exiting 78 without its /dev/kvm group.
    # The unit is stopped here, which is exactly what install_unit requires;
    # it writes nothing when the unit is already current.
    install_unit(target, log)
    # The mode is the RELEASE's, not the target's or the checkout's (design 5.2):
    # a rollback to a release staged in the other mode runs in that release's.
    mode = release_mode(directory)
    log.line(f"apply: {directory.name} was staged in {mode} mode")
    compose = compose_command(
        project=target.project, env_file=target.env_file,
        files=staged_compose_files(current_link(target), target.profile, mode),
    )
    if mode == "full":
        cache_root = toolbox_cache_root(target.env_file)
        archive = toolbox_dir(target) / f"toolbox-{toolbox_version}.tar"
        if not archive.is_file():
            raise ReleaseError("apply-failed", f"the staged Toolbox archive is missing: {archive}")
        load_toolbox(compose=compose, repo=repo, cache_root=cache_root, archive=archive,
                     toolbox_version=toolbox_version, log=log)
    else:
        log.line("apply: core release; no Workspace runtime, so no Toolbox to load")
    code, _tail = systemctl(["start", target.unit], log=log, state="apply-failed", check=False)
    if code != 0:
        handle_start_failure(target, compose, code, version=directory.name, log=log)


# The one line the app logs when it refuses a Workspace metadata file written
# by another build (cognita.workspace._verify_layout).  It ends with the exact
# reset command, which deploy reports without running it.
_WORKSPACE_LAYOUT_REFUSAL = "Discard and regenerate it with:"
_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def container_refusal(compose: list[str], log: Log) -> str:
    """The app container's own refusal line, or "" when it printed none.

    systemd's text names `systemctl status` and `journalctl`, and neither
    shows WHY the app container is unhealthy -- that is in the container's
    log.  Compose prefixes each line with the container name, and the app's
    own colour codes survive `--no-color`; both are stripped.
    """
    _code, tail = run(compose + ["logs", "--no-color", "--tail", "40", "cognita"],
                      log=log, state="apply-failed", check=False, quiet=True)
    hints = [_ANSI.sub("", line).split("| ", 1)[-1].strip()
             for line in tail.splitlines() if _WORKSPACE_LAYOUT_REFUSAL in line]
    return hints[-1] if hints else ""


def _reset_all_command(target: Target) -> str:
    """The command that clears the index and Workspace state (design 19.6).

    A `local` install is driven by its launcher, named by COGNITA_COMMAND, with no scripts path for the
    user to type; every other target keeps the reset script command it always printed."""
    if target.name == LOCAL_TARGET:
        return f"{command_name(read_env_file(target.env_file))} reset all"
    return f"python3 scripts/reset_disposable_state.py --target {target.name} --scope all --apply"


def _retry_command(target: Target, version: str) -> str:
    """How to start the release again after fixing a refusal (design 19.6): the launcher for a
    `local` install, which has no scripts path to type; `select` for kei's targets, as before."""
    if target.name == LOCAL_TARGET:
        return f"{command_name(read_env_file(target.env_file))} restart"
    return f"python3 scripts/release.py select --target {target.name} --version {version}"


def handle_start_failure(target: Target, compose: list[str], code: int, *,
                         version: str, log: Log) -> None:
    """Report a failed start and leave any Workspace reset to the operator.

    The 13.1.0 deployment on 2026-09-22 first encountered a Workspace layout
    refusal and later automated its reset. Current manual-only policy means
    neither deploy nor select may invoke that destructive recovery. Preserve
    the container's reason and give the exact reset and retry commands.
    """
    refusal = container_refusal(compose, log)
    message = f"systemctl --user start {target.unit} failed ({code})"
    if refusal:
        message += (
            "\nthe cognita container refuses to start:\n"
            f"    {refusal}\n"
            "No reset was run."
        )
        if f"--target {target.name} --scope workspaces --apply" in refusal:
            message += (
                "\nIf you choose to discard this target's Workspace scratch state, run:\n"
                f"    python3 scripts/reset_disposable_state.py --target {target.name} "
                "--scope workspaces --apply"
            )
        else:
            message += "\nReview the refusal's target and scope before any manual reset."
        message += (
            "\nAfter resolving the refusal, retry:\n"
            f"    {_retry_command(target, version)}"
        )
    raise ReleaseError("apply-failed", message)


# --------------------------------------------------------------------------
# verify
# --------------------------------------------------------------------------


def healthz(port: int, *, attempts: int = 30, log: Log) -> dict:
    """GET /healthz on loopback.  The unit already waited for the healthcheck;
    these retries only cover the seconds between container-healthy and the
    route answering."""
    url = f"http://127.0.0.1:{port}/healthz"
    last = ""
    for attempt in range(1, attempts + 1):
        try:
            with urllib.request.urlopen(url, timeout=10) as response:
                payload = json.loads(response.read().decode("utf-8"))
            log.line(f"verify: {url} answered on attempt {attempt}")
            return payload
        except (urllib.error.URLError, OSError, ValueError) as exc:
            last = str(exc)
            if attempt == attempts:
                break
            _sleep(2)
    raise ReleaseError("verify-failed", f"{url} did not answer: {last}")


def _sleep(seconds: float) -> None:
    # One import-free place to wait, so a reader can see every wait this script
    # performs: healthz polling and nothing else.
    import time

    time.sleep(seconds)


def check_healthz(target: Target, version: str, log: Log, *, expect_test_mode: bool | None = None) -> dict:
    payload = healthz(target.mcp_port, log=log)
    reported = str(payload.get("version", ""))
    log.line(f"verify: /healthz version={reported} status={payload.get('status')}")
    if reported != version:
        raise ReleaseError("verify-failed", f"/healthz reports {reported!r}, expected {version!r}")
    if target.profile in GPU_PROFILES:
        gpu = str(payload.get("embed", {}).get("gpu", ""))
        log.line(f"verify: embed.gpu={gpu}")
        # The GPU is an accelerator, never a dependency (CLAUDE.md, 6.0), and a
        # deploy is no exception. Until 13.2.3 this FAILED the deploy when no
        # card qualified -- which happens whenever another process has the
        # cards (beta rebuilding its index pegged both at 90-100% on
        # 2026-09-22) or the card scan has not landed yet -- while the release
        # was applied and serving the whole time; only the "verified" stamp and
        # the live self-test were lost. Doug: "Why is the deploy blocked on the
        # video cards??" It is not any more: the state is logged, a warning
        # says what the server will do (embed on CPU until a card frees up),
        # and verification carries on.
        if gpu == "disabled":
            log.line("verify: WARNING the GPU is disabled in this target's acceleration config")
        elif gpu != "ready":
            skipped = payload.get("embed", {}).get("devices_skipped") or []
            log.line(f"verify: WARNING no GPU qualifies right now ({'; '.join(map(str, skipped)) or 'no detail'}); "
                     "the server embeds on the CPU until a card frees up. Not a deploy failure.")
    # The store reports itself unavailable rather than exiting when the
    # database schema is not the one this image understands (section 4.1), so a
    # started service is not by itself a working index.  The reason it gives is
    # the actionable part -- it carries the exact reset command.
    index = payload.get("index")
    if isinstance(index, dict):
        state = str(index.get("status", ""))
        log.line(f"verify: index.status={state}")
        if state != "ok":
            # This is the expected end of the FIRST 13.0 deploy on a target
            # whose database predates the schema row, so the message carries
            # the two commands that finish the job rather than leaving someone
            # to find them in DEPLOYMENT.md.  The release itself is staged,
            # started and running; only its verification failed.
            raise ReleaseError(
                "verify-failed",
                f"the index is {state}: {index.get('reason', 'no reason reported')}\n"
                f"{version} is running; the index is what is unavailable. If this is the first "
                f"13.0 deploy on {target.name}, its database is populated and unversioned "
                "(DESIGN-13.0 §4.1) and this is expected exactly once. Clear it and verify:\n"
                f"    {_reset_all_command(target)}\n"
                f"    {_retry_command(target, version)}",
            )
    else:
        log.line("verify: WARNING /healthz does not report index status; skipping that check")
    if expect_test_mode is not None:
        if "test_mode" not in payload:
            log.line("verify: WARNING /healthz does not report test_mode; skipping that check")
        elif bool(payload["test_mode"]) != expect_test_mode:
            raise ReleaseError(
                "verify-failed" if expect_test_mode else "test-mode-stuck",
                f"/healthz reports test_mode={payload['test_mode']}, expected {expect_test_mode}",
            )
    return payload


def self_test_key(repo: Path) -> str | None:
    """The public built-in test key.  auth_policy.py owns it (section 7.3)."""
    return _module_literal(repo / "src" / "cognita" / "auth_policy.py", "SELF_TEST_API_KEY")


def run_live_selftest(
    *, repo: Path, target: Target, compose_files: list[Path], key: str, log: Log, state: str,
    log_dir: Path, mode: str = "full", stop_check: Callable[[], bool] | None = None,
) -> dict:
    """Call the live self-test client (scripts/kei_http_selftest.py live).

    It runs the product assertions, including the Workspace smoke, inside the
    already-running app container against the container's own MCP port -- the
    host interpreter has neither httpx nor cognita.  The port is therefore the
    CONTAINER's 8675, not whatever the target publishes.  The key goes in on
    stdin, never in argv.
    """
    command = [sys.executable, str(repo / "scripts" / "kei_http_selftest.py"), "live",
               "--compose-project", target.project, "--env-file", str(target.env_file)]
    for path in compose_files:
        command += ["--compose-file", str(path)]
    command += ["--mcp-port", str(CONTAINER_MCP_PORT), "--connector", target.connector,
                "--mode", mode, "--log-dir", str(log_dir)]
    log.line(f"verify: live self-test against project {target.project}, connector {target.connector}")
    # stop_check (design 21.2) is passed only when there is one, so the call is otherwise what it was.
    _code, tail = run(command, log=log, state=state, cwd=repo, stdin_text=key + "\n",
                      **({"stop_check": stop_check} if stop_check is not None else {}))
    receipt = None
    for line in tail.splitlines():
        if line.startswith("selftest_receipt="):
            try:
                receipt = json.loads(line.partition("=")[2])
            except ValueError:
                receipt = None
    if (not isinstance(receipt, dict) or receipt.get("schema") != 1 or receipt.get("mode") != mode
            or any(receipt.get(key) != "passed" for key in ("result", "mandatory_ocr", "missing_file_parity"))
            or not isinstance(receipt.get("canonical_log"), str) or not receipt["canonical_log"]):
        raise ReleaseError(state, "live self-test lacks a passing mandatory HTTP/OCR receipt")
    return receipt


def expect_unauthorized(target: Target, key: str, log: Log) -> None:
    """Outside test mode the built-in key must be a 401 on the real connector.

    This is the half of section 7.3 that a passing self-test cannot show: the
    key working in test mode proves nothing about it being refused afterwards.
    """
    url = f"http://127.0.0.1:{target.mcp_port}/mcp/connectors/{target.connector}/mcp"
    request = urllib.request.Request(
        url, method="POST",
        data=b'{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}',
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json",
                 "Accept": "application/json, text/event-stream"},
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            status = response.status
    except urllib.error.HTTPError as exc:
        status = exc.code
    except (urllib.error.URLError, OSError) as exc:
        raise ReleaseError("verify-failed",
                           f"could not probe the connector route {url}: {exc}") from exc
    log.line(f"verify: built-in key on connector {target.connector} -> HTTP {status}")
    if status != 401:
        raise ReleaseError(
            "verify-failed",
            f"the built-in test key returned {status} on connector {target.connector}, expected 401",
        )


def test_mode_override_text() -> str:
    """COGNITA_TEST_MODE=1 and restart: "no".

    Without ``restart: "no"`` a crash would restart the container straight into
    a fresh 30-minute test-mode window (section 7.3).
    """
    return (
        "# Generated by scripts/release.py -- live verification only, removed in finally.\n"
        "services:\n"
        "  cognita:\n"
        "    restart: \"no\"\n"
        "    environment:\n"
        "      COGNITA_TEST_MODE: \"1\"\n"
    )


def verify_release(repo: Path, target: Target, version: str, log: Log) -> None:
    """Deploy is deploy: the release is verified when the right version answers.

    Doug, 2026-09-22: "Deploy is deploy. QA is QA." Until 13.2.3 this went on
    to recreate the app in test mode and run the live self-test, so a busy
    GPU or one failing self-test step reported the DEPLOY as failed while the
    release was applied and serving the whole time. The live self-test is
    QA: `release.py qa` runs it on demand, and `deploy --test` runs it after
    the suite because the operator asked for QA. Superseded before that: a
    `--skip-selftest` flag, retired in 13.0.
    """
    payload = check_healthz(target, version, log)
    log.line(f"verify: healthz ok for {version} ({payload.get('service')})")


def qa_release(repo: Path, target: Target, version: str, tmp: Path, log: Log,
               stop_check: Callable[[], bool] | None = None) -> None:
    """QA: the live self-test against the real connector, in test mode, then back.

    ``stop_check`` (design 21.2) stops the live self-test on request: ``Stopped`` is raised, and the
    ``finally`` below still puts the target back in normal mode, exactly as for a failure."""
    check_healthz(target, version, log)
    key = self_test_key(repo)
    if key is None:
        log.line("verify: WARNING auth_policy.py has no SELF_TEST_API_KEY; the live self-test cannot run")
        return

    # QA runs in the mode the selected release was staged in (design 5.2).
    mode = release_mode(current_link(target))
    log.line(f"verify: QA on {target.name} in {mode} mode (read from the selected release)")
    files = staged_compose_files(current_link(target), target.profile, mode)
    override = tmp / "test-mode.override.yaml"
    override.write_text(test_mode_override_text(), encoding="utf-8")
    recreate = compose_command(project=target.project, env_file=target.env_file, files=files)
    attempted_test_mode = False
    try:
        log.line("verify: recreating the app container in test mode")
        attempted_test_mode = True
        run(recreate + ["-f", str(override), "up", "-d", "--no-deps", "--no-build", "--pull", "never",
                        "--wait", "cognita"], log=log, state="verify-failed", cwd=repo)
        check_healthz(target, version, log, expect_test_mode=True)
        run_live_selftest(repo=repo, target=target, compose_files=files, key=key, log=log,
                          state="verify-failed", log_dir=logs_dir(target), mode=mode, stop_check=stop_check)
    finally:
        override.unlink(missing_ok=True)
        if attempted_test_mode:
            # Always leave the target in normal mode, on success and on failure.
            log.line("verify: recreating the app container in normal mode")
            try:
                code, _tail = run(recreate + ["up", "-d", "--no-deps", "--no-build", "--pull", "never",
                                              "--wait", "cognita"],
                                  log=log, state="test-mode-stuck", cwd=repo, check=False)
            except Exception as exc:
                raise ReleaseError(
                    "test-mode-stuck",
                    f"the app container could not be recreated in normal mode: {exc}; "
                    f"run: systemctl --user restart {target.unit}",
                ) from exc
            if code:
                raise ReleaseError(
                    "test-mode-stuck",
                    "the app container could not be recreated in normal mode; "
                    f"run: systemctl --user restart {target.unit} "
                    "(test mode also expires 30 minutes after startup)",
                )
            try:
                check_healthz(target, version, log, expect_test_mode=False)
                expect_unauthorized(target, key, log)
            except ReleaseError as exc:
                raise ReleaseError(
                    "test-mode-stuck",
                    f"normal-mode restoration could not be verified: {exc}. "
                    f"Run `python3 scripts/release.py qa --target {target.name}` after restoring the service.",
                ) from exc
    log.line("verify: live self-test passed and the target is back in normal mode")


# --------------------------------------------------------------------------
# The throwaway test stack (section 7.1)
# --------------------------------------------------------------------------

# One generator, in scripts/kei_http_selftest.py.  The isolated HTTP self-test
# and this script both need a complete synthetic installation, and two copies
# of that YAML would drift the day one of them gained a setting.  The module is
# loaded by path because release.py runs on the host interpreter, which has no
# Cognita package to import it from -- and because importing it must not pull
# in httpx, which that file imports lazily for exactly this reason.
TEST_SLUG = TARGETS["test"].connector


def _selftest_module():
    spec = importlib.util.spec_from_file_location(
        "cognita_kei_http_selftest", REPO_ROOT / "scripts" / "kei_http_selftest.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def write_installation(root: Path, *, mcp_port: int, admin_port: int, version: str,
                       release_target: str, gpu: bool, models_root: Path, log: Log,
                       mode: str = "full"):
    """Generate everything compose.yaml needs under one root (section 7.1).

    The work is kei_http_selftest.write_config's: secrets including a
    self-signed Admin TLS pair, a minimal config set with a second populated
    project, the capacity and transfer markers, and an env file.  Returns that
    function's Installation (api_key, env_path, postgres_password).  `gpu`
    enables GPU embedding in the generated acceleration config so an amd
    throwaway stack embeds on the real cards (section 7.1).
    """
    installation = _selftest_module().write_config(
        root, mcp_port, admin_port, version, release_target=release_target, gpu=gpu,
        models_root=models_root, mode=mode)
    log.line(f"test: installation generated under {root} (MCP {mcp_port}, Admin {admin_port}, "
             f"release target {release_target}, models {models_root})")
    return installation


def test_override_text(*, version: str, run_id: str, dsn: str, models_root: str,
                       mode: str = "full", test_runner_ref: str | None = None) -> str:
    """The throwaway stack's override: a disposable database and a test runner.

    Ports, roots and device group IDs all come from the generated env file, so
    the only things that must be said here are the ones compose.yaml cannot
    take from the environment.
    """
    runner = test_runner_ref or f"{TEST_IMAGE_PREFIX}:{version}-{run_id}"
    return f"""# Generated by scripts/release.py for test run {run_id}.
services:
  postgres:
    volumes:
      - type: volume
        source: pgdata
        target: /var/lib/postgresql
  cognita:
    # The throwaway stack exists only to be tested, so it runs in test mode
    # from the start: the live self-test authenticates with the built-in key
    # (section 7.3). restart is off so a crash cannot open a fresh window.
    environment:
      COGNITA_TEST_MODE: "1"
    restart: "no"
  app-test:
    image: {runner}
    profiles: ["test"]
    user: "${{COGNITA_SERVICE_UID:-1000}}:${{COGNITA_SERVICE_GID:-1000}}"
    working_dir: /opt/cognita-tests
    environment:
      COGNITA_TEST_PG_DSN: {dsn}
      HOME: /var/lib/cognita/models
      HF_HOME: /var/lib/cognita/models/.cache/huggingface
    volumes:
      - type: bind
        source: {models_root}
        target: /var/lib/cognita/models
    networks:
      - cognita-internal
volumes:
  pgdata:
    name: cognita-test-{run_id}-pg
"""


def parse_pytest_summary(output: str) -> str:
    for line in reversed(output.splitlines()):
        if re.search(r"\d+ (passed|failed|error|skipped)", line):
            return line.strip()
    return "no pytest summary line found"


def run_test_stack(repo: Path, target: Target, version: str, commit: str,
                   image_refs: dict[str, str], log: Log, *, run_id: str | None = None,
                   profile: str | None = None, mode: str = "full",
                   test_runner_ref: str | None = None, no_build: bool = False,
                   candidate_images_dir: Path | None = None) -> dict:
    """Start an isolated mode-specific stack and run the accepted test runner.

    Every disposable thing this creates carries the run ID. Cleanup tears down
    only that Compose project, verifies its resources are gone, then removes
    the run-owned temporary root and (when built here) test image tag.
    """
    run_id = run_id or uuid.uuid4().hex[:12]
    profile = profile or target.profile
    project = f"cognita-test-{run_id}"
    # The system temp directory, with no flag to move it: one run root, named
    # for the run, removed in `finally`.
    root = Path(tempfile.gettempdir()) / f"cognita-test-{run_id}"
    test_image = test_runner_ref or f"{TEST_IMAGE_PREFIX}:{version}-{run_id}"
    toolbox_version = read_toolbox_version(repo, log) if mode == "full" else ""
    log.line(f"test: run {run_id}, project {project}, root {root}")

    compose: list[str] = []
    root_owned = False
    test_image_owned = False
    http_receipt = None
    try:
        # write_config creates the roots under here and refuses an existing
        # directory, so this makes only the run root itself, owner-only.
        root.mkdir(parents=True, exist_ok=False)
        root_owned = True
        if os.name == "posix":
            root.chmod(0o700)
        if not no_build:
            if image_exists(test_image):
                raise ReleaseError("test-failed", f"refusing to replace preexisting test image tag {test_image}")
            log.line(f"test: building {test_image} (target stage: test)")
            try:
                run(["docker", "build", "-f", str(repo / "containers" / "cognita" / "Dockerfile"),
                     "--target", "test", "--build-arg", f"COGNITA_VERSION={version}",
                     "--build-arg", f"COGNITA_COMMIT={commit}", "-t", test_image, str(repo)],
                    log=log, state="test-failed", cwd=repo)
            except Exception:
                test_image_owned = image_exists(test_image)
                raise
            test_image_owned = True

        mcp_port, admin_port = free_port(), free_port()
        # 13.0 §7.1 step 3: the TARGET's model cache, read-write.  The models
        # and the compiled MIGraphX programs are a cache, not state: sharing
        # them is what keeps a test run from downloading gigabytes and
        # recompiling every shape, and the app writes into them exactly as the
        # target's own service does.
        models_root = target_models_root(target.env_file)
        # 14.2.0: the images carry no OCR weights, so the stack's OCR (and the suite's OCR tests) find
        # them only in this cache. Confirm they are there, verified, before the stack starts; a cache
        # that already has them costs one hash pass and no network.
        fetch_ocr_weights(models_root, log)
        generated = write_installation(root, mcp_port=mcp_port, admin_port=admin_port,
                                       version=version, release_target=target.name,
                                       gpu=profile in GPU_PROFILES, models_root=models_root, mode=mode,
                                       log=log)
        env_file = Path(generated.env_path)
        dsn = f"postgresql://cognita:{generated.postgres_password}@postgres:5432/cognita"
        override = root / "compose.test.yaml"
        override.write_text(
            test_override_text(version=version, run_id=run_id, dsn=dsn,
                               models_root=str(models_root), mode=mode,
                               test_runner_ref=test_image),
            encoding="utf-8",
        )
        # The throwaway stack consumes this invocation's build references.
        # They stay tagged until after this stack is torn down.
        if candidate_images_dir is not None:
            images = candidate_images_dir / f"compose.{profile}.{mode}.images.yaml"
            if not images.is_file():
                raise ReleaseError("test-failed", f"candidate image fragment is missing: {images}")
        else:
            images = root / "compose.images.yaml"
            images.write_text(compose_images_text(version, commit, image_refs), encoding="utf-8")
        files = [*checkout_compose_files(repo, profile, mode), images, override]
        compose = compose_command(project=project, env_file=env_file, files=files)

        if mode == "full":
            archive = toolbox_dir(target) / f"toolbox-{toolbox_version}.tar"
            if not archive.is_file():
                raise ReleaseError("test-failed", f"the Toolbox archive is missing; run build first: {archive}")
            cache_root = toolbox_cache_root(env_file)
            cache_root.mkdir(parents=True, exist_ok=True)
            if os.name == "posix":
                cache_root.chmod(0o700)
            log.line(f"test: toolbox cache root ready: {cache_root}")
            load_toolbox(compose=compose, repo=repo, cache_root=cache_root,
                         archive=archive, toolbox_version=toolbox_version, log=log)
        log.line("test: starting the throwaway stack")
        run(compose + ["up", "-d", "--no-build", "--pull", "never", "--wait"],
            log=log, state="test-failed", cwd=repo)

        # asyncio_mode comes from pyproject.toml in a checkout; the image has
        # only the tests, so it is passed here instead.  -rs prints every skip
        # reason, which the report has to carry.
        pytest_command = (
            "python -c \"import cognita; print('cognita package:', cognita.__file__)\" && "
            "python -m pytest tests -o asyncio_mode=auto -rfEs -q -p no:cacheprovider"
        )
        code, output = run(
            compose + ["run", "--pull", "never", "--rm", "--no-deps",
                       "--entrypoint", "sh", "app-test", "-c", pytest_command],
            log=log, state="test-failed", cwd=repo, check=False,
        )
        log.line(f"test: pytest summary: {parse_pytest_summary(output)}")
        if code:
            raise ReleaseError("test-failed", f"pytest failed ({code}) inside {test_image}:\n{output}")

        key = self_test_key(repo)
        if key is None:
            raise ReleaseError("test-failed", "mandatory HTTP/OCR self-test cannot run: SELF_TEST_API_KEY is unavailable")
        else:
            stack = Target(name=target.name, profile=profile, env_file=env_file,
                           unit=target.unit, project=project, mcp_port=mcp_port,
                           admin_port=admin_port, connector=TEST_SLUG)
            http_receipt = run_live_selftest(repo=repo, target=stack, compose_files=files, key=key, log=log,
                                            state="test-failed", log_dir=logs_dir(target), mode=mode)
    finally:
        log.line(f"test: cleaning up run {run_id}")
        cleanup_errors: list[str] = []
        if compose:
            code, tail = run(compose + ["down", "-v", "--remove-orphans"], log=log,
                             state="test-failed", cwd=repo, check=False)
            if code:
                cleanup_errors.append(f"compose down exited {code}: {tail}")
        resources_clear = _report_residue(project, run_id, log) if compose else True
        if not resources_clear:
            cleanup_errors.append(f"Compose resources remain or could not be checked for {project}")
        if root_owned and root.exists() and resources_clear:
            try:
                shutil.rmtree(root)
            except OSError as exc:
                cleanup_errors.append(f"temporary root {root} could not be removed: {exc}")
        if root_owned and root.exists() and resources_clear:
            cleanup_errors.append(f"temporary root remains after cleanup: {root}")
        elif root_owned and root.exists():
            cleanup_errors.append(f"temporary root retained while Compose resources remain: {root}")
        if test_image_owned:
            code, tail = run(["docker", "image", "rm", test_image], log=log,
                             state="test-failed", cwd=repo, check=False)
            if code:
                cleanup_errors.append(f"test image {test_image} could not be removed: {tail}")
            elif image_exists(test_image):
                cleanup_errors.append(f"test image tag remains after removal: {test_image}")
        if cleanup_errors:
            raise ReleaseError("test-failed", "cleanup incomplete: " + "; ".join(cleanup_errors))
    if http_receipt is None:
        raise ReleaseError("test-failed", "mandatory HTTP/OCR evidence is missing")
    return {**http_receipt,
            "canonical_log": str(log.path.resolve()) if log.path is not None else http_receipt["canonical_log"],
            "http_log": http_receipt["canonical_log"], "cleanup": "verified", "run_id": run_id}


_BUNDLE_SCRIPT_SUFFIXES = {".md", ".ps1", ".sh", ".vbs", ".service", ".conf", ".py"}
_BUNDLE_FORBIDDEN_SUFFIXES = {".exe", ".dll", ".bat", ".cmd", ".com", ".msi"}


def _bundle_windows(repo: Path, images_dir: Path, cpu_archive: Path,
                    output: Path, log: Log, *, mode: str) -> None:
    if mode not in {"core", "full"}:
        raise ReleaseError("usage", "Windows bundle mode must be core or full")
    values = candidate_manifest(images_dir)
    if mode == "full":
        cpu_full_qualification(values)
    version, commit = values["version"], values["commit"]
    require_clean_checkout(repo, log)
    source_tree = subprocess.run(["git", "-C", str(repo), "rev-parse", f"{commit}^{{tree}}"],
                                 capture_output=True, text=True, check=False, timeout=60)
    checkout_tree = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD^{tree}"],
                                   capture_output=True, text=True, check=False, timeout=60)
    if (source_tree.returncode or checkout_tree.returncode
            or source_tree.stdout.strip() != checkout_tree.stdout.strip()):
        raise ReleaseError("usage", "bundle source tree differs from the candidate image commit")
    cpu_ref = values["image_ref_cognita_cpu"]
    verify_candidate_image(cpu_ref, version, commit, values["image_cognita_cpu"])
    cpu_hash = sha256_file(cpu_archive)
    if cpu_hash != values.get("cpu_archive_sha256"):
        raise ReleaseError("usage", "CPU archive SHA-256 does not match the candidate build")
    if output.exists():
        raise ReleaseError("usage", f"bundle output already exists: {output}")

    windows_root = repo / "scripts" / "windows"
    installer = windows_root / "Install-CognitaWindows.ps1"
    if not installer.is_file():
        raise ReleaseError("usage", f"Windows installer is missing: {installer}")
    runbook_candidates = [path for path in (repo / "docs").glob("*WINDOWS*.md")
                          if "DESIGN" not in path.name.upper() and "PROBE" not in path.name.upper()]
    runbook_candidates.extend(path for path in windows_root.rglob("*.md") if path.is_file())
    if not runbook_candidates:
        raise ReleaseError("usage", "Windows operator guide is missing from scripts/windows or docs")

    staging = output.with_name(output.name + f".{uuid.uuid4().hex}.staging")
    output.parent.mkdir(parents=True, exist_ok=True)
    staging.mkdir()
    try:
        static_files = ["compose.yaml", "compose.cpu.yaml",
                        "scripts/set-admin-credentials.py",
                        "scripts/windows/Update-Release.py",
                        "scripts/run-selftest.py", "scripts/provision_selftest.py"]
        if mode == "full":
            if not values.get("image_ref_workspace_runtime"):
                raise ReleaseError("usage", "full Windows bundle requested but the Workspace runtime image is absent")
            static_files.append("compose.workspace.yaml")
        for relative in static_files:
            source = repo / relative
            if not source.is_file():
                raise ReleaseError("usage", f"bundle input is missing: {source}")
            destination = staging / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, destination)
        shutil.copyfile(cpu_archive, staging / "cognita-cpu.tar")
        shutil.copytree(windows_root, staging / "scripts" / "windows", dirs_exist_ok=True)
        (staging / "docs").mkdir()
        for guide in sorted(path for path in runbook_candidates if path.is_relative_to(repo / "docs")):
            shutil.copyfile(guide, staging / "docs" / guide.name)

        fragments = []
        bundle_mode = mode
        selected_profiles = [("cpu", bundle_mode)]
        for profile, profile_mode in selected_profiles:
            fragment_name = f"compose.{profile}.{profile_mode}.images.yaml"
            fragment = images_dir / fragment_name
            if not fragment.is_file():
                raise ReleaseError("usage", f"candidate Compose image fragment is missing: {fragment}")
            refs, _test_ref, _manifest = validate_candidate(
                images_dir, profile=profile, mode=profile_mode, version=version, commit=commit, repo=repo)
            if fragment.read_text(encoding="utf-8").count(refs["cognita"]) != 1:
                raise ReleaseError("usage", f"candidate image fragment does not match accepted app image: {fragment}")
            target_fragment = staging / fragment_name
            shutil.copyfile(fragment, target_fragment)
            fragments.append(target_fragment)

        if bundle_mode == "full":
            workspace_name = values.get("workspace_archive", "")
            workspace_source = images_dir / workspace_name
            toolbox_source = Path(values.get("toolbox_archive", ""))
            if (not workspace_source.is_file() or not toolbox_source.is_file()
                    or sha256_file(workspace_source) != values.get("workspace_archive_sha256")
                    or sha256_file(toolbox_source) != values.get("toolbox_sha256")):
                raise ReleaseError("usage", "candidate Workspace runtime or Toolbox archive is missing or has changed")
            shutil.copyfile(workspace_source, staging / "workspace-runtime.tar")
            shutil.copyfile(toolbox_source, staging / "toolbox.tar")

        allowed_metadata = ("version", "commit", "image_ref_cognita_cpu", "image_cognita_cpu",
                            "image_ref_postgres", "image_postgres",
                            "image_ref_cognita_amd", "image_cognita_amd",
                            "image_ref_cognita_nvidia", "image_cognita_nvidia",
                            "image_ref_workspace_runtime", "image_workspace_runtime",
                            "fragment_cpu_core", "fragment_cpu_full", "fragment_amd_core", "fragment_amd_full",
                            "fragment_nvidia_core", "fragment_nvidia_full",
                            "test_runner_ref", "test_runner_id",
                            "toolbox_version", "toolbox_sha256", "qualification_cpu_full")
        release_lines = [(key, values[key]) for key in allowed_metadata if values.get(key)]
        release_lines.append(("bundle_mode", bundle_mode))
        (staging / "release.txt").write_text("".join(f"{key}: {value}\n" for key, value in release_lines),
                                               encoding="utf-8")

        for path in staging.rglob("*"):
            if path.is_symlink() or (path.is_file() and path.suffix.lower() in _BUNDLE_FORBIDDEN_SUFFIXES):
                raise ReleaseError("usage", f"bundle contains unsupported executable content: {path.relative_to(staging)}")
            if path.is_file() and path.suffix.lower() not in _BUNDLE_SCRIPT_SUFFIXES | {".yaml", ".yml", ".txt", ".tar"}:
                raise ReleaseError("usage", f"bundle contains an unapproved file type: {path.relative_to(staging)}")
        bundle_files = [path for path in staging.rglob("*") if path.is_file() and path.name != "SHA256SUMS"]
        _write_sha256sums(bundle_files, staging / "SHA256SUMS", relative_to=staging)
        verify_bundle_checksums(staging)
        staging.rename(output)
        log.line(f"bundle: assembled {bundle_mode} Windows bundle at {output} ({len(bundle_files)} files)")
    except Exception:
        if staging.exists():
            shutil.rmtree(staging)
            if staging.exists():
                log.line(f"bundle: cleanup failed; staging directory remains: {staging}")
        raise


def verify_bundle_checksums(directory: Path) -> None:
    sums_path = directory / "SHA256SUMS"
    if not sums_path.is_file():
        raise ReleaseError("usage", f"bundle checksum file is missing: {sums_path}")
    listed: dict[str, str] = {}
    for line in sums_path.read_text(encoding="utf-8").splitlines():
        digest, separator, relative = line.partition("  ")
        candidate = Path(relative)
        if not separator or candidate.is_absolute() or ".." in candidate.parts or relative in listed:
            raise ReleaseError("usage", f"invalid SHA256SUMS entry: {line!r}")
        target = directory / candidate
        if not target.is_file() or sha256_file(target) != digest:
            raise ReleaseError("usage", f"bundle file is missing or has a hash mismatch: {relative}")
        listed[relative] = digest
    actual = {path.relative_to(directory).as_posix() for path in directory.rglob("*")
              if path.is_file() and path.name != "SHA256SUMS"}
    if set(listed) != actual:
        raise ReleaseError("usage", "bundle contains files outside SHA256SUMS or omits listed files")


def _report_residue(project: str, run_id: str, log: Log) -> bool:
    """Never call a run leak-free without checking (AGENTS.md)."""
    try:
        container_result = subprocess.run(
            ["docker", "ps", "-a", "--filter", f"label=com.docker.compose.project={project}", "-q"],
            capture_output=True, text=True, check=False, timeout=60,
        )
        volume_result = subprocess.run(
            ["docker", "volume", "ls", "--filter", f"name=cognita-test-{run_id}", "-q"],
            capture_output=True, text=True, check=False, timeout=60,
        )
        network_result = subprocess.run(
            ["docker", "network", "ls", "--filter", f"label=com.docker.compose.project={project}", "-q"],
            capture_output=True, text=True, check=False, timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.line(f"test: residue check failed for {project}: {exc}")
        return False
    if container_result.returncode or volume_result.returncode or network_result.returncode:
        log.line(f"test: residue check failed for {project}: docker ps exit "
                 f"{container_result.returncode}, docker volume ls exit {volume_result.returncode}, "
                 f"docker network ls exit {network_result.returncode}")
        return False
    containers = container_result.stdout.strip()
    volumes = volume_result.stdout.strip()
    networks = network_result.stdout.strip()
    if containers or volumes or networks:
        log.line(f"test: RESIDUE LEFT BEHIND containers={containers!r} volumes={volumes!r} "
                 f"networks={networks!r}")
        return False
    else:
        log.line(f"test: cleanup verified: no containers or volumes remain for {project}")
        return True


# --------------------------------------------------------------------------
# install-unit, status, prune
# --------------------------------------------------------------------------


def render_unit(target: Target, version_note: str) -> str:
    """Render the unit from the template (design 5.6).

    Two placeholders exist for `local` and render to today's exact text for
    every table target, so kei's installed units are not rewritten:
    ``${DESCRIPTION}`` and ``${EXEC_START_PRE}``.  The latter sits at the very
    START of the ExecStart line, so an empty value leaves no blank line (a
    line of its own would not be byte-identical); for `local` it carries whole
    ``ExecStartPre=`` lines, each ending in a newline.  The template cannot
    carry a comment about either, because the comment would be rendered into
    kei's units too.

    The mode of the files comes from the selected release (design 5.2).
    """
    template = string.Template(UNIT_TEMPLATE.read_text(encoding="utf-8"))
    current = current_link(target)
    files = staged_compose_files(current, target.profile)
    if target.name != LOCAL_TARGET:
        return template.substitute(
            DESCRIPTION=f"Cognita {target.name} container stack ({target.profile}, {version_note})",
            EXEC_START_PRE="",
            PROFILE=target.profile,
            PROJECT=target.project,
            ENV_FILE=str(target.env_file),
            CURRENT=str(current),
            COMPOSE_FILES=" ".join(f"-f {path}" for path in files),
        )
    # systemd splits an Exec line on whitespace, and cloud-sync folders have
    # spaces in their names, so every path is double-quoted.  `%` cannot occur
    # (design section 3), so no specifier can expand inside the quotes.
    local_values = read_env_file(target.env_file)
    roots = document_roots(local_values)
    pre = "".join(f'ExecStartPre=-/usr/bin/mkdir -p -- "{path}"\n' for _key, path in roots)
    return template.substitute(
        DESCRIPTION=f"Cognita ({target.profile}, installed by {command_name(local_values)})",
        EXEC_START_PRE=pre,
        PROFILE=target.profile,
        PROJECT=target.project,
        ENV_FILE=f'"{target.env_file}"',
        CURRENT=f'"{current}"',
        COMPOSE_FILES=" ".join(f'-f "{path}"' for path in files),
    )


def unit_is_active(target: Target, log: Log) -> bool:
    code, tail = run(["systemctl", "--user", "is-active", target.unit], log=log,
                     state="usage", check=False, quiet=True)
    state = tail.strip() or "unknown"
    log.line(f"unit: {target.unit} is {state} (exit {code})")
    return code == 0


def install_unit(target: Target, log: Log) -> None:
    # Refuse while the target is running.  systemd stops a unit with the
    # ExecStop of the file that is CURRENTLY on disk, so overwriting the unit
    # and reloading leaves the old stack with no command that can bring it
    # down: its containers keep running under a project the new ExecStop may
    # not name, and `systemctl stop` then tears down the wrong thing or
    # nothing at all.  Stopping first costs one command and removes the whole
    # class of problem.
    if unit_is_active(target, log):
        raise ReleaseError(
            "usage",
            f"{target.unit} is active. Stop it first:\n"
            f"    systemctl --user stop {target.unit}\n"
            "After a daemon-reload the running stack can only be stopped by the ExecStop of "
            "the unit file it was started with, so the unit must not be replaced underneath it.",
        )
    rendered = render_unit(target, version_note="release.py-managed")
    log.line(f"install-unit: rendered {target.unit} for {target.name} "
             f"({len(rendered.splitlines())} lines, {rendered.count('ExecStartPre=')} ExecStartPre)")
    path = SYSTEMD_USER_DIR / target.unit
    SYSTEMD_USER_DIR.mkdir(parents=True, exist_ok=True)
    retired = retire_dropins(target, log)
    existing = path.read_text(encoding="utf-8") if path.is_file() else ""
    if existing == rendered:
        log.line(f"install-unit: {path} is already current; nothing to write")
        if retired:
            systemctl(["daemon-reload"], log=log, state="apply-failed")
        return
    diff = list(difflib.unified_diff(existing.splitlines(), rendered.splitlines(),
                                     fromfile=f"{path} (installed)", tofile=f"{path} (new)", lineterm=""))
    log.line(f"install-unit: {path} differs; the change is:")
    for line in diff:
        log.raw(line)
    if existing:
        saved = target_root(target) / "units" / f"{target.unit}.{dt.datetime.now():%Y%m%d%H%M%S}"
        saved.parent.mkdir(parents=True, exist_ok=True)
        saved.write_text(existing, encoding="utf-8")
        log.line(f"install-unit: saved the replaced unit as {saved}")
    path.write_text(rendered, encoding="utf-8")
    log.line(f"install-unit: wrote {path}")
    systemctl(["daemon-reload"], log=log, state="apply-failed")


def enable_unit(target: Target, log: Log | None = None) -> None:
    """`systemctl --user enable` (design 5.6): start at login/boot.  No lock."""
    log = log or Log(None)
    log.line(f"unit: enabling {target.unit} for {target.name}")
    systemctl(["enable", target.unit], log=log, state="apply-failed")


def dropin_paths(target: Target) -> list[Path]:
    """Every systemd drop-in that applies to the target's unit."""
    directory = SYSTEMD_USER_DIR / f"{target.unit}.d"
    return sorted(directory.glob("*.conf")) if directory.is_dir() else []


def retire_dropins(target: Target, log: Log) -> list[Path]:
    """Move the unit's drop-ins aside; a drop-in can silently replace the unit.

    13.2.2, found on kei 2026-09-22: beta's first 13.x deploy stopped the old
    stack, wrote the 13.x unit, started it -- and 12.18.5 came back.  The 12.x
    release machinery had left `<unit>.d/90-cognita-release-lifecycle.conf`,
    whose `ExecStart=` / `ExecStop=` lines override the unit file's, so the
    unit systemd actually ran was the retired controller, not what this tool
    had just written and logged.  Nothing in the unit file itself shows that.
    A unit this tool manages has no drop-ins (DEPLOYMENT.md: no TLS drop-in
    either), so every one found is saved beside the replaced units and
    removed, and the caller reloads the daemon.
    """
    retired: list[Path] = []
    for conf in dropin_paths(target):
        saved = target_root(target) / "units" / f"{target.unit}.d.{conf.name}.{dt.datetime.now():%Y%m%d%H%M%S}"
        saved.parent.mkdir(parents=True, exist_ok=True)
        saved.write_text(conf.read_text(encoding="utf-8"), encoding="utf-8")
        conf.unlink()
        log.line(f"install-unit: retired drop-in {conf} (saved as {saved}); its Exec lines "
                 f"would have overridden the unit this tool writes")
        retired.append(conf)
    if retired:
        try:
            (SYSTEMD_USER_DIR / f"{target.unit}.d").rmdir()
        except OSError:
            pass
    return retired


def refuse_dropins(target: Target) -> None:
    """A unit with drop-ins is not the unit this tool wrote; do not start it."""
    found = dropin_paths(target)
    if found:
        listed = ", ".join(str(path) for path in found)
        raise ReleaseError(
            "usage",
            f"{target.unit} has drop-in(s) that override it: {listed}. What systemd would start "
            f"is not what this tool installed. Stop the unit and run: python3 scripts/release.py "
            f"install-unit --target {target.name}",
        )


def report_running_labels(target: Target, log: Log,
                          services: tuple[str, ...] = ("cognita", "workspace-runtime")) -> None:
    """Ask the RUNNING containers what they were built from.

    release.txt says what was staged; these labels say what is actually up.
    The two disagreeing is worth seeing, and it is the whole reason the commit
    is a label (`org.opencontainers.image.revision`) and not only a file.
    """
    for service in services:
        found = subprocess.run(
            ["docker", "ps", "--filter", f"label=com.docker.compose.project={target.project}",
             "--filter", f"label=com.docker.compose.service={service}", "-q"],
            capture_output=True, text=True, check=False, timeout=60,
        ).stdout.split()
        if not found:
            log.line(f"status: container {service}: not running")
            continue
        labels = subprocess.run(
            ["docker", "inspect", "--format",
             '{{index .Config.Labels "org.opencontainers.image.version"}} '
             '{{index .Config.Labels "org.opencontainers.image.revision"}}', found[0]],
            capture_output=True, text=True, check=False, timeout=60,
        ).stdout.strip()
        log.line(f"status: container {service}: labels version/revision = {labels or '(none)'}")


class _ListLog(Log):
    """A Log that keeps its lines instead of printing them, for `status_lines`."""

    def __init__(self) -> None:
        super().__init__(None)
        self.lines: list[str] = []

    def line(self, text: str) -> None:
        self.lines.append(text)

    def raw(self, text: str) -> None:
        self.lines.append(text)


def status_lines(target: Target) -> list[str]:
    """The `status` output, as lines, for a caller that prints or parses it.

    No lock and no printing (design 5.7): `cmd_status` prints them, and the
    `cognita` CLI holds the lock itself and adds its own lines.  The helper
    calls' own chatter goes to a discarded log, so only status lines come back.
    """
    out, quiet = _ListLog(), _ListLog()
    check_release_target_key(target, out)
    link = current_link(target)
    services: tuple[str, ...] = ("cognita", "workspace-runtime")
    if not link.exists():
        out.line(f"status: {target.name} has no current release ({link} does not exist)")
    else:
        directory = link.resolve()
        values = read_release_text(directory)
        out.line(f"status: current -> {directory}")
        for key in ("version", "commit", "profile", "mode", "toolbox_version", "built_at", "full_suite"):
            out.line(f"status: {key}: {values.get(key, '(not recorded)')}")
        # The tag is what the release starts; the ID is what it was built from.
        # They are reported separately because a later build at the same
        # version replaces the ID and leaves the tag alone -- which is the
        # whole point of the tag.
        tags = recorded_release_tags(values)
        services = tuple(tags)
        for key, service in (("image_cognita", "cognita"), ("image_workspace_runtime", "workspace-runtime")):
            if service not in tags:  # a core release has no Workspace runtime image
                continue
            tag = tags[service]
            state = "present" if image_exists(tag) else "MISSING (this release cannot start)"
            out.line(f"status: image {service}: {tag} [{state}]")
            out.line(f"status: image {service} built as: {values.get(key, '(not recorded)')}")
    report_running_labels(target, out, services)
    code, tail = run(["systemctl", "--user", "is-active", target.unit], log=quiet,
                     state="usage", check=False, quiet=True)
    out.line(f"status: unit {target.unit}: {tail.strip() or 'unknown'} (exit {code})")
    try:
        payload = healthz(target.mcp_port, attempts=1, log=quiet)
        out.line(f"status: /healthz version={payload.get('version')} status={payload.get('status')} "
                 f"embed.gpu={payload.get('embed', {}).get('gpu')}")
    except ReleaseError as exc:
        out.line(f"status: /healthz did not answer: {exc}")
    # `current` is a symlink to one of these directories, not a release of its
    # own, and the tool's own working directories are not releases either.
    root = target_root(target)
    versions = sorted(p.name for p in root.iterdir()
                      if p.is_dir() and not p.is_symlink() and (p / "release.txt").is_file()
                      ) if root.is_dir() else []
    out.line(f"status: staged releases: {', '.join(versions) or '(none)'}")
    return out.lines


def _published_ref_used_elsewhere(target: Target, ref: str, pruned: Path) -> bool:
    for other in target_root(target).iterdir():
        if other.is_symlink() or not other.is_dir() or other.resolve() == pruned.resolve():
            continue
        if (other / "release.txt").is_file() and ref in read_release_text(other).get("published_refs", "").split():
            return True
    return False


def prune(target: Target, keep: int, log: Log) -> None:
    current = current_link(target).resolve() if current_link(target).exists() else None
    # `current` is a symlink INTO this directory, so `is_dir()` is true for it
    # and `p != current` compares the link path against a resolved one and is
    # never equal.  Without the symlink test the link itself is a candidate:
    # after `select` of an older release, pruning would read the running
    # release's release.txt through the link, untag its images and then try to
    # rmtree a symlink.  The real directory it points at is excluded by the
    # comparison below, which is what `p != current` is for.
    candidates = [p for p in target_root(target).iterdir()
                  if p.is_dir() and not p.is_symlink()
                  and (p / "release.txt").is_file() and p.resolve() != current]
    candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    kept = candidates[: max(keep - (1 if current else 0), 0)]
    for directory in candidates:
        if directory in kept:
            log.line(f"prune: keeping {directory.name}")
            continue
        # Untag only references this release records. The tag keeps its image
        # available after containers stop; a legacy tag may still be shared by
        # another staged release, so check that before dropping it.
        values = read_release_text(directory)
        commit = values.get("commit", "")
        if commit:
            try:
                tags = recorded_release_tags(values)
            except ReleaseError as exc:
                log.line(f"prune: retaining {directory}: {exc}")
                continue
            untag_failed = False
            for service, tag in tags.items():
                if other_release_uses_tag(tag, directory, log):
                    log.line(f"prune: retaining shared or uncertain {service} tag {tag}")
                    continue
                code, _tail = run(["docker", "image", "rm", tag], log=log, state="usage",
                                  check=False, quiet=True)
                log.line(f"prune: untagged {service} {tag} (exit {code})")
                untag_failed |= bool(code)
            if untag_failed:
                log.line(f"prune: retaining {directory} because an image tag could not be removed")
                continue
            # A release staged from published images also holds the pulled
            # digest references; while one remains the image is not freed, so a
            # pruned local release would keep its ~3 GB (final review of the
            # installer, 2026-09-28).  A ref another staged release of this
            # target still records stays.  A failed removal only means the
            # image is still in use elsewhere; the directory goes regardless.
            for ref in values.get("published_refs", "").split():
                if _published_ref_used_elsewhere(target, ref, directory):
                    log.line(f"prune: retaining published image {ref}: another staged release records it")
                    continue
                code, _tail = run(["docker", "image", "rm", ref], log=log, state="usage",
                                  check=False, quiet=True)
                log.line(f"prune: removed published image {ref} (exit {code})")
        else:
            log.line(f"prune: {directory.name} records no commit; leaving its images tagged")
        shutil.rmtree(directory)
        log.line(f"prune: removed {directory.name}")
    if current:
        log.line(f"prune: never removing the current release: {current.name}")


def other_release_uses_tag(tag: str, excluding: Path | None, log: Log) -> bool:
    """Retain an image when another staged release may still consume its tag."""
    targets = list(TARGETS.values())
    if local_env_file().is_file():
        # The `local` target's releases are scanned too (design 5.1).  An env
        # file that is there but cannot be resolved is uncertainty, and
        # uncertainty retains the tag, as every other case in this function does.
        try:
            targets.append(resolve_target(LOCAL_TARGET))
        except ReleaseError as exc:
            log.line(f"prune: the local target could not be resolved ({exc}); retaining {tag}")
            return True
    for other_target in targets:
        root = target_root(other_target)
        if not root.is_dir():
            continue
        for directory in root.iterdir():
            if (not directory.is_dir() or directory.is_symlink()
                    or (excluding is not None and directory.resolve() == excluding.resolve())):
                continue
            metadata = directory / "release.txt"
            compose = directory / "compose.images.yaml"
            if not metadata.is_file() and not compose.is_file():
                continue
            values = read_release_text(directory)
            if values.get("version") and values.get("commit"):
                try:
                    recorded = recorded_release_tags(values)
                except ReleaseError:
                    log.line(f"prune: incomplete image references at {directory}; retaining {tag}")
                    return True
                if tag in recorded.values():
                    log.line(f"prune: {tag} is also recorded by {directory}")
                    return True
                if compose.is_file() and tag in compose.read_text(encoding="utf-8"):
                    log.line(f"prune: {tag} is also named by {compose}")
                    return True
            else:
                log.line(f"prune: incomplete metadata at {directory}; retaining {tag} conservatively")
                return True
    return False


def retire_build_references(
    images: dict[str, str], *, target: Target, version: str, commit: str,
    built_ids: dict[str, str] | None, staged_directory: Path | None,
    stage_attempted: bool, test_started: bool, run_id: str, log: Log,
) -> None:
    """Drop only this run's build tags after proving every consumer is gone or retained.

    A failed proof leaves the exact tags in place for inspection. The caller uses
    this in ``finally``; cleanup must never replace the build, test, or deploy
    failure that brought execution there.
    """
    try:
        if test_started:
            project = f"cognita-test-{run_id}"
            test_root = Path(tempfile.gettempdir()) / project
            if not _report_residue(project, run_id, log) or test_root.exists():
                log.line(f"build: retaining invocation tags {images}: test stack or root "
                         f"{test_root} was not verified removed")
                return
        if stage_attempted and staged_directory is None:
            log.line(f"build: retaining invocation tags {images}: staging did not finish; "
                     "release image ownership is uncertain")
            return
        if staged_directory is not None:
            values = read_release_text(staged_directory)
            retained = recorded_release_tags(values)
            expected = release_tags(version, commit, target)
            compose = (staged_directory / "compose.images.yaml").read_text(encoding="utf-8")
            if (built_ids is None or values.get("version") != version
                    or values.get("commit") != commit or retained != expected):
                log.line(f"build: retaining invocation tags {images}: staged release metadata "
                         f"at {staged_directory} does not prove ownership")
                return
            for service, tag in retained.items():
                recorded_id = values.get("image_cognita" if service == "cognita"
                                         else "image_workspace_runtime")
                if (tag == images[service] or f"    image: {tag}\n" not in compose
                        or recorded_id != built_ids[service] or image_id(tag) != built_ids[service]):
                    log.line(f"build: retaining invocation tags {images}: {service} "
                             f"is not retained by staged tag {tag} with the built image ID")
                    return
        for service, reference in images.items():
            try:
                if other_release_uses_tag(reference, None, log):
                    log.line(f"build: retaining invocation tag {reference}: a staged release uses it")
                    continue
                observed_id = image_id(reference)
                if built_ids is not None and observed_id != built_ids[service]:
                    log.line(f"build: retaining invocation tag {reference}: its image ID "
                             f"changed from {built_ids[service]} to {observed_id}")
                    continue
                code, tail = run(["docker", "image", "rm", reference], log=log,
                                 state="build-failed", check=False, quiet=True)
                if code:
                    log.line(f"build: retaining invocation tag {reference}: docker image rm "
                             f"exited {code}: {tail}")
                else:
                    log.line(f"build: removed invocation tag {reference}; retained release tags "
                             "and test teardown were verified")
            except Exception as exc:
                log.line(f"build: retaining invocation tag {reference}: cleanup failed ({exc})")
    except Exception as exc:
        log.line(f"build: retaining invocation tags {images}: cleanup proof failed ({exc})")


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------


def cmd_build(args, target: Target, log: Log) -> None:
    repo = REPO_ROOT
    commit = require_clean_checkout(repo, log)
    version = read_version(repo, log)
    export_version(version, log)
    toolbox_version = read_toolbox_version(repo, log)
    with target_lock(target_root(target) / ".lock", log):
        doctor(target, log)
        with tempfile.TemporaryDirectory(prefix="cognita-release-") as tmp:
            images, _ids = build_images(repo, target, version, commit, Path(tmp), log)
        build_toolbox(repo, target, toolbox_version, log)
    log.line(f"build: {version} ({commit}) complete; standalone images remain usable as "
             f"{images['cognita']} and {images['workspace-runtime']} until explicitly removed")


def cmd_build_candidate(args, target: Target, log: Log) -> None:
    if target.name != "test":
        raise ReleaseError("usage", "candidate multi-profile builds are restricted to --target test")
    repo = REPO_ROOT
    commit = require_clean_checkout(repo, log)
    version = read_version(repo, log)
    export_version(version, log)
    profiles = [part.strip() for part in args.profiles.split(",") if part.strip()]
    with target_lock(target_root(target) / ".lock", log):
        build_candidate_images(repo, target, version, commit, profiles,
                               args.write_images, args.export_cpu, args.write_sums, log)
    log.line(f"build: candidate images for {version} ({commit}) are ready")


_REGISTRY_PATTERN = re.compile(r"^[a-z0-9][a-z0-9.-]*(:[0-9]+)?(/[a-z0-9][a-z0-9._-]*)+$")


def _capture(command: list[str], *, timeout: int = 600) -> str:
    """One child process whose WHOLE stdout is wanted (`run` keeps only a tail)."""
    result = subprocess.run(command, capture_output=True, text=True, check=False, timeout=timeout)
    if result.returncode:
        raise ReleaseError("build-failed", f"command failed ({result.returncode}): {' '.join(command)}\n"
                           + result.stderr.strip()[-1200:])
    return result.stdout


def pushed_digest(repository: str, tagged_reference: str) -> str:
    """``repository@sha256:...`` for an image just pushed, from its RepoDigests."""
    digests = json.loads(_capture(["docker", "image", "inspect", "--format", "{{json .RepoDigests}}",
                                   tagged_reference]) or "null")
    matches = sorted({digest for digest in digests or [] if digest.partition("@")[0] == repository})
    if len(matches) != 1:
        raise ReleaseError("build-failed",
                           f"expected exactly one {repository} digest on {tagged_reference}, found {matches}")
    return matches[0]


def manifest_compressed_size(reference: str) -> int:
    """Compressed size in bytes: the sum of the pushed manifest's layer sizes."""
    # A plain-HTTP registry on this host (the installer proof's stand-in for
    # GHCR) needs --insecure, or `docker manifest` tries HTTPS and reports "no
    # such manifest" for an image it has just pushed (seen 2026-09-28 on kei).
    host = reference.split("/", 1)[0]
    insecure = ["--insecure"] if host.split(":", 1)[0] in {"localhost", "127.0.0.1"} else []
    data = json.loads(_capture(["docker", "manifest", "inspect", *insecure, "-v", reference]))
    entries = data if isinstance(data, list) else [data]
    if len(entries) > 1:
        entries = [entry for entry in entries
                   if (entry.get("Descriptor", {}).get("platform") or {}).get("architecture") == "amd64"]
    if len(entries) != 1:
        raise ReleaseError("build-failed", f"cannot pick one manifest from `docker manifest inspect` for {reference}")
    manifest = entries[0].get("SchemaV2Manifest") or entries[0].get("OCIManifest") or {}
    layers = manifest.get("layers") or []
    if not layers:
        raise ReleaseError("build-failed", f"the manifest for {reference} lists no layers")
    return sum(int(layer["size"]) for layer in layers)


def publish_release(repo: Path, target: Target, version: str, commit: str, toolbox_version: str,
                    registry: str, *, amd: bool, nvidia: bool = False, log: Log,
                    output: Path | None = None) -> None:
    """Build, prove and push the published images (design 5.5).  Nothing is
    pushed unless the CPU OCR smoke and the full CPU suite both pass; the
    published-release.txt file is written last, after every digest is known."""
    if not _REGISTRY_PATTERN.fullmatch(registry):
        raise ReleaseError("usage", f"--registry {registry!r} is not host/namespace (for example ghcr.io/owner)")
    output = output or PUBLISHED_RELEASE
    validate_cpu_ocr_inputs(repo)
    # DESIGN-LINUX-INSTALLER 5.5 step 2: the images do not carry the OCR weights, so they are
    # fetched into the test target's model cache root first; the OCR smoke and the test stack both
    # read them from there.
    weights_dir = fetch_ocr_weights(target_models_root(target.env_file), log)
    stage_microsandbox_wheel(repo, log)

    # Fail fast: the CPU app, runtime and Toolbox are what the suite needs, so
    # the (much larger) AMD and NVIDIA images are built only after they pass.  Nothing is
    # pushed before every build and both proofs are done.
    cpu_ref = candidate_app_reference("cpu", version, commit)
    log.line(f"publish: building cpu app image {cpu_ref}")
    docker_build_app(repo, "cpu", version, commit, cpu_ref, log)
    cpu_id = verify_candidate_image(cpu_ref, version, commit)
    runtime_ref = candidate_workspace_reference(version, commit)
    log.line(f"publish: building the Workspace runtime image {runtime_ref}")
    docker_build_workspace_runtime(repo, version, commit, runtime_ref, log)
    verify_candidate_image(runtime_ref, version, commit)
    archive = build_toolbox(repo, target, toolbox_version, log)
    # build_toolbox returns as soon as the archive exists and never checks the image tag, but the push
    # below tags and pushes `toolbox_tag(...)`.  So the tag is ALWAYS reloaded from the archive: the
    # published Toolbox must be the very image every kei target runs, not whatever the shared tag holds.
    # 2026-09-29 (P10a): the tag held an older build of 12.6.0 (created 09-21) while main, beta and test
    # all run the 09-22 archive's build; 14.2.1 published the older one, and adopting the test target
    # then saw a "different" Toolbox and tried to replace the image its existing Workspaces still use.
    # (Final review finding 6, a tag removed by an uninstall elsewhere, is covered by the same load.)
    toolbox_local = toolbox_tag(toolbox_version)
    log.line(f"publish: loading {archive} so {toolbox_local} is exactly the archive's image")
    run(["docker", "load", "-i", str(archive)], log=log, state="build-failed")
    if not image_exists(toolbox_local):
        raise ReleaseError("build-failed", f"docker load of {archive} did not create {toolbox_local}")
    cpu_ocr_smoke(cpu_id, repo, log, weights_dir=weights_dir)
    log.line("publish: running the full cpu suite in the throwaway stack; a failure pushes nothing")
    run_test_stack(repo, target, version, commit, {"cognita": cpu_ref, "workspace-runtime": runtime_ref},
                   log, profile="cpu", mode="full")
    local_refs = {"cognita_cpu": cpu_ref}
    if amd:
        amd_ref = candidate_app_reference("amd", version, commit)
        log.line(f"publish: building amd app image {amd_ref}")
        docker_build_app(repo, "amd", version, commit, amd_ref, log)
        verify_candidate_image(amd_ref, version, commit)
        local_refs["cognita_amd"] = amd_ref
    if nvidia:
        # 15.0.0 (DESIGN-NVIDIA-ACCELERATION 10): a pip-only build (the NVIDIA runtimes come from PyPI wheels), so
        # kei, which has no NVIDIA card, builds it; the live canary happens on the machine that has one.
        nvidia_ref = candidate_app_reference("nvidia", version, commit)
        log.line(f"publish: building nvidia app image {nvidia_ref}")
        docker_build_app(repo, "nvidia", version, commit, nvidia_ref, log)
        verify_candidate_image(nvidia_ref, version, commit)
        local_refs["cognita_nvidia"] = nvidia_ref
    log.line(f"publish: app images to push={sorted(key for key in local_refs if key.startswith('cognita_'))} "
             f"(amd={amd} nvidia={nvidia})")
    local_refs["workspace_runtime"] = runtime_ref
    local_refs["toolbox"] = toolbox_tag(toolbox_version)

    remote_refs = {
        "cognita_cpu": f"{registry}/cognita-app:{version}-cpu",
        "cognita_amd": f"{registry}/cognita-app:{version}-amd",
        "cognita_nvidia": f"{registry}/cognita-app:{version}-nvidia",
        "workspace_runtime": f"{registry}/cognita-workspace-runtime:{version}",
        "toolbox": f"{registry}/cognita-workspace-toolbox:{toolbox_version}",
    }
    digests: dict[str, str] = {}
    sizes: dict[str, int] = {}
    for key, local in local_refs.items():
        remote = remote_refs[key]
        run(["docker", "tag", local, remote], log=log, state="build-failed")
        run(["docker", "push", remote], log=log, state="build-failed")
        digests[key] = pushed_digest(remote.rsplit(":", 1)[0], remote)
        sizes[key] = manifest_compressed_size(digests[key])
        log.line(f"publish: pushed {key}: {digests[key]} ({sizes[key]} compressed bytes)")

    rows = [("version", version), ("commit", commit),
            ("published_at", dt.datetime.now().astimezone().isoformat(timespec="seconds"))]
    rows += [(f"image_ref_{key}", digests[key]) for key in local_refs if key != "toolbox"]
    rows.append(("image_ref_toolbox", digests["toolbox"]))
    rows.append(("toolbox_version", toolbox_version))
    rows += [(f"size_{key}", str(sizes[key])) for key in local_refs if key != "toolbox"]
    rows.append(("size_toolbox", str(sizes["toolbox"])))
    output.parent.mkdir(parents=True, exist_ok=True)
    staging = output.with_name(f".{output.name}.{uuid.uuid4().hex}.staging")
    try:
        staging.write_text(published_release_text(rows), encoding="utf-8", newline="\n")
        staging.replace(output)
    finally:
        staging.unlink(missing_ok=True)
    # Visibility is per package, not per version: once a package is public, every later push to it is
    # public too, so this is a one-time step for a newly created package.
    log.line(f"publish: wrote {output} for {version} ({commit}); commit it as '{version}: publish images'. "
             "The first publish to a new registry creates private packages: make each one public in GitHub "
             "once; later releases stay public")
    build_wsl_artifacts(repo, target, version, commit, output, log)


# The Windows setup chat's recipe (design 19.3).  Its contract, agreed 2026-09-29: it takes the
# published commit, the published-release file and an output directory; it writes a WSL image and a
# source tarball, and prints one `file=<name> sha256=<hex> bytes=<n>` line for each on stdout.
WSL_RECIPE = Path("containers") / "wsl" / "build-wsl-image.sh"
_WSL_KEYS = {"cognita-wsl-": ("wsl_image_sha256", "size_wsl_image"),
             "cognita-src-": ("src_tarball_sha256", "size_src_tarball")}


def build_wsl_artifacts(repo: Path, target: Target, version: str, commit: str, published: Path,
                        log: Log) -> list[Path]:
    """Design 19.3, steps 3-4: run the recipe AFTER the container images are pushed and the
    published-release file is written (the image carries that file, so it cannot come first), then
    append the four keys the Windows build script reads.  The image's own copy of the file lacks
    them, which is harmless: it never needs its own hash.  Without a recipe, nothing happens.
    Uploading the two files to the GitHub release is Doug's step; their paths are logged for him."""
    recipe = repo / WSL_RECIPE
    if not recipe.is_file():
        log.line(f"publish: no WSL image recipe ({WSL_RECIPE}); skipped")
        return []
    out = target_root(target) / "wsl" / version
    out.mkdir(parents=True, exist_ok=True)
    log.line(f"publish: building the WSL image and source tarball for {version} ({commit}) into {out}")
    stdout = _capture(["bash", str(recipe), "--commit", commit, "--published-release", str(published),
                       "--out", str(out), "--repo", str(repo)], timeout=3600)
    rows: list[tuple[str, str]] = []
    files: list[Path] = []
    for line in stdout.splitlines():
        fields = dict(part.split("=", 1) for part in line.split() if "=" in part)
        name = fields.get("file", "")
        for prefix, (sha_key, size_key) in _WSL_KEYS.items():
            if name.startswith(prefix):
                if not re.fullmatch(r"[0-9a-f]{64}", fields.get("sha256", "")) or not fields.get("bytes", "").isdigit():
                    raise ReleaseError("build-failed", f"the WSL recipe printed an unreadable line: {line!r}")
                rows += [(sha_key, fields["sha256"]), (size_key, fields["bytes"])]
                files.append(out / name)
    if sorted(key for key, _ in rows) != sorted(key for pair in _WSL_KEYS.values() for key in pair):
        raise ReleaseError("build-failed", "the WSL recipe did not report both files (image and source "
                           f"tarball); the container images are pushed and {published} has no WSL keys")
    with published.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(published_release_text(rows))
    for path in files:
        log.line(f"publish: WSL artifact {path} — upload it to the GitHub release v{version}")
    return files


def cmd_publish(args, target: Target, log: Log) -> None:
    if target.name != "test":
        raise ReleaseError("usage", "publish is restricted to --target test")
    repo = REPO_ROOT
    commit = require_clean_checkout(repo, log)
    version = read_version(repo, log)
    export_version(version, log)
    toolbox_version = read_toolbox_version(repo, log)
    with target_lock(target_root(target) / ".lock", log):
        publish_release(repo, target, version, commit, toolbox_version, args.registry.rstrip("/"),
                        amd=args.amd, nvidia=args.nvidia, log=log)
    log.line(f"publish: {version} ({commit}) is published")


def cmd_deploy(args, target: Target, log: Log) -> None:
    if args.connector and not args.test:
        raise ReleaseError("usage", "--connector requires --test")
    if args.test:
        target = with_qa_connector(target, args.connector)
    repo = REPO_ROOT
    commit = require_clean_checkout(repo, log)
    version = read_version(repo, log)
    export_version(version, log)
    toolbox_version = read_toolbox_version(repo, log)
    with target_lock(target_root(target) / ".lock", log):
        doctor(target, log)
        # DESIGN-LINUX-INSTALLER 5.8: the images no longer carry the OCR weights, so a deploy makes
        # sure the target's model cache has them BEFORE anything is built or applied; that is what
        # keeps OCR working on kei main, beta and test the first time they run a 14.2 image.
        # Idempotent.  It runs after the cheap refusals (usage, version reuse) on both paths, so a
        # refused deploy never downloads 98 MB first (phase 2 final review, 2026-09-29).
        if args.no_build:
            if target.name != "beta" or not args.images:
                raise ReleaseError("usage", "--no-build deployment is supported only for Beta with --images")
            values = candidate_manifest(args.images)
            source_commit = values["commit"]
            check_version_free(target, version, source_commit, log)
            fetch_ocr_weights(target_models_root(target.env_file), log)
            source_tree = subprocess.run(["git", "-C", str(repo), "rev-parse", f"{source_commit}^{{tree}}"],
                                         capture_output=True, text=True, check=False, timeout=60)
            head_tree = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD^{tree}"],
                                       capture_output=True, text=True, check=False, timeout=60)
            if (source_tree.returncode or head_tree.returncode
                    or source_tree.stdout.strip() != head_tree.stdout.strip()):
                raise ReleaseError("usage", "Beta no-build source tree differs from the accepted candidate")
            profile, mode = args.profile, args.mode
            if (profile, mode) != ("amd", "full"):
                raise ReleaseError("usage", "Beta deployment requires --profile amd --mode full")
            images, test_runner, values = validate_candidate(
                args.images, profile=profile, mode=mode, version=version, commit=source_commit)
            ids = {service: image_id(ref) for service, ref in images.items()}
            toolbox_version = values.get("toolbox_version")
            if not toolbox_version:
                raise ReleaseError("usage", "candidate release.txt is missing toolbox_version")
            if toolbox_version != read_toolbox_version(repo, log):
                raise ReleaseError("usage", "candidate Toolbox version does not match this source tree")
            stage_candidate_toolbox(values, target, log)
            run_id = uuid.uuid4().hex[:12]
            run_test_stack(repo, target, version, source_commit, images, log,
                           run_id=run_id, profile=profile, mode=mode,
                           test_runner_ref=test_runner, no_build=True,
                           candidate_images_dir=args.images)
            directory = stage_release(repo, target, version, source_commit, ids,
                                      toolbox_version, True, log, mode=mode)
            apply_release(repo, target, directory, toolbox_version, log)
            verify_release(repo, target, version, log)
            if args.test:
                with tempfile.TemporaryDirectory(prefix="cognita-release-") as tmp_name:
                    qa_release(repo, target, version, Path(tmp_name), log)
            log.line(f"deploy: reused candidate images for {version} ({source_commit}); no image build ran")
            return
        check_version_free(target, version, commit, log)
        fetch_ocr_weights(target_models_root(target.env_file), log)
        with tempfile.TemporaryDirectory(prefix="cognita-release-") as tmp_name:
            tmp = Path(tmp_name)
            run_id = uuid.uuid4().hex[:12]
            images = build_references(target, version, commit, run_id)
            ids: dict[str, str] | None = None
            directory: Path | None = None
            stage_attempted = False
            test_started = False
            try:
                images, ids = build_images(repo, target, version, commit, tmp, log, run_id=run_id)
                build_toolbox(repo, target, toolbox_version, log)
                if args.test:
                    test_started = True
                    run_test_stack(repo, target, version, commit, images, log, run_id=run_id)
                stage_attempted = True
                directory = stage_release(repo, target, version, commit, ids, toolbox_version,
                                          bool(args.test), log)
                apply_release(repo, target, directory, toolbox_version, log)
                verify_release(repo, target, version, log)
                if args.test:
                    qa_release(repo, target, version, tmp, log)
            finally:
                retire_build_references(
                    images, target=target, version=version, commit=commit, built_ids=ids,
                    staged_directory=directory, stage_attempted=stage_attempted,
                    test_started=test_started, run_id=run_id, log=log)
    log.line(f"deploy: {version} ({commit}) is live on {target.name}"
             + (" and passed QA" if args.test else " (QA not run: release.py qa runs the live self-test)"))


def select_release(target: Target, version: str, log: Log) -> None:
    """Apply and verify an already-staged release.  Takes NO lock (design 5.7):
    `cmd_select` holds it, and so does the `cognita` CLI around its own calls."""
    repo = REPO_ROOT
    directory = release_dir(target, version)
    if not directory.is_dir():
        raise ReleaseError("usage", f"no such release: {directory}")
    values = read_release_text(directory)
    # What has to exist is what compose.images.yaml names: this release's
    # own tags.  The recorded IDs are for `status` -- a later build at the
    # same version legitimately replaces them.
    commit = values.get("commit", "")
    if not commit:
        raise ReleaseError("usage", f"{directory}/release.txt records no commit")
    for service, tag in recorded_release_tags(values).items():
        if not image_exists(tag):
            raise ReleaseError(
                "usage",
                f"{version} cannot start: the {service} image {tag} is missing. "
                f"Deploy {version} again to rebuild and retag it.")
        log.line(f"select: {service} image present: {tag}")
    toolbox_version = values.get("toolbox_version") or read_toolbox_version(repo, log)
    log.line(f"select: {version} ({values.get('commit', 'unknown commit')}) on {target.name} "
             f"in {release_mode(directory)} mode")
    export_version(version, log)
    apply_release(repo, target, directory, toolbox_version, log)
    verify_release(repo, target, version, log)


def cmd_select(args, target: Target, log: Log) -> None:
    with target_lock(target_root(target) / ".lock", log):
        select_release(target, args.version, log)
    log.line(f"select: {args.version} is live on {target.name}")


def cmd_status(args, target: Target, log: Log) -> None:
    for line in status_lines(target):
        log.line(line)


def cmd_qa(args, target: Target, log: Log) -> None:
    """QA on the running release: the live self-test, in test mode, then back."""
    target = with_qa_connector(target, args.connector)
    repo = REPO_ROOT
    with target_lock(target_root(target) / ".lock", log):
        current = current_link(target)
        if not current.exists():
            raise ReleaseError("usage", f"no release is selected on {target.name}")
        version = read_release_text(current).get("version") or read_version(repo, log)
        export_version(version, log)
        with tempfile.TemporaryDirectory(prefix="cognita-release-") as tmp_name:
            qa_release(repo, target, version, Path(tmp_name), log)
    log.line(f"qa: {version} on {target.name} passed the live self-test")


def cmd_test(args, target: Target, log: Log) -> None:
    repo = REPO_ROOT
    commit = require_clean_checkout(repo, log)
    version = read_version(repo, log)
    export_version(version, log)
    with target_lock(target_root(target) / ".lock", log):
        profile = args.profile or target.profile
        mode = args.mode
        if (args.profile is not None or args.no_build) and target.name != "test":
            raise ReleaseError("usage", "mode-aware candidate qualification is restricted to --target test")
        publish_cpu_full = args.no_build and args.images is not None and profile == "cpu" and mode == "full"
        if publish_cpu_full:
            update_cpu_full_qualification(args.images, None)
        doctor(target, log, profile=profile, mode=mode)
        if args.no_build:
            if not args.images:
                raise ReleaseError("usage", "test --no-build requires --images")
            refs, test_runner, values = validate_candidate(
                args.images, profile=profile, mode=mode, version=version, commit=commit)
            receipt = run_test_stack(repo, target, version, commit, refs, log,
                                     profile=profile, mode=mode, test_runner_ref=test_runner,
                                     no_build=True, candidate_images_dir=args.images)
            if publish_cpu_full:
                qualification = {**receipt, **{key: values[key] for key in
                    ("version", "commit", "image_cognita_cpu", "image_workspace_runtime", "test_runner_id",
                     "toolbox_version", "toolbox_sha256")}}
                # Validate the same schema the bundle/update consumers require.
                cpu_full_qualification({**values, "qualification_cpu_full": json.dumps(qualification)})
                update_cpu_full_qualification(args.images, qualification)
            log.line(f"test: {version} ({commit}) passed the {profile}/{mode} suite using existing images")
            return
        with tempfile.TemporaryDirectory(prefix="cognita-release-") as tmp:
            run_id = uuid.uuid4().hex[:12]
            images = build_references(dataclasses.replace(target, profile=profile), version, commit, run_id)
            ids: dict[str, str] | None = None
            test_started = False
            try:
                images, ids = build_images(repo, dataclasses.replace(target, profile=profile), version, commit, Path(tmp), log,
                                           run_id=run_id, mode=mode)
                if mode == "full":
                    build_toolbox(repo, target, read_toolbox_version(repo, log), log)
                test_started = True
                run_test_stack(repo, target, version, commit, images, log, run_id=run_id,
                               profile=profile, mode=mode)
            finally:
                retire_build_references(
                    images, target=target, version=version, commit=commit, built_ids=ids,
                    staged_directory=None, stage_attempted=False,
                    test_started=test_started, run_id=run_id, log=log)
    log.line(f"test: {version} ({commit}) passed the full suite")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="release.py", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    subparsers = parser.add_subparsers(dest="command", required=True)
    release_windows.add_parser(subparsers)

    def with_target(sub, *, required: bool = True):
        sub.add_argument("--target", choices=TARGET_CHOICES, required=required)
        return sub

    deploy = with_target(subparsers.add_parser("deploy"))
    deploy.add_argument("--test", action="store_true",
                        help="run the full suite before applying and the live self-test (QA) after")
    deploy.add_argument("--connector", help="installed combined connector for live QA (required with --test on local)")
    deploy.add_argument("--profile", choices=PROFILES, default="amd")
    deploy.add_argument("--mode", choices=("core", "full"), default="full")
    deploy.add_argument("--images", type=Path)
    deploy.add_argument("--no-build", action="store_true")
    qa = with_target(subparsers.add_parser("qa"))
    qa.add_argument("--connector", help="installed combined connector for live QA (required on local)")
    test = with_target(subparsers.add_parser("test"))
    test.add_argument("--profile", choices=PROFILES)
    test.add_argument("--mode", choices=("core", "full"), default="full")
    test.add_argument("--images", type=Path)
    test.add_argument("--no-build", action="store_true")
    build = with_target(subparsers.add_parser("build"))
    build.add_argument("--profiles", help="candidate profiles: cpu, and optionally amd and nvidia (cpu,amd,nvidia)")
    build.add_argument("--write-images", type=Path)
    build.add_argument("--export-cpu", type=Path)
    build.add_argument("--write-sums", type=Path)
    publisher = with_target(subparsers.add_parser(
        "publish", help="build, prove and push the published images (restricted to --target test)"))
    publisher.add_argument("--registry", required=True, help="host/namespace, for example ghcr.io/owner")
    publisher.add_argument("--amd", action="store_true", help="also build and push the AMD app image")
    publisher.add_argument("--nvidia", action="store_true", help="also build and push the NVIDIA app image")
    bundle = subparsers.add_parser("bundle-windows")
    bundle.add_argument("--target", choices=("test",), required=True)
    bundle.add_argument("--images", type=Path, required=True)
    bundle.add_argument("--cpu-archive", type=Path, required=True)
    bundle.add_argument("--output", type=Path, required=True)
    bundle.add_argument("--mode", choices=("core", "full"), required=True,
                        help="bundle mode selected after the corresponding qualification lane passes")
    select = with_target(subparsers.add_parser("select"))
    select.add_argument("--version", required=True)
    with_target(subparsers.add_parser("status"))
    with_target(subparsers.add_parser("doctor"), required=False)
    with_target(subparsers.add_parser("install-unit"))
    pruner = with_target(subparsers.add_parser("prune"))
    pruner.add_argument("--keep", type=int, default=3)
    return parser


# Commands that start, stop, select or rewrite a table target's running service.  Refused once the
# installer has adopted that target (design section 13): kei main, 2026-09-29.  Its old unit is disabled
# but still on disk for rollback, and `deploy --target main` would re-enable it and start a SECOND stack
# on the same PostgreSQL data folder as the running ./cognita one.  Read-only commands stay allowed.
_ADOPTED_REFUSED = frozenset({"deploy", "select", "qa", "install-unit", "prune"})


def adopted_by_local(target: Target) -> bool:
    """True when the local install's env file points at this table target's config folder."""
    if target.name not in TARGETS:
        return False
    local = read_env_file(local_env_file()).get("COGNITA_CONFIG_ROOT", "")
    own = read_env_file(target.env_file).get("COGNITA_CONFIG_ROOT", "")
    return bool(local and own) and os.path.realpath(local) == os.path.realpath(own)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    # The Windows command owns installation-local logging. Dispatch before
    # deriving a Linux target or creating anything under RELEASES_ROOT.
    if args.command == "deploy-windows":
        return release_windows.main(args, repo=REPO_ROOT, api=sys.modules[__name__])
    try:
        target = resolve_target(args.target) if getattr(args, "target", None) else None
    except ReleaseError as exc:
        # No log file yet: the log directory belongs to the target that failed to resolve.
        print(f"release.py {args.command}: FAILED [{exc.state}] {exc}", file=sys.stderr, flush=True)
        return EXIT_CODES.get(exc.state, 1)
    if target is not None and args.command in _ADOPTED_REFUSED and adopted_by_local(target):
        print(f"release.py {args.command}: FAILED [usage] {target.name} was moved onto ./cognita (its data "
              f"folders are the ones {local_env_file()} names), so release.py no longer starts or selects it. "
              f"Use ./cognita update / status / rollback, or --target {LOCAL_TARGET}.", file=sys.stderr, flush=True)
        return EXIT_CODES.get("usage", 1)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    log = Log(logs_dir(target) / f"{args.command}-{stamp}.log")
    log.line(f"release.py {args.command} target={target.name if target else '(none)'} "
             f"repo={REPO_ROOT} releases={releases_root_for(target)}"
             + (f" profile={target.profile} env={target.env_file} unit={target.unit} "
                f"mcp={target.mcp_port} admin={target.admin_port}" if target else ""))
    try:
        if args.command == "doctor":
            doctor(target, log)
        elif args.command == "build":
            if args.profiles:
                if not args.write_images or not args.export_cpu or not args.write_sums:
                    raise ReleaseError("usage", "candidate build requires --write-images, --export-cpu, and --write-sums")
                cmd_build_candidate(args, target, log)
            else:
                cmd_build(args, target, log)
        elif args.command == "deploy":
            cmd_deploy(args, target, log)
        elif args.command == "select":
            cmd_select(args, target, log)
        elif args.command == "test":
            cmd_test(args, target, log)
        elif args.command == "bundle-windows":
            _bundle_windows(REPO_ROOT, args.images, args.cpu_archive, args.output, log, mode=args.mode)
        elif args.command == "qa":
            cmd_qa(args, target, log)
        elif args.command == "status":
            cmd_status(args, target, log)
        elif args.command == "publish":
            cmd_publish(args, target, log)
        elif args.command == "install-unit":
            install_unit(target, log)
        elif args.command == "prune":
            prune(target, args.keep, log)
        else:  # pragma: no cover - argparse rejects anything else
            raise ReleaseError("usage", f"unknown command: {args.command}")
    except ReleaseError as exc:
        log.line(f"{args.command}: FAILED [{exc.state}] {exc}")
        log.line(f"{args.command}: log is {log.path}")
        log.close()
        return EXIT_CODES.get(exc.state, 1)
    except KeyboardInterrupt:  # pragma: no cover - interactive only
        log.line(f"{args.command}: interrupted; the log is {log.path}")
        log.close()
        return 1
    log.line(f"{args.command}: ok; log is {log.path}")
    log.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
