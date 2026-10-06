"""edit_document through the gateway (DESIGN-2.0-edit-document.md §7 #13-18).

A fake ASGI worker stands in for the retrieval engine: it records every
JSON-RPC message it receives and answers update_document/tools/list with canned
engine responses. The gateway reaches it through tests/engine_fakes.py's
FakeEngineHost, whose httpx client is an ASGITransport so no sockets are
involved. (Before 14.0.0 the worker stood in for knowledge-rag and the gateway's
client was monkeypatched.)
"""
from cognita.connectors import PUBLIC_CONTRACT_VERSION

import json

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from httpx import ASGITransport, AsyncClient

from cognita.connectors import ConnectorStore
from cognita.auth_policy import AuthenticationPolicyStore
from cognita.backups import backup_id_of, backup_if_exists, resolve_target
from cognita.config import CognitaConfig
from cognita.engine_local import LocalEngineHost
from cognita.gateway import create_gateway_app
from cognita.registry import Project, Registry
from engine_fakes import FakeEngineHost

ENGINE_TOOLS = [
    {"name": n, "description": n, "inputSchema": {"type": "object"}}
    for n in [
        "search_knowledge", "get_document", "search_similar", "list_documents",
        "list_categories", "get_index_stats", "get_reindex_status",
        "evaluate_retrieval", "add_document", "update_document",
        "remove_document", "add_from_url", "reindex_documents",
        "move_document",
    ]
]

DOC_TEXT = "# Smoke\r\n\r\nalpha line\r\nbeta line\r\n"  # CRLF, like OneDrive files


def make_fake_worker(seen: list, documents_dir) -> FastAPI:
    app = FastAPI()

    @app.post("/mcp")
    async def mcp(request: Request):
        msg = await request.json()
        seen.append(msg)
        if msg.get("method") == "tools/list":
            return JSONResponse({"jsonrpc": "2.0", "id": msg["id"],
                                 "result": {"tools": ENGINE_TOOLS}})
        params = msg.get("params")
        args = (params.get("arguments") or {}) if isinstance(params, dict) else {}
        engine = {"status": "success", "old_chunks_removed": 2, "new_chunks_added": 3,
                  "dedup_skipped": 0, "filepath": args.get("filepath", "")}
        if params.get("name") == "move_document" and "expected_policy_revision" in args:
            engine = {
                "status": "success", "filepath": args.get("filepath", ""),
                "new_filepath": args.get("new_filepath", ""), "kind": "directory",
                "policy_revision": args["expected_policy_revision"] + 1,
                "indexing": {"state": "pending", "job_id": "directory-job"},
            }
        if params.get("name") == "remove_document" and args.get("delete_file"):
            # Deletion snapshots now belong to the engine. This worker double
            # must model that owner rather than relying on a proxy snapshot.
            target = resolve_target(documents_dir, args["filepath"])
            made = backup_if_exists(documents_dir, args["filepath"])
            assert target is not None and made is not None
            if getattr(request.app.state, "refuse_delete", False):
                engine = LocalEngineHost._delete_failed(
                    args["filepath"], target, "Synthetic unlink refusal"
                )
            else:
                target.unlink()
                engine = {"status": "success", "filepath": args["filepath"],
                          "source": str(target), "chunks_removed": 2, "was_indexed": True,
                          "delete_file_requested": True, "file_deleted": True,
                          "file_was_on_disk": True, "indexing_suppressed": False,
                          "pruned_directories": [], "previous_backup_id": backup_id_of(made)}
        return JSONResponse({"jsonrpc": "2.0", "id": msg["id"], "result": {
            "content": [{"type": "text", "text": json.dumps(engine)}],
            "isError": engine["status"] == "error"}})

    return app


@pytest.fixture
def env(tmp_path, full_mode_workspace_service):
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "note.md").write_bytes(DOC_TEXT.encode("utf-8"))

    registry = Registry(tmp_path / "registry.yaml")
    registry.add(Project(name="RW", documents_dir=docs, data_dir=tmp_path / "d1"))
    registry.add(Project(name="RO", documents_dir=docs, data_dir=tmp_path / "d2",
                         writable=False))
    # 13.0 §7.3: the `config.test_mode` registry-token fallback this fixture
    # authenticated through is deleted. Two project-scoped keys from the
    # parent-owned policy store replace the two project tokens, so the
    # writable and read-only halves stay distinguishable by credential. OAuth
    # is off so an unknown bearer stays a 401, not a 503.
    auth = AuthenticationPolicyStore(
        tmp_path / "authentication.yaml", project_names=["RW", "RO"]
    )
    auth.mutate_global(expected_revision=0, oauth_enabled=False, static_key_action="generate")
    tok_rw = auth.mutate_project(
        "RW", expected_revision=1, static_key_action="generate")["generated_key"]
    tok_ro = auth.mutate_project(
        "RO", expected_revision=2, static_key_action="generate")["generated_key"]

    connector_store = ConnectorStore(tmp_path / "connectors.yaml")
    connector_store.create(
        expected_revision=0,
        name="Test connector",
        project_names=["RW", "RO"],
        project_access={"RO": "read"},
    )

    seen: list = []
    worker = make_fake_worker(seen, docs)
    config = CognitaConfig(
        registry_path=tmp_path / "registry.yaml",
        connectors_path=tmp_path / "connectors.yaml",
        data_root=tmp_path,
    )
    app = create_gateway_app(
        config, registry, engine=FakeEngineHost(worker), connector_store=connector_store,
        authentication_store=auth, workspace_service=full_mode_workspace_service,
    )
    connector = connector_store.snapshot().connectors[0]
    app.state.test_connector_id = connector.id
    app.state.test_connector_slug = connector.slug
    app.state.test_connector_store = connector_store
    app.state.test_engine_worker = worker
    return app, tok_rw, tok_ro, docs, seen


def call(name, arguments, msg_id=7, project="RW"):
    arguments = {"project": project, **arguments}
    return {"jsonrpc": "2.0", "id": msg_id, "method": "tools/call",
            "params": {"name": name, "arguments": arguments}}


async def post(app, token, payload):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        return await c.post(f"/mcp/connectors/{app.state.test_connector_slug}/mcp/v{PUBLIC_CONTRACT_VERSION}", json=payload,
                            headers={"Authorization": f"Bearer {token}"})


def result_payload(response) -> dict:
    body = response.json()
    return json.loads(body["result"]["content"][0]["text"])


# 13. request transformed to update_document with spliced content, same id
async def test_edit_transformed_to_update_document(env):
    app, tok, _, docs, seen = env
    r = await post(app, tok, call("edit_document", {
        "filepath": "note.md", "old_str": "beta line", "new_str": "BETA"}))
    assert r.status_code == 200
    assert len(seen) == 1
    upstream = seen[0]
    assert upstream["id"] == 7  # id preserved
    assert upstream["params"]["name"] == "update_document"
    # ABSOLUTE path forwarded — the engine resolves relative paths against the
    # worker CWD, not the docs dir (the update_document path quirk)
    assert upstream["params"]["arguments"]["filepath"] == str((docs / "note.md").resolve())
    content = upstream["params"]["arguments"]["content"]
    assert "BETA" in content and "beta line" not in content
    assert "alpha line" in content
    # 5.6: the fixture is a CRLF file, and it STAYS one. This asserted the
    # opposite until 5.6.0 — the whole file came back LF-folded because the edit
    # wrote normalized text back, so changing one word silently rewrote every
    # line ending in the document. Both the untouched lines and the replacement
    # carry CRLF here.
    assert content == "# Smoke\r\n\r\nalpha line\r\nBETA\r\n"


