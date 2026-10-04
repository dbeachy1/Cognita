# test_password.ps1 - the Admin password path and the real Invoke-External (design 5.6, 14.2)
#
# These tests run REAL child processes (a second powershell.exe) and REAL named pipes, because the
# thing under test is exactly what happens on those boundaries. They wait only on a process's exit or
# a pipe connection, never on the wall clock; the timeouts passed are hang guards.
. (Join-Path $PSScriptRoot '..\CognitaWin.ps1') -NoMain
. (Join-Path $PSScriptRoot '_harness.ps1')

$script:Pw = 'p' + (Get-Utf8 0xE4) + 'ssw' + (Get-Utf8 0xF6) + 'rd' + (Get-Utf8 0x20AC)     # the design's example password: p, a-umlaut, ssw, o-umlaut, rd, euro sign
$script:PsExe = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'

function New-ChildScript {
    param([string]$Name, [string]$Text)
    $p = Join-Path $script:TestDir $Name
    [System.IO.File]::WriteAllText($p, $Text, (New-Object System.Text.UTF8Encoding($false)))
    return $p
}
function ConvertTo-Hex { param([byte[]]$Bytes) return (($Bytes | ForEach-Object { $_.ToString('x2') }) -join '') }

Test-Case 'real child: a non-ASCII password reaches its stdin byte-exact as UTF-8, no BOM, then EOF' {
    Use-RealExternal; Use-RealClock
    $child = New-ChildScript 'dumpstdin.ps1' '$b = New-Object IO.MemoryStream; [Console]::OpenStandardInput().CopyTo($b); [Console]::Out.Write((($b.ToArray() | ForEach-Object { $_.ToString("x2") }) -join ""))'
    $r = Invoke-External -FilePath $script:PsExe -Arguments @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $child) -TimeoutSec 60 -StdinText ($script:Pw + "`n")
    Assert-Equal 0 $r.ExitCode 'child exit'
    $expected = ConvertTo-Hex ([System.Text.Encoding]::UTF8.GetBytes($script:Pw + "`n"))
    Assert-Equal $expected $r.Stdout.Trim() 'bytes on the child stdin'
    Assert-False ($r.Stdout.StartsWith('efbbbf')) 'no BOM'
    Assert-Match $r.Stdout.Trim() '^70c3a4737377c3b67264e282ac0a$' 'p, a-umlaut (c3a4), ssw, o-umlaut (c3b6), rd, euro (e282ac), LF'
}

Test-Case 'real child: the password is not in the logged command line, only its length' {
    Use-RealExternal; Use-RealClock
    $child = New-ChildScript 'noop.ps1' '# nothing'
    [void](Invoke-External -FilePath $script:PsExe -Arguments @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $child) -TimeoutSec 60 -StdinText ($script:Pw + "`n"))
    $log = Get-LogText
    Assert-NotMatch $log ([regex]::Escape($script:Pw)) 'password not in the log'
    Assert-Match $log 'stdin=10 chars \(not logged\)' 'length only'
}

Test-Case 'real child: stdout and stderr decode as UTF-8, exit code returned, WSL_UTF8=1 is set' {
    Use-RealExternal; Use-RealClock
    # The child script is ASCII-only (PowerShell 5.1 would read a BOM-less UTF-8 file as ANSI); it builds
    # the non-ASCII text from code points.
    $child = New-ChildScript 'out.ps1' ('$o=[Console]::OpenStandardOutput(); $b=[Text.Encoding]::UTF8.GetBytes("caf" + [char]0xE9 + " " + [char]0x20AC + "|" + $env:WSL_UTF8); $o.Write($b,0,$b.Length); $o.Flush(); $e=[Console]::OpenStandardError(); $x=[Text.Encoding]::UTF8.GetBytes("err" + [char]0xF6); $e.Write($x,0,$x.Length); $e.Flush(); exit 7')
    $r = Invoke-External -FilePath $script:PsExe -Arguments @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $child) -TimeoutSec 60
    Assert-Equal 7 $r.ExitCode 'exit code'
    Assert-Equal ('caf' + (Get-Utf8 0xE9) + ' ' + (Get-Utf8 0x20AC) + '|1') $r.Stdout 'stdout with WSL_UTF8=1'
    Assert-Equal ('err' + (Get-Utf8 0xF6)) $r.Stderr 'stderr'
    Assert-Match (Get-LogText) 'exec done: exit=7 ms=\d+ timedout=False' 'exit code and duration logged'
}

