// cognita.exe: the `cognita` command on a Windows PC (docs/DESIGN-WINDOWS-INSTALLER.md section 8).
//
// A small console launcher, not a batch file. Spike S7 proved that a .cmd wrapper makes Ctrl+C in
// `cognita logs -f` ask "Terminate batch job (Y/N)?", and that a launcher like this one does not:
// it ignores Ctrl+C and Ctrl+Break itself (the child still gets them, because it shares the
// console), waits for the child, and returns the child's exit code.
//
// It runs   powershell.exe -NoProfile -ExecutionPolicy Bypass -File "<app>\cognita.ps1" <args>
// with each argument quoted by the Windows command-line rules. -ExecutionPolicy Bypass matters: a
// .ps1 on PATH is blocked by Windows 11's default execution policy when started from a
// PowerShell prompt, and this executable is what makes `cognita` work from every terminal.
//
// Layout: %LOCALAPPDATA%\Cognita\bin\cognita.exe (this file, built by windows\build_setup.py) and
// %LOCALAPPDATA%\Cognita\app\cognita.ps1 are siblings under one folder, so the script is found
// relative to this executable's own location and nothing is hard-coded.
//
// Built with the C# compiler that ships with .NET Framework 4.x on every Windows:
//   %WINDIR%\Microsoft.NET\Framework64\v4.0.30319\csc.exe /nologo /target:exe /out:cognita.exe cognita.cs
using System;
using System.Diagnostics;
using System.IO;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Text;

static class Launcher {
    delegate bool HandlerRoutine(uint ctrlType);

    [DllImport("kernel32.dll")]
    static extern bool SetConsoleCtrlHandler(HandlerRoutine handler, bool add);

    // CTRL_C_EVENT = 0, CTRL_BREAK_EVENT = 1. Anything else (close, logoff, shutdown) keeps its
    // default handling, so closing the terminal window still ends the launcher.
    static readonly HandlerRoutine KeepRunning = ctrlType => ctrlType == 0 || ctrlType == 1;

    // Quotes one argument the way CommandLineToArgvW reads it back: an argument with no space,
    // tab or double quote passes through; anything else is wrapped in quotes, with every
    // backslash run that precedes a quote (or the closing quote) doubled and each inner quote
    // escaped.
    static string Quote(string arg) {
        if (arg.Length > 0 && arg.IndexOfAny(new[] { ' ', '\t', '"' }) < 0) {
            return arg;
        }
        var sb = new StringBuilder("\"");
        int backslashes = 0;
        foreach (char c in arg) {
            if (c == '\\') {
                backslashes++;
                continue;
            }
            if (c == '"') {
                sb.Append('\\', backslashes * 2 + 1);
                sb.Append('"');
                backslashes = 0;
                continue;
            }
            sb.Append('\\', backslashes);
            backslashes = 0;
            sb.Append(c);
        }
        sb.Append('\\', backslashes * 2);
        sb.Append('"');
        return sb.ToString();
    }

    static int Main(string[] args) {
        string exeDir = Path.GetDirectoryName(Assembly.GetExecutingAssembly().Location);
        string script = Path.GetFullPath(Path.Combine(exeDir, @"..\app\cognita.ps1"));
        if (!File.Exists(script)) {
            Console.Error.WriteLine("Cognita is not installed correctly: " + script + " is missing. Run Cognita Setup again.");
            return 1;
        }

        SetConsoleCtrlHandler(KeepRunning, true);

        var psi = new ProcessStartInfo(Environment.ExpandEnvironmentVariables(
            @"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe"));
        var sb = new StringBuilder("-NoProfile -ExecutionPolicy Bypass -File ");
        sb.Append(Quote(script));
        foreach (string a in args) {
            sb.Append(' ').Append(Quote(a));
        }
        psi.Arguments = sb.ToString();
        psi.UseShellExecute = false;
        // Started from a PowerShell 7 window, the child would inherit 7's PSModulePath and
        // Windows PowerShell 5.1 would then try to load 7's copies of its built-in modules
        // (Microsoft.PowerShell.Security failed to load that way: Get-AuthenticodeSignature was
        // unusable). Without the variable, 5.1 builds its own default module path.
        psi.EnvironmentVariables.Remove("PSModulePath");
        using (var p = Process.Start(psi)) {
            p.WaitForExit();
            return p.ExitCode;
        }
    }
}
