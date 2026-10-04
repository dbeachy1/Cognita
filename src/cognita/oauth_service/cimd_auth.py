"""Cognita-owned CIMD ``private_key_jwt`` support.

django-oauth-toolkit's CIMD resolver intentionally only creates public
applications.  OpenAI's client metadata document is an asymmetric client,
so this module adds the narrow extension without changing DOT's resolver or
its public-client/DCR behavior.
"""

from __future__ import annotations

import base64
import json
import logging
import math
import secrets
import ssl
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from datetime import timedelta
from urllib.parse import urlsplit

import urllib3
from django.core.exceptions import ValidationError
from django.db import IntegrityError
from django.utils import timezone
from django.utils.module_loading import import_string
from jwcrypto import jwk, jws
from oauth2_provider import cimd as dot_cimd
from oauth2_provider.models import get_application_model
from oauth2_provider.settings import oauth2_settings

log = logging.getLogger("cognita.oauth_service.cimd_auth")

CLIENT_ASSERTION_TYPE = "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"
ALLOWED_ALGORITHMS = frozenset({"RS256"})
MAX_CLOCK_SKEW_SECONDS = 300
MAX_ASSERTION_LIFETIME_SECONDS = 600
MIN_JWKS_AGE_SECONDS = 60
MAX_JWKS_AGE_SECONDS = 3600
MAX_JWKS_KEYS = 20
MAX_REPLAY_ENTRIES = 4096

# Claude advertises RFC 7523's JWT bearer grant alongside the authorization
# code and refresh grants in its public CIMD document.  Cognita does not issue
# that grant, but it is an auxiliary token grant and does not change the DOT
# Application authorization grant represented by the row.  Keep this list
# deliberately narrow: accepting arbitrary additional grant names would hide a
# client/server protocol mismatch and could store an application whose actual
# capabilities are broader than Cognita's token endpoint.
PUBLIC_AUXILIARY_GRANT_TYPES = frozenset({
    "urn:ietf:params:oauth:grant-type:jwt-bearer",
})


class AssertionRejected(ValueError):
    """A client assertion failed one stable, nonsecret validation category."""

    def __init__(self, category: str):
        super().__init__(category)
        self.category = category


class JWKSFetchError(ValueError):
    """The bounded JWKS transport could not return a document."""


class JWKSValidationError(ValueError):
    """The returned JWKS document or key material was invalid."""


@dataclass(frozen=True)
class ClientAuthMetadata:
    client_id: str
    jwks_uri: str
    algorithms: frozenset[str]
    expires_at: float


@dataclass(frozen=True)
class _CachedJWKS:
    keys: tuple[dict, ...]
    expires_at: float


_lock = threading.RLock()
_metadata: OrderedDict[str, ClientAuthMetadata] = OrderedDict()
_jwks: OrderedDict[str, _CachedJWKS] = OrderedDict()
_replays: OrderedDict[tuple[str, str], float] = OrderedDict()
_MAX_METADATA_ENTRIES = 256
_MAX_JWKS_ENTRIES = 128


def _setting(name: str, default):
    # Read the backing mapping first so a runtime reconfiguration (the parent
    # may update public-origin/OAuth settings while the child remains alive)
    # and deterministic test transports are observed despite DOT's lazy cache.
    value = getattr(oauth2_settings, "user_settings", {}).get(name, default)
    if value is default:
        value = getattr(oauth2_settings, name, default)
    if isinstance(value, str):
        try:
            return import_string(value)
        except (ImportError, AttributeError, ValueError):
            return default
    return value


def _allowed_hosts() -> set[str]:
    user_settings = getattr(oauth2_settings, "user_settings", {})
    if "CIMD_ALLOWED_HOSTS" in user_settings:
        configured = user_settings["CIMD_ALLOWED_HOSTS"]
    else:
        # Keep the lazy DOT setting uncached until the backing mapping has no
        # explicit value.  Evaluating it as a ``dict.get`` default would cache
        # an earlier host list and make runtime/test reconfiguration stale.
        configured = getattr(oauth2_settings, "CIMD_ALLOWED_HOSTS", ())
    configured = configured or ()
    # Cognita's config validator intentionally permits exact hostnames only.
    return {str(host).lower() for host in configured if isinstance(host, str)}


