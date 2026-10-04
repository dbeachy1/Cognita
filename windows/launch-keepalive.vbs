' launch-keepalive.vbs - keeps Cognita's WSL distro running for the signed-in user.
'
' Started by the "Cognita" Scheduled Task at logon (wscript.exe //B), and by "cognita start"
' and Setup. Design: docs/DESIGN-WINDOWS-INSTALLER.md section 5.8.
'
' Loop:
'   1. If <home>\stopped exists, exit. ("cognita stop" and the uninstaller create it.)
'   2. Run   wsl.exe -d <distro> -u <user> --exec /usr/local/libexec/cognita-keepalive   hidden
'      and wait for it. That program is "exec sleep infinity", so it only returns when the
'      distro stops (for example after "wsl --shutdown") or something goes wrong.
'   3. Append a line to <home>\logs\keepalive.log, wait 5, 10, 20, 40, then 60 seconds (60
'      thereafter) and go round again. A run that lasted more than 10 minutes counts as healthy
'      and the wait starts again from 5 seconds.
'
' <home> is %COGNITA_HOME% when set, otherwise %LOCALAPPDATA%\Cognita. The distro name and the
' Linux user are read from <home>\settings.json with a regular expression (no JSON library in
' VBScript); the defaults are Cognita and cognita.
'
' Test hooks (used only by windows/tests/test_keepalive.ps1, never set in production):
'   COGNITA_WSL_EXE               program to run instead of wsl.exe
'   COGNITA_KEEPALIVE_FAKE=1      do not really sleep (the wait is logged instead), and take the
'                                 length of a run from <home>\fake-run-seconds.txt so a test can
'                                 drive the backoff with a fake clock and no real waiting.
Option Explicit

Dim fso, shell, env, home, settingsPath, stoppedPath, logsDir, logPath
Dim distro, linuxUser, wslExe, fake, cmd
Dim delay, startedAt, ranSeconds, exitCode

Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")
Set env = shell.Environment("PROCESS")

home = env("COGNITA_HOME")
If home = "" Then home = env("LOCALAPPDATA") & "\Cognita"
settingsPath = home & "\settings.json"
stoppedPath = home & "\stopped"
logsDir = home & "\logs"
logPath = logsDir & "\keepalive.log"
fake = (env("COGNITA_KEEPALIVE_FAKE") = "1")
wslExe = env("COGNITA_WSL_EXE")
If wslExe = "" Then wslExe = "wsl.exe"

Function Pad2(n)
    Pad2 = Right("0" & CStr(n), 2)
End Function

Function Stamp()
    Dim d
    d = Now
    Stamp = Year(d) & "-" & Pad2(Month(d)) & "-" & Pad2(Day(d)) & " " & Pad2(Hour(d)) & ":" & Pad2(Minute(d)) & ":" & Pad2(Second(d))
End Function

Sub LogLine(text)
    Dim ts
    On Error Resume Next
    If Not fso.FolderExists(logsDir) Then fso.CreateFolder logsDir
    Set ts = fso.OpenTextFile(logPath, 8, True)
    If Err.Number = 0 Then
        ts.WriteLine Stamp() & " " & text
        ts.Close
    End If
    Err.Clear
    On Error GoTo 0
End Sub

Function ReadSetting(name, fallback)
    Dim ts, text, re, m
    ReadSetting = fallback
    If Not fso.FileExists(settingsPath) Then Exit Function
    On Error Resume Next
    Set ts = fso.OpenTextFile(settingsPath, 1, False)
    If Err.Number <> 0 Then
        Err.Clear
        Exit Function
    End If
    text = ts.ReadAll
    ts.Close
    On Error GoTo 0
    Set re = CreateObject("VBScript.RegExp")
    re.Pattern = """" & name & """\s*:\s*""([^""]+)"""
    Set m = re.Execute(text)
    If m.Count > 0 Then ReadSetting = m(0).SubMatches(0)
End Function

Function ReadFakeRunSeconds()
    Dim ts, p
    p = home & "\fake-run-seconds.txt"
    ReadFakeRunSeconds = 0
    If fso.FileExists(p) Then
        Set ts = fso.OpenTextFile(p, 1, False)
        ReadFakeRunSeconds = CLng(Trim(ts.ReadAll))
        ts.Close
    End If
End Function

Sub Pause(seconds)
    If fake Then
        LogLine "fake pause " & seconds & "s"
    Else
        WScript.Sleep seconds * 1000
    End If
End Sub

distro = ReadSetting("distro", "Cognita")
linuxUser = ReadSetting("linux_user", "cognita")
cmd = """" & wslExe & """ -d " & distro & " -u " & linuxUser & " --exec /usr/local/libexec/cognita-keepalive"
LogLine "keepalive loop started distro=" & distro & " user=" & linuxUser

delay = 5
Do
    If fso.FileExists(stoppedPath) Then
        LogLine "stopped flag present; exiting"
        WScript.Quit 0
    End If
    startedAt = Now
    exitCode = shell.Run(cmd, 0, True)
    If fake Then
        ranSeconds = ReadFakeRunSeconds()
    Else
        ranSeconds = DateDiff("s", startedAt, Now)
    End If
    If fso.FileExists(stoppedPath) Then
        LogLine "keepalive ended (exit " & exitCode & ", ran " & ranSeconds & "s) and the stopped flag is present; exiting"
        WScript.Quit 0
    End If
    If ranSeconds > 600 Then delay = 5
    LogLine "keepalive ended (exit " & exitCode & ", ran " & ranSeconds & "s); relaunching in " & delay & "s"
    Pause delay
    delay = delay * 2
    If delay > 60 Then delay = 60
Loop
