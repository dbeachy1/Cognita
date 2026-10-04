"""The 13.0 §4.1 database schema-version check, against a real PostgreSQL.

Five cases, one per branch of `Store._check_schema_version`: fresh, equal,
newer, older, and unversioned-but-populated. Each mismatch case asserts three
things, and the third is the point of the feature:

1. `connect()` raises `SchemaVersionMismatch`;
2. the message names BOTH versions (where there is a stored one) and the exact
   reset command, so the user can act on it without reading the source;
3. **nothing changed** — the schema list and the metadata row are identical
   before and after. "Nothing was changed" is a promise in the message text; a
   test that only checked the exception would let the promise rot.

Activated by COGNITA_TEST_PG_DSN like the other integration suites, and skipped
everywhere else. Every case runs in its OWN throwaway database, created and
dropped here: the marker is per-database, so a shared one could not express
"fresh" at all and the sibling suites' project schemas would make every case
look populated.

    COGNITA_TEST_PG_DSN=postgresql://cognita:...@127.0.0.1:55432/cognita \
        .venv/bin/python -m pytest tests/test_schema_version.py -v
"""

import os
import uuid

import asyncpg
import pytest

from cognita.release_identity import DATABASE_SCHEMA_VERSION
from cognita.store import (
    METADATA_RELATION,
    SchemaVersionMismatch,
    Store,
    reset_command_for,
    schema_for,
)

DSN = os.environ.get("COGNITA_TEST_PG_DSN", "")
pytestmark = pytest.mark.skipif(not DSN, reason="COGNITA_TEST_PG_DSN not set")

DIMS = 8


def dsn_for_database(name: str) -> str:
    """Rewrite the configured DSN's database name, keeping host/user/password."""
    base, _, _ = DSN.rpartition("/")
    return f"{base}/{name}"


@pytest.fixture
async def blank_db():
    """An empty database of its own, dropped afterwards.

    CREATE/DROP DATABASE cannot run inside a transaction block, so they go out
    over a plain connection to the configured database.
    """
    name = f"cognita_sv_{uuid.uuid4().hex[:12]}"
    admin = await asyncpg.connect(DSN)
    try:
        await admin.execute(f'CREATE DATABASE "{name}"')
    finally:
        await admin.close()
    try:
        yield dsn_for_database(name)
    finally:
        admin = await asyncpg.connect(DSN)
        try:
            # Anything still holding the database open would fail the DROP;
            # every Store in these tests is closed by then, so this is belt and
            # braces against a failed assertion leaving a pool behind.
            await admin.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = $1 AND pid <> pg_backend_pid()",
                name,
            )
            await admin.execute(f'DROP DATABASE IF EXISTS "{name}"')
        finally:
            await admin.close()


async def schema_names(dsn: str) -> list[str]:
    """Every non-system schema, the observable "did any DDL run?" fingerprint."""
    conn = await asyncpg.connect(dsn)
    try:
        rows = await conn.fetch(
            "SELECT nspname FROM pg_namespace "
            "WHERE nspname NOT LIKE 'pg\\_%' AND nspname <> 'information_schema' "
            "ORDER BY nspname"
        )
        return [r["nspname"] for r in rows]
    finally:
        await conn.close()


async def stored_version(dsn: str) -> int | None:
    conn = await asyncpg.connect(dsn)
    try:
        if await conn.fetchval("SELECT to_regclass($1)", METADATA_RELATION) is None:
            return None
        return await conn.fetchval(
            f"SELECT schema_version FROM {METADATA_RELATION} WHERE singleton"
        )
    finally:
        await conn.close()


async def stamp(dsn: str, version: int) -> None:
    """Write a version marker by hand — the only way to fake another image."""
    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute(
            f"""CREATE TABLE {METADATA_RELATION} (
                  singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
                  schema_version integer NOT NULL CHECK (schema_version > 0),
                  stamped_at timestamptz NOT NULL DEFAULT now());
                INSERT INTO {METADATA_RELATION} (singleton, schema_version)
                  VALUES (true, {int(version)});"""
        )
    finally:
        await conn.close()