def _exact_allowed_https_uri(uri: object, *, allowed_hosts: set[str] | None = None) -> bool:
    if not isinstance(uri, str) or len(uri) > 2048:
        return False
    try:
        parsed = urlsplit(uri)
        host = parsed.hostname
        port = parsed.port
    except (TypeError, ValueError):
        return False
    hosts = _allowed_hosts() if allowed_hosts is None else allowed_hosts
    return bool(
        parsed.scheme.lower() == "https"
        and host
        and hosts
        and host.lower() in hosts
        and parsed.username is None
        and parsed.password is None
        and parsed.fragment == ""
        and parsed.path
        and (port is None or 1 <= port <= 65535)
    )


def _safe_client_host(client_id: object) -> str:
    """Return only a bounded hostname for authentication diagnostics."""
    if not isinstance(client_id, str):
        return "unknown"
    try:
        host = urlsplit(client_id).hostname
    except (TypeError, ValueError):
        return "unknown"
    if not host or len(host) > 253:
        return "unknown"
    return host.lower()


def _safe_algorithm(value: object) -> str:
    """Never reflect an attacker-controlled JOSE value into logs."""
    return value if value in ALLOWED_ALGORITHMS else "unsupported"


def log_auth_rejection(
    category: str,
    *,
    correlation_id: str | None = None,
    client_id: object = None,
    algorithm: object = None,
) -> None:
    """Emit one stable authentication failure without credential material."""
    correlation = correlation_id or secrets.token_hex(8)
    log.info(
        "OAuth client authentication rejected category=%s correlation_id=%s "
        "client_host=%s alg=%s",
        category,
        correlation,
        _safe_client_host(client_id),
        _safe_algorithm(algorithm),
    )


def _bounded_age(value: object) -> int:
    try:
        age = int(value)
    except (TypeError, ValueError):
        age = MAX_JWKS_AGE_SECONDS
    return max(MIN_JWKS_AGE_SECONDS, min(age, MAX_JWKS_AGE_SECONDS))


class SafeJWKSFetcher:
    """Fetch a JWKS with the same bounded SSRF defenses as DOT CIMD."""

    def fetch(self, uri: str) -> tuple[dict, int]:
        if not _exact_allowed_https_uri(uri):
            raise ValueError("JWKS URI is not an allowed HTTPS endpoint")
        parsed = urlsplit(uri)
        port = parsed.port or 443
        try:
            # Reuse DOT's maintained DNS/private-address checks, including
            # mapped, 6to4, Teredo, and NAT64 IPv6 handling.
            addresses = dot_cimd._resolve_and_validate(parsed.hostname, port)
        except (OSError, ValueError) as exc:
            raise ValueError("JWKS host could not be resolved safely") from exc

        timeout = float(getattr(oauth2_settings, "CIMD_FETCH_TIMEOUT_SECONDS", 5))
        deadline = time.monotonic() + timeout
        path = parsed.path + (("?" + parsed.query) if parsed.query else "")
        last_error = None
        for address in addresses:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            pool = urllib3.HTTPSConnectionPool(
                host=address,
                port=port,
                timeout=urllib3.Timeout(connect=remaining, read=remaining, total=remaining),
                retries=False,
                maxsize=1,
                ssl_context=ssl.create_default_context(),
                server_hostname=parsed.hostname,
            )
            try:
                response = pool.urlopen(
                    "GET",
                    path,
                    headers={"Host": parsed.netloc, "Accept": "application/json"},
                    redirect=False,
                    preload_content=False,
                )
                try:
                    if response.status != 200:
                        raise ValueError("JWKS endpoint returned an unexpected status")
                    media_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
                    if media_type != "application/json" and not (
                        media_type.startswith("application/") and media_type.endswith("+json")
                    ):
                        raise ValueError("JWKS endpoint did not return JSON")
                    max_size = int(getattr(oauth2_settings, "CIMD_MAX_DOCUMENT_SIZE", 16 * 1024))
                    body = response.read(max_size + 1)
                    if len(body) > max_size:
                        raise ValueError("JWKS document exceeds the maximum size")
                    data = json.loads(body)
                    if not isinstance(data, dict):
                        raise TypeError("JWKS document is not an object")
                    return data, _cache_control_age(response.headers.get("Cache-Control"))
                finally:
                    response.release_conn()
            except (urllib3.exceptions.HTTPError, ValueError, json.JSONDecodeError) as exc:
                last_error = exc
            finally:
                pool.close()
        raise ValueError("JWKS fetch failed") from last_error


