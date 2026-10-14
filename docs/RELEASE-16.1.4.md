General DOCX extraction now includes text inside Word content controls. Previously, a document whose text was entirely inside a control failed with `parse_failed`; a mixed document could index successfully while omitting that text.

**Latest verified platform installers (16.1.0; these do not include this patch):**
[Windows installer 16.1.0](https://github.com/dbeachy1/Cognita/releases/download/v16.1.0/Cognita-Setup-16.1.0-r1.exe) |
[Linux source installer 16.1.0](https://github.com/dbeachy1/Cognita/releases/download/v16.1.0/cognita-src-16.1.0.tar.gz).

This is a source patch for existing installations. The attached patch source archive and Python wheel are build inputs, not platform installers. No new Windows executable or installer is supplied by this source patch.

The extractor handles nested block and inline controls and controls in tables. Content-control properties are excluded from indexed text. The transformation is limited to the parsed in-memory document: original DOCX bytes, formatting, and byte hashes are unchanged. Existing heading and table formatting in extracted text is retained.

The audiobook speech-projection parser retains its separate, stricter supported-structure rules. MCP arguments, results, and connector generations remain unchanged (combined v6, Workspace v3).

After upgrading, retry uploads rejected with `parse_failed`. For documents already indexed with omissions, request `reindex_documents(project=..., full_rebuild=true)` for the affected project; smart reindex can skip unchanged source files. A full rebuild re-embeds the project's documents and can take time. This release does not automatically start one.

The behavior and acceptance cases are defined in [the extraction spec](SPEC-DOCX-CONTENT-CONTROLS.md).
