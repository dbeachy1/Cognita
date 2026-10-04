from __future__ import annotations

import asyncio
import ctypes
import logging
import os
from pathlib import Path
import queue
import shutil
import socket
import subprocess
import sys
from tempfile import TemporaryDirectory
import threading
from types import SimpleNamespace

import pytest

from cognita.oauth_service import __main__ as oauth_main

log = logging.getLogger(__name__)


PROJECT_ROOT = Path(__file__).parents[1]

# Hang guards, never pass conditions. The child always either prints
# LISTENING_MARKER or exits (stdout EOF), and always exits once its sentinel
# parent is gone, so both signals are guaranteed in a correct run; the bounds
# only stop a child that hangs alive from hanging the run.
STARTUP_BOUND_S = 60.0
EXIT_BOUND_S = 30.0
LISTENING_MARKER = "cognita-test-oauth-listening"

# Test-owned launcher for the real OAuth child: the package's own main(), plus
# one stdout line once Uvicorn's startup has bound the listening socket.
LAUNCHER_SOURCE = f'''\
import uvicorn

from cognita.oauth_service import __main__ as oauth_main

_startup = uvicorn.Server.startup


async def _announcing_startup(self, sockets=None):
    await _startup(self, sockets=sockets)
    if self.started:
        print({LISTENING_MARKER!r}, flush=True)


uvicorn.Server.startup = _announcing_startup
oauth_main.main()
'''


def _await_listening(child: subprocess.Popen) -> bool:
    """Return True on LISTENING_MARKER, False on stdout EOF (child exiting)."""
    verdict: queue.Queue[bool] = queue.Queue()

    def pump() -> None:
        try:
            for line in child.stdout:
                if line.strip() == LISTENING_MARKER:
                    verdict.put(True)
                    return
        except (OSError, ValueError) as exc:
            log.warning("OAuth test child stdout read failed pid=%s: %r", child.pid, exc)
        verdict.put(False)

    threading.Thread(target=pump, name="oauth-child-stdout", daemon=True).start()
    try:
        listening = verdict.get(timeout=STARTUP_BOUND_S)
    except queue.Empty:
        raise AssertionError(
            f"OAuth child sent no listening marker within {STARTUP_BOUND_S}s "
            f"(still running={child.poll() is None})"
        ) from None
    log.info("OAuth test child pid=%s listening=%s", child.pid, listening)
    if not listening:
        # stdout reached EOF, so the child is exiting; the wait is a guard.
        child.wait(timeout=EXIT_BOUND_S)
    return listening


