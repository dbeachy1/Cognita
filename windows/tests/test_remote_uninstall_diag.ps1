# test_remote_uninstall_diag.ps1 - Funnel (design 9), uninstall (7.5), diagnostics (10), 14.2
. (Join-Path $PSScriptRoot '..\CognitaWin.ps1') -NoMain
. (Join-Path $PSScriptRoot '_harness.ps1')
Add-Type -AssemblyName System.IO.Compression.FileSystem

$script:Pw = 'p' + (Get-Utf8 0xE4) + 'ssw' + (Get-Utf8 0xF6) + 'rd' + (Get-Utf8 0x20AC)
$script:TsExe = 'C:\ts\tailscale.exe'
$script:DnsName = 'cognita-testpc.tail1234.ts.net.'

function Get-TsStatusJson {
    param([string]$State = 'Running')
    return ('{"BackendState": "' + $State + '", "Version": "1.102.4", "Self": {"DNSName": "' + $script:DnsName + '", "HostName": "cognita-testpc", "Online": true}, "Peer": {"nodekey:abc": {"HostName": "other-device", "DNSName": "other-device.tail1234.ts.net."}}}')
}
function Get-FunnelJsonFor {
    param([hashtable]$PortToTarget)
    if ($PortToTarget.Count -eq 0) { return '{}' }
    $tcp = @(); $web = @()
    foreach ($k in $PortToTarget.Keys) {
        $tcp += ('"{0}": {{"HTTPS": true}}' -f $k)
        $web += ('"cognita-testpc.tail1234.ts.net:{0}": {{"Handlers": {{"/": {{"Proxy": "{1}"}}}}}}' -f $k, $PortToTarget[$k])
    }
    return ('{"TCP": {' + ($tcp -join ', ') + '}, "Web": {' + ($web -join ', ') + '}}')
}
function Add-TsFakes {
    param([string]$State = 'Running', [hashtable]$Serving = @{})
    $script:TailscaleExe = $script:TsExe
    $script:tsState = $State
    $script:funnelJson = (Get-FunnelJsonFor $Serving)
    Add-ExtRule 'tailscale\.exe status --json' { param($c) New-ExtResult -Stdout (Get-TsStatusJson $script:tsState) }
    Add-ExtRule 'tailscale\.exe funnel status --json' { param($c) New-ExtResult -Stdout $script:funnelJson }
    Add-ExtRule 'tailscale\.exe funnel --bg' (New-ExtResult -Stdout 'Available on the internet.')
    Add-ExtRule '/usr/local/bin/cognita remote-access' (New-ExtResult)
}
function Initialize-InstalledForRemote {
    $vhd = Join-Path $script:TestDir 'vhd'
    return (New-TestSettings -State 'installed' -Vhd $vhd -RootPaths @('C:\Docs'))
}

# ---- funnel: pure parts ---------------------------------------------------------------------------
Test-Case 'funnel status JSON: ports come from TCP and Web, targets from the handlers' {
    $j = '{"TCP": {"443": {"HTTPS": true}, "8443": {"HTTPS": true}}, "Web": {"cognita-testpc.tail1234.ts.net:443": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:8675"}}}}, "AllowFunnel": {"cognita-testpc.tail1234.ts.net:443": true}}'
    $h = Get-FunnelServing ($j | ConvertFrom-Json)
    Assert-True ($h.ContainsKey(443) -and $h.ContainsKey(8443)) 'both ports in use'
    Assert-Equal 'http://127.0.0.1:8675' @($h[443])[0] 'target of 443'
    Assert-Equal 0 @($h[8443]).Count 'a TCP-only forward has no target'
    Assert-Equal 0 (Get-FunnelServing $null).Count 'nothing served'
}

Test-Case 'funnel plan: never clobber 443; offer 8443 then 10000; reuse our own; nothing free means none' {
    $mcp = 8675
    $p = Get-FunnelPlan -Serving @{} -McpPort $mcp
    Assert-Equal 'set' $p.Action 'free 443'; Assert-Equal 443 $p.Port 'port'
    $p = Get-FunnelPlan -Serving @{ 443 = @('http://127.0.0.1:8675') } -McpPort $mcp
    Assert-Equal 'reuse' $p.Action 'ours on 443'; Assert-Equal 443 $p.Port 'port'
    $p = Get-FunnelPlan -Serving @{ 443 = @('http://localhost:8675/') } -McpPort $mcp
    Assert-Equal 'reuse' $p.Action 'localhost spelling and a trailing slash'
    $p = Get-FunnelPlan -Serving @{ 443 = @('http://127.0.0.1:3000') } -McpPort $mcp
    Assert-Equal 'choose' $p.Action 'someone else''s on 443'; Assert-Equal '8443,10000' ($p.Free -join ',') 'alternatives'
    $p = Get-FunnelPlan -Serving @{ 443 = @('http://127.0.0.1:3000'); 8443 = @('http://127.0.0.1:1') } -McpPort $mcp
    Assert-Equal 'choose' $p.Action 'one alternative left'; Assert-Equal '10000' ($p.Free -join ',') 'only 10000'
    $p = Get-FunnelPlan -Serving @{ 443 = @('http://127.0.0.1:3000'); 8443 = @('x'); 10000 = @('y') } -McpPort $mcp
    Assert-Equal 'none' $p.Action 'nothing free'
    $p = Get-FunnelPlan -Serving @{ 443 = @('a') } -McpPort $mcp -Preferred 8443
    Assert-Equal 'set' $p.Action 'preferred and free'; Assert-Equal 8443 $p.Port 'port'
    $p = Get-FunnelPlan -Serving @{ 443 = @('a'); 8443 = @('b') } -McpPort $mcp -Preferred 8443
    Assert-Equal 'none' $p.Action 'preferred but busy: never clobbered'
    $p = Get-FunnelPlan -Serving @{ 443 = @() } -McpPort $mcp
    Assert-Equal 'choose' $p.Action 'a TCP-only forward on 443 is not ours'
}

Test-Case 'public URL: Self.DNSName without the trailing dot, :port only when it is not 443' {
    Assert-Equal 'https://cognita-testpc.tail1234.ts.net' (Get-PublicUrlFromDnsName -DnsName 'cognita-testpc.tail1234.ts.net.' -Port 443) '443'
    Assert-Equal 'https://cognita-testpc.tail1234.ts.net:8443' (Get-PublicUrlFromDnsName -DnsName 'cognita-testpc.tail1234.ts.net.' -Port 8443) '8443'
}

# ---- remote-access verb -------------------------------------------------------------------------------
Test-Case 'remote-access: free 443 -> funnel --bg --https=443 8675, funnel recorded, URL saved through the Linux CLI with the password on stdin' {
    $s = Initialize-InstalledForRemote
    Add-TsFakes
    $script:Interactive = $true; $script:SecretAnswer = $script:Pw
    $r = Invoke-RemoteAccessVerb -Opts @{}
    Assert-Equal 'ok' $r.Status ("status: " + ($script:Out | Select-Object -Last 3))
    Assert-Equal 'https://cognita-testpc.tail1234.ts.net' $r.Values['public_url'] 'public url'
    $f = (Get-ExtCallsMatching 'funnel --bg')[0]
    Assert-Equal 'funnel|--bg|--https=443|8675' ($f.Arguments -join '|') 'exact funnel command'
    $lc = (Get-ExtCallsMatching 'cognita remote-access')[0]
    Assert-Equal "remote-access|--external-url|https://cognita-testpc.tail1234.ts.net|--admin-password-stdin|--non-interactive|--progress-file|$(ConvertTo-WslMntPath $script:ProgressFile)" ($lc.Arguments[6..($lc.Arguments.Count - 1)] -join '|') 'Linux call'
    Assert-Equal ($script:Pw + "`n") $lc.StdinText 'password on stdin'
    $saved = Read-Settings
    Assert-Equal 443 $saved.funnel.https_port 'funnel recorded because WE set it'
    Assert-Equal 8675 $saved.funnel.target 'target recorded'
    Assert-NotMatch ((Get-LogText) + ((Get-ExtCallLines) -join "`n")) ([regex]::Escape($script:Pw)) 'password nowhere'
}

