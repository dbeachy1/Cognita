# test_distro.ps1 - WSL install, ownership, image import, default distro, keepalive, roots, state (design 5.4-5.8, 14.2)
. (Join-Path $PSScriptRoot '..\CognitaWin.ps1') -NoMain
. (Join-Path $PSScriptRoot '_harness.ps1')

# The real task functions, kept before any test replaces them with the harness fakes (Install-DefaultFakes).
$script:RealRegisterCognitaTask = (Get-Command Register-CognitaTask -CommandType Function).ScriptBlock
$script:RealStartCognitaTask = (Get-Command Start-CognitaTask -CommandType Function).ScriptBlock

function Set-Distro {
    param([string]$BasePath, [string]$Name = 'Cognita', [string]$Guid = '{11111111-1111-1111-1111-111111111111}')
    $script:LxssDistros = @([pscustomobject]@{ Guid = $Guid; Name = $Name; BasePath = $BasePath })
}
function Set-MarkerFake {
    param([string]$Id = '', [string]$Kind = 'id')
    if ($Kind -eq 'id') { Add-ExtRule 'cat /etc/cognita-distro' (New-ExtResult -Stdout ('{"installation_id": "' + $Id + '"}')) }
    elseif ($Kind -eq 'absent') { Add-ExtRule 'cat /etc/cognita-distro' (New-ExtResult -ExitCode 1 -Stderr 'cat: /etc/cognita-distro: No such file or directory') }
    else { Add-ExtRule 'cat /etc/cognita-distro' (New-ExtResult -ExitCode -1 -Stderr 'Wsl/Service/CreateInstance/HCS/HCS_E_SERVICE_NOT_AVAILABLE') }
}

# ---- ownership (design 5.7 step 4) -----------------------------------------------------
Test-Case 'ownership: no distro named Cognita' {
    $s = New-TestSettings
    $o = Get-DistroOwnership -Settings $s
    Assert-False $o.Exists 'not there'
    Assert-Equal 0 (Get-ExtCallLines).Count 'nothing was run'
}

Test-Case 'ownership: interrupted import (pending record, matching BasePath, no marker) is ours' {
    $vhd = Join-Path $script:TestDir 'vhd'
    $s = New-TestSettings -State 'import-pending' -Vhd $vhd
    Set-Distro -BasePath $vhd
    Set-MarkerFake -Kind 'absent'
    $o = Get-DistroOwnership -Settings $s
    Assert-True ($o.Exists -and $o.Owned) 'ours'
    Assert-Match $o.Reason 'interrupted import' 'reason'
}

Test-Case 'ownership: marker equal to installation_id is ours; a different id is foreign' {
    $vhd = Join-Path $script:TestDir 'vhd'
    $s = New-TestSettings -State 'installed' -Vhd $vhd
    Set-Distro -BasePath $vhd
    Set-MarkerFake -Id $s.installation_id
    Assert-True (Get-DistroOwnership -Settings $s).Owned 'marker matches'
    $script:ExtRules.Clear()
    Set-MarkerFake -Id '00000000-0000-0000-0000-000000000000'
    $o = Get-DistroOwnership -Settings $s
    Assert-True ($o.Exists -and -not $o.Owned) 'marker mismatch is foreign'
    Assert-Match $o.Reason 'different install' 'reason'
}

Test-Case 'ownership: BasePath that is not vhd_dir is foreign even with a matching marker' {
    $s = New-TestSettings -State 'installed'
    Set-Distro -BasePath 'C:\Elsewhere\wsl'
    Set-MarkerFake -Id $s.installation_id
    $o = Get-DistroOwnership -Settings $s
    Assert-False $o.Owned 'foreign'
    Assert-Equal 0 (Get-ExtCallLines).Count 'and the distro was never even started to look'
}

Test-Case 'ownership: no record on this PC is foreign; installed record with no marker is foreign; unreadable marker with matching BasePath is ours' {
    Set-Distro -BasePath 'C:\x'
    Assert-False (Get-DistroOwnership -Settings $null).Owned 'no record'
    $vhd = Join-Path $script:TestDir 'vhd'
    $s = New-TestSettings -State 'installed' -Vhd $vhd
    Set-Distro -BasePath $vhd
    Set-MarkerFake -Kind 'absent'
    Assert-False (Get-DistroOwnership -Settings $s).Owned 'installed but no marker'
    $script:ExtRules.Clear()
    Set-MarkerFake -Kind 'unreadable'
    Assert-True (Get-DistroOwnership -Settings $s).Owned 'unreadable marker, disk folder matches'
}

Test-Case 'ownership: -SkipMarker never starts the distro' {
    $vhd = Join-Path $script:TestDir 'vhd'
    $s = New-TestSettings -State 'installed' -Vhd $vhd
    Set-Distro -BasePath $vhd
    $o = Get-DistroOwnership -Settings $s -SkipMarker
    Assert-True $o.Owned 'BasePath decides'
    Assert-Equal 0 (Get-ExtCallLines).Count 'no wsl call'
}

# ---- wsl-install (design 5.4) ---------------------------------------------------------------
Test-Case 'wsl-install: missing WSL runs "wsl --install --no-distribution" plainly and reports restart-required when RebootPending appears' {
    $script:installed = $false
    Add-ExtRule 'wsl\.exe --version' { param($c) if ($script:installed) { New-ExtResult -Stdout 'WSL version: 2.7.14.0' } else { New-ExtResult -ExitCode 1 -Stdout 'not installed' } }
    Add-ExtRule 'wsl\.exe --status' (New-ExtResult -ExitCode 1)
    Add-ExtRule 'wsl\.exe --install --no-distribution' { param($c) $script:installed = $true; $script:RestartPending = $true; New-ExtResult -ExitCode 0 }
    $r = Invoke-WslInstallVerb -Opts @{}
    Assert-Equal 'restart-required' $r.Status 'status'
    Assert-Equal 1 $r.Values['restart'] 'restart=1'
    Assert-Equal 3010 (Get-ExitCodeForStatus $r.Status) 'exit 3010'
    $call = (Get-ExtCallsMatching '--install')[0]
    Assert-Equal 'wsl.exe' (Split-Path -Leaf $call.FilePath) 'started directly, not through Start-Process -Verb RunAs'
    Assert-Equal '--install|--no-distribution' ($call.Arguments -join '|') 'exactly these arguments, no --distribution, no .wslconfig'
    # P1: with its output captured the inbox stub only prints "not installed"; it needs a window.
    Assert-Equal $true $call.OwnConsole 'started in its own console window, output not captured'
}

Test-Case 'wsl-install: exit 0 with no RebootPending and WSL working is ok (restart=0)' {
    $script:installed = $false
    Add-ExtRule 'wsl\.exe --version' { param($c) if ($script:installed) { New-ExtResult -Stdout 'WSL version: 2.7.14.0' } else { New-ExtResult -ExitCode 1 } }
    Add-ExtRule 'wsl\.exe --status' (New-ExtResult -ExitCode 1)
    Add-ExtRule 'wsl\.exe --install' { param($c) $script:installed = $true; New-ExtResult -ExitCode 0 }
    $r = Invoke-WslInstallVerb -Opts @{}
    Assert-Equal 'ok' $r.Status 'ok'
    Assert-Equal 0 $r.Values['restart'] 'restart=0'
}

Test-Case 'wsl-install: belt and braces, exit 0 but WSL still not usable means restart required' {
    Add-ExtRule 'wsl\.exe --version' (New-ExtResult -ExitCode 1)
    Add-ExtRule 'wsl\.exe --status' (New-ExtResult -ExitCode 1)
    Add-ExtRule 'wsl\.exe --install' (New-ExtResult -ExitCode 0)
    Assert-Equal 'restart-required' (Invoke-WslInstallVerb -Opts @{}).Status 'restart'
}

Test-Case 'wsl-install: declining the UAC prompt gives the design message and nothing else changes' {
    Add-ExtRule 'wsl\.exe --version' (New-ExtResult -ExitCode 1)
    Add-ExtRule 'wsl\.exe --status' (New-ExtResult -ExitCode 1)
    Add-ExtRule 'wsl\.exe --install' (New-ExtResult -ExitCode 1 -Stdout 'The operation was canceled by the user. Error code: ERROR_CANCELLED')
    $r = Invoke-WslInstallVerb -Opts @{}
    Assert-Equal 'failed' $r.Status 'failed'
    Assert-Equal 'uac-declined' $r.Values['reason'] 'reason'
    $p = Get-ProgressObjects
    $f = @($p | Where-Object { $_.state -eq 'failed' })[0]
    Assert-Equal 'Setup needs your permission once to turn on WSL.' $f.message 'message'
    Assert-Equal 'Run Setup again when you are ready.' $f.fix 'fix'
}

