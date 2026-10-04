# test_cli.ps1 - the cognita command: status, forwarding, start/stop/restart (design 8, 5.5, 14.2)
. (Join-Path $PSScriptRoot '..\CognitaWin.ps1') -NoMain
. (Join-Path $PSScriptRoot '_harness.ps1')

$script:CliStatusJson = '{"installed": true, "running": true, "version": "14.1.0", "admin_url": "http://127.0.0.1:8676", "mcp_url": "http://127.0.0.1:8675", "public_url": "https://cog.example.ts.net", "workspace": "on", "acceleration": "cpu"}'

function Invoke-Cli {
    param([string[]]$CliArgs)
    return (Invoke-HelperMain -Argv (@('cli') + $CliArgs))
}
function Get-Text { return (($script:Out | ForEach-Object { $_ }) -join "`n") }
function Initialize-Installed {
    param([string[]]$Roots = @('C:\Users\me\Docs', 'D:\Work'))
    $vhd = Join-Path $script:TestDir 'vhd'
    $s = New-TestSettings -State 'installed' -Vhd $vhd -RootPaths $Roots
    $script:LxssDistros = @([pscustomobject]@{ Guid = '{g}'; Name = 'Cognita'; BasePath = $vhd })
    return $s
}
function Set-Running { param([bool]$Running) $script:distroRunning = $Running }
function Add-CliFakes {
    $script:distroRunning = $true
    $script:up = @{ '/mnt/cognita-roots/1' = $true; '/mnt/cognita-roots/2' = $true }
    $script:driveMissing = @{}
    $script:FstabRoots = @{}
    $script:Terminates = 0
    $script:healthyAfter = 1
    $script:healthPolls = 0
    $script:StatusNow = $script:CliStatusJson
    Add-ExtRule 'wsl\.exe --list --running --quiet' { param($c) if ($script:distroRunning) { New-ExtResult -Stdout "Cognita`r`n" } else { New-ExtResult -Stdout '' } }
    # `cognita stop` writes the stopped flag BEFORE it terminates, and Restart-CognitaDistro removes it
    # before it does (design 18.1 rule 2): the flag tells the two terminates apart. A restart brings the
    # distro back and applies fstab, so every root whose drive is present is mounted afterwards (a
    # mount is never made any other way: there is NO rule for `--exec mount`).
    Add-ExtRule 'wsl\.exe --terminate' {
        param($c)
        $script:Terminates++
        if (Test-Path -LiteralPath (Get-StoppedFlagPath)) { $script:distroRunning = $false }
        else {
            $script:distroRunning = $true
            foreach ($k in (@($script:up.Keys) + @($script:FstabRoots.Keys))) { $script:up[$k] = (-not $script:driveMissing[$k]) }
        }
        New-ExtResult
    }
    Add-ExtRule 'nsenter -t 1 -m -- mountpoint -q (/mnt/cognita-roots/\d)' { param($c) if ($script:up[$c.Arguments[-1]]) { New-ExtResult } else { New-ExtResult -ExitCode 1 } }
    Add-ReadyFakes
    Add-ExtRule 'cognita status --json' { param($c) $script:healthPolls++; if ($script:healthPolls -ge $script:healthyAfter) { New-ExtResult -Stdout $script:StatusNow } else { New-ExtResult -Stdout ($script:StatusNow -replace '"running": true', '"running": false') } }
    Add-ExtRule '/usr/local/bin/cognita (restart|stop)' (New-ExtResult)
}

# ---- status never starts a stopped distro -------------------------------------------------------
Test-Case 'status: not installed' {
    Add-CliFakes
    $code = Invoke-Cli @('status')
    Assert-Equal 0 $code 'exit'
    Assert-Match (Get-Text) 'Cognita is not installed on this PC\. Run Cognita Setup\.' 'message'
    Assert-Equal 0 (Get-ExtCallLines).Count 'no wsl call'
}

