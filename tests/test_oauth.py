"""Package-backed Cognita 8.0 OAuth integration coverage.

These tests intentionally exercise the real single-worker Uvicorn child over HTTP.
The child owns the temporary DOT database; no legacy OAuth store is involved.
"""

from __future__ import annotations

import asyncio
import base64
import ctypes
import ctypes.wintypes
import hashlib
import html
import logging
import os
import re
import socket
import sqlite3
import subprocess
import queue
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
import yaml
from argon2 import PasswordHasher

from cognita.oauth_service_client import OAuthServiceClient

log = logging.getLogger(__name__)

# Hang guard on a wait for a real child process to exit after terminate/kill
# (exit is guaranteed; a kill follows a terminate that is not honored). It is
# generous because a correct run never spends it; a regression fails with a
# named timeout instead of hanging.
STOP_BOUND_S = 30.0

# Hang guard on the child's startup handshake. The child always either prints
# LISTENING_MARKER or exits (stdout EOF), so the signal is guaranteed; the
# bound only stops a child that hangs alive from hanging the run.
STARTUP_BOUND_S = 60.0
LISTENING_MARKER = "cognita-test-oauth-listening"

# Test-owned launcher for the real OAuth child: the package's own main(), plus
# one stdout line once Uvicorn's startup has bound the listening socket.
LAUNCHER_SOURCE = f'''\
import os

import uvicorn

from cognita.oauth_service import __main__ as oauth_main

_startup = uvicorn.Server.startup


async def _announcing_startup(self, sockets=None):
    await _startup(self, sockets=sockets)
    if self.started:
        print({LISTENING_MARKER!r}, os.getpid(), flush=True)


uvicorn.Server.startup = _announcing_startup
oauth_main.main()
'''

# On Windows, venv\Scripts\python.exe is a redirector stub: Popen.pid is the
# stub, and the interpreter that holds oauth.sqlite3 and the port is a second
# process the stub's job kills when the stub dies, a moment AFTER the stub's
# own exit is visible. The old fixed 0.2s nap after terminate() hid that gap.
# The launcher reports the interpreter's own pid, and _stop waits on that
# process's handle, the kernel's signal that it has exited and let go of its
# files. On POSIX the venv python is the interpreter itself, so Popen.wait is
# already that signal.
_REAL_PROCESS_HANDLES: dict[int, int] = {}


def _hold_real_process(child: subprocess.Popen, real_pid: int) -> None:
    """Open a wait handle on the interpreter the stub started (Windows only)."""
    if sys.platform != "win32" or real_pid == child.pid:
        log.info("OAuth test child pid=%s is the interpreter; no extra handle", child.pid)
        return
    synchronize = 0x00100000
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = ctypes.wintypes.HANDLE
    kernel32.OpenProcess.argtypes = [ctypes.wintypes.DWORD, ctypes.wintypes.BOOL, ctypes.wintypes.DWORD]
    # Opened while the interpreter is certainly alive (it just printed its
    # marker), so the pid cannot have been reused yet.
    handle = kernel32.OpenProcess(synchronize, False, real_pid)
    assert handle, f"cannot open OAuth interpreter pid={real_pid}: winerror={ctypes.get_last_error()}"
    log.info("OAuth test child stub pid=%s interpreter pid=%s", child.pid, real_pid)
    _REAL_PROCESS_HANDLES[child.pid] = handle


def _wait_real_process(child: subprocess.Popen) -> None:
    """Wait (hang-guarded) until the interpreter behind the stub has exited."""
    handle = _REAL_PROCESS_HANDLES.pop(child.pid, None)
    if handle is None:
        return
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.WaitForSingleObject.restype = ctypes.wintypes.DWORD
    kernel32.WaitForSingleObject.argtypes = [ctypes.wintypes.HANDLE, ctypes.wintypes.DWORD]
    kernel32.CloseHandle.argtypes = [ctypes.wintypes.HANDLE]
    try:
        result = kernel32.WaitForSingleObject(handle, int(STOP_BOUND_S * 1000))
    finally:
        kernel32.CloseHandle(handle)
    log.info("OAuth test child stub pid=%s interpreter wait result=%s", child.pid, result)
    assert result == 0, (
        f"OAuth interpreter behind stub pid={child.pid} did not exit within "
        f"{STOP_BOUND_S}s (wait result={result})"
    )

