from types import SimpleNamespace

from cognita.watcher import _READ_ONLY_EVENTS, WatcherManager


def test_watcher_keeps_read_only_events_quiet():
    assert "opened" in _READ_ONLY_EVENTS and "closed_no_write" in _READ_ONLY_EVENTS


def test_unwatch_discards_path_bound_asset_service():
    manager = WatcherManager(
        SimpleNamespace(exclude_patterns=[], sync_conflict_patterns=[]),
        asset_services={"TheLargerVoice": object()},
    )
    manager.unwatch("TheLargerVoice")
    assert "TheLargerVoice" not in manager.asset_services
