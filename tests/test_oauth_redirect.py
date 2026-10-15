"""The shared OAuth redirect rule (16.1.5): any HTTPS host, loopback-only HTTP."""
import pytest

from cognita.oauth_redirect import redirect_uri_safe


@pytest.mark.parametrize("uri", [
    "https://www.typingmind.com/oauth/callback",
    "https://www.typingmind.com",
    "https://claude.ai/api/mcp/auth_callback",
    "https://chatgpt.com/connector_platform_oauth_redirect?x=1",
    "http://127.0.0.1:49152/callback",
    "http://localhost:3000/callback",
    "http://[::1]:8080/callback",
    "HTTPS://Example.COM/cb",
    "https://xn--clude-6ve.ai/cb",
    # The proxy checks the outgoing Location, which carries the client's state.
    "https://client.example/cb?code=abc&state=" + "s" * 4096,
])
def test_safe_redirects(uri):
    assert redirect_uri_safe(uri)


@pytest.mark.parametrize("uri", [
    "http://evil.example/callback",
    "http://localhost.evil.example/callback",
    "https://user:pw@example.com/cb",
    "https://example.com/cb#fragment",
    "javascript:alert(1)",
    "ftp://example.com/cb",
    "https:///no-host",
    "https://example.com:99999/cb",
    "https://exa\x00mple.com/cb",
    "https://clаude.ai/cb",
    "https://cl%D0%B0ude.ai/cb",
    "https://a.com;frame-ancestors/cb",
    "https://a,b.com/cb",
    "https://a_b.com/cb",
    "",
    None,
    42,
])
def test_unsafe_redirects(uri):
    assert not redirect_uri_safe(uri)


def test_consent_csp_permits_the_callback_origin_except_for_ipv6_literals():
    from django.conf import settings

    if not settings.configured:  # same minimal setup as test_oauth_cimd_auth_12_3
        settings.configure(
            SECRET_KEY="redirect-test",
            USE_TZ=True,
            INSTALLED_APPS=["django.contrib.auth", "django.contrib.contenttypes", "oauth2_provider"],
            DATABASES={"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}},
            OAUTH2_PROVIDER={},
        )
        import django

        django.setup()
    from django.http import HttpResponse

    from cognita.oauth_service.views import _browser_security

    csp = _browser_security(HttpResponse(), "https://www.typingmind.com/oauth/callback?x=1")["Content-Security-Policy"]
    assert "form-action 'self' https://www.typingmind.com;" in csp
    csp = _browser_security(HttpResponse(), "http://localhost:3000/cb")["Content-Security-Policy"]
    assert "form-action 'self' http://localhost:3000;" in csp
    csp = _browser_security(HttpResponse(), "http://evil.example/cb")["Content-Security-Policy"]
    assert "form-action 'self';" in csp
    csp = _browser_security(HttpResponse(), None)["Content-Security-Policy"]
    assert "form-action 'self';" in csp
    # CSP has no IPv6 source syntax; omit form-action so Chrome does not block.
    csp = _browser_security(HttpResponse(), "http://[::1]:8080/cb")["Content-Security-Policy"]
    assert "form-action" not in csp and "frame-ancestors 'none'" in csp
