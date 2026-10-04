"""Execute the packaged Windows control paths in fresh, isolated PowerShell processes."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import hashlib
import json
import os
import re
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock
import uuid

REPO = Path(__file__).resolve().parents[1]
INSTALLER = REPO / "scripts/windows/Install-CognitaWindows.ps1"
PWSH = shutil.which("pwsh")


def native_systemd_unavailability():
    """Separate native host-manager proof from the packaged application suite."""
    for command in ("systemctl", "systemd-run"):
        if not shutil.which(command):
            return (f"native systemd user-manager proof requires {command}; "
                    "the application image does not include a host service manager")
    try:
        probe = subprocess.run(["systemctl", "--user", "is-system-running"],
                               capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"native systemd user-manager probe unavailable ({type(exc).__name__}); requires a Linux host/user bus"
    # A degraded manager still owns transient units and cgroups. Do not skip
    # actual launch/timeout/cancellation assertions because another unit failed.
    if (probe.returncode, probe.stdout.strip()) not in ((0, "running"), (1, "degraded")):
        return "native systemd user manager is unavailable; requires an actual Linux host with a reachable user bus"
    return None


class NativeSystemdEnvironmentTests(unittest.TestCase):
    def test_missing_host_tools_skip_before_any_manager_command(self):
        for missing in ("systemctl", "systemd-run"):
            with self.subTest(command=missing), mock.patch.object(shutil, "which", side_effect=lambda name: None if name == missing else f"/usr/bin/{name}"), mock.patch.object(subprocess, "run") as run:
                self.assertIn(missing, native_systemd_unavailability())
                run.assert_not_called()

    def test_unavailable_user_bus_is_explicit_but_degraded_manager_is_exercised(self):
        with mock.patch.object(shutil, "which", return_value="/usr/bin/tool"), mock.patch.object(subprocess, "run") as run:
            run.return_value = subprocess.CompletedProcess([], 1, "offline\n", "no bus")
            self.assertIn("reachable user bus", native_systemd_unavailability())
            for status, result in (("running", 0), ("degraded", 1)):
                run.return_value = subprocess.CompletedProcess([], result, status + "\n", "")
                self.assertIsNone(native_systemd_unavailability())
            run.assert_called_with(["systemctl", "--user", "is-system-running"], capture_output=True, text=True, timeout=10)


def ps_string(value: object) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def production_definitions() -> str:
    # Keep initialization, StrictMode, and every production function. Only the final
    # action dispatch is withheld so each test can supply controlled OS/API boundaries.
    return INSTALLER.read_text(encoding="utf-8").split("\n$mutex=", 1)[0]


NATIVE_INSTALL_BOUNDARY = r'''
import json, pathlib, sys
state_path, record_path, log_path, exe, *args = sys.argv[1:]
state_path, record_path, log_path = map(pathlib.Path, (state_path, record_path, log_path))
state = json.loads(state_path.read_text()) if state_path.exists() else {'exists':False,'marker':''}
with log_path.open('a') as stream: stream.write(json.dumps(args)+'\n')
def save(): state_path.write_text(json.dumps(state))
def record(): return json.loads(record_path.read_text())
if args[0] == '--list':
    if state['exists']: print('Cognita-Windows')
elif args[0] == '--install': state['exists']=True; save()
elif args[0] == '--terminate': pass
else:
    assert args[:5] == ['-d','Cognita-Windows','-u','root','--exec'], args
    cmd=args[5:]
    if cmd[0]=='timeout': cmd=cmd[4:]
    if 'cognita-owned-cleanup' in cmd: sys.exit(0)
    if 'cognita-owned-launch' in cmd:
        assert state.get('systemd_ready'), 'Long work preceded PID1 proof'
        cmd=cmd[cmd.index('cognita-owned-launch')+4:]
    if cmd[0]=='wslpath': print('/accepted/'+pathlib.PurePosixPath(cmd[-1]).name)
    elif cmd[:2]==['cat','/etc/cognita-install-id']: print(state['marker'])
    elif cmd[0]=='stat': print('1:2')
    elif cmd[0]=='bash':
        text=cmd[2]
        if 'cognita-stop-owned-units' in cmd:
            for unit in cmd[cmd.index('cognita-stop-owned-units')+1:]: print('absent '+unit)
        elif 'if test -f /etc/cognita-install-id' in text: print(state['marker'])
        elif 'mktemp /etc/.cognita-install-id.' in text:
            assert record()['sources'][-1]['canonical_identity'] is None
            state['marker']=record()['installation_id']; save()
            if state.pop('fail_after_marker',False): save();sys.exit(17)
        elif 'systemd=true' in text: pass
        elif 'cat /proc/1/comm' in text: state['systemd_ready']=True; save()
        elif 'mktemp /run/cognita-install.' in text: state['bootstrap']=True; save()
        elif 'complete=false' in text:
            print(json.dumps(dict(complete=True,sources_ready=True,hook_current=True,root_shared=True,docker_active=True)))
        elif 'grep -q' in text: print('configured' if state.get('bootstrap') else 'incomplete')
        elif 'docker container rm --force' in text: pass
        else: raise AssertionError('Unexpected bash boundary')
    elif cmd[0]=='python3':
        if len(cmd)>2 and pathlib.PurePosixPath(cmd[1]).name=='Update-Release.py':
            assert cmd[2] in ('guard','ports')
            if cmd[2]=='guard': print(json.dumps(dict(manual=False,verified=bool(state.get('bootstrap')))))
        elif len(cmd)>1 and cmd[1]=='/usr/local/libexec/cognita-prepare-sources':
            r=record()
            for source in r['sources']:
                source['canonical_identity']='fixture-'+source['alias'];source['last_runtime_identity']={'device':1,'inode':2}
            record_path.write_text(json.dumps(r));print('cognita source preparation complete: 2 aliases projection_changed=false')
        elif 'source-identities.json' in cmd[-1]: print('')  # Real all-present contract emits LF only.
        elif 'admin_password_hash' in cmd[-1]: print('set')
        elif 'allow=(' in cmd[-1]: pass
        else: raise AssertionError('Unexpected Python boundary')
    elif cmd[0]=='systemctl': assert cmd[1] in ('start','enable')
    elif cmd[0]=='docker':
        if cmd[1]=='load': state['loaded']=True;save()
        elif cmd[1]=='image':
            r=record()
            if '{{json .RepoDigests}}' in cmd: print(json.dumps(['example/pgvector@sha256:'+'a'*64]))
            elif cmd[-1]==r['image_ref_cognita_cpu']: print(r['image_cognita_cpu']+' '+r['version']+' '+r['commit'])
            else: print(r['image_postgres'])
        elif cmd[1]=='inspect': print(record()['image_cognita_cpu'])
        elif cmd[1]=='compose':
            if 'ps' in cmd: print('app-container')
            elif '--defer-connector-check' in cmd: print('SELFTEST_FIXTURES_READY_CONNECTOR_DEFERRED')
            elif any('exec python /tmp/run-selftest.py' in part for part in cmd):
                assert sys.stdin.buffer.read()==b'synthetic-static-secret\n';print('PASS synthetic native runner')
            elif 'config' in cmd or 'run' in cmd: pass
            else: raise AssertionError('Unexpected compose boundary')
        else: raise AssertionError('Unexpected Docker boundary')
    else: raise AssertionError('Unexpected native boundary')
'''


class OwnedJob:
    """A test process starts suspended and joins its job before any code can run."""
    def __enter__(self):
        class BasicLimit(ctypes.Structure):
            _fields_ = [("process_time", ctypes.c_longlong), ("job_time", ctypes.c_longlong),
                        ("flags", wintypes.DWORD), ("min_working", ctypes.c_size_t),
                        ("max_working", ctypes.c_size_t), ("active_limit", wintypes.DWORD),
                        ("affinity", ctypes.c_size_t), ("priority", wintypes.DWORD),
                        ("scheduling", wintypes.DWORD)]

        class ExtendedLimit(ctypes.Structure):
            _fields_ = [("basic", BasicLimit), ("io", ctypes.c_ulonglong * 6),
                        ("process_memory", ctypes.c_size_t), ("job_memory", ctypes.c_size_t),
                        ("peak_process", ctypes.c_size_t), ("peak_job", ctypes.c_size_t)]

        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel.CreateJobObjectW.restype = wintypes.HANDLE
        self.kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        self.kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                                       ctypes.c_void_p, wintypes.DWORD]
        self.kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        self.kernel.QueryInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                                         ctypes.c_void_p, wintypes.DWORD,
                                                         ctypes.c_void_p]
        self.kernel.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self.handle = self.kernel.CreateJobObjectW(None, None)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            limits = ExtendedLimit()
            limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if not self.kernel.SetInformationJobObject(self.handle, 9, ctypes.byref(limits),
                                                       ctypes.sizeof(limits)):
                raise ctypes.WinError(ctypes.get_last_error())
        except BaseException:
            self.kernel.CloseHandle(self.handle)
            raise
        return self

    def start(self, arguments):
        process = subprocess.Popen(arguments, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   text=True, creationflags=0x4)  # CREATE_SUSPENDED
        try:
            if not self.kernel.AssignProcessToJobObject(self.handle, int(process._handle)):
                raise ctypes.WinError(ctypes.get_last_error())
            resume = ctypes.WinDLL("ntdll").NtResumeProcess
            resume.argtypes = [wintypes.HANDLE]
            if resume(int(process._handle)) != 0:
                raise RuntimeError("Could not resume the owned PowerShell test process")
        except BaseException:
            process.kill()
            process.wait(timeout=10)
            raise
        return process

    def active(self):
        class Accounting(ctypes.Structure):
            _fields_ = [("times", ctypes.c_longlong * 4), ("faults", wintypes.DWORD),
                        ("total", wintypes.DWORD), ("active", wintypes.DWORD),
                        ("terminated", wintypes.DWORD)]
        accounting = Accounting()
        if not self.kernel.QueryInformationJobObject(self.handle, 1, ctypes.byref(accounting),
                                                     ctypes.sizeof(accounting), None):
            raise ctypes.WinError(ctypes.get_last_error())
        return accounting.active

    def __exit__(self, exc_type, exc, traceback):
        remaining = self.active()
        try:
            if remaining:
                if not self.kernel.TerminateJobObject(self.handle, 1):
                    raise ctypes.WinError(ctypes.get_last_error())
                deadline = time.monotonic() + 10
                while self.active() and time.monotonic() < deadline:
                    time.sleep(0.02)
                if self.active():
                    raise AssertionError("Owned test process tree did not terminate")
                if exc is None:
                    raise AssertionError("Test exited with live owned descendants")
        finally:
            self.kernel.CloseHandle(self.handle)


def run_ps(body: str, *, definitions=True, shell=None):
    if os.name != "nt" or not PWSH:
        raise unittest.SkipTest("Windows and PowerShell 7 required for native installer control")
    with tempfile.TemporaryDirectory(prefix="cognita-windows-recovery-control-") as temporary:
        root = Path(temporary)
        source = root / "control-test.ps1"
        content = (production_definitions() if definitions else "") + "\n" + body
        source.write_text(content, encoding="utf-8-sig")
        with OwnedJob() as job:
            process = job.start([shell or PWSH, "-NoProfile", "-NonInteractive", "-File", str(source)])
            try:
                stdout, stderr = process.communicate(timeout=40)
            finally:
                if process.poll() is None:
                    job.kernel.TerminateJobObject(job.handle, 1)
                    process.wait(timeout=10)
            result = subprocess.CompletedProcess(process.args, process.returncode, stdout, stderr)
    if root.exists():
        raise AssertionError(f"Owned control-test directory remains: {root}")
    return result


@unittest.skipUnless(os.name == "nt" and PWSH, "Windows and PowerShell 7 required")
class WindowsRecoveryControlTests(unittest.TestCase):
    def check_ps(self, body, **kwargs):
        result = run_ps(body, **kwargs)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def test_windows_powershell_rejected_before_dispatch(self):
        legacy = shutil.which("powershell.exe")
        if not legacy:
            self.skipTest("Windows PowerShell 5.1 unavailable")
        result = run_ps(f"& {ps_string(INSTALLER)} -Action Status", definitions=False, shell=legacy)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("requires PowerShell 7 or later", result.stderr)

    def test_fresh_start_and_projection_changes(self):
        for changed in (False, True):
            with self.subTest(projection_changed=changed):
                self.check_ps(f"""