Test-Case 'wsl-install (P3 on the VM): a bare exit 1 with nothing captured (what a UAC "No" looks like from its own console window) says how to recover' {
    Add-ExtRule 'wsl\.exe --version' (New-ExtResult -ExitCode 1)
    Add-ExtRule 'wsl\.exe --status' (New-ExtResult -ExitCode 1)
    Add-ExtRule 'wsl\.exe --install' (New-ExtResult -ExitCode 1)
    $r = Invoke-WslInstallVerb -Opts @{}
    Assert-Equal 'failed' $r.Status 'failed'
    $f = @((Get-ProgressObjects) | Where-Object { $_.state -eq 'failed' })[0]
    Assert-Equal 'WSL was not turned on.' $f.message 'plain message, not an exit code'
    Assert-Match $f.fix 'If Windows asked for permission and you chose No, press Turn on WSL again and choose Yes\.' 'the likely cause and its fix come first'
    Assert-Match $f.fix 'report the problem with the diagnostics file \(wsl --install exit 1\)' 'the exit code is still there for support'
    Assert-Match (Get-LogText) 'wsl-install: wsl --install exit 1 after \d+ ms' 'logged with its duration'
}

Test-Case 'wsl-install: old WSL runs "wsl --update" first' {
    $script:updated = $false
    Add-ExtRule 'wsl\.exe --version' { param($c) if ($script:updated) { New-ExtResult -Stdout 'WSL version: 2.7.14.0' } else { New-ExtResult -Stdout 'WSL version: 1.2.5.0' } }
    Add-ExtRule 'wsl\.exe --update' { param($c) $script:updated = $true; New-ExtResult }
    $r = Invoke-WslInstallVerb -Opts @{}
    Assert-Equal 'ok' $r.Status 'ok'
    Assert-Equal 1 (Get-ExtCallsMatching '--update').Count 'update ran'
    Assert-Equal $true (Get-ExtCallsMatching '--update')[0].OwnConsole 'wsl --update in its own console window too'
    Assert-Equal 0 (Get-ExtCallsMatching '--install').Count 'install did not'
}

Test-Case 'wsl-install: WSL already present is a no-op' {
    Add-ExtRule 'wsl\.exe --version' (New-ExtResult -Stdout 'WSL version: 2.7.14.0')
    $r = Invoke-WslInstallVerb -Opts @{}
    Assert-Equal 'ok' $r.Status 'ok'
    Assert-Equal 1 (Get-ExtCallLines).Count 'only the version probe ran'
}

Test-Case 'restart-for-wsl: RunOnce value, resume in settings, restart only with --now' {
    $o = ConvertFrom-HelperArgs @('--setup-exe', 'C:\Users\me\Downloads\Cognita-Setup-14.1.0-r1.exe')
    $r = Invoke-RestartForWslVerb -Opts $o.Opts
    Assert-Equal 'ok' $r.Status 'status'
    $k = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\RunOnce|CognitaSetup'
    Assert-Equal '"C:\Users\me\Downloads\Cognita-Setup-14.1.0-r1.exe" /resume' $script:Registry[$k] 'RunOnce value points at the downloaded Setup.exe'
    Assert-Equal 'after-wsl' (Read-Settings).resume 'resume recorded before the restart'
    Assert-Equal 0 (Get-ExtCallsMatching 'shutdown').Count 'no restart without --now'
    Add-ExtRule 'shutdown\.exe' (New-ExtResult)
    $o2 = ConvertFrom-HelperArgs @('--setup-exe', 'C:\Setup.exe', '--now')
    [void](Invoke-RestartForWslVerb -Opts $o2.Opts)
    $sd = (Get-ExtCallsMatching 'shutdown')[0]
    # Design 19.1 item 1: /t 0. A timeout above 0 implies /f (force-close programs), which loses the
    # user's unsaved work; the test pins both the exact arguments and that no /f or timeout above 0 is there.
    Assert-Equal '/r|/t|0|/c|Restarting to finish turning on WSL for Cognita' ($sd.Arguments -join '|') 'shutdown.exe /r /t 0 /c ...'
    Assert-Equal 0 @($sd.Arguments | Where-Object { $_ -eq '/f' }).Count 'no /f'
    Assert-Match (Get-LogText) 'restart-for-wsl: running shutdown.exe /r /t 0' 'the arguments are logged'
}

Test-Case 'restart-for-wsl (design 19.11 R1): --now --after-pid starts a detached hidden powershell that waits for Setup''s PID, then runs shutdown /r /t 0; the helper itself never runs shutdown.exe' {
    $o = ConvertFrom-HelperArgs @('--setup-exe', 'C:\Setup.exe', '--now', '--after-pid', '4321')
    $r = Invoke-RestartForWslVerb -Opts $o.Opts
    Assert-Equal 'ok' $r.Status 'status'
    Assert-Equal 'after-wsl' (Read-Settings).resume 'resume still recorded before the waiter is started'
    Assert-Equal '"C:\Setup.exe" /resume' $script:Registry['HKCU:\Software\Microsoft\Windows\CurrentVersion\RunOnce|CognitaSetup'] 'RunOnce still written'
    Assert-Equal 0 (Get-ExtCallsMatching 'shutdown').Count 'the helper does not run shutdown.exe itself: Setup is still running and would refuse the restart'
    Assert-Equal 1 $script:Detached.Count 'exactly one detached process'
    $d = $script:Detached[0]
    Assert-Match $d.FilePath 'WindowsPowerShell\\v1\.0\\powershell\.exe$' 'Windows PowerShell 5.1'
    $line = ($d.Arguments -join ' ')
    Assert-Equal '-Command' $d.Arguments[-2] 'the waiter script is the -Command argument'
    $cmd = [string]$d.Arguments[-1]
    Assert-NotMatch $cmd '"' 'no double quote inside: the script travels as ONE quoted argument'
    Assert-Match (ConvertTo-ArgString $d.Arguments) ('"' + [regex]::Escape($cmd) + '"$') 'and is quoted as exactly one argument on the real command line'
    # Parse the script the way PowerShell 5.1 will: it must be valid and do the right things.
    $errs = $null
    $tokens = [System.Management.Automation.PSParser]::Tokenize($cmd, [ref]$errs)
    Assert-Equal 0 @($errs).Count 'the waiter script parses'
    $cmdNames = @($tokens | Where-Object { $_.Type -eq 'Command' } | ForEach-Object { $_.Content })
    Assert-True ($cmdNames -contains 'Get-Process') 'it looks the process up'
    Assert-True ($cmdNames -contains 'shutdown') 'then runs shutdown'
    Assert-Match $cmd 'Get-Process -Id 4321 ' 'by the PID Setup passed'
    Assert-Match $cmd '\.WaitForExit\(\)' 'and waits for it to exit (a signal)'
    $shutArgs = @($tokens | Where-Object { $_.Type -in 'CommandArgument', 'String' -and $_.StartLine -eq 1 } | ForEach-Object { $_.Content })
    Assert-True ($shutArgs -contains 'Restarting to finish turning on WSL for Cognita') 'the comment is ONE argument to shutdown'
    Assert-Match $cmd 'shutdown /r /t 0 /c ' 'restart, no delay'
    Assert-Match $cmd 'StartTime\.ToFileTimeUtc\(\) -eq -?\d+' 'waits only on the process with Setup''s start time (a reused PID is not waited on)'
    Assert-Match $line 'Restarting to finish turning on WSL for Cognita' 'the existing comment text'
    Assert-NotMatch $line '(^|\s)/f(\s|$)' 'no /f: programs are not force-closed'
    Assert-NotMatch $line 'Start-Sleep|timeout' 'no timer'
    Assert-Match (Get-LogText) 'restart-for-wsl: starting a detached hidden powershell that waits for pid 4321' 'the PID and the detached start are logged'
    Assert-Match (Get-LogText) 'restart-for-wsl: detached waiter started=True waiting_for_pid=4321' 'and the outcome'
    Assert-Equal 4321 $r.Values['restart_after_pid'] 'the result names the PID'
}

