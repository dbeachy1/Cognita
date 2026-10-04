"""Token generation and verification (DESIGN.md §6).

Tokens are 32 random bytes, URL-safe base64. Only the hex SHA-256 is ever
stored; verification hashes the presented token and compares in constant time.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets


def generate_token() -> str:
    """Return a new high-entropy bearer token (shown to the user ONCE)."""
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    """Hex SHA-256 of a token — the only form that is ever persisted."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def token_matches(presented: str, stored_hash: str) -> bool:
    """Constant-time comparison of a presented token against a stored hash."""
    return hmac.compare_digest(hash_token(presented), stored_hash)
