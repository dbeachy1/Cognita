# Cognita-Windows operator guide

This private Windows reference install runs the accepted Linux/amd64 CPU image in
Docker inside its own Ubuntu 24.04 WSL2 distribution. It does not use Docker Desktop,
the default WSL distribution, Cognita K, Beta, a Windows port proxy, or personal
documents unless a source is explicitly selected.

## Install from an accepted bundle

Install PowerShell 7 or later and run the installer with `pwsh.exe` as the Windows
account that will run Cognita. Windows PowerShell 5.1 is unsupported; the installer
rejects it before changing installation state. Keep that account logged in for the
login task. The bundle must remain at a stable local path because the
owner record binds Repair and image loading to its accepted files.

```powershell
Set-Location 'B:\Cognita-Windows-Bundle'
pwsh.exe -NoProfile -File .\scripts\windows\Install-CognitaWindows.ps1 -Action Install -BundlePath $PWD.Path -Mode Core
```

Use `-Mode Full` only for a bundle qualified for full mode. The installer verifies all
files against `SHA256SUMS`, records the bundle's release identity, installs the pinned
Ubuntu Docker packages with package-start suppression, prepares source mounts before
Docker starts, and loads the app plus Compose-pinned PostgreSQL/pgvector image from the
single CPU archive. Before Compose, it verifies the database reference, image ID,
repository digest association, and derived archive transport tag alongside the app
image ID and OCI labels. No registry access is needed during startup. It publishes MCP
at `127.0.0.1:10675` and Admin at `127.0.0.1:10676`.

The first install securely prompts for the Admin username and password. A caller can
provide a `PSCredential` through `-AdminCredential` for unattended setup. Cognita's
Argon2id verifier is established before the app container starts; the credential is sent
to the accepted image's bundled `set-admin-credentials.py` over stdin and is never put
in the owner record, command arguments, or environment. Reruns and Repair preserve an
existing verifier and use the supplied credential only to authenticate Admin operations.

Optional document roots are selected explicitly. Aliases are lowercase and unique;
`cognita-self-test` is reserved for Cognita's synthetic Self-Test fixtures.

```powershell
pwsh.exe -NoProfile -File .\scripts\windows\Install-CognitaWindows.ps1 -Action Install -BundlePath $PWD.Path `
  -Mode Core -Source 'manuals=D:\CognitaDocuments\Manuals' `
  -SmbSource 'archive=\\fileserver\share\CognitaArchive'
```

The installer requires selected source directories to be available during initial
identity capture. It stores their locators in owner-only `B:\Cognita-Windows-State\install.json`.
The state directory is outside the closed bundle and is fixed across installs and upgrades.
Its ACL grants the installing account and Administrators Full Control, plus
`BUILTIN\Users` non-inheriting Read/Execute for WSL DrvFS traversal. The record and
all other children remain accessible only to the installing account and Administrators.
There is no `%LOCALAPPDATA%` record, migration, or fallback. It does not
install SMB credentials. A later source outage is reported as unavailable while Docker
and Cognita can start; the source's prior index remains searchable. `Repair` restores a
verified mount after the same source returns.

## Lifecycle

```powershell
pwsh.exe -NoProfile -File .\scripts\windows\Install-CognitaWindows.ps1 -Action Status
pwsh.exe -NoProfile -File .\scripts\windows\Install-CognitaWindows.ps1 -Action Start
pwsh.exe -NoProfile -File .\scripts\windows\Install-CognitaWindows.ps1 -Action Stop
pwsh.exe -NoProfile -File .\scripts\windows\Install-CognitaWindows.ps1 -Action Repair
```

The per-user Scheduled Task invokes `wscript.exe //B` and an owner-only VBScript. The
script starts a hidden `wsl.exe` process and remains alive for the systemd unit lifetime,
preventing WSL idle shutdown while containers are healthy. `Stop` stops that unit and
lets the hidden task exit. Login starts the stack automatically. Neither the global WSL
default distribution nor `.wslconfig` is changed.

Repair uses only the exact recorded bundle and checks the independent owner marker in
the dedicated distro. It verifies source identities before stopping the healthy stack.
An unchanged healthy installation needs no restart. A substituted source becomes
unavailable, and Repair reports the identity mismatch while Cognita keeps running.

A Windows-only Compose overlay supplies the ephemeral schema-1 source identity
projection to Cognita read-only. A changed child bind or projection makes Repair stop
and recreate Cognita, then verify its hidden Scheduled Task keepalive. If the source
startup hook or shared-root propagation needs repair, it also stops Docker, refreshes
the hook, and verifies the root before restarting Docker. Docker's `ExecStartPre` then
runs before Compose can use the single `rslave` source bind. Repair can complete an
interrupted installation from the recorded bundle; it preserves configuration and
secrets. State reset remains a separate explicit action. A mismatched owner marker or
fixed-name resource is refused.

## Explicit state reset and uninstall

Reset is a separate, confirmed operation. It removes only PostgreSQL's derived index
files and Workspace scratch under this installation; it preserves the Toolbox image
cache, source documents, configuration, credentials, and the install record.

```powershell
pwsh.exe -NoProfile -File .\scripts\windows\Install-CognitaWindows.ps1 -Action Reset-State
```

Uninstall removes the scheduled task and stops Cognita and Docker. The distro and its
roots remain unless a second, separately confirmed unregister action is requested.
Unregistering loses the disposable index and Workspace scratch. The owner record and
retained roots are intentionally left for inspection and a possible same-bundle Repair.

```powershell
pwsh.exe -NoProfile -File .\scripts\windows\Install-CognitaWindows.ps1 -Action Uninstall -ConfirmUninstall -ConfirmUnregisterDistro
```

The installer first requests `UNINSTALL Cognita-Windows`, then separately requests
`UNREGISTER Cognita-Windows`. The first removes the login task and application unit;
the second deletes the dedicated distro and its disposable index and Workspace scratch.
Never unregister a distro whose marker does not match
`B:\Cognita-Windows-State\install.json`.

## Diagnostics

`Status` prints health, task state, mode, release version/commit, fixed resource names,
and the number of configured aliases. It does not print source locators, credentials,
or document names. Inspect systemd diagnostics inside the owned distro with:

```powershell
wsl.exe -d Cognita-Windows -u root -- systemctl status cognita-compose.service docker.service
wsl.exe -d Cognita-Windows -u root -- journalctl -u cognita-compose.service -u docker.service --since today
```

The two published ports are loopback-only. If either is already occupied, the installer
stops before creating WSL resources. A failed step reports `Status` as the safe next
command; rerunning the same accepted bundle resumes from the owner record. The
installer does not automatically reset databases, scratch, or source state.
