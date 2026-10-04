; Test harness for windows\setup\pure.iss (design 19.10). It is NOT Cognita Setup: it includes only the
; PURE block, runs a list of cases when it starts, writes one PASS or FAIL line per case plus a final
; TOTAL line to the file named by /RESULT=<path> on its command line, and exits without installing.
; tests\test_setup_pure.py compiles it with ISCC into a temp folder and runs it with
; /VERYSILENT /SUPPRESSMSGBOXES /RESULT=<file>, then reads the file.
;
; Nothing here touches the machine: no helper, no registry, no wizard. The harness never runs the real
; Setup.

[Setup]
AppName=Cognita Pure Tests
AppVersion=0
DefaultDirName={tmp}\CognitaPureTests
CreateAppDir=no
Uninstallable=no
DisableProgramGroupPage=yes
DisableWelcomePage=yes
DisableDirPage=yes
DisableReadyPage=yes
DisableFinishedPage=yes
PrivilegesRequired=lowest
OutputBaseFilename=pure_tests

[Code]
#include "..\pure.iss"

var
  CaseCount: Integer;
  FailCount: Integer;
  Report: String;

procedure Note(const Line: String);
begin
  Report := Report + Line + #13#10;
end;

procedure CheckStr(const Name, Got, Want: String);
begin
  CaseCount := CaseCount + 1;
  if Got = Want then
    Note('PASS ' + Name)
  else
  begin
    FailCount := FailCount + 1;
    Note('FAIL ' + Name + ': got [' + Got + '] want [' + Want + ']');
  end;
end;

procedure CheckInt(const Name: String; const Got, Want: Integer);
begin
  CheckStr(Name, IntToStr(Got), IntToStr(Want));
end;

procedure CheckBool(const Name: String; const Got, Want: Boolean);
begin
  CheckStr(Name, IntToStr(Ord(Got)), IntToStr(Ord(Want)));
end;

{ ---- PickMode and ModeFromState (design 18.2, 19.2) ---- }
procedure CasesPickMode;
var
  Line: String;
begin
  CheckInt('PickMode fresh: no distro', PickMode(False, False, 'none', '', '14.2.2'), ModeFresh);
  CheckInt('PickMode fresh: stale record, no distro', PickMode(False, True, 'installed', '14.2.0', '14.2.2'), ModeFresh);
  CheckInt('PickMode finish: distro, install never completed', PickMode(True, False, 'installing', '', '14.2.2'), ModeFinish);
  CheckInt('PickMode finish: import-pending', PickMode(True, False, 'import-pending', '', '14.2.2'), ModeFinish);
  CheckInt('PickMode repair: same version', PickMode(True, True, 'installed', '14.2.2', '14.2.2'), ModeRepair);
  CheckInt('PickMode update: older installed', PickMode(True, True, 'installed', '14.2.0', '14.2.2'), ModeUpdate);
  CheckInt('PickMode update: version unknown', PickMode(True, True, 'installed', '', '14.2.2'), ModeUpdate);
  CheckInt('PickMode reinstall: keep-data uninstall, same version', PickMode(True, True, 'uninstalled', '14.2.2', '14.2.2'), ModeReinstall);
  CheckInt('PickMode reinstall: keep-data uninstall, other version', PickMode(True, True, 'Uninstalled', '14.1.0', '14.2.2'), ModeReinstall);
  CheckInt('PickMode downgrade is still an update (InitializeSetup refuses it)', PickMode(True, True, 'installed', '14.3.0', '14.2.2'), ModeUpdate);
  CheckBool('SetupIsOlder: downgrade refused', SetupIsOlder('14.3.0', '14.2.2'), True);

  Line := 'result=ok;installed=1;state=installed;distro=present;owned=1;linux_version=14.2.2;admin_user=doug';
  CheckInt('ModeFromState repair', ModeFromState(Line, '14.2.2'), ModeRepair);
  CheckInt('ModeFromState update', ModeFromState(Line, '14.3.0'), ModeUpdate);
  CheckBool('StateDistroOwned owned', StateDistroOwned(Line), True);
  Line := 'result=ok;installed=0;state=none;distro=absent;owned=0';
  CheckInt('ModeFromState fresh', ModeFromState(Line, '14.2.2'), ModeFresh);
  Line := 'result=ok;installed=0;state=installing;distro=present;owned=1';
  CheckInt('ModeFromState finish', ModeFromState(Line, '14.2.2'), ModeFinish);
  Line := 'result=ok;installed=1;state=uninstalled;distro=present;owned=1;linux_version=14.2.2';
  CheckInt('ModeFromState reinstall', ModeFromState(Line, '14.2.2'), ModeReinstall);
  Line := 'result=ok;installed=0;state=none;distro=present;owned=0';
  CheckBool('StateDistroOwned foreign distro is not ours', StateDistroOwned(Line), False);
  CheckInt('ModeFromState foreign distro is fresh', ModeFromState(Line, '14.2.2'), ModeFresh);
  Line := 'result=ok;installed=1;state=installed;distro=present;owned=0;linux_version=14.2.2';
  CheckInt('ModeFromState foreign distro never repairs', ModeFromState(Line, '14.2.2'), ModeFresh);
end;

