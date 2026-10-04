"""App-side pieces of the Linux installer (docs/DESIGN-LINUX-INSTALLER.md 6.4, 7.2, 7.3, 9).

No network, no model download (fastembed is stubbed), no Docker, no sleeps.
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient

from cognita import prefetch_models
from cognita.admin_api import create_admin_app
from cognita.config import CognitaConfig
from cognita.document_roots import display_for
from cognita.engine_local import LocalEngineHost
from cognita.localization import load_catalog
from cognita.registry import Project, Registry
from cognita.retrieval import RetrievalCore
from cognita.store import reset_command_for
from cognita.workspace_store import workspace_reset_command
from fastembed_fakes import _FakeCrossEncoder, _FakeTextEmbedding
from retrieval_fakes import HashEmbedder

# ---------------------------------------------------------------------------
# 7.2  GET /api/document-roots and POST /api/projects/path-info {root, folder}
# ---------------------------------------------------------------------------


@pytest.fixture
def admin(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    registry = Registry(tmp_path / "registry.yaml")
    config = CognitaConfig(
        registry_path=tmp_path / "registry.yaml",
        data_root=tmp_path / "data",
        public_base_url="https://cognita.example.com",
        admin_allowed_hosts=["*"],
        admin_password_sha256="",
    )
    return create_admin_app(config, registry), root


async def _client(app):
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def _probe(app, **body):
    async with await _client(app) as c:
        return await c.post("/api/projects/path-info", json=body)


async def test_document_roots_empty_when_unset(admin, monkeypatch):
    app, _ = admin
    monkeypatch.delenv("COGNITA_DOCUMENT_ROOTS", raising=False)
    async with await _client(app) as c:
        r = await c.get("/api/document-roots")
    assert r.status_code == 200 and r.json() == {"roots": []}


async def test_document_roots_lists_the_configured_roots(admin, monkeypatch):
    app, _ = admin
    monkeypatch.setenv("COGNITA_DOCUMENT_ROOTS", '["/home/u/Documents", "/home/u/OneDrive - Personal"]')
    monkeypatch.delenv("COGNITA_DOCUMENT_ROOT_DISPLAYS", raising=False)
    async with await _client(app) as c:
        r = await c.get("/api/document-roots")
    # 19.2: each root is {path, display}; with no display the display is the path.
    assert r.json() == {"roots": [
        {"path": "/home/u/Documents", "display": "/home/u/Documents"},
        {"path": "/home/u/OneDrive - Personal", "display": "/home/u/OneDrive - Personal"}]}


@pytest.mark.parametrize("raw", ["not json", '{"a": 1}', '"/x"', "[1, null, \"\"]"])
async def test_document_roots_malformed_value_means_no_roots(admin, monkeypatch, raw):
    app, _ = admin
    monkeypatch.setenv("COGNITA_DOCUMENT_ROOTS", raw)
    async with await _client(app) as c:
        r = await c.get("/api/document-roots")
    assert r.status_code == 200 and r.json() == {"roots": []}


async def test_root_folder_probe_success_leaves_no_probe_file(admin, monkeypatch):
    app, root = admin
    monkeypatch.setenv("COGNITA_DOCUMENT_ROOTS", f'["{root.as_posix()}"]')
    inside = root / "Project Files"
    inside.mkdir()
    (inside / "a.txt").write_bytes(b"abc")
    r = await _probe(app, root=root.as_posix(), folder="Project Files")
    assert r.status_code == 200
    body = r.json()
    assert body["readable"] is True and body["writable"] is True
    assert body["message"] == "Cognita can read and write this folder."
    assert body["file_count"] == 1 and body["total_bytes"] == 3
    assert body["path"] == str(inside.resolve())
    assert sorted(p.name for p in inside.iterdir()) == ["a.txt"]  # probe file removed


async def test_root_folder_probe_empty_folder_means_the_root_itself(admin, monkeypatch):
    app, root = admin
    monkeypatch.setenv("COGNITA_DOCUMENT_ROOTS", f'["{root.as_posix()}"]')
    r = await _probe(app, root=root.as_posix(), folder="")
    assert r.status_code == 200 and r.json()["path"] == str(root.resolve())


async def test_root_folder_probe_missing_folder_says_create_it(admin, monkeypatch):
    app, root = admin
    monkeypatch.setenv("COGNITA_DOCUMENT_ROOTS", f'["{root.as_posix()}"]')
    r = await _probe(app, root=root.as_posix(), folder="nope")
    assert r.status_code == 200
    body = r.json()
    assert body["message"] == "This folder does not exist. Create it first."
    assert body["exists"] is False and body["readable"] is False and body["writable"] is False
    assert not (root / "nope").exists()  # the probe never creates the folder


async def test_root_folder_probe_a_file_is_not_a_folder(admin, monkeypatch):
    app, root = admin
    monkeypatch.setenv("COGNITA_DOCUMENT_ROOTS", f'["{root.as_posix()}"]')
    (root / "f.txt").write_bytes(b"x")
    r = await _probe(app, root=root.as_posix(), folder="f.txt")
    assert r.status_code == 200 and "not a folder" in r.json()["message"]


async def test_root_folder_probe_root_not_listed_is_refused(admin, monkeypatch, tmp_path):
    app, root = admin
    monkeypatch.setenv("COGNITA_DOCUMENT_ROOTS", f'["{root.as_posix()}"]')
    other = tmp_path / "other"
    other.mkdir()
    r = await _probe(app, root=other.as_posix(), folder="")
    assert r.status_code == 400 and "not one of the folders" in r.json()["detail"]
    r = await _probe(app, root=root.as_posix() + "/", folder="")  # exact match only
    assert r.status_code == 400


@pytest.mark.parametrize("folder", ["..", "a/../..", "../other", "a/../../other"])
async def test_root_folder_probe_dot_dot_is_refused(admin, monkeypatch, folder):
    app, root = admin
    monkeypatch.setenv("COGNITA_DOCUMENT_ROOTS", f'["{root.as_posix()}"]')
    r = await _probe(app, root=root.as_posix(), folder=folder)
    assert r.status_code == 400 and "'..'" in r.json()["detail"]


async def test_root_folder_probe_absolute_folder_is_refused(admin, monkeypatch):
    app, root = admin
    monkeypatch.setenv("COGNITA_DOCUMENT_ROOTS", f'["{root.as_posix()}"]')
    r = await _probe(app, root=root.as_posix(), folder="/etc")
    assert r.status_code == 400 and "relative" in r.json()["detail"]


async def test_root_folder_probe_symlink_escaping_the_root_is_refused(admin, monkeypatch, tmp_path):
    app, root = admin
    monkeypatch.setenv("COGNITA_DOCUMENT_ROOTS", f'["{root.as_posix()}"]')
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        (root / "link").symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory symlinks unavailable: {type(exc).__name__}")
    r = await _probe(app, root=root.as_posix(), folder="link")
    assert r.status_code == 400 and "outside the documents root" in r.json()["detail"]
    assert list(outside.iterdir()) == []  # nothing was probed there


async def test_root_folder_probe_read_only_folder(admin, monkeypatch):
    app, root = admin
    monkeypatch.setenv("COGNITA_DOCUMENT_ROOTS", f'["{root.as_posix()}"]')
    (root / "ro").mkdir()
    real_open = os.open

    def refuse_create(path, flags, *args, **kwargs):
        if flags & os.O_CREAT:
            raise PermissionError(13, "Permission denied")
        return real_open(path, flags, *args, **kwargs)

    # Root can write anywhere and Windows has no POSIX modes, so simulate the refusal.
    monkeypatch.setattr("cognita.admin_api.os.open", refuse_create)
    r = await _probe(app, root=root.as_posix(), folder="ro")
    body = r.json()
    assert r.status_code == 200
    assert body["readable"] is True and body["writable"] is False
    assert "cannot write" in body["message"]
    assert list((root / "ro").iterdir()) == []


async def test_root_folder_probe_write_test_uses_exclusive_create_and_logs_the_path(admin, monkeypatch, caplog):
    app, root = admin
    monkeypatch.setenv("COGNITA_DOCUMENT_ROOTS", f'["{root.as_posix()}"]')
    seen = []
    real_open = os.open

    def recording_open(path, flags, *args, **kwargs):
        seen.append((Path(path).name, flags))
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr("cognita.admin_api.os.open", recording_open)
    with caplog.at_level(logging.INFO, logger="cognita.admin"):
        await _probe(app, root=root.as_posix(), folder="")
    names = [(name, flags) for name, flags in seen if name.startswith(".cognita-write-test-")]
    assert len(names) == 1
    name, flags = names[0]
    assert flags & os.O_CREAT and flags & os.O_EXCL
    assert len(name) == len(".cognita-write-test-") + 16
    text = caplog.text
    assert "created write-test file path=" in text and "removed write-test file path=" in text


async def test_path_info_requires_one_form(admin, monkeypatch):
    app, root = admin
    monkeypatch.setenv("COGNITA_DOCUMENT_ROOTS", f'["{root.as_posix()}"]')
    r = await _probe(app)
    assert r.status_code == 400
    r = await _probe(app, documents_dir=str(root), root=root.as_posix(), folder="")
    assert r.status_code == 400


async def test_path_info_absolute_form_is_unchanged(admin):
    app, root = admin
    (root / "one.txt").write_bytes(b"abc")
    r = await _probe(app, documents_dir=str(root))
    payload = r.json()
    assert {key: payload[key] for key in ("status", "path", "file_count", "total_bytes")} == {
        "status": "ok", "path": str(root.resolve()), "file_count": 1, "total_bytes": 3,
    }
    assert payload["presentation_id"] == "admin.project.path_checked"
    assert payload["presentation_values"] == {"folder": display_for(payload["path"])}


async def test_new_project_form_ships_the_root_and_folder_controls(admin):
    app, _ = admin
    async with await _client(app) as c:
        page = (await c.get("/")).text
        script = (await c.get("/static/app.js")).text
    for needle in ('name="documents_root"', 'name="documents_folder"', 'id="documents-root-fields"'):
        assert needle in page
    catalog = load_catalog("en-US")
    assert 'data-i18n="admin.projects.test_path"' in page
    assert "/api/document-roots" in script and "currentDocumentsDir" in script
    assert 't("admin.projects.test_folder")' in script
    assert catalog["admin.projects.test_folder"] == "Test folder"


# ---------------------------------------------------------------------------
# 6.4  python -m cognita.prefetch_models (fastembed stubbed)
# ---------------------------------------------------------------------------


def _prefetch_config(tmp_path, **overrides):
    values = dict(
        embedding_model="fake/embed", embedding_dimensions=4, models_cache_dir=tmp_path / "cache",
        embedding_threads=0, embed_batch_size=8, reranker_model="fake/rerank",
    )
    values.update(overrides)
    return SimpleNamespace(**values)


def test_expected_bytes_covers_the_default_models():
    defaults = CognitaConfig()
    assert set(prefetch_models.EXPECTED_BYTES) >= {defaults.embedding_model, defaults.reranker_model}
    assert all(size > 0 for size in prefetch_models.EXPECTED_BYTES.values())


def test_prefetch_downloads_both_models_and_logs_each(fake_fastembed, tmp_path, caplog):
    config = _prefetch_config(tmp_path)
    with caplog.at_level(logging.INFO, logger="cognita.prefetch"):
        assert prefetch_models.prefetch(config) is True
    assert _FakeTextEmbedding.instances[0].init_kwargs["model_name"] == "fake/embed"
    assert _FakeTextEmbedding.instances[0].embed_calls[0]["texts"]  # one string embedded
    assert _FakeCrossEncoder.instances[0].init_kwargs["model_name"] == "fake/rerank"
    assert _FakeCrossEncoder.instances[0].init_kwargs["cache_dir"] == str(config.models_cache_dir)
    lines = [r.getMessage() for r in caplog.records]
    for model in ("fake/embed", "fake/rerank"):
        assert any(line.startswith(f"prefetch.start model={model} cache=") for line in lines)
        assert any(line.startswith(f"prefetch.done model={model} elapsed_s=") and "bytes_on_disk=" in line for line in lines)
    assert not any(line.startswith("prefetch.failed") for line in lines)


def test_prefetch_embedder_failure_is_reported_and_the_reranker_still_runs(fake_fastembed, tmp_path, caplog, monkeypatch):
    def boom(self, texts, **kwargs):
        raise RuntimeError("no network")
        yield  # pragma: no cover - makes this a generator like the real embed()

    monkeypatch.setattr(_FakeTextEmbedding, "embed", boom)
    with caplog.at_level(logging.INFO, logger="cognita.prefetch"):
        assert prefetch_models.prefetch(_prefetch_config(tmp_path)) is False
    lines = [r.getMessage() for r in caplog.records]
    assert any(line.startswith("prefetch.failed model=fake/embed reason=EmbeddingUnavailable:") for line in lines)
    assert any(line.startswith("prefetch.done model=fake/rerank") for line in lines)


def test_prefetch_reranker_that_cannot_load_is_a_failure_not_a_silent_none(fake_fastembed, tmp_path, caplog):
    class Unloadable(_FakeCrossEncoder):
        def __init__(self, **kwargs):
            raise RuntimeError("model files missing")

    sys.modules["fastembed.rerank.cross_encoder"].TextCrossEncoder = Unloadable
    with caplog.at_level(logging.INFO, logger="cognita.prefetch"):
        assert prefetch_models.prefetch(_prefetch_config(tmp_path)) is False
    lines = [r.getMessage() for r in caplog.records]
    assert any(line.startswith("prefetch.failed model=fake/rerank reason=RuntimeError:") for line in lines)


def test_main_exit_code_follows_the_result(fake_fastembed, tmp_path, monkeypatch):
    config = _prefetch_config(tmp_path)
    monkeypatch.setattr("cognita.config.load_config", lambda *a, **k: config)
    assert prefetch_models.main([]) == 0
    monkeypatch.setattr(prefetch_models, "prefetch", lambda _c: False)
    assert prefetch_models.main([]) == 1


def test_main_config_failure_exits_1(monkeypatch, caplog):
    def bad(*_a, **_k):
        raise ValueError("Config file x must contain a YAML mapping")

    monkeypatch.setattr("cognita.config.load_config", bad)
    with caplog.at_level(logging.INFO, logger="cognita.prefetch"):
        assert prefetch_models.main([]) == 1
    assert "prefetch.failed model=<config> reason=ValueError" in caplog.text


def test_bytes_on_disk_counts_the_models_directory_and_skips_symlinks(tmp_path):
    model_dir = tmp_path / "models--BAAI--bge-large-en-v1.5"
    (model_dir / "blobs").mkdir(parents=True)
    (model_dir / "snapshots" / "abc").mkdir(parents=True)
    (model_dir / "blobs" / "b1").write_bytes(b"x" * 100)
    (model_dir / "blobs" / "b2").write_bytes(b"x" * 20)
    (tmp_path / "models--other--model").mkdir()
    (tmp_path / "models--other--model" / "z").write_bytes(b"x" * 5000)
    try:
        (model_dir / "snapshots" / "abc" / "model.onnx").symlink_to(model_dir / "blobs" / "b1")
    except OSError:
        pass  # no symlinks on this machine: the count is still 120
    assert prefetch_models.bytes_on_disk(tmp_path, "BAAI/bge-large-en-v1.5") == 120


def test_bytes_on_disk_falls_back_to_the_whole_cache(tmp_path):
    (tmp_path / "layout").mkdir()
    (tmp_path / "layout" / "f").write_bytes(b"x" * 7)
    assert prefetch_models.bytes_on_disk(tmp_path, "unknown/model") == 7
    assert prefetch_models.bytes_on_disk(tmp_path / "missing", "unknown/model") == 0


# ---------------------------------------------------------------------------
# 7.3  the empty-walk refusal becomes the project's reindex progress error
# ---------------------------------------------------------------------------


class _FakeStore:
    """Enough Store for index_project on a folder with nothing in it."""

    def __init__(self, indexed):
        self.indexed = indexed
        self.deleted = 0

    async def ensure_project(self, project):
        pass

    async def list_sources(self, project):
        return dict(self.indexed)

    async def delete_documents_not_in(self, project, live):
        self.deleted += 1
        return len(self.indexed)

    async def delete_all_documents(self, project):
        self.deleted += 1
        return len(self.indexed)


async def test_empty_walk_refusal_carries_the_plain_message(tmp_path, caplog):
    docs = tmp_path / "docs"
    docs.mkdir()
    store = _FakeStore({"a.md": {}, "b.md": {}})
    core = RetrievalCore(store, HashEmbedder(32))
    with caplog.at_level(logging.INFO, logger="cognita.retrieval"):
        summary = await core.index_project("P", docs)
    assert summary["removed"] == 0 and store.deleted == 0
    assert summary["empty_root_message"] == (
        f"The documents folder {docs} is empty or not mounted. Nothing was removed. "
        "When the files are back, restart Cognita or reindex the project."
    )
    # The existing error line stays, and the summary error now names a count, not the whole list.
    assert any("Refusing to remove all 2 documents" in r.getMessage() for r in caplog.records)
    assert any("holds 2 documents" in e for e in summary["errors"])


async def test_an_index_that_is_already_empty_is_not_a_refusal(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    core = RetrievalCore(_FakeStore({}), HashEmbedder(32))
    summary = await core.index_project("P", docs)
    assert "empty_root_message" not in summary


class _Lock:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _EmptyRootCore:
    def __init__(self, message):
        self.store = SimpleNamespace(pool=None)
        self.embedder = None
        self.reranker = None
        self.exclude_patterns = []
        self.sync_conflict_patterns = []
        self.sync_conflicts: dict[str, list[str]] = {}
        self._message = message

    def write_lock(self, project_name):
        return _Lock()

    async def index_project(self, project_name, documents_dir, *, force, progress):
        progress({"total_files": 0, "processed": 0, "indexed": 0, "skipped": 0, "errors": []})
        summary = {"indexed": 0, "skipped": 0, "removed": 0, "errors": ["removal sweep skipped"],
                   "total_files": 0, "tier_changed": 0, "chunks_purged": 0}
        if self._message:
            summary["empty_root_message"] = self._message
        return summary


def _engine(tmp_path, message):
    documents = tmp_path / "documents"
    documents.mkdir()
    registry = Registry(tmp_path / "registry.yaml")
    project = Project(name="KEI", documents_dir=documents, data_dir=tmp_path / "data")
    registry.add(project)
    config = CognitaConfig(connectors_path=tmp_path / "connectors.yaml", watch_enabled=False, pg_probe_interval_s=0)
    return LocalEngineHost(config, registry, _EmptyRootCore(message)), project


async def test_engine_puts_the_refusal_message_where_admin_reads_the_reindex_error(tmp_path):
    message = "The documents folder /x is empty or not mounted. Nothing was removed."
    host, project = _engine(tmp_path, message)
    assert host.start_background_reindex(project, "incremental") is True
    await asyncio.wait_for(host._reindex_tasks[project.name], timeout=5)
    progress = host._reindex_progress[project.name]
    assert progress["active"] is False and progress["error"] == message
    status = await host._get_reindex_status(project, {})
    assert status["reindex"]["last_error"] == message


class _MissingRootCore(_EmptyRootCore):
    async def index_project(self, project_name, documents_dir, *, force, progress):
        raise NotADirectoryError(f"documents_dir is not a readable directory: {documents_dir}")


async def test_a_missing_documents_folder_is_reported_in_plain_words(tmp_path):
    # C10, seen on the installer VM: the folder was gone at startup and Admin showed
    # "NotADirectoryError: documents_dir is not a readable directory: ...".
    host, project = _engine(tmp_path, "")
    host.core = _MissingRootCore("")
    assert host.start_background_reindex(project, "incremental") is True
    await asyncio.wait_for(host._reindex_tasks[project.name], timeout=5)
    error = host._reindex_progress[project.name]["error"]
    assert error == (f"The documents folder {project.documents_dir} is missing or not readable. "
                     "Nothing was removed. When it is back, restart Cognita or reindex the project.")


async def test_engine_sets_no_error_for_a_normal_reindex(tmp_path):
    host, project = _engine(tmp_path, "")
    host.start_background_reindex(project, "incremental")
    await asyncio.wait_for(host._reindex_tasks[project.name], timeout=5)
    assert "error" not in host._reindex_progress[project.name]


async def test_admin_project_status_shows_the_reindex_error(tmp_path):
    message = "The documents folder /x is empty or not mounted. Nothing was removed."
    host, project = _engine(tmp_path, message)
    host.start_background_reindex(project, "incremental")
    await asyncio.wait_for(host._reindex_tasks[project.name], timeout=5)

    config = CognitaConfig(registry_path=tmp_path / "registry.yaml", data_root=tmp_path / "data",
                           admin_allowed_hosts=["*"], admin_password_sha256="")
    real_call = host.call_tool

    async def call_tool(proj, tool, arguments):
        if tool == "get_index_stats":
            return {"stats": {"total_documents": 0, "total_chunks": 0}}
        return await real_call(proj, tool, arguments)

    host.call_tool = call_tool
    app = create_admin_app(config, host.registry, engine=host)
    async with await _client(app) as c:
        r = await c.get("/api/projects/KEI/status")
    assert r.status_code == 200
    assert r.json()["reindex_error"] == message


# ---------------------------------------------------------------------------
# 9  reset hints name the launcher for a `local` install
# ---------------------------------------------------------------------------


def test_index_reset_hint_for_local_names_the_launcher(monkeypatch):
    monkeypatch.delenv("COGNITA_COMMAND", raising=False)
    assert reset_command_for("local") == "./cognita reset index"


@pytest.mark.parametrize("target", ["main", "beta", None])
def test_index_reset_hint_for_other_targets_is_unchanged(target):
    assert reset_command_for(target) == (
        f"python3 scripts/reset_disposable_state.py --target {target or '<your target>'} --scope index --apply"
    )


def test_workspace_reset_hint_for_local_names_the_launcher(monkeypatch):
    monkeypatch.setenv("COGNITA_RELEASE_TARGET", "local")
    monkeypatch.delenv("COGNITA_COMMAND", raising=False)
    assert workspace_reset_command() == "./cognita reset workspaces"


def test_workspace_reset_hint_for_other_targets_is_unchanged(monkeypatch):
    monkeypatch.setenv("COGNITA_RELEASE_TARGET", "main")
    assert workspace_reset_command() == (
        "python3 scripts/reset_disposable_state.py --target main --scope workspaces --apply"
    )
    monkeypatch.delenv("COGNITA_RELEASE_TARGET")
    assert workspace_reset_command() == (
        "python3 scripts/reset_disposable_state.py --target <your target> --scope workspaces --apply"
    )
