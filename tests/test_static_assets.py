"""Vendored admin-UI assets are served from /static (D7: no CDN, no build step).

Guards against a missing/empty vendored file (e.g. alpine.min.js dropped from a
checkout) shipping a silently broken admin UI — the modals would just never
open, with no server-side symptom.
"""

from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from cognita.admin_api import create_admin_app
from cognita.config import CognitaConfig
from cognita.registry import Registry
from cognita import __version__


@pytest.fixture
def app(tmp_path):
    registry_path = tmp_path / "registry.yaml"
    config = CognitaConfig(
        registry_path=registry_path, data_root=tmp_path / "data", admin_password_sha256=""
    )
    config.admin_allowed_hosts = ["*"]  # ASGI client sends Host: t
    return create_admin_app(config, Registry(registry_path))


@pytest.mark.parametrize("path,marker", [
    ("/static/app.js", b"alpine:init"),      # modal component registration
    ("/static/admin-state.js", b"CognitaAdminState"),
    ("/static/admin.css", b"Cognita-specific Admin layout"),
    ("/static/alpine.min.js", b"Alpine"),    # vendored Alpine 3.x
    ("/static/pico.min.css", b"--pico"),     # vendored Pico v2
])
async def test_vendored_asset_served(app, path, marker):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.get(path)
    assert r.status_code == 200
    assert marker in r.content


async def test_index_wires_modal_and_scripts(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.get("/")
    assert r.status_code == 200
    html = r.text
    assert 'x-data="modal"' in html
    assert 'id="authentication-card"' in html
    assert 'id="debug-tokens-toggle"' not in html
    assert 'data-section="authorized-clients"' in html
    assert 'data-i18n="admin.nav.connectors">Connectors</strong>' in html
    assert "Default Read-only" in html
    assert "All enabled projects" in html
    assert "Selected projects only" in html
    assert 'name="exclude_from_default_permissions"' in html
    assert "Exclude from default permissions" in html
    assert 'id="project-settings-dialog"' in html
    for asset in (
        "/static/favicon.svg", "/static/pico.min.css", "/static/admin.css",
        "/static/admin-state.js", "/static/app.js", "/static/alpine.min.js",
    ):
        assert f'{asset}?v={__version__}' in html
    # app.js must load BEFORE alpine.min.js: the alpine:init listener that
    # registers the modal component has to exist when Alpine boots.
    assert html.index("/static/app.js") < html.index("/static/alpine.min.js")


def test_login_shell_assets_use_runtime_version_placeholder():
    login = (Path(__file__).parents[1] / "src" / "cognita" / "web" / "login.html").read_text(
        encoding="utf-8"
    )
    assert login.count("/static/favicon.svg?v=__COGNITA_VERSION__") == 2
    assert "/static/pico.min.css?v=__COGNITA_VERSION__" in login


async def test_admin_javascript_wires_authentication_policy(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        js = (await c.get("/static/app.js")).text
    assert 'api("/api/authentication"' in js
    assert "Save and lock out clients" in js
    assert "global-generate" in js


async def test_admin_credential_delete_explains_normal_retention_reset(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        js = (await c.get("/static/app.js")).text
    assert 't("admin.credential.delete.normal")' in js
    assert 't("admin.credential.consequence.normal")' in js
    from cognita.localization import load_catalog
    english = load_catalog("en-US")
    assert "clear any Pin and start a fresh 30-day clock from credential deletion" in english["admin.credential.delete.normal"]
    assert "Clears any Pin and starts a fresh 30-day Workspace retention clock" in english["admin.credential.consequence.normal"]


async def test_admin_javascript_wires_connector_policy_boundary(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        js = (await c.get("/static/app.js")).text
    assert 'api("/api/connectors")' in js
    assert "expected_revision" in js
    assert "revision_conflict" in js
    assert "X-CSRF-Token" in js
    assert "future enabled projects are included unless excluded from defaults" in js
    assert "Excluded by project default" in js
    assert 'method: "PATCH"' in js
    assert "exclude_from_default_permissions" in js
    assert 'data-act="settings"' in js
    assert "connectionSummary" in js
    assert 'connector.name || t("admin.oauth.deleted_connector")' in js
    assert "const draft = connectorDraft(connector);" in js
    assert "captureConnectorDraft();" in js
    assert "if (current) beginConnectorEdit(current);" in js
    assert 'typeof ts === "number" ? ts * 1000 : ts' in js


async def test_admin_javascript_displays_code_owned_connector_contract(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        js = (await c.get("/static/app.js")).text
        css = (await c.get("/static/admin.css")).text
    assert "contract_version" in js
    assert "Stable MCP URL" in js
    assert "Current MCP URL" in js
    assert "connector-url-row" in js
    assert "connector-url-row" in css
    assert "Publish next contract version" not in js
    assert '"/contract-version"' not in js
    assert '.connector-url-row button { width: 100%; }' in css
    assert '.connector-card .actions button { margin-bottom: 0; }' in css
