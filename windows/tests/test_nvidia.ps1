# test_nvidia.ps1 - NVIDIA detection, the pinned toolkit step, and the .wslconfig reclaim line (design 22.2, 22.5, 22.9, 22.10, 22.12)
# The verbs' use of these (install, update, state, status, the forwarded install and rollback) is tested
# beside their other tests in test_install.ps1, test_distro.ps1 and test_cli.ps1.
. (Join-Path $PSScriptRoot '..\CognitaWin.ps1') -NoMain
. (Join-Path $PSScriptRoot '_harness.ps1')

# ---- ConvertTo-NvidiaDriverVersion ------------------------------------------------------------------
Test-Case 'ConvertTo-NvidiaDriverVersion (design 22.2): the WDDM string becomes NVIDIA''s own version; anything else is unknown' {
    $table = @(
        @('32.0.15.8092', '580.92'),
        @('31.0.15.5222', '552.22'),
        @('27.21.14.5671', '456.71'),
        @('32.0.15.616', '506.16'),
        @(' 32.0.15.8092 ', '580.92'),
        @('32.0.16.1001', '610.01'),
        @('', ''),
        @('abc', ''),
        @('1.2.3', ''),
        @('32.0.15.8092.1', ''),
        @('32.0.15.80920', ''),
        @('32.0.x.8092', ''),
        @('32.0.15.', '')
    )
    foreach ($row in $table) { Assert-Equal $row[1] (ConvertTo-NvidiaDriverVersion $row[0]) ("[{0}]" -f $row[0]) }
}

# ---- Get-NvidiaCard ----------------------------------------------------------------------------------
function Set-Smi {
    param([string]$Stdout = '', [int]$ExitCode = 0, [bool]$TimedOut = $false)
    $script:NvidiaSmiPath = 'C:\Windows\System32\nvidia-smi.exe'
    $script:SmiResult = New-ExtResult -ExitCode $ExitCode -Stdout $Stdout -TimedOut $TimedOut
    Add-ExtRule 'nvidia-smi\.exe --query-gpu=name,driver_version --format=csv,noheader' { param($c) $script:SmiResult }
}
function New-Vc { param([string]$Name, [string]$Driver) return [pscustomobject]@{ Name = $Name; DriverVersion = $Driver } }

Test-Case 'nvidia (22.2): nvidia-smi with a driver of 580 or newer is ok; the call, its timeout and the log line are exact' {
    Set-Smi "NVIDIA GeForce RTX 4090, 616.92`r`n"
    $c = Get-NvidiaCard
    Assert-Equal 'ok' $c.State 'state'
    Assert-Equal 'NVIDIA GeForce RTX 4090' $c.Name 'name'
    Assert-Equal '616.92' $c.Driver 'driver'
    Assert-Equal 'nvidia-smi' $c.Source 'source'
    $call = $script:ExtCalls[0]
    Assert-Equal 'C:\Windows\System32\nvidia-smi.exe' $call.FilePath 'the System32 nvidia-smi'
    Assert-Equal '--query-gpu=name,driver_version|--format=csv,noheader' ($call.Arguments -join '|') 'arguments'
    Assert-Equal 20 $call.TimeoutSec 'timeout 20 s'
    Assert-Match (Get-LogText) 'nvidia: source=nvidia-smi state=ok name=\[NVIDIA GeForce RTX 4090\] driver=\[616\.92\] rows=1' 'one log line with the values'
}

Test-Case 'nvidia (22.2): a driver below 580 is old; exactly 580.00 is ok' {
    Set-Smi "NVIDIA GeForce GTX 1080, 531.79`n"
    $c = Get-NvidiaCard
    Assert-Equal 'old' $c.State 'old'
    Assert-Equal '531.79' $c.Driver 'driver'
    Assert-Equal 'NVIDIA GeForce GTX 1080' $c.Name 'first row when none qualifies'
    $script:ExtRules.Clear()
    Set-Smi "NVIDIA RTX A2000, 580.00`n"
    Assert-Equal 'ok' (Get-NvidiaCard).State '580.00 qualifies'
}

Test-Case 'nvidia (22.2): two cards, one old and one ok, is ok and names the qualifying card (either order); two old cards stay old with the first name' {
    Set-Smi "NVIDIA GeForce GTX 1080, 531.79`nNVIDIA GeForce RTX 4090, 616.92`n"
    $c = Get-NvidiaCard
    Assert-Equal 'ok' $c.State 'mixed is ok'
    Assert-Equal 'NVIDIA GeForce RTX 4090' $c.Name 'the qualifying row'
    Assert-Equal '616.92' $c.Driver 'its driver'
    Assert-Match (Get-LogText) 'rows=2' 'both rows counted'
    $script:ExtRules.Clear()
    Set-Smi "NVIDIA GeForce RTX 4090, 616.92`nNVIDIA GeForce GTX 1080, 531.79`n"
    Assert-Equal 'NVIDIA GeForce RTX 4090' (Get-NvidiaCard).Name 'other order'
    $script:ExtRules.Clear()
    Set-Smi "NVIDIA GeForce GTX 1080, 531.79`nNVIDIA GeForce GTX 970, 470.10`n"
    $o = Get-NvidiaCard
    Assert-Equal 'old' $o.State 'two old cards'
    Assert-Equal 'NVIDIA GeForce GTX 1080' $o.Name 'first row'
}

