"""Django settings for the isolated Cognita OAuth child."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from cognita.config import CognitaConfig
from cognita.public_url import effective_public_base_url
from cognita.registry import Registry

_RUNTIME_CONFIG: CognitaConfig | None = None


@dataclass(frozen=True)
class ServiceContext:
    config: CognitaConfig
    key: bytes
    store_path: Path
    registry: Registry
    internal_client_id: str = "cognita-internal"
    subject_username: str = "cognita-oauth-subject"


def _allowed_hosts(config: CognitaConfig) -> list[str]:
    hosts = ["127.0.0.1", "localhost", "[::1]", "testserver"]
    parsed = urlparse(config.public_base_url or "")
    if parsed.hostname and parsed.hostname not in hosts:
        hosts.append(parsed.hostname)
    return hosts


class PublicURLRuntimeSettingsMiddleware:
    """Refresh host and cookie policy before Django processes each request.

    The OAuth child is intentionally long-lived while Admin public-URL changes
    take effect immediately.  These Django settings are otherwise frozen at
    child startup, which would reject the new proxy Host or retain the wrong
    Secure-cookie policy after a HTTPS/local-development transition.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        from django.conf import settings

        config = _RUNTIME_CONFIG
        if config is not None:
            value = effective_public_base_url(config)
            config.public_base_url = value
            secure = urlparse(value).scheme.lower() == "https"
            settings.ALLOWED_HOSTS = _allowed_hosts(config)
            settings.SESSION_COOKIE_SECURE = secure
            settings.CSRF_COOKIE_SECURE = secure
        return self.get_response(request)


def configure_django(config: CognitaConfig, key: bytes, store_path: Path) -> None:
    """Configure and initialize Django exactly once for the child process."""
    global _RUNTIME_CONFIG

    import django
    from django.conf import settings

    _RUNTIME_CONFIG = config

    if not settings.configured:
        secure = (urlparse(config.public_base_url or "").scheme.lower() == "https")
        settings.configure(
            DEBUG=False,
            SECRET_KEY=key.hex(),
            ROOT_URLCONF="cognita.oauth_service.root_urls",
            ALLOWED_HOSTS=_allowed_hosts(config),
            USE_TZ=True,
            TIME_ZONE="UTC",
            LANGUAGE_CODE="en-us",
            DEFAULT_AUTO_FIELD="django.db.models.BigAutoField",
            INSTALLED_APPS=[
                "django.contrib.auth",
                "django.contrib.contenttypes",
                "django.contrib.sessions",
                "django.contrib.messages",
                "oauth2_provider",
            ],
            MIDDLEWARE=[
                "cognita.oauth_service.settings.PublicURLRuntimeSettingsMiddleware",
                "django.middleware.security.SecurityMiddleware",
                "django.contrib.sessions.middleware.SessionMiddleware",
                "django.middleware.common.CommonMiddleware",
                "django.middleware.csrf.CsrfViewMiddleware",
                "django.contrib.auth.middleware.AuthenticationMiddleware",
                "django.contrib.messages.middleware.MessageMiddleware",
            ],
            TEMPLATES=[{
                "BACKEND": "django.template.backends.django.DjangoTemplates",
                # Keep the browser presentation project-owned so DOT's default
                # stylesheet reference cannot reappear through app template
                # resolution.  The templates intentionally use an inline style
                # block because the OAuth browser CSP permits only self-contained
                # markup and styles.
                "DIRS": [str(Path(__file__).parent / "templates")],
                "APP_DIRS": True,
                "OPTIONS": {"context_processors": [
                    "django.template.context_processors.request",
                    "django.contrib.auth.context_processors.auth",
                    "django.contrib.messages.context_processors.messages",
                ]},
            }],
            DATABASES={
                "default": {
                    "ENGINE": "django.db.backends.sqlite3",
                    "NAME": str(store_path),
                    "OPTIONS": {"transaction_mode": "IMMEDIATE", "timeout": 20},
                }
            },
            LOGIN_URL="/oauth/login",
            SESSION_COOKIE_NAME="cognita_oauth_session",
            SESSION_COOKIE_AGE=300,
            SESSION_COOKIE_PATH="/oauth",
            SESSION_COOKIE_HTTPONLY=True,
            SESSION_COOKIE_SAMESITE="Lax",
            SESSION_COOKIE_SECURE=secure,
            CSRF_COOKIE_SECURE=secure,
            CSRF_COOKIE_HTTPONLY=False,
            CSRF_COOKIE_PATH="/oauth",
            OAUTH2_PROVIDER={
                # RFC 9207 requires a stable issuer in every authorization
                # response. Keep this tied to the configured public origin;
                # never derive it from an untrusted request Host header.
                "OIDC_ISS_ENDPOINT": config.public_base_url.rstrip("/"),
                "SCOPES": {
                    "cognita:access": "Cognita project access",
                    "introspection": "Internal token introspection",
                },
                "DEFAULT_SCOPES": ["cognita:access"],
                "PKCE_REQUIRED": True,
                "REQUEST_APPROVAL_PROMPT": "force",
                # User-selected 8.0 policy: DOT owns reusable refresh credentials. A
                # one-shot administrative revoke is available at child startup for
                # emergency recovery; ordinary starts preserve live connections.
                "ROTATE_REFRESH_TOKEN": False,
                "REFRESH_TOKEN_GRACE_PERIOD_SECONDS": 0,
                "REFRESH_TOKEN_REUSE_PROTECTION": False,
                "ACCESS_TOKEN_EXPIRE_SECONDS": int(config.oauth_access_token_ttl_seconds),
                "REFRESH_TOKEN_EXPIRE_SECONDS": None,
                "COMPLIANT_BCP_RFC9700_TOKEN_STORAGE": True,
                "COMPLIANT_BCP_RFC9700_AUTHZ_RESPONSE_ISS": True,
                "COMPLIANT_BCP_RFC9700_IMPLICIT_GRANT": True,
                "COMPLIANT_BCP_RFC9700_PASSWORD_GRANT": True,
                "COMPLIANT_BCP_RFC9700_PKCE_METHOD": True,
                "COMPLIANT_BCP_RFC9700_PKCE_REQUIRED": True,
                "ALLOWED_REDIRECT_URI_SCHEMES": ["https", "http"],
                "ALLOW_URI_WILDCARDS": False,
                "OAUTH2_RESPONSE_TYPES_SUPPORTED": ["code"],
                "OAUTH2_GRANT_TYPES_SUPPORTED": ["authorization_code", "refresh_token"],
                "OAUTH2_TOKEN_ENDPOINT_AUTH_METHODS_SUPPORTED": ["none"],
                "DCR_ENABLED": True,
                "DCR_REGISTRATION_PERMISSION_CLASSES": (
                    "oauth2_provider.dcr.AllowAllDCRPermission",
                ),
                "CIMD_ENABLED": True,
                "CIMD_ALLOWED_HOSTS": list(config.oauth_cimd_allowed_hosts),
                "CIMD_REGISTRATION_PERMISSION_CLASSES": (
                    "oauth2_provider.cimd.HostAllowlistCIMDPermission",
                ),
                # Cognita's validator adds RFC 7523 private_key_jwt for the
                # OpenAI CIMD client while retaining DOT's public ``none`` path.
                "CIMD_JWKS_FETCHER": "cognita.oauth_service.cimd_auth.SafeJWKSFetcher",
                "OAUTH2_VALIDATOR_CLASS": "cognita.oauth_service.policy.CognitaOAuth2Validator",
            },
        )
    django.setup()
