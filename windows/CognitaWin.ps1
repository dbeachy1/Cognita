<#
CognitaWin.ps1 - the Windows-side helper for the Cognita installer.

Design: docs/DESIGN-WINDOWS-INSTALLER.md (section 5 the helper, 7 flows, 8 the cognita
command, 9 remote access, 10 diagnostics, 14.2 tests). Runs on Windows PowerShell 5.1
(no PowerShell 7 syntax, no modules, no downloads of PowerShell code). This file is pure
ASCII on purpose: a UTF-8 file without a BOM that holds non-ASCII text is read as ANSI by
PowerShell 5.1. Build a non-ASCII character with [char]0x.... instead.

Usage:  CognitaWin.ps1 <verb> [--option value ...]
Verbs:  state, check, wsl-install, restart-for-wsl, roots, install, update, add-folder,
        remote-access, start, stop, restart, status, diagnostics, uninstall,
        password-broker, cli

Output contract (design 5.1):
  - stdout: one JSON progress object per line (schema, time, stage, title, state,
    bytes_done, bytes_total, message, fix), the Linux CLI's schema, and the Linux CLI's own
    progress lines relayed unchanged. The LAST stdout line is
    result=ok|failed|restart-required;key=value;...   Exit code 0, 1 or 3010.
    (The cli verb is the exception: it prints plain text for a person and no result line.)
  - log: %LOCALAPPDATA%\Cognita\logs\helper-<yyyyMMdd-HHmmss>-<pid>.log in local time (the PID
    keeps two helpers started in the same second from sharing a file, design 18.5).
    Never a password, token or document content.

Test seam (design 14.2): dot-source with -NoMain, then replace Invoke-External,
$script:Clock and the small reader/writer functions (Get-RegistryValue, Get-Memory..., and
so on) and call the functions directly. Every external process goes through
Invoke-External; every wait goes through Wait-Until and $script:Clock.

Deviations from the letter of the design are marked "DESIGN NOTE" where they occur.
#>
# A plain param block on purpose: no [Parameter()] attribute, so this is not an "advanced" script
# and PowerShell adds no common parameters. With them, `--in` and `--out` (password-broker) are
# read as abbreviations of -InformationAction / -OutVariable and rejected as ambiguous. Everything
# after the verb lands in $args untouched.
param(
    [string]$Verb,
    [switch]$NoMain
)

# ---------------------------------------------------------------------------------------
# Constants and script state
# ---------------------------------------------------------------------------------------
$script:HelperDir = $PSScriptRoot
$script:DefaultDistro = 'Cognita'
$script:DefaultLinuxUser = 'cognita'
$script:LinuxCliPath = '/usr/local/bin/cognita'
$script:KeepalivePath = '/usr/local/libexec/cognita-keepalive'
$script:RootsMountBase = '/mnt/cognita-roots'
$script:TaskName = 'Cognita'
$script:ReleasesUrl = 'https://github.com/dbeachy1/Cognita/releases'
$script:TailscaleMsiUrl = 'https://pkgs.tailscale.com/stable/tailscale-setup-latest-amd64.msi'
# Design 22.5: the ONE NVIDIA Container Toolkit version Cognita pins. containers/wsl/build-wsl-image.sh
# pins the same version, key URL and list URL for the image; tests/test_wsl_image_recipe.py greps both
# files and keeps them equal (it greps THIS line, so keep its exact form). Never generates a CDI spec (it would embed this host's driver folder).
$script:NvidiaToolkitVersion = '1.20.1-1'
$script:NvidiaKeyUrl = 'https://nvidia.github.io/libnvidia-container/gpgkey'
$script:NvidiaListUrl = 'https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list'
# The four packages the pin covers: the -base package is the toolkit's own dependency at the same
# version; it is named and held too so nothing can move it apart from the other three.
$script:NvidiaToolkitPackages = @('nvidia-container-toolkit', 'nvidia-container-toolkit-base', 'libnvidia-container1', 'libnvidia-container-tools')
$script:HumanMode = $false          # true for the cli verb: text for a person, no JSON, no result line
$script:StdoutWriter = $null
$script:LogFile = $null
$script:LogLines = New-Object System.Collections.ArrayList
$script:LogCap = 20000
$script:CurrentVerb = ''
$script:SupportedLocales = @('en-US', 'es-ES', 'fr-FR', 'de-DE', 'it-IT', 'pt-BR')
$script:Locale = 'en-US'
$script:LocaleCatalog = $null
# Real clock. Tests replace this object with a fake whose Now and Sleep are scriptblocks.
$script:Clock = [pscustomobject]@{
    Now   = { [DateTime]::Now }
    Sleep = { param($Milliseconds) Start-Sleep -Milliseconds $Milliseconds }
}

function Get-ClockNow { return [DateTime](& $script:Clock.Now) }
function Invoke-ClockSleep { param([int]$Milliseconds) & $script:Clock.Sleep $Milliseconds }

# ---------------------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------------------
function Get-DataRoot {
    # COGNITA_HOME exists for tests and for launch-keepalive.vbs, which honors the same variable.
    if ($env:COGNITA_HOME) { return $env:COGNITA_HOME }
    return (Join-Path $env:LOCALAPPDATA 'Cognita')
}
function Get-SettingsPath { return (Join-Path (Get-DataRoot) 'settings.json') }
function Get-LogsDir { return (Join-Path (Get-DataRoot) 'logs') }
function Get-StoppedFlagPath { return (Join-Path (Get-DataRoot) 'stopped') }
# Design 21.3: the flag Setup writes (an empty file) when the user presses "Skip self-tests". The helper
# turns it into the skip file the Linux CLI watches (<progress-file>.skip); Setup owns this one.
function Get-SkipRequestPath { return (Join-Path (Get-DataRoot) 'skip-self-test.request') }
function Get-DefaultVhdDir { return (Join-Path (Get-DataRoot) 'wsl') }
function Get-AppDir { return $script:HelperDir }
function Get-WslExe {
    $p = Join-Path $env:SystemRoot 'System32\wsl.exe'
    if (Test-Path -LiteralPath $p) { return $p }
    return 'wsl.exe'
}

# ---------------------------------------------------------------------------------------
# Logging (design 5.1). Local time. One place; never call it with a password.
# ---------------------------------------------------------------------------------------
function Start-HelperLog {
    param([string]$VerbName)
    $dir = Get-LogsDir
    try {
        if (-not (Test-Path -LiteralPath $dir)) { [void](New-Item -ItemType Directory -Path $dir -Force) }
        $stamp = (Get-ClockNow).ToString('yyyyMMdd-HHmmss')
        # The PID in the name (design 18.5): two helpers started in the same second (Setup starts
        # the password broker and the verb together) must not append to one file.
        $script:LogFile = Join-Path $dir ('helper-{0}-{1}.log' -f $stamp, $PID)
    } catch {
        $script:LogFile = $null
    }
    Write-Log ("helper start verb={0} ps={1} user={2} pid={3}" -f $VerbName, $PSVersionTable.PSVersion, $env:USERNAME, $PID)
}

function Write-Log {
    param([string]$Message)
    $line = '{0} [helper] {1}' -f (Get-ClockNow).ToString('yyyy-MM-dd HH:mm:ss.fff'), $Message
    if ($script:LogLines.Count -lt $script:LogCap) { [void]$script:LogLines.Add($line) }
    if ($script:LogFile) {
        try {
            [System.IO.File]::AppendAllText($script:LogFile, $line + "`n", (New-Object System.Text.UTF8Encoding($false)))
        } catch {
            # A log that cannot be written must not stop the verb; the in-memory copy keeps it.
            [void]$script:LogLines.Add(('{0} [helper] log write failed: {1}' -f (Get-ClockNow).ToString('yyyy-MM-dd HH:mm:ss.fff'), $_.Exception.Message))
        }
    }
}

function Limit-LogText {
    # Long output keeps its head and, more important for a failure, its tail.
    param([string]$Text, [int]$Max = 6000)
    if ($null -eq $Text) { return '' }
    if ($Text.Length -le $Max) { return $Text }
    $head = [int]($Max / 3)
    $tail = $Max - $head
    return ($Text.Substring(0, $head) + ("...[{0} chars omitted]..." -f ($Text.Length - $Max)) + $Text.Substring($Text.Length - $tail))
}

# ---------------------------------------------------------------------------------------
# Output: progress lines, human lines, the result line (design 5.1)
# ---------------------------------------------------------------------------------------
function Write-Out {
    # The one place stdout is written. Machine mode writes UTF-8 without a BOM and LF line ends
    # straight to the stdout stream, whatever the console code page is.
    param([string]$Text)
    try {
        if ($script:HumanMode) { [Console]::Out.WriteLine($Text); return }
        if ($null -eq $script:StdoutWriter) {
            $w = New-Object System.IO.StreamWriter([Console]::OpenStandardOutput(), (New-Object System.Text.UTF8Encoding($false)))
            $w.NewLine = "`n"
            $w.AutoFlush = $true
            $script:StdoutWriter = $w
        }
        $script:StdoutWriter.WriteLine($Text)
    } catch {
        # A process started with no stdout (Setup starts password-broker without waiting and without
        # capturing it) must still finish its job; the line is dropped and the reason logged.
        Write-Log ("stdout write failed, line dropped: {0}" -f $_.Exception.Message)
    }
}

function ConvertTo-JsonString {
    # Own escaper: PowerShell 5.1's ConvertTo-Json turns an apostrophe into ', and the Pascal
    # side that reads these lines should not have to know that.
    param([string]$Text)
    if ($null -eq $Text) { return 'null' }
    $sb = New-Object System.Text.StringBuilder
    [void]$sb.Append('"')
    foreach ($ch in $Text.ToCharArray()) {
        $code = [int]$ch
        if ($ch -ceq [char]34) { [void]$sb.Append('\"') }
        elseif ($ch -ceq [char]92) { [void]$sb.Append('\\') }
        elseif ($code -eq 10) { [void]$sb.Append('\n') }
        elseif ($code -eq 13) { [void]$sb.Append('\r') }
        elseif ($code -eq 9) { [void]$sb.Append('\t') }
        elseif ($code -lt 32) { [void]$sb.Append(('\u{0:x4}' -f $code)) }
        else { [void]$sb.Append($ch) }
    }
    [void]$sb.Append('"')
    return $sb.ToString()
}

function Format-ProgressJson {
    param([string]$Stage, [string]$Title, [string]$State, $BytesDone, $BytesTotal, [string]$Message, [string]$Fix,
        [string]$MessageId = '', $Values = $null)
    $parts = New-Object System.Collections.ArrayList
    [void]$parts.Add('"schema": 1')
    [void]$parts.Add('"time": ' + (ConvertTo-JsonString ((Get-ClockNow).ToString('yyyy-MM-ddTHH:mm:ss'))))
    [void]$parts.Add('"stage": ' + (ConvertTo-JsonString $Stage))
    [void]$parts.Add('"title": ' + (ConvertTo-JsonString $Title))
    [void]$parts.Add('"state": ' + (ConvertTo-JsonString $State))
    # Like the Linux CLI: bytes only on download stages, message and fix only when there is text.
    if ($null -ne $BytesDone) { [void]$parts.Add('"bytes_done": ' + [string][int64]$BytesDone) }
    if ($null -ne $BytesTotal) { [void]$parts.Add('"bytes_total": ' + [string][int64]$BytesTotal) }
    if ($Message) { [void]$parts.Add('"message": ' + (ConvertTo-JsonString $Message)) }
    if ($Fix) { [void]$parts.Add('"fix": ' + (ConvertTo-JsonString $Fix)) }
    $presentationId = $MessageId
    $presentationValues = $Values
    if (-not $presentationId -and $State -eq 'failed') {
        $presentationId = 'setup.generic.failure'
        $presentationValues = @{ detail = $Message }
    } elseif (-not $presentationId -and $State -eq 'warning') {
        $presentationId = 'setup.generic.warning'
        $presentationValues = @{ detail = $Message }
    }
    if ($presentationId) {
        $localized = Get-LocalizedProgressText -Id $presentationId -Title $Title -Message $Message -Fix $Fix -Values $presentationValues
        if ($localized.Title) { [void]$parts.Add('"title_display": ' + (ConvertTo-JsonString $localized.Title)) }
        if ($localized.Message) { [void]$parts.Add('"message_display": ' + (ConvertTo-JsonString $localized.Message)) }
        if ($localized.Fix) { [void]$parts.Add('"fix_display": ' + (ConvertTo-JsonString $localized.Fix)) }
        if ($Fix -and ($script:Locale -ne 'en-US' -or $presentationId -in @('setup.generic.failure', 'setup.generic.warning')) -and $localized.Fix -cne $Fix) {
            [void]$parts.Add('"fix_technical": ' + (ConvertTo-JsonString $Fix))
        }
    } elseif ($script:Locale -ne 'en-US') {
        $localizedTitle = Get-LocalizedStageTitle -Stage $Stage -Title $Title
        if ($localizedTitle) {
            [void]$parts.Add('"title_display": ' + (ConvertTo-JsonString $localizedTitle))
        } elseif ($Title) {
            $generic = Get-LocalizedProgressText -Id 'setup.generic.warning' -Title '' -Message '' -Fix '' `
                -Values @{ detail = $Title }
            [void]$parts.Add('"title_display": ' + (ConvertTo-JsonString $generic.Title))
            [void]$parts.Add('"message_display": ' + (ConvertTo-JsonString $generic.Message))
        }
        if ($localizedTitle -and $State -eq 'progress' -and $Message) {
            $status = Get-LocalizedProgressText -Id 'setup.progress.status' -Title '' -Message '' -Fix ''
            [void]$parts.Add('"message_display": ' + (ConvertTo-JsonString $status.Message))
        }
    }
    return ('{' + ($parts -join ', ') + '}')
}

function Set-HelperLocale {
    $candidate = [string]$env:COGNITA_LANG
    if ($script:SupportedLocales -ccontains $candidate) { $script:Locale = $candidate }
    else { $script:Locale = 'en-US' }
    $catalogName = 'windows-setup.{0}.json' -f $script:Locale
    $path = Join-Path (Join-Path $script:HelperDir 'locales') $catalogName
    if (-not (Test-Path -LiteralPath $path)) { $path = Join-Path $script:HelperDir $catalogName }
    try {
        $script:LocaleCatalog = (Get-Content -LiteralPath $path -Raw -Encoding UTF8 -ErrorAction Stop) | ConvertFrom-Json
    } catch {
        $script:Locale = 'en-US'
        $fallback = Join-Path (Join-Path $script:HelperDir 'locales') 'windows-setup.en-US.json'
        if (-not (Test-Path -LiteralPath $fallback)) { $fallback = Join-Path $script:HelperDir 'windows-setup.en-US.json' }
        try { $script:LocaleCatalog = (Get-Content -LiteralPath $fallback -Raw -Encoding UTF8 -ErrorAction Stop) | ConvertFrom-Json }
        catch { $script:LocaleCatalog = $null; Write-Log ("locale catalog unavailable: {0}" -f $_.Exception.Message) }
    }
}

function Get-LocalizedProgressText {
    param([string]$Id, [string]$Title, [string]$Message, [string]$Fix, $Values = $null)
    $entry = $null
    if ($script:LocaleCatalog -and $script:LocaleCatalog.PSObject.Properties[$Id]) {
        $entry = $script:LocaleCatalog.$Id
    }
    if (-not $entry -and $script:Locale -ne 'en-US') {
        $fallbackPath = Join-Path (Join-Path $script:HelperDir 'locales') 'windows-setup.en-US.json'
        if (-not (Test-Path -LiteralPath $fallbackPath)) { $fallbackPath = Join-Path $script:HelperDir 'windows-setup.en-US.json' }
        try {
            $fallbackCatalog = (Get-Content -LiteralPath $fallbackPath -Raw -Encoding UTF8 -ErrorAction Stop) | ConvertFrom-Json
            if ($fallbackCatalog.PSObject.Properties[$Id]) { $entry = $fallbackCatalog.$Id }
        } catch { Write-Log ("English locale fallback could not be read: {0}" -f $_.Exception.Message) }
    }
    if (-not $entry) { return @{ Title = $Title; Message = $Message; Fix = $Fix } }
    $translated = @{ Title = [string]$entry.title; Message = [string]$entry.message; Fix = [string]$entry.fix }
    if ($Values) {
        $keys = if ($Values -is [System.Collections.IDictionary]) { @($Values.Keys) } else { @($Values.PSObject.Properties | ForEach-Object { $_.Name }) }
        foreach ($key in $keys) {
            $token = '{' + [string]$key + '}'
            $value = if ($Values -is [System.Collections.IDictionary]) { $Values[$key] } else { $Values.$key }
            if ($key -eq 'reason' -and $entry.PSObject.Properties['reasons'] -and $entry.reasons.PSObject.Properties[[string]$value]) {
                $value = [string]$entry.reasons.([string]$value)
            }
            foreach ($field in @('Title', 'Message', 'Fix')) {
                $translated[$field] = $translated[$field].Replace($token, [string]$value)
            }
        }
    }
    return $translated
}

function Test-SetupMessageId {
    param([string]$Id)
    if ($script:LocaleCatalog -and $script:LocaleCatalog.PSObject.Properties[$Id]) { return $true }
    if ($script:Locale -eq 'en-US') { return $false }
    $fallbackPath = Join-Path (Join-Path $script:HelperDir 'locales') 'windows-setup.en-US.json'
    if (-not (Test-Path -LiteralPath $fallbackPath)) { $fallbackPath = Join-Path $script:HelperDir 'windows-setup.en-US.json' }
    try {
        $fallbackCatalog = (Get-Content -LiteralPath $fallbackPath -Raw -Encoding UTF8 -ErrorAction Stop) | ConvertFrom-Json
        return [bool]$fallbackCatalog.PSObject.Properties[$Id]
    } catch { return $false }
}

function Add-LocalizedRelayFields {
    param([string]$JsonLine)
    try { $o = $JsonLine | ConvertFrom-Json -ErrorAction Stop }
    catch { return $JsonLine }
    if ($o.PSObject.Properties['title_display'] -or $o.PSObject.Properties['message_display'] -or
        $o.PSObject.Properties['fix_display']) { return $JsonLine }
    $id = [string]$o.presentation_id
    $values = $o.presentation_values
    if (-not $id -and [string]$o.state -in @('failed', 'warning')) {
        $id = if ([string]$o.state -eq 'warning') { 'setup.generic.warning' } else { 'setup.generic.failure' }
        $values = @{ detail = [string]$o.message }
    } elseif ($id -and -not (Test-SetupMessageId -Id $id) -and [string]$o.state -in @('failed', 'warning')) {
        $id = if ([string]$o.state -eq 'warning') { 'setup.generic.warning' } else { 'setup.generic.failure' }
        $values = @{ detail = [string]$o.message }
    }
    if (-not $id) {
        $stage = [string]$o.stage
        $stageKey = $stage
        $title = ''
        $extra = New-Object System.Collections.ArrayList
        if ($script:Locale -ne 'en-US' -and $stage -eq 'proof' -and [string]$o.title -eq 'Self-tests skipped') {
            $localized = Get-LocalizedProgressText -Id 'setup.progress.proof_skipped' -Title ([string]$o.title) `
                -Message ([string]$o.message) -Fix ([string]$o.fix)
            if ($localized.Title) { [void]$extra.Add('"title_display": ' + (ConvertTo-JsonString $localized.Title)) }
            if ($localized.Message) { [void]$extra.Add('"message_display": ' + (ConvertTo-JsonString $localized.Message)) }
            if ($localized.Fix) { [void]$extra.Add('"fix_display": ' + (ConvertTo-JsonString $localized.Fix)) }
        } elseif ($script:Locale -ne 'en-US') {
            $title = Get-LocalizedStageTitle -Stage $stageKey -Title ([string]$o.title)
            if ($title) {
                [void]$extra.Add('"title_display": ' + (ConvertTo-JsonString $title))
            } elseif ([string]$o.title) {
                $generic = Get-LocalizedProgressText -Id 'setup.generic.warning' -Title '' -Message '' -Fix '' `
                    -Values @{ detail = [string]$o.title }
                [void]$extra.Add('"title_display": ' + (ConvertTo-JsonString $generic.Title))
                [void]$extra.Add('"message_display": ' + (ConvertTo-JsonString $generic.Message))
            }
            if ($title -and [string]$o.state -eq 'progress' -and [string]$o.message) {
                $status = Get-LocalizedProgressText -Id 'setup.progress.status' -Title '' -Message '' -Fix ''
                [void]$extra.Add('"message_display": ' + (ConvertTo-JsonString $status.Message))
            }
        }
        if ($extra.Count -eq 0 -or -not $JsonLine.TrimEnd().EndsWith('}')) { return $JsonLine }
        $trimmedLine = $JsonLine.TrimEnd()
        return ($trimmedLine.Substring(0, $trimmedLine.Length - 1) + ', ' + ($extra -join ', ') + '}')
    }
    $localized = Get-LocalizedProgressText -Id $id -Title ([string]$o.title) -Message ([string]$o.message) `
        -Fix ([string]$o.fix) -Values $values
    if (-not $localized.Title -and -not $localized.Message -and -not $localized.Fix) { return $JsonLine }
    $extra = New-Object System.Collections.ArrayList
    if ($localized.Title) { [void]$extra.Add('"title_display": ' + (ConvertTo-JsonString $localized.Title)) }
    if ($localized.Message) { [void]$extra.Add('"message_display": ' + (ConvertTo-JsonString $localized.Message)) }
    if ($localized.Fix) { [void]$extra.Add('"fix_display": ' + (ConvertTo-JsonString $localized.Fix)) }
    if ($o.fix -and ($script:Locale -ne 'en-US' -or $id -in @('setup.generic.failure', 'setup.generic.warning')) -and $localized.Fix -cne [string]$o.fix) {
        [void]$extra.Add('"fix_technical": ' + (ConvertTo-JsonString ([string]$o.fix)))
    }
    if ($extra.Count -eq 0 -or -not $JsonLine.TrimEnd().EndsWith('}')) { return $JsonLine }
    $trimmedLine = $JsonLine.TrimEnd()
    return ($trimmedLine.Substring(0, $trimmedLine.Length - 1) + ', ' + ($extra -join ', ') + '}')
}

function Get-LocalizedStageTitle {
    param([string]$Stage, [string]$Title)
    if (-not $script:LocaleCatalog -or -not $script:LocaleCatalog.PSObject.Properties['setup.progress_titles']) { return '' }
    $titles = $script:LocaleCatalog.'setup.progress_titles'
    if (-not $titles.PSObject.Properties[$Stage] -and $Stage -match '^check\.') { $Stage = 'check' }
    if (-not $titles.PSObject.Properties[$Stage]) { return '' }
    $translated = [string]$titles.$Stage
    if ($Stage -eq 'acceleration') {
        $vendor = if ($Title -match '(?i)NVIDIA') { 'NVIDIA' } elseif ($Title -match '(?i)AMD') { 'AMD' } else { '' }
        if (-not $vendor) { return $translated.Replace(' {vendor}', '') }
        return $translated.Replace('{vendor}', $vendor)
    }
    return $translated
}

function Get-LocalizedFolderReason {
    param([string]$PresentationId, [string]$Reason)
    if ($PresentationId -and $PresentationId -ne 'setup.generic.failure' -and
        $script:LocaleCatalog -and $script:LocaleCatalog.PSObject.Properties[$PresentationId]) {
        $text = Get-LocalizedProgressText -Id $PresentationId -Title '' -Message '' -Fix ''
        return $text.Message
    }
    $text = Get-LocalizedProgressText -Id 'setup.generic.failure' -Title '' -Message '' -Fix '' -Values @{ detail = $Reason }
    return ($text.Title + ': ' + $text.Message)
}

function Write-HumanProgress {
    param([string]$Title, [string]$State, [string]$Message, [string]$Fix)
    switch ($State) {
        'start'   { Write-Out ('- ' + $Title) }
        'done'    { Write-Out ('  done: ' + $Title) }
        'failed'  {
            Write-Out ('  FAILED: ' + $Title)
            if ($Message) { Write-Out ('  ' + $Message) }
            if ($Fix) { Write-Out ('  ' + $Fix) }
        }
        'warning' {
            Write-Out ('  Warning: ' + $Title)
            if ($Message) { Write-Out ('  ' + $Message) }
            if ($Fix) { Write-Out ('  ' + $Fix) }
        }
        default   { if ($Message -and $Title) { Write-Out ('  ' + $Title + ': ' + $Message) } }
    }
}

function Write-ProgressLine {
    param(
        [string]$Stage,
        [string]$Title,
        [ValidateSet('start', 'progress', 'done', 'failed', 'warning')][string]$State = 'progress',
        $BytesDone = $null,
        $BytesTotal = $null,
        [string]$Message = '',
        [string]$Fix = '',
        [string]$MessageId = '',
        $Values = $null
    )
    Write-Log ("progress stage={0} state={1} title={2} message={3} fix={4}" -f $Stage, $State, $Title, $Message, $Fix)
    if ($script:HumanMode) {
        Write-HumanProgress -Title $Title -State $State -Message $Message -Fix $Fix
        return
    }
    Write-Out (Format-ProgressJson -Stage $Stage -Title $Title -State $State -BytesDone $BytesDone -BytesTotal $BytesTotal -Message $Message -Fix $Fix -MessageId $MessageId -Values $Values)
}

function Write-InfoLine {
    # A plain informational line: text for a person in the terminal, a progress line for Setup.
    param([string]$Text, [string]$Stage = 'info')
    if ($script:HumanMode) { Write-Out $Text; return }
    Write-ProgressLine -Stage $Stage -Title $Text -State 'progress' -Message $Text
}

function Write-RelayedLine {
    # A progress line written by the Linux CLI. Setup gets it unchanged; a person gets text.
    param([string]$JsonLine)
    Write-Log ('relay ' + $JsonLine)
    if ($script:HumanMode) {
        try {
            $o = $JsonLine | ConvertFrom-Json
            Write-HumanProgress -Title ([string]$o.title) -State ([string]$o.state) -Message ([string]$o.message) -Fix ([string]$o.fix)
        } catch {
            Write-Log ('relay parse failed (human mode): ' + $_.Exception.Message)
        }
        return
    }
    Write-Out (Add-LocalizedRelayFields -JsonLine $JsonLine)
}

function Format-ResultValue {
    # Design 18.5: a result value is percent-encoded so a path or a message that holds a semicolon
    # cannot be cut in two by the reader. ONE function, used by the result writer for every value:
    # % becomes %25 (first, so the encoding of ; is not re-encoded) and ; becomes %3B. Setup decodes
    # exactly those two. (This replaces the earlier rule that turned ; into a comma, which changed the
    # text.) A line break still becomes a space: the result is one line.
    param($Value)
    if ($null -eq $Value) { return '' }
    $s = [string]$Value
    $s = ($s -replace "[\r\n]+", ' ')
    return (($s -replace '%', '%25') -replace ';', '%3B')
}

function Format-ResultLine {
    param([string]$Status, $Values)
    $sb = New-Object System.Text.StringBuilder
    [void]$sb.Append('result=' + $Status)
    if ($null -ne $Values) {
        foreach ($k in $Values.Keys) {
            [void]$sb.Append(';' + $k + '=' + (Format-ResultValue $Values[$k]))
        }
    }
    return $sb.ToString()
}

function New-VerbResult {
    param([ValidateSet('ok', 'failed', 'restart-required')][string]$Status = 'ok', $Values = $null)
    if ($null -eq $Values) { $Values = [ordered]@{} }
    return [pscustomobject]@{ Status = $Status; Values = $Values }
}

function Get-ExitCodeForStatus {
    param([string]$Status)
    switch ($Status) {
        'ok' { return 0 }
        'restart-required' { return 3010 }
        default { return 1 }
    }
}

# ---------------------------------------------------------------------------------------
# Waiting: every bounded wait in this file goes through Wait-Until (design 5.1)
# ---------------------------------------------------------------------------------------
function Wait-Until {
    <#
    Polls $Condition every $IntervalMs of clock time until it is true or $TimeoutSec of clock
    time has passed. Returns $true or $false (timeout). With -Stage it writes a progress line
    at least every $HeartbeatSec, so Setup's elapsed clock keeps moving (design 4.2 step 6).
    A condition that throws counts as "not yet" and the exception is logged.
    #>
    param(
        [scriptblock]$Condition,
        [int]$TimeoutSec,
        [int]$IntervalMs = 1000,
        [string]$Description = 'condition',
        [string]$Stage = '',
        [string]$Title = '',
        [int]$HeartbeatSec = 5
    )
    $start = Get-ClockNow
    $deadline = $start.AddSeconds($TimeoutSec)
    $lastBeat = $start
    $polls = 0
    Write-Log ("wait start: {0} timeout={1}s interval={2}ms" -f $Description, $TimeoutSec, $IntervalMs)
    while ($true) {
        $polls++
        $ok = $false
        try { $ok = [bool](& $Condition) } catch { Write-Log ("wait condition threw (treated as not yet): {0}" -f $_.Exception.Message) }
        $now = Get-ClockNow
        if ($ok) {
            Write-Log ("wait done: {0} after {1:N1}s, polls={2}" -f $Description, ($now - $start).TotalSeconds, $polls)
            return $true
        }
        if ($now -ge $deadline) {
            Write-Log ("wait TIMED OUT: {0} after {1:N1}s, polls={2}" -f $Description, ($now - $start).TotalSeconds, $polls)
            return $false
        }
        if ($Stage -and (($now - $lastBeat).TotalSeconds -ge $HeartbeatSec)) {
            $t = $Title
            if (-not $t) { $t = $Description }
            Write-ProgressLine -Stage $Stage -Title $t -State 'progress' -Message ("Still working, {0} s so far." -f [int]($now - $start).TotalSeconds)
            $lastBeat = $now
        }
        Invoke-ClockSleep $IntervalMs
    }
}

# ---------------------------------------------------------------------------------------
# External processes: THE ONE function (design 5.1). Tests replace it.
# ---------------------------------------------------------------------------------------
function ConvertTo-ArgString {
    # Windows command-line quoting (CommandLineToArgvW rules). An argument list, never a
    # command string, is what callers pass; this is only the last step before CreateProcess.
    param([string[]]$Arguments)
    $out = New-Object System.Collections.ArrayList
    foreach ($a in $Arguments) {
        if ($null -eq $a) { $a = '' }
        if ($a.Length -gt 0 -and $a -notmatch '[\s"]') { [void]$out.Add($a); continue }
        $sb = New-Object System.Text.StringBuilder
        [void]$sb.Append('"')
        $bs = 0
        foreach ($ch in $a.ToCharArray()) {
            if ($ch -eq '\') { $bs++; continue }
            if ($ch -eq '"') {
                [void]$sb.Append([char]92, ($bs * 2 + 1))
                [void]$sb.Append([char]34)
                $bs = 0
                continue
            }
            if ($bs -gt 0) { [void]$sb.Append([char]92, $bs); $bs = 0 }
            [void]$sb.Append($ch)
        }
        if ($bs -gt 0) { [void]$sb.Append([char]92, ($bs * 2)) }
        [void]$sb.Append('"')
        [void]$out.Add($sb.ToString())
    }
    return ($out -join ' ')
}

function Invoke-External {
    <#
    Runs one process and returns
      [pscustomobject]@{ ExitCode; Stdout; Stderr; TimedOut; StartError; Ms }
    - Arguments is an array (design: never a command string).
    - WSL_UTF8=1 is set so wsl.exe writes UTF-8; stdout and stderr are decoded as UTF-8.
    - StdinText, when given, is written as UTF-8 WITHOUT a BOM through our own StreamWriter on
      the raw stdin stream, then stdin is closed. (.NET Framework has no StandardInputEncoding
      and Console.InputEncoding is the OEM code page, which would turn a non-ASCII password into
      question marks. Design 5.6 step 3.) The text is never logged, only its length.
    - TimeoutSec is per call and measured on $script:Clock. On timeout the process is killed.
    - OnPoll runs every PollIntervalMs (default 1000) of clock time while the process runs; OnOutputLine gets
      every complete stdout line as it arrives; OnErrorLine does the same for stderr (used to
      catch the sign-in link `tailscale up` prints there).
    #>
    param(
        [string]$FilePath,
        [string[]]$Arguments = @(),
        [int]$TimeoutSec = 120,
        [string]$StdinText = $null,
        [scriptblock]$OnPoll = $null,
        [scriptblock]$OnOutputLine = $null,
        [scriptblock]$OnErrorLine = $null,
        [int]$PollIntervalMs = 1000,
        [switch]$NoLogOutput,
        # Run the process in its OWN visible console window with nothing redirected (no stdin,
        # no captured output). Needed for `wsl.exe --install` / `--update` before WSL exists:
        # Windows' inbox wsl.exe stub prints "not installed" and exits 1 when its output is
        # redirected (seen in P1 from Setup, and earlier over SSH), but installs, with one UAC
        # prompt and its own progress text, when it has a console of its own (P1, 2026-09-29).
        [switch]$OwnConsole
    )
    $argString = ConvertTo-ArgString $Arguments
    $stdinInfo = 'none'
    if ($null -ne $StdinText) { $stdinInfo = ('{0} chars (not logged)' -f $StdinText.Length) }
    if ($OwnConsole) { $stdinInfo = 'own console window, output not captured' }
    Write-Log ("exec: {0} {1} [timeout={2}s stdin={3}]" -f $FilePath, $argString, $TimeoutSec, $stdinInfo)
    $started = Get-ClockNow
    $result = [pscustomobject]@{ ExitCode = -1; Stdout = ''; Stderr = ''; TimedOut = $false; StartError = ''; Ms = 0 }
    $p = $null
    try {
        $psi = New-Object System.Diagnostics.ProcessStartInfo
        $psi.FileName = $FilePath
        $psi.Arguments = $argString
        if ($OwnConsole) {
            # ShellExecute gives the child a new console window of its own, like Start-Process.
            $psi.UseShellExecute = $true
            $psi.WindowStyle = [System.Diagnostics.ProcessWindowStyle]::Normal
        } else {
            $psi.UseShellExecute = $false
            $psi.CreateNoWindow = $true
            $psi.RedirectStandardOutput = $true
            $psi.RedirectStandardError = $true
            $psi.RedirectStandardInput = $true
            $psi.StandardOutputEncoding = New-Object System.Text.UTF8Encoding($false)
            $psi.StandardErrorEncoding = New-Object System.Text.UTF8Encoding($false)
            $psi.EnvironmentVariables['WSL_UTF8'] = '1'
        }
        $p = New-Object System.Diagnostics.Process
        $p.StartInfo = $psi
        # .NET Framework builds Process.StandardInput from the console input encoding, and when
        # that is UTF-8 it writes a BOM the moment the process starts (measured: efbbbf before
        # the first byte we write). A BOM would become part of the password, so the console
        # input encoding is set to UTF-8 WITHOUT a preamble around Start and restored after.
        $savedInputEncoding = $null
        if ($null -ne $StdinText) {
            try {
                $savedInputEncoding = [Console]::InputEncoding
                [Console]::InputEncoding = New-Object System.Text.UTF8Encoding($false)
            } catch {
                Write-Log ("console input encoding not changed (no console?): {0}" -f $_.Exception.Message)
                $savedInputEncoding = $null
            }
        }
        try {
            [void]$p.Start()
        } finally {
            if ($null -ne $savedInputEncoding) {
                try { [Console]::InputEncoding = $savedInputEncoding } catch { Write-Log ("console input encoding restore failed: {0}" -f $_.Exception.Message) }
            }
        }
    } catch {
        $result.StartError = $_.Exception.Message
        Write-Log ("exec start FAILED: {0}" -f $_.Exception.Message)
        return $result
    }
    try {
        if ($OwnConsole) {
            # Nothing redirected: there is no stdin to close and no output to read.
        } elseif ($null -ne $StdinText) {
            $sw = New-Object System.IO.StreamWriter($p.StandardInput.BaseStream, (New-Object System.Text.UTF8Encoding($false)))
            try { $sw.Write($StdinText); $sw.Flush() } catch { Write-Log ("exec stdin write failed: {0}" -f $_.Exception.Message) }
            try { $sw.Close() } catch { Write-Log ("exec stdin close failed: {0}" -f $_.Exception.Message) }
        } else {
            try { $p.StandardInput.Close() } catch { Write-Log ("exec stdin close failed: {0}" -f $_.Exception.Message) }
        }
        $outSb = New-Object System.Text.StringBuilder
        $errSb = New-Object System.Text.StringBuilder
        $outBuf = New-Object 'char[]' 4096
        $errBuf = New-Object 'char[]' 4096
        $outTask = $null
        $errTask = $null
        if (-not $OwnConsole) {
            $outTask = $p.StandardOutput.ReadAsync($outBuf, 0, $outBuf.Length)
            $errTask = $p.StandardError.ReadAsync($errBuf, 0, $errBuf.Length)
        }
        $pending = ''
        $errPending = ''
        $deadline = $started.AddSeconds($TimeoutSec)
        $lastPoll = $started
        while ($true) {
            $moved = $false
            if ($null -ne $outTask -and $outTask.IsCompleted) {
                $n = $outTask.Result
                if ($n -gt 0) {
                    $chunk = [string]::new($outBuf, 0, $n)
                    [void]$outSb.Append($chunk)
                    if ($OnOutputLine) {
                        $pending += $chunk
                        while (($i = $pending.IndexOf("`n")) -ge 0) {
                            $line = $pending.Substring(0, $i).TrimEnd("`r")
                            $pending = $pending.Substring($i + 1)
                            try { & $OnOutputLine $line } catch { Write-Log ("OnOutputLine threw: {0}" -f $_.Exception.Message) }
                        }
                    }
                    $outTask = $p.StandardOutput.ReadAsync($outBuf, 0, $outBuf.Length)
                } else { $outTask = $null }
                $moved = $true
            }
            if ($null -ne $errTask -and $errTask.IsCompleted) {
                $n = $errTask.Result
                if ($n -gt 0) {
                    $errChunk = [string]::new($errBuf, 0, $n)
                    [void]$errSb.Append($errChunk)
                    if ($OnErrorLine) {
                        $errPending += $errChunk
                        while (($j = $errPending.IndexOf("`n")) -ge 0) {
                            $eline = $errPending.Substring(0, $j).TrimEnd("`r")
                            $errPending = $errPending.Substring($j + 1)
                            try { & $OnErrorLine $eline } catch { Write-Log ("OnErrorLine threw: {0}" -f $_.Exception.Message) }
                        }
                    }
                    $errTask = $p.StandardError.ReadAsync($errBuf, 0, $errBuf.Length)
                } else { $errTask = $null }
                $moved = $true
            }
            $now = Get-ClockNow
            if ($OnPoll -and (($now - $lastPoll).TotalMilliseconds -ge $PollIntervalMs)) {
                $lastPoll = $now
                try { & $OnPoll } catch { Write-Log ("OnPoll threw: {0}" -f $_.Exception.Message) }
            }
            if ($null -eq $outTask -and $null -eq $errTask -and $p.HasExited) { break }
            if ($now -ge $deadline) {
                $result.TimedOut = $true
                Write-Log ("exec TIMED OUT after {0}s; killing pid {1}" -f $TimeoutSec, $p.Id)
                try { $p.Kill() } catch { Write-Log ("exec kill failed: {0}" -f $_.Exception.Message) }
                break
            }
            if (-not $moved) { Invoke-ClockSleep 50 }
        }
        if ($OnOutputLine -and $pending.Length -gt 0) {
            try { & $OnOutputLine $pending.TrimEnd("`r") } catch { Write-Log ("OnOutputLine threw: {0}" -f $_.Exception.Message) }
        }
        if ($OnErrorLine -and $errPending.Length -gt 0) {
            try { & $OnErrorLine $errPending.TrimEnd("`r") } catch { Write-Log ("OnErrorLine threw: {0}" -f $_.Exception.Message) }
        }
        if (-not $result.TimedOut) {
            $p.WaitForExit()
            $result.ExitCode = $p.ExitCode
        } else {
            $result.ExitCode = 124
        }
        $result.Stdout = $outSb.ToString()
        $result.Stderr = $errSb.ToString()
    } catch {
        $result.StartError = $_.Exception.Message
        Write-Log ("exec FAILED while running: {0}" -f $_.Exception.Message)
        try { if (-not $p.HasExited) { $p.Kill() } } catch { Write-Log ("exec cleanup kill failed: {0}" -f $_.Exception.Message) }
    } finally {
        if ($p) { $p.Dispose() }
    }
    $result.Ms = [int]((Get-ClockNow) - $started).TotalMilliseconds
    if ($NoLogOutput) {
        Write-Log ("exec done: exit={0} ms={1} stdout={2} chars stderr={3} chars (output not logged)" -f $result.ExitCode, $result.Ms, $result.Stdout.Length, $result.Stderr.Length)
    } else {
        Write-Log ("exec done: exit={0} ms={1} timedout={2} stdout=[{3}] stderr=[{4}]" -f $result.ExitCode, $result.Ms, $result.TimedOut, (Limit-LogText $result.Stdout), (Limit-LogText $result.Stderr))
    }
    return $result
}

function Invoke-Passthrough {
    # A command that must own the console (interactive prompts, logs -f): stdio is inherited,
    # nothing is captured. Returns the exit code. Tests replace it.
    param([string]$FilePath, [string[]]$Arguments = @())
    $argString = ConvertTo-ArgString $Arguments
    Write-Log ("passthrough: {0} {1}" -f $FilePath, $argString)
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = $FilePath
    $psi.Arguments = $argString
    $psi.UseShellExecute = $false
    $psi.EnvironmentVariables['WSL_UTF8'] = '1'
    $p = New-Object System.Diagnostics.Process
    $p.StartInfo = $psi
    try {
        [void]$p.Start()
        $p.WaitForExit()
        $code = $p.ExitCode
    } catch {
        Write-Log ("passthrough FAILED: {0}" -f $_.Exception.Message)
        $code = -1
    } finally {
        $p.Dispose()
    }
    Write-Log ("passthrough done: exit={0}" -f $code)
    return $code
}

# ---------------------------------------------------------------------------------------
# WSL wrappers. DESIGN NOTE: the design writes "wsl.exe -d <distro> -u <user> -- cognita ...".
# Without --exec, wsl.exe hands the command to the user's shell, which would eat the
# backslashes in a Windows path passed as --documents-display and split a path with spaces.
# --exec runs the program with argv exactly as given, so this file uses
# "--exec /usr/local/bin/cognita ...". One place to change it: Get-WslExecArgs.
# ---------------------------------------------------------------------------------------
function Get-DistroName {
    param($Settings)
    if ($Settings -and $Settings.distro) { return [string]$Settings.distro }
    return $script:DefaultDistro
}
function Get-LinuxUser {
    param($Settings)
    if ($Settings -and $Settings.linux_user) { return [string]$Settings.linux_user }
    return $script:DefaultLinuxUser
}

function Get-WslExecArgs {
    param([string]$Distro, [string]$User, [string[]]$Command)
    return (@('-d', $Distro, '-u', $User, '--exec') + $Command)
}

function Invoke-Wsl {
    # Runs a command inside the distro as $User. Never used for status or forwarded verbs on a
    # stopped distro: callers check Test-DistroRunning first (design 8).
    param(
        $Settings,
        [string]$User,
        [string[]]$Command,
        [int]$TimeoutSec = 120,
        [string]$StdinText = $null,
        [scriptblock]$OnPoll = $null,
        [scriptblock]$OnOutputLine = $null,
        [int]$PollIntervalMs = 1000,
        [switch]$NoLogOutput
    )
    $distro = Get-DistroName $Settings
    if (-not $User) { $User = Get-LinuxUser $Settings }
    $wslArgs = Get-WslExecArgs -Distro $distro -User $User -Command $Command
    return (Invoke-External -FilePath (Get-WslExe) -Arguments $wslArgs -TimeoutSec $TimeoutSec -StdinText $StdinText -OnPoll $OnPoll -OnOutputLine $OnOutputLine -PollIntervalMs $PollIntervalMs -NoLogOutput:$NoLogOutput)
}

function Invoke-WslScript {
    # A shell script fed on stdin ("sh -s"): no quoting of the script through wsl.exe's command
    # line. Commands inside the script that read stdin must be given </dev/null.
    param(
        $Settings,
        [string]$User,
        [string]$ScriptText,
        [string[]]$ScriptArgs = @(),
        [int]$TimeoutSec = 120,
        # Design 22.5: the toolkit install beats every 5 s while its script runs.
        [scriptblock]$OnPoll = $null,
        [int]$PollIntervalMs = 1000
    )
    return (Invoke-Wsl -Settings $Settings -User $User -Command (@('sh', '-s', '--') + $ScriptArgs) -StdinText $ScriptText -TimeoutSec $TimeoutSec -OnPoll $OnPoll -PollIntervalMs $PollIntervalMs)
}

function Remove-NulAndBom {
    param([string]$Text)
    if ($null -eq $Text) { return '' }
    return (($Text -replace "`0", '') -replace ([string][char]0xFEFF), '')
}

# ---------------------------------------------------------------------------------------
# Arguments: --name value, --name=value, --flag (design verbs use this style)
# ---------------------------------------------------------------------------------------
# The reason text shutdown.exe shows for the restart Setup asks for (restart-for-wsl).
$script:RestartComment = 'Restarting to finish turning on WSL for Cognita'
# Where users can ask for help (2026-09-29); the same URL as Setup's SupportUrl. Nothing is ever
# sent from here: diagnostics are a file the user may choose to attach there.
$script:SupportUrl = 'https://github.com/dbeachy1/Cognita/issues/new'
$script:BoolFlags = @('now', 'keep-data', 'delete-data', 'yes', 'non-interactive', 'no-open', 'after-wsl-install', 'install-tailscale', 'json', 'wsl-memory-reclaim')

function ConvertFrom-HelperArgs {
    param([string[]]$Tokens)
    $opts = @{}
    $pos = New-Object System.Collections.ArrayList
    $Tokens = @($Tokens)
    for ($i = 0; $i -lt $Tokens.Count; $i++) {
        $t = [string]$Tokens[$i]
        if ($t -match '^--([A-Za-z0-9][A-Za-z0-9-]*)(=(.*))?$') {
            $name = $Matches[1].ToLowerInvariant()
            if ($Matches[2]) {
                $opts[$name] = $Matches[3]
            } elseif ($script:BoolFlags -contains $name) {
                $opts[$name] = $true
            } elseif (($i + 1) -lt $Tokens.Count) {
                $i++
                $opts[$name] = [string]$Tokens[$i]
            } else {
                $opts[$name] = $true
            }
        } else {
            [void]$pos.Add($t)
        }
    }
    return @{ Opts = $opts; Positional = @($pos) }
}

function Get-Opt {
    param($Opts, [string]$Name, $Default = $null)
    if ($Opts -and $Opts.ContainsKey($Name)) { return $Opts[$Name] }
    return $Default
}

# ---------------------------------------------------------------------------------------
# settings.json (design 3.1). Atomic write: settings.json.tmp, then Replace (or Move the first time).
# ---------------------------------------------------------------------------------------
function Set-SettingProp {
    param($Obj, [string]$Name, $Value)
    if ($Obj.PSObject.Properties[$Name]) { $Obj.$Name = $Value }
    else { Add-Member -InputObject $Obj -NotePropertyName $Name -NotePropertyValue $Value -Force }
}

function New-Settings {
    param([string]$VhdDir, [string]$Distro = '', [string]$LinuxUser = '')
    if (-not $Distro) { $Distro = $script:DefaultDistro }
    if (-not $LinuxUser) { $LinuxUser = $script:DefaultLinuxUser }
    $stamp = (Get-ClockNow).ToString('yyyy-MM-ddTHH:mm:ss')
    return [pscustomobject][ordered]@{
        schema          = 1
        installation_id = [guid]::NewGuid().ToString()
        state           = 'new'
        distro          = $Distro
        linux_user      = $LinuxUser
        vhd_dir         = $VhdDir
        mcp_port        = 8675
        admin_port      = 8676
        roots           = @()
        workspace       = 'on'
        setup_version   = ''
        setup_revision  = 0
        # Design 18.2: set only after `cognita install` / `cognita update` exits 0 (the Setup version
        # that reached it), never cleared by a failure. `installed` in the state verb means an owned
        # distro AND this is set. admin_user is set with it.
        linux_installed_version = ''
        admin_user      = ''
        # Design 21.3: what the last install/update read as COGNITA_PROOF from the Linux side's
        # install.env (passed | skipped | '' = unknown). `state` reports it as proof=.
        linux_proof     = ''
        # Design 22.3/22.4: the acceleration profile the Linux side reported after the last install,
        # update or forwarded install/rollback (cpu | amd | nvidia, '' = unknown). Never overwritten
        # with '' once known (design 22.12 item 1a). `update` reads it to decide on the toolkit step.
        acceleration    = ''
        funnel          = $null
        resume          = $null
        created         = $stamp
        updated         = $stamp
    }
}

function Read-Settings {
    $path = Get-SettingsPath
    if (-not (Test-Path -LiteralPath $path)) { return $null }
    try {
        $text = [System.IO.File]::ReadAllText($path, (New-Object System.Text.UTF8Encoding($false)))
        $s = $text | ConvertFrom-Json
        if ($null -eq $s.roots) { Set-SettingProp $s 'roots' @() }
        return $s
    } catch {
        Write-Log ("settings.json could not be read ({0}): {1}" -f $path, $_.Exception.Message)
        return $null
    }
}

function Save-Settings {
    param($Settings)
    $path = Get-SettingsPath
    $dir = Split-Path -Parent $path
    if (-not (Test-Path -LiteralPath $dir)) { [void](New-Item -ItemType Directory -Path $dir -Force) }
    Set-SettingProp $Settings 'updated' ((Get-ClockNow).ToString('yyyy-MM-ddTHH:mm:ss'))
    $json = ConvertTo-Json -InputObject $Settings -Depth 6
    $tmp = $path + '.tmp'
    [System.IO.File]::WriteAllText($tmp, $json, (New-Object System.Text.UTF8Encoding($false)))
    if (Test-Path -LiteralPath $path) {
        # [NullString]::Value: a plain $null becomes "" for a .NET string parameter, which Replace rejects.
        [System.IO.File]::Replace($tmp, $path, [NullString]::Value)
        Write-Log ("settings saved (replace): state={0} roots={1}" -f $Settings.state, @(Get-SettingsRoots $Settings).Count)
    } else {
        [System.IO.File]::Move($tmp, $path)
        Write-Log ("settings saved (first write, move): state={0}" -f $Settings.state)
    }
}

function Get-SettingsRoots {
    param($Settings)
    if ($null -eq $Settings) { return @() }
    $r = @()
    foreach ($x in @($Settings.roots)) { if ($null -ne $x) { $r += $x } }
    return $r
}

# ---------------------------------------------------------------------------------------
# Small readers and writers of machine state. Tests replace these (design 14.2).
# ---------------------------------------------------------------------------------------
function Get-RegistryValue {
    param([string]$Path, [string]$Name)
    try { return (Get-ItemProperty -LiteralPath $Path -Name $Name -ErrorAction Stop).$Name } catch { return $null }
}
function Test-RegistryKey { param([string]$Path) return (Test-Path -LiteralPath $Path) }
function Set-RegistryValue {
    param([string]$Path, [string]$Name, $Value, [string]$Type = 'String')
    if (-not (Test-Path -LiteralPath $Path)) { [void](New-Item -Path $Path -Force) }
    [void](New-ItemProperty -LiteralPath $Path -Name $Name -Value $Value -PropertyType $Type -Force)
}
function Get-LxssBase { return 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Lxss' }
function Get-LxssDistros {
    # All registered WSL distros: Guid, Name, BasePath (the folder that holds its virtual disk).
    $base = Get-LxssBase
    $out = @()
    if (-not (Test-Path -LiteralPath $base)) { return $out }
    foreach ($k in (Get-ChildItem -LiteralPath $base -ErrorAction SilentlyContinue)) {
        $name = $k.GetValue('DistributionName')
        if ($name) { $out += [pscustomobject]@{ Guid = $k.PSChildName; Name = [string]$name; BasePath = [string]$k.GetValue('BasePath') } }
    }
    return $out
}
function Get-LxssDefaultDistribution { return (Get-RegistryValue -Path (Get-LxssBase) -Name 'DefaultDistribution') }
function Set-LxssDefaultDistribution { param([string]$Guid) Set-RegistryValue -Path (Get-LxssBase) -Name 'DefaultDistribution' -Value $Guid -Type 'String' }

function Get-OsInfo {
    $build = Get-RegistryValue -Path 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion' -Name 'CurrentBuildNumber'
    if (-not $build) { $build = [Environment]::OSVersion.Version.Build }
    return [pscustomobject]@{ Is64 = [Environment]::Is64BitOperatingSystem; Build = [int]$build }
}
function Get-MemoryBytes {
    try { return [int64](Get-CimInstance -ClassName Win32_ComputerSystem -ErrorAction Stop).TotalPhysicalMemory } catch { Write-Log ("memory read failed: {0}" -f $_.Exception.Message); return [int64]0 }
}
function Get-VirtualizationInfo {
    $hv = $false; $fw = $false
    try { $hv = [bool](Get-CimInstance -ClassName Win32_ComputerSystem -ErrorAction Stop).HypervisorPresent } catch { Write-Log ("HypervisorPresent read failed: {0}" -f $_.Exception.Message) }
    try { $fw = [bool](@(Get-CimInstance -ClassName Win32_Processor -ErrorAction Stop | Where-Object { $_.VirtualizationFirmwareEnabled }).Count -gt 0) } catch { Write-Log ("VirtualizationFirmwareEnabled read failed: {0}" -f $_.Exception.Message) }
    return [pscustomobject]@{ Hypervisor = $hv; Firmware = $fw }
}
function Get-ExistingAncestor {
    param([string]$Path)
    $p = $Path
    while ($p -and -not (Test-Path -LiteralPath $p)) {
        $parent = Split-Path -Parent $p
        if ($parent -eq $p) { break }
        $p = $parent
    }
    return $p
}
function Get-FreeSpaceBytes {
    param([string]$Path)
    try {
        $root = [System.IO.Path]::GetPathRoot([System.IO.Path]::GetFullPath((Get-ExistingAncestor $Path)))
        return [int64](New-Object System.IO.DriveInfo($root)).AvailableFreeSpace
    } catch { Write-Log ("free space read failed for {0}: {1}" -f $Path, $_.Exception.Message); return [int64]-1 }
}
function Get-DriveTypeName {
    # 'Fixed', 'Removable', 'Network', 'CDRom', 'Ram', 'NoRootDirectory' or 'Unknown'.
    param([string]$DriveLetter)
    try { return (New-Object System.IO.DriveInfo($DriveLetter)).DriveType.ToString() }
    catch { Write-Log ("drive type read failed for {0}: {1}" -f $DriveLetter, $_.Exception.Message); return 'Unknown' }
}
function Get-ListeningPorts {
    # [pscustomobject]@{ Port; ProcessId; Process }
    $out = @()
    try {
        foreach ($c in (Get-NetTCPConnection -State Listen -ErrorAction Stop)) {
            $name = ''
            try { $name = (Get-Process -Id $c.OwningProcess -ErrorAction Stop).ProcessName } catch { $name = '' }
            $out += [pscustomobject]@{ Port = [int]$c.LocalPort; ProcessId = [int]$c.OwningProcess; Process = $name }
        }
    } catch { Write-Log ("listening ports read failed: {0}" -f $_.Exception.Message) }
    return $out
}
function Get-WslConfigPath {
    # Where the user's .wslconfig lives (it may not exist). Design 22.9: also the one place
    # Set-WslReclaimSetting writes, so tests point both at a temp file by replacing this function.
    return (Join-Path $env:USERPROFILE '.wslconfig')
}
function Get-WslConfigText {
    $p = Get-WslConfigPath
    if (Test-Path -LiteralPath $p) {
        try { return [System.IO.File]::ReadAllText($p) } catch { Write-Log (".wslconfig read failed: {0}" -f $_.Exception.Message) }
    }
    return $null
}
function Get-DockerDesktopInfo {
    # Installed, and which WSL distros its "WSL integration" is turned on for. Docker Desktop keeps
    # that list as IntegratedWslDistros in %APPDATA%\Docker\settings-store.json (older builds:
    # settings.json); the key is absent when no distro is ticked. A distro on that list gets Docker
    # Desktop's docker instead of its own, which would break Cognita's; a distro not on it needs no
    # attention, so the check says nothing then (2026-09-29: a "leave Cognita unchecked" note shown
    # before the distro even existed sent the user looking for a box that was not there).
    $exe = Join-Path $env:ProgramFiles 'Docker\Docker\Docker Desktop.exe'
    $installed = Test-Path -LiteralPath $exe
    $integrated = @()
    if ($installed) {
        foreach ($name in @('settings-store.json', 'settings.json')) {
            $p = Join-Path $env:APPDATA ('Docker\' + $name)
            if (-not (Test-Path -LiteralPath $p)) { continue }
            try {
                $o = [System.IO.File]::ReadAllText($p) | ConvertFrom-Json
                $list = $o.PSObject.Properties['IntegratedWslDistros']
                if ($list -and $list.Value) { $integrated = @($list.Value | ForEach-Object { [string]$_ }) }
                Write-Log ("docker desktop: {0} integrated_distros=[{1}]" -f $name, ($integrated -join ','))
                break
            } catch { Write-Log ("docker desktop: {0} unreadable: {1}" -f $name, $_.Exception.Message) }
        }
    }
    return [pscustomobject]@{ Installed = $installed; IntegratedDistros = $integrated }
}
function Get-NvidiaSmiPath {
    # Design 22.2 rule 1: the nvidia-smi.exe the NVIDIA driver installs into System32; $null when absent.
    # A reader of its own so tests never look at the real machine.
    $p = Join-Path $env:SystemRoot 'System32\nvidia-smi.exe'
    if (Test-Path -LiteralPath $p) { return $p }
    return $null
}
function Get-VideoControllers {
    # Design 22.2 rule 2: [pscustomobject]@{ Name; DriverVersion } per display adapter, from the CIM
    # class Win32_VideoController. Throws when CIM fails; Get-NvidiaCard catches and logs. A reader of its
    # own so tests replace it.
    $out = @()
    foreach ($c in @(Get-CimInstance -ClassName Win32_VideoController -ErrorAction Stop)) {
        $out += [pscustomobject]@{ Name = [string]$c.Name; DriverVersion = [string]$c.DriverVersion }
    }
    return $out
}
function Get-ReparseTarget {
    # The target of a junction or symbolic link, or $null when the path is neither.
    param([string]$Path)
    try {
        $item = Get-Item -LiteralPath $Path -Force -ErrorAction Stop
        if (-not ($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint)) { return $null }
        $t = $item.Target
        if ($t -is [array]) { $t = $t[0] }
        if (-not $t) { return $null }
        $t = [string]$t
        if ($t.StartsWith('\??\')) { $t = $t.Substring(4) }
        if (-not [System.IO.Path]::IsPathRooted($t)) { $t = [System.IO.Path]::GetFullPath((Join-Path (Split-Path -Parent $Path) $t)) }
        return $t
    } catch { Write-Log ("reparse target read failed for {0}: {1}" -f $Path, $_.Exception.Message); return $null }
}
function Test-DirectoryExists { param([string]$Path) return (Test-Path -LiteralPath $Path -PathType Container) }
function Get-DesktopPath { return [Environment]::GetFolderPath('Desktop') }
function Get-ComputerNameText { return $env:COMPUTERNAME }
function Test-Interactive {
    try { return ([Environment]::UserInteractive -and -not [Console]::IsInputRedirected) }
    catch { Write-Log ("interactive check failed, assuming not a terminal: {0}" -f $_.Exception.Message); return $false }
}
function Read-Line {
    param([string]$Prompt)
    return (Read-Host -Prompt $Prompt)
}
function Read-SecretLine {
    param([string]$Prompt)
    $ss = Read-Host -Prompt $Prompt -AsSecureString
    $bstr = [System.Runtime.InteropServices.Marshal]::SecureStringToBSTR($ss)
    try { return [System.Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr) }
    finally { [System.Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr) }
}
function Start-DetachedHidden {
    # Starts a process that outlives this helper, hidden, without waiting for it (design 19.11 R1's
    # restart waiter). Returns $true when it started. A seam like Open-InExplorer: the tests replace it
    # with a recorder, so no real process is ever started by a test.
    param([string]$FilePath, [string[]]$Arguments = @())
    try {
        [void](Start-Process -FilePath $FilePath -ArgumentList (ConvertTo-ArgString $Arguments) -WindowStyle Hidden)
        return $true
    } catch {
        Write-Log ("detached start of {0} failed: {1}" -f $FilePath, $_.Exception.Message)
        return $false
    }
}
function Open-InExplorer {
    param([string]$Path)
    try { Start-Process -FilePath 'explorer.exe' -ArgumentList ('/select,"{0}"' -f $Path) } catch { Write-Log ("explorer open failed: {0}" -f $_.Exception.Message) }
}
function Show-FolderPicker {
    # Returns the chosen folder or $null. Only used when no PATH was given to add-folder.
    try {
        Add-Type -AssemblyName System.Windows.Forms
        $d = New-Object System.Windows.Forms.FolderBrowserDialog
        $d.Description = 'Choose the folder Cognita should read and write documents in.'
        if ($d.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) { return $d.SelectedPath }
    } catch { Write-Log ("folder picker failed: {0}" -f $_.Exception.Message) }
    return $null
}

# ---------------------------------------------------------------------------------------
# Path rules and fstab (design 5.5)
# ---------------------------------------------------------------------------------------
function Get-NormalizedPath {
    # Full path without a trailing backslash and without a \\?\ prefix, for comparing and converting.
    param([string]$Path)
    $p = $Path
    if ($p.StartsWith('\\?\') -and -not $p.StartsWith('\\?\UNC\')) { $p = $p.Substring(4) }
    $full = [System.IO.Path]::GetFullPath($p)
    if ($full.Length -gt 3) { $full = $full.TrimEnd('\') }
    return $full
}

function Test-PathNests {
    # True when the two paths are the same folder, or one is inside the other.
    param([string]$A, [string]$B)
    $a = (Get-NormalizedPath $A).TrimEnd('\')
    $b = (Get-NormalizedPath $B).TrimEnd('\')
    if ($a -ieq $b) { return $true }
    if ($a.StartsWith($b + '\', [System.StringComparison]::OrdinalIgnoreCase)) { return $true }
    if ($b.StartsWith($a + '\', [System.StringComparison]::OrdinalIgnoreCase)) { return $true }
    return $false
}

function ConvertTo-ForwardSlashPath {
    param([string]$WindowsPath)
    return ((Get-NormalizedPath $WindowsPath) -replace '\\', '/')
}

function ConvertTo-FstabSource {
    # C:\Users\me\My Docs -> C:/Users/me/My\040Docs  (forward slashes; space is the only escape
    # needed because a backslash cannot occur after conversion). Design 5.5 rule 3.
    param([string]$WindowsPath)
    if ($WindowsPath -match "[\t\r\n]") { throw 'The path contains a tab or a line break, which fstab cannot carry.' }
    $s = ConvertTo-ForwardSlashPath $WindowsPath
    return ($s -replace ' ', '\040')
}

function ConvertTo-WslMntPath {
    # C:\Users\me\x -> /mnt/c/Users/me/x. For an argument (never through a shell), so no escaping.
    param([string]$WindowsPath)
    $s = ConvertTo-ForwardSlashPath $WindowsPath
    if ($s -match '^([A-Za-z]):(/.*)?$') { return ('/mnt/' + $Matches[1].ToLowerInvariant() + $Matches[2]) }
    throw ("Not a drive path: {0}" -f $WindowsPath)
}

function Get-RootMountPoint { param([int]$N) return ('{0}/{1}' -f $script:RootsMountBase, $N) }

function Get-FstabLineForRoot {
    # Design 18.1 rule 1 (P1, 2026-09-29): `shared` is what makes Docker's view of the root a shared
    # mount after a distro start, so the Linux installer's rslave bind is accepted. Every WSL mount is
    # private otherwise. (Superseded: 5.5 rule 4's line, which ended `noatime,nofail 0 0` without it.)
    param([string]$WindowsPath, [int]$N)
    return ('{0} {1} drvfs uid=1000,gid=1000,noatime,nofail,shared 0 0' -f (ConvertTo-FstabSource $WindowsPath), (Get-RootMountPoint $N))
}

function Get-FstabBaseLine {
    # Design 22.14 (P4, 2026-09-30): the folder that holds every root's mount point, bound onto itself and
    # shared. When a root's folder or drive is missing at distro start, its drvfs line fails (nofail) and
    # its mount point is a plain directory on WSL's private `/`. Docker then refused the rslave bind
    # ("path /mnt/cognita-roots/1 is mounted on /mnt/c but it is not a shared or slave mount": Docker
    # matches mount points by string prefix) and Cognita did not start at all, although `start` was
    # written to go on and name the missing folder. Inside this shared bind the bind is accepted, Cognita
    # starts, the folder reads as unavailable, and the empty-walk guard keeps that project's index.
    return ('{0} {0} none bind,shared 0 0' -f $script:RootsMountBase)
}

function Get-FstabMountField {
    # Field 2 (the mount point) of one fstab line, or '' for a blank line or a comment.
    param([string]$Line)
    $trim = $Line.TrimStart()
    if ($trim.Length -eq 0 -or $trim.StartsWith('#')) { return '' }
    $fields = $trim -split '\s+'
    if ($fields.Count -ge 2) { return $fields[1] }
    return ''
}

function Update-FstabBaseText {
    <#
    Design 22.14. Like Update-FstabText, for the base line: exactly one line whose mount point is the
    roots folder, placed right before the first root line (appended when there is none yet; root lines
    are appended after it later). The order matters: fstab is mounted top to bottom, and a bind of the
    roots folder made AFTER the roots would sit on top of them and hide every one. Every other line is
    kept byte for byte; an identical base line keeps its own bytes (a trailing CR included). Returns
    @{ Text; Action = 'added'|'replaced'|'unchanged' }; 'replaced' covers wrong text, a duplicate, or a
    line in the wrong place.
    #>
    param([string]$ExistingText, [string]$NewLine)
    if ($null -eq $ExistingText) { $ExistingText = '' }
    $base = $script:RootsMountBase
    $kept = New-Object System.Collections.ArrayList
    $found = 0
    $same = $null
    foreach ($ln in ($ExistingText -split "`n", -1)) {
        if ((Get-FstabMountField $ln) -ceq $base) {
            $found++
            if ($null -eq $same -and $ln.TrimEnd("`r") -ceq $NewLine) { $same = $ln }
            continue
        }
        [void]$kept.Add($ln)
    }
    $put = $NewLine
    if ($null -ne $same) { $put = $same }
    $at = -1
    for ($i = 0; $i -lt $kept.Count; $i++) {
        if ((Get-FstabMountField $kept[$i]).StartsWith($base + '/')) { $at = $i; break }
    }
    if ($at -ge 0) { $kept.Insert($at, $put) }
    else {
        # Append after the last real line; keep the file newline-terminated.
        if ($kept.Count -gt 0 -and $kept[$kept.Count - 1] -ceq '') { $kept.RemoveAt($kept.Count - 1) }
        [void]$kept.Add($put)
        [void]$kept.Add('')
    }
    $text = $kept -join "`n"
    $action = 'replaced'
    if ($text -ceq $ExistingText) { $action = 'unchanged' } elseif ($found -eq 0) { $action = 'added' }
    return @{ Text = $text; Action = $action }
}

function Update-FstabText {
    <#
    Returns @{ Text; Action = 'added'|'replaced'|'unchanged' }. Lines are identified by their
    mount point (field 2). Every line that is not for $MountPoint is kept byte for byte
    (split on LF only, so CR bytes and a missing final newline survive). A second line for the
    same mount point is dropped: a mount point is never duplicated.
    #>
    param([string]$ExistingText, [string]$MountPoint, [string]$NewLine)
    if ($null -eq $ExistingText) { $ExistingText = '' }
    $lines = $ExistingText -split "`n", -1
    $out = New-Object System.Collections.ArrayList
    $seen = $false
    $action = 'unchanged'
    foreach ($ln in $lines) {
        $trim = $ln.TrimStart()
        $isOurs = $false
        if ($trim.Length -gt 0 -and -not $trim.StartsWith('#')) {
            $fields = $trim -split '\s+'
            if ($fields.Count -ge 2 -and $fields[1] -ceq $MountPoint) { $isOurs = $true }
        }
        if (-not $isOurs) { [void]$out.Add($ln); continue }
        if ($seen) { $action = 'replaced'; continue }
        $seen = $true
        if ($ln.TrimEnd("`r") -ceq $NewLine) { [void]$out.Add($ln) }
        else { [void]$out.Add($NewLine); $action = 'replaced' }
    }
    if (-not $seen) {
        # Append after the last real line; keep the file newline-terminated.
        if ($out.Count -gt 0 -and $out[$out.Count - 1] -ceq '') { $out.RemoveAt($out.Count - 1) }
        [void]$out.Add($NewLine)
        [void]$out.Add('')
        $action = 'added'
    }
    return @{ Text = ($out -join "`n"); Action = $action }
}

function Test-DisplayText {
    # The Linux CLI's display-text rule (its design 19.2): 1-400 printable characters, starts
    # with a letter, backslash or slash, no leading or trailing whitespace, no $, no double
    # quote, no # after whitespace. Returns $null when acceptable, else a reason and presentation ID.
    param([string]$Text)
    if ([string]::IsNullOrEmpty($Text)) { return [pscustomobject]@{ Reason = 'Choose a folder.'; ReasonId = 'setup.folder.choose' } }
    if ($Text.Length -gt 400) { return [pscustomobject]@{ Reason = 'This folder path is longer than Cognita can store (400 characters). Choose a folder with a shorter path.'; ReasonId = 'setup.folder.too_long' } }
    if ($Text -match '[\x00-\x1f\x7f]') { return [pscustomobject]@{ Reason = 'Cognita cannot use a folder whose path contains a control character or a line break. Choose or rename the folder.'; ReasonId = 'setup.folder.control_character' } }
    if ($Text.Contains('$') -or $Text.Contains('"') -or $Text -match '\s#') {
        return [pscustomobject]@{ Reason = 'Cognita cannot use a folder whose path contains $, a double quote, or a # after a space. Choose or rename the folder.'; ReasonId = 'setup.folder.unsupported_characters' }
    }
    if ($Text -ne $Text.Trim()) { return [pscustomobject]@{ Reason = 'Cognita cannot use a folder whose path starts or ends with a space. Choose or rename the folder.'; ReasonId = 'setup.folder.edge_spaces' } }
    if ($Text -notmatch '^[A-Za-z\\/]') { return [pscustomobject]@{ Reason = 'Choose a folder on one of this PC''s drives, for example C:\Users\me\Documents.'; ReasonId = 'setup.folder.full_path' } }
    return $null
}

function Get-OwnFolders {
    # Folders a projects folder may never contain or sit inside (design 5.5 rule 1).
    param($Settings)
    $list = @((Get-DataRoot))
    if ($Settings -and $Settings.vhd_dir) { $list += [string]$Settings.vhd_dir }
    return $list
}

function Test-RootPath {
    <#
    Design 5.5 rule 1. Returns [pscustomobject]@{ Ok; Reason; ReasonId; Path; Existing } where Path is the
    resolved folder (a junction or symbolic link is validated and mounted as its target) and
    Existing is the root number when the same folder is already a root.
    #>
    param([string]$Path, $Settings, [string]$ExtraOwnFolder = '')
    $bad = { param($why, $reasonId = '') [pscustomobject]@{ Ok = $false; Reason = $why; ReasonId = $reasonId; Path = $Path; Existing = 0 } }
    Write-Log ("root validate: input=[{0}]" -f $Path)
    if ([string]::IsNullOrWhiteSpace($Path)) { return (& $bad 'Choose a folder.' 'setup.folder.choose') }
    if ($Path -match "[\r\n\t]") { return (& $bad 'Cognita cannot use a folder whose path contains a line break or a tab. Choose or rename the folder.' 'setup.folder.control_character') }
    if ($Path.StartsWith('\\')) { return (& $bad 'Cognita supports folders on this PC''s own drives.' 'setup.folder.local_drive') }
    if ($Path -notmatch '^[A-Za-z]:[\\/]') { return (& $bad 'Choose a folder on one of this PC''s drives, for example C:\Users\me\Documents.' 'setup.folder.full_path') }
    $resolved = $null
    try { $resolved = Get-NormalizedPath $Path } catch { return (& $bad ('That is not a usable folder path: ' + $_.Exception.Message) 'setup.folder.invalid_path') }
    # A component that starts with # cannot be written to fstab (it would read as a comment).
    $comps = ($resolved.Substring(2) -split '\\') | Where-Object { $_ -ne '' }
    foreach ($c in $comps) {
        if ($c.StartsWith('#')) { return (& $bad 'Cognita cannot use a folder with a name that starts with #. Choose or rename the folder.' 'setup.folder.hash_name') }
    }
    if ($resolved -match '^[A-Za-z]:\\?$') { return (& $bad 'Choose a folder, not a whole drive.' 'setup.folder.drive_root') }
    if (-not (Test-DirectoryExists $resolved)) { return (& $bad 'That folder does not exist.' 'setup.folder.missing') }
    # Junction or symbolic link: validate and mount the target.
    for ($hop = 0; $hop -lt 8; $hop++) {
        $t = Get-ReparseTarget $resolved
        if (-not $t) { break }
        Write-Log ("root validate: {0} is a link, target {1}" -f $resolved, $t)
        if ($t.StartsWith('\\')) { return (& $bad 'Cognita supports folders on this PC''s own drives.' 'setup.folder.local_drive') }
        try { $resolved = Get-NormalizedPath $t } catch { return (& $bad 'That folder is a link to a place Cognita cannot use.' 'setup.folder.link_unusable') }
        if ($resolved -notmatch '^[A-Za-z]:\\') { return (& $bad 'That folder is a link to a place Cognita cannot use.' 'setup.folder.link_unusable') }
    }
    if ($resolved -match '^[A-Za-z]:\\?$') { return (& $bad 'Choose a folder, not a whole drive.' 'setup.folder.drive_root') }
    if (-not (Test-DirectoryExists $resolved)) { return (& $bad 'That folder does not exist.' 'setup.folder.missing') }
    $dt = Get-DriveTypeName $resolved.Substring(0, 1)
    Write-Log ("root validate: drive {0} type={1}" -f $resolved.Substring(0, 1), $dt)
    if ($dt -eq 'Network') { return (& $bad 'Cognita supports folders on this PC''s own drives, not mapped network drives.' 'setup.folder.network_drive') }
    if ($dt -ne 'Fixed' -and $dt -ne 'Removable') { return (& $bad 'Cognita supports folders on this PC''s own drives.' 'setup.folder.local_drive') }
    $displayWhy = Test-DisplayText $resolved
    if ($displayWhy) { return (& $bad $displayWhy.Reason $displayWhy.ReasonId) }
    $own = @(Get-OwnFolders $Settings)
    if ($ExtraOwnFolder) { $own += $ExtraOwnFolder }
    foreach ($o in $own) {
        if ($o -and (Test-PathNests $resolved $o)) {
            return (& $bad ("Choose a folder outside Cognita's own data folders ({0})." -f $o) 'setup.folder.data_overlap')
        }
    }
    $existing = 0
    foreach ($r in (Get-SettingsRoots $Settings)) {
        if ($resolved -ieq (Get-NormalizedPath ([string]$r.windows))) { $existing = [int]$r.n; continue }
        if (Test-PathNests $resolved ([string]$r.windows)) {
            return (& $bad ("This folder overlaps another projects folder Cognita already uses ({0}). Choose a folder that neither contains nor sits inside it." -f $r.windows) 'setup.folder.projects_overlap')
        }
    }
    Write-Log ("root validate: OK resolved=[{0}] existing={1}" -f $resolved, $existing)
    return [pscustomobject]@{ Ok = $true; Reason = ''; ReasonId = ''; Path = $resolved; Existing = $existing }
}

# ---------------------------------------------------------------------------------------
# WSL state, distro state and ownership (design 5.3, 5.4, 5.7)
# ---------------------------------------------------------------------------------------
function Get-WslState {
    # @{ State = 'present'|'old'|'missing'; Version }. Not a failure: Setup offers the WSL page.
    # Proven: the inbox stub exits non-zero and prints "not installed" (design 5.3).
    $r = Invoke-External -FilePath (Get-WslExe) -Arguments @('--version') -TimeoutSec 30
    $text = Remove-NulAndBom ($r.Stdout + "`n" + $r.Stderr)
    if ($r.StartError) {
        Write-Log 'wsl state: wsl.exe could not be started -> missing'
        return [pscustomobject]@{ State = 'missing'; Version = '' }
    }
    if ($r.ExitCode -eq 0) {
        if ($text -match '(\d+)\.(\d+)\.(\d+)') {
            $ver = '{0}.{1}.{2}' -f $Matches[1], $Matches[2], $Matches[3]
            if ([int]$Matches[1] -ge 2) {
                Write-Log ("wsl state: present version={0}" -f $ver)
                return [pscustomobject]@{ State = 'present'; Version = $ver }
            }
            Write-Log ("wsl state: old version={0}" -f $ver)
            return [pscustomobject]@{ State = 'old'; Version = $ver }
        }
        Write-Log 'wsl state: --version exited 0 but printed no version number -> old'
        return [pscustomobject]@{ State = 'old'; Version = '' }
    }
    $s = Invoke-External -FilePath (Get-WslExe) -Arguments @('--status') -TimeoutSec 30
    if (-not $s.StartError -and $s.ExitCode -eq 0) {
        Write-Log 'wsl state: --version unknown but --status works -> old'
        return [pscustomobject]@{ State = 'old'; Version = '' }
    }
    Write-Log 'wsl state: neither --version nor --status works -> missing'
    return [pscustomobject]@{ State = 'missing'; Version = '' }
}

function Test-RestartPending {
    # Proven signal (spike S2): this key, readable without admin, exists after wsl --install
    # even though the command exits 0 and `wsl --version` already works.
    return (Test-RegistryKey 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Component Based Servicing\RebootPending')
}

function Test-DistroRunning {
    param($Settings)
    $name = Get-DistroName $Settings
    $r = Invoke-External -FilePath (Get-WslExe) -Arguments @('--list', '--running', '--quiet') -TimeoutSec 30
    if ($r.StartError) { return $false }
    $lines = (Remove-NulAndBom $r.Stdout) -split "`r?`n"
    foreach ($l in $lines) {
        if ($l.Trim() -ieq $name) { Write-Log ("distro {0} is running" -f $name); return $true }
    }
    Write-Log ("distro {0} is not running" -f $name)
    return $false
}

function Get-DistroMarker {
    # Reads /etc/cognita-distro as root. This starts a stopped distro, so the callers that must
    # never start it (status) do not use it.
    param($Settings)
    $r = Invoke-Wsl -Settings $Settings -User 'root' -Command @('cat', '/etc/cognita-distro') -TimeoutSec 90
    if ($r.StartError -or $r.TimedOut) { return [pscustomobject]@{ Status = 'unreadable'; Id = '' } }
    if ($r.ExitCode -eq 0) {
        try {
            $o = (Remove-NulAndBom $r.Stdout) | ConvertFrom-Json
            if ($o.installation_id) { return [pscustomobject]@{ Status = 'id'; Id = [string]$o.installation_id } }
        } catch { Write-Log ("marker parse failed: {0}" -f $_.Exception.Message) }
        return [pscustomobject]@{ Status = 'unreadable'; Id = '' }
    }
    if (($r.Stderr + $r.Stdout) -match 'No such file') { return [pscustomobject]@{ Status = 'absent'; Id = '' } }
    return [pscustomobject]@{ Status = 'unreadable'; Id = '' }
}

function Get-DistroOwnership {
    <#
    Design 5.7 step 4, used by every verb that touches the distro:
    the Lxss key's BasePath equals settings.vhd_dir, and (once written) /etc/cognita-distro's id
    equals installation_id. A pending record + a matching BasePath + no marker yet = an
    interrupted import = ours. Anything else named like ours is foreign.
    -SkipMarker: never start the distro; BasePath alone decides.
    #>
    param($Settings, [switch]$SkipMarker)
    $name = Get-DistroName $Settings
    $d = $null
    foreach ($x in (Get-LxssDistros)) { if ($x.Name -ieq $name) { $d = $x; break } }
    if (-not $d) {
        Write-Log ("ownership: no distro named {0}" -f $name)
        return [pscustomobject]@{ Exists = $false; Owned = $false; Reason = 'absent'; Guid = ''; BasePath = '' }
    }
    $mk = { param($owned, $why) Write-Log ("ownership: distro {0} owned={1} ({2})" -f $name, $owned, $why); [pscustomobject]@{ Exists = $true; Owned = $owned; Reason = $why; Guid = $d.Guid; BasePath = $d.BasePath } }
    if (-not $Settings -or -not $Settings.vhd_dir) { return (& $mk $false 'no record of this install on this PC') }
    $baseOk = $false
    try { $baseOk = ((Get-NormalizedPath $d.BasePath) -ieq (Get-NormalizedPath ([string]$Settings.vhd_dir))) } catch { $baseOk = $false }
    if (-not $baseOk) { return (& $mk $false ('its disk folder {0} is not this install''s {1}' -f $d.BasePath, $Settings.vhd_dir)) }
    if ($SkipMarker) { return (& $mk $true 'disk folder matches; marker not read') }
    $m = Get-DistroMarker -Settings $Settings
    if ($m.Status -eq 'id') {
        if ($m.Id -ieq [string]$Settings.installation_id) { return (& $mk $true 'marker matches') }
        return (& $mk $false 'its marker belongs to a different install')
    }
    if ($m.Status -eq 'absent') {
        if ($Settings.state -eq 'import-pending') { return (& $mk $true 'interrupted import: pending record, disk folder matches, no marker yet') }
        return (& $mk $false 'no marker although the install record says it finished')
    }
    return (& $mk $true 'disk folder matches; marker could not be read')
}

# ---------------------------------------------------------------------------------------
# check (design 5.3): all checks run, failures are listed together
# ---------------------------------------------------------------------------------------
function New-CheckResult {
    param([string]$Id, [string]$Title, [ValidateSet('ok', 'warn', 'fail')][string]$State, [string]$Message = '', [string]$Fix = '', [string]$MessageId = '', $Values = $null)
    return [pscustomobject]@{ Id = $Id; Title = $Title; State = $State; Message = $Message; Fix = $Fix; MessageId = $MessageId; Values = $Values }
}

function ConvertFrom-WslConfig {
    # Keys of the [wsl2] section, lower-cased. This function only reads. (Superseded history: it used to say
    # ".wslconfig is never written". Since design 22.9 Setup, when the user ticks the box on the Ready page,
    # adds exactly one line, autoMemoryReclaim=dropCache, through Set-WslReclaimSetting below: the only
    # writer of this file, and it keeps every other byte.)
    param([string]$Text)
    $h = @{}
    if (-not $Text) { return $h }
    $section = ''
    foreach ($raw in ($Text -split "`r?`n")) {
        $l = $raw.Trim()
        if ($l -eq '' -or $l.StartsWith('#') -or $l.StartsWith(';')) { continue }
        if ($l -match '^\[(.+)\]$') { $section = $Matches[1].Trim().ToLowerInvariant(); continue }
        if ($section -ne 'wsl2') { continue }
        $i = $l.IndexOf('=')
        if ($i -gt 0) { $h[$l.Substring(0, $i).Trim().ToLowerInvariant()] = $l.Substring($i + 1).Trim() }
    }
    return $h
}

function ConvertFrom-WslSize {
    # 8GB, 4096MB, 512KB, 1TB or a plain byte count (WSL's units are binary). $null when unreadable.
    param([string]$Text)
    if ($Text -match '^\s*(\d+(\.\d+)?)\s*(KB|MB|GB|TB|B)?\s*$') {
        $n = [double]$Matches[1]
        switch ($Matches[3].ToUpperInvariant()) {
            'KB' { return [int64]($n * 1KB) }
            'MB' { return [int64]($n * 1MB) }
            'GB' { return [int64]($n * 1GB) }
            'TB' { return [int64]($n * 1TB) }
            default { return [int64]$n }
        }
    }
    return $null
}

# ---------------------------------------------------------------------------------------
# NVIDIA card detection (design 22.2)
# ---------------------------------------------------------------------------------------
function ConvertTo-NvidiaDriverVersion {
    # The Windows (WDDM) driver version of an NVIDIA card, "32.0.15.8092", is NVIDIA's own version in
    # disguise: field 3's LAST digit and field 4 (left-padded to 4 digits) make 5 digits, and those read as
    # <first 3>.<last 2>. 32.0.15.8092 -> 58092 -> 580.92; 31.0.15.5222 -> 552.22; 27.21.14.5671 -> 456.71.
    # Anything that does not have exactly 4 numeric fields (or a field 4 longer than 4 digits) is unknown: ''.
    param([string]$Wddm)
    $parts = @(([string]$Wddm).Trim() -split '\.')
    if ($parts.Count -ne 4) { return '' }
    foreach ($p in $parts) { if ($p -notmatch '^\d{1,9}$') { return '' } }
    if ($parts[3].Length -gt 4) { return '' }
    $digits = $parts[2].Substring($parts[2].Length - 1) + $parts[3].PadLeft(4, '0')
    return ('{0}.{1}' -f [int]$digits.Substring(0, 3), $digits.Substring(3, 2))
}

function Get-NvidiaCard {
    # Design 22.2. Returns [pscustomobject]@{ State; Name; Driver; Source }:
    #   State  none = no NVIDIA card seen; ok = a card with driver >= 580.00, or a card whose driver version
    #          could not be read (unknown is not held against the card, as on Linux); old = every NVIDIA
    #          card seen has a readable driver below 580.
    #   Source nvidia-smi | wmi | none (for the log).
    # nvidia-smi first (it reports NVIDIA's own driver version), then the CIM video controllers. Never throws:
    # every failure is logged and ends as "none" or a fall-through to the next source.
    $rows = New-Object System.Collections.ArrayList
    $source = 'none'
    $notes = New-Object System.Collections.ArrayList
    try {
        $smi = Get-NvidiaSmiPath
        if (-not $smi) {
            [void]$notes.Add('nvidia-smi.exe not present')
        } else {
            $r = Invoke-External -FilePath $smi -Arguments @('--query-gpu=name,driver_version', '--format=csv,noheader') -TimeoutSec 20
            if ($r.TimedOut) { [void]$notes.Add('nvidia-smi timed out') }
            elseif ($r.StartError) { [void]$notes.Add(('nvidia-smi did not start: ' + $r.StartError)) }
            elseif ($r.ExitCode -ne 0) { [void]$notes.Add(('nvidia-smi exit ' + $r.ExitCode)) }
            else {
                $skipped = 0
                foreach ($raw in ((Remove-NulAndBom $r.Stdout) -split "`r?`n")) {
                    $l = $raw.Trim()
                    if (-not $l) { continue }
                    $i = $l.LastIndexOf(',')
                    if ($i -le 0) { $skipped++; continue }
                    $name = $l.Substring(0, $i).Trim()
                    $ver = $l.Substring($i + 1).Trim()
                    if (-not $name) { $skipped++; continue }
                    $driver = ''
                    if ($ver -match '^(\d+)\.(\d+)') { $driver = ($Matches[1] + '.' + $Matches[2]) }
                    [void]$rows.Add([pscustomobject]@{ Name = $name; Driver = $driver })
                }
                if ($rows.Count -eq 0) { [void]$notes.Add(('nvidia-smi gave no parseable row (skipped ' + $skipped + ' line(s))')) }
                else { $source = 'nvidia-smi' }
            }
        }
    } catch {
        [void]$notes.Add(('nvidia-smi step threw: ' + $_.Exception.Message))
    }
    if ($rows.Count -eq 0) {
        try {
            foreach ($c in @(Get-VideoControllers)) {
                if ([string]$c.Name -notmatch '(?i)NVIDIA') { continue }
                [void]$rows.Add([pscustomobject]@{ Name = [string]$c.Name; Driver = (ConvertTo-NvidiaDriverVersion ([string]$c.DriverVersion)) })
            }
            if ($rows.Count -gt 0) { $source = 'wmi' } else { [void]$notes.Add('no NVIDIA row in the video controllers') }
        } catch {
            [void]$notes.Add(('video controller read failed: ' + $_.Exception.Message))
        }
    }
    $state = 'none'; $name = ''; $driver = ''
    if ($rows.Count -gt 0) {
        $state = 'old'
        $pick = $rows[0]
        foreach ($row in $rows) {
            $isOk = $true
            if ($row.Driver) { $isOk = ([int](($row.Driver -split '\.')[0]) -ge 580) }
            if ($isOk) { $state = 'ok'; $pick = $row; break }
        }
        $name = $pick.Name; $driver = $pick.Driver
    }
    $extra = ''
    if ($notes.Count -gt 0) { $extra = (' notes=[{0}]' -f ($notes -join '; ')) }
    Write-Log ("nvidia: source={0} state={1} name=[{2}] driver=[{3}] rows={4}{5}" -f $source, $state, $name, $driver, $rows.Count, $extra)
    return [pscustomobject]@{ State = $state; Name = $name; Driver = $driver; Source = $source }
}

# ---------------------------------------------------------------------------------------
# .wslconfig: Windows may take back the memory WSL holds as file cache (design 22.9)
# ---------------------------------------------------------------------------------------
$script:WslReclaimLine = 'autoMemoryReclaim=dropCache'

function Find-WslConfigKeySection {
    # The section ('experimental' or 'wsl2', lower-cased) in which $Key (any case, any value) is set,
    # '' when it is in neither. Section-aware: a key of the same name under another header does not count.
    param([string]$Text, [string]$Key, [string[]]$Sections)
    if (-not $Text) { return '' }
    $section = ''
    foreach ($raw in ($Text -split "`r?`n|`r")) {
        $l = $raw.Trim()
        if ($l -eq '' -or $l.StartsWith('#') -or $l.StartsWith(';')) { continue }
        if ($l -match '^\[(.+)\]$') { $section = $Matches[1].Trim().ToLowerInvariant(); continue }
        if ($Sections -notcontains $section) { continue }
        $i = $l.IndexOf('=')
        if ($i -gt 0 -and $l.Substring(0, $i).Trim().ToLowerInvariant() -eq $Key.ToLowerInvariant()) { return $section }
    }
    return ''
}

function Get-WslReclaimSetting {
    # 'set' when autoMemoryReclaim (any case, any value, "disabled" included) is in [experimental] or
    # [wsl2]; 'unset' when it is not, or there is no file; 'unreadable' when the file exists but cannot be
    # read. Preflight reports it as wsl_reclaim; Set-WslReclaimSetting re-reads it before writing.
    $text = Get-WslConfigText
    if ($null -eq $text) {
        $p = Get-WslConfigPath
        if (Test-Path -LiteralPath $p) {
            Write-Log ("wsl_reclaim: [{0}] exists but could not be read; unreadable" -f $p)
            return 'unreadable'
        }
        Write-Log 'wsl_reclaim: no .wslconfig; unset'
        return 'unset'
    }
    $sec = Find-WslConfigKeySection -Text $text -Key 'autoMemoryReclaim' -Sections @('experimental', 'wsl2')
    $v = 'unset'; if ($sec) { $v = 'set' }
    Write-Log ("wsl_reclaim: {0} (autoMemoryReclaim found in section [{1}], file is {2} chars)" -f $v, $sec, $text.Length)
    return $v
}

function Set-WslReclaimSetting {
    # Design 22.9: adds EXACTLY ONE line, autoMemoryReclaim=dropCache, to the user's .wslconfig, and nothing
    # else. Run FIRST by the install and update verbs (before the import or anything in the distro), only
    # when Setup passed --wsl-memory-reclaim.
    #   1. Reads the file again: when the key is there now, nothing is written.
    #   2. Copies the file to .wslconfig.cognita-backup (overwritten each time; no file, no backup).
    #   3. The line goes first after an existing [experimental] header; with none, a new [experimental]
    #      section is appended (one blank line before it). A new file holds just that section.
    #   4. Every other byte is kept: a BOM stays a BOM (UTF-8 or UTF-16), the file's own line endings (LF
    #      for a new file), order and comments. The body is edited as Latin-1 text, which maps every byte
    #      to one character and back, so bytes that are not valid UTF-8 survive untouched.
    #   5. Written to a temp file beside it, then moved over it.
    # A failure is a WARNING (stage wslconfig) and never stops the install. Returns
    # [pscustomobject]@{ Status = added | already-set | failed; Section; Detail }.
    $path = Get-WslConfigPath
    $backup = $path + '.cognita-backup'
    $tmp = $path + '.cognita-tmp'
    try {
        $cur = Get-WslReclaimSetting
        if ($cur -eq 'set') {
            Write-Log ("wslconfig: autoMemoryReclaim is already set in [{0}]; nothing written" -f $path)
            return [pscustomobject]@{ Status = 'already-set'; Section = ''; Detail = '' }
        }
        if ($cur -eq 'unreadable') { throw 'the file could not be read' }
        $exists = Test-Path -LiteralPath $path
        $bytes = New-Object byte[] 0
        if ($exists) { $bytes = [System.IO.File]::ReadAllBytes($path) }
        $bodyStart = 0
        $encName = 'utf-8'
        $enc = [System.Text.Encoding]::GetEncoding(28591)
        if ($bytes.Length -ge 3 -and $bytes[0] -eq 0xEF -and $bytes[1] -eq 0xBB -and $bytes[2] -eq 0xBF) { $bodyStart = 3; $encName = 'utf-8-bom' }
        elseif ($bytes.Length -ge 2 -and $bytes[0] -eq 0xFF -and $bytes[1] -eq 0xFE) { $bodyStart = 2; $encName = 'utf-16le-bom'; $enc = New-Object System.Text.UnicodeEncoding($false, $false) }
        elseif ($bytes.Length -ge 2 -and $bytes[0] -eq 0xFE -and $bytes[1] -eq 0xFF) { $bodyStart = 2; $encName = 'utf-16be-bom'; $enc = New-Object System.Text.UnicodeEncoding($true, $false) }
        $prefix = New-Object byte[] $bodyStart
        if ($bodyStart -gt 0) { [Array]::Copy($bytes, 0, $prefix, 0, $bodyStart) }
        $body = $enc.GetString($bytes, $bodyStart, $bytes.Length - $bodyStart)
        $eol = "`n"
        $m = [regex]::Match($body, "\r\n|\n|\r")
        if ($m.Success) { $eol = $m.Value }
        $hdr = [regex]::Match($body, '(?im)^[ \t]*\[[ \t]*experimental[ \t]*\][ \t]*(\r\n|\n|\r|\z)')
        $section = ''
        if ($hdr.Success) {
            $section = 'existing [experimental]'
            if ($hdr.Groups[1].Value -ne '') { $newBody = $body.Insert($hdr.Index + $hdr.Length, $script:WslReclaimLine + $eol) }
            else { $newBody = $body + $eol + $script:WslReclaimLine }
        } else {
            $section = 'new [experimental]'
            $nb = $body
            if ($nb.Length -gt 0) {
                if (-not ($nb.EndsWith("`n") -or $nb.EndsWith("`r"))) { $nb += $eol }
                if (-not $nb.EndsWith($eol + $eol)) { $nb += $eol }
            }
            $newBody = $nb + '[experimental]' + $eol + $script:WslReclaimLine + $eol
        }
        $nbytes = $enc.GetBytes($newBody)
        $ms = New-Object System.IO.MemoryStream
        $ms.Write($prefix, 0, $prefix.Length)
        $ms.Write($nbytes, 0, $nbytes.Length)
        $outBytes = $ms.ToArray()
        if ($exists) { [System.IO.File]::Copy($path, $backup, $true) }
        [System.IO.File]::WriteAllBytes($tmp, $outBytes)
        if ($exists) { [System.IO.File]::Replace($tmp, $path, [NullString]::Value) }
        else { [System.IO.File]::Move($tmp, $path) }
        $eolName = 'lf'; if ($eol -eq "`r`n") { $eolName = 'crlf' } elseif ($eol -eq "`r") { $eolName = 'cr' }
        $bk = 'none'; if ($exists) { $bk = $backup }
        Write-Log ("wslconfig: added {0} section={1} encoding={2} eol={3} bytes={4}->{5} backup=[{6}]" -f $script:WslReclaimLine, $section, $encName, $eolName, $bytes.Length, $outBytes.Length, $bk)
        return [pscustomobject]@{ Status = 'added'; Section = $section; Detail = '' }
    } catch {
        $why = $_.Exception.Message
        Write-Log ("wslconfig: could not add {0} to [{1}]: {2}" -f $script:WslReclaimLine, $path, $why)
        try { if (Test-Path -LiteralPath $tmp -PathType Leaf) { Remove-Item -LiteralPath $tmp -Force } }
        catch { Write-Log ("wslconfig: temp file cleanup failed: {0}" -f $_.Exception.Message) }
        Write-ProgressLine -Stage 'wslconfig' -Title 'WSL memory settings' -State 'warning' -Message ("Setup could not change your WSL settings ({0}). Cognita works without it; WSL just keeps more memory." -f (Limit-LogText $why 200))
        return [pscustomobject]@{ Status = 'failed'; Section = ''; Detail = $why }
    }
}

function Get-CheckResults {
    param([string]$Phase, $Opts, $Settings)
    $res = New-Object System.Collections.ArrayList

    # Windows
    $os = Get-OsInfo
    if ($os.Is64 -and $os.Build -ge 22000) {
        [void]$res.Add((New-CheckResult 'windows' 'Windows version' 'ok'))
    } else {
        [void]$res.Add((New-CheckResult 'windows' 'Windows version' 'fail' 'Cognita needs 64-bit Windows 11.' 'Update Windows, then run Setup again.'))
    }

    # Virtualization: advisory only, both flags read true inside VMs; the import decides.
    $v = Get-VirtualizationInfo
    if ($v.Hypervisor -or $v.Firmware) {
        [void]$res.Add((New-CheckResult 'virtualization' 'Virtualization' 'ok'))
    } else {
        [void]$res.Add((New-CheckResult 'virtualization' 'Virtualization' 'warn' 'Windows does not report hardware virtualization.' 'If Setup stops later, turn on virtualization (Intel VT-x or AMD-V/SVM) in your PC''s BIOS/UEFI settings. On a virtual machine, turn on nested virtualization.'))
    }

    # WSL: never a failure
    $wsl = Get-WslState
    [void]$res.Add((New-CheckResult 'wsl' 'Windows Subsystem for Linux' 'ok' ('wsl=' + $wsl.State)))

    # Restart pending (not right after our own wsl-install, which is expected to set it)
    if (Get-Opt $Opts 'after-wsl-install' $false) {
        Write-Log 'check: restart-pending not evaluated (--after-wsl-install)'
    } elseif (Test-RestartPending) {
        [void]$res.Add((New-CheckResult 'restart' 'Pending restart' 'fail' 'Windows is waiting for a restart.' 'Restart, then run Setup again.'))
    } else {
        [void]$res.Add((New-CheckResult 'restart' 'Pending restart' 'ok'))
    }

    # .wslconfig networking and memory (read only)
    $cfgText = Get-WslConfigText
    $cfg = ConvertFrom-WslConfig $cfgText
    if ($cfg.ContainsKey('localhostforwarding') -and $cfg['localhostforwarding'] -match '^(false|0|no|off)$') {
        [void]$res.Add((New-CheckResult 'wslconfig' 'WSL networking' 'fail' 'Your WSL settings turn off localhost forwarding, so Windows could not reach Cognita.' 'Remove localhostForwarding=false from %USERPROFILE%\.wslconfig.'))
    } else {
        [void]$res.Add((New-CheckResult 'wslconfig' 'WSL networking' 'ok'))
    }

    # Distro name
    $own = Get-DistroOwnership -Settings $Settings -SkipMarker
    if ($own.Exists -and -not $own.Owned) {
        [void]$res.Add((New-CheckResult 'distro' 'Distro name' 'fail' 'A WSL distro named Cognita already exists and was not created by this Setup.' 'Rename or remove it, then run Setup again.'))
    } else {
        [void]$res.Add((New-CheckResult 'distro' 'Distro name' 'ok'))
    }

    # Docker Desktop: a problem only when its WSL integration is turned on for Cognita's own distro
    # (then it replaces the distro's docker and Cognita cannot start). Installed but not integrated
    # is fine and says nothing: a user is never told what they do not have to do.
    $dd = Get-DockerDesktopInfo
    $distroName = 'Cognita'
    if ($Settings -and $Settings.PSObject.Properties['distro'] -and $Settings.distro) { $distroName = [string]$Settings.distro }
    if ($dd.Installed -and ($dd.IntegratedDistros -contains $distroName)) {
        [void]$res.Add((New-CheckResult 'docker' 'Docker Desktop' 'fail' ("Docker Desktop's WSL integration is turned on for {0}, which takes over its Docker." -f $distroName) ("In Docker Desktop, open Settings > Resources > WSL integration, untick {0}, apply, then try again." -f $distroName)))
    } else {
        [void]$res.Add((New-CheckResult 'docker' 'Docker Desktop' 'ok'))
    }

    # Memory: 16 GB PC (WSL gets half by default and the Linux CLI refuses under 7.5 GiB)
    $mem = Get-MemoryBytes
    $cfgMem = $null
    if ($cfg.ContainsKey('memory')) { $cfgMem = ConvertFrom-WslSize $cfg['memory'] }
    if ($null -ne $cfgMem) {
        $pass = ($cfgMem -ge (8 * 1GB))
        Write-Log ("check memory: .wslconfig memory={0} bytes total={1} bytes pass={2}" -f $cfgMem, $mem, $pass)
    } else {
        $pass = ($mem -ge 16000000000)
        Write-Log ("check memory: total={0} bytes pass={1}" -f $mem, $pass)
    }
    if ($pass) { [void]$res.Add((New-CheckResult 'memory' 'Memory' 'ok')) }
    else { [void]$res.Add((New-CheckResult 'memory' 'Memory' 'fail' 'Cognita needs a PC with at least 16 GB of memory.' 'Run Cognita on a PC with 16 GB or more, or give WSL at least 8 GB in %USERPROFILE%\.wslconfig.')) }

    if ($Phase -eq 'final') {
        # Disk at the data folder (vhd_dir). Setup computes the requirement from its compiled-in sizes
        # (Linux design 6.3's formula: downloads x 4 + models + 3.5 GB, plus 1 GB for the image) and
        # passes it as --disk-bytes. Without it only the fixed 4.5 GB floor of that formula is checked.
        $vhd = [string](Get-Opt $Opts 'data-dir' '')
        if (-not $vhd -and $Settings) { $vhd = [string]$Settings.vhd_dir }
        if (-not $vhd) { $vhd = Get-DefaultVhdDir }
        $need = [int64](Get-Opt $Opts 'disk-bytes' 0)
        if ($need -le 0) { $need = [int64](3.5 * 1GB) + 1GB; Write-Log 'check disk: no --disk-bytes given, using the 4.5 GB floor' }
        $free = Get-FreeSpaceBytes $vhd
        Write-Log ("check disk: data dir={0} free={1} need={2}" -f $vhd, $free, $need)
        if ($free -ge 0 -and $free -lt $need) {
            $root = [System.IO.Path]::GetPathRoot((Get-NormalizedPath $vhd))
            [void]$res.Add((New-CheckResult 'disk' 'Free disk space' 'fail' ('Setup needs {0} GB free on {1}.' -f [Math]::Ceiling($need / 1GB), $root) 'Free space, or choose another data location under Advanced.'))
        } else {
            [void]$res.Add((New-CheckResult 'disk' 'Free disk space' 'ok'))
        }
        # Ports: nothing may listen on Windows unless it is this install
        $mcp = [int](Get-Opt $Opts 'mcp-port' 0); if (-not $mcp -and $Settings) { $mcp = [int]$Settings.mcp_port }; if (-not $mcp) { $mcp = 8675 }
        $adm = [int](Get-Opt $Opts 'admin-port' 0); if (-not $adm -and $Settings) { $adm = [int]$Settings.admin_port }; if (-not $adm) { $adm = 8676 }
        $listen = @(Get-ListeningPorts)
        $bad = New-Object System.Collections.ArrayList
        foreach ($pair in @(@($mcp, 'mcp_port'), @($adm, 'admin_port'))) {
            $port = [int]$pair[0]
            $l = $listen | Where-Object { $_.Port -eq $port } | Select-Object -First 1
            if (-not $l) { continue }
            $ours = ($Settings -and $Settings.state -eq 'installed' -and [int]$Settings.($pair[1]) -eq $port)
            Write-Log ("check ports: port {0} listened by [{1}] pid={2} ours={3}" -f $port, $l.Process, $l.ProcessId, $ours)
            if (-not $ours) {
                $who = $l.Process; if (-not $who) { $who = 'another program' }
                [void]$res.Add((New-CheckResult 'ports' 'Ports' 'fail' ('Port {0} is in use by {1}.' -f $port, $who) 'Choose other ports under Advanced.' 'setup.check.ports_in_use' @{ port = $port; process = $who }))
                [void]$bad.Add($port)
            }
        }
        if ($bad.Count -eq 0) { [void]$res.Add((New-CheckResult 'ports' 'Ports' 'ok')) }
    }
    return @($res)
}

function Invoke-CheckVerb {
    param($Opts)
    $phase = [string](Get-Opt $Opts 'phase' 'preflight')
    if ($phase -ne 'preflight' -and $phase -ne 'final') { return (New-VerbResult 'failed' ([ordered]@{ error = 'phase must be preflight or final' })) }
    $settings = Read-Settings
    Write-ProgressLine -Stage 'check' -Title ('Checking this PC (' + $phase + ')') -State 'start'
    $results = @(Get-CheckResults -Phase $phase -Opts $Opts -Settings $settings)
    $failures = 0; $warnings = 0; $wslState = 'missing'
    foreach ($r in $results) {
        switch ($r.State) {
            'ok' { Write-ProgressLine -Stage ('check.' + $r.Id) -Title $r.Title -State 'done' }
            'warn' { $warnings++; Write-ProgressLine -Stage ('check.' + $r.Id) -Title $r.Title -State 'warning' -Message $r.Message -Fix $r.Fix -MessageId $r.MessageId -Values $r.Values }
            'fail' { $failures++; Write-ProgressLine -Stage ('check.' + $r.Id) -Title $r.Title -State 'failed' -Message $r.Message -Fix $r.Fix -MessageId $r.MessageId -Values $r.Values }
        }
        if ($r.Id -eq 'wsl') { $wslState = ($r.Message -replace '^wsl=', '') }
    }
    Write-Log ("check summary: phase={0} failures={1} warnings={2} wsl={3}" -f $phase, $failures, $warnings, $wslState)
    # Design 19.8 item 19: the `resume=after-wsl` marker is only meaningful until WSL is really there. Once
    # a preflight sees WSL present, the restart it was waiting for has happened, so it is cleared HERE, not
    # only by a later install: a Setup that was abandoned after the restart otherwise found the marker on
    # every later run and showed "continuing after the restart" forever. Only the preflight phase does it
    # (the final check runs at the end of an install, when the marker is already gone or irrelevant), and
    # only that one marker value, so nothing else stored in `resume` is touched.
    if ($phase -eq 'preflight' -and $wslState -eq 'present' -and $settings -and [string]$settings.resume -eq 'after-wsl') {
        Set-SettingProp $settings 'resume' $null
        Save-Settings $settings
        Write-Log 'check: WSL is present, so the resume=after-wsl marker was cleared'
    } elseif ($settings -and [string]$settings.resume) {
        Write-Log ("check: resume marker [{0}] left as it is (phase={1} wsl={2})" -f $settings.resume, $phase, $wslState)
    }
    if ($failures -eq 0) {
        Write-ProgressLine -Stage 'check' -Title 'Checking this PC' -State 'done'
    } else {
        # Setup shows every failed line's message and fix. A closing "N problem(s) were found. Fix the
        # problems listed above" failed line only repeated them (seen on the VM with one pending-restart
        # problem, 2026-09-29), so the count goes to the log and the result, not on screen.
        Write-Log ('check: {0} problem(s) found, {1} warning(s)' -f $failures, $warnings)
    }
    $vals = [ordered]@{ wsl = $wslState; failures = $failures; warnings = $warnings }
    if ($phase -eq 'preflight') {
        # Design 22.2 / 22.9: facts Setup uses to decide its Acceleration page and the Ready page's .wslconfig
        # box. Not check results: no progress line, never a warning or a failure. The final phase does not
        # detect again. Neither function throws (each logs what it found).
        $nv = Get-NvidiaCard
        $vals['nvidia'] = $nv.State
        $vals['nvidia_name'] = $nv.Name
        $vals['nvidia_driver'] = $nv.Driver
        $vals['wsl_reclaim'] = (Get-WslReclaimSetting)
        Write-Log ("check preflight facts: nvidia={0} nvidia_name=[{1}] nvidia_driver=[{2}] wsl_reclaim={3}" -f $vals['nvidia'], $vals['nvidia_name'], $vals['nvidia_driver'], $vals['wsl_reclaim'])
    }
    if ($failures -gt 0) { return (New-VerbResult 'failed' $vals) }
    return (New-VerbResult 'ok' $vals)
}

# ---------------------------------------------------------------------------------------
# wsl-install (design 5.4)
# ---------------------------------------------------------------------------------------
function Test-UacDeclined {
    param($R)
    $t = (Remove-NulAndBom ($R.Stdout + ' ' + $R.Stderr))
    return ($R.ExitCode -eq 1223 -or $R.ExitCode -eq -2147023673 -or $t -match 'ERROR_CANCELLED|canceled by the user|cancelled by the user|0x800704c7')
}

function Invoke-WslInstallVerb {
    param($Opts)
    $st = Get-WslState
    Write-Log ("wsl-install: starting state={0} version={1}" -f $st.State, $st.Version)
    if ($st.State -eq 'present') {
        Write-ProgressLine -Stage 'wsl' -Title 'Windows Subsystem for Linux' -State 'done' -Message 'WSL is already installed.'
        return (New-VerbResult 'ok' ([ordered]@{ wsl = 'present'; restart = 0 }))
    }
    $beat = { Write-ProgressLine -Stage 'wsl' -Title 'Turning on WSL' -State 'progress' -Message 'Still working. Windows may be asking for your permission.' }
    if ($st.State -eq 'old') {
        # Step 1: wsl --update elevates itself.
        Write-ProgressLine -Stage 'wsl' -Title 'Updating WSL' -State 'start'
        $u = Invoke-External -FilePath (Get-WslExe) -Arguments @('--update') -TimeoutSec 900 -OnPoll $beat -PollIntervalMs 5000 -OwnConsole
        if ($u.ExitCode -ne 0) {
            if (Test-UacDeclined $u) {
                Write-ProgressLine -Stage 'wsl' -Title 'Updating WSL' -State 'failed' -Message 'Setup needs your permission once to turn on WSL.' -Fix 'Run Setup again when you are ready.' -MessageId 'wsl.permission.warning'
                return (New-VerbResult 'failed' ([ordered]@{ wsl = 'old'; restart = 0; reason = 'uac-declined' }))
            }
            Write-ProgressLine -Stage 'wsl' -Title 'Updating WSL' -State 'failed' -Message ('wsl --update failed (exit {0}).' -f $u.ExitCode) -Fix 'Run Setup again. If it keeps failing, use Save diagnostics.' -MessageId 'wsl.update.failed' -Values @{ exit_code = $u.ExitCode }
            return (New-VerbResult 'failed' ([ordered]@{ wsl = 'old'; restart = 0; reason = 'update-failed' }))
        }
    } else {
        # Step 2: started plainly from the user's session: it elevates itself with ONE UAC prompt
        # (proven; starting it pre-elevated produced two). It MUST have a console window of its
        # own: with its output captured, the inbox stub only prints "not installed" (P1).
        Write-ProgressLine -Stage 'wsl' -Title 'Turning on WSL' -State 'start'
        $i = Invoke-External -FilePath (Get-WslExe) -Arguments @('--install', '--no-distribution') -TimeoutSec 1800 -OnPoll $beat -PollIntervalMs 5000 -OwnConsole
        if ($i.ExitCode -ne 0) {
            if (Test-UacDeclined $i) {
                Write-ProgressLine -Stage 'wsl' -Title 'Turning on WSL' -State 'failed' -Message 'Setup needs your permission once to turn on WSL.' -Fix 'Run Setup again when you are ready.' -MessageId 'wsl.permission.warning'
                return (New-VerbResult 'failed' ([ordered]@{ wsl = 'missing'; restart = 0; reason = 'uac-declined' }))
            }
            # With its own console window the output is not captured, so a UAC "No" usually looks
            # exactly like this: exit 1 after a few seconds, nothing printed (P3 on the VM, 2026-09-29).
            # Test-UacDeclined only catches the exit codes that say so; the text covers both cases.
            Write-Log ("wsl-install: wsl --install exit {0} after {1} ms; most often the permission prompt was declined" -f $i.ExitCode, $i.Ms)
            Write-ProgressLine -Stage 'wsl' -Title 'Turning on WSL' -State 'failed' -Message 'WSL was not turned on.' -Fix ('If Windows asked for permission and you chose No, press Turn on WSL again and choose Yes. If you chose Yes, report the problem with the diagnostics file (wsl --install exit {0}).' -f $i.ExitCode)
            return (New-VerbResult 'failed' ([ordered]@{ wsl = 'missing'; restart = 0; reason = 'install-failed' }))
        }
    }
    # Step 3: restart detection. Exit 0 and a working `wsl --version` do not mean no restart.
    $pending = Test-RestartPending
    $after = Get-WslState
    Write-Log ("wsl-install: after command restart-pending={0} wsl={1}" -f $pending, $after.State)
    if ($pending -or $after.State -ne 'present') {
        Write-ProgressLine -Stage 'wsl' -Title 'Turning on WSL' -State 'done' -Message 'Windows must restart to finish turning on WSL.'
        return (New-VerbResult 'restart-required' ([ordered]@{ wsl = $after.State; restart = 1 }))
    }
    Write-ProgressLine -Stage 'wsl' -Title 'Turning on WSL' -State 'done'
    return (New-VerbResult 'ok' ([ordered]@{ wsl = 'present'; restart = 0 }))
}

function Invoke-RestartForWslVerb {
    param($Opts)
    $setup = [string](Get-Opt $Opts 'setup-exe' '')
    if ($setup) {
        # RunOnce points at the downloaded Setup.exe where the user ran it; no copy of 500 MB is kept.
        $localeName = switch ($script:Locale) {
            'es-ES' { 'spanish' }
            'fr-FR' { 'french' }
            'de-DE' { 'german' }
            'it-IT' { 'italian' }
            'pt-BR' { 'brazilianportuguese' }
            default { 'english' }
        }
        $value = '"{0}" /LANG={1} /resume' -f $setup, $localeName
        Set-RegistryValue -Path 'HKCU:\Software\Microsoft\Windows\CurrentVersion\RunOnce' -Name 'CognitaSetup' -Value $value -Type 'String'
        Write-Log ("restart-for-wsl: RunOnce CognitaSetup = {0}" -f $value)
    } else {
        Write-Log 'restart-for-wsl: no --setup-exe given; RunOnce value not written'
    }
    $s = Read-Settings
    if (-not $s) { $s = New-Settings -VhdDir (Get-DefaultVhdDir) }
    Set-SettingProp $s 'resume' 'after-wsl'
    Save-Settings $s
    if (Get-Opt $Opts 'now' $false) {
        # Design 19.1 item 1: /t 0, never a timeout above 0. shutdown.exe treats any /t above 0 as
        # implying /f, which force-closes programs without letting them save the user's work. /t 0 does
        # not force anything: programs get the normal close request. Setup asks "Save your work in other
        # programs first" before it calls this.
        $shutdownArgs = @('/r', '/t', '0', '/c', $script:RestartComment)
        $afterPid = [string](Get-Opt $Opts 'after-pid' '')
        if ($afterPid) {
            # Design 19.11 R1: without /f a RUNNING Setup answers Windows' "may we shut down?" with No, so
            # `shutdown /r /t 0` from inside Setup stalls on "Cognita Setup is preventing restart". Setup
            # passes its own PID; a detached hidden powershell waits for that process to EXIT (a signal,
            # no timer) and only then restarts. Setup closes right after this verb returns.
            $waitPid = 0
            if (-not ([int]::TryParse($afterPid, [ref]$waitPid) -and $waitPid -gt 0)) {
                Write-Log ("restart-for-wsl: --after-pid [{0}] is not a process id; nothing started" -f $afterPid)
                return (New-VerbResult 'failed' ([ordered]@{ reason = 'bad-after-pid' }))
            }
            # Setup is still running now, so its start time can be read here. The waiter usually starts
            # after Setup has exited; if Windows has already given that PID to another process, waiting
            # on it could delay the restart for hours. So the waiter waits only on a process with this
            # PID AND this start time (review of 19.11). 0 (unreadable) means "restart at once".
            $startFt = [int64]0
            try { $sp = Get-Process -Id $waitPid -ErrorAction Stop; $startFt = $sp.StartTime.ToFileTimeUtc() } catch { Write-Log ("restart-for-wsl: start time of pid {0} not readable: {1}" -f $waitPid, $_.Exception.Message) }
            # Single quotes inside: the whole script is ONE quoted argument, and a native command gets a
            # single-quoted string with spaces as one argument (a quote in the log path is doubled). The
            # waiter appends one line to this helper's log afterwards (pid found, shutdown exit code),
            # so a restart that never happened leaves a trace.
            $logQ = ([string]$script:LogFile).Replace("'", "''")
            $logPart = ''
            if ($script:LogFile) {
                $logPart = ("; Add-Content -LiteralPath '{0}' -Value ((Get-Date -Format 'yyyy-MM-dd HH:mm:ss.fff') + ' [restart-waiter] pid={1} found=' + `$found + ' shutdown_exit=' + `$LASTEXITCODE)" -f $logQ, $waitPid)
            }
            $waiter = ("`$p = Get-Process -Id {0} -ErrorAction SilentlyContinue; `$found = [bool](`$p -and {1} -ne 0 -and `$p.StartTime.ToFileTimeUtc() -eq {1}); if (`$found) {{ `$p.WaitForExit() }}; shutdown /r /t 0 /c '{2}'{3}" -f $waitPid, $startFt, $script:RestartComment, $logPart)
            $psExe = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
            $psArgs = @('-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-Command', $waiter)
            Write-Log ("restart-for-wsl: starting a detached hidden powershell that waits for pid {0} (start {1}) to exit, then runs shutdown /r /t 0 (no /f)" -f $waitPid, $startFt)
            $started = Start-DetachedHidden -FilePath $psExe -Arguments $psArgs
            Write-Log ("restart-for-wsl: detached waiter started={0} waiting_for_pid={1}" -f $started, $waitPid)
            if (-not $started) { return (New-VerbResult 'failed' ([ordered]@{ reason = 'restart-waiter-failed' })) }
            return (New-VerbResult 'ok' ([ordered]@{ resume = 'after-wsl'; restart_after_pid = $waitPid }))
        }
        Write-Log 'restart-for-wsl: --now without --after-pid; running shutdown.exe directly (a caller that is still running may stall the restart)'
        Write-Log ("restart-for-wsl: running shutdown.exe {0}" -f ($shutdownArgs -join ' '))
        $r = Invoke-External -FilePath (Join-Path $env:SystemRoot 'System32\shutdown.exe') -Arguments $shutdownArgs -TimeoutSec 30
        Write-Log ("restart-for-wsl: shutdown.exe exit={0}" -f $r.ExitCode)
        if ($r.ExitCode -ne 0) { return (New-VerbResult 'failed' ([ordered]@{ reason = 'shutdown-failed' })) }
    }
    return (New-VerbResult 'ok' ([ordered]@{ resume = 'after-wsl' }))
}

# ---------------------------------------------------------------------------------------
# Roots: fstab, mount points, mounts (design 5.5)
# ---------------------------------------------------------------------------------------
function Get-RootDownMessage {
    param([string]$WindowsPath)
    return ('Your projects folder {0} is not available to Cognita. Reconnect the drive or restore the folder, then run: cognita restart.' -f $WindowsPath)
}

function Test-RootMounted {
    # Design 18.1 rule 3: the question is "does DOCKER see the root", and Docker (systemd) lives in
    # the distro's base mount namespace, not in the namespace of the wsl.exe session this call runs
    # in. `nsenter -t 1 -m` enters PID 1's namespace, so the answer is Docker's. As root: entering
    # another mount namespace needs it. (Superseded: a plain `mountpoint -q` as the Linux user, which
    # reads this session's own copy of the namespace and so misjudged both ways in P1.)
    param($Settings, [int]$N)
    $r = Invoke-Wsl -Settings $Settings -User 'root' -Command @('nsenter', '-t', '1', '-m', '--', 'mountpoint', '-q', (Get-RootMountPoint $N)) -TimeoutSec 60
    $mounted = ((-not $r.StartError) -and (-not $r.TimedOut) -and $r.ExitCode -eq 0)
    Write-Log ("root {0} mounted={1} (Docker's view: nsenter -t 1 -m mountpoint -q exit {2})" -f $N, $mounted, $r.ExitCode)
    return $mounted
}

function Get-RootStates {
    # One entry per root: N, Windows, MountPoint, Mounted. Only asks; changes nothing.
    param($Settings)
    $out = @()
    foreach ($r in (Get-SettingsRoots $Settings)) {
        $out += [pscustomobject]@{ N = [int]$r.n; Windows = [string]$r.windows; MountPoint = [string]$r.linux; Mounted = (Test-RootMounted -Settings $Settings -N ([int]$r.n)) }
    }
    return $out
}

function Write-FstabViaRoot {
    # Reads /etc/fstab, applies Update-FstabText, writes it back through a temp file and mv on
    # the same filesystem, as root. Returns 'added'|'replaced'|'unchanged'. -Base: $Line is the roots
    # folder's own line and goes through Update-FstabBaseText, which also places it (design 22.14).
    param($Settings, [string]$MountPoint, [string]$Line, [switch]$Base)
    $r = Invoke-Wsl -Settings $Settings -User 'root' -Command @('cat', '/etc/fstab') -TimeoutSec 90
    $existing = ''
    if ($r.ExitCode -eq 0) { $existing = $r.Stdout }
    elseif (($r.Stderr + $r.Stdout) -match 'No such file') { $existing = '' }
    else { throw ('Could not read /etc/fstab inside the distro (exit {0}).' -f $r.ExitCode) }
    if ($Base) { $u = Update-FstabBaseText -ExistingText $existing -NewLine $Line }
    else { $u = Update-FstabText -ExistingText $existing -MountPoint $MountPoint -NewLine $Line }
    Write-Log ("fstab: mount point {0} action={1}" -f $MountPoint, $u.Action)
    if ($u.Action -eq 'unchanged') { return 'unchanged' }
    $delim = 'COGNITA_EOF_' + [guid]::NewGuid().ToString('N')
    if ($u.Text.Contains($delim)) { throw 'fstab text contains the heredoc delimiter (impossible).' }
    # The new text travels inside the script as a quoted heredoc (no expansion, bytes intact). A
    # heredoc body always ends with LF, so when the text itself does not, that LF is truncated
    # away again and the file's bytes stay exactly what Update-FstabText produced.
    $trim = ''
    $body = $u.Text
    if (-not $body.EndsWith("`n")) { $body = $body + "`n"; $trim = "truncate -s -1 /etc/fstab.cognita-new`n" }
    $script = "set -e`n" + "cat > /etc/fstab.cognita-new <<'" + $delim + "'`n" + $body + $delim + "`n"
    $script += $trim + "chmod 644 /etc/fstab.cognita-new`nchown root:root /etc/fstab.cognita-new`nmv -f /etc/fstab.cognita-new /etc/fstab`nexit 0`n"
    $w = Invoke-WslScript -Settings $Settings -User 'root' -ScriptText $script -TimeoutSec 90
    if ($w.ExitCode -ne 0) { throw ('Could not write /etc/fstab inside the distro (exit {0}): {1}' -f $w.ExitCode, (Limit-LogText $w.Stderr 400)) }
    return $u.Action
}

function New-RootMountPoint {
    # Creates the mount point directory as root 0755 (never chowned), before the distro restart that
    # applies the fstab line (design 18.1 rule 4). THE HELPER NEVER MOUNTS A ROOT ITSELF (rule 2):
    # this function used to be Mount-RootPoint, which "creates the mount point as root 0755 (never
    # chowned) and mounts it, only when it is not mounted already (mounting twice would stack, design 5.5
    # rule 5)" and ran `wsl -u root --exec mount <mp>` (superseded; the mkdir half stays). P1 proved that a mount made in a
    # running distro is visible only to the wsl.exe session that made it, not to Docker and not to
    # later sessions, so it "succeeded" and the install then died at the Docker bind. A mount made from
    # /etc/fstab when the distro STARTS is seen by all of them; Restart-CognitaDistro makes that happen.
    param($Settings, [int]$N)
    $mp = Get-RootMountPoint $N
    $m = Invoke-Wsl -Settings $Settings -User 'root' -Command @('mkdir', '-p', '-m', '0755', $mp) -TimeoutSec 60
    Write-Log ("root {0}: mkdir -p -m 0755 {1} exit {2}" -f $N, $mp, $m.ExitCode)
    if ($m.ExitCode -ne 0) { throw ('Could not create {0} inside the distro (exit {1}).' -f $mp, $m.ExitCode) }
}

function Write-RootFstabLine {
    # One root's line into /etc/fstab (only when it differs) and its mount point directory. Returns
    # 'added'|'replaced'|'unchanged'. A line without `shared` (an install from before design 18.1) counts
    # as different, so it is replaced here and the caller restarts the distro (rule 5).
    param($Settings, [int]$N, [string]$WindowsPath)
    $mp = Get-RootMountPoint $N
    $line = Get-FstabLineForRoot -WindowsPath $WindowsPath -N $N
    $action = Write-FstabViaRoot -Settings $Settings -MountPoint $mp -Line $line
    New-RootMountPoint -Settings $Settings -N $N
    Write-Log ("root {0}: fstab line action={1} windows=[{2}]" -f $N, $action, $WindowsPath)
    return $action
}

function Write-RootsBaseFstabLine {
    # Design 22.14: the shared bind of the roots folder (Get-FstabBaseLine), written after the root lines
    # (whose New-RootMountPoint has created the folder) and placed before them in the file. Returns
    # 'added'|'replaced'|'unchanged'; a change counts toward the caller's one distro restart, so an install
    # from before 22.14 gets it from its next Setup run.
    param($Settings)
    $action = Write-FstabViaRoot -Settings $Settings -MountPoint $script:RootsMountBase -Line (Get-FstabBaseLine) -Base
    Write-Log ("roots base {0}: fstab line action={1}" -f $script:RootsMountBase, $action)
    return $action
}

function Test-RootAccess {
    # Readable and writable by the Linux user: create and delete a .cognita-write-test-<hex> file.
    param($Settings, [int]$N)
    $mp = Get-RootMountPoint $N
    $hex = [guid]::NewGuid().ToString('N').Substring(0, 12)
    $script = "set -e`n" + 'dir="$1"' + "`n" + 'ls -A "$dir" >/dev/null </dev/null' + "`n" + 't="$dir/.cognita-write-test-$2"' + "`n" + ': > "$t" </dev/null' + "`n" + 'rm -f "$t"' + "`nexit 0`n"
    $r = Invoke-WslScript -Settings $Settings -User (Get-LinuxUser $Settings) -ScriptText $script -ScriptArgs @($mp, $hex) -TimeoutSec 60
    Write-Log ("root {0} access test: exit {1}" -f $N, $r.ExitCode)
    return @{ Ok = ($r.ExitCode -eq 0); Detail = (Limit-LogText (($r.Stderr + ' ' + $r.Stdout).Trim()) 300) }
}

function Restore-RootMounts {
    <#
    start and restart (design 5.5, revised by 18.1): when any root is unmounted in Docker's view, the
    distro is restarted through Restart-CognitaDistro so /etc/fstab is applied at its start. There is
    no `mount` here any more (it used to be Repair-RootMounts: "start and restart remount first (design
    5.5): every root that mountpoint -q reports unmounted", which ran `mount <mp>` as root; superseded,
    because that mount lived in one session only, see New-RootMountPoint). Returns
    @{ Restarted; Ready; Still } where Still is the roots that are unmounted afterwards, as objects
    with N, Windows and Message. Restarted=$true means Cognita has just been started fresh by the
    distro's own boot, so the caller must NOT also run `cognita restart`.
    #>
    param($Settings, [string]$Stage = 'start')
    $down = @()
    foreach ($r in (Get-SettingsRoots $Settings)) {
        $n = [int]$r.n
        if (Test-RootMounted -Settings $Settings -N $n) { Write-Log ("remount: root {0} already mounted, nothing to do" -f $n); continue }
        $down += [pscustomobject]@{ N = $n; Windows = [string]$r.windows; Message = (Get-RootDownMessage ([string]$r.windows)) }
    }
    if ($down.Count -eq 0) {
        Write-Log 'remount: every root is mounted in Docker''s view; the distro is not restarted'
        return @{ Restarted = $false; Ready = $true; Still = @() }
    }
    Write-Log ("remount: {0} root(s) unmounted ({1}); restarting the distro so fstab is applied at its start" -f $down.Count, (($down | ForEach-Object { $_.N }) -join ','))
    $rs = Restart-CognitaDistro -Settings $Settings -Reason 'roots unmounted' -Stage $Stage
    if (-not $rs.Ready) { return @{ Restarted = $true; Ready = $false; Still = @($down) } }
    $still = @()
    foreach ($d in $down) {
        if (Test-RootMounted -Settings $Settings -N $d.N) { Write-Log ("remount: root {0} is mounted after the restart" -f $d.N) }
        else { Write-Log ("remount: root {0} ({1}) is STILL unmounted after the restart" -f $d.N, $d.Windows); $still += $d }
    }
    return @{ Restarted = $true; Ready = $true; Still = @($still) }
}

function Assert-RootsAvailable {
    # install, update and add-folder check every root first and stop with the message, so the
    # Linux CLI is never run while a root is down (design 5.5, request item 14).
    param($Settings)
    $down = @(Get-RootStates -Settings $Settings | Where-Object { -not $_.Mounted })
    if ($down.Count -eq 0) { return $null }
    $msgs = @($down | ForEach-Object { Get-RootDownMessage $_.Windows })
    Write-Log ("roots check: {0} root(s) down" -f $down.Count)
    return ($msgs -join ' ')
}

function Add-CognitaRoot {
    <#
    Design 5.5 in full. -TellLinux runs "cognita add-folder" (later roots); the first root is
    passed to "cognita install" by the install flow instead. Returns
    @{ Ok; N; Path; Reason }.
    #>
    # Revised by design 18.1 (rule 2 and 4): fstab line, mount point directory, ONE Restart-CognitaDistro
    # (fstab is applied when the distro starts; the helper never mounts), the mount check in Docker's view,
    # then the access test, then (add-folder) `cognita add-folder`. -SyncOthers (install and repair) also
    # rewrites the other roots' lines, so an install from before `shared` is repaired by the same restart.
    param($Settings, [string]$WindowsPath, [switch]$TellLinux, [switch]$SyncOthers, [string]$Stage = 'folder')
    $v = Test-RootPath -Path $WindowsPath -Settings $Settings
    if (-not $v.Ok) { return @{ Ok = $false; N = 0; Path = $WindowsPath; Reason = $v.Reason } }
    $resolved = $v.Path
    $n = [int]$v.Existing
    if ($n -eq 0) {
        $used = @(Get-SettingsRoots $Settings | ForEach-Object { [int]$_.n })
        for ($k = 1; $k -le 9; $k++) { if ($used -notcontains $k) { $n = $k; break } }
        if ($n -eq 0) { return @{ Ok = $false; N = 0; Path = $resolved; Reason = 'Cognita supports up to 9 projects folders.' } }
    }
    Write-Log ("add root: n={0} windows=[{1}] existing={2} syncOthers={3}" -f $n, $resolved, $v.Existing, [bool]$SyncOthers)
    Write-ProgressLine -Stage $Stage -Title 'Connecting your projects folder' -State 'start'
    $mp = Get-RootMountPoint $n
    $changed = 0
    $act = Write-RootFstabLine -Settings $Settings -N $n -WindowsPath $resolved
    if ($act -ne 'unchanged') { $changed++ }
    $checkRoots = @($n)
    if ($SyncOthers) {
        foreach ($r in (Get-SettingsRoots $Settings)) {
            $k = [int]$r.n
            if ($k -eq $n) { continue }
            $a = Write-RootFstabLine -Settings $Settings -N $k -WindowsPath ([string]$r.windows)
            if ($a -ne 'unchanged') { $changed++ }
            $checkRoots += $k
        }
    }
    if ((Write-RootsBaseFstabLine -Settings $Settings) -ne 'unchanged') { $changed++ }
    $needRestart = ($changed -gt 0)
    $why = ('{0} fstab line(s) added or replaced' -f $changed)
    if (-not $needRestart) {
        foreach ($k in $checkRoots) {
            if (-not (Test-RootMounted -Settings $Settings -N $k)) { $needRestart = $true; $why = ('root {0} is not mounted in Docker''s view' -f $k); break }
        }
    }
    Write-Log ("add root: restart decision needRestart={0} ({1}) roots checked=[{2}]" -f $needRestart, $why, ($checkRoots -join ','))
    if ($needRestart) {
        Write-ProgressLine -Stage $Stage -Title 'Connecting your projects folder' -State 'progress' -Message 'Restarting Cognita''s Linux so it can see the folder. This takes about a minute.'
        $rs = Restart-CognitaDistro -Settings $Settings -Reason ('projects folder {0}: {1}' -f $n, $why) -Stage $Stage
        if (-not $rs.Ready) {
            $why2 = 'Cognita''s Linux did not finish starting within 5 minutes after the folder {0} was connected.' -f $resolved
            Write-ProgressLine -Stage $Stage -Title 'Connecting your projects folder' -State 'failed' -Message $why2 -Fix 'Run Setup again. If it keeps failing, use Save diagnostics.'
            return @{ Ok = $false; N = $n; Path = $resolved; Reason = $why2 }
        }
    }
    if (-not (Test-RootMounted -Settings $Settings -N $n)) {
        $why = 'Cognita could not open the folder {0} from inside WSL.' -f $resolved
        Write-ProgressLine -Stage $Stage -Title 'Connecting your projects folder' -State 'failed' -Message ($why + ' The folder is not mounted where Cognita''s Docker can see it.') -Fix 'Check that the folder still exists and the drive is connected, then run Setup again.'
        return @{ Ok = $false; N = $n; Path = $resolved; Reason = $why }
    }
    $acc = Test-RootAccess -Settings $Settings -N $n
    if (-not $acc.Ok) {
        $why = 'Cognita cannot read and write in {0} from inside WSL.' -f $resolved
        Write-ProgressLine -Stage $Stage -Title 'Connecting your projects folder' -State 'failed' -Message ($why + ' ' + $acc.Detail) -Fix 'Check the folder''s permissions (your Windows account must be able to create files there), then run Setup again.'
        return @{ Ok = $false; N = $n; Path = $resolved; Reason = $why }
    }
    if ($TellLinux) {
        # Exactly the design's 5.5 step 6 (plus the progress file the runner adds); no --non-interactive,
        # which the Linux CLI's add-folder is not documented to take.
        $args2 = @('add-folder', $mp, '--display', $resolved)
        $lr = Invoke-CognitaLinux -Settings $Settings -CliArgs $args2 -Stage 'add_folder' -TimeoutSec 600 -Title 'Add a projects folder'
        if ($lr.ExitCode -ne 0) {
            $why = 'Cognita could not add the folder: ' + $lr.Summary
            return @{ Ok = $false; N = $n; Path = $resolved; Reason = $why }
        }
    }
    $rootsNew = @()
    foreach ($r in (Get-SettingsRoots $Settings)) { if ([int]$r.n -ne $n) { $rootsNew += $r } }
    $rootsNew += [pscustomobject][ordered]@{ n = $n; windows = $resolved; linux = $mp }
    Set-SettingProp $Settings 'roots' @($rootsNew | Sort-Object { [int]$_.n })
    Save-Settings $Settings
    Write-ProgressLine -Stage $Stage -Title 'Connecting your projects folder' -State 'done'
    return @{ Ok = $true; N = $n; Path = $resolved; Reason = '' }
}

# ---------------------------------------------------------------------------------------
# Startup at login and staying up (design 5.8)
# ---------------------------------------------------------------------------------------
function New-CognitaTaskParts {
    # The pieces of the login task (design 5.8). Building them changes nothing on the machine, so a test
    # can check every setting without registering a task.
    param([string]$VbsPath)
    $user = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
    return @{
        Action    = (New-ScheduledTaskAction -Execute (Join-Path $env:SystemRoot 'System32\wscript.exe') -Argument ('//B "{0}"' -f $VbsPath))
        Trigger   = (New-ScheduledTaskTrigger -AtLogOn -User $user)
        Settings  = (New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries)
        Principal = (New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited)
    }
}
function Register-CognitaTask {
    # Registered without admin (proven). Trigger at logon; wscript //B launch-keepalive.vbs.
    param([string]$VbsPath)
    $p = New-CognitaTaskParts -VbsPath $VbsPath
    # -ErrorAction Stop (design 18.5): without it a failure is a non-terminating error, the catch in
    # Register-Keepalive never fires, and "registered" is reported for a task that does not exist.
    [void](Register-ScheduledTask -TaskName $script:TaskName -Action $p.Action -Trigger $p.Trigger -Settings $p.Settings -Principal $p.Principal -Force -ErrorAction Stop)
}
function Get-CognitaTaskInfo {
    # @{ Exists; State; LastResult; LastRun } or Exists=$false.
    try {
        $t = Get-ScheduledTask -TaskName $script:TaskName -ErrorAction Stop
        $i = Get-ScheduledTaskInfo -TaskName $script:TaskName -ErrorAction Stop
        return [pscustomobject]@{ Exists = $true; State = [string]$t.State; LastResult = $i.LastTaskResult; LastRun = [string]$i.LastRunTime }
    } catch {
        Write-Log ("scheduled task '{0}' not readable (treated as missing): {1}" -f $script:TaskName, $_.Exception.Message)
        return [pscustomobject]@{ Exists = $false; State = ''; LastResult = 0; LastRun = '' }
    }
}
function Start-CognitaTask { Start-ScheduledTask -TaskName $script:TaskName -ErrorAction Stop }   # Stop: so Start-Keepalive's catch and warning fire (design 18.5)
function Unregister-CognitaTask { Unregister-ScheduledTask -TaskName $script:TaskName -Confirm:$false -ErrorAction Stop }

function Register-Keepalive {
    $vbs = Join-Path (Get-AppDir) 'launch-keepalive.vbs'
    Write-Log ("keepalive: registering scheduled task '{0}' for {1}" -f $script:TaskName, $vbs)
    try { Register-CognitaTask -VbsPath $vbs; return $true }
    catch { Write-Log ("keepalive: task registration FAILED: {0}" -f $_.Exception.Message); return $false }
}

function Start-Keepalive {
    # Removes the stopped flag (cognita start does the same), makes sure the task exists, runs it.
    $flag = Get-StoppedFlagPath
    if (Test-Path -LiteralPath $flag) {
        Remove-Item -LiteralPath $flag -Force
        Write-Log 'keepalive: removed the stopped flag'
    }
    $info = Get-CognitaTaskInfo
    if (-not $info.Exists) { Write-Log 'keepalive: task missing, registering'; [void](Register-Keepalive) }
    try { Start-CognitaTask; Write-Log 'keepalive: task started'; return $true }
    catch { Write-Log ("keepalive: task start FAILED: {0}" -f $_.Exception.Message); return $false }
}

function Test-DistroReady {
    # systemd running or degraded, and docker answers as the Linux user.
    param($Settings)
    $s = Invoke-Wsl -Settings $Settings -User 'root' -Command @('systemctl', 'is-system-running') -TimeoutSec 60
    $sys = ($s.Stdout.Trim() -split "`r?`n" | Select-Object -First 1)
    if ($sys -ne 'running' -and $sys -ne 'degraded') { Write-Log ("distro ready? systemd={0}" -f $sys); return $false }
    $d = Invoke-Wsl -Settings $Settings -User (Get-LinuxUser $Settings) -Command @('docker', 'info', '--format', '{{.ServerVersion}}') -TimeoutSec 60
    Write-Log ("distro ready? systemd={0} docker exit={1}" -f $sys, $d.ExitCode)
    return ($d.ExitCode -eq 0)
}

function Wait-DistroReady {
    param($Settings, [int]$TimeoutSec = 300, [string]$Stage = 'keepalive')
    $t0 = Get-ClockNow
    $cond = { Test-DistroReady -Settings $Settings }.GetNewClosure()
    $ok = Wait-Until -Condition $cond -TimeoutSec $TimeoutSec -IntervalMs 3000 -Description 'distro ready (systemd + docker)' -Stage $Stage -Title 'Starting Cognita''s Linux'
    Write-Log ("distro ready wait: ok={0} took {1:N1}s" -f $ok, ((Get-ClockNow) - $t0).TotalSeconds)
    return $ok
}

function Restart-CognitaDistro {
    <#
    The ONE way a root's mount is made or repaired (design 18.1 rule 2). /etc/fstab is applied when the
    distro STARTS, and only a mount made then is seen by Docker and by every later session, so:
      1. remove the stopped flag (the keepalive loop must relaunch the distro, not exit),
      2. wsl --terminate <distro>,
      3. run the login task (the keepalive relaunches the distro; fstab is applied at its start),
      4. Wait-DistroReady, bounded 300 s.
    Each step and the time taken are logged. Returns @{ Ready; Seconds }. Cognita itself comes up with the
    distro (Docker, linger and the Linux CLI's unit start at boot), so a caller that has just restarted
    the distro must not also run `cognita restart`.
    #>
    param($Settings, [string]$Reason = '', [string]$Stage = 'keepalive')
    $t0 = Get-ClockNow
    $distro = Get-DistroName $Settings
    Write-Log ("restart distro: begin distro={0} reason=[{1}]" -f $distro, $Reason)
    $flag = Get-StoppedFlagPath
    if (Test-Path -LiteralPath $flag) {
        Remove-Item -LiteralPath $flag -Force
        Write-Log 'restart distro: step 1 removed the stopped flag'
    } else { Write-Log 'restart distro: step 1 no stopped flag to remove' }
    $t = Invoke-External -FilePath (Get-WslExe) -Arguments @('--terminate', $distro) -TimeoutSec 60
    Write-Log ("restart distro: step 2 wsl --terminate {0} exit {1}" -f $distro, $t.ExitCode)
    $started = Start-Keepalive
    Write-Log ("restart distro: step 3 login task run ok={0}" -f $started)
    $ready = Wait-DistroReady -Settings $Settings -TimeoutSec 300 -Stage $Stage
    $secs = ((Get-ClockNow) - $t0).TotalSeconds
    Write-Log ("restart distro: end ready={0} took {1:N1}s" -f $ready, $secs)
    return @{ Ready = $ready; Seconds = $secs }
}

# ---------------------------------------------------------------------------------------
# Image import (design 5.7)
# ---------------------------------------------------------------------------------------
function Get-Sha256OfFile {
    # Streams the file and writes a progress line every 5 seconds of clock time.
    param([string]$Path, [string]$Stage = 'import')
    $sha = [System.Security.Cryptography.SHA256]::Create()
    $fs = [System.IO.File]::Open($Path, 'Open', 'Read', 'Read')
    try {
        $buf = New-Object 'byte[]' (4MB)
        $total = $fs.Length
        $done = [int64]0
        $last = Get-ClockNow
        while (($n = $fs.Read($buf, 0, $buf.Length)) -gt 0) {
            [void]$sha.TransformBlock($buf, 0, $n, $null, 0)
            $done += $n
            $now = Get-ClockNow
            if (($now - $last).TotalSeconds -ge 5) {
                Write-ProgressLine -Stage $Stage -Title 'Checking the Linux image' -State 'progress' -BytesDone $done -BytesTotal $total
                $last = $now
            }
        }
        [void]$sha.TransformFinalBlock($buf, 0, 0)
        return (($sha.Hash | ForEach-Object { $_.ToString('x2') }) -join '')
    } finally { $fs.Dispose(); $sha.Dispose() }
}

function Get-ImportFailureKind {
    param([string]$Text)
    if ($Text -match 'HCS_E_SERVICE_NOT_AVAILABLE') { return 'restart' }
    if ($Text -match 'HCS_E_HYPERV_NOT_INSTALLED|0x80370102|0x80370104|0x80370114|HCS_E_[A-Z_]+|virtualization') { return 'virtualization' }
    return 'other'
}

function Invoke-ImportImage {
    <#
    Design 5.7 steps 1-6. Returns @{ Status = 'ok'|'restart-required'|'failed'; Reason; Settings }.
    $Settings is the record to use (created here for a fresh install).
    #>
    param($Settings, [string]$ImagePath, [string]$ImageSha256, [string]$VhdDir)
    $savedDefault = Get-LxssDefaultDistribution
    Write-Log ("import: saved default distribution guid=[{0}]" -f $savedDefault)
    Write-ProgressLine -Stage 'import' -Title 'Setting up Cognita''s Linux' -State 'start'
    if (-not $ImagePath -or -not (Test-Path -LiteralPath $ImagePath)) {
        Write-ProgressLine -Stage 'import' -Title 'Setting up Cognita''s Linux' -State 'failed' -Message 'The Linux image file is missing.' -Fix 'Run Setup again.'
        return @{ Status = 'failed'; Reason = 'image-missing'; Settings = $Settings }
    }
    if ($ImageSha256) {
        $actual = Get-Sha256OfFile -Path $ImagePath
        Write-Log ("import: image sha256 actual={0} expected={1}" -f $actual, $ImageSha256)
        if ($actual -ine $ImageSha256) {
            Write-ProgressLine -Stage 'import' -Title 'Setting up Cognita''s Linux' -State 'failed' -Message 'The Linux image did not match its checksum, so it was not used.' -Fix 'Download Setup again and run it.'
            return @{ Status = 'failed'; Reason = 'image-hash-mismatch'; Settings = $Settings }
        }
    } else { Write-Log 'import: no --image-sha256 given; image hash not checked' }
    Set-SettingProp $Settings 'state' 'import-pending'
    Set-SettingProp $Settings 'vhd_dir' $VhdDir
    Save-Settings $Settings
    $parent = Split-Path -Parent $VhdDir
    if ($parent -and -not (Test-Path -LiteralPath $parent)) {
        # Design 19.7 item 20: every folder this call creates is recorded (settings `created_dirs`,
        # deepest first) so delete-data can remove them when they are empty. New-Item -Force creates ALL
        # the missing ancestors ("D:\Data\Cognita" when only "D:\" exists), so the missing ones are listed
        # BEFORE creating, walking up until an ancestor exists. A folder that already existed is never
        # listed: it is not ours to remove.
        $made = @()
        $walk = $parent
        while ($walk -and -not (Test-Path -LiteralPath $walk)) {
            $made += $walk
            $up = Split-Path -Parent $walk
            if ($up -eq $walk) { break }
            $walk = $up
        }
        [void](New-Item -ItemType Directory -Path $parent -Force)
        $recorded = @()
        if ($Settings.PSObject.Properties['created_dirs'] -and $null -ne $Settings.created_dirs) { $recorded = @($Settings.created_dirs) }
        foreach ($m in $made) { if ($recorded -notcontains $m) { $recorded += $m } }
        Set-SettingProp $Settings 'created_dirs' @($recorded)
        Save-Settings $Settings
        Write-Log ("import: created the disk folder's parent(s) [{0}]; recorded as created_dirs (deepest first): [{1}]" -f $parent, ($recorded -join '; '))
    } else { Write-Log ("import: the disk folder's parent [{0}] already exists; nothing recorded in created_dirs" -f $parent) }
    $name = Get-DistroName $Settings
    $beat = { Write-ProgressLine -Stage 'import' -Title 'Setting up Cognita''s Linux' -State 'progress' -Message 'Importing the Linux image. This takes a minute.' }
    $r = Invoke-External -FilePath (Get-WslExe) -Arguments @('--import', $name, $VhdDir, $ImagePath, '--version', '2') -TimeoutSec 1800 -OnPoll $beat -PollIntervalMs 5000
    $text = Remove-NulAndBom ($r.Stdout + "`n" + $r.Stderr)
    if ($r.ExitCode -ne 0) {
        $kind = Get-ImportFailureKind $text
        Write-Log ("import: FAILED exit={0} kind={1}" -f $r.ExitCode, $kind)
        if ($kind -eq 'restart') {
            Write-ProgressLine -Stage 'import' -Title 'Setting up Cognita''s Linux' -State 'warning' -Message 'Windows must restart before WSL can start virtual machines.' -Fix 'Restart Windows, then run Setup again.'
            return @{ Status = 'restart-required'; Reason = 'hcs-service-not-available'; Settings = $Settings }
        }
        if ($kind -eq 'virtualization') {
            Write-ProgressLine -Stage 'import' -Title 'Setting up Cognita''s Linux' -State 'failed' -Message 'WSL could not start a virtual machine.' -Fix 'Turn on virtualization (Intel VT-x or AMD-V/SVM) in your PC''s BIOS/UEFI settings. On a virtual machine, turn on nested virtualization.' -MessageId 'setup.import.virtualization'
            return @{ Status = 'failed'; Reason = 'virtualization'; Settings = $Settings }
        }
        Write-ProgressLine -Stage 'import' -Title 'Setting up Cognita''s Linux' -State 'failed' -Message ('wsl --import failed (exit {0}): {1}' -f $r.ExitCode, (Limit-LogText $text.Trim() 300)) -Fix 'Run Setup again. If it keeps failing, use Save diagnostics.'
        return @{ Status = 'failed'; Reason = 'import-failed'; Settings = $Settings }
    }
    # The extracted image belongs to Setup (it extracted it into its own temp folder and Inno removes
    # that folder), so the helper deletes nothing it did not create. (The design's 5.7 step 3 said the
    # helper deletes it; Setup's contract, relayed by the lead, says it must not.)
    Write-Log 'import: image file left for Setup to remove'
    # /etc/cognita-distro: the marker that makes ownership provable (step 5)
    $script = "set -e`numask 022`n" + 'printf ''{"installation_id": "%s"}\n'' "$1" > /etc/cognita-distro.new' + "`nmv -f /etc/cognita-distro.new /etc/cognita-distro`nexit 0`n"
    $w = Invoke-WslScript -Settings $Settings -User 'root' -ScriptText $script -ScriptArgs @([string]$Settings.installation_id) -TimeoutSec 180
    if ($w.ExitCode -ne 0) {
        Write-ProgressLine -Stage 'import' -Title 'Setting up Cognita''s Linux' -State 'failed' -Message ('Could not write the ownership marker in the new distro (exit {0}).' -f $w.ExitCode) -Fix 'Run Setup again.'
        return @{ Status = 'failed'; Reason = 'marker-failed'; Settings = $Settings }
    }
    Set-SettingProp $Settings 'state' 'installed'
    Set-SettingProp $Settings 'resume' $null
    Save-Settings $Settings
    # Step 6: restore the default distro if the import changed it (proven not to, but verify)
    $nowDefault = Get-LxssDefaultDistribution
    if ($savedDefault -and $nowDefault -ne $savedDefault) {
        Write-Log ("import: default distribution changed from {0} to {1}; restoring" -f $savedDefault, $nowDefault)
        Set-LxssDefaultDistribution -Guid $savedDefault
    } else {
        Write-Log ("import: default distribution unchanged (saved=[{0}] now=[{1}])" -f $savedDefault, $nowDefault)
    }
    Write-ProgressLine -Stage 'import' -Title 'Setting up Cognita''s Linux' -State 'done'
    return @{ Status = 'ok'; Reason = ''; Settings = $Settings }
}

function Invoke-UnregisterDistro {
    param($Settings)
    $name = Get-DistroName $Settings
    $r = Invoke-External -FilePath (Get-WslExe) -Arguments @('--unregister', $name) -TimeoutSec 300
    Write-Log ("unregister {0}: exit {1}" -f $name, $r.ExitCode)
    return ($r.ExitCode -eq 0)
}

# ---------------------------------------------------------------------------------------
# The Linux CLI, called inside the distro (design 5.1 "Linux progress into Windows", 7.1)
# ---------------------------------------------------------------------------------------
function New-ProgressFilePath {
    return (Join-Path ([System.IO.Path]::GetTempPath()) ('cognita-progress-{0}.jsonl' -f [guid]::NewGuid().ToString('N')))
}

function Read-NewFileLines {
    # Complete new lines (UTF-8) appended to $Path since State.Offset. A line the writer has not
    # finished yet stays unread until its LF arrives.
    param([string]$Path, $State)
    if (-not (Test-Path -LiteralPath $Path)) { return @() }
    $fs = $null
    try {
        $fs = [System.IO.File]::Open($Path, 'Open', 'Read', 'ReadWrite, Delete')
        if ($fs.Length -le $State.Offset) { return @() }
        [void]$fs.Seek($State.Offset, 'Begin')
        $len = [int]($fs.Length - $State.Offset)
        $buf = New-Object 'byte[]' $len
        $got = 0
        while ($got -lt $len) { $n = $fs.Read($buf, $got, $len - $got); if ($n -le 0) { break }; $got += $n }
        $lastLf = -1
        for ($i = $got - 1; $i -ge 0; $i--) { if ($buf[$i] -eq 10) { $lastLf = $i; break } }
        if ($lastLf -lt 0) { return @() }
        $text = (New-Object System.Text.UTF8Encoding($false)).GetString($buf, 0, $lastLf + 1)
        $State.Offset = $State.Offset + $lastLf + 1
        if ($text.Length -gt 0 -and $text[0] -eq [char]0xFEFF) { $text = $text.Substring(1) }
        return @($text -split "`r?`n" | Where-Object { $_.Trim() -ne '' })
    } catch {
        Write-Log ("progress file read failed: {0}" -f $_.Exception.Message)
        return @()
    } finally { if ($fs) { $fs.Dispose() } }
}

function Sync-LinuxProgress {
    # Relays every new Linux progress line unchanged; heartbeats when nothing arrived for 5 s.
    param($State, [string]$Stage)
    $lines = @(Read-NewFileLines -Path $State.Path -State $State)
    foreach ($l in $lines) {
        try {
            $o = $l | ConvertFrom-Json
            if ($o.state -eq 'failed') { $State.Failed = $o }
            # Design 19.10b: the Linux update's release switch happens inside its `start` stage (apply_release
            # + verify), and that stage is closed ("done") only when the proof stage begins. So a relayed
            # `start` + `done` line means the release was switched; `update` uses it to decide whether "To go
            # back: cognita rollback" is true advice. Anything before it left the old release in place.
            if ($o.stage -eq 'start' -and $o.state -eq 'done') { $State.SwitchDone = $true; Write-Log 'linux progress: the start stage is done (the release switch happened)' }
            # Design 19.6 item 13: compare the incoming title with the PREVIOUS one BEFORE assigning it.
            # The old code assigned first and then asked "is there a title", which is true for every line,
            # so a same-title warning or message line (no byte counts) wiped the counts and Setup's bar
            # fell back to "Still working". Only a line that carries a NEW title starts a new stage's counts.
            $newTitle = ($o.title -and ([string]$o.title -ne [string]$State.LastTitle))
            # Design 18.5: a heartbeat reuses the last title the Linux side relayed, so Setup's line does
            # not flip to "Cognita is still working" in the middle of "Pulling the images".
            if ($o.title) { $State.LastTitle = [string]$o.title }
            # Design 21.3 (found while building it): the same for the STAGE. A heartbeat used to carry the
            # caller's stage ('cognita'), so Setup's current stage flipped away from `proof` every 5 s and
            # the Skip self-tests button (which is shown only while the stage is `proof`) would have
            # flickered off during a three-minute self-test. A heartbeat now reuses the last stage the
            # Linux side relayed.
            if ($o.stage) { $State.LastStage = [string]$o.stage }
            # The same for the byte counts: a download reports every ~7 s, so a heartbeat without them
            # flipped Setup's line and bar back to "Still working" between reports (P1, 2026-09-29).
            # A line from a NEW title without counts clears them, so an old stage's bytes never linger.
            if ($null -ne $o.bytes_total -and [int64]$o.bytes_total -gt 0) {
                $State.LastBytesDone = [int64]$o.bytes_done
                $State.LastBytesTotal = [int64]$o.bytes_total
            } elseif ($newTitle) {
                $State.LastBytesDone = $null
                $State.LastBytesTotal = $null
                Write-Log ("linux progress: new title [{0}] without counts; remembered byte counts cleared" -f $State.LastTitle)
            } elseif ($o.title -and $null -ne $State.LastBytesTotal) {
                Write-Log ("linux progress: same title [{0}] without counts; kept {1}/{2} bytes" -f $State.LastTitle, $State.LastBytesDone, $State.LastBytesTotal)
            }
        } catch {
            Write-Log ("progress line is not JSON, not relayed: {0}" -f (Limit-LogText $l 200))
            continue
        }
        Write-RelayedLine $l
        $State.Relayed++
        $State.LastEmit = Get-ClockNow
    }
    # Design 21.3: Setup writes the request flag when the user presses Skip self-tests. The skip file sits
    # beside the progress file (same /mnt path the CLI sees), and the CLI stops its self-test when it
    # appears. Created once per run; ONE progress line tells Setup the request was passed on.
    $skipFile = [string]$State.Path + '.skip'
    if ($State.Path -and -not $State.SkipRequested -and (Test-Path -LiteralPath (Get-SkipRequestPath))) {
        if (-not (Test-Path -LiteralPath $skipFile)) {
            try {
                [System.IO.File]::WriteAllBytes($skipFile, [byte[]]@())
                $State.SkipRequested = $true
                Write-Log ("self-test skip requested by Setup; skip file {0} created" -f $skipFile)
                Write-ProgressLine -Stage 'proof' -Title 'Running self-tests to verify the installation' -State 'progress' -Message 'Stopping the self-tests...'
                $State.LastEmit = Get-ClockNow
            } catch {
                Write-Log ("self-test skip requested by Setup but the skip file {0} could not be created: {1}" -f $skipFile, $_.Exception.Message)
            }
        } else {
            $State.SkipRequested = $true
            Write-Log ("self-test skip requested by Setup; skip file {0} already exists" -f $skipFile)
        }
    }
    $now = Get-ClockNow
    if ($lines.Count -eq 0 -and (($now - $State.LastEmit).TotalSeconds -ge 5)) {
        $beatTitle = [string]$State.LastTitle
        if (-not $beatTitle) { $beatTitle = [string]$State.StartTitle }
        if (-not $beatTitle) { $beatTitle = 'Working' }
        $beatStage = [string]$State.LastStage
        if (-not $beatStage) { $beatStage = $Stage }
        Write-ProgressLine -Stage $beatStage -Title $beatTitle -State 'progress' -BytesDone $State.LastBytesDone -BytesTotal $State.LastBytesTotal -Message ('Still working, {0} s so far.' -f [int]($now - $State.Start).TotalSeconds)
        $State.LastEmit = $now
    }
}

function Invoke-CognitaLinux {
    <#
    Runs "cognita <CliArgs>" inside the distro as the Linux user, with --progress-file pointing at
    a Windows temp file the helper tails. Returns
    [pscustomobject]@{ ExitCode; Failed; Summary; Stdout; Stderr; TimedOut }.
    The password, when there is one, goes only through StdinText.
    #>
    param(
        $Settings,
        [string[]]$CliArgs,
        [string]$StdinText = $null,
        [string]$Stage = 'cognita',
        [int]$TimeoutSec = 7200,
        [switch]$NoProgressFile,
        # The title a heartbeat carries until the Linux side has relayed one of its own (design 18.5).
        [string]$Title = ''
    )
    $cliArgs2 = @($CliArgs)
    $state = @{ Path = $null; Offset = [int64]0; Failed = $null; Relayed = 0; LastEmit = (Get-ClockNow); Start = (Get-ClockNow); LastTitle = ''; StartTitle = $Title; LastBytesDone = $null; LastBytesTotal = $null; SwitchDone = $false; LastStage = ''; SkipRequested = $false }
    if (-not $NoProgressFile) {
        $state.Path = New-ProgressFilePath
        $cliArgs2 += @('--progress-file', (ConvertTo-WslMntPath $state.Path))
        # Design 21.3: a request flag left by an earlier run (Setup killed, a crash) must not skip THIS
        # run's self-test. Setup deletes its own stale one at start too; this is the helper's half.
        $req = Get-SkipRequestPath
        if (Test-Path -LiteralPath $req) {
            try { Remove-Item -LiteralPath $req -Force; Write-Log ("linux cli: removed a stale self-test skip request {0}" -f $req) }
            catch { Write-Log ("linux cli: could not remove a stale self-test skip request {0}: {1}" -f $req, $_.Exception.Message) }
        }
        $staleSkip = $state.Path + '.skip'
        if (Test-Path -LiteralPath $staleSkip) {
            try { Remove-Item -LiteralPath $staleSkip -Force; Write-Log ("linux cli: removed a stale skip file {0}" -f $staleSkip) }
            catch { Write-Log ("linux cli: could not remove a stale skip file {0}: {1}" -f $staleSkip, $_.Exception.Message) }
        }
    }
    $poll = $null
    if ($state.Path) { $poll = { Sync-LinuxProgress -State $state -Stage $Stage }.GetNewClosure() }
    Write-Log ("linux cli: {0}" -f ((@($cliArgs2) | ForEach-Object { $_ }) -join ' '))
    $r = Invoke-Wsl -Settings $Settings -User (Get-LinuxUser $Settings) -Command (@($script:LinuxCliPath) + $cliArgs2) -TimeoutSec $TimeoutSec -StdinText $StdinText -OnPoll $poll
    if ($state.Path) {
        Sync-LinuxProgress -State $state -Stage $Stage
        try { if (Test-Path -LiteralPath $state.Path) { Remove-Item -LiteralPath $state.Path -Force } } catch { Write-Log ("progress file cleanup failed: {0}" -f $_.Exception.Message) }
        # Design 21.3: the skip file and the request flag are this run's; both go away with it.
        $skipFile = $state.Path + '.skip'
        try { if (Test-Path -LiteralPath $skipFile) { Remove-Item -LiteralPath $skipFile -Force } } catch { Write-Log ("skip file cleanup failed: {0}" -f $_.Exception.Message) }
        $req = Get-SkipRequestPath
        try {
            if (Test-Path -LiteralPath $req) { Remove-Item -LiteralPath $req -Force; Write-Log ("linux cli: removed the self-test skip request {0} at the end of the run (skipRequested={1})" -f $req, $state.SkipRequested) }
        } catch { Write-Log ("skip request cleanup failed: {0}" -f $_.Exception.Message) }
    }
    $summary = ''
    if ($state.Failed) { $summary = (([string]$state.Failed.message) + ' ' + ([string]$state.Failed.fix)).Trim() }
    if (-not $summary -and $r.ExitCode -ne 0) {
        $tail = ($r.Stderr + "`n" + $r.Stdout).Trim()
        if ($tail.Length -gt 400) { $tail = $tail.Substring($tail.Length - 400) }
        $summary = $tail
        if ($r.StartError) { $summary = $r.StartError }
        if ($r.TimedOut) { $summary = 'The command did not finish in time.' }
    }
    Write-Log ("linux cli done: exit={0} failedLine={1} relayed={2} switchDone={3} skipRequested={4} summary=[{5}]" -f $r.ExitCode, [bool]$state.Failed, $state.Relayed, $state.SwitchDone, $state.SkipRequested, (Limit-LogText $summary 300))
    return [pscustomobject]@{ ExitCode = $r.ExitCode; Failed = $state.Failed; Summary = $summary; Stdout = $r.Stdout; Stderr = $r.Stderr; TimedOut = $r.TimedOut; SwitchDone = [bool]$state.SwitchDone; SkipRequested = [bool]$state.SkipRequested }
}

# Design 21.3: the Linux side's install.env (KEY=VALUE lines), read through the same wsl seam as every
# other in-distro command. --exec runs no shell, so "~" would not expand; a one-line script does it.
# Only COGNITA_PROOF, COGNITA_VERSION and (design 22.4) COGNITA_ACCELERATION values are ever logged (the
# file also holds ports and paths).
$script:InstallEnvScript = "cat `"`$HOME/.config/cognita/install.env`" 2>/dev/null </dev/null`nexit 0`n"

function Get-LinuxInstallEnv {
    # Returns a hashtable of the file's KEY=VALUE pairs; empty when the file or the distro cannot be read.
    param($Settings)
    $env2 = @{}
    $r = $null
    try { $r = Invoke-WslScript -Settings $Settings -User (Get-LinuxUser $Settings) -ScriptText $script:InstallEnvScript -TimeoutSec 60 }
    catch { Write-Log ("install.env: read failed: {0}" -f $_.Exception.Message); return $env2 }
    if ($r.ExitCode -ne 0) { Write-Log ("install.env: read failed: exit {0}" -f $r.ExitCode); return $env2 }
    foreach ($line in ((Remove-NulAndBom $r.Stdout) -split "`r?`n")) {
        if ($line -match '^\s*([A-Za-z_][A-Za-z0-9_]*)=(.*)$') { $env2[$Matches[1]] = $Matches[2].Trim().Trim('"').Trim("'") }
    }
    $shown = @()
    foreach ($k in ($env2.Keys | Sort-Object)) {
        if ($k -eq 'COGNITA_PROOF' -or $k -eq 'COGNITA_VERSION' -or $k -eq 'COGNITA_ACCELERATION') { $shown += ('{0}={1}' -f $k, $env2[$k]) } else { $shown += $k }
    }
    Write-Log ("install.env: {0} keys read: {1}" -f $env2.Count, ($shown -join ' '))
    return $env2
}

function Get-LinuxProofState {
    # 'passed' | 'skipped' | '' (unknown: no file, no key, or a value the Linux side does not write).
    param($Settings, [string]$Caller = '')
    $e = Get-LinuxInstallEnv -Settings $Settings
    $p = ''
    if ($e.ContainsKey('COGNITA_PROOF')) { $p = ([string]$e['COGNITA_PROOF']).ToLowerInvariant() }
    if ($p -ne 'passed' -and $p -ne 'skipped') {
        if ($p) { Write-Log ("proof state ({0}): unknown value [{1}] treated as unknown" -f $Caller, $p) }
        $p = ''
    }
    Write-Log ("proof state ({0}): [{1}]" -f $Caller, $p)
    return $p
}

function Get-LinuxStatusJson {
    # cognita status --json inside the running distro; $null when it cannot be read.
    param($Settings)
    $r = Invoke-Wsl -Settings $Settings -User (Get-LinuxUser $Settings) -Command @($script:LinuxCliPath, 'status', '--json') -TimeoutSec 120
    if ($r.ExitCode -ne 0) { Write-Log ("status --json failed: exit {0}" -f $r.ExitCode); return $null }
    $t = $r.Stdout
    $a = $t.IndexOf('{'); $b = $t.LastIndexOf('}')
    if ($a -lt 0 -or $b -le $a) { Write-Log 'status --json: no JSON object in the output'; return $null }
    try { return ($t.Substring($a, $b - $a + 1) | ConvertFrom-Json) } catch { Write-Log ("status --json parse failed: {0}" -f $_.Exception.Message); return $null }
}

# ---------------------------------------------------------------------------------------
# The Admin password (design 5.6): two pipes and one standard input. Never an argument, an
# environment variable, a file, or a log line.
# ---------------------------------------------------------------------------------------
function New-BrokerPipe {
    # Current-user-only ACL, one instance, asynchronous so the wait can be bounded.
    param([string]$Name, [ValidateSet('In', 'Out')][string]$Direction)
    $sid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User
    $ps = New-Object System.IO.Pipes.PipeSecurity
    $rule = New-Object System.IO.Pipes.PipeAccessRule($sid, [System.IO.Pipes.PipeAccessRights]::ReadWrite, [System.Security.AccessControl.AccessControlType]::Allow)
    $ps.AddAccessRule($rule)
    $dir = [System.IO.Pipes.PipeDirection]::In
    if ($Direction -eq 'Out') { $dir = [System.IO.Pipes.PipeDirection]::Out }
    return (New-Object System.IO.Pipes.NamedPipeServerStream($Name, $dir, 1, [System.IO.Pipes.PipeTransmissionMode]::Byte, [System.IO.Pipes.PipeOptions]::Asynchronous, 4096, 4096, $ps))
}

function Wait-PipeConnection {
    param($Server, [int]$TimeoutSec, [string]$Description)
    $ar = $Server.BeginWaitForConnection($null, $null)
    $cond = { $ar.IsCompleted }.GetNewClosure()
    if (-not (Wait-Until -Condition $cond -TimeoutSec $TimeoutSec -IntervalMs 200 -Description $Description)) { return $false }
    $Server.EndWaitForConnection($ar)
    return $true
}

function Receive-BrokerPassword {
    # Pipe A: Setup writes the password as UTF-16LE with no terminator and closes.
    param($Server, [int]$TimeoutSec = 600)
    # RACE, measured: a client can connect, write and close in the moments between the pipe being
    # created and this wait starting (Setup does exactly that: it retries CreateFile and writes the
    # instant the pipe exists). BeginWaitForConnection then throws "The pipe is being closed", but the
    # bytes are still in the pipe's buffer. When that happens they are read straight from the pipe
    # handle, which does not care about the stream object's connection state.
    $early = $false
    try {
        if (-not (Wait-PipeConnection -Server $Server -TimeoutSec $TimeoutSec -Description 'password pipe A connection')) { throw 'Nothing was written to the password pipe in time.' }
    } catch {
        if ($_.Exception.InnerException -is [System.IO.IOException]) {
            $early = $true
            Write-Log ("broker: the client wrote and closed before the wait began ({0}); reading its buffered bytes from the handle" -f $_.Exception.InnerException.Message.Trim())
        } else { throw }
    }
    $ms = New-Object System.IO.MemoryStream
    $buf = New-Object 'byte[]' 4096
    if ($early) {
        $h = New-Object Microsoft.Win32.SafeHandles.SafeFileHandle($Server.SafePipeHandle.DangerousGetHandle(), $false)
        $fs = New-Object System.IO.FileStream($h, [System.IO.FileAccess]::Read, 4096, $false)
        try { while (($n = $fs.Read($buf, 0, $buf.Length)) -gt 0) { $ms.Write($buf, 0, $n) } } finally { $fs.Dispose() }
    } else {
        while (($n = $Server.Read($buf, 0, $buf.Length)) -gt 0) { $ms.Write($buf, 0, $n) }
    }
    $bytes = $ms.ToArray()
    Write-Log ("broker: received {0} bytes on pipe A (content not logged)" -f $bytes.Length)
    if ($bytes.Length -eq 0) { throw 'The password pipe closed without any data.' }
    return $bytes
}

function Send-BrokerPassword {
    # Pipe B: served once. The bytes are relayed exactly as received (UTF-16LE).
    param($Server, [byte[]]$Bytes, [int]$TimeoutSec = 600)
    if (-not (Wait-PipeConnection -Server $Server -TimeoutSec $TimeoutSec -Description 'password pipe B connection')) { throw 'Nobody read the password pipe in time.' }
    $Server.Write($Bytes, 0, $Bytes.Length)
    $Server.Flush()
    try { $Server.WaitForPipeDrain() } catch { Write-Log ("broker: drain wait ended: {0}" -f $_.Exception.Message) }
    Write-Log ("broker: served {0} bytes on pipe B (content not logged)" -f $Bytes.Length)
}

function Invoke-PasswordBrokerVerb {
    param($Opts)
    $a = [string](Get-Opt $Opts 'in' '')
    $b = [string](Get-Opt $Opts 'out' '')
    if ($a -notmatch '^[A-Za-z0-9_.-]{8,128}$' -or $b -notmatch '^[A-Za-z0-9_.-]{8,128}$') {
        Write-Log 'broker: pipe names missing or not acceptable'
        return (New-VerbResult 'failed' ([ordered]@{ error = 'bad pipe names' }))
    }
    $t0 = Get-ClockNow
    $bytes = $null
    $sa = $null; $sb = $null
    try {
        $sa = New-BrokerPipe -Name $a -Direction 'In'
        $bytes = Receive-BrokerPassword -Server $sa -TimeoutSec 600
        $sa.Dispose(); $sa = $null
        $sb = New-BrokerPipe -Name $b -Direction 'Out'
        $left = 600 - [int]((Get-ClockNow) - $t0).TotalSeconds
        if ($left -lt 5) { $left = 5 }
        Send-BrokerPassword -Server $sb -Bytes $bytes -TimeoutSec $left
    } catch {
        Write-Log ("broker: ended without serving the password: {0}" -f $_.Exception.Message)
        return (New-VerbResult 'failed' ([ordered]@{ error = 'broker-failed' }))
    } finally {
        if ($bytes) { [Array]::Clear($bytes, 0, $bytes.Length) }
        if ($sa) { $sa.Dispose() }
        if ($sb) { $sb.Dispose() }
    }
    return (New-VerbResult 'ok')
}

function Connect-PasswordPipe {
    # Design 19.10: ONE blocking connect, not a poll loop. NamedPipeClientStream.Connect(timeoutMs) waits
    # until the broker has created the pipe and is listening, and returns the moment it can connect; the
    # timeout is only a hang guard for a broker that never comes (a signal that is guaranteed in a correct
    # run). Nothing here sleeps or reads the helper's clock, so a test that runs the real broker waits on
    # the pipe itself, never on a timer. (Setup's own side of pipe A does the same with Connect.) A client
    # that failed to connect is disposed and never reused: measured before, a failed client did not
    # connect on a second try. Returns the connected client; throws TimeoutException when none.
    param([string]$Name, [int]$TimeoutSec)
    $c = New-Object System.IO.Pipes.NamedPipeClientStream('.', $Name, [System.IO.Pipes.PipeDirection]::In)
    try {
        $c.Connect($TimeoutSec * 1000)
        return $c
    } catch {
        $c.Dispose()
        throw
    }
}

function Read-PasswordFromPipe {
    param([string]$Name, [int]$TimeoutSec = 60)
    $client = $null
    try {
        Write-Log ("password pipe: connecting to [{0}] (blocking connect, hang guard {1}s)" -f $Name, $TimeoutSec)
        try { $client = Connect-PasswordPipe -Name $Name -TimeoutSec $TimeoutSec }
        catch {
            Write-Log ("password pipe: connect failed: {0}: {1}" -f $_.Exception.GetType().Name, $_.Exception.Message)
            throw 'The password did not arrive.'
        }
        Write-Log 'password pipe: connected'
        $ms = New-Object System.IO.MemoryStream
        $buf = New-Object 'byte[]' 4096
        while (($n = $client.Read($buf, 0, $buf.Length)) -gt 0) { $ms.Write($buf, 0, $n) }
        $bytes = $ms.ToArray()
        Write-Log ("password pipe: read {0} bytes (content not logged)" -f $bytes.Length)
        if ($bytes.Length -eq 0) { throw 'The password pipe was empty.' }
        $pw = [System.Text.Encoding]::Unicode.GetString($bytes)
        [Array]::Clear($bytes, 0, $bytes.Length)
        return $pw
    } finally { if ($client) { $client.Dispose() } }
}

function Get-AdminPasswordFromOpts {
    # Setup: --password-pipe B. Terminal: Read-Host -AsSecureString. Otherwise there is no source.
    param($Opts)
    $pipe = [string](Get-Opt $Opts 'password-pipe' '')
    if ($pipe) {
        Write-Log 'password source: pipe'
        return (Read-PasswordFromPipe -Name $pipe -TimeoutSec 60)
    }
    if (Test-Interactive) {
        Write-Log 'password source: terminal prompt'
        $p = Read-SecretLine 'Cognita Admin password'
        if (-not $p) { throw 'No password was entered.' }
        return $p
    }
    throw 'No password source: pass --password-pipe, or run this from a terminal.'
}

# ---------------------------------------------------------------------------------------
# NVIDIA Container Toolkit in the distro (design 22.5, 22.12 items 3 to 7 and 13)
# ---------------------------------------------------------------------------------------
# Both scripts run as root through "sh -s -- <version>": the pinned version is $1, never interpolated into
# the text. Every command that could read stdin has </dev/null (the script itself arrives on stdin).
# Never generates a CDI spec: Docker uses the nvidia runtime, not CDI.

# Present = all four packages at the pin (dpkg-query) AND Docker itself lists the nvidia runtime (item 5:
# Docker is asked, not the daemon.json file). Prints one "toolkit:" line; exit 0 present, 1 not.
$script:NvidiaToolkitCheckScript = @'
ver="$1"
for p in nvidia-container-toolkit nvidia-container-toolkit-base libnvidia-container1 libnvidia-container-tools; do
  have=$(dpkg-query -W -f='${db:Status-Status} ${Version}' "$p" 2>/dev/null </dev/null || true)
  if [ "$have" != "installed $ver" ]; then
    echo "toolkit: $p is ${have:-absent}, not installed $ver"
    exit 1
  fi
done
runtimes=$(timeout 60 docker info --format '{{json .Runtimes}}' 2>/dev/null </dev/null || true)
case "$runtimes" in
  *'"nvidia"'*) echo "toolkit: present $ver"; exit 0 ;;
esac
echo "toolkit: Docker does not list the nvidia runtime"
exit 1
'@
$script:NvidiaToolkitCheckScript = ($script:NvidiaToolkitCheckScript -replace "`r`n", "`n")

# The install. Keyring and list via curl to a temp file (no curl pipes: sh has no pipefail), apt pinned to
# the version with holds around it, then the Docker runtime entry and a Docker restart. If Docker does not
# answer afterwards (item 3) daemon.json goes back to what it was, Docker restarts again, and the script
# prints "toolkit: rolled back (<which step>)" and exits 3, so the install never leaves Docker down because
# of this step. (15.1.0 review: the line names the step that failed, and says so when Docker still does not
# answer after the rollback; that is a Docker problem the Linux CLI then reports in its own words.)
$script:NvidiaToolkitInstallScript = @'
set -e
export DEBIAN_FRONTEND=noninteractive
ver="$1"
pkgs="nvidia-container-toolkit nvidia-container-toolkit-base libnvidia-container1 libnvidia-container-tools"
keyring=/etc/apt/keyrings/nvidia-container-toolkit-keyring.gpg
have=$(dpkg-query -W -f='${Version}' nvidia-container-toolkit 2>/dev/null </dev/null || true)
mkdir -p /etc/apt/keyrings </dev/null
curl -fsSL -o /tmp/cognita-nv.gpg https://nvidia.github.io/libnvidia-container/gpgkey </dev/null
gpg --batch --yes --dearmor -o "$keyring" /tmp/cognita-nv.gpg </dev/null
rm -f /tmp/cognita-nv.gpg
curl -fsSL -o /tmp/cognita-nv.list https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list </dev/null
sed 's#deb https://#deb [signed-by=/etc/apt/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' /tmp/cognita-nv.list > /etc/apt/sources.list.d/nvidia-container-toolkit.list
rm -f /tmp/cognita-nv.list
apt-get update -qq </dev/null
apt-mark unhold $pkgs </dev/null || true
apt-get install -y -o Dpkg::Options::=--force-confold --allow-downgrades --allow-change-held-packages "nvidia-container-toolkit=$ver" "nvidia-container-toolkit-base=$ver" "libnvidia-container1=$ver" "libnvidia-container-tools=$ver" </dev/null
apt-mark hold $pkgs </dev/null
had_json=0
if [ -f /etc/docker/daemon.json ]; then
  cp -p /etc/docker/daemon.json /etc/docker/daemon.json.cognita-bak </dev/null
  had_json=1
fi
rollback() {
  if [ "$had_json" = 1 ]; then
    cp -p /etc/docker/daemon.json.cognita-bak /etc/docker/daemon.json </dev/null
  else
    rm -f /etc/docker/daemon.json
  fi
  if [ "$1" = restart ]; then
    timeout 90 systemctl restart docker </dev/null || true
    if ! timeout 60 docker info >/dev/null 2>&1 </dev/null; then
      echo "toolkit: rolled back ($2), and docker still does not answer"
      exit 3
    fi
  fi
  echo "toolkit: rolled back ($2)"
  exit 3
}
if ! nvidia-ctk runtime configure --runtime=docker </dev/null; then
  rollback norestart "nvidia-ctk failed"
fi
if timeout 90 systemctl restart docker </dev/null && timeout 60 docker info >/dev/null 2>&1 </dev/null; then
  echo "toolkit: installed $ver (was ${have:-none})"
  exit 0
fi
rollback restart "docker did not answer"
'@
$script:NvidiaToolkitInstallScript = ($script:NvidiaToolkitInstallScript -replace "`r`n", "`n")

function Get-NvidiaToolkitLine {
    # The script's own "toolkit:" line from its output ('' when there is none).
    param([string]$Text)
    foreach ($l in ((Remove-NulAndBom $Text) -split "`r?`n")) {
        if ($l.Trim().StartsWith('toolkit:')) { return $l.Trim() }
    }
    return ''
}

function Test-NvidiaToolkit {
    # Design 22.12 items 5 and 6: is the pinned toolkit already in the distro AND does Docker list the nvidia
    # runtime? One WSL call, no progress lines (a user is not shown work that did not happen), one log line.
    # Returns [pscustomobject]@{ Present; Detail }. Never throws: any failure to ask means "not present".
    param($Settings)
    $present = $false; $detail = ''
    try {
        $r = Invoke-WslScript -Settings $Settings -User 'root' -ScriptText $script:NvidiaToolkitCheckScript -ScriptArgs @($script:NvidiaToolkitVersion) -TimeoutSec 120
        $present = (-not $r.StartError) -and (-not $r.TimedOut) -and ($r.ExitCode -eq 0)
        $detail = Get-NvidiaToolkitLine $r.Stdout
        if (-not $detail) { $detail = ('exit {0}' -f $r.ExitCode) }
        Write-Log ("nvidia toolkit: check pin=[{0}] present={1} exit={2} timedOut={3} startError=[{4}] line=[{5}]" -f $script:NvidiaToolkitVersion, $present, $r.ExitCode, $r.TimedOut, $r.StartError, $detail)
    } catch {
        $detail = ('the check threw: ' + $_.Exception.Message)
        Write-Log ("nvidia toolkit: check failed ({0}); treated as not present" -f $_.Exception.Message)
    }
    return [pscustomobject]@{ Present = $present; Detail = $detail }
}

function Install-NvidiaToolkit {
    # Design 22.5: installs the pinned toolkit in the distro (as root), with progress lines (stage nvidia:
    # start, a heartbeat every 5 s, then done or the warning). Returns [pscustomobject]@{ Ok; Detail }. A
    # failure is a WARNING here and never a stop: the caller carries on, and the Linux CLI (install) falls
    # back to the CPU by itself with its own note. Never throws.
    param($Settings)
    $title = 'NVIDIA support in Cognita''s Linux'
    $t0 = Get-ClockNow
    Write-ProgressLine -Stage 'nvidia' -Title $title -State 'start'
    $beat = { Write-ProgressLine -Stage 'nvidia' -Title 'NVIDIA support in Cognita''s Linux' -State 'progress' -Message 'Still working.' }
    $ok = $false; $why = ''; $line = ''
    try {
        $r = Invoke-WslScript -Settings $Settings -User 'root' -ScriptText $script:NvidiaToolkitInstallScript -ScriptArgs @($script:NvidiaToolkitVersion) -TimeoutSec 900 -OnPoll $beat -PollIntervalMs 5000
        $line = Get-NvidiaToolkitLine $r.Stdout
        $errTail = ((Remove-NulAndBom $r.Stderr) -replace '\s+', ' ').Trim()
        if ($errTail.Length -gt 400) { $errTail = $errTail.Substring($errTail.Length - 400) }
        $secs = [int]((Get-ClockNow) - $t0).TotalSeconds
        Write-Log ("nvidia toolkit: install pin=[{0}] exit={1} timedOut={2} startError=[{3}] seconds={4} line=[{5}] stderr tail=[{6}]" -f $script:NvidiaToolkitVersion, $r.ExitCode, $r.TimedOut, $r.StartError, $secs, $line, $errTail)
        if ($r.StartError) { $why = [string]$r.StartError }
        elseif ($r.TimedOut) { $why = 'it did not finish in 15 minutes' }
        # 15.1.0 review: the script says which rollback it was, and whether Docker answers after it.
        elseif ($r.ExitCode -eq 3 -and $line -match 'rolled back \(nvidia-ctk failed\)') { $why = 'registering the nvidia runtime with Docker failed, so the change was undone' }
        elseif ($r.ExitCode -eq 3 -and $line -match 'docker still does not answer') { $why = 'Docker did not start with it; the change was undone, but Docker still does not answer' }
        elseif ($r.ExitCode -eq 3 -and $line -match 'rolled back') { $why = 'Docker did not start with it, so the change was undone' }
        elseif ($r.ExitCode -ne 0) {
            $tail = $errTail; if ($tail.Length -gt 200) { $tail = $tail.Substring($tail.Length - 200) }
            $why = ('exit {0}' -f $r.ExitCode); if ($tail) { $why += ': ' + $tail }
        } else { $ok = $true }
    } catch {
        $why = $_.Exception.Message
        Write-Log ("nvidia toolkit: install threw: {0}" -f $why)
    }
    if ($ok) {
        Write-ProgressLine -Stage 'nvidia' -Title $title -State 'done'
    } else {
        # Design 22.12 item 13: one wording for install and update.
        Write-ProgressLine -Stage 'nvidia' -Title $title -State 'warning' -Message ("Setup could not install NVIDIA's container support in Cognita's Linux ({0}). If Cognita cannot use the card without it, it uses the CPU." -f $why) -Fix 'Run Setup again later.'
    }
    return [pscustomobject]@{ Ok = $ok; Detail = $(if ($ok) { $line } else { $why }) }
}

function Invoke-NvidiaToolkitStep {
    # The shared step of install (before the Linux CLI, when nvidia was asked) and update (3b, when the
    # recorded acceleration is nvidia): ask whether the pinned toolkit is already there; install it only
    # when it is not. A failure is a warning, never a stop. Returns [pscustomobject]@{ Ran; Ok; Detail }.
    param($Settings, [string]$Caller = '')
    try {
        $t = Test-NvidiaToolkit -Settings $Settings
        if ($t.Present) {
            Write-Log ("nvidia toolkit step ({0}): already present at the pin; no install, no progress lines" -f $Caller)
            return [pscustomobject]@{ Ran = $false; Ok = $true; Detail = $t.Detail }
        }
        Write-Log ("nvidia toolkit step ({0}): not present ({1}); installing the pin" -f $Caller, $t.Detail)
        $i = Install-NvidiaToolkit -Settings $Settings
        return [pscustomobject]@{ Ran = $true; Ok = $i.Ok; Detail = $i.Detail }
    } catch {
        Write-Log ("nvidia toolkit step ({0}) threw: {1}" -f $Caller, $_.Exception.Message)
        Write-ProgressLine -Stage 'nvidia' -Title 'NVIDIA support in Cognita''s Linux' -State 'warning' -Message ("Setup could not install NVIDIA's container support in Cognita's Linux ({0}). If Cognita cannot use the card without it, it uses the CPU." -f $_.Exception.Message) -Fix 'Run Setup again later.'
        return [pscustomobject]@{ Ran = $true; Ok = $false; Detail = $_.Exception.Message }
    }
}

function Get-SettingsAcceleration {
    # The recorded acceleration profile, lower-cased; '' when none is recorded (an install from before 15.1).
    param($Settings)
    if ($Settings -and $Settings.PSObject.Properties['acceleration'] -and $Settings.acceleration) { return ([string]$Settings.acceleration).Trim().ToLowerInvariant() }
    return ''
}

function Update-SettingsAcceleration {
    # Design 22.12 item 1a: record what `status --json` says, and ONLY when it says something. A known value
    # is never overwritten with ''. Returns the value the status reported ('' when it did not say), which is
    # what the verb's result line carries (never a stale recorded value). The caller saves the settings.
    param($Settings, $Status, [string]$Caller = '')
    $said = ''
    if ($Status -and $Status.PSObject.Properties['acceleration'] -and $Status.acceleration) { $said = ([string]$Status.acceleration).Trim().ToLowerInvariant() }
    $was = Get-SettingsAcceleration $Settings
    if ($said) { Set-SettingProp $Settings 'acceleration' $said }
    elseif (-not $Settings.PSObject.Properties['acceleration']) { Set-SettingProp $Settings 'acceleration' '' }
    Write-Log ("acceleration ({0}): status said [{1}], settings had [{2}], settings now [{3}]" -f $Caller, $said, $was, (Get-SettingsAcceleration $Settings))
    return $said
}

# ---------------------------------------------------------------------------------------
# install (design 7.1, 7.2)
# ---------------------------------------------------------------------------------------
function Get-LinuxInstallArgs {
    # Design 18.2: an empty $AdminUser means "Cognita is already installed here" (linux_installed_version
    # is set), and --admin-user is then NOT passed: the Linux side keeps the user it has.
    # Design 22.3: $Acceleration is what Setup asked for. 'nvidia' -> --acceleration nvidia plus
    # --acceleration-fallback cpu (the Linux CLI then falls back to the CPU by itself, with its own note,
    # when it cannot use the card); 'cpu' -> --acceleration cpu; '' -> no flag (the Linux side keeps what its
    # env file says). Placed after --workspace.
    param([string]$Display, [string]$AdminUser, [string]$Workspace, [int]$McpPort, [int]$AdminPort, [string]$Acceleration = '')
    $a = @('install', '--non-interactive', '--yes', '--documents', (Get-RootMountPoint 1), '--documents-display', $Display)
    if ($AdminUser) { $a += @('--admin-user', $AdminUser) }
    $a += @('--admin-password-stdin', '--command-name', 'cognita',
        '--remote-access', 'no', '--workspace', $Workspace)
    if ($Acceleration -eq 'nvidia') { $a += @('--acceleration', 'nvidia', '--acceleration-fallback', 'cpu') }
    elseif ($Acceleration -eq 'cpu') { $a += @('--acceleration', 'cpu') }
    $a += @('--mcp-port', [string]$McpPort, '--admin-port', [string]$AdminPort)
    return $a
}

function Get-PortFromUrl {
    param([string]$Url)
    try { return ([uri]$Url).Port } catch { return 0 }
}

function Invoke-InstallVerb {
    param($Opts)
    $t0 = Get-ClockNow
    $settings = Read-Settings
    # The password is read first: the broker serves it for at most 10 minutes and a long import must
    # not use that up. It lives in this variable only.
    $pw = $null
    try { $pw = Get-AdminPasswordFromOpts $Opts }
    catch {
        Write-ProgressLine -Stage 'password' -Title 'Admin password' -State 'failed' -Message $_.Exception.Message -Fix 'Run Setup again.'
        return (New-VerbResult 'failed' ([ordered]@{ reason = 'no-password' }))
    }
    # Design 22.3: what Setup asked for. Absent means the Linux CLI gets no acceleration flag and keeps what its
    # env file says. Anything but cpu or nvidia is a Setup bug, not a user state.
    $accelAsked = ''
    $accelRaw = Get-Opt $Opts 'acceleration' $null
    if ($null -ne $accelRaw) {
        $accelAsked = ([string]$accelRaw).Trim().ToLowerInvariant()
        if ($accelAsked -ne 'cpu' -and $accelAsked -ne 'nvidia') {
            Write-Log ("install: --acceleration [{0}] is not cpu or nvidia; refused" -f $accelRaw)
            Write-ProgressLine -Stage 'acceleration' -Title 'Acceleration' -State 'failed' -Message 'Setup passed an acceleration the helper does not know.' -Fix 'Run Setup again. If it keeps failing, use Save diagnostics.'
            return (New-VerbResult 'failed' ([ordered]@{ reason = 'bad-acceleration' }))
        }
    }
    $reclaimAsked = [bool](Get-Opt $Opts 'wsl-memory-reclaim' $false)
    $adminUser = [string](Get-Opt $Opts 'admin-user' 'admin')
    $folder = [string](Get-Opt $Opts 'projects-folder' '')
    $existingRoots = @(Get-SettingsRoots $settings)
    if (-not $folder -and $existingRoots.Count -gt 0) { $folder = [string]$existingRoots[0].windows; Write-Log ("install: no --folder given, reusing root 1 [{0}]" -f $folder) }
    if (-not $folder) {
        Write-ProgressLine -Stage 'folder' -Title 'Projects folder' -State 'failed' -Message 'No projects folder was chosen.' -Fix 'Run Setup again and choose a folder.'
        return (New-VerbResult 'failed' ([ordered]@{ reason = 'no-folder' }))
    }
    $vhd = [string](Get-Opt $Opts 'data-dir' '')
    # Design 18.2 guard: a distro that is ours already lives where settings.vhd_dir says, and its data
    # cannot move by passing another --data-dir (a wrong caller must not be able to point a repair at a
    # second location). Ownership is judged from the disk folder alone (-SkipMarker: no WSL command yet).
    if ($settings -and $settings.vhd_dir) {
        $ownEarly = Get-DistroOwnership -Settings $settings -SkipMarker
        if ($ownEarly.Owned) {
            $same = $false
            try { $same = ($vhd -and ((Get-NormalizedPath $vhd) -ieq (Get-NormalizedPath ([string]$settings.vhd_dir)))) }
            catch { $same = $false; Write-Log ("install: --data-dir [{0}] could not be compared with the disk folder ({1}); treated as different" -f $vhd, $_.Exception.Message) }
            if ($vhd -and -not $same) {
                Write-Log ("install: --data-dir [{0}] differs from the owned distro's disk folder [{1}]; ignored" -f $vhd, $settings.vhd_dir)
            }
            $vhd = [string]$settings.vhd_dir
        }
    }
    if (-not $vhd -and $settings -and $settings.vhd_dir) { $vhd = [string]$settings.vhd_dir }
    if (-not $vhd) { $vhd = Get-DefaultVhdDir }
    $v = Test-RootPath -Path $folder -Settings $settings -ExtraOwnFolder $vhd
    if (-not $v.Ok) {
        Write-ProgressLine -Stage 'folder' -Title 'Projects folder' -State 'failed' -Message $v.Reason -Fix 'Choose another folder.'
        return (New-VerbResult 'failed' ([ordered]@{ reason = 'bad-folder' }))
    }

    # Decision: import, skip (our distro exists), redo (interrupted import), or refuse (foreign).
    $needImport = $true
    if ($settings) {
        $own = Get-DistroOwnership -Settings $settings
        if ($own.Exists -and -not $own.Owned) {
            Write-ProgressLine -Stage 'import' -Title 'Setting up Cognita''s Linux' -State 'failed' -Message 'A WSL distro named Cognita already exists and was not created by this Setup.' -Fix 'Rename or remove it, then run Setup again.'
            return (New-VerbResult 'failed' ([ordered]@{ reason = 'foreign-distro' }))
        }
        if ($own.Exists -and $own.Owned -and $settings.state -eq 'import-pending') {
            Write-Log 'install: interrupted import found; unregistering our half-imported distro and importing again'
            if (-not (Invoke-UnregisterDistro -Settings $settings)) {
                Write-ProgressLine -Stage 'import' -Title 'Setting up Cognita''s Linux' -State 'failed' -Message 'The half-finished import could not be removed.' -Fix 'Run Setup again.'
                return (New-VerbResult 'failed' ([ordered]@{ reason = 'unregister-failed' }))
            }
            # A new distro has no Cognita in it (design 18.2): the recorded Linux install is void.
            Set-SettingProp $settings 'linux_installed_version' ''
            Set-SettingProp $settings 'admin_user' ''
            Set-SettingProp $settings 'linux_proof' ''
        } elseif ($own.Exists -and $own.Owned) {
            $needImport = $false
            Write-Log ("install: our distro exists (state={0} linux_installed_version=[{1}]); import skipped" -f $settings.state, $settings.linux_installed_version)
        } elseif (-not $own.Exists -and ($settings.state -eq 'installed' -or [string]$settings.linux_installed_version -or @(Get-SettingsRoots $settings).Count -gt 0)) {
            # Design 19.2 item 4: whatever `state` says. A keep-data uninstall leaves state=uninstalled with
            # the folder list and the recorded Linux install; if the distro is then removed (wsl --unregister
            # by hand, a disk cleanup), those records describe a Cognita that no longer exists. Only
            # state=installed used to reset them, so an `uninstalled` + missing-distro install kept root 1
            # and refused every other projects folder with a stale "folder-not-first".
            Write-Log ("install: the distro is gone (state={0} linux_installed_version=[{1}] roots={2}); importing a fresh one and resetting the installation id, the folder list and the recorded Linux install" -f $settings.state, $settings.linux_installed_version, @(Get-SettingsRoots $settings).Count)
            Set-SettingProp $settings 'installation_id' ([guid]::NewGuid().ToString())
            Set-SettingProp $settings 'roots' @()
            Set-SettingProp $settings 'linux_installed_version' ''
            Set-SettingProp $settings 'admin_user' ''
            Set-SettingProp $settings 'linux_proof' ''
        }
    }
    # Design 18.2 guard: once root 1 exists (after any reset above), the projects folder is that folder.
    # Another one is `cognita add-folder`'s job, never an install's: a second Setup run must not remount
    # the first root elsewhere under a Cognita that already has documents indexed from it.
    $rootOne = @(Get-SettingsRoots $settings | Where-Object { [int]$_.n -eq 1 })
    if ($rootOne.Count -gt 0 -and [int]$v.Existing -ne 1) {
        Write-Log ("install: refused projects folder [{0}]: root 1 already exists as [{1}]" -f $v.Path, $rootOne[0].windows)
        # Design 19.2 item 7: says what IS true (the folder Cognita already uses) and where to go next.
        Write-ProgressLine -Stage 'folder' -Title 'Projects folder' -State 'failed' -Message ('Cognita already uses {0} as its projects folder.' -f [string]$rootOne[0].windows) -Fix 'Keep that folder; add others later with cognita add-folder.'
        return (New-VerbResult 'failed' ([ordered]@{ reason = 'folder-not-first'; root1 = [string]$rootOne[0].windows }))
    }
    # Design 19.2 item 8: a recorded Funnel publishes THIS Cognita's MCP port. Installing with another
    # --mcp-port would move Cognita off the port the Funnel forwards to, and remote access would silently
    # stop working. Refused before anything in settings changes. Only when --mcp-port is given: without it
    # the recorded port stays as it is. The fix names the command that really turns the Funnel off: neither
    # this helper nor the Linux CLI has a `remote-access --off` (checked 2026-09-29), and the helper's own
    # uninstall uses exactly `tailscale funnel --https=<port> off`.
    # Design 19.11 R7: the record is only believed while Tailscale still serves it. A Funnel the user
    # turned off by hand (or a Tailscale that is gone) is cleared here, logged, and does not block.
    # R6: the refusal reports the OLD MCP port as funnel_target; `funnel_port` means the public https
    # port everywhere else (the `state` verb), so one key never carries two meanings.
    if ($settings -and $settings.funnel -and $null -ne (Get-Opt $Opts 'mcp-port' $null)) {
        $askedMcp = [int](Get-Opt $Opts 'mcp-port' 0)
        $funnelTarget = [int]$settings.funnel.target
        Write-Log ("install: funnel guard asked mcp-port={0} funnel target={1} funnel https port={2}" -f $askedMcp, $funnelTarget, $settings.funnel.https_port)
        # Tailscale is asked only when the port really differs: the same port never conflicts.
        if ($askedMcp -ne $funnelTarget -and (Clear-FunnelRecordIfNotServed -Settings $settings -Caller 'install')) {
            Write-ProgressLine -Stage 'ports' -Title 'MCP port' -State 'failed' -Message ('Remote access uses port {0}.' -f $funnelTarget) -Fix ('Turn remote access off first (tailscale funnel --https={0} off), then change the port.' -f $settings.funnel.https_port)
            return (New-VerbResult 'failed' ([ordered]@{ reason = 'port-in-use-by-funnel'; funnel_target = $funnelTarget; asked = $askedMcp }))
        }
    }
    if (-not $settings) { $settings = New-Settings -VhdDir $vhd }
    Set-SettingProp $settings 'vhd_dir' $vhd
    Set-SettingProp $settings 'mcp_port' ([int](Get-Opt $Opts 'mcp-port' $settings.mcp_port))
    Set-SettingProp $settings 'admin_port' ([int](Get-Opt $Opts 'admin-port' $settings.admin_port))
    Set-SettingProp $settings 'workspace' ([string](Get-Opt $Opts 'workspace' $settings.workspace))
    $sv = [string](Get-Opt $Opts 'setup-version' ''); if ($sv) { Set-SettingProp $settings 'setup_version' $sv }
    $sr = [string](Get-Opt $Opts 'setup-revision' ''); if ($sr) { Set-SettingProp $settings 'setup_revision' ([int]$sr) }
    $linuxWasInstalled = [bool]([string]$settings.linux_installed_version)
    Write-Log ("install: decision needImport={0} folder=[{1}] vhd=[{2}] ports={3}/{4} workspace={5} linuxAlreadyInstalled={6} acceleration=[{7}] wslMemoryReclaim={8}" -f $needImport, $v.Path, $vhd, $settings.mcp_port, $settings.admin_port, $settings.workspace, $linuxWasInstalled, $accelAsked, $reclaimAsked)

    # Design 22.9: first, before the import or anything in the distro, so the setting is there the next time
    # WSL starts. A failure is a warning inside the function and never stops the install.
    if ($reclaimAsked) { [void](Set-WslReclaimSetting) }

    if ($needImport) {
        $imp = Invoke-ImportImage -Settings $settings -ImagePath ([string](Get-Opt $Opts 'image' '')) -ImageSha256 ([string](Get-Opt $Opts 'image-sha256' '')) -VhdDir $vhd
        if ($imp.Status -eq 'restart-required') { return (New-VerbResult 'restart-required' ([ordered]@{ reason = $imp.Reason })) }
        if ($imp.Status -ne 'ok') { return (New-VerbResult 'failed' ([ordered]@{ reason = $imp.Reason })) }
    } else {
        Set-SettingProp $settings 'state' 'installed'
        Set-SettingProp $settings 'resume' $null
        Save-Settings $settings
    }

    # Keepalive now, before anything else runs in the distro, so it cannot idle-stop between stages.
    Write-ProgressLine -Stage 'keepalive' -Title 'Keeping Cognita running' -State 'start'
    if (-not (Register-Keepalive)) {
        Write-ProgressLine -Stage 'keepalive' -Title 'Keeping Cognita running' -State 'warning' -Message 'Could not register the sign-in task, so Cognita will not start by itself after a restart.' -Fix 'Run Setup again to retry.'
    }
    [void](Start-Keepalive)
    if (-not (Wait-DistroReady -Settings $settings -TimeoutSec 300 -Stage 'keepalive')) {
        Write-ProgressLine -Stage 'keepalive' -Title 'Keeping Cognita running' -State 'failed' -Message 'Cognita''s Linux did not finish starting within 5 minutes.' -Fix 'Run Setup again. If it keeps failing, use Save diagnostics.'
        return (New-VerbResult 'failed' ([ordered]@{ reason = 'distro-not-ready' }))
    }
    Write-ProgressLine -Stage 'keepalive' -Title 'Keeping Cognita running' -State 'done'

    # Workspace needs /dev/kvm (W12, C7)
    $kvm = Invoke-Wsl -Settings $settings -User (Get-LinuxUser $settings) -Command @('test', '-e', '/dev/kvm') -TimeoutSec 60
    $hasKvm = ($kvm.ExitCode -eq 0)
    $ws = [string]$settings.workspace
    if ($ws -eq 'on' -and -not $hasKvm) {
        $ws = 'off'
        Set-SettingProp $settings 'workspace' 'off'
        Write-ProgressLine -Stage 'workspace' -Title 'Workspace' -State 'warning' -Message 'This PC''s WSL has no /dev/kvm, so Workspace (sandboxed code running) will be off. Everything else will be installed.' -Fix 'To use Workspace, turn on virtualization (VT-x/AMD-V) in the BIOS, or nested virtualization on a virtual machine, then run: cognita install --workspace on' -MessageId 'setup.workspace.kvm_unavailable'
    }
    Write-Log ("install: kvm={0} workspace effective={1}" -f $hasKvm, $ws)

    # Projects folder (root 1): the fstab line, ONE distro restart that applies it, the mount in Docker's
    # view and the access test (design 18.1 rule 4; -SyncOthers repairs the other roots' lines too), then
    # every root must be up before the Linux CLI runs.
    $add = Add-CognitaRoot -Settings $settings -WindowsPath $v.Path -Stage 'folder' -SyncOthers
    if (-not $add.Ok) { return (New-VerbResult 'failed' ([ordered]@{ reason = 'folder'; detail = $add.Reason })) }
    $down = Assert-RootsAvailable -Settings $settings
    if ($down) {
        Write-ProgressLine -Stage 'folder' -Title 'Projects folder' -State 'failed' -Message $down
        return (New-VerbResult 'failed' ([ordered]@{ reason = 'root-down' }))
    }

    # Design 18.2: when the tree in the distro is older than this Setup (an install or repair by a newer
    # Setup over an older image or an older update), the same tree swap `update` does runs before the Linux
    # CLI, so `cognita install` runs this Setup's code. Only when Setup handed over its source tarball.
    $tar = [string](Get-Opt $Opts 'src' '')
    $treeVersion = [string](Get-Opt $Opts 'setup-version' '')
    if ($tar -and $treeVersion) {
        if (-not (Test-Path -LiteralPath $tar)) {
            Write-Log ("install: --src [{0}] does not exist; tree check skipped" -f $tar)
        } elseif (Test-CognitaTreeOlder -Settings $settings -SetupVersion $treeVersion) {
            if (-not (Install-CognitaTree -Settings $settings -Tarball $tar -Sha256 ([string](Get-Opt $Opts 'src-sha256' '')) -Version $treeVersion -Stage 'install.tree')) {
                return (New-VerbResult 'failed' ([ordered]@{ reason = 'tree' }))
            }
        } else { Write-Log 'install: the distro''s tree is not older than this Setup; no tree swap' }
    } else { Write-Log ("install: no --src or no --setup-version given (src=[{0}] version=[{1}]); tree check skipped" -f $tar, $treeVersion) }

    # The real install: the Linux CLI
    $display = [string](Get-SettingsRoots $settings | Where-Object { [int]$_.n -eq 1 } | Select-Object -First 1).windows
    if (-not $display) { $display = $v.Path }
    # --admin-user only on a first install: with Cognita already in the distro the Linux side keeps its user.
    $adminForCli = $adminUser
    if ($linuxWasInstalled) { $adminForCli = ''; Write-Log 'install: Cognita is already installed in this distro; --admin-user is not passed' }
    # Design 22.3: nvidia asked -> the pinned toolkit first (after the tree swap, so the distro is up and its
    # roots are mounted). Its failure is a warning and the Linux CLI still runs with --acceleration-fallback
    # cpu, which then falls back by itself with its own note.
    if ($accelAsked -eq 'nvidia') {
        $tk = Invoke-NvidiaToolkitStep -Settings $settings -Caller 'install'
        Write-Log ("install: nvidia toolkit step ran={0} ok={1} detail=[{2}]" -f $tk.Ran, $tk.Ok, $tk.Detail)
    }
    $cliArgs = Get-LinuxInstallArgs -Display $display -AdminUser $adminForCli -Workspace $ws -McpPort ([int]$settings.mcp_port) -AdminPort ([int]$settings.admin_port) -Acceleration $accelAsked
    $lr = Invoke-CognitaLinux -Settings $settings -CliArgs $cliArgs -StdinText ($pw + "`n") -Stage 'cognita' -TimeoutSec 7200 -Title 'Installing Cognita'
    $pw = $null
    if ($lr.ExitCode -eq 10) {
        Write-ProgressLine -Stage 'cognita' -Title 'Installing Cognita' -State 'failed' -Message 'The installer stopped for a logout or reboot (exit 10), which cannot happen in this image.' -Fix 'Run Setup again. If it keeps failing, use Save diagnostics.'
        return (New-VerbResult 'failed' ([ordered]@{ reason = 'exit-10' }))
    }
    if ($lr.ExitCode -ne 0) {
        if (-not $lr.Failed) {
            Write-ProgressLine -Stage 'cognita' -Title 'Installing Cognita' -State 'failed' -Message ('The Cognita installer failed (exit {0}). {1}' -f $lr.ExitCode, $lr.Summary) -Fix 'Run Setup again. If it keeps failing, use Save diagnostics.'
        }
        return (New-VerbResult 'failed' ([ordered]@{ reason = 'linux-install'; exit = $lr.ExitCode }))
    }
    # Design 18.2: the install state is recorded only now, after `cognita install` exited 0; a failure
    # above leaves both keys as they were. The version is Setup's own, else the one Linux reports below.
    $recordVersion = [string](Get-Opt $Opts 'setup-version' '')
    if ($recordVersion) { Set-SettingProp $settings 'linux_installed_version' $recordVersion }
    if (-not $linuxWasInstalled) { Set-SettingProp $settings 'admin_user' $adminUser }
    Save-Settings $settings
    Write-Log ("install: recorded linux_installed_version=[{0}] admin_user=[{1}]" -f $settings.linux_installed_version, $settings.admin_user)

    # Ports come from the Linux side after an install; say so when they differ.
    $st = Get-LinuxStatusJson -Settings $settings
    $version = ''; $public = ''
    if ($st) {
        $version = [string]$st.version
        if ($st.public_url) { $public = [string]$st.public_url }
        $mp = Get-PortFromUrl ([string]$st.mcp_url); $ap = Get-PortFromUrl ([string]$st.admin_url)
        if ($mp -gt 0 -and $mp -ne [int]$settings.mcp_port) { Write-InfoLine ("The MCP port is {0}, not the {1} that was asked for." -f $mp, $settings.mcp_port); Set-SettingProp $settings 'mcp_port' $mp }
        if ($ap -gt 0 -and $ap -ne [int]$settings.admin_port) { Write-InfoLine ("The Admin port is {0}, not the {1} that was asked for." -f $ap, $settings.admin_port); Set-SettingProp $settings 'admin_port' $ap }
        if ($st.workspace) { Set-SettingProp $settings 'workspace' ([string]$st.workspace) }
    }
    # Design 22.3 + 22.12 item 1a: settings keep the profile the Linux side reports, never '' over a known one.
    $accelReported = Update-SettingsAcceleration -Settings $settings -Status $st -Caller 'install'
    if (-not [string]$settings.linux_installed_version -and $version) {
        # No --setup-version was given (a hand-run install): the version the Linux side reports stands in.
        Set-SettingProp $settings 'linux_installed_version' $version
        Write-Log ("install: linux_installed_version taken from status --json: {0}" -f $version)
    }
    Save-Settings $settings
    $exe = Join-Path (Join-Path (Get-DataRoot) 'bin') 'cognita.exe'
    if (-not (Test-Path -LiteralPath $exe)) {
        Write-ProgressLine -Stage 'command' -Title 'The cognita command' -State 'warning' -Message ('{0} was not found.' -f $exe) -Fix 'Run Setup again to repair the cognita command.'
    } else { Write-Log ("install: cognita.exe present at {0}" -f $exe) }
    # Design 21.3: what the Linux side recorded about the self-test (passed | skipped | '' unknown), so
    # Setup's Finished page can say the self-tests were skipped.
    $proof = Get-LinuxProofState -Settings $settings -Caller 'install'
    Set-SettingProp $settings 'linux_proof' $proof
    Save-Settings $settings
    $secs = [int]((Get-ClockNow) - $t0).TotalSeconds
    Write-Log ("install: finished in {0}s version={1} proof=[{2}] skipRequested={3} acceleration asked=[{4}] reported=[{5}]" -f $secs, $version, $proof, $lr.SkipRequested, $accelAsked, $accelReported)
    return (New-VerbResult 'ok' ([ordered]@{
        version = $version; public_url = $public; admin_url = [string]$st.admin_url; mcp_url = [string]$st.mcp_url
        workspace = [string]$settings.workspace; mcp_port = $settings.mcp_port; admin_port = $settings.admin_port; seconds = $secs; acceleration = $accelReported; proof = $proof }))
}

# ---------------------------------------------------------------------------------------
# update (design 7.3)
# ---------------------------------------------------------------------------------------
function Get-TreeInstallScript {
    # Extracts the source tarball (its contents sit at its root, no top-level folder) into
    # /opt/cognita/trees/<version>-<commit12>, where the commit comes from the tree's own
    # .cognita-tree stamp, switches the src symlink atomically and keeps the newest three trees.
    # Args: tarball path, sha256 (may be empty), Cognita version.
    $s = @(
        'set -e',
        'tarball="$1"; want="$2"; ver="$3"',
        'base=/opt/cognita/trees',
        'mkdir -p "$base"',
        'rm -rf "$base"/.new-*',
        'if [ -n "$want" ]; then',
        '  have=$(sha256sum "$tarball" </dev/null | cut -d '' '' -f1)',
        '  if [ "$have" != "$want" ]; then echo "source tarball checksum mismatch: $have" >&2; exit 20; fi',
        'fi',
        'tmp="$base/.new-$$"',
        'mkdir "$tmp"',
        'tar -xzf "$tarball" -C "$tmp" </dev/null',
        'if [ ! -e "$tmp/.cognita-tree" ]; then echo "source tarball has no .cognita-tree stamp" >&2; rm -rf "$tmp"; exit 21; fi',
        'commit=$(sed -n ''s/^commit:[[:space:]]*//p'' "$tmp/.cognita-tree" </dev/null | head -n 1 | tr -d ''\r '')',
        'if [ -z "$commit" ]; then echo ".cognita-tree has no commit line" >&2; rm -rf "$tmp"; exit 22; fi',
        'name="$ver-$(printf ''%s'' "$commit" | cut -c1-12)"',
        'rm -rf "$base/$name"',
        'mv "$tmp" "$base/$name"',
        'rm -f /opt/cognita/src.new',
        'ln -s "$base/$name" /opt/cognita/src.new',
        'mv -T /opt/cognita/src.new /opt/cognita/src',
        'cur=$(readlink -f /opt/cognita/src)',
        'for d in $(ls -1t "$base" | tail -n +4); do',
        '  if [ "$base/$d" != "$cur" ]; then rm -rf "$base/$d"; fi',
        'done',
        'echo "tree $name is now current"',
        'exit 0'
    )
    return (($s -join "`n") + "`n")
}

function Get-CognitaTreeVersion {
    # The Cognita version of the source tree the distro runs from: /opt/cognita/src is a symlink to
    # /opt/cognita/trees/<version>-<commit12> (the image recipe and Get-TreeInstallScript both make it
    # so), so the version is read from that directory name. '' when it cannot be read or parsed.
    param($Settings)
    $r = Invoke-Wsl -Settings $Settings -User 'root' -Command @('readlink', '-f', '/opt/cognita/src') -TimeoutSec 60
    if ($r.StartError -or $r.TimedOut -or $r.ExitCode -ne 0) {
        Write-Log ("tree version: readlink exit={0} startError=[{1}]; unknown" -f $r.ExitCode, $r.StartError)
        return ''
    }
    $leaf = ((Remove-NulAndBom $r.Stdout).Trim() -split '/')[-1]
    if ($leaf -match '^(\d+(\.\d+){1,3})-[0-9A-Za-z]+$') {
        Write-Log ("tree version: {0} (directory {1})" -f $Matches[1], $leaf)
        return $Matches[1]
    }
    Write-Log ("tree version: directory name [{0}] does not look like <version>-<commit>; unknown" -f $leaf)
    return ''
}

function Test-CognitaTreeOlder {
    # True when the distro's tree is older than $SetupVersion (or its version cannot be told: swapping a
    # tree is idempotent, so "cannot tell" errs toward swapping). A newer or equal tree is left alone.
    param($Settings, [string]$SetupVersion)
    $have = Get-CognitaTreeVersion -Settings $Settings
    if (-not $have) { Write-Log 'tree check: version unknown, so the tree will be swapped'; return $true }
    try {
        $older = ([version]$have -lt [version]$SetupVersion)
    } catch {
        Write-Log ("tree check: could not compare {0} with {1} ({2}); the tree will be swapped" -f $have, $SetupVersion, $_.Exception.Message)
        return $true
    }
    Write-Log ("tree check: distro tree {0}, Setup {1}, older={2}" -f $have, $SetupVersion, $older)
    return $older
}

function Install-CognitaTree {
    # Design 7.3 step 4: the source tree swap, shared by update and (18.2) install. Returns $true on
    # success; on failure it has written the failed progress line and the caller returns its result.
    param($Settings, [string]$Tarball, [string]$Sha256, [string]$Version, [string]$Stage = 'update.tree')
    Write-ProgressLine -Stage $Stage -Title 'Updating Cognita''s files' -State 'start'
    $tr = Invoke-WslScript -Settings $Settings -User 'root' -ScriptText (Get-TreeInstallScript) -ScriptArgs @((ConvertTo-WslMntPath $Tarball), $Sha256, $Version) -TimeoutSec 900
    Write-Log ("tree swap: version={0} tarball=[{1}] exit={2}" -f $Version, $Tarball, $tr.ExitCode)
    if ($tr.ExitCode -ne 0) {
        Write-ProgressLine -Stage $Stage -Title 'Updating Cognita''s files' -State 'failed' -Message ('Could not install the new Cognita files (exit {0}): {1}' -f $tr.ExitCode, (Limit-LogText (($tr.Stderr + ' ' + $tr.Stdout).Trim()) 300)) -Fix 'Run Setup again. Your installed Cognita was not changed.'
        return $false
    }
    Write-ProgressLine -Stage $Stage -Title 'Updating Cognita''s files' -State 'done'
    return $true
}

function Compare-DottedVersion {
    # Numeric, dotted compare (design 19.2 item 6): -1 when $A is older than $B, 0 when equal, 1 when newer.
    # Components are compared as integers ("14.10.0" is newer than "14.9.0"; a text compare gets that
    # wrong), a missing component counts as 0 ("14.2" equals "14.2.0"). $null when either side is not
    # made of dot-separated whole numbers, so the caller can log "cannot tell" and decide.
    param([string]$A, [string]$B)
    $pa = @(([string]$A).Trim() -split '\.')
    $pb = @(([string]$B).Trim() -split '\.')
    foreach ($p in ($pa + $pb)) { if ($p -notmatch '^\d{1,9}$') { return $null } }
    $n = [Math]::Max($pa.Count, $pb.Count)
    for ($i = 0; $i -lt $n; $i++) {
        $x = 0; if ($i -lt $pa.Count) { $x = [int64]$pa[$i] }
        $y = 0; if ($i -lt $pb.Count) { $y = [int64]$pb[$i] }
        if ($x -lt $y) { return -1 }
        if ($x -gt $y) { return 1 }
    }
    return 0
}

function Invoke-UpdateVerb {
    param($Opts)
    $settings = Read-Settings
    if (-not $settings -or $settings.state -ne 'installed') {
        Write-ProgressLine -Stage 'update' -Title 'Update' -State 'failed' -Message 'Cognita is not installed on this PC.' -Fix 'Run Setup to install it.'
        return (New-VerbResult 'failed' ([ordered]@{ reason = 'not-installed' }))
    }
    # Design 19.2 item 6: no downgrade. `update` moves Cognita forward only; when the recorded Linux
    # install is NEWER than this Setup, running the update would put an older tree over newer data.
    # Decided before the password is read: nothing about it needs the password or the distro.
    $installedVer = [string]$settings.linux_installed_version
    $setupVer = [string](Get-Opt $Opts 'setup-version' '')
    if ($installedVer -and $setupVer) {
        $cmp = Compare-DottedVersion $installedVer $setupVer
        Write-Log ("update: version guard installed=[{0}] setup=[{1}] compare={2}" -f $installedVer, $setupVer, $(if ($null -eq $cmp) { 'unparseable' } else { $cmp }))
        if ($cmp -eq 1) {
            Write-ProgressLine -Stage 'update' -Title 'Update' -State 'failed' -Message ('Cognita {0} is installed; this Setup is older ({1}).' -f $installedVer, $setupVer) -Fix 'Use a newer Setup, or cognita rollback to go back a release.'
            return (New-VerbResult 'failed' ([ordered]@{ reason = 'older-setup'; installed = $installedVer; setup = $setupVer }))
        }
    } else { Write-Log ("update: version guard skipped (installed=[{0}] setup=[{1}])" -f $installedVer, $setupVer) }
    $pw = $null
    try { $pw = Get-AdminPasswordFromOpts $Opts }
    catch {
        Write-ProgressLine -Stage 'password' -Title 'Admin password' -State 'failed' -Message $_.Exception.Message -Fix 'Run Setup again.'
        return (New-VerbResult 'failed' ([ordered]@{ reason = 'no-password' }))
    }
    # Design 22.9: --wsl-memory-reclaim (Setup's ticked box) -> the .wslconfig line, FIRST: before the
    # distro is started or touched, so it is in place the next time WSL starts. Failure is a warning only.
    $reclaimAsked = [bool](Get-Opt $Opts 'wsl-memory-reclaim' $false)
    Write-Log ("update: wslMemoryReclaim={0} recorded acceleration=[{1}]" -f $reclaimAsked, (Get-SettingsAcceleration $settings))
    if ($reclaimAsked) { [void](Set-WslReclaimSetting) }
    # Design 19.3 item 3: Cognita may have been stopped (`cognita stop` leaves the `stopped` flag and the
    # distro idle-stopped), and `cognita update` inside the distro needs the distro up. Same steps as
    # `install`: Start-Keepalive removes the stopped flag and runs the login task (registering it when it
    # is missing), then the distro must answer (bounded 300 s). Done after the password is read, so the
    # broker's ten minutes are not spent waiting on a slow start.
    Write-ProgressLine -Stage 'keepalive' -Title 'Keeping Cognita running' -State 'start'
    $kaStarted = Start-Keepalive
    Write-Log ("update: keepalive started={0}; waiting for the distro" -f $kaStarted)
    if (-not (Wait-DistroReady -Settings $settings -TimeoutSec 300 -Stage 'keepalive')) {
        Write-ProgressLine -Stage 'keepalive' -Title 'Keeping Cognita running' -State 'failed' -Message 'Cognita''s Linux did not finish starting within 5 minutes.' -Fix 'Run Setup again. Your installed Cognita was not changed.'
        return (New-VerbResult 'failed' ([ordered]@{ reason = 'distro-not-ready' }))
    }
    Write-ProgressLine -Stage 'keepalive' -Title 'Keeping Cognita running' -State 'done'
    $before = Get-LinuxStatusJson -Settings $settings
    $from = ''; if ($before) { $from = [string]$before.version }
    Write-ProgressLine -Stage 'update' -Title 'Updating Cognita' -State 'start'
    # Disk: Setup passes the requirement it computed from the new release's sizes.
    $needBytes = [int64](Get-Opt $Opts 'disk-bytes' 0)
    if ($needBytes -gt 0) {
        $free = Get-FreeSpaceBytes ([string]$settings.vhd_dir)
        Write-Log ("update: disk check free={0} need={1}" -f $free, $needBytes)
        if ($free -ge 0 -and $free -lt $needBytes) {
            $root = [System.IO.Path]::GetPathRoot((Get-NormalizedPath ([string]$settings.vhd_dir)))
            Write-ProgressLine -Stage 'update' -Title 'Updating Cognita' -State 'failed' -Message ('The update needs {0} GB free on {1}.' -f [Math]::Ceiling($needBytes / 1GB), $root) -Fix 'Free space and run Setup again. Your installed Cognita was not changed.'
            return (New-VerbResult 'failed' ([ordered]@{ reason = 'disk'; from = $from }))
        }
    }
    # 2. roots. First bring every fstab line up to the current shape (design 18.1 rule 5: an install
    # from before `shared` gets its lines rewritten by the next Setup run, and one from before design 22.14
    # gets the roots folder's shared bind line, then ONE distro restart); then
    # check Docker's view of every root and stop with the message when one is down.
    $syncChanged = 0
    foreach ($r in (Get-SettingsRoots $settings)) {
        $act = Write-RootFstabLine -Settings $settings -N ([int]$r.n) -WindowsPath ([string]$r.windows)
        if ($act -ne 'unchanged') { $syncChanged++ }
    }
    if ((Write-RootsBaseFstabLine -Settings $settings) -ne 'unchanged') { $syncChanged++ }
    Write-Log ("update: fstab lines rewritten={0}" -f $syncChanged)
    if ($syncChanged -gt 0) {
        Write-ProgressLine -Stage 'update' -Title 'Updating Cognita' -State 'progress' -Message 'Restarting Cognita''s Linux to reconnect your projects folders. This takes about a minute.'
        $rs = Restart-CognitaDistro -Settings $settings -Reason ('update: {0} fstab line(s) rewritten' -f $syncChanged) -Stage 'update'
        if (-not $rs.Ready) {
            Write-ProgressLine -Stage 'update' -Title 'Updating Cognita' -State 'failed' -Message 'Cognita''s Linux did not finish starting within 5 minutes.' -Fix 'Run Setup again. Your installed Cognita was not changed.'
            return (New-VerbResult 'failed' ([ordered]@{ reason = 'distro-not-ready'; from = $from }))
        }
    }
    $down = Assert-RootsAvailable -Settings $settings
    if ($down) {
        Write-ProgressLine -Stage 'update' -Title 'Updating Cognita' -State 'failed' -Message $down
        return (New-VerbResult 'failed' ([ordered]@{ reason = 'root-down'; from = $from }))
    }
    # 3. Docker Engine: a failure is a warning, not a stop
    Write-ProgressLine -Stage 'update.docker' -Title 'Updating Docker Engine' -State 'start'
    $beat = { Write-ProgressLine -Stage 'update.docker' -Title 'Updating Docker Engine' -State 'progress' -Message 'Still working.' }
    $dscript = "set -e`nexport DEBIAN_FRONTEND=noninteractive`napt-get update -qq </dev/null`napt-get install -y --only-upgrade docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin </dev/null`nexit 0`n"
    $dr = Invoke-Wsl -Settings $settings -User 'root' -Command @('sh', '-s') -StdinText $dscript -TimeoutSec 1800 -OnPoll $beat -PollIntervalMs 5000
    if ($dr.ExitCode -ne 0) {
        Write-ProgressLine -Stage 'update.docker' -Title 'Updating Docker Engine' -State 'warning' -Message ('The Docker Engine upgrade did not finish (exit {0}). Cognita will still be updated.' -f $dr.ExitCode)
    } else { Write-ProgressLine -Stage 'update.docker' -Title 'Updating Docker Engine' -State 'done' }
    # 3b. (design 22.4) the NVIDIA Container Toolkit, only when this install runs on the NVIDIA profile:
    # this is what moves an existing install's toolkit when a newer Setup pins a newer one (the WSL image
    # only reaches new installs). Same rule as install: a failure is a warning, never a stop.
    if ((Get-SettingsAcceleration $settings) -eq 'nvidia') {
        $tk = Invoke-NvidiaToolkitStep -Settings $settings -Caller 'update'
        Write-Log ("update: nvidia toolkit step ran={0} ok={1} detail=[{2}]" -f $tk.Ran, $tk.Ok, $tk.Detail)
    } else { Write-Log ("update: no nvidia toolkit step (recorded acceleration [{0}])" -f (Get-SettingsAcceleration $settings)) }
    # 4. source tree
    $tar = [string](Get-Opt $Opts 'src' '')
    $newVersion = [string](Get-Opt $Opts 'setup-version' '')
    if (-not $tar -or -not $newVersion -or -not (Test-Path -LiteralPath $tar)) {
        Write-ProgressLine -Stage 'update.tree' -Title 'Updating Cognita''s files' -State 'failed' -Message 'The Cognita source package is missing from Setup.' -Fix 'Download Setup again and run it.'
        return (New-VerbResult 'failed' ([ordered]@{ reason = 'no-source'; from = $from }))
    }
    if (-not (Install-CognitaTree -Settings $settings -Tarball $tar -Sha256 ([string](Get-Opt $Opts 'src-sha256' '')) -Version $newVersion -Stage 'update.tree')) {
        return (New-VerbResult 'failed' ([ordered]@{ reason = 'tree'; from = $from }))
    }
    # 5. the Linux CLI's update
    $lr = Invoke-CognitaLinux -Settings $settings -CliArgs @('update', '--no-pull', '--non-interactive', '--admin-password-stdin') -StdinText ($pw + "`n") -Stage 'cognita' -TimeoutSec 7200 -Title 'Updating Cognita'
    $pw = $null
    if ($lr.ExitCode -ne 0) {
        # Design 19.3 item 10b: the Linux side writes its own `failed` progress line, with its own message
        # and fix (which already says "Go back with: cognita rollback" when it switched releases); a second
        # helper line would replace it on Setup's page with a vaguer one. The helper writes its own only
        # when Linux wrote none (a crash, a timeout, a kill). And "To go back: cognita rollback" is true
        # advice only when the release was actually switched (the Linux `start` stage reached "done");
        # before that the old release is still running and there is nothing to roll back to.
        if (-not $lr.Failed) {
            $fix = 'Run Setup again. If it keeps failing, use Save diagnostics.'
            if ($lr.SwitchDone) { $fix = 'To go back: cognita rollback' }
            Write-ProgressLine -Stage 'update' -Title 'Updating Cognita' -State 'failed' -Message ('The update failed. ' + $lr.Summary) -Fix $fix
            Write-Log ("update: Linux wrote no failed line; the helper wrote its own (switchDone={0}, fix=[{1}])" -f $lr.SwitchDone, $fix)
        } else { Write-Log 'update: Linux wrote its own failed line; the helper writes none' }
        return (New-VerbResult 'failed' ([ordered]@{ reason = 'linux-update'; exit = $lr.ExitCode; from = $from }))
    }
    # Design 18.2: recorded only now, after `cognita update` exited 0 (a failure never clears or moves it).
    Set-SettingProp $settings 'linux_installed_version' $newVersion
    Save-Settings $settings
    Write-Log ("update: linux_installed_version recorded as {0}" -f $newVersion)
    # 6. keepalive re-registered (the launcher may have changed)
    if (-not (Register-Keepalive)) {
        Write-ProgressLine -Stage 'keepalive' -Title 'Keeping Cognita running' -State 'warning' -Message 'Could not re-register the sign-in task.' -Fix 'Run Setup again to retry.'
    }
    $after = Get-LinuxStatusJson -Settings $settings
    $to = ''; if ($after) { $to = [string]$after.version }
    # Design 22.4 + 22.12 item 1a: refreshed from status --json, never '' over a known value.
    $accelReported = Update-SettingsAcceleration -Settings $settings -Status $after -Caller 'update'
    $sv = [string](Get-Opt $Opts 'setup-version' ''); if ($sv) { Set-SettingProp $settings 'setup_version' $sv }
    $sr = [string](Get-Opt $Opts 'setup-revision' ''); if ($sr) { Set-SettingProp $settings 'setup_revision' ([int]$sr) }
    Save-Settings $settings
    Write-ProgressLine -Stage 'update' -Title 'Updating Cognita' -State 'done'
    $proof = Get-LinuxProofState -Settings $settings -Caller 'update'
    Set-SettingProp $settings 'linux_proof' $proof
    Save-Settings $settings
    Write-Log ("update: from={0} to={1} proof=[{2}] skipRequested={3} acceleration=[{4}]" -f $from, $to, $proof, $lr.SkipRequested, $accelReported)
    return (New-VerbResult 'ok' ([ordered]@{ from = $from; to = $to; acceleration = $accelReported; proof = $proof }))
}

# ---------------------------------------------------------------------------------------
# state, roots, add-folder (design 5.2, 5.5, 7.2)
# ---------------------------------------------------------------------------------------
function Get-DirTopLevelBytes {
    # Size of the files directly inside $Path (the virtual disk lives there). Never recurses.
    param([string]$Path)
    $sum = [int64]0
    try {
        if (Test-Path -LiteralPath $Path -PathType Container) {
            foreach ($f in (Get-ChildItem -LiteralPath $Path -File -Force -ErrorAction Stop)) { $sum += $f.Length }
        }
    } catch { Write-Log ("size read failed for {0}: {1}" -f $Path, $_.Exception.Message) }
    return $sum
}

function Invoke-StateVerb {
    param($Opts)
    $s = Read-Settings
    $own = Get-DistroOwnership -Settings $s -SkipMarker
    $running = $false
    if ($own.Exists) { $running = Test-DistroRunning -Settings $s }
    # Design 18.2 (revised): installed means EXACTLY "our distro is owned AND `cognita install` / `cognita
    # update` has exited 0 in it" (linux_installed_version is set). It no longer looks at settings.state,
    # so a keep-data uninstall (state=uninstalled, distro and version kept) still reports installed=1 and
    # Setup picks repair or update; a first install that failed part-way reports 0 and Setup picks "finish".
    $linuxVersion = ''
    $adminUser = ''
    if ($s) { $linuxVersion = [string]$s.linux_installed_version; $adminUser = [string]$s.admin_user }
    $installed = [bool]($own.Exists -and $own.Owned -and $linuxVersion)
    $roots = @(Get-SettingsRoots $s)
    $root1 = ''
    foreach ($r in $roots) { if ([int]$r.n -eq 1) { $root1 = [string]$r.windows } }
    # Keys are the contract Setup reads (lead's message, from Cognita.iss): installed, resume,
    # distro (present|absent), running, version (the installed Cognita version: the release Setup last
    # installed or updated to, recorded in settings.json because status must not start the distro),
    # root1, mcp_port, admin_port, workspace, vhd_dir, vhd_bytes. state and owned are extra. Design 18.2
    # adds linux_version (the Setup version that last got `cognita install`/`update` to exit 0, '' when
    # none) and admin_user; `version` stays the Setup version recorded, as before.
    # Design 19.11 R6 + R7: funnel_port is the Funnel's PUBLIC https port (443, 8443, ...), empty when none
    # is recorded, and only reported while Tailscale still serves it (a stale record is cleared first), so
    # Setup's lock on the MCP port matches reality. (The old MCP port the install guard refuses on is
    # reported there as funnel_target.)
    $funnelPort = ''
    if ($s -and $s.funnel -and (Clear-FunnelRecordIfNotServed -Settings $s -Caller 'state')) { $funnelPort = [string]$s.funnel.https_port }
    # Design 21.3: the self-test outcome the last install/update run read from the Linux side's install.env
    # (recorded in settings.json as linux_proof). `state` reads it from there and never runs a command in
    # the distro: it must stay cheap and must never be able to start a stopped distro (the test named
    # "the distro is never started" asserts exactly one wsl call). Empty when unknown.
    $proofState = ''
    if ($s -and $s.PSObject.Properties['linux_proof'] -and $s.linux_proof) { $proofState = [string]$s.linux_proof }
    $vals = [ordered]@{
        installed  = [int]$installed
        resume     = $(if ($s -and $s.resume) { [string]$s.resume } else { '' })
        distro     = $(if ($own.Exists) { 'present' } else { 'absent' })
        running    = [int]$running
        version    = $(if ($s) { [string]$s.setup_version } else { '' })
        root1      = $root1
        mcp_port   = $(if ($s) { $s.mcp_port } else { '' })
        admin_port = $(if ($s) { $s.admin_port } else { '' })
        workspace  = $(if ($s) { [string]$s.workspace } else { '' })
        vhd_dir    = $(if ($s) { [string]$s.vhd_dir } else { '' })
        vhd_bytes  = $(if ($s -and $s.vhd_dir) { (Get-DirTopLevelBytes ([string]$s.vhd_dir)) } else { 0 })
        state      = $(if ($s) { [string]$s.state } else { 'none' })
        owned      = [int][bool]$own.Owned
        linux_version = $linuxVersion
        admin_user = $adminUser
        funnel_port = $funnelPort
        proof      = $proofState
        # Design 22.4: settings' acceleration ('' when unknown: an install from before 15.1). Read from
        # settings only; nothing runs in the distro.
        acceleration = $(if ($s) { (Get-SettingsAcceleration $s) } else { '' })
    }
    Write-Log ("state: installed={0} state={1} distro={2} owned={3} running={4} linux_version=[{5}] admin_user=[{6}] funnel_port=[{7}] proof=[{8}] acceleration=[{9}]" -f $vals.installed, $vals.state, $vals.distro, $vals.owned, $vals.running, $vals.linux_version, $vals.admin_user, $vals.funnel_port, $vals.proof, $vals.acceleration)
    return (New-VerbResult 'ok' $vals)
}

function Invoke-RootsVerb {
    param($Opts, $Positional)
    $path = [string](Get-Opt $Opts 'validate' '')
    if (-not $path) { return (New-VerbResult 'failed' ([ordered]@{ error = 'usage: roots --validate PATH' })) }
    $s = Read-Settings
    $v = Test-RootPath -Path $path -Settings $s -ExtraOwnFolder ([string](Get-Opt $Opts 'data-dir' ''))
    if ($v.Ok) { return (New-VerbResult 'ok' ([ordered]@{ ok = 1; reason = ''; path = $v.Path })) }
    # Design 18.5: an invalid folder is a normal ANSWER to the question asked, so the verb succeeded
    # (result=ok, exit 0) with ok=0 and the reason. `failed` is kept for an internal error or bad usage.
    Write-Log ("roots --validate: folder rejected, answer ok=0 reason=[{0}]" -f $v.Reason)
    $presentationId = [string]$v.ReasonId
    if (-not $presentationId) { $presentationId = 'setup.generic.failure' }
    $reasonDisplay = Get-LocalizedFolderReason -PresentationId $presentationId -Reason ([string]$v.Reason)
    return (New-VerbResult 'ok' ([ordered]@{ ok = 0; reason = $v.Reason; presentation_id = $presentationId; reason_display = $reasonDisplay }))
}

$script:StoppedMessage = 'Cognita is stopped. Start it with: cognita start'

function Invoke-AddFolderVerb {
    param($Opts, $Positional)
    $s = Read-Settings
    if (-not $s -or $s.state -ne 'installed') {
        Write-InfoLine 'Cognita is not installed on this PC. Run Cognita Setup.'
        return (New-VerbResult 'failed' ([ordered]@{ reason = 'not-installed' }))
    }
    $path = [string](Get-Opt $Opts 'path' '')
    if (-not $path -and @($Positional).Count -gt 0) { $path = [string]@($Positional)[0] }
    if (-not $path) {
        $path = Show-FolderPicker
        if (-not $path) {
            Write-InfoLine 'No folder was chosen.'
            return (New-VerbResult 'failed' ([ordered]@{ reason = 'cancelled' }))
        }
    }
    if (-not (Test-DistroRunning -Settings $s)) {
        Write-InfoLine $script:StoppedMessage
        return (New-VerbResult 'failed' ([ordered]@{ reason = 'stopped' }))
    }
    $down = Assert-RootsAvailable -Settings $s
    if ($down) {
        Write-ProgressLine -Stage 'add_folder' -Title 'Add a projects folder' -State 'failed' -Message $down
        return (New-VerbResult 'failed' ([ordered]@{ reason = 'root-down' }))
    }
    $r = Add-CognitaRoot -Settings $s -WindowsPath $path -TellLinux -Stage 'add_folder'
    if (-not $r.Ok) {
        Write-ProgressLine -Stage 'add_folder' -Title 'Add a projects folder' -State 'failed' -Message $r.Reason
        return (New-VerbResult 'failed' ([ordered]@{ reason = 'add-failed'; detail = $r.Reason }))
    }
    Write-InfoLine ('Added {0} as projects folder {1}.' -f $r.Path, $r.N)
    return (New-VerbResult 'ok' ([ordered]@{ n = $r.N; path = $r.Path }))
}

# ---------------------------------------------------------------------------------------
# start, stop, restart, status (design 8)
# ---------------------------------------------------------------------------------------
function Get-InstalledSettingsOrExplain {
    $s = Read-Settings
    if (-not $s -or $s.state -ne 'installed') {
        Write-InfoLine 'Cognita is not installed on this PC. Run Cognita Setup.'
        return $null
    }
    return $s
}

function Wait-CognitaHealthy {
    param($Settings, [int]$TimeoutSec = 300)
    $cond = { $st = Get-LinuxStatusJson -Settings $Settings; return [bool]($st -and $st.running) }.GetNewClosure()
    return (Wait-Until -Condition $cond -TimeoutSec $TimeoutSec -IntervalMs 5000 -Description 'Cognita healthy (status --json running)' -Stage 'start' -Title 'Waiting for Cognita to answer')
}

function Invoke-StartVerb {
    param($Opts)
    $s = Get-InstalledSettingsOrExplain
    if (-not $s) { return (New-VerbResult 'failed' ([ordered]@{ reason = 'not-installed' })) }
    Write-InfoLine 'Starting Cognita...'
    [void](Start-Keepalive)
    $cond = { Test-DistroRunning -Settings $s }.GetNewClosure()
    if (-not (Wait-Until -Condition $cond -TimeoutSec 120 -IntervalMs 2000 -Description 'distro running after keepalive start' -Stage 'start' -Title 'Starting Cognita''s Linux')) {
        Write-ProgressLine -Stage 'start' -Title 'Starting Cognita' -State 'failed' -Message 'Cognita''s Linux did not start within 2 minutes.' -Fix 'Run: cognita diagnostics'
        return (New-VerbResult 'failed' ([ordered]@{ reason = 'distro-not-running' }))
    }
    # Judge the roots only once systemd has finished booting: fstab is applied during that boot, and
    # "running" in `wsl --list` only says the virtual machine exists.
    if (-not (Wait-DistroReady -Settings $s -TimeoutSec 300 -Stage 'start')) {
        Write-ProgressLine -Stage 'start' -Title 'Starting Cognita' -State 'failed' -Message 'Cognita''s Linux did not finish starting within 5 minutes.' -Fix 'Run: cognita diagnostics'
        return (New-VerbResult 'failed' ([ordered]@{ reason = 'distro-not-ready' }))
    }
    # Design 18.1: a root that is unmounted in Docker's view means the distro is restarted (fstab is applied
    # when it starts); the helper never mounts. A distro that has just been restarted starts Cognita
    # itself, so `cognita restart` is NOT run after it (the old remount-then-restart step is gone).
    $rr = Restore-RootMounts -Settings $s -Stage 'start'
    if ($rr.Restarted -and -not $rr.Ready) {
        Write-ProgressLine -Stage 'start' -Title 'Starting Cognita' -State 'failed' -Message 'Cognita''s Linux did not finish restarting within 5 minutes.' -Fix 'Run: cognita diagnostics'
        return (New-VerbResult 'failed' ([ordered]@{ reason = 'distro-not-ready' }))
    }
    $still = @($rr.Still)
    foreach ($d in $still) { Write-InfoLine $d.Message }
    if ($rr.Restarted) { Write-Log ("start: the distro was restarted to apply fstab ({0} root(s) still down); a fresh start already starts Cognita, so cognita restart is not run" -f $still.Count) }
    else { Write-Log 'start: no root was unmounted, so the distro was not restarted' }
    if (-not (Wait-CognitaHealthy -Settings $s -TimeoutSec 300)) {
        Write-ProgressLine -Stage 'start' -Title 'Starting Cognita' -State 'failed' -Message 'Cognita did not answer within 5 minutes.' -Fix 'Run: cognita logs app'
        return (New-VerbResult 'failed' ([ordered]@{ reason = 'not-healthy' }))
    }
    Write-InfoLine 'Cognita is running.'
    return (New-VerbResult 'ok' ([ordered]@{ running = 1; roots_down = $still.Count }))
}

function Invoke-StopVerb {
    param($Opts)
    $s = Get-InstalledSettingsOrExplain
    if (-not $s) { return (New-VerbResult 'failed' ([ordered]@{ reason = 'not-installed' })) }
    $flag = Get-StoppedFlagPath
    [System.IO.File]::WriteAllText($flag, ((Get-ClockNow).ToString('yyyy-MM-dd HH:mm:ss') + "`n"))
    Write-Log 'stop: stopped flag created (the keepalive will not relaunch)'
    if (Test-DistroRunning -Settings $s) {
        $r = Invoke-CognitaLinux -Settings $s -CliArgs @('stop') -NoProgressFile -TimeoutSec 300
        Write-Log ("stop: cognita stop exit {0}" -f $r.ExitCode)
        $t = Invoke-External -FilePath (Get-WslExe) -Arguments @('--terminate', (Get-DistroName $s)) -TimeoutSec 60
        Write-Log ("stop: wsl --terminate exit {0}" -f $t.ExitCode)
    } else { Write-Log 'stop: distro was not running' }
    Write-InfoLine 'Cognita is stopped. It will not start again until you run: cognita start'
    return (New-VerbResult 'ok' ([ordered]@{ running = 0 }))
}

function Invoke-RestartVerb {
    param($Opts)
    $s = Get-InstalledSettingsOrExplain
    if (-not $s) { return (New-VerbResult 'failed' ([ordered]@{ reason = 'not-installed' })) }
    if (-not (Test-DistroRunning -Settings $s)) {
        Write-Log 'restart: distro is not running; doing a start instead'
        return (Invoke-StartVerb -Opts $Opts)
    }
    $rr = Restore-RootMounts -Settings $s -Stage 'restart'
    if ($rr.Restarted -and -not $rr.Ready) {
        Write-ProgressLine -Stage 'restart' -Title 'Restarting Cognita' -State 'failed' -Message 'Cognita''s Linux did not finish restarting within 5 minutes.' -Fix 'Run: cognita diagnostics'
        return (New-VerbResult 'failed' ([ordered]@{ reason = 'distro-not-ready' }))
    }
    $still = @($rr.Still)
    foreach ($d in $still) { Write-InfoLine $d.Message }
    if ($rr.Restarted) {
        # Design 18.1: the distro itself was restarted so fstab could be applied, and Cognita came up with
        # it. Running `cognita restart` on top would restart Cognita a second time for nothing.
        Write-Log ("restart: the distro was restarted to apply fstab ({0} root(s) still down); cognita restart is not run because a fresh start already starts Cognita" -f $still.Count)
        if (-not (Wait-CognitaHealthy -Settings $s -TimeoutSec 300)) {
            Write-ProgressLine -Stage 'restart' -Title 'Restarting Cognita' -State 'failed' -Message 'Cognita did not answer within 5 minutes.' -Fix 'Run: cognita logs app'
            return (New-VerbResult 'failed' ([ordered]@{ reason = 'not-healthy' }))
        }
        Write-InfoLine 'Cognita restarted.'
        return (New-VerbResult 'ok' ([ordered]@{ running = 1; roots_down = $still.Count }))
    }
    Write-Log 'restart: no root was unmounted; running cognita restart'
    $r = Invoke-CognitaLinux -Settings $s -CliArgs @('restart') -NoProgressFile -TimeoutSec 600
    if ($r.ExitCode -ne 0) {
        Write-ProgressLine -Stage 'restart' -Title 'Restarting Cognita' -State 'failed' -Message ('cognita restart failed (exit {0}). {1}' -f $r.ExitCode, $r.Summary)
        return (New-VerbResult 'failed' ([ordered]@{ reason = 'restart-failed'; exit = $r.ExitCode }))
    }
    Write-InfoLine 'Cognita restarted.'
    return (New-VerbResult 'ok' ([ordered]@{ running = 1; roots_down = $still.Count }))
}

function Invoke-StatusVerb {
    # Never starts a stopped distro (W13).
    param($Opts)
    $s = Read-Settings
    if (-not $s -or $s.state -ne 'installed') {
        Write-InfoLine 'Cognita is not installed on this PC. Run Cognita Setup.'
        return (New-VerbResult 'ok' ([ordered]@{ installed = 0; running = 0 }))
    }
    if (-not (Test-DistroRunning -Settings $s)) {
        Write-InfoLine $script:StoppedMessage
        return (New-VerbResult 'ok' ([ordered]@{ installed = 1; running = 0 }))
    }
    $st = Get-LinuxStatusJson -Settings $s
    $version = ''; $running = 0; $public = ''
    if ($st) {
        $version = [string]$st.version
        if ($st.running) { $running = 1 }
        if ($st.public_url) { $public = [string]$st.public_url }
        if ($running) { Write-InfoLine ('Cognita {0} is running.' -f $version) } else { Write-InfoLine ('Cognita {0} is installed but not answering yet.' -f $version) }
        if ($st.admin_url) { Write-InfoLine ('  Admin:        {0}' -f $st.admin_url) }
        if ($st.mcp_url) { Write-InfoLine ('  MCP:          {0}' -f $st.mcp_url) }
        if ($public) { Write-InfoLine ('  Public URL:   {0}' -f $public) }
        if ($st.workspace) { Write-InfoLine ('  Workspace:    {0}' -f $st.workspace) }
        # Design 22.4: said only when status --json said it (nvidia -> NVIDIA GPU, amd -> AMD GPU, else CPU).
        if ($st.PSObject.Properties['acceleration'] -and [string]$st.acceleration) {
            $accLabel = 'CPU'
            switch (([string]$st.acceleration).Trim().ToLowerInvariant()) { 'nvidia' { $accLabel = 'NVIDIA GPU' } 'amd' { $accLabel = 'AMD GPU' } }
            Write-InfoLine ('  Acceleration: {0}' -f $accLabel)
            Write-Log ("status: acceleration reported [{0}] shown as [{1}]" -f $st.acceleration, $accLabel)
        }
        # Design 21.1: said only when the self-tests were skipped (the Linux side's status --json `proof`).
        if ($st.PSObject.Properties['proof'] -and [string]$st.proof -eq 'skipped') { Write-InfoLine '  Self-tests:   skipped at install (run Cognita Setup again to run them)' }
    } else {
        Write-InfoLine 'Cognita''s Linux is running, but its status could not be read.'
    }
    $down = 0
    foreach ($r in (Get-RootStates -Settings $s)) {
        if ($r.Mounted) { Write-InfoLine ('  Projects folder: {0} (connected)' -f $r.Windows) }
        else { $down++; Write-InfoLine ('  ' + (Get-RootDownMessage $r.Windows)) }
    }
    $task = Get-CognitaTaskInfo
    if ($task.Exists) { Write-InfoLine ('  Starts when you sign in to Windows: yes (task state {0})' -f $task.State) }
    else { Write-InfoLine '  Starts when you sign in to Windows: NO (the sign-in task is missing; run Cognita Setup again to repair)' }
    return (New-VerbResult 'ok' ([ordered]@{ installed = 1; running = $running; version = $version; public_url = $public; roots_down = $down }))
}

# ---------------------------------------------------------------------------------------
# Forwarded verbs and the cognita command (design 8)
# ---------------------------------------------------------------------------------------
function Test-ForwardedPortFlags {
    param([string[]]$Tokens)
    foreach ($t in @($Tokens)) { if ($t -match '^--(mcp-port|admin-port)(=|$)') { return $true } }
    return $false
}

function Invoke-ForwardVerb {
    # Only when the distro is running: a forwarded command never boots the distro by accident.
    param([string]$LinuxVerb, [string[]]$Tokens)
    $s = Read-Settings
    if (-not $s -or $s.state -ne 'installed') {
        Write-InfoLine 'Cognita is not installed on this PC. Run Cognita Setup.'
        return [pscustomobject]@{ Status = 'failed'; Values = ([ordered]@{ reason = 'not-installed' }); ExitCode = 1 }
    }
    if ($LinuxVerb -eq 'install' -and (Test-ForwardedPortFlags $Tokens)) {
        Write-InfoLine 'Change ports by running Setup again and choosing Advanced.'
        Write-Log 'forward: install with port flags refused'
        return [pscustomobject]@{ Status = 'failed'; Values = ([ordered]@{ reason = 'ports-via-setup' }); ExitCode = 1 }
    }
    if (-not (Test-DistroRunning -Settings $s)) {
        Write-InfoLine $script:StoppedMessage
        Write-Log ("forward: {0} not run because the distro is stopped" -f $LinuxVerb)
        return [pscustomobject]@{ Status = 'failed'; Values = ([ordered]@{ reason = 'stopped' }); ExitCode = 1 }
    }
    if ($LinuxVerb -eq 'install') {
        $down = Assert-RootsAvailable -Settings $s
        if ($down) { Write-InfoLine $down; return [pscustomobject]@{ Status = 'failed'; Values = ([ordered]@{ reason = 'root-down' }); ExitCode = 1 } }
    }
    $wslArgs = Get-WslExecArgs -Distro (Get-DistroName $s) -User (Get-LinuxUser $s) -Command (@($script:LinuxCliPath, $LinuxVerb) + @($Tokens))
    $code = Invoke-Passthrough -FilePath (Get-WslExe) -Arguments $wslArgs
    if (($LinuxVerb -eq 'install' -or $LinuxVerb -eq 'rollback') -and $code -eq 0) {
        # Ports may have moved on the Linux side (install): refresh, and say so when they differ. The
        # acceleration profile joins them (design 22.4), and a rollback that exits 0 refreshes it too
        # (design 22.12 item 16: a rollback can move to a release of another profile). The forwarded path
        # does not run Install-NvidiaToolkit; the Linux side's own note says what is missing.
        $st = Get-LinuxStatusJson -Settings $s
        if ($st) {
            $changed = $false
            if ($LinuxVerb -eq 'install') {
                $mp = Get-PortFromUrl ([string]$st.mcp_url); $ap = Get-PortFromUrl ([string]$st.admin_url)
                if ($mp -gt 0 -and $mp -ne [int]$s.mcp_port) { Write-InfoLine ('The MCP port is now {0} (it was {1}).' -f $mp, $s.mcp_port); Set-SettingProp $s 'mcp_port' $mp; $changed = $true }
                if ($ap -gt 0 -and $ap -ne [int]$s.admin_port) { Write-InfoLine ('The Admin port is now {0} (it was {1}).' -f $ap, $s.admin_port); Set-SettingProp $s 'admin_port' $ap; $changed = $true }
                if ($st.workspace -and [string]$st.workspace -ne [string]$s.workspace) { Set-SettingProp $s 'workspace' ([string]$st.workspace); $changed = $true }
            }
            $accBefore = Get-SettingsAcceleration $s
            $hadKey = [bool]$s.PSObject.Properties['acceleration']
            [void](Update-SettingsAcceleration -Settings $s -Status $st -Caller ('forward ' + $LinuxVerb))
            if ((Get-SettingsAcceleration $s) -ne $accBefore -or -not $hadKey) { $changed = $true }
            if ($changed) { Save-Settings $s }
        } else { Write-Log ("forward: {0} exited 0 but status --json could not be read; settings left as they are" -f $LinuxVerb) }
    }
    $status = 'ok'; if ($code -ne 0) { $status = 'failed' }
    return [pscustomobject]@{ Status = $status; Values = ([ordered]@{ exit = $code }); ExitCode = $code }
}

function Get-CliHelpText {
    return @(
        'cognita - the Cognita command for Windows',
        '',
        '  cognita status                 Show whether Cognita is running (never starts it)',
        '  cognita start | stop | restart',
        '  cognita logs [app|workspace|install] [-f]',
        '  cognita add-folder [PATH]      Give Cognita another projects folder',
        '  cognita remote-access          Publish the MCP address with Tailscale Funnel',
        '  cognita password               Change the Admin password',
        '  cognita reset index|workspaces|all',
        '  cognita rollback               Go back to the previous version after a failed update',
        '  cognita diagnostics            Save a zip for support on your Desktop',
        '  cognita update                 How to update',
        '  cognita uninstall              How to uninstall',
        '',
        'Commands not listed here are passed to the Cognita installer inside WSL, and only',
        'while Cognita is running.'
    )
}

function Invoke-CliVerb {
    param([string[]]$Tokens)
    $Tokens = @($Tokens)
    if ($Tokens.Count -eq 0 -or $Tokens[0] -in @('help', '--help', '-h', '/?')) {
        foreach ($l in (Get-CliHelpText)) { Write-Out $l }
        return [pscustomobject]@{ Status = 'ok'; Values = ([ordered]@{}); ExitCode = 0 }
    }
    $cmd = $Tokens[0].ToLowerInvariant()
    $rest = @(); if ($Tokens.Count -gt 1) { $rest = $Tokens[1..($Tokens.Count - 1)] }
    Write-Log ("cli: command={0} args={1}" -f $cmd, ($rest -join ' '))
    $parsed = ConvertFrom-HelperArgs $rest
    $r = $null
    switch ($cmd) {
        'status' { $r = Invoke-StatusVerb -Opts $parsed.Opts }
        'start' { $r = Invoke-StartVerb -Opts $parsed.Opts }
        'stop' { $r = Invoke-StopVerb -Opts $parsed.Opts }
        'restart' { $r = Invoke-RestartVerb -Opts $parsed.Opts }
        'add-folder' { $r = Invoke-AddFolderVerb -Opts $parsed.Opts -Positional $parsed.Positional }
        'remote-access' { $r = Invoke-RemoteAccessVerb -Opts $parsed.Opts }
        'diagnostics' { $r = Invoke-DiagnosticsVerb -Opts $parsed.Opts }
        'update' {
            Write-InfoLine ('Download the new Cognita-Setup.exe from {0} and run it.' -f $script:ReleasesUrl)
            $r = New-VerbResult 'ok'
        }
        'uninstall' {
            Write-InfoLine 'Uninstall Cognita from Settings > Apps > Installed apps.'
            $r = New-VerbResult 'ok'
        }
        default { return (Invoke-ForwardVerb -LinuxVerb $cmd -Tokens $rest) }
    }
    $code = Get-ExitCodeForStatus $r.Status
    if ($code -eq 3010) { $code = 0 }
    return [pscustomobject]@{ Status = $r.Status; Values = $r.Values; ExitCode = $code }
}

# ---------------------------------------------------------------------------------------
# remote-access: Tailscale Funnel (design 9)
# ---------------------------------------------------------------------------------------
function Find-TailscaleExe {
    $c = Join-Path (Join-Path $env:ProgramFiles 'Tailscale') 'tailscale.exe'
    if (Test-Path -LiteralPath $c) { return $c }
    try { $g = Get-Command tailscale.exe -ErrorAction Stop; return [string]$g.Source } catch { return $null }
}

function Invoke-Download {
    # Bounded, with a heartbeat. Returns $true when the file arrived.
    param([string]$Url, [string]$Dest, [int]$TimeoutSec = 900, [string]$Stage = 'remote')
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    $wc = New-Object System.Net.WebClient
    try {
        $t = $wc.DownloadFileTaskAsync($Url, $Dest)
        $cond = { $t.IsCompleted }.GetNewClosure()
        if (-not (Wait-Until -Condition $cond -TimeoutSec $TimeoutSec -IntervalMs 500 -Description ('download ' + $Url) -Stage $Stage -Title 'Downloading Tailscale')) {
            $wc.CancelAsync(); return $false
        }
        if ($t.IsFaulted) { Write-Log ("download failed: {0}" -f $t.Exception.InnerException.Message); return $false }
        return $true
    } finally { $wc.Dispose() }
}

function Get-FileSignatureInfo {
    param([string]$Path)
    $s = Get-AuthenticodeSignature -FilePath $Path
    $subj = ''
    if ($s.SignerCertificate) { $subj = [string]$s.SignerCertificate.Subject }
    return [pscustomobject]@{ Status = [string]$s.Status; Subject = $subj }
}

function Install-TailscaleMsi {
    # Downloads the official installer, checks that its Authenticode signer is Tailscale Inc., runs it.
    # msiexec asks for admin itself; that prompt is Tailscale's installer, not ours. Unproven from a
    # non-elevated session (spike S5): /passive keeps the basic UI so Windows can show that prompt.
    $dest = Join-Path ([System.IO.Path]::GetTempPath()) ('tailscale-setup-{0}.msi' -f [guid]::NewGuid().ToString('N'))
    Write-ProgressLine -Stage 'remote' -Title 'Downloading Tailscale' -State 'start'
    if (-not (Invoke-Download -Url $script:TailscaleMsiUrl -Dest $dest)) {
        Write-ProgressLine -Stage 'remote' -Title 'Downloading Tailscale' -State 'failed' -Message 'The Tailscale installer could not be downloaded.' -Fix 'Check your internet connection and try again, or install Tailscale yourself from tailscale.com and run this again.'
        return $false
    }
    $sig = Get-FileSignatureInfo -Path $dest
    Write-Log ("tailscale msi signature: status={0} subject={1}" -f $sig.Status, $sig.Subject)
    if ($sig.Status -ne 'Valid' -or $sig.Subject -notmatch 'O=Tailscale Inc\.') {
        try { Remove-Item -LiteralPath $dest -Force } catch { Write-Log ("could not delete the rejected installer: {0}" -f $_.Exception.Message) }
        Write-ProgressLine -Stage 'remote' -Title 'Downloading Tailscale' -State 'failed' -Message 'The downloaded file is not signed by Tailscale Inc., so it was not run.' -Fix 'Install Tailscale yourself from tailscale.com and run this again.' -MessageId 'setup.tailscale.signature_refused'
        return $false
    }
    Write-ProgressLine -Stage 'remote' -Title 'Downloading Tailscale' -State 'done'
    Write-ProgressLine -Stage 'remote' -Title 'Installing Tailscale' -State 'start'
    $beat = { Write-ProgressLine -Stage 'remote' -Title 'Installing Tailscale' -State 'progress' -Message 'Still working. Windows may be asking for your permission.' }
    $r = Invoke-External -FilePath (Join-Path $env:SystemRoot 'System32\msiexec.exe') -Arguments @('/i', $dest, '/passive', '/norestart') -TimeoutSec 900 -OnPoll $beat -PollIntervalMs 5000
    try { Remove-Item -LiteralPath $dest -Force } catch { Write-Log ("could not delete the installer: {0}" -f $_.Exception.Message) }
    if ($r.ExitCode -ne 0 -and $r.ExitCode -ne 3010) {
        Write-ProgressLine -Stage 'remote' -Title 'Installing Tailscale' -State 'failed' -Message ('The Tailscale installer failed (exit {0}).' -f $r.ExitCode) -Fix 'Install Tailscale yourself from tailscale.com and run this again.'
        return $false
    }
    $cond = { [bool](Find-TailscaleExe) }
    if (-not (Wait-Until -Condition $cond -TimeoutSec 60 -IntervalMs 1000 -Description 'tailscale.exe present after install')) {
        Write-ProgressLine -Stage 'remote' -Title 'Installing Tailscale' -State 'failed' -Message 'Tailscale was installed but its command was not found.' -Fix 'Sign out and in again, or restart Windows, then run this again.'
        return $false
    }
    Write-ProgressLine -Stage 'remote' -Title 'Installing Tailscale' -State 'done'
    return $true
}

function Invoke-Tailscale {
    param([string]$Exe, [string[]]$TsArgs, [int]$TimeoutSec = 60, [scriptblock]$OnPoll = $null, [scriptblock]$OnErrorLine = $null, [scriptblock]$OnOutputLine = $null, [int]$PollIntervalMs = 1000, [switch]$NoLogOutput)
    return (Invoke-External -FilePath $Exe -Arguments $TsArgs -TimeoutSec $TimeoutSec -OnPoll $OnPoll -OnErrorLine $OnErrorLine -OnOutputLine $OnOutputLine -PollIntervalMs $PollIntervalMs -NoLogOutput:$NoLogOutput)
}

function Get-TailscaleStatusObject {
    # Design 18.5 (review 9): `tailscale status --json` lists every device on the tailnet, with user names
    # and addresses that are not ours to write into a log the user will attach to a support request. The
    # call runs with -NoLogOutput, and only BackendState and Self.DNSName are logged here.
    param([string]$Exe)
    $r = Invoke-Tailscale -Exe $Exe -TsArgs @('status', '--json') -TimeoutSec 30 -NoLogOutput
    if ($r.ExitCode -ne 0 -and -not $r.Stdout) { Write-Log ("tailscale status: exit {0} and no output" -f $r.ExitCode); return $null }
    $t = $r.Stdout
    $a = $t.IndexOf('{'); $b = $t.LastIndexOf('}')
    if ($a -lt 0 -or $b -le $a) { Write-Log 'tailscale status: no JSON object in the output'; return $null }
    try {
        $o = ($t.Substring($a, $b - $a + 1) | ConvertFrom-Json)
        $dns = ''
        if ($o.PSObject.Properties['Self'] -and $o.Self) { $dns = [string]$o.Self.DNSName }
        Write-Log ("tailscale status: BackendState={0} Self.DNSName=[{1}]" -f $o.BackendState, $dns)
        return $o
    } catch { Write-Log ("tailscale status parse failed: {0}" -f $_.Exception.Message); return $null }
}

function Get-FunnelServing {
    # port -> array of target strings, from the parsed `tailscale funnel status --json`.
    # Anything in TCP or Web counts as "in use" (a TCP forward has no Web entry).
    param($Json)
    $h = @{}
    if ($null -eq $Json) { return $h }
    if ($Json.PSObject.Properties['TCP'] -and $Json.TCP) {
        foreach ($p in $Json.TCP.PSObject.Properties) { $port = [int]$p.Name; if (-not $h.ContainsKey($port)) { $h[$port] = @() } }
    }
    if ($Json.PSObject.Properties['Web'] -and $Json.Web) {
        foreach ($w in $Json.Web.PSObject.Properties) {
            $port = 0
            if ($w.Name -match ':(\d+)$') { $port = [int]$Matches[1] }
            if ($port -eq 0) { continue }
            if (-not $h.ContainsKey($port)) { $h[$port] = @() }
            $handlers = $w.Value.Handlers
            if ($handlers) {
                foreach ($hd in $handlers.PSObject.Properties) {
                    $target = 'other'
                    if ($hd.Value.PSObject.Properties['Proxy'] -and $hd.Value.Proxy) { $target = [string]$hd.Value.Proxy }
                    $h[$port] += $target
                }
            }
        }
    }
    return $h
}

function Test-FunnelTargetIsMcp {
    param([string]$Target, [int]$McpPort)
    return ($Target -match ('^https?://(127\.0\.0\.1|localhost|\[::1\]):{0}/?$' -f $McpPort))
}

function Get-FunnelPlan {
    <#
    Never clobber an existing Funnel on 443 (design 9 step 4). Returns
    @{ Port; Action = 'set'|'reuse'|'choose'|'none'; Free = ports free among 8443, 10000 }:
      set    443 is unused: use it
      reuse  443 already serves localhost:<mcp-port>: nothing to set (and nothing to record)
      choose 443 serves something else: the caller offers Free (8443, 10000) or skips
      none   443 is busy and nothing else is free
    -Preferred picks a specific port (used after the user chose one).
    #>
    param($Serving, [int]$McpPort, [int]$Preferred = 0)
    $eval = {
        param($port)
        if (-not $Serving.ContainsKey($port)) { return 'free' }
        $targets = @($Serving[$port])
        if ($targets.Count -gt 0 -and @($targets | Where-Object { -not (Test-FunnelTargetIsMcp $_ $McpPort) }).Count -eq 0) { return 'ours' }
        return 'busy'
    }
    $free = @(8443, 10000 | Where-Object { (& $eval $_) -eq 'free' })
    if ($Preferred -gt 0) {
        $e = & $eval $Preferred
        if ($e -eq 'free') { return @{ Port = $Preferred; Action = 'set'; Free = $free } }
        if ($e -eq 'ours') { return @{ Port = $Preferred; Action = 'reuse'; Free = $free } }
        return @{ Port = 0; Action = 'none'; Free = $free }
    }
    $e443 = & $eval 443
    if ($e443 -eq 'free') { return @{ Port = 443; Action = 'set'; Free = $free } }
    if ($e443 -eq 'ours') { return @{ Port = 443; Action = 'reuse'; Free = $free } }
    if ($free.Count -gt 0) { return @{ Port = 0; Action = 'choose'; Free = $free } }
    return @{ Port = 0; Action = 'none'; Free = $free }
}

function Get-FunnelServingNow {
    param([string]$Exe)
    $r = Invoke-Tailscale -Exe $Exe -TsArgs @('funnel', 'status', '--json') -TimeoutSec 30
    $t = ($r.Stdout).Trim()
    if (-not $t.StartsWith('{')) { Write-Log 'funnel status: no JSON (nothing served)'; return @{} }
    try { return (Get-FunnelServing ($t | ConvertFrom-Json)) } catch { Write-Log ("funnel status parse failed: {0}" -f $_.Exception.Message); return @{} }
}

function Test-FunnelRecordServed {
    # Does Tailscale still serve the RECORDED Funnel: the recorded https port forwarding exactly to
    # localhost:<recorded target>? The same test the uninstall uses before it turns a Funnel off, and the
    # same status parse as remote-access (Get-FunnelServing). Returns @{ Served; Why } with Served $true,
    # $false or $null (UNKNOWN). Only two answers mean "not served": tailscale.exe is absent, or the status
    # read succeeded (exit 0, JSON) and the recorded port is not in it. A stopped service, a timeout, a
    # start error or output that is not JSON is UNKNOWN: the Funnel config lives inside Tailscale and comes
    # back with the service, so clearing our record then would leave an old port published that uninstall
    # no longer knows to turn off (review of 19.11, 2026-09-29).
    param($Funnel)
    $port = [int]$Funnel.https_port
    $target = [int]$Funnel.target
    $ts = Find-TailscaleExe
    if (-not $ts) { return @{ Served = $false; Why = 'tailscale.exe not found' } }
    try {
        $r = Invoke-Tailscale -Exe $ts -TsArgs @('funnel', 'status', '--json') -TimeoutSec 30
    } catch {
        return @{ Served = $null; Why = ('tailscale could not be started: {0}' -f $_.Exception.Message) }
    }
    $t = ([string]$r.Stdout).Trim()
    if ($r.TimedOut -or $r.ExitCode -ne 0 -or -not $t.StartsWith('{')) {
        return @{ Served = $null; Why = ('tailscale did not answer (exit {0}, timed out {1}, json {2})' -f $r.ExitCode, [bool]$r.TimedOut, $t.StartsWith('{')) }
    }
    try { $serving = Get-FunnelServing ($t | ConvertFrom-Json) } catch {
        return @{ Served = $null; Why = ('funnel status parse failed: {0}' -f $_.Exception.Message) }
    }
    $exact = ($serving.ContainsKey($port) -and @($serving[$port]).Count -gt 0 -and @(@($serving[$port]) | Where-Object { -not (Test-FunnelTargetIsMcp $_ $target) }).Count -eq 0)
    if ($exact) { return @{ Served = $true; Why = 'still served' } }
    return @{ Served = $false; Why = ('tailscale does not serve https port {0} forwarding to localhost:{1}' -f $port, $target) }
}

function Clear-FunnelRecordIfNotServed {
    # Design 19.11 R7. When settings record a Funnel that Tailscale no longer serves (turned off by hand,
    # Tailscale removed), the record is stale: clear settings.funnel, save, log why. When Tailscale cannot
    # be asked (UNKNOWN) the record is kept, so the port lock and the uninstall's Funnel removal stay.
    # Returns $true when a Funnel is still recorded after the call.
    param($Settings, [string]$Caller)
    if (-not ($Settings -and $Settings.funnel)) { return $false }
    $t = Test-FunnelRecordServed -Funnel $Settings.funnel
    Write-Log ("{0}: recorded funnel https_port={1} target={2} served={3} ({4})" -f $Caller, $Settings.funnel.https_port, $Settings.funnel.target, $t.Served, $t.Why)
    if ($null -eq $t.Served) { Write-Log ("{0}: could not ask Tailscale; funnel record kept" -f $Caller); return $true }
    if ($t.Served) { return $true }
    Set-SettingProp $Settings 'funnel' $null
    Save-Settings $Settings
    Write-Log ("{0}: recorded funnel cleared from settings: {1}" -f $Caller, $t.Why)
    return $false
}

function Get-PublicUrlFromDnsName {
    param([string]$DnsName, [int]$Port)
    $h = $DnsName.TrimEnd('.')
    if ($Port -eq 443) { return ('https://' + $h) }
    return ('https://{0}:{1}' -f $h, $Port)
}

function Invoke-RemoteAccessVerb {
    param($Opts)
    $s = Get-InstalledSettingsOrExplain
    if (-not $s) { return (New-VerbResult 'failed' ([ordered]@{ reason = 'not-installed' })) }
    Write-InfoLine 'Web AI clients such as claude.ai need a fixed public HTTPS address to reach Cognita. Tailscale Funnel gives you one for free. Only the MCP port is published, never Admin.'
    $pw = $null
    try { $pw = Get-AdminPasswordFromOpts $Opts }
    catch {
        Write-ProgressLine -Stage 'password' -Title 'Admin password' -State 'failed' -Message $_.Exception.Message
        return (New-VerbResult 'failed' ([ordered]@{ reason = 'no-password' }))
    }
    $mcp = [int]$s.mcp_port
    # 2. Tailscale present?
    $ts = Find-TailscaleExe
    if (-not $ts) {
        # --yes (from Setup, which has already explained the download) or --install-tailscale is consent.
        $consent = ([bool](Get-Opt $Opts 'yes' $false) -or [bool](Get-Opt $Opts 'install-tailscale' $false))
        if (-not $consent -and (Test-Interactive)) {
            $a = Read-Line 'Tailscale is not installed. Setup will download the official installer (about 40 MB) from pkgs.tailscale.com and run it. Continue? (y/N)'
            $consent = ($a -match '^(y|yes)$')
        }
        if (-not $consent) {
            Write-ProgressLine -Stage 'remote' -Title 'Tailscale' -State 'failed' -Message 'Tailscale is not installed.' -Fix 'Allow Setup to download the official Tailscale installer, or install Tailscale yourself from tailscale.com, then run this again.'
            return (New-VerbResult 'failed' ([ordered]@{ reason = 'tailscale-missing' }))
        }
        if (-not (Install-TailscaleMsi)) { return (New-VerbResult 'failed' ([ordered]@{ reason = 'tailscale-install' })) }
        $ts = Find-TailscaleExe
    }
    Write-Log ("remote-access: tailscale at {0}" -f $ts)
    # 3. Signed in?
    $st = Get-TailscaleStatusObject -Exe $ts
    if (-not $st -or $st.BackendState -ne 'Running') {
        $stateNow = ''; if ($st) { $stateNow = [string]$st.BackendState }
        Write-Log ("remote-access: BackendState={0}; running tailscale up" -f $stateNow)
        $name = ((Get-ComputerNameText).ToLowerInvariant() -replace '[^a-z0-9-]', '-')
        $seen = @{ Url = '' }
        $onLine = {
            param($line)
            if ($line -match '(https://login\.tailscale\.com/\S+)' -and $seen.Url -ne $Matches[1]) {
                $seen.Url = $Matches[1]
                Write-ProgressLine -Stage 'remote.login' -Title 'Sign in to Tailscale' -State 'warning' -Message ('Open this link to sign in to Tailscale: ' + $seen.Url) -Fix 'Setup continues by itself when you have signed in.'
            }
        }.GetNewClosure()
        # Design 18.4: every heartbeat of the sign-in wait carries the link again (in the message), so the
        # link on Setup's page never scrolls away or disappears once the first warning line is replaced.
        $beat = {
            $m = 'Still waiting.'
            if ($seen.Url) { $m = 'Still waiting. Open this link to sign in to Tailscale: ' + $seen.Url }
            Write-ProgressLine -Stage 'remote.login' -Title 'Waiting for you to sign in to Tailscale' -State 'progress' -Message $m
        }.GetNewClosure()
        $up = Invoke-Tailscale -Exe $ts -TsArgs @('up', '--unattended', ('--hostname=cognita-' + $name)) -TimeoutSec 600 -OnPoll $beat -PollIntervalMs 5000 -OnErrorLine $onLine -OnOutputLine $onLine
        if ($up.ExitCode -ne 0) {
            $why = 'Tailscale sign-in did not finish.'
            if ($up.TimedOut) { $why = 'Tailscale sign-in did not finish within 10 minutes.' }
            Write-ProgressLine -Stage 'remote.login' -Title 'Sign in to Tailscale' -State 'failed' -Message $why -Fix 'Run this again and open the link it prints.'
            return (New-VerbResult 'failed' ([ordered]@{ reason = 'tailscale-login' }))
        }
        $cond = { $x = Get-TailscaleStatusObject -Exe $ts; return [bool]($x -and $x.BackendState -eq 'Running') }.GetNewClosure()
        if (-not (Wait-Until -Condition $cond -TimeoutSec 120 -IntervalMs 3000 -Description 'tailscale BackendState Running' -Stage 'remote.login' -Title 'Waiting for Tailscale')) {
            Write-ProgressLine -Stage 'remote.login' -Title 'Sign in to Tailscale' -State 'failed' -Message 'Tailscale did not report that it is running.' -Fix 'Run this again.'
            return (New-VerbResult 'failed' ([ordered]@{ reason = 'tailscale-not-running' }))
        }
    }
    # 4. Funnel: never clobber one that is already serving something else
    $serving = Get-FunnelServingNow -Exe $ts
    $wanted = [int](Get-Opt $Opts 'funnel-port' 0)
    $plan = Get-FunnelPlan -Serving $serving -McpPort $mcp -Preferred $wanted
    Write-Log ("remote-access: funnel plan action={0} port={1} free=[{2}]" -f $plan.Action, $plan.Port, ($plan.Free -join ','))
    if ($plan.Action -eq 'choose') {
        if (Test-Interactive) {
            $pick = Read-Line ('HTTPS port 443 already serves something else on your tailnet. Use {0} instead? Type the port, or press Enter to skip' -f ($plan.Free -join ' or '))
            if ($pick -match '^\d+$') { $plan = Get-FunnelPlan -Serving $serving -McpPort $mcp -Preferred ([int]$pick) }
        } else {
            Write-ProgressLine -Stage 'remote.funnel' -Title 'Funnel' -State 'warning' -Message ('HTTPS port 443 already serves something else. Funnel can use {0} instead.' -f ($plan.Free -join ' or ')) -Fix 'Choose one of those ports, or skip remote access.'
            return (New-VerbResult 'failed' ([ordered]@{ reason = 'funnel-port-busy'; offer = ($plan.Free -join ',') }))
        }
    }
    if ($plan.Action -eq 'choose' -or $plan.Action -eq 'none') {
        Write-ProgressLine -Stage 'remote.funnel' -Title 'Funnel' -State 'failed' -Message 'No free Funnel port was chosen, so remote access was skipped. Your existing Funnel was not changed.' -Fix 'Free an HTTPS port on your tailnet (443, 8443 or 10000) and run this again.'
        return (New-VerbResult 'failed' ([ordered]@{ reason = 'funnel-no-port' }))
    }
    $setByUs = $false
    if ($plan.Action -eq 'set') {
        Write-ProgressLine -Stage 'remote.funnel' -Title 'Turning on Funnel' -State 'start'
        $f = Invoke-Tailscale -Exe $ts -TsArgs @('funnel', '--bg', ('--https=' + $plan.Port), [string]$mcp) -TimeoutSec 120
        $ftext = ($f.Stdout + "`n" + $f.Stderr)
        if ($f.ExitCode -ne 0 -or $ftext -match 'not enabled on your tailnet') {
            $link = ''
            if ($ftext -match '(https://\S+)') { $link = $Matches[1] }
            if ($ftext -match 'not enabled on your tailnet') {
                # Design 18.4: a `failed` line (was `warning`) whose message carries the approval link, and the
                # same link on the result line, so Setup can show it as a link and offer Retry.
                Write-ProgressLine -Stage 'remote.funnel' -Title 'Turning on Funnel' -State 'failed' -Message ('Your tailnet''s owner must turn on Funnel once. Open this link, turn it on, then press Retry. ' + $link) -Fix 'Then run this again (or press Retry in Setup).' -MessageId 'setup.funnel.enable_required' -Values @{ link = $link }
                return (New-VerbResult 'failed' ([ordered]@{ reason = 'funnel-not-enabled'; link = $link }))
            }
            Write-ProgressLine -Stage 'remote.funnel' -Title 'Turning on Funnel' -State 'failed' -Message ('tailscale funnel failed (exit {0}).' -f $f.ExitCode) -Fix 'Run this again. If it keeps failing, use cognita diagnostics.'
            return (New-VerbResult 'failed' ([ordered]@{ reason = 'funnel-failed' }))
        }
        $setByUs = $true
        Write-ProgressLine -Stage 'remote.funnel' -Title 'Turning on Funnel' -State 'done'
    } else { Write-Log ("remote-access: port {0} already serves localhost:{1}; nothing to set" -f $plan.Port, $mcp) }
    # 5. Public URL from Self.DNSName
    $cond = { $x = Get-TailscaleStatusObject -Exe $ts; return [bool]($x -and $x.Self -and $x.Self.DNSName) }.GetNewClosure()
    [void](Wait-Until -Condition $cond -TimeoutSec 60 -IntervalMs 2000 -Description 'tailscale Self.DNSName')
    $st2 = Get-TailscaleStatusObject -Exe $ts
    if (-not ($st2 -and $st2.Self -and $st2.Self.DNSName)) {
        Write-ProgressLine -Stage 'remote.funnel' -Title 'Funnel address' -State 'failed' -Message 'Tailscale did not report this PC''s address.' -Fix 'Run this again.'
        return (New-VerbResult 'failed' ([ordered]@{ reason = 'no-dnsname' }))
    }
    $url = Get-PublicUrlFromDnsName -DnsName ([string]$st2.Self.DNSName) -Port $plan.Port
    Write-Log ("remote-access: public url {0}" -f $url)
    if ($setByUs) {
        Set-SettingProp $s 'funnel' ([pscustomobject][ordered]@{ https_port = $plan.Port; target = $mcp })
        Save-Settings $s
    }
    # 6. Linux: save the public base URL, check /healthz through it and the 401
    $lr = Invoke-CognitaLinux -Settings $s -CliArgs @('remote-access', '--external-url', $url, '--admin-password-stdin', '--non-interactive') -StdinText ($pw + "`n") -Stage 'remote_access' -TimeoutSec 1800 -Title 'Remote access'
    $pw = $null
    if ($lr.ExitCode -ne 0) {
        if (-not $lr.Failed) { Write-ProgressLine -Stage 'remote_access' -Title 'Remote access' -State 'failed' -Message ('Saving the public address failed (exit {0}). {1}' -f $lr.ExitCode, $lr.Summary) }
        return (New-VerbResult 'failed' ([ordered]@{ reason = 'linux-remote-access'; public_url = $url }))
    }
    Write-InfoLine ('Remote access is on. Public address: {0}' -f $url)
    return (New-VerbResult 'ok' ([ordered]@{ public_url = $url; port = $plan.Port }))
}

# ---------------------------------------------------------------------------------------
# diagnostics (design 10)
# ---------------------------------------------------------------------------------------
function Protect-DiagnosticText {
    # /mcp/<token> path segments become /mcp/<redacted> (tokens ride in the URL path).
    param([string]$Text)
    if ([string]::IsNullOrEmpty($Text)) { return $Text }
    return [regex]::Replace($Text, '/mcp/[^\s/"''?<>&\\]+', '/mcp/<redacted>')
}

function Protect-DiagnosticFiles {
    # The final scan: every text file in the staging folder, in place. Returns how many changed.
    param([string]$Dir)
    $changed = 0
    $exts = @('.txt', '.log', '.json', '.jsonl', '.md', '.conf', '.ini', '.csv', '.env', '.yaml', '.yml')
    foreach ($f in (Get-ChildItem -LiteralPath $Dir -Recurse -File -Force -ErrorAction SilentlyContinue)) {
        if ($exts -notcontains $f.Extension.ToLowerInvariant()) { continue }
        try {
            $utf8 = New-Object System.Text.UTF8Encoding($false)
            $t = [System.IO.File]::ReadAllText($f.FullName, $utf8)
            $n = Protect-DiagnosticText $t
            if ($n -ne $t) { [System.IO.File]::WriteAllText($f.FullName, $n, $utf8); $changed++ }
        } catch { Write-Log ("diagnostics scan: could not scan {0}: {1}" -f $f.FullName, $_.Exception.Message) }
    }
    Write-Log ("diagnostics scan: {0} file(s) had a token path redacted" -f $changed)
    return $changed
}

function Format-WslConfigForDiagnostics {
    # Keys and values, except the value of anything named like a secret.
    param([string]$Text)
    if (-not $Text) { return '(no .wslconfig)' }
    $out = @()
    foreach ($l in ($Text -split "`r?`n")) {
        $redact = $false; $key = ''
        if ($l -match '^\s*([^=#;\[]+?)\s*=\s*(.*)$') {
            $key = $Matches[1]      # captured first: the next -match would overwrite $Matches
            if ($key -match '(?i)pass|secret|token|key') { $redact = $true }
        }
        if ($redact) { $out += ($key + '=<redacted>') } else { $out += $l }
    }
    return ($out -join "`n")
}

function Save-DiagFile {
    param([string]$Dir, [string]$Name, [string]$Text)
    $p = Join-Path $Dir $Name
    $parent = Split-Path -Parent $p
    if (-not (Test-Path -LiteralPath $parent)) { [void](New-Item -ItemType Directory -Path $parent -Force) }
    [System.IO.File]::WriteAllText($p, $Text, (New-Object System.Text.UTF8Encoding($false)))
}

function Save-DiagCommand {
    param([string]$Dir, [string]$Name, [string]$Exe, [string[]]$CmdArgs, [int]$TimeoutSec = 60)
    $r = Invoke-External -FilePath $Exe -Arguments $CmdArgs -TimeoutSec $TimeoutSec -NoLogOutput
    $text = ('$ {0} {1}' -f (Split-Path -Leaf $Exe), ($CmdArgs -join ' ')) + "`nexit code: " + $r.ExitCode + "`n`n" + (Remove-NulAndBom $r.Stdout)
    if ($r.Stderr) { $text += "`n--- stderr ---`n" + (Remove-NulAndBom $r.Stderr) }
    Save-DiagFile -Dir $Dir -Name $Name -Text $text
}

function Copy-FileShared {
    # Copies a file that another process may have open (the log we are writing).
    param([string]$From, [string]$To)
    $in = [System.IO.File]::Open($From, 'Open', 'Read', 'ReadWrite, Delete')
    try {
        $out = [System.IO.File]::Create($To)
        try { $in.CopyTo($out) } finally { $out.Dispose() }
    } finally { $in.Dispose() }
}

function Save-WindowsDiagnostics {
    param([string]$Stage, $Settings, [string]$SetupLog)
    $w = Join-Path $Stage 'windows'
    [void](New-Item -ItemType Directory -Path $w -Force)
    $logs = Get-LogsDir
    if (Test-Path -LiteralPath $logs) {
        foreach ($f in (Get-ChildItem -LiteralPath $logs -File -Force -ErrorAction SilentlyContinue)) {
            try { Copy-FileShared -From $f.FullName -To (Join-Path (Join-Path $w 'logs') $f.Name) } catch {
                Write-Log ("diagnostics: could not copy log {0}: {1}" -f $f.Name, $_.Exception.Message)
                try { [void](New-Item -ItemType Directory -Path (Join-Path $w 'logs') -Force); Copy-FileShared -From $f.FullName -To (Join-Path (Join-Path $w 'logs') $f.Name) } catch { Write-Log ("diagnostics: retry failed for {0}" -f $f.Name) }
            }
        }
    }
    if ($SetupLog -and (Test-Path -LiteralPath $SetupLog)) {
        try { Copy-FileShared -From $SetupLog -To (Join-Path $w ('setup-' + (Split-Path -Leaf $SetupLog))) } catch { Write-Log ("diagnostics: could not copy the Setup log: {0}" -f $_.Exception.Message) }
    }
    if (Test-Path -LiteralPath (Get-SettingsPath)) { Copy-FileShared -From (Get-SettingsPath) -To (Join-Path $w 'settings.json') }
    $wsl = Get-WslExe
    Save-DiagCommand -Dir $w -Name 'wsl-version.txt' -Exe $wsl -CmdArgs @('--version')
    Save-DiagCommand -Dir $w -Name 'wsl-list.txt' -Exe $wsl -CmdArgs @('-l', '-v')
    Save-DiagCommand -Dir $w -Name 'wsl-status.txt' -Exe $wsl -CmdArgs @('--status')
    Save-DiagFile -Dir $w -Name 'wslconfig.txt' -Text (Format-WslConfigForDiagnostics (Get-WslConfigText))
    $os = Get-OsInfo; $v = Get-VirtualizationInfo
    $vhd = ''; if ($Settings) { $vhd = [string]$Settings.vhd_dir }
    if (-not $vhd) { $vhd = Get-DefaultVhdDir }
    $sys = @(
        ('windows build: {0} (64-bit: {1})' -f $os.Build, $os.Is64),
        ('hypervisor present: {0}' -f $v.Hypervisor),
        ('virtualization firmware enabled: {0}' -f $v.Firmware),
        ('memory bytes: {0}' -f (Get-MemoryBytes)),
        ('free bytes at {0}: {1}' -f $vhd, (Get-FreeSpaceBytes $vhd))
    ) -join "`n"
    Save-DiagFile -Dir $w -Name 'system.txt' -Text $sys
    $task = Get-CognitaTaskInfo
    # Real newlines (design 18.5): inside single quotes the backtick-n was written as two literal characters.
    Save-DiagFile -Dir $w -Name 'scheduled-task.txt' -Text (@(
        ('task exists: {0}' -f $task.Exists),
        ('state: {0}' -f $task.State),
        ('last result: {0}' -f $task.LastResult),
        ('last run: {0}' -f $task.LastRun)
    ) -join "`n")
    $mcp = 8675; $adm = 8676
    if ($Settings) { $mcp = [int]$Settings.mcp_port; $adm = [int]$Settings.admin_port }
    $ls = @(Get-ListeningPorts | Where-Object { $_.Port -eq $mcp -or $_.Port -eq $adm })
    $ltxt = @($ls | ForEach-Object { ('port {0}: pid {1} {2}' -f $_.Port, $_.ProcessId, $_.Process) })
    if ($ltxt.Count -eq 0) { $ltxt = @('nothing is listening on the MCP or Admin port') }
    Save-DiagFile -Dir $w -Name 'listeners.txt' -Text ($ltxt -join "`n")
    $ts = Find-TailscaleExe
    if ($ts) {
        # Self only: other devices on the tailnet are not collected.
        $tso = Get-TailscaleStatusObject -Exe $ts
        if ($tso) {
            $selfOnly = [ordered]@{ BackendState = $tso.BackendState; Version = $tso.Version; Self = $tso.Self }
            Save-DiagFile -Dir $w -Name 'tailscale-status.json' -Text (ConvertTo-Json -InputObject $selfOnly -Depth 6)
        } else { Save-DiagFile -Dir $w -Name 'tailscale-status.json' -Text '(tailscale status could not be read)' }
        Save-DiagCommand -Dir $w -Name 'tailscale-funnel.txt' -Exe $ts -CmdArgs @('funnel', 'status')
    } else { Save-DiagFile -Dir $w -Name 'tailscale-status.json' -Text '(tailscale is not installed)' }
}

function Save-LinuxDiagnostics {
    param([string]$Stage, $Settings)
    $l = Join-Path $Stage 'linux'
    [void](New-Item -ItemType Directory -Path $l -Force)
    if (-not $Settings) { Save-DiagFile -Dir $l -Name 'UNAVAILABLE.txt' -Text 'No Cognita install record on this PC, so there is nothing to collect inside WSL.'; return }
    $probe = Invoke-Wsl -Settings $Settings -User 'root' -Command @('true') -TimeoutSec 90
    if ($probe.ExitCode -ne 0) {
        Save-DiagFile -Dir $l -Name 'UNAVAILABLE.txt' -Text ("The distro could not be started for diagnostics (exit {0}).`n{1}`n{2}" -f $probe.ExitCode, (Remove-NulAndBom $probe.Stdout), (Remove-NulAndBom $probe.Stderr))
        return
    }
    $zipWin = Join-Path $l 'cognita-diagnostics.zip'
    $z = Invoke-Wsl -Settings $Settings -User (Get-LinuxUser $Settings) -Command @($script:LinuxCliPath, 'diagnostics', '--out', (ConvertTo-WslMntPath $zipWin)) -TimeoutSec 600
    if ($z.ExitCode -ne 0) { Save-DiagFile -Dir $l -Name 'cognita-diagnostics.error.txt' -Text ("cognita diagnostics failed (exit {0}).`n{1}`n{2}" -f $z.ExitCode, (Remove-NulAndBom $z.Stdout), (Remove-NulAndBom $z.Stderr)) }
    # The root lines AND the roots folder's base line (design 22.14), with line numbers: the base line
    # must come before every root line, and this file is where that shows.
    $script1 = "grep -nE '^[^#]*/mnt/cognita-roots(/|[[:space:]])' /etc/fstab </dev/null || true`nexit 0`n"
    $f = Invoke-WslScript -Settings $Settings -User 'root' -ScriptText $script1 -TimeoutSec 60
    Save-DiagFile -Dir $l -Name 'fstab-roots.txt' -Text (Remove-NulAndBom $f.Stdout)
    # Two views since design 18.1: this session's own copy of the mount namespace, and PID 1's, which is what
    # Docker sees (nsenter -t 1 -m) with its propagation column (`shared` is what the rslave bind needs).
    $script2 = "mount </dev/null | grep cognita-roots || true`necho '--- docker view (nsenter -t 1 -m) ---'`nnsenter -t 1 -m -- findmnt -n -o TARGET,FSTYPE,PROPAGATION </dev/null | grep cognita-roots || true`nexit 0`n"
    $m = Invoke-WslScript -Settings $Settings -User 'root' -ScriptText $script2 -TimeoutSec 60
    Save-DiagFile -Dir $l -Name 'mounts.txt' -Text (Remove-NulAndBom $m.Stdout)
    $sf = Invoke-Wsl -Settings $Settings -User 'root' -Command @('systemctl', '--failed', '--no-pager') -TimeoutSec 60
    Save-DiagFile -Dir $l -Name 'systemctl-failed.txt' -Text (Remove-NulAndBom ($sf.Stdout + $sf.Stderr))
    $jr = Invoke-Wsl -Settings $Settings -User 'root' -Command @('journalctl', '-b', '--no-pager', '-n', '2000') -TimeoutSec 120 -NoLogOutput
    Save-DiagFile -Dir $l -Name 'journal-boot.txt' -Text (Remove-NulAndBom ($jr.Stdout + $jr.Stderr))
}

function New-ZipFromDirectory {
    # Own zip writer: ZipFile.CreateFromDirectory under Windows PowerShell 5.1 writes entry names with
    # backslashes (measured), which unzip tools other than Explorer treat as part of the file name.
    # Entries here use forward slashes, as the zip format requires.
    param([string]$Dir, [string]$ZipPath)
    Add-Type -AssemblyName System.IO.Compression
    $root = (Get-Item -LiteralPath $Dir).FullName.TrimEnd('\')
    $fs = [System.IO.File]::Create($ZipPath)
    try {
        $zip = New-Object System.IO.Compression.ZipArchive($fs, [System.IO.Compression.ZipArchiveMode]::Create, $false)
        try {
            foreach ($f in (Get-ChildItem -LiteralPath $Dir -Recurse -File -Force)) {
                $rel = $f.FullName.Substring($root.Length + 1) -replace '\\', '/'
                $entry = $zip.CreateEntry($rel, [System.IO.Compression.CompressionLevel]::Optimal)
                $es = $entry.Open()
                try {
                    $in = [System.IO.File]::OpenRead($f.FullName)
                    try { $in.CopyTo($es) } finally { $in.Dispose() }
                } finally { $es.Dispose() }
            }
        } finally { $zip.Dispose() }
    } finally { $fs.Dispose() }
}

function Invoke-DiagnosticsVerb {
    param($Opts)
    $s = Read-Settings
    $computer = Get-ComputerNameText
    $stamp = (Get-ClockNow).ToString('yyyyMMdd-HHmmss')
    $name = 'Cognita-diagnostics-{0}-{1}.zip' -f $computer, $stamp
    $out = [string](Get-Opt $Opts 'out' '')
    if (-not $out) { $out = Join-Path (Get-DesktopPath) $name }
    elseif (Test-Path -LiteralPath $out -PathType Container) { $out = Join-Path $out $name }
    Write-ProgressLine -Stage 'diagnostics' -Title 'Collecting diagnostics' -State 'start'
    $stage = Join-Path ([System.IO.Path]::GetTempPath()) ('cognita-diag-' + [guid]::NewGuid().ToString('N'))
    [void](New-Item -ItemType Directory -Path $stage -Force)
    try {
        try { Save-WindowsDiagnostics -Stage $stage -Settings $s -SetupLog ([string](Get-Opt $Opts 'setup-log' '')) }
        catch { Write-Log ("diagnostics: windows part failed: {0}" -f $_.Exception.Message); Save-DiagFile -Dir (Join-Path $stage 'windows') -Name 'error.txt' -Text ('The Windows part failed: ' + $_.Exception.Message) }
        Write-ProgressLine -Stage 'diagnostics' -Title 'Collecting diagnostics' -State 'progress' -Message 'Windows part done; collecting inside WSL.'
        try { Save-LinuxDiagnostics -Stage $stage -Settings $s }
        catch { Write-Log ("diagnostics: linux part failed: {0}" -f $_.Exception.Message); Save-DiagFile -Dir (Join-Path $stage 'linux') -Name 'UNAVAILABLE.txt' -Text ('The Linux part failed: ' + $_.Exception.Message) }
        $readme = @(
            'Cognita diagnostics',
            ('Collected: ' + (Get-ClockNow).ToString('yyyy-MM-dd HH:mm:ss') + ' (local time)'),
            '',
            'windows/  Cognita''s Windows logs and settings.json, WSL version and list, the .wslconfig keys,',
            '          Windows build, virtualization flags, memory, free disk, the sign-in task, what listens on',
            '          the two ports, and this PC''s own Tailscale entry and Funnel status.',
            'linux/    The Cognita installer''s diagnostics zip, the projects-folder lines of /etc/fstab and the',
            '          mounts, failed systemd units and the boot journal.',
            '',
            'No passwords, secrets, tokens or document contents are collected. Any /mcp/<token> address',
            'in a text file was replaced with /mcp/<redacted>.',
            '',
            'Nothing was sent anywhere. To get help, attach this zip to a new issue at',
            $script:SupportUrl
        ) -join "`n"
        Save-DiagFile -Dir $stage -Name 'README.txt' -Text $readme
        [void](Protect-DiagnosticFiles -Dir $stage)
        $outDir = Split-Path -Parent $out
        if ($outDir -and -not (Test-Path -LiteralPath $outDir)) { [void](New-Item -ItemType Directory -Path $outDir -Force) }
        if (Test-Path -LiteralPath $out) { Remove-Item -LiteralPath $out -Force }
        New-ZipFromDirectory -Dir $stage -ZipPath $out
    } finally {
        try { Remove-NoFollow -Path $stage } catch { Write-Log ("diagnostics: staging cleanup failed: {0}" -f $_.Exception.Message) }
    }
    $size = (Get-Item -LiteralPath $out).Length
    Write-Log ("diagnostics: wrote {0} ({1} bytes)" -f $out, $size)
    Write-ProgressLine -Stage 'diagnostics' -Title 'Collecting diagnostics' -State 'done' -Message ('Saved {0}' -f $out)
    Write-InfoLine ('Diagnostics saved to {0}' -f $out)
    Write-InfoLine ('Nothing was sent anywhere. To get help, attach this file to a new issue at {0}' -f $script:SupportUrl)
    if (-not (Get-Opt $Opts 'no-open' $false)) { Open-InExplorer -Path $out }
    return (New-VerbResult 'ok' ([ordered]@{ zip = $out }))
}

# ---------------------------------------------------------------------------------------
# uninstall (design 7.5): delete only known items, never follow a reparse point
# ---------------------------------------------------------------------------------------
function Remove-NoFollow {
    # Removes a file or folder tree. A reparse point (junction, symbolic link) is removed AS a link;
    # what it points at is never entered.
    param([string]$Path)
    $item = Get-Item -LiteralPath $Path -Force -ErrorAction SilentlyContinue
    if (-not $item) { return }
    if ($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) {
        Write-Log ("delete: {0} is a link; removing the link only" -f $Path)
        if ($item.PSIsContainer) { [System.IO.Directory]::Delete($Path, $false) }
        else { [System.IO.File]::Delete($Path) }
        return
    }
    if ($item.PSIsContainer) {
        foreach ($child in [System.IO.Directory]::GetFileSystemEntries($Path)) { Remove-NoFollow -Path $child }
        [System.IO.Directory]::Delete($Path, $false)
    } else {
        [System.IO.File]::SetAttributes($Path, [System.IO.FileAttributes]::Normal)
        [System.IO.File]::Delete($Path)
    }
}

function Get-UninstallDeleteList {
    # The known items that "delete data" removes. Nothing under any root is ever on this list.
    param($Settings)
    $d = Get-DataRoot
    $list = @(
        (Join-Path $d 'settings.json'),
        (Join-Path $d 'settings.json.tmp'),
        (Join-Path $d 'stopped'),
        (Join-Path $d 'logs')
    )
    return $list
}

function Assert-DeleteListSafe {
    param([string[]]$Paths, $Settings)
    foreach ($p in @($Paths)) {
        foreach ($r in (Get-SettingsRoots $Settings)) {
            if (Test-PathNests $p ([string]$r.windows)) { throw ("Refusing to delete {0}: it overlaps the projects folder {1}." -f $p, $r.windows) }
        }
    }
}

function Invoke-UninstallVerb {
    param($Opts)
    $delete = [bool](Get-Opt $Opts 'delete-data' $false)
    $keep = [bool](Get-Opt $Opts 'keep-data' $false)
    if ($delete -eq $keep) { return (New-VerbResult 'failed' ([ordered]@{ error = 'usage: uninstall --keep-data|--delete-data' })) }
    $s = Read-Settings
    Write-Log ("uninstall: mode={0}" -f $(if ($delete) { 'delete-data' } else { 'keep-data' }))
    # 1. stopped flag first: the keepalive stops relaunching
    $dataRoot = Get-DataRoot
    if (-not (Test-Path -LiteralPath $dataRoot)) { [void](New-Item -ItemType Directory -Path $dataRoot -Force) }
    [System.IO.File]::WriteAllText((Get-StoppedFlagPath), ((Get-ClockNow).ToString('yyyy-MM-dd HH:mm:ss') + "`n"))
    # 2. inside the distro, if it is ours and starts
    $ours = $false
    if ($s) {
        $own = Get-DistroOwnership -Settings $s
        $ours = ($own.Exists -and $own.Owned)
        Write-Log ("uninstall: distro exists={0} owned={1} ({2})" -f $own.Exists, $own.Owned, $own.Reason)
    }
    if ($ours) {
        Write-ProgressLine -Stage 'uninstall' -Title 'Removing Cognita inside WSL' -State 'start'
        # Design 18.3: --yes --non-interactive. Without them the Linux CLI asks "Uninstall Cognita now?"
        # and fails on the empty stdin (P1 review 4), so the Linux side was never actually uninstalled.
        $r = Invoke-Wsl -Settings $s -User (Get-LinuxUser $s) -Command @($script:LinuxCliPath, 'uninstall', '--yes', '--non-interactive') -StdinText '' -TimeoutSec 900
        Write-Log ("uninstall: cognita uninstall exit {0}" -f $r.ExitCode)
        if ($r.ExitCode -ne 0) { Write-ProgressLine -Stage 'uninstall' -Title 'Removing Cognita inside WSL' -State 'warning' -Message ('cognita uninstall inside WSL did not finish (exit {0}); the Windows parts continue.' -f $r.ExitCode) }
        else { Write-ProgressLine -Stage 'uninstall' -Title 'Removing Cognita inside WSL' -State 'done' }
    } else { Write-Log 'uninstall: no owned distro that starts; skipping the inside-WSL step' }
    # 3. task and Funnel (Funnel only if we set it and it still shows exactly that target)
    $task = Get-CognitaTaskInfo
    if ($task.Exists) {
        try { Unregister-CognitaTask; Write-Log 'uninstall: scheduled task removed' } catch { Write-Log ("uninstall: task removal failed: {0}" -f $_.Exception.Message) }
    }
    if ($s -and $s.funnel) {
        $ts = Find-TailscaleExe
        if ($ts) {
            $serving = Get-FunnelServingNow -Exe $ts
            $port = [int]$s.funnel.https_port
            $exact = ($serving.ContainsKey($port) -and @($serving[$port]).Count -gt 0 -and @(@($serving[$port]) | Where-Object { -not (Test-FunnelTargetIsMcp $_ ([int]$s.funnel.target)) }).Count -eq 0)
            Write-Log ("uninstall: funnel recorded port={0} target={1} exactStillServed={2}" -f $port, $s.funnel.target, $exact)
            if ($exact) {
                $f = Invoke-Tailscale -Exe $ts -TsArgs @('funnel', ('--https=' + $port), 'off') -TimeoutSec 60
                Write-Log ("uninstall: tailscale funnel off exit {0}" -f $f.ExitCode)
            }
        } else { Write-Log 'uninstall: funnel recorded but tailscale.exe not found; nothing turned off' }
    }
    # 4. terminate, and only a distro that is OURS (design 18.5): a foreign distro that happens to be
    # named like ours must not be shut down by an uninstall of ours.
    if ($s -and $ours) {
        $t = Invoke-External -FilePath (Get-WslExe) -Arguments @('--terminate', (Get-DistroName $s)) -TimeoutSec 60
        Write-Log ("uninstall: wsl --terminate exit {0}" -f $t.ExitCode)
    } else { Write-Log ("uninstall: wsl --terminate skipped (settings={0} distroIsOurs={1})" -f [bool]$s, $ours) }
    if (-not $delete) {
        # 5. keep data: settings.json stays with state uninstalled, logs stay, the distro stays
        $bytes = 0; $vhd = ''
        if ($s) {
            Set-SettingProp $s 'state' 'uninstalled'
            Set-SettingProp $s 'funnel' $null
            Set-SettingProp $s 'resume' $null
            Save-Settings $s
            $vhd = [string]$s.vhd_dir
            $bytes = Get-DirTopLevelBytes $vhd
        }
        Write-InfoLine ("Your documents were not touched. Cognita's data is kept in {0} ({1:N1} GB). Installing Cognita again reuses it." -f $vhd, ($bytes / 1GB))
        return (New-VerbResult 'ok' ([ordered]@{ kept = 1; vhd_dir = $vhd; vhd_bytes = $bytes }))
    }
    # 6. delete data: ownership again, unregister, copy the logs out, delete only known items
    $copy = Join-Path ([System.IO.Path]::GetTempPath()) ('Cognita-uninstall-{0}' -f (Get-ClockNow).ToString('yyyyMMdd-HHmmss'))
    $logs = Get-LogsDir
    $copyLogs = {
        if (Test-Path -LiteralPath $logs) {
            [void](New-Item -ItemType Directory -Path $copy -Force)
            foreach ($f in (Get-ChildItem -LiteralPath $logs -File -Force -ErrorAction SilentlyContinue)) {
                try { Copy-FileShared -From $f.FullName -To (Join-Path $copy $f.Name) } catch { Write-Log ("uninstall: could not copy log {0}: {1}" -f $f.Name, $_.Exception.Message) }
            }
            Write-InfoLine ('Logs were copied to {0}' -f $copy)
        }
    }
    if ($s) {
        $own2 = Get-DistroOwnership -Settings $s
        if ($own2.Exists -and $own2.Owned) {
            if (-not (Invoke-UnregisterDistro -Settings $s)) {
                # Design 19.4 item 9: by now the Linux side is uninstalled, the task and the Funnel are gone
                # and the distro is terminated, so Cognita IS removed; only the data could not be deleted.
                # The record must say so BEFORE this returns failed (state=uninstalled, as a keep-data
                # uninstall leaves it): with state left at `installed` the next Setup run would offer a
                # repair of an install that no longer works. The logs are copied out here too (nothing
                # else will), so Setup can name the folder in its closing text.
                Set-SettingProp $s 'state' 'uninstalled'
                Set-SettingProp $s 'funnel' $null
                Set-SettingProp $s 'resume' $null
                Save-Settings $s
                Write-Log 'uninstall: wsl --unregister failed; settings saved with state=uninstalled before returning failed'
                Write-ProgressLine -Stage 'uninstall' -Title 'Deleting Cognita''s data' -State 'failed' -Message 'wsl --unregister failed, so nothing was deleted.' -Fix 'Close programs using Cognita''s distro and try again.'
                & $copyLogs
                $failVals = [ordered]@{ reason = 'unregister-failed' }
                if (Test-Path -LiteralPath $copy) { $failVals['logs_copy'] = $copy }
                Write-Log ("uninstall: returning failed reason=unregister-failed logs_copy=[{0}]" -f $failVals['logs_copy'])
                return (New-VerbResult 'failed' $failVals)
            }
        } elseif ($own2.Exists) {
            Write-ProgressLine -Stage 'uninstall' -Title 'Deleting Cognita''s data' -State 'warning' -Message ('A distro named {0} exists but is not this install''s, so it was NOT removed.' -f (Get-DistroName $s))
        }
    }
    & $copyLogs
    $list = @(Get-UninstallDeleteList -Settings $s)
    Assert-DeleteListSafe -Paths $list -Settings $s
    $removed = 0
    $log0 = $script:LogFile
    foreach ($p in $list) {
        try {
            if ($p -eq (Get-LogsDir)) { $script:LogFile = $null }   # our own log lives there
            Remove-NoFollow -Path $p; $removed++
        } catch { $script:LogFile = $log0; Write-Log ("uninstall: could not delete {0}: {1}" -f $p, $_.Exception.Message) }
    }
    # the disk folder: only if empty after the unregister (never recursively)
    if ($s -and $s.vhd_dir -and (Test-Path -LiteralPath ([string]$s.vhd_dir) -PathType Container)) {
        $vhdItem = [string]$s.vhd_dir
        $left = @([System.IO.Directory]::GetFileSystemEntries($vhdItem))
        if ($left.Count -eq 0) { try { [System.IO.Directory]::Delete($vhdItem, $false) } catch { $script:LogFile = $log0; Write-Log ("uninstall: could not remove the empty disk folder: {0}" -f $_.Exception.Message) } }
        else { $script:LogFile = $log0; Write-Log ("uninstall: disk folder {0} not empty after unregister ({1} item(s)); left alone" -f $vhdItem, $left.Count) }
    }
    # Design 19.7 item 20: the parent folder(s) the helper itself created for a custom disk location
    # (settings `created_dirs`, deepest first), removed only when EMPTY and never recursively: a folder the
    # user has since put something in stays. The helper's own log file usually went with the logs
    # folder above, so these decisions reach only the in-memory log (Write-Log tolerates that); each still
    # states its value.
    if ($s -and $s.PSObject.Properties['created_dirs'] -and $null -ne $s.created_dirs) {
        foreach ($dir in @($s.created_dirs)) {
            $d = [string]$dir
            try {
                if (-not $d) { continue }
                if ([System.IO.Path]::GetPathRoot($d) -ieq $d) { Write-Log ("uninstall: created dir [{0}] is a drive root; left alone" -f $d); continue }
                if (-not (Test-Path -LiteralPath $d -PathType Container)) { Write-Log ("uninstall: created dir [{0}] is already gone" -f $d); continue }
                $entries = @([System.IO.Directory]::GetFileSystemEntries($d))
                if ($entries.Count -eq 0) {
                    [System.IO.Directory]::Delete($d, $false)
                    Write-Log ("uninstall: removed the empty created dir [{0}]" -f $d)
                } else { Write-Log ("uninstall: created dir [{0}] holds {1} item(s); left alone" -f $d, $entries.Count) }
            } catch { Write-Log ("uninstall: could not remove created dir [{0}]: {1}" -f $d, $_.Exception.Message) }
        }
    }
    return (New-VerbResult 'ok' ([ordered]@{ deleted = 1; removed = $removed; logs_copy = $(if (Test-Path -LiteralPath $copy) { $copy } else { '' }) }))
}

# ---------------------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------------------
function Remove-OldHelperLogs {
    # Every cognita command writes a helper log; keep the newest 100.
    try {
        $dir = Get-LogsDir
        if (-not (Test-Path -LiteralPath $dir)) { return }
        $old = @(Get-ChildItem -LiteralPath $dir -Filter 'helper-*.log' -File -ErrorAction Stop | Sort-Object Name -Descending | Select-Object -Skip 100)
        foreach ($f in $old) { Remove-Item -LiteralPath $f.FullName -Force }
        if ($old.Count -gt 0) { Write-Log ("log cleanup: removed {0} helper log(s) older than the newest 100" -f $old.Count) }
    } catch { Write-Log ("log cleanup failed: {0}" -f $_.Exception.Message) }
}

function Invoke-Verb {
    param([string]$VerbName, [string[]]$Tokens)
    $parsed = ConvertFrom-HelperArgs $Tokens
    $o = $parsed.Opts
    switch ($VerbName) {
        'state' { return (Invoke-StateVerb -Opts $o) }
        'check' { return (Invoke-CheckVerb -Opts $o) }
        'wsl-install' { return (Invoke-WslInstallVerb -Opts $o) }
        'restart-for-wsl' { return (Invoke-RestartForWslVerb -Opts $o) }
        'roots' { return (Invoke-RootsVerb -Opts $o -Positional $parsed.Positional) }
        'install' { return (Invoke-InstallVerb -Opts $o) }
        'update' { return (Invoke-UpdateVerb -Opts $o) }
        'add-folder' { return (Invoke-AddFolderVerb -Opts $o -Positional $parsed.Positional) }
        'remote-access' { return (Invoke-RemoteAccessVerb -Opts $o) }
        'start' { return (Invoke-StartVerb -Opts $o) }
        'stop' { return (Invoke-StopVerb -Opts $o) }
        'restart' { return (Invoke-RestartVerb -Opts $o) }
        'status' { return (Invoke-StatusVerb -Opts $o) }
        'diagnostics' { return (Invoke-DiagnosticsVerb -Opts $o) }
        'uninstall' { return (Invoke-UninstallVerb -Opts $o) }
        'password-broker' { return (Invoke-PasswordBrokerVerb -Opts $o) }
        'cli' { return (Invoke-CliVerb -Tokens $Tokens) }
        default { return (New-VerbResult 'failed' ([ordered]@{ error = ('unknown verb: ' + $VerbName) })) }
    }
}

function Invoke-HelperMain {
    param([string[]]$Argv)
    $Argv = @($Argv | Where-Object { $null -ne $_ })
    $verbName = ''
    if ($Argv.Count -gt 0) { $verbName = ([string]$Argv[0]).ToLowerInvariant() }
    $tokens = @()
    if ($Argv.Count -gt 1) { $tokens = @($Argv[1..($Argv.Count - 1)]) }
    $script:CurrentVerb = $verbName
    $script:HumanMode = ($verbName -eq 'cli')
    Start-HelperLog -VerbName $verbName
    Set-HelperLocale
    Remove-OldHelperLogs
    # A person's terminal: show non-ASCII folder names properly, and the UTF-8 text that wsl.exe
    # writes (WSL_UTF8=1) when a forwarded command owns the console. Restored on the way out so the
    # user's shell keeps its own code page.
    $savedOutEnc = $null
    if ($script:HumanMode) {
        try {
            $savedOutEnc = [Console]::OutputEncoding
            [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
        } catch {
            Write-Log ("console output encoding not changed: {0}" -f $_.Exception.Message)
            $savedOutEnc = $null
        }
    }
    $code = 1
    try {
        if (-not $verbName) {
            $r = New-VerbResult 'failed' ([ordered]@{ error = 'usage: CognitaWin.ps1 <verb> [--option value ...]' })
        } else {
            $r = Invoke-Verb -VerbName $verbName -Tokens $tokens
        }
        $code = Get-ExitCodeForStatus $r.Status
        if ($r.PSObject.Properties['ExitCode']) { $code = [int]$r.ExitCode }
        if (-not $script:HumanMode) { Write-Out (Format-ResultLine -Status $r.Status -Values $r.Values) }
        Write-Log ("helper end verb={0} status={1} exit={2}" -f $verbName, $r.Status, $code)
    } catch {
        Write-Log ("UNHANDLED in verb {0}: {1}`n{2}" -f $verbName, $_.Exception.Message, $_.ScriptStackTrace)
        Write-ProgressLine -Stage 'helper' -Title 'Cognita helper' -State 'failed' -Message ('Unexpected error: ' + $_.Exception.Message) -Fix 'Use Save diagnostics (or run: cognita diagnostics) and send the file.'
        if (-not $script:HumanMode) { Write-Out (Format-ResultLine -Status 'failed' -Values ([ordered]@{ error = $_.Exception.Message })) }
        $code = 1
    } finally {
        if ($null -ne $savedOutEnc) {
            try { [Console]::OutputEncoding = $savedOutEnc } catch { Write-Log ("console output encoding restore failed: {0}" -f $_.Exception.Message) }
        }
    }
    return $code
}

if (-not $NoMain) {
    $mainArgs = @()
    if ($Verb) { $mainArgs += $Verb }
    if ($args) { $mainArgs += @($args | ForEach-Object { [string]$_ }) }
    exit (Invoke-HelperMain -Argv $mainArgs)
}
