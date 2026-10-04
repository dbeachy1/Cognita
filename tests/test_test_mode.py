"""13.0 §7.3 test mode and the built-in test key.

The contract under test, in one sentence: a public key checked into source
authenticates as a principal that can see exactly the `Self-Test` project of
one real connector, only while the process was started in test mode, and only
for thirty minutes.

Every scope assertion here runs against a connector that ALSO serves a
populated `Other-Project`, because a scope test against a server with one
project proves nothing. `Other-Project` must never appear in a response body,
and no call that names it may reach the worker, the Workspace manager or the
bridge.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from httpx import ASGITransport, AsyncClient

from cognita.admin_api import create_admin_app
from cognita.auth_policy import (
    SELF_TEST_API_KEY,
    SELF_TEST_PROJECT_NAME,
    AuthenticationPolicyStore,
    AuthPrincipal,
    SelfTestModeGate,
    self_test_principal_id,
)
from cognita.bridge import BridgeError, BridgeService
from cognita.config import CognitaConfig
from cognita.connectors import (
    PUBLIC_CONTRACT_VERSION,
    ConnectorStore,
    WorkspaceConnectorStore,
)
from cognita.gateway import create_gateway_app
from cognita.registry import Project, Registry
from cognita.workspace import (
    WorkspaceError,
    WorkspaceManager,
    WorkspaceMetadataStore,
    workspace_tool_result,
)
from engine_fakes import FakeEngineHost

OTHER_PROJECT = "Other-Project"
OTHER_SECRET = "other-project-canary-line"


class FakeRuntime:
    """Minimal broker stand-in: records every call with its workspace id."""

    def __init__(self):
        self.calls: list[tuple[str, str, dict]] = []
        self.files: dict[str, str] = {}
        # Mirror the broker's job ownership boundary: a handle must name a
        # job registered for the same Workspace. Returning success for every
        # job_get would make forged-handle tests pass through an impossible
        # broker response and hide gateway regressions.
        self.jobs: dict[tuple[str, str], str] = {}

    def call(self, workspace_id, operation, arguments, **_kwargs):
        self.calls.append((workspace_id, operation, dict(arguments)))
        if operation == "fs_write":
            self.files[arguments.get("path", "")] = arguments.get("text", "")
            return {"path": arguments.get("path"), "bytes_written": len(arguments.get("text", ""))}
        if operation == "fs_read":
            return {"content": self.files.get(arguments.get("path", ""), ""), "eof": True}
        if operation == "job_start":
            job_id = f"job-{len(self.jobs) + 1}"
            self.jobs[(workspace_id, job_id)] = "running"
            return {"job_id": job_id, "state": "running"}
        if operation in {"job_get", "job_cancel"}:
            job_id = arguments.get("job_id", "")
            state = self.jobs.get((workspace_id, job_id))
            if state is None:
                raise WorkspaceError("path_unavailable", "Workspace job was not found")
            if state == "running":
                state = "succeeded" if operation == "job_get" else "canceled"
            self.jobs[(workspace_id, job_id)] = state
            return {"state": state, "stdout": "", "stderr": ""}
        if operation == "inspect":
            return {"state": "running"}
        return {"ok": True}


@dataclass
class Env:
    app: FastAPI
    primary_slug: str
    primary_id: str
    other_only_slug: str
    workspace_slug: str
    ordinary_key: str
    seen: list[dict]
    runtime: FakeRuntime
    bridge_calls: list[tuple]
    manager: WorkspaceManager
    gate: SelfTestModeGate
    config: CognitaConfig
    registry: Registry
    connectors: ConnectorStore


class _RecordingBridge:
    """Stands in for BridgeService at the gateway seam.

    The gateway must refuse a forbidden project BEFORE the bridge is reached,
    so what this records is as important as what it returns.
    """

    def __init__(self, calls: list[tuple]):
        self.calls = calls

    async def execute(self, principal, connector, project, tool, arguments, *,
                      connector_id=None, contract_version=PUBLIC_CONTRACT_VERSION):
        name = getattr(project, "name", project)
        self.calls.append((getattr(principal, "kind", None), name, tool))
        # The shape is the bridge's own success contract
        # (`result_contracts`); a loose dict is rejected at the gateway seam.
        return {
            "status": "success",
            "transfer_id": "8f14e45f-ceea-4e3c-9c6f-2b8c2c1d0001",
            "direction": "to_workspace" if tool == "copy_to_workspace" else "from_workspace",
            "project": name, "file_count": 0, "bytes": 0,
            "manifest": [], "committed": [], "skipped": [],
        }


def _build(tmp_path, *, test_mode: bool) -> Env:
    seen: list[dict] = []
    worker = FastAPI()

    @worker.post("/mcp")
    async def mcp(request: Request):  # the fake worker every proxied call lands on
        message = await request.json()
        seen.append(message)
        if message.get("method") == "tools/call":
            payload = {"status": "success", "tool": message["params"]["name"],
                       "arguments": message["params"].get("arguments", {})}
            return JSONResponse({"jsonrpc": "2.0", "id": message.get("id"), "result": {
                "content": [{"type": "text", "text": json.dumps(payload)}],
                "isError": False,
            }})
        return JSONResponse({"jsonrpc": "2.0", "id": message.get("id"), "result": {}})

    self_docs = tmp_path / "self-test-docs"
    other_docs = tmp_path / "other-docs"
    self_docs.mkdir()
    other_docs.mkdir()
    (self_docs / "plan.md").write_text("# Self-Test\n\nplan\n", encoding="utf-8")
    (other_docs / "secret.md").write_text(f"# Other\n\n{OTHER_SECRET}\n", encoding="utf-8")

    registry = Registry(tmp_path / "registry.yaml")
    registry.add(Project(name=SELF_TEST_PROJECT_NAME, documents_dir=self_docs,
                         data_dir=tmp_path / "d-self"))
    registry.add(Project(name=OTHER_PROJECT, documents_dir=other_docs,
                         data_dir=tmp_path / "d-other"))

    auth = AuthenticationPolicyStore(
        tmp_path / "authentication.yaml",
        project_names=[SELF_TEST_PROJECT_NAME, OTHER_PROJECT],
    )
    ordinary_key = auth.mutate_global(
        expected_revision=0, oauth_enabled=False, static_key_action="generate",
    )["generated_key"]

    connectors = ConnectorStore(tmp_path / "connectors.yaml")
    primary = connectors.create(
        expected_revision=0, name="Primary",
        project_names=[SELF_TEST_PROJECT_NAME, OTHER_PROJECT],
        workspace_enabled=True, default_workspace_transfer="allow",
    ).connectors[0]
    # A second connector that does NOT serve Self-Test: the key authenticates
    # against it (it is enabled) and must then see nothing at all.
    other_only = connectors.create(
        expected_revision=1, name="Other Only", project_mode="selected",
        default_access=None, project_access={OTHER_PROJECT: "write"},
        project_names=[SELF_TEST_PROJECT_NAME, OTHER_PROJECT],
        workspace_enabled=True, default_workspace_transfer="allow",
    ).connectors[1]

    workspace_connectors = WorkspaceConnectorStore(tmp_path / "workspace-connectors.yaml")
    workspace_surface = workspace_connectors.create(
        expected_revision=0, display_name="Runner", enabled=True, slug="runner",
    )

    metadata = WorkspaceMetadataStore(tmp_path / "workspace.sqlite3")
    runtime = FakeRuntime()
    manager = WorkspaceManager(metadata, runtime)
    bridge_calls: list[tuple] = []

    config = CognitaConfig(
        registry_path=registry.path, connectors_path=connectors.path,
        data_root=tmp_path, public_base_url="https://cognita.example",
        oauth_enabled=False, self_test_mode=test_mode,
    )
    app = create_gateway_app(
        config, registry, engine=FakeEngineHost(worker), connector_store=connectors,
        authentication_store=auth, workspace_connector_store=workspace_connectors,
        workspace_service=manager, bridge_service=_RecordingBridge(bridge_calls),
    )
    return Env(
        app=app, primary_slug=primary.slug, primary_id=primary.id,
        other_only_slug=other_only.slug, workspace_slug=workspace_surface.slug,
        ordinary_key=ordinary_key, seen=seen, runtime=runtime,
        bridge_calls=bridge_calls, manager=manager,
        gate=app.state.cognita_test_mode_gate, config=config, registry=registry,
        connectors=connectors,
    )


@pytest.fixture
def normal(tmp_path):
    env = _build(tmp_path, test_mode=False)
    yield env
    env.manager.metadata.close()


@pytest.fixture
def testmode(tmp_path):
    env = _build(tmp_path, test_mode=True)
    yield env
    env.manager.metadata.close()


def _rpc(name, arguments, msg_id=1):
    return {"jsonrpc": "2.0", "id": msg_id, "method": "tools/call",
            "params": {"name": name, "arguments": arguments}}


async def _post(env: Env, path: str, payload, key: str):
    async with AsyncClient(transport=ASGITransport(app=env.app),
                           base_url="https://cognita.example") as client:
        return await client.post(path, json=payload,
                                 headers={"Authorization": f"Bearer {key}"})


def _combined(env: Env, *, version: int | None = PUBLIC_CONTRACT_VERSION,
              slug: str | None = None) -> str:
    slug = slug or env.primary_slug
    if version is None:
        return f"/mcp/connectors/{slug}/mcp"
    return f"/mcp/connectors/{slug}/mcp/v{version}"


def _payload(response) -> dict:
    result = response.json()["result"]
    if "structuredContent" in result:
        return result["structuredContent"]
    return json.loads(result["content"][0]["text"])


# --- the key outside test mode ---------------------------------------------


async def test_the_key_is_401_on_every_route_in_normal_mode(normal):
    """Stable, versioned, Workspace-only, Admin, OAuth, retired and future."""
    live_routes = [
        _combined(normal, version=None),
        _combined(normal),
        _combined(normal, slug=normal.other_only_slug),
        f"/mcp/workspace/{normal.workspace_slug}/mcp",
        f"/mcp/workspace/{normal.workspace_slug}/mcp/v3",
    ]
    for path in live_routes:
        response = await _post(normal, path, _rpc("list_projects", {}), SELF_TEST_API_KEY)
        assert response.status_code == 401, path

    # Retired and future generations fail closed on the route predicate, before
    # authentication runs at all, so they answer 404 and never disclose whether
    # the credential would have been accepted.
    for path in (_combined(normal, version=3), _combined(normal, version=99)):
        response = await _post(normal, path, _rpc("list_projects", {}), SELF_TEST_API_KEY)
        assert response.status_code == 404, path

    async with AsyncClient(transport=ASGITransport(app=normal.app),
                           base_url="https://cognita.example") as client:
        oauth = [
            await client.post("/oauth/token", json={},
                              headers={"Authorization": f"Bearer {SELF_TEST_API_KEY}"}),
            await client.post("/oauth/register", json={},
                              headers={"Authorization": f"Bearer {SELF_TEST_API_KEY}"}),
            await client.get("/.well-known/oauth-authorization-server",
                             headers={"Authorization": f"Bearer {SELF_TEST_API_KEY}"}),
        ]
    assert [item.status_code for item in oauth] == [404, 404, 404]

    admin = create_admin_app(normal.config, normal.registry)
    async with AsyncClient(transport=ASGITransport(app=admin),
                           base_url="https://cognita.example") as client:
        for route in ("/api/state", "/api/connectors", "/api/workspaces"):
            response = await client.get(
                route, headers={"Authorization": f"Bearer {SELF_TEST_API_KEY}"})
            assert response.status_code != 200, route


async def test_the_key_is_401_even_when_it_is_stored_as_a_project_token(normal, tmp_path):
    """Nothing written to a credential file can promote the key."""
    from cognita.tokens import hash_token

    normal.registry.add(Project(
        name="Planted", documents_dir=tmp_path / "self-test-docs",
        data_dir=tmp_path / "d-planted", token_sha256=hash_token(SELF_TEST_API_KEY),
    ))
    response = await _post(normal, _combined(normal), _rpc("list_projects", {}),
                           SELF_TEST_API_KEY)
    assert response.status_code == 401


async def test_healthz_reports_test_mode_false_in_normal_mode(normal):
    async with AsyncClient(transport=ASGITransport(app=normal.app),
                           base_url="https://cognita.example") as client:
        health = (await client.get("/healthz")).json()
    assert health["test_mode"] is False
    assert health["index"] == {"status": "ok"}


# --- the key in test mode ---------------------------------------------------


async def test_healthz_reports_test_mode_true_in_test_mode(testmode):
    async with AsyncClient(transport=ASGITransport(app=testmode.app),
                           base_url="https://cognita.example") as client:
        health = (await client.get("/healthz")).json()
    assert health["test_mode"] is True


async def test_discovery_shows_only_self_test(testmode):
    for path in (_combined(testmode, version=None), _combined(testmode)):
        response = await _post(testmode, path, _rpc("list_projects", {}), SELF_TEST_API_KEY)
        assert response.status_code == 200, path
        payload = _payload(response)
        assert [item["name"] for item in payload["projects"]] == [SELF_TEST_PROJECT_NAME]
        assert OTHER_PROJECT not in response.text

    # The ordinary credential still sees both: the scoping belongs to the
    # test principal, not to the connector.
    ordinary = await _post(testmode, _combined(testmode), _rpc("list_projects", {}),
                           testmode.ordinary_key)
    assert sorted(item["name"] for item in _payload(ordinary)["projects"]) == [
        OTHER_PROJECT, SELF_TEST_PROJECT_NAME,
    ]


async def test_a_direct_call_naming_another_project_is_refused_and_never_dispatched(testmode):
    response = await _post(
        testmode, _combined(testmode),
        _rpc("search_knowledge", {"project": OTHER_PROJECT, "query": "x"}),
        SELF_TEST_API_KEY,
    )
    assert response.status_code == 200
    assert _payload(response)["reason"] == "project_unavailable"
    assert OTHER_PROJECT not in response.text
    assert testmode.seen == []


async def test_self_test_calls_are_dispatched(testmode):
    response = await _post(
        testmode, _combined(testmode),
        _rpc("search_knowledge", {"project": SELF_TEST_PROJECT_NAME, "query": "x"}),
        SELF_TEST_API_KEY,
    )
    assert response.status_code == 200
    assert _payload(response)["status"] == "success"
    assert testmode.seen and testmode.seen[0]["params"]["name"] == "search_knowledge"


async def test_an_omitted_project_means_self_test(testmode):
    response = await _post(
        testmode, _combined(testmode), _rpc("search_knowledge", {"query": "x"}),
        SELF_TEST_API_KEY,
    )
    assert response.status_code == 200
    assert _payload(response)["status"] == "success"
    # Inference is what proves the principal has exactly one accessible
    # project: with two, the call would have been rejected for naming none.
    assert testmode.seen[0]["params"]["arguments"].get("project") in {
        None, SELF_TEST_PROJECT_NAME,
    }
    assert OTHER_PROJECT not in response.text


async def test_batch_children_are_checked_one_by_one(testmode):
    response = await _post(
        testmode, _combined(testmode),
        _rpc("batch", {"calls": [
            {"tool": "search_knowledge",
             "arguments": {"project": SELF_TEST_PROJECT_NAME, "query": "a"}},
            {"tool": "search_knowledge",
             "arguments": {"project": OTHER_PROJECT, "query": "b"}},
            {"tool": "search_knowledge",
             "arguments": {"project": SELF_TEST_PROJECT_NAME, "query": "c"}},
        ], "on_error": "continue"}),
        SELF_TEST_API_KEY,
    )
    assert response.status_code == 200
    payload = _payload(response)
    statuses = [item["status"] for item in payload["results"]]
    assert statuses == ["success", "error", "success"]
    assert "project_unavailable" in json.dumps(payload["results"][1])
    assert OTHER_PROJECT not in response.text
    dispatched = [item["params"]["arguments"].get("query") for item in testmode.seen]
    assert dispatched == ["a", "c"]


async def test_a_nested_project_selector_is_refused(testmode):
    response = await _post(
        testmode, _combined(testmode),
        _rpc("search_knowledge", {
            "project": SELF_TEST_PROJECT_NAME,
            "filters": {"project": OTHER_PROJECT},
        }),
        SELF_TEST_API_KEY,
    )
    assert response.status_code == 200
    assert OTHER_PROJECT not in response.text
    assert testmode.seen == []


async def test_a_bridge_call_for_another_project_never_reaches_the_bridge(testmode):
    for tool in ("copy_to_workspace", "copy_from_workspace"):
        response = await _post(
            testmode, _combined(testmode),
            _rpc(tool, {"project": OTHER_PROJECT, "paths": ["a.md"]}),
            SELF_TEST_API_KEY,
        )
        assert response.status_code == 200
        assert _payload(response)["reason"] == "project_unavailable"
        assert OTHER_PROJECT not in response.text
    assert testmode.bridge_calls == []


async def test_both_bridge_directions_work_for_self_test(testmode):
    for tool in ("copy_to_workspace", "copy_from_workspace"):
        response = await _post(
            testmode, _combined(testmode),
            _rpc(tool, {"project": SELF_TEST_PROJECT_NAME, "paths": ["plan.md"]}),
            SELF_TEST_API_KEY,
        )
        assert response.status_code == 200
        assert _payload(response)["status"] == "success"
    assert [item[1] for item in testmode.bridge_calls] == [
        SELF_TEST_PROJECT_NAME, SELF_TEST_PROJECT_NAME,
    ]
    assert {item[0] for item in testmode.bridge_calls} == {"self_test"}


async def test_the_principals_own_workspace_executes_reads_and_writes(testmode):
    expected_id = self_test_principal_id(testmode.primary_id)
    write = await _post(
        testmode, _combined(testmode),
        _rpc("workspace_write_file", {"path": "notes.txt", "text": "hello"}),
        SELF_TEST_API_KEY,
    )
    assert _payload(write)["status"] == "success"
    read = await _post(
        testmode, _combined(testmode),
        _rpc("workspace_read_file", {"path": "notes.txt", "offset": 0,
                                     "max_bytes": 1024, "encoding": "text"}),
        SELF_TEST_API_KEY,
    )
    assert _payload(read)["status"] == "success"
    job = await _post(
        testmode, _combined(testmode),
        _rpc("workspace_start_job", {"argv": ["true"]}),
        SELF_TEST_API_KEY,
    )
    assert _payload(job)["status"] == "success"
    job_id = _payload(job)["job"]["job_id"]
    for tool in ("workspace_get_job", "workspace_cancel_job"):
        completed = await _post(
            testmode, _combined(testmode), _rpc(tool, {"job_id": job_id}), SELF_TEST_API_KEY,
        )
        assert _payload(completed)["status"] == "success"
        assert _payload(completed)["job"]["state"] == "succeeded"

    record = testmode.manager.metadata.get_by_principal(expected_id)
    assert record is not None
    # Everything ran inside that one Workspace, and its ID is derivable, so a
    # Workspace left behind by a killed run is found again rather than orphaned.
    assert {call[0] for call in testmode.runtime.calls} == {record.workspace_id}
    assert self_test_principal_id(testmode.primary_id) == expected_id


@pytest.mark.parametrize("tool", ["workspace_get_job", "workspace_cancel_job"])
@pytest.mark.parametrize("handle_source", ["unknown", "other_workspace"])
async def test_a_forged_workspace_or_job_handle_selects_nothing(testmode, tool, handle_source):
    forged_workspace = "ffffffff-ffff-4fff-8fff-ffffffffffff"
    forged_job_id = "job-from-somewhere-else"
    if handle_source == "other_workspace":
        forged_job_id = testmode.runtime.call(
            forged_workspace, "job_start", {"argv": ["true"]},
        )["job_id"]
        # Seeding a foreign job is setup, not a gateway call. Everything
        # observed below must still target only the authenticated Workspace.
        testmode.runtime.calls.clear()
    bad = await _post(
        testmode, _combined(testmode),
        _rpc("workspace_read_file", {"path": "notes.txt", "workspace_id": forged_workspace,
                                     "offset": 0, "max_bytes": 16, "encoding": "text"}),
        SELF_TEST_API_KEY,
    )
    assert _payload(bad)["reason"] == "invalid_arguments"

    own_job = await _post(testmode, _combined(testmode),
                         _rpc("workspace_start_job", {"argv": ["true"]}), SELF_TEST_API_KEY)
    assert _payload(own_job)["status"] == "success"
    assert _payload(own_job)["job"]["job_id"] != forged_job_id
    forged_job = await _post(
        testmode, _combined(testmode),
        _rpc(tool, {"job_id": forged_job_id}),
        SELF_TEST_API_KEY,
    )
    # A job handle names a job WITHIN the caller's own Workspace: one this
    # principal never started is simply not there, and it can never select
    # another Workspace.
    assert _payload(forged_job)["status"] == "error"
    assert _payload(forged_job)["reason"] == "path_unavailable"
    record = testmode.manager.metadata.get_by_principal(
        self_test_principal_id(testmode.primary_id))
    assert {call[0] for call in testmode.runtime.calls} == {record.workspace_id}
    if handle_source == "other_workspace":
        assert testmode.runtime.jobs[(forged_workspace, forged_job_id)] == "running"


async def test_the_principals_own_job_can_be_canceled_and_stays_terminal(testmode):
    started = await _post(
        testmode, _combined(testmode), _rpc("workspace_start_job", {"argv": ["true"]}),
        SELF_TEST_API_KEY,
    )
    assert _payload(started)["status"] == "success"
    job_id = _payload(started)["job"]["job_id"]
    for tool in ("workspace_cancel_job", "workspace_get_job"):
        result = await _post(
            testmode, _combined(testmode), _rpc(tool, {"job_id": job_id}), SELF_TEST_API_KEY,
        )
        assert _payload(result)["status"] == "success"
        assert _payload(result)["job"]["state"] == "canceled"


async def test_a_connector_without_self_test_gets_nothing(testmode):
    listed = await _post(
        testmode, _combined(testmode, slug=testmode.other_only_slug),
        _rpc("list_projects", {}), SELF_TEST_API_KEY,
    )
    assert listed.status_code == 200
    assert _payload(listed)["projects"] == []
    assert OTHER_PROJECT not in listed.text

    call = await _post(
        testmode, _combined(testmode, slug=testmode.other_only_slug),
        _rpc("search_knowledge", {"project": OTHER_PROJECT, "query": "x"}),
        SELF_TEST_API_KEY,
    )
    assert _payload(call)["reason"] in {"project_unavailable", "no_projects_configured"}
    assert OTHER_PROJECT not in call.text

    workspace = await _post(
        testmode, _combined(testmode, slug=testmode.other_only_slug),
        _rpc("workspace_write_file", {"path": "x.txt", "text": "x"}),
        SELF_TEST_API_KEY,
    )
    assert _payload(workspace)["reason"] == "project_unavailable"
    assert testmode.runtime.calls == []


async def test_self_test_is_reachable_even_when_excluded_from_default_permissions(
    testmode,
):
    """The exclusion keeps a project away from OAuth and global keys.

    The built-in key is neither: it is an explicit single-project grant, so it
    still reaches Self-Test — and still nothing else.
    """
    testmode.registry.update_settings(
        SELF_TEST_PROJECT_NAME, exclude_from_default_permissions=True,
    )
    listed = await _post(testmode, _combined(testmode), _rpc("list_projects", {}),
                         SELF_TEST_API_KEY)
    assert [item["name"] for item in _payload(listed)["projects"]] == [
        SELF_TEST_PROJECT_NAME,
    ]


async def test_a_disabled_connector_does_not_admit_the_key(testmode):
    snapshot = testmode.connectors.snapshot()
    testmode.connectors.update(
        testmode.primary_id, expected_revision=snapshot.revision,
        project_names=[SELF_TEST_PROJECT_NAME, OTHER_PROJECT], enabled=False,
    )
    response = await _post(testmode, _combined(testmode), _rpc("list_projects", {}),
                           SELF_TEST_API_KEY)
    assert response.status_code == 401


async def test_the_key_is_still_refused_on_the_workspace_only_route_in_test_mode(testmode):
    for path in (f"/mcp/workspace/{testmode.workspace_slug}/mcp",
                 f"/mcp/workspace/{testmode.workspace_slug}/mcp/v3"):
        response = await _post(testmode, path, _rpc("workspace_info", {}),
                               SELF_TEST_API_KEY)
        assert response.status_code == 401, path


# --- expiry -----------------------------------------------------------------


async def test_the_key_is_rejected_after_thirty_minutes(testmode):
    gate = testmode.gate
    now = [0.0]
    # Drive the gate from a fake monotonic clock. `started_at` was taken from
    # the real clock when the app was built, so it is re-based here; both
    # values belong to the same clock afterwards.
    gate.clock = lambda: now[0]
    gate.started_at = 0.0

    async with AsyncClient(transport=ASGITransport(app=testmode.app),
                           base_url="https://cognita.example") as client:
        headers = {"Authorization": f"Bearer {SELF_TEST_API_KEY}"}
        path = _combined(testmode)
        initialized = await client.post(path, json={
            "jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {},
        }, headers=headers)
        assert initialized.status_code == 200

        now[0] = 1799.0
        before = await client.post(path, json=_rpc("list_projects", {}), headers=headers)
        assert before.status_code == 200
        assert (await client.get("/healthz")).json()["test_mode"] is True

        now[0] = 1800.0
        after = await client.post(path, json=_rpc("list_projects", {}), headers=headers)
        assert after.status_code == 401
        assert (await client.get("/healthz")).json()["test_mode"] is False

        # The ordinary credential is untouched by expiry.
        ordinary = await client.post(
            path, json=_rpc("list_projects", {}),
            headers={"Authorization": f"Bearer {testmode.ordinary_key}"},
        )
        assert ordinary.status_code == 200


def test_the_gate_never_opens_outside_test_mode():
    gate = SelfTestModeGate(False, clock=lambda: 0.0)
    assert gate.active() is False
    assert gate.remaining_seconds() == 0.0


# --- the ordinary principal is unaffected -----------------------------------


@pytest.mark.parametrize("mode", ["normal", "testmode"])
async def test_an_ordinary_principal_is_unaffected(request, mode):
    env: Env = request.getfixturevalue(mode)
    listed = await _post(env, _combined(env), _rpc("list_projects", {}), env.ordinary_key)
    assert sorted(item["name"] for item in _payload(listed)["projects"]) == [
        OTHER_PROJECT, SELF_TEST_PROJECT_NAME,
    ]
    call = await _post(
        env, _combined(env),
        _rpc("search_knowledge", {"project": OTHER_PROJECT, "query": "x"}),
        env.ordinary_key,
    )
    assert _payload(call)["status"] == "success"
    # The exact project is stripped before the worker sees it (9.0 routing), so
    # the evidence that this call was admitted is that it was dispatched.
    assert env.seen[-1]["params"]["name"] == "search_knowledge"


# --- the shared owners, called directly -------------------------------------


def _self_test_principal(connector_id: str) -> AuthPrincipal:
    return AuthPrincipal(
        kind="self_test", principal_id=self_test_principal_id(connector_id),
        surface_kind="combined", surface_id=connector_id,
    )


def test_the_bridge_refuses_another_project_and_another_connectors_handle(tmp_path):
    connectors = ConnectorStore(tmp_path / "connectors.yaml")
    connector = connectors.create(
        expected_revision=0, name="Primary",
        project_names=[SELF_TEST_PROJECT_NAME, OTHER_PROJECT],
        workspace_enabled=True, default_workspace_transfer="allow",
    ).connectors[0]
    service = BridgeService(object(), transfer_client=object(),
                            staging_root=tmp_path / "staging")
    principal = _self_test_principal(connector.id)
    self_test = Project(name=SELF_TEST_PROJECT_NAME, documents_dir=tmp_path,
                        data_dir=tmp_path / "d1")
    other = Project(name=OTHER_PROJECT, documents_dir=tmp_path, data_dir=tmp_path / "d2")

    for direction in ("to_workspace", "from_workspace"):
        service._check_policy(principal, connector, self_test, direction)
        with pytest.raises(BridgeError) as forbidden:
            service._check_policy(principal, connector, other, direction)
        assert forbidden.value.reason == "project_forbidden"

        foreign = _self_test_principal("00000000-0000-4000-8000-000000000000")
        with pytest.raises(BridgeError) as wrong_connector:
            service._check_policy(foreign, connector, self_test, direction)
        assert wrong_connector.value.reason == "project_forbidden"


def test_the_workspace_adapter_refuses_another_connectors_handle(tmp_path):
    metadata = WorkspaceMetadataStore(tmp_path / "workspace.sqlite3")
    try:
        manager = WorkspaceManager(metadata, FakeRuntime())
        connector_id = "11111111-1111-4111-8111-111111111111"
        principal = _self_test_principal(connector_id)
        ok = workspace_tool_result(manager, principal, "workspace_write_file",
                                   {"path": "a.txt", "text": "x"},
                                   connector_id=connector_id)
        assert ok["status"] == "success"

        forged = workspace_tool_result(
            manager, principal, "workspace_write_file",
            {"path": "a.txt", "text": "x"},
            connector_id="22222222-2222-4222-8222-222222222222",
        )
        assert forged["status"] == "error"
        assert forged["reason"] == "unauthorized"
    finally:
        metadata.close()


def test_a_non_self_test_principal_still_needs_a_uuid4(tmp_path):
    """The UUIDv5 exception is recomputed, never a general relaxation."""
    metadata = WorkspaceMetadataStore(tmp_path / "workspace.sqlite3")
    try:
        manager = WorkspaceManager(metadata, FakeRuntime())

        class _Principal:
            principal_id = self_test_principal_id("some-connector")
            surface_id = "another-connector"

        with pytest.raises(WorkspaceError) as refused:
            manager.execute(_Principal(), "workspace_write_file",
                            {"path": "a.txt", "text": "x"},
                            connector_id="another-connector")
        assert refused.value.reason == "invalid_arguments"
    finally:
        metadata.close()


def test_the_principal_id_is_deterministic_per_connector():
    first = self_test_principal_id("connector-a")
    assert first == self_test_principal_id("connector-a")
    assert first != self_test_principal_id("connector-b")
    assert len(first) == 36


def test_the_key_is_the_documented_public_constant():
    # AGENTS.md: this value is public test material and is never rotated as
    # "cleanup". A change here is a change to the release script's contract.
    assert SELF_TEST_API_KEY == "cognita-self-test-only-v1"
