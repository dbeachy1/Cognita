# Install, update, and operate Cognita

## Install

On Linux, clone the repository and run the installer:

```bash
git clone https://github.com/dbeachy1/Cognita
cd Cognita
./cognita install
```

The installer checks the host, configures Docker when needed, asks for the documents
folder and Admin credentials, and verifies the running service. Ubuntu 24.04 is the
recommended Linux distribution.

On Windows 11, download `Cognita-Setup-<version>.exe` from the project's latest release
and follow Setup. Cognita runs in WSL; Setup checks the system and verifies the service
before it finishes.

For assistant clients that connect from outside the machine, follow
[Remote access](docs/REMOTE-ACCESS.md). Publish only the MCP endpoint. Keep the Admin
page on the local machine or a trusted network.

## Update and verify

On Linux, run the installer command from the repository:

```bash
./cognita update
./cognita status
./cognita logs app
```

On Windows, run a newer Setup package. The installer checks the existing installation
and verifies the updated service. If an update does not work, use `./cognita rollback`
on Linux or the `cognita rollback` command on Windows.

The health endpoint is available at `http://127.0.0.1:8675/healthz` by default. It
reports the service status and version. The Admin interface uses port 8676 by default.
Keep that port private even when the MCP endpoint is reachable remotely.

## Everyday commands

```text
cognita status
cognita start | stop | restart
cognita logs [app|workspace|install] [-f]
cognita add-folder [PATH]
cognita password
cognita remote-access
cognita reset index|workspaces|all
cognita rollback
cognita diagnostics
```

On Linux, prefix the commands with `./` when running them from the repository. On
Windows, Setup adds `cognita` to the command path. Run `cognita --help` for the current
command list.

## Remove Cognita

Use the platform's installer to uninstall Cognita. By default, uninstalling keeps its
index, settings, credentials, workspace state, and source documents. If you choose to
delete Cognita data, review the installer's list and confirmation prompt carefully;
source documents are not managed by Cognita's uninstaller.
