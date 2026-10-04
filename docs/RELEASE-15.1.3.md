# Cognita 15.1.3 release notes

## Changed

- Local engine operations are organized into focused modules for reads, document changes, transfers,
  and reindexing. The host retains transport, dispatch, and lifecycle responsibilities.
- CLI skip checks cover additional cases for items that are already indexed.

## Compatibility

MCP arguments, results, and contract versions are unchanged.
