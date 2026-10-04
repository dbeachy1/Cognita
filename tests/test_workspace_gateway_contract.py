"""Gateway boundary regressions for strict Workspace adapter envelopes."""

import pytest
from argon2 import PasswordHasher
from httpx import ASGITransport, AsyncClient

from cognita.auth_policy import CredentialPolicyStore
from cognita.config import CognitaConfig
from cognita.connectors import ConnectorStore, WorkspaceConnectorStore
from cognita.gateway import create_gateway_app
from cognita.registry import Project, Registry


class _WorkspaceService:
    def __init__(self):
        self.write_calls = 0

    def execute(self, _principal, tool, _arguments, *, connector_id=None):
        del connector_id
        if tool == "workspace_info":
            return {"status": "success", "workspace": None}
        if tool == "workspace_write_file":
            self.write_calls += 1
            result = {
                "status": "success", "workspace": {},
                "data": {"path": "batch.txt"},
            }
            if self.write_calls == 2:
                result["idempotent_replay"] = True
            return result
        if tool == "workspace_start_job":
            return {
                "status": "success", "workspace": {},
                "job": {"job_id": "job-1", "state": "running"},
                "idempotent_replay": True,
            }
        if tool == "workspace_get_job":
            return {
                "status": "success", "workspace": {},
                "job": {"job_id": "job-1", "state": "succeeded"},
            }
        if tool == "workspace_cancel_job":
            return {
                "status": "success", "workspace": {},
                "job": {"job_id": "job-1", "state": "canceled"},
                "idempotent_replay": True,
            }
        raise AssertionError(f"unexpected Workspace tool: {tool}")


def _call(name, arguments, message_id):
    return {
        "jsonrpc": "2.0", "id": message_id, "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    }


@pytest.mark.anyio
async def test_authenticated_gateway_dispatch_accepts_workspace_success_variants(tmp_path):
    registry = Registry(tmp_path / "registry.yaml")
    registry.add(Project(name="Knowledge", documents_dir=tmp_path, data_dir=tmp_path))
    connectors = ConnectorStore(tmp_path / "connectors.yaml")
    workspace_connectors = WorkspaceConnectorStore(tmp_path / "workspace-connectors.yaml")
    surface = workspace_connectors.create(
        expected_revision=0, display_name="Runner", enabled=True, slug="runner"
    )
    credentials = CredentialPolicyStore(
        tmp_path / "credentials-v2.json",
        master_key_dir=tmp_path / "master-keys",
        admin_password_hash=PasswordHasher().hash("admin-password"),
    )
    _row, secret = credentials.add_credential(
        "workspace", surface.id, "Runner client", surface_slug=surface.slug,
        password="admin-password",
    )
    config = CognitaConfig(
        registry_path=registry.path,
        connectors_path=connectors.path,
        public_base_url="https://example.test",
        oauth_enabled=False,
    )
    service = _WorkspaceService()
    app = create_gateway_app(
        config,
        registry,
        connector_store=connectors,
        workspace_connector_store=workspace_connectors,
        credential_store=credentials,
        workspace_service=service,
    )
    path = f"/mcp/workspace/{surface.slug}/mcp/v3"
    headers = {"Authorization": f"Bearer {secret}"}
    calls = [
        ("workspace_info", {}, 1),
        ("workspace_write_file", {"path": "batch.txt", "text": "x"}, 2),
        ("workspace_write_file", {"path": "batch.txt", "text": "x"}, 3),
        ("workspace_start_job", {"argv": ["true"]}, 4),
        ("workspace_get_job", {"job_id": "job-1"}, 5),
        ("workspace_cancel_job", {"job_id": "job-1"}, 6),
    ]
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://example.test") as client:
        responses = [
            await client.post(path, json=_call(name, arguments, message_id), headers=headers)
            for name, arguments, message_id in calls
        ]

    assert all(response.status_code == 200 for response in responses)
    results = [response.json()["result"] for response in responses]
    assert all(result["isError"] is False for result in results)
    assert results[0]["structuredContent"] == {"status": "success", "workspace": None}
    assert results[2]["structuredContent"]["idempotent_replay"] is True
    assert results[3]["structuredContent"]["job"]["state"] == "running"
    assert results[4]["structuredContent"]["job"]["state"] == "succeeded"
    assert results[5]["structuredContent"]["job"]["state"] == "canceled"
