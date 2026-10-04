"""Admin API tests: project CRUD and retired credential controls.

No engine is wired in, so nothing indexes — these exercise the
registry/HTTP surface, not the retrieval engine. Authentication policy
generation and one-time display are covered by test_admin_authentication_11.
"""

import asyncio
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from cognita.admin_api import create_admin_app
from cognita.config import CognitaConfig, load_config
from cognita.registry import Registry


@pytest.fixture
def ctx(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    registry_path = tmp_path / "registry.yaml"
    registry = Registry(registry_path)
    config = CognitaConfig(
        registry_path=registry_path,
        data_root=tmp_path / "data",
        public_base_url="https://cognita.example.com",
        # Not what these tests are about; the ASGI client sends Host: test.
        admin_allowed_hosts=["*"],
        admin_password_sha256="",  # these test CRUD, not auth — keep the surface open
    )
    app = create_admin_app(config, registry)
    return app, registry, config, docs


async def _client(app):
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def test_list_empty(ctx):
    app, *_ = ctx
    async with await _client(app) as c:
        r = await c.get("/api/projects")
    assert r.status_code == 200
    assert r.json()["projects"] == []


async def test_debug_tokens_mode_is_retired(ctx):
    app, _, _, _ = ctx
    async with await _client(app) as c:
        responses = await asyncio.gather(
            c.get("/api/debug-tokens-mode"),
            c.put("/api/debug-tokens-mode", json={"enabled": True}),
        )
    assert all(response.status_code == 410 for response in responses)


def test_debug_tokens_mode_cannot_survive_restart(tmp_path, monkeypatch):
    cfg_path = tmp_path / "cognita.yaml"
    cfg_path.write_text("debug_tokens_mode: true\ntest_mode: true\n", encoding="utf-8")
    monkeypatch.setenv("COGNITA_DEBUG_TOKENS_MODE", "true")
    monkeypatch.setenv("COGNITA_TEST_MODE", "1")
    loaded = load_config(cfg_path)
    assert loaded.debug_tokens_mode is False
    # 13.0 §7.3: `test_mode` is gone as a field. A stale key left in an old
    # config file must still load (it is ignored, not an error) and must not
    # reappear as an attribute, and COGNITA_TEST_MODE must not reach the config
    # either: 13.0 test mode is decided in `cmd_serve` and lives in
    # `self_test_mode`, which load_config never sets.
    assert not hasattr(loaded, "test_mode")
    assert loaded.self_test_mode is False


async def test_release_admin_retires_static_key_controls(ctx):
    app, registry, config, docs = ctx
    # (13.0 §7.3: the `config.test_mode = False` that stood here went with the
    # field; the Admin static-key controls are retired regardless of mode.)
    async with await _client(app) as c:
        await c.post("/api/projects", json={"name": "KEI", "documents_dir": str(docs)})
        key = await c.post("/api/projects/KEI/api-key")
        diagnostic = await c.put("/api/debug-tokens-mode", json={"enabled": True})
    assert key.status_code == 410
    assert diagnostic.status_code == 410
    assert registry.get("KEI").token_sha256 == ""


async def test_add_does_not_emit_retired_project_oauth_url(ctx):
    app, registry, _config, docs = ctx
    async with await _client(app) as c:
        r = await c.post("/api/projects", json={"name": "KEI", "documents_dir": str(docs)})
    assert r.status_code == 201
    body = r.json()
    assert "token" not in body
    assert "connector_url" not in body
    assert "connector_path" not in body
    assert "reminder" not in body
    project = registry.get("KEI")
    assert project is not None
    assert project.token_sha256 == ""


async def test_project_default_permissions_setting_create_view_and_update(ctx):
    app, registry, _config, docs = ctx
    async with await _client(app) as c:
        created = await c.post(
            "/api/projects",
            json={
                "name": "Excluded",
                "documents_dir": str(docs),
                "exclude_from_default_permissions": True,
            },
        )
        assert created.status_code == 201
        assert created.json()["exclude_from_default_permissions"] is True
        listed = await c.get("/api/projects")
        status = await c.get("/api/projects/Excluded/status")
        updated = await c.patch(
            "/api/projects/Excluded",
            json={"exclude_from_default_permissions": False},
        )
    assert listed.json()["projects"][0]["exclude_from_default_permissions"] is True
    assert status.json()["exclude_from_default_permissions"] is True
    assert updated.status_code == 200
    assert updated.json() == {
        "name": "Excluded", "exclude_from_default_permissions": False,
    }
    assert Registry(registry.path).get("Excluded").exclude_from_default_permissions is False


async def test_project_default_permissions_patch_is_strict(ctx):
    app, _registry, _config, docs = ctx
    async with await _client(app) as c:
        await c.post("/api/projects", json={"name": "KEI", "documents_dir": str(docs)})
        unknown = await c.patch(
            "/api/projects/KEI",
            json={"exclude_from_default_permissions": True, "enabled": False},
        )
        invalid = await c.patch(
            "/api/projects/KEI",
            json={"exclude_from_default_permissions": "true"},
        )
        missing = await c.patch("/api/projects/KEI", json={})
        absent = await c.patch(
            "/api/projects/missing",
            json={"exclude_from_default_permissions": True},
        )
    assert unknown.status_code == 422
    assert invalid.status_code == 422
    assert missing.status_code == 422
    assert absent.status_code == 404


async def test_add_duplicate_409(ctx):
    app, _, _, docs = ctx
    async with await _client(app) as c:
        await c.post("/api/projects", json={"name": "KEI", "documents_dir": str(docs)})
        r = await c.post("/api/projects", json={"name": "KEI", "documents_dir": str(docs)})
    assert r.status_code == 409


async def test_add_bad_name_400(ctx):
    app, _, _, docs = ctx
    async with await _client(app) as c:
        r = await c.post("/api/projects", json={"name": "has space", "documents_dir": str(docs)})
    assert r.status_code == 400


async def test_add_missing_docs_dir_400(ctx):
    app, *_ = ctx
    async with await _client(app) as c:
        r = await c.post(
            "/api/projects", json={"name": "X", "documents_dir": "B:/nope/does-not-exist"}
        )
    assert r.status_code == 400


async def test_documents_path_info_counts_files_and_bytes(ctx):
    app, _, _, docs = ctx
    nested = docs / "nested"
    nested.mkdir()
    (docs / "one.txt").write_bytes(b"abc")
    (nested / "two.bin").write_bytes(b"12345")
    async with await _client(app) as c:
        r = await c.post("/api/projects/path-info", json={"documents_dir": str(docs)})
    assert r.status_code == 200
    assert r.json() == {
        "status": "ok",
        "path": str(docs.resolve()),
        "file_count": 2,
        "total_bytes": 8,
    }


async def test_documents_root_probe_returns_folder_presentation_ids(ctx, monkeypatch):
    app, _, _, docs = ctx
    root = str(docs.resolve())
    monkeypatch.setattr("cognita.admin_api.configured_document_roots", lambda: [root])
    (docs / "available").mkdir()
    async with await _client(app) as c:
        invalid = await c.post(
            "/api/projects/path-info", json={"root": root, "folder": "../outside"}
        )
        missing = await c.post(
            "/api/projects/path-info", json={"root": root, "folder": "not-created"}
        )
        available = await c.post(
            "/api/projects/path-info", json={"root": root, "folder": "available"}
        )

    assert invalid.status_code == 400
    assert invalid.json()["detail"] == "The folder may not contain '..'."
    assert invalid.json()["presentation_id"] == "admin.folder.invalid"
    assert invalid.json()["presentation_values"] == {"folder": "../outside"}
    assert missing.status_code == 200
    assert missing.json()["presentation_id"] == "admin.folder.missing"
    assert missing.json()["presentation_values"] == {"folder": str(docs / "not-created")}
    assert missing.json()["message"] == "This folder does not exist. Create it first."
    assert available.status_code == 200
    assert available.json()["presentation_id"] == "admin.folder.readwrite"
    assert available.json()["presentation_values"] == {"folder": str(docs / "available")}
    assert available.json()["message"] == "Cognita can read and write this folder."


async def test_documents_path_info_does_not_follow_directory_symlinks(ctx, tmp_path):
    app, _, _, docs = ctx
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "private-name.txt").write_bytes(b"not counted")
    link = docs / "linked-directory"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory symlinks unavailable: {type(exc).__name__}")

    async with await _client(app) as c:
        r = await c.post("/api/projects/path-info", json={"documents_dir": str(docs)})

    assert r.status_code == 200
    assert r.json()["file_count"] == 0
    assert r.json()["total_bytes"] == 0