Test-Case 'status: a STOPPED distro is reported as stopped and is never started (W13)' {
    [void](Initialize-Installed)
    Add-CliFakes; Set-Running $false
    $code = Invoke-Cli @('status')
    Assert-Equal 0 $code 'exit'
    Assert-Match (Get-Text) 'Cognita is stopped\. Start it with: cognita start' 'message'
    $calls = Get-ExtCallLines
    Assert-Equal 1 $calls.Count 'exactly one wsl call'
    Assert-Match $calls[0] 'wsl\.exe --list --running --quiet' 'only asked which distros run'
    Assert-Equal 0 (Get-ExtCallsMatching '--exec|-d Cognita').Count 'nothing was executed inside the distro'
    Assert-Equal 0 $script:TaskStarted 'and the sign-in task was not run'
}

Test-Case 'status: a running distro prints version, URLs, Workspace, each folder with its Windows path, and the task state' {
    [void](Initialize-Installed)
    Add-CliFakes
    $script:up['/mnt/cognita-roots/2'] = $false
    $code = Invoke-Cli @('status')
    Assert-Equal 0 $code 'exit'
    $t = Get-Text
    Assert-Match $t 'Cognita 14\.1\.0 is running\.' 'running'
    Assert-Match $t 'Admin:\s+http://127\.0\.0\.1:8676' 'admin url'
    Assert-Match $t 'MCP:\s+http://127\.0\.0\.1:8675' 'mcp url'
    Assert-Match $t 'Public URL:\s+https://cog\.example\.ts\.net' 'public url'
    Assert-Match $t 'Workspace:\s+on' 'workspace'
    Assert-Match $t 'Projects folder: C:\\Users\\me\\Docs \(connected\)' 'root 1'
    Assert-Match $t 'Your projects folder D:\\Work is not available to Cognita\. Reconnect the drive or restore the folder, then run: cognita restart\.' 'root 2 is down'
    Assert-Match $t 'Starts when you sign in to Windows: yes' 'task'
    Assert-Equal 0 (Get-ExtCallsMatching '--terminate|-u root --exec mount').Count 'status changes nothing'
    Assert-False ($t -match '(?m)^\{') 'text for a person, not JSON'
}

Test-Case 'status (design 21.1): the skipped self-tests line is printed only when the Linux side says skipped' {
    [void](Initialize-Installed)
    Add-CliFakes
    [void](Invoke-Cli @('status'))
    Assert-False ((Get-Text) -match 'Self-tests:') 'nothing when the Linux side reports no proof value'
    $saved = $script:CliStatusJson
    $script:CliStatusJson = $saved.TrimEnd('}') + ', "proof": "skipped"}'
    Add-CliFakes
    $script:Out.Clear()
    [void](Invoke-Cli @('status'))
    Assert-Match (Get-Text) 'Self-tests:\s+skipped at install \(run Cognita Setup again to run them\)' 'skipped line'
    $script:CliStatusJson = $saved.TrimEnd('}') + ', "proof": "passed"}'
    Add-CliFakes
    $script:Out.Clear()
    [void](Invoke-Cli @('status'))
    Assert-False ((Get-Text) -match 'Self-tests:') 'nothing after a pass'
    $script:CliStatusJson = $saved
}

Test-Case 'status: the sign-in task missing is said out loud' {
    [void](Initialize-Installed)
    Add-CliFakes
    $script:TaskInfo = [pscustomobject]@{ Exists = $false; State = ''; LastResult = 0; LastRun = '' }
    [void](Invoke-Cli @('status'))
    Assert-Match (Get-Text) 'Starts when you sign in to Windows: NO' 'warning'
}

Test-Case 'status as a helper verb (not cli): progress lines and a result line, same behavior' {
    [void](Initialize-Installed)
    Add-CliFakes; Set-Running $false
    $code = Invoke-HelperMain -Argv @('status')
    Assert-Equal 0 $code 'exit'
    Assert-Match ((Get-OutLines)[-1]) '^result=ok;installed=1;running=0$' 'result line'
    Assert-Equal 1 (Get-ExtCallLines).Count 'never started'
}

# ---- forwarded verbs ----------------------------------------------------------------------------------
Test-Case 'forward: a running distro gets "wsl -d <distro> -u <user> --exec cognita <args>" with the console inherited' {
    [void](Initialize-Installed)
    Add-CliFakes
    $script:PassthroughExit = 0
    $code = Invoke-Cli @('logs', 'app', '-f')
    Assert-Equal 0 $code 'exit'
    Assert-Equal 1 $script:PassthroughCalls.Count 'one passthrough'
    Assert-Equal 'wsl.exe -d Cognita -u cognita --exec /usr/local/bin/cognita logs app -f' $script:PassthroughCalls[0] 'command'
}

