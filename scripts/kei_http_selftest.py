#!/usr/bin/env python3
"""Run the public MCP self-test against a RUNNING Cognita stack (13.0 §7.3).

    printf '%s\n' "$KEY" | python3 scripts/kei_http_selftest.py live --mode full \
      --compose-project <project> --env-file <env> --compose-file <f> [...] \
      --connector <slug> [--mcp-port 8675] [--log-dir <dir>]

The runner executes inside the stack's own app container, because the host has
Docker but not Cognita's dependencies. The key arrives on stdin and never
appears in argv, a log, or any environment but the child shell that reads it.
The required mode selects the applicable checks: full includes Workspace and
bridge sections; core asserts their absence and checks bounded stale calls.

`write_config` also lives here: it generates the complete synthetic
installation that `scripts/release.py test` starts its throwaway stack from,
so there is one generator rather than two that drift.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import NamedTuple

from cognita.connectors import PUBLIC_CONTRACT_VERSION

PROJECT = "Self-Test"
# 13.0 §7.1: a second, populated project in the generated installation. The
# test-mode principal is scoped to `Self-Test` alone, and that scope can only
# be proven against a project that exists and has something in it.
OTHER_PROJECT = "Other-Project"
SLUG = "self-test"
ADMIN_USERNAME = "isolated-test"
ADMIN_PASSWORD = "isolated-admin-self-test"
# 13.0 §8 removed UPGRADE_OLD_VERSION and the old/new/old upgrade rehearsal it
# named: a version change carries no state across, so there is nothing to
# rehearse. The product assertions and `write_config` below are unchanged.
#
# The app container's own MCP port. Live mode talks to this from INSIDE the
# container, so it is the container's port and not a published host port.
INTERNAL_MCP_PORT = 8675


_MULTILINE_ARGV_HTTP_PROBE = r'''import base64, json, sys, uuid, httpx
url = sys.argv[1]
token = sys.stdin.readline().rstrip("\n")
if not token:
    raise SystemExit("multiline argv HTTP check received no token")
client = httpx.Client(timeout=60, headers={
    "Authorization": "Bearer " + token,
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
})
session = None
sequence = 0
def post(message):
    global session
    headers = {"mcp-session-id": session} if session else {}
    response = client.post(url, json=message, headers=headers)
    session = response.headers.get("mcp-session-id", session)
    body = response.text
    if response.headers.get("content-type", "").startswith("text/event-stream"):
        frames = [line[5:].strip() for line in body.splitlines() if line.startswith("data:")]
        body = frames[-1] if frames else ""
    response.raise_for_status()
    return json.loads(body) if body else {}
def invoke(name, arguments, *, progress_token=None):
    global sequence
    sequence += 1
    params = {"name": name, "arguments": arguments}
    if progress_token is not None:
        params["_meta"] = {"progressToken": progress_token}
    body = post({"jsonrpc": "2.0", "id": sequence, "method": "tools/call", "params": params})
    if "error" in body:
        raise AssertionError(name + " returned a JSON-RPC error")
    content = body.get("result", {}).get("content", [])
    text = next((item.get("text") for item in content
                 if item.get("type") == "text" and isinstance(item.get("text"), str)), None)
    if text is None:
        raise AssertionError(name + " returned no text payload")
    return json.loads(text)
def call(name, arguments, *, progress_token=None):
    payload = invoke(name, arguments, progress_token=progress_token)
    if payload.get("status") != "success":
        raise AssertionError(name + " failed: " + str(payload.get("reason", "unknown")))
    if isinstance(payload.get("job"), dict):
        return payload["job"]
    # Native MCP text payloads carry document fields at the top level.
    # Only Workspace job results have a nested job record to unwrap.
    return payload
post({"jsonrpc": "2.0", "id": 0, "method": "initialize",
      "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                 "clientInfo": {"name": "cognita-multiline-argv-check", "version": "1"}}})
post({"jsonrpc": "2.0", "method": "notifications/initialized"})
run = uuid.uuid4().hex
source = "print('line1')\nprint('line2')"
argv = ["python3", "-c", source]
invalid = invoke("workspace_start_job", {
    "argv": ["python3", "\x00"], "cwd": "/workspace", "timeout": 30, "env": {},
    "idempotency_key": "http-multiline-" + run + "-nul",
})
if invalid.get("status") != "error" or "job" in invalid or "job_id" in invalid:
    raise AssertionError("NUL argv was accepted or created a job")
def start(suffix):
    return call("workspace_start_job", {
        "argv": argv, "cwd": "/workspace", "timeout": 30, "env": {},
        "wait_ms": 10000, "output_encoding": "text",
        "idempotency_key": "http-multiline-" + run + "-" + suffix,
    })
first = start("first")
second = start("second")
expected = "line1\nline2\n"
for label, job in (("first", first), ("second", second)):
    if not isinstance(job.get("job_id"), str) or job.get("state") != "succeeded":
        raise AssertionError(label + " multiline argv job did not succeed")
    if job.get("stdout") != expected:
        raise AssertionError(label + " multiline argv stdout mismatch: " + repr(job.get("stdout")))
    fetched = call("workspace_get_job", {"job_id": job["job_id"]})
    if fetched.get("state") != "succeeded":
        raise AssertionError(label + " repeated-job get did not return terminal success")
    if base64.b64decode(fetched.get("stdout", ""), validate=True) != expected.encode():
        raise AssertionError(label + " repeated-job get stdout mismatch")
if first["job_id"] == second["job_id"]:
    raise AssertionError("distinct idempotency keys returned the same job")
replay = start("second")
if replay.get("job_id") != second["job_id"]:
    raise AssertionError("same-key replay did not return its original job")
canceled = call("workspace_cancel_job", {"job_id": first["job_id"]})
if canceled.get("state") != "succeeded":
    raise AssertionError("cancel of a completed job lost its broker result")
filepath = "cognita-selftest-operation-replay-" + run + ".md"
operation_id = "http-operation-replay-" + run
document = "Synthetic HTTP idempotency fixture " + run + "\n"
try:
    call("add_document", {"project": "Self-Test", "filepath": filepath,
                           "content": document, "category": "general"})
    before = call("list_backups", {"project": "Self-Test", "filepath": filepath})
    if before.get("backups") != []:
        raise AssertionError("new idempotency fixture already has backups")
    arguments = {"project": "Self-Test", "filepath": filepath,
                 "delete_file": True, "operation_id": operation_id}
    removed = call("remove_document", arguments, progress_token="initial-progress-token")
    backup_id = removed.get("previous_backup_id")
    if not isinstance(backup_id, str) or not backup_id:
        raise AssertionError("remove_document did not return its backup receipt")
    after_first = call("list_backups", {"project": "Self-Test", "filepath": filepath})
    if [item.get("backup_id") for item in after_first.get("backups", [])] != [backup_id]:
        raise AssertionError("first remove_document did not create exactly one backup")
    replay = call("remove_document", arguments, progress_token="retry-progress-token")
    if replay.get("replayed") is not True or replay.get("previous_backup_id") != backup_id:
        raise AssertionError("metadata-only retry did not replay the original backup receipt")
    after_replay = call("list_backups", {"project": "Self-Test", "filepath": filepath})
    if [item.get("backup_id") for item in after_replay.get("backups", [])] != [backup_id]:
        raise AssertionError("remove_document replay created a duplicate backup")
    changed = invoke("remove_document", {
        **arguments, "delete_file": False,
    }, progress_token="changed-logical-arguments")
    if changed.get("status") != "error" or changed.get("reason") != "operation_conflict":
        raise AssertionError("changed remove_document arguments did not conflict")
    absent = invoke("read_document", {"project": "Self-Test", "filepath": filepath})
    if absent.get("status") != "error" or absent.get("reason") != "not_found":
        raise AssertionError("remove_document retry left its source fixture present")
finally:
    cleanup = invoke("remove_document", {
        "project": "Self-Test", "filepath": filepath, "delete_file": True,
        "operation_id": operation_id + "-cleanup",
    })
    if cleanup.get("status") not in {"success", "error"} or (
        cleanup.get("status") == "error" and cleanup.get("reason") != "not_found"
    ):
        raise AssertionError("could not remove the owned idempotency fixture")
client.close()
print("PASS canonical HTTP multiline argv, repeated jobs, and metadata-independent delete replay")
'''




class RunnerError(RuntimeError):
    """A setup, lifecycle, or transport check failed."""




def private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=False)
    if os.name == "posix":
        path.chmod(0o700)


def private_file(path: Path, content: str | bytes) -> None:
    data = content.encode("utf-8") if isinstance(content, str) else content
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as stream:
            fd = -1
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        if fd >= 0:
            os.close(fd)
    if os.name == "posix":
        path.chmod(0o600)




def kvm_gid() -> int:
    try:
        return int(Path("/dev/kvm").stat().st_gid)
    except (OSError, ValueError) as exc:
        raise RunnerError("KEI does not expose /dev/kvm; Workspace runtime cannot be tested") from exc


def run_command(command: list[str], cwd: Path, log_path: Path, timeout: float) -> int:
    """Run one bounded owned process and append its output to a task log."""
    with log_path.open("ab") as stream:
        stream.write(("$ " + " ".join(command) + "\n").encode("utf-8"))
        stream.flush()
        process = subprocess.Popen(command, cwd=cwd, stdout=stream, stderr=subprocess.STDOUT)
        try:
            return process.wait(timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            process.kill()
            process.wait(timeout=30)
            raise RunnerError(f"command timed out after {timeout:.0f}s; see {log_path}") from exc




class Installation(NamedTuple):
    """What a generated installation hands back to whoever started it.

    The PostgreSQL password is here because the throwaway stack's test runner
    needs the in-network DSN (`postgresql://cognita:<pw>@postgres:5432/cognita`)
    and the secret file it is written to is only readable inside a container.
    """

    api_key: str
    env_path: Path
    postgres_password: str


def _self_signed_admin_tls(secrets_root: Path) -> None:
    """Generate the Admin TLS pair Compose mounts as secrets.

    Empty files were enough while nothing served TLS, but a generated
    installation should be a real one.  openssl is on kei; if it is ever
    missing the stack still starts with the empty files it used before, and
    this says which of the two happened rather than failing silently.
    """
    cert = secrets_root / "admin_tls_certfile"
    key = secrets_root / "admin_tls_keyfile"
    if shutil.which("openssl"):
        result = subprocess.run(
            ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "2",
             "-subj", "/CN=cognita-test", "-keyout", str(key), "-out", str(cert)],
            capture_output=True, text=True, check=False, timeout=120,
        )
        if result.returncode == 0:
            if os.name == "posix":
                cert.chmod(0o600)
                key.chmod(0o600)
            print("installation: generated a self-signed Admin TLS pair")
            return
        print(f"installation: openssl failed ({result.returncode}); using empty TLS secret files")
    else:
        print("installation: openssl is not on PATH; using empty TLS secret files")
    private_file(cert, b"")
    private_file(key, b"")


def _device_gid(pattern: str, fallback: int) -> int:
    """Group ID of a real device node, never of /dev/dri itself.

    That directory is root:root on kei, so copying its gid would add group 0
    to the container.  kei's cards are card1..card3, not card0, which is why
    this matches a pattern instead of a fixed name.
    """
    nodes = sorted(Path("/dev/dri").glob(pattern)) if Path("/dev/dri").is_dir() else []
    return nodes[0].stat().st_gid if nodes else fallback


def write_config(run: Path, mcp_port: int, admin_port: int, version: str,
                 *, release_target: str = "test", gpu: bool = False,
                 models_root: Path | None = None, mode: str = "full") -> Installation:
    """Generate a complete synthetic installation under one root.

    This is the ONE generator for a throwaway Cognita: the isolated HTTP
    self-test starts from it, and so does `scripts/release.py` (its `test`
    stack and the `test` deployment target).  13.0 §7.1 requires everything
    compose.yaml needs to be here -- five secrets, the capacity marker, a
    minimal config set and an env file -- plus a SECOND populated project that
    the self-test principal must never see, because an isolation check against
    an empty project proves nothing.
    """
    if mode not in {"core", "full"}:
        raise ValueError("mode must be core or full")
    config = run / "config"
    secrets_root = run / "secrets"
    projects_root = run / "projects"
    postgres_root = run / "postgres"
    workspaces_root = run / "workspaces"
    transfers_root = run / "transfers"
    # 13.0 §7.1 step 3: the model cache is the TARGET's, bind-mounted
    # read-write, not a fresh directory per run.  A private one would make
    # every throwaway stack re-download the embedding and reranker models and
    # recompile its MIGraphX programs, and would make a test run need egress
    # on a box that already has all of it on disk.  Caller passes the root it
    # read out of the target's env file; a run with nowhere to point falls
    # back to its own directory and pays that price honestly.
    models_root = Path(models_root) if models_root is not None else run / "models"
    # The toolbox cache root is named in the env file but deliberately NOT
    # created here: whoever provisions the archive creates it (the self-test's
    # provision_toolbox_cache, release.py's load_toolbox), and private_dir
    # refuses a directory that already exists.
    toolbox_root = workspaces_root / "toolbox-cache"
    roots = [config, secrets_root, projects_root, postgres_root, transfers_root]
    if mode == "full":
        roots.append(workspaces_root)
    for path in roots:
        private_dir(path)
    # The model cache may be a shared target root that already exists and is
    # not this run's to own, so it is created only when it is this run's own.
    if not models_root.exists():
        private_dir(models_root)
    private_dir(config / "data")
    for project in (PROJECT, OTHER_PROJECT):
        private_dir(projects_root / project)
        private_dir(config / "data" / project)
    # The second project has a document in it on purpose: the test-mode
    # principal is supposed to be unable to see this file, and a check against
    # an empty project would pass whether or not the scope is enforced.
    (projects_root / OTHER_PROJECT / "other-project-note.md").write_text(
        "# Other project\n\nA document the self-test principal must never see.\n", encoding="utf-8")

    root_id = str(uuid.uuid4())
    if mode == "full":
        (workspaces_root / ".cognita-12-workspaces.json").write_text(
            json.dumps({"schema": 1, "root_id": root_id, "role": "workspaces"}) + "\n", encoding="utf-8"
        )
    (transfers_root / ".cognita-12-transfer-root.json").write_text(
        json.dumps({"schema": 1, "root_id": root_id, "marker_owned": True}) + "\n", encoding="utf-8"
    )
    if os.name == "posix":
        if mode == "full":
            (workspaces_root / ".cognita-12-workspaces.json").chmod(0o600)
        (transfers_root / ".cognita-12-transfer-root.json").chmod(0o600)

    # Keep the raw key in memory and the child environment only. The policy
    # file stores the digest accepted by Cognita, never the raw value.
    api_key = "cog_sk_v1_" + secrets.token_urlsafe(32)
    digest = hashlib.sha256(api_key.encode("utf-8")).hexdigest()
    created = datetime.now(UTC).isoformat(timespec="seconds")
    # One ordinary key, accepted on both projects: the synthetic client is an
    # ordinary principal and must be able to reach the project the test-mode
    # principal cannot, or the isolation check has nothing to compare against.
    policy = ["version: 1", "revision: 0", "global:", "  oauth_enabled: false", "projects:"]
    registry = ["version: 1", "projects:"]
    for project in (PROJECT, OTHER_PROJECT):
        policy += [
            f"  {project}:",
            "    oauth_mode: disabled",
            "    static_key:",
            "      algorithm: sha256",
            f"      digest: {digest}",
            f"      key_id: '{digest[:12]}'",
            f"      created_at: '{created}'",
        ]
        registry += [
            f"  - name: {project}",
            f"    documents_dir: {projects_root / project}",
            f"    data_dir: /app/config/data/{project}",
            "    enabled: true",
            "    writable: true",
        ]
    private_file(config / "authentication.yaml", "\n".join(policy) + "\n")
    private_file(config / "registry.yaml", "\n".join(registry) + "\n")
    private_file(config / "connectors.yaml", f"""version: 1