Test-Case 'remote-access: 443 already serves our MCP port -> nothing is set, nothing recorded' {
    $s = Initialize-InstalledForRemote
    Add-TsFakes -Serving @{ 443 = 'http://127.0.0.1:8675' }
    $script:Interactive = $true; $script:SecretAnswer = 'pw'
    $r = Invoke-RemoteAccessVerb -Opts @{}
    Assert-Equal 'ok' $r.Status 'ok'
    Assert-Equal 0 (Get-ExtCallsMatching 'funnel --bg').Count 'no funnel command'
    Assert-True ($null -eq (Read-Settings).funnel) 'funnel is recorded only when this install set it'
    Assert-Match (Get-LogText) 'port 443 already serves localhost:8675; nothing to set' 'decision logged'
}

Test-Case 'remote-access: 443 serves something else -> never clobbered; Setup (no terminal) gets funnel-port-busy with the offer' {
    $s = Initialize-InstalledForRemote
    Add-TsFakes -Serving @{ 443 = 'http://127.0.0.1:3000' }
    $script:Interactive = $false
    $opts = (ConvertFrom-HelperArgs @('--yes')).Opts
    # No terminal and no password pipe: give the password through the pipe-less test seam.
    function Get-AdminPasswordFromOpts { param($Opts) return 'pw' }
    $r = Invoke-RemoteAccessVerb -Opts $opts
    Assert-Equal 'failed' $r.Status 'failed'
    Assert-Equal 'funnel-port-busy' $r.Values['reason'] 'reason'
    Assert-Equal '8443,10000' $r.Values['offer'] 'offer'
    Assert-Equal 0 (Get-ExtCallsMatching 'funnel --bg').Count 'the existing Funnel was not touched'
    Assert-Equal 0 (Get-ExtCallsMatching 'cognita remote-access').Count 'no Linux call'
}

Test-Case 'remote-access: --funnel-port 8443 (or typing it in a terminal) uses 8443 and the URL carries the port' {
    $s = Initialize-InstalledForRemote
    Add-TsFakes -Serving @{ 443 = 'http://127.0.0.1:3000' }
    function Get-AdminPasswordFromOpts { param($Opts) return 'pw' }
    $opts = (ConvertFrom-HelperArgs @('--funnel-port', '8443')).Opts
    $r = Invoke-RemoteAccessVerb -Opts $opts
    Assert-Equal 'ok' $r.Status 'ok'
    Assert-Equal 'funnel|--bg|--https=8443|8675' ((Get-ExtCallsMatching 'funnel --bg')[0].Arguments -join '|') 'command'
    Assert-Equal 'https://cognita-testpc.tail1234.ts.net:8443' $r.Values['public_url'] 'URL with the port'
    Assert-Equal 8443 (Read-Settings).funnel.https_port 'recorded port'
    # in a terminal
    $script:ExtCalls.Clear()
    $script:Interactive = $true; $script:LineAnswers.Add('10000') | Out-Null
    $r2 = Invoke-RemoteAccessVerb -Opts @{}
    Assert-Equal 'https://cognita-testpc.tail1234.ts.net:10000' $r2.Values['public_url'] 'typed port'
}

Test-Case 'remote-access: no free Funnel port at all -> failed, nothing changed' {
    $s = Initialize-InstalledForRemote
    Add-TsFakes -Serving @{ 443 = 'a'; 8443 = 'b'; 10000 = 'c' }
    function Get-AdminPasswordFromOpts { param($Opts) return 'pw' }
    $r = Invoke-RemoteAccessVerb -Opts @{}
    Assert-Equal 'funnel-no-port' $r.Values['reason'] 'reason'
    Assert-Equal 0 (Get-ExtCallsMatching 'funnel --bg').Count 'nothing set'
}

Test-Case 'remote-access: "Funnel is not enabled on your tailnet" shows the owner link and can be retried' {
    $s = Initialize-InstalledForRemote
    Add-TsFakes
    $script:ExtRules.Insert(0, @{ Pattern = 'funnel --bg'; Response = (New-ExtResult -ExitCode 1 -Stderr "Funnel is not enabled on your tailnet.`n`nTo enable, visit:`n`n         https://login.tailscale.com/f/funnel?node=nABC123`n") })
    function Get-AdminPasswordFromOpts { param($Opts) return 'pw' }
    $r = Invoke-RemoteAccessVerb -Opts @{}
    Assert-Equal 'failed' $r.Status 'failed'
    Assert-Equal 'funnel-not-enabled' $r.Values['reason'] 'reason'
    Assert-Equal 'https://login.tailscale.com/f/funnel?node=nABC123' $r.Values['link'] 'link'
    # Design 18.4: a `failed` progress line (it was a warning) carrying the approval link in its message.
    $w = @((Get-ProgressObjects) | Where-Object { $_.stage -eq 'remote.funnel' -and $_.state -eq 'failed' })
    Assert-Equal 1 $w.Count 'one failed line'
    Assert-Equal 0 @((Get-ProgressObjects) | Where-Object { $_.stage -eq 'remote.funnel' -and $_.state -eq 'warning' }).Count 'and no warning any more'
    Assert-Match $w[0].message "Your tailnet's owner must turn on Funnel once\. Open this link, turn it on, then press Retry\. https://login\.tailscale\.com/f/funnel\?node=nABC123" 'design text with the clickable link'
    Assert-Equal 'result=failed;reason=funnel-not-enabled;link=https://login.tailscale.com/f/funnel?node=nABC123' (Format-ResultLine $r.Status $r.Values) 'the result line: reason and link (design 18.4)'
    Assert-True ($null -eq (Read-Settings).funnel) 'nothing recorded'
    # retry after the owner enabled it
    $script:ExtRules.RemoveAt(0)
    $r2 = Invoke-RemoteAccessVerb -Opts @{}
    Assert-Equal 'ok' $r2.Status 'retry works'
}

Test-Case 'remote-access: not signed in runs "tailscale up --unattended --hostname=cognita-<name>", surfaces the link once, waits for Running' {
    $s = Initialize-InstalledForRemote
    Add-TsFakes -State 'NeedsLogin'
    $script:ExtRules.Insert(0, @{ Pattern = 'tailscale\.exe up'; Response = {
        param($c)
        & $c.OnErrorLine 'To authenticate, visit:'
        & $c.OnErrorLine '        https://login.tailscale.com/a/abc123def'
        & $c.OnErrorLine '        https://login.tailscale.com/a/abc123def'
        $script:tsState = 'Running'
        New-ExtResult } })
    function Get-AdminPasswordFromOpts { param($Opts) return 'pw' }
    $r = Invoke-RemoteAccessVerb -Opts @{}
    Assert-Equal 'ok' $r.Status ("status: " + ($script:Out | Select-Object -Last 3))
    $up = (Get-ExtCallsMatching 'tailscale\.exe up')[0]
    Assert-Equal 'up|--unattended|--hostname=cognita-testpc' ($up.Arguments -join '|') 'the proven command'
    Assert-Equal 600 $up.TimeoutSec 'bounded at 10 minutes'
    $links = @((Get-ProgressObjects) | Where-Object { $_.stage -eq 'remote.login' -and $_.state -eq 'warning' })
    Assert-Equal 1 $links.Count 'the link is shown once'
    Assert-Match $links[0].message 'Open this link to sign in to Tailscale: https://login\.tailscale\.com/a/abc123def' 'clickable link'
}

