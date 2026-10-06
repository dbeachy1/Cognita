from __future__ import annotations

import json

import httpx
import pytest

from cognita.config import CognitaConfig
from cognita.engine_local import LocalEngineHost
from cognita.proxy import _forward_headers
from cognita.gateway import _suppress_wire_bodies
from cognita.registry import Project, Registry
from cognita.store import SchemaVersionMismatch, Store
from cognita.tokens import generate_token, hash_token
from cognita.retrieval import RetrievalCore

from retrieval_fakes import HashEmbedder, OverlapReranker
from test_book_service import _fixture


def _rpc(method: str, params=None, msg_id=1):
    return {"jsonrpc": "2.0", "id": msg_id, "method": method, "params": params or {}}


@pytest.fixture
def host(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    _fixture(docs)
    registry = Registry(tmp_path / "registry.yaml")
    registry.add(Project(
        name="fixture", documents_dir=docs, data_dir=tmp_path / "data",
        token_sha256=hash_token(generate_token()),
    ))
    store = Store("postgresql://unavailable.invalid/fixture", embedding_dimensions=32)
    # The datasource remains unopened; this models an index outage without
    # requiring a database process. Book and project-file routes must still
    # return source-side facts.
    store.schema_error = SchemaVersionMismatch("synthetic Postgres outage")
    core = RetrievalCore(store, HashEmbedder(32), OverlapReranker())
    return LocalEngineHost(CognitaConfig(), registry, core)


async def _post(host, message):
    transport = httpx.ASGITransport(app=host.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://fixture") as client:
        return await client.post("/engine/fixture/mcp", json=message)


@pytest.mark.asyncio
async def test_gateway_catalog_and_source_reads_work_during_postgres_outage(host):
    listed = await _post(host, _rpc("tools/list"))
    names = {item["name"] for item in listed.json()["result"]["tools"]}
    assert {
        "audiobook_inspect_chapter", "audiobook_prepare_chapter",
        "audiobook_get_chapter", "audiobook_find_chunk",
        "set_folder_indexing", "list_project_files", "read_project_file",
    }.issubset(names)

    response = await _post(host, _rpc("tools/call", {
        "name": "audiobook_inspect_chapter",
        "arguments": {
            "project": "fixture", "chapter_id": "ch1",
            "prose_filepath": "Chapters/1/chapter.docx",
            "tagged_filepath": "Chapters/1/chapter_audio-tags.docx",
        },
    }))
    assert response.status_code == 200
    result = response.json()["result"]
    payload = json.loads(result["content"][0]["text"])
    assert payload["status"] == "success"
    assert payload["data"]["speech_text"] == "hello"

    files = await _post(host, _rpc("tools/call", {
        "name": "list_project_files",
        "arguments": {"project": "fixture", "path": "Chapters/1"},
    }, msg_id=2))
    file_payload = json.loads(files.json()["result"]["content"][0]["text"])
    assert file_payload["status"] == "success"
    assert {item["path"] for item in file_payload["data"]["entries"]} >= {
        "Chapters/1/chapter.docx", "Chapters/1/chapter_audio-tags.docx",
    }
    assert not (host.registry.get("fixture").documents_dir / ".cognita-storage").exists()


def test_book_wire_capture_suppresses_source_and_malformed_bodies():
    for raw in (
        b'{"jsonrpc":"2.0","method":"tools/call","params":{"name":"read_project_file","arguments":{"content_base64":"c2VjcmV0"}}}',
        b'{"method":"batch","params":[{"method":"tools/call"}]}',
        b'{"jsonrpc":"2.0","method":"tools/call","params":{"name":"audiobook_import_audio"}}',
        b'{"method":"tools/call","params":{"name":"read_project_file"',
    ):
        assert _suppress_wire_bodies(raw) is True
    assert _suppress_wire_bodies(
        b'{"jsonrpc":"2.0","method":"tools/call","params":{"name":"search_knowledge"}}'
    ) is False


def test_proxy_replaces_caller_supplied_book_principal_with_authenticated_state():
    from starlette.requests import Request
    from types import SimpleNamespace

    scope = {
        "type": "http", "method": "POST", "path": "/mcp", "headers": [
            (b"x-cognita-principal-id", b"spoofed-owner"),
            (b"content-type", b"application/json"),
        ], "query_string": b"", "server": ("test", 80),
        "client": ("127.0.0.1", 1234), "scheme": "http", "http_version": "1.1",
    }
    request = Request(scope)
    request.state.cognita_principal = SimpleNamespace(principal_id="authenticated-user", key_id="key-1")
    headers = _forward_headers(request)
    assert headers["x-cognita-principal-id"] == "authenticated-user"
    assert "spoofed-owner" not in headers.values()