Test-Case 'restart-for-wsl (design 19.11 R1): a PID that is not a positive number, or a waiter that would not start, fails and says why' {
    foreach ($bad in @('abc', '0', '-5')) {
        $script:Detached.Clear()
        $r = Invoke-RestartForWslVerb -Opts (ConvertFrom-HelperArgs @('--now', '--after-pid', $bad)).Opts
        Assert-Equal 'failed' $r.Status ("[$bad] failed")
        Assert-Equal 'bad-after-pid' $r.Values['reason'] ("[$bad] reason")
        Assert-Equal 0 $script:Detached.Count ("[$bad] nothing started")
        Assert-Equal 0 (Get-ExtCallsMatching 'shutdown').Count ("[$bad] no direct shutdown either")
    }
    $script:DetachedResult = $false
    $r2 = Invoke-RestartForWslVerb -Opts (ConvertFrom-HelperArgs @('--now', '--after-pid', '77')).Opts
    Assert-Equal 'failed' $r2.Status 'waiter start failure fails'
    Assert-Equal 'restart-waiter-failed' $r2.Values['reason'] 'reason'
    Assert-Match (Get-LogText) 'detached waiter started=False waiting_for_pid=77' 'logged with values'
}

Test-Case 'restart-for-wsl (design 19.11 R1): without --now nothing is restarted and no waiter is started (the install flow leaves the restart to Inno)' {
    $r = Invoke-RestartForWslVerb -Opts (ConvertFrom-HelperArgs @('--setup-exe', 'C:\Setup.exe')).Opts
    Assert-Equal 'ok' $r.Status 'ok'
    Assert-Equal 'after-wsl' (Read-Settings).resume 'resume recorded'
    Assert-Equal 0 $script:Detached.Count 'no waiter'
    Assert-Equal 0 (Get-ExtCallsMatching 'shutdown').Count 'no shutdown'
}

# ---- keepalive and readiness (design 5.8, 5.7 step 7) -------------------------------------------
Test-Case 'keepalive: task registered for launch-keepalive.vbs in the app folder; start clears the stopped flag' {
    Assert-True (Register-Keepalive) 'registered'
    Assert-Equal (Join-Path (Join-Path $script:TestDir 'app') 'launch-keepalive.vbs') $script:TaskRegistered[0] 'the VBS path'
    $flag = Get-StoppedFlagPath
    [System.IO.File]::WriteAllText($flag, 'x')
    Assert-True (Start-Keepalive) 'started'
    Assert-False (Test-Path $flag) 'the stopped flag is removed'
    Assert-Equal 1 $script:TaskStarted 'task run'
    $script:TaskInfo = [pscustomobject]@{ Exists = $false; State = ''; LastResult = 0; LastRun = '' }
    $script:TaskRegistered.Clear()
    [void](Start-Keepalive)
    Assert-Equal 1 $script:TaskRegistered.Count 'a missing task is registered before it is run'
}

Test-Case 'keepalive: the login task definition (built, never registered): logon trigger, wscript //B, no admin, no time limit, IgnoreNew, battery-proof' {
    # The real New-ScheduledTask* cmdlets build the pieces in memory; nothing is registered.
    $p = New-CognitaTaskParts -VbsPath 'C:\Users\me\AppData\Local\Cognita\app\launch-keepalive.vbs'
    Assert-Match $p.Action.Execute 'System32\\wscript\.exe$' 'wscript'
    Assert-Equal '//B "C:\Users\me\AppData\Local\Cognita\app\launch-keepalive.vbs"' $p.Action.Arguments 'hidden batch mode, quoted path'
    Assert-Match ($p.Trigger.CimClass.CimClassName) 'LogonTrigger' 'at logon'
    Assert-Equal ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name) $p.Trigger.UserId 'for the current user'
    Assert-Equal 'Interactive' ([string]$p.Principal.LogonType) 'interactive logon'
    Assert-Equal 'Limited' ([string]$p.Principal.RunLevel) 'no elevation (registered without admin)'
    Assert-Equal 'IgnoreNew' ([string]$p.Settings.MultipleInstances) 'a second copy is ignored'
    Assert-Equal 'PT0S' ([string]$p.Settings.ExecutionTimeLimit) 'no time limit'
    Assert-False $p.Settings.DisallowStartIfOnBatteries 'starts on battery'
    Assert-False $p.Settings.StopIfGoingOnBatteries 'is not stopped on battery'
}

Test-Case 'keepalive (design 18.5): Register-ScheduledTask and Start-ScheduledTask run with -ErrorAction Stop, so a NON-terminating cmdlet error still reaches the catch, the log and the warning' {
    # The fakes below report failure the way the real cmdlets often do: Write-Error (non-terminating). Only a
    # call made with -ErrorAction Stop turns that into an exception; without it Register-Keepalive said
    # "registered" for a task that does not exist.
    function Register-ScheduledTask { [CmdletBinding()] param($TaskName, $Action, $Trigger, $Settings, $Principal, [switch]$Force) Write-Error 'boom: access denied' }
    function Start-ScheduledTask { [CmdletBinding()] param($TaskName) Write-Error 'boom: cannot start' }
    ${function:Register-CognitaTask} = $script:RealRegisterCognitaTask
    ${function:Start-CognitaTask} = $script:RealStartCognitaTask
    Assert-False (Register-Keepalive) 'registration failure reported as false'
    Assert-Match (Get-LogText) 'keepalive: task registration FAILED: boom: access denied' 'and logged'
    Assert-False (Start-Keepalive) 'start failure reported as false'
    Assert-Match (Get-LogText) 'keepalive: task start FAILED: boom: cannot start' 'and logged'
}

Test-Case 'keepalive: a failing task registration is logged and reported, not thrown' {
    function Register-CognitaTask { param([string]$VbsPath) throw 'access denied' }
    Assert-False (Register-Keepalive) 'false'
    Assert-Match (Get-LogText) 'task registration FAILED: access denied' 'logged'
}

Test-Case 'readiness: waits for systemd running/degraded AND docker, bounded at 300 s on the fake clock' {
    $s = New-TestSettings
    $script:calls = 0
    Add-ExtRule 'systemctl is-system-running' { param($c) $script:calls++; if ($script:calls -lt 3) { New-ExtResult -ExitCode 1 -Stdout "starting`n" } else { New-ExtResult -Stdout "degraded`n" } }
    Add-ExtRule 'docker info' { param($c) New-ExtResult -Stdout '27.3.1' }
    Assert-True (Wait-DistroReady -Settings $s -TimeoutSec 300) 'ready'
    Assert-Equal 6000 $script:SleepTotalMs 'two 3-second sleeps'
    $script:ExtRules.Clear()
    Add-ExtRule 'systemctl is-system-running' (New-ExtResult -ExitCode 1 -Stdout "starting`n")
    $t0 = Get-ClockNow
    Assert-False (Wait-DistroReady -Settings $s -TimeoutSec 300) 'times out'
    Assert-True ((((Get-ClockNow) - $t0).TotalSeconds) -ge 300) 'a full 300 s of clock time passed'
    Assert-Match (Get-LogText) 'distro ready wait: ok=False took 30\d\.\d' 'the time taken is logged'
}

# ---- roots: mount, verify, never chown (design 5.5) -----------------------------------------------
function Add-RootFakes { Add-MountWorldFakes }
function Get-CallIndex {
    param([string]$Pattern)
    $lines = Get-ExtCallLines
    return [array]::IndexOf(@($lines | ForEach-Object { $_ -match $Pattern }), $true)
}

Test-Case 'add root (design 18.1): fstab line WITH shared, mount point 0755 BEFORE the one restart, mount checked in Docker''s view, THEN the write test as the Linux user; the helper never runs mount' {
    $s = New-TestSettings
    $folder = New-Dir 'My Docs'
    Add-RootFakes
    $r = Add-CognitaRoot -Settings $s -WindowsPath $folder
    Assert-True $r.Ok ("add root: " + $r.Reason)
    Assert-Equal 1 $r.N 'root 1'
    $iFstab = Get-CallIndex '-u root --exec sh -s'
    $iMkdir = Get-CallIndex 'mkdir -p -m 0755 /mnt/cognita-roots/1'
    $iTerm = Get-CallIndex '--terminate Cognita'
    $iCheck = Get-CallIndex 'nsenter -t 1 -m -- mountpoint -q /mnt/cognita-roots/1'
    $iAccess = Get-CallIndex '-u cognita --exec sh -s'
    Assert-True (($iFstab -ge 0) -and ($iFstab -lt $iMkdir) -and ($iMkdir -lt $iTerm) -and ($iTerm -lt $iCheck) -and ($iCheck -lt $iAccess)) ("order: fstab, mkdir, terminate, nsenter mount check, access test; got {0} {1} {2} {3} {4}" -f $iFstab, $iMkdir, $iTerm, $iCheck, $iAccess)
    Assert-Equal 1 $script:Terminates 'ONE distro restart'
    Assert-Equal 0 (Get-ExtCallsMatching '--exec mount').Count 'the helper never runs mount for a root'
    Assert-Equal 0 (Get-ExtCallsMatching 'chown|chmod').Count 'no chown or chmod on the mount point (root-owned 0755 stays)'
    Assert-Equal 1 $script:TaskStarted 'the login task was run by the restart'
    $acc = @(Get-ExtCallsMatching '-u cognita --exec sh -s')[0]
    Assert-Match $acc.StdinText '\.cognita-write-test-\$2' 'create-and-delete test file'
    Assert-Equal '/mnt/cognita-roots/1' $acc.Arguments[-2] 'tested on the mount point'
    $saved = Read-Settings
    Assert-Equal 1 @(Get-SettingsRoots $saved).Count 'root saved'
    Assert-Equal $folder (@(Get-SettingsRoots $saved))[0].windows 'windows path saved'
    Assert-Equal '/mnt/cognita-roots/1' (@(Get-SettingsRoots $saved))[0].linux 'linux path saved'
    $fs = (Get-ExtCallsMatching '-u root --exec sh -s')[0]
    Assert-True ($fs.StdinText -match [regex]::Escape(($folder -replace '\\', '/' -replace ' ', '\040') + ' /mnt/cognita-roots/1 drvfs uid=1000,gid=1000,noatime,nofail,shared 0 0')) 'fstab line has the forward-slash source with \040, nofail and shared'
    Assert-Match (Get-LogText) 'add root: restart decision needRestart=True \(2 fstab line\(s\) added or replaced\)' 'the decision is logged with its reason (root 1 and the roots folder''s base line, design 22.14)'
    $base = (Get-ExtCallsMatching '-u root --exec sh -s')[1]
    Assert-True ($base.StdinText.Contains((Get-FstabBaseLine) + "`n")) 'the base line is written too, before the one restart'
}

