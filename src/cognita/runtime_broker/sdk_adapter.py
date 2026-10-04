"""Async official Microsandbox SDK boundary.

The Workspace broker does not invoke the ``msb`` CLI.  ``sdk_adapter_v2`` is
the sole runtime contract; this module is the import path the broker, the
compatibility shim in ``upstream.py`` and the typed fakes in the tests use, and
it re-exports that implementation unchanged.

Superseded history: through 12.x this file also carried a complete earlier
implementation -- its own ``PINNED_*`` constants, ``RuntimeAdapter`` protocol,
``UnavailableAdapter``, ``pinned_sdk_info``, ``_sdk_types`` and
``MicrosandboxSdkAdapter`` -- with the re-export below appended after it.  The
re-export won, so every one of those definitions was dead and Python only ever
bound the v2 objects; one of them (``copy_to_host``) had even come adrift of its
class and sat at module level with an indented body.  13.0 deletes the dead
copy rather than leaving two implementations where a reader has to work out
which one runs.  Nothing imported a name that is not in the list below.
"""

from __future__ import annotations

from .sdk_adapter_v2 import (  # noqa: F401 - re-exported on purpose; see the docstring
    ExecResult,
    MicrosandboxSdkAdapter,
    PINNED_RUNTIME_ROOT,
    PINNED_SDK_VERSION,
    PINNED_WHEEL_FILENAME,
    PINNED_WHEEL_SHA256,
    RuntimeAdapter,
    SdkOperationError,
    SdkRuntimeInfo,
    UnavailableAdapter,
    _local_pull_policy,
    _sdk_types,
    operation_context,
    pinned_sdk_info,
    verify_runtime_selection,
)

__all__ = [
    "ExecResult",
    "MicrosandboxSdkAdapter",
    "PINNED_RUNTIME_ROOT",
    "PINNED_SDK_VERSION",
    "PINNED_WHEEL_FILENAME",
    "PINNED_WHEEL_SHA256",
    "RuntimeAdapter",
    "SdkOperationError",
    "SdkRuntimeInfo",
    "UnavailableAdapter",
    "_local_pull_policy",
    "_sdk_types",
    "operation_context",
    "pinned_sdk_info",
    "verify_runtime_selection",
]