Test-Case 'forward: the Linux exit code is the command''s exit code' {
    [void](Initialize-Installed)
    Add-CliFakes
    $script:PassthroughExit = 3
    Assert-Equal 3 (Invoke-Cli @('reset', 'index')) 'exit 3 passed through'
}

Test-Case 'forward: a STOPPED distro is never started by a forwarded verb; same message as status' {
    [void](Initialize-Installed)
    Add-CliFakes; Set-Running $false
    foreach ($verb in @(@('logs', 'app'), @('password'), @('reset', 'all'), @('rollback'))) {
        $script:Out.Clear()
        $code = Invoke-Cli $verb
        Assert-Equal 1 $code ("exit for " + $verb[0])
        Assert-Match (Get-Text) 'Cognita is stopped\. Start it with: cognita start' 'message'
    }
    Assert-Equal 0 $script:PassthroughCalls.Count 'nothing was forwarded'
    Assert-Equal 0 (Get-ExtCallsMatching '--exec').Count 'nothing was executed inside the distro'
}

Test-Case 'forward: install with --mcp-port or --admin-port is refused before anything runs' {
    [void](Initialize-Installed)
    Add-CliFakes
    foreach ($a in @(@('install', '--mcp-port', '9000'), @('install', '--admin-port=9001'), @('install', '--workspace', 'off', '--mcp-port', '1'))) {
        $script:Out.Clear()
        $code = Invoke-Cli $a
        Assert-Equal 1 $code 'exit 1'
        Assert-Match (Get-Text) 'Change ports by running Setup again and choosing Advanced\.' 'message'
    }
    Assert-Equal 0 $script:PassthroughCalls.Count 'not forwarded'
    Assert-Equal 0 (Get-ExtCallLines).Count 'no wsl call at all'
}

Test-Case 'forward: install --workspace is forwarded after every root is checked; ports are refreshed from status --json afterwards' {
    [void](Initialize-Installed)
    Add-CliFakes
    $script:StatusNow = $script:CliStatusJson -replace '8675', '9000'
    $code = Invoke-Cli @('install', '--workspace', 'off')
    Assert-Equal 0 $code 'exit'
    Assert-Equal 1 $script:PassthroughCalls.Count 'forwarded'
    Assert-Equal 9000 (Read-Settings).mcp_port 'settings follow the Linux side'
    Assert-Match (Get-Text) 'The MCP port is now 9000 \(it was 8675\)\.' 'said out loud'
}

Test-Case 'forward: install with a root that is down stops before the Linux CLI runs' {
    [void](Initialize-Installed)
    Add-CliFakes
    $script:up['/mnt/cognita-roots/2'] = $false
    $code = Invoke-Cli @('install', '--workspace', 'on')
    Assert-Equal 1 $code 'exit'
    Assert-Match (Get-Text) 'D:\\Work is not available to Cognita' 'names the folder'
    Assert-Equal 0 $script:PassthroughCalls.Count 'not forwarded'
}

Test-Case 'status (22.4): the Acceleration line follows Workspace and says NVIDIA GPU, AMD GPU or CPU; no line when status does not say' {
    [void](Initialize-Installed)
    Add-CliFakes
    foreach ($pair in @(@('nvidia', 'NVIDIA GPU'), @('amd', 'AMD GPU'), @('cpu', 'CPU'), @('something-new', 'CPU'))) {
        $script:Out.Clear()
        $script:StatusNow = $script:CliStatusJson -replace '"acceleration": "cpu"', ('"acceleration": "' + $pair[0] + '"')
        $code = Invoke-Cli @('status')
        Assert-Equal 0 $code 'exit'
        $t = Get-Text
        Assert-Match $t ('(?m)^  Workspace:\s+on\n  Acceleration: ' + [regex]::Escape($pair[1]) + '$') ("the line after Workspace for " + $pair[0])
    }
    $script:Out.Clear()
    $script:StatusNow = $script:CliStatusJson -replace ', "acceleration": "cpu"', ''
    [void](Invoke-Cli @('status'))
    Assert-NotMatch (Get-Text) 'Acceleration' 'no line when status did not say'
}

