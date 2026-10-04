# test_install.ps1 - progress relay, install, update (design 5.1, 7.1, 7.2, 7.3, 14.2)
. (Join-Path $PSScriptRoot '..\CognitaWin.ps1') -NoMain
. (Join-Path $PSScriptRoot '_harness.ps1')

$script:Pw = 'p' + (Get-Utf8 0xE4) + 'ssw' + (Get-Utf8 0xF6) + 'rd' + (Get-Utf8 0x20AC)

$script:StatusJson = '{"installed": true, "running": true, "version": "14.1.0", "admin_url": "http://127.0.0.1:8676", "mcp_url": "http://127.0.0.1:8675", "public_url": null, "workspace": "on", "acceleration": "cpu"}'
$script:L1 = '{"schema": 1, "time": "2026-03-01T09:00:01", "stage": "checks", "title": "Checking this machine", "state": "start"}'
$script:L2 = '{"schema": 1, "time": "2026-03-01T09:00:02", "stage": "images", "title": "Downloading Cognita images", "state": "progress", "bytes_done": 1000, "bytes_total": 4000}'
$script:L3 = '{"schema": 1, "time": "2026-03-01T09:00:03", "stage": "finish", "title": "Cognita is installed", "state": "done"}'

function Write-ProgressFileLines {
    param([string]$Text)
    [System.IO.File]::AppendAllText($script:ProgressFile, $Text)
}

function New-FakeImage {
    $p = Join-Path $script:TestDir 'cognita-wsl.tar.gz'
    [System.IO.File]::WriteAllBytes($p, [byte[]](1..200))
    $sha = [System.Security.Cryptography.SHA256]::Create()
    $hash = (($sha.ComputeHash([System.IO.File]::ReadAllBytes($p)) | ForEach-Object { $_.ToString('x2') }) -join '')
    return @{ Path = $p; Sha = $hash }
}

# ---- progress relay ----------------------------------------------------------------------------
Test-Case 'relay: Linux progress lines are relayed unchanged and in order; a half-written line waits for its newline' {
    $s = New-TestSettings
    Add-ExtRule '/usr/local/bin/cognita install' {
        param($c)
        Write-ProgressFileLines ($script:L1 + "`n" + $script:L2 + "`n" + '{"schema": 1, "time": "2026-03-01T09:00:03", "sta')
        Invoke-ClockSleep 1000; & $c.OnPoll
        $script:afterFirst = (Get-OutLines).Count
        Write-ProgressFileLines ('ge": "finish", "title": "Cognita is installed", "state": "done"}' + "`n")
        Invoke-ClockSleep 1000; & $c.OnPoll
        New-ExtResult
    }
    $r = Invoke-CognitaLinux -Settings $s -CliArgs @('install', '--non-interactive') -Stage 'cognita'
    Assert-Equal 0 $r.ExitCode 'exit'
    Assert-Equal 2 $script:afterFirst 'only the two complete lines so far'
    $out = Get-OutLines
    Assert-Equal $script:L1 $out[0] 'line 1 byte for byte'
    Assert-Equal $script:L2 $out[1] 'line 2 byte for byte'
    Assert-Equal $script:L3 $out[2] 'line 3, once its newline arrived, byte for byte'
    $call = (Get-ExtCallsMatching 'cognita install')[0]
    $pfArg = $call.Arguments[-1]
    Assert-Equal (ConvertTo-WslMntPath $script:ProgressFile) $pfArg 'the progress file is passed as its /mnt/<drive> path'
    Assert-Equal '--progress-file' $call.Arguments[-2] 'flag'
    Assert-False (Test-Path $script:ProgressFile) 'the temp file is removed afterwards'
}

Test-Case 'relay: a line that is not JSON is logged and not relayed; a failed line is remembered' {
    $s = New-TestSettings
    $failed = '{"schema": 1, "time": "2026-03-01T09:00:04", "stage": "proof", "title": "Proof", "state": "failed", "message": "The self test failed at step 12.", "fix": "Run: cognita install"}'
    Add-ExtRule '/usr/local/bin/cognita install' {
        param($c)
        Write-ProgressFileLines ("not json at all`n" + $script:L1 + "`n" + $failed + "`n")
        Invoke-ClockSleep 1000; & $c.OnPoll
        New-ExtResult -ExitCode 1
    }
    $r = Invoke-CognitaLinux -Settings $s -CliArgs @('install') -Stage 'cognita'
    Assert-Equal 2 (Get-OutLines).Count 'two JSON lines relayed'
    Assert-Equal 'The self test failed at step 12. Run: cognita install' $r.Summary 'summary is the failed line''s message and fix'
    Assert-Match (Get-LogText) 'progress line is not JSON, not relayed: not json at all' 'logged'
}

Test-Case 'relay: nothing from Linux for 5 s means a heartbeat progress line from the helper' {
    $s = New-TestSettings
    Add-ExtRule '/usr/local/bin/cognita install' {
        param($c)
        foreach ($i in 1..12) { Invoke-ClockSleep 1000; & $c.OnPoll }
        New-ExtResult
    }
    [void](Invoke-CognitaLinux -Settings $s -CliArgs @('install') -Stage 'cognita')
    $beats = @((Get-ProgressObjects) | Where-Object { $_.stage -eq 'cognita' -and $_.state -eq 'progress' })
    Assert-Equal 2 $beats.Count 'at 5 s and 10 s'
    Assert-Match $beats[0].message 'Still working, 5 s so far' 'message'
}

Test-Case 'relay (design 18.5): a heartbeat carries the LAST title the Linux side relayed, and the caller''s title before any line has arrived; never "Cognita is still working"' {
    $s = New-TestSettings
    Add-ExtRule '/usr/local/bin/cognita install' {
        param($c)
        foreach ($i in 1..6) { Invoke-ClockSleep 1000; & $c.OnPoll }
        Write-ProgressFileLines ($script:L2 + "`n")
        Invoke-ClockSleep 1000; & $c.OnPoll
        foreach ($i in 1..6) { Invoke-ClockSleep 1000; & $c.OnPoll }
        New-ExtResult
    }
    [void](Invoke-CognitaLinux -Settings $s -CliArgs @('install') -Stage 'cognita' -Title 'Installing Cognita')
    $beats = @((Get-ProgressObjects) | Where-Object { $_.state -eq 'progress' -and $_.message -like 'Still working*' })
    Assert-Equal 2 $beats.Count 'two heartbeats'
    Assert-Equal 'Installing Cognita' $beats[0].title 'before any Linux line: the title the caller gave'
    Assert-Equal 'Downloading Cognita images' $beats[1].title 'after: the last relayed title'
    # Design 21.3: the stage follows the same rule as the title (Setup shows Skip self-tests only while
    # the current stage is `proof`, so a heartbeat must not flip it to the caller's stage).
    Assert-Equal 'cognita' $beats[0].stage 'before any Linux line: the caller''s stage'
    Assert-Equal 'images' $beats[1].stage 'after: the last relayed stage (L2 is stage images)'
    Assert-Equal 0 @((Get-ProgressObjects) | Where-Object { $_.title -eq 'Cognita is still working' }).Count 'the generic title is gone'
}

Test-Case 'relay (P1, design 19.6 item 13): a heartbeat carries the last relayed byte counts; a SAME-title warning line without counts keeps them; a NEW title without counts clears them' {
    $s = New-TestSettings
    # The same title as L2 ("Downloading Cognita images"), a warning with no byte counts: the real Linux
    # side sends such a line in the middle of a download. The old code cleared the counts on it.
    $script:SameTitleWarning = '{"schema": 1, "time": "2026-03-01T09:00:08", "stage": "images", "title": "Downloading Cognita images", "state": "warning", "message": "a mirror was slow"}'
    Add-ExtRule '/usr/local/bin/cognita install' {
        param($c)
        Write-ProgressFileLines ($script:L2 + "`n")
        Invoke-ClockSleep 1000; & $c.OnPoll
        foreach ($i in 1..6) { Invoke-ClockSleep 1000; & $c.OnPoll }
        Write-ProgressFileLines ($script:SameTitleWarning + "`n")
        Invoke-ClockSleep 1000; & $c.OnPoll
        foreach ($i in 1..6) { Invoke-ClockSleep 1000; & $c.OnPoll }
        Write-ProgressFileLines ($script:L3 + "`n")
        Invoke-ClockSleep 1000; & $c.OnPoll
        foreach ($i in 1..6) { Invoke-ClockSleep 1000; & $c.OnPoll }
        New-ExtResult
    }
    [void](Invoke-CognitaLinux -Settings $s -CliArgs @('install') -Stage 'cognita' -Title 'Installing Cognita')
    $beats = @((Get-ProgressObjects) | Where-Object { $_.message -like 'Still working*' })
    Assert-Equal 3 $beats.Count 'three heartbeats'
    Assert-Equal 1000 ([int64]$beats[0].bytes_done) 'the download heartbeat keeps bytes_done'
    Assert-Equal 4000 ([int64]$beats[0].bytes_total) 'and bytes_total, so the bar does not reset'
    Assert-Equal 1000 ([int64]$beats[1].bytes_done) 'after a same-title warning without counts: bytes_done is still kept'
    Assert-Equal 4000 ([int64]$beats[1].bytes_total) 'and bytes_total'
    Assert-Equal 'Downloading Cognita images' $beats[1].title 'the title did not change'
    Assert-True ($null -eq $beats[2].bytes_total) 'after a new title without counts, no stale bytes'
    Assert-Match (Get-LogText) 'linux progress: same title \[Downloading Cognita images\] without counts; kept 1000/4000 bytes' 'the keep decision is logged with its values'
    Assert-Match (Get-LogText) 'linux progress: new title \[Cognita is installed\] without counts; remembered byte counts cleared' 'the clear decision is logged'
}

Test-Case 'relay: human mode shows the Linux lines as text' {
    $script:HumanMode = $true
    Write-RelayedLine $script:L1
    Write-RelayedLine '{"schema": 1, "time": "t", "stage": "x", "title": "Pulling", "state": "failed", "message": "no route", "fix": "check network"}'
    $t = (Get-OutLines) -join "`n"
    Assert-Match $t 'Checking this machine' 'start line'
    Assert-Match $t 'FAILED: Pulling' 'failed line'
    Assert-Match $t 'check network' 'fix'
}

# ---- skip the self-tests (design 21.3) ---------------------------------------------------------------
$script:ProofStart = '{"schema": 1, "time": "2026-03-01T09:00:05", "stage": "proof", "title": "Running self-tests to verify the installation", "state": "start"}'
$script:StoppingMessage = 'Stopping the self-tests...'

Test-Case 'skip (design 21.3): a request flag that appears during the proof stage creates the skip file ONCE, emits ONE "Stopping the self-tests..." proof line, and both files are gone after the run' {
    $s = New-TestSettings
    $skipFile = $script:ProgressFile + '.skip'
    Add-ExtRule '/usr/local/bin/cognita install' {
        param($c)
        Write-ProgressFileLines ($script:ProofStart + "`n")
        Invoke-ClockSleep 1000; & $c.OnPoll
        $script:skipBefore = Test-Path -LiteralPath ($script:ProgressFile + '.skip')
        [System.IO.File]::WriteAllBytes((Get-SkipRequestPath), [byte[]]@())
        Invoke-ClockSleep 1000; & $c.OnPoll
        $script:skipAfter = Test-Path -LiteralPath ($script:ProgressFile + '.skip')
        $script:skipSize = (Get-Item -LiteralPath ($script:ProgressFile + '.skip')).Length
        # more polls with the request flag still there: no second file, no second line
        Invoke-ClockSleep 1000; & $c.OnPoll
        Invoke-ClockSleep 1000; & $c.OnPoll
        $script:stoppingLines = @((Get-ProgressObjects) | Where-Object { $_.message -eq $script:StoppingMessage }).Count
        # a heartbeat after the request still says the stage is `proof` (Setup keys the button on it)
        Invoke-ClockSleep 6000; & $c.OnPoll
        New-ExtResult
    }
    $r = Invoke-CognitaLinux -Settings $s -CliArgs @('install') -Stage 'cognita' -Title 'Installing Cognita'
    Assert-Equal 0 $r.ExitCode 'exit'
    Assert-False $script:skipBefore 'no skip file before the request'
    Assert-True $script:skipAfter 'the skip file exists after the request'
    Assert-Equal 0 $script:skipSize 'the skip file is empty'
    Assert-Equal 1 $script:stoppingLines 'exactly one Stopping line'
    Assert-True $r.SkipRequested 'SkipRequested is returned'
    $stop = @((Get-ProgressObjects) | Where-Object { $_.message -eq $script:StoppingMessage })[0]
    Assert-Equal 'proof' $stop.stage 'the line is stage proof'
    Assert-Equal 'progress' $stop.state 'state progress'
    $beats = @((Get-ProgressObjects) | Where-Object { $_.message -like 'Still working*' })
    Assert-Equal 'proof' $beats[-1].stage 'a heartbeat after the request keeps stage proof'
    Assert-False (Test-Path -LiteralPath $skipFile) 'the skip file is removed after the run'
    Assert-False (Test-Path -LiteralPath (Get-SkipRequestPath)) 'the request flag is removed after the run'
    Assert-Match (Get-LogText) 'self-test skip requested by Setup; skip file .*progress\.jsonl\.skip created' 'the decision is logged with the path'
    Assert-Equal 1 @([regex]::Matches((Get-LogText), 'skip file .* created')).Count 'created once'
}

Test-Case 'skip (design 21.3): no request flag means no skip file and no Stopping line' {
    $s = New-TestSettings
    Add-ExtRule '/usr/local/bin/cognita install' {
        param($c)
        Write-ProgressFileLines ($script:ProofStart + "`n")
        foreach ($i in 1..3) { Invoke-ClockSleep 1000; & $c.OnPoll }
        $script:skipSeen = Test-Path -LiteralPath ($script:ProgressFile + '.skip')
        New-ExtResult
    }
    $r = Invoke-CognitaLinux -Settings $s -CliArgs @('install') -Stage 'cognita'
    Assert-False $script:skipSeen 'no skip file'
    Assert-False $r.SkipRequested 'SkipRequested false'
    Assert-Equal 0 @((Get-ProgressObjects) | Where-Object { $_.message -eq $script:StoppingMessage }).Count 'no Stopping line'
    Assert-NotMatch (Get-LogText) 'self-test skip requested' 'nothing logged about a request'
}

Test-Case 'skip (design 21.3): a stale request flag (and a stale skip file) from an earlier run is removed at the start, logged, and never skips this run' {
    $s = New-TestSettings
    [System.IO.File]::WriteAllBytes((Get-SkipRequestPath), [byte[]]@())
    [System.IO.File]::WriteAllBytes(($script:ProgressFile + '.skip'), [byte[]]@())
    Add-ExtRule '/usr/local/bin/cognita install' {
        param($c)
        $script:flagAtStart = Test-Path -LiteralPath (Get-SkipRequestPath)
        $script:skipAtStart = Test-Path -LiteralPath ($script:ProgressFile + '.skip')
        Write-ProgressFileLines ($script:ProofStart + "`n")
        foreach ($i in 1..2) { Invoke-ClockSleep 1000; & $c.OnPoll }
        $script:skipLater = Test-Path -LiteralPath ($script:ProgressFile + '.skip')
        New-ExtResult
    }
    $r = Invoke-CognitaLinux -Settings $s -CliArgs @('install') -Stage 'cognita'
    Assert-False $script:flagAtStart 'the stale request flag was removed before the CLI started'
    Assert-False $script:skipAtStart 'the stale skip file was removed before the CLI started'
    Assert-False $script:skipLater 'and no skip file appeared'
    Assert-False $r.SkipRequested 'not skipped'
    Assert-Match (Get-LogText) 'removed a stale self-test skip request .*skip-self-test\.request' 'flag removal logged'
    Assert-Match (Get-LogText) 'removed a stale skip file .*progress\.jsonl\.skip' 'skip file removal logged'
}

