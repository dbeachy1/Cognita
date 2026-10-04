"""Source deletion snapshots and repeatable bulk receipts, without live PostgreSQL.

Only the store is replaced: the host, retrieval write lock, filesystem, backup
copy/lookup and de-index list are real. No models or live services are started.
"""

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from cognita import backups, engine_documents, engine_transfers
from cognita.auth_policy import SELF_TEST_API_KEY
from cognita.backups import backup_if_exists, find_backup, list_backup_entries
from cognita.connectors import ConnectorStore
from cognita.config import CognitaConfig
from cognita.engine_local import LocalEngineHost
from cognita.gateway import create_gateway_app
from cognita.registry import Project, Registry
from cognita.retrieval import RetrievalCore
from retrieval_fakes import HashEmbedder, OverlapReranker


class _DocumentStore:
    def __init__(self):
        self.rows = {}
        self.deleted = []

    async def get_document(self, project, source):
        return self.rows.get(source)

    async def chunk_count(self, project, source):
        row = self.rows.get(source)
        return row.chunks if row else 0

    async def list_documents(self, project):
        return list(self.rows.values())

    async def delete_document(self, project, source):
        self.deleted.append(source)
        return self.rows.pop(source, None) is not None


@pytest.fixture
def env(tmp_path):
    docs = tmp_path / "documents"
    docs.mkdir()
    project = Project(name="Self-Test", documents_dir=docs, data_dir=tmp_path / "data")
    registry = Registry(tmp_path / "registry.yaml")
    registry.add(project)
    store = _DocumentStore()
    core = RetrievalCore(store, HashEmbedder(), OverlapReranker())
    config = CognitaConfig(data_root=tmp_path, connectors_path=tmp_path / "connectors.yaml",
                           backup_keep_per_file=0)
    return LocalEngineHost(config, registry, core), project, docs, store


async def call(env, tool, arguments):
    host, project, _docs, _store = env
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=host.app),
                                base_url="http://test") as client:
        response = await client.post(f"/engine/{project.name}/mcp", json={
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": tool, "arguments": arguments},
        })
    assert response.status_code == 200
    result = response.json()["result"]
    payload = json.loads(result["content"][0]["text"])
    assert payload == result["structuredContent"]
    assert result["isError"] == (payload["status"] == "error")
    return payload


@pytest.fixture
async def gateway_env(env, monkeypatch):
    host, project, docs, store = env
    connectors = ConnectorStore(host.config.connectors_path)
    connectors.create(expected_revision=0, name="Synthetic delete regression",
                      project_names=[project.name])
    host.connector_store = connectors
    config = host.config.model_copy(update={"self_test_mode": True})
    # Own and close the real in-process engine client; no runtime is started.
    async with host.make_client() as upstream:
        monkeypatch.setattr(host, "make_client", lambda: upstream)
        app = create_gateway_app(config, host.registry, engine=host, connector_store=connectors)
        connector = connectors.snapshot().connectors[0]
        yield app, connector.slug, host, project, docs, store


async def gateway_call(gateway_env, tool, arguments):
    app, slug, _host, project, _docs, _store = gateway_env
    # This built-in public test key is accepted only in test mode for Self-Test.
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                base_url="http://test") as client:
        response = await client.post(f"/mcp/connectors/{slug}/mcp", json={
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": tool, "arguments": (
                arguments if tool == "batch" else {"project": project.name, **arguments}
            )},
        }, headers={"Authorization": "Bearer " + SELF_TEST_API_KEY})
    assert response.status_code == 200
    result = response.json()["result"]
    payload = json.loads(result["content"][0]["text"])
    assert payload == result["structuredContent"]
    if payload["status"] in {"success", "error"}:
        assert result["isError"] == (payload["status"] == "error")
    return payload


