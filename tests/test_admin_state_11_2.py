"""Deterministic specifications for the framework-free 11.2 Admin shell state."""

from pathlib import Path

ROOT = Path(__file__).parents[1]
STATE_JS = ROOT / "src" / "cognita" / "web" / "admin-state.js"
INDEX_HTML = ROOT / "src" / "cognita" / "web" / "index.html"


def test_route_normalization_and_encoding():
    source = STATE_JS.read_text(encoding="utf-8")
    assert 'if (value === "#projects/view")' in source
    assert 'if (value === "#authentication/global")' in source
    assert 'decodeURIComponent(encoded)' in source
    assert 'return { ...ROUTE_DEFAULT }' in source
    assert 'encodeURIComponent(r.project)' in source


def test_preload_is_parallel_and_one_slice_failure_isolated():
    source = STATE_JS.read_text(encoding="utf-8")
    assert source.count('"/api/') >= 5
    assert 'return Promise.all(tasks)' in source
    assert 'rejectRequest(state, name, generation, error)' in source
    assert 'slice.status = slice.data === null ? "error" : "ready"' in source


def test_request_generation_and_mutation_matrix():
    source = STATE_JS.read_text(encoding="utf-8")
    assert 'generation !== slice.generation' in source
    assert '"project:create": ["projects", "connectors", "authentication", "oauthStatus", "oauthGrants"]' in source
    assert '"oauth:revoke-all": ["oauthGrants", "projects"]' in source
    assert '"authentication:key-generate": ["authentication"]' in source
    assert 'slice.invalidated = true' in source


def test_shell_assets_and_aria_panels_are_cache_busted():
    html = INDEX_HTML.read_text(encoding="utf-8")
    assert 'href="/static/admin.css?v=__COGNITA_VERSION__"' in html
    assert 'src="/static/admin-state.js?v=__COGNITA_VERSION__"' in html
    assert 'src="/static/app.js?v=__COGNITA_VERSION__"' in html
    assert 'src="/static/alpine.min.js?v=__COGNITA_VERSION__"' in html
    # Connector entity and function tablists are dynamic; their panels remain
    # explicit ARIA tabpanels in the static shell.
    assert html.count('role="tablist"') == 5
    assert html.count('role="tabpanel"') == 14
    assert 'id="connector-entity-tabs"' in html
    assert 'id="connector-tabs"' in html
    assert 'id="connector-access-panel"' in html
    assert 'id="connector-transfer-panel"' in html
    assert 'id="panel-connectors"' in html and 'id="panel-authentication"' in html
    assert '<style>' not in html


def test_tab_strips_are_compact_and_left_aligned():
    css = (ROOT / "src" / "cognita" / "web" / "admin.css").read_text(encoding="utf-8")
    assert ".admin-tabs { display: flex; justify-content: flex-start; gap: .35rem;" in css