Test-Case 'remote-access (design 18.4): EVERY heartbeat during the sign-in wait carries the sign-in link again, so the link never disappears' {
    $s = Initialize-InstalledForRemote
    Add-TsFakes -State 'NeedsLogin'
    $script:ExtRules.Insert(0, @{ Pattern = 'tailscale\.exe up'; Response = {
        param($c)
        # a beat before the link is known says only "Still waiting"; then the link is printed once; then
        # three more beats (a 5-second poll each) must each repeat it
        Invoke-ClockSleep 5000; & $c.OnPoll
        & $c.OnErrorLine '        https://login.tailscale.com/a/abc123def'
        foreach ($i in 1..3) { Invoke-ClockSleep 5000; & $c.OnPoll }
        $script:tsState = 'Running'
        New-ExtResult } })
    function Get-AdminPasswordFromOpts { param($Opts) return 'pw' }
    $r = Invoke-RemoteAccessVerb -Opts @{}
    Assert-Equal 'ok' $r.Status ("status: " + ($script:Out | Select-Object -Last 3))
    $up = (Get-ExtCallsMatching 'tailscale\.exe up')[0]
    Assert-Equal 5000 $up.PollIntervalMs 'a heartbeat every 5 s'
    $beats = @((Get-ProgressObjects) | Where-Object { $_.stage -eq 'remote.login' -and $_.state -eq 'progress' -and $_.message -like 'Still waiting*' })
    Assert-Equal 4 $beats.Count 'four heartbeats in all'
    Assert-Equal 'Still waiting.' $beats[0].message 'before the link exists there is nothing to repeat'
    foreach ($b in $beats[1..3]) { Assert-Match $b.message 'https://login\.tailscale\.com/a/abc123def' 'the link is in the heartbeat message' }
}

Test-Case 'tailscale status (design 18.5): run with -NoLogOutput; the log gets BackendState and Self.DNSName and nothing about other devices' {
    $s = Initialize-InstalledForRemote
    Add-TsFakes
    $st = Get-TailscaleStatusObject -Exe $script:TsExe
    Assert-Equal 'Running' $st.BackendState 'parsed'
    $c = (Get-ExtCallsMatching 'tailscale\.exe status --json')[0]
    Assert-True $c.NoLogOutput 'the call was made with -NoLogOutput'
    $log = Get-LogText
    Assert-Match $log ('tailscale status: BackendState=Running Self.DNSName=\[' + [regex]::Escape($script:DnsName) + '\]') 'only those two values are logged'
    Assert-NotMatch $log 'other-device|Peer|nodekey' 'nothing about the rest of the tailnet'
}

Test-Case 'tailscale status (design 18.5): through the REAL Invoke-External the peer list never reaches the log' {
    Use-RealExternal
    Use-RealClock
    $cmd = Join-Path $script:TestDir 'fake-tailscale.cmd'
    $json = '{"BackendState": "Running", "Self": {"DNSName": "cognita-testpc.tail1234.ts.net."}, "Peer": {"nodekey:abc": {"HostName": "other-device", "DNSName": "other-device.tail1234.ts.net."}}}'
    [System.IO.File]::WriteAllText($cmd, ("@echo off`r`necho " + $json + "`r`nexit /b 0`r`n"))
    $st = Get-TailscaleStatusObject -Exe $cmd
    Assert-Equal 'Running' $st.BackendState 'the real process ran and its output parsed'
    $log = Get-LogText
    Assert-Match $log 'exec done: exit=0 .* \(output not logged\)' 'the exec line says the output is not logged'
    Assert-NotMatch $log 'other-device' 'the peer is nowhere in the log'
    Assert-Match $log 'tailscale status: BackendState=Running Self.DNSName=\[cognita-testpc\.tail1234\.ts\.net\.\]' 'our own two values are'
}

Test-Case 'remote-access: a sign-in that never finishes fails after the 10-minute bound' {
    $s = Initialize-InstalledForRemote
    Add-TsFakes -State 'NeedsLogin'
    $script:ExtRules.Insert(0, @{ Pattern = 'tailscale\.exe up'; Response = (New-ExtResult -ExitCode 124 -TimedOut $true) })
    function Get-AdminPasswordFromOpts { param($Opts) return 'pw' }
    $r = Invoke-RemoteAccessVerb -Opts @{}
    Assert-Equal 'tailscale-login' $r.Values['reason'] 'reason'
    Assert-Match ((@(Get-ProgressObjects) | ForEach-Object { $_.message }) -join ' ') 'did not finish within 10 minutes' 'message'
}

Test-Case 'remote-access: after a finished sign-in, Running is awaited for at most 120 s of clock time' {
    $s = Initialize-InstalledForRemote
    Add-TsFakes -State 'NeedsLogin'
    $script:ExtRules.Insert(0, @{ Pattern = 'tailscale\.exe up'; Response = (New-ExtResult) })   # up exits 0 but the state never becomes Running
    function Get-AdminPasswordFromOpts { param($Opts) return 'pw' }
    $t0 = Get-ClockNow
    $r = Invoke-RemoteAccessVerb -Opts @{}
    Assert-Equal 'tailscale-not-running' $r.Values['reason'] 'reason'
    Assert-True ((((Get-ClockNow) - $t0).TotalSeconds) -ge 120) 'a full 120 s waited'
    Assert-True ((((Get-ClockNow) - $t0).TotalSeconds) -lt 140) 'and no more than that'
    Assert-Equal 0 (Get-ExtCallsMatching 'funnel --bg').Count 'Funnel is not touched'
}

Test-Case 'remote-access: an installer that finishes but leaves no tailscale.exe fails after 60 s of clock time' {
    $s = Initialize-InstalledForRemote
    $script:TailscaleExe = $null
    Add-ExtRule 'msiexec\.exe' (New-ExtResult)
    function Invoke-Download { param([string]$Url, [string]$Dest, [int]$TimeoutSec = 900, [string]$Stage = 'remote') [System.IO.File]::WriteAllBytes($Dest, [byte[]](1)); return $true }
    function Get-FileSignatureInfo { param([string]$Path) return [pscustomobject]@{ Status = 'Valid'; Subject = 'O=Tailscale Inc.' } }
    function Get-AdminPasswordFromOpts { param($Opts) return 'pw' }
    $t0 = Get-ClockNow
    $r = Invoke-RemoteAccessVerb -Opts (ConvertFrom-HelperArgs @('--yes')).Opts
    Assert-Equal 'tailscale-install' $r.Values['reason'] 'reason'
    Assert-True ((((Get-ClockNow) - $t0).TotalSeconds) -ge 60) 'a full 60 s waited'
    Assert-Match ((@(Get-ProgressObjects) | ForEach-Object { $_.message }) -join ' ') 'its command was not found' 'message'
}

Test-Case 'download: the real Invoke-Download fetches a file:// URL (no network), reports a missing file as false' {
    # A local file:// address exercises the real WebClient path with no network. The wait is on the
    # download task's own completion; the real clock is used so it cannot time out before the thread runs.
    Use-RealClock
    $src = Join-Path $script:TestDir 'source.bin'
    [System.IO.File]::WriteAllBytes($src, [byte[]](1..200))
    $dest = Join-Path $script:TestDir 'dest.bin'
    Assert-True (Invoke-Download -Url ([uri]$src).AbsoluteUri -Dest $dest -TimeoutSec 60) 'downloaded'
    Assert-Equal 200 (Get-Item -LiteralPath $dest).Length 'all bytes'
    Assert-False (Invoke-Download -Url ([uri](Join-Path $script:TestDir 'missing.bin')).AbsoluteUri -Dest (Join-Path $script:TestDir 'd2.bin') -TimeoutSec 60) 'a missing file is false, not an exception'
    Assert-Match (Get-LogText) 'download failed' 'and it is logged'
}

Test-Case 'signature: the real Get-FileSignatureInfo reads an Authenticode signer' {
    $ps = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
    $sig = Get-FileSignatureInfo -Path $ps
    Assert-Equal 'Valid' $sig.Status 'Windows'' own powershell.exe is validly signed'
    Assert-Match $sig.Subject 'O=Microsoft Corporation' 'subject is the signer''s'
    $unsigned = Join-Path $script:TestDir 'unsigned.msi'
    [System.IO.File]::WriteAllBytes($unsigned, [byte[]](1, 2, 3))
    Assert-True ((Get-FileSignatureInfo -Path $unsigned).Status -ne 'Valid') 'an unsigned file is not Valid'
}

