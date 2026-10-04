"""Focused contract tests for Worker C's Windows installer and source helper."""
from __future__ import annotations

import base64
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from contextlib import contextmanager
from types import SimpleNamespace
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
WINDOWS = REPO / "scripts" / "windows"


@contextmanager
def owned_temp():
    with tempfile.TemporaryDirectory(prefix="cognita-windows-worker-c-test-") as temporary:
        root = Path(temporary)
        yield root
    if root.exists():
        raise AssertionError(f"task-owned temporary directory was not removed: {root}")


def _load_helper():
    path = WINDOWS / "Prepare-Sources.py"
    spec = importlib.util.spec_from_file_location("cognita_windows_prepare_sources", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run_powershell(test_script: str) -> subprocess.CompletedProcess[str]:
    powershell = shutil.which("pwsh") or shutil.which("powershell.exe")
    if powershell is None:
        raise unittest.SkipTest("PowerShell is required to exercise the Windows installer")
    with tempfile.TemporaryDirectory(prefix="cognita-windows-powershell-test-") as temporary:
        root = Path(temporary)
        script_path = root / "installer-contract-test.ps1"
        script_path.write_bytes(b"\xef\xbb\xbf" + test_script.encode("utf-8"))
        result = subprocess.run(
            [powershell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(script_path)],
            check=False,
            capture_output=True,
            text=True,
        )
    if root.exists():
        raise AssertionError(f"task-owned PowerShell test directory was not removed: {root}")
    return result


class WindowsCpuWorkerCTests(unittest.TestCase):
    def test_verify_bundle_requires_postgres_metadata_and_current_compose_reference(self):
        installer = (WINDOWS / "Install-CognitaWindows.ps1").read_text(encoding="utf-8")
        start = installer.index("function Get-PostgresComposeImageReference(")
        end = installer.index("\nfunction Secure-Path(", start)
        definitions = installer[start:end]
        reference = "registry.example/pgvector:pg18@sha256:" + "a" * 64
        base_metadata = (
            "version: 13.5.0\ncommit: " + "b" * 40 +
            "\nimage_ref_cognita_cpu: cognita/app:accepted\nimage_cognita_cpu: sha256:" + "c" * 64 +
            "\nbundle_mode: core\n"
        )
        valid_metadata = base_metadata + f"image_ref_postgres: {reference}\nimage_postgres: sha256:{'d' * 64}\n"
        wrong_metadata = base_metadata + f"image_ref_postgres: elsewhere.example/pgvector:pg18@sha256:{'a' * 64}\nimage_postgres: sha256:{'d' * 64}\n"
        with tempfile.TemporaryDirectory(prefix="cognita-windows-postgres-bundle-test-") as temporary:
            root = Path(temporary)
            (root / "compose.yaml").write_text(f"services:\n  postgres:\n    image: {reference}\n", encoding="utf-8")
            test_script = f"""
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$Mode=$null
{definitions}
$root='{str(root).replace("'", "''")}'
$base='{base_metadata.replace("'", "''")}'
$valid='{valid_metadata.replace("'", "''")}'
$wrong='{wrong_metadata.replace("'", "''")}'
function Write-Sums {{
    $rows=@()
    foreach($name in @('compose.yaml','release.txt')){{
        $hash=(Get-FileHash -Algorithm SHA256 -LiteralPath (Join-Path $root $name)).Hash.ToLowerInvariant()
        $rows+=\"$hash  $name\"
    }}
    [IO.File]::WriteAllLines((Join-Path $root 'SHA256SUMS'),$rows)
}}
[IO.File]::WriteAllText((Join-Path $root 'release.txt'),$base)
Write-Sums
$missing=$false
try{{ $null=Verify-Bundle $root }}catch{{ $missing=$_.Exception.Message -ceq 'release.txt is missing image_ref_postgres.' }}
if(-not $missing){{throw 'Bundle missing PostgreSQL metadata was accepted.'}}
[IO.File]::WriteAllText((Join-Path $root 'release.txt'),$valid); Write-Sums
$accepted=Verify-Bundle $root
if($accepted.Metadata.image_ref_postgres -cne '{reference}'){{throw 'Current Compose database pin was not accepted.'}}
[IO.File]::WriteAllText((Join-Path $root 'release.txt'),$wrong); Write-Sums
$rejected=$false
try{{ $null=Verify-Bundle $root }}catch{{ $rejected=$_.Exception.Message -ceq 'Bundle PostgreSQL reference differs from the base Compose postgres image.' }}
if(-not $rejected){{throw 'Bundle from a different repository was accepted.'}}
"""
            result = _run_powershell(test_script)
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertFalse(root.exists(), f"task-owned PostgreSQL bundle fixture remains: {root}")

    def test_postgres_image_is_verified_by_exact_id_repo_digest_and_transport_tag(self):
        installer = (WINDOWS / "Install-CognitaWindows.ps1").read_text(encoding="utf-8")
        start = installer.index("function Read-RequiredLastLine(")
        end = installer.index("\nfunction Verify-Bundle(", start)
        definitions = installer[start:end]
        reference = "registry.example/team/pgvector:pg18@sha256:" + "a" * 64
        expected_id = "sha256:" + "d" * 64
        repo_digest = "registry.example/team/pgvector@sha256:" + "a" * 64
        transport = "registry.example/team/pgvector:cognita-transport-" + "a" * 64
        with tempfile.TemporaryDirectory(prefix="cognita-windows-postgres-identity-test-") as temporary:
            root = Path(temporary)
            (root / "compose.yaml").write_text(f"services:\n  postgres:\n    image: {reference}\n", encoding="utf-8")
            test_script = f"""
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$script:ExpectedId = '{expected_id}'
$script:ExpectedRepo = '{repo_digest}'
$script:InspectCalls = [Collections.Generic.List[string]]::new()
{definitions}
function Wsl([string[]]$CommandArguments,[int]$Timeout=1800) {{
    if($CommandArguments -contains 'inspect') {{
        $ref=[string]$CommandArguments[-1]; $script:InspectCalls.Add($ref)
        if($CommandArguments -contains '{{{{.Id}}}}') {{
            if($ref -eq 'substituted') {{ return @('sha256:'+('e'*64)) }}
            return @($script:ExpectedId)
        }}
        if($CommandArguments -contains '{{{{json .RepoDigests}}}}') {{ return @('[\"'+$script:ExpectedRepo+'\"]') }}
    }}
    throw 'Unexpected PostgreSQL verification command.'
}}
$bundle=[pscustomobject]@{{Root='{str(root).replace("'", "''")}';Metadata=@{{image_ref_postgres='{reference}';image_postgres=$script:ExpectedId}}}}
Verify-PostgresBundleImage $bundle
if($script:InspectCalls.Count -ne 3 -or $script:InspectCalls[0] -cne '{reference}' -or $script:InspectCalls[1] -cne '{reference}' -or $script:InspectCalls[2] -cne '{transport}') {{ throw 'All three PostgreSQL identities were not inspected.' }}
$bundle.Metadata.image_ref_postgres='another.example/team/pgvector:pg18@sha256:'+('a'*64)
$wrongRepository=$false
try {{ Verify-PostgresBundleImage $bundle }} catch {{ $wrongRepository=$_.Exception.Message -ceq 'Bundle PostgreSQL reference differs from the base Compose postgres image.' }}
if(-not $wrongRepository) {{ throw 'A database from another repository was accepted.' }}
$bundle.Metadata.image_ref_postgres='{reference}'; $script:ExpectedId='sha256:'+('e'*64)
$substituted=$false
try {{ Verify-PostgresBundleImage $bundle }} catch {{ $substituted=$_.Exception.Message -ceq 'PostgreSQL digest reference resolves to an image ID different from release.txt.' }}
if(-not $substituted) {{ throw 'A substituted database image ID was accepted.' }}
$script:ExpectedId='{expected_id}'; $script:ExpectedRepo='registry.example/team/other@sha256:'+('a'*64)
$wrongDigest=$false
try {{ Verify-PostgresBundleImage $bundle }} catch {{ $wrongDigest=$_.Exception.Message -ceq 'PostgreSQL image lacks the normalized repository@digest association required by base Compose.' }}
if(-not $wrongDigest) {{ throw 'A wrong repository digest association was accepted.' }}
"""
            result = _run_powershell(test_script)
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertFalse(root.exists(), f"task-owned PostgreSQL fixture remains: {root}")

    def test_archive_checksum_helper_requires_one_entry_and_matches_full_hash(self):
        installer = (WINDOWS / "Install-CognitaWindows.ps1").read_text(encoding="utf-8")
        function_start = installer.index("function Verify-AcceptedArchiveChecksum(")
        function_end = installer.index("\n\nfunction Verify-Bundle", function_start)
        definition = installer[function_start:function_end]
        with tempfile.TemporaryDirectory(prefix="cognita-windows-checksum-test-") as temporary:
            root = Path(temporary)
            root_literal = str(root).replace("'", "''")
            test_script = f"""
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
{definition}
$root = '{root_literal}'
$archive = Join-Path $root 'cognita-cpu.tar'
$checksums = Join-Path $root 'SHA256SUMS'
[IO.File]::WriteAllBytes($archive, [byte[]](1,2,3,4,5))
$actual = (Get-FileHash -Algorithm SHA256 -LiteralPath $archive).Hash.ToUpperInvariant()
[IO.File]::WriteAllText($checksums, "$actual  cognita-cpu.tar`n")
$verified = Verify-AcceptedArchiveChecksum $checksums 'cognita-cpu.tar' $archive
if($verified -cne $actual){{throw 'Exactly-one checksum entry returned an unexpected digest.'}}

[IO.File]::WriteAllText($checksums, "")
$missingRejected = $false
try {{$null=Verify-AcceptedArchiveChecksum $checksums 'cognita-cpu.tar' $archive}} catch {{$missingRejected=$_.Exception.Message -ceq 'Archive must have exactly one accepted checksum entry.'}}
if(-not $missingRejected){{throw 'Missing checksum entry was not rejected with the safe cardinality error.'}}

[IO.File]::WriteAllText($checksums, "$actual  cognita-cpu.tar`n$actual  cognita-cpu.tar`n")
$duplicateRejected = $false
try {{$null=Verify-AcceptedArchiveChecksum $checksums 'cognita-cpu.tar' $archive}} catch {{$duplicateRejected=$_.Exception.Message -ceq 'Archive must have exactly one accepted checksum entry.'}}
if(-not $duplicateRejected){{throw 'Duplicate checksum entries were not rejected with the safe cardinality error.'}}

[IO.File]::WriteAllText($checksums, (('0' * 64) + "  cognita-cpu.tar`n"))
$mismatchRejected = $false
try {{$null=Verify-AcceptedArchiveChecksum $checksums 'cognita-cpu.tar' $archive}} catch {{$mismatchRejected=$_.Exception.Message -ceq 'Archive does not match its accepted checksum.'}}
if(-not $mismatchRejected){{throw 'A full SHA-256 mismatch was not rejected safely.'}}
"""
            result = _run_powershell(test_script)
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertFalse(root.exists(), f"task-owned checksum fixture remains: {root}")

    def test_launcher_task_principal_matches_current_account_by_sid(self):
        installer = (WINDOWS / "Install-CognitaWindows.ps1").read_text(encoding="utf-8")
        function_start = installer.index("function Resolve-TaskPrincipalSid(")
        function_end = installer.index("\nfunction Stop-TaskSession", function_start)
        definitions = installer[function_start:function_end]
        test_script = f"""
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$script:Root = 'B:\\Cognita-Windows-State'
$env:SystemRoot = [Environment]::GetEnvironmentVariable('SystemRoot')
{definitions}
$current = [Security.Principal.WindowsIdentity]::GetCurrent().Name
$short = $current.Split('\\')[-1]
$action = [pscustomobject]@{{Execute=(Join-Path $env:SystemRoot 'System32\\wscript.exe'); Arguments='//B "B:\\Cognita-Windows-State\\bin\\launch-cognita.vbs"'}}
foreach($userId in @($short, $current)) {{
    $task = [pscustomobject]@{{Actions=@($action); Principal=[pscustomobject]@{{UserId=$userId;LogonType='Interactive'}}}}
    Assert-LauncherTask $task
}}

$different = [Security.Principal.NTAccount]::new('BUILTIN\\Users').Translate([Security.Principal.SecurityIdentifier])
$currentSid = [Security.Principal.WindowsIdentity]::GetCurrent().User
if($different.Value -ceq $currentSid.Value){{throw 'The distinct-principal fixture unexpectedly resolved to the current user.'}}
$rejectedIds = @('BUILTIN\\Users', 'MAIA\\')
foreach($userId in $rejectedIds) {{
    $task = [pscustomobject]@{{Actions=@($action); Principal=[pscustomobject]@{{UserId=$userId;LogonType='Interactive'}}}}
    $rejected = $false
    try {{ Assert-LauncherTask $task }} catch {{$rejected=$_.Exception.Message -ceq 'A Scheduled Task with the Cognita-Windows name has an unrecognized action or principal; it was left unchanged.'}}
    if(-not $rejected){{throw "Invalid or different task principal was accepted: $userId"}}
}}

$wrongAction = [pscustomobject]@{{Actions=@([pscustomobject]@{{Execute=(Join-Path $env:SystemRoot 'System32\\notepad.exe');Arguments='//B "B:\\Cognita-Windows-State\\bin\\launch-cognita.vbs"'}}); Principal=[pscustomobject]@{{UserId=$current;LogonType='Interactive'}}}}
$rejected = $false
try {{ Assert-LauncherTask $wrongAction }} catch {{$rejected=$true}}
if(-not $rejected){{throw 'A task with a different launcher action was accepted.'}}
$wrongLogon = [pscustomobject]@{{Actions=@($action); Principal=[pscustomobject]@{{UserId=$current;LogonType='Password'}}}}
$rejected = $false
try {{ Assert-LauncherTask $wrongLogon }} catch {{$rejected=$true}}
if(-not $rejected){{throw 'A task with a different logon type was accepted.'}}
"""
        result = _run_powershell(test_script)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)

    def test_real_run_and_wsl_forward_exact_ordered_argv_to_fake_commands(self):
        import sys
        installer = (WINDOWS / "Install-CognitaWindows.ps1").read_text(encoding="utf-8")
        definitions = installer.split("\n$mutex=", 1)[0]
        python = str(sys.executable).replace("'", "''")
        # Replace only external executable resolution. The real native process
        # helper executes a harmless Python logger, which observes every argument.
        harness = f"""
$script:NativeProcess = ${{function:Invoke-OwnedProcess}}
function Invoke-OwnedProcess([string]$Exe,[string[]]$CommandArguments,[int]$Timeout,$InputText=$null) {{
    $logger='import json,sys; print(json.dumps(sys.argv[1:]))'
    return & $script:NativeProcess '{python}' (@('-c',$logger,$Exe)+$CommandArguments) $Timeout $InputText
}}
"""
        test_script = f"""
{definitions}
{harness}
$runExpected = @('first', '', 'two words', '--flag', 'last')
$runObserved = @((Run 'fake-argv-command' $runExpected 30) | ConvertFrom-Json)
if ($runObserved[0] -cne 'fake-argv-command' -or ($runObserved[1..($runObserved.Count-1)] -join "`n") -cne ($runExpected -join "`n") -or $runObserved.Count -ne ($runExpected.Count+1)) {{
    throw "Run changed argv: count=$($runObserved.Count) values=$($runObserved -join '|')"
}}
$wslInput = @('bash', '-lc', 'printf %s "two words"', '--literal', '')
$wslObserved = @((Wsl $wslInput 41) | ConvertFrom-Json)
$wslExpected = @('wsl.exe','-d', 'Cognita-Windows', '-u', 'root', '--exec','timeout','--signal=TERM','--kill-after=5s','41s') + $wslInput
if (($wslObserved -join "`n") -cne ($wslExpected -join "`n") -or $wslObserved.Count -ne $wslExpected.Count) {{
    throw "Wsl changed argv: count=$($wslObserved.Count) values=$($wslObserved -join '|')"
}}
"""
        # Run/Wsl stay intact; the process boundary still executes a real native child.
        self.assertNotIn("function Run(", harness)
        self.assertNotIn("function Wsl(", harness)
        result = _run_powershell(test_script)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)

    def test_install_first_run_normalizes_and_consumes_bundle_and_record_paths(self):
        import sys
        from test_windows_recovery_control import run_ps
        with owned_temp() as root:
            logger = root / "native-wsl-boundary.py"
            calls = root / "native-calls.jsonl"
            logger.write_text(
                "import json, sys\n"
                "log, exe, *arguments = sys.argv[1:]\n"
                "with open(log, 'a', encoding='utf-8') as stream:\n"
                "    stream.write(json.dumps({'Exe': exe, 'Args': arguments}) + '\\n')\n"
                "paths = {'B:/Cognita-Windows-Bundle': '/mnt/b/Cognita-Windows-Bundle', "
                "'B:/Cognita-Windows-State/install.json': '/mnt/b/Cognita-Windows-State/install.json'}\n"
                "if 'wslpath' in arguments:\n"
                "    print(paths[arguments[-1]])\n", encoding="utf-8",
            )
            def quote(value):
                return str(value).replace("'", "''")
            test_script = f"""
$script:Distro = 'Cognita-Windows'
$script:RecordPath = 'B:\\Cognita-Windows-State\\install.json'
$script:SavedRecords=0
function Save-Record($Record) {{$script:SavedRecords++}}
$script:NativeProcess=${{function:Invoke-OwnedProcess}}
function Invoke-OwnedProcess([string]$Exe,[string[]]$CommandArguments,[int]$Timeout,$InputText=$null,[scriptblock]$OnInterrupted=$null,[switch]$AwaitBoundedCompletion,[scriptblock]$OnStarted=$null) {{
    return & $script:NativeProcess '{quote(sys.executable)}' (@('{quote(logger)}','{quote(calls)}',$Exe)+$CommandArguments) $Timeout -OnStarted $OnStarted
}}
$bundle=[pscustomobject]@{{Root='B:\\Cognita-Windows-Bundle';Metadata=@{{bundle_mode='core';version='13.5.0';toolbox_version='';image_ref_postgres='pgvector/pgvector:pg18@sha256:{'a' * 64}';image_postgres='sha256:{'d' * 64}'}}}}
$synthetic=[pscustomobject]@{{alias='cognita-self-test';source_kind='installation_ext4';locator='/srv/cognita/sources/cognita-self-test';canonical_identity='old-ext4';last_runtime_identity=@{{device=1;inode=2}}}}
$external=[pscustomobject]@{{alias='manuals';source_kind='ntfs';locator='D:\\synthetic';canonical_identity='external-identity';last_runtime_identity=@{{device=3;inode=4}}}}
$record=[pscustomobject]@{{installation_id='11111111-2222-4333-8444-555555555555';image_ref_cognita_cpu='cognita/app:accepted';image_cognita_cpu='sha256:{'c' * 64}';version='13.5.0';commit='{'1' * 40}';mode='core';sources=@($synthetic,$external)}}
$created=Install-FirstRun $bundle $record
if(-not $created -or $script:SavedRecords -ne 1 -or $null -ne $synthetic.canonical_identity -or $null -ne $synthetic.last_runtime_identity -or $external.canonical_identity -cne 'external-identity'){{throw 'FirstRun did not recapture only the recreated synthetic identity'}}
$nativeCalls=@(Get-Content -LiteralPath '{quote(calls)}'|ForEach-Object {{$_|ConvertFrom-Json}})
$pathCalls=@($nativeCalls | Where-Object {{$_.Args -contains 'wslpath'}})
if($pathCalls.Count -ne 2) {{ throw "Expected two path conversions, got $($pathCalls.Count)." }}
$actualInputs=@($pathCalls | ForEach-Object {{[string]$_.Args[-1]}})
if(($actualInputs -join "`n") -cne "B:/Cognita-Windows-Bundle`nB:/Cognita-Windows-State/install.json") {{ throw "Wrong normalized inputs: $($actualInputs -join ', ')" }}
$setup=@($nativeCalls | Where-Object {{$_.Args -contains 'bash' -and $_.Args[-1] -like '*mktemp /run/cognita-install.XXXXXX*'}} | Select-Object -Last 1)
if($setup.Count -ne 1) {{ throw 'First-run setup command was not issued.' }}
$outer=[string]$setup[0].Args[-1]
if($outer.Contains("`r") -or -not $outer.Contains('trap')){{throw 'Bootstrap launcher lacks LF normalization or cleanup'}}
$encoded=[regex]::Match($outer, "printf %s '([^']+)' \\| base64 -d").Groups[1].Value
if(-not $encoded) {{ throw 'Could not locate the encoded first-run script.' }}
$decoded=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($encoded))
if($decoded.Contains("`r")){{throw 'Generated Linux bootstrap retained CRLF'}}
foreach($linuxPath in @('/mnt/b/Cognita-Windows-Bundle','/mnt/b/Cognita-Windows-State/install.json')) {{
    $pathB64=[Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($linuxPath))
    if(-not $decoded.Contains($pathB64)) {{ throw "Converted Linux path was not passed downstream: $linuxPath" }}
}}
if($bundle.Root -cne 'B:\\Cognita-Windows-Bundle' -or $script:RecordPath -cne 'B:\\Cognita-Windows-State\\install.json') {{ throw 'WSL conversion changed a persisted Windows path.' }}
foreach($call in @($nativeCalls|Where-Object {{$_.Args[0] -ceq '-d'}})){{
    if($call -eq $setup[0]){{
        if(($call.Args[0..6] -join '|') -cne '-d|Cognita-Windows|-u|root|--exec|bash|-lc' -or $call.Args[8] -cne 'cognita-owned-launch' -or $call.Args[10] -notmatch '^cognita-windows-command-[0-9a-f]{{32}}\\.service$' -or $call.Args[11] -cne '3600' -or ($call.Args[12..13] -join '|') -cne 'bash|-lc'){{throw 'FirstRun long command lost manager ownership or exact original argv'}}
        if($call.Args[7] -notlike '*--quiet*--expand-environment=no*'){{throw 'Manager wrapper can pollute output or expand literal arguments'}}
    }} else {{
        $deadline=if($call.Args -contains 'cognita-owned-cleanup'){{'65s'}}else{{'30s'}}
        $expectedBound='-d|Cognita-Windows|-u|root|--exec|timeout|--signal=TERM|--kill-after=5s|'+$deadline
        if(($call.Args[0..8] -join '|') -cne $expectedBound){{throw 'FirstRun native WSL forwarding lost ordered command arguments or timeout'}}
    }}
}}
"""
            # All production forwarding wrappers and FirstRun stay intact. Only the
            # native process and record-storage boundaries use synthetic resources.
            self.assertNotIn("function Run(", test_script)
            self.assertNotIn("function Wsl(", test_script)
            result = run_ps(test_script)
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)

    def test_every_installer_wslpath_consumer_uses_required_conversion_helper(self):
        installer = (WINDOWS / "Install-CognitaWindows.ps1").read_text(encoding="utf-8")
        self.assertIn("$bundleWsl=Convert-WindowsPathToWsl -Path $Bundle.Root", installer)
        self.assertIn("$recordWsl=Convert-WindowsPathToWsl -Path $script:RecordPath", installer)
        self.assertIn("$tarWsl=Convert-WindowsPathToWsl -Path $tar", installer)
        self.assertIn("$path=Convert-WindowsPathToWsl -Path $file", installer)
        direct_calls = [line for line in installer.splitlines() if "'wslpath'" in line]
        self.assertEqual(len(direct_calls), 1, "wslpath invocation must be centralized in the conversion helper")
        support_start = installer.index("function Say(")
        support_end = installer.index("\n\nfunction Verify-Bundle", support_start)
        paths = [
            (r"B:\Cognita-Windows-Bundle", "B:/Cognita-Windows-Bundle", "/mnt/b/Cognita-Windows-Bundle"),
            (r"B:\Cognita-Windows-State\install.json", "B:/Cognita-Windows-State/install.json", "/mnt/b/Cognita-Windows-State/install.json"),
            (r"B:\Cognita-Windows-Bundle\cognita-cpu.tar", "B:/Cognita-Windows-Bundle/cognita-cpu.tar", "/mnt/b/Cognita-Windows-Bundle/cognita-cpu.tar"),
            (r"B:\Cognita-Windows-Bundle\workspace-runtime.tar", "B:/Cognita-Windows-Bundle/workspace-runtime.tar", "/mnt/b/Cognita-Windows-Bundle/workspace-runtime.tar"),
            (r"B:\Cognita-Windows-Bundle\toolbox.tar", "B:/Cognita-Windows-Bundle/toolbox.tar", "/mnt/b/Cognita-Windows-Bundle/toolbox.tar"),
        ]
        mapping = "\n".join(f"    '{normalized}' = '{linux}'" for _, normalized, linux in paths)
        calls = "\n".join(
            f"$result=Convert-WindowsPathToWsl -Path '{windows}' -Operation 'convert {index}'; if($result -cne '{linux}'){{throw 'Unexpected converted output {index}'}}"
            for index, (windows, _, linux) in enumerate(paths)
        )
        test_script = f"""
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$script:Distro = 'Cognita-Windows'
$script:FailConversion = $false
$script:ExpectedPaths = @{{
{mapping}
}}
{installer[support_start:support_end]}
function Run([string]$Exe,[string[]]$CommandArgs,[int]$Timeout=1800) {{
    if($CommandArgs -notcontains 'wslpath') {{ return @() }}
    if($script:FailConversion) {{ throw 'mock failure SECRET=must-not-escape' }}
    $inputPath=[string]$CommandArgs[-1]
    if(-not $script:ExpectedPaths.ContainsKey($inputPath)) {{ throw "Unexpected wslpath input: $inputPath" }}
    return @($script:ExpectedPaths[$inputPath])
}}
{calls}
try {{ Read-RequiredLastLine -Lines @() -Operation 'controlled conversion'; throw 'Empty output was incorrectly accepted.' }}
catch {{ if($_.Exception.Message -cnotmatch '^controlled conversion returned no output\\.$'){{throw}} }}
try {{ Read-RequiredLastLine -Lines @('  ') -Operation 'blank conversion'; throw 'Whitespace output was incorrectly accepted.' }}
catch {{ if($_.Exception.Message -cnotmatch '^blank conversion returned empty output\\.$'){{throw}} }}
$script:FailConversion=$true
try {{ Convert-WindowsPathToWsl -Path 'B:\\private.tar' -Operation 'convert protected archive'; throw 'Command failure was incorrectly accepted.' }}
catch {{ if($_.Exception.Message -cnotmatch '^convert protected archive failed while running wslpath\\.$' -or $_.Exception.Message -match 'must-not-escape'){{throw}} }}
"""
        result = _run_powershell(test_script)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)

    def test_install_state_has_one_fixed_b_drive_authority_and_no_fallback(self):
        installer = (WINDOWS / "Install-CognitaWindows.ps1").read_text(encoding="utf-8")
        guide = (WINDOWS / "README-Windows.md").read_text(encoding="utf-8")
        self.assertIn("$script:Root = 'B:\\Cognita-Windows-State'", installer)
        self.assertIn("$script:RecordPath = Join-Path $script:Root 'install.json'", installer)
        self.assertNotIn("$ENV:LOCALAPPDATA", installer.upper())
        self.assertNotIn("AppData\\Local", installer)
        self.assertIn(r"B:\Cognita-Windows-State\install.json", guide)
        self.assertNotIn("%LOCALAPPDATA%\\Cognita-Windows\\install.json", guide)
        for name in ("Do-Install", "Do-Status", "Do-Start", "Do-Stop", "Do-Repair", "Do-Reset", "Do-Uninstall"):
            start = installer.index(f"function {name}")
            end = installer.find("\nfunction ", start + 1)
            body = installer[start:] if end < 0 else installer[start:end]
            self.assertTrue(
                "$script:RecordPath" in body or "Read-Record" in body or "Verify-Owner" in body,
                f"{name} must use the fixed record authority",
            )
        self.assertNotIn("LOCALAPPDATA", installer.upper())
        self.assertNotIn("migrate", installer.lower())
        self.assertNotIn("fallback", installer.lower())

    def test_state_directory_acl_is_traversable_but_record_and_backup_are_owner_admin_only(self):
        installer = (WINDOWS / "Install-CognitaWindows.ps1").read_text(encoding="utf-8")
        start = installer.index("function Secure-Path")
        end = installer.index("\nfunction Read-Record", start)
        definitions = installer[start:end]
        test_script = f"""
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
{definitions}
$script:OwnedPrefix = 'Cognita-Windows-Acl-Test-'
$script:CleanupParent = if (Test-Path -LiteralPath 'B:\' -PathType Container) {{
    [IO.Path]::GetFullPath('B:\')
}} else {{
    [IO.Path]::GetFullPath($env:TEMP)
}}
$script:Root = [IO.Path]::GetFullPath((Join-Path $script:CleanupParent ($script:OwnedPrefix + [guid]::NewGuid().ToString('N'))))
$script:RecordPath = Join-Path $script:Root 'install.json'
try {{
    $record = [ordered]@{{schema=1;installation_id='first'}}
    Save-Record $record
    $rootAcl = Get-Acl -LiteralPath $script:Root
    $usersSid = ([Security.Principal.NTAccount]'BUILTIN\\Users').Translate([Security.Principal.SecurityIdentifier]).Value
    $rootRules = $rootAcl.GetAccessRules($true, $true, [Security.Principal.NTAccount])
    $userRules = @($rootRules | Where-Object {{
        $_.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value -eq $usersSid
    }})
    if ($userRules.Count -ne 1 -or $userRules[0].AccessControlType -ne 'Allow' -or
        $userRules[0].FileSystemRights -ne ([Security.AccessControl.FileSystemRights]::ReadAndExecute -bor [Security.AccessControl.FileSystemRights]::Synchronize) -or
        $userRules[0].InheritanceFlags -ne [Security.AccessControl.InheritanceFlags]::None -or
        $userRules[0].PropagationFlags -ne [Security.AccessControl.PropagationFlags]::None) {{
        throw "State directory Users ACL mismatch: count=$($userRules.Count) rights=$($userRules[0].FileSystemRights) inheritance=$($userRules[0].InheritanceFlags) propagation=$($userRules[0].PropagationFlags) type=$($userRules[0].AccessControlType)."
    }}
    if (-not $rootAcl.AreAccessRulesProtected) {{ throw 'State directory ACL is not protected.' }}
    if ($rootAcl.Owner -cne [Security.Principal.WindowsIdentity]::GetCurrent().Name) {{ throw "Unexpected state directory owner: $($rootAcl.Owner)" }}
    $script:BinPath = Join-Path $script:Root 'bin'
    $script:OtherChildPath = Join-Path $script:Root 'config'
    $null = New-Item -ItemType Directory -Path $script:BinPath
    $null = New-Item -ItemType Directory -Path $script:OtherChildPath
    Secure-Path $script:BinPath -Directory
    Secure-Path $script:OtherChildPath -Directory
    foreach ($childPath in @($script:BinPath, $script:OtherChildPath)) {{
        $childAcl = Get-Acl -LiteralPath $childPath
        if (-not $childAcl.AreAccessRulesProtected) {{ throw "Child directory ACL is not protected: $childPath" }}
        if ($childAcl.Owner -cne [Security.Principal.WindowsIdentity]::GetCurrent().Name) {{ throw "Unexpected child directory owner: $childPath ($($childAcl.Owner))" }}
        $childRules = $childAcl.GetAccessRules($true, $true, [Security.Principal.NTAccount])
        $childUsers = @($childRules | Where-Object {{
            $_.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value -eq $usersSid
        }})
        if ($childUsers.Count -ne 0) {{ throw "BUILTIN\\Users traversal ACE leaked to child directory: $childPath" }}
        $childAccounts = @($childRules | Where-Object AccessControlType -eq Allow | ForEach-Object {{
            $_.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value
        }} | Sort-Object -Unique)
        $expectedDirectoryAccounts = @(
            ([Security.Principal.WindowsIdentity]::GetCurrent().User.Value),
            ([Security.Principal.NTAccount]'BUILTIN\\Administrators').Translate([Security.Principal.SecurityIdentifier]).Value
        ) | Sort-Object -Unique
        if (($childAccounts -join ',') -cne ($expectedDirectoryAccounts -join ',')) {{ throw "Unexpected child directory principals: $childPath ($childAccounts)" }}
        foreach ($rule in @($childRules | Where-Object AccessControlType -eq Allow)) {{
            if (($rule.FileSystemRights -band [Security.AccessControl.FileSystemRights]::FullControl) -ne
                [Security.AccessControl.FileSystemRights]::FullControl -or
                $rule.InheritanceFlags -ne ([Security.AccessControl.InheritanceFlags]::ContainerInherit -bor [Security.AccessControl.InheritanceFlags]::ObjectInherit)) {{
                throw "Child directory grant differs from owner/Admin FullControl inheritance: $childPath"
            }}
        }}
    }}
    function Assert-PrivateFile([string]$Path) {{
        $acl = Get-Acl -LiteralPath $Path
        if (-not $acl.AreAccessRulesProtected) {{ throw "File ACL is inheriting: $Path" }}
        if ($acl.Owner -cne [Security.Principal.WindowsIdentity]::GetCurrent().Name) {{ throw "Unexpected file owner: $Path ($($acl.Owner))" }}
        $rules = $acl.GetAccessRules($true, $true, [Security.Principal.NTAccount])
        $accounts = @($rules | Where-Object AccessControlType -eq Allow | ForEach-Object {{
            $_.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value
        }} | Sort-Object -Unique)
        $expected = @(
            ([Security.Principal.WindowsIdentity]::GetCurrent().User.Value),
            ([Security.Principal.NTAccount]'BUILTIN\\Administrators').Translate([Security.Principal.SecurityIdentifier]).Value
        ) | Sort-Object -Unique
        if (($accounts -join ',') -cne ($expected -join ',')) {{ throw "Unexpected file principals: $Path ($accounts)" }}
        foreach ($rule in @($rules | Where-Object AccessControlType -eq Allow)) {{
            if (($rule.FileSystemRights -band [Security.AccessControl.FileSystemRights]::FullControl) -ne
                [Security.AccessControl.FileSystemRights]::FullControl) {{ throw "File grant is not FullControl: $Path" }}
        }}
    }}
    Assert-PrivateFile $script:RecordPath
    $record.installation_id = 'second'
    Save-Record $record
    $previous = $script:RecordPath + '.previous'
    Assert-PrivateFile $script:RecordPath
    Assert-PrivateFile $previous
    if ((Get-Content -Raw -LiteralPath $previous | ConvertFrom-Json).installation_id -cne 'first') {{
        throw 'Atomic replacement did not retain the prior record as .previous.'
    }}
    if (@(Get-ChildItem -LiteralPath $script:Root -Filter '.install-*.tmp').Count -ne 0) {{
        throw 'Atomic record temporary file remains.'
    }}
}} finally {{
    $canonicalRoot = [IO.Path]::GetFullPath($script:Root).TrimEnd([IO.Path]::DirectorySeparatorChar)
    $canonicalParent = [IO.Directory]::GetParent($canonicalRoot).FullName.TrimEnd([IO.Path]::DirectorySeparatorChar)
    $expectedParent = [IO.Path]::GetFullPath($script:CleanupParent).TrimEnd([IO.Path]::DirectorySeparatorChar)
    $leaf = [IO.Path]::GetFileName($canonicalRoot)
    if (-not $canonicalParent.Equals($expectedParent, [StringComparison]::OrdinalIgnoreCase) -or
        $leaf -cnotmatch ('^' + [regex]::Escape($script:OwnedPrefix) + '[0-9a-f]{{32}}$')) {{
        throw "Refusing to remove unverified ACL test root: $canonicalRoot"
    }}
    if (Test-Path -LiteralPath $canonicalRoot) {{ Remove-Item -LiteralPath $canonicalRoot -Recurse -Force }}
    if (Test-Path -LiteralPath $script:Root) {{ throw "Task-owned ACL test directory remains after cleanup: $script:Root" }}
}}
"""
        result = _run_powershell(test_script)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)

    def test_optional_owner_marker_read_handles_empty_and_present_output(self):
        powershell = shutil.which("powershell.exe") or shutil.which("pwsh")
        if powershell is None:
            self.skipTest("PowerShell is required to exercise the Windows installer helper")

        installer = (WINDOWS / "Install-CognitaWindows.ps1").read_text(encoding="utf-8")
        function_start = installer.index("function Read-OptionalLastLine")
        function_end = installer.index("\n\nfunction Verify-Bundle", function_start)
        definition = installer[function_start:function_end]
        test_script = f"""
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
{definition}
$empty = Read-OptionalLastLine -Lines @()
if ($null -eq $empty -or $empty -cne '') {{
    throw 'Empty optional marker output was not normalized to an empty string.'
}}
$present = Read-OptionalLastLine -Lines @('ignored', '  marker-id  ')
if ($present -cne 'marker-id') {{
    throw "Present optional marker output was not trimmed: $present"
}}
"""
        encoded = base64.b64encode(test_script.encode("utf-16-le")).decode("ascii")
        result = subprocess.run(
            [powershell, "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        self.assertIn("$markerValue=Read-OptionalLastLine -Lines $marker", installer)
        self.assertIn("$ownerId=Read-OptionalLastLine -Lines $owner", installer)

    def test_distro_exists_normalizes_each_wsl_output_line_and_matches_exact_name(self):
        powershell = shutil.which("powershell.exe") or shutil.which("pwsh")
        if powershell is None:
            self.skipTest("PowerShell is required to exercise the Windows installer helper")

        installer = (WINDOWS / "Install-CognitaWindows.ps1").read_text(encoding="utf-8")
        function_start = installer.index("function DistroExists")
        function_end = installer.index("\n\nfunction Verify-Bundle", function_start)
        definition = installer[function_start:function_end]
        test_script = f"""
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
function Run([string]$Exe,[string[]]$CommandArguments,[int]$Timeout=1800) {{
    return @($script:RunOutput)
}}
{definition}
$nul = [char]0
$targetWithNuls = "C${{nul}}o${{nul}}g${{nul}}n${{nul}}i${{nul}}t${{nul}}a${{nul}}-${{nul}}W${{nul}}i${{nul}}n${{nul}}d${{nul}}o${{nul}}w${{nul}}s${{nul}}"
$cases = @(
    @{{ label = 'zero lines'; output = @(); expected = $false }},
    @{{ label = 'one line'; output = @("  $targetWithNuls  "); expected = $true }},
    @{{ label = 'multiple lines'; output = @("Ubuntu$nul", " $targetWithNuls ", 'Debian'); expected = $true }},
    @{{ label = 'similar name only'; output = @("Cognita-Windows-Preview$nul", 'Ubuntu'); expected = $false }}
)
foreach ($case in $cases) {{
    $script:RunOutput = $case.output
    $actual = DistroExists 'Cognita-Windows'
    if ($actual -ne $case.expected) {{
        throw "DistroExists failed $($case.label): expected=$($case.expected) actual=$actual"
    }}
}}
"""
        encoded = base64.b64encode(test_script.encode("utf-16-le")).decode("ascii")
        result = subprocess.run(
            [powershell, "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)

    def test_bundle_installer_owns_mutex_before_record_and_distro_creation(self):
        script = (WINDOWS / "Install-CognitaWindows.ps1").read_text(encoding="utf-8")
        self.assertIn("Local\\Cognita-Windows-Installer", script)
        install = script[script.index("function Do-Install"):script.index("function Do-Status")]
        self.assertLess(install.index("Save-Record $record"), install.index("Install-FirstRun $bundle $record"))
        self.assertIn("SHA256SUMS", script)
        self.assertIn("Get-FileHash -Algorithm SHA256", script)
        self.assertIn("image_cognita_cpu", script)
        self.assertIn("image_workspace_runtime", script)
        self.assertIn("org.opencontainers.image.revision", script)
        self.assertIn("Invoke-WslWithSecret (@('-d',$script:Distro,'-u','root','--exec')+$composeArgs) $payload", script)
        self.assertIn("Invoke-WslWithSecret $wslArgs $rawKey", script)
        self.assertIn("$process.StandardInput.WriteLineAsync($InputText)", script)
        self.assertIn("source_kind='ntfs'", script)
        self.assertIn("source_kind='smb'", script)
        self.assertIn("last_runtime_identity=$null", script)

    def test_projection_matches_source_mount_guard_schema_and_keeps_identity_facts_separate(self):
        helper = (WINDOWS / "Prepare-Sources.py").read_text(encoding="utf-8")
        guard = (REPO / "src" / "cognita" / "source_mount_guard.py").read_text(encoding="utf-8")
        self.assertIn("{'schema': 1, 'sources': observations}", helper)
        self.assertIn("'identity': identity", helper)
        self.assertIn("'observation': observation", helper)
        self.assertIn("row.get('last_runtime_identity')", helper)
        self.assertIn("row.get('canonical_identity')", helper)
        self.assertNotIn("expected_identity", helper)
        self.assertNotIn("startup_observation", helper)
        self.assertIn('{"schema", "sources"}', guard)
        self.assertIn('{"alias", "source_kind", "observation", "identity"}', guard)

    def test_helper_emits_exact_schema_one_row_and_owner_identity_is_not_runtime_identity(self):
        helper = _load_helper()
        runtime = {"device": 31, "inode": 47}
        row = helper.projection_row("archive", "smb", "available", runtime)
        self.assertEqual(row, {
            "alias": "archive", "source_kind": "smb", "observation": "available",
            "identity": runtime,
        })
        with owned_temp() as root:
            with mock.patch.object(helper, "mount_identity", return_value="filesystem:device"), \
                    mock.patch.object(helper, "run", return_value=SimpleNamespace(stdout="9p\n", returncode=0)):
                canonical = helper.canonical_identity("smb", r"\\fileserver\share\folder", root)
            self.assertTrue(canonical.startswith("smb://fileserver/share:"))
            self.assertNotEqual(canonical, runtime)

    def test_lifecycle_uses_hidden_long_lived_launcher_and_fixed_owner_identity(self):
        script = (WINDOWS / "Install-CognitaWindows.ps1").read_text(encoding="utf-8")
        launcher = (WINDOWS / "cognita-session.sh").read_text(encoding="utf-8")
        self.assertIn("wscript.exe", script)
        self.assertIn("//B", script)
        self.assertIn("sh.Run cmd, 0, True", script)
        self.assertIn("--exec /usr/local/libexec/cognita-session", script)
        self.assertIn("systemctl is-active --quiet", launcher)
        self.assertIn("cognita-install-id", script)
        self.assertIn("Local\\Cognita-Windows-Installer", script)
        self.assertIn("Reset-State", script)
        self.assertNotIn("-Action Reset-State", script)
        self.assertIn("Write-Error ($_.Exception.Message", script)
        self.assertIn("Source: ${source}:$line ($context)", script)
        self.assertIn("Assert-LauncherTask $task", script)
        self.assertIn("$actions[0].Arguments -ine $expectedArgs", script)
        self.assertIn("$taskSid.Value -ceq $currentSid.Value", script)
        self.assertIn("$principal.LogonType -ne 'Interactive'", script)

    def test_docker_prestart_orders_shared_source_preparation_before_compose(self):
        script = (WINDOWS / "Install-CognitaWindows.ps1").read_text(encoding="utf-8")
        helper = (WINDOWS / "Prepare-Sources.py").read_text(encoding="utf-8")
        self.assertIn("ExecStartPre=/usr/local/libexec/cognita-prepare-sources", script)
        self.assertIn("up --detach --no-build --pull never", script)
        self.assertNotIn("up --detach --wait", script)
        bootstrap = script[script.index("function Get-LinuxBootstrapScript"):script.index("function Initialize-ToolboxCache")]
        self.assertLess(bootstrap.index('atomic_copy "$bundle/scripts/windows/Prepare-Sources.py"'), bootstrap.index("systemctl unmask docker.service"))
        self.assertIn("mount', '--make-rshared'", helper)
        self.assertIn("rslave", (REPO / "compose.yaml").read_text(encoding="utf-8"))
        self.assertIn("source-identities.json", helper)
        self.assertIn("compose.windows-projection.yaml", script)
        self.assertIn("COGNITA_SOURCE_IDENTITIES_FILE: /run/cognita/source-identities.json", script)
        self.assertIn("read_only: true", script)

    def test_full_mode_prepares_workspace_capacity_marker_and_exact_kvm_gid(self):
        script = (WINDOWS / "Install-CognitaWindows.ps1").read_text(encoding="utf-8")
        self.assertIn(".cognita-12-workspaces.json", script)
        self.assertIn("\"role\":\"workspaces\"", script)
        self.assertIn("stat -c %g /dev/kvm", script)
        self.assertIn("test -r /dev/kvm -a -w /dev/kvm", script)

    def test_reset_preserves_workspace_toolbox_cache_and_capacity_marker(self):
        script = (WINDOWS / "Reset-CognitaState.sh").read_text(encoding="utf-8")
        self.assertIn("! -name toolbox-cache ! -name .cognita-12-workspaces.json", script)
        self.assertNotIn("$root/sources", script)
        self.assertNotIn("$root/config", script)

    def test_source_owner_marker_must_match(self):
        helper = _load_helper()
        with owned_temp() as root:
            pointer_path = root / "install-record-path"
            record_path = root / "install.json"
            marker_path = root / "owner-id"
            record_path.write_text(json.dumps({
                "installation_id": "5e0b0e9f-843f-46b6-9617-0135266ca620",
                "distro": "Cognita-Windows", "source_root": "/srv/cognita/sources", "sources": [],
            }), encoding="utf-8")
            live_pointer = "/mnt/b/Cognita-Windows-State/install.json"
            pointer_path.write_text(live_pointer + "\n", encoding="utf-8")
            marker_path.write_text("different\n", encoding="ascii")

            def path_factory(value):
                return record_path if value == live_pointer else Path(value)
            with mock.patch.object(helper, "RECORD_PATH", pointer_path), \
                    mock.patch.object(helper, "MARKER_PATH", marker_path), \
                    mock.patch.object(helper, "Path", path_factory):
                with self.assertRaisesRegex(RuntimeError, "does not match"):
                    helper.read_owned_record()

    def test_source_owner_record_reads_absolute_path_from_pointer_file(self):
        helper = _load_helper()
        with owned_temp() as root:
            pointer_path = root / "cognita-install-record-path"
            record_path = root / "windows" / "install.json"
            record_path.parent.mkdir()
            live_pointer = "/mnt/b/Cognita-Windows-State/install.json"
            expected = {
                "installation_id": "5e0b0e9f-843f-46b6-9617-0135266ca620",
                "distro": "Cognita-Windows", "source_root": str(helper.ROOT), "sources": [],
            }
            record_path.write_text(json.dumps(expected), encoding="utf-8")
            pointer_path.write_text(live_pointer + "\n", encoding="utf-8")
            marker_path = root / "owner-id"
            marker_path.write_text(expected["installation_id"] + "\n", encoding="ascii")

            def path_factory(value):
                return record_path if value == live_pointer else Path(value)
            with mock.patch.object(helper, "RECORD_PATH", pointer_path), \
                    mock.patch.object(helper, "MARKER_PATH", marker_path), \
                    mock.patch.object(helper, "Path", path_factory):
                self.assertEqual(helper.read_owned_record(), expected)

    def test_source_owner_record_rejects_bad_pointer_and_target_without_disclosing_path(self):
        helper = _load_helper()
        cases = (
            ("", "pointer is empty or malformed"),
            ("relative/install.json", "pointer must name an absolute path"),
            ("/missing/private/install.json", "target is missing or not a file"),
            ("/private/path/with\nnewline", "pointer is empty or malformed"),
        )
        with owned_temp() as root:
            pointer_path = root / "cognita-install-record-path"
            marker_path = root / "owner-id"
            marker_path.write_text("owner-id\n", encoding="ascii")
            for pointer, message in cases:
                with self.subTest(pointer=pointer):
                    pointer_path.write_text(pointer, encoding="utf-8")
                    with mock.patch.object(helper, "RECORD_PATH", pointer_path), mock.patch.object(helper, "MARKER_PATH", marker_path):
                        with self.assertRaisesRegex(RuntimeError, message) as raised:
                            helper.read_owned_record()
                    if pointer:
                        self.assertNotIn(pointer, str(raised.exception))

    def test_source_owner_record_rejects_nonfile_unreadable_and_invalid_json_targets(self):
        helper = _load_helper()
        with owned_temp() as root:
            pointer_path = root / "cognita-install-record-path"
            marker_path = root / "owner-id"
            marker_path.write_text("owner-id\n", encoding="ascii")
            directory = root / "record-directory"
            directory.mkdir()
            missing = root / "missing.json"
            missing.write_text("valid file, simulated unreadable", encoding="utf-8")
            invalid_json = root / "invalid.json"
            invalid_json.write_text("{ definitely not json", encoding="utf-8")
            invalid_encoding = root / "invalid-encoding.json"
            invalid_encoding.write_bytes(b"\xff\xfe\x00")
            mappings = {
                "/tmp/cognita-record-directory": directory,
                "/mnt/c/private/install.json": invalid_json,
                "/mnt/c/private/invalid-encoding.json": invalid_encoding,
                "/mnt/c/private/missing.json": missing,
            }
            def path_factory(value):
                return mappings.get(value, Path(value))
            with mock.patch.object(helper, "RECORD_PATH", pointer_path), \
                    mock.patch.object(helper, "MARKER_PATH", marker_path), \
                    mock.patch.object(helper, "Path", path_factory):
                pointer_path.write_text("/tmp/cognita-record-directory", encoding="utf-8")
                with self.assertRaisesRegex(RuntimeError, "target is missing or not a file"):
                    helper.read_owned_record()
                pointer_path.write_text("/mnt/c/private/install.json", encoding="utf-8")
                with self.assertRaisesRegex(RuntimeError, "contains invalid JSON"):
                    helper.read_owned_record()
                pointer_path.write_text("/mnt/c/private/invalid-encoding.json", encoding="utf-8")
                with self.assertRaisesRegex(RuntimeError, "contains invalid JSON"):
                    helper.read_owned_record()
                pointer_path.write_text("/mnt/c/private/missing.json", encoding="utf-8")
                original_read_text = Path.read_text
                def fail_target_read(path, *args, **kwargs):
                    if path == missing:
                        raise PermissionError
                    return original_read_text(path, *args, **kwargs)
                with mock.patch.object(Path, "read_text", fail_target_read):
                    with self.assertRaisesRegex(RuntimeError, "target is unreadable"):
                        helper.read_owned_record()

    def test_old_path_as_json_contract_is_rejected(self):
        helper = _load_helper()
        with owned_temp() as root:
            pointer_path = root / "cognita-install-record-path"
            marker_path = root / "owner-id"
            marker_path.write_text("owner-id\n", encoding="ascii")
            # The live pointer contains a path, never the JSON document itself.
            pointer_path.write_text("/mnt/b/Cognita-Windows-State/install.json", encoding="utf-8")
            with mock.patch.object(helper, "RECORD_PATH", pointer_path), mock.patch.object(helper, "MARKER_PATH", marker_path):
                with self.assertRaisesRegex(RuntimeError, "target is missing or not a file"):
                    helper.read_owned_record()

    def test_source_owner_record_update_replaces_json_target_and_preserves_pointer(self):
        helper = _load_helper()
        with owned_temp() as root:
            pointer_path = root / "cognita-install-record-path"
            record_path = root / "install.json"
            live_pointer = "/mnt/b/Cognita-Windows-State/install.json"
            pointer_path.write_text(live_pointer + "\n", encoding="utf-8")
            record_path.write_text('{"old":true}\n', encoding="utf-8")

            def path_factory(value):
                return record_path if value == live_pointer else Path(value)
            updated = {"installation_id": "owner-id", "updated": True}
            with mock.patch.object(helper, "RECORD_PATH", pointer_path), \
                    mock.patch.object(helper, "Path", path_factory):
                helper.write_owned_record(updated)
            self.assertEqual(pointer_path.read_text(encoding="utf-8"), live_pointer + "\n")
            self.assertEqual(json.loads(record_path.read_text(encoding="utf-8")), updated)
            self.assertFalse(record_path.with_name(record_path.name + ".tmp").exists())

    def test_known_absent_source_projects_last_runtime_facts_unavailable(self):
        helper = _load_helper()
        with owned_temp() as root:
            source_path = root / "not-mounted"
            result_type = type("Result", (), {"returncode": 0, "stdout": str(source_path)})
            with mock.patch.object(helper, "run", lambda *args, **kwargs: result_type()):
                result = helper.prepare_source({
                    "alias": "manuals", "source_kind": "ntfs", "locator": r"D:\\documents",
                    "canonical_identity": "expected-filesystem-id",
                    "last_runtime_identity": {"device": 4, "inode": 9},
                }, root / "sources" / "manuals")
            self.assertEqual(result, {
                "alias": "manuals", "source_kind": "ntfs",
                "observation": "unavailable", "identity": {"device": 4, "inode": 9},
            })

    def test_initial_absent_source_without_canonical_identity_fails(self):
        helper = _load_helper()
        with owned_temp() as root:
            source_path = root / "not-mounted"
            result_type = type("Result", (), {"returncode": 0, "stdout": str(source_path)})
            with mock.patch.object(helper, "run", lambda *args, **kwargs: result_type()):
                with self.assertRaisesRegex(RuntimeError, "initial identity capture failed"):
                    helper.prepare_source({
                        "alias": "manuals", "source_kind": "ntfs", "locator": r"D:\\documents",
                        "canonical_identity": None, "last_runtime_identity": None,
                    }, root / "sources" / "manuals")

    def test_reserved_selftest_root_is_created_without_replacement(self):
        helper = _load_helper()
        with owned_temp() as temporary:
            root = temporary / "cognita-self-test"
            root.mkdir()
            fixture = root / "synthetic.txt"
            fixture.write_text("only the reserved fixture", encoding="utf-8")
            before = (root.stat().st_dev, root.stat().st_ino)
            with mock.patch.object(helper, "mount_identity", lambda _path: "device:inode"), \
                    mock.patch.object(
                        helper, "pwd",
                        SimpleNamespace(getpwnam=mock.Mock(
                            return_value=SimpleNamespace(pw_uid=1234, pw_gid=5678),
                        )),
                    ), \
                    mock.patch.object(helper.os, "chown", create=True) as chown:
                result = helper.prepare_source({
                    "alias": "cognita-self-test", "source_kind": "installation_ext4",
                    "locator": "/srv/cognita/sources/cognita-self-test", "canonical_identity": None,
                    "last_runtime_identity": None,
                }, root)
            chown.assert_called_once_with(root, 1234, 5678)
            after = (root.stat().st_dev, root.stat().st_ino)
            self.assertEqual(before, after)
            self.assertEqual(fixture.read_text(encoding="utf-8"), "only the reserved fixture")
            self.assertEqual(result["observation"], "available")
            self.assertEqual(result["source_kind"], "installation_ext4")
            self.assertEqual(result["identity"], {"device": root.stat().st_dev, "inode": root.stat().st_ino})

    def test_admin_credential_contract_is_argon2_stdin_for_both_modes(self):
        script = (WINDOWS / "Install-CognitaWindows.ps1").read_text(encoding="utf-8")
        self.assertIn("[PSCredential] $AdminCredential", script)
        self.assertIn("function Set-AdminCredentialsIfMissing", script)
        self.assertIn("--stdin-json", script)
        self.assertIn("admin_password_hash", script)
        self.assertIn("argon2", script)
        self.assertNotIn("SHA256]::HashData", script)
        self.assertNotIn("admin_password_sha256:", script)
        install = script[script.index("function Do-Install"):script.index("function Do-Status")]
        self.assertLess(install.index("Set-AdminCredentialsIfMissing"), install.index("Do-Start"))

    def test_repair_recreates_changed_projection_before_restoring_task_keepalive(self):
        script = (WINDOWS / "Install-CognitaWindows.ps1").read_text(encoding="utf-8")
        start = script[script.index("function Do-Start"):script.index("function Do-Stop")]
        self.assertLess(start.index("--force-recreate"), start.index("Start-ScheduledTask"))
        self.assertIn("$taskState -ne 'Running'", start)
        self.assertIn("projection_changed=true", script)

    def test_uninstall_keeps_owner_record_and_reset_is_separate(self):
        script = (WINDOWS / "Install-CognitaWindows.ps1").read_text(encoding="utf-8")
        uninstall = script[script.index("function Do-Uninstall"):script.index("$mutex=")]
        self.assertIn("Unregister-ScheduledTask", uninstall)
        self.assertIn("--unregister", uninstall)
        self.assertIn("Remove-Item -LiteralPath $vbs", uninstall)
        self.assertNotIn("Remove-Item -LiteralPath $script:RecordPath", uninstall)
        self.assertIn("function Do-Reset", script)


if __name__ == "__main__":
    unittest.main()