def _cache_control_age(value: object) -> int:
    if not isinstance(value, str):
        return MAX_JWKS_AGE_SECONDS
    import re

    match = re.search(r"max-age\s*=\s*(\d+)", value, re.IGNORECASE)
    if not match or "no-cache" in value.lower() or "no-store" in value.lower():
        return MIN_JWKS_AGE_SECONDS
    return _bounded_age(match.group(1))


def _remember_metadata(record: ClientAuthMetadata) -> None:
    with _lock:
        _metadata[record.client_id] = record
        _metadata.move_to_end(record.client_id)
        while len(_metadata) > _MAX_METADATA_ENTRIES:
            _metadata.popitem(last=False)


def _metadata_record(client_id: str) -> ClientAuthMetadata | None:
    now = time.monotonic()
    with _lock:
        record = _metadata.get(client_id)
        if record is not None and record.expires_at > now:
            _metadata.move_to_end(client_id)
            return record
        if record is not None:
            _metadata.pop(client_id, None)
    return None


def _fetch_metadata(client_id: str) -> tuple[dict, int]:
    if not dot_cimd._registration_permitted(None, client_id):
        raise ValueError("CIMD host is not permitted")
    if not dot_cimd._validate_client_id_url(client_id):
        raise ValueError("CIMD client_id URL is invalid")
    fetcher = _setting("CIMD_METADATA_FETCHER", dot_cimd.SafeMetadataFetcher)
    data, age = fetcher().fetch(client_id)
    if not isinstance(data, dict) or data.get("client_id") != client_id:
        raise ValueError("CIMD metadata client_id mismatch")
    return data, int(age)


def _public_application_kwargs(metadata: dict) -> dict:
    """Build public CIMD fields while preserving one Cognita protocol exception.

    DOT's public CIMD builder requires exactly one non-refresh grant.  A public
    client may advertise RFC 7523's JWT bearer grant as an auxiliary capability
    while still using Cognita's authorization-code flow.  Remove that one
    bounded auxiliary value before delegating the remaining metadata checks to
    DOT; every other unsupported grant continues to fail closed.
    """
    grant_types = metadata.get("grant_types", ["authorization_code"])
    if not isinstance(grant_types, list) or not all(isinstance(value, str) for value in grant_types):
        raise ValueError("CIMD grant_types must be an array of strings")
    unsupported_auxiliary = {
        value
        for value in grant_types
        if value not in {"authorization_code", "refresh_token"}
        and value not in PUBLIC_AUXILIARY_GRANT_TYPES
    }
    if unsupported_auxiliary:
        raise ValueError("CIMD metadata contains an unsupported grant type")
    normalized = dict(metadata)
    normalized["grant_types"] = [
        value for value in grant_types if value not in PUBLIC_AUXILIARY_GRANT_TYPES
    ]
    try:
        return dot_cimd._build_application_kwargs(normalized)
    except dot_cimd.CIMDError as exc:
        raise ValueError("CIMD public metadata is invalid") from exc


def _private_metadata(client_id: str, metadata: dict) -> ClientAuthMetadata | None:
    if metadata.get("token_endpoint_auth_method", "none") != "private_key_jwt":
        return None
    jwks_uri = metadata.get("jwks_uri")
    if not _exact_allowed_https_uri(jwks_uri):
        raise ValueError("CIMD private_key_jwt metadata has an invalid jwks_uri")
    # RFC 7591 client metadata uses the singular
    # ``token_endpoint_auth_signing_alg`` member.  The similarly named
    # ``*_values_supported`` member belongs to authorization-server metadata,
    # not a client metadata document.  ChatGPT's production CIMD document uses
    # the standard singular form.
    declared = metadata.get("token_endpoint_auth_signing_alg")
    if not isinstance(declared, str) or not declared:
        raise ValueError("CIMD signing algorithm is invalid")
    algorithms = frozenset({declared}) & ALLOWED_ALGORITHMS
    if not algorithms:
        raise ValueError("CIMD metadata does not permit an allowed signing algorithm")
    return ClientAuthMetadata(client_id, jwks_uri, algorithms, 0.0)


