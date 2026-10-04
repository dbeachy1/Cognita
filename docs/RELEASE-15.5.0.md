# Cognita 15.5.0 release notes

## Changed

- Windows Setup now tells users who have forgotten their current Admin password to run
  `cognita password` in Windows Terminal to set a new one, then return to Setup. The message
  appears when password verification fails and is translated into all six supported UI languages.
- The Cognita icon in the Setup page header now fits with space below it.

## Compatibility

MCP arguments, results, and contract versions are unchanged. Application and MCP logs and
console scripts remain in English.

This source release does not include a public Windows installer or container image assets.