Test-Case 'real child: arguments arrive exactly as given (spaces, quotes, trailing backslash)' {
    Use-RealExternal; Use-RealClock
    $child = New-ChildScript 'echoargs.ps1' 'foreach ($a in $args) { [Console]::Out.WriteLine("<" + $a + ">") }'
    $args2 = @('a b', 'c"d', 'e\', 'f g\', 'h;i', 'C:\Users\me\My Docs')
    $r = Invoke-External -FilePath $script:PsExe -Arguments (@('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $child) + $args2) -TimeoutSec 60
    $got = @($r.Stdout -split "`r?`n" | Where-Object { $_ -ne '' })
    Assert-Equal (($args2 | ForEach-Object { '<' + $_ + '>' }) -join '|') ($got -join '|') 'each argument survives'
}

Test-Case 'real child: OnOutputLine gets each stdout line, OnErrorLine each stderr line' {
    Use-RealExternal; Use-RealClock
    $child = New-ChildScript 'lines.ps1' '[Console]::Out.WriteLine("one"); [Console]::Out.WriteLine("two"); [Console]::Error.WriteLine("link https://login.example/a/xyz"); [Console]::Out.Write("three-no-newline")'
    $script:outLines = New-Object System.Collections.ArrayList
    $script:errLines = New-Object System.Collections.ArrayList
    $r = Invoke-External -FilePath $script:PsExe -Arguments @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $child) -TimeoutSec 60 -OnOutputLine { param($l) [void]$script:outLines.Add($l) } -OnErrorLine { param($l) [void]$script:errLines.Add($l) }
    Assert-Equal 'one|two|three-no-newline' ($script:outLines -join '|') 'stdout lines, the last one without a newline too'
    Assert-Equal 'link https://login.example/a/xyz' ($script:errLines -join '|') 'stderr line'
}

Test-Case 'real Invoke-External: a program that does not exist is StartError, exit -1, not an exception' {
    Use-RealExternal; Use-RealClock
    $r = Invoke-External -FilePath (Join-Path $script:TestDir 'nope.exe') -Arguments @('x') -TimeoutSec 5
    Assert-Equal -1 $r.ExitCode 'exit -1'
    Assert-True ($r.StartError.Length -gt 0) 'start error text'
    Assert-Match (Get-LogText) 'exec start FAILED' 'logged'
}

Test-Case 'real Invoke-External: a hung child is killed at the timeout on the FAKE clock (no real waiting)' {
    Use-RealExternal
    Use-FakeClock
    $child = New-ChildScript 'hang.ps1' 'Start-Sleep -Seconds 120'
    $t0 = Get-ClockNow
    $r = Invoke-External -FilePath $script:PsExe -Arguments @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $child) -TimeoutSec 30
    Assert-True $r.TimedOut 'timed out'
    Assert-Equal 124 $r.ExitCode 'exit 124'
    Assert-True ((((Get-ClockNow) - $t0).TotalSeconds) -ge 30) 'a full 30 s of fake clock time'
    Assert-Match (Get-LogText) 'exec TIMED OUT after 30s; killing pid' 'logged'
}

Test-Case 'real Invoke-External: OnPoll runs about once per second of clock time' {
    Use-RealExternal
    Use-FakeClock
    $child = New-ChildScript 'hang2.ps1' 'Start-Sleep -Seconds 120'
    $script:polls = 0
    [void](Invoke-External -FilePath $script:PsExe -Arguments @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $child) -TimeoutSec 10 -OnPoll { $script:polls++ })
    Assert-True (($script:polls -ge 9) -and ($script:polls -le 11)) ("polls in 10 s of clock: {0}" -f $script:polls)
}

Test-Case 'broker pipes: current-user-only ACL, one instance' {
    $name = 'cogtest-' + [guid]::NewGuid().ToString('N')
    $srv = New-BrokerPipe -Name $name -Direction 'In'
    try {
        $rules = @($srv.GetAccessControl().GetAccessRules($true, $true, [System.Security.Principal.SecurityIdentifier]))
        $me = [System.Security.Principal.WindowsIdentity]::GetCurrent().User
        Assert-Equal 1 $rules.Count 'exactly one access rule'
        Assert-Equal $me.Value $rules[0].IdentityReference.Value 'the current user'
        Assert-Equal 'Allow' $rules[0].AccessControlType.ToString() 'allow'
        Assert-Throws { [void](New-BrokerPipe -Name $name -Direction 'In') } '' 'a second instance of the same pipe is refused'
    } finally { $srv.Dispose() }
}

