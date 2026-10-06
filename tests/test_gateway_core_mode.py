"""End-to-end tests of the gateway in 4.0 core mode (engine=LocalEngineHost).

The full production path — bearer auth → token routing → connector/project
policy → proxy.py mutation guards → in-process ASGI →
retrieval core → PostgreSQL — with the deterministic fakes for models.
Activated by COGNITA_TEST_PG_DSN.

This is the M3 claim under test: proxy.py runs UNCHANGED against the local
engine, so everything the 3.x gateway guaranteed still holds without workers.
"""
from cognita.connectors import PUBLIC_CONTRACT_VERSION

import json
import os
import uuid

import pytest
from httpx import ASGITransport, AsyncClient

from cognita.auth_policy import AuthenticationPolicyStore
from cognita.config import CognitaConfig
from cognita.connectors import ConnectorStore
from cognita.engine_local import LocalEngineHost
from cognita.gateway import create_gateway_app
from cognita.registry import Project, Registry
from cognita.retrieval import RetrievalCore
from cognita.store import Store
from retrieval_fakes import HashEmbedder, OverlapReranker

DSN = os.environ.get("COGNITA_TEST_PG_DSN", "")
pytestmark = pytest.mark.skipif(not DSN, reason="COGNITA_TEST_PG_DSN not set")

DIMS = 32


@pytest.fixture
async def env(tmp_path):
    """Gateway app in core mode with one writable and one read-only project."""
    rw_name = f"T{uuid.uuid4().hex[:10]}"
    ro_name = f"T{uuid.uuid4().hex[:10]}"
    rw_docs = tmp_path / "rw_docs"
    rw_docs.mkdir()
    (rw_docs / "guide.md").write_text(
        "# Guide\n\nHow to configure the pcie riser for the gpu build.", encoding="utf-8"
    )
    ro_docs = tmp_path / "ro_docs"
    ro_docs.mkdir()
    (ro_docs / "notes.md").write_text("# Notes\n\nread-only project notes", encoding="utf-8")

    registry = Registry(tmp_path / "registry.yaml")
    registry.add(Project(name=rw_name, documents_dir=rw_docs, data_dir=tmp_path / "d1"))
    registry.add(Project(name=ro_name, documents_dir=ro_docs, data_dir=tmp_path / "d2",
                         writable=False))
    # 13.0 §7.3: the `config.test_mode` registry-token fallback this fixture
    # authenticated through is deleted. It now authenticates the way production
    # does, with project-scoped keys from the parent-owned policy store — one
    # per project, because the helpers below map a credential to the project it
    # may call. OAuth is off so an unknown bearer stays a 401 rather than a 503
    # from the absent OAuth child, which `test_auth_still_enforced` asserts.
    #
    # This file is PG-gated, so it skips on Windows: the conversion was proven
    # against a real pgvector container on kei (2026-09-22), where the fallback
    # deletion had otherwise left 11 of these failing on a non-JSON 401 body.
    auth = AuthenticationPolicyStore(
        tmp_path / "authentication.yaml", project_names=[rw_name, ro_name]
    )
    auth.mutate_global(expected_revision=0, oauth_enabled=False, static_key_action="generate")
    rw_token = auth.mutate_project(
        rw_name, expected_revision=1, static_key_action="generate")["generated_key"]
    ro_token = auth.mutate_project(
        ro_name, expected_revision=2, static_key_action="generate")["generated_key"]

    config = CognitaConfig(registry_path=tmp_path / "registry.yaml", data_root=tmp_path)
    connector_store = ConnectorStore(tmp_path / "connectors.yaml")
    connector_config = connector_store.create(
        expected_revision=0,
        name="Core Test",
        # Keep both joins explicit: v5 mutation calls must exercise the
        # connector's writable project grant, while the second project is a
        # deliberately read-only override.
        project_access={rw_name: "write", ro_name: "read"},
        project_names=[rw_name, ro_name],
    )
    store = Store(DSN, embedding_dimensions=DIMS)
    core = RetrievalCore(store, HashEmbedder(DIMS), OverlapReranker())
    # The engine performs a trusted connector-policy recheck at write
    # admission. Use the same fixture-backed policy store as the gateway;
    # otherwise the engine falls back to config.connectors_path and rejects
    # every otherwise-authorized mutation as project_unavailable.
    engine = LocalEngineHost(config, registry, core, connector_store=connector_store)
    await store.connect()
    for name, docs in ((rw_name, rw_docs), (ro_name, ro_docs)):
        await store.ensure_project(name)
        await core.index_project(name, docs)

    app = create_gateway_app(
        config, registry, engine=engine, connector_store=connector_store,
        authentication_store=auth,
    )
    app.state.test_connector_slug = connector_config.connectors[0].slug
    app.state.test_project_for_token = {rw_token: rw_name, ro_token: ro_name}
    yield app, engine, rw_name, rw_token, ro_token, rw_docs
    await store.drop_project(rw_name)
    await store.drop_project(ro_name)
    await store.close()