async def test_documents_path_info_does_not_expose_nested_name_on_scan_failure(
    ctx, monkeypatch
):
    app, _, _, docs = ctx

    def fail_scan(_path):
        raise OSError("access denied: private-name.txt")

    monkeypatch.setattr("cognita.admin_api.os.scandir", fail_scan)
    async with await _client(app) as c:
        r = await c.post("/api/projects/path-info", json={"documents_dir": str(docs)})

    assert r.status_code == 400
    assert r.json()["detail"] == "Documents folder cannot be read completely"
    assert "private-name.txt" not in r.text


@pytest.mark.parametrize("documents_dir", ["relative/folder", "B:/nope/does-not-exist"])
async def test_documents_path_info_rejects_invalid_path(ctx, documents_dir):
    app, *_ = ctx
    async with await _client(app) as c:
        r = await c.post("/api/projects/path-info", json={"documents_dir": documents_dir})
    assert r.status_code == 400
    assert "detail" in r.json()


async def test_regenerate_token_is_retired(ctx):
    app, registry, _, docs = ctx
    async with await _client(app) as c:
        await c.post("/api/projects", json={"name": "KEI", "documents_dir": str(docs)})
        r = await c.post("/api/projects/KEI/api-key")
    assert r.status_code == 410
    assert registry.get("KEI").token_sha256 == ""