Test-Case 'broker steps: A (UTF-16LE in, no terminator) is served on B byte for byte' {
    # Single thread, no waiting: the fake clock's Sleep is what the broker calls while it waits for a
    # connection, so the "other side" (Setup on A, the helper on B) acts inside that Sleep.
    $a = 'cogtest-a-' + [guid]::NewGuid().ToString('N'); $b = 'cogtest-b-' + [guid]::NewGuid().ToString('N')
    $bytes = [System.Text.Encoding]::Unicode.GetBytes($script:Pw)
    $sa = New-BrokerPipe -Name $a -Direction 'In'
    $sb = $null; $script:cb = $null; $script:task = $null
    try {
        $script:didA = $false
        $script:Clock = [pscustomobject]@{
            Now   = { $script:FakeNow }
            Sleep = { param($Milliseconds)
                $script:FakeNow = $script:FakeNow.AddMilliseconds($Milliseconds)
                if (-not $script:didA) {
                    $script:didA = $true
                    # Setup's side of pipe A: connect, write the string's own UTF-16LE bytes, close.
                    $ca = New-Object System.IO.Pipes.NamedPipeClientStream('.', $a, [System.IO.Pipes.PipeDirection]::Out)
                    $ca.Connect(10000)
                    $ca.Write($bytes, 0, $bytes.Length); $ca.Flush(); $ca.Dispose()
                }
            }
        }
        $got = Receive-BrokerPassword -Server $sa -TimeoutSec 60
        Assert-Equal (ConvertTo-Hex $bytes) (ConvertTo-Hex $got) 'A: bytes received'
        Assert-Equal $script:Pw ([System.Text.Encoding]::Unicode.GetString($got)) 'A: decodes to the password'
        # The helper's side of pipe B: connect and start reading, again from inside the broker's wait.
        $sb = New-BrokerPipe -Name $b -Direction 'Out'
        $script:buf = New-Object 'byte[]' 4096
        $script:Clock = [pscustomobject]@{
            Now   = { $script:FakeNow }
            Sleep = { param($Milliseconds)
                $script:FakeNow = $script:FakeNow.AddMilliseconds($Milliseconds)
                if ($null -eq $script:cb) {
                    $script:cb = New-Object System.IO.Pipes.NamedPipeClientStream('.', $b, [System.IO.Pipes.PipeDirection]::In)
                    $script:cb.Connect(10000)
                    $script:task = $script:cb.ReadAsync($script:buf, 0, $script:buf.Length)
                }
            }
        }
        Send-BrokerPassword -Server $sb -Bytes $got -TimeoutSec 60
        Assert-True $script:task.Wait(30000) 'the client read finished'
        Assert-Equal (ConvertTo-Hex $bytes) (ConvertTo-Hex $script:buf[0..($script:task.Result - 1)]) 'B: bytes served'
    } finally { $sa.Dispose(); if ($sb) { $sb.Dispose() }; if ($script:cb) { $script:cb.Dispose() } }
    Assert-NotMatch (Get-LogText) ([regex]::Escape($script:Pw)) 'password not logged'
    Assert-Match (Get-LogText) 'broker: received \d+ bytes on pipe A \(content not logged\)' 'byte counts logged'
}

Test-Case 'broker race: a client that writes and closes BEFORE the broker starts waiting still delivers the password' {
    # Setup retries opening pipe A and writes the instant it exists, which can be before the broker's
    # BeginWaitForConnection. Measured before the fix: about one run in three lost the password.
    $a = 'cogtest-a-' + [guid]::NewGuid().ToString('N')
    $bytes = [System.Text.Encoding]::Unicode.GetBytes($script:Pw)
    $sa = New-BrokerPipe -Name $a -Direction 'In'
    try {
        $ca = New-Object System.IO.Pipes.NamedPipeClientStream('.', $a, [System.IO.Pipes.PipeDirection]::Out)
        $ca.Connect(10000)
        $ca.Write($bytes, 0, $bytes.Length); $ca.Flush(); $ca.Dispose()
        $got = Receive-BrokerPassword -Server $sa -TimeoutSec 60
        Assert-Equal (ConvertTo-Hex $bytes) (ConvertTo-Hex $got) 'bytes received'
    } finally { $sa.Dispose() }
    Assert-Match (Get-LogText) 'the client wrote and closed before the wait began' 'the race path was taken and logged'
    Assert-NotMatch (Get-LogText) ([regex]::Escape($script:Pw)) 'password not logged'
}