$bundleRoot=Join-Path $PSScriptRoot 'accepted-start-bundle';$null=New-Item -ItemType Directory $bundleRoot
$cpuId='sha256:'+('c'*64);$pgId='sha256:'+('d'*64);$pgRef='example/pgvector:pg18@sha256:'+('a'*64)
[IO.File]::WriteAllText((Join-Path $bundleRoot 'compose.yaml'),"services:`n  postgres:`n    image: $pgRef`n")
[IO.File]::WriteAllText((Join-Path $bundleRoot 'release.txt'),"version: 13.5.0`ncommit: $('b'*40)`nimage_ref_cognita_cpu: cognita:cpu`nimage_cognita_cpu: $cpuId`nimage_ref_postgres: $pgRef`nimage_postgres: $pgId`nbundle_mode: core`n")
$sumRows=@('compose.yaml','release.txt')|ForEach-Object {{(Get-FileHash -Algorithm SHA256 (Join-Path $bundleRoot $_)).Hash.ToLowerInvariant()+'  '+$_}}
[IO.File]::WriteAllLines((Join-Path $bundleRoot 'SHA256SUMS'),$sumRows)
$record=[pscustomobject]@{{mode='core';version='13.5.0';commit=('b'*40);mcp_port=10675;image_cognita_cpu=$cpuId;image_ref_cognita_cpu='cognita:cpu';image_ref_postgres=$pgRef;image_postgres=$pgId;bundle_path=$bundleRoot;bundle_sha256=(Get-FileHash -Algorithm SHA256 (Join-Path $bundleRoot 'SHA256SUMS')).Hash.ToLowerInvariant()}}
$script:NativeCalls=[Collections.Generic.List[object]]::new();$script:SystemdReady=$true
$script:TaskState='Ready';$script:Starts=0
function Verify-Owner {{return $record}}
function Get-ScheduledTask {{[pscustomobject]@{{State=$script:TaskState}}}}
function Assert-LauncherTask($TaskObject) {{}}
function Start-ScheduledTask {{$script:Starts++;$script:TaskState='Running'}}
function Start-Sleep {{}}
function Invoke-RestMethod {{[pscustomobject]@{{status='ok';version='13.5.0';workspace=[pscustomobject]@{{mode='core'}}}}}}
function Invoke-OwnedProcess([string]$Exe,[string[]]$CommandArguments,[int]$Timeout,$InputText=$null) {{
    $script:NativeCalls.Add([pscustomobject]@{{Exe=$Exe;Arguments=@($CommandArguments);Timeout=$Timeout}})
    $output=if($CommandArguments -contains 'wslpath'){{'/accepted/path'}}elseif($CommandArguments -contains 'ps'){{'app-container'}}elseif($CommandArguments -contains '{{{{json .RepoDigests}}}}'){{'["example/pgvector@sha256:'+('a'*64)+'"]'}}elseif($CommandArguments -contains 'inspect'){{if($CommandArguments[-1] -ceq 'cognita:cpu'){{"$cpuId 13.5.0 $('b'*40)"}}elseif($CommandArguments[-1] -ceq 'app-container'){{$cpuId}}else{{$pgId}}}}elseif($CommandArguments -contains 'grep -q ''$argon2'''){{'configured'}}elseif(($CommandArguments -join ' ') -match 'grep -q'){{'configured'}}else{{''}}
    return @{{exit_code=0;stdout=$output;stderr=''}}
}}
if($script:ProjectionChanged -or $null -ne $script:AdminPassword -or $null -ne $script:AdminCsrf){{throw 'Fresh action optional state was not initialized'}}
{('$script:ProjectionChanged=$true' if changed else '')}
Do-Start
if($script:ProjectionChanged -or $script:Starts -ne 1 -or $script:TaskState -ne 'Running'){{throw 'Start state transition was incorrect'}}
$recreates=@($script:NativeCalls|Where-Object {{$_.Arguments -contains '--force-recreate'}})
if($recreates.Count -ne {int(changed)}){{throw 'Projection recreation did not follow the changed/unchanged contract'}}
if($script:NativeCalls[0].Arguments -notcontains 'cognita-owned-launch' -and $script:NativeCalls[0].Arguments -notcontains 'timeout'){{throw 'Start WSL operation lacks bounded Linux ownership'}}
""")

    def test_stop_checks_linux_unit_when_task_missing_or_present(self):
        for present in (False, True):
            with self.subTest(task_present=present):
                self.check_ps(f"""
