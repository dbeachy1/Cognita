"""In-app auth for the admin surface (DESIGN.md §6/§8).

The admin API/UI historically relied on a 127.0.0.1 bind for its security. To
reach it from another machine we bind it to a non-loopback address instead, and
gate it with a password *in the app* (no reverse proxy — Doug's call). Only the
password hash is ever stored; comparison is constant-time.

Auth is a **form login + signed session cookie** (not HTTP Basic — that made the
browser cache credentials with no way to log out). Flow:

- No password configured + loopback bind  -> open (unchanged local UX).
- Password configured                     -> login required; a signed cookie
                                             carries the session on every route.
- Non-loopback bind + no password         -> refused at startup (see __main__).

The cookie is a stdlib HMAC-signed token (no external dependency). Its signing
key is derived from `admin_password_sha256`, so changing the admin password (or
username) invalidates every outstanding session automatically. Over a public
bind the cookie is only as private as the transport — put TLS in front (e.g. the
tunnel / a mkcert cert) or keep the bind on a trusted LAN.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import json
import logging
import os
import secrets
import socket
import time
from pathlib import Path

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from .config import CognitaConfig
from .tokens import hash_token

log = logging.getLogger("cognita.admin_auth")

SESSION_COOKIE = "cognita_admin_session"

_LOOPBACK_NAMES = {"localhost", ""}


def is_loopback(host: str) -> bool:
    """True if `host` is a loopback bind (127.0.0.0/8, ::1, or 'localhost').

    A non-loopback bind (0.0.0.0, a LAN/public IP, or any other hostname) is
    treated as exposed and therefore requires an admin password.
    """
    h = (host or "").strip().strip("[]").lower()
    if h in _LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(h).is_loopback
    except ValueError:
        # A non-numeric hostname other than 'localhost' — treat as exposed.
        return False


def admin_auth_configured(config: CognitaConfig) -> bool:
    """Whether an admin password hash is set (login will be required)."""
    return bool(config.admin_password_hash or config.admin_password_sha256)


def has_argon2_credentials(config: CognitaConfig) -> bool:
    """Whether credentials are safe to expose through the public OAuth form."""
    return bool(config.admin_password_hash.startswith("$argon2id$"))


def verify_login(config: CognitaConfig, username: str, password: str) -> bool:
    """Constant-time check of a submitted username + password against config."""
    if not admin_auth_configured(config):
        return True  # open admin (loopback only — the startup guard enforces that)
    # compare_digest on str operands raises TypeError unless BOTH are pure ASCII,
    # so a non-ASCII username 500'd the login route instead of returning 401 —
    # and would have locked the admin out permanently had one ever been set.
    # Bytes have no such restriction and compare just as constant-time.
    user_ok = hmac.compare_digest(
        (username or "").encode("utf-8"), config.admin_username.encode("utf-8")
    )
    if config.admin_password_hash:
        try:
            pass_ok = PasswordHasher().verify(config.admin_password_hash, password or "")
        except (VerifyMismatchError, VerificationError, InvalidHashError):
            pass_ok = False
    else:
        # Compatibility is intentionally limited to the LAN admin login. OAuth
        # startup requires Argon2id, so this fast legacy verifier is never
        # exposed by the public authorization endpoint.
        pass_ok = hmac.compare_digest(
            hash_token(password or "").encode("ascii"),
            config.admin_password_sha256.encode("utf-8"),
        )
    return user_ok and pass_ok


def credential_fingerprint(config: CognitaConfig) -> str:
    """Stable grant binding that changes with either admin credential field."""
    verifier = config.admin_password_hash or config.admin_password_sha256
    material = f"cognita.oauth.credentials.v1|{config.admin_username}|{verifier}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


# ----------------------------------------------------------------- session cookie


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64d(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


SESSION_KEY_FILENAME = ".session_key"

# Cache the per-install key by path: _session_secret runs on EVERY authenticated
# request, and this must not become a file read per request.
_SESSION_KEYS: dict[str, bytes] = {}


def _session_key_path(config: CognitaConfig) -> Path:
    return Path(config.data_root) / SESSION_KEY_FILENAME


def _install_session_key(config: CognitaConfig) -> bytes:
    """A random per-install secret, generated once and persisted under data_root.

    Why this exists: the signing key used to be derived from
    `admin_password_sha256` ALONE, and that field ships with a default. Anyone
    holding the repo could therefore compute the key of any install still on the
    default password and mint a valid admin cookie offline — no password, no
    login request, nothing to rate-limit. A secret that is random per install
    closes that; the password hash stays mixed in below so changing the password
    still invalidates every outstanding session.

    If the key cannot be persisted (read-only disk), fall back to a process-
    lifetime random key: sessions then die on restart, which is fail-SAFE.

    🔴 A key we FAILED TO READ is not a key that is absent. Until 5.6.3 an
    OSError here logged a warning and fell through to the generate-and-write
    branch, whose open() carries O_TRUNC — so one transient read error (a virus
    scanner holding the file on Windows, a stale NFS handle, an EINTR) DESTROYED
    the durable secret and logged every admin session out for good, with only a
    warning line to say why. The write-failure fallback was fail-safe and this
    one was not, which is precisely backwards: a read failure is the transient
    case and a write failure is the permanent one.

    So the two are now distinguished. Unreadable-but-present means use an
    ephemeral key for this process and LEAVE THE FILE ALONE; the next read very
    likely succeeds and every outstanding cookie still verifies. Only a genuinely
    absent or unusably-short file is written.
    """
    path = _session_key_path(config)
    cached = _SESSION_KEYS.get(str(path))
    if cached is not None:
        return cached
    key: bytes | None = None
    unreadable = False
    try:
        if path.is_file():
            # 🔴 NOT .strip(). This is 32 RANDOM BYTES, not text, and six byte
            # values (0x09 0x0a 0x0b 0x0c 0x0d 0x20) are whitespace: a key that
            # happened to begin or end with one came back SHORT, failed the
            # length check, and was regenerated and overwritten. That is 4.7% of
            # all generated keys (1 - (250/256)^2), so roughly one install in
            # twenty-one logged every admin session out on EVERY restart while
            # reporting nothing but a debug-level line — and the whole point of
            # persisting this file is that a restart does not do that.
            #
            # It is the same mistake as the read path this release started with:
            # normalizing data that must be handled byte for byte. A secret is
            # the least forgiving possible place to do it.
            raw = path.read_bytes()
            if len(raw) >= 32:
                key = raw
            else:
                # Genuinely short — a truncated write. Replacing it is right; it
                # cannot verify any existing cookie anyway.
                log.warning("Admin session key %s is too short (%d bytes); replacing it.",
                            path, len(raw))
    except OSError as exc:
        unreadable = True
        log.warning(
            "Could not READ admin session key %s (%s) — using a process-lifetime key "
            "for now and leaving the file untouched, so existing sessions survive "
            "once it is readable again.", path, exc,
        )
    if key is None and unreadable:
        # Ephemeral, and deliberately NOT cached: caching it would pin the wrong
        # key for the life of the process and turn a transient error into a
        # permanent one by the back door.
        return secrets.token_bytes(32)
    if key is None:
        key = secrets.token_bytes(32)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            # Create 0600 BEFORE writing, so the secret is never briefly world-readable.
            # 🔴 O_BINARY is not optional. On Windows os.open() defaults to TEXT
            # mode, which rewrites every 0x0a byte as 0x0d 0x0a — and a random
            # 32-byte key contains a 0x0a 11.8% of the time (1 - (255/256)^32).
            # The file then read back 33 bytes that were never the key, so the
            # signing secret changed at the next start and every admin session
            # died. The constant only exists on Windows; 0 elsewhere is a no-op.
            #
            # This is the SAME defect 5.0.0 fixed in the document write path
            # (see LocalEngineHost._write_verbatim: Path.write_text translating
            # "\n" to os.linesep), surviving in the one place where the payload
            # is a cryptographic secret rather than prose.
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC
                         | getattr(os, "O_BINARY", 0), 0o600)
            try:
                os.write(fd, key)
            finally:
                os.close(fd)
            log.info("Generated a new admin session signing key at %s", path)
        except OSError as exc:
            log.warning(
                "Could not persist admin session key %s (%s) — using a process-lifetime "
                "key; admin sessions will not survive a restart.", path, exc,
            )
    _SESSION_KEYS[str(path)] = key
    return key


def durable_install_secret(config: CognitaConfig) -> bytes | None:
    """Return the per-install secret only when it was safely persisted."""
    key = _install_session_key(config)
    try:
        stored = _session_key_path(config).read_bytes()
    except OSError:
        return None
    return key if len(stored) >= 32 and hmac.compare_digest(key, stored) else None


def revoke_all_sessions(config: CognitaConfig) -> None:
    """Rotate the install session key, invalidating every outstanding cookie.

    Logout used to be cosmetic: it called delete_cookie and nothing else. There
    is no server-side session store and no issued-at floor, so a cookie captured
    beforehand kept working until `exp` — up to admin_session_max_age_days, 30
    by default — no matter how many times the admin "logged out". The only
    revocation lever was changing the password.

    Rotating the key logs out every session for this install, including the
    admin's other browsers. With a single admin account that is the correct
    reading of "log me out", and it is the behavior someone clicking logout on
    a shared or lost machine is relying on.
    """
    path = _session_key_path(config)
    _SESSION_KEYS.pop(str(path), None)
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        log.warning("Could not rotate the admin session key %s: %s", path, exc)
    _install_session_key(config)  # regenerate immediately


def _session_secret(config: CognitaConfig) -> bytes:
    """Signing key: a random per-install secret, bound to the current credentials.

    Ties every session to the current credentials: change the password (or
    disable auth) and all outstanding cookies stop verifying. The per-install
    secret is what stops the key being derivable from public repo defaults.
    """
    base = config.admin_password_hash or config.admin_password_sha256 or "cognita-open-admin"
    return hmac.new(
        _install_session_key(config), ("cognita.session.v1|" + base).encode("utf-8"), hashlib.sha256
    ).digest()


def _sign(config: CognitaConfig, body: str) -> str:
    # `body` is always our own base64 (ASCII); an attacker-supplied cookie can
    # carry anything, so encode defensively rather than raising UnicodeEncodeError
    # out of a verification path whose contract is "return None on a bad token".
    return _b64e(
        hmac.new(_session_secret(config), body.encode("utf-8"), hashlib.sha256).digest()
    )


def issue_session_token(config: CognitaConfig) -> str:
    """Mint a signed session token for the configured admin user."""
    exp = int(time.time()) + max(1, config.admin_session_max_age_days) * 86400
    payload = {"u": config.admin_username, "exp": exp}
    body = _b64e(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8"))
    return f"{body}.{_sign(config, body)}"


def read_session_user(config: CognitaConfig, token: str | None) -> str | None:
    """Return the username from a valid, unexpired session token, else None."""
    if not token or "." not in token:
        return None
    body, _, sig = token.partition(".")
    # Both operands come straight from the caller's cookie, which Starlette
    # decodes latin-1 — a non-ASCII byte made compare_digest raise TypeError and
    # 500 every admin route. This function's contract is "None on a bad token".
    if not hmac.compare_digest(sig.encode("utf-8"), _sign(config, body).encode("ascii")):
        return None
    try:
        payload = json.loads(_b64d(body))
    except (ValueError, json.JSONDecodeError):
        return None
    if int(payload.get("exp", 0)) < int(time.time()):
        return None
    user = payload.get("u")
    # Reject a cookie minted for a different username (defense in depth: the
    # signing key already rotates when the password changes, but the username
    # can change without the password).
    if user != config.admin_username:
        return None
    return user


def session_cookie_kwargs(config: CognitaConfig) -> dict:
    """Flags for Response.set_cookie — Secure only when the admin serves TLS."""
    tls_on = bool(config.admin_tls_certfile and config.admin_tls_keyfile)
    return {
        "max_age": max(1, config.admin_session_max_age_days) * 86400,
        "httponly": True,
        "secure": tls_on,
        "samesite": "lax",
        "path": "/",
    }


# ------------------------------------------------------- trusted hosts (5.4)

# Always allowed: the machine talking to itself.
_ALWAYS_ALLOWED = ("localhost", "127.0.0.1", "::1", "[::1]")


def _cert_hostnames(certfile) -> list[str]:
    """The subjectAltName entries of the admin TLS certificate.

    The cert is the closest thing to a DECLARATION of which names this surface
    is served for: mkcert was run with exactly the names the browser has to
    trust, so reading it is how the allow-list stays correct without anyone
    maintaining a second list that drifts.
    """
    if not certfile:
        return []
    try:
        from cryptography import x509
        from cryptography.x509.oid import ExtensionOID

        cert = x509.load_pem_x509_certificate(Path(certfile).read_bytes())
        san = cert.extensions.get_extension_for_oid(
            ExtensionOID.SUBJECT_ALTERNATIVE_NAME
        ).value
        names = list(san.get_values_for_type(x509.DNSName))
        names += [str(ip) for ip in san.get_values_for_type(x509.IPAddress)]
        return names
    except Exception as exc:  # noqa: BLE001 - never let this break startup
        log.warning("Could not read hostnames from %s: %s", certfile, exc)
        return []


def _local_addresses() -> list[str]:
    """This machine's own hostname, FQDN and routable IPv4 addresses."""
    out: list[str] = []
    try:
        host = socket.gethostname()
        out.append(host)
        out.append(socket.getfqdn(host))
        for info in socket.getaddrinfo(host, None):
            out.append(info[4][0])
    except OSError as exc:
        log.warning("Could not enumerate local addresses: %s", exc)
    return out


