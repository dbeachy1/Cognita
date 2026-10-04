# test_checks.ps1 - the check table (design 5.3, 14.2)
. (Join-Path $PSScriptRoot '..\CognitaWin.ps1') -NoMain
. (Join-Path $PSScriptRoot '_harness.ps1')

function Set-WslPresent {
    Add-ExtRule 'wsl\.exe --version' (New-ExtResult -Stdout "WSL version: 2.7.14.0`nKernel version: 6.6.87.2-1`n")
}
function Get-Failures {
    param($Results)
    return ,@($Results | Where-Object { $_.State -eq 'fail' } | ForEach-Object { $_.Id })
}
function Invoke-Checks {
    param([string]$Phase = 'preflight', [hashtable]$Opts = @{}, $Settings = $null)
    $r = Get-CheckResults -Phase $Phase -Opts $Opts -Settings $Settings
    return ,@($r)
}

Test-Case 'check: a healthy PC passes every check, WSL present' {
    Set-WslPresent
    $r = Invoke-Checks
    Assert-Equal 0 (Get-Failures $r).Count 'no failures'
    Assert-Equal 'wsl=present' ($r | Where-Object { $_.Id -eq 'wsl' }).Message 'wsl state'
}

Test-Case 'check: Windows older than build 22000, or 32-bit, fails with the design message' {
    Set-WslPresent
    $script:OsBuild = 19045
    $f = (Invoke-Checks) | Where-Object { $_.Id -eq 'windows' }
    Assert-Equal 'fail' $f.State 'old build'
    Assert-Equal 'Cognita needs 64-bit Windows 11.' $f.Message 'message'
    $script:OsBuild = 26100; $script:Os64 = $false
    Assert-Equal 'fail' ((Invoke-Checks) | Where-Object { $_.Id -eq 'windows' }).State '32-bit'
}

Test-Case 'check: virtualization is advisory only (a warning, never a failure)' {
    Set-WslPresent
    $script:VirtHyper = $false; $script:VirtFw = $false
    $r = Invoke-Checks
    $v = $r | Where-Object { $_.Id -eq 'virtualization' }
    Assert-Equal 'warn' $v.State 'warning'
    Assert-Equal 0 (Get-Failures $r).Count 'not a failure'
}

Test-Case 'check: WSL missing (inbox stub) and old WSL are states, not failures' {
    Add-ExtRule 'wsl\.exe --version' (New-ExtResult -ExitCode 1 -Stdout 'Windows Subsystem for Linux is not installed.')
    Add-ExtRule 'wsl\.exe --status' (New-ExtResult -ExitCode 1 -Stdout 'Windows Subsystem for Linux is not installed.')
    $r = Invoke-Checks
    Assert-Equal 'wsl=missing' ($r | Where-Object { $_.Id -eq 'wsl' }).Message 'missing'
    Assert-Equal 0 (Get-Failures $r).Count 'not a failure'
}

Test-Case 'check: wsl --version unknown but --status works is "old"; a version below 2.0 is "old"' {
    Add-ExtRule 'wsl\.exe --version' (New-ExtResult -ExitCode 1 -Stdout 'Invalid command line option: --version')
    Add-ExtRule 'wsl\.exe --status' (New-ExtResult -ExitCode 0 -Stdout 'Default Distribution: Ubuntu')
    Assert-Equal 'old' (Get-WslState).State 'status works'
    $script:ExtRules.Clear()
    Add-ExtRule 'wsl\.exe --version' (New-ExtResult -ExitCode 0 -Stdout 'WSL version: 1.2.5.0')
    $s = Get-WslState
    Assert-Equal 'old' $s.State 'below 2.0'
    Assert-Equal '1.2.5' $s.Version 'version parsed'
}

Test-Case 'check: wsl.exe that cannot be started is "missing"; UTF-16 style NULs are ignored' {
    Add-ExtRule 'wsl\.exe --version' (New-ExtResult -ExitCode -1 -StartError 'The system cannot find the file specified')
    Assert-Equal 'missing' (Get-WslState).State 'no wsl.exe'
    $script:ExtRules.Clear()
    $nul = [string][char]0
    Add-ExtRule 'wsl\.exe --version' (New-ExtResult -ExitCode 0 -Stdout ("W${nul}S${nul}L${nul} ${nul}v${nul}e${nul}r${nul}s${nul}i${nul}o${nul}n${nul}:${nul} ${nul}2${nul}.${nul}7${nul}.${nul}1${nul}4${nul}"))
    Assert-Equal 'present' (Get-WslState).State 'NULs stripped'
}