@pytest.mark.parametrize("state", ["never_indexed", "deindexed", "indexed"])
async def test_proxy_engine_delete_and_replay_create_exactly_one_snapshot(gateway_env, state):
    _app, _slug, host, project, docs, store = gateway_env
    original = b"# Synthetic bridge\r\n\r\nexact source\r\n"
    (docs / "alpha.txt").write_bytes(original)
    if state == "indexed":
        store.rows["alpha.txt"] = SimpleNamespace(source="alpha.txt", chunks=1)
    elif state == "deindexed":
        host.deindexed(project).add("alpha.txt")
    arguments = {"filepath": "alpha.txt", "delete_file": True, "operation_id": "owned-delete"}
    out = await gateway_call(gateway_env, "remove_document", arguments)
    assert out["status"] == "success" and out["file_deleted"] is True
    assert out["was_indexed"] is (state == "indexed")
    assert len(list_backup_entries(docs, "alpha.txt")) == 1
    assert find_backup(docs, "alpha.txt", out["previous_backup_id"]).read_bytes() == original
    replay = await gateway_call(gateway_env, "remove_document", arguments)
    assert replay["replayed"] is True and replay["previous_backup_id"] == out["previous_backup_id"]
    assert len(list_backup_entries(docs, "alpha.txt")) == 1
    assert not (docs / "alpha.txt").exists()
    assert "alpha.txt" not in store.rows and "alpha.txt" not in host.deindexed(project).paths()


async def test_proxy_deindex_missing_and_batched_deletion_preserve_receipt_semantics(gateway_env):
    _app, _slug, host, project, docs, store = gateway_env
    (docs / "kept.md").write_bytes(b"synthetic retained source")
    store.rows["kept.md"] = SimpleNamespace(source="kept.md", chunks=1)
    out = await gateway_call(gateway_env, "remove_document", {"filepath": "kept.md"})
    assert out["status"] == "success" and out["file_deleted"] is False
    assert (docs / "kept.md").is_file() and "kept.md" in host.deindexed(project).paths()
    assert "previous_backup_id" not in out and list_backup_entries(docs) == []
    for path in ("one.md", "two.md"):
        (docs / path).write_bytes(path.encode())
    out = await gateway_call(gateway_env, "batch", {
        "calls": [{"tool": "remove_document", "arguments": {
            "project": project.name, "filepath": path, "delete_file": True,
        }} for path in ("one.md", "absent.md", "two.md")], "on_error": "continue",
    })
    assert out["status"] == "partial_failure"
    assert (out["succeeded"], out["failed"], out["skipped"]) == (2, 1, 0)
    children = [entry["result"]["structuredContent"] for entry in out["results"]]
    assert children[1]["status"] == "error" and children[1]["reason"] == "not_found"
    assert len(list_backup_entries(docs)) == 2
    for path, child in zip(("one.md", "two.md"), (children[0], children[2])):
        assert child["file_deleted"] is True
        assert len(list_backup_entries(docs, path)) == 1
        assert find_backup(docs, path, child["previous_backup_id"]).read_bytes() == path.encode()


@pytest.mark.parametrize("indexed", [False, True])
async def test_proxy_delete_backup_failure_is_fail_closed(gateway_env, monkeypatch, indexed):
    _app, _slug, host, project, docs, store = gateway_env
    (docs / "alpha.txt").write_bytes(b"synthetic preserved source")
    if indexed:
        store.rows["alpha.txt"] = SimpleNamespace(source="alpha.txt", chunks=1)
    else:
        host.deindexed(project).add("alpha.txt")
    def refused(*args, **kwargs):
        raise backups.BackupError("synthetic backup refusal")
    monkeypatch.setattr(engine_documents, "backup_if_exists", refused)
    out = await gateway_call(gateway_env, "remove_document", {"filepath": "alpha.txt", "delete_file": True})
    assert out["status"] == "error" and out["reason"] == "backup_failed"
    assert out["file_deleted"] is False and out["chunks_removed"] == 0
    assert (docs / "alpha.txt").read_bytes() == b"synthetic preserved source"
    assert ("alpha.txt" in store.rows) is indexed and store.deleted == []
    assert ("alpha.txt" in host.deindexed(project).paths()) is (not indexed)
    assert list_backup_entries(docs) == []