def allowed_admin_hosts(config: CognitaConfig) -> list[str]:
    """The Host header values the admin surface will answer to.

    Why this exists: nothing validated `Host`, so a page the admin visits could
    rebind attacker.tld to this machine's address and the browser would treat
    `http://attacker.tld:8676/` as same-origin — reading responses from an
    authenticated admin session. `SameSite=Lax` does not help, because after
    rebinding the request IS same-site.

    Why it is DERIVED rather than configured: an allow-list that omits the name
    the operator actually types locks them out of the admin UI with no route
    back but SSH, and that risk is exactly why this was not added earlier. So the
    default is computed from things that already know the answer — the TLS
    certificate's SANs (mkcert was run with the names the browser must trust),
    the machine's own hostname and FQDN, its routable addresses, and loopback.
    This covers the names and addresses in the certificate and on the host
    without copying them into another static allow-list.

    `admin_allowed_hosts` in config overrides the derivation entirely, and
    `["*"]` switches the check off — the escape hatch, in the config file rather
    than in a code change.
    """
    configured = [str(h).strip() for h in (config.admin_allowed_hosts or []) if str(h).strip()]
    if configured:
        return configured
    hosts = list(_ALWAYS_ALLOWED)
    hosts += _cert_hostnames(config.admin_tls_certfile)
    hosts += _local_addresses()
    if is_loopback(config.admin_host):
        hosts.append(config.admin_host)
    elif config.admin_host and config.admin_host not in ("0.0.0.0", "::"):
        # A specific non-loopback bind names a reachable address by definition.
        hosts.append(config.admin_host)
    seen, out = set(), []
    for h in hosts:
        key = h.strip().strip("[]").lower()
        if key and key not in seen:
            seen.add(key)
            out.append(h)
    return out
