"""The operation log behind operation_id (5.2, review finding C6)."""

from cognita import idempotency
from cognita.idempotency import (
    MAX_OPERATION_ID_LEN,
    OperationLog,
    normalize_operation_id,
    operation_request_digest,
    with_operation_id_argument,
)


def test_remembers_and_returns_the_stored_payload():
    log = OperationLog()
    assert log.get("P", "update_document", "op1") is None
    log.remember("P", "update_document", "op1", {"status": "success", "chunks_added": 3})
    assert log.get("P", "update_document", "op1")["chunks_added"] == 3


def test_scoped_per_project_and_per_tool():
    """Two projects may legitimately use the same id, and the same id on a
    different tool is a different operation."""
    log = OperationLog()
    log.remember("A", "update_document", "op1", {"who": "A"})
    assert log.get("B", "update_document", "op1") is None
    assert log.get("A", "add_document", "op1") is None
    assert log.get("A", "update_document", "op1") == {"who": "A"}


def test_connector_scope_and_request_digest_prevent_cross_connector_replay():
    log = OperationLog()
    message = {"method": "tools/call", "id": 1,
               "params": {"name": "update_document", "arguments": {"x": "a"}}}
    digest = operation_request_digest(message)
    log.remember("connector-a", "P", "update_document", "op1", {"who": "A"}, digest)
    assert log.lookup("connector-a", "P", "update_document", "op1", digest) == (
        "replay", {"who": "A"}
    )
    assert log.lookup("connector-b", "P", "update_document", "op1", digest) == (
        "missing", None
    )
    changed = {**message, "params": {"name": "update_document",
                                      "arguments": {"x": "b"}}}
    assert log.lookup("connector-a", "P", "update_document", "op1",
                      operation_request_digest(changed)) == ("conflict", None)


def test_operation_digest_ignores_request_id_and_transport_metadata():
    first = {
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {
            "name": "remove_document",
            "arguments": {"filepath": "alpha.txt", "delete_file": True},
            "_meta": {"progressToken": "attempt-one"},
        },
    }
    retry = {
        "jsonrpc": "2.0", "id": 2, "method": "tools/call",
        "params": {
            "name": "remove_document",
            "arguments": {"filepath": "alpha.txt", "delete_file": True},
            "_meta": {"progressToken": "attempt-two"},
        },
    }
    assert operation_request_digest(first) == operation_request_digest(retry)

    changed = {
        **retry,
        "params": {
            **retry["params"],
            "arguments": {"filepath": "alpha.txt", "delete_file": False},
        },
    }
    assert operation_request_digest(first) != operation_request_digest(changed)


class _FakeTime:
    """Stands in for the `time` module inside `cognita.idempotency` only."""

    def __init__(self):
        self.now = 1_000.0

    def monotonic(self) -> float:
        return self.now


def test_entries_expire(monkeypatch):
    # The TTL is measured on an injected clock: the old real 50 ms TTL and
    # 80 ms sleep failed whenever the machine stalled between remember and the
    # first get. The entry now expires exactly when the test moves the clock.
    clock = _FakeTime()
    monkeypatch.setattr(idempotency, "time", clock)
    log = OperationLog(ttl_s=0.05)
    log.remember("P", "t", "op1", {"status": "success"})
    assert log.get("P", "t", "op1") is not None
    clock.now += 0.08
    assert log.get("P", "t", "op1") is None


def test_bounded_so_a_client_cannot_grow_it_without_limit():
    log = OperationLog(max_entries=10)
    for i in range(50):
        log.remember("P", "t", f"op{i}", {"i": i})
    assert len(log._done) <= 10


def test_normalize_accepts_absent_and_blank_as_not_supplied():
    """Absent is the ordinary case, not an error — the argument is optional."""
    for raw in (None, "", "   "):
        value, err = normalize_operation_id(raw)
        assert value is None and err is None


def test_normalize_refuses_a_non_string_or_overlong_id():
    value, err = normalize_operation_id(12345)
    assert value is None and err["reason"] == "invalid"

    value, err = normalize_operation_id("x" * (MAX_OPERATION_ID_LEN + 1))
    assert value is None and err["reason"] == "invalid"


def test_normalize_trims():
    assert normalize_operation_id("  op-1  ") == ("op-1", None)


def test_with_operation_id_argument_does_not_mutate_the_original():
    """The tool defs are module-level constants shared by every request; adding
    a property in place would leak into the read-only surface too."""
    original = {"name": "update_document",
                "inputSchema": {"type": "object", "properties": {"filepath": {"type": "string"}},
                                "required": ["filepath"]}}
    augmented = with_operation_id_argument(original)
    assert "operation_id" in augmented["inputSchema"]["properties"]
    assert "operation_id" not in original["inputSchema"]["properties"]
    # and it stays OPTIONAL — a required argument would be a breaking wire change
    assert augmented["inputSchema"]["required"] == ["filepath"]