def rpc(method, params=None, msg_id=1):
    return {"jsonrpc": "2.0", "id": msg_id, "method": method, "params": params or {}}


async def post(app, token, message):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        path = f"/mcp/connectors/{app.state.test_connector_slug}/mcp/v{PUBLIC_CONTRACT_VERSION}"
        return await client.post(path, json=message,
                                 headers={"Authorization": f"Bearer {token}"})


async def call(app, token, tool, arguments=None):
    call_arguments = dict(arguments or {})
    # v5 no longer infers a project when a connector can reach more than one;
    # every tool call must carry the exact project identity declared by its
    # public input schema.
    call_arguments.setdefault("project", app.state.test_project_for_token[token])
    r = await post(app, token, rpc("tools/call", {"name": tool, "arguments": call_arguments}))
    assert r.status_code == 200, r.text
    body = r.json()
    assert "error" not in body, body
    return json.loads(body["result"]["content"][0]["text"])


async def test_auth_still_enforced(env):
    app, *_ = env
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        path = f"/mcp/connectors/{app.state.test_connector_slug}/mcp/v{PUBLIC_CONTRACT_VERSION}"
        r = await client.post(path, json=rpc("tools/list"))
    assert r.status_code == 401


async def test_tools_list_declares_project_routing_and_augmented(env):
    app, _, rw_name, rw_token, _, rw_docs = env
    r = await post(app, rw_token, rpc("tools/list"))
    tools = r.json()["result"]["tools"]
    names = {t["name"] for t in tools}
    # engine tools + every gateway tool on a writable project
    assert {"search_knowledge", "add_document", "reindex_documents"} <= names
    assert {"read_document", "edit_document", "edit_document_batch", "insert_in_document",
            "list_backups", "diff_backup", "restore_backup", "get_self_test_plan"} <= names
    assert "find_literal" in names  # 4.5
    # v5 publishes one connector-scoped, project-neutral catalog.  Project
    # identity is an exact declared argument on calls, not an implicit prefix
    # stamped into descriptions.
    # Workspace tools are principal-scoped and take no `project` argument
    # (same exclusion as the catalog tests, d5f4046). This assertion only held
    # while 13.0.0 hid them by default; 13.0.1 shipped without running the
    # PG-gated suite, so it first failed on the 13.0.2 `deploy --test`.
    assert all(
        "project" in tool["inputSchema"].get("properties", {})
        for tool in tools
        if tool["name"] not in {"batch", "list_projects"} and not tool["name"].startswith("workspace_")
    )
    assert not any(rw_name in tool["description"] for tool in tools)


async def test_readonly_project_filtered_and_blocked(env):
    app, _, _, _, ro_token, _ = env
    r = await post(app, ro_token, rpc("tools/list"))
    names = {t["name"] for t in r.json()["result"]["tools"]}
    # The current catalog is connector-scoped and therefore identical for
    # writable/read-only projects.  Access is enforced when the call resolves
    # the exact project, not by silently changing tools/list.
    assert "add_document" in names and "edit_document" in names
    assert "search_knowledge" in names and "read_document" in names
    # 4.5: find_literal is read-only by construction and remains callable on a
    # read-only project — an exhaustive "where did I write this?" sweep is the
    # only tool available there.
    assert "find_literal" in names

    blocked = await call(app, ro_token, "add_document", {
        "content": "x", "filepath": "x.md",
    })
    assert blocked["reason"] == "read_only"


async def test_search_through_gateway(env):
    app, _, _, rw_token, _, rw_docs = env
    payload = await call(app, rw_token, "search_knowledge", {"query": "pcie riser gpu"})
    assert payload["status"] == "success"
    assert payload["results"][0]["source"] == str(rw_docs / "guide.md")


async def test_find_literal_through_gateway(env):
    """4.5 end-to-end over the real remote surface, including the read-only
    project — the allow-list filter is the one place this could silently
    vanish, and a dropped tool looks exactly like 'the string is not there'."""
    app, _, _, rw_token, ro_token, rw_docs = env

    payload = await call(app, rw_token, "find_literal", {"pattern": "pcie riser"})
    assert payload["status"] == "success"
    assert payload["total_matches"] == 1
    hit = payload["matches"][0]
    assert hit["filepath"] == "guide.md"
    assert hit["source"] == str(rw_docs / "guide.md")
    assert hit["line_number"] == 3 and hit["tier"] == "embedded"

    ro = await call(app, ro_token, "find_literal", {"pattern": "read-only project"})
    assert ro["total_matches"] == 1
    assert ro["matches"][0]["filepath"] == "notes.md"

    absent = await call(app, ro_token, "find_literal", {"pattern": "zz_nowhere"})
    assert absent["status"] == "success" and absent["total_matches"] == 0


