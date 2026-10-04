"""Static contract checks for the 11.2 Admin panel migration."""

from pathlib import Path


ROOT = Path(__file__).parents[1]
HTML = (ROOT / "src/cognita/web/index.html").read_text(encoding="utf-8")
JS = (ROOT / "src/cognita/web/app.js").read_text(encoding="utf-8")
CSS = (ROOT / "src/cognita/web/admin.css").read_text(encoding="utf-8")


def test_authentication_panels_use_native_accordions_and_local_controls():
    assert 'id="authentication-global-details"' in HTML
    assert '<details class="auth-project"' in JS
    assert 'id="authentication-search"' in HTML
    assert 'id="authentication-expand-all"' in HTML
    assert 'id="authentication-collapse-all"' in HTML
    assert 'data-auth-action="global-generate"' in JS
    assert 'data-auth-action="project-revoke"' in JS


def test_key_lifecycle_is_immediate_and_one_time():
    assert "/static-key/" in JS
    assert 'action === "generate"' in JS and 'action === "revoke"' in JS
    assert 'static_key_action: "unchanged"' in JS
    assert "The key may have been replaced, but its value was not received" in JS
    assert 'navigator.clipboard.writeText("")' not in JS
    assert 'id="copy-auth-key"' in HTML
    assert ">Copy key</button>" in HTML


def test_first_revoke_request_explicitly_declines_lockout_confirmation():
    assert 'if (action === "revoke") requestBody.confirm_lockout = false;' in JS


def test_project_oauth_edits_are_persisted_as_drafts_before_render():
    assert "authState.drafts.set(row.dataset.authProject, draft);" in JS


def test_key_modal_clears_secret_on_every_close_path():
    assert '$("#key-dialog").addEventListener("close"' in JS


def test_project_create_and_deep_link_controls_exist():
    assert 'data-created-auth' in JS
    assert 'data-created-another' in JS
    assert 'id="project-settings-auth"' in HTML
    assert '#authentication/" + encodeURIComponent' in JS


def test_responsive_authentication_and_focus_styles_exist():
    assert ".auth-project summary" in CSS
    assert ".project-highlight" in CSS
    assert 'role="tabpanel"' in HTML
    assert 'hidden' in JS
