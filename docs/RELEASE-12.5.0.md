# Cognita 12.5.0 release notes

Cognita 12.5.0 restored the stable combined MCP URL
`/mcp/connectors/<slug>/mcp`. It serves the same current v4 catalog as the immutable
`/mcp/connectors/<slug>/mcp/v4` route, while each URL retains its own OAuth resource identity.
Admin displays and copies both URLs.

Retired combined `/v1`–`/v3`, future generations, malformed paths, query aliases, and trailing
slashes continue to fail closed. Workspace-only routes keep their independent policy.

This release retains Admin-owned AMD acceleration, offline GPU OCR, and the Workspace runtime
from 12.4.0. There is no connector schema-generation change, data migration, or Knowledge re-index
operation.