Test-Case 'install.env (design 21.3): read through the wsl seam as the Linux user; only COGNITA_PROOF and COGNITA_VERSION values are logged' {
    $s = New-TestSettings
    Add-ExtRule '-u cognita --exec sh -s' { param($c) $script:envScript = [string]$c.StdinText; New-ExtResult -Stdout ("COGNITA_PROOF=skipped`nCOGNITA_VERSION=14.2.4`nCOGNITA_ADMIN_USER=hunter2`nCOGNITA_HOME=/home/x`n") }
    $e = Get-LinuxInstallEnv -Settings $s
    Assert-Equal 4 $e.Count 'four keys parsed'
    Assert-Equal 'skipped' $e['COGNITA_PROOF'] 'proof'
    Assert-Match $script:envScript '\.config/cognita/install\.env' 'the file read'
    Assert-False $script:envScript.Contains("`r") 'LF only'
    $log = Get-LogText
    Assert-Match $log 'install\.env: 4 keys read: .*COGNITA_PROOF=skipped' 'proof value logged'
    Assert-Match $log 'COGNITA_VERSION=14\.2\.4' 'version value logged'
    Assert-NotMatch $log 'hunter2' 'no other value is logged'
    Assert-NotMatch $log '/home/x' 'no path value is logged'
    Assert-Equal 'skipped' (Get-LinuxProofState -Settings $s -Caller 'test') 'proof state'
}

Test-Case 'install.env (design 21.3): no file, an unreadable distro, or an unknown value all mean an EMPTY proof, logged' {
    $s = New-TestSettings
    Add-ExtRule '-u cognita --exec sh -s' (New-ExtResult -Stdout '')
    Assert-Equal '' (Get-LinuxProofState -Settings $s -Caller 'test') 'no file'
    $script:ExtRules.Clear()
    Add-ExtRule '-u cognita --exec sh -s' (New-ExtResult -ExitCode 1 -Stderr 'no distro')
    Assert-Equal '' (Get-LinuxProofState -Settings $s -Caller 'test') 'wsl failed'
    Assert-Match (Get-LogText) 'install\.env: read failed: exit 1' 'logged'
    $script:ExtRules.Clear()
    Add-ExtRule '-u cognita --exec sh -s' (New-ExtResult -Stdout "COGNITA_PROOF=maybe`n")
    Assert-Equal '' (Get-LinuxProofState -Settings $s -Caller 'test') 'unknown value'
    Assert-Match (Get-LogText) 'unknown value \[maybe\]' 'logged'
}

# ---- install -------------------------------------------------------------------------------------
function Add-InstallFakes {
    param([bool]$Kvm = $true, [int]$LinuxExit = 0, [string]$Status = $script:StatusJson, [bool]$WithProgress = $true, [string]$TreeLink = '/opt/cognita/trees/14.1.0-0123456789ab')
    $script:kvm = $Kvm
    $script:linuxExit = $LinuxExit
    $script:FakeStatusJson = $Status
    $script:TreeLink = $TreeLink
    Add-ExtRule 'wsl\.exe --import' (New-ExtResult)
    Add-ExtRule 'wsl\.exe --unregister' (New-ExtResult)
    Add-ExtRule 'cat /etc/cognita-distro' { param($c) New-ExtResult -Stdout ('{"installation_id": "' + $script:ownerId + '"}') }
    Add-ExtRule 'readlink -f /opt/cognita/src' { param($c) New-ExtResult -Stdout ($script:TreeLink + "`n") }
    # fstab, mount point, nsenter mount check, terminate (which applies fstab), readiness, access test: the
    # fake distro of design 18.1. There is NO rule for `--exec mount`; the helper must never run it.
    Add-MountWorldFakes
    Add-ExtRule 'test -e /dev/kvm' { param($c) if ($script:kvm) { New-ExtResult } else { New-ExtResult -ExitCode 1 } }
    Add-ExtRule 'cognita status --json' { param($c) New-ExtResult -Stdout $script:FakeStatusJson }
    Add-ExtRule '/usr/local/bin/cognita install' {
        param($c)
        if ($script:writeProgress) { Write-ProgressFileLines ($script:L1 + "`n" + $script:L2 + "`n" + $script:L3 + "`n"); Invoke-ClockSleep 1000; & $c.OnPoll }
        New-ExtResult -ExitCode $script:linuxExit
    }
    $script:writeProgress = $WithProgress
    $bin = Join-Path $env:COGNITA_HOME 'bin'
    [void](New-Item -ItemType Directory -Path $bin -Force)
    [System.IO.File]::WriteAllText((Join-Path $bin 'cognita.exe'), 'x')
}
function Get-InstallOpts {
    param([string]$Folder, $Image, [string[]]$Extra = @())
    $a = @('--projects-folder', $Folder, '--admin-user', 'boss', '--image', $Image.Path, '--image-sha256', $Image.Sha, '--data-dir', (Join-Path $script:TestDir 'vhd'), '--setup-version', '14.1.0', '--setup-revision', '2') + $Extra
    return (ConvertFrom-HelperArgs $a).Opts
}

Test-Case 'install: a fresh machine runs import, keepalive, folder, then the Linux CLI with exactly the design arguments; the password only on stdin' {
    $folder = New-Dir 'My Projects'
    $img = New-FakeImage
    $script:ownerId = 'irrelevant'
    Add-InstallFakes
    $script:Interactive = $true; $script:SecretAnswer = $script:Pw
    $r = Invoke-InstallVerb -Opts (Get-InstallOpts -Folder $folder -Image $img)
    Assert-Equal 'ok' $r.Status ("status (" + ($script:Out | Select-Object -Last 3) + ")")
    Assert-Equal '14.1.0' $r.Values['version'] 'version from status --json'
    Assert-Equal 'http://127.0.0.1:8676' $r.Values['admin_url'] 'admin url'
    # Design 18.1 rule 4: import, keepalive (its readiness wait), the fstab line for root 1, ONE distro
    # restart, the mount in Docker's view, the access test, then `cognita install`. No mount by hand.
    $calls = @($script:ExtCalls)
    $iImport = [array]::IndexOf(@($calls | ForEach-Object { $_.Line -match '--import' }), $true)
    $iReady = [array]::IndexOf(@($calls | ForEach-Object { $_.Line -match 'systemctl is-system-running' }), $true)
    $iFstab = [array]::IndexOf(@($calls | ForEach-Object { $_.StdinText -match 'fstab\.cognita-new' }), $true)
    $iTerm = [array]::IndexOf(@($calls | ForEach-Object { $_.Line -match '--terminate Cognita' }), $true)
    $iMount = [array]::IndexOf(@($calls | ForEach-Object { $_.Line -match 'nsenter -t 1 -m -- mountpoint -q /mnt/cognita-roots/1' }), $true)
    $iAccess = [array]::IndexOf(@($calls | ForEach-Object { $_.Line -match '-u cognita --exec sh -s' }), $true)
    $iCli = [array]::IndexOf(@($calls | ForEach-Object { $_.Line -match 'cognita install' }), $true)
    Assert-True (($iImport -ge 0) -and ($iImport -lt $iReady) -and ($iReady -lt $iFstab) -and ($iFstab -lt $iTerm) -and ($iTerm -lt $iMount) -and ($iMount -lt $iAccess) -and ($iAccess -lt $iCli)) ("order: import, keepalive ready, fstab, restart, mount check, access test, Linux install; got {0} {1} {2} {3} {4} {5} {6}" -f $iImport, $iReady, $iFstab, $iTerm, $iMount, $iAccess, $iCli)
    Assert-Equal 1 $script:Terminates 'exactly one distro restart in a fresh install'
    Assert-Equal 0 (Get-ExtCallsMatching '--exec mount').Count 'the helper never mounts a root'
    $cli = (Get-ExtCallsMatching 'cognita install')[0]
    $expected = "-d|Cognita|-u|cognita|--exec|/usr/local/bin/cognita|install|--non-interactive|--yes|--documents|/mnt/cognita-roots/1|--documents-display|$folder|--admin-user|boss|--admin-password-stdin|--command-name|cognita|--remote-access|no|--workspace|on|--mcp-port|8675|--admin-port|8676|--progress-file|$(ConvertTo-WslMntPath $script:ProgressFile)"
    Assert-Equal $expected ($cli.Arguments -join '|') 'exact command'
    Assert-Equal ($script:Pw + "`n") $cli.StdinText 'password plus one newline on stdin'
    $everything = ((Get-ExtCallLines) -join "`n") + (Get-LogText) + ((Get-OutLines) -join "`n") + (Read-Settings | ConvertTo-Json -Depth 5)
    Assert-NotMatch $everything ([regex]::Escape($script:Pw)) 'the password is nowhere in arguments, log, output or settings'
    $stages = @((Get-ProgressObjects) | ForEach-Object { $_.stage })
    Assert-True ($stages -contains 'import' -and $stages -contains 'keepalive' -and $stages -contains 'folder' -and $stages -contains 'images') 'stages seen, including a relayed Linux stage'
    Assert-Equal 1 @($script:Out | Where-Object { $_ -eq $script:L2 }).Count 'the Linux line was relayed unchanged'
    $saved = Read-Settings
    Assert-Equal 'installed' $saved.state 'state'
    Assert-Equal '14.1.0' $saved.setup_version 'setup version'
    Assert-Equal 2 $saved.setup_revision 'setup revision'
    Assert-Equal $folder (@(Get-SettingsRoots $saved))[0].windows 'root saved'
    Assert-Equal 1 $script:TaskRegistered.Count 'keepalive registered'
    Assert-Equal 2 $script:TaskStarted 'and started: once for the keepalive, once by the fstab restart'
    # Design 18.2: recorded after `cognita install` exited 0, and only then.
    Assert-Equal '14.1.0' $saved.linux_installed_version 'linux_installed_version = the Setup version'
    Assert-Equal 'boss' $saved.admin_user 'admin_user'
}

Test-Case 'install (design 18.2): linux_installed_version and admin_user are set ONLY after cognita install exits 0; a failure never sets or clears them' {
    $folder = New-Dir 'p'; $img = New-FakeImage; $script:ownerId = 'x'
    Add-InstallFakes -LinuxExit 1 -WithProgress $false
    $script:Interactive = $true; $script:SecretAnswer = 'pw'
    $r = Invoke-InstallVerb -Opts (Get-InstallOpts -Folder $folder -Image $img)
    Assert-Equal 'failed' $r.Status 'failed'
    $s1 = Read-Settings
    Assert-Equal '' ([string]$s1.linux_installed_version) 'not recorded after a failed install (import and root did finish)'
    Assert-Equal '' ([string]$s1.admin_user) 'no admin_user either'
    Assert-Equal 'installed' $s1.state 'the import itself did finish, which is exactly why `state` needs linux_installed_version to tell finish mode'
}

Test-Case 'install (design 18.2): a failed RERUN leaves the recorded version alone (never cleared by a failure)' {
    $folder = New-Dir 'p'; $img = New-FakeImage
    $vhd = Join-Path $script:TestDir 'vhd'
    $s = New-TestSettings -State 'installed' -Vhd $vhd -RootPaths @($folder)
    Set-SettingProp $s 'linux_installed_version' '14.0.0'; Set-SettingProp $s 'admin_user' 'oldadmin'; Save-Settings $s
    $script:LxssDistros = @([pscustomobject]@{ Guid = '{g}'; Name = 'Cognita'; BasePath = $vhd })
    $script:ownerId = $s.installation_id
    Add-InstallFakes -LinuxExit 1 -WithProgress $false
    $script:mounted['/mnt/cognita-roots/1'] = $true
    $script:Interactive = $true; $script:SecretAnswer = 'pw'
    $r = Invoke-InstallVerb -Opts (Get-InstallOpts -Folder $folder -Image $img)
    Assert-Equal 'failed' $r.Status 'failed'
    $after = Read-Settings
    Assert-Equal '14.0.0' $after.linux_installed_version 'the earlier version is still recorded'
    Assert-Equal 'oldadmin' $after.admin_user 'and the earlier admin user'
}

Test-Case 'install (design 18.2): with Cognita already installed here, --admin-user is NOT passed; the recorded admin_user stays' {
    $folder = New-Dir 'p'; $img = New-FakeImage
    $vhd = Join-Path $script:TestDir 'vhd'
    $s = New-TestSettings -State 'installed' -Vhd $vhd -RootPaths @($folder)
    Set-SettingProp $s 'linux_installed_version' '14.0.0'; Set-SettingProp $s 'admin_user' 'oldadmin'; Save-Settings $s
    $script:LxssDistros = @([pscustomobject]@{ Guid = '{g}'; Name = 'Cognita'; BasePath = $vhd })
    $script:ownerId = $s.installation_id
    Add-InstallFakes
    $script:mounted['/mnt/cognita-roots/1'] = $true
    $script:Interactive = $true; $script:SecretAnswer = 'pw'
    $r = Invoke-InstallVerb -Opts (Get-InstallOpts -Folder $folder -Image $img)
    Assert-Equal 'ok' $r.Status 'ok'
    $cli = (Get-ExtCallsMatching 'cognita install')[0]
    Assert-NotMatch ($cli.Arguments -join ' ') '--admin-user' 'no --admin-user on a repair or update install'
    Assert-Match ($cli.Arguments -join ' ') '--admin-password-stdin' 'the password is still handed over'
    $after = Read-Settings
    Assert-Equal '14.1.0' $after.linux_installed_version 'the new Setup version is recorded after the exit 0'
    Assert-Equal 'oldadmin' $after.admin_user 'admin_user untouched'
    Assert-Match (Get-LogText) '--admin-user is not passed' 'decision logged'
}

Test-Case 'install (design 18.2): a reinstall over a distro that was unregistered by hand starts over: --admin-user passed again, recorded install cleared' {
    $folder = New-Dir 'p'; $img = New-FakeImage
    $s = New-TestSettings -State 'installed' -RootPaths @('D:\Old')
    Set-SettingProp $s 'linux_installed_version' '14.0.0'; Set-SettingProp $s 'admin_user' 'oldadmin'; Save-Settings $s
    $script:LxssDistros = @()
    $script:ownerId = 'x'
    Add-InstallFakes
    $script:Interactive = $true; $script:SecretAnswer = 'pw'
    $r = Invoke-InstallVerb -Opts (Get-InstallOpts -Folder $folder -Image $img)
    Assert-Equal 'ok' $r.Status 'ok'
    $cli = (Get-ExtCallsMatching 'cognita install')[0]
    Assert-Match ($cli.Arguments -join ' ') '--admin-user boss' 'a fresh distro has no Cognita, so the admin user is asked for'
    Assert-Equal 'boss' (Read-Settings).admin_user 'the new admin user recorded'
}

