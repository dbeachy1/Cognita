"""The single authority for Cognita's versions and contract generations (13.0 §4).

A leaf module of literals: it imports nothing from the rest of the package, so
anything that displays or checks a version can read it without dragging in the
application. Everything derives from here — ``__version__``, wheel metadata,
``/healthz``, ``initialize``, Admin, self-test, broker health, OCI labels and
image tags.

Move a literal below in the same commit as the change that requires it.
"""

from __future__ import annotations

# The shape of this module itself: which names a reader of a build label, a
# health payload or a release directory can expect to find here. Bump it only
# when a name below is removed or changes meaning.
IDENTITY_SCHEMA = 1

# The application version — what `/healthz`, the wheel metadata, the Admin UI,
# the `initialize` serverInfo, the self-test plan and the image tag all report.
# Move it in the same commit as the change that ships.
APPLICATION_VERSION = "15.3.0"

# The public combined-connector MCP generation, served at
# `/mcp/connectors/<slug>/mcp/v<N>`. Bump ONLY for a client-visible change to
# that contract — a tool's wire shape, the error envelope, the catalog — never
# for an ordinary release; `cognita.connectors` re-exports it under the name
# the rest of the code already uses, PUBLIC_CONTRACT_VERSION.
COMBINED_CONTRACT_VERSION = 5

# The Workspace-only MCP generation, served at `/mcp/workspace/<slug>/mcp/v<N>`.
# Bump ONLY for a client-visible change to the Workspace-only contract. It is
# independent of the combined generation and of the application version.
WORKSPACE_CONTRACT_VERSION = 3

# The complete Cognita PostgreSQL schema — documents, asset catalog and OCR —
# has ONE version, stamped once in ``public.cognita_metadata`` (13.0 §4.1).
# Bump it when any of that DDL changes shape. There is no forward migration:
# an image that finds a different number refuses to touch the database and
# tells the user the exact reset command. See ``cognita.store``.
DATABASE_SCHEMA_VERSION = 1

# The Workspace metadata SQLite layout (``cognita.workspace.WorkspaceMetadataStore
# .SCHEMA_SQL``), stamped in that file's ``workspace_schema`` row (13.2.0). Bump it
# when that DDL changes shape, in the same commit. A file at this number is left
# alone; a file with no stamp is judged by its columns (13.1.0 and earlier wrote
# none); a file at a higher number was written by a newer build and is refused.
WORKSPACE_SCHEMA_VERSION = 1

# What an OLDER Workspace file gets. Doug, 2026-09-22: the default is a reset --
# "if it's explicitly set to no reset, then we don't reset; otherwise we do."
# Set this False, in the same commit as the bump above, only when every change
# since the previous number is additive (new tables, new columns that are
# nullable or carry a default) and the store may add them in place and restamp.
# It goes back to True with the next bump unless that one is additive too.
WORKSPACE_SCHEMA_RESET_REQUIRED = True

# The Workspace Toolbox image the broker's loader will accept (13.0 §6.2). The
# loader's contract is fixed: tag ``cognita-workspace-toolbox:<TOOLBOX_VERSION>``
# and archive ``toolbox-<TOOLBOX_VERSION>.tar`` in the toolbox-cache root
# (``cognita.runtime_broker.image_cache``, which owns the same literal on the
# loader side). This one moves only when the Toolbox Dockerfile changes, which
# is why it is not the application version.
TOOLBOX_VERSION = "12.6.0"
