; Cognita Setup (Inno Setup 6.3 or later). Design: docs/DESIGN-WINDOWS-INSTALLER.md sections 4, 5.6, 7, 8, 9, 11.
;
; Build it with windows\build_setup.py, which passes these /D defines (all required):
;   Version, Revision                 the Cognita version and this Setup build's revision
;   ImagePath, ImageSha256, ImageSize the Linux image tarball (cognita-wsl-<v>.tar.gz)
;   SrcPath, SrcSha256, SrcSize       the source tarball for updates (cognita-src-<v>.tar.gz)
;   SizeCognitaCpu, SizeWorkspaceRuntime, SizeToolbox   compressed container image sizes (bytes)
;   SizeCognitaNvidia                 the NVIDIA Cognita image's compressed size (bytes); 0 = this build has no
;                                     NVIDIA image (design 22.7), so the Acceleration page offers no GPU choice
;   LauncherExe                      the compiled cognita.exe (windows\launcher\cognita.cs)
;   WindowsDir                        the folder holding CognitaWin.ps1, cognita.ps1, launch-keepalive.vbs
;   OutputDir                         where Cognita-Setup-<version>-r<Revision>.exe is written
;
; THE HELPER CALL CONTRACT THIS FILE ASSUMES (windows\CognitaWin.ps1, docs section 5.1 and 5.2).
; Section 5.2's verb table names the verbs and result keys but not every command-line option, and
; section 7.2 does not name the `state` keys that carry the saved values. This file therefore uses
; the following; each is built in ONE place (the Args* functions and State* readers below) so a
; rename in the helper is a one-line change here.
;   state                                     result keys (design 18.2): installed (1 iff the distro is ours AND
;                                             the Linux install completed), state (new|import-pending|installed|
;                                             uninstalled, design 19.4 item 23; `none` when there is no settings
;                                             file at all. The older list none|installing|installed is superseded),
;                                             distro (present|absent), owned (0 = a distro of
;                                             that name that is not ours), linux_version (the Setup version
;                                             that last finished cognita install/update), admin_user, resume,
;                                             running, plus, for the saved-value pages
;                                             and the uninstall form: root1, mcp_port, admin_port, workspace,
;                                             vhd_dir, vhd_bytes (all optional; a missing key falls back to a
;                                             default), and funnel_port (design 19.2 item 8: the public HTTPS
;                                             port of the Funnel Setup turned on; absent or empty when none is
;                                             recorded; ReadState reads it into StFunnelPort). Design 19.11 R7:
;                                             reported only while Tailscale still serves that Funnel (the
;                                             helper clears a stale record first). The install refusal
;                                             port-in-use-by-funnel reports the old MCP port as
;                                             funnel_target, never as funnel_port (R6). Design 22.4: acceleration
;                                             (cpu|amd|nvidia, or empty when unknown: an install from before 15.1;
;                                             ReadState reads it into StAcceleration).
;                                             Setup picks ONE mode from them (PickMode):
;                                               fresh   no owned distro
;                                               finish  owned distro, installed=0: every page as fresh, but the
;                                                       data location is fixed to vhd_dir; image AND source
;                                                       tarball are always unpacked and passed
;                                               repair  installed=1, state not `uninstalled`, linux_version =
;                                                       this Setup's version: folder, user and data location
;                                                       read-only, ONE password (Setup uses it to check Cognita
;                                                       afterwards); --admin-user is NOT passed
;                                               update  installed=1, state not `uninstalled`, another version:
;                                                       as repair, but the update verb with --src
;                                               reinstall  installed=1 AND state=uninstalled (after a keep-data
;                                                       uninstall; whatever the version): as repair (the Ready page
;                                                       says "Reinstall Cognita <v> (your data is kept)"), and the
;                                                       install verb with --src, NEVER update (the helper refuses
;                                                       it: the Linux side removed its releases)
;   Result values are percent-encoded by the helper (%25 for '%', %3B for ';'); ResultValue decodes
;   them, in that one place (design 18.5).
;   check --phase preflight|final [--data-dir P --mcp-port N --admin-port N --disk-bytes N]
;                                             The preflight result also carries (design 22.2 and 22.9; CheckPreflight
;                                             reads them): nvidia=none|ok|old (ok = a card with driver 580 or newer,
;                                             or a card whose driver could not be read; old = every card seen has a
;                                             readable older driver), nvidia_name=<text>, nvidia_driver=<major.minor
;                                             or empty>, wsl_reclaim=set|unset|unreadable (an autoMemoryReclaim key
;                                             in .wslconfig). They are not warnings and never fail the check.
;   wsl-install
;   restart-for-wsl --setup-exe <path to this Setup.exe> [--now --after-pid <this Setup's process id>]
;                                             (design 19.11 R1) without --now it only records RunOnce and the
;                                             resume marker (the install flow: Inno's own Finished page then
;                                             offers the restart, through NeedRestart). With --now --after-pid
;                                             the helper starts a detached hidden powershell that waits for
;                                             that process to exit and then runs `shutdown /r /t 0`: a running
;                                             Setup would refuse a non-forced restart. Only the WSL page's
;                                             "Restart now" uses it.
;   roots --validate PATH [--data-dir P]      an invalid folder answers result=ok;ok=0;reason=<text> (exit 0);
;                                             `failed` is an internal error only. Setup also runs it from
;                                             Advanced with the new data location (design 19.7 item 16)
;   password-broker --in <pipe A> --out <pipe B>   (section 5.6; started with ewNoWait)
;   install --projects-folder P [--admin-user U] --workspace on|off --mcp-port N --admin-port N
;           --data-dir P [--image P --image-sha256 H] [--src P --src-sha256 H]
;           --setup-version V --setup-revision R --password-pipe B
;           (every install run passes --src; fresh and finish also pass --image; repair and reinstall
;           pass no --admin-user and no --image)
;           [--acceleration cpu|nvidia] [--wsl-memory-reclaim]   (design 22.3, 22.7, 22.9, 22.12 item 1:
;           --acceleration is passed by AccelArg: always on fresh and finish (cpu when the Acceleration page is
;           hidden), on repair and reinstall only when the page was shown and the user changed the radio or the
;           install is known to run on cpu or nvidia, never on update; absent = the Linux side keeps what its env
;           file says. --wsl-memory-reclaim only when the Ready page's box was shown and ticked.)
;           The result carries acceleration=<cpu|amd|nvidia> (may be absent): the Finished page's line comes only
;           from it (design 22.12 item 11). A warning of stage `acceleration` is the Linux side dropping the GPU;
;           OnHelperLine adds "To try the GPU again, run Setup again and choose NVIDIA." when it has no fix.
;           Failure reasons Setup shows like any other failure (progress message and fix): folder-not-first
;           (a folder other than root 1 once root 1 exists: "Cognita already uses <root1> as its projects
;           folder."), distro-not-ready (update, start), port-in-use-by-funnel (a new MCP port while a Funnel
;           targets the old one; design 19.2 item 8), older-setup (update refuses a downgrade; design 19.2 item 6).
;   update  --src P --src-sha256 H --setup-version V --setup-revision R --disk-bytes N --password-pipe B
;           [--wsl-memory-reclaim]                (design 22.9; no --acceleration: an update never changes it);
;           the result carries acceleration=<cpu|amd|nvidia> (may be absent), as install's does
;   remote-access --yes [--funnel-port N] --password-pipe B
;                                             a failure may carry link=<url> (reason=funnel-not-enabled):
;                                             Setup shows the link and Retry, which hands the password over
;                                             again (new broker, new pipes) and reruns the verb (design 18.4).
;                                             Any http(s):// address in a progress `message` is clickable
;                                             while it is the latest message.
;   diagnostics --setup-log <Setup's own {log} path>
;   uninstall --keep-data | --delete-data     a delete-data result carries logs_copy=<folder in %TEMP%> when logs
;                                             were copied. A delete-data run whose `wsl --unregister` failed ends
;                                             result=failed with state ALREADY saved as uninstalled (design 19.4
;                                             item 9): the uninstaller then says "Cognita was removed, but its
;                                             data could not be deleted. Logs: <path>"
; Every verb prints JSON progress lines and ends with `result=ok|failed|restart-required;k=v;...`
; and exits 0, 1 or 3010 (section 5.1).
; Setup's own log ({log}) is copied to %LOCALAPPDATA%\Cognita\logs\setup-<yyyyMMdd-HHmmss>.log in
; DeinitializeSetup, and the uninstaller's to uninstall-<stamp>.log at the end of an uninstall
; (design 18.5; after a delete-data uninstall it goes next to the helper's %TEMP% copy instead).

#ifndef Version
  #error Version is not defined. Build with windows\build_setup.py.
#endif
#ifndef Revision
  #error Revision is not defined. Build with windows\build_setup.py.
#endif
#ifndef ImagePath
  #error ImagePath is not defined. Build with windows\build_setup.py.
#endif
#ifndef ImageSha256
  #error ImageSha256 is not defined. Build with windows\build_setup.py.
#endif
#ifndef SrcPath
  #error SrcPath is not defined. Build with windows\build_setup.py.
#endif
#ifndef SrcSha256
  #error SrcSha256 is not defined. Build with windows\build_setup.py.
#endif
#ifndef SizeCognitaCpu
  #error SizeCognitaCpu is not defined. Build with windows\build_setup.py.
#endif
#ifndef SizeWorkspaceRuntime
  #error SizeWorkspaceRuntime is not defined. Build with windows\build_setup.py.
#endif
#ifndef SizeToolbox
  #error SizeToolbox is not defined. Build with windows\build_setup.py.
#endif
#ifndef SizeCognitaNvidia
  #error SizeCognitaNvidia is not defined. Build with windows\build_setup.py.
#endif
#ifndef ImageSize
  #error ImageSize is not defined. Build with windows\build_setup.py.
#endif
#ifndef LauncherExe
  #error LauncherExe is not defined. Build with windows\build_setup.py.
#endif
#ifndef WindowsDir
  #error WindowsDir is not defined. Build with windows\build_setup.py.
#endif
#ifndef OutputDir
  #error OutputDir is not defined. Build with windows\build_setup.py.
#endif

; Where users can ask for help (2026-09-29): the repository's new-issue page, public by ship
; time. Setup never sends anything itself; every place that offers Save diagnostics also says where
; the user may attach the file, and that nothing was sent. windows\CognitaWin.ps1 has the same URL.
#define SupportUrl "https://github.com/dbeachy1/Cognita/issues/new"

#pragma message "Cognita Setup {#Version} revision {#Revision}"

[Setup]
; A fixed AppId: a newer Setup over an older install is an update (section 7.3).
AppId={{17D9746C-1795-4815-9300-7ECE429CEE13}
AppName=Cognita
AppVersion={#Version}
AppVerName=Cognita {#Version}
AppPublisher=Cognita
VersionInfoVersion={#Version}.{#Revision}
VersionInfoDescription=Cognita Setup {#Version} revision {#Revision}
; Per user, no admin prompt (W1). Only WSL's own installer asks for permission, once.
PrivilegesRequired=lowest
DefaultDirName={localappdata}\Cognita\app
DisableDirPage=yes
DisableProgramGroupPage=yes
DefaultGroupName=Cognita
; Our own Ready page (Advanced, final checks) replaces Inno's.
DisableReadyPage=yes
DisableWelcomePage=no
; The Linux image is x86_64: Windows on ARM is refused (section 4.1).
ArchitecturesAllowed=x64os
ArchitecturesInstallIn64BitMode=x64os
MinVersion=10.0.22000
SetupLogging=yes
; The uninstaller writes its log always too (design 18.5 copies it beside the helper logs).
UninstallLogging=yes
ChangesEnvironment=yes
OutputDir={#OutputDir}
OutputBaseFilename=Cognita-Setup-{#Version}-r{#Revision}
; The two payload tarballs are already compressed (nocompression below); nothing else is big.
Compression=lzma2
SolidCompression=no
WizardStyle=modern
UninstallDisplayName=Cognita
; Design 19.1 item 5: one Setup at a time. A second copy (the RunOnce resume plus a double-click, say)
; gets Inno's own "already running" message instead of two installs racing each other.
SetupMutex=CognitaSetup_17D9746C
; Design 19.1 item 22 (was CloseApplications=no): replacing bin\cognita.exe fails while a terminal still
; runs `cognita logs -f`. With the Restart Manager on, Inno offers to close that program instead of
; its Retry/Ignore/Abort. The filter keeps it to executables, so it never touches a document window.
CloseApplications=yes
CloseApplicationsFilter=*.exe

[Messages]
; Inno asks this AFTER the Remove Cognita dialog (AskUninstallChoice). Its stock text, "completely
; remove Cognita and all of its components", contradicted a "keep Cognita's data" choice made a
; moment earlier (P2, 2026-09-29).
ConfirmUninstall=Remove %1 from this PC now? Your documents are never touched, and your choice about Cognita's data applies.
; Design 19.4 item 21: ONE closing box. Inno shows one of these when the uninstall ends; they carry the
; documents sentence, so the usPostUninstall message box that used to say it is gone. (A delete-data run
; whose data could not be deleted shows its own text first, from CurUninstallStepChanged.)
UninstalledAll=Cognita was removed. Your documents were not touched.
UninstalledMost=Cognita was removed. Your documents were not touched.

[Files]
; Order matters little here (SolidCompression=no). Temporary copies first: the helper must run
; from {tmp} before Inno installs anything (section 4.2). The same source is installed below.
Source: "{#WindowsDir}\CognitaWin.ps1"; Flags: dontcopy noencryption
; The payload. DestName is the name ExtractTemporaryFile takes (verified on 6.7.3: it matches
; DestName, not the source file's own name). Extracted only when a step needs them, deleted after.
Source: "{#ImagePath}"; DestName: "cognita-wsl.tar.gz"; Flags: dontcopy nocompression noencryption
Source: "{#SrcPath}"; DestName: "cognita-src.tar.gz"; Flags: dontcopy nocompression noencryption
; Installed files: app\ (Inno owns and removes it) and bin\ (the launcher).
Source: "{#WindowsDir}\CognitaWin.ps1"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#WindowsDir}\cognita.ps1"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#WindowsDir}\launch-keepalive.vbs"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#LauncherExe}"; DestDir: "{localappdata}\Cognita\bin"; DestName: "cognita.exe"; Flags: ignoreversion

[INI]
; Cognita Admin opens the Admin URL. The port is whatever Setup was given (Advanced can change it).
Filename: "{app}\CognitaAdmin.url"; Section: "InternetShortcut"; Key: "URL"; String: "{code:AdminUrl}"

[UninstallDelete]
Type: files; Name: "{app}\CognitaAdmin.url"

[Icons]
Name: "{group}\Cognita Admin"; Filename: "{app}\CognitaAdmin.url"
; -ExecutionPolicy Bypass: Windows 11's default policy blocks a local .ps1; -NoExit keeps the window open.
Name: "{group}\Cognita Status"; Filename: "{sys}\WindowsPowerShell\v1.0\powershell.exe"; Parameters: "-NoExit -NoProfile -ExecutionPolicy Bypass -File ""{app}\cognita.ps1"" status"
; Diagnostics (design 19.4 item 23): the window is SHOWN, with its progress, and stays open (-NoExit) so
; the path of the saved zip can be read. It used to run hidden, which left the user clicking an entry
; that appeared to do nothing for as long as the collection took.
; Design 19.11 R4: it runs the HUMAN launcher (cognita.ps1 -> the helper's cli verb), the same form as the
; Status entry above, not CognitaWin.ps1 directly, whose output is the machine progress lines (JSON).
Name: "{group}\Cognita Diagnostics"; Filename: "{sys}\WindowsPowerShell\v1.0\powershell.exe"; Parameters: "-NoExit -NoProfile -ExecutionPolicy Bypass -File ""{app}\cognita.ps1"" diagnostics"

[Registry]
; PATH (section 4.4): stays REG_EXPAND_SZ. Two entries so an empty PATH does not gain a leading ';'.
Root: HKCU; Subkey: "Environment"; ValueType: expandsz; ValueName: "Path"; ValueData: "{localappdata}\Cognita\bin"; Check: PathIsEmpty
Root: HKCU; Subkey: "Environment"; ValueType: expandsz; ValueName: "Path"; ValueData: "{olddata};{localappdata}\Cognita\bin"; Check: PathNeedsAppend

[Run]
; The Finished page's "Open Cognita Admin". Offered only after a successful install.
Filename: "{code:AdminUrl}"; Description: "Open Cognita Admin"; Flags: postinstall shellexec skipifsilent; Check: OpenAdminOffered

[Code]
{ ---------------------------------------------------------------------------------------------
  Windows API calls: a named pipe for the Admin password (section 5.6, proven in spike S1),
  UuidCreate for the random pipe names, GetTickCount for the elapsed clock.
  --------------------------------------------------------------------------------------------- }
type
  TUuid = record
    D1: Cardinal;
    D2: Cardinal;
    D3: Cardinal;
    D4: Cardinal;
  end;

function GetTickCount: DWORD; external 'GetTickCount@kernel32.dll stdcall';
{ Setup's own process id (design 19.11 R1): passed to `restart-for-wsl --now --after-pid` so the helper's
  detached waiter restarts Windows only after THIS process has exited. }
function GetCurrentProcessId: Cardinal; external 'GetCurrentProcessId@kernel32.dll stdcall';
{ Design 21.4 (VM proof, 2026-09-29): while ExecAndLogOutput waits for the helper, Inno disables the
  wizard window, so nothing on the Installing page could be clicked (the Skip self-tests and Copy
  buttons showed as disabled). The helper's line callback re-enables the window; Inno still pumps
  messages while it waits, so the click then reaches the button. }
{ Plain Integer/Cardinal signatures: a BOOL/HWND-typed import raised "Could not call proc" at run time. }
function EnableWindow(hWnd: Cardinal; bEnable: Integer): Integer; external 'EnableWindow@user32.dll stdcall';
function IsWindowEnabled(hWnd: Cardinal): Integer; external 'IsWindowEnabled@user32.dll stdcall';
function CreateFileW(lpFileName: String; dwDesiredAccess, dwShareMode, lpSecurityAttributes,
  dwCreationDisposition, dwFlagsAndAttributes, hTemplateFile: Cardinal): THandle;
  external 'CreateFileW@kernel32.dll stdcall';
function WriteFile(hFile: THandle; const lpBuffer: String; nNumberOfBytesToWrite: Cardinal;
  var lpNumberOfBytesWritten: Cardinal; lpOverlapped: Cardinal): BOOL;
  external 'WriteFile@kernel32.dll stdcall';
function CloseHandle(hObject: THandle): BOOL; external 'CloseHandle@kernel32.dll stdcall';
function UuidCreate(var Uuid: TUuid): Longint; external 'UuidCreate@rpcrt4.dll stdcall';
{ lpValue = 0 (a null pointer) deletes the variable from Setup's own environment. }
function SetEnvironmentVariableW(lpName: String; lpValue: Cardinal): BOOL;
  external 'SetEnvironmentVariableW@kernel32.dll stdcall';

const
  DefaultMcpPort = 8675;
  DefaultAdminPort = 8676;
  NvidiaDriverUrl = 'https://www.nvidia.com/drivers';   { design 22.1: shown, clickable, when the NVIDIA driver is older than 580 }

var
  { What `state` told us }
  StInstalled: Boolean;
  StResumeAfterWsl: Boolean;
  StDistroPresent: Boolean;       { distro=present AND ours (owned<>0): the distro Setup may work in }
  StLinuxVersion: String;         { linux_version: the Setup version that last finished cognita install/update }
  StAdminUser: String;            { admin_user: the Admin user recorded with linux_version }
  StRoot1: String;
  StVhdDir: String;
  StVhdBytes: String;
  StWorkspace: String;
  StFunnelPort: String;           { funnel_port: the public HTTPS port of a Funnel Setup turned on; '' when none is recorded (design 19.2 item 8) }
  StAcceleration: String;         { acceleration: cpu | amd | nvidia, '' when unknown (an install from before 15.1; design 22.4) }
  { What the user chose (Advanced can change ports, data location and Workspace) }
  ChosenMcpPort: Integer;
  ChosenAdminPort: Integer;
  ChosenDataDir: String;
  ChosenWorkspaceOn: Boolean;
  AdminUser: String;
  AdminPassword: String;          { memory only, cleared in DeinitializeSetup (section 5.6) }
  { What the checks told us }
  WslState: String;               { present | missing | old | '' before the first check }
  WslPageStage: Integer;          { 0 = asking to turn WSL on, 1 = asking Restart now / Later }
  PreflightWarned: Boolean;
  { What `check --phase preflight` told us about NVIDIA and WSL's file cache (design 22.2, 22.9) }
  NvState: String;                { nvidia: none | ok | old, '' before the first check }
  NvName: String;                 { nvidia_name }
  NvDriver: String;               { nvidia_driver: <major.minor> or '' }
  WslReclaim: String;             { wsl_reclaim: set | unset | unreadable, '' before the first check }
  { The Acceleration page's pick (design 22.7, 22.12 item 1) }
  ChosenAccel: String;            { cpu | nvidia }
  AccelDefaultPick: String;       { the radio the page opened with (AccelDefault), to tell a user's change from the default }
  AccelInited: Boolean;           { ChosenAccel has been set once from AccelDefault; Back and Next then keep the user's pick }
  AccelTouched: Boolean;          { the pick differs from the default the page opened with (set in the page's Next) }
  { Flow state }
  ResumeSwitch: Boolean;
  CloseQuietly: Boolean;
  InstallMode: Integer;           { ModeFresh, ModeRepair, ModeUpdate, ModeFinish or ModeReinstall (design 18.2) }
  RemoteStage: Integer;           { Remote access page: 0 = choose, 1 = failed with a link (Retry shown), 2 = turned on }
  RemoteFunnelPort: String;       { the public port the user accepted after funnel-port-busy; kept for Retry }
  RemoteLinkUrl: String;          { the link= of a failed remote-access result (design 18.4) }
  UninstLogsCopy: String;         { logs_copy of a delete-data uninstall: where the helper copied the logs }
  InstallAttempted: Boolean;
  InstallOk: Boolean;
  InstallRestart: Boolean;        { the install run needs a Windows restart (helper exit 3010): NeedRestart returns this (design 19.11 R1) }
  InstallResumeSaved: Boolean;    { restart-for-wsl recorded RunOnce and the resume marker for that restart; False = Setup will not reopen by itself }
  InstallWarnText: String;        { WarnText of the install/update run, kept for the Finished page (design 19.4 item 2): remote access runs its own helper verb, which resets WarnText }
  InstallSignInWarned: Boolean;   { the install/update run warned that the sign-in task could not be registered }
  ResultVersion: String;
  ResultWorkspace: String;        { the workspace value the install/update result (`status`) reported; '' when it reported none }
  ResultAccel: String;            { the acceleration the install/update result reported (cpu|amd|nvidia); '' when it reported none: the Finished page's only source (design 22.12 item 11) }
  PublicUrl: String;
  RemoteNote: String;
  HelperFolder: String;           { where the helper runs from: the temp folder first, the app folder after install }
  { One helper run's outcome, filled by OnHelperLine }
  LastResult: String;
  HelperExit: Integer;
  FailCount: Integer;
  FailText: String;
  FailStage: String;
  FailMessage: String;
  FailFix: String;
  WarnText: String;
  SignInWarned: Boolean;          { a warning line of stage `keepalive` arrived: the sign-in task is not in place }
  LastStatus: String;             { ok | restart-required | failed, from JudgeResult }
  RunMode: Integer;               { 0 = log only, 1 = busy marquee page, 2 = progress page }
  RunStartTick: DWORD;
  CurTitle: String;
  CurStage: String;               { stage of the latest progress line that carried one (remote.login = the Tailscale sign-in wait) }
  LastDone: Int64;                { the latest byte counts, kept while the title does not change (design 19.6 item 13) }
  LastTotal: Int64;
  LinkUrl: String;
  { Wizard pieces }
  BusyPage: TOutputMarqueeProgressWizardPage;
  ProgressPage: TOutputProgressWizardPage;
  { The Tailscale sign-in link, also as text that can be selected, plus a Copy button: the tailnet's
    owner often opens the link on another device (design 19.5 item 12). Shown while the link is. }
  ProgressLinkEdit: TNewEdit;
  ProgressCopyButton: TNewButton;
  ProgressWaitLabel: TNewStaticText;
  { Design 21.4: "Skip self-tests", shown under the bar while the Linux side is on its `proof` stage. }
  ProgressSkipButton: TNewButton;
  SkipPressed: Boolean;           { the user pressed it in this install run (the request flag was written) }
  WizardUp: Boolean;              { InitializeWizard has run: WizardForm exists (the first helper call, `state`, runs before it) }
  FinProofSkipped: Boolean;       { the install/update result carried proof=skipped: the Finished page says so }
  PageWsl: TWizardPage;
  WslLabel: TNewStaticText;
  WslRestartNow: TNewRadioButton;
  WslRestartLater: TNewRadioButton;
  PageFolder: TInputDirWizardPage;
  FolderNote: TNewStaticText;
  FolderReason: TNewStaticText;   { why Cognita cannot use the folder just typed (roots --validate ok=0) }
  PageAdmin: TInputQueryWizardPage;
  PageAccel: TWizardPage;           { Acceleration (design 22.7): after Admin sign-in, before Ready }
  AccelLabelText: TNewStaticText;
  AccelUseGpu: TNewRadioButton;
  AccelUseCpu: TNewRadioButton;
  AccelNote: TNewStaticText;        { "Setup checks the card while it installs ...", only when the GPU is offered }
  AccelLink: TNewStaticText;        { the NVIDIA driver address, only for an old driver }
  PageReady: TWizardPage;
  ReadyText: TNewStaticText;
  ReclaimBox: TNewCheckBox;         { design 22.9: let Windows take back WSL's file cache; shown only when wsl_reclaim=unset }
  ReclaimText: TNewStaticText;      { the box's caption, wrapped, beside it (a check box does not wrap its caption) }
  AdvancedButton: TNewButton;
  PageRemote: TWizardPage;
  RemoteLabel: TNewStaticText;
  RemoteSkip: TNewRadioButton;
  RemoteSetUp: TNewRadioButton;
  RemoteLinkLabel: TNewStaticText;  { the link from a failed remote-access result }
  RemoteLinkEdit: TNewEdit;         { the same link as selectable text, with a Copy button (design 19.5 item 12) }
  RemoteCopyButton: TNewButton;
  RemoteRetryButton: TNewButton;
  DiagButton: TNewButton;
  LogLink: TNewStaticText;
  ReportLink: TNewStaticText;       { "Report a problem": opens the support page, sends nothing }
  FinDiagZip: String;               { the diagnostics zip saved automatically after an install failure }
  { Finished page, success: clickable Admin / MCP / public addresses, the connector hint and the commands }
  FinAdminPre, FinAdminLink, FinAdminPost: TNewStaticText;
  FinMcpPre, FinMcpLink: TNewStaticText;
  FinPubPre, FinPubLink: TNewStaticText;
  FinConnectLabel: TNewStaticText;
  FinMemo: TNewMemo;
  { Uninstall }
  UninstallDeleteData: Boolean;
  UninstallDeleteFailed: Boolean; { a delete-data uninstall whose helper run failed: its data could not be deleted (design 19.4 item 9) }

{ The PURE block (no wizard objects, no globals of this file) lives in pure.iss so a test harness can
  compile it alone (design 19.10: tests\pure_tests.iss, run by tests\test_setup_pure.py). It defines
  the Mode* constants, PickMode, ResultValue, JsonField, RedactToken, the version compare and the other
  helpers that need no wizard. It is #included here, in [Code], where the block used to be inline. }
#include "pure.iss"

function HasSwitch(const Name: String): Boolean;
var
  I: Integer;
begin
  Result := False;
  for I := 1 to ParamCount do
    if CompareText(ParamStr(I), Name) = 0 then
      Result := True;
end;

function PsExe: String;
begin
  Result := ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe');
end;

function HelperPath: String;
begin
  Result := HelperFolder + '\CognitaWin.ps1';
end;

function AdminUrl(Param: String): String;
begin
  Result := 'http://localhost:' + IntToStr(ChosenAdminPort) + '/';
end;

function LogsDir: String;
begin
  Result := ExpandConstant('{localappdata}\Cognita\logs');
end;

function OnOff(On: Boolean): String;
begin
  if On then
    Result := 'on'
  else
    Result := 'off';
end;

{ Design 18.2: what each mode fixes. finish, repair and update all work in the owned distro, so the
  data location is fixed to its vhd_dir; repair, update and reinstall also fix the Admin user, which
  only makes sense once Cognita has been installed, and (design 19.2 item 7) every non-fresh mode fixes
  the folder (root 1) once it exists. A fixed field with no saved value to show stays editable, so
  Setup can never be stuck on an empty read-only box (the Admin user is the exception: its box says
  "(unchanged)", because Setup does not pass the name at all there). }
function DataLocationLocked: Boolean;
begin
  Result := (InstallMode <> ModeFresh) and (StVhdDir <> '');
end;

{ Repair, update and reinstall-after-uninstall all run over an installed Cognita. }
function RunsOverInstalled: Boolean;
begin
  Result := (InstallMode = ModeRepair) or (InstallMode = ModeUpdate) or (InstallMode = ModeReinstall);
end;

{ Design 19.2 item 7 (was: only repair, update and reinstall): the folder is locked whenever root 1
  exists and the run is not fresh. Finish is included: the helper refuses a projects folder other
  than root 1 once root 1 exists, so offering an editable box there only led to a refusal later. }
function FolderLocked: Boolean;
begin
  Result := (InstallMode <> ModeFresh) and (StRoot1 <> '');
end;

{ Design 19.2 item 14 (was: locked only when admin_user was known): over an installed Cognita the user
  field is always read-only. Setup does not pass --admin-user there, so an editable box would accept a
  name and then drop it; when the name is not known the box says "(unchanged)". }
function UserLocked: Boolean;
begin
  Result := RunsOverInstalled;
end;

{ Design 19.2 item 8: a recorded Funnel points at the MCP port, so the helper's install refuses a
  different one (reason=port-in-use-by-funnel). Advanced shows the port read-only instead. An update
  already locks both ports, with its own note. }
function McpPortLockedByFunnel: Boolean;
begin
  Result := (StFunnelPort <> '') and (InstallMode <> ModeUpdate);
end;

{ A fresh random name for a pipe: 128 bits from the system's UUID generator. }
function RandomPipeName: String;
var
  U: TUuid;
begin
  U.D1 := 0;
  U.D2 := 0;
  U.D3 := 0;
  U.D4 := 0;
  UuidCreate(U);
  Result := 'cognita-' + HexOf(U.D1) + HexOf(U.D2) + HexOf(U.D3) + HexOf(U.D4);
end;

{ Writes S to the named pipe as UTF-16LE (the String's own bytes), no terminator (section 5.6). }
function WritePipe(const PipeName, S: String): Boolean;
var
  H: THandle;
  Written: Cardinal;
begin
  Result := False;
  H := CreateFileW(PipeName, $40000000 { GENERIC_WRITE }, 0, 0, 3 { OPEN_EXISTING }, 0, 0);
  if H = THandle(-1) then
    Exit;
  try
    Result := WriteFile(H, S, Length(S) * 2, Written, 0);
    Result := Result and (Integer(Written) = Length(S) * 2);
  finally
    CloseHandle(H);
  end;
end;

{ ---------------------------------------------------------------------------------------------
  Running the helper
  --------------------------------------------------------------------------------------------- }

procedure ResetRun;
begin
  LastResult := '';
  HelperExit := -1;
  FailCount := 0;
  FailText := '';
  FailStage := '';
  FailMessage := '';
  FailFix := '';
  WarnText := '';
  SignInWarned := False;
  CurTitle := '';
  CurStage := '';
  LastDone := 0;
  LastTotal := 0;
  LinkUrl := '';
end;

{ Puts Text on the clipboard. Inno's script has no clipboard call, so this hands the text to Windows'
  own clip.exe through a temp file (a file, not the command line: the text never meets cmd's quoting).
  The text itself is never logged, only its length: a sign-in link is one-time but still a credential
  in spirit. A failure is logged and otherwise ignored; the box the text came from can be selected and
  copied by hand. }
procedure CopyTextToClipboard(const Text: String);
var
  Tmp: String;
  Code: Integer;
begin
  if Text = '' then
  begin
    Log('clipboard: nothing to copy');
    Exit;
  end;
  Tmp := ExpandConstant('{tmp}\cognita-clip.txt');
  try
    if not SaveStringToFile(Tmp, Text, False) then
    begin
      Log('clipboard: could not write ' + Tmp);
      Exit;
    end;
    if Exec(ExpandConstant('{cmd}'), '/C clip < "' + Tmp + '"', '', SW_HIDE, ewWaitUntilTerminated, Code) then
      Log('clipboard: copied ' + IntToStr(Length(Text)) + ' characters, clip.exe exit ' + IntToStr(Code))
    else
      Log('clipboard: clip.exe could not be started: ' + SysErrorMessage(Code));
  except
    Log('clipboard: exception: ' + GetExceptionMessage);
  end;
  DeleteFile(Tmp);
end;

procedure ProgressCopyClick(Sender: TObject);
begin
  CopyTextToClipboard(ProgressLinkEdit.Text);
end;

{ Design 21.4: the request flag the helper watches (design 21.3). The helper's data folder is
  %LOCALAPPDATA%\Cognita, the same place its settings and logs live. }
function SkipRequestFile: String;
begin
  Result := ExpandConstant('{localappdata}\Cognita\skip-self-test.request');
end;

{ Removes the request flag if it is there; Why says which moment this is (start, end) for the log. }
procedure DeleteSkipRequest(const Why: String);
var
  F: String;
begin
  F := SkipRequestFile;
  if FileExists(F) then
  begin
    if DeleteFile(F) then
      Log('self-test skip: removed the request flag ' + F + ' (' + Why + ')')
    else
      Log('self-test skip: could not remove the request flag ' + F + ' (' + Why + ')');
  end
  else
    Log('self-test skip: no request flag to remove (' + Why + ')');
end;

{ Design 21.4: the button's click. Writes the request flag (an empty file), remembers the press, disables
  the button and says what is happening; the helper turns the flag into the skip file the Linux side
  watches, and the next progress line replaces this text. }
procedure ProgressSkipClick(Sender: TObject);
begin
  ProgressSkipButton.Enabled := False;
  if SaveStringToFile(SkipRequestFile, '', False) and FileExists(SkipRequestFile) then
  begin
    SkipPressed := True;
    Log('self-test skip requested: wrote ' + SkipRequestFile);
    ProgressPage.SetText(CurTitle, 'Stopping the self-tests...');
  end
  else
  begin
    { Nothing was requested, so the button stays usable: pressing it again tries again. }
    ProgressSkipButton.Enabled := True;
    Log('self-test skip requested but the request flag could not be written: ' + SkipRequestFile);
  end;
end;

procedure ShowProgressLine(const Title, Msg: String; const Done, Total: Int64);
var
  Line2: String;
  SkipWant: Boolean;
begin
  Line2 := Msg;
  if Total > 0 then
  begin
    if Line2 <> '' then
      Line2 := FormatBytes(Done) + ' of ' + FormatBytes(Total) + '  -  ' + Line2
    else
      Line2 := FormatBytes(Done) + ' of ' + FormatBytes(Total);
  end;
  { A link in the message (Tailscale sign-in, section 9 step 3) becomes clickable while it is the
    latest message; the helper repeats it in every heartbeat (design 18.4), so it does not vanish. }
  LinkUrl := UrlInText(Msg);
  ProgressPage.SetText(Title, Line2 + '     Elapsed ' + FormatElapsed(Int64(GetTickCount - RunStartTick)));
  { A stage with a byte count fills the bar; any other stage (start, self-test, the Linux side's own
    steps) shows a moving marquee, never a still empty bar: a bar that does not move for three minutes
    makes the user think Setup is stuck (2026-09-29). }
  if Total > 0 then
  begin
    ProgressPage.ProgressBar.Style := npbstNormal;
    ProgressPage.SetProgress(Integer((Done * 1000) div Total), 1000);
  end
  else
  begin
    ProgressPage.ProgressBar.Style := npbstMarquee;
    ProgressPage.SetProgress(0, 0);
  end;
  if LinkUrl <> '' then
  begin
    ProgressPage.Msg2Label.Cursor := crHand;
    ProgressPage.Msg2Label.Font.Color := clBlue;
    ProgressPage.Msg2Label.Font.Style := [fsUnderline];
  end
  else
  begin
    ProgressPage.Msg2Label.Cursor := crDefault;
    ProgressPage.Msg2Label.Font.Color := clWindowText;
    ProgressPage.Msg2Label.Font.Style := [];
  end;
  { The same link as selectable text with a Copy button (design 19.5 item 12). The text is only
    assigned when it changed, so a selection the user has made survives the 5-second heartbeat. }
  if LinkUrl <> '' then
  begin
    if ProgressLinkEdit.Text <> LinkUrl then
      ProgressLinkEdit.Text := LinkUrl;
    ProgressLinkEdit.Visible := True;
    ProgressCopyButton.Visible := True;
  end
  else
  begin
    ProgressLinkEdit.Visible := False;
    ProgressCopyButton.Visible := False;
  end;
  { Design 19.9: Setup runs the helper synchronously, so the sign-in wait cannot be cancelled; the page
    says how long it lasts instead. Only the Tailscale sign-in (stage remote.login) is that wait. }
  ProgressWaitLabel.Visible := (LinkUrl <> '') and (CurStage = 'remote.login');
  { Design 21.4: Skip self-tests while the Linux side is on its proof stage and it has not been pressed;
    re-evaluated on every progress line. Never together with the Copy button (a sign-in link on this
    stage would be a helper bug, but the two must not overlap in any case). }
  SkipWant := ProofSkipVisible(CurStage, SkipPressed) and (not ProgressCopyButton.Visible);
  if SkipWant <> ProgressSkipButton.Visible then
    Log('self-test skip: button visible=' + IntToStr(Ord(SkipWant)) + ' stage=[' + CurStage + '] pressed=' + IntToStr(Ord(SkipPressed)));
  ProgressSkipButton.Visible := SkipWant;
end;

procedure OnHelperLine(const S: String; const Error, FirstLine: Boolean);
var
  L, St, Stage, Title, Msg, Fix, WarnFix: String;
  Done, Total: Int64;
  TitleNew: Boolean;
begin
  L := Trim(S);
  if L = '' then
    Exit;
  { The helper never prints the password; connector tokens are redacted anyway. }
  Log('helper: ' + RedactToken(L));
  { WizardUp: `state` runs from InitializeSetup, before the wizard exists; touching WizardForm there is
    "Could not call proc" (VM, 2026-09-29). }
  if WizardUp and (IsWindowEnabled(WizardForm.Handle) = 0) then
  begin
    EnableWindow(WizardForm.Handle, 1);
    Log('helper: the wizard window was disabled while waiting; enabled it so the page''s buttons work');
  end;
  if Copy(L, 1, 7) = 'result=' then
  begin
    LastResult := L;
    Exit;
  end;
  if L[1] <> '{' then
    Exit;
  St := JsonField(L, 'state');
  Stage := JsonField(L, 'stage');
  Title := JsonField(L, 'title');
  Msg := JsonField(L, 'message');
  Fix := JsonField(L, 'fix');
  Done := StrToInt64Def(JsonField(L, 'bytes_done'), 0);
  Total := StrToInt64Def(JsonField(L, 'bytes_total'), 0);
  { Design 19.6 item 13: byte counts belong to a title. A line that reports them replaces the remembered
    ones; a line WITHOUT them (a heartbeat, a warning or a message on the same step) keeps them while the
    title has not changed, so the bar does not fall back to empty in the middle of a download. A NEW
    title starts from zero. (The helper clears its own remembered counts the same way.) }
  TitleNew := (Title <> '') and (Title <> CurTitle);
  if Stage <> '' then
    CurStage := Stage;
  if Title <> '' then
    CurTitle := Title
  else if (CurTitle = '') and (Stage <> '') then
    CurTitle := Stage;
  if JsonField(L, 'bytes_total') <> '' then
  begin
    LastDone := Done;
    LastTotal := Total;
  end
  else if TitleNew then
  begin
    LastDone := 0;
    LastTotal := 0;
  end
  else if LastTotal > 0 then
    Log('progress: a line without byte counts on the same step keeps ' + IntToStr(LastDone) + ' of ' + IntToStr(LastTotal));
  if St = 'failed' then
  begin
    FailCount := FailCount + 1;
    FailStage := CurTitle;
    FailMessage := Msg;
    FailFix := Fix;
    FailText := FailText + Msg;
    if Fix <> '' then
      FailText := FailText + #13#10 + 'What to do: ' + Fix;
    FailText := FailText + #13#10#13#10;
  end
  else if St = 'warning' then
  begin
    if Msg <> '' then
    begin
      WarnText := WarnText + Msg;
      { Design 22.7 and 22.12 item 2: a warning of the `acceleration` stage (the Linux side dropped the GPU)
        that carries no fix of its own gets "run Setup again and choose NVIDIA". Rerunning Setup is what
        works: the install is then on the CPU image, which ignores Admin's GPU switch. }
      WarnFix := WarningFix(Stage, Fix);
      if (Fix = '') and (WarnFix <> '') then
        Log('helper: warning of stage [' + Stage + '] has no fix of its own; added the line "' + WarnFix + '"');
      if WarnFix <> '' then
        WarnText := WarnText + #13#10 + WarnFix;
      WarnText := WarnText + #13#10#13#10;
    end;
    { The helper's `keepalive` stage is the sign-in task (design 19.4 item 2): when it warns, "Cognita
      starts when you sign in to Windows" would be untrue, so the Finished page drops that sentence. }
    if Stage = 'keepalive' then
    begin
      SignInWarned := True;
      Log('helper: the sign-in task warning was seen (stage keepalive)');
    end;
  end;
  if RunMode = 2 then
    ShowProgressLine(CurTitle, Msg, LastDone, LastTotal)
  else if RunMode = 1 then
  begin
    if CurTitle <> '' then
      BusyPage.SetText(CurTitle, '');
    BusyPage.Animate;
  end;
end;

{ The helper's last result line, judged together with its exit code (section 5.1): 'ok' needs
  exit 0, 'restart-required' needs exit 3010, everything else (including no result line at all)
  is 'failed'. Sets LastStatus. }
procedure JudgeResult;
var
  R: String;
begin
  R := ResultValue(LastResult, 'result');
  LastStatus := 'failed';
  if (R = 'ok') and (HelperExit = 0) then
    LastStatus := 'ok'
  else if (R = 'restart-required') and (HelperExit = 3010) then
    LastStatus := 'restart-required'
  else if R = '' then
    Log('helper: no result line was printed (exit ' + IntToStr(HelperExit) + ')')
  else if (R = 'ok') or (R = 'restart-required') then
    Log('helper: result=' + R + ' does not match exit code ' + IntToStr(HelperExit));
end;

{ Guarantees a readable failure when the helper failed without saying why. }
procedure EnsureFailureText;
begin
  if FailCount = 0 then
  begin
    FailCount := 1;
    if FailStage = '' then
      FailStage := CurTitle;
    if FailMessage = '' then
    begin
      if LastResult = '' then
        FailMessage := 'The Cognita helper stopped without a result (exit code ' + IntToStr(HelperExit) + ').'
      else
        FailMessage := 'The Cognita helper reported a failure without saying what went wrong (exit code ' +
          IntToStr(HelperExit) + ').';
    end;
    if FailFix = '' then
      FailFix := 'Choose Save diagnostics. To get help, attach the file to a new issue at {#SupportUrl}';
    FailText := FailMessage + #13#10 + 'What to do: ' + FailFix + #13#10#13#10;
  end;
end;

{ Runs `powershell CognitaWin.ps1 <Verb> <Args>` and waits. Output goes through OnHelperLine, so
  the last result line, failures and warnings are filled in. Returns True when the verb ended
  ok; LastStatus tells ok from restart-required. Never raises. }
function RunHelper(const Verb, Args: String): Boolean;
var
  Params: String;
  Code: Integer;
  Tick: DWORD;
begin
  ResetRun;
  Params := '-NoProfile -NonInteractive -ExecutionPolicy Bypass -File ' + QuoteArg(HelperPath) + ' ' + Verb;
  if Args <> '' then
    Params := Params + ' ' + Args;
  Log('helper start: verb=' + Verb + ' args=' + RedactToken(Args) + ' helper=' + HelperPath);
  Tick := GetTickCount;
  Code := -1;
  try
    if not ExecAndLogOutput(PsExe, Params, '', SW_HIDE, ewWaitUntilTerminated, Code, @OnHelperLine) then
    begin
      Log('helper: powershell could not be started: ' + SysErrorMessage(Code));
      FailMessage := 'Windows PowerShell could not be started (' + SysErrorMessage(Code) + ').';
    end;
  except
    Log('helper: exception while running ' + Verb + ': ' + GetExceptionMessage);
    FailMessage := 'Running the Cognita helper failed: ' + GetExceptionMessage;
  end;
  HelperExit := Code;
  JudgeResult;
  if LastStatus = 'failed' then
    EnsureFailureText;
  Result := LastStatus = 'ok';
  Log('helper done: verb=' + Verb + ' exit=' + IntToStr(Code) + ' status=' + LastStatus +
    ' elapsed_ms=' + IntToStr(Integer(GetTickCount - Tick)) + ' failures=' + IntToStr(FailCount));
end;

{ A short verb behind the marquee page, so the wizard shows something while it runs. }
function RunHelperBusy(const Title, Verb, Args: String): Boolean;
begin
  BusyPage.SetText(Title, '');
  BusyPage.Show;
  RunMode := 1;
  try
    Result := RunHelper(Verb, Args);
  finally
    RunMode := 0;
    BusyPage.Hide;
  end;
end;

{ ---------------------------------------------------------------------------------------------
  The Admin password: two pipes and a broker (section 5.6 steps 1 and 2)
  --------------------------------------------------------------------------------------------- }

{ Starts the broker, hands it the password, and returns the name of the pipe the verb reads
  from. False when the broker could not be started or never opened its pipe within 60 seconds
  (design 19.5 item 15; it was 10). A cold Windows PowerShell 5.1 start behind antivirus scanning or a
  busy disk has taken longer than 10 seconds, and the pipe only exists once the broker is running. The
  limit is measured on the tick clock, not counted in attempts, so a slow connect attempt cannot
  stretch it. }
function HandOverPassword(var PipeB: String): Boolean;
var
  PipeA: String;
  Code, Attempts: Integer;
  Started: DWORD;
begin
  Result := False;
  PipeA := RandomPipeName;
  PipeB := RandomPipeName;
  Log('password broker: starting (two random pipe names; the password itself is never logged)');
  if not Exec(PsExe, '-NoProfile -NonInteractive -ExecutionPolicy Bypass -File ' + QuoteArg(HelperPath) +
    ' password-broker --in ' + PipeA + ' --out ' + PipeB, '', SW_HIDE, ewNoWait, Code) then
  begin
    Log('password broker: could not start: ' + SysErrorMessage(Code));
    Exit;
  end;
  Started := GetTickCount;
  Attempts := 0;
  while Integer(GetTickCount - Started) < 60000 do
  begin
    Attempts := Attempts + 1;
    if WritePipe('\\.\pipe\' + PipeA, AdminPassword) then
    begin
      Result := True;
      Log('password broker: password written after ' + IntToStr(Attempts) + ' attempt(s), ' +
        IntToStr(Integer(GetTickCount - Started)) + ' ms');
      Exit;
    end;
    Sleep(100);
  end;
  Log('password broker: gave up opening the pipe after ' + IntToStr(Attempts) + ' attempts (60 s): the Setup helper did not start in time');
end;

{ ---------------------------------------------------------------------------------------------
  Arguments for the helper (one place; see the contract at the top of this file)
  --------------------------------------------------------------------------------------------- }

{ The NVIDIA Cognita image's size in this build; 0 = this build has no NVIDIA image (design 22.7). }
function NvidiaBuildBytes: Int64;
begin
  Result := StrToInt64('{#SizeCognitaNvidia}');
end;

{ Which flavor of the Acceleration page this run has (AccelHidden, AccelOffer, AccelOldDriver, AccelNoBuild). }
function AccelMode: Integer;
begin
  Result := AccelPageMode(NvState, NvidiaBuildBytes, InstallMode);
end;

{ The profile the install will have as far as Setup knows (cpu, amd, nvidia) or '' when it keeps a profile
  Setup does not know (design 22.12 items 1(b), 1(c) and 10). }
function EffectiveAccel: String;
begin
  Result := AccelEffective(AccelMode, InstallMode, ChosenAccel, AccelTouched, StAcceleration);
end;

{ The WSL memory option for the install and update verbs: the box is shown (wsl_reclaim=unset) and ticked.
  Only the Ready page's check box changes it; before the Ready page exists nothing is passed. }
function ReclaimArgText: String;
begin
  Result := WslReclaimArg(WslReclaim, ReclaimBox.Checked);
end;

{ Design 22.7 and 22.12 items 1(c) and 10: the Cognita image sized is the one the install will have (the
  NVIDIA image for nvidia, the larger of the CPU and NVIDIA images when the profile is unknown, so the disk
  check never under-sizes). }
function ImagesBytes: Int64;
begin
  Result := AccelCognitaBytes(EffectiveAccel, NvState, StrToInt64('{#SizeCognitaCpu}'), NvidiaBuildBytes);
  if ChosenWorkspaceOn then
    Result := Result + StrToInt64('{#SizeWorkspaceRuntime}') + StrToInt64('{#SizeToolbox}');
end;

{ Search models Cognita downloads at install (Linux design 6.3: "2.3 GB today"). Only used for the
  size figures shown on the Ready page and the disk estimate passed to `check`. }
function ModelsBytes: Int64;
begin
  Result := StrToInt64('2300000000');
end;

function DownloadBytes: Int64;
begin
  Result := ImagesBytes + ModelsBytes;
end;

{ The Windows design 5.3 disk formula (Linux design 6.3's, with its sizes compiled in): image
  sizes x 4 + models + 3.5 GB, plus 1 GB for the imported Linux image. }
function DiskNeededBytes: Int64;
begin
  Result := ImagesBytes * 4 + ModelsBytes + StrToInt64('4500000000');
end;

function ArgsCheckFinal: String;
begin
  Result := '--phase final --data-dir ' + QuoteArg(ChosenDataDir) + ' --mcp-port ' + IntToStr(ChosenMcpPort) +
    ' --admin-port ' + IntToStr(ChosenAdminPort) + ' --disk-bytes ' + IntToStr(DiskNeededBytes);
end;

{ The `install` verb. --admin-user is not passed on a repair or a reinstall (the user is read-only
  there, design 18.2). --image only when the Linux image was unpacked (fresh and finish); --src on
  every install run, so the helper can swap an older tree in the distro before cognita install. }
function ArgsInstall(const PipeB, ImageFile, SrcFile: String): String;
begin
  Result := '--projects-folder ' + QuoteArg(PageFolder.Values[0]);
  if not RunsOverInstalled then
    Result := Result + ' --admin-user ' + QuoteArg(AdminUser);
  Result := Result + ' --workspace ' + OnOff(ChosenWorkspaceOn) + ' --mcp-port ' + IntToStr(ChosenMcpPort) +
    ' --admin-port ' + IntToStr(ChosenAdminPort) + ' --data-dir ' + QuoteArg(ChosenDataDir);
  if ImageFile <> '' then
    Result := Result + ' --image ' + QuoteArg(ImageFile) + ' --image-sha256 {#ImageSha256}';
  if SrcFile <> '' then
    Result := Result + ' --src ' + QuoteArg(SrcFile) + ' --src-sha256 {#SrcSha256}';
  { Design 22.7 and 22.12 item 1: --acceleration after --workspace (AccelArg decides whether and what),
    --wsl-memory-reclaim when the Ready page's box was shown and ticked (design 22.9). }
  Result := Result + AccelArg(AccelMode, InstallMode, ChosenAccel, AccelTouched, StAcceleration);
  Result := Result + ' --setup-version {#Version} --setup-revision {#Revision} --password-pipe ' + PipeB;
  Result := Result + ReclaimArgText;
end;

function ArgsUpdate(const PipeB, SrcFile: String): String;
begin
  Result := '--src ' + QuoteArg(SrcFile) + ' --src-sha256 {#SrcSha256} --setup-version {#Version}' +
    ' --setup-revision {#Revision} --disk-bytes ' + IntToStr(DiskNeededBytes) + ' --password-pipe ' + PipeB +
    ReclaimArgText;
end;

{ Logs what Setup decided about acceleration and WSL's memory option for this run, with the values it
  decided from; called once, right before the helper is started (design 22.8). }
procedure LogAccelDecision(const Verb: String);
begin
  Log('acceleration: verb=' + Verb + ' mode=' + IntToStr(InstallMode) + ' page_mode=' + IntToStr(AccelMode) +
    ' nvidia=[' + NvState + '] build_bytes=' + IntToStr(NvidiaBuildBytes) + ' default=[' + AccelDefaultPick +
    '] chosen=[' + ChosenAccel + '] touched=' + IntToStr(Ord(AccelTouched)) + ' installed_profile=[' + StAcceleration +
    '] effective=[' + EffectiveAccel + '] argument=[' + Trim(AccelArg(AccelMode, InstallMode, ChosenAccel, AccelTouched,
    StAcceleration)) + '] images_bytes=' + IntToStr(ImagesBytes));
  Log('wsl memory: wsl_reclaim=[' + WslReclaim + '] box_visible=' + IntToStr(Ord(WslReclaimBoxVisible(WslReclaim))) +
    ' box_ticked=' + IntToStr(Ord(ReclaimBox.Checked)) + ' argument=[' + Trim(ReclaimArgText) + ']');
end;

{ ---------------------------------------------------------------------------------------------
  `state` (sections 5.2 and 7.2)
  --------------------------------------------------------------------------------------------- }

procedure ReadState;
var
  P: Integer;
begin
  StInstalled := IsTruthy(ResultValue(LastResult, 'installed'));
  StResumeAfterWsl := ResultValue(LastResult, 'resume') = 'after-wsl';
  StDistroPresent := StateDistroOwned(LastResult);
  StLinuxVersion := ResultValue(LastResult, 'linux_version');
  StAdminUser := ResultValue(LastResult, 'admin_user');
  StRoot1 := ResultValue(LastResult, 'root1');
  StVhdDir := ResultValue(LastResult, 'vhd_dir');
  StVhdBytes := ResultValue(LastResult, 'vhd_bytes');
  StWorkspace := Lowercase(ResultValue(LastResult, 'workspace'));
  { Design 19.2 item 8 and 19.11 R6/R7. `state` reports funnel_port = the recorded Funnel's PUBLIC https
    port while Tailscale still serves it (or cannot be asked); empty when none is recorded or it was
    turned off by hand. Non-empty locks the MCP port in Advanced. }
  StFunnelPort := ResultValue(LastResult, 'funnel_port');
  { Design 22.4: the acceleration the install runs on; '' when unknown (before 15.1) or a word Setup does not know. }
  StAcceleration := AccelKnown(ResultValue(LastResult, 'acceleration'));
  P := StrToIntDef(ResultValue(LastResult, 'mcp_port'), 0);
  if (P >= 1024) and (P <= 65535) then
    ChosenMcpPort := P;
  P := StrToIntDef(ResultValue(LastResult, 'admin_port'), 0);
  if (P >= 1024) and (P <= 65535) then
    ChosenAdminPort := P;
  { The saved data location only matters when the owned distro exists: it is where that distro's
    virtual disk lives. With no distro the location is a free choice (the default, or Advanced). }
  if (StVhdDir <> '') and StDistroPresent then
    ChosenDataDir := StVhdDir;
  if StWorkspace = 'off' then
    ChosenWorkspaceOn := False
  else if StWorkspace = 'on' then
    ChosenWorkspaceOn := True;
  Log('state: installed=' + IntToStr(Ord(StInstalled)) + ' resume_after_wsl=' + IntToStr(Ord(StResumeAfterWsl)) +
    ' distro_present=' + IntToStr(Ord(StDistroPresent)) + ' linux_version=' + StLinuxVersion +
    ' admin_user=' + StAdminUser + ' root1=' + StRoot1 +
    ' mcp=' + IntToStr(ChosenMcpPort) + ' admin=' + IntToStr(ChosenAdminPort) + ' data_dir=' + ChosenDataDir +
    ' workspace=' + OnOff(ChosenWorkspaceOn) + ' funnel_port=' + StFunnelPort + ' acceleration=[' + StAcceleration +
    '] (reported [' + ResultValue(LastResult, 'acceleration') + '])');
end;

{ ---------------------------------------------------------------------------------------------
  PATH (section 4.4)
  --------------------------------------------------------------------------------------------- }

function BinDir: String;
begin
  Result := ExpandConstant('{localappdata}\Cognita\bin');
end;

function PathIsEmpty: Boolean;
var
  Old: String;
begin
  Result := (not RegQueryStringValue(HKEY_CURRENT_USER, 'Environment', 'Path', Old)) or (Trim(Old) = '');
end;

function PathNeedsAppend: Boolean;
var
  Old: String;
begin
  Result := False;
  if not RegQueryStringValue(HKEY_CURRENT_USER, 'Environment', 'Path', Old) then
    Exit;
  if Trim(Old) = '' then
    Exit;
  Result := not PathHasPart(Old, BinDir);
end;

procedure RemoveBinFromPath;
var
  Old, New: String;
begin
  if not RegQueryStringValue(HKEY_CURRENT_USER, 'Environment', 'Path', Old) then
  begin
    Log('path: no user Path value; nothing to remove');
    Exit;
  end;
  if not PathHasPart(Old, BinDir) then
  begin
    Log('path: ' + BinDir + ' is not in the user Path; nothing to remove');
    Exit;
  end;
  New := PathRemovePart(Old, BinDir);
  { RegQueryStringValue returns the raw (unexpanded) text, so other entries keep their %VARS%. }
  if RegWriteExpandStringValue(HKEY_CURRENT_USER, 'Environment', 'Path', New) then
    Log('path: removed ' + BinDir + ' from the user Path (' + IntToStr(Length(Old)) + ' -> ' + IntToStr(Length(New)) + ' characters)')
  else
    Log('path: could not rewrite the user Path value');
end;

{ ---------------------------------------------------------------------------------------------
  Wizard
  --------------------------------------------------------------------------------------------- }

{ A plain label on the Finished page; hidden until the success layout places it. }
function NewFinishedText(const Cap: String): TNewStaticText;
begin
  Result := TNewStaticText.Create(WizardForm);
  Result.Parent := WizardForm.FinishedPage;
  Result.Caption := Cap;
  Result.Visible := False;
end;

{ Opens one web address in the default browser (every clickable link goes through here). }
procedure OpenUrl(const Url: String);
var
  Code: Integer;
begin
  if Url = '' then
    Exit;
  Log('link: opening ' + RedactToken(Url));
  ShellExec('open', Url, '', '', SW_SHOWNORMAL, ewNoWait, Code);
end;

procedure LinkClick(Sender: TObject);
begin
  OpenUrl(LinkUrl);
end;

procedure RemoteLinkClick(Sender: TObject);
begin
  OpenUrl(RemoteLinkUrl);
end;

procedure AccelLinkClick(Sender: TObject);
begin
  OpenUrl(NvidiaDriverUrl);
end;

{ A click on the check box's caption toggles the box, as a click on a real check box's caption would. }
procedure ReclaimTextClick(Sender: TObject);
begin
  ReclaimBox.Checked := not ReclaimBox.Checked;
  Log('ready page: the WSL memory caption was clicked; box_ticked=' + IntToStr(Ord(ReclaimBox.Checked)));
end;

procedure FinAdminClick(Sender: TObject);
begin
  OpenUrl(AdminUrl(''));
end;

procedure FinMcpClick(Sender: TObject);
begin
  OpenUrl('http://localhost:' + IntToStr(ChosenMcpPort) + '/');
end;

procedure FinPubClick(Sender: TObject);
begin
  OpenUrl(PublicUrl);
end;

{ A link-styled label: the same control the failure page uses for "Open the log folder". }
procedure StyleAsLink(const L: TNewStaticText);
begin
  L.Cursor := crHand;
  L.Font.Color := clBlue;
  L.Font.Style := [fsUnderline];
end;

procedure OpenLogsClick(Sender: TObject);
var
  Code: Integer;
begin
  ForceDirectories(LogsDir);
  ShellExec('open', LogsDir, '', '', SW_SHOWNORMAL, ewNoWait, Code);
end;

{ The support page opens in the user's browser only when they click it; nothing is put in the
  address (no log text, no machine details). }
procedure ReportProblemClick(Sender: TObject);
var
  Code: Integer;
begin
  Log('support: the user opened the issue page');
  ShellExec('open', '{#SupportUrl}', '', '', SW_SHOWNORMAL, ewNoWait, Code);
end;

procedure SaveDiagnosticsClick(Sender: TObject);
var
  Zip: String;
begin
  Log('diagnostics: requested by the user (Finished page or a failure dialog)');
  if RunHelperBusy('Saving diagnostics', 'diagnostics', '--setup-log ' + QuoteArg(ExpandConstant('{log}'))) then
  begin
    { The helper has already opened Explorer with the file selected. Nothing is sent: the user
      decides whether to attach it anywhere, so the box says where they can and offers the page. }
    Zip := ResultValue(LastResult, 'zip');
    if MsgBox('Diagnostics were saved to:' + #13#10#13#10 + Zip + #13#10#13#10 +
      'They contain logs and settings but no passwords, tokens or documents. Nothing was sent ' +
      'anywhere.' + #13#10#13#10 + 'To get help, attach this file to a new issue at' + #13#10 +
      '{#SupportUrl}' + #13#10#13#10 + 'Open that page now?', mbInformation, MB_YESNO) = IDYES then
      ReportProblemClick(nil);
  end
  else
    MsgBox('Saving diagnostics failed.' + #13#10#13#10 + FailText + #13#10 +
      'The logs are still in ' + LogsDir + '. To get help, attach them to a new issue at ' +
      '{#SupportUrl}', mbError, MB_OK);
end;

{ On any failure Setup saves the diagnostics zip to the Desktop by itself (Doug, 2026-09-29: the user
  should not have to find a hidden log folder). It is a LOCAL file only: nothing is sent, and the
  failure text names the file and where the user may attach it. Running the helper resets the failure
  fields, so they are kept and restored around the call. Returns the zip path, or '' when saving
  failed (the text then points at the log folder instead). }
function AutoSaveDiagnostics(const Why: String): String;
var
  KText, KStage, KMsg, KFix, KResult: String;
  KCount: Integer;
begin
  KText := FailText; KStage := FailStage; KMsg := FailMessage; KFix := FailFix; KResult := LastResult;
  KCount := FailCount;
  Result := '';
  Log('diagnostics: saving automatically after a failure (' + Why + ')');
  if RunHelperBusy('Saving diagnostics', 'diagnostics', '--no-open --setup-log ' + QuoteArg(ExpandConstant('{log}'))) then
    Result := ResultValue(LastResult, 'zip');
  Log('diagnostics: automatic save zip=[' + Result + ']');
  FailText := KText; FailStage := KStage; FailMessage := KMsg; FailFix := KFix; LastResult := KResult;
  FailCount := KCount;
end;

{ The closing text of every failure: where the diagnostics are, that nothing was sent, where help is. }
function DiagnosticsSentence(const Zip: String): String;
begin
  if Zip <> '' then
    Result := 'Diagnostics were saved to:' + #13#10 + Zip + #13#10 + 'Nothing was sent anywhere. To get help, ' +
      'attach that file to a new issue at {#SupportUrl} (Report a problem).'
  else
    Result := 'Nothing was sent anywhere. Saving diagnostics did not work; the logs are in ' + LogsDir +
      '. To get help, attach them to a new issue at {#SupportUrl} (Report a problem).';
end;

procedure ShowFileInExplorer(const Path: String);
var
  Code: Integer;
begin
  Log('diagnostics: showing the file in Explorer');
  ShellExec('open', 'explorer.exe', '/select,"' + Path + '"', '', SW_SHOWNORMAL, ewNoWait, Code);
end;

procedure ShowDiagFileClick(Sender: TObject);
begin
  if FinDiagZip <> '' then
    ShowFileInExplorer(FinDiagZip)
  else
    SaveDiagnosticsClick(Sender);
end;

{ Design 19.5 item 12: a radio button in a custom form moves AND selects with the arrow keys, as in a
  standard dialog. Focus alone moved between the buttons here without checking one, so the choice on
  screen and the choice Setup acted on could disagree. Every radio button of a custom form or page
  (the uninstall form, the WSL restart choice, the Remote page) gets this handler as its OnEnter: the
  button that receives the focus is the one that is checked. Tab from another control lands on the
  checked button of the group, so tabbing in changes nothing. }
procedure RadioEnter(Sender: TObject);
begin
  if not TNewRadioButton(Sender).Checked then
  begin
    TNewRadioButton(Sender).Checked := True;
    Log('keyboard: radio "' + TNewRadioButton(Sender).Caption + '" checked by focus');
  end;
end;

{ A failure from the preflight check, wsl-install, the folder check, the final check or the
  restart-for-wsl step (design 18.5, extended by 19 to the last three): the same text, and the same
  two ways out the Installing page's failure gives: Save diagnostics and the log folder. Save
  diagnostics ends the modal call with mrYes and the loop shows the dialog again afterwards, the
  same pattern as the Advanced form. The text is captured first: saving diagnostics runs the
  helper, which resets the failure fields. }
procedure ShowFailuresWithHelp(const Heading: String);
var
  Form: TSetupForm;
  Body: TNewMemo;
  OpenLogs, Report: TNewStaticText;
  DiagB, OkB: TNewButton;
  R: Integer;
  Zip: String;
begin
  Log('failure dialog: ' + Heading + ' failures=' + IntToStr(FailCount));
  Zip := AutoSaveDiagnostics(Heading);
  Form := CreateCustomForm(ScaleX(480), ScaleY(310), False, False);
  try
    Form.Caption := 'Cognita Setup';
    Body := TNewMemo.Create(Form);
    Body.Parent := Form;
    Body.Left := ScaleX(14);
    Body.Top := ScaleY(14);
    Body.Width := ScaleX(452);
    Body.Height := ScaleY(200);
    Body.ReadOnly := True;
    Body.ScrollBars := ssVertical;
    Body.WordWrap := True;
    Body.Text := Heading + #13#10#13#10 + FailText + DiagnosticsSentence(Zip);
    OpenLogs := TNewStaticText.Create(Form);
    OpenLogs.Parent := Form;
    OpenLogs.Left := ScaleX(14);
    OpenLogs.Top := ScaleY(224);
    OpenLogs.Caption := 'Open the log folder';
    StyleAsLink(OpenLogs);
    OpenLogs.OnClick := @OpenLogsClick;
    Report := TNewStaticText.Create(Form);
    Report.Parent := Form;
    Report.Left := OpenLogs.Left + OpenLogs.Width + ScaleX(24);
    Report.Top := OpenLogs.Top;
    Report.Caption := 'Report a problem';
    StyleAsLink(Report);
    Report.OnClick := @ReportProblemClick;
    DiagB := TNewButton.Create(Form);
    DiagB.Parent := Form;
    { The zip is already on the Desktop: the button shows it. When saving failed it tries again. }
    if Zip <> '' then
      DiagB.Caption := 'Show the file'
    else
      DiagB.Caption := 'Save diagnostics';
    DiagB.Left := ScaleX(14);
    DiagB.Top := Form.ClientHeight - ScaleY(25 + 14);
    DiagB.Width := ScaleX(120);
    DiagB.Height := ScaleY(25);
    DiagB.ModalResult := mrYes;
    OkB := TNewButton.Create(Form);
    OkB.Parent := Form;
    OkB.Caption := 'OK';
    OkB.Left := Form.ClientWidth - ScaleX(80 + 14);
    OkB.Top := Form.ClientHeight - ScaleY(25 + 14);
    OkB.Width := ScaleX(80);
    OkB.Height := ScaleY(25);
    OkB.ModalResult := mrOk;
    OkB.Default := True;
    OkB.Cancel := True;
    { Design 19.5 item 12: the memo is the first control, so it took the focus and swallowed Enter (a
      multi-line edit treats Enter as a new line). The default button holds the focus instead. }
    Form.ActiveControl := OkB;
    Form.FlipAndCenterIfNeeded(True, WizardForm, False);
    repeat
      R := Form.ShowModal;
      if R = mrYes then
      begin
        if Zip <> '' then
          ShowFileInExplorer(Zip)
        else
          SaveDiagnosticsClick(nil);
      end;
    until R <> mrYes;
  finally
    Form.Free;
  end;
end;

{ True when Dir holds nothing at all (no files, no folders). Used by Advanced: the data location must be
  a new folder or an empty one (design 19.7 item 16). }
function DirIsEmpty(const Dir: String): Boolean;
var
  F: TFindRec;
begin
  Result := True;
  if FindFirst(AddBackslash(Dir) + '*', F) then
  begin
    try
      repeat
        if (F.Name <> '.') and (F.Name <> '..') then
          Result := False;
      until (not Result) or (not FindNext(F));
    finally
      FindClose(F);
    end;
  end;
end;

{ The data location Advanced was given, checked on OK (design 19.7 item 16). Returns '' when it is fine,
  otherwise the sentence to show. Only a location the user CAN change and did change is checked: an
  unchanged default is the one Setup has always used, and a locked one is where the distro already is.
  The order is cheapest first; the helper's own check (roots --validate, which knows the projects
  folder and the folders Cognita already uses) runs last, because it starts PowerShell. }
function DataFolderProblem(const Dir: String): String;
var
  Reason: String;
begin
  Result := '';
  if HasBadArgChar(Dir) then
    Result := 'The data folder cannot contain a double quote.'
  else if IsDriveRoot(Dir) then
    Result := 'Choose a folder on that drive, not the whole drive (for example ' + AddBackslash(Dir) + 'Cognita).'
  else if (Length(Dir) < 3) or (Copy(Dir, 2, 2) <> ':\') then
    Result := 'Choose a data folder on one of this PC''s own drives (a full path that starts with a drive letter).'
  else if IsOneDrivePath(Dir, GetEnv('OneDrive'), GetEnv('OneDriveConsumer'), GetEnv('OneDriveCommercial')) then
    Result := 'Cognita''s data cannot live in a OneDrive folder.'
  else if DirExists(Dir) and (not DirIsEmpty(Dir)) then
    Result := 'Choose a new folder or an empty one. That folder already holds files, and Cognita''s data must not be mixed with them.'
  else if not RunHelperBusy('Checking the data folder', 'roots', '--validate ' + QuoteArg(PageFolder.Values[0]) +
    ' --data-dir ' + QuoteArg(Dir)) then
  begin
    Log('advanced: roots --validate failed internally for data_dir=' + Dir);
    Result := 'Setup could not check that folder.' + #13#10#13#10 + FailText;
  end
  else if ResultValue(LastResult, 'ok') = '0' then
  begin
    Reason := ResultValue(LastResult, 'reason');
    if Reason = '' then
      Reason := 'Cognita cannot use that folder.';
    Result := Reason;
  end;
  if Result <> '' then
    Log('advanced: data_dir=' + Dir + ' refused: ' + Result)
  else
    Log('advanced: data_dir=' + Dir + ' accepted');
end;

{ Defined below, with the Ready page's other code; AdvancedClick rebuilds the summary with it. }
procedure ShowReadyPage; forward;

procedure AdvancedClick(Sender: TObject);
var
  Form: TSetupForm;
  McpEdit, AdminEdit, DataEdit: TNewEdit;
  WorkspaceBox: TNewCheckBox;
  Lbl, FunnelNote: TNewStaticText;
  OkButton, CancelButton, BrowseButton: TNewButton;
  Mcp, Adm, NoteH: Integer;
  Dir, Problem: String;
  Done: Boolean;
begin
  { A recorded Funnel points at the MCP port: the port shows read-only with the reason under it, and
    every row below moves down by the note's height (design 19.2 item 8). }
  NoteH := 0;
  if McpPortLockedByFunnel then
    NoteH := ScaleY(32);
  Form := CreateCustomForm(ScaleX(440), ScaleY(250) + NoteH, False, False);
  try
    Form.Caption := 'Advanced';

    Lbl := TNewStaticText.Create(Form);
    Lbl.Parent := Form;
    Lbl.Left := ScaleX(12);
    Lbl.Top := ScaleY(14);
    Lbl.Caption := 'MCP port (connectors):';
    McpEdit := TNewEdit.Create(Form);
    McpEdit.Parent := Form;
    McpEdit.Left := ScaleX(200);
    McpEdit.Top := ScaleY(10);
    McpEdit.Width := ScaleX(80);
    McpEdit.Text := IntToStr(ChosenMcpPort);
    if NoteH > 0 then
    begin
      McpEdit.ReadOnly := True;
      FunnelNote := TNewStaticText.Create(Form);
      FunnelNote.Parent := Form;
      FunnelNote.Left := ScaleX(12);
      FunnelNote.Top := ScaleY(36);
      FunnelNote.Width := ScaleX(416);
      FunnelNote.AutoSize := False;
      FunnelNote.WordWrap := True;
      FunnelNote.Height := NoteH - ScaleY(4);
      FunnelNote.Caption := 'Remote access uses this port. Turn remote access off first to change it.';
      Log('advanced: the MCP port is read-only; a Funnel is recorded on public port ' + StFunnelPort);
    end;

    Lbl := TNewStaticText.Create(Form);
    Lbl.Parent := Form;
    Lbl.Left := ScaleX(12);
    Lbl.Top := ScaleY(46) + NoteH;
    Lbl.Caption := 'Admin port:';
    AdminEdit := TNewEdit.Create(Form);
    AdminEdit.Parent := Form;
    AdminEdit.Left := ScaleX(200);
    AdminEdit.Top := ScaleY(42) + NoteH;
    AdminEdit.Width := ScaleX(80);
    AdminEdit.Text := IntToStr(ChosenAdminPort);

    Lbl := TNewStaticText.Create(Form);
    Lbl.Parent := Form;
    Lbl.Left := ScaleX(12);
    Lbl.Top := ScaleY(82) + NoteH;
    Lbl.Width := ScaleX(416);
    Lbl.AutoSize := False;
    Lbl.WordWrap := True;
    Lbl.Height := ScaleY(48);
    { The helper's update verb carries no ports and no Workspace switch, so an update keeps what is
      installed; changing them is a repair (a rerun of Setup), design 7.2. Showing editable fields
      here would accept a change and then drop it. }
    if InstallMode = ModeUpdate then
      Lbl.Caption := 'An update keeps your ports, your Workspace setting and where Cognita keeps its data. ' +
        'To change the ports or Workspace, run Setup again after the update.'
    else if DataLocationLocked then
      Lbl.Caption := 'Where Cognita keeps its data (its index, settings and Workspace). Cognita''s Linux ' +
        'system is already there, so this cannot change. Your documents stay where they are.'
    else
      Lbl.Caption := 'Where Cognita keeps its data (its index, settings and Workspace). Choose a new or empty ' +
        'folder on a drive with room if C: is small. Your documents stay where they are.';
    DataEdit := TNewEdit.Create(Form);
    DataEdit.Parent := Form;
    DataEdit.Left := ScaleX(12);
    DataEdit.Top := ScaleY(134) + NoteH;
    DataEdit.Width := ScaleX(330);
    DataEdit.Text := ChosenDataDir;
    { With an owned distro (finish, repair, update) the data lives where its virtual disk already is:
      the location is shown but cannot be changed (design 18.2). }
    if DataLocationLocked then
      DataEdit.ReadOnly := True;
    BrowseButton := TNewButton.Create(Form);
    BrowseButton.Parent := Form;
    BrowseButton.Left := ScaleX(350);
    BrowseButton.Top := ScaleY(132) + NoteH;
    BrowseButton.Width := ScaleX(78);
    BrowseButton.Height := ScaleY(25);
    BrowseButton.Caption := 'Browse...';
    BrowseButton.ModalResult := mrYes;
    BrowseButton.Enabled := not DataLocationLocked;

    WorkspaceBox := TNewCheckBox.Create(Form);
    WorkspaceBox.Parent := Form;
    WorkspaceBox.Left := ScaleX(12);
    WorkspaceBox.Top := ScaleY(172) + NoteH;
    WorkspaceBox.Width := ScaleX(416);
    WorkspaceBox.Caption := 'Turn on Workspace (Cognita can run code for you in a sandbox)';
    WorkspaceBox.Checked := ChosenWorkspaceOn;
    if InstallMode = ModeUpdate then
    begin
      McpEdit.ReadOnly := True;
      AdminEdit.ReadOnly := True;
      WorkspaceBox.Enabled := False;
    end;

    OkButton := TNewButton.Create(Form);
    OkButton.Parent := Form;
    OkButton.Caption := 'OK';
    OkButton.Left := Form.ClientWidth - ScaleX(75 + 6 + 75 + 12);
    OkButton.Top := Form.ClientHeight - ScaleY(25 + 12);
    OkButton.Width := ScaleX(75);
    OkButton.Height := ScaleY(25);
    OkButton.ModalResult := mrOk;
    OkButton.Default := True;
    CancelButton := TNewButton.Create(Form);
    CancelButton.Parent := Form;
    CancelButton.Caption := 'Cancel';
    CancelButton.Left := Form.ClientWidth - ScaleX(75 + 12);
    CancelButton.Top := Form.ClientHeight - ScaleY(25 + 12);
    CancelButton.Width := ScaleX(75);
    CancelButton.Height := ScaleY(25);
    CancelButton.ModalResult := mrCancel;
    CancelButton.Cancel := True;
    { Design 19.5 item 12: Enter means OK here. Without this the first edit box held the focus. }
    Form.ActiveControl := OkButton;

    Form.FlipAndCenterIfNeeded(True, WizardForm, False);
    Done := False;
    while not Done do
    begin
      { A small loop instead of event handlers: Browse (mrYes) and OK (mrOk) both end the modal
        call and come back here; a bad value shows its message and the form is shown again. }
      case Form.ShowModal of
        mrYes:
          begin
            Dir := DataEdit.Text;
            if BrowseForFolder('Choose where Cognita keeps its data', Dir, True) then
              DataEdit.Text := Dir;
          end;
        mrOk:
          begin
            Dir := RemoveBackslash(Trim(DataEdit.Text));
            if not PortOk(McpEdit.Text, Mcp) then
              MsgBox('The MCP port must be a number from 1024 to 65535.', mbError, MB_OK)
            else if not PortOk(AdminEdit.Text, Adm) then
              MsgBox('The Admin port must be a number from 1024 to 65535.', mbError, MB_OK)
            else if Mcp = Adm then
              MsgBox('The MCP port and the Admin port must be different.', mbError, MB_OK)
            else
            begin
              { Design 19.7 item 16: a changed, changeable data location must pass DataFolderProblem
                (no drive root, a new or empty folder, not OneDrive, and the helper's roots --validate). }
              Problem := '';
              if (not DataLocationLocked) and (CompareText(Dir, ChosenDataDir) <> 0) then
                Problem := DataFolderProblem(Dir)
              else
                Log('advanced: data_dir not checked (locked=' + IntToStr(Ord(DataLocationLocked)) +
                  ' unchanged=' + IntToStr(Ord(CompareText(Dir, ChosenDataDir) = 0)) + ')');
              if Problem <> '' then
                MsgBox(Problem, mbError, MB_OK)
              else
              begin
                ChosenMcpPort := Mcp;
                ChosenAdminPort := Adm;
                if not DataLocationLocked then
                  ChosenDataDir := Dir;
                ChosenWorkspaceOn := WorkspaceBox.Checked;
                Log('advanced: mcp=' + IntToStr(Mcp) + ' admin=' + IntToStr(Adm) + ' data_dir=' + ChosenDataDir +
                  ' workspace=' + OnOff(ChosenWorkspaceOn));
                { 15.1.0 review: the Ready summary was built only when the page was entered, so after OK here it
                  still showed the old ports, Workspace and sizes while the install used the new ones. }
                ShowReadyPage;
                { As when the page is entered: Enter means Install, not Advanced again. }
                WizardForm.ActiveControl := WizardForm.NextButton;
                Done := True;
              end;
            end;
          end;
      else
        Done := True;
      end;
    end;
  finally
    Form.Free;
  end;
end;

{ ---------------------------------------------------------------------------------------------
  The remote access step (section 9, design 18.4). It runs from the Remote page's Next button and
  from that page's Retry button, so it lives above InitializeWizard, which wires the button.
  --------------------------------------------------------------------------------------------- }

{ One try at `remote-access`: a NEW broker and new pipes (a broker serves the password once), then
  the verb behind the progress page. HandedOver is False when the password could not be handed to
  the broker, in which case the verb was not run. }
function RemoteAttempt(const Extra, Title: String; var HandedOver: Boolean): Boolean;
var
  PipeB: String;
begin
  Result := False;
  HandedOver := HandOverPassword(PipeB);
  if not HandedOver then
    Exit;
  { Design 19.4 item 23: the caption of this step is its own (the page was still saying "Installing
    Cognita" while Tailscale was being set up). }
  ProgressPage.Caption := 'Setting up remote access';
  ProgressPage.Description := 'Setup is setting up remote access. Keep this window open.';
  ProgressPage.SetText(Title, '');
  ProgressLinkEdit.Visible := False;
  ProgressCopyButton.Visible := False;
  ProgressWaitLabel.Visible := False;
  ProgressSkipButton.Visible := False;
  ProgressPage.Show;
  WizardForm.BackButton.Visible := False;
  RunMode := 2;
  RunStartTick := GetTickCount;
  try
    Result := RunHelper('remote-access', '--yes' + Extra + ' --password-pipe ' + PipeB);
  finally
    RunMode := 0;
    ProgressPage.Hide;
  end;
end;

procedure RemoteCopyClick(Sender: TObject);
begin
  CopyTextToClipboard(RemoteLinkEdit.Text);
end;

{ A failure that carries a link (design 18.4, e.g. reason=funnel-not-enabled): the outcome stays on
  the Remote page as ONE fixed sentence (design 19.4 item 17: the helper's own message, which already
  holds the link, used to be pasted in as well, so the link showed twice), then the link below it as
  a clickable link AND as selectable text with a Copy button (design 19.5 item 12: the tailnet's owner
  often opens it on another device), then Retry. Next then carries on to the Finished page without
  remote access. The controls are moved up under the short sentence: the page was laid out for the
  long intro text. }
procedure ShowRemoteLinkStage(const Link: String);
begin
  RemoteStage := 1;
  RemoteLinkUrl := Link;
  RemoteLabel.Height := ScaleY(48);
  RemoteLabel.Caption := 'Remote access needs one more step from you: open the link below and follow it, ' +
    'then press Retry, or press Next to carry on without remote access.';
  RemoteSkip.Visible := False;
  RemoteSetUp.Visible := False;
  RemoteLinkLabel.Top := ScaleY(56);
  RemoteLinkLabel.Caption := Link;
  RemoteLinkLabel.Visible := True;
  RemoteLinkEdit.Top := ScaleY(82);
  RemoteLinkEdit.Text := Link;
  RemoteLinkEdit.Visible := True;
  RemoteCopyButton.Top := RemoteLinkEdit.Top - ScaleY(2);
  RemoteCopyButton.Left := RemoteLinkEdit.Left + RemoteLinkEdit.Width + ScaleX(8);
  RemoteCopyButton.Visible := True;
  RemoteRetryButton.Top := ScaleY(118);
  RemoteRetryButton.Visible := True;
  Log('remote access: outcome shown on the Remote page with a link, a copy box and Retry; link=' + RedactToken(Link));
end;

{ Leaves the link stage: the link, its copy box and Retry go away (a Retry that worked, or one that
  ended skipped). }
procedure HideRemoteLinkStage;
begin
  RemoteLinkLabel.Visible := False;
  RemoteLinkEdit.Visible := False;
  RemoteCopyButton.Visible := False;
  RemoteRetryButton.Visible := False;
end;

{ Runs the verb and turns its outcome into the Finished page's note (RemoteNote), the public address
  (PublicUrl) and, for a failure with a link, the Remote page's link and Retry (RemoteStage = 1). }
procedure RunRemoteAccess;
var
  AltPort, Link, Extra: String;
  Ok, Handed, PortBusy: Boolean;
begin
  RemoteNote := '';
  Extra := '';
  if RemoteFunnelPort <> '' then
    Extra := ' --funnel-port ' + RemoteFunnelPort;
  Log('remote access: starting; funnel_port=' + RemoteFunnelPort + ' stage=' + IntToStr(RemoteStage));
  Ok := RemoteAttempt(Extra, 'Setting up remote access', Handed);
  if not Handed then
  begin
    RemoteNote := 'Remote access was not set up: Setup could not hand the password to its helper (the Setup helper did not start in time). Run "cognita remote-access" later.';
    Exit;
  end;
  { Section 9 step 4: the helper never replaces a Funnel that already serves something else on
    port 443. It answers reason=funnel-port-busy with the free ports it can use instead, and the
    rerun names one with --funnel-port. A new broker serves the rerun (the first served its one use). }
  PortBusy := (not Ok) and (ResultValue(LastResult, 'reason') = 'funnel-port-busy');
  if PortBusy then
  begin
    AltPort := ResultValue(LastResult, 'offer');
    Log('remote access: public port 443 is taken; offered ' + AltPort);
    if Pos(',', AltPort) > 0 then
      AltPort := Copy(AltPort, 1, Pos(',', AltPort) - 1);
    if (AltPort <> '') and (MsgBox('Tailscale Funnel on this PC already uses public port 443 for something ' +
      'else, and Setup will not replace it.' + #13#10#13#10 + 'Use public port ' + AltPort + ' for Cognita ' +
      'instead? Its address will then end in :' + AltPort + '.', mbConfirmation, MB_YESNO) = IDYES) then
    begin
      RemoteFunnelPort := AltPort;
      Ok := RemoteAttempt(' --funnel-port ' + AltPort, 'Setting up remote access on port ' + AltPort, Handed);
      if not Handed then
      begin
        RemoteNote := 'Remote access was not set up: Setup could not hand the password to its helper (the Setup helper did not start in time). Run "cognita remote-access" later.';
        Exit;
      end;
      PortBusy := (not Ok) and (ResultValue(LastResult, 'reason') = 'funnel-port-busy');
    end
    else
      Log('remote access: the alternate port was declined or none was offered (offer=[' + AltPort + '])');
  end;
  { Design 19.4 item 11: no port was accepted, so remote access is SKIPPED, which is the user's own
    decision and not a failure: a note on the Finished page, no error box. The helper answered with a
    warning line and no `failed` line, so the generic "reported a failure without saying" text that
    EnsureFailureText builds must not be shown either. }
  if PortBusy then
  begin
    RemoteNote := 'Remote access was skipped: port 443 of your Funnel is in use. Run cognita remote-access to choose another port.';
    Log('remote access: skipped; ' + RemoteNote);
    if RemoteStage = 1 then
    begin
      HideRemoteLinkStage;
      RemoteLabel.Caption := RemoteNote + ' Press Next to finish.';
    end;
    RemoteStage := 2;
    Exit;
  end;
  if Ok then
  begin
    PublicUrl := ResultValue(LastResult, 'public_url');
    RemoteNote := 'Remote access is on.';
    Log('remote access: on; public_url_set=' + IntToStr(Ord(PublicUrl <> '')));
    if RemoteStage = 1 then
    begin
      { A Retry that worked: say so on the page and let Next carry on. }
      HideRemoteLinkStage;
      RemoteLabel.Caption := 'Remote access is on. Press Next to finish.';
    end;
    RemoteStage := 2;
  end
  else
  begin
    Link := ResultValue(LastResult, 'link');
    RemoteNote := 'Remote access was not set up: ' + FailMessage;
    if FailFix <> '' then
      RemoteNote := RemoteNote + ' ' + FailFix;
    RemoteNote := RemoteNote + ' You can try again any time with: cognita remote-access';
    Log('remote access: failed; reason=' + ResultValue(LastResult, 'reason') + ' link_set=' + IntToStr(Ord(Link <> '')));
    if Link <> '' then
      ShowRemoteLinkStage(Link)
    else
      MsgBox('Remote access was not set up.' + #13#10#13#10 + FailText + 'Cognita itself is installed and working. ' +
        'You can try again later with: cognita remote-access', mbInformation, MB_OK);
  end;
end;

procedure RemoteRetryClick(Sender: TObject);
begin
  Log('remote access: Retry pressed');
  RunRemoteAccess;
end;

procedure InitializeWizard;
var
  Idx: Integer;
begin
  WizardUp := True;
  CloseQuietly := False;
  BusyPage := CreateOutputMarqueeProgressPage('Working', 'One moment...');
  ProgressPage := CreateOutputProgressPage('Installing Cognita',
    'Setup is installing Cognita. This can take several minutes; keep this window open.');
  ProgressPage.Msg2Label.OnClick := @LinkClick;
  { The Tailscale sign-in link as selectable text with a Copy button, and the "how long" sentence
    (design 19.5 item 12 and 19.9). Hidden until ShowProgressLine sees a sign-in link. They sit under
    the progress bar, on the page's own surface. }
  ProgressLinkEdit := TNewEdit.Create(ProgressPage);
  ProgressLinkEdit.Parent := ProgressPage.Surface;
  ProgressLinkEdit.Left := 0;
  ProgressLinkEdit.Top := ProgressPage.ProgressBar.Top + ProgressPage.ProgressBar.Height + ScaleY(24);
  ProgressLinkEdit.Width := ProgressPage.SurfaceWidth - ScaleX(92);
  ProgressLinkEdit.ReadOnly := True;
  ProgressLinkEdit.Visible := False;
  ProgressCopyButton := TNewButton.Create(ProgressPage);
  ProgressCopyButton.Parent := ProgressPage.Surface;
  ProgressCopyButton.Left := ProgressLinkEdit.Left + ProgressLinkEdit.Width + ScaleX(8);
  ProgressCopyButton.Top := ProgressLinkEdit.Top - ScaleY(2);
  ProgressCopyButton.Width := ScaleX(84);
  ProgressCopyButton.Height := ScaleY(25);
  ProgressCopyButton.Caption := 'Copy link';
  ProgressCopyButton.OnClick := @ProgressCopyClick;
  ProgressCopyButton.Visible := False;
  ProgressWaitLabel := TNewStaticText.Create(ProgressPage);
  ProgressWaitLabel.Parent := ProgressPage.Surface;
  ProgressWaitLabel.Left := 0;
  ProgressWaitLabel.Top := ProgressLinkEdit.Top + ScaleY(32);
  ProgressWaitLabel.Width := ProgressPage.SurfaceWidth;
  ProgressWaitLabel.AutoSize := False;
  ProgressWaitLabel.WordWrap := True;
  ProgressWaitLabel.Height := ScaleY(34);
  ProgressWaitLabel.Caption := 'Setup waits up to 10 minutes for the sign-in.';
  ProgressWaitLabel.Visible := False;
  { Design 21.4: Skip self-tests, under the bar on the right (the row the Copy button uses; the two are
    never visible together). Shown by ShowProgressLine while the stage is `proof`. }
  ProgressSkipButton := TNewButton.Create(ProgressPage);
  ProgressSkipButton.Parent := ProgressPage.Surface;
  ProgressSkipButton.Width := ScaleX(120);
  ProgressSkipButton.Height := ScaleY(25);
  ProgressSkipButton.Left := ProgressPage.SurfaceWidth - ProgressSkipButton.Width;
  ProgressSkipButton.Top := ProgressPage.ProgressBar.Top + ProgressPage.ProgressBar.Height + ScaleY(22);
  ProgressSkipButton.Caption := 'Skip self-tests';
  ProgressSkipButton.OnClick := @ProgressSkipClick;
  ProgressSkipButton.Visible := False;

  { WSL (shown only when the preflight check says WSL is missing or too old) }
  PageWsl := CreateCustomPage(wpWelcome, 'Turn on WSL',
    'One-time Windows setup before Cognita can install.');
  WslLabel := TNewStaticText.Create(PageWsl);
  WslLabel.Parent := PageWsl.Surface;
  WslLabel.Left := 0;
  WslLabel.Top := 0;
  WslLabel.Width := PageWsl.SurfaceWidth;
  WslLabel.AutoSize := False;
  WslLabel.WordWrap := True;
  WslLabel.Height := ScaleY(110);
  WslLabel.Caption := 'Cognita runs inside WSL, Windows'' built-in Linux support. Setup will turn it on. ' +
    'Windows asks your permission once, and a window shows WSL being installed. Then your PC must ' +
    'restart. Save your work in other programs first. Setup opens again by itself after you sign back in.';
  WslRestartNow := TNewRadioButton.Create(PageWsl);
  WslRestartNow.Parent := PageWsl.Surface;
  WslRestartNow.Left := 0;
  WslRestartNow.Top := ScaleY(120);
  WslRestartNow.Width := PageWsl.SurfaceWidth;
  WslRestartNow.Caption := 'Restart now';
  WslRestartNow.Checked := True;
  WslRestartNow.OnEnter := @RadioEnter;
  WslRestartNow.Visible := False;
  WslRestartLater := TNewRadioButton.Create(PageWsl);
  WslRestartLater.Parent := PageWsl.Surface;
  WslRestartLater.Left := 0;
  WslRestartLater.Top := ScaleY(146);
  WslRestartLater.Width := PageWsl.SurfaceWidth;
  WslRestartLater.Caption := 'Later (Setup continues after your next restart)';
  WslRestartLater.OnEnter := @RadioEnter;
  WslRestartLater.Visible := False;
  WslPageStage := 0;

  { Projects folder }
  PageFolder := CreateInputDirPage(PageWsl.ID, 'Projects folder',
    'Where are the documents Cognita should search?',
    'Cognita reads and writes documents in this folder and its subfolders, in place. Nothing outside it is ' +
    'visible to Cognita.', False, '');
  PageFolder.Add('');
  PageFolder.Values[0] := '';
  FolderNote := TNewStaticText.Create(PageFolder);
  FolderNote.Parent := PageFolder.Surface;
  FolderNote.Left := 0;
  FolderNote.Top := PageFolder.Buttons[0].Top + PageFolder.Buttons[0].Height + ScaleY(24);
  FolderNote.Width := PageFolder.SurfaceWidth;
  FolderNote.AutoSize := False;
  FolderNote.WordWrap := True;
  FolderNote.Height := ScaleY(60);
  FolderNote.Caption := 'If this folder is in OneDrive, right-click it and choose "Always keep on this device".';
  { The reason `roots --validate` gave for a folder Cognita cannot use (design 18.5), shown on the page. }
  FolderReason := TNewStaticText.Create(PageFolder);
  FolderReason.Parent := PageFolder.Surface;
  FolderReason.Left := 0;
  FolderReason.Top := FolderNote.Top + FolderNote.Height;
  FolderReason.Width := PageFolder.SurfaceWidth;
  FolderReason.AutoSize := False;
  FolderReason.WordWrap := True;
  FolderReason.Height := ScaleY(60);
  FolderReason.Font.Color := clRed;
  FolderReason.Caption := '';

  { Admin sign-in. The sub-caption is set per mode in CurPageChanged. The page is created with
    a caption long enough for the update guidance:
    Inno places the fields below the caption's height at creation and never moves them, so a caption
    that later grows to two lines runs under the first field. }
  PageAdmin := CreateInputQueryPage(PageFolder.ID, 'Admin sign-in',
    'Choose the sign-in for Cognita Admin.',
    'Updating Cognita from an older version. Enter your current password once. To change it, run cognita password in a terminal.');
  Idx := PageAdmin.Add('User name:', False);
  PageAdmin.Values[Idx] := 'admin';
  Idx := PageAdmin.Add('Password:', True);
  Idx := PageAdmin.Add('Password again:', True);

  { Acceleration (design 22.7): shown only when preflight saw an NVIDIA card (ShouldSkipPage). The texts, the
    enabled radio, the link and the note are set per run in CurPageChanged; the controls are placed there too,
    because the label's height depends on its text. }
  PageAccel := CreateCustomPage(PageAdmin.ID, 'Acceleration',
    'Choose what Cognita uses for its heavy work.');
  AccelLabelText := TNewStaticText.Create(PageAccel);
  AccelLabelText.Parent := PageAccel.Surface;
  AccelLabelText.Left := 0;
  AccelLabelText.Top := 0;
  AccelLabelText.Width := PageAccel.SurfaceWidth;
  AccelLabelText.AutoSize := False;
  AccelLabelText.WordWrap := True;
  AccelLabelText.Height := ScaleY(48);
  AccelLink := TNewStaticText.Create(PageAccel);
  AccelLink.Parent := PageAccel.Surface;
  AccelLink.Left := 0;
  AccelLink.Top := ScaleY(52);
  AccelLink.Caption := NvidiaDriverUrl;
  StyleAsLink(AccelLink);
  AccelLink.OnClick := @AccelLinkClick;
  AccelLink.Visible := False;
  AccelUseGpu := TNewRadioButton.Create(PageAccel);
  AccelUseGpu.Parent := PageAccel.Surface;
  AccelUseGpu.Left := 0;
  AccelUseGpu.Top := ScaleY(76);
  AccelUseGpu.Width := PageAccel.SurfaceWidth;
  AccelUseGpu.Caption := 'Use the NVIDIA GPU (recommended).';
  AccelUseGpu.OnEnter := @RadioEnter;
  AccelUseCpu := TNewRadioButton.Create(PageAccel);
  AccelUseCpu.Parent := PageAccel.Surface;
  AccelUseCpu.Left := 0;
  AccelUseCpu.Top := ScaleY(100);
  AccelUseCpu.Width := PageAccel.SurfaceWidth;
  AccelUseCpu.Caption := 'Use the CPU only.';
  AccelUseCpu.Checked := True;
  AccelUseCpu.OnEnter := @RadioEnter;
  AccelNote := TNewStaticText.Create(PageAccel);
  AccelNote.Parent := PageAccel.Surface;
  AccelNote.Left := 0;
  AccelNote.Top := ScaleY(132);
  AccelNote.Width := PageAccel.SurfaceWidth;
  AccelNote.AutoSize := False;
  AccelNote.WordWrap := True;
  AccelNote.Height := ScaleY(32);
  AccelNote.Caption := 'Setup checks the card while it installs. If Cognita cannot use it, Setup installs for the CPU and tells you why.';
  AccelNote.Visible := False;

  { Ready }
  PageReady := CreateCustomPage(PageAccel.ID, 'Ready to install', 'Setup has what it needs.');
  ReadyText := TNewStaticText.Create(PageReady);
  ReadyText.Parent := PageReady.Surface;
  ReadyText.Left := 0;
  ReadyText.Top := 0;
  ReadyText.Width := PageReady.SurfaceWidth;
  ReadyText.AutoSize := False;
  ReadyText.WordWrap := True;
  ReadyText.Height := PageReady.SurfaceHeight - ScaleY(40);
  { Design 22.9: the WSL file-cache check box, under the summary and above Advanced. Hidden (and the summary at
    full height) unless wsl_reclaim=unset; ShowReadyPage sets that each time the page is shown. Ticked by
    default. Neither TNewCheckBox nor TNewRadioButton wraps its caption (verified: ISCC rejects WordWrap on both),
    and this caption is three lines, so the box is a bare check square and the design's caption, word for word,
    is a wrapped label beside it that toggles the box when clicked (ReclaimTextClick). }
  ReclaimBox := TNewCheckBox.Create(PageReady);
  ReclaimBox.Parent := PageReady.Surface;
  ReclaimBox.Left := 0;
  ReclaimBox.Width := ScaleX(18);
  ReclaimBox.Height := ScaleY(17);
  ReclaimBox.Top := PageReady.SurfaceHeight - ScaleY(28) - ScaleY(48) - ScaleY(6);
  ReclaimBox.Checked := True;
  ReclaimBox.Visible := False;
  ReclaimText := TNewStaticText.Create(PageReady);
  ReclaimText.Parent := PageReady.Surface;
  ReclaimText.Left := ScaleX(20);
  ReclaimText.Top := ReclaimBox.Top + ScaleY(1);
  ReclaimText.Width := PageReady.SurfaceWidth - ScaleX(20);
  ReclaimText.AutoSize := False;
  ReclaimText.WordWrap := True;
  ReclaimText.Height := ScaleY(48);
  ReclaimText.Cursor := crHand;
  ReclaimText.Caption := 'Let Windows take back memory that WSL holds as file cache (adds one setting to ' +
    '%USERPROFILE%\.wslconfig, which applies to all WSL distros, from the next time WSL starts)';
  ReclaimText.OnClick := @ReclaimTextClick;
  ReclaimText.Visible := False;
  AdvancedButton := TNewButton.Create(PageReady);
  AdvancedButton.Parent := PageReady.Surface;
  AdvancedButton.Left := 0;
  AdvancedButton.Top := PageReady.SurfaceHeight - ScaleY(28);
  AdvancedButton.Width := ScaleX(100);
  AdvancedButton.Height := ScaleY(25);
  AdvancedButton.Caption := 'Advanced...';
  AdvancedButton.OnClick := @AdvancedClick;

  { Remote access (after the install step) }
  PageRemote := CreateCustomPage(wpInstalling, 'Remote access (recommended)',
    'Give your assistant a way to reach Cognita.');
  RemoteLabel := TNewStaticText.Create(PageRemote);
  RemoteLabel.Parent := PageRemote.Surface;
  RemoteLabel.Left := 0;
  RemoteLabel.Top := 0;
  RemoteLabel.Width := PageRemote.SurfaceWidth;
  RemoteLabel.AutoSize := False;
  RemoteLabel.WordWrap := True;
  RemoteLabel.Height := ScaleY(176);
  { 2026-09-29 (Doug): recommended, not optional. What decides it is WHO makes the connection to Cognita:
    an assistant whose own servers connect (claude.ai, Claude Desktop, ChatGPT, Gemini) needs a public
    address; only a tool on this PC that reaches Cognita itself (Claude Code, Codex, Google Antigravity,
    Cursor, a local LLM) can do without, and even then a tunnel is recommended. Cognita listens on this PC
    only (127.0.0.1), so "on this PC" is exact. The old text ("optional", "use Cognita from claude.ai over
    the internet") read as if this were a niche extra. }
  RemoteLabel.Caption := 'Assistants such as claude.ai, Claude Desktop, ChatGPT and Gemini connect to Cognita from ' +
    'their own servers, so they need a public address for it: a tunnel. Setup can set one up now with ' +
    'Tailscale Funnel. It is free, and the address stays the same afterwards.' + #13#10#13#10 +
    'You can do without a tunnel only if you ONLY connect from a tool on this PC that can reach Cognita ' +
    'itself (Claude Code, Codex, Google Antigravity, Cursor, or an LLM you run locally). Even then a tunnel ' +
    'is recommended, so any assistant can use Cognita.' + #13#10#13#10 +
    'If Tailscale is not installed, Setup downloads the official installer (about 40 MB) from ' +
    'pkgs.tailscale.com and runs it; Tailscale asks for permission itself. Cognita''s Admin page is never ' +
    'made public.';
  RemoteSkip := TNewRadioButton.Create(PageRemote);
  RemoteSkip.Parent := PageRemote.Surface;
  RemoteSkip.Left := 0;
  RemoteSkip.Top := ScaleY(212);
  RemoteSkip.Width := PageRemote.SurfaceWidth;
  RemoteSkip.Caption := 'Skip for now. I can do it later with: cognita remote-access';
  RemoteSkip.OnEnter := @RadioEnter;
  RemoteSetUp := TNewRadioButton.Create(PageRemote);
  RemoteSetUp.Parent := PageRemote.Surface;
  RemoteSetUp.Left := 0;
  RemoteSetUp.Top := ScaleY(186);
  RemoteSetUp.Width := PageRemote.SurfaceWidth;
  RemoteSetUp.Caption := 'Set up remote access with Tailscale Funnel now (recommended)';
  RemoteSetUp.Checked := True;
  RemoteSetUp.OnEnter := @RadioEnter;
  { After a failure that carries a link (design 18.4: Funnel not enabled on the tailnet): the link, and
    Retry, which hands the password over again and reruns the verb. Hidden until then. }
  RemoteLinkLabel := TNewStaticText.Create(PageRemote);
  RemoteLinkLabel.Parent := PageRemote.Surface;
  RemoteLinkLabel.Left := 0;
  RemoteLinkLabel.Top := ScaleY(130);
  RemoteLinkLabel.Width := PageRemote.SurfaceWidth;
  RemoteLinkLabel.AutoSize := False;
  RemoteLinkLabel.Height := ScaleY(18);
  StyleAsLink(RemoteLinkLabel);
  RemoteLinkLabel.OnClick := @RemoteLinkClick;
  RemoteLinkLabel.Visible := False;
  { The same link as selectable text with a Copy button (design 19.5 item 12); ShowRemoteLinkStage
    places them under the link. }
  RemoteLinkEdit := TNewEdit.Create(PageRemote);
  RemoteLinkEdit.Parent := PageRemote.Surface;
  RemoteLinkEdit.Left := 0;
  RemoteLinkEdit.Top := ScaleY(150);
  RemoteLinkEdit.Width := PageRemote.SurfaceWidth - ScaleX(92);
  RemoteLinkEdit.ReadOnly := True;
  RemoteLinkEdit.Visible := False;
  RemoteCopyButton := TNewButton.Create(PageRemote);
  RemoteCopyButton.Parent := PageRemote.Surface;
  RemoteCopyButton.Left := RemoteLinkEdit.Left + RemoteLinkEdit.Width + ScaleX(8);
  RemoteCopyButton.Top := RemoteLinkEdit.Top - ScaleY(2);
  RemoteCopyButton.Width := ScaleX(84);
  RemoteCopyButton.Height := ScaleY(25);
  RemoteCopyButton.Caption := 'Copy link';
  RemoteCopyButton.OnClick := @RemoteCopyClick;
  RemoteCopyButton.Visible := False;
  RemoteRetryButton := TNewButton.Create(PageRemote);
  RemoteRetryButton.Parent := PageRemote.Surface;
  RemoteRetryButton.Left := 0;
  RemoteRetryButton.Top := ScaleY(160);
  RemoteRetryButton.Width := ScaleX(100);
  RemoteRetryButton.Height := ScaleY(25);
  RemoteRetryButton.Caption := 'Retry';
  RemoteRetryButton.OnClick := @RemoteRetryClick;
  RemoteRetryButton.Visible := False;
  RemoteStage := 0;

  { Finished page extras (visible only after a failure) }
  DiagButton := TNewButton.Create(WizardForm);
  DiagButton.Parent := WizardForm.FinishedPage;
  DiagButton.Caption := 'Save diagnostics';
  DiagButton.Width := ScaleX(120);
  DiagButton.Height := ScaleY(25);
  DiagButton.OnClick := @SaveDiagnosticsClick;
  DiagButton.Visible := False;
  LogLink := TNewStaticText.Create(WizardForm);
  LogLink.Parent := WizardForm.FinishedPage;
  LogLink.Caption := 'Open the log folder';
  LogLink.Cursor := crHand;
  LogLink.Font.Color := clBlue;
  LogLink.Font.Style := [fsUnderline];
  LogLink.OnClick := @OpenLogsClick;
  LogLink.Visible := False;
  ReportLink := TNewStaticText.Create(WizardForm);
  ReportLink.Parent := WizardForm.FinishedPage;
  ReportLink.Caption := 'Report a problem';
  StyleAsLink(ReportLink);
  ReportLink.OnClick := @ReportProblemClick;
  ReportLink.Visible := False;

  { Finished page after a successful install: the addresses are links (the same link control as
    "Open the log folder"), then how to connect a client and the everyday commands (design C12, P1). }
  FinAdminPre := NewFinishedText('Admin:');
  FinAdminLink := NewFinishedText('');
  StyleAsLink(FinAdminLink);
  FinAdminLink.OnClick := @FinAdminClick;
  FinAdminPost := NewFinishedText('');
  FinMcpPre := NewFinishedText('MCP:');
  FinMcpLink := NewFinishedText('');
  StyleAsLink(FinMcpLink);
  FinMcpLink.OnClick := @FinMcpClick;
  FinPubPre := NewFinishedText('Public:');
  FinPubLink := NewFinishedText('');
  StyleAsLink(FinPubLink);
  FinPubLink.OnClick := @FinPubClick;
  FinConnectLabel := NewFinishedText('');
  FinConnectLabel.AutoSize := False;
  FinConnectLabel.WordWrap := True;
  FinMemo := TNewMemo.Create(WizardForm);
  FinMemo.Parent := WizardForm.FinishedPage;
  FinMemo.ReadOnly := True;
  FinMemo.ScrollBars := ssVertical;
  FinMemo.WordWrap := True;
  FinMemo.Visible := False;
end;

function InitializeSetup: Boolean;
begin
  Result := False;
  ChosenMcpPort := DefaultMcpPort;
  ChosenAdminPort := DefaultAdminPort;
  ChosenDataDir := ExpandConstant('{localappdata}\Cognita\wsl');
  ChosenWorkspaceOn := True;
  RunMode := 0;
  ResumeSwitch := HasSwitch('/resume');
  { Setup started from a PowerShell 7 window inherits 7's PSModulePath, and every Windows
    PowerShell 5.1 helper it runs would then fail to load 5.1's own built-in modules (seen with
    Get-AuthenticodeSignature). Without the variable, 5.1 builds its default module path. The
    cognita.exe launcher does the same. }
  SetEnvironmentVariableW('PSModulePath', 0);
  Log('setup start: version={#Version} revision={#Revision} resume_switch=' + IntToStr(Ord(ResumeSwitch)) +
    ' srcexe=' + ExpandConstant('{srcexe}'));
  if WizardSilent then
  begin
    Log('setup: a silent install is not supported; Setup asks questions (folder, password). Refusing.');
    Exit;
  end;
  if not FileExists(PsExe) then
  begin
    MsgBox('Windows PowerShell 5.1 was not found at ' + PsExe + '. Cognita Setup needs it.', mbError, MB_OK);
    Exit;
  end;
  try
    ExtractTemporaryFile('CognitaWin.ps1');
  except
    Log('setup: could not extract the helper: ' + GetExceptionMessage);
    MsgBox('Setup could not unpack its helper script: ' + GetExceptionMessage, mbError, MB_OK);
    Exit;
  end;
  HelperFolder := ExpandConstant('{tmp}');
  if not RunHelper('state', '') then
  begin
    MsgBox('Setup could not read the current state of this PC.' + #13#10#13#10 + FailText +
      'Setup log: ' + ExpandConstant('{log}'), mbError, MB_OK);
    Exit;
  end;
  ReadState;
  InstallMode := ModeFromState(LastResult, '{#Version}');
  Log('setup: install_mode=' + IntToStr(InstallMode) + ' (0 fresh, 1 repair, 2 update, 3 finish, 4 reinstall) from distro_owned=' +
    IntToStr(Ord(StDistroPresent)) + ' installed=' + IntToStr(Ord(StInstalled)) + ' linux_version=' +
    StLinuxVersion + ' setup_version={#Version} admin_user=' + StAdminUser);
  { Design 19.2 item 6: no downgrade. The version compare is numeric, so 14.10 is newer than 14.9. Only an
    installed Cognita counts (installed=1 means an owned distro AND a recorded linux_version): a stale
    version with no distro is a fresh install, and the helper resets those records. The helper's `update`
    verb refuses the same case (reason=older-setup), so this is the friendly front door, not the only lock. }
  if StInstalled and SetupIsOlder(StLinuxVersion, '{#Version}') then
  begin
    Log('setup: refusing a downgrade; installed ' + StLinuxVersion + ' is newer than this Setup {#Version}');
    MsgBox(DowngradeText(StLinuxVersion, '{#Version}'), mbError, MB_OK);
    Exit;
  end;
  Log('setup: no downgrade (installed=[' + StLinuxVersion + '] setup={#Version} compare=' +
    IntToStr(CompareVersions(StLinuxVersion, '{#Version}')) + ')');
  Result := True;
end;

{ ---- Copying a Setup or uninstall log into %LOCALAPPDATA%\Cognita\logs (design 18.5) ---- }

{ The local date and time as yyyyMMdd-HHmmss (the machine's local time, like every Cognita log). }
function StampNow: String;
begin
  Result := GetDateTimeString('yyyymmdd-hhnnss', #0, #0);
end;

{ Copies the log Inno is writing (the log constant) to DestFile. The log is open for writing while
  Setup runs, so CopyFile can be refused; the fallback reads the bytes with LoadStringFromFile.
  A missing or unreadable log is logged and skipped; it never stops Setup or the uninstaller. }
function CopyOwnLog(const DestFile: String): Boolean;
var
  Src: String;
  Data: AnsiString;
begin
  Result := False;
  Src := '';
  try
    Src := ExpandConstant('{log}');
  except
    Log('log copy: no log file is being written; nothing to copy (' + GetExceptionMessage + ')');
    Exit;
  end;
  if (Src = '') or (not FileExists(Src)) then
  begin
    Log('log copy: the log file ' + Src + ' does not exist; nothing to copy');
    Exit;
  end;
  try
    ForceDirectories(ExtractFileDir(DestFile));
    if CopyFile(Src, DestFile, False) then
    begin
      Result := True;
      Log('log copy: ' + Src + ' -> ' + DestFile + ' (CopyFile)');
      Exit;
    end;
    Log('log copy: CopyFile of ' + Src + ' was refused; reading the bytes instead');
    if LoadStringFromFile(Src, Data) and SaveStringToFile(DestFile, Data, False) then
    begin
      Result := True;
      Log('log copy: ' + Src + ' -> ' + DestFile + ' (' + IntToStr(Length(Data)) + ' bytes read and written)');
    end
    else
      Log('log copy: could not copy ' + Src + ' to ' + DestFile);
  except
    Log('log copy: exception copying ' + Src + ' to ' + DestFile + ': ' + GetExceptionMessage);
  end;
end;

procedure DeinitializeSetup;
begin
  AdminPassword := '';
  { Inno writes Setup's own log under %TEMP%, where nobody looks for it: copy it beside the helper
    logs, so support has both in one folder (design 18.5). }
  CopyOwnLog(LogsDir + '\setup-' + StampNow + '.log');
end;

{ Tight: the WSL check box takes room from the summary (design 22.9), so the two blank lines between its blocks
  become one line break; every word stays. }
function ReadySummary(const Tight: Boolean): String;
var
  Head, Gap, AccelText, Reason: String;
begin
  Gap := #13#10#13#10;
  if Tight then
    Gap := #13#10;
  if InstallMode = ModeUpdate then
  begin
    if StLinuxVersion <> '' then
      Head := 'Update Cognita ' + StLinuxVersion + ' to {#Version}.'
    else
      Head := 'Update Cognita to {#Version}.';
  end
  else if InstallMode = ModeRepair then
    Head := 'Repair Cognita {#Version} (Setup runs the install again over what is there).'
  else if InstallMode = ModeReinstall then
    Head := 'Reinstall Cognita {#Version} (your data is kept).'
  else if InstallMode = ModeFinish then
    Head := 'Finish installing Cognita {#Version} (an earlier install did not complete; Setup continues it).'
  else
    Head := 'Install Cognita {#Version}.';
  Result := Head + Gap +
    'Projects folder:  ' + PageFolder.Values[0] + #13#10;
  { Design 19.2 item 14: a user name only when it is known (recorded by the helper) or really passed
    (fresh and finish). Over an installed Cognita that never told us the name, Setup does not know it and
    passes none, so the line is left out rather than showing a name that means nothing. }
  if AdminUser <> '' then
    Result := Result + 'Admin sign-in:  ' + AdminUser + #13#10;
  { Design 22.1, 22.12 items 1(b), 1(c) and 10: the acceleration the install will have, in every mode; the
    reason in parentheses when the GPU was not offered; "unchanged" when the install keeps a profile Setup
    does not know. }
  AccelText := AccelReadyValue(EffectiveAccel);
  Reason := AccelReadyReason(AccelMode, InstallMode, NvState, EffectiveAccel);
  if Reason <> '' then
    AccelText := AccelText + ' ' + Reason;
  Result := Result +
    'Ports:  MCP ' + IntToStr(ChosenMcpPort) + ', Admin ' + IntToStr(ChosenAdminPort) + #13#10 +
    'Workspace:  ' + OnOff(ChosenWorkspaceOn) + #13#10 +
    'Acceleration:  ' + AccelText + #13#10 +
    'Cognita keeps its data in:  ' + ChosenDataDir + Gap;
  Log('ready page: acceleration line=[' + AccelText + '] effective=[' + EffectiveAccel + '] page_mode=' + IntToStr(AccelMode) +
    ' mode=' + IntToStr(InstallMode) + ' touched=' + IntToStr(Ord(AccelTouched)) + ' images_bytes=' + IntToStr(ImagesBytes));
  { Over an installed Cognita the images and models are already here, so only what changed is
    fetched; the compiled-in total would overstate it. }
  if RunsOverInstalled then
    Result := Result + 'Setup downloads only what this PC does not already have (at most about ' +
      FormatBytes(DownloadBytes) + ') and needs about '
  else
    Result := Result + 'Setup then downloads about ' + FormatBytes(DownloadBytes) +
      ' (Cognita and its search models) and needs about ';
  Result := Result + FormatBytes(DiskNeededBytes) + ' of free disk space. Pressing Install checks your ports and disk ' +
    'space again with these choices.';
  if InstallMode = ModeUpdate then
    Result := Result + ' An update keeps your ports and Workspace setting; to change them, run Setup again afterwards.'
  else if DataLocationLocked then
    Result := Result + ' Use Advanced to change the ports or to turn Workspace off.'
  else
    Result := Result + ' Use Advanced to change the ports, where Cognita keeps its data, or to turn Workspace off.';
end;

function ShouldSkipPage(PageID: Integer): Boolean;
begin
  Result := False;
  if PageID = PageWsl.ID then
    Result := not ((WslState = 'missing') or (WslState = 'old'))
  else if PageID = PageAccel.ID then
    Result := AccelMode = AccelHidden
  else if PageID = PageRemote.ID then
    Result := not InstallOk;
end;

{ One row of the Finished page's address list: a caption, the address as a link, and optionally a
  trailing plain text (the sign-in user). Y moves down one row. }
procedure PlaceLinkRow(const Pre, Link: TNewStaticText; const LinkText: String;
  const Post: TNewStaticText; const PostText: String; const X: Integer; var Y: Integer);
begin
  Pre.Left := X;
  Pre.Top := Y;
  Pre.Visible := True;
  Link.Caption := LinkText;
  Link.Left := X + ScaleX(62);
  Link.Top := Y;
  Link.Visible := True;
  if Post <> nil then
  begin
    Post.Caption := PostText;
    Post.Left := Link.Left + Link.Width + ScaleX(8);
    Post.Top := Y;
    Post.Visible := True;
  end;
  Y := Y + ScaleY(18);
end;

{ The Finished page after a successful install (design 0 step 9, C12, P1): that Cognita starts at
  sign-in, the Admin / MCP / public addresses as links, how to connect a client (the same sentence
  the Linux installer's finish screen gives), and a small scrolling memo with the everyday commands.
  The wizard's own text label is sized to its text; the memo takes whatever height is left above the
  "Open Cognita Admin" check box. }
procedure LayoutFinishedSuccess;
var
  X, Y, W, Bottom: Integer;
  Cap, WsText, UserText, Notes, AccelText: String;
begin
  X := WizardForm.FinishedLabel.Left;
  W := WizardForm.FinishedLabel.Width;
  WizardForm.FinishedHeadingLabel.Caption := 'Cognita is installed';
  { Design 19.4 item 2: "Cognita starts when you sign in to Windows" is dropped when the helper warned
    that the sign-in task could not be registered, because then it is not true. }
  Cap := 'Cognita {#Version} is running.';
  if not InstallSignInWarned then
    Cap := Cap + ' Cognita starts when you sign in to Windows.';
  { Workspace shows what the install/update result (`status`) reported, which can differ from the
    Ready choice (for example Workspace turned off because this PC's WSL has no /dev/kvm). Only when
    the result said nothing does the choice stand in for it. }
  if ResultWorkspace <> '' then
    WsText := ResultWorkspace
  else
    WsText := OnOff(ChosenWorkspaceOn);
  Cap := Cap + #13#10 + 'Workspace:  ' + WsText;
  { Design 22.7 and 22.12 item 11: the Acceleration line comes only from the result; no line when it did not say. }
  AccelText := AccelFinishedLine(ResultAccel);
  if AccelText <> '' then
    Cap := Cap + #13#10 + AccelText;
  Log('finished page: acceleration=[' + AccelText + '] from_result=' + IntToStr(Ord(ResultAccel <> '')) +
    ' result_value=[' + ResultAccel + ']');
  if RemoteNote <> '' then
    Cap := Cap + #13#10 + RemoteNote;
  WizardForm.FinishedLabel.Caption := Cap;
  WizardForm.FinishedLabel.AdjustHeight;
  Y := WizardForm.FinishedLabel.Top + WizardForm.FinishedLabel.Height + ScaleY(10);
  { The user name only when it is known or really passed (design 19.2 item 14). }
  UserText := '';
  if AdminUser <> '' then
    UserText := '(user: ' + AdminUser + ')';
  PlaceLinkRow(FinAdminPre, FinAdminLink, AdminUrl(''), FinAdminPost, UserText, X, Y);
  PlaceLinkRow(FinMcpPre, FinMcpLink, 'http://localhost:' + IntToStr(ChosenMcpPort) + '/', nil, '', X, Y);
  if PublicUrl <> '' then
    PlaceLinkRow(FinPubPre, FinPubLink, PublicUrl, nil, '', X, Y);
  FinConnectLabel.Left := X;
  FinConnectLabel.Top := Y + ScaleY(4);
  FinConnectLabel.Width := W;
  FinConnectLabel.Caption := 'To connect claude.ai or ChatGPT: open Cognita Admin, go to Connectors, create a ' +
    'connector, and use its address.';
  { Design 21.4: one line after the addresses when the self-tests were skipped; nothing else here changes. }
  if FinProofSkipped then
    FinConnectLabel.Caption := FinConnectLabel.Caption + #13#10 + SkippedProofNote;
  Log('finished page: skipped-self-tests note shown=' + IntToStr(Ord(FinProofSkipped)));
  FinConnectLabel.AdjustHeight;
  FinConnectLabel.Visible := True;
  Y := FinConnectLabel.Top + FinConnectLabel.Height + ScaleY(8);
  { The "Open Cognita Admin" check box sits at the bottom; the memo fills the space above it. }
  Bottom := WizardForm.FinishedPage.ClientHeight;
  if WizardForm.RunList.Visible then
  begin
    WizardForm.RunList.Height := ScaleY(26);
    WizardForm.RunList.Top := Bottom - WizardForm.RunList.Height - ScaleY(2);
    Bottom := WizardForm.RunList.Top - ScaleY(6);
  end;
  FinMemo.Left := X;
  FinMemo.Top := Y;
  FinMemo.Width := W;
  FinMemo.Height := Bottom - Y;
  if FinMemo.Height < ScaleY(48) then
    FinMemo.Height := ScaleY(48);
  { The memo starts with what the install run warned about (design 19.4 item 2): those lines used to be
    shown once in a box during the run, if at all, and were gone from the Finished page. }
  Notes := '';
  if Trim(InstallWarnText) <> '' then
    Notes := 'Please note:' + #13#10#13#10 + InstallWarnText;
  FinMemo.Text := Notes + 'Every day: use the Start menu (Cognita Admin, Cognita Status, Cognita Diagnostics), or type ' +
    'these in a terminal window:' + #13#10 +
    '  cognita status' + #13#10 +
    '  cognita logs app -f' + #13#10 +
    '  cognita start | stop | restart' + #13#10 +
    '  cognita password' + #13#10 +
    '  cognita add-folder' + #13#10 +
    '  cognita remote-access' + #13#10 +
    '  cognita diagnostics' + #13#10#13#10 +
    'Update: run a newer Cognita Setup.' + #13#10 +
    'Uninstall: Settings > Apps > Installed apps.';
  FinMemo.Visible := True;
  Log('finished page: success layout label_h=' + IntToStr(WizardForm.FinishedLabel.Height) + ' memo_top=' + IntToStr(Y) +
    ' memo_h=' + IntToStr(FinMemo.Height) + ' page_h=' + IntToStr(WizardForm.FinishedPage.ClientHeight) +
    ' public_link=' + IntToStr(Ord(PublicUrl <> '')) + ' warnings_chars=' + IntToStr(Length(InstallWarnText)) +
    ' sign_in_warned=' + IntToStr(Ord(InstallSignInWarned)) + ' workspace=' + WsText +
    ' workspace_from_status=' + IntToStr(Ord(ResultWorkspace <> '')) + ' user_shown=' + IntToStr(Ord(AdminUser <> '')));
end;

{ Fills and lays out the Acceleration page for this run (design 22.1 and 22.7): the text for the page's flavor,
  the GPU radio enabled only when it is offered, the pick restored from ChosenAccel (Back and Next keep it), the
  note only when offered, the driver link only for an old driver. }
procedure ShowAccelPage;
var
  Mode: Integer;
  Card, Drv, GpuCap, CpuCap: String;
  Y: Integer;
begin
  Mode := AccelMode;
  Card := NvName;
  if Card = '' then
    Card := 'NVIDIA graphics card';
  Drv := NvDriver;
  if Drv = '' then
    Drv := 'version unknown';
  if Mode = AccelOffer then
    AccelLabelText.Caption := 'Setup found an ' + Card + ' (driver ' + Drv + '). Cognita can use it to index your ' +
      'documents and read text in images much faster than the CPU.'
  else if Mode = AccelOldDriver then
    AccelLabelText.Caption := 'Your NVIDIA driver is ' + Drv + '. Cognita needs driver 580 or newer to use the card. ' +
      'Update the driver, then run Setup again and choose NVIDIA.'
  else
    AccelLabelText.Caption := 'This version of Cognita has no NVIDIA build, so it installs for the CPU.';
  AccelLabelText.Width := PageAccel.SurfaceWidth;
  AccelLabelText.AdjustHeight;
  Y := AccelLabelText.Top + AccelLabelText.Height + ScaleY(8);
  AccelLink.Visible := Mode = AccelOldDriver;
  if AccelLink.Visible then
  begin
    AccelLink.Top := Y;
    Y := Y + AccelLink.Height + ScaleY(10);
  end;
  { The sizes are the compiled-in image sizes, formatted like every other size on Setup's pages. }
  GpuCap := 'Use the NVIDIA GPU (recommended).';
  if NvidiaBuildBytes > 0 then
    GpuCap := GpuCap + ' Cognita downloads about ' + FormatBytes(NvidiaBuildBytes) + '.';
  CpuCap := 'Use the CPU only. Cognita downloads about ' + FormatBytes(StrToInt64('{#SizeCognitaCpu}')) + '.';
  AccelUseGpu.Caption := GpuCap;
  AccelUseCpu.Caption := CpuCap;
  AccelUseGpu.Top := Y;
  AccelUseCpu.Top := Y + ScaleY(24);
  AccelNote.Top := AccelUseCpu.Top + ScaleY(32);
  AccelUseGpu.Enabled := Mode = AccelOffer;
  AccelNote.Visible := Mode = AccelOffer;
  { CPU first, then GPU: checking a radio unchecks its sibling, and a disabled GPU radio must never stay checked. }
  AccelUseCpu.Checked := True;
  if (Mode = AccelOffer) and (ChosenAccel = 'nvidia') then
    AccelUseGpu.Checked := True;
  Log('accel page shown: mode=' + IntToStr(Mode) + ' card=[' + NvName + '] driver=[' + NvDriver + '] gpu_enabled=' +
    IntToStr(Ord(AccelUseGpu.Enabled)) + ' chosen=[' + ChosenAccel + '] gpu_checked=' + IntToStr(Ord(AccelUseGpu.Checked)) +
    ' link=' + IntToStr(Ord(AccelLink.Visible)) + ' note=' + IntToStr(Ord(AccelNote.Visible)) +
    ' label_h=' + IntToStr(AccelLabelText.Height));
end;

{ The Ready page (design 22.9): the check box when wsl_reclaim=unset, the summary above it at the height that is
  left. Logs how much room the summary needs and has, so an overflow is in the log and not only on a screen. }
procedure ShowReadyPage;
var
  Avail, Needed: Integer;
  BoxShown: Boolean;
begin
  BoxShown := WslReclaimBoxVisible(WslReclaim);
  ReclaimBox.Visible := BoxShown;
  ReclaimText.Visible := BoxShown;
  if BoxShown then
    Avail := ReclaimBox.Top - ScaleY(4)
  else
    Avail := PageReady.SurfaceHeight - ScaleY(40);
  ReadyText.Caption := ReadySummary(BoxShown);
  ReadyText.Height := Avail;
  ReadyText.AdjustHeight;
  Needed := ReadyText.Height;
  ReadyText.Height := Avail;
  Log('ready page: wsl_reclaim=[' + WslReclaim + '] box_visible=' + IntToStr(Ord(BoxShown)) + ' box_ticked=' +
    IntToStr(Ord(ReclaimBox.Checked)) + ' summary_height_available=' + IntToStr(Avail) + ' needed=' + IntToStr(Needed) +
    ' overflow=' + IntToStr(Ord(Needed > Avail)));
end;

procedure CurPageChanged(CurPageID: Integer);
var
  Top: Integer;
  Cap: String;
begin
  WizardForm.NextButton.Caption := SetupMessage(msgButtonNext);
  WizardForm.BackButton.Visible := True;
  { P1 showed a Back button on the Installing page while the install ran (design 18.5). That page is
    Inno's own wpInstalling here and, from ssPostInstall on, our progress page: RunInstallFlow and
    RemoteAttempt hide the button right after they Show it, because Show does not come through
    here. Nothing
    after the install step can be gone back to either, so the Remote access and Finished pages have
    none, and the Welcome page has nothing to go back to (the line above had been showing a Back
    button on the Welcome and Finished pages too). }
  if (CurPageID = wpWelcome) or (CurPageID = wpInstalling) or (CurPageID = PageRemote.ID) or (CurPageID = wpFinished) then
    WizardForm.BackButton.Visible := False;
  if CurPageID = wpWelcome then
  begin
    if ResumeSwitch or StResumeAfterWsl then
      WizardForm.WelcomeLabel2.Caption := 'Setup is continuing after the restart. Press Next to check this PC again and finish installing Cognita.'
    else if InstallMode = ModeUpdate then
    begin
      if StLinuxVersion <> '' then
        WizardForm.WelcomeLabel2.Caption := 'Cognita ' + StLinuxVersion + ' is installed. This Setup updates it to {#Version}. Press Next to check this PC.'
      else
        WizardForm.WelcomeLabel2.Caption := 'Cognita is installed. This Setup updates it to {#Version}. Press Next to check this PC.';
    end
    else if InstallMode = ModeRepair then
      WizardForm.WelcomeLabel2.Caption := 'Cognita is already set up on this PC. This Setup checks it and repairs what is wrong. Press Next to check this PC.'
    else if InstallMode = ModeReinstall then
      WizardForm.WelcomeLabel2.Caption := 'Cognita was uninstalled from this PC and its data was kept. This Setup installs Cognita {#Version} again over that data. ' +
        'Press Next to check this PC.'
    else if InstallMode = ModeFinish then
      WizardForm.WelcomeLabel2.Caption := 'An earlier install of Cognita on this PC did not finish. This Setup continues it, and nothing already set up is lost. ' +
        'Press Next to check this PC.'
    else
      WizardForm.WelcomeLabel2.Caption := 'This installs Cognita {#Version} on your PC, and starts it whenever you sign in to Windows. ' +
        'Press Next to check that this PC can run it.';
  end
  else if CurPageID = PageWsl.ID then
  begin
    if WslPageStage = 0 then
      WizardForm.NextButton.Caption := 'Turn on WSL'
    else
    begin
      WizardForm.NextButton.Caption := 'Continue';
      WizardForm.BackButton.Visible := False;
    end;
  end
  else if CurPageID = PageFolder.ID then
  begin
    if FolderLocked then
    begin
      { Every non-fresh mode once root 1 exists: the folder is the one Cognita already uses (design
        18.2, 19.2 item 7). The helper refuses another one anyway. }
      PageFolder.Values[0] := StRoot1;
      PageFolder.Edits[0].ReadOnly := True;
      PageFolder.Buttons[0].Enabled := False;
      FolderNote.Caption := 'To add another folder later: cognita add-folder';
    end
    else
    begin
      { Fresh (and a finish that has no root 1 yet): editable. A leftover record of a folder is offered. }
      if (PageFolder.Values[0] = '') and (StRoot1 <> '') then
        PageFolder.Values[0] := StRoot1;
      PageFolder.Edits[0].ReadOnly := False;
      PageFolder.Buttons[0].Enabled := True;
      FolderNote.Caption := 'If this folder is in OneDrive, right-click it and choose "Always keep on this device".';
    end;
  end
  else if CurPageID = PageAdmin.ID then
  begin
    if UserLocked then
    begin
      { Design 19.2 item 14: read-only over an installed Cognita; "(unchanged)" when the helper never
        recorded a name (AdminPageNext then passes and remembers no name). }
      if StAdminUser <> '' then
        PageAdmin.Values[0] := StAdminUser
      else
        PageAdmin.Values[0] := '(unchanged)';
      PageAdmin.Edits[0].ReadOnly := True;
    end
    else
      PageAdmin.Edits[0].ReadOnly := False;
    if not RunsOverInstalled then
    begin
      PageAdmin.SubCaptionLabel.Caption := 'You use this user name and password to open Cognita Admin in your browser. Type the password twice.';
      PageAdmin.PromptLabels[1].Caption := 'Password:';
    end
    else
    begin
      { Repair, update and reinstall use the existing password only to check Cognita afterwards. }
      PageAdmin.PromptLabels[1].Caption := 'Current password:';
      if InstallMode = ModeUpdate then
      begin
        if StLinuxVersion <> '' then
          PageAdmin.SubCaptionLabel.Caption := 'Updating Cognita ' + StLinuxVersion + ' to {#Version}. Enter your current Admin password once. To change it, run cognita password in a terminal.'
        else
          PageAdmin.SubCaptionLabel.Caption := 'Updating Cognita to {#Version}. Enter your current Admin password once. To change it, run cognita password in a terminal.';
      end
      else
        PageAdmin.SubCaptionLabel.Caption := 'Enter your current Admin password once. To change it, run "cognita password" in a terminal.';
    end;
    PageAdmin.PromptLabels[2].Visible := not RunsOverInstalled;
    PageAdmin.Edits[2].Visible := not RunsOverInstalled;
  end
  else if CurPageID = PageAccel.ID then
    ShowAccelPage
  else if CurPageID = PageReady.ID then
  begin
    WizardForm.NextButton.Caption := SetupMessage(msgButtonInstall);
    ShowReadyPage;
    { Advanced is the page's only control, so Inno focuses it and Enter opened Advanced instead of
      installing. Install is what Enter means here. }
    WizardForm.ActiveControl := WizardForm.NextButton;
  end
  else if CurPageID = PageRemote.ID then
    WizardForm.NextButton.Caption := SetupMessage(msgButtonNext)
  else if CurPageID = wpFinished then
  begin
    { The line at the top of this procedure sets every page's button to "Next"; the last page's is "Finish". }
    WizardForm.NextButton.Caption := SetupMessage(msgButtonFinish);
    DiagButton.Visible := False;
    LogLink.Visible := False;
    FinAdminPre.Visible := False;
    FinAdminLink.Visible := False;
    FinAdminPost.Visible := False;
    FinMcpPre.Visible := False;
    FinMcpLink.Visible := False;
    FinPubPre.Visible := False;
    FinPubLink.Visible := False;
    FinConnectLabel.Visible := False;
    FinMemo.Visible := False;
    if InstallOk then
      LayoutFinishedSuccess
    else if InstallRestart then
    begin
      { Design 19.11 R1: NeedRestart returned True, so Inno shows its own "Yes, restart now / No, I will
        restart later" radio buttons here and restarts Windows after Setup has exited. Our text replaces
        Inno's label; the radio buttons are kept visible and placed under it explicitly, so a label of a
        different height than Inno's cannot cover them. }
      WizardForm.FinishedHeadingLabel.Caption := 'A restart is needed';
      if InstallResumeSaved then
        WizardForm.FinishedLabel.Caption := 'Windows must restart to finish turning on WSL. Save your work in other ' +
          'programs first. Setup opens again by itself after you sign back in, and continues from there.'
      else
        WizardForm.FinishedLabel.Caption := 'Windows must restart to finish turning on WSL. Save your work in other ' +
          'programs first. Setup could not save where it stopped, so after the restart run Setup again to continue.';
      WizardForm.FinishedLabel.AdjustHeight;
      WizardForm.YesRadio.Left := WizardForm.FinishedLabel.Left;
      WizardForm.YesRadio.Width := WizardForm.FinishedLabel.Width;
      WizardForm.YesRadio.Top := WizardForm.FinishedLabel.Top + WizardForm.FinishedLabel.Height + ScaleY(16);
      WizardForm.NoRadio.Left := WizardForm.YesRadio.Left;
      WizardForm.NoRadio.Width := WizardForm.YesRadio.Width;
      WizardForm.NoRadio.Top := WizardForm.YesRadio.Top + WizardForm.YesRadio.Height + ScaleY(6);
      WizardForm.YesRadio.Visible := True;
      WizardForm.NoRadio.Visible := True;
      Log('finished page: restart layout resume_saved=' + IntToStr(Ord(InstallResumeSaved)) + ' label_h=' +
        IntToStr(WizardForm.FinishedLabel.Height) + ' yes_top=' + IntToStr(WizardForm.YesRadio.Top) +
        ' no_top=' + IntToStr(WizardForm.NoRadio.Top));
    end
    else if InstallAttempted then
    begin
      { Design 19.4 item 10: the same layout as the success page. A short label at the top (what
        failed, and that the earlier Cognita is untouched), the failure text in a scrolling read-only
        memo (a long failure used to push the buttons off the page), and a fixed row at the bottom
        (Save diagnostics, Open the log folder). The heading says which run it was. }
      WizardForm.FinishedHeadingLabel.Caption := FailHeading(InstallMode);
      Cap := 'Step that failed:  ' + FailStage;
      if StillThereText(InstallMode) <> '' then
        Cap := Cap + #13#10#13#10 + StillThereText(InstallMode);
      WizardForm.FinishedLabel.Caption := Cap;
      WizardForm.FinishedLabel.AdjustHeight;
      Top := WizardForm.FinishedPage.ClientHeight - DiagButton.Height - ScaleY(2);
      DiagButton.Left := WizardForm.FinishedLabel.Left;
      DiagButton.Top := Top;
      if FinDiagZip <> '' then
        DiagButton.Caption := 'Show the file'
      else
        DiagButton.Caption := 'Save diagnostics';
      DiagButton.OnClick := @ShowDiagFileClick;
      DiagButton.Visible := True;
      LogLink.Left := DiagButton.Left + DiagButton.Width + ScaleX(16);
      LogLink.Top := Top + ScaleY(4);
      LogLink.Visible := True;
      ReportLink.Left := LogLink.Left + LogLink.Width + ScaleX(24);
      ReportLink.Top := LogLink.Top;
      ReportLink.Visible := True;
      FinMemo.Left := WizardForm.FinishedLabel.Left;
      FinMemo.Top := WizardForm.FinishedLabel.Top + WizardForm.FinishedLabel.Height + ScaleY(10);
      FinMemo.Width := WizardForm.FinishedLabel.Width;
      FinMemo.Height := Top - ScaleY(8) - FinMemo.Top;
      if FinMemo.Height < ScaleY(48) then
        FinMemo.Height := ScaleY(48);
      { (A source line may not START with #13#10: ISPP reads a leading # as a preprocessor directive.) }
      FinMemo.Text := FailText + 'Log folder:  ' + LogsDir + #13#10 + 'Setup log:  ' + ExpandConstant('{log}') + #13#10#13#10 +
        'Nothing is lost: run Setup again to continue.' + #13#10#13#10 + DiagnosticsSentence(FinDiagZip);
      FinMemo.Visible := True;
      Log('finished page: failure layout mode=' + IntToStr(InstallMode) + ' heading=[' + FailHeading(InstallMode) +
        '] still_there=' + IntToStr(Ord(StillThereText(InstallMode) <> '')) + ' memo_h=' + IntToStr(FinMemo.Height) +
        ' fail_chars=' + IntToStr(Length(FailText)) + ' stage=[' + FailStage + ']');
    end;
  end;
end;

function OpenAdminOffered: Boolean;
begin
  Result := InstallOk;
end;

{ ---------------------------------------------------------------------------------------------
  Next button: each page runs its step (section 4.2)
  --------------------------------------------------------------------------------------------- }

function CheckPreflight: Boolean;
begin
  Result := RunHelperBusy('Checking this PC', 'check', '--phase preflight');
  if not Result then
  begin
    ShowFailuresWithHelp('Cognita cannot be installed on this PC yet.');
    Exit;
  end;
  WslState := Lowercase(ResultValue(LastResult, 'wsl'));
  Log('preflight: wsl=' + WslState);
  { Design 22.2 and 22.9: what the helper saw of NVIDIA and of .wslconfig. Not check results: no warning, no failure. }
  NvState := Lowercase(Trim(ResultValue(LastResult, 'nvidia')));
  NvName := ResultValue(LastResult, 'nvidia_name');
  NvDriver := ResultValue(LastResult, 'nvidia_driver');
  WslReclaim := Lowercase(Trim(ResultValue(LastResult, 'wsl_reclaim')));
  { The Acceleration page's default, and the pick: set ONCE (a second preflight, after turning WSL on or after
    Back, keeps what the user picked), unless the card or build no longer offers NVIDIA. }
  AccelDefaultPick := AccelDefault(AccelMode, InstallMode, StAcceleration);
  if not AccelInited then
  begin
    ChosenAccel := AccelDefaultPick;
    AccelInited := True;
  end
  else if (ChosenAccel = 'nvidia') and (AccelMode <> AccelOffer) then
    ChosenAccel := 'cpu';
  Log('preflight: nvidia=' + NvState + ' name=[' + NvName + '] driver=[' + NvDriver + '] wsl_reclaim=' + WslReclaim +
    ' | acceleration: mode=' + IntToStr(InstallMode) + ' page_mode=' + IntToStr(AccelMode) + ' build_bytes=' +
    IntToStr(NvidiaBuildBytes) + ' installed_profile=[' + StAcceleration + '] default=[' + AccelDefaultPick +
    '] chosen=[' + ChosenAccel + '] page_shown=' + IntToStr(Ord(AccelMode <> AccelHidden)));
  if (WarnText <> '') and (not PreflightWarned) then
  begin
    PreflightWarned := True;
    MsgBox('Please note:' + #13#10#13#10 + WarnText, mbInformation, MB_OK);
  end;
end;

{ The WSL page: turn WSL on, then ask about the restart (section 5.4, section 4.3) }
function WslPageNext: Boolean;
begin
  Result := False;
  if WslPageStage = 0 then
  begin
    Log('wsl page: running wsl-install');
    if RunHelperBusy('Turning on WSL', 'wsl-install', '') then
    begin
      { Turned on and no restart needed: check again and carry on. }
      if CheckPreflight then
      begin
        if (WslState = 'missing') or (WslState = 'old') then
          MsgBox('WSL is still not available. Run Setup again after restarting Windows.', mbError, MB_OK)
        else
          Result := True;
      end;
      Exit;
    end;
    if LastStatus = 'restart-required' then
    begin
      WslPageStage := 1;
      { Design 19.1 item 1: the helper's restart never force-closes programs (no /f), so a program with
        unsaved work would hold the restart up; the user is told to save first. }
      WslLabel.Caption := 'WSL is turned on, but Windows must restart before Cognita can use it. ' +
        'Save your work in other programs first. Setup opens again ' +
        'by itself after you sign back in, and continues from there.';
      WslRestartNow.Visible := True;
      WslRestartLater.Visible := True;
      WizardForm.NextButton.Caption := 'Continue';
      WizardForm.BackButton.Visible := False;
      Log('wsl page: restart required; asking Restart now or Later');
      Exit;
    end;
    ShowFailuresWithHelp('Setup could not turn on WSL.');
    Exit;
  end;
  { Stage 1: Restart now or Later. Both write the RunOnce value and the resume record through
    the helper; Setup then ends without the "Exit Setup?" question (proven in spike S1). }
  if WslRestartNow.Checked then
  begin
    Log('wsl page: restart now chosen; setup pid=' + IntToStr(Integer(GetCurrentProcessId)));
    { Design 19.11 R1: the helper starts a detached waiter that restarts Windows once THIS process has
      exited (a running Setup would answer Windows' "may we shut down?" with No). Setup closes right below. }
    if not RunHelperBusy('Restarting Windows', 'restart-for-wsl', '--setup-exe ' + QuoteArg(ExpandConstant('{srcexe}')) +
      ' --now --after-pid ' + IntToStr(Integer(GetCurrentProcessId))) then
    begin
      ShowFailuresWithHelp('Setup could not restart Windows.');
      Exit;
    end;
  end
  else
  begin
    Log('wsl page: later chosen');
    if not RunHelperBusy('Saving your place', 'restart-for-wsl', '--setup-exe ' + QuoteArg(ExpandConstant('{srcexe}'))) then
    begin
      ShowFailuresWithHelp('Setup could not save where it stopped.');
      Exit;
    end;
    MsgBox('Setup will continue after your next restart.', mbInformation, MB_OK);
  end;
  CloseQuietly := True;
  WizardForm.Close;
end;

function FolderPageNext: Boolean;
var
  Path, Reason: String;
begin
  Result := False;
  FolderReason.Caption := '';
  if FolderLocked then
  begin
    { Every non-fresh mode keeps the folder Cognita already uses; the helper checks it again on install. }
    Log('folder page: locked to ' + StRoot1 + ' (mode ' + IntToStr(InstallMode) + '); not validated again');
    Result := True;
    Exit;
  end;
  Path := Trim(PageFolder.Values[0]);
  if Path = '' then
  begin
    MsgBox('Choose the folder that holds your documents.', mbError, MB_OK);
    Exit;
  end;
  if HasBadArgChar(Path) then
  begin
    MsgBox('That folder name cannot contain a double quote.', mbError, MB_OK);
    Exit;
  end;
  Path := RemoveBackslash(Path);
  PageFolder.Values[0] := Path;
  { An invalid folder is a normal answer, result=ok;ok=0;reason=<text> (design 18.5); `failed` is only
    an internal error. }
  if not RunHelperBusy('Checking the folder', 'roots', '--validate ' + QuoteArg(Path)) then
  begin
    Log('folder page: roots --validate failed internally for ' + Path);
    { Design 19 (18.5 extended): an internal error here offers Save diagnostics like the other failures. }
    ShowFailuresWithHelp('Setup could not check that folder.');
    Exit;
  end;
  if ResultValue(LastResult, 'ok') = '0' then
  begin
    Reason := ResultValue(LastResult, 'reason');
    if Reason = '' then
      Reason := 'The folder cannot be used.';
    Log('folder page: ' + Path + ' refused: ' + Reason);
    FolderReason.Caption := 'Cognita cannot use that folder: ' + Reason;
    MsgBox('Cognita cannot use that folder.' + #13#10#13#10 + Reason, mbError, MB_OK);
    Exit;
  end;
  Log('folder page: ' + Path + ' accepted');
  Result := True;
end;

function AdminPageNext: Boolean;
var
  Pw, Pw2: String;
begin
  Result := False;
  Pw := PageAdmin.Values[1];
  Pw2 := PageAdmin.Values[2];
  if UserLocked then
  begin
    { Design 19.2 item 14: the box is read-only, and its "(unchanged)" text is not a name. AdminUser is
      only what the helper recorded ('' when it never did), so Ready and Finished show a name only when
      it is known; --admin-user is not passed in these modes (ArgsInstall). }
    AdminUser := StAdminUser;
    Log('admin page: user locked; recorded user=[' + AdminUser + ']');
  end
  else
  begin
    AdminUser := Trim(PageAdmin.Values[0]);
    if AdminUser = '' then
    begin
      MsgBox('Type a user name.', mbError, MB_OK);
      Exit;
    end;
    if HasBadArgChar(AdminUser) then
    begin
      MsgBox('The user name cannot contain a double quote or a control character.', mbError, MB_OK);
      Exit;
    end;
  end;
  if Pw = '' then
  begin
    MsgBox('Type a password.', mbError, MB_OK);
    Exit;
  end;
  if (not RunsOverInstalled) and (Pw <> Pw2) then
  begin
    MsgBox('The two passwords are not the same.', mbError, MB_OK);
    Exit;
  end;
  AdminPassword := Pw;
  Log('admin page: user=[' + AdminUser + '] locked=' + IntToStr(Ord(UserLocked)) + ' password accepted (the password itself is never logged)');
  Result := True;
end;

{ The Acceleration page's Next (design 22.7 and 22.12 item 1(b)): records the pick, and whether it differs from
  the default the page opened with (AccelTouched). A GPU radio that is not enabled can never be the pick. }
function AccelPageNext: Boolean;
begin
  if AccelUseGpu.Enabled and AccelUseGpu.Checked then
    ChosenAccel := 'nvidia'
  else
    ChosenAccel := 'cpu';
  AccelTouched := ChosenAccel <> AccelDefaultPick;
  Log('accel page: mode=' + IntToStr(AccelMode) + ' chosen=' + ChosenAccel + ' default=' + AccelDefaultPick +
    ' touched=' + IntToStr(Ord(AccelTouched)) + ' installed_profile=[' + StAcceleration + '] argument=[' +
    Trim(AccelArg(AccelMode, InstallMode, ChosenAccel, AccelTouched, StAcceleration)) + ']');
  Result := True;
end;

{ The final check. The helper's fixes say "choose other ports under Advanced" and "choose another data
  location under Advanced", but Advanced cannot change a value the mode locks (an update locks the
  ports; every non-fresh mode locks the data location), so the text is rewritten for those cases
  (design 19.4 item 18; RewriteAdvancedHints, tested in the harness). The failure dialog is the one with
  Save diagnostics (design 18.5, extended by 19 to this check). }
function ReadyPageNext: Boolean;
var
  Drive, Fixed, Heading: String;
begin
  { Design 22.9: the check box state is logged when Install is pressed, with what the other new choices were. }
  LogAccelDecision('ready');
  Result := RunHelperBusy('Checking ports and disk space', 'check', ArgsCheckFinal);
  if not Result then
  begin
    Drive := Copy(ChosenDataDir, 1, 2);
    Fixed := RewriteAdvancedHints(FailText, InstallMode, DataLocationLocked, Drive);
    if Fixed <> FailText then
      Log('ready check: rewrote the "under Advanced" fixes for mode ' + IntToStr(InstallMode) +
        ' (data_locked=' + IntToStr(Ord(DataLocationLocked)) + ' drive=' + Drive + ')');
    FailText := Fixed;
    if InstallMode = ModeUpdate then
      Heading := 'Setup cannot update with what this PC has right now. Fix the problem and try again.'
    else
      Heading := 'Setup cannot install with these choices. Change them under Advanced, or fix the problem, and try again.';
    ShowFailuresWithHelp(Heading);
  end;
end;

function NextButtonClick(CurPageID: Integer): Boolean;
begin
  Result := True;
  if CurPageID = wpWelcome then
    Result := CheckPreflight
  else if CurPageID = PageWsl.ID then
    Result := WslPageNext
  else if CurPageID = PageFolder.ID then
    Result := FolderPageNext
  else if CurPageID = PageAdmin.ID then
    Result := AdminPageNext
  else if CurPageID = PageAccel.ID then
    Result := AccelPageNext
  else if CurPageID = PageReady.ID then
    Result := ReadyPageNext
  else if CurPageID = PageRemote.ID then
  begin
    if RemoteStage <> 0 then
      Log('remote access: carrying on to the Finished page; stage=' + IntToStr(RemoteStage))
    else if RemoteSetUp.Checked then
    begin
      RunRemoteAccess;
      { A failure with a link keeps the user here to open it and press Retry (design 18.4). }
      if RemoteStage = 1 then
        Result := False;
    end
    else
      Log('remote access: skipped by the user');
  end;
end;

procedure CancelButtonClick(CurPageID: Integer; var Cancel, Confirm: Boolean);
begin
  if CloseQuietly then
  begin
    Confirm := False;
    Log('closing Setup quietly from a page (restart flow)');
  end;
end;

{ ---------------------------------------------------------------------------------------------
  The Installing page (section 4.2 step 6, sections 7.1 to 7.3)
  --------------------------------------------------------------------------------------------- }

procedure ExtractPayload(const TempName: String; const Title: String);
begin
  ProgressPage.SetText(Title, 'Unpacking. This takes a moment.');
  ProgressPage.SetProgress(0, 0);
  Log('payload: extracting ' + TempName + ' to {tmp}');
  ExtractTemporaryFile(TempName);
end;

procedure RunInstallFlow;
var
  PipeB, ImageFile, SrcFile, Verb, Args: String;
  Status: String;
begin
  InstallAttempted := True;
  InstallOk := False;
  InstallRestart := False;
  InstallResumeSaved := False;
  ImageFile := '';
  SrcFile := '';
  FailCount := 0;
  FailText := '';
  FailStage := 'Starting';
  InstallWarnText := '';
  InstallSignInWarned := False;
  ResultWorkspace := '';
  ResultAccel := '';
  { Design 21.4: a new run starts with the button unpressed and enabled, and no request flag left over
    from an earlier run (the helper removes a stale one too). }
  SkipPressed := False;
  FinProofSkipped := False;
  ProgressSkipButton.Enabled := True;
  DeleteSkipRequest('start of the install run');
  { Design 19.4 item 23: the page's caption names the run ("Updating Cognita", "Repairing Cognita" ...),
    not always "Installing". The text under it says the same; the remote-access step sets its own. }
  ProgressPage.Caption := ProgressCaption(InstallMode);
  ProgressPage.Description := 'Setup is ' + Lowercase(Copy(ProgressCaption(InstallMode), 1, 1)) +
    Copy(ProgressCaption(InstallMode), 2, 100) + '. This can take several minutes; keep this window open.';
  Log('install flow: progress caption=[' + ProgressPage.Caption + ']');
  ProgressPage.SetText('Preparing', '');
  ProgressLinkEdit.Visible := False;
  ProgressCopyButton.Visible := False;
  ProgressWaitLabel.Visible := False;
  ProgressSkipButton.Visible := False;
  ProgressPage.Show;
  { The Installing page: a Back button here lets the user walk away from a running install (P1, design 18.5). }
  WizardForm.BackButton.Visible := False;
  RunMode := 2;
  RunStartTick := GetTickCount;
  try
    try
      { The helper now runs from the app folder, where Inno has just installed it. }
      HelperFolder := ExpandConstant('{app}');
      if InstallMode = ModeUpdate then
      begin
        { Update: the source tree only; the helper swaps it in and runs cognita update (section 7.3). }
        ExtractPayload('cognita-src.tar.gz', 'Preparing the update');
        SrcFile := ExpandConstant('{tmp}\cognita-src.tar.gz');
        Verb := 'update';
      end
      else
      begin
        { Every install run (fresh, finish, repair, reinstall after an uninstall) carries the source
          tarball, so the helper can swap an older tree in the distro before cognita install. Fresh
          and finish also unpack the image: the helper may need to import it again (section 5.7 step
          4) even when an unfinished distro exists. Repair and reinstall never need the image: the
          distro is there. }
        Verb := 'install';
        if (InstallMode = ModeFresh) or (InstallMode = ModeFinish) then
        begin
          ExtractPayload('cognita-wsl.tar.gz', 'Preparing the Linux image');
          ImageFile := ExpandConstant('{tmp}\cognita-wsl.tar.gz');
        end
        else
          Log('install: mode ' + IntToStr(InstallMode) + ' runs over the existing Cognita distro; the Linux image is not needed');
        ExtractPayload('cognita-src.tar.gz', 'Preparing the Cognita files');
        SrcFile := ExpandConstant('{tmp}\cognita-src.tar.gz');
      end;
      Log('install flow: mode=' + IntToStr(InstallMode) + ' verb=' + Verb + ' image=' + IntToStr(Ord(ImageFile <> '')) +
        ' src=' + IntToStr(Ord(SrcFile <> '')) + ' admin_user_passed=' + IntToStr(Ord((Verb = 'install') and (not RunsOverInstalled))));
      FailStage := 'Handing the Admin password to the installer';
      if not HandOverPassword(PipeB) then
      begin
        FailCount := 1;
        { Design 19.5 item 15: the broker gets 60 seconds to open its pipe; if it never did, the helper
          itself did not start (a blocked or very slow PowerShell), which is what the text says. }
        FailMessage := 'Setup could not hand the Admin password to its helper: the Setup helper did not start in time.';
        FailFix := 'Run Setup again. If it keeps happening, choose Save diagnostics and attach the file to a new issue at {#SupportUrl}';
        FailText := FailMessage + #13#10 + 'What to do: ' + FailFix + #13#10#13#10;
        Exit;
      end;
      { Design 22.8: the acceleration and WSL-memory decisions, with the values they came from, right before
        the arguments are built. (The helper start line below logs the arguments themselves.) }
      LogAccelDecision(Verb);
      if Verb = 'update' then
        Args := ArgsUpdate(PipeB, SrcFile)
      else
        Args := ArgsInstall(PipeB, ImageFile, SrcFile);
      ProgressPage.SetText(ProgressCaption(InstallMode), '');
      RunStartTick := GetTickCount;
      RunHelper(Verb, Args);
      { Design 19.4 item 2: remote access runs its own helper verb next, and every RunHelper resets
        WarnText, so the warnings of THIS run are kept for the Finished page right here. }
      InstallWarnText := WarnText;
      InstallSignInWarned := SignInWarned;
      Log('install flow: kept ' + IntToStr(Length(InstallWarnText)) + ' characters of warnings for the Finished page; sign_in_warned=' +
        IntToStr(Ord(InstallSignInWarned)));
      Status := LastStatus;
      if Status = 'ok' then
      begin
        InstallOk := True;
        ResultVersion := ResultValue(LastResult, 'version');
        PublicUrl := ResultValue(LastResult, 'public_url');
        { Design 19.4 item 2: the Finished page shows the workspace value the result (`status`) reported;
          the Ready choice (ChosenWorkspaceOn) is left as it was. }
        ResultWorkspace := Lowercase(ResultValue(LastResult, 'workspace'));
        Log('install flow: result workspace=[' + ResultWorkspace + '] chosen=' + OnOff(ChosenWorkspaceOn));
        { Design 22.1, 22.7 and 22.12 item 11: the Finished page's Acceleration line comes only from the result
          (`status --json`'s acceleration), never from the choice. An unknown word counts as "did not say". }
        ResultAccel := AccelKnown(ResultValue(LastResult, 'acceleration'));
        Log('install flow: result acceleration=[' + ResultAccel + '] (reported [' + ResultValue(LastResult, 'acceleration') +
          ']) requested=[' + Trim(AccelArg(AccelMode, InstallMode, ChosenAccel, AccelTouched, StAcceleration)) + ']');
        { Design 21.4: the self-tests were skipped; the Finished page says so (and how to run them). }
        FinProofSkipped := ResultValue(LastResult, 'proof') = 'skipped';
        Log('install flow: result proof=[' + ResultValue(LastResult, 'proof') + '] skip_pressed=' + IntToStr(Ord(SkipPressed)) +
          ' finished_page_note=' + IntToStr(Ord(FinProofSkipped)));
      end
      else if Status = 'restart-required' then
        InstallRestart := True;
      Log('install flow: verb=' + Verb + ' status=' + Status + ' version=' + ResultVersion + ' public_url_set=' +
        IntToStr(Ord(PublicUrl <> '')));
    except
      Log('install flow: exception: ' + GetExceptionMessage);
      FailCount := 1;
      FailMessage := 'Setup stopped unexpectedly: ' + GetExceptionMessage;
      FailFix := 'Choose Save diagnostics. To get help, attach the file to a new issue at {#SupportUrl}';
      FailText := FailMessage + #13#10 + 'What to do: ' + FailFix + #13#10#13#10;
    end;
  finally
    RunMode := 0;
    ProgressSkipButton.Visible := False;
    ProgressPage.Hide;
    { Design 21.4: the request flag belongs to this run; it does not outlive it. }
    DeleteSkipRequest('end of the install run');
    { The big files are removed as soon as a step is done with them (section 4.1). }
    DeleteFile(ExpandConstant('{tmp}\cognita-wsl.tar.gz'));
    DeleteFile(ExpandConstant('{tmp}\cognita-src.tar.gz'));
  end;
  if InstallRestart then
  begin
    { Design 19.11 R1: Setup NEVER restarts Windows from inside itself here (a running Setup answers
      Windows' "may we shut down?" with No, and the restart stalls on "Cognita Setup is preventing
      restart"). The helper only records RunOnce and the resume marker (no --now); NeedRestart below
      returns True, so Inno's own Finished page offers "Restart now / later" and Inno restarts after
      Setup has exited. The Finished page (CurPageChanged) says to save work first. }
    Log('install flow: the helper needs a restart (exit 3010); recording where Setup stopped, no restart from here');
    InstallResumeSaved := RunHelperBusy('Saving your place', 'restart-for-wsl', '--setup-exe ' + QuoteArg(ExpandConstant('{srcexe}')));
    Log('install flow: resume recorded=' + IntToStr(Ord(InstallResumeSaved)) + '; NeedRestart will return True');
    { Design 19.11 R3: a failed record step is not silent. }
    if not InstallResumeSaved then
      ShowFailuresWithHelp('Setup could not save where it stopped.');
  end
  else if not InstallOk then
    { The failure Finished page names this file (and its button shows it). }
    FinDiagZip := AutoSaveDiagnostics('the install flow failed');
end;

{ Inno calls this after the install steps (ssPostInstall included) finish. True only for the install-flow
  case that needs a restart (design 19.11 R1); Inno's Finished page then offers Restart now / later. }
function NeedRestart: Boolean;
begin
  Result := InstallRestart;
  Log('NeedRestart: ' + IntToStr(Ord(Result)) + ' (resume recorded=' + IntToStr(Ord(InstallResumeSaved)) + ')');
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssPostInstall then
    RunInstallFlow;
end;

{ ---------------------------------------------------------------------------------------------
  Uninstall (sections 4.5 and 7.5)
  --------------------------------------------------------------------------------------------- }

function AskUninstallChoice(var DeleteData: Boolean): Boolean;
var
  Form: TSetupForm;
  Head, KeepNote, DeleteNote: TNewStaticText;
  RadioPanel: TPanel;
  KeepRadio, DeleteRadio: TNewRadioButton;
  OkButton, CancelButton: TNewButton;
  Size: String;
begin
  Result := False;
  DeleteData := False;
  Form := CreateCustomForm(ScaleX(460), ScaleY(230), False, False);
  try
    Form.Caption := 'Remove Cognita';
    Head := TNewStaticText.Create(Form);
    Head.Parent := Form;
    Head.Left := ScaleX(14);
    Head.Top := ScaleY(14);
    Head.Width := ScaleX(432);
    Head.AutoSize := False;
    Head.WordWrap := True;
    Head.Height := ScaleY(36);
    Head.Caption := 'Remove Cognita. Your documents are never touched.';
    { Design 19.11 R2: the two radio buttons (and their notes) sit in their OWN borderless panel. Directly
      on the form they were siblings of Continue and Cancel, so the arrow keys walked from a radio button
      to the buttons (and selected "Also delete" through the OnEnter handler). In a panel the arrows move
      only between the two radios. The panel is not a tab stop; Tab reaches the checked radio. It spans
      from the old radio top (58) to the bottom of the delete note (188), above the buttons. }
    RadioPanel := TPanel.Create(Form);
    RadioPanel.Parent := Form;
    RadioPanel.Left := ScaleX(14);
    RadioPanel.Top := ScaleY(58);
    RadioPanel.Width := ScaleX(432);
    RadioPanel.Height := ScaleY(130);
    RadioPanel.BevelOuter := bvNone;
    RadioPanel.Caption := '';
    KeepRadio := TNewRadioButton.Create(Form);
    KeepRadio.Parent := RadioPanel;
    KeepRadio.Left := 0;
    KeepRadio.Top := 0;
    KeepRadio.Width := ScaleX(432);
    KeepRadio.Caption := 'Keep Cognita''s data (recommended)';
    KeepRadio.Checked := True;
    KeepRadio.OnEnter := @RadioEnter;
    KeepNote := TNewStaticText.Create(Form);
    KeepNote.Parent := RadioPanel;
    KeepNote.Left := ScaleX(20);
    KeepNote.Top := ScaleY(22);
    KeepNote.Width := ScaleX(412);
    KeepNote.AutoSize := False;
    KeepNote.WordWrap := True;
    KeepNote.Height := ScaleY(32);
    KeepNote.Caption := 'Installing Cognita again reuses your index, settings and credentials.';
    DeleteRadio := TNewRadioButton.Create(Form);
    DeleteRadio.Parent := RadioPanel;
    DeleteRadio.Left := 0;
    DeleteRadio.Top := ScaleY(60);
    DeleteRadio.Width := ScaleX(432);
    Size := '';
    if StVhdBytes <> '' then
      Size := FormatBytes(StrToInt64Def(StVhdBytes, 0)) + ' ';
    { A radio button's caption does not wrap, so the size and the path go in the note below it,
      which does; with them in the caption the path ran off the dialog (P2, 2026-09-29). }
    DeleteRadio.Caption := 'Also delete Cognita''s data: index, settings, credentials, Workspace';
    DeleteRadio.OnEnter := @RadioEnter;
    DeleteNote := TNewStaticText.Create(Form);
    DeleteNote.Parent := RadioPanel;
    DeleteNote.Left := ScaleX(20);
    DeleteNote.Top := ScaleY(82);
    DeleteNote.Width := ScaleX(412);
    DeleteNote.AutoSize := False;
    DeleteNote.WordWrap := True;
    DeleteNote.Height := ScaleY(48);
    if StVhdDir <> '' then
    begin
      if Size <> '' then
        DeleteNote.Caption := 'That is ' + Size + 'in ' + StVhdDir + '. '
      else
        DeleteNote.Caption := 'It is in ' + StVhdDir + '. ';
      DeleteNote.Caption := DeleteNote.Caption + 'This cannot be undone. Setup asks you to confirm.';
    end
    else
      DeleteNote.Caption := 'This cannot be undone. Setup asks you to confirm.';
    OkButton := TNewButton.Create(Form);
    OkButton.Parent := Form;
    OkButton.Caption := 'Continue';
    OkButton.Left := Form.ClientWidth - ScaleX(80 + 6 + 80 + 14);
    OkButton.Top := Form.ClientHeight - ScaleY(25 + 14);
    OkButton.Width := ScaleX(80);
    OkButton.Height := ScaleY(25);
    OkButton.ModalResult := mrOk;
    OkButton.Default := True;
    CancelButton := TNewButton.Create(Form);
    CancelButton.Parent := Form;
    CancelButton.Caption := 'Cancel';
    CancelButton.Left := Form.ClientWidth - ScaleX(80 + 14);
    CancelButton.Top := Form.ClientHeight - ScaleY(25 + 14);
    CancelButton.Width := ScaleX(80);
    CancelButton.Height := ScaleY(25);
    CancelButton.ModalResult := mrCancel;
    CancelButton.Cancel := True;
    { Design 19.5 item 12: Enter means Continue. (The arrow keys move and select between the two radio
      buttons through their OnEnter handlers; Tab reaches them from here.) }
    Form.ActiveControl := OkButton;
    Form.FlipAndCenterIfNeeded(True, nil, False);
    if Form.ShowModal = mrOk then
    begin
      Result := True;
      DeleteData := DeleteRadio.Checked;
    end;
    Log('uninstall: first form closed ok=' + IntToStr(Ord(Result)) + ' delete_data=' + IntToStr(Ord(DeleteData)) +
      ' (radios in their own panel, design 19.11 R2)');
  finally
    Form.Free;
  end;
end;

{ The second form for "delete": lists exactly what goes and needs the word DELETE typed. }
var
  DeleteConfirmEdit: TNewEdit;
  DeleteConfirmOk: TNewButton;

procedure DeleteConfirmChange(Sender: TObject);
begin
  DeleteConfirmOk.Enabled := DeleteConfirmEdit.Text = 'DELETE';
end;

function ConfirmDelete: Boolean;
var
  Form: TSetupForm;
  Head: TNewStaticText;
  CancelButton: TNewButton;
  Where: String;
begin
  Result := False;
  Form := CreateCustomForm(ScaleX(480), ScaleY(300), False, False);
  try
    Form.Caption := 'Delete Cognita''s data';
    Where := StVhdDir;
    if Where = '' then
      Where := ExpandConstant('{localappdata}\Cognita\wsl');
    Head := TNewStaticText.Create(Form);
    Head.Parent := Form;
    Head.Left := ScaleX(14);
    Head.Top := ScaleY(14);
    Head.Width := ScaleX(452);
    Head.AutoSize := False;
    Head.WordWrap := True;
    Head.Height := ScaleY(190);
    Head.Caption := 'This permanently deletes:' + #13#10#13#10 +
      '  - the Cognita Linux system and everything in it: the search index, settings, connector keys, ' +
      'credentials and Workspace files (at ' + Where + ')' + #13#10 +
      '  - Cognita''s Windows settings and logs (under %LOCALAPPDATA%\Cognita)' + #13#10#13#10 +
      'Your documents are never touched. Setup copies the logs to a folder in %TEMP% first.' + #13#10#13#10 +
      'To go ahead, type the word DELETE below.';
    DeleteConfirmEdit := TNewEdit.Create(Form);
    DeleteConfirmEdit.Parent := Form;
    DeleteConfirmEdit.Left := ScaleX(14);
    DeleteConfirmEdit.Top := ScaleY(212);
    DeleteConfirmEdit.Width := ScaleX(200);
    DeleteConfirmEdit.OnChange := @DeleteConfirmChange;
    DeleteConfirmOk := TNewButton.Create(Form);
    DeleteConfirmOk.Parent := Form;
    DeleteConfirmOk.Caption := 'Delete';
    DeleteConfirmOk.Left := Form.ClientWidth - ScaleX(80 + 6 + 80 + 14);
    DeleteConfirmOk.Top := Form.ClientHeight - ScaleY(25 + 14);
    DeleteConfirmOk.Width := ScaleX(80);
    DeleteConfirmOk.Height := ScaleY(25);
    DeleteConfirmOk.ModalResult := mrOk;
    DeleteConfirmOk.Enabled := False;
    CancelButton := TNewButton.Create(Form);
    CancelButton.Parent := Form;
    CancelButton.Caption := 'Cancel';
    CancelButton.Left := Form.ClientWidth - ScaleX(80 + 14);
    CancelButton.Top := Form.ClientHeight - ScaleY(25 + 14);
    CancelButton.Width := ScaleX(80);
    CancelButton.Height := ScaleY(25);
    CancelButton.ModalResult := mrCancel;
    CancelButton.Cancel := True;
    { The one form whose focus is not its default button: Delete is disabled until the word is typed
      (and is deliberately not the default), so the edit box where the word goes holds the focus. }
    Form.ActiveControl := DeleteConfirmEdit;
    Form.FlipAndCenterIfNeeded(True, nil, False);
    Result := (Form.ShowModal = mrOk) and (DeleteConfirmEdit.Text = 'DELETE');
  finally
    Form.Free;
  end;
end;

function InitializeUninstall: Boolean;
var
  Wanted: Boolean;
begin
  Result := True;
  UninstallDeleteData := False;
  UninstallDeleteFailed := False;
  HelperFolder := ExpandConstant('{app}');
  RunMode := 0;
  if UninstallSilent then
  begin
    Log('uninstall: silent uninstall always keeps data');
    Exit;
  end;
  { The saved location and size for the wording; a failure here only loses the numbers. }
  if RunHelper('state', '') then
    ReadState
  else
    Log('uninstall: could not read state; the delete form will not show a size');
  if not AskUninstallChoice(Wanted) then
  begin
    Log('uninstall: cancelled at the first form');
    Result := False;
    Exit;
  end;
  if Wanted then
  begin
    if not ConfirmDelete then
    begin
      Log('uninstall: cancelled at the DELETE confirmation');
      Result := False;
      Exit;
    end;
    UninstallDeleteData := True;
  end;
  Log('uninstall: delete_data=' + IntToStr(Ord(UninstallDeleteData)));
end;

procedure OnUninstallLine(const S: String; const Error, FirstLine: Boolean);
var
  L: String;
begin
  L := Trim(S);
  if L = '' then
    Exit;
  OnHelperLine(S, Error, FirstLine);
  if (L[1] = '{') and (CurTitle <> '') then
  begin
    UninstallProgressForm.StatusLabel.Caption := CurTitle;
  end;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  Code: Integer;
  Flag, Text, FailReasonText: String;
begin
  if CurUninstallStep = usUninstall then
  begin
    { Before Inno deletes app\ (the helper lives there): stop Cognita, remove the task, and
      keep or delete the data (section 7.5). }
    if UninstallDeleteData then
      Flag := '--delete-data'
    else
      Flag := '--keep-data';
    ResetRun;
    HelperFolder := ExpandConstant('{app}');
    Log('uninstall: running the helper: uninstall ' + Flag);
    Code := -1;
    try
      ExecAndLogOutput(PsExe, '-NoProfile -NonInteractive -ExecutionPolicy Bypass -File ' + QuoteArg(HelperPath) +
        ' uninstall ' + Flag, '', SW_HIDE, ewWaitUntilTerminated, Code, @OnUninstallLine);
    except
      Log('uninstall: exception while running the helper: ' + GetExceptionMessage);
    end;
    HelperExit := Code;
    Log('uninstall: helper exit=' + IntToStr(Code) + ' last line: ' + LastResult);
    JudgeResult;
    { A delete-data run copies the helper logs to %TEMP% (it deletes the Cognita folder) and says where. }
    UninstLogsCopy := ResultValue(LastResult, 'logs_copy');
    if LastStatus <> 'ok' then
    begin
      EnsureFailureText;
      FailReasonText := ResultValue(LastResult, 'reason');
      { Design 19.11 R5: "its data could not be deleted" is true ONLY for the failed `wsl --unregister`
        (reason=unregister-failed). Any other failure of a delete-data run (the Linux uninstall, the task,
        a delete of a known item) says something else went wrong, so it gets the "part of the cleanup did
        not finish" box below with the failure text, like a keep-data run. }
      Log('uninstall: helper did not finish ok: status=' + LastStatus + ' reason=[' + FailReasonText + '] delete_data=' +
        IntToStr(Ord(UninstallDeleteData)));
      if UninstallDeleteData and (FailReasonText = 'unregister-failed') then
      begin
        { Design 19.4 item 9: a delete-data run that failed (`wsl --unregister` did not work) has already
          saved state=uninstalled, and Inno removes Cognita's own files regardless, so Cognita IS removed;
          only its data was not deleted. The closing text comes from the result: it is shown once, at the
          end (usPostUninstall), instead of this box, so the user gets one closing message. The helper's
          own failure lines are in the log. }
        UninstallDeleteFailed := True;
        Log('uninstall: the data could not be deleted (helper status=' + LastStatus + ' exit=' + IntToStr(HelperExit) +
          ' logs_copy=[' + UninstLogsCopy + ']); the closing text will say so. ' + FailMessage);
      end
      else
        SuppressibleMsgBox('Cognita''s own files are being removed, but part of the cleanup did not finish.' + #13#10#13#10 +
          FailText + 'Logs: ' + LogsDir, mbError, MB_OK, IDOK);
    end;
  end
  else if CurUninstallStep = usPostUninstall then
  begin
    RemoveBinFromPath;
    { The helper deletes %LOCALAPPDATA%\Cognita on a delete-data run, but it runs from app\ inside
      it, so that folder was still in use then; Inno has now removed app\, which leaves the parent
      empty (P2, 2026-09-29). RemoveDir only removes an EMPTY folder, so nothing else can go. }
    if UninstallDeleteData then
    begin
      if RemoveDir(ExpandConstant('{localappdata}\Cognita')) then
        Log('uninstall: removed the empty Cognita folder')
      else
        Log('uninstall: the Cognita folder was not removed (absent or not empty)');
    end;
    { Design 19.4 item 21: ONE closing box. Inno's own final message (UninstalledAll / UninstalledMost, set
      in [Messages] to "Cognita was removed. Your documents were not touched.") is the success text, so
      the box that used to say it here is gone. The one exception is a delete-data run whose data could
      not be deleted (item 9): that text is Setup's own, from the helper's result, and the user needs
      the log path in it. }
    if UninstallDeleteFailed then
    begin
      Text := UninstLogsCopy;
      if Text = '' then
        Text := LogsDir;
      Log('uninstall: closing text for a failed data delete; logs=' + Text);
      SuppressibleMsgBox('Cognita was removed, but its data could not be deleted. Logs: ' + Text,
        mbError, MB_OK, IDOK);
    end
    else
      Log('uninstall: closing text is Inno''s own (delete_data=' + IntToStr(Ord(UninstallDeleteData)) +
        ' data_dir=[' + StVhdDir + ']); no Setup box');
  end;
end;

{ The uninstaller's own log, kept the same way as Setup's (design 18.5). A keep-data uninstall leaves
  %LOCALAPPDATA%\Cognita\logs in place, so the log goes there. A delete-data uninstall has just
  removed that folder on purpose: the log goes beside the copy of the helper logs the helper made
  in %TEMP% (its logs_copy result), so the deletion leaves nothing behind in the Cognita folder. }
procedure DeinitializeUninstall;
var
  Dest: String;
begin
  if not UninstallDeleteData then
    Dest := LogsDir + '\uninstall-' + StampNow + '.log'
  else if UninstLogsCopy <> '' then
    Dest := UninstLogsCopy + '\uninstall-' + StampNow + '.log'
  else
    Dest := RemoveBackslash(GetEnv('TEMP')) + '\Cognita-uninstall-' + StampNow + '\uninstall.log';
  CopyOwnLog(Dest);
end;
