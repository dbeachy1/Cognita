"""Focused contracts for the 12.6 Admin/version boundary."""

from pathlib import Path

from cognita.localization import load_catalog

ROOT = Path(__file__).parents[1]
ADMIN = (ROOT / "src/cognita/admin_api.py").read_text(encoding="utf-8")
WORKSPACE_ADMIN = (ROOT / "src/cognita/workspace_admin.py").read_text(encoding="utf-8")
HTML = (ROOT / "src/cognita/web/index.html").read_text(encoding="utf-8")
LOGIN = (ROOT / "src/cognita/web/login.html").read_text(encoding="utf-8")
JS = (ROOT / "src/cognita/web/app.js").read_text(encoding="utf-8")


def test_version_is_server_sourced_for_both_shells():
    assert "__COGNITA_VERSION__" in HTML
    assert "__COGNITA_VERSION__" in LOGIN
    assert '"/api/bootstrap"' in ADMIN
    assert '"version": __version__' in ADMIN
    assert 'document.title = t("admin.title", { version: bootstrap.version })' in JS


def test_workspace_contract_keeps_three_subtabs_and_unknown_safe_metrics():
    assert HTML.count('role="tablist"') >= 5
    assert 'data-route="#workspaces/runtime"' in HTML
    assert 'data-route="#workspaces/policy"' in HTML
    assert 'data-route="#workspaces/connectors"' in HTML
    assert "formatMaybeBytes" in JS
    assert 'measurement_status' in WORKSPACE_ADMIN
    assert 'path_status' in WORKSPACE_ADMIN


def test_bulk_preview_and_truthful_remove_boundary_are_additive():
    assert "/api/workspaces/bulk-delete/preview" in ADMIN
    assert "preview_bulk_workspace_action" in WORKSPACE_ADMIN
    assert "apply_bulk_workspace_action" in WORKSPACE_ADMIN
    assert '"reset"' not in ADMIN.split("_WORKSPACE_ACTIONS", 1)[1].split("})", 1)[0]
    assert "/api/workspaces/bulk-delete/preview" in JS
    assert "preview_token" in JS


def test_workspace_rows_expose_bounded_owner_error_and_usage_fields():
    for field in ("owner_status", "desired_state", "last_error_code", "runtime_generation",
                  "path_status", "measured_at", "usage_status"):
        assert field in WORKSPACE_ADMIN
    assert 't("admin.workspace.action.diagnostics")' in JS
    assert 't("admin.workspace.action.retry")' in JS


def test_remove_confirmation_shows_exact_workspace_facts_and_bulk_preview_does_not_infer_them():
    assert 't("admin.workspace.remove.confirm.body"' in JS
    english = load_catalog("en-US")
    for label in ("Last real activity", "Actual", "Apparent", "Measured at"):
        assert label in english["admin.workspace.remove.confirm.body"]
    assert "cannot be undone" in english["admin.workspace.remove.confirm.body"]
    assert "workspaceMeasuredValue(record, \"actual_bytes\", \"measured_allocated_bytes\")" in JS
    assert "workspaceMeasuredValue(record, \"apparent_bytes\", \"measured_apparent_bytes\")" in JS
    assert "reclaimEstimateVerified" in JS
    assert "previewTargets || records" not in JS
    assert 'const targetSummary = previewTargets.map' in JS
