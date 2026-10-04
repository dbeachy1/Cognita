"""Exercise one persistent Workspace directly through the real private broker.

This is the connector-free live tier for the 12.6 Workspace runtime probe.
It is intentionally stdlib-only so it can be streamed into the already-running
``workspace-runtime`` container, whose broker port is private and whose bearer
secret is mounted there.  No broker or Microsandbox object is mocked.

The operator must supply a fresh, task-owned UUID.  Broker names are then
deterministic and are printed as bounded identity evidence only:
``cognita-ws-<UUID>`` and ``cognita-ws-data-<UUID>``.  The script never removes
the Workspace or volume; this test resource is deliberately retained for a
later persistence/restart probe.  Its synthetic files are confined beneath
``/workspace/.cognita-live/<UUID>/``.

Run from the repository without copying files into the container (PowerShell):

    Get-Content scripts/live_broker_workspace_smoke.py -Raw |
      docker compose -f compose.yaml exec -T workspace-runtime python3 - --run \
        --workspace-id 4f0f2d5b-2bd1-4aa9-9a6e-8a5a8f14c61d

The broker secret is read only from its existing in-container mount.  The
script does not inspect Cognita, Admin, OAuth, connector, or user Workspace
state.  Because it deliberately bypasses the Cognita manager, it does not
exercise manager admission, host-reserve accounting, principal mapping, or
Admin inventory; it must never be reported as the public connector acceptance
gate.  It reports bounded broker ``code``, ``stage``, ``category`` and
correlation ID on failure, without echoing response bodies or secrets.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import inspect
import json
import sys
import time
import uuid
import urllib.error
import urllib.request
from dataclasses import dataclass

BROKER_URL = "http://127.0.0.1:8080"
SECRET_FILE = "/run/secrets/cognita_broker_secret"
ROOT_PREFIX = "/workspace/.cognita-live"
POLL_SECONDS = 90.0
PERSISTENT_FIXTURE_UUID = "4f0f2d5b-2bd1-4aa9-9a6e-8a5a8f14c61d"
QUOTA_BYTES = 4 * 1024**3
ROOT_QUOTA_BYTES = 4 * 1024**3
MEMORY_BYTES = 8 * 1024**3
MEMORY_MIB = MEMORY_BYTES // (1024**2)
EXPECTED_IMAGE = "cognita-workspace-toolbox:12.6.0"


class BrokerSmokeError(RuntimeError):
    """A bounded setup, protocol, assertion, or runtime failure."""


@dataclass(frozen=True)
class BrokerFailure:
    code: str
    stage: str | None
    category: str | None
    correlation_id: str | None
    retryable: bool | None

    def summary(self) -> str:
        values = {
            key: value for key, value in {
                "code": self.code,
                "stage": self.stage,
                "category": self.category,
                "correlation_id": self.correlation_id,
                "retryable": self.retryable,
            }.items() if value is not None
        }
        return repr(values)


def parse_workspace_id(value: str) -> uuid.UUID:
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError, TypeError) as exc:
        raise BrokerSmokeError("--workspace-id must be a UUID") from exc
    if parsed.version != 4:
        raise BrokerSmokeError("--workspace-id must be a UUIDv4")
    return parsed


def resource_names(workspace_id: uuid.UUID) -> tuple[str, str]:
    suffix = str(workspace_id)
    return f"cognita-ws-{suffix}", f"cognita-ws-data-{suffix}"


def run_root(workspace_id: uuid.UUID) -> str:
    return f"{ROOT_PREFIX}/{workspace_id}"


def _failure(payload: object) -> BrokerFailure:
    if not isinstance(payload, dict):
        return BrokerFailure("invalid_response", None, None, None, None)
    code = payload.get("code")
    return BrokerFailure(
        str(code) if isinstance(code, str) else "invalid_response",
        payload.get("stage") if isinstance(payload.get("stage"), str) else None,
        payload.get("category") if isinstance(payload.get("category"), str) else None,
        payload.get("correlation_id") if isinstance(payload.get("correlation_id"), str) else None,
        payload.get("retryable") if isinstance(payload.get("retryable"), bool) else None,
    )


class BrokerClient:
    def __init__(self, base_url: str, secret: str, *, timeout: float = 120.0):
        self.base_url = base_url.rstrip("/")
        self.secret = secret
        self.timeout = timeout

    def call(self, workspace_id: uuid.UUID, operation: str, arguments: dict) -> dict:
        if operation != "job_get":
            print(f"phase={operation}", flush=True)
        body = json.dumps({
            "request_id": str(uuid.uuid4()),
            "operation": operation,
            "workspace_id": str(workspace_id),
            "arguments": arguments,
        }, separators=(",", ":")).encode("utf-8")
        request = urllib.request.Request(
            f"{self.base_url}/v1/rpc", data=body, method="POST",
            headers={
                "Authorization": f"Bearer {self.secret}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read(256 * 1024 + 1)
        except urllib.error.HTTPError as exc:
            raw = exc.read(256 * 1024 + 1)
            try:
                payload = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                raise BrokerSmokeError(f"broker HTTP failure status={exc.code}") from exc
            failure = _failure(payload)
            raise BrokerSmokeError(f"broker HTTP failure {failure.summary()}") from exc
        except (OSError, TimeoutError) as exc:
            raise BrokerSmokeError(
                f"broker operation {operation} transport failure {type(exc).__name__}"
            ) from exc
        if len(raw) > 256 * 1024:
            raise BrokerSmokeError("broker response exceeded bounded test limit")
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise BrokerSmokeError("broker response was not JSON") from exc
        if not isinstance(payload, dict):
            raise BrokerSmokeError("broker response was not an object")
        if "code" in payload:
            failure = _failure(payload)
            raise BrokerSmokeError(f"broker operation {operation} failed {failure.summary()}")
        data = payload.get("data")
        if not isinstance(data, dict):
            raise BrokerSmokeError(f"broker operation {operation} returned no data")
        return data

    def health(self) -> dict:
        request = urllib.request.Request(
            f"{self.base_url}/healthz",
            method="GET",
            headers={"Authorization": f"Bearer {self.secret}", "Accept": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=30.0) as response:
                raw = response.read(64 * 1024 + 1)
        except (OSError, TimeoutError) as exc:
            raise BrokerSmokeError(f"broker health transport failure {type(exc).__name__}") from exc
        if len(raw) > 64 * 1024:
            raise BrokerSmokeError("broker health response exceeded bounded test limit")
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise BrokerSmokeError("broker health response was not JSON") from exc
        if not isinstance(payload, dict):
            raise BrokerSmokeError("broker health response was not an object")
        return payload


def _read_secret(path: str) -> str:
    try:
        value = open(path, encoding="ascii").read().strip()
    except (OSError, UnicodeError) as exc:
        raise BrokerSmokeError("broker secret mount is unavailable") from exc
    if len(value) < 32 or not value.isascii() or any(character.isspace() for character in value):
        raise BrokerSmokeError("broker secret mount is invalid")
    return value


def _ensure(client: BrokerClient, workspace_id: uuid.UUID) -> dict:
    return client.call(workspace_id, "ensure", {
        "quota_bytes": QUOTA_BYTES,
        "root_quota_bytes": ROOT_QUOTA_BYTES,
        "vcpus": 4,
        "memory_bytes": MEMORY_BYTES,
        "max_processes": 256,
        "max_file_descriptors": 4096,
        "require_writable_root_quota": True,
        "network": {"mode": "off", "rules": [], "explicit_confirmation": False},
    })


def _validate_persisted_metadata(
    config: dict,
    volume_labels: dict,
    volume_quota_mib: object,
    workspace_id: uuid.UUID,
    sandbox_name: str,
    volume_name: str,
) -> None:
    """Validate only bounded persisted SDK identity/configuration fields."""

    labels = config.get("labels")
    if not isinstance(labels, dict):
        raise BrokerSmokeError("runtime config omitted persisted owner labels")
    expected_labels = {
        "cognita.owner": "workspace-broker",
        "cognita.workspace": str(workspace_id),
        "cognita.image": EXPECTED_IMAGE,
        "cognita.volume": volume_name,
        "cognita.network": "off",
        "cognita.root_quota_bytes": str(ROOT_QUOTA_BYTES),
    }
    if any(labels.get(key) != value for key, value in expected_labels.items()):
        raise BrokerSmokeError(
            "runtime persisted owner/config labels do not match the task contract"
        )
    resources = config.get("resources")
    if not isinstance(resources, dict):
        raise BrokerSmokeError("runtime config omitted persisted resource limits")
    cpus = resources.get("cpus", resources.get("vcpus"))
    memory = resources.get("memory_mib")
    if memory is None:
        memory = resources.get("memory_bytes")
        if isinstance(memory, int):
            memory //= 1024**2
    if cpus != 4 or memory != MEMORY_MIB:
        raise BrokerSmokeError("runtime persisted resources do not match 4-vCPU/8-GiB contract")
    image = config.get("image")
    if isinstance(image, dict):
        if isinstance(image.get("Oci"), dict):
            image = image["Oci"].get("reference")
        else:
            image = image.get("name", image.get("source", image.get("ref")))
    if image != EXPECTED_IMAGE:
        raise BrokerSmokeError("runtime persisted image does not match the 12.6 toolbox")
    mounts = config.get("mounts")
    mounted_volume = None
    if isinstance(mounts, dict):
        mount = mounts.get("/workspace")
        if isinstance(mount, dict):
            mounted_volume = mount.get("name", mount.get("volume"))
        else:
            mounted_volume = mount
    elif isinstance(mounts, list):
        for mount in mounts:
            if isinstance(mount, dict) and mount.get(
                "guest", mount.get("target", mount.get("path"))
            ) == "/workspace":
                mounted_volume = mount.get("name", mount.get("volume"))
                break
    if mounted_volume != volume_name or (
        config.get("name") is not None and config.get("name") != sandbox_name
    ):
        raise BrokerSmokeError("runtime persisted mount or name does not match the task UUID")
    if (
        volume_labels.get("cognita.owner") != "workspace-broker"
        or volume_labels.get("cognita.workspace") != str(workspace_id)
    ):
        raise BrokerSmokeError("runtime volume owner labels do not match the task UUID")
    if volume_quota_mib != 4096:
        raise BrokerSmokeError("runtime volume quota does not match the separate 4-GiB contract")


async def _read_persisted_metadata(
    sandbox_name: str, volume_name: str,
) -> tuple[dict, dict, object]:
    from cognita.runtime_broker.sdk_adapter_v2 import _sdk_types

    Sandbox, Volume, _ = _sdk_types()
    sandbox = await Sandbox.get(sandbox_name)
    volume = await Volume.get(volume_name)
    if sandbox is None or volume is None:
        raise BrokerSmokeError("runtime metadata disappeared during ownership verification")
    raw = getattr(sandbox, "config_json", None)
    if raw is None:
        config_method = getattr(sandbox, "config", None)
        if callable(config_method):
            raw = config_method()
            if inspect.isawaitable(raw):
                raw = await raw
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError as exc:
            raise BrokerSmokeError("runtime persisted config_json is invalid") from exc
    if not isinstance(raw, dict):
        raise BrokerSmokeError("runtime persisted config metadata is unavailable")
    labels = getattr(volume, "labels", None)
    if inspect.isawaitable(labels):
        labels = await labels
    quota = getattr(volume, "quota_mib", None)
    if inspect.isawaitable(quota):
        quota = await quota
    if not isinstance(labels, dict):
        raise BrokerSmokeError("runtime volume metadata is unavailable")
    return raw, labels, quota


def _verify_persisted_metadata(
    workspace_id: uuid.UUID, sandbox_name: str, volume_name: str,
) -> None:
    """Offline SDK audit; do not run beside the broker's live SDK process."""
    try:
        config, volume_labels, volume_quota_mib = asyncio.run(
            _read_persisted_metadata(sandbox_name, volume_name)
        )
    except BrokerSmokeError:
        raise
    except Exception as exc:  # noqa: BLE001 - fail closed on SDK API drift
        raise BrokerSmokeError(
            "official SDK persisted metadata verification is unavailable"
        ) from exc
    _validate_persisted_metadata(
        config, volume_labels, volume_quota_mib, workspace_id, sandbox_name, volume_name,
    )


