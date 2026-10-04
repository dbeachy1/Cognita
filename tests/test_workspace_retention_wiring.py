"""Bounded production retention pass wiring without a live broker."""

import asyncio

from cognita.__main__ import _workspace_retention_loop


def test_retention_loop_runs_startup_pass_and_stops_cleanly():
    stopping = asyncio.Event()
    calls = []

    class Credentials:
        def reconcile_tombstones(self, manager, **kwargs):
            calls.append(("credentials", kwargs))
            return []

    class Manager:
        def cleanup_retention(self, **kwargs):
            calls.append(("workspaces", kwargs))
            stopping.set()
            return {"items": [], "next_cursor": ""}

    asyncio.run(_workspace_retention_loop(Credentials(), Manager(), stopping))
    assert calls == [
        ("credentials", {"limit": 32, "after_credential_id": ""}),
        ("workspaces", {"apply": True, "limit": 32, "after_workspace_id": ""}),
    ]
