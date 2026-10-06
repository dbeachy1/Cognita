"""Tests for the 4.0 in-process engine (engine_local.py).

Protocol tests run anywhere (no PostgreSQL). Tool tests need a live store —
activated by COGNITA_TEST_PG_DSN like the other integration suites — and use
the deterministic fakes (no model downloads). Shapes are asserted against the
3.x engine's result payloads (D4.4: identical names/args/shapes).
"""

import asyncio
import base64
import json
import logging
import os
import threading
import uuid
from pathlib import Path

import httpx
import pytest

from cognita.config import CognitaConfig
from cognita.books.schemas import ALL_ADDITIVE_TOOL_NAMES
from cognita.editing import content_sha256
from cognita.manifest import file_facts
from cognita.engine_local import ENGINE_TOOL_DEFS, LocalEngineHost, make_snippet
from cognita.books.service import BookServiceError
from cognita.parsing import ExtensionPolicy
from cognita.registry import Project, Registry
from cognita.retrieval import RetrievalCore
from cognita.store import SchemaVersionMismatch, Store
from cognita.tokens import generate_token, hash_token
from retrieval_fakes import HashEmbedder, OverlapReranker

DSN = os.environ.get("COGNITA_TEST_PG_DSN", "")
DIMS = 32

EXPECTED_TOOLS = {
    "search_knowledge", "get_document", "search_similar", "get_documents", "list_documents",
    "list_categories", "get_index_stats", "get_reindex_status", "evaluate_retrieval",
    "add_document", "update_document", "remove_document", "remove_documents", "move_document",
    "add_from_url", "reindex_documents",
    "find_literal",  # 4.5
    "copy_document", "copy_directory", "remove_directory",  # 5.0
    "write_documents",  # 6.0.13 — atomic multi-document write
    "put_asset", "update_asset_metadata", "search_assets", "list_assets",
    "get_asset_info", "get_asset", "reindex_assets", "ocr_asset", "remove_asset",  # 10.1
    "audiobook_inspect_chapter", "audiobook_prepare_chapter",
    "audiobook_get_chapter", "audiobook_find_chunk",
    "audiobook_record_generation", "audiobook_import_audio",
    "audiobook_build", "audiobook_commit_build",
    "audiobook_get_job", "audiobook_cancel_job", "audiobook_get_generations",
    "audiobook_get_book",
    "book_get_index_status",
    "set_folder_indexing", "list_project_files", "read_project_file",
}


def make_registry(tmp_path, docs_dir, name) -> Registry:
    registry = Registry(tmp_path / "registry.yaml")
    registry.add(Project(name=name, documents_dir=docs_dir, data_dir=tmp_path / "data",
                         token_sha256=hash_token(generate_token())))
    return registry


def make_host(tmp_path, docs_dir, name) -> LocalEngineHost:
    registry = make_registry(tmp_path, docs_dir, name)
    store = Store(DSN or "postgresql://nowhere/none", embedding_dimensions=DIMS)
    core = RetrievalCore(store, HashEmbedder(DIMS), OverlapReranker())
    return LocalEngineHost(CognitaConfig(), registry, core)


def rpc(method, params=None, msg_id=1):
    return {"jsonrpc": "2.0", "id": msg_id, "method": method, "params": params or {}}


