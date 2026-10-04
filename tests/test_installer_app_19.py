"""App side of design 19 (docs/DESIGN-LINUX-INSTALLER.md 19.2 and 19.6): documents roots with a display
path, and the one command name the app's hints use.

No network, no Docker, no sleeps.  A display is text for PEOPLE (the Windows path a WSL install's folder
lives at); the container path stays what identifies a root everywhere.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from test_installer_app import _engine, _FakeStore, _MissingRootCore

from cognita.admin_api import create_admin_app
from cognita.config import CognitaConfig
from cognita.document_roots import (
    configured_document_displays,
    configured_document_roots,
    display_for,
    roots_with_displays,
)
from cognita.registry import Registry
from cognita.retrieval import RetrievalCore
from cognita.runtime_broker.state import reset_command as broker_reset_command
from cognita.store import reset_command_for
from cognita.workspace_store import workspace_reset_command
from retrieval_fakes import HashEmbedder

WINDOWS_ROOT = "D:\\Documents"


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch):
    for name in ("COGNITA_DOCUMENT_ROOTS", "COGNITA_DOCUMENT_ROOT_DISPLAYS", "COGNITA_COMMAND",
                 "COGNITA_RELEASE_TARGET"):
        monkeypatch.delenv(name, raising=False)


def _roots(monkeypatch, *paths, displays=None):
    monkeypatch.setenv("COGNITA_DOCUMENT_ROOTS", json.dumps(list(paths)))
    if displays is not None:
        monkeypatch.setenv("COGNITA_DOCUMENT_ROOT_DISPLAYS", json.dumps(displays))


@pytest.fixture
def admin(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    registry = Registry(tmp_path / "registry.yaml")
    config = CognitaConfig(
        registry_path=tmp_path / "registry.yaml", data_root=tmp_path / "data",
        public_base_url="https://cognita.example.com", admin_allowed_hosts=["*"], admin_password_sha256="")
    return create_admin_app(config, registry), registry, root


async def _client(app):
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


# ---------------------------------------------------------------------------
# document_roots.py: reading both variables, and the path-to-display lookup
# ---------------------------------------------------------------------------


def test_roots_and_displays_are_read_from_their_own_variables(monkeypatch):
    _roots(monkeypatch, "/mnt/d/Documents", "/home/u/Other", displays={"/mnt/d/Documents": WINDOWS_ROOT})
    assert configured_document_roots() == ["/mnt/d/Documents", "/home/u/Other"]
    assert configured_document_displays() == {"/mnt/d/Documents": WINDOWS_ROOT}
    assert roots_with_displays() == [{"path": "/mnt/d/Documents", "display": WINDOWS_ROOT},
                                     {"path": "/home/u/Other", "display": "/home/u/Other"}]


@pytest.mark.parametrize("raw", ["not json", '["a"]', '"x"', '{"/a": 1, "": "x", "/b": ""}'])
def test_a_malformed_displays_variable_means_no_displays(monkeypatch, raw):
    monkeypatch.setenv("COGNITA_DOCUMENT_ROOT_DISPLAYS", raw)
    assert configured_document_displays() == {}


def test_display_for_is_the_path_when_there_are_no_displays(monkeypatch):
    _roots(monkeypatch, "/mnt/d/Documents")
    assert display_for("/mnt/d/Documents/Manuals") == "/mnt/d/Documents/Manuals"


def test_display_for_joins_the_part_under_the_root_with_the_displays_separator(monkeypatch):
    _roots(monkeypatch, "/mnt/d/Documents", "/home/u/Notes",
           displays={"/mnt/d/Documents": WINDOWS_ROOT, "/home/u/Notes": "/srv/notes"})
    assert display_for("/mnt/d/Documents") == WINDOWS_ROOT
    assert display_for("/mnt/d/Documents/Manuals/Aircraft") == "D:\\Documents\\Manuals\\Aircraft"
    assert display_for("/mnt/d/Documents/") == WINDOWS_ROOT                 # a trailing slash changes nothing
    assert display_for("/home/u/Notes/2026/a") == "/srv/notes/2026/a"        # a slash display keeps slashes


def test_display_for_leaves_a_path_under_no_root_or_a_sibling_prefix_alone(monkeypatch):
    _roots(monkeypatch, "/mnt/d/Documents", displays={"/mnt/d/Documents": WINDOWS_ROOT})
    assert display_for("/tmp/elsewhere") == "/tmp/elsewhere"
    assert display_for("/mnt/d/Documents-old/x") == "/mnt/d/Documents-old/x"   # not under the root, only a prefix


def test_display_for_a_drive_root_and_a_unc_display(monkeypatch):
    _roots(monkeypatch, "/mnt/d", "/mnt/nas", displays={"/mnt/d": "D:\\", "/mnt/nas": "\\\\nas\\docs"})
    assert display_for("/mnt/d/Manuals") == "D:\\Manuals"
    assert display_for("/mnt/nas/a/b") == "\\\\nas\\docs\\a\\b"


def test_the_longest_root_wins(monkeypatch):
    _roots(monkeypatch, "/a", "/a/b", displays={"/a": "X:\\a", "/a/b": "Y:\\b"})
    assert display_for("/a/b/c") == "Y:\\b\\c"
    assert display_for("/a/c") == "X:\\a\\c"


# ---------------------------------------------------------------------------
# Admin: document-roots, the Test folder message, the add-project error, the project list
# ---------------------------------------------------------------------------


async def test_document_roots_returns_path_and_display(admin, monkeypatch):
    app, _registry, root = admin
    _roots(monkeypatch, root.as_posix(), displays={root.as_posix(): WINDOWS_ROOT})
    async with await _client(app) as c:
        response = await c.get("/api/document-roots")
    assert response.json() == {"roots": [{"path": root.as_posix(), "display": WINDOWS_ROOT}]}


async def _probe(app, **body):
    async with await _client(app) as c:
        return await c.post("/api/projects/path-info", json=body)


async def test_test_folder_answers_with_the_display_and_keeps_the_container_path(admin, monkeypatch):
    app, _registry, root = admin
    _roots(monkeypatch, root.as_posix(), displays={root.as_posix(): WINDOWS_ROOT})
    (root / "Manuals").mkdir()
    body = (await _probe(app, root=root.as_posix(), folder="Manuals")).json()
    assert body["display"] == "D:\\Documents\\Manuals"
    assert body["path"] == str((root / "Manuals").resolve())        # the container path is not the display
    assert body["message"] == "Cognita can read and write this folder."


async def test_a_windows_style_folder_works_in_test_folder(admin, monkeypatch):
    app, _registry, root = admin
    _roots(monkeypatch, root.as_posix(), displays={root.as_posix(): WINDOWS_ROOT})
    (root / "Manuals" / "Aircraft").mkdir(parents=True)
    response = await _probe(app, root=root.as_posix(), folder="Manuals\\Aircraft")
    body = response.json()
    assert response.status_code == 200 and body["readable"] is True and body["writable"] is True
    assert body["path"] == str((root / "Manuals" / "Aircraft").resolve())
    assert body["display"] == "D:\\Documents\\Manuals\\Aircraft"


@pytest.mark.parametrize("folder", ["..\\other", "Manuals\\..\\..\\x", "\\etc", "\\\\host\\share"])
async def test_backslash_forms_are_refused_after_conversion(admin, monkeypatch, folder):
    app, _registry, root = admin
    _roots(monkeypatch, root.as_posix())
    response = await _probe(app, root=root.as_posix(), folder=folder)
    assert response.status_code == 400


async def test_a_backslash_folder_reaches_create_project_as_a_slash_path(admin, monkeypatch):
    # The browser turns Manuals\Aircraft into Manuals/Aircraft before it builds documents_dir; what the
    # server then creates is the same folder the Test folder probe just looked at.
    app, registry, root = admin
    _roots(monkeypatch, root.as_posix(), displays={root.as_posix(): WINDOWS_ROOT})
    (root / "Manuals" / "Aircraft").mkdir(parents=True)
    async with await _client(app) as c:
        response = await c.post("/api/projects", json={
            "name": "Aircraft", "documents_dir": root.as_posix() + "/" + "Manuals\\Aircraft".replace("\\", "/")})
    assert response.status_code == 201
    assert Path(registry.get("Aircraft").documents_dir) == root / "Manuals" / "Aircraft"


async def test_the_project_list_carries_the_display_of_each_folder(admin, monkeypatch):
    app, _registry, root = admin
    _roots(monkeypatch, root.as_posix(), displays={root.as_posix(): WINDOWS_ROOT})
    (root / "Manuals").mkdir()
    async with await _client(app) as c:
        await c.post("/api/projects", json={"name": "Manuals", "documents_dir": (root / "Manuals").as_posix()})
        listing = (await c.get("/api/projects")).json()["projects"]
    assert listing[0]["documents_display"] == "D:\\Documents\\Manuals"
    assert listing[0]["documents_dir"] == str(root / "Manuals")      # the path is still there


async def test_the_project_list_display_is_the_path_when_no_root_has_one(admin, monkeypatch):
    app, _registry, root = admin
    _roots(monkeypatch, root.as_posix())
    async with await _client(app) as c:
        await c.post("/api/projects", json={"name": "P", "documents_dir": root.as_posix()})
        listing = (await c.get("/api/projects")).json()["projects"]
    assert listing[0]["documents_display"] == listing[0]["documents_dir"]


async def test_the_add_project_error_shows_the_display(admin, monkeypatch):
    app, _registry, root = admin
    _roots(monkeypatch, root.as_posix(), displays={root.as_posix(): WINDOWS_ROOT})
    async with await _client(app) as c:
        response = await c.post("/api/projects", json={
            "name": "Nope", "documents_dir": root.as_posix() + "/does/not/exist"})
    assert response.status_code == 400
    assert "D:\\Documents\\does\\not\\exist" in response.json()["detail"]
    assert root.as_posix() not in response.json()["detail"]


def test_the_page_script_converts_backslashes_for_test_folder_and_save_and_uses_displays():
    script = (Path(__file__).resolve().parents[1] / "src" / "cognita" / "web" / "app.js").read_text(encoding="utf-8")
    # One helper builds the folder for BOTH the probe and the save, so one conversion covers both.
    assert 'value.trim().replace(/\\\\/g, "/")' in script
    assert script.count("currentDocumentsFolder(addForm)") >= 1 and "currentDocumentsDir(form)" in script
    for needle in ("documents_display", "option.textContent = root.display", "option.value = root.path"):
        assert needle in script


# ---------------------------------------------------------------------------
# The empty-or-missing folder messages show the display too (design 7.3, 19.2)
# ---------------------------------------------------------------------------


async def test_the_empty_folder_message_shows_the_display(tmp_path, monkeypatch):
    docs = tmp_path / "docs"
    docs.mkdir()
    _roots(monkeypatch, docs.as_posix(), displays={docs.as_posix(): WINDOWS_ROOT})
    core = RetrievalCore(_FakeStore({"a.md": {}, "b.md": {}}), HashEmbedder(32))
    summary = await core.index_project("P", docs)
    assert summary["empty_root_message"] == (
        f"The documents folder {WINDOWS_ROOT} is empty or not mounted. Nothing was removed. "
        "When the files are back, restart Cognita or reindex the project.")


async def test_the_missing_folder_message_shows_the_display(tmp_path, monkeypatch):
    host, project = _engine(tmp_path, "")
    _roots(monkeypatch, project.documents_dir.as_posix(), displays={project.documents_dir.as_posix(): WINDOWS_ROOT})
    host.core = _MissingRootCore("")
    assert host.start_background_reindex(project, "incremental") is True
    await asyncio.wait_for(host._reindex_tasks[project.name], timeout=5)
    assert host._reindex_progress[project.name]["error"] == (
        f"The documents folder {WINDOWS_ROOT} is missing or not readable. Nothing was removed. "
        "When it is back, restart Cognita or reindex the project.")


# ---------------------------------------------------------------------------
# 19.6 the command name in the app's hints
# ---------------------------------------------------------------------------


def test_every_app_hint_for_a_local_install_uses_cognita_command(monkeypatch):
    monkeypatch.setenv("COGNITA_RELEASE_TARGET", "local")
    monkeypatch.setenv("COGNITA_COMMAND", "cognita")
    assert reset_command_for("local") == "cognita reset index"
    assert workspace_reset_command() == "cognita reset workspaces"
    assert broker_reset_command() == "cognita reset workspaces"


def test_the_hints_default_to_the_launcher_when_no_command_is_set(monkeypatch):
    monkeypatch.setenv("COGNITA_RELEASE_TARGET", "local")
    assert reset_command_for("local") == "./cognita reset index"
    assert workspace_reset_command() == "./cognita reset workspaces"
    assert broker_reset_command() == "./cognita reset workspaces"


def test_the_broker_hint_for_other_targets_is_unchanged(monkeypatch):
    monkeypatch.setenv("COGNITA_COMMAND", "cognita")            # ignored: kei's targets keep their script command
    monkeypatch.setenv("COGNITA_RELEASE_TARGET", "test")
    assert broker_reset_command() == (
        "python3 scripts/reset_disposable_state.py --target test --scope workspaces --apply")
    monkeypatch.delenv("COGNITA_RELEASE_TARGET")
    assert broker_reset_command() == (
        "python3 scripts/reset_disposable_state.py --target <your target> --scope workspaces --apply")
    assert reset_command_for("main") == (
        "python3 scripts/reset_disposable_state.py --target main --scope index --apply")