Test-Case 'nvidia (22.2): a name that holds a comma splits on the LAST comma; a version nvidia-smi cannot give is unknown and not held against the card' {
    Set-Smi "NVIDIA RTX, Special Edition, 590.44`n"
    $c = Get-NvidiaCard
    Assert-Equal 'NVIDIA RTX, Special Edition' $c.Name 'name keeps its comma'
    Assert-Equal '590.44' $c.Driver 'version after the last comma'
    $script:ExtRules.Clear()
    Set-Smi "NVIDIA GeForce RTX 4090, [N/A]`n"
    $u = Get-NvidiaCard
    Assert-Equal 'ok' $u.State 'unknown driver is ok'
    Assert-Equal '' $u.Driver 'driver unknown'
    Assert-Equal 'NVIDIA GeForce RTX 4090' $u.Name 'name'
}

Test-Case 'nvidia (22.2): garbage, an empty answer, a non-zero exit or a timeout from nvidia-smi falls through to WMI, with the reason logged' {
    $script:VideoControllers = @((New-Vc 'NVIDIA GeForce RTX 4090' '32.0.15.8092'))
    foreach ($case in @(
        @{ Out = "this is not csv`n"; Exit = 0; Timed = $false; Reason = 'no parseable row' },
        @{ Out = ''; Exit = 0; Timed = $false; Reason = 'no parseable row' },
        @{ Out = 'NVIDIA-SMI has failed'; Exit = 9; Timed = $false; Reason = 'nvidia-smi exit 9' },
        @{ Out = ''; Exit = 0; Timed = $true; Reason = 'nvidia-smi timed out' })) {
        $script:ExtRules.Clear(); $script:LogLines.Clear()
        Set-Smi $case.Out $case.Exit $case.Timed
        $c = Get-NvidiaCard
        Assert-Equal 'wmi' $c.Source ("source for " + $case.Reason)
        Assert-Equal 'ok' $c.State 'ok from WMI'
        Assert-Equal '580.92' $c.Driver 'WDDM converted'
        Assert-Match (Get-LogText) ([regex]::Escape($case.Reason)) 'the fall-through reason is in the log'
    }
}

Test-Case 'nvidia (22.2): nvidia-smi that throws (the call itself fails) falls through to WMI' {
    $script:NvidiaSmiPath = 'C:\Windows\System32\nvidia-smi.exe'     # no external rule: the fake throws "Unexpected external call"
    $script:VideoControllers = @((New-Vc 'NVIDIA GeForce RTX 3060' '31.0.15.5222'))
    $c = Get-NvidiaCard
    Assert-Equal 'wmi' $c.Source 'wmi'
    Assert-Equal '552.22' $c.Driver 'WDDM 31.0.15.5222 is 552.22'
    Assert-Match (Get-LogText) 'nvidia-smi step threw' 'logged'
}

Test-Case 'nvidia (22.2): no nvidia-smi.exe, only WMI rows: a non-NVIDIA card is none, an NVIDIA one with an unreadable version is ok, an old one is old' {
    Assert-Equal 'none' (Get-NvidiaCard).State 'no cards at all'
    $script:VideoControllers = @((New-Vc 'Intel(R) UHD Graphics' '31.0.101.4502'), (New-Vc 'AMD Radeon RX 7900' '31.0.24002.92'))
    $none = Get-NvidiaCard
    Assert-Equal 'none' $none.State 'non-NVIDIA cards'
    Assert-Equal 'none' $none.Source 'source none'
    Assert-Match (Get-LogText) 'nvidia: source=none state=none name=\[\] driver=\[\] rows=0' 'logged'
    $script:VideoControllers = @((New-Vc 'Intel(R) UHD Graphics' '31.0.101.4502'), (New-Vc 'nvidia geforce rtx 4070' 'garbage'))
    $u = Get-NvidiaCard
    Assert-Equal 'ok' $u.State 'unknown driver is not held against the card'
    Assert-Equal '' $u.Driver 'unknown driver'
    Assert-Equal 'nvidia geforce rtx 4070' $u.Name 'the name test is case-insensitive'
    $script:VideoControllers = @((New-Vc 'NVIDIA GeForce GTX 1060' '27.21.14.5671'))
    $o = Get-NvidiaCard
    Assert-Equal 'old' $o.State 'old'
    Assert-Equal '456.71' $o.Driver 'WDDM 27.21.14.5671 is 456.71'
    $script:VideoControllers = @((New-Vc 'NVIDIA GeForce GTX 1060' '27.21.14.5671'), (New-Vc 'NVIDIA GeForce RTX 4090' '32.0.15.8092'))
    $m = Get-NvidiaCard
    Assert-Equal 'ok' $m.State 'mixed is ok'
    Assert-Equal 'NVIDIA GeForce RTX 4090' $m.Name 'the qualifying row'
}

Test-Case 'nvidia (22.2): CIM throwing is logged and means none; Get-NvidiaCard never throws' {
    $script:VideoControllersThrow = 'CIM is broken'
    $c = Get-NvidiaCard
    Assert-Equal 'none' $c.State 'none'
    Assert-Match (Get-LogText) 'video controller read failed: CIM is broken' 'the swallowed exception is logged'
    Assert-Equal 1 @($script:LogLines | Where-Object { $_ -match '\[helper\] nvidia: source=' }).Count 'exactly one summary line'
}

