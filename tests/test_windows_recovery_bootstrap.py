"""Execute the Windows installer's generated bootstrap with synthetic boundaries.

Linux cases run in an isolated root namespace supplied by the test, never WSL
or a running Cognita installation. PowerShell cases execute the real helper.
"""
from __future__ import annotations

import base64
from contextlib import contextmanager
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import signal
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
INSTALLER = REPO / "scripts/windows/Install-CognitaWindows.ps1"


@contextmanager
def owned_temp():
    with tempfile.TemporaryDirectory(prefix="cognita-windows-recovery-bootstrap-") as name:
        root = Path(name)
        print(f"Owned bootstrap fixture root: {root}", flush=True)
        yield root
    if root.exists():
        raise AssertionError(f"owned test root remains: {root}")


def run_owned(arguments, **kwargs):
    """Bound the process and retain ownership of its complete Linux process group."""
    linux = os.name == "posix"
    with subprocess.Popen(arguments, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          text=True, start_new_session=linux, **kwargs) as process:
        try:
            stdout, stderr = process.communicate(timeout=30)
        except subprocess.TimeoutExpired:
            if linux:
                os.killpg(process.pid, signal.SIGKILL)
            else:
                # PowerShell fixtures create no child processes.
                process.kill()
            stdout, stderr = process.communicate()
            raise AssertionError(f"owned fixture exceeded 30 seconds: {stderr}")
        return subprocess.CompletedProcess(arguments, process.returncode, stdout, stderr)


def function_source(name: str, following: str) -> str:
    source = INSTALLER.read_text(encoding="utf-8")
    start = source.index(f"function {name}(")
    return source[start:source.index(f"\nfunction {following}(", start)]


def here_string(function: str, variable: str) -> str:
    start = function.index(f"${variable}=@'\n") + len(f"${variable}=@'\n")
    return function[start:function.index("\n'@", start)]


def bootstrap_template(mode: str, bundle: Path, record: Path) -> str:
    # Linux CI has no PowerShell. Execute the same literal returned by the pure
    # generation helper; the Windows test below separately proves its expansion.
    template = here_string(function_source("Get-LinuxBootstrapScript", "Initialize-ToolboxCache"), "scriptText")
    replacements = {
        "__INSTALL_ID__": "11111111-1111-4111-8111-111111111111",
        "__BUNDLE_B64__": base64.b64encode(str(bundle).encode()).decode(),
        "__RECORD_B64__": base64.b64encode(str(record).encode()).decode(),
        "__MODE__": mode, "__VERSION__": "13.5.0", "__TOOLBOX_VERSION__": "test-version",
        "__WORKSPACE_FILES__": "--file /srv/cognita/compose.workspace.yaml" if mode == "full" else "",
    }
    for key, value in replacements.items():
        template = template.replace(key, value)
    return template


