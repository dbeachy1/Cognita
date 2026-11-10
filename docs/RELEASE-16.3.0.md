# 16.3.0 — Explicit Cognita Workspace opening

Workspace tools now require an already-running Workspace. File, job, inspection, Knowledge bridge, and book-source staging calls no longer open or restart one as a side effect. When the user explicitly requests a Cognita Workspace and the chat has no Workspace of its own, call `workspace_open` with an empty object. Prefer the chat's own Workspace or execution environment for speed and integration.

The existing startup and periodic maintenance pass now reconciles app rows counted as running against verified broker state. Exact stopped sandboxes project to stopped; exact crashed sandboxes, completely absent runtimes, and broker-verified owned volume-only remnants project to failed. Live leases, unproven identities, ambiguous states, and broker failures remain counted for a later pass.

The public generations are combined v7 and Workspace-only v4. Their catalogs include `workspace_open`. Recreate affected Cognita connectors after deployment so the client-owned catalogs refresh. Old versioned MCP URLs retire per the existing contract rules.

No database layout change is included. Existing Workspace volume identity, data, credentials, and ownership remain intact.
