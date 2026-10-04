# Cognita 15.3.0 release notes

## Changed

- Windows Setup, Admin, and OAuth user interfaces have catalogs for U.S. English, Spanish,
  French, German, Italian, and Brazilian Portuguese. Setup retains its selected language
  across a WSL restart. Logs, diagnostics, and console scripts remain in English.
- Windows Setup shows the Cognita icon in its wizard. Its upgrade page names the installed
  and incoming versions and asks for the existing Admin password once. To change that
  password separately, run `cognita password` in a terminal.
- Setup translates its own progress and folder guidance while preserving English technical
  detail in diagnostics. Admin and OAuth select the language before sign-in.

## Compatibility

MCP arguments, results, and contract versions are unchanged.

## Source for the Workspace runtime

The release assets include `cognita-libkrunfw-corresponding-source-v0.7.0.tar` for the
bundled Microsandbox firmware. Its SHA-256 and component revisions are recorded in
`THIRD_PARTY_NOTICES.md`.
