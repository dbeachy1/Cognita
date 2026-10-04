# _harness.ps1 - the tiny test framework and the fakes for windows/tests/test_*.ps1.
#
# Every test file starts with:
#     . (Join-Path $PSScriptRoot '..\CognitaWin.ps1') -NoMain
#     . (Join-Path $PSScriptRoot '_harness.ps1')
# and ends with Complete-Tests (exit code 1 and "FAIL:" lines when anything failed).
#
# Rules (docs/DESIGN-WINDOWS-INSTALLER.md 14.2 and the repo's CLAUDE.md):
#   - no real WSL, registry writes, scheduled tasks, network or user profile: Invoke-External and
#     every reader/writer function is replaced here or in the test;
#   - no real sleeps and no waiting on the wall clock: the clock is a fake whose Sleep advances Now.
#     (A test that runs a REAL child process waits only on that process's own exit; its timeout is
#     a hang guard.)
#   - files live in a temp directory made per test and removed after it.

$script:Failures = New-Object System.Collections.ArrayList
$script:Passed = 0
$script:RealClock = $script:Clock
# The real Invoke-External, kept so the few tests that run a REAL child process (the password path,
# quoting) can switch back to it with Use-RealExternal. Captured before the fake is defined below.
$script:RealExternalBlock = (Get-Command Invoke-External -CommandType Function).ScriptBlock
$script:UseRealExternal = $false
# The real Get-WslConfigText, kept for the same reason: the default fake below answers from $script:WslConfig when
# a test set it and otherwise reads the (temp) file Get-WslConfigPath points at, so the .wslconfig writer tests
# (design 22.9) and the preflight reader see one file.
$script:RealGetWslConfigTextBlock = (Get-Command Get-WslConfigText -CommandType Function).ScriptBlock
function Use-RealExternal { $script:UseRealExternal = $true }
function Use-FakeExternal { $script:UseRealExternal = $false }

# ---- assertions ----------------------------------------------------------------------
function Assert-True {
    param($Condition, [string]$Message = 'condition was false')
    if (-not $Condition) { throw $Message }
}
function Assert-False {
    param($Condition, [string]$Message = 'condition was true')
    if ($Condition) { throw $Message }
}
function Assert-Equal {
    param($Expected, $Actual, [string]$What = 'value')
    $e = $Expected; $a = $Actual
    if ($e -is [array]) { $e = ($e -join '|') }
    if ($a -is [array]) { $a = ($a -join '|') }
    if (-not ([string]$e -ceq [string]$a)) { throw ("{0}: expected [{1}] but got [{2}]" -f $What, $e, $a) }
}
function Assert-Match {
    param([string]$Text, [string]$Pattern, [string]$What = 'text')
    if ($Text -notmatch $Pattern) { throw ("{0}: [{1}] does not match /{2}/" -f $What, $Text, $Pattern) }
}
function Assert-NotMatch {
    param([string]$Text, [string]$Pattern, [string]$What = 'text')
    if ($Text -match $Pattern) { throw ("{0}: [{1}] unexpectedly matches /{2}/" -f $What, $Text, $Pattern) }
}
function Assert-Throws {
    param([scriptblock]$Body, [string]$Pattern = '', [string]$What = 'call')
    $threw = $false; $msg = ''
    try { & $Body } catch { $threw = $true; $msg = $_.Exception.Message }
    if (-not $threw) { throw ("{0}: expected an exception but none was thrown" -f $What) }
    if ($Pattern -and $msg -notmatch $Pattern) { throw ("{0}: exception [{1}] does not match /{2}/" -f $What, $msg, $Pattern) }
}

