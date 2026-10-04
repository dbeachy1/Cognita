"""Execute maintenance boundaries without invoking WSL or the live installer."""
from __future__ import annotations

from contextlib import contextmanager
import io
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import sys
from types import ModuleType
import unittest
from unittest import mock
import urllib.request

REPO = Path(__file__).resolve().parents[1]
WINDOWS = REPO / 'scripts' / 'windows'
spec = importlib.util.spec_from_file_location('windows_update', WINDOWS / 'Update-Release.py')
assert spec and spec.loader
update = importlib.util.module_from_spec(spec)
spec.loader.exec_module(update)


@contextmanager
def owned_temp():
    with tempfile.TemporaryDirectory(prefix='cognita-windows-update-test-') as value:
        path = Path(value)
        yield path
    if path.exists():
        raise AssertionError(f'Owned test fixture remains: {path}')


def run_ps(case, *, setup=''):
    executable = shutil.which('pwsh')
    if not executable:
        raise unittest.SkipTest('PowerShell 7 required for executable lifecycle checks')
    source = (WINDOWS / 'Install-CognitaWindows.ps1').read_text()
    definitions = source[source.index('function Say('):source.index('\n$mutex=')]
    with owned_temp() as root:
        script = root / 'test.ps1'
        script.write_bytes(PS_MODEL.replace('__DEFINITIONS__', definitions)
                           .replace('__ROOT__', str(root).replace("'", "''"))
                           .replace('__SETUP__', setup).replace('__CASE__', case)
                           .replace('\r\n', '\n').replace('\n', '\r\n').encode('utf-8-sig'))
        process = subprocess.Popen([executable, '-NoProfile', '-NonInteractive', '-File', str(script)],
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            output, error = process.communicate(timeout=30)
        finally:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=10)
        return process.returncode, output + error


PS_MODEL = r'''
Set-StrictMode -Version Latest
$ErrorActionPreference='Stop'
$script:Distro='Cognita-Windows';$script:Project='cognita-windows';$script:Unit='cognita-compose.service';$script:Task='Cognita-Windows'
$script:McpPort=10675;$script:AdminPort=10676;$script:CurrentMode='full';$script:ProjectionChanged=$false
$Mode=$null;$Source=@();$SmbSource=@();$ExpectedInstallationId='11111111-1111-1111-1111-111111111111'
__DEFINITIONS__
$script:Root='__ROOT__';$script:RecordPath=Join-Path $script:Root 'install.json'
$BundlePath='candidate'
$script:Record=[pscustomobject]@{schema=1;installation_id=$ExpectedInstallationId;distro=$script:Distro;compose_project=$script:Project;unit=$script:Unit;task=$script:Task;source_root='/srv/cognita/sources';config_root='/srv/cognita/config';postgres_root='/srv/cognita/postgres';model_cache_root='/srv/cognita/models';transfer_root='/srv/cognita/transfers';workspace_root='/srv/cognita/workspaces';mcp_port=8675;admin_port=10676;bundle_path='prior';bundle_sha256='prior-sum';version='13.5.0';commit=('a'*40);mode='full';image_ref_cognita_cpu='prior-cpu';image_cognita_cpu='prior-cpu-id';image_ref_postgres='pg-pin';image_postgres='pg-id';image_ref_workspace_runtime='prior-workspace';image_workspace_runtime='prior-workspace-id';sources=@([pscustomobject]@{alias='cognita-self-test'});created_utc='created';updated_utc='old'}
$script:Record|ConvertTo-Json -Depth 12|Set-Content -LiteralPath $script:RecordPath
$script:OldBytes=[IO.File]::ReadAllBytes($script:RecordPath)
$script:Events=[Collections.Generic.List[string]]::new();$script:Failure='';$script:Manual=$true
$script:Prior=@{Root='prior';Sum='prior-sum';Metadata=@{version='13.5.0';commit=('a'*40);bundle_mode='full';image_ref_cognita_cpu='prior-cpu';image_cognita_cpu='prior-cpu-id';image_ref_postgres='pg-pin';image_postgres='pg-id';image_ref_workspace_runtime='prior-workspace';image_workspace_runtime='prior-workspace-id'}}
$script:Candidate=@{Root='candidate';Sum='candidate-sum';Metadata=@{version='13.6.0';commit=('b'*40);bundle_mode='full';qualification_cpu_full='executed';image_ref_cognita_cpu='candidate-cpu';image_cognita_cpu='candidate-cpu-id';image_ref_postgres='pg-pin';image_postgres='pg-id';image_ref_workspace_runtime='candidate-workspace';image_workspace_runtime='candidate-workspace-id'}}
function Verify-Owner{return $script:Record}
function Verify-Bundle($Path){if($Path -ceq 'prior'){return $script:Prior};return $script:Candidate}
function Get-SourceRepairState($b){return [pscustomobject]@{complete=$true;sources_ready=$true;root_shared=$true;docker_active=$true;hook_current=$true}}
function Get-ScheduledTask {param($TaskName,$ErrorAction);return [pscustomobject]@{State='Running'}}
function Assert-LauncherTask($task){$script:Events.Add('task')}
function Convert-WindowsPathToWsl($Path,$Operation){return '/mapped/'+$Path}
function Invoke-UpdateHelper($Operation,$Arguments,$Timeout){
    $script:Events.Add($Operation)
    if($Operation -eq $script:Failure){throw "injected $Operation"}
    if($Operation -ceq 'begin'){return @('/tmp/cognita-windows-update-owned')}
    if($Operation -ceq 'guard'){return @('{"manual":'+$script:Manual.ToString().ToLowerInvariant()+',"verified":true}')}
    return @('passed')
}
function Save-Record($r){$script:Events.Add('publish');$r|ConvertTo-Json -Depth 12|Set-Content -LiteralPath $script:RecordPath;if($script:Failure -ceq 'publish'){throw 'injected publication after replace'};$script:Published=$r}
function Secure-Path {param($Path,[switch]$Directory,[switch]$StateRootTraversal)}
function DistroExists($name){return $true}
function Run($Exe,$Arguments,$Timeout){if($Arguments -contains 'cat' -or $Arguments -contains 'bash'){return @($ExpectedInstallationId)};throw 'unexpected process'}
function Test-InstalledReleaseInputs($b,$r){return $true}
function Install-FirstRun($b,$r){throw 'bootstrap is forbidden'}
function Prepare-RecordedSources {param($Bundle,[switch]$AcceptedHelper);throw 'source preparation is forbidden'}
function Stop-TaskSession{throw 'stack/unit stop is forbidden'}
function Invoke-Compose{throw 'unexpected direct Compose'}
function Get-Sources {return @($script:Record.sources)}
__SETUP__
__CASE__
'''