Test-Case 'forward (22.4): a forwarded install that exits 0 refreshes settings'' acceleration (with ports and workspace); a failure or a status that does not say changes nothing' {
    $s = Initialize-Installed
    Set-SettingProp $s 'acceleration' 'cpu'; Save-Settings $s
    Add-CliFakes
    $script:StatusNow = $script:CliStatusJson -replace '"acceleration": "cpu"', '"acceleration": "nvidia"'
    $code = Invoke-Cli @('install', '--acceleration', 'nvidia')
    Assert-Equal 0 $code 'exit'
    Assert-Equal 'nvidia' (Read-Settings).acceleration 'refreshed'
    Assert-Match (Get-LogText) 'acceleration \(forward install\): status said \[nvidia\], settings had \[cpu\], settings now \[nvidia\]' 'logged with values'
    $script:PassthroughExit = 1
    $script:StatusNow = $script:CliStatusJson
    [void](Invoke-Cli @('install', '--acceleration', 'cpu'))
    Assert-Equal 'nvidia' (Read-Settings).acceleration 'a failed install changes nothing'
    $script:PassthroughExit = 0
    $script:StatusNow = $script:CliStatusJson -replace ', "acceleration": "cpu"', ''
    [void](Invoke-Cli @('install', '--workspace', 'on'))
    Assert-Equal 'nvidia' (Read-Settings).acceleration 'a status that does not say never overwrites a known value'
}

Test-Case 'forward (22.12 item 16): a forwarded rollback that exits 0 refreshes acceleration (only that); a failed rollback does not' {
    $s = Initialize-Installed
    Set-SettingProp $s 'acceleration' 'nvidia'; Save-Settings $s
    Add-CliFakes
    $script:StatusNow = ($script:CliStatusJson -replace '"acceleration": "cpu"', '"acceleration": "cpu"') -replace '8675', '9000'
    $script:PassthroughExit = 1
    [void](Invoke-Cli @('rollback'))
    Assert-Equal 'nvidia' (Read-Settings).acceleration 'a failed rollback changes nothing'
    Assert-Equal 0 (Get-ExtCallsMatching 'cognita status --json').Count 'and does not even read status'
    $script:PassthroughExit = 0
    $code = Invoke-Cli @('rollback')
    Assert-Equal 0 $code 'exit'
    Assert-Equal 'cpu' (Read-Settings).acceleration 'refreshed from status'
    Assert-Equal 8675 (Read-Settings).mcp_port 'the ports are not refreshed by a rollback'
    Assert-Match (Get-LogText) 'acceleration \(forward rollback\): status said \[cpu\], settings had \[nvidia\], settings now \[cpu\]' 'logged with values'
}

Test-Case 'cli: help, update and uninstall messages' {
    Add-CliFakes
    Assert-Equal 0 (Invoke-Cli @()) 'no args: help'
    Assert-Match (Get-Text) 'cognita status' 'help lists status'
    $script:Out.Clear()
    [void](Invoke-Cli @('update'))
    Assert-Match (Get-Text) 'Download the new Cognita-Setup\.exe from https://github\.com/.+ and run it\.' 'update'
    $script:Out.Clear()
    [void](Invoke-Cli @('uninstall'))
    Assert-Match (Get-Text) 'Uninstall Cognita from Settings > Apps > Installed apps\.' 'uninstall'
    Assert-Equal 0 (Get-ExtCallLines).Count 'no wsl call'
}

Test-Case 'cli: every token after the command is forwarded untouched (-n, -f, -v are the Linux command''s flags)' {
    [void](Initialize-Installed)
    Add-CliFakes
    [void](Invoke-Cli @('logs', '-n', '50', '-f', '-v', '-NoMain'))
    Assert-Equal 'wsl.exe -d Cognita -u cognita --exec /usr/local/bin/cognita logs -n 50 -f -v -NoMain' $script:PassthroughCalls[0] 'every token forwarded'
}