function Test-Case {
    param([string]$Name, [scriptblock]$Body)
    # COGNITA_TEST_FILTER (a regex on the test name) runs only the matching tests while one is being debugged;
    # unset, every test runs. The exit code and the summary line count only the tests that ran.
    if ($env:COGNITA_TEST_FILTER -and $Name -notmatch $env:COGNITA_TEST_FILTER) { return }
    try {
        Initialize-TestEnv
        & $Body
        $script:Passed++
        Write-Host ("ok   {0}" -f $Name)
    } catch {
        [void]$script:Failures.Add(("FAIL: {0}: {1}" -f $Name, $_.Exception.Message))
        Write-Host ("FAIL: {0}: {1}" -f $Name, $_.Exception.Message)
        if ($_.ScriptStackTrace) { Write-Host ("      at " + ($_.ScriptStackTrace -split "`n" | Select-Object -First 3) -join ' | ') }
        # COGNITA_TEST_DUMP=1 prints every recorded external call and the helper log of the failed test.
        if ($env:COGNITA_TEST_DUMP) {
            $n = 0
            foreach ($l in @($script:ExtCalls | ForEach-Object { $_.Line })) { Write-Host ("      call {0}: {1}" -f $n, $l); $n++ }
            foreach ($l in $script:LogLines) { Write-Host ("      log: " + $l) }
        }
    } finally {
        Remove-TestEnv
    }
}

function Complete-Tests {
    Write-Host ("{0} passed, {1} failed" -f $script:Passed, $script:Failures.Count)
    foreach ($f in $script:Failures) { Write-Host $f }
    if ($script:Failures.Count -gt 0) { exit 1 }
    exit 0
}

# ---- the fake clock ---------------------------------------------------------------
function Use-FakeClock {
    param([DateTime]$Start = ([DateTime]'2026-03-01T09:00:00'))
    $script:FakeNow = $Start
    $script:SleepTotalMs = 0
    $script:Clock = [pscustomobject]@{
        Now   = { $script:FakeNow }
        Sleep = { param($Milliseconds) $script:FakeNow = $script:FakeNow.AddMilliseconds($Milliseconds); $script:SleepTotalMs += $Milliseconds }
    }
}
function Use-RealClock { $script:Clock = $script:RealClock }

# ---- captured output -------------------------------------------------------------
$script:Out = New-Object System.Collections.ArrayList
function Write-Out { param([string]$Text) [void]$script:Out.Add($Text) }
# "return ," keeps a one-element array an array (PowerShell would unroll it to a scalar).
function Get-OutLines { return ,@($script:Out) }
function Get-ProgressObjects {
    $o = @()
    foreach ($l in $script:Out) { if ($l.StartsWith('{')) { $o += ($l | ConvertFrom-Json) } }
    return ,@($o)
}
function Get-LogText { return ($script:LogLines -join "`n") }

# ---- per-test environment -------------------------------------------------------------
function Initialize-TestEnv {
    $script:TestDir = Join-Path ([System.IO.Path]::GetTempPath()) ('cogtest-' + [guid]::NewGuid().ToString('N'))
    [void](New-Item -ItemType Directory -Path $script:TestDir -Force)
    $script:SavedHome = $env:COGNITA_HOME
    $env:COGNITA_HOME = Join-Path $script:TestDir 'home'
    [void](New-Item -ItemType Directory -Path $env:COGNITA_HOME -Force)
    $script:LogFile = $null
    $script:LogLines = New-Object System.Collections.ArrayList
    $script:Out = New-Object System.Collections.ArrayList
    $script:HumanMode = $false
    $script:ExtRules = New-Object System.Collections.ArrayList
    $script:ExtCalls = New-Object System.Collections.ArrayList
    Use-FakeExternal
    Use-FakeClock
    Install-DefaultFakes
}
function Remove-TestEnv {
    Use-RealClock
    $env:COGNITA_HOME = $script:SavedHome
    try { if ($script:TestDir -and (Test-Path -LiteralPath $script:TestDir)) { Remove-NoFollow -Path $script:TestDir } } catch { Write-Host ("cleanup failed: " + $_.Exception.Message) }
}

# ---- the fake Invoke-External ----------------------------------------------------------
function New-ExtResult {
    param([int]$ExitCode = 0, [string]$Stdout = '', [string]$Stderr = '', [bool]$TimedOut = $false, [string]$StartError = '')
    return [pscustomobject]@{ ExitCode = $ExitCode; Stdout = $Stdout; Stderr = $Stderr; TimedOut = $TimedOut; StartError = $StartError; Ms = 1 }
}
function Add-ExtRule {
    # Pattern is matched (regex) against "<file name> <arguments joined by a space>". Response is a
    # result object or a scriptblock taking ($Call) and returning one. First match wins. Rules can be
    # inserted at the front with -First so a test overrides a default.
    param([string]$Pattern, $Response, [switch]$First)
    $r = @{ Pattern = $Pattern; Response = $Response }
    if ($First) { $script:ExtRules.Insert(0, $r) } else { [void]$script:ExtRules.Add($r) }
}
function Get-ExtCallLines { return ,@($script:ExtCalls | ForEach-Object { $_.Line }) }
function Get-ExtCallsMatching {
    param([string]$Pattern)
    return ,@($script:ExtCalls | Where-Object { $_.Line -match $Pattern })
}

