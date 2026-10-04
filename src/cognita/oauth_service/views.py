"""DOT view adapters and bounded internal control endpoints."""

from __future__ import annotations

import base64
import binascii
import copy
import hashlib
import hmac
import ipaddress
import json
import logging
import time
from collections import defaultdict, deque
from pathlib import Path
from urllib.parse import urlencode, urlparse, urlsplit

from django.contrib.auth import get_user_model, login, logout
from django.contrib.auth.hashers import check_password
from django.db import transaction
from django.http import HttpRequest, HttpResponse, HttpResponseBadRequest, JsonResponse, QueryDict
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views import View
from oauth2_provider.exceptions import OAuthToolkitError
from oauth2_provider.models import (
    get_access_token_model,
    get_application_model,
    get_refresh_token_model,
    refresh_token_expire_timedelta,
    revoke_access_token,
)
from oauth2_provider.oauth2_backends import _add_iss_to_redirect
from oauth2_provider.settings import oauth2_settings
from oauth2_provider.views import AuthorizationView
from oauth2_provider.views.dynamic_client_registration import (
    DynamicClientRegistrationManagementView,
    DynamicClientRegistrationView,
)
from oauth2_provider.views.introspect import IntrospectTokenView
from oauthlib.oauth2.rfc6749.errors import CustomOAuth2Error

from cognita.admin_auth import has_argon2_credentials, verify_login
from cognita.localization import SUPPORTED_LOCALES, resolve_locale, translate
from cognita.public_url import effective_public_base_url

from .policy import get_policy
from .principal import OAuthPrincipalStore
from .settings import ServiceContext

log = logging.getLogger("cognita.oauth_service.views")
_CONTEXT: ServiceContext | None = None
_LOGIN_FAILURES: dict[str, deque[float]] = defaultdict(deque)
_LOGIN_FAILURE_LIMIT = 5
_LOGIN_FAILURE_WINDOW_S = 60
_LOGIN_FAILURE_BUCKET_CAP = 1024
_LANGUAGE_OPTIONS = (
    ("en-US", "English"),
    ("es-ES", "Español"),
    ("fr-FR", "Français"),
    ("de-DE", "Deutsch"),
    ("it-IT", "Italiano"),
    ("pt-BR", "Português (Brasil)"),
)


def set_context(ctx: ServiceContext) -> None:
    global _CONTEXT
    _CONTEXT = ctx


def context() -> ServiceContext:
    if _CONTEXT is None:
        raise RuntimeError("OAuth service context is not initialized")
    return _CONTEXT


def _public_base_url() -> str:
    """Refresh the persisted Admin URL before producing OAuth metadata."""
    value = effective_public_base_url(context().config).rstrip("/")
    context().config.public_base_url = value
    # DOT reads this setting for RFC 9207 authorization error redirects. The
    # metadata views below use the same refreshed value directly.
    try:
        oauth2_settings.user_settings["OIDC_ISS_ENDPOINT"] = value
        if "OIDC_ISS_ENDPOINT" in oauth2_settings._cached_attrs:
            del oauth2_settings.OIDC_ISS_ENDPOINT
            oauth2_settings._cached_attrs.discard("OIDC_ISS_ENDPOINT")
    except (AttributeError, TypeError):
        log.warning("OAuth issuer setting could not be refreshed")
    return value


def _header_locale(request: HttpRequest) -> str:
    """Choose a supported locale from Accept-Language without trusting raw tags."""
    choices: list[tuple[float, int, str]] = []
    for index, part in enumerate(request.headers.get("Accept-Language", "").split(",")[:32]):
        fields = [field.strip() for field in part.split(";")]
        tag = fields[0].replace("_", "-")
        quality = 1.0
        for field in fields[1:]:
            if field.startswith("q="):
                try:
                    quality = float(field[2:])
                except ValueError:
                    quality = 0.0
                break
        if not 0.0 < quality <= 1.0:
            continue
        normalized = tag.lower()
        match = next((supported for supported in SUPPORTED_LOCALES if supported.lower() == normalized), None)
        if match is None and normalized in {"es", "fr", "de", "it"}:
            match = resolve_locale(normalized)
        if match is not None:
            choices.append((quality, -index, match))
    return max(choices)[2] if choices else "en-US"


