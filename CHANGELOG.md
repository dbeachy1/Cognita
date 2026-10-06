# Changelog

## 15.7.0 — Restore supported Workspace write sizes (2026-10-05)

- Workspace writes can use the existing 1 MiB file allowance through the private broker,
  including base64 binary content and text that expands when encoded as JSON.
- Broker request-validation failures retain their argument-error classification instead of
  being reported as an unavailable Workspace runtime.
- MCP tool schemas and contract versions are unchanged. See `docs/RELEASE-15.7.0.md`.

## 15.6.0 — Finish localized Admin responses (2026-10-04)

- Admin shows translated completion text after the Brave Search connection test and a translated
  retention choice when confirming credential deletion.
- Inline Admin failures retain labeled English technical detail when no translated outcome exists.
- Windows Setup diagnostic logging uses English identifiers and reasons while keeping the visible
  six-language guidance.
- The README's language-support note is again at the end, including the English-only logging
  and console-script boundary.
- MCP arguments, results, and contract versions are unchanged. See `docs/RELEASE-15.6.0.md`.

## 15.5.0 — Clarify password recovery and fix Setup icon clipping (2026-10-04)

- When an upgrade password is wrong, Windows Setup shows the `cognita password` command for
  setting a new Admin password without the old one. This guidance is localized in all six UI
  languages.
- The small Setup header icon fits within its display area with space below it.
- MCP arguments, results, and contract versions are unchanged. See `docs/RELEASE-15.5.0.md`.

## 15.4.0 — Complete localized Setup and Admin guidance (2026-10-04)

- Windows Setup uses high-resolution Cognita artwork in its Welcome panel, title bar, and
  taskbar. Setup and uninstall present remaining progress and recovery guidance in the six
  supported UI languages while retaining English technical details.
- Windows upgrades verify the current Admin password before leaving the password page and
  again before changing the installed service or files.
- The Admin Workspace network editor and configured-key status use the selected language.
  Generic errors keep their specific recovery instructions available.
- MCP arguments, results, and contract versions are unchanged. See `docs/RELEASE-15.4.0.md`.

## 15.3.0 — Initial operator localization and simpler Windows upgrades (2026-10-04)

- Windows Setup, Admin, and OAuth gained catalogs for U.S. English, Spanish, French, German,
  Italian, and Brazilian Portuguese. Logs, diagnostics, and console scripts remain in English.
- Windows Setup shows the Cognita mark in its page header, names the installed and incoming versions during an
  upgrade, and asks for the current Admin password once. Password changes use `cognita password`.
- MCP arguments, results, and contract versions are unchanged. See `docs/RELEASE-15.3.0.md`.

## 15.2.0 — Quieter GPU health checks and Windows password confirmation (2026-10-04)

- Repeated GPU profile, probe-choice, and VRAM-ceiling DEBUG messages no longer fill logs during
  routine health checks. GPU worker diagnostics remain available when enabled.
- Windows Setup asks for the existing Admin password twice during update, repair, and reinstall,
  catching a hidden-field typing mismatch before it starts. `cognita password` remains the way to
  change the password.
- MCP arguments, results, and contract versions are unchanged. See `docs/RELEASE-15.2.0.md`.

## 15.1.3 — Engine code organization (2026-10-03)

- Local engine operations are organized into focused modules for reads, document changes, transfers,
  and reindexing. CLI checks for skipping an already indexed item now cover additional cases.
- MCP arguments, results, and contract versions are unchanged. See `docs/RELEASE-15.1.3.md`.

## 15.1.2 — Workspace self-test W11 stands on its own (2026-10-01)

- The Workspace self-test's W11 (copies between Workspace and Knowledge) copied back out a file
  that W2 creates, while declaring only W1 as a prerequisite; an assistant that ran W11 without
  W2, or cleaned up W2's files first, failed with a missing source. W11 now round-trips the file
  it copies in itself and creates its own folder. Workspace plan version `workspace-4`. See
  `docs/RELEASE-15.1.2.md`.
- MCP schemas are unchanged.

## 15.1.1 — Windows: Cognita starts when a projects folder is missing; image catalog kept (2026-09-30)

- On Windows, a projects folder missing when Cognita starts (a drive unplugged, a folder renamed)
  stopped Cognita from starting at all. It now starts, `cognita start` names the missing folder,
  and `cognita restart` reconnects it once it is back. Setup adds one line to the distro's
  `/etc/fstab`; an existing install gets it from its next Setup run.
- A project whose folder is empty or not mounted no longer loses its PNG asset catalog (titles,
  descriptions and tags) at startup or on `reindex_assets`; the sweep is refused with
  `root_empty`, as the text index already did.
- A PNG that fails to read during the startup scan or `reindex_assets` (a disk error, a file being
  rewritten by a sync client) keeps its catalog row and metadata; it used to be dropped and come back
  blank.
- MCP schemas are unchanged.

## 15.1.0 — NVIDIA acceleration in Windows Setup; WSL memory reclaim (2026-09-30)

- Windows Setup offers NVIDIA acceleration on a PC with an NVIDIA card and driver 580 or newer
  (an Acceleration page naming the card, the GPU or the CPU, and the download size); an older
  driver is named with the download link. A card that does not verify ends as a CPU install that
  says why. Nothing is installed on Windows.
- The WSL image carries the NVIDIA Container Toolkit 1.20.1 (held); Setup installs or updates it
  in an older distro when NVIDIA is chosen.
- Setup offers, ticked by default, to add `autoMemoryReclaim=dropCache` to `.wslconfig` so Windows
  gets back the RAM WSL holds as file cache. The file is backed up first.
- The Linux installer gains `--acceleration-fallback cpu`.
- MCP schemas are unchanged.

## 15.0.3 — Admin shows the card in use after a restart; watcher summary line (2026-09-30)

- Admin's GPU acceleration page showed "CPU fallback" after every restart until Verify was pressed,
  while indexing ran on the card; it now shows "GPU (not checked since restart)", and reasons in words.
  A card indexing switched off after a failed startup check, and OCR on GPU while Knowledge GPU is off,
  now show as CPU, with the reason.
- Each file-watcher batch that embeds something writes one `embed.done ... walk=watcher` line;
  before, watcher indexing, the path most edits take, logged no summary.
- MCP schemas are unchanged.

## 15.0.2 — Cards come back after yielding; summaries count their own work (2026-09-30)

- A card that yielded to another program mid-index was never used again until restart (the dead
  pool kept the GPU lease); it is released before the card starts again. AMD and NVIDIA.
- `embed.done` reports each job's own chunks and devices again: after the first job, every
  summary said `chunks=0 decision=cpu` because the scheduler's workers credited the first job.
- Self-test G7/G8 check the index scheduler's warm cards instead of the pre-10.0 pool markers.
- A rerun of `./cognita update` after a failed start says the service failed and how to
  start it or roll back, instead of "Already up to date".
- MCP schemas are unchanged.

## 15.0.1 — NVIDIA on a desktop card; any Linux; self-test plan sections (2026-09-30)

- NVIDIA cards are gated on free VRAM only, so a card that also drives a desktop is used; AMD
  keeps its 20% utilization limit, and an explicit `gpu_max_busy_percent` applies to both.
  NVIDIA acceleration was verified on a desktop system with an RTX 4090.
- `embed.done` says `decision=gpu` when a card embedded anything (the scheduler path never
  set it). The NVIDIA image's hand-run preflight imports Cognita again. The installer stops
  with the fix when a documents folder is on a host-mounted filesystem.
- The Linux installer no longer refuses a distribution: any systemd Linux on x86_64 with Docker
  installs, Ubuntu 24.04 is the tested one. The Docker and Tailscale helpers use Debian's or
  Ubuntu's apt repository on that family and link the vendor's instructions elsewhere.
- `get_self_test_plan` with a section: steps 1-9 are found (they were answered as
  operator-only), every section of a group carries the group's intro (the asset fixture
  image and paths, the byte and OCR rules), and the index's `available` flag matches what
  each plan contains.
- MCP schemas are unchanged.

## 15.0.0 — NVIDIA acceleration (2026-09-29)

- A third image profile, `nvidia` (`cognita-app:<version>-nvidia`): indexing embeds on an
  NVIDIA card through CUDA, and OCR runs on it too. Same fallback rules as AMD: the CPU is
  always there and the index is always correct.
- The installer offers NVIDIA when it finds the driver (R580 or newer) and Docker's `nvidia`
  runtime; an older driver is named and not offered.
- `update` and an `install` rerun re-check a saved GPU profile (AMD or NVIDIA) before staging
  and fall back to the CPU when its runtime is gone; `status` hints at the repair for a failed
  service; `diagnostics` adds `gpu.txt`; `release.py publish --nvidia` publishes the image.
- MCP schemas are unchanged.

## 14.2.5 — Admin follows the system theme (2026-09-29)

- With no theme picked, the sign-in page and Admin follow the desktop's light or dark
  setting instead of forcing light. The Theme menu gains **System**.
- MCP schemas are unchanged.

## 14.2.4 — Final review fixes for the installer work (2026-09-29)

- The AMD check gates cards on the model's own room only, so other apps' load at install time
  cannot make the install permanently CPU-only.
- `copy_to_workspace` and `copy_from_workspace` each describe their own result lists.
- `reset_disposable_state.py` refuses a target `./cognita` adopted.
- MCP schemas are unchanged apart from one description.

## 14.2.3 — Two Workspace tool descriptions (2026-09-29)

- `output_encoding` says that leaving it out means base64; the copy tools say that
  `manifest` lists the source files and `committed` where they landed. Descriptions only.
- MCP schemas are unchanged apart from those two descriptions.

## 14.2.2 — The published Toolbox matches the selected release (2026-09-29)

- `release.py publish` always loads the Toolbox archive before pushing, so the published
  Toolbox is the build used by the selected target (14.2.1 published an older build of 12.6.0).
- The installer reads `cognita.yaml`, not the secrets folder, to know whether the Admin
  uses HTTPS (adoption's password check and `status --json`).
- MCP schemas are unchanged.

## 14.2.1 — The AMD check skips a card too small to use (2026-09-29)

- With "all cards" selected, the acceleration check skips a card without room for the
  model (the rule indexing already uses) instead of failing on it.
- MCP schemas are unchanged.

## 14.2.0 — The Linux installer, phase 2: OCR weights leave the images (2026-09-29)

- The EasyOCR weights are downloaded into the model cache (`<models>/easyocr`, mounted at
  `/var/lib/cognita/models/easyocr`) instead of being baked into the CPU and AMD images.
  The worker still verifies their hashes; when they are missing OCR reports "OCR model
  files are missing from the model cache. Rerun the installer to download them."
- `release.py deploy` fetches the weights into the target's model cache before applying;
  `publish` fetches them before the OCR smoke and test stack; `stage_cpu_ocr_models` and
  `containers/cognita-amd/models/*.pth` are removed.
- MCP schemas are unchanged.

## 14.1.0 — The Linux installer, phase 1 (2026-09-28)

- `./cognita install` installs Cognita on Ubuntu 24.04 from prebuilt images, with
  checks, generated secrets, a user systemd unit, and a live proof before it reports
  success; everyday commands for status, logs, update, rollback, reset, uninstall,
  add-folder, password and Tailscale Funnel.
- `release.py publish` and a `local` target; Admin picks a project folder inside a
  documents folder and tests it; a missing documents folder is reported plainly.
- Fix: the live self-test driver no longer runs a Workspace check for a core install.
- MCP schemas are unchanged.

## 14.0.1 — OneDrive safeBackup copies are sync conflicts (2026-09-28)

- The Linux OneDrive client names a conflict copy `<name>-<host>-safeBackup-0001.<ext>`.
  That shape was missing from the default `sync_conflict_patterns`, so sync conflict
  copies could be indexed as documents. One was present in the Self-Test pack and failed
  the 14.0.0 web self-test's pack and cleanup checks. It is now skipped, counted and
  reported like every other conflict copy. MCP schemas are unchanged.

## 14.0.0 — Licensed components replaced (2026-09-28)

Cognita is Apache-2.0. This release removes the three components that could not ship
under those terms.

- PDF text now comes from pypdfium2 (PDFium, BSD-3-Clause) instead of PyMuPDF
  (AGPL-3.0). Page markers and failure answers are unchanged. **Run
  `reindex_documents(full_rebuild=true)` once per project after upgrading**, because a
  normal reindex skips unchanged PDFs and keeps the old extraction.
- The default search reranker is now `BAAI/bge-reranker-v2-m3` (Apache-2.0), replacing
  `jinaai/jina-reranker-v2-base-multilingual` (CC-BY-NC-4.0). Cognita fetches it by
  pinned revision and checks every file's SHA-256. The first start downloads about
  2.3 GB in the background; searches use RRF order until it is ready. `/healthz` gains
  a `reranker` block showing the model and its state. A `reranker_model` set in config
  is still honored.
- `THIRD_PARTY_NOTICES.md` is new and ships in the image with `LICENSE`.

### Removed

- `knowledge-rag` and everything that existed only for it: the `engine: workers`
  3.x rollback path (the worker supervisor and its worker-client), stdio mode
  (`serve --stdio`, `scripts/start-stdio.bat`), and the `worker_port_min`,
  `worker_port_max`, `worker_probe_interval_s` and `stdio_default_project` settings.
  `engine: workers` in config or `COGNITA_ENGINE=workers` now stops startup with a
  plain message. Removing knowledge-rag also takes chromadb and PyMuPDF out of the
  image.

MCP tool schemas and contract generations are unchanged. The database schema is
unchanged.

## 13.7.1 — OCR ignores host access-time stamps (2026-09-28)

- OCR no longer answers `source_changed` when only a file's access time (and the
  ctime that moves with it) changed during the read. Windows stamps last-access on
  the first read of a OneDrive/DrvFS file; identity, size, mtime and the byte hash
  still decide whether the source changed.
- The deploy script re-renders the systemd unit so its Compose file list follows the
  release it starts, and the live OCR check accepts a GPU engine.

## 13.7.0 — Connector replay and Workspace repairs (2026-09-28)

- Return broker-validated job results after repeated identical commands replace
  local admission-cache state; preserve strict admission and idempotency replay.
- Accept valid multiline command arguments while preserving NUL, type, byte,
  count, and path guards; qualify real multiline Python execution over HTTP.
- Replay identical document mutations and preserve deletion backup receipts despite
  transport metadata changes; retain changed-content and authorization guards.
- Preserve the existing SQLite layout, Toolbox version, and MCP schemas.

## 13.6.0 — Windows CPU maintenance repairs (2026-09-28)

- Package a dedicated, hash-locked CPU EasyOCR runtime and verified offline
  models. Qualify the final application image as UID 1000 without networking
  before proceeding to downstream candidate builds. MCP schemas are unchanged.
- Own OCR cache directories explicitly so numeric service UIDs work without a
  username entry, and clean worker caches after success, failure, or cancellation.
- Require real OCR, cache/search publication, source timestamp preservation,
  error-envelope parity, and cleanup in canonical HTTP qualification.
- Include committed OCR results in asset searches when the source has no asset
  catalog row, while preserving catalog-hash and filesystem freshness checks.
- Create deletion backups at the file-removal owner, including unindexed source
  files. Avoid duplicate proxy snapshots and keep backup failure fail-closed.
- Make the public Self-Test's bulk-backup assertion repeatable with retained
  history by checking the current operation's receipt identities.
- Add a canonical Windows image-set update that preserves installation identity,
  credentials, source mappings, recorded ports, and PostgreSQL while verifying
  the new same-candidate application and Workspace images.

## 13.5.0 — Windows WSL2 CPU candidate (2026-09-27)

- Build AMD and Windows WSL2 CPU images from one source candidate, with
  Core and Full installation modes, hidden WSL startup, and guarded source-mount
  identities. MCP contract generations are unchanged.

## 13.4.0 — Reap unused GPUs after cold startup (2026-09-27)

- Arm the configured GPU idle-linger timer when cold provider startup finishes
  after CPU fallback has already drained the indexing queue.
- Preserve admission and external-compute safety by evaluating the ready transition
  under the scheduler capacity lock; newly admitted work cancels the pending reap.
- Add focused timer diagnostics and a deterministic regression test for the cold-start
  race. Failed stop callbacks remain visible and retry instead of falsely reporting a
  cold device. MCP contracts and storage schemas are unchanged.

## 13.3.0 — Source cleanup and review corrections (2026-09-24)

- Clarified source comments and synthetic examples without changing tool behavior.
- Removed unreachable local-engine response code; the active result builder retains
  the tool error flag and structured result contract.
- Separated Admin connector routes and Workspace metadata storage into cohesive
  modules while preserving existing authorization, schema, and import behavior.
- Replaced personal model-cache and stdio defaults with a user-home cache location
  and explicit/configured project selection; existing explicit settings are preserved.
- Corrected QA restoration after startup failure, concurrent-build image ownership,
  and selected-release Toolbox lookup in the manual reset helper.
- Preserve request diagnostics without consuming rejected request bodies; redact
  common credential headers in opt-in wire capture.
- Treat incomplete Workspace usage totals as unavailable for current quota reporting.

## 13.2.11 — Log timestamps in the host's time zone (2026-09-23)

- Compose: the three containers mount the host's `/etc/localtime` read-only.
  Cognita's log formatter stamps the process's local clock and a container
  with no time zone is on UTC, so every log line read four hours off the box
  beside it. Nothing to configure: the containers read the system's default.
  No code change; the version moves so `/healthz` says which build has it.

## 13.2.10 — Wire capture: log the data itself (2026-09-23)

- Gateway: `mcp_wire_capture: true` (config, off by default) writes every
  request and response on the connector MCP route, whole, to
  `<log_dir>/mcp-wire/<utc>-<connector>-<ids>.json` — request headers with
  the bearer token redacted, request body verbatim, response status, headers
  and body verbatim. `mcp_wire_capture_keep` (500) bounds the directory.
  This lets administrators enable detailed capture when troubleshooting.
