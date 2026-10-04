"""Execute installer lifecycle paths against an owned stateful external boundary."""
from __future__ import annotations

import base64
from contextlib import contextmanager
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock


REPO = Path(__file__).resolve().parents[1]
WINDOWS = REPO / "scripts" / "windows"


@contextmanager
def owned_temp():
    with tempfile.TemporaryDirectory(prefix="cognita-windows-recovery-test-") as value:
        root = Path(value)
        yield root
    if root.exists():
        raise AssertionError(f"owned temporary fixture remains: {root}")


def helper_module():
    spec = importlib.util.spec_from_file_location("cognita_recovery_sources", WINDOWS / "Prepare-Sources.py")
    assert spec and spec.loader
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    return helper


def run_ps(script: str, root: Path):
    powershell = shutil.which("pwsh") or shutil.which("powershell.exe")
    if not powershell:
        raise unittest.SkipTest("PowerShell is required for executable installer lifecycle checks")
    path = root / "lifecycle.ps1"
    path.write_text(script, encoding="utf-8-sig")
    # The model never starts a child process. The sole owned PowerShell process
    # is always reaped before the fixture can be removed, including timeouts.
    process = subprocess.Popen(
        [powershell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(path)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        output, errors = process.communicate(timeout=30)
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=10)
    return process.returncode, output, errors


MODEL = r"""
Set-StrictMode -Version Latest
$ErrorActionPreference='Stop'
$Mode='Core';$Source=@();$SmbSource=@();$BundlePath=$null
$script:Distro='Cognita-Windows';$script:Project='cognita-windows';$script:Unit='cognita-compose.service';$script:Task='Cognita-Windows'
$script:McpPort=10675;$script:AdminPort=10676;$script:CurrentMode='core';$script:ProjectionChanged=$false
$script:Events=[Collections.Generic.List[string]]::new()
$script:State=[pscustomobject]@{absent_units=@();stop_failure='';exists=$true;marker='11111111-1111-1111-1111-111111111111';complete=$true;sources_ready=$true;hook_current=$true;root_shared=$true;docker_active=$true;task='Running';unit=$true;images=$true;configured=$true;projection_change=$false;source_mismatch=$false;alias_available=$true;bootstrap_failure=$false;toolbox=$false;enabled=$true;recreated=$false}
__DEFINITIONS__
$script:Root='__ROOT__'
$script:RecordPath=Join-Path $script:Root 'install.json'
$script:Bundle=[pscustomobject]@{Root=$script:Root;Sum='sum';Metadata=@{version='13.5.0';commit=('b'*40);bundle_mode='core';image_ref_cognita_cpu='cognita:cpu';image_cognita_cpu=('sha256:'+('c'*64));image_ref_postgres=('example/pgvector:pg18@sha256:'+('a'*64));image_postgres=('sha256:'+('d'*64));image_ref_workspace_runtime='cognita:workspace';image_workspace_runtime=('sha256:'+('e'*64));toolbox_version='0.1.2'}}
$record=[pscustomobject]@{schema=1;installation_id=$script:State.marker;distro=$script:Distro;compose_project=$script:Project;unit=$script:Unit;task=$script:Task;source_root='/srv/cognita/sources';config_root='/srv/cognita/config';postgres_root='/srv/cognita/postgres';model_cache_root='/srv/cognita/models';transfer_root='/srv/cognita/transfers';workspace_root='/srv/cognita/workspaces';mcp_port=10675;admin_port=10676;bundle_path=$script:Root;bundle_sha256='sum';version='13.5.0';commit=('b'*40);mode='core';image_ref_cognita_cpu='cognita:cpu';image_cognita_cpu=('sha256:'+('c'*64));image_ref_postgres=$script:Bundle.Metadata.image_ref_postgres;image_postgres=$script:Bundle.Metadata.image_postgres;image_ref_workspace_runtime='cognita:workspace';image_workspace_runtime=$script:Bundle.Metadata.image_workspace_runtime;sources=@([pscustomobject]@{alias='manuals';source_kind='ntfs';locator='X:synthetic';canonical_identity='external-volume';last_runtime_identity=@{device=20;inode=30}},[pscustomobject]@{alias='cognita-self-test';source_kind='installation_ext4';locator='/srv/cognita/sources/cognita-self-test';canonical_identity='old-ext4';last_runtime_identity=@{device=40;inode=50}});created_utc='old';updated_utc='old'}
$script:Record=$record
function Secure-Path {param($Path,[switch]$Directory,[switch]$StateRootTraversal)}
Save-Record $record
function Verify-Bundle($Path){$script:Events.Add('verify-bundle');return $script:Bundle}
function Get-Sources {return @($script:Record.sources)}
function Run([string]$Exe,[string[]]$CommandArguments,[int]$Timeout=1800){
    if($Exe -cne 'wsl.exe'){throw 'Only modeled WSL process requests are permitted.'}
    if($CommandArguments[0] -eq '--list'){if($script:State.exists){return @('Cognita-Windows')}else{return @()}}
    if($CommandArguments[0] -eq '--unregister'){$script:Events.Add('unregister');$script:State.exists=$false;$script:State.marker='';$script:State.complete=$false;return @()}
    if($CommandArguments[4] -ne '--exec'){throw 'Production Wsl forwarding dropped an argument.'}
    $cmd=@($CommandArguments[5..($CommandArguments.Count-1)])
    if($cmd[0] -eq 'wslpath'){return @('/accepted/'+[IO.Path]::GetFileName($cmd[-1]))}
    if($cmd[0] -eq 'bash'){
        if($cmd -contains 'cognita-stop-owned-units'){
            $index=[Array]::IndexOf($cmd,'cognita-stop-owned-units')
            foreach($unitName in $cmd[($index+1)..($cmd.Count-1)]){
                if($script:State.stop_failure -ceq $unitName){throw 'real owned-unit stop failure'}
                if($unitName -cin $script:State.absent_units){"absent $unitName";continue}
                if($unitName -ceq $script:Unit){$script:Events.Add('stop-unit');$script:State.task='Ready';$script:State.unit=$false}
                else {$script:Events.Add('stop-docker');$script:State.docker_active=$false}
                "stopped $unitName"
            }
            return
        }
        $text=$cmd[-1]
        if($text -match 'if test -f /etc/cognita-install-id'){return @($script:State.marker)}
        if($text -match 'complete=false'){
            return @((@{complete=$script:State.complete;sources_ready=$script:State.sources_ready;hook_current=$script:State.hook_current;root_shared=$script:State.root_shared;docker_active=$script:State.docker_active}|ConvertTo-Json -Compress))
        }
        if($text -match 'helper=/usr/local/libexec/cognita-prepare-sources'){$script:Events.Add('refresh-hook');$script:State.hook_current=$true;return @()}
        if($text -match 'grep -q'){if($script:State.configured){return @('configured')}else{return @('incomplete')}}
        if($text -match 'rm -f /etc/systemd'){$script:Events.Add('remove-hooks');$script:State.complete=$false;$script:State.docker_active=$false;return @()}
        throw 'Unexpected modeled bash request.'
    }
    if($cmd[0] -eq 'cat'){return @($script:State.marker)}
    if($cmd[0] -eq 'python3' -and [IO.Path]::GetFileName($cmd[1]) -ceq 'Update-Release.py'){
        if($cmd[2] -ceq 'guard'){
            # These are observed source-boundary facts. A pending identity or
            # projection change must still reach the original source owner.
            $verified=$script:State.complete -and -not $script:State.source_mismatch -and -not $script:State.projection_change
            return @((@{manual=$false;verified=$verified}|ConvertTo-Json -Compress))
        }
        if($cmd[2] -ceq 'ports'){$script:Events.Add('verify-ports');return @()}
        throw 'Unexpected maintenance helper boundary.'
    }
    if($cmd[0] -eq 'python3' -and $cmd[1] -ne '-c'){
        $script:Events.Add('prepare-sources')
        if($script:State.source_mismatch){$script:State.alias_available=$false;throw 'configured source identity changed for alias manuals; the alias is unavailable. Restore the recorded source or explicitly reconfigure that source before Repair.'}
        $script:State.root_shared=$true
        $r=Read-Record
        $fixture=$r.sources|Where-Object alias -eq 'cognita-self-test'
        if(-not $fixture.canonical_identity){$fixture.canonical_identity='new-ext4';$fixture.last_runtime_identity=@{device=60;inode=70};Save-Record $r}
        return @('cognita source preparation complete: 2 aliases projection_changed='+$script:State.projection_change.ToString().ToLowerInvariant())
    }
    if($cmd[0] -eq 'systemctl'){
        if($cmd[1] -eq 'stop'){$script:Events.Add('stop-docker');$script:State.docker_active=$false;return @()}
        if($cmd[1] -eq 'start'){
            if(-not $script:State.hook_current -or -not $script:State.root_shared){throw 'Docker started without the verified source hook/shared root.'}
            $script:Events.Add('start-docker');$script:State.docker_active=$true;return @()
        }
        if($cmd[1] -eq 'enable'){$script:Events.Add('enable-unit');$script:State.enabled=$true;return @()}
        if($cmd[1] -eq 'disable'){$script:Events.Add('disable-unit');$script:State.enabled=$false;return @()}
    }
    if($cmd[0] -eq 'docker'){
        if($cmd[1] -eq 'load'){$script:Events.Add('load-image');$script:State.images=$true;return @()}
        if($cmd[1] -eq 'inspect'){return @($script:Record.image_cognita_cpu)}
        if($cmd[1] -eq 'compose'){$script:Events.Add('validate-compose');return @()}
        if($cmd[1] -eq 'image'){
            if(-not $script:State.images){throw 'image absent'}
            $ref=$cmd[-1]
            if($ref -eq $script:Record.image_ref_cognita_cpu){return @($script:Record.image_cognita_cpu+' '+$script:Record.version+' '+$script:Record.commit)}
            if($ref -eq $script:Record.image_ref_workspace_runtime){return @($script:Record.image_workspace_runtime+' '+$script:Record.version+' '+$script:Record.commit)}
            if($ref -like 'cognita-workspace-toolbox:*'){return @('sha256:'+('f'*64))}
            if($cmd -contains '{{json .RepoDigests}}'){return @('["example/pgvector@sha256:'+('a'*64)+'"]')}
            return @($script:Record.image_postgres)
        }
    }
    throw 'Unexpected modeled external process request.'
}
function Get-ScheduledTask {param($TaskName,$ErrorAction);if($script:State.task -ne 'missing'){return [pscustomobject]@{State=$script:State.task}}}
function Assert-LauncherTask($task){$script:Events.Add('verify-task')}
function Install-Launcher($r){$script:Events.Add('install-launcher');$script:State.task='Ready'}
function Install-FirstRun($b,$r){
    $script:Events.Add('bootstrap')
    if(-not $script:State.exists -or -not $script:State.marker){
        $script:State.exists=$true;$script:State.marker=$r.installation_id;$script:State.recreated=$true
        Reset-RecreatedSyntheticIdentity $r
        $script:Events.Add('recapture-synthetic')
    }
    if($script:State.bootstrap_failure){throw 'injected bootstrap failure'}
    $script:State.complete=$true;$script:State.sources_ready=$true;$script:State.hook_current=$true;$script:State.root_shared=$true
    return $script:State.recreated
}
function Initialize-InstallationConfiguration($r){$script:Events.Add('initialize-config')}
function Initialize-ToolboxCache($r,$b){$script:Events.Add('initialize-toolbox');$script:State.toolbox=$true}
function Set-AdminCredentialsIfMissing {$script:Events.Add('configure-admin');$script:State.configured=$true}
function Verify-AcceptedArchiveChecksum($path,$name,$archive){return 'accepted'}
function Invoke-Compose([string[]]$CommandArguments,[int]$Timeout=1800){
    if($CommandArguments[0] -eq 'ps'){return @('container-id')}
    if($CommandArguments[0] -eq 'down'){$script:Events.Add('compose-down');return @()}
    throw 'Unexpected modeled Compose request.'
}
function Do-Start {
    if($script:Record.mode -eq 'full' -and -not $script:State.toolbox){throw 'Full broker started before Toolbox initialization.'}
    if($script:ProjectionChanged){$script:Events.Add('recreate-cognita');$script:ProjectionChanged=$false}
    $script:Events.Add('start-task');$script:State.unit=$true;$script:State.task='Running'
}
function Invoke-RestMethod {param($TimeoutSec,$Uri);if(-not $script:State.unit){throw 'not running'};return [pscustomobject]@{status='ok';version=$script:Record.version;workspace=[pscustomobject]@{mode=$script:Record.mode}}}
function Invoke-SelfTest($r){$script:Events.Add('selftest')}
function Read-Host($prompt){if($prompt -match 'UNINSTALL'){return 'UNINSTALL Cognita-Windows'};return 'UNREGISTER Cognita-Windows'}
function Unregister-ScheduledTask {param($TaskName,$Confirm,$ErrorAction);$script:Events.Add('remove-task');$script:State.task='missing'}
function Assert-Event([string]$event,[bool]$expected){if($script:Events.Contains($event) -ne $expected){throw "Unexpected event $event; events=$($script:Events -join ',')"}}
function Assert-Before([string]$before,[string]$after){if($script:Events.IndexOf($before) -lt 0 -or $script:Events.IndexOf($before) -ge $script:Events.IndexOf($after)){throw "Ordering failed $before before $after; events=$($script:Events -join ',')"}}
$script:Events.Clear()
__CASE__
"""


class RecoveryLifecycleTests(unittest.TestCase):
    def assert_case(self, case: str):
        source = (WINDOWS / "Install-CognitaWindows.ps1").read_text(encoding="utf-8")
        definitions = source[source.index("function Say("):source.index("\n$mutex=")]
        with owned_temp() as root:
            (root / "scripts" / "windows").mkdir(parents=True)
            shutil.copyfile(WINDOWS / "Prepare-Sources.py", root / "scripts" / "windows" / "Prepare-Sources.py")
            (root / "compose.yaml").write_text("services:\n  postgres:\n    image: example/pgvector:pg18@sha256:" + "a" * 64 + "\n", encoding="utf-8")
            script = MODEL.replace("__DEFINITIONS__", definitions).replace("__ROOT__", str(root).replace("'", "''")).replace("__CASE__", case)
            code, output, errors = run_ps(script, root)
            self.assertEqual(code, 0, output + errors)

    def test_unchanged_repair_and_same_bundle_install_leave_healthy_stack_untouched(self):
        self.assert_case(r"""
Do-Repair
foreach($event in @('bootstrap','stop-unit','stop-docker','start-docker','load-image','recreate-cognita','selftest','configure-admin','initialize-config','start-task')){Assert-Event $event $false}
Assert-Event 'prepare-sources' $false
Assert-Event 'verify-ports' $true
$script:Events.Clear()
Do-Install
foreach($event in @('bootstrap','stop-unit','load-image','recreate-cognita','selftest')){Assert-Event $event $false}
if($script:State.task -ne 'Running' -or -not $script:State.unit){throw 'Healthy owner session changed.'}
""")

    def test_changed_projection_recreates_only_app_after_verification(self):
        self.assert_case(r"""
$script:State.projection_change=$true
Do-Repair
Assert-Before 'prepare-sources' 'stop-unit'
Assert-Before 'stop-unit' 'recreate-cognita'
foreach($event in @('bootstrap','stop-docker','start-docker','load-image','selftest')){Assert-Event $event $false}
if($script:State.task -ne 'Running'){throw 'Repaired stack lacks its owner session.'}
""")

    def test_hook_and_root_repairs_refresh_only_source_startup_files(self):
        self.assert_case(r"""
$script:State.hook_current=$false;$script:State.root_shared=$false
Do-Repair
Assert-Before 'prepare-sources' 'stop-unit'
Assert-Before 'stop-unit' 'stop-docker'
Assert-Before 'stop-docker' 'refresh-hook'
Assert-Before 'refresh-hook' 'start-docker'
Assert-Before 'start-docker' 'recreate-cognita'
foreach($event in @('bootstrap','load-image','selftest','initialize-config')){Assert-Event $event $false}
""")

    def test_substitution_fails_before_service_stop_for_complete_and_partial_repair(self):
        self.assert_case(r"""
foreach($complete in @($true,$false)){
    $script:State.complete=$complete;$script:State.source_mismatch=$true;$script:Events.Clear()
    $failed=$false
    try{Do-Repair}catch{$failed=$_.Exception.Message -match 'alias is unavailable'}
    if(-not $failed -or $script:State.alias_available){throw 'Substituted source was admitted.'}
    foreach($event in @('stop-unit','stop-docker','bootstrap','load-image','recreate-cognita')){Assert-Event $event $false}
    if(-not $script:State.unit -or $script:State.task -ne 'Running'){throw 'Source failure stopped the healthy stack.'}
}
""")

    def test_partial_install_completion_and_failure_preserve_owner_record(self):
        self.assert_case(r"""
$script:State.complete=$false;$script:State.images=$false;$script:State.configured=$false
Do-Repair
Assert-Before 'prepare-sources' 'stop-unit'
Assert-Before 'bootstrap' 'load-image'
Assert-Before 'load-image' 'initialize-config'
Assert-Before 'initialize-config' 'configure-admin'
Assert-Before 'configure-admin' 'start-task'
Assert-Event 'selftest' $true
$saved=Read-Record
if($saved.installation_id -cne $script:Record.installation_id -or $saved.sources[0].canonical_identity -cne 'external-volume'){throw 'Partial repair replaced external ownership.'}
$script:State.complete=$false;$script:State.bootstrap_failure=$true;$script:Events.Clear()
$failed=$false;try{Do-Repair}catch{$failed=$_.Exception.Message -eq 'injected bootstrap failure'}
if(-not $failed){throw 'Injected bootstrap failure was hidden.'}
Assert-Event 'load-image' $false
if(-not(Test-Path -LiteralPath $script:RecordPath)){throw 'Failed completion removed the owner record.'}
""")

    def test_partial_marker_without_app_or_docker_units_resumes_through_real_stop(self):
        self.assert_case(r"""
$script:State.complete=$false;$script:State.sources_ready=$false;$script:State.images=$false;$script:State.configured=$false
$script:State.task='missing';$script:State.unit=$false;$script:State.docker_active=$false
$script:State.absent_units=@('cognita-compose.service','docker.service','docker.socket')
Do-Install
Assert-Event 'bootstrap' $true
Assert-Event 'load-image' $true
Assert-Event 'selftest' $true
if($script:State.task -cne 'Running'){throw 'Missing-unit partial installation did not resume'}
""")

    def test_real_stop_failure_for_each_owned_unit_prevents_partial_bootstrap(self):
        self.assert_case(r"""
foreach($unitToFail in @('cognita-compose.service','docker.service','docker.socket')){
    $script:Events.Clear();$script:State.complete=$false;$script:State.stop_failure=$unitToFail
    $failed=$false;try{Do-Install}catch{$failed=$_.Exception.Message -ceq 'real owned-unit stop failure'}
    if(-not $failed){throw "Stop failure was hidden: $unitToFail"}
    Assert-Event 'bootstrap' $false
    Assert-Event 'load-image' $false
}
""")

    def test_missing_distro_repair_recaptures_only_synthetic_identity_before_prepare(self):
        self.assert_case(r"""
$script:State.exists=$false;$script:State.complete=$false;$script:State.sources_ready=$false;$script:State.images=$false;$script:State.configured=$false
Do-Repair
Assert-Before 'recapture-synthetic' 'prepare-sources'
$r=Read-Record
if($r.sources[1].canonical_identity -cne 'new-ext4' -or $r.sources[0].canonical_identity -cne 'external-volume' -or $r.sources[0].last_runtime_identity.device -ne 20){throw 'Reconstruction changed an external identity or retained old fixture identity.'}
""")

    def test_recreated_distro_external_mismatch_never_loads_or_starts_images(self):
        self.assert_case(r"""
$script:State.exists=$false;$script:State.complete=$false;$script:State.sources_ready=$false;$script:State.source_mismatch=$true
$failed=$false;try{Do-Repair}catch{$failed=$_.Exception.Message -match 'identity changed'}
if(-not $failed){throw 'Recreation bypassed the external identity check.'}
Assert-Before 'recapture-synthetic' 'prepare-sources'
foreach($event in @('load-image','start-task','configure-admin')){Assert-Event $event $false}
if((Read-Record).sources[0].canonical_identity -cne 'external-volume'){throw 'External identity was erased.'}
""")

    def test_uninstall_unregister_reinstall_preserves_external_and_recaptures_fixture(self):
        self.assert_case(r"""
$ConfirmUninstall=$true;$ConfirmUnregisterDistro=$true
Do-Uninstall
Assert-Before 'stop-unit' 'compose-down'
Assert-Before 'remove-task' 'unregister'
if($script:State.exists -or -not(Test-Path -LiteralPath $script:RecordPath)){throw 'Uninstall ownership boundary failed.'}
$script:State.images=$false;$script:State.configured=$false;$script:Events.Clear()
Do-Install
Assert-Before 'recapture-synthetic' 'prepare-sources'
$r=Read-Record
if($r.sources[1].canonical_identity -cne 'new-ext4' -or $r.sources[0].canonical_identity -cne 'external-volume'){throw 'Reinstall retained old ext4 identity or erased external identity.'}
""")

    def test_full_completion_initializes_toolbox_before_service_start(self):
        self.assert_case(r"""
$script:Record.mode='full';$script:Bundle.Metadata.bundle_mode='full';Save-Record $script:Record
$Mode='Full'
$script:State.complete=$false;$script:State.images=$false;$script:State.configured=$false
Do-Repair
Assert-Before 'load-image' 'initialize-toolbox'
Assert-Before 'initialize-toolbox' 'configure-admin'
Assert-Before 'initialize-toolbox' 'start-task'
""")


class SourceRecoveryTests(unittest.TestCase):
    def test_volume_guid_serial_survive_wsl_device_changes_and_reject_substitution(self):
        helper = helper_module()
        volume = r"\\?\Volume{11111111-1111-1111-1111-111111111111}\:1234abcd"
        with owned_temp() as root:
            upstream = root / "upstream"
            upstream.mkdir()
            target = root / "alias"
            target.mkdir()
            row = {"alias": "alias", "source_kind": "ntfs", "locator": r"X:\synthetic",
                   "canonical_identity": "ntfs:" + volume.lower(), "last_runtime_identity": {"device": 10, "inode": 20}}
            observations = []
            def run(*args, **kwargs):
                if args[0] == "wslpath":
                    self.assertEqual(args[-1], "X:/synthetic")
                    return SimpleNamespace(returncode=0, stdout=str(upstream))
                encoded = args[-1]
                script = base64.b64decode(encoded).decode("utf-16-le")
                self.assertIn("GetVolumeNameForVolumeMountPoint", script)
                self.assertIn("GetVolumeInformation", script)
                return SimpleNamespace(returncode=0, stdout=volume)
            with mock.patch.object(helper, "run", side_effect=run), mock.patch.object(helper, "bind_source"), mock.patch.object(helper, "unbind_source") as detach:
                for device in (100, 200):
                    with mock.patch.object(helper, "runtime_identity", return_value={"device": device, "inode": 20}):
                        observations.append(helper.prepare_source(row, target))
                self.assertEqual([item["identity"]["device"] for item in observations], [100, 200])
                self.assertEqual(row["canonical_identity"], "ntfs:" + volume.lower())
                volume = volume.replace("1234abcd", "8765abcd")
                with self.assertRaisesRegex(helper.SourceIdentityMismatch, "identity changed.*alias"):
                    helper.prepare_source(row, target)
                detach.assert_called_once_with(target)

    def test_native_windows_query_observes_only_owned_fixture_volume(self):
        if not shutil.which("powershell.exe"):
            self.skipTest("Windows native volume APIs are required")
        helper = helper_module()
        with owned_temp() as root:
            def native_run(*args, **kwargs):
                return subprocess.run([shutil.which("powershell.exe"), *args[1:]], capture_output=True, text=True, timeout=20, check=False)
            with mock.patch.object(helper, "run", side_effect=native_run):
                identity = helper.windows_volume_identity(str(root))
            self.assertRegex(identity, r"^\\\\\?\\volume\{[0-9a-f-]{36}\}\\:[0-9a-f]{8}$")

    def test_mismatch_publishes_complete_unavailable_projection_and_cleans_atomic_temp(self):
        helper = helper_module()
        with owned_temp() as root:
            source_root = root / "sources"
            source_root.mkdir()
            projection = root / "run" / "identities.json"
            record = {"sources": [
                {"alias": "manuals", "source_kind": "ntfs", "canonical_identity": "retained-volume", "last_runtime_identity": {"device": 1, "inode": 2}},
                {"alias": "cognita-self-test", "source_kind": "installation_ext4", "canonical_identity": "retained-ext4", "last_runtime_identity": {"device": 3, "inode": 4}},
            ]}
            def prepare(row, target):
                if row["alias"] == "manuals":
                    raise helper.SourceIdentityMismatch("configured source identity changed for alias manuals")
                return helper.projection_row(row["alias"], row["source_kind"], "available", row["last_runtime_identity"])
            with mock.patch.object(helper, "ROOT", source_root), mock.patch.object(helper, "PROJECTION", projection), mock.patch.object(helper, "wait_for_drvfs"), mock.patch.object(helper, "run"), mock.patch.object(helper, "read_owned_record", return_value=record), mock.patch.object(helper, "prepare_source", side_effect=prepare), mock.patch.object(helper, "write_owned_record") as write_record:
                with self.assertRaises(helper.SourceIdentityMismatch):
                    helper.prepare()
            sources = json.loads(projection.read_text())["sources"]
            self.assertEqual(sources[0], {"alias": "manuals", "source_kind": "ntfs", "observation": "unavailable", "identity": {"device": 1, "inode": 2}})
            self.assertEqual(sources[1]["observation"], "available")
            write_record.assert_not_called()
            self.assertFalse(projection.with_suffix(".tmp").exists())