def _locale_for(request: HttpRequest) -> str:
    cookie = request.COOKIES.get("cognita_lang", "")
    if cookie in SUPPORTED_LOCALES:
        return cookie
    return _header_locale(request)


def _message(locale: str, key: str, values: dict | None = None) -> str:
    return translate(locale, key, values)


def _language_page_context(request: HttpRequest) -> dict:
    locale = _locale_for(request)
    return {
        "locale": locale,
        "language_options": [
            {"tag": tag, "name": name, "selected": tag == locale}
            for tag, name in _LANGUAGE_OPTIONS
        ],
        "language_label": _message(locale, "oauth.language.label"),
        "language_submit": _message(locale, "oauth.language.submit"),
    }


def _set_language_cookie(response: HttpResponse, locale: str) -> HttpResponse:
    response.set_cookie(
        "cognita_lang",
        locale,
        max_age=365 * 24 * 60 * 60,
        path="/oauth",
        secure=urlparse(_public_base_url()).scheme.lower() == "https",
        httponly=True,
        samesite="Lax",
    )
    return response


def _language_switch(request: HttpRequest, *, login_page: bool = False) -> HttpResponse:
    locale = request.POST.get("language", "")
    target = request.POST.get("next", "")
    if locale not in SUPPORTED_LOCALES:
        return HttpResponseBadRequest("Invalid language.")
    validated = _validated_next(request, target)
    if validated is None:
        return HttpResponseBadRequest("Authorization request is invalid.")
    destination = f"/oauth/login?{urlencode({'next': validated})}" if login_page else validated
    return _set_language_cookie(redirect(destination), locale)


def _json_error(error: str, description: str, status: int = 400) -> JsonResponse:
    response = JsonResponse({"error": error, "error_description": description}, status=status)
    response["Cache-Control"] = "no-store"
    return response


def _internal_ok(request: HttpRequest) -> bool:
    header = request.headers.get("Authorization", "")
    if not header.startswith("Basic "):
        return False
    try:
        raw = base64.b64decode(header[6:].encode("ascii"), validate=True)
        client_id, secret = raw.split(b":", 1)
        presented_id = client_id.decode("utf-8")
        presented_secret = secret.decode("utf-8")
    except (ValueError, UnicodeError, binascii.Error):
        return False
    if not hmac.compare_digest(presented_id, context().internal_client_id):
        return False
    app = get_application_model().objects.filter(client_id=presented_id).first()
    return bool(app and check_password(presented_secret, app.client_secret))


def _internal_error() -> JsonResponse:
    response = JsonResponse({"error": "unauthorized"}, status=401)
    response["WWW-Authenticate"] = 'Basic realm="cognita-oauth"'
    response["Cache-Control"] = "no-store"
    return response


def _browser_security(response: HttpResponse) -> HttpResponse:
    """Apply the browser-flow protections to pages, errors, and redirects."""
    response["Cache-Control"] = "no-store"
    response["Pragma"] = "no-cache"
    response["Content-Security-Policy"] = (
        "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; "
        "frame-ancestors 'none'; base-uri 'none'"
    )
    response["X-Frame-Options"] = "DENY"
    response["Referrer-Policy"] = "no-referrer"
    response["X-Content-Type-Options"] = "nosniff"
    return response


def _client_address(request: HttpRequest) -> str:
    """Use only the sanitized address supplied by the loopback parent."""
    for candidate in (
        request.headers.get("X-Real-IP", "").strip(),
        str(request.META.get("REMOTE_ADDR", "?")),
    ):
        try:
            return str(ipaddress.ip_address(candidate))
        except ValueError:
            continue
    return "?"


def _login_blocked(address: str) -> bool:
    now = time.monotonic()
    failures = _LOGIN_FAILURES.get(address)
    if failures is None:
        return False
    while failures and failures[0] < now - _LOGIN_FAILURE_WINDOW_S:
        failures.popleft()
    if not failures:
        _LOGIN_FAILURES.pop(address, None)
        return False
    return len(failures) >= _LOGIN_FAILURE_LIMIT


