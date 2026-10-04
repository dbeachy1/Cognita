# test_core.ps1 - output contract, clock and waits, arguments, settings.json, logging (design 5.1, 3.1, 14.2)
. (Join-Path $PSScriptRoot '..\CognitaWin.ps1') -NoMain
. (Join-Path $PSScriptRoot '_harness.ps1')

Test-Case 'progress line: exact fields, in order, JSON that parses' {
    Use-FakeClock ([DateTime]'2026-03-01T09:30:15')
    $j = Format-ProgressJson -Stage 'images' -Title 'Downloading' -State 'progress' -BytesDone 1048576 -BytesTotal 4194304 -Message '' -Fix ''
    $o = $j | ConvertFrom-Json
    Assert-Equal 1 $o.schema 'schema'
    Assert-Equal '2026-03-01T09:30:15' $o.time 'time is local ISO from the clock'
    Assert-Equal 'images' $o.stage 'stage'
    Assert-Equal 'progress' $o.state 'state'
    Assert-Equal 1048576 $o.bytes_done 'bytes_done'
    Assert-Equal 4194304 $o.bytes_total 'bytes_total'
    Assert-Match $j '^\{"schema": 1, "time": "[^"]+", "stage": "images", "title": "Downloading", "state": "progress", "bytes_done": 1048576, "bytes_total": 4194304\}$' 'key order and shape'
}

Test-Case 'progress line: failed carries message and fix; no bytes keys when not a download' {
    $j = Format-ProgressJson -Stage 'check.disk' -Title 'Free disk space' -State 'failed' -BytesDone $null -BytesTotal $null -Message 'Setup needs 40 GB free on C:\.' -Fix 'Free space.'
    $o = $j | ConvertFrom-Json
    Assert-Equal 'failed' $o.state 'state'
    Assert-Equal 'Setup needs 40 GB free on C:\.' $o.message 'message'
    Assert-Equal 'Free space.' $o.fix 'fix'
    Assert-False ($j -match 'bytes_') 'no bytes keys'
}

Test-Case 'Setup locale warning: display fields localize while diagnostic fields stay English' {
    $oldLocale = $env:COGNITA_LANG
    try {
        $env:COGNITA_LANG = 'es-ES'
        Set-HelperLocale
        $j = Format-ProgressJson -Stage 'wsl' -Title 'Turning on WSL' -State 'failed' -BytesDone $null -BytesTotal $null `
            -Message 'Setup needs your permission once to turn on WSL.' -Fix 'Run Setup again when you are ready.' `
            -MessageId 'wsl.permission.warning'
        $o = $j | ConvertFrom-Json
        Assert-Equal 'wsl' $o.stage 'machine stage stays stable'
        Assert-Equal 'failed' $o.state 'machine result stays stable'
        Assert-Equal 'Setup needs your permission once to turn on WSL.' $o.message 'English diagnostic message'
        Assert-Equal 'Run Setup again when you are ready.' $o.fix 'English diagnostic fix'
        Assert-Equal ('"El programa de instalaci\u00f3n necesita su permiso una vez para activar WSL."' | ConvertFrom-Json) $o.message_display 'Spanish presentation'
        Assert-Equal ('"Vuelva a ejecutar el programa de instalaci\u00f3n cuando est\u00e9 listo."' | ConvertFrom-Json) $o.fix_display 'Spanish recovery guidance'

        $j = Format-ProgressJson -Stage 'wsl' -Title 'Updating WSL' -State 'failed' -BytesDone $null -BytesTotal $null `
            -Message 'wsl --update failed (exit 5).' -Fix 'Run Setup again. If it keeps failing, use Save diagnostics.' `
            -MessageId 'wsl.update.failed' -Values @{ exit_code = 5 }
        $o = $j | ConvertFrom-Json
        Assert-Equal 'wsl' $o.stage 'second machine stage stays stable'
        Assert-Equal 'wsl --update failed (exit 5).' $o.message 'second English diagnostic message'
        Assert-Equal ('"No se pudo actualizar WSL (c\u00f3digo de salida 5)."' | ConvertFrom-Json) $o.message_display 'distinct same-stage Spanish failure'
    } finally { $env:COGNITA_LANG = $oldLocale; Set-HelperLocale }
}

