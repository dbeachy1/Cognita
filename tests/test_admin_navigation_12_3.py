"""Focused static contracts for the 12.3 connector entity navigation."""

from pathlib import Path

from cognita import __version__
from cognita.localization import load_catalog
from cognita.release_identity import APPLICATION_VERSION

ROOT = Path(__file__).parents[1]
HTML = (ROOT / "src/cognita/web/index.html").read_text(encoding="utf-8")
STATE = (ROOT / "src/cognita/web/admin-state.js").read_text(encoding="utf-8")
JS = (ROOT / "src/cognita/web/app.js").read_text(encoding="utf-8")


def test_connector_entity_and_function_rows_are_independent():
    assert 'id="connector-entity-tabs"' in HTML
    assert 'id="connector-tabs"' in HTML
    assert HTML.count('role="tablist"') == 5
    assert 'data-connector-selector' not in HTML
    assert 'class="connector-card"' not in HTML
    assert "connectorFunctionLabels" in JS
    assert "Add connector" in JS


def test_connector_routes_are_immutable_id_based_and_alias_aware():
    assert "#connectors/add" in STATE
    assert 'encodeURIComponent(String(r.connectorId))' in STATE
    for alias in ("#connectors", "#connectors/setup", "#connectors/clients", "#connectors/access", "#connectors/transfer"):
        assert alias in STATE
    assert "normalizeConnectorRoute" in STATE
    assert "decodeURIComponent(parts[0])" in STATE
    assert 'route && route.legacy ? fn : "settings"' in STATE
    assert 'return { ...route, pending: true }' in JS
    assert 'if (!nextRoute.pending && window.location.hash !== canonical)' in JS


def test_settings_presents_stable_and_immutable_current_urls():
    assert "stable_url" in JS
    assert "current_url" in JS
    assert 't("admin.connectors.stable_url.title")' in JS
    assert 't("admin.connectors.current_url.title")' in JS
    english = load_catalog("en-US")
    assert english["admin.connectors.stable_url.title"] == "Stable MCP URL"
    assert english["admin.connectors.current_url.title"] == "Current generation MCP URL"
    assert 'data-connector-copy-url="stable"' in JS
    assert 'data-connector-copy-url="current"' in JS


def test_connector_drafts_and_bulk_revoke_remain_entity_scoped():
    assert "connectorSettingsDrafts: new Map()" in JS
    assert "captureConnectorSettingsDraft();" in JS
    assert "state.editingConnectorId = connector.id;" in JS
    assert '.filter((grant) => grantConnectorId(grant) === String(connector.id))' in JS
    assert 'api("/api/oauth/grants/" + encodeURIComponent(grantId)' in JS
    assert "function activateShellRoute(route, focus = false, revalidate = true)" in JS
    assert "activateShellRoute(route, false, false);" in JS


def test_admin_assets_use_runtime_version_placeholder_and_match_package():
    for asset in ("admin.css", "admin-state.js", "app.js", "alpine.min.js"):
        assert f"/static/{asset}?v=__COGNITA_VERSION__" in HTML
    # 13.0 §4: neither file spells the version any more — both derive it from
    # `release_identity`, which is what this assertion now proves. The point is
    # unchanged: the asset cache-buster the Admin page serves is the package
    # version, not a third copy of the number.
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert 'version = { attr = "cognita.release_identity.APPLICATION_VERSION" }' in pyproject
    assert f'version = "{__version__}"' not in pyproject
    package = (ROOT / "src/cognita/__init__.py").read_text(encoding="utf-8")
    assert "from .release_identity import APPLICATION_VERSION as __version__" in package
    assert __version__ == APPLICATION_VERSION
