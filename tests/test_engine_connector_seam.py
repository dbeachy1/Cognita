"""Focused tests for the 9.0 engine connector identity/policy seam."""

import asyncio
import json
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar

import httpx
import pytest
from starlette.requests import Request

from cognita.config import CognitaConfig
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

    replay = await _post(host, project.name, "move_document", args)
    assert replay == payload
    task = host._reindex_tasks.get(project.name)
    if task is not None:
        await task


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
