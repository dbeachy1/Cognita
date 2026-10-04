# Cognita 12.17.0 release notes

This maintenance release publishes combined contract v5 and Workspace-only contract v3. It
exposes flushed running Workspace job output with stable byte offsets while preserving base64 wire
encoding, cancellation, idempotency, resource accounting, and cleanup behavior.

Workspace receipts report stale-file hashes, search-specific timeout reasons, replay status,
destination hashes, unread-output `has_more`, and the last measured usage. The public self-test
adds the running `begin\n` check and instructs callers to base64-decode stdout and stderr before
comparing markers.

Retired contract generations are closed at the route and catalog boundaries.
