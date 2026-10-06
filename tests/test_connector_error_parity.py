from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest
from jsonschema import Draft202012Validator

from cognita.auth_policy import AuthenticationPolicyStore
from cognita.config import CognitaConfig
from cognita.connectors import ConnectorStore, PUBLIC_CONTRACT_VERSION
from cognita.engine_local import LocalEngineHost
from cognita.gateway import create_gateway_app
from cognita.registry import Project, Registry


@pytest.fixture
def connected_gateway(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    registry = Registry(tmp_path / "registry.yaml")
    registry.add(Project(name="Self-Test", documents_dir=docs, data_dir=tmp_path / "project-data"))
    connectors = ConnectorStore(tmp_path / "connectors.yaml")
    connector = connectors.create(
        expected_revision=0, name="Parity test", project_names=["Self-Test"],
    ).connectors[0]
    auth = AuthenticationPolicyStore(
        tmp_path / "authentication.yaml", project_names=["Self-Test"],
    )
    token = auth.mutate_global(
        expected_revision=0, oauth_enabled=False, static_key_action="generate",
    )["generated_key"]
    config = CognitaConfig(
        registry_path=registry.path, connectors_path=connectors.path, data_root=tmp_path,
    )
    host = LocalEngineHost(
        config, registry, SimpleNamespace(store=SimpleNamespace(pool=None)),
        connector_store=connectors,
    )
    app = create_gateway_app(
        config, registry, engine=host, connector_store=connectors,
        authentication_store=auth,
    )
    return app, connector.slug, token


@pytest.mark.asyncio
async def test_connected_get_document_errors_match_engine_code_and_schema(connected_gateway):
    app, slug, token = connected_gateway
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://fixture",
    ) as client:
        listed = await client.post(
            f"/mcp/connectors/{slug}/mcp/v{PUBLIC_CONTRACT_VERSION}",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
            headers={"Authorization": f"Bearer {token}"},
        )
        tools = listed.json()["result"]["tools"]
        schema = next(item["outputSchema"] for item in tools if item["name"] == "get_document")

        for request_id, filepath in (
            (2, "cognita-selftest-parity-missing.md"),
            (3, "unrelated-missing.md"),
        ):
            response = await client.post(
                f"/mcp/connectors/{slug}/mcp/v{PUBLIC_CONTRACT_VERSION}",
                json={
                    "jsonrpc": "2.0", "id": request_id, "method": "tools/call",
                    "params": {"name": "get_document", "arguments": {"project": "Self-Test", "filepath": filepath}},
                },
                headers={"Authorization": f"Bearer {token}"},
            )
            assert response.status_code == 200
            result = response.json()["result"]
            payload = result["structuredContent"]
            assert payload == {
                "status": "error", "reason": "not_found",
                "message": f"Document not found: {filepath}",
                "error_code": "INVALID_ARGUMENT",
            }
            assert json.loads(result["content"][0]["text"]) == payload
            assert result["isError"] is True
            Draft202012Validator(schema).validate(payload)


@pytest.mark.asyncio
async def test_generic_proxy_and_gateway_errors_receive_the_same_code(connected_gateway):
    app, slug, token = connected_gateway
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://fixture",
    ) as client:
        path = f"/mcp/connectors/{slug}/mcp/v{PUBLIC_CONTRACT_VERSION}"
        headers = {"Authorization": f"Bearer {token}"}
        listed = await client.post(
            path, json={"jsonrpc": "2.0", "id": 10, "method": "tools/list", "params": {}},
            headers=headers,
        )
        schemas = {
            tool["name"]: tool["outputSchema"]
            for tool in listed.json()["result"]["tools"]
        }
        requests = (
            (
                11, "read_document", {"project": "Self-Test", "filepath": "missing.md"},
                {
                    "status": "error", "reason": "not_found",
                    "message": "No such document: 'missing.md'.",
                    "error_code": "INVALID_ARGUMENT",
                },
            ),
            (
                12, "get_document", {"project": "unknown", "filepath": "missing.md"},
                {
                    "status": "error", "reason": "project_unavailable",
                    "message": "The requested project is unavailable through this connector.",
                    "error_code": "INVALID_ARGUMENT",
                },
            ),
        )
        for request_id, name, arguments, expected in requests:
            response = await client.post(
                path,
                json={
                    "jsonrpc": "2.0", "id": request_id, "method": "tools/call",
                    "params": {"name": name, "arguments": arguments},
                },
                headers=headers,
            )
            assert response.status_code == 200
            result = response.json()["result"]
            assert result["structuredContent"] == expected
            assert json.loads(result["content"][0]["text"]) == expected
            assert result["isError"] is True
            Draft202012Validator(schemas[name]).validate(expected)