async def build_pre_13_project(dsn: str, project: str) -> None:
    """A populated database with no marker: the FIRST 13.0 deploy on main.

    Deliberately built by hand rather than by calling ensure_project, because
    this image can no longer produce an unversioned database — which is exactly
    why the case has to be constructed to be tested at all.
    """
    conn = await asyncpg.connect(dsn)
    try:
        s = f'"{schema_for(project)}"'
        await conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
        await conn.execute(
            f"""CREATE SCHEMA {s};
                CREATE TABLE {s}.documents (
                  doc_id text PRIMARY KEY, source text NOT NULL UNIQUE,
                  category text NOT NULL DEFAULT 'general', format text,
                  keywords text[], content_hash text NOT NULL,
                  file_mtime timestamptz, file_size bigint,
                  indexed_at timestamptz NOT NULL DEFAULT now(),
                  tier text NOT NULL DEFAULT 'embedded', content text);
                INSERT INTO {s}.documents (doc_id, source, content_hash)
                  VALUES ('legacy', 'old.md', 'h');"""
        )
    finally:
        await conn.close()


def assert_actionable(message: str, *, found: int | None, reset_command: str) -> None:
    """Both numbers (where there is a stored one) and the command to fix it."""
    assert str(DATABASE_SCHEMA_VERSION) in message
    if found is None:
        assert "unversioned" in message
    else:
        assert str(found) in message
    assert "Nothing was changed." in message
    assert reset_command in message


# ---------- fresh ----------


async def test_fresh_database_is_created_and_stamped(blank_db):
    assert await stored_version(blank_db) is None

    store = Store(blank_db, embedding_dimensions=DIMS)
    await store.connect()
    try:
        assert store.schema_error is None
        assert await stored_version(blank_db) == DATABASE_SCHEMA_VERSION
        # ...and the store is fully usable, which is the other half of "fresh":
        # the final DDL has to stand on its own with no migration behind it.
        project = f"T{uuid.uuid4().hex[:10]}"
        await store.ensure_project(project)
        assert await store.has_project(project)
    finally:
        await store.close()


async def test_second_start_on_a_stamped_database_proceeds(blank_db):
    """Equal: the ordinary restart, and it must not re-stamp or duplicate."""
    first = Store(blank_db, embedding_dimensions=DIMS)
    await first.connect()
    await first.close()
    before = await schema_names(blank_db)

    second = Store(blank_db, embedding_dimensions=DIMS)
    await second.connect()
    try:
        assert second.schema_error is None
        assert await stored_version(blank_db) == DATABASE_SCHEMA_VERSION
        assert await schema_names(blank_db) == before
    finally:
        await second.close()


# ---------- the three refusals ----------


@pytest.mark.parametrize(
    "found",
    [
        pytest.param(DATABASE_SCHEMA_VERSION + 1, id="newer"),
        pytest.param(DATABASE_SCHEMA_VERSION + 7, id="much_newer"),
    ],
)
async def test_newer_database_is_refused_untouched(blank_db, found):
    await stamp(blank_db, found)
    before = await schema_names(blank_db)

    store = Store(blank_db, embedding_dimensions=DIMS, reset_command=reset_command_for("main"))
    with pytest.raises(SchemaVersionMismatch) as caught:
        await store.connect()

    # The exact wording of design §4.1, asserted as one string: a reworded
    # message is a contract change for whoever reads /healthz.
    message = str(caught.value)
    assert message == (
        f"Database schema {found} is newer than this image supports "
        f"({DATABASE_SCHEMA_VERSION}). Nothing was changed. "
        f"To reset the index: {reset_command_for('main')}"
    )
    assert_actionable(message, found=found, reset_command=reset_command_for("main"))
    assert await schema_names(blank_db) == before
    assert await stored_version(blank_db) == found  # the marker is not rewritten
    assert store.schema_error is caught.value


