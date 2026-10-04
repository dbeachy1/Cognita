"""Private Cognita Workspace runtime broker.

This package is deliberately independent of Cognita's public authentication,
Knowledge engine, and project/config stores.  The HTTP application is intended
to run only on the Compose-internal network.
"""

from .app import create_app
from .jobs import OutputPage, OutputRing, process_identity_is_current
from .network import BraveSearchService, NetworkPolicy, NetworkRule
from .protocol import (
    BROKER_PROTOCOL_VERSION,
    BrokerOperation,
    ErrorCode,
    RpcRequest,
)
from .state import JobRecord, RuntimeStateStore, WorkspaceRecord
from .sdk_adapter import MicrosandboxSdkAdapter
from .validation import ResourcePolicy, normalize_guest_path, normalize_rpc_arguments

__all__ = [
    "BROKER_PROTOCOL_VERSION",
    "BrokerOperation",
    "ErrorCode",
    "RpcRequest",
    "create_app",
    "BraveSearchService",
    "NetworkPolicy",
    "NetworkRule",
    "OutputPage",
    "OutputRing",
    "process_identity_is_current",
    "JobRecord",
    "RuntimeStateStore",
    "WorkspaceRecord",
    "MicrosandboxSdkAdapter",
    "ResourcePolicy",
    "normalize_guest_path",
    "normalize_rpc_arguments",
]