Test-Case 'END TO END x12: the real broker process never loses the password to that race' {
    # Design 19.10: no Use-RealClock here any more. The test's own reads wait on the pipe (Connect with a
    # hang-guard timeout, Read-PasswordFromPipe's blocking connect, WaitForExit on the broker process),
    # never on a poll loop over the wall clock. The fake clock only stamps log lines.
    $helper = (Resolve-Path (Join-Path $PSScriptRoot '..\CognitaWin.ps1')).Path
    for ($i = 0; $i -lt 12; $i++) {
        $a = 'cogtest-a-' + [guid]::NewGuid().ToString('N'); $b = 'cogtest-b-' + [guid]::NewGuid().ToString('N')
        $psi = New-Object System.Diagnostics.ProcessStartInfo
        $psi.FileName = $script:PsExe
        $psi.Arguments = ConvertTo-ArgString @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $helper, 'password-broker', '--in', $a, '--out', $b)
        $psi.UseShellExecute = $false; $psi.RedirectStandardOutput = $true; $psi.CreateNoWindow = $true
        $psi.EnvironmentVariables['COGNITA_HOME'] = $env:COGNITA_HOME
        $p = [System.Diagnostics.Process]::Start($psi)
        try {
            $ca = New-Object System.IO.Pipes.NamedPipeClientStream('.', $a, [System.IO.Pipes.PipeDirection]::Out)
            $ca.Connect(60000)
            $pw = $script:Pw + $i
            $bytes = [System.Text.Encoding]::Unicode.GetBytes($pw)
            $ca.Write($bytes, 0, $bytes.Length); $ca.Flush(); $ca.Dispose()
            Assert-Equal $pw (Read-PasswordFromPipe -Name $b -TimeoutSec 60) ("round {0}" -f $i)
            Assert-True $p.WaitForExit(60000) 'broker exits'
            Assert-Equal 0 $p.ExitCode 'broker exit code'
        } finally { if (-not $p.HasExited) { $p.Kill() }; $p.Dispose() }
    }
}

Test-Case 'broker: waits are bounded by the fake clock (nobody connects to A, nobody reads B, nobody serves B)' {
    $a = 'cogtest-a-' + [guid]::NewGuid().ToString('N')
    $sa = New-BrokerPipe -Name $a -Direction 'In'
    try {
        $t0 = Get-ClockNow
        Assert-Throws { Receive-BrokerPassword -Server $sa -TimeoutSec 600 } 'Nothing was written to the password pipe in time' 'A timeout'
        Assert-True ((((Get-ClockNow) - $t0).TotalSeconds) -ge 600) 'a full 600 s of fake clock time'
    } finally { $sa.Dispose() }
    $b = 'cogtest-b-' + [guid]::NewGuid().ToString('N')
    $sb = New-BrokerPipe -Name $b -Direction 'Out'
    try { Assert-Throws { Send-BrokerPassword -Server $sb -Bytes ([byte[]](1, 2)) -TimeoutSec 600 } 'Nobody read the password pipe in time' 'B timeout' } finally { $sb.Dispose() }
}

Test-Case 'password client (design 19.10): a connect that times out becomes "The password did not arrive", logged with the reason; the real connect connects without polling' {
    # Read-PasswordFromPipe waits with ONE blocking Connect whose timeout is a hang guard. Proving the
    # timeout path with the real Connect would mean really waiting, so the connect step is replaced here
    # by one that reports the timeout at once, exactly as Connect does when the guard expires.
    $realConnect = (Get-Command Connect-PasswordPipe -CommandType Function).ScriptBlock
    $script:ConnectAsked = $null
    try {
        function script:Connect-PasswordPipe { param([string]$Name, [int]$TimeoutSec) $script:ConnectAsked = "$Name|$TimeoutSec"; throw (New-Object System.TimeoutException) }
        Assert-Throws { [void](Read-PasswordFromPipe -Name 'cogtest-none-0123456789' -TimeoutSec 60) } 'The password did not arrive' 'client timeout'
    } finally { Set-Item -Path Function:\script:Connect-PasswordPipe -Value $realConnect }
    Assert-Equal 'cogtest-none-0123456789|60' $script:ConnectAsked 'the pipe name and the hang-guard seconds reach the connect step'
    Assert-Match (Get-LogText) 'password pipe: connect failed: TimeoutException' 'the failure and its reason are logged'
    # The restored REAL connect against a pipe that already exists returns a connected client at once
    # (no waiting: the server instance exists, which is all Connect needs).
    $name = 'cogtest-b-' + [guid]::NewGuid().ToString('N')
    $srv = New-BrokerPipe -Name $name -Direction 'Out'
    $c = $null
    try {
        $c = Connect-PasswordPipe -Name $name -TimeoutSec 60
        Assert-True $c.IsConnected 'the real connect returned a connected client'
    } finally { if ($c) { $c.Dispose() }; $srv.Dispose() }
}