class PowerShellLifecycleTests(unittest.TestCase):
    def case(self, value, setup=''):
        code, output = run_ps(value, setup=setup)
        self.assertEqual(code, 0, output)

    def test_success_publishes_one_current_record_after_packaged_proof(self):
        self.case(r'''
Do-UpdateRelease
if(($script:Events -join ',') -cne 'task,begin,apply,task,publish,cleanup'){throw ($script:Events -join ',')}
$next=$script:Published
foreach($key in @('installation_id','created_utc','mode','mcp_port','admin_port','image_postgres','image_ref_postgres')){if($next.$key -cne $script:Record.$key){throw "changed preserved $key"}}
if($next.version -cne '13.6.0' -or $next.commit -cne ('b'*40) -or $next.bundle_path -cne 'candidate' -or $next.image_workspace_runtime -cne 'candidate-workspace-id'){throw 'incomplete current record'}
if($next.PSObject.Properties['active_application']){throw 'parallel provenance introduced'}
''')

    def test_workspace_failure_restores_selection_and_never_publishes(self):
        self.case(r'''
$script:Failure='apply';$failed=$false
try{Do-UpdateRelease}catch{$failed=$_.Exception.Message -match 'injected apply'}
if(-not $failed -or ($script:Events -join ',') -cne 'task,begin,apply,rollback,cleanup'){throw 'failure ordering'}
if([Convert]::ToBase64String([IO.File]::ReadAllBytes($script:RecordPath)) -cne [Convert]::ToBase64String($script:OldBytes)){throw 'owner record changed'}
''')

    def test_publication_failure_restores_exact_prior_record_bytes(self):
        self.case(r'''
$script:Failure='publish';$failed=$false
try{Do-UpdateRelease}catch{$failed=$_.Exception.Message -match 'injected publication'}
if(-not $failed -or -not $script:Events.Contains('rollback') -or -not $script:Events.Contains('cleanup')){throw 'failed publication cleanup'}
if([Convert]::ToBase64String([IO.File]::ReadAllBytes($script:RecordPath)) -cne [Convert]::ToBase64String($script:OldBytes)){throw 'exact prior owner record not restored'}
''')

    def test_failed_rollback_stops_only_the_pair_and_cleans_snapshot(self):
        self.case(r'''
function Invoke-UpdateHelper($Operation,$Arguments,$Timeout){$script:Events.Add($Operation);if($Operation -in @('apply','rollback')){throw 'failure'};if($Operation -eq 'begin'){return @('/tmp/cognita-windows-update-owned')};return @('passed')}
try{Do-UpdateRelease}catch{}
if(($script:Events -join ',') -cne 'task,begin,apply,rollback,stop-pair,cleanup'){throw 'unverified restoration did not stop/cleanup'}
''')

    def test_preflight_failure_leaves_record_and_selection_unmodified(self):
        self.case(r'''
$script:Failure='begin';try{Do-UpdateRelease}catch{}
if(($script:Events -join ',') -cne 'task,begin'){throw 'preflight failure mutated state'}
''')

    def test_engine_drift_and_missing_qualification_refuse_before_begin(self):
        self.case(r'''
foreach($kind in @('engine','qualification')){
    $script:Events.Clear()
    if($kind -eq 'engine'){$script:Candidate.Metadata.image_postgres='other'}else{$script:Candidate.Metadata.image_postgres='pg-id';$script:Candidate.Metadata.qualification_cpu_full=''}
    $failed=$false;try{Do-UpdateRelease}catch{$failed=$true}
    if(-not $failed -or $script:Events.Contains('begin')){throw 'unqualified or engine-changing candidate admitted'}
}
''')

    def test_manual_alias_is_guarded_before_partial_install_bootstrap(self):
        self.case(r'''
$Mode='Full';$BundlePath='prior'
function Get-SourceRepairState($b){return [pscustomobject]@{complete=$false;sources_ready=$true;root_shared=$true;docker_active=$true;hook_current=$true}}
$failed=$false;try{Do-Install -CompletePartial}catch{$failed=$_.Exception.Message -match 'mapping was preserved'}
if(-not $failed -or -not $script:Events.Contains('guard')){throw 'manual alias reached bootstrap'}
''')

    def test_repair_preserves_manual_alias_even_if_service_failed(self):
        self.case(r'''
Do-Repair
if(-not $script:Events.Contains('guard') -or -not $script:Events.Contains('repair')){throw 'repair did not use preservation path'}
''')

    def test_numeric_recorded_ports_replace_stale_defaults(self):
        self.case(r'''
$r=Read-Record
if($script:McpPort -ne 8675 -or $script:AdminPort -ne 10676){throw 'recorded ports were rejected or ignored'}
$r.mcp_port='8675';$r|ConvertTo-Json -Depth 12|Set-Content $script:RecordPath
$rejected=$false;try{Read-Record}catch{$rejected=$true}
if(-not $rejected){throw 'string port admitted'}
''')


