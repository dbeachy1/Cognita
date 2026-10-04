"""Ctrl+C shutdown wiring (see __main__._install_shutdown).

Regression guard for the "first Ctrl+C does nothing, second one works" bug:
uvicorn installs its OS signal handler as `server.handle_exit` (via
capture_signals), NOT via install_signal_handlers — so we must override
handle_exit, and we must set force_exit too (a graceful shutdown blocks on the
long-lived MCP SSE stream).
"""

import asyncio
import inspect

import uvicorn

from cognita.__main__ import _install_shutdown


class _FakeServer:
    def __init__(self):
        self.should_exit = False
        self.force_exit = False


def test_one_signal_stops_all_servers_gracefully():
    a, b = _FakeServer(), _FakeServer()
    stopping = asyncio.Event()
    _install_shutdown((a, b), stopping)

    # Both servers must share the same handler bound to handle_exit (the name
    # capture_signals installs) — one press stops BOTH, not just one.
    assert a.handle_exit is b.handle_exit

    a.handle_exit(2, None)  # single SIGINT
    for s in (a, b):
        assert s.should_exit is True
        assert s.force_exit is False  # graceful first (bounded by timeout)
    assert stopping.is_set()


def test_second_signal_escalates_to_force():
    a, b = _FakeServer(), _FakeServer()
    stopping = asyncio.Event()
    _install_shutdown((a, b), stopping)
    a.handle_exit(2, None)  # first press: graceful
    a.handle_exit(2, None)  # second press: force
    for s in (a, b):
        assert s.force_exit is True


def test_uvicorn_still_installs_handle_exit():
    """If uvicorn ever renames/relocates its signal hook, this fails loudly —
    our override of `handle_exit` would silently stop working otherwise."""
    src = inspect.getsource(uvicorn.Server.capture_signals)
    assert "self.handle_exit" in src
