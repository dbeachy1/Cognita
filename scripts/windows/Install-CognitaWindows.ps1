[CmdletBinding()]
param(
    [ValidateSet('Install','Status','Stop','Start','Repair','Reset-State','Uninstall','UpdateRelease')]
    [string] $Action = 'Install',
    [string] $BundlePath = (Get-Location).Path,
    [string] $ExpectedInstallationId,
    [ValidateSet('Core','Full')][string] $Mode,
    [string[]] $Source = @(),
    [string[]] $SmbSource = @(),
    [PSCredential] $AdminCredential,
    [switch] $ConfirmUninstall,
    [switch] $ConfirmUnregisterDistro
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
# These APIs require modern .NET. Fail before acquiring resources or changing state.
if ($PSVersionTable.PSEdition -ne 'Core' -or $PSVersionTable.PSVersion.Major -lt 7) {
    throw 'Cognita-Windows requires PowerShell 7 or later. Run: pwsh.exe -NoProfile -File .\scripts\windows\Install-CognitaWindows.ps1 -Action Status'
}
$script:Root = 'B:\Cognita-Windows-State'
$script:RecordPath = Join-Path $script:Root 'install.json'
$script:Distro = 'Cognita-Windows'
$script:Project = 'cognita-windows'
$script:Unit = 'cognita-compose.service'
$script:Task = 'Cognita-Windows'
$script:McpPort = 10675
$script:AdminPort = 10676
$script:AdminCredential = $AdminCredential
$script:ProjectionChanged = $false
$script:CurrentMode = $null
$script:AdminUsername = $null
$script:AdminPassword = $null
$script:AdminCsrf = $null
$script:SystemdReady = $false

function Say([string]$Message) { Write-Host "Cognita-Windows: $Message" }
function Wait-OwnedProcess([Diagnostics.Process]$Process,[int]$Timeout) {
    return $Process.WaitForExit($Timeout * 1000)
}
function Stop-OwnedProcess([Diagnostics.Process]$Process) {
    # Kill(true) signals descendants but WaitForExit only waits for the parent.
    # Retain handles to identified descendants so cleanup also waits for them.
    $descendants=[Collections.Generic.List[Diagnostics.Process]]::new()
    $parents=[Collections.Generic.Queue[Diagnostics.Process]]::new();$parents.Enqueue($Process)
    $inspectionError=$null
    try {
        while($parents.Count){
            $parent=$parents.Dequeue()
            $parentStarted=$parent.StartTime.ToUniversalTime()
            foreach($row in @(Get-CimInstance -ClassName Win32_Process -Filter "ParentProcessId = $($parent.Id)" -OperationTimeoutSec 10)){
                $child=$null
                try {
                    $child=[Diagnostics.Process]::GetProcessById([int]$row.ProcessId)
                    $created=$child.StartTime.ToUniversalTime();$path=$child.MainModule.FileName
                    if($created -lt $parentStarted -or [Math]::Abs(($created-$row.CreationDate.ToUniversalTime()).TotalMilliseconds) -gt 1 -or $path -ine $row.ExecutablePath){$child.Dispose();continue}
                    $descendants.Add($child);$parents.Enqueue($child)
                } catch [ArgumentException] {if($child){$child.Dispose()}} # Child already exited.
                catch {if($child){$child.Dispose()};throw}
            }
        }
    } catch {$inspectionError=$_}
    finally {
        try {
            if(-not $Process.HasExited){$Process.Kill($true)}
            # The parent may already have exited while a descendant held a stream open.
            foreach($child in $descendants){if(-not $child.HasExited){$child.Kill($true)}}
            if(-not $Process.WaitForExit(10000)){throw 'Owned command parent did not exit after termination.'}
            foreach($child in $descendants){
                if(-not $child.WaitForExit(10000)){throw "Owned command descendant PID $($child.Id) did not exit after termination."}
            }
            if($inspectionError){throw 'Owned command stopped, but descendant identity inspection failed; cleanup could not be fully verified.'}
        } finally {foreach($child in $descendants){$child.Dispose()}}
    }
}
function Invoke-OwnedProcess([string]$Exe,[string[]]$CommandArguments,[int]$Timeout,[AllowNull()][string]$InputText=$null,[scriptblock]$OnInterrupted=$null,[switch]$AwaitBoundedCompletion,[scriptblock]$OnStarted=$null) {
    if($Timeout -lt 1 -or $Timeout -gt 86400){throw 'Command timeout must be between 1 and 86400 seconds.'}
    # Get-Command can return multiple PATH matches (System32 and WindowsApps WSL).
    # Native invocation resolves the first match; retain that same single authority.
    $resolved=Get-Command $Exe -CommandType Application -ErrorAction Stop|Select-Object -First 1
    $start=[Diagnostics.ProcessStartInfo]::new([string]$resolved.Source)
    $start.UseShellExecute=$false;$start.CreateNoWindow=$true
    $start.RedirectStandardOutput=$true;$start.RedirectStandardError=$true;$start.RedirectStandardInput=$true
    foreach($argument in $CommandArguments){$start.ArgumentList.Add($argument)}
    $process=[Diagnostics.Process]::new();$process.StartInfo=$start;$started=$false;$complete=$false
    try {
        if(-not $process.Start()){throw "Could not start owned command: $([IO.Path]::GetFileName($Exe))."}
        $started=$true
        if($OnStarted){& $OnStarted}
        $null=$process.StartTime
        $watch=[Diagnostics.Stopwatch]::StartNew()
        $stdoutTask=$process.StandardOutput.ReadToEndAsync();$stderrTask=$process.StandardError.ReadToEndAsync()
        if($PSBoundParameters.ContainsKey('InputText') -and $null -ne $InputText){
            # Linux read -r removes LF only. StreamWriter's Windows CRLF default
            # would leave a CR in the credential/key and invalidate authentication.
            $process.StandardInput.NewLine="`n"
            $process.StandardInput.AutoFlush=$true
            $writeTask=$process.StandardInput.WriteLineAsync($InputText)
            if(-not $writeTask.Wait($Timeout*1000)){throw "Command stdin timed out after $Timeout seconds: $([IO.Path]::GetFileName($Exe))."}
            $null=$writeTask.GetAwaiter().GetResult()
        }
        $process.StandardInput.Close()
        $remainingSeconds=[Math]::Max(1,[int][Math]::Ceiling($Timeout-($watch.ElapsedMilliseconds/1000)))
        if(-not (Wait-OwnedProcess $process $remainingSeconds)){throw "Command timed out after $Timeout seconds: $([IO.Path]::GetFileName($Exe))."}
        $remaining=[Math]::Max(0,($Timeout*1000)-[int]$watch.ElapsedMilliseconds)
        if(-not [Threading.Tasks.Task]::WhenAll([Threading.Tasks.Task[]]@($stdoutTask,$stderrTask)).Wait($remaining)){throw "Command output timed out after $Timeout seconds: $([IO.Path]::GetFileName($Exe))."}
        $stdout=$stdoutTask.GetAwaiter().GetResult();$stderr=$stderrTask.GetAwaiter().GetResult()
        $complete=$true
        return @{exit_code=$process.ExitCode;stdout=$stdout;stderr=$stderr}
    } finally {
        try {
            if($started -and -not $complete){
                # Killing the Windows client cannot establish Linux absence. Keep
                # it alive until the manager-owned operation or short deadline settles.
                try {
                    if($OnInterrupted){& $OnInterrupted}
                    elseif($AwaitBoundedCompletion -and -not $process.WaitForExit([Math]::Max(0,($Timeout*1000)-[int]$watch.ElapsedMilliseconds))){
                        throw 'Bounded Linux primitive did not settle; Linux cleanup could not be verified.'
                    }
                } finally {Stop-OwnedProcess $process}
            }
        }
        finally {$process.Dispose()}
    }
}
function Get-OwnedWslLaunchScript {
    # systemd owns the work. The short client records submission settlement;
    # a cancellation marker also blocks registration that arrives late.
    return @'
set -eu
root=$1; unit=$2; deadline=$3; shift 3
umask 077
mkdir -p -- "$root"
trap 'printf settled >"$root/settled"' EXIT
if test -e "$root/cancel"; then printf skipped >"$root/not-submitted"; exit 125; fi
systemd-run --quiet --no-ask-password --unit="$unit" --description='Cognita Windows bounded operation' --collect --wait --pipe --service-type=exec --expand-environment=no --property="RuntimeMaxSec=${deadline}s" --property=TimeoutStartSec=30s --property=TimeoutStopSec=5s --property=KillMode=control-group --property=SendSIGKILL=yes --property=DefaultDependencies=no -- bash -c '
set -eu
root=$1; shift
sed -n "s/^0:://p" /proc/self/cgroup >"$root/cgroup"
printf registered >"$root/registered"
test ! -e "$root/cancel" || exit 125
exec "$@"
' cognita-owned-exec "$root" "$@"
'@
}
function Get-OwnedWslCleanupScript {
    return @'
set -eu
root=$1; unit=$2
umask 077
mkdir -p -- "$root"
printf cancel >"$root/cancel"
# Repeat an exact-unit stop until the submitting client settles. This covers
# cancellation before registration and between the ExecStart guard and exec.
end=$((SECONDS+45))
while :; do
    load=$(systemctl show --property=LoadState --value "$unit" 2>/dev/null) || test "$load" = not-found
    if test "$load" != not-found; then
        # Manager publication also proves registration if cancellation stops the
        # unit before the guarded ExecStart can acknowledge it.
        printf observed >"$root/registered"
        timeout --kill-after=2s 12s systemctl stop "$unit"
    fi
    if test -e "$root/settled"; then break; fi
    if test "$SECONDS" -ge "$end"; then echo 'Owned Linux launch did not settle' >&2; exit 1; fi
    sleep 0.05
done
# A failed submission without ExecStart acknowledgement does not prove that a
# delayed registration is absent. Retain the cancellation guard and fail closed.
test -e "$root/registered" || test -e "$root/not-submitted" || { echo 'Owned Linux registration is unverified' >&2; exit 1; }
load=$(systemctl show --property=LoadState --value "$unit" 2>/dev/null) || test "$load" = not-found
if test "$load" != not-found; then
    timeout --kill-after=2s 12s systemctl stop "$unit"
    state=$(systemctl show --property=ActiveState --value "$unit")
    case "$state" in inactive|failed) ;; *) echo 'Owned Linux unit remains active' >&2; exit 1;; esac