class PowerShellBootstrapTests(unittest.TestCase):
    def run_ps(self, script):
        executable = shutil.which("pwsh") or shutil.which("powershell.exe")
        if not executable:
            self.skipTest("PowerShell is unavailable; Linux shell execution remains covered")
        with owned_temp() as root:
            path = root / "fixture.ps1"
            path.write_text("Set-StrictMode -Version Latest\n$ErrorActionPreference='Stop'\n" + script,
                            encoding="utf-8-sig")
            return run_owned([executable, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(path)])

    def test_generation_expands_the_executable_template(self):
        definitions = function_source("Get-LinuxBootstrapScript", "Initialize-ToolboxCache")
        result = self.run_ps(definitions + """
$bundle=[pscustomobject]@{Metadata=@{bundle_mode='full';version='13.5.0';toolbox_version='test-version'}}
$record=[pscustomobject]@{installation_id='11111111-1111-4111-8111-111111111111'}
$generated=Get-LinuxBootstrapScript $bundle $record '/synthetic bundle' '/synthetic record'
[Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($generated))
""")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(base64.b64decode(result.stdout.strip()).decode(),
                         bootstrap_template("full", PurePosixPath("/synthetic bundle"), PurePosixPath("/synthetic record")))

    def test_toolbox_loader_uses_persistent_compose_service_and_conditional_import(self):
        definitions = function_source("Initialize-ToolboxCache", "Initialize-InstallationConfiguration")
        result = self.run_ps(definitions + """
$script:Calls=[Collections.Generic.List[object]]::new()
$script:FailVerify=$false
function Say([string]$Message) {}
function Invoke-Compose([string[]]$ComposeArgs,[int]$Timeout=600) {
    $script:Calls.Add(@($ComposeArgs))
    if($script:FailVerify -and $ComposeArgs -contains 'verify'){throw 'not imported'}
}
$record=[pscustomobject]@{mode='core'}
$bundle=[pscustomobject]@{Metadata=@{toolbox_version='test-version'}}
Initialize-ToolboxCache $record $bundle
if($script:Calls.Count){throw 'Core mode contacted the Workspace loader'}
$record.mode='full'
Initialize-ToolboxCache $record $bundle
$verified=@($script:Calls.ToArray())
$script:Calls.Clear();$script:FailVerify=$true
Initialize-ToolboxCache $record $bundle
@{verified=$verified;imported=@($script:Calls.ToArray());mode=$script:CurrentMode}|ConvertTo-Json -Depth 6 -Compress
""")
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = json.loads(result.stdout)
        base = ["run", "--pull", "never", "--rm", "--no-deps", "workspace-runtime",
                "python3", "-m", "cognita.runtime_broker.image_cache"]
        archive = "/var/lib/cognita/toolbox-cache/toolbox-test-version.tar"
        for branch, actions in (("verified", ["materialize-binding", "verify"]),
                                ("imported", ["materialize-binding", "verify", "load"])):
            self.assertEqual(calls[branch], [base + [action, "--archive", archive] for action in actions])
        self.assertEqual(calls["mode"], "full")