$script:Stopped=$false;$script:Verified=$false;$script:Asserted=$false
function Get-ScheduledTask {{ {("[pscustomobject]@{State='Ready'}" if present else "$null")} }}
function Assert-LauncherTask($TaskObject) {{$script:Asserted=$true}}
function Invoke-OwnedProcess([string]$Exe,[string[]]$CommandArguments,[int]$Timeout,$InputText=$null) {{
    if($CommandArguments -contains 'cognita-stop-owned-units'){{$script:Stopped=$true;$script:Verified=$true;return @{{exit_code=0;stdout='stopped cognita-compose.service';stderr=''}}}}
    throw 'Unexpected stop boundary'
}}
Stop-TaskSession
if(-not $script:Stopped -or -not $script:Verified -or $script:Asserted -ne ${str(present).lower()}){{throw 'Stop skipped unit teardown or owner verification'}}
""")

    def test_stop_refuses_unverified_unit_stop(self):
        self.check_ps("""
function Get-ScheduledTask {$null}
function Invoke-OwnedProcess {return @{exit_code=0;stdout='';stderr=''}}
$rejected=$false
try{Stop-TaskSession}catch{$rejected=$_.Exception.Message -ceq 'Owned unit stop/absence could not be verified: cognita-compose.service.'}
if(-not $rejected){throw 'Stop accepted an active unit'}
""")

    def test_real_admin_session_initializes_and_replaces_csrf(self):
        self.check_ps("""
