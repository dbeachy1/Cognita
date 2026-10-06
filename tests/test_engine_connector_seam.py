"""Focused tests for the 9.0 engine connector identity/policy seam."""

import asyncio
import base64
import hashlib
import json
import os
import threading
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar

import httpx
import pytest
from starlette.requests import Request

from cognita.config import CognitaConfig
from cognita.assets.models import AssetRecord
from cognita.assets.service import AssetService
from cognita.books.state import ProjectState
from cognita.connectors import ConnectorStore
from cognita.engine_local import LocalEngineHost
from cognita.proxy import _forward_headers
from cognita.registry import Project, Registry
from cognita.store import DocumentRecord


class _Lock:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()

    async def acquire_within(self, _timeout: float) -> bool:
        await self._lock.acquire()
        return True

    async def release(self) -> None:
        self._lock.release()

    async def __aenter__(self):
        await self._lock.acquire()
        return self

    async def __aexit__(self, *_args):
        self._lock.release()


class _ReentrantLock(_Lock):
    """Match the engine's per-task re-entrant write lock in seam tests."""

    def __init__(self) -> None:
        super().__init__()
        self._owner = None
        self._depth = 0

    async def acquire_within(self, _timeout: float) -> bool:
        task = asyncio.current_task()
        if self._owner is task:
            self._depth += 1
            return True
        await self._lock.acquire()
        self._owner = task
        self._depth = 1
        return True

    async def __aenter__(self):
        await self.acquire_within(0)
        return self

    async def __aexit__(self, *_args):
        self._release_now()

    def _release_now(self) -> None:
        self._depth -= 1
        if self._depth == 0:
            self._owner = None
            self._lock.release()

    async def release(self) -> None:
        self._release_now()


class _Core:
    def __init__(self) -> None:
        self.store = SimpleNamespace(pool=None)
        self.embedder = None
        self.reranker = None
        self.exclude_patterns = []
        self.sync_conflict_patterns = []
        self.index_calls: list[tuple[str, Path]] = []
        self._locks: dict[str, _Lock] = {}
        self._deindexed: dict[str, object] = {}

    def write_lock(self, project_name: str) -> _Lock:
        return self._locks.setdefault(project_name, _Lock())

    def effective_index_policy_for(self, _project_name: str):
        return None

    def deindexed_for(self, project_name: str):
        return self._deindexed.get(project_name)

    def set_deindexed(self, project_name: str, paths) -> None:
        self._deindexed[project_name] = paths

    async def index_project(self, project_name, documents_dir, *, force, progress):
        self.index_calls.append((project_name, documents_dir))
        progress({"total_files": 0, "processed": 0, "indexed": 0, "skipped": 0, "errors": []})
        return {
            "indexed": 0,
            "skipped": 0,
            "removed": 0,
            "errors": [],
            "total_files": 0,
            "tier_changed": 0,
            "chunks_purged": 0,
            "force": force,
        }


def _host(tmp_path: Path, *, connector_store: ConnectorStore | None = None):
    documents = tmp_path / "documents"
    documents.mkdir()
    registry = Registry(tmp_path / "registry.yaml")
    project = Project(name="KEI", documents_dir=documents, data_dir=tmp_path / "data")
    registry.add(project)
    config = CognitaConfig(
        connectors_path=tmp_path / "connectors.yaml",
        watch_enabled=False,
        pg_probe_interval_s=0,
    )
    core = _Core()
    host = LocalEngineHost(config, registry, core, connector_store=connector_store)
    return host, project, core


def _rpc(tool: str, arguments: dict | None = None) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": tool, "arguments": arguments or {}},
    }


async def _post(
    host: LocalEngineHost, project_name: str, tool: str, arguments=None, *,
    connector_id=None, project_key_project=None,
):
    transport = httpx.ASGITransport(app=host.app)
    headers = {}
    if connector_id:
        headers["x-cognita-connector-id"] = connector_id
    if project_key_project:
        headers["x-cognita-project-key-project"] = project_key_project
    async with httpx.AsyncClient(transport=transport, base_url="http://engine") as client:
        response = await client.post(
            f"/engine/{project_name}/mcp",
            json=_rpc(tool, arguments),
            headers=headers or None,
        )
    body = response.json()
    payload = json.loads(body["result"]["content"][0]["text"])
    # A connector may consume either MCP representation.  Exercise their shared
    # boundary here so an engine result cannot pass through the text mirror while
    # structuredContent remains stale, malformed, or absent.
    assert body["result"]["structuredContent"] == payload
    return payload


@pytest.mark.asyncio
async def test_list_documents_actual_engine_payload_passes_result_contract(tmp_path):
    """Exercise the engine handler through ASGI, including output validation.

    The strict schema used to describe ``id`` as an integer even though the
    engine's content-addressed ``DocumentRecord.doc_id`` has always been a
    string.  A normal listing therefore became an ``internal_error`` at the
    wire boundary before a connector could read it.
    """
    host, project, core = _host(tmp_path)
    document = project.documents_dir / "fixture.md"
    document.write_bytes(b"fixture\n")
    record = DocumentRecord(
        doc_id="abc123def456",
        source="fixture.md",
        category="general",
        format="md",
        keywords=["fixture"],
        content_hash="a" * 64,
        file_size=document.stat().st_size,
    )

    class _Store:
        async def list_documents(self, _project):
            return [record]

        async def chunk_counts(self, _project):
            return {record.doc_id: 1}

    store = _Store()
    host.store = core.store = store
    payload = await _post(
        host, project.name, "list_documents",
        {"prefix": "fixture.md", "include_hashes": True},
    )

    assert payload["status"] == "success"
    assert payload["documents"][0]["id"] == record.doc_id
    assert payload["documents"][0]["on_disk"] is True
    assert payload["result_key"] == "documents"


