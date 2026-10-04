"""Current self-test instructions are executable against the emitted catalog."""

from __future__ import annotations

import base64
import hashlib
import re

from cognita import __version__
from cognita.engine_local import ENGINE_TOOL_DEFS
from cognita.proxy import public_tool_catalog
from cognita.selftest import (
    SELF_TEST_CRLF_BASE64,
    SELF_TEST_CRLF_BYTES,
    SELF_TEST_CRLF_SHA256,
    SELF_TEST_PLAN_VERSION,
    build_self_test_plan,
)


def _schema(name: str) -> dict:
    return next(item for item in public_tool_catalog() if item["name"] == name)


def test_b1_and_b4_encoded_reads_use_get_document_and_actual_schema() -> None:
    plan = build_self_test_plan("10.1.0", readonly=False)
    b1 = plan[plan.index("B1."):plan.index("B2.")]
    b4 = plan[plan.index("B4."):plan.index("B4b.")]

    # This guards the emitted instructions, not a duplicate expected string.
    assert "then get_document" in b1
    assert "then get_document" in b4
    assert (
        f"add_document filepath='cognita-selftest-bytes.md' content='{SELF_TEST_CRLF_BASE64}'"
        "\n    content_encoding='base64', then get_document filepath='cognita-selftest-bytes.md'"
        "\n    content_encoding='base64'"
    ) in b1
    assert (
        f"content='{SELF_TEST_CRLF_BASE64}', content_encoding='base64', then get_document it with"
        "\n    content_encoding='base64'"
    ) in b4
    assert not re.search(r"read_document[^\n]*content_encoding", b1 + b4)
    get_schema = _schema("get_document")["inputSchema"]
    read_schema = _schema("read_document")["inputSchema"]
    assert get_schema["properties"]["content_encoding"]["enum"] == ["utf-8", "base64"]
    assert "content_encoding" not in read_schema["properties"]

    add_schema = _schema("add_document")["inputSchema"]
    assert add_schema["properties"]["content_encoding"]["enum"] == ["utf-8", "base64"]
    assert {"content", "filepath"}.issubset(add_schema["required"])


def test_encoded_selftest_fixture_is_exact_six_byte_round_trip() -> None:
    raw = base64.b64decode(SELF_TEST_CRLF_BASE64, validate=True)
    assert raw == SELF_TEST_CRLF_BYTES
    assert len(raw) == 6
    assert hashlib.sha256(raw).hexdigest() == SELF_TEST_CRLF_SHA256


def test_10_catalog_is_authoritative_for_prescribed_encoded_tools() -> None:
    catalog = public_tool_catalog()
    assert catalog
    names = {item["name"] for item in catalog}
    assert {"add_document", "get_document", "read_document", "ocr_asset"}.issubset(names)
    # The current engine definition is the source used to emit the public
    # catalog; this prevents a stale hand-maintained test schema from passing.
    engine_get = next(item for item in ENGINE_TOOL_DEFS if item["name"] == "get_document")
    emitted_get = _schema("get_document")
    assert emitted_get["inputSchema"]["properties"]["content_encoding"] == engine_get["inputSchema"]["properties"]["content_encoding"]


def test_selftest_plan_reports_distinct_contract_failure_classes_and_refresh_limits() -> None:
    plan = build_self_test_plan("10.1.0", readonly=False)
    for classification in ("client_contract_mismatch", "plan_contract_mismatch", "server_execution_failure"):
        assert classification in plan
    assert "UNVERIFIED" in plan
    assert "Cognita cannot invalidate a\nclient-owned cache" in plan
    assert "SUPPORTED DISCOVERY REFRESH" in plan
    assert "recreate the client connector with the canonical V3 URL" in plan
    assert "There is no server-side\nin-place refresh operation" in plan
    assert SELF_TEST_PLAN_VERSION == __version__
