# Cognita 15.6.0 release notes

## Changed

- Admin shows translated completion text after a successful Brave Search connection test.
- The final credential-deletion confirmation shows the translated retention choice instead
  of a message ID.
- Inline Admin failures show labeled English technical detail when no translated outcome exists.
- Setup diagnostics record English identifiers and reasons rather than translated UI text.
- The README highlights the six-language interface near the beginning; logging and console
  scripts remain English-only.
- Windows Setup, its uninstaller, and the launcher are signed and timestamped through
  Microsoft Azure Artifact Signing with Douglas Beachy as publisher. Setup's signature is
  verified before its release checksum is generated.

## Compatibility

MCP arguments, results, and contract versions are unchanged. Application and MCP logs and
console scripts remain in English.

The release includes a signed Windows Setup download and a Linux source archive. The Linux
archive uses the release's container images. SHA-256 checksums accompany the downloads.

The separate `cognita-license-notices-15.6.0.zip` companion contains retained component
licenses and `source-notice-manifest.json`, with exact corresponding-source download
links, versions, hashes, and build inputs. The matching firmware source is provided as
`cognita-libkrunfw-corresponding-source-v0.7.0.tar`. These source downloads are optional
and are not needed to install or run Cognita.
