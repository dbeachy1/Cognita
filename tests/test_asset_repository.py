from cognita.assets.repository import asset_ddl, schema_for


def sql_only(ddl: str) -> str:
    """The DDL with its `--` comment lines removed.

    The comments deliberately name the 9.0 migration and the meta table that
    13.0 deleted, so an assertion that they are gone has to look at what
    Postgres will run rather than at the paragraph explaining why it will not.
    """
    return "\n".join(
        line for line in ddl.splitlines() if not line.lstrip().startswith("--")
    )


def test_asset_ddl_is_additive_and_schema_qualified():
    ddl = asset_ddl("KEI")
    assert schema_for("KEI") == "proj_KEI"
    assert "asset_operations" in ddl and "asset_chunks" in ddl and "ON DELETE CASCADE" in ddl
    assert "connector_id text NOT NULL" in ddl
    assert "PRIMARY KEY (connector_id, tool, operation_id)" in ddl


def test_asset_ddl_carries_no_version_marker_and_no_migration():
    """13.0 §4.1: the per-project version table and the 9.0 DO migration are gone.

    They were not merely redundant once `public.cognita_metadata` existed: an
    older image that still knew `asset_schema_meta` could run its own migration
    against a database this image had stamped, so both halves had to go together.
    """
    ddl = sql_only(asset_ddl("KEI"))
    assert "asset_schema_meta" not in ddl
    assert "schema_version" not in ddl
    assert "DO $$" not in ddl
    assert "ALTER TABLE" not in ddl
    # The legacy connector id survives only as the comment explaining the
    # column's history — never as an UPDATE that rewrites existing rows.
    assert "UPDATE" not in ddl
    assert "__legacy_asset_connector__" not in ddl