Test-Case 'remote-access: Tailscale missing and no consent is a clear failure; nothing is downloaded' {
    $s = Initialize-InstalledForRemote
    $script:TailscaleExe = $null
    $script:downloads = 0
    function Invoke-Download { param([string]$Url, [string]$Dest, [int]$TimeoutSec = 900, [string]$Stage = 'remote') $script:downloads++; return $true }
    function Get-AdminPasswordFromOpts { param($Opts) return 'pw' }
    $r = Invoke-RemoteAccessVerb -Opts @{}
    Assert-Equal 'tailscale-missing' $r.Values['reason'] 'reason'
    Assert-Equal 0 $script:downloads 'no download without consent'
}

Test-Case 'remote-access: --yes downloads the official MSI, checks the signer, runs msiexec, then continues' {
    $s = Initialize-InstalledForRemote
    $script:TailscaleExe = $null
    Add-ExtRule 'tailscale\.exe status --json' { param($c) New-ExtResult -Stdout (Get-TsStatusJson 'Running') }
    Add-ExtRule 'tailscale\.exe funnel status --json' (New-ExtResult -Stdout '{}')
    Add-ExtRule 'tailscale\.exe funnel --bg' (New-ExtResult)
    Add-ExtRule '/usr/local/bin/cognita remote-access' (New-ExtResult)
    Add-ExtRule 'msiexec\.exe' { param($c) $script:TailscaleExe = $script:TsExe; New-ExtResult }
    $script:dlUrl = ''
    function Invoke-Download { param([string]$Url, [string]$Dest, [int]$TimeoutSec = 900, [string]$Stage = 'remote') $script:dlUrl = $Url; [System.IO.File]::WriteAllBytes($Dest, [byte[]](1, 2, 3)); return $true }
    function Get-FileSignatureInfo { param([string]$Path) return [pscustomobject]@{ Status = 'Valid'; Subject = 'CN=Tailscale Inc., O=Tailscale Inc., L=Toronto, S=Ontario, C=CA' } }
    function Get-AdminPasswordFromOpts { param($Opts) return 'pw' }
    $opts = (ConvertFrom-HelperArgs @('--yes')).Opts
    $r = Invoke-RemoteAccessVerb -Opts $opts
    Assert-Equal 'ok' $r.Status ("status: " + ($script:Out | Select-Object -Last 3))
    Assert-Equal 'https://pkgs.tailscale.com/stable/tailscale-setup-latest-amd64.msi' $script:dlUrl 'the official URL'
    $m = (Get-ExtCallsMatching 'msiexec')[0]
    Assert-Equal '/i' $m.Arguments[0] 'msiexec /i'
    Assert-Equal '/passive|/norestart' ($m.Arguments[2..3] -join '|') 'basic UI so Windows can ask for permission'
    Assert-False (Test-Path $m.Arguments[1]) 'the downloaded MSI is removed afterwards'
}

Test-Case 'remote-access: an MSI not signed by Tailscale Inc. is never run and is deleted' {
    $s = Initialize-InstalledForRemote
    $script:TailscaleExe = $null
    function Invoke-Download { param([string]$Url, [string]$Dest, [int]$TimeoutSec = 900, [string]$Stage = 'remote') [System.IO.File]::WriteAllBytes($Dest, [byte[]](1, 2, 3)); $script:msiPath = $Dest; return $true }
    function Get-FileSignatureInfo { param([string]$Path) return [pscustomobject]@{ Status = 'Valid'; Subject = 'CN=Someone Else, O=Someone Else Ltd' } }
    function Get-AdminPasswordFromOpts { param($Opts) return 'pw' }
    $r = Invoke-RemoteAccessVerb -Opts (ConvertFrom-HelperArgs @('--yes')).Opts
    Assert-Equal 'tailscale-install' $r.Values['reason'] 'reason'
    Assert-Equal 0 (Get-ExtCallsMatching 'msiexec').Count 'not run'
    Assert-False (Test-Path $script:msiPath) 'deleted'
    Assert-Match ((@(Get-ProgressObjects) | ForEach-Object { $_.message }) -join ' ') 'not signed by Tailscale Inc\.' 'message'
    $script:ExtCalls.Clear(); $script:Out.Clear()
    function Get-FileSignatureInfo { param([string]$Path) return [pscustomobject]@{ Status = 'HashMismatch'; Subject = 'O=Tailscale Inc.' } }
    Assert-Equal 'tailscale-install' (Invoke-RemoteAccessVerb -Opts (ConvertFrom-HelperArgs @('--yes')).Opts).Values['reason'] 'a broken signature is refused too'
}

Test-Case 'remote-access: Linux failing to save the address is reported with the address' {
    $s = Initialize-InstalledForRemote
    Add-TsFakes
    $script:ExtRules.Insert(0, @{ Pattern = 'cognita remote-access'; Response = (New-ExtResult -ExitCode 1 -Stderr '502 from the public address') })
    function Get-AdminPasswordFromOpts { param($Opts) return 'pw' }
    $r = Invoke-RemoteAccessVerb -Opts @{}
    Assert-Equal 'failed' $r.Status 'failed'
    Assert-Equal 'linux-remote-access' $r.Values['reason'] 'reason'
    Assert-Equal 'https://cognita-testpc.tail1234.ts.net' $r.Values['public_url'] 'address returned so it can be shown'
}

# ---- uninstall (design 7.5) -----------------------------------------------------------------------------------
function Initialize-UninstallWorld {
    param([switch]$WithFunnel)
    $docs = New-Dir 'docs'
    [System.IO.File]::WriteAllText((Join-Path $docs 'precious.txt'), 'the user''s document')
    $vhd = New-Dir 'vhd'
    [System.IO.File]::WriteAllBytes((Join-Path $vhd 'ext4.vhdx'), (New-Object byte[] 4096))
    $s = New-TestSettings -State 'installed' -Vhd $vhd -RootPaths @($docs)
    if ($WithFunnel) { Set-SettingProp $s 'funnel' ([pscustomobject]@{ https_port = 443; target = 8675 }); Save-Settings $s }
    $script:LxssDistros = @([pscustomobject]@{ Guid = '{g}'; Name = 'Cognita'; BasePath = $vhd })
    $logs = New-Dir 'home\logs'
    [System.IO.File]::WriteAllText((Join-Path $logs 'helper-old.log'), "an old log`n")
    [void](New-Dir 'home\bin'); [System.IO.File]::WriteAllText((Join-Path $env:COGNITA_HOME 'bin\cognita.exe'), 'x')
    [void](New-Dir 'home\app'); [System.IO.File]::WriteAllText((Join-Path $env:COGNITA_HOME 'app\CognitaWin.ps1'), 'x')
    $script:ownerId = $s.installation_id
    $script:flagAtFirstCall = $null
    Add-ExtRule 'cat /etc/cognita-distro' { param($c) New-ExtResult -Stdout ('{"installation_id": "' + $script:ownerId + '"}') }
    # Design 18.3: the Linux CLI asks "Uninstall Cognita now?" and fails on the empty stdin unless BOTH flags
    # are present, so the fake refuses the call without them (exit 2, as the real CLI's prompt would fail).
    Add-ExtRule '/usr/local/bin/cognita uninstall' {
        param($c)
        if ($null -eq $script:flagAtFirstCall) { $script:flagAtFirstCall = (Test-Path (Get-StoppedFlagPath)) }
        if (($c.Arguments -notcontains '--yes') -or ($c.Arguments -notcontains '--non-interactive')) { return (New-ExtResult -ExitCode 2 -Stderr 'Uninstall Cognita now? [y/N] (no answer on stdin)') }
        New-ExtResult
    }
    Add-ExtRule 'wsl\.exe --terminate' (New-ExtResult)
    $script:UninstVhd = $vhd
    Add-ExtRule 'wsl\.exe --unregister' { param($c) $d = Join-Path $script:UninstVhd 'ext4.vhdx'; if (Test-Path -LiteralPath $d) { Remove-Item -LiteralPath $d -Force }; New-ExtResult }
    return @{ Docs = $docs; Vhd = $vhd; Settings = $s }
}

