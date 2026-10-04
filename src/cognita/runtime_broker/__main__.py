"""Executable entry point for the internal ``workspace-runtime`` container."""

from __future__ import annotations

import os

import uvicorn

from .app import create_app
from .journal import RequestJournal
from .state import RuntimeStateStore
from .sdk_adapter import MicrosandboxSdkAdapter


def main() -> None:
    journal_path = os.environ.get(
        "COGNITA_BROKER_JOURNAL_PATH",
        "/root/.microsandbox/cognita-broker-requests.sqlite3",
    )
    state_path = os.environ.get(
        "COGNITA_BROKER_STATE_PATH",
        "/root/.microsandbox/cognita-broker-state.sqlite3",
    )
    app = create_app(
        adapter=MicrosandboxSdkAdapter(
            image=os.environ.get("COGNITA_WORKSPACE_TOOLBOX_IMAGE", "cognita-workspace-toolbox:12.6.0"),
            timeout_seconds=float(os.environ.get("COGNITA_MSB_TIMEOUT_SECONDS", "600")),
        ),
        journal=RequestJournal(journal_path),
        state_store=RuntimeStateStore(state_path),
        startup_probe=True,
    )
    uvicorn.run(
        app,
        host=os.environ.get("COGNITA_RUNTIME_BROKER_HOST", "0.0.0.0"),
        port=int(os.environ.get("COGNITA_RUNTIME_BROKER_PORT", "8080")),
        log_config=None,
    )


if __name__ == "__main__":
    main()
