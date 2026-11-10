# Cognita 16.4.0 release notes

## Workspace runtime reconciliation

Reconciliation now recognizes terminal stopped and crashed sandboxes when the broker confirms the canonical Workspace volume name and sandbox identity, even if the SDK does not provide a volume ID. It also recognizes broker-verified volume-only remnants when the SDK cannot report the host backing path. Mismatched identities remain counted.

The combined MCP contract remains v7 and the Workspace-only contract remains v4.