def _application_from_metadata(client_id: str, metadata: dict, age: int):
    """Create/update the DOT row while retaining private auth metadata in cache."""
    auth = _private_metadata(client_id, metadata)
    if auth is None:
        return None
    # Reuse DOT's complete redirect/grant validation, but do not let its
    # public-client auth-method restriction reject this asymmetric document.
    public_metadata = dict(metadata)
    public_metadata["token_endpoint_auth_method"] = "none"
    kwargs = dot_cimd._build_application_kwargs(public_metadata)
    Application = get_application_model()
    try:
        application = Application.objects.get(client_id=client_id)
        if application.registration_source != Application.RegistrationSource.CIMD:
            raise ValueError("CIMD client_id collides with a non-CIMD application")
    except Application.DoesNotExist:
        application = Application(client_id=client_id, client_secret="")
    application.user = None
    application.client_type = Application.CLIENT_CONFIDENTIAL
    application.registration_source = Application.RegistrationSource.CIMD
    application.cimd_expires_at = timezone.now() + timedelta(seconds=max(60, age))
    for field, value in kwargs.items():
        setattr(application, field, value)
    try:
        application.full_clean(exclude=["client_secret"], validate_unique=False)
    except ValidationError as exc:
        raise ValueError("CIMD private client could not be stored") from exc
    try:
        application.save()
    except IntegrityError as exc:
        # Concurrent first sight of the same URL: use the winner, but retain
        # the non-CIMD collision guard before accepting its representation.
        winner = Application.objects.filter(client_id=client_id).first()
        if (
            winner is None
            or winner.registration_source != Application.RegistrationSource.CIMD
            or winner.client_type != Application.CLIENT_CONFIDENTIAL
        ):
            raise ValueError("CIMD private client could not be stored") from exc
        application = winner
    _remember_metadata(
        ClientAuthMetadata(client_id, auth.jwks_uri, auth.algorithms, time.monotonic() + max(60, age))
    )
    return application


def _application_from_public_metadata(client_id: str, metadata: dict, age: int):
    """Create or update a public DOT row from Cognita-validated CIMD metadata."""
    kwargs = _public_application_kwargs(metadata)
    Application = get_application_model()
    try:
        application = Application.objects.get(client_id=client_id)
        if application.registration_source != Application.RegistrationSource.CIMD:
            raise ValueError("CIMD client_id collides with a non-CIMD application")
    except Application.DoesNotExist:
        application = Application(client_id=client_id)
    application.user = None
    application.client_type = Application.CLIENT_PUBLIC
    application.registration_source = Application.RegistrationSource.CIMD
    application.cimd_expires_at = timezone.now() + timedelta(seconds=max(60, age))
    for field, value in kwargs.items():
        setattr(application, field, value)
    try:
        application.full_clean(exclude=["client_secret"], validate_unique=False)
    except ValidationError as exc:
        raise ValueError("CIMD public client could not be stored") from exc
    try:
        application.save()
    except IntegrityError as exc:
        winner = Application.objects.filter(client_id=client_id).first()
        if (
            winner is None
            or winner.registration_source != Application.RegistrationSource.CIMD
            or winner.client_type != Application.CLIENT_PUBLIC
        ):
            raise ValueError("CIMD public client could not be stored") from exc
        application = winner
    return application


def _resolve_cimd_application(client_id: str):
    """Resolve either public or asymmetric Cognita CIMD metadata."""
    if not isinstance(client_id, str) or not dot_cimd.is_cimd_client_id(client_id):
        return None
    metadata, age = _fetch_metadata(client_id)
    if _private_metadata(client_id, metadata) is not None:
        return _application_from_metadata(client_id, metadata, age)
    return _application_from_public_metadata(client_id, metadata, age)


def resolve_private_application(client_id: object):
    """Resolve only asymmetric CIMD clients; ``None`` delegates to DOT."""
    if not isinstance(client_id, str) or not dot_cimd.is_cimd_client_id(client_id):
        return None
    Application = get_application_model()
    existing = Application.objects.filter(client_id=client_id).first()
    if existing is not None:
        cached = _metadata_record(client_id)
        if cached is not None:
            return existing if existing.client_type == Application.CLIENT_CONFIDENTIAL else None
    application = _resolve_cimd_application(client_id)
    return (
        application
        if application is not None and application.client_type == Application.CLIENT_CONFIDENTIAL
        else None
    )


def metadata_for_application(application) -> ClientAuthMetadata | None:
    if (
        application is None
        or application.registration_source != application.RegistrationSource.CIMD
        or application.client_type != application.CLIENT_CONFIDENTIAL
    ):
        return None
    record = _metadata_record(application.client_id)
    if record is not None:
        return record
    # A process restart loses the bounded in-memory cache. Re-resolve the
    # document before every subsequent assertion rather than guessing from the
    # DOT row or silently downgrading the client to public authentication.
    metadata, age = _fetch_metadata(application.client_id)
    refreshed = _application_from_metadata(application.client_id, metadata, age)
    return None if refreshed is None else _metadata_record(application.client_id)