{ ---- ResultValue and DecodeResultValue (design 18.5) ---- }
procedure CasesResultValue;
begin
  CheckStr('Decode %3B', DecodeResultValue('a%3Bb'), 'a;b');
  CheckStr('Decode %3b lower case', DecodeResultValue('a%3bb'), 'a;b');
  CheckStr('Decode %25', DecodeResultValue('100%25'), '100%');
  CheckStr('Decode %253B is the text %3B', DecodeResultValue('%253B'), '%3B');
  CheckStr('Decode trailing %', DecodeResultValue('abc%'), 'abc%');
  CheckStr('Decode a lone %', DecodeResultValue('%'), '%');
  CheckStr('Decode a % with one digit after it', DecodeResultValue('x%2'), 'x%2');
  CheckStr('Decode leaves other escapes alone', DecodeResultValue('a%20b'), 'a%20b');
  CheckStr('Decode empty', DecodeResultValue(''), '');
  CheckStr('ResultValue status', ResultValue('result=ok;path=C:\a', 'result'), 'ok');
  CheckStr('ResultValue decodes ;', ResultValue('result=ok;path=C:\a%3Bb;x=1', 'path'), 'C:\a;b');
  CheckStr('ResultValue decodes %', ResultValue('result=ok;pct=50%25;x=1', 'pct'), '50%');
  CheckStr('ResultValue trailing % in the last value', ResultValue('result=failed;reason=a;k=%', 'k'), '%');
  CheckStr('ResultValue absent key', ResultValue('result=ok;a=1', 'b'), '');
  CheckStr('ResultValue key is not a suffix match (version vs linux_version)', ResultValue('result=ok;linux_version=14.2.2', 'version'), '');
  CheckStr('ResultValue last field', ResultValue('result=ok;a=1;b=2', 'b'), '2');
  CheckStr('ResultValue empty value', ResultValue('result=ok;a=;b=2', 'a'), '');
  CheckStr('ResultValue empty line', ResultValue('', 'result'), '');
  { Design 21.4: the proof= key the install and update results carry }
  CheckStr('ResultValue proof=skipped parse', ResultValue('result=ok;version=14.2.4;seconds=200;proof=skipped', 'proof'), 'skipped');
  CheckStr('ResultValue proof=passed parse', ResultValue('result=ok;from=14.2.3;to=14.2.4;proof=passed', 'proof'), 'passed');
  CheckStr('ResultValue proof= empty (unknown)', ResultValue('result=ok;version=14.2.4;proof=', 'proof'), '');
  CheckStr('ResultValue proof absent (an older helper)', ResultValue('result=ok;version=14.2.4', 'proof'), '');
end;

{ ---- The Skip self-tests button (design 21.4) ---- }
procedure CasesSkipProof;
begin
  CheckBool('ProofSkipVisible proof stage, not pressed', ProofSkipVisible('proof', False), True);
  CheckBool('ProofSkipVisible proof stage, pressed', ProofSkipVisible('proof', True), False);
  CheckBool('ProofSkipVisible other stage', ProofSkipVisible('images', False), False);
  CheckBool('ProofSkipVisible the sign-in stage (Copy button owns it)', ProofSkipVisible('remote.login', False), False);
  CheckBool('ProofSkipVisible no stage yet', ProofSkipVisible('', False), False);
  CheckBool('ProofSkipVisible other stage, pressed', ProofSkipVisible('start', True), False);
  CheckStr('SkippedProofNote', SkippedProofNote, 'The self-tests were skipped. To run them later, run this Setup again.');
end;

{ ---- JsonField ---- }
procedure CasesJsonField;
var
  L: String;