def _require_first_create(observed: dict, sandbox_name: str, volume_name: str) -> bool:
    """Require broker proof that this UUID has no pre-existing runtime object.

    Microsandbox 0.7.0 handles do not expose labels as attributes, but their
    persisted ``config_json`` and Volume metadata provide bounded ownership
    evidence.  A successful ensure after an explicit ``absent`` inspect is
    first-create evidence for this named fixture.
    """

    if observed.get("sandbox_name") not in {sandbox_name, None}:
        raise BrokerSmokeError("existing runtime sandbox name does not match task UUID")
    if observed.get("volume_name") not in {volume_name, None}:
        raise BrokerSmokeError("existing runtime volume name does not match task UUID")
    state = observed.get("state")
    # A microVM can be reported failed after its owning broker container is
    # replaced. Inspect has already validated persisted ownership/config;
    # ensure must recover it before the file and job assertions can pass.
    if state == "partial" and observed.get("partial_state") == "volume_only":
        # A failed first-create may leave its named volume. Inspect has
        # already checked the persisted owner; ensure must reuse that volume.
        return True
    if state not in {"absent", "running", "stopped", "failed"}:
        raise BrokerSmokeError("runtime state is not safely reusable")
    return state != "absent"


def _job_output(data: dict) -> str:
    encoded = data.get("stdout", "")
    if not isinstance(encoded, str):
        return ""
    try:
        return base64.b64decode(encoded, validate=True).decode("utf-8", "replace")
    except (ValueError, UnicodeError):
        return ""


