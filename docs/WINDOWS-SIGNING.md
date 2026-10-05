# Signing Windows builds

`windows/build_setup.py` signs Cognita Windows builds with Microsoft Azure Artifact Signing when all three signing paths are configured. A signed build signs and verifies the launcher before packaging, asks Inno Setup to sign Setup and its uninstaller, then verifies Setup before writing its `.sha256` file. `--upload` requires this signed path. Local development builds can omit the signing options and remain unsigned.

## Configure a signing workstation

Create a metadata JSON file outside the repository using the values for the Azure Artifact Signing account and certificate profile. Do not add the real file to Git:

```json
{
  "Endpoint": "https://<account-endpoint>/",
  "CodeSigningAccountName": "<account-name>",
  "CertificateProfileName": "<profile-name>"
}
```

Install a Windows SDK version supported by Azure Artifact Signing and the matching Artifact Signing client tools. Use the x64 `signtool.exe` and `Azure.CodeSigning.Dlib.dll` together. Configure authentication on the workstation using an Azure supported credential, such as an authenticated Azure CLI session or managed identity; the metadata file contains account and profile identifiers, not credentials.

### Azure CLI authentication

For an interactive Azure CLI session, sign in to the intended tenant and complete its MFA prompt:

```powershell
az login --tenant <tenant-id>
```

Use the normal interactive sign-in flow; do not add `--use-device-code` by default. `AzureCliCredential` must be able to find Azure CLI. If using a portable Azure CLI installation, add its `bin` directory to `PATH` before running the build.

Set paths for the current PowerShell session:

```powershell
$env:COGNITA_SIGNING_METADATA = 'C:\signing\metadata.json'
$env:COGNITA_SIGNTOOL = 'C:\Program Files (x86)\Windows Kits\10\bin\10.0.26100.0\x64\signtool.exe'
$env:COGNITA_SIGNING_DLIB = 'C:\Program Files (x86)\Microsoft\ArtifactSigningClientTools\bin\Azure.CodeSigning.Dlib.dll'
```

Or pass `--signing-metadata`, `--signtool`, and `--signing-dlib` directly. Explicit command-line paths override their matching environment variables. All three paths must resolve to files. The build uses SHA-256 and Microsoft's Artifact Signing timestamp service (`http://timestamp.acs.microsoft.com`), then runs `signtool verify /pa /all /tw` on the launcher and final Setup.

```powershell
python windows\build_setup.py --image PATH --src PATH --upload
```

`--upload` attaches Setup and its hash only after signing and signature verification succeed. Real account names, endpoints, metadata files, and credentials belong in local configuration or the machine's credential provider, never in committed files.
