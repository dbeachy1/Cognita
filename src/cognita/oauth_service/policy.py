"""Cognita's exact one-connector RFC 8707 resource policy."""

from __future__ import annotations

import hashlib
import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml
from django.db import connection, transaction
from oauth2_provider.models import (
    get_access_token_model,
    get_refresh_token_model,
)
from oauth2_provider.oauth2_validators import OAuth2Validator
from oauthlib.oauth2.rfc6749.errors import CustomOAuth2Error

from cognita.config import CognitaConfig
from cognita.connectors import (
    PUBLIC_CONTRACT_VERSION,
    ConnectorDefinition,
    ConnectorPolicyError,
    ConnectorStore,
    EffectiveAccess,
    RouteResource,
    build_connector_url,
    is_supported_route,
    parse_route_resource,
    resolve_project_access,
)
from cognita.public_url import effective_public_base_url
from cognita.registry import Registry

log = logging.getLogger("cognita.oauth_service.policy")

_INVALID_TARGET = "Cognita requires exactly one enabled connector resource."
_UNAVAILABLE_TARGET = "The requested connector resource is unavailable."


def _invalid_target(description: str = _INVALID_TARGET) -> CustomOAuth2Error:
    """Return the stable OAuth error used for all invalid resource requests."""
    return CustomOAuth2Error(error="invalid_target", description=description)


