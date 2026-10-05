Cognita lets Claude, ChatGPT and other AI assistants read, search and edit your own documents,
on your own computer. You point it at your folders; it indexes them locally and serves them to
the assistant through the Model Context Protocol (MCP). Your files stay on your machine: the
only things Cognita downloads are its own software and search models.

It also gives the assistant an optional **Workspace**: a private, sandboxed Linux machine where
it can run code, process files and search the web, then copy results back into your documents.

## What you get

- **Search that understands meaning and exact text.** Every question runs a keyword search and a
  meaning search together, merges them and reranks the results. Exact-string search
  (`find_literal`) covers code and config files too.
- **Documents it can read and write.** Markdown, text, PDF, Word, Excel, PowerPoint, CSV, JSON,
  XML and notebooks are indexed. The assistant can create, edit, move and remove documents where
  you allow it. Every destructive change is backed up first and can be restored.
- **Files are kept exactly as written.** What the assistant writes is stored byte for byte, and
  what it reads is the file itself, not a converted copy.
- **Text from images.** Text in PNG images is read with OCR and becomes searchable.
- **Projects and connectors.** Each documents folder is a project. Each connector (one per
  assistant or person) chooses which projects it can see, and whether it can only read or can
  also write. Projects are fully separate from one another.
- **Workspace (optional).** A sandboxed Linux machine per connector, for running programs and
  scripts on copies of your files. Nothing in it touches your documents until the assistant copies
  a result back, and only where that connector may write.
- **An Admin page in your browser**, behind a password, to add folders and projects, create
  connectors, and check that everything works.
- **AMD or NVIDIA graphics acceleration (optional)** for faster indexing and OCR (AMD on Linux;
  NVIDIA on Linux and Windows):
  on an RTX 4090, indexing ran at 112 chunks a second against 3.6 on the CPU. Without a
  supported card, everything runs on the CPU with the same results.

**[Everything Cognita can do](docs/CAPABILITIES.md)**: every feature, every permission and all
57 tools, each in a sentence.

The [error reason codes](docs/ERROR-REASONS.md) document machine-readable error values for
clients.