@pytest.mark.asyncio
async def test_find_literal_drops_plain_project_hit_excluded_while_walk_waits(tmp_path, monkeypatch):
    """A post-walk folder exclusion cannot leak a stale literal-search hit."""
    host, project, core = _host(tmp_path)
    private = project.documents_dir / "private"
    private.mkdir()
    target = private / "note.md"
    target.write_text("race-only-literal-token", encoding="utf-8")
    record = DocumentRecord(
        doc_id="literal-race", source="private/note.md", category="general",
        format="md", keywords=[], content_hash="a" * 64, file_size=target.stat().st_size,
    )

    class _Store:
        async def list_documents(self, _project):
            return [record]

    host.store = core.store = _Store()
    core.policy_for = lambda _project: SimpleNamespace(
        tier_for=lambda suffix: "general" if suffix == ".md" else None,
    )
    core.effective_index_policy_for = lambda _project: host.effective_index_policy_for(project)

    entered, release = threading.Event(), threading.Event()
    original = host._walk_literal

    def paused(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return original(*args, **kwargs)

    monkeypatch.setattr(host, "_walk_literal", paused)
    task = asyncio.create_task(_post(
        host, project.name, "find_literal", {"pattern": "race-only-literal-token"},
    ))
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        state = ProjectState.initialize(project.documents_dir)
        assert state.set_folder_rule(
            "private", False, 0, owner_key="principal:local-admin",
            project=project.name, tool="set_folder_indexing",
            operation_id="exclude-literal-race", args_sha256="c" * 64,
            result={"path": "private"},
        )[0] == "committed"
    finally:
        release.set()
    payload = await task
    assert payload["status"] == "success"
    assert payload["matches"] == []
    assert payload["total_matches"] == 0


@pytest.mark.asyncio
async def test_find_literal_rechecks_folder_policy_after_second_scan(tmp_path, monkeypatch):
    """A second awaited scan cannot publish hits excluded during that scan."""
    host, project, core = _host(tmp_path)
    records = []
    for name in ("first", "second"):
        folder = project.documents_dir / name
        folder.mkdir()
        target = folder / "note.md"
        target.write_text("second-scan-race-token", encoding="utf-8")
        records.append(DocumentRecord(
            doc_id=f"literal-{name}", source=f"{name}/note.md", category="general",
            format="md", keywords=[], content_hash="a" * 64,
            file_size=target.stat().st_size,
        ))

    class _Store:
        async def list_documents(self, _project):
            return records

    host.store = core.store = _Store()
    core.policy_for = lambda _project: SimpleNamespace(
        tier_for=lambda suffix: "general" if suffix == ".md" else None,
    )
    core.effective_index_policy_for = lambda _project: host.effective_index_policy_for(project)

    first_entered, first_release = threading.Event(), threading.Event()
    second_entered, second_release = threading.Event(), threading.Event()
    original = host._walk_literal
    calls = 0

    def pause_scans(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            first_entered.set()
            assert first_release.wait(5)
        elif calls == 2:
            second_entered.set()
            assert second_release.wait(5)
        return original(*args, **kwargs)

    monkeypatch.setattr(host, "_walk_literal", pause_scans)
    task = asyncio.create_task(_post(
        host, project.name, "find_literal", {"pattern": "second-scan-race-token"},
    ))
    try:
        assert await asyncio.to_thread(first_entered.wait, 5)
        state = ProjectState.initialize(project.documents_dir)
        assert state.set_folder_rule(
            "first", False, 0, owner_key="principal:local-admin",
            project=project.name, tool="set_folder_indexing",
            operation_id="exclude-first-literal-race", args_sha256="e" * 64,
            result={"path": "first"},
        )[0] == "committed"
        first_release.set()
        assert await asyncio.to_thread(second_entered.wait, 5)
        assert state.set_folder_rule(
            "second", False, 1, owner_key="principal:local-admin",
            project=project.name, tool="set_folder_indexing",
            operation_id="exclude-second-literal-race", args_sha256="f" * 64,
            result={"path": "second"},
        )[0] == "committed"
    finally:
        first_release.set()
        second_release.set()
    payload = await task
    assert payload["status"] == "success"
    assert payload["matches"] == []
    assert payload["total_matches"] == 0
    assert payload["files_with_matches"] == 0
    assert payload["files_scanned"] == 0
    assert payload["truncated"] is False


@pytest.mark.asyncio
async def test_search_knowledge_drops_cached_plain_project_hit_excluded_while_awaited(tmp_path):
    """A stale ordinary-search cache result is filtered at final publication."""
    host, project, core = _host(tmp_path)
    private = project.documents_dir / "private"
    private.mkdir()
    (private / "note.md").write_text("cached ordinary hit", encoding="utf-8")
    core.policy_for = lambda _project: SimpleNamespace(
        tier_for=lambda suffix: "general" if suffix == ".md" else None,
    )
    core.effective_index_policy_for = lambda _project: host.effective_index_policy_for(project)
    entered, release = asyncio.Event(), asyncio.Event()
    cached = [{
        "source": "private/note.md", "content": "cached ordinary hit",
        "category": "general", "chunk_index": 0, "score": 0.9,
    }]

    async def stale_cached_search(*_args, **_kwargs):
        entered.set()
        await release.wait()
        return [dict(hit) for hit in cached]

    core.search = stale_cached_search
    task = asyncio.create_task(_post(
        host, project.name, "search_knowledge", {"query": "cached ordinary hit"},
    ))
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        state = ProjectState.initialize(project.documents_dir)
        assert state.set_folder_rule(
            "private", False, 0, owner_key="principal:local-admin",
            project=project.name, tool="set_folder_indexing",
            operation_id="exclude-cached-search", args_sha256="d" * 64,
            result={"path": "private"},
        )[0] == "committed"
    finally:
        release.set()
    payload = await task
    assert payload["status"] == "no_results"
    assert payload["results"] == []


@pytest.mark.asyncio
async def test_search_similar_rechecks_folder_policy_after_book_refresh(tmp_path):
    """A folder exclusion during the last awaited refresh wins at publication."""
    host, project, core = _host(tmp_path)
    reference = project.documents_dir / "reference.md"
    private = project.documents_dir / "private"
    private.mkdir()
    candidate = private / "note.md"
    reference.write_text("reference", encoding="utf-8")
    candidate.write_text("similar candidate", encoding="utf-8")
    reference_record = DocumentRecord(
        doc_id="reference", source="reference.md", category="general", format="md",
        keywords=[], content_hash="a" * 64, file_size=reference.stat().st_size,
    )
    candidate_record = DocumentRecord(
        doc_id="candidate", source="private/note.md", category="general", format="md",
        keywords=[], content_hash="b" * 64, file_size=candidate.stat().st_size,
    )
    core.policy_for = lambda _project: SimpleNamespace(
        tier_for=lambda suffix: "general" if suffix == ".md" else None,
    )
    core.effective_index_policy_for = lambda _project: host.effective_index_policy_for(project)

    dense_entered, dense_release = asyncio.Event(), asyncio.Event()
    refresh_entered, refresh_release = asyncio.Event(), asyncio.Event()

    class _Store:
        async def get_document(self, _project, source):
            return reference_record if source == "reference.md" else candidate_record

        async def first_chunk_embedding(self, _project, _source):
            return [0.25]

        async def dense_search(self, *_args, **_kwargs):
            dense_entered.set()
            await dense_release.wait()
            return [SimpleNamespace(
                source="private/note.md", score=0.1, category="general",
                content="similar candidate", doc_id="candidate",
            )]

    refresh_calls = 0

    async def effective_sources(*_args, **_kwargs):
        nonlocal refresh_calls
        refresh_calls += 1
        if refresh_calls == 2:
            refresh_entered.set()
            await refresh_release.wait()
        return None, None, {}

    host.store = core.store = _Store()
    core._effective_indexed_sources = effective_sources
    task = asyncio.create_task(_post(
        host, project.name, "search_similar", {"filepath": "reference.md"},
    ))
    try:
        await asyncio.wait_for(dense_entered.wait(), timeout=5)
        dense_release.set()
        await asyncio.wait_for(refresh_entered.wait(), timeout=5)
        state = ProjectState.initialize(project.documents_dir)
        assert state.set_folder_rule(
            "private", False, 0, owner_key="principal:local-admin",
            project=project.name, tool="set_folder_indexing",
            operation_id="exclude-similar-folder-race", args_sha256="b" * 64,
            result={"path": "private"},
        )[0] == "committed"
    finally:
        dense_release.set()
        refresh_release.set()
    payload = await task
    assert payload["status"] == "no_results"
    assert payload["similar_documents"] == []


@pytest.mark.asyncio
async def test_search_similar_drops_per_file_exclusion_committed_while_query_awaits(tmp_path):
    """The final general check retains durable per-file exclusion behavior."""
    host, project, core = _host(tmp_path)
    reference = project.documents_dir / "reference.md"
    private = project.documents_dir / "private"
    private.mkdir()
    candidate = private / "note.md"
    reference.write_text("reference", encoding="utf-8")
    candidate.write_text("similar candidate", encoding="utf-8")
    reference_record = DocumentRecord(
        doc_id="reference", source="reference.md", category="general", format="md",
        keywords=[], content_hash="a" * 64, file_size=reference.stat().st_size,
    )
    candidate_record = DocumentRecord(
        doc_id="candidate", source="private/note.md", category="general", format="md",
        keywords=[], content_hash="b" * 64, file_size=candidate.stat().st_size,
    )
    core.policy_for = lambda _project: SimpleNamespace(
        tier_for=lambda suffix: "general" if suffix == ".md" else None,
    )
    core.effective_index_policy_for = lambda _project: host.effective_index_policy_for(project)
    entered, release = asyncio.Event(), asyncio.Event()

    class _Store:
        async def get_document(self, _project, source):
            return reference_record if source == "reference.md" else candidate_record

        async def first_chunk_embedding(self, _project, _source):
            return [0.25]

        async def dense_search(self, *_args, **_kwargs):
            entered.set()
            await release.wait()
            return [SimpleNamespace(
                source="private/note.md", score=0.1, category="general",
                content="similar candidate", doc_id="candidate",
            )]

    async def effective_sources(*_args, **_kwargs):
        return None, None, {}

    host.store = core.store = _Store()
    core._effective_indexed_sources = effective_sources
    task = asyncio.create_task(_post(
        host, project.name, "search_similar", {"filepath": "reference.md"},
    ))
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        assert host.deindexed(project).add("private/note.md")
    finally:
        release.set()
    payload = await task
    assert payload["status"] == "no_results"
    assert payload["similar_documents"] == []
    assert host.deindexed(project).path.is_file()


@pytest.mark.asyncio
async def test_project_file_listing_reports_actual_index_and_blocked_outage(tmp_path):
    """Storage listing distinguishes a derived fact from a missing index row."""
    host, project, core = _host(tmp_path)
    (project.documents_dir / "note.md").write_text("fixture", encoding="utf-8")

    class IndexedStore:
        async def indexed_source_paths(self, _project):
            return {"note.md"}

    host.store = core.store = IndexedStore()
    service = host.book_service_for(project)
    direct = service.list_files(
        "", recursive=True, effective_index=host.effective_index_policy_for(project),
        effective_read_only=lambda rel: host._listing_read_only_for(project, rel, None, None),
        index_state=host._listing_index_state_for(project, service, {"note.md"}),
    )
    assert direct["entries"]
    from cognita.books.schemas import success_envelope
    success_envelope("list_project_files", direct)
    indexed = await _post(host, project.name, "list_project_files", {
        "project": project.name, "path": "", "recursive": True,
    })
    note = next(entry for entry in indexed["data"]["entries"] if entry["path"] == "note.md")
    assert note["index_state"] == "indexed"
    assert note["effective_read_only"] is False

    class UnavailableStore:
        async def indexed_source_paths(self, _project):
            raise RuntimeError("database unavailable")

    host.store = core.store = UnavailableStore()
    blocked = await _post(host, project.name, "list_project_files", {
        "project": project.name, "path": "", "recursive": True,
    })
    note = next(entry for entry in blocked["data"]["entries"] if entry["path"] == "note.md")
    assert note["index_state"] == "blocked"
    assert note["error"] == {"code": "index_unavailable", "message": "derived index is unavailable"}

    project.writable = False
    readonly = await _post(host, project.name, "list_project_files", {
        "project": project.name, "path": "", "recursive": True,
    })
    note = next(entry for entry in readonly["data"]["entries"] if entry["path"] == "note.md")
    assert note["effective_read_only"] is True


@pytest.mark.asyncio
async def test_remove_document_actual_engine_payload_passes_result_contract(tmp_path):
    """A successful delete with ghost forensics survives result validation."""
    host, project, core = _host(tmp_path)
    document = project.documents_dir / "fixture.md"
    document.write_bytes(b"fixture\n")
    record = DocumentRecord(
        doc_id="abc123def456",
        source="fixture.md",
        category="general",
        format="md",
        content_hash="a" * 64,
        file_size=document.stat().st_size,
    )

    class _Store:
        async def chunk_count(self, _project, _source):
            return 1

        async def get_document(self, _project, _source):
            return record

    async def remove_file(_project, _source):
        return None

    class _Deindexed:
        def discard(self, _source):
            return False

    store = _Store()
    host.store = core.store = store
    core._locks[project.name] = _ReentrantLock()
    core.remove_file = remove_file
    host.deindexed = lambda _project: _Deindexed()
    payload = await _post(
        host, project.name, "remove_document",
        {"filepath": "fixture.md", "delete_file": True},
    )

    assert payload["status"] == "success"
    assert payload["file_deleted"] is True
    assert isinstance(payload["ghost_check"], str)
    assert not document.exists()


@pytest.mark.asyncio
async def test_move_document_actual_engine_payload_passes_result_contract(tmp_path):
    """Content-addressed IDs also remain strings in move receipts."""
    host, project, core = _host(tmp_path)
    source = project.documents_dir / "source.md"
    source.write_bytes(b"fixture\n")

    async def move_file(_project, _docs, _old, _new):
        return "abc123def456", 1

    core.move_file = move_file
    core.policy_for = lambda _project: SimpleNamespace(
        tier_for=lambda _suffix: "embedded",
        all_extensions={".md"},
    )
    core._locks[project.name] = _ReentrantLock()
    payload = await _post(
        host, project.name, "move_document",
        {"filepath": "source.md", "new_filepath": "moved.md"},
    )

    assert payload["status"] == "success"
    assert payload["doc_id"] == "abc123def456"
    assert not source.exists()
    assert (project.documents_dir / "moved.md").exists()


@pytest.mark.asyncio
async def test_directory_move_preserves_explicit_rules_and_per_file_exclusions(tmp_path):
    """The directory variant carries only durable policy decisions, not backups."""
    host, project, core = _host(tmp_path)
    source = project.documents_dir / "source"
    (source / "nested").mkdir(parents=True)
    (source / "nested" / "note.md").write_text("fixture", encoding="utf-8")
    core._locks[project.name] = _ReentrantLock()
    state = host.project_state_for(project)
    assert state is None
    state = ProjectState.initialize(project.documents_dir)
    assert state.set_folder_rule(
        "source", False, 0, owner_key="principal:local-admin", project=project.name,
        tool="set_folder_indexing", operation_id="disable-source", args_sha256="a" * 64,
        result={"path": "source"},
    )[0] == "committed"
    host.deindexed(project).add("source/nested/note.md")

    args = {
        "filepath": "source", "new_filepath": "archive/source",
        "expected_policy_revision": 1, "operation_id": "move-source-1",
    }
    payload = await _post(host, project.name, "move_document", args)
    assert payload == {
        "status": "success", "filepath": "source", "new_filepath": "archive/source",
        "kind": "directory", "policy_revision": 2,
        "indexing": {"state": "pending", "job_id": payload["indexing"]["job_id"]},
    }
    assert not source.exists()
    assert (project.documents_dir / "archive" / "source" / "nested" / "note.md").is_file()
    assert host.deindexed(project).sorted() == ["archive/source/nested/note.md"]
    assert state.folder_policy().rules == (("archive/source", False),)

    # A process can die after the receipt transaction but before the final
    # journal phase.  Reopen proves the receipt and owned target facts, then
    # completes the journal without attempting to rename the directory back.
    journal_id = "directory-move:" + hashlib.sha256(
        (project.name + "\0principal:local-admin\0move-source-1").encode("utf-8")
    ).hexdigest()
    state.advance_publication(journal_id, "deindexed_published")
    reopened_core = _Core()
    reopened_core._locks[project.name] = _ReentrantLock()
    reopened = LocalEngineHost(host.config, host.registry, reopened_core)
    await reopened._recover_directory_move_publications(project)
    assert state.publication(journal_id)["phase"] == "committed"
    assert (project.documents_dir / "archive" / "source").is_dir()

    replay = await _post(host, project.name, "move_document", args)
    assert replay == payload
    task = host._reindex_tasks.get(project.name)
    if task is not None:
        await task


@pytest.mark.asyncio
async def test_directory_move_refuses_plain_project_storage_authority_before_mutation(tmp_path):
    """The folder-policy database is authority even without a book layout."""
    host, project, core = _host(tmp_path)
    core._locks[project.name] = _ReentrantLock()
    state = ProjectState.initialize(project.documents_dir)
    assert state.set_folder_rule(
        "private", False, 0, owner_key="principal:local-admin", project=project.name,
        tool="set_folder_indexing", operation_id="exclude-private", args_sha256="b" * 64,
        result={"path": "private"},
    )[0] == "committed"
    (project.documents_dir / "private").mkdir()
    for old, new in (
        (".cognita-storage", "archived-state"),
        ("private", ".cognita-storage/replaced"),
    ):
        result = await _post(host, project.name, "move_document", {
            "filepath": old, "new_filepath": new, "expected_policy_revision": 1,
            "operation_id": f"refuse-{old.replace('/', '-')}",
        })
        assert result["status"] == "error" and result["reason"] == "permission_denied"
    assert (project.documents_dir / ".cognita-storage" / "state.sqlite").is_file()
    reopened = ProjectState.discover(project.documents_dir)
    assert reopened is not None and reopened.folder_policy().rules == (("private", False),)


@pytest.mark.asyncio
async def test_directory_move_rebases_live_asset_catalog_identity_before_reconcile(tmp_path):
    """A real directory rename retains an existing catalog row rather than recreating it."""
    host, project, core = _host(tmp_path)
    core._locks[project.name] = _ReentrantLock()
    source = project.documents_dir / "art"
    source.mkdir()
    (source / "cover.png").write_bytes(b"not decoded by this move test")
    asset = AssetRecord(
        asset_id="asset-kept", filepath="art/cover.png",
        metadata={"title": "Catalog-only title", "tags": ["kept"]},
        received_size=1, received_sha256="a" * 64, final_size=1,
        final_sha256="a" * 64, width=1, height=1,
        metadata_storage="catalog", metadata_revision=7,
        provenance_state="cabx_present_unverified", cabx_chunk_count=2,
    )
    assets = AssetService(project)
    assets._memory[asset.filepath] = asset
    host._asset_services[(project.name, None)] = assets
    ProjectState.initialize(project.documents_dir)

    payload = await _post(host, project.name, "move_document", {
        "filepath": "art", "new_filepath": "archive/art",
        "expected_policy_revision": 0, "operation_id": "rebase-assets",
    })
    assert payload["status"] == "success"
    assert (project.documents_dir / "archive" / "art" / "cover.png").is_file()
    assert "art/cover.png" not in assets._memory
    preserved = assets._memory["archive/art/cover.png"]
    assert preserved is asset
    assert (preserved.asset_id, preserved.metadata["title"], preserved.metadata_revision,
            preserved.provenance_state) == (
        "asset-kept", "Catalog-only title", 7, "cabx_present_unverified",
    )
    task = host._reindex_tasks.get(project.name)
    if task is not None:
        await task


@pytest.mark.asyncio
async def test_directory_move_reports_failed_asset_rebase_and_restores_directory(tmp_path):
    """A failed catalog transaction cannot be reported as a successful move."""
    host, project, core = _host(tmp_path)
    core._locks[project.name] = _ReentrantLock()
    source = project.documents_dir / "art"
    source.mkdir()
    (source / "cover.png").write_bytes(b"fixture")
    ProjectState.initialize(project.documents_dir)

    class FailingAssets:
        async def rebase_directory_sources(self, old, _new):
            if old == "art":
                raise RuntimeError("simulated catalog failure")
            return 0

        def apply_directory_source_rebase(self, _old, _new):
            raise AssertionError("failed primary rebase must not update a cache")

    host._asset_services[(project.name, None)] = FailingAssets()
    payload = await _post(host, project.name, "move_document", {
        "filepath": "art", "new_filepath": "archive/art",
        "expected_policy_revision": 0, "operation_id": "failing-rebase",
    })
    assert payload["status"] == "error"
    assert payload["reason"] == "asset_rebase_failed"
    assert payload["details"]["rolled_back"] is True
    assert source.is_dir()
    assert not (project.documents_dir / "archive" / "art").exists()
    state = ProjectState.discover(project.documents_dir)
    assert state is not None and state.folder_policy().policy_revision == 0
    assert not state.pending_publications("directory_move")


@pytest.mark.asyncio
async def test_directory_move_recovers_interrupted_rename_before_same_id_retry(tmp_path):
    """A reopened host restores owned facts before it classifies the old path."""
    host, project, core = _host(tmp_path)
    core._locks[project.name] = _ReentrantLock()
    source = project.documents_dir / "source"
    (source / "nested").mkdir(parents=True)
    (source / "nested" / "note.md").write_text("fixture", encoding="utf-8")
    state = ProjectState.initialize(project.documents_dir)
    state.set_folder_rule(
        "source", False, 0, owner_key="principal:local-admin", project=project.name,
        tool="set_folder_indexing", operation_id="disable-source", args_sha256="a" * 64,
        result={"path": "source"},
    )
    legacy = host.deindexed(project)
    legacy.add("source/nested/note.md")
    old_bytes = legacy.owned_bytes()
    staged, _ = legacy.rebased_paths("source", "archive/source")
    new_bytes = legacy.serialized(staged)
    args = {
        "filepath": "source", "new_filepath": "archive/source",
        "expected_policy_revision": 1, "operation_id": "interrupted-move",
    }
    args_sha256 = hashlib.sha256(json.dumps({
        "filepath": "source", "new_filepath": "archive/source",
        "expected_policy_revision": 1,
    }, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    journal_id = "directory-move:test-interrupted"
    state.begin_publication(journal_id, "directory_move", {
        "old": "source", "new": "archive/source",
        "owner_key": "principal:local-admin", "operation_id": args["operation_id"],
        "args_sha256": args_sha256,
        "old_deindexed": base64.b64encode(old_bytes).decode(),
        "old_deindexed_sha256": hashlib.sha256(old_bytes).hexdigest(),
        "new_deindexed": base64.b64encode(new_bytes).decode(),
        "new_deindexed_sha256": hashlib.sha256(new_bytes).hexdigest(),
        "directory_identity": list((source.stat().st_dev, source.stat().st_ino)),
    })
    target = project.documents_dir / "archive" / "source"
    target.parent.mkdir()
    os.replace(source, target)
    state.advance_publication(journal_id, "renamed")
    legacy.publish_owned_bytes(hashlib.sha256(old_bytes).hexdigest(), new_bytes)
    state.advance_publication(journal_id, "deindexed_published")

    # The durable prepared journal is a hard temporary exclusion for both
    # prefixes, before a watcher or query can publish either one.
    assert host.effective_index_policy_for(project).decision("source/nested/note.md").indexed is False
    assert host.effective_index_policy_for(project).decision("archive/source/nested/note.md").indexed is False

    reopened_core = _Core()
    reopened_core._locks[project.name] = _ReentrantLock()
    reopened = LocalEngineHost(host.config, host.registry, reopened_core)
    await reopened._recover_directory_move_publications(project)
    assert source.is_dir()
    assert not target.exists()
    assert reopened.deindexed(project).sorted() == ["source/nested/note.md"]
    assert not state.pending_publications("directory_move")

    replay = await _post(reopened, project.name, "move_document", args)
    assert replay["status"] == "success"
    assert target.is_dir()
    assert reopened.deindexed(project).sorted() == ["archive/source/nested/note.md"]
    task = reopened._reindex_tasks.get(project.name)
    if task is not None:
        await task


@pytest.mark.asyncio
async def test_directory_move_failed_rename_leaves_exact_old_per_file_decision(tmp_path, monkeypatch):
    """The journal is staged, but a failed rename cannot publish rebased paths."""
    host, project, core = _host(tmp_path)
    core._locks[project.name] = _ReentrantLock()
    source = project.documents_dir / "source"
    source.mkdir()
    (source / "note.md").write_text("fixture", encoding="utf-8")
    state = ProjectState.initialize(project.documents_dir)
    state.set_folder_rule(
        "source", False, 0, owner_key="principal:local-admin", project=project.name,
        tool="set_folder_indexing", operation_id="disable-source", args_sha256="a" * 64,
        result={"path": "source"},
    )
    host.deindexed(project).add("source/note.md")
    original_replace = os.replace

    def fail_directory_rename(old, new):
        if Path(old) == source:
            raise OSError("simulated rename failure")
        return original_replace(old, new)

    monkeypatch.setattr("cognita.engine_documents.os.replace", fail_directory_rename)
    payload = await _post(host, project.name, "move_document", {
        "filepath": "source", "new_filepath": "archive/source",
        "expected_policy_revision": 1, "operation_id": "failed-move",
    })
    assert payload["status"] == "error"
    assert source.is_dir()
    assert not (project.documents_dir / "archive" / "source").exists()
    assert host.deindexed(project).sorted() == ["source/note.md"]
    assert state.folder_policy().rules == (("source", False),)
    assert not state.pending_publications("directory_move")


@pytest.mark.asyncio
async def test_copy_document_actual_engine_payload_includes_disk_fact(tmp_path):
    """Copy receipts expose the observed on-disk postcondition."""
    host, project, core = _host(tmp_path)
    source = project.documents_dir / "source.md"
    source.write_bytes(b"fixture\n")

    class _Store:
        async def get_document(self, _project, _source):
            return None

    async def index_file(*_args, **_kwargs):
        return "abc123def456", 1

    core.store = host.store = _Store()
    core._locks[project.name] = _ReentrantLock()
    core.index_file = index_file
    core.policy_for = lambda _project: SimpleNamespace(tier_for=lambda _suffix: "embedded")
    payload = await _post(
        host, project.name, "copy_document",
        {"src_filepath": "source.md", "dst_filepath": "copy.md"},
    )

    assert payload["status"] == "success"
    assert payload["on_disk"] is True
    assert payload["filepath"] == "copy.md"
    assert (project.documents_dir / "copy.md").read_bytes() == b"fixture\n"


@pytest.mark.asyncio
async def test_copy_directory_actual_engine_payload_includes_disk_fact(tmp_path):
    """The directory adapter uses the same validated per-file receipt."""
    host, project, core = _host(tmp_path)
    source_dir = project.documents_dir / "source"
    source_dir.mkdir()
    (source_dir / "fixture.md").write_bytes(b"fixture\n")

    class _Store:
        async def get_document(self, _project, _source):
            return None

    class _BulkJob:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

    async def index_file(*_args, **_kwargs):
        return "abc123def456", 1

    core.store = host.store = _Store()
    core._locks[project.name] = _ReentrantLock()
    core.index_file = index_file
    core.policy_for = lambda _project: SimpleNamespace(tier_for=lambda _suffix: "embedded")
    core.bulk_gpu_job = lambda *_args, **_kwargs: _BulkJob()
    payload = await _post(
        host, project.name, "copy_directory",
        {"src_prefix": "source", "dst_prefix": "copy"},
    )

    assert payload["status"] == "success"
    assert payload["documents"][0]["on_disk"] is True
    assert payload["result_key"] == "documents"


@pytest.mark.asyncio
async def test_remove_documents_actual_child_error_passes_result_contract(tmp_path):
    """Batched removal preserves the single-file error facts it nests."""
    host, project, core = _host(tmp_path)
    unsupported = project.documents_dir / "fixture.zip"
    unsupported.write_bytes(b"fixture")

    class _Store:
        async def chunk_count(self, _project, _source):
            return 0

        async def get_document(self, _project, _source):
            return None

    class _Policy:
        def __init__(self):
            self.all_extensions = {".md"}

        def tier_for(self, _suffix):
            return None

    core.store = host.store = _Store()
    core._locks[project.name] = _ReentrantLock()
    core.policy_for = lambda _project: _Policy()
    host.deindexed = lambda _project: SimpleNamespace()
    payload = await _post(
        host, project.name, "remove_documents",
        {"filepaths": ["fixture.zip"], "delete_file": False},
    )

    assert payload["status"] == "partial_failure"
    child = payload["documents"][0]
    assert child["status"] == "error"
    assert child["error"]["reason"] == "unindexable_extension"
    assert child["error"]["file_deleted"] is False
    assert child["error"]["source"].endswith("fixture.zip")
    assert unsupported.exists()


class _FakeAssetService:
    created: ClassVar[list[str | None]] = []

    def __init__(self, project, _repository=None, **kwargs):
        self.project_name = project.name
        self.connector_id = kwargs["connector_id"]
        self.created.append(self.connector_id)

    async def list_assets(self, _args):
        # The connector id is an internal service-construction detail. Keep the
        # tool result on the public list_assets contract while checking the
        # trusted identity through the cache and constructor observations below.
        return {"status": "success", "project": self.project_name,
                "assets": [], "next_cursor": None}


def test_project_key_grant_header_cannot_be_spoofed_by_client():
    request = Request({
        "type": "http",
        "method": "POST",
        "path": "/mcp",
        "headers": [
            (b"x-cognita-connector-id", b"spoofed-connector"),
            (b"x-cognita-project-key-project", b"SpoofedProject"),
            (b"x-client-header", b"preserved"),
        ],
    })
    request.state.cognita_connector_id = "trusted-connector"
    request.state.cognita_project_key_project = "DeepSeek"

    forwarded = _forward_headers(request)

    assert forwarded["x-cognita-connector-id"] == "trusted-connector"
    assert forwarded["x-cognita-project-key-project"] == "DeepSeek"
    assert forwarded["x-client-header"] == "preserved"


@pytest.mark.asyncio
async def test_asset_service_cache_uses_trusted_connector_header(monkeypatch, tmp_path):
    from cognita import engine_local

    _FakeAssetService.created = []
    monkeypatch.setattr(engine_local, "AssetService", _FakeAssetService)
    host, project, _core = _host(tmp_path)

    connector_a = str(uuid.uuid4())
    connector_b = str(uuid.uuid4())
    first = await _post(host, project.name, "list_assets", connector_id=connector_a)
    again = await _post(host, project.name, "list_assets", connector_id=connector_a)
    other = await _post(host, project.name, "list_assets", connector_id=connector_b)

    assert first == {"status": "success", "project": project.name,
                     "assets": [], "next_cursor": None}
    assert again == first
    assert other == first
    assert _FakeAssetService.created == [connector_a, connector_b]
    assert set(host._asset_services) == {
        (project.name, connector_a),
        (project.name, connector_b),
    }


@pytest.mark.asyncio
async def test_asset_backfill_and_watcher_race_share_project_lock(tmp_path):
    from cognita.watcher import WatcherManager

    class ObservedLock(_Lock):
        """The project lock, reporting when a second claimant arrives while
        it is held."""

        def __init__(self, contender_arrived: asyncio.Event) -> None:
            super().__init__()
            self.contender_arrived = contender_arrived

        async def __aenter__(self):
            if self._lock.locked():
                self.contender_arrived.set()
            return await super().__aenter__()

    contender_arrived = asyncio.Event()

    class AssetService:
        def __init__(self):
            self.active = 0
            self.maximum = 0
            self.calls = []

        async def _run(self, kind):
            self.active += 1
            self.maximum = max(self.maximum, self.active)
            self.calls.append(kind)
            if len(self.calls) == 1:
                # Was a real 10 ms sleep hoping the watcher would arrive in
                # the meantime. Now the first run holds the section until the
                # watcher has observably arrived: either blocked on the shared
                # project lock (correct), or inside _run itself (the bug,
                # which pushes `maximum` to 2).
                await asyncio.wait_for(contender_arrived.wait(), timeout=5)
            else:
                contender_arrived.set()
            self.active -= 1
            return {"indexed": 1, "removed": 0, "errors": []}

        async def reconcile_all(self):
            return await self._run("startup")

        async def reconcile_paths(self, paths):
            assert paths == ["existing.png"]
            return await self._run("watcher")

    host, project, core = _host(tmp_path)
    core._locks[project.name] = ObservedLock(contender_arrived)
    service = AssetService()
    host._asset_services[(project.name, None)] = service
    watcher = WatcherManager(core, debounce_s=0, asset_services={project.name: service})
    watcher._docs_dirs[project.name] = Path(project.documents_dir)

    assert host.start_background_asset_reconcile(project) is True
    assert host.start_background_asset_reconcile(project) is False
    watcher_task = asyncio.create_task(watcher._sync(project.name, {"existing.png": "closed"}))
    await asyncio.wait_for(
        asyncio.gather(host._asset_reconcile_tasks[project.name], watcher_task),
        timeout=5,
    )

    assert service.calls == ["startup", "watcher"]
    assert service.maximum == 1
    assert host.start_background_asset_reconcile(project) is True
    await asyncio.wait_for(host._asset_reconcile_tasks[project.name], timeout=5)


@pytest.mark.parametrize(
    "change",
    [
        "read_only",
        "disable_connector",
        "delete_connector",
        "disable_project",
        "exclude_defaults",
        "remove_access",
    ],
)
@pytest.mark.asyncio
async def test_reindex_rechecks_current_connector_policy_before_indexing(tmp_path, change):
    connector_path = tmp_path / "connectors.yaml"
    store = ConnectorStore(connector_path)
    host, project, core = _host(tmp_path, connector_store=store)
    connector = store.create(
        expected_revision=0,
        name="Cognita",
        project_names=[project.name],
    ).connectors[0]

    started = await _post(
        host,
        project.name,
        "reindex_documents",
        {"force": True},
        connector_id=connector.id,
    )
    assert started["status"] == "started"

    if change == "delete_connector":
        store.delete(connector.id, expected_revision=1)
    elif change == "disable_project":
        project.enabled = False
    elif change == "exclude_defaults":
        host.registry.update_settings(
            project.name, exclude_from_default_permissions=True,
        )
    elif change == "remove_access":
        store.update(
            connector.id,
            expected_revision=1,
            project_names=[project.name],
            project_mode="selected",
            project_access={},
        )
    else:
        store.update(
            connector.id,
            expected_revision=1,
            project_names=[project.name],
            **(
                {"enabled": False}
                if change == "disable_connector"
                else {"default_access": "read"}
            ),
        )
    await asyncio.sleep(0)

    assert core.index_calls == []
    assert host._reindex_progress[project.name]["active"] is False
    assert "denied" in host._reindex_progress[project.name]["error"]


@pytest.mark.asyncio
async def test_reindex_fails_closed_when_current_policy_is_malformed(tmp_path):
    connector_path = tmp_path / "connectors.yaml"
    store = ConnectorStore(connector_path)
    host, project, core = _host(tmp_path, connector_store=store)
    connector = store.create(
        expected_revision=0,
        name="Cognita",
        project_names=[project.name],
    ).connectors[0]
    connector_path.write_text("version: [malformed", encoding="utf-8")

    result = await _post(
        host,
        project.name,
        "reindex_documents",
        connector_id=connector.id,
    )

    assert result == {
        "status": "error",
        "reason": "policy_unavailable",
        "message": "Connector policy is unavailable for background reindex.",
    }
    assert core.index_calls == []


@pytest.mark.asyncio
async def test_engine_rechecks_connector_write_policy_for_every_mutation(tmp_path):
    store = ConnectorStore(tmp_path / "connectors.yaml")
    host, project, _core = _host(tmp_path, connector_store=store)
    connector = store.create(
        expected_revision=0,
        name="Read only",
        default_access="read",
        project_names=[project.name],
    ).connectors[0]

    result = await _post(
        host,
        project.name,
        "remove_document",
        {"filepath": "missing.md", "delete_file": True},
        connector_id=connector.id,
    )

    assert result == {
        "status": "error", "reason": "read_only",
        "message": "The connector has read-only access to this project.",
    }


def test_engine_honors_trusted_project_key_grant_for_excluded_project(tmp_path):
    store = ConnectorStore(tmp_path / "connectors.yaml")
    host, project, _core = _host(tmp_path, connector_store=store)
    host.registry.update_settings(
        project.name, exclude_from_default_permissions=True,
    )
    connector = store.create(
        expected_revision=0,
        name="Cognita",
        project_names=[project.name],
    ).connectors[0]

    current_project = host.registry.get(project.name)
    denied = host._connector_write_denial(
        current_project, connector.id,
    )
    granted = host._connector_write_denial(
        current_project, connector.id, project.name,
    )

    assert denied["reason"] == "project_unavailable"
    assert granted is None