function Invoke-External {
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
        [switch]$OwnConsole
    )
    if ($script:UseRealExternal) { return (& $script:RealExternalBlock @PSBoundParameters) }
    $line = ('{0} {1}' -f (Split-Path -Leaf $FilePath), ($Arguments -join ' '))
    $call = [pscustomobject]@{ Line = $line; FilePath = $FilePath; Arguments = @($Arguments); StdinText = $StdinText; OnPoll = $OnPoll; OnOutputLine = $OnOutputLine; OnErrorLine = $OnErrorLine; TimeoutSec = $TimeoutSec; PollIntervalMs = $PollIntervalMs; OwnConsole = [bool]$OwnConsole; NoLogOutput = [bool]$NoLogOutput }
    [void]$script:ExtCalls.Add($call)
    # The real function logs the command line; keep that so tests can look for secrets in the log.
    Write-Log ("exec(fake): {0}" -f $line)
    foreach ($rule in $script:ExtRules) {
        if ($line -match $rule.Pattern) {
            $resp = $rule.Response
            if ($resp -is [scriptblock]) { return (& $resp $call) }
            return $resp
        }
    }
    throw ("Unexpected external call: {0}" -f $line)
}

# ---- the fake distro's mounts (design 18.1) ---------------------------------------------------
# A root is mounted in Docker's view only after the distro STARTS with its fstab line in place: the
# fstab lines written through `wsl -u root --exec sh -s` are remembered in $script:FstabRoots, and a
# `wsl --terminate` (Restart-CognitaDistro's step 2) is what turns them into $script:mounted entries.
# Mount checks are answered ONLY for the nsenter -t 1 -m form (a plain `mountpoint -q`, the old
# per-session check, has no rule and so throws "Unexpected external call"), and there is deliberately NO
# rule for `--exec mount`: the helper must never run it. A test that needs a root sh -s behavior of its
# own adds that rule BEFORE calling this (first match wins).
function Add-MountWorldFakes {
    param([bool]$Ready = $true)
    $script:FstabRoots = @{}
    $script:mounted = @{}
    $script:Terminates = 0
    Add-ExtRule 'cat /etc/fstab' (New-ExtResult -Stdout "/dev/sda / ext4 defaults 0 1`n")
    Add-ExtRule '-u root --exec sh -s' { param($c) foreach ($m in [regex]::Matches([string]$c.StdinText, '(/mnt/cognita-roots/\d) drvfs')) { $script:FstabRoots[$m.Groups[1].Value] = $true }; New-ExtResult }
    Add-ExtRule 'mkdir -p -m 0755 /mnt/cognita-roots/\d' (New-ExtResult)
    Add-ExtRule 'nsenter -t 1 -m -- mountpoint -q (/mnt/cognita-roots/\d)' { param($c) if ($script:mounted.ContainsKey($c.Arguments[-1])) { New-ExtResult } else { New-ExtResult -ExitCode 1 } }
    Add-ExtRule 'wsl\.exe --terminate' { param($c) $script:Terminates++; foreach ($k in @($script:FstabRoots.Keys)) { $script:mounted[$k] = $true }; New-ExtResult }
    Add-ExtRule '-u cognita --exec sh -s' (New-ExtResult)
    if ($Ready) { Add-ReadyFakes }
}
function Add-ReadyFakes {
    # Wait-DistroReady: systemd running and docker answering as the Linux user.
    Add-ExtRule 'systemctl is-system-running' (New-ExtResult -Stdout "running`n")
    Add-ExtRule 'docker info' (New-ExtResult -Stdout '27.3.1')
}

$script:PassthroughCalls = New-Object System.Collections.ArrayList
function Invoke-Passthrough {
    param([string]$FilePath, [string[]]$Arguments = @())
    [void]$script:PassthroughCalls.Add(('{0} {1}' -f (Split-Path -Leaf $FilePath), ($Arguments -join ' ')))
    return $script:PassthroughExit
}
$script:PassthroughExit = 0