Test-Case 'Setup relay: two acceleration outcomes share a stage but use distinct localized IDs' {
    $oldLocale = $env:COGNITA_LANG
    try {
        $env:COGNITA_LANG = 'es-ES'
        Set-HelperLocale
        Write-RelayedLine '{"schema":1,"stage":"acceleration","state":"warning","title":"Acceleration","message":"The requested NVIDIA GPU is unavailable (the NVIDIA driver is older than 580), so Cognita will use the CPU.","fix":"Run Setup again later.","presentation_id":"setup.acceleration.fallback_unavailable","presentation_values":{"vendor":"NVIDIA","reason":"driver_too_old"}}'
        Write-RelayedLine '{"schema":1,"stage":"acceleration","state":"failed","title":"Acceleration","message":"The NVIDIA GPU could not be verified. Cognita will use the CPU.","fix":"Run Setup again later.","presentation_id":"setup.acceleration.verification_failed","presentation_values":{"vendor":"NVIDIA"}}'
        $p = Get-ProgressObjects
        Assert-Equal 'acceleration' $p[0].stage 'stage stays stable'
        Assert-Equal 'The requested NVIDIA GPU is unavailable (the NVIDIA driver is older than 580), so Cognita will use the CPU.' $p[0].message 'English diagnostic message remains intact'
        Assert-Equal ('"La GPU NVIDIA solicitada no est\u00e1 disponible (el controlador NVIDIA es anterior a la versi\u00f3n 580), as\u00ed que Cognita usar\u00e1 la CPU."' | ConvertFrom-Json) $p[0].message_display 'reason enum is localized'
        Assert-Equal ('"No se pudo verificar la GPU NVIDIA. Cognita usar\u00e1 la CPU."' | ConvertFrom-Json) $p[1].message_display 'same stage uses the second message ID'
    } finally { $env:COGNITA_LANG = $oldLocale; Set-HelperLocale }
}

Test-Case 'Setup relay: unknown failure gets a localized summary and an English technical detail' {
    $oldLocale = $env:COGNITA_LANG
    try {
        $env:COGNITA_LANG = 'fr-FR'
        Set-HelperLocale
        Write-RelayedLine '{"schema":1,"stage":"folder","state":"failed","title":"Projects folder","message":"mount failed (exit 7)","fix":"Run Setup again."}'
        $p = (Get-ProgressObjects)[0]
        Assert-Equal 'mount failed (exit 7)' $p.message 'diagnostic detail remains English'
        Assert-Equal ('"D\u00e9tail technique en anglais : mount failed (exit 7)"' | ConvertFrom-Json) $p.message_display 'localized generic failure includes labeled detail'
        Assert-Match $p.title_display 'pas pu terminer cette .tape' 'localized summary'
        Write-RelayedLine '{"schema":1,"stage":"checks","state":"warning","title":"Unrecognized warning","message":"The optional component was skipped."}'
        $p = (Get-ProgressObjects)[1]
        Assert-Equal ('"D\u00e9tail technique en anglais : The optional component was skipped."' | ConvertFrom-Json) $p.message_display 'unknown warning detail is also explicitly labeled'
    } finally { $env:COGNITA_LANG = $oldLocale; Set-HelperLocale }
}