Test-Case 'uninstall --keep-data: stopped flag first, Linux uninstall, task removed, terminate; settings stay as "uninstalled"; logs and the distro stay' {
    $w = Initialize-UninstallWorld
    $opts = (ConvertFrom-HelperArgs @('--keep-data')).Opts
    $r = Invoke-UninstallVerb -Opts $opts
    Assert-Equal 'ok' $r.Status ("status: " + ($script:Out | Select-Object -Last 3))
    Assert-True $script:flagAtFirstCall 'the stopped flag existed before cognita uninstall ran'
    Assert-Equal 1 $script:TaskRemoved 'scheduled task removed'
    Assert-Equal 1 (Get-ExtCallsMatching 'wsl\.exe --terminate Cognita').Count 'terminated (the distro is ours)'
    Assert-Equal 0 (Get-ExtCallsMatching '--unregister').Count 'the distro is kept'
    # Design 18.3: exactly these arguments, so the CLI does not stop to ask and fail on an empty stdin.
    $un = (Get-ExtCallsMatching 'cognita uninstall')[0]
    Assert-Equal '-d|Cognita|-u|cognita|--exec|/usr/local/bin/cognita|uninstall|--yes|--non-interactive' ($un.Arguments -join '|') 'cognita uninstall --yes --non-interactive'
    Assert-Equal 0 @((Get-ProgressObjects) | Where-Object { $_.stage -eq 'uninstall' -and $_.state -eq 'warning' }).Count 'and it succeeded: no warning about the inside-WSL step'
    Assert-Equal 1 @((Get-ProgressObjects) | Where-Object { $_.stage -eq 'uninstall' -and $_.state -eq 'done' }).Count 'reported done'
    $s = Read-Settings
    Assert-Equal 'uninstalled' $s.state 'state'
    Assert-True ($null -eq $s.funnel) 'funnel cleared'
    Assert-True (Test-Path (Join-Path $env:COGNITA_HOME 'logs\helper-old.log')) 'logs stay'
    Assert-True (Test-Path (Join-Path $w.Vhd 'ext4.vhdx')) 'the disk stays'
    Assert-True (Test-Path (Join-Path $w.Docs 'precious.txt')) 'documents untouched'
    Assert-Equal 1 $r.Values['kept'] 'kept=1'
    Assert-Equal 4096 $r.Values['vhd_bytes'] 'size for the last page'
    Assert-Match ((Get-OutLines) -join "`n") 'Your documents were not touched\. Cognita.s data is kept in .+vhd \(0\.0 GB\)\. Installing Cognita again reuses it\.' 'design message'
}

Test-Case 'uninstall --delete-data: ownership re-checked BEFORE wsl --unregister; logs copied out; only known items deleted; documents, bin and app untouched' {
    $w = Initialize-UninstallWorld
    $r = Invoke-UninstallVerb -Opts (ConvertFrom-HelperArgs @('--delete-data')).Opts
    Assert-Equal 'ok' $r.Status ("status: " + ($script:Out | Select-Object -Last 3))
    $calls = Get-ExtCallLines
    $iMarker = [array]::LastIndexOf(@($calls | ForEach-Object { $_ -match 'cat /etc/cognita-distro' }), $true)
    $iUn = [array]::IndexOf(@($calls | ForEach-Object { $_ -match '--unregister Cognita' }), $true)
    Assert-True (($iUn -ge 0) -and ($iMarker -ge 0) -and ($iMarker -lt $iUn)) 'the marker was read again right before the unregister'
    Assert-False (Test-Path (Get-SettingsPath)) 'settings.json deleted'
    Assert-False (Test-Path (Get-StoppedFlagPath)) 'stopped flag deleted'
    Assert-False (Test-Path (Join-Path $env:COGNITA_HOME 'logs')) 'logs deleted'
    Assert-False (Test-Path $w.Vhd) 'the empty disk folder is removed after the unregister'
    Assert-True (Test-Path (Join-Path $env:COGNITA_HOME 'bin\cognita.exe')) 'bin belongs to Inno: untouched'
    Assert-True (Test-Path (Join-Path $env:COGNITA_HOME 'app\CognitaWin.ps1')) 'app belongs to Inno: untouched'
    Assert-True (Test-Path (Join-Path $w.Docs 'precious.txt')) 'documents untouched'
    $copy = $r.Values['logs_copy']
    Assert-True ($copy -like '*Cognita-uninstall-*') 'the logs were copied to a Cognita-uninstall-<stamp> folder'
    Assert-True (Test-Path (Join-Path $copy 'helper-old.log')) 'old log copied'
    Remove-Item -LiteralPath $copy -Recurse -Force
}

Test-Case 'uninstall --delete-data: a foreign distro named Cognita is NOT unregistered' {
    $w = Initialize-UninstallWorld
    $script:ownerId = 'someone-elses-id'
    $r = Invoke-UninstallVerb -Opts (ConvertFrom-HelperArgs @('--delete-data')).Opts
    Assert-Equal 0 (Get-ExtCallsMatching '--unregister').Count 'not unregistered'
    Assert-Equal 0 (Get-ExtCallsMatching 'cognita uninstall').Count 'and Cognita''s Linux uninstall was not run in it either'
    Assert-Equal 0 (Get-ExtCallsMatching '--terminate').Count 'and it was not terminated: wsl --terminate only for a distro that is ours (design 18.5)'
    Assert-Match (Get-LogText) 'uninstall: wsl --terminate skipped \(settings=True distroIsOurs=False\)' 'the skip is logged with its values'
    Assert-Match ((@(Get-ProgressObjects) | ForEach-Object { $_.message }) -join ' ') 'is not this install.s, so it was NOT removed' 'said out loud'
    Remove-Item -LiteralPath $r.Values['logs_copy'] -Recurse -Force -ErrorAction SilentlyContinue
}

Test-Case 'uninstall --delete-data (design 19.4 item 9): a failed unregister stops before anything on the Windows side is deleted, BUT settings are saved as state=uninstalled first and the result keeps the copied logs' {
    $w = Initialize-UninstallWorld -WithFunnel
    $script:TailscaleExe = $script:TsExe
    Add-ExtRule 'tailscale\.exe funnel status --json' (New-ExtResult -Stdout (Get-FunnelJsonFor @{ 443 = 'http://127.0.0.1:8675' }))
    Add-ExtRule 'tailscale\.exe funnel --https=443 off' (New-ExtResult)
    Set-SettingProp $w.Settings 'resume' 'after-wsl'; Save-Settings $w.Settings
    $script:ExtRules.Insert(0, @{ Pattern = '--unregister'; Response = (New-ExtResult -ExitCode 1) })
    $r = Invoke-UninstallVerb -Opts (ConvertFrom-HelperArgs @('--delete-data')).Opts
    Assert-Equal 'failed' $r.Status 'failed'
    Assert-Equal 'unregister-failed' $r.Values['reason'] 'reason'
    Assert-True (Test-Path (Get-SettingsPath)) 'settings.json still there (the disk and the record of it are not deleted)'
    $s = Read-Settings
    Assert-Equal 'uninstalled' $s.state 'state=uninstalled: Cognita is removed, only the data is left'
    Assert-True ($null -eq $s.funnel) 'the funnel record cleared, as a keep-data uninstall does'
    Assert-True ($null -eq $s.resume) 'and the resume marker'
    Assert-True (Test-Path (Join-Path $env:COGNITA_HOME 'logs\helper-old.log')) 'logs still there'
    Assert-True (Test-Path (Join-Path $w.Vhd 'ext4.vhdx')) 'the disk is still there (nothing was deleted)'
    $copy = [string]$r.Values['logs_copy']
    Assert-True ($copy -like '*Cognita-uninstall-*') 'the result names the copied logs folder for Setup''s closing text'
    Assert-True (Test-Path (Join-Path $copy 'helper-old.log')) 'the old log is in the copy'
    Assert-Match (Get-LogText) 'uninstall: wsl --unregister failed; settings saved with state=uninstalled before returning failed' 'the decision is logged'
    Assert-Match (Get-LogText) 'uninstall: returning failed reason=unregister-failed logs_copy=\[.*Cognita-uninstall-' 'the returned values are logged'
    Remove-Item -LiteralPath $copy -Recurse -Force -ErrorAction SilentlyContinue
}

