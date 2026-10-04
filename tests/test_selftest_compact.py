"""Current self-test catalog and opt-in collection projection contracts."""

import asyncio
import json

from cognita import __version__
from cognita.assets.service import AssetService
from cognita.selftest import ASSET_TEST_DATA_URL, select_self_test_plan


def test_self_test_index_and_exact_sections_share_plan_wording():
    index = select_self_test_plan(__version__, False, "index")
    ids = {item["id"] for item in index["sections"]}
    assert {"B", "B1", "B4", "R3"} <= ids
    assert {str(step) for step in range(1, 52)} | {"D", "E", "GL", "GL1", "GL8"} <= ids
    assert len(ids) == len(index["sections"])
    plan = select_self_test_plan(__version__, False, "full")["plan"]
    assert "GL1. GLOB SEMANTICS." in plan
    assert "G1. GLOB SEMANTICS." not in plan
    # Step 51 names the pack DIRECTORY; the old 'cognita-selftest-' string was
    # not a directory and stranded the pack on every literal run (13.0.2).
    assert "51. CLEANUP: remove_directory prefix='cognita-selftest-pack' delete_files=true" in plan
    assert "prefix='cognita-selftest-' delete_files" not in plan
    assert "list_documents prefix='cognita-selftest-pack' — expect 0 documents" in plan
    assert "D1." in select_self_test_plan(__version__, False, "D")["instructions"]
    assert "E1. REASON ON EVERY ERROR PATH" in select_self_test_plan(__version__, False, "E")["instructions"]
    assert "GL2. A ZERO IS DIAGNOSABLE" in select_self_test_plan(__version__, False, "GL")["instructions"]
    b1 = select_self_test_plan(__version__, False, "B1")
    b4 = select_self_test_plan(__version__, False, "B4")
    assert b1["plan_version"] == b4["plan_version"] == __version__
    assert "SHARED SAFETY INSTRUCTIONS" in b1["instructions"]
    assert "YQ0KYg0K" in b1["instructions"]
    assert "YQ0KYg0K" in b4["instructions"]
    assert b1["cleanup_ids"] == ["B5"]


def test_step49_scopes_exact_receipts_without_discarding_history():
    plan = select_self_test_plan(__version__, False, "full")["plan"]
    step = select_self_test_plan(__version__, False, "49")["instructions"]
    wording = step[step.index("49. REMOVE_DIRECTORY"):]
    assert wording in plan
    for assertion in (
        "(filepath, backup_id) pairs as BEFORE",
        "AFTER minus BEFORE",
        "equal EXACTLY RECEIPTS",
        "all 5 receipt paths present",
        "documents_removed 5",
        "files_deleted 5",
        "a backups array with 5 entries",
        "pruned_directories",
        "expect not_found (disk gone)",
        "FAILS if the not_found arrives inside a success envelope",
        "Missing or extra NEW receipts are a",
        "never delete historical backups",
        "time bounds alone do not identify the operation",
    ):
        assert assertion in wording
    assert "EXACTLY the same 5 backup_ids" not in wording


def test_self_test_unknown_section_reports_stable_valid_ids():
    result = select_self_test_plan(__version__, False, "not-a-section")
    assert result["status"] == "error"
    assert result["reason"] == "unknown_section"
    assert "B1" in result["valid_sections"]


def test_summary_projection_is_smaller_and_keeps_order(tmp_path):
    class Project:
        name = "test"
        documents_dir = tmp_path
        data_dir = tmp_path / "data"

    async def run():
        service = AssetService(Project())
        for index, name in enumerate(("b.png", "a.png")):
            await service.put_asset({
                "filepath": "art/" + name,
                "image": {"image_url": ASSET_TEST_DATA_URL},
                "metadata": {
                    "title": "A title " + str(index),
                    "description": "large description " * 20,
                    "alt_text": "alt text " * 20,
                    "prompts": {"user": "prompt " * 20},
                },
                "operation_id": "compact-" + str(index),
            })
        full = await service.list_assets({"detail": "full"})
        compact = await service.list_assets({"detail": "summary"})
        assert [row["filepath"] for row in full["assets"]] == [row["filepath"] for row in compact["assets"]]
        assert len(json.dumps(compact, separators=(",", ":"))) < len(json.dumps(full, separators=(",", ":")))
        assert all("description" not in row and "provenance_state" not in row for row in compact["assets"])
        assert {"mime_type", "final_size", "final_sha256"} <= compact["assets"][0].keys()
        # 13.0.2: a listing is not a search — neither projection carries the
        # search-only fields (full leaked score 1.0 / "keyword" through 13.0.1).
        for listing in (full, compact):
            assert all("score" not in row and "search_method" not in row for row in listing["assets"])
        searched = await service.search_assets({"query": "title", "hybrid_alpha": 0})
        assert searched["results"] and all({"score", "search_method"} <= row.keys() for row in searched["results"])

    asyncio.run(run())