Test-Case 'Setup relay: known Linux stages show localized titles with English diagnostics intact' {
    $oldLocale = $env:COGNITA_LANG
    try {
        $env:COGNITA_LANG = 'es-ES'
        Set-HelperLocale
        Write-RelayedLine '{"schema":1,"stage":"images","state":"progress","title":"Downloading Cognita","message":"Pulling image"}'
        $p = (Get-ProgressObjects)[0]
        Assert-Equal 'Downloading Cognita' $p.title 'English machine title remains available'
        Assert-Equal 'Descargando Cognita' $p.title_display 'Setup stage title is localized'
        Assert-Equal 'Pulling image' $p.message 'technical progress text remains intact'
        Assert-Equal ('"Cognita est\u00e1 trabajando en este paso."' | ConvertFrom-Json) $p.message_display 'technical progress detail is summarized in Spanish'
        Write-RelayedLine '{"schema":1,"stage":"proof","state":"done","title":"Self-tests skipped","message":"The self-tests were stopped at your request. Run Setup again later."}'
        $p = (Get-ProgressObjects)[1]
        Assert-Equal 'Pruebas omitidas' $p.title_display 'known title override is localized'
        Assert-Equal ('"Detuvo las pruebas. Vuelva a ejecutar el programa de instalaci\u00f3n m\u00e1s tarde para realizarlas."' | ConvertFrom-Json) $p.message_display 'self-test outcome and next step are localized'
    } finally { $env:COGNITA_LANG = $oldLocale; Set-HelperLocale }
}

Test-Case 'Setup helper: successful progress titles use existing stage names and retain English fields' {
    $oldLocale = $env:COGNITA_LANG
    try {
        $env:COGNITA_LANG = 'es-ES'
        Set-HelperLocale
        Write-ProgressLine -Stage 'import' -Title 'Setting up Cognita''s Linux' -State 'start'
        Write-ProgressLine -Stage 'check.disk' -Title 'Free disk space' -State 'done'
        $p = Get-ProgressObjects
        Assert-Equal 'import' $p[0].stage 'existing stable helper stage is preserved'
        Assert-Equal 'Setting up Cognita''s Linux' $p[0].title 'English diagnostic title remains available'
        Assert-Equal 'Configurando el entorno Linux de Cognita' $p[0].title_display 'successful helper start is localized'
        Assert-Equal 'Comprobando este equipo' $p[1].title_display 'existing per-check substage uses the stable check title'
    } finally { $env:COGNITA_LANG = $oldLocale; Set-HelperLocale }
}

Test-Case 'folder validation: known bounded reasons get localized display and preserve English reason' {
    $oldLocale = $env:COGNITA_LANG
    try {
        $env:COGNITA_LANG = 'es-ES'
        Set-HelperLocale
        $empty = Test-RootPath -Path '' -Settings $null
        Assert-Equal 'setup.folder.choose' $empty.ReasonId 'empty path has stable presentation ID'
        $lineBreak = Test-RootPath -Path "C:\\bad`nfolder" -Settings $null
        Assert-Equal 'setup.folder.control_character' $lineBreak.ReasonId 'control character has stable presentation ID'
        $display = Get-LocalizedFolderReason -PresentationId $lineBreak.ReasonId -Reason $lineBreak.Reason
        Assert-Equal ('"La ruta de la carpeta contiene un salto de l\u00ednea, una tabulaci\u00f3n o un car\u00e1cter de control. Elija otra carpeta o cambie su nombre."' | ConvertFrom-Json) $display 'specific Spanish guidance'
        $line = Format-ResultLine 'ok' ([ordered]@{ ok = 0; reason = $lineBreak.Reason; presentation_id = $lineBreak.ReasonId; reason_display = $display })
        Assert-Match $line '^result=ok;' 'machine result stays stable'
        Assert-True ($line.Contains('reason=' + $lineBreak.Reason)) 'English diagnostic reason stays stable'
        Assert-True ($line.Contains('presentation_id=' + $lineBreak.ReasonId)) 'presentation ID is carried'
        Assert-True ($line.Contains('reason_display=' + $display)) 'localized text is available to Setup'
    } finally { $env:COGNITA_LANG = $oldLocale; Set-HelperLocale }
}

