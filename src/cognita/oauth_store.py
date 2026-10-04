"""Durable, hash-only state for Cognita's OAuth authorization server.

The public credentials (codes, access tokens, and refresh tokens) are opaque
random values. Only SHA-256 digests reach SQLite. Transactions serialize every
single-use transition so concurrent code exchanges or refreshes cannot both win.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


SCOPE = "cognita:access"


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _secret(prefix: str) -> str:
    return prefix + secrets.token_urlsafe(32)


class OAuthStoreError(Exception):
    """OAuth protocol error suitable for a token-endpoint response."""

    def __init__(
        self,
        code: str,
        description: str,
        *,
        category: str | None = None,
        client_id: str = "",
        grant_id: str = "",
    ):
        super().__init__(description)
        self.code = code
        self.description = description
        self.category = category or code
        # These are correlations of non-secret identifiers only. Callers must
        # never pass an access, refresh, or authorization-code value here.
        self.client_ref = _digest(client_id)[:12] if client_id else "none"
        self.grant_ref = _digest(grant_id)[:12] if grant_id else "none"


@dataclass(frozen=True)
class ClientRegistration:
    client_id: str
    client_name: str
    redirect_uris: tuple[str, ...]
    source: str


@dataclass(frozen=True)
class AccessGrant:
    grant_id: str
    client_id: str
    client_name: str
    project: str
    resource: str
    created_at: int
    last_used_at: int | None


class OAuthStore:
    """Small SQLite repository shared by the gateway and local admin app."""

    def __init__(self, path: Path, access_ttl_seconds: int, transaction_secret: bytes):
        self.path = Path(path)
        self.access_ttl_seconds = max(60, int(access_ttl_seconds))
        self.transaction_secret = transaction_secret
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=10000")
        return conn

    def _initialize(self) -> None:
        with self._connect() as db:
            db.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS clients (
                    client_id TEXT PRIMARY KEY,
                    client_name TEXT NOT NULL,
                    redirect_uris TEXT NOT NULL,
                    source TEXT NOT NULL,
                    created_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS auth_requests (
                    request_hash TEXT PRIMARY KEY,
                    client_id TEXT NOT NULL,
                    client_name TEXT NOT NULL,
                    redirect_uri TEXT NOT NULL,
                    resource TEXT NOT NULL,
                    project TEXT NOT NULL,
                    scope TEXT NOT NULL,
                    state TEXT NOT NULL,
                    code_challenge TEXT NOT NULL,
                    expires_at INTEGER NOT NULL,
                    used_at INTEGER
                );
                CREATE TABLE IF NOT EXISTS grants (
                    grant_id TEXT PRIMARY KEY,
                    client_id TEXT NOT NULL,
                    client_name TEXT NOT NULL,
                    project TEXT NOT NULL,
                    resource TEXT NOT NULL,
                    subject TEXT NOT NULL,
                    credential_fingerprint TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    last_used_at INTEGER,
                    revoked_at INTEGER
                );
                CREATE TABLE IF NOT EXISTS authorization_codes (
                    code_hash TEXT PRIMARY KEY,
                    grant_id TEXT NOT NULL REFERENCES grants(grant_id),
                    client_id TEXT NOT NULL,
                    redirect_uri TEXT NOT NULL,
                    resource TEXT NOT NULL,
                    code_challenge TEXT NOT NULL,
                    expires_at INTEGER NOT NULL,
                    used_at INTEGER
                );
                CREATE TABLE IF NOT EXISTS access_tokens (
                    token_hash TEXT PRIMARY KEY,
                    grant_id TEXT NOT NULL REFERENCES grants(grant_id),
                    resource TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    expires_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS refresh_tokens (
                    token_hash TEXT PRIMARY KEY,
                    grant_id TEXT NOT NULL REFERENCES grants(grant_id),
                    family_id TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    used_at INTEGER,
                    revoked_at INTEGER
                );
                CREATE INDEX IF NOT EXISTS access_grant_idx ON access_tokens(grant_id);
                CREATE INDEX IF NOT EXISTS refresh_grant_idx ON refresh_tokens(grant_id);
                CREATE INDEX IF NOT EXISTS grant_project_idx ON grants(project);
                """
            )

    def cleanup(self) -> None:
        """Bound abandoned public state without touching active grants."""
        now = int(time.time())
        with self._connect() as db:
            db.execute("DELETE FROM auth_requests WHERE expires_at < ?", (now - 3600,))
            db.execute("DELETE FROM authorization_codes WHERE expires_at < ?", (now - 86400,))
            db.execute("DELETE FROM access_tokens WHERE expires_at < ?", (now - 86400,))
            db.execute(
                "DELETE FROM clients WHERE created_at < ? AND client_id NOT IN "
                "(SELECT DISTINCT client_id FROM grants)",
                (now - 86400,),
            )

    # ------------------------------------------------------------ clients

    def put_client(self, registration: ClientRegistration) -> None:
        with self._connect() as db:
            db.execute(
                "INSERT INTO clients(client_id, client_name, redirect_uris, source, created_at) "
                "VALUES(?,?,?,?,?) ON CONFLICT(client_id) DO UPDATE SET "
                "client_name=excluded.client_name, redirect_uris=excluded.redirect_uris, "
                "source=excluded.source",
                (
                    registration.client_id,
                    registration.client_name,
                    json.dumps(registration.redirect_uris),
                    registration.source,
                    int(time.time()),
                ),
            )

    def get_client(self, client_id: str) -> ClientRegistration | None:
        with self._connect() as db:
            row = db.execute("SELECT * FROM clients WHERE client_id=?", (client_id,)).fetchone()
        if row is None:
            return None
        return ClientRegistration(
            row["client_id"], row["client_name"], tuple(json.loads(row["redirect_uris"])), row["source"]
        )

    def client_count(self) -> int:
        with self._connect() as db:
            return int(db.execute("SELECT COUNT(*) FROM clients").fetchone()[0])

    # ------------------------------------------------------ authorization

    def _authorization_request_token(self) -> str:
        nonce = _secret("cog_req_")
        signature = hmac.new(
            self.transaction_secret, nonce.encode("ascii"), hashlib.sha256
        ).hexdigest()
        return f"{nonce}.{signature}"

    def _valid_authorization_request_token(self, token: str) -> bool:
        nonce, separator, signature = token.rpartition(".")
        if not separator:
            return False
        expected = hmac.new(
            self.transaction_secret, nonce.encode("utf-8"), hashlib.sha256
        ).hexdigest()
        return hmac.compare_digest(signature.encode("utf-8"), expected.encode("ascii"))

    def create_auth_request(self, fields: dict[str, str]) -> str:
        request_token = self._authorization_request_token()
        now = int(time.time())
        with self._connect() as db:
            db.execute(
                "INSERT INTO auth_requests VALUES(?,?,?,?,?,?,?,?,?,?,NULL)",
                (
                    _digest(request_token),
                    fields["client_id"], fields["client_name"], fields["redirect_uri"],
                    fields["resource"], fields["project"], fields["scope"],
                    fields.get("state", ""), fields["code_challenge"], now + 300,
                ),
            )
        return request_token

    def get_auth_request(self, request_token: str) -> dict[str, Any] | None:
        if not self._valid_authorization_request_token(request_token):
            return None
        now = int(time.time())
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM auth_requests WHERE request_hash=? AND used_at IS NULL "
                "AND expires_at>=?", (_digest(request_token), now)
            ).fetchone()
        return dict(row) if row else None

    def deny_auth_request(self, request_token: str) -> dict[str, Any] | None:
        if not self._valid_authorization_request_token(request_token):
            return None
        now = int(time.time())
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM auth_requests WHERE request_hash=? AND used_at IS NULL "
                "AND expires_at>=?", (_digest(request_token), now)
            ).fetchone()
            if row:
                db.execute(
                    "UPDATE auth_requests SET used_at=? WHERE request_hash=?",
                    (now, _digest(request_token)),
                )
            db.commit()
        return dict(row) if row else None

    def approve_auth_request(
        self, request_token: str, subject: str, fingerprint: str
    ) -> tuple[str, dict[str, Any]]:
        if not self._valid_authorization_request_token(request_token):
            raise OAuthStoreError("invalid_request", "Authorization request is invalid")
        now = int(time.time())
        code = _secret("cog_ac_")
        grant_id = secrets.token_urlsafe(18)
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM auth_requests WHERE request_hash=? AND used_at IS NULL "
                "AND expires_at>=?", (_digest(request_token), now)
            ).fetchone()
            if row is None:
                db.rollback()
                raise OAuthStoreError("invalid_request", "Authorization request expired or was used")
            db.execute(
                "UPDATE auth_requests SET used_at=? WHERE request_hash=?", (now, _digest(request_token))
            )
            db.execute(
                "INSERT INTO grants VALUES(?,?,?,?,?,?,?,?,NULL,NULL)",
                (
                    grant_id, row["client_id"], row["client_name"], row["project"],
                    row["resource"], subject, fingerprint, now,
                ),
            )
            db.execute(
                "INSERT INTO authorization_codes VALUES(?,?,?,?,?,?,?,NULL)",
                (
                    _digest(code), grant_id, row["client_id"], row["redirect_uri"],
                    row["resource"], row["code_challenge"], now + 300,
                ),
            )
            db.commit()
        return code, dict(row)

    def exchange_code(
        self,
        code: str,
        client_id: str,
        redirect_uri: str,
        resource: str,
        verifier_challenge: str,
        fingerprint: str,
    ) -> dict[str, Any]:
        now = int(time.time())
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT c.*, g.credential_fingerprint, g.revoked_at FROM authorization_codes c "
                "JOIN grants g ON g.grant_id=c.grant_id WHERE c.code_hash=?",
                (_digest(code),),
            ).fetchone()
            if (
                row is None or row["used_at"] is not None or row["expires_at"] < now
                or row["revoked_at"] is not None or row["client_id"] != client_id
                or row["redirect_uri"] != redirect_uri or row["resource"] != resource
                or row["code_challenge"] != verifier_challenge
                or row["credential_fingerprint"] != fingerprint
            ):
                db.rollback()
                raise OAuthStoreError("invalid_grant", "Authorization code is invalid or expired")
            db.execute("UPDATE authorization_codes SET used_at=? WHERE code_hash=?", (now, _digest(code)))
            result = self._mint_pair(db, row["grant_id"], resource, now)
            db.commit()
        return result

    def _mint_pair(
        self, db: sqlite3.Connection, grant_id: str, resource: str, now: int,
        family_id: str | None = None,
    ) -> dict[str, Any]:
        access = _secret("cog_at_")
        refresh = _secret("cog_rt_")
        family = family_id or secrets.token_urlsafe(18)
        db.execute(
            "INSERT INTO access_tokens VALUES(?,?,?,?,?)",
            (_digest(access), grant_id, resource, now, now + self.access_ttl_seconds),
        )
        db.execute(
            "INSERT INTO refresh_tokens VALUES(?,?,?,?,NULL,NULL)",
            (_digest(refresh), grant_id, family, now),
        )
        return {
            "access_token": access,
            "refresh_token": refresh,
            "token_type": "Bearer",
            "expires_in": self.access_ttl_seconds,
            "scope": SCOPE,
            "_grant_ref": _digest(grant_id)[:12],
        }

    def refresh(
        self, refresh_token: str, client_id: str, resource: str, fingerprint: str
    ) -> dict[str, Any]:
        now = int(time.time())
        token_hash = _digest(refresh_token)
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT r.*, r.revoked_at AS token_revoked_at, g.client_id, g.resource, "
                "g.credential_fingerprint, g.revoked_at AS grant_revoked_at "
                "FROM refresh_tokens r JOIN grants g ON g.grant_id=r.grant_id "
                "WHERE r.token_hash=?", (token_hash,)
            ).fetchone()
            if row is None:
                db.rollback()
                raise OAuthStoreError(
                    "invalid_grant", "Refresh token is invalid",
                    category="unknown_refresh_token",
                )
            # Binding checks intentionally precede all replay/revocation state.
            # A token presented by the wrong client, resource, or credential
            # must not let that caller revoke the legitimate grant family.
            if row["client_id"] != client_id:
                db.rollback()
                raise OAuthStoreError(
                    "invalid_grant", "Refresh token is not valid for this client",
                    category="client_mismatch", client_id=client_id, grant_id=row["grant_id"],
                )
            if row["resource"] != resource:
                db.rollback()
                raise OAuthStoreError(
                    "invalid_grant", "Refresh token is not valid for this resource",
                    category="resource_mismatch", client_id=client_id, grant_id=row["grant_id"],
                )
            if row["credential_fingerprint"] != fingerprint:
                db.rollback()
                raise OAuthStoreError(
                    "invalid_grant", "Refresh token is not valid for this installation",
                    category="credential_mismatch", client_id=client_id, grant_id=row["grant_id"],
                )
            if row["grant_revoked_at"] is not None:
                db.rollback()
                raise OAuthStoreError(
                    "invalid_grant",
                    "OAuth grant was revoked or replayed; fresh authorization is required",
                    category="grant_revoked", client_id=client_id, grant_id=row["grant_id"],
                )
            if row["token_revoked_at"] is not None:
                db.rollback()
                raise OAuthStoreError(
                    "invalid_grant", "Refresh token was revoked; fresh authorization is required",
                    category="refresh_token_revoked", client_id=client_id, grant_id=row["grant_id"],
                )
            if row["used_at"] is not None:
                # This is the first correctly bound replay only when the grant
                # is still live. Preserve the first replay timestamp and avoid
                # repeat writes once the family is already revoked.
                db.execute(
                    "UPDATE grants SET revoked_at=? WHERE grant_id=? AND revoked_at IS NULL",
                    (now, row["grant_id"]),
                )
                db.execute(
                    "UPDATE refresh_tokens SET revoked_at=? WHERE family_id=? AND revoked_at IS NULL",
                    (now, row["family_id"]),
                )
                db.commit()
                raise OAuthStoreError(
                    "invalid_grant",
                    "Refresh token was replayed and the grant was revoked; fresh authorization is required",
                    category="refresh_token_reuse", client_id=client_id, grant_id=row["grant_id"],
                )
            db.execute("UPDATE refresh_tokens SET used_at=? WHERE token_hash=?", (now, token_hash))
            result = self._mint_pair(db, row["grant_id"], resource, now, row["family_id"])
            db.commit()
        return result

    # ------------------------------------------------------------ resource

    def validate_access(self, token: str, resource: str, fingerprint: str) -> AccessGrant | None:
        now = int(time.time())
        with self._connect() as db:
            row = db.execute(
                "SELECT a.expires_at, g.* FROM access_tokens a "
                "JOIN grants g ON g.grant_id=a.grant_id WHERE a.token_hash=? AND a.resource=?",
                (_digest(token), resource),
            ).fetchone()
            if (
                row is None or row["expires_at"] < now or row["revoked_at"] is not None
                or row["credential_fingerprint"] != fingerprint
            ):
                return None
            db.execute("UPDATE grants SET last_used_at=? WHERE grant_id=?", (now, row["grant_id"]))
        return AccessGrant(
            row["grant_id"], row["client_id"], row["client_name"], row["project"],
            row["resource"], row["created_at"], now,
        )

    def revoke_token(self, token: str) -> None:
        now = int(time.time())
        token_hash = _digest(token)
        with self._connect() as db:
            row = db.execute(
                "SELECT grant_id FROM access_tokens WHERE token_hash=? UNION ALL "
                "SELECT grant_id FROM refresh_tokens WHERE token_hash=? LIMIT 1",
                (token_hash, token_hash),
            ).fetchone()
            if row:
                db.execute("UPDATE grants SET revoked_at=? WHERE grant_id=? AND revoked_at IS NULL", (now, row["grant_id"]))

    # --------------------------------------------------------------- admin

    def list_grants(self) -> list[AccessGrant]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM grants WHERE revoked_at IS NULL ORDER BY created_at DESC"
            ).fetchall()
        return [
            AccessGrant(
                r["grant_id"], r["client_id"], r["client_name"], r["project"],
                r["resource"], r["created_at"], r["last_used_at"],
            )
            for r in rows
        ]

    def revoke_grant(self, grant_id: str) -> bool:
        with self._connect() as db:
            cur = db.execute(
                "UPDATE grants SET revoked_at=? WHERE grant_id=? AND revoked_at IS NULL",
                (int(time.time()), grant_id),
            )
        return cur.rowcount > 0

    def revoke_all(self) -> int:
        with self._connect() as db:
            cur = db.execute(
                "UPDATE grants SET revoked_at=? WHERE revoked_at IS NULL", (int(time.time()),)
            )
        return cur.rowcount

    def revoke_stale_credentials(self, fingerprint: str) -> int:
        """Revoke grants created under an older admin credential verifier."""
        with self._connect() as db:
            cur = db.execute(
                "UPDATE grants SET revoked_at=? WHERE revoked_at IS NULL "
                "AND credential_fingerprint<>?",
                (int(time.time()), fingerprint),
            )
        return cur.rowcount