VERIFIER = "v" * 64
CHALLENGE = base64.urlsafe_b64encode(
    hashlib.sha256(VERIFIER.encode()).digest()
).rstrip(b"=").decode()
TOKEN_KEYS = {"access_token", "expires_in", "refresh_token", "scope", "token_type"}
CALLBACK_URI = "http://127.0.0.1:49152/callback?existing=%2B%20value&encoded=%2Foauth%3Fnext%3Dyes"
CONNECTOR_ID = "2c520a44-2037-4bb5-a565-d88ec2bb02d1"
OTHER_CONNECTOR_ID = "3d631b55-3148-4cc6-b676-e99fd3cc13e2"
CONNECTOR_SLUG = "cognita"
OTHER_CONNECTOR_SLUG = "other-connector"


def _port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _write_config(root: Path, port: int) -> tuple[Path, Path]:
    data = root / "data"
    data.mkdir()
    registry = root / "registry.yaml"
    registry.write_text(
        yaml.safe_dump({
            "version": 1,
            "projects": [{
                "name": "demo",
                "documents_dir": str(root / "docs"),
                "data_dir": str(root / "project"),
                "enabled": True,
            }, {
                "name": "disabled",
                "documents_dir": str(root / "disabled-docs"),
                "data_dir": str(root / "disabled-project"),
                "enabled": False,
            }],
        }),
        encoding="utf-8",
    )
    connectors = root / "connectors.yaml"
    connectors.write_text(
        yaml.safe_dump({
            "version": 1,
            "revision": 1,
            "connectors": [
                {
                    "id": CONNECTOR_ID,
                    "name": "Cognita",
                    "enabled": True,
                    "project_mode": "all",
                    "default_access": "write",
                    "project_access": {},
                },
                {
                    "id": OTHER_CONNECTOR_ID,
                    "name": "Other connector",
                    "enabled": True,
                    "project_mode": "all",
                    "default_access": "write",
                    "project_access": {},
                },
            ],
        }),
        encoding="utf-8",
    )
    config = root / "cognita.yaml"
    config.write_text(
        yaml.safe_dump({
            "data_root": str(data),
            "registry_path": str(registry),
            "public_base_url": f"http://127.0.0.1:{port}",
            "oauth_service_port": port,
            "oauth_service_store_path": str(data / "oauth.sqlite3"),
            "connectors_path": str(connectors),
            "oauth_allowed_client_hosts": ["chatgpt.com", "claude.ai", "127.0.0.1"],
            "oauth_cimd_allowed_hosts": ["chatgpt.com", "claude.ai"],
            "admin_username": "admin",
            "admin_password_hash": PasswordHasher().hash("pw"),
        }),
        encoding="utf-8",
    )
    return config, data


def _start(
    config: Path, port: int, *, revoke_all_on_start: bool = False
) -> subprocess.Popen:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(Path(__file__).parents[1] / "src")
    creationflags = (
        getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        | getattr(subprocess, "CREATE_NO_WINDOW", 0)
    )
    # Superseded: this ran `-m cognita.oauth_service` directly. The launcher
    # runs the same cognita.oauth_service.__main__.main() and additionally
    # prints LISTENING_MARKER once Uvicorn has bound its socket, which is the
    # signal _wait_ready waits on instead of polling the port.
    launcher = config.parent / "oauth_child_launcher.py"
    launcher.write_text(LAUNCHER_SOURCE, encoding="utf-8")
    command = [
        sys.executable,
        str(launcher),
        "--config",
        str(config),
        "--port",
        str(port),
        "--parent-pid",
        str(os.getpid()),
    ]
    if revoke_all_on_start:
        command.append("--revoke-all-on-start")
    return subprocess.Popen(
        command,
        cwd=Path(__file__).parents[1],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        creationflags=creationflags,
    )


def _stop(child: subprocess.Popen) -> None:
    if child.poll() is None:
        child.terminate()
        # Wait on the child's own exit (bounded) instead of a fixed 0.2s nap;
        # only a child that outlives the bound is forced.
        try:
            child.wait(timeout=STOP_BOUND_S)
        except subprocess.TimeoutExpired:
            log.warning(
                "OAuth test child pid=%s survived terminate for %.1fs; killing",
                child.pid, STOP_BOUND_S,
            )
    if child.poll() is None:
        child.kill()
    child.wait(timeout=STOP_BOUND_S)
    assert child.poll() is not None
    # The stub's exit is not the interpreter's (see _REAL_PROCESS_HANDLES).
    _wait_real_process(child)