# ---- start ---------------------------------------------------------------------------------------------
Test-Case 'start (design 18.1): an unmounted root restarts the DISTRO once (fstab is applied at its start), never runs mount, and does NOT also run cognita restart (a fresh start already starts Cognita)' {
    [void](Initialize-Installed)
    Add-CliFakes
    Set-Running $true
    $script:up['/mnt/cognita-roots/2'] = $false
    $script:healthyAfter = 3
    [System.IO.File]::WriteAllText((Get-StoppedFlagPath), 'x')
    $code = Invoke-Cli @('start')
    Assert-Equal 0 $code ("exit: " + (Get-Text))
    Assert-False (Test-Path (Get-StoppedFlagPath)) 'stopped flag removed'
    Assert-Equal 2 $script:TaskStarted 'the task was run by start and again by the distro restart'
    Assert-Equal 1 $script:Terminates 'the distro was restarted exactly once'
    Assert-Equal 0 (Get-ExtCallsMatching '--exec mount').Count 'the helper never mounts'
    Assert-Equal 0 (Get-ExtCallsMatching 'cognita restart').Count 'no cognita restart on top of a fresh start'
    Assert-True ($script:up['/mnt/cognita-roots/2']) 'the root is mounted afterwards'
    Assert-True ($script:healthPolls -ge 3) 'kept asking for health until it answered'
    Assert-Match (Get-Text) 'Cognita is running\.' 'result printed'
    Assert-Match (Get-LogText) 'start: the distro was restarted to apply fstab \(0 root\(s\) still down\); a fresh start already starts Cognita, so cognita restart is not run' 'which happened is logged'
    Assert-Match (Get-LogText) 'remount: 1 root\(s\) unmounted \(2\)' 'and why'
}

Test-Case 'start: nothing was down means no distro restart and no cognita restart' {
    [void](Initialize-Installed)
    Add-CliFakes
    $code = Invoke-Cli @('start')
    Assert-Equal 0 $code 'exit'
    Assert-Equal 0 $script:Terminates 'no restart of the distro'
    Assert-Equal 0 (Get-ExtCallsMatching '--exec mount').Count 'no mount'
    Assert-Equal 0 (Get-ExtCallsMatching 'cognita restart').Count 'no restart'
    Assert-Match (Get-LogText) 'start: no root was unmounted, so the distro was not restarted' 'logged'
}

Test-Case 'start: roots are judged only after systemd has finished booting; a distro that never gets ready fails with a message' {
    [void](Initialize-Installed)
    Add-CliFakes
    $script:ExtRules.Insert(0, @{ Pattern = 'systemctl is-system-running'; Response = (New-ExtResult -ExitCode 1 -Stdout "starting`n") })
    $t0 = Get-ClockNow
    Assert-Equal 1 (Invoke-Cli @('start')) 'exit 1'
    Assert-True ((((Get-ClockNow) - $t0).TotalSeconds) -ge 300) 'a full 300 s waited'
    Assert-Match (Get-Text) 'did not finish starting within 5 minutes' 'message'
    Assert-Equal 0 (Get-ExtCallsMatching 'nsenter').Count 'no root was judged before the distro was ready'
}

Test-Case 'start: a folder that will not come back is reported by name, and start still finishes' {
    [void](Initialize-Installed)
    Add-CliFakes
    $script:up['/mnt/cognita-roots/2'] = $false
    $script:driveMissing['/mnt/cognita-roots/2'] = $true
    $code = Invoke-Cli @('start')
    Assert-Equal 0 $code 'exit'
    Assert-Equal 1 $script:Terminates 'restarted once to apply fstab, then it is reported, not retried'
    Assert-Match (Get-Text) 'Your projects folder D:\\Work is not available to Cognita\.' 'named'
    Assert-Match (Get-LogText) 'start: the distro was restarted to apply fstab \(1 root\(s\) still down\)' 'logged with the count'
}