class RemoteLifecycleTests(unittest.TestCase):
    def test_pipe_timeout_stops_session_even_after_leader_exits(self):
        process = mock.MagicMock()
        process.__enter__.return_value = process
        process.pid = 12345
        process.poll.return_value = 0
        process.communicate.side_effect = [subprocess.TimeoutExpired(['owned'], 1), ('', '')]
        with mock.patch.object(subprocess, 'Popen', return_value=process), \
                mock.patch.object(update.signal, 'SIGKILL', 9, create=True), \
                mock.patch.object(os, 'killpg', create=True) as kill:
            with self.assertRaises(subprocess.TimeoutExpired):
                update.run(['owned'], timeout=1)
        kill.assert_called_once_with(12345, 9)
        self.assertEqual(process.communicate.call_count, 2)
        process.wait.assert_called_once_with(timeout=10)
        process.__exit__.assert_called_once()

    def test_status_reports_labels_readiness_and_port_disagreement(self):
        observer = object.__new__(update.Update)
        observer.record = {'version':'13.6.0','commit':'b'*40,'mode':'full','mcp_port':8675,'admin_port':10676,
                           'image_cognita_cpu':'cpu','image_workspace_runtime':'runtime','image_postgres':'pg'}
        observer.version = '13.6.0'
        labels = {'org.opencontainers.image.version':'13.6.0','org.opencontainers.image.revision':'b'*40}
        values = {service:{'Image':image,'State':{'Running':True,'Health':{'Status':'healthy'}},
                           'Config':{'Labels':dict(labels),'Env':['COGNITA_VERSION=13.6.0']}}
                  for service,image in (('cognita','cpu'),('workspace-runtime','runtime'),('postgres','pg'))}
        observer.container = mock.Mock(side_effect=lambda service,**kw: values[service])
        observer.config = mock.Mock(return_value={})
        observer.validate_ports = mock.Mock()
        with owned_temp() as root:
            observer.old_bundle = root
            (root/'compose.cpu.images.yaml').write_bytes(b'pair')
            (root/'compose.cpu.full.images.yaml').write_bytes(b'pair')
            with mock.patch.object(update,'ROOT',root):
                self.assertTrue(observer.status()['coherent'])
                values['workspace-runtime']['Config']['Labels']['org.opencontainers.image.revision'] = 'c'*40
                result = observer.status()
                self.assertFalse(result['coherent'])
                self.assertEqual(result['services']['workspace-runtime']['commit'], 'c'*40)
                values['workspace-runtime']['Config']['Labels'] = dict(labels)
                observer.validate_ports.side_effect = RuntimeError('ports drift')
                result = observer.status()
                self.assertFalse(result['coherent'])
                self.assertFalse(result['ports_agree'])

    def test_partial_candidate_is_recognized_before_current_validation(self):
        observer = object.__new__(update.Update)
        observer.record = {'mode':'full','image_ref_postgres':'pg-pin','image_postgres':'pg-id','unit':'unit'}
        observer.old = {'version':'old','commit':'old-sha','image_cognita_cpu':'old-cpu','image_workspace_runtime':'old-runtime'}
        observer.new = {'version':'new','commit':'new-sha','bundle_mode':'full','image_ref_postgres':'pg-pin',
                        'image_postgres':'pg-id','image_cognita_cpu':'new-cpu','image_workspace_runtime':'new-runtime'}
        observer.version = 'new'
        observer.run = mock.Mock()
        observer.image = mock.Mock()
        observer.preserved = mock.Mock(return_value={'preserved':True})
        observer.container = mock.Mock(side_effect=[{'Image':'new-cpu'}, {'Image':'old-runtime'}])
        with owned_temp() as root:
            installed, old, new = root/'installed', root/'old', root/'new'
            for path in (installed, old, new):
                path.mkdir()
                for name in ('compose.yaml','compose.cpu.yaml','compose.workspace.yaml'):
                    (path/name).write_bytes(b'identical base')
            (old/'compose.cpu.full.images.yaml').write_bytes(b'old pair')
            (new/'compose.cpu.full.images.yaml').write_bytes(b'new pair')
            (installed/'compose.cpu.images.yaml').write_bytes(b'new pair')
            observer.old_bundle, observer.candidate = old, new
            with mock.patch.object(update, 'ROOT', installed):
                self.assertEqual(observer.preflight(), {'preserved':True})
                observer.container.side_effect = [{'Image':'unrelated'}, {'Image':'old-runtime'}]
                with self.assertRaisesRegex(RuntimeError, 'Unrelated running image drift'):
                    observer.preflight()
                (installed/'compose.cpu.images.yaml').write_bytes(b'third selection')
                with self.assertRaisesRegex(RuntimeError, 'Unrecognized partial release selection'):
                    observer.preflight()

    def test_changed_base_compose_and_database_engine_fail_before_mutation(self):
        observer = object.__new__(update.Update)
        observer.record = {'mode':'full','image_ref_postgres':'pg-pin','image_postgres':'pg-id'}
        observer.new = {'bundle_mode':'full','image_ref_postgres':'pg-pin','image_postgres':'other'}
        observer.run = mock.Mock()
        with self.assertRaisesRegex(RuntimeError, 'PostgreSQL engine change'):
            observer.preflight()
        observer.run.assert_not_called()
        observer.new['image_postgres'] = 'pg-id'
        with owned_temp() as root:
            installed, candidate = root/'installed', root/'candidate'
            installed.mkdir()
            candidate.mkdir()
            (installed/'compose.yaml').write_bytes(b'old base')
            (candidate/'compose.yaml').write_bytes(b'changed base')
            observer.candidate = candidate
            with mock.patch.object(update, 'ROOT', installed):
                with self.assertRaisesRegex(RuntimeError, 'Base Compose changes'):
                    observer.preflight()
        observer.run.assert_not_called()

    def test_source_guard_uses_effective_mount_and_rejects_projection_drift(self):
        original = Path.read_text
        with owned_temp() as root:
            aliases = root/'sources'
            aliases.mkdir()
            reserved, manual = aliases/'cognita-self-test', aliases/'manual'
            reserved.mkdir()
            manual.mkdir()
            projection = root/'projection.json'
            rows = [{'alias': path.name,'source_kind':kind,'observation':'available',
                     'identity':{'device':path.stat().st_dev,'inode':path.stat().st_ino}}
                    for path, kind in ((reserved,'installation_ext4'),(manual,'ntfs'))]
            projection.write_text(json.dumps({'schema':1,'sources':rows}))
            record = {'sources':[{'alias':'cognita-self-test'}]}
            with mock.patch.object(update,'ROOT',root), mock.patch.object(update,'PROJECTION',projection), \
                    mock.patch.object(update,'effective_mount',side_effect=lambda path: 2 if path==manual else 1), \
                    mock.patch.object(Path,'read_text',autospec=True,side_effect=lambda path,*a,**kw: '' if path.as_posix()=='/proc/self/mountinfo' else original(path,*a,**kw)):
                facts = update.source_facts(record)
                self.assertTrue(facts['manual'])
                self.assertEqual(facts['aliases']['manual'][-1],2)
                rows[1]['identity']['inode'] += 1
                projection.write_text(json.dumps({'schema':1,'sources':rows}))
                with self.assertRaisesRegex(RuntimeError, 'effective alias'):
                    update.source_facts(record)

    def test_toolbox_cleanup_refuses_to_remove_unowned_name(self):
        observer = object.__new__(update.Update)
        observer.record = {'compose_project':'cognita-windows'}
        observer.run = mock.Mock(return_value=subprocess.CompletedProcess([],0,json.dumps([
            {'Config':{'Labels':{'com.docker.compose.project':'unrelated'}}}])))
        with self.assertRaisesRegex(RuntimeError,'ownership could not be verified'):
            observer.cleanup_oneoff('cognita-windows-operation-'+'a'*32)
        self.assertEqual(observer.run.call_count,1)

    def test_packaged_proof_executes_client_operations_and_cleans_on_job_failure(self):
        for job_state in ('succeeded','failed'):
            with self.subTest(job_state=job_state), owned_temp() as root:
                secret = root/'synthetic-bearer'
                secret.write_text('synthetic test input')
                events = []
                state = {'present':False}
                class Client:
                    def __init__(self,*args,**kw): pass
                    def health(self): return {'runtime':'ready'}
                    def call(self,workspace,operation,arguments):
                        self.assert_owned(workspace)
                        events.append(operation)
                        if operation=='ensure':
                            state['present']=True
                            return {'state':'running'}
                        if operation=='inspect':
                            return {'state':'running' if state['present'] else 'absent'}
                        if operation=='fs_write':
                            return {'bytes':21}
                        if operation=='fs_read':
                            return {'content':'Cognita update proof\n'}
                        if operation=='job_start':
                            return {'job_id':'owned-job'}
                        if operation=='job_get':
                            return {'state':job_state,'exit_code':0 if job_state=='succeeded' else 1}
                        if operation=='remove':
                            state['present']=False
                            return {'state':'absent'}
                        raise AssertionError(operation)
                    def assert_owned(self,value):
                        if value!='owned-workspace':
                            raise AssertionError('unexpected workspace')
                module = ModuleType('cognita.workspace')
                module.BrokerRuntimeClient=Client
                with mock.patch.dict(sys.modules,{'cognita.workspace':module}), \
                        mock.patch.dict(os.environ,{'COGNITA_WORKSPACE_RUNTIME_URL':'http://private/v1','COGNITA_INTERNAL_BEARER_FILE':str(secret)}), \
                        mock.patch.object(sys,'argv',['proof','owned-workspace']):
                    previous_handler = update.signal.getsignal(update.signal.SIGTERM)
                    try:
                        if job_state=='failed':
                            with self.assertRaises(AssertionError):
                                exec(compile(update.WORKSPACE_PROOF,'packaged-workspace-proof','exec'),{})
                        else:
                            exec(compile(update.WORKSPACE_PROOF,'packaged-workspace-proof','exec'),{})
                    finally:
                        update.signal.signal(update.signal.SIGTERM, previous_handler)
                self.assertEqual(events,['ensure','inspect','fs_write','fs_read','job_start','job_get','remove','inspect'])
                self.assertFalse(state['present'])

    def test_broker_readiness_does_not_accept_a_different_reported_version(self):
        with owned_temp() as root:
            secret = root/'synthetic-bearer'
            secret.write_text('synthetic test input')
            class Client:
                def __init__(self,base_url,*args,**kw): self.base_url=base_url.rstrip('/')
                def health(self): return {'runtime':'ready'}
            module = ModuleType('cognita.workspace')
            module.BrokerRuntimeClient=Client
            for actual in ('13.6.0','13.5.0'):
                with self.subTest(actual=actual), mock.patch.dict(sys.modules,{'cognita.workspace':module}), \
                        mock.patch.dict(os.environ,{'COGNITA_WORKSPACE_RUNTIME_URL':'http://private-runtime','COGNITA_INTERNAL_BEARER_FILE':str(secret)}), \
                        mock.patch.object(sys,'argv',['proof','readiness','13.6.0']), \
                        mock.patch.object(urllib.request,'urlopen',return_value=io.BytesIO(json.dumps({'info':{'version':actual}}).encode())) as opening:
                    if actual != '13.6.0':
                        with self.assertRaises(AssertionError):
                            exec(compile(update.WORKSPACE_CLIENT + update.BROKER_READINESS,'packaged-broker-readiness','exec'),{})
                    else:
                        exec(compile(update.WORKSPACE_CLIENT + update.BROKER_READINESS,'packaged-broker-readiness','exec'),{})
                    opening.assert_called_once_with('http://private-runtime/openapi.json',timeout=10)

    def test_broker_readiness_uses_real_routes_for_root_and_v1_urls(self):
        from fastapi.testclient import TestClient
        from cognita.runtime_broker.app import create_app
        from cognita.release_identity import APPLICATION_VERSION
        from cognita.workspace import BrokerRuntimeClient
        from urllib.parse import urlsplit

        with owned_temp() as root:
            secret = root / 'synthetic-bearer'
            secret.write_text('s' * 32)
            app = create_app(secret='s' * 32)
            # Exercise the actual HTTP routes without starting a VM/runtime.
            app.state.broker_service._runtime_ready = True
            with TestClient(app) as transport:
                self.assertEqual(transport.get('/v1/openapi.json').status_code, 404)
                for suffix in ('', '/', '/v1', '/v1/'):
                    with self.subTest(suffix=suffix):
                        def open_local(url, timeout):
                            response = transport.get(urlsplit(url).path)
                            response.raise_for_status()
                            return io.BytesIO(response.content)
                        def broker(base_url, bearer, **kwargs):
                            return BrokerRuntimeClient(base_url, bearer, client=transport, **kwargs)
                        with mock.patch.dict(os.environ, {'COGNITA_WORKSPACE_RUNTIME_URL': 'http://testserver' + suffix,
                                'COGNITA_INTERNAL_BEARER_FILE': str(secret)}), \
                                mock.patch.object(sys, 'argv', ['proof', 'readiness', APPLICATION_VERSION]), \
                                mock.patch('cognita.workspace.BrokerRuntimeClient', side_effect=broker), \
                                mock.patch.object(urllib.request, 'urlopen', side_effect=open_local):
                            exec(compile(update.WORKSPACE_CLIENT + update.BROKER_READINESS,
                                         'packaged-broker-readiness', 'exec'), {})

    def test_release_env_changes_only_one_authoritative_entry(self):
        data = b'# installed\r\nCOGNITA_VERSION=old\r\nCOGNITA_MCP_HOST_PORT=8675\r\nCOGNITA_SERVICE_UID=1000\r\nSECRET_PATH=/keep\r\n'
        self.assertEqual(update.release_environment(data, '13.6.0'), data.replace(b'=old', b'=13.6.0'))
        with self.assertRaises(RuntimeError):
            update.release_environment(data + b'COGNITA_VERSION=other\n', '13.6.0')

    def test_child_compose_env_uses_installed_values_over_stale_parent(self):
        observer = object.__new__(update.Update)
        observer.values = {'COGNITA_VERSION': 'old', 'COGNITA_SERVICE_UID': '1000', 'COGNITA_MCP_HOST_PORT': '8675'}
        observer.version = '13.6.0'
        observer.prefix = ['docker', 'compose']
        observer.run = mock.Mock(return_value=None)
        with mock.patch.dict(os.environ, {'COGNITA_VERSION': 'wrong', 'COGNITA_SERVICE_UID': '0'}):
            observer.compose('config')
        env = observer.run.call_args.kwargs['env']
        self.assertEqual(env['COGNITA_VERSION'], '13.6.0')
        self.assertEqual(env['COGNITA_SERVICE_UID'], '1000')
        self.assertEqual(env['COGNITA_MCP_HOST_PORT'], '8675')

    def test_running_listener_drift_is_rejected_even_when_rendered_ports_match(self):
        observer = object.__new__(update.Update)
        observer.record = {'mcp_port': 8675, 'admin_port': 10676}
        config = {'services': {'cognita': {'ports': [
            {'host_ip': '127.0.0.1', 'target': 8675, 'published': '8675'},
            {'host_ip': '127.0.0.1', 'target': 8676, 'published': '10676'}]}}}
        container = {'HostConfig': {'PortBindings': {'8675/tcp': [{'HostIp': '127.0.0.1', 'HostPort': '8675'}],
                                                  '8676/tcp': [{'HostIp': '127.0.0.1', 'HostPort': '10676'}]}}}
        observer.container = mock.Mock(return_value=container)
        observer.validate_ports(config)
        container['HostConfig']['PortBindings']['8675/tcp'][0]['HostIp'] = '0.0.0.0'
        with self.assertRaisesRegex(RuntimeError, 'owned listeners'):
            observer.validate_ports(config)

    def test_apply_requires_proof_and_cleanup_before_returning(self):
        observer = mock.Mock()
        observer.preserved.return_value = {'fixed': True}
        observer.candidate = Path('/candidate')
        observer.new = {'version': '13.6.0'}
        with owned_temp() as root:
            (root / 'snapshot.json').write_text(json.dumps({'before': {'fixed': True}}))
            (root / 'env').write_bytes(b'COGNITA_VERSION=old\n')
            observer.verify.side_effect = RuntimeError('Workspace operation failed')
            with self.assertRaisesRegex(RuntimeError, 'Workspace operation'):
                update.apply(observer, root)
            self.assertTrue((root / 'selected').exists())
            self.assertEqual([call[0] for call in observer.mock_calls],
                             ['preserved','load_images','compose','selection','toolbox','start_pair','verify'])
            observer.compose.assert_called_once_with('stop', '--timeout', '30', 'cognita', 'workspace-runtime')

    def test_preselection_failure_does_not_stop_healthy_services(self):
        observer = mock.Mock()
        observer.preserved.return_value = {'fixed': True}
        with owned_temp() as root:
            (root / 'snapshot.json').write_text(json.dumps({'before': {'fixed': True}}))
            update.rollback(observer, root)
        observer.compose.assert_not_called()

    def test_failed_restore_stops_pair_and_reports_explicit_failure(self):
        observer = mock.Mock()
        observer.old_bundle = Path('/prior')
        observer.old = {'version': 'prior'}
        observer.start_pair.side_effect = RuntimeError('startup refused')
        with owned_temp() as root:
            (root / 'snapshot.json').write_text(json.dumps({'before': {}}))
            (root / 'env').write_bytes(b'COGNITA_VERSION=old\n')
            (root / 'selected').touch()
            with self.assertRaisesRegex(RuntimeError, 'left stopped; PostgreSQL retained'):
                update.rollback(observer, root)
        self.assertEqual(observer.compose.call_count, 2)
        for call in observer.compose.call_args_list:
            self.assertEqual(call.args, ('stop', '--timeout', '30', 'cognita', 'workspace-runtime'))

    def test_toolbox_engine_descriptor_is_distinct_from_sdk_config(self):
        import hashlib
        import tarfile
        tag = 'cognita-workspace-toolbox:12.6.0'
        config = 'sha256:' + 'a' * 64
        manifest = json.dumps({'config': {'digest': config}}).encode()
        manifest_id = 'sha256:' + hashlib.sha256(manifest).hexdigest()
        index = json.dumps({'manifests': [{'digest': manifest_id, 'size': len(manifest)}]}).encode()
        engine_id = 'sha256:' + hashlib.sha256(index).hexdigest()
        with owned_temp() as root:
            archive = root / 'toolbox.tar'
            with tarfile.open(archive, 'w') as saved:
                for name, data in {
                    'index.json': json.dumps({'manifests': [{'digest': engine_id, 'size': len(index),
                        'annotations': {'io.containerd.image.name': 'docker.io/library/' + tag}}]}).encode(),
                    'blobs/sha256/' + engine_id[7:]: index,
                    'blobs/sha256/' + manifest_id[7:]: manifest,
                }.items():
                    info = tarfile.TarInfo(name)
                    info.size = len(data)
                    saved.addfile(info, io.BytesIO(data))
            identity = update.toolbox_engine_identity(archive, tag, config)
            self.assertEqual(identity, engine_id)
            self.assertNotEqual(identity, config)
            self.assertTrue(update.engine_matches_toolbox({'Id': engine_id, 'Descriptor': {'digest': engine_id}}, identity, config))
            self.assertTrue(update.engine_matches_toolbox({'Id': config}, identity, config))
            self.assertFalse(update.engine_matches_toolbox({'Id': engine_id, 'Descriptor': {'digest': config}}, identity, config))
            self.assertFalse(update.engine_matches_toolbox({'Id': engine_id}, identity, config))
            with self.assertRaisesRegex(RuntimeError, 'differs from config'):
                update.toolbox_engine_identity(archive, tag, 'sha256:' + 'b' * 64)
            with self.assertRaisesRegex(RuntimeError, 'tag is missing'):
                update.toolbox_engine_identity(archive, 'wrong:tag', config)
            with tarfile.open(root / 'classic.tar', 'w'):
                pass
            self.assertIsNone(update.toolbox_engine_identity(root / 'classic.tar', tag, config))

    def test_toolbox_reuses_or_reloads_docker_descriptor_without_equating_config(self):
        config_id, engine_id = 'sha256:' + 'a' * 64, 'sha256:' + 'b' * 64
        for initially_matching, loaded_matching in ((True, True), (False, True), (False, False)):
            with self.subTest(initially_matching=initially_matching, loaded_matching=loaded_matching), owned_temp() as root:
                (root / 'toolbox.tar').write_bytes(b'accepted synthetic archive')
                (root / 'toolbox-12.6.0.tar').write_bytes(b'accepted synthetic archive')
                observer = object.__new__(update.Update)
                observer.values = {'COGNITA_TOOLBOX_IMAGE_CACHE_ROOT': str(root), 'COGNITA_WORKSPACE_DATA_ROOT': str(root)}
                observer.loader = mock.Mock(return_value=subprocess.CompletedProcess([], 0))
                binding = {'tag': 'cognita-workspace-toolbox:12.6.0', 'config_digest': config_id,
                           'archive_digest': 'sha256:' + update.digest(root / 'toolbox.tar')}
                observer.runtime_command = mock.Mock(return_value=subprocess.CompletedProcess([], 0, json.dumps(binding)))
                calls = []
                def run(argv, **kwargs):
                    calls.append(argv)
                    if argv[:2] == ['docker', 'load']:
                        return subprocess.CompletedProcess(argv, 0, '')
                    matching = initially_matching if len(calls) == 1 else loaded_matching
                    digest = engine_id if matching else 'sha256:' + 'c' * 64
                    return subprocess.CompletedProcess(argv, 0, json.dumps([{'Id': digest, 'Descriptor': {'digest': digest}}]))
                observer.run = run
                with mock.patch.object(update, 'toolbox_engine_identity', return_value=engine_id):
                    if loaded_matching:
                        observer.toolbox(root, {'toolbox_version': '12.6.0'})
                    else:
                        with self.assertRaisesRegex(RuntimeError, 'Docker Toolbox identity'):
                            observer.toolbox(root, {'toolbox_version': '12.6.0'})
                loads = [call for call in calls if call[:2] == ['docker', 'load']]
                self.assertEqual(len(loads), int(not initially_matching))
                self.assertEqual(observer.loader.call_count, 3 if loaded_matching else 1)

    def test_all_inline_shell_forms_normalize_only_program_operand(self):
        # run_ps writes the actual Windows PowerShell file with CRLF; never
        # infer newline behavior from Python's universal-newline read_text.
        code, output = run_ps(r'''
if(-not [Text.Encoding]::UTF8.GetString([IO.File]::ReadAllBytes($PSCommandPath)).Contains("`r`n")){throw 'expected physical CRLF source'}
$script:SystemdReady=$true
$program="case x in`r`nx) printf ok;;`r`nesac"
$argument="literal`r`nargument"
foreach($shell in @('bash','sh','/bin/bash','/bin/sh')){
 foreach($flag in @('-c','-lc')){
  foreach($budget in @(30,600)){
   $inputArgs=@('-d',$script:Distro,'-u','root','--exec',$shell,$flag,$program,$argument)
   $call=Get-BoundedWslArguments $inputArgs $budget
   if($call.Arguments[-2] -cne $program.Replace("`r`n","`n")){throw 'script operand not normalized'}
   if($call.Arguments[-1] -cne $argument -or $inputArgs[-2] -cne $program){throw 'literal argument or caller array changed'}
   if($budget -gt 300 -and $call.Arguments[7].Contains("`r")){throw 'owned launch script retained CRLF'}
  }
 }
}
foreach($command in @(@('python3','-c',$program,$argument),@('printf',$program,$argument))){
 $call=Get-BoundedWslArguments (@('-d',$script:Distro,'-u','root','--exec')+$command) 30
 if($call.Arguments[-2] -cne $program -or $call.Arguments[-1] -cne $argument){throw 'non-shell argument changed'}
}
Write-Output 'all shell forms and literal arguments passed'
''')
        self.assertEqual(code, 0, output)
        self.assertIn('all shell forms and literal arguments passed', output)

    def test_preflight_failure_phases_are_safe_and_precise(self):
        cases = {
            'owner': "function Verify-Owner {throw 'private-child-content'}",
            'prior-bundle': "function Verify-Bundle {throw 'private-child-content'}",
            'candidate-bundle': "function Verify-Bundle($Path){if($Path -ceq 'prior'){return $script:Prior};throw 'private-child-content'}",
            'source-state': "function Get-SourceRepairState {throw 'private-child-content'}",
            'launcher': "function Assert-LauncherTask {throw 'private-child-content'}",
            'paths': "function Convert-WindowsPathToWsl {throw 'private-child-content'}",
            'begin': "function Invoke-UpdateHelper {throw 'private-child-content'}",
        }
        for phase, setup in cases.items():
            with self.subTest(phase=phase):
                code, output = run_ps("try { Do-UpdateRelease } catch {}; Write-Output 'settled'", setup=setup)
                self.assertEqual(code, 0, output)
                self.assertIn('COGNITA_UPDATE_PHASE preflight-' + phase + ' failed', output)
                self.assertNotIn('private-child-content', output)

    def test_powershell_relays_phase_before_a_later_exception_overwrites_it(self):
        source = (WINDOWS / 'Install-CognitaWindows.ps1').read_text()
        production_run = source[source.index('function Run('):source.index('function Wsl(')]
        code, output = run_ps("try { Run 'controlled-command' @() 5 | Out-Null } catch {}; Write-Output 'settled'",
            setup=production_run + """function Invoke-OwnedProcess {
                return @{exit_code=1;stdout='private-child-text';stderr="`nCOGNITA_UPDATE_PHASE apply-toolbox failed`nCOGNITA_UPDATE_PHASE private-word failed`n"}
            }""")
        self.assertEqual(code, 0, output)
        self.assertIn('COGNITA_UPDATE_PHASE apply-toolbox failed', output)
        self.assertNotIn('private-child-text', output)
        self.assertNotIn('private-word', output)

    def test_failed_phase_reports_only_phase_and_retains_original_error(self):
        output = io.StringIO()
        with mock.patch('sys.stderr', output):
            with self.assertRaisesRegex(RuntimeError, 'private child content'):
                update.update_phase('apply-toolbox', mock.Mock(side_effect=RuntimeError('private child content')))
        self.assertEqual(output.getvalue(), 'COGNITA_UPDATE_PHASE apply-toolbox started\nCOGNITA_UPDATE_PHASE apply-toolbox failed\n')

    def test_toolbox_verification_owns_cleanup_on_loader_failure(self):
        observer = object.__new__(update.Update)
        observer.snapshot = None
        observer.record = {'compose_project': 'cognita-windows'}
        observer.compose = mock.Mock(side_effect=RuntimeError('loader timed out'))
        container = [{'Config': {'Labels': {'cognita.windows.update-owned': 'true',
                                            'com.docker.compose.project': 'cognita-windows'}}}]
        observer.run = mock.Mock(side_effect=[subprocess.CompletedProcess([], 0, json.dumps(container)),
                                             subprocess.CompletedProcess([], 0, ''),
                                             subprocess.CompletedProcess([], 0, '')])
        with self.assertRaisesRegex(RuntimeError, 'loader timed out'):
            observer.loader('verify', '12.6.0')
        command = observer.compose.call_args.args
        self.assertEqual(command[0], 'run')
        self.assertIn('--rm', command)
        self.assertIn('--no-deps', command)
        name = command[command.index('--name') + 1]
        self.assertTrue(name.startswith('cognita-windows-operation-'))
        self.assertEqual(observer.run.call_args_list[1].args[0], ['docker','container','rm','--force',name])


if __name__ == '__main__':
    unittest.main()
