"""Run a disposable, connector-free Workspace integration smoke test.

This harness talks to the public Workspace-only MCP route over HTTP.  It does
not start Docker, import an SDK, or fake the runtime: the configured Beta
gateway must already point at the real Cognita manager and broker.  A static
Workspace credential and an authenticated Admin session are supplied by the
operator, never created by this script.

The safety contract is deliberately strict:

* the route must be a Workspace-only route (never a combined connector);
* the credential must have no Workspace before the run, proving this run owns
  the first-use allocation;
* only a fresh ``/workspace/.cognita-self-test/<RUN>`` tree is touched; and
* cleanup removes the exact runtime object through Admin and proves its
  diagnostics endpoint is gone.  The script refuses to run if cleanup cannot
  be configured before the first mutation.

Example (PowerShell):

    $env:COGNITA_WORKSPACE_MCP_URL = 'https://beta.example/mcp/workspace/live/mcp/v3'
    $env:COGNITA_WORKSPACE_API_KEY = 'cog_sk_v2_...'
    $env:COGNITA_WORKSPACE_ADMIN_URL = 'https://beta.example'
    $env:COGNITA_ADMIN_COOKIE = 'cognita_session=...; cognita_csrf=...'
    $env:COGNITA_ADMIN_CSRF = '...'
    python scripts/live_workspace_smoke.py --run

The key and cookies are never printed.  No Admin connector or credential is
created by this harness; an existing dedicated test credential is required.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlparse

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

ROOT = PurePosixPath("/workspace/.cognita-self-test")
JOB_POLL_SECONDS = 90.0
REQUIRED_ENV = (
    "COGNITA_WORKSPACE_MCP_URL",
    "COGNITA_WORKSPACE_API_KEY",
    "COGNITA_WORKSPACE_ADMIN_URL",
    "COGNITA_ADMIN_COOKIE",
    "COGNITA_ADMIN_CSRF",
)


class HarnessError(RuntimeError):
    """A bounded setup, protocol, assertion, or cleanup failure."""


def _workspace_route(url: str) -> bool:
    """Accept only the current stable/current Workspace MCP route."""

    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return False
    parts = [part for part in parsed.path.split("/") if part]
    # /mcp/workspace/<slug>/mcp and /mcp/workspace/<slug>/mcp/v3 are the only
    # supported direct routes.  Combined routes must never be used here.
    return (
        len(parts) in {4, 5}
        and parts[:2] == ["mcp", "workspace"]
        and parts[2]
        and parts[3] == "mcp"
        and (len(parts) == 4 or parts[4] == "v3")
        and not parsed.query
        and not parsed.fragment
    )


def _run_root(run_id: str) -> str:
    """Return one normalized run root and reject path-shaped identifiers."""

    if not run_id or len(run_id) > 48 or any(
        ch not in "abcdefghijklmnopqrstuvwxyz0123456789-" for ch in run_id
    ):
        raise HarnessError("run id must be lowercase ASCII letters, digits, and hyphens")
    if run_id in {".", ".."}:
        raise HarnessError("run id is not a path component")
    return str(ROOT / run_id)


def _payload(response: httpx.Response) -> dict[str, Any]:
    """Decode either plain JSON or the final SSE data frame."""

    body = response.text
    if response.headers.get("content-type", "").startswith("text/event-stream"):
        frames = [line[5:].strip() for line in body.splitlines() if line.startswith("data:")]
        if not frames:
            raise HarnessError("MCP response contained no SSE data frame")
        body = frames[-1]
    try:
        value = json.loads(body)
    except (TypeError, ValueError) as exc:
        raise HarnessError("MCP response was not JSON") from exc
    if not isinstance(value, dict):
        raise HarnessError("MCP response was not a JSON object")
    return value


class McpClient:
    """Minimal streamable-HTTP client for the real Workspace gateway."""

    def __init__(self, url: str, token: str, *, timeout: float = 120.0):
        self.url = url
        self.http = httpx.Client(
            timeout=timeout,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json, text/event-stream",
                "Content-Type": "application/json",
            },
        )
        self.session_id: str | None = None
        self._next_id = 0

    def close(self) -> None:
        self.http.close()

    def _post(self, message: dict[str, Any]) -> dict[str, Any]:
        headers = {"mcp-session-id": self.session_id} if self.session_id else {}
        response = self.http.post(self.url, json=message, headers=headers)
        response.raise_for_status()
        if session_id := response.headers.get("mcp-session-id"):
            self.session_id = session_id
        return _payload(response)

    def initialize(self) -> dict[str, Any]:
        self._next_id += 1
        envelope = self._post({
            "jsonrpc": "2.0",
            "id": self._next_id,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "cognita-live-workspace-smoke", "version": "1"},
            },
        })
        if "error" in envelope:
            raise HarnessError("Workspace MCP initialize was rejected")
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})
        result = envelope.get("result")
        if not isinstance(result, dict):
            raise HarnessError("Workspace MCP initialize returned no result")
        return result

    def tools(self) -> list[str]:
        self._next_id += 1
        envelope = self._post({
            "jsonrpc": "2.0", "id": self._next_id, "method": "tools/list", "params": {},
        })
        result = envelope.get("result")
        tools = result.get("tools") if isinstance(result, dict) else None
        if not isinstance(tools, list):
            raise HarnessError("Workspace MCP tools/list returned no tool list")
        return [
            item["name"] for item in tools
            if isinstance(item, dict) and isinstance(item.get("name"), str)
        ]

    def call(self, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        self._next_id += 1
        envelope = self._post({
            "jsonrpc": "2.0",
            "id": self._next_id,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments or {}},
        })
        if "error" in envelope:
            raise HarnessError(f"Workspace tool {name} returned a JSON-RPC error")
        result = envelope.get("result")
        content = result.get("content") if isinstance(result, dict) else None
        text_block = next(
            (
                item for item in content or ()
                if isinstance(item, dict) and item.get("type") == "text"
            ),
            None,
        )
        if not isinstance(text_block, dict) or not isinstance(text_block.get("text"), str):
            raise HarnessError(f"Workspace tool {name} returned no JSON text payload")
        try:
            payload = json.loads(text_block["text"])
        except ValueError as exc:
            raise HarnessError(f"Workspace tool {name} returned non-JSON text") from exc
        if not isinstance(payload, dict):
            raise HarnessError(f"Workspace tool {name} returned a non-object payload")
        return payload


class AdminClient:
    """Admin transport using an already authenticated, CSRF-bound session."""

    def __init__(self, base_url: str, cookie: str, csrf: str, *, timeout: float = 30.0):
        self.http = httpx.Client(
            base_url=base_url.rstrip("/"),
            timeout=timeout,
            headers={"Cookie": cookie, "X-CSRF-Token": csrf, "Accept": "application/json"},
        )

    def close(self) -> None:
        self.http.close()

    def diagnostics(self, workspace_id: str) -> tuple[int, dict[str, Any]]:
        response = self.http.get(f"/api/workspaces/{workspace_id}/diagnostics")
        if response.status_code == 404:
            return response.status_code, {}
        response.raise_for_status()
        value = response.json()
        if not isinstance(value, dict):
            raise HarnessError("Admin diagnostics returned a non-object payload")
        return response.status_code, value

    def remove(self, workspace_id: str, revision: int, token: str) -> dict[str, Any]:
        response = self.http.post(
            f"/api/workspaces/{workspace_id}/remove",
            json={
                "expected_revision": revision,
                "confirm": True,
                "idempotency_token": token,
            },
        )
        response.raise_for_status()
        value = response.json()
        if not isinstance(value, dict):
            raise HarnessError("Admin remove returned a non-object payload")
        return value


@dataclass
class RunState:
    root: str
    workspace_id: str | None = None
    job_id: str | None = None
    cleanup_error: str | None = None


def _require_success(name: str, payload: dict[str, Any]) -> dict[str, Any]:
    if payload.get("status") != "success":
        evidence = {
            field: payload.get(field)
            for field in ("reason", "broker_code", "broker_stage", "correlation_id", "retryable")
            if payload.get(field) is not None
        }
        raise HarnessError(f"{name} failed with bounded runtime evidence {evidence!r}")
    return payload


def _workspace_id(payload: dict[str, Any]) -> tuple[str, int]:
    workspace = payload.get("workspace")
    if not isinstance(workspace, dict):
        raise HarnessError("Workspace response omitted its durable identity")
    workspace_id = workspace.get("workspace_id")
    revision = workspace.get("revision")
    if (
        not isinstance(workspace_id, str)
        or not workspace_id
        or isinstance(revision, bool)
        or not isinstance(revision, int)
    ):
        raise HarnessError("Workspace response contained an invalid durable identity")
    return workspace_id, revision


def _cleanup(mcp: McpClient, admin: AdminClient, state: RunState) -> None:
    """Remove only the run root and exact Workspace identity, reporting failures."""

    try:
        if state.job_id:
            result = mcp.call("workspace_get_job", {
                "job_id": state.job_id,
                "stdout_offset": 0,
                "stderr_offset": 0,
                "max_bytes": 1024,
            })
            job = result.get("job") if isinstance(result.get("job"), dict) else {}
            if job.get("state") not in {"succeeded", "failed", "canceled", "timed_out", "lost"}:
                mcp.call("workspace_cancel_job", {
                    "job_id": state.job_id,
                    "idempotency_key": f"{state.root.rsplit('/', 1)[-1]}-cleanup-cancel",
                })
        if state.workspace_id is not None:
            mcp.call("workspace_remove_paths", {
                "paths": [state.root], "recursive": True,
                "idempotency_key": f"{state.root.rsplit('/', 1)[-1]}-cleanup-root",
            })
        if state.workspace_id is None:
            return
        status, evidence = admin.diagnostics(state.workspace_id)
        if status == 404:
            return
        workspace = evidence.get("workspace")
        revision = workspace.get("revision") if isinstance(workspace, dict) else None
        if not isinstance(revision, int):
            raise HarnessError(
                "Admin cleanup could not obtain the exact current Workspace revision"
            )
        admin.remove(state.workspace_id, revision, f"live-smoke-remove-{state.workspace_id}")
        status, _ = admin.diagnostics(state.workspace_id)
        if status != 404:
            raise HarnessError("Admin cleanup did not prove the Workspace runtime/volume absent")
    except Exception as exc:  # noqa: BLE001 - preserve primary test failure
        state.cleanup_error = type(exc).__name__


def run() -> int:
    missing = [name for name in REQUIRED_ENV if not os.environ.get(name)]
    if missing:
        raise HarnessError("missing required environment: " + ", ".join(missing))
    mcp_url = os.environ["COGNITA_WORKSPACE_MCP_URL"]
    if not _workspace_route(mcp_url):
        raise HarnessError("COGNITA_WORKSPACE_MCP_URL must be the current Workspace-only MCP route")
    root = _run_root(f"live-{uuid.uuid4().hex[:16]}")
    state = RunState(root=root)
    mcp = McpClient(mcp_url, os.environ["COGNITA_WORKSPACE_API_KEY"])
    admin = AdminClient(
        os.environ["COGNITA_WORKSPACE_ADMIN_URL"],
        os.environ["COGNITA_ADMIN_COOKIE"],
        os.environ["COGNITA_ADMIN_CSRF"],
    )
    try:
        mcp.initialize()
        from cognita.proxy import WORKSPACE_TOOL_NAMES

        names = mcp.tools()
        required = set(WORKSPACE_TOOL_NAMES)
        if not required.issubset(names):
            raise HarnessError("Workspace-only catalog is missing required tools")
        if any(
            name.startswith(("add_", "read_document", "copy_to_workspace", "copy_from_workspace"))
            for name in names
        ):
            raise HarnessError("refusing a combined Knowledge/bridge catalog")

        before = _require_success("workspace_info", mcp.call("workspace_info"))
        if before.get("workspace") is not None:
            raise HarnessError("credential already owns a Workspace; refusing to touch it")

        content = f"cognita live Workspace smoke {root}\n"
        write = _require_success("workspace_write_file", mcp.call("workspace_write_file", {
            "path": f"{root}/probe.txt", "text": content, "create_policy": "parents",
            "idempotency_key": f"{root.rsplit('/', 1)[-1]}-write",
        }))
        workspace_id, _ = _workspace_id(write)
        state.workspace_id = workspace_id

        read = _require_success("workspace_read_file", mcp.call("workspace_read_file", {
            "path": f"{root}/probe.txt", "offset": 0, "max_bytes": 1024, "encoding": "text",
        }))
        data = read.get("data")
        if not isinstance(data, dict) or data.get("content") != content:
            raise HarnessError("Workspace read-back did not match the synthetic bytes")

        started = _require_success("workspace_start_job", mcp.call("workspace_start_job", {
            "argv": ["python3", "-c", "print('cognita-live-workspace-ok')"],
            "timeout": 60, "env": {},
            "idempotency_key": f"{root.rsplit('/', 1)[-1]}-job",
        }))
        job = started.get("job")
        if not isinstance(job, dict) or not isinstance(job.get("job_id"), str):
            raise HarnessError("Workspace job response omitted job_id")
        state.job_id = job["job_id"]
        deadline = time.monotonic() + JOB_POLL_SECONDS
        while True:
            result = _require_success("workspace_get_job", mcp.call("workspace_get_job", {
                "job_id": state.job_id, "stdout_offset": 0, "stderr_offset": 0, "max_bytes": 1024,
            }))
            observed = result.get("job")
            if not isinstance(observed, dict):
                raise HarnessError("Workspace job response omitted job state")
            if observed.get("state") in {"succeeded", "failed", "canceled", "timed_out", "lost"}:
                try:
                    stdout = base64.b64decode(observed.get("stdout", ""), validate=True).decode("utf-8")
                except (ValueError, TypeError):
                    stdout = ""
                if observed.get("state") != "succeeded" or "cognita-live-workspace-ok" not in stdout:
                    raise HarnessError("synthetic Workspace job did not succeed")
                break
            if time.monotonic() >= deadline:
                raise HarnessError("synthetic Workspace job exceeded bounded poll deadline")
            time.sleep(0.25)
        print(f"PASS workspace_id={state.workspace_id} root={state.root} write/read/job=ok")
        return 0
    finally:
        _cleanup(mcp, admin, state)
        mcp.close()
        admin.close()
        if state.cleanup_error:
            raise HarnessError(f"cleanup did not prove removal ({state.cleanup_error})")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true", help="perform the live disposable test")
    args = parser.parse_args(argv)
    if not args.run:
        parser.error("refusing to run without explicit --run")
    try:
        return run()
    except HarnessError as exc:
        print(f"BLOCKED/FAIL: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    except httpx.HTTPError as exc:
        # Do not echo response bodies or URLs, which may contain operator
        # details.  The exception class is enough to classify transport loss.
        print(f"BLOCKED/FAIL: {type(exc).__name__}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
