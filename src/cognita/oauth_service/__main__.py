"""Run one loopback Uvicorn worker for the Cognita OAuth child."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import os
import sys
from pathlib import Path

import uvicorn

from .asgi import create_application
from .bootstrap import bootstrap_service


def _args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=None)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--parent-pid", type=int, required=True)
    parser.add_argument("--revoke-all-on-start", action="store_true")
    return parser.parse_args()


def _parent_alive(parent_pid: int) -> bool:
    if sys.platform != "win32":
        try:
            os.kill(parent_pid, 0)
            return True
        except OSError:
            return False
    import ctypes
    import ctypes.wintypes

    process_query_limited_information = 0x1000
    handle = ctypes.windll.kernel32.OpenProcess(
        process_query_limited_information, False, parent_pid
    )
    if not handle:
        return False
    try:
        exit_code = ctypes.wintypes.DWORD()
        if not ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return False
        return exit_code.value == 259  # STILL_ACTIVE
    finally:
        ctypes.windll.kernel32.CloseHandle(handle)


async def _watch_parent(server, parent_pid: int):
    while not server.should_exit:
        await asyncio.sleep(0.25)
        if not _parent_alive(parent_pid):
            server.should_exit = True
            return


def main() -> None:
    args = _args()
    ctx = bootstrap_service(args.config)
    application = create_application(ctx)
    if args.revoke_all_on_start:
        from .views import revoke_all_connections

        # This flag is an explicit one-shot emergency action. Let any native DOT
        # failure abort startup so readiness cannot advertise a partially revoked DB.
        try:
            changed = revoke_all_connections()
        except Exception:
            logging.getLogger("cognita.oauth_service").exception(
                "OAuth startup emergency revocation failed"
            )
            raise
        logging.getLogger("cognita.oauth_service").info(
            "OAuth startup emergency revocation completed connections_changed=%d", changed
        )

    async def runner():
        config = uvicorn.Config(
            application,
            host="127.0.0.1",
            port=args.port,
            workers=1,
            log_level="warning",
            timeout_graceful_shutdown=1,
        )
        server = uvicorn.Server(config)
        watcher = asyncio.create_task(_watch_parent(server, args.parent_pid))
        try:
            await server.serve()
        finally:
            watcher.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await watcher

    asyncio.run(runner())


if __name__ == "__main__":
    main()
