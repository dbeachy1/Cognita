"""13.2.6: the first call after a deploy works for a workspace that already existed.

DESIGN-13.2-CONNECTOR-DIAGNOSTICS §6. Every deploy restarts the workspace broker
and its generation moves, so the first call for every pre-existing workspace is
rejected with generation_conflict. The job and fs paths already retried once;
admission (and workspace_info) did not, and the bridge turned that rejection
into ``internal_error: Bridge operation failed``. Prod, 2026-09-23 04:40Z, the
first copy_to_workspace after the 13.2.5 deploy. Doug: "I shouldn't be finding
this in production."
"""

from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from cognita.bridge import BridgeService, bridge_tool_result
from cognita.connectors import ConnectorDefinition
from cognita.workspace import WorkspaceError, WorkspaceManager, WorkspaceMetadataStore


class _Principal:
    principal_id = "11111111-1111-4111-8111-111111111111"
    surface_id = "surface-1"


class _RestartedBroker:
    """Answers like the real broker after a restart: the first inspect that
    carries the old generation is rejected; anything after the reset passes."""

    def __init__(self):
        self.inspects = 0
        self.rejections = 0
        self.calls: list[str] = []

    def call(self, workspace_id, operation, arguments, **kwargs):
        self.calls.append(operation)
        if operation == "inspect":
            self.inspects += 1
            if kwargs.get("expected_runtime_generation"):
                self.rejections += 1
                raise WorkspaceError("generation_conflict", "runtime changed",
                                     reset_runtime_generation=True)
            return {"state": "running"}
        if operation == "fs_usage":
            return {"entries": [], "truncated": False, "total_bytes": 0}
        return {"ok": True}


def _existing_running_workspace(tmp_path: Path):
    store = WorkspaceMetadataStore(tmp_path / "workspace.sqlite3")
    row = store.create(_Principal.principal_id, "surface-1", "Workspace", now="2026-09-22T00:00:00+00:00",
                       quota_bytes=4 * 1024**3, retention_days=7)
    # The row last talked to the broker at generation 8; the broker is now at 9.
    store.update(row.workspace_id, state="running", desired_state="running", runtime_generation=8)
    return store


def test_admission_retries_once_after_a_broker_restart(tmp_path: Path, caplog):
    store = _existing_running_workspace(tmp_path)
    try:
        broker = _RestartedBroker()
        manager = WorkspaceManager(store, broker)
        with caplog.at_level(logging.INFO, logger="cognita.workspace"):
            result = manager.execute(_Principal(), "workspace_make_directory", {"path": "after-deploy"})
        assert result["status"] == "success", result
        # Exactly one rejection, and admission's inspects were "rejected, then
        # retried" (a mutation measures the workspace afterwards: more inspects
        # follow, none rejected).
        assert broker.rejections == 1
        assert broker.calls[:2] == ["inspect", "inspect"]
        row = store.get_by_principal(_Principal.principal_id)
        assert row.state == "running"
        assert any("inspect retried once after broker generation change" in r.getMessage()
                   for r in caplog.records)
        # A second operation carries the current generation: no conflict at all.
        assert manager.execute(_Principal(), "workspace_make_directory", {"path": "again"})["status"] == "success"
        assert broker.rejections == 1
    finally:
        store.close()


def test_workspace_info_is_not_degraded_by_the_first_call_after_a_restart(tmp_path: Path):
    store = _existing_running_workspace(tmp_path)
    try:
        manager = WorkspaceManager(store, _RestartedBroker())
        info = manager.info(_Principal())
        assert info["workspace"]["runtime_available"] is True, info["workspace"]
        assert info["workspace"]["operational_state"] == "running"
    finally:
        store.close()


def test_a_second_conflict_in_a_row_still_propagates(tmp_path: Path):
    store = _existing_running_workspace(tmp_path)
    try:
        class AlwaysConflicting:
            def call(self, workspace_id, operation, arguments, **kwargs):
                raise WorkspaceError("generation_conflict", "runtime changed", reset_runtime_generation=True)

        manager = WorkspaceManager(store, AlwaysConflicting())
        with pytest.raises(WorkspaceError) as failure:
            manager.execute(_Principal(), "workspace_make_directory", {"path": "x"})
        assert failure.value.reason == "generation_conflict"
    finally:
        store.close()


@pytest.mark.asyncio
async def test_bridge_reports_admissions_own_reason_not_internal_error(tmp_path: Path, caplog):
    class RejectingWorkspace:
        def _admit(self, principal, connector_id=None):
            raise WorkspaceError("generation_conflict", "Workspace runtime rejected the operation",
                                 broker_code="generation_conflict", retryable=True)

    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "note.md").write_text("x", encoding="utf-8")
    project = SimpleNamespace(name="Project", documents_dir=docs, enabled=True, writable=True)
    connector = ConnectorDefinition(id=str(uuid4()), name="Bridge", slug="bridge", workspace_enabled=True,
                                    default_workspace_transfer="allow", default_access="write")
    service = BridgeService(RejectingWorkspace(), SimpleNamespace(), staging_root=tmp_path / "staging")
    with caplog.at_level(logging.ERROR, logger="cognita.bridge"):
        result = await bridge_tool_result(
            service, SimpleNamespace(principal_id=str(uuid4())), connector, project,
            "copy_to_workspace", {"project": "Project", "paths": ["note.md"], "destination": "."},
        )
    assert result["status"] == "error"
    assert result["reason"] == "generation_conflict"
    assert result["retryable"] is True
    assert "internal_error" not in str(result)
    assert not any("unexpected bridge failure" in r.getMessage() for r in caplog.records)