def _record_login_failure(address: str) -> None:
    if len(_LOGIN_FAILURES) >= _LOGIN_FAILURE_BUCKET_CAP and address not in _LOGIN_FAILURES:
        _LOGIN_FAILURES.pop(next(iter(_LOGIN_FAILURES)))
    _LOGIN_FAILURES[address].append(time.monotonic())


def _validated_next(request: HttpRequest, value: str) -> str | None:
    """Validate the bounded preserved authorization request through DOT itself."""
    if not isinstance(value, str) or not value or len(value) > 8192:
        return None
    parsed = urlsplit(value)
    if parsed.scheme or parsed.netloc or parsed.fragment or parsed.path != "/oauth/authorize":
        return None
    probe = copy.copy(request)
    probe.META = dict(request.META)
    probe.META["QUERY_STRING"] = parsed.query
    probe.path = probe.path_info = parsed.path
    probe.GET = QueryDict(parsed.query)
    view = CognitaAuthorizationView()
    view.setup(probe)
    view.oauth2_data = {}
    try:
        _scopes, credentials = view.validate_authorization_request(probe)
    except OAuthToolkitError:
        return None
    application = get_application_model().objects.filter(
        client_id=credentials.get("client_id")
    ).first()
    if application is None or application.skip_authorization:
        return None
    return value


class ReadyView(View):
    def get(self, request: HttpRequest) -> JsonResponse:
        if not _internal_ok(request):
            return _internal_error()
        if not has_argon2_credentials(context().config):
            return JsonResponse({"ready": False}, status=503)
        return JsonResponse({"ready": True})


class LoginView(View):
    def dispatch(self, request, *args, **kwargs):
        return _browser_security(super().dispatch(request, *args, **kwargs))

    @staticmethod
    def _page(
        request: HttpRequest,
        next_url: str,
        error: str | None = None,
        *,
        status: int = 200,
    ) -> HttpResponse:
        """Render the short browser-flow login page without exposing secrets."""
        page = _language_page_context(request)
        locale = page["locale"]
        page.update({
            "next": next_url,
            "error": _message(locale, error) if error else None,
            "ui": {
                "title": _message(locale, "oauth.title.login"),
                "heading": _message(locale, "oauth.login.heading"),
                "intro": _message(locale, "oauth.login.intro"),
                "username": _message(locale, "oauth.login.username"),
                "password": _message(locale, "oauth.login.password"),
                "continue": _message(locale, "oauth.login.continue"),
            },
        })
        return render(
            request,
            "cognita/login.html",
            page,
            status=status,
        )

    def get(self, request: HttpRequest) -> HttpResponse:
        next_url = _validated_next(request, request.GET.get("next", ""))
        if next_url is None:
            return _json_error("invalid_request", "Authorization request is invalid.")
        return self._page(request, next_url)


    def post(self, request: HttpRequest) -> HttpResponse:
        if "change_language" in request.POST:
            return _language_switch(request, login_page=True)
        username = request.POST.get("username", "")
        password = request.POST.get("password", "")
        next_url = _validated_next(request, request.POST.get("next", ""))
        if next_url is None:
            return _json_error("invalid_request", "Authorization request is invalid.")
        address = _client_address(request)
        if _login_blocked(address):
            log.warning("OAuth login rejected reason=login_rate_limit remote=%s", address)
            return self._page(
                request,
                next_url,
                "oauth.login.too_many_attempts",
                status=429,
            )
        if not has_argon2_credentials(context().config) or not verify_login(
            context().config, username, password
        ):
            _record_login_failure(address)
            log.warning("OAuth login rejected reason=invalid_login remote=%s", address)
            return self._page(request, next_url, "oauth.login.invalid_credentials", status=401)
        _LOGIN_FAILURES.pop(address, None)
        subject = get_user_model().objects.get(username=context().subject_username)
        login(request, subject, backend="django.contrib.auth.backends.ModelBackend")
        request.session.set_expiry(300)
        return redirect(next_url)