revision: 0
connectors:
  - id: {uuid.uuid4()}
    name: {PROJECT}
    slug: {SLUG}
    enabled: true
    project_mode: selected
    default_access: null
    project_access:
      {PROJECT}: write
      {OTHER_PROJECT}: write
    workspace_enabled: {str(mode == "full").lower()}
    default_workspace_transfer: allow
""")
    if mode == "full":
        private_file(config / "workspace-connectors.yaml", "version: 1\nrevision: 0\nworkspace_connectors: []\n")
    # On an amd target the throwaway stack embeds on the real cards, so the live
    # self-test is the real-device coverage 13.0 §7.1 relies on; OCR stays on
    # the CPU to keep the throwaway light. `gpu_device_ids: []` means every
    # card the service can see.
    knowledge_gpu = "true" if gpu else "false"
    private_file(config / "acceleration.yaml",
                 f"schema: 1\nrevision: 0\nknowledge:\n  gpu_enabled: {knowledge_gpu}\n"
                 "  gpu_device_ids: []\nocr:\n  device: cpu\n  gpu_device_ids: []\n")

    _self_signed_admin_tls(secrets_root)
    postgres_password = secrets.token_urlsafe(32)
    private_file(secrets_root / "postgres.password", postgres_password)
    private_file(secrets_root / "postgres.dsn", f"postgresql://cognita:{postgres_password}@postgres:5432/cognita")
    if mode == "full":
        private_file(secrets_root / "broker.secret", secrets.token_urlsafe(48))
    admin_digest = hashlib.sha256(ADMIN_PASSWORD.encode("utf-8")).hexdigest()
    private_file(config / "cognita.yaml", f"""mcp_host: 0.0.0.0
