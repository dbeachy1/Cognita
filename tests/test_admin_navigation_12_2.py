"""Static and deterministic contracts for the 12.2 Admin navigation shell."""

from pathlib import Path

ROOT = Path(__file__).parents[1]
HTML = (ROOT / "src/cognita/web/index.html").read_text(encoding="utf-8")
STATE = (ROOT / "src/cognita/web/admin-state.js").read_text(encoding="utf-8")
JS = (ROOT / "src/cognita/web/app.js").read_text(encoding="utf-8")
CSS = (ROOT / "src/cognita/web/admin.css").read_text(encoding="utf-8")


def test_canonical_connector_and_workspace_routes_and_aliases():
    for route in (
        "#connectors/setup", "#connectors/clients", "#connectors/access", "#connectors/transfer",
        "#workspaces/runtime", "#workspaces/policy", "#workspaces/connectors",
    ):
        assert f'"{route}"' in STATE or f"'{route}'" in STATE
    # Connector function routes are now generated for the selected immutable-ID
    # entity; only the top-level Connectors entry remains static HTML.
    assert 'data-route="#connectors/setup"' in HTML
    for route in ("#connectors/clients", "#connectors/access", "#connectors/transfer"):
        assert f'data-route="{route}"' not in HTML
    # 12.3 resolves legacy connector fragments through the alias table before
    # normalization, rather than branching on one raw hash literal.
    assert "CONNECTOR_LEGACY_ALIASES" in STATE
    assert "const alias = CONNECTOR_LEGACY_ALIASES[raw]" in STATE
    assert "legacy: true" in STATE
    assert 'value === "#workspaces"' in STATE
    assert 'replaceState(null, "", fragment)' in JS


def test_connector_and_workspace_tablists_are_top_level_and_complete():
    assert HTML.count('role="tablist"') == 5
    assert HTML.index('id="connector-entity-tabs"') < HTML.index('id="connector-tabs"')
    assert HTML.index('id="connector-tabs"') < HTML.index('id="connector-setup-panel"')
    assert HTML.index('id="workspace-tabs"') < HTML.index('id="workspace-runtime-panel"')
    assert HTML.index('id="connector-access-panel"') > HTML.index("</form>", HTML.index('id="connector-form"'))
    assert 'id="connector-access-selector"' not in HTML
    assert 'id="connector-transfer-selector"' not in HTML
    assert 'id="connector-policy-transfer-default"' in HTML
    assert 'id="connector-policy-high-trust-confirm"' in HTML
    assert 'id="setup-material-card"' not in HTML
    assert "Client setup material" not in HTML + JS


def test_discrete_tabs_have_theme_responsive_accessible_states():
    assert "column-gap: 8px" in CSS
    assert "border: 0" in CSS
    assert "min-height: 40px" in CSS
    assert '[aria-selected="true"]' in CSS
    assert ":focus-visible" in CSS
    assert 'overflow-x: auto' in CSS
    assert "--admin-tab-foreground: #373c44" in CSS
    assert 'html[data-theme="dark"]' in CSS


def test_connection_instructions_are_credential_scoped_and_transient():
    assert 't("admin.credential.setup.heading")' in JS
    assert "connection-instructions" in JS
    assert "current_password: proof" in JS
    assert "localStorage" in JS
    assert 'window.adminNavigate("#workspaces/connectors")' in JS


def test_primary_selection_and_history_routes_are_canonical():
    assert 'button.id.startsWith("tab-")' in JS
    assert 'route.top === button.id.replace(/^tab-/' in JS
    assert 'window.location.hash !== canonical' in JS
    assert 'window.history.replaceState(null, "", canonical)' in JS
    assert 'window.adminNavigate("#connectors/clients")' in JS


def test_policy_saves_are_isolated_and_drafts_are_memory_only():
    assert "state.connectorDrafts.set" in JS
    assert "captureConnectorDraft();" in JS
    access_payload = JS.index('const payload = kind === "access"')
    request = JS.index('await api("/api/connectors/"', access_payload)
    policy_block = JS[access_payload:request]
    assert "project_mode: draft.access.project_mode" in policy_block
    assert "default_access: draft.access.default_access" in policy_block
    assert "project_access: draft.access.project_access" in policy_block
    assert "default_workspace_transfer: draft.transfer.default_workspace_transfer" in policy_block
    assert "project_transfer: draft.transfer.project_transfer" in policy_block
    assert "confirm_high_trust: draft.transfer.confirm_high_trust" in policy_block
    assert "name:" not in policy_block
    assert "enabled:" not in policy_block