- The captured request and response showed that some MCP clients validate each result
  validates every result against the tool list it fetched when it CONNECTED,
  and its "refresh tools" reads that in-memory list rather than asking the
  server again. The plugin's own error for the plan was "Structured content
  does not match the tool's output schema: data must NOT have additional
  properties …" — the pre-13.2.7 schema. Disconnect/connect in the plugin
  re-fetched the list and all three plan variants returned. No server change
  was needed for that; the capture is here for the next time.

## 13.2.9 — Log the stuff you need to see (2026-09-23)

- Gateway `tool call` line: argument VALUES for identifiers and switches
  (`project=Self-Test,section=B1`; lists as `<n items>`; content, text,
  argv, env, edits, base64, patterns and hashes stay name-only), `reason=`
  on an error, the structured result's top-level keys, a fingerprint of the
  text block (sha prefix, non-ASCII and control-character counts, line count)
  and the response content type — so a result a client shows and one it
  drops can be compared from the server side alone.
- Gateway: one `tools list` line per tools/list with tool count and a catalog
  digest, so the log shows WHEN a client re-fetched its tool list and WHICH
  catalog it got. Prod's log had shown SillyTavern's "refresh tools" never
  reached the server.

## 13.2.8 — The copy tools say where a selection lands (2026-09-23)

- `copy_to_workspace` / `copy_from_workspace`: the description and the `paths`
  and `destination` argument descriptions now state the placement rule — a
  selected FILE lands at `<destination>/<basename>`, a selected DIRECTORY at
  `<destination>/<its name>/` with its subtree, so selecting a tree's files one
  by one flattens them; select the directory to keep the layout — with an
  example of each. The behavior is unchanged and always was this; the
  description said only "copy selected files", and a DeepSeek session read
  the flattening as a bug. Catalog metadata only: names, arguments and
  results unchanged. `tests/test_bridge_tool_descriptions.py`.

## 13.2.7 — get_self_test_plan matches the outputSchema it advertises (2026-09-23)

- The plan answer for a Workspace-enabled connector carried
  `workspace_plan_version`, and its `index` mixed in the Workspace rows, while
  the advertised outputSchema forbade both. The server never validated this one
  result (built without its tool name), so nothing was logged; a client that
  validates results against outputSchema (SillyTavern's node MCP client) showed
  `[No content]` for every plan since 12.x, while claude.ai, which does not
  validate, showed it. Doug's SillyTavern retry after 13.2.6 pinned it: the
  51 KB index failed the same way as the 218 KB full plan.
- Schema: `workspace_plan_version` optional on every success branch; the index
  rows are described as the two shapes they are. Additive, no wire change.
- Gateway: the plan answer is validated like every other tool result, so a
  contract break is an ERROR line and an `output_contract_violation` answer.
- A regression test validates the result against the schema from `tools/list`.

## 13.2.6 — The first call after a deploy works for a workspace that already existed (2026-09-23)

- Workspace: every deploy restarts the broker and its generation moves, so the
  first call for every pre-existing workspace was rejected with
  `generation_conflict`. Job and fs calls already retried once after the reset;
  admission and `workspace_info` did not. Both now retry once after the reset.
- Bridge: a workspace rejection during admission reaches the client with its
  own reason (`generation_conflict`, `retryable: true`) instead of
  `internal_error: Bridge operation failed` and an ERROR traceback.
- A regression test covers admission after a broker restart.

## 13.2.5 — copy_to_workspace imports into subdirectories; the log says why when it can't (2026-09-23)

- Workspace runtime: the transfer import creates the guest parent directory
  before copying (the same component-wise mkdir `fs_write` uses). The SDK's
  `copy_from_host` does not create it, so every `copy_to_workspace` into a
  subdirectory failed with `FilesystemError` after the first flat file worked
  (prod, 2026-09-23 02:38Z). Pre-existing since 12.x; every fake at every
  layer accepted any guest path. `tests/test_transfer_import_parents.py`.
- Workspace runtime: a failed import logs stage, category, guest path depth
  and whether the staged host file existed — never a path.
- Workspace: broker rejections say why in the line (operation, category,
  stage, reason, workspace id). A `not_found` on `fs_stat` — the normal
  "not there yet" answer during a transfer's inventory — is DEBUG, not
  WARNING; one failing copy used to print 114 identical, reason-less warnings.
- Console: both formatters append a record's `extra=` fields as `key=value`
  on the message line, ahead of any traceback, so every existing structured
  site is readable in `docker logs`. Redaction runs on the finished line.

## 13.2.4 — Every connector MCP exchange is logged (2026-09-22)

- Gateway: one `mcp exchange` line per HTTP request on the connector route —
  methods, ids, batch shape, request bytes, the client's Accept /
  Content-Type / MCP-Protocol-Version headers, session-id presence, user
  agent, and the reply's HTTP status, media type, bytes and elapsed ms.
  Rejected requests are logged the same way.
- Gateway: `initialize` logs the client's declared name, version, requested
  protocol version and capability names.
- Gateway: the 13.2.3 `tool call` line adds the JSON-RPC id, argument names,
  block types, text-block chars and `structuredContent` bytes.
- Never the token, an argument value, or content (pinned by
  `tests/test_gateway_mcp_logging.py`).

## 13.2.3 — One log line per tool call (2026-09-22)

- Gateway: every `tools/call` on the combined route logs connector, tool,
  payload status, error flag, content block count, bytes and elapsed ms. No
  arguments, no content.
- release.py: `deploy` and `select`
  verify that the right version answers and stop there; a busy or absent GPU
  is a warning with the server's reasons, never a failure. The live self-test
  is QA: `release.py qa --target <t>` runs it on demand, and `deploy --test`
  runs it after the suite. install-unit retires drop-ins (from 13.2.2) and the
  deploy-time Workspace reset (13.2.0) are unchanged.

## 13.2.2 — Stale key row is redrawn, not reported (2026-09-22)

- Admin: deleting a Private Bearer Token key the server no longer lists redraws
  the list from the server (dropping the stale row) and says so, instead of
  stopping with "refresh the credential list".

## 13.2.1 — Admin key list fix; the suite stops betting on wall-clock time (2026-09-22)

- Admin: a created Private Bearer Token key rendered as "Unnamed" with no ID,
  and every row action on it said "not found". The admin route converted the
  stored record (a slot-based dataclass, no `__dict__`) to an empty dict.
  Pre-existing since 12.x; the route test used dicts. Fixed, with a test on
  the real record type.
- Tests: every real sleep, millisecond TTL and unbounded await removed from
  the affected test files. Waits are on signals or an
  injected clock; timeouts only guard against hangs on guaranteed signals.
  Seams with unchanged production defaults: injectable clock on the index
  scheduler's GPU runtime (plus queued chunks stamped from the scheduler's own
  clock), injectable timer factory on the warm GPU pool. Watcher tests use a
  fake observer; OS event delivery is no longer part of the unit suite.

## 13.2.0 — Workspace schema version; deploy resets Workspace state itself (2026-09-22)

These changes followed a deployment that changed the Workspace SQLite layout
and required several manual steps.

- **The Workspace metadata file carries a schema version** (`workspace_schema`
  row; `WORKSPACE_SCHEMA_VERSION` in `release_identity.py`, now 1). A file at
  the build's version is left alone. A file at a higher version was written by
  a newer build and is refused. An older file -- older stamp, or no stamp with
  columns missing -- is **reset by default**; only when the build sets
  `WORKSPACE_SCHEMA_RESET_REQUIRED = False` (an explicitly additive change) are
  the missing columns added in place and the file restamped. A file with no
  stamp whose tables already match (what 13.1.0 wrote) is simply stamped, so
  this release itself needs no reset. The index's own schema version
  (`DATABASE_SCHEMA_VERSION`) is unchanged and unrelated.
- **`deploy` runs the Workspace reset itself.** When the build it just started
  refuses the Workspace layout, `deploy` reads the refusal from the container,
  runs `reset_disposable_state.py --scope workspaces` (`--called-by-deploy`,
  phrase on stdin, lock already held), and carries on to verify. One command
  again. The index is never reset by deploy.
- **Admin: "Add key" on a connector works.** The Private Bearer Token key list
  (the feature 12.x called "named credentials"; renamed in the UI at Doug's
  request) stamped the policy revision on its inner list element while every
  click handler read it from the outer panel, so on a combined connector
  "Add credential" always sent revision 0 and the server answered 409
  "Credential policy changed". For as long as the feature existed. Fixed;
  the server now also logs which refusal it mapped to that message.
- **Admin: no more browser prompt boxes.** The app's modal gained fields
  (text, password, select); the five credential dialogs that still used the
  browser's raw `prompt()` (label and password on Add key, the password on
  reveal and rotate, the retention choice on delete) use it now.
- Left over from 13.1.0's rehearsal, also here: the reset brings down the
  containers a failed unit start leaves behind, and a failed start quotes the
  container's refusal instead of only systemd's text.

## 13.1.0 — Workspace run-and-wait, text output, tail reads, usage visibility (2026-09-22)

This release adds Workspace job waiting, text and base64 output options, ranged
file reads, and usage visibility.

- `workspace_start_job` / `workspace_get_job` accept `wait_ms` (0–55000) and
  report `waited_ms` and `wake_reason` (`exited`, `timeout`, `gone`). Only a
  waiting call leaves the event loop, on a fixed ten-thread pool.
- Job output can be recoded with `output_encoding` (`auto`, `text`, `base64`)
  and `strip_ansi`; the job reports `stdout_encoding`/`stderr_encoding` and,
  in `text` mode, `stdout_lossy`/`stderr_lossy`.
- `workspace_read_file` gains `start_line`/`end_line`/`tail_lines` (broker
  `fs_lines`) with `total_bytes`, `total_lines`, `has_more`;
  `workspace_get_job` gains `tail_lines` with `stdout_lines`/`stderr_lines`.
- Every Workspace response carries `quota_remaining_bytes` and
  `quota_warning`; `workspace_info` adds `usage_by_directory` and
  `last_auto_action`/`last_auto_action_at`. Apparent bytes come from the
  broker's `fs_usage` and a write or job start refreshes a missing or stale
  sample (12.18.2–12.18.4).
- Error envelopes keep their reason when the broker sends no
  `correlation_id` (12.18.1).
- A retried asset mutation (`put_asset`, `update_asset_metadata`,
  `remove_asset`, `reindex_assets`) with the same `operation_id` and
  arguments replays the original failure, reason and message, marked
  `idempotent_replay: true`, instead of answering `operation_conflict`
  (12.18.5). The stored failure record now keeps the message.
- A replayed Workspace call carries both `idempotent_replay` and `replayed`.
- Self-test sections W13 and W14; the Workspace plan moves to `workspace-3`.
- Not ported: 12.18.5's OCR-limit helper and step-51 fix, which 13.0.2 had
  already shipped by another route. 13.0.2's broker-side usage walk stays.

### Deploying

`workspaces` gains two nullable columns (`last_auto_action`,
`last_auto_action_at`). 13.0 does not migrate Workspace state, so on every
existing target the first `deploy` ends in `apply-failed` with the server
refusing to start and the deploy output quoting the container's reset
command; run `reset_disposable_state.py --scope workspaces --apply` and then
`select`. The index is untouched. `DEPLOYMENT.md` has the sequence.

Two deploy-path fixes from rehearsing that on a test installation: the reset
brings down the containers a failed unit start leaves behind (it used to wait
and refuse, needing a hand `docker compose down`), and a failed unit start in
`release.py` now quotes the app container's refusal line instead of only
systemd's text. Then, after the prod deploy, `deploy` learned to run that
Workspace reset itself (`reset_disposable_state.py --called-by-deploy`, phrase
on stdin, lock already held) and continue to verify, so the next layout change
is one command again. The index is never reset by deploy.

## 13.0.2 — Self-test findings from the 13.0.1 live run (2026-09-22)