class CognitaAuthorizationView(AuthorizationView):
    def get_context_data(self, **kwargs):
        context_data = super().get_context_data(**kwargs)
        page = _language_page_context(self.request)
        locale = page["locale"]
        page["language_next"] = (
            _validated_next(self.request, "/oauth/authorize?" + self.request.GET.urlencode())
            if self.request.GET else None
        )
        ui = {
            "title": _message(locale, "oauth.consent.title"),
            "title_error": _message(locale, "oauth.consent.title_error"),
            "heading_error": _message(locale, "oauth.consent.heading_error"),
            "error_return": _message(locale, "oauth.consent.error_return"),
            "intro": _message(locale, "oauth.consent.intro"),
            "connector_access": _message(locale, "oauth.consent.connector_access"),
            "no_projects": _message(locale, "oauth.consent.no_projects"),
            "unavailable_connector": _message(locale, "oauth.consent.unavailable_connector"),
            "review": _message(locale, "oauth.consent.review"),
            "deny": _message(locale, "oauth.consent.deny"),
            "authorize": _message(locale, "oauth.consent.authorize"),
        }
        application = context_data.get("application")
        if application is not None:
            ui["heading"] = _message(
                locale, "oauth.consent.heading", {"application": application.name}
            )
        error = context_data.get("error")
        if error:
            error_id = (
                "oauth.consent.error_invalid_request"
                if getattr(error, "error", "") == "invalid_request"
                else "oauth.consent.error_unknown"
            )
            ui["error_message"] = _message(locale, error_id)
        page["ui"] = ui
        context_data.update(page)
        resource = kwargs.get("resource") or self.oauth2_data.get("resource")
        if isinstance(resource, str):
            policy = get_policy()
            summary = policy.connection_summary(resource)
            definition = policy.connector_for(resource)
            if summary is not None and definition is not None:
                mode_key = (
                    "oauth.consent.project_mode_all"
                    if definition.project_mode == "all"
                    else "oauth.consent.project_mode_selected"
                )
                mode_values = {"name": summary["name"]}
                if definition.project_mode == "all":
                    mode_values["access"] = _message(
                        locale, f"oauth.access.{definition.default_access}"
                    )
                ui["project_mode"] = _message(
                    locale,
                    mode_key,
                    mode_values,
                )
                summary = {
                    **summary,
                    "projects": [
                        {
                            **project,
                            "access_label": _message(locale, f"oauth.access.{project['access']}"),
                        }
                        for project in summary.get("projects", [])
                    ],
                    "project_mode": definition.project_mode,
                }
            context_data["connector"] = summary
        return context_data

    def error_response(self, error, application, **kwargs):
        """Add RFC 9207's issuer after DOT builds an authorization error redirect.

        Toolkit's RFC 9700 setting covers successful responses in its OAuthLib
        core. Error responses are assembled by the view before that core hook,
        so preserve Toolkit's validation and redirect ownership and add the
        same configured issuer to the resulting redirect only.
        """
        response = super().error_response(error, application, **kwargs)
        if response.status_code >= 400 and response.get("Content-Type", "").startswith("text/html"):
            page = _language_page_context(self.request)
            code = getattr(getattr(error, "error", None), "error", "")
            error_id = (
                "oauth.consent.error_invalid_request"
                if code == "invalid_request"
                else "oauth.consent.error_unknown"
            )
            locale = page["locale"]
            page["ui"] = {
                "title_error": _message(locale, "oauth.consent.title_error"),
                "heading_error": _message(locale, "oauth.consent.heading_error"),
                "error_return": _message(locale, "oauth.consent.error_return"),
                "error_message": _message(locale, error_id),
            }
            response = render(
                self.request,
                "oauth2_provider/authorize.html",
                {**page, "error": True},
                status=response.status_code,
            )
        location = response.get("Location")
        if location and oauth2_settings.COMPLIANT_BCP_RFC9700_AUTHZ_RESPONSE_ISS:
            response["Location"] = _add_iss_to_redirect(
                location,
                _public_base_url(),
            )
        return response

    def dispatch(self, request, *args, **kwargs):
        _public_base_url()
        if request.method == "GET" and not request.user.is_authenticated:
            self.oauth2_data = {}
            try:
                _scopes, credentials = self.validate_authorization_request(request)
                application = get_application_model().objects.filter(
                    client_id=credentials.get("client_id")
                ).first()
                if application is not None and application.skip_authorization:
                    return _browser_security(
                        _json_error("invalid_request", "Authorization approval is required.")
                    )
            except OAuthToolkitError as error:
                return _browser_security(self.error_response(error, application=None))
            except Exception as exc:
                log.error("OAuth authorization prevalidation failed type=%s", type(exc).__name__)
                return _browser_security(
                    _json_error("invalid_request", "Authorization request is invalid.")
                )
        return _browser_security(super().dispatch(request, *args, **kwargs))

    def get(self, request, *args, **kwargs):
        try:
            _scopes, credentials = self.validate_authorization_request(request)
            application = get_application_model().objects.filter(
                client_id=credentials.get("client_id")
            ).first()
            if application is not None and application.skip_authorization:
                return _json_error("invalid_request", "Authorization approval is required.")
            return super().get(request, *args, **kwargs)
        except OAuthToolkitError:
            return super().get(request, *args, **kwargs)
        except Exception as exc:
            log.error("OAuth authorization request failed type=%s", type(exc).__name__)
            return _json_error("invalid_request", "Authorization request is invalid.")


    def form_valid(self, form):
        response = super().form_valid(form)
        logout(self.request)
        response.delete_cookie("cognita_oauth_session", path="/oauth")
        return response

    def post(self, request, *args, **kwargs):
        if "change_language" in request.POST:
            return _language_switch(request)
        return super().post(request, *args, **kwargs)


