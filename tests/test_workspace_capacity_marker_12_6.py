"""Focused marker-only Workspace capacity wiring checks."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from cognita.workspace import MountedFilesystemCapacity
from cognita.workspace_admin import WorkspaceAdminAdapter

ROOT = Path(__file__).parents[1]
MARKER_SOURCE = (
    "${COGNITA_WORKSPACE_DATA_ROOT:?COGNITA_WORKSPACE_DATA_ROOT is required}/"
    ".cognita-12-workspaces.json"
)
MARKER_TARGET = "/run/cognita/workspace-capacity.marker"


def test_marker_probe_stats_filesystem_without_reading_marker(tmp_path: Path, monkeypatch) -> None:
    marker = tmp_path / ".cognita-12-workspaces.json"
    marker.write_text('{"schema": 1}\n', encoding="utf-8")
    calls: list[str] = []

    class Stats:
        f_frsize = 4096
        f_bsize = 4096
        f_blocks = 100
        f_bavail = 25

    monkeypatch.setattr(
        "cognita.workspace.os.statvfs",
        lambda path: calls.append(path) or Stats(),
        raising=False,
    )

    assert MountedFilesystemCapacity(marker).snapshot() == (409_600, 102_400)
    assert calls == [str(marker)]


def test_marker_probe_fails_closed_when_missing_or_not_regular(tmp_path: Path) -> None:
    missing = MountedFilesystemCapacity(tmp_path / "missing-marker")
    with pytest.raises(OSError):
        missing.snapshot()

    directory = tmp_path / "marker-directory"
    directory.mkdir()
    with pytest.raises(OSError):
        MountedFilesystemCapacity(directory).snapshot()


def test_compose_mounts_only_marker_into_cognita_and_disables_creation() -> None:
    core = yaml.safe_load((ROOT / "compose.yaml").read_text(encoding="utf-8"))
    workspace = yaml.safe_load((ROOT / "compose.workspace.yaml").read_text(encoding="utf-8"))
    cognita_mounts = workspace["services"]["cognita"]["volumes"]
    marker = next(item for item in cognita_mounts if item.get("target") == MARKER_TARGET)

    assert marker["source"] == MARKER_SOURCE
    assert marker["read_only"] is True
    assert marker["bind"]["create_host_path"] is False
    assert not any(
        item.get("source", "").startswith("${COGNITA_WORKSPACE_DATA_ROOT")
        and item.get("target") != MARKER_TARGET
        for item in cognita_mounts
    )
    assert not any(
        item.get("target") == MARKER_TARGET
        for item in core["services"]["cognita"]["volumes"]
    )
    runtime_mounts = workspace["services"]["workspace-runtime"]["volumes"]
    assert any(item.get("target") == "/root/.microsandbox" for item in runtime_mounts)


def test_admin_projects_domain_capacity_without_host_root_access() -> None:
    manager = SimpleNamespace(
        metadata=SimpleNamespace(held_growth_bytes=lambda: 0),
        host_reserve_bytes=200,
        storage_snapshot=lambda: {
            "host_root": "/configured/workspaces",
            "container_root": "/root/.microsandbox",
            "filesystem_capacity_bytes": 1000,
            "filesystem_free_bytes": 500,
            "reserve_bytes": 200,
            "admissible_free_bytes": 300,
            "workspace_allocated_bytes": 40,
            "workspace_apparent_bytes": 60,
            "measured_at": "2026-09-19T00:00:00+00:00",
            "measurement_status": "fresh",
            "measurement_reason": None,
        }
    )
    adapter = WorkspaceAdminAdapter.__new__(WorkspaceAdminAdapter)
    adapter.manager = manager
    adapter.host_root = "/configured/workspaces"
    adapter.container_root = "/root/.microsandbox"

    snapshot = adapter._storage_snapshot([])

    assert snapshot["filesystem_capacity_bytes"] == 1000
    assert snapshot["filesystem_free_bytes"] == 500
    assert snapshot["admissible_free_bytes"] == 300
    assert snapshot["measurement_source"] == "mounted filesystem marker"


def test_storage_snapshot_includes_newer_active_growth_accounting(tmp_path: Path) -> None:
    from cognita.workspace import WorkspaceManager, WorkspaceMetadataStore

    store = WorkspaceMetadataStore(tmp_path / "workspace.sqlite3")
    try:
        provider = SimpleNamespace(root=str(tmp_path / "marker"), snapshot=lambda: (1000, 900))
        manager = WorkspaceManager(
            store,
            capacity_provider=provider,
            strict_capacity=True,
            host_reserve_bytes=100,
        )
        manager._active_potential_growth = lambda: 200

        assert manager.storage_snapshot().admissible_free_bytes == 600
    finally:
        store.close()