def _await_listening(child: subprocess.Popen) -> None:
    """Block until the launcher reports that Uvicorn is listening.

    A reader thread forwards one verdict: the interpreter pid on
    LISTENING_MARKER, None on EOF (the child exited or closed stdout before
    listening). Either arrives in every correct or failed run; the
    STARTUP_BOUND_S get() is only a guard against a child that hangs alive.
    """
    verdict: queue.Queue[int | None] = queue.Queue()

    def pump() -> None:
        try:
            for line in child.stdout:
                marker, _, pid = line.strip().partition(" ")
                if marker == LISTENING_MARKER:
                    verdict.put(int(pid))
                    return
        except (OSError, ValueError) as exc:
            log.warning("OAuth test child stdout read failed pid=%s: %r", child.pid, exc)
        verdict.put(None)

    threading.Thread(target=pump, name="oauth-child-stdout", daemon=True).start()
    try:
        real_pid = verdict.get(timeout=STARTUP_BOUND_S)
    except queue.Empty:
        raise AssertionError(
            f"OAuth child sent no listening marker within {STARTUP_BOUND_S}s "
            f"(still running={child.poll() is None})"
        ) from None
    log.info("OAuth test child pid=%s listening interpreter_pid=%s", child.pid, real_pid)
    if real_pid is None:
        # stdout reached EOF, so the child is exiting; the wait is a guard.
        child.wait(timeout=STOP_BOUND_S)
        raise AssertionError("OAuth child exited before listening: " + _stderr(child))
    _hold_real_process(child, real_pid)


def _wait_ready(client: httpx.Client, data: Path) -> str:
    # Superseded: this polled the key file and /_cognita/ready every 0.1s for
    # up to 20s. It now waits on the child's own listening signal, after which
    # the key file (written by bootstrap before Uvicorn starts) must exist and
    # one authenticated readiness request must answer 200.
    child = _wait_ready._child
    _await_listening(child)
    key_path = data / ".oauth-service-key"
    assert key_path.exists(), "OAuth child listened without its owned key"
    key = key_path.read_bytes().hex()
    response = client.get("/_cognita/ready", auth=httpx.BasicAuth("cognita-internal", key))
    assert response.status_code == 200, response.text
    return key




def _stderr(child: subprocess.Popen) -> str:
    if child.poll() is None:
        return "child still running"
    try:
        _, stderr = child.communicate(timeout=1.0)
    except subprocess.TimeoutExpired:
        return "child exited without readable diagnostics"
    return stderr[-4000:]