$script:AdminCredential=[PSCredential]::new('admin',(ConvertTo-SecureString 'synthetic-admin-password' -AsPlainText -Force))
$script:AdminCsrf='stale-session-token';$script:Requests=[Collections.Generic.List[object]]::new()
function Invoke-RestMethod {
    param($Uri,$Method,$WebSession,$TimeoutSec,$ErrorAction,$ContentType,$Body,$Headers)
    $script:Requests.Add([pscustomobject]@{Path=([uri]$Uri).AbsolutePath;Method=$Method;Headers=$Headers})
    if($Uri -like '*/api/session'){return [pscustomobject]@{auth_required=$true}}
    if($Uri -like '*/api/login'){
        $credentials=$Body|ConvertFrom-Json
        if($credentials.username -cne 'admin' -or $credentials.password -cne 'synthetic-admin-password'){throw 'Credentials did not reach login body'}
        $WebSession.Cookies.Add([uri]'http://127.0.0.1:10676/',[Net.Cookie]::new('cognita_csrf','new-session-token','/'))
        return [pscustomobject]@{ok=$true}
    }
    return [pscustomobject]@{ok=$true}
}
$session=Get-AdminSession
$null=Invoke-AdminRequest $session 'GET' '/api/projects'
if($script:Requests[0].Headers -or $script:Requests[1].Headers){throw 'New login reused stale CSRF'}
if($script:Requests[2].Headers['X-CSRF-Token'] -cne 'new-session-token'){throw 'Authenticated API request omitted its new CSRF'}
if($null -ne $script:AdminPassword -or $null -ne $script:AdminUsername){throw 'Plaintext login state remains after login'}
""")

    def test_fresh_complete_install_preserves_production_run_and_wsl_normalization(self):
        with tempfile.TemporaryDirectory(prefix="cognita-windows-native-install-") as temporary:
            root = Path(temporary)
            bundle = root / "bundle"
            bundle.mkdir()
            source = root / "selected-source"
            source.mkdir()
            state = root / "state"
            backend = root / "native-boundary.py"
            backend.write_text(NATIVE_INSTALL_BOUNDARY, encoding="utf-8")
            calls = root / "calls.jsonl"
            external = root / "external.json"
            reference = "example/pgvector:pg18@sha256:" + "a" * 64
            metadata = ("version: 13.5.0\ncommit: " + "b" * 40 +
                        "\nimage_ref_cognita_cpu: cognita:cpu\nimage_cognita_cpu: sha256:" + "c" * 64 +
                        f"\nimage_ref_postgres: {reference}\nimage_postgres: sha256:" + "d" * 64 +
                        "\nbundle_mode: core\nimage_ref_workspace_runtime: \nimage_workspace_runtime: \ntoolbox_version: \n")
            (bundle / "release.txt").write_text(metadata)
            (bundle / "compose.yaml").write_text(f"services:\n  postgres:\n    image: {reference}\n")
            (bundle / "cognita-cpu.tar").write_bytes(b"synthetic accepted archive")
            (bundle / "scripts/windows").mkdir(parents=True)
            shutil.copyfile(REPO / "scripts/windows/Prepare-Sources.py", bundle / "scripts/windows/Prepare-Sources.py")
            accepted = [p for p in bundle.rglob("*") if p.is_file()]
            (bundle / "SHA256SUMS").write_text("".join(hashlib.sha256(p.read_bytes()).hexdigest()+"  "+p.relative_to(bundle).as_posix()+"\n" for p in accepted))
            api = self.selftest_harness("pass", "core").split("function Invoke-OwnedProcess", 1)[0]
            api = api.replace("        default {throw", "        '/healthz' {return [pscustomobject]@{status='ok';version='13.5.0';workspace=[pscustomobject]@{mode='core'}}}\n        default {throw")
            body = api + f"""
$script:SystemdReady=$false
$Mode='Core';$BundlePath={ps_string(bundle)};$Source=@('manuals='+{ps_string(source)});$SmbSource=@()
$script:Root={ps_string(state)};$script:RecordPath=Join-Path $script:Root 'install.json'
$script:TaskObject=$null
function Get-NetTCPConnection {{}}
function Get-ScheduledTask {{$script:TaskObject}}
function New-ScheduledTaskAction {{param($Execute,$Argument);[pscustomobject]@{{Execute=$Execute;Arguments=$Argument}}}}
function New-ScheduledTaskTrigger {{param([switch]$AtLogOn,$User);[pscustomobject]@{{User=$User}}}}
function New-ScheduledTaskSettingsSet {{param($ExecutionTimeLimit,$MultipleInstances,[switch]$AllowStartIfOnBatteries,[switch]$DontStopIfGoingOnBatteries);[pscustomobject]@{{}}}}
function New-ScheduledTaskPrincipal {{param($UserId,$LogonType,$RunLevel);[pscustomobject]@{{UserId=$UserId;LogonType=$LogonType}}}}
function Register-ScheduledTask {{param($TaskName,$Action,$Trigger,$Settings,$Principal,[switch]$Force);$script:TaskObject=[pscustomobject]@{{State='Ready';Actions=@($Action);Principal=$Principal}}}}
function Start-ScheduledTask {{$script:TaskObject.State='Running'}}
$script:NativeProcess=${{function:Invoke-OwnedProcess}}
function Invoke-OwnedProcess {{
 param([string]$Exe,[string[]]$CommandArguments,[int]$Timeout,$InputText=$null,[scriptblock]$OnInterrupted=$null,[switch]$AwaitBoundedCompletion,[scriptblock]$OnStarted=$null)
 $nativeArgs=@({ps_string(backend)},{ps_string(external)},$script:RecordPath,{ps_string(calls)},$Exe)+$CommandArguments
 if($PSBoundParameters.ContainsKey('InputText')){{return & $script:NativeProcess {ps_string(sys.executable)} $nativeArgs $Timeout $InputText -OnStarted $OnStarted}}
 return & $script:NativeProcess {ps_string(sys.executable)} $nativeArgs $Timeout -OnStarted $OnStarted
}}
Do-Install
$r=Read-Record
if($script:TaskObject.State -cne 'Running' -or $script:KeyActive -or $script:Connector -or -not $script:Revoked -or -not $script:Deleted){{throw 'Fresh installation did not complete startup and verified Self-Test cleanup'}}
if(-not $r.sources[0].canonical_identity -or -not $r.sources[1].canonical_identity -or $r.mode -cne 'core'){{throw 'Fresh source identities were not recorded'}}
if($null -ne $script:AdminPassword -or $null -ne $script:AdminCsrf){{throw 'Fresh action retained secret session state'}}
"""
            self.assertNotIn("function Run(", body)
            self.assertNotIn("function Wsl(", body)
            result = self.check_ps(body)
            self.assertNotIn("synthetic-static-secret", result.stdout + result.stderr)
            observed = [json.loads(line) for line in calls.read_text().splitlines()]
            self.assertTrue(any("source-identities.json" in " ".join(call) for call in observed))
            self.assertTrue(any("cognita-owned-launch" in call and "load" in call for call in observed))
            self.assertTrue(any("cognita-owned-cleanup" in call for call in observed))
            self.assertTrue(json.loads(external.read_text())["loaded"])
        self.assertFalse(root.exists())

    def test_recreated_identity_is_durable_before_marker_publication_and_fresh_retry(self):
        with tempfile.TemporaryDirectory(prefix="cognita-windows-recreated-identity-") as temporary:
            root = Path(temporary)
            backend = root / "native.py"
            backend.write_text(NATIVE_INSTALL_BOUNDARY)
            external = root / "external.json"
            external.write_text(json.dumps(dict(exists=False,marker="",fail_after_marker=True)))
            calls = root / "calls.jsonl"
            common = f"""