begin
  L := '{"state":"failed","message":"a \"quoted\" b\nline two","bytes_done":123,"stage":"x","ok":true}';
  CheckStr('JsonField string', JsonField(L, 'state'), 'failed');
  CheckStr('JsonField escaped quote and newline', JsonField(L, 'message'), 'a "quoted" b' + #13#10 + 'line two');
  CheckStr('JsonField number', JsonField(L, 'bytes_done'), '123');
  CheckStr('JsonField word', JsonField(L, 'ok'), 'true');
  CheckStr('JsonField absent', JsonField(L, 'nope'), '');
  CheckStr('JsonField unicode escape', JsonField('{"t":"A\u0042C"}', 't'), 'ABC');
  CheckStr('JsonField key inside a string value does not match', JsonField('{"message":"\"state\":\"x\"","state":"ok"}', 'state'), 'ok');
  CheckStr('JsonField spaces around the colon', JsonField('{ "a" : "b" , "c" : 7 }', 'c'), '7');
  CheckStr('JsonField backslash path', JsonField('{"p":"C:\\Users\\x"}', 'p'), 'C:\Users\x');
  CheckStr('JsonField no object', JsonField('plain text', 'a'), '');
end;

{ ---- RedactToken (CLAUDE.md: never log a connector token) ---- }
procedure CasesRedactToken;
begin
  CheckStr('RedactToken path', RedactToken('http://localhost:8675/mcp/abcDEF123/sse'), 'http://localhost:8675/mcp/<redacted>/sse');
  CheckStr('RedactToken at the end', RedactToken('url=http://h/mcp/secret'), 'url=http://h/mcp/<redacted>');
  CheckStr('RedactToken stops at a quote', RedactToken('{"u":"http://h/mcp/secret"}'), '{"u":"http://h/mcp/<redacted>"}');
  CheckStr('RedactToken two tokens', RedactToken('http://h/mcp/one and http://h/mcp/two'), 'http://h/mcp/<redacted> and http://h/mcp/<redacted>');
  CheckStr('RedactToken upper case path', RedactToken('http://h/MCP/secret/x'), 'http://h/MCP/<redacted>/x');
  CheckStr('RedactToken without a token is unchanged', RedactToken('nothing to hide at http://h/admin'), 'nothing to hide at http://h/admin');
  CheckStr('RedactToken empty segment is unchanged', RedactToken('http://h/mcp/'), 'http://h/mcp/');
  CheckStr('RedactToken empty', RedactToken(''), '');
end;

{ ---- PathRemovePart and PathHasPart (design 4.4) ---- }
procedure CasesPath;
begin
  CheckStr('PathRemovePart middle', PathRemovePart('C:\a;C:\Cognita\bin;C:\b', 'C:\Cognita\bin'), 'C:\a;C:\b');
  CheckStr('PathRemovePart case and trailing backslash', PathRemovePart('C:\a;c:\COGNITA\bin\;C:\b', 'C:\Cognita\bin'), 'C:\a;C:\b');
  CheckStr('PathRemovePart first', PathRemovePart('C:\Cognita\bin;C:\b', 'C:\Cognita\bin'), 'C:\b');
  CheckStr('PathRemovePart last', PathRemovePart('C:\a;C:\Cognita\bin', 'C:\Cognita\bin'), 'C:\a');
  CheckStr('PathRemovePart only part', PathRemovePart('C:\Cognita\bin', 'C:\Cognita\bin'), '');
  CheckStr('PathRemovePart absent leaves every part as it was', PathRemovePart('%USERPROFILE%\x;C:\b', 'C:\Cognita\bin'), '%USERPROFILE%\x;C:\b');
  CheckStr('PathRemovePart drops empty parts', PathRemovePart('C:\a;;C:\b', 'C:\zzz'), 'C:\a;C:\b');
  CheckStr('PathRemovePart empty', PathRemovePart('', 'C:\x'), '');
  CheckBool('PathHasPart yes', PathHasPart('C:\a;C:\Cognita\bin\;C:\b', 'c:\cognita\bin'), True);
  CheckBool('PathHasPart no', PathHasPart('C:\a;C:\b', 'C:\Cognita\bin'), False);
  CheckBool('PathHasPart a prefix is not a part', PathHasPart('C:\Cognita\bin2', 'C:\Cognita\bin'), False);
end;

{ ---- UrlInText ---- }
procedure CasesUrl;
begin
  CheckStr('UrlInText sign-in message', UrlInText('Open this link to sign in to Tailscale: https://login.tailscale.com/a/abc123.'), 'https://login.tailscale.com/a/abc123');
  CheckStr('UrlInText none', UrlInText('Still waiting.'), '');
  CheckStr('UrlInText first of two', UrlInText('a http://one.example/x b https://two.example/y'), 'http://one.example/x');
  CheckStr('UrlInText https before http', UrlInText('a https://two.example/y b http://one.example/x'), 'https://two.example/y');
  CheckStr('UrlInText in brackets', UrlInText('see (http://x.example/z) now'), 'http://x.example/z');
  CheckStr('UrlInText stops at a line break', UrlInText('go https://x.example/z' + #13#10 + 'next'), 'https://x.example/z');
  CheckStr('UrlInText scheme alone is nothing', UrlInText('the scheme https:// alone'), '');
  CheckStr('UrlInText upper case scheme', UrlInText('Go HTTPS://X.EXAMPLE/Z now'), 'HTTPS://X.EXAMPLE/Z');
  CheckStr('UrlInText trailing punctuation', UrlInText('link: http://x.example/z;'), 'http://x.example/z');
  CheckStr('UrlInText keeps a query string', UrlInText('open https://x.example/a?b=1&c=2 please'), 'https://x.example/a?b=1&c=2');
end;

{ ---- Version compare (design 19.2 item 6) ---- }
procedure CasesVersion;
begin
  CheckInt('CompareVersions equal', CompareVersions('14.2.2', '14.2.2'), 0);
  CheckInt('CompareVersions older', CompareVersions('14.2.1', '14.2.2'), -1);
  CheckInt('CompareVersions newer', CompareVersions('14.2.3', '14.2.2'), 1);
  CheckInt('CompareVersions 14.10 is newer than 14.9', CompareVersions('14.10.0', '14.9.0'), 1);
  CheckInt('CompareVersions 14.9 is older than 14.10', CompareVersions('14.9', '14.10'), -1);
  CheckInt('CompareVersions 14.10.0 vs 14.9.9', CompareVersions('14.10.0', '14.9.9'), 1);
  CheckInt('CompareVersions major beats minor', CompareVersions('15.0.0', '14.99.99'), 1);
  CheckInt('CompareVersions a missing part is 0', CompareVersions('14.2', '14.2.0'), 0);
  CheckInt('CompareVersions missing part is older than a real one', CompareVersions('14.2', '14.2.1'), -1);
  CheckInt('CompareVersions leading v', CompareVersions('v14.2.2', '14.2.2'), 0);
  CheckInt('CompareVersions suffix after the digits is ignored', CompareVersions('14.2.2-rc1', '14.2.2'), 0);
  CheckInt('CompareVersions empty vs empty', CompareVersions('', ''), 0);
  CheckInt('VersionPart 2 of 14.10.2', VersionPart('14.10.2', 2), 10);
  CheckInt('VersionPart 4 of 14.10.2 is 0', VersionPart('14.10.2', 4), 0);
  CheckBool('SetupIsOlder equal is not older', SetupIsOlder('14.2.2', '14.2.2'), False);
  CheckBool('SetupIsOlder installed older is not older', SetupIsOlder('14.2.0', '14.2.2'), False);
  CheckBool('SetupIsOlder 14.10 installed, 14.9 Setup', SetupIsOlder('14.10.0', '14.9.9'), True);
  CheckBool('SetupIsOlder unknown installed version', SetupIsOlder('', '14.2.2'), False);
  CheckBool('SetupIsOlder unknown Setup version', SetupIsOlder('14.2.2', ''), False);
  CheckStr('DowngradeText', DowngradeText('14.3.0', '14.2.2'),
    'Cognita 14.3.0 is installed; this Setup is older (14.2.2). Use a newer Setup, or "cognita rollback" to go back a release.');
end;

{ ---- Wording by mode (design 19.4 items 10, 18 and 23) ---- }
procedure CasesWording;
begin
  CheckStr('FailHeading fresh', FailHeading(ModeFresh), 'Cognita was not installed');
  CheckStr('FailHeading finish', FailHeading(ModeFinish), 'Cognita was not installed');
  CheckStr('FailHeading update', FailHeading(ModeUpdate), 'Cognita was not updated');
  CheckStr('FailHeading repair', FailHeading(ModeRepair), 'Cognita was not repaired');
  CheckStr('FailHeading reinstall', FailHeading(ModeReinstall), 'Cognita was not reinstalled');
  CheckStr('StillThereText fresh', StillThereText(ModeFresh), '');
  CheckStr('StillThereText finish', StillThereText(ModeFinish), '');
  CheckStr('StillThereText update', StillThereText(ModeUpdate), 'The Cognita you had is still there.');
  CheckStr('StillThereText repair', StillThereText(ModeRepair), 'The Cognita you had is still there.');
  CheckStr('StillThereText reinstall', StillThereText(ModeReinstall), 'The Cognita you had is still there.');
  CheckStr('ProgressCaption fresh', ProgressCaption(ModeFresh), 'Installing Cognita');
  CheckStr('ProgressCaption finish', ProgressCaption(ModeFinish), 'Installing Cognita');
  CheckStr('ProgressCaption update', ProgressCaption(ModeUpdate), 'Updating Cognita');
  CheckStr('ProgressCaption repair', ProgressCaption(ModeRepair), 'Repairing Cognita');
  CheckStr('ProgressCaption reinstall', ProgressCaption(ModeReinstall), 'Reinstalling Cognita');
  CheckStr('RewriteAdvancedHints ports in an update',
    RewriteAdvancedHints('Port 8675 is in use by x.' + #13#10 + 'What to do: Choose other ports under Advanced.', ModeUpdate, True, 'D:'),
    'Port 8675 is in use by x.' + #13#10 + 'What to do: Run Setup again after the update to change ports.');
  CheckStr('RewriteAdvancedHints ports in a repair are left alone',
    RewriteAdvancedHints('Choose other ports under Advanced.', ModeRepair, True, 'D:'), 'Choose other ports under Advanced.');
  CheckStr('RewriteAdvancedHints data location when locked',
    RewriteAdvancedHints('Setup needs 12 GB free on D:\.' + #13#10 + 'What to do: Free space, or choose another data location under Advanced.', ModeRepair, True, 'D:'),
    'Setup needs 12 GB free on D:\.' + #13#10 + 'What to do: Free space on D:.');
  CheckStr('RewriteAdvancedHints data location when it can be changed',
    RewriteAdvancedHints('Free space, or choose another data location under Advanced.', ModeFresh, False, 'D:'),
    'Free space, or choose another data location under Advanced.');
  CheckStr('RewriteAdvancedHints both in an update',
    RewriteAdvancedHints('Choose other ports under Advanced. Free space, or choose another data location under Advanced.', ModeUpdate, True, 'E:'),
    'Run Setup again after the update to change ports. Free space on E:.');
  CheckStr('RewriteAdvancedHints without a drive',
    RewriteAdvancedHints('choose another data location under Advanced', ModeReinstall, True, ''), 'free space on that drive');
  CheckStr('ReplaceNoCase keeps a lower case start', ReplaceNoCase('please choose x now', 'CHOOSE X', 'do y'), 'please do y now');
end;

{ ---- Data location checks (design 19.7 item 16) ---- }
procedure CasesLocation;
begin
  CheckBool('IsDriveRoot D:', IsDriveRoot('D:'), True);
  CheckBool('IsDriveRoot D:\', IsDriveRoot('D:\'), True);
  CheckBool('IsDriveRoot d:/', IsDriveRoot('d:/'), True);
  CheckBool('IsDriveRoot with spaces', IsDriveRoot('  E:\  '), True);
  CheckBool('IsDriveRoot a folder', IsDriveRoot('D:\Cognita'), False);
  CheckBool('IsDriveRoot empty', IsDriveRoot(''), False);
  CheckBool('PathIsUnder inside', PathIsUnder('C:\Users\x\OneDrive\data', 'c:\users\x\onedrive'), True);
  CheckBool('PathIsUnder itself', PathIsUnder('C:\Users\x\OneDrive\', 'C:\Users\x\OneDrive'), True);
  CheckBool('PathIsUnder a sibling with the same prefix', PathIsUnder('C:\Users\x\OneDrive2', 'C:\Users\x\OneDrive'), False);
  CheckBool('PathIsUnder empty root', PathIsUnder('C:\x', ''), False);
  CheckBool('IsOneDrivePath by variable', IsOneDrivePath('D:\Sync\Cognita', 'D:\Sync', '', ''), True);
  CheckBool('IsOneDrivePath by folder name', IsOneDrivePath('C:\Users\x\OneDrive\Cognita', '', '', ''), True);
  CheckBool('IsOneDrivePath by organization folder name', IsOneDrivePath('C:\Users\x\OneDrive - Contoso\Cognita', '', '', ''), True);
  CheckBool('IsOneDrivePath ordinary folder', IsOneDrivePath('D:\Cognita\data', 'C:\Users\x\OneDrive', '', ''), False);
  CheckBool('IsOneDrivePath a folder that only starts with the word', IsOneDrivePath('D:\OneDriveBackups\data', '', '', ''), False);
end;

{ ---- The smaller helpers Setup uses on every page ---- }
procedure CasesSmall;
var
  P: Integer;
begin
  CheckBool('PortOk 8675', PortOk('8675', P), True);
  CheckInt('PortOk value', P, 8675);
  CheckBool('PortOk too low', PortOk('1023', P), False);
  CheckBool('PortOk too high', PortOk('65536', P), False);
  CheckBool('PortOk not a number', PortOk('abc', P), False);
  CheckBool('PortOk with spaces', PortOk(' 9000 ', P), True);
  CheckStr('FormatBytes MB', FormatBytes(512000000), '512 MB');
  CheckStr('FormatBytes GB', FormatBytes(1440000000), '1.4 GB');
  CheckStr('FormatElapsed', FormatElapsed(125000), '2:05');
  CheckStr('QuoteArg plain', QuoteArg('C:\a b'), '"C:\a b"');
  CheckStr('QuoteArg trailing backslash doubles', QuoteArg('C:\a b\'), '"C:\a b\\"');
  CheckBool('HasBadArgChar quote', HasBadArgChar('a"b'), True);
  CheckBool('HasBadArgChar control character', HasBadArgChar('a' + #9 + 'b'), True);
  CheckBool('HasBadArgChar clean', HasBadArgChar('C:\Docs\x'), False);
  CheckBool('IsTruthy 1', IsTruthy('1'), True);
  CheckBool('IsTruthy On', IsTruthy(' On '), True);
  CheckBool('IsTruthy 0', IsTruthy('0'), False);
  CheckBool('IsTruthy empty', IsTruthy(''), False);
end;

{ ---- NVIDIA acceleration page and the WSL memory check box (design 22.7, 22.9, 22.12) ---- }
procedure CasesAccelPageMode;
var
  Big: Int64;
begin
  Big := StrToInt64('4700000000');   { size_cognita_nvidia, as a build with an NVIDIA image reports it }
  { Update never shows the page, whatever the card says. }
  CheckInt('AccelPageMode update hides (ok, build)', AccelPageMode('ok', Big, ModeUpdate), AccelHidden);
  CheckInt('AccelPageMode update hides (old)', AccelPageMode('old', Big, ModeUpdate), AccelHidden);
  CheckInt('AccelPageMode update hides (ok, no build)', AccelPageMode('ok', 0, ModeUpdate), AccelHidden);
  { The four install modes that can show it. }
  CheckInt('AccelPageMode fresh ok with build offers', AccelPageMode('ok', Big, ModeFresh), AccelOffer);
  CheckInt('AccelPageMode finish ok with build offers', AccelPageMode('ok', Big, ModeFinish), AccelOffer);
  CheckInt('AccelPageMode repair ok with build offers', AccelPageMode('ok', Big, ModeRepair), AccelOffer);
  CheckInt('AccelPageMode reinstall ok with build offers', AccelPageMode('ok', Big, ModeReinstall), AccelOffer);
  CheckInt('AccelPageMode ok without a build', AccelPageMode('ok', 0, ModeFresh), AccelNoBuild);
  CheckInt('AccelPageMode old with a build', AccelPageMode('old', Big, ModeFresh), AccelOldDriver);
  CheckInt('AccelPageMode old without a build is still the driver text', AccelPageMode('old', 0, ModeFresh), AccelOldDriver);
  CheckInt('AccelPageMode none hides', AccelPageMode('none', Big, ModeFresh), AccelHidden);
  CheckInt('AccelPageMode empty hides (the helper did not say)', AccelPageMode('', Big, ModeFresh), AccelHidden);
  CheckInt('AccelPageMode an unknown word hides', AccelPageMode('maybe', Big, ModeFresh), AccelHidden);
  CheckInt('AccelPageMode is case-blind', AccelPageMode(' OK ', Big, ModeRepair), AccelOffer);
end;

procedure CasesAccelDefault;
begin
  CheckStr('AccelDefault fresh offer is nvidia', AccelDefault(AccelOffer, ModeFresh, ''), 'nvidia');
  CheckStr('AccelDefault finish offer is nvidia', AccelDefault(AccelOffer, ModeFinish, ''), 'nvidia');
  CheckStr('AccelDefault repair offer, installed nvidia', AccelDefault(AccelOffer, ModeRepair, 'nvidia'), 'nvidia');
  CheckStr('AccelDefault reinstall offer, installed nvidia', AccelDefault(AccelOffer, ModeReinstall, 'nvidia'), 'nvidia');
  CheckStr('AccelDefault repair offer, installed cpu', AccelDefault(AccelOffer, ModeRepair, 'cpu'), 'cpu');
  CheckStr('AccelDefault repair offer, unknown counts as cpu', AccelDefault(AccelOffer, ModeRepair, ''), 'cpu');
  CheckStr('AccelDefault reinstall offer, unknown counts as cpu', AccelDefault(AccelOffer, ModeReinstall, ''), 'cpu');
  CheckStr('AccelDefault repair offer, installed amd', AccelDefault(AccelOffer, ModeRepair, 'amd'), 'cpu');
  CheckStr('AccelDefault old driver is cpu even when fresh', AccelDefault(AccelOldDriver, ModeFresh, ''), 'cpu');
  CheckStr('AccelDefault no build is cpu even when fresh', AccelDefault(AccelNoBuild, ModeFresh, ''), 'cpu');
  CheckStr('AccelDefault hidden is cpu', AccelDefault(AccelHidden, ModeFresh, ''), 'cpu');
  CheckStr('AccelDefault update is cpu (page hidden)', AccelDefault(AccelHidden, ModeUpdate, 'nvidia'), 'cpu');
  CheckStr('AccelDefault old driver, installed nvidia is cpu', AccelDefault(AccelOldDriver, ModeRepair, 'nvidia'), 'cpu');
end;

procedure CasesAccelArg;
begin
  { Update: never, however it is asked. }
  CheckStr('AccelArg update, page hidden', AccelArg(AccelHidden, ModeUpdate, 'cpu', False, 'nvidia'), '');
  CheckStr('AccelArg update, even if offered and touched', AccelArg(AccelOffer, ModeUpdate, 'nvidia', True, ''), '');
  { Fresh and finish: always explicit. }
  CheckStr('AccelArg fresh, page shown, nvidia', AccelArg(AccelOffer, ModeFresh, 'nvidia', False, ''), ' --acceleration nvidia');
  CheckStr('AccelArg fresh, page shown, cpu picked', AccelArg(AccelOffer, ModeFresh, 'cpu', True, ''), ' --acceleration cpu');
  CheckStr('AccelArg fresh, page hidden is cpu (the user never chose a GPU)', AccelArg(AccelHidden, ModeFresh, 'nvidia', False, ''), ' --acceleration cpu');
  CheckStr('AccelArg fresh, old driver page is cpu', AccelArg(AccelOldDriver, ModeFresh, 'cpu', False, ''), ' --acceleration cpu');
  CheckStr('AccelArg fresh, no build page is cpu', AccelArg(AccelNoBuild, ModeFresh, 'cpu', False, ''), ' --acceleration cpu');
  CheckStr('AccelArg finish, page shown, nvidia', AccelArg(AccelOffer, ModeFinish, 'nvidia', False, ''), ' --acceleration nvidia');
  CheckStr('AccelArg finish, page hidden is cpu', AccelArg(AccelHidden, ModeFinish, 'cpu', False, ''), ' --acceleration cpu');
  CheckStr('AccelArg fresh, nvidia chosen but the page did not offer it falls to cpu', AccelArg(AccelNoBuild, ModeFresh, 'nvidia', False, ''), ' --acceleration cpu');
  { Repair and reinstall, page hidden: keep what is installed. }
  CheckStr('AccelArg repair, page hidden keeps', AccelArg(AccelHidden, ModeRepair, 'cpu', False, 'nvidia'), '');
  CheckStr('AccelArg reinstall, page hidden keeps', AccelArg(AccelHidden, ModeReinstall, 'cpu', False, ''), '');
  { Repair and reinstall, page shown, profile known to be cpu or nvidia: the pick is passed. }
  CheckStr('AccelArg repair, installed nvidia, untouched', AccelArg(AccelOffer, ModeRepair, 'nvidia', False, 'nvidia'), ' --acceleration nvidia');
  CheckStr('AccelArg repair, installed nvidia, switched to cpu', AccelArg(AccelOffer, ModeRepair, 'cpu', True, 'nvidia'), ' --acceleration cpu');
  CheckStr('AccelArg repair, installed cpu, untouched', AccelArg(AccelOffer, ModeRepair, 'cpu', False, 'cpu'), ' --acceleration cpu');
  CheckStr('AccelArg reinstall, installed cpu, switched to nvidia', AccelArg(AccelOffer, ModeReinstall, 'nvidia', True, 'cpu'), ' --acceleration nvidia');
  CheckStr('AccelArg reinstall, installed nvidia, untouched', AccelArg(AccelOffer, ModeReinstall, 'nvidia', False, 'nvidia'), ' --acceleration nvidia');
  { Design 22.12 item 1(b): an unknown profile is never turned into a switch by an untouched default. }
  CheckStr('AccelArg repair, unknown, untouched passes nothing', AccelArg(AccelOffer, ModeRepair, 'cpu', False, ''), '');
  CheckStr('AccelArg reinstall, unknown, untouched passes nothing', AccelArg(AccelOffer, ModeReinstall, 'cpu', False, ''), '');
  CheckStr('AccelArg repair, unknown, user chose nvidia', AccelArg(AccelOffer, ModeRepair, 'nvidia', True, ''), ' --acceleration nvidia');
  CheckStr('AccelArg repair, unknown, user changed it back to cpu is a touch', AccelArg(AccelOffer, ModeRepair, 'cpu', True, ''), ' --acceleration cpu');
  CheckStr('AccelArg repair, installed amd, untouched passes nothing', AccelArg(AccelOffer, ModeRepair, 'cpu', False, 'amd'), '');
  CheckStr('AccelArg repair, installed amd, user chose nvidia', AccelArg(AccelOffer, ModeRepair, 'nvidia', True, 'amd'), ' --acceleration nvidia');
  CheckStr('AccelArg repair, unknown, old driver page, untouched passes nothing', AccelArg(AccelOldDriver, ModeRepair, 'cpu', False, ''), '');
  CheckStr('AccelArg repair, installed nvidia, no build page passes cpu', AccelArg(AccelNoBuild, ModeRepair, 'cpu', False, 'nvidia'), ' --acceleration cpu');
  CheckStr('AccelArg a word the helper never sends counts as unknown', AccelArg(AccelOffer, ModeRepair, 'cpu', False, 'tpu'), '');
end;

procedure CasesAccelLabelAndReady;
begin
  CheckStr('AccelLabel nvidia', AccelLabel('nvidia'), 'NVIDIA GPU');
  CheckStr('AccelLabel amd', AccelLabel('amd'), 'AMD GPU');
  CheckStr('AccelLabel cpu', AccelLabel('cpu'), 'CPU');
  CheckStr('AccelLabel anything else is CPU', AccelLabel('tpu'), 'CPU');
  CheckStr('AccelLabel empty is CPU', AccelLabel(''), 'CPU');
  CheckStr('AccelLabel is case-blind', AccelLabel('NVIDIA'), 'NVIDIA GPU');
  CheckStr('AccelKnown cpu', AccelKnown('cpu'), 'cpu');
  CheckStr('AccelKnown mixed case', AccelKnown(' Nvidia '), 'nvidia');
  CheckStr('AccelKnown unknown word', AccelKnown('tpu'), '');
  CheckStr('AccelKnown empty', AccelKnown(''), '');
  { AccelReadyReason: every page mode, every install mode. }
  CheckStr('AccelReadyReason offered has none', AccelReadyReason(AccelOffer, ModeFresh, 'ok', 'cpu'), '');
  CheckStr('AccelReadyReason old driver', AccelReadyReason(AccelOldDriver, ModeFresh, 'old', 'cpu'), '(NVIDIA driver too old)');
  CheckStr('AccelReadyReason old driver, repair', AccelReadyReason(AccelOldDriver, ModeRepair, 'old', 'cpu'), '(NVIDIA driver too old)');
  CheckStr('AccelReadyReason no build', AccelReadyReason(AccelNoBuild, ModeFinish, 'ok', 'cpu'), '(no NVIDIA build in this version)');
  CheckStr('AccelReadyReason no card, fresh', AccelReadyReason(AccelHidden, ModeFresh, 'none', 'cpu'), '(no NVIDIA graphics card found)');
  CheckStr('AccelReadyReason no card, finish', AccelReadyReason(AccelHidden, ModeFinish, 'none', 'cpu'), '(no NVIDIA graphics card found)');
  CheckStr('AccelReadyReason no card, repair (page hidden)', AccelReadyReason(AccelHidden, ModeRepair, 'none', 'cpu'), '(no NVIDIA graphics card found)');
  CheckStr('AccelReadyReason no card, reinstall (page hidden)', AccelReadyReason(AccelHidden, ModeReinstall, 'none', 'cpu'), '(no NVIDIA graphics card found)');
  CheckStr('AccelReadyReason update has none (no card)', AccelReadyReason(AccelHidden, ModeUpdate, 'none', 'cpu'), '');
  CheckStr('AccelReadyReason update has none (card present)', AccelReadyReason(AccelHidden, ModeUpdate, 'ok', 'cpu'), '');
  CheckStr('AccelReadyReason no reason beside an nvidia line', AccelReadyReason(AccelHidden, ModeRepair, 'none', 'nvidia'), '');
  CheckStr('AccelReadyReason no reason beside unchanged', AccelReadyReason(AccelHidden, ModeReinstall, 'none', ''), '');
  CheckStr('AccelReadyReason hidden with an unknown state says nothing', AccelReadyReason(AccelHidden, ModeFresh, '', 'cpu'), '');
  { AccelEffective / AccelReadyValue: what Ready shows, per mode. }
  CheckStr('AccelEffective update shows the installed profile', AccelEffective(AccelHidden, ModeUpdate, 'cpu', False, 'nvidia'), 'nvidia');
  CheckStr('AccelEffective update, unknown', AccelEffective(AccelHidden, ModeUpdate, 'cpu', False, ''), '');
  CheckStr('AccelEffective fresh, page hidden is cpu', AccelEffective(AccelHidden, ModeFresh, 'cpu', False, ''), 'cpu');
  CheckStr('AccelEffective fresh, nvidia picked', AccelEffective(AccelOffer, ModeFresh, 'nvidia', False, ''), 'nvidia');
  CheckStr('AccelEffective repair, hidden, installed amd', AccelEffective(AccelHidden, ModeRepair, 'cpu', False, 'amd'), 'amd');
  CheckStr('AccelEffective repair, hidden, unknown', AccelEffective(AccelHidden, ModeReinstall, 'cpu', False, ''), '');
  CheckStr('AccelEffective repair, shown, unknown, untouched', AccelEffective(AccelOffer, ModeRepair, 'cpu', False, ''), '');
  CheckStr('AccelEffective repair, shown, unknown, touched', AccelEffective(AccelOffer, ModeRepair, 'nvidia', True, ''), 'nvidia');
  CheckStr('AccelReadyValue nvidia', AccelReadyValue('nvidia'), 'NVIDIA GPU');
  CheckStr('AccelReadyValue cpu', AccelReadyValue('cpu'), 'CPU');
  CheckStr('AccelReadyValue unknown is unchanged', AccelReadyValue(''), 'unchanged');
end;

procedure CasesAccelBytes;
begin
  CheckStr('AccelCognitaBytes cpu', IntToStr(AccelCognitaBytes('cpu', 'ok', 600, 4700)), '600');
  CheckStr('AccelCognitaBytes nvidia', IntToStr(AccelCognitaBytes('nvidia', 'ok', 600, 4700)), '4700');
  CheckStr('AccelCognitaBytes nvidia but the build has no image falls to cpu', IntToStr(AccelCognitaBytes('nvidia', 'ok', 600, 0)), '600');
  CheckStr('AccelCognitaBytes amd is sized like the cpu image', IntToStr(AccelCognitaBytes('amd', 'ok', 600, 4700)), '600');
  CheckStr('AccelCognitaBytes unknown takes the larger (nvidia build)', IntToStr(AccelCognitaBytes('', 'ok', 600, 4700)), '4700');
  CheckStr('AccelCognitaBytes unknown with no NVIDIA card is cpu', IntToStr(AccelCognitaBytes('', 'none', 600, 4700)), '600');
  CheckStr('AccelCognitaBytes unknown, no nvidia build is cpu', IntToStr(AccelCognitaBytes('', 'ok', 600, 0)), '600');
  CheckStr('AccelCognitaBytes unknown, a smaller nvidia figure keeps the larger cpu', IntToStr(AccelCognitaBytes('', 'ok', 5000, 4700)), '5000');
end;

procedure CasesAccelFinishedAndWarning;
begin
  CheckStr('AccelFinishedLine nvidia', AccelFinishedLine('nvidia'), 'Acceleration:  NVIDIA GPU');
  CheckStr('AccelFinishedLine cpu', AccelFinishedLine('cpu'), 'Acceleration:  CPU');
  CheckStr('AccelFinishedLine amd', AccelFinishedLine('amd'), 'Acceleration:  AMD GPU');
  CheckStr('AccelFinishedLine the result did not say: no line', AccelFinishedLine(''), '');
  CheckStr('AccelFinishedLine an unknown word: no line', AccelFinishedLine('tpu'), '');
  CheckStr('WarningFix acceleration stage without a fix', WarningFix('acceleration', ''), 'To try the GPU again, run Setup again and choose NVIDIA.');
  CheckStr('WarningFix acceleration stage keeps its own fix', WarningFix('acceleration', 'Do this.'), 'Do this.');
  CheckStr('WarningFix another stage without a fix gets none', WarningFix('keepalive', ''), '');
  CheckStr('WarningFix another stage keeps its fix', WarningFix('nvidia', 'Run Setup again later.'), 'Run Setup again later.');
  CheckStr('WarningFix no stage, no fix', WarningFix('', ''), '');
end;

procedure CasesWslReclaim;
begin
  CheckBool('WslReclaimBoxVisible unset', WslReclaimBoxVisible('unset'), True);
  CheckBool('WslReclaimBoxVisible set', WslReclaimBoxVisible('set'), False);
  CheckBool('WslReclaimBoxVisible unreadable', WslReclaimBoxVisible('unreadable'), False);
  CheckBool('WslReclaimBoxVisible empty (an older helper)', WslReclaimBoxVisible(''), False);
  CheckBool('WslReclaimBoxVisible case and spaces', WslReclaimBoxVisible(' Unset '), True);
  CheckStr('WslReclaimArg visible and ticked', WslReclaimArg('unset', True), ' --wsl-memory-reclaim');
  CheckStr('WslReclaimArg visible and unticked', WslReclaimArg('unset', False), '');
  CheckStr('WslReclaimArg hidden (set) even if the box says ticked', WslReclaimArg('set', True), '');
  CheckStr('WslReclaimArg hidden (unreadable) even if the box says ticked', WslReclaimArg('unreadable', True), '');
end;

function InitializeSetup: Boolean;
var
  ResultFile: String;
begin
  Result := False;
  ResultFile := ExpandConstant('{param:RESULT|}');
  CaseCount := 0;
  FailCount := 0;
  Report := '';
  try
    CasesPickMode;
    CasesResultValue;
    CasesJsonField;
    CasesRedactToken;
    CasesPath;
    CasesUrl;
    CasesVersion;
    CasesWording;
    CasesLocation;
    CasesSmall;
    CasesSkipProof;
    CasesAccelPageMode;
    CasesAccelDefault;
    CasesAccelArg;
    CasesAccelLabelAndReady;
    CasesAccelBytes;
    CasesAccelFinishedAndWarning;
    CasesWslReclaim;
  except
    { A case that raises is a failure with its message, never a silent stop. }
    FailCount := FailCount + 1;
    Note('FAIL harness exception: ' + GetExceptionMessage);
  end;
  Note('TOTAL ' + IntToStr(CaseCount) + ' FAILED ' + IntToStr(FailCount));
  if ResultFile = '' then
    Log('pure tests: no /RESULT= given; the report was ' + Report)
  else if not SaveStringToFile(ResultFile, Report, False) then
    Log('pure tests: could not write ' + ResultFile);
end;