Test-Case 'folder validation: unknown reason uses localized generic text with labeled English detail' {
    $oldLocale = $env:COGNITA_LANG
    try {
        $env:COGNITA_LANG = 'fr-FR'
        Set-HelperLocale
        $display = Get-LocalizedFolderReason -PresentationId 'setup.generic.failure' -Reason 'unexpected path parser detail'
        Assert-True ($display.Contains('en anglais : unexpected path parser detail')) 'unknown detail has an explicit localized label'
    } finally { $env:COGNITA_LANG = $oldLocale; Set-HelperLocale }
}

Test-Case 'unsupported Setup locale falls back to English' {
    $oldLocale = $env:COGNITA_LANG
    try {
        $env:COGNITA_LANG = 'pt-PT'
        Set-HelperLocale
        Assert-Equal 'en-US' $script:Locale 'unsupported locale is rejected'
        $j = Format-ProgressJson -Stage 'wsl' -Title 'Turning on WSL' -State 'failed' -BytesDone $null -BytesTotal $null `
            -Message 'English source message' -Fix 'English source fix' -MessageId 'wsl.permission.warning'
        $o = $j | ConvertFrom-Json
        Assert-Equal 'Setup needs your permission once to turn on WSL.' $o.message_display 'fallback presentation is English'
    } finally { $env:COGNITA_LANG = $oldLocale; Set-HelperLocale }
}

Test-Case 'JSON escaping: quote, backslash, apostrophe stay readable, control chars escaped, non-ASCII raw' {
    $e = Get-Utf8 0xE9, 0x20AC
    $j = Format-ProgressJson -Stage 's' -Title "it's" -State 'warning' -BytesDone $null -BytesTotal $null -Message ("a`"b\c`nline2 $e") -Fix ''
    Assert-False ($j -match '\\u0027') 'apostrophe not escaped as \u0027'
    $o = $j | ConvertFrom-Json
    Assert-Equal "it's" $o.title 'title'
    Assert-Equal ("a`"b\c`nline2 $e") $o.message 'message round trip'
}

Test-Case 'progress line goes to stdout as one line (machine mode) and is logged' {
    Write-ProgressLine -Stage 'import' -Title 'Setting up' -State 'start'
    $lines = Get-OutLines
    Assert-Equal 1 $lines.Count 'one stdout line'
    Assert-True ($lines[0].StartsWith('{') -and $lines[0].EndsWith('}')) 'a JSON object'
    Assert-Match (Get-LogText) 'progress stage=import state=start' 'logged'
}

Test-Case 'human mode prints text, never JSON' {
    $script:HumanMode = $true
    Write-ProgressLine -Stage 'x' -Title 'Connecting your projects folder' -State 'failed' -Message 'boom' -Fix 'fix it'
    Write-InfoLine 'hello'
    $lines = Get-OutLines
    Assert-True ($lines -contains 'hello') 'info line printed as is'
    Assert-False (($lines | Where-Object { $_.StartsWith('{') }).Count -gt 0) 'no JSON in human mode'
    Assert-True ((($lines -join "`n") -match 'FAILED') -and (($lines -join "`n") -match 'fix it')) 'failure text and fix shown'
}

Test-Case 'relayed Linux line is written unchanged' {
    $line = '{"schema": 1, "time": "2026-03-01T09:00:00", "stage": "images", "title": "Pulling images", "state": "progress", "bytes_done": 5, "bytes_total": 10}'
    Write-RelayedLine $line
    Assert-Equal $line (Get-OutLines)[0] 'byte for byte'
}

Test-Case 'result line: format, sanitizing, exit codes' {
    Assert-Equal 'result=ok;wsl=present;distro=absent' (Format-ResultLine 'ok' ([ordered]@{ wsl = 'present'; distro = 'absent' })) 'ok'
    Assert-Equal 'result=failed;reason=a b%3Bc' (Format-ResultLine 'failed' ([ordered]@{ reason = "a`nb;c" })) 'newline becomes a space, semicolon is percent-encoded (design 18.5)'
    Assert-Equal 'result=restart-required' (Format-ResultLine 'restart-required' $null) 'no keys'
    Assert-Equal 0 (Get-ExitCodeForStatus 'ok') 'ok'
    Assert-Equal 1 (Get-ExitCodeForStatus 'failed') 'failed'
    Assert-Equal 3010 (Get-ExitCodeForStatus 'restart-required') 'restart'
}

# What Setup does with a result line (design 18.5): split on ; then on the first =, then decode %3B and %25.
# Decoded in ONE pass over "%25" and "%3B" so that a literal "%3B" typed by a user survives the round trip.
function ConvertFrom-TestResultLine {
    param([string]$Line)
    $h = [ordered]@{}
    $parts = $Line -split ';'
    $h['result'] = $parts[0].Substring('result='.Length)
    foreach ($p in $parts[1..($parts.Count - 1)]) {
        $i = $p.IndexOf('=')
        $raw = $p.Substring($i + 1)
        $h[$p.Substring(0, $i)] = [regex]::Replace($raw, '%(25|3B)', { param($m) if ($m.Groups[1].Value -eq '25') { '%' } else { ';' } })
    }
    return $h
}

Test-Case 'result values: % becomes %25 and ; becomes %3B, in one function, and a value with both round-trips' {
    Assert-Equal '100%25' (Format-ResultValue '100%') 'percent'
    Assert-Equal 'a%3Bb' (Format-ResultValue 'a;b') 'semicolon'
    Assert-Equal '%253B' (Format-ResultValue '%3B') 'a literal %3B is encoded (percent first), so it is not read back as a semicolon'
    Assert-Equal 'a b' (Format-ResultValue "a`r`nb") 'line breaks still become one space'
    Assert-Equal '' (Format-ResultValue $null) 'null'
    $path = 'C:\Users\me\Docs;2024\50% done'
    $line = Format-ResultLine 'ok' ([ordered]@{ ok = 1; path = $path; reason = 'x;y%3Bz' })
    Assert-Equal 4 @($line -split ';').Count 'the line has exactly result plus the three keys (no stray split)'
    $back = ConvertFrom-TestResultLine $line
    Assert-Equal $path $back['path'] 'the path with ; and % is read back exactly'
    Assert-Equal 'x;y%3Bz' $back['reason'] 'a literal %3B in a value survives too'
    Assert-Equal '1' $back['ok'] 'plain values unchanged'
}

Test-Case 'result values: EVERY verb result goes through the one encoder (an invalid folder with a semicolon in its reason)' {
    $line = Format-ResultLine 'ok' ([ordered]@{ ok = 0; reason = 'The folder C:\a;b does not exist; sorry.' })
    Assert-NotMatch $line 'does not exist;' 'no raw semicolon inside a value'
    Assert-Match $line 'C:\\a%3Bb does not exist%3B sorry\.$' 'encoded'
}

Test-Case 'every verb ends with the result line and maps the exit code (roots --validate)' {
    $d = New-Dir 'projects'
    $code = Invoke-HelperMain -Argv @('roots', '--validate', $d)
    Assert-Equal 0 $code 'exit code ok'
    $last = (Get-OutLines)[-1]
    Assert-Match $last '^result=ok;ok=1;' 'last line is the result'
    $script:Out.Clear()
    $code = Invoke-HelperMain -Argv @('roots', '--validate', 'C:\')
    Assert-Equal 0 $code 'an invalid folder is a normal answer: exit 0 (design 18.5)'
    Assert-Match ((Get-OutLines)[-1]) '^result=ok;ok=0;reason=Choose a folder, not a whole drive' 'ok=0 and the reason on a result=ok line'
    $script:Out.Clear()
    $code = Invoke-HelperMain -Argv @('roots')
    Assert-Equal 1 $code 'no --validate at all is a usage error, which stays failed'
    Assert-Match ((Get-OutLines)[-1]) '^result=failed;error=usage' 'usage error'
}

Test-Case 'unknown verb and a verb that throws both end with result=failed and exit 1' {
    Assert-Equal 1 (Invoke-HelperMain -Argv @('nonsense')) 'unknown verb'
    Assert-Match ((Get-OutLines)[-1]) '^result=failed;error=unknown verb' 'unknown'
    $script:Out.Clear()
    function Invoke-StateVerb { param($Opts) throw 'kaboom' }
    Assert-Equal 1 (Invoke-HelperMain -Argv @('state')) 'throwing verb'
    Assert-Match ((Get-OutLines)[-1]) '^result=failed;error=kaboom' 'exception on the result line'
    Assert-Match (Get-LogText) 'UNHANDLED in verb state: kaboom' 'logged with the stack'
}

Test-Case 'argument parser: --name value, --name=value, flags, positionals' {
    $p = ConvertFrom-HelperArgs @('--phase', 'final', '--data-dir=C:\a b', '--now', '--keep-data', 'POS', '--flag-last')
    Assert-Equal 'final' $p.Opts['phase'] 'phase'
    Assert-Equal 'C:\a b' $p.Opts['data-dir'] 'name=value'
    Assert-True $p.Opts['now'] 'boolean flag'
    Assert-True $p.Opts['keep-data'] 'boolean flag 2'
    Assert-Equal 'POS' $p.Positional[0] 'positional'
    Assert-True $p.Opts['flag-last'] 'trailing unknown flag is true'
}

Test-Case 'Wait-Until: returns true as soon as the condition holds' {
    $script:n = 0
    $ok = Wait-Until -Condition { $script:n++; $script:n -ge 3 } -TimeoutSec 60 -IntervalMs 1000 -Description 'three polls'
    Assert-True $ok 'true'
    Assert-Equal 2000 $script:SleepTotalMs 'two sleeps of 1000 ms on the fake clock'
}

Test-Case 'Wait-Until: times out on the fake clock after exactly the timeout' {
    $start = Get-ClockNow
    $ok = Wait-Until -Condition { $false } -TimeoutSec 45 -IntervalMs 1000 -Description 'never'
    Assert-False $ok 'false on timeout'
    Assert-Equal 45 ([int]((Get-ClockNow) - $start).TotalSeconds) 'elapsed clock time'
    Assert-Match (Get-LogText) 'wait TIMED OUT: never after 45' 'the timeout is logged with its length'
}

Test-Case 'Wait-Until: a progress line at least every 5 seconds during a long wait' {
    [void](Wait-Until -Condition { $false } -TimeoutSec 32 -IntervalMs 1000 -Description 'long' -Stage 'keepalive' -Title 'Waiting')
    $all = Get-ProgressObjects
    $prog = @($all | Where-Object { $_.stage -eq 'keepalive' })
    Assert-True ($prog.Count -ge 6) ("expected >= 6 heartbeat lines in 32 s, got {0}" -f $prog.Count)
    $times = @($prog | ForEach-Object { [DateTime]$_.time })
    for ($i = 1; $i -lt $times.Count; $i++) {
        Assert-True (($times[$i] - $times[$i - 1]).TotalSeconds -le 5.01) 'gap between heartbeats is at most 5 s'
    }
}

Test-Case 'Wait-Until: a condition that throws is not yet, and the exception is logged' {
    $script:m = 0
    $ok = Wait-Until -Condition { $script:m++; if ($script:m -lt 3) { throw 'not ready' }; $true } -TimeoutSec 30 -IntervalMs 500 -Description 'flaky'
    Assert-True $ok 'true once it stops throwing'
    Assert-Match (Get-LogText) 'wait condition threw \(treated as not yet\): not ready' 'swallowed exception logged'
}

Test-Case 'settings.json: first write moves the temp file, second replaces; atomic, valid, no secrets' {
    $path = Get-SettingsPath
    Assert-False (Test-Path $path) 'starts absent'
    $s = New-Settings -VhdDir (Join-Path $script:TestDir 'vhd')
    Save-Settings $s
    Assert-True (Test-Path $path) 'written'
    Assert-False (Test-Path ($path + '.tmp')) 'temp file gone after the first write'
    Assert-Match (Get-LogText) 'settings saved \(first write, move\)' 'first write is a move'
    Set-SettingProp $s 'state' 'installed'
    Set-SettingProp $s 'mcp_port' 9000
    Save-Settings $s
    Assert-False (Test-Path ($path + '.tmp')) 'temp file gone after the replace'
    Assert-Match (Get-LogText) 'settings saved \(replace\)' 'second write is a replace'
    $r = Read-Settings
    Assert-Equal 'installed' $r.state 'state persisted'
    Assert-Equal 9000 $r.mcp_port 'port persisted'
    Assert-Equal 1 $r.schema 'schema'
    Assert-Equal 0 @($r.roots).Count 'roots is an empty array'
    Assert-True ($null -eq $r.funnel) 'funnel null'
    Assert-True ($null -eq $r.resume) 'resume null'
    $raw = [System.IO.File]::ReadAllText($path)
    Assert-NotMatch $raw '(?i)password|token|secret' 'no secrets in settings.json'
    Assert-Match $raw '"distro":\s*"Cognita"' 'distro key shape the keepalive script reads'
    Assert-Match $raw '"linux_user":\s*"cognita"' 'linux_user key shape the keepalive script reads'
}

Test-Case 'settings.json: a one-element roots list stays a list through save and read' {
    $s = New-TestSettings -RootPaths @('C:\Docs')
    $r = Read-Settings
    Assert-Equal 1 @(Get-SettingsRoots $r).Count 'one root'
    Assert-Equal 'C:\Docs' (@(Get-SettingsRoots $r))[0].windows 'root path'
    Assert-Match ([System.IO.File]::ReadAllText((Get-SettingsPath))) '"roots":\s*\[' 'serialized as a JSON array'
}

Test-Case 'log: local time, one file per run, name helper-<stamp>-<pid>.log, never a password' {
    Use-RealClock
    Assert-Equal 'Local' ((Get-ClockNow).Kind.ToString()) 'the real clock is local time'
    Use-FakeClock ([DateTime]'2026-03-01T09:30:15')
    Start-HelperLog -VerbName 'state'
    Write-Log 'hello'
    Assert-Equal ('helper-20260301-093015-{0}.log' -f $PID) (Split-Path -Leaf $script:LogFile) ("log file name carries the stamp and this process's PID: {0}" -f $script:LogFile)
    Assert-True ($script:LogFile -like '*\logs\helper-20260301-093015-*.log') 'in the logs folder'
    $text = [System.IO.File]::ReadAllText($script:LogFile)
    Assert-Match $text '^2026-03-01 09:30:15\.000 \[helper\] helper start verb=state' 'stamp is the local clock'
    Assert-Match $text 'hello' 'message written'
}

Test-Case 'ConvertTo-ArgString: quoting rules' {
    Assert-Equal 'a b' (ConvertTo-ArgString @('a', 'b')) 'plain'
    Assert-Equal '"a b" c' (ConvertTo-ArgString @('a b', 'c')) 'space quoted'
    Assert-Equal '"a\"b"' (ConvertTo-ArgString @('a"b')) 'embedded quote'
    Assert-Equal '"C:\my dir\\"' (ConvertTo-ArgString @('C:\my dir\')) 'trailing backslash doubled inside quotes'
    Assert-Equal '""' (ConvertTo-ArgString @('')) 'empty argument'
}

Test-Case 'Limit-LogText keeps head and tail' {
    $t = ('a' * 100) + ('b' * 100)
    $l = Limit-LogText $t 60
    Assert-True ($l.StartsWith('a' * 20)) 'head kept'
    Assert-True ($l.EndsWith('b' * 40)) 'tail kept'
    Assert-Match $l '\.\.\.\[140 chars omitted\]\.\.\.' 'says how much was omitted'
}

Complete-Tests
