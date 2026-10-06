# Cognita 16.1.0 release notes

Development record for the KEI validation build. This is not published; qualification, review, and release assets remain pending.

The connected gateway exposed error_code INVALID_ARGUMENT for a missing-document self-test, while the installed engine's raw ASGI result omitted that field. Legacy status:error payloads now gain the stable code before validation and serialization across the shared result builder and generic proxy/gateway responses. Existing explicit error codes and diagnostic fields are preserved. Generated book and project-storage error contracts remain strict.

This is a server-side result-parity correction. The combined connector remains v6, Workspace remains v3, the PostgreSQL schema remains 1, the Workspace metadata schema remains 1, and the Toolbox remains 12.6.0. No connector recreation is needed for a schema or catalog change.

The latest verified downloads remain the [Windows installer 15.7.0](https://github.com/dbeachy1/Cognita/releases/download/v15.7.0/Cognita-Setup-15.7.0-r1.exe) and [Linux source installer 15.7.0](https://github.com/dbeachy1/Cognita/releases/download/v15.7.0/cognita-src-15.7.0.tar.gz). No 16.1.0 installer or source archive is attached.