Test-Case 'uninstall --delete-data (design 19.7 item 20): the parent folders the helper created for a custom disk location are removed when empty, deepest first; one that holds something stays' {
    $w = Initialize-UninstallWorld
    # C:\<t>\a\b (deepest first, as the helper records them) both created by the helper; C:\<t>\c\d likewise,
    # but the user has since put a file in c.
    $b = New-Dir 'a\b'; $a = Join-Path $script:TestDir 'a'
    $d = New-Dir 'c\d'; $c = Join-Path $script:TestDir 'c'
    [System.IO.File]::WriteAllText((Join-Path $c 'users-file.txt'), 'x')
    $s = Read-Settings
    Set-SettingProp $s 'created_dirs' @($b, $a, $d, $c)
    Save-Settings $s
    $r = Invoke-UninstallVerb -Opts (ConvertFrom-HelperArgs @('--delete-data')).Opts
    Assert-Equal 'ok' $r.Status ("status: " + ($script:Out | Select-Object -Last 3))
    Assert-False (Test-Path -LiteralPath $b) 'the empty deepest folder is removed'
    Assert-False (Test-Path -LiteralPath $a) 'and then its now-empty parent'
    Assert-False (Test-Path -LiteralPath $d) 'the empty deepest folder of the second chain is removed'
    Assert-True (Test-Path -LiteralPath (Join-Path $c 'users-file.txt')) 'a folder holding the user''s file is left alone'
    Remove-Item -LiteralPath $r.Values['logs_copy'] -Recurse -Force -ErrorAction SilentlyContinue
}

Test-Case 'uninstall --keep-data does not remove created_dirs (the disk and its record stay)' {
    $w = Initialize-UninstallWorld
    $b = New-Dir 'a\b'
    $s = Read-Settings; Set-SettingProp $s 'created_dirs' @($b); Save-Settings $s
    $r = Invoke-UninstallVerb -Opts (ConvertFrom-HelperArgs @('--keep-data')).Opts
    Assert-Equal 'ok' $r.Status 'ok'
    Assert-True (Test-Path -LiteralPath $b) 'kept'
    Assert-Equal $b (@((Read-Settings).created_dirs))[0] 'and still recorded'
}

Test-Case 'uninstall --delete-data: a disk folder with other files in it is left alone (never recursive)' {
    $w = Initialize-UninstallWorld
    [System.IO.File]::WriteAllText((Join-Path $w.Vhd 'not-ours.txt'), 'x')
    $r = Invoke-UninstallVerb -Opts (ConvertFrom-HelperArgs @('--delete-data')).Opts
    Assert-True (Test-Path (Join-Path $w.Vhd 'not-ours.txt')) 'left alone'
    Assert-Match (Get-LogText) 'not empty after unregister \(1 item\(s\)\); left alone' 'logged'
    Remove-Item -LiteralPath $r.Values['logs_copy'] -Recurse -Force -ErrorAction SilentlyContinue
}