Test-Case 'broker verb rejects unusable pipe names' {
    $r = Invoke-PasswordBrokerVerb -Opts @{ in = 'x'; out = 'y' }
    Assert-Equal 'failed' $r.Status 'failed'
}

Test-Case 'END TO END: Setup-side write to A, the real password-broker process, the helper reading B -> the same password' {
    # Design 19.10: waits on the pipe and the process only (see the x12 test above); no real clock.
    $a = 'cogtest-a-' + [guid]::NewGuid().ToString('N'); $b = 'cogtest-b-' + [guid]::NewGuid().ToString('N')
    $helper = Join-Path $PSScriptRoot '..\CognitaWin.ps1'
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = $script:PsExe
    $psi.Arguments = ConvertTo-ArgString @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', (Resolve-Path $helper).Path, 'password-broker', '--in', $a, '--out', $b)
    $psi.UseShellExecute = $false; $psi.RedirectStandardOutput = $true; $psi.CreateNoWindow = $true
    $psi.EnvironmentVariables['COGNITA_HOME'] = $env:COGNITA_HOME
    $p = [System.Diagnostics.Process]::Start($psi)
    try {
        $ca = New-Object System.IO.Pipes.NamedPipeClientStream('.', $a, [System.IO.Pipes.PipeDirection]::Out)
        $ca.Connect(60000)          # Setup retries the open while the broker starts
        $bytes = [System.Text.Encoding]::Unicode.GetBytes($script:Pw)
        $ca.Write($bytes, 0, $bytes.Length); $ca.Flush(); $ca.Dispose()
        $got = Read-PasswordFromPipe -Name $b -TimeoutSec 60
        Assert-Equal $script:Pw $got 'the helper reads exactly what Setup wrote'
        Assert-True $p.WaitForExit(60000) 'the broker exits after the one read'
        Assert-Equal 0 $p.ExitCode 'broker exit code'
        $out = $p.StandardOutput.ReadToEnd().Trim()
        Assert-Match $out 'result=ok$' 'broker result line'
        Assert-NotMatch $out ([regex]::Escape($script:Pw)) 'password not on the broker stdout'
    } finally { if (-not $p.HasExited) { $p.Kill() }; $p.Dispose() }
}

Test-Case 'password source: pipe, terminal prompt, or refusal' {
    $script:Interactive = $false
    Assert-Throws { [void](Get-AdminPasswordFromOpts @{}) } 'No password source' 'no source'
    $script:Interactive = $true; $script:SecretAnswer = 'typed-secret'
    Assert-Equal 'typed-secret' (Get-AdminPasswordFromOpts @{}) 'terminal prompt'
    Assert-Match (Get-LogText) 'password source: terminal prompt' 'logged which source'
    $script:SecretAnswer = ''
    Assert-Throws { [void](Get-AdminPasswordFromOpts @{}) } 'No password was entered' 'empty prompt'
}

Test-Case 'Linux CLI call: the password is written to stdin once with one newline, never an argument, log line or output' {
    Add-ExtRule '/usr/local/bin/cognita update' (New-ExtResult)
    $s = New-TestSettings
    $r = Invoke-CognitaLinux -Settings $s -CliArgs @('update', '--no-pull', '--non-interactive', '--admin-password-stdin') -StdinText ($script:Pw + "`n") -Stage 'cognita'
    Assert-Equal 0 $r.ExitCode 'exit'
    $call = (Get-ExtCallsMatching 'cognita update')[0]
    Assert-Equal ($script:Pw + "`n") $call.StdinText 'stdin is the password plus one newline'
    Assert-NotMatch ($call.Arguments -join ' ') ([regex]::Escape($script:Pw)) 'not an argument'
    Assert-Match ($call.Arguments -join ' ') '--admin-password-stdin' 'the flag that says so'
    Assert-NotMatch (Get-LogText) ([regex]::Escape($script:Pw)) 'not in the log'
    Assert-NotMatch ((Get-OutLines) -join "`n") ([regex]::Escape($script:Pw)) 'not in the output'
    Assert-Equal '-d|Cognita|-u|cognita|--exec|/usr/local/bin/cognita|update' (($call.Arguments[0..6]) -join '|') 'wsl -d <distro> -u <user> --exec cognita <verb>'
}

Complete-Tests