The [latest GitHub release](https://github.com/dbeachy1/Cognita/releases/latest) offers a
Windows Setup download (`Cognita-Setup-<version>-r<N>.exe`) and a Linux archive
(`cognita-src-<version>.tar.gz`). Both install the same Cognita version. The Linux archive pulls
the published container images during installation.

## Install on Linux

Tested on Ubuntu 24.04, and that is what we recommend; other Linux distributions should work.
Cognita runs in Docker, so what your machine needs is a 64-bit Intel/AMD processor, systemd,
and Docker Engine with the Compose plugin. On Debian, Ubuntu and their relatives the installer
can install Docker for you; elsewhere, install it first with your distribution's own steps.

**You need:** 8 GB of memory, about 15 GB of free disk, and an internet connection for the first
install. That install downloads about 1.5 GB of software (12.6 GB with AMD acceleration, 4.7 GB
with NVIDIA) and 3.6 GB of search models. Workspace also needs hardware virtualization
(`/dev/kvm`); without it, everything else works.

Download `cognita-src-<version>.tar.gz` from the
[latest release](https://github.com/dbeachy1/Cognita/releases/latest), then extract it into a
new directory and run the installer from there:

```bash
mkdir Cognita && tar -xzf cognita-src-15.6.0.tar.gz -C Cognita
cd Cognita
./cognita install
```

The install asks only what it cannot know:

1. Your documents folder (default `~/Documents`).
2. An Admin username (default `admin`) and a password.
3. CPU, AMD or NVIDIA acceleration, only when a suitable card is present.
4. Whether to turn on Workspace, only when the machine supports it.
5. Whether to set up remote access now (see below; you can do it later).

**AMD acceleration** has been tested on Linux with dual AMD R9700 GPUs.

**NVIDIA acceleration** needs the NVIDIA driver at version 580 or newer and the
[NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html),
which gives Docker its `nvidia` runtime. The installer offers NVIDIA only when both are there,
and tries the card with a real inference before it relies on it. It has been proven on an RTX
4090 under WSL2; a plain Linux machine with an NVIDIA card has not been tested yet.

It shows its plan before it changes anything. If Docker is missing, it says what it will install
and asks first. It downloads prebuilt software (nothing is compiled on your machine), starts
Cognita as a service that comes back after a reboot, and runs a full self-test before it says it
is done.

## Install on Windows

For Windows 11 on a 64-bit PC (not Windows on ARM). Cognita runs inside WSL, Windows' built-in
Linux support; Setup turns it on for you if it is not on yet, and you never open a Linux window.

**You need:** 16 GB of memory, about 12 GB of free disk, hardware virtualization turned on in
the BIOS/UEFI (most PCs have it on; on a virtual machine, nested virtualization), and an
internet connection for the first install. That install downloads about 1.5 GB of software and
3.6 GB of search models.

Download the Windows Setup executable from the
[latest release](https://github.com/dbeachy1/Cognita/releases/latest) and run it. You do not
need to be an administrator; Windows asks your permission once, the first time, to turn on WSL.
Setup is signed and timestamped with Microsoft Azure Artifact Signing. Its verified publisher
is **Douglas Beachy**. To check the download, right-click the executable, open **Properties >
Digital Signatures**, and verify the signature. The release includes its SHA-256 checksum.

Setup checks your PC first and says exactly what is wrong, and how to fix it, before it changes
anything. Then it asks only what it cannot know:

1. Your projects folder: the folder whose documents Cognita should search. It works in that
   folder and its subfolders, in place; nothing outside it is visible to Cognita. It must be on
   one of this PC's own drives, not a network share. If it is in OneDrive, right-click it and
   choose **Always keep on this device**, so its files are really on the disk.
2. An Admin username (default `admin`) and a password.
3. CPU or NVIDIA acceleration, only when the PC has an NVIDIA graphics card. NVIDIA needs the
   card's driver at version 580 or newer (Setup says so, with the download link, when it is
   older) and downloads 4.7 GB instead of 0.6 GB. Setup tries the card with real work while it
   installs; if Cognita cannot use it, it installs for the CPU and says why. Nothing extra is
   installed on Windows. Tested through Setup on an RTX 4090.
4. Whether to set up remote access now (recommended; see below). Assistants such as claude.ai,
   Claude Desktop, ChatGPT and Gemini need it; a tool on this PC that reaches Cognita itself
   (Claude Code, Codex, Google Antigravity, Cursor) does not.

**Advanced** on the last page before installing changes the ports, where Cognita keeps its data
(for a PC with a small C: drive), or turns Workspace off. The same page has a box, ticked, that
lets Windows take back memory WSL holds as file cache (WSL keeps the cache from each download in
RAM and never returns it by default). It adds
one line, `autoMemoryReclaim=dropCache`, to `%USERPROFILE%\.wslconfig`; that applies to all your
WSL distros from the next time WSL starts. Untick it to leave the file alone. The box is not shown
when your `.wslconfig` already has an `autoMemoryReclaim` setting, or cannot be read.

The first time, turning on WSL needs one restart; Setup opens again by itself after you sign
back in (if it does not, run the Setup file again: it carries on where it stopped). The install
then takes about five minutes, most of it downloading, then a full self-test before Setup says
it is done. You can skip the self-test with a button and run it later by running Setup again.
The Finished page shows the Admin address and user, the MCP address and the everyday
commands.

**Every day.** Cognita starts whenever you sign in to Windows. The Start menu has Cognita Admin,
Cognita Status and Cognita Diagnostics, and the `cognita` command works from any terminal:
`cognita status`, `start | stop | restart`, `logs [app|workspace|install] [-f]`,
`add-folder [PATH]` (another projects folder; without a path it opens a folder picker),
`password`, `remote-access`, `reset index|workspaces|all`, `rollback` and `diagnostics`. If a
projects folder's drive is not connected, Cognita starts anyway, `cognita status` says the folder
is not available, nothing is removed from its index, and `cognita restart` reconnects it once the
drive is back.

**Update and repair.** Run a newer Setup; it asks for your Admin password, updates Cognita, and
proves the result. Your data is kept, and `cognita rollback` returns to the previous version if
an update fails. Running the same Setup again repairs an install. Ports can only be changed by
running Setup again and choosing **Advanced**.

**Uninstall.** Use **Settings > Apps > Installed apps**. By default Cognita's program and its
sign-in task are removed and the index, settings, credentials and Workspace are kept in
Cognita's WSL disk, so installing again reuses them. To delete the data too, Setup lists exactly
what will go and asks you to type `DELETE`. Your documents are never touched.

**When something goes wrong.** Setup shows the stage, what it saw and how to fix it, saves
`Cognita-diagnostics-<computer>-<date>.zip` on your Desktop by itself, and says where it is
(**Show the file** opens it). At any other time, `cognita diagnostics` writes the same file. It
holds Cognita's logs and settings, WSL and Windows facts, and a README of what is inside; never
passwords, keys, tokens or your documents. Nothing is ever sent anywhere by itself. To get help,
attach it to a [new issue](https://github.com/dbeachy1/Cognita/issues/new); the **Report a
problem** link in Setup goes there. Common causes Setup names for you: a port in use (choose
others under **Advanced**), `localhostForwarding=false` in your `.wslconfig`, a WSL distro named
Cognita that Setup did not create, or Docker Desktop's WSL integration turned on for Cognita
(leave it unchecked).

## Connect your assistant

1. Open the Admin page. The install's finish screen shows its address, for example
   `http://127.0.0.1:8676`.
2. Under **Connectors**, create a connector, choose its projects and whether it may write, and
   copy its **Stable MCP URL**.
3. Give that URL to your assistant.

A program on your PC that connects to an MCP address itself (Claude Code, Cursor, VS Code,
SillyTavern's MCP plugin) uses the local address as shown. claude.ai, Claude Desktop, ChatGPT
and other assistants whose own servers make the connection need a public HTTPS address for
your Cognita; `./cognita remote-access` sets one up with Tailscale Funnel.
[docs/REMOTE-ACCESS.md](docs/REMOTE-ACCESS.md) explains it and the alternatives: Cloudflare
Tunnel, or your own reverse proxy. The Admin page is never published this way; it stays on
your machine or your network.

**The Admin password also guards the public address.** When an assistant such as ChatGPT or
claude.ai connects through OAuth, its sign-in page is on the public address and asks for the
Admin user and password. So once remote access is on, that password is reachable from the
internet: make it a strong one. Five wrong passwords in a minute block further tries from that
address for a while.

**Change the Admin password** from a terminal (the Admin page has no setting for it):

```bash
cognita password
```

That is the Windows command; on Linux run `./cognita password` in the clone. It asks for the new
password twice (not the old one: being able to run it on this machine is the proof), then
restarts Cognita, which takes a few seconds. Every assistant signed in through OAuth is signed
out and must sign in again with the new password; connectors that use their Stable MCP URL
are not affected. On Windows, Setup asks for the new password the next time it updates or
repairs.

## Everyday use

```bash
./cognita status                 # what is running, which version, where the Admin page is
./cognita logs [app|workspace] [-f]
./cognita start | stop | restart
./cognita add-folder PATH        # another documents folder
./cognita password               # change the Admin password
./cognita remote-access          # set up or change the public address
./cognita update                 # move to the latest release, then prove it works
./cognita rollback               # go back to the previous release
./cognita reset index|workspaces|all
./cognita uninstall [--delete-data]
```

`./cognita` works from any folder inside the clone. `update` and `rollback` run the self-test
before they report success. `uninstall` keeps your documents, settings and index unless you add
`--delete-data`; it never deletes your documents folders.

## The tools your assistant gets

- **Search:** by meaning and keywords together, by similarity to a document, and exhaustive
  exact-text search with line numbers.
- **Read:** whole documents, up to 100 at once, or one section or line range.
- **Write** (only where the connector may write): create, replace, make exact edits, insert,
  write a set of files all or nothing, copy, move, remove, save a web page.
- **Backups:** list, compare and restore the backup taken before every change.
- **Images:** publish PNGs, read their text with OCR, search their metadata.
- **Workspace:** files, commands and web search in the sandbox, and copies to and from projects.

[docs/CAPABILITIES.md](docs/CAPABILITIES.md#tool-reference) lists all 57 tools by name.

The tool names and shapes are stable across compatible releases. Anything that can send an
HTTPS POST can use the MCP endpoint directly. Errors include a machine-readable reason, and
document edits preserve the file's exact bytes.

## Security and privacy

- Cognita listens only on your own machine (`127.0.0.1`) unless you choose otherwise.
- The Admin page needs a password. Cognita stores only a hash of it, never the password.
- A connector's key is shown once and stored only as a hash. Keys never appear in logs.
- Your documents and index never leave your machine. The downloads are Cognita's software and
  the open search and OCR models.

## Under the hood

PostgreSQL with pgvector holds the index, one schema per project. Search uses
`BAAI/bge-large-en-v1.5` for meaning, PostgreSQL full-text search for keywords, reciprocal rank
fusion to merge them, and `BAAI/bge-reranker-v2-m3` to rerank. EasyOCR reads images. Workspace
runs on Microsandbox virtual machines. Everything runs in Docker containers, managed by a user
systemd service.

See [Everything Cognita can do](docs/CAPABILITIES.md) for the public tool and permission
catalog, and [CHANGELOG.md](CHANGELOG.md) for release history.

## Development

```bash
python3 -m venv venv && venv/bin/pip install -e .[dev]
venv/bin/python -m pytest
venv/bin/python -m ruff check src/ tests/
```

Tests never depend on the wall clock and need no network. The tests that need PostgreSQL skip
when it is not available.

## License

Cognita is licensed under the [Apache License 2.0](LICENSE). The third-party components and
models it uses, and their licenses, are listed in
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

## Word temporary files and watcher retries

While a document is open, Microsoft Word may create an owner/lock file named
`~$*.docx`. These temporary files are not DOCX documents. If a watcher warning
says `reason=per-path reconciliation failure` and its associated `paths=` entry
names one of these files, add `~$*.docx` to `index_exclude_patterns` in your active
`cognita.yaml`, preserving any existing exclusions. For example:

```yaml
index_exclude_patterns:
  - backups
  - "~$*.docx"
```

Restart Cognita to apply the setting: run `./cognita restart` on Linux or
`cognita restart` on Windows. The exclusion applies to watcher events and
directory scans; normal Word documents remain indexable. A retry warning for
another path requires checking that path's underlying error.

## Languages

Cognita's Windows installer and web UI (Admin and OAuth) support U.S. English,
Spanish, French, German, Italian, and Brazilian Portuguese. Diagnostic logging
and console scripts remain in English.