$script:Root={ps_string(root / 'state')};$script:RecordPath=Join-Path $script:Root 'install.json'
$bundle=[pscustomobject]@{{Root={ps_string(root)};Sum='fixture';Metadata=@{{version='13.5.0';commit=('b'*40);bundle_mode='core';image_ref_cognita_cpu='cognita:cpu';image_cognita_cpu=('sha256:'+('c'*64));image_ref_postgres=('example/pgvector:pg18@sha256:'+('a'*64));image_postgres=('sha256:'+('d'*64));image_ref_workspace_runtime='';image_workspace_runtime='';toolbox_version=''}}}}
$script:NativeProcess=${{function:Invoke-OwnedProcess}}
function Invoke-OwnedProcess {{
 param([string]$Exe,[string[]]$CommandArguments,[int]$Timeout,$InputText=$null,[scriptblock]$OnInterrupted=$null,[switch]$AwaitBoundedCompletion,[scriptblock]$OnStarted=$null)
 return & $script:NativeProcess {ps_string(sys.executable)} (@({ps_string(backend)},{ps_string(external)},$script:RecordPath,{ps_string(calls)},$Exe)+$CommandArguments) $Timeout -OnStarted $OnStarted
}}
"""
            self.check_ps(common + """
$rows=@([pscustomobject]@{alias='manuals';source_kind='ntfs';locator='X:synthetic';canonical_identity='external-volume';last_runtime_identity=@{device=20;inode=30}},[pscustomobject]@{alias='cognita-self-test';source_kind='installation_ext4';locator='/srv/cognita/sources/cognita-self-test';canonical_identity='destroyed-ext4';last_runtime_identity=@{device=40;inode=50}})
$r=New-Record $bundle $rows;Save-Record $r
$interrupted=$false
try{Install-FirstRun $bundle $r}catch{$interrupted=$_.Exception.Message -like 'Command failed (17):*'}
if(-not $interrupted){throw 'Between-durable-step interruption was not reached'}
$saved=Read-Record
if($null -ne $saved.sources[1].canonical_identity -or $null -ne $saved.sources[1].last_runtime_identity -or $saved.sources[0].canonical_identity -cne 'external-volume' -or $saved.sources[0].last_runtime_identity.device -ne 20){throw 'Identity invalidation was not durable before marker publication'}
""")
            self.check_ps(common + """
$r=Read-Record
if(Install-FirstRun $bundle $r){throw 'Retry recreated an already marked distribution'}
$saved=Read-Record
if($null -ne $saved.sources[1].canonical_identity -or $saved.sources[0].canonical_identity -cne 'external-volume'){throw 'Fresh retry resurrected destroyed identity or cleared external ownership'}
""")
            observed = [json.loads(line) for line in calls.read_text().splitlines()]
            self.assertEqual(sum(call[0] == "--install" for call in observed), 1)
            self.assertTrue(json.loads(external.read_text())["bootstrap"])
        self.assertFalse(root.exists())

    def test_selftest_revokes_key_and_deletes_connector_on_success_and_failure(self):
        for mode in ("core", "full"):
            for outcome in ("pass", "runner_failure", "process_exception", "readback_failure", "no_key",
                            "revoke_failure", "delete_failure"):
                with self.subTest(mode=mode, outcome=outcome):
                    result = self.check_ps(self.selftest_harness(outcome, mode))
                    self.assertNotIn("synthetic-static-secret", result.stdout + result.stderr)

    @staticmethod
    def selftest_harness(outcome, mode):
        return r"""