mcp_port: 8675
admin_host: 127.0.0.1
admin_port: 8676
admin_username: {ADMIN_USERNAME}
admin_password_hash: ''
admin_password_sha256: {admin_digest}
data_root: /app/config/data
engine: core
pg_dsn: postgresql:///cognita
oauth_enabled: false
public_base_url: http://127.0.0.1:{mcp_port}
registry_path: /app/config/registry.yaml
connectors_path: /app/config/connectors.yaml
authentication_path: /app/config/authentication.yaml
acceleration_path: /app/config/acceleration.yaml
models_cache_dir: /var/lib/cognita/models
watch_enabled: false
log_level: INFO
log_dir: /app/config/logs
""")
    env = f"""COGNITA_VERSION={version}
COGNITA_RELEASE_TARGET={release_target}
COGNITA_CONFIG_ROOT={config}
COGNITA_PROJECTS_ROOT={projects_root}
COGNITA_POSTGRES_DATA_ROOT={postgres_root}
COGNITA_TRANSFER_STAGING_ROOT={transfers_root}
COGNITA_SECRETS_ROOT={secrets_root}
COGNITA_MODEL_CACHE_ROOT={models_root}
COGNITA_SERVICE_UID={os.getuid()}
COGNITA_SERVICE_GID={os.getgid()}
COGNITA_MCP_HOST_PORT={mcp_port}
COGNITA_MCP_BIND_ADDRESS=127.0.0.1
COGNITA_ADMIN_HOST_PORT={admin_port}
COGNITA_ADMIN_BIND_ADDRESS=127.0.0.1
"""
    if mode == "full":
        env += (f"COGNITA_WORKSPACE_DATA_ROOT={workspaces_root}\n"
                f"COGNITA_TOOLBOX_IMAGE_CACHE_ROOT={toolbox_root}\n"
                f"COGNITA_KVM_GID={kvm_gid()}\n"
                f"COGNITA_VIDEO_GID={_device_gid('card*', 44)}\n"
                f"COGNITA_RENDER_GID={_device_gid('renderD*', 109)}\n")
    env_path = run / "compose.env"
    private_file(env_path, env)
    return Installation(api_key=api_key, env_path=env_path, postgres_password=postgres_password)










def run_selftest(
    repo: Path, compose: list[str], key: str, run: Path,
    *, slug: str = SLUG, mcp_port: int = INTERNAL_MCP_PORT, mode: str = "full",
    output: Path | None = None, capture_failure_logs: bool = False,
) -> int:
    """Run the public self-test runner INSIDE the app container.

    The connector slug and port are parameters because 13.0's live mode
    (§7.3) points the same procedure at the real connector on a running
    target; the isolated stack keeps the synthetic `self-test` defaults.
    """
    output = output or (run / "selftest.log")
    # The host may have Docker but not Cognita's exact release dependencies.
    # Copy the runner into the candidate image so imports resolve from the same
    # installed package in that image, then pass the synthetic key through
    # stdin into a child environment instead of exposing it in argv or logs.
    if run_command(compose + ["cp", str(repo / "scripts" / "run-selftest.py"), "cognita:/tmp/cognita-run-selftest.py"], repo, output, 60):
        raise RunnerError(f"could not copy self-test runner into candidate; see {output}")
    internal_url = (
        f"http://127.0.0.1:{mcp_port}/mcp/connectors/{slug}/mcp/v{PUBLIC_CONTRACT_VERSION}"
    )
    command = compose + [
        "exec", "-T", "-e", f"COGNITA_TEST_PROJECT={PROJECT}", "cognita", "sh", "-c",
        "read -r COGNITA_TEST_API_KEY; export COGNITA_TEST_API_KEY; "
        f"exec python /tmp/cognita-run-selftest.py --provision-ocr-fixtures --mode {mode} {internal_url}",
    ]
    with output.open("wb") as stream:
        stream.write((f"$ docker compose exec cognita run-selftest.py --provision-ocr-fixtures --mode {mode} {internal_url}\n").encode())
        stream.flush()
        process = subprocess.Popen(command, cwd=repo, stdin=subprocess.PIPE, stdout=stream, stderr=subprocess.STDOUT)
        result_code = 1
        try:
            _stdout, _stderr = process.communicate((key + "\n").encode("utf-8"), timeout=1800)
            result_code = process.returncode
            # The multiline-argv check starts Workspace jobs, so it belongs to
            # full mode only.  A core install has no Workspace runtime and it
            # failed there with runtime_unavailable after the core runner had
            # passed 61/61 (installer proof VM, 2026-09-28); kei's targets are
            # all full, which is why it never showed before.
            if result_code == 0 and mode == "full":
                result_code = run_multiline_argv_http_check(
                    repo, compose, key, internal_url, output,
                )
            return result_code
        except subprocess.TimeoutExpired as exc:
            process.kill()
            process.communicate(timeout=30)
            raise RunnerError(f"self-test timed out; see {output}") from exc
        finally:
            if capture_failure_logs and result_code:
                # Canonical disposable stacks vanish immediately after this
                # call. Preserve bounded app diagnostics before teardown; the
                # existing app formatter redacts credentials and payloads.
                # Live deployments do not opt in to this stack-wide capture.
                try:
                    run_command(compose + ["logs", "--no-color", "--tail", "120", "cognita"],
                                repo, output, 60)
                except (OSError, RunnerError, subprocess.SubprocessError) as exc:
                    with output.open("ab") as diagnostic:
                        diagnostic.write(f"app failure log capture failed type={type(exc).__name__}\n".encode())
            run_command(compose + ["exec", "-T", "cognita", "rm", "-f", "/tmp/cognita-run-selftest.py"], repo, output, 60)


def run_multiline_argv_http_check(
    repo: Path, compose: list[str], key: str, url: str, output: Path,
) -> int:
    """Prove multiline argv and retained terminal job results over public MCP HTTP."""
    command = compose + [
        "exec", "-T", "-e", f"COGNITA_TEST_PROJECT={PROJECT}", "cognita",
        "python", "-c", _MULTILINE_ARGV_HTTP_PROBE, url,
    ]
    with output.open("ab") as stream:
        stream.write(b"$ docker compose exec cognita canonical HTTP multiline argv and job-retention check\n")
        stream.flush()
        process = subprocess.Popen(command, cwd=repo, stdin=subprocess.PIPE,
                                   stdout=stream, stderr=subprocess.STDOUT)
        try:
            process.communicate((key + "\n").encode("utf-8"), timeout=180)
            return process.returncode
        except subprocess.TimeoutExpired as exc:
            process.kill()
            process.communicate(timeout=30)
            raise RunnerError(f"multiline argv HTTP check timed out; see {output}") from exc






# 13.0 §8: `_upgrade_marker`, `_verify_upgrade_workspace`,
# `_assert_runtime_stopped` and `run_failed_workspace_upgrade` (the
# old/new/old rehearsal and its alternative stack builder) are DELETED.
# A version change carries no disposable state across, so there is no
# transition to rehearse and no previous state to check.




def tail(path: Path, count: int = 80) -> str:
    if not path.is_file():
        return ""
    return "".join(path.read_text(encoding="utf-8", errors="replace").splitlines(True)[-count:])








# 13.0 §8: `_admin_json` and `run_failed_record_client_phase` (the
# failed-before/failed-after transition phases) are DELETED with the
# upgrade rehearsal they served.






def _add_live_arguments(parser: argparse.ArgumentParser) -> None:
    """Declare the `live` subcommand's arguments (13.0 §7.3).

    `live` is deliberately the ONLY mode that touches a stack it did not
    create: it builds nothing, writes no configuration, reads no secret, and
    starts and stops nothing. It is handed the coordinates of a stack that is
    already up and runs the product assertions against it.
    """
    parser.add_argument(
        "action", nargs="?", choices=("live",),
        help="`live`: run the self-test against an ALREADY RUNNING stack",
    )
    parser.add_argument("--compose-project", help="[live] Compose project name of the running stack")
    parser.add_argument("--env-file", type=Path, help="[live] the env file that stack was started with")
    parser.add_argument(
        "--compose-file", type=Path, action="append", default=None, dest="compose_files",
        help="[live] a Compose file of the running stack; repeat, in the unit's order",
    )
    parser.add_argument(
        "--mcp-port", type=int, default=INTERNAL_MCP_PORT,
        help=f"[live] the app container's OWN MCP port (default {INTERNAL_MCP_PORT})",
    )
    parser.add_argument(
        "--connector", default=SLUG,
        help=f"[live] connector slug serving the {PROJECT} project (default {SLUG!r})",
    )
    parser.add_argument(
        "--mode", choices=("core", "full"), required=True, dest="host_mode",
        help="[live] host Workspace capability being qualified",
    )
    parser.add_argument(
        "--log-dir", type=Path, default=None,
        help="[live] directory for the self-test log (default: a temp directory)",
    )


def run_live(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    """Run the public self-test against a running stack, from inside its app container.

    Why inside: the host has Docker but not Cognita's dependencies (no httpx,
    no installed `cognita`), which is exactly why `run_selftest` has always
    exec'd into the container. Live mode reuses that procedure against the
    real connector and the container's own MCP port. The key arrives on stdin
    and never appears in argv, in a log, or in the environment of anything but
    the child shell that consumes it.

    The runner executes only the checks selected by `--mode`: full includes
    caller-callable Workspace/bridge sections, while core verifies their
    omission and bounded stale-call behavior.
    """
    missing = [
        name for name, value in (
            ("--compose-project", args.compose_project),
            ("--env-file", args.env_file),
            ("--compose-file", args.compose_files),
        ) if not value
    ]
    if missing:
        parser.error(f"live mode requires {', '.join(missing)}")
    if not 1 <= int(args.mcp_port) <= 65535:
        parser.error("--mcp-port must be a TCP port")
    if not args.env_file.is_file():
        raise RunnerError(f"--env-file is not a file: {args.env_file}")
    for path in args.compose_files:
        if not path.is_file():
            raise RunnerError(f"--compose-file is not a file: {path}")
    key = sys.stdin.read(4096).strip()
    if not key:
        raise RunnerError("live mode reads the API key from stdin; none was supplied")
    repo = Path(__file__).resolve().parents[1]
    log_dir = args.log_dir or Path(tempfile.mkdtemp(prefix="cognita-live-selftest-"))
    log_dir.mkdir(parents=True, exist_ok=True)
    output = log_dir / f"live-selftest-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}.log"
    compose = ["docker", "compose", "-p", str(args.compose_project),
               "--env-file", str(args.env_file)]
    for path in args.compose_files:
        compose += ["-f", str(path)]
    print(
        f"live self-test: project={args.compose_project} connector={args.connector} "
        f"container_mcp_port={args.mcp_port} log={output}"
    )
    try:
        code = run_selftest(
            repo, compose, key, log_dir,
            slug=str(args.connector), mcp_port=int(args.mcp_port), mode=args.host_mode, output=output,
            capture_failure_logs=str(args.compose_project).startswith("cognita-test-"),
        )
    except (OSError, subprocess.SubprocessError, RunnerError) as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        print(f"log: {output}", file=sys.stderr)
        if output.exists():
            print(tail(output), file=sys.stderr)
        return 1
    finally:
        key = ""
    if code:
        print(f"FAIL: live self-test exited {code}; see {output}", file=sys.stderr)
        print(_failure_summary(output), file=sys.stderr)
        return 1
    receipt = None
    for line in _scorecard_lines(output):
        if line.startswith("selftest_receipt="):
            try:
                receipt = json.loads(line.partition("=")[2])
            except ValueError:
                receipt = None
    if (not isinstance(receipt, dict) or receipt.get("schema") != 1
            or receipt.get("mode") != args.host_mode or receipt.get("result") != "passed"
            or receipt.get("mandatory_ocr") != "passed" or receipt.get("missing_file_parity") != "passed"):
        print(f"FAIL: missing or failed mandatory HTTP/OCR receipt; see {output}", file=sys.stderr)
        return 1
    receipt["canonical_log"] = str(output.resolve())
    print("selftest_receipt=" + json.dumps(receipt, sort_keys=True))
    coverage = "Knowledge + Workspace/bridge sections" if args.host_mode == "full" else "Knowledge + core negatives"
    print(f"live self-test=PASS ({coverage}) log={output}")
    print(_scorecard_total(output))
    return 0


def _scorecard_lines(output: Path) -> list[str]:
    try:
        return output.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as exc:
        return [f"(self-test log unreadable: {exc})"]


def _scorecard_total(output: Path) -> str:
    for line in reversed(_scorecard_lines(output)):
        if "steps passed" in line:
            return line.strip()
    return "(no scorecard total in the self-test log)"


def _failure_summary(output: Path) -> str:
    """Return the failing scorecard lines, so a failure is readable without the log."""
    failures = [line for line in _scorecard_lines(output) if line.strip().startswith("FAIL")]
    total = _scorecard_total(output)
    if not failures:
        return f"{total}\n{tail(output)}"
    return "\n".join([total, *failures[:40]])


def main(argv: list[str] | None = None) -> int:
    """Live mode only.

    13.0 §8: the isolated stack builder that used to live here is deleted.
    It pinned 12.17.0 image tags, built its own Compose stack and owned its own
    synthetic installation -- all three of which `scripts/release.py test` now
    does, from the same `write_config` below, against images it has just built.
    Two stack builders meant the one nobody ran was the one that rotted.

    What went with it, honestly: the Admin-only W3 stop/restart persistence
    gate and the W10 two-principal isolation phase had no runner other than
    that builder, and the live plan (`run_workspace_plan`) covers neither.
    W3 is a release check someone must drive by hand or rebuild on top of
    `release.py test`; W10's scope question is partly answered by the
    test-mode principal tests, which prove one principal sees only
    `Self-Test`, but not by two ordinary principals against each other.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    _add_live_arguments(parser)
    args = parser.parse_args(argv)
    if args.action != "live":
        parser.error("live is the only mode: pass `live` with --compose-project, --env-file "
                     "and --compose-file (see scripts/release.py, which calls this)")
    return run_live(args, parser)


if __name__ == "__main__":
    raise SystemExit(main())
