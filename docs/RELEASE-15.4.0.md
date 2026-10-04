# Cognita 15.4.0 release notes

## Changed

- Windows Setup uses a high-resolution Cognita mark in the Welcome panel and its executable
  icon. The title bar and taskbar use that icon as well.
- Setup and uninstall complete the six-language operator text, including progress, failure,
  and recovery guidance. Specific recovery steps remain visible when a technical error is
  otherwise shown under a translated heading.
- Admin localizes the Workspace network rule editor and configured-key status. Unmapped API
  errors show a translated outcome with English detail in a labeled disclosure.
- Windows upgrades continue to identify the installed and incoming versions, ask once for
  the current Admin password, and direct password changes to `cognita password`.

## Compatibility

MCP arguments, results, and contract versions are unchanged. Application and MCP logs and
console scripts remain in English.

This source release does not include a public Windows installer or container image assets.