$script:Outcome=OUTCOME;$script:Mode=MODE;$script:SystemdReady=$true
$script:AdminCredential=[PSCredential]::new('admin',(ConvertTo-SecureString 'synthetic-password' -AsPlainText -Force))
$script:Connector=$null;$script:KeyActive=$false;$script:KeyWasGenerated=$false
$script:Revoked=$false;$script:Deleted=$false;$script:SecretReceived=$false;$script:ContainerCleanups=0
function Invoke-RestMethod {
    param($Uri,$Method,$WebSession,$TimeoutSec,$ErrorAction,$ContentType,$Body,$Headers)
    $path=([uri]$Uri).AbsolutePath;$bodyObject=if($Body){$Body|ConvertFrom-Json}else{$null}
    switch($path){
        '/api/session' {return [pscustomobject]@{auth_required=$true}}
        '/api/login' {$WebSession.Cookies.Add([uri]'http://127.0.0.1:10676/',[Net.Cookie]::new('cognita_csrf','csrf','/'));return @{ok=$true}}
        '/api/projects' {return [pscustomobject]@{projects=@([pscustomobject]@{name='Self-Test';documents_dir='/srv/cognita/sources/cognita-self-test';enabled=$true;writable=$true})}}
        '/api/connectors' {
            if($Method -ceq 'POST'){
                $script:Connector=[pscustomobject]@{id='synthetic-connector';slug='self-test';name='Self-Test';enabled=($script:Outcome -cne 'readback_failure');project_mode='selected';project_access=[pscustomobject]@{'Self-Test'='write'};workspace_enabled=($script:Mode -ceq 'full')}
                return [pscustomobject]@{connector=$script:Connector}
            }
            return [pscustomobject]@{revision=7;connectors=@($script:Connector|Where-Object {$null -ne $_})}
        }
        '/api/connectors/synthetic-connector' {
            if($Method -cne 'DELETE' -or ([uri]$Uri).Query -cne '?expected_revision=7'){throw 'Wrong temporary connector deletion request'}
            if($script:Outcome -cne 'delete_failure'){$script:Connector=$null};$script:Deleted=$true;return @{ok=$true}
        }
        '/api/authentication' {return [pscustomobject]@{revision=11;projects=@([pscustomobject]@{name='Self-Test';static_key_override=$script:KeyActive})}}
        '/api/authentication/projects/Self-Test/static-key/generate' {
            $script:KeyActive=$true;$script:KeyWasGenerated=$true
            return [pscustomobject]@{generated_key=$(if($script:Outcome -ceq 'no_key'){''}else{'synthetic-static-secret'})}
        }
        '/api/authentication/projects/Self-Test/static-key/revoke' {
            if(-not $bodyObject.confirm_lockout -or $bodyObject.expected_revision -ne 11){throw 'Incorrect key revoke revision/confirmation'}
            if($script:Outcome -cne 'revoke_failure'){$script:KeyActive=$false};$script:Revoked=$true;return @{ok=$true}
        }
        default {throw "Unexpected Admin boundary: $Method $path"}
    }
}
function Invoke-OwnedProcess([string]$Exe,[string[]]$CommandArguments,[int]$Timeout,$InputText=$null) {
    if($CommandArguments -contains 'cognita-owned-cleanup'){return @{exit_code=0;stdout='';stderr=''}}
    if($CommandArguments -contains 'stat'){return @{exit_code=0;stdout='1:2';stderr=''}}
    if($CommandArguments -contains '--defer-connector-check'){return @{exit_code=0;stdout='SELFTEST_FIXTURES_READY_CONNECTOR_DEFERRED';stderr=''}}
    if(($CommandArguments -join ' ') -like '*docker container rm --force*'){$script:ContainerCleanups++;return @{exit_code=0;stdout='';stderr=''}}
    if(($CommandArguments -join ' ') -like '*exec python /tmp/run-selftest.py*'){
        if($InputText -cne 'synthetic-static-secret'){throw 'Raw key did not reach bounded stdin'}
        if(($CommandArguments -join ' ') -like '*synthetic-static-secret*' -or (Test-Path Env:COGNITA_TEST_API_KEY)){throw 'Raw key leaked into arguments/environment'}
        if($CommandArguments -notcontains '--name' -or $CommandArguments -notcontains 'cognita-owned-launch'){throw 'Secret runner lacks bounded ownership'}
        $script:SecretReceived=$true
        if($script:Outcome -ceq 'process_exception'){throw 'Synthetic process exception'}
        return @{exit_code=$(if($script:Outcome -ceq 'runner_failure'){17}else{0});stdout="PASS synthetic`n";stderr=''}
    }
    if($CommandArguments -contains 'python3'){return @{exit_code=0;stdout='';stderr=''}}
    throw 'Unexpected WSL process boundary'
}
$failed=$false;$failureMessage=''
try{Invoke-SelfTest ([pscustomobject]@{mode=$script:Mode})}catch{$failed=$true;$failureMessage=$_.Exception.Message}
if($failed -ne ($script:Outcome -cne 'pass')){throw 'Self-Test did not preserve the runner/read-back outcome'}
if($script:Outcome -in @('revoke_failure','delete_failure')){
    $expected=if($script:Outcome -ceq 'revoke_failure'){'project-scoped Self-Test static key could not be revoked and verified'}else{'temporary Self-Test connector could not be removed and verified'}
    if($failureMessage -cne ('Self-Test cleanup incomplete: '+$expected)){throw 'Unverified cleanup did not fail closed with resource-specific evidence'}
    if($null -ne $script:AdminPassword -or $null -ne $script:AdminCsrf){throw 'Cleanup failure retained session secrets'}
    return
}
if($script:Connector -or -not $script:Deleted -or $script:KeyActive){throw 'Temporary connector or generated key remains'}
if($script:KeyWasGenerated -and -not $script:Revoked){throw 'Generated key was not revoked'}
if($script:Outcome -in @('pass','runner_failure','process_exception') -and (-not $script:SecretReceived -or $script:ContainerCleanups -ne 2)){throw 'Secret/container lifecycle did not execute'}
if($null -ne $script:AdminPassword -or $null -ne $script:AdminCsrf){throw 'Self-Test left session secrets in optional state'}
""".replace("OUTCOME", ps_string(outcome)).replace("MODE", ps_string(mode))

    def test_native_process_timeout_stops_parent_and_descendant(self):
        self.check_native_process_cleanup(canceled=False)

    def test_native_process_cancellation_stops_parent_and_descendant(self):
        self.check_native_process_cleanup(canceled=True)

    def check_native_process_cleanup(self, canceled):
        with tempfile.TemporaryDirectory(prefix="cognita-windows-recovery-process-") as temporary:
            root = Path(temporary)
            child = root / "child.py"
            parent = root / "parent.py"
            ready = root / "ready.json"
            child.write_text("import time\ntime.sleep(120)\n", encoding="utf-8")
            parent.write_text(
                "import json, os, subprocess, sys, time\n"
                "child = subprocess.Popen([sys.executable, sys.argv[1]])\n"
                "open(sys.argv[2], 'w').write(json.dumps({'parent': os.getpid(), 'child': child.pid}))\n"
                "time.sleep(120)\n", encoding="utf-8",
            )
            # The completion boundary waits for explicit readiness and then forces
            # the real timeout cleanup; no wall-clock startup race or soak is used.
            self.check_ps(f"""