async def test_regenerate_unknown_404(ctx):
    app, *_ = ctx
    async with await _client(app) as c:
        r = await c.post("/api/projects/ghost/token")
    assert r.status_code == 410


async def test_remove_project(ctx):
    app, registry, _, docs = ctx
    async with await _client(app) as c:
        await c.post("/api/projects", json={"name": "KEI", "documents_dir": str(docs)})
        r = await c.delete("/api/projects/KEI")
    assert r.status_code == 200
    assert r.json() == {"removed": "KEI", "dataDeleted": False}
    assert registry.get("KEI") is None


async def test_remove_with_delete_data(ctx):
    app, registry, _config, docs = ctx
    async with await _client(app) as c:
        await c.post("/api/projects", json={"name": "KEI", "documents_dir": str(docs)})
        # Simulate an existing index dir under data_root.
        index_dir = Path(registry.get("KEI").data_dir)
        index_dir.mkdir(parents=True, exist_ok=True)
        (index_dir / "chroma.sqlite3").write_text("x", encoding="utf-8")
        r = await c.delete("/api/projects/KEI?deleteData=true")
    assert r.status_code == 200
    assert r.json()["dataDeleted"] is True
    assert not index_dir.exists()
    # Source documents are never touched.
    assert docs.is_dir()


async def test_remove_unknown_404(ctx):
    app, *_ = ctx
    async with await _client(app) as c:
        r = await c.delete("/api/projects/ghost")
    assert r.status_code == 404


async def test_reindex_without_worker_503(ctx):
    app, _, _, docs = ctx
    async with await _client(app) as c:
        await c.post("/api/projects", json={"name": "KEI", "documents_dir": str(docs)})
        r = await c.post("/api/projects/KEI/reindex")
    assert r.status_code == 503


async def test_status_without_worker(ctx):
    app, _, _, docs = ctx
    async with await _client(app) as c:
        await c.post("/api/projects", json={"name": "KEI", "documents_dir": str(docs)})
        r = await c.get("/api/projects/KEI/status")
    assert r.status_code == 200
    body = r.json()
    assert body["worker_status"] == "stopped"
    assert body["doc_count"] is None


# ------------------------------------------------- persisted color theme

async def test_index_without_theme_cookie_follows_system(ctx):
    """No choice made: no data-theme, so Pico follows prefers-color-scheme."""
    app, *_ = ctx
    async with await _client(app) as c:
        r = await c.get("/")
    assert r.status_code == 200
    assert "data-theme" not in r.text.split("<head>", 1)[0]


async def test_index_language_cookie_precedes_accept_language(ctx):
    app, *_ = ctx
    async with await _client(app) as c:
        preferred = await c.get("/", headers={"Accept-Language": "fr-FR, en-US;q=0.8"})
        c.cookies.set("cognita_lang", "pt-BR")
        chosen = await c.get("/", headers={"Accept-Language": "fr-FR"})
    assert '<html lang="fr-FR"' in preferred.text
    assert '<html lang="pt-BR"' in chosen.text


async def test_index_theme_cookie_light_stays_light(ctx):
    app, *_ = ctx
    async with await _client(app) as c:
        c.cookies.set("cognita_theme", "light")
        r = await c.get("/")
    assert r.status_code == 200
    assert 'data-theme="light"' in r.text


async def test_index_theme_cookie_stamps_dark(ctx):
    app, *_ = ctx
    async with await _client(app) as c:
        c.cookies.set("cognita_theme", "dark")
        r = await c.get("/")
    assert r.status_code == 200
    assert 'data-theme="dark"' in r.text
    assert 'data-theme="light"' not in r.text


async def test_index_bogus_theme_falls_back_to_system(ctx):
    """An unrecognized cookie value must never inject an arbitrary attribute."""
    app, *_ = ctx
    async with await _client(app) as c:
        c.cookies.set("cognita_theme", "neon</html>")
        r = await c.get("/")
    assert r.status_code == 200
    assert "data-theme" not in r.text.split("<head>", 1)[0]
    assert "neon" not in r.text


async def test_delete_data_guard_refuses_outside_data_root(ctx, tmp_path):
    """A data_dir that resolves outside data_root must never be deleted."""
    from cognita.admin_api import _safe_delete_index
    from cognita.registry import Project

    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / "keep.txt").write_text("important", encoding="utf-8")
    _, _, config, _ = ctx
    project = Project(name="Z", documents_dir=outside, data_dir=outside)
    assert _safe_delete_index(config, project) is False
    assert outside.is_dir()  # untouched