Test-Case 'add root: line already right and root already mounted in Docker''s view: no fstab write, no restart' {
    $s = New-TestSettings
    $folder = New-Dir 'docs'
    Add-RootFakes
    $line = Get-FstabLineForRoot -WindowsPath (Get-NormalizedPath $folder) -N 1
    $script:ExtRules.Insert(0, @{ Pattern = 'cat /etc/fstab'; Response = (New-ExtResult -Stdout ("/dev/sda / ext4 defaults 0 1`n" + (Get-FstabBaseLine) + "`n" + $line + "`n")) })
    $script:mounted['/mnt/cognita-roots/1'] = $true
    $r = Add-CognitaRoot -Settings $s -WindowsPath $folder
    Assert-True $r.Ok 'ok'
    Assert-Equal 0 $script:Terminates 'nothing changed and it is mounted, so the distro is left running'
    Assert-Equal 0 (Get-ExtCallsMatching '-u root --exec sh -s').Count 'fstab not rewritten'
    Assert-Equal 0 (Get-ExtCallsMatching '--exec mount').Count 'and never mounted by hand'
    Assert-Match (Get-LogText) 'needRestart=False' 'decision logged'
}

Test-Case 'add root: line already right but the root is NOT mounted in Docker''s view: one restart, no mount' {
    $s = New-TestSettings
    $folder = New-Dir 'docs'
    Add-RootFakes
    $line = Get-FstabLineForRoot -WindowsPath (Get-NormalizedPath $folder) -N 1
    $script:ExtRules.Insert(0, @{ Pattern = 'cat /etc/fstab'; Response = (New-ExtResult -Stdout ("/dev/sda / ext4 defaults 0 1`n" + (Get-FstabBaseLine) + "`n" + $line + "`n")) })
    $script:FstabRoots['/mnt/cognita-roots/1'] = $true
    $r = Add-CognitaRoot -Settings $s -WindowsPath $folder
    Assert-True $r.Ok 'ok after the restart'
    Assert-Equal 1 $script:Terminates 'restarted once so fstab is applied'
    Assert-Equal 0 (Get-ExtCallsMatching '--exec mount').Count 'no mount'
    Assert-Match (Get-LogText) "needRestart=True \(root 1 is not mounted in Docker's view\)" 'reason logged'
}

Test-Case 'add root (design 18.1 rule 5): an fstab line from before `shared` is replaced and followed by one restart' {
    $s = New-TestSettings
    $folder = New-Dir 'docs'
    Add-RootFakes
    $old = ((Get-NormalizedPath $folder) -replace '\\', '/') + ' /mnt/cognita-roots/1 drvfs uid=1000,gid=1000,noatime,nofail 0 0'
    $script:ExtRules.Insert(0, @{ Pattern = 'cat /etc/fstab'; Response = (New-ExtResult -Stdout ("/dev/sda / ext4 defaults 0 1`n" + $old + "`n")) })
    $script:mounted['/mnt/cognita-roots/1'] = $true   # mounted from before, but without shared
    $r = Add-CognitaRoot -Settings $s -WindowsPath $folder
    Assert-True $r.Ok 'ok'
    $w = (Get-ExtCallsMatching '-u root --exec sh -s')[0]
    Assert-Match $w.StdinText 'nofail,shared 0 0' 'the rewritten line has shared'
    Assert-NotMatch $w.StdinText 'noatime,nofail 0 0' 'and the old shape is gone'
    Assert-Equal 1 $script:Terminates 'followed by a restart even though it was mounted'
    Assert-Match (Get-LogText) 'root 1: fstab line action=replaced' 'action logged'
}

Test-Case 'add root: a root that is still not mounted after the restart, a distro that never gets ready, and a failed write test each report the folder and do not save it' {
    $s = New-TestSettings
    $folder = New-Dir 'docs'
    Add-RootFakes
    $script:ExtRules.Insert(0, @{ Pattern = 'wsl\.exe --terminate'; Response = (New-ExtResult) })   # the restart applies nothing
    $r = Add-CognitaRoot -Settings $s -WindowsPath $folder
    Assert-False $r.Ok 'failed'
    Assert-Match $r.Reason 'could not open the folder' 'reason names the folder'
    Assert-Equal 0 @(Get-SettingsRoots (Read-Settings)).Count 'not saved'
    Assert-Equal 0 (Get-ExtCallsMatching '-u cognita --exec sh -s').Count 'the access test is not attempted on an unmounted root'
    $script:ExtRules.Clear(); $script:ExtCalls.Clear(); Add-RootFakes
    $script:ExtRules.Insert(0, @{ Pattern = 'systemctl is-system-running'; Response = (New-ExtResult -ExitCode 1 -Stdout "starting`n") })
    $t0 = Get-ClockNow
    $r1 = Add-CognitaRoot -Settings $s -WindowsPath $folder
    Assert-False $r1.Ok 'a distro that never gets ready'
    Assert-Match $r1.Reason 'did not finish starting within 5 minutes' 'reason'
    Assert-True ((((Get-ClockNow) - $t0).TotalSeconds) -ge 300) 'a full 300 s of clock time was waited, no more'
    Assert-Equal 0 @(Get-SettingsRoots (Read-Settings)).Count 'not saved'
    $script:ExtRules.Clear(); $script:ExtCalls.Clear(); Add-RootFakes
    $script:ExtRules.Insert(0, @{ Pattern = '-u cognita --exec sh -s'; Response = (New-ExtResult -ExitCode 1 -Stderr 'Permission denied') })
    $r2 = Add-CognitaRoot -Settings $s -WindowsPath $folder
    Assert-False $r2.Ok 'failed write test'
    Assert-Match $r2.Reason 'cannot read and write' 'reason'
}

Test-Case 'add root: -SyncOthers rewrites every other root''s line too and still restarts only ONCE' {
    $one = New-Dir 'docs1'
    $two = New-Dir 'docs2'
    $s = New-TestSettings -RootPaths @($one, $two)
    Add-RootFakes
    $old1 = ((Get-NormalizedPath $one) -replace '\\', '/') + ' /mnt/cognita-roots/1 drvfs uid=1000,gid=1000,noatime,nofail 0 0'
    $old2 = ((Get-NormalizedPath $two) -replace '\\', '/') + ' /mnt/cognita-roots/2 drvfs uid=1000,gid=1000,noatime,nofail 0 0'
    $script:ExtRules.Insert(0, @{ Pattern = 'cat /etc/fstab'; Response = (New-ExtResult -Stdout ("/dev/sda / ext4 defaults 0 1`n" + $old1 + "`n" + $old2 + "`n")) })
    $r = Add-CognitaRoot -Settings $s -WindowsPath $one -SyncOthers
    Assert-True $r.Ok 'ok'
    Assert-Equal 3 (Get-ExtCallsMatching '-u root --exec sh -s').Count 'both lines rewritten, and the base line added (design 22.14)'
    Assert-Equal 1 $script:Terminates 'one restart for both'
    Assert-Equal 2 @(Get-SettingsRoots (Read-Settings)).Count 'roots still two'
}

