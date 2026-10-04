"""Unit tests for scripts/cognita_cli.py, the ./cognita installer front (docs/DESIGN-LINUX-INSTALLER.md).

No network, no Docker, no systemd, no real subprocess and NO waiting: every machine fact, every
command and every Admin call goes through the fakes defined here, and the two loops that would
otherwise wait (the Funnel healthz retry and the model-size ticker) take an injected sleeper and a
scripted ticker.  tests/test_cognita_cli_flows.py imports the fakes from this module.

Paths under test are written as POSIX strings (tmp_path.as_posix()) because the tool runs on Linux
and does its path arithmetic with posixpath; the flow tests replace the path-rule check with a
permissive one only because a Windows temp directory is not an absolute POSIX path.  The path rules
themselves are tested directly, here, with real POSIX strings.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))


def _load_cli():
    spec = importlib.util.spec_from_file_location("cognita_cli_under_test", REPO / "scripts" / "cognita_cli.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


cli = _load_cli()
release = cli.release


# --------------------------------------------------------------------------
# Fakes
# --------------------------------------------------------------------------

UBUNTU_2404 = 'ID=ubuntu\nVERSION_ID="24.04"\nVERSION_CODENAME=noble\n'


class FakeHost(cli.Host):
    """A dict-backed machine.  The defaults are a healthy Ubuntu 24.04 x86_64 box with /dev/kvm."""

    def __init__(self, home: str = "/home/tester"):
        self.home_dir = home
        self.files: dict[str, str] = {"/proc/1/comm": "systemd\n", "/etc/os-release": UBUNTU_2404,
                                      "/proc/meminfo": "MemTotal:       16303680 kB\nMemFree: 1 kB\n"}
        self.existing: set[str] = {"/dev/kvm"}
        self.dirs: set[str] = set()
        self.chars: set[str] = {"/dev/kvm"}
        self.readonly: set[str] = set()
        self.unreadable: set[str] = set()
        self.globs: dict[str, list[str]] = {}
        self.gids: dict[str, int] = {"/dev/kvm": 108}
        self.commands: set[str] = {"docker", "sudo"}    # a stock Ubuntu box has sudo (15.0.1 checks for it)
        self.busy_ports: set[int] = set()
        self.free = 500 * 10**9
        self.docker_free: int | None = None
        self.devices: dict[str, int] = {}
        self.machine_name = "x86_64"
        self.sizes: dict[str, int] = {}
        self.temp: dict[str, str] = {}
        self.removed: list[str] = []
        self.links: dict[str, str] = {}
        self.name = "tester"

    def add_dir(self, path: str) -> None:
        self.existing.add(path)
        self.dirs.add(path)

    def read_text(self, path):
        return self.files.get(path)

    def machine(self):
        return self.machine_name

    def exists(self, path):
        return path in self.existing or path in self.dirs or path in self.files

    def is_dir(self, path):
        return path in self.dirs

    def is_char_device(self, path):
        return path in self.chars

    def access(self, path, mode):
        if mode == os.R_OK:
            return path not in self.unreadable
        return path not in self.readonly

    def glob(self, pattern):
        return list(self.globs.get(pattern, []))

    def gid_of(self, path):
        return self.gids.get(path)

    def uid(self):
        return 1000

    def gid(self):
        return 1000

    def user(self):
        return self.name

    def hostname(self):
        return "Box_1"

    def home(self):
        return self.home_dir

    def which(self, name):
        return f"/usr/bin/{name}" if name in self.commands else None

    def realpath(self, path):
        for link, target in self.links.items():   # like os.path.realpath, a link resolves as a PREFIX too
            if path == link or path.startswith(link + "/"):
                return target + path[len(link):]
        return path

    def free_bytes(self, path):
        if self.docker_free is not None and path.startswith("/var/lib/docker"):
            return self.docker_free
        return self.free

    def device_of(self, path):
        return self.devices.get(path, 1)

    def port_free(self, port):
        return port not in self.busy_ports

    def dir_size(self, path):
        return self.sizes.get(path, 0)

    def write_temp(self, text):
        name = f"/tmp/cognita-install-{len(self.temp)}.tmp"
        self.temp[name] = text
        return name

    def remove(self, path):
        self.removed.append(path)


class FakeRunner:
    """Scripted commands.  `script` maps an argv prefix to a Result (or a callable returning one);
    everything else succeeds with no output.  Every call is recorded in `calls` as (kind, argv, stdin)."""

    def __init__(self, order: list | None = None):
        self.script: list[tuple[tuple[str, ...], object]] = []
        self.calls: list[tuple[str, list[str], str | None]] = []
        self.order = order if order is not None else []
        self.ticks = 0
        self.tick_calls = 0
        self.on_call = None
        self.linger = False        # `loginctl show-user` follows enable-linger / disable-linger

    def when(self, *prefix: str, result=None, rc: int = 0, out: str = "", err: str = ""):
        self.script.append((prefix, result if result is not None else cli.Result(rc, out, err)))
        return self

    def _handle(self, kind, argv, stdin=None):
        self.calls.append((kind, list(argv), stdin))
        self.order.append(("run", kind, " ".join(argv)))
        if self.on_call:
            self.on_call(kind, list(argv), stdin)
        if argv[:3] == ["sudo", "loginctl", "enable-linger"]:
            self.linger = True
        elif argv[:3] == ["sudo", "loginctl", "disable-linger"]:
            self.linger = False
        for prefix, result in self.script:
            if tuple(argv[:len(prefix)]) == prefix:
                return result(argv) if callable(result) else result
        return cli.Result(0, "", "")

    def capture(self, argv, *, stdin_text=None, timeout=120):
        return self._handle("capture", argv, stdin_text)

    def stream(self, argv, *, stdin_text=None, check=True, quiet=False, state="usage"):
        result = self._handle("stream", argv, stdin_text)
        if check and result.rc:
            raise release.ReleaseError(state, f"command failed ({result.rc}): {' '.join(argv)}")
        return result

    def interactive(self, argv):
        return self._handle("interactive", argv).rc

    def ticking(self, argv, tick, interval_s, *, stdin_text=None):
        for _ in range(self.ticks):
            self.tick_calls += 1
            tick()
        return self._handle("ticking", argv, stdin_text).rc

    def argvs(self, kind: str | None = None) -> list[list[str]]:
        return [argv for k, argv, _ in self.calls if kind in (None, k)]

    def joined(self, kind: str | None = None) -> list[str]:
        return [" ".join(argv) for argv in self.argvs(kind)]


def healthy_runner(order: list | None = None) -> FakeRunner:
    runner = FakeRunner(order)
    runner.when("systemctl", "--user", "is-system-running", out="running\n")
    runner.when("docker", "version", out="27.0.1\n")
    runner.when("getent", "group", "docker", out="docker:x:999:tester\n")
    runner.when("docker", "compose", "version", out="2.29.0\n")
    runner.when("docker", "context", "show", out="default\n")
    runner.when("docker", "info", "--format", "{{.DockerRootDir}}", out="/var/lib/docker\n")
    runner.when("stat", "-f", "-c", "%T", out="ext2/ext3\n")
    runner.when("loginctl", "show-user",
                result=lambda argv: cli.Result(0, "Linger=yes\n" if runner.linger else "Linger=no\n"))
    return runner


class RecordingLog(cli.InstallLog):
    """InstallLog that also remembers everything said to the screen, whether or not a file is attached
    (and after the file is gone: uninstall deletes its own log)."""

    def __init__(self, path=None):
        self.said: list[str] = []
        super().__init__(path)

    def raw(self, text):
        self.said.append(text)
        super().raw(text)


class ScriptedInput:
    """Stands in for input() and getpass(): answers popped in order; an unexpected prompt fails loudly."""

    def __init__(self, answers=(), secrets=()):
        self.answers = list(answers)
        self.secrets = list(secrets)
        self.prompts: list[str] = []
        self.secret_prompts: list[str] = []

    def ask(self, prompt):
        self.prompts.append(prompt)
        assert self.answers, f"unexpected prompt: {prompt!r}"
        return self.answers.pop(0)

    def secret(self, prompt):
        self.secret_prompts.append(prompt)
        assert self.secrets, f"unexpected password prompt: {prompt!r}"
        return self.secrets.pop(0)


class FakeAdminState:
    """The one Admin server every FakeAdmin talks to: projects, connectors, the GPU policy."""

    def __init__(self, password: str = "correct horse"):
        self.password = password
        self.projects: list[dict] = []
        self.connectors: list[dict] = []
        self.revision = 0
        self.gpu = {"schema": 1, "revision": 0, "knowledge": {"gpu_enabled": False, "gpu_device_ids": []},
                    "ocr": {"device": "cpu", "gpu_device_ids": []}}
        self.restart_required = False
        self.verify_result = {"state": "passed", "cards": [], "runtimes": {}, "cleanup": "passed"}
        self.calls: list[tuple[str, str, object]] = []
        self.fail: dict[tuple[str, str], Exception] = {}
        self.public_url: str | None = None
        self.order: list = []
        self.workspaces: list[dict] = []


class FakeAdmin:
    def __init__(self, state: FakeAdminState, port: int = 0, https: bool = False):
        self.state = state
        self.port = port
        self.https = https

    def login(self, username, password):
        self.state.calls.append(("LOGIN", username, None))
        self.state.order.append(("admin", "LOGIN"))
        if password != self.state.password:
            raise cli.AdminError(401, "Invalid username or password")

    def request(self, method, path, body=None, *, timeout=60):
        state = self.state
        state.calls.append((method, path, body))
        state.order.append(("admin", f"{method} {path}"))
        for (m, prefix), error in state.fail.items():
            if m == method and path.startswith(prefix):
                raise error
        if method == "GET" and path == "/api/session":
            return {"auth_required": True, "authenticated": True}
        if method == "GET" and path == "/api/projects":
            return {"oauth_status": "ready", "projects": [dict(p) for p in state.projects]}
        if method == "POST" and path == "/api/projects":
            state.projects.append({"name": body["name"], "documents_dir": body["documents_dir"]})
            return {"name": body["name"]}
        if method == "DELETE" and path.startswith("/api/projects/"):
            name = path.split("/")[3].split("?")[0]
            state.projects = [p for p in state.projects if p["name"] != name]
            return {"removed": name, "dataDeleted": "deleteData=true" in path}
        if method == "GET" and path == "/api/connectors":
            return {"revision": state.revision, "connectors": [dict(c) for c in state.connectors]}
        if method == "POST" and path == "/api/connectors":
            assert body["expected_revision"] == state.revision, "stale connector revision"
            state.revision += 1
            # Admin derives the slug from the name ("Install proof" -> "install-proof").
            made = {**body, "id": f"c{state.revision}", "slug": body["name"].lower().replace(" ", "-")}
            made.pop("expected_revision")
            state.connectors.append(made)
            return {"revision": state.revision, "connector": made}
        if method == "DELETE" and path.startswith("/api/connectors/"):
            ident = path.split("/")[3].split("?")[0]
            expected = int(path.split("expected_revision=")[1])
            if expected != state.revision:
                raise cli.AdminError(409, "revision conflict")
            state.connectors = [c for c in state.connectors if c["id"] != ident]
            state.revision += 1
            return {"deleted": ident, "revision": state.revision}
        if method == "GET" and path == "/api/settings/gpu-acceleration":
            return self._gpu_status()
        if method == "PATCH" and path == "/api/settings/gpu-acceleration":
            assert body["expected_revision"] == state.gpu["revision"], "stale gpu revision"
            assert set(body) == {"expected_revision", "idempotency_token", "knowledge", "ocr"}
            state.gpu = {"schema": 1, "revision": state.gpu["revision"] + 1,
                         "knowledge": body["knowledge"], "ocr": body["ocr"]}
            state.restart_required = True
            return self._gpu_status()
        if method == "POST" and path == "/api/settings/gpu-acceleration/verify":
            return state.verify_result
        if method == "GET" and path.startswith("/api/workspaces?"):
            return {"workspaces": [dict(w) for w in state.workspaces]}
        if method == "DELETE" and path.startswith("/api/workspaces/"):
            ident = path.split("/")[3]
            assert body["confirm"] is True and isinstance(body["expected_revision"], int)
            for row in state.workspaces:
                if row["workspace_id"] == ident:
                    row["state"] = "deleting"
            return {"accepted": True}
        if method == "PATCH" and path == "/api/settings/public-base-url":
            state.public_url = body["public_base_url"]
            return {"public_base_url": body["public_base_url"], "source": "admin"}
        raise AssertionError(f"unexpected Admin call {method} {path}")

    def _gpu_status(self):
        return {"configured": self.state.gpu, "restart": {"required": self.state.restart_required}}


class FakeReleaseTool:
    """Replaces release.py's new functions (Coder A's) and the slow existing ones, by name.  The pure
    helpers (read_env_file, read_release_text, staged_compose_files, compose_command,
    recorded_release_tags, target_lock, Log, ReleaseError) stay real."""

    def __init__(self, monkeypatch, env_path: Path, order: list, *, version="14.1.0", toolbox="12.6.0"):
        self.env_path = env_path
        self.order = order
        self.version = version
        self.toolbox = toolbox
        self.calls: list[tuple] = []
        self.qa_error: Exception | None = None
        self.qa_stop_checks: list = []               # the stop_check each qa_release call got (design 21.2)
        self.qa_stop: object = None         # called with it; may raise release.Stopped
        self.stage_error: Exception | None = None
        self.apply_error: Exception | None = None
        self.select_error: Exception | None = None
        self.prune_error: Exception | None = None
        self.stage_count = 0
        self.qa_fail_profiles: set[str] = set()
        self.foreign_targets: dict = {}
        self.status = ["status: current -> fake", "status: version: fake"]
        for name in ("resolve_target", "stage_published", "write_folders_fragment", "enable_unit",
                     "status_lines", "select_release", "prune"):
            monkeypatch.setattr(release, name, getattr(self, name), raising=False)
        monkeypatch.setattr(release, "apply_release", self.apply_release)
        monkeypatch.setattr(release, "verify_release", self.verify_release)
        monkeypatch.setattr(release, "qa_release", self.qa_release)
        monkeypatch.setattr(release, "export_version", self.export_version)
        monkeypatch.setattr(release, "TARGETS", self.foreign_targets)

    def env(self) -> dict:
        return release.read_env_file(self.env_path)

    def resolve_target(self, name):
        env = self.env()
        self.calls.append(("resolve_target", name))
        return SimpleNamespace(
            name=name, profile=env.get("COGNITA_ACCELERATION", "cpu"), env_file=self.env_path,
            unit="cognita.service", project="cognita", mcp_port=int(env.get("COGNITA_MCP_HOST_PORT", 8675)),
            admin_port=int(env.get("COGNITA_ADMIN_HOST_PORT", 8676)), connector="install-proof",
            releases_root=Path(env["COGNITA_RELEASES_ROOT"]))

    # The compressed sizes Rig.write_published records, in the order release.stage_published reports the
    # images (None = PostgreSQL, whose size the published file does not record).
    IMAGE_SIZES = (("app", 720_000_000), ("runtime", 210_000_000), ("toolbox", 430_000_000), ("postgres", None))

    def stage_published(self, target, log, on_image=None):
        env = self.env()
        self.order.append(("release", "stage_published"))
        self.calls.append(("stage_published", target.profile, env.get("COGNITA_WORKSPACE")))
        self.stage_count += 1
        if self.stage_error:
            raise self.stage_error
        if on_image is not None:
            full = env.get("COGNITA_WORKSPACE") == "on"
            for name, size in self.IMAGE_SIZES:
                if full or name in ("app", "postgres"):
                    on_image(f"ghcr.io/x/{name}@sha256:{'0' * 64}", size)
        directory = Path(env["COGNITA_RELEASES_ROOT"]) / "local" / env["COGNITA_VERSION"]
        directory.mkdir(parents=True, exist_ok=True)
        mode = "full" if env.get("COGNITA_WORKSPACE") == "on" else "core"
        (directory / "release.txt").write_text(
            f"version: {env['COGNITA_VERSION']}\ncommit: abc123\ntarget: local\nprofile: {target.profile}\n"
            f"mode: {mode}\nimage_ref_cognita: cognita/app:{env['COGNITA_VERSION']}-local-{target.profile}-abc123\n"
            f"image_ref_workspace_runtime: cognita/workspace-runtime:{env['COGNITA_VERSION']}-local-abc123\n"
            f"toolbox_version: {self.toolbox}\nbuilt_at: 2026-09-28T{10 + self.stage_count:02d}:00:00+00:00\n"
            f"published_refs: ghcr.io/x/cognita-app@sha256:aaa ghcr.io/x/toolbox@sha256:bbb\n",
            encoding="utf-8")
        for name in ("compose.yaml", "compose.cpu.yaml", "compose.amd.yaml", "compose.nvidia.yaml", "compose.images.yaml"):
            (directory / name).write_text("services: {}\n", encoding="utf-8")
        return directory

    def write_folders_fragment(self, target, directory, log=None):
        self.order.append(("release", "write_folders_fragment"))
        self.calls.append(("write_folders_fragment", str(directory)))
        if log is not None:
            log.line(f"folders: fake fragment for {Path(directory).name}")

    def apply_release(self, repo, target, directory, toolbox_version, log):
        self.order.append(("release", "apply_release"))
        self.calls.append(("apply_release", directory.name, toolbox_version, target.profile))
        if self.apply_error:
            raise self.apply_error
        self._point_current(directory)

    def _point_current(self, directory: Path):
        if directory.name == "current":     # re-applying the selected release: already selected
            return
        current = directory.parent / "current"
        if current.is_dir() and not current.is_symlink():
            import shutil
            shutil.rmtree(current)
        current.mkdir(exist_ok=True)
        for source in directory.iterdir():
            (current / source.name).write_bytes(source.read_bytes())

    def enable_unit(self, target, log=None):
        self.order.append(("release", "enable_unit"))
        self.calls.append(("enable_unit",))
        if log is not None:
            log.line(f"unit: fake enable of {target.unit}")

    def verify_release(self, repo, target, version, log):
        self.order.append(("release", "verify_release"))
        self.calls.append(("verify_release", version))

    def qa_release(self, repo, target, version, tmp, log, stop_check=None):
        self.order.append(("release", "qa_release"))
        self.calls.append(("qa_release", version, target.profile))
        self.qa_stop_checks.append(stop_check)
        if self.qa_stop is not None:
            # A stand-in for the live self-test the person stopped: what release.run does once its
            # stop_check answers true (design 21.2).
            self.qa_stop(stop_check)
        if self.qa_error:
            raise self.qa_error
        if target.profile in self.qa_fail_profiles:
            raise release.ReleaseError("verify-failed", f"live self-test failed on {target.profile}")

    def select_release(self, target, version, log):
        self.order.append(("release", "select_release"))
        self.calls.append(("select_release", version))
        if self.select_error:
            raise self.select_error
        self._point_current(Path(self.env()["COGNITA_RELEASES_ROOT"]) / "local" / version)

    def prune(self, target, keep, log):
        self.order.append(("release", "prune"))
        self.calls.append(("prune", keep))
        if self.prune_error:
            raise self.prune_error
        log.line(f"prune: fake prune keep={keep}")

    def status_lines(self, target):
        return list(self.status)

    def export_version(self, version, log):
        self.calls.append(("export_version", version))

    def names(self) -> list[str]:
        return [c[0] for c in self.calls]


class Sleeper:
    def __init__(self):
        self.calls: list[float] = []

    def __call__(self, seconds):
        self.calls.append(seconds)


class Rig:
    """A whole fake machine plus the Ctx that drives it."""

    def __init__(self, tmp_path: Path, monkeypatch, *, answers=(), secrets=(), non_interactive=False):
        self.tmp = tmp_path
        self.root = tmp_path.as_posix()
        self.order: list = []
        self.host = FakeHost(home=f"{self.root}/home")
        self.docs = f"{self.root}/docs"
        self.host.add_dir(self.docs)
        Path(self.docs).mkdir(parents=True, exist_ok=True)
        self.data = f"{self.root}/data"
        self.env_path = tmp_path / "config" / "cognita.env"
        self.repo = tmp_path / "repo"
        (self.repo / "containers").mkdir(parents=True)
        (self.repo / "scripts").mkdir()
        # A clone has a .git; plain `update` stops with its own message without one (design 19.9 item 8).
        self.host.existing.add(str(self.repo / ".git"))
        self.environ: dict[str, str] = {}          # the process environment ensure_session_env may set (item 12)
        self.stdin_lines: list[bytes] = []          # what --admin-password-stdin reads, one line per read (item 9)
        self.write_published()
        self.log = RecordingLog(None)
        self.input = ScriptedInput(answers, secrets)
        self.ui = cli.UI(self.log, non_interactive=non_interactive, input_fn=self.input.ask,
                         getpass_fn=self.input.secret)
        self.sh = healthy_runner(self.order)
        self.sleep = Sleeper()
        self.reexec_calls: list[list[str]] = []
        self.http_calls: list[tuple] = []
        self.http_script: list = []
        self.admin_state = FakeAdminState()
        self.admin_state.order = self.order
        self.tool = FakeReleaseTool(monkeypatch, self.env_path, self.order)
        self.ctx = cli.Ctx(
            log=self.log, ui=self.ui, sh=self.sh, host=self.host, repo=self.repo, env_path=self.env_path,
            admin_factory=lambda port, https=False: FakeAdmin(self.admin_state, port, https),
            http=self._http, sleep=self.sleep, reexec=self.reexec_calls.append,
            check_paths=lambda path: [], environ=self.environ, stdin_line=self._stdin_line)

    def _stdin_line(self) -> bytes:
        return self.stdin_lines.pop(0) if self.stdin_lines else b""

    def write_published(self, version="14.1.0", *, nvidia=False):
        """``nvidia=True`` also lists an NVIDIA image (5.1 GB, the design's estimate) the way `publish --nvidia` would."""
        extra = ("image_ref_cognita_nvidia: ghcr.io/dbeachy1/cognita-app@sha256:" + "3" * 64 + "\n"
                 "size_cognita_nvidia: 5100000000\n") if nvidia else ""
        (self.repo / "containers" / "published-release.txt").write_text(
            f"version: {version}\ncommit: {'a' * 40}\npublished_at: 2026-09-28T10:00:00\n"
            "image_ref_cognita_cpu: ghcr.io/dbeachy1/cognita-app@sha256:" + "1" * 64 + "\n"
            "image_ref_cognita_amd: ghcr.io/dbeachy1/cognita-app@sha256:" + "2" * 64 + "\n"
            + extra +
            "size_cognita_cpu: 720000000\nsize_cognita_amd: 20000000000\n"
            "size_workspace_runtime: 210000000\nsize_toolbox: 430000000\ntoolbox_version: 12.6.0\n",
            encoding="utf-8")

    def _http(self, method, url, *, data=None, headers=None, timeout=20):
        self.http_calls.append((method, url))
        return self.http_script.pop(0) if self.http_script else (200, '{"service": "cognita"}')

    def args(self, *argv):
        return cli.build_parser().parse_args(list(argv))

    def install_args(self, *extra):
        return self.args("install", "--documents", self.docs, "--data-dir", self.data,
                         "--admin-user", "admin", *extra)

    def env(self) -> dict:
        return release.read_env_file(self.env_path)

    def screen(self) -> str:
        """Everything said to the user so far, in order."""
        return "\n".join(self.log.said)


@pytest.fixture
def rig(tmp_path, monkeypatch):
    return Rig(tmp_path, monkeypatch)


# --------------------------------------------------------------------------
# Path rules and containment (section 3)
# --------------------------------------------------------------------------


@pytest.mark.parametrize("path", ["/home/u/Documents", "/home/u/OneDrive - Personal", "/data/a b/c", "/x"])
def test_ordinary_paths_and_paths_with_spaces_are_accepted(path):
    assert cli.path_problems(path) == []


@pytest.mark.parametrize("path, fragment", [
    ("relative/path", "absolute"),
    ("/a/b\nc", "newline"),
    ("/a/$HOME", "dollar"),
    ('/a/"b"', "double quote"),
    ("/a/'b'", "single quote"),
    ("/a\\b", "backslash"),
    ("/a/100%", "percent"),
    ("/a/b #c", "space then #"),
    ("/a/b ", "starts or ends"),
])
def test_path_rules_reject_what_compose_systemd_or_the_env_file_would_misread(path, fragment):
    problems = cli.path_problems(path)
    assert problems and any(fragment in p for p in problems), problems


def test_containment_is_refused_in_both_directions_and_between_roots(rig):
    host = rig.host
    # data dir inside a documents root, and a documents root inside the data dir
    assert cli.containment_problems(host, "/home/u/Documents/cognita", ["/home/u/Documents"])
    assert cli.containment_problems(host, "/home/u", ["/home/u/Documents"])
    # identical
    assert cli.containment_problems(host, "/home/u/Documents", ["/home/u/Documents"])
    # two roots nesting, either way round
    assert cli.containment_problems(host, "/data/c", ["/docs/a", "/docs/a/b"])
    assert cli.containment_problems(host, "/data/c", ["/docs/a/b", "/docs/a"])
    # siblings that merely share a name prefix are fine
    assert cli.containment_problems(host, "/data/cognita", ["/data/cognita-docs", "/data/other"]) == []


def test_a_symlink_cannot_hide_an_overlap(rig):
    rig.host.links["/home/u/link"] = "/home/u/Documents"
    assert cli.containment_problems(rig.host, "/home/u/link/cognita", ["/home/u/Documents"])


# --------------------------------------------------------------------------
# The env file (section 3)
# --------------------------------------------------------------------------


def test_the_env_file_round_trips_through_release_read_env_file(tmp_path):
    values = {"COGNITA_RELEASE_TARGET": "local", "COGNITA_VERSION": "14.1.0", "COGNITA_PROJECTS_ROOT":
              "/home/u/OneDrive - Personal", "COGNITA_PROJECTS_ROOT_2": "/data/two", "COGNITA_MCP_HOST_PORT": "8675",
              "COGNITA_FUTURE_KEY": "kept", "COGNITA_KVM_GID": ""}
    text = cli.render_env(values)
    path = tmp_path / "cognita.env"
    path.write_text(text, encoding="utf-8")
    parsed = release.read_env_file(path)
    assert parsed["COGNITA_PROJECTS_ROOT"] == "/home/u/OneDrive - Personal"
    assert parsed["COGNITA_FUTURE_KEY"] == "kept"          # a key this version does not know is not dropped
    assert "COGNITA_KVM_GID" not in parsed                 # an empty value is not written
    keys = [line.split("=")[0] for line in text.splitlines() if "=" in line and not line.startswith("#")]
    assert keys.index("COGNITA_VERSION") < keys.index("COGNITA_PROJECTS_ROOT") < keys.index("COGNITA_PROJECTS_ROOT_2")


@pytest.mark.skipif(os.name != "posix", reason="POSIX file modes")
def test_the_env_file_is_written_0600_and_atomically(rig):
    cli.write_env(rig.ctx, {"COGNITA_VERSION": "1"})
    assert oct(rig.env_path.stat().st_mode & 0o777) == "0o600"
    assert not list(rig.env_path.parent.glob(".*tmp"))


def test_document_roots_are_read_in_slot_order():
    env = {"COGNITA_PROJECTS_ROOT": "/a", "COGNITA_PROJECTS_ROOT_3": "/c", "COGNITA_PROJECTS_ROOT_2": "/b"}
    assert cli.document_roots(env) == ["/a", "/b", "/c"]


def test_published_release_file_parses_and_sizes_follow_the_profile_and_mode(rig):
    published = cli.parse_published((rig.repo / "containers" / "published-release.txt").read_text())
    assert published["version"] == "14.1.0" and published["toolbox_version"] == "12.6.0"
    cpu_core = cli.download_bytes(published, "cpu", False)
    cpu_full = cli.download_bytes(published, "cpu", True)
    amd_full = cli.download_bytes(published, "amd", True)
    assert cpu_full - cpu_core == 210_000_000 + 430_000_000
    assert cpu_core == 720_000_000 + cli.POSTGRES_IMAGE_BYTES + cli.EXPECTED_MODEL_BYTES + cli.OCR_WEIGHTS_BYTES
    assert amd_full > cpu_full
    # Disk = compressed images x4 + models + Workspace import + Toolbox archive + margin (section 6.3).
    images = 720_000_000 + cli.POSTGRES_IMAGE_BYTES + 210_000_000 + 430_000_000
    assert cli.disk_bytes(published, "cpu", True) == (
        images * 4 + cli.EXPECTED_MODEL_BYTES + cli.OCR_WEIGHTS_BYTES + cli.DISK_MARGIN_BYTES
        + cli.WORKSPACE_IMPORT_BYTES + cli.TOOLBOX_ARCHIVE_BYTES)


def test_no_published_release_file_says_what_to_do(rig):
    (rig.repo / "containers" / "published-release.txt").unlink()
    with pytest.raises(cli.CliError) as info:
        cli.read_published(rig.ctx)
    assert "published-release.txt" in str(info.value) and "git pull" in (info.value.hint or "")


# --------------------------------------------------------------------------
# Step 1: every check, with its message (A3)
# --------------------------------------------------------------------------


def _facts(rig, **override):
    facts = cli.collect_facts(rig.ctx, rig.data, [rig.docs], [8675, 8676], rerun=False)
    for key, value in override.items():
        setattr(facts, key, value)
    return facts


def _evaluate(rig, facts, **kw):
    published = cli.read_published(rig.ctx)
    params = dict(published=published, data_dir=rig.data, docs=[rig.docs], profile="cpu", workspace=True,
                  mcp_port=8675, admin_port=8676)
    params.update(kw)
    return cli.evaluate(rig.ctx, facts, **params)


def test_a_healthy_machine_passes_every_check(rig):
    report = _evaluate(rig, _facts(rig))
    assert report.problems == [] and not report.needs_docker and not report.manager_blocked
    assert report.workspace_available and not report.amd_ok


@pytest.mark.parametrize("os_release", [
    'ID=debian\nVERSION_ID="12"\nVERSION_CODENAME=bookworm\n',
    'ID=fedora\nVERSION_ID="40"\n',
    'ID=arch\n',
    'ID=ubuntu\nVERSION_ID="26.04"\nVERSION_CODENAME=resolute\n',
    'ID=homebrew\n',
])
def test_any_linux_distribution_installs_without_force(rig, os_release):
    """15.0.1 (Doug): "It's just Linux. We're Docker." Cognita runs in its containers; the host supplies
    Docker, systemd and an x86_64 CPU, each checked for what it is. Until 15.0.0 anything but Ubuntu 24.04 was
    refused unless --force, which blocked Debian, Fedora and kei (Ubuntu 26.04) for no technical reason."""
    rig.host.files["/etc/os-release"] = os_release
    report = _evaluate(rig, _facts(rig))
    assert report.problems == [], [p.name for p in report.problems]
    assert not [note for note in report.notes if "Forced" in note or "unsupported" in note]
    assert "tested on Ubuntu 24.04; any systemd Linux with Docker is accepted" in "\n".join(rig.log._pending)


def test_a_non_x86_machine_is_refused_for_the_real_reason(rig):
    rig.host.machine_name = "aarch64"
    problems = _evaluate(rig, _facts(rig)).problems
    assert [p.name for p in problems] == ["Processor"]
    assert "x86_64" in problems[0].fix and "aarch64" in problems[0].saw


@pytest.mark.parametrize("os_release, expected", [
    ('ID=ubuntu\nVERSION_CODENAME=noble\n', ("ubuntu", "noble")),
    ('ID=linuxmint\nID_LIKE="ubuntu debian"\nVERSION_CODENAME=wilma\nUBUNTU_CODENAME=noble\n', ("ubuntu", "noble")),
    ('ID=pop\nID_LIKE="ubuntu debian"\nUBUNTU_CODENAME=noble\n', ("ubuntu", "noble")),
    ('ID=debian\nVERSION_CODENAME=bookworm\n', ("debian", "bookworm")),
    # A Debian derivative's own codename is not in Docker's repository; DEBIAN_CODENAME names the base.
    ('ID=linuxmint\nID_LIKE=debian\nVERSION_CODENAME=faye\nDEBIAN_CODENAME=bookworm\n', ("debian", "bookworm")),
    ('ID=kali\nID_LIKE=debian\nVERSION_CODENAME=kali-rolling\n', None),
    ('ID=fedora\nVERSION_ID="40"\n', None),
    ('ID=arch\n', None),
    ('ID=debian\n', None),                       # no codename: the repository line cannot be written
])
def test_the_apt_family_picks_the_repository_docker_and_tailscale_publish(rig, os_release, expected):
    rig.host.files["/etc/os-release"] = os_release
    assert cli.apt_family(rig.host.os_release()) == expected


def _debian_bookworm(rig):
    rig.host.files["/etc/os-release"] = 'ID=debian\nVERSION_ID="12"\nVERSION_CODENAME=bookworm\n'
    rig.host.commands.add("sudo")


def test_docker_is_installed_from_the_debian_repository_on_debian(rig):
    _debian_bookworm(rig)
    facts = _facts(rig)
    rig.sh.calls.clear()
    cli.install_docker(rig.ctx, rig.args("install", "--install-docker", "yes"), facts)
    argvs = [" ".join(argv) for argv in rig.sh.argvs()]
    assert any("https://download.docker.com/linux/debian/gpg" in argv for argv in argvs)
    assert not any("/linux/ubuntu" in argv for argv in argvs)
    # The repository line itself, which is what lands in /etc/apt/sources.list.d/docker.list.
    listing = "".join(rig.host.temp.values())
    assert "https://download.docker.com/linux/debian bookworm stable" in listing
    assert ("GET", "https://download.docker.com/linux/debian/dists/bookworm/Release") in rig.http_calls


def test_a_codename_docker_does_not_publish_stops_before_any_sudo_step(rig):
    """15.0.1 review: the repository line was written before the first `apt-get update`, so a codename Docker
    does not serve left a broken /etc/apt/sources.list.d/docker.list behind for every later update."""
    _debian_bookworm(rig)
    facts = _facts(rig)
    rig.sh.calls.clear()
    rig.http_script.append((404, "Not Found"))
    with pytest.raises(cli.CliError) as info:
        cli.install_docker(rig.ctx, rig.args("install", "--install-docker", "yes"), facts)
    assert "no packages for 'bookworm'" in str(info.value) and "HTTP 404" in str(info.value)
    assert "https://docs.docker.com/engine/install/debian/" in (info.value.hint or "")
    assert not [argv for argv in rig.sh.argvs() if argv[:1] == ["sudo"]] and not rig.host.temp


def test_no_sudo_stops_with_the_fix_before_any_step(rig):
    """Debian installed with a root password has no sudo; the first step used to fail as a bare exit 127."""
    rig.host.files["/etc/os-release"] = 'ID=debian\nVERSION_ID="12"\nVERSION_CODENAME=bookworm\n'
    rig.host.commands.discard("sudo")
    facts = _facts(rig)
    rig.sh.calls.clear()
    with pytest.raises(cli.CliError) as info:
        cli.install_docker(rig.ctx, rig.args("install", "--install-docker", "yes"), facts)
    assert "needs sudo" in str(info.value) and "usermod -aG sudo" in (info.value.hint or "")
    assert not rig.sh.argvs() and not rig.http_calls


def test_docker_missing_on_a_non_debian_distribution_points_at_dockers_instructions(rig):
    rig.host.files["/etc/os-release"] = 'ID=fedora\nVERSION_ID="40"\n'
    rig.host.commands.add("sudo")
    facts = _facts(rig)
    rig.sh.calls.clear()
    with pytest.raises(cli.CliError) as info:
        cli.install_docker(rig.ctx, rig.args("install", "--install-docker", "yes"), facts)
    hint = info.value.hint or ""
    assert "https://docs.docker.com/engine/install/ " in hint and "/install/ubuntu/" not in hint
    assert "fedora 40" in str(info.value)
    assert not rig.sh.argvs()                                                  # nothing was run


def test_tailscale_is_installed_from_the_debian_repository_on_debian(rig):
    rig.host.commands.add("sudo")
    cli.tailscale_install(rig.ctx, ("debian", "bookworm"))
    argvs = [" ".join(argv) for argv in rig.sh.argvs()]
    assert any("https://pkgs.tailscale.com/stable/debian/bookworm.noarmor.gpg" in argv for argv in argvs)
    assert not any("/stable/ubuntu/" in argv for argv in argvs)


def test_remote_access_on_fedora_without_tailscale_points_at_tailscales_page_and_runs_nothing(rig):
    rig.host.files["/etc/os-release"] = 'ID=fedora\nVERSION_ID="40"\n'
    rig.host.commands.add("sudo")
    env = {"COGNITA_MCP_HOST_PORT": "8675"}
    rig.sh.calls.clear()
    with pytest.raises(cli.CliError) as info:
        cli.remote_access(rig.ctx, rig.args("remote-access", "--remote-access", "yes"), env, None, "admin", "pw")
    assert "https://tailscale.com/download/linux" in (info.value.hint or "")
    assert not [argv for argv in rig.sh.argvs() if argv[:1] == ["sudo"]]


def test_selinux_enforcing_docker_is_a_warning_not_a_stop(rig):
    rig.sh.when("docker", "info", "--format", "{{json .SecurityOptions}}",
                out='["name=seccomp,profile=builtin","name=selinux"]\n')
    facts = _facts(rig)
    assert facts.docker_selinux
    report = _evaluate(rig, facts)
    assert report.problems == []
    assert any("SELinux" in warning for warning in report.warnings)


SHARED_ROOT = "44 1 259:5 / / rw,relatime shared:1 - ext4 /dev/nvme0n1p2 rw\n"
PRIVATE_ROOT = "569 535 8:64 / / rw,relatime - ext4 /dev/sde rw,discard\n"


def test_mount_propagation_takes_the_longest_containing_mount_point():
    table = (SHARED_ROOT
             + "90 44 0:50 / /mnt/cognita-roots/1 rw,noatime shared:40 - 9p drvfs rw\n"
             + "91 44 0:51 / /srv/My\\040Docs rw master:7 - ext4 /dev/sdb rw\n"
             + "92 44 0:52 / /srv/data rw - ext4 /dev/sdc rw\n")
    assert cli.mount_propagation(table, "/home/u/docs") == ("shared", "/")
    assert cli.mount_propagation(table, "/mnt/cognita-roots/1/Notes") == ("shared", "/mnt/cognita-roots/1")
    assert cli.mount_propagation(table, "/srv/My Docs/a") == ("slave", "/srv/My Docs")
    assert cli.mount_propagation(table, "/srv/data") == ("private", "/srv/data")
    assert cli.mount_propagation(table, "/srv/database") == ("shared", "/")   # a prefix is not a parent


def test_documents_on_a_private_mount_under_wsl_stop_with_the_command(rig):
    rig.host.files["/proc/1/mountinfo"] = PRIVATE_ROOT
    rig.host.files["/proc/sys/kernel/osrelease"] = "6.18.33.2-microsoft-standard-WSL2\n"
    facts = _facts(rig)
    assert facts.wsl and facts.docs_mount[rig.docs] == ("private", "/")
    report = _evaluate(rig, facts)
    [problem] = report.problems
    assert problem.name == "Documents folder" and "private mount (/)" in problem.saw
    assert "sudo nsenter -t 1 -m -- mount --make-rshared /" in problem.fix
    assert "Cognita Setup" in problem.fix


def test_documents_on_a_shared_mount_pass(rig):
    rig.host.files["/proc/1/mountinfo"] = SHARED_ROOT
    facts = _facts(rig)
    assert not facts.wsl and facts.docs_mount[rig.docs] == ("shared", "/")
    assert _evaluate(rig, facts).problems == []


def test_docker_missing_is_a_prerequisite_not_a_failure(rig):
    rig.host.commands.discard("docker")
    report = _evaluate(rig, _facts(rig))
    assert report.needs_docker and report.problems == []


def test_docker_permission_denied_is_the_group_case(rig):
    rig.sh.script.insert(0, (("docker", "version"), cli.Result(1, "", "permission denied while trying to connect")))
    facts = _facts(rig)
    report = _evaluate(rig, facts)
    assert report.docker_denied and facts.docker_group_member and report.problems == []


def test_a_stopped_docker_daemon_names_the_command(rig):
    rig.sh.script.insert(0, (("docker", "version"), cli.Result(1, "", "Cannot connect to the Docker daemon")))
    report = _evaluate(rig, _facts(rig))
    assert [p.name for p in report.problems] == ["Docker"]
    assert "systemctl enable --now docker" in report.problems[0].fix


def test_docker_desktop_is_refused(rig):
    rig.sh.script.insert(0, (("docker", "context", "show"), cli.Result(0, "desktop-linux\n")))
    report = _evaluate(rig, _facts(rig))
    assert [p.name for p in report.problems] == ["Docker Desktop"]


def test_an_old_docker_and_a_missing_compose_plugin_are_both_listed_together(rig):
    rig.sh.script.insert(0, (("docker", "version"), cli.Result(0, "24.0.7\n")))
    rig.sh.script.insert(0, (("docker", "compose", "version"), cli.Result(1, "", "unknown command")))
    report = _evaluate(rig, _facts(rig))
    assert [p.name for p in report.problems] == ["Docker version", "Docker Compose"]


def test_the_systemd_user_manager_that_cannot_reach_docker_is_detected(rig):
    rig.sh.script.insert(0, (("systemd-run", "--user"), cli.Result(1, "", "permission denied")))
    facts = _facts(rig)
    report = _evaluate(rig, facts)
    assert report.manager_blocked and report.problems == []
    # The probe is exactly the design's command (section 4 step 1).
    assert ["systemd-run", "--user", "--wait", "--pipe", "--quiet", "docker", "info", "--format",
            "{{.ServerVersion}}"] in rig.sh.argvs()


def test_the_user_systemd_manager_not_answering_is_a_problem(rig):
    rig.sh.script.insert(0, (("systemctl", "--user", "is-system-running"), cli.Result(1, "", "Failed to connect to bus")))
    assert [p.name for p in _evaluate(rig, _facts(rig)).problems] == ["systemd user manager"]
    rig.host.files["/proc/1/comm"] = "init\n"
    assert [p.name for p in _evaluate(rig, _facts(rig)).problems] == ["systemd"]


@pytest.mark.parametrize("fstype", ["nfs", "nfs4", "cifs", "smb2", "fuseblk", "fuse", "9p", "virtiofs"])
def test_a_network_or_fuse_filesystem_for_the_data_directory_is_refused(rig, fstype):
    rig.sh.script.insert(0, (("stat", "-f", "-c", "%T"), cli.Result(0, f"{fstype}\n")))
    report = _evaluate(rig, _facts(rig))
    assert [p.name for p in report.problems] == ["Data directory"]
    assert fstype in report.problems[0].saw and "--data-dir" in report.problems[0].fix


def test_an_unwritable_data_directory_is_refused(rig):
    facts = _facts(rig, data_writable=False)
    assert [p.name for p in _evaluate(rig, facts).problems] == ["Data directory"]


@pytest.mark.parametrize("state, fragment", [("missing", "does not exist"), ("notdir", "not a directory"),
                                             ("noread", "not readable"), ("nowrite", "not writable")])
def test_the_documents_folder_must_exist_and_be_readable_and_writable(rig, state, fragment):
    facts = _facts(rig, docs={rig.docs: state})
    report = _evaluate(rig, facts)
    assert [p.name for p in report.problems] == ["Documents folder"]
    assert fragment in report.problems[0].saw


def test_the_documents_folder_states_come_from_the_host(rig):
    rig.host.readonly.add(rig.docs)
    assert _facts(rig).docs[rig.docs] == "nowrite"
    rig.host.unreadable.add(rig.docs)
    assert _facts(rig).docs[rig.docs] == "noread"
    other = cli.collect_facts(rig.ctx, rig.data, [rig.docs + "/nope"], [8675], rerun=False)
    assert other.docs[rig.docs + "/nope"] == "missing"


def test_a_documents_folder_on_removable_media_warns_about_mount_points(rig):
    facts = _facts(rig, docs={"/media/tester/usb": "ok"})
    report = _evaluate(rig, facts, docs=["/media/tester/usb"])
    assert any("nofail" in w for w in report.warnings)


def test_a_port_in_use_names_the_flag_that_changes_it(rig):
    rig.host.busy_ports = {8676}
    report = _evaluate(rig, _facts(rig))
    assert [p.name for p in report.problems] == ["Port"]
    assert "--admin-port" in report.problems[0].fix and "8676" in report.problems[0].saw


def test_ports_held_by_this_installs_own_containers_are_not_a_conflict_on_rerun(rig):
    rig.host.busy_ports = {8675, 8676}
    rig.sh.script.insert(0, (("docker", "ps"), cli.Result(0, "abc123\n")))
    facts = cli.collect_facts(rig.ctx, rig.data, [rig.docs], [8675, 8676], rerun=True)
    assert facts.ports_busy == []
    fresh = cli.collect_facts(rig.ctx, rig.data, [rig.docs], [8675, 8676], rerun=False)
    assert fresh.ports_busy == [8675, 8676] and "named cognita already exists" in fresh.collision


def test_too_little_memory_is_refused_with_the_reason(rig):
    # The 6 GB proof VM: installed, then every OCR call waited for 3 GB free and timed out.
    rig.host.files["/proc/meminfo"] = "MemTotal:        6066688 kB\n"
    report = _evaluate(rig, _facts(rig))
    assert [p.name for p in report.problems] == ["Memory"]
    assert "5.8 GB" in report.problems[0].saw and "8 GB" in report.problems[0].fix
    rig.host.files["/proc/meminfo"] = "MemTotal:        7990000 kB\n"   # an "8 GB" machine
    assert _evaluate(rig, _facts(rig)).problems == []
    del rig.host.files["/proc/meminfo"]                                  # unreadable: not a refusal
    assert _evaluate(rig, _facts(rig)).problems == []


def test_low_disk_where_data_and_docker_share_a_filesystem_is_one_finding(rig):
    rig.host.free = 5 * 10**9
    report = _evaluate(rig, _facts(rig))
    assert [p.name for p in report.problems] == ["Disk space"]
    assert "both live" in report.problems[0].saw and "GB" in report.problems[0].saw


def test_low_disk_on_separate_filesystems_reports_each_one(rig):
    rig.host.free = 5 * 10**9
    rig.host.docker_free = 6 * 10**9
    rig.host.devices = {"/var/lib/docker": 2}
    rig.host.existing.add("/var/lib/docker")
    facts = _facts(rig)
    assert not facts.same_fs
    report = _evaluate(rig, facts)
    assert [p.name for p in report.problems] == ["Disk space", "Disk space"]
    assert "Docker stores images" in report.problems[1].saw


def test_a_smaller_choice_needs_less_disk(rig):
    rig.host.free = 12 * 10**9
    big = _evaluate(rig, _facts(rig), profile="amd", workspace=True)
    small = _evaluate(rig, _facts(rig), profile="cpu", workspace=False)
    assert big.problems and not small.problems


def test_an_install_this_tool_did_not_create_is_refused_and_named(rig):
    rig.sh.script.insert(0, (("docker", "ps"), cli.Result(0, "deadbeef\n")))
    facts = _facts(rig)
    report = _evaluate(rig, facts)
    assert [p.name for p in report.problems] == ["Existing Cognita"]
    assert "named cognita already exists" in report.problems[0].saw
    rig.sh.script.pop(0)
    rig.host.existing.add(f"{rig.host.home()}/.config/systemd/user/cognita.service")
    assert "cognita.service" in _evaluate(rig, _facts(rig)).problems[0].saw


def test_no_kvm_gives_the_c7_message_and_still_installs(rig):
    rig.host.chars.clear()
    report = _evaluate(rig, _facts(rig))
    assert report.problems == [] and not report.workspace_available
    note = next(n for n in report.notes if "hardware virtualization" in n)
    assert note == cli.KVM_MESSAGE and "./cognita install --workspace on" in note


def test_amd_needs_kfd_a_render_node_the_module_and_an_amd_display_controller(rig):
    host = rig.host
    host.existing |= {"/dev/kfd", "/sys/module/amdgpu"}
    host.globs["/dev/dri/renderD*"] = ["/dev/dri/renderD128"]
    host.globs["/dev/dri/card*"] = ["/dev/dri/card1"]
    host.gids.update({"/dev/dri/card1": 44, "/dev/dri/renderD128": 992})
    assert _facts(rig).video_gid == 44 and _facts(rig).render_gid == 992
    assert _evaluate(rig, _facts(rig)).amd_ok                       # no lspci installed: not required
    host.commands.add("lspci")
    rig.sh.when("lspci", "-nn", out="03:00.0 VGA compatible controller [0300]: AMD [1002:744c]\n")
    assert _evaluate(rig, _facts(rig)).amd_ok
    rig.sh.script.insert(0, (("lspci", "-nn"), cli.Result(0, "00:02.0 VGA [0300]: Intel [8086:46a6]\n")))
    assert not _evaluate(rig, _facts(rig)).amd_ok                    # lspci says no AMD display controller
    host.existing.discard("/dev/kfd")
    rig.sh.script.pop(0)
    assert not _evaluate(rig, _facts(rig)).amd_ok


def test_an_amd_card_without_the_kernel_driver_prints_the_driver_message(rig):
    rig.host.commands.add("lspci")
    rig.sh.when("lspci", "-nn", out="03:00.0 Display controller [0380]: AMD [1002:744c]\n")
    report = _evaluate(rig, _facts(rig))
    assert not report.amd_ok and cli.AMD_DRIVER_MESSAGE in report.notes
    assert "rocm.docs.amd.com/projects/install-on-linux" in cli.AMD_DRIVER_MESSAGE


NVIDIA_RUNTIMES = '{"io.containerd.runc.v2":{"path":"runc"},"nvidia":{"path":"nvidia-container-runtime"},"runc":{"path":"runc"}}\n'
NVIDIA_LSPCI = "01:00.0 VGA compatible controller [0300]: NVIDIA Corporation AD102 [10de:2684] (rev a1)\n"
NVIDIA_WSL_SMI_LIB = "/usr/lib/wsl/lib/libnvidia-ml.so.1"


def _nvidia_linux(rig, *, runtime=True, lspci=NVIDIA_LSPCI):
    """A plain Linux box: the kernel driver is loaded, Docker knows the `nvidia` runtime, lspci lists the card."""
    rig.host.existing.add("/proc/driver/nvidia/version")
    rig.sh.when("docker", "info", "--format", "{{json .Runtimes}}",
                out=NVIDIA_RUNTIMES if runtime else '{"runc":{"path":"runc"}}\n')
    if lspci is not None:
        rig.host.commands.add("lspci")
        rig.sh.when("lspci", "-nn", out=lspci)


def _nvidia_wsl(rig, *, runtime=True):
    """WSL2: no /proc/driver/nvidia, the Windows driver's /dev/dxg and libnvidia-ml, and an lspci that lists NOTHING."""
    rig.host.existing |= {"/dev/dxg", NVIDIA_WSL_SMI_LIB}
    rig.sh.when("docker", "info", "--format", "{{json .Runtimes}}",
                out=NVIDIA_RUNTIMES if runtime else '{"runc":{"path":"runc"}}\n')
    rig.host.commands.add("lspci")
    rig.sh.when("lspci", "-nn", out="")


def test_nvidia_on_plain_linux_needs_the_driver_the_runtime_and_an_nvidia_display_controller(rig):
    _nvidia_linux(rig)
    facts = _facts(rig)
    assert (facts.nvidia_driver, facts.nvidia_wsl, facts.nvidia_runtime, facts.lspci_nvidia) == (True, False, True, True)
    assert cli.nvidia_qualifies(facts) and _evaluate(rig, facts).nvidia_ok
    assert ["docker", "info", "--format", "{{json .Runtimes}}"] in rig.sh.argvs("capture")   # asked the way the design says
    # lspci says there is no NVIDIA display controller: not qualified (the card is not this machine's).
    rig.sh.script.insert(0, (("lspci", "-nn"), cli.Result(0, "00:02.0 VGA [0300]: Intel [8086:46a6]\n")))
    facts = _facts(rig)
    assert facts.lspci_nvidia is False and not cli.nvidia_qualifies(facts)
    # A non-display NVIDIA function (an audio device) does not count either.
    rig.sh.script[0] = (("lspci", "-nn"), cli.Result(0, "01:00.1 Audio device [0403]: NVIDIA Corporation [10de:22ba]\n"))
    assert _facts(rig).lspci_nvidia is False


def test_nvidia_without_lspci_is_not_refused_for_it(rig):
    _nvidia_linux(rig, lspci=None)
    facts = _facts(rig)
    assert facts.lspci_nvidia is None and cli.nvidia_qualifies(facts)


def test_nvidia_under_wsl_never_consults_lspci(rig):
    _nvidia_wsl(rig)
    facts = _facts(rig)
    assert (facts.nvidia_driver, facts.nvidia_wsl, facts.nvidia_runtime, facts.lspci_nvidia) == (True, True, True, False)
    assert cli.nvidia_qualifies(facts)          # lspci listed nothing (False) and the WSL condition still wins
    # /dev/dxg alone (no libnvidia-ml.so.1 mounted) is not a driver.
    rig.host.existing.discard(NVIDIA_WSL_SMI_LIB)
    assert not _facts(rig).nvidia_driver


def test_nvidia_qualifies_is_exactly_driver_and_runtime_and_wsl_or_lspci_not_false():
    def facts(**kw):
        return cli.Facts(**kw)

    for driver in (False, True):
        for runtime in (None, False, True):          # None = Docker did not answer: unknown never qualifies
            for wsl in (False, True):
                for lspci in (None, False, True):
                    for version in (None, "550.127", "580.65", "616.92"):
                        expected = (driver and runtime is True and (wsl or lspci is not False)
                                    and version != "550.127")
                        got = cli.nvidia_qualifies(facts(nvidia_driver=driver, nvidia_runtime=runtime,
                                                         nvidia_wsl=wsl, lspci_nvidia=lspci,
                                                         nvidia_driver_version=version))
                        assert got == expected, (driver, runtime, wsl, lspci, version)


PROC_NVIDIA_VERSION = ("NVRM version: NVIDIA UNIX Open Kernel Module for x86_64  {v}  Release Build  "
                       "(dvs-builder@U22-I3-AF03-09-1)  Sun Jul 20 06:17:01 UTC 2025\n"
                       "GCC version:  gcc version 13.3.0 (Ubuntu 13.3.0-6ubuntu2~24.04)\n")


def test_the_nvidia_driver_version_is_read_and_a_driver_below_r580_is_not_offered(rig):
    """15.0 review: nothing checked the version on Linux, so an R550 box was offered NVIDIA, pulled the ~5 GB image
    and only then failed verification."""
    _nvidia_linux(rig)
    rig.host.files["/proc/driver/nvidia/version"] = PROC_NVIDIA_VERSION.format(v="580.65.06")
    facts = _facts(rig)
    assert facts.nvidia_driver_version == "580.65" and cli.nvidia_qualifies(facts)
    rig.host.files["/proc/driver/nvidia/version"] = PROC_NVIDIA_VERSION.format(v="550.127.05")
    facts = _facts(rig)
    assert facts.nvidia_driver_version == "550.127" and not cli.nvidia_qualifies(facts)
    report = _evaluate(rig, facts)
    assert not report.nvidia_ok and cli.NVIDIA_DRIVER_OLD_MESSAGE in report.notes
    assert cli.NVIDIA_RUNTIME_MESSAGE not in report.notes     # the runtime is there; the driver is the reason
    assert "R580" in cli.NVIDIA_DRIVER_OLD_MESSAGE and "nvidia.com/drivers" in cli.NVIDIA_DRIVER_OLD_MESSAGE
    assert "nvidia_driver_version=550.127" in "\n".join(rig.log._pending)


def test_an_unreadable_driver_version_is_not_held_against_the_card(rig):
    """Unknown is not too old: verification still catches it as `driver_too_old`."""
    _nvidia_linux(rig)                           # /proc/driver/nvidia/version exists but has no readable text
    facts = _facts(rig)
    assert facts.nvidia_driver_version is None and cli.nvidia_qualifies(facts)
    assert "NVIDIA driver version could not be read" in "\n".join(rig.log._pending)


def test_under_wsl_the_driver_version_comes_from_the_nvidia_smi_wsl_mounts(rig):
    _nvidia_wsl(rig)
    rig.host.existing.add(cli.NVIDIA_WSL_SMI)
    rig.sh.when(cli.NVIDIA_WSL_SMI, "--query-gpu=driver_version", "--format=csv,noheader", out="616.92\n")
    facts = _facts(rig)
    assert facts.nvidia_driver_version == "616.92" and cli.nvidia_qualifies(facts)
    rig.sh.script.insert(0, ((cli.NVIDIA_WSL_SMI, "--query-gpu=driver_version", "--format=csv,noheader"),
                             cli.Result(0, "566.36\n")))
    assert not cli.nvidia_qualifies(_facts(rig))


def test_an_nvidia_card_without_the_kernel_driver_prints_the_driver_message(rig):
    rig.host.commands.add("lspci")
    rig.sh.when("lspci", "-nn", out=NVIDIA_LSPCI)
    report = _evaluate(rig, _facts(rig))
    assert not report.nvidia_ok and cli.NVIDIA_DRIVER_MESSAGE in report.notes
    assert "Cognita does not install drivers" in cli.NVIDIA_DRIVER_MESSAGE and "nvidia.com/drivers" in cli.NVIDIA_DRIVER_MESSAGE


def test_an_nvidia_driver_without_the_runtime_prints_the_toolkit_message(rig):
    _nvidia_linux(rig, runtime=False)
    report = _evaluate(rig, _facts(rig))
    assert not report.nvidia_ok and cli.NVIDIA_RUNTIME_MESSAGE in report.notes
    assert "NVIDIA Container Toolkit" in cli.NVIDIA_RUNTIME_MESSAGE and "`nvidia` runtime" in cli.NVIDIA_RUNTIME_MESSAGE
    assert "container-toolkit/latest/install-guide.html" in cli.NVIDIA_RUNTIME_MESSAGE
    # WSL says the same when the distro has no toolkit.
    wsl_notes = _evaluate(rig, _facts(rig, nvidia_wsl=True, lspci_nvidia=False)).notes
    assert cli.NVIDIA_RUNTIME_MESSAGE in wsl_notes


def test_every_nvidia_prerequisite_note_names_the_command_that_turns_it_on_afterwards():
    """15.0 second review: a rerun keeps the `cpu` an install saved, so a note that only said "run the install
    again" left a user who fixed the prerequisite on the CPU with no way forward shown."""
    for note in (cli.NVIDIA_DRIVER_MESSAGE, cli.NVIDIA_DRIVER_OLD_MESSAGE, cli.NVIDIA_RUNTIME_MESSAGE,
                 cli.NVIDIA_DOCKER_MESSAGE):
        assert "./cognita install --acceleration nvidia" in note, note


def test_nvidia_says_nothing_when_no_nvidia_card_is_there(rig):
    report = _evaluate(rig, _facts(rig))
    assert not report.nvidia_ok
    assert not [note for note in report.notes if "NVIDIA" in note]


def test_when_docker_cannot_be_asked_the_note_says_so_instead_of_guessing_the_toolkit_is_missing(rig):
    _nvidia_linux(rig)
    rig.host.commands.discard("docker")                    # Docker is not installed yet
    facts = _facts(rig)
    assert facts.docker_state == "missing" and not facts.nvidia_runtime
    report = _evaluate(rig, facts)
    assert not report.nvidia_ok and cli.NVIDIA_DOCKER_MESSAGE in report.notes
    assert cli.NVIDIA_RUNTIME_MESSAGE not in report.notes
    assert not [argv for argv in rig.sh.argvs("capture") if argv[:2] == ["docker", "info"]]


def test_an_unreadable_runtime_list_is_unknown_not_absent_and_is_logged(rig):
    _nvidia_linux(rig)
    rig.sh.script.insert(0, (("docker", "info", "--format", "{{json .Runtimes}}"), cli.Result(0, "not json\n")))
    facts = _facts(rig)
    assert facts.nvidia_driver and facts.nvidia_runtime is None and not cli.nvidia_qualifies(facts)
    assert "could not read Docker's runtime list" in "\n".join(rig.log._pending)
    report = _evaluate(rig, facts)
    assert cli.NVIDIA_DOCKER_MESSAGE in report.notes and cli.NVIDIA_RUNTIME_MESSAGE not in report.notes


def test_a_docker_info_that_fails_leaves_the_runtime_unknown(rig):
    """15.0 review: a daemon that is down (after a host or WSL restart) used to read as "no nvidia runtime"."""
    _nvidia_linux(rig)
    rig.sh.script.insert(0, (("docker", "info", "--format", "{{json .Runtimes}}"),
                             cli.Result(1, "", "Cannot connect to the Docker daemon")))
    facts = _facts(rig)
    assert facts.nvidia_runtime is None and not cli.nvidia_qualifies(facts)
    assert "docker info did not answer" in "\n".join(rig.log._pending)


def test_the_facts_line_carries_the_four_nvidia_facts(rig):
    _nvidia_linux(rig)
    _facts(rig)
    text = "\n".join(rig.log._pending)
    assert "nvidia_driver=True nvidia_wsl=False nvidia_runtime=True lspci_nvidia=True" in text


def test_every_failure_is_listed_together_before_anything_changes(rig):
    rig.host.files["/etc/os-release"] = 'ID=fedora\nVERSION_ID="40"\n'
    rig.host.busy_ports = {8675}
    rig.host.free = 2 * 10**9
    report = _evaluate(rig, _facts(rig, docs={rig.docs: "missing"}))
    assert {p.name for p in report.problems} >= {"Port", "Disk space", "Documents folder"}
    assert "Operating system" not in {p.name for p in report.problems}     # 15.0.1: no distribution gate
    cli.print_problems(rig.ctx, report)
    assert "Fix:" in "\n".join(rig.log._pending)


# --------------------------------------------------------------------------
# UI: non-interactive runs name the missing flag instead of asking (section 2.2)
# --------------------------------------------------------------------------


def test_non_interactive_ask_and_confirm_name_the_flag(tmp_path):
    ui = cli.UI(cli.InstallLog(None), non_interactive=True)
    with pytest.raises(cli.CliError) as ask:
        ui.ask("Where are your documents?", default=None, flag="documents")
    assert "--documents" in (ask.value.hint or "")
    # A question with a shown default takes it unattended (C2).
    assert ui.ask("Admin username", default="admin", flag="admin-user") == "admin"
    # A yes/no PREFERENCE takes its default too; a CONSENT question never does (C4).
    assert ui.confirm("Turn on Workspace?", default=True, flag="workspace", preference=True) is True
    assert ui.confirm("Funnel now?", default=False, flag="remote-access", preference=True) is False
    with pytest.raises(cli.CliError):
        ui.confirm("Install Tailscale now?", default=True, flag="remote-access")
    with pytest.raises(cli.CliError) as confirm:
        ui.confirm("Install Docker Engine now?", default=True, flag="install-docker")
    assert "--install-docker" in (confirm.value.hint or "")
    assert ui.confirm("x?", default=False, preset=True, flag="yes") is True   # a flag answers without asking


def test_get_password_needs_the_file_flag_when_non_interactive(rig):
    rig.ui.non_interactive = True
    with pytest.raises(cli.CliError) as info:
        cli.get_password(rig.ctx, rig.args("install"), prompt="Admin password", twice=True)
    assert "--admin-password-file" in (info.value.hint or "")


def test_the_password_file_is_read_and_left_alone_and_never_logged(rig):
    path = rig.tmp / "pw.txt"
    path.write_text("s3cret-value\n", encoding="utf-8")
    before = path.read_bytes()
    args = rig.args("install", "--admin-password-file", str(path))
    assert cli.get_password(rig.ctx, args, prompt="x", twice=True) == "s3cret-value"
    assert path.read_bytes() == before
    assert "s3cret-value" not in "\n".join(rig.log._pending)


def test_a_typed_password_must_match_twice_and_is_not_empty(rig):
    rig.input.secrets = ["", "one", "two", "same", "same"]
    assert cli.get_password(rig.ctx, rig.args("install"), prompt="Pick", twice=True) == "same"
    assert len(rig.input.secret_prompts) == 5
    assert "same" not in "\n".join(rig.log._pending)


# --------------------------------------------------------------------------
# Step 5: layout, secrets, seeds
# --------------------------------------------------------------------------


def _plan(rig, **kw):
    params = dict(documents=[rig.docs], data_dir=rig.data, admin_user="admin", mcp_port=8675, admin_port=8676)
    params.update(kw)
    return cli.Plan(**params)


def _values(rig, plan=None, existing=None, workspace=True):
    plan = plan or _plan(rig)
    plan.workspace = workspace
    facts = _facts(rig)
    return cli.build_env_values(rig.ctx, plan, facts, existing or {}, cli.read_published(rig.ctx))


def test_the_env_values_carry_every_key_the_proof_needs(rig):
    values = _values(rig)
    assert values["COGNITA_RELEASE_TARGET"] == "local" and values["COGNITA_VERSION"] == "14.1.0"
    for key in ("COGNITA_CONFIG_ROOT", "COGNITA_PROJECTS_ROOT", "COGNITA_POSTGRES_DATA_ROOT",
                "COGNITA_WORKSPACE_DATA_ROOT", "COGNITA_TRANSFER_STAGING_ROOT", "COGNITA_SECRETS_ROOT",
                "COGNITA_MODEL_CACHE_ROOT", "COGNITA_TOOLBOX_IMAGE_CACHE_ROOT", "COGNITA_RELEASES_ROOT",
                "COGNITA_SERVICE_UID", "COGNITA_SERVICE_GID", "COGNITA_KVM_GID", "COGNITA_MCP_HOST_PORT",
                "COGNITA_MCP_BIND_ADDRESS", "COGNITA_ADMIN_HOST_PORT", "COGNITA_ADMIN_BIND_ADDRESS",
                "COGNITA_ACCELERATION", "COGNITA_WORKSPACE"):
        assert values[key], key
    assert values["COGNITA_KVM_GID"] == "108"
    assert values["COGNITA_MCP_BIND_ADDRESS"] == values["COGNITA_ADMIN_BIND_ADDRESS"] == "127.0.0.1"
    assert values["COGNITA_RELEASES_ROOT"] == f"{rig.data}/releases"
    core = _values(rig, workspace=False)
    assert core["COGNITA_WORKSPACE"] == "off" and "COGNITA_KVM_GID" not in core


def test_a_rerun_never_repoints_a_data_root_and_extra_roots_survive(rig):
    existing = {"COGNITA_CONFIG_ROOT": "/elsewhere/config", "COGNITA_PROJECTS_ROOT_2": "/docs/two",
                "COGNITA_LINGER_SET_BY_INSTALLER": "1"}
    values = _values(rig, plan=_plan(rig, documents=[rig.docs, "/docs/two"]), existing=existing)
    assert values["COGNITA_CONFIG_ROOT"] == "/elsewhere/config"
    assert values["COGNITA_PROJECTS_ROOT_2"] == "/docs/two"
    assert values["COGNITA_LINGER_SET_BY_INSTALLER"] == "1"
    shrunk = _values(rig, plan=_plan(rig), existing=existing)
    assert "COGNITA_PROJECTS_ROOT_2" not in shrunk


def test_layout_creates_secrets_seeds_and_the_marker_and_never_seeds_authentication(rig):
    plan, values = _plan(rig), _values(rig)
    cli.ensure_layout(rig.ctx, values, plan)
    secrets_dir, config = Path(values["COGNITA_SECRETS_ROOT"]), Path(values["COGNITA_CONFIG_ROOT"])
    password = (secrets_dir / "postgres.password").read_text()
    assert len(password) == 64 and int(password, 16) >= 0
    assert (secrets_dir / "postgres.dsn").read_text() == f"postgresql://cognita:{password}@postgres:5432/cognita"
    assert len((secrets_dir / "broker.secret").read_text()) == 96
    assert (secrets_dir / "admin_tls_certfile").read_bytes() == b"" and (secrets_dir / "admin_tls_keyfile").read_bytes() == b""
    marker = json.loads((Path(values["COGNITA_WORKSPACE_DATA_ROOT"]) / ".cognita-12-workspaces.json").read_text())
    assert marker["schema"] == 1 and marker["role"] == "workspaces" and marker["root_id"]
    assert (config / "registry.yaml").read_text() == "version: 1\nprojects: []\n"
    assert (config / "connectors.yaml").read_text() == "version: 1\nrevision: 0\nconnectors: []\n"
    assert (config / "acceleration.yaml").read_text() == (
        "schema: 1\nrevision: 0\nknowledge:\n  gpu_enabled: false\n  gpu_device_ids: []\nocr:\n"
        "  device: cpu\n  gpu_device_ids: []\n")
    body = (config / "cognita.yaml").read_text()
    assert "public_base_url: http://127.0.0.1:8675\n" in body and "admin_password_hash: \"\"\n" in body
    assert not (config / "authentication.yaml").exists()
    assert (config / "data").is_dir() and (config / "logs").is_dir()
    assert cli.selftest_root(values).is_dir()          # created user-owned BEFORE Compose can create it root-owned


def test_the_public_url_seed_follows_the_mcp_port(rig):
    plan = _plan(rig, mcp_port=8875, admin_port=8876)
    cli.ensure_layout(rig.ctx, _values(rig, plan=plan), plan)
    text = Path(f"{rig.data}/config/cognita.yaml").read_text()
    assert "public_base_url: http://127.0.0.1:8875\n" in text


def test_no_workspace_means_no_capacity_marker(rig):
    plan = _plan(rig)
    values = _values(rig, plan=plan, workspace=False)
    cli.ensure_layout(rig.ctx, values, plan)
    assert not (Path(values["COGNITA_WORKSPACE_DATA_ROOT"]) / ".cognita-12-workspaces.json").exists()


@pytest.mark.skipif(os.name != "posix", reason="POSIX file modes")
def test_directories_are_0700_and_secrets_0600(rig):
    plan, values = _plan(rig), _values(rig)
    cli.ensure_layout(rig.ctx, values, plan)
    for key in ("COGNITA_CONFIG_ROOT", "COGNITA_SECRETS_ROOT", "COGNITA_POSTGRES_DATA_ROOT"):
        assert oct(Path(values[key]).stat().st_mode & 0o777) == "0o700"
    assert oct((Path(values["COGNITA_SECRETS_ROOT"]) / "broker.secret").stat().st_mode & 0o777) == "0o600"


def test_rerunning_the_layout_rotates_nothing(rig):
    plan, values = _plan(rig), _values(rig)
    cli.ensure_layout(rig.ctx, values, plan)
    secrets_dir = Path(values["COGNITA_SECRETS_ROOT"])
    config = Path(values["COGNITA_CONFIG_ROOT"])
    before = {p.name: p.read_bytes() for p in [*secrets_dir.iterdir(), *config.glob("*.yaml")]}
    (config / "registry.yaml").write_text("version: 1\nprojects:\n  - name: mine\n", encoding="utf-8")
    before["registry.yaml"] = (config / "registry.yaml").read_bytes()
    cli.ensure_layout(rig.ctx, values, plan)
    after = {p.name: p.read_bytes() for p in [*secrets_dir.iterdir(), *config.glob("*.yaml")]}
    assert after == before


def test_a_dsn_that_disagrees_with_its_password_stops_the_install_and_is_preserved(rig):
    plan, values = _plan(rig), _values(rig)
    cli.ensure_layout(rig.ctx, values, plan)
    dsn = Path(values["COGNITA_SECRETS_ROOT"]) / "postgres.dsn"
    dsn.write_text("postgresql://cognita:other@postgres:5432/cognita", encoding="utf-8")
    with pytest.raises(cli.CliError) as info:
        cli.ensure_layout(rig.ctx, values, plan)
    assert "postgres.dsn" in str(info.value)
    assert dsn.read_text() == "postgresql://cognita:other@postgres:5432/cognita"


def test_a_lost_password_file_is_recovered_from_the_dsn_not_rotated(rig):
    plan, values = _plan(rig), _values(rig)
    cli.ensure_layout(rig.ctx, values, plan)
    secrets_dir = Path(values["COGNITA_SECRETS_ROOT"])
    original = (secrets_dir / "postgres.password").read_text()
    (secrets_dir / "postgres.password").write_text("", encoding="utf-8")
    cli.ensure_layout(rig.ctx, values, plan)
    assert (secrets_dir / "postgres.password").read_text() == original


def test_an_invalid_existing_seed_is_named_and_never_overwritten(rig):
    plan, values = _plan(rig), _values(rig)
    config = Path(values["COGNITA_CONFIG_ROOT"])
    config.mkdir(parents=True)
    (config / "connectors.yaml").write_text("\x00\x01 not yaml at all", encoding="utf-8")
    with pytest.raises(cli.CliError) as info:
        cli.ensure_layout(rig.ctx, values, plan)
    assert "connectors.yaml" in str(info.value)
    assert (config / "connectors.yaml").read_text() == "\x00\x01 not yaml at all"


def test_a_symlink_where_a_secret_belongs_is_refused(rig):
    if os.name != "posix":
        pytest.skip("symlinks need POSIX")
    plan, values = _plan(rig), _values(rig)
    secrets_dir = Path(values["COGNITA_SECRETS_ROOT"])
    secrets_dir.mkdir(parents=True)
    (secrets_dir / "elsewhere").write_text("x", encoding="utf-8")
    os.symlink(secrets_dir / "elsewhere", secrets_dir / "broker.secret")
    with pytest.raises(cli.CliError) as info:
        cli.ensure_layout(rig.ctx, values, plan)
    assert "broker.secret" in str(info.value)


def test_the_obsolete_empty_authentication_seed_is_removed_but_real_policy_is_not(rig):
    plan, values = _plan(rig), _values(rig)
    cli.ensure_layout(rig.ctx, values, plan)
    auth = Path(values["COGNITA_CONFIG_ROOT"]) / "authentication.yaml"
    auth.write_bytes(b"version: 1\nrevision: 0\nprojects: {}\n")
    cli.ensure_layout(rig.ctx, values, plan)
    assert not auth.exists()
    auth.write_text("version: 1\nrevision: 3\nprojects:\n  a: {}\n", encoding="utf-8")
    cli.ensure_layout(rig.ctx, values, plan)
    assert auth.exists()


def test_admin_tls_files_are_copied_0600_and_named_in_cognita_yaml(rig):
    cert, key = rig.tmp / "c.pem", rig.tmp / "k.pem"
    cert.write_text("CERT", encoding="utf-8")
    key.write_text("KEY", encoding="utf-8")
    plan = _plan(rig, tls_cert=str(cert), tls_key=str(key), admin_lan=True)
    values = _values(rig, plan=plan)
    cli.ensure_layout(rig.ctx, values, plan)
    secrets_dir = Path(values["COGNITA_SECRETS_ROOT"])
    assert (secrets_dir / "admin_tls_certfile").read_text() == "CERT"
    text = Path(values["COGNITA_CONFIG_ROOT"], "cognita.yaml").read_text()
    assert "admin_tls_certfile: /run/secrets/admin_tls_certfile" in text
    assert "admin_tls_keyfile: /run/secrets/admin_tls_keyfile" in text
    assert values["COGNITA_ADMIN_BIND_ADDRESS"] == "0.0.0.0"
    assert cli.admin_https(values) is True


# --------------------------------------------------------------------------
# sudo discipline (C4): the command and its reason are shown BEFORE it runs
# --------------------------------------------------------------------------


def test_every_sudo_step_prints_its_command_and_reason_before_running(rig):
    shown_first = []

    def check(kind, argv, stdin):
        # At the moment the runner is called, the announcement must already be on screen (in the log).
        shown_first.append("\n".join(rig.log._pending))

    rig.sh.on_call = check
    cli.sudo_step(rig.ctx, "let your account run Docker without sudo", ["usermod", "-aG", "docker", "tester"])
    text = shown_first[-1]
    assert "sudo usermod -aG docker tester" in text and "why: let your account run Docker without sudo" in text
    assert rig.sh.argvs("interactive") == [["sudo", "usermod", "-aG", "docker", "tester"]]


def test_a_failing_sudo_step_stops_with_the_reason_and_the_next_command(rig):
    rig.sh.when("sudo", "apt-get", rc=100)
    with pytest.raises(cli.CliError) as info:
        cli.sudo_step(rig.ctx, "refresh the package lists", ["apt-get", "update"])
    assert "refresh the package lists" in str(info.value) and "./cognita install" in (info.value.hint or "")


# --------------------------------------------------------------------------
# Screens
# --------------------------------------------------------------------------


def test_the_plan_lists_what_will_happen_including_sudo_and_download_size(rig):
    published = cli.read_published(rig.ctx)
    plan = _plan(rig)
    plan.acceleration, plan.workspace = "cpu", True
    text = "\n".join(cli.plan_text(plan, published, profile="cpu", workspace=True, linger_needed=True))
    assert rig.data in text and rig.docs in text and "localhost only" in text
    assert "linger" in text and "GB" in text and "100 Mbit/s" in text and "14.1.0" in text
    assert "linger" not in "\n".join(cli.plan_text(plan, published, profile="cpu", workspace=True,
                                                   linger_needed=False))


def test_the_finish_screen_has_the_real_values_and_every_command(rig):
    values = _values(rig, plan=_plan(rig, documents=[rig.docs, "/docs/two"]))
    values["COGNITA_PROJECTS_ROOT_2"] = "/docs/two"
    text = cli.finish_text(rig.ctx, values, version="14.1.0", admin_user="admin", public=None,
                           warnings=["Heads up."])
    assert text.startswith("Heads up.\n\nCognita 14.1.0 is installed and working.")
    assert "Admin        http://127.0.0.1:8676   user: admin" in text
    assert "MCP (local)  http://127.0.0.1:8675" in text
    assert "not set up — ./cognita remote-access" in text
    assert "Acceleration CPU        Workspace: on" in text
    assert f"Data         {rig.data}" in text and f"Documents    {rig.docs}" in text and "/docs/two" in text
    assert "Admin → Connectors" in text and "Stable MCP URL" in text
    for command in ("./cognita status", "./cognita logs [app|workspace] [-f]", "./cognita start|stop|restart",
                    "./cognita password", "./cognita add-folder PATH", "./cognita update", "./cognita rollback",
                    "./cognita reset index", "./cognita uninstall", "./cognita remote-access"):
        assert command in text, command
    public = cli.finish_text(rig.ctx, values, version="14.1.0", admin_user="admin",
                             public="https://cognita-box.tail1234.ts.net", warnings=[])
    assert "Public       https://cognita-box.tail1234.ts.net" in public


def test_the_install_flags_of_section_2_2_all_parse(rig):
    args = rig.args("install", "--documents", "/d", "--admin-user", "bob", "--admin-password-file", "/p",
                    "--acceleration", "amd", "--workspace", "off", "--data-dir", "/x", "--mcp-port", "9",
                    "--admin-port", "10", "--admin-lan", "--admin-tls-cert", "/c", "--admin-tls-key", "/k",
                    "--remote-access", "yes", "--install-docker", "no", "--tailscale-name", "n",
                    "--adopt", "/e", "--force", "--yes", "--non-interactive")
    assert (args.documents, args.acceleration, args.workspace, args.mcp_port, args.admin_lan) == ("/d", "amd", "off", 9, True)
    assert args.install_docker == "no" and args.yes and args.non_interactive and args.force
    assert rig.args("install").admin_lan is None and rig.args("install", "--no-admin-lan").admin_lan is False
    # --yes skips only the final plan confirmation; plain-HTTP Admin has its own flag (final review, finding 9).
    assert rig.args("install", "--yes").accept_plain_http_admin is False
    assert rig.args("install", "--accept-plain-http-admin").accept_plain_http_admin is True


def test_the_help_says_adopt_is_the_request_to_stop_the_old_service_and_yes_is_not_it(capsys):
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["install", "--help"])
    text = " ".join(capsys.readouterr().out.split())
    assert "This IS the request to stop that target's service" in text and "--yes is not needed" in text
    assert "skip only the final plan confirmation" in text and "--yes does not accept this" in text


def test_the_launcher_is_thin_lf_and_hands_everything_to_the_cli():
    text = (REPO / "cognita").read_bytes()
    assert b"\r" not in text and text.startswith(b"#!/usr/bin/env bash\n")
    lines = text.decode().splitlines()
    assert len(lines) <= 25
    body = "\n".join(lines)
    assert 'exec python3 "$ROOT/scripts/cognita_cli.py" "$@"' in body
    assert "3, 11" in body and "apt-get install -y python3" in body
    assert "dnf install -y python3" in body and "Ubuntu 24.04" not in body     # 15.0.1: not Ubuntu-only
    assert "readlink -f" in body          # the root comes from the file's own path, not the caller's cwd