@pytest.mark.parametrize("failure", ["exception", "silent_noop"])
async def test_proxy_failed_unlink_never_claims_success_or_removes_index(gateway_env, monkeypatch, failure):
    _app, _slug, _host, _project, docs, store = gateway_env
    target = docs / "alpha.txt"
    original = b"synthetic unlink failure source\r\n"
    target.write_bytes(original)
    store.rows["alpha.txt"] = SimpleNamespace(source="alpha.txt", chunks=1)
    real_unlink = Path.unlink

    def failed_unlink(path, *args, **kwargs):
        if path == target:
            if failure == "exception":
                raise PermissionError("Synthetic unlink refusal")
            return None
        return real_unlink(path, *args, **kwargs)

    with monkeypatch.context() as scoped:
        scoped.setattr(Path, "unlink", failed_unlink)
        out = await gateway_call(gateway_env, "remove_document", {
            "filepath": "alpha.txt", "delete_file": True,
        })
    assert out["status"] == "error" and out["reason"] == "delete_failed"
    assert out["file_deleted"] is False and target.read_bytes() == original
    assert "alpha.txt" in store.rows and store.deleted == []
    entries = list_backup_entries(docs, "alpha.txt")
    assert len(entries) == 1
    assert find_backup(docs, "alpha.txt", entries[0]["backup_id"]).read_bytes() == original


@pytest.mark.parametrize("tool", ["batch", "remove_documents"])
@pytest.mark.parametrize("on_error", ["stop", "continue"])
async def test_proxy_batched_delete_keeps_order_and_one_snapshot_per_success(gateway_env, tool, on_error):
    _app, _slug, _host, project, docs, store = gateway_env
    paths = ["one.md", "absent.md", "two.py"]
    for path in (paths[0], paths[2]):
        (docs / path).write_bytes(path.encode())
    store.rows[paths[0]] = SimpleNamespace(source=paths[0], chunks=1)
    if tool == "batch":
        arguments = {"calls": [{"tool": "remove_document", "arguments": {
            "project": project.name, "filepath": path, "delete_file": True,
        }} for path in paths], "on_error": on_error}
    else:
        arguments = {"filepaths": paths, "delete_file": True, "on_error": on_error}
    out = await gateway_call(gateway_env, tool, arguments)
    continued = on_error == "continue"
    assert out["status"] == "partial_failure"
    assert (out["succeeded"], out["failed"], out["skipped"]) == (2 if continued else 1, 1, 0 if continued else 1)
    entries = out["results"] if tool == "batch" else out["documents"]
    assert entries[2]["status"] == ("success" if continued else "skipped")
    assert not (docs / paths[0]).exists() and paths[0] not in store.rows
    assert (docs / paths[2]).exists() is (not continued)
    snapshots = list_backup_entries(docs)
    assert len(snapshots) == (2 if continued else 1)
    assert {entry["filepath"] for entry in snapshots} == ({paths[0], paths[2]} if continued else {paths[0]})
    for entry in snapshots:
        assert find_backup(docs, entry["filepath"], entry["backup_id"]).read_bytes() == entry["filepath"].encode()


async def test_proxy_missing_disk_file_only_removes_existing_index_row(gateway_env):
    _app, _slug, _host, _project, docs, store = gateway_env
    store.rows["missing.md"] = SimpleNamespace(source="missing.md", chunks=1)
    out = await gateway_call(gateway_env, "remove_document", {
        "filepath": "missing.md", "delete_file": True,
    })
    assert out["status"] == "success" and out["was_indexed"] is True
    assert out["file_deleted"] is False and out["file_was_on_disk"] is False
    assert "previous_backup_id" not in out and list_backup_entries(docs) == []
    assert "missing.md" not in store.rows and not (docs / "missing.md").exists()