Test-Case 'add root: the second folder takes the next number and tells the Linux CLI; nested folders are refused before anything runs' {
    $one = New-Dir 'docs1'
    $s = New-TestSettings -RootPaths @($one)
    $two = New-Dir 'docs2'
    Add-RootFakes
    Add-ExtRule '-u cognita --exec /usr/local/bin/cognita add-folder' (New-ExtResult)
    $r = Add-CognitaRoot -Settings $s -WindowsPath $two -TellLinux
    Assert-True $r.Ok 'ok'
    Assert-Equal 2 $r.N 'root 2'
    Assert-True ((Get-CallIndex '--terminate Cognita') -ge 0 -and (Get-CallIndex '--terminate Cognita') -lt (Get-CallIndex 'nsenter -t 1 -m') -and (Get-CallIndex 'nsenter -t 1 -m') -lt (Get-CallIndex 'cognita add-folder')) 'add-folder (design 18.1): fstab, restart, mount check, THEN cognita add-folder'
    Assert-Equal 0 (Get-ExtCallsMatching '--exec mount').Count 'no mount'
    $cli = (Get-ExtCallsMatching 'cognita add-folder')[0]
    Assert-Equal "add-folder|/mnt/cognita-roots/2|--display|$two|--progress-file|$(ConvertTo-WslMntPath $script:ProgressFile)" ($cli.Arguments[6..($cli.Arguments.Count - 1)] -join '|') 'add-folder with the mount point, the Windows path as display, progress file'
    $calls = (Get-ExtCallLines).Count
    $nested = New-Dir 'docs1\inner'
    $r2 = Add-CognitaRoot -Settings $s -WindowsPath $nested -TellLinux
    Assert-False $r2.Ok 'nested refused'
    Assert-Equal $calls (Get-ExtCallLines).Count 'no wsl command ran for it'
}

Test-Case 'add root: at most 9 roots' {
    $paths = @(); 1..9 | ForEach-Object { $paths += (New-Dir "r$_") }
    $s = New-TestSettings -RootPaths $paths
    $r = Add-CognitaRoot -Settings $s -WindowsPath (New-Dir 'tenth')
    Assert-False $r.Ok 'refused'
    Assert-Match $r.Reason 'up to 9' 'reason'
}

Test-Case 'a root that is down: the message names the Windows path, Assert-RootsAvailable stops before the Linux CLI' {
    $s = New-TestSettings -RootPaths @('C:\Users\me\Docs', 'D:\Work')
    Add-ExtRule 'mountpoint -q /mnt/cognita-roots/1' (New-ExtResult)
    Add-ExtRule 'mountpoint -q /mnt/cognita-roots/2' (New-ExtResult -ExitCode 1)
    $m = Assert-RootsAvailable -Settings $s
    Assert-Equal 'Your projects folder D:\Work is not available to Cognita. Reconnect the drive or restore the folder, then run: cognita restart.' $m 'exact message'
    $script:ExtRules.Clear()
    Add-ExtRule 'mountpoint -q' (New-ExtResult)
    Assert-True ($null -eq (Assert-RootsAvailable -Settings $s)) 'all up: no message'
}

Test-Case 'mount check (design 18.1 rule 3): root, through nsenter into PID 1''s mount namespace (Docker''s view), never a plain mountpoint in the session' {
    $s = New-TestSettings -RootPaths @('C:\A')
    Add-ExtRule 'mountpoint -q' (New-ExtResult)
    Assert-True (Test-RootMounted -Settings $s -N 2) 'mounted'
    $c = (Get-ExtCallsMatching 'mountpoint')[0]
    Assert-Equal '-d|Cognita|-u|root|--exec|nsenter|-t|1|-m|--|mountpoint|-q|/mnt/cognita-roots/2' ($c.Arguments -join '|') 'exact command'
    $script:ExtRules.Clear()
    Add-ExtRule 'mountpoint -q' (New-ExtResult -ExitCode 1)
    Assert-False (Test-RootMounted -Settings $s -N 2) 'exit 1 is unmounted'
    $script:ExtRules.Clear()
    Add-ExtRule 'mountpoint -q' (New-ExtResult -ExitCode 1 -Stderr 'nsenter: cannot open /proc/1/ns/mnt' -TimedOut $true)
    Assert-False (Test-RootMounted -Settings $s -N 2) 'a timeout is unmounted, not mounted'
    Assert-Match (Get-LogText) "root 2 mounted=False \(Docker's view: nsenter -t 1 -m mountpoint -q exit 1\)" 'logged with the value'
}

# ---- Restart-CognitaDistro and Restore-RootMounts (design 18.1 rule 2) --------------------------------
Test-Case 'Restart-CognitaDistro: stopped flag removed first, wsl --terminate, the login task run, then readiness; each step and the time taken logged' {
    $s = New-TestSettings
    [System.IO.File]::WriteAllText((Get-StoppedFlagPath), 'x')
    $script:flagAtTerminate = $null
    $script:taskAtTerminate = -1
    Add-ExtRule 'wsl\.exe --terminate Cognita' { param($c) $script:flagAtTerminate = (Test-Path (Get-StoppedFlagPath)); $script:taskAtTerminate = $script:TaskStarted; New-ExtResult }
    Add-ReadyFakes
    $r = Restart-CognitaDistro -Settings $s -Reason 'a test'
    Assert-True $r.Ready 'ready'
    Assert-False $script:flagAtTerminate 'the flag was gone BEFORE the terminate, so the keepalive loop relaunches instead of exiting'
    Assert-Equal 0 $script:taskAtTerminate 'the login task had not run yet when the distro was terminated'
    Assert-Equal 1 $script:TaskStarted 'and it ran once after'
    $calls = Get-ExtCallLines
    Assert-Equal 1 @($calls | Where-Object { $_ -match '--terminate' }).Count 'exactly one terminate'
    Assert-True ((Get-CallIndex '--terminate Cognita') -lt (Get-CallIndex 'systemctl is-system-running')) 'terminate before the readiness probe'
    $log = Get-LogText
    Assert-Match $log 'restart distro: begin distro=Cognita reason=\[a test\]' 'begin'
    Assert-Match $log 'restart distro: step 1 removed the stopped flag' 'step 1'
    Assert-Match $log 'restart distro: step 2 wsl --terminate Cognita exit 0' 'step 2'
    Assert-Match $log 'restart distro: step 3 login task run ok=True' 'step 3'
    Assert-Match $log 'restart distro: end ready=True took \d+\.\ds' 'the time taken'
}

Test-Case 'Restart-CognitaDistro: a distro that never gets ready is waited for 300 s of clock time and reported, not assumed' {
    $s = New-TestSettings
    Add-ExtRule 'wsl\.exe --terminate' (New-ExtResult)
    Add-ExtRule 'systemctl is-system-running' (New-ExtResult -ExitCode 1 -Stdout "starting`n")
    $t0 = Get-ClockNow
    $r = Restart-CognitaDistro -Settings $s -Reason 'never ready'
    Assert-False $r.Ready 'not ready'
    Assert-True ((((Get-ClockNow) - $t0).TotalSeconds) -ge 300) 'a full 300 s waited'
    Assert-True ((((Get-ClockNow) - $t0).TotalSeconds) -lt 320) 'and no more'
    Assert-Match (Get-LogText) 'restart distro: end ready=False took 30\d\.\ds' 'logged with the time'
    Assert-Match (Get-LogText) 'restart distro: step 1 no stopped flag to remove' 'no flag is logged as such'
}

Test-Case 'Restore-RootMounts: every root mounted means no restart and no mount' {
    $s = New-TestSettings -RootPaths @('C:\A', 'D:\B')
    Add-MountWorldFakes
    $script:mounted['/mnt/cognita-roots/1'] = $true; $script:mounted['/mnt/cognita-roots/2'] = $true
    $r = Restore-RootMounts -Settings $s
    Assert-False $r.Restarted 'not restarted'
    Assert-Equal 0 @($r.Still).Count 'none down'
    Assert-Equal 0 $script:Terminates 'no terminate'
    Assert-Equal 0 (Get-ExtCallsMatching '--exec mount').Count 'no mount'
    Assert-Match (Get-LogText) 'remount: root 1 already mounted, nothing to do' 'decision logged'
}

Test-Case 'Restore-RootMounts: any unmounted root restarts the distro ONCE (fstab is applied at its start) and never runs mount' {
    $s = New-TestSettings -RootPaths @('C:\A', 'D:\B', 'E:\C')
    Add-MountWorldFakes
    $script:FstabRoots = @{ '/mnt/cognita-roots/1' = $true; '/mnt/cognita-roots/2' = $true; '/mnt/cognita-roots/3' = $true }
    $script:mounted['/mnt/cognita-roots/1'] = $true; $script:mounted['/mnt/cognita-roots/3'] = $true
    $r = Restore-RootMounts -Settings $s
    Assert-True ($r.Restarted -and $r.Ready) 'restarted and ready'
    Assert-Equal 0 @($r.Still).Count 'all up afterwards'
    Assert-Equal 1 $script:Terminates 'one terminate'
    Assert-Equal 0 (Get-ExtCallsMatching '--exec mount').Count 'never mount'
    Assert-Match (Get-LogText) 'remount: 1 root\(s\) unmounted \(2\)' 'which roots, logged'
}

