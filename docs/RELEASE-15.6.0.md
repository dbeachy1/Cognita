# Cognita 15.6.0 release notes

## Changed

- Admin shows translated completion text after a successful Brave Search connection test.
- The final credential-deletion confirmation shows the translated retention choice instead
  of a message ID.
- Inline Admin failures show labeled English technical detail when no translated outcome exists.
- Setup diagnostics record English identifiers and reasons rather than translated UI text.
- The README's six-language UI note is at the end, with logging and console scripts noted as
  English-only.

## Compatibility

MCP arguments, results, and contract versions are unchanged. Application and MCP logs and
console scripts remain in English.

The GitHub release provides a Windows Setup download and a Linux source archive. The Linux
archive uses the published container images. Windows Setup is unsigned; Windows Smart App
Control can block unsigned applications.