@dataclass(frozen=True)
class ResourcePolicy:
    """Resolve canonical connector resources against current parent policy.

    Connector definitions are parent-owned. The OAuth child therefore keeps no
    authorization-time copy: every resource validation reads a bounded,
    atomically-written connector snapshot. Registry reloads are deliberately
    performed alongside that read so a malformed current policy fails closed
    rather than accidentally treating a selected connector as unrestricted.
    """

    config: CognitaConfig
    registry: Registry
    connector_store: ConnectorStore | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self.connector_store is None:
            object.__setattr__(
                self,
                "connector_store",
                ConnectorStore(
                    Path(self.config.connectors_path), persist_migrations=False
                ),
            )

    def _public_base_url(self) -> str:
        """Refresh the Admin-selected identity before validating a resource."""
        value = effective_public_base_url(self.config)
        self.config.public_base_url = value
        return value

    def resource_for(self, connector_id: str) -> str:
        """Return the canonical resource for the deployed public contract."""
        snapshot, _registry = self._current_snapshot()
        connector = next(
            (item for item in snapshot.connectors if item.id == connector_id), None
        )
        if connector is None:
            raise ConnectorPolicyError("connector not found")
        return build_connector_url(
            self._public_base_url(), connector.slug, PUBLIC_CONTRACT_VERSION
        )

    def _current_snapshot(self):
        """Load one complete connector policy and current project registry."""
        try:
            current_registry = Registry(Path(self.config.registry_path))
            snapshot = self.connector_store.snapshot(current_registry.projects)
        except (
            ConnectorPolicyError,
            OSError,
            TypeError,
            ValueError,
            UnicodeError,
            yaml.YAMLError,
        ) as exc:
            log.error("OAuth connector policy unavailable reason=%s", type(exc).__name__)
            raise _invalid_target(_UNAVAILABLE_TARGET) from exc
        return snapshot, current_registry

    def connector_resource_for(self, resource: str) -> RouteResource | None:
        """Return a supported canonical identity for an exact resource URI.

        Parsing alone is deliberately not enough here.  This helper is also
        used when enumerating OAuth connections, so treating a syntactically
        valid retired or future generation as an identity would expose stale
        connection records. The connector layer owns stable/current route
        publication; OAuth only consumes it and never performs version
        arithmetic locally.
        """
        try:
            parsed = parse_route_resource(
                resource, public_origin=self._public_base_url()
            )
        except (ConnectorPolicyError, TypeError, ValueError):
            return None
        if parsed is None or parsed.family != "combined" or not is_supported_route(parsed):
            return None
        return parsed

    def connector_id_for(self, resource: str) -> str | None:
        """Return a canonical connector UUID for an exact versioned URI."""
        connector = self.connector_for(resource)
        return connector.id if connector is not None else None

    @staticmethod
    def _connector_for_parsed(snapshot, parsed) -> ConnectorDefinition | None:
        return next(
            (item for item in snapshot.connectors if item.slug == parsed.slug),
            None,
        )

    @staticmethod
    def _published(parsed, connector: ConnectorDefinition) -> bool:
        return parsed.family == "combined" and is_supported_route(parsed)

    def connector_for(self, resource: str) -> ConnectorDefinition | None:
        """Return an enabled connector for a canonical resource, if present."""
        parsed = self.connector_resource_for(resource)
        if parsed is None:
            return None
        snapshot, _registry = self._current_snapshot()
        connector = self._connector_for_parsed(snapshot, parsed)
        if (
            connector is None
            or not connector.enabled
            or not self._published(parsed, connector)
        ):
            return None
        return connector

    def project_for(self, resource: str) -> None:
        """Retain the old connection-record shape without reviving project URLs."""
        return None

    def accessible_projects(self, resource: str) -> list[EffectiveAccess]:
        """Return the current enabled project access for a connector resource."""
        parsed = self.connector_resource_for(resource)
        if parsed is None:
            return []
        snapshot, current_registry = self._current_snapshot()
        connector = self._connector_for_parsed(snapshot, parsed)
        if (
            connector is None
            or not connector.enabled
            or not self._published(parsed, connector)
        ):
            return []
        return [
            EffectiveAccess(connector.id, project.name, access, snapshot.revision)
            for project in sorted(current_registry.projects, key=lambda item: item.name)
            if project.enabled
            and (access := resolve_project_access(connector, project)) is not None
        ]

    def connection_summary(self, resource: str) -> dict[str, Any] | None:
        """Describe one resource-bound authorization using current policy."""
        parsed = self.connector_resource_for(resource)
        if parsed is None:
            return None
        snapshot, current_registry = self._current_snapshot()
        connector = self._connector_for_parsed(snapshot, parsed)
        projects: list[dict[str, str]] = []
        if (
            connector is not None
            and connector.enabled
            and self._published(parsed, connector)
        ):
            projects = [
                {"name": project.name, "access": access}
                for project in sorted(current_registry.projects, key=lambda item: item.name)
                if project.enabled
                and (access := resolve_project_access(connector, project)) is not None
            ]
        if connector is None:
            return None
        return {
            "id": connector.id,
            "name": connector.name if connector is not None else None,
            "enabled": bool(connector is not None and connector.enabled),
            "revision": snapshot.revision,
            "projects": projects,
        }


    def validate(self, resources: Any) -> str:
        """Validate exactly one enabled connector resource and return it unchanged."""
        if isinstance(resources, str):
            values = [resources]
        elif isinstance(resources, (list, tuple)):
            values = list(resources)
        else:
            values = []
        if len(values) != 1 or not isinstance(values[0], str):
            log.info("OAuth connector resource denied reason=resource_count")
            raise _invalid_target()

        resource = values[0]
        parsed = self.connector_resource_for(resource)
        if parsed is None:
            log.info("OAuth connector resource denied reason=resource_shape")
            raise _invalid_target()
        snapshot, _registry = self._current_snapshot()
        connector = self._connector_for_parsed(snapshot, parsed)
        if (
            connector is None
            or not connector.enabled
            or not self._published(parsed, connector)
        ):
            log.info(
                "OAuth connector resource denied connector_id=%s reason=%s",
                parsed.slug,
                (
                    "not_found"
                    if connector is None
                    else "disabled"
                    if not connector.enabled
                    else "unpublished"
                ),
            )
            raise _invalid_target()
        log.info(
            "OAuth connector resource accepted connector_id=%s revision=%d",
            connector.id,
            snapshot.revision,
        )
        return resource

    @staticmethod
    def request_resources(request: Any) -> list[str]:
        """Recover all resource values from oauthlib's normalized request body."""
        values = getattr(request, "resource", None)
        if isinstance(values, str):
            values = [values]
        elif isinstance(values, (list, tuple)):
            values = list(values)
        else:
            values = []

        decoded = getattr(request, "decoded_body", None)
        if isinstance(decoded, dict) and "resource" in decoded:
            raw = decoded["resource"]
            if isinstance(raw, str):
                values = [raw]
            elif isinstance(raw, (list, tuple)):
                values = list(raw)
        elif isinstance(decoded, (list, tuple)):
            body_values = [
                entry[1]
                for entry in decoded
                if isinstance(entry, (list, tuple)) and len(entry) == 2 and entry[0] == "resource"
            ]
            if body_values:
                values = body_values
        return values

    def enforce(self, request: Any) -> str:
        """Validate and normalize a request's resource list in place."""
        resource = self.validate(self.request_resources(request))
        request.resource = [resource]
        return resource


_POLICY: ResourcePolicy | None = None


def set_policy(policy: ResourcePolicy) -> None:
    global _POLICY
    _POLICY = policy


