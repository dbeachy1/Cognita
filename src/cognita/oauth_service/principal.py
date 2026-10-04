"""Durable OAuth-grant identity for Cognita 12.

DOT owns OAuth token material and expiry.  This small store owns only the
non-secret relationship between an authorization grant and Cognita's durable
principal.  Keeping it separate means refresh rotation and grant revocation do
not accidentally collapse distinct consents that happen to share user/client/
resource metadata.
"""

from __future__ import annotations

import hashlib
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Iterable

from .storage import ensure_private_sqlite


class DurablePrincipalError(RuntimeError):
    """Base class for durable principal storage failures."""


class PrincipalBindingConflict(DurablePrincipalError):
    """A token is already bound to another principal."""


class PrincipalRevoked(DurablePrincipalError):
    """A revoked principal cannot receive new token bindings."""


class PrincipalValidationError(DurablePrincipalError):
    """A caller supplied malformed or incomplete identity data."""


@dataclass(frozen=True, slots=True)
class OAuthPrincipal:
    principal_id: str
    user_id: str
    application_id: str
    exact_resource: str
    created_at: str
    revoked_at: str | None = None


@dataclass(frozen=True, slots=True)
class TokenBinding:
    principal_id: str
    token_kind: str
    token_primary_key: str
    created_at: str


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _id(value: object, name: str) -> str:
    if isinstance(value, uuid.UUID):
        return str(value)
    if not isinstance(value, str):
        raise PrincipalValidationError(f"{name} must be a string")
    try:
        return str(uuid.UUID(value))
    except ValueError as exc:
        raise PrincipalValidationError(f"{name} must be a UUID") from exc


def _text(value: object, name: str, limit: int = 512) -> str:
    if not isinstance(value, str) or not value or len(value) > limit or "\x00" in value:
        raise PrincipalValidationError(f"{name} is invalid")
    return value