# ---- preflight result keys ---------------------------------------------------------------------------
Test-Case 'preflight (22.2, 22.9): the result gains nvidia, nvidia_name, nvidia_driver and wsl_reclaim after warnings; no progress line for them; the final phase does not detect' {
    Add-ExtRule 'wsl\.exe --version' (New-ExtResult -Stdout "WSL version: 2.7.14.0`n")
    Set-Smi "NVIDIA GeForce RTX 4090, 616.92`n"
    $r = Invoke-CheckVerb -Opts @{ phase = 'preflight' }
    Assert-Equal 'ok' $r.Status 'status'
    Assert-Equal 'wsl,failures,warnings,nvidia,nvidia_name,nvidia_driver,wsl_reclaim' (@($r.Values.Keys) -join ',') 'key order'
    Assert-Equal 'ok' $r.Values['nvidia'] 'nvidia'
    Assert-Equal 'NVIDIA GeForce RTX 4090' $r.Values['nvidia_name'] 'name'
    Assert-Equal '616.92' $r.Values['nvidia_driver'] 'driver'
    Assert-Equal 'unset' $r.Values['wsl_reclaim'] 'no .wslconfig'
    Assert-Match (Format-ResultLine 'ok' $r.Values) ';nvidia=ok;nvidia_name=NVIDIA GeForce RTX 4090;nvidia_driver=616\.92;wsl_reclaim=unset$' 'on the result line'
    foreach ($p in (Get-ProgressObjects)) { Assert-True ($p.stage -eq 'check' -or $p.stage -like 'check.*') ("only check progress lines, saw " + $p.stage) }
    Assert-Match (Get-LogText) 'check preflight facts: nvidia=ok nvidia_name=\[NVIDIA GeForce RTX 4090\] nvidia_driver=\[616\.92\] wsl_reclaim=unset' 'logged with values'
    $script:ExtCalls.Clear()
    $f = Invoke-CheckVerb -Opts @{ phase = 'final' }
    Assert-False $f.Values.Contains('nvidia') 'the final phase has no nvidia key'
    Assert-False $f.Values.Contains('wsl_reclaim') 'nor wsl_reclaim'
    Assert-Equal 0 (Get-ExtCallsMatching 'nvidia-smi').Count 'and does not run nvidia-smi'
}

Test-Case 'preflight (22.2): no card at all gives nvidia=none with empty name and driver; a CIM failure still ends in a result' {
    Add-ExtRule 'wsl\.exe --version' (New-ExtResult -Stdout "WSL version: 2.7.14.0`n")
    $script:VideoControllersThrow = 'no CIM'
    $r = Invoke-CheckVerb -Opts @{ phase = 'preflight' }
    Assert-Equal 'ok' $r.Status 'status'
    Assert-Equal 'none' $r.Values['nvidia'] 'none'
    Assert-Equal '' $r.Values['nvidia_name'] 'no name'
    Assert-Equal '' $r.Values['nvidia_driver'] 'no driver'
}

# ---- Get-WslReclaimSetting (22.9) --------------------------------------------------------------------
$script:Latin1 = [System.Text.Encoding]::GetEncoding(28591)
function Write-Cfg { param([string]$Text) [System.IO.File]::WriteAllBytes($script:WslConfigPath, $script:Latin1.GetBytes($Text)) }
function Get-CfgText { return $script:Latin1.GetString([System.IO.File]::ReadAllBytes($script:WslConfigPath)) }

Test-Case 'wsl_reclaim (22.9): no file is unset; a key in [experimental] or [wsl2] (any case, any value) is set; the same key elsewhere, or a comment, is not' {
    Assert-Equal 'unset' (Get-WslReclaimSetting) 'no file'
    Write-Cfg "[wsl2]`nmemory=8GB`n"
    Assert-Equal 'unset' (Get-WslReclaimSetting) 'file without the key'
    Write-Cfg "[experimental]`nautoMemoryReclaim=dropCache`n"
    Assert-Equal 'set' (Get-WslReclaimSetting) 'experimental'
    Write-Cfg "[EXPERIMENTAL]`r`n  AUTOMEMORYRECLAIM = disabled`r`n"
    Assert-Equal 'set' (Get-WslReclaimSetting) 'any case, any value, "disabled" included'
    Write-Cfg "[wsl2]`nautoMemoryReclaim=gradual`n"
    Assert-Equal 'set' (Get-WslReclaimSetting) 'wsl2'
    Write-Cfg "[boot]`nautoMemoryReclaim=dropCache`n"
    Assert-Equal 'unset' (Get-WslReclaimSetting) 'another section does not count'
    Write-Cfg "[experimental]`n# autoMemoryReclaim=dropCache`n; autoMemoryReclaim=dropCache`n"
    Assert-Equal 'unset' (Get-WslReclaimSetting) 'comments do not count'
    Assert-Match (Get-LogText) 'wsl_reclaim: unset' 'logged with the value found'
}

Test-Case 'wsl_reclaim (22.9): a file that exists but cannot be read is unreadable' {
    Write-Cfg "[wsl2]`nmemory=8GB`n"
    $fs = [System.IO.File]::Open($script:WslConfigPath, [System.IO.FileMode]::Open, [System.IO.FileAccess]::ReadWrite, [System.IO.FileShare]::None)
    try { $v = Get-WslReclaimSetting } finally { $fs.Dispose() }
    Assert-Equal 'unreadable' $v 'locked file'
    Assert-Match (Get-LogText) 'exists but could not be read; unreadable' 'logged'
}