$script:Ready={ps_string(ready)}
function Wait-OwnedProcess([Diagnostics.Process]$Process,[int]$Timeout) {{
    $deadline=[DateTime]::UtcNow.AddSeconds(10)
    while(-not (Test-Path -LiteralPath $script:Ready)){{if([DateTime]::UtcNow -gt $deadline){{throw 'Native descendant did not signal readiness'}};Start-Sleep -Milliseconds 10}}
    {("throw [OperationCanceledException]::new('Synthetic cancellation')" if canceled else "return $false")}
}}
$timedOut=$false
try{{$null=Run {ps_string(sys.executable)} @({ps_string(parent)},{ps_string(child)},{ps_string(ready)}) 19}}
catch{{$timedOut=$_.Exception.Message -like {ps_string('Synthetic cancellation' if canceled else 'Command timed out after 19 seconds:*')}}}
if(-not $timedOut){{throw 'Run did not enforce its timeout outcome'}}
$ids=Get-Content -Raw -LiteralPath $script:Ready|ConvertFrom-Json
foreach($processId in @($ids.parent,$ids.child)){{if(Get-Process -Id $processId -ErrorAction SilentlyContinue){{throw 'Owned native command process remains after timeout'}}}}
""")
            self.assertTrue(ready.exists(), "Native child readiness path was not executed")
        self.assertFalse(root.exists(), f"Owned native-process fixture remains: {root}")

    def test_secret_process_exception_removes_exact_container(self):
        self.check_ps("""
$script:ArgumentsSeen=$null;$script:CleanupName=$null
function Invoke-OwnedProcess([string]$Exe,[string[]]$CommandArguments,[int]$Timeout,$InputText=$null) {
    $script:ArgumentsSeen=@($CommandArguments)
    if($InputText -cne 'synthetic-secret'){throw 'Secret stdin was not forwarded'}
    throw 'Synthetic canceled operation'
}
function Remove-OwnedCommandContainer([string]$Name){$script:CleanupName=$Name}
$failed=$false
try{Invoke-WslWithSecret @('-d','Cognita-Windows','-u','root','--exec','docker','compose','run','--rm','cognita') 'synthetic-secret' 23}catch{$failed=$_.Exception.Message -ceq 'Synthetic canceled operation'}
if(-not $failed -or $script:CleanupName -notmatch '^cognita-windows-operation-[0-9a-f]{32}$'){throw 'Canceled secret process skipped exact container cleanup'}
if($script:ArgumentsSeen -contains 'synthetic-secret' -or $script:ArgumentsSeen -notcontains '23s' -or $script:ArgumentsSeen -notcontains $script:CleanupName){throw 'Secret operation lost bounded ownership or exposed its key'}
""")

    def test_native_secret_reaches_stdin_only(self):
        digest = hashlib.sha256(b"synthetic-secret\n").hexdigest()
        self.check_ps(f"""
$code='import hashlib,os,sys; raw=sys.stdin.buffer.read(); assert hashlib.sha256(raw).hexdigest() == "{digest}"; v=raw[:-1].decode(); assert all(v not in x for x in sys.argv); assert all(v not in x for x in os.environ.values()); print("PASS exact LF stdin")'
$result=Invoke-OwnedProcess {ps_string(sys.executable)} @('-c',$code) 30 'synthetic-secret'
if($result.exit_code -ne 0 -or $result.stdout.Trim() -cne 'PASS exact LF stdin' -or $result.stderr){{throw 'Owned process did not keep exact LF stdin private'}}
""")

    def test_duplicate_native_resolution_keeps_first_match_and_real_runner(self):
        self.check_ps(f"""
function Get-Command {{
    param($Name,$CommandType,$ErrorAction)
    if($Name -ceq 'wsl.exe'){{return @([pscustomobject]@{{Source={ps_string(sys.executable)}}},[pscustomobject]@{{Source='B:\\nonexistent-second-match.exe'}})}}
    Microsoft.PowerShell.Core\\Get-Command $Name -CommandType $CommandType -ErrorAction $ErrorAction
}}
$output=Read-RequiredLastLine -Lines (Run 'wsl.exe' @('-c','print("native-first-match")') 30) -Operation 'Native duplicate-resolution regression'
if($output -cne 'native-first-match'){{throw 'Native application matches were concatenated or reordered'}}
""")

    def test_actual_read_only_wsl_listing_uses_real_runner(self):
        result = self.check_ps("""
$names=@(Run 'wsl.exe' @('--list','--quiet') 30|ForEach-Object {($_ -replace "`0",'').Trim()})
if(-not $names.Count){throw 'Read-only WSL listing returned no installed distributions'}
Write-Host "Read-only WSL listing completed: $($names.Count) names"
""")
        self.assertIn("Read-only WSL listing completed:", result.stdout)

    def test_timeout_seconds_reach_native_wait_boundary(self):
        self.check_ps("""
