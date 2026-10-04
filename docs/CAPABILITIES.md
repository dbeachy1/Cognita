# What Cognita can do

This is the full list. The [README](../README.md) has the highlights and the install steps.

Cognita connects an AI assistant (Claude, ChatGPT or any MCP client) to folders of documents on
your own computer. The assistant can search them, read them and, where you allow it, change
them. Cognita can also give the assistant a private Linux machine, the **Workspace**, for running
programs on copies of your files.

- [Projects and documents](#projects-and-documents)
- [Search](#search)
- [Reading](#reading)
- [Writing](#writing)
- [Backups and undo](#backups-and-undo)
- [Images and OCR](#images-and-ocr)
- [Indexing](#indexing)
- [Workspace](#workspace)
- [Connectors and access](#connectors-and-access)
- [The Admin page](#the-admin-page)
- [Checking that it works](#checking-that-it-works)
- [Tool reference](#tool-reference)
- [What stays stable](#what-stays-stable)

## Projects and documents

- **A project is one documents folder.** Cognita searches it and its subfolders, in place. It
  never moves or copies your files anywhere else. Each project has its own index, kept fully
  apart from the others.
- **Changes on disk are picked up by themselves.** Cognita watches each folder. A file you add,
  edit or delete in any program (or that a sync tool like OneDrive brings in) is reindexed
  without anyone asking.
- **Documents searched by meaning and by words:** Markdown, plain text, PDF, Word (`.docx`),
  Excel (`.xlsx`), PowerPoint (`.pptx`), CSV, JSON, XML and Jupyter notebooks.
- **Code and config found by exact text only:** `.py`, `.sh`, `.js`, `.ts`, `.tsx`, `.jsx`,
  `.c`, `.h`, `.cpp`, `.css`, `.yml`, `.yaml`, `.toml`, `.ini`, `.sql` and `.json5`. These are
  listed and readable, and exact-text search finds anything in them, but they are not part of
  meaning search, where code tends to crowd out prose. Both lists can be changed per project
  in Cognita's project settings file.
- **PNG images** are handled as assets, with their text read by OCR (see
  [Images and OCR](#images-and-ocr)).
- **Categories.** Every document has a category, taken from its folder by default. Search and
  listings can be narrowed to one category.
- **A removal stays removed.** Taking a document out of the index without deleting the file puts
  it on the project's skip list, so the watcher and later rebuilds leave it out. Writing to that
  path again brings it back.

## Search

- **Question search** (`search_knowledge`). Every query runs a keyword search and a meaning
  search together, merges the two result lists, then reranks the top results with a second model
  that reads each passage against the question. The caller can lean the mix toward exact words
  or toward meaning, set a minimum score, narrow to a category, and ask for short snippets or
  full passages.
- **More like this** (`search_similar`). Finds documents close in meaning to one you name.
- **Exact text, everywhere** (`find_literal`). Every occurrence of a string or regular
  expression across the whole project, with file and line number, read from the files on disk
  rather than the index. It is exhaustive: when it finds nothing, the text is not there. It can
  be narrowed by file pattern or category, and it says plainly when a filter matched no files at
  all, so an empty answer is never ambiguous. Built for renames, stale references and "where
  else did I write this?".
- **Listings.** All documents, by category or folder (`list_documents`); categories with counts
  (`list_categories`); projects the connector can see (`list_projects`); index size and health
  (`get_index_stats`).
- **Change manifest.** `list_documents` can include each file's hashes, size and modified time
  read from disk, plus whether the index is behind the file. One call shows what changed since
  the last sync.
- **Search quality check** (`evaluate_retrieval`). Give it questions and the document each
  should find; it reports how well search ranks them.

## Reading

- **Whole documents** (`get_document`) and **up to 100 at once** (`get_documents`, in the order
  asked, with a per-file answer when one is missing).
- **Part of a document** (`read_document`): one Markdown section by its heading, or a range of
  lines. This is the cheap way to read before an edit.
- **Exactly what is on disk.** A read returns the file's own bytes: its line endings, byte-order
  mark and whitespace are kept. Exact bytes are also available as base64. PDF and Office files
  have no text on disk, so for those the answer is the extracted text, and it says so.
- **Version stamps.** Every read returns hashes of the file. Passing one back on a later write
  makes the write refuse if the file changed in between (see below).

## Writing

Writing is only possible where the connector has read/write access to the project.

- **Create or replace a file** (`add_document`, `update_document`). Missing folders are created.
- **Small, exact edits** (`edit_document`): replace one exact piece of text, or every copy of
  it. Several edits to one file go in one call (`edit_document_batch`), all or nothing.
- **Add text without touching the rest** (`insert_in_document`): at the top, at the bottom, at
  the end of a named section, or after a section's introduction.
- **Several files as one unit** (`write_documents`): up to 100 files that either all land or
  none do. If indexing fails afterwards, every file is put back.
- **Copy, move and rename** (`copy_document`, `copy_directory`, `move_document`). Copies are
  byte for byte and never pass through the assistant. A folder copy refuses before writing
  anything if any destination exists, and undoes itself if one file fails.
- **Remove** one file, a list of files, or a folder (`remove_document`, `remove_documents`,
  `remove_directory`), either just from the index or from disk too.
- **Save a web page** (`add_from_url`): fetch a URL, convert it to Markdown, save and index it.
- **Run many calls in one request** (`batch`): up to 50 tool calls, run in order.

Every write follows the same rules:

- **Stored exactly as sent.** Nothing is trimmed, added or converted, line endings included.
- **Backed up first.** Every write that changes or removes an existing file saves a backup
  before it touches the file, and the answer names that backup. If the backup fails, the write
  does not happen.
- **Safe against someone else's change.** With a version stamp from an earlier read, a write
  refuses if the file changed since, instead of overwriting the other change.
- **Try before writing.** Edits and inserts have a dry run that shows the change without making
  it.
- **Searchable at once.** A written file is indexed before the call returns.

## Backups and undo

- Every destructive write leaves a timestamped backup in a `backups` folder inside the project,
  mirroring the original path. Backups are never indexed.
- `list_backups` lists them for one file, a folder, or a time window, which is how a bulk change
  is found and undone as a set.
- `diff_backup` shows what changed since a backup, without restoring anything.
- `restore_backup` puts a backup back. It backs up the current file first, so a restore can be
  undone too.

## Images and OCR

- **PNG assets.** The assistant can publish a generated PNG with metadata (`put_asset`), read it
  back as an image (`get_asset`), inspect it without downloading it (`get_asset_info`), list
  (`list_assets`), update its metadata (`update_asset_metadata`) and remove it
  (`remove_asset`). PNG files added to the folder by other means are brought into the catalog
  with `reindex_assets`.
- **Text from images.** OCR (EasyOCR) reads the text in a PNG (`ocr_asset`) and returns it with
  the regions it came from. That text becomes searchable.
- **Search by description** (`search_assets`): searches asset metadata.

## Indexing

- Indexing is automatic: at start-up, when a file changes on disk, and on every write through
  Cognita.
- A full rebuild or a catch-up after large outside changes runs in the background
  (`reindex_documents`), and its progress can be followed (`get_reindex_status`).
- The index is derived data: it can always be rebuilt from your documents, and
  `cognita reset index` does exactly that.
- **AMD or NVIDIA graphics acceleration (Linux, optional).** Indexing and OCR can use a
  supported AMD card, or an NVIDIA card with driver 580 or newer and the NVIDIA Container
  Toolkit. If the card lacks free memory, is missing or fails, the work falls back to the CPU
  and the index comes out the same. Only the time changes. An AMD card busier than 20% is also
  left alone; an NVIDIA card is used however busy it is, as long as the memory is free.

## Workspace

An optional private Linux machine, one per connector credential (or per claude.ai or ChatGPT
authorization), kept between chats.

- **Files.** List, read (by bytes, by lines or the last lines), write, edit by exact
  replacement, create folders, copy, move, remove and search by name or content
  (`workspace_list_files`, `workspace_read_file`, `workspace_write_file`,
  `workspace_edit_file`, `workspace_make_directory`, `workspace_copy_paths`,
  `workspace_move_paths`, `workspace_remove_paths`, `workspace_search`, `workspace_stat`).
- **Run programs.** Start a command or a shell script as a background job, wait for it or check
  back later, read its output as it runs, and cancel it (`workspace_start_job`,
  `workspace_get_job`, `workspace_cancel_job`). Python, Node.js, git, curl and jq are installed,
  and the assistant can add packages.
- **Move files between a project and the Workspace** (`copy_to_workspace`,
  `copy_from_workspace`). Nothing in the Workspace touches your documents until the assistant
  copies a result back, and only into a project the connector may write and may transfer to.
- **Web search** (`workspace_web_search`) through Brave Search, when you add a Brave API key in
  Admin.
- **Controlled internet access.** The Workspace reaches only the domains and ports you allow in
  Admin.
- **Limits you set.** A disk quota per Workspace, and an idle time after which it stops. A
  Workspace unused for long enough is deleted; **Pin** in Admin keeps one indefinitely. Treat
  it as scratch space: keep anything important in a project.
- **Workspace-only connectors.** A connector that gets a Workspace and no document projects,
  for a tool that only needs a sandbox.
- It needs hardware virtualization. Without it, everything else in Cognita still works.

## Connectors and access

- **A connector is one door into Cognita**, usually one per assistant or person. Each has its own
  MCP address and its own permissions.
- **Which projects it sees:** all of them, or a chosen list. A project can be marked so that
  "all projects" connectors do not include it unless it is granted by name.
- **What it may do in each project:** read only, or read and write. The default applies to every
  project, and any project can be set differently.
- **Workspace per connector:** on or off, and per project whether files may be copied between
  that project and the Workspace.
- **Sign-in.** claude.ai, Claude Desktop and ChatGPT sign in with OAuth: you approve the
  assistant once, in your browser, with your Admin username and password. Other tools use a
  static private key sent as a `Bearer` header. A key is shown once and stored only as a hash. Admin lists
  every live authorization and can revoke it.
- **One project, no argument needed.** When a connector sees exactly one project, tools may omit
  the `project` argument.
- **Public address.** Web-based assistants need a public HTTPS address; see
  [REMOTE-ACCESS.md](REMOTE-ACCESS.md) for Tailscale Funnel, Cloudflare Tunnel or your own
  reverse proxy. The Admin page is never published that way.

## The Admin page

A password-protected page in your browser, on your own machine by default.

- **Projects:** add a project from a documents folder, see its size and index state, exclude
  it from "all projects" connectors, remove it.
- **Connectors:** create and edit connectors and their access, copy their MCP addresses, create
  static keys, see and revoke OAuth authorizations.
- **Workspaces:** every Workspace with its owner, size, last use and deletion date; start, stop, pin
  or remove one; runtime status; quota, idle time, allowed internet destinations and the Brave
  Search key.
- **Settings:** GPU acceleration for indexing and OCR, sign-in settings, and a light, dark or
  system theme.

## Checking that it works

- **Built-in self-test.** Ask the assistant to "run the Cognita self-test". It fetches a
  checklist (`get_self_test_plan`) and runs it against a dedicated Self-Test project, covering
  every tool except `add_from_url`, then reports a scorecard. On Linux, install, update and
  repair run it too.
- `cognita status` shows what is running, the version and the addresses; `cognita diagnostics`
  saves a support file with logs and settings, never passwords, keys or documents. The
  [README](../README.md#everyday-use) lists every command.

## Tool reference

All 57 tools. "Write" tools need read/write access; "Workspace" tools need Workspace on for
the connector.

| Tool | Kind | What it does |
|---|---|---|
| `search_knowledge` | Search | Keyword + meaning search, reranked |
| `search_similar` | Search | Documents close in meaning to a given one |
| `find_literal` | Search | Every exact occurrence of a string or regex, with line numbers |
| `list_documents` | Search | Indexed documents, by category or folder; optional change manifest |
| `list_categories` | Search | Categories and their document counts |
| `list_projects` | Search | Projects this connector can see |
| `get_index_stats` | Search | Index size, model, per-category counts, skip list |
| `evaluate_retrieval` | Search | Scores search against expected answers |
| `get_document` | Read | One whole document |
| `get_documents` | Read | Up to 100 documents in one call |
| `read_document` | Read | A section or a line range of one document |
| `add_document` | Write | Create (or replace) a file |
| `update_document` | Write | Replace a file's whole content |
| `edit_document` | Write | Replace one exact piece of text |
| `edit_document_batch` | Write | Several exact edits to one file, all or nothing |
| `insert_in_document` | Write | Add text at the top, bottom or a section |
| `write_documents` | Write | Up to 100 files, all or nothing |
| `copy_document` | Write | Byte-exact copy of one file |
| `copy_directory` | Write | Byte-exact copy of a folder |
| `move_document` | Write | Move or rename a file |
| `remove_document` | Write | Take one file out of the index, or delete it |
| `remove_documents` | Write | The same for up to 100 files |
| `remove_directory` | Write | The same for a whole folder |
| `add_from_url` | Write | Save a web page as a Markdown document |
| `batch` | Any | Up to 50 tool calls in order |
| `list_backups` | Backups | Backups for a file, folder or time window |
| `diff_backup` | Backups | What changed since a backup |
| `restore_backup` | Write | Put a backup back (itself undoable) |
| `put_asset` | Write | Publish a PNG with metadata |
| `update_asset_metadata` | Write | Change a PNG's metadata |
| `remove_asset` | Write | Remove a PNG and its OCR data |
| `reindex_assets` | Write | Catalog PNG files added outside Cognita |
| `get_asset` | Images | A PNG as an image |
| `get_asset_info` | Images | A PNG's metadata and facts, without the image |
| `list_assets` | Images | Published PNGs |
| `search_assets` | Images | Search PNG metadata |
| `ocr_asset` | Images | Read the text in a PNG |
| `reindex_documents` | Index | Background catch-up or full rebuild |
| `get_reindex_status` | Index | Progress of that rebuild |
| `get_self_test_plan` | Test | The self-test checklist |
| `workspace_info` | Workspace | This connector's Workspace, if it has one yet |
| `workspace_list_files` | Workspace | List files (creates the Workspace on first use) |
| `workspace_stat` | Workspace | Facts about one path |
| `workspace_read_file` | Workspace | Read a file, by bytes or lines |
| `workspace_write_file` | Workspace | Write a file |
| `workspace_edit_file` | Workspace | Exact-text edits to a file |
| `workspace_make_directory` | Workspace | Create a folder |
| `workspace_copy_paths` | Workspace | Copy inside the Workspace |
| `workspace_move_paths` | Workspace | Move inside the Workspace |
| `workspace_remove_paths` | Workspace | Remove files or folders |
| `workspace_search` | Workspace | Search by name or content |
| `workspace_start_job` | Workspace | Run a command or script |
| `workspace_get_job` | Workspace | A job's state and output |
| `workspace_cancel_job` | Workspace | Stop a job |
| `workspace_web_search` | Workspace | Web search through Brave |
| `copy_to_workspace` | Workspace | Copy project files into the Workspace |
| `copy_from_workspace` | Workspace + Write | Copy Workspace files into a project |

Each tool's own description, served to the assistant with the tool, gives every argument.

## What stays stable

- Tool names, arguments and result shapes do not change within a major version, so a connector
  you added keeps working across updates. New tools and new optional arguments can appear at any
  time.
- A tool that is given an argument it does not know refuses the call instead of ignoring it.
- Errors come back in one fixed format with a reason code. Anything that can send an HTTPS POST
  can use the MCP endpoint directly. Document edits preserve the file's exact bytes. See the
  [error reason codes](ERROR-REASONS.md) for the current machine-readable vocabulary.
- Every user-visible change is in [CHANGELOG.md](../CHANGELOG.md).