def get_policy() -> ResourcePolicy:
    if _POLICY is None:
        raise RuntimeError("OAuth resource policy is not initialized")
    return _POLICY


class CognitaOAuth2Validator(OAuth2Validator):
    """DOT validator with Cognita's single-connector resource binding."""

    @staticmethod
    def _auth_correlation_id(request) -> str:
        """Own an opaque request-local ID without trusting inbound headers."""
        values = getattr(request, "validator_log", None)
        if not isinstance(values, dict):
            values = {}
            request.validator_log = values
        correlation = values.get("cognita_auth_correlation_id")
        if not isinstance(correlation, str):
            correlation = uuid.uuid4().hex
            values["cognita_auth_correlation_id"] = correlation
        return correlation

    @classmethod
    def _log_auth_rejection(
        cls,
        request,
        category: str,
        *,
        client_id: object = None,
    ) -> None:
        """Log at most one root rejection category for an OAuth request."""
        from .cimd_auth import log_auth_rejection

        values = getattr(request, "validator_log", None)
        if not isinstance(values, dict):
            values = {}
            request.validator_log = values
        if "cognita_auth_rejection" in values:
            return
        values["cognita_auth_rejection"] = category
        log_auth_rejection(
            category,
            correlation_id=cls._auth_correlation_id(request),
            client_id=client_id,
        )

    @staticmethod
    def _has_parameter(request, name: str) -> bool:
        decoded = getattr(request, "decoded_body", None)
        if isinstance(decoded, dict):
            return name in decoded
        return isinstance(decoded, (list, tuple)) and any(
            isinstance(item, (list, tuple)) and len(item) == 2 and item[0] == name
            for item in decoded
        )

    @classmethod
    def _parameter_value(cls, request, name: str):
        """Read a token parameter across oauthlib request representations.

        oauthlib normally projects form fields onto request attributes, but
        some Django/adaptor paths retain only ``decoded_body``. private_key_jwt
        clients may omit the redundant form ``client_id``; locating the
        assertion must not depend on that projection having run.
        """
        value = getattr(request, name, None)
        if value is not None:
            return value
        decoded = getattr(request, "decoded_body", None)
        if isinstance(decoded, dict):
            return decoded.get(name)
        if isinstance(decoded, (list, tuple)):
            for item in decoded:
                if isinstance(item, (list, tuple)) and len(item) == 2 and item[0] == name:
                    return item[1]
        return None

    @classmethod
    def _uses_assertion_auth(cls, request) -> bool:
        return cls._has_parameter(request, "client_assertion") or cls._has_parameter(
            request, "client_assertion_type"
        )

    @classmethod
    def _private_client(cls, request):
        """Resolve asymmetric CIMD metadata without weakening public clients."""
        from django.utils import timezone
        from oauth2_provider import cimd as dot_cimd
        from oauth2_provider.models import get_application_model

        from .cimd_auth import (
            AssertionRejected,
            assertion_client_id_hint,
            metadata_for_application,
            resolve_private_application,
        )

        client_id = getattr(request, "client_id", None)
        if not client_id and cls._has_parameter(request, "client_assertion"):
            try:
                client_id = assertion_client_id_hint(
                    cls._parameter_value(request, "client_assertion")
                )
            except AssertionRejected as exc:
                cls._log_auth_rejection(request, exc.category)
                return None
            # RFC 7523 private_key_jwt identifies the client through the
            # assertion issuer. oauthlib expects request.client_id earlier in
            # authorization-code validation, so supply the bounded hint here.
            # Full signature and iss/sub equality validation still occurs in
            # authenticate_client before the code is consumed.
            request.client_id = client_id
        if isinstance(client_id, str) and dot_cimd.is_cimd_client_id(client_id):
            existing = getattr(request, "client", None)
            if existing is None:
                existing = get_application_model().objects.filter(client_id=client_id).first()
            # Stable public CIMD rows remain DOT-owned. Only revisit one when
            # its normal metadata freshness window has elapsed, allowing a
            # metadata document to opt into private_key_jwt without fetching
            # on every public-client request.
            if (
                existing is not None
                and existing.client_type != existing.CLIENT_CONFIDENTIAL
                and existing.cimd_expires_at is not None
                and timezone.now() <= existing.cimd_expires_at
            ):
                return None
            try:
                application = resolve_private_application(client_id)
            except Exception:  # noqa: BLE001 - pre-auth resolution must fail closed
                cls._log_auth_rejection(
                    request,
                    "metadata_resolution",
                    client_id=client_id,
                )
                application = None
            if application is not None:
                request.client = application
                return application
        application = getattr(request, "client", None)
        try:
            return application if metadata_for_application(application) is not None else None
        except Exception:  # noqa: BLE001 - stale metadata must never downgrade auth
            # A persisted asymmetric row must never fall back to DOT's public
            # CIMD path when its metadata/JWKS identity cannot be refreshed.
            if application is not None and getattr(application, "client_type", None) == application.CLIENT_CONFIDENTIAL:
                cls._log_auth_rejection(
                    request,
                    "metadata_resolution",
                    client_id=getattr(application, "client_id", None),
                )
            return None

    def _load_application(self, client_id, request):
        private = self._private_client(request)
        if private is not None and private.client_id == client_id:
            return private
        # A previously accepted asymmetric row must never be handed back to
        # DOT's resolver, whose public-only refresh path could downgrade it if
        # its metadata changed or became unavailable.
        from oauth2_provider.models import get_application_model

        application = get_application_model().objects.filter(client_id=client_id).first()
        if (
            application is not None
            and application.registration_source == application.RegistrationSource.CIMD
            and application.client_type == application.CLIENT_CONFIDENTIAL
        ):
            return None
        return super()._load_application(client_id, request)

    def client_authentication_required(self, request, *args, **kwargs):
        if self._private_client(request) is not None:
            return True
        # An assertion-shaped request must never be silently treated as a
        # public ``none`` client when metadata resolution or method selection
        # fails. Route it through authenticate_client for a fail-closed result
        # and a stable diagnostic category.
        if self._uses_assertion_auth(request):
            return True
        return super().client_authentication_required(request, *args, **kwargs)

    def authenticate_client(self, request, *args, **kwargs):
        from .cimd_auth import metadata_for_application, validate_client_assertion

        correlation = self._auth_correlation_id(request)
        private = self._private_client(request)
        if private is not None:
            # private_key_jwt is the sole accepted method for this row. Basic
            # auth and client_secret parameters are explicit downgrade attempts.
            headers = getattr(request, "headers", {}) or {}
            if headers.get("HTTP_AUTHORIZATION") or headers.get("Authorization"):
                self._log_auth_rejection(
                    request,
                    "auth_method_mismatch",
                    client_id=private.client_id,
                )
                return False
            if self._has_parameter(request, "client_secret"):
                self._log_auth_rejection(
                    request,
                    "auth_method_mismatch",
                    client_id=private.client_id,
                )
                return False
            assertion_type = self._parameter_value(request, "client_assertion_type")
            assertion = self._parameter_value(request, "client_assertion")
            if assertion_type is None:
                self._log_auth_rejection(
                    request,
                    "assertion_type_missing",
                    client_id=private.client_id,
                )
                return False
            if assertion_type != "urn:ietf:params:oauth:client-assertion-type:jwt-bearer":
                self._log_auth_rejection(
                    request,
                    "assertion_type_invalid",
                    client_id=private.client_id,
                )
                return False
            if not isinstance(assertion, str) or not assertion:
                self._log_auth_rejection(
                    request,
                    "missing_assertion",
                    client_id=private.client_id,
                )
                return False
            from cognita.public_url import effective_public_base_url

            from .views import context

            endpoint = effective_public_base_url(context().config).rstrip("/") + "/oauth/token"
            try:
                auth_metadata = metadata_for_application(private)
            except Exception:  # noqa: BLE001 - resolution details stay private
                auth_metadata = None
            if auth_metadata is None:
                self._log_auth_rejection(
                    request,
                    "metadata_resolution",
                    client_id=private.client_id,
                )
                return False
            return validate_client_assertion(
                auth_metadata,
                assertion,
                endpoint,
                correlation_id=correlation,
            )
        if self._uses_assertion_auth(request):
            self._log_auth_rejection(
                request,
                "auth_method_mismatch",
                client_id=getattr(request, "client_id", None),
            )
            return False
        return super().authenticate_client(request, *args, **kwargs)

    def _check_and_set_request_resource(self, request):
        # DOT first applies its authorization-code/refresh-token narrowing rules;
        # Cognita then requires the resulting resource to remain exactly one
        # currently enabled connector resource.
        result = super()._check_and_set_request_resource(request)
        get_policy().enforce(request)
        return result

    def _create_authorization_code(self, request, code, expires=None):
        # This is the last hook before DOT persists Grant.resource. Enforcing
        # here makes disabled/deleted connectors fail before a code is created.
        get_policy().enforce(request)
        with transaction.atomic():
            grant = super()._create_authorization_code(request, code, expires)
            resources = getattr(request, "resource", None) or []
            if len(resources) != 1 or not isinstance(resources[0], str):
                raise _invalid_target()
            principal_id = str(uuid.uuid4())
            now = datetime.now(UTC).isoformat(timespec="seconds")
            with connection.cursor() as cursor:
                cursor.execute(
                    "INSERT INTO oauth_principal"
                    "(principal_id,user_id,application_id,exact_resource,created_at,revoked_at,migration_key) "
                    "VALUES(%s,%s,%s,%s,%s,NULL,NULL)",
                    [principal_id, str(request.user.pk), str(request.client.pk), resources[0], now],
                )
                cursor.execute(
                    "INSERT INTO oauth_token_binding"
                    "(principal_id,token_kind,token_primary_key,created_at) VALUES(%s,%s,%s,%s)",
                    [principal_id, "authorization_code", str(grant.pk), now],
                )
            return grant

    @staticmethod
    def _principal_for_binding(token_kind: str, token_primary_key: str) -> str | None:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT p.principal_id FROM oauth_principal p "
                "JOIN oauth_token_binding b ON b.principal_id=p.principal_id "
                "WHERE b.token_kind=%s AND b.token_primary_key=%s "
                "AND p.revoked_at IS NULL",
                [token_kind, str(token_primary_key)],
            )
            row = cursor.fetchone()
        return None if row is None else str(row[0])

    @staticmethod
    def _bind_principal(principal_id: str, bindings: list[tuple[str, str]]) -> None:
        now = datetime.now(UTC).isoformat(timespec="seconds")
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT revoked_at FROM oauth_principal WHERE principal_id=%s",
                [principal_id],
            )
            principal = cursor.fetchone()
            if principal is None or principal[0] is not None:
                raise CustomOAuth2Error(
                    error="invalid_grant",
                    description="The authorization principal is unavailable.",
                )
            for token_kind, token_key in bindings:
                cursor.execute(
                    "SELECT principal_id FROM oauth_token_binding "
                    "WHERE token_kind=%s AND token_primary_key=%s",
                    [token_kind, str(token_key)],
                )
                existing = cursor.fetchone()
                if existing is not None and str(existing[0]) != principal_id:
                    raise CustomOAuth2Error(
                        error="invalid_grant",
                        description="The token binding conflicts with another principal.",
                    )
                if existing is None:
                    cursor.execute(
                        "INSERT INTO oauth_token_binding"
                        "(principal_id,token_kind,token_primary_key,created_at) "
                        "VALUES(%s,%s,%s,%s)",
                        [principal_id, token_kind, str(token_key), now],
                    )

    def _save_bearer_token(self, token, request, *args, **kwargs):
        """Persist DOT tokens and durable principal bindings in one transaction."""
        if request.grant_type == "authorization_code":
            from oauth2_provider.models import get_grant_model

            grant = get_grant_model().objects.filter(
                code=request.code, application=request.client
            ).first()
            principal_id = (
                None if grant is None
                else self._principal_for_binding("authorization_code", str(grant.pk))
            )
        elif request.grant_type == "refresh_token":
            refresh = getattr(request, "refresh_token_instance", None)
            principal_id = (
                None if refresh is None
                else self._principal_for_binding("refresh", str(refresh.pk))
            )
        else:
            principal_id = None
        if principal_id is None:
            raise CustomOAuth2Error(
                error="invalid_grant",
                description="The authorization grant is not bound to a durable principal.",
            )

        result = super()._save_bearer_token(token, request, *args, **kwargs)
        access_raw = token.get("access_token")
        access = None
        if isinstance(access_raw, str):
            checksum = hashlib.sha256(access_raw.encode("utf-8")).hexdigest()
            access = get_access_token_model().objects.filter(token_checksum=checksum).first()
            if access is None:
                access = get_access_token_model().objects.filter(token=access_raw).first()
        bindings: list[tuple[str, str]] = []
        if access is not None:
            bindings.append(("access", str(access.pk)))
        refresh_raw = token.get("refresh_token")
        if isinstance(refresh_raw, str):
            checksum = hashlib.sha256(refresh_raw.encode("utf-8")).hexdigest()
            refresh = get_refresh_token_model().objects.filter(token_checksum=checksum).first()
            if refresh is None:
                refresh = get_refresh_token_model().objects.filter(token=refresh_raw).first()
            if refresh is not None:
                bindings.append(("refresh", str(refresh.pk)))
        if not bindings:
            raise CustomOAuth2Error(
                error="server_error",
                description="The issued token could not be bound to its principal.",
            )
        self._bind_principal(principal_id, bindings)
        return result