def _host_allowed(uri: str, configured: list[str]) -> bool:
    parsed = urlparse(uri)
    if parsed.fragment or parsed.username or parsed.password or "*" in uri or not parsed.hostname:
        return False
    host = parsed.hostname.lower()
    allowed = {item.lower().lstrip(".") for item in configured}
    if parsed.scheme == "http":
        return host in {"127.0.0.1", "localhost", "::1"}
    if parsed.scheme != "https":
        return False
    return any(host == item or host.endswith("." + item) for item in allowed)


class CognitaDCRView(DynamicClientRegistrationView):
    @staticmethod
    def _validate(data: dict) -> str | None:
        redirect_uris = data.get("redirect_uris", [])
        if not isinstance(redirect_uris, list) or not 1 <= len(redirect_uris) <= 10:
            return "redirect_uris must contain between one and ten URIs"
        if not all(isinstance(uri, str) and _host_allowed(uri, context().config.oauth_allowed_client_hosts) for uri in redirect_uris):
            return "redirect_uris contains an unsupported URI"
        if data.get("token_endpoint_auth_method", "client_secret_basic") != "none":
            return "Only public clients are supported"
        grant_types = data.get("grant_types", ["authorization_code"])
        if not isinstance(grant_types, list) or set(grant_types) - {"authorization_code", "refresh_token"} or "authorization_code" not in grant_types:
            return "Only authorization_code with optional refresh_token is supported"
        if data.get("response_types", ["code"]) != ["code"]:
            return "Only the code response type is supported"
        scopes = data.get("scope", "cognita:access")
        if not isinstance(scopes, str) or set(scopes.split()) - {"cognita:access"}:
            return "Only the Cognita access scope is supported"
        return None

    def post(self, request, *args, **kwargs):
        try:
            data = json.loads(request.body)
        except (json.JSONDecodeError, ValueError):
            return _json_error("invalid_client_metadata", "Request body must be valid JSON")
        if not isinstance(data, dict):
            return _json_error("invalid_client_metadata", "Request body must be a JSON object")
        error = self._validate(data)
        if error:
            return _json_error("invalid_client_metadata", error)
        # The IMMEDIATE SQLite transaction serializes the capacity check with DOT's
        # Application insert so concurrent anonymous registrations cannot exceed it.
        with transaction.atomic():
            applications = get_application_model().objects.exclude(
                client_id=context().internal_client_id
            )
            if applications.count() >= 100:
                return _json_error(
                    "temporarily_unavailable", "Registration capacity reached.", 503
                )
            return super().post(request, *args, **kwargs)


class CognitaDCRManagementView(DynamicClientRegistrationManagementView):
    pass