Test-Case 'Restore-RootMounts: a folder that will not come back is reported with its Windows path; a distro that will not get ready says so' {
    $s = New-TestSettings -RootPaths @('D:\Gone')
    Add-MountWorldFakes
    $script:ExtRules.Insert(0, @{ Pattern = 'wsl\.exe --terminate'; Response = (New-ExtResult) })   # fstab applies nothing: the drive is gone
    $r = Restore-RootMounts -Settings $s
    Assert-True $r.Restarted 'restarted'
    Assert-Equal 1 @($r.Still).Count 'still down'
    Assert-Equal 'D:\Gone' $r.Still[0].Windows 'names the folder'
    Assert-Match $r.Still[0].Message 'not available to Cognita' 'message'
    $script:ExtRules.Clear(); $script:ExtCalls.Clear()
    Add-MountWorldFakes
    $script:ExtRules.Insert(0, @{ Pattern = 'systemctl is-system-running'; Response = (New-ExtResult -ExitCode 1 -Stdout "starting`n") })
    $r2 = Restore-RootMounts -Settings $s
    Assert-True ($r2.Restarted -and -not $r2.Ready) 'restarted but never ready'
}

Test-Case 'design 18.1 rule 6: no code path of the helper runs mount for a root (only a comment may say the word)' {
    $src = [System.IO.File]::ReadAllText((Join-Path $PSScriptRoot '..\CognitaWin.ps1'))
    $noBlocks = [regex]::Replace($src, '(?s)<#.*?#>', '')
    $code = (($noBlocks -split "`n") | Where-Object { -not $_.TrimStart().StartsWith('#') }) -join "`n"
    Assert-NotMatch $code "'mount'" 'no quoted mount command argument anywhere'
    Assert-NotMatch $code "'umount'" 'and no umount either'
    Assert-NotMatch $code '--exec mount|Mount-RootPoint|Repair-RootMounts' 'the old functions and command are gone'
    # The one function that starts a mount is Restart-CognitaDistro (a distro start applies fstab): it terminates, it does not mount.
    Assert-Match $code 'Arguments @\(''--terminate'', \$distro\)' 'the restart is a terminate'
}

# ---- import (design 5.7) -------------------------------------------------------------------------
function New-FakeImage {
    $p = Join-Path $script:TestDir 'cognita-wsl.tar.gz'
    [System.IO.File]::WriteAllBytes($p, [byte[]](1..200))
    $sha = [System.Security.Cryptography.SHA256]::Create()
    $hash = (($sha.ComputeHash([System.IO.File]::ReadAllBytes($p)) | ForEach-Object { $_.ToString('x2') }) -join '')
    return @{ Path = $p; Sha = $hash }
}

Test-Case 'import: hash checked, pending record written BEFORE the import, marker written, state installed, image deleted' {
    $img = New-FakeImage
    $vhd = Join-Path $script:TestDir 'vhd'
    $s = New-Settings -VhdDir $vhd
    $script:stateAtImport = ''
    Add-ExtRule 'wsl\.exe --import' { param($c) $script:stateAtImport = (Read-Settings).state; New-ExtResult }
    Add-ExtRule '-u root --exec sh -s' (New-ExtResult)
    $r = Invoke-ImportImage -Settings $s -ImagePath $img.Path -ImageSha256 $img.Sha -VhdDir $vhd
    Assert-Equal 'ok' $r.Status 'status'
    Assert-Equal 'import-pending' $script:stateAtImport 'settings.json said import-pending while the import ran'
    $imp = (Get-ExtCallsMatching '--import')[0]
    Assert-Equal "--import|Cognita|$vhd|$($img.Path)|--version|2" ($imp.Arguments -join '|') 'exact import command'
    $marker = (Get-ExtCallsMatching 'sh -s')[0]
    Assert-Equal $s.installation_id $marker.Arguments[-1] 'the marker carries the installation id'
    Assert-Match $marker.StdinText '/etc/cognita-distro' 'writes /etc/cognita-distro'
    Assert-Equal 'installed' (Read-Settings).state 'state installed'
    Assert-True (Test-Path $img.Path) 'the extracted image file belongs to Setup: the helper deletes nothing it did not create'
}

Test-Case 'import (design 19.7 item 20): the parent folders the helper creates for a custom disk location are recorded as created_dirs, deepest first; folders that already existed are not' {
    $img = New-FakeImage
    Add-ExtRule 'wsl\.exe --import' (New-ExtResult)
    Add-ExtRule '-u root --exec sh -s' (New-ExtResult)
    # TestDir exists; data and data\Cognita do not: both are ours.
    $vhd = Join-Path $script:TestDir 'data\Cognita\wsl'
    $s = New-Settings -VhdDir $vhd
    $r = Invoke-ImportImage -Settings $s -ImagePath $img.Path -ImageSha256 $img.Sha -VhdDir $vhd
    Assert-Equal 'ok' $r.Status 'status'
    $saved = Read-Settings
    $want = @((Join-Path $script:TestDir 'data\Cognita'), (Join-Path $script:TestDir 'data'))
    Assert-Equal ($want -join '|') (@($saved.created_dirs) -join '|') 'both created ancestors, deepest first, in settings.json'
    Assert-True (Test-Path -LiteralPath (Join-Path $script:TestDir 'data\Cognita')) 'and they really were created'
    Assert-Match (Get-LogText) 'import: created the disk folder''s parent\(s\)' 'the decision is logged'
    # a second import over the now-existing parent records nothing more (no duplicates, nothing new)
    $script:LogLines.Clear()
    $s2 = Read-Settings
    $r2 = Invoke-ImportImage -Settings $s2 -ImagePath $img.Path -ImageSha256 $img.Sha -VhdDir $vhd
    Assert-Equal 'ok' $r2.Status 'second import ok'
    Assert-Equal 2 @((Read-Settings).created_dirs).Count 'still exactly the two'
    Assert-Match (Get-LogText) 'the disk folder''s parent \[.*\] already exists; nothing recorded in created_dirs' 'existing parent logged'
    # the default location's parent (the data folder) already exists: nothing recorded at all
    $vhd3 = Join-Path $script:TestDir 'vhd'
    $s3 = New-Settings -VhdDir $vhd3
    [void](Invoke-ImportImage -Settings $s3 -ImagePath $img.Path -ImageSha256 $img.Sha -VhdDir $vhd3)
    Assert-True ($null -eq (Read-Settings).PSObject.Properties['created_dirs'] -or @((Read-Settings).created_dirs).Count -eq 0) 'no folder made, no record'
}

Test-Case 'import: a checksum mismatch stops before anything is imported' {
    $img = New-FakeImage
    $s = New-Settings -VhdDir (Join-Path $script:TestDir 'vhd')
    $r = Invoke-ImportImage -Settings $s -ImagePath $img.Path -ImageSha256 ('0' * 64) -VhdDir (Join-Path $script:TestDir 'vhd')
    Assert-Equal 'failed' $r.Status 'failed'
    Assert-Equal 'image-hash-mismatch' $r.Reason 'reason'
    Assert-Equal 0 (Get-ExtCallLines).Count 'no wsl command'
    Assert-False (Test-Path (Get-SettingsPath)) 'no record written'
}

