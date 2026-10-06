# Cognita 15.7.0 release notes

## Fixed

- Workspace file writes now fit the existing 1 MiB allowance through the private runtime
  broker. Its serialized request limits account for base64 expansion, JSON escaping, and
  the request envelope.
- Invalid broker requests return an argument error instead of misleadingly reporting
  that the Workspace runtime is unavailable.

## Compatibility

MCP tool schemas, result shapes, and contract versions are unchanged. The file-content
limit remains 1 MiB. Existing hash guards, job admission, and bounded request reading remain
in effect.

Windows release downloads are signed and timestamped with Douglas Beachy as publisher.
Platform downloads, source and license companions, and their SHA-256 checksums accompany
the GitHub release.
