# Cognita 15.2.0 release notes

## Fixed

- Routine health checks no longer repeat GPU profile, probe-choice, and VRAM-ceiling DEBUG messages.
- Windows Setup asks for the existing Admin password twice during update, repair, and reinstall.
  The password is still used to verify the installation and is not changed by Setup.

## Compatibility

MCP arguments, results, and contract versions are unchanged.

## Source for the Workspace runtime

The release assets include `cognita-libkrunfw-corresponding-source-v0.7.0.tar`
for the bundled Microsandbox firmware. Its SHA-256 and component revisions are
recorded in `THIRD_PARTY_NOTICES.md`.