Test-Case 'check: RebootPending fails; not evaluated right after our own wsl-install' {
    Set-WslPresent
    $script:RestartPending = $true
    $f = (Invoke-Checks) | Where-Object { $_.Id -eq 'restart' }
    Assert-Equal 'fail' $f.State 'pending'
    Assert-Equal 'Windows is waiting for a restart.' $f.Message 'message'
    Assert-Equal 'Restart, then run Setup again.' $f.Fix 'fix'
    $r = Invoke-Checks -Opts @{ 'after-wsl-install' = $true }
    Assert-Equal 0 @($r | Where-Object { $_.Id -eq 'restart' }).Count 'skipped'
}

Test-Case 'check: .wslconfig localhostForwarding=false fails; true, other sections, mirrored networking are fine' {
    Set-WslPresent
    $script:WslConfig = "[wsl2]`nlocalhostForwarding=false`n"
    $f = (Invoke-Checks) | Where-Object { $_.Id -eq 'wslconfig' }
    Assert-Equal 'fail' $f.State 'false'
    Assert-Match $f.Fix 'Remove localhostForwarding=false' 'fix'
    $script:WslConfig = "[wsl2]`nlocalhostForwarding = FALSE`n"
    Assert-Equal 'fail' ((Invoke-Checks) | Where-Object { $_.Id -eq 'wslconfig' }).State 'spaces and case'
    $script:WslConfig = "[wsl2]`nlocalhostForwarding=true`nnetworkingMode=mirrored`n"
    Assert-Equal 'ok' ((Invoke-Checks) | Where-Object { $_.Id -eq 'wslconfig' }).State 'true + mirrored'
    $script:WslConfig = "[experimental]`nlocalhostForwarding=false`n"
    Assert-Equal 'ok' ((Invoke-Checks) | Where-Object { $_.Id -eq 'wslconfig' }).State 'other section'
    $script:WslConfig = "[wsl2]`n# localhostForwarding=false`n"
    Assert-Equal 'ok' ((Invoke-Checks) | Where-Object { $_.Id -eq 'wslconfig' }).State 'commented out'
}