class OAuthPrincipalStore:
    """SQLite-backed principal and token-binding service.

    The connection is short-lived per operation and serialized with an
    in-process lock; SQLite's ``BEGIN IMMEDIATE`` provides the corresponding
    cross-process lock for code/token issuance and migration.
    """

    def __init__(self, path: Path, *, create: bool = True):
        self.path = Path(path)
        self._lock = threading.RLock()
        if create:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            ensure_private_sqlite(self.path, create=True)
            self._schema()
        elif not self.path.exists():
            raise DurablePrincipalError("OAuth principal database is missing")

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=20, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    @contextmanager
    def _database(self):
        """Close each short-lived connection, including on failed operations."""
        db = self._connect()
        try:
            yield db
        finally:
            db.close()

    def _schema(self) -> None:
        with self._database() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS oauth_principal (
                    principal_id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    application_id TEXT NOT NULL,
                    exact_resource TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    revoked_at TEXT,
                    migration_key TEXT UNIQUE
                );
                CREATE TABLE IF NOT EXISTS oauth_token_binding (
                    principal_id TEXT NOT NULL REFERENCES oauth_principal(principal_id),
                    token_kind TEXT NOT NULL,
                    token_primary_key TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (token_kind, token_primary_key)
                );
                CREATE INDEX IF NOT EXISTS oauth_token_binding_principal
                    ON oauth_token_binding(principal_id);
                """
            )

    @staticmethod
    def _principal(row: sqlite3.Row | None) -> OAuthPrincipal | None:
        if row is None:
            return None
        return OAuthPrincipal(row["principal_id"], row["user_id"], row["application_id"], row["exact_resource"], row["created_at"], row["revoked_at"])

    def get(self, principal_id: str) -> OAuthPrincipal | None:
        principal_id = _id(principal_id, "principal_id")
        with self._lock, self._database() as db:
            return self._principal(db.execute("SELECT * FROM oauth_principal WHERE principal_id=?", (principal_id,)).fetchone())

    def principal_ids_for_connection(
        self, user_id: str, application_id: str, exact_resource: str
    ) -> tuple[str, ...]:
        """Return every durable grant for one DOT connection identity.

        A connection groups all consents sharing the DOT user/application/
        resource tuple, while each consent has its own durable principal.  The
        revoke path must therefore include already-revoked rows as well as live
        rows: after a crash between the principal and DOT stores, the live DOT
        group is the durable retry handle and this lookup must remain stable.
        """
        user_id = _text(user_id, "user_id")
        application_id = _text(application_id, "application_id")
        exact_resource = _text(exact_resource, "exact_resource")
        with self._lock, self._database() as db:
            rows = db.execute(
                "SELECT principal_id FROM oauth_principal "
                "WHERE user_id=? AND application_id=? AND exact_resource=? "
                "ORDER BY principal_id",
                (user_id, application_id, exact_resource),
            ).fetchall()
        return tuple(str(row["principal_id"]) for row in rows)

    def create_principal(self, user_id: str, application_id: str, exact_resource: str, *, code_key: str | None = None, principal_id: str | None = None) -> OAuthPrincipal:
        user_id = _text(user_id, "user_id")
        application_id = _text(application_id, "application_id")
        exact_resource = _text(exact_resource, "exact_resource")
        principal_id = _id(principal_id or uuid.uuid4(), "principal_id")
        code_key = None if code_key is None else _text(code_key, "code_key")
        now = _now()
        with self._lock, self._database() as db:
            try:
                db.execute("BEGIN IMMEDIATE")
                db.execute("INSERT INTO oauth_principal(principal_id,user_id,application_id,exact_resource,created_at,migration_key) VALUES(?,?,?,?,?,?)", (principal_id, user_id, application_id, exact_resource, now, code_key))
                if code_key is not None:
                    db.execute("INSERT INTO oauth_token_binding(principal_id,token_kind,token_primary_key,created_at) VALUES(?,?,?,?)", (principal_id, "authorization_code", code_key, now))
                db.commit()
            except sqlite3.IntegrityError as exc:
                db.rollback()
                raise PrincipalBindingConflict("principal or authorization code already exists") from exc
        return OAuthPrincipal(principal_id, user_id, application_id, exact_resource, now)

    def bind_token(self, principal_id: str, token_kind: str, token_primary_key: str) -> TokenBinding:
        principal_id = _id(principal_id, "principal_id")
        token_kind = _text(token_kind, "token_kind", 64)
        token_primary_key = _text(token_primary_key, "token_primary_key")
        if token_kind not in {"authorization_code", "access", "refresh"}:
            raise PrincipalValidationError("unsupported token kind")
        now = _now()
        with self._lock, self._database() as db:
            db.execute("BEGIN IMMEDIATE")
            self._bind_many(db, principal_id, ((token_kind, token_primary_key),), now)
            db.commit()
        return TokenBinding(principal_id, token_kind, token_primary_key, now)

    @staticmethod
    def _bind_many(db: sqlite3.Connection, principal_id: str, bindings: Iterable[tuple[str, str]], now: str) -> None:
        principal = db.execute("SELECT revoked_at FROM oauth_principal WHERE principal_id=?", (principal_id,)).fetchone()
        if principal is None:
            db.rollback()
            raise PrincipalValidationError("principal is not persisted")
        if principal["revoked_at"] is not None:
            db.rollback()
            raise PrincipalRevoked("principal is revoked")
        for token_kind, token_primary_key in bindings:
            existing = db.execute("SELECT principal_id FROM oauth_token_binding WHERE token_kind=? AND token_primary_key=?", (token_kind, token_primary_key)).fetchone()
            if existing and existing["principal_id"] != principal_id:
                db.rollback()
                raise PrincipalBindingConflict("token is already bound to another principal")
            if not existing:
                db.execute("INSERT INTO oauth_token_binding(principal_id,token_kind,token_primary_key,created_at) VALUES(?,?,?,?)", (principal_id, token_kind, token_primary_key, now))

    def principal_for_token(self, token_kind: str, token_primary_key: str) -> OAuthPrincipal | None:
        token_kind = _text(token_kind, "token_kind", 64)
        token_primary_key = _text(token_primary_key, "token_primary_key")
        with self._lock, self._database() as db:
            row = db.execute("SELECT p.* FROM oauth_principal p JOIN oauth_token_binding b ON b.principal_id=p.principal_id WHERE b.token_kind=? AND b.token_primary_key=? AND p.revoked_at IS NULL", (token_kind, token_primary_key)).fetchone()
            return self._principal(row)

    def binding_for_token(self, token_kind: str, token_primary_key: str) -> TokenBinding | None:
        token_kind = _text(token_kind, "token_kind", 64)
        token_primary_key = _text(token_primary_key, "token_primary_key")
        with self._lock, self._database() as db:
            row = db.execute("SELECT * FROM oauth_token_binding WHERE token_kind=? AND token_primary_key=?", (token_kind, token_primary_key)).fetchone()
            return None if row is None else TokenBinding(row["principal_id"], row["token_kind"], row["token_primary_key"], row["created_at"])

    def bind_exchange(self, code_key: str, access_key: str, refresh_key: str | None = None) -> OAuthPrincipal:
        code_key = _text(code_key, "code_key")
        access_key = _text(access_key, "access_key")
        with self._lock, self._database() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT p.* FROM oauth_principal p JOIN oauth_token_binding b ON b.principal_id=p.principal_id WHERE b.token_kind='authorization_code' AND b.token_primary_key=? AND p.revoked_at IS NULL", (code_key,)).fetchone()
            principal = self._principal(row)
            if principal is None:
                db.rollback()
                raise PrincipalValidationError("authorization code is unbound or revoked")
            bindings = [("access", access_key)]
            if refresh_key is not None:
                bindings.append(("refresh", _text(refresh_key, "refresh_key")))
            self._bind_many(db, principal.principal_id, bindings, _now())
            db.commit()
            return principal

    def bind_refresh_rotation(self, old_refresh_key: str, replacement_refresh_key: str, replacement_access_key: str | None = None) -> OAuthPrincipal:
        old_refresh_key = _text(old_refresh_key, "old_refresh_key")
        with self._lock, self._database() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT p.* FROM oauth_principal p JOIN oauth_token_binding b ON b.principal_id=p.principal_id WHERE b.token_kind='refresh' AND b.token_primary_key=? AND p.revoked_at IS NULL", (old_refresh_key,)).fetchone()
            principal = self._principal(row)
            if principal is None:
                db.rollback()
                raise PrincipalValidationError("refresh token is unbound or revoked")
            bindings = [("refresh", _text(replacement_refresh_key, "replacement_refresh_key"))]
            if replacement_access_key is not None:
                bindings.append(("access", _text(replacement_access_key, "replacement_access_key")))
            self._bind_many(db, principal.principal_id, bindings, _now())
            db.commit()
            return principal

    def revoke(self, principal_id: str) -> bool:
        principal_id = _id(principal_id, "principal_id")
        with self._lock, self._database() as db:
            db.execute("BEGIN IMMEDIATE")
            changed = db.execute("UPDATE oauth_principal SET revoked_at=COALESCE(revoked_at,?) WHERE principal_id=?", (_now(), principal_id)).rowcount
            db.commit()
            return bool(changed)

    def migrate_connection(self, user_id: str, application_id: str, exact_resource: str, token_bindings: Iterable[tuple[str, str]], *, migration_key: str | None = None) -> OAuthPrincipal:
        """Idempotently bind live 11.x connection tokens to one principal."""
        user_id = _text(user_id, "user_id")
        application_id = _text(application_id, "application_id")
        exact_resource = _text(exact_resource, "exact_resource")
        key = migration_key or hashlib.sha256(f"{user_id}\0{application_id}\0{exact_resource}".encode()).hexdigest()
        key = _text(key, "migration_key")
        with self._lock, self._database() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM oauth_principal WHERE migration_key=?", (key,)).fetchone()
            if row is None:
                principal_id = str(uuid.uuid4())
                now = _now()
                db.execute("INSERT INTO oauth_principal(principal_id,user_id,application_id,exact_resource,created_at,migration_key) VALUES(?,?,?,?,?,?)", (principal_id, user_id, application_id, exact_resource, now, key))
                row = db.execute("SELECT * FROM oauth_principal WHERE principal_id=?", (principal_id,)).fetchone()
            elif row["revoked_at"] is not None:
                db.rollback()
                raise PrincipalRevoked("migrated principal is revoked")
            for token_kind, token_key in token_bindings:
                token_kind = _text(token_kind, "token_kind", 64)
                token_key = _text(token_key, "token_key")
                if token_kind not in {"authorization_code", "access", "refresh"}:
                    db.rollback()
                    raise PrincipalValidationError("unsupported token kind")
                existing = db.execute("SELECT principal_id FROM oauth_token_binding WHERE token_kind=? AND token_primary_key=?", (token_kind, token_key)).fetchone()
                if existing and existing["principal_id"] != row["principal_id"]:
                    db.rollback()
                    raise PrincipalBindingConflict("live token is bound to another principal")
                if not existing:
                    db.execute("INSERT INTO oauth_token_binding(principal_id,token_kind,token_primary_key,created_at) VALUES(?,?,?,?)", (row["principal_id"], token_kind, token_key, _now()))
            db.commit()
            return self._principal(row)  # type: ignore[return-value]

    # Named seams used by the OAuth child and route adapters.  Keeping these
    # wrappers here prevents each transport from reimplementing binding policy.
    def create_authorization_principal(self, user_id: str, application_id: str, exact_resource: str, code_key: str) -> OAuthPrincipal:
        return self.create_principal(user_id, application_id, exact_resource, code_key=code_key)

    def bind_authorization_code(self, principal_id: str, code_key: str) -> TokenBinding:
        return self.bind_token(principal_id, "authorization_code", code_key)

    def bind_access_token(self, principal_id: str, token_key: str) -> TokenBinding:
        return self.bind_token(principal_id, "access", token_key)

    def bind_refresh_token(self, principal_id: str, token_key: str) -> TokenBinding:
        return self.bind_token(principal_id, "refresh", token_key)

    def principal_for_access_token(self, token_key: str) -> OAuthPrincipal | None:
        return self.principal_for_token("access", token_key)

    def principal_for_refresh_token(self, token_key: str) -> OAuthPrincipal | None:
        return self.principal_for_token("refresh", token_key)

    def revoke_principal(self, principal_id: str) -> bool:
        return self.revoke(principal_id)


__all__ = [
    "DurablePrincipalStore", "PrincipalRecord",
    "DurablePrincipalError", "OAuthPrincipal", "OAuthPrincipalStore",
    "PrincipalBindingConflict", "PrincipalRevoked", "PrincipalValidationError",
    "TokenBinding",
]

DurablePrincipalStore = OAuthPrincipalStore
PrincipalRecord = OAuthPrincipal