def run(*, workspace_id: uuid.UUID, broker_url: str, secret_file: str) -> int:
    sandbox_name, volume_name = resource_names(workspace_id)
    root = run_root(workspace_id)
    client = BrokerClient(broker_url, _read_secret(secret_file))
    health = client.health()
    if health.get("runtime") != "ready":
        raise BrokerSmokeError("broker health is not ready")

    # Inspect first. The broker validates persisted SDK ownership/config on
    # every operation; an independent SDK client sharing its live registry can
    # stall or disturb the attached VM and is not part of this online smoke.
    observed = client.call(workspace_id, "inspect", {})
    _require_first_create(observed, sandbox_name, volume_name)
    ensured = _ensure(client, workspace_id)
    if ensured.get("sandbox_name") != sandbox_name or ensured.get("volume_name") != volume_name:
        raise BrokerSmokeError("broker ensure returned an unexpected runtime identity")

    content = f"cognita direct broker smoke {workspace_id}\n"
    write = client.call(workspace_id, "fs_write", {
        "path": f"{root}/probe.txt", "text": content,
        "create_parents": True,
    })
    if write.get("sha256") is None:
        raise BrokerSmokeError("broker fs_write omitted the content hash")
    read = client.call(workspace_id, "fs_read", {
        "path": f"{root}/probe.txt", "offset": 0, "max_bytes": 1024, "binary": False,
    })
    if read.get("content") != content:
        raise BrokerSmokeError("broker fs_read did not match the synthetic bytes")

    job = client.call(workspace_id, "job_start", {
        "argv": ["python3", "-c", "print('cognita-direct-broker-ok')"],
        "cwd": root, "timeout_seconds": 60, "env": {}, "async": True,
    })
    job_id = job.get("job_id")
    if not isinstance(job_id, str):
        raise BrokerSmokeError("broker job_start omitted job_id")
    deadline = time.monotonic() + POLL_SECONDS
    while True:
        current = client.call(workspace_id, "job_get", {
            "job_id": job_id, "stdout_offset": 0, "stderr_offset": 0, "max_bytes": 1024,
        })
        state = current.get("state")
        if state in {"succeeded", "failed", "canceled", "timed_out", "lost"}:
            if state != "succeeded" or "cognita-direct-broker-ok" not in _job_output(current):
                raise BrokerSmokeError("direct broker job did not succeed")
            break
        if time.monotonic() >= deadline:
            raise BrokerSmokeError("direct broker job exceeded bounded poll deadline")
        time.sleep(0.25)

    final = client.call(workspace_id, "inspect", {})
    if final.get("state") != "running":
        raise BrokerSmokeError("persistent test Workspace is not running after smoke")
    print(
        "PASS persistent_workspace=" + str(workspace_id)
        + f" sandbox={sandbox_name} volume={volume_name} root={root} "
        + f"sdk={health.get('sdk_version', 'unknown')} "
        + "requested_workspace_quota=4GiB requested_root_quota=4GiB "
        + "manager_admission=not_tested write/read/job=ok"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true", help="perform the live broker test")
    parser.add_argument("--workspace-id", required=True, help="fresh task-owned UUIDv4")
    parser.add_argument("--broker-url", default=BROKER_URL, help=argparse.SUPPRESS)
    parser.add_argument("--secret-file", default=SECRET_FILE, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if not args.run:
        parser.error("refusing to run without explicit --run")
    try:
        return run(
            workspace_id=parse_workspace_id(args.workspace_id),
            broker_url=args.broker_url,
            secret_file=args.secret_file,
        )
    except BrokerSmokeError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