Test-Case 'check: a foreign distro named Cognita fails; ours (disk folder matches) passes' {
    Set-WslPresent
    $vhd = Join-Path $script:TestDir 'vhd'
    $s = New-TestSettings -State 'import-pending' -Vhd $vhd
    $script:LxssDistros = @([pscustomobject]@{ Guid = '{g1}'; Name = 'Cognita'; BasePath = 'C:\Somewhere\Else' })
    $f = (Invoke-Checks -Settings $s) | Where-Object { $_.Id -eq 'distro' }
    Assert-Equal 'fail' $f.State 'foreign'
    Assert-Equal 'A WSL distro named Cognita already exists and was not created by this Setup.' $f.Message 'message'
    Assert-Equal 'Rename or remove it, then run Setup again.' $f.Fix 'fix'
    $script:LxssDistros = @([pscustomobject]@{ Guid = '{g1}'; Name = 'Cognita'; BasePath = ('\\?\' + $vhd + '\') })
    Assert-Equal 'ok' ((Invoke-Checks -Settings $s) | Where-Object { $_.Id -eq 'distro' }).State 'ours, with \\?\ prefix and trailing slash'
    $script:LxssDistros = @()
    Assert-Equal 'ok' ((Invoke-Checks -Settings $null) | Where-Object { $_.Id -eq 'distro' }).State 'no distro'
    $script:LxssDistros = @([pscustomobject]@{ Guid = '{g1}'; Name = 'Cognita'; BasePath = 'C:\x' })
    Assert-Equal 'fail' ((Invoke-Checks -Settings $null) | Where-Object { $_.Id -eq 'distro' }).State 'a distro but no record on this PC'
}

Test-Case 'check: Docker Desktop says nothing unless its WSL integration is on for Cognita' {
    Set-WslPresent
    $script:DockerDesktop = $true
    $script:DockerDesktopIntegrated = @('Ubuntu')
    $r = Invoke-Checks
    Assert-Equal 'ok' ($r | Where-Object { $_.Id -eq 'docker' }).State 'installed, another distro integrated: nothing to say'
    $script:DockerDesktopIntegrated = @('Ubuntu', 'Cognita')
    $r = Invoke-Checks
    $d = $r | Where-Object { $_.Id -eq 'docker' }
    Assert-Equal 'fail' $d.State 'Cognita integrated: refused'
    Assert-Match $d.Message 'WSL integration is turned on for Cognita' 'message'
    Assert-Match $d.Fix 'untick Cognita' 'fix'
    $script:DockerDesktopIntegrated = @('Cognita-Windows')
    $s = [pscustomobject]@{ distro = 'Cognita-Windows' }
    Assert-Equal 'fail' ((Invoke-Checks -Settings $s) | Where-Object { $_.Id -eq 'docker' }).State 'the settings distro name is the one checked'
    $script:DockerDesktop = $false
    $script:DockerDesktopIntegrated = @('Cognita')
    Assert-Equal 'ok' ((Invoke-Checks) | Where-Object { $_.Id -eq 'docker' }).State 'not installed'
}

Test-Case 'check: memory needs 16 GB, or an 8 GB .wslconfig share' {
    Set-WslPresent
    $script:MemBytes = [int64]8000000000
    $f = (Invoke-Checks) | Where-Object { $_.Id -eq 'memory' }
    Assert-Equal 'fail' $f.State '8 GB PC'
    Assert-Equal 'Cognita needs a PC with at least 16 GB of memory.' $f.Message 'message'
    $script:MemBytes = [int64]17000000000
    Assert-Equal 'ok' ((Invoke-Checks) | Where-Object { $_.Id -eq 'memory' }).State '16 GB PC (about 17e9 bytes reported)'
    $script:MemBytes = [int64]12000000000; $script:WslConfig = "[wsl2]`nmemory=8GB`n"
    Assert-Equal 'ok' ((Invoke-Checks) | Where-Object { $_.Id -eq 'memory' }).State '12 GB PC with WSL raised to 8 GB'
    $script:MemBytes = [int64]34000000000; $script:WslConfig = "[wsl2]`nmemory=4GB`n"
    Assert-Equal 'fail' ((Invoke-Checks) | Where-Object { $_.Id -eq 'memory' }).State '32 GB PC with WSL capped at 4 GB'
}

Test-Case 'check: preflight does not look at disk or ports' {
    Set-WslPresent
    function Get-ListeningPorts { throw 'ports must not be read in preflight' }
    $script:FreeBytes = [int64]1
    $r = Invoke-Checks -Phase 'preflight'
    Assert-Equal 0 @($r | Where-Object { $_.Id -eq 'disk' -or $_.Id -eq 'ports' }).Count 'no disk or ports result'
}

Test-Case 'check final: disk needs --disk-bytes (computed by Setup) free at --data-dir' {
    Set-WslPresent
    # Setup passes the requirement: e.g. 4 x 4 GB downloads + 2 GB models + 3.5 GB + 1 GB = 22.5 GB -> "23 GB"
    $opts = @{ 'disk-bytes' = [string]([int64](22.5 * 1GB)); 'data-dir' = 'C:\Cognita\wsl' }
    $script:FreeBytes = [int64](22GB)
    $f = (Invoke-Checks -Phase 'final' -Opts $opts) | Where-Object { $_.Id -eq 'disk' }
    Assert-Equal 'fail' $f.State '22 GB free is short'
    Assert-Equal 'Setup needs 23 GB free on C:\.' $f.Message 'message names the size and the drive'
    Assert-Match $f.Fix 'choose another data location under Advanced' 'fix'
    $script:FreeBytes = [int64](23GB)
    Assert-Equal 'ok' ((Invoke-Checks -Phase 'final' -Opts $opts) | Where-Object { $_.Id -eq 'disk' }).State '23 GB free is enough'
    Assert-Match (Get-LogText) 'check disk: data dir=C:\\Cognita\\wsl free=\d+ need=\d+' 'the numbers are logged'
    # No --disk-bytes: the fixed 4.5 GB floor, and the decision is logged.
    $script:FreeBytes = [int64](4GB)
    Assert-Equal 'fail' ((Invoke-Checks -Phase 'final' -Opts @{ 'data-dir' = 'C:\Cognita\wsl' }) | Where-Object { $_.Id -eq 'disk' }).State 'below the floor'
    Assert-Match (Get-LogText) 'no --disk-bytes given, using the 4.5 GB floor' 'logged'
}

Test-Case 'check final: a port in use fails naming the program; our own install''s ports do not' {
    Set-WslPresent
    $script:ListeningPorts = @([pscustomobject]@{ Port = 8675; ProcessId = 4321; Process = 'python' })
    $f = (Invoke-Checks -Phase 'final') | Where-Object { $_.Id -eq 'ports' }
    Assert-Equal 'fail' $f.State 'port taken'
    Assert-Equal 'Port 8675 is in use by python.' $f.Message 'message'
    Assert-Equal 'Choose other ports under Advanced.' $f.Fix 'fix'
    $s = New-TestSettings -State 'installed'
    $r = Invoke-Checks -Phase 'final' -Settings $s
    Assert-Equal 'ok' ($r | Where-Object { $_.Id -eq 'ports' }).State 'this install''s own port is not a conflict'
    Assert-Match (Get-LogText) 'port 8675 listened by \[python\] pid=4321 ours=True' 'decision logged'
    $r2 = Invoke-Checks -Phase 'final' -Opts @{ 'mcp-port' = '9000'; 'admin-port' = '9001' } -Settings $s
    Assert-Equal 'ok' ($r2 | Where-Object { $_.Id -eq 'ports' }).State 'other ports are free'
}

Test-Case 'check verb: all checks run and failures are listed together, result line carries the counts' {
    Add-ExtRule 'wsl\.exe --version' (New-ExtResult -Stdout 'WSL version: 2.7.14.0')
    $script:OsBuild = 19045
    $script:MemBytes = [int64]4000000000
    $script:RestartPending = $true
    $o = ConvertFrom-HelperArgs @('--phase', 'preflight')
    $r = Invoke-CheckVerb -Opts $o.Opts
    Assert-Equal 'failed' $r.Status 'status'
    Assert-Equal 3 $r.Values['failures'] 'three failures reported together'
    Assert-Equal 'present' $r.Values['wsl'] 'wsl key'
    $all = Get-ProgressObjects
    $failed = @($all | Where-Object { $_.state -eq 'failed' -and $_.stage -like 'check.*' })
    Assert-Equal 3 $failed.Count 'one failed progress line per failure, each with message and fix'
    foreach ($f in $failed) { Assert-True ($f.message -and $f.fix) 'message and fix present' }
    Assert-Match (Get-LogText) 'check summary: phase=preflight failures=3' 'summary logged'
}

Test-Case 'check verb (design 19.8 item 19): a preflight that sees WSL present clears resume=after-wsl; WSL still missing, the final phase and other resume values leave it alone' {
    $s = New-TestSettings -State 'new'
    Set-SettingProp $s 'resume' 'after-wsl'; Save-Settings $s
    # WSL is still missing (the restart has not happened): the marker stays
    Add-ExtRule 'wsl\.exe --version' (New-ExtResult -ExitCode 1 -Stdout 'not installed')
    Add-ExtRule 'wsl\.exe --status' (New-ExtResult -ExitCode 1 -Stdout 'not installed')
    $r0 = Invoke-CheckVerb -Opts (ConvertFrom-HelperArgs @('--phase', 'preflight')).Opts
    Assert-Equal 'after-wsl' (Read-Settings).resume 'WSL missing: the marker stays'
    Assert-Match (Get-LogText) 'check: resume marker \[after-wsl\] left as it is \(phase=preflight wsl=' 'the skip is logged with its values'
    # WSL present, but the FINAL phase: not its business
    $script:ExtRules.Clear(); $script:LogLines.Clear()
    Set-WslPresent
    [void](Invoke-CheckVerb -Opts (ConvertFrom-HelperArgs @('--phase', 'final')).Opts)
    Assert-Equal 'after-wsl' (Read-Settings).resume 'the final phase leaves it alone'
    # WSL present in the preflight: cleared, and saved
    $script:LogLines.Clear()
    $r = Invoke-CheckVerb -Opts (ConvertFrom-HelperArgs @('--phase', 'preflight')).Opts
    Assert-Equal 'present' $r.Values['wsl'] 'wsl present'
    Assert-True ($null -eq (Read-Settings).resume) 'the resume marker is cleared in settings.json'
    Assert-Match (Get-LogText) 'check: WSL is present, so the resume=after-wsl marker was cleared' 'the decision is logged'
    Assert-Equal 'new' (Read-Settings).state 'nothing else in the settings changed'
    # a marker with another value is not this check's to clear
    $s2 = Read-Settings; Set-SettingProp $s2 'resume' 'something-else'; Save-Settings $s2
    [void](Invoke-CheckVerb -Opts (ConvertFrom-HelperArgs @('--phase', 'preflight')).Opts)
    Assert-Equal 'something-else' (Read-Settings).resume 'only after-wsl is cleared'
    # no settings at all: no crash, nothing written
    Remove-Item -LiteralPath (Get-SettingsPath) -Force
    [void](Invoke-CheckVerb -Opts (ConvertFrom-HelperArgs @('--phase', 'preflight')).Opts)
    Assert-False (Test-Path -LiteralPath (Get-SettingsPath)) 'no settings file was invented'
}

Test-Case 'check verb: rejects an unknown phase' {
    $o = ConvertFrom-HelperArgs @('--phase', 'bogus')
    Assert-Equal 'failed' (Invoke-CheckVerb -Opts $o.Opts).Status 'failed'
}

Complete-Tests
