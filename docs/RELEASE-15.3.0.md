# Cognita 15.3.0 private test build notes

## Changed

- Windows Setup, Admin, and OAuth introduced catalogs for U.S. English, Spanish, French,
  German, Italian, and Brazilian Portuguese. Setup retains its selected language
  across a WSL restart. Logs, diagnostics, and console scripts remain in English.
- Windows Setup shows the Cognita mark in its page header. Its upgrade page names the installed
  and incoming versions and asks for the existing Admin password once. To change that
  password separately, run `cognita password` in a terminal.
- Setup translates its own progress and folder guidance while preserving English technical
  detail in diagnostics. Admin and OAuth select the language before sign-in.

## Compatibility

MCP arguments, results, and contract versions are unchanged.

The 15.3.0 installer was used for a private Windows upgrade test; no public binary assets
were published for this build.