@pytest.mark.parametrize("failure", ["exception", "no_receipt"])
async def test_bulk_delete_cannot_unlink_without_a_snapshot(env, monkeypatch, failure):
    _host, _project, docs, store = env
    (docs / "pack").mkdir()
    target = docs / "pack/one.md"
    target.write_bytes(b"synthetic bulk source")
    store.rows["pack/one.md"] = SimpleNamespace(source="pack/one.md", chunks=1)

    def failed_backup(*args, **kwargs):
        if failure == "exception":
            raise backups.BackupError("synthetic backup refusal")
        return None

    monkeypatch.setattr(engine_transfers, "backup_if_exists", failed_backup)
    out = await call(env, "remove_directory", {"prefix": "pack", "delete_files": True})
    assert out["status"] == "partial" and out["files_deleted"] == 0
    assert out["documents_removed"] == 0 and out["chunks_removed"] == 0
    assert out["backups"] == [] and len(out["failures"]) == 1
    assert target.read_bytes() == b"synthetic bulk source"
    assert "pack/one.md" in store.rows and store.deleted == []


@pytest.mark.parametrize("state", ["never_indexed", "deindexed", "indexed"])
@pytest.mark.parametrize("suffix", [".md", ".py"])
async def test_existing_source_delete_has_one_exact_recoverable_snapshot(env, state, suffix):
    host, project, docs, store = env
    path = "bridge/alpha" + suffix
    original = b"\xef\xbb\xbf# Synthetic bridge\r\n\r\n  exact bytes\t\r\n"
    target = docs / path
    target.parent.mkdir()
    target.write_bytes(original)
    if state == "deindexed":
        host.deindexed(project).add(path)
    elif state == "indexed":
        store.rows[path] = SimpleNamespace(source=path, chunks=1 if suffix == ".md" else 0)

    out = await call(env, "remove_document", {"filepath": path, "delete_file": True})
    assert out["status"] == "success" and out["file_deleted"] is True
    assert out["was_indexed"] is (state == "indexed")
    assert out["deleted_bytes_sha256"] == hashlib.sha256(original).hexdigest()
    assert out["previous_backup_id"]
    entries = list_backup_entries(docs, path)
    assert len(entries) == 1
    assert entries[0]["backup_id"] == out["previous_backup_id"]
    snapshot = find_backup(docs, path, out["previous_backup_id"])
    assert snapshot is not None and snapshot.read_bytes() == original
    assert not target.exists() and path not in store.rows
    assert path not in host.deindexed(project).paths()


@pytest.mark.parametrize("state", ["unindexed", "indexed"])
@pytest.mark.parametrize("failure", ["exception", "no_receipt"])
async def test_backup_failure_preserves_source_index_and_suppression(env, monkeypatch, state, failure):
    host, project, docs, store = env
    path = "alpha.txt"
    original = b"synthetic source\r\n"
    (docs / path).write_bytes(original)
    if state == "indexed":
        store.rows[path] = SimpleNamespace(source=path, chunks=1)
    else:
        host.deindexed(project).add(path)
    before_rows = dict(store.rows)
    before_suppression = host.deindexed(project).paths()

    def failed_backup(*args, **kwargs):
        if failure == "exception":
            raise backups.BackupError("synthetic backup refusal")
        return None

    monkeypatch.setattr(engine_documents, "backup_if_exists", failed_backup)
    out = await call(env, "remove_document", {"filepath": path, "delete_file": True})
    assert out["status"] == "error" and out["reason"] == "backup_failed"
    assert out["file_deleted"] is False
    assert (docs / path).read_bytes() == original
    assert store.rows == before_rows and store.deleted == []
    assert host.deindexed(project).paths() == before_suppression
    assert list_backup_entries(docs) == []