async def post(host, name, message):
    transport = httpx.ASGITransport(app=host.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        return await client.post(f"/engine/{name}/mcp", json=message)


async def call(host, name, tool, arguments=None):
    """tools/call and unwrap the engine-style text-block payload."""
    r = await post(host, name, rpc("tools/call", {"name": tool, "arguments": arguments or {}}))
    assert r.status_code == 200
    body = r.json()
    payload = json.loads(body["result"]["content"][0]["text"])
    # 5.0 §11.1: isError is no longer hardcoded false — it tracks the payload's
    # own status. Asserting the two AGREE on every single call this suite makes
    # is a stronger check than pinning either one, and it is what stops the flag
    # drifting away from the field clients actually read.
    assert body["result"]["isError"] == (payload.get("status") == "error")
    return payload


# ---------------------------------------------------------------------------
# Protocol (no PostgreSQL needed)
# ---------------------------------------------------------------------------


@pytest.fixture
def proto_host(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    return make_host(tmp_path, docs, "PROTO")


async def test_initialize_shape(proto_host):
    r = await post(proto_host, "PROTO",
                   rpc("initialize", {"protocolVersion": "2025-06-18",
                                      "capabilities": {}, "clientInfo": {"name": "t"}}))
    result = r.json()["result"]
    assert result["protocolVersion"] == "2025-06-18"  # echoes the client's version
    assert "tools" in result["capabilities"]
    assert result["serverInfo"]["name"] == "cognita-engine"


async def test_notifications_get_202(proto_host):
    r = await post(proto_host, "PROTO",
                   {"jsonrpc": "2.0", "method": "notifications/initialized"})
    assert r.status_code == 202


async def test_tools_list_serves_the_engine_tools(proto_host):
    r = await post(proto_host, "PROTO", rpc("tools/list"))
    tools = r.json()["result"]["tools"]
    assert {t["name"] for t in tools} == EXPECTED_TOOLS
    for t in tools:
        assert t["description"]
        assert t["inputSchema"]["type"] == "object"
    # spot-check argument names carried over from 3.x
    by_name = {t["name"]: t for t in tools}
    assert set(by_name["search_knowledge"]["inputSchema"]["properties"]) == {
        "query", "max_results", "category", "hybrid_alpha", "min_score", "snippet_mode",
        "retrieval_profile"}
    assert by_name["search_knowledge"]["inputSchema"]["required"] == ["query"]
    assert set(by_name["reindex_documents"]["inputSchema"]["properties"]) == {
        "force", "full_rebuild"}
    assert by_name["move_document"]["inputSchema"]["required"] == ["filepath", "new_filepath"]
    assert set(by_name["move_document"]["inputSchema"]["properties"]).issuperset({
        "expected_policy_revision", "operation_id",
    })


async def test_unknown_project_404(proto_host):
    r = await post(proto_host, "NOPE", rpc("tools/list"))
    assert r.status_code == 404


async def test_unknown_tool_is_jsonrpc_error(proto_host):
    r = await post(proto_host, "PROTO", rpc("tools/call", {"name": "bogus", "arguments": {}}))
    assert r.json()["error"]["code"] == -32602


async def test_unknown_method_is_jsonrpc_error(proto_host):
    r = await post(proto_host, "PROTO", rpc("bogus/method"))
    assert r.json()["error"]["code"] == -32601


async def test_get_and_delete_are_405(proto_host):
    transport = httpx.ASGITransport(app=proto_host.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        assert (await client.get("/engine/PROTO/mcp")).status_code == 405
        assert (await client.delete("/engine/PROTO/mcp")).status_code == 405


def test_make_snippet_truncates_at_natural_break():
    text = ("word " * 150).strip()
    snip = make_snippet(text)
    assert len(snip) <= 504 and snip.endswith("...")
    assert make_snippet("short") == "short"


def test_engine_tool_defs_have_no_duplicates():
    names = [t["name"] for t in ENGINE_TOOL_DEFS]
    # Core tools plus implemented book generation/import/build and storage.
    assert len(names) == len(set(names)) == len(EXPECTED_TOOLS) == 46


async def test_asset_mutation_uses_project_lock_and_type_gate(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    host = make_host(tmp_path, docs, "asset-lock")
    project = host.registry.get("asset-lock")
    held = []

    class FakeAssets:
        async def put_asset(self, args):
            held.append(host.core.write_lock(project.name).locked())
            return {"status": "success"}

    host._asset_services[(project.name, None)] = FakeAssets()
    args = {
        "filepath": "x.png",
        "image": {"image_url": "data:image/png;base64,eA=="},
        "operation_id": "lock-test",
    }
    assert (await host._dispatch(project, "put_asset", args))["status"] == "success"
    assert held == [True]
    bad = await host._dispatch(project, "put_asset", {**args, "overwrite": "false"})
    assert bad["status"] == "error"
    assert held == [True]


# ---------------------------------------------------------------------------
# Degraded start: a database this image does not understand (13.0 §4.1)
#
# No PostgreSQL needed — the refusal happens in connect(), so a store that
# raises SchemaVersionMismatch there is a faithful stand-in for a real one that
# read the wrong version out of a real database.
# ---------------------------------------------------------------------------

MISMATCH_MESSAGE = (
    "Database schema 2 is newer than this image supports (1). Nothing was changed. "
    "To reset the index: python3 scripts/reset_disposable_state.py --target main "
    "--scope index --apply"
)


@pytest.fixture
def mismatched_host(tmp_path, monkeypatch):
    """A host whose store refuses its database on connect."""
    docs = tmp_path / "docs"
    docs.mkdir()
    host = make_host(tmp_path, docs, "MISMATCH")
    error = SchemaVersionMismatch(MISMATCH_MESSAGE)

    async def refuse():
        host.store.schema_error = error
        raise error

    monkeypatch.setattr(host.store, "connect", refuse)
    return host


async def test_startup_survives_a_schema_version_mismatch(mismatched_host, caplog):
    """The process must stay up: killing it would take Workspaces down too."""
    with caplog.at_level(logging.ERROR, logger="cognita.engine"):
        await mismatched_host.startup()  # must not raise
    assert MISMATCH_MESSAGE in caplog.text
    # Nothing that needs the database was started.
    assert mismatched_host.watcher is None
    assert mismatched_host._probe_task is None


async def test_startup_recovers_bookmark_publications_under_project_lock(mismatched_host, monkeypatch):
    project = mismatched_host.registry.projects[0]
    observed = []

    class RecoveryProbe:
        def recover_interrupted_import_jobs(self):
            observed.append("imports")

        def recover_bookmark_publications(self):
            assert mismatched_host.core.write_lock(project.name).locked()
            observed.append("bookmarks")

    monkeypatch.setattr(mismatched_host, "book_service_for", lambda _project: RecoveryProbe())
    original_connect = mismatched_host.store.connect

    async def connect_after_recovery():
        observed.append("connect")
        await original_connect()

    monkeypatch.setattr(mismatched_host.store, "connect", connect_after_recovery)
    await mismatched_host.startup()
    assert observed == ["imports", "bookmarks", "connect"]


async def test_bookmark_recovery_conflict_is_isolated_to_its_project(mismatched_host, monkeypatch, caplog):
    first = mismatched_host.registry.projects[0]
    second_docs = first.documents_dir.parent / "healthy-docs"
    second_docs.mkdir()
    mismatched_host.registry.add(Project(
        name="healthy-project", documents_dir=second_docs,
        data_dir=first.data_dir.parent / "healthy-data",
    ))
    observed = []

    class RecoveryProbe:
        def __init__(self, project):
            self.project = project

        def recover_interrupted_import_jobs(self):
            observed.append((self.project.name, "imports"))

        def recover_bookmark_publications(self):
            assert mismatched_host.core.write_lock(self.project.name).locked()
            observed.append((self.project.name, "bookmarks"))
            if self.project.name == first.name:
                raise BookServiceError("publication_conflict", "details must not be logged")

    monkeypatch.setattr(mismatched_host, "book_service_for", RecoveryProbe)
    with caplog.at_level(logging.ERROR, logger="cognita.engine"):
        await mismatched_host.startup()

    assert observed == [
        (first.name, "imports"), (first.name, "bookmarks"),
        ("healthy-project", "imports"), ("healthy-project", "bookmarks"),
    ]
    assert "project=" + first.name in caplog.text
    assert "publication_conflict" in caplog.text
    assert "details must not be logged" not in caplog.text


@pytest.mark.asyncio
async def test_shutdown_keeps_build_worker_owned_until_media_thread_exits(tmp_path, monkeypatch):
    docs = tmp_path / "docs"
    docs.mkdir()
    host = make_host(tmp_path, docs, "build-shutdown")
    project = host.registry.get("build-shutdown")
    entered = threading.Event()
    release = threading.Event()
    job_id = str(uuid.uuid4())
    expected_job_id = job_id

    class BlockingBuildService:
        _active_build_jobs = set()
        accepted_head_revision = 7
        calls = []

        def build(self, _request, *, owner_key):
            self.calls.append(owner_key)
            return {
                "job_id": job_id, "job_revision": 1, "state": "queued",
                "poll_after_seconds": 1, "pinned_inputs_sha256": "2" * 64,
            }, False

        def run_build_job(self, job_id):
            assert job_id == expected_job_id
            entered.set()
            if not release.wait(timeout=5):
                raise AssertionError("test did not release the controlled media worker")

    service = BlockingBuildService()
    monkeypatch.setattr(host, "book_service_for", lambda _project: service)
    args = {
        "operation_id": "shutdown-build",
        "expected_head_revision": None,
        "input": {
            "kind": "chapter", "chapter_id": "ch1", "snapshot_id": "snapshot-1",
            "expected_manifest_revision": 1, "request_plan_sha256": "0" * 64,
            "takes": [{"chunk_id": "c1", "take_id": "take-1", "request_sha256": "1" * 64}],
        },
        "mode": "production_pcm", "outputs": {"master": True}, "gaps": [],
        "metadata": {"title": "Fixture", "author": "Fixture", "edition": "1"},
    }
    response = await host._dispatch(project, "audiobook_build", args)
    assert response["status"] == "success", (response, service.calls)
    assert await asyncio.to_thread(entered.wait, 3)
    key = (project.name, job_id)
    worker_task = host._book_build_tasks[key]

    shutdown = asyncio.create_task(host.shutdown())
    try:
        await asyncio.sleep(0.05)
        assert not shutdown.done()
        assert host._book_build_tasks.get(key) is worker_task
        assert job_id in service._active_build_jobs
        assert service.accepted_head_revision == 7
    finally:
        release.set()
    await asyncio.wait_for(shutdown, timeout=5)
    await asyncio.sleep(0)
    assert key not in host._book_build_tasks
    assert job_id not in service._active_build_jobs
    assert service.accepted_head_revision == 7


async def test_index_status_reports_unavailable_with_the_reason(mismatched_host):
    await mismatched_host.startup()
    assert mismatched_host.index_status == {
        "status": "unavailable", "reason": MISMATCH_MESSAGE,
    }


async def test_index_status_is_ok_before_any_refusal(proto_host):
    assert proto_host.index_status == {"status": "ok"}


async def test_every_index_tool_returns_the_reason(mismatched_host):
    await mismatched_host.startup()
    project = mismatched_host.registry.get("MISMATCH")
    # One read, one write, one asset tool: the gate is above all three.
    for tool, args in (
        ("search_knowledge", {"query": "anything"}),
        ("add_document", {"filepath": "x.md", "content": "hi"}),
        ("list_assets", {}),
    ):
        payload = await mismatched_host._dispatch(project, tool, args)
        assert payload["status"] == "error", tool
        assert payload["reason"] == "index_unavailable", tool
        assert payload["message"] == MISMATCH_MESSAGE, tool


async def test_ensure_project_refuses_for_admin_add_project(mismatched_host):
    """Admin's add-project calls the store directly, past tool dispatch."""
    await mismatched_host.startup()
    with pytest.raises(SchemaVersionMismatch):
        await mismatched_host.store.ensure_project("Anything")


# ---------------------------------------------------------------------------
# Tools (need PostgreSQL)
# ---------------------------------------------------------------------------

pg = pytest.mark.skipif(not DSN, reason="COGNITA_TEST_PG_DSN not set")


@pytest.fixture
async def env(tmp_path):
    """A connected host with a small indexed corpus."""
    name = f"T{uuid.uuid4().hex[:10]}"
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "rocm.md").write_text(
        "# ROCm Build\n\n## Steps\n\nInstall the rocm stack and run the build script.",
        encoding="utf-8",
    )
    (docs / "pcie.md").write_text(
        "# Slots\n\nThe board has three PCIe x16 slots at Gen5 and Gen4 speeds.",
        encoding="utf-8",
    )
    host = make_host(tmp_path, docs, name)
    await host.store.connect()
    await host.store.ensure_project(name)
    project = host.registry.get(name)
    await host.core.index_project(name, docs)
    yield host, project, docs
    await host.store.drop_project(name)
    await host.store.close()


@pg
async def test_search_knowledge_shape(env):
    host, project, docs = env
    payload = await call(host, project.name, "search_knowledge", {"query": "pcie slots"})
    assert payload["status"] == "success"
    assert set(payload) >= {"query", "hybrid_alpha", "result_count",
                            "filtered_by_score", "cache_hit_rate", "results"}
    top = payload["results"][0]
    assert top["source"] == str(docs / "pcie.md")  # absolute, as 3.x emitted
    assert top["filename"] == "pcie.md"
    assert "content_length" in top  # snippet_mode default true
    assert set(top) >= {"content", "category", "chunk_index", "score", "raw_rrf_score",
                        "search_method", "keywords", "routed_by"}


@pg
async def test_search_knowledge_validations(env):
    host, project, _ = env
    assert (await call(host, project.name, "search_knowledge",
                       {"query": "  "}))["message"] == "Query cannot be empty"
    none = await call(host, project.name, "search_knowledge",
                      {"query": "zqxwv nonexistent zzz", "hybrid_alpha": 0.0})
    assert none["status"] == "no_results"


@pg
async def test_search_knowledge_unknown_category_is_no_results_not_error(env):
    """An unknown category matches nothing; it is not a validation failure.

    The message names the categories actually in the index (same source
    list_categories reads) so a typo is still diagnosable.
    """
    host, project, _ = env
    payload = await call(host, project.name, "search_knowledge",
                         {"query": "rocm build", "category": "nope"})
    assert payload["status"] == "no_results"
    assert "nope" in payload["message"] and "general" in payload["message"]


@pg
async def test_every_listed_category_is_searchable(env):
    """Regression: every key list_categories() returns must be searchable.

    search_knowledge used to validate `category` against a config-derived list
    (keyword_routes + category_mappings + "general"), which is empty by default
    and so rejected every real category while list_categories/list_documents/
    get_index_stats read the live store. One assertion covers the whole class.
    """
    host, project, _ = env
    # add_document accepts an arbitrary category, so a real one exists that no
    # config table knows about — the exact shape of the original bug.
    added = await call(host, project.name, "add_document",
                       {"content": "# Card Sources\n\nA reference list of rocm build notes.",
                        "filepath": "reference/sources.md", "category": "reference"})
    assert added["status"] == "success"

    categories = (await call(host, project.name, "list_categories"))["categories"]
    assert set(categories) >= {"general", "reference"}

    for key in categories:
        payload = await call(host, project.name, "search_knowledge",
                             {"query": "rocm build notes", "category": key})
        assert payload["status"] == "success", f"category {key!r}: {payload}"
        assert all(r["category"] == key for r in payload["results"])


@pg
async def test_get_document_shape(env):
    host, project, docs = env
    payload = await call(host, project.name, "get_document", {"filepath": "rocm.md"})
    doc = payload["document"]
    assert payload["status"] == "success"
    assert doc["content"].startswith("# ROCm Build")
    assert doc["source"] == str(docs / "rocm.md")
    assert (doc["filename"], doc["format"], doc["category"]) == ("rocm.md", ".md", "general")
    assert doc["chunk_count"] >= 1
    missing = await call(host, project.name, "get_document", {"filepath": "nope.md"})
    assert missing["status"] == "error" and "Document not found" in missing["message"]


@pg
async def test_get_document_is_byte_verbatim_through_the_live_tool(env):
    """The unit-level guard is tests/test_read_verbatim.py; this is the wire.

    `content` used to be parse_file's extraction, so a .json came back
    re-serialized and a .md came back with its frontmatter deleted. Asserted
    against bytes_sha256 from the same payload, which has always described the
    file, so the two halves of one response have to agree.
    """
    import hashlib

    host, project, docs = env
    sent = '{\n  "k": ["a", "b"],\n  "n": 1\n}\n'
    out = await call(host, project.name, "add_document",
                     {"filepath": "verbatim.json", "content": sent})
    assert out["status"] == "success"

    payload = await call(host, project.name, "get_document",
                         {"filepath": "verbatim.json"})
    doc = payload["document"]
    assert doc["content"] == sent
    assert doc["content_is_extracted"] is False
    assert hashlib.sha256(doc["content"].encode("utf-8")).hexdigest() == doc["bytes_sha256"]
    # the extraction still exists and still differs — it is what search indexes
    assert doc["indexed_sha256"] != doc["bytes_sha256"]


@pg
async def test_get_document_distinguishes_lossy_text_from_exact_base64(env):
    host, project, docs = env
    raw = b"prefix\x00needle\xffsuffix"
    added = await call(host, project.name, "add_document", {
        "filepath": "lossy.md",
        "content": base64.b64encode(raw).decode("ascii"),
        "content_encoding": "base64",
    })
    assert added["status"] == "success"

    readable = await call(host, project.name, "get_document", {"filepath": "lossy.md"})
    assert readable["document"]["content"] == "prefix\x00needle\ufffdsuffix"
    assert readable["document"]["content_is_lossy"] is True
    assert readable["document"]["index_text_sanitized"] is True
    assert "does NOT hash" in readable["document"]["content_note"]

    exact = await call(host, project.name, "get_document", {
        "filepath": "lossy.md", "content_encoding": "base64",
    })
    assert base64.b64decode(exact["document"]["content"]) == raw
    assert exact["document"]["utf8_valid"] is False
    assert exact["document"]["decode_error_bytes"] == 1
    assert exact["document"]["content_is_lossy"] is False
    assert "exact original file bytes" in exact["document"]["content_note"]
    assert (docs / "lossy.md").read_bytes() == raw

    found = await call(host, project.name, "find_literal", {"pattern": "needle"})
    match = next(item for item in found["matches"] if item["filepath"] == "lossy.md")
    assert match["content_is_lossy"] is True
    assert match["index_text_sanitized"] is True
    assert match["decode_error_bytes"] == 1


@pg
async def test_get_documents_facts_only_is_named_and_content_free(env):
    host, project, docs = env
    payload = await call(host, project.name, "get_documents", {
        "filepaths": ["rocm.md"], "include_content": False,
    })
    entry = payload["documents"][0]
    assert payload["result_key"] == "documents" and entry["status"] == "success"
    assert entry["document"]["include_content"] is False
    assert "content" not in entry["document"]
    assert entry["document"]["tier"] == "embedded"
    assert entry["document"]["chunk_count"] >= 1
    assert entry["document"]["category"] == "general"

    absolute = await call(host, project.name, "get_documents", {
        "filepaths": [str((docs / "rocm.md").resolve())], "include_content": False,
    })
    assert absolute["status"] == "error" and absolute["reason"] == "invalid_path"


@pg
async def test_get_document_does_not_call_an_existing_file_missing(env):
    """A file on disk that extracts to nothing answered `not_found` (5.6.2).

    add_document refuses to CREATE one, so it is written straight to disk here —
    which is also the only way it happens in production: cloud sync landing a
    file Cognita never indexed. `not_found` about a file that exists is the same
    untruth as a reformatted read, and it sent callers away from content
    read_document would have handed them.
    """
    host, project, docs = env
    (docs / "front_only.md").write_bytes(b"---\ntitle: only frontmatter\n---\n")

    payload = await call(host, project.name, "get_document",
                         {"filepath": "front_only.md"})
    assert payload["status"] == "error"
    assert payload["reason"] == "no_indexable_content"   # NOT not_found
    assert payload["size_bytes"] == 32
    assert payload["bytes_sha256"]
    assert "exists on disk" in payload["message"]
    assert "read_document" in payload["hint"]

    # ...and a genuinely absent file is still not_found. The two cases were one
    # payload before, which is what made the untruth invisible.
    absent = await call(host, project.name, "get_document",
                        {"filepath": "no_such_file.md"})
    assert absent["reason"] == "not_found"


@pg
async def test_list_documents_and_categories(env):
    host, project, docs = env
    listing = await call(host, project.name, "list_documents")
    assert listing["status"] == "success" and listing["filter"] == "all"
    assert listing["count"] == 2
    entry = next(e for e in listing["documents"] if e["source"].endswith("pcie.md"))
    # The 3.x keys keep their names and meaning (D4.4 frozen shapes); 4.4 adds
    # the tier markers and 5.0 the relative filepath (2.7) alongside them. The
    # set is asserted EXACTLY, so a new key is a conscious decision rather than
    # something that drifts in.
    assert set(entry) >= {"id", "source", "category", "format", "chunks", "keywords"}
    assert set(entry) - {"id", "source", "category", "format", "chunks", "keywords"} == {
        "tier", "semantic_searchable", "filepath"
    }
    assert entry["chunks"] >= 1
    # 5.0 §5.1: `source` stays the absolute host path, `filepath` is the
    # relative one every tool accepts as input.
    assert entry["source"] == str(docs / "pcie.md")
    assert entry["filepath"] == "pcie.md"
    # 5.0 §5.3: the response names the key holding its collection. Unbounded, so
    # it is named but NOT duplicated under "results".
    assert listing["result_key"] == "documents"
    assert "results" not in listing

    cats = await call(host, project.name, "list_categories")
    assert cats["categories"] == {"general": 2}
    assert cats["total_documents"] == 2


@pg
async def test_get_index_stats_shape(env):
    host, project, _ = env
    payload = await call(host, project.name, "get_index_stats")
    stats = payload["stats"]
    assert stats["total_documents"] == 2 and stats["total_chunks"] >= 2
    assert set(stats) >= {"categories", "supported_formats", "embedding_model",
                          "embedding_dim", "reranker_model", "chunk_size",
                          "chunk_overlap", "query_cache", "reindex"}
    assert stats["reindex"] == {"active": False}
    assert set(stats["query_cache"]) == {"size", "max_size", "hits", "misses", "hit_rate"}


@pg
async def test_add_update_remove_document_flow(env):
    host, project, docs = env
    added = await call(host, project.name, "add_document",
                       {"content": "# New\n\nfresh content about the build",
                        "filepath": "notes/new.md", "category": "general"})
    assert added["status"] == "success"
    assert added["chunks_added"] >= 1 and added["dedup_skipped"] == 0
    # 5.0 §5.1: the write tools used to echo the ABSOLUTE path under "filepath",
    # which is the key list_documents uses for a relative one. Now they agree —
    # filepath is relative, source is absolute — so a result can be fed straight
    # back into another tool.
    assert added["filepath"] == "notes/new.md"
    assert added["source"] == str((docs / "notes" / "new.md").resolve())
    assert (docs / "notes" / "new.md").is_file()

    updated = await call(host, project.name, "update_document",
                         {"filepath": "notes/new.md", "content": "# New v2\n\nrewritten"})
    assert updated["status"] == "success"
    assert updated["filepath"] == "notes/new.md"
    assert updated["source"] == str((docs / "notes" / "new.md").resolve())
    assert updated["old_chunks_removed"] >= 1 and updated["new_chunks_added"] >= 1
    assert (docs / "notes" / "new.md").read_text(encoding="utf-8").startswith("# New v2")

    removed = await call(host, project.name, "remove_document",
                         {"filepath": "notes/new.md", "delete_file": True})
    assert removed["status"] == "success"
    assert removed["filepath"] == "notes/new.md"
    assert removed["chunks_removed"] >= 1 and removed["file_deleted"] is True
    # 5.0 §11.2: the emptied directory goes too, so a probe is fully undoable.
    assert removed["pruned_directories"] == ["notes"]
    assert not (docs / "notes").exists()
    assert not (docs / "notes" / "new.md").exists()

    gone = await call(host, project.name, "remove_document", {"filepath": "notes/new.md"})
    assert gone["status"] == "error" and "not found" in gone["message"].lower()


@pg
async def test_write_documents_enforces_aggregate_base64_limit(env, monkeypatch):
    import cognita.engine_documents as engine_documents

    host, project, docs = env
    monkeypatch.setattr(engine_documents, "MAX_BASE64_ATOMIC_SET_BYTES", 5)
    result = await call(host, project.name, "write_documents", {
        "documents": [
            {"filepath": "aggregate/a.md", "content": "YWJj", "content_encoding": "base64"},
            {"filepath": "aggregate/b.md", "content": "ZGVm", "content_encoding": "base64"},
        ],
    })
    assert result["status"] == "error" and result["reason"] == "too_large"
    assert result["limit_bytes"] == 5 and result["documents_written"] == 0
    assert not (docs / "aggregate").exists()


@pg
async def test_remove_documents_continue_and_stop_preserve_order_and_receipts(env):
    host, project, docs = env

    async def add(filepath):
        result = await call(host, project.name, "add_document",
                            {"filepath": filepath, "content": f"# {filepath}\n\nowned fixture"})
        assert result["status"] == "success"

    await add("bulk/a.md")
    await add("bulk/b.md")
    continued = await call(host, project.name, "remove_documents", {
        "filepaths": ["bulk/a.md", "bulk/missing.md", "bulk/b.md"],
        "delete_file": True, "on_error": "continue",
    })
    assert continued["status"] == "partial_failure"
    assert continued["result_key"] == "documents"
    assert [entry["status"] for entry in continued["documents"]] == ["success", "error", "success"]
    assert continued["succeeded"] == 2 and continued["failed"] == 1 and continued["skipped"] == 0
    assert {receipt["filepath"] for receipt in continued["backups"]} == {"bulk/a.md", "bulk/b.md"}
    assert not (docs / "bulk" / "a.md").exists()
    assert not (docs / "bulk" / "b.md").exists()

    await add("bulk/c.md")
    await add("bulk/d.md")
    stopped = await call(host, project.name, "remove_documents", {
        "filepaths": ["bulk/c.md", "bulk/missing-2.md", "bulk/d.md"],
        "delete_file": True, "on_error": "stop",
    })
    assert [entry["status"] for entry in stopped["documents"]] == ["success", "error", "skipped"]
    assert stopped["skipped"] == 1 and (docs / "bulk" / "d.md").exists()
    cleanup = await call(host, project.name, "remove_documents", {
        "filepaths": ["bulk/d.md"], "delete_file": True,
    })
    assert cleanup["status"] == "success"


@pg
async def test_remove_documents_validates_all_paths_and_refuses_directories(env):
    host, project, docs = env
    owned = docs / "owned-dir"
    owned.mkdir()
    try:
        result = await call(host, project.name, "remove_documents", {
            "filepaths": ["owned-dir", "rocm.md"], "delete_file": True,
        })
        assert result["status"] == "error" and result["reason"] == "invalid_path"
        assert (docs / "rocm.md").exists()

        duplicate = await call(host, project.name, "remove_documents", {
            "filepaths": ["rocm.md", "./rocm.md"],
        })
        assert duplicate["status"] == "error" and duplicate["reason"] == "duplicate_path"
        assert (docs / "rocm.md").exists()

        absolute = await call(host, project.name, "remove_documents", {
            "filepaths": [str((docs / "rocm.md").resolve())], "delete_file": True,
        })
        assert absolute["status"] == "error" and absolute["reason"] == "invalid_path"
        assert (docs / "rocm.md").exists()
    finally:
        owned.rmdir()


@pg
async def test_remove_documents_backup_failure_does_not_delete_or_deindex(env, monkeypatch):
    host, project, docs = env
    import cognita.engine_documents as engine_documents
    original = docs / "rocm.md"
    monkeypatch.setattr(
        engine_documents, "backup_if_exists",
        lambda *args, **kwargs: (_ for _ in ()).throw(engine_documents.BackupError("fixture failure")),
    )
    result = await call(host, project.name, "remove_documents", {
        "filepaths": ["rocm.md"], "delete_file": True,
    })
    assert result["status"] == "partial_failure"
    assert result["documents"][0]["error"]["reason"] == "backup_failed"
    assert original.exists()
    assert (await call(host, project.name, "list_documents", {"prefix": "rocm.md"}))['count'] == 1


@pg
async def test_move_document_tool(env):
    host, project, docs = env
    stats_before = await host.store.stats(project.name)

    moved = await call(host, project.name, "move_document",
                       {"filepath": "rocm.md", "new_filepath": "Guides/rocm-build.md"})
    assert moved["status"] == "success"
    assert moved["chunks_moved"] >= 1
    # 5.0 §5.1: the *_filepath keys are relative, the *_source keys absolute.
    assert moved["old_filepath"] == "rocm.md"
    assert moved["new_filepath"] == "Guides/rocm-build.md"
    assert moved["filepath"] == moved["new_filepath"]
    assert moved["new_source"] == str((docs / "Guides" / "rocm-build.md").resolve())
    assert moved["source"] == moved["new_source"]

    # file relocated on disk
    assert not (docs / "rocm.md").exists()
    assert (docs / "Guides" / "rocm-build.md").is_file()
    # no re-embed, no duplicate: same total doc/chunk counts
    assert await host.store.stats(project.name) == stats_before

    # searchable at the new path, absent at the old
    hits = await call(host, project.name, "search_knowledge",
                      {"query": "rocm build", "hybrid_alpha": 0.0})
    sources = [r["source"] for r in hits["results"]]
    assert any(s.endswith("Guides/rocm-build.md") or s.endswith("Guides\\rocm-build.md")
               for s in sources)
    assert all("rocm.md" not in s or "rocm-build.md" in s for s in sources)

    listing = await call(host, project.name, "list_documents")
    listed = [e["source"] for e in listing["documents"]]
    assert any(s.endswith("rocm-build.md") for s in listed)
    assert not any(s.endswith(str(docs / "rocm.md")) for s in listed)


@pg
async def test_move_document_error_cases(env):
    host, project, docs = env
    # missing source
    r = await call(host, project.name, "move_document",
                   {"filepath": "ghost.md", "new_filepath": "x.md"})
    assert r["status"] == "error" and "not found" in r["message"].lower()
    # destination already exists (pcie.md is indexed)
    r = await call(host, project.name, "move_document",
                   {"filepath": "rocm.md", "new_filepath": "pcie.md"})
    assert r["status"] == "error" and "exists" in r["message"].lower()
    assert (docs / "rocm.md").is_file()  # untouched after the refusal
    # same path
    r = await call(host, project.name, "move_document",
                   {"filepath": "rocm.md", "new_filepath": "rocm.md"})
    assert r["status"] == "error"
    # path escape (both directions)
    r = await call(host, project.name, "move_document",
                   {"filepath": "rocm.md", "new_filepath": "../escape.md"})
    assert r["status"] == "error" and "outside" in r["message"].lower()
    r = await call(host, project.name, "move_document",
                   {"filepath": "../escape.md", "new_filepath": "x.md"})
    assert r["status"] == "error" and "outside" in r["message"].lower()
    # missing arg
    r = await call(host, project.name, "move_document", {"filepath": "rocm.md"})
    assert r["status"] == "error" and "required" in r["message"].lower()


@pg
async def test_write_tools_reject_path_escape(env):
    host, project, _ = env
    for tool, args in [
        ("add_document", {"content": "x", "filepath": "../escape.md"}),
        ("update_document", {"filepath": "../escape.md", "content": "x"}),
        ("remove_document", {"filepath": "../escape.md"}),
    ]:
        payload = await call(host, project.name, tool, args)
        assert payload["status"] == "error", tool


@pg
async def test_add_document_refuses_unindexable_extension(env):
    """4.6.0: the extension check runs BEFORE the write, so a rejected add
    leaves nothing on disk — it used to write the file, then fail the parse,
    and the orphan was invisible to list_documents forever."""
    host, project, docs = env
    payload = await call(host, project.name, "add_document",
                         {"content": "PK...", "filepath": "notes/archive.zip"})
    assert payload["status"] == "error"
    assert not (docs / "notes" / "archive.zip").exists()
    assert not (docs / "notes").exists()  # not even the parent dir was created
    # The message is the model-facing contract: what happened, what is allowed,
    # and what to do instead.
    msg = payload["message"]
    assert "NOTHING was written" in msg
    assert ".zip" in msg and ".md" in msg
    assert "download" in msg
    assert ".md" in payload["allowed_extensions"]
    assert ".zip" not in payload["allowed_extensions"]


@pg
async def test_update_document_never_clobbers_an_unindexable_file(env):
    """The destructive half of the same bug: update_document checked only that
    the file EXISTED, so it replaced a real archive's bytes with text and only
    then raised "Unsupported format"."""
    host, project, docs = env
    archive = docs / "archive.zip"
    original = b"PK\x03\x04 not really a zip, but binary"
    archive.write_bytes(original)

    payload = await call(host, project.name, "update_document",
                         {"filepath": "archive.zip", "content": "# clobbered"})
    assert payload["status"] == "error"
    assert "NOTHING was written" in payload["message"]
    assert archive.read_bytes() == original


@pg
async def test_move_document_refuses_unindexable_destination(env):
    """A rename to an unindexable suffix used to move the file and delete the
    index row before failing — the document vanished from search."""
    host, project, docs = env
    stats_before = await host.store.stats(project.name)

    payload = await call(host, project.name, "move_document",
                         {"filepath": "rocm.md", "new_filepath": "rocm.zip"})
    assert payload["status"] == "error"
    assert "NOTHING was written" in payload["message"]
    assert (docs / "rocm.md").is_file() and not (docs / "rocm.zip").exists()
    assert await host.store.stats(project.name) == stats_before

    listing = await call(host, project.name, "list_documents")
    assert any(e["source"].endswith("rocm.md") for e in listing["documents"])


@pg
async def test_search_similar_shape(env):
    host, project, docs = env
    payload = await call(host, project.name, "search_similar", {"filepath": "rocm.md"})
    if payload["status"] == "success":  # tiny corpus can produce no neighbors
        assert payload["reference"] == "rocm.md"
        entry = payload["similar_documents"][0]
        assert set(entry) == {"source", "filepath", "filename", "category",
                              "similarity", "score", "preview"}
        assert entry["filename"] != "rocm.md"  # never returns the reference doc
        # 5.0 §5.2: a real, non-null score. Reading `score` used to yield None,
        # so a ranking could not be thresholded — on a tool that is only a
        # ranking. It mirrors `similarity` rather than inventing a second scale.
        assert entry["score"] is not None and entry["score"] == entry["similarity"]
        scores = [e["score"] for e in payload["similar_documents"]]
        assert scores == sorted(scores, reverse=True)
        # 5.0 §5.1: the relative path feeds straight back into another tool.
        assert not entry["filepath"].startswith("/")
        assert entry["source"].endswith(entry["filepath"])
        # 5.0 §5.3: reading "results" off this response used to yield nothing.
        assert payload["result_key"] == "similar_documents"
        assert payload["results"] == payload["similar_documents"]
    unindexed = await call(host, project.name, "search_similar", {"filepath": "ghost.md"})
    assert unindexed["status"] == "no_results"


@pg
async def test_evaluate_retrieval_shape(env):
    host, project, _ = env
    cases = json.dumps([{"query": "pcie slots", "expected_filepath": "pcie.md"}])
    payload = await call(host, project.name, "evaluate_retrieval", {"test_cases": cases})
    assert payload["status"] == "success"
    assert payload["total_queries"] == 1
    assert payload["recall_at_5"] == 1.0 and payload["mrr_at_5"] > 0
    assert payload["per_query"][0]["found_at_rank"] == 1
    bad = await call(host, project.name, "evaluate_retrieval", {"test_cases": "not json"})
    assert bad["status"] == "error"


@pg
async def test_add_from_url_end_to_end(env, monkeypatch):
    """Serves a real page from a local HTTP server — fetch, HTML-strip, title
    detection, save, index, searchability. No external network.

    The SSRF guard is bypassed FOR THIS TEST ONLY, deliberately and in the open:
    the test server is on 127.0.0.1, which the guard exists to refuse, and what
    is under test here is the fetch/parse/index pipeline rather than the address
    policy. The policy has its own tests below, and there is no runtime switch —
    production has no way to turn this off.
    """
    import http.server
    import threading

    host, project, docs = env

    async def _allow_loopback_for_this_test(url):
        return None

    monkeypatch.setattr(LocalEngineHost, "_assert_public_host",
                        staticmethod(_allow_loopback_for_this_test))

    class Page(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            body = (b"<html><head><title>Riser Cable Guide</title>"
                    b"<script>ignored()</script></head>"
                    b"<body><nav>skip me</nav><h1>Riser Cable Guide</h1>"
                    b"<p>Route the pcie riser cable away from the fans.</p></body></html>")
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Page)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}/guide"
        payload = await call(host, project.name, "add_from_url",
                             {"url": url, "category": "general"})
        assert payload["status"] == "success", payload
        assert payload["chunks_added"] >= 1
        # 5.0 §5.1: filepath is relative now; `source` carries the absolute
        # path. Both are asserted so a regression on either is caught here.
        assert payload["filepath"] == "general/riser-cable-guide.md"
        saved = docs / payload["filepath"]
        assert saved.is_file()
        assert Path(payload["source"]) == saved.resolve()
        text = saved.read_text(encoding="utf-8")
        assert text.startswith("# Riser Cable Guide")  # <title> detected
        assert f"Source: {url}" in text
        assert "ignored()" not in text and "skip me" not in text  # script/nav stripped
        hits = await call(host, project.name, "search_knowledge",
                          {"query": "riser cable fans", "hybrid_alpha": 0.0})
        assert hits["status"] == "success"
        assert any("riser-cable-guide" in r["source"] for r in hits["results"])
    finally:
        server.shutdown()

    bad = await call(host, project.name, "add_from_url", {"url": "ftp://nope"})
    assert bad["status"] == "error" and "http" in bad["message"]


@pg
async def test_reindex_background_lifecycle(env):
    host, project, docs = env
    (docs / "later.md").write_text("# Later\n\nnew file added on disk", encoding="utf-8")
    started = await call(host, project.name, "reindex_documents", {"force": True})
    assert started["status"] in ("started", "already_running")
    # Was a 100 x 50 ms poll of get_reindex_status. Now waits on the
    # background reindex task itself, bounded; the status call below still
    # has to report it finished.
    await asyncio.wait_for(host._reindex_tasks[project.name], timeout=30)
    status = await call(host, project.name, "get_reindex_status")
    assert status["reindex"]["active"] is False
    assert status["reindex"]["last_result"]["indexed"] == 1  # just the new file
    listing = await call(host, project.name, "list_documents")
    assert listing["count"] == 3


@pg
async def test_admin_call_tool_path(env):
    host, project, _ = env
    payload = await host.call_tool(project, "get_index_stats")
    assert payload["status"] == "success" and payload["stats"]["total_documents"] == 2


# ---------------------------------------------------------------------------
# 4.4 registered tier — the tool surface (spec self-test cases 1-16)
# ---------------------------------------------------------------------------

SCRIPT_BODY = (
    "#!/usr/bin/env python3\n"
    "def build_worldbook(sections_dir):\n"
    "    return sorted(sections_dir.glob('*.md'))\n"
)


@pytest.fixture
async def env_script(env):
    """The standard corpus plus a registered-tier script."""
    host, project, docs = env
    (docs / "build_worldbook.py").write_text(SCRIPT_BODY, encoding="utf-8")
    await host.core.index_project(project.name, docs)
    return host, project, docs


@pg
async def test_registered_document_appears_in_list_documents(env_script):
    """Case 1 — load-bearing: a path that never appears in a listing cannot be
    known, and get_document on a known path is the point of the tier."""
    host, project, docs = env_script
    payload = await call(host, project.name, "list_documents")
    by_source = {e["source"]: e for e in payload["documents"]}

    entry = by_source[str(docs / "build_worldbook.py")]
    assert entry["tier"] == "registered"
    assert entry["semantic_searchable"] is False
    assert entry["chunks"] == 0
    assert by_source[str(docs / "rocm.md")]["tier"] == "embedded"
    assert by_source[str(docs / "rocm.md")]["semantic_searchable"] is True
    assert payload["registered_count"] == 1
    assert payload["embedded_count"] == 2


@pg
async def test_get_document_returns_registered_content_in_full(env_script):
    """Case 2."""
    host, project, _ = env_script
    payload = await call(host, project.name, "get_document",
                         {"filepath": "build_worldbook.py"})
    assert payload["status"] == "success"
    doc = payload["document"]
    assert "build_worldbook" in doc["content"]
    assert "sections_dir.glob" in doc["content"]  # full content, not a snippet
    assert doc["tier"] == "registered"
    assert doc["semantic_searchable"] is False
    assert doc["chunk_count"] == 0  # stored whole — 0 is the truth, not a gap


@pg
async def test_registered_document_is_keyword_searchable(env_script):
    """Case 4 — findable by a literal string at a default hybrid_alpha."""
    host, project, docs = env_script
    payload = await call(host, project.name, "search_knowledge",
                         {"query": "build_worldbook"})
    assert payload["status"] == "success"
    hit = next(r for r in payload["results"]
               if r["source"] == str(docs / "build_worldbook.py"))
    # Case 6: results carry the tier marker.
    assert hit["tier"] == "registered"
    assert hit["semantic_searchable"] is False
    assert hit["search_method"] == "keyword"


@pg
async def test_registered_document_absent_from_semantic_only_search(env_script):
    """Case 5 — at hybrid_alpha=1.0 the keyword leg is off, so this tier
    cannot appear at all."""
    host, project, docs = env_script
    payload = await call(host, project.name, "search_knowledge",
                         {"query": "build_worldbook", "hybrid_alpha": 1.0})
    results = payload.get("results", [])
    assert all(r["source"] != str(docs / "build_worldbook.py") for r in results)


@pg
async def test_search_similar_gives_a_specific_tier_error(env_script):
    """Case 7 — never a generic 'not found': list_documents just showed it."""
    host, project, _ = env_script
    payload = await call(host, project.name, "search_similar",
                         {"filepath": "build_worldbook.py"})
    assert payload["status"] == "error"
    assert payload["reason"] == "registered_document"
    assert payload["tier"] == "registered"
    assert "registered" in payload["message"].lower()
    assert "search_knowledge" in payload["message"]


@pg
async def test_add_document_to_registered_path_creates_no_chunks(env):
    """Case 8 — writes route by extension at write time."""
    host, project, docs = env
    added = await call(host, project.name, "add_document",
                       {"content": "def helper():\n    return 42\n",
                        "filepath": "scripts/helper.py"})
    assert added["status"] == "success"
    assert added["chunks_added"] == 0
    assert added["tier"] == "registered"
    assert added["semantic_searchable"] is False
    assert (docs / "scripts" / "helper.py").is_file()
    assert await host.store.chunk_count(project.name, "scripts/helper.py") == 0
    assert await host.store.first_chunk_embedding(project.name, "scripts/helper.py") is None
    # ...and it is immediately readable back, which is the originating use case.
    got = await call(host, project.name, "get_document", {"filepath": "scripts/helper.py"})
    assert "helper" in got["document"]["content"]


@pg
async def test_update_document_on_registered_path_stays_vectorless(env_script):
    """Case 9 (engine half — the gateway owns the backup)."""
    host, project, _ = env_script
    updated = await call(host, project.name, "update_document",
                         {"filepath": "build_worldbook.py",
                          "content": "def build_worldbook():\n    return 'v2marker'\n"})
    assert updated["status"] == "success"
    assert updated["new_chunks_added"] == 0
    assert updated["tier"] == "registered"
    assert await host.store.chunk_count(project.name, "build_worldbook.py") == 0
    found = await call(host, project.name, "search_knowledge", {"query": "v2marker"})
    assert found["status"] == "success"


@pg
async def test_move_document_across_tier_boundaries(env_script):
    """Cases 10 + 11 — a rename that crosses tiers must re-evaluate the tier
    and clean up whatever the old tier left behind."""
    host, project, docs = env_script

    # .md -> .py : chunks and vectors must go away.
    moved = await call(host, project.name, "move_document",
                       {"filepath": "rocm.md", "new_filepath": "rocm.py"})
    assert moved["status"] == "success"
    assert await host.store.chunk_count(project.name, "rocm.py") == 0
    assert await host.store.first_chunk_embedding(project.name, "rocm.py") is None
    assert (await host.store.get_document(project.name, "rocm.py")).tier == "registered"
    assert await host.store.get_document(project.name, "rocm.md") is None

    # .py -> .md : chunks and vectors must appear.
    back = await call(host, project.name, "move_document",
                      {"filepath": "build_worldbook.py", "new_filepath": "wb.md"})
    assert back["status"] == "success"
    assert await host.store.chunk_count(project.name, "wb.md") > 0
    assert await host.store.first_chunk_embedding(project.name, "wb.md") is not None
    assert (await host.store.get_document(project.name, "wb.md")).tier == "embedded"

    assert await host.store.check_consistency(project.name) == []


@pg
async def test_get_index_stats_reports_tiers_separately(env_script):
    """Case 12 — a merged count would hide whether the tiers are behaving."""
    host, project, _ = env_script
    stats = (await call(host, project.name, "get_index_stats"))["stats"]

    assert stats["total_documents"] == 3
    tiers = stats["tiers"]
    assert tiers["embedded"]["documents"] == 2
    assert tiers["embedded"]["chunks"] == stats["total_chunks"]
    assert tiers["embedded"]["vectors"] == stats["total_chunks"]
    assert tiers["registered"]["documents"] == 1
    assert tiers["registered"]["chunks"] == 0
    assert tiers["registered"]["vectors"] == 0
    assert ".py" in tiers["registered"]["extensions"]
    assert ".md" in tiers["embedded"]["extensions"]


@pg
async def test_evaluate_retrieval_skips_registered_documents(env_script):
    """Retrieval quality is not a meaningful measure for documents excluded
    from retrieval."""
    host, project, _ = env_script
    payload = await call(host, project.name, "evaluate_retrieval",
                         {"test_cases": json.dumps(
                             [{"query": "build_worldbook",
                               "expected_filepath": "build_worldbook.py"}])})
    assert payload["status"] == "success"
    assert payload["per_query"][0]["found_at_rank"] is None


@pg
async def test_migration_purges_vectors_and_reports_the_purge(env):
    """Cases 14 + 16 — the 4.3 -> 4.4 upgrade shape, end to end.

    A .py indexed under 4.3 rules has live chunks and vectors. After the tier
    moves, those must be GONE (a surviving vector would keep answering semantic
    queries for a document that is supposed to have none) and the reindex must
    say how much it destroyed.
    """
    host, project, docs = env
    (docs / "build_worldbook.py").write_text(SCRIPT_BODY, encoding="utf-8")

    # 4.3 world: .py is embedded.
    host.core.set_policy(project.name, ExtensionPolicy.build([".md", ".py"], [".sh"]))
    await host.core.index_project(project.name, docs)
    assert await host.store.chunk_count(project.name, "build_worldbook.py") > 0
    assert await host.store.first_chunk_embedding(project.name, "build_worldbook.py")

    # 4.4 world: .py moves to registered. First reindex must purge and report.
    host.core.set_policy(project.name, ExtensionPolicy.build([".md"], [".py", ".sh"]))
    summary = await host.core.index_project(project.name, docs)

    assert summary["tier_changed"] == 1
    assert summary["chunks_purged"] > 0
    assert await host.store.chunk_count(project.name, "build_worldbook.py") == 0
    assert await host.store.first_chunk_embedding(project.name, "build_worldbook.py") is None
    assert await host.store.check_consistency(project.name) == []

    # Case 14: no orphaned vector survives to answer a semantic query.
    semantic = await call(host, project.name, "search_knowledge",
                          {"query": "build_worldbook sections_dir glob",
                           "hybrid_alpha": 1.0})
    assert all(r["source"] != str(docs / "build_worldbook.py")
               for r in semantic.get("results", []))

    # Case 16: the second run is a no-op — the migration does not repeat.
    again = await host.core.index_project(project.name, docs)
    assert again["tier_changed"] == 0
    assert again["chunks_purged"] == 0


@pg
async def test_json_stays_embedded_through_migration(env):
    """Case 15 — the deliberate exception. Lorebooks and ST config exports are
    .json and are content, not code; they must keep their vectors."""
    host, project, docs = env
    (docs / "lorebook.json").write_text(
        '{"entries": [{"key": "Westfinster", '
        '"content": "A trading city on the northern coast."}]}',
        encoding="utf-8",
    )
    await host.core.index_project(project.name, docs)

    doc = await host.store.get_document(project.name, "lorebook.json")
    assert doc is not None and doc.tier == "embedded"
    assert await host.store.chunk_count(project.name, "lorebook.json") > 0
    assert await host.store.first_chunk_embedding(project.name, "lorebook.json") is not None

    stats = (await call(host, project.name, "get_index_stats"))["stats"]
    assert ".json" in stats["tiers"]["embedded"]["extensions"]
    assert ".json" not in stats["tiers"]["registered"]["extensions"]


def test_overlapping_extension_resolves_to_registered_and_warns(tmp_path, caplog):
    """Case 13 — registered wins. If embedded won, naming an already-embedded
    extension in registered_extensions would do nothing and the feature would
    ship as a no-op."""
    docs = tmp_path / "docs"
    docs.mkdir()
    host = make_host(tmp_path, docs, "T1")
    project = host.registry.get("T1")
    project.indexed_extensions = [".md", ".py"]
    project.registered_extensions = [".py", ".sh"]

    with caplog.at_level("WARNING"):
        policy = host.apply_extension_policy(project)

    assert policy.tier_for(".py") == "registered"
    assert policy.tier_for(".sh") == "registered"
    assert policy.tier_for(".md") == "embedded"
    assert policy.conflicts == (".py",)
    assert ".py" in caplog.text and "BOTH" in caplog.text
    assert host.core.policy_for("T1") is policy


def test_per_project_extension_overrides_beat_the_global_default(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    host = make_host(tmp_path, docs, "T2")
    project = host.registry.get("T2")

    # No override: inherit the global config default.
    assert host.apply_extension_policy(project).tier_for(".py") == "registered"

    # Override: this connector alone stops collecting .py.
    project.registered_extensions = [".sh"]
    policy = host.apply_extension_policy(project)
    assert policy.tier_for(".py") is None
    assert policy.tier_for(".sh") == "registered"


# ---------------------------------------------------------------------------
# Category preservation on rewrite (the second out-of-scope bug from the 4.4
# spec: edit_document silently reset a document's category to "general")
# ---------------------------------------------------------------------------


@pg
async def test_update_document_preserves_category(env):
    """update_document must not silently re-derive the category.

    detect_category() falls back to "general" whenever no category_mapping
    matches the path — and on kei category_mappings is empty — so re-parsing on
    every write reset every explicitly-chosen category to "general".
    """
    host, project, _ = env
    await call(host, project.name, "add_document",
               {"content": "# Lore\n\nThe city of Westfinster.",
                "filepath": "lore/city.md", "category": "worldbook"})
    assert (await host.store.get_document(project.name, "lore/city.md")).category == "worldbook"

    await call(host, project.name, "update_document",
               {"filepath": "lore/city.md", "content": "# Lore\n\nWestfinster, rewritten."})

    assert (await host.store.get_document(project.name, "lore/city.md")).category == "worldbook"


@pg
async def test_edit_document_write_path_preserves_category(env):
    """The gateway turns edit_document/insert_in_document into ONE
    update_document call, so the engine-side rewrite is the shared write path
    for all of them."""
    host, project, docs = env
    await call(host, project.name, "add_document",
               {"content": "alpha one\nalpha two\n",
                "filepath": "notes/edit.md", "category": "worldbook"})

    # Exactly what _handle_edit_document forwards after splicing the file.
    (docs / "notes" / "edit.md").write_text("alpha ONE\nalpha two\n", encoding="utf-8")
    await call(host, project.name, "update_document",
               {"filepath": "notes/edit.md", "content": "alpha ONE\nalpha two\n"})

    assert (await host.store.get_document(project.name, "notes/edit.md")).category == "worldbook"


@pg
async def test_explicit_category_override_still_wins(env):
    """add_document's category argument must still beat the stored value."""
    host, project, _ = env
    await call(host, project.name, "add_document",
               {"content": "# One", "filepath": "x.md", "category": "worldbook"})
    await call(host, project.name, "add_document",
               {"content": "# Two", "filepath": "x.md", "category": "reference"})
    assert (await host.store.get_document(project.name, "x.md")).category == "reference"


@pg
async def test_reindex_preserves_category_of_a_changed_file(env):
    """The same reset happened on any reindex that re-parsed a changed file —
    the categories merely survived until a file was touched."""
    host, project, docs = env
    await call(host, project.name, "add_document",
               {"content": "# Lore\n\noriginal", "filepath": "lore/keep.md",
                "category": "worldbook"})

    (docs / "lore" / "keep.md").write_text("# Lore\n\nedited on disk", encoding="utf-8")
    summary = await host.core.index_project(project.name, docs)

    assert summary["indexed"] >= 1
    assert (await host.store.get_document(project.name, "lore/keep.md")).category == "worldbook"


@pg
async def test_full_rebuild_re_derives_category_from_mappings(env):
    """force=True is the escape hatch: it re-derives from category_mappings, so
    editing the mapping config and rebuilding actually applies it."""
    host, project, docs = env
    await call(host, project.name, "add_document",
               {"content": "# Lore\n\nbody", "filepath": "lore/derive.md",
                "category": "stale-value"})

    host.core.category_mappings = {"lore/": "worldbook"}
    await host.core.index_project(project.name, docs, force=True)

    assert (await host.store.get_document(project.name, "lore/derive.md")).category == "worldbook"


@pg
async def test_registered_document_also_keeps_its_category(env):
    """The fix must hold for the 4.4 tier too — it shares the write path."""
    host, project, _ = env
    await call(host, project.name, "add_document",
               {"content": "def helper():\n    return 1\n",
                "filepath": "scripts/tool.py", "category": "worldbook"})
    await call(host, project.name, "update_document",
               {"filepath": "scripts/tool.py", "content": "def helper():\n    return 2\n"})

    doc = await host.store.get_document(project.name, "scripts/tool.py")
    assert doc.category == "worldbook"
    assert doc.tier == "registered"


# ---------------------------------------------------------------------------
# find_literal (4.5) — exhaustive literal/regex search. The pure matcher is
# covered by tests/test_literals.py; these assert the WALK: index-driven file
# selection, live disk reads, tiers, filters, caps and skips.
# ---------------------------------------------------------------------------


@pytest.fixture
async def env_grep(env):
    """The standard corpus plus files planted for literal search."""
    host, project, docs = env
    (docs / "notes.md").write_text(
        "# Notes\n\nzz_needle once\nzz_needle twice: zz_needle\nZZ_NEEDLE caps\n",
        encoding="utf-8",
    )
    (docs / "sub").mkdir()
    (docs / "sub" / "deep.md").write_text("deeper zz_needle here\n", encoding="utf-8")
    (docs / "build_worldbook.py").write_text(
        SCRIPT_BODY + 'STALE = "zz_needle/config.yaml"\n', encoding="utf-8"
    )
    await host.core.index_project(project.name, docs)
    return host, project, docs


@pg
async def test_find_literal_finds_every_occurrence_across_files(env_grep):
    host, project, docs = env_grep
    payload = await call(host, project.name, "find_literal", {"pattern": "zz_needle"})

    assert payload["status"] == "success"
    assert payload["total_matches"] == 5  # notes 3 (one line twice) + deep 1 + py 1
    assert payload["files_with_matches"] == 3
    assert payload["truncated"] is False
    assert payload["case_sensitive"] is True


@pg
async def test_find_literal_compact_mode_preserves_matches_and_drops_legacy_alias(env_grep):
    host, project, _ = env_grep
    legacy = await call(host, project.name, "find_literal", {"pattern": "zz_needle"})
    compact = await call(host, project.name, "find_literal",
                         {"pattern": "zz_needle", "compact": True})
    assert legacy["result_key"] == compact["result_key"] == "matches"
    assert legacy["matches"] == compact["matches"]
    assert legacy["total_matches"] == compact["total_matches"]
    assert "results" in legacy
    assert "results" not in compact
    assert len(json.dumps(compact, separators=(",", ":"))) < len(json.dumps(legacy, separators=(",", ":")))


@pg
async def test_find_literal_results_are_ordered_by_filepath_then_line(env_grep):
    """Deterministic ordering. There is no relevance here, and imposing one
    would reintroduce the ambiguity the tool exists to remove."""
    host, project, _ = env_grep
    payload = await call(host, project.name, "find_literal", {"pattern": "zz_needle"})
    keys = [(m["filepath"], m["line_number"], m["column"]) for m in payload["matches"]]
    assert keys == sorted(keys)


@pg
async def test_find_literal_reaches_registered_documents_by_default(env_grep):
    """The capability with no other path: a .py file has no embedding and
    cannot be reached semantically at all."""
    host, project, docs = env_grep
    payload = await call(host, project.name, "find_literal", {"pattern": "zz_needle"})
    hits = [m for m in payload["matches"] if m["filepath"] == "build_worldbook.py"]
    assert len(hits) == 1
    assert hits[0]["tier"] == "registered"
    assert hits[0]["source"] == str(docs / "build_worldbook.py")


@pg
async def test_find_literal_can_exclude_the_registered_tier(env_grep):
    host, project, _ = env_grep
    payload = await call(host, project.name, "find_literal",
                         {"pattern": "zz_needle", "include_registered": False})
    assert payload["total_matches"] == 4
    assert all(m["tier"] == "embedded" for m in payload["matches"])


@pg
async def test_find_literal_reads_disk_not_the_index(env_grep):
    """Content edited on disk WITHOUT reindexing must still be found — a match
    cannot be allowed to hide behind a stale index or a chunk boundary."""
    host, project, docs = env_grep
    (docs / "notes.md").write_text("zz_offindex marker\n", encoding="utf-8")

    payload = await call(host, project.name, "find_literal", {"pattern": "zz_offindex"})
    assert payload["total_matches"] == 1
    assert payload["matches"][0]["filepath"] == "notes.md"


@pg
async def test_find_literal_zero_matches_is_success_not_error(env_grep):
    """A zero result is a load-bearing answer — it is what makes a sweep
    trustworthy, so it must never be dressed up as a failure."""
    host, project, _ = env_grep
    payload = await call(host, project.name, "find_literal", {"pattern": "zz_absent"})
    assert payload["status"] == "success"
    assert payload["total_matches"] == 0
    assert payload["matches"] == []
    assert payload["files_scanned"] > 0


@pg
async def test_find_literal_case_insensitive_opt_in(env_grep):
    host, project, _ = env_grep
    payload = await call(host, project.name, "find_literal",
                         {"pattern": "zz_needle", "case_sensitive": False})
    assert payload["total_matches"] == 6  # + the ZZ_NEEDLE caps line


@pg
async def test_find_literal_bad_regex_is_a_clean_error(env_grep):
    host, project, _ = env_grep
    payload = await call(host, project.name, "find_literal",
                         {"pattern": "[", "regex": True})
    assert payload["status"] == "error"
    assert payload["reason"] == "bad_pattern"
    assert "Invalid regular expression" in payload["message"]
    assert "Traceback" not in payload["message"]


@pg
async def test_find_literal_regex_mode(env_grep):
    host, project, _ = env_grep
    payload = await call(host, project.name, "find_literal",
                         {"pattern": r"zz_(needle|thread)", "regex": True})
    assert payload["total_matches"] == 5
    assert payload["regex"] is True


@pg
async def test_find_literal_glob_restricts_the_walk(env_grep):
    host, project, _ = env_grep
    sub = await call(host, project.name, "find_literal",
                     {"pattern": "zz_needle", "filepath_glob": "sub/*.md"})
    assert sub["total_matches"] == 1
    assert sub["files_scanned"] == 1
    assert sub["filepath_glob"] == "sub/*.md"

    # a bare extension glob matches basenames anywhere, not just the root
    scripts = await call(host, project.name, "find_literal",
                         {"pattern": "zz_needle", "filepath_glob": "*.py"})
    assert scripts["total_matches"] == 1
    assert scripts["matches"][0]["filepath"] == "build_worldbook.py"


@pg
async def test_find_literal_category_filter(env_grep):
    host, project, _ = env_grep
    await call(host, project.name, "add_document",
               {"content": "zz_needle in lore\n", "filepath": "lore.md",
                "category": "worldbook"})
    payload = await call(host, project.name, "find_literal",
                         {"pattern": "zz_needle", "category": "worldbook"})
    assert payload["total_matches"] == 1
    assert payload["matches"][0]["filepath"] == "lore.md"
    assert payload["category"] == "worldbook"


@pg
async def test_find_literal_truncates_loudly_and_counts_honestly(env_grep):
    """The cap limits what is RETURNED; total_matches stays the true count, or
    a truncated sweep would read as a complete one."""
    host, project, _ = env_grep
    payload = await call(host, project.name, "find_literal",
                         {"pattern": "zz_needle", "max_matches": 2})
    assert len(payload["matches"]) == 2
    assert payload["total_matches"] == 5
    assert payload["truncated"] is True
    assert "5 matches found" in payload["message"]


@pg
async def test_find_literal_context_lines(env_grep):
    host, project, _ = env_grep
    payload = await call(host, project.name, "find_literal",
                         {"pattern": "ZZ_NEEDLE", "context_lines": 1})
    (hit,) = payload["matches"]
    assert hit["context_before"] == ["zz_needle twice: zz_needle"]
    assert hit["line_number"] == 5


@pg
async def test_find_literal_line_numbers_feed_read_document(env_grep):
    """1-indexed and numbered off the same normalized text read_document uses,
    so a hit can be handed straight back for a ranged read."""
    host, project, docs = env_grep
    payload = await call(host, project.name, "find_literal", {"pattern": "ZZ_NEEDLE"})
    hit = payload["matches"][0]
    lines = (docs / "notes.md").read_text(encoding="utf-8").split("\n")
    assert lines[hit["line_number"] - 1] == hit["line"]
    assert hit["line"][hit["column"] - 1:].startswith("ZZ_NEEDLE")


@pg
async def test_find_literal_skips_binary_files_without_blowing_up(env_grep):
    host, project, docs = env_grep
    # The classifier intentionally accepts a small number of controls in text.
    # Use enough distinct binary controls to cross both safety thresholds rather
    # than pinning this search test to a below-threshold false positive.
    (docs / "notes.md").write_bytes(
        b"zz_needle\x00\x01\x02\x03\x04\x05\x06\x07\x08binary"
    )

    payload = await call(host, project.name, "find_literal", {"pattern": "zz_needle"})
    assert payload["status"] == "success"
    assert payload["files_skipped"] == 1
    assert payload["skipped"][0] == {"filepath": "notes.md", "reason": "binary_content"}
    assert payload["total_matches"] == 2  # the other two files still scanned


@pg
async def test_find_literal_reports_undecodable_files_instead_of_aborting(env_grep):
    host, project, docs = env_grep
    (docs / "notes.md").write_bytes(b"\xff\xfe latin gibberish zz_needle")

    payload = await call(host, project.name, "find_literal", {"pattern": "zz_needle"})
    assert payload["status"] == "success"
    assert payload["skipped"][0]["reason"] == "unsupported_text_encoding"
    assert payload["total_matches"] == 2


@pg
async def test_find_literal_reports_indexed_files_missing_from_disk(env_grep):
    """A hole in a sweep must be visible, never counted as no-match."""
    host, project, docs = env_grep
    (docs / "sub" / "deep.md").unlink()

    payload = await call(host, project.name, "find_literal", {"pattern": "zz_needle"})
    assert payload["skipped"] == [{"filepath": "sub/deep.md", "reason": "missing_on_disk"}]
    assert payload["total_matches"] == 4


@pg
async def test_find_literal_never_walks_the_backup_tree(env_grep):
    """A rename sweep must not drown in hits from old snapshots."""
    host, project, docs = env_grep
    backups = docs / "backups"
    backups.mkdir()
    (backups / "notes.md.20260801-000000.md").write_text(
        "zz_needle zz_needle zz_needle\n", encoding="utf-8"
    )
    await host.core.index_project(project.name, docs)

    payload = await call(host, project.name, "find_literal", {"pattern": "zz_needle"})
    assert payload["total_matches"] == 5
    assert not any("backups" in m["filepath"] for m in payload["matches"])


@pg
async def test_find_literal_empty_pattern_is_refused(env_grep):
    host, project, _ = env_grep
    payload = await call(host, project.name, "find_literal", {"pattern": ""})
    assert payload["status"] == "error"
    assert payload["reason"] == "bad_pattern"


# ---------------------------------------------------------------------------
# 5.0 — verbatim writes, manifest, write guards, directory tools
# ---------------------------------------------------------------------------

VERBATIM_PAYLOADS = [b"a\nb", b"a\nb\n", b"a\nb\n\n", b" a\nb", b"a\r\nb\r\n"]


@pg
@pytest.mark.parametrize("payload", VERBATIM_PAYLOADS, ids=lambda p: repr(p))
async def test_add_document_round_trips_bytes(env, payload):
    """Push, read back off disk, assert byte equality — all five payloads.

    The read-back is the FILE, not get_document: get_document returns the
    INDEXED extraction (EOLs folded, .md frontmatter stripped), which is the
    normalization that is allowed to stay because it applies to the indexed copy
    and never to the stored one. read_document and bytes_sha256 are the
    byte-verbatim read paths, and this asserts against the same bytes they see.
    """
    host, project, docs = env
    text = payload.decode("utf-8")
    out = await call(host, project.name, "add_document",
                     {"filepath": "verbatim.md", "content": text})
    assert out["status"] == "success"
    assert (docs / "verbatim.md").read_bytes() == payload


@pg
async def test_update_document_round_trips_bytes(env):
    host, project, docs = env
    await call(host, project.name, "add_document",
               {"filepath": "verbatim.md", "content": "seed\n"})
    for payload in VERBATIM_PAYLOADS:
        out = await call(host, project.name, "update_document",
                         {"filepath": "verbatim.md", "content": payload.decode("utf-8")})
        assert out["status"] == "success"
        assert (docs / "verbatim.md").read_bytes() == payload


@pg
async def test_manifest_hashes_match_independently_computed_ones(env):
    """The manifest's hashes must describe the FILES, not the index."""
    import hashlib

    host, project, docs = env
    out = await call(host, project.name, "list_documents", {"include_hashes": True})
    assert out["status"] == "success" and out["count"] == 2
    for entry in out["documents"]:
        raw = (docs / entry["filepath"]).read_bytes()
        assert entry["bytes_sha256"] == hashlib.sha256(raw).hexdigest()
        assert entry["content_sha256"] == content_sha256(raw.decode("utf-8"))
        assert entry["size_bytes"] == len(raw)
        assert entry["on_disk"] is True
        assert entry["index_drift"] is False
    assert out["drift_count"] == 0 and out["missing_on_disk_count"] == 0


@pg
async def test_manifest_reports_index_disk_drift(env):
    """Written behind Cognita's back: reads stay correct, drift becomes visible.

    This is the sync race made observable — before 5.0 there was no way to see
    the index and the disk disagreeing at all.
    """
    host, project, docs = env
    (docs / "pcie.md").write_bytes(b"# Slots\n\nrewritten by another writer\n")
    out = await call(host, project.name, "list_documents", {"include_hashes": True})
    drifted = [d for d in out["documents"] if d["filepath"] == "pcie.md"]
    assert drifted and drifted[0]["index_drift"] is True
    assert out["drift_count"] == 1
    assert "reindex_documents" in out["drift_hint"]
    # The READ path is unaffected: get_document parses from disk every time.
    doc = await call(host, project.name, "get_document", {"filepath": "pcie.md"})
    assert "another writer" in doc["document"]["content"]
    assert doc["document"]["index_drift"] is True


@pg
async def test_manifest_flags_a_document_whose_file_is_gone(env):
    host, project, docs = env
    (docs / "pcie.md").unlink()
    out = await call(host, project.name, "list_documents", {"include_hashes": True})
    gone = [d for d in out["documents"] if d["filepath"] == "pcie.md"][0]
    assert gone["on_disk"] is False and gone["index_drift"] is True
    assert out["missing_on_disk_count"] == 1


@pg
async def test_prefix_filter_selects_rather_than_being_ignored(env):
    host, project, docs = env
    (docs / "pack").mkdir()
    await call(host, project.name, "add_document",
               {"filepath": "pack/a.md", "content": "pack alpha"})
    assert (await call(host, project.name, "list_documents", {"prefix": "pack/"}))["count"] == 1
    assert (await call(host, project.name, "list_documents", {}))["count"] == 3
    # A prefix matching nothing is an empty list, not everything — the failure
    # mode 2.4 exists to prevent, checked from the other side.
    assert (await call(host, project.name, "list_documents",
                       {"prefix": "nope/"}))["count"] == 0


@pg
async def test_update_document_rejects_a_stale_hash(env):
    """The 2026-08-26 regression, in one test."""
    host, project, docs = env
    stale = content_sha256((docs / "rocm.md").read_text(encoding="utf-8"))
    await call(host, project.name, "update_document",
               {"filepath": "rocm.md", "content": "changed by someone else\n"})
    out = await call(host, project.name, "update_document",
                     {"filepath": "rocm.md", "content": "clobbered",
                      "expected_sha256": stale})
    assert out["status"] == "error" and out["reason"] == "stale_file"
    assert out["actual_sha256"] != stale
    assert (docs / "rocm.md").read_bytes() == b"changed by someone else\n"


@pg
async def test_add_document_overwrite_rejects_a_stale_hash(env):
    host, project, docs = env
    stale = content_sha256((docs / "rocm.md").read_text(encoding="utf-8"))
    await call(host, project.name, "update_document",
               {"filepath": "rocm.md", "content": "moved on\n"})
    out = await call(host, project.name, "add_document",
                     {"filepath": "rocm.md", "content": "clobbered",
                      "expected_sha256": stale})
    assert out["status"] == "error" and out["reason"] == "stale_file"
    assert (docs / "rocm.md").read_bytes() == b"moved on\n"


@pg
async def test_expected_sha256_on_a_missing_file_is_a_rejection(env):
    """You named a version to replace; "it is gone" is the change you asked about."""
    host, project, _ = env
    out = await call(host, project.name, "add_document",
                     {"filepath": "never-existed.md", "content": "x",
                      "expected_sha256": "a" * 64})
    assert out["status"] == "error" and out["reason"] == "stale_file"
    assert out["actual_sha256"] is None


@pg
async def test_matching_hash_lets_the_write_through(env):
    host, project, docs = env
    current = content_sha256((docs / "rocm.md").read_text(encoding="utf-8"))
    out = await call(host, project.name, "update_document",
                     {"filepath": "rocm.md", "content": "guarded write\n",
                      "expected_sha256": current})
    assert out["status"] == "success"
    assert (docs / "rocm.md").read_bytes() == b"guarded write\n"
    assert out["content_sha256"] == content_sha256("guarded write\n")


@pg
async def test_oversize_content_is_refused_not_truncated(env):
    host, project, docs = env
    from cognita.engine_local import MAX_CONTENT_BYTES

    out = await call(host, project.name, "add_document",
                     {"filepath": "huge.md", "content": "x" * (MAX_CONTENT_BYTES + 1)})
    assert out["status"] == "error" and out["reason"] == "too_large"
    assert out["limit_bytes"] == MAX_CONTENT_BYTES
    assert not (docs / "huge.md").exists()  # nothing written, nothing truncated


@pg
async def test_omitted_category_is_inferred_from_the_path(env):
    """5.0 §5.3: an omitted category is no longer forced to "general"."""
    host, project, docs = env
    host.core.category_mappings = {"manuals/": "manuals"}
    out = await call(host, project.name, "add_document",
                     {"filepath": "manuals/board.md", "content": "board manual text"})
    assert out["status"] == "success"
    assert out["category"] == "manuals"  # the STORED category is echoed back
    listed = await call(host, project.name, "list_documents", {"category": "manuals"})
    assert listed["count"] == 1


@pg
async def test_explicit_category_still_wins(env):
    host, project, _ = env
    host.core.category_mappings = {"manuals/": "manuals"}
    out = await call(host, project.name, "add_document",
                     {"filepath": "manuals/board.md", "content": "text",
                      "category": "hardware"})
    assert out["category"] == "hardware"


@pg
async def test_overwrite_without_a_category_keeps_the_existing_one(env):
    """The 4.4.1-class bug on the add_document path: a rewrite must not silently
    reclassify a document that had a deliberate category."""
    host, project, _ = env
    await call(host, project.name, "add_document",
               {"filepath": "note.md", "content": "v1", "category": "deliberate"})
    out = await call(host, project.name, "add_document",
                     {"filepath": "note.md", "content": "v2"})
    assert out["category"] == "deliberate"


@pg
async def test_remove_document_prunes_the_directory_it_emptied(env):
    host, project, docs = env
    await call(host, project.name, "add_document",
               {"filepath": "probe/deep/x.md", "content": "probe"})
    assert (docs / "probe" / "deep").is_dir()
    out = await call(host, project.name, "remove_document",
                     {"filepath": "probe/deep/x.md", "delete_file": True})
    assert out["status"] == "success"
    assert out["pruned_directories"] == ["probe/deep", "probe"]
    assert not (docs / "probe").exists()
    assert docs.is_dir()  # never the documents root itself


@pg
async def test_remove_document_keeps_a_directory_that_still_has_files(env):
    host, project, docs = env
    await call(host, project.name, "add_document", {"filepath": "keep/a.md", "content": "a"})
    await call(host, project.name, "add_document", {"filepath": "keep/b.md", "content": "b"})
    out = await call(host, project.name, "remove_document",
                     {"filepath": "keep/a.md", "delete_file": True})
    assert out["pruned_directories"] == []
    assert (docs / "keep" / "b.md").exists()


@pg
async def test_sync_conflict_name_is_refused_on_write(env):
    host, project, docs = env
    out = await call(host, project.name, "add_document",
                     {"filepath": "notes-PC-conflict.md", "content": "conflict copy"})
    assert out["status"] == "error" and out["reason"] == "sync_conflict_name"
    # 4.6.0's rule: never write a file Cognita cannot index. Nothing on disk.
    assert not (docs / "notes-PC-conflict.md").exists()


# ---------------------------------------------------------------- copy tools


async def _make_pack(host, project, docs):
    for name, body in (("one.md", "pack alpha"), ("two.md", "pack beta"),
                       ("three.txt", "pack gamma")):
        out = await call(host, project.name, "add_document",
                         {"filepath": f"pack/{name}", "content": body, "category": "packs"})
        assert out["status"] == "success"


@pg
async def test_copy_document_is_byte_exact_and_indexed(env):
    host, project, docs = env
    await _make_pack(host, project, docs)
    out = await call(host, project.name, "copy_document",
                     {"src_filepath": "pack/one.md", "dst_filepath": "copy/one.md"})
    assert out["status"] == "success"
    assert (docs / "copy" / "one.md").read_bytes() == (docs / "pack" / "one.md").read_bytes()
    assert out["category"] == "packs"  # inherited from the source document
    found = await call(host, project.name, "find_literal", {"pattern": "pack alpha"})
    assert found["files_with_matches"] == 2  # indexed on arrival, no reindex


@pg
async def test_copy_document_refuses_an_existing_destination(env):
    host, project, docs = env
    await _make_pack(host, project, docs)
    await call(host, project.name, "copy_document",
               {"src_filepath": "pack/one.md", "dst_filepath": "copy/one.md"})
    (docs / "copy" / "one.md").write_bytes(b"do not lose me")
    out = await call(host, project.name, "copy_document",
                     {"src_filepath": "pack/two.md", "dst_filepath": "copy/one.md"})
    assert out["status"] == "error" and out["reason"] == "destination_exists"
    assert (docs / "copy" / "one.md").read_bytes() == b"do not lose me"
    # opt in, and the replaced content is backed up rather than lost
    out = await call(host, project.name, "copy_document",
                     {"src_filepath": "pack/two.md", "dst_filepath": "copy/one.md",
                      "overwrite": True})
    assert out["status"] == "success" and out["previous_backup_id"]


@pg
async def test_copy_directory_reproduces_every_file(env):
    host, project, docs = env
    await _make_pack(host, project, docs)
    out = await call(host, project.name, "copy_directory",
                     {"src_prefix": "pack", "dst_prefix": "packcopy"})
    assert out["status"] == "success" and out["files_copied"] == 3
    assert sorted(out["destination_paths"]) == [
        "packcopy/one.md", "packcopy/three.txt", "packcopy/two.md"]
    for entry in out["documents"]:
        src = docs / entry["source_filepath"]
        dst = docs / entry["filepath"]
        assert dst.read_bytes() == src.read_bytes()
    listed = await call(host, project.name, "list_documents", {"prefix": "packcopy/"})
    assert listed["count"] == 3
    assert {d["category"] for d in listed["documents"]} == {"packs"}


@pg
async def test_copy_directory_refuses_conflicts_and_writes_nothing(env):
    """A refusal that half-wrote would be worse than no refusal."""
    host, project, docs = env
    await _make_pack(host, project, docs)
    await call(host, project.name, "add_document",
               {"filepath": "packcopy/two.md", "content": "pre-existing"})
    out = await call(host, project.name, "copy_directory",
                     {"src_prefix": "pack", "dst_prefix": "packcopy"})
    assert out["status"] == "error" and out["reason"] == "destination_exists"
    assert out["conflicts"] == ["packcopy/two.md"]
    assert out["file_count"] == 3
    # untouched: the two non-conflicting destinations were never created
    assert not (docs / "packcopy" / "one.md").exists()
    assert not (docs / "packcopy" / "three.txt").exists()
    assert (docs / "packcopy" / "two.md").read_bytes() == b"pre-existing"


@pg
async def test_copy_directory_is_non_recursive_by_default(env):
    host, project, docs = env
    await _make_pack(host, project, docs)
    await call(host, project.name, "add_document",
               {"filepath": "pack/nested/deep.md", "content": "deep"})
    flat = await call(host, project.name, "copy_directory",
                      {"src_prefix": "pack", "dst_prefix": "flatcopy"})
    assert flat["files_copied"] == 3
    assert not (docs / "flatcopy" / "nested").exists()
    deep = await call(host, project.name, "copy_directory",
                      {"src_prefix": "pack", "dst_prefix": "deepcopy", "recursive": True})
    assert deep["files_copied"] == 4
    assert (docs / "deepcopy" / "nested" / "deep.md").exists()


@pg
async def test_copy_directory_names_what_it_skipped(env):
    host, project, docs = env
    await _make_pack(host, project, docs)
    (docs / "pack" / "stray.bin").write_bytes(b"\x00\x01")
    (docs / "pack" / "notes-PC-conflict.md").write_bytes(b"conflict copy")
    out = await call(host, project.name, "copy_directory",
                     {"src_prefix": "pack", "dst_prefix": "packcopy"})
    assert out["status"] == "success" and out["files_copied"] == 3
    reasons = {s["reason"] for s in out["skipped"]}
    assert reasons == {"not_indexable", "sync_conflict"}
    assert not (docs / "packcopy" / "stray.bin").exists()


@pg
async def test_copy_directory_refuses_an_empty_or_missing_source(env):
    host, project, _ = env
    missing = await call(host, project.name, "copy_directory",
                         {"src_prefix": "nope", "dst_prefix": "x"})
    assert missing["status"] == "error" and missing["reason"] == "not_found"
    same = await call(host, project.name, "copy_directory",
                      {"src_prefix": ".", "dst_prefix": "."})
    assert same["status"] == "error"


@pg
async def test_remove_directory_refuses_a_non_empty_directory(env):
    host, project, docs = env
    await _make_pack(host, project, docs)
    out = await call(host, project.name, "remove_directory", {"prefix": "pack"})
    assert out["status"] == "error" and out["reason"] == "not_empty"
    assert out["file_count"] == 3
    assert (docs / "pack" / "one.md").exists()  # untouched on refusal
    assert (await call(host, project.name, "list_documents",
                       {"prefix": "pack/"}))["count"] == 3


@pg
async def test_remove_directory_deletes_backs_up_and_prunes(env):
    host, project, docs = env
    await _make_pack(host, project, docs)
    out = await call(host, project.name, "remove_directory",
                     {"prefix": "pack", "delete_files": True})
    assert out["status"] == "success"
    assert out["documents_removed"] == 3 and out["files_deleted"] == 3
    assert len(out["backups"]) == 3
    assert out["pruned_directories"] == ["pack"]
    assert not (docs / "pack").exists()
    assert (await call(host, project.name, "list_documents", {"prefix": "pack/"}))["count"] == 0
    # The bulk operation is recoverable as a SET, which is the point.
    from cognita.backups import list_backup_entries

    entries = list_backup_entries(docs, prefix="pack/")
    assert {e["backup_id"] for e in entries} == {b["backup_id"] for b in out["backups"]}


@pg
async def test_remove_directory_refuses_the_documents_root(env):
    host, project, _ = env
    out = await call(host, project.name, "remove_directory",
                     {"prefix": ".", "delete_files": True})
    assert out["status"] == "error"


# ---------------------------------------------------------------------------
# 5.0 — result shapes: paths that feed back in, real scores, named collections
# ---------------------------------------------------------------------------


@pg
async def test_search_results_carry_a_filepath_that_feeds_straight_back(env):
    """The 2.7 defect: a hit could not be handed to another tool without string
    surgery against an absolute host path."""
    host, project, docs = env
    hits = await call(host, project.name, "search_knowledge", {"query": "pcie slots"})
    top = hits["results"][0]
    assert top["source"] == str(docs / "pcie.md")   # absolute, as 3.x emitted
    assert top["filepath"] == "pcie.md"             # relative, as the tools accept
    fetched = await call(host, project.name, "get_document", {"filepath": top["filepath"]})
    assert fetched["status"] == "success"
    assert fetched["document"]["filepath"] == "pcie.md"


@pg
async def test_write_tools_return_a_relative_filepath_and_absolute_source(env):
    host, project, docs = env
    out = await call(host, project.name, "add_document",
                     {"filepath": "pack/note.md", "content": "text"})
    assert out["filepath"] == "pack/note.md"
    assert out["source"] == str((docs / "pack" / "note.md").resolve())
    # The returned filepath is directly reusable — that is the whole point.
    assert (await call(host, project.name, "get_document",
                       {"filepath": out["filepath"]}))["status"] == "success"


@pg
async def test_search_similar_scores_are_present_and_descending(env):
    """2.8: every entry used to read as a null score, so a ranking could not be
    thresholded — on a tool whose entire output is a ranking."""
    host, project, docs = env
    for i in range(3):
        (docs / f"extra{i}.md").write_text(
            f"# Slots {i}\n\nPCIe slots and lanes at Gen5 speeds, board {i}.",
            encoding="utf-8")
    await host.core.index_project(project.name, docs)
    out = await call(host, project.name, "search_similar", {"filepath": "pcie.md"})
    assert out["status"] == "success"
    scores = [e["score"] for e in out["similar_documents"]]
    assert all(s is not None for s in scores)
    assert scores == sorted(scores, reverse=True)
    assert all(e["score"] == e["similarity"] for e in out["similar_documents"])
    assert all(e["filepath"] and not e["filepath"].startswith("/")
               for e in out["similar_documents"])


@pg
async def test_collections_name_their_own_key(env):
    """2.9: reading `results` off a search_similar response used to yield an
    empty list, which reads as "no matches" rather than "wrong key"."""
    host, project, docs = env
    await host.core.index_project(project.name, docs)

    hits = await call(host, project.name, "search_knowledge", {"query": "pcie"})
    assert hits["result_key"] == "results"

    listed = await call(host, project.name, "list_documents", {})
    assert listed["result_key"] == "documents"
    # Unbounded: named but deliberately NOT duplicated.
    assert "results" not in listed

    found = await call(host, project.name, "find_literal", {"pattern": "PCIe"})
    assert found["result_key"] == "matches"
    assert found["results"] == found["matches"]

    sim = await call(host, project.name, "search_similar", {"filepath": "pcie.md"})
    if sim["status"] == "success":
        assert sim["result_key"] == "similar_documents"
        assert sim["results"] == sim["similar_documents"]


@pg
async def test_find_literal_distinguishes_its_two_kinds_of_zero(env):
    """2.3: the ambiguity that turned correct glob behavior into a reported bug."""
    host, project, docs = env

    # (a) files were scanned; the string is genuinely absent.
    absent = await call(host, project.name, "find_literal",
                        {"pattern": "zzmarker_definitely_absent"})
    assert absent["status"] == "success" and absent["total_matches"] == 0
    assert absent["reason"] == "no_matches"
    assert absent["files_scanned"] > 0

    # (b) the filter selected nothing, so the search never ran.
    filtered = await call(host, project.name, "find_literal",
                          {"pattern": "PCIe", "filepath_glob": "nope/*.md"})
    assert filtered["status"] == "success" and filtered["total_matches"] == 0
    assert filtered["reason"] == "no_documents_selected"
    assert filtered["files_scanned"] == 0
    assert filtered["corpus_size"] == 2
    assert "never ran" in filtered["message"].lower()


@pg
async def test_glob_star_does_not_cross_a_separator(env):
    """Pinned against the 2026-08-29 misdiagnosis: this zero is CORRECT."""
    host, project, docs = env
    (docs / "deep").mkdir()
    (docs / "deep" / "nested.md").write_text("# Deep\n\nzzmarker_deep here",
                                             encoding="utf-8")
    await host.core.index_project(project.name, docs)

    shallow = await call(host, project.name, "find_literal",
                         {"pattern": "zzmarker_deep", "filepath_glob": "deep/*.md"})
    assert shallow["total_matches"] == 1  # nested.md IS directly in deep/

    # ...but a pattern one level too shallow selects nothing, and says so.
    wrong = await call(host, project.name, "find_literal",
                       {"pattern": "zzmarker_deep", "filepath_glob": "*.md/x"})
    assert wrong["reason"] == "no_documents_selected"

    # A bare basename pattern reaches any depth.
    bare = await call(host, project.name, "find_literal",
                      {"pattern": "zzmarker_deep", "filepath_glob": "*.md"})
    assert bare["total_matches"] == 1


@pg
async def test_the_three_count_views_reconcile(env):
    """Three views of one index that disagree mean one is reading stale state,
    and every "how many documents?" answer after that is a guess."""
    host, project, docs = env
    await call(host, project.name, "add_document",
               {"filepath": "manuals/a.md", "content": "alpha", "category": "manuals"})
    await call(host, project.name, "add_document",
               {"filepath": "manuals/b.md", "content": "beta", "category": "manuals"})

    listed = await call(host, project.name, "list_documents", {})
    cats = await call(host, project.name, "list_categories", {})
    stats = await call(host, project.name, "get_index_stats", {})

    assert listed["count"] == cats["total_documents"] == stats["stats"]["total_documents"]
    from collections import Counter

    per_category = Counter(d["category"] for d in listed["documents"])
    assert dict(per_category) == cats["categories"] == stats["stats"]["categories"]


@pg
async def test_index_stats_reports_sync_conflicts_it_skipped(env):
    """A false positive here means a real document is missing from the corpus,
    so the skip must be visible rather than merely logged."""
    host, project, docs = env
    (docs / "rocm-PC-conflict.md").write_text("# Conflict copy\n\nnot a document",
                                              encoding="utf-8")
    summary = await host.core.index_project(project.name, docs, force=True)
    assert summary["sync_conflicts_skipped"] == 1

    listed = await call(host, project.name, "list_documents", {})
    assert all("conflict" not in d["filepath"] for d in listed["documents"])

    stats = await call(host, project.name, "get_index_stats", {})
    conflicts = stats["stats"]["sync_conflicts"]
    assert conflicts["count"] == 1
    assert conflicts["files"] == ["rocm-PC-conflict.md"]
    assert conflicts["patterns"]  # the rule in force is stated, so it can be changed


# ---------------------------------------------------------------------------
# 5.0.1 — ghost forensics: telling a failed delete from a sync resurrection
# ---------------------------------------------------------------------------


@pg
async def test_remove_document_reports_what_it_deleted(env):
    """A deleted file reappearing has two causes that look identical from the
    tool surface: the delete failed, or cloud sync put it back. The mtime is the
    only discriminator, and the moment BEFORE the unlink is the only time it can
    be captured. Without it the 2026-08-29 self-test run could only infer.
    """
    host, project, docs = env
    await call(host, project.name, "add_document",
               {"filepath": "ghost.md", "content": "haunted"})
    facts_before = file_facts(docs / "ghost.md")

    out = await call(host, project.name, "remove_document",
                     {"filepath": "ghost.md", "delete_file": True})
    assert out["status"] == "success"
    assert out["deleted_mtime"] == facts_before["mtime"]
    assert out["deleted_bytes_sha256"] == facts_before["bytes_sha256"]
    assert out["deleted_size_bytes"] == facts_before["size_bytes"]
    # The result must also say what to DO with those values — the model reading
    # this cannot see the code, and a bare timestamp reads as noise.
    assert "GHOST" in out["ghost_check"]
    assert not (docs / "ghost.md").exists()


@pg
async def test_a_resurrected_file_is_identifiable_by_its_mtime(env):
    """Simulates the sync resurrection: the file comes back with its ORIGINAL
    mtime and content, which is exactly what distinguishes it from a rewrite."""
    import os

    host, project, docs = env
    await call(host, project.name, "add_document",
               {"filepath": "ghost.md", "content": "haunted"})
    original = file_facts(docs / "ghost.md")
    out = await call(host, project.name, "remove_document",
                     {"filepath": "ghost.md", "delete_file": True})

    # OneDrive restores the file with its original mtime preserved.
    (docs / "ghost.md").write_bytes(b"haunted")
    os.utime(docs / "ghost.md", (original["mtime_epoch"], original["mtime_epoch"]))

    resurrected = file_facts(docs / "ghost.md")
    assert resurrected["mtime"] == out["deleted_mtime"]
    assert resurrected["bytes_sha256"] == out["deleted_bytes_sha256"]


@pg
async def test_remove_document_without_deleting_reports_no_delete_facts(env):
    """delete_file=false removes only the index row, so there is nothing deleted
    to describe — inventing the fields would imply a delete that never happened."""
    host, project, docs = env
    out = await call(host, project.name, "remove_document", {"filepath": "rocm.md"})
    assert out["status"] == "success" and out["file_deleted"] is False
    assert "deleted_mtime" not in out and "ghost_check" not in out
    assert (docs / "rocm.md").is_file()


@pg
async def test_remove_directory_reports_the_backup_id_set(env):
    """A bulk call normally produces ONE shared backup_id, because ids are
    per-second timestamps — that is what makes the operation enumerable as a set,
    and a runner expecting one id per file reads it as a collision. Reporting the
    DISTINCT set says which it is without anyone having to guess."""
    host, project, docs = env
    for name in ("a.md", "b.md", "c.md"):
        await call(host, project.name, "add_document",
                   {"filepath": f"bulk/{name}", "content": f"content {name}"})

    out = await call(host, project.name, "remove_directory",
                     {"prefix": "bulk", "delete_files": True})
    assert out["status"] == "success" and out["files_deleted"] == 3
    assert len(out["backups"]) == 3
    assert all(b["deleted_mtime"] and b["deleted_bytes_sha256"] for b in out["backups"])
    # One id per operation in the normal case; more only if the call straddled a
    # second boundary. Either way every id in the set is listed.
    assert 1 <= len(out["backup_ids"]) <= 3
    assert set(out["backup_ids"]) == {b["backup_id"] for b in out["backups"]}

    from cognita.backups import list_backup_entries

    entries = list_backup_entries(docs, prefix="bulk/")
    assert {e["backup_id"] for e in entries} == set(out["backup_ids"])


# ---------------------------------------------------------------------------
# 5.0.2: removals are one critical section (the watcher cannot interleave)
# ---------------------------------------------------------------------------


@pg
async def test_remove_directory_survives_a_watcher_sync_mid_operation(env):
    """The 2026-08-29 finding, pinned.

    remove_directory de-indexes N documents and THEN deletes N files. Both loops
    used to run as bare awaits, so the watcher's debounced whole-tree sync could
    land between them, walk a tree where the files still existed, and index a row
    the call had just deleted. The caller got documents_removed: 3 and an
    immediate list_documents showing one of them still present, with null disk
    facts and index_drift — one observation, two possible states, no way to tell
    a lagging index from a delete that failed.

    Here the watcher is simulated exactly as it behaves: index_project on its own
    TASK, started from inside the removal. Re-entrancy is per task, so the sync
    blocks on the lock until the operation finishes, by which time the files are
    gone and there is nothing to re-index.
    """
    host, project, docs = env
    for name in ("one.md", "two.md", "three.md"):
        await call(host, project.name, "add_document",
                   {"filepath": f"pack/{name}", "content": f"pack content {name}"})

    syncs: list[asyncio.Task] = []
    original = host.core.remove_file

    async def racing_remove_file(name, source):
        removed = await original(name, source)
        # A watcher flush waking up mid-removal. asyncio.sleep(0) hands it the
        # loop so it reaches the lock before we carry on.
        syncs.append(asyncio.create_task(host.core.index_project(name, docs)))
        await asyncio.sleep(0)
        return removed

    host.core.remove_file = racing_remove_file
    try:
        out = await call(host, project.name, "remove_directory",
                         {"prefix": "pack", "delete_files": True})
    finally:
        host.core.remove_file = original
    for task in syncs:
        # Bounded: a sync wedged on the lock must fail this test, not hang it.
        await asyncio.wait_for(task, timeout=30)

    assert out["status"] == "success"
    assert out["documents_removed"] == 3 and out["files_deleted"] == 3

    # The claim the tool made has to still be true on the very next call.
    listing = await call(host, project.name, "list_documents", {})
    assert [d for d in listing["documents"] if d["filepath"].startswith("pack/")] == []
    assert not (docs / "pack").exists()


@pg
async def test_remove_document_holds_the_lock_across_deindex_and_unlink(env):
    """Same section, single-document path. The window is narrower — one row, one
    unlink — but it is the same window, and a file still on disk between the two
    is exactly what a sync would re-index."""
    host, project, docs = env
    await call(host, project.name, "add_document",
               {"filepath": "solo/note.md", "content": "solo note"})

    held: list[bool] = []
    original = host.core.remove_file

    async def watching_remove_file(name, source):
        removed = await original(name, source)
        held.append(host.core.write_lock(name).locked())
        return removed

    host.core.remove_file = watching_remove_file
    try:
        out = await call(host, project.name, "remove_document",
                         {"filepath": "solo/note.md", "delete_file": True})
    finally:
        host.core.remove_file = original

    assert out["status"] == "success"
    assert held == [True]  # still inside the section when the row went
    assert not (docs / "solo" / "note.md").exists()


# ---------------------------------------------------------------------------
# 5.7: remove_document — the durable de-index, and the delete that no longer
# needs an index row. Reported from a connector session on 2026-08-31.
# ---------------------------------------------------------------------------


@pg
async def test_deindexing_without_deleting_is_durable(env):
    """The headline regression. Until 5.7 this call dropped the row, left the
    file, and returned status:"success" with a reindex_warning explaining that
    the watcher would undo it — so the postcondition `success` claims held for an
    unbounded short interval and then reverted. A caller branching on `status`,
    which is the obvious thing to do, was told the document was gone while search
    still returned it.

    index_project IS what the watcher runs on a filesystem event (watcher._sync),
    so calling it here is the event, without a ten-second debounce in the suite.
    """
    host, project, docs = env
    out = await call(host, project.name, "remove_document", {"filepath": "rocm.md"})
    assert out["status"] == "success" and out["file_deleted"] is False
    assert out["indexing_suppressed"] is True
    assert out["already_deindexed"] is False
    assert out["was_indexed"] is True and out["chunks_removed"] >= 1
    assert (docs / "rocm.md").is_file()  # the file is KEPT — that was the ask

    # The watcher's event, and then a forced full reindex on top of it. Neither
    # brings the document back; before 5.7 the first one did.
    await host.core.index_project(project.name, docs)
    await host.core.index_project(project.name, docs, force=True)
    listing = await call(host, project.name, "list_documents", {})
    assert "rocm.md" not in {d["filepath"] for d in listing["documents"]}
    assert (docs / "rocm.md").is_file()


@pg
async def test_the_suppression_survives_a_restart(env):
    """It is written to the project's data_dir, not held in the core — a list
    that lived only in memory would revert on the next bounce, which is the same
    self-reverting bug with a longer fuse."""
    host, project, docs = env
    await call(host, project.name, "remove_document", {"filepath": "rocm.md"})

    fresh = LocalEngineHost(CognitaConfig(), host.registry, RetrievalCore(
        host.store, HashEmbedder(DIMS), OverlapReranker()))
    assert "rocm.md" in fresh.deindexed(project).paths()
    await fresh.core.index_project(project.name, docs)
    listing = await call(fresh, project.name, "list_documents", {})
    assert "rocm.md" not in {d["filepath"] for d in listing["documents"]}


@pg
async def test_a_repeated_deindex_is_idempotent_and_says_which_it_was(env):
    host, project, _docs = env
    first = await call(host, project.name, "remove_document", {"filepath": "rocm.md"})
    second = await call(host, project.name, "remove_document", {"filepath": "rocm.md"})
    assert first["already_deindexed"] is False
    # The second call finds no row — the zero has to say which kind of zero it
    # is, so was_indexed carries it rather than the caller inferring from 0.
    assert second["status"] == "success" and second["already_deindexed"] is True
    assert second["chunks_removed"] == 0 and second["was_indexed"] is False
    assert second["indexing_suppressed"] is True


@pg
async def test_a_deindexed_file_can_still_be_deleted(env):
    """The stranding. Once the row was gone the delete returned not_found, so the
    file sat on disk, invisible to search and unreachable by every tool; the only
    documented route back was re-adding the document and removing it again."""
    host, project, docs = env
    await call(host, project.name, "remove_document", {"filepath": "rocm.md"})
    assert (docs / "rocm.md").is_file()

    out = await call(host, project.name, "remove_document",
                     {"filepath": "rocm.md", "delete_file": True})
    assert out["status"] == "success"
    assert out["file_deleted"] is True and out["was_indexed"] is False
    assert out["chunks_removed"] == 0
    assert not (docs / "rocm.md").exists()
    # And the suppression goes with the file, so a later document written to the
    # same path is not silently invisible.
    assert "rocm.md" not in host.deindexed(project).paths()


@pg
async def test_removing_a_never_indexed_file_deletes_it_and_reports_zero_chunks(env):
    """A file with an indexable extension that no walk has reached yet — the
    same shape as the de-indexed case, arrived at without a remove_document."""
    host, project, docs = env
    (docs / "stray.md").write_text("never indexed", encoding="utf-8")

    out = await call(host, project.name, "remove_document",
                     {"filepath": "stray.md", "delete_file": True})
    assert out["status"] == "success"
    assert out["chunks_removed"] == 0 and out["was_indexed"] is False
    assert out["file_deleted"] is True
    assert not (docs / "stray.md").exists()


@pg
async def test_a_path_with_neither_row_nor_file_is_still_not_found(env):
    """The one case that must NOT change: idempotency.py and the self-test plan
    both pin not_found here, and 'delete by path' must not turn a typo into a
    success."""
    host, project, _docs = env
    for args in ({"filepath": "nope.md"}, {"filepath": "nope.md", "delete_file": True}):
        out = await call(host, project.name, "remove_document", args)
        assert out["status"] == "error" and out["reason"] == "not_found"


@pg
async def test_writing_to_a_deindexed_path_readmits_it(env):
    """The route back, and a correctness requirement rather than a courtesy: the
    walk skips suppressed sources, so a row indexed for a still-listed path would
    be swept away by the next walk — a write that reported success and then
    quietly lost its document."""
    host, project, docs = env
    await call(host, project.name, "remove_document", {"filepath": "rocm.md"})
    out = await call(host, project.name, "add_document",
                     {"filepath": "rocm.md", "content": "# ROCm\n\nback again"})
    assert out["status"] == "success"
    assert "rocm.md" not in host.deindexed(project).paths()

    await host.core.index_project(project.name, docs)  # the sweep that used to eat it
    listing = await call(host, project.name, "list_documents", {})
    assert "rocm.md" in {d["filepath"] for d in listing["documents"]}


@pg
async def test_moving_a_deindexed_file_readmits_both_ends(env):
    host, project, docs = env
    await call(host, project.name, "remove_document", {"filepath": "rocm.md"})
    out = await call(host, project.name, "move_document",
                     {"filepath": "rocm.md", "new_filepath": "moved/rocm.md"})
    assert out["status"] == "success"
    listed = host.deindexed(project).paths()
    assert "rocm.md" not in listed and "moved/rocm.md" not in listed

    await host.core.index_project(project.name, docs)
    listing = await call(host, project.name, "list_documents", {})
    assert "moved/rocm.md" in {d["filepath"] for d in listing["documents"]}


@pg
async def test_remove_document_refuses_to_delete_inside_backups(env):
    """Resolving by path instead of by row hands this tool a delete it never had.
    backups/ is where that matters: the recovery tree is never indexed, so until
    5.7 every file in it was out of reach here by ACCIDENT — and a restore point
    is the one file in the tree with no backup of its own."""
    host, project, docs = env
    victim = docs / "backups" / "DESIGN.20260829-141203.md"
    victim.parent.mkdir(parents=True, exist_ok=True)
    victim.write_text("a restore point", encoding="utf-8")

    out = await call(host, project.name, "remove_document",
                     {"filepath": "backups/DESIGN.20260829-141203.md",
                      "delete_file": True})
    assert out["status"] == "error" and out["reason"] == "invalid_path"
    assert victim.is_file()


@pg
async def test_remove_document_will_not_delete_an_extension_it_does_not_index(env):
    """The other bound on the new reach. A documents tree also holds images and
    archives Cognita deliberately does not manage, and un-stranding a .md is no
    reason to hand out a general delete primitive over them."""
    host, project, docs = env
    (docs / "photo.jpg").write_bytes(b"\xff\xd8\xff\xe0 not a document")

    out = await call(host, project.name, "remove_document",
                     {"filepath": "photo.jpg", "delete_file": True})
    assert out["status"] == "error" and out["reason"] == "unindexable_extension"
    assert out["file_deleted"] is False
    assert (docs / "photo.jpg").is_file()


@pg
async def test_file_deleted_reports_the_outcome_not_the_argument(env):
    """`file_deleted` was a straight echo of `delete_file`, so it read true
    whatever happened — the reason proxy.py has to re-check the disk behind this
    tool. Here the row exists and the file does not (index drift, or a retry of a
    delete that already landed): nothing is deleted, and saying "deleted" about
    that would be the same lie."""
    host, project, docs = env
    (docs / "rocm.md").unlink()  # out-of-band, so the row outlives the file

    out = await call(host, project.name, "remove_document",
                     {"filepath": "rocm.md", "delete_file": True})
    assert out["status"] == "success"
    assert out["file_deleted"] is False and out["file_was_on_disk"] is False
    assert out["delete_file_requested"] is True  # the argument, echoed separately
    assert out["was_indexed"] is True and out["chunks_removed"] >= 1
    # There was no file, so there are no forensics to describe.
    assert "deleted_mtime" not in out and "ghost_check" not in out
    listing = await call(host, project.name, "list_documents", {})
    assert "rocm.md" not in {d["filepath"] for d in listing["documents"]}


@pg
async def test_a_failed_delete_leaves_the_index_entry_in_place(env, monkeypatch):
    """Delete first, de-index only if it worked — _remove_directory's order, for
    its reason. The reverse leaves a live file on disk with no index row, which
    is precisely the stranding this rewrite removes, reached from the other side.
    """
    host, project, docs = env

    def refuse(self, *a, **kw):
        raise OSError(13, "Permission denied")

    monkeypatch.setattr(Path, "unlink", refuse)
    out = await call(host, project.name, "remove_document",
                     {"filepath": "rocm.md", "delete_file": True})
    assert out["status"] == "error" and out["reason"] == "delete_failed"
    assert out["file_deleted"] is False
    monkeypatch.undo()

    assert (docs / "rocm.md").is_file()
    listing = await call(host, project.name, "list_documents", {})
    assert "rocm.md" in {d["filepath"] for d in listing["documents"]}


@pg
async def test_deleting_the_file_carries_no_suppression_and_no_warning(env):
    host, project, docs = env
    out = await call(host, project.name, "remove_document",
                     {"filepath": "rocm.md", "delete_file": True})
    assert out["status"] == "success" and "reindex_warning" not in out
    assert out["indexing_suppressed"] is False
    assert out["ghost_check"] and out["deleted_mtime"]
    assert not (docs / "rocm.md").exists()


@pg
async def test_get_index_stats_reports_every_deindexed_path(env):
    """5.0 §10's rule applied to the new exclusion: the cost of any filter is a
    document missing from the corpus for a reason nobody can see, so an exclusion
    is REPORTED, not merely logged. It is also the only route back — the list
    names what to write to if a suppression was a mistake."""
    host, project, _docs = env
    await call(host, project.name, "remove_document", {"filepath": "rocm.md"})

    stats = await call(host, project.name, "get_index_stats", {})
    block = stats["stats"]["deindexed"]
    assert block["count"] == 1 and block["files"] == ["rocm.md"]
    assert "load_error" not in block
    assert block["list_file"].endswith("deindexed.json")


@pg
async def test_suppressing_every_file_empties_the_index_without_tripping_the_guard(env):
    """index_project refuses a removal sweep when the walk finds NOTHING, because
    a vacuous `source <> ALL('{}')` once destroyed a whole index on an unmounted
    documents_dir. That guard must fire on an empty WALK, not on an empty result:
    a project whose every file is deliberately suppressed is a correct empty
    corpus, and reporting it as the unmounted-disk emergency would be a false
    alarm on a state the caller asked for."""
    host, project, docs = env
    for name in ("rocm.md", "pcie.md"):
        await call(host, project.name, "remove_document", {"filepath": name})

    summary = await host.core.index_project(project.name, docs)
    assert summary["errors"] == []
    assert summary["deindexed_skipped"] == 2
    listing = await call(host, project.name, "list_documents", {})
    assert listing["documents"] == []
    assert (docs / "rocm.md").is_file() and (docs / "pcie.md").is_file()


@pg
async def test_an_unreadable_documents_dir_still_refuses_the_sweep(env):
    """The other half of the guard, pinned in the same commit that loosened it:
    a walk that finds nothing at all must still refuse to delete the index."""
    host, project, docs = env
    for name in ("rocm.md", "pcie.md"):
        (docs / name).unlink()

    summary = await host.core.index_project(project.name, docs)
    assert summary["removed"] == 0
    assert any("removal sweep skipped" in e for e in summary["errors"])
    listing = await call(host, project.name, "list_documents", {})
    assert len(listing["documents"]) == 2  # rows kept: this is a mount failure


@pg
async def test_every_error_path_names_a_machine_readable_reason(env):
    """The 2026-08-29 finding: four paths returned reason=None while the plan
    told clients to assert on `reason` and never on message text. The source
    guard is tests/test_error_reasons.py; this is the same rule enforced against
    what actually comes back over the wire, on the paths a client hits most.

    The four originals are first — each one is a real client mistake (a typo'd
    path, a re-run of a move) and each one was previously indistinguishable from
    every other failure without parsing prose.
    """
    host, project, _docs = env
    await call(host, project.name, "add_document",
               {"filepath": "reasons/here.md", "content": "here"})

    cases = [
        # (tool, arguments, expected reason)
        ("get_document", {"filepath": "reasons/nope.md"}, "not_found"),
        ("remove_document", {"filepath": "reasons/nope.md"}, "not_found"),
        ("move_document", {"filepath": "reasons/here.md",
                           "new_filepath": "reasons/here.md"}, "same_path"),
        ("move_document", {"filepath": "reasons/here.md",
                           "new_filepath": "rocm.md"}, "destination_exists"),
        # and the rest of the surface, so a regression anywhere shows up here
        ("move_document", {"filepath": "reasons/gone.md",
                           "new_filepath": "reasons/x.md"}, "not_found"),
        ("update_document", {"filepath": "reasons/gone.md", "content": "x"}, "not_found"),
        ("add_document", {"filepath": "reasons/blank.md", "content": "   "}, "invalid"),
        ("add_document", {"filepath": "", "content": "x"}, "invalid"),
        ("add_document", {"filepath": "../escape.md", "content": "x"}, "invalid_path"),
        ("add_document", {"filepath": "reasons/thing.zip", "content": "x"},
         "unindexable_extension"),
        ("search_knowledge", {"query": "   "}, "invalid"),
        ("search_similar", {"filepath": ""}, "invalid"),
        ("evaluate_retrieval", {"test_cases": "{not json"}, "invalid"),
        ("add_from_url", {"url": ""}, "invalid"),
        ("add_from_url", {"url": "ftp://x/y"}, "invalid"),
        ("list_documents", {"path_prefix": "reasons"}, "unknown_argument"),
        ("remove_directory", {"prefix": "reasons"}, "not_empty"),
        ("copy_document", {"src_filepath": "reasons/here.md",
                           "dst_filepath": "rocm.md"}, "destination_exists"),
    ]
    for tool, args, expected in cases:
        payload = await call(host, project.name, tool, args)
        assert payload["status"] == "error", f"{tool} {args} did not fail"
        assert payload.get("reason") == expected, (
            f"{tool} {args}: reason {payload.get('reason')!r}, expected {expected!r} "
            f"(message: {payload.get('message')!r})")


# ------------------------------------- 5.1: a failed write leaves nothing behind


@pg
async def test_add_document_that_parses_to_nothing_leaves_no_orphan(env):
    """A payload that passes validation but extracts to no text.

    content.strip() is non-empty, so the emptiness check lets it through; the
    extension is indexable; the bytes land. Then _extract_markdown strips the
    YAML frontmatter, parse_file returns None, and the tool answers
    parse_failed — with the file sitting on disk, orphaned, because nothing will
    ever index it. That is precisely the outcome 4.6.0's unindexable-extension
    guard exists to prevent, reached through CONTENT instead of extension.
    """
    host, project, docs = env
    out = await call(host, project.name, "add_document",
                     {"filepath": "front.md", "content": "---\ntitle: x\n---\n"})
    assert out["status"] == "error"
    assert out["reason"] == "parse_failed"
    assert out["rolled_back"] is True
    assert not (docs / "front.md").exists(), "orphan file left on disk"


@pg
async def test_update_document_that_parses_to_nothing_restores_the_file(env):
    """Same trigger on update, where the damage is worse: the disk held the new
    content, the index still held the OLD document's chunks — so
    search_knowledge and get_document served text no longer in the file — and
    the caller had been told the call failed."""
    host, project, docs = env
    await call(host, project.name, "add_document",
               {"filepath": "notes.md", "content": "# Real\n\nreal body text here\n"})
    before = (docs / "notes.md").read_bytes()

    out = await call(host, project.name, "update_document",
                     {"filepath": "notes.md", "content": "---\ntitle: x\n---\n"})
    assert out["status"] == "error"
    assert out["reason"] == "parse_failed"
    assert out["rolled_back"] is True
    assert (docs / "notes.md").read_bytes() == before, "file was not restored"

    # and the index still describes the document that is actually on disk
    got = await call(host, project.name, "get_document", {"filepath": "notes.md"})
    assert "real body text here" in got["document"]["content"]


@pg
async def test_writes_are_refused_into_the_backups_tree(env):
    """backups/ is the recovery tree. resolve_target only refuses escapes
    OUTSIDE documents_dir, the backups/ check lived only in the two directory
    tools, and backup_if_exists returns None for a path already under backups/ —
    so the mandatory-backup hook took no snapshot and raised no objection. A
    recovery point could be overwritten silently and irreversibly."""
    host, project, docs = env
    (docs / "backups").mkdir(exist_ok=True)
    (docs / "backups" / "note.20260101-120000.md").write_bytes(b"the recovery point\n")

    for tool, args in [
        ("add_document", {"filepath": "backups/new.md", "content": "x\n"}),
        ("update_document", {"filepath": "backups/note.20260101-120000.md", "content": "x\n"}),
    ]:
        out = await call(host, project.name, tool, args)
        assert out["status"] == "error", tool
        assert out["reason"] == "invalid_path", tool

    assert (docs / "backups" / "note.20260101-120000.md").read_bytes() == b"the recovery point\n"
    assert not (docs / "backups" / "new.md").exists()


@pg
async def test_update_document_refuses_a_sync_conflict_name(env):
    """add_document has always refused these; update_document did not, so
    writing to an EXISTING conflict-named file succeeded and indexed a row that
    the next reindex walk (which excludes conflict copies) silently dropped."""
    host, project, docs = env
    (docs / "notes-DESKTOP-conflict.md").write_bytes(b"# conflict copy\n\nbody\n")

    out = await call(host, project.name, "update_document",
                     {"filepath": "notes-DESKTOP-conflict.md", "content": "# new\n\nbody\n"})
    assert out["status"] == "error"
    assert out["reason"] == "sync_conflict_name"
    assert (docs / "notes-DESKTOP-conflict.md").read_bytes() == b"# conflict copy\n\nbody\n"


# ------------------------------------------- 5.1: add_from_url address policy


@pytest.mark.parametrize("url", [
    "http://127.0.0.1:8675/healthz",       # the gateway itself
    "http://localhost:8676/api/projects",  # the admin API
    "http://169.254.169.254/latest/meta-data/",  # cloud metadata
    "http://192.168.1.1/",                 # the LAN router
    "http://10.0.0.5/",
    "http://[::1]/",
])
async def test_add_from_url_refuses_non_public_addresses(url):
    """add_from_url fetches from THIS SERVER and the result is stored and
    readable back out via get_document/read_document/find_literal — so an
    unvalidated fetch is a read primitive into everything the host can reach,
    not a blind SSRF. The only check used to be the URL scheme.

    Tests the guard directly: it needs no database, and the refusal has to
    happen before any connection is attempted.
    """
    from cognita.engine_local import _UrlRefused

    with pytest.raises(_UrlRefused) as caught:
        await LocalEngineHost._assert_public_host(url)
    assert caught.value.reason == "invalid"
    assert "not a public address" in str(caught.value)
