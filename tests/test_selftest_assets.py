import base64
import hashlib

from cognita.selftest import (
    ASSET_REMOVE_DISPOSABLE,
    ASSET_RUN_A,
    ASSET_RUN_B,
    ASSET_TEST_A,
    ASSET_TEST_B,
    ASSET_TEST_DATA_URL,
    ASSET_TEST_SHA256,
    ASSET_TEST_SIZE,
    build_self_test_plan,
)


def test_certified_png_fixture_matches_pinned_facts():
    prefix, encoded = ASSET_TEST_DATA_URL.split(",", 1)
    assert prefix == "data:image/png;base64"
    decoded = base64.b64decode(encoded, validate=True)
    assert decoded.startswith(b"\x89PNG\r\n\x1a\n")
    assert len(decoded) == ASSET_TEST_SIZE
    assert hashlib.sha256(decoded).hexdigest() == ASSET_TEST_SHA256


def test_writable_asset_plan_has_functional_contract_coverage():
    plan = build_self_test_plan("10.1.0", readonly=False)
    assert "These are REQUIRED" in plan
    assert "A1." in plan and "A12." in plan
    assert ASSET_TEST_A in plan and ASSET_TEST_B in plan
    assert ASSET_RUN_A in plan and ASSET_RUN_B in plan
    assert ASSET_REMOVE_DISPOSABLE in plan
    assert ASSET_TEST_DATA_URL in plan
    for tool in (
        "put_asset", "update_asset_metadata", "search_assets", "list_assets",
        "get_asset_info", "get_asset", "reindex_assets", "remove_asset",
    ):
        assert tool in plan
    for contract in (
        "expected_received_size", "expected_received_sha256",
        "overwrite=false", "metadata_revision",
        "idempotent_replay=true", "operation_conflict", "stale_file",
        "size_mismatch", "catalog_drift=false", "hybrid_alpha=0",
        "hybrid_alpha=1", "next_cursor", "image/png block",
        "must never alter PNG bytes", "backup_id", "metadata.asset_id",
        "no_matches", "search-only score", "too_large",
        'project="Self-Test"',
    ):
        assert contract in plan
    asset_plan = plan[plan.index("ASSET CHECKS (10.1;"):]
    assert "historical retained canaries" in asset_plan
    assert "must never modify, reindex, or remove" in asset_plan
    assert "A11. REMOVE A DISPOSABLE ASSET" in asset_plan
    assert "A12. CLEAN UP THIS RUN" in asset_plan
    assert "A, B, and the disposable" in asset_plan
    assert "owned by another run" in asset_plan


def test_readonly_asset_plan_exercises_every_read_endpoint():
    plan = build_self_test_plan("10.1.0", readonly=True)
    for tool in ("search_assets", "list_assets", "get_asset_info", "get_asset"):
        assert tool in plan
    assert "R-A1." in plan and "R-A5." in plan
    assert "catalog_drift=false" in plan and "stale_file" in plan
    for mutating in ("put_asset", "update_asset_metadata", "reindex_assets", "remove_asset"):
        assert mutating not in plan