# ---- Set-WslReclaimSetting (22.9) --------------------------------------------------------------------
Test-Case 'wslconfig (22.9): the key goes in as the FIRST line after an existing [experimental] header; every other byte stays; the backup is the original' {
    $orig = "# my settings`n[wsl2]`nmemory=8GB`n[experimental]`nsparseVhd=true`n`n[boot]`nsystemd=true`n"
    Write-Cfg $orig
    $r = Set-WslReclaimSetting
    Assert-Equal 'added' $r.Status 'status'
    Assert-Equal "# my settings`n[wsl2]`nmemory=8GB`n[experimental]`nautoMemoryReclaim=dropCache`nsparseVhd=true`n`n[boot]`nsystemd=true`n" (Get-CfgText) 'exactly one line added'
    Assert-Equal $orig ($script:Latin1.GetString([System.IO.File]::ReadAllBytes($script:WslConfigPath + '.cognita-backup'))) 'backup is the original, byte for byte'
    Assert-False (Test-Path -LiteralPath ($script:WslConfigPath + '.cognita-tmp')) 'no temp file left'
    Assert-Match (Get-LogText) 'wslconfig: added autoMemoryReclaim=dropCache section=existing \[experimental\] encoding=utf-8 eol=lf bytes=\d+->\d+ backup=\[' 'logged with the section, encoding, endings and sizes'
    Assert-Equal 'set' (Get-WslReclaimSetting) 'and the reader now says set'
    $before = Get-CfgText
    $r2 = Set-WslReclaimSetting
    Assert-Equal 'already-set' $r2.Status 'a second run does nothing'
    Assert-Equal $before (Get-CfgText) 'file unchanged'
    Assert-Match (Get-LogText) 'wslconfig: autoMemoryReclaim is already set' 'logged'
}

Test-Case 'wslconfig (22.9): a header with odd spacing and case is still the [experimental] header' {
    Write-Cfg "[wsl2]`nmemory=8GB`n  [ Experimental ]  `nsparseVhd=true`n"
    [void](Set-WslReclaimSetting)
    Assert-Equal "[wsl2]`nmemory=8GB`n  [ Experimental ]  `nautoMemoryReclaim=dropCache`nsparseVhd=true`n" (Get-CfgText) 'inserted after it'
}

Test-Case 'wslconfig (22.9): no [experimental] section appends one, with one blank line before it; CRLF and LF files keep their own endings' {
    Write-Cfg "[wsl2]`r`nmemory=8GB`r`n"
    $r = Set-WslReclaimSetting
    Assert-Equal 'added' $r.Status 'added'
    Assert-Equal "[wsl2]`r`nmemory=8GB`r`n`r`n[experimental]`r`nautoMemoryReclaim=dropCache`r`n" (Get-CfgText) 'CRLF kept everywhere'
    Assert-Match (Get-LogText) 'section=new \[experimental\] encoding=utf-8 eol=crlf' 'logged'
    Remove-Item -LiteralPath $script:WslConfigPath, ($script:WslConfigPath + '.cognita-backup') -Force
    Write-Cfg "[wsl2]`nmemory=8GB`n"
    [void](Set-WslReclaimSetting)
    Assert-Equal "[wsl2]`nmemory=8GB`n`n[experimental]`nautoMemoryReclaim=dropCache`n" (Get-CfgText) 'LF kept'
}

Test-Case 'wslconfig (22.9): the last line without a newline, a file that already ends in a blank line, and a header that is the last line' {
    Write-Cfg "[wsl2]`nmemory=8GB"
    [void](Set-WslReclaimSetting)
    Assert-Equal "[wsl2]`nmemory=8GB`n`n[experimental]`nautoMemoryReclaim=dropCache`n" (Get-CfgText) 'no trailing newline'
    Remove-Item -LiteralPath $script:WslConfigPath -Force
    Write-Cfg "[wsl2]`nmemory=8GB`n`n"
    [void](Set-WslReclaimSetting)
    Assert-Equal "[wsl2]`nmemory=8GB`n`n[experimental]`nautoMemoryReclaim=dropCache`n" (Get-CfgText) 'already one blank line: not doubled'
    Remove-Item -LiteralPath $script:WslConfigPath -Force
    Write-Cfg "[wsl2]`r`nmemory=8GB`r`n[experimental]"
    [void](Set-WslReclaimSetting)
    Assert-Equal "[wsl2]`r`nmemory=8GB`r`n[experimental]`r`nautoMemoryReclaim=dropCache" (Get-CfgText) 'header last: the line follows it, the file still has no trailing newline'
}

Test-Case 'wslconfig (22.9): no file creates one with just the section (LF) and writes no backup' {
    Assert-False (Test-Path -LiteralPath $script:WslConfigPath) 'starts absent'
    $r = Set-WslReclaimSetting
    Assert-Equal 'added' $r.Status 'added'
    Assert-Equal "[experimental]`nautoMemoryReclaim=dropCache`n" (Get-CfgText) 'new file'
    Assert-False (Test-Path -LiteralPath ($script:WslConfigPath + '.cognita-backup')) 'no file, no backup'
    Assert-Match (Get-LogText) 'bytes=0->\d+ backup=\[none\]' 'logged'
}

Test-Case 'wslconfig (22.9): a key in either section, with any value, writes nothing and no backup' {
    foreach ($t in @("[experimental]`nautoMemoryReclaim=disabled`n", "[wsl2]`nAutoMemoryReclaim = gradual`n")) {
        Write-Cfg $t
        $r = Set-WslReclaimSetting
        Assert-Equal 'already-set' $r.Status 'already set'
        Assert-Equal $t (Get-CfgText) 'untouched'
        Assert-False (Test-Path -LiteralPath ($script:WslConfigPath + '.cognita-backup')) 'no backup'
    }
}

