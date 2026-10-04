# Cognita 8.0 OAuth Toolkit proof

Date: 2026-09-13

The executable proof in [proof.py](proof.py) uses only synthetic users, applications, tokens, and a task-owned temporary SQLite database. It never prints token values, token hashes, credentials, request bodies, or database paths. The script closes Django connections before temporary-root teardown and asserts that each owned root is absent.

The tested stack was Python 3.13.14, Django 6.1.1, django-oauth-toolkit 3.4.1, and oauthlib 3.3.1.

## Supported plaintext grace configuration

Command:

    venv\Scripts\python.exe -u scripts\oauth8_proof\proof.py --grace 120 --plaintext --skip-migration

The redacted result passed:

- PKCE S256 authorization-code redirect: 302.
- Token response contained only the standard public keys.
- Bearer introspection and direct confidential-client HTTP Basic introspection: 200, active, exact RFC 8707 audience.
- Successor introspection stayed active before and after a fresh SQLite connection.
- Replaying the predecessor returned HTTP 200 and the same successor.
- Two genuinely concurrent independent SQLite connections both returned 200 with access and refresh tokens; both responses contained the same successor, whose introspection was active.
- Explicit revocation returned 200 and subsequent introspection was inactive.
- Plaintext token columns were present, as required by this grace configuration.

SQLite was configured with Django's documented `transaction_mode="IMMEDIATE"` and a 20-second timeout.

## Hash-only strict configuration

Command:

    venv\Scripts\python.exe -u scripts\oauth8_proof\proof.py --grace 0 --skip-migration

The redacted result passed:

- PKCE S256, standard token response, bearer and Basic introspection, exact audience, restart persistence, and explicit revocation.
- Hash-only rows had blank token columns and populated checksum columns.
- Replaying the predecessor returned HTTP 400 `invalid_grant`.

The genuinely concurrent pair produced one successful refresh and one `DoesNotExist` exception from DOT 3.4.1. No SQLite lock exception occurred with `IMMEDIATE`. This is a package concurrency defect requiring resolution before selecting hash-only strict mode for production; the proof does not waive it.

## Grace and hash-storage compatibility

DOT's deployment check rejects a positive grace period with hash-only storage as `oauth2_provider.E001`, explaining that grace must return a previously issued token that is no longer stored in plaintext. The supported tested grace choice is therefore recoverable plaintext token storage with rotation and reuse protection. The hash-only choice is tested only with grace disabled.

## Synthetic legacy checksum migration

Command:

    venv\Scripts\python.exe -u scripts\oauth8_proof\proof.py --migration-only

The proof inserted synthetic native DOT rows with blank token columns and SHA-256 checksums, then verified:

- Imported access-token introspection was HTTP 200 and active.
- Imported refresh succeeded with HTTP 200.
- The successor introspected as active.
- Imported rows retained no plaintext.
- A previously revoked imported refresh returned HTTP 400 `invalid_grant`.

This demonstrates the native DOT checksum field can validate synthetic legacy checksum rows without a second validator or plaintext import. It does not migrate a production database.

## Validation and scope

- `venv\Scripts\ruff.exe check scripts\oauth8_proof\proof.py`: passed.
- `venv\Scripts\python.exe -m py_compile scripts\oauth8_proof\proof.py`: passed.
- Temporary environments and proof databases were cleaned and verified absent.
- No Cognita production source, schema, version, deployment, or production credentials were changed.
