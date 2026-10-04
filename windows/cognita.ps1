# cognita.ps1 - the PowerShell entry of the "cognita" command (design section 8).
#
# bin\cognita.exe (a small console launcher) starts:
#     powershell.exe -NoProfile -ExecutionPolicy Bypass -File <app>\cognita.ps1 <arguments>
# and returns this script's exit code. This script loads CognitaWin.ps1 and runs its "cli" verb.
#
# DESIGN NOTE: the design says "calls CognitaWin.ps1 cli @args". Passing the arguments as one
# explicit array to Invoke-HelperMain does the same thing but cannot lose or re-bind arguments:
# splatting $args into a script would let PowerShell treat a Linux flag such as -n or -f as one
# of the helper's own parameters (for example -n matches -NoMain).
$cliArgs = @($args)   # captured first: dot-sourcing the helper below rebinds $args
$helper = Join-Path $PSScriptRoot 'CognitaWin.ps1'
. $helper -NoMain
exit (Invoke-HelperMain -Argv (@('cli') + $cliArgs))