async def test_older_database_is_refused_untouched(blank_db, monkeypatch):
    """No forward migration: an older stamp is as fatal as a newer one.

    This is the case where the temptation to "just upgrade it" lives, and it is
    the one 13.0 deliberately does not implement.

    The stored version has to be BELOW the image's, and the image's literal is
    currently 1 with a `schema_version > 0` check under it, so there is no
    number to store. Raising the image's idea of its own version is the honest
    way to reach the branch, and it is what a future bump will do for real.
    """
    image_version = DATABASE_SCHEMA_VERSION + 1
    monkeypatch.setattr("cognita.store.DATABASE_SCHEMA_VERSION", image_version)
    await stamp(blank_db, DATABASE_SCHEMA_VERSION)
    before = await schema_names(blank_db)

    store = Store(blank_db, embedding_dimensions=DIMS, reset_command=reset_command_for("main"))
    with pytest.raises(SchemaVersionMismatch) as caught:
        await store.connect()

    message = str(caught.value)
    assert f"Database schema {DATABASE_SCHEMA_VERSION} is older than this image " \
           f"supports ({image_version})." in message
    assert "Nothing was changed." in message
    assert reset_command_for("main") in message
    assert await schema_names(blank_db) == before
    assert await stored_version(blank_db) == DATABASE_SCHEMA_VERSION


async def test_unversioned_but_populated_is_refused_untouched(blank_db):
    """The first 13.0 start on main: a 12.x index with no marker.

    Stamping it would be the dangerous shortcut — the data is not in this
    image's shape — so the store refuses and names the reset command instead.
    """
    project = f"T{uuid.uuid4().hex[:10]}"
    await build_pre_13_project(blank_db, project)
    before = await schema_names(blank_db)
    assert schema_for(project) in before

    store = Store(blank_db, embedding_dimensions=DIMS, reset_command=reset_command_for("main"))
    with pytest.raises(SchemaVersionMismatch) as caught:
        await store.connect()

    message = str(caught.value)
    assert_actionable(message, found=None, reset_command=reset_command_for("main"))
    assert await schema_names(blank_db) == before
    # The marker was NOT created — the database is exactly as it was found, so
    # the reset the message asks for is still the user's own decision.
    assert await stored_version(blank_db) is None


async def test_damaged_marker_on_a_populated_database_is_not_fresh(blank_db):
    """An empty cognita_metadata beside real data must never read as empty.

    An interrupted stamp leaves exactly this. Treating it as fresh would write
    this image's version over a database in someone else's shape — the one
    outcome the whole check exists to prevent.
    """
    project = f"T{uuid.uuid4().hex[:10]}"
    await build_pre_13_project(blank_db, project)
    conn = await asyncpg.connect(blank_db)
    try:
        await conn.execute(
            f"""CREATE TABLE {METADATA_RELATION} (
                  singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
                  schema_version integer NOT NULL CHECK (schema_version > 0),
                  stamped_at timestamptz NOT NULL DEFAULT now());"""
        )
    finally:
        await conn.close()
    before = await schema_names(blank_db)

    store = Store(blank_db, embedding_dimensions=DIMS)
    with pytest.raises(SchemaVersionMismatch):
        await store.connect()
    assert await stored_version(blank_db) is None
    assert await schema_names(blank_db) == before


# ---------- the message itself ----------


async def test_reset_command_falls_back_to_a_placeholder_target(blank_db):
    """The store does not know its deployment target, and must not guess one."""
    await stamp(blank_db, DATABASE_SCHEMA_VERSION + 1)
    store = Store(blank_db, embedding_dimensions=DIMS)
    with pytest.raises(SchemaVersionMismatch) as caught:
        await store.connect()
    message = str(caught.value)
    assert "--target <your target> --scope index --apply" in message
    assert "reset_disposable_state.py" in message


async def test_a_refused_store_refuses_ensure_project_and_the_pool(blank_db):
    """The refusal is sticky: no caller can talk its way past it.

    Admin's add-project calls store.ensure_project directly rather than going
    through the engine's tool dispatch, so the guard has to live here too.
    """
    await stamp(blank_db, DATABASE_SCHEMA_VERSION + 1)
    store = Store(blank_db, embedding_dimensions=DIMS)
    with pytest.raises(SchemaVersionMismatch):
        await store.connect()
    before = await schema_names(blank_db)

    with pytest.raises(SchemaVersionMismatch):
        await store.ensure_project(f"T{uuid.uuid4().hex[:10]}")
    with pytest.raises(SchemaVersionMismatch):
        _ = store.pool
    assert await schema_names(blank_db) == before
