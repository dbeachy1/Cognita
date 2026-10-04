"""Unit tests for cognita.store — pure logic, no PostgreSQL required.

The transactional behavior itself is proven against a real server in
tests/test_store_pg.py (set COGNITA_TEST_PG_DSN); these cover the
SQL-identifier safety rails and value encoding that must hold before any
SQL is ever sent.
"""

import pytest

from cognita.release_identity import DATABASE_SCHEMA_VERSION
from cognita.store import (
    DEFAULT_RESET_COMMAND,
    METADATA_RELATION,
    ChunkRecord,
    DocumentRecord,
    Store,
    metadata_ddl,
    parse_vector,
    render_ddl,
    reset_command_for,
    schema_for,
    vector_literal,
)


# ---------- schema_for: the identifier safety rail ----------


def test_schema_for_preserves_case():
    assert schema_for("KEI") == "proj_KEI"
    assert schema_for("ALTEA") == "proj_ALTEA"


def test_schema_for_allows_registry_names():
    # Same NAME_RE as the registry: letters/digits/hyphens, leading alphanumeric.
    assert schema_for("my-proj") == "proj_my-proj"
    assert schema_for("a") == "proj_a"
    assert schema_for("0day") == "proj_0day"


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "-leading-hyphen",
        "has space",
        'x"; DROP SCHEMA public CASCADE; --',
        "a;b",
        'quote"inside',
        "dot.dot",
        "slash/../up",
        "under_score",  # NAME_RE never allowed underscores; keep it that way
    ],
)
def test_schema_for_rejects_unsafe_names(bad):
    with pytest.raises(ValueError):
        schema_for(bad)


# ---------- DDL rendering ----------


def test_render_ddl_embeds_dimensions_and_quoted_schema():
    ddl = render_ddl("KEI", 1024)
    assert '"proj_KEI"' in ddl
    assert "vector(1024)" in ddl
    assert "ON DELETE CASCADE" in ddl
    assert "UNIQUE (doc_id, chunk_index)" in ddl
    assert "GENERATED ALWAYS AS (to_tsvector('english', content)) STORED" in ddl
    assert "hnsw" in ddl and "gin" in ddl


def test_render_ddl_is_idempotent_sql():
    # Every CREATE must be IF NOT EXISTS so ensure_project is safely re-runnable.
    ddl = render_ddl("KEI", 8)
    creates = [line for line in ddl.splitlines() if line.startswith("CREATE")]
    assert creates and all("IF NOT EXISTS" in line for line in creates)


def test_render_ddl_rejects_bad_dimensions():
    with pytest.raises(ValueError):
        render_ddl("KEI", 0)
    with pytest.raises(ValueError):
        render_ddl("KEI", -1)


def test_render_ddl_rejects_bad_project():
    with pytest.raises(ValueError):
        render_ddl('x"; DROP TABLE --', 8)


def test_render_ddl_states_the_final_shape_with_no_migration():
    """13.0 §4.1/§8: no ALTER, no DO block, no information_schema probe.

    The 4.4 tier/content columns and the 4.4.0 tsv rebuild used to arrive after
    the CREATEs. They are part of the CREATE now, because a database that is not
    already this shape is refused rather than migrated — and a leftover
    migration branch is worse than none: an older image could still run it.
    """
    # Executable SQL only: the comments deliberately NAME the deleted branches,
    # so a naive substring search would match its own explanation.
    ddl = sql_only(render_ddl("KEI", 8))
    assert "ALTER TABLE" not in ddl
    assert "DO $$" not in ddl
    assert "DROP COLUMN" not in ddl
    assert "information_schema" not in ddl
    # ...and the columns those ALTERs used to add are in the table itself.
    assert "tier         text NOT NULL DEFAULT 'embedded'" in ddl
    assert "content      text" in ddl
    # The translate-weighted documents.tsv — the 4.4.1 fix — is the shipped one.
    assert "translate(coalesce(source, ''), '/_-.', '    ')" in ddl
    assert "setweight(to_tsvector('english', coalesce(content, '')), 'B')" in ddl


def sql_only(ddl: str) -> str:
    """The DDL with its `--` comment lines removed.

    Comments in this DDL name the migration branches 13.0 deleted, by design —
    an assertion that a branch is gone has to look at what Postgres will run,
    not at the paragraph explaining why it no longer does.
    """
    return "\n".join(
        line for line in ddl.splitlines() if not line.lstrip().startswith("--")
    )


# ---------- the one schema version (13.0 §4.1) ----------


def test_metadata_ddl_stamps_the_image_version_once():
    ddl = metadata_ddl()
    assert METADATA_RELATION == "public.cognita_metadata"
    assert f"CREATE TABLE IF NOT EXISTS {METADATA_RELATION}" in ddl
    assert f"VALUES (true, {DATABASE_SCHEMA_VERSION})" in ddl
    # ON CONFLICT DO NOTHING: a second start never rewrites an existing stamp.
    assert "ON CONFLICT (singleton) DO NOTHING" in ddl


def test_reset_command_names_the_target_or_says_it_cannot():
    assert reset_command_for("main") == (
        "python3 scripts/reset_disposable_state.py --target main --scope index --apply"
    )
    assert reset_command_for(None) == DEFAULT_RESET_COMMAND
    assert "--target <your target>" in DEFAULT_RESET_COMMAND


def test_a_new_store_has_no_schema_error():
    assert Store("postgresql:///nowhere").schema_error is None


# ---------- vector literal encoding ----------


def test_vector_literal_roundtrip():
    vec = [0.5, -1.25, 3.0, 0.0]
    lit = vector_literal(vec, 4)
    assert lit == "[0.5,-1.25,3.0,0.0]"
    assert parse_vector(lit) == vec


def test_vector_literal_rejects_dimension_mismatch():
    with pytest.raises(ValueError):
        vector_literal([1.0, 2.0], 3)


# ---------- Store guards (no server needed) ----------


def test_store_requires_connect():
    with pytest.raises(RuntimeError, match="not connected"):
        _ = Store("postgresql://nowhere/none").pool


def test_store_rejects_bad_dimensions():
    with pytest.raises(ValueError):
        Store("postgresql://nowhere/none", embedding_dimensions=0)


async def test_replace_document_rejects_zero_chunks_before_touching_pool():
    store = Store("postgresql://nowhere/none", embedding_dimensions=8)
    doc = DocumentRecord(doc_id="d1", source="a.md", content_hash="h")
    with pytest.raises(ValueError, match="zero chunks"):
        await store.replace_document("KEI", doc, [])


async def test_replace_document_rejects_bad_project_before_touching_pool():
    store = Store("postgresql://nowhere/none", embedding_dimensions=8)
    doc = DocumentRecord(doc_id="d1", source="a.md", content_hash="h")
    chunk = ChunkRecord(chunk_id="c1", chunk_index=0, content="x", embedding=[0.0] * 8)
    with pytest.raises(ValueError, match="Invalid project name"):
        await store.replace_document('x"; DROP', doc, [chunk])