# ---- default fakes for the machine readers and writers ------------------------------------
function Install-DefaultFakes {
    $script:PassthroughCalls = New-Object System.Collections.ArrayList
    $script:PassthroughExit = 0
    $script:Registry = @{}          # 'path|name' -> value
    $script:LxssDistros = @()       # objects: Guid, Name, BasePath
    $script:LxssDefault = $null
    $script:DefaultSets = New-Object System.Collections.ArrayList
    $script:RegistryKeys = New-Object System.Collections.ArrayList
    $script:RestartPending = $false
    $script:WslConfig = $null
    $script:WslConfigPath = Join-Path $script:TestDir 'userprofile\.wslconfig'
    [void](New-Item -ItemType Directory -Path (Split-Path -Parent $script:WslConfigPath) -Force)
    # Design 22.2: the video controllers and nvidia-smi. Default: no NVIDIA card anywhere (the common case).
    $script:VideoControllers = @()
    $script:VideoControllersThrow = $null
    $script:NvidiaSmiPath = $null
    $script:ListeningPorts = @()
    $script:FreeBytes = [int64]500GB
    $script:MemBytes = [int64]34000000000
    $script:OsBuild = 26100
    $script:Os64 = $true
    $script:VirtHyper = $true
    $script:VirtFw = $true
    $script:DockerDesktop = $false
    $script:DockerDesktopIntegrated = @()
    $script:DriveTypes = @{}
    $script:TaskInfo = [pscustomobject]@{ Exists = $true; State = 'Ready'; LastResult = 0; LastRun = '' }
    $script:TaskRegistered = New-Object System.Collections.ArrayList
    $script:TaskStarted = 0
    $script:TaskRemoved = 0
    $script:Explorer = New-Object System.Collections.ArrayList
    $script:Detached = New-Object System.Collections.ArrayList
    $script:DetachedResult = $true
    $script:Interactive = $false
    $script:SecretAnswer = ''
    $script:LineAnswers = New-Object System.Collections.ArrayList
    $script:TailscaleExe = $null
    $script:ProgressFile = Join-Path $script:TestDir 'progress.jsonl'
    $script:ComputerName = 'TESTPC'
    $script:DesktopDir = Join-Path $script:TestDir 'desktop'
    [void](New-Item -ItemType Directory -Path $script:DesktopDir -Force)

    function script:Get-RegistryValue { param([string]$Path, [string]$Name) $k = "$Path|$Name"; if ($script:Registry.ContainsKey($k)) { return $script:Registry[$k] }; return $null }
    function script:Test-RegistryKey { param([string]$Path) return ($script:RestartPending -and $Path -match 'RebootPending') -or ($script:RegistryKeys -contains $Path) }
    function script:Set-RegistryValue { param([string]$Path, [string]$Name, $Value, [string]$Type = 'String') $script:Registry["$Path|$Name"] = $Value }
    function script:Get-LxssDistros { return @($script:LxssDistros) }
    function script:Get-LxssDefaultDistribution { return $script:LxssDefault }
    function script:Set-LxssDefaultDistribution { param([string]$Guid) [void]$script:DefaultSets.Add($Guid); $script:LxssDefault = $Guid }
    function script:Get-OsInfo { return [pscustomobject]@{ Is64 = $script:Os64; Build = $script:OsBuild } }
    function script:Get-MemoryBytes { return $script:MemBytes }
    function script:Get-VirtualizationInfo { return [pscustomobject]@{ Hypervisor = $script:VirtHyper; Firmware = $script:VirtFw } }
    function script:Get-FreeSpaceBytes { param([string]$Path) return $script:FreeBytes }
    function script:Get-DriveTypeName { param([string]$DriveLetter) if ($script:DriveTypes.ContainsKey($DriveLetter.ToUpper())) { return $script:DriveTypes[$DriveLetter.ToUpper()] }; return 'Fixed' }
    function script:Get-ListeningPorts { return @($script:ListeningPorts) }
    function script:Get-WslConfigPath { return $script:WslConfigPath }
    function script:Get-WslConfigText { if ($null -ne $script:WslConfig) { return $script:WslConfig }; return (& $script:RealGetWslConfigTextBlock) }
    function script:Get-NvidiaSmiPath { return $script:NvidiaSmiPath }
    function script:Get-VideoControllers { if ($script:VideoControllersThrow) { throw $script:VideoControllersThrow }; return @($script:VideoControllers) }
    function script:Get-DockerDesktopInfo { return [pscustomobject]@{ Installed = $script:DockerDesktop; IntegratedDistros = @($script:DockerDesktopIntegrated) } }
    function script:Register-CognitaTask { param([string]$VbsPath) [void]$script:TaskRegistered.Add($VbsPath) }
    function script:Get-CognitaTaskInfo { return $script:TaskInfo }
    function script:Start-CognitaTask { $script:TaskStarted++ }
    function script:Unregister-CognitaTask { $script:TaskRemoved++ }
    function script:Open-InExplorer { param([string]$Path) [void]$script:Explorer.Add($Path) }
    # Design 19.11 R1: the detached restart waiter. Records the command; never starts a process.
    function script:Start-DetachedHidden { param([string]$FilePath, [string[]]$Arguments = @()) [void]$script:Detached.Add([pscustomobject]@{ FilePath = $FilePath; Arguments = @($Arguments) }); return $script:DetachedResult }
    function script:Show-FolderPicker { return $null }
    function script:Test-Interactive { return $script:Interactive }
    function script:Read-SecretLine { param([string]$Prompt) return $script:SecretAnswer }
    function script:Read-Line { param([string]$Prompt) if ($script:LineAnswers.Count -gt 0) { $a = $script:LineAnswers[0]; $script:LineAnswers.RemoveAt(0); return $a }; return '' }
    function script:Find-TailscaleExe { return $script:TailscaleExe }
    function script:Get-ComputerNameText { return $script:ComputerName }
    function script:Get-DesktopPath { return $script:DesktopDir }
    function script:New-ProgressFilePath { return $script:ProgressFile }
    function script:Get-AppDir { return (Join-Path $script:TestDir 'app') }
}