Test-Case 'start: health never answering fails after 300 s of clock time; the distro never starting fails after 120 s' {
    [void](Initialize-Installed)
    Add-CliFakes
    $script:healthyAfter = 100000
    $t0 = Get-ClockNow
    Assert-Equal 1 (Invoke-Cli @('start')) 'exit 1'
    Assert-True ((((Get-ClockNow) - $t0).TotalSeconds) -ge 300) 'a full 300 s waited'
    Assert-Match (Get-Text) 'Cognita did not answer within 5 minutes\.' 'message'
    $script:Out.Clear(); $script:ExtCalls.Clear()
    Set-Running $false
    $script:ExtRules.Insert(0, @{ Pattern = 'wsl\.exe --list --running --quiet'; Response = (New-ExtResult -Stdout '') })
    $t1 = Get-ClockNow
    Assert-Equal 1 (Invoke-Cli @('start')) 'exit 1'
    Assert-True ((((Get-ClockNow) - $t1).TotalSeconds) -ge 120) 'a full 120 s waited'
    Assert-Match (Get-Text) 'did not start within 2 minutes' 'message'
}

# ---- stop ------------------------------------------------------------------------------------------------
Test-Case 'stop: the stopped flag exists BEFORE cognita stop, then the distro is terminated' {
    [void](Initialize-Installed)
    Add-CliFakes
    $script:flagAtStop = $null
    $script:ExtRules.Insert(0, @{ Pattern = 'cognita stop'; Response = { param($c) $script:flagAtStop = (Test-Path (Get-StoppedFlagPath)); New-ExtResult } })
    $code = Invoke-Cli @('stop')
    Assert-Equal 0 $code 'exit'
    Assert-True $script:flagAtStop 'the keepalive is told not to relaunch before anything stops'
    $calls = Get-ExtCallLines
    $iStop = [array]::IndexOf(@($calls | ForEach-Object { $_ -match 'cognita stop' }), $true)
    $iTerm = [array]::IndexOf(@($calls | ForEach-Object { $_ -match '--terminate Cognita' }), $true)
    Assert-True (($iStop -ge 0) -and ($iTerm -gt $iStop)) 'cognita stop, then wsl --terminate'
    Assert-Match (Get-Text) 'will not start again until you run: cognita start' 'says how to start again'
}

Test-Case 'stop: an already stopped distro is not started just to stop it' {
    [void](Initialize-Installed)
    Add-CliFakes; Set-Running $false
    $code = Invoke-Cli @('stop')
    Assert-Equal 0 $code 'exit'
    Assert-True (Test-Path (Get-StoppedFlagPath)) 'flag still created'
    Assert-Equal 0 (Get-ExtCallsMatching '--exec|--terminate').Count 'nothing run inside, nothing terminated'
}

# ---- restart ----------------------------------------------------------------------------------------------
Test-Case 'restart (design 18.1): an unmounted root restarts the DISTRO (fstab applied at its start) and cognita restart is NOT run; the log says which happened' {
    [void](Initialize-Installed)
    Add-CliFakes
    $script:up['/mnt/cognita-roots/1'] = $false
    $script:healthyAfter = 2
    $code = Invoke-Cli @('restart')
    Assert-Equal 0 $code ("exit: " + (Get-Text))
    Assert-Equal 1 $script:Terminates 'one distro restart'
    Assert-Equal 0 (Get-ExtCallsMatching '--exec mount').Count 'the helper never mounts'
    Assert-Equal 0 (Get-ExtCallsMatching 'cognita restart').Count 'no cognita restart after a fresh start'
    Assert-True ($script:up['/mnt/cognita-roots/1']) 'root 1 is mounted afterwards'
    Assert-True ($script:healthPolls -ge 2) 'health awaited'
    Assert-Match (Get-LogText) 'restart: the distro was restarted to apply fstab \(0 root\(s\) still down\); cognita restart is not run because a fresh start already starts Cognita' 'decision logged'
    Assert-Match (Get-Text) 'Cognita restarted\.' 'said'
}

Test-Case 'restart: every root mounted means cognita restart runs and the distro is left alone' {
    [void](Initialize-Installed)
    Add-CliFakes
    $code = Invoke-Cli @('restart')
    Assert-Equal 0 $code 'exit'
    Assert-Equal 0 $script:Terminates 'no distro restart'
    Assert-Equal 1 (Get-ExtCallsMatching 'cognita restart').Count 'cognita restart ran'
    Assert-Match (Get-LogText) 'restart: no root was unmounted; running cognita restart' 'logged'
}