Test-Case 'wslconfig (22.9): a UTF-8 BOM stays a BOM, and bytes that are not valid UTF-8 survive untouched' {
    $bom = [byte[]](0xEF, 0xBB, 0xBF)
    $rest = $script:Latin1.GetBytes("[wsl2]`r`n# caf" + [string][char]0xE9 + "`r`nmemory=8GB`r`n")      # 0xE9 alone is not valid UTF-8
    [System.IO.File]::WriteAllBytes($script:WslConfigPath, [byte[]]($bom + $rest))
    $r = Set-WslReclaimSetting
    Assert-Equal 'added' $r.Status 'added'
    $now = [System.IO.File]::ReadAllBytes($script:WslConfigPath)
    Assert-Equal '239,187,191' ($now[0..2] -join ',') 'the BOM is still the first three bytes'
    Assert-Equal "[wsl2]`r`n# caf" ([string]$script:Latin1.GetString($now, 3, 13)) 'the start is as it was'
    $expected = $script:Latin1.GetBytes("[wsl2]`r`n# caf" + [string][char]0xE9 + "`r`nmemory=8GB`r`n`r`n[experimental]`r`nautoMemoryReclaim=dropCache`r`n")
    Assert-Equal (($bom + $expected) -join ',') ($now -join ',') 'every original byte kept (E9 included), CRLF, the section appended'
    Assert-Match (Get-LogText) 'encoding=utf-8-bom eol=crlf' 'logged'
}

Test-Case 'wslconfig (22.9): a UTF-16 file with a BOM stays UTF-16 with its BOM' {
    $enc = New-Object System.Text.UnicodeEncoding($false, $true)
    $orig = $enc.GetPreamble() + $enc.GetBytes("[wsl2]`r`nmemory=8GB`r`n")
    [System.IO.File]::WriteAllBytes($script:WslConfigPath, [byte[]]$orig)
    $r = Set-WslReclaimSetting
    Assert-Equal 'added' $r.Status ("added: " + $r.Detail)
    $now = [System.IO.File]::ReadAllBytes($script:WslConfigPath)
    Assert-Equal '255,254' ($now[0..1] -join ',') 'FF FE kept'
    Assert-Equal "[wsl2]`r`nmemory=8GB`r`n`r`n[experimental]`r`nautoMemoryReclaim=dropCache`r`n" ((New-Object System.Text.UnicodeEncoding($false, $false)).GetString($now, 2, $now.Length - 2)) 'text in UTF-16'
    Assert-Match (Get-LogText) 'encoding=utf-16le-bom' 'logged'
}

Test-Case 'wslconfig (22.9): the backup is overwritten each time Setup changes the file' {
    Write-Cfg "[wsl2]`nmemory=8GB`n"
    [void](Set-WslReclaimSetting)
    Write-Cfg "[wsl2]`nmemory=12GB`n"
    [void](Set-WslReclaimSetting)
    Assert-Equal "[wsl2]`nmemory=12GB`n" ($script:Latin1.GetString([System.IO.File]::ReadAllBytes($script:WslConfigPath + '.cognita-backup'))) 'the backup is the latest original'
}

Test-Case 'wslconfig (22.9): a failure (the backup cannot be written) is a warning, the file is untouched, no temp is left, and nothing is thrown' {
    $orig = "[wsl2]`nmemory=8GB`n"
    Write-Cfg $orig
    [void](New-Item -ItemType Directory -Path ($script:WslConfigPath + '.cognita-backup'))     # a folder where the backup file should go
    $r = Set-WslReclaimSetting
    Assert-Equal 'failed' $r.Status 'failed'
    Assert-Equal $orig (Get-CfgText) 'the original is untouched'
    Assert-False (Test-Path -LiteralPath ($script:WslConfigPath + '.cognita-tmp')) 'no temp file'
    $w = @((Get-ProgressObjects) | Where-Object { $_.stage -eq 'wslconfig' -and $_.state -eq 'warning' })
    Assert-Equal 1 $w.Count 'one warning line'
    Assert-Match $w[0].message '^Setup could not change your WSL settings \(.+\)\. Cognita works without it; WSL just keeps more memory\.$' 'the design text'
    Assert-Match (Get-LogText) 'wslconfig: could not add autoMemoryReclaim=dropCache' 'logged'
}

Test-Case 'wslconfig (22.9): an unreadable file is a warning too' {
    Write-Cfg "[wsl2]`nmemory=8GB`n"
    $fs = [System.IO.File]::Open($script:WslConfigPath, [System.IO.FileMode]::Open, [System.IO.FileAccess]::ReadWrite, [System.IO.FileShare]::None)
    try { $r = Set-WslReclaimSetting } finally { $fs.Dispose() }
    Assert-Equal 'failed' $r.Status 'failed'
    Assert-Equal 1 @((Get-ProgressObjects) | Where-Object { $_.stage -eq 'wslconfig' -and $_.state -eq 'warning' }).Count 'one warning'
}