@unittest.skipUnless(sys.platform.startswith("linux"), "requires Linux permission semantics; no existing WSL distro is used")
class GeneratedLinuxBootstrapTests(unittest.TestCase):
    def fixture(self, root: Path, mode="full"):
        state = root / "state"
        bundle = root / "bundle with spaces"
        for directory in (state, bundle / "scripts/windows", root / "etc/systemd/system", root / "sbin", root / "fake-bin"):
            directory.mkdir(parents=True, exist_ok=True)
        (root / "etc/wsl.conf").write_text("[boot]\nsystemd=true\n")
        (root / "kvm").touch()
        for name in ("compose.yaml", "compose.cpu.yaml", "compose.cpu.full.images.yaml", "compose.cpu.core.images.yaml", "compose.workspace.yaml", "toolbox.tar"):
            (bundle / name).write_text(f"synthetic {name}\n")
        for name in ("run-selftest.py", "provision_selftest.py", "set-admin-credentials.py"):
            (bundle / "scripts" / name).write_text(f"# synthetic {name}\n")
        for name in ("Prepare-Sources.py", "cognita-session.sh", "Reset-CognitaState.sh"):
            (bundle / "scripts/windows" / name).write_text(f"# synthetic {name}\n")
        fake_bin = root / "fake-bin"
        for name in ("systemctl", "apt-get", "getent", "useradd", "usermod", "groupadd"):
            path = fake_bin / name
            path.write_text("#!/bin/sh\nexit 0\n")
            path.chmod(0o755)
        (fake_bin / "ps").write_text("#!/bin/sh\nprintf 'systemd\\n'\n")
        (fake_bin / "ps").chmod(0o755)
        script = bootstrap_template(mode, bundle, root / "record.json")
        paths = {"/srv/cognita": str(state), "/usr/local/libexec": str(root / "libexec"),
                 "/usr/sbin": str(root / "sbin"), "/etc": str(root / "etc"),
                 "/dev/kvm": str(root / "kvm"), "/app/config": str(state / "config")}
        for original, replacement in paths.items():
            script = script.replace(original, replacement)
        # This fixture has no privilege to create users or chown files to root.
        # Resolve only those synthetic ownership boundaries to the runner's
        # numeric IDs; Docker's legitimate --user UID:GID need not have a passwd
        # entry. GNU install/chown still enforce real directory/file semantics.
        uid, gid = os.getuid(), os.getgid()
        script = script.replace("cognita-admin:cognita-admin", f"{uid}:{gid}")
        script = script.replace("root:root", f"{uid}:{gid}")
        script = script.replace("-o cognita-admin -g cognita-admin", f"-o {uid} -g {gid}")
        real_id = shutil.which("id")
        self.assertIsNotNone(real_id, "Linux fixture requires the native id command")
        # Keep the generated script's service-name lookup intact at its existing
        # synthetic user-account boundary, including distinct UID and GID values.
        (fake_bin / "id").write_text(
            f"#!/bin/sh\nif [ \"$#\" = 2 ] && [ \"$2\" = cognita-admin ]; then\n"
            f"case \"$1\" in -u) printf '%s\\n' {uid};; -g) printf '%s\\n' {gid};; *) exit 2;; esac\n"
            f"else exec '{real_id}' \"$@\"; fi\n")
        (fake_bin / "id").chmod(0o755)
        path = root / "bootstrap.sh"
        path.write_text(script)
        environment = dict(os.environ, PATH=str(fake_bin) + os.pathsep + os.environ["PATH"])
        return state, path, environment

    def execute(self, path, environment, ok=True):
        result = run_owned(["bash", str(path)], env=environment)
        if ok:
            self.assertEqual(result.returncode, 0, result.stderr)
        else:
            self.assertNotEqual(result.returncode, 0)
        self.assertFalse(list(path.parent.rglob(".cognita-install-*")), "bootstrap temporary files remain")
        return result

    def initialize_application(self, state):
        source = here_string(function_source("Initialize-InstallationConfiguration", "Install-Launcher"), "initialize")
        source = source.replace("/app/config", str(state / "config"))
        environment = dict(os.environ, PYTHONPATH=str(REPO / "src"))
        if (state / "config/broker.secret").exists():
            environment["COGNITA_INTERNAL_BEARER_FILE"] = str(state / "config/broker.secret")
        return run_owned([sys.executable, "-c", source], env=environment)

    def test_numeric_runner_ownership_does_not_require_passwd_registration(self):
        with owned_temp() as root, mock.patch("pwd.getpwuid", side_effect=KeyError(os.getuid())):
            state, path, environment = self.fixture(root)
            self.execute(path, environment)
            for name in ("compose.env", "config/postgres.password", "config/broker.secret"):
                metadata = (state / name).stat()
                self.assertEqual((metadata.st_uid, metadata.st_gid), (os.getuid(), os.getgid()))
            installed = (state / "compose.env").read_text()
            self.assertIn(f"COGNITA_SERVICE_UID={os.getuid()}\n", installed)
            self.assertIn(f"COGNITA_SERVICE_GID={os.getgid()}\n", installed)

    def test_real_directory_permissions_policy_initialization_and_rerun_preserve_children(self):
        with owned_temp() as root:
            state, path, environment = self.fixture(root)
            documents = state / "sources/selected-documents"
            nested = state / "workspaces/existing-guest"
            for directory in (documents, nested):
                directory.mkdir(parents=True)
                sentinel = directory / "sentinel.txt"
                sentinel.write_text("selected contents must remain untouched\n")
                sentinel.chmod(0o640)
                if os.getuid() == 0:
                    os.chown(sentinel, 12345, 12345)
            snapshots = [(p, p.read_bytes(), p.stat()) for p in (documents / "sentinel.txt", nested / "sentinel.txt")]
            self.execute(path, environment)
            for name in ("compose.env", "config/postgres.password", "config/broker.secret"):
                metadata = (state / name).stat()
                self.assertEqual((metadata.st_uid, metadata.st_gid), (os.getuid(), os.getgid()))
            installed_environment = (state / "compose.env").read_text()
            self.assertIn(f"COGNITA_SERVICE_UID={os.getuid()}\n", installed_environment)
            self.assertIn(f"COGNITA_SERVICE_GID={os.getgid()}\n", installed_environment)
            for name in ("data", "logs"):
                self.assertEqual((state / "config" / name).stat().st_mode & 0o777, 0o700)
            self.assertFalse((state / "config/authentication.yaml").exists())
            initialized = self.initialize_application(state)
            self.assertEqual(initialized.returncode, 0, initialized.stderr)
            from cognita.auth_policy import AuthenticationPolicyStore
            from cognita.config import load_config
            config = load_config(state / "config/cognita.yaml")
            self.assertEqual(config.public_base_url, "http://127.0.0.1:10675")
            policy = AuthenticationPolicyStore(state / "config/authentication.yaml")
            self.assertTrue(policy.effective_oauth("Self-Test"))
            before = {name: (state / "config" / name).read_bytes() for name in
                      ("postgres.password", "postgres.dsn", "broker.secret", "authentication.yaml", "cognita.yaml")}
            self.execute(path, environment)
            self.assertEqual(before, {name: (state / "config" / name).read_bytes() for name in before})
            for sentinel, content, metadata in snapshots:
                self.assertEqual(sentinel.read_bytes(), content)
                self.assertEqual((sentinel.stat().st_uid, sentinel.stat().st_gid, sentinel.stat().st_mode),
                                 (metadata.st_uid, metadata.st_gid, metadata.st_mode))

    def test_exact_obsolete_seed_recovery_preserves_operator_policy(self):
        with owned_temp() as root:
            state, path, environment = self.fixture(root, "core")
            self.execute(path, environment)
            policy = state / "config/authentication.yaml"
            policy.write_text("version: 1\nrevision: 0\nprojects: {}\n")
            self.execute(path, environment)
            self.assertFalse(policy.exists())
            initialized = self.initialize_application(state)
            self.assertEqual(initialized.returncode, 0, initialized.stderr)
            from cognita.auth_policy import AuthenticationPolicyStore
            store = AuthenticationPolicyStore(policy)
            store.mutate_global(expected_revision=0, oauth_enabled=False)
            content = policy.read_bytes()
            self.execute(path, environment)
            self.assertEqual(policy.read_bytes(), content)
            self.assertFalse(AuthenticationPolicyStore(policy).effective_oauth("Self-Test"))
            self.assertFalse((state / "workspaces").exists(), "core bootstrap allocated Workspace state")

    def test_failed_generation_and_publication_rerun_without_rotating_credentials(self):
        with owned_temp() as root:
            state, path, environment = self.fixture(root)
            openssl = root / "fake-bin/openssl"
            openssl.write_text("#!/bin/sh\nprintf partial\nexit 9\n")
            openssl.chmod(0o755)
            self.execute(path, environment, ok=False)
            self.assertFalse((state / "config/postgres.password").exists(), "partial generated password was published")
            openssl.unlink()
            self.execute(path, environment)
            password = (state / "config/postgres.password").read_bytes()
            generated = state / "compose.windows-projection.yaml"
            previous = generated.read_bytes()
            move = root / "fake-bin/mv"
            move.write_text("#!/bin/sh\ncase \"$*\" in *compose.windows-projection.yaml*) exit 8;; esac\nexec /bin/mv \"$@\"\n")
            move.chmod(0o755)
            self.execute(path, environment, ok=False)
            self.assertEqual(generated.read_bytes(), previous)
            self.assertEqual((state / "config/postgres.password").read_bytes(), password)
            move.unlink()
            self.execute(path, environment)
            self.assertEqual((state / "config/postgres.password").read_bytes(), password)

    def test_invalid_existing_secret_is_preserved_and_package_policy_restored(self):
        with owned_temp() as root:
            state, path, environment = self.fixture(root)
            policy = root / "sbin/policy-rc.d"
            policy.write_text("#!/bin/sh\nexit 17\n")
            policy.chmod(0o750)
            apt = root / "fake-bin/apt-get"
            apt.write_text("#!/bin/sh\nexit 7\n")
            apt.chmod(0o755)
            self.execute(path, environment, ok=False)
            self.assertEqual(policy.read_text(), "#!/bin/sh\nexit 17\n")
            self.assertEqual(policy.stat().st_mode & 0o777, 0o750)
            apt.write_text("#!/bin/sh\nexit 0\n")
            self.execute(path, environment)
            secret = state / "config/broker.secret"
            secret.write_text("broken secret with whitespace\n")
            failed = self.execute(path, environment, ok=False)
            self.assertIn("Broker secret is invalid; it was preserved", failed.stderr)
            self.assertEqual(secret.read_text(), "broken secret with whitespace\n")

    def test_signal_interruption_cleans_pending_write_and_rerun_preserves_credentials(self):
        with owned_temp() as root:
            state, path, environment = self.fixture(root)
            self.execute(path, environment)
            credentials = {name: (state / "config" / name).read_bytes() for name in
                           ("postgres.password", "postgres.dsn", "broker.secret")}
            previous = (state / "compose.env").read_bytes()
            move = root / "fake-bin/mv"
            move.write_text("#!/bin/sh\ncase \"$*\" in *compose.env*) kill -TERM \"$PPID\"; exit 143;; esac\nexec /bin/mv \"$@\"\n")
            move.chmod(0o755)
            result = self.execute(path, environment, ok=False)
            self.assertEqual(result.returncode, 143)
            self.assertEqual((state / "compose.env").read_bytes(), previous)
            move.unlink()
            self.execute(path, environment)
            self.assertEqual(credentials, {name: (state / "config" / name).read_bytes() for name in credentials})

    def test_invalid_operator_configuration_is_preserved(self):
        with owned_temp() as root:
            state, path, environment = self.fixture(root, "core")
            self.execute(path, environment)
            config = state / "config/cognita.yaml"
            invalid = b"mcp_port: not-a-number\npublic_base_url: https://example.test/operator\n"
            config.write_bytes(invalid)
            result = self.initialize_application(state)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(config.read_bytes(), invalid)

    @unittest.skipUnless(os.environ.get("COGNITA_BOOTSTRAP_BIND_FIXTURE"), "requires an explicitly supplied synthetic read-only Docker bind fixture")
    def test_selected_source_bind_is_never_traversed_or_modified(self):
        # The disposable-container runner owns this root and removes its
        # container before the synthetic host source. No live mount is used.
        root = Path(os.environ["COGNITA_BOOTSTRAP_BIND_FIXTURE"])
        state, path, environment = self.fixture(root)
        sentinel = state / "sources/selected-documents/sentinel.txt"
        before = sentinel.stat()
        content = sentinel.read_bytes()
        self.assertTrue(os.path.ismount(sentinel.parent))
        self.execute(path, environment)
        after = sentinel.stat()
        self.assertEqual(sentinel.read_bytes(), content)
        self.assertEqual((after.st_uid, after.st_gid, after.st_mode, after.st_mtime_ns),
                         (before.st_uid, before.st_gid, before.st_mode, before.st_mtime_ns))

    def test_existing_admin_credentials_and_public_url_overrides_survive_initialization(self):
        with owned_temp() as root:
            state, path, environment = self.fixture(root, "core")
            self.execute(path, environment)
            import yaml
            from argon2 import PasswordHasher
            from cognita.config import load_config
            from cognita.public_url import PublicBaseURLStore
            config_path = state / "config/cognita.yaml"
            data = yaml.safe_load(config_path.read_text())
            verifier = PasswordHasher().hash("synthetic-password-for-test-only")
            data.update(admin_username="synthetic-admin", admin_password_hash=verifier,
                        public_base_url="https://example.test/deployment")
            config_path.write_text(yaml.safe_dump(data))
            PublicBaseURLStore(load_config(config_path)).save("https://example.test/admin-override")
            before = config_path.read_bytes()
            result = self.initialize_application(state)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(config_path.read_bytes(), before)
            self.assertEqual(load_config(config_path).public_base_url, "https://example.test/admin-override")
            self.assertEqual(load_config(config_path).admin_password_hash, verifier)


if __name__ == "__main__":
    unittest.main()