# ---- Tailscale Funnel status (design 19.11 R7: install and state ask Tailscale whether a recorded Funnel is still served)
# Serving: @{ 443 = 'http://127.0.0.1:8675' } -> the JSON `tailscale funnel status --json` prints; {} when empty.
# Add-FunnelStatusFake makes tailscale.exe "installed" and answers that command; $script:FunnelJson can be
# changed by the test between calls.
function Get-FunnelStatusJson {
    param([hashtable]$Serving)
    if ($Serving.Count -eq 0) { return '{}' }
    $tcp = @(); $web = @()
    foreach ($k in $Serving.Keys) {
        $tcp += ('"{0}": {{"HTTPS": true}}' -f $k)
        $web += ('"testpc.tail1234.ts.net:{0}": {{"Handlers": {{"/": {{"Proxy": "{1}"}}}}}}' -f $k, $Serving[$k])
    }
    return ('{"TCP": {' + ($tcp -join ', ') + '}, "Web": {' + ($web -join ', ') + '}}')
}
function Add-FunnelStatusFake {
    param([hashtable]$Serving = @{})
    $script:TailscaleExe = 'C:\ts\tailscale.exe'
    $script:FunnelJson = (Get-FunnelStatusJson $Serving)
    Add-ExtRule 'tailscale\.exe funnel status --json' { param($c) New-ExtResult -Stdout $script:FunnelJson }
}

# ---- small builders --------------------------------------------------------------------
function New-TestSettings {
    param([string]$State = 'installed', [string]$Vhd = '', [string[]]$RootPaths = @())
    if (-not $Vhd) { $Vhd = Join-Path $script:TestDir 'vhd' }
    $s = New-Settings -VhdDir $Vhd
    Set-SettingProp $s 'state' $State
    $roots = @()
    $n = 1
    foreach ($p in $RootPaths) { $roots += [pscustomobject][ordered]@{ n = $n; windows = $p; linux = ('/mnt/cognita-roots/' + $n) }; $n++ }
    Set-SettingProp $s 'roots' $roots
    Save-Settings $s
    return $s
}
function New-Dir {
    param([string]$Name)
    $p = Join-Path $script:TestDir $Name
    [void](New-Item -ItemType Directory -Path $p -Force)
    return $p
}
function Get-Utf8 { param([int[]]$Codes) return (-join ($Codes | ForEach-Object { [char]$_ })) }
