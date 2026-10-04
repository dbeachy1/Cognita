# test_keepalive.ps1 - launch-keepalive.vbs: relaunch backoff, reset, stop flag, settings regex (design 5.8, 14.2)
#
# The real script is run by cscript.exe. Two test hooks in the script keep the tests off the wall
# clock: COGNITA_KEEPALIVE_FAKE=1 makes its pauses log lines instead of sleeping and takes each run's
# length from <home>\fake-run-seconds.txt; COGNITA_WSL_EXE points it at a fake wsl (a .cmd that runs a
# small script). No real WSL is involved.
. (Join-Path $PSScriptRoot '..\CognitaWin.ps1') -NoMain
. (Join-Path $PSScriptRoot '_harness.ps1')

$script:Vbs = (Resolve-Path (Join-Path $PSScriptRoot '..\launch-keepalive.vbs')).Path

function New-FakeWsl {
    param([int[]]$Schedule)
    $home2 = $env:COGNITA_HOME
    [System.IO.File]::WriteAllText((Join-Path $home2 'schedule.txt'), (($Schedule | ForEach-Object { [string]$_ }) -join "`n"))
    $ps1 = @'
$h = $env:COGNITA_HOME
$n = 0
if (Test-Path (Join-Path $h 'runs.txt')) { $n = [int](Get-Content (Join-Path $h 'runs.txt')) }
$n++
Set-Content -Path (Join-Path $h 'runs.txt') -Value $n
Add-Content -Path (Join-Path $h 'args.txt') -Value ($args -join ' ')
$sched = @(Get-Content (Join-Path $h 'schedule.txt'))
Set-Content -Path (Join-Path $h 'fake-run-seconds.txt') -Value $sched[$n - 1]
if ($n -ge $sched.Count) { Set-Content -Path (Join-Path $h 'stopped') -Value 'stop' }
exit $n
'@
    [System.IO.File]::WriteAllText((Join-Path $home2 'fake-wsl.ps1'), $ps1)
    $cmd = "@echo off`r`npowershell.exe -NoProfile -ExecutionPolicy Bypass -File `"%~dp0fake-wsl.ps1`" %*`r`nexit /b %ERRORLEVEL%`r`n"
    [System.IO.File]::WriteAllText((Join-Path $home2 'fake-wsl.cmd'), $cmd)
    return (Join-Path $home2 'fake-wsl.cmd')
}

function Invoke-Keepalive {
    param([string]$WslExe)
    $saved = @{ F = $env:COGNITA_KEEPALIVE_FAKE; W = $env:COGNITA_WSL_EXE }
    $env:COGNITA_KEEPALIVE_FAKE = '1'
    $env:COGNITA_WSL_EXE = $WslExe
    try {
        $psi = New-Object System.Diagnostics.ProcessStartInfo
        $psi.FileName = Join-Path $env:SystemRoot 'System32\cscript.exe'
        $psi.Arguments = '//B //Nologo "' + $script:Vbs + '"'
        $psi.UseShellExecute = $false
        $psi.CreateNoWindow = $true
        $p = [System.Diagnostics.Process]::Start($psi)
        # A hang guard only: the script ends by itself when the fake wsl creates the stopped flag.
        if (-not $p.WaitForExit(120000)) { try { $p.Kill() } catch { }; throw 'keepalive script did not finish (hang guard)' }
        $code = $p.ExitCode
        $p.Dispose()
        return $code
    } finally {
        $env:COGNITA_KEEPALIVE_FAKE = $saved.F
        $env:COGNITA_WSL_EXE = $saved.W
    }
}
function Get-Delays {
    $log = [System.IO.File]::ReadAllText((Join-Path $env:COGNITA_HOME 'logs\keepalive.log'))
    return @([regex]::Matches($log, 'relaunching in (\d+)s') | ForEach-Object { $_.Groups[1].Value })
}
function Write-SettingsJson {
    param([string]$Distro, [string]$User)
    # PowerShell 5.1's ConvertTo-Json pads the colon with two spaces; the script must cope.
    [System.IO.File]::WriteAllText((Join-Path $env:COGNITA_HOME 'settings.json'), ("{`n  `"schema`":  1,`n  `"distro`":  `"$Distro`",`n  `"linux_user`":  `"$User`",`n  `"vhd_dir`":  `"C:\\Users\\me\\AppData\\Local\\Cognita\\wsl`"`n}`n"))
}

