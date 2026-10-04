# Cognita 13.0.0 release notes

This release consolidated image build, test, deployment, and live verification
in the repository's release tooling. Its test workflow uses an isolated stack
with its own PostgreSQL instance.

The PostgreSQL schema now carries a version of its own, independent of the
application release and the MCP contract generations. An image that finds a
database it does not understand changes nothing: it starts with the index
unavailable, reports both versions and the reason on `/healthz`, answers every
index, search and asset tool with `index_unavailable`, and prints the exact
reset command. Workspace tools keep working. There is no forward migration and
no automatic reset.

The derived search index and Workspace scratch data can be reset explicitly
with `scripts/reset_disposable_state.py`. Normal deployment and startup do not
reset this state. Original source documents remain outside Cognita's reset
operations.

Live verification exercises the MCP connection and the synthetic Self-Test
project. Test-only authentication is available only in test mode and is
restricted to that synthetic project; normal operation rejects it.

Public MCP contract generations are unchanged: combined v5 and Workspace-only
v3. No connector needs re-registration.

An older unversioned database may require the documented manual reset before
this release can use the index. The application reports the schema mismatch and
provides the recovery instructions; it does not erase or migrate the index
automatically.