fi
if test -f "$root/cgroup"; then
    group=$(cat "$root/cgroup")
    case "$group" in /*) ;; *) echo 'Invalid owned cgroup identity' >&2; exit 1;; esac
    if test -e "/sys/fs/cgroup$group/cgroup.procs" && test -n "$(cat "/sys/fs/cgroup$group/cgroup.procs")"; then
        echo 'Owned Linux cgroup remains populated' >&2; exit 1
    fi
fi
rm -f -- "$root/cancel" "$root/registered" "$root/not-submitted" "$root/settled" "$root/cgroup"
rmdir -- "$root"
test ! -e "$root"
'@
}
function Remove-OwnedLinuxCommand($Call) {
    if(-not $Call.UnitName -or -not $Call.LaunchSubmitted -or $Call.CleanupAttempted){return}
    $Call.CleanupAttempted=$true
    if($Call.UnitName -notmatch '^cognita-windows-command-[0-9a-f]{32}\.service$' -or $Call.OwnerRoot -cne ('/run/'+$Call.UnitName.Replace('.service',''))){throw 'Invalid owned Linux command identity.'}
    $cleanup=(Get-OwnedWslCleanupScript).Replace("`r`n","`n")
    $arguments=@('-d',$script:Distro,'-u','root','--exec','timeout','--signal=TERM','--kill-after=5s','65s','bash','-lc',$cleanup,'cognita-owned-cleanup',$Call.OwnerRoot,$Call.UnitName)
    $result=Invoke-OwnedProcess 'wsl.exe' $arguments 75 -AwaitBoundedCompletion
    if($result.exit_code -ne 0){throw "Owned Linux command cleanup is unverified: $($Call.UnitName), guard $($Call.OwnerRoot). Ownership retained; inspect this exact unit before retrying."}
}
function Get-BoundedWslArguments([string[]]$CommandArguments,[int]$Timeout) {
    $separator=[Array]::IndexOf($CommandArguments,'--exec')
    if($separator -lt 0){$separator=[Array]::IndexOf($CommandArguments,'--')}
    $distroIndex=[Array]::IndexOf($CommandArguments,'-d')
    $containerName=$null;$unitName=$null;$ownerRoot=$null;$bounded=@($CommandArguments);$linuxBound=$false
    if($separator -ge 0 -and $separator -lt ($CommandArguments.Count-1) -and $distroIndex -ge 0 -and $CommandArguments[$distroIndex+1] -ceq $script:Distro){
        $command=@($CommandArguments[($separator+1)..($CommandArguments.Count-1)])
        # Git may materialize this PowerShell file with CRLF. Linux shell
        # program text must use LF; only normalize the shell's script operand,
        # never paths, positional arguments, Python code, or secret stdin.
        if($command.Count -ge 3 -and $command[0] -cin @('bash','sh','/bin/bash','/bin/sh') -and $command[1] -cin @('-c','-lc')){
            $command[2]=$command[2].Replace("`r`n","`n")
        }
        if($command.Count -gt 2 -and $command[0] -ceq 'docker' -and $command[1] -ceq 'compose'){
            $runIndex=[Array]::IndexOf($command,'run')
            if($runIndex -ge 2){
                $containerName='cognita-windows-operation-'+[guid]::NewGuid().ToString('N')
                $command=@($command[0..$runIndex])+@('--name',$containerName)+@($command[($runIndex+1)..($command.Count-1)])
            }
        }
        if($Timeout -gt 300){
            if(-not $script:SystemdReady){
                Run 'wsl.exe' @('-d',$script:Distro,'-u','root','--exec','bash','-lc','test "$(cat /proc/1/comm)" = systemd') 30|Out-Null
                $script:SystemdReady=$true
            }
            $unitName='cognita-windows-command-'+[guid]::NewGuid().ToString('N')+'.service'
            $ownerRoot='/run/'+$unitName.Replace('.service','')
            $launch=(Get-OwnedWslLaunchScript).Replace("`r`n","`n")
            $bounded=@($CommandArguments[0..$separator])+@('bash','-lc',$launch,'cognita-owned-launch',$ownerRoot,$unitName,[string]$Timeout)+$command
        } else {
            # Existing metadata/configuration/identity calls take 30-120s, service
            # control 180s, and fixture provision 300s. Wait their original Linux
            # deadline on cancellation (at most 315s including host transport),
            # preserving WSL interop. Bootstrap/image loads, Toolbox, Compose
            # lifecycle, reset, and secret runners use the manager-owned path.
            $bounded=@($CommandArguments[0..$separator])+@('timeout','--signal=TERM','--kill-after=5s',"$($Timeout)s")+$command
        }
        $linuxBound=$true
    }
    return @{Arguments=$bounded;ContainerName=$containerName;LinuxBound=$linuxBound;UnitName=$unitName;OwnerRoot=$ownerRoot;CleanupAttempted=$false;LaunchSubmitted=$false}
}
function Remove-OwnedCommandContainer([string]$Name) {
    if($Name -notmatch '^cognita-windows-operation-[0-9a-f]{32}$'){throw 'Invalid owned command container identity.'}
    $cleanup='set -eu; docker info >/dev/null; if docker container inspect "$1" >/dev/null 2>&1; then docker container rm --force "$1" >/dev/null; fi; if docker container inspect "$1" >/dev/null 2>&1; then echo "Owned operation container remains" >&2; exit 1; fi'
    try {Wsl @('bash','-lc',$cleanup,'cognita-command-cleanup',$Name) 60|Out-Null}
    catch {throw "Owned operation container cleanup could not be verified: $Name. Inspect Cognita-Windows Docker before retrying."}
}
function Run([string]$Exe,[string[]]$CommandArguments,[int]$Timeout=1800) {
    $isWsl=([IO.Path]::GetFileName($Exe) -ieq 'wsl.exe')
    if($isWsl){
        # --exec preserves Linux argv; the legacy -- path reparses command text.
        $separator=[Array]::IndexOf($CommandArguments,'--')
        if($separator -ge 0){$CommandArguments=@($CommandArguments);$CommandArguments[$separator]='--exec'}
    }
    $call=if($isWsl){Get-BoundedWslArguments $CommandArguments $Timeout}else{@{Arguments=$CommandArguments;ContainerName=$null;LinuxBound=$false;UnitName=$null}}
    # Leave the Linux deadline time to terminate its group before the host deadline.
    $hostTimeout=if($call.LinuxBound){$Timeout+15}else{$Timeout}
    try {
        if($call.UnitName){$result=Invoke-OwnedProcess $Exe $call.Arguments $hostTimeout -OnInterrupted {Remove-OwnedLinuxCommand $call} -OnStarted {$call.LaunchSubmitted=$true}}
        elseif($call.LinuxBound){$result=Invoke-OwnedProcess $Exe $call.Arguments $hostTimeout -AwaitBoundedCompletion}
        else {$result=Invoke-OwnedProcess $Exe $call.Arguments $hostTimeout}
    } finally {
        try {if($call.UnitName){Remove-OwnedLinuxCommand $call}}
        finally {if($call.ContainerName){Remove-OwnedCommandContainer $call.ContainerName}}
    }
    # Relay only code-owned bounded phase records even when a later rollback
    # replaces the original exception. Never relay arbitrary child output here.
    foreach($match in [regex]::Matches(($result.stdout+$result.stderr),'(?m)^COGNITA_UPDATE_PHASE ((?:(?:apply|rollback|repair|current)-(?:load-images|toolbox|start-pair|verify)|preflight-(?:owner|prior-bundle|candidate-bundle|source-state|launcher|paths|begin))) (started|passed|failed)\r?$')){
        [Console]::Error.WriteLine($match.Value.TrimEnd())
    }
    if ($result.exit_code -ne 0) {
        $safe = $result.stdout + $result.stderr
        $safe = [regex]::Replace($safe,'(?i)(password|token|secret|api[_-]?key)(\s*[=:]\s*)\S+','$1$2[redacted]')
        throw "Command failed ($($result.exit_code)): $([IO.Path]::GetFileName($Exe))`n$safe"
    }
    return @(($result.stdout+$result.stderr) -split '\r?\n' | Where-Object {$_ -ne ''})
}
function Wsl([string[]]$CommandArguments,[int]$Timeout=1800) { Run 'wsl.exe' (@('-d',$script:Distro,'-u','root','--exec')+$CommandArguments) $Timeout }
function DistroExists([string]$Name) {
    $names = @(Run 'wsl.exe' @('--list','--quiet') 30 | ForEach-Object {
        ([string]$_ -replace "`0",'').Trim()
    })
    return $names -contains $Name
}
function Read-OptionalLastLine([AllowNull()][AllowEmptyCollection()][object[]]$Lines) {
    if($null -eq $Lines -or $Lines.Count -eq 0){return [string]::Empty}
    return ([string]$Lines[-1]).Trim()
}
function Read-RequiredLastLine([AllowNull()][AllowEmptyCollection()][object[]]$Lines,[string]$Operation) {
    if($null -eq $Lines -or $Lines.Count -eq 0){throw "$Operation returned no output."}
    $value=([string]$Lines[-1]).Trim()
    if([string]::IsNullOrWhiteSpace($value)){throw "$Operation returned empty output."}
    return $value
}
function Convert-WindowsPathToWsl([string]$Path,[string]$Operation) {
    # WSL parses trailing command arguments itself and strips Windows backslashes.
    # Forward slashes are accepted by wslpath and leave the recorded Windows path intact.
    $wslInput=$Path.Replace('\','/')
    try {$output=@(Run 'wsl.exe' @('-d',$script:Distro,'-u','root','--exec','wslpath','-a',$wslInput) 30)}
    catch {throw "$Operation failed while running wslpath."}
    return Read-RequiredLastLine -Lines $output -Operation $Operation
}

function Verify-AcceptedArchiveChecksum([string]$ChecksumPath,[string]$ArchiveName,[string]$ArchivePath) {
    $checksumMatches = [Collections.Generic.List[string]]::new()
    foreach($entry in [IO.File]::ReadAllLines($ChecksumPath)) {
        if($entry -match '^([0-9a-fA-F]{64})  ([^\r\n]+)$' -and $Matches[2] -ceq $ArchiveName) {
            $checksumMatches.Add($Matches[1].ToUpperInvariant())
        }
    }
    if($checksumMatches.Count -ne 1){throw 'Archive must have exactly one accepted checksum entry.'}
    if(-not(Test-Path -LiteralPath $ArchivePath -PathType Leaf)){throw 'Accepted archive is missing.'}
    $actual=(Get-FileHash -Algorithm SHA256 -LiteralPath $ArchivePath).Hash.ToUpperInvariant()
    if($actual -cne $checksumMatches[0]){throw 'Archive does not match its accepted checksum.'}
    return $actual
}

function Get-PostgresComposeImageReference([string]$ComposePath) {
    if(-not(Test-Path -LiteralPath $ComposePath -PathType Leaf)){throw 'Base Compose file is missing from the Windows bundle.'}
    $lines=[IO.File]::ReadAllLines($ComposePath)
    $inServices=$false;$servicesSeen=$false;$inPostgres=$false;$postgresSeen=$false;$references=[Collections.Generic.List[string]]::new()
    foreach($line in $lines){
        if($line -match '^services:\s*(?:#.*)?$'){if($servicesSeen){throw 'Base Compose file has duplicate services sections.'};$inServices=$true;$servicesSeen=$true;continue}
        if($inServices -and $line -match '^[^\s#].*:\s*(?:#.*)?$'){$inServices=$false;$inPostgres=$false}
        if(-not $inServices){continue}
        if($line -match '^  postgres:\s*(?:#.*)?$'){if($postgresSeen){throw 'Base Compose file has duplicate postgres services.'};$inPostgres=$true;$postgresSeen=$true;continue}
        if($inPostgres -and $line -match '^  [A-Za-z0-9_-]+:\s*(?:#.*)?$'){$inPostgres=$false}
        if($inPostgres -and $line -match '^    image:\s*(\S+)\s*(?:#.*)?$'){$references.Add($Matches[1])}
    }
    if($references.Count -ne 1){throw 'Base Compose postgres service must declare exactly one image.'}
    if($references[0] -notmatch '^[A-Za-z0-9][A-Za-z0-9._:/-]*:[A-Za-z0-9_][A-Za-z0-9_.-]*@sha256:[0-9a-fA-F]{64}$'){
        throw 'Base Compose postgres image must use exact repository:tag@sha256:<64 hex> form.'
    }
    return $references[0]
}

function Get-PostgresImageParts([string]$Reference) {
    if($Reference -notmatch '^(?<repository>[A-Za-z0-9][A-Za-z0-9._:/-]*):(?<tag>[A-Za-z0-9_][A-Za-z0-9_.-]*)@sha256:(?<digest>[0-9a-fA-F]{64})$'){
        throw 'PostgreSQL image reference is not an exact digest-pinned repository:tag.'
    }
    $repository=$Matches.repository;$digest=$Matches.digest.ToLowerInvariant()
    return @{Repository=$repository;Digest=$digest;NormalizedRepoDigest="$repository@sha256:$digest";TransportTag="$($repository):cognita-transport-$digest"}
}

function Verify-PostgresBundleImage($Bundle) {
    $reference=Get-PostgresComposeImageReference (Join-Path $Bundle.Root 'compose.yaml')
    if($Bundle.Metadata.image_ref_postgres -cne $reference){throw 'Bundle PostgreSQL reference differs from the base Compose postgres image.'}
    $parts=Get-PostgresImageParts $reference
    $expectedId=[string]$Bundle.Metadata.image_postgres
    try{$namedId=Read-RequiredLastLine -Lines (Wsl @('docker','image','inspect','--format','{{.Id}}',$reference) 120) -Operation 'PostgreSQL digest image inspection'}
    catch{throw 'PostgreSQL digest reference could not be resolved from the closed CPU archive.'}
    if($namedId -cne $expectedId){throw 'PostgreSQL digest reference resolves to an image ID different from release.txt.'}
    try{$repoJson=Read-RequiredLastLine -Lines (Wsl @('docker','image','inspect','--format','{{json .RepoDigests}}',$reference) 120) -Operation 'PostgreSQL repository digest inspection';$repoDigests=@($repoJson|ConvertFrom-Json)}
    catch{throw 'PostgreSQL repository digest association could not be inspected.'}
    if($repoDigests -cnotcontains $parts.NormalizedRepoDigest){throw 'PostgreSQL image lacks the normalized repository@digest association required by base Compose.'}
    try{$transportId=Read-RequiredLastLine -Lines (Wsl @('docker','image','inspect','--format','{{.Id}}',$parts.TransportTag) 120) -Operation 'PostgreSQL transport tag inspection'}
    catch{throw 'Derived PostgreSQL transport tag is missing from the closed CPU archive.'}
    if($transportId -cne $expectedId){throw 'Derived PostgreSQL transport tag resolves to an image ID different from release.txt.'}
}

function Verify-Bundle([string]$Path) {
    $root = (Resolve-Path -LiteralPath $Path).Path
    $sumPath = Join-Path $root 'SHA256SUMS'
    if (-not (Test-Path -LiteralPath $sumPath -PathType Leaf)) { throw 'Bundle SHA256SUMS is missing.' }
    $expected = @{}
    foreach ($line in [IO.File]::ReadAllLines($sumPath)) {
        if ($line -notmatch '^([0-9a-fA-F]{64})  (.+)$') { throw 'Invalid SHA256SUMS entry.' }
        $name = $Matches[2].Replace('/','\')
        if ([IO.Path]::IsPathRooted($name) -or $name.Split('\') -contains '..' -or $expected.ContainsKey($name)) { throw 'Unsafe or duplicate bundle path.' }
        $expected[$name] = $Matches[1].ToUpperInvariant()
        $file = Join-Path $root $name
        if (-not (Test-Path -LiteralPath $file -PathType Leaf) -or (Get-FileHash -Algorithm SHA256 -LiteralPath $file).Hash -ne $expected[$name]) { throw "Bundle hash mismatch or missing file: $name" }
    }
    $actual = @(Get-ChildItem -LiteralPath $root -File -Recurse | Where-Object Name -ne 'SHA256SUMS' | ForEach-Object { [IO.Path]::GetRelativePath($root,$_.FullName).Replace('/','\') })
    $expectedNames=(@($expected.Keys | Sort-Object) -join "`n")
    $actualNames=(@($actual | Sort-Object) -join "`n")
    if ($expectedNames -cne $actualNames) { throw 'Bundle contains unlisted files or omits listed files.' }
    $meta = @{}
    foreach ($line in [IO.File]::ReadAllLines((Join-Path $root 'release.txt'))) { $p=$line -split ':',2; if ($p.Count -eq 2) { $key=$p[0].Trim();if($meta.ContainsKey($key)){throw "release.txt repeats metadata key $key."};$meta[$key]=$p[1].Trim() } }
    foreach ($key in @('version','commit','image_ref_cognita_cpu','image_cognita_cpu','image_ref_postgres','image_postgres','bundle_mode')) { if (-not $meta[$key]) { throw "release.txt is missing $key." } }
    if($meta.version -notmatch '^\d+\.\d+\.\d+(?:-[A-Za-z0-9.-]+)?$' -or $meta.commit -notmatch '^[0-9a-f]{40}$' -or $meta.image_cognita_cpu -notmatch '^sha256:[0-9a-f]{64}$' -or $meta.image_ref_cognita_cpu -notmatch '^[A-Za-z0-9._:/-]+$' -or $meta.image_postgres -notmatch '^sha256:[0-9a-f]{64}$'){throw 'release.txt has an invalid version, commit, or image identity.'}
    $composePostgres=Get-PostgresComposeImageReference (Join-Path $root 'compose.yaml')
    if($meta.image_ref_postgres -cne $composePostgres){throw 'Bundle PostgreSQL reference differs from the base Compose postgres image.'}
    if ($meta.bundle_mode -notin @('core','full')) { throw 'Bundle mode is invalid.' }
    if ($Mode -and $meta.bundle_mode -ne $Mode.ToLowerInvariant()) { throw 'Requested mode differs from the bundle mode.' }
    if ($meta.bundle_mode -eq 'full' -and (-not $meta.image_ref_workspace_runtime -or -not $meta.image_workspace_runtime)) { throw 'Full bundle lacks an accepted Workspace image identity.' }
    if($meta.bundle_mode -eq 'full' -and ($meta.image_workspace_runtime -notmatch '^sha256:[0-9a-f]{64}$' -or $meta.image_ref_workspace_runtime -notmatch '^[A-Za-z0-9._:/-]+$' -or $meta.toolbox_version -notmatch '^\d+\.\d+\.\d+$')){throw 'Full bundle has an invalid Workspace or Toolbox identity.'}
    return @{ Root=$root; Metadata=$meta; Sum=(Get-FileHash -Algorithm SHA256 -LiteralPath $sumPath).Hash.ToLowerInvariant() }
}

function Secure-Path([string]$Path,[switch]$Directory,[switch]$StateRootTraversal) {
    if($StateRootTraversal -and (-not $Directory -or $Path -cne $script:Root)){throw 'The DrvFS traversal grant is valid only for the fixed state root.'}
    $who=[Security.Principal.WindowsIdentity]::GetCurrent().Name
    $acl=if($Directory){[Security.AccessControl.DirectorySecurity]::new()}else{[Security.AccessControl.FileSecurity]::new()}
    $acl.SetAccessRuleProtection($true,$false); $acl.SetOwner([Security.Principal.NTAccount]$who)
    $inherit=if($Directory){[Security.AccessControl.InheritanceFlags]'ContainerInherit,ObjectInherit'}else{[Security.AccessControl.InheritanceFlags]::None}
    foreach($account in @($who,'BUILTIN\Administrators')) { $acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new($account,'FullControl',$inherit,[Security.AccessControl.PropagationFlags]::None,[Security.AccessControl.AccessControlType]::Allow)) }
    if($StateRootTraversal) { $acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new('BUILTIN\Users','ReadAndExecute',[Security.AccessControl.InheritanceFlags]::None,[Security.AccessControl.PropagationFlags]::None,[Security.AccessControl.AccessControlType]::Allow)) }
    if($Directory){
        $current=Get-Acl -LiteralPath $Path
        $sid=[Security.Principal.WindowsIdentity]::GetCurrent().User.Value
        $currentOwner=([Security.Principal.NTAccount]$current.Owner).Translate([Security.Principal.SecurityIdentifier]).Value
        $ruleKey={param($rule)($rule.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value,[int]$rule.FileSystemRights,[int]$rule.InheritanceFlags,[int]$rule.PropagationFlags,[int]$rule.AccessControlType)-join ':'}
        $currentRules=@($current.GetAccessRules($true,$false,[Security.Principal.NTAccount])|ForEach-Object {&$ruleKey $_}|Sort-Object)
        $desiredRules=@($acl.GetAccessRules($true,$false,[Security.Principal.NTAccount])|ForEach-Object {&$ruleKey $_}|Sort-Object)
        if($current.AreAccessRulesProtected -and $currentOwner -ceq $sid -and ($currentRules -join ',') -ceq ($desiredRules -join ',')){return}
        Set-Acl -LiteralPath $Path -AclObject $acl
    } else {[IO.FileSystemAclExtensions]::SetAccessControl([IO.FileInfo]::new($Path),$acl)}
}
function Save-Record($Record) {
    $null=New-Item -ItemType Directory -Path $script:Root -Force; Secure-Path $script:Root -Directory -StateRootTraversal
    $temp=Join-Path $script:Root ('.install-'+[guid]::NewGuid().ToString('N')+'.tmp')
    try {
        [IO.File]::WriteAllText($temp,($Record|ConvertTo-Json -Depth 12)+"`n",[Text.UTF8Encoding]::new($false)); Secure-Path $temp
        if(Test-Path -LiteralPath $script:RecordPath){$prev=$script:RecordPath+'.previous';if(Test-Path -LiteralPath $prev){Remove-Item -LiteralPath $prev -Force};[IO.File]::Replace($temp,$script:RecordPath,$prev,$true);Secure-Path $prev;Secure-Path $script:RecordPath}
        else{[IO.File]::Move($temp,$script:RecordPath);Secure-Path $script:RecordPath}
    } finally { if(Test-Path -LiteralPath $temp){Remove-Item -LiteralPath $temp -Force} }
}
function Read-Record {
    if(-not(Test-Path -LiteralPath $script:RecordPath -PathType Leaf)){throw 'Cognita-Windows is not installed.'}
    $r=Get-Content -Raw -LiteralPath $script:RecordPath|ConvertFrom-Json
    $fixed=($r.schema -eq 1 -and $r.distro -ceq $script:Distro -and $r.compose_project -ceq $script:Project -and $r.unit -ceq $script:Unit -and $r.task -ceq $script:Task -and $r.source_root -ceq '/srv/cognita/sources' -and $r.config_root -ceq '/srv/cognita/config' -and $r.postgres_root -ceq '/srv/cognita/postgres' -and $r.model_cache_root -ceq '/srv/cognita/models' -and $r.transfer_root -ceq '/srv/cognita/transfers' -and $r.workspace_root -ceq '/srv/cognita/workspaces')
    $validPorts=($r.mcp_port -is [long] -or $r.mcp_port -is [int]) -and ($r.admin_port -is [long] -or $r.admin_port -is [int]) -and $r.mcp_port -ge 1 -and $r.mcp_port -le 65535 -and $r.admin_port -ge 1 -and $r.admin_port -le 65535 -and $r.mcp_port -ne $r.admin_port
    if(-not $fixed -or -not $validPorts -or $r.installation_id -notmatch '^[0-9a-f-]{36}$'){throw 'Install record identity or fixed resource mapping is invalid.'}
    $script:McpPort=[int]$r.mcp_port;$script:AdminPort=[int]$r.admin_port
    return $r
}
function Verify-Owner {
    $r=Read-Record
    if(-not(DistroExists $script:Distro)){throw 'Owned WSL distro is missing; use Repair from its recorded bundle.'}
    $marker=Read-RequiredLastLine -Lines (Wsl @('cat','/etc/cognita-install-id') 30) -Operation 'Read installation ownership marker'
    if($marker -cne $r.installation_id){throw 'Distro marker does not match install.json; lifecycle operation refused.'};return $r
}

function Get-Sources {
    $rows=[Collections.Generic.List[object]]::new();$names=[Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
    foreach($entry in $Source){$alias,$path=$entry -split '=',2;if(-not $path -or $alias -notmatch '^[a-z0-9][a-z0-9_-]{0,31}$' -or $alias -ieq 'cognita-self-test' -or -not $names.Add($alias)){throw '-Source values must be unique alias=existing-directory entries; aliases are lowercase and cannot be cognita-self-test.'};$full=(Resolve-Path -LiteralPath $path).Path;if(-not(Test-Path -LiteralPath $full -PathType Container)){throw "Source alias $alias is not a directory."};$rows.Add([pscustomobject]@{alias=$alias;source_kind='ntfs';locator=$full;canonical_identity=$null;last_runtime_identity=$null})}
    foreach($entry in $SmbSource){$alias,$path=$entry -split '=',2;if(-not $path -or $alias -notmatch '^[a-z0-9][a-z0-9_-]{0,31}$' -or $alias -ieq 'cognita-self-test' -or -not $names.Add($alias) -or $path -notmatch '^\\\\[^\\]+\\[^\\]+'){throw '-SmbSource values must be unique alias=\\server\share\path entries.'};if(-not(Test-Path -LiteralPath $path -PathType Container)){throw "SMB source is unavailable during initial install: alias $alias."};$rows.Add([pscustomobject]@{alias=$alias;source_kind='smb';locator=$path;canonical_identity=$null;last_runtime_identity=$null})}
    $rows.Add([pscustomobject]@{alias='cognita-self-test';source_kind='installation_ext4';locator='/srv/cognita/sources/cognita-self-test';canonical_identity=$null;last_runtime_identity=$null})
    return @($rows)
}
function New-Record($Bundle,$Sources) {
    $m=$Bundle.Metadata
    return [ordered]@{schema=1;installation_id=[guid]::NewGuid().ToString();distro=$script:Distro;compose_project=$script:Project;unit=$script:Unit;task=$script:Task;source_root='/srv/cognita/sources';config_root='/srv/cognita/config';postgres_root='/srv/cognita/postgres';model_cache_root='/srv/cognita/models';transfer_root='/srv/cognita/transfers';workspace_root='/srv/cognita/workspaces';mcp_port=$script:McpPort;admin_port=$script:AdminPort;bundle_path=$Bundle.Root;bundle_sha256=$Bundle.Sum;version=$m.version;commit=$m.commit;mode=$m.bundle_mode;image_ref_cognita_cpu=$m.image_ref_cognita_cpu;image_cognita_cpu=$m.image_cognita_cpu;image_ref_postgres=$m.image_ref_postgres;image_postgres=$m.image_postgres;image_ref_workspace_runtime=$m.image_ref_workspace_runtime;image_workspace_runtime=$m.image_workspace_runtime;sources=@($Sources);created_utc=[DateTime]::UtcNow.ToString('o');updated_utc=[DateTime]::UtcNow.ToString('o')}
}

function Assert-Available($Sources) {
    if((DistroExists $script:Distro) -and -not(Test-Path -LiteralPath $script:RecordPath)){throw 'The fixed WSL distro name is occupied without an owner record; refusing adoption.'}
    $inUse=Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue|Where-Object LocalPort -in @($script:McpPort,$script:AdminPort)
    if($inUse){throw "A fixed Cognita-Windows port is already occupied: $($inUse[0].LocalPort)."}
    if((Get-ScheduledTask -TaskName $script:Task -ErrorAction SilentlyContinue) -and -not(Test-Path -LiteralPath $script:RecordPath)){throw 'A fixed Scheduled Task name is occupied without an owner record.'}
}
function Install-FirstRun($Bundle,$Record) {
    $createdNow=$false
    if(-not(DistroExists $script:Distro)){
        Say 'creating dedicated Ubuntu 24.04 WSL2 distribution'
        Run 'wsl.exe' @('--install','--distribution','Ubuntu-24.04','--name',$script:Distro,'--no-launch') 1800|Out-Null
        $createdNow=$true
    }
    $marker=@(Run 'wsl.exe' @('-d',$script:Distro,'-u','root','--exec','bash','-lc','if test -f /etc/cognita-install-id; then cat /etc/cognita-install-id; fi') 30)
    $markerValue=Read-OptionalLastLine -Lines $marker
    if($markerValue -and $markerValue -cne $Record.installation_id){throw 'Distro ownership marker mismatch; refusing to adopt or remove it.'}
    if(-not $markerValue){
        if(-not $createdNow){$answer=Read-Host "Unmarked Cognita-Windows distro will be removed and recreated. Type UNREGISTER $script:Distro";if($answer -cne "UNREGISTER $script:Distro"){throw 'Unmarked distro retained. Run Status, then Install again after review.'};Run 'wsl.exe' @('--unregister',$script:Distro) 1800|Out-Null;Run 'wsl.exe' @('--install','--distribution','Ubuntu-24.04','--name',$script:Distro,'--no-launch') 1800|Out-Null;$createdNow=$true}
        # Commit the owner-record identity invalidation before publishing the Linux
        # marker. A retry seeing that marker must never retain the destroyed ext4 ID.
        if($createdNow){Reset-RecreatedSyntheticIdentity $Record}
        $writeMarker='set -euo pipefail; umask 077; temporary=$(mktemp /etc/.cognita-install-id.XXXXXX); trap ''rm -f -- "$temporary"'' EXIT; printf ''%s\n'' ''__INSTALL_ID__'' >"$temporary"; chmod 600 "$temporary"; mv -T -- "$temporary" /etc/cognita-install-id'
        Run 'wsl.exe' @('-d',$script:Distro,'-u','root','--exec','bash','-lc',$writeMarker.Replace('__INSTALL_ID__',$Record.installation_id)) 30|Out-Null
    }
    $bundleWsl=Convert-WindowsPathToWsl -Path $Bundle.Root -Operation 'Convert bundle path to WSL'
    $enableSystemd='set -euo pipefail; if ! grep -q ''^systemd=true$'' /etc/wsl.conf 2>/dev/null; then temporary=$(mktemp /etc/.cognita-wsl-conf.XXXXXX); trap ''rm -f -- "$temporary"'' EXIT; printf ''[boot]\nsystemd=true\n'' >"$temporary"; chmod 644 "$temporary"; mv -T -- "$temporary" /etc/wsl.conf; fi'
    Run 'wsl.exe' @('-d',$script:Distro,'-u','root','--exec','bash','-lc',$enableSystemd) 30|Out-Null
    Run 'wsl.exe' @('--terminate',$script:Distro) 60|Out-Null
    Run 'wsl.exe' @('-d',$script:Distro,'-u','root','--exec','bash','-lc','test "$(cat /proc/1/comm)" = systemd') 30|Out-Null
    $script:SystemdReady=$true
    $recordWsl=Convert-WindowsPathToWsl -Path $script:RecordPath -Operation 'Convert install-record path to WSL'
    $scriptText=Get-LinuxBootstrapScript $Bundle $Record $bundleWsl $recordWsl
    $b64=[Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($scriptText))
    $launch=@'
set -euo pipefail
bootstrap=$(mktemp /run/cognita-install.XXXXXX)
trap 'rm -f -- "$bootstrap"' EXIT
printf %s '__BOOTSTRAP_B64__' | base64 -d >"$bootstrap"
bash "$bootstrap"
'@
    Wsl @('bash','-lc',$launch.Replace("`r`n","`n").Replace('__BOOTSTRAP_B64__',$b64)) 3600|Out-Null
    return $createdNow
}

function Get-LinuxBootstrapScript($Bundle,$Record,[string]$BundleWsl,[string]$RecordWsl) {
    $id=$Record.installation_id
    $bundleB64=[Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($BundleWsl))
    $recordB64=[Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($RecordWsl))
    $scriptText=@'
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
# Every replacement is completed beside its destination before publication.
# Only these exact temporary files belong to this invocation; never sweep a tree.
umask 077
owned_temps=()
cleanup() { local path; for path in "${owned_temps[@]}"; do rm -f -- "$path"; done; }
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
assert_regular() { test ! -L "$1" && { test ! -e "$1" || test -f "$1"; } || { echo "Installer file is not a regular file: $1" >&2; exit 1; }; }
atomic_generate() {
  local destination=$1 mode=$2 owner=$3 temporary
  shift 3
  assert_regular "$destination"
  temporary=$(mktemp "$(dirname "$destination")/.cognita-install-$(basename "$destination").XXXXXX")
  owned_temps+=("$temporary")
  "$@" >"$temporary"
  chmod "$mode" "$temporary"
  chown "$owner" "$temporary"
  mv -T -- "$temporary" "$destination"
}
atomic_write() { atomic_generate "$1" "$2" "$3" cat; }
atomic_copy() { atomic_write "$2" "$3" "$4" <"$1"; }
owned_directory() {
  test ! -L "$1" && { test ! -e "$1" || test -d "$1"; } || { echo "Installer directory is not a directory: $1" >&2; exit 1; }
  install -d -o cognita-admin -g cognita-admin -m "$2" "$1"
}
if ! grep -q '^systemd=true$' /etc/wsl.conf 2>/dev/null; then
  printf '[boot]\nsystemd=true\n' | atomic_write /etc/wsl.conf 0644 root:root
fi
test "$(ps -p 1 -o comm= | tr -d ' ')" = systemd || { echo 'The dedicated WSL distro must be restarted to activate systemd'; exit 1; }
systemctl mask docker.service docker.socket 2>/dev/null || true
# Restore any preexisting package-start policy, including when apt fails.
policy=/usr/sbin/policy-rc.d
assert_regular "$policy"
policy_backup=''
if test -e "$policy"; then policy_backup=$(mktemp /usr/sbin/.cognita-install-policy.XXXXXX); owned_temps+=("$policy_backup"); cp -p -- "$policy" "$policy_backup"; fi
restore_policy() { if test -n "$policy_backup"; then mv -T -- "$policy_backup" "$policy"; else rm -f -- "$policy"; fi; cleanup; }
trap restore_policy EXIT
printf '#!/bin/sh\nexit 101\n' | atomic_write "$policy" 0755 root:root
apt-get update
apt-get install -y ca-certificates python3 util-linux openssl
apt-get install -y docker.io=29.1.3-0ubuntu3~24.04.2 docker-compose-v2=2.40.3+ds1-0ubuntu1~24.04.1
if test -n "$policy_backup"; then mv -T -- "$policy_backup" "$policy"; policy_backup=''; else rm -f -- "$policy"; fi
trap cleanup EXIT
getent passwd cognita-admin >/dev/null || useradd --create-home --shell /bin/bash cognita-admin
usermod -aG docker cognita-admin
for directory in /srv/cognita /srv/cognita/config /srv/cognita/postgres /srv/cognita/models /srv/cognita/transfers /srv/cognita/sources; do owned_directory "$directory" 0750; done
if [ '__MODE__' = full ]; then owned_directory /srv/cognita/workspaces 0700; fi
install -d -m 0755 /usr/local/libexec
printf '%s\n' '__INSTALL_ID__' | atomic_write /etc/cognita-install-id 0600 root:root
bundle=$(printf %s '__BUNDLE_B64__' | base64 -d)
record_path=$(printf %s '__RECORD_B64__' | base64 -d)
printf '%s\n' "$record_path" | atomic_write /etc/cognita-install-record-path 0600 root:root
atomic_copy "$bundle/scripts/windows/Prepare-Sources.py" /usr/local/libexec/cognita-prepare-sources 0755 root:root
atomic_copy "$bundle/scripts/windows/cognita-session.sh" /usr/local/libexec/cognita-session 0755 root:root
atomic_copy "$bundle/scripts/windows/Reset-CognitaState.sh" /usr/local/libexec/cognita-reset-state 0755 root:root
atomic_copy "$bundle/compose.yaml" /srv/cognita/compose.yaml 0644 cognita-admin:cognita-admin
atomic_copy "$bundle/compose.cpu.yaml" /srv/cognita/compose.cpu.yaml 0644 cognita-admin:cognita-admin
atomic_copy "$bundle/compose.cpu.__MODE__.images.yaml" /srv/cognita/compose.cpu.images.yaml 0644 cognita-admin:cognita-admin
if [ '__MODE__' = full ]; then atomic_copy "$bundle/compose.workspace.yaml" /srv/cognita/compose.workspace.yaml 0644 cognita-admin:cognita-admin; fi
atomic_write /srv/cognita/compose.windows-projection.yaml 0644 cognita-admin:cognita-admin <<'EOF'
services:
  cognita:
    environment:
      COGNITA_SOURCE_IDENTITIES_FILE: /run/cognita/source-identities.json
    volumes:
      - type: bind
        source: /run/cognita/source-identities.json
        target: /run/cognita/source-identities.json
        read_only: true
EOF
for name in run-selftest.py provision_selftest.py set-admin-credentials.py; do atomic_copy "$bundle/scripts/$name" "/srv/cognita/$name" 0644 cognita-admin:cognita-admin; done
if [ '__MODE__' = full ]; then owned_directory /srv/cognita/workspaces/toolbox-cache 0700; atomic_copy "$bundle/toolbox.tar" "/srv/cognita/workspaces/toolbox-cache/toolbox-__TOOLBOX_VERSION__.tar" 0600 cognita-admin:cognita-admin; fi
if [ '__MODE__' = full ]; then
  marker=/srv/cognita/workspaces/.cognita-12-workspaces.json
  if [ -L "$marker" ]; then echo 'Workspace capacity marker must not be a symlink' >&2; exit 1; fi
  assert_regular "$marker"
  if [ ! -s "$marker" ]; then atomic_generate "$marker" 0600 cognita-admin:cognita-admin python3 -c 'import json,uuid; print(json.dumps({"schema":1,"root_id":str(uuid.uuid4()),"role":"workspaces"},separators=(",",":")))'; fi
  python3 -c 'import json,sys,uuid; p=json.load(open(sys.argv[1])); assert p["schema"] == 1 and p["role"] == "workspaces"; uuid.UUID(p["root_id"])' "$marker" || { echo 'Workspace capacity marker is invalid; it was preserved' >&2; exit 1; }
  chown cognita-admin:cognita-admin "$marker"; chmod 0600 "$marker"
fi
password=/srv/cognita/config/postgres.password; dsn=/srv/cognita/config/postgres.dsn
assert_regular "$password"; assert_regular "$dsn"
# Recover only empty files left by the older direct-write installer. A nonempty
# credential is never rotated implicitly, including when validation fails.
if [ ! -s "$password" ]; then
  if [ -s "$dsn" ]; then echo 'PostgreSQL password is missing but its DSN exists; credentials were preserved' >&2; exit 1; fi
  atomic_generate "$password" 0600 cognita-admin:cognita-admin openssl rand -hex 32
fi
python3 -c 'import pathlib,sys; v=pathlib.Path(sys.argv[1]).read_text().strip(); assert v and len(v)<=4096 and not any(c.isspace() or c in ":/@?#%" for c in v)' "$password" || { echo 'PostgreSQL password is invalid; it was preserved' >&2; exit 1; }
if [ ! -s "$dsn" ]; then printf 'postgresql://cognita:%s@postgres:5432/cognita' "$(cat "$password")" | atomic_write "$dsn" 0600 cognita-admin:cognita-admin; fi
python3 -c 'import pathlib,sys; p=pathlib.Path(sys.argv[1]).read_text().strip(); d=pathlib.Path(sys.argv[2]).read_text().strip(); assert d == "postgresql://cognita:"+p+"@postgres:5432/cognita"' "$password" "$dsn" || { echo 'PostgreSQL DSN differs from its password; both were preserved' >&2; exit 1; }
for name in admin_tls_certfile admin_tls_keyfile; do file="/srv/cognita/config/$name"; assert_regular "$file"; if [ ! -e "$file" ]; then printf '' | atomic_write "$file" 0600 cognita-admin:cognita-admin; fi; done
if [ '__MODE__' = full ]; then
  secret=/srv/cognita/config/broker.secret; assert_regular "$secret"
  if [ ! -s "$secret" ]; then atomic_generate "$secret" 0600 cognita-admin:cognita-admin openssl rand -hex 48; fi
  python3 -c 'import pathlib,sys; v=pathlib.Path(sys.argv[1]).read_text().strip(); assert v and len(v)<=4096 and not any(c.isspace() for c in v)' "$secret" || { echo 'Broker secret is invalid; it was preserved' >&2; exit 1; }
fi
for name in registry.yaml connectors.yaml authentication.yaml acceleration.yaml cognita.yaml; do assert_regular "/srv/cognita/config/$name"; done
if [ ! -s /srv/cognita/config/registry.yaml ]; then printf 'version: 1\nprojects: []\n' | atomic_write /srv/cognita/config/registry.yaml 0600 cognita-admin:cognita-admin; fi
if [ ! -s /srv/cognita/config/connectors.yaml ]; then printf 'version: 1\nrevision: 0\nconnectors: []\n' | atomic_write /srv/cognita/config/connectors.yaml 0600 cognita-admin:cognita-admin; fi
# AuthenticationPolicyStore alone owns schema initialization. Repair only the
# exact obsolete installer seed, never a policy that may contain operator data.
python3 - /srv/cognita/config/authentication.yaml <<'PY'
from pathlib import Path
import sys
path = Path(sys.argv[1])
if path.is_file() and path.read_bytes() == b'version: 1\nrevision: 0\nprojects: {}\n':
    path.unlink()
    print('Removed obsolete empty installer authentication seed; application will initialize its policy')
PY
if [ ! -s /srv/cognita/config/acceleration.yaml ]; then printf 'schema: 1\nrevision: 0\nknowledge:\n  gpu_enabled: false\n  gpu_device_ids: []\nocr:\n  device: cpu\n  gpu_device_ids: []\n' | atomic_write /srv/cognita/config/acceleration.yaml 0600 cognita-admin:cognita-admin; fi
if [ ! -s /srv/cognita/config/cognita.yaml ]; then printf 'mcp_host: 0.0.0.0\nmcp_port: 8675\nadmin_host: 0.0.0.0\nadmin_port: 8676\nadmin_username: admin\nadmin_password_hash: ""\npublic_base_url: http://127.0.0.1:10675\nregistry_path: /app/config/registry.yaml\nconnectors_path: /app/config/connectors.yaml\nauthentication_path: /app/config/authentication.yaml\nacceleration_path: /app/config/acceleration.yaml\ndata_root: /app/config/data\nwatch_enabled: true\nlog_level: INFO\nlog_dir: /app/config/logs\n' | atomic_write /srv/cognita/config/cognita.yaml 0600 cognita-admin:cognita-admin; fi
owned_directory /srv/cognita/config/data 0700
owned_directory /srv/cognita/config/logs 0700
service_uid=$(id -u cognita-admin); service_gid=$(id -g cognita-admin)
atomic_write /srv/cognita/compose.env 0600 cognita-admin:cognita-admin <<EOF
COGNITA_VERSION=__VERSION__
COGNITA_RELEASE_TARGET=windows
COGNITA_CONFIG_ROOT=/srv/cognita/config
COGNITA_PROJECTS_ROOT=/srv/cognita/sources
COGNITA_POSTGRES_DATA_ROOT=/srv/cognita/postgres
COGNITA_TRANSFER_STAGING_ROOT=/srv/cognita/transfers
COGNITA_SECRETS_ROOT=/srv/cognita/config
COGNITA_MODEL_CACHE_ROOT=/srv/cognita/models
COGNITA_SERVICE_UID=$service_uid
COGNITA_SERVICE_GID=$service_gid
COGNITA_MCP_HOST_PORT=10675
COGNITA_MCP_BIND_ADDRESS=127.0.0.1
COGNITA_ADMIN_HOST_PORT=10676
COGNITA_ADMIN_BIND_ADDRESS=127.0.0.1
EOF
if [ '__MODE__' = full ]; then
  test -e /dev/kvm; kvm_gid=$(stat -c %g /dev/kvm)
  if ! getent group "$kvm_gid" >/dev/null; then groupadd --system --gid "$kvm_gid" cognita-kvm; fi
  test -r /dev/kvm -a -w /dev/kvm
  { cat /srv/cognita/compose.env; printf 'COGNITA_WORKSPACE_DATA_ROOT=/srv/cognita/workspaces\nCOGNITA_TOOLBOX_IMAGE_CACHE_ROOT=/srv/cognita/workspaces/toolbox-cache\nCOGNITA_KVM_GID=%s\n' "$kvm_gid"; } | atomic_write /srv/cognita/compose.env 0600 cognita-admin:cognita-admin
fi
# Directory roots above are installer-owned. Their children may be PostgreSQL,
# Microsandbox state, or selected document bind mounts and must not be traversed.
for name in postgres.password postgres.dsn broker.secret admin_tls_certfile admin_tls_keyfile registry.yaml connectors.yaml authentication.yaml acceleration.yaml cognita.yaml; do
  file="/srv/cognita/config/$name"
  if [ -f "$file" ]; then chown cognita-admin:cognita-admin "$file"; chmod 0600 "$file"; fi
done
atomic_write /etc/systemd/system/cognita-compose.service 0644 root:root <<'EOF'
[Unit]
Description=Cognita Windows Compose stack
Requires=docker.service
After=docker.service
[Service]
Type=oneshot
RemainAfterExit=yes
WorkingDirectory=/srv/cognita
ExecStart=/usr/bin/docker compose --project-name cognita-windows --env-file /srv/cognita/compose.env --file /srv/cognita/compose.yaml --file /srv/cognita/compose.cpu.yaml __WORKSPACE_FILES__ --file /srv/cognita/compose.cpu.images.yaml --file /srv/cognita/compose.windows-projection.yaml up --detach --no-build --pull never
ExecStop=/usr/bin/docker compose --project-name cognita-windows --env-file /srv/cognita/compose.env --file /srv/cognita/compose.yaml --file /srv/cognita/compose.cpu.yaml __WORKSPACE_FILES__ --file /srv/cognita/compose.cpu.images.yaml --file /srv/cognita/compose.windows-projection.yaml stop
TimeoutStartSec=120
TimeoutStopSec=120
[Install]
WantedBy=multi-user.target
EOF
install -d -m 0755 /etc/systemd/system/docker.service.d
printf '[Service]\nExecStartPre=/usr/local/libexec/cognita-prepare-sources\n' | atomic_write /etc/systemd/system/docker.service.d/20-cognita-sources.conf 0644 root:root
systemctl unmask docker.service docker.socket
systemctl daemon-reload
'@
    $workspaceFiles=if($Bundle.Metadata.bundle_mode -eq 'full'){'--file /srv/cognita/compose.workspace.yaml'}else{''}
    $toolboxVersion=if($Bundle.Metadata.toolbox_version){$Bundle.Metadata.toolbox_version}else{''}
    $scriptText=$scriptText.Replace('__INSTALL_ID__',$id).Replace('__BUNDLE_B64__',$bundleB64).Replace('__RECORD_B64__',$recordB64).Replace('__MODE__',$Bundle.Metadata.bundle_mode).Replace('__VERSION__',$Bundle.Metadata.version).Replace('__WORKSPACE_FILES__',$workspaceFiles).Replace('__TOOLBOX_VERSION__',$toolboxVersion)
    # Bash consumes LF even when the closed bundle was checked out on Windows.
    return $scriptText.Replace("`r`n","`n")
}

function Initialize-ToolboxCache($Record,$Bundle) {
    if($Record.mode -ne 'full'){return}
    $script:CurrentMode=$Record.mode
    $archive="/var/lib/cognita/toolbox-cache/toolbox-$($Bundle.Metadata.toolbox_version).tar"
    # The Compose service supplies the persistent Microsandbox/cache mounts,
    # HOME, numeric service identity, and KVM group. Docker's image store alone
    # is insufficient: Microsandbox imports its own verified Toolbox binding.
    $loader=@('run','--pull','never','--rm','--no-deps','workspace-runtime','python3','-m','cognita.runtime_broker.image_cache')
    Invoke-Compose ($loader+@('materialize-binding','--archive',$archive)) 600|Out-Null
    try {
        Invoke-Compose ($loader+@('verify','--archive',$archive)) 600|Out-Null
        Say 'Toolbox already verified in the persistent Microsandbox cache.'
        return
    } catch {
        # The canonical release path likewise treats a failed verification as
        # requiring an import; load performs identity verification itself.
        Say 'Toolbox cache verification requires import; loading the accepted archive.'
    }
    Invoke-Compose ($loader+@('load','--archive',$archive)) 1800|Out-Null
}

function Initialize-InstallationConfiguration($Record) {
    $script:CurrentMode=$Record.mode
    $initialize=@'
from pathlib import Path
import os
import tempfile
import yaml
from cognita.config import load_config
from cognita.auth_policy import AuthenticationPolicyStore
def initialize():
    path = Path('/app/config/cognita.yaml')
    data = yaml.safe_load(path.read_text(encoding='utf-8'))
    if not isinstance(data, dict):
        raise ValueError('Installation configuration must be a YAML mapping')
    config = load_config(path)
    AuthenticationPolicyStore(config.authentication_path)
    if os.environ.get('COGNITA_INTERNAL_BEARER_FILE'):
        from cognita.runtime_broker.app import _configured_secret
        _configured_secret(None)
    # The deployment YAML seeds the application-owned public URL authority. Any
    # persisted Admin override continues to take precedence through load_config.
    if not data.get('public_base_url'):
        data['public_base_url'] = 'http://127.0.0.1:10675'
        fd, name = tempfile.mkstemp(prefix='.cognita-install-config.', dir=path.parent)
        temporary = Path(name)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                yaml.safe_dump(data, stream, sort_keys=False, allow_unicode=True)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
    load_config(path)

try:
    initialize()
except Exception as exc:
    # Schema errors can include input values. Expose only the error category,
    # never credentials or operator configuration in installer diagnostics.
    raise SystemExit('Installation configuration validation failed (' + type(exc).__name__ + '); existing values were preserved') from None
print('Installation configuration and application-owned authentication policy validated')
'@
    Invoke-Compose @('run','--pull','never','--rm','--no-deps','--entrypoint','python','cognita','-c',$initialize) 120|Out-Null
}

function Install-Launcher($Record) {
    $bin=Join-Path $script:Root 'bin';$null=New-Item -ItemType Directory -Path $bin -Force;Secure-Path $bin -Directory
    $vbs=Join-Path $bin 'launch-cognita.vbs'
    $content=@'
Option Explicit
Dim sh, cmd
Set sh = CreateObject("WScript.Shell")
cmd = Chr(34) & sh.ExpandEnvironmentStrings("%SystemRoot%\System32\wsl.exe") & Chr(34) & " -d Cognita-Windows -u root --exec /usr/local/libexec/cognita-session"
sh.Run cmd, 0, True
'@
    [IO.File]::WriteAllText($vbs,$content,[Text.UTF8Encoding]::new($false));Secure-Path $vbs
    $act=New-ScheduledTaskAction -Execute (Join-Path $env:SystemRoot 'System32\wscript.exe') -Argument ("//B `"$vbs`"")
    $trigger=New-ScheduledTaskTrigger -AtLogOn -User ([Security.Principal.WindowsIdentity]::GetCurrent().Name)
    $settings=New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
    $principal=New-ScheduledTaskPrincipal -UserId ([Security.Principal.WindowsIdentity]::GetCurrent().Name) -LogonType Interactive -RunLevel Limited
    $existing=Get-ScheduledTask -TaskName $script:Task -ErrorAction SilentlyContinue
    if($existing){Assert-LauncherTask $existing}
    Register-ScheduledTask -TaskName $script:Task -Action $act -Trigger $trigger -Settings $settings -Principal $principal -Force|Out-Null
}
function Resolve-TaskPrincipalSid([string]$UserId) {
    if([string]::IsNullOrWhiteSpace($UserId) -or $UserId -cne $UserId.Trim() -or $UserId -notmatch '^[^\\/:*?"<>|]+(?:\\[^\\/:*?"<>|]+)?$'){
        throw 'The Scheduled Task principal is malformed or cannot be resolved.'
    }
    try {
        $account=[Security.Principal.NTAccount]::new($UserId)
        $sid=$account.Translate([Security.Principal.SecurityIdentifier])
        if($null -eq $sid -or $sid.Value -notmatch '^S-1-(?:\d+-)*\d+$'){throw 'invalid SID'}
        return $sid
    } catch {
        throw 'The Scheduled Task principal is malformed or cannot be resolved.'
    }
}
function Assert-LauncherTask($TaskObject) {
    $expectedVbs=(Join-Path $script:Root 'bin\launch-cognita.vbs')
    $expectedExe=(Join-Path $env:SystemRoot 'System32\wscript.exe')
    $expectedArgs="//B `"$expectedVbs`""
    $actions=@($TaskObject.Actions);$principal=$TaskObject.Principal
    $matchesCurrentUser=$false
    try {
        $taskSid=Resolve-TaskPrincipalSid ([string]$principal.UserId)
        $currentSid=[Security.Principal.WindowsIdentity]::GetCurrent().User
        $matchesCurrentUser=$taskSid.Value -ceq $currentSid.Value
    } catch {}
    if($actions.Count -ne 1 -or $actions[0].Execute -ine $expectedExe -or $actions[0].Arguments -ine $expectedArgs -or -not $matchesCurrentUser -or $principal.LogonType -ne 'Interactive'){
        throw 'A Scheduled Task with the Cognita-Windows name has an unrecognized action or principal; it was left unchanged.'
    }
}
function Stop-InstalledUnits([string[]]$Units) {
    foreach($unitName in $Units){if($unitName -cnotin @($script:Unit,'docker.service','docker.socket')){throw 'Refusing to stop an unrelated systemd unit.'}}
    $stop=@'
set -eu
for unit do
    # A marker can exist before systemd was enabled or Docker/app units installed.
    # Offline unit-file absence is decisive only while no systemd manager runs.
    if test "$(cat /proc/1/comm)" != systemd; then
        files=$(systemctl --root=/ list-unit-files --no-legend "$unit")
        if test -z "$files"; then printf 'absent %s\n' "$unit"; continue; fi
        echo "Installed owned unit cannot be stopped without systemd: $unit" >&2; exit 1
    fi
  load=$(systemctl show --property=LoadState --value "$unit" 2>/dev/null) || {
    test "$load" = not-found || { echo "Cannot inspect owned unit: $unit" >&2; exit 1; }
  }
  if test "$load" = not-found; then printf 'absent %s\n' "$unit"; continue; fi
  case "$load" in loaded|masked) ;; *) echo "Invalid owned unit load state: $unit" >&2; exit 1 ;; esac
  systemctl stop "$unit"
  active=$(systemctl show --property=ActiveState --value "$unit")
  case "$active" in inactive|failed) printf 'stopped %s\n' "$unit" ;; *) echo "Owned unit remains active: $unit" >&2; exit 1 ;; esac
done
'@
    $result=@(Wsl (@('bash','-lc',$stop.Replace("`r`n","`n"),'cognita-stop-owned-units')+$Units) 180)
    foreach($unitName in $Units){
        if(@($result|Where-Object {$_ -ceq "absent $unitName" -or $_ -ceq "stopped $unitName"}).Count -ne 1){throw "Owned unit stop/absence could not be verified: $unitName."}
    }
}
function Stop-TaskSession {
    $task=Get-ScheduledTask -TaskName $script:Task -ErrorAction SilentlyContinue
    if($task){Assert-LauncherTask $task}
    Stop-InstalledUnits @($script:Unit)
    $end=[DateTime]::UtcNow.AddMinutes(2)
    do { $task=Get-ScheduledTask -TaskName $script:Task -ErrorAction SilentlyContinue; if($task -and $task.State -eq 'Running'){Start-Sleep -Seconds 2} } while($task -and $task.State -eq 'Running' -and [DateTime]::UtcNow -lt $end)
    if($task -and $task.State -eq 'Running'){throw 'The owned hidden WSL launcher did not exit within two minutes.'}
}

function Read-AdminCredential {
    if(-not $script:AdminCredential){
        if([Console]::IsInputRedirected){throw 'Noninteractive install must pass -AdminCredential.'}
        $script:AdminCredential=Get-Credential -UserName 'admin' -Message 'Set or verify Cognita Admin credentials'
    }
    if(-not $script:AdminCredential){throw 'Cognita Admin credentials are required before the container can start.'}
    $secure=$script:AdminCredential.Password
    $bstr=[Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
    try {
        $script:AdminUsername=$script:AdminCredential.UserName
        $script:AdminPassword=[Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)
    } finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr) }
    if(-not $script:AdminUsername -or -not $script:AdminPassword){throw 'Cognita Admin username and password must not be empty.'}
}

function Set-AdminCredentialsIfMissing {
    $state=Read-RequiredLastLine -Lines (Wsl @('python3','-c','from pathlib import Path; s=Path("/srv/cognita/config/cognita.yaml").read_text(); v=next(x.split(":",1)[1].strip() for x in s.splitlines() if x.startswith("admin_password_hash:")).strip(chr(34)+chr(39)); print("set" if v.startswith(chr(36)+"argon2") else "unset")') 30) -Operation 'Inspect Admin credential initialization state'
    if($state -eq 'unset'){
        Read-AdminCredential
        $payload=@{username=$script:AdminUsername;password=$script:AdminPassword}|ConvertTo-Json -Compress
        if($payload.Length -gt 65536){throw 'Admin credential payload exceeds the bounded stdin limit.'}
        $composeArgs=@('docker','compose','--project-name',$script:Project,'--env-file','/srv/cognita/compose.env','--file','/srv/cognita/compose.yaml','--file','/srv/cognita/compose.cpu.yaml')
        if($script:CurrentMode -eq 'full'){$composeArgs+=@('--file','/srv/cognita/compose.workspace.yaml')}
        $composeArgs+=@('--file','/srv/cognita/compose.cpu.images.yaml','--file','/srv/cognita/compose.windows-projection.yaml','run','--rm','--no-deps','-T','--volume','/srv/cognita/set-admin-credentials.py:/tmp/set-admin-credentials.py:ro','--entrypoint','python','cognita','/tmp/set-admin-credentials.py','--config','/app/config/cognita.yaml','--stdin-json')
        try { $result=Invoke-WslWithSecret (@('-d',$script:Distro,'-u','root','--exec')+$composeArgs) $payload }
        finally { $payload=$null }
        if($result.exit_code -ne 0){throw 'The accepted app image could not establish Argon2id Admin credentials through set-admin-credentials.py stdin mode.'}
        Say 'Argon2id Admin credentials were configured before Cognita startup.'
    } elseif($state -ne 'set') { throw 'Could not determine whether Argon2id Admin credentials are configured.' }
    if($script:AdminCredential){Read-AdminCredential}
}

function Invoke-AdminRequest($Session,[string]$Method,[string]$Path,$Body=$null) {
    $uri="http://127.0.0.1:$script:AdminPort$Path";$headers=@{}
    if($script:AdminCsrf){$headers['X-CSRF-Token']=$script:AdminCsrf}
    $parameters=@{Uri=$uri;Method=$Method;WebSession=$Session;TimeoutSec=20;ErrorAction='Stop'}
    if($headers.Count){$parameters.Headers=$headers}
    if($null -ne $Body){$parameters.ContentType='application/json';$parameters.Body=($Body|ConvertTo-Json -Depth 10 -Compress)}
    try{return Invoke-RestMethod @parameters}
    catch { throw "Admin API operation failed: $Method $Path (HTTP status unavailable; inspect Cognita-Windows Status)." }
}

function Get-AdminSession {
    # A CSRF token belongs to one login session; never reuse it for the new session.
    $script:AdminCsrf=$null
    $session=[Microsoft.PowerShell.Commands.WebRequestSession]::new()
    $status=Invoke-AdminRequest $session 'GET' '/api/session'
    if($status.auth_required){
        if(-not $script:AdminPassword){Read-AdminCredential}
        try{Invoke-AdminRequest $session 'POST' '/api/login' @{username=$script:AdminUsername;password=$script:AdminPassword}|Out-Null}
        catch{throw 'Cognita Admin login failed; Self-Test did not run.'}
        finally{$script:AdminPassword=$null;$script:AdminUsername=$null}
        $cookie=$session.Cookies.GetCookies([uri]"http://127.0.0.1:$script:AdminPort/")['cognita_csrf']
        if(-not $cookie){throw 'Cognita Admin login did not establish its CSRF session.'}
        $script:AdminCsrf=$cookie.Value
    } else { throw 'Admin authentication is not enabled; Cognita-Windows requires Argon2id credentials in both modes.' }
    return $session
}

function Invoke-Compose([string[]]$ComposeArgs,[int]$Timeout=600) {
    $prefix=@('docker','compose','--project-name',$script:Project,'--env-file','/srv/cognita/compose.env','--file','/srv/cognita/compose.yaml','--file','/srv/cognita/compose.cpu.yaml')
    if($script:CurrentMode -eq 'full'){$prefix+=@('--file','/srv/cognita/compose.workspace.yaml')}
    $prefix+=@('--file','/srv/cognita/compose.cpu.images.yaml','--file','/srv/cognita/compose.windows-projection.yaml')
    return Wsl ($prefix+$ComposeArgs) $Timeout
}

function Invoke-SelfTest([object]$Record) {
    $script:CurrentMode=$Record.mode
    $session=Get-AdminSession
    $documents='/srv/cognita/sources/cognita-self-test'
    $projects=Invoke-AdminRequest $session 'GET' '/api/projects'
    $project=@($projects.projects|Where-Object name -ceq 'Self-Test')|Select-Object -First 1
    if($project){
        if($project.documents_dir -cne $documents -or -not $project.enabled -or -not $project.writable){throw 'Existing Self-Test project does not match the reserved writable source; installer will not adopt it.'}
    } else {
        Invoke-AdminRequest $session 'POST' '/api/projects' @{name='Self-Test';documents_dir=$documents;writable=$true;exclude_from_default_permissions=$false}|Out-Null
        $verify=Invoke-AdminRequest $session 'GET' '/api/projects'
        $project=@($verify.projects|Where-Object name -ceq 'Self-Test')|Select-Object -First 1
        if(-not $project -or $project.documents_dir -cne $documents -or -not $project.writable){throw 'Admin did not confirm the reserved Self-Test project registration.'}
    }

    $rootIdentity=Read-RequiredLastLine -Lines (Wsl @('stat','-c','%d:%i',$documents) 30) -Operation 'Inspect Self-Test source identity'
    $clear='from pathlib import Path; r=Path("/srv/cognita/sources/cognita-self-test"); allow=("cognita-selftest/ocr/canonical-clear.png","cognita-selftest/ocr/blank.png","cognita-selftest/ocr/malformed.png","cognita-selftest/ocr/animated.png","cognita-selftest/ocr/not-a-png.txt","cognita-selftest/ocr/over-limit.png"); [(r/p).unlink() for p in allow if (r/p).is_file() and not (r/p).is_symlink()]'
    Wsl @('python3','-c',$clear) 30|Out-Null

    $provision=@('--volume','/srv/cognita/provision_selftest.py:/tmp/provision_selftest.py:ro','--entrypoint','python','cognita','/tmp/provision_selftest.py','--documents-dir',$documents,'--data-dir','/app/config/data/Self-Test','--registry','/app/config/registry.yaml','--defer-connector-check')
    $provisionResult=Invoke-Compose (@('run','--rm','--no-deps')+$provision) 300
    if(($provisionResult -join "`n") -notmatch 'SELFTEST_FIXTURES_READY_CONNECTOR_DEFERRED'){throw 'Packaged Self-Test fixture provisioning did not confirm deferred connector mode.'}
    if((Read-RequiredLastLine -Lines (Wsl @('stat','-c','%d:%i',$documents) 30) -Operation 'Verify provisioned Self-Test source identity') -cne $rootIdentity){throw 'Self-Test source root identity changed during fixture provisioning.'}

    $connectorId=$null;$rawKey=$null;$keyGenerated=$false;$cleanupErrors=[Collections.Generic.List[string]]::new()
    try {
    $connectors=Invoke-AdminRequest $session 'GET' '/api/connectors'
    foreach($stale in @($connectors.connectors|Where-Object slug -ceq 'self-test')){
        if($stale.name -cne 'Self-Test' -or $stale.project_mode -cne 'selected' -or @($stale.project_access.PSObject.Properties.Name) -join ',' -cne 'Self-Test'){throw 'A non-Self-Test connector occupies the reserved self-test slug; it was left unchanged.'}
        $null=Invoke-AdminRequest $session 'DELETE' ("/api/connectors/$($stale.id)?expected_revision=$($connectors.revision)")
        $connectors=Invoke-AdminRequest $session 'GET' '/api/connectors'
    }
    $workspaceAllowed=($Record.mode -eq 'full')
    $connectorBody=@{expected_revision=$connectors.revision;name='Self-Test';enabled=$true;project_mode='selected';default_access=$null;project_access=@{'Self-Test'='write'};workspace_enabled=$workspaceAllowed;default_workspace_transfer=$(if($workspaceAllowed){'allow'}else{'deny'})}
    $created=Invoke-AdminRequest $session 'POST' '/api/connectors' $connectorBody
    $connectorId=$created.connector.id
    $readback=Invoke-AdminRequest $session 'GET' '/api/connectors'
    $connector=@($readback.connectors|Where-Object slug -ceq 'self-test')|Select-Object -First 1
    if(-not $connector -or -not $connector.enabled -or $connector.project_mode -cne 'selected' -or $connector.project_access.'Self-Test' -cne 'write' -or $connector.workspace_enabled -ne $workspaceAllowed){throw 'Self-Test connector read-back did not match exact project/write/mode policy.'}

        $auth=Invoke-AdminRequest $session 'GET' '/api/authentication'
        $oldRow=@($auth.projects|Where-Object name -ceq 'Self-Test')|Select-Object -First 1
        if($oldRow -and $oldRow.static_key_override){Invoke-AdminRequest $session 'POST' '/api/authentication/projects/Self-Test/static-key/revoke' @{expected_revision=$auth.revision;confirm_lockout=$true}|Out-Null;$auth=Invoke-AdminRequest $session 'GET' '/api/authentication';$oldRow=@($auth.projects|Where-Object name -ceq 'Self-Test')|Select-Object -First 1;if($oldRow.static_key_override){throw 'A previous Self-Test project key could not be revoked and verified.'}}
        $keyResult=Invoke-AdminRequest $session 'POST' '/api/authentication/projects/Self-Test/static-key/generate' @{expected_revision=$auth.revision}
        $keyGenerated=$true
        $rawKey=[string]$keyResult.generated_key
        if(-not $rawKey){throw 'Self-Test project key generation returned no key.'}
        $runArgs=@('--volume','/srv/cognita/run-selftest.py:/tmp/run-selftest.py:ro','--entrypoint','sh','cognita','-c',"read -r COGNITA_TEST_API_KEY; export COGNITA_TEST_API_KEY COGNITA_TEST_PROJECT=Self-Test; exec python /tmp/run-selftest.py --mode $($Record.mode) http://cognita:8675/mcp/connectors/self-test/mcp")
        $wslArgs=@('-d',$script:Distro,'-u','root','--exec','docker','compose','--project-name',$script:Project,'--env-file','/srv/cognita/compose.env','--file','/srv/cognita/compose.yaml','--file','/srv/cognita/compose.cpu.yaml')
        if($Record.mode -eq 'full'){$wslArgs+=@('--file','/srv/cognita/compose.workspace.yaml')}
    $wslArgs+=@('--file','/srv/cognita/compose.cpu.images.yaml','--file','/srv/cognita/compose.windows-projection.yaml','run','--rm','--no-deps','-T')+$runArgs
        $result=Invoke-WslWithSecret $wslArgs $rawKey
        if($result.exit_code -ne 0){throw "Self-Test runner failed with exit code $($result.exit_code); synthetic resources will be cleaned up."}
        Say 'Self-Test runner completed against the synthetic Self-Test project.'
    } finally {
        if($keyGenerated){
            try{$current=Invoke-AdminRequest $session 'GET' '/api/authentication';Invoke-AdminRequest $session 'POST' '/api/authentication/projects/Self-Test/static-key/revoke' @{expected_revision=$current.revision;confirm_lockout=$true}|Out-Null;$after=Invoke-AdminRequest $session 'GET' '/api/authentication';$row=@($after.projects|Where-Object name -ceq 'Self-Test')|Select-Object -First 1;if($row.static_key_override){throw 'Self-Test key revocation did not read back as complete.'}}
            catch{$cleanupErrors.Add('project-scoped Self-Test static key could not be revoked and verified')}
        }
        if($connectorId){
            try{$current=Invoke-AdminRequest $session 'GET' '/api/connectors';$left=@($current.connectors|Where-Object id -ceq $connectorId);if($left){Invoke-AdminRequest $session 'DELETE' ("/api/connectors/${connectorId}?expected_revision=$($current.revision)")|Out-Null};$after=Invoke-AdminRequest $session 'GET' '/api/connectors';if(@($after.connectors|Where-Object id -ceq $connectorId).Count){throw 'temporary Self-Test connector remains'}}
            catch{$cleanupErrors.Add('temporary Self-Test connector could not be removed and verified')}
        }
        $rawKey=$null;$script:AdminPassword=$null;$script:AdminUsername=$null;$script:AdminCsrf=$null
        if($cleanupErrors.Count){throw ('Self-Test cleanup incomplete: '+($cleanupErrors -join '; '))}
    }
}

function Invoke-WslWithSecret([string[]]$Arguments,[string]$Secret,[int]$Timeout=1800) {
    $call=Get-BoundedWslArguments $Arguments $Timeout
    try {
        if($call.UnitName){$result=Invoke-OwnedProcess (Join-Path $env:SystemRoot 'System32\wsl.exe') $call.Arguments ($Timeout+15) $Secret -OnInterrupted {Remove-OwnedLinuxCommand $call} -OnStarted {$call.LaunchSubmitted=$true}}
        else {$result=Invoke-OwnedProcess (Join-Path $env:SystemRoot 'System32\wsl.exe') $call.Arguments ($Timeout+15) $Secret -AwaitBoundedCompletion}
        $stdout=$result.stdout
        $passed=([regex]::Matches($stdout,'(?m)^PASS\b')).Count;$failed=([regex]::Matches($stdout,'(?m)^FAIL\b')).Count
        Say "Self-Test checks passed=$passed failed=$failed"
        return @{exit_code=$result.exit_code;passed=$passed;failed=$failed}
    } finally {
        $Secret=$null
        try {if($call.UnitName){Remove-OwnedLinuxCommand $call}}
        finally {if($call.ContainerName){Remove-OwnedCommandContainer $call.ContainerName}}
    }
}
function Reset-RecreatedSyntheticIdentity($Record) {
    # Only the installation-owned fixture changes identity with the disposable
    # distro. External identities remain authoritative across reconstruction.
    $synthetic=@($Record.sources|Where-Object {$_.alias -ceq 'cognita-self-test' -and $_.source_kind -ceq 'installation_ext4' -and $_.locator -ceq '/srv/cognita/sources/cognita-self-test'})
    if($synthetic.Count -ne 1){throw 'The reserved Self-Test source mapping is invalid; reconstruction refused.'}
    $synthetic[0].canonical_identity=$null
    $synthetic[0].last_runtime_identity=$null
    Save-Record $Record
}

function Assert-RecordedBundle($Bundle,$Record) {
    if($Record.bundle_sha256 -cne $Bundle.Sum -or $Record.version -cne $Bundle.Metadata.version -or $Record.commit -cne $Bundle.Metadata.commit -or $Record.mode -cne $Bundle.Metadata.bundle_mode){throw 'Bundle identity differs from install.json; restore the exact recorded bundle before Repair.'}
}

function Get-SourceRepairState($Bundle) {
    # Observe installed artifacts and propagation rather than keeping a second
    # completion flag in the owner record. Missing durable files are a partial
    # installation; they require the normal completion path.
    $helperHash=(Get-FileHash -Algorithm SHA256 -LiteralPath (Join-Path $Bundle.Root 'scripts/windows/Prepare-Sources.py')).Hash.ToLowerInvariant()
    $check=@'
complete=false; sources_ready=false; hook_current=false; root_shared=false; docker_active=false
if systemctl is-active --quiet docker.service; then docker_active=true; fi
if test -f /etc/cognita-install-record-path && getent passwd cognita-admin >/dev/null; then sources_ready=true; fi
if test -f /etc/cognita-install-record-path -a -x /usr/local/libexec/cognita-prepare-sources -a -x /usr/local/libexec/cognita-session -a -f /etc/systemd/system/cognita-compose.service -a -f /srv/cognita/compose.env -a -f /srv/cognita/config/cognita.yaml -a -f /srv/cognita/compose.cpu.images.yaml; then complete=true; fi
if test "$(sha256sum /usr/local/libexec/cognita-prepare-sources 2>/dev/null | cut -d' ' -f1)" = '__HELPER_HASH__' && test "$(cat /etc/systemd/system/docker.service.d/20-cognita-sources.conf 2>/dev/null)" = "$(printf '[Service]\nExecStartPre=/usr/local/libexec/cognita-prepare-sources')"; then hook_current=true; fi
case "$(findmnt --noheadings --mountpoint /srv/cognita/sources --output PROPAGATION 2>/dev/null)" in *shared*) root_shared=true;; esac
printf '{"complete":%s,"sources_ready":%s,"hook_current":%s,"root_shared":%s,"docker_active":%s}\n' "$complete" "$sources_ready" "$hook_current" "$root_shared" "$docker_active"
'@
    $check=$check.Replace('__HELPER_HASH__',$helperHash)
    $result=Read-RequiredLastLine -Lines (Wsl @('bash','-lc',$check) 60) -Operation 'Inspect source repair prerequisites'
    try {$state=$result|ConvertFrom-Json}catch{throw 'Source repair prerequisites returned malformed output.'}
    foreach($field in @('complete','sources_ready','hook_current','root_shared','docker_active')){if($state.$field -isnot [bool]){throw 'Source repair prerequisites returned invalid facts.'}}
    return $state
}

function Prepare-RecordedSources($Bundle,[switch]$AcceptedHelper) {
    $helper='/usr/local/libexec/cognita-prepare-sources'
    if($AcceptedHelper){$helper=Convert-WindowsPathToWsl -Path (Join-Path $Bundle.Root 'scripts/windows/Prepare-Sources.py') -Operation 'Convert accepted source helper path to WSL'}
    $prepared=@(Wsl @('python3',$helper) 90)
    $summary=$prepared|Where-Object {$_ -match 'projection_changed=(true|false)$'}|Select-Object -Last 1
    if(-not $summary){throw 'Source preparation did not report its projection state.'}
    $script:ProjectionChanged=([string]$summary) -match 'projection_changed=true$'
}

function Refresh-SourceStartup($Bundle) {
    $bundlePath=Convert-WindowsPathToWsl -Path $Bundle.Root -Operation 'Convert accepted startup bundle path to WSL'
    $bundleB64=[Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($bundlePath))
    $refresh=@'
set -eu
bundle=$(printf %s '__BUNDLE_B64__' | base64 -d)
helper=/usr/local/libexec/cognita-prepare-sources
hook=/etc/systemd/system/docker.service.d/20-cognita-sources.conf
trap 'rm -f "$helper.tmp" "$hook.tmp"' EXIT
install -d -m 0755 /usr/local/libexec /etc/systemd/system/docker.service.d
install -m 0755 "$bundle/scripts/windows/Prepare-Sources.py" "$helper.tmp"
printf '[Service]\nExecStartPre=/usr/local/libexec/cognita-prepare-sources\n' >"$hook.tmp"
chmod 0644 "$hook.tmp"
mv "$helper.tmp" "$helper"
mv "$hook.tmp" "$hook"
systemctl daemon-reload
'@
    Wsl @('bash','-lc',$refresh.Replace('__BUNDLE_B64__',$bundleB64)) 90|Out-Null
}

function Test-InstalledReleaseInputs($Bundle,$Record) {
    try {
        Verify-PostgresBundleImage $Bundle
        foreach($reference in @($Record.image_ref_cognita_cpu)){
            $image=Read-RequiredLastLine -Lines (Wsl @('docker','image','inspect','--format','{{.Id}} {{index .Config.Labels "org.opencontainers.image.version"}} {{index .Config.Labels "org.opencontainers.image.revision"}}',$reference) 60) -Operation 'Inspect installed CPU image'
            if($image -cne "$($Record.image_cognita_cpu) $($Record.version) $($Record.commit)"){return $false}
        }
        if($Record.mode -eq 'full'){
            $image=Read-RequiredLastLine -Lines (Wsl @('docker','image','inspect','--format','{{.Id}} {{index .Config.Labels "org.opencontainers.image.version"}} {{index .Config.Labels "org.opencontainers.image.revision"}}',$Record.image_ref_workspace_runtime) 60) -Operation 'Inspect installed Workspace image'
            if($image -cne "$($Record.image_workspace_runtime) $($Record.version) $($Record.commit)"){return $false}
            $toolbox=Read-RequiredLastLine -Lines (Wsl @('docker','image','inspect','--format','{{.Id}}',"cognita-workspace-toolbox:$($Bundle.Metadata.toolbox_version)") 60) -Operation 'Inspect installed Toolbox image'
            if($toolbox -notmatch '^sha256:[0-9a-f]{64}$'){return $false}
        }
        $configured=Read-RequiredLastLine -Lines (Wsl @('bash','-lc','if grep -q ''\$argon2'' /srv/cognita/config/cognita.yaml; then echo configured; else echo incomplete; fi') 30) -Operation 'Inspect installed Admin credential state'
        return $configured -ceq 'configured'
    } catch {return $false}
}

function Repair-ExistingInstallation($Bundle,$Record,$State) {
    $script:CurrentMode=$Record.mode
    # Observe the effective projection before any helper can regenerate it.
    # A manual alias belongs to the working installation even when the durable
    # installer source list has not yet caught up with the manual proof.
    $preservation=Get-SourcePreservationGuard $Record
    if($preservation.manual){
        Repair-PreservedInstallation $Bundle $Record $State
        return
    }
    $task=Get-ScheduledTask -TaskName $script:Task -ErrorAction SilentlyContinue
    if($task){Assert-LauncherTask $task}
    $health=try{Invoke-RestMethod -TimeoutSec 3 -Uri "http://127.0.0.1:$($Record.mcp_port)/healthz"}catch{$null}
    if($preservation.verified -and $State.complete -and $State.docker_active -and $State.hook_current -and $State.root_shared -and $task -and $task.State -eq 'Running' -and $health -and $health.PSObject.Properties['workspace'] -and $health.status -eq 'ok' -and $health.version -ceq $Record.version -and $health.workspace.mode -ceq $Record.mode -and (Test-InstalledReleaseInputs $Bundle $Record)){
        Assert-RecordedPorts $Record
        Say 'verified the unchanged healthy installation; no source preparation or restart was needed.'
        return
    }
    # The accepted helper validates upstreams before stopping a healthy unit.
    # A substitution detaches only its alias, publishes unavailable facts, and
    # fails here while the stack and hidden owner session remain running.
    Prepare-RecordedSources $Bundle -AcceptedHelper
    $changed=$script:ProjectionChanged
    if(-not $State.hook_current -or -not $State.root_shared){
        Stop-TaskSession
        Stop-InstalledUnits @('docker.service','docker.socket')
        Refresh-SourceStartup $Bundle
        Prepare-RecordedSources $Bundle
        $verified=Get-SourceRepairState $Bundle
        if(-not $verified.hook_current -or -not $verified.root_shared){throw 'Source startup hook or shared-root repair could not be verified; run -Action Status and restore the recorded bundle.'}
        Wsl @('systemctl','start','docker.service') 180|Out-Null
        if(-not(Test-InstalledReleaseInputs $Bundle $Record)){Do-Install -CompletePartial;return}
        # Both hook refresh and root propagation require a new app projection
        # inode and bind, even if the helper's second observation is unchanged.
        $script:ProjectionChanged=$true
        Do-Start
        Say 'repair refreshed source startup resources and recreated Cognita.'
        return
    }
    if(-not $State.docker_active){Wsl @('systemctl','start','docker.service') 180|Out-Null}
    if(-not(Test-InstalledReleaseInputs $Bundle $Record)){Do-Install -CompletePartial;return}
    if($changed){
        Stop-TaskSession
        $script:ProjectionChanged=$true
        Do-Start
        Say 'repair recreated Cognita to consume verified source changes.'
        return
    }
    $task=Get-ScheduledTask -TaskName $script:Task -ErrorAction SilentlyContinue
    if($task){Assert-LauncherTask $task}
    $health=try{Invoke-RestMethod -TimeoutSec 3 -Uri "http://127.0.0.1:$($Record.mcp_port)/healthz"}catch{$null}
    if($task -and $task.State -eq 'Running' -and $health -and $health.PSObject.Properties['workspace'] -and $health.status -eq 'ok' -and $health.version -ceq $Record.version -and $health.workspace.mode -ceq $Record.mode){
        $container=Read-RequiredLastLine -Lines (Invoke-Compose @('ps','-q','cognita') 60) -Operation 'Inspect repaired Cognita container'
        $image=Read-RequiredLastLine -Lines (Wsl @('docker','inspect','--format','{{.Image}}',$container) 60) -Operation 'Inspect repaired Cognita image'
        if($image -cne $Record.image_cognita_cpu){throw 'Running container image ID differs from the accepted CPU image.'}
        Say 'repair verified unchanged sources and healthy installation; no restart was needed.'
        return
    }
    Do-Start
    Say 'repair started the unchanged recorded installation.'
}

function Do-Install([switch]$CompletePartial) {
    if(-not $Mode){throw 'Install requires -Mode Core or -Mode Full to match the qualified bundle.'}
    $bundle=Verify-Bundle $BundlePath;$sources=Get-Sources;$newInstall=-not(Test-Path -LiteralPath $script:RecordPath)
    if(-not $newInstall){
        $record=Read-Record
        Assert-RecordedBundle $bundle $record
        if(($Source.Count+$SmbSource.Count) -gt 0){$requested=@(Get-Sources|Select-Object alias,source_kind,locator|Sort-Object alias|ConvertTo-Json -Depth 5 -Compress);$recorded=@($record.sources|Select-Object alias,source_kind,locator|Sort-Object alias|ConvertTo-Json -Depth 5 -Compress);if($requested -cne $recorded){throw 'Source configuration differs from the owner record; explicit source reconfiguration is required.'}}
        if(DistroExists $script:Distro){
            $owner=@(Run 'wsl.exe' @('-d',$script:Distro,'-u','root','--exec','bash','-lc','if test -f /etc/cognita-install-id; then cat /etc/cognita-install-id; fi') 30)
            $ownerId=Read-OptionalLastLine -Lines $owner
            if($ownerId -and $ownerId -cne $record.installation_id){throw 'Distro ownership marker differs from install.json; lifecycle operation refused.'}
            if($ownerId){
                $repairState=Get-SourceRepairState $bundle
                $preservation=Get-SourcePreservationGuard $record
                if($preservation.manual){Repair-PreservedInstallation $bundle $record $repairState;return}
                if($repairState.complete -and -not $CompletePartial){Repair-ExistingInstallation $bundle $record $repairState;return}
                if($repairState.sources_ready){Prepare-RecordedSources $bundle -AcceptedHelper}
                Stop-TaskSession
                Stop-InstalledUnits @('docker.service','docker.socket')
            }
        }
    }
    else{Assert-Available $sources;$record=New-Record $bundle $sources;Save-Record $record}
    $null=Install-FirstRun $bundle $record
    Prepare-RecordedSources $bundle
    if(-not (Get-SourceRepairState $bundle).root_shared){throw 'The source root is not shared; refusing image load or Compose startup.'}
    if($newInstall){
        $missing=Read-OptionalLastLine -Lines (Wsl @('python3','-c','import json; p=json.load(open("/run/cognita/source-identities.json")); print(" ".join(sorted(s["alias"] for s in p["sources"] if s["source_kind"] != "installation_ext4" and s["observation"] != "available")))') 30)
        if($missing){throw "Initial source identity capture failed for aliases: $missing. Restore those selected sources and rerun Install; no project data was indexed."}
    }
    Install-Launcher $record
    Wsl @('systemctl','start','docker.service') 180|Out-Null
    $tar=Join-Path $bundle.Root 'cognita-cpu.tar';$tarWsl=Convert-WindowsPathToWsl -Path $tar -Operation 'Convert CPU image archive path to WSL'
    $null=Verify-AcceptedArchiveChecksum (Join-Path $bundle.Root 'SHA256SUMS') 'cognita-cpu.tar' $tar
    Wsl @('docker','load','--input',$tarWsl) 3600|Out-Null
    Verify-PostgresBundleImage $bundle
    $inspect=Wsl @('docker','image','inspect','--format','{{.Id}} {{index .Config.Labels "org.opencontainers.image.version"}} {{index .Config.Labels "org.opencontainers.image.revision"}}',$record.image_ref_cognita_cpu) 120
    if((Read-RequiredLastLine -Lines $inspect -Operation 'Inspect loaded CPU image') -cne "$($record.image_cognita_cpu) $($record.version) $($record.commit)"){throw 'Loaded CPU image ID or OCI version/revision labels differ from the accepted release.'}
    $script:CurrentMode=$record.mode
    Initialize-InstallationConfiguration $record
    if($record.mode -eq 'full'){
        foreach($archive in @('workspace-runtime.tar','toolbox.tar')){$file=Join-Path $bundle.Root $archive;$null=Verify-AcceptedArchiveChecksum (Join-Path $bundle.Root 'SHA256SUMS') $archive $file;$path=Convert-WindowsPathToWsl -Path $file -Operation "Convert $archive path to WSL";Wsl @('docker','load','--input',$path) 3600|Out-Null}
        $runtime=Wsl @('docker','image','inspect','--format','{{.Id}} {{index .Config.Labels "org.opencontainers.image.version"}} {{index .Config.Labels "org.opencontainers.image.revision"}}',$record.image_ref_workspace_runtime) 120
        if((Read-RequiredLastLine -Lines $runtime -Operation 'Inspect loaded Workspace image') -cne "$($record.image_workspace_runtime) $($record.version) $($record.commit)"){throw 'Loaded Workspace image ID or OCI labels differ from the accepted release.'}
        $toolbox=Wsl @('docker','image','inspect','--format','{{.Id}}',"cognita-workspace-toolbox:$($bundle.Metadata.toolbox_version)") 120
        if((Read-RequiredLastLine -Lines $toolbox -Operation 'Inspect loaded Toolbox image') -notmatch '^sha256:[0-9a-f]{64}$'){throw 'Accepted Toolbox image tag did not load.'}
        $script:CurrentMode=$record.mode
        Initialize-ToolboxCache $record $bundle
    }
    $composeFiles=@('--file','/srv/cognita/compose.yaml','--file','/srv/cognita/compose.cpu.yaml')
    if($record.mode -eq 'full'){$composeFiles+=@('--file','/srv/cognita/compose.workspace.yaml')}
    $composeFiles+=@('--file','/srv/cognita/compose.cpu.images.yaml','--file','/srv/cognita/compose.windows-projection.yaml')
    Wsl (@('docker','compose','--project-name',$script:Project,'--env-file','/srv/cognita/compose.env')+$composeFiles+@('config')) 120|Out-Null
    $script:CurrentMode=$record.mode
    Set-AdminCredentialsIfMissing
    Wsl @('systemctl','enable',$script:Unit) 60|Out-Null
    $record=Read-Record
    $record.updated_utc=[DateTime]::UtcNow.ToString('o')
    Save-Record $record
    Do-Start
    Invoke-SelfTest $record
    Say "accepted CPU image $($record.image_cognita_cpu); use -Action Start to run the recorded installation"
}
function Do-Status {
    if(-not(Test-Path -LiteralPath $script:RecordPath)){Say 'not installed';return};$r=Verify-Owner
    $t=Get-ScheduledTask -TaskName $script:Task -ErrorAction SilentlyContinue;if($t){Assert-LauncherTask $t};$state=if($t){$t.State}else{'missing'}
    $health=try{(Invoke-RestMethod -TimeoutSec 3 -Uri "http://127.0.0.1:$($r.mcp_port)/healthz").status}catch{'unavailable'}
    Say "status=$health task=$state mode=$($r.mode) version=$($r.version) commit=$($r.commit) distro=$script:Distro source-alias-count=$(@($r.sources).Count)"
    if($r.mode -eq 'full'){
        try {
            $observed=Invoke-UpdateHelper 'status' @('--record',(Convert-WindowsPathToWsl $script:RecordPath 'Convert owner record'), '--old-bundle',(Convert-WindowsPathToWsl $r.bundle_path 'Convert current bundle'),'--candidate',(Convert-WindowsPathToWsl $r.bundle_path 'Convert current bundle')) 120
            Say ('observed release selection: '+(Read-RequiredLastLine $observed 'Inspect release selection'))
        } catch {Say 'release selection could not be inspected; installation may be partial. Run the canonical deploy-windows command to retry the same qualified candidate.'}
    }
}
function Do-Start {
    $r=Verify-Owner;$script:CurrentMode=$r.mode
    $bundle=Verify-Bundle $r.bundle_path;Assert-RecordedBundle $bundle $r
    if(-not(Test-InstalledReleaseInputs $bundle $r)){throw 'Recorded release inputs are unavailable or inconsistent; use Repair from the recorded bundle.'}
    Assert-RecordedPorts $r
    if($script:ProjectionChanged){
        Invoke-Compose @('up','--detach','--no-deps','--no-build','--pull','never','--force-recreate','cognita') 600|Out-Null
        $script:ProjectionChanged=$false
    }
    $task=Get-ScheduledTask -TaskName $script:Task -ErrorAction SilentlyContinue;if(-not $task){Install-Launcher $r}else{Assert-LauncherTask $task};Start-ScheduledTask -TaskName $script:Task
    $end=[DateTime]::UtcNow.AddMinutes(2)
    do {
        Start-Sleep -Seconds 2
        try { $h=Invoke-RestMethod -TimeoutSec 3 -Uri "http://127.0.0.1:$($r.mcp_port)/healthz" }
        catch { continue }
        if($h.version -cne $r.version){throw 'Running Cognita version differs from the accepted bundle.'}
        if($null -eq $h.workspace -or $h.workspace.mode -notin @('core','full')){throw 'Cognita health is missing the Worker B workspace-mode contract.'}
        if($h.workspace.mode -ne $r.mode){throw 'Running Cognita workspace mode differs from the recorded install mode.'}
        if($h.status -eq 'ok'){
            $container=Read-RequiredLastLine -Lines (Invoke-Compose @('ps','-q','cognita') 60) -Operation 'Inspect started Cognita container'
            if(-not $container){throw 'Compose has no Cognita app container.'}
            $actualImage=Read-RequiredLastLine -Lines (Wsl @('docker','inspect','--format','{{.Image}}',$container) 60) -Operation 'Inspect started Cognita image'
            if($actualImage -cne $r.image_cognita_cpu){throw 'Running container image ID differs from the accepted CPU image.'}
            $taskDeadline=[DateTime]::UtcNow.AddSeconds(30)
            do {$taskState=(Get-ScheduledTask -TaskName $script:Task).State;if($taskState -ne 'Running'){Start-Sleep -Seconds 2}} while($taskState -ne 'Running' -and [DateTime]::UtcNow -lt $taskDeadline)
            if($taskState -ne 'Running'){throw 'Cognita is healthy but its hidden Scheduled Task keepalive is not Running.'}
            if($r.mode -eq 'full'){
                $bundlePath=Convert-WindowsPathToWsl $r.bundle_path 'Convert current bundle for runtime verification'
                Invoke-UpdateHelper 'current' @('--record',(Convert-WindowsPathToWsl $script:RecordPath 'Convert current owner record'),'--old-bundle',$bundlePath,'--candidate',$bundlePath) 180|Out-Null
            }
            Say "healthy version=$($r.version) mode=$($r.mode) image=$actualImage"
            return
        }
    } while([DateTime]::UtcNow -lt $end)
    throw 'Cognita did not become healthy in two minutes. Run -Action Status; logs are available through the recorded unit.'
}
function Do-Stop {$null=Verify-Owner;Stop-TaskSession;Say 'Cognita unit stopped; hidden WSL client is exiting.'}
function Do-Repair {
    $r=Read-Record
    if($Mode -and $Mode.ToLowerInvariant() -ne $r.mode){throw 'Repair mode must match the recorded installation.'}
    $Mode=(Get-Culture).TextInfo.ToTitleCase($r.mode);$BundlePath=$r.bundle_path
    $bundle=Verify-Bundle $BundlePath;Assert-RecordedBundle $bundle $r
    if(DistroExists $script:Distro){
        # A missing marker is the explicit interruption/recreation path owned by
        # Install-FirstRun; mismatched nonempty markers are never adopted.
        $owner=Read-OptionalLastLine -Lines (Wsl @('bash','-lc','if test -f /etc/cognita-install-id; then cat /etc/cognita-install-id; fi') 30)
        if($owner -and $owner -cne $r.installation_id){throw 'Distro marker does not match install.json; lifecycle operation refused.'}
        if($owner){
            $state=Get-SourceRepairState $bundle
            $preservation=Get-SourcePreservationGuard $r
            if($preservation.manual){Repair-PreservedInstallation $bundle $r $state;return}
            if($state.complete){Repair-ExistingInstallation $bundle $r $state;return}
        }
    }
    Do-Install -CompletePartial
    Say 'repair completed the partial installation; no application state was reset.'
}
function Do-Reset {
    $r=Verify-Owner;$answer=Read-Host 'This resets only the derived index and Workspace scratch. Type RESET COGNITA STATE'
    if($answer -cne 'RESET COGNITA STATE'){Say 'reset cancelled';return}
    Stop-TaskSession
    Wsl @('/usr/local/libexec/cognita-reset-state') 1800|Out-Null
    Say 'derived state reset; original source documents, credentials, and configuration were preserved.'
}
function Do-Uninstall {
    if(-not $ConfirmUninstall){throw 'Uninstall requires -ConfirmUninstall and the exact phrase.'};$r=Verify-Owner
    $script:CurrentMode=$r.mode
    $answer=Read-Host "Type UNINSTALL $script:Distro to remove only Cognita startup resources";if($answer -cne "UNINSTALL $script:Distro"){Say 'uninstall cancelled';return}
    Stop-TaskSession
    Invoke-Compose @('down','--remove-orphans') 600|Out-Null
    Unregister-ScheduledTask -TaskName $script:Task -Confirm:$false -ErrorAction SilentlyContinue
    $vbs=Join-Path $script:Root 'bin\launch-cognita.vbs'
    if(Test-Path -LiteralPath $vbs){Remove-Item -LiteralPath $vbs -Force}
    Wsl @('systemctl','disable',$script:Unit) 60|Out-Null
    Wsl @('bash','-lc','rm -f /etc/systemd/system/cognita-compose.service /etc/systemd/system/docker.service.d/20-cognita-sources.conf /usr/local/libexec/cognita-prepare-sources /usr/local/libexec/cognita-session /usr/local/libexec/cognita-reset-state; systemctl daemon-reload; systemctl stop docker.service docker.socket') 180|Out-Null
    if($ConfirmUnregisterDistro){$confirm=Read-Host "Type UNREGISTER $script:Distro to delete its disposable index and scratch state";if($confirm -ceq "UNREGISTER $script:Distro"){Run 'wsl.exe' @('--unregister',$script:Distro) 1800|Out-Null}}
    Say "startup task and application unit removed; the owner record and host data/configuration roots are retained at $script:Root and in the dedicated distro."
}

function Invoke-UpdateHelper([string]$Operation,[string[]]$Arguments,[int]$Timeout=120) {
    $helper=Convert-WindowsPathToWsl (Join-Path $PSScriptRoot 'Update-Release.py') 'Convert maintenance lifecycle helper'
    return Wsl (@('python3',$helper,$Operation)+$Arguments) $Timeout
}
function Get-SourcePreservationGuard($Record) {
    $path=Convert-WindowsPathToWsl $script:RecordPath 'Convert owner record for preservation guard'
    $value=Read-RequiredLastLine (Invoke-UpdateHelper 'guard' @('--record',$path) 60) 'Inspect source preservation guard'
    $guard=$value|ConvertFrom-Json
    if($guard.manual -isnot [bool] -or $guard.verified -isnot [bool]){throw 'Source preservation guard returned invalid facts.'}
    return $guard
}
function Assert-RecordedPorts($Record) {
    $path=Convert-WindowsPathToWsl $script:RecordPath 'Convert owner record for port verification'
    Invoke-UpdateHelper 'ports' @('--record',$path) 120|Out-Null
}
function Repair-PreservedInstallation($Bundle,$Record,$State) {
    if(-not $State.complete -or -not $State.sources_ready -or -not $State.root_shared -or -not $State.docker_active){throw 'A live source alias is absent from the installer record. Existing source/startup resources must be repaired explicitly before lifecycle bootstrap; the mapping was preserved.'}
    $task=Get-ScheduledTask -TaskName $script:Task -ErrorAction SilentlyContinue
    if(-not $task -or $task.State -ne 'Running'){throw 'The live manual source mapping requires its existing running keepalive; explicit startup repair is required.'}
    Assert-LauncherTask $task
    if(-not(Test-InstalledReleaseInputs $Bundle $Record)){throw 'The preserved installation has missing or inconsistent current release inputs; bootstrap was refused.'}
    $recordPath=Convert-WindowsPathToWsl $script:RecordPath 'Convert preserved owner record'
    $bundlePath=Convert-WindowsPathToWsl $Bundle.Root 'Convert preserved bundle'
    Invoke-UpdateHelper 'repair' @('--record',$recordPath,'--old-bundle',$bundlePath,'--candidate',$bundlePath) 1800|Out-Null
    Say 'verified or started the current services using the preserved source mapping.'
}
function Do-UpdateRelease {
    $preflightPhase='owner'
    try {
    if($ExpectedInstallationId -notmatch '^[0-9a-f-]{36}$'){throw 'UpdateRelease requires the canonical deployment command and -ExpectedInstallationId.'}
    $record=Verify-Owner
    if($record.installation_id -cne $ExpectedInstallationId){throw 'Expected installation ID differs from the owned installation.'}
    if($record.mode -cne 'full'){throw 'Maintenance update requires the existing Full installation.'}
    $preflightPhase='prior-bundle'
    $prior=Verify-Bundle $record.bundle_path;Assert-RecordedBundle $prior $record
    $preflightPhase='candidate-bundle'
    $candidate=Verify-Bundle $BundlePath
    if($candidate.Metadata.bundle_mode -cne 'full' -or -not $candidate.Metadata['qualification_cpu_full']){throw 'Candidate lacks canonical CPU/full qualification; use scripts/release.py deploy-windows.'}
    if($candidate.Metadata.image_postgres -cne $record.image_postgres -or $candidate.Metadata.image_ref_postgres -cne $record.image_ref_postgres){throw 'Maintenance update cannot change the PostgreSQL engine.'}
    $preflightPhase='source-state'
    $state=Get-SourceRepairState $prior
    if(-not $state.complete -or -not $state.sources_ready -or -not $state.root_shared -or -not $state.docker_active){throw 'Existing startup/source resources are incomplete; this update cannot bootstrap or prepare sources.'}
    $preflightPhase='launcher'
    $task=Get-ScheduledTask -TaskName $script:Task -ErrorAction SilentlyContinue
    if(-not $task -or $task.State -ne 'Running'){throw 'The existing hidden keepalive must be Running before maintenance.'}
    Assert-LauncherTask $task
    $script:CurrentMode='full'
    $preflightPhase='paths'
    $arguments=@('--record',(Convert-WindowsPathToWsl $script:RecordPath 'Convert update owner record'),'--old-bundle',(Convert-WindowsPathToWsl $prior.Root 'Convert prior bundle'),'--candidate',(Convert-WindowsPathToWsl $candidate.Root 'Convert candidate bundle'))
    $snapshot=$null;$published=$false;$restoreError=$null;$publicationAttempted=$false
    $recordBytes=[IO.File]::ReadAllBytes($script:RecordPath)
    } catch {
        [Console]::Error.WriteLine("COGNITA_UPDATE_PHASE preflight-$preflightPhase failed")
        throw
    }
    [Console]::Error.WriteLine('COGNITA_UPDATE_PHASE preflight-paths passed')
    try {
        # begin admits only the recorded or this exact candidate selection;
        # it intentionally runs before ordinary fully-current image validation.
        try {
        $snapshot=Read-RequiredLastLine (Invoke-UpdateHelper 'begin' $arguments 180) 'Capture protected update snapshot'
        } catch {
            [Console]::Error.WriteLine('COGNITA_UPDATE_PHASE preflight-begin failed')
            throw
        }
        Invoke-UpdateHelper 'apply' @('--snapshot',$snapshot) 14400|Out-Null
        $task=Get-ScheduledTask -TaskName $script:Task -ErrorAction SilentlyContinue
        if(-not $task -or $task.State -ne 'Running'){throw 'Packaged verification passed but the hidden keepalive is no longer Running.'}
        Assert-LauncherTask $task
        $next=$record|ConvertTo-Json -Depth 12|ConvertFrom-Json
        foreach($key in @('version','commit','image_ref_cognita_cpu','image_cognita_cpu','image_ref_workspace_runtime','image_workspace_runtime')){$next.$key=$candidate.Metadata[$key]}
        $next.bundle_path=$candidate.Root;$next.bundle_sha256=$candidate.Sum;$next.updated_utc=[DateTime]::UtcNow.ToString('o')
        $publicationAttempted=$true
        Save-Record $next
        $published=$true
        Say "deployed image pair version=$($next.version) source=$($next.commit); packaged Workspace operation and cleanup passed. Public connector acceptance follows separately."
    } finally {
        if($snapshot){
            try {
                if(-not $published){
                    if($publicationAttempted){
                        try{Restore-UpdateRecordBytes $recordBytes}
                        catch{$restoreError=$_}
                    }
                    try{Invoke-UpdateHelper 'rollback' @('--snapshot',$snapshot) 7200|Out-Null}
                    catch{$restoreError=$_}
                    if($restoreError){Invoke-UpdateHelper 'stop-pair' @('--snapshot',$snapshot) 180|Out-Null}
                }
            } finally {Invoke-UpdateHelper 'cleanup' @('--snapshot',$snapshot) 60|Out-Null}
            if($restoreError){throw $restoreError}
        }
    }
}
function Restore-UpdateRecordBytes([byte[]]$Bytes) {
    $temporary=Join-Path $script:Root ('.update-record-'+[guid]::NewGuid().ToString('N')+'.tmp')
    try {
        [IO.File]::WriteAllBytes($temporary,$Bytes);Secure-Path $temporary
        [IO.File]::Move($temporary,$script:RecordPath,$true);Secure-Path $script:RecordPath
        if([Convert]::ToBase64String([IO.File]::ReadAllBytes($script:RecordPath)) -cne [Convert]::ToBase64String($Bytes)){throw 'Prior owner record bytes could not be restored.'}
    } finally {
        if(Test-Path -LiteralPath $temporary){Remove-Item -LiteralPath $temporary -Force}
        if(Test-Path -LiteralPath $temporary){throw 'Owned record restoration temporary file remains.'}
    }
}

$mutex=[Threading.Mutex]::new($false,'Local\Cognita-Windows-Installer');$owned=$false
try {
    try { $owned = $mutex.WaitOne([TimeSpan]::Zero) }
    catch [Threading.AbandonedMutexException] { $owned = $true }
    if(-not $owned){throw 'Another Cognita-Windows lifecycle action is active.'}
    switch($Action){'Install'{Do-Install};'Status'{Do-Status};'Stop'{Do-Stop};'Start'{Do-Start};'Repair'{Do-Repair};'Reset-State'{Do-Reset};'Uninstall'{Do-Uninstall};'UpdateRelease'{Do-UpdateRelease}}
} catch {
    $invocation=$_.InvocationInfo
    $source=if($invocation.ScriptName){[IO.Path]::GetFileName($invocation.ScriptName)}else{'Install-CognitaWindows.ps1'}
    $line=if($invocation.ScriptLineNumber){$invocation.ScriptLineNumber}else{'unknown'}
    $frames=@($_.ScriptStackTrace -split "`r?`n" | ForEach-Object { if($_ -match '^\s*at\s+([^,]+)'){"at $($Matches[1].Trim())"} })
    $context=if($frames.Count){$frames -join ' <- '}else{'no PowerShell call frames'}
    Write-Error ($_.Exception.Message+"`nSource: ${source}:$line ($context)`nInspect current ownership with: .\Install-CognitaWindows.ps1 -Action Status");exit 1
}
finally {$script:AdminPassword=$null;$script:AdminUsername=$null;$script:AdminCsrf=$null;$script:AdminCredential=$null;if($owned){$mutex.ReleaseMutex()};$mutex.Dispose()}