async def test_edit_document_end_to_end(env):
    """The important one: proxy.py's anchored edit -> update_document transform
    lands on the LOCAL engine, which writes the file and reindexes it."""
    app, _, rw_name, rw_token, _, rw_docs = env
    edit = await call(app, rw_token, "edit_document", {
        "filepath": "guide.md",
        "old_str": "pcie riser",
        "new_str": "PCIe Gen5 riser",
    })
    assert edit["status"] == "success", edit
    assert edit["replacements"] == 1
    assert "new_content_sha256" in edit and "context_diff" in edit
    # the file actually changed on disk...
    assert "PCIe Gen5 riser" in (rw_docs / "guide.md").read_text(encoding="utf-8")
    # ...a backup of the pre-edit state exists (mandatory-backup invariant)...
    backups = list((rw_docs / "backups").rglob("guide.*"))
    assert backups, "edit must snapshot the file before writing"
    # ...and the change is immediately searchable (reindexed through the core)
    hits = await call(app, rw_token, "search_knowledge",
                      {"query": "Gen5 riser", "hybrid_alpha": 0.0, "snippet_mode": False})
    assert hits["status"] == "success"
    assert "PCIe Gen5 riser" in hits["results"][0]["content"]


async def test_read_document_and_selftest_served_by_gateway(env):
    app, _, _, rw_token, _, _ = env
    doc = await call(app, rw_token, "read_document", {"filepath": "guide.md"})
    assert doc["status"] == "success"
    assert "content_sha256" in doc

    plan = await call(app, rw_token, "get_self_test_plan")
    assert plan["status"] == "success"
    assert "SELF-TEST PLAN" in plan["plan"]  # the plan is prose for the chat model


async def test_remove_document_backs_up_first(env):
    app, _, _, rw_token, _, rw_docs = env
    added = await call(app, rw_token, "add_document",
                       {"content": "# Temp\n\nto be removed shortly", "filepath": "temp.md"})
    assert added["status"] == "success"
    removed = await call(app, rw_token, "remove_document",
                         {"filepath": "temp.md", "delete_file": True})
    assert removed["status"] == "success" and removed["file_deleted"] is True
    assert not (rw_docs / "temp.md").exists()
    assert list((rw_docs / "backups").rglob("temp.*")), "remove must back up first"


async def test_remove_documents_replays_the_completed_partial_result(env):
    app, _, _, rw_token, _, rw_docs = env
    added = await call(app, rw_token, "add_document", {
        "filepath": "bulk-replay.md", "content": "# Replay\n\nowned fixture",
    })
    assert added["status"] == "success"
    arguments = {
        "filepaths": ["bulk-replay.md", "bulk-missing.md"],
        "delete_file": True, "on_error": "continue", "operation_id": "remove-op-1",
    }
    first = await call(app, rw_token, "remove_documents", arguments)
    second = await call(app, rw_token, "remove_documents", arguments)
    assert first["status"] == "partial_failure"
    assert second["replayed"] is True
    assert second["documents"] == first["documents"]
    assert len(list((rw_docs / "backups").rglob("bulk-replay.*"))) == 1


async def test_move_document_backs_up_and_reindexes(env):
    """4.1: move through the gateway — the source is snapshotted first, the file
    relocates, and it's searchable at the new path via the in-process core."""
    app, _, _, rw_token, _, rw_docs = env
    moved = await call(app, rw_token, "move_document",
                       {"filepath": "guide.md", "new_filepath": "docs/guide-renamed.md"})
    assert moved["status"] == "success"
    assert not (rw_docs / "guide.md").exists()
    assert (rw_docs / "docs" / "guide-renamed.md").is_file()
    # mandatory backup of the source (house rule, enforced by the gateway)
    assert list((rw_docs / "backups").rglob("guide.*")), "move must back up the source first"
    # searchable at the new path
    hits = await call(app, rw_token, "search_knowledge",
                      {"query": "pcie riser gpu", "hybrid_alpha": 0.0, "snippet_mode": False})
    assert any("guide-renamed.md" in r["source"] for r in hits["results"])


async def test_move_document_advertised_and_readonly_blocked(env):
    app, _, _, rw_token, ro_token, _ = env
    # writable project: tool is advertised
    r = await post(app, rw_token, rpc("tools/list"))
    assert "move_document" in {t["name"] for t in r.json()["result"]["tools"]}
    # read-only project: still advertised in the connector-scoped catalog, and
    # a call is rejected by project access policy.
    r = await post(app, ro_token, rpc("tools/list"))
    assert "move_document" in {t["name"] for t in r.json()["result"]["tools"]}
    blocked = await call(app, ro_token, "move_document", {
        "filepath": "notes.md", "new_filepath": "x.md",
    })
    assert blocked["reason"] == "read_only"


async def test_initialize_handshake_through_gateway(env):
    app, _, _, rw_token, _, _ = env
    r = await post(app, rw_token, rpc("initialize", {
        "protocolVersion": "2025-03-26", "capabilities": {},
        "clientInfo": {"name": "claude", "version": "1"}}))
    result = r.json()["result"]
    assert result["serverInfo"]["name"] == "Cognita"
    assert result["protocolVersion"] == "2025-03-26"