# ---- the toolkit scripts (22.5, 22.12 items 3 to 7) --------------------------------------------------
Test-Case 'toolkit (22.5): the pin, the key URL and the list URL are literal text in the helper, and the install script names them' {
    $src = [System.IO.File]::ReadAllText((Join-Path $PSScriptRoot '..\CognitaWin.ps1'))
    Assert-True ($src.Contains("`$script:NvidiaToolkitVersion = '1.20.1-1'" + "`n")) 'the pin line, in exactly this form (a Python test greps it)'
    Assert-Equal '1.20.1-1' $script:NvidiaToolkitVersion 'value'
    Assert-True ($src.Contains('https://nvidia.github.io/libnvidia-container/gpgkey')) 'key URL'
    Assert-True ($src.Contains('https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list')) 'list URL'
    Assert-True ($script:NvidiaToolkitInstallScript.Contains($script:NvidiaKeyUrl)) 'the script uses the key URL'
    Assert-True ($script:NvidiaToolkitInstallScript.Contains($script:NvidiaListUrl)) 'and the list URL'
    foreach ($p in $script:NvidiaToolkitPackages) { Assert-True ($script:NvidiaToolkitInstallScript.Contains($p)) ("package " + $p) }
    Assert-Equal 4 $script:NvidiaToolkitPackages.Count 'four packages'
}

Test-Case 'toolkit (22.12 items 3, 4, 7): the install script - no curl pipes, a temp-file keyring with --yes, noninteractive apt with the three flags, holds, a daemon.json backup and a rollback with exit 3, no cdi generate, LF only' {
    $t = $script:NvidiaToolkitInstallScript
    Assert-False $t.Contains("`r") 'LF only'
    Assert-NotMatch $t 'cdi generate' 'never cdi generate'
    Assert-NotMatch $t '(?m)curl[^\n]*\|' 'no curl pipes'
    Assert-Match $t 'curl -fsSL -o /tmp/cognita-nv\.gpg https://nvidia\.github\.io/libnvidia-container/gpgkey' 'key to a temp file'
    Assert-Match $t 'gpg --batch --yes --dearmor -o "\$keyring" /tmp/cognita-nv\.gpg' 'dearmor to the keyring with --batch --yes'
    Assert-Match $t 'rm -f /tmp/cognita-nv\.gpg' 'temp removed'
    Assert-Match $t 'curl -fsSL -o /tmp/cognita-nv\.list https://nvidia\.github\.io/libnvidia-container/stable/deb/nvidia-container-toolkit\.list' 'list to a temp file'
    Assert-Match $t 'sed ''s#deb https://#deb \[signed-by=/etc/apt/keyrings/nvidia-container-toolkit-keyring\.gpg\] https://#g'' /tmp/cognita-nv\.list > /etc/apt/sources\.list\.d/nvidia-container-toolkit\.list' 'sed into place'
    Assert-Match $t 'export DEBIAN_FRONTEND=noninteractive' 'noninteractive'
    Assert-Match $t 'apt-get install -y -o Dpkg::Options::=--force-confold --allow-downgrades --allow-change-held-packages ' 'the install flags'
    Assert-Match $t 'apt-mark unhold \$pkgs </dev/null \|\| true' 'unhold, failure ignored'
    Assert-Match $t 'apt-mark hold \$pkgs </dev/null' 'hold'
    Assert-Match $t '"nvidia-container-toolkit=\$ver" "nvidia-container-toolkit-base=\$ver" "libnvidia-container1=\$ver" "libnvidia-container-tools=\$ver"' 'all four at the pin, from $1'
    Assert-Match $t 'ver="\$1"' 'the version is $1'
    Assert-NotMatch $t '1\.20\.1' 'the version is never written into the script text'
    Assert-Match $t 'cp -p /etc/docker/daemon\.json /etc/docker/daemon\.json\.cognita-bak' 'daemon.json backup'
    Assert-Match $t 'nvidia-ctk runtime configure --runtime=docker' 'runtime entry'
    Assert-Match $t 'timeout 60 docker info' 'Docker must answer, bounded'
    Assert-NotMatch $t '(?m)^\s*(if )?systemctl restart' 'every docker restart is bounded (docker.service has TimeoutStartSec=0)'
    Assert-Match $t 'timeout 90 systemctl restart docker' 'bounded restart'
    Assert-Match $t 'toolkit: rolled back' 'rollback line'
    Assert-Match $t 'exit 3' 'exit 3'
    Assert-Match $t 'toolkit: installed \$ver \(was \$\{have:-none\}\)' 'success line'
}

Test-Case 'toolkit (22.12 items 5, 7): the present check asks dpkg for all four packages at $1 and Docker for the nvidia runtime; LF only; no cdi generate' {
    $t = $script:NvidiaToolkitCheckScript
    Assert-False $t.Contains("`r") 'LF only'
    Assert-NotMatch $t 'cdi generate' 'never cdi generate'
    Assert-Match $t 'for p in nvidia-container-toolkit nvidia-container-toolkit-base libnvidia-container1 libnvidia-container-tools; do' 'all four'
    # 15.1.0 review: a removed-but-not-purged package still prints a version, so the status must say
    # "installed". Not the abbreviation: the packages are held, and a held package abbreviates to "hi".
    Assert-Match $t 'dpkg-query -W -f=''\$\{db:Status-Status\} \$\{Version\}'' "\$p"' 'dpkg-query per package, with the status'
    Assert-Match $t 'if \[ "\$have" != "installed \$ver" \]' 'installed at the pin'
    Assert-NotMatch $t 'Status-Abbrev' 'never the abbreviation (hi for a held package)'
    Assert-Match $t 'timeout 60 docker info --format ''\{\{json \.Runtimes\}\}''' 'Docker is asked, bounded'
    Assert-NotMatch $t 'daemon\.json' 'not the file'
    Assert-NotMatch $t '1\.20\.1' 'version only as $1'
}