Add-Type -TypeDefinition 'public class ControlledWait : System.Diagnostics.Process { public int ObservedMilliseconds; public new bool WaitForExit(int milliseconds) { ObservedMilliseconds=milliseconds; return false; } }'
$controlled=[ControlledWait]::new()
try {
    if(Wait-OwnedProcess $controlled 37){throw 'Native wait result was changed'}
    if($controlled.ObservedMilliseconds -ne 37000){throw 'Timeout seconds were not forwarded as native milliseconds'}
} finally {$controlled.Dispose()}
""")


@unittest.skipUnless(sys.platform == "linux", "requires actual Linux systemd user-manager/cgroup proof on the host")
class LinuxOwnedCommandTests(unittest.TestCase):
    def test_systemd_success_timeout_cancel_and_registration_race(self):
        unavailable = native_systemd_unavailability()
        if unavailable:
            self.skipTest(unavailable)
        source = INSTALLER.read_text(encoding="utf-8")
        launch = re.search(r"function Get-OwnedWslLaunchScript.*?return @'\n(.*?)\n'@", source, re.S).group(1)
        cleanup = re.search(r"function Get-OwnedWslCleanupScript.*?return @'\n(.*?)\n'@", source, re.S).group(1)
        # Use the real user manager's cgroups for synthetic work. Production uses
        # the dedicated distro's root manager; all properties/scripts stay intact.
        launch = launch.replace("systemd-run ", "systemd-run --user ")
        cleanup = cleanup.replace("systemctl ", "systemctl --user ")
        for outcome in ("success", "timeout", "cancel", "before_registration", "registration_race"):
            with self.subTest(outcome=outcome):
                with tempfile.TemporaryDirectory(prefix="cognita-linux-control-") as temporary:
                    root = Path(temporary)
                    guard = root / "guard"
                    ready = root / "ready.json"
                    worked = root / "worked"
                    unit = "cognita-windows-command-" + uuid.uuid4().hex + ".service"
                    print(f"Owned Linux fixture: {outcome} unit={unit} root={root}", flush=True)
                    group = None
                    process = None
                    cleaner = None
                    try:
                        if outcome == "before_registration":
                            guard.mkdir()
                            (guard / "cancel").write_text("cancel")
                        if outcome == "success":
                            code = ("import hashlib,os,subprocess,sys; raw=sys.stdin.buffer.read(); "
                                    "assert raw == b'synthetic-key\\n'; "
                                    "assert sys.argv[1] == '$literal ${HOME} $$'; "
                                    "assert all('synthetic-key' not in v for v in os.environ.values()); "
                                    "subprocess.run(['bash','-c','IFS= read -r key; test \"$key\" = synthetic-key'],input=raw,check=True); "
                                    "print('PASS byte stdin and Linux read-r')")
                            command = [sys.executable, "-c", code, "$literal ${HOME} $$"]
                        else:
                            code = ("import json,os,pathlib,subprocess,sys,time; "
                                    "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(120)']); "
                                    "pathlib.Path(sys.argv[1]).write_text(json.dumps({'parent':os.getpid(),'child':child.pid})); "
                                    "pathlib.Path(sys.argv[2]).write_text('actual work'); time.sleep(120)")
                            command = [sys.executable, "-c", code, str(ready), str(worked)]
                        current_launch = launch
                        if outcome == "registration_race":
                            # Hold the real submission at an explicit signal; cleanup
                            # begins before release, then a late real ExecStart runs.
                            gate = root / "gate"
                            submitted = root / "submitted"
                            current_launch = (f"submit() {{ touch '{submitted}'; while test ! -e '{gate}'; do sleep .01; done; command systemd-run \"$@\"; }}\n" +
                                              launch.replace("systemd-run --user", "submit --user"))
                        process = subprocess.Popen(["bash", "-c", current_launch, "cognita-owned-launch", str(guard), unit,
                                                    "1" if outcome == "timeout" else "120", *command],
                                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                        if outcome in ("cancel", "timeout"):
                            deadline = time.monotonic() + 10
                            while not ready.exists() and time.monotonic() < deadline:
                                if process.poll() is not None:
                                    break
                                time.sleep(.01)
                            self.assertTrue(ready.exists(), "Synthetic Linux work did not signal readiness")
                            group = (guard / "cgroup").read_text().strip()
                        if outcome == "cancel":
                            subprocess.run(["bash", "-c", cleanup, "cognita-owned-cleanup", str(guard), unit],
                                           check=True, capture_output=True, timeout=55)
                            output, errors = process.communicate(timeout=10)
                        elif outcome == "registration_race":
                            deadline = time.monotonic() + 10
                            while not submitted.exists() and time.monotonic() < deadline:
                                time.sleep(.01)
                            self.assertTrue(submitted.exists())
                            cleaner = subprocess.Popen(["bash", "-c", cleanup, "cognita-owned-cleanup", str(guard), unit],
                                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                            while not (guard / "cancel").exists() and time.monotonic() < deadline:
                                time.sleep(.01)
                            self.assertTrue((guard / "cancel").exists())
                            gate.touch()
                            clean_output, clean_errors = cleaner.communicate(timeout=55)
                            self.assertEqual(cleaner.returncode, 0, clean_output + clean_errors)
                            output, errors = process.communicate(timeout=10)
                            self.assertFalse(worked.exists(), "Canceled registration started actual work")
                        else:
                            output, errors = process.communicate(input=b"synthetic-key\n" if outcome == "success" else b"", timeout=15)
                            if (guard / "cgroup").exists():
                                group = (guard / "cgroup").read_text().strip()
                            subprocess.run(["bash", "-c", cleanup, "cognita-owned-cleanup", str(guard), unit],
                                           check=True, capture_output=True, timeout=55)
                        if outcome == "success":
                            self.assertEqual(process.returncode, 0, output + errors)
                            self.assertEqual(errors, b"", "Manager status polluted command stderr")
                            self.assertIn(b"PASS byte stdin and Linux read-r", output)
                        elif outcome == "before_registration":
                            self.assertFalse(worked.exists())
                        elif outcome != "cancel":
                            self.assertNotEqual(process.returncode, 0)
                        self.assertFalse(guard.exists(), "Linux ownership guard remains after verified teardown")
                        if ready.exists():
                            for pid in json.loads(ready.read_text()).values():
                                self.assertFalse(Path(f"/proc/{pid}").exists(), "Owned Linux descendant remains")
                        state = subprocess.run(["systemctl", "--user", "show", "--property=LoadState", "--value", unit],
                                               capture_output=True, text=True, timeout=10).stdout.strip()
                        self.assertEqual(state, "not-found")
                        if group:
                            processes = Path("/sys/fs/cgroup" + group + "/cgroup.procs")
                            self.assertTrue(not processes.exists() or not processes.read_text().strip())
                    finally:
                        # Backstop owns only this recorded unit and submitting clients.
                        subprocess.run(["systemctl", "--user", "stop", unit], capture_output=True, timeout=15)
                        for owned in (cleaner, process):
                            if owned is not None:
                                if owned.poll() is None:
                                    owned.kill()
                                owned.communicate(timeout=10)
                        state = subprocess.run(["systemctl", "--user", "show", "--property=LoadState", "--value", unit],
                                               capture_output=True, text=True, timeout=10).stdout.strip()
                        self.assertEqual(state, "not-found", "Task-owned transient unit remains")
                self.assertFalse(root.exists(), "Owned Linux fixture root remains")


if __name__ == "__main__":
    unittest.main()