Test-Case 'keepalive: relaunch waits 5, 10, 20, 40, 60, then 5 again after a run longer than 10 minutes, then 10' {
    Write-SettingsJson -Distro 'custom-distro' -User 'custom-user'
    $wsl = New-FakeWsl -Schedule @(1, 2, 3, 4, 5, 700, 1, 2)
    $code = Invoke-Keepalive -WslExe $wsl
    Assert-Equal 0 $code 'the script exits 0 when the stopped flag appears'
    Assert-Equal '5,10,20,40,60,5,10' ((Get-Delays) -join ',') 'the backoff sequence'
    Assert-Equal 8 ([int](Get-Content (Join-Path $env:COGNITA_HOME 'runs.txt'))) 'eight runs, no more after the stopped flag'
    $log = [System.IO.File]::ReadAllText((Join-Path $env:COGNITA_HOME 'logs\keepalive.log'))
    Assert-Match $log 'keepalive ended \(exit 6, ran 700s\); relaunching in 5s' 'a line per relaunch with the exit code and the run length'
    Assert-Match $log 'stopped flag is present; exiting' 'the exit is logged'
    Assert-Match $log '^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} keepalive loop started distro=custom-distro user=custom-user' 'first line: local time stamp, distro and user'
}

Test-Case 'keepalive: exactly 600 s does not reset the backoff, 601 s does' {
    Write-SettingsJson -Distro 'Cognita' -User 'cognita'
    $wsl = New-FakeWsl -Schedule @(1, 1, 1, 1, 1, 600, 601, 1)
    [void](Invoke-Keepalive -WslExe $wsl)
    Assert-Equal '5,10,20,40,60,60,5' ((Get-Delays) -join ',') 'the delay caps at 60 and only a run over 600 s resets it'
}

Test-Case 'keepalive: the distro name and Linux user come from settings.json' {
    Write-SettingsJson -Distro 'custom-distro' -User 'custom-user'
    $wsl = New-FakeWsl -Schedule @(1)
    [void](Invoke-Keepalive -WslExe $wsl)
    $args1 = @(Get-Content (Join-Path $env:COGNITA_HOME "args.txt"))[0]
    Assert-Equal '-d custom-distro -u custom-user --exec /usr/local/libexec/cognita-keepalive' $args1 'wsl.exe arguments'
}

Test-Case 'keepalive: without settings.json the defaults are Cognita and cognita' {
    $wsl = New-FakeWsl -Schedule @(1)
    [void](Invoke-Keepalive -WslExe $wsl)
    Assert-Equal '-d Cognita -u cognita --exec /usr/local/libexec/cognita-keepalive' (@(Get-Content (Join-Path $env:COGNITA_HOME "args.txt"))[0]) 'defaults'
}

Test-Case 'keepalive: a stopped flag that exists at the start means no wsl run at all' {
    Write-SettingsJson -Distro 'Cognita' -User 'cognita'
    $wsl = New-FakeWsl -Schedule @(1, 1)
    [System.IO.File]::WriteAllText((Join-Path $env:COGNITA_HOME 'stopped'), 'stop')
    $code = Invoke-Keepalive -WslExe $wsl
    Assert-Equal 0 $code 'exit 0'
    Assert-False (Test-Path (Join-Path $env:COGNITA_HOME 'runs.txt')) 'wsl never ran'
    Assert-Match ([System.IO.File]::ReadAllText((Join-Path $env:COGNITA_HOME 'logs\keepalive.log'))) 'stopped flag present; exiting' 'logged'
}

Test-Case 'keepalive: the log is appended to across runs of the script, never rewritten' {
    Write-SettingsJson -Distro 'Cognita' -User 'cognita'
    [void](New-Dir 'home\logs')
    [System.IO.File]::WriteAllText((Join-Path $env:COGNITA_HOME 'logs\keepalive.log'), "2026-01-01 00:00:00 an earlier session`n")
    $wsl = New-FakeWsl -Schedule @(1)
    [void](Invoke-Keepalive -WslExe $wsl)
    $log = [System.IO.File]::ReadAllText((Join-Path $env:COGNITA_HOME 'logs\keepalive.log'))
    Assert-True ($log.StartsWith('2026-01-01 00:00:00 an earlier session')) 'the old line is still first'
    Assert-Match $log 'keepalive loop started' 'the new session was appended'
}

Test-Case 'keepalive script: every line is ASCII and LF (design: works with LF, no non-ASCII)' {
    $bytes = [System.IO.File]::ReadAllBytes($script:Vbs)
    Assert-Equal 0 @($bytes | Where-Object { $_ -gt 127 }).Count 'ASCII only'
    Assert-Equal 0 @($bytes | Where-Object { $_ -eq 13 }).Count 'no CR'
}

Complete-Tests