async def test_plural_delete_reuses_single_file_snapshot_owner(env):
    _host, _project, docs, _store = env
    originals = {"one.md": b"first\r\n", "two.py": b"second\n"}
    for path, body in originals.items():
        (docs / path).write_bytes(body)
    out = await call(env, "remove_documents", {
        "filepaths": ["one.md", "missing.md", "two.py"],
        "delete_file": True, "on_error": "continue",
    })
    assert out["status"] == "partial_failure"
    assert (out["succeeded"], out["failed"], out["skipped"]) == (2, 1, 0)
    assert out["documents"][1]["reason"] == "not_found"
    receipts = {(b["filepath"], b["backup_id"]) for b in out["backups"]}
    assert receipts == {(b["filepath"], b["backup_id"]) for b in list_backup_entries(docs)}
    assert len(receipts) == 2
    for path, backup_id in receipts:
        assert find_backup(docs, path, backup_id).read_bytes() == originals[path]
        assert not (docs / path).exists()


async def test_bulk_receipt_delta_survives_history_repeats_and_shared_ids(env, monkeypatch):
    _host, _project, docs, store = env
    monkeypatch.setattr(backups, "_timestamp", lambda: "20260928-044953")
    paths = ["pack/one.md", "pack/two.md", "pack/three.txt", "pack/fresh.md", "pack/fresh.py"]
    for run in range(2):
        for path in paths:
            target = docs / path
            target.parent.mkdir(exist_ok=True)
            target.write_bytes(b"earlier overwrite snapshot")
            backup_if_exists(docs, path)
            target.write_bytes(f"current synthetic run {run}".encode())
            store.rows[path] = SimpleNamespace(source=path, chunks=0 if path.endswith(".py") else 1)
        before = {(b["filepath"], b["backup_id"]) for b in list_backup_entries(docs, prefix="pack/")}
        out = await call(env, "remove_directory", {"prefix": "pack", "delete_files": True})
        assert out["status"] == "success"
        assert (out["documents_removed"], out["files_deleted"], out["chunks_removed"]) == (5, 5, 4)
        assert out["pruned_directories"] == ["pack"]
        assert len(out["backups"]) == 5
        receipts = {(b["filepath"], b["backup_id"]) for b in out["backups"]}
        after = {(b["filepath"], b["backup_id"]) for b in list_backup_entries(docs, prefix="pack/")}
        assert len(receipts) == 5 and after - before == receipts
        assert before <= after  # Historical backups remain recoverable.
        assert len({backup_id for _path, backup_id in receipts}) == 1
        assert "(filepath, backup_id)" in out["restore_hint"]
        assert "before" in out["restore_hint"] and "after" in out["restore_hint"]
        assert "returns exactly this set" not in out["restore_hint"]
        for path, backup_id in receipts:
            assert find_backup(docs, path, backup_id).read_bytes() == f"current synthetic run {run}".encode()
            assert not (docs / path).exists() and path not in store.rows


async def test_negative_delete_guards_do_not_touch_sources_or_backups(env):
    host, project, docs, store = env
    (docs / "source.md").write_bytes(b"keep this synthetic source")
    out = await call(env, "remove_document", {"filepath": "source.md"})
    assert out["status"] == "success" and out["file_deleted"] is False
    assert (docs / "source.md").is_file() and "source.md" in host.deindexed(project).paths()
    for path, body, reason in [("archive.zip", b"synthetic archive", "unindexable_extension"),
                               ("backups/canary.md", b"synthetic recovery point", "invalid_path")]:
        target = docs / path
        target.parent.mkdir(exist_ok=True)
        target.write_bytes(body)
        refused = await call(env, "remove_document", {"filepath": path, "delete_file": True})
        assert refused["status"] == "error" and refused["reason"] == reason
        assert target.read_bytes() == body
    missing = await call(env, "remove_document", {"filepath": "absent.md", "delete_file": True})
    assert missing["status"] == "error" and missing["reason"] == "not_found"
    assert list_backup_entries(docs) == []
    assert store.deleted == ["source.md"]