Test-Case 'restart: a folder that cannot come back is reported by name after the distro restart' {
    [void](Initialize-Installed)
    Add-CliFakes
    $script:up['/mnt/cognita-roots/2'] = $false
    $script:driveMissing['/mnt/cognita-roots/2'] = $true
    Assert-Equal 0 (Invoke-Cli @('restart')) 'exit'
    Assert-Match (Get-Text) 'Your projects folder D:\\Work is not available to Cognita\.' 'named'
    Assert-Equal 0 (Get-ExtCallsMatching 'cognita restart').Count 'still no cognita restart'
}

Test-Case 'restart: a stopped distro means start, not a restart into nothing' {
    [void](Initialize-Installed)
    Add-CliFakes
    Set-Running $false
    $script:listCalls = 0
    $script:ExtRules.Insert(0, @{ Pattern = 'wsl\.exe --list --running --quiet'; Response = { param($c) $script:listCalls++; if ($script:listCalls -ge 2) { New-ExtResult -Stdout "Cognita`r`n" } else { New-ExtResult -Stdout '' } } })
    $code = Invoke-Cli @('restart')
    Assert-Equal 0 $code ("exit: " + (Get-Text))
    Assert-Equal 1 $script:TaskStarted 'the sign-in task was run'
}

Test-Case 'restart: a Linux failure is reported' {
    [void](Initialize-Installed)
    Add-CliFakes
    $script:ExtRules.Insert(0, @{ Pattern = 'cognita restart'; Response = (New-ExtResult -ExitCode 1 -Stderr 'unit failed') })
    Assert-Equal 1 (Invoke-Cli @('restart')) 'exit 1'
    Assert-Match (Get-Text) 'cognita restart failed \(exit 1\)' 'message'
}

# ---- add-folder ---------------------------------------------------------------------------------------------
Test-Case 'add-folder: stopped distro is not started; a running one adds the folder as the next root' {
    $one = New-Dir 'one'
    $vhd = Join-Path $script:TestDir 'vhd'
    [void](New-TestSettings -State 'installed' -Vhd $vhd -RootPaths @($one))
    Add-CliFakes; Set-Running $false
    $two = New-Dir 'two'
    Assert-Equal 1 (Invoke-Cli @('add-folder', $two)) 'stopped: exit 1'
    Assert-Match (Get-Text) 'Cognita is stopped' 'message'
    Assert-Equal 0 (Get-ExtCallsMatching '--exec').Count 'nothing run inside'
    Set-Running $true; $script:Out.Clear()
    $script:up = @{ '/mnt/cognita-roots/1' = $true }
    Add-ExtRule 'cat /etc/fstab' (New-ExtResult -Stdout "/dev/sda / ext4 defaults 0 1`n")
    Add-ExtRule '-u root --exec sh -s' { param($c) foreach ($m in [regex]::Matches([string]$c.StdinText, '(/mnt/cognita-roots/\d) drvfs')) { $script:FstabRoots[$m.Groups[1].Value] = $true }; New-ExtResult }
    Add-ExtRule 'mkdir -p -m 0755' (New-ExtResult)
    Add-ExtRule '-u cognita --exec sh -s' (New-ExtResult)
    Add-ExtRule '/usr/local/bin/cognita add-folder' (New-ExtResult)
    Assert-Equal 0 (Invoke-Cli @('add-folder', $two)) ("added: " + (Get-Text))
    Assert-Match (Get-Text) 'Added .+two as projects folder 2\.' 'message'
    Assert-Equal 2 @(Get-SettingsRoots (Read-Settings)).Count 'two roots'
    Assert-Equal 1 $script:Terminates 'the new folder is connected by one distro restart (design 18.1), never by mount'
    Assert-Equal 0 (Get-ExtCallsMatching '--exec mount').Count 'no mount'
    $calls = Get-ExtCallLines
    $iTerm = [array]::IndexOf(@($calls | ForEach-Object { $_ -match '--terminate Cognita' }), $true)
    $iAdd = [array]::IndexOf(@($calls | ForEach-Object { $_ -match 'cognita add-folder' }), $true)
    Assert-True (($iTerm -ge 0) -and ($iAdd -gt $iTerm)) 'restart first, then cognita add-folder'
}

Complete-Tests
