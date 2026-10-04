"""One-shot SDK readiness probe used by host preflight."""

from __future__ import annotations

import asyncio
import os

from .sdk_adapter import MicrosandboxSdkAdapter


async def _main() -> None:
    adapter = MicrosandboxSdkAdapter(
        image=os.environ.get("COGNITA_WORKSPACE_TOOLBOX_IMAGE", "cognita-workspace-toolbox:12.6.0"),
        timeout_seconds=120,
    )
    await adapter.readiness_probe()


if __name__ == "__main__":
    asyncio.run(_main())
