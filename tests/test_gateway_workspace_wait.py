"""A1 (DESIGN-12.18-WORKSPACE-NEXT-FEATURES.md §3.1): the gateway's
_workspace_call runs a wait_ms > 0 call in the fixed 10-thread
workspace-wait executor (loop.run_in_executor), so it cannot freeze the
event loop. This is the one gateway-level test §3.1 asks for: "a wait call
does not block a concurrent ping."

Route and auth setup mirrors
tests/test_route_contracts_12.py::test_workspace_stable_and_versioned_routes_authenticate
-- the Workspace-only route needs no Knowledge project/engine at all, so a
fake WorkspaceManager is enough here.
"""

from __future__ import annotations

import asyncio
import threading

import pytest
from argon2 import PasswordHasher
from httpx import ASGITransport, AsyncClient

from cognita.auth_policy import CredentialPolicyStore
from cognita.config import CognitaConfig
from cognita.connectors import ConnectorStore, WorkspaceConnectorStore
from cognita.gateway import create_gateway_app
from cognita.registry import Project, Registry


class BlockingWorkspaceManager:
    """A fake WorkspaceManager.execute that blocks on a real threading.Event
    for a wait_ms call, so the test can prove the gateway offloaded it
    instead of running it on the event loop. A non-waiting call (or any
    other tool) answers immediately."""

    def __init__(self):
        self.entered = threading.Event()
        self.release = threading.Event()
        # 2026-09-22: whether the wait call ran on a thread that is running
        # an event loop. That is the regression itself, observed directly,
        # rather than inferred from how long a concurrent ping took.
        self.ran_on_event_loop: bool | None = None

    def execute(self, principal, tool, arguments, *, connector_id=None):
        if tool == "workspace_start_job" and isinstance(arguments, dict) and arguments.get("wait_ms"):
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                self.ran_on_event_loop = False
            else:
                self.ran_on_event_loop = True
            self.entered.set()
            if self.ran_on_event_loop:
                # Blocking here would wedge the loop that has to report the
                # failure; answer at once and let the assertion name it.
                return {
                    "status": "success", "workspace": {}, "job": {"job_id": "job-1", "state": "succeeded"},
                    "waited_ms": 0, "wake_reason": "exited",
                }
            self.release.wait(5)
            return {
                "status": "success", "workspace": {}, "job": {"job_id": "job-1", "state": "succeeded"},
                "waited_ms": 1, "wake_reason": "exited",
            }
        return {"status": "success", "workspace": {}, "data": {}}


@pytest.fixture
def app_and_manager(tmp_path):
    registry = Registry(tmp_path / "registry.yaml")
    registry.add(Project(name="Knowledge", documents_dir=tmp_path, data_dir=tmp_path))
    workspace_connectors = WorkspaceConnectorStore(tmp_path / "workspace-connectors.yaml")
    surface = workspace_connectors.create(
        expected_revision=0, display_name="Workspace", enabled=True, slug="workspace",
    )
    credentials = CredentialPolicyStore(
        tmp_path / "credentials-v2.json",
        master_key_dir=tmp_path / "master-keys",
        admin_password_hash=PasswordHasher().hash("correct horse"),
    )
    _row, secret = credentials.add_credential(
        "workspace", surface.id, "Runner", surface_slug=surface.slug, password="correct horse",
    )
    config = CognitaConfig(
        registry_path=registry.path, public_base_url="https://example.test", oauth_enabled=False,
    )
    manager = BlockingWorkspaceManager()
    app = create_gateway_app(
        config, registry,
        connector_store=ConnectorStore(tmp_path / "connectors.yaml"),
        workspace_connector_store=workspace_connectors,
        credential_store=credentials,
        workspace_service=manager,
    )
    return app, manager, secret


def test_wait_call_does_not_block_a_concurrent_ping(app_and_manager):
    """A synchronous outer test, deliberately not itself an async test.

    A stuck event loop cannot be detected from a coroutine running ON that
    same loop -- if _workspace_call regressed to calling execute()
    synchronously, the loop would be wedged inside the fake manager's
    threading.Event.wait() and no other coroutine (an async sleep, a
    wait_for timeout, a concurrent ping) could run on it either, so an
    async-test version of this check could pass "successfully" just by
    running everything sequentially after the block clears. Running the
    whole scenario's event loop in a background OS thread and bounding it
    with a real ``Thread.join(timeout=...)`` from this outer, unaffected
    thread is what actually proves the loop stayed free.

    2026-09-22: the proof above is superseded. A 2 s join made the verdict
    depend on how fast a busy machine ran the scenario. The fake manager now
    records whether the wait call ran on an event-loop thread, which is the
    regression itself and needs no clock; and on that path it answers at
    once instead of blocking, so the loop is never wedged. The outer thread
    and its generous join remain as a backstop so any hang fails by name.
    """
    app, manager, secret = app_and_manager
    headers = {"Authorization": f"Bearer {secret}"}
    wait_call = {
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "workspace_start_job", "arguments": {"argv": ["sleep", "5"], "wait_ms": 5000}},
    }
    ping_call = {"jsonrpc": "2.0", "id": 2, "method": "ping"}
    outcome: dict = {}
    failure: list[BaseException] = []

    async def scenario() -> None:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="https://example.test") as client:
            wait_task = asyncio.create_task(
                client.post("/mcp/workspace/workspace/mcp", json=wait_call, headers=headers)
            )
            # run_in_executor hands the blocking call to a worker thread and
            # returns immediately, so a short async sleep is enough for the
            # fake manager to have set ``entered`` if the offload happened.
            # 2026-09-22: superseded -- the "short async sleep" was a 50 x
            # 20 ms poll, a bet on timing. Now waits on ``entered`` itself
            # from a helper thread (so this loop stays free), bounded at 5 s.
            await asyncio.to_thread(manager.entered.wait, 5)
            outcome["entered_before_ping"] = manager.entered.is_set()
            ping_response = await client.post("/mcp/workspace/workspace/mcp", json=ping_call, headers=headers)
            outcome["ping_status"] = ping_response.status_code
            outcome["ping_result"] = ping_response.json().get("result")
            manager.release.set()
            wait_response = await asyncio.wait_for(wait_task, timeout=5)
            outcome["wait_status"] = wait_response.status_code

    def run() -> None:
        try:
            asyncio.run(scenario())
        except BaseException as exc:  # noqa: BLE001 - every failure, including AssertionError, must reach the outer thread
            failure.append(exc)

    worker = threading.Thread(target=run, name="gateway-wait-test-scenario")
    worker.start()
    # Backstop only (see the docstring): generous, so it never decides a
    # healthy run; it exists so a hang fails instead of freezing the suite.
    worker.join(timeout=10.0)
    if worker.is_alive():
        # The loop is wedged inside the fake manager's blocking wait(). Let
        # it drain so the thread does not leak past this test, then fail
        # loudly rather than reporting a false pass once it eventually clears.
        manager.release.set()
        worker.join(timeout=6.0)
        assert not worker.is_alive(), "the scenario thread is still running after release"
        pytest.fail(
            "the gateway wait scenario did not finish within 10s; a wait_ms "
            "workspace_start_job call may be blocking the event loop"
        )

    if failure:
        raise failure[0]
    assert manager.ran_on_event_loop is False, (
        "a wait_ms workspace_start_job call ran on the event loop thread; "
        "_workspace_call is not offloading the wait to the workspace-wait executor"
    )
    assert outcome["entered_before_ping"] is True, "the wait call never reached the blocking fake manager"
    assert outcome["ping_status"] == 200
    assert outcome["ping_result"] == {}
    assert outcome["wait_status"] == 200