def _jwks_for(record: ClientAuthMetadata, *, force: bool = False) -> tuple[dict, ...]:
    now = time.monotonic()
    with _lock:
        cached = _jwks.get(record.jwks_uri)
        if cached is not None and not force and cached.expires_at > now:
            _jwks.move_to_end(record.jwks_uri)
            return cached.keys
    fetcher = _setting("CIMD_JWKS_FETCHER", SafeJWKSFetcher)
    # Share DOT's non-blocking in-flight cap. A key-rotation storm must not
    # turn assertion validation into an unbounded outbound work queue.
    with dot_cimd._fetch_slot() as acquired:
        if not acquired:
            raise JWKSFetchError("JWKS fetch capacity reached")
        try:
            document, age = fetcher().fetch(record.jwks_uri)
        except Exception as exc:
            raise JWKSFetchError("JWKS fetch failed") from exc
    try:
        values = document.get("keys") if isinstance(document, dict) else None
        if not isinstance(values, list) or not 1 <= len(values) <= MAX_JWKS_KEYS:
            raise ValueError("JWKS keys are invalid")
        keys: list[dict] = []
        seen: set[str] = set()
        for value in values:
            if not isinstance(value, dict) or value.get("kty") != "RSA" or "d" in value:
                raise ValueError("JWKS contains an unsupported key")
            kid = value.get("kid")
            if not isinstance(kid, str) or not kid or kid in seen:
                raise ValueError("JWKS key IDs are invalid")
            if (
                value.get("use", "sig") != "sig"
                or value.get("alg", "RS256") not in ALLOWED_ALGORITHMS
            ):
                raise ValueError("JWKS key algorithm is invalid")
            # Parse now so malformed RSA material never reaches assertion handling.
            jwk.JWK.from_json(json.dumps(value, separators=(",", ":")))
            seen.add(kid)
            keys.append(value)
    except Exception as exc:
        raise JWKSValidationError("JWKS document is invalid") from exc
    cached = _CachedJWKS(tuple(keys), now + _bounded_age(age))
    with _lock:
        _jwks[record.jwks_uri] = cached
        _jwks.move_to_end(record.jwks_uri)
        while len(_jwks) > _MAX_JWKS_ENTRIES:
            _jwks.popitem(last=False)
    return cached.keys


def _b64_json(value: str) -> dict:
    if not isinstance(value, str) or len(value) > 4096:
        raise ValueError("JWT segment is invalid")
    raw = base64.b64decode(
        value + "=" * (-len(value) % 4), altchars=b"-_", validate=True
    )
    result = json.loads(raw)
    if not isinstance(result, dict):
        raise TypeError("JWT JSON is invalid")
    return result


def _claim_number(claims: dict, name: str) -> float:
    value = claims.get(name)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError("JWT time claim is invalid")
    return float(value)


def _claim_audience(claims: dict, endpoint: str) -> bool:
    audience = claims.get("aud")
    if isinstance(audience, str):
        return audience == endpoint
    return isinstance(audience, list) and endpoint in audience and all(isinstance(item, str) for item in audience)


def assertion_client_id_hint(assertion: object) -> str:
    """Read the unverified issuer solely to locate the client metadata record.

    RFC 7523 does not require a duplicate ``client_id`` token-request
    parameter. The returned issuer is never trusted as authentication: the
    signature and exact issuer/subject claims are verified later against the
    fetched CIMD record.
    """
    if not isinstance(assertion, str) or len(assertion) > 16 * 1024:
        raise AssertionRejected("malformed_jwt")
    parts = assertion.split(".")
    if len(parts) != 3:
        raise AssertionRejected("malformed_jwt")
    try:
        claims = _b64_json(parts[1])
    except Exception as exc:
        raise AssertionRejected("malformed_jwt") from exc
    issuer = claims.get("iss")
    if not isinstance(issuer, str) or not issuer or len(issuer) > 2048:
        raise AssertionRejected("issuer_mismatch")
    return issuer