Test-Case 'install (design 18.2 guard): a --data-dir that differs from an OWNED distro''s disk folder is ignored and logged' {
    $folder = New-Dir 'p'; $img = New-FakeImage
    $vhd = Join-Path $script:TestDir 'vhd'
    $s = New-TestSettings -State 'installed' -Vhd $vhd -RootPaths @($folder)
    Set-SettingProp $s 'linux_installed_version' '14.0.0'; Save-Settings $s
    $script:LxssDistros = @([pscustomobject]@{ Guid = '{g}'; Name = 'Cognita'; BasePath = $vhd })
    $script:ownerId = $s.installation_id
    Add-InstallFakes
    $script:mounted['/mnt/cognita-roots/1'] = $true
    $script:Interactive = $true; $script:SecretAnswer = 'pw'
    $other = Join-Path $script:TestDir 'elsewhere'
    $opts = (ConvertFrom-HelperArgs @('--projects-folder', $folder, '--image', $img.Path, '--image-sha256', $img.Sha, '--data-dir', $other, '--setup-version', '14.1.0')).Opts
    $r = Invoke-InstallVerb -Opts $opts
    Assert-Equal 'ok' $r.Status 'ok'
    Assert-Equal $vhd (Read-Settings).vhd_dir 'the distro''s own disk folder stays'
    Assert-Equal 0 (Get-ExtCallsMatching '--import|--unregister').Count 'nothing imported or moved'
    Assert-Match (Get-LogText) ([regex]::Escape("--data-dir [$other] differs from the owned distro's disk folder [$vhd]; ignored")) 'logged with both values'
    # a --data-dir that is the same folder is not worth a log line
    $script:Out.Clear(); $script:LogLines.Clear(); $script:ExtCalls.Clear()
    $opts2 = (ConvertFrom-HelperArgs @('--projects-folder', $folder, '--image', $img.Path, '--image-sha256', $img.Sha, '--data-dir', ($vhd + '\'), '--setup-version', '14.1.0')).Opts
    [void](Invoke-InstallVerb -Opts $opts2)
    Assert-NotMatch (Get-LogText) 'differs from the owned distro' 'same folder: no complaint'
}

Test-Case 'install (design 18.2 guard, 19.2 item 7): once root 1 exists, a different projects folder is refused naming root 1 and pointing at add-folder; nothing changes' {
    $one = New-Dir 'one'; $two = New-Dir 'two'; $img = New-FakeImage
    $vhd = Join-Path $script:TestDir 'vhd'
    $s = New-TestSettings -State 'installed' -Vhd $vhd -RootPaths @($one)
    Set-SettingProp $s 'linux_installed_version' '14.0.0'; Save-Settings $s
    $script:LxssDistros = @([pscustomobject]@{ Guid = '{g}'; Name = 'Cognita'; BasePath = $vhd })
    $script:ownerId = $s.installation_id
    Add-InstallFakes
    $script:mounted['/mnt/cognita-roots/1'] = $true
    $script:Interactive = $true; $script:SecretAnswer = 'pw'
    $r = Invoke-InstallVerb -Opts (Get-InstallOpts -Folder $two -Image $img)
    Assert-Equal 'failed' $r.Status 'failed'
    Assert-Equal 'folder-not-first' $r.Values['reason'] 'reason'
    $f = @((Get-ProgressObjects) | Where-Object { $_.state -eq 'failed' })
    Assert-Equal 1 $f.Count 'one failed line'
    Assert-Equal ("Cognita already uses $one as its projects folder.") $f[0].message 'the message names the folder Cognita already uses'
    Assert-Equal 'Keep that folder; add others later with cognita add-folder.' $f[0].fix 'the fix'
    Assert-Equal $one $r.Values['root1'] 'the result carries root1'
    Assert-Equal 0 (Get-ExtCallsMatching 'cognita install|--terminate|--import|sh -s').Count 'no install, no restart, no fstab write'
    Assert-Equal 1 @(Get-SettingsRoots (Read-Settings)).Count 'roots unchanged'
    Assert-Equal $one (@(Get-SettingsRoots (Read-Settings)))[0].windows 'still the first folder'
    # the same folder, or none given, is a normal repair
    $script:Out.Clear(); $script:ExtCalls.Clear()
    $r2 = Invoke-InstallVerb -Opts (Get-InstallOpts -Folder $one -Image $img)
    Assert-Equal 'ok' $r2.Status 'the same folder is fine'
}

Test-Case 'install (design 18.2 tree swap): a distro tree OLDER than this Setup is swapped through the same script update uses, BEFORE cognita install; equal or newer is left alone' {
    $folder = New-Dir 'p'; $img = New-FakeImage; $script:ownerId = 'x'
    $tar = Join-Path $script:TestDir 'cognita-src-14.1.0.tar.gz'
    [System.IO.File]::WriteAllBytes($tar, [byte[]](1..50))
    $sha = ('cd' * 32)
    $script:Interactive = $true; $script:SecretAnswer = 'pw'
    foreach ($case in @(
        @{ Link = '/opt/cognita/trees/14.0.0-0123456789ab'; Swap = $true; What = 'older' },
        @{ Link = '/opt/cognita/trees/14.1.0-0123456789ab'; Swap = $false; What = 'equal' },
        @{ Link = '/opt/cognita/trees/14.2.0-0123456789ab'; Swap = $false; What = 'newer' },
        @{ Link = '/opt/cognita/src'; Swap = $true; What = 'a name that is not <version>-<commit>' }
    )) {
        $script:ExtRules.Clear(); $script:ExtCalls.Clear(); $script:Out.Clear(); $script:LogLines.Clear()
        Remove-Item -LiteralPath (Get-SettingsPath) -Force -ErrorAction SilentlyContinue
        Add-InstallFakes -TreeLink $case.Link
        $opts = Get-InstallOpts -Folder $folder -Image $img -Extra @('--src', $tar, '--src-sha256', $sha)
        $r = Invoke-InstallVerb -Opts $opts
        Assert-Equal 'ok' $r.Status ($case.What + ': ok')
        $calls = @($script:ExtCalls)
        $iTree = [array]::IndexOf(@($calls | ForEach-Object { $_.StdinText -match '/opt/cognita/trees' }), $true)
        $iCli = [array]::IndexOf(@($calls | ForEach-Object { $_.Line -match 'cognita install' }), $true)
        Assert-Equal 1 @($calls | Where-Object { $_.Line -match 'readlink -f /opt/cognita/src' }).Count ($case.What + ': the tree version was read once')
        if ($case.Swap) {
            Assert-True (($iTree -ge 0) -and ($iTree -lt $iCli)) ($case.What + ': tree swapped before cognita install')
            Assert-Equal ((ConvertTo-WslMntPath $tar) + '|' + $sha + '|14.1.0') ($calls[$iTree].Arguments[-3..-1] -join '|') ($case.What + ': the same script arguments update uses')
            Assert-Match $calls[$iTree].StdinText 'mv -T /opt/cognita/src.new /opt/cognita/src' 'the atomic swap script'
        } else {
            Assert-Equal -1 $iTree ($case.What + ': no tree swap')
        }
    }
    # no --src: no check at all (nothing to swap with)
    $script:ExtRules.Clear(); $script:ExtCalls.Clear(); $script:LogLines.Clear()
    Remove-Item -LiteralPath (Get-SettingsPath) -Force -ErrorAction SilentlyContinue
    Add-InstallFakes
    $r2 = Invoke-InstallVerb -Opts (Get-InstallOpts -Folder $folder -Image $img)
    Assert-Equal 'ok' $r2.Status 'ok'
    Assert-Equal 0 (Get-ExtCallsMatching 'readlink').Count 'without --src the tree is not even looked at'
    Assert-Match (Get-LogText) 'tree check skipped' 'logged'
}

Test-Case 'install (design 18.2 tree swap): a tree that cannot be swapped stops before cognita install with reason tree' {
    $folder = New-Dir 'p'; $img = New-FakeImage; $script:ownerId = 'x'
    $tar = Join-Path $script:TestDir 'cognita-src-14.1.0.tar.gz'
    [System.IO.File]::WriteAllBytes($tar, [byte[]](1..50))
    Add-InstallFakes -TreeLink '/opt/cognita/trees/14.0.0-0123456789ab'
    $script:ExtRules.Insert(0, @{ Pattern = '-u root --exec sh -s'; Response = { param($c)
        if ($c.StdinText -match '/opt/cognita/trees') { return (New-ExtResult -ExitCode 20 -Stderr 'source tarball checksum mismatch') }
        foreach ($m in [regex]::Matches([string]$c.StdinText, '(/mnt/cognita-roots/\d) drvfs')) { $script:FstabRoots[$m.Groups[1].Value] = $true }
        New-ExtResult } })
    $script:Interactive = $true; $script:SecretAnswer = 'pw'
    $r = Invoke-InstallVerb -Opts (Get-InstallOpts -Folder $folder -Image $img -Extra @('--src', $tar, '--src-sha256', ('ab' * 32)))
    Assert-Equal 'failed' $r.Status 'failed'
    Assert-Equal 'tree' $r.Values['reason'] 'reason'
    Assert-Equal 0 (Get-ExtCallsMatching 'cognita install').Count 'the Linux install did not run'
    Assert-Equal '' ([string](Read-Settings).linux_installed_version) 'nothing recorded'
}

Test-Case 'install: no /dev/kvm turns Workspace off with the C7 warning and says so on the command line' {
    $folder = New-Dir 'p'; $img = New-FakeImage; $script:ownerId = 'x'
    Add-InstallFakes -Kvm $false
    $script:Interactive = $true; $script:SecretAnswer = 'pw'
    $r = Invoke-InstallVerb -Opts (Get-InstallOpts -Folder $folder -Image $img)
    Assert-Equal 'ok' $r.Status 'ok'
    $cli = (Get-ExtCallsMatching 'cognita install')[0]
    Assert-Match ($cli.Arguments -join ' ') '--workspace off' 'workspace off passed'
    $w = @((Get-ProgressObjects) | Where-Object { $_.stage -eq 'workspace' -and $_.state -eq 'warning' })
    Assert-Equal 1 $w.Count 'a warning'
    Assert-Match $w[0].message '/dev/kvm' 'names /dev/kvm'
}

Test-Case 'install: --workspace off and custom ports are passed through and saved' {
    $folder = New-Dir 'p'; $img = New-FakeImage; $script:ownerId = 'x'
    Add-InstallFakes -Status ($script:StatusJson -replace '8676', '9001' -replace '8675', '9000' -replace '"workspace": "on"', '"workspace": "off"')
    $script:Interactive = $true; $script:SecretAnswer = 'pw'
    $r = Invoke-InstallVerb -Opts (Get-InstallOpts -Folder $folder -Image $img -Extra @('--workspace', 'off', '--mcp-port', '9000', '--admin-port', '9001'))
    Assert-Equal 'ok' $r.Status 'ok'
    $cli = (Get-ExtCallsMatching 'cognita install')[0]
    Assert-Match ($cli.Arguments -join ' ') '--workspace off --mcp-port 9000 --admin-port 9001' 'flags'
    $s = Read-Settings
    Assert-Equal 9000 $s.mcp_port 'saved'
    Assert-Equal 9001 $s.admin_port 'saved'
    Assert-Equal 'off' $s.workspace 'saved'
}

Test-Case 'install: the Linux side reports different ports than asked, the helper says so and records them' {
    $folder = New-Dir 'p'; $img = New-FakeImage; $script:ownerId = 'x'
    Add-InstallFakes -Status ($script:StatusJson -replace '8675', '8700')
    $script:Interactive = $true; $script:SecretAnswer = 'pw'
    [void](Invoke-InstallVerb -Opts (Get-InstallOpts -Folder $folder -Image $img))
    Assert-Equal 8700 (Read-Settings).mcp_port 'settings follow Linux'
    Assert-Match ((@(Get-ProgressObjects) | ForEach-Object { $_.message }) -join "`n") 'The MCP port is 8700, not the 8675' 'said out loud'
}

Test-Case 'install: exit 10 is reported as a failure naming it; any other failure carries the Linux message' {
    $folder = New-Dir 'p'; $img = New-FakeImage; $script:ownerId = 'x'
    Add-InstallFakes -LinuxExit 10 -WithProgress $false
    $script:Interactive = $true; $script:SecretAnswer = 'pw'
    $r = Invoke-InstallVerb -Opts (Get-InstallOpts -Folder $folder -Image $img)
    Assert-Equal 'failed' $r.Status 'failed'
    Assert-Equal 'exit-10' $r.Values['reason'] 'reason'
    Assert-Match ((@(Get-ProgressObjects) | ForEach-Object { $_.message }) -join "`n") 'exit 10' 'named'
    $script:ExtRules.Clear(); $script:Out.Clear(); $script:ExtCalls.Clear()
    $img2 = New-FakeImage
    Add-InstallFakes -LinuxExit 1 -WithProgress $false
    $r2 = Invoke-InstallVerb -Opts (Get-InstallOpts -Folder $folder -Image $img2)
    Assert-Equal 'failed' $r2.Status 'failed'
    Assert-Equal 'linux-install' $r2.Values['reason'] 'reason'
}

Test-Case 'install: a projects folder that is refused stops everything before any WSL command' {
    $img = New-FakeImage; $script:ownerId = 'x'
    Add-InstallFakes
    $script:Interactive = $true; $script:SecretAnswer = 'pw'
    $r = Invoke-InstallVerb -Opts (Get-InstallOpts -Folder 'C:\' -Image $img)
    Assert-Equal 'failed' $r.Status 'failed'
    Assert-Equal 'bad-folder' $r.Values['reason'] 'reason'
    Assert-Equal 0 (Get-ExtCallLines).Count 'nothing ran'
    Assert-False (Test-Path (Get-SettingsPath)) 'nothing recorded'
}

Test-Case 'install: no password source fails before anything changes' {
    $folder = New-Dir 'p'; $img = New-FakeImage
    Add-InstallFakes
    $script:Interactive = $false
    $r = Invoke-InstallVerb -Opts (Get-InstallOpts -Folder $folder -Image $img)
    Assert-Equal 'no-password' $r.Values['reason'] 'reason'
    Assert-Equal 0 (Get-ExtCallLines).Count 'nothing ran'
}

Test-Case 'install: a foreign distro named Cognita is refused, nothing is imported or unregistered' {
    $folder = New-Dir 'p'; $img = New-FakeImage
    $vhd = Join-Path $script:TestDir 'vhd'
    $s = New-TestSettings -State 'installed' -Vhd $vhd
    $script:LxssDistros = @([pscustomobject]@{ Guid = '{g}'; Name = 'Cognita'; BasePath = 'C:\Someone\Else' })
    $script:ownerId = 'x'
    Add-InstallFakes
    $script:Interactive = $true; $script:SecretAnswer = 'pw'
    $r = Invoke-InstallVerb -Opts (Get-InstallOpts -Folder $folder -Image $img)
    Assert-Equal 'foreign-distro' $r.Values['reason'] 'reason'
    Assert-Equal 0 (Get-ExtCallsMatching '--import|--unregister').Count 'no import, no unregister'
}

Test-Case 'install: an interrupted import (pending record, our BasePath, no marker) is unregistered and imported again' {
    $folder = New-Dir 'p'; $img = New-FakeImage
    $vhd = Join-Path $script:TestDir 'vhd'
    $s = New-TestSettings -State 'import-pending' -Vhd $vhd
    $script:LxssDistros = @([pscustomobject]@{ Guid = '{g}'; Name = 'Cognita'; BasePath = $vhd })
    Add-InstallFakes
    $script:ExtRules.Insert(0, @{ Pattern = 'cat /etc/cognita-distro'; Response = (New-ExtResult -ExitCode 1 -Stderr 'cat: /etc/cognita-distro: No such file or directory') })
    $script:Interactive = $true; $script:SecretAnswer = 'pw'
    $r = Invoke-InstallVerb -Opts (Get-InstallOpts -Folder $folder -Image $img)
    Assert-Equal 'ok' $r.Status 'ok'
    $lines = Get-ExtCallLines
    $iUn = [array]::IndexOf(@($lines | ForEach-Object { $_ -match '--unregister Cognita' }), $true)
    $iIm = [array]::IndexOf(@($lines | ForEach-Object { $_ -match '--import' }), $true)
    Assert-True (($iUn -ge 0) -and ($iUn -lt $iIm)) 'unregister first, then import'
}

Test-Case 'install (design 19.11 R5): a half-imported distro that cannot be unregistered fails with reason=unregister-failed (Setup keys the "data could not be deleted" text on exactly this reason from the uninstall)' {
    $folder = New-Dir 'p'; $img = New-FakeImage
    $vhd = Join-Path $script:TestDir 'vhd'
    $s = New-TestSettings -State 'import-pending' -Vhd $vhd
    $script:LxssDistros = @([pscustomobject]@{ Guid = '{g}'; Name = 'Cognita'; BasePath = $vhd })
    Add-InstallFakes
    $script:ExtRules.Insert(0, @{ Pattern = 'cat /etc/cognita-distro'; Response = (New-ExtResult -ExitCode 1 -Stderr 'cat: /etc/cognita-distro: No such file or directory') })
    $script:ExtRules.Insert(0, @{ Pattern = '--unregister'; Response = (New-ExtResult -ExitCode 1) })
    $script:Interactive = $true; $script:SecretAnswer = 'pw'
    $r = Invoke-InstallVerb -Opts (Get-InstallOpts -Folder $folder -Image $img)
    Assert-Equal 'failed' $r.Status 'failed'
    Assert-Equal 'unregister-failed' $r.Values['reason'] 'reason'
    Assert-Equal 0 (Get-ExtCallsMatching '--import').Count 'no import after the failed unregister'
}

Test-Case 'install (rerun): our distro exists, so the import is skipped; keepalive re-registered; the Linux install still runs (repair)' {
    $folder = New-Dir 'p'; $img = New-FakeImage
    $vhd = Join-Path $script:TestDir 'vhd'
    $s = New-TestSettings -State 'installed' -Vhd $vhd -RootPaths @($folder)
    $script:LxssDistros = @([pscustomobject]@{ Guid = '{g}'; Name = 'Cognita'; BasePath = $vhd })
    $script:ownerId = $s.installation_id
    Add-InstallFakes
    $script:Interactive = $true; $script:SecretAnswer = 'pw'
    $script:mounted['/mnt/cognita-roots/1'] = $true
    $r = Invoke-InstallVerb -Opts (Get-InstallOpts -Folder $folder -Image $img)
    Assert-Equal 'ok' $r.Status 'ok'
    Assert-Equal 0 (Get-ExtCallsMatching '--import').Count 'no import'
    Assert-Equal 1 $script:TaskRegistered.Count 'keepalive re-registered'
    Assert-Equal 1 (Get-ExtCallsMatching 'cognita install').Count 'Linux install ran'
    Assert-Equal 1 @(Get-SettingsRoots (Read-Settings)).Count 'roots idempotent: still one'
}

Test-Case 'install: rerun after an uninstall that kept the data reuses the distro' {
    $folder = New-Dir 'p'; $img = New-FakeImage
    $vhd = Join-Path $script:TestDir 'vhd'
    $s = New-TestSettings -State 'uninstalled' -Vhd $vhd -RootPaths @($folder)
    $script:LxssDistros = @([pscustomobject]@{ Guid = '{g}'; Name = 'Cognita'; BasePath = $vhd })
    $script:ownerId = $s.installation_id
    Add-InstallFakes
    [System.IO.File]::WriteAllText((Get-StoppedFlagPath), 'x')
    $script:Interactive = $true; $script:SecretAnswer = 'pw'
    $r = Invoke-InstallVerb -Opts (Get-InstallOpts -Folder $folder -Image $img)
    Assert-Equal 'ok' $r.Status 'ok'
    Assert-Equal 0 (Get-ExtCallsMatching '--import').Count 'no import'
    Assert-Equal 'installed' (Read-Settings).state 'installed again'
    Assert-False (Test-Path (Get-StoppedFlagPath)) 'the stopped flag left by uninstall is cleared so the keepalive runs'
}

Test-Case 'install: a fresh import with no --image fails cleanly' {
    $folder = New-Dir 'p'; $script:ownerId = 'x'
    Add-InstallFakes
    $script:Interactive = $true; $script:SecretAnswer = 'pw'
    $opts = (ConvertFrom-HelperArgs @('--projects-folder', $folder, '--data-dir', (Join-Path $script:TestDir 'vhd'))).Opts
    $r = Invoke-InstallVerb -Opts $opts
    Assert-Equal 'failed' $r.Status 'failed'
    Assert-Equal 'image-missing' $r.Values['reason'] 'reason'
}

Test-Case 'install: settings say installed but the distro is gone (unregistered by hand): a fresh import with a new id' {
    $folder = New-Dir 'p'; $img = New-FakeImage
    $s = New-TestSettings -State 'installed' -RootPaths @('D:\Old')
    $oldId = $s.installation_id
    $script:LxssDistros = @()
    $script:ownerId = 'x'
    Add-InstallFakes
    $script:Interactive = $true; $script:SecretAnswer = 'pw'
    $r = Invoke-InstallVerb -Opts (Get-InstallOpts -Folder $folder -Image $img)
    Assert-Equal 'ok' $r.Status 'ok'
    Assert-True ((Read-Settings).installation_id -ne $oldId) 'a new installation id'
    Assert-Equal 1 (Get-ExtCallsMatching '--import').Count 'imported'
    Assert-Equal 1 @(Get-SettingsRoots (Read-Settings)).Count 'the stale folder list was reset'
}

Test-Case 'install (design 19.2 item 4): state=uninstalled + a missing distro + recorded roots/version: the stale records are reset whatever state says, so root 1 is not kept' {
    $img = New-FakeImage
    $cases = @(
        @{ What = 'uninstalled with a recorded version and a root'; Version = '14.0.0'; Roots = @('D:\Old'); State = 'uninstalled' },
        @{ What = 'uninstalled with only a root'; Version = ''; Roots = @('D:\Old'); State = 'uninstalled' },
        @{ What = 'import-pending with only a recorded version'; Version = '14.0.0'; Roots = @(); State = 'import-pending' }
    )
    foreach ($case in $cases) {
        $folder = New-Dir ('p' + [guid]::NewGuid().ToString('N'))
        $script:ExtRules.Clear(); $script:ExtCalls.Clear(); $script:Out.Clear(); $script:LogLines.Clear()
        $s = New-TestSettings -State $case.State -RootPaths $case.Roots
        Set-SettingProp $s 'linux_installed_version' $case.Version; Set-SettingProp $s 'admin_user' 'oldadmin'; Save-Settings $s
        $oldId = $s.installation_id
        $script:LxssDistros = @()
        $script:ownerId = 'x'
        Add-InstallFakes
        $script:Interactive = $true; $script:SecretAnswer = 'pw'
        $r = Invoke-InstallVerb -Opts (Get-InstallOpts -Folder $folder -Image $img)
        Assert-Equal 'ok' $r.Status ($case.What + ': ok (not refused as folder-not-first)')
        $after = Read-Settings
        Assert-True ($after.installation_id -ne $oldId) ($case.What + ': a new installation id')
        Assert-Equal 1 (Get-ExtCallsMatching '--import').Count ($case.What + ': imported')
        Assert-Equal $folder (@(Get-SettingsRoots $after))[0].windows ($case.What + ': root 1 is the folder just given')
        Assert-Equal 1 @(Get-SettingsRoots $after).Count ($case.What + ': the old root is gone')
        # --admin-user is passed again: the recorded install was void, and the new user recorded.
        Assert-Match ((Get-ExtCallsMatching 'cognita install')[0].Arguments -join ' ') '--admin-user boss' ($case.What + ': a fresh distro asks for the admin user')
        Assert-Equal 'boss' $after.admin_user ($case.What + ': the new admin user recorded')
        Assert-Match (Get-LogText) 'install: the distro is gone \(state=' ($case.What + ': the reset decision is logged with its values')
    }
}

Test-Case 'install (design 19.2 item 8): a recorded Funnel refuses an --mcp-port that differs from its target, and changes nothing; the same port, or none, is fine' {
    $folder = New-Dir 'p'; $img = New-FakeImage
    $vhd = Join-Path $script:TestDir 'vhd'
    $s = New-TestSettings -State 'installed' -Vhd $vhd -RootPaths @($folder)
    Set-SettingProp $s 'linux_installed_version' '14.0.0'
    Set-SettingProp $s 'funnel' ([pscustomobject][ordered]@{ https_port = 443; target = 8675 })
    Save-Settings $s
    $script:LxssDistros = @([pscustomobject]@{ Guid = '{g}'; Name = 'Cognita'; BasePath = $vhd })
    $script:ownerId = $s.installation_id
    Add-InstallFakes
    # Design 19.11 R7: the recorded Funnel is still served by Tailscale (public 443 -> localhost:8675).
    Add-FunnelStatusFake -Serving @{ 443 = 'http://127.0.0.1:8675' }
    $script:mounted['/mnt/cognita-roots/1'] = $true
    $script:Interactive = $true; $script:SecretAnswer = 'pw'
    $r = Invoke-InstallVerb -Opts (Get-InstallOpts -Folder $folder -Image $img -Extra @('--mcp-port', '9000'))
    Assert-Equal 'failed' $r.Status 'failed'
    Assert-Equal 'port-in-use-by-funnel' $r.Values['reason'] 'reason'
    # Design 19.11 R6: the old MCP port is funnel_target (funnel_port always means the public https port)
    Assert-Equal 8675 $r.Values['funnel_target'] 'the old target is reported as funnel_target'
    Assert-False $r.Values.Contains('funnel_port') 'and there is no funnel_port on the refusal'
    Assert-Equal 9000 $r.Values['asked'] 'asked'
    Assert-Match (Get-LogText) 'install: recorded funnel https_port=443 target=8675 served=True' 'the Tailscale check is logged'
    $f = @((Get-ProgressObjects) | Where-Object { $_.state -eq 'failed' })
    Assert-Equal 1 $f.Count 'one failed line'
    Assert-Equal 'Remote access uses port 8675.' $f[0].message 'the message names the port the Funnel forwards to'
    Assert-Equal 'Turn remote access off first (tailscale funnel --https=443 off), then change the port.' $f[0].fix 'the fix names the command that really turns the Funnel off'
    Assert-Equal 0 (Get-ExtCallsMatching 'cognita install|--terminate|--import|sh -s').Count 'no install, no restart, no fstab write'
    Assert-Equal 8675 (Read-Settings).mcp_port 'the recorded port did not move'
    Assert-Match (Get-LogText) 'install: funnel guard asked mcp-port=9000 funnel target=8675' 'the values are logged'
    # the same port as the Funnel's target is not a change
    $script:Out.Clear(); $script:ExtCalls.Clear()
    $r2 = Invoke-InstallVerb -Opts (Get-InstallOpts -Folder $folder -Image $img -Extra @('--mcp-port', '8675'))
    Assert-Equal 'ok' $r2.Status 'the same port is fine'
    # and no --mcp-port at all keeps the recorded one
    $script:Out.Clear(); $script:ExtCalls.Clear()
    $r3 = Invoke-InstallVerb -Opts (Get-InstallOpts -Folder $folder -Image $img)
    Assert-Equal 'ok' $r3.Status 'no --mcp-port is fine'
    # with no Funnel recorded, another port is allowed (the existing behavior)
    $s2 = Read-Settings; Set-SettingProp $s2 'funnel' $null; Save-Settings $s2
    $script:Out.Clear(); $script:ExtCalls.Clear()
    $r4 = Invoke-InstallVerb -Opts (Get-InstallOpts -Folder $folder -Image $img -Extra @('--mcp-port', '9000'))
    Assert-Equal 'ok' $r4.Status 'no Funnel recorded: another port is allowed'
}

Test-Case 'install (design 19.11 R7): a Funnel turned off by hand, or a Tailscale that is gone, does not block an --mcp-port change: the record is cleared, logged, and the install goes on' {
    foreach ($case in @(@{ What = 'funnel off in tailscale'; Ts = $true; Serving = @{} }, @{ What = 'funnel now serves something else'; Ts = $true; Serving = @{ 443 = 'http://127.0.0.1:3000' } }, @{ What = 'tailscale.exe absent'; Ts = $false; Serving = @{} })) {
        $script:ExtRules.Clear(); $script:ExtCalls.Clear(); $script:Out.Clear(); $script:LogLines.Clear()
        $folder = New-Dir ('p-' + [guid]::NewGuid().ToString('N').Substring(0, 6)); $img = New-FakeImage
        $vhd = Join-Path $script:TestDir 'vhd'
        $s = New-TestSettings -State 'installed' -Vhd $vhd -RootPaths @($folder)
        Set-SettingProp $s 'linux_installed_version' '14.0.0'
        Set-SettingProp $s 'funnel' ([pscustomobject][ordered]@{ https_port = 443; target = 8675 })
        Save-Settings $s
        $script:LxssDistros = @([pscustomobject]@{ Guid = '{g}'; Name = 'Cognita'; BasePath = $vhd })
        $script:ownerId = $s.installation_id
        Add-InstallFakes
        Add-FunnelStatusFake -Serving $case.Serving
        if (-not $case.Ts) { $script:TailscaleExe = $null }
        $script:mounted['/mnt/cognita-roots/1'] = $true
        $script:Interactive = $true; $script:SecretAnswer = 'pw'
        $r = Invoke-InstallVerb -Opts (Get-InstallOpts -Folder $folder -Image $img -Extra @('--mcp-port', '9000'))
        Assert-Equal 'ok' $r.Status ($case.What + ': the port change is allowed')
        Assert-True ($null -eq (Read-Settings).funnel) ($case.What + ': the stale Funnel record is cleared')
        # (settings.mcp_port ends up as whatever `cognita status` reports; the fake status says 8675, so
        # the new port is proven by what the Linux install was asked for.)
        Assert-Match ((Get-ExtCallsMatching 'cognita install')[0].Arguments -join ' ') '--mcp-port 9000' ($case.What + ': the Linux install was asked for the new port')
        Assert-Match (Get-LogText) 'install: recorded funnel https_port=443 target=8675 served=False' ($case.What + ': the check and its answer are logged')
        Assert-Match (Get-LogText) 'install: recorded funnel cleared from settings: ' ($case.What + ': the clearing and its reason are logged')
    }
}

Test-Case 'install (review of 19.11): Tailscale present but not answering is UNKNOWN: the Funnel record is kept and the port change is still refused' {
    $folder = New-Dir 'p'; $img = New-FakeImage
    $vhd = Join-Path $script:TestDir 'vhd'
    $s = New-TestSettings -State 'installed' -Vhd $vhd -RootPaths @($folder)
    Set-SettingProp $s 'linux_installed_version' '14.0.0'
    Set-SettingProp $s 'funnel' ([pscustomobject][ordered]@{ https_port = 443; target = 8675 })
    Save-Settings $s
    $script:LxssDistros = @([pscustomobject]@{ Guid = '{g}'; Name = 'Cognita'; BasePath = $vhd })
    $script:ownerId = $s.installation_id
    Add-InstallFakes
    $script:TailscaleExe = 'C:\ts\tailscale.exe'
    Add-ExtRule 'tailscale\.exe funnel status --json' (New-ExtResult -ExitCode 1 -Stderr 'failed to connect to local tailscaled')
    $script:mounted['/mnt/cognita-roots/1'] = $true
    $script:Interactive = $true; $script:SecretAnswer = 'pw'
    $r = Invoke-InstallVerb -Opts (Get-InstallOpts -Folder $folder -Image $img -Extra @('--mcp-port', '9000'))
    Assert-Equal 'failed' $r.Status 'refused: the Funnel may come back with the service'
    Assert-Equal 'port-in-use-by-funnel' $r.Values['reason'] 'reason'
    Assert-Equal 443 (Read-Settings).funnel.https_port 'the record is kept'
    Assert-Match (Get-LogText) 'install: could not ask Tailscale; funnel record kept' 'the decision is logged'
    Assert-Equal 0 (Get-ExtCallsMatching 'cognita install').Count 'nothing installed'
}

Test-Case 'install (design 19.11 R7): the same --mcp-port as the Funnel target never asks Tailscale' {
    $folder = New-Dir 'p'; $img = New-FakeImage
    $vhd = Join-Path $script:TestDir 'vhd'
    $s = New-TestSettings -State 'installed' -Vhd $vhd -RootPaths @($folder)
    Set-SettingProp $s 'linux_installed_version' '14.0.0'
    Set-SettingProp $s 'funnel' ([pscustomobject][ordered]@{ https_port = 443; target = 8675 })
    Save-Settings $s
    $script:LxssDistros = @([pscustomobject]@{ Guid = '{g}'; Name = 'Cognita'; BasePath = $vhd })
    $script:ownerId = $s.installation_id
    Add-InstallFakes
    $script:mounted['/mnt/cognita-roots/1'] = $true
    $script:Interactive = $true; $script:SecretAnswer = 'pw'
    $r = Invoke-InstallVerb -Opts (Get-InstallOpts -Folder $folder -Image $img -Extra @('--mcp-port', '8675'))
    Assert-Equal 'ok' $r.Status 'ok'
    Assert-Equal 0 (Get-ExtCallsMatching 'tailscale').Count 'no Tailscale call'
    Assert-Equal 443 (Read-Settings).funnel.https_port 'the Funnel record is untouched'
}

Test-Case 'install: import reporting a needed restart ends the verb with restart-required (exit 3010)' {
    $folder = New-Dir 'p'; $img = New-FakeImage; $script:ownerId = 'x'
    Add-InstallFakes
    $script:ExtRules.Insert(0, @{ Pattern = 'wsl\.exe --import'; Response = (New-ExtResult -ExitCode 1 -Stderr 'HCS_E_SERVICE_NOT_AVAILABLE') })
    $script:Interactive = $true; $script:SecretAnswer = 'pw'
    $r = Invoke-InstallVerb -Opts (Get-InstallOpts -Folder $folder -Image $img)
    Assert-Equal 'restart-required' $r.Status 'status'
    Assert-Equal 3010 (Get-ExitCodeForStatus $r.Status) 'exit code'
}

Test-Case 'install: a missing cognita.exe is a warning, not a failure' {
    $folder = New-Dir 'p'; $img = New-FakeImage; $script:ownerId = 'x'
    Add-InstallFakes
    Remove-Item -LiteralPath (Join-Path (Join-Path $env:COGNITA_HOME 'bin') 'cognita.exe') -Force
    $script:Interactive = $true; $script:SecretAnswer = 'pw'
    $r = Invoke-InstallVerb -Opts (Get-InstallOpts -Folder $folder -Image $img)
    Assert-Equal 'ok' $r.Status 'ok'
    Assert-Equal 1 @((Get-ProgressObjects) | Where-Object { $_.stage -eq 'command' -and $_.state -eq 'warning' }).Count 'warning'
}

Test-Case 'install: the whole verb through Invoke-HelperMain ends with a result line and exit 0' {
    $folder = New-Dir 'p'; $img = New-FakeImage; $script:ownerId = 'x'
    Add-InstallFakes
    $script:Interactive = $true; $script:SecretAnswer = 'pw'
    $argv = @('install', '--projects-folder', $folder, '--image', $img.Path, '--image-sha256', $img.Sha, '--data-dir', (Join-Path $script:TestDir 'vhd'))
    $code = Invoke-HelperMain -Argv $argv
    Assert-Equal 0 $code 'exit code'
    Assert-Match ((Get-OutLines)[-1]) '^result=ok;version=14\.1\.0;public_url=;admin_url=http://127\.0\.0\.1:8676;' 'last line'
}

function Add-InstallEnvFake {
    # Answers the helper's install.env read (a script on stdin to "sh -s" as the Linux user) with $Text;
    # any other Linux-user script gets the default empty success (first match wins, so this goes first).
    param([string]$Text)
    $script:InstallEnvText = $Text
    Add-ExtRule '-u cognita --exec sh -s' { param($c) if ([string]$c.StdinText -match 'install\.env') { New-ExtResult -Stdout $script:InstallEnvText } else { New-ExtResult } } -First
}

Test-Case 'install (design 21.3): after a skipped self-test the result line carries proof=skipped, and settings and state remember it' {
    $folder = New-Dir 'p'; $img = New-FakeImage; $script:ownerId = 'x'
    Add-InstallFakes
    Add-InstallEnvFake "COGNITA_PROOF=skipped`nCOGNITA_VERSION=14.1.0`n"
    $script:Interactive = $true; $script:SecretAnswer = 'pw'
    $r = Invoke-InstallVerb -Opts (Get-InstallOpts -Folder $folder -Image $img)
    Assert-Equal 'ok' $r.Status 'ok'
    Assert-Equal 'skipped' $r.Values['proof'] 'proof value'
    Assert-Match (Format-ResultLine 'ok' $r.Values) ';proof=skipped$' 'on the result line'
    Assert-Equal 'skipped' (Read-Settings).linux_proof 'recorded in settings.json'
    Assert-Match (Get-LogText) 'install: finished in \d+s version=14\.1\.0 proof=\[skipped\]' 'logged with its value'
}

Test-Case 'install (design 21.3): a passed self-test says proof=passed; nothing readable says an empty proof (the key is still on the line)' {
    $folder = New-Dir 'p'; $img = New-FakeImage; $script:ownerId = 'x'
    Add-InstallFakes
    Add-InstallEnvFake "COGNITA_PROOF=passed`n"
    $script:Interactive = $true; $script:SecretAnswer = 'pw'
    $r = Invoke-InstallVerb -Opts (Get-InstallOpts -Folder $folder -Image $img)
    Assert-Equal 'passed' $r.Values['proof'] 'passed'
    $script:ExtRules.Clear(); $script:ExtCalls.Clear()
    Add-InstallFakes
    $r2 = Invoke-InstallVerb -Opts (Get-InstallOpts -Folder $folder -Image $img)
    Assert-Equal 'ok' $r2.Status 'ok'
    Assert-Equal '' $r2.Values['proof'] 'unknown is empty'
    Assert-Match (Format-ResultLine 'ok' $r2.Values) ';proof=$' 'the key is on the line, empty'
}

# ---- update --------------------------------------------------------------------------------------
function Add-UpdateFakes {
    param([int]$LinuxExit = 0, [int]$TreeExit = 0, [int]$DockerExit = 0)
    $script:versions = @('14.0.0', '14.1.0')
    $script:statusCalls = 0
    $script:treeExit = $TreeExit; $script:dockerExit = $DockerExit; $script:linuxExit = $LinuxExit
    Add-ExtRule 'cognita status --json' { param($c) $v = $script:versions[[Math]::Min($script:statusCalls, 1)]; $script:statusCalls++; New-ExtResult -Stdout ('{"running": true, "version": "' + $v + '"}') }
    Add-ExtRule '-u root --exec sh -s' {
        param($c)
        if ($c.StdinText -match 'apt-get') { return (New-ExtResult -ExitCode $script:dockerExit -Stderr 'apt failed') }
        if ($c.StdinText -match '/opt/cognita/trees') { return (New-ExtResult -ExitCode $script:treeExit -Stderr 'tar: error') }
        foreach ($m in [regex]::Matches([string]$c.StdinText, '(/mnt/cognita-roots/\d) drvfs')) { $script:FstabRoots[$m.Groups[1].Value] = $true }
        return (New-ExtResult)
    }
    Add-ExtRule '/usr/local/bin/cognita update' { param($c) New-ExtResult -ExitCode $script:linuxExit -Stderr 'boom' }
    # The fake distro (design 18.1): its fstab already holds the CURRENT line for every root in settings
    # (so the update finds nothing to rewrite), and every root is mounted in Docker's view. Tests that want
    # an old-shape line or an unmounted root change $script:UpdFstab / $script:mounted afterwards.
    Add-MountWorldFakes
    $fstab = "/dev/sda / ext4 defaults 0 1`n" + (Get-FstabBaseLine) + "`n"
    foreach ($r in (Get-SettingsRoots (Read-Settings))) {
        $fstab += (Get-FstabLineForRoot -WindowsPath ([string]$r.windows) -N ([int]$r.n)) + "`n"
        $script:mounted[[string]$r.linux] = $true
        $script:FstabRoots[[string]$r.linux] = $true
    }
    $script:UpdFstab = $fstab
    $script:ExtRules.Insert(0, @{ Pattern = 'cat /etc/fstab'; Response = { param($c) New-ExtResult -Stdout $script:UpdFstab } })
}
function Get-UpdateOptsPath { return (Join-Path $script:TestDir 'cognita-src-14.1.0.tar.gz') }
function Get-UpdateOpts {
    $tar = Join-Path $script:TestDir 'cognita-src-14.1.0.tar.gz'
    [System.IO.File]::WriteAllBytes($tar, [byte[]](1..50))
    return (ConvertFrom-HelperArgs @('--src', $tar, '--src-sha256', ('ab' * 32), '--setup-version', '14.1.0', '--setup-revision', '2', '--disk-bytes', [string](10GB))).Opts
}

Test-Case 'update: roots checked, Docker upgraded, tree swapped, Linux update --no-pull with the password on stdin, keepalive re-registered' {
    $s = New-TestSettings -RootPaths @('C:\Docs')
    Add-UpdateFakes
    $script:Interactive = $true; $script:SecretAnswer = $script:Pw
    $opts = Get-UpdateOpts
    $r = Invoke-UpdateVerb -Opts $opts
    Assert-Equal 'ok' $r.Status ("status: " + ($script:Out | Select-Object -Last 3))
    Assert-Equal '14.0.0' $r.Values['from'] 'from'
    Assert-Equal '14.1.0' $r.Values['to'] 'to'
    $lines = Get-ExtCallLines
    $iMp = [array]::IndexOf(@($lines | ForEach-Object { $_ -match 'mountpoint -q' }), $true)
    $iApt = [array]::IndexOf(@($script:ExtCalls | ForEach-Object { $_.StdinText -match 'apt-get' }), $true)
    $iTree = [array]::IndexOf(@($script:ExtCalls | ForEach-Object { $_.StdinText -match '/opt/cognita/trees' }), $true)
    $iUp = [array]::IndexOf(@($lines | ForEach-Object { $_ -match 'cognita update' }), $true)
    Assert-True (($iMp -ge 0) -and ($iMp -lt $iApt) -and ($iApt -lt $iTree) -and ($iTree -lt $iUp)) 'order: roots, docker, tree, linux update'
    $tree = $script:ExtCalls[$iTree]
    Assert-Equal ((ConvertTo-WslMntPath (Get-UpdateOptsPath)) + '|' + ('ab' * 32) + '|14.1.0') ($tree.Arguments[-3..-1] -join '|') 'tree script arguments: tarball, sha256, version (the commit comes from the tree''s .cognita-tree)'
    Assert-Match $tree.StdinText 'mv -T /opt/cognita/src.new /opt/cognita/src' 'atomic symlink swap'
    Assert-Match $tree.StdinText 'tail -n \+4' 'keeps the newest three trees'
    $up = $script:ExtCalls[$iUp]
    Assert-Match ($up.Arguments -join ' ') 'update --no-pull --non-interactive --admin-password-stdin --progress-file' 'flags'
    Assert-Equal ($script:Pw + "`n") $up.StdinText 'password on stdin'
    Assert-Equal 1 $script:TaskRegistered.Count 'keepalive re-registered'
    Assert-Equal '14.1.0' (Read-Settings).setup_version 'setup version recorded'
    Assert-Equal '14.1.0' (Read-Settings).linux_installed_version 'design 18.2: linux_installed_version recorded after cognita update exited 0'
    Assert-Equal 0 $script:Terminates 'the fstab lines were already current, so the distro was not restarted'
    Assert-Equal 0 (Get-ExtCallsMatching '--exec mount').Count 'no mount'
    Assert-NotMatch (((Get-ExtCallLines) -join "`n") + (Get-LogText)) ([regex]::Escape($script:Pw)) 'password nowhere'
}

Test-Case 'update (design 21.3): the result line carries proof= read from install.env after cognita update exits 0; a FAILED update reads nothing' {
    $s = New-TestSettings -RootPaths @('C:\Docs')
    Add-UpdateFakes
    Add-InstallEnvFake "COGNITA_PROOF=skipped`n"
    $script:Interactive = $true; $script:SecretAnswer = $script:Pw
    $r = Invoke-UpdateVerb -Opts (Get-UpdateOpts)
    Assert-Equal 'ok' $r.Status 'ok'
    Assert-Equal 'skipped' $r.Values['proof'] 'proof=skipped'
    Assert-Equal 'skipped' (Read-Settings).linux_proof 'recorded'
    $script:ExtRules.Clear(); $script:ExtCalls.Clear()
    Add-UpdateFakes -LinuxExit 1
    Add-InstallEnvFake "COGNITA_PROOF=skipped`n"
    $r2 = Invoke-UpdateVerb -Opts (Get-UpdateOpts)
    Assert-Equal 'failed' $r2.Status 'failed'
    Assert-Equal 0 @($script:ExtCalls | Where-Object { [string]$_.StdinText -match 'install\.env' }).Count 'install.env is not read after a failed run'
}

Test-Case 'update (design 18.1 rule 5): an fstab line from before `shared` is rewritten and the distro restarted ONCE before Docker and the tree are touched' {
    $s = New-TestSettings -RootPaths @('C:\Docs')
    Add-UpdateFakes
    $script:UpdFstab = "/dev/sda / ext4 defaults 0 1`nC:/Docs /mnt/cognita-roots/1 drvfs uid=1000,gid=1000,noatime,nofail 0 0`n"
    $script:Interactive = $true; $script:SecretAnswer = 'pw'
    $r = Invoke-UpdateVerb -Opts (Get-UpdateOpts)
    Assert-Equal 'ok' $r.Status ("status: " + ($script:Out | Select-Object -Last 3))
    Assert-Equal 1 $script:Terminates 'one restart'
    $calls = @($script:ExtCalls)
    $iFstab = [array]::IndexOf(@($calls | ForEach-Object { $_.StdinText -match 'fstab\.cognita-new' }), $true)
    $iTerm = [array]::IndexOf(@($calls | ForEach-Object { $_.Line -match '--terminate Cognita' }), $true)
    $iApt = [array]::IndexOf(@($calls | ForEach-Object { $_.StdinText -match 'apt-get' }), $true)
    Assert-True (($iFstab -ge 0) -and ($iFstab -lt $iTerm) -and ($iTerm -lt $iApt)) 'fstab rewrite, restart, then Docker'
    Assert-Match $calls[$iFstab].StdinText 'nofail,shared 0 0' 'the new line has shared'
    Assert-Equal 0 (Get-ExtCallsMatching '--exec mount').Count 'no mount'
}

Test-Case 'update (design 22.14): an install with current root lines but no base line gets it, before root 1, and ONE restart' {
    $s = New-TestSettings -RootPaths @('C:\Docs')
    Add-UpdateFakes
    $r1 = Get-FstabLineForRoot -WindowsPath 'C:\Docs' -N 1
    $script:UpdFstab = "/dev/sda / ext4 defaults 0 1`n" + $r1 + "`n"
    $script:Interactive = $true; $script:SecretAnswer = 'pw'
    $r = Invoke-UpdateVerb -Opts (Get-UpdateOpts)
    Assert-Equal 'ok' $r.Status ("status: " + ($script:Out | Select-Object -Last 3))
    Assert-Equal 1 $script:Terminates 'one restart'
    $w = @($script:ExtCalls | Where-Object { [string]$_.StdinText -match 'fstab\.cognita-new' })
    Assert-Equal 1 $w.Count 'only the base line was written; the root line was already current'
    Assert-True ($w[0].StdinText.Contains("/dev/sda / ext4 defaults 0 1`n" + (Get-FstabBaseLine) + "`n" + $r1 + "`n")) 'base before root 1'
    Assert-Match (Get-LogText) 'update: fstab lines rewritten=1' 'logged'
}

Test-Case 'update: a distro that does not come back after the fstab restart stops the update before anything else' {
    $s = New-TestSettings -RootPaths @('C:\Docs')
    Add-UpdateFakes
    $script:UpdFstab = "/dev/sda / ext4 defaults 0 1`nC:/Docs /mnt/cognita-roots/1 drvfs uid=1000,gid=1000,noatime,nofail 0 0`n"
    $script:ExtRules.Insert(0, @{ Pattern = 'systemctl is-system-running'; Response = (New-ExtResult -ExitCode 1 -Stdout "starting`n") })
    $script:Interactive = $true; $script:SecretAnswer = 'pw'
    $r = Invoke-UpdateVerb -Opts (Get-UpdateOpts)
    Assert-Equal 'failed' $r.Status 'failed'
    Assert-Equal 'distro-not-ready' $r.Values['reason'] 'reason'
    Assert-Equal 0 (Get-ExtCallsMatching 'apt-get|cognita update|trees').Count 'nothing after it ran'
}

Test-Case 'update (design 18.2): a failed cognita update does not record the new version' {
    $s = New-TestSettings -RootPaths @('C:\Docs')
    Set-SettingProp $s 'linux_installed_version' '14.0.0'; Save-Settings $s
    Add-UpdateFakes -LinuxExit 1
    $script:Interactive = $true; $script:SecretAnswer = 'pw'
    $r = Invoke-UpdateVerb -Opts (Get-UpdateOpts)
    Assert-Equal 'failed' $r.Status 'failed'
    Assert-Equal '14.0.0' (Read-Settings).linux_installed_version 'still the old version'
}
Test-Case 'update: a root that is down stops the update before Docker or the tree are touched' {
    $s = New-TestSettings -RootPaths @('D:\Gone')
    Add-UpdateFakes
    $script:ExtRules.Insert(0, @{ Pattern = 'mountpoint -q'; Response = (New-ExtResult -ExitCode 1) })
    $script:Interactive = $true; $script:SecretAnswer = 'pw'
    $r = Invoke-UpdateVerb -Opts (Get-UpdateOpts)
    Assert-Equal 'root-down' $r.Values['reason'] 'reason'
    Assert-Equal 0 (Get-ExtCallsMatching 'sh -s|cognita update').Count 'nothing else ran'
    $f = @((Get-ProgressObjects) | Where-Object { $_.state -eq 'failed' })[0]
    Assert-Match $f.message 'D:\\Gone is not available to Cognita' 'the message names the folder'
}

Test-Case 'update: a Docker upgrade failure is a warning and the update continues' {
    $s = New-TestSettings
    Add-UpdateFakes -DockerExit 100
    $script:Interactive = $true; $script:SecretAnswer = 'pw'
    $r = Invoke-UpdateVerb -Opts (Get-UpdateOpts)
    Assert-Equal 'ok' $r.Status 'still ok'
    Assert-Equal 1 @((Get-ProgressObjects) | Where-Object { $_.stage -eq 'update.docker' -and $_.state -eq 'warning' }).Count 'a warning'
}

Test-Case 'update: a tree failure stops before the Linux update' {
    $s = New-TestSettings
    Add-UpdateFakes -TreeExit 20
    $script:Interactive = $true; $script:SecretAnswer = 'pw'
    $r = Invoke-UpdateVerb -Opts (Get-UpdateOpts)
    Assert-Equal 'tree' $r.Values['reason'] 'reason'
    Assert-Equal 0 (Get-ExtCallsMatching 'cognita update').Count 'no Linux update'
}

Test-Case 'update (design 19.3 item 10b): the helper writes its own failed line ONLY when Linux wrote none, and "To go back: cognita rollback" ONLY when the release switch stage was done' {
    $script:LinuxStartDone = '{"schema": 1, "time": "2026-03-01T09:00:01", "stage": "start", "title": "Starting Cognita", "state": "done"}'
    $script:LinuxStartBegun = '{"schema": 1, "time": "2026-03-01T09:00:01", "stage": "start", "title": "Starting Cognita", "state": "start"}'
    $script:LinuxFailedLine = '{"schema": 1, "time": "2026-03-01T09:00:02", "stage": "proof", "title": "Proving it works", "state": "failed", "message": "The proof failed.", "fix": "Go back with: cognita rollback"}'
    # a) Linux failed before the switch and wrote nothing failed (a crash): the helper's own line, NO rollback advice
    $s = New-TestSettings
    Add-UpdateFakes -LinuxExit 1
    $script:Interactive = $true; $script:SecretAnswer = 'pw'
    $script:ExtRules.Insert(0, @{ Pattern = '/usr/local/bin/cognita update'; Response = { param($c) Write-ProgressFileLines ($script:LinuxStartBegun + "`n"); Invoke-ClockSleep 1000; & $c.OnPoll; New-ExtResult -ExitCode 1 -Stderr 'boom' } })
    $r = Invoke-UpdateVerb -Opts (Get-UpdateOpts)
    Assert-Equal 'linux-update' $r.Values['reason'] 'a: reason'
    $f = @((Get-ProgressObjects) | Where-Object { $_.state -eq 'failed' })
    Assert-Equal 1 $f.Count 'a: exactly one failed line (the helper''s)'
    Assert-Match $f[0].message '^The update failed\.' 'a: the helper''s own message'
    Assert-NotMatch $f[0].fix 'rollback' 'a: the switch did not finish, so no "go back" advice'
    Assert-Match (Get-LogText) 'update: Linux wrote no failed line; the helper wrote its own \(switchDone=False' 'a: decision logged with its value'
    # b) Linux switched the release (its `start` stage reached done) and then died without a failed line: rollback advice
    $script:ExtRules.Clear(); $script:Out.Clear(); $script:ExtCalls.Clear(); $script:LogLines.Clear()
    Add-UpdateFakes -LinuxExit 1
    $script:ExtRules.Insert(0, @{ Pattern = '/usr/local/bin/cognita update'; Response = { param($c) Write-ProgressFileLines ($script:LinuxStartBegun + "`n" + $script:LinuxStartDone + "`n"); Invoke-ClockSleep 1000; & $c.OnPoll; New-ExtResult -ExitCode 1 -Stderr 'boom' } })
    $r2 = Invoke-UpdateVerb -Opts (Get-UpdateOpts)
    Assert-Equal 'linux-update' $r2.Values['reason'] 'b: reason'
    $f2 = @((Get-ProgressObjects) | Where-Object { $_.state -eq 'failed' })
    Assert-Equal 1 $f2.Count 'b: one failed line'
    Assert-Equal 'To go back: cognita rollback' $f2[0].fix 'b: the rollback hint, because the switch is done'
    Assert-Match (Get-LogText) 'switchDone=True' 'b: logged'
    # c) Linux wrote its own failed line: the helper adds NOTHING (Linux's message and fix stand on Setup's page)
    $script:ExtRules.Clear(); $script:Out.Clear(); $script:ExtCalls.Clear(); $script:LogLines.Clear()
    Add-UpdateFakes -LinuxExit 1
    $script:ExtRules.Insert(0, @{ Pattern = '/usr/local/bin/cognita update'; Response = { param($c) Write-ProgressFileLines ($script:LinuxStartDone + "`n" + $script:LinuxFailedLine + "`n"); Invoke-ClockSleep 1000; & $c.OnPoll; New-ExtResult -ExitCode 1 -Stderr 'boom' } })
    $r3 = Invoke-UpdateVerb -Opts (Get-UpdateOpts)
    Assert-Equal 'linux-update' $r3.Values['reason'] 'c: reason'
    $f3 = @((Get-ProgressObjects) | Where-Object { $_.state -eq 'failed' })
    Assert-Equal 1 $f3.Count 'c: only Linux''s failed line, relayed'
    Assert-Equal 'The proof failed.' $f3[0].message 'c: it is Linux''s message'
    Assert-Equal 0 @((Get-ProgressObjects) | Where-Object { $_.message -like 'The update failed.*' }).Count 'c: no helper line'
    Assert-Match (Get-LogText) 'update: Linux wrote its own failed line; the helper writes none' 'c: logged'
}

Test-Case 'update (design 19.3 item 3): a stopped Cognita is started first: the stopped flag is removed, the keepalive task started and the distro awaited BEFORE anything runs in the distro' {
    $s = New-TestSettings -RootPaths @('C:\Docs')
    Add-UpdateFakes
    [System.IO.File]::WriteAllText((Get-StoppedFlagPath), 'x')
    $script:Interactive = $true; $script:SecretAnswer = 'pw'
    $r = Invoke-UpdateVerb -Opts (Get-UpdateOpts)
    Assert-Equal 'ok' $r.Status ("status: " + ($script:Out | Select-Object -Last 3))
    Assert-False (Test-Path -LiteralPath (Get-StoppedFlagPath)) 'the stopped flag is gone'
    Assert-True ($script:TaskStarted -ge 1) 'the keepalive task was started'
    $calls = @($script:ExtCalls)
    $iReady = [array]::IndexOf(@($calls | ForEach-Object { $_.Line -match 'systemctl is-system-running' }), $true)
    $iStatus = [array]::IndexOf(@($calls | ForEach-Object { $_.Line -match 'cognita status --json' }), $true)
    $iApt = [array]::IndexOf(@($calls | ForEach-Object { $_.StdinText -match 'apt-get' }), $true)
    Assert-True (($iReady -ge 0) -and ($iReady -lt $iStatus) -and ($iStatus -lt $iApt)) ("the readiness wait comes first, then the status read, then Docker; got {0} {1} {2}" -f $iReady, $iStatus, $iApt)
    $stages = @((Get-ProgressObjects) | Where-Object { $_.stage -eq 'keepalive' } | ForEach-Object { $_.state })
    Assert-Equal 'start|done' ($stages -join '|') 'keepalive start and done lines'
    Assert-Match (Get-LogText) 'keepalive: removed the stopped flag' 'the flag removal is logged'
    Assert-Match (Get-LogText) 'update: keepalive started=True; waiting for the distro' 'the start is logged with its value'
    # and a distro that never comes up stops the update with the reason, before anything else ran
    $script:ExtRules.Clear(); $script:Out.Clear(); $script:ExtCalls.Clear()
    Add-UpdateFakes
    $script:ExtRules.Insert(0, @{ Pattern = 'systemctl is-system-running'; Response = (New-ExtResult -ExitCode 1 -Stdout "starting`n") })
    $r2 = Invoke-UpdateVerb -Opts (Get-UpdateOpts)
    Assert-Equal 'failed' $r2.Status 'failed'
    Assert-Equal 'distro-not-ready' $r2.Values['reason'] 'reason'
    Assert-Equal 0 (Get-ExtCallsMatching 'status --json|apt-get|cognita update|trees').Count 'nothing else ran'
}

Test-Case 'Compare-DottedVersion (design 19.2 item 6): numeric, dotted, missing parts are 0, unparseable is null' {
    Assert-Equal 1 (Compare-DottedVersion '14.10.0' '14.9.0') '14.10.0 is newer than 14.9.0 (a text compare says the opposite)'
    Assert-Equal -1 (Compare-DottedVersion '14.2.1' '14.2.2') 'older'
    Assert-Equal 0 (Compare-DottedVersion '14.2' '14.2.0') 'a missing part is 0'
    Assert-Equal 0 (Compare-DottedVersion '14.2.2' '14.2.2') 'equal'
    Assert-Equal 1 (Compare-DottedVersion '15.0.0' '14.99.99') 'the major wins'
    Assert-True ($null -eq (Compare-DottedVersion '14.2.x' '14.2.2')) 'not numeric: null'
    Assert-True ($null -eq (Compare-DottedVersion '' '14.2.2')) 'empty: null'
}

Test-Case 'update (design 19.2 item 6): an installed Cognita NEWER than this Setup is refused (older-setup) before the password, the distro or anything else; equal or older installed proceeds' {
    $s = New-TestSettings -RootPaths @('C:\Docs')
    Set-SettingProp $s 'linux_installed_version' '14.10.0'; Save-Settings $s
    Add-UpdateFakes
    $script:Interactive = $false      # no password source at all: the refusal must come first
    $r = Invoke-UpdateVerb -Opts (Get-UpdateOpts)    # --setup-version 14.1.0
    Assert-Equal 'failed' $r.Status 'failed'
    Assert-Equal 'older-setup' $r.Values['reason'] 'reason (not no-password: the guard runs first)'
    $f = @((Get-ProgressObjects) | Where-Object { $_.state -eq 'failed' })
    Assert-Equal 1 $f.Count 'one failed line'
    Assert-Equal 'Cognita 14.10.0 is installed; this Setup is older (14.1.0).' $f[0].message 'the message'
    Assert-Equal 'Use a newer Setup, or cognita rollback to go back a release.' $f[0].fix 'the fix'
    Assert-Equal 0 (Get-ExtCallLines).Count 'nothing ran in WSL or anywhere else'
    Assert-Equal 0 $script:TaskStarted 'the keepalive was not touched'
    Assert-Equal '14.10.0' (Read-Settings).linux_installed_version 'the record is unchanged'
    Assert-Match (Get-LogText) 'update: version guard installed=\[14\.10\.0\] setup=\[14\.1\.0\] compare=1' 'the comparison is logged with its values'
    # equal and older installed versions go on to update
    foreach ($installed in @('14.1.0', '14.0.0')) {
        $script:ExtRules.Clear(); $script:Out.Clear(); $script:ExtCalls.Clear()
        $s2 = Read-Settings; Set-SettingProp $s2 'linux_installed_version' $installed; Save-Settings $s2
        Add-UpdateFakes
        $script:Interactive = $true; $script:SecretAnswer = 'pw'
        $ok = Invoke-UpdateVerb -Opts (Get-UpdateOpts)
        Assert-Equal 'ok' $ok.Status ("installed $installed with Setup 14.1.0 updates")
    }
}

Test-Case 'update: not installed, or no source package, fail cleanly' {
    $r = Invoke-UpdateVerb -Opts @{}
    Assert-Equal 'not-installed' $r.Values['reason'] 'not installed'
    $s = New-TestSettings
    Add-UpdateFakes
    $script:Interactive = $true; $script:SecretAnswer = 'pw'
    $r2 = Invoke-UpdateVerb -Opts @{}
    Assert-Equal 'no-source' $r2.Values['reason'] 'no tarball'
}

Test-Case 'tree install script: sha check, stamp and commit from .cognita-tree, atomic swap, LF only' {
    $t = Get-TreeInstallScript
    Assert-Match $t 'sha256sum "\$tarball" </dev/null' 'checksum with stdin closed so it cannot eat the script'
    Assert-Match $t 'exit 20' 'checksum mismatch exits 20'
    Assert-Match $t 'tar -xzf "\$tarball" -C "\$tmp"' 'contents at the tarball root, extracted into the tree folder'
    Assert-Match $t '\.cognita-tree' 'stamp required'
    Assert-Match $t 'name="\$ver-\$\(printf' 'name is <version>-<first 12 of the commit>'
    Assert-Match $t 'cut -c1-12' 'commit12'
    Assert-Match $t 'ln -s "\$base/\$name" /opt/cognita/src\.new' 'link to the new tree'
    Assert-False ($t.Contains("`r")) 'LF only'
}

# ---- NVIDIA acceleration and the .wslconfig line (design 22.3, 22.4, 22.9, 22.12) -------------------------
$script:NvStatusJson = '{"installed": true, "running": true, "version": "14.1.0", "admin_url": "http://127.0.0.1:8676", "mcp_url": "http://127.0.0.1:8675", "public_url": null, "workspace": "on", "acceleration": "nvidia"}'
# What status says when the Linux CLI fell back to the CPU by itself.
$script:CliFallbackStatusJson = $script:StatusJson
$script:NoAccelStatusJson ='{"installed": true, "running": true, "version": "14.1.0", "admin_url": "http://127.0.0.1:8676", "mcp_url": "http://127.0.0.1:8675", "public_url": null, "workspace": "on"}'

function Add-InstallToolkitFakes {
    # The toolkit's two scripts, told apart by what they read on stdin (the check reads `docker info`, the
    # install runs apt-get). Both are the "-u root --exec sh -s -- <pin>" form; the fstab writes and the
    # Docker upgrade are NOT (no "--"), so Add-InstallFakes / Add-UpdateFakes keep answering those.
    param([int]$CheckExit = 0, [int]$ToolkitInstallExit = 0)
    $script:TkCheckExit = $CheckExit; $script:TkInstallExit = $ToolkitInstallExit
    Add-ExtRule '-u root --exec sh -s -- 1\.20\.1-1$' {
        param($c)
        if ([string]$c.StdinText -match 'apt-get install') {
            if ($script:TkInstallExit -eq 0) { return (New-ExtResult -Stdout "toolkit: installed 1.20.1-1 (was none)`n") }
            return (New-ExtResult -ExitCode $script:TkInstallExit -Stdout "toolkit: rolled back`n")
        }
        if ($script:TkCheckExit -eq 0) { return (New-ExtResult -Stdout "toolkit: present 1.20.1-1`n") }
        return (New-ExtResult -ExitCode $script:TkCheckExit -Stdout "toolkit: nvidia-container-toolkit is absent, not 1.20.1-1`n")
    } -First
}
function Get-ToolkitCalls { return ,@($script:ExtCalls | Where-Object { $_.Line -match '-u root --exec sh -s -- 1\.20\.1-1$' }) }
function Get-CallIndex { param([string]$Pattern, [int]$Nth = 1) $n = 0; $i = 0; foreach ($c in @($script:ExtCalls)) { if ($c.Line -match $Pattern) { $n++; if ($n -eq $Nth) { return $i } }; $i++ }; return -1 }

Test-Case 'Get-LinuxInstallArgs (22.3): nvidia adds --acceleration nvidia --acceleration-fallback cpu, cpu adds --acceleration cpu, empty adds nothing; all after --workspace' {
    $base = (Get-LinuxInstallArgs -Display 'C:\Docs' -AdminUser 'boss' -Workspace 'on' -McpPort 8675 -AdminPort 8676) -join '|'
    Assert-Equal 'install|--non-interactive|--yes|--documents|/mnt/cognita-roots/1|--documents-display|C:\Docs|--admin-user|boss|--admin-password-stdin|--command-name|cognita|--remote-access|no|--workspace|on|--mcp-port|8675|--admin-port|8676' $base 'unchanged without the parameter'
    Assert-Equal $base ((Get-LinuxInstallArgs -Display 'C:\Docs' -AdminUser 'boss' -Workspace 'on' -McpPort 8675 -AdminPort 8676 -Acceleration '') -join '|') 'empty adds nothing'
    $n = (Get-LinuxInstallArgs -Display 'C:\Docs' -AdminUser '' -Workspace 'off' -McpPort 9000 -AdminPort 9001 -Acceleration 'nvidia') -join '|'
    Assert-Match $n '--workspace\|off\|--acceleration\|nvidia\|--acceleration-fallback\|cpu\|--mcp-port\|9000\|--admin-port\|9001$' 'nvidia, straight after --workspace'
    Assert-NotMatch $n '--admin-user' 'no admin user when empty'
    $c = (Get-LinuxInstallArgs -Display 'C:\Docs' -AdminUser '' -Workspace 'off' -McpPort 9000 -AdminPort 9001 -Acceleration 'cpu') -join '|'
    Assert-Match $c '--workspace\|off\|--acceleration\|cpu\|--mcp-port' 'cpu'
    Assert-NotMatch $c 'fallback' 'no fallback flag for cpu'
}

Test-Case 'install (22.3): --acceleration nvidia with the toolkit missing installs it as root BEFORE the Linux CLI, the CLI gets the fallback flag, and settings and the result carry acceleration=nvidia from status' {
    $folder = New-Dir 'p'; $img = New-FakeImage; $script:ownerId = 'x'
    Add-InstallFakes -Status $script:NvStatusJson
    Add-InstallToolkitFakes -CheckExit 1
    $script:Interactive = $true; $script:SecretAnswer = 'pw'
    $r = Invoke-InstallVerb -Opts (Get-InstallOpts -Folder $folder -Image $img -Extra @('--acceleration', 'nvidia'))
    Assert-Equal 'ok' $r.Status ("status: " + ($script:Out | Select-Object -Last 3))
    $tk = Get-ToolkitCalls
    Assert-Equal 2 $tk.Count 'the check, then the install'
    Assert-True ((Get-CallIndex '-u root --exec sh -s -- 1\.20\.1-1$' 2) -lt (Get-CallIndex 'cognita install')) 'the toolkit install ran before the Linux CLI'
    $cli = (Get-ExtCallsMatching 'cognita install')[0]
    Assert-Match ($cli.Arguments -join '|') '--workspace\|on\|--acceleration\|nvidia\|--acceleration-fallback\|cpu\|--mcp-port\|8675\|--admin-port\|8676\|--progress-file' 'CLI argv'
    Assert-Equal 'nvidia' $r.Values['acceleration'] 'result value'
    Assert-Match (Format-ResultLine 'ok' $r.Values) ';acceleration=nvidia;proof=' 'on the result line'
    Assert-Equal 'nvidia' (Read-Settings).acceleration 'settings'
    Assert-Match (Get-LogText) 'install: decision .* acceleration=\[nvidia\]' 'the decision line carries the asked value'
    $nv = @((Get-ProgressObjects) | Where-Object { $_.stage -eq 'nvidia' })
    Assert-Equal 'start,done' (($nv | ForEach-Object { $_.state }) -join ',') 'progress: start, done'
}

Test-Case 'install (22.3, 22.12 item 6): --acceleration nvidia with the toolkit already present runs the check only: no install call, no nvidia progress line' {
    $folder = New-Dir 'p'; $img = New-FakeImage; $script:ownerId = 'x'
    Add-InstallFakes -Status $script:NvStatusJson
    Add-InstallToolkitFakes -CheckExit 0
    $script:Interactive = $true; $script:SecretAnswer = 'pw'
    $r = Invoke-InstallVerb -Opts (Get-InstallOpts -Folder $folder -Image $img -Extra @('--acceleration', 'nvidia'))
    Assert-Equal 'ok' $r.Status 'ok'
    Assert-Equal 1 (Get-ToolkitCalls).Count 'one call: the check'
    Assert-Equal 0 @((Get-ProgressObjects) | Where-Object { $_.stage -eq 'nvidia' }).Count 'nothing shown'
    Assert-Match ((Get-ExtCallsMatching 'cognita install')[0].Arguments -join '|') '--acceleration\|nvidia\|--acceleration-fallback\|cpu' 'the fallback flag is still passed'
}

Test-Case 'install (22.3, 22.12 item 13): a toolkit that fails (rolled back, exit 3) is a warning and the Linux CLI STILL runs with --acceleration nvidia --acceleration-fallback cpu; the install succeeds' {
    $folder = New-Dir 'p'; $img = New-FakeImage; $script:ownerId = 'x'
    Add-InstallFakes -Status $script:CliFallbackStatusJson
    Add-InstallToolkitFakes -CheckExit 1 -ToolkitInstallExit 3
    $script:Interactive = $true; $script:SecretAnswer = 'pw'
    $r = Invoke-InstallVerb -Opts (Get-InstallOpts -Folder $folder -Image $img -Extra @('--acceleration', 'nvidia'))
    Assert-Equal 'ok' $r.Status 'the install is a success'
    $w = @((Get-ProgressObjects) | Where-Object { $_.stage -eq 'nvidia' -and $_.state -eq 'warning' })
    Assert-Equal 1 $w.Count 'one warning'
    Assert-Match $w[0].message "^Setup could not install NVIDIA's container support in Cognita's Linux \(.+\)\. If Cognita cannot use the card without it, it uses the CPU\.$" 'wording'
    Assert-Equal 'Run Setup again later.' $w[0].fix 'fix'
    Assert-Equal 1 (Get-ExtCallsMatching 'cognita install').Count 'the Linux CLI ran'
    Assert-Match ((Get-ExtCallsMatching 'cognita install')[0].Arguments -join '|') '--acceleration\|nvidia\|--acceleration-fallback\|cpu' 'with the fallback flag'
    Assert-Equal 'cpu' $r.Values['acceleration'] 'what Linux ended up with is what is recorded'
    Assert-Equal 'cpu' (Read-Settings).acceleration 'settings say cpu'
}
Test-Case 'install (22.3): --acceleration cpu passes --acceleration cpu and never touches the toolkit; absent passes no acceleration flag and never touches the toolkit' {
    $folder = New-Dir 'p'; $img = New-FakeImage; $script:ownerId = 'x'
    Add-InstallFakes
    Add-InstallToolkitFakes
    $script:Interactive = $true; $script:SecretAnswer = 'pw'
    $r = Invoke-InstallVerb -Opts (Get-InstallOpts -Folder $folder -Image $img -Extra @('--acceleration', 'cpu'))
    Assert-Equal 'ok' $r.Status 'ok'
    $a = ((Get-ExtCallsMatching 'cognita install')[0].Arguments -join '|')
    Assert-Match $a '--workspace\|on\|--acceleration\|cpu\|--mcp-port' 'cpu flag'
    Assert-NotMatch $a 'fallback' 'no fallback for cpu'
    Assert-Equal 0 (Get-ToolkitCalls).Count 'no toolkit call'
    $script:ExtRules.Clear(); $script:ExtCalls.Clear()
    Add-InstallFakes
    Add-InstallToolkitFakes
    $r2 = Invoke-InstallVerb -Opts (Get-InstallOpts -Folder $folder -Image $img)
    Assert-Equal 'ok' $r2.Status 'ok'
    Assert-NotMatch ((Get-ExtCallsMatching 'cognita install')[0].Arguments -join '|') '--acceleration' 'no acceleration flag at all'
    Assert-Equal 0 (Get-ToolkitCalls).Count 'no toolkit call'
    Assert-Match (Get-LogText) 'install: decision .* acceleration=\[\]' 'the decision line shows empty'
}

Test-Case 'install (22.3): any other --acceleration value, or one with no value, is bad-acceleration: failed before anything runs, with the Setup-bug line' {
    $folder = New-Dir 'p'; $img = New-FakeImage; $script:ownerId = 'x'
    foreach ($bad in @(@('--acceleration', 'amd'), @('--acceleration', 'gpu'), @('--acceleration', ''), @('--acceleration'))) {
        $script:Out.Clear(); $script:ExtRules.Clear(); $script:ExtCalls.Clear()
        Add-InstallFakes
        $script:Interactive = $true; $script:SecretAnswer = 'pw'
        $opts = (ConvertFrom-HelperArgs (@('--projects-folder', $folder, '--admin-user', 'boss', '--image', $img.Path, '--image-sha256', $img.Sha, '--data-dir', (Join-Path $script:TestDir 'vhd'), '--setup-version', '14.1.0') + $bad)).Opts
        $r = Invoke-InstallVerb -Opts $opts
        Assert-Equal 'failed' $r.Status ("failed for [" + ($bad -join ' ') + "]")
        Assert-Equal 'bad-acceleration' $r.Values['reason'] 'reason'
        $f = @((Get-ProgressObjects) | Where-Object { $_.state -eq 'failed' })
        Assert-Equal 1 $f.Count 'one failed line'
        Assert-Equal 'Setup passed an acceleration the helper does not know.' $f[0].message 'the text'
        Assert-Equal 0 $script:ExtCalls.Count 'nothing ran'
    }
}

Test-Case 'install (22.12 item 1a): a status that does not say acceleration never overwrites a known one with empty; the result says what status said (empty)' {
    $folder = New-Dir 'p'; $img = New-FakeImage; $script:ownerId = 'x'
    $s = New-TestSettings -State 'new'
    Set-SettingProp $s 'acceleration' 'nvidia'; Save-Settings $s
    Add-InstallFakes -Status $script:NoAccelStatusJson
    $script:Interactive = $true; $script:SecretAnswer = 'pw'
    $r = Invoke-InstallVerb -Opts (Get-InstallOpts -Folder $folder -Image $img)
    Assert-Equal 'ok' $r.Status 'ok'
    Assert-Equal 'nvidia' (Read-Settings).acceleration 'the known value stays'
    Assert-Equal '' $r.Values['acceleration'] 'result: status did not say'
    Assert-Match (Format-ResultLine 'ok' $r.Values) ';acceleration=;proof=' 'key present, empty'
    Assert-Match (Get-LogText) 'acceleration \(install\): status said \[\], settings had \[nvidia\], settings now \[nvidia\]' 'decision logged with values'
}

Test-Case 'install (22.9): without --wsl-memory-reclaim the .wslconfig is never touched' {
    $folder = New-Dir 'p'; $img = New-FakeImage; $script:ownerId = 'x'
    Add-InstallFakes
    $script:Interactive = $true; $script:SecretAnswer = 'pw'
    [System.IO.File]::WriteAllText($script:WslConfigPath, "[wsl2]`nmemory=12GB`n")
    $r0 = Invoke-InstallVerb -Opts (Get-InstallOpts -Folder $folder -Image $img)
    Assert-Equal 'ok' $r0.Status 'ok without the flag'
    Assert-Equal "[wsl2]`nmemory=12GB`n" ([System.IO.File]::ReadAllText($script:WslConfigPath)) 'untouched'
    Assert-False (Test-Path -LiteralPath ($script:WslConfigPath + '.cognita-backup')) 'no backup'
}

Test-Case 'install (22.9): --wsl-memory-reclaim writes the .wslconfig line FIRST (before the import or anything in the distro)' {
    $folder = New-Dir 'p'; $img = New-FakeImage; $script:ownerId = 'x'
    Add-InstallFakes
    $script:Interactive = $true; $script:SecretAnswer = 'pw'
    [System.IO.File]::WriteAllText($script:WslConfigPath, "[wsl2]`nmemory=12GB`n")
    $r = Invoke-InstallVerb -Opts (Get-InstallOpts -Folder $folder -Image $img -Extra @('--wsl-memory-reclaim'))
    Assert-Equal 'ok' $r.Status ("ok with the flag: " + ($script:Out | Select-Object -Last 3))
    Assert-Equal "[wsl2]`nmemory=12GB`n`n[experimental]`nautoMemoryReclaim=dropCache`n" ([System.IO.File]::ReadAllText($script:WslConfigPath)) 'the line was added'
    $log = @($script:LogLines)
    $iAdd = -1; $iExec = -1
    for ($i = 0; $i -lt $log.Count; $i++) { if ($iAdd -lt 0 -and $log[$i] -match 'wslconfig: added autoMemoryReclaim=dropCache') { $iAdd = $i }; if ($iExec -lt 0 -and $log[$i] -match 'exec\(fake\)') { $iExec = $i } }
    Assert-True (($iAdd -ge 0) -and ($iAdd -lt $iExec)) ("the .wslconfig change (log line {0}) came before the first process was started (log line {1})" -f $iAdd, $iExec)
    Assert-Match (Get-LogText) 'install: decision .* wslMemoryReclaim=True' 'the decision line shows the flag'
}

Test-Case 'install (22.9): a .wslconfig that cannot be changed is a warning and the install goes on to the end' {
    $folder = New-Dir 'p'; $img = New-FakeImage; $script:ownerId = 'x'
    Add-InstallFakes
    $script:Interactive = $true; $script:SecretAnswer = 'pw'
    [System.IO.File]::WriteAllText($script:WslConfigPath, "[wsl2]`nmemory=12GB`n")
    [void](New-Item -ItemType Directory -Path ($script:WslConfigPath + '.cognita-backup'))
    $r = Invoke-InstallVerb -Opts (Get-InstallOpts -Folder $folder -Image $img -Extra @('--wsl-memory-reclaim'))
    Assert-Equal 'ok' $r.Status 'the install still succeeds'
    Assert-Equal 1 @((Get-ProgressObjects) | Where-Object { $_.stage -eq 'wslconfig' -and $_.state -eq 'warning' }).Count 'one warning, stage wslconfig'
    Assert-Equal 1 (Get-ExtCallsMatching 'cognita install').Count 'the Linux CLI ran'
}

# ---- update ---------------------------------------------------------------------------------------------
function Set-UpdateStatusFake {
    # After the first status read (before) the next ones say $AfterJson, so the update's "after" read sees it.
    param([string]$AfterJson)
    $script:AfterStatus = $AfterJson
    $script:UpdStatusReads = 0
    Add-ExtRule 'cognita status --json' { param($c) $script:UpdStatusReads++; if ($script:UpdStatusReads -le 1) { New-ExtResult -Stdout '{"running": true, "version": "14.0.0", "acceleration": "nvidia"}' } else { New-ExtResult -Stdout $script:AfterStatus } } -First
}

Test-Case 'update (22.4): a recorded nvidia runs the toolkit step as 3b (after the Docker upgrade, before the tree); present means the check only' {
    $s = New-TestSettings -RootPaths @('C:\Docs')
    Set-SettingProp $s 'acceleration' 'nvidia'; Save-Settings $s
    Add-UpdateFakes
    Add-InstallToolkitFakes -CheckExit 0
    Set-UpdateStatusFake '{"running": true, "version": "14.1.0", "acceleration": "nvidia"}'
    $script:Interactive = $true; $script:SecretAnswer = 'pw'
    $r = Invoke-UpdateVerb -Opts (Get-UpdateOpts)
    Assert-Equal 'ok' $r.Status ("status: " + ($script:Out | Select-Object -Last 3))
    Assert-Equal 1 (Get-ToolkitCalls).Count 'only the check'
    $iDocker = [array]::IndexOf(@($script:ExtCalls | ForEach-Object { $_.StdinText -match 'apt-get install -y --only-upgrade' }), $true)
    $iTk = Get-CallIndex '-u root --exec sh -s -- 1\.20\.1-1$'
    $iTree = [array]::IndexOf(@($script:ExtCalls | ForEach-Object { $_.StdinText -match '/opt/cognita/trees' }), $true)
    Assert-True (($iDocker -ge 0) -and ($iDocker -lt $iTk) -and ($iTk -lt $iTree)) ("order: docker {0}, toolkit {1}, tree {2}" -f $iDocker, $iTk, $iTree)
    Assert-Equal 'nvidia' $r.Values['acceleration'] 'result'
    Assert-Match (Format-ResultLine 'ok' $r.Values) ';acceleration=nvidia;proof=' 'on the result line'
    Assert-Equal 0 @((Get-ProgressObjects) | Where-Object { $_.stage -eq 'nvidia' }).Count 'no nvidia progress when nothing was installed'
}

Test-Case 'update (22.4): a toolkit that must be installed shows its progress; its failure is a warning and the update goes on' {
    $s = New-TestSettings -RootPaths @('C:\Docs')
    Set-SettingProp $s 'acceleration' 'nvidia'; Save-Settings $s
    Add-UpdateFakes
    Add-InstallToolkitFakes -CheckExit 1 -ToolkitInstallExit 3
    Set-UpdateStatusFake '{"running": true, "version": "14.1.0", "acceleration": "cpu"}'
    $script:Interactive = $true; $script:SecretAnswer = 'pw'
    $r = Invoke-UpdateVerb -Opts (Get-UpdateOpts)
    Assert-Equal 'ok' $r.Status 'the update is still a success'
    Assert-Equal 'start,warning' ((@((Get-ProgressObjects) | Where-Object { $_.stage -eq 'nvidia' }) | ForEach-Object { $_.state }) -join ',') 'start, warning'
    Assert-Equal 1 (Get-ExtCallsMatching 'cognita update').Count 'the Linux update ran'
    Assert-Equal 'cpu' $r.Values['acceleration'] 'refreshed from status after the update'
    Assert-Equal 'cpu' (Read-Settings).acceleration 'and recorded'
}

Test-Case 'update (22.4): cpu, amd or an unknown recorded acceleration never runs the toolkit step' {
    foreach ($rec in @('cpu', 'amd', '')) {
        $script:ExtRules.Clear(); $script:ExtCalls.Clear()
        $s = New-TestSettings -RootPaths @('C:\Docs')
        Set-SettingProp $s 'acceleration' $rec; Save-Settings $s
        Add-UpdateFakes
        Add-InstallToolkitFakes -CheckExit 1
        $script:Interactive = $true; $script:SecretAnswer = 'pw'
        $r = Invoke-UpdateVerb -Opts (Get-UpdateOpts)
        Assert-Equal 'ok' $r.Status ("ok for [" + $rec + "]")
        Assert-Equal 0 (Get-ToolkitCalls).Count ("no toolkit call for [" + $rec + "]")
        Assert-Match (Get-LogText) 'update: no nvidia toolkit step' 'decision logged'
    }
}

Test-Case 'update (22.4, 22.12 item 1a): acceleration is refreshed from status; a status that does not say leaves a known value; the result says what status said' {
    $s = New-TestSettings -RootPaths @('C:\Docs')
    Set-SettingProp $s 'acceleration' 'cpu'; Save-Settings $s
    Add-UpdateFakes
    Set-UpdateStatusFake '{"running": true, "version": "14.1.0", "acceleration": "nvidia"}'
    $script:Interactive = $true; $script:SecretAnswer = 'pw'
    $r = Invoke-UpdateVerb -Opts (Get-UpdateOpts)
    Assert-Equal 'nvidia' (Read-Settings).acceleration 'moved to what status said'
    Assert-Equal 'nvidia' $r.Values['acceleration'] 'result'
    $script:ExtRules.Clear(); $script:ExtCalls.Clear()
    Add-UpdateFakes
    Add-InstallToolkitFakes -CheckExit 0
    Set-UpdateStatusFake '{"running": true, "version": "14.1.0"}'
    $r2 = Invoke-UpdateVerb -Opts (Get-UpdateOpts)
    Assert-Equal 'ok' $r2.Status 'ok'
    Assert-Equal 'nvidia' (Read-Settings).acceleration 'a silent status never overwrites with empty'
    Assert-Equal '' $r2.Values['acceleration'] 'result is empty'
    Assert-Match (Format-ResultLine 'ok' $r2.Values) ';acceleration=;proof=' 'key present'
}

Test-Case 'update (22.9): --wsl-memory-reclaim writes the line before the distro is started; a failure is a warning' {
    $s = New-TestSettings -RootPaths @('C:\Docs')
    Add-UpdateFakes
    $script:Interactive = $true; $script:SecretAnswer = 'pw'
    $opts = Get-UpdateOpts
    $opts['wsl-memory-reclaim'] = $true
    $r = Invoke-UpdateVerb -Opts $opts
    Assert-Equal 'ok' $r.Status ("ok: " + ($script:Out | Select-Object -Last 3))
    Assert-Equal "[experimental]`nautoMemoryReclaim=dropCache`n" ([System.IO.File]::ReadAllText($script:WslConfigPath)) 'created'
    $log = @($script:LogLines)
    $iAdd = -1; $iExec = -1
    for ($i = 0; $i -lt $log.Count; $i++) { if ($iAdd -lt 0 -and $log[$i] -match 'wslconfig: added autoMemoryReclaim=dropCache') { $iAdd = $i }; if ($iExec -lt 0 -and $log[$i] -match 'exec\(fake\)') { $iExec = $i } }
    Assert-True (($iAdd -ge 0) -and ($iAdd -lt $iExec)) ("added at log line {0}, first process at {1}" -f $iAdd, $iExec)
    # A failure is a warning, the update continues.
    $script:ExtRules.Clear(); $script:ExtCalls.Clear(); $script:Out.Clear()
    Remove-Item -LiteralPath $script:WslConfigPath -Force
    [System.IO.File]::WriteAllText($script:WslConfigPath, "[wsl2]`nmemory=12GB`n")
    [void](New-Item -ItemType Directory -Path ($script:WslConfigPath + '.cognita-backup') -Force)
    Add-UpdateFakes
    $r2 = Invoke-UpdateVerb -Opts $opts
    Assert-Equal 'ok' $r2.Status 'the update still succeeds'
    Assert-Equal 1 @((Get-ProgressObjects) | Where-Object { $_.stage -eq 'wslconfig' -and $_.state -eq 'warning' }).Count 'one warning'
}

Test-Case 'every shell script the helper sends into WSL passes "sh -n" (parse check, nothing is executed)' {
    # Scripts travel as stdin to "sh -s"; sh -n only parses them and runs nothing. Needs a POSIX sh on
    # this PC (Git for Windows ships one); without it there is nothing to check with.
    $sh = $null
    foreach ($c in @('C:\Program Files\Git\usr\bin\sh.exe', 'C:\Program Files\Git\bin\sh.exe')) { if (Test-Path -LiteralPath $c) { $sh = $c; break } }
    if (-not $sh) { Write-Host '      (skipped: no sh.exe available)'; return }
    # Drive the flows that build scripts, with fakes, and collect every stdin script.
    $s = New-TestSettings -RootPaths @('C:\Docs')
    Add-UpdateFakes
    # No root line in the fake fstab: the direct calls below and the update's own rewrite both write one.
    $script:UpdFstab = "/dev/sda / ext4 defaults 0 1"
    Add-ExtRule 'wsl\.exe --import' (New-ExtResult)
    [void](Write-FstabViaRoot -Settings $s -MountPoint '/mnt/cognita-roots/1' -Line (Get-FstabLineForRoot -WindowsPath 'C:\Docs' -N 1))
    [void](Write-FstabViaRoot -Settings $s -MountPoint '/mnt/cognita-roots/2' -Line (Get-FstabLineForRoot -WindowsPath 'D:\My Docs' -N 2))
    [void](Test-RootAccess -Settings $s -N 1)
    $img = New-FakeImage
    $vhd = Join-Path $script:TestDir 'vhd'
    [void](Invoke-ImportImage -Settings (New-Settings -VhdDir $vhd) -ImagePath $img.Path -ImageSha256 $img.Sha -VhdDir $vhd)
    $script:Interactive = $true; $script:SecretAnswer = 'pw'
    [void](Invoke-UpdateVerb -Opts (Get-UpdateOpts))
    $scripts = @($script:ExtCalls | Where-Object { $_.StdinText -and $_.Line -match 'sh -s' } | ForEach-Object { $_.StdinText })
    Assert-True ($scripts.Count -ge 5) ("collected {0} scripts" -f $scripts.Count)
    Use-RealExternal; Use-RealClock
    $n = 0
    foreach ($t in $scripts) {
        $n++
        $f = Join-Path $script:TestDir ('script{0}.sh' -f $n)
        [System.IO.File]::WriteAllText($f, $t, (New-Object System.Text.UTF8Encoding($false)))
        $r = Invoke-External -FilePath $sh -Arguments @('-n', ($f -replace '\\', '/')) -TimeoutSec 60
        Assert-Equal 0 $r.ExitCode ("script {0} does not parse: {1} (starts: {2})" -f $n, $r.Stderr, (($t -split "`n" | Select-Object -First 3) -join ' | '))
    }
}

Complete-Tests