Test-Case 'import: default distro is restored when the import changed it, left alone when it did not' {
    $img = New-FakeImage
    $vhd = Join-Path $script:TestDir 'vhd'
    $script:LxssDefault = '{OLD-DEFAULT}'
    Add-ExtRule 'wsl\.exe --import' { param($c) $script:LxssDefault = '{NEW-COGNITA}'; New-ExtResult }
    Add-ExtRule '-u root --exec sh -s' (New-ExtResult)
    $r = Invoke-ImportImage -Settings (New-Settings -VhdDir $vhd) -ImagePath $img.Path -ImageSha256 $img.Sha -VhdDir $vhd
    Assert-Equal 'ok' $r.Status 'ok'
    Assert-Equal '{OLD-DEFAULT}' $script:LxssDefault 'restored'
    Assert-Equal 1 $script:DefaultSets.Count 'one registry write'
    Assert-Match (Get-LogText) 'default distribution changed from \{OLD-DEFAULT\} to \{NEW-COGNITA\}; restoring' 'decision logged'
    # unchanged: nothing written
    $script:ExtRules.Clear(); $script:DefaultSets.Clear()
    $img2 = New-FakeImage
    $script:LxssDefault = '{OLD-DEFAULT}'
    Add-ExtRule 'wsl\.exe --import' (New-ExtResult)
    Add-ExtRule '-u root --exec sh -s' (New-ExtResult)
    [void](Invoke-ImportImage -Settings (New-Settings -VhdDir $vhd) -ImagePath $img2.Path -ImageSha256 $img2.Sha -VhdDir $vhd)
    Assert-Equal 0 $script:DefaultSets.Count 'no write when it did not change'
    # no previous distro: nothing to restore
    $script:ExtRules.Clear()
    $img3 = New-FakeImage
    $script:LxssDefault = $null
    Add-ExtRule 'wsl\.exe --import' { param($c) $script:LxssDefault = '{NEW-COGNITA}'; New-ExtResult }
    Add-ExtRule '-u root --exec sh -s' (New-ExtResult)
    [void](Invoke-ImportImage -Settings (New-Settings -VhdDir $vhd) -ImagePath $img3.Path -ImageSha256 $img3.Sha -VhdDir $vhd)
    Assert-Equal 0 $script:DefaultSets.Count 'with no previous default there is nothing to restore'
}

Test-Case 'import: HCS_E_SERVICE_NOT_AVAILABLE means restart required; virtualization errors give the BIOS message' {
    $img = New-FakeImage
    $vhd = Join-Path $script:TestDir 'vhd'
    Add-ExtRule 'wsl\.exe --import' (New-ExtResult -ExitCode 1 -Stderr 'Wsl/Service/RegisterDistro/CreateVm/HCS/HCS_E_SERVICE_NOT_AVAILABLE')
    $r = Invoke-ImportImage -Settings (New-Settings -VhdDir $vhd) -ImagePath $img.Path -ImageSha256 $img.Sha -VhdDir $vhd
    Assert-Equal 'restart-required' $r.Status 'restart'
    $script:ExtRules.Clear(); $script:Out.Clear()
    $img2 = New-FakeImage
    Add-ExtRule 'wsl\.exe --import' (New-ExtResult -ExitCode 1 -Stderr 'Wsl/Service/RegisterDistro/CreateVm/HCS/HCS_E_HYPERV_NOT_INSTALLED')
    $r2 = Invoke-ImportImage -Settings (New-Settings -VhdDir $vhd) -ImagePath $img2.Path -ImageSha256 $img2.Sha -VhdDir $vhd
    Assert-Equal 'failed' $r2.Status 'failed'
    Assert-Equal 'virtualization' $r2.Reason 'reason'
    $f = @(Get-ProgressObjects | Where-Object { $_.state -eq 'failed' })[0]
    Assert-Match $f.fix "Turn on virtualization \(Intel VT-x or AMD-V/SVM\) in your PC's BIOS/UEFI settings\. On a virtual machine, turn on nested virtualization\." 'design text'
    Assert-Equal 'virtualization' (Get-ImportFailureKind 'error 0x80370102') '0x80370102 too'
    Assert-Equal 'other' (Get-ImportFailureKind 'The system cannot find the file specified') 'unrelated errors are generic'
}

Test-Case 'import: the wait for the image import writes a progress line at least every 5 s (heartbeat)' {
    $img = New-FakeImage
    $vhd = Join-Path $script:TestDir 'vhd'
    Add-ExtRule 'wsl\.exe --import' { param($c) foreach ($i in 1..4) { Invoke-ClockSleep 6000; if ($c.OnPoll) { & $c.OnPoll } }; New-ExtResult }
    Add-ExtRule '-u root --exec sh -s' (New-ExtResult)
    [void](Invoke-ImportImage -Settings (New-Settings -VhdDir $vhd) -ImagePath $img.Path -ImageSha256 $img.Sha -VhdDir $vhd)
    $beats = @((Get-ProgressObjects) | Where-Object { $_.stage -eq 'import' -and $_.state -eq 'progress' -and $_.message -like 'Importing the Linux image*' })
    Assert-Equal 4 $beats.Count 'one per poll'
    Assert-Equal 5000 (Get-ExtCallsMatching '--import')[0].PollIntervalMs 'the beat is polled every 5 s (not every second)'
}

# ---- the state verb --------------------------------------------------------------------------------
Test-Case 'state: nothing installed' {
    $r = Invoke-StateVerb -Opts @{}
    Assert-Equal 'ok' $r.Status 'ok'
    Assert-Equal 0 $r.Values['installed'] 'installed=0'
    Assert-Equal 'absent' $r.Values['distro'] 'distro=absent'
    Assert-Equal 0 (Get-ExtCallLines).Count 'read-only: no wsl call for a missing distro'
}

Test-Case 'state: installed and running, with the keys Setup reads (folder, ports, size)' {
    $vhd = New-Dir 'vhd'
    [System.IO.File]::WriteAllBytes((Join-Path $vhd 'ext4.vhdx'), (New-Object byte[] 1234))
    $s = New-TestSettings -State 'installed' -Vhd $vhd -RootPaths @('C:\Users\me\Docs')
    Set-SettingProp $s 'setup_version' '14.1.0'
    Set-SettingProp $s 'linux_installed_version' '14.1.0'
    Set-SettingProp $s 'admin_user' 'boss'
    Save-Settings $s
    Set-Distro -BasePath $vhd
    Add-ExtRule 'wsl\.exe --list --running --quiet' (New-ExtResult -Stdout "Cognita`r`n")
    $r = Invoke-StateVerb -Opts @{}
    Assert-Equal 1 $r.Values['installed'] 'installed'
    Assert-Equal '14.1.0' $r.Values['linux_version'] 'linux_version (design 18.2)'
    Assert-Equal 'boss' $r.Values['admin_user'] 'admin_user (design 18.2)'
    Assert-Equal 'installed' $r.Values['state'] 'the settings state stays as its own key'
    Assert-Equal 1 $r.Values['running'] 'running'
    Assert-Equal 'C:\Users\me\Docs' $r.Values['root1'] 'root1'
    Assert-Equal 8675 $r.Values['mcp_port'] 'mcp_port'
    Assert-Equal 8676 $r.Values['admin_port'] 'admin_port'
    Assert-Equal 1234 $r.Values['vhd_bytes'] 'vhd_bytes'
    Assert-Equal 'present' $r.Values['distro'] 'distro is present|absent'
    Assert-Equal '14.1.0' $r.Values['version'] 'version'
    Assert-Equal 1 (Get-ExtCallLines).Count 'only the running-list query; the marker is not read (the distro is never started)'
}

Test-Case 'state (design 21.3): proof= is the self-test outcome the last install/update recorded (empty when unknown), read from settings with no wsl call beyond the running list' {
    $vhd = New-Dir 'vhd'
    $s = New-TestSettings -State 'installed' -Vhd $vhd
    Set-SettingProp $s 'linux_installed_version' '14.1.0'
    Set-Distro -BasePath $vhd
    Add-ExtRule 'wsl\.exe --list --running --quiet' (New-ExtResult -Stdout "Cognita`r`n")
    $r0 = Invoke-StateVerb -Opts @{}
    Assert-Equal '' $r0.Values['proof'] 'nothing recorded: empty'
    Set-SettingProp $s 'linux_proof' 'skipped'; Save-Settings $s
    $r1 = Invoke-StateVerb -Opts @{}
    Assert-Equal 'skipped' $r1.Values['proof'] 'recorded skipped'
    Assert-Match (Format-ResultLine 'ok' $r1.Values) ';proof=skipped(;|$)' 'on the result line'
    Assert-Match (Get-LogText) 'state: .* proof=\[skipped\]' 'logged with its value'
    Set-SettingProp $s 'linux_proof' 'passed'; Save-Settings $s
    Assert-Equal 'passed' (Invoke-StateVerb -Opts @{}).Values['proof'] 'recorded passed'
    Assert-Equal 0 (Get-ExtCallsMatching 'install\.env|sh -s').Count 'state never runs a script in the distro'
}

