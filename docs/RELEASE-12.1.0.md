# Cognita 12.1.0 release notes

Released 2026-09-17.

Cognita 12.1.0 corrected migration handling for the containerized Knowledge and Workspace
deployment introduced in 12.0.0. The connector contract did not change.

## Corrections

- Migration inventory accepts deterministic symlinks whose resolved target is a regular file
  inside the explicitly mapped root, while rejecting escapes, broken links, directory links,
  junctions, and special files.
- PostgreSQL migration invokes `pg_dump --dbname` with the source DSN and redacts command details
  from failure and timeout exceptions.
- `mapped-roots.json` and `migration-manifest.json` persist the same final
  `source_unchanged` result after an apply run.