class IntrospectionView(IntrospectTokenView):
    """Report a token active only while its connector contract is current.

    DOT's stock introspection view checks token expiry and revocation, but it
    does not revalidate an RFC 8707 audience against mutable server policy.
    Deploying a new code-owned public contract retires the previous audience
    immediately, so that policy check belongs on introspection as well as
    issuance and gateway dispatch.
    """

    @staticmethod
    def get_token_response(token_value=None):
        response = IntrospectTokenView.get_token_response(token_value)
        try:
            claims = json.loads(response.content)
        except (TypeError, ValueError, json.JSONDecodeError):
            return response
        if claims.get("active") is not True:
            return response
        audiences = claims.get("aud")
        try:
            current = (
                isinstance(audiences, list)
                and len(audiences) == 1
                and isinstance(audiences[0], str)
                and get_policy().connector_for(audiences[0]) is not None
            )
        except CustomOAuth2Error:
            current = False
        if current:
            # The principal binding is project-owned and keyed by DOT's token
            # primary key, never by the bearer value.  Historical tokens may
            # have no binding; the current route fails closed when it requires
            # the current-generation principal claim.
            try:
                digest = hashlib.sha256(str(token_value).encode("utf-8")).hexdigest()
                token = get_access_token_model().objects.filter(token_checksum=digest).first()
                if token is None:
                    token = get_access_token_model().objects.filter(token=token_value).first()
                if token is not None:
                    principal = OAuthPrincipalStore(context().store_path).principal_for_token("access", str(token.pk))
                    if principal is not None:
                        claims["cognita_principal_id"] = principal.principal_id
                        response.content = json.dumps(claims).encode("utf-8")
            except Exception as exc:
                log.error("OAuth principal introspection lookup failed type=%s", type(exc).__name__)
            return response
        log.info("OAuth token introspection denied reason=retired_or_unavailable_audience")
        return JsonResponse({"active": False}, status=200)


def _iso(value):
    return value.isoformat().replace("+00:00", "Z") if value else None


def _resource_for(token):
    resources = token.resource or []
    if len(resources) != 1 or not isinstance(resources[0], str):
        return None
    return resources[0]


def _refresh_is_live(token, now) -> bool:
    if token.revoked is not None or token.access_token is None:
        return False
    expiry = refresh_token_expire_timedelta()
    return not expiry or token.access_token.expires + expiry > now


def _connection_key(token, resource):
    return (token.user_id, token.application_id, resource)


def _connection_groups():
    access_model = get_access_token_model()
    refresh_model = get_refresh_token_model()
    groups = defaultdict(lambda: {"access": [], "refresh": []})
    now = timezone.now()

    for token in access_model.objects.select_related("application", "user"):
        if not token.is_valid():
            continue
        resource = _resource_for(token)
        # Keep the complete versioned resource URI as the connection identity.
        # In particular, do not resurrect pre-9.3.2 unversioned audiences from
        # an older OAuth database as visible connections.
        if resource is None or get_policy().connector_resource_for(resource) is None:
            continue
        groups[_connection_key(token, resource)]["access"].append(token)

    for token in refresh_model.objects.filter(revoked__isnull=True).select_related(
        "application", "user", "access_token"
    ):
        if not _refresh_is_live(token, now):
            continue
        resource = _resource_for(token)
        if resource is None or get_policy().connector_resource_for(resource) is None:
            continue
        groups[_connection_key(token, resource)]["refresh"].append(token)
    return groups


def _connection_id(key) -> str:
    subject_id, application_id, resource = key
    payload = f"{subject_id}:{application_id}:{resource}".encode()
    return hmac.new(context().key, payload, hashlib.sha256).hexdigest()[:32]


def _record(key, tokens) -> dict:
    _user_id, _app_id, resource = key
    all_tokens = tokens["refresh"] + tokens["access"]
    app = all_tokens[0].application
    created = min(token.created for token in all_tokens)
    connector = get_policy().connection_summary(resource)
    return {
        "id": _connection_id(key),
        "client_id": app.client_id,
        "client_name": app.name,
        "project": None,
        "connector": connector,
        "resource": resource,
        "created_at": _iso(created),
        "last_used_at": None,
    }


def revoke_all_connections() -> int:
    """Revoke every currently live connection through DOT's native model APIs."""
    return _revoke_groups(_connection_groups())