# 14. backup fires before the worker sees the write
async def test_backup_created_before_write(env):
    app, tok, _, docs, seen = env
    await post(app, tok, call("edit_document", {
        "filepath": "note.md", "old_str": "beta line", "new_str": "BETA"}))
    backups = list((docs / "backups").glob("note.*.md"))
    assert len(backups) == 1
    # the backup is the PRE-edit content
    assert b"beta line" in backups[0].read_bytes()


# 15. response merges engine chunk stats + gateway edit fields
async def test_response_synthesis(env):
    app, tok, _, _, _ = env
    # multi-line anchor against a CRLF file -> exercises the normalized path
    # (a single-line anchor contains no newline, so it matches CRLF files "exact")
    r = await post(app, tok, call("edit_document", {
        "filepath": "note.md", "old_str": "alpha line\nbeta line", "new_str": "BETA"}))
    merged = result_payload(r)
    assert merged["status"] == "success"
    assert merged["replacements"] == 1
    assert merged["match_mode"] == "newline_normalized"
    assert merged["old_chunks_removed"] == 2  # engine fields passed through
    assert merged["new_chunks_added"] == 3
    assert "> BETA" in merged["context_diff"]


# rejections never reach the worker and never write a backup
async def test_not_found_rejected_locally(env):
    app, tok, _, docs, seen = env
    r = await post(app, tok, call("edit_document", {
        "filepath": "note.md", "old_str": "no such text", "new_str": "x"}))
    payload = result_payload(r)
    assert payload["status"] == "error" and payload["reason"] == "not_found"
    assert seen == []  # worker never called
    assert not (docs / "backups").exists()


async def test_ambiguous_rejected_locally(env):
    app, tok, _, _, seen = env
    r = await post(app, tok, call("edit_document", {
        "filepath": "note.md", "old_str": "line", "new_str": "row"}))
    assert result_payload(r)["reason"] == "ambiguous"
    assert seen == []