Test-Case 'uninstall: the delete list is exactly the known items and never overlaps a root' {
    $w = Initialize-UninstallWorld
    $list = @(Get-UninstallDeleteList -Settings $w.Settings)
    $names = @($list | ForEach-Object { Split-Path -Leaf $_ })
    Assert-Equal 'settings.json|settings.json.tmp|stopped|logs' ($names -join '|') 'the known items and nothing else'
    $rel = @($list | ForEach-Object { $_.Substring($env:COGNITA_HOME.Length).TrimStart('\') })
    Assert-Equal 'settings.json|settings.json.tmp|stopped|logs' ($rel -join '|') 'all directly under the data folder; no app, bin or documents'
    Assert-Equal 0 @($list | Where-Object { Test-PathNests $_ $w.Docs }).Count 'no item overlaps a root'
    Assert-True ($null -eq (Assert-DeleteListSafe -Paths $list -Settings $w.Settings)) 'safe'
    $bad = New-TestSettings -State 'installed' -RootPaths @((Join-Path $env:COGNITA_HOME 'logs\rootinside'))
    Assert-Throws { Assert-DeleteListSafe -Paths $list -Settings $bad } 'Refusing to delete' 'a root inside a listed folder is refused'
}

Test-Case 'delete: a junction inside a deleted folder is removed as a link and its target is never entered' {
    $target = New-Dir 'precious-target'
    [System.IO.File]::WriteAllText((Join-Path $target 'keep.txt'), 'keep me')
    $victim = New-Dir 'victim'
    [System.IO.File]::WriteAllText((Join-Path $victim 'a.txt'), 'a')
    [void](New-Item -ItemType Junction -Path (Join-Path $victim 'link') -Target $target)
    $file = Join-Path $victim 'sub'; [void](New-Item -ItemType Directory -Path $file)
    [System.IO.File]::WriteAllText((Join-Path $file 'b.txt'), 'b')
    [System.IO.File]::SetAttributes((Join-Path $file 'b.txt'), [System.IO.FileAttributes]::ReadOnly)
    Remove-NoFollow -Path $victim
    Assert-False (Test-Path $victim) 'the folder is gone'
    Assert-True (Test-Path (Join-Path $target 'keep.txt')) 'what the junction pointed at is untouched'
    Assert-Match (Get-LogText) 'is a link; removing the link only' 'logged'
    # and a junction given directly
    $lnk = Join-Path $script:TestDir 'direct-link'
    [void](New-Item -ItemType Junction -Path $lnk -Target $target)
    Remove-NoFollow -Path $lnk
    Assert-False (Test-Path $lnk) 'link removed'
    Assert-True (Test-Path (Join-Path $target 'keep.txt')) 'target untouched'
}

Test-Case 'uninstall --delete-data: a junction planted in logs\ that points at a projects folder cannot reach the documents' {
    $w = Initialize-UninstallWorld
    [void](New-Item -ItemType Junction -Path (Join-Path $env:COGNITA_HOME 'logs\sneaky') -Target $w.Docs)
    $r = Invoke-UninstallVerb -Opts (ConvertFrom-HelperArgs @('--delete-data')).Opts
    Assert-True (Test-Path (Join-Path $w.Docs 'precious.txt')) 'the document survives'
    Assert-False (Test-Path (Join-Path $env:COGNITA_HOME 'logs')) 'logs removed'
    Remove-Item -LiteralPath $r.Values['logs_copy'] -Recurse -Force -ErrorAction SilentlyContinue
}

Test-Case 'uninstall: Funnel is turned off only when settings record that we set it AND the exact target is still served' {
    $w = Initialize-UninstallWorld -WithFunnel
    $script:TailscaleExe = $script:TsExe
    Add-ExtRule 'tailscale\.exe funnel status --json' (New-ExtResult -Stdout (Get-FunnelJsonFor @{ 443 = 'http://127.0.0.1:8675' }))
    Add-ExtRule 'tailscale\.exe funnel --https=443 off' (New-ExtResult)
    [void](Invoke-UninstallVerb -Opts (ConvertFrom-HelperArgs @('--keep-data')).Opts)
    Assert-Equal 1 (Get-ExtCallsMatching 'funnel --https=443 off').Count 'turned off'
    # someone re-pointed the Funnel at another service: leave it
    $script:ExtRules.Clear(); $script:ExtCalls.Clear()
    $w2 = Initialize-UninstallWorld -WithFunnel
    Add-ExtRule 'tailscale\.exe funnel status --json' (New-ExtResult -Stdout (Get-FunnelJsonFor @{ 443 = 'http://127.0.0.1:3000' }))
    Add-ExtRule 'tailscale\.exe funnel --https=443 off' (New-ExtResult)
    [void](Invoke-UninstallVerb -Opts (ConvertFrom-HelperArgs @('--keep-data')).Opts)
    Assert-Equal 0 (Get-ExtCallsMatching 'funnel --https=443 off').Count 'a Funnel that no longer points at us is not touched'
    Assert-Match (Get-LogText) 'exactStillServed=False' 'decision logged'
    # not recorded as ours: never touched, even though it matches
    $script:ExtRules.Clear(); $script:ExtCalls.Clear()
    $w3 = Initialize-UninstallWorld
    Add-ExtRule 'tailscale\.exe funnel status --json' (New-ExtResult -Stdout (Get-FunnelJsonFor @{ 443 = 'http://127.0.0.1:8675' }))
    Add-ExtRule 'tailscale\.exe funnel --https=443 off' (New-ExtResult)
    [void](Invoke-UninstallVerb -Opts (ConvertFrom-HelperArgs @('--keep-data')).Opts)
    Assert-Equal 0 (Get-ExtCallsMatching 'funnel').Count 'not recorded as ours: no tailscale call at all'
}

Test-Case 'uninstall: a distro that does not start does not stop the Windows parts' {
    $w = Initialize-UninstallWorld
    $script:ExtRules.Insert(0, @{ Pattern = 'cognita uninstall'; Response = (New-ExtResult -ExitCode -1 -Stderr 'HCS_E_SERVICE_NOT_AVAILABLE') })
    $r = Invoke-UninstallVerb -Opts (ConvertFrom-HelperArgs @('--keep-data')).Opts
    Assert-Equal 'ok' $r.Status 'ok'
    Assert-Equal 1 $script:TaskRemoved 'task still removed'
    Assert-Equal 'uninstalled' (Read-Settings).state 'state still recorded'
    Assert-Equal 1 @((Get-ProgressObjects) | Where-Object { $_.stage -eq 'uninstall' -and $_.state -eq 'warning' }).Count 'a warning'
}

Test-Case 'uninstall (design 18.5): no distro at all (unregistered by hand) or no settings: nothing is terminated' {
    $w = Initialize-UninstallWorld
    $script:LxssDistros = @()
    $r = Invoke-UninstallVerb -Opts (ConvertFrom-HelperArgs @('--keep-data')).Opts
    Assert-Equal 'ok' $r.Status 'ok'
    Assert-Equal 0 (Get-ExtCallsMatching '--terminate|cognita uninstall').Count 'no terminate and no inside-WSL step for a distro that does not exist'
    Assert-Equal 1 $script:TaskRemoved 'the Windows parts still ran'
}

Test-Case 'uninstall: needs exactly one of --keep-data / --delete-data' {
    Assert-Equal 'failed' (Invoke-UninstallVerb -Opts @{}).Status 'neither'
    Assert-Equal 'failed' (Invoke-UninstallVerb -Opts @{ 'keep-data' = $true; 'delete-data' = $true }).Status 'both'
}

# ---- diagnostics (design 10) -----------------------------------------------------------------------------------
function Add-DiagFakes {
    param([bool]$DistroStarts = $true)
    $script:TailscaleExe = $script:TsExe
    $script:WslConfig = "[wsl2]`nmemory=8GB`nsecretKey=abc123`n"
    Add-ExtRule 'wsl\.exe --version' (New-ExtResult -Stdout 'WSL version: 2.7.14.0')
    Add-ExtRule 'wsl\.exe -l -v' (New-ExtResult -Stdout '* Cognita Running 2')
    Add-ExtRule 'wsl\.exe --status' (New-ExtResult -Stdout 'Default Version: 2')
    if ($DistroStarts) {
        Add-ExtRule '-u root --exec true' (New-ExtResult)
        Add-ExtRule '/usr/local/bin/cognita diagnostics --out (\S+)' {
            param($c)
            $p = $c.Arguments[-1]
            if ($p -match '^/mnt/([a-z])/(.*)$') { $win = $Matches[1].ToUpper() + ':\' + ($Matches[2] -replace '/', '\'); [System.IO.File]::WriteAllBytes($win, [byte[]](80, 75, 5, 6, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0)) }
            New-ExtResult
        }
        Add-ExtRule '-u root --exec sh -s' { param($c) if ($c.StdinText -match 'grep -nE') { New-ExtResult -Stdout "2:/mnt/cognita-roots /mnt/cognita-roots none bind,shared 0 0`n3:C:/Docs /mnt/cognita-roots/1 drvfs uid=1000,gid=1000,noatime,nofail,shared 0 0`n" } else { New-ExtResult -Stdout "C:\ on /mnt/cognita-roots/1 type 9p`n" } }
        Add-ExtRule 'systemctl --failed' (New-ExtResult -Stdout '0 loaded units listed.')
        Add-ExtRule 'journalctl -b' (New-ExtResult -Stdout "Sep 29 client GET /mcp/SECRETTOKEN123456/ from 10.0.0.2`nother line`n")
    } else {
        Add-ExtRule '-u root --exec true' (New-ExtResult -ExitCode -1 -Stderr 'Wsl/Service/CreateInstance/HCS_E_SERVICE_NOT_AVAILABLE')
    }
    Add-ExtRule 'tailscale\.exe status --json' (New-ExtResult -Stdout (Get-TsStatusJson 'Running'))
    Add-ExtRule 'tailscale\.exe funnel status' (New-ExtResult -Stdout 'https://cognita-testpc.tail1234.ts.net (Funnel on)')
}
function Read-ZipEntries {
    param([string]$Path)
    $z = [System.IO.Compression.ZipFile]::OpenRead($Path)
    try {
        $h = @{}
        foreach ($e in $z.Entries) {
            $sr = New-Object System.IO.StreamReader($e.Open(), (New-Object System.Text.UTF8Encoding($false)))
            try { $h[$e.FullName] = $sr.ReadToEnd() } finally { $sr.Dispose() }
        }
        return $h
    } finally { $z.Dispose() }
}

Test-Case 'token scan: /mcp/<token> segments are redacted, other text is untouched' {
    Assert-Equal 'GET /mcp/<redacted> HTTP/1.1' (Protect-DiagnosticText 'GET /mcp/abcDEF123_-token HTTP/1.1') 'in a request line'
    Assert-Equal 'https://x.ts.net/mcp/<redacted>/sse?x=1' (Protect-DiagnosticText 'https://x.ts.net/mcp/tok123/sse?x=1') 'in a URL, before the next slash'
    Assert-Equal 'a /mcp/<redacted> b /mcp/<redacted>' (Protect-DiagnosticText 'a /mcp/one b /mcp/two') 'several'
    Assert-Equal '/mcp/<redacted>' (Protect-DiagnosticText '/mcp/<redacted>') 'idempotent'
    Assert-Equal 'nothing to see' (Protect-DiagnosticText 'nothing to see') 'untouched'
}

Test-Case 'diagnostics: a zip on the Desktop with windows/, linux/, README.txt; tokens redacted everywhere; Self only; no secrets' {
    $vhd = Join-Path $script:TestDir 'vhd'
    $s = New-TestSettings -State 'installed' -Vhd $vhd -RootPaths @('C:\Docs')
    $script:LxssDistros = @([pscustomobject]@{ Guid = '{g}'; Name = 'Cognita'; BasePath = $vhd })
    New-Item -ItemType Directory -Path (Get-LogsDir) -Force | Out-Null
    [System.IO.File]::WriteAllText((Join-Path (Get-LogsDir) 'helper-20260101-000000.log'), "old run: connector called /mcp/LOGTOKEN999 ok`n")
    $setupLog = Join-Path $script:TestDir 'Setup Log 2026.txt'
    [System.IO.File]::WriteAllText($setupLog, "setup line with /mcp/SETUPTOKEN777`n")
    Add-DiagFakes
    $opts = (ConvertFrom-HelperArgs @('--setup-log', $setupLog, '--no-open')).Opts
    $r = Invoke-DiagnosticsVerb -Opts $opts
    Assert-Equal 'ok' $r.Status ("status: " + ($script:Out | Select-Object -Last 3))
    $zip = $r.Values['zip']
    Assert-True ($zip -like (Join-Path $script:DesktopDir 'Cognita-diagnostics-TESTPC-*.zip')) ("on the Desktop with the computer name and a stamp: " + $zip)
    $e = Read-ZipEntries $zip
    $names = @($e.Keys | Sort-Object)
    foreach ($need in @('README.txt', 'windows/settings.json', 'windows/wsl-version.txt', 'windows/wsl-list.txt', 'windows/wsl-status.txt', 'windows/wslconfig.txt', 'windows/system.txt', 'windows/scheduled-task.txt', 'windows/listeners.txt', 'windows/tailscale-status.json', 'windows/tailscale-funnel.txt', 'windows/setup-Setup Log 2026.txt', 'linux/cognita-diagnostics.zip', 'linux/fstab-roots.txt', 'linux/mounts.txt', 'linux/systemctl-failed.txt', 'linux/journal-boot.txt')) {
        Assert-True ($names -contains $need) ("entry $need present; have: " + ($names -join ', '))
    }
    Assert-True (@($names | Where-Object { $_ -like 'windows/logs/helper-*.log' }).Count -ge 1) 'the helper logs are included'
    Assert-Match $e['linux/journal-boot.txt'] '/mcp/<redacted>/' 'journal token redacted'
    Assert-Match $e['windows/setup-Setup Log 2026.txt'] '/mcp/<redacted>' 'Setup log token redacted'
    $all = ($e.Keys | Where-Object { $_ -notlike '*.zip' } | ForEach-Object { $e[$_] }) -join "`n"
    foreach ($tok in @('SECRETTOKEN123456', 'LOGTOKEN999', 'SETUPTOKEN777')) { Assert-NotMatch $all $tok "no $tok anywhere" }
    Assert-Match $e['windows/tailscale-status.json'] 'cognita-testpc\.tail1234\.ts\.net' 'Self is there'
    Assert-NotMatch $e['windows/tailscale-status.json'] 'other-device' 'other devices on the tailnet are not collected'
    Assert-Match $e['windows/wslconfig.txt'] 'memory=8GB' 'wslconfig keys kept'
    Assert-Match $e['windows/wslconfig.txt'] 'secretKey=<redacted>' 'a secret-like value is not collected'
    Assert-NotMatch $all 'abc123' 'and it is nowhere else'
    Assert-Match $e['README.txt'] 'No passwords, secrets, tokens or document contents are collected' 'README says what is (not) collected'
    Assert-Match $e['README.txt'] 'Nothing was sent anywhere\. To get help, attach this zip to a new issue at\s+https://github\.com/dbeachy1/Cognita/issues/new' 'README says nothing was sent and where help is'
    Assert-Match ((Get-OutLines) -join "`n") 'Nothing was sent anywhere\. To get help, attach this file to a new issue at https://github\.com/dbeachy1/Cognita/issues/new' 'and so does the verb''s own output'
    Assert-NotMatch $all '(?i)password.{0,4}[:=]' 'no password fields'
    Assert-Equal 0 $script:Explorer.Count 'not opened with --no-open'
    Assert-Match $e['windows/settings.json'] '"state":\s*"installed"' 'settings.json copied'
    # Design 18.5: real newlines in scheduled-task.txt (it was one line with literal backtick-n in it).
    Assert-Equal 4 @($e['windows/scheduled-task.txt'] -split "`n").Count 'four lines'
    Assert-Match $e['windows/scheduled-task.txt'] "^task exists: True`nstate: Ready`nlast result: 0`nlast run: " 'one fact per line'
    Assert-NotMatch $e['windows/scheduled-task.txt'] '`n' 'no literal backtick-n left in the text'
    Assert-Match $e['linux/mounts.txt'] 'cognita-roots' 'the mounts file is there'
    Assert-Match $e['linux/fstab-roots.txt'] '(?m)^2:/mnt/cognita-roots /mnt/cognita-roots none bind,shared' 'the base line is in fstab-roots.txt with its line number (design 22.14)'
    $g = @($script:ExtCalls | Where-Object { [string]$_.StdinText -match 'grep -nE' })
    Assert-Equal 1 $g.Count 'one fstab script'
    Assert-True ($g[0].StdinText.Contains("'^[^#]*/mnt/cognita-roots(/|[[:space:]])'")) 'the pattern matches the base line as well as the root lines'
}

Test-Case 'diagnostics (design 18.1): the mounts file asks for Docker''s view (nsenter -t 1 -m) as well as the session''s own' {
    New-Item -ItemType Directory -Path (Get-LogsDir) -Force | Out-Null
    $s = New-TestSettings -State 'installed'
    Add-DiagFakes
    [void](Invoke-DiagnosticsVerb -Opts (ConvertFrom-HelperArgs @('--no-open')).Opts)
    $scripts = @($script:ExtCalls | Where-Object { $_.StdinText -match 'grep cognita-roots' } | ForEach-Object { $_.StdinText })
    Assert-Equal 1 $scripts.Count 'one mounts script'
    Assert-Match $scripts[0] 'nsenter -t 1 -m -- findmnt -n -o TARGET,FSTYPE,PROPAGATION' 'PID 1''s namespace with the propagation column'
}

Test-Case 'diagnostics: opens Explorer on the zip by default; --out names the file or a folder' {
    New-Item -ItemType Directory -Path (Get-LogsDir) -Force | Out-Null
    $s = New-TestSettings -State 'installed'
    Add-DiagFakes
    $out = Join-Path $script:TestDir 'named.zip'
    $r = Invoke-DiagnosticsVerb -Opts (ConvertFrom-HelperArgs @('--out', $out)).Opts
    Assert-Equal $out $r.Values['zip'] 'the file asked for'
    Assert-Equal 1 $script:Explorer.Count 'Explorer opened'
    Assert-Equal $out $script:Explorer[0] 'on the zip'
    $dir = New-Dir 'outdir'
    $r2 = Invoke-DiagnosticsVerb -Opts (ConvertFrom-HelperArgs @('--out', $dir, '--no-open')).Opts
    Assert-True ($r2.Values['zip'] -like (Join-Path $dir 'Cognita-diagnostics-*.zip')) 'a folder gets a named zip'
}

Test-Case 'diagnostics: a distro that will not start still gives the Windows half and says why in linux/' {
    New-Item -ItemType Directory -Path (Get-LogsDir) -Force | Out-Null
    $s = New-TestSettings -State 'installed'
    Add-DiagFakes -DistroStarts $false
    $r = Invoke-DiagnosticsVerb -Opts (ConvertFrom-HelperArgs @('--no-open')).Opts
    Assert-Equal 'ok' $r.Status 'still ok'
    $e = Read-ZipEntries $r.Values['zip']
    Assert-True ($e.ContainsKey('windows/wsl-version.txt') -and $e.ContainsKey('windows/settings.json')) 'Windows part present'
    Assert-Match $e['linux/UNAVAILABLE.txt'] 'could not be started for diagnostics' 'reason recorded'
    Assert-Match $e['linux/UNAVAILABLE.txt'] 'HCS_E_SERVICE_NOT_AVAILABLE' 'with the error'
}

Test-Case 'diagnostics: no install record at all still produces a zip' {
    New-Item -ItemType Directory -Path (Get-LogsDir) -Force | Out-Null
    Add-DiagFakes
    $r = Invoke-DiagnosticsVerb -Opts (ConvertFrom-HelperArgs @('--no-open')).Opts
    Assert-Equal 'ok' $r.Status 'ok'
    Assert-Match ((Read-ZipEntries $r.Values['zip'])['linux/UNAVAILABLE.txt']) 'No Cognita install record' 'says so'
}

Test-Case 'diagnostics: the staging folder is removed' {
    New-Item -ItemType Directory -Path (Get-LogsDir) -Force | Out-Null
    $s = New-TestSettings -State 'installed'
    Add-DiagFakes
    $before = @(Get-ChildItem -LiteralPath ([System.IO.Path]::GetTempPath()) -Directory -Filter 'cognita-diag-*' -ErrorAction SilentlyContinue).Count
    [void](Invoke-DiagnosticsVerb -Opts (ConvertFrom-HelperArgs @('--no-open')).Opts)
    $after = @(Get-ChildItem -LiteralPath ([System.IO.Path]::GetTempPath()) -Directory -Filter 'cognita-diag-*' -ErrorAction SilentlyContinue).Count
    Assert-Equal $before $after 'no cognita-diag-* folder left behind'
}

Complete-Tests