Test-Case 'state (22.4): reports acceleration from settings ("" when unknown), runs nothing in the distro, and the key is on the result line' {
    $vhd = New-Dir 'vhd'
    $s = New-TestSettings -State 'installed' -Vhd $vhd
    Set-SettingProp $s 'linux_installed_version' '14.1.0'
    Set-Distro -BasePath $vhd
    Add-ExtRule 'wsl\.exe --list --running --quiet' (New-ExtResult -Stdout "Cognita`r`n")
    $r0 = Invoke-StateVerb -Opts @{}
    Assert-Equal '' $r0.Values['acceleration'] 'nothing recorded: empty'
    Assert-Match (Format-ResultLine 'ok' $r0.Values) ';acceleration=(;|$)' 'the key is on the line, empty'
    Set-SettingProp $s 'acceleration' 'nvidia'; Save-Settings $s
    $r1 = Invoke-StateVerb -Opts @{}
    Assert-Equal 'nvidia' $r1.Values['acceleration'] 'recorded nvidia'
    Assert-Match (Format-ResultLine 'ok' $r1.Values) ';acceleration=nvidia(;|$)' 'on the result line'
    Assert-Match (Get-LogText) 'state: .* acceleration=\[nvidia\]' 'logged with its value'
    Assert-Equal 0 (Get-ExtCallsMatching 'sh -s|cognita status').Count 'state never runs anything in the distro'
    Set-SettingProp $s 'acceleration' 'NVIDIA'; Save-Settings $s
    Assert-Equal 'nvidia' (Invoke-StateVerb -Opts @{}).Values['acceleration'] 'lower-cased'
}

Test-Case 'state: a resume marker and an uninstalled-but-kept install are reported' {
    $vhd = New-Dir 'vhd'
    $s = New-TestSettings -State 'uninstalled' -Vhd $vhd
    Set-SettingProp $s 'resume' 'after-wsl'; Save-Settings $s
    Set-Distro -BasePath $vhd
    Add-ExtRule 'wsl\.exe --list --running --quiet' (New-ExtResult -Stdout '')
    $r = Invoke-StateVerb -Opts @{}
    Assert-Equal 0 $r.Values['installed'] 'no recorded Linux install: not installed (design 18.2)'
    Assert-Equal 'uninstalled' $r.Values['state'] 'state'
    Assert-Equal 1 $r.Values['owned'] 'the kept distro is ours'
    Assert-Equal 'after-wsl' $r.Values['resume'] 'resume'
    Assert-Equal 0 $r.Values['running'] 'not running'
    Assert-Equal '' $r.Values['linux_version'] 'linux_version empty'
    Assert-Equal '' $r.Values['admin_user'] 'admin_user empty'
}

Test-Case 'state (design 19.11 R6/R7): funnel_port is the PUBLIC https port, only while Tailscale still serves the recorded Funnel; empty when none is recorded' {
    $vhd = New-Dir 'vhd'
    $s = New-TestSettings -State 'installed' -Vhd $vhd
    Set-Distro -BasePath $vhd
    Add-ExtRule 'wsl\.exe --list --running --quiet' (New-ExtResult -Stdout '')
    # none recorded: empty, and Tailscale is not asked at all
    $r0 = Invoke-StateVerb -Opts @{}
    Assert-Equal '' $r0.Values['funnel_port'] 'no Funnel recorded: empty'
    Assert-Equal 0 (Get-ExtCallsMatching 'tailscale').Count 'no Funnel recorded: Tailscale is not asked'
    # recorded on 8443 -> the MCP port 8675, and still served: funnel_port is 8443 (not 8675)
    Set-SettingProp $s 'funnel' ([pscustomobject][ordered]@{ https_port = 8443; target = 8675 }); Save-Settings $s
    Add-FunnelStatusFake -Serving @{ 8443 = 'http://127.0.0.1:8675' }
    $r1 = Invoke-StateVerb -Opts @{}
    Assert-Equal 8443 $r1.Values['funnel_port'] 'recorded + still served: the public https port'
    Assert-Equal 8443 (Read-Settings).funnel.https_port 'the record is kept'
    Assert-Match (Get-LogText) 'state: recorded funnel https_port=8443 target=8675 served=True' 'the check is logged with its values'
    Assert-Match (Get-LogText) 'funnel_port=\[8443\]' 'and the reported value'
    Assert-Match ((Format-ResultLine 'ok' $r1.Values)) ';funnel_port=8443(;|$)' 'on the result line'
    # turned off by hand: cleared, logged, reported empty
    $script:FunnelJson = '{}'
    $r2 = Invoke-StateVerb -Opts @{}
    Assert-Equal '' $r2.Values['funnel_port'] 'recorded + not served: empty'
    Assert-True ($null -eq (Read-Settings).funnel) 'and the stale record is cleared'
    Assert-Match (Get-LogText) 'state: recorded funnel cleared from settings: tailscale does not serve https port 8443 forwarding to localhost:8675' 'the clearing and its reason are logged'
    # Tailscale gone: the same
    Set-SettingProp $s 'funnel' ([pscustomobject][ordered]@{ https_port = 443; target = 8675 }); Save-Settings $s
    $script:TailscaleExe = $null
    $r3 = Invoke-StateVerb -Opts @{}
    Assert-Equal '' $r3.Values['funnel_port'] 'tailscale.exe absent: empty'
    Assert-True ($null -eq (Read-Settings).funnel) 'record cleared'
    Assert-Match (Get-LogText) 'recorded funnel cleared from settings: tailscale.exe not found' 'reason logged'
}

Test-Case 'state (review of 19.11): Tailscale present but not answering (service stopped) is UNKNOWN: the Funnel record is kept and still reported' {
    $vhd = New-Dir 'vhd'
    $s = New-TestSettings -State 'installed' -Vhd $vhd
    Set-Distro -BasePath $vhd
    Add-ExtRule 'wsl\.exe --list --running --quiet' (New-ExtResult -Stdout '')
    Set-SettingProp $s 'funnel' ([pscustomobject][ordered]@{ https_port = 443; target = 8675 }); Save-Settings $s
    $script:TailscaleExe = 'C:\ts\tailscale.exe'
    Add-ExtRule 'tailscale\.exe funnel status --json' (New-ExtResult -ExitCode 1 -Stderr 'failed to connect to local tailscaled; it doesn''t appear to be running')
    $r = Invoke-StateVerb -Opts @{}
    Assert-Equal 443 $r.Values['funnel_port'] 'still reported: the Funnel comes back with the service'
    Assert-Equal 443 (Read-Settings).funnel.https_port 'the record is kept'
    Assert-Match (Get-LogText) 'state: could not ask Tailscale; funnel record kept' 'the decision is logged'
    Assert-Match (Get-LogText) 'served=\s*\(tailscale did not answer \(exit 1' 'with the reason'
}

Test-Case 'state (design 18.2): installed=1 means an owned distro AND a recorded Linux install; state= keeps the settings state' {
    $vhd = New-Dir 'vhd'
    # 1. import finished (state installed, distro owned) but `cognita install` never exited 0: finish mode, installed=0
    $s = New-TestSettings -State 'installed' -Vhd $vhd
    Set-Distro -BasePath $vhd
    Add-ExtRule 'wsl\.exe --list --running --quiet' (New-ExtResult -Stdout '')
    $r1 = Invoke-StateVerb -Opts @{}
    Assert-Equal 0 $r1.Values['installed'] 'an interrupted or failed first install is not installed'
    Assert-Equal 'installed' $r1.Values['state'] 'settings state is installed'
    Assert-Equal 1 $r1.Values['owned'] 'owned'
    Assert-Equal '' $r1.Values['linux_version'] 'no version recorded'
    # 2. after `cognita install` exited 0
    Set-SettingProp $s 'linux_installed_version' '14.2.0'; Set-SettingProp $s 'admin_user' 'admin'; Save-Settings $s
    $r2 = Invoke-StateVerb -Opts @{}
    Assert-Equal 1 $r2.Values['installed'] 'installed'
    Assert-Equal '14.2.0' $r2.Values['linux_version'] 'linux_version'
    Assert-Equal 'admin' $r2.Values['admin_user'] 'admin_user'
    # 3. after a KEEP-DATA uninstall: state=uninstalled, distro and version kept, still installed=1, so Setup picks repair or update
    Set-SettingProp $s 'state' 'uninstalled'; Save-Settings $s
    $r3 = Invoke-StateVerb -Opts @{}
    Assert-Equal 1 $r3.Values['installed'] 'installed=1 after a keep-data uninstall'
    Assert-Equal 'uninstalled' $r3.Values['state'] 'and state= still says uninstalled'
    Assert-Equal '14.2.0' $r3.Values['linux_version'] 'version kept'
    Assert-Match ((Format-ResultLine 'ok' $r3.Values)) '^result=ok;installed=1;' 'on the result line'
    # 4. a version with no owned distro (unregistered by hand) or a foreign distro is NOT installed
    $script:LxssDistros = @()
    Assert-Equal 0 (Invoke-StateVerb -Opts @{}).Values['installed'] 'distro gone: not installed'
    Set-Distro -BasePath 'C:\Someone\Else'
    Assert-Equal 0 (Invoke-StateVerb -Opts @{}).Values['installed'] 'foreign distro: not installed'
}

Complete-Tests