def _reconcile_revoked_workspaces(principal_ids: tuple[str, ...]) -> None:
    """Mark mapped Workspaces revoked without changing retention or contents.

    The OAuth child and Workspace manager use separate SQLite stores.  Principal
    revocation is completed before this method, and DOT token revocation waits
    until this method succeeds.  A failure therefore leaves the live DOT group
    as a retry handle while access is already denied by principal introspection.
    A missing metadata database means no Workspace has been created yet and is
    intentionally a no-op.
    """
    metadata_path = Path(context().config.data_root) / "workspace-metadata.sqlite3"
    if not metadata_path.is_file():
        return

    from cognita.workspace import WorkspaceMetadataStore

    metadata = WorkspaceMetadataStore(metadata_path)
    reconciled = 0
    try:
        for principal_id in principal_ids:
            record = metadata.get_by_principal(principal_id)
            if record is not None:
                # Revoke alone retains disk, pin state, and any existing
                # retention intent.  Credential deletion is the separate path
                # that records tombstone/retention choices.
                metadata.set_owner_status(record.workspace_id, "revoked")
                reconciled += 1
    finally:
        metadata.close()
    log.info(
        "OAuth connection revoke reconciled Workspace owners principals=%d workspaces=%d",
        len(principal_ids), reconciled,
    )


def _reconcile_connection_principals(groups) -> None:
    """Revoke durable principals and reconcile their Workspace owner status.

    This ordering is deliberate: a crash or partial failure before DOT revoke
    is safe to retry from the still-visible connection group.  Principal
    introspection fails closed immediately, and each operation is idempotent.
    """
    store = OAuthPrincipalStore(context().store_path, create=False)
    principal_ids: set[str] = set()
    for key in groups:
        subject_id, application_id, resource = key
        principal_ids.update(
            store.principal_ids_for_connection(
                str(subject_id), str(application_id), resource
            )
        )
    ordered = tuple(sorted(principal_ids))
    for principal_id in ordered:
        store.revoke_principal(principal_id)
    _reconcile_revoked_workspaces(ordered)
    log.info("OAuth connection revoke reconciled durable principals count=%d", len(ordered))


def _revoke_groups(groups) -> int:
    access_model = get_access_token_model()
    _reconcile_connection_principals(groups)
    changed = 0
    with transaction.atomic():
        for key, tokens in groups.items():
            for token in tokens["refresh"]:
                token.revoke()
            for token in tokens["access"]:
                current = access_model.objects.filter(pk=token.pk).first()
                if current is not None and current.is_valid():
                    revoke_access_token(current)

            if key in _connection_groups():
                raise RuntimeError("DOT revoke did not remove the connection group")
            changed += 1
    return changed


class ConnectionsView(View):
    def dispatch(self, request, *args, **kwargs):
        if not _internal_ok(request):
            return _internal_error()
        return super().dispatch(request, *args, **kwargs)

    def get(self, request):
        # Internal control callers use Basic auth rather than a browser session, but
        # Django's standard CSRF middleware still protects mutating requests. Calling
        # get_token makes the normal csrftoken cookie available for the parent client.
        from django.middleware.csrf import get_token

        get_token(request)
        groups = _connection_groups()
        return JsonResponse([_record(key, values) for key, values in groups.items()], safe=False)

    def delete(self, request, connection_id=None):
        groups = _connection_groups()
        if connection_id is None:
            return JsonResponse({"revoked_count": _revoke_groups(groups)})
        for key, values in groups.items():
            if _connection_id(key) == connection_id:
                return JsonResponse({"revoked_count": _revoke_groups({key: values})})
        return _json_error("not_found", "Connection not found.", 404)


class ServiceMetadataView(View):
    def get(self, request):
        base = _public_base_url()
        return JsonResponse({
            "issuer": base,
            "authorization_endpoint": f"{base}/oauth/authorize",
            "token_endpoint": f"{base}/oauth/token",
            "registration_endpoint": f"{base}/oauth/register",
            "revocation_endpoint": f"{base}/oauth/revoke",
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "code_challenge_methods_supported": ["S256"],
            "token_endpoint_auth_methods_supported": ["none", "private_key_jwt"],
            "scopes_supported": ["cognita:access"],
            "client_id_metadata_document_supported": True,
            "authorization_response_iss_parameter_supported": True,
        })