Test-Case 'toolkit (22.12 item 7): every command in both scripts that could read stdin has </dev/null' {
    foreach ($t in @($script:NvidiaToolkitCheckScript, $script:NvidiaToolkitInstallScript)) {
        foreach ($l in ($t -split "`n")) {
            if ($l -match '(^|[\s(])(curl|gpg|apt-get|apt-mark|nvidia-ctk|systemctl|dpkg-query|docker info|cp -p|mkdir)\s' -and $l -notmatch '^\s*echo ') {
                Assert-True ($l -match '</dev/null') ("no </dev/null on: " + $l)
            }
        }
    }
}

Test-Case 'toolkit: both scripts pass "sh -n" (parse only, nothing is executed)' {
    $sh = $null
    foreach ($c in @('C:\Program Files\Git\usr\bin\sh.exe', 'C:\Program Files\Git\bin\sh.exe')) { if (Test-Path -LiteralPath $c) { $sh = $c; break } }
    if (-not $sh) { Write-Host '      (skipped: no sh.exe available)'; return }
    $texts = @($script:NvidiaToolkitCheckScript, $script:NvidiaToolkitInstallScript)
    $files = @()
    $n = 0
    foreach ($t in $texts) { $n++; $f = Join-Path $script:TestDir ('toolkit{0}.sh' -f $n); [System.IO.File]::WriteAllText($f, $t, (New-Object System.Text.UTF8Encoding($false))); $files += $f }
    Use-RealExternal; Use-RealClock
    foreach ($f in $files) {
        $r = Invoke-External -FilePath $sh -Arguments @('-n', ($f -replace '\\', '/')) -TimeoutSec 60
        Assert-Equal 0 $r.ExitCode ("does not parse: " + $r.Stderr)
    }
}

# ---- Test-NvidiaToolkit / Install-NvidiaToolkit / Invoke-NvidiaToolkitStep -------------------------------
function Add-ToolkitFakes {
    # CheckExit/InstallExit: the exit code the fake distro gives each script. The two are told apart by what
    # the script reads on stdin, as in a real run. $script:InstallRan counts install-script runs.
    param([int]$CheckExit = 0, [int]$InstallExit = 0, [string]$InstallStdout = 'toolkit: installed 1.20.1-1 (was none)', [string]$InstallStderr = '', [bool]$Beats = $false, [bool]$InstallTimedOut = $false)
    $script:CheckExit = $CheckExit; $script:InstallExit = $InstallExit; $script:InstallStdout = $InstallStdout; $script:InstallStderr = $InstallStderr
    $script:BeatsOn = $Beats; $script:InstallTimedOut = $InstallTimedOut
    $script:InstallRan = 0; $script:CheckRan = 0; $script:SeenPollInterval = 0
    Add-ExtRule '-u root --exec sh -s -- ' {
        param($c)
        if ([string]$c.StdinText -match 'apt-get install') {
            $script:InstallRan++
            $script:SeenPollInterval = $c.PollIntervalMs
            if ($script:BeatsOn) { & $c.OnPoll; & $c.OnPoll }
            return (New-ExtResult -ExitCode $script:InstallExit -Stdout $script:InstallStdout -Stderr $script:InstallStderr -TimedOut $script:InstallTimedOut)
        }
        $script:CheckRan++
        if ($script:CheckExit -eq 0) { return (New-ExtResult -ExitCode 0 -Stdout "toolkit: present 1.20.1-1`n") }
        return (New-ExtResult -ExitCode $script:CheckExit -Stdout "toolkit: nvidia-container-toolkit is absent, not 1.20.1-1`n")
    }
}
function Get-NvidiaProgress { return ,@((Get-ProgressObjects) | Where-Object { $_.stage -eq 'nvidia' }) }

Test-Case 'toolkit step (22.5, 22.12 item 6): already present at the pin means one check, NO install, and no progress lines at all' {
    $s = New-TestSettings
    Add-ToolkitFakes -CheckExit 0
    $r = Invoke-NvidiaToolkitStep -Settings $s -Caller 'install'
    Assert-True $r.Ok 'ok'
    Assert-False $r.Ran 'no install ran'
    Assert-Equal 1 $script:CheckRan 'one check'
    Assert-Equal 0 $script:InstallRan 'no install call'
    Assert-Equal 0 (Get-NvidiaProgress).Count 'a user is not shown work that did not happen'
    $call = $script:ExtCalls[0]
    Assert-Equal 'wsl.exe -d Cognita -u root --exec sh -s -- 1.20.1-1' $call.Line 'as root, the pin passed as $1'
    Assert-NotMatch ([string]$call.StdinText) '1\.20\.1' 'the pin is not interpolated into the script'
    Assert-Match (Get-LogText) 'nvidia toolkit: check pin=\[1\.20\.1-1\] present=True exit=0 .* line=\[toolkit: present 1\.20\.1-1\]' 'check logged with values'
    Assert-Match (Get-LogText) 'nvidia toolkit step \(install\): already present at the pin; no install' 'decision logged'
}

