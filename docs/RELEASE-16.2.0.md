# Cognita 16.2.0 release notes

Codex and ChatGPT can reconnect when OpenAI advertises additional OAuth grants.

**Latest verified platform installers (16.1.0)**

[Windows installer 16.1.0](https://github.com/dbeachy1/Cognita/releases/download/v16.1.0/Cognita-Setup-16.1.0-r1.exe) |
[Linux source installer 16.1.0](https://github.com/dbeachy1/Cognita/releases/download/v16.1.0/cognita-src-16.1.0.tar.gz).

Cognita selects the authorization-code grant from the OAuth client metadata document. It continues to validate the registered callback, PKCE, and the client signature for signed clients.