- Workspace usage is measured. The sandbox SDK's `used_bytes` returns 0 for a
  directory volume that holds files, and the broker took that 0 as a
  measurement; every Workspace reported `measured_apparent_bytes: 0` beside
  `usage_status: "measured"`, and the transfer quota check had nothing to
  check against. The broker now measures the volume's own backing directory
  under the SDK data dir (verified: a real directory named by the SDK's
  handle, with the SDK's lock file beside it) with a bounded stat walk that
  never follows symlinks and never reads contents. The SDK figure is used only
  when it is non-zero; when neither source is available the status is
  `unknown`, not 0. `workspace_info` now reports the persisted record's usage
  fields (`fresh`, numeric) like every other Workspace tool instead of the
  broker's raw vocabulary (`measured`, allocated null).
- OCR reports the limit it enforces. The per-side dimension limit is
  `min(ocr_max_dimension, 4096)` (the asset store's own bound, as DESIGN-10.0
  states) and the pre-scan always applied it, but `limits.max_dimension` said
  8192 and so did the worker's request. One helper now feeds all four sites.
- `list_assets` entries no longer carry `score` / `search_method`. Those are
  search-only fields; a listing is not a search.
- `copy_from_workspace` with `conflict_policy: skip` reports `bytes: 0` for
  a transfer that wrote nothing. It used to report the staged manifest's size.
- Self-test plan step 51 names the pack directory (`cognita-selftest-pack`)
  instead of a string that is not a directory, so a literal run no longer
  strands the pack; its zero-document check no longer sweeps in the server's
  permanent `cognita-selftest/ocr/` fixtures. R-A1 and A7 assert the
  `list_assets` shape above.

## 13.0.1 — Workspace on by default (2026-09-22)

- A new connector gets Workspace tools and Knowledge ↔ Workspace transfer by
  default. They used to default off, so every installation shipped without the
  feature until someone found the checkbox; the first 13.0 live check on main
  failed for exactly that reason. A connector record that already says off
  stays off unless an administrator enables it for a connector.

## 13.0.0 — One script builds, tests and deploys (2026-09-22)

- `scripts/release.py` is the whole deployment mechanism: one command builds,
  tests, applies and verifies a release, with no automatic rollback, no
  deployment journal and no state-transition rehearsal.
- The PostgreSQL schema carries a version. An image that does not understand
  the database serves with the index unavailable, says so on `/healthz`, and
  prints the exact `reset_disposable_state.py` command instead of migrating.
- Live verification runs against the real connector with a built-in public test
  key, accepted only while the server was started in test mode and only for the
  `Self-Test` project; it expires 30 minutes after startup.
- A release is a directory: `/data/cognita/releases/<target>/<version>/` holds
  the Compose files, a generated `compose.images.yaml` that names each image
  by a per-release tag (`cognita/app:<version>-<commit12>`) that only that
  release owns, the Toolbox archive and a `release.txt` recording the image
  IDs. The `current` symlink is
  the selection, and the user systemd unit runs `docker compose ... up` against
  it. Startup never builds, pulls, tests, migrates, restores or resets.
- `deploy` does not run the full test suite by default: a small change ships on
  the build plus the live self-test, and `test` or `deploy --test` runs the
  suite in a throwaway stack with its own PostgreSQL, ports and configuration.
- `release.py select --version <old>` starts an earlier 13.x release with one
  restart. There is no automatic rollback; `select` never resets anything.
- `scripts/reset_disposable_state.py` is the only thing that discards the index
  or the Workspace scratch state. It prints a plan, requires a typed
  confirmation phrase to apply, and is never called by deploy, select or
  startup. Documents, assets, backups, credentials, configuration, the model
  cache and other targets are out of its reach.
- `src/cognita/release_identity.py` is the single version authority:
  `__version__`, wheel metadata, `/healthz`, `initialize`, Admin, the self-test,
  broker health, OCI labels and image tags all derive from it.
- Public MCP contract generations are unchanged: combined v5, Workspace-only
  v3.
- `release.py doctor` is the one host-prerequisite check: Docker, Compose, the
  systemd user manager, `/dev/kvm`, the AMD devices, a writable releases root
  and the target's env file.

### Removed

The 12.x release machinery is deleted rather than disabled: `kei_release.py`
and its `release_target`/`release_identity`/`release_builder`/
`release_qualification`/`release_resources`/`release_adoption` modules, the
sealed-bundle and candidate/promote/rollback/record-live workflow with its
`release.json`, `selected.json`, `live.json` and per-result evidence files,
`adopt-systemd.sh`, `materialize-release-evidence.sh`,
`verify-release-evidence.sh`, `install-container-systemd.sh`,
`deploy-container.sh`, all six `preflight-*.sh` gates, `image_package_inventory.py`,
`bootstrap-toolbox-cache.sh`, the per-profile systemd unit files,
`containers/release-policy.json`, `config/cognita-target.example.json`, the
manifest's `release` field and per-image `tag`/`digest`/`status`,
`src/cognita/migration12.py` with the `migrate-12` command, the PostgreSQL
restore mount and its `.cognita-restore-complete` healthcheck gate, the
`ALTER`/`DO` schema-migration branches with the per-project asset and OCR meta
tables, the automatic removal and recreation of a failed Workspace VM, and
every test whose only subject was one of those.

## 12.17.0 — Workspace streaming and contract consistency (2026-09-21)

- Publish the combined Cognita v5 and independent Workspace-only v3 contracts
  with the current Workspace response schemas; retired generations fail closed.
- Make running Workspace jobs expose flushed stdout/stderr bytes and offsets,
  including the self-test's explicitly flushed `begin\n` before a long sleep,
  while preserving base64 wire encoding and cancellation/cleanup invariants.
- Repair stale hash, regex timeout, idempotent replay, destination hash, and
  measured usage receipts, with focused contract and self-test coverage.

## 12.16.0 — Packaged OCR worker source isolation (2026-09-21)

- Keep the isolated OCR worker rooted at the image's retained `/app/src` source
  tree when the service package is installed into `site-packages`, preserving
  the qualified OCR runtime and CPU fallback in deployed Docker services.
- Keep the public MCP schemas and contract generations unchanged.

## 12.15.0 — Failed first-use Workspace cleanup (2026-09-20)

- Allow an explicit Admin Remove to clear a failed first-use Workspace row
  whose volume name was never persisted, but only after the broker proves the
  canonical sandbox and volume names are absent and both object IDs are null.
- Keep ordinary retry, bulk deletion, and every incomplete or mismatched
  broker observation fail closed.

## 12.14.0 — Workspace lifecycle repair (2026-09-20)

- Let the broker own failed Workspace teardown so Admin removal can clean a
  sandbox whose named volume is already missing without attempting a failing
  app-level stop first.
- Initialize new broker Workspace rows with an unused one-shot volume-creation
  allowance even after migrating databases whose existing rows are permanently
  marked as already attempted.

## 12.13.0 — Runtime recovery and release maintenance (2026-09-20)

- Recover owned desired-running Workspaces after a broker restart or runtime
  crash without replaying jobs or replacing the existing sandbox.
- Repair the public Workspace self-test acceptance plan and keep its checks
  aligned with caller-reachable behavior.
- Reorder AMD image layers so source and version changes reuse the expensive
  ROCm, embedding, OCR, and model layers; document bounded 100 GB BuildKit
  retention and current-plus-previous release image retention for deployments.
- Restore each copy button's original label, including the stable "Copy URL"
  label after rapid repeated clicks.

## 12.12.0 — Public Workspace self-test contract repairs (2026-09-20)

- Align the public Workspace self-test with caller-reachable behavior: precreation `workspace_info` may report no workspace, quota remains observational, traversal reports `invalid_arguments`, and W11 denied-policy checks are conditional on an already configured connector.
- Remove the unsupported toolbox digest expectation and keep the pinned Workspace toolbox image unchanged.

## 12.11.0 — Workspace job admission recovery (2026-09-20)

- Reconcile cached active-job records with the broker before blocking Workspace mutations, new jobs, bridge transfers, or lifecycle actions. An interrupted caller can no longer leave a completed job marked active indefinitely.
- Include the active job ID and timing in genuine `job_running` errors, and report degraded runtime availability separately from the stored lifecycle state.
- Extend the public caller self-test with an unpolled job completion check; keep privileged restart checks in the release self-test.

## 12.10.0 — Workspace edit connector schema repair (2026-09-20)

- Publish the existing `workspace_edit_file` edit entries as `{match, replacement}` objects in both current connector catalogs. The runtime and caller self-test already use this shape; clients must recreate their connector to refresh its cached schema.

## 12.9.0 — Workspace bridge release verification repairs (2026-09-20)

- Accept an unmeasured Workspace usage value while checking bridge transfer quota. The configured quota remains enforced.
- Correct W11 caller self-test bridge arguments to use Workspace-root-relative paths, matching the bridge contract.

## 12.8.0 — Workspace filesystem and caller self-test repairs (2026-09-20)

- Fixed copy and move destination checks against the pinned Microsandbox SDK,
  and selected its file or recursive-directory removal method correctly.
- Gave API-created Workspace paths to the unprivileged guest user so Python,
  Node, and other jobs can write beneath those paths.
- Returned `path_conflict` for stale hashes and existing destinations instead
  of reporting Workspace capacity contention.
- Bounded regex search below the gateway timeout so catastrophic patterns
  return a broker result and the owned worker is terminated.
- Removed Admin lifecycle and second-principal steps from the public Workspace
  self-test. W3 restart persistence and W10 isolation remain release checks.

## 12.7.0 — Workspace job default directory (2026-09-19)

- Fixed public Workspace jobs with omitted or root working directory so the
  broker receives `/workspace` rather than an invalid empty path.
- Extended the public-to-broker contract test and the live Workspace smoke
  to exercise the default directory and current read/job response fields.
- Kept the 12.6 guest toolbox image pinned for existing Workspaces while
  versioning the Cognita and broker services at 12.7.0.

## 12.6.0 — Workspace/Admin gap closure (2026-09-19)

- Sourced Admin and login version displays from the running release constant.
- Added truthful Workspace storage/usage projections, filters, cursor paging,
  diagnostics, safe retry, and responsive row actions.
- Replaced Reset and immediate bulk deletion claims with a single Remove action
  and an expiring, revision-bound bulk preview/confirmation flow.
- Added explicit credential deletion retention choices and documented the
  named-volume/host-root ownership boundary.

## 12.5.0 — Restore the stable combined MCP URL (2026-09-19)

- Restored `/mcp/connectors/<slug>/mcp` as the stable alias of the current v4
  catalog, with its own exact OAuth resource identity. Immutable `/v4` remains
  available; retired v1-v3 and future generations remain rejected.
- Restored both copyable URLs in Admin and added positive stable-route, OAuth,
  catalog-equivalence, and rejection checks to prevent this regression.
- No connector schema-generation change, data migration, or Knowledge re-index.

## 12.4.0 — AMD GPU acceleration and Workspace/OAuth administration (2026-09-17)

- Added Admin-owned, revisioned GPU/OCR acceleration policy with safe staged
  apply, verification, rollback, restart/recreate state, and diagnostics.
- Added the isolated AMD ROCm/MIGraphX embedding and EasyOCR runtimes, with
  hash-qualified offline EasyOCR model artifacts and CPU/AMD Compose boundaries.
- Completed the containerized Workspace broker and durable OAuth administration
  surfaces for the 12.4 release.

## 12.3.0 — Connector entity tabs and immutable URL identities (2026-09-17)

- Added dynamic connector entity tabs with connector-scoped function tabs and an
  Add connector tab that remains last, including draft, history, deletion, and
  focus behavior keyed by immutable connector IDs.
- Exposed stable recommended MCP URLs separately from the current
  generation-pinned URL, with compatibility aliases and cache-busted 12.3.0
  release assets.

## 12.1.1 — Admin URL and connector UX (2026-09-17)

- Added an Admin-editable canonical public URL persisted in Cognita-owned state,
  with immediate connector-link and OAuth metadata effect.
- Added explicit Connector subtabs for settings, per-project access, and
  per-project Workspace transfer, with clearer active and focus states.

## 12.1.0 — Migration integrity corrections (2026-09-17)

- Preserved deterministic, safe in-root model-cache symlinks in migration inventory.
- Passed PostgreSQL source DSNs through `pg_dump --dbname` while redacting failure
  and timeout details that could expose credentials.
- Made the final source-integrity result authoritative in both nonsecret migration
  artifacts so an apply run cannot persist a stale `source_unchanged` value.

## 12.0.0 — Containerized Knowledge and Workspaces (2026-09-17)

- Added the digest-pinned Compose/systemd stack, PostgreSQL 18 + pgvector, and
  private Microsandbox 0.7.0 Workspace runtime.
- Added named surface-scoped credentials, durable OAuth principals, Workspace
  lifecycle/jobs/quotas/retention, strict guest networking, and Admin operations.
- Added Knowledge to Workspace bridge manifests, conflict/hash receipts, staging
  cleanup, and watcher/index reconciliation without model-context file bytes.
- Added strict host-to-container migration maps for every configured folder root,
  a nonsecret verification manifest, beta 9675/9676 operation, and production
  8675/8676 promotion/rollback.
- Workspace remains CPU-only; NVIDIA Workspace GPU support is not part of 12.0.

See README.md and DEPLOYMENT.md for installation and operator procedures.

Behavior changes to the tool surface, newest first. **Every entry here is something
a client can observe** — a new tool, a changed result shape, a call that used to
succeed and now fails, or the reverse.

This file starts at 5.0.0. Earlier history is in the design documents and `git log`;
it is not reconstructed here, because a changelog written after the fact from commit
messages is a guess dressed as a record.

Why it exists: the downstream SwarmUI project accumulated a whole rule set built
around constraints that had stopped being true, because the constraints changed
silently. A behavior change that is not written down is a trap for whoever reads the
old docs next.

## 11.4.2 — 2026-09-17

**Self-test project routing clarification.** The emitted self-test contract now
requires the exact provisioned `Self-Test` project for every project-scoped
operation, including ordinary document, byte-fidelity, search, manifest, copy,
de-index, error-envelope, asset, OCR, read-only, and cleanup checks. It explicitly
forbids selecting or testing any other project.

## 11.4.1 — 2026-09-17

**Structured result contract correction.** Runtime validation now accepts the
content-addressed string IDs emitted by `list_documents` and the explanatory
`ghost_check` text emitted by successful `remove_document` deletes. These calls
no longer get replaced with an internal or unknown-outcome error after the
underlying operation succeeds. Tool names, arguments, and intended result
semantics are unchanged. The corrected `outputSchema` is part of the client-owned
connector snapshot, so existing Cognita connectors must be recreated after this
deployment before client-visible verification can be considered complete.

## 11.4.0 — 2026-09-16

**Reliable incremental filesystem reconciliation.** Native and polling watchers
now converge text documents and PNG assets from current on-disk state, including
populated directory events, targeted updates/deletions, retryable transient
failures, and root-identity safety. Quiet polling does not start reconciliation;
the public MCP contract is unchanged.

## 11.3.0 — 2026-09-16

**Compact Admin navigation.** Primary Admin tabs and Projects subtabs remain
left-aligned with consistent gaps instead of distributing across the page. No
connector behavior, accessibility contract, or MCP surface changed.

## 11.2.0 — 2026-09-16

**Tabbed Admin UI and immediate key lifecycle.** The authenticated Admin shell now
organizes Projects, Connectors, and Authentication into separate tabs, with View /
Create New project subtabs and collapsed per-project authentication sections.
Project creation refreshes Authentication without a hard browser refresh. Static-key
Generate New and Revoke are immediate operations with a one-time copyable key modal;
OAuth policy changes remain explicit and revision checked. The MCP contract and
connector URLs are unchanged, and all shipped Admin assets use the 11.2.0 cache
version.

Rollback to 11.1 preserves the resulting authentication policy because no persisted
schema fields were added, but the older Admin UI restores the staged-key interaction
and its second Save action. Administrators must follow that older workflow after a
  rollback.

## 11.1.2 — 2026-09-16

### Fixed

- GPU indexing callbacks are now awaited before their results reach vector
  validation. Full reindex jobs no longer reject coroutine objects as malformed
  embeddings and unnecessarily retry those batches on other devices or the CPU.
- Scheduler device-failure warnings now include a bounded, single-line cause so
  future embedding failures can be diagnosed without enabling verbose logging.

---

## 11.1.1 — 2026-09-16

### Fixed

- Valid OAuth and static credentials now complete the MCP handshake even when
  current connector policy exposes no projects to that principal. Project
  authorization remains enforced on every tool call.
- A project-scoped static key is now recognized as an explicit grant for its
  project through an all-project connector, including when the project is
  excluded from inherited connector permissions. Other credentials remain
  excluded, and the connector default still controls read versus write access.
- A tool call that omits `project` now uses the sole project currently
  accessible to that principal through the connector. Calls still require an
  explicit project when more than one is available and fail generically when
  none are available.

---

## 11.1.0 — 2026-09-16

**Authentication warning reliability.** The Admin Authentication status endpoint
now correctly evaluates connector lockout warnings against the effective-access
project field, avoiding a production crash when connector access is configured.
Projects can now be excluded from all-mode connector defaults while retaining
explicit grants. Before rolling back to an older binary, convert sensitive
connectors to selected mode or remove their default scope because older binaries
may discard this additive registry field.

## 11.0.0 — 2026-09-16

**OAuth and durable static private-key authentication.** Canonical v3 and
compatible v2 connector requests now accept either an exact-audience OAuth token
or a generated `cog_sk_v1_...` private key sent as `Authorization: Bearer`.
OAuth defaults and project overrides coexist with one global static key and one
optional project override; project keys are scoped to their project, while a
project override shadows the global key. The Admin Authentication card stages
generate/clear changes, shows generated keys once, and requires explicit
confirmation before leaving enabled projects without client authentication.
Legacy test-mode project tokens, Debug Tokens Mode, and secret-bearing MCP paths
are retired; the public MCP tool schemas and v3/v2 contract window are unchanged.

## 10.4.0 — 2026-09-16

**Safe dot-directory asset paths.** PNG assets beneath ordinary dot-prefixed
directories such as `.obsidian` are now accepted. Empty components, `.` and `..`
traversal, project-root escape, backup trees, symlink traversal, case collisions,
and unsupported media types remain rejected independently.

## 10.3.0 — 2026-09-16

**Self-test release identity.** `get_self_test_plan` now derives `plan_version`
from the running Cognita package version. The self-test and server versions therefore
always match, and release tests enforce the invariant instead of maintaining an
independent manual plan-version string.

## 10.2.0 — 2026-09-16

**Rolling connector contract compatibility.** The current v3 connector URL remains the
canonical registration surface while the immediately preceding v2 URL remains callable for
in-flight clients. v2 serves the frozen 10.0 39-tool catalog without the v3-only
`remove_asset` tool or `outputSchema` declarations; compatible v2 calls use current
implementations. Calls that cannot be honored under v2 return a structured
`upgrade_required` tool error directing the client to the current connector URL.

## 10.0.0 — 2026-09-15

**Offline PNG OCR and scheduling.** The `ocr_asset` tool adds bounded OCR for
authorized static PNGs, deterministic source freshness validation, offline EasyOCR
worker isolation, and searchable OCR-derived results. Requests use one caller-selected
end-to-end timeout (default 120 seconds, valid 10–600), fresh device/RAM gates, and
recoverable GPU/CPU memory-pressure failover without partial publication. The accepted
10.0 public catalog contains 39 tools at contract v2; at that release connector URLs
served only that current published generation.

## 9.3.3 — 2026-09-15

**Connector URL slugs and contract cutover messaging.** Connector URLs now use a
stable human-readable connector slug, such as
`/mcp/connectors/cognita/mcp/v1`, while the UUID remains an internal policy and
audit identity. Publishing the next contract version retires older versions
immediately; clients must reconnect using the newly advertised URL. The Admin
connector cards also no longer reserve an unnecessary bottom margin after their
action buttons.

## 9.3.2 — 2026-09-15

**Versioned connector contract URLs.** Connector cards now advertise canonical
`/mcp/connectors/<connector-id>/mcp/v1` URLs and can publish the next contract
generation for clients that need to rescan. The 9.3.2 cutover rejects unversioned
connector resources; previously published versioned URLs remain valid.

## 9.3.1 — 2026-09-15

**Unified Cognita mark.** The admin favicon and MCP connector icon now share the
original Cognita open-C geometry and blue background. This replaces the unrelated
filled C introduced in 9.3.0 without changing connector URLs, OAuth, tools, or
request behavior. The Add project form now includes an authenticated **Test path**
action that validates the absolute documents directory and reports its recursive
regular-file count and total bytes without reading file contents or exposing names.
The bundled self-test plan label is now 9.3.1, with a release assertion preventing
it from drifting behind the advertised server version again.

## 9.3.0 — 2026-09-15

**MCP connector identity.** Initialize responses now advertise a branded Cognita
icon using the MCP `serverInfo.icons` metadata introduced in the 2025-11-25
contract. The fixed same-origin 512px PNG is available without connector
credentials so supporting clients can render it during connection setup. Older
clients can ignore the additive metadata and retain the existing connector
behavior.

## 9.2.0 — 2026-09-15

**Connector efficiency and exact-byte compatibility.** The public 38-tool catalog
now includes ordered `get_documents` and `remove_documents` collections plus the
connector-scoped `batch` envelope. Batch execution is sequential and non-atomic:
earlier successful children remain applied after an error, and each child supplies
its own project; `write_documents` remains the atomic document-set operation.
Plural reads/removals preserve input order and return one named `documents`
collection with per-path outcomes and counts. Removal is explicit-path only and
never recursive; backups still precede deletion.

Writes and reads accept opt-in `content_encoding="base64"` for exact original
bytes, while existing UTF-8 defaults remain compatible. Exact bytes and backups
are retained for rollback; an older server may be unable to index newly accepted
malformed UTF-8 and must not normalize or delete it. `find_literal(compact=true)`
and asset `detail="summary"` are opt-in projections: default aliases, fields,
ordering, and cursors remain unchanged. Self-test plans expose stable full/index/
group/exact sections, encoded B1/B4 fixtures, independent read-back, and measured
connector-call/serialized-response totals without promising a fixed target.

## 9.1.1 — 2026-09-15

**Bounded OAuth discovery diagnostics.** Expected fallback 404s for the standard
`openid-configuration` and `oauth-authorization-server` metadata names now log at
DEBUG across their RFC-style and MCP protected-resource URL shapes. Unknown
well-known names, malformed lookalikes, and malformed MCP paths remain warnings;
request-path secret redaction is unchanged.

## 9.1.0 — 2026-09-15

**Live PostgreSQL asset catalog compatibility.** Asset catalog rows returned by
PostgreSQL retain their asset identity and metadata in list, info, and keyword
search responses. Catalog metadata and stored idempotency results encoded as JSON
strings are decoded safely, so exact replays of asset writes return the original
result with `idempotent_replay: true` and metadata revisions continue to increment.

## 8.1.0 — 2026-09-14 (candidate)

**RFC 9207 authorization issuer.** OAuth authorization-code success and error
redirects now include the configured public issuer in the iss parameter. The
issuer is fixed from public_base_url, and existing callback query parameters,
state, PKCE, and Toolkit redirect validation remain intact. OAuth metadata
advertises support for the authorization-response issuer parameter.

---

## 8.0.1 — 2026-09-14 (candidate)

**On-demand OAuth readiness.** After the bounded startup wait, Cognita no longer polls
the child readiness endpoint on a timer. The shared gateway and admin paths check the
same child when health or an OAuth-dependent operation needs it, while a background
watcher reports an actual child exit promptly without respawning. Live children can
recover on a later authenticated check; exited children remain unavailable until an
explicit parent restart. Existing HTTPX/readiness logging and token ownership are
unchanged.

**Self-test rates are informational.** G9 now records embedding throughput without a
minimum per-card pass/fail floor; correctness, canary, release, and VRAM checks remain
required. This 8.0.1 candidate is not a deployment claim; deployment follows its
review checkpoint.

---

## 8.0.0 — 2026-09-14

**OAuth child-service candidate.** The parent now supervises one loopback-only Django
OAuth Toolkit child and forwards public OAuth traffic through a bounded authenticated
client. Toolkit owns OAuth client records, authorization codes, access and refresh
tokens, persistence, refresh handling, introspection, and revocation. Parent health
reports child unavailability as degraded and authentication-dependent operations return
503; the admin project list remains usable without fabricating zero connections. The
candidate also includes bounded child recovery and shutdown behavior plus an explicit
offline legacy migration command.

**Stable package-owned refresh policy.** Cognita configures Toolkit to reuse each
refresh token without rotation, retry grace, age expiry, or replay-family invalidation.
Token values remain hash-only at rest. Ordinary restart, admin-password change, and
project disable do not revoke connections; explicit RFC 7009/client or Connected Clients
admin revocation persists across restart. The one-shot
`cognita serve --revoke-oauth-tokens-on-start` emergency action revokes every connection
through Toolkit before readiness, then continues startup without deleting the database
or client registrations.

Selected refresh-policy acceptance, production-copy migration and rollback rehearsal,
complete host acceptance, and final quiescent import passed. Version 8.0.0 is deployed.

---

## 7.3.0 — 2026-09-13

**Quiet OAuth discovery diagnostics.** Missing Authorization on a project MCP
discovery request still returns the same 401 bearer challenge, but its
coalesced application diagnostic is INFO. Invalid or expired bearer tokens remain
WARNING so actionable authentication failures stay visible. Coalescing keys,
windows, suppressed counts, and protocol responses are unchanged.
## 7.2.0 — 2026-09-13

**OAuth refresh rejection diagnostics and retry-storm containment.** Refresh
requests validate their client, resource, and admin-credential binding before any
replay-induced revocation. Already revoked grants reject repeated attempts without
rewriting their revocation time or updating the whole refresh family again.
Structured rejection categories and non-secret client/grant references distinguish
unknown credentials, binding mismatches, replay, and prior revocation. Repeated
OAuth token and MCP authentication rejection messages are coalesced with bounded
in-memory state; per-request HTTP access logs remain available.

Refresh tokens still rotate, only hashes are stored, and a correctly bound replay
still revokes the entire grant. Concurrent duplicates, stale caches, and lost
successful responses require fresh authorization; this release does not restore
already revoked connections. Recovery instructions and the confirmed September 12
replay sequence are documented in the README and OAuth incident record.

## 7.1.1 — 2026-09-10

**Complete asset self-test protocol.** The server-provided writable self-test now
requires deterministic checks for all seven PNG asset tools, including byte and
metadata verification, replay and conflict handling, optimistic concurrency,
pagination, both search modes, reindexing, and bounded-input rejection. Read-only
self-tests exercise every asset read endpoint when an existing asset is available.

## 7.1.0 — 2026-09-10

**PNG asset workspace.** Core-engine projects now expose seven additive asset tools
for publishing, updating, searching, listing, inspecting, retrieving, and reindexing
static PNG files. Writes accept strict PNG data URLs, preserve received bytes in the
normal backup tree when metadata embedding changes the file, and use durable operation
IDs, verified backups, atomic publication, catalog transactions, and startup recovery.

Asset metadata follows the Cognita image-metadata v1 schema and can be embedded in a
bounded iTXt chunk or held catalog-only for provenance-sensitive PNGs. Asset search is
isolated from document retrieval and reuses Cognita's dense, lexical, RRF, and reranking
pipeline. Read-only policy, per-project write serialization, watcher reconciliation,
pagination, configured lower limits, and privacy-safe logging apply across the surface.

## 7.0.4 — 2026-09-09

**OAuth callback navigation fix.** The consent page now permits its one validated
callback origin in the `form-action` Content Security Policy. Browsers can therefore
follow the authorization response to Claude or a local Codex callback while all
other form destinations remain blocked.

## 7.0.3 — 2026-09-09

**OAuth diagnostics.** Authorization, token exchange, and rejected MCP requests now
record privacy-safe correlation details, including a hashed flow identifier,
registration type, callback route, rejection reason, and Cloudflare Ray ID. OAuth
codes, tokens, PKCE material, state, and credentials remain excluded from logs.

## 7.0.2 — 2026-09-09

**Dark OAuth consent.** The public authorization page now uses a dark color scheme
with accessible contrast for the consent card, credential fields, errors, and action
buttons. OAuth protocol behavior is unchanged.

## 7.0.1 — 2026-09-09

**Admin asset cache migration.** The 7.0 admin page now loads its JavaScript with a
versioned URL. Browsers that cached the 6.x script no longer run that incompatible
code against the redesigned OAuth HTML and remain stuck showing client data as
“Loading…”. No OAuth protocol or MCP tool behavior changed.

## 7.0.0 — 2026-09-09

**OAuth-only production authentication.** Every project now has a non-secret
`/mcp/{project}` connector URL and a browser authorization flow compatible with MCP
clients using DCR or CIMD. The server publishes protected-resource and authorization
metadata, requires authorization code + PKCE-S256, issues one-hour project-bound
access tokens, rotates refresh tokens, detects refresh reuse, and supports revocation.

OAuth secrets are opaque and stored only as SHA-256 hashes in a restricted SQLite
store. Public client metadata fetching has an HTTPS allowlist, public-address checks,
no redirects, and response/time limits. The consent page uses the existing Cognita
admin identity without exposing the admin application or creating a persistent public
login session.

Admin password storage moves to Argon2id. The old SHA-256 field permits migration-time
LAN admin login only; OAuth refuses to start until the credentials script replaces it.
Changing credentials invalidates admin sessions and OAuth grants.

The admin page now shows connector URLs and connected clients, supports grant
revocation, and no longer mints an API key with every project. Static API keys and the
legacy secret URL are accepted only when the process is explicitly started with
`cognita serve --test`; release mode ignores them and logs a secret-free, rate-limited
error. Test mode cannot be enabled from configuration or the environment.

## 6.4.2 — 2026-09-05

**US English everywhere.** A sweep of every tracked file replaced 319 British
spellings with their US forms — `behaviour` → `behavior`, the whole `-ise`/
`-isation` family → `-ize`/`-ization`, `artefact` → `artifact`, `cancelled` →
`canceled`, and the rest. Prose, comments, docstrings, test names and one
identifier (`gpu_probe.normalise_cards` → `normalize_cards`, internal, no wire
shape touched).

Two strings a client can actually see moved: the `gpu_cards` rejection now reads
`unrecognised value` → `unrecognized value`, and the same word in the GPU worker's
exit-code description. No tool name, argument or result key changed.

Words where `-ise` is correct in US English too (`advise`, `comprise`, `exercise`,
`promise`, `revise`, `supervise`, `surprise`, …) were left alone, as was
`asyncio.CancelledError` and the plural noun `analyses`.

## 6.4.1 — 2026-09-02

**The available cards are listed in the log at startup, at INFO level.** 6.4.0
shipped the `gpu_cards` setting but printed its numbering only in `/healthz` and
in an `embed.plan` line emitted the first time a GPU job ran — so answering
"which card is 1?" needed an HTTP call or an index that had already happened,
and the person who needs the answer is configuring the box before either.

```
GPU cards   : 3 found, 2 usable  [gpu_cards=all]
   card 0  card1  0000:03:00.0    31.80 GB free   usable
   card 1  card2  0000:07:00.0    31.80 GB free   usable
   card 2  card3  0000:7e:00.0     1.86 GB free   unusable: vram_free=1.86GB<6.19GB
   Set `gpu_cards` in config/cognita.yaml to choose — the number after `card` is
   what to write (e.g. gpu_cards: [0, 1]).
```

Cards are listed even when acceleration is off, since someone about to enable it
needs the indices first. A machine with no cards says so in one line. An index
naming no card is warned about here too — the earliest point it can be caught.
README and the config template point at it.

## 6.4.0 — 2026-09-02

**`gpu_cards`: choose which graphics cards Cognita may use.** New optional config
key, documented in `config/cognita.example.yaml` and the README. Default `all`,
which is what every existing install already did — **no existing configuration
changes behavior.**

```yaml
gpu_cards: all      # every card present (default)
gpu_cards: none     # no card at all; index on the CPU
gpu_cards: 1        # only the card Cognita lists as 1
gpu_cards: [0, 2]   # only those two
```

Observable changes for a client:

- `/healthz` gains `gpu_cards` (the selection in force) and a `card` index on
  every entry in `devices`. That index is the value to write in `gpu_cards`.
- `/healthz` gains `gpu_cards_warnings` when the setting names a card that does
  not exist. Absent otherwise. This exists because every GPU fault falls back to
  the CPU, so a typo'd index is otherwise invisible — a correct index, several
  times slower, with nothing to say why.
- `devices_skipped` entries now carry the card index: `card3[2]:vram_free=...`
  rather than `card3:vram_free=...`, and a card excluded by configuration names
  the setting that excluded it (`not in gpu_cards=[0]`) instead of always
  claiming `gpu_device_ids`.
- Device discovery is ordered by **PCI address** rather than by sysfs card name,
  so an index means the same physical card across reboots. The reference machine
  is unaffected — `card1`/`card2`/`card3` sort identically either way — but a box
  with ten or more cards was previously ordered `card1, card10, card2`.

No MCP tool shape changed.

## 6.3.0 — 2026-09-02

**The GPU batch shape was 64 and should have been 4. Indexing is ~39% faster and
a small write on a warm card is 23x faster.**

`gpu_worker.embed_batch` pads every slice out to a full `gpu_batch_size` and
discards the surplus, so the batch size is both the compiled tensor shape and
the floor on what a one-chunk write costs. 64 was justified by "64 measured as
fast as anything larger" — true, and beside the point: nobody had measured
anything smaller. A controlled sweep used one compiled shape per worker, arms interleaved,
two runs sharing batch 4 as a control (agreeing to 1.4%):

| batch | bulk (256-chunk slice) | a 1-chunk write |
|---|---|---|
| 2 | 46.2 chunks/s | 0.044s |
| **4** | **49.5 chunks/s** | **0.081s** |
| 8 | 45.9 chunks/s | 0.176s |
| 16 | 48.6 chunks/s | 0.329s |
| 64 | 35.7 chunks/s | 1.832s |

Bulk is flat from 2 to 16 and collapses at 64; small-write cost is exactly
proportional to the batch (~0.021–0.029s per padded sequence), so
`64 × 0.0286 = 1.83s` — precisely what a single-chunk write cost in production.

### Changed

- **`gpu_batch_size`: 64 → 4.** Faster at both ends. ⚠️ The first walk after
  upgrading pays one ~25–38s MIGraphX compile for the new shape and writes
  ~1.4GB into `gpu_program_cache_dir`. Once.
- **`gpu_warm_min_chunks`: 16 → 0** — the floor is removed. At 0.081s there is
  no size worth declining a card that is already up. It remains a config key for
  a box with the opposite problem (contended cards, idle CPU).

### Fixed

- **A one-line write on warm cards now takes ~0.08s instead of 1.83s**, and runs
  on the GPU rather than pegging 32 CPU cores. 6.2.1's floor was derived from
  wall clock alone and ignored what a CPU embed costs on a shared box.

### Notes

- Self-test **G8 is reversed**: it asserted `decision=cpu` for a one-line write
  and now asserts `decision=gpu pool=warm` under 0.2s. New **G9** checks the
  reported rate is ≥ ~45 chunks/s per card — mid-30s means the old shape.
- No tool's wire shape moved.

---

## 6.2.1 — 2026-09-02

**A warm claim is cheap, not free — measured, and given a floor.**

6.2.0 sent every chunk to a pool that was already up, on the reasoning that a
claim costs nothing. A controlled measurement on one document family found:

| arm | measurement | derived |
|---|---|---|
| CPU, 172 chunks | 21.145s | 8.1 chunks/s |
| warm GPU (2 cards), 686 chunks | 12.97s `embed_wall` | 52.9 chunks/s |
| warm GPU, 1 chunk, ×4 consecutive | 1.813 / 1.787 / 1.791 / 1.812 s | ~1.80s fixed, **per call** |
| CPU, 1 chunk | ~0.025s | — |

Four flat samples make the 1.80s a per-call round-trip cost, not a first-call
effect. Break-even is `1.80 / (1/8.1 - 1/52.9)` = **17.2 chunks**.

### Added

- **`gpu_warm_min_chunks` (default 16)** — the floor for claiming a WARM pool,
  set just under the measured break-even. `0` restores 6.2.0's behavior. It is
  a separate number from `gpu_min_chunks` (300) on purpose: one prices a ~1.8s
  round-trip, the other a ~33s shape compile.

### Fixed

- **A trivial write no longer pays 1.8s to use a warm GPU.** A one-paragraph
  note went from ~0.025s (6.1) to ~1.80s (6.2.0) and is back to ~0.025s.
  Everything from ~16 chunks up still rides the warm pool: 172 chunks is 5.0s
  against 21.1s on the CPU.
- **A walk no longer latches "CPU" for its whole duration** when the estimate
  says so. With lingering on, another job can park a pool at any point during a
  long walk; the latch (correct in 6.1, where nothing could change the answer)
  meant the walk never asked again.

---

## 6.2.0 — 2026-09-02

**The GPU stays warm between jobs, and while it is up it takes every chunk.**

The warm pool now measures the claim cost and applies a minimum useful-work threshold.

### Added

- **A pool that finishes a job now stays alive for `gpu_idle_linger_s` (default
  30s) instead of being torn down immediately.** The next job claims it and pays
  nothing for start-up. Every claim resets the clock, so a burst of edits keeps
  the cards; a genuinely idle period reaps them, releasing the VRAM and the
  lease.
- **`gpu_idle_linger_s` config key.** `0` restores the 6.1 behavior exactly
  (tear the pool down at the end of every job). Negative is refused at startup.
- **`/healthz` reports `gpu_warm`** — devices, idle seconds, seconds until the
  reap — when a pool is parked. A held `gpu_lease` no longer implies a running
  job, so the two are now distinguishable without reading the log.
- **`embed.gpu.warm` log lines** (`park` / `claim` / `reap` / `discard` /
  `shutdown`) and a `pool=warm|cold` field on `embed.plan` and `embed.done`.

### Changed

- **A small write now uses the GPU when one is already running.**
  `gpu_min_chunks` still decides whether a job is worth a *cold* start — that
  meaning is unchanged — but it no longer gates work when there is no start-up
  to amortize. Observable as `decision=gpu` on documents far below the
  threshold, and only ever when a pool is already up.
- **`embed.gpu.device` rows are emitted by the teardown itself**
  (`GpuPool.shutdown`) rather than by each caller, in one shape for both the
  walk and the single-document path. A pool now outlives the job that used it,
  so a caller logging cumulative device totals at the end of *its* job would
  report a later job's numbers as its own.

### Unchanged

- The §9.1 canary is not re-run on a claim: it proves this machine's GPU agrees
  with this machine's CPU, which cannot have changed for the same processes with
  the same model.
- Search queries still embed on the CPU.
- No tool's wire shape moved.

---

## 6.1.2 — 2026-09-02

**The self-test plan now asserts on the payload that carried 6.1.1's bug.**

### Changed

- **Step 40 asserts `index_drift: false` and the three hashes agreeing**, on the
  `get_document` that follows an `edit_document`. That is the exact call whose
  payload reported `index_drift: true` beside three identical hashes — in two
  separate self-test runs, with no step failing either time, because nothing
  asserted on it. The step also now says what the shape means, so a future run
  reports the four fields instead of re-deriving the significance from scratch.
  6.1.1 fixed the defect; this is what stops the next one of its kind hiding for
  five releases.

---

## 6.1.1 — 2026-09-02

**`index_drift` is decided on CONTENT, not on the stat.** Both defects here were
found by reading response payloads during the 6.1.0 self-test run; neither
failed a step, which is the point — an assertion nobody wrote is a signal
nobody checks.

### Fixed

- 🔴 **`index_drift: true` on a file that had not changed by one byte.**
  `get_document` and `list_documents(include_hashes=true)` decided drift purely
  by comparing the on-disk stat against the stat the index stored, so ANY change
  to `st_mtime` reported drift beside three identical hashes
  (`content_sha256` == `bytes_sha256` == `indexed_sha256`). The cause is a
  second writer that changes no content: `onedrive --monitor` rewrites the local
  mtime to WHOLE-SECOND precision after it uploads a file, so a document indexed
  at `10:11:19.933396` stats at `10:11:19.000000`. Some sync clients reduce
  timestamps to whole-second precision after uploading a file.
  It self-cleared within minutes — the next walk re-stats, finds the content
  identical and refreshes the row — which is why it read as cosmetic; what it
  actually did was make the one signal `include_hashes` exists to produce
  unreliable on the deployments most likely to need it. Seen twice: 5.6.3 on
  2026-08-31 and 6.1.0 on 2026-09-02.

  The stat is now a pre-filter and the bytes are the verdict.
  `get_document` parses the file anyway, so it settles drift with that
  extraction hash outright, in both directions — a stat that agrees can no
  longer mask a real change either. `list_documents` hashes every file for the
  manifest anyway, so it clears a moved stat whenever the indexed hash AGREES
  with a hash of the current bytes. Disagreement still falls back to the stat,
  deliberately: `content_hash` is the hash of the EXTRACTION, so for a PDF, a
  .docx or a .md with frontmatter it legitimately matches neither file hash, and
  reading that as drift would trade a false positive for a permanent one.
  Regression suite: `tests/test_manifest.py`.

- **`get_self_test_plan` step 47 told every run to expect a tier it could not
  get.** The step said "the .md goes to the embedded tier and the .txt to the
  registered tier"; `.txt` is embedded-tier, as the same plan's step B2 and
  `get_index_stats` both say. The step tested one tier twice while its heading
  claimed two. The second fixture is now `fresh.py`, so the step tests what its
  title says, and the expected tiers and chunk counts are stated outright.

---

## 6.1.0 — 2026-09-02

**New tool: `write_documents` — write N documents as one unit, or write none.**
Additive; every existing tool is unchanged, so no connector needs
re-registration.

### Added

- 🔴 **`write_documents(documents=[{filepath, content, category?,
  expected_sha256?}, ...])`.** For documents that form a **set** which must
  agree with each other — sections of one work, a file plus a manifest compiled
  from it, anything where half-applied is worse than not-applied.

  **The failure it exists for.** A client pushing such a set issues N separate
  `update_document` calls, and each one writes its file and then spends ~6
  seconds embedding. A fault anywhere in that window leaves some files new and
  some old — and **every file is individually valid**, so nothing downstream can
  tell the set is inconsistent. On 2026-09-02 a service restart cut a live
  13-section worldbook push: four sections had the new shape, nine did not, and
  the compiled artifact was built from the nine. Nothing looked broken.

  6.0.12 made each *call* honest. It could not make the *sequence* atomic,
  because Cognita never knew a sequence existed. This tool is the caller
  saying so.

  Four phases: **validate everything** (a rejection anywhere writes nothing and
  reports `documents_written: 0`), **stage everything** as sibling temp files,
  **publish** with N `os.replace` calls, then **index** — and any indexing
  failure, `CancelledError` included, restores every file and returns an error.
  A filepath may appear only once per batch; 100 documents per call.

⚠️ **What it does NOT claim.** This is not a journal. A `SIGKILL` landing
between two renames still leaves a split set. The window in which a partial set
is observable shrinks from the many seconds of a per-document loop to the
microseconds of N renames — orders of magnitude, not zero. Closing it entirely
needs an on-disk intent log and a startup recovery pass, which is a larger
feature and is deliberately not what shipped here.

📌 The self-test plan gains step 33b, which exercises the tool and then proves
the all-or-nothing property by sending a valid document alongside an
unindexable one and checking the valid one did **not** change.

### Two things a new mutating tool must join, found by its own tests failing

- **`readonly.MUTATING_TOOLS`** — this set gates the engine's **per-project
  write lock**, so a mutating tool omitted from it runs unserialized against a
  full rebuild and against every other write. Nothing errors; the races are
  simply no longer prevented. It also drives the read-only refusal and the
  `tools/list` annotation.
- **The gateway's backup path.** `_handle_engine_write` is built around a single
  `arguments.filepath`, and a batch has none — so it would have taken the "no
  filepath to lock/back up" branch and forwarded the write with **no backups and
  no edit lock**, silently. `write_documents` gets its own handler
  (`_handle_batch_write`) which locks every target in sorted order (two batches
  sharing files in opposite orders is otherwise a deadlock that hangs the whole
  project's write lock), backs up every existing target before anything is
  written, aborts the entire call if any backup fails, and returns
  `previous_backup_ids` as a `{filepath: backup_id}` map.

---

## 6.0.12 — 2026-09-02

**A write cut off by a shutdown left its content on disk while telling the
caller it had failed.** No tool changes shape. This is a data-integrity fix.

### Fixed

- 🔴 **The write rollback caught `Exception`, and the failure that happens in
  production is a `BaseException`.** `add_document` and `update_document` write
  the file verbatim, then parse/chunk/embed (~6s), then commit the index —
  rolling the file back if the index step throws. But `asyncio.CancelledError`
  derives from `BaseException`, so **uvicorn canceling an in-flight request at
  shutdown walked straight past the rollback**. Every other fatal path was
  handled; the only one that actually occurs was not.

  The consequence is worse than a lost write: the caller is told the call
  failed while the new bytes sit on disk, so **"it errored, therefore nothing
  changed" was false**. A client pushing a multi-document set — 13 worldbook
  sections plus a compiled artifact built from them — could have some writes
  land, one report failure while landing anyway, and the rest never issue. Every
  file stays individually valid and the set is inconsistent, which is the
  failure mode nothing catches by looking. Observed live on 2026-09-02.

  Now caught as `BaseException`, and the undo runs **synchronously** — you
  cannot `await` your way out of a cancellation, so an `await` in the handler
  would be canceled too and skip the rollback entirely.

- 🔴 **The rollback could itself destroy the document.** `_undo_write` restored
  with `write_bytes`, which truncates in place — interrupt the recovery and the
  file is left half-written by the very code protecting it, with the original
  bytes now nowhere to retry from. It stages and `os.replace`s like
  `_write_verbatim` does, so an interrupted restore leaves the file untouched.

⚠️ **A rollback never turns a failure into a success.** The error still
propagates to the caller. The change is only to what is left on disk.

---

## 6.0.11 — 2026-09-02

**Every multi-card GPU walk understated its own throughput by roughly the
device count.** No tool changes shape; this is the `embed.done` summary line.

### Fixed

- 🔴 **The job-level `rate` divided by the SUM of per-device elapsed.** With one
  device that is correct. With two cards working concurrently the sum is up to
  twice the wall time they actually spent, so the reported rate was half the
  real one — and it got worse in proportion to the number of cards, i.e. the
  better the hardware, the bigger the understatement. A two-card rebuild
  reported **14.9/s** for 12,589 chunks it embedded at
  **~29/s**. The per-device rows were right the whole time; only the line
  everyone reads first was wrong, which is the same shape as the 6.0.7 defects.

  `rate` now divides by a new `embed_wall` field: the **union** of the batches'
  busy intervals. Deliberately a union and not `last_end - first_start` — a span
  would sweep in the gaps where the walk is parsing and writing to Postgres,
  which is most of a CPU walk's clock, and §14.4 nominates `rate` as the number
  that answers "is the GPU worth it" **by comparing the two paths**. On a single
  device the intervals are disjoint, so the union is the sum and **the CPU
  line's rate does not move by a digit**. `elapsed` stays on the line as
  device-seconds; `wall` still includes everything.

### Added

- **`embed_wall`** on `embed.done` — how long something was actually embedding,
  sitting between `elapsed` (device-seconds) and `wall` (the job's whole clock).

---

## 6.0.10 — 2026-09-02

**Log-label fix.** No behavior change beyond the wording of one DEBUG line.

### Fixed

- **Two different events were both logged as `slice out`.** 6.0.8 gave the
  parent a per-slice trace line under that label, and the worker already used
  `slice in` / `slice out` from its own point of view — so a live trace showed
  the parent's request and the worker's reply under one name, four lines apart.
  The parent's line is now `slice send`. Only visible with `gpu_log_debug`.

---

## 6.0.9 — 2026-09-02

**Cognita was killing its own GPU workers.** Since 6.0.5, on every machine with
two or more qualifying cards, the GPU was never used at all — every walk fell
back to the CPU. No tool changes shape; this restores the accelerator.

### Fixed

- 🔴 **`PR_SET_PDEATHSIG` is keyed to the spawning THREAD, not the parent
  process.** The worker armed `prctl(PR_SET_PDEATHSIG, SIGKILL)` so it would not
  outlive a `SIGKILL`ed service (§8.6). But the kernel delivers that signal when
  **the thread that created the child terminates** — and 6.0.5 made spin-up
  concurrent, spawning each worker from a short-lived thread that returns the
  instant the handshake lands. So the kernel `SIGKILL`ed every worker
  milliseconds after it came up, before the canary could complete. Replaced with
  a `getppid()` watcher thread in the worker (`--parent-pid`), which has no
  coupling to which thread called `Popen`.

  Three coincidences hid it for four releases: `_run_per_worker` runs a *single*
  device inline on the caller's thread, so one-card boxes worked and every
  one-card reproduction passed; §10's CPU fallback is silent and correct, so the
  only symptom was a slower walk; and the parent never read the exit status, so
  `killed by signal 9` never reached the log. That last one is what 6.0.8 fixed,
  and it is how this was found — the first `embed.gpu.died` line named it.

  Observed as repeated double-card failures, each logging only
  `worker exited mid-slice`. Proven by
  spawning the same two workers from the main thread (both canaries pass) and
  from a thread that exits (both `SIGKILL`ed), seconds apart in one process.

---

## 6.0.8 — 2026-09-02

**A GPU worker that dies now says why.** No tool changes any shape; this is
entirely about the log, and it follows repeated live failures that produced no
usable evidence.

### The failure this fixes

Between 2026-09-01 23:17 and 2026-09-02 01:43, three double-card GPU spin-ups
died on their first inference. All three logged the same two lines and nothing
else — no exit code, no signal, no stack, no output from either worker:

```
ERROR cognita.gpu: gpu.worker[card1] canary could not run: worker exited mid-slice
ERROR cognita.gpu: gpu.worker[card2] canary could not run: worker exited mid-slice
```

Each time the walk completed correctly on the CPU (§10 held), so nothing was
lost but the speed — and the cause. It is not reproducible on demand: it only
appears under concurrent load, and a single worker on the same build, same box
and same program cache passes its canary every time.

### Fixed

- **The worker's exit status was read and discarded.** `Popen.returncode` was
  available on every death path and never consulted, so `gpu_worker.main()`'s
  exit codes — which name the stage that failed — never reached the log. A
  **warmup** failure (code 3, the first inference, before any slice) and a
  **slice** failure (code 5) were reported with the identical message, and the
  message named the slice. New `embed.gpu.died` ERROR line carries the device,
  pid, stage, exit code with its meaning, or the signal name for a native crash.

- 🔴 **The worker's dying words were being thrown away by our own teardown.**
  The stderr relay is a daemon thread; `terminate()` closed the pipe in its
  `finally` without joining it, and every caller terminates a failed worker
  immediately. So the sequence on every failure was: worker writes why it is
  dying → parent closes the pipe → relay cut off mid-read. DESIGN-6.0 §14.6 had
  asserted the opposite behavior since 6.0.0, which is why nobody looked. The
  relay is now drained before the pipe closes, and the parent keeps an 80-line
  tail so the report quotes the cause rather than pointing at a line that may
  never have landed.

- **Native crashes left no stack trace, in a process that is mostly C++.**
  HIP, MIGraphX and ONNX Runtime can SIGSEGV or `abort()` where Python's
  `except` cannot see it — the process simply vanishes. The worker now enables
  `faulthandler` for all threads, so a native fault writes its stack to stderr
  and the relay carries it into the log.

- **A wedged worker was killed without ever recording where it hung.** The
  worker registers a `SIGUSR1` stack dump; the parent signals it and waits
  before terminating on a slice timeout.

- **Worker exception logging was `TypeName: message` with no traceback.** Now
  the full traceback, one leveled line per frame.

### Added

- **`gpu_log_debug: true`** (config) — the GPU path's per-spawn, per-slice,
  per-teardown and concurrent-spin-up trace, without dropping every other logger
  to DEBUG. Intended to be left ON while an intermittent fault is being chased.
  Death reports are never gated on it.

---

## 6.0.7 — 2026-09-01

**The logs now tell the truth about performance.** No tool changes any shape;
these are all observability fixes, and each one made a number that a human reads
say something false.

### Fixed

- **The GPU's measured throughput was understated by roughly 3x.** The §9.1
  canary is the *first* inference on a device, so it pays the one-off 25-38s
  MIGraphX shape compile — and it was recorded as ordinary work. On the first
  live GPU walk a card genuinely doing ~73 chunks/s reported ~28, because ~30s
  of compile sat inside its `elapsed`. §14.4 nominates `rate` as the number
  answering "is the GPU still worth it", so that is precisely the field it
  corrupted. The canary is now timed separately and reported as `canary_s` on
  the per-device line. **This is also the explanation for the gap between
  §5.2's 148.3 chunks/s and §12.5's measured ~28** — most of it was never a
  discrepancy, it was the compile being counted as embedding.

- **A pure-GPU walk logged `device=cpu chunks=1`.** The canary's CPU reference
  ran inside the open job, so one non-corpus chunk appeared as CPU work on every
  GPU walk and fed into `est_error`.

- **Failed embeds counted as work completed.** `chunks` was recorded as
  `len(texts)` on the failure path, so a walk that aborted mid-window logged
  `chunks=512 ... indexed=0`, and anything summing `embed.done chunks=` counted
  vectors that are not in the index. Time spent is still recorded — that part
  was deliberate and is unchanged — but `chunks` now reports what was produced.

- **Two different record shapes were both called `embed.done`.** The job summary
  (§14.4, "one line") already carries a `device=` row per worker, and the
  per-device hardware lines used the same event name — so `grep 'embed.done' |
  sum chunks=` counted a 2,036-chunk walk as 6,108. Those lines are now
  `embed.gpu.device`, alongside `embed.gpu.init` and `embed.gpu.canary`, and the
  CPU-fallback line is `embed.gpu.fallback`.

- **`est_error` was permanently wrong on any corpus containing a PDF.** §4.2
  excludes binary formats from `est_chunks` by design — a PDF's size says
  nothing about its text — but their chunks land in the actual, so the error was
  a large positive number for ever regardless of the §4.1 formula's accuracy.
  Since §14.4 calls `est_error` "the only place a drift would show up", a value
  that reads as signal and cannot move is worse than none: it now reports
  `est_error=n/a est_error_reason=binary_formats`.

---

## 6.0.6 — 2026-09-01

**The sequence-length pin, and the documentation the feature owed.** No tool
changes any shape.

### Fixed

- **`gpu_fixed_seq_len` could silently shorten documents on the GPU only.** The
  pin sets truncation *and* padding, and padding to a fixed shape is numerically
  inert while truncation changes the text the model sees. Pinned below a model's
  own context — 512 under an 8192-token model, say — every long chunk was
  truncated on the GPU path while the CPU path kept the full text, leaving one
  corpus holding two incompatible embeddings of the same document. §9.1's canary
  is ~12 tokens long, so it agrees to 1e-07 however badly this is set and could
  never have caught it. The pin is now clamped to the model's own maximum, with
  a warning naming the direction. Inert on the shipped `bge-large-en-v1.5`,
  where both values are 512 — this was a trap for the first person to change the
  embedding model.

- **The worker's startup handshake could claim a pin that failed.** The pinning
  result was discarded and the requested value reported regardless, so a worker
  whose shapes actually varied — and which therefore recompiled per batch, the
  one thing the pin exists to prevent — announced a fixed shape. It now reports
  what was actually pinned, or nothing.

- **`embed_batch` materialized a whole slice** in a list comprehension while its
  own docstring claimed to follow 5.8's streaming lesson. It now streams, so
  peak transient memory is one batch rather than the slice.

### Added

- **The self-test plan covers the GPU (G1-G6).** Skipped and reported as such
  unless `/healthz` says a device is ready. The steps assert what the
  accelerator must not change: that a `decision=gpu` summary is backed by a
  device row with real chunks, that semantic search over GPU-embedded vectors
  works (the query is embedded on the CPU, so this is the end-to-end proof the
  two agree), that a canary line exists per device, that the VRAM came back, and
  that the walk is green regardless.

- The change records the measured performance and verifies it against the shipped behavior.

---

## 6.0.5 — 2026-09-01

**Startup is concurrent across devices, and three quieter traps closed.** No
tool changes any shape.

### Changed

- **Multi-GPU spin-up is now concurrent (§6.3).** Both per-device startup steps
  — spawning each worker and proving it with the §9.1 canary — ran back to back,
  so N devices cost N spin-ups in sequence, all of it inside the project write
  lock where concurrent writes are *refused* rather than queued. Measured on the
  two reference cards: card1 ready at 22:59:05.78 and card2 at 22:59:08.78, then
  canaries two seconds apart again. Both steps now run at once and device order
  is preserved, so wall-clock start-up is one device's regardless of how many
  join — which is what §6.3 promised.

### Fixed

- **The GPU venv build script reported success for a missing library.** The
  check was `ldd <lib> | grep 'not found' || echo "all resolve"` — if the file
  does not exist, `ldd` fails, `grep` matches nothing, and the `||` branch prints
  success and exits 0. The soname was hardcoded to migraphx-libs 2.17.0, so the
  next version bump was precisely the case that triggered it, and the failure
  stayed silent until runtime, where ONNX Runtime falls back to the CPU. The
  script now fails loudly, and matches whatever `libmigraphx_gpu.so.*` shipped
  instead of pinning a version.

- **`fastembed` is pinned exactly, in both environments.** `pyproject.toml` said
  `>=0.7` while the worker's build script said `==0.8.0`; they matched only by
  accident. The service and the worker are separate environments sharing one
  model cache, and fastembed picks its ONNX artifact from version-specific
  metadata — so a drift between them could have had the two processes load
  different artifacts from that shared cache, which is the exact divergence a
  single cache exists to prevent.

- **A short pipe write could wedge a worker.** `write_frame` discarded the byte
  count from `write()`. Both ends are raw unbuffered pipes, so that is a single
  `write(2)`, and a signal arriving mid-transfer returns short — with frames of
  ~512 KB per request and ~2 MB per reply, far past `PIPE_BUF`. The remainder
  was dropped, desyncing the stream: the peer reads a length that is really
  payload and blocks for bytes that never arrive, until the slice timeout kills
  a healthy worker. The read side has had `_read_exactly` for this reason since
  it shipped; the write side was the asymmetry.

---

## 6.0.4 — 2026-09-01

**Two ways a GPU could take a project down, both bounded now.** No tool changes
any shape.

### Fixed

- **A worker wedging during startup hung the walk forever, holding the project
  write lock.** The startup handshake was an unbounded blocking read, and the
  worker only answers *after* model resolution (possibly a first-run download),
  the HIP init and the MIGraphX shape compile — any of which can wedge on a
  missing model cache, an unreachable network or a card in a bad state. Because
  the walk holds the write lock, and concurrent writes to a locked project are
  *refused* rather than queued, the symptom was a reindex stuck at
  `active: true` indefinitely with every connector write to that project
  failing and no log line after `embed.plan`. Bounded by the new
  `gpu_worker_startup_timeout_s` (default 300s — generous, because a real first
  load takes minutes, but finite). `embed()` has had a timeout since it
  shipped; `start()` had none.

- **The device gate ignored the configured batch size.** `batch_ceiling_gb` was
  a hardcoded `3.62` in two unrelated places — the gate and `/healthz` — which
  is the measured figure for the default batch of 64 and wrong for every other
  value. Raising `gpu_batch_size` to fastembed's own default of 256 (the obvious
  "make it faster" knob, and nothing warned) left the gate admitting a card with
  7.7 GiB free to a worker that needs 8.2: it OOMs at inference, dies, its slice
  is retried on the identically-sized second card and dies there too — while
  `/healthz` reported `gpu: ready` throughout. The ceiling is now derived from
  the batch size through both of §12.3's measured points, and `/healthz` and the
  walk read the same function.

---

## 6.0.3 — 2026-09-01

**`copy_directory` uses the GPU as one job (DESIGN-6.0 §4.4).** No tool changes
any shape; what changes is how a bulk import is embedded.

### Fixed

- **A bulk copy decided per file, and both outcomes were wrong.** `copy_directory`
  loops `index_file`, and each file made its own GPU decision — so a copy of 500
  modest documents had every individual decision correct, fell under
  `gpu_min_chunks` every time, and never used the GPU once, on precisely the
  workload the feature exists for. After 5.21.0 made single-document writes
  GPU-capable, the other half appeared: a copy of 500 *large* documents paid a
  full pool spin-up per file — subprocess, model load, a 25-38s MIGraphX shape
  compile, canary, teardown — which is far slower than simply staying on the
  CPU. The estimate is now taken once over the whole file list and one pool and
  one lease are held for the duration, which is what §4.4 specified.

- **A nested caller could truncate the enclosing job's summary.** `embed_job`
  nests by joining, and `index_file` closed its job unconditionally, so the
  first file of a bulk job would have latched the shared summary and printed it
  — reporting a 500-document copy as having cost the chunks of its first file.
  A joined caller's `done()` is now ignored; only the opener closes the job.

---

## 6.0.2 — 2026-09-01

**Code review of the whole 6.0 range (b20e2aa..HEAD), and the failure paths it
found.** No tool changes any shape. Everything here is a fix to what happens
when something goes wrong, which is the half of 6.0 that had no test coverage:
not one spec in the range ever drove a real GPU pool, so the canary, the pool
teardown and the §8.6 termination could all have been deleted with the suite
still green.

### Fixed

- **A reindex whose parse-ahead producer died could delete documents it never
  looked at.** The producer always enqueues its end-of-stream sentinel, so a
  producer that failed after N of M files was indistinguishable from one that
  finished: the removal sweep saw N live documents and deleted the other M−N —
  present, readable files — while the walk returned `outcome: "ok"` with an
  empty `errors` list. The sweep is now skipped, loudly, when the producer did
  not complete. This is the highest-consequence fix in the release.

- **A GPU fault could fail an index the CPU would have finished.** §10's rule is
  that the walk always completes and the only variable is how long it took. A
  `check_canary` that raised anything other than `GpuUnavailable` propagated out
  of the walk instead. It now falls back to the CPU, as the single-document path
  always did.

- **A GPU failure could disable the GPU until the service restarted.** The pool
  was registered for teardown only *after* the canary ran, so a raise in between
  left worker subprocesses holding their VRAM and the process-wide lease held
  forever — every later walk and every later document write silently ran on the
  CPU, reporting `gpu_lease=busy`.

- **Device-yielding (§6.4) never fired.** The re-check subtracted a *card-wide*
  VRAM figure from its own threshold, so the more VRAM another application took,
  the more certain the worker was to keep the card. Measured: another process
  holding 27 GiB of a 32 GiB card still passed. It now compares against the
  worker's own settled baseline, so a rebuild gives the card back.

- **The §9.1 canary could not see a wrong vector width.** It compared with
  `zip`, which truncates to the shorter side, so a device returning a short
  vector passed on its prefix — the loudest possible "this provider is not
  computing what we think", invisible to the one check that exists to catch it.

- **A worker error outside the expected contract lost its slice**, surfacing as
  `KeyError` and forcing the whole window into a per-document CPU retry, with
  the real cause going to stderr instead of `cognita.log`.

- **A tier migration ran on the CPU.** Moving an extension between
  `registered_extensions` and `indexed_extensions` leaves every file
  byte-identical, so the estimator's mtime check called the largest job the
  product performs "unchanged" and estimated ~0 chunks. It now compares the
  stored tier, exactly as the walk itself does.

- **Trivial work no longer pays for the GPU.** The pool started on the first
  window holding *one* chunk, so a walk whose real work was two chunks still
  paid the full ~33s spin-up while holding the project write lock and refusing
  connector writes. It now starts once the accumulated real work clears
  `gpu_min_chunks`.

- **`embed.done` no longer reports `decision=gpu` for a walk that ran entirely
  on the CPU** after every device yielded or died.

- Nonsense GPU settings are refused at startup rather than deep in a walk. In
  particular `gpu_canary_tolerance: 1.0` used to leave the canary running,
  logging and passing while guarding nothing.

---

## 6.0.0 — 2026-09-01

**GPU-accelerated indexing, and the instrumentation that made it provable.**
Adds GPU-assisted indexing while retaining CPU fallback.
No tool changes any shape: `search_knowledge`, `add_document` and the rest
behave identically and need no connector re-registration. What changes is how
fast a large index gets built, and what `/healthz` tells you.

### Changed

- **`/healthz` gains an `embed` object.** It reports the CPU provider always,
  and when a GPU is configured, the devices that qualify with their free VRAM —
  plus, when one does not, **why**. "gpu: disabled", "gpu: enabled but
  gpu_venv_python is unset" and "gpu: no device qualifies" want completely
  different responses, and a bare `false` is the answer that sends someone
  hunting. Existing fields (`status`, `service`, `version`) are unchanged, so a
  client that reads only those sees nothing new.

- **Indexing can use GPUs.** A whole-corpus rebuild and any large document
  write are routed to every qualifying device, in parallel, and fall back to
  the CPU whenever one is unavailable.

  ⚠️ **A speed-up multiplier is deliberately not quoted here.** The figures from
  development came from throwaway benchmark scripts whose comparison arms ran in
  a fixed order on a GPU that ramps its clocks under load, so they measured a
  warm-up curve as much as anything else. What IS verified on the shipped build,
  from production logs: both cards are used, work splits evenly between them,
  vectors match the CPU's to 1.5e-07, and all VRAM is returned on completion.
  The honest number will come from `embed.done` lines on real rebuilds, which
  is exactly what the instrumentation below exists to produce.

- **Every embed is now timed and logged**, CPU and GPU alike, with the same
  field names on both paths (`embed.plan` / `embed.batch` / `embed.done`). A
  rebuild before and after enabling a GPU is comparable by diffing two log
  lines. This shipped first, on its own, and stands on its own.

### Fixed

- **A document edit no longer chunks a file it is about to skip.** A file whose
  mtime moved but whose bytes did not was being fully chunked and then
  discarded — wasted work that repeated on every watcher sync, so a live
  application rewriting unchanged files paid it forever. Introduced in 5.12.0,
  found on a production box, and regression-tested.

- **Single-document writes are no longer invisible in the log.** `update_document`
  re-embeds the whole document (it always has), but only whole-corpus walks were
  instrumented — so an edit could drive every core on the machine while every
  logged walk reported `chunks=0`. Both paths are now scoped the same way.

- 🔴 **A reindex no longer refuses concurrent writes while it starts up.** The
  GPU pool was started while holding the project write lock, and before any file
  had been parsed — so a walk could hold that lock for a minute or more spawning
  workers and compiling, and an `update_document` arriving in that window was
  REFUSED rather than delayed. Seen in production as three failed saves twenty
  seconds apart. Devices now start on the first batch that genuinely has work,
  so a walk with nothing to index never touches a card and never holds the lock.

### Notes for operators

- **Nothing turns on by itself.** `gpu_enabled` defaults to false and
  `gpu_venv_python` is empty, which is the state on every machine that has not
  deliberately built a GPU worker environment. With either unset, the CPU path
  runs exactly as before.
- **The GPU runs in a separate virtual environment**, never the service's. A GPU
  build of onnxruntime installs the same module as the CPU build, so a broken
  wheel in the service's environment would not degrade indexing — it would stop
  it. Two environments, always.
- **Vectors are FP32 on both devices** and agree to ~1e-7. A corpus indexed
  partly on CPU and partly on GPU needs no reconciliation.
- **A large document write uses a GPU too, not just a whole-corpus walk.** An
  edit re-embeds the entire document, so a one-line change to a big file is
  thousands of chunks — the expensive case, not the cheap one. Small edits stay
  on the CPU, where a card would only slow them down.
- **Set `gpu_program_cache_dir`.** Workers are short-lived by design so VRAM is
  returned between jobs, which means each one compiles the model graph at
  startup — measured at ~60s on the reference deployment. The cache turns that
  compile into a load. Budget ~1.4 GB per input shape, and do not put it on a
  tmpfs: at that size a RAM-backed /tmp trades the video memory this design
  carefully returns for system memory it never gives back.

### Why this is a major version

No tool changes shape and no connector needs re-registering, so nothing
downstream breaks. The major bump reflects the size of what moved underneath:
indexing gained a second execution device, a subprocess lifecycle, a device
gate, a process-wide lease and a correctness canary, and the walk itself was
restructured from a sequential parse-embed-store loop into a parse-ahead
pipeline. That is a different engine doing the same job through the same
interface.

---

## 5.7.0 — 2026-08-31

**`remove_document` without `delete_file` undid itself, and `remove_document`
with it could not reach the file that was left behind.** Two halves of one
mistake — treating the index row as the thing that exists — reported from a
connector session. De-indexing is now durable, and the delete resolves its target
by path.

### Added

- **A per-project de-index list.** `remove_document` with `delete_file: false`
  records the path in `deindexed.json` in the project's `data_dir`, and every
  indexing walk consults it. The watcher does not bring the document back, a
  `force` reindex does not, and neither does a restart or a full rebuild.
  `delete_file: false` now means "keep the file, stop indexing it" — which is
  what callers were asking for, and a coherent operation rather than a race.

  It is a **file beside the project, not a table in the schema**, deliberately.
  The index is derived data: DESIGN-4.0's recovery story is `pg_dump` for backup
  and reindex-from-source for repair, and dropping a schema to rebuild it is a
  supported move. A de-index is the one piece of state here that cannot be
  re-derived from the corpus — it is a decision — so putting it in the schema
  would make the documented recovery path silently revert every one of them.

- **`get_index_stats` reports the list**, under `deindexed` (`count`, `files`,
  `list_file`, and `load_error` if the file could not be parsed). Same rule as
  `sync_conflicts` in 5.0 §10: the cost of any exclusion mechanism is a document
  missing from the corpus for a reason nobody can see, so an exclusion is
  reported and never merely logged. It is also the only route back — the list
  names what to write to if a suppression was a mistake.

- **`indexing_suppressed`, `already_deindexed`, `was_indexed`,
  `file_was_on_disk` and `delete_file_requested`** on the `remove_document`
  result. `already_deindexed` makes the call idempotent and says which it was;
  `was_indexed` is what tells a `chunks_removed: 0` which kind of zero it is;
  `delete_file_requested` echoes the argument so a caller can confirm it arrived
  as intended.

- **`reason: "delete_failed"`** for a `delete_file: true` that could not remove
  the file. The index entry is deliberately LEFT IN PLACE, so the document stays
  searchable and nothing is stranded.

- **New self-test steps D1–D4** covering the durable de-index, its survival
  across a forced reindex, deleting an already-de-indexed file, and re-admission
  by writing to the path.

### Fixed

- 🔴 **`remove_document(delete_file: false)` reported `success` for a removal
  that reverted itself.** It dropped the row, left the file on disk, and the
  watcher re-indexed it on the next filesystem event in the project — so the
  postcondition `success` claims held for an unbounded short interval and then
  undid itself. The response carried a `reindex_warning` string saying so, but a
  caller that branches on `status` and not on prose (the obvious thing to do)
  was told the document was gone while search still returned it. **Fixed by
  making the removal true**, not by renaming the status.

- 🔴 **`delete_file: true` failed on an already-de-indexed file, stranding it.**
  The old order was de-index, then unlink, with a `not_found` return in between,
  so once the row was gone the delete refused and the file sat on disk:
  invisible to search, unreachable by every tool, recoverable only by re-adding
  the document and removing it again — which nothing documented. The target is
  resolved by path now and the delete does not require a row; with no index entry
  the result is `success` with `chunks_removed: 0` and `was_indexed: false`.

- 🔴 **`file_deleted` echoed the argument instead of reporting the outcome.**
  `unlink()` failures were logged and swallowed, and the field said `true`
  regardless — which is why `proxy.py` had to re-check the disk behind this tool.
  It is now true only when a file was present and is gone, verified by a `stat`
  after the unlink; a failure returns `delete_failed` with the index entry
  intact.

- **The removal sweep's empty-walk guard fired on the wrong condition.** It
  refused when `live_sources` came back empty, which is right for an unmounted
  `documents_dir` and wrong for a project whose every file is deliberately
  excluded — the correct empty corpus would have been reported as the
  unmounted-disk emergency. It now turns on whether the WALK found anything, and
  the deliberate case goes through a new explicit `delete_all_documents` rather
  than relying on `source <> ALL('{}')` being vacuously true, which is the
  accident that once destroyed a whole index.

### Changed

- **`reindex_warning` is gone from the `remove_document` result.** It described
  behavior that no longer happens; leaving it would be the 5.6.1 defect (a
  description outliving the behavior it describes) in a field instead of a
  docstring.

- **`remove_document` refuses paths under `backups/`.** Resolving by path rather
  than by row hands the tool a delete it never had, and the recovery tree is
  never indexed — so until now every file in it was unreachable here by accident.
  It is now `invalid_path` on purpose: a restore point is the one file in the
  tree with no backup of its own. It also refuses an extension the project does
  not index (`unindexable_extension`), because un-stranding a `.md` is no reason
  to hand out a general delete primitive over the images and archives that also
  live in a documents tree.

- **Any explicit write re-admits the path it wrote** — `add_document`,
  `update_document`, `copy_document`/`copy_directory`, and `move_document` at
  both ends. This is a correctness requirement, not a courtesy: the walk skips
  suppressed sources, so a row indexed for a still-listed path would be swept
  away by the next walk, and the write would look like it worked and then
  quietly lose its document.

- **The documented indexing behavior now matches the implementation.**
  De-indexing is described accurately, and writes no longer recreate entries
  that the user explicitly removed.

---

## 5.6.3 — 2026-08-30

**The admin session key was not stored byte for byte, so it did not survive a
restart.** Same disease as the read path this release began with — normalizing
data that has to be handled verbatim — in the one place the payload is a
cryptographic secret. Three defects in one function, found by chasing a ~1-in-3
flake in the admin-auth suite instead of dismissing it as noise.

### Fixed

- 🔴 **`.strip()` was applied to a 32-byte random secret.** Six byte values are
  whitespace (`0x09 0x0a 0x0b 0x0c 0x0d 0x20`), so a key that happened to begin
  or end with one came back short, failed the `len >= 32` check, and was
  regenerated and overwritten. That is **4.7% of all generated keys**
  (1 − (250/256)²). On those installs every admin session was invalidated on
  every restart — the exact property persisting this file exists to provide —
  and the only symptom was a log line.

- 🔴 **`os.open()` defaults to TEXT MODE on Windows**, so every `0x0a` byte in
  the key was written as `0x0d 0x0a`. A random 32-byte key contains a `0x0a`
  **11.8%** of the time (1 − (255/256)³²), and the file then held 33 bytes that
  were never the key. `O_BINARY` is now passed (`getattr(os, "O_BINARY", 0)` —
  the constant exists only on Windows, and 0 is a no-op elsewhere).

  This is the **same defect 5.0.0 fixed for documents** — `Path.write_text`
  translating `\n` to `os.linesep`, described at length in `_write_verbatim`'s
  docstring — surviving in a second writer nobody re-audited because its payload
  was not prose. This affected Windows builds and first surfaced in the Windows
  test suite.

- **A transient read error destroyed the key.** An `OSError` from `read_bytes()`
  logged a warning and fell through to the generate-and-write branch, whose
  `open()` carries `O_TRUNC` — so a virus scanner holding the file for a few
  milliseconds, a stale NFS handle or an `EINTR` **replaced the durable
  secret**. The write-failure fallback was already fail-safe (a process-lifetime
  key); the read-failure path was the opposite, which is backwards, because a
  read failure is the transient case. Unreadable-but-present now means use an
  ephemeral key for this request and **leave the file alone**. That key is
  deliberately **not cached**, or one transient error would become permanent by
  the back door.

### Impact

Combined, roughly **16% of installs on Windows** and **4.7% on Linux** minted a
session key that could not be read back, logging every admin session out at the
next restart and silently rewriting the key file. It self-healed on the
following restart most of the time, which is precisely why it read as flakiness
rather than as a bug. On Linux, only the `.strip()` half of the defect applied.

No action is needed on upgrade: an existing key file is now read byte for byte,
which *preserves* keys that previous versions would have discarded.

### Tests

`tests/test_admin_auth.py` gains 17 cases — every whitespace byte in each
position, a key containing `0x0a`, an all-`0x0a` key, and the transient-read
paths — all confirmed to fail against the previous code. They assert on the FILE
as well as on the returned key, because each bug was a side effect: a test
checking only the return value passes while the secret on disk is already gone.

---

## 5.6.2 — 2026-08-30

### Fixed

- **`get_document` reported a file that EXISTS as `not_found`.** `parse_file`
  returns `None` when a document extracts to no indexable text — a `.md` that is
  only YAML frontmatter, or a whitespace-only file — and that was turned into
  "Document not found". Same class of untruth as the reformatted read this
  release began with: the caller is told the wrong thing about the bytes on
  disk. It was also self-defeating, because `read_document` serves that file
  perfectly, so a caller believing `not_found` stopped one call short of its own
  content.

  `add_document` refuses to CREATE such a file (5.1, `parse_failed` + rollback),
  so the way one appears is the second writer this project already knows about:
  cloud sync landing a file Cognita never indexed. That is exactly when a
  truthful answer matters.

### Added

- **New `reason` value: `no_indexable_content`** (DESIGN-5.0 §11.1). Carries
  `size_bytes`, `bytes_sha256`, `mtime` and a hint pointing at `read_document`.
  A genuinely absent path still returns `not_found` — the two cases shared one
  payload before, which is what made the untruth invisible.

  Additive to the closed vocabulary, and both guards that police it
  (`KNOWN_REASONS` and the doc/code cross-check in `test_error_reasons.py`) are
  updated with it.

---

## 5.6.1 — 2026-08-30

### Fixed

- **The tool descriptions still described the old behavior.** `read_document`'s
  description carried a `⚠️` telling callers its `text` is NEWLINE-NORMALIZED and
  "NOT a byte-exact copy of a CRLF file". As of 5.6.0 that is simply false, and a
  tool description is not documentation a client can choose to read — it is the
  primary signal the model uses to decide how to call the tool, so a stale one
  actively steers callers wrong. Both `read_document` and `get_document` now
  state that their text is byte-verbatim, and `get_document` names its one
  exception (`content_is_extracted` on binary formats).

  Found by calling the deployed 5.6.0 server through its own MCP surface: the
  behavior was correct and the description shipped alongside it was not.

---

## 5.6.0 — 2026-08-30

**Reads are byte-verbatim.** 5.0.0 made the write path byte-exact and stopped
there, so for five releases Cognita stored perfectly and answered wrong. A store
does not edit what it is handed — in either direction. Reported by a connector
client that pushed `.json` section files and got them back reformatted.

### Fixed

- 🔴 **`get_document` returned the INDEXER's extraction, not the document.**
  `.json` came back through `json.dumps(json.loads(raw), indent=2)` — a compact
  inline array exploded across four lines, the trailing newline gone. `.md` came
  back with its **YAML frontmatter deleted**. `.csv` came back rewritten as a
  `" | "`-joined table. Anything with CRLF came back folded to LF. None of it was
  announced, and every other read path — `content_sha256`, `bytes_sha256`,
  `find_literal`, `list_documents` — reported the truth about the same file the
  whole time, so the single tool you reach for to READ a document was the one
  tool lying about it. `content` is now the file: decoded UTF-8, nothing else
  touched.

  The extraction still exists and is unchanged; it is what gets chunked,
  embedded and searched. It is simply never what a read returns.

- **`read_document` folded CRLF/CR to LF and dropped the BOM.** `text` is now
  verbatim. This one was *documented*, on the grounds that `edit_document`
  matches anchors against normalized text so an anchor copied out of a read had
  to be normalized too. **The grounds were false**: `apply_edit` normalizes
  `old_str` as well as the file, so a CRLF anchor has always matched a CRLF
  file. The folding bought nothing and cost byte fidelity on the tool whose
  stated job is verbatim reads.

- **An edit re-flavored the whole file.** The edit tools compute against
  newline-normalized text and that normalized text was written back, so changing
  one word in a CRLF document rewrote every line ending in it and deleted its
  BOM. Matching still happens in LF-space — that is a matching convenience and
  keeps anchors flavor-agnostic — but the result now goes through
  `editing.restore_line_endings()` first: untouched lines keep the exact
  separator they had (byte-exact even on a mixed-ending file), new lines take the
  file's dominant one, and the BOM survives. Covers `edit_document`,
  `edit_document_batch` and `insert_in_document`.

### Added

- **`get_document` → `content_is_extracted` and `content_note`.** `false` for
  every text document. `true` only for a genuinely binary format (`.pdf`,
  `.docx`, `.xlsx`, `.pptx`), which has no text on disk to return — there
  `content` is extracted text and the response says so outright rather than
  leaving you to discover it. Additive; the tool's wire shape is otherwise
  unchanged.

### Changed

- **`read_document` → `normalized_line_endings` is now always `false`.** Kept in
  the shape because a connector may branch on it; nothing normalizes `text` any
  more. `content_note` no longer describes folding.

### Unchanged, and worth being explicit about

- **`content_sha256` still folds CRLF/CR to LF and drops the BOM**, everywhere it
  appears. It is the **write-guard stamp** `expected_sha256` compares against,
  and the folding is what stops an EOL-only rewrite by OneDrive false-positiving
  as a stale file. It is a version stamp, not a description of the bytes — use
  `bytes_sha256` for byte comparisons. The two are equal on an LF file with no
  BOM, which is most of them.

### For clients

If you compared a read-back against what you pushed and adjusted your comparison
to make it agree — stripping, re-serializing, tolerating a missing trailing
newline — **delete that code**. It now masks real corruption. `sha256(content)`
from `get_document` equals `bytes_sha256` for any text document.

⚠️ **Files written through a client whose local copy came from `get_document`
may have been stored in the reformatted shape** — the bytes that came out are
the bytes that went back in. The content is semantically identical; only the
formatting moved. Nothing needs migrating, but a first diff after upgrading may
be noisy on those files.

### Why it survived five releases

Both halves were found before and explained away. A self-test run on 2026-08-29
byte-compared a CRLF payload, saw a mismatch, and concluded the write was broken;
the write was perfect. The fix was to document the read's normalization — and
then the self-test step written to *guard* against silent normalization was
rewritten to *expect* it. The guard passed while the bug it existed to catch sat
underneath it. When a read disagrees with what was written, fix the read.

Regression suite: `tests/test_read_verbatim.py`, the mirror of
`tests/test_write_verbatim.py`. Its core assertion is one line — for each format,
sha256 of the bytes read back equals sha256 of the bytes sent.

---

## 5.5.0 — 2026-08-29

Both items came out of the 5.4.0 self-test run, which passed every step and found
no defect. What it found instead was a surface that is harder to use correctly
than it needs to be: an undo point you cannot name, and a breaking change this
file never called breaking.

### Added

- **Every write that takes a backup now names it: `previous_backup_id`.**
  `update_document`, `remove_document`, `move_document`, `edit_document`,
  `edit_document_batch`, `insert_in_document` and `restore_backup` all take a
  mandatory backup and said nothing about it — only the `add_document` OVERWRITE
  path and the copy / `remove_directory` tools did. So naming the undo point for
  your own write meant calling `list_backups` afterwards, taking the newest entry
  and hoping nothing else had touched that path in between. On a shared path that
  is a guess, and it had to be right, because this is the **only** undo path in
  the system.

  Additive and **success only** — a payload reporting that the write did not
  happen does not carry an undo point for it, so the error envelope (§11.1) is
  unchanged. Nothing about backups themselves changed: the field names an id
  `list_backups` was already reporting. DESIGN-5.0 §7.5 has the per-tool table.

### Fixed

- **A REFUSED `add_document` over an existing file no longer claims
  `overwrote_existing: true`.** The overwrite forensics (2.10.6) were merged into
  the result whatever the engine came back with, so a write that did not happen
  reported the hash and backup id of the file it had supposedly buried. Those
  fields — and `remove_document`'s "the file STILL EXISTS on disk" warning, which
  begins "The engine reported deletion" — now appear on `success` payloads only.

### Corrected documentation

- **The 5.1.0 type-validation entry did not say it was BREAKING.** It described
  the improvement (an `internal_error` became a named `reason: "invalid"`) and
  not the client impact: a call that passed a wrong-typed argument and *got away
  with it* under 5.0.x now fails. The 5.1.0 entry below now says so, and names
  `evaluate_retrieval(test_cases=...)` as the case most likely to bite.
- **Self-test step 41 said to restore "the oldest entry of this run", and gave no
  way to tell which entries were this run's.** Backups survive deletes and
  accumulate on the reused fixture path — the 5.4.0 run found 20 on one file, the
  oldest left by an earlier session. Restoring that one asserts a hash this run
  never wrote, and when the file already matches it the answer is
  `reason: "no_change"`: a correct refusal that reads as a failed step. Step 39
  now records the `previous_backup_id` of the edit that takes the H5 snapshot,
  and step 41 restores **that id**, then repeats the call to assert `no_change`
  — which nothing in the plan had covered.

---

## 5.4.0 — 2026-08-29

The last item from the 5.0.2 review, and the one I had wrongly parked as needing
Doug to hand me a list. He did not: the machine already knows the answer.

### Added

- **The admin surface validates the `Host` header (DNS rebinding).** Nothing
  checked it, so a page the admin visited could rebind `attacker.tld` to this
  machine and the browser would treat `http://attacker.tld:8676/` as
  **same-origin**, reading responses from an authenticated admin session.
  `SameSite=Lax` does not help — after rebinding the request *is* same-site.

  The allow-list is **derived**, not configured, because an allow-list that omits
  the name the operator actually types locks them out of the admin UI with no
  route back but SSH — which is precisely why this was not added sooner. It is
  computed from things that already know the answer: **the TLS certificate's own
  subjectAltNames**, the machine's hostname and FQDN, its routable addresses,
  and loopback. The effective list is logged at startup.

  `admin_allowed_hosts` in config overrides the derivation; `["*"]` disables the
  check. A missing or unreadable certificate degrades to the local names rather
  than raising — a surface that will not start is worse than a shorter list.

### Fixed

- `config.py` still described the admin password as shipping with a known default
  of `admin` / `welcome`. That stopped being true in 5.1.0, which removed it
  precisely because it disabled the exposed-bind startup guard.

---

## 5.3.0 — 2026-08-29

Both items came out of the 5.0.2 review's "needs a decision" pile. Neither
needed one: both were testable with fixtures I control, and both were real.

### Fixed

- **A write arriving during a reindex hung instead of answering.**
  `index_project` holds the project write lock for a WHOLE corpus walk, and the
  gateway's read timeout is `None`, so a mutating call queued behind a full
  rebuild waited indefinitely and the caller only ever learned about it as a
  client-side timeout — which it would then retry into a `stale_file` /
  `not_found` for a write that had by then succeeded. Measured on a 150-document
  synthetic corpus against real Postgres: a concurrent `remove_file` waited
  1.42s, exactly the rebuild's remaining duration. On a large corpus with a
  real embedder that can take substantially longer.
  A mutating call now waits at most 20s for the lock and then returns
  **`reason: "busy"`** with `retry_after_seconds` and the live `reindex` block.
  The reindex's own guarantee is untouched — it still holds the lock for the
  whole walk, which is what makes it all-or-nothing — and since 5.2.0 the retry
  that follows is safe. **Client impact: a new `busy` reason on mutating tools.**

- **After a rollback to `engine: workers`, guarded writes and the manifest broke
  silently.** The pinned 3.x engine declares a smaller argument surface than this
  gateway advertises — verified against the installed package's own signatures:
  `add_document(content, filepath, category)` and `update_document(filepath,
  content)` take no `expected_sha256`, and `list_documents(category)` takes
  neither `prefix` nor `include_hashes`. Forwarded verbatim, each failed at the
  worker, in the one mode whose entire job is to still work.
  `expected_sha256` is now stripped at the forward point — after the gateway's
  own stale check has run, so the guarantee survives the rollback — and
  `prefix`/`include_hashes` are **refused** with `reason: "unsupported_format"`
  rather than dropped, because a silently ignored filter returns a wrong answer
  that looks like a right one (§2). Only applies when running the rollback
  engine; `engine: core` is unaffected.

---

## 5.2.0 — 2026-08-29

### Added

- **`operation_id`: retrying a write after a timeout is now safe.** A client that
  times out does not cancel the first attempt — it is still running and still
  holding the write lock — so the retry executed against the state that attempt
  had already produced and was told `stale_file` / `not_found` /
  `destination_exists` for a write that **succeeded**. Every mutating tool now
  accepts an optional `operation_id`; a repeat call with the same id returns the
  first call's full stored result with **`replayed: true`** and writes nothing.
  Errors are remembered too, so retrying a genuine failure does not quietly
  re-attempt it. **Client impact: purely additive — omitting `operation_id`
  behaves exactly as before.** DESIGN-5.0 §11.1a.

---

## 5.1.1 — 2026-08-29

### Fixed

- **A category-filtered search could return zero results over a full category.**
  An HNSW index scan surfaces `hnsw.ef_search` candidates (default 40, never set
  here) and the category predicate is applied to the JOIN output *afterwards* —
  so when none of the 40 nearest chunks are in the requested category,
  `search_knowledge(query=…, category=…)` returned nothing while the category was
  full. In a measured corpus, a category-filtered scan over 11,871 chunks with 73 documents
  in that category planned `Index Scan … rows=40` into a join producing
  `rows=0` for `LIMIT 5`. Silent, and worse as a corpus grows — it reads as
  search quality degrading rather than a bug. Filtered dense searches now set
  `hnsw.iterative_scan = strict_order` and raise `ef_search`; the same query
  returns its 5 rows in 7ms. Unfiltered search is deliberately unchanged.
  Requires pgvector 0.8 or newer.

---

## 5.1.0 — 2026-08-29

A full code review of 5.0.2, run as five reviewers split by subject (write path,
retrieval, security, spec coverage, wire contract) with every finding re-derived
against the code before it was accepted. **No tool's wire shape changed** — no
argument renamed, removed or made required, no result key dropped — so no
connector needs re-registering. The behavior changes a client can observe are
below.

### Security

- **The admin session cookie was forgeable from a constant published in this
  repo.** The signing key was derived from `admin_password_sha256` alone, and
  that field shipped with a default (`sha256("welcome")`). On any install still
  on the default the key was a public constant, so a valid admin cookie could be
  minted offline — no password, no login request, no rate limit to defeat —
  reaching token minting, project creation pointed anywhere on disk, and
  `/api/browse`. The key is now 32 random bytes per install, persisted `0600`
  under `data_root`, with the password hash still mixed in so changing the
  password still invalidates every session.

- **The exposed-bind startup guard could never fire.** It refuses a non-loopback
  `admin_host` when no password is set, and "is one set?" is
  `bool(admin_password_sha256)` — never false while a default shipped. The
  default is now empty, matching `config/cognita.example.yaml`. **Client impact:
  an install that relied on the built-in `admin`/`welcome` now has an OPEN admin
  surface on loopback and REFUSES to start off loopback.** Run
  `scripts/set-admin-credentials.*` to set one.

- **Logout now actually revokes.** It only cleared the cookie; a copy captured
  beforehand stayed valid until `exp` — 30 days by default. It rotates the
  install signing key, so **logging out ends every session for that admin,
  including other browsers.**

- **`/api/login` is throttled** — 5 failures per source address, then 60 seconds
  of HTTP 429 with `Retry-After`. There was no counter, delay or lockout at all,
  over an unsalted single-round SHA-256 verifier.

- **`add_from_url` was an SSRF with the response handed back.** The only check
  was the URL scheme, redirects were followed automatically, and the body was
  buffered whole. Its result is stored and readable via `get_document` /
  `read_document` / `find_literal`, so it was a read primitive into everything
  the host can reach — loopback, the LAN, cloud metadata. Now every hop is
  resolved and refused unless every address is public, redirects are followed by
  hand so each is re-validated, and the body is capped. **Client impact: a URL
  resolving to a private, loopback or link-local address returns
  `reason: "invalid"`.**

- **Two token spellings leaked into the log**: `/MCP/<token>` and
  `/mcp//<token>` (the ordinary result of a client base URL ending in `/`). The
  redaction rule was case-sensitive and could not match an empty path segment,
  so a live token reached `logs/cognita.log`. Both covered, plus the
  `Authorization: Bearer` form.

- **`/api/browse` is loopback-only.** It enumerates the server's filesystem, the
  UI never calls it, and its docstring still claimed the admin surface was
  127.0.0.1-only — untrue since 3.0.

- A non-ASCII session cookie 500'd every admin route and a non-ASCII username
  500'd `/api/login` (`hmac.compare_digest` rejects non-ASCII `str`). Both now
  return cleanly.

### Fixed — data loss and integrity

- **An unreadable `documents_dir` deleted the entire project index.** `os.walk`
  swallows every error, including "the root does not exist", and yields nothing;
  that empty list reached `delete_documents_not_in`, whose `source <> ALL($1)` is
  vacuously TRUE for an empty array. Every row deleted, every chunk cascaded, one
  log line reading `removed: 187`. A restart before the OneDrive mount was ready
  was enough. The walk now raises on an unreadable root, and the removal sweep
  refuses to run when it found zero files while the index holds rows.

- **`add_document`/`update_document` reported `parse_failed` with the bytes
  already written.** Content that passes the emptiness check can still extract to
  nothing (a file that is only YAML frontmatter). On add that left an orphan
  nothing would ever index; on update the disk held new content while the index
  held the old document's chunks, and the caller was told it failed. Both now
  restore and report `rolled_back: true`.

- **`copy_directory`'s rollback had the exact race 5.0.2 closed for removals** —
  de-index and unlink as two awaits with the lock released between them, so the
  watcher could re-index a row the rollback was undoing. Found independently by
  two reviewers. `copy_directory`, its rollback, and `copy_document` now hold the
  project write lock across the whole operation.

- **`remove_directory` selected index rows by the caller's raw prefix but files
  by the resolved path.** A prefix of `packs/./krea2` deleted 17 files and matched
  no stored source: `documents_removed: 0, files_deleted: 17`, leaving 17 rows for
  files that no longer exist. It also de-indexed everything *before* backing
  anything up, so a backup failure left a live file with no index row.

- **All four single-file writers could write into `backups/`**, silently
  destroying a recovery point — `backup_if_exists` returns `None` for a path
  already under `backups/`, so the mandatory-backup hook took no snapshot and
  raised no objection. §7 said this tree is never written through the tool
  surface; now it is not. **Client impact: `reason: "invalid_path"`.**

- **`update_document` and `move_document` never ran the sync-conflict name
  check** `add_document` does, so a write to an existing `*-conflict.md`
  succeeded and indexed a row the next reindex walk dropped.

- **Writes are now crash-atomic.** `_write_verbatim` truncated in place; it now
  stages, fsyncs and `os.replace`s. Byte-verbatim behavior is unchanged.

### Fixed — `restore_backup`, the documented undo that did not work

- **It could not restore a deleted file** — the one case `remove_document` and
  `remove_directory` advertise it for. It always synthesized `update_document`,
  which refuses a path not on disk, so the documented recovery path returned
  `reason: "not_found"` with the backup sitting right there. It synthesizes
  `add_document` when the target is absent.
- **It normalized what it restored**, rewriting every line ending and dropping
  the BOM, so `bytes_sha256` no longer matched the backup it claimed to restore.
  Now byte-verbatim. **Client impact: `no_change` compares BYTES, so an LF backup
  against a CRLF file is now a real restore rather than a refusal.**
- **`expected_sha256` was silently ignored when the target was missing.** That
  case is `stale_file` now.

### Fixed — retrieval correctness

- **`find_literal` could not match any pattern containing a newline** and
  reported it as `reason: "no_matches"` — "an exhaustive search, so the answer is
  'not present'". A confident zero for a string that is there. It scans whole
  documents now; `^`/`$` still apply per line.
- **`search_similar` applied its `LIMIT` before dedup and self-exclusion**, so a
  document with 25+ chunks filled the budget with its own chunks and the tool
  answered "No similar documents found" while real neighbors sat in the index.
- **Every multi-chunk document indexed its own tail twice** — the overlap step
  emitted a final chunk wholly contained in its predecessor. ~7% fewer chunks and
  embeddings on a 14-chunk document, and no more duplicate search hits.
- **Markdown code-block masking could fabricate content**: prose containing the
  literal `__CODE_BLOCK_0__` was replaced with an unrelated code block.
- **UTF-16/32 files are decoded properly** instead of becoming NUL-laden text
  Postgres rejects, which left them silently unindexed.
- `find_literal` now strips the BOM like every other read path, and the watcher
  no longer drops events under symlinked subdirectories.
- **`find_literal` has a 20-second sweep budget.** `re` has no timeout, so a
  catastrophic-backtracking pattern ran forever in an uncancellable thread — from
  a READ-ONLY token. **Client impact: an interrupted sweep returns
  `timed_out: true` and `exhaustive: false`, and says its zero cannot be
  trusted.**

### Added

- **`list_documents` says which kind of zero it found.** A prefix or category
  that selects nothing now returns `corpus_size`, a message, and
  `available_categories` — the ambiguity `find_literal` already avoids.
- **Arguments are validated against their declared `type`. BREAKING — see
  below.** `max_results: "abc"` raised out of the dispatcher and came back as
  `internal_error` with raw exception text; it is now `reason: "invalid"` naming
  the argument, the type expected, the type received, and that NOTHING was
  executed. Types only — no range or enum checking, because the wire contract is
  frozen.

  **Client impact: a call that passed a wrong-typed argument and got away with it
  under 5.0.x now fails.** The likeliest victim is
  **`evaluate_retrieval(test_cases=...)`**, which is declared `"type": "string"`
  — a JSON array serialized *as a string*,
  `'[{"query": "...", "expected_filepath": "..."}]'`. A client passing a native
  list was accepted leniently before and is refused now (`'test_cases' expects
  string, got list`); the 5.4.0 self-test run hit exactly that. Same class:
  an int or bool sent as a quoted string (`read_document(start_line="1")`,
  `remove_document(delete_file="true")`), a list where a string is declared
  (`find_literal(pattern=["a"])`), and a float for an integer
  (`start_line=1.5`). Nothing runs in any of these cases. *(This paragraph was
  added in 5.5.0: the entry originally described only the improvement.)*
- **The error `reason` vocabulary is genuinely closed.** The guard walked dict
  literals, and `EditReject` builds its payload in `__init__`, so 20 raise sites
  were invisible and three values (`would_empty_file`, `batch_aborted`,
  `out_of_range`) shipped undocumented; `bad_pattern` had drifted the other way.
  All four are now in §11.1, and a test cross-checks the doc against the code.
- **Error payloads no longer carry absolute host paths.** `str(OSError)` renders
  the server's directory layout and username, and that was the shared
  `unreadable` message for every gateway read path.
- **Backup failures and path refusals are tool-level errors with a `reason`**,
  not bare JSON-RPC errors. **Client impact: a failed backup now arrives as
  `status: "error", reason: "backup_failed"` instead of a `-32602`.**

### Corrected documentation

- **Documentation understated the impact of a leaked token.**
  Projects can be writable: `remote_readonly` defaults `false` and
  `Project.writable` defaults `true`, so the default deployment serves the full
  26-tool surface over the tunnel — which is what makes the connector self-test's
  write steps work. All three now state the real blast radius and name the two
  controls that exist.
- The self-test plan still told the model "Writes strip: `add_document` and
  `update_document` store `content.strip()`" — false since 5.0.0, and
  contradicting the byte-fidelity section of the same document.

---

## 5.0.2 — 2026-08-29

Follow-up to the 5.0.1 self-test run, which passed every step. Two findings: one race
that made a correct delete look like a failed one, and one contract the surface was
telling clients to rely on without always honoring it.

### Fixed

- **A removal is now one critical section, so its result cannot be contradicted by the
  very next call.** `remove_directory` de-indexes N documents and then deletes N
  files; both loops ran as bare awaits, so the watcher's debounced whole-tree sync
  (`index_project`, same lock, its own task) could land between them, walk a tree
  where the files still existed, and index a row the call had just deleted. The
  2026-08-29 run got `documents_removed: 3, files_deleted: 3` and an immediate
  `list_documents` still showing one of them — `mtime`, `size_bytes` and
  `bytes_sha256` all null, `index_drift` true — which cleared on its own a call later.

  `remove_document`, `remove_directory` and `move_document` now hold the project write
  lock across their whole operation. **Client impact: there is no settling window and
  nothing to poll.** A row still listed after one of these calls is a defect, not lag;
  the self-test plan asserts the listing immediately, with no retry. The lock is
  re-entrant per task so the outer claim can coexist with the per-call one.
  The write operation now keeps the project lock across the whole operation.

  Distinguishing this from a cloud-sync ghost, since both show a "deleted" thing still
  present: sync can restore a FILE, never a ROW. Null disk facts with `index_drift`
  mean there is no file, only a leftover index record — Cognita's own state, and a
  real defect. A ghost carries real values because a ghost is a real file. §10.1.

- **Every error payload names a `reason`.** The plan tells clients to branch on
  `reason` and never on message text, which is only honest if the field is always
  there — and four paths still returned `reason: null`: `get_document` and
  `remove_document` not-found, `move_document` same-path and destination-exists. All
  four now carry one (`not_found`, `not_found`, `same_path`, `destination_exists`), as
  do the two dozen other engine errors that had the same gap, and the closed
  vocabulary is tabulated in §11.1. Two runtime backstops stamp `reason: "error"` and
  log the tool if a new payload ever reaches the wire without one, and
  `tests/test_error_reasons.py` fails the build before it can.

- **`add_document` and `update_document` no longer claim to strip a trailing
  newline.** Their tool descriptions still said so — false since 5.0.0 made writes
  byte-verbatim, and the tool description is what the model on the other end reads.
  This is the same class of defect as 5.0.1's `read_document` claim; both are now
  covered by tests that assert the description does not say it.

### Added

- **`remove_document` without `delete_file` returns a `reindex_warning`.** De-indexing
  a file that stays on disk does not stick: the watcher indexes every supported file
  it finds, so the next filesystem event in that project brings the document back.
  The behavior is unchanged and always was this way; saying so out loud is the fix.
  §11.2.

---

## 5.0.1 — 2026-08-29

Follow-up to the 5.0.0 self-test run, which passed every step. Two findings, both
about *telling two states apart* rather than about broken behavior — the same class
of defect 5.0.0 fixed for `find_literal`'s zero.

### Fixed

- **`read_document` now declares that its text is newline-normalized, and carries the
  file's real byte facts.** 5.0.0's self-test plan claimed `read_document` was
  byte-verbatim. It is not, and must not be: `edit_document` matches anchors against
  normalized text, so an anchor copied out of a `read_document` response has to be
  normalized too or read→edit composition breaks. The write was always correct —
  what was wrong was the claim. A client byte-comparing a CRLF payload against
  `read_document`'s text therefore reported a perfectly stored file as corrupt, which
  is exactly what happened on 2026-08-29.
  `read_document` now returns `bytes_sha256` and `size_bytes` (the **whole file**,
  byte for byte, even on a ranged read), `line_endings`, `normalized_line_endings`
  and a `content_note`. **Verify a write against `bytes_sha256`.**
  The self-test's byte-fidelity header and a new step B4 now assert exactly this.

### Added

- **Ghost forensics on delete.** `remove_document` with `delete_file: true` returns
  `deleted_mtime`, `deleted_size_bytes`, `deleted_bytes_sha256` and a `ghost_check`
  line; `remove_directory` returns the same per file. A deleted file that reappears
  is a cloud-sync resurrection if its mtime is unchanged and a real defect if it is
  newer — and that value only exists before the unlink. See §10.1.
- **`remove_directory` returns `backup_ids`**, the distinct set for the operation. A
  bulk call normally produces ONE shared id (ids are per-second stamps; the
  `-1`/`-2`/`-3` suffix disambiguates repeat backups of the *same file*), which is
  what makes the operation enumerable as a unit and is **not** a collision. Now
  documented in §7.3, because a runner expecting one id per file reads it as one.

### Self-test plan

- Byte-fidelity steps verify against `bytes_sha256`, not `read_document`'s text, and
  new step B4 asserts the three read paths differ **only** in the documented ways —
  the guard against the next silent normalization landing in a path described as
  verbatim.
- A ghost-check preamble covering every cleanup assertion, with the forensic
  signature, so a sync resurrection is reported as informational rather than filed as
  a delete bug.
- "Assert on `reason`, never on message text" stated explicitly — `not_found` is a
  `reason` value, and the message reads "No such document: '<path>'".
- A warning that a client which raises on `status: "error"` has discarded the payload
  every negative step asserts against.
- A precondition on G1 naming the fixture state it depends on: with a `.md` file
  directly in the pack directory, case (c) correctly returns `no_matches` instead of
  `no_documents_selected` and the step proves nothing.

---

## 5.0.0 — 2026-08-29

Major version: several documented behaviors changed. Nothing about the transport,
the endpoint URL, authentication, or any existing argument's meaning changed.

### Fixed — silent data corruption

- **Writes are byte-verbatim.** `add_document` and `update_document` wrote
  `content.strip()` through `Path.write_text`, which discarded leading whitespace and
  *all* trailing newlines, and — in text mode with `newline=None` — translated `\n`
  to `os.linesep` on write. Stored `a\nb\n` as `a\nb`, `  a\nb` as `a\nb`, and on
  Windows turned `a\r\nb\r\n` into `a\r\r\nb`. The leading strip silently corrupted
  any file whose first character is a space or tab. Now the content is encoded to
  UTF-8 and written as bytes: no strip, no newline translation, no platform
  dependence.
  **Client impact:** the `.rstrip("\n")` comparison workaround is obsolete and should
  be **deleted** — it will now mask a real corruption. Read back with
  `read_document` or the manifest's `bytes_sha256` for byte comparison;
  `get_document` returns the indexed extraction, which is normalized by design.
  > ⚠️ **Corrected in 5.0.1:** that last sentence was wrong about `read_document`.
  > Its `text` and `content_sha256` are newline-normalized too. **`bytes_sha256` is
  > the only byte-exact check** — see the 5.0.1 entry above. The entry is left in place rather than edited,
  > because a changelog that quietly rewrites itself is not a record.
- **Unrecognized tool arguments are refused instead of dropped.**
  `list_documents(path_prefix=…)` used to ignore the key, return all 338 documents
  and report `status: "success"`. Any argument absent from a tool's `inputSchema` now
  returns `reason: "unknown_argument"`, naming the rejected keys and listing the
  accepted ones. Applies to all 26 tools.
  **Client impact:** a call that was silently doing the wrong thing now fails loudly.
- **`isError` is set on failing tool results.** It was hardcoded `false` on every
  result. A client checking transport status, or merely the presence of `result`, saw
  success — which produced a wrong "the file is still present" about a deleted file on
  2026-08-29. `status` inside the payload remains authoritative and unchanged.

### Added — write safety

- **`expected_sha256` on `update_document`**, and on `add_document` when the target
  path already exists. The edit tools have had this since 2.7; the two tools that
  replace a whole file had no concurrency check at all, and the push path overwrites
  through `add_document`. A stale hash is refused with `reason: "stale_file"` and
  **no backup is taken**. `expected_sha256` against a path with no file is also a
  rejection (`actual_sha256: null`) — you named a version to replace.
  **Client impact:** the project rule "prefer `edit_document_batch` over full-file
  `update_document`" existed only to route around this gap and can be dropped.

### Added — tools

- **`copy_document`** and **`copy_directory`** — byte-exact duplication, indexed on
  arrival, refusing an existing destination by default and naming every conflict
  before writing anything. `copy_directory` is non-recursive by default, rolls back
  every file it wrote if one fails, and returns the destination list with hashes.
- **`remove_directory`** — the undo for `copy_directory`. Refuses a directory that
  still holds files unless `delete_files=true`, backs up every file it deletes, and
  prunes emptied directories.
- Tool count: **23 → 26** on a writable project; 13 on a read-only one, unchanged.
  No connector re-registration is needed — the server re-serves `tools/list` on every
  connection.

### Added — the manifest

- **`list_documents(prefix=…, include_hashes=true)`.** `include_hashes` adds
  `content_sha256`, `bytes_sha256`, `size_bytes`, `mtime`, `on_disk`,
  `indexed_sha256` and `index_drift` per entry, all read **from the file on disk at
  request time**, plus `drift_count` / `missing_on_disk_count` at the top level. A
  sync diff is now one call. `prefix` is a plain string prefix on the relative path;
  append `/` to scope strictly to a directory.
- `get_document` carries the same hash and stat fields.

### Added — transport

- **JSON-RPC 2.0 batch requests.** An array body used to return `-32600`. Elements
  execute in order, sequentially, through the same policy path as single calls.
  Notifications get no entry; an empty array is `-32600`; one failing element does
  not fail the batch. **A batch is not a transaction.**

### Changed — result shapes

- ⚠️ **`filepath` is now the RELATIVE path on every tool that returns one**, and the
  absolute host path moved to `source`. `add_document`, `update_document`,
  `remove_document` and `move_document` previously returned the absolute path under
  `filepath`, contradicting `list_documents`. `search_knowledge`, `search_similar`
  and `evaluate_retrieval` now return both forms, so a search hit can be fed straight
  into `get_document` with no string surgery. Both forms are still accepted as input
  everywhere.
- **`search_similar` entries carry a non-null `score`** (same value as `similarity`,
  descending). Reading `score` used to yield `null`, making thresholding impossible.
- **Every collection response carries `result_key`**, naming the key that holds its
  collection, so a generic client can write `payload[payload["result_key"]]` instead
  of memorizing `results` / `similar_documents` / `documents` / `matches` /
  `backups`. The bounded collections additionally carry a `results` alias;
  `list_documents` and `copy_directory` do not, because duplicating an unbounded list
  would double the payload.
- **`remove_document` reports `pruned_directories`** and now prunes parent
  directories it empties, up to but never including the documents root.

### Changed — indexing

- **An omitted `category` on `add_document` is inferred, not forced to `"general"`.**
  Resolution is: explicit argument → the document's existing category on an overwrite
  → `category_mappings` path lookup → `"general"`. The result echoes the category
  actually stored. Before this, a project's `category_mappings` were a no-op for
  every document written through the connector, and an overwrite could silently
  reclassify a document that had a deliberate category.
- **Cloud-sync conflict copies are never indexed.** Filenames matching
  `sync_conflict_patterns` (OneDrive/Dropbox/Syncthing shapes, configurable, `[]` to
  disable) are skipped by the reindex walk and by the watcher, and writing to such a
  name is refused with `reason: "sync_conflict_name"`. Skips are logged and reported
  in `get_index_stats().sync_conflicts` with the count, the files and the patterns —
  no filename rule is free of false positives, so the mitigation is visibility.

### Changed — diagnostics

- **A zero from `find_literal` now says which kind of zero it is.**
  `reason: "no_documents_selected"` means the filters selected no files and **the
  search never ran** (with the corpus size, the filter responsible, and — when a glob
  has a `/` but no `**` — the `**` form that would have worked).
  `reason: "no_matches"` means files were scanned and the string is genuinely absent.
  Previously both produced an identical payload, which on 2026-08-29 turned correct
  glob behavior into a reported bug.
  **`filepath_glob` semantics are unchanged and remain shell-compatible:** `*` never
  crosses a `/`, so `Wildcards/*.txt` means files directly in `Wildcards`. Use
  `Wildcards/**/*.txt` for the subtree.
- **`list_backups` takes `prefix`, `since` and `until`**, so the backups from a single
  bulk operation are enumerable as a set.
- **Size ceiling documented and enforced before the write**: 5,000,000 bytes, refused
  with `reason: "too_large"` naming both numbers. Nothing is ever truncated.

### Documentation

- Documentation: the public MCP surface, error envelope, concurrency model,
  copy semantics, write guards, path behavior, collection keys, and size limits.
- New: this changelog.
- The self-test plan gained byte-fidelity steps (run **first**), manifest and
  write-guard steps, copy and directory steps, glob/shape/count steps, and two
  optional sections — raw-transport and server-shell — that a connector client
  reports SKIPPED rather than failed.
## 12.2.0 — Admin tab navigation redesign (2026-09-17)

- Reorganized Admin Connectors and Workspaces into canonical, keyboard-accessible
  secondary tab rows with independent policy panels and shared connector selection.
- Replaced standalone client setup material with credential-scoped Connection instructions.
- Updated current release assets and container version surfaces to 12.2.0.