# 16. tools/list: injected when writable, absent + blocked when read-only
async def test_tools_list_injection_writable(env):
    app, tok, _, _, _ = env
    r = await post(app, tok, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    names = [t["name"] for t in r.json()["result"]["tools"]]
    assert "edit_document" in names and "update_document" in names


async def test_tools_list_filtered_readonly(env):
    app, _, tok_ro, _, _ = env
    r = await post(app, tok_ro, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    names = [t["name"] for t in r.json()["result"]["tools"]]
    assert "edit_document" in names and "update_document" in names
    assert "search_knowledge" in names


async def test_edit_blocked_on_readonly_project(env):
    app, _, tok_ro, _, seen = env
    r = await post(app, tok_ro, call("edit_document", {
        "filepath": "note.md", "old_str": "beta line", "new_str": "x"}, project="RO"))
    assert result_payload(r)["reason"] == "read_only"
    assert seen == []


# 17. path escape and nonexistent file
async def test_path_escape_rejected(env):
    app, tok, _, _, seen = env
    r = await post(app, tok, call("edit_document", {
        "filepath": "../outside.md", "old_str": "a", "new_str": "b"}))
    assert result_payload(r)["reason"] == "invalid_path"
    assert seen == []


async def test_missing_file_rejected(env):
    app, tok, _, _, seen = env
    r = await post(app, tok, call("edit_document", {
        "filepath": "ghost.md", "old_str": "a", "new_str": "b"}))
    payload = result_payload(r)
    assert payload["reason"] == "not_found"
    assert "add_document" in payload["message"]
    assert seen == []


async def test_move_refusals_do_not_create_source_backups(env):
    app, tok, _, docs, seen = env
    (docs / "occupied.md").write_text("destination\n", encoding="utf-8")

    same = await post(app, tok, call("move_document", {
        "filepath": "note.md", "new_filepath": "note.md"}))
    assert result_payload(same)["reason"] == "same_path"
    assert not (docs / "backups").exists()

    occupied = await post(app, tok, call("move_document", {
        "filepath": "note.md", "new_filepath": "occupied.md"}))
    assert result_payload(occupied)["reason"] == "destination_exists"
    assert not (docs / "backups").exists()
    assert (docs / "note.md").is_file() and (docs / "occupied.md").is_file()
    assert seen == []


async def test_directory_move_same_id_retry_forwards_after_old_source_disappears(env):
    """Directory replay belongs to durable engine state, not proxy file checks."""
    app, tok, _, docs, seen = env
    (docs / "source").mkdir()
    first = {
        "filepath": "source", "new_filepath": "archive/source",
        "expected_policy_revision": 0, "operation_id": "directory-replay-1",
    }
    initial = await post(app, tok, call("move_document", first, msg_id=81))
    assert result_payload(initial)["status"] == "success"
    # The engine completed the first move.  The proxy must not reinterpret the
    # retry as a missing file, strip its operation ID, or create a second backup.
    (docs / "source").rmdir()
    replay = await post(app, tok, call("move_document", first, msg_id=82))
    assert result_payload(replay)["status"] == "success"
    assert len(seen) == 2
    assert seen[1]["params"]["arguments"]["filepath"] == "source"
    assert seen[1]["params"]["arguments"]["operation_id"] == "directory-replay-1"
    assert not (docs / "backups").exists()


# 18. per-path lock identity
def test_edit_lock_same_path_same_lock():
    from cognita.proxy import _edit_lock

    assert _edit_lock("X") is _edit_lock("X")
    assert _edit_lock("X") is not _edit_lock("Y")


# ------------------------------------------------------------- batch (2.1)


async def test_batch_one_upstream_call_with_final_content(env):
    app, tok, _, docs, seen = env
    r = await post(app, tok, call("edit_document_batch", {
        "filepath": "note.md",
        "edits": [
            {"old_str": "alpha line", "new_str": "ALPHA"},
            {"old_str": "beta line", "new_str": "BETA"},
        ]}))
    assert r.status_code == 200
    assert len(seen) == 1  # ONE update_document for the whole batch
    upstream = seen[0]
    assert upstream["params"]["name"] == "update_document"
    content = upstream["params"]["arguments"]["content"]
    assert "ALPHA" in content and "BETA" in content  # both edits in final text

    merged = result_payload(r)
    assert merged["status"] == "success"
    assert merged["edits_applied"] == 2
    assert merged["replacements"] == 2
    assert [e["index"] for e in merged["edits"]] == [0, 1]
    assert merged["old_chunks_removed"] == 2  # engine fields pass through
    assert merged["context_diff"].startswith("--- before")

    # ONE backup for the whole batch
    assert len(list((docs / "backups").glob("note.*.md"))) == 1


async def test_batch_abort_never_reaches_worker(env):
    app, tok, _, docs, seen = env
    r = await post(app, tok, call("edit_document_batch", {
        "filepath": "note.md",
        "edits": [
            {"old_str": "alpha line", "new_str": "ALPHA"},
            {"old_str": "missing anchor", "new_str": "x"},
        ]}))
    p = result_payload(r)
    assert p["reason"] == "batch_aborted" and p["failed_edit"] == 1
    assert p["edits_applied"] == 0
    assert seen == []  # worker never called
    assert not (docs / "backups").exists()  # no backup consumed


async def test_tools_list_includes_batch_when_writable(env):
    app, tok, tok_ro, _, _ = env
    r = await post(app, tok, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    names = [t["name"] for t in r.json()["result"]["tools"]]
    assert "edit_document_batch" in names and "edit_document" in names
    r = await post(app, tok_ro, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    names = [t["name"] for t in r.json()["result"]["tools"]]
    assert "edit_document_batch" in names


async def test_batch_blocked_on_readonly(env):
    app, _, tok_ro, _, seen = env
    r = await post(app, tok_ro, call("edit_document_batch", {
        "filepath": "note.md", "edits": [{"old_str": "a", "new_str": "b"}]}, project="RO"))
    assert result_payload(r)["reason"] == "read_only"
    assert seen == []


# ------------------------------------------------- 2.10.4 review fixes


async def test_malformed_params_do_not_500(env):
    """params as a list used to AttributeError -> raw 500 (finding 6)."""
    app, tok, _, _, _ = env
    r = await post(app, tok, {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                              "params": [1, 2, 3]})
    assert r.status_code != 500  # forwarded to worker with tool="" (engine errors cleanly)


async def test_direct_engine_write_buffered_with_backup(env):
    """Direct update_document now rides the locked, buffered path (finding 2):
    backup still made, worker response passed through unchanged."""
    app, tok, _, docs, seen = env
    r = await post(app, tok, call("update_document", {
        "filepath": "note.md", "content": "replaced wholesale"}))
    assert r.status_code == 200
    assert seen[0]["params"]["name"] == "update_document"  # forwarded verbatim
    body = r.json()
    assert body["result"]["isError"] is False  # worker response intact
    assert len(list((docs / "backups").glob("note.*.md"))) == 1  # backup made


async def test_direct_write_path_escape_refused(env):
    """A tool-level refusal carrying reason "invalid_path", not a bare JSON-RPC
    error. DESIGN-5.0 §11.1 reserves the protocol shape for parse errors,
    unknown method/tool, an empty batch and the read-only block — and tells
    clients to branch on `reason`. The engine already returns invalid_path for
    this exact condition; the gateway sent a -32602 with nothing to branch on."""
    app, tok, _, _, seen = env
    r = await post(app, tok, call("update_document", {
        "filepath": "../outside.md", "content": "x"}))
    p = result_payload(r)
    assert p["status"] == "error"
    assert p["reason"] == "invalid_path"
    assert "outside this project" in p["message"]
    assert seen == []


async def test_add_overwrite_carries_forensics(env):
    """2.10.6: add_document over an existing file (the resurrection signature)
    stamps the result with the leftover's hash + backup id — the manual
    investigation that cracked the run-5 ghost, automated."""
    from cognita.editing import content_sha256

    app, tok, _, docs, _ = env
    r = await post(app, tok, call("add_document", {
        "filepath": "note.md", "content": "fresh content", "category": "general"}))
    p = result_payload(r)
    assert p["overwrote_existing"] is True
    assert p["previous_content_sha256"] == content_sha256(DOC_TEXT)  # the leftover, identified
    backups = list((docs / "backups").glob("note.*.md"))
    assert len(backups) == 1
    assert p["previous_backup_id"] in backups[0].name  # forensics point at the snapshot


async def test_add_new_file_no_forensics(env):
    app, tok, _, _, _ = env
    r = await post(app, tok, call("add_document", {
        "filepath": "brand-new.md", "content": "hello", "category": "general"}))
    assert "overwrote_existing" not in result_payload(r)


async def test_remove_document_delete_verified_on_disk(env):
    """2.10.5 detected swallowed unlink failures with a gateway warning. The
    current engine refuses the delete itself; a surviving file must still be
    reported, and must never arrive inside a success envelope."""
    app, tok, _, docs, _ = env
    app.state.test_engine_worker.state.refuse_delete = True
    r = await post(app, tok, call("remove_document", {
        "filepath": "note.md", "delete_file": True}))
    p = result_payload(r)
    assert p["status"] == "error" and p["reason"] == "delete_failed"
    assert p["file_deleted"] is False
    assert r.json()["result"]["isError"] is True
    assert (docs / "note.md").read_bytes() == DOC_TEXT.encode()
    # without delete_file, no disk expectation -> no warning
    r = await post(app, tok, call("remove_document", {"filepath": "note.md"}))
    assert "gateway_warning" not in result_payload(r)
    assert "previous_backup_id" not in result_payload(r)


async def test_unreadable_file_is_clean_error(env, monkeypatch):
    """OSError on read (locked file / OneDrive placeholder) used to 500
    (finding 5)."""
    from pathlib import Path

    app, tok, _, _, seen = env

    def boom(self):
        raise PermissionError("locked by another process")

    monkeypatch.setattr(Path, "read_bytes", boom)
    r = await post(app, tok, call("read_document", {"filepath": "note.md"}))
    assert r.status_code == 200
    p = result_payload(r)
    assert p["reason"] == "unreadable" and "locked" in p["message"]
    assert seen == []


# ---------------------------------------------------- request logging (2.10.1)


async def test_tool_calls_log_shape_without_private_values(env, caplog):
    import logging as _logging

    app, tok, _, _, _ = env
    with caplog.at_level(_logging.INFO, logger="cognita.proxy"):
        await post(app, tok, call("edit_document", {
            "filepath": "note.md", "old_str": "beta line", "new_str": "BETA"}))
    line = next(r.message for r in caplog.records if "MCP [" in r.message)
    assert "call edit_document" in line
    assert '"argument_count": 3' in line
    assert "beta line" not in line and "BETA" not in line and "note.md" not in line
    assert "[RW]" in line  # project name tagged


async def test_logged_args_never_include_bulk_content(env, caplog):
    import logging as _logging

    app, tok, _, _, _ = env
    big = "x" * 5000
    with caplog.at_level(_logging.INFO, logger="cognita.proxy"):
        await post(app, tok, call("update_document", {
            "filepath": "note.md", "content": big}))
    line = next(r.message for r in caplog.records if "call update_document" in r.message)
    assert '"argument_count": 2' in line
    assert big[:200] not in line
    assert len(line) < 3000


async def test_rejected_calls_are_logged_too(env, caplog):
    import logging as _logging

    app, _, tok_ro, _, _ = env
    with caplog.at_level(_logging.INFO, logger="cognita.gateway"):
        await post(app, tok_ro, call("edit_document", {
            "filepath": "note.md", "old_str": "a", "new_str": "b"}, project="RO"))
    msgs = [r.message for r in caplog.records]
    assert any("project authorization denied" in m and "read_only" in m for m in msgs)


# ----------------------------------------------------- self-test plan (2.10)


async def test_self_test_plan_writable(env):
    app, tok, _, _, seen = env
    p = result_payload(await post(app, tok, call("get_self_test_plan", {})))
    assert p["status"] == "success"
    from cognita import __version__
    assert p["server_version"] == __version__
    plan = p["plan"]
    assert __version__ in plan
    # the plan must exercise the full surface and manage its own file
    for step in ("add_document", "read_document", "dry_run", "expected_sha256",
                 "edit_document_batch", "end_of_intro", "end_of_section",
                 "list_backups", "diff_backup", "restore_backup",
                 "remove_document", "cognita-selftest.md", "stale_file"):
        assert step in plan, f"plan missing {step}"
    assert seen == []  # served by the gateway, worker untouched


async def test_combined_current_self_test_adds_workspace_sections_and_bridge_by_policy(env):
    app, tok, _, _, seen = env
    store = app.state.test_connector_store
    store.update(
        app.state.test_connector_id,
        expected_revision=store.snapshot().revision,
        project_names=["RW", "RO"],
        workspace_enabled=True,
        default_workspace_transfer="allow",
    )
    full = result_payload(await post(app, tok, call("get_self_test_plan", {})))
    assert full["status"] == "success"
    assert "## W1:" in full["plan"] and "## W11:" in full["plan"]
    assert "CALL workspace_start_job" in full["plan"]
    index = result_payload(await post(app, tok, call("get_self_test_plan", {"section": "index"})))
    assert "W11" in {row["id"] for row in index["sections"]}
    bridge = result_payload(await post(app, tok, call("get_self_test_plan", {"section": "W11"})))
    assert bridge["status"] == "success"
    assert '"project":"RW"' in bridge["plan"]
    assert seen == []


async def test_self_test_plan_readonly(env):
    app, _, tok_ro, _, _ = env
    p = result_payload(await post(app, tok_ro, call("get_self_test_plan", {}, project="RO")))
    plan = p["plan"]
    assert "READ-ONLY" in plan
    assert "add_document" not in plan  # no write steps offered
    assert "read-only" in plan  # includes the negative write test
    # advertised on both lists
    r = await post(app, tok_ro, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert "get_self_test_plan" in [t["name"] for t in r.json()["result"]["tools"]]


# -------------------------------------------------------- retention (2.9)


async def test_gateway_threads_backup_keep(tmp_path):
    """With backup_keep_per_file=1, a second edit prunes the first backup."""
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "note.md").write_bytes(DOC_TEXT.encode("utf-8"))
    registry = Registry(tmp_path / "registry.yaml")
    registry.add(Project(name="RW", documents_dir=docs, data_dir=tmp_path / "d"))
    # 13.0 §7.3: see the `env` fixture above — a global static key from the
    # policy store replaces the deleted registry-token fallback.
    auth = AuthenticationPolicyStore(
        tmp_path / "authentication.yaml", project_names=["RW"]
    )
    tok = auth.mutate_global(
        expected_revision=0, oauth_enabled=False, static_key_action="generate"
    )["generated_key"]
    connector_store = ConnectorStore(tmp_path / "connectors.yaml")
    connector_store.create(expected_revision=0, name="Test connector", project_names=["RW"])
    seen: list = []
    config = CognitaConfig(
        registry_path=tmp_path / "registry.yaml",
        connectors_path=tmp_path / "connectors.yaml",
        data_root=tmp_path,
        backup_keep_per_file=1,
    )
    app = create_gateway_app(
        config, registry, engine=FakeEngineHost(make_fake_worker(seen, docs)),
        connector_store=connector_store,
        authentication_store=auth,
    )
    connector = connector_store.snapshot().connectors[0]
    app.state.test_connector_id = connector.id
    app.state.test_connector_slug = connector.slug

    await post(app, tok, call("edit_document", {
        "filepath": "note.md", "old_str": "beta line", "new_str": "BETA"}))
    (docs / "note.md").write_text("second state\n", encoding="utf-8")
    await post(app, tok, call("edit_document", {
        "filepath": "note.md", "old_str": "second state", "new_str": "THIRD"}))
    backups = list((docs / "backups").glob("note.*.md"))
    assert len(backups) == 1  # pruned to keep=1
    assert "second state" in backups[0].read_text(encoding="utf-8")  # the newer one


# ------------------------------------------------------- diff_backup (2.8)


async def test_diff_backup_shows_changes_since(env):
    app, tok, _, docs, seen = env
    old = DOC_TEXT.replace("beta line", "beta line ORIGINAL").replace("\r\n", "\n")
    _plant_backup(docs, "note.md", "20260101-120000", old)
    r = await post(app, tok, call("diff_backup", {
        "filepath": "note.md", "backup_id": "20260101-120000"}))
    p = result_payload(r)
    assert p["status"] == "success" and p["identical"] is False
    assert p["diff"].startswith("--- backup 20260101-120000")
    assert "+++ current" in p["diff"]
    assert "-beta line ORIGINAL" in p["diff"] and "+beta line" in p["diff"]
    assert seen == []  # disk only, worker untouched


async def test_diff_backup_identical(env):
    app, tok, _, docs, _ = env
    _plant_backup(docs, "note.md", "20260101-120000", DOC_TEXT.replace("\r\n", "\n"))
    p = result_payload(await post(app, tok, call("diff_backup", {
        "filepath": "note.md", "backup_id": "20260101-120000"})))
    assert p["identical"] is True and "diff" not in p


async def test_diff_backup_unknown_id_and_readonly_access(env):
    app, tok, tok_ro, docs, _ = env
    _plant_backup(docs, "note.md", "20260101-120000", "x")
    p = result_payload(await post(app, tok, call("diff_backup", {
        "filepath": "note.md", "backup_id": "20990101-000000"})))
    assert p["reason"] == "not_found" and "20260101-120000" in p["hint"]
    # read-only projects can diff, and see the tool advertised
    p = result_payload(await post(app, tok_ro, call("diff_backup", {
        "filepath": "note.md", "backup_id": "20260101-120000"}, project="RO")))
    assert p["status"] == "success"
    r = await post(app, tok_ro, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert "diff_backup" in [t["name"] for t in r.json()["result"]["tools"]]


# ------------------------------------------------------ staleness guard (2.7)


async def test_read_returns_content_sha_and_mtime(env):
    app, tok, _, docs, _ = env
    p = result_payload(await post(app, tok, call("read_document", {"filepath": "note.md"})))
    assert len(p["content_sha256"]) == 64
    assert p["mtime"]
    # hash is of the normalized text — matches what the matcher sees
    from cognita.editing import content_sha256
    assert p["content_sha256"] == content_sha256(DOC_TEXT)


async def test_matching_sha_allows_write(env):
    app, tok, _, _, seen = env
    sha = result_payload(await post(app, tok, call("read_document", {"filepath": "note.md"})))["content_sha256"]
    r = await post(app, tok, call("edit_document", {
        "filepath": "note.md", "old_str": "beta line", "new_str": "BETA",
        "expected_sha256": sha[:16]}))  # prefix form
    assert result_payload(r)["status"] == "success"
    assert len(seen) == 1


async def test_stale_sha_rejects_write(env):
    app, tok, _, docs, seen = env
    sha = result_payload(await post(app, tok, call("read_document", {"filepath": "note.md"})))["content_sha256"]
    # the file changes out from under the model (OneDrive / hand edit)
    (docs / "note.md").write_text("completely different now\nbeta line\n", encoding="utf-8")
    r = await post(app, tok, call("edit_document", {
        "filepath": "note.md", "old_str": "beta line", "new_str": "BETA",
        "expected_sha256": sha}))
    p = result_payload(r)
    assert p["reason"] == "stale_file"
    assert p["actual_sha256"] != sha
    assert "re-read" in p["hint"].lower()
    assert seen == [] and not (docs / "backups").exists()  # nothing written


async def test_short_sha_rejected_as_invalid(env):
    app, tok, _, _, seen = env
    r = await post(app, tok, call("edit_document", {
        "filepath": "note.md", "old_str": "beta line", "new_str": "BETA",
        "expected_sha256": "abc"}))
    assert result_payload(r)["reason"] == "invalid"
    assert seen == []


async def test_write_returns_next_credential(env):
    """A successful write hands back new_content_sha256 — the hash of what the
    engine persists — so chained edits never need a re-read.

    5.0: "what the engine persists" is now the content VERBATIM. DOC_TEXT ends
    with a newline, so the stripped and unstripped hashes genuinely differ here;
    predicting the stripped one would hand the caller a credential that its own
    next guarded write would reject as stale.
    """
    from cognita.editing import content_sha256

    app, tok, _, _, seen = env
    r = await post(app, tok, call("edit_document", {
        "filepath": "note.md", "old_str": "beta line", "new_str": "BETA"}))
    merged = result_payload(r)
    sent = seen[0]["params"]["arguments"]["content"]
    assert merged["new_content_sha256"] == content_sha256(sent)
    assert content_sha256(sent) != content_sha256(sent.strip()), (
        "fixture must keep its trailing newline, or this asserts nothing"
    )
    assert merged["new_content_sha256"] != content_sha256(DOC_TEXT)  # it changed


async def test_dry_run_returns_current_credential(env):
    """Dry run leaves the file unchanged, so it must hand back the CURRENT
    hash (the right expected_sha256 for the apply), not the would-be hash."""
    from cognita.editing import content_sha256

    app, tok, _, _, _ = env
    r = await post(app, tok, call("edit_document", {
        "filepath": "note.md", "old_str": "beta line", "new_str": "BETA",
        "dry_run": True}))
    p = result_payload(r)
    assert p["current_content_sha256"] == content_sha256(DOC_TEXT)
    assert "new_content_sha256" not in p
    assert "current_content_sha256" in p["message"]  # tells the model the flow


async def test_stale_sha_guards_batch_and_restore(env):
    app, tok, _, docs, seen = env
    _plant_backup(docs, "note.md", "20260101-120000", "old contents\n")
    stale = "0" * 64
    r = await post(app, tok, call("edit_document_batch", {
        "filepath": "note.md", "expected_sha256": stale,
        "edits": [{"old_str": "beta line", "new_str": "BETA"}]}))
    assert result_payload(r)["reason"] == "stale_file"
    r = await post(app, tok, call("restore_backup", {
        "filepath": "note.md", "backup_id": "20260101-120000",
        "expected_sha256": stale}))
    assert result_payload(r)["reason"] == "stale_file"
    assert seen == []


# ------------------------------------------------------------ insert (2.5)


async def test_insert_via_gateway(env):
    app, tok, _, docs, seen = env
    r = await post(app, tok, call("insert_in_document", {
        "filepath": "note.md", "text": "gamma line", "position": "end"}))
    merged = result_payload(r)
    assert merged["status"] == "success"
    assert merged["position"] == "end"
    # 2.10.4: flush after the last content line (was 6 — after the phantom ''
    # from the file's trailing newline, which gained a stray blank line)
    assert merged["inserted_at_line"] == 5
    assert merged["old_chunks_removed"] == 2  # engine fields pass through
    # one update_document; existing bytes intact, block appended
    assert len(seen) == 1
    content = seen[0]["params"]["arguments"]["content"]
    # no blank between content and block — and the inserted line takes the
    # file's own CRLF (5.6), rather than the whole file being folded to LF
    assert "beta line\r\ngamma line" in content
    assert "alpha line" in content
    assert "\n" not in content.replace("\r\n", "")
    # backed up like every write
    assert len(list((docs / "backups").glob("note.*.md"))) == 1


async def test_insert_dry_run_no_side_effects(env):
    app, tok, _, docs, seen = env
    r = await post(app, tok, call("insert_in_document", {
        "filepath": "note.md", "text": "x", "position": "start", "dry_run": True}))
    p = result_payload(r)
    assert p["dry_run"] is True and p["applied"] is False
    assert p["inserted_at_line"] == 1
    assert seen == [] and not (docs / "backups").exists()


async def test_insert_blocked_on_readonly_and_advertised_when_writable(env):
    app, tok, tok_ro, _, seen = env
    r = await post(app, tok_ro, call("insert_in_document", {
        "filepath": "note.md", "text": "x", "position": "end"}, project="RO"))
    assert result_payload(r)["reason"] == "read_only"
    assert seen == []
    r = await post(app, tok, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    names = [t["name"] for t in r.json()["result"]["tools"]]
    assert "insert_in_document" in names


# ----------------------------------------------------- backup tools (2.4)


def _plant_backup(docs, name, backup_id, content):
    b = docs / "backups"
    b.mkdir(exist_ok=True)
    stem, suffix = name.rsplit(".", 1)
    # newline="" or this helper does not plant the bytes it was asked for: in
    # text mode Python rewrites every "\n" to os.linesep, so on Windows a
    # fixture naming "old\n" put "old\r\n" on disk. That is the very translation
    # 5.0 removed from the write path, and it made this file assert that restore
    # NORMALIZES — the defect, pinned as the expectation.
    (b / f"{stem}.{backup_id}.{suffix}").write_text(content, encoding="utf-8", newline="")


async def test_list_backups_via_gateway(env):
    app, tok, tok_ro, docs, seen = env
    _plant_backup(docs, "note.md", "20260101-120000", "v1")
    _plant_backup(docs, "note.md", "20260615-093000", "v2")
    r = await post(app, tok, call("list_backups", {"filepath": "note.md"}))
    p = result_payload(r)
    assert p["count"] == 2
    assert [b["backup_id"] for b in p["backups"]] == ["20260615-093000", "20260101-120000"]
    assert "YYYYMMDD-HHMMSS" in p["naming"]
    assert seen == []  # never touches the worker
    # read-only projects can list too, and see the tool advertised
    r = await post(app, tok_ro, call("list_backups", {}, project="RO"))
    assert result_payload(r)["count"] == 2
    r = await post(app, tok_ro, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    names = [t["name"] for t in r.json()["result"]["tools"]]
    assert "list_backups" in names and "restore_backup" in names


async def test_restore_backup_roundtrip(env):
    app, tok, _, docs, seen = env
    _plant_backup(docs, "note.md", "20260101-120000", "the old contents\n")
    r = await post(app, tok, call("restore_backup", {
        "filepath": "note.md", "backup_id": "20260101-120000"}))
    merged = result_payload(r)
    assert merged["status"] == "success"
    assert merged["restored_from_backup"] == "20260101-120000"
    assert merged["old_chunks_removed"] == 2  # engine fields pass through
    assert merged["context_diff"].startswith("--- before")
    # worker got ONE update_document with the backup's content, absolute path
    assert len(seen) == 1
    args = seen[0]["params"]["arguments"]
    assert seen[0]["params"]["name"] == "update_document"
    assert args["content"] == "the old contents\n"
    assert args["filepath"] == str((docs / "note.md").resolve())
    # the CURRENT content was snapshotted first -> restore is undoable
    snapshots = list((docs / "backups").glob("note.*.md"))
    assert len(snapshots) == 2  # planted + pre-restore snapshot
    assert any("alpha line" in s.read_text(encoding="utf-8") for s in snapshots)


async def test_restore_unknown_id_hints_available(env):
    app, tok, _, docs, seen = env
    _plant_backup(docs, "note.md", "20260101-120000", "v1")
    r = await post(app, tok, call("restore_backup", {
        "filepath": "note.md", "backup_id": "20990101-000000"}))
    p = result_payload(r)
    assert p["reason"] == "not_found"
    assert "20260101-120000" in p["hint"]
    assert seen == []


async def test_restore_identical_content_no_change(env):
    """no_change means the BYTES already match — the file is CRLF, so the
    backup must be too. It used to compare normalized text, which called an
    LF backup 'identical' to a CRLF file and refused a restore that would in
    fact have rewritten every line ending in the document."""
    app, tok, _, docs, seen = env
    _plant_backup(docs, "note.md", "20260101-120000", DOC_TEXT)
    r = await post(app, tok, call("restore_backup", {
        "filepath": "note.md", "backup_id": "20260101-120000"}))
    assert result_payload(r)["reason"] == "no_change"
    assert seen == []


async def test_restore_differing_only_in_line_endings_is_a_real_restore(env):
    """The EOL-only case the old normalized comparison swallowed. The file is
    CRLF and the backup is the same text in LF: those are different bytes, so
    this is a restore, and it must hand the engine the backup's bytes verbatim
    rather than a re-normalized copy of them."""
    app, tok, _, docs, seen = env
    _plant_backup(docs, "note.md", "20260101-120000", DOC_TEXT.replace("\r\n", "\n"))
    r = await post(app, tok, call("restore_backup", {
        "filepath": "note.md", "backup_id": "20260101-120000"}))
    assert result_payload(r)["status"] == "success"
    assert len(seen) == 1
    assert seen[0]["params"]["arguments"]["content"] == DOC_TEXT.replace("\r\n", "\n")


async def test_restore_recreates_a_deleted_file(env):
    """The case restore_backup is ADVERTISED for and could not do.

    remove_document/remove_directory hand back backup ids and say
    'restore_backup puts any of them back' (DESIGN-5.0 §7.2). The handler
    always synthesized update_document, and the engine refuses a path that is
    not on disk — so the documented undo for a delete returned
    reason: not_found while the backup sat right there.
    """
    app, tok, _, docs, seen = env
    _plant_backup(docs, "note.md", "20260101-120000", "recovered\n")
    (docs / "note.md").unlink()

    r = await post(app, tok, call("restore_backup", {
        "filepath": "note.md", "backup_id": "20260101-120000"}))
    merged = result_payload(r)
    assert merged["status"] == "success"
    assert merged["restored_from_backup"] == "20260101-120000"
    # add_document, not update_document: there is nothing on disk to update.
    assert len(seen) == 1
    assert seen[0]["params"]["name"] == "add_document"
    assert seen[0]["params"]["arguments"]["content"] == "recovered\n"


async def test_restore_honors_expected_sha256_on_a_missing_file(env):
    """expected_sha256 was silently ignored when the file was gone.

    The guard lived inside `if target.is_file():`, so naming a version to
    replace did nothing for a deleted file — a declared precondition that
    could not fire. Both write tools treat this as stale_file.
    """
    from cognita.editing import content_sha256

    app, tok, _, docs, seen = env
    _plant_backup(docs, "note.md", "20260101-120000", "recovered\n")
    (docs / "note.md").unlink()

    r = await post(app, tok, call("restore_backup", {
        "filepath": "note.md", "backup_id": "20260101-120000",
        "expected_sha256": content_sha256("something else entirely"),
    }))
    p = result_payload(r)
    assert p["status"] == "error"
    assert p["reason"] == "stale_file"
    assert p["actual_sha256"] is None
    assert seen == []  # nothing was written


async def test_restore_is_byte_verbatim_including_the_bom(env):
    """A BOM'd backup restores WITH its BOM.

    The restore path decoded through the shared BOM-stripping helper and then
    re-normalized, so an undo silently dropped the marker and rewrote the line
    endings. Writes are byte-verbatim since 5.0; the restore has to hand over
    the bytes it means to persist.
    """
    app, tok, _, docs, seen = env
    b = docs / "backups"
    b.mkdir(exist_ok=True)
    (b / "note.20260101-120000.md").write_bytes(b"\xef\xbb\xbfwith bom\r\nsecond\r\n")

    r = await post(app, tok, call("restore_backup", {
        "filepath": "note.md", "backup_id": "20260101-120000"}))
    assert result_payload(r)["status"] == "success"
    content = seen[0]["params"]["arguments"]["content"]
    assert content == "﻿with bom\r\nsecond\r\n"
    # and it round-trips to the backup's exact bytes
    assert content.encode("utf-8") == b"\xef\xbb\xbfwith bom\r\nsecond\r\n"


async def test_restore_blocked_on_readonly(env):
    app, _, tok_ro, docs, seen = env
    _plant_backup(docs, "note.md", "20260101-120000", "v1")
    r = await post(app, tok_ro, call("restore_backup", {
        "filepath": "note.md", "backup_id": "20260101-120000"}, project="RO"))
    assert result_payload(r)["reason"] == "read_only"
    assert seen == []


# ------------------------------------------------------------ dry_run (2.2)


async def test_dry_run_previews_without_side_effects(env):
    app, tok, _, docs, seen = env
    before = (docs / "note.md").read_bytes()
    r = await post(app, tok, call("edit_document", {
        "filepath": "note.md", "old_str": "beta line", "new_str": "BETA",
        "dry_run": True}))
    p = result_payload(r)
    assert p["status"] == "success" and p["dry_run"] is True and p["applied"] is False
    assert p["replacements"] == 1
    assert "> BETA" in p["context_diff"]
    assert "NOTHING was written" in p["message"]
    # zero side effects
    assert seen == []                                  # worker never called
    assert not (docs / "backups").exists()             # no backup consumed
    assert (docs / "note.md").read_bytes() == before   # file untouched


async def test_dry_run_failed_match_flagged(env):
    app, tok, _, _, seen = env
    r = await post(app, tok, call("edit_document", {
        "filepath": "note.md", "old_str": "no such anchor", "new_str": "x",
        "dry_run": True}))
    p = result_payload(r)
    assert p["reason"] == "not_found" and p["dry_run"] is True
    assert seen == []


async def test_read_document_serves_from_disk(env):
    app, tok, _, docs, seen = env
    r = await post(app, tok, call("read_document", {
        "filepath": "note.md", "start_line": 3, "end_line": 4}))
    p = result_payload(r)
    assert p["status"] == "success"
    # 5.6: verbatim means VERBATIM — a CRLF file reads back CRLF. This asserted
    # "alpha line\nbeta line" until 5.6.0, on the theory that edit anchors needed
    # folded text; apply_edit normalizes old_str too, so they never did.
    assert p["text"] == "alpha line\r\nbeta line"
    assert p["total_lines"] == 5
    assert seen == []  # never touches the worker


async def test_read_document_works_on_readonly_project(env):
    app, _, tok_ro, _, seen = env
    r = await post(app, tok_ro, call("read_document", {"filepath": "note.md"}, project="RO"))
    assert result_payload(r)["status"] == "success"
    assert seen == []
    # and it's advertised in the read-only tools/list
    r = await post(app, tok_ro, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    names = [t["name"] for t in r.json()["result"]["tools"]]
    assert "read_document" in names and "edit_document" in names


async def test_read_document_in_writable_tools_list(env):
    app, tok, _, _, _ = env
    r = await post(app, tok, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    names = [t["name"] for t in r.json()["result"]["tools"]]
    assert {"read_document", "edit_document", "edit_document_batch"} <= set(names)


async def test_read_document_anchor_roundtrip(env):
    """The core guarantee: text from read_document works as an edit anchor."""
    app, tok, _, _, seen = env
    r = await post(app, tok, call("read_document", {"filepath": "note.md",
                                                    "start_line": 3, "end_line": 4}))
    anchor = result_payload(r)["text"]
    r = await post(app, tok, call("edit_document", {
        "filepath": "note.md", "old_str": anchor, "new_str": "REPLACED", "dry_run": True}))
    p = result_payload(r)
    assert p["status"] == "success" and p["replacements"] == 1


async def test_read_document_path_guards(env):
    app, tok, _, _, _ = env
    r = await post(app, tok, call("read_document", {"filepath": "../escape.md"}))
    assert result_payload(r)["reason"] == "invalid_path"
    r = await post(app, tok, call("read_document", {"filepath": "ghost.md"}))
    assert result_payload(r)["reason"] == "not_found"


async def test_batch_dry_run(env):
    app, tok, _, docs, seen = env
    r = await post(app, tok, call("edit_document_batch", {
        "filepath": "note.md", "dry_run": True,
        "edits": [
            {"old_str": "alpha line", "new_str": "ALPHA"},
            {"old_str": "beta line", "new_str": "BETA"},
        ]}))
    p = result_payload(r)
    assert p["dry_run"] is True and p["applied"] is False
    assert p["edits_applied"] == 2  # would-be count
    assert p["context_diff"].startswith("--- before")
    assert seen == [] and not (docs / "backups").exists()


# ------------------------------------------ 5.1: the error envelope


async def test_backup_failure_is_a_tool_error_with_a_reason(env, monkeypatch):
    """A failed backup is the highest-consequence error this server produces:
    the write did NOT happen and the cause is recoverable. It came back as a
    bare JSON-RPC -32602, so a client branching on `reason` — which §11.1 tells
    it to do — saw the most important failure as a malformed request."""
    import cognita.proxy as proxy_mod
    from cognita.backups import BackupError

    app, tok, _, _, seen = env

    def boom(*a, **k):
        raise BackupError("could not back up note.md before write: disk full")

    monkeypatch.setattr(proxy_mod, "backup_if_exists", boom)
    r = await post(app, tok, call("update_document", {
        "filepath": "note.md", "content": "new content\n"}))
    p = result_payload(r)
    assert p["status"] == "error"
    assert p["reason"] == "backup_failed"
    assert "No changes were made" in p["message"]
    assert seen == [], "the write must not reach the engine"


async def test_an_unparseable_engine_response_is_not_reported_as_success(env, monkeypatch):
    """The transform defaulted a non-JSON engine body to status "success", so
    any framing change or truncation on the engine hop turned a failed write
    into a reported success — carrying new_content_sha256 for content that was
    never persisted."""
    from cognita.proxy import _edit_response_transform

    transform = _edit_response_transform(
        "note.md", {"replacements": 1, "new_content_sha256": "deadbeef"}, "--- before\n"
    )
    payload = transform({
        "jsonrpc": "2.0", "id": 1,
        "result": {"content": [{"type": "text", "text": "not json at all"}]},
    })
    merged = json.loads(payload["result"]["content"][0]["text"])
    assert merged["status"] == "error"
    assert merged["reason"] == "internal_error"
    assert payload["result"]["isError"] is True
    # and it must NOT be decorated with a hash for content that was never written
    assert "new_content_sha256" not in merged
    assert "context_diff" not in merged


async def test_a_failed_edit_is_not_decorated_with_a_hash_of_unwritten_content(env):
    """On a failure the payload carried new_content_sha256 and a context_diff
    describing a change that was never persisted. A client feeding that hash
    back as expected_sha256 got a spurious stale_file forever."""
    from cognita.proxy import _edit_response_transform

    transform = _edit_response_transform(
        "note.md", {"replacements": 1, "new_content_sha256": "deadbeef"}, "--- before\n"
    )
    engine_error = json.dumps({"status": "error", "reason": "parse_failed",
                               "message": "no indexable text"})
    payload = transform({
        "jsonrpc": "2.0", "id": 1,
        "result": {"content": [{"type": "text", "text": engine_error}]},
    })
    merged = json.loads(payload["result"]["content"][0]["text"])
    assert merged["status"] == "error"
    assert merged["reason"] == "parse_failed"
    assert "new_content_sha256" not in merged
    assert "context_diff" not in merged
    assert payload["result"]["isError"] is True


async def test_error_messages_do_not_carry_absolute_host_paths(env, monkeypatch):
    """str(OSError) renders the server's directory layout and username into the
    shared `unreadable` payload — every gateway read/edit/diff/restore path —
    and that text lands in connector transcripts and shared chats."""
    from pathlib import Path

    app, tok, _, docs, _seen = env

    real = Path.read_bytes

    def boom(self, *a, **k):
        if self.name == "note.md":
            raise PermissionError(13, "Permission denied", str(self))
        return real(self, *a, **k)

    monkeypatch.setattr(Path, "read_bytes", boom)
    r = await post(app, tok, call("read_document", {"filepath": "note.md"}))
    p = result_payload(r)
    assert p["reason"] == "unreadable"
    assert "Permission denied" in p["message"]
    assert str(docs) not in p["message"]
    assert "note.md" not in p["message"].replace("Could not read the file", "")


# ------------------------------------------ 5.2: operation_id makes a retry safe


async def test_retry_with_the_same_operation_id_replays_and_does_not_rewrite(env):
    """Review finding C6, the case a client actually hits.

    A connector that times out does NOT cancel the first attempt — it is still
    running and still holding the write lock, so the retry executes against the
    state the first attempt already produced and is told stale_file / not_found /
    destination_exists for a write that SUCCEEDED. One observation, two possible
    states, and the client's only reasonable reading is that its write failed.
    """
    import cognita.proxy as proxy_mod

    proxy_mod._operations.clear()
    app, tok, _, _docs, seen = env

    first = await post(app, tok, call("edit_document", {
        "filepath": "note.md", "old_str": "beta line", "new_str": "BETA",
        "operation_id": "op-abc-123",
    }))
    assert result_payload(first)["status"] == "success"
    assert len(seen) == 1, "the first attempt must reach the engine"

    # The retry: byte-identical call, same operation_id.
    second = await post(app, tok, call("edit_document", {
        "filepath": "note.md", "old_str": "beta line", "new_str": "BETA",
        "operation_id": "op-abc-123",
    }))
    replayed = result_payload(second)
    assert replayed["status"] == "success", "a retry must not report failure"
    assert replayed["replayed"] is True
    assert replayed["replacements"] == result_payload(first)["replacements"]
    assert len(seen) == 1, "the retry must NOT reach the engine a second time"
    proxy_mod._operations.clear()


async def test_delete_retry_ignores_transport_metadata_and_replays_backup_receipt(env):
    """A new JSON-RPC ID/progress token must not turn a logical retry into a conflict."""
    import cognita.proxy as proxy_mod

    proxy_mod._operations.clear()
    app, tok, _, docs, seen = env
    arguments = {
        "filepath": "note.md", "delete_file": True,
        "operation_id": "remove-metadata-retry",
    }

    first_message = call("remove_document", arguments, msg_id=71)
    first_message["params"]["_meta"] = {"progressToken": "first-attempt"}
    first_response = await post(app, tok, first_message)
    first = result_payload(first_response)
    assert first["status"] == "success" and first["file_deleted"] is True
    assert first.get("previous_backup_id")
    assert not (docs / "note.md").exists()
    assert len(seen) == 1

    retry_message = call("remove_document", arguments, msg_id=72)
    retry_message["params"]["_meta"] = {"progressToken": "retry-attempt"}
    retry_response = await post(app, tok, retry_message)
    replay = result_payload(retry_response)
    assert replay["status"] == "success"
    assert replay["replayed"] is True
    assert replay["previous_backup_id"] == first["previous_backup_id"]
    assert len(seen) == 1, "replay must not execute deletion or create another backup"
    backups = list((docs / "backups").rglob("note.*"))
    assert len(backups) == 1

    changed = call("remove_document", {
        **arguments, "delete_file": False,
    }, msg_id=73)
    changed["params"]["_meta"] = {"progressToken": "changed-operation"}
    changed_payload = result_payload(await post(app, tok, changed))
    assert changed_payload["reason"] == "operation_conflict"
    assert len(seen) == 1
    proxy_mod._operations.clear()


async def test_without_an_operation_id_nothing_changes(env):
    """The argument is opt-in: a call that omits it behaves exactly as before,
    which is what makes this additive against the frozen wire contract."""
    import cognita.proxy as proxy_mod

    proxy_mod._operations.clear()
    app, tok, _, _docs, seen = env
    for _ in range(2):
        r = await post(app, tok, call("edit_document", {
            "filepath": "note.md", "old_str": "beta line", "new_str": "BETA"}))
        assert result_payload(r)["status"] == "success"
    # BOTH reached the engine: with no operation_id there is nothing to replay
    # against, which is the pre-5.2 behavior and must be preserved exactly.
    assert len(seen) == 2
    assert all("replayed" not in result_payload(r) for r in [r])
    proxy_mod._operations.clear()


async def test_a_different_operation_id_is_a_different_write(env):
    import cognita.proxy as proxy_mod

    proxy_mod._operations.clear()
    app, tok, _, _docs, seen = env
    await post(app, tok, call("edit_document", {
        "filepath": "note.md", "old_str": "beta line", "new_str": "BETA",
        "operation_id": "op-1"}))
    await post(app, tok, call("edit_document", {
        "filepath": "note.md", "old_str": "alpha line", "new_str": "ALPHA",
        "operation_id": "op-2"}))
    assert len(seen) == 2
    proxy_mod._operations.clear()


async def test_a_failed_write_is_replayed_as_the_same_failure(env):
    """Errors are remembered too. A retry of a call that genuinely failed must
    not silently re-attempt a write the caller believes did not happen."""
    import cognita.proxy as proxy_mod

    proxy_mod._operations.clear()
    app, tok, _, _docs, seen = env
    args = {"filepath": "note.md", "old_str": "no such text", "new_str": "x",
            "operation_id": "op-fail"}
    first = result_payload(await post(app, tok, call("edit_document", args)))
    assert first["status"] == "error" and first["reason"] == "not_found"

    second = result_payload(await post(app, tok, call("edit_document", args)))
    assert second["status"] == "error"
    assert second["reason"] == "not_found"
    assert second["replayed"] is True
    assert seen == []
    proxy_mod._operations.clear()


async def test_operation_id_is_not_forwarded_to_the_engine(env):
    """The gateway consumes it. Forwarded, it would hit the ENGINE's strict
    argument gate — whose schemas do not declare it — and be refused, turning
    the retry guard into a call that always fails."""
    import cognita.proxy as proxy_mod

    proxy_mod._operations.clear()
    app, tok, _, _docs, seen = env
    await post(app, tok, call("update_document", {
        "filepath": "note.md", "content": "fresh\n", "operation_id": "op-strip"}))
    assert len(seen) == 1
    assert "operation_id" not in seen[0]["params"]["arguments"]
    proxy_mod._operations.clear()


async def test_operation_id_is_advertised_on_mutating_tools_only(env):
    """A caller cannot use an argument tools/list does not declare — and the
    strict-argument gate would refuse it — so discoverability is part of the fix."""
    app, tok, tok_ro, _docs, _seen = env
    r = await post(app, tok, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    tools = {t["name"]: t for t in r.json()["result"]["tools"]}
    assert "operation_id" in tools["update_document"]["inputSchema"]["properties"]
    assert "operation_id" in tools["edit_document"]["inputSchema"]["properties"]
    assert "operation_id" not in tools["search_knowledge"]["inputSchema"].get("properties", {})

    ro = await post(app, tok_ro, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    for t in ro.json()["result"]["tools"]:
        if t["name"] in {"add_document", "update_document", "remove_document",
                          "move_document", "edit_document", "edit_document_batch",
                          "insert_in_document", "restore_backup"}:
            assert "operation_id" in t["inputSchema"].get("properties", {}), t["name"]


async def test_a_non_string_operation_id_is_refused(env):
    app, tok, _, _docs, seen = env
    p = result_payload(await post(app, tok, call("edit_document", {
        "filepath": "note.md", "old_str": "beta line", "new_str": "B",
        "operation_id": 12345})))
    assert p["status"] == "error" and p["reason"] == "invalid"
    assert seen == []


# 5.5: every write that takes a backup names it (DESIGN-5.0 §7.5). Until now
# only the add_document OVERWRITE path did, so naming the undo point for your
# own write meant listing backups afterwards and betting on the newest entry.


def _only_backup_id(docs) -> str:
    backups = sorted((docs / "backups").glob("note.*.md"))
    assert len(backups) == 1, [b.name for b in backups]
    return backups[0].stem.split(".", 1)[1]


async def test_edit_names_the_backup_it_took(env):
    app, tok, _, docs, _ = env
    p = result_payload(await post(app, tok, call("edit_document", {
        "filepath": "note.md", "old_str": "beta line", "new_str": "BETA"})))
    assert p["previous_backup_id"] == _only_backup_id(docs)
    # and it is the PRE-edit content, i.e. a real undo point for this call
    backup = (docs / "backups") / f"note.{p['previous_backup_id']}.md"
    assert "beta line" in backup.read_text(encoding="utf-8")


async def test_insert_and_batch_name_their_backup(env):
    app, tok, _, docs, _ = env
    first = result_payload(await post(app, tok, call("insert_in_document", {
        "filepath": "note.md", "text": "tail\n", "position": "end"})))
    assert first["previous_backup_id"] == _only_backup_id(docs)
    second = result_payload(await post(app, tok, call("edit_document_batch", {
        "filepath": "note.md", "edits": [{"old_str": "alpha line", "new_str": "A"}]})))
    # two writes in the same second -> two DISTINCT ids (the -1 suffix), because
    # no recovery point is ever overwritten
    assert second["previous_backup_id"] != first["previous_backup_id"]
    on_disk = {b.stem.split(".", 1)[1] for b in (docs / "backups").glob("note.*.md")}
    assert on_disk == {first["previous_backup_id"], second["previous_backup_id"]}


async def test_update_names_the_backup_it_took(env):
    app, tok, _, docs, _ = env
    p = result_payload(await post(app, tok, call("update_document", {
        "filepath": "note.md", "content": "replaced\n"})))
    assert p["previous_backup_id"] == _only_backup_id(docs)


async def test_remove_names_the_backup_that_holds_the_deleted_file(env):
    """The delete is the case that needs the id most: the backup IS the file."""
    app, tok, _, docs, _ = env
    p = result_payload(await post(app, tok, call("remove_document", {
        "filepath": "note.md", "delete_file": True})))
    assert p["previous_backup_id"] == _only_backup_id(docs)
    assert p["file_deleted"] is True
    assert not (docs / "note.md").exists()
    assert (docs / "backups" / f"note.{p['previous_backup_id']}.md").read_bytes() == DOC_TEXT.encode()


async def test_restore_names_the_snapshot_it_overwrote(env):
    """The undo is itself undoable by id."""
    app, tok, _, docs, _ = env
    _plant_backup(docs, "note.md", "20260101-120000", "the old contents\n")
    p = result_payload(await post(app, tok, call("restore_backup", {
        "filepath": "note.md", "backup_id": "20260101-120000"})))
    assert p["restored_from_backup"] == "20260101-120000"
    undo = (docs / "backups") / f"note.{p['previous_backup_id']}.md"
    assert undo.read_bytes() == DOC_TEXT.encode("utf-8")  # what the restore clobbered


async def test_restore_of_a_deleted_file_names_no_snapshot(env):
    """Nothing was overwritten, so there is no undo point to name."""
    app, tok, _, docs, _ = env
    _plant_backup(docs, "note.md", "20260101-120000", "recovered\n")
    (docs / "note.md").unlink()
    p = result_payload(await post(app, tok, call("restore_backup", {
        "filepath": "note.md", "backup_id": "20260101-120000"})))
    assert p["status"] == "success" and "previous_backup_id" not in p


def test_an_undo_point_never_rides_on_an_error_payload():
    """The field means "here is the undo point for the write that happened", so
    it must not appear on the payload saying the write did not happen — the
    error envelope (§11.1) is a closed shape."""
    from cognita.proxy import _augment_result_transform

    def payload(status: str) -> dict:
        return {"result": {"content": [
            {"type": "text", "text": json.dumps({"status": status, "reason": "stale_file"})}]}}

    ok = _augment_result_transform({"previous_backup_id": "X"}, when_success=True)(
        payload("success"))
    assert json.loads(ok["result"]["content"][0]["text"])["previous_backup_id"] == "X"
    bad = _augment_result_transform({"previous_backup_id": "X"}, when_success=True)(
        payload("error"))
    assert "previous_backup_id" not in json.loads(bad["result"]["content"][0]["text"])
