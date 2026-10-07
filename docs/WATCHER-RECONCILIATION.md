# Watcher reconciliation and queue clearing

Watcher batches pass through `RetrievalCore._relative_dirty_path` as the shared
path validation authority. Invalid paths are terminal failures: they are
reported with `retryable: false`, omitted from retry lists, and removed from a
watcher batch before directory markers reach either asset or text reconciliation.
Valid paths in a mixed batch continue normally. Root safety, filesystem reads,
embedding, and store failures remain retryable; failed reads or embeddings keep
the previously indexed rows intact.

`WatcherManager.clear_queue(name)` clears pending dirty paths and the project's
retry deadline, then cancels and awaits an active batch. A per-project queue
generation prevents that cancelled batch from restoring work captured before
the clear. Filesystem events marked after the clear use the current generation
and remain eligible for reconciliation. The operation returns `project`,
`cleared_paths`, and `active_cancelled`. Clearing a queue does not initiate a
full reconciliation.

The exact root-owned `.cognita-storage` subtree is ignored
at native and polling event intake and removed from stale queued batches before
either asset or text reconciliation. The comparison is case-insensitive on all
hosts, consistent with effective index policy, and matches only the first path
component, so a user directory such as `.cognita-storage-other` or a nested
`.cognita-storage` remains eligible.

Successful no-op watcher summaries, including skip-only passes, are logged at
DEBUG. Summaries with indexed, metadata-refreshed, or removed rows stay at INFO;
failures and retry scheduling remain visible, including nonretryable errors
retained in an asset component summary. Asset reconciliation follows the same
rule: successful no-op completions are DEBUG, while changes, errors, and refused
outcomes remain visible.

In Admin, use the **Clear watcher queue** action on a project. The matching
authenticated endpoint is `POST /api/projects/{name}/watcher/clear-queue`.
From a terminal, run `python scripts/clear_watcher_queue.py PROJECT_NAME`; the
script prompts for the Admin password unless `COGNITA_ADMIN_PASSWORD` is set.
`--url` selects the Admin base URL (default: `COGNITA_ADMIN_URL` or
`http://127.0.0.1:8676`), and `--username` selects the Admin username (default:
`COGNITA_ADMIN_USERNAME` or `admin`). For example:

```powershell
python scripts/clear_watcher_queue.py KEI --url http://127.0.0.1:8676 --username admin
```

For HTTPS Admin installations, use a hostname covered by the server certificate.
If its issuing CA is not in Python's default trust store, set `SSL_CERT_FILE`
to that CA's public PEM certificate before running the script. Certificate
chain and hostname verification remain enabled. Replace `cognita-host` and
the certificate path in these examples with your installation's values.

```sh
SSL_CERT_FILE=/path/to/rootCA.pem python3 scripts/clear_watcher_queue.py KEI --url https://cognita-host:8676 --username admin
```

```powershell
$env:SSL_CERT_FILE = "C:\path\to\rootCA.pem"
python scripts/clear_watcher_queue.py KEI --url https://cognita-host:8676 --username admin
```