def _run(command: list[str], cwd: Path, *, env: dict[str, str] | None = None, timeout: float = 120) -> tuple[str, str]:
    process = subprocess.Popen(
        command,
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        process.kill()
        stdout, stderr = process.communicate(timeout=5)
        raise AssertionError(f"owned subprocess timed out: {command!r}\\n{stderr[-2000:]}") from exc
    assert process.returncode == 0, f"{command!r}\\nstdout={stdout[-2000:]}\\nstderr={stderr[-2000:]}"
    return stdout, stderr


def test_parent_alive_posix_and_windows_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(oauth_main.sys, "platform", "linux")
    monkeypatch.setattr(oauth_main.os, "kill", lambda pid, signal: None)
    assert oauth_main._parent_alive(1234)

    def missing(pid, signal):
        raise ProcessLookupError(pid)

    monkeypatch.setattr(oauth_main.os, "kill", missing)
    assert not oauth_main._parent_alive(1234)

    class Kernel:
        exit_code = 259
        closed: list[int] = []

        def OpenProcess(self, access, inherit, pid):
            return 77

        def GetExitCodeProcess(self, handle, pointer):
            pointer._obj.value = self.exit_code
            return 1

        def CloseHandle(self, handle):
            self.closed.append(handle)
            return 1

    kernel = Kernel()
    monkeypatch.setattr(oauth_main.sys, "platform", "win32")
    monkeypatch.setattr(ctypes, "windll", SimpleNamespace(kernel32=kernel), raising=False)
    assert oauth_main._parent_alive(1234)
    kernel.exit_code = 0
    assert not oauth_main._parent_alive(1234)
    assert kernel.closed == [77, 77]

    kernel.OpenProcess = lambda access, inherit, pid: 0
    assert not oauth_main._parent_alive(1234)


@pytest.mark.asyncio
async def test_parent_watcher_sets_server_exit_without_process_sweep(monkeypatch: pytest.MonkeyPatch) -> None:
    class Server:
        should_exit = False

    calls = iter([True, False])
    monkeypatch.setattr(oauth_main, "_parent_alive", lambda pid: next(calls))
    # The watcher's 0.25s poll interval is replaced by a single loop yield, so
    # the test waits on the watcher's own two liveness checks, not on 0.5s of
    # real time fitting inside the 2s bound. Only oauth_main's reference to
    # asyncio is swapped; the event loop keeps the real module.
    real_sleep = asyncio.sleep
    sleeps: list[float] = []

    async def yielding_sleep(delay: float) -> None:
        sleeps.append(delay)
        await real_sleep(0)

    monkeypatch.setattr(oauth_main, "asyncio", SimpleNamespace(sleep=yielding_sleep))
    server = Server()
    await asyncio.wait_for(oauth_main._watch_parent(server, 1234), timeout=5)
    assert server.should_exit is True
    assert len(sleeps) == 2


def test_real_child_exits_after_exact_parent_sentinel_death() -> None:
    config = None
    parent = None
    child = None
    with TemporaryDirectory(prefix="cognita-oauth8-parent-watch-") as owned:
        root = Path(owned)
        data = root / "data"
        data.mkdir()
        registry = root / "registry.yaml"
        registry.write_text(
            "version: 1\nprojects:\n  - name: demo\n    documents_dir: docs\n    data_dir: project\n    enabled: true\n",
            encoding="utf-8",
        )
        config = root / "cognita.yaml"
        config.write_text(
            "\n".join([
                f"data_root: {data}",
                f"registry_path: {registry}",
                "public_base_url: https://kei.example",
                f"oauth_service_store_path: {data / 'oauth.sqlite3'}",
                "admin_username: admin",
                "admin_password_hash: '$argon2id$v=19$m=65536,t=3,p=4$abcdefghijklmnop$abcdefghijklmnop'",
            ]),
            encoding="utf-8",
        )
        env = dict(os.environ)
        env["PYTHONPATH"] = str(PROJECT_ROOT / "src")
        # The sentinel parent blocks on its own stdin until the test terminates
        # it, instead of sleeping 30s: its lifetime no longer races how long
        # the child takes to start on a loaded machine.
        parent = subprocess.Popen(
            [sys.executable, "-c", "import sys; sys.stdin.read()"],
            stdin=subprocess.PIPE,
        )
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        # Superseded: this ran `-m cognita.oauth_service` directly. The launcher
        # runs the same main() and prints LISTENING_MARKER once Uvicorn has
        # bound its socket, which the test waits on instead of polling.
        launcher = root / "oauth_child_launcher.py"
        launcher.write_text(LAUNCHER_SOURCE, encoding="utf-8")
        child = subprocess.Popen(
            [
                sys.executable,
                str(launcher),
                "--config",
                str(config),
                "--port",
                str(port),
                "--parent-pid",
                str(parent.pid),
            ],
            cwd=PROJECT_ROOT,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            key_path = data / ".oauth-service-key"
            # Superseded: this polled for the key file every 0.1s for up to
            # 20s. It now waits on the child's own listening signal (the key is
            # written by bootstrap before Uvicorn starts, so it must exist).
            listening = _await_listening(child)
            assert listening, "child exited before listening: " + (
                child.stderr.read() if child.stderr else "child exited"
            )
            assert key_path.exists(), "child did not initialize its owned key"
            assert child.poll() is None
            parent.terminate()
            parent.wait(timeout=5)
            # Hang guard: the child's parent watcher guarantees the exit.
            child.wait(timeout=EXIT_BOUND_S)
            assert child.returncode == 0, child.stderr.read() if child.stderr else ""
        finally:
            if child is not None and child.poll() is None:
                child.kill()
                child.wait(timeout=5)
            if parent is not None and parent.poll() is None:
                parent.kill()
                parent.wait(timeout=5)
            if parent is not None and parent.stdin is not None:
                parent.stdin.close()
            if child is not None:
                child.communicate(timeout=5)
    assert not root.exists()


def test_installed_wheel_contains_oauth_host_and_exact_pins() -> None:
    with TemporaryDirectory(prefix="cognita-oauth8-wheel-") as owned:
        root = Path(owned)
        source = root / "source"
        shutil.copytree(PROJECT_ROOT / "src", source / "src")
        shutil.copy2(PROJECT_ROOT / "pyproject.toml", source / "pyproject.toml")
        shutil.copy2(PROJECT_ROOT / "README.md", source / "README.md")
        dist = root / "dist"
        install = root / "install"
        _run(
            [sys.executable, "-m", "build", "--wheel", "--no-isolation", "--outdir", str(dist)],
            source,
        )
        wheels = list(dist.glob("*.whl"))
        assert len(wheels) == 1
        _run(
            [
                sys.executable,
                "-m",
                "pip",
                "install",
                "--no-deps",
                "--no-cache-dir",
                "--target",
                str(install),
                str(wheels[0]),
            ],
            source,
        )
        probe = """
import importlib.metadata
import importlib.util
from pathlib import Path
metadata = importlib.metadata.metadata('cognita')
requirements = set(metadata.get_all('Requires-Dist') or [])
assert 'Django==6.1.1' in requirements
assert 'django-oauth-toolkit==3.4.1' in requirements
assert 'oauthlib==3.3.1' in requirements
module = importlib.util.find_spec('cognita.oauth_service.__main__')
assert module and Path(module.origin).is_relative_to(Path(r'''INSTALL'''))
""".replace("INSTALL", str(install).replace("\\", "\\\\"))
        env = dict(os.environ)
        env["PYTHONPATH"] = str(install)
        _run([sys.executable, "-c", probe], source, env=env)
    assert not root.exists()