Test-Case 'toolkit step (22.5): not present means the install runs as root with the pin as $1; start, a heartbeat every 5 s, then done' {
    $s = New-TestSettings
    Add-ToolkitFakes -CheckExit 1 -Beats $true
    $r = Invoke-NvidiaToolkitStep -Settings $s -Caller 'update'
    Assert-True $r.Ok 'ok'
    Assert-True $r.Ran 'ran'
    Assert-Equal 1 $script:InstallRan 'one install'
    Assert-Equal 5000 $script:SeenPollInterval 'the heartbeat interval is 5 s'
    $calls = (Get-ExtCallsMatching 'sh -s')
    Assert-Equal 2 $calls.Count 'check then install'
    foreach ($c in $calls) { Assert-Equal 'wsl.exe -d Cognita -u root --exec sh -s -- 1.20.1-1' $c.Line 'both as root with the pin' }
    $p = Get-NvidiaProgress
    Assert-Equal 'start,progress,progress,done' (($p | ForEach-Object { $_.state }) -join ',') 'start, two heartbeats, done'
    Assert-Equal 'NVIDIA support in Cognita''s Linux' $p[0].title 'title'
    Assert-Equal 'Still working.' $p[1].message 'heartbeat text'
    Assert-Match (Get-LogText) 'nvidia toolkit: install pin=\[1\.20\.1-1\] exit=0 .* line=\[toolkit: installed 1\.20\.1-1 \(was none\)\]' 'install logged with values'
}

Test-Case 'toolkit step (22.12 items 3, 13): a rollback (exit 3) is a warning with the design wording and fix, never a stop; no done line' {
    $s = New-TestSettings
    Add-ToolkitFakes -CheckExit 1 -InstallExit 3 -InstallStdout "toolkit: rolled back`n"
    $r = Invoke-NvidiaToolkitStep -Settings $s -Caller 'install'
    Assert-False $r.Ok 'not ok'
    Assert-True $r.Ran 'ran'
    $p = Get-NvidiaProgress
    Assert-Equal 'start,warning' (($p | ForEach-Object { $_.state }) -join ',') 'start then the warning'
    Assert-Equal "Setup could not install NVIDIA's container support in Cognita's Linux (Docker did not start with it, so the change was undone). If Cognita cannot use the card without it, it uses the CPU." $p[1].message 'message'
    Assert-Equal 'Run Setup again later.' $p[1].fix 'fix'
    Assert-Match (Get-LogText) 'nvidia toolkit: install pin=\[1\.20\.1-1\] exit=3 .* line=\[toolkit: rolled back\]' 'logged'
}

Test-Case 'toolkit step (15.1.0 review): the rollback line names its step, and says when Docker still does not answer' {
    $s = New-TestSettings
    Add-ToolkitFakes -CheckExit 1 -InstallExit 3 -InstallStdout "toolkit: rolled back (nvidia-ctk failed)`n"
    [void](Invoke-NvidiaToolkitStep -Settings $s -Caller 'install')
    $p = Get-NvidiaProgress
    Assert-Match $p[-1].message '\(registering the nvidia runtime with Docker failed, so the change was undone\)' 'nvidia-ctk step'
    $script:Out.Clear()
    Add-ToolkitFakes -CheckExit 1 -InstallExit 3 -InstallStdout "toolkit: rolled back (docker did not answer), and docker still does not answer`n"
    [void](Invoke-NvidiaToolkitStep -Settings $s -Caller 'install')
    $p = Get-NvidiaProgress
    Assert-Match $p[-1].message '\(Docker did not start with it; the change was undone, but Docker still does not answer\)' 'Docker still down'
}

Test-Case 'toolkit step (22.5): any other failure, a timeout and a start error each end in the warning with the cause; nothing throws' {
    $s = New-TestSettings
    Add-ToolkitFakes -CheckExit 1 -InstallExit 100 -InstallStderr "E: Unable to locate package`nE: more`n"
    $r = Invoke-NvidiaToolkitStep -Settings $s -Caller 'install'
    Assert-False $r.Ok 'not ok'
    $w = @((Get-NvidiaProgress) | Where-Object { $_.state -eq 'warning' })
    Assert-Equal 1 $w.Count 'one warning'
    Assert-Match $w[0].message 'in Cognita''s Linux \(exit 100: E: Unable to locate package E: more\)\. If Cognita cannot use the card without it, it uses the CPU\.$' 'exit code and the stderr tail, on one line'
    $script:ExtRules.Clear(); $script:Out.Clear()
    Add-ToolkitFakes -CheckExit 1 -InstallExit 1 -InstallTimedOut $true
    [void](Invoke-NvidiaToolkitStep -Settings $s -Caller 'install')
    Assert-Match (((Get-NvidiaProgress) | Where-Object { $_.state -eq 'warning' }).message) '\(it did not finish in 15 minutes\)' 'timeout'
    $script:ExtRules.Clear(); $script:Out.Clear()
    Add-ExtRule '-u root --exec sh -s -- ' { param($c) if ([string]$c.StdinText -match 'apt-get install') { New-ExtResult -ExitCode -1 -StartError 'wsl.exe could not start' } else { New-ExtResult -ExitCode 1 } }
    [void](Invoke-NvidiaToolkitStep -Settings $s -Caller 'install')
    Assert-Match (((Get-NvidiaProgress) | Where-Object { $_.state -eq 'warning' }).message) '\(wsl\.exe could not start\)' 'start error'
}

Test-Case 'toolkit step (22.5): a check that cannot even run counts as not present, and an install call that throws is still only a warning' {
    $s = New-TestSettings
    # No rule at all: the fake external throws "Unexpected external call" for both calls.
    $r = Invoke-NvidiaToolkitStep -Settings $s -Caller 'update'
    Assert-False $r.Ok 'not ok'
    Assert-Equal 1 @((Get-NvidiaProgress) | Where-Object { $_.state -eq 'warning' }).Count 'a warning, not an exception'
    Assert-Match (Get-LogText) 'nvidia toolkit: check failed \(Unexpected external call' 'the swallowed check exception is logged'
    Assert-Match (Get-LogText) 'nvidia toolkit: install threw: Unexpected external call' 'and the install one'
}

Complete-Tests