def _remember_replay(client_id: str, jti: str, expires_at: float) -> str | None:
    now = time.time()
    with _lock:
        stale = [key for key, expiry in _replays.items() if expiry <= now]
        for key in stale:
            _replays.pop(key, None)
        key = (client_id, jti)
        if key in _replays:
            return "replay_detected"
        # Never evict an unexpired assertion to make room: doing so would let a
        # captured assertion become usable again under load.  Capacity
        # exhaustion therefore fails closed for the new assertion.
        if len(_replays) >= MAX_REPLAY_ENTRIES:
            return "replay_capacity"
        _replays[key] = expires_at
        _replays.move_to_end(key)
    return None


def validate_client_assertion(
    record: ClientAuthMetadata,
    assertion: object,
    token_endpoint: str,
    *,
    correlation_id: str | None = None,
) -> bool:
    """Validate one RFC 7523 client assertion, returning no sensitive detail."""
    algorithm: object = None

    def reject(category: str) -> bool:
        log_auth_rejection(
            category,
            correlation_id=correlation_id,
            client_id=record.client_id,
            algorithm=algorithm,
        )
        return False

    try:
        if not isinstance(assertion, str) or len(assertion) > 16 * 1024:
            return reject("malformed_jwt")
        parts = assertion.split(".")
        if len(parts) != 3:
            return reject("malformed_jwt")
        try:
            header = _b64_json(parts[0])
        except Exception:  # noqa: BLE001 - stable category, no JWT reflection
            return reject("malformed_jwt")
        algorithm = header.get("alg")
        if algorithm not in record.algorithms or algorithm not in ALLOWED_ALGORITHMS:
            return reject("unsupported_algorithm")
        kid = header.get("kid")
        if not isinstance(kid, str) or not kid:
            return reject("missing_kid")
        try:
            keys = _jwks_for(record)
        except JWKSFetchError:
            return reject("jwks_fetch_failed")
        except JWKSValidationError:
            return reject("jwks_invalid")
        matching = next((item for item in keys if item.get("kid") == kid), None)
        if matching is None:
            # A key rotation is allowed one bounded refresh per assertion.
            try:
                keys = _jwks_for(record, force=True)
            except JWKSFetchError:
                return reject("jwks_fetch_failed")
            except JWKSValidationError:
                return reject("jwks_invalid")
            matching = next((item for item in keys if item.get("kid") == kid), None)
        if matching is None:
            return reject("unknown_kid")
        try:
            verifier = jws.JWS()
            verifier.deserialize(assertion)
        except Exception:  # noqa: BLE001 - malformed compact JWS
            return reject("malformed_jwt")
        try:
            verifier.verify(jwk.JWK.from_json(json.dumps(matching, separators=(",", ":"))))
        except Exception:  # noqa: BLE001 - signature details are secret-adjacent
            return reject("signature_invalid")
        try:
            claims = _b64_json(parts[1])
        except Exception:  # noqa: BLE001 - stable category, no JWT reflection
            return reject("malformed_jwt")
        if claims.get("iss") != record.client_id:
            return reject("issuer_mismatch")
        if claims.get("sub") != record.client_id:
            return reject("subject_mismatch")
        if not _claim_audience(claims, token_endpoint):
            return reject("audience_mismatch")
        now = time.time()
        try:
            issued = _claim_number(claims, "iat")
        except (TypeError, ValueError):
            return reject("iat_invalid")
        if not math.isfinite(issued) or issued < now - MAX_CLOCK_SKEW_SECONDS:
            return reject("iat_invalid")
        if issued > now + MAX_CLOCK_SKEW_SECONDS:
            return reject("not_yet_valid")
        try:
            expires = _claim_number(claims, "exp")
        except (TypeError, ValueError):
            return reject("expiration_invalid")
        if not math.isfinite(expires):
            return reject("expiration_invalid")
        if expires <= now - MAX_CLOCK_SKEW_SECONDS:
            return reject("expired")
        if expires <= issued or expires - issued > MAX_ASSERTION_LIFETIME_SECONDS:
            return reject("lifetime_invalid")
        jti = claims.get("jti")
        if not isinstance(jti, str) or not (1 <= len(jti) <= 256):
            return reject("jti_invalid")
        replay_category = _remember_replay(
            record.client_id, jti, expires + MAX_CLOCK_SKEW_SECONDS
        )
        return True if replay_category is None else reject(replay_category)
    except Exception:  # noqa: BLE001 - fail closed without sensitive exception detail
        return reject("validation_internal_error")


def clear_caches_for_tests() -> None:
    """Clear bounded process state; intended for deterministic test isolation."""
    with _lock:
        _metadata.clear()
        _jwks.clear()
        _replays.clear()
