# test_paths_fstab.ps1 - path conversion, fstab rewrite, root validation (design 5.5, 14.2)
. (Join-Path $PSScriptRoot '..\CognitaWin.ps1') -NoMain
. (Join-Path $PSScriptRoot '_harness.ps1')

# ---- fstab source and mount path conversion -----------------------------------------
Test-Case 'fstab source: forward slashes, space becomes \040, dots kept' {
    Assert-Equal 'C:/Users/me/My\040Docs' (ConvertTo-FstabSource 'C:\Users\me\My Docs') 'space'
    Assert-Equal 'D:/a.b/c.d.e' (ConvertTo-FstabSource 'D:\a.b\c.d.e') 'dots'
    Assert-Equal 'C:/Users/me/Docs' (ConvertTo-FstabSource 'C:\Users\me\Docs\') 'trailing backslash dropped'
}

Test-Case 'fstab source: non-ASCII names are carried as they are (proven S3)' {
    $name = (Get-Utf8 0x0414, 0x043E, 0x043A, 0x0443, 0x043C, 0x0435, 0x043D, 0x0442, 0x044B) + ' ' + (Get-Utf8 0xDC) + 'n' + (Get-Utf8 0xEF) + 'code'
    $src = ConvertTo-FstabSource ('C:\Users\me\' + $name)
    Assert-Equal ('C:/Users/me/' + ($name -replace ' ', '\040')) $src 'unicode kept, only the space escaped'
}

Test-Case 'fstab source: a tab or newline cannot be written and is refused' {
    Assert-Throws { ConvertTo-FstabSource "C:\a`tb" } 'tab or a line break' 'tab'
    Assert-Throws { ConvertTo-FstabSource "C:\a`nb" } 'tab or a line break' 'newline'
}

Test-Case 'WSL mount path for a Windows path (progress file)' {
    Assert-Equal '/mnt/c/Users/me/x y/p.jsonl' (ConvertTo-WslMntPath 'C:\Users\me\x y\p.jsonl') 'lower-case drive, no escaping'
    Assert-Equal '/mnt/d/a' (ConvertTo-WslMntPath 'D:\a') 'other drive'
}

Test-Case 'fstab line: the proven shape with nofail AND shared (design 18.1 rule 1)' {
    Assert-Equal 'C:/Users/me/My\040Docs /mnt/cognita-roots/1 drvfs uid=1000,gid=1000,noatime,nofail,shared 0 0' (Get-FstabLineForRoot -WindowsPath 'C:\Users\me\My Docs' -N 1) 'line'
    Assert-Match (Get-FstabLineForRoot -WindowsPath 'D:\x' -N 3) ' drvfs uid=1000,gid=1000,noatime,nofail,shared 0 0$' 'every root gets the same options, only the source and mount point differ'
}

Test-Case 'fstab rewrite: a line from before `shared` (design 18.1 rule 5) is replaced in place, so the next Setup run repairs it' {
    $old = 'C:/Docs /mnt/cognita-roots/1 drvfs uid=1000,gid=1000,noatime,nofail 0 0'
    $line = Get-FstabLineForRoot -WindowsPath 'C:\Docs' -N 1
    $u = Update-FstabText -ExistingText ("/dev/sda / ext4 defaults 0 1`n" + $old + "`n") -MountPoint '/mnt/cognita-roots/1' -NewLine $line
    Assert-Equal 'replaced' $u.Action 'the old shape counts as different (the caller then restarts the distro)'
    Assert-Equal ("/dev/sda / ext4 defaults 0 1`n" + $line + "`n") $u.Text 'same folder, now with shared'
    Assert-Match $u.Text 'nofail,shared 0 0' 'shared present'
}

# ---- fstab rewrite ------------------------------------------------------------------------
Test-Case 'fstab rewrite: foreign lines kept byte for byte, ours appended once' {
    $orig = "# /etc/fstab: static file system information`nLABEL=cloudimg-rootfs / ext4 defaults 0 1`r`n`n#comment /mnt/cognita-roots/1 not a real line`n/dev/sdb1 /data ext4 defaults 0 2   `n"
    $line = Get-FstabLineForRoot -WindowsPath 'C:\Docs' -N 1
    $u = Update-FstabText -ExistingText $orig -MountPoint '/mnt/cognita-roots/1' -NewLine $line
    Assert-Equal 'added' $u.Action 'action'
    Assert-Equal ($orig + $line + "`n") $u.Text 'original bytes untouched, our line appended, newline-terminated'
}

Test-Case 'fstab rewrite: idempotent, a second run changes nothing and never duplicates' {
    $line = Get-FstabLineForRoot -WindowsPath 'C:\Docs' -N 1
    $u1 = Update-FstabText -ExistingText "/dev/sda / ext4 defaults 0 1`n" -MountPoint '/mnt/cognita-roots/1' -NewLine $line
    $u2 = Update-FstabText -ExistingText $u1.Text -MountPoint '/mnt/cognita-roots/1' -NewLine $line
    Assert-Equal 'unchanged' $u2.Action 'unchanged'
    Assert-Equal $u1.Text $u2.Text 'same text'
    Assert-Equal 1 ([regex]::Matches($u2.Text, '/mnt/cognita-roots/1 ')).Count 'exactly one line for the mount point'
}

Test-Case 'fstab rewrite: an existing line for the mount point is replaced only when it differs' {
    $old = 'C:/Old /mnt/cognita-roots/1 drvfs uid=1000,gid=1000,noatime 0 0'
    $line = Get-FstabLineForRoot -WindowsPath 'C:\New' -N 1
    $text = "a / b c d e`n" + $old + "`nz /z zz d 0 0`n"
    $u = Update-FstabText -ExistingText $text -MountPoint '/mnt/cognita-roots/1' -NewLine $line
    Assert-Equal 'replaced' $u.Action 'action'
    Assert-Equal ("a / b c d e`n" + $line + "`nz /z zz d 0 0`n") $u.Text 'replaced in place, neighbors untouched'
}

Test-Case 'fstab rewrite: duplicate lines for one mount point collapse to one' {
    $l = 'C:/A /mnt/cognita-roots/1 drvfs uid=1000,gid=1000,noatime,nofail 0 0'
    $u = Update-FstabText -ExistingText ($l + "`n" + $l + "`n") -MountPoint '/mnt/cognita-roots/1' -NewLine $l
    Assert-Equal ($l + "`n") $u.Text 'one line'
    Assert-Equal 'replaced' $u.Action 'reported as a change'
}

Test-Case 'fstab rewrite: identified by field 2, so /mnt/cognita-roots/10 and other roots are foreign to root 1' {
    $other2 = 'C:/Two /mnt/cognita-roots/2 drvfs uid=1000,gid=1000,noatime,nofail 0 0'
    $other10 = 'C:/Ten /mnt/cognita-roots/10 drvfs uid=1000,gid=1000,noatime,nofail 0 0'
    $line = Get-FstabLineForRoot -WindowsPath 'C:\One' -N 1
    $u = Update-FstabText -ExistingText ($other2 + "`n" + $other10 + "`n") -MountPoint '/mnt/cognita-roots/1' -NewLine $line
    Assert-Equal ($other2 + "`n" + $other10 + "`n" + $line + "`n") $u.Text 'other roots kept, root 1 added'
}

Test-Case 'fstab rewrite: text with no final newline gets one only where a line is added' {
    $line = Get-FstabLineForRoot -WindowsPath 'C:\One' -N 1
    $u = Update-FstabText -ExistingText '/dev/sda / ext4 defaults 0 1' -MountPoint '/mnt/cognita-roots/1' -NewLine $line
    Assert-Equal ("/dev/sda / ext4 defaults 0 1`n" + $line + "`n") $u.Text 'terminated'
    $u2 = Update-FstabText -ExistingText '' -MountPoint '/mnt/cognita-roots/1' -NewLine $line
    Assert-Equal ($line + "`n") $u2.Text 'empty file'
}

# ---- the roots folder's shared bind (design 22.14, P4) ---------------------------------------
Test-Case 'base line: the roots folder bound onto itself, shared' {
    Assert-Equal '/mnt/cognita-roots /mnt/cognita-roots none bind,shared 0 0' (Get-FstabBaseLine) 'line'
}

Test-Case 'base line: added before the first root line, every other line kept byte for byte' {
    $r1 = Get-FstabLineForRoot -WindowsPath 'C:\One' -N 1
    $r2 = Get-FstabLineForRoot -WindowsPath 'C:\Two' -N 2
    $orig = "LABEL=x / ext4 defaults 0 1`r`n" + $r1 + "`n/dev/sdb1 /data ext4 defaults 0 2`n" + $r2 + "`n"
    $u = Update-FstabBaseText -ExistingText $orig -NewLine (Get-FstabBaseLine)
    Assert-Equal 'added' $u.Action 'action'
    Assert-Equal ("LABEL=x / ext4 defaults 0 1`r`n" + (Get-FstabBaseLine) + "`n" + $r1 + "`n/dev/sdb1 /data ext4 defaults 0 2`n" + $r2 + "`n") $u.Text 'base right before root 1, nothing else moved'
}

Test-Case 'base line: with no root line yet it is appended, and a root line added later lands after it' {
    $u = Update-FstabBaseText -ExistingText '/dev/sda / ext4 defaults 0 1' -NewLine (Get-FstabBaseLine)
    Assert-Equal ("/dev/sda / ext4 defaults 0 1`n" + (Get-FstabBaseLine) + "`n") $u.Text 'appended, newline-terminated'
    $r1 = Get-FstabLineForRoot -WindowsPath 'C:\One' -N 1
    $u2 = Update-FstabText -ExistingText $u.Text -MountPoint '/mnt/cognita-roots/1' -NewLine $r1
    Assert-Equal ("/dev/sda / ext4 defaults 0 1`n" + (Get-FstabBaseLine) + "`n" + $r1 + "`n") $u2.Text 'root after base'
    $u3 = Update-FstabBaseText -ExistingText '' -NewLine (Get-FstabBaseLine)
    Assert-Equal ((Get-FstabBaseLine) + "`n") $u3.Text 'empty file'
}

Test-Case 'base line: already in place is unchanged (its CR kept); after the roots, duplicated or different it is replaced' {
    $b = Get-FstabBaseLine
    $r1 = Get-FstabLineForRoot -WindowsPath 'C:\One' -N 1
    $good = "/dev/sda / ext4 defaults 0 1`n" + $b + "`r`n" + $r1 + "`n"
    $u = Update-FstabBaseText -ExistingText $good -NewLine $b
    Assert-Equal 'unchanged' $u.Action 'in place'
    Assert-Equal $good $u.Text 'same bytes'
    $after = $r1 + "`n" + $b + "`n"
    $u2 = Update-FstabBaseText -ExistingText $after -NewLine $b
    Assert-Equal 'replaced' $u2.Action 'after the roots: a bind made then would hide them'
    Assert-Equal ($b + "`n" + $r1 + "`n") $u2.Text 'moved before root 1'
    $u3 = Update-FstabBaseText -ExistingText ($b + "`n" + $b + "`n" + $r1 + "`n") -NewLine $b
    Assert-Equal 'replaced' $u3.Action 'duplicate'
    Assert-Equal ($b + "`n" + $r1 + "`n") $u3.Text 'one line'
    $u4 = Update-FstabBaseText -ExistingText ('/mnt/cognita-roots /mnt/cognita-roots none bind 0 0' + "`n" + $r1 + "`n") -NewLine $b
    Assert-Equal 'replaced' $u4.Action 'different options'
    Assert-Equal ($b + "`n" + $r1 + "`n") $u4.Text 'rewritten'
}

Test-Case 'base line: a comment naming the folder and /mnt/cognita-roots-old are not ours' {
    $b = Get-FstabBaseLine
    $orig = "# /mnt/cognita-roots /mnt/cognita-roots none bind 0 0`nX /mnt/cognita-roots-old none bind 0 0`n"
    $u = Update-FstabBaseText -ExistingText $orig -NewLine $b
    Assert-Equal 'added' $u.Action 'action'
    Assert-Equal ($orig + $b + "`n") $u.Text 'both kept, base appended'
}

Test-Case 'Write-FstabViaRoot -Base: writes the base line through the same heredoc script' {
    $r1 = Get-FstabLineForRoot -WindowsPath 'C:\One' -N 1
    Add-ExtRule 'cat /etc/fstab' (New-ExtResult -Stdout ("/dev/sda / ext4 defaults 0 1`n" + $r1 + "`n"))
    Add-ExtRule '-u root --exec sh -s' (New-ExtResult)
    $s = New-TestSettings -RootPaths @()
    Assert-Equal 'added' (Write-RootsBaseFstabLine -Settings $s) 'action'
    $w = (Get-ExtCallsMatching 'sh -s')[0]
    Assert-True ($w.StdinText.Contains("/dev/sda / ext4 defaults 0 1`n" + (Get-FstabBaseLine) + "`n" + $r1 + "`n")) 'base before root 1'
    Assert-Match (Get-LogText) 'roots base /mnt/cognita-roots: fstab line action=added' 'logged'
}

Test-Case 'Write-FstabViaRoot: reads, sends the heredoc script as root, mv on the same filesystem' {
    $line = Get-FstabLineForRoot -WindowsPath 'C:\One' -N 1
    Add-ExtRule 'wsl\.exe -d Cognita -u root --exec cat /etc/fstab' (New-ExtResult -Stdout "/dev/sda / ext4 defaults 0 1`r`n")
    Add-ExtRule 'wsl\.exe -d Cognita -u root --exec sh -s' (New-ExtResult)
    $s = New-TestSettings -RootPaths @()
    $action = Write-FstabViaRoot -Settings $s -MountPoint '/mnt/cognita-roots/1' -Line $line
    Assert-Equal 'added' $action 'action'
    $w = (Get-ExtCallsMatching 'sh -s')[0]
    Assert-Match $w.StdinText "cat > /etc/fstab.cognita-new <<'COGNITA_EOF_[0-9a-f]{32}'" 'quoted heredoc'
    Assert-True ($w.StdinText.Contains("/dev/sda / ext4 defaults 0 1`r`n" + $line + "`n")) 'foreign line kept with its CR, ours appended'
    Assert-Match $w.StdinText 'mv -f /etc/fstab.cognita-new /etc/fstab' 'temp file then mv'
    Assert-Match $w.StdinText 'chown root:root /etc/fstab.cognita-new' 'root-owned'
}

Test-Case 'Write-FstabViaRoot: unchanged text writes nothing' {
    $line = Get-FstabLineForRoot -WindowsPath 'C:\One' -N 1
    Add-ExtRule 'cat /etc/fstab' (New-ExtResult -Stdout ($line + "`n"))
    $s = New-TestSettings -RootPaths @()
    $action = Write-FstabViaRoot -Settings $s -MountPoint '/mnt/cognita-roots/1' -Line $line
    Assert-Equal 'unchanged' $action 'action'
    Assert-Equal 0 (Get-ExtCallsMatching 'sh -s').Count 'no write'
}

# ---- display text (the Linux CLI's rule) -----------------------------------------------------
Test-Case 'display text rule: $, double quote, # after a space, length, control characters' {
    Assert-True ($null -eq (Test-DisplayText 'C:\Users\me\Docs')) 'plain path ok'
    Assert-True ($null -eq (Test-DisplayText 'C:\a#b')) '# not after whitespace is fine'
    Assert-Match (Test-DisplayText 'C:\cost$') '\$, a double quote, or a # after a space' 'dollar'
    Assert-Match (Test-DisplayText 'C:\a"b') '\$, a double quote, or a # after a space' 'quote'
    Assert-Match (Test-DisplayText 'C:\a #b') '\$, a double quote, or a # after a space' 'hash after space'
    Assert-Match (Test-DisplayText ('C:\' + ('x' * 400))) 'longer than' '401 chars'
    Assert-True ($null -eq (Test-DisplayText ('C:\' + ('x' * 397)))) '400 chars ok'
    Assert-Match (Test-DisplayText "C:\a$([char]7)b") 'control character' 'control char'
    Assert-Match (Test-DisplayText '') 'Choose a folder' 'empty'
}

# ---- root validation -----------------------------------------------------------------------
Test-Case 'root: an ordinary folder passes; a non-ASCII folder passes' {
    $s = New-TestSettings
    $d = New-Dir 'Projects'
    $v = Test-RootPath -Path $d -Settings $s
    Assert-True $v.Ok ("ordinary: " + $v.Reason)
    $u = New-Dir ((Get-Utf8 0x0414, 0x043E, 0x043A) + ' ' + (Get-Utf8 0xDC) + 'n')
    Assert-True (Test-RootPath -Path $u -Settings $s).Ok 'non-ASCII'
}

Test-Case 'root: drive root, UNC, relative, missing, control characters are refused with a reason' {
    $s = New-TestSettings
    Assert-Match (Test-RootPath -Path 'C:\' -Settings $s).Reason 'not a whole drive' 'drive root'
    Assert-Match (Test-RootPath -Path 'C:' -Settings $s).Reason 'Choose a folder on one of' 'C: alone is not an absolute folder path'
    Assert-Match (Test-RootPath -Path '\\server\share\docs' -Settings $s).Reason "own drives" 'UNC'
    Assert-Match (Test-RootPath -Path 'docs\sub' -Settings $s).Reason 'Choose a folder on one of' 'relative'
    Assert-Match (Test-RootPath -Path (Join-Path $script:TestDir 'does-not-exist') -Settings $s).Reason 'does not exist' 'missing'
    Assert-Match (Test-RootPath -Path "C:\a`nb" -Settings $s).Reason 'line break or a tab' 'newline'
    Assert-Match (Test-RootPath -Path "C:\a`tb" -Settings $s).Reason 'line break or a tab' 'tab'
    Assert-Match (Test-RootPath -Path '' -Settings $s).Reason 'Choose a folder' 'empty'
}

Test-Case 'root: a mapped network drive and other non-local drives are refused' {
    $s = New-TestSettings
    $d = New-Dir 'mapped'
    $letter = $d.Substring(0, 1).ToUpper()
    $script:DriveTypes[$letter] = 'Network'
    Assert-Match (Test-RootPath -Path $d -Settings $s).Reason 'mapped network drives' 'network'
    $script:DriveTypes[$letter] = 'CDRom'
    Assert-Match (Test-RootPath -Path $d -Settings $s).Reason "own drives" 'cd-rom'
    $script:DriveTypes[$letter] = 'Removable'
    Assert-True (Test-RootPath -Path $d -Settings $s).Ok 'removable is allowed'
}

Test-Case 'root: names fstab or Compose cannot carry are refused ($, # component, # after a space)' {
    $s = New-TestSettings
    $a = New-Dir 'has$dollar'
    Assert-Match (Test-RootPath -Path $a -Settings $s).Reason '\$, a double quote, or a # after a space' 'dollar'
    $b = New-Dir 'x #hash'
    Assert-Match (Test-RootPath -Path $b -Settings $s).Reason '\$, a double quote, or a # after a space' 'space hash'
    $c = New-Dir '#leading'
    Assert-Match (Test-RootPath -Path $c -Settings $s).Reason 'starts with #' 'leading hash component'
}

Test-Case 'root: may not contain, sit inside, or equal Cognita''s own folders (data root, vhd_dir)' {
    $vhd = New-Dir 'vhd'
    $s = New-TestSettings -Vhd $vhd
    $inHome = New-Dir 'home\sub'
    Assert-Match (Test-RootPath -Path $inHome -Settings $s).Reason 'outside Cognita' 'inside the data root'
    Assert-Match (Test-RootPath -Path $env:COGNITA_HOME -Settings $s).Reason 'outside Cognita' 'the data root itself'
    Assert-Match (Test-RootPath -Path $script:TestDir -Settings $s).Reason 'outside Cognita' 'a parent of the data root'
    Assert-Match (Test-RootPath -Path $vhd -Settings $s).Reason 'outside Cognita' 'the vhd folder'
    $inVhd = New-Dir 'vhd\deeper'
    Assert-Match (Test-RootPath -Path $inVhd -Settings $s).Reason 'outside Cognita' 'inside the vhd folder'
}

Test-Case 'root: does not nest with another root, in either direction; the same folder is "existing"' {
    $one = New-Dir 'docs'
    $s = New-TestSettings -RootPaths @($one)
    $inside = New-Dir 'docs\sub'
    Assert-Match (Test-RootPath -Path $inside -Settings $s).Reason 'overlaps another projects folder' 'inside an existing root'
    $s2 = New-TestSettings -RootPaths @((New-Dir 'work\deep'))
    Assert-Match (Test-RootPath -Path (Join-Path $script:TestDir 'work') -Settings $s2).Reason 'overlaps another projects folder' 'containing an existing root'
    $again = Test-RootPath -Path ($one + '\') -Settings $s
    Assert-True $again.Ok 'the same folder is fine'
    Assert-Equal 1 $again.Existing 'and reports which root it is'
    $sibling = New-Dir 'docs2'
    Assert-True (Test-RootPath -Path $sibling -Settings $s).Ok 'a sibling with a shared name prefix is not nested'
}

Test-Case 'root: comparison ignores case' {
    $one = New-Dir 'CaseDocs'
    $s = New-TestSettings -RootPaths @($one)
    $v = Test-RootPath -Path $one.ToUpper() -Settings $s
    Assert-True $v.Ok 'same folder in another case is the same root'
    Assert-Equal 1 $v.Existing 'existing'
}

Test-Case 'root: a junction is resolved and its target is what gets validated and mounted' {
    $s = New-TestSettings
    $real = New-Dir 'real-projects'
    $link = Join-Path $script:TestDir 'link-to-projects'
    [void](New-Item -ItemType Junction -Path $link -Target $real)
    $v = Test-RootPath -Path $link -Settings $s
    Assert-True $v.Ok ("junction to an ordinary folder: " + $v.Reason)
    Assert-Equal $real $v.Path 'the resolved path is the target, not the link'
    # A junction that points INTO the data root is refused because its target is what is validated.
    $bad = Join-Path $script:TestDir 'link-to-home'
    [void](New-Item -ItemType Junction -Path $bad -Target $env:COGNITA_HOME)
    Assert-Match (Test-RootPath -Path $bad -Settings $s).Reason 'outside Cognita' 'link to the data root'
}

Test-Case 'roots verb: --validate reports ok or the reason' {
    $d = New-Dir 'okfolder'
    $o = ConvertFrom-HelperArgs @('--validate', $d)
    $r = Invoke-RootsVerb -Opts $o.Opts -Positional $o.Positional
    Assert-Equal 'ok' $r.Status 'status'
    Assert-Equal 1 $r.Values['ok'] 'ok=1'
    $o2 = ConvertFrom-HelperArgs @('--validate', 'C:\')
    $r2 = Invoke-RootsVerb -Opts $o2.Opts -Positional $o2.Positional
    Assert-Equal 'ok' $r2.Status 'an invalid folder is an answer, so the verb succeeded (design 18.5)'
    Assert-Equal 0 (Get-ExitCodeForStatus $r2.Status) 'exit 0'
    Assert-Equal 0 $r2.Values['ok'] 'ok=0'
    Assert-Match $r2.Values['reason'] 'not a whole drive' 'and the reason'
    $r3 = Invoke-RootsVerb -Opts @{} -Positional @()
    Assert-Equal 'failed' $r3.Status 'no path given is a usage error: failed'
}

Complete-Tests
