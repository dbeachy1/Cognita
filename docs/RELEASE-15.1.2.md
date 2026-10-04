# Cognita 15.1.2 release notes

## Fixed

- **Workspace self-test section W11 no longer depends on W2.** W11 tests copying between the
  Workspace and Knowledge. It copied a file that section W2 creates back into Knowledge, but said
  it needed only W1, so an assistant that ran W11 on its own, or cleaned up after W2 first,
  failed with "source not found" (ChatGPT did exactly that). W11 now creates its own folder and
  round-trips the Knowledge file it copied in itself. The Workspace plan version is now
  `workspace-4`.

## Compatibility

Nothing to do. MCP arguments and results are unchanged.