def _register(client: httpx.Client) -> str:
    response = client.post(
        "/oauth/register",
        json={
            "client_name": "HTTP test client",
            "redirect_uris": [CALLBACK_URI],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
            "scope": "cognita:access",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["client_id"]


def _authorize(
    client: httpx.Client,
    client_id: str,
    resource: str,
    *,
    submitted_resource: str | None = None,
    state: str = "state-1",
) -> str:
    params = {
        "client_id": client_id,
        "redirect_uri": CALLBACK_URI,
        "response_type": "code",
        "code_challenge": CHALLENGE,
        "code_challenge_method": "S256",
        "resource": resource,
        "scope": "cognita:access",
        "state": state,
    }
    response = client.get("/oauth/authorize", params=params, follow_redirects=False)
    assert response.status_code in {302, 303}, response.text
    login_url = response.headers["location"]
    assert urlsplit(login_url).path == "/oauth/login"

    login_page = client.get(login_url)
    assert login_page.status_code == 200
    csrf_match = re.search(
        r'name=["\']?csrfmiddlewaretoken["\']? value="([^"]+)"', login_page.text
    )
    assert csrf_match, login_page.text[:2000]
    csrf = csrf_match.group(1)
    next_url = html.unescape(
        re.search(r'name=["\']?next["\']? value="([^"]+)"', login_page.text).group(1)
    )
    logged_in = client.post(
        "/oauth/login",
        data={"csrfmiddlewaretoken": csrf, "username": "admin", "password": "pw", "next": next_url},
        follow_redirects=False,
    )
    assert logged_in.status_code in {302, 303}, logged_in.text
    assert "cognita_oauth_session" in client.cookies

    consent = client.get(logged_in.headers["location"])
    assert consent.status_code == 200, (
        f"status={consent.status_code} location={consent.headers.get('location')} "
        f"cookies={client.cookies}"
    )
    csrf = re.search(
        r'name=["\']?csrfmiddlewaretoken["\']? value="([^"]+)"', consent.text
    ).group(1)
    hidden = {
        key: html.unescape(value)
        for key, value in re.findall(
            r'<input[^>]+name="([^"]+)"[^>]+value="([^"]*)"', consent.text
        )
    }
    hidden["csrfmiddlewaretoken"] = csrf
    hidden["allow"] = "Authorize"
    if submitted_resource is not None:
        hidden["resource"] = submitted_resource
    approved = client.post(
        "/oauth/authorize",
        data=hidden,
        follow_redirects=False,
    )
    assert approved.status_code in {302, 303}, approved.text
    query = parse_qs(urlsplit(approved.headers["location"]).query)
    assert query["state"] == [state]
    if submitted_resource is not None:
        assert query["error"] == ["invalid_target"]
        assert query["existing"] == ["+ value"]
        assert query["encoded"] == ["/oauth?next=yes"]
        assert query["iss"] == [urlsplit(resource).scheme + "://" + urlsplit(resource).netloc]
        return ""
    assert query["existing"] == ["+ value"]
    assert query["encoded"] == ["/oauth?next=yes"]
    assert query["iss"] == [urlsplit(resource).scheme + "://" + urlsplit(resource).netloc]
    return query["code"][0]


def _exchange(client: httpx.Client, client_id: str, code: str, resource: str) -> dict:
    response = client.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "client_id": client_id,
            "code": code,
            "redirect_uri": CALLBACK_URI,
            "resource": resource,
            "code_verifier": VERIFIER,
        },
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert set(payload) == TOKEN_KEYS
    return payload


def test_package_oauth_http_flow_lifecycle_and_restart() -> None:
    port = _port()
    with TemporaryDirectory(prefix="cognita-oauth8-http-") as owned:
        root = Path(owned)
        config, data = _write_config(root, port)
        child = _start(config, port)
        try:
            with httpx.Client(
                base_url=f"http://127.0.0.1:{port}",
                timeout=5,
                follow_redirects=False,
            ) as client:
                _wait_ready._child = child
                key = _wait_ready(client, data)
                internal = httpx.BasicAuth("cognita-internal", key)
                assert child.poll() is None, _stderr(child)
                metadata = client.get("/.well-known/oauth-authorization-server")
                assert metadata.status_code == 200
                assert metadata.json()["issuer"] == f"http://127.0.0.1:{port}"
                assert metadata.json()["authorization_response_iss_parameter_supported"] is True

                client_id = _register(client)
                resource = f"http://127.0.0.1:{port}/mcp/connectors/{CONNECTOR_SLUG}/mcp/v{PUBLIC_CONTRACT_VERSION}"
                _authorize(
                    client,
                    client_id,
                    resource,
                    submitted_resource="not-a-resource",
                    state="state + /?",
                )
                code = _authorize(client, client_id, resource)
                other_resource = f"http://127.0.0.1:{port}/mcp/connectors/{OTHER_CONNECTOR_SLUG}/mcp/v{PUBLIC_CONTRACT_VERSION}"
                switched = client.post(
                    "/oauth/token",
                    data={
                        "grant_type": "authorization_code",
                        "client_id": client_id,
                        "code": code,
                        "redirect_uri": CALLBACK_URI,
                        "resource": other_resource,
                        "code_verifier": VERIFIER,
                    },
                )
                assert switched.status_code == 400
                assert switched.json()["error"] == "invalid_target"

                code = _authorize(client, client_id, resource)
                tokens = _exchange(client, client_id, code, resource)

                introspection = client.post(
                    "/_cognita/introspect",
                    auth=internal,
                    data={"token": tokens["access_token"]},
                )
                assert introspection.status_code == 200
                claims = introspection.json()
                assert claims["active"] is True
                assert claims["aud"] == [resource]

                duplicate = client.post(
                    "/oauth/token",
                    content=urlencode([
                        ("grant_type", "refresh_token"),
                        ("client_id", client_id),
                        ("refresh_token", tokens["refresh_token"]),
                        ("resource", resource),
                        ("resource", f"http://127.0.0.1:{port}/mcp/disabled"),
                    ]),
                    headers={"content-type": "application/x-www-form-urlencoded"},
                )
                assert duplicate.status_code == 400
                assert duplicate.json()["error"] == "invalid_target"

                refresh_switch = client.post(
                    "/oauth/token",
                    data={
                        "grant_type": "refresh_token",
                        "client_id": client_id,
                        "refresh_token": tokens["refresh_token"],
                        "resource": other_resource,
                    },
                )
                assert refresh_switch.status_code == 400
                assert refresh_switch.json()["error"] == "invalid_target"

                reusable = client.post(
                    "/oauth/token",
                    data={
                        "grant_type": "refresh_token",
                        "client_id": client_id,
                        "refresh_token": tokens["refresh_token"],
                    },
                )
                assert reusable.status_code == 200, reusable.text
                successor = reusable.json()
                assert successor["refresh_token"] == tokens["refresh_token"]
                retry = client.post(
                    "/oauth/token",
                    data={
                        "grant_type": "refresh_token",
                        "client_id": client_id,
                        "refresh_token": tokens["refresh_token"],
                    },
                )
                assert retry.status_code == 200
                assert retry.json()["refresh_token"] == tokens["refresh_token"]

                client.cookies.delete("csrftoken")
                groups = client.get("/_cognita/connections", auth=internal)
                assert groups.status_code == 200
                assert client.cookies.get("csrftoken")
                assert len(groups.json()) == 1
                connection_id = groups.json()[0]["id"]
                csrf_token = client.cookies.get("csrftoken")
                assert csrf_token
                csrf_rejected = client.delete(
                    f"/_cognita/connections/{connection_id}", auth=internal
                )
                assert csrf_rejected.status_code == 403
                parent_client = OAuthServiceClient(
                    base_url=f"http://127.0.0.1:{port}",
                    internal_client_id="cognita-internal",
                    internal_client_secret=key,
                )
                revoked_count = asyncio.run(parent_client.revoke_connection(connection_id))
                assert revoked_count == 1
                inactive = client.post(
                    "/_cognita/introspect",
                    auth=internal,
                    data={"token": successor["access_token"]},
                )
                assert inactive.json() == {"active": False}

                # A new authorization proves the child can continue after group revoke.
                fresh = _exchange(
                    client,
                    client_id,
                    _authorize(client, client_id, resource),
                    resource,
                )
                concurrent_source = fresh["refresh_token"]

            def refresh_once() -> tuple[int, str]:
                with httpx.Client(
                    base_url=f"http://127.0.0.1:{port}", timeout=5
                ) as independent:
                    response = independent.post(
                        "/oauth/token",
                        data={
                            "grant_type": "refresh_token",
                            "client_id": client_id,
                            "refresh_token": concurrent_source,
                        },
                    )
                    payload = response.json()
                    return response.status_code, payload.get("refresh_token", "")

            with ThreadPoolExecutor(max_workers=2) as pool:
                concurrent = list(pool.map(lambda _: refresh_once(), range(2)))
            assert [status for status, _ in concurrent] == [200, 200]
            assert len({token for _, token in concurrent}) == 1
            assert concurrent[0][1] == concurrent_source

            _stop(child)
            sqlite = sqlite3.connect(data / "oauth.sqlite3")
            try:
                for table in ("oauth2_provider_accesstoken", "oauth2_provider_refreshtoken"):
                    rows = sqlite.execute(f"SELECT token, token_checksum FROM {table}").fetchall()
                    assert rows
                    assert all(token == "" and checksum for token, checksum in rows)
            finally:
                sqlite.close()
            child = _start(config, port)
            with httpx.Client(
                base_url=f"http://127.0.0.1:{port}", timeout=5
            ) as restarted:
                _wait_ready._child = child
                key = _wait_ready(restarted, data)
                connections = restarted.get(
                    "/_cognita/connections",
                    auth=httpx.BasicAuth("cognita-internal", key),
                )
                assert connections.status_code == 200
                assert len(connections.json()) == 1

            _stop(child)
            child = _start(config, port, revoke_all_on_start=True)
            with httpx.Client(
                base_url=f"http://127.0.0.1:{port}", timeout=5
            ) as emergency:
                _wait_ready._child = child
                key = _wait_ready(emergency, data)
                emergency_connections = emergency.get(
                    "/_cognita/connections",
                    auth=httpx.BasicAuth("cognita-internal", key),
                )
                assert emergency_connections.json() == []
                # The one-shot action revokes token state while preserving the DOT
                # application and its DCR registration for a later reconnect.
                final_tokens = _exchange(
                    emergency,
                    client_id,
                    _authorize(emergency, client_id, resource),
                    resource,
                )
                assert final_tokens["refresh_token"]

                # A legacy policy-file generation cannot override the
                # code-owned public contract or invalidate its V2 tokens.
                config_payload = yaml.safe_load(config.read_text(encoding="utf-8"))
                connectors_path = Path(config_payload["connectors_path"])
                connector_payload = yaml.safe_load(
                    connectors_path.read_text(encoding="utf-8")
                )
                connector_payload["revision"] += 1
                connector_payload["connectors"][0]["contract_version"] = 2
                connectors_path.write_text(
                    yaml.safe_dump(connector_payload), encoding="utf-8"
                )
                still_active = emergency.post(
                    "/_cognita/introspect",
                    auth=httpx.BasicAuth("cognita-internal", key),
                    data={"token": final_tokens["access_token"]},
                )
                assert still_active.status_code == 200
                assert still_active.json()["active"] is True
                assert still_active.json()["aud"] == [resource]
        finally:
            _stop(child)
    assert not root.exists()
